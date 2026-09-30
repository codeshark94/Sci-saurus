"""Durable process supervision for autonomous Composer missions.

The Composer owns scientific admission and checkpoints. This module owns the
outer process lifetime: when a child run exits after a recoverable blocker or
an ordinary interruption, it reopens the same project with ``--resume``
semantics. It never fabricates a completed result and never extends the
immutable mission deadline.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import multiprocessing
from pathlib import Path
import re
import sqlite3
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux are the supported runtime
    fcntl = None

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.composer import ComposerRunner


TERMINAL_STATUSES = frozenset({"completed", "candidate_needs_review"})
STOP_REASONS = frozenset({
    "hard_deadline", "required_stage_window_does_not_fit_remaining_deadline",
    "provider_configuration", "missing_stage_input", "stage_quota_exhausted",
    "workflow_validation",
})
SUPERVISOR_SCHEMA_VERSION = "composer-supervisor-3"
DEFAULT_WATCHDOG_SECONDS = 300.0


def _composer_child_entry(workflow, resume, on_progress, result_pipe,
                          additional_seconds=None):
    """Run one Composer attempt in a killable process.

    The parent owns supervision.  A provider or a library call that ignores
    its Python timeout must not be able to keep the only control loop alive
    forever.  ``fork`` keeps the caller's progress callback and test doubles
    available on the supported macOS/Linux runtime; the parent never shares a
    SQLite connection with this child.
    """
    try:
        runner_options = {"resume": resume, "on_progress": on_progress}
        if additional_seconds is not None:
            runner_options["additional_seconds"] = additional_seconds
        runner = ComposerRunner(workflow, **runner_options)
        result_pipe.send({"kind": "result", "result": runner.run()})
    except BaseException as exc:  # the parent turns this into a typed retry
        try:
            result_pipe.send({
                "kind": "exception",
                "type": type(exc).__name__,
                "error": str(exc)[:4096],
            })
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        result_pipe.close()


def _result_fingerprint(result):
    stages = result.get("stages", {}) if isinstance(result, dict) else {}
    if not isinstance(stages, dict):
        stages = {}
    compact = {
        stage_id: {
            "status": value.get("status"),
            "error": _normalize_semantic_value(str(value.get("error", ""))[:512]),
            "review_status": value.get("review_status"),
            "release_blocking": value.get("release_blocking"),
            "result_sha256": value.get("result_sha256"),
            "output_sha256": value.get("output_sha256"),
        }
        for stage_id, value in sorted(stages.items())
        if isinstance(value, dict)
    }
    requests = result.get("active_research_requests", []) if isinstance(result, dict) else []
    request_signatures = sorted({
        _research_request_fingerprint(request)
        for request in requests if isinstance(request, dict)
    }) if isinstance(requests, list) else []
    blockers = result.get("active_blockers", []) if isinstance(result, dict) else []
    if not isinstance(blockers, list):
        blockers = []
    blocker_state = []
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        rate_limit = blocker.get("rate_limit")
        blocker_state.append({
            key: _normalize_semantic_value(blocker.get(key))
            for key in ("stage_id", "reason", "stop_reason", "dimension", "limit",
                        "observed", "recoverable", "failure_class", "watchdog")
            if key in blocker
        } | ({"rate_limit": {
            key: rate_limit.get(key)
            for key in ("provider", "status_code", "provider_error_kind")
            if key in rate_limit
        }} if isinstance(rate_limit, dict) else {}))
    return json.dumps({
        "status": result.get("status") if isinstance(result, dict) else None,
        "stop_reason": result.get("stop_reason") if isinstance(result, dict) else None,
        "stages": compact,
        "active_research_requests": request_signatures,
        "active_blockers": blocker_state,
    }, ensure_ascii=False, sort_keys=True)


def _normalize_semantic_value(value):
    """Drop volatile attempt identity while keeping recovery intent stable."""
    if isinstance(value, str):
        normalized = re.sub(
            r"artifact:[A-Za-z0-9._/-]+@[1-9][0-9]*", "artifact:<version>", value.strip())
        normalized = re.sub(
            r"\b(attempt|cycle|continuation)[-_ ]\d+\b",
            lambda match: match.group(1).casefold() + "-<n>", normalized,
            flags=re.IGNORECASE)
        normalized = re.sub(r"\b[0-9a-f]{32,64}\b", "<digest>", normalized,
                            flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", normalized)
    if isinstance(value, list):
        return [_normalize_semantic_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalize_semantic_value(item)
            for key, item in sorted(value.items())
            if key not in {"id", "created_at", "updated_at", "timestamp"}
        }
    return value


def _research_request_fingerprint(request):
    fields = (
        "kind", "owner", "objective", "success_condition", "evidence_needed",
        "source_stage_id", "target_stage_id", "target_stage_kind", "recovery_mode",
        "repair_policy_revision",
        "repair_commands", "acceptance_checks", "review_directives",
        "experiment_repair_plan", "repair_strategy",
    )
    stable = {
        key: _normalize_semantic_value(request[key])
        for key in fields if key in request
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _remaining(result):
    value = result.get("remaining_seconds") if isinstance(result, dict) else None
    value = _finite_number(value)
    return value if value is not None else 0.0


def _finite_number(value):
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _stop_reason(result):
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("interim_report"), dict):
        return result["interim_report"].get("stop_reason")
    return result.get("stop_reason")


def _has_active_model_rate_limit(result):
    """Keep a model 429 paused until an explicit operator resume."""
    if not isinstance(result, dict):
        return False
    blockers = (result.get("active_blockers") if "active_blockers" in result
                else result.get("blockers", []))
    if not isinstance(blockers, list):
        return False
    for blocker in blockers:
        if (not isinstance(blocker, dict)
                or not isinstance(blocker.get("rate_limit"), dict)
                or blocker["rate_limit"].get("provider") != "model"
                or blocker["rate_limit"].get("status_code") != 429):
            continue
        return True
    return False


def _has_pending_foundry_scientific_repair(result, workflow):
    """Whether resume can turn a durable truncated-patch review into work."""
    if not isinstance(result, dict):
        return False
    context = result.get("context")
    stages = result.get("stages")
    if not isinstance(context, dict) or not isinstance(stages, dict):
        return False
    experiment = context.get("experiment")
    stage = stages.get("experiment")
    if not isinstance(experiment, dict) or not isinstance(stage, dict):
        return False
    recovery = experiment.get("failure_recovery")
    if not isinstance(recovery, dict):
        return False
    dossier_ref = recovery.get("dossier_ref") or experiment.get("failure_dossier_ref")
    error = str(experiment.get("error") or stage.get("error") or "").casefold()
    attempts = stage.get("attempts")
    latest = attempts[-1] if isinstance(attempts, list) and attempts else None
    if not (
            experiment.get("failure_class") == "model_contract"
            and recovery.get("failure_class") == "model_contract"
            and recovery.get("recovery_mode") == "format_repair_then_rerun"
            and experiment.get("format_recovery_dispatched") is True
            and experiment.get("results_status") in (None, "not_executed")
            and isinstance(dossier_ref, str)
            and dossier_ref.startswith(
                "artifact:command/composer/failure-recovery/experiment/attempt-")
            and stage.get("status") in {"blocked", "failed", "paused"}
            and isinstance(latest, dict)
            and latest.get("state") == "failed"
            and latest.get("failure_class") == "model_contract"
            and all(phrase in error for phrase in (
                "capability foundry did not admit a program",
                "program author response was incomplete",
                "finish_reason=length",
            ))):
        return False
    if ComposerRunner._persisted_blocking_foundry_feedback(experiment) is None:
        return False
    workflow_stages = workflow.get("stages") if isinstance(workflow, dict) else None
    experiment_stage = next((item for item in workflow_stages
                             if isinstance(item, dict)
                             and item.get("id") == "experiment"
                             and item.get("kind") == "experiment"), None) \
        if isinstance(workflow_stages, list) else None
    expected_capability_id = ComposerRunner._stage_experiment_capability_id_from_context(
        workflow, context, experiment_stage)
    latest_project_dir = latest.get("project_dir")
    if ComposerRunner._has_executed_experiment_result(
            experiment, expected_capability_id, project_dir=latest_project_dir):
        return False
    return True


class ComposerSupervisor:
    """Keep a Composer process alive across recoverable child exits."""

    def __init__(self, workflow, *, initial_resume=False, initial_additional_seconds=None,
                 poll_seconds=5.0,
                 on_progress=None, process_watchdog=True,
                 watchdog_seconds=DEFAULT_WATCHDOG_SECONDS):
        if not isinstance(workflow, dict):
            raise ValidationError("supervisor workflow must be an object")
        if type(poll_seconds) not in (int, float) or poll_seconds < 0:
            raise ValidationError("supervisor poll_seconds must be nonnegative")
        if type(watchdog_seconds) not in (int, float) or watchdog_seconds < 30:
            raise ValidationError("supervisor watchdog_seconds must be at least 30")
        if type(process_watchdog) is not bool:
            raise ValidationError("supervisor process_watchdog must be Boolean")
        if (initial_additional_seconds is not None
                and (type(initial_additional_seconds) not in (int, float)
                     or not math.isfinite(initial_additional_seconds)
                     or initial_additional_seconds <= 0)):
            raise ValidationError(
                "supervisor initial_additional_seconds must be finite and positive")
        self.workflow = deepcopy(workflow)
        self.initial_resume = bool(initial_resume)
        self.initial_additional_seconds = (
            float(initial_additional_seconds)
            if initial_additional_seconds is not None else None)
        self.poll_seconds = float(poll_seconds)
        self.on_progress = on_progress or (lambda state: None)
        self.process_watchdog = process_watchdog
        self.watchdog_seconds = float(watchdog_seconds)
        self.restart_count = 0
        self.last_fingerprint = None
        self.identical_exit_count = 0
        self._project_lock = None

    @property
    def project_root(self):
        return Path(self.workflow["project_id"]).resolve()

    def _acquire_project_lock(self):
        """Prevent two supervisors from mutating one Composer ledger."""
        if fcntl is None:
            return
        lock_path = self.project_root / "state" / "supervisor.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise ValidationError(
                f"Composer workflow is already supervised: {self.workflow.get('id')}"
            ) from exc
        self._project_lock = handle

    def _release_project_lock(self):
        handle = self._project_lock
        self._project_lock = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _write_state(self, *, child_status, action, result=None, error=None,
                     watchdog=None):
        """Expose supervision without pretending the child is still running."""
        output = self.project_root / "output"
        output.mkdir(parents=True, exist_ok=True)
        state = {
            "schema_version": SUPERVISOR_SCHEMA_VERSION,
            "workflow_id": self.workflow.get("id"),
            "status": "stopped" if action == "stop" else "supervising",
            "child_status": child_status,
            "action": action,
            "restart_count": self.restart_count,
            "identical_exit_count": self.identical_exit_count,
            "updated_at_epoch": time.time(),
        }
        if isinstance(result, dict):
            state.update({
                "phase": result.get("phase"),
                "remaining_seconds": result.get("remaining_seconds"),
                "continuation_cycles": result.get("continuation_cycles", 0),
            })
        if isinstance(watchdog, dict):
            state["watchdog"] = deepcopy(watchdog)
        if error:
            state["error"] = str(error)[:2048]
        temporary = output / "supervisor-state.json.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2))
        temporary.replace(output / "supervisor-state.json")

    @staticmethod
    def _stage_progress_signature(project_dir):
        """Return semantic signals from the currently admitted stage.

        A Composer heartbeat is intentionally cheap and can continue while a
        nested survey/experiment runner is wedged.  Stage runners already
        expose a small progress projection and an event ledger, so the outer
        watchdog reads those durable signals instead of treating file mtime as
        scientific progress.
        """
        if not isinstance(project_dir, str) or not project_dir.strip():
            return None
        root = Path(project_dir).expanduser()
        progress_path = root / "output" / "progress.json"
        progress = {}
        try:
            progress = json.loads(progress_path.read_text())
            if not isinstance(progress, dict):
                progress = {}
        except (OSError, TypeError, ValueError):
            progress = {}
        semantic_progress = (
            progress.get("phase"),
            progress.get("checkpoint"),
            tuple(progress.get("active_tasks", []))
            if isinstance(progress.get("active_tasks"), list) else (),
            len(progress.get("information_changes", []))
            if isinstance(progress.get("information_changes"), list) else None,
            len(progress.get("verified_changes", []))
            if isinstance(progress.get("verified_changes"), list) else None,
            len(progress.get("blockers", []))
            if isinstance(progress.get("blockers"), list) else None,
        )
        database = root / "state" / "control.sqlite"
        event_seq = None
        active_tasks = ()
        try:
            connection = sqlite3.connect(database, timeout=0.2)
            event_row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events"
            ).fetchone()
            event_seq = event_row[0] if event_row else 0
            task_rows = connection.execute(
                "SELECT task_id, state FROM tasks "
                "WHERE state IN ('queued', 'running', 'awaiting_review') "
                "ORDER BY updated_at DESC LIMIT 16"
            ).fetchall()
            active_tasks = tuple((row[0], row[1]) for row in task_rows)
            connection.close()
        except (OSError, sqlite3.Error):
            pass
        if progress == {} and event_seq is None:
            return None
        return {
            "project_dir": str(root),
            "progress": semantic_progress,
            "event_seq": event_seq,
            "active_tasks": active_tasks,
        }

    def _live_snapshot(self):
        """Read small durable signals used by the outer watchdog.

        ``heartbeat`` is deliberately excluded from ``signature``.  It proves
        that the Composer thread is alive, while ``signature`` must change only
        when the run's stage, ledger, usage, or nested operation state changes.
        This prevents a ticker from masking a semantic stall.
        """
        output = self.project_root / "output"
        progress_path = output / "progress.json"
        progress = {}
        progress_stat = None
        try:
            progress_stat = progress_path.stat()
            progress = json.loads(progress_path.read_text())
            if not isinstance(progress, dict):
                progress = {}
        except (OSError, TypeError, ValueError):
            progress = {}
        database = self.project_root / "state" / "control.sqlite"
        database_stat = None
        event_seq = None
        active_attempts = []
        try:
            database_stat = database.stat()
            connection = sqlite3.connect(database, timeout=0.2)
            event_row = connection.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events"
            ).fetchone()
            event_seq = event_row[0] if event_row else 0
            rows = connection.execute(
                "SELECT a.task_id, a.state, a.created_at "
                "FROM attempts a WHERE a.state IN ('started', 'running') "
                "ORDER BY a.created_at DESC LIMIT 16"
            ).fetchall()
            connection.close()
            active_attempts = [
                {"task_id": row[0], "state": row[1], "created_at": row[2]}
                for row in rows
            ]
        except (OSError, sqlite3.Error):
            active_attempts = []
        stage_signals = []
        stages = progress.get("stages", {}) if isinstance(progress, dict) else {}
        if isinstance(stages, dict):
            for stage_id, stage in sorted(stages.items()):
                if not isinstance(stage, dict):
                    continue
                signal = self._stage_progress_signature(stage.get("project_dir"))
                if signal is not None:
                    stage_signals.append((stage_id, signal))
        semantic_signature = (
            progress.get("phase") if isinstance(progress, dict) else None,
            progress.get("status") if isinstance(progress, dict) else None,
            progress.get("state_revision") if isinstance(progress, dict) else None,
            progress.get("continuation_cycles") if isinstance(progress, dict) else None,
            tuple(
                (stage_id, stage.get("status"), stage.get("attempt_count"))
                for stage_id, stage in sorted(stages.items())
                if isinstance(stage, dict)
            ) if isinstance(stages, dict) else (),
            tuple(
                (key, progress.get("usage", {}).get(key))
                for key in ("model_calls", "input_tokens", "output_tokens", "openalex_requests")
            ) if isinstance(progress.get("usage"), dict) else (),
            event_seq,
            tuple((row["task_id"], row["state"]) for row in active_attempts),
            tuple(
                (stage_id, signal["progress"], signal["event_seq"], signal["active_tasks"])
                for stage_id, signal in stage_signals
            ),
        )
        signature = (
            semantic_signature,
        )
        return {
            "progress": progress,
            "active_attempts": active_attempts,
            "stage_signals": stage_signals,
            "heartbeat_mtime_ns": progress_stat.st_mtime_ns if progress_stat else None,
            "database_mtime_ns": database_stat.st_mtime_ns if database_stat else None,
            "semantic_signature": semantic_signature,
            "signature": signature,
        }

    def _watchdog_remaining(self, snapshot):
        progress = snapshot.get("progress", {}) if isinstance(snapshot, dict) else {}
        value = progress.get("remaining_seconds") if isinstance(progress, dict) else None
        value = _finite_number(value)
        if value is not None:
            return max(0.0, value)
        hard_seconds = _finite_number(self.workflow.get("time_policy", {}).get("hard_seconds", 0))
        return max(0.0, hard_seconds if hard_seconds is not None else 0.0)

    def _watchdog_result(self, snapshot, stale_seconds):
        progress = snapshot.get("progress", {}) if isinstance(snapshot, dict) else {}
        if not isinstance(progress, dict):
            progress = {}
        phase = progress.get("phase") or "workflow"
        active = snapshot.get("active_attempts", []) if isinstance(snapshot, dict) else []
        return {
            "status": "blocked",
            "phase": phase,
            "remaining_seconds": self._watchdog_remaining(snapshot),
            "stages": deepcopy(progress.get("stages", {})),
            "active_research_requests": deepcopy(
                progress.get("active_research_requests", [])),
            "retry_schedule": deepcopy(progress.get("retry_schedule", {})),
            "blockers": [{
                "stage_id": phase.split(":", 1)[0],
                "reason": (
                    "Composer watchdog terminated an in-flight process after "
                    f"{stale_seconds:.1f}s without a durable heartbeat"
                ),
                "watchdog": True,
                "active_attempts": [item.get("task_id") for item in active],
            }],
        }

    @staticmethod
    def _scheduled_retry_wait(snapshot):
        """Return whether the child is intentionally sleeping until a retry.

        A Composer retry can wait for a provider reset for hours while still
        making the correct durable decision every checkpoint. The watchdog
        must not kill that process merely because the semantic state is
        unchanged; doing so discards the persisted retry fence and can
        redispatch the same provider-blocked packet.
        """
        if not isinstance(snapshot, dict):
            return False
        progress = snapshot.get("progress", {})
        if not isinstance(progress, dict) or progress.get("status") != "running":
            return False
        if snapshot.get("active_attempts"):
            return False
        schedule = progress.get("retry_schedule")
        if not isinstance(schedule, dict):
            return False
        now = time.time()
        return any(
            isinstance(item, dict)
            and isinstance(item.get("not_before_epoch"), (int, float))
            and item["not_before_epoch"] > now
            for item in schedule.values()
        )

    @staticmethod
    def _admitted_survey_fallback_ready(result):
        """Recognize an OpenAlex pause whose approved Crossref route is ready.

        The Composer records the route change before it exits the paused child.
        Waiting on the old provider's reset after that point only delays the
        next resume; the durable context already selects the metadata fallback.
        Only the current survey blocker is bypassable; historical or unrelated
        provider cooldowns must retain their own retry schedule.
        """
        if not isinstance(result, dict):
            return False
        context = result.get("context")
        stages = result.get("stages")
        if not isinstance(context, dict) or not isinstance(stages, dict):
            return False
        if "active_blockers" in result:
            blockers = result.get("active_blockers")
            if not isinstance(blockers, list):
                return False
        else:
            blockers = result.get("blockers", [])
        if not isinstance(blockers, list):
            return False

        def is_openalex_cooldown(blocker):
            if not isinstance(blocker, dict) or blocker.get("reason") != "provider_cooldown":
                return False
            provider_error = str(blocker.get("provider_error", "")).casefold()
            rate_limit = blocker.get("rate_limit")
            if isinstance(rate_limit, dict):
                provider = rate_limit.get("provider")
                if provider is not None and str(provider).strip():
                    return str(provider).casefold() == "openalex"
            return "openalex" in provider_error

        openalex_blockers = [blocker for blocker in blockers if is_openalex_cooldown(blocker)]
        if not openalex_blockers:
            return False

        fallback_stages = set()
        for blocker in openalex_blockers:
            stage_id = blocker.get("stage_id")
            if not isinstance(stage_id, str):
                continue
            stage_context = context.get(stage_id)
            stage = stages.get(stage_id)
            if not isinstance(stage_context, dict) or not isinstance(stage, dict):
                continue
            fallback = stage_context.get("provider_fallback")
            if not isinstance(fallback, dict):
                continue
            if (
                fallback.get("status") == "admitted"
                and fallback.get("mode") == "crossref_metadata"
                and fallback.get("source_provider") == "openalex"
                and stage.get("kind") == "survey"
                and stage.get("status") in {"paused", "retrying"}
            ):
                fallback_stages.add(stage_id)
        if not fallback_stages:
            return False

        now_epoch = time.time()
        for blocker in blockers:
            if not isinstance(blocker, dict):
                continue
            retry_epoch = _finite_number(blocker.get("retry_after_epoch"))
            if retry_epoch is not None:
                delay = max(0.0, retry_epoch - now_epoch)
            else:
                retry_seconds = _finite_number(blocker.get("retry_after_seconds"))
                delay = max(0.0, retry_seconds) if retry_seconds is not None else 0.0
            if delay <= 0:
                continue
            if blocker.get("stage_id") not in fallback_stages or not is_openalex_cooldown(blocker):
                return False
        return True

    def _wait_before_resume(self, result):
        remaining = _remaining(result)
        if remaining <= 0:
            return False
        if self._admitted_survey_fallback_ready(result):
            self._write_state(
                child_status=result.get("status"),
                action="resume_admitted_provider_fallback",
                result=result,
            )
            return True
        # Identical blocker fingerprints back off rather than hammering the
        # same provider or stage. A changed checkpoint resets the delay.
        if self.poll_seconds == 0:
            delay = 0.0
        else:
            delay = min(60.0, max(self.poll_seconds, 2.0) * (2 ** min(self.identical_exit_count - 1, 5)))
        retry_after = None
        active_blockers = result.get("active_blockers")
        if isinstance(active_blockers, list):
            blockers = active_blockers
        elif "active_blockers" not in result:
            blockers = result.get("blockers", [])
        else:
            historical = result.get("blockers", [])
            blockers = [
                blocker for blocker in historical
                if isinstance(blocker, dict)
                and _finite_number(blocker.get("retry_after_epoch")) is not None
            ] if isinstance(historical, list) else []
        if not isinstance(blockers, list):
            blockers = []
        now_epoch = time.time()
        for blocker in blockers:
            if not isinstance(blocker, dict):
                continue
            retry_epoch = _finite_number(blocker.get("retry_after_epoch"))
            if retry_epoch is not None:
                value = max(0.0, retry_epoch - now_epoch)
            else:
                retry_seconds = _finite_number(blocker.get("retry_after_seconds"))
                if retry_seconds is None:
                    continue
                value = max(0.0, retry_seconds)
            if value > 0:
                retry_after = max(retry_after or 0.0, value)
        if retry_after is not None:
            delay = max(delay, retry_after)
        delay = min(delay, max(0.0, remaining))
        self._write_state(child_status=result.get("status"), action="waiting_to_resume", result=result)
        deadline = time.monotonic() + delay
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return True
            time.sleep(min(5.0, left))

    def _should_resume(self, result):
        if not isinstance(result, dict):
            return True
        if _has_active_model_rate_limit(result):
            return False
        active_blockers = result.get("active_blockers")
        if not isinstance(active_blockers, list):
            active_blockers = result.get("blockers", [])
        if isinstance(active_blockers, list) and any(
                isinstance(blocker, dict)
                and blocker.get("failure_class") == "harness_bug"
                for blocker in active_blockers):
            # Runtime defects require a source fix and a fresh process import.
            # Re-forking the same loaded supervisor would repeat the defect
            # and could burn more provider calls without changing evidence.
            return False
        status = result.get("status")
        if _remaining(result) <= 0:
            return False
        pending_foundry_repair = _has_pending_foundry_scientific_repair(
            result, self.workflow)
        # A repeated blocked summary is normally terminal for this supervisor
        # invocation. A persisted methods finding plus a truncated author
        # response is different: Composer can reconcile that checkpoint into
        # a source-repair order without replaying the experiment. Do not let
        # the generic identical-exit guard hide that one durable recovery.
        if self.identical_exit_count >= 1 and not pending_foundry_repair:
            return False
        stop_reason = _stop_reason(result)
        if stop_reason == "stage_quota_exhausted":
            context = result.get("context")
            stages = result.get("stages")
            if not isinstance(context, dict) or not isinstance(stages, dict):
                return False
            pending_quota_recovery = any(
                isinstance(stage_context, dict)
                and stage_context.get("review_status") == "stage_quota_exhausted"
                and isinstance(stage_context.get("quota_recovery"), dict)
                and stage_context["quota_recovery"].get("status") == "required"
                and stage_id in stages
                for stage_id, stage_context in context.items()
            )
            if not pending_quota_recovery:
                return False
        elif stop_reason in STOP_REASONS:
            return False
        if status in {
                "candidate_needs_review", "research_expansion_required", "review_rejected"}:
            requests = result.get("active_research_requests")
            return isinstance(requests, list) and any(
                isinstance(request, dict)
                and isinstance(request.get("objective"), str)
                and request["objective"].strip()
                for request in requests
            )
        if status in TERMINAL_STATUSES:
            return False
        if status not in {"blocked", "paused", "failed"}:
            return False
        requests = result.get("active_research_requests")
        if isinstance(requests, list) and any(
                isinstance(request, dict)
                and isinstance(request.get("objective"), str)
                and request["objective"].strip()
                for request in requests):
            return True
        if self._admitted_survey_fallback_ready(result):
            return True
        blockers = (result.get("active_blockers") if "active_blockers" in result
                    else result.get("blockers", []))
        if isinstance(blockers, list):
            for blocker in blockers:
                if not isinstance(blocker, dict):
                    continue
                rate_limit = blocker.get("rate_limit")
                if (blocker.get("reason") == "provider_cooldown"
                        and isinstance(rate_limit, dict)
                        and rate_limit.get("provider") == "model"):
                    return False
                if blocker.get("reason") == "provider_cooldown":
                    retry_epoch = _finite_number(blocker.get("retry_after_epoch"))
                    retry_seconds = _finite_number(blocker.get("retry_after_seconds"))
                    if ((retry_epoch is not None and retry_epoch > time.time())
                            or (retry_seconds is not None and retry_seconds > 0)):
                        return True
                if blocker.get("watchdog") is True and blocker.get("active_attempts"):
                    return True
                if blocker.get("recoverable") is True:
                    return True
        return pending_foundry_repair

    def _child_exception_text(self, message):
        """Convert a child exception message without reviving an interrupt."""
        if isinstance(message, dict) and message.get("type") == "KeyboardInterrupt":
            self._write_state(
                child_status="interrupted", action="stop",
                error=message.get("error") or "Composer child interrupted",
            )
            raise KeyboardInterrupt(message.get("error") or "Composer child interrupted")
        if isinstance(message, dict):
            return f"{message.get('type', 'ComposerError')}: {message.get('error', '')}"
        return "Composer child exited without a result"

    def _child_exception_result(self, message):
        """Return a typed stop for deterministic child construction failures."""
        error = self._child_exception_text(message)
        exception_type = message.get("type") if isinstance(message, dict) else None
        stop_reason = "workflow_validation" if exception_type == "ValidationError" else None
        blocker = {"stage_id": "workflow", "reason": error}
        if stop_reason is not None:
            blocker.update({"stop_reason": stop_reason, "recoverable": False})
        else:
            blocker.update({"recoverable": True, "failure_class": "child_exception"})
        return {
            "status": "blocked",
            "stop_reason": stop_reason,
            "remaining_seconds": self._watchdog_remaining(self._live_snapshot()),
            "blockers": [blocker],
        }

    def _run_in_process(self):
        resume = self.initial_resume or (self.project_root / "state" / "control.sqlite").is_file()
        additional_seconds = self.initial_additional_seconds
        while True:
            self._write_state(child_status="starting", action="dispatch", result=None)
            try:
                runner_options = {"resume": resume, "on_progress": self.on_progress}
                if additional_seconds is not None:
                    runner_options["additional_seconds"] = additional_seconds
                runner = ComposerRunner(self.workflow, **runner_options)
                additional_seconds = None
                result = runner.run()
            except KeyboardInterrupt:
                self._mark_interrupted_checkpoint()
                self._write_state(child_status="interrupted", action="stop")
                raise
            except Exception as exc:
                # A constructor/control-plane crash is restartable only while
                # the mission wall remains live. Keep the error visible and
                # let the next resume reconcile durable child attempts.
                result = {
                    "status": "blocked",
                    "remaining_seconds": max(
                    0.0, float(self.workflow.get("time_policy", {}).get("hard_seconds", 0))),
                    "blockers": [{"stage_id": "workflow",
                                  "reason": f"{type(exc).__name__}: {exc}",
                                  "recoverable": not isinstance(exc, ValidationError),
                                  "failure_class": "child_exception"}],
                }
                if isinstance(exc, ValidationError):
                    result["stop_reason"] = "workflow_validation"
                    result["blockers"][0].update({
                        "stop_reason": "workflow_validation",
                        "recoverable": False,
                    })
                self._write_state(child_status="crashed", action="retry_after_crash", result=result,
                                  error=exc)
            self.restart_count += 1
            fingerprint = _result_fingerprint(result)
            if fingerprint == self.last_fingerprint:
                self.identical_exit_count += 1
            else:
                self.identical_exit_count = 0
            self.last_fingerprint = fingerprint
            if not self._should_resume(result):
                self._write_state(child_status=result.get("status"), action="stop", result=result)
                return result
            if not self._wait_before_resume(result):
                self._write_state(child_status=result.get("status"), action="stop", result=result)
                return result
            resume = True

    def _mark_interrupted_checkpoint(self):
        """Make a foreground stop visible before the next explicit resume."""
        output = self.project_root / "output"
        progress_path = output / "progress.json"
        try:
            progress = json.loads(progress_path.read_text())
        except (OSError, TypeError, ValueError):
            progress = None
        active_run_id = progress.get("run_id") if isinstance(progress, dict) else None
        for name in ("progress.json", "run.json", "interim_report.json"):
            path = output / name
            try:
                value = json.loads(path.read_text())
            except (OSError, TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            if (name != "progress.json" and isinstance(active_run_id, str)
                    and value.get("run_id") != active_run_id):
                # A prior terminal report is not the interrupted checkpoint.
                # Leave it as an honest historical record instead of making
                # it appear to describe the newest run.
                continue
            value["status"] = "paused"
            if name == "progress.json":
                value["phase"] = "paused"
            blockers = value.setdefault("blockers", [])
            if not isinstance(blockers, list):
                blockers = []
                value["blockers"] = blockers
            if not any(isinstance(item, dict)
                       and item.get("stage_id") == "workflow"
                       and item.get("stop_reason") == "process_interrupted"
                       for item in blockers):
                blockers.append({
                    "stage_id": "workflow",
                    "reason": "KeyboardInterrupt: termination requested",
                    "stop_reason": "process_interrupted",
                })
            value["updated_at_epoch"] = time.time()
            temporary = path.with_name(path.name + ".tmp")
            try:
                temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True))
                temporary.replace(path)
            except OSError:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _run_one_process(self, resume, *, additional_seconds=None):
        """Run one Composer attempt while the parent remains killable."""
        try:
            context = multiprocessing.get_context("fork")
        except ValueError:
            # The project is developed on macOS/Linux.  Keep a portable
            # fallback for runtimes without fork rather than silently losing
            # the original retry semantics.
            return self._run_one_in_process(
                resume, additional_seconds=additional_seconds)
        parent_pipe, child_pipe = context.Pipe(duplex=False)
        child = context.Process(
            target=_composer_child_entry,
            args=(self.workflow, resume, self.on_progress, child_pipe,
                  additional_seconds),
            name=f"scisaurus-composer-{self.workflow.get('id', 'run')}",
        )
        child.start()
        child_pipe.close()
        message = None
        last_signature = None
        last_activity = time.monotonic()
        monitor_interval = max(0.25, min(5.0, self.poll_seconds or 0.25))
        try:
            while child.is_alive():
                while parent_pipe.poll():
                    try:
                        message = parent_pipe.recv()
                    except EOFError:
                        break
                snapshot = self._live_snapshot()
                signature = snapshot.get("signature")
                if signature != last_signature:
                    last_signature = signature
                    last_activity = time.monotonic()
                stale = time.monotonic() - last_activity
                scheduled_wait = self._scheduled_retry_wait(snapshot)
                self._write_state(
                    child_status="running", action="monitoring",
                    result=snapshot.get("progress"),
                    watchdog={
                        "process_pid": child.pid,
                        "stale_seconds": round(stale, 3),
                        "semantic_stale_seconds": round(stale, 3),
                        "heartbeat_stale_seconds": (
                            round(max(0.0, time.time() - (
                                snapshot["heartbeat_mtime_ns"] / 1_000_000_000
                            )), 3)
                            if snapshot.get("heartbeat_mtime_ns") else None
                        ),
                        "limit_seconds": self.watchdog_seconds,
                        "active_attempt_count": len(snapshot.get("active_attempts", [])),
                        "stage_signal_count": len(snapshot.get("stage_signals", [])),
                    },
                )
                # A provider call can legitimately have no semantic
                # checkpoint for several minutes while its durable attempt is
                # still leased.  The stage/deadline and provider timeout own
                # that call; killing the child here would turn a slow valid
                # response into a result-unknown retry.  Only the no-attempt
                # case is a supervisor-level stall.
                active_attempts = snapshot.get("active_attempts", [])
                if (stale >= self.watchdog_seconds
                        and not scheduled_wait
                        and not active_attempts):
                    result = self._watchdog_result(snapshot, stale)
                    self._write_state(
                        child_status="watchdog_terminated",
                        action="retry_after_watchdog", result=result,
                        watchdog={
                            "process_pid": child.pid,
                            "stale_seconds": round(stale, 3),
                            "semantic_stale_seconds": round(stale, 3),
                            "limit_seconds": self.watchdog_seconds,
                            "active_attempt_count": len(snapshot.get("active_attempts", [])),
                            "stage_signal_count": len(snapshot.get("stage_signals", [])),
                        },
                    )
                    child.terminate()
                    child.join(timeout=5.0)
                    if child.is_alive():
                        child.kill()
                        child.join(timeout=5.0)
                    return result
                time.sleep(monitor_interval)
            child.join(timeout=5.0)
            while parent_pipe.poll():
                try:
                    message = parent_pipe.recv()
                except EOFError:
                    break
        except KeyboardInterrupt:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5.0)
            self._mark_interrupted_checkpoint()
            self._write_state(child_status="interrupted", action="stop")
            raise
        finally:
            parent_pipe.close()
        if isinstance(message, dict) and message.get("kind") == "result":
            return message.get("result")
        if isinstance(message, dict) and message.get("kind") == "exception":
            # A terminal interrupt can arrive at the Composer child rather
            # than the supervisor process when both share the launcher's
            # foreground input.  Do not reinterpret that explicit stop as a
            # recoverable blocker: doing so leaves the supervisor alive and
            # dispatches a second Composer against the same checkpoint.
            return self._child_exception_result(message)
        else:
            error = f"Composer child exited without a result (exitcode={child.exitcode})"
        return {
            "status": "blocked",
            "remaining_seconds": self._watchdog_remaining(self._live_snapshot()),
            "blockers": [{"stage_id": "workflow", "reason": error}],
        }

    def _run_one_in_process(self, resume, *, additional_seconds=None):
        try:
            runner_options = {"resume": resume, "on_progress": self.on_progress}
            if additional_seconds is not None:
                runner_options["additional_seconds"] = additional_seconds
            runner = ComposerRunner(self.workflow, **runner_options)
            return runner.run()
        except KeyboardInterrupt:
            self._write_state(child_status="interrupted", action="stop")
            raise
        except Exception as exc:
            result = {
                "status": "blocked",
                "remaining_seconds": max(
                    0.0, float(self.workflow.get("time_policy", {}).get("hard_seconds", 0))),
                "blockers": [{"stage_id": "workflow", "reason": f"{type(exc).__name__}: {exc}"}],
            }
            if isinstance(exc, ValidationError):
                result["stop_reason"] = "workflow_validation"
                result["blockers"][0].update({
                    "stop_reason": "workflow_validation",
                    "recoverable": False,
                })
            self._write_state(child_status="crashed", action="retry_after_crash", result=result,
                              error=exc)
            return result

    def run(self):
        self._acquire_project_lock()
        try:
            if not self.process_watchdog:
                return self._run_in_process()
            resume = self.initial_resume or (self.project_root / "state" / "control.sqlite").is_file()
            additional_seconds = self.initial_additional_seconds
            while True:
                self._write_state(child_status="starting", action="dispatch", result=None)
                result = self._run_one_process(
                    resume, additional_seconds=additional_seconds)
                additional_seconds = None
                self.restart_count += 1
                fingerprint = _result_fingerprint(result)
                if fingerprint == self.last_fingerprint:
                    self.identical_exit_count += 1
                else:
                    self.identical_exit_count = 0
                self.last_fingerprint = fingerprint
                if not self._should_resume(result):
                    self._write_state(child_status=result.get("status"), action="stop", result=result)
                    return result
                if not self._wait_before_resume(result):
                    self._write_state(child_status=result.get("status"), action="stop", result=result)
                    return result
                resume = True
        finally:
            self._release_project_lock()


def supervise_composer(workflow, *, initial_resume=False, initial_additional_seconds=None,
                       poll_seconds=5.0,
                       on_progress=None, process_watchdog=True,
                       watchdog_seconds=DEFAULT_WATCHDOG_SECONDS):
    """Convenience entry point used by the CLI and the local launch script."""
    return ComposerSupervisor(
        workflow, initial_resume=initial_resume,
        initial_additional_seconds=initial_additional_seconds,
        poll_seconds=poll_seconds,
        on_progress=on_progress, process_watchdog=process_watchdog,
        watchdog_seconds=watchdog_seconds,
    ).run()
