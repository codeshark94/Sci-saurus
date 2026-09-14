"""Project-scoped executive composer for end-to-end research workflows.

The composer is the control loop around specialist runners.  It owns the
workflow graph, dependency admission, elapsed-time accounting, checkpoints,
feedback routing, and release proposal.  It does not make scientific
acceptance decisions or edit departmental artifacts directly; those remain the
responsibility of the stage runner and its independent checks.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import itertools
import json
import math
import os
import platform
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
import time
import uuid

from scisaurus.core.errors import NotFoundError, StateError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.messages import MessageBus
from scisaurus.core.schema import canonical_bytes, now_iso
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager


SCHEMA_VERSION = "composer-workflow-1"
RUN_SCHEMA_VERSION = "composer-run-1"
STAGE_KINDS = frozenset({"topic_discovery", "survey", "experiment", "interpretation", "argument", "paper"})
# These are the only stage outcomes that satisfy a downstream dependency.
# ``research_expansion_required`` and ``review_rejected`` are terminal for the
# current attempt, but they are scientific holds: the affected closure must
# be reopened before any consumer can read the incumbent packet.
STAGE_READY_STATUSES = frozenset({"completed", "accepted", "candidate_needs_review"})
STAGE_HOLD_STATUSES = frozenset({"research_expansion_required", "review_rejected"})
STAGE_ROLES = {
    "topic_discovery": "research.intelligence",
    "survey": "research.intelligence",
    "experiment": "methods.validation",
    "interpretation": "strategy.interpretation",
    "argument": "strategy.argument",
    "paper": "editorial.composer",
}
DEPARTMENT_ADDRESSES = {
    "research.intelligence": {"dept": "research", "agent": "chief"},
    "methods.validation": {"dept": "methods", "agent": "chief"},
    "strategy.interpretation": {"dept": "strategy", "agent": "chief"},
    "strategy.argument": {"dept": "strategy", "agent": "chief"},
    "editorial.composer": {"dept": "editorial", "agent": "editor-in-chief"},
}
COMMAND_ADDRESSES = {
    "arbiter": {"dept": "executive-command", "agent": "arbiter"},
    "progress": {"dept": "executive-command", "agent": "progress-controller"},
    "intent": {"dept": "executive-command", "agent": "intent-keeper"},
}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
# Autonomous execution is the safe default: a transient model, provider, or
# validation failure cannot terminate a workflow merely because a counter was
# exhausted.  A small deterministic job can opt into ``bounded`` explicitly;
# every mode remains fenced by the immutable mission and stage deadlines.
DEFAULT_RETRY_POLICY = {"mode": "until_deadline", "max_attempts": None, "backoff_seconds": 2.0}
# Research re-entry is also deadline-governed by default.  A deterministic
# workflow may opt into a finite cycle budget explicitly; the manuscript's
# independent three-round peer review remains a separate contract.
DEFAULT_CONTINUATION_POLICY = {"mode": "until_deadline", "max_cycles": None}


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _positive_number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{name} must be finite and positive")


def validate_workflow(value):
    """Validate the immutable composer workflow contract."""
    fields = {"schema_version", "id", "revision", "project_id", "objective", "stages", "time_policy", "completion"}
    allowed_fields = fields | {"retry_policy", "continuation_policy", "organization", "exploration_seed",
                               "topic_reuse_allowed", "experiment_catalog", "topic_exclusions",
                               "topic_history_path"}
    if not isinstance(value, dict) or set(value) - allowed_fields or not fields.issubset(value):
        raise ValidationError(
            f"composer workflow requires {sorted(fields)} and permits ['continuation_policy', 'exploration_seed', 'organization', 'retry_policy', 'topic_reuse_allowed', 'experiment_catalog', 'topic_exclusions', 'topic_history_path']")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError(f"composer workflow schema must be {SCHEMA_VERSION}")
    _identifier(value["id"], "workflow id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("workflow revision must be a positive integer")
    _text(value["project_id"], "workflow project_id")
    _text(value["objective"], "workflow objective")
    if "exploration_seed" in value and (
            type(value["exploration_seed"]) is not int or value["exploration_seed"] < 0):
        raise ValidationError("workflow exploration_seed must be a non-negative integer when configured")
    if "topic_reuse_allowed" in value and type(value["topic_reuse_allowed"]) is not bool:
        raise ValidationError("workflow topic_reuse_allowed must be Boolean when configured")
    if "topic_history_path" in value:
        history_path = value["topic_history_path"]
        if not isinstance(history_path, str) or not history_path.strip():
            raise ValidationError("workflow topic_history_path must be a nonempty absolute path")
        if not Path(history_path).is_absolute():
            raise ValidationError("workflow topic_history_path must be an absolute path")
        if Path(history_path).exists() and not Path(history_path).is_file():
            raise ValidationError("workflow topic_history_path must name a file")
    if "topic_exclusions" in value:
        exclusions = value["topic_exclusions"]
        if (not isinstance(exclusions, dict)
                or set(exclusions) != {"capability_ids", "topic_ids"}
                or not isinstance(exclusions["capability_ids"], list)
                or not isinstance(exclusions["topic_ids"], list)):
            raise ValidationError("workflow topic_exclusions requires capability_ids and topic_ids lists")
        for name in ("capability_ids", "topic_ids"):
            if len(exclusions[name]) != len(set(exclusions[name])):
                raise ValidationError(f"workflow topic_exclusions.{name} must be unique")
            for item in exclusions[name]:
                _identifier(item, f"workflow topic exclusion {name} entry")
    if "experiment_catalog" in value:
        catalog = value["experiment_catalog"]
        if (not isinstance(catalog, list) or not catalog
                or len(catalog) > 16):
            raise ValidationError("workflow experiment_catalog must contain one to sixteen entries")
        catalog_ids = set()
        for entry in catalog:
            if not isinstance(entry, dict) or set(entry) != {"id", "config_path"}:
                raise ValidationError("workflow experiment_catalog entries require exactly id and config_path")
            entry_id = _identifier(entry["id"], "experiment catalog id")
            if entry_id in catalog_ids:
                raise ValidationError("workflow experiment_catalog IDs must be unique")
            catalog_ids.add(entry_id)
            path = Path(entry["config_path"])
            if not path.is_absolute() or not path.is_file():
                raise ValidationError("experiment catalog config_path must be an existing absolute file")
    stages = value["stages"]
    if not isinstance(stages, list) or not stages:
        raise ValidationError("composer workflow requires at least one stage")
    stage_ids, outputs = set(), set()
    stage_fields = {"id", "kind", "config_path", "project_dir", "depends_on", "estimate_seconds", "bindings",
                    "deadline_seconds", "reuse_completed", "reuse_output_path"}
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != stage_fields:
            raise ValidationError(f"composer stage requires exactly {sorted(stage_fields)}")
        stage_id = _identifier(stage["id"], "stage id")
        if stage_id in stage_ids:
            raise ValidationError("composer stage IDs must be unique")
        stage_ids.add(stage_id)
        if stage["kind"] not in STAGE_KINDS:
            raise ValidationError(f"unsupported composer stage kind: {stage['kind']}")
        for key in ("config_path", "project_dir"):
            path = Path(stage[key])
            if not path.is_absolute() or not path.exists():
                raise ValidationError(f"stage {stage_id} {key} must be an existing absolute path")
        _positive_number(stage["estimate_seconds"], f"stage {stage_id} estimate_seconds")
        _positive_number(stage["deadline_seconds"], f"stage {stage_id} deadline_seconds")
        deps = stage["depends_on"]
        if not isinstance(deps, list) or len(deps) != len(set(deps)) or any(not isinstance(dep, str) for dep in deps):
            raise ValidationError(f"stage {stage_id} depends_on must be a unique string list")
        if stage_id in deps:
            raise ValidationError(f"stage {stage_id} cannot depend on itself")
        bindings = stage["bindings"]
        if not isinstance(bindings, list):
            raise ValidationError(f"stage {stage_id} bindings must be a list")
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"target", "source"}:
                raise ValidationError(f"stage {stage_id} binding must have target and source")
            _text(binding["target"], f"stage {stage_id} binding target")
            _text(binding["source"], f"stage {stage_id} binding source")
        if type(stage["reuse_completed"]) is not bool:
            raise ValidationError(f"stage {stage_id} reuse_completed must be Boolean")
        checkpoint = stage["reuse_output_path"]
        if checkpoint is not None:
            if (not isinstance(checkpoint, str) or not Path(checkpoint).is_absolute()
                    or not Path(checkpoint).is_file()):
                raise ValidationError(f"stage {stage_id} reuse_output_path must be an existing absolute file or null")
        if stage["reuse_completed"] and checkpoint is None:
            # A stage may use its conventional output/run.json when no file is
            # supplied.  The explicit Boolean is what makes that reuse a
            # deliberate workflow decision rather than an implicit fallback.
            pass
        outputs.add(stage_id)
    for stage in stages:
        if set(stage["depends_on"]) - stage_ids:
            raise ValidationError(f"stage {stage['id']} depends on an unknown stage")
    # Dependency graph must be acyclic; a topological order is also used for
    # deterministic dispatch and restart accounting.
    visiting, visited = set(), set()
    by_id = {stage["id"]: stage for stage in stages}
    def visit(stage_id):
        if stage_id in visiting:
            raise ValidationError("composer stage graph must be acyclic")
        if stage_id in visited:
            return
        visiting.add(stage_id)
        for dep in by_id[stage_id]["depends_on"]:
            visit(dep)
        visiting.remove(stage_id)
        visited.add(stage_id)
    for stage_id in stage_ids:
        visit(stage_id)
    policy = value["time_policy"]
    policy_fields = {"first_result_seconds", "target_seconds", "hard_seconds", "checkpoint_seconds"}
    if not isinstance(policy, dict) or set(policy) != policy_fields:
        raise ValidationError(f"time_policy requires exactly {sorted(policy_fields)}")
    for key in policy_fields:
        _positive_number(policy[key], f"time_policy.{key}")
    if not (policy["first_result_seconds"] <= policy["target_seconds"] <= policy["hard_seconds"]):
        raise ValidationError("composer time policy requires first_result <= target <= hard")
    for stage in stages:
        if stage["deadline_seconds"] > policy["hard_seconds"]:
            raise ValidationError(f"stage {stage['id']} deadline_seconds cannot exceed composer hard_seconds")
    retry_policy = value.get("retry_policy")
    if retry_policy is not None:
        retry_fields = {"mode", "max_attempts", "backoff_seconds"}
        if not isinstance(retry_policy, dict) or set(retry_policy) - retry_fields \
                or "backoff_seconds" not in retry_policy:
            raise ValidationError(f"retry_policy requires backoff_seconds and permits {sorted(retry_fields - {'backoff_seconds'})}")
        mode = retry_policy.get("mode", "bounded")
        if mode not in {"bounded", "until_deadline"}:
            raise ValidationError("retry_policy.mode must be bounded or until_deadline")
        attempts = retry_policy.get("max_attempts")
        if mode == "bounded":
            if type(attempts) is not int or not 1 <= attempts <= 8:
                raise ValidationError("retry_policy.max_attempts must be an integer between one and eight for bounded mode")
        elif attempts is not None and (type(attempts) is not int or attempts < 1):
            raise ValidationError("retry_policy.max_attempts may be omitted or a positive integer in until_deadline mode")
        if (type(retry_policy["backoff_seconds"]) not in (int, float)
                or not math.isfinite(retry_policy["backoff_seconds"])
                or retry_policy["backoff_seconds"] < 0):
                raise ValidationError("retry_policy.backoff_seconds must be finite and non-negative")
    continuation_policy = value.get("continuation_policy")
    if continuation_policy is not None:
        continuation_fields = {"mode", "max_cycles"}
        if (not isinstance(continuation_policy, dict)
                or set(continuation_policy) - continuation_fields
                or not continuation_policy):
            raise ValidationError("continuation_policy permits mode and max_cycles")
        mode = continuation_policy.get("mode", "bounded")
        if mode not in {"bounded", "until_deadline"}:
            raise ValidationError("continuation_policy.mode must be bounded or until_deadline")
        cycles = continuation_policy.get("max_cycles")
        if mode == "bounded":
            if type(cycles) is not int or not 0 <= cycles <= 8:
                raise ValidationError("continuation_policy.max_cycles must be an integer between zero and eight for bounded mode")
        elif cycles is not None and (type(cycles) is not int or cycles < 0):
            raise ValidationError("continuation_policy.max_cycles may be omitted or a non-negative integer in until_deadline mode")
    organization = value.get("organization")
    if organization is not None:
        from scisaurus.runtime.departments import validate_organization
        validated_organization = validate_organization(organization)
        configured_departments = {item["id"] for item in validated_organization["departments"]}
        required_departments = {STAGE_ROLES[stage["kind"]].split(".", 1)[0] for stage in stages}
        if required_departments - configured_departments:
            raise ValidationError(
                "project organization is missing stage-owning departments: "
                + ", ".join(sorted(required_departments - configured_departments)))
    completion = value["completion"]
    completion_fields = {"required_stage_ids", "release_requires_human"}
    if not isinstance(completion, dict) or set(completion) != completion_fields:
        raise ValidationError(f"completion requires exactly {sorted(completion_fields)}")
    required = completion["required_stage_ids"]
    if not isinstance(required, list) or not required or len(required) != len(set(required)) or set(required) - stage_ids:
        raise ValidationError("completion.required_stage_ids must name unique known stages")
    if type(completion["release_requires_human"]) is not bool:
        raise ValidationError("completion.release_requires_human must be Boolean")
    canonical_bytes(value)
    return value


def _set_path(container, path, value):
    parts = path.split(".")
    if not parts or any(not part for part in parts):
        raise ValidationError("binding target path is invalid")
    current = container
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            raise ValidationError(f"binding target does not exist: {path}")
        current = current[part]
    if not isinstance(current, dict) or parts[-1] not in current:
        raise ValidationError(f"binding target does not exist: {path}")
    current[parts[-1]] = deepcopy(value)


def _get_path(container, path):
    current = container
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValidationError(f"binding source does not exist: {path}")
        current = current[part]
    return deepcopy(current)


class ComposerRunner:
    """Execute a complete stage graph with a durable composer feedback loop.

    The stage runners are deliberately called through a small allowlist.  A
    workflow cannot smuggle an arbitrary shell command into the control plane;
    operations that need a program or MCP service remain inside their own
    project-scoped runner.
    """

    def __init__(self, workflow, *, resume=False, clock=time.monotonic, on_progress=None,
                 additional_seconds=None):
        self.workflow = deepcopy(validate_workflow(workflow))
        if additional_seconds is not None and not resume:
            raise ValidationError("additional_seconds is only valid when resuming a Composer project")
        self.root = Path(self.workflow["project_id"]).resolve()
        # project_id is the stable identity; the workflow's project directory
        # is derived from it so a config cannot redirect the control ledger.
        self.root.mkdir(parents=True, exist_ok=True)
        self.topic_history_path = self._resolve_topic_history_path()
        self.topic_history_scope = self._topic_history_scope_key()
        self.topic_history = self._load_topic_history()
        existing = (self.root / "state" / "control.sqlite").exists()
        if existing and not resume:
            raise ValidationError("composer project already exists; pass resume=True to continue it")
        if resume and not existing:
            raise ValidationError("composer resume requires an existing project")
        self.control = ControlStore(self.root)
        self.store = ArtifactStore(self.control)
        self.messages = MessageBus(self.control)
        self.store.init_project(principal_note=f"composer:{self.workflow['id']}")
        self.tasks = TaskManager(self.control)
        from scisaurus.runtime.departments import DepartmentRuntime
        self.departments = DepartmentRuntime(
            self.control, self.store, self.messages, self.tasks,
            project_id=self.workflow["project_id"], organization=self.workflow.get("organization"),
        )
        # The live progress ticker runs in a background thread while the
        # project ledger is deliberately bound to the Composer thread.  Keep a
        # plain-data snapshot for that ticker; SQLite connections are not
        # shared across threads.
        self.organization_snapshot = deepcopy(self.departments.snapshot())
        self.clock = clock
        self.on_progress = on_progress or (lambda state: None)
        self._progress_lock = threading.Lock()
        self._live_progress_epoch = 0
        self.started = self.clock()
        # ``monotonic`` is the right clock while this process is alive, but it
        # cannot survive a pause/restart.  Keep a wall-clock fence alongside
        # it so ``--resume`` cannot silently grant the mission another hard
        # window.  The monotonic deadline remains the fast local check.
        self.started_epoch = time.time()
        self.deadline_epoch = self.started_epoch + float(self.workflow["time_policy"]["hard_seconds"])
        policy = self.workflow["time_policy"]
        self.deadline = self.started + float(policy["hard_seconds"])
        self.next_checkpoint = self.started
        self.run_id = uuid.uuid4().hex
        # A fresh mission receives one entropy-backed exploration seed.  It is
        # persisted in the run input/checkpoints so retries and resumes repeat
        # the same proposal, while two independently created missions explore
        # different literature samples and topic generations.  A workflow can
        # pin the seed when exact replay is desired.
        configured_seed = self.workflow.get("exploration_seed")
        self.exploration_seed = (configured_seed if configured_seed is not None
                                 else secrets.randbits(63))
        self.context = {}
        self.stage_records = {}
        self.feedback = []
        self.blockers = []
        self.usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.continuation_cycles = 0
        self.reopened_stage_ids = set()
        self.continuation_pending_stage_ids = set()
        self.active_research_requests = []
        self.department_activity = []
        self.deadline_extensions = []
        self.deadline_decisions = []
        # A hold can echo the same work order in every returned stage packet.
        # Suppress that exact request for the current Composer invocation after
        # its owning stage has been attempted.  The set is intentionally
        # process-local: an explicit resume creates a fresh attempt and may
        # retry the unresolved request after the operator grants more time.
        self._attempted_request_signatures = set()
        self.status = "running"
        self._progress_snapshot = {
            "schema_version": "composer-checkpoint-1", "workflow_id": self.workflow["id"],
            "run_id": self.run_id,
            "phase": "initialized", "elapsed_seconds": 0.0,
            "remaining_seconds": max(0.0, float(self.workflow["time_policy"]["hard_seconds"])),
            "started_at_epoch": self.started_epoch, "deadline_at_epoch": self.deadline_epoch,
            "exploration_seed": self.exploration_seed,
            "retry_policy": self._retry_policy(),
            "continuation_policy": self._continuation_policy(),
            "continuation_cycles": 0, "reopened_stage_ids": [],
            "continuation_pending_stage_ids": [], "active_research_requests": [],
            "department_activity": [], "stages": {}, "context": {}, "feedback": [],
            "blockers": [], "usage": deepcopy(self.usage), "deadline_decisions": [],
            "deadline_extensions": [],
            "organization": deepcopy(self.organization_snapshot),
            "topic_history_path": str(self.topic_history_path),
            "topic_history_scope": self.topic_history_scope,
            "topic_history_entries": len(self.topic_history.get("entries", [])),
        }
        self._workflow_record = None
        if not existing:
            self._workflow_record = self._publish("command/composer/workflow", "note", self.workflow, "command.composer")
            self._publish("inputs/composer-run", "note", {
                "schema_version": "composer-run-input-1", "workflow_ref": self._workflow_record["artifact_ref"],
                "run_id": self.run_id, "resume": False,
                "exploration_seed": self.exploration_seed,
            }, "command.composer")
        else:
            head = self.store.head("command/composer/workflow")
            if head is None or json.loads(self.store.read_body(head["body_hash"])) != self.workflow:
                raise ValidationError("composer resume workflow does not match the original immutable workflow")
            self._restore()
        if additional_seconds is not None:
            self._extend_deadline(additional_seconds)

    def close(self):
        if self.control is not None:
            self.control.close()
            self.control = None

    def _publish(self, logical_id, artifact_type, body, author):
        return self.store.publish_artifact(logical_id=logical_id, artifact_type=artifact_type,
                                           author=author, body=canonical_bytes(body),
                                           media_type="application/json")

    def _remaining(self):
        remaining = self.deadline - self.clock()
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining = min(remaining, self.deadline_epoch - time.time())
        if remaining <= 0:
            raise ValidationError("composer hard deadline exceeded")
        return remaining

    def _deadline_dispatch_floor(self):
        """Return the small control-plane window needed for safe dispatch.

        Stage estimates are forecasts, not execution fences.  In the
        deadline-governed mode, a residual window may still produce a useful
        accepted stage result, so the Composer keeps admitting work until only
        this checkpoint-and-ledger margin remains.  The margin follows the
        configured checkpoint cadence and is capped to keep it small relative
        to a scientific stage.
        """
        checkpoint = float(self.workflow["time_policy"]["checkpoint_seconds"])
        return max(0.5, min(5.0, checkpoint / 10.0))

    def _deadline_exhausted(self):
        """Return whether the immutable mission wall has been reached."""
        remaining = self.deadline - self.clock()
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining = min(remaining, self.deadline_epoch - time.time())
        return remaining <= 0

    def _extend_deadline(self, additional_seconds):
        """Apply an explicit human extension without mutating the workflow graph."""
        if not isinstance(additional_seconds, (int, float)) or isinstance(additional_seconds, bool) \
                or not math.isfinite(additional_seconds) or additional_seconds <= 0:
            raise ValidationError("additional_seconds must be finite and positive")
        extension = float(additional_seconds)
        if self._workflow_record is None and not (self.root / "state" / "control.sqlite").exists():
            raise ValidationError("a deadline extension requires an existing Composer project")
        old_deadline_epoch = self.deadline_epoch
        old_deadline = self.deadline
        new_deadline_epoch = old_deadline_epoch + extension
        new_deadline = old_deadline + extension
        body = {
            "schema_version": "composer-deadline-extension-1",
            "workflow_id": self.workflow["id"], "run_id": self.run_id,
            "additional_seconds": extension,
            "previous_deadline_at_epoch": old_deadline_epoch,
            "deadline_at_epoch": new_deadline_epoch,
            "created_at": now_iso(),
        }
        record = self._publish(
            f"command/composer/deadline-extensions/{uuid.uuid4().hex}",
            "decision_note", body, "principal")
        self.deadline_epoch = new_deadline_epoch
        self.deadline = new_deadline
        self.deadline_extensions.append({
            "additional_seconds": extension,
            "previous_deadline_at_epoch": old_deadline_epoch,
            "deadline_at_epoch": new_deadline_epoch,
            "artifact_ref": record["artifact_ref"],
        })
        # Persist the extension immediately.  If the process is interrupted
        # before the next stage boundary, resume still sees the new mission
        # wall rather than silently discarding the human decision.
        self._checkpoint("deadline_extended", force=True)

    def interim_report(self, *, stop_reason=None, persist=True):
        """Return and optionally persist a concise human-readable run snapshot.

        The Composer's full checkpoint and ledger remain the source of truth.
        This projection is intentionally small: it tells an operator what was
        completed, what is active or held, why execution stopped, and exactly
        how to resume with an explicit deadline extension.
        """
        now = self.clock()
        remaining = self.deadline - now
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining = min(remaining, self.deadline_epoch - time.time())
        rows = []
        completed, active, pending, held, blocked = [], [], [], [], []
        for stage in self.workflow["stages"]:
            stage_id = stage["id"]
            record = self.stage_records.get(stage_id, {})
            status = record.get("status", "pending")
            attempts = record.get("attempts", [])
            if not isinstance(attempts, list):
                attempts = []
            attempt_count = record.get("attempt_count", len(attempts))
            if type(attempt_count) is not int or attempt_count < 0:
                attempt_count = len(attempts)
            row = {
                "id": stage_id,
                "kind": stage["kind"],
                "status": status,
                "attempts": attempt_count,
            }
            output_path = record.get("output_path")
            if isinstance(output_path, str) and output_path:
                row["output_path"] = output_path
            error = record.get("error")
            if isinstance(error, str) and error:
                row["error"] = error[:240]
            rows.append(row)
            if status in STAGE_READY_STATUSES:
                completed.append(stage_id)
            elif status in {"running", "retrying"}:
                active.append(stage_id)
            elif status in STAGE_HOLD_STATUSES:
                held.append(stage_id)
            elif status == "blocked":
                blocked.append(stage_id)
            else:
                pending.append(stage_id)
        if stop_reason is None:
            if self._deadline_exhausted():
                stop_reason = "hard_deadline"
            elif self.status == "paused" and any(
                    isinstance(item, dict)
                    and item.get("reason") == "required_stage_window_does_not_fit_remaining_deadline"
                    for item in self.blockers):
                stop_reason = "required_stage_window_does_not_fit_remaining_deadline"
            else:
                stop_reason = self.status
        organization = self.organization_snapshot if isinstance(self.organization_snapshot, dict) else {}
        open_orders = organization.get("open_work_orders", [])
        if not isinstance(open_orders, list):
            open_orders = []
        orders = []
        for item in open_orders[:8]:
            if not isinstance(item, dict):
                continue
            orders.append({key: item[key] for key in (
                "task_id", "department", "state", "kind", "objective") if key in item})
        blockers = []
        for item in self.blockers[-3:]:
            if isinstance(item, dict):
                blockers.append({key: item[key] for key in ("stage_id", "reason", "attempts") if key in item})
            else:
                blockers.append({"reason": str(item)[:240]})
        report = {
            "schema_version": "composer-interim-report-1",
            "workflow_id": self.workflow["id"],
            "run_id": self.run_id,
            "status": self.status,
            "stop_reason": stop_reason,
            "last_phase": self._progress_snapshot.get("phase", self.status),
            "elapsed_seconds": max(0.0, now - self.started),
            "remaining_seconds": max(0.0, remaining),
            "deadline_seconds": self.workflow["time_policy"]["hard_seconds"],
            "started_at_epoch": self.started_epoch,
            "deadline_at_epoch": self.deadline_epoch,
            "completed_stage_ids": completed,
            "active_stage_ids": active,
            "pending_stage_ids": pending,
            "held_stage_ids": held,
            "blocked_stage_ids": blocked,
            "stages": rows,
            "pending_work_orders": orders,
            "blockers": blockers,
            "deadline_decisions": deepcopy(self.deadline_decisions),
            "deadline_extensions": deepcopy(self.deadline_extensions),
            "next_actions": [
                "Extend the deadline and resume the same workflow."
                if stop_reason in {"hard_deadline", "required_stage_window_does_not_fit_remaining_deadline"}
                else "Inspect the blocker and resume the same workflow after the cause is recoverable.",
                "Resume reconciles interrupted attempts before dispatching a fresh isolated attempt.",
            ],
            "resume_hint": (
                "python -m scisaurus.cli run-composer --workflow "
                "<path-to-the-same-workflow.json> --resume --extend-deadline-seconds <N>"
            ),
        }
        if persist:
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            artifact_ref = None
            artifact_error = None
            if self.control is not None:
                try:
                    artifact = self._publish(
                        "command/composer/interim-report", "report", report, "command.composer")
                    artifact_ref = artifact["artifact_ref"]
                except Exception as exc:
                    artifact_error = f"{type(exc).__name__}: {exc}"
            persisted = {**report, "artifact_ref": artifact_ref,
                         "path": str((output / "interim_report.json").resolve())}
            if artifact_error:
                persisted["artifact_error"] = artifact_error
            temporary = output / f"interim-report-{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(canonical_bytes(persisted))
            temporary.replace(output / "interim_report.json")
            report = persisted
        return report

    def _retry_policy(self):
        """Return the Composer retry policy for this immutable run.

        ``until_deadline`` deliberately has no attempt-count stop condition.
        The hard wall and dependency admission remain the termination
        conditions; a residual stage may be admitted until the small
        control-plane margin, but it can never extend the mission wall.
        """
        configured = self.workflow.get("retry_policy") or {}
        policy = {**DEFAULT_RETRY_POLICY, **configured}
        # Before ``mode`` was introduced, the presence of ``max_attempts``
        # meant a bounded policy.  Preserve that unambiguous legacy contract
        # while an omitted policy gets the autonomous deadline-governed mode.
        if "mode" not in configured and "max_attempts" in configured:
            policy["mode"] = "bounded"
        return policy

    def _continuation_policy(self):
        """Return the research-continuation policy for this immutable run."""
        configured = self.workflow.get("continuation_policy") or {}
        policy = {**DEFAULT_CONTINUATION_POLICY, **configured}
        # Preserve the pre-mode meaning of an explicitly supplied cycle count.
        if "mode" not in configured and "max_cycles" in configured:
            policy["mode"] = "bounded"
        return policy

    def _continuation_requests(self):
        """Collect validated research requests that can reopen a scoped stage.

        A request is a scientific work order, not a prose edit.  The specialist
        runners validate its shape before it reaches this method; the Composer
        copies only the stable public fields into its control plan.  A malformed
        request is a durable rejection, not a reason to abort an otherwise
        recoverable run.
        """
        requests = []
        seen = set()
        for stage_id, context in self.context.items():
            if not isinstance(context, dict):
                continue
            has_candidates = False
            containers = []
            for label in ("research_expansion_requests", "research_requests"):
                if label not in context or context[label] is None:
                    continue
                value = context[label]
                has_candidates = has_candidates or (
                    bool(value) if isinstance(value, list) else True)
                containers.append((label, value))
            for label, candidates in containers:
                if not isinstance(candidates, list):
                    rejection = self.departments.reject_request(
                        candidates, source_stage_id=stage_id,
                        reason=f"{label} must be a list when supplied",
                    )
                    self.department_activity.append({
                        "action": "reject_work_order", "stage_id": stage_id,
                        "request_id": rejection["request_id"], "rejection": rejection,
                    })
                    continue
                for request in candidates:
                    if not isinstance(request, dict):
                        rejection = self.departments.reject_request(
                            request, source_stage_id=stage_id,
                            reason="work-order request must be an object",
                        )
                        self.department_activity.append({
                            "action": "reject_work_order", "stage_id": stage_id,
                            "request_id": rejection["request_id"], "rejection": rejection,
                        })
                        continue
                    item = {key: deepcopy(request.get(key)) for key in (
                        "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed")}
                    item["schema_version"] = "department-work-order-1"
                    try:
                        self.departments.validate_request(item)
                    except ValidationError as exc:
                        rejection = self.departments.reject_request(
                            request, source_stage_id=stage_id,
                            reason=f"{type(exc).__name__}: {exc}",
                        )
                        self.department_activity.append({
                            "action": "reject_work_order", "stage_id": stage_id,
                            "request_id": rejection["request_id"], "rejection": rejection,
                        })
                        continue
                    request_id = item["id"]
                    if request_id in seen:
                        continue
                    item.pop("schema_version", None)
                    item["source_stage_id"] = stage_id
                    if self._research_request_signature(item) in self._attempted_request_signatures:
                        continue
                    requests.append(item)
                    seen.add(request_id)
            if context.get("status") == "review_rejected" and not has_candidates:
                request_id = f"{stage_id}-editorial-reconsideration"
                requests.append({
                    "id": request_id,
                    "kind": "manuscript_revision",
                    "owner": "editorial.composer",
                    "objective": "Reopen the manuscript with a fresh reviewer-facing composition attempt.",
                    "why": "The bounded editorial cycle ended with material findings still unresolved.",
                    "success_condition": "The same reviewer panel accepts the revised incumbent and the editor-in-chief records acceptance.",
                    "evidence_needed": "The prior draft, review findings, and every surgical repair remain available in the manuscript project.",
                    "source_stage_id": stage_id,
                })
        return requests

    @staticmethod
    def _continuation_targets(requests, by_id):
        """Map research work orders to the smallest stage closure that can answer them."""
        target_kinds = set()
        for request in requests:
            kind = request.get("kind")
            owner = request.get("owner")
            if kind == "topic_refinement":
                target_kinds.add("topic_discovery")
            elif kind in {"literature_expansion", "full_text_retrieval"} or owner == "research.intelligence":
                target_kinds.add("survey")
            elif kind in {"additional_experiment", "analysis_display", "analysis_repair"} \
                    or owner == "methods.validation":
                target_kinds.add("experiment")
            elif kind == "interpretation_expansion" or owner == "strategy.interpretation":
                target_kinds.add("interpretation")
            elif kind == "manuscript_revision" or owner == "editorial.composer":
                target_kinds.add("paper")
        targets = {stage_id for stage_id, stage in by_id.items() if stage["kind"] in target_kinds}
        if not targets:
            return set()
        # Rerun every downstream consumer because its bound packet may have
        # changed.  The closure is deterministic and keeps unrelated branches
        # intact.
        changed = True
        while changed:
            changed = False
            for stage_id, stage in by_id.items():
                if stage_id in targets:
                    continue
                if set(stage["depends_on"]) & targets:
                    targets.add(stage_id)
                    changed = True
        return targets

    def _record_continuation(self, requests, reopened_stage_ids):
        """Persist one continuation decision and route its work orders."""
        cycle = self.continuation_cycles
        owners = {item.get("owner") for item in requests}
        if len(owners) == 1 and next(iter(owners)):
            recipient = self._address_for_role(next(iter(owners)))
        else:
            recipient = COMMAND_ADDRESSES["arbiter"]
        feedback = {
            "event_id": f"composer-continuation-{cycle}-{uuid.uuid4().hex}",
            "stage_id": "workflow",
            "role": "executive-command",
            "from": COMMAND_ADDRESSES["progress"],
            "to": recipient,
            "status": "continuing",
            "action": "continue_research",
            "cycle": cycle,
            "max_cycles": self._continuation_policy()["max_cycles"],
            "research_requests": deepcopy(requests),
            "reopened_stage_ids": sorted(reopened_stage_ids),
            "next_condition": "complete each scoped work order, rerun downstream interpretation and review, and re-evaluate the release gate",
            "message_id": f"composer-continuation-{cycle}-{uuid.uuid4().hex}",
            "message_disposition": "scheduled",
        }
        self.feedback.append(feedback)
        note = self._publish(
            f"command/composer/continuation/{cycle}", "decision_note", feedback, "command.composer")
        self._route_feedback(feedback, note)

    def _begin_continuation(self, completed, by_id):
        """Reopen the affected closure after a research or review request."""
        # A continuation is new scientific work.  Check the immutable mission
        # wall before publishing its decision or activating any work order.
        self._remaining()
        policy = self._continuation_policy()
        if (policy.get("mode", "bounded") == "bounded"
                and self.continuation_cycles >= policy["max_cycles"]):
            return False
        requests = self._continuation_requests()
        targets = self._continuation_targets(requests, by_id)
        if not requests or not targets:
            return False
        self.continuation_cycles += 1
        self.active_research_requests = requests
        self.reopened_stage_ids = set(targets)
        self.continuation_pending_stage_ids = set(targets)
        completed.difference_update(targets)
        self._record_continuation(requests, targets)
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "activate_work_orders",
            "work_orders": self.departments.activate_work_orders(requests),
        })
        self._checkpoint(f"continuation:{self.continuation_cycles}:admitted", force=True)
        return True

    def _stage_for_cycle(self, stage):
        """Route a reopened stage into a cycle-specific project namespace."""
        if stage["id"] not in self.reopened_stage_ids:
            return stage
        candidate = deepcopy(stage)
        base = Path(stage["project_dir"]).resolve()
        candidate["project_dir"] = str(
            base / "continuations" / f"cycle-{self.continuation_cycles}")
        candidate["reuse_completed"] = False
        candidate["reuse_output_path"] = None
        return candidate

    def _runtime_context(self, model):
        """Expose safe, actionable execution capabilities to topic selection.

        This is intentionally a capability inventory rather than a dump of the
        process environment: credentials, tokens, and unrelated host variables
        never enter a model prompt.  The selected topic is therefore judged
        against the tools that the actual Composer can call.
        """
        executable_names = ("python3", "git", "tectonic", "pdflatex", "latexmk",
                            "node", "npm", "Rscript", "julia")
        package_names = ("numpy", "scipy", "pandas", "sklearn", "matplotlib",
                         "statsmodels", "sympy", "torch", "tensorflow")
        try:
            from importlib.util import find_spec
            packages = {name: find_spec(name) is not None for name in package_names}
        except (ImportError, ModuleNotFoundError, ValueError):
            packages = {name: False for name in package_names}
        project_files = []
        ignored_roots = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules"}
        inventory_roots = [(self.root, "composer")]
        for stage in self.workflow["stages"]:
            inventory_roots.append((Path(stage["project_dir"]).resolve(), f"stage:{stage['id']}"))
        seen_files = set()
        for stage in self.workflow["stages"]:
            descriptor = Path(stage["config_path"]).resolve()
            if descriptor.is_file() and str(descriptor) not in seen_files:
                seen_files.add(str(descriptor))
                project_files.append(f"config:{stage['id']}/{descriptor.name}")
                if len(project_files) >= 100:
                    break
        try:
            for base, label in inventory_roots:
                if not base.exists():
                    continue
                for path in sorted(base.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(base)
                    if relative.parts and (relative.parts[0] in {"state", "objects"}
                                           or relative.parts[0] in ignored_roots
                                           or any(part in ignored_roots for part in relative.parts)):
                        continue
                    identity = str(path.resolve())
                    if identity in seen_files:
                        continue
                    seen_files.add(identity)
                    project_files.append(f"{label}/{relative}")
                    if len(project_files) >= 100:
                        break
                if len(project_files) >= 100:
                    break
        except OSError:
            project_files = []
        experiment_contract = None
        experiment_catalog = []
        catalog = self.workflow.get("experiment_catalog") or []
        for entry in catalog:
            try:
                configured = json.loads(Path(entry["config_path"]).read_text())
            except (OSError, ValueError) as exc:
                raise ValidationError(
                    f"experiment capability template is unreadable: {entry['config_path']}") from exc
            experiment = configured.get("experiment") if isinstance(configured, dict) else None
            if not isinstance(experiment, dict):
                raise ValidationError("experiment capability template must contain an experiment object")
            experiment_catalog.append({
                "id": entry["id"],
                "study_type": experiment.get("study_type"),
                "domain": experiment.get("domain"),
                "research_question": experiment.get("research_question"),
                "method": experiment.get("method"),
                "stopping_rule": experiment.get("stopping_rule"),
                "primary_outcomes": [
                    {key: outcome.get(key) for key in ("id", "definition", "unit")}
                    for outcome in experiment.get("primary_outcomes", [])
                    if isinstance(outcome, dict)
                ],
                "required_asset_roles": [
                    {key: asset.get(key) for key in ("role", "media_types", "min_count")}
                    for asset in experiment.get("required_assets", [])
                    if isinstance(asset, dict)
                ],
                "execution_adapter": (experiment.get("execution") or {}).get("adapter"),
            })
        experiment_stages = [stage for stage in self.workflow["stages"] if stage["kind"] == "experiment"]
        if experiment_stages and not catalog:
            try:
                configured = json.loads(Path(experiment_stages[0]["config_path"]).read_text())
                experiment = configured.get("experiment") if isinstance(configured, dict) else None
                if isinstance(experiment, dict):
                    experiment_contract = {
                        "study_type": experiment.get("study_type"),
                        "research_question": experiment.get("research_question"),
                        "method": experiment.get("method"),
                        "stopping_rule": experiment.get("stopping_rule"),
                        "primary_outcomes": [
                            {key: outcome.get(key) for key in ("id", "definition", "unit")}
                            for outcome in experiment.get("primary_outcomes", [])
                            if isinstance(outcome, dict)
                        ],
                        "required_asset_roles": [
                            {key: asset.get(key) for key in ("role", "media_types", "min_count")}
                            for asset in experiment.get("required_assets", [])
                            if isinstance(asset, dict)
                        ],
                        "execution_adapter": (experiment.get("execution") or {}).get("adapter"),
                    }
            except (OSError, ValueError, TypeError):
                experiment_contract = None
        return {
            "operating_system": platform.system(),
            "platform": platform.machine(),
            "python_version": sys.version.split()[0],
            "current_date": time.strftime("%Y-%m-%d", time.gmtime()),
            "executables": {name: shutil.which(name) is not None for name in executable_names},
            "python_packages": packages,
            "model_protocol": model.get("protocol") if isinstance(model, dict) else None,
            "model_name": model.get("model") if isinstance(model, dict) else None,
            "configured_stage_kinds": [stage["kind"] for stage in self.workflow["stages"]],
            "experiment_contract": experiment_contract,
            "experiment_catalog": experiment_catalog,
            "topic_exclusions": self._effective_topic_exclusions(),
            "topic_history": self._topic_history_context(),
            "project_files": project_files,
            "project_scoped_execution": True,
        }

    def _topic_sampling_seed(self):
        """Derive a stable per-cycle seed from the mission exploration seed."""
        material = f"{self.exploration_seed}:topic:{self.continuation_cycles}".encode("utf-8")
        # The digest is intentionally wide for entropy, then reduced to the
        # signed-int64 range accepted by the configured model endpoint.
        from scisaurus.runtime.models import MAX_PROVIDER_SEED
        return int(hashlib.sha256(material).hexdigest()[:16], 16) % MAX_PROVIDER_SEED

    def _resolve_topic_history_path(self):
        """Resolve the durable history shared by a family of Composer runs.

        A Composer project is intentionally immutable and each fresh mission
        gets its own project directory.  Topic memory therefore lives beside
        that directory, rather than inside a mutable stage checkpoint.  A
        workflow may pin an explicit path; otherwise the parent of the run
        family receives a hidden, project-local history file.
        """
        configured = self.workflow.get("topic_history_path")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser().resolve()
        family_root = self.root.parent.parent
        # Avoid turning a shallow project path such as ``/tmp/composer`` into
        # a write to the filesystem root.  Normal run families (for example
        # ``project/.runs/run/composer``) still share the stable ``.runs``
        # parent; a shallow project keeps history beside its composer root.
        if family_root == Path(self.root.anchor or "/"):
            family_root = self.root.parent
        if family_root == Path(self.root.anchor or "/"):
            family_root = Path.cwd()
        return (family_root / ".scisaurus-topic-history.json").resolve()

    def _topic_history_scope_key(self):
        """Keep topic memory isolated by objective and executable portfolio."""
        catalog = [
            item.get("id") for item in self.workflow.get("experiment_catalog", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        material = {
            "objective": self.workflow["objective"],
            "experiment_capability_ids": sorted(catalog),
            "stage_kinds": [item["kind"] for item in self.workflow["stages"]],
        }
        return hashlib.sha256(canonical_bytes(material)).hexdigest()

    @staticmethod
    def _validate_topic_history_document(document):
        """Validate the small append-only topic-history envelope."""
        if not isinstance(document, dict) or document.get("schema_version") != "topic-history-1":
            raise ValidationError("topic history has an unsupported schema")
        scopes = document.get("scopes")
        if not isinstance(scopes, dict):
            raise ValidationError("topic history scopes must be an object")
        for scope_key, scope in scopes.items():
            if not isinstance(scope_key, str) or not isinstance(scope, dict):
                raise ValidationError("topic history contains an invalid scope")
            entries = scope.get("entries", [])
            if not isinstance(entries, list):
                raise ValidationError("topic history entries must be a list")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValidationError("topic history entry must be an object")
                if not isinstance(entry.get("topic_id"), str) or not entry["topic_id"].strip():
                    raise ValidationError("topic history entry requires topic_id")
                for key in ("title", "research_question", "domain"):
                    if key in entry and entry[key] is not None and not isinstance(entry[key], str):
                        raise ValidationError(f"topic history entry {key} must be a string")
        return document

    def _load_topic_history(self):
        """Load the current objective's topic memory, or an empty scope."""
        empty = {"schema_version": "topic-history-1", "scope_key": self.topic_history_scope,
                 "entries": [], "capability_counts": {}}
        path = self.topic_history_path
        if not path.is_file():
            return empty
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"topic history is unreadable: {path}") from exc
        self._validate_topic_history_document(document)
        scope = document["scopes"].get(self.topic_history_scope, {})
        if not isinstance(scope, dict):
            raise ValidationError("topic history scope is invalid")
        entries = scope.get("entries", [])
        if not isinstance(entries, list):
            raise ValidationError("topic history scope entries must be a list")
        # Keep the durable file complete.  Prompt projections are bounded in
        # `_topic_history_context`, but an old attempt remains available for
        # deterministic repeat checks and audit.
        entries = deepcopy(entries)
        counts = {}
        for entry in entries:
            capability = entry.get("experiment_capability_id")
            if isinstance(capability, str) and capability:
                counts[capability] = counts.get(capability, 0) + 1
        return {"schema_version": "topic-history-1", "scope_key": self.topic_history_scope,
                "entries": entries, "capability_counts": counts}

    def _effective_topic_exclusions(self):
        """Merge configured exclusions with automatic recent-direction memory."""
        configured = self.workflow.get("topic_exclusions") or {}
        capability_ids = list(configured.get("capability_ids", [])) if isinstance(configured, dict) else []
        topic_ids = list(configured.get("topic_ids", [])) if isinstance(configured, dict) else []
        entries = self.topic_history.get("entries", [])
        for entry in entries:
            topic_id = entry.get("topic_id") if isinstance(entry, dict) else None
            if isinstance(topic_id, str) and topic_id and topic_id not in topic_ids:
                topic_ids.append(topic_id)
        catalog_ids = [
            item.get("id") for item in self.workflow.get("experiment_catalog", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        # Rotate the most recently attempted capability while another
        # capability remains eligible.  Once the portfolio has been covered,
        # the next direction may return to the least-recent capability.
        if len(catalog_ids) > 1:
            explicit = set(capability_ids)
            available = [item for item in catalog_ids if item not in explicit]
            recent_capability = next(
                (entry.get("experiment_capability_id") for entry in reversed(entries)
                 if isinstance(entry, dict) and isinstance(entry.get("experiment_capability_id"), str)),
                None,
            )
            if (recent_capability in available
                    and any(item != recent_capability for item in available)
                    and recent_capability not in capability_ids):
                capability_ids.append(recent_capability)
        return {"capability_ids": list(dict.fromkeys(capability_ids)),
                "topic_ids": list(dict.fromkeys(topic_ids))}

    def _topic_history_context(self):
        """Return bounded history summaries suitable for the intake prompt."""
        entries = []
        for entry in self.topic_history.get("entries", [])[-24:]:
            if not isinstance(entry, dict):
                continue
            entries.append({key: entry.get(key) for key in (
                "topic_id", "title", "domain", "research_question", "experiment_capability_id",
                "parent_topic_id", "refinement_cycle", "changed_dimensions")})
        return {
            "schema_version": "topic-history-1",
            "scope_key": self.topic_history_scope,
            "entries": entries,
            "capability_counts": deepcopy(self.topic_history.get("capability_counts", {})),
        }

    def _record_topic_history(self, context):
        """Append an accepted topic selection to the project-family memory."""
        topic = context.get("topic") if isinstance(context, dict) else None
        if not isinstance(topic, dict) or not isinstance(topic.get("id"), str):
            return
        from scisaurus.runtime.topic_discovery import topic_signature
        entry = {
            "topic_id": topic["id"],
            "title": topic.get("title"),
            "domain": topic.get("domain"),
            "research_question": topic.get("research_question"),
            "experiment_capability_id": topic.get("experiment_capability_id"),
            "signature": topic_signature(topic),
            "run_id": self.run_id,
            "recorded_at": now_iso(),
        }
        evolution = context.get("topic_evolution") if isinstance(context, dict) else None
        if isinstance(evolution, dict) and evolution.get("mode") == "refinement":
            entry["parent_topic_id"] = evolution.get("parent_topic_id")
            entry["refinement_cycle"] = evolution.get("cycle")
            entry["refinement_reason"] = evolution.get("reason")
            entry["changed_dimensions"] = list(evolution.get("changed_dimensions", []))
        path = self.topic_history_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(path) + ".lock")
        lock = lock_path.open("a+")
        flock = None
        try:
            try:
                import fcntl
                flock = fcntl
                flock.flock(lock.fileno(), flock.LOCK_EX)
            except (ImportError, OSError):
                flock = None
            if path.is_file():
                try:
                    document = json.loads(path.read_text())
                except (OSError, ValueError) as exc:
                    raise ValidationError(f"topic history is unreadable: {path}") from exc
                self._validate_topic_history_document(document)
            else:
                document = {"schema_version": "topic-history-1", "scopes": {}}
            scope = document["scopes"].setdefault(self.topic_history_scope, {"entries": []})
            entries = scope.setdefault("entries", [])
            fingerprint = entry["signature"]["fingerprint"]
            new_entry = False
            if not any(
                    isinstance(item, dict)
                    and (item.get("run_id") == self.run_id
                         or ((item.get("signature") or {}).get("fingerprint") == fingerprint))
                    for item in entries):
                entries.append(entry)
                new_entry = True
            scope["entries"] = entries
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(canonical_bytes(document))
            os.replace(temporary, path)
            self.topic_history = self._load_topic_history()
            if new_entry and self.control is not None:
                self._publish(
                    f"command/composer/topic-history/{topic['id']}",
                    "note",
                    {"schema_version": "topic-history-entry-1",
                     "scope_key": self.topic_history_scope, "entry": entry},
                    "command.composer",
                )
        finally:
            if flock is not None:
                try:
                    flock.flock(lock.fileno(), flock.LOCK_UN)
                except OSError:
                    pass
            lock.close()

    def _requests_for_stage(self, stage_id):
        return [deepcopy(item) for item in self.active_research_requests
                if item.get("source_stage_id") == stage_id
                or stage_id in self.reopened_stage_ids]

    @staticmethod
    def _research_request_signature(request):
        """Return a stable identity for one substantive work-order attempt."""
        stable = {key: request.get(key) for key in (
            "id", "kind", "owner", "objective", "why", "success_condition",
            "evidence_needed", "source_stage_id")}
        return hashlib.sha256(canonical_bytes(stable)).hexdigest()

    def _mark_research_requests_attempted(self, stage):
        """Fence echoed work orders after their owning stage has returned.

        A scientific hold remains visible as an open departmental order, but
        the same request must not reopen the identical stage closure in a hot
        loop.  A changed request body receives a different signature and can
        trigger a new continuation in this invocation.
        """
        from scisaurus.runtime.departments import REQUEST_STAGE_KINDS

        for request in self.active_research_requests:
            if not isinstance(request, dict):
                continue
            if REQUEST_STAGE_KINDS.get(request.get("kind")) != stage.get("kind"):
                continue
            self._attempted_request_signatures.add(self._research_request_signature(request))

    def _free_topic_stage(self):
        """Return a topic stage that feeds a scholarly survey.

        A free-topic mission is defined by its dependency graph, not by the
        presence of a particular executable capability catalog.  Catalogs are
        useful for selecting pinned experiment adapters, while a topic→survey
        edge is the actual boundary at which literature evidence must be able
        to send the question back for redesign.
        """
        stages = self.workflow.get("stages", [])
        for topic in stages:
            if topic.get("kind") != "topic_discovery":
                continue
            pending = [topic.get("id")]
            seen = set()
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                for stage in stages:
                    if current not in stage.get("depends_on", []):
                        continue
                    if stage.get("kind") == "survey":
                        return topic
                    pending.append(stage.get("id"))
        return None

    def _topic_stage_for_survey(self, survey_stage=None):
        """Find the topic ancestor for one survey stage, if any."""
        if survey_stage is None:
            return self._free_topic_stage()
        by_id = {item.get("id"): item for item in self.workflow.get("stages", [])
                 if isinstance(item, dict)}
        pending = list(survey_stage.get("depends_on", []))
        seen = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            candidate = by_id.get(current)
            if not isinstance(candidate, dict):
                continue
            if candidate.get("kind") == "topic_discovery":
                return candidate
            pending.extend(candidate.get("depends_on", []))
        return None

    def _survey_context_for_topic(self, topic_stage):
        """Return the latest survey context downstream of a topic stage."""
        stages = self.workflow.get("stages", [])
        by_id = {item.get("id"): item for item in stages if isinstance(item, dict)}
        pending = [topic_stage.get("id")]
        seen = set()
        survey_ids = []
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for stage in stages:
                if current not in stage.get("depends_on", []):
                    continue
                if stage.get("kind") == "survey":
                    survey_ids.append(stage.get("id"))
                pending.append(stage.get("id"))
        for survey_id in survey_ids:
            context = self.context.get(survey_id)
            if isinstance(context, dict):
                return context
        return next((value for value in self.context.values()
                     if isinstance(value, dict) and value.get("kind") == "survey"), {})

    def _topic_refinement_context(self, stage):
        """Build the evidence handoff for a substantive topic revision."""
        if stage.get("kind") != "topic_discovery":
            return None
        requests = [item for item in self._requests_for_stage(stage["id"])
                    if item.get("kind") == "topic_refinement"]
        if not requests:
            return None
        parent_context = self.context.get(stage["id"], {})
        parent = parent_context.get("topic") if isinstance(parent_context, dict) else None
        if not isinstance(parent, dict):
            return None
        survey = self._survey_context_for_topic(stage)
        survey_evidence = {}
        survey_dir = survey.get("project_dir") if isinstance(survey, dict) else None
        if isinstance(survey_dir, str):
            survey_root = Path(survey_dir).resolve() / "output"
            for filename in ("gap-assessment.json", "coverage.json"):
                path = survey_root / filename
                if not path.is_file():
                    continue
                try:
                    value = json.loads(path.read_text())
                except (OSError, ValueError, TypeError):
                    continue
                # Keep the handoff bounded; source spans and work IDs are
                # enough to direct the next search without copying a whole
                # literature database into the topic model prompt.
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
                if len(encoded) > 20000:
                    encoded = encoded[:20000]
                survey_evidence[filename] = encoded
        feedback = {
            "gap_state": survey.get("gap_state") if isinstance(survey, dict) else None,
            "nomination": deepcopy(survey.get("nomination")) if isinstance(survey, dict) else None,
            "assessment_ref": survey.get("assessment_ref") if isinstance(survey, dict) else None,
            "evidence": survey_evidence,
            "requests": [{key: item.get(key) for key in (
                "id", "kind", "objective", "why", "success_condition", "evidence_needed")}
                         for item in requests],
        }
        return {
            "mode": "refinement",
            "cycle": self.continuation_cycles,
            "parent_topic_id": parent.get("id"),
            "parent_topic": deepcopy(parent),
            "reason": "The literature and admission review did not support the current question as a sufficient journal study.",
            "changed_dimensions": list(("mechanism", "data_regime", "comparison", "measurement", "theory")),
            "survey_feedback": feedback,
        }

    def _gate_free_topic_survey(self, result, *, stage=None):
        """Hold a free-topic mission until its question survives literature review.

        A bounded survey can be faithful while still failing to establish an
        experiment-worthy distinction.  Treat that outcome as a scientific
        redesign request instead of allowing the Composer to pass a thin
        question directly to the executable experiment.
        """
        if self._topic_stage_for_survey(stage) is None or not isinstance(result, dict):
            return result
        if result.get("status") not in {"completed", "accepted"}:
            return result
        state = result.get("gap_state")
        if state == "eligible_for_experiment":
            return result
        if state not in {"refuted_by_prior_work", "insufficient_evidence"}:
            return result
        gated = deepcopy(result)
        requests = [deepcopy(item) for item in result.get("research_expansion_requests", [])
                    if isinstance(item, dict)]
        known = {item.get("id") for item in requests if isinstance(item.get("id"), str)}

        def add(item):
            if item["id"] not in known:
                requests.append(item)
                known.add(item["id"])

        if state == "insufficient_evidence":
            # The first insufficient survey is an evidence problem, not a
            # scientific-direction problem.  Give the survey one scoped
            # expansion pass before asking the topic stage to pivot.  The
            # previous Composer result is durable context, so a resumed or
            # continued run can distinguish that first pass from a repeated
            # failure without relying on an in-memory counter.
            prior_survey = self._survey_context_for_topic(
                self._topic_stage_for_survey(stage))
            expanded_before = (
                isinstance(prior_survey, dict)
                and prior_survey.get("gap_state") == "insufficient_evidence"
                and prior_survey.get("topic_admission") in {
                    "expand_literature_before_refine", "refine_before_experiment"
                })
            add({
                "id": "topic-literature-expansion",
                "kind": "literature_expansion",
                "owner": "research.intelligence",
                "objective": "Expand the scholarly search with exact terminology, citation chaining, and verified full text for the selected question before making an admission decision.",
                "why": "The recorded survey does not yet provide enough comparable evidence to judge whether the question distinguishes unresolved work.",
                "success_condition": "A current survey and gap assessment either establishes experiment eligibility or supplies evidence for a substantive question redesign.",
                "evidence_needed": "Relevant primary studies, identity reconciliation, full-text source spans, and a current gap assessment.",
            })
            if expanded_before:
                add({
                    "id": f"topic-refinement-{state}",
                    "kind": "topic_refinement",
                    "owner": "research.intelligence",
                    "objective": "Redesign the selected question around the expanded survey evidence so that it tests a meaningful mechanism, comparison, boundary, or measurement rather than repeating the current narrow direction.",
                    "why": "The expanded literature search still cannot establish an experiment-worthy distinction; the question must evolve before execution.",
                    "success_condition": "A new question materially changes at least one substantive dimension, survives the topic maturity review, and is re-surveyed under its own search terms.",
                    "evidence_needed": "The parent question, expanded survey map, gap assessment, counter-search findings, and an explicit account of the changed scientific dimension.",
                })
        elif state == "refuted_by_prior_work":
            add({
                "id": f"topic-refinement-{state}",
                "kind": "topic_refinement",
                "owner": "research.intelligence",
                "objective": "Redesign the selected question around the refuting literature so that it tests a meaningful mechanism, comparison, boundary, or measurement rather than repeating an established result.",
                "why": "The current literature assessment identifies prior work that already answers the selected question; the question must pivot before execution.",
                "success_condition": "A new question materially changes at least one substantive dimension, survives the topic maturity review, and is re-surveyed under its own search terms.",
                "evidence_needed": "The parent question, refuting source spans, survey map, counter-search findings, and an explicit account of the changed scientific dimension.",
            })
        gated["status"] = "research_expansion_required"
        gated["topic_admission"] = (
            "refine_before_experiment"
            if any(item.get("kind") == "topic_refinement" for item in requests)
            else "expand_literature_before_refine"
        )
        gated["research_expansion_requests"] = requests
        return gated

    def _address_for_role(self, role):
        """Resolve a role through the live project charter.

        The static map remains a compatibility fallback for command roles and
        old workflows.  A custom organization chief is authoritative for its
        department, so every new handoff uses that chief automatically.
        """
        if isinstance(role, str):
            department = role.split(".", 1)[0]
            if department in self.departments.charters:
                return self.departments.address(department)
        return deepcopy(DEPARTMENT_ADDRESSES.get(role, COMMAND_ADDRESSES["arbiter"]))

    @staticmethod
    def _prior_stage_project(stage_id, context):
        value = context.get(stage_id, {}) if isinstance(context, dict) else {}
        path = value.get("project_dir") if isinstance(value, dict) else None
        return Path(path).resolve() if isinstance(path, str) and Path(path).is_absolute() else None

    def _augment_full_text_routes(self, config, source_project):
        """Add OA locations from the incumbent survey for a full-text continuation."""
        if not source_project:
            return
        works_path = source_project / "output" / "works.json"
        try:
            works = json.loads(works_path.read_text())
        except (OSError, ValueError):
            return
        failed_ids = set()
        coverage_path = source_project / "output" / "coverage.json"
        try:
            coverage = json.loads(coverage_path.read_text())
            failed_ids = {
                item.get("work_id") for item in coverage.get("access_and_limit_gaps", [])
                if isinstance(item, dict) and item.get("kind") == "full_text_failure"
            }
        except (OSError, ValueError, AttributeError):
            pass
        survey = config.get("survey") if isinstance(config, dict) else None
        if not isinstance(survey, dict) or not isinstance(survey.get("full_text_sources"), list):
            return
        existing = {item.get("work_id") for item in survey["full_text_sources"] if isinstance(item, dict)}
        for work in works:
            if (not isinstance(work, dict) or work.get("work_id") in existing
                    or work.get("work_id") in failed_ids):
                continue
            locations = work.get("locations") or []
            location = next((item for item in locations if isinstance(item, dict)
                             and (item.get("pdf_url") or item.get("landing_page_url"))), None)
            url = ((location.get("pdf_url") or location.get("landing_page_url"))
                   if location is not None else None)
            if not url:
                url = work.get("source_url")
            if not url:
                doi = work.get("doi")
                url = "https://doi.org/" + doi if isinstance(doi, str) and doi.strip() else None
            if not url:
                continue
            survey["full_text_sources"].append({
                "work_id": work["work_id"], "title": work["title"], "url": url,
                "section_markers": ["Abstract"],
            })
            existing.add(work["work_id"])

    def _apply_topic_to_survey_config(self, stage, config):
        """Project the selected free-topic question into its survey intake.

        A free-topic workflow should be runnable without hand-written binding
        entries for the common question/query fields.  Explicit bindings still
        run afterward and therefore remain authoritative for deliberate
        overrides.
        """
        topic_stage_ids = {item["id"] for item in self.workflow["stages"]
                           if item["kind"] == "topic_discovery"}
        pending = list(stage.get("depends_on", []))
        upstream = set()
        by_id = {item["id"]: item for item in self.workflow["stages"]}
        while pending:
            dependency = pending.pop()
            if dependency in upstream:
                continue
            upstream.add(dependency)
            pending.extend(by_id.get(dependency, {}).get("depends_on", []))
        if not (upstream & topic_stage_ids):
            return config
        topic_context = next((value for value in self.context.values()
                              if isinstance(value, dict)
                              and value.get("kind") == "topic_discovery"
                              and isinstance(value.get("topic"), dict)), None)
        if topic_context is None:
            return config
        survey = config.get("survey") if isinstance(config, dict) else None
        topic = topic_context["topic"]
        if not isinstance(survey, dict):
            return config
        # A catalog-backed stage is a new project identity.  Reusing the
        # template's project or capability IDs would leak the previous
        # experiment into the survey ledger and can collide with its reserved
        # verification capacity.  Keep one worker slot for the single-request
        # provider while reserving the independent verification slot required
        # by the survey contract.
        config["project_id"] = str(Path(stage["project_dir"]).resolve())
        limits = config.setdefault("limits", {})
        if type(limits.get("concurrent_calls")) is int and limits["concurrent_calls"] < 2:
            limits["concurrent_calls"] = 2
        limits["worker_concurrency"] = 1
        if isinstance(topic.get("id"), str):
            topic_key = re.sub(r"[^a-z0-9_-]+", "-", topic["id"].casefold()).strip("-")[:48]
            if topic_key:
                survey["id"] = f"topic-{topic_key}-literature"[:64]
                for capability_key in ("bibliography", "identity", "full_text"):
                    capability = survey.get(capability_key)
                    if isinstance(capability, dict) and isinstance(capability.get("id"), str):
                        capability["id"] = f"topic-{topic_key}-{capability_key}"[:64]
        survey["question"] = topic["research_question"]
        if isinstance(topic.get("id"), str):
            survey["proposed_gap"] = {
                "id": f"topic-{topic['id']}"[:64],
                "statement": topic.get("why_promising") or topic["research_question"],
            }
        # The discovery sampler is intentionally broad: it gives the topic
        # selector a current landscape, but those records are not evidence
        # for the selected question.  Carrying their IDs into the survey
        # would silently mix unrelated papers into the new study.  A
        # catalog-backed mission therefore starts from the selected queries;
        # the survey provider, planner, and expansion rounds build the seed
        # set from question-relevant records.  Legacy non-catalog workflows
        # retain their explicit seed IDs.
        if self.workflow.get("experiment_catalog"):
            survey["seed_work_ids"] = []
        else:
            recent = topic_context.get("recent_papers", [])
            seed_work_ids = [item.get("work_id") for item in recent
                             if isinstance(item, dict) and re.fullmatch(r"W\d+", str(item.get("work_id", "")))]
            survey["seed_work_ids"] = list(dict.fromkeys(seed_work_ids))[:max(0, survey.get("search", {}).get("max_works", 0) - survey.get("search", {}).get("challenge_reserve", 0))]
        if self.workflow.get("experiment_catalog"):
            # Explicit full-text routes are part of a fixed study template.
            # Let the current survey discover open locations for the selected
            # question instead of fetching the previous topic's URLs.
            survey["full_text_sources"] = []
        queries = topic.get("search_queries")
        if isinstance(queries, list) and queries:
            unique_queries = self._topic_search_queries(topic)
            # The survey's configured planner width is the explicit intake
            # budget.  A topic may propose more discovery terms, but silently
            # dispatching all of them would defeat provider-aware quotas.
            width = (survey.get("search", {}) or {}).get("queries_per_role")
            if type(width) is int and width > 0:
                unique_queries = unique_queries[:width]
            survey["seed_queries"] = unique_queries
        if isinstance(config.get("objective"), str):
            config["objective"] = (
                f"Investigate the selected question with a bounded scholarly survey: "
                f"{topic['research_question']}")
        if isinstance(config.get("supplied_context"), str):
            config["supplied_context"] = (
                "A catalog-backed free-topic intake selected this direction. "
                "Rebuild the literature map around the current question and preserve only evidence "
                "that is relevant to the selected study.\n" + topic["research_question"])
        return config

    @staticmethod
    def _topic_search_queries(topic):
        """Add one compact exact-concept query to the selected topic.

        OpenAlex's stemmed ``search`` endpoint treats long mixed queries as a
        relevance bag.  A broad first query can therefore return unrelated
        records before a distinctive method phrase is ever searched.  The
        selected topic already supplies the terminology; recover a repeated
        two-to-four-token phrase (or a hyphenated method name) and place its
        exact form first.  The rest of the model's queries remain intact.
        """
        raw = [item.strip() for item in topic.get("search_queries", [])
               if isinstance(item, str) and item.strip()]
        raw = list(dict.fromkeys(raw))
        if not raw:
            return []
        text = " ".join([
            str(topic.get("title", "")),
            str(topic.get("research_question", "")),
            *raw,
        ])
        phrase_candidates = []
        for match in re.finditer(r"\b[a-zA-Z][a-zA-Z0-9]*(?:-[a-zA-Z0-9]+)+\b", text):
            pieces = [piece.casefold() for piece in match.group(0).split("-") if piece]
            if len(pieces) >= 2:
                phrase_candidates.append(" ".join(pieces))
        token_lists = [re.findall(r"[a-zA-Z][a-zA-Z0-9]*", query.casefold()) for query in raw]
        stop = {
            "a", "an", "and", "at", "by", "for", "from", "how", "in", "of", "on", "or", "the", "to",
            "under", "with", "without", "versus", "relative", "does", "do", "can", "what", "when", "which",
            "using", "across", "between", "within", "on",
        }
        if token_lists:
            first = token_lists[0]
            for width in range(4, 1, -1):
                for start in range(0, len(first) - width + 1):
                    window = first[start:start + width]
                    content = [token for token in window if token not in stop and not token.isdigit()]
                    if len(content) < 2:
                        continue
                    support = sum(set(window).issubset(set(other)) for other in token_lists[1:])
                    if support:
                        phrase_candidates.append(" ".join(window))
        phrase = next((candidate for candidate in phrase_candidates
                       if len(candidate.split()) >= 2), None)
        if phrase:
            exact_query = f'"{phrase}"'
            raw = [exact_query, *raw]
        return list(dict.fromkeys(raw))

    def _apply_topic_to_experiment_config(self, stage, config):
        """Select a pinned executable capability for a free-topic mission.

        A topic proposal is only actionable when the workflow exposes more
        than one independently pinned experiment capability.  The selected
        template supplies the scientific design and program pair; the current
        run supplies the accepted literature gate and project-local paths.
        This keeps topic discovery genuinely exploratory without granting a
        model authority to invent an unreviewed command.
        """
        catalog = self.workflow.get("experiment_catalog")
        if not catalog:
            return config
        topic_context = next((value for value in self.context.values()
                              if isinstance(value, dict)
                              and value.get("kind") == "topic_discovery"
                              and isinstance(value.get("topic"), dict)), None)
        if topic_context is None:
            return config
        selected = topic_context["topic"]
        capability_id = selected.get("experiment_capability_id")
        entry = next((item for item in catalog if item["id"] == capability_id), None)
        if entry is None:
            raise ValidationError(
                "topic selection must name one configured experiment capability")
        try:
            template = json.loads(Path(entry["config_path"]).read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(
                f"experiment capability template is unreadable: {entry['config_path']}") from exc
        experiment = template.get("experiment") if isinstance(template, dict) else None
        if not isinstance(experiment, dict):
            raise ValidationError("experiment capability template must contain an experiment object")
        current = config.get("experiment")
        if not isinstance(current, dict):
            raise ValidationError("experiment stage config must contain an experiment object")
        selected_experiment = deepcopy(experiment)
        # The template is a pinned capability, while the live literature gate
        # belongs to this mission and is supplied by Composer bindings below.
        selected_experiment["literature_gate"] = deepcopy(current.get("literature_gate"))
        selected_experiment["revision"] = int(current.get("revision", selected_experiment.get("revision", 1)))
        config["experiment"] = selected_experiment
        config["project_id"] = str(Path(stage["project_dir"]).resolve())
        for branch in ("execution", "validation"):
            client = config["experiment"][branch].get("client")
            if isinstance(client, dict):
                client["cwd"] = str(Path(stage["project_dir"]).resolve())
        context = config.get("supplied_context", "")
        config["supplied_context"] = (
            f"{context}\nSelected executable capability: {capability_id}.\n"
            f"Selected research question: {selected.get('research_question', '')}"
        )
        return config

    @staticmethod
    def _synchronize_research_packet_identity(packet):
        """Replace stale template identity after a current result is bound."""
        if not isinstance(packet, dict):
            return packet
        results = packet.get("results_package")
        if not isinstance(results, dict):
            return packet
        question = results.get("question")
        if isinstance(question, str) and question.strip():
            packet["study_question"] = question
        procedure = next((item.get("description") for item in results.get("procedures", [])
                          if isinstance(item, dict) and isinstance(item.get("description"), str)
                          and item.get("description").strip()), None)
        if isinstance(procedure, str) and procedure.strip():
            packet["scope_statement"] = procedure
        # A packet prepared for another experiment must not retain its old
        # evidence IDs or literature prose.  The current survey projection is
        # added by the paper synchronizer; result IDs remain authoritative for
        # the interpretation and argument stages.
        result_ids = []
        for key in ("procedures", "metrics", "findings"):
            result_ids.extend(item.get("id") for item in results.get(key, [])
                              if isinstance(item, dict) and isinstance(item.get("id"), str))
        result_ids.extend(f"limitation-{index}" for index, _ in enumerate(results.get("limitations", [])))
        packet["evidence_ids"] = list(dict.fromkeys(result_ids))
        packet["literature_evidence"] = []
        packet["reference_cards"] = []
        return packet

    def _downstream_paper_stage(self, stage_id):
        """Find the paper consumer whose publication contract governs a stage."""
        by_id = {item["id"]: item for item in self.workflow["stages"]}
        pending = [item["id"] for item in self.workflow["stages"]
                   if item["kind"] == "paper" and stage_id in item.get("depends_on", [])]
        seen = set()
        while pending:
            candidate = pending.pop(0)
            if candidate in seen:
                continue
            seen.add(candidate)
            stage = by_id.get(candidate)
            if stage is None:
                continue
            if stage["kind"] == "paper":
                return stage
            pending.extend(next_stage["id"] for next_stage in by_id.values()
                           if candidate in next_stage.get("depends_on", []))
        # The direct-dependency walk above is sufficient for the ordinary
        # Composer graph.  Keep a transitive fallback for a branched workflow.
        for stage in by_id.values():
            if stage["kind"] != "paper":
                continue
            pending = list(stage.get("depends_on", []))
            visited = set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                if current == stage_id:
                    return stage
                pending.extend(by_id.get(current, {}).get("depends_on", []))
        return None

    def _paper_depth_profile(self, stage_id):
        paper_stage = self._downstream_paper_stage(stage_id)
        if paper_stage is None:
            return None
        try:
            descriptor_path = Path(paper_stage["config_path"])
            descriptor = json.loads(descriptor_path.read_text())
            # Composer paper stages normally point at a small descriptor whose
            # ``paper_config_path`` names the independently versioned release
            # contract.  Read that nested contract before deciding whether an
            # upstream experiment/survey must satisfy journal-grade floors.
            nested_path = descriptor.get("paper_config_path")
            if isinstance(nested_path, str):
                candidate = Path(nested_path)
                if not candidate.is_absolute():
                    candidate = descriptor_path.parent / candidate
                if candidate.is_file():
                    descriptor = json.loads(candidate.read_text())
        except (OSError, ValueError, TypeError):
            return None
        if (descriptor.get("schema_version") == "paper-release-score-3"
                or descriptor.get("document_type") == "research_paper" and "depth_profile" in descriptor):
            try:
                from scisaurus.runtime.scholarly_depth import PROFILES, profile_for_paper
                return descriptor.get("depth_profile"), PROFILES[profile_for_paper(descriptor)]
            except (KeyError, ValidationError):
                return None
        return None

    def _ensure_journal_quality_contract(self, stage, config):
        """Bind a substantive analysis floor to experiments feeding a paper."""
        profile = self._paper_depth_profile(stage["id"])
        if profile is None or stage["kind"] != "experiment":
            return config
        experiment = config.get("experiment")
        if not isinstance(experiment, dict):
            return config
        from scisaurus.runtime.research_quality import ensure_minimum_quality_contract
        # The downstream journal floor is monotone: retain stricter local
        # requirements, but never let an older weak contract turn a research
        # paper into a thin validation note.
        experiment["quality_contract"] = ensure_minimum_quality_contract(
            experiment.get("quality_contract"), study_type=experiment.get("study_type"))
        return config

    def _argument_minimums(self, stage_id, config):
        """Raise argument display floors to the publication profile."""
        minimums = {"figures": 2, "tables": 1, "experiments": 2}
        profile = self._paper_depth_profile(stage_id)
        if profile is not None:
            _, floor = profile
            minimums["figures"] = max(minimums["figures"], int(floor.get("min_figures", 0)))
            minimums["tables"] = max(minimums["tables"], int(floor.get("min_tables", 0)))
        return minimums

    @staticmethod
    def _packet_unit_ids(packet):
        contract = packet.get("writer_contract", {}) if isinstance(packet, dict) else {}
        order = contract.get("section_order", []) if isinstance(contract, dict) else []
        ids = []
        for section in order:
            if not isinstance(section, dict):
                continue
            for unit_id in section.get("unit_ids", []):
                if isinstance(unit_id, str) and unit_id not in ids:
                    ids.append(unit_id)
        return ids

    def _synchronize_paper_figure_arguments(self, paper_config, packet, argument_package):
        """Carry every accepted result figure into the release bindings."""
        if paper_config.get("schema_version") != "paper-release-score-3":
            return paper_config
        results = packet.get("results_package") if isinstance(packet, dict) else None
        assets = [asset for asset in (results or {}).get("assets", [])
                  if isinstance(asset, dict) and asset.get("role") == "figure"]
        if not assets:
            return paper_config
        asset_ids = {asset.get("id") for asset in assets if isinstance(asset.get("id"), str)}
        # A paper descriptor can have been prepared against an earlier result
        # package.  Stale figure bindings are removed as one explicit
        # reconciliation step; carrying them forward would make the release
        # claim an asset that is no longer part of the accepted evidence.
        arguments = [item for item in paper_config.get("figure_arguments", [])
                     if isinstance(item, dict) and item.get("asset_id") in asset_ids]
        paper_config["figure_arguments"] = arguments
        known = {item.get("asset_id") for item in arguments}
        plan = {}
        argument = argument_package.get("argument") if isinstance(argument_package, dict) else None
        for item in (argument or {}).get("figure_plan", []):
            if isinstance(item, dict) and item.get("kind") == "figure" and isinstance(item.get("asset_id"), str):
                plan[item["asset_id"]] = item
        unit_ids = self._packet_unit_ids(packet)
        used_units = {item.get("unit_id") for item in arguments if isinstance(item, dict)}
        preferred = [unit_id for unit_id in unit_ids
                     if unit_id.startswith(("results_", "interpretation_"))]
        for asset in assets:
            asset_id = asset.get("id")
            if not isinstance(asset_id, str) or asset_id in known:
                continue
            planned = plan.get(asset_id, {})
            unit_id = next((item for item in preferred if item not in used_units), None)
            if unit_id is None:
                unit_id = next((item for item in preferred), None)
            if unit_id is None:
                # Let the normal release validator report an unbindable asset;
                # inventing a manuscript address would violate surgical edits.
                continue
            observation = planned.get("readout") or asset.get("caption")
            why = planned.get("purpose") or "This display exposes a result pattern needed by the argument."
            if not isinstance(observation, str) or not observation.strip():
                continue
            arguments.append({"asset_id": asset_id, "observation": observation,
                              "unit_id": unit_id, "why": why})
            known.add(asset_id)
            used_units.add(unit_id)
            contract = packet.get("writer_contract")
            if isinstance(contract, dict):
                readings = contract.setdefault("figure_readings", [])
                if not isinstance(readings, list):
                    readings = []
                    contract["figure_readings"] = readings
                readings[:] = [item for item in readings
                               if isinstance(item, dict) and item.get("asset_id") in asset_ids]
                if not any(isinstance(item, dict) and item.get("asset_id") == asset_id for item in readings):
                    readings.append({"asset_id": asset_id, "observation": observation,
                                     "unit_id": unit_id, "why": why})
        return paper_config

    def _synchronize_catalog_paper_inputs(self, paper_config, packet, argument_package):
        """Rebuild topic-dependent paper contracts from the accepted run.

        Free-topic stages begin with reusable descriptors so the Composer can
        validate a workflow before any provider call.  Those descriptors are
        structural templates, not scientific content.  Once a capability has
        produced a result, carrying the old claims, storyline, or figure jobs
        forward would make a new experiment wear the previous paper's skin.
        This projection keeps the section tree stable while replacing every
        content-bearing edge with the current result and accepted argument.
        """
        if not self.workflow.get("experiment_catalog"):
            return paper_config, packet
        if not isinstance(packet, dict):
            return paper_config, packet
        results = packet.get("results_package")
        if not isinstance(results, dict):
            return paper_config, packet

        argument = argument_package.get("argument") if isinstance(argument_package, dict) else None
        if isinstance(argument, dict):
            packet["research_argument"] = deepcopy(argument)
            if isinstance(argument_package.get("review"), dict):
                packet["research_argument_review"] = deepcopy(argument_package["review"])

        question = results.get("question") or packet.get("study_question")
        if not isinstance(question, str) or not question.strip():
            question = "What mechanism explains the observed difference under the declared study conditions?"
        procedure = next((item.get("description") for item in results.get("procedures", [])
                           if isinstance(item, dict) and isinstance(item.get("description"), str)
                           and item["description"].strip()), None)
        scope = procedure or packet.get("scope_statement") or question
        if not isinstance(scope, str) or not scope.strip():
            scope = question
        packet["study_question"] = question
        packet["scope_statement"] = scope
        packet["composition_goal"] = (
            "Write a complete scientific paper for the selected study. Preserve the observed results, "
            "explain competing mechanisms, connect each figure to an argument, and keep the conclusion "
            "inside the declared data and design boundary."
        )

        # The packet is the writer's literature-facing context.  It is rebuilt
        # from the current accepted survey so background prose cannot inherit a
        # prior experiment's source cards.
        survey = None
        if "survey" in self.context:
            from scisaurus.runtime.paper import load_paper_survey
            # An accepted survey is a hard dependency of a research-paper
            # stage.  Let a broken or stale survey identity reach the normal
            # Composer retry path instead of silently falling back to an old
            # packet with no literature basis.
            survey = load_paper_survey(paper_config)
        if isinstance(survey, dict):
            cards, literature = [], []
            for source_ref, source in list(survey.get("sources", {}).items())[:50]:
                if not isinstance(source, dict):
                    continue
                work_id = source.get("work_id")
                if not isinstance(work_id, str) or not work_id:
                    continue
                title = source.get("title") or work_id
                abstract = source.get("abstract") or source.get("text") or ""
                if not isinstance(abstract, str):
                    abstract = str(abstract)
                authors = source.get("authors") or "Authors not supplied by the survey."
                if isinstance(authors, list):
                    authors = ", ".join(
                        item if isinstance(item, str) else str(item)
                        for item in authors)
                cards.append({
                    "source_ref": source_ref, "work_id": work_id,
                    "title": str(title), "authors": str(authors),
                    "year": source.get("year"),
                    "abstract": abstract[:1800],
                    "representation": source.get("representation"),
                    "reader_use": "Use this record only for the background and related-work context supported by the survey.",
                })
                literature.append({
                    "id": f"literature-{len(literature)}", "work_id": work_id,
                    "source_ref": source_ref, "quote": str(title),
                    "relation": "context",
                })
            packet["reference_cards"] = cards
            packet["literature_evidence"] = literature

        unit_ids = self._packet_unit_ids(packet)
        if not unit_ids:
            return paper_config, packet

        def unit_for(*prefixes):
            return next((item for item in unit_ids
                         if item.startswith(prefixes)), unit_ids[0])

        def first_text(items, key):
            return next((item.get(key) for item in items
                         if isinstance(item, dict) and isinstance(item.get(key), str)
                         and item[key].strip()), None)

        procedures = [item for item in results.get("procedures", []) if isinstance(item, dict)]
        metrics = [item for item in results.get("metrics", []) if isinstance(item, dict)]
        findings = [item for item in results.get("findings", []) if isinstance(item, dict)]
        raw_limitations = [item for item in results.get("limitations", []) if isinstance(item, str) and item.strip()]
        evidence, evidence_by_kind = [], {key: [] for key in ("procedure", "result", "finding", "limitation")}
        evidence_ids = set()

        def add_evidence(kind, locator, quote, relation="support"):
            if not isinstance(locator, str) or not locator.strip() or not isinstance(quote, str) or not quote.strip():
                return None
            prefix = {"procedure": "procedure", "result": "result", "finding": "finding",
                      "limitation": "limitation"}[kind]
            base = f"{prefix}-{locator.replace('/', '-') }".replace(" ", "-")
            base = re.sub(r"[^a-zA-Z0-9_-]", "-", base).strip("-").casefold()
            if not base or not base[0].isalpha():
                base = f"evidence-{prefix}"
            evidence_id = base[:64]
            suffix = 2
            while evidence_id in evidence_ids:
                stem = base[: max(1, 63 - len(str(suffix)))]
                evidence_id = f"{stem}-{suffix}"
                suffix += 1
            item = {"id": evidence_id, "kind": kind, "locator": locator,
                    "quote": quote, "relation": relation}
            evidence.append(item)
            evidence_ids.add(evidence_id)
            evidence_by_kind[kind].append(evidence_id)
            return evidence_id

        for item in procedures:
            add_evidence("procedure", item.get("id"), item.get("description"))
        for item in metrics:
            add_evidence("result", item.get("id"), item.get("presentation"))
        for item in findings:
            add_evidence("finding", item.get("id"), item.get("statement"))
        for index, limitation in enumerate(raw_limitations):
            add_evidence("limitation", f"limitation/{index}", limitation, "qualify")

        fallback_evidence = next((items for items in evidence_by_kind.values() if items), [])
        if not fallback_evidence:
            return paper_config, packet

        thesis = None
        if isinstance(argument, dict):
            primary = argument.get("primary_argument")
            if isinstance(primary, dict) and isinstance(primary.get("thesis"), str) and primary["thesis"].strip():
                thesis = primary["thesis"].strip()
        thesis = thesis or (findings[0].get("statement") if findings else question)
        result_proposition = first_text(findings, "statement") or first_text(metrics, "presentation") or question
        limitation_proposition = raw_limitations[0] if raw_limitations else (
            "The interpretation is limited to the declared data, model, and parameter range.")
        method_proposition = procedure or "The study follows the declared reproducible comparison protocol."
        beats = [
            {"id": "motivation", "role": "motivation",
             "proposition": f"The study examines {question.rstrip('?')} under a defined empirical boundary."},
            {"id": "question", "role": "question", "proposition": question},
            {"id": "method", "role": "method", "proposition": method_proposition},
            {"id": "result", "role": "result", "proposition": result_proposition},
            {"id": "interpretation", "role": "interpretation", "proposition": thesis},
            {"id": "limitation", "role": "limitation", "proposition": limitation_proposition},
            {"id": "conclusion", "role": "conclusion", "proposition": thesis},
        ]
        beat_evidence = {
            "motivation": evidence_by_kind["procedure"] or evidence_by_kind["result"] or fallback_evidence,
            "question": evidence_by_kind["procedure"] or evidence_by_kind["result"] or fallback_evidence,
            "method": evidence_by_kind["procedure"] or fallback_evidence,
            "result": evidence_by_kind["finding"] or evidence_by_kind["result"] or fallback_evidence,
            "interpretation": evidence_by_kind["finding"] or evidence_by_kind["result"] or fallback_evidence,
            "limitation": evidence_by_kind["limitation"] or fallback_evidence,
            "conclusion": evidence_by_kind["finding"] or evidence_by_kind["result"] or fallback_evidence,
        }
        beat_units = {
            "motivation": [unit_for("intro_")], "question": [unit_for("question_")],
            "method": [unit_for("methods_")], "result": [unit_for("results_")],
            "interpretation": [unit_for("interpretation_", "discussion_")],
            "limitation": [unit_for("limitations_")], "conclusion": [unit_for("conclusion_")],
        }

        if paper_config.get("schema_version") in {"paper-release-score-2", "paper-release-score-3"}:
            paper_config["storyline"] = {
                "id": str(paper_config.get("storyline", {}).get("id", "study-storyline")),
                "revision": int(paper_config.get("storyline", {}).get("revision", 1)) + 1,
                "thesis": thesis, "beats": beats,
            }
            paper_config["evidence"] = evidence
            paper_config["claims"] = [{
                "id": f"claim-{beat['id']}", "statement": beat["proposition"],
                "unit_ids": beat_units[beat["id"]],
                "evidence_ids": list(dict.fromkeys(beat_evidence[beat["id"]])),
                "storyline_id": beat["id"],
            } for beat in beats]
            topic = next((value.get("topic") for value in self.context.values()
                          if isinstance(value, dict) and value.get("kind") == "topic_discovery"
                          and isinstance(value.get("topic"), dict)), None)
            if isinstance(topic, dict) and isinstance(topic.get("title"), str) and topic["title"].strip():
                paper_config["title"] = topic["title"].strip()
            elif isinstance(results.get("id"), str):
                paper_config["title"] = results["id"].replace("_", " ").replace("-", " ").title()

        contract = packet.get("writer_contract") if isinstance(packet.get("writer_contract"), dict) else {}
        exact = []
        if procedure:
            exact.append({"text": procedure, "unit_id": unit_for("methods_")})
        for item in findings[:4]:
            exact.append({"text": item["statement"], "unit_id": unit_for("results_")})
        for item in raw_limitations[:2]:
            exact.append({"text": item, "unit_id": unit_for("limitations_")})
        exact.extend([
            {"text": question, "unit_id": unit_for("question_")},
            {"text": thesis, "unit_id": unit_for("interpretation_", "discussion_")},
        ])
        contract["required_exact_content"] = exact
        contract["required_citation_markers"] = [
            f"[[cite:{reference['key']}]]" for reference in paper_config.get("references", [])
            if isinstance(reference, dict) and isinstance(reference.get("key"), str)
        ]
        contract["figure_readings"] = [
            {"asset_id": item["asset_id"], "observation": item["observation"],
             "unit_id": item["unit_id"], "why": item["why"]}
            for item in paper_config.get("figure_arguments", [])
            if isinstance(item, dict)
        ]
        contract["surface_rules"] = [
            "Write for a scientific reader and keep execution bookkeeping out of the manuscript.",
            "Results report observations; interpretation and Discussion explain mechanisms and distinguish possibilities from established findings.",
            "Use each numeric result where it advances the argument and avoid repeating the same fact across sections.",
            "Limitations must state how the declared design boundary changes the interpretation.",
        ]
        packet["writer_contract"] = contract
        return paper_config, packet

    @staticmethod
    def _extend_paper_images(config, packet, paper_config):
        images = list(config.get("images", []))
        result_path = paper_config.get("results_package")
        base = Path(result_path).resolve().parent if isinstance(result_path, str) else None
        for asset in (packet.get("results_package", {}) or {}).get("assets", []):
            if not isinstance(asset, dict) or asset.get("role") != "figure" or base is None:
                continue
            candidate = (base / asset.get("path", "")).resolve()
            if candidate.is_file() and str(candidate) not in images:
                images.append(str(candidate))
        config["images"] = images
        return config

    def _adapt_continuation_config(self, stage, config):
        """Derive a fresh stage configuration from explicit research work orders."""
        requests = self._requests_for_stage(stage["id"])
        if not requests:
            return config
        kind = stage["kind"]
        if kind == "survey":
            config["project_id"] = str(Path(stage["project_dir"]).resolve())
            survey = config.get("survey", {})
            search = survey.get("search", {})
            # Expansion is deliberate and bounded.  It increases discovery,
            # full-text, and citation capacity together so the next gate does
            # not simply see more abstracts while remaining evidence-poor.
            journal_profile = self._paper_depth_profile(stage["id"])
            if journal_profile is not None:
                # A journal continuation must widen the search graph and the
                # full-text frontier together.  Increasing only the abstract
                # limit creates a larger bibliography without improving the
                # evidence needed for methods and related-work claims.
                search["queries_per_role"] = min(8, max(search.get("queries_per_role", 1) + 1, 4))
                search["results_per_query"] = min(100, max(search.get("results_per_query", 1) + 10, 50))
                search["max_works"] = min(1000, max(search.get("max_works", 1) + 50, 120))
                search["max_full_texts"] = min(100, max(search.get("max_full_texts", 1) + 10, 20))
                search["max_api_calls"] = min(1000, max(search.get("max_api_calls", 1) + 60, 100))
                search["challenge_reserve"] = min(20, max(search.get("challenge_reserve", 0), 5))
                search["saturation_rounds"] = min(8, max(search.get("saturation_rounds", 1), 3))
                search["expansion_seed_count"] = min(20, max(search.get("expansion_seed_count", 1), 4))
            else:
                search["max_works"] = min(1000, max(search.get("max_works", 1) + 10, 20))
                search["max_full_texts"] = min(100, max(search.get("max_full_texts", 1) + 5, 5))
                search["max_api_calls"] = min(1000, max(search.get("max_api_calls", 1) + 20, 40))
            search["expansion_rounds"] = min(8, search.get("expansion_rounds", 0) + 1)
            survey["revision"] = int(survey.get("revision", 1)) + self.continuation_cycles
            extra_queries = [item["objective"][:2048] for item in requests
                             if item.get("kind") in {"literature_expansion", "full_text_retrieval"}]
            for query in extra_queries:
                if query and query not in survey.get("seed_queries", []):
                    survey.setdefault("seed_queries", []).append(query)
            self._augment_full_text_routes(config, self._prior_stage_project("survey", self.context))
        elif kind == "experiment":
            config["project_id"] = str(Path(stage["project_dir"]).resolve())
            experiment = config.get("experiment", {})
            experiment["revision"] = int(experiment.get("revision", 1)) + self.continuation_cycles
            context = config.get("supplied_context", "")
            config["supplied_context"] = (
                f"{context}\nContinuation work orders:\n"
                + json.dumps(requests, ensure_ascii=False, sort_keys=True))
        elif kind in {"interpretation", "paper"}:
            # These descriptors are intentionally strict.  Materialize an
            # immutable, cycle-specific packet instead of smuggling control
            # fields into the descriptor or rerunning with unchanged input.
            packet_key = "input_path" if kind == "interpretation" else "packet_path"
            self._materialize_follow_up_packet(stage, config, requests, packet_key)
        return config

    @staticmethod
    def _follow_up_projection(requests):
        return [{
            "kind": item["kind"],
            "objective": item["objective"],
            "why": item["why"],
            "success_condition": item["success_condition"],
            "evidence_needed": item["evidence_needed"],
        } for item in requests]

    def _materialize_follow_up_packet(self, stage, config, requests, packet_key):
        source_value = config.get(packet_key)
        if not isinstance(source_value, str) or not Path(source_value).is_file():
            raise ValidationError(f"continuation {stage['kind']} packet is unreadable: {source_value}")
        try:
            packet = json.loads(Path(source_value).read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"continuation {stage['kind']} packet is not valid JSON: {source_value}") from exc
        if not isinstance(packet, dict):
            raise ValidationError(f"continuation {stage['kind']} packet must be a JSON object")
        packet = deepcopy(packet)
        packet["scientific_follow_up"] = self._follow_up_projection(requests)
        packet["follow_up_instruction"] = (
            "Address every supplied scientific follow-up by incorporating its evidence or analysis into this fresh pass. "
            "Keep unresolved explanations provisional and do not claim completion when the requested evidence is absent."
        )
        target = (Path(stage["project_dir"]).resolve() / "inputs"
                  / f"{stage['kind']}-follow-up-cycle-{self.continuation_cycles}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        body = canonical_bytes(packet)
        if target.exists() and target.read_bytes() != body:
            raise ValidationError(f"continuation packet was modified: {target}")
        if not target.exists():
            target.write_bytes(body)
        config[packet_key] = str(target)

    def _project_continuation_requests(self, packet, stage):
        """Give reopened interpretation and composition stages an explicit task.

        Their descriptors intentionally remain strict and immutable.  The
        scoped work order therefore travels with the fresh evidence packet,
        where the specialist can act on its objective and success condition
        without allowing Composer control vocabulary into the manuscript.
        """
        requests = self._requests_for_stage(stage["id"])
        if not requests:
            return packet
        packet["scientific_follow_up"] = self._follow_up_projection(requests)
        packet["follow_up_instruction"] = (
            "Address every supplied scientific follow-up by incorporating its evidence or analysis into this fresh pass. "
            "Keep unresolved explanations provisional and do not claim completion when the requested evidence is absent."
        )
        return packet

    @staticmethod
    def _attempt_stage(stage, attempt_number):
        """Isolate a retried stage while keeping every prior attempt inspectable."""
        candidate = deepcopy(stage)
        if attempt_number <= 1:
            return candidate
        base = Path(stage["project_dir"]).resolve()
        candidate["project_dir"] = str(base / "attempts" / f"attempt-{attempt_number}")
        # A failed attempt is never a reusable checkpoint.  Successful stages
        # are skipped by the Composer before this helper is reached.
        candidate["reuse_completed"] = False
        candidate["reuse_output_path"] = None
        return candidate

    def _record_retry_feedback(self, stage, *, attempt_number, retry_index, error, delay_seconds):
        """Record a retry decision before dispatching the next isolated attempt."""
        policy = self._retry_policy()
        event_id = f"composer-retry-{stage['id']}-{attempt_number}"
        feedback = {
            "event_id": event_id,
            "stage_id": stage["id"],
            "role": STAGE_ROLES[stage["kind"]],
            "from": COMMAND_ADDRESSES["progress"],
            "to": COMMAND_ADDRESSES["progress"],
            "status": "retrying",
            "action": "retry_stage",
            "attempt_number": attempt_number,
            "retry_index": retry_index,
            "retry_mode": policy.get("mode", "bounded"),
            "max_attempts": policy.get("max_attempts") if policy.get("mode", "bounded") == "bounded" else None,
            "delay_seconds": delay_seconds,
            "error": f"{type(error).__name__}: {error}",
            "next_condition": "dispatch a fresh isolated stage attempt within the Composer hard deadline",
            "stage_deadline_seconds": stage["deadline_seconds"],
            "dependencies": list(stage["depends_on"]),
            "message_id": event_id,
            "message_disposition": "scheduled",
        }
        self.feedback.append(feedback)
        note = self._publish(
            f"command/composer/retry/{stage['id']}-{attempt_number}",
            "decision_note", feedback, "command.composer")
        self._route_feedback(feedback, note)

    def _wait_before_retry(self, stage, *, attempt_number, retry_index, error, downstream_seconds):
        """Pace a retry without sleeping past the hard deadline."""
        policy = self._retry_policy()
        base_delay = float(policy["backoff_seconds"])
        if policy.get("mode", "bounded") == "until_deadline":
            # A zero-delay provider failure must not become a hot loop.  This
            # floor is a pacing guard, not a retry limit; the deadline remains
            # the only count-independent stop condition.
            base_delay = max(0.25, base_delay)
        delay = min(60.0, base_delay * (2 ** min(max(0, retry_index - 1), 6)))
        remaining = self._remaining()
        if policy.get("mode", "bounded") == "until_deadline":
            # In autonomous mode a failed stage may still be retried in the
            # residual window.  Keep only the control-plane margin here; the
            # specialist receives the actual remaining deadline and decides
            # whether its own contract can finish.
            required_window = self._deadline_dispatch_floor()
        else:
            required_window = min(float(stage["estimate_seconds"]), downstream_seconds)
        if remaining <= delay + max(0.2, required_window):
            return False
        self._record_retry_feedback(stage, attempt_number=attempt_number, retry_index=retry_index,
                                    error=error,
                                    delay_seconds=delay)
        wait_until = self.clock() + delay
        while True:
            left = wait_until - self.clock()
            if left <= 0:
                return True
            self._remaining()
            self._checkpoint(f"{stage['id']}:retry_wait")
            time.sleep(min(1.0, left))

    def _checkpoint(self, phase, *, force=False):
        now = self.clock()
        if not force and now < self.next_checkpoint:
            return
        # Refresh the organization only on the Composer thread.  The live
        # ticker consumes the cached plain-data projection below.
        self.organization_snapshot = deepcopy(self.departments.snapshot())
        remaining_snapshot = self.deadline - now
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining_snapshot = min(remaining_snapshot, self.deadline_epoch - time.time())
        state = {
            "schema_version": "composer-checkpoint-1", "workflow_id": self.workflow["id"],
            "run_id": self.run_id,
            "phase": phase, "elapsed_seconds": max(0.0, now - self.started),
            "remaining_seconds": max(0.0, remaining_snapshot),
            "started_at_epoch": self.started_epoch, "deadline_at_epoch": self.deadline_epoch,
            "retry_policy": self._retry_policy(),
            "continuation_policy": self._continuation_policy(),
            "exploration_seed": self.exploration_seed,
            "continuation_cycles": self.continuation_cycles,
            "reopened_stage_ids": sorted(self.reopened_stage_ids),
            "continuation_pending_stage_ids": sorted(self.continuation_pending_stage_ids),
            "active_research_requests": deepcopy(self.active_research_requests),
            "department_activity": deepcopy(self.department_activity),
            "deadline_extensions": deepcopy(self.deadline_extensions),
            "stages": deepcopy(self.stage_records), "context": deepcopy(self.context),
            "feedback": deepcopy(self.feedback),
            "blockers": deepcopy(self.blockers), "usage": deepcopy(self.usage),
            "deadline_decisions": deepcopy(self.deadline_decisions),
            "organization": deepcopy(self.organization_snapshot),
            "topic_history_path": str(self.topic_history_path),
            "topic_history_scope": self.topic_history_scope,
            "topic_history_entries": len(self.topic_history.get("entries", [])),
        }
        with self._progress_lock:
            self._progress_snapshot = deepcopy(state)
        self._publish(f"command/composer/checkpoints/{len(self.feedback) + len(self.stage_records) + 1}",
                      "progress_checkpoint", state, "command.composer")
        with self._progress_lock:
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "progress.json").write_bytes(canonical_bytes(state))
        self.next_checkpoint = now + float(self.workflow["time_policy"]["checkpoint_seconds"])
        self.on_progress({"phase": phase, "elapsed_seconds": round(state["elapsed_seconds"], 2),
                          "remaining_seconds": round(state["remaining_seconds"], 2),
                          "stages": deepcopy(state.get("stages", {})),
                          "blockers": deepcopy(state.get("blockers", [])),
                          "organization": deepcopy(state["organization"])})

    def _live_progress(self, phase, *, epoch=None):
        """Publish a non-ledger progress tick while a provider call is active.

        A long model request may not emit a semantic event until it returns.
        The tick deliberately writes only the live checkpoint projection; it
        does not create a decision or alter task state. Durable control events
        continue to be written by ``_checkpoint`` at stage boundaries.
        """
        now = self.clock()
        remaining_snapshot = self.deadline - now
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining_snapshot = min(remaining_snapshot, self.deadline_epoch - time.time())
        # The ticker must not read mutable Composer state while a specialist
        # callback is updating it.  It consumes the last complete checkpoint
        # and changes only the heartbeat fields for this atomic write.
        with self._progress_lock:
            if epoch is not None and epoch != self._live_progress_epoch:
                return
            state = deepcopy(self._progress_snapshot)
            state.setdefault("organization", deepcopy(self.organization_snapshot))
            state.update({
                "schema_version": "composer-checkpoint-1", "workflow_id": self.workflow["id"],
                "run_id": self.run_id,
                "phase": phase, "elapsed_seconds": max(0.0, now - self.started),
                "remaining_seconds": max(0.0, remaining_snapshot),
                "started_at_epoch": self.started_epoch, "deadline_at_epoch": self.deadline_epoch,
            })
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            temporary = output / f"progress-live-{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(canonical_bytes(state))
            temporary.replace(output / "progress.json")
        self.on_progress({"phase": phase, "elapsed_seconds": round(state["elapsed_seconds"], 2),
                          "remaining_seconds": round(state["remaining_seconds"], 2),
                          "stages": deepcopy(state.get("stages", {})),
                          "blockers": deepcopy(state.get("blockers", [])),
                          "organization": deepcopy(state["organization"])})

    def _start_live_progress(self, stage):
        """Start a bounded ticker for one admitted stage."""
        interval = max(0.5, float(self.workflow["time_policy"]["checkpoint_seconds"]))
        stop = threading.Event()
        with self._progress_lock:
            self._live_progress_epoch += 1
            epoch = self._live_progress_epoch

        def tick():
            while not stop.wait(interval):
                try:
                    self._live_progress(f"{stage['id']}:running", epoch=epoch)
                except Exception:
                    # A live status tick must never hide the stage's real
                    # result or turn a completed provider call into a failure.
                    return

        thread = threading.Thread(target=tick, name=f"composer-progress-{stage['id']}", daemon=True)
        thread.start()

        def finish():
            with self._progress_lock:
                self._live_progress_epoch += 1
            stop.set()
            thread.join(timeout=min(interval, 2.0))

        return finish

    def _restore(self):
        timing_state = None
        # The run input is the earliest durable record and carries the seed
        # even when a process exits before its first stage checkpoint.
        run_input = self.store.head("inputs/composer-run")
        if run_input is not None:
            try:
                input_body = json.loads(self.store.read_body(run_input["body_hash"]))
            except (OSError, TypeError, ValueError):
                input_body = None
            if (isinstance(input_body, dict) and type(input_body.get("exploration_seed")) is int
                    and input_body["exploration_seed"] >= 0):
                self.exploration_seed = input_body["exploration_seed"]
        head = self.store.head("command/composer/run")
        head_status = None
        if head is not None:
            body = json.loads(self.store.read_body(head["body_hash"]))
            self.status = body.get("status", "running")
            head_status = self.status
            self.stage_records = body.get("stages", {})
            self.context = body.get("context", {})
            self.feedback = body.get("feedback", [])
            self.blockers = body.get("blockers", [])
            self.usage = body.get("usage", self.usage)
            self.deadline_decisions = body.get("deadline_decisions", [])
            self.continuation_cycles = body.get("continuation_cycles", 0)
            self.reopened_stage_ids = set(body.get("reopened_stage_ids", []))
            self.continuation_pending_stage_ids = set(body.get("continuation_pending_stage_ids", []))
            self.active_research_requests = body.get("active_research_requests", [])
            self.department_activity = body.get("department_activity", [])
            self.deadline_extensions = body.get("deadline_extensions", [])
            if (type(body.get("exploration_seed")) is int
                    and body["exploration_seed"] >= 0):
                self.exploration_seed = body["exploration_seed"]
            if isinstance(body.get("organization"), dict):
                self.organization_snapshot = deepcopy(body["organization"])
            self._progress_snapshot = deepcopy(body)
            timing_state = body
        progress_path = self.root / "output" / "progress.json"
        # A process can die after a checkpoint but before publishing the final
        # run report.  Restore that checkpoint as the authoritative in-flight
        # state so the next Composer invocation can mark an interrupted
        # attempt unknown and dispatch a fresh one.
        if (head is None or head_status == "running") and progress_path.is_file():
            try:
                checkpoint = json.loads(progress_path.read_text())
            except (OSError, ValueError):
                checkpoint = None
            if isinstance(checkpoint, dict):
                timing_state = checkpoint
                self.stage_records = checkpoint.get("stages", self.stage_records)
                if "context" in checkpoint:
                    self.context = checkpoint.get("context", self.context)
                self.feedback = checkpoint.get("feedback", self.feedback)
                self.blockers = checkpoint.get("blockers", self.blockers)
                self.usage = checkpoint.get("usage", self.usage)
                self.deadline_decisions = checkpoint.get("deadline_decisions", self.deadline_decisions)
                self.continuation_cycles = checkpoint.get("continuation_cycles", self.continuation_cycles)
                self.reopened_stage_ids = set(checkpoint.get("reopened_stage_ids", self.reopened_stage_ids))
                self.continuation_pending_stage_ids = set(checkpoint.get(
                    "continuation_pending_stage_ids", self.continuation_pending_stage_ids))
                self.active_research_requests = checkpoint.get(
                    "active_research_requests", self.active_research_requests)
                self.department_activity = checkpoint.get("department_activity", self.department_activity)
                self.deadline_extensions = checkpoint.get("deadline_extensions", self.deadline_extensions)
                if (type(checkpoint.get("exploration_seed")) is int
                        and checkpoint["exploration_seed"] >= 0):
                    self.exploration_seed = checkpoint["exploration_seed"]
                if isinstance(checkpoint.get("organization"), dict):
                    self.organization_snapshot = deepcopy(checkpoint["organization"])
                self._progress_snapshot = deepcopy(checkpoint)
        self._restore_context_from_stage_records()
        # Older Composer reports did not carry an epoch fence.  They retain
        # their historical restart behavior; every new run persists the
        # fields above so future resumes remain inside one mission wall.
        if isinstance(timing_state, dict):
            started_epoch = timing_state.get("started_at_epoch")
            deadline_epoch = timing_state.get("deadline_at_epoch")
            if (type(started_epoch) in (int, float) and math.isfinite(started_epoch)
                    and type(deadline_epoch) in (int, float) and math.isfinite(deadline_epoch)
                    and deadline_epoch >= started_epoch):
                self.started_epoch = float(started_epoch)
                self.deadline_epoch = float(deadline_epoch)
                elapsed_hint = timing_state.get("elapsed_seconds", 0.0)
                if type(elapsed_hint) not in (int, float) or not math.isfinite(elapsed_hint) or elapsed_hint < 0:
                    elapsed_hint = 0.0
                wall_elapsed = max(0.0, time.time() - self.started_epoch)
                elapsed = max(float(elapsed_hint), wall_elapsed)
                self.started = self.clock() - elapsed
                # Rebuild the live monotonic fence from the persisted wall
                # fence, including any explicit extensions.  Reapplying only
                # the original workflow duration would silently discard a
                # previously recorded extension on the next resume.
                wall_remaining = self.deadline_epoch - time.time()
                self.deadline = self.clock() + wall_remaining
        if self.status in {"completed", "candidate_needs_review", "research_expansion_required", "review_rejected",
                           "blocked", "paused"}:
            # A resumed workflow must explicitly continue from a non-terminal
            # checkpoint; completed output remains inspectable and immutable.
            self.status = "running"

    def _restore_context_from_stage_records(self):
        """Rehydrate completed stage packets when resuming an older checkpoint.

        Early Composer checkpoints persisted stage records but not the in-memory
        context.  A resume must still be able to bind accepted survey,
        experiment, interpretation, and argument outputs without replaying
        expensive upstream work or admitting a consumer against no packet.
        New checkpoints carry ``context`` directly; this reconstruction is a
        compatibility path for those already on disk.
        """
        by_id = {stage["id"]: stage for stage in self.workflow["stages"]}
        for stage_id, record in self.stage_records.items():
            if stage_id in self.context or not isinstance(record, dict):
                continue
            stage = by_id.get(stage_id)
            if stage is None:
                continue
            candidates = [record.get("output_path")]
            project_dir = record.get("project_dir") or stage.get("project_dir")
            if isinstance(project_dir, str):
                candidates.append(str(Path(project_dir) / "output" / "run.json"))
            output_path = next((Path(item) for item in candidates
                                if isinstance(item, str) and Path(item).is_file()), None)
            if output_path is None:
                continue
            try:
                payload = json.loads(output_path.read_text())
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            context = {**payload, "stage_id": stage_id, "kind": stage["kind"],
                       "project_dir": str(Path(stage.get("project_dir", project_dir)).resolve()),
                       "output_path": str(output_path.resolve())}
            if stage["kind"] == "experiment":
                context["results_package"] = payload.get("results_package")
            if stage["kind"] == "interpretation" and "interpretation" not in context:
                context["interpretation"] = payload
            if stage["kind"] == "argument":
                context["argument_package_path"] = str(output_path.resolve())
            self.context[stage_id] = context

    def _reconcile_interrupted_attempt(self, attempt_id):
        """Close an attempt left running before a process interruption.

        The stage record is only a summary.  The task ledger is authoritative
        about whether an external call is still started, already unknown, or
        has a settled outcome, so resumption reconciles that ledger before a
        fresh attempt is admitted.
        """
        try:
            attempt = self.tasks.get_attempt(attempt_id)
        except NotFoundError:
            # Older checkpoints may predate task-attempt persistence.
            return
        state = attempt.get("state")
        if state == "started":
            self.tasks.reconcile_unknown(attempt_id, "command.composer")
            return
        if state in {"result_unknown", "failed", "succeeded", "cancelled"}:
            return
        raise StateError(f"interrupted Composer attempt has unsupported state: {state}")

    def _source_value(self, expression):
        parts = expression.split(".")
        if len(parts) < 2 or parts[0] not in self.context:
            raise ValidationError(f"binding source must name a completed stage: {expression}")
        return _get_path(self.context[parts[0]], ".".join(parts[1:]))

    def _synchronize_paper_references(self, paper_config):
        """Project newly accepted survey sources into a continuation paper descriptor.

        A paper descriptor may have been prepared before the survey expanded.
        Keeping its original references is correct for an immutable first
        attempt, but it would make a continuation gate inspect stale coverage.
        This helper appends one reader-facing reference per newly captured
        source, preferring full text, while retaining every original key and
        evidence locator.  It never invents a DOI or authorship record.
        """
        try:
            from scisaurus.runtime.paper import load_paper_survey
            survey = load_paper_survey(paper_config)
        except Exception:
            # The normal paper validator will report the precise dependency
            # problem.  Do not hide it behind a best-effort synchronization.
            raise
        control = ControlStore(paper_config["survey_project_dir"])
        try:
            store = ArtifactStore(control)
            works = {}
            rows = control._conn.execute(
                "SELECT logical_id, MAX(version) AS version FROM artifacts "
                "WHERE logical_id LIKE 'kb/works/%' GROUP BY logical_id"
            ).fetchall()
            for row in rows:
                record = store.get(f"artifact:{row['logical_id']}@{row['version']}")
                body = json.loads(store.read_body(record["body_hash"]))
                works[body.get("work_id")] = body
            identities = {}
            for ref, identity in survey.get("identities", {}).items():
                if isinstance(identity, dict) and identity.get("work_id"):
                    identities[identity["work_id"]] = identity
        finally:
            control.close()

        # Catalog-backed missions are a new scientific identity.  Their
        # references must be projected from the current survey rather than
        # appended to a descriptor inherited from a previous topic.
        if self.workflow.get("experiment_catalog"):
            paper_config["references"] = []
        existing_refs = paper_config.get("references", [])
        existing_source_refs = {item.get("source_ref") for item in existing_refs if isinstance(item, dict)}
        existing_keys = {item.get("key") for item in existing_refs if isinstance(item, dict)}
        # Keep the configured order stable while prioritizing full text for new
        # entries.  A bounded cap prevents a broad search from turning the
        # writer prompt into an accidental bibliography dump.
        sources = sorted(
            survey.get("sources", {}).items(),
            key=lambda pair: (pair[1].get("representation") != "full_text", pair[1].get("work_id", "")),
        )
        profile_floor = 0
        depth = paper_config.get("depth_profile")
        if isinstance(depth, dict):
            profile_floor = int(depth.get("min_references", 0))
        target = max(profile_floor, len(existing_refs))
        target = min(max(target, profile_floor + 10), 50) if profile_floor else min(max(target, 20), 50)
        for source_ref, source in sources:
            if len(paper_config["references"]) >= target and source.get("representation") != "full_text":
                break
            if source_ref in existing_source_refs:
                continue
            work_id = source.get("work_id")
            work = works.get(work_id, {})
            key = re.sub(r"[^a-z0-9_-]+", "-", f"oa_{str(work_id).casefold()}").strip("-")[:64]
            if not key or not key[0].isalpha():
                key = f"ref_{len(existing_keys)}"
            if key in existing_keys:
                key = f"oa_{str(work_id).casefold()}_{len(existing_keys)}"
            identity = identities.get(work_id, {})
            # DOI fields are only emitted when an independently reconciled
            # identity exists.  A catalog record alone remains a URL citation.
            doi = identity.get("doi") if identity.get("status") == "verified" else None
            authors = work.get("authors") or source.get("authors") or "Authors not supplied by OpenAlex"
            if isinstance(authors, list):
                authors = ", ".join(
                    item if isinstance(item, str) else str(item) for item in authors)
            paper_config["references"].append({
                "key": key,
                "title": work.get("title") or source.get("title") or f"OpenAlex work {work_id}",
                "authors": str(authors),
                "year": str(work.get("year") or "unknown"),
                "doi": doi,
                "url": source.get("url") or f"https://openalex.org/{work_id}",
                "source_ref": source_ref,
                **({"identity_ref": next((ref for ref, item in survey.get("identities", {}).items()
                                          if isinstance(item, dict) and item.get("work_id") == work_id), None)}
                   if doi else {}),
            })
            existing_source_refs.add(source_ref)
            existing_keys.add(key)
            if len(paper_config["references"]) >= target:
                # Continue only when the full-text floor still needs entries.
                full_text_count = sum(
                    survey.get("sources", {}).get(item.get("source_ref"), {}).get("representation") == "full_text"
                    for item in paper_config["references"]
                )
                full_text_floor = int(depth.get("min_full_text_references", 0)) if isinstance(depth, dict) else 0
                if full_text_count >= full_text_floor:
                    break
        return paper_config

    def _interpretation_binding_path(self, value):
        """Return a direct interpretation artifact for a bound package path.

        Stage outputs are often persisted as envelopes (status, usage, and the
        actual scientific interpretation).  The release validator consumes
        the inner interpretation object, so a path binding must project that
        shape explicitly instead of handing the envelope to the next stage.
        The projection is content-addressed and immutable within this
        Composer run.
        """
        if not isinstance(value, str):
            return value
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            return value
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"bound interpretation artifact is unreadable: {path}") from exc
        if isinstance(payload, dict) and isinstance(payload.get("interpretation"), dict):
            interpretation = payload["interpretation"]
            from scisaurus.runtime.scientific_interpretation import validate_interpretation
            validate_interpretation(interpretation)
            digest = hashlib.sha256(canonical_bytes(interpretation)).hexdigest()
            target = self.root / "output" / "bindings" / f"scientific-interpretation-{digest}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() != canonical_bytes(interpretation):
                raise ValidationError("content-addressed interpretation binding was modified")
            if not target.exists():
                target.write_bytes(canonical_bytes(interpretation))
            return str(target.resolve())
        from scisaurus.runtime.scientific_interpretation import validate_interpretation
        validate_interpretation(payload)
        return value

    def _apply_bindings(self, payload, bindings):
        for binding in bindings:
            value = self._source_value(binding["source"])
            # A path reference to a JSON package is resolved to the package
            # body when it is bound into another stage's packet.
            if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                try:
                    value = json.loads(Path(value).read_text())
                except (OSError, ValueError) as exc:
                    raise ValidationError(f"bound JSON package is unreadable: {value}") from exc
            _set_path(payload, binding["target"], value)
        return payload

    def _stage_task(self, stage):
        prior_record = self.stage_records.get(stage["id"], {})
        continuation_task = stage["id"] in self.reopened_stage_ids and self.continuation_cycles
        task_id = None if continuation_task else (
            prior_record.get("task_id") if isinstance(prior_record, dict) else None)
        if not isinstance(task_id, str) or not task_id:
            task_id = f"composer-{self.workflow['id']}-{stage['id']}"
            if continuation_task:
                task_id += f"-continuation-{self.continuation_cycles}"
        try:
            existing = self.tasks.get(task_id)
        except NotFoundError:
            existing = None
        if (isinstance(prior_record, dict) and prior_record.get("status") == "running"
                and existing is not None and existing["state"] in {"completed", "failed", "cancelled", "stale"}):
            # The task may have reached a terminal lifecycle state just
            # before the Composer crashed while recording its stage result.
            # Preserve that history and dispatch a deterministic recovery
            # generation instead of attempting an illegal terminal-to-queued
            # transition.
            recovery = hashlib.sha256(
                str(prior_record.get("attempt_id", "unknown")).encode("utf-8")
            ).hexdigest()[:12]
            task_id = f"{task_id}-recovery-{recovery}"
            try:
                existing = self.tasks.get(task_id)
            except NotFoundError:
                existing = None
        if existing is not None and existing["state"] in {"blocked", "paused"}:
            self.tasks.transition(task_id, "queued", "command.composer", reason="scoped stage recovery")
        if existing is not None:
            return self.tasks.get(task_id)
        role = STAGE_ROLES[stage["kind"]]
        self.tasks.create(task_id, "production", {
            "stage_id": stage["id"], "kind": stage["kind"], "objective": self.workflow["objective"],
            "depends_on": stage["depends_on"], "config_path": stage["config_path"],
            "department": role.split(".", 1)[0], "role": role,
        }, "command.composer")
        return self.tasks.admit(task_id, "command.composer")

    def _run_stage(self, stage, *, attempt_number=1):
        """Dispatch one allowlisted specialist runner and return its context."""
        kind = stage["kind"]
        project_dir = Path(stage["project_dir"])
        output_path = None
        stage_deadline = min(float(stage["deadline_seconds"]), self._remaining())
        prior_run = (Path(stage["reuse_output_path"])
                     if stage["reuse_output_path"] is not None
                     else project_dir / "output" / "run.json")
        # A fresh free-topic mission must sample a new scholarly pool.  Topic
        # reuse is an explicit replay mode and is opt-in at workflow level;
        # other stages retain their existing checkpoint-reuse contract.
        topic_reuse_allowed = (kind != "topic_discovery"
                               or self.workflow.get("topic_reuse_allowed", False))
        if stage["reuse_completed"] and topic_reuse_allowed and prior_run.is_file():
            try:
                prior = json.loads(prior_run.read_text())
            except (OSError, ValueError) as exc:
                raise ValidationError(f"reusable stage output is unreadable: {prior_run}") from exc
            if prior.get("status") in {"completed", "accepted"}:
                output_value = (prior.get("output_path") or prior.get("results_package")
                                or prior.get("pdf") or str(prior_run.resolve()))
                context = {**prior, "stage_id": stage["id"], "kind": stage["kind"],
                           "project_dir": str(project_dir.resolve()), "output_path": str(Path(output_value).resolve())}
                if stage["kind"] == "experiment":
                    context["results_package"] = prior.get("results_package")
                if stage["kind"] == "argument":
                    context["argument_package_path"] = context["output_path"]
                self._publish(f"command/composer/reuse/{stage['id']}-{len(self.feedback) + 1}",
                              "decision_note", {
                                  "stage_id": stage["id"], "action": "reuse_completed_stage",
                                  "source_run": str(prior_run.resolve()), "status": prior.get("status"),
                              }, "command.composer")
                return context
        config = json.loads(Path(stage["config_path"]).read_text())
        config = self._adapt_continuation_config(stage, config)
        # A deadline-governed Composer mission carries its repair contract
        # into specialist model-validation loops as well.  This keeps one
        # malformed response local to its assignment instead of making the
        # whole stage fail at a smaller legacy ``max_rounds`` counter.
        if (kind in {"survey", "experiment"}
                and self._retry_policy().get("mode", "bounded") == "until_deadline"):
            config.setdefault("limits", {})["repair_mode"] = "until_deadline"
        if (kind == "topic_discovery"
                and self._retry_policy().get("mode", "bounded") == "until_deadline"):
            config["repair_mode"] = "until_deadline"
        if stage["id"] in self.reopened_stage_ids and self.continuation_cycles:
            # A continuation must never overwrite its incumbent.  Specialist
            # runners create new output roots, so remap their configured
            # result path into the cycle namespace before dispatch.
            cycle_root = Path(stage["project_dir"]).resolve()
            if kind in {"interpretation", "argument"}:
                configured_output = Path(config["output_path"])
                config["output_path"] = str(cycle_root / configured_output.name)
            elif kind == "paper":
                configured_output = Path(config["output_dir"])
                config["output_dir"] = str(cycle_root / configured_output.name)
        if attempt_number > 1:
            attempt_root = Path(stage["project_dir"]).resolve()
            if kind in {"interpretation", "argument"}:
                output_path = Path(config["output_path"])
                config["output_path"] = str(attempt_root / output_path.name)
            elif kind == "paper":
                output_dir = Path(config["output_dir"])
                config["output_dir"] = str(attempt_root / output_dir.name)
        if kind == "topic_discovery":
            from scisaurus.runtime.topic_discovery import (
                TopicDiscoveryRunner, validate_topic_stage_config,
            )
            # Another fresh Composer mission may have recorded a direction
            # after this process was initialized.  Refresh the family memory
            # at the admission boundary so parallel missions see the latest
            # completed selection before they ask the model for a topic.
            self.topic_history = self._load_topic_history()
            descriptor = validate_topic_stage_config(config)
            model = json.loads(Path(descriptor["model_config_path"]).read_text())
            runner = TopicDiscoveryRunner(model, deadline_seconds=stage_deadline)
            maturity_rounds = descriptor.get("maturity_review_rounds", 0)
            # Journal-oriented free-topic missions target a research paper,
            # not an executable demo.  Give the intake an independent maturity
            # screen by default; ordinary legacy topic stages keep their
            # original one-pass contract unless they opt in explicitly.
            if maturity_rounds == 0 and (
                    self.workflow.get("experiment_catalog")
                    or any(item.get("kind") == "paper" for item in self.workflow["stages"])):
                maturity_rounds = 2
            result = runner.run(
                self.workflow["objective"],
                candidate_count=descriptor["candidate_count"],
                max_attempts=descriptor["max_attempts"],
                repair_mode=descriptor.get("repair_mode", "bounded"),
                runtime_context=self._runtime_context(model),
                sampling_seed=self._topic_sampling_seed(),
                maturity_review_rounds=maturity_rounds,
                refinement_context=self._topic_refinement_context(stage),
            )
            output_path = Path(descriptor["output_path"])
            if attempt_number > 1 or stage["id"] in self.reopened_stage_ids:
                output_path = Path(stage["project_dir"]) / output_path.name
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(canonical_bytes(result))
        elif kind in {"survey", "experiment"}:
            if kind == "survey":
                config = self._apply_topic_to_survey_config(stage, config)
            elif kind == "experiment":
                config = self._apply_topic_to_experiment_config(stage, config)
            config = self._apply_bindings(config, stage["bindings"])
            if kind == "experiment":
                config = self._ensure_journal_quality_contract(stage, config)
            config.setdefault("limits", {})["wall_clock_seconds"] = min(
                float(config["limits"]["wall_clock_seconds"]), stage_deadline)
            if "time_policy" in config and isinstance(config["time_policy"], dict):
                config["time_policy"]["hard_seconds"] = min(
                    float(config["time_policy"].get("hard_seconds", stage_deadline)), stage_deadline)
                config["time_policy"]["target_seconds"] = min(
                    float(config["time_policy"].get("target_seconds", stage_deadline)),
                    config["time_policy"]["hard_seconds"])
                config["time_policy"]["first_result_seconds"] = min(
                    float(config["time_policy"].get("first_result_seconds", config["time_policy"]["target_seconds"])),
                    config["time_policy"]["target_seconds"])
            if kind == "survey":
                from scisaurus.runtime.survey import SurveyRunner
                project_dir = Path(stage["project_dir"])
                prior_run = project_dir / "output" / "run.json"
                resume_policy = None
                if prior_run.is_file():
                    try:
                        prior = json.loads(prior_run.read_text())
                    except (OSError, ValueError):
                        prior = {}
                    if prior.get("status") in {"blocked", "paused"}:
                        # A known literature review blocker is a recoverable
                        # stage outcome.  Reopen only the affected scope and
                        # charge a scoped recovery window; never restart the
                        # whole survey. Composer-level retries remain open
                        # until the immutable mission hard wall.
                        resume_policy = {
                            "additional_seconds": min(1200.0, float(config["limits"]["wall_clock_seconds"])),
                            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                            "source_changes": {"mode": "reopen", "reopen_scopes": ["focused_review"]},
                        }
                        self._publish(f"command/composer/recovery/{stage['id']}-{len(self.feedback) + 1}",
                                      "decision_note", {
                                          "stage_id": stage["id"], "action": "resume_scoped_stage",
                                          "scope": "focused_review", "reason": prior.get("error", "recoverable stage blocker"),
                                          "additional_seconds": resume_policy["additional_seconds"],
                                      }, "command.composer")
                result = SurveyRunner(stage["project_dir"], config, on_progress=self._stage_progress(stage),
                                      resume_policy=resume_policy).run()
            else:
                from scisaurus.runtime.experiment import ExperimentRunner
                result = ExperimentRunner(stage["project_dir"], config, on_progress=self._stage_progress(stage)).run()
            if kind == "survey":
                gated = self._gate_free_topic_survey(result, stage=stage)
                if gated is not result:
                    # Preserve the SurveyRunner's own output and write the
                    # Composer admission decision beside it so a process
                    # interruption can restore the hold and its work orders
                    # without relying on in-memory state.
                    gated_path = Path(stage["project_dir"]) / "output" / "composer-gated-run.json"
                    gated_path.parent.mkdir(parents=True, exist_ok=True)
                    gated_path.write_bytes(canonical_bytes(gated))
                    result = gated
                    output_path = gated_path
                else:
                    result = gated
            if output_path is None:
                output_path = Path(stage["project_dir"]) / "output" / "run.json"
        elif kind == "interpretation":
            from scisaurus.runtime.scientific_interpretation import ScientificInterpretationRunner
            descriptor_bindings = [item for item in stage["bindings"]
                                   if not item["target"].startswith("packet.")]
            config = self._apply_bindings(config, descriptor_bindings)
            model_path = Path(config.pop("model_config_path"))
            input_path = Path(config.pop("input_path"))
            output_path = Path(config.pop("output_path"))
            if config:
                raise ValidationError(f"interpretation descriptor has unknown fields: {sorted(config)}")
            packet = json.loads(input_path.read_text())
            packet = self._project_continuation_requests(packet, stage)
            for binding in stage["bindings"]:
                if binding["target"].startswith("packet."):
                    value = self._source_value(binding["source"])
                    if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                        value = json.loads(Path(value).read_text())
                    _set_path(packet, binding["target"][len("packet."):], value)
            self._synchronize_research_packet_identity(packet)
            result = ScientificInterpretationRunner(
                json.loads(model_path.read_text()), deadline_seconds=stage_deadline).run(packet)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(canonical_bytes(result))
            result = {"status": "completed", "output_path": str(output_path.resolve()),
                      "interpretation": result}
        elif kind == "argument":
            from scisaurus.runtime.research_argument import ResearchArgumentRunner, argument_evidence_packet
            descriptor_bindings = [item for item in stage["bindings"]
                                   if not item["target"].startswith("packet.")]
            config = self._apply_bindings(config, descriptor_bindings)
            model_path = Path(config.pop("model_config_path"))
            input_path = Path(config.pop("input_path"))
            output_path = Path(config.pop("output_path"))
            if config:
                raise ValidationError(f"argument descriptor has unknown fields: {sorted(config)}")
            packet = json.loads(input_path.read_text())
            for binding in stage["bindings"]:
                if binding["target"].startswith("packet."):
                    value = self._source_value(binding["source"])
                    if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                        value = json.loads(Path(value).read_text())
                    _set_path(packet, binding["target"][len("packet."):], value)
            self._synchronize_research_packet_identity(packet)
            model = json.loads(model_path.read_text())
            minimums = self._argument_minimums(stage["id"], config)
            result = ResearchArgumentRunner(model, deadline_seconds=stage_deadline).run(
                argument_evidence_packet(packet), min_figures=minimums["figures"],
                min_tables=minimums["tables"], min_experiments=minimums["experiments"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(canonical_bytes(result))
            result = {"status": "completed", "output_path": str(output_path.resolve()),
                      "argument_package": result, "argument": result.get("argument")}
        else:  # paper
            from scisaurus.runtime.paper_pipeline import PaperPipelineRunner
            # The paper descriptor names three independently versioned inputs.
            # Bindings addressed as ``packet.*`` or ``paper_config.*`` are
            # applied to those loaded objects; descriptor bindings (for
            # example ``argument_package_path``) are applied before loading.
            descriptor_bindings = [item for item in stage["bindings"]
                                   if not item["target"].startswith(("packet.", "paper_config."))]
            config = self._apply_bindings(config, descriptor_bindings)
            required = {"packet_path", "model_config_path", "paper_config_path", "output_dir", "argument_package_path",
                        "images", "max_review_rounds", "review_deadline_seconds", "pipeline_deadline_seconds",
                        "argument_deadline_seconds", "min_argument_figures", "min_argument_tables", "min_argument_experiments"}
            optional = {"draft_path", "initial_review_package_path", "release_on_review_limit",
                        "review_max_output_tokens", "review_reasoning_effort",
                        "review_call_timeout_seconds", "review_inter_request_interval_seconds",
                        "repair_max_output_tokens", "model_call_timeout_seconds",
                        "review_arbiter_enabled", "model_concurrency"}
            if set(config) - required - optional or not required.issubset(config):
                raise ValidationError(
                    f"paper descriptor requires {sorted(required)} and permits {sorted(optional)}")
            if "release_on_review_limit" in config and type(config["release_on_review_limit"]) is not bool:
                raise ValidationError("paper descriptor release_on_review_limit must be Boolean")
            if "review_arbiter_enabled" in config and type(config["review_arbiter_enabled"]) is not bool:
                raise ValidationError("paper descriptor review_arbiter_enabled must be Boolean")
            packet = json.loads(Path(config["packet_path"]).read_text())
            packet = self._project_continuation_requests(packet, stage)
            model = json.loads(Path(config["model_config_path"]).read_text())
            paper_config = json.loads(Path(config["paper_config_path"]).read_text())
            draft = (json.loads(Path(config["draft_path"]).read_text())
                     if config.get("draft_path") else None)
            initial_review_package = (json.loads(Path(config["initial_review_package_path"]).read_text())
                                     if config.get("initial_review_package_path") else None)
            for binding in stage["bindings"]:
                if binding["target"].startswith("packet."):
                    value = self._source_value(binding["source"])
                    if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                        value = json.loads(Path(value).read_text())
                    _set_path(packet, binding["target"][len("packet."):], value)
                elif binding["target"].startswith("paper_config."):
                    value = self._source_value(binding["source"])
                    if binding["target"] == "paper_config.interpretation_file":
                        value = self._interpretation_binding_path(value)
                    _set_path(paper_config, binding["target"][len("paper_config."):], value)
            self._synchronize_research_packet_identity(packet)
            # The paper descriptor is often prepared before discovery.  Once
            # the current survey has been accepted, project its actual source
            # set into the fresh paper input on the first pass as well as on a
            # continuation; otherwise the depth gate would inspect stale
            # references from an earlier mission.
            if "survey" in self.context:
                paper_config = self._synchronize_paper_references(paper_config)
            argument_package_path = config["argument_package_path"]
            argument_package = json.loads(Path(argument_package_path).read_text()) if argument_package_path else None
            paper_config = self._synchronize_paper_figure_arguments(
                paper_config, packet, argument_package)
            paper_config, packet = self._synchronize_catalog_paper_inputs(
                paper_config, packet, argument_package)
            config = self._extend_paper_images(config, packet, paper_config)
            runner = PaperPipelineRunner(packet=packet, model_config=model, paper_config=paper_config,
                output_dir=config["output_dir"], image_paths=config["images"],
                max_review_rounds=config["max_review_rounds"],
                review_deadline_seconds=min(config["review_deadline_seconds"], stage_deadline),
                pipeline_deadline_seconds=min(config["pipeline_deadline_seconds"], stage_deadline),
                argument_deadline_seconds=min(config["argument_deadline_seconds"], stage_deadline),
                review_max_output_tokens=config.get("review_max_output_tokens"),
                review_reasoning_effort=config.get("review_reasoning_effort", "xhigh"),
                review_call_timeout_seconds=min(config.get("review_call_timeout_seconds", 300.0), stage_deadline),
                review_inter_request_interval_seconds=config.get("review_inter_request_interval_seconds", 0.5),
                repair_max_output_tokens=config.get("repair_max_output_tokens"),
                model_call_timeout_seconds=min(config.get("model_call_timeout_seconds", 300.0), stage_deadline),
                model_concurrency=config.get("model_concurrency", 1),
                review_arbiter_enabled=bool(config.get("review_arbiter_enabled", True)),
                draft=draft, initial_review_package=initial_review_package,
                release_on_review_limit=bool(config.get("release_on_review_limit", False)),
                initial_argument_package=argument_package,
                min_argument_figures=config["min_argument_figures"], min_argument_tables=config["min_argument_tables"],
                min_argument_experiments=config["min_argument_experiments"],
                feedback_callback=lambda event: self._record_internal_feedback(stage, event))
            result = runner.run()
            output_path = Path(result["pdf"] or result.get("preflight_path")
                               or result.get("pipeline_result_path") or config["output_dir"])
        if not isinstance(result, dict):
            raise ValidationError(f"{kind} stage returned a non-object result")
        context = {**result, "stage_id": stage["id"], "kind": kind,
                   "project_dir": str(Path(stage["project_dir"]).resolve()),
                   "output_path": str(output_path.resolve())}
        if kind == "experiment":
            context["results_package"] = result.get("results_package")
        if kind == "argument":
            context["argument_package_path"] = context["output_path"]
        return context

    def _stage_progress(self, stage):
        def callback(state):
            self._checkpoint(f"{stage['id']}:{state.get('phase', 'progress')}")
        return callback

    def _record_feedback(self, stage, context):
        role = STAGE_ROLES[stage["kind"]]
        action = ("advance" if context.get("status") in {"completed", "accepted", "candidate_needs_review"}
                  else "reconcile_blocker")
        if context.get("status") == "research_expansion_required":
            action = "request_research_expansion"
        elif context.get("status") == "review_rejected":
            action = "editor_rejected"
        if action == "advance":
            consumers = [candidate for candidate in self.workflow["stages"]
                         if stage["id"] in candidate["depends_on"]]
            recipient = (self._address_for_role(STAGE_ROLES[consumers[0]["kind"]])
                         if consumers else COMMAND_ADDRESSES["intent"])
        else:
            recipient = COMMAND_ADDRESSES["arbiter"]
            if action == "request_research_expansion":
                requests = context.get("research_expansion_requests", [])
                owners = {item.get("owner") for item in requests if isinstance(item, dict)}
                if len(owners) == 1 and next(iter(owners)):
                    recipient = self._address_for_role(next(iter(owners)))
            elif action == "editor_rejected":
                recipient = self._address_for_role("editorial.composer")
        feedback = {
            "stage_id": stage["id"], "role": STAGE_ROLES[stage["kind"]],
            "from": COMMAND_ADDRESSES["progress"], "to": recipient,
            "status": context.get("status"), "output_path": context.get("output_path"),
            "scientific_state": context.get("gap_state") or context.get("review_status") or context.get("argument_status"),
            "research_expansion_requests": deepcopy(context.get("research_expansion_requests", [])),
            "action": action,
            "stage_deadline_seconds": stage["deadline_seconds"],
            "elapsed_seconds": context.get("elapsed_seconds"),
            "dependencies": list(stage["depends_on"]),
            "next_condition": "all declared dependencies current and stage output independently checked",
        }
        if context.get("status") == "research_expansion_required":
            feedback["next_condition"] = "complete every scoped literature, experiment, or analysis request and re-run the admission gate"
        elif context.get("status") == "review_rejected":
            feedback["next_condition"] = "reopen the rejected research or manuscript scope and obtain a new three-stage editorial decision"
        elif context.get("status") not in {"completed", "accepted", "candidate_needs_review"}:
            feedback["next_condition"] = "reconcile the stage blocker before downstream admission"
        elif context.get("status") == "candidate_needs_review":
            feedback["next_condition"] = "principal review is required before external submission"
        feedback["message_id"] = f"composer-feedback-{uuid.uuid4().hex}"
        feedback["message_disposition"] = "scheduled" if action == "advance" else "escalated"
        self.feedback.append(feedback)
        note = self._publish(f"command/composer/feedback/{stage['id']}-{len(self.feedback)}", "decision_note", feedback,
                             "command.composer")
        self._route_feedback(feedback, note)

    def _feedback_address(self, event):
        """Choose the receiving organ for an internal specialist exchange."""
        kind = event.get("kind")
        reviewer = event.get("reviewer_id")
        if kind in {"review_failure", "synthesis_failure", "arbitration_failure", "repair_failure"}:
            return COMMAND_ADDRESSES["arbiter"]
        if kind == "research_gate":
            owners = {item.get("owner") for item in event.get("expansion_requests", [])
                      if isinstance(item, dict)}
            if len(owners) == 1 and next(iter(owners)):
                return self._address_for_role(next(iter(owners)))
            return COMMAND_ADDRESSES["arbiter"]
        if kind == "editor_decision":
            return self._address_for_role("editorial.composer")
        if kind == "review":
            # A blocking critique is a dispute for the Arbiter; ordinary
            # findings remain with the department that owns the surface.
            if (event.get("severity_counts") or {}).get("blocking", 0):
                return COMMAND_ADDRESSES["arbiter"]
            if reviewer == "science":
                return self._address_for_role("strategy.interpretation")
            if reviewer == "methods":
                return self._address_for_role("methods.validation")
            if reviewer == "journal_editor":
                # Publication-depth findings are editorial admission
                # decisions.  They must reach the editor-in-chief rather than
                # being treated as an ordinary copy edit.
                return self._address_for_role("editorial.composer")
            return self._address_for_role("editorial.composer")
        if kind in {"argument", "draft", "repair"}:
            return self._address_for_role("editorial.composer")
        if kind == "release":
            return COMMAND_ADDRESSES["intent"]
        if kind == "synthesis":
            return (COMMAND_ADDRESSES["arbiter"]
                    if event.get("decision") == "insufficient_evidence"
                    else self._address_for_role("editorial.composer"))
        return COMMAND_ADDRESSES["arbiter"]

    def _record_internal_feedback(self, stage, event):
        """Route reviewer/editor events through the same Composer desk.

        Stage runners retain write authority over their own artifacts, while
        the Composer owns the organizational exchange. Event IDs are stable
        across checkpoint resume, so replaying a cached review cannot create
        duplicate messages or acknowledgements.
        """
        if not isinstance(event, dict) or not event.get("kind"):
            raise ValidationError("internal composer feedback requires a kind")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            stable = canonical_bytes({key: event[key] for key in sorted(event) if key != "artifact_path"})
            event_id = f"{stage['id']}:{hashlib.sha256(stable).hexdigest()[:24]}"
        # PaperPipelineRunner deliberately uses semantic event IDs so a
        # replay inside one pass is idempotent.  A reopened paper is a new
        # scientific pass, so namespace those IDs by Composer cycle while
        # preserving same-cycle deduplication and resume stability.
        # ArtifactRef logical names cannot contain a colon.  Normalize model
        # supplied IDs once at the control boundary and retain a short digest
        # when normalization changed their identity.
        safe_event_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", event_id).strip("-")
        if safe_event_id != event_id:
            safe_event_id = f"{safe_event_id[:72]}-{hashlib.sha256(event_id.encode('utf-8')).hexdigest()[:8]}"
        event_id = safe_event_id or f"{stage['id']}-{hashlib.sha256(canonical_bytes(event)).hexdigest()[:24]}"
        if (self.continuation_cycles and stage["id"] in self.reopened_stage_ids
                and not event_id.startswith(f"cycle-{self.continuation_cycles}-")):
            event_id = f"cycle-{self.continuation_cycles}-{event_id}"
        if any(item.get("event_id") == event_id for item in self.feedback):
            return
        status = event.get("status", "needs_revision")
        failure = event.get("kind", "").endswith("failure") or status == "blocked"
        recipient = self._feedback_address(event)
        action = ("reconcile_blocker" if failure else
                  "request_research_expansion" if status == "research_expansion_required" else
                  "editor_rejected" if status == "rejected" else
                  "request_revision" if status in {"needs_revision", "revise"} else
                  "verify_repair" if event.get("kind") == "repair" else
                  "propose_release" if event.get("kind") == "release" else
                  "record_observation")
        feedback = {
            "event_id": event_id,
            "stage_id": stage["id"],
            "role": STAGE_ROLES[stage["kind"]],
            "from": {"dept": "specialist-review", "agent": event["kind"]},
            "to": recipient,
            "status": status,
            "action": action,
            "event": deepcopy(event),
            "output_path": event.get("artifact_path") or event.get("pdf_path"),
            "stage_deadline_seconds": stage["deadline_seconds"],
            "dependencies": list(stage["depends_on"]),
            "next_condition": (
                "Arbiter validates the failure and Progress Controller reopens only the affected scope"
                if failure else
                "the receiving department records a scoped response before the next Composer admission"
            ),
            "message_id": "composer-feedback-" + hashlib.sha256(
                f"{self.workflow['id']}:{event_id}".encode("utf-8")).hexdigest()[:32],
            "message_disposition": "escalated" if failure else "scheduled",
        }
        if status == "research_expansion_required":
            feedback["next_condition"] = (
                "Complete every requested literature, experiment, or interpretation workstream, then re-admit the paper stage."
            )
        elif status == "rejected":
            feedback["next_condition"] = (
                "Reopen the rejected research scope and obtain a new three-stage reviewer and editor decision."
            )
        self.feedback.append(feedback)
        note = self._publish(
            f"command/composer/internal/{stage['id']}/{event_id}",
            "decision_note", feedback, "command.composer")
        self._route_feedback(feedback, note)
        self._checkpoint(f"{stage['id']}:feedback:{event['kind']}")

    def _route_feedback(self, feedback, note):
        """Route one durable command decision through the project message bus.

        The note is the immutable decision record; the envelope is the
        asynchronous handoff a department would receive in an ordinary
        organization.  Both carry the same body and reference, so a resumed
        Composer can reconstruct who acted, why, and what must happen next.
        """
        message_type = "decision" if feedback.get("action") == "advance" else "critique"
        envelope = {
            "message_id": feedback["message_id"],
            "project_id": self.workflow["project_id"],
            "type": message_type,
            "from": feedback.get("from", COMMAND_ADDRESSES["progress"]),
            "to": feedback.get("to", COMMAND_ADDRESSES["arbiter"]),
            "subject": f"{feedback.get('action', 'review')}:{feedback.get('stage_id', 'workflow')}",
            "body": json.dumps(feedback, ensure_ascii=False, sort_keys=True),
            "refs": [note["artifact_ref"]],
            "created_at": now_iso(),
            "idempotency_key": f"feedback:{self.workflow['id']}:{feedback.get('event_id') or feedback.get('message_id')}",
        }
        message_id = self.messages.publish(envelope)
        # Persist the receiving department's inbox and any typed work orders
        # before acknowledging the envelope.  This makes a handoff an actual
        # organizational operation rather than metadata attached only to the
        # Composer's own ledger.
        self.departments.receive_message(message_id, feedback, note["artifact_ref"])
        # The recipient's department chief owns receipt.  Acknowledging the
        # envelope here is the bounded command-desk handoff: the specialist
        # stage itself still has to satisfy its own acceptance contract before
        # the next dependency is admitted.  If receipt cannot be committed,
        # surface that control-plane failure instead of presenting a false
        # downstream decision.
        owner = f"{feedback['to']['dept']}.{feedback['to']['agent']}"
        # Receipt is a local control-plane operation and must remain durable
        # even when the stage has just crossed its execution deadline.  The
        # deadline controls new scientific work; it does not erase the audit
        # trail for a failed handoff.
        fence = self.messages.lease(message_id, owner=owner, ttl_seconds=30.0)
        self.messages.acknowledge(message_id, fence, feedback["message_disposition"], actor=owner)
        if message_id != feedback["message_id"]:
            raise ValidationError("message bus returned a different feedback message ID")

    def _record_blocker_feedback(self, stage, error):
        feedback = {
            "stage_id": stage["id"], "role": STAGE_ROLES[stage["kind"]],
            "from": COMMAND_ADDRESSES["progress"], "to": COMMAND_ADDRESSES["arbiter"],
            "status": "blocked", "action": "reconcile_blocker",
            "error": str(error), "stage_deadline_seconds": stage["deadline_seconds"],
            "dependencies": list(stage["depends_on"]),
            "next_condition": "Arbiter validates the blocker and Progress Controller reopens only its affected scope",
        }
        feedback["message_id"] = f"composer-feedback-{uuid.uuid4().hex}"
        feedback["message_disposition"] = "escalated"
        self.feedback.append(feedback)
        note = self._publish(f"command/composer/feedback/{stage['id']}-{len(self.feedback)}", "decision_note", feedback,
                             "command.composer")
        self._route_feedback(feedback, note)

    def run(self):
        try:
            by_id = {stage["id"]: stage for stage in self.workflow["stages"]}
            completed = {stage_id for stage_id, row in self.stage_records.items()
                         if row.get("status") in STAGE_READY_STATUSES}
            required_ids = set(self.workflow["completion"]["required_stage_ids"])
            if self.continuation_pending_stage_ids:
                completed.difference_update(self.continuation_pending_stage_ids)
            # A checkpoint may predate the immediate-hold admission rule.  On
            # resume, repair/review holds are reconciled before the scheduler
            # can admit any downstream consumer; this also prevents an
            # interrupted run from silently treating an old proposal as data.
            held = {stage_id for stage_id, row in self.stage_records.items()
                    if row.get("status") in STAGE_HOLD_STATUSES}
            if held and self._begin_continuation(completed, by_id):
                self._checkpoint("continuation:resume_admitted", force=True)
            elif held:
                hold_status = ("research_expansion_required"
                               if any(self.stage_records[item].get("status") == "research_expansion_required"
                                      for item in held)
                               else "review_rejected")
                self.status = hold_status
                self._checkpoint(f"resume:{hold_status}", force=True)
                return self._finish()
            while True:
                if required_ids.issubset(completed) and self.continuation_pending_stage_ids.issubset(completed):
                    # A completed graph may still contain a first-class
                    # research request or an editorial rejection.  Reopen only
                    # the affected closure, then let the normal dependency
                    # scheduler execute it under the same hard wall.
                    if self._begin_continuation(completed, by_id):
                        continue
                    break
                self._remaining()
                progress = False
                for stage in self.workflow["stages"]:
                    stage_id = stage["id"]
                    if stage_id in completed:
                        continue
                    if not set(stage["depends_on"]).issubset(completed):
                        continue
                    # The estimate is a planning reservation, not a promise.
                    # Bounded jobs reserve the complete downstream closure;
                    # an autonomous deadline-governed job may use a residual
                    # window for the next specialist and let that specialist's
                    # own contract decide whether a useful result fits.
                    remaining = self._remaining()
                    downstream_ids = set()
                    frontier = [stage_id]
                    while frontier:
                        current = frontier.pop()
                        if current in downstream_ids:
                            continue
                        downstream_ids.add(current)
                        frontier.extend(item for item, candidate in by_id.items()
                                        if current in candidate["depends_on"])
                    downstream = sum(float(by_id[item]["estimate_seconds"]) for item in downstream_ids)
                    required_window = min(float(stage["estimate_seconds"]), downstream)
                    retry_policy = self._retry_policy()
                    if remaining < required_window:
                        if (retry_policy.get("mode", "bounded") == "until_deadline"
                                and remaining > self._deadline_dispatch_floor()):
                            self.deadline_decisions.append({
                                "stage_id": stage_id,
                                "admission": "residual_window",
                                "remaining_seconds": remaining,
                                "estimated_stage_seconds": float(stage["estimate_seconds"]),
                                "estimated_downstream_seconds": downstream,
                                "safety_margin_seconds": self._deadline_dispatch_floor(),
                                "reason": "full_stage_estimate_does_not_fit; autonomous mode keeps the residual window",
                            })
                        else:
                            self.status = "paused"
                            self.blockers.append({
                                "stage_id": stage_id,
                                "reason": "required_stage_window_does_not_fit_remaining_deadline",
                                "remaining_seconds": remaining,
                                "required_stage_seconds": required_window,
                                "mode": retry_policy.get("mode", "bounded"),
                            })
                            self.deadline_decisions.append({
                                "stage_id": stage_id,
                                "admission": "deferred",
                                "remaining_seconds": remaining,
                                "required_stage_seconds": required_window,
                                "reason": "required_stage_window_does_not_fit_remaining_deadline",
                            })
                            self._checkpoint("paused_deadline", force=True)
                            return self._finish()
                    # Continuation stages receive a cycle-specific project
                    # namespace.  The original stage descriptor remains the
                    # immutable graph identity; only the dispatch copy changes.
                    stage = self._stage_for_cycle(stage)
                    prior_record = self.stage_records.get(stage_id, {})
                    attempt_history = deepcopy(prior_record.get("attempts", []))
                    if not isinstance(attempt_history, list):
                        attempt_history = []
                    if prior_record.get("status") == "running" and prior_record.get("attempt_id"):
                        # A process interruption can leave a provider call
                        # without an observable outcome.  Preserve that
                        # uncertainty and start the next attempt in a fresh
                        # directory instead of overwriting its workspace.
                        self._reconcile_interrupted_attempt(prior_record["attempt_id"])
                        if not any(item.get("attempt_id") == prior_record["attempt_id"]
                                   and item.get("state") == "unknown"
                                   for item in attempt_history if isinstance(item, dict)):
                            attempt_history.append({
                                "attempt_number": prior_record.get("attempt_number", len(attempt_history) + 1),
                                "attempt_id": prior_record["attempt_id"],
                                "state": "unknown",
                                "project_dir": prior_record.get("project_dir", stage["project_dir"]),
                                "error": "Composer resumed after an incomplete stage attempt; outcome was not observed.",
                            })
                    last_error = None
                    stage_succeeded = False
                    context = None
                    retry_indices = (
                        itertools.count()
                        if retry_policy.get("mode", "bounded") == "until_deadline"
                        else range(retry_policy["max_attempts"])
                    )
                    for retry_index in retry_indices:
                        attempt_number = len(attempt_history) + 1
                        if retry_index:
                            try:
                                if not self._wait_before_retry(
                                        stage, attempt_number=attempt_number,
                                        retry_index=retry_index, error=last_error,
                                        downstream_seconds=downstream):
                                    last_error = ValidationError(
                                        "retry budget no longer fits the Composer hard deadline")
                                    break
                            except Exception as exc:
                                last_error = exc
                                break
                        attempt_stage = self._attempt_stage(stage, attempt_number)
                        task = self._stage_task(stage)
                        task_id = task["task_id"]
                        attempt_id = f"{task_id}-{uuid.uuid4().hex}"
                        attempt_started = self.clock()
                        self.tasks.start_attempt(
                            task_id, attempt_id, owner="command.composer",
                            lease_ttl_seconds=max(1.0, self._remaining()),
                            payload={"stage_id": stage_id, "kind": stage["kind"],
                                     "attempt_number": attempt_number,
                                     "project_dir": attempt_stage["project_dir"]})
                        self.stage_records[stage_id] = {
                            "kind": stage["kind"], "status": "running",
                            "task_id": task_id, "attempt_id": attempt_id, "attempt_number": attempt_number,
                            "attempt_count": attempt_number,
                            "started_elapsed": attempt_started - self.started,
                            "project_dir": attempt_stage["project_dir"],
                            "attempts": deepcopy(attempt_history),
                        }
                        self._checkpoint(f"{stage_id}:admitted", force=True)
                        stop_live_progress = self._start_live_progress(attempt_stage)
                        try:
                            context = (self._run_stage(attempt_stage)
                                       if attempt_number == 1
                                       else self._run_stage(attempt_stage, attempt_number=attempt_number))
                            outcome = context.get("status")
                            if outcome not in {"completed", "accepted", "candidate_needs_review",
                                               "research_expansion_required", "review_rejected"}:
                                raise ValidationError(f"stage {stage_id} did not complete: {outcome}")
                            if stage["kind"] == "topic_discovery":
                                # Record the direction at admission time, even
                                # when a later survey or experiment hold stops
                                # the mission.  A failed attempt is still an
                                # attempted direction and must not be selected
                                # again by the next free-topic mission.
                                self._record_topic_history(context)
                            for key in self.usage:
                                value = context.get("usage", {}).get(key, 0)
                                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                                    self.usage[key] += value
                            self.tasks.finish_attempt(attempt_id, "succeeded", usage=context.get("usage", {}))
                            self.tasks.transition(task_id, "awaiting_review", "command.composer", reason="stage output returned")
                            if outcome not in STAGE_HOLD_STATUSES:
                                self.tasks.transition(task_id, "completed", "command.composer", reason="stage-specific checks passed")
                            context["stage_id"] = stage_id
                            context["attempt_number"] = attempt_number
                            self.context[stage_id] = context
                            self._mark_research_requests_attempted(stage)
                            if self.active_research_requests:
                                self.department_activity.append({
                                    "cycle": self.continuation_cycles,
                                    "action": "resolve_work_orders",
                                    "stage_id": stage_id,
                                    "outcome": outcome,
                                    "work_orders": self.departments.resolve_work_orders(
                                        self.active_research_requests,
                                        stage_kind=stage["kind"], outcome=outcome),
                                })
                            self.continuation_pending_stage_ids.discard(stage_id)
                            attempt_history.append({
                                "attempt_number": attempt_number,
                                "attempt_id": attempt_id,
                                "state": "succeeded",
                                "project_dir": attempt_stage["project_dir"],
                                "elapsed_seconds": self.clock() - attempt_started,
                            })
                            self.stage_records[stage_id] = {
                                "kind": stage["kind"],
                                "status": (outcome if outcome in {
                                    "candidate_needs_review", "research_expansion_required", "review_rejected"}
                                           else "completed"),
                                "task_id": task_id, "attempt_id": attempt_id,
                                "attempt_number": attempt_number,
                                "attempt_count": attempt_number,
                                "attempts": deepcopy(attempt_history),
                                "output_path": context.get("output_path"),
                                "elapsed_seconds": self.clock() - self.started,
                            }
                            stage_succeeded = True
                            break
                        except Exception as exc:
                            last_error = exc
                            try:
                                self.tasks.finish_attempt(attempt_id, "failed", usage={})
                                self.tasks.transition(task_id, "blocked", "command.composer", reason=str(exc))
                            except Exception:
                                pass
                            attempt_history.append({
                                "attempt_number": attempt_number,
                                "attempt_id": attempt_id,
                                "state": "failed",
                                "project_dir": attempt_stage["project_dir"],
                                "error": f"{type(exc).__name__}: {exc}",
                                "elapsed_seconds": self.clock() - attempt_started,
                            })
                            retry_open = (
                                retry_policy.get("mode", "bounded") == "until_deadline"
                                or retry_index + 1 < retry_policy.get("max_attempts", 0)
                            )
                            self.stage_records[stage_id] = {
                                "kind": stage["kind"],
                                "status": "retrying" if retry_open else "blocked",
                                "task_id": task_id, "attempt_id": attempt_id, "attempt_number": attempt_number,
                                "attempt_count": attempt_number, "attempts": deepcopy(attempt_history),
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                            self._checkpoint(f"{stage_id}:retrying" if retry_open else f"{stage_id}:failed", force=True)
                        finally:
                            stop_live_progress()
                    if stage_succeeded:
                        completed.add(stage_id)
                        self._record_feedback(stage, context)
                        if context.get("status") in STAGE_HOLD_STATUSES:
                            # A hold is a control decision, not a successful
                            # dependency.  Start the owning continuation now;
                            # otherwise return the hold without admitting a
                            # consumer that would write against insufficient
                            # or rejected evidence.
                            if self._begin_continuation(completed, by_id):
                                self._checkpoint(f"{stage_id}:continuation_admitted", force=True)
                                progress = True
                                break
                            self.status = context["status"]
                            self._checkpoint(f"{stage_id}:{context['status']}", force=True)
                            return self._finish()
                        self._checkpoint(f"{stage_id}:completed", force=True)
                        progress = True
                        break
                    error = last_error or ValidationError(
                        f"stage {stage_id} exhausted its retry policy")
                    self.stage_records[stage_id] = {
                        "kind": stage["kind"], "status": "blocked",
                        "task_id": task_id,
                        "attempt_count": len(attempt_history), "attempts": deepcopy(attempt_history),
                        "error": f"{type(error).__name__}: {error}",
                    }
                    self.blockers.append({"stage_id": stage_id, "reason": str(error),
                                          "attempts": len(attempt_history)})
                    self._record_blocker_feedback(stage, error)
                    self.status = "blocked"
                    self._checkpoint(f"{stage_id}:blocked", force=True)
                    return self._finish()
                if not progress:
                    self.status = "blocked"
                    self.blockers.append({"reason": "composer made no dependency-respecting progress"})
                    self._checkpoint("blocked_no_progress", force=True)
                    return self._finish()
            # A stage can finish its bounded execution while its artifact is
            # still a candidate.  Preserve that distinction at the workflow
            # level so the CLI and any scheduler cannot mistake a candidate
            # for a completed release.
            research_stage = any(
                row.get("status") == "research_expansion_required"
                or self.context.get(stage_id, {}).get("status") == "research_expansion_required"
                for stage_id, row in self.stage_records.items())
            rejected_stage = any(
                row.get("status") == "review_rejected"
                or self.context.get(stage_id, {}).get("status") == "review_rejected"
                for stage_id, row in self.stage_records.items())
            candidate_stage = any(
                row.get("status") == "candidate_needs_review"
                or self.context.get(stage_id, {}).get("status") == "candidate_needs_review"
                for stage_id, row in self.stage_records.items())
            self.status = ("research_expansion_required" if research_stage
                           else "review_rejected" if rejected_stage
                           else "candidate_needs_review" if candidate_stage else "completed")
            self._checkpoint("complete_proposed", force=True)
            return self._finish()
        except Exception as exc:
            self.status = "blocked"
            self.blockers.append({"reason": f"{type(exc).__name__}: {exc}"})
            self._checkpoint("blocked", force=True)
            return self._finish()
        finally:
            self.close()

    def _finish(self):
        deadline_exhausted = self._deadline_exhausted()
        # A provider may return just after the fence even when admission was
        # valid.  Such an artifact remains inspectable, but the mission must
        # not report graph completion after its hard wall has passed.
        if deadline_exhausted and self.status not in {"blocked", "paused"}:
            self.status = "blocked"
            self.blockers.append({
                "stage_id": "workflow",
                "reason": "hard_deadline_after_stage_completion",
            })
        elapsed = max(0.0, self.clock() - self.started)
        if self.control is not None:
            # Final publication is on the Composer thread, so it is safe to
            # refresh the durable organization projection here.
            self.organization_snapshot = deepcopy(self.departments.snapshot())
        interim = None
        interim_error = None
        if deadline_exhausted or self.status in {"blocked", "paused"}:
            try:
                stop_reason = "hard_deadline" if deadline_exhausted else None
                if stop_reason is None and self.status == "paused" and any(
                        isinstance(item, dict)
                        and item.get("reason") == "required_stage_window_does_not_fit_remaining_deadline"
                        for item in self.blockers):
                    stop_reason = "required_stage_window_does_not_fit_remaining_deadline"
                interim = self.interim_report(
                    stop_reason=stop_reason or self.status)
            except Exception as exc:
                # The run report remains publishable even if a secondary
                # progress projection encounters an I/O or ledger failure.
                interim_error = f"{type(exc).__name__}: {exc}"
        result = {
            "schema_version": RUN_SCHEMA_VERSION, "run_id": self.run_id, "workflow_id": self.workflow["id"],
            "status": self.status, "stages": deepcopy(self.stage_records), "context": deepcopy(self.context),
            "feedback": deepcopy(self.feedback), "blockers": deepcopy(self.blockers), "usage": deepcopy(self.usage),
            "organization": deepcopy(self.organization_snapshot),
            "retry_policy": self._retry_policy(),
            "continuation_policy": self._continuation_policy(),
            "continuation_cycles": self.continuation_cycles,
            "reopened_stage_ids": sorted(self.reopened_stage_ids),
            "continuation_pending_stage_ids": sorted(self.continuation_pending_stage_ids),
            "active_research_requests": deepcopy(self.active_research_requests),
            "department_activity": deepcopy(self.department_activity),
            "deadline_decisions": deepcopy(self.deadline_decisions),
            "deadline_extensions": deepcopy(self.deadline_extensions),
            "topic_history_path": str(self.topic_history_path),
            "topic_history_scope": self.topic_history_scope,
            "topic_history_entries": len(self.topic_history.get("entries", [])),
            "started_at_epoch": self.started_epoch, "deadline_at_epoch": self.deadline_epoch,
            "elapsed_seconds": elapsed, "deadline_seconds": self.workflow["time_policy"]["hard_seconds"],
            "release_status": (
                "research_expansion_required" if self.status == "research_expansion_required"
                else "review_rejected" if self.status == "review_rejected"
                else "candidate_needs_review" if self.status == "candidate_needs_review"
                else "needs_human_approval" if self.status == "completed"
                and self.workflow["completion"]["release_requires_human"]
                else "not_released"
            ),
            "event_chain": self.control.verify_chain() if self.control is not None else None,
            "interim_report_path": interim.get("path") if isinstance(interim, dict) else None,
        }
        if isinstance(interim, dict):
            result["interim_report"] = deepcopy(interim)
        if interim_error:
            result["interim_report_error"] = interim_error
        if self.control is not None:
            self._publish("command/composer/run", "report", result, "command.composer")
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "run.json").write_bytes(canonical_bytes(result))
            self.on_progress({"phase": self.status, "elapsed_seconds": round(elapsed, 2),
                              "stages": deepcopy(self.stage_records), "blockers": deepcopy(self.blockers),
                              "organization": deepcopy(result["organization"])})
        return result


def read_interim_report(project_dir):
    """Load the latest concise Composer interim report from a project."""
    path = Path(project_dir).resolve() / "output" / "interim_report.json"
    if path.is_file():
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"Composer interim report is not valid JSON: {path}") from exc
        if not isinstance(report, dict) or report.get("schema_version") != "composer-interim-report-1":
            raise ValidationError(f"Composer interim report has an unsupported schema: {path}")
        return report

    # A hard process interruption can occur before ``_finish`` gets to write
    # the projection.  The live checkpoint is still an inspectable, atomic
    # source for a short recovery report in that case.
    checkpoint_path = path.parent / "progress.json"
    if not checkpoint_path.is_file():
        raise ValidationError(f"Composer interim report does not exist: {path}")
    try:
        checkpoint = json.loads(checkpoint_path.read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError(f"Composer progress checkpoint is not valid JSON: {checkpoint_path}") from exc
    if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != "composer-checkpoint-1":
        raise ValidationError(f"Composer progress checkpoint has an unsupported schema: {checkpoint_path}")
    stages = checkpoint.get("stages", {})
    if not isinstance(stages, dict):
        stages = {}
    workflow = None
    control = None
    workflow_error = None
    try:
        control = ControlStore(project_dir)
        artifacts = ArtifactStore(control)
        workflow_head = artifacts.head("command/composer/workflow")
        if workflow_head is not None:
            workflow = json.loads(artifacts.read_body(workflow_head["body_hash"]))
    except Exception as exc:
        workflow = None
        workflow_error = f"{type(exc).__name__}: {exc}"
    finally:
        if control is not None:
            control.close()
    workflow_stages = workflow.get("stages", []) if isinstance(workflow, dict) else []
    ordered_ids = [stage["id"] for stage in workflow_stages
                   if isinstance(stage, dict) and isinstance(stage.get("id"), str)]
    stage_kind_by_id = {stage["id"]: stage.get("kind") for stage in workflow_stages
                        if isinstance(stage, dict) and isinstance(stage.get("id"), str)}
    if not ordered_ids:
        ordered_ids = sorted(stages)
    rows = []
    completed, active, pending, held, blocked = [], [], [], [], []
    for stage_id in ordered_ids:
        record = stages.get(stage_id, {}) if isinstance(stages.get(stage_id, {}), dict) else {}
        status = record.get("status", "pending")
        attempts = record.get("attempt_count", len(record.get("attempts", []))
                              if isinstance(record.get("attempts", []), list) else 0)
        row = {"id": stage_id, "kind": record.get("kind") or stage_kind_by_id.get(stage_id), "status": status,
               "attempts": attempts}
        if isinstance(record.get("output_path"), str):
            row["output_path"] = record["output_path"]
        if isinstance(record.get("error"), str):
            row["error"] = record["error"][:240]
        rows.append(row)
        if status in STAGE_READY_STATUSES:
            completed.append(stage_id)
        elif status in {"running", "retrying"}:
            active.append(stage_id)
        elif status in STAGE_HOLD_STATUSES:
            held.append(stage_id)
        elif status == "blocked":
            blocked.append(stage_id)
        else:
            pending.append(stage_id)
    started_at_epoch = checkpoint.get("started_at_epoch")
    deadline_at_epoch = checkpoint.get("deadline_at_epoch")
    duration = None
    if (type(started_at_epoch) in (int, float) and type(deadline_at_epoch) in (int, float)
            and math.isfinite(started_at_epoch) and math.isfinite(deadline_at_epoch)
            and deadline_at_epoch >= started_at_epoch):
        duration = deadline_at_epoch - started_at_epoch
    report = {
        "schema_version": "composer-interim-report-1",
        "workflow_id": checkpoint.get("workflow_id"),
        "run_id": checkpoint.get("run_id"),
        "status": "interrupted",
        "stop_reason": "process_interrupted",
        "last_phase": checkpoint.get("phase"),
        "elapsed_seconds": checkpoint.get("elapsed_seconds", 0.0),
        "remaining_seconds": checkpoint.get("remaining_seconds", 0.0),
        "deadline_seconds": workflow.get("time_policy", {}).get("hard_seconds", duration)
        if isinstance(workflow, dict) else duration,
        "started_at_epoch": started_at_epoch,
        "deadline_at_epoch": deadline_at_epoch,
        "completed_stage_ids": completed,
        "active_stage_ids": active,
        "pending_stage_ids": pending,
        "held_stage_ids": held,
        "blocked_stage_ids": blocked,
        "stages": rows,
        "pending_work_orders": (checkpoint.get("organization", {}) or {}).get("open_work_orders", [])[:8]
        if isinstance(checkpoint.get("organization"), dict) else [],
        "blockers": checkpoint.get("blockers", [])[-3:]
        if isinstance(checkpoint.get("blockers"), list) else [],
        "deadline_extensions": checkpoint.get("deadline_extensions", []),
        "next_actions": ["Inspect retained stage outputs and resume the same workflow."],
        "resume_hint": (
            "python -m scisaurus.cli run-composer --workflow "
            "<path-to-the-same-workflow.json> --resume --extend-deadline-seconds <N>"
        ),
        "source": str(checkpoint_path.resolve()),
    }
    if workflow_error:
        report["workflow_metadata_error"] = workflow_error
    return report


__all__ = ["SCHEMA_VERSION", "RUN_SCHEMA_VERSION", "validate_workflow", "ComposerRunner",
           "read_interim_report"]
