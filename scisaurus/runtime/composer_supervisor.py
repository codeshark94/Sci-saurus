"""Durable process supervision for autonomous Composer missions.

The Composer owns scientific admission and checkpoints. This module owns the
outer process lifetime: when a child run exits after a recoverable blocker or
an ordinary interruption, it reopens the same project with ``--resume``
semantics. It never fabricates a completed result and never extends the
immutable mission deadline.
"""

from __future__ import annotations

from copy import deepcopy
import json
import multiprocessing
from pathlib import Path
import sqlite3
import time

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.composer import ComposerRunner


TERMINAL_STATUSES = frozenset({"completed", "candidate_needs_review"})
STOP_REASONS = frozenset({
    "hard_deadline", "required_stage_window_does_not_fit_remaining_deadline",
    "provider_configuration", "missing_stage_input", "stage_quota_exhausted",
})
SUPERVISOR_SCHEMA_VERSION = "composer-supervisor-3"
DEFAULT_WATCHDOG_SECONDS = 300.0


def _composer_child_entry(workflow, resume, on_progress, result_pipe):
    """Run one Composer attempt in a killable process.

    The parent owns supervision.  A provider or a library call that ignores
    its Python timeout must not be able to keep the only control loop alive
    forever.  ``fork`` keeps the caller's progress callback and test doubles
    available on the supported macOS/Linux runtime; the parent never shares a
    SQLite connection with this child.
    """
    try:
        runner = ComposerRunner(workflow, resume=resume, on_progress=on_progress)
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
            "attempt_count": value.get("attempt_count"),
            "error": str(value.get("error", ""))[:512],
        }
        for stage_id, value in sorted(stages.items())
        if isinstance(value, dict)
    }
    return json.dumps({
        "status": result.get("status") if isinstance(result, dict) else None,
        "stop_reason": result.get("stop_reason") if isinstance(result, dict) else None,
        "stages": compact,
    }, ensure_ascii=False, sort_keys=True)


def _remaining(result):
    value = result.get("remaining_seconds") if isinstance(result, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0


def _stop_reason(result):
    if not isinstance(result, dict):
        return None
    if isinstance(result.get("interim_report"), dict):
        return result["interim_report"].get("stop_reason")
    return result.get("stop_reason")


class ComposerSupervisor:
    """Keep a Composer process alive across recoverable child exits."""

    def __init__(self, workflow, *, initial_resume=False, poll_seconds=5.0,
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
        self.workflow = deepcopy(workflow)
        self.initial_resume = bool(initial_resume)
        self.poll_seconds = float(poll_seconds)
        self.on_progress = on_progress or (lambda state: None)
        self.process_watchdog = process_watchdog
        self.watchdog_seconds = float(watchdog_seconds)
        self.restart_count = 0
        self.last_fingerprint = None
        self.identical_exit_count = 0

    @property
    def project_root(self):
        return Path(self.workflow["project_id"]).resolve()

    def _write_state(self, *, child_status, action, result=None, error=None,
                     watchdog=None):
        """Expose supervision without pretending the child is still running."""
        output = self.project_root / "output"
        output.mkdir(parents=True, exist_ok=True)
        state = {
            "schema_version": SUPERVISOR_SCHEMA_VERSION,
            "workflow_id": self.workflow.get("id"),
            "status": "supervising",
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
        if isinstance(value, (int, float)):
            return max(0.0, float(value))
        return max(0.0, float(self.workflow.get("time_policy", {}).get("hard_seconds", 0)))

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

    def _wait_before_resume(self, result):
        remaining = _remaining(result)
        if remaining <= 0:
            return False
        # Identical blocker fingerprints back off rather than hammering the
        # same provider or stage. A changed checkpoint resets the delay.
        if self.poll_seconds == 0:
            delay = 0.0
        else:
            delay = min(60.0, max(self.poll_seconds, 2.0) * (2 ** min(self.identical_exit_count - 1, 5)))
        retry_after = None
        for blocker in result.get("blockers", []) if isinstance(result, dict) else []:
            if not isinstance(blocker, dict):
                continue
            value = blocker.get("retry_after_seconds")
            if isinstance(value, (int, float)) and value > 0:
                retry_after = max(retry_after or 0.0, float(value))
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
        status = result.get("status")
        if status in TERMINAL_STATUSES:
            return False
        if _remaining(result) <= 0 or _stop_reason(result) in STOP_REASONS:
            return False
        if status in {"research_expansion_required", "review_rejected"}:
            return bool(result.get("active_research_requests"))
        return status in {"blocked", "paused", "failed"}

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

    def _run_in_process(self):
        resume = self.initial_resume or (self.project_root / "state" / "control.sqlite").is_file()
        while True:
            self._write_state(child_status="starting", action="dispatch", result=None)
            try:
                runner = ComposerRunner(
                    self.workflow, resume=resume, on_progress=self.on_progress)
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
                    "blockers": [{"stage_id": "workflow", "reason": f"{type(exc).__name__}: {exc}"}],
                }
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
        for name in ("progress.json", "run.json", "interim_report.json"):
            path = output / name
            try:
                value = json.loads(path.read_text())
            except (OSError, TypeError, ValueError):
                continue
            if not isinstance(value, dict):
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

    def _run_one_process(self, resume):
        """Run one Composer attempt while the parent remains killable."""
        try:
            context = multiprocessing.get_context("fork")
        except ValueError:
            # The project is developed on macOS/Linux.  Keep a portable
            # fallback for runtimes without fork rather than silently losing
            # the original retry semantics.
            return self._run_one_in_process(resume)
        parent_pipe, child_pipe = context.Pipe(duplex=False)
        child = context.Process(
            target=_composer_child_entry,
            args=(self.workflow, resume, self.on_progress, child_pipe),
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
                if stale >= self.watchdog_seconds and not scheduled_wait:
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
            error = self._child_exception_text(message)
        else:
            error = f"Composer child exited without a result (exitcode={child.exitcode})"
        return {
            "status": "blocked",
            "remaining_seconds": self._watchdog_remaining(self._live_snapshot()),
            "blockers": [{"stage_id": "workflow", "reason": error}],
        }

    def _run_one_in_process(self, resume):
        try:
            runner = ComposerRunner(
                self.workflow, resume=resume, on_progress=self.on_progress)
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
            self._write_state(child_status="crashed", action="retry_after_crash", result=result,
                              error=exc)
            return result

    def run(self):
        if not self.process_watchdog:
            return self._run_in_process()
        resume = self.initial_resume or (self.project_root / "state" / "control.sqlite").is_file()
        while True:
            self._write_state(child_status="starting", action="dispatch", result=None)
            result = self._run_one_process(resume)
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


def supervise_composer(workflow, *, initial_resume=False, poll_seconds=5.0,
                       on_progress=None, process_watchdog=True,
                       watchdog_seconds=DEFAULT_WATCHDOG_SECONDS):
    """Convenience entry point used by the CLI and the local launch script."""
    return ComposerSupervisor(
        workflow, initial_resume=initial_resume, poll_seconds=poll_seconds,
        on_progress=on_progress, process_watchdog=process_watchdog,
        watchdog_seconds=watchdog_seconds,
    ).run()
