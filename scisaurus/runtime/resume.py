"""Durable restart inspection, unknown-call reconciliation, and deadline replanning."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from scisaurus.core.budget import BudgetManager
from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager


RESUME_SCOPES = frozenset({"operations", "retrieval", "mapping", "focused_review",
                           "integrated_review", "gap_assessment", "production", "rendering"})


def source_manifest(root, paths=None):
    root = Path(root).resolve()
    files = ([root / item for item in paths] if paths is not None
             else sorted((root / "scisaurus").rglob("*.py")))
    result = {}
    for path in files:
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def validate_resume_policy(value, *, wall_clock_seconds):
    fields = {"additional_seconds", "unknown_outcomes", "source_changes"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"resume policy requires exactly {sorted(fields)}")
    seconds = value["additional_seconds"]
    if (type(seconds) not in (int, float) or not math.isfinite(seconds)
            or not 0 < seconds <= wall_clock_seconds):
        raise ValidationError("resume additional_seconds must fit the configured wall-clock limit")
    unknown = value["unknown_outcomes"]
    if (not isinstance(unknown, dict) or set(unknown) != {"mode", "usage_per_attempt"}
            or unknown["mode"] not in {"block", "charge_and_retry"}
            or not isinstance(unknown["usage_per_attempt"], dict)):
        raise ValidationError("resume unknown_outcomes requires a mode and usage_per_attempt")
    if unknown["mode"] == "charge_and_retry" and not unknown["usage_per_attempt"]:
        raise ValidationError("retrying an unknown outcome requires conservative nonempty usage")
    for key, amount in unknown["usage_per_attempt"].items():
        if (not isinstance(key, str) or not key or type(amount) not in (int, float)
                or not math.isfinite(amount) or amount < 0):
            raise ValidationError("resume unknown usage must contain nonnegative named quantities")
    source = value["source_changes"]
    if (not isinstance(source, dict) or set(source) != {"mode", "reopen_scopes"}
            or source["mode"] not in {"reject", "reopen"}
            or not isinstance(source["reopen_scopes"], list)
            or len(source["reopen_scopes"]) != len(set(source["reopen_scopes"]))
            or set(source["reopen_scopes"]) - RESUME_SCOPES):
        raise ValidationError("resume source_changes requires reject/reopen and known unique scopes")
    if source["mode"] == "reopen" and not source["reopen_scopes"]:
        raise ValidationError("adopting changed source requires explicit reopened scopes")
    if source["mode"] == "reject" and source["reopen_scopes"]:
        raise ValidationError("reject mode cannot claim reopened source scopes")
    canonical_bytes(value)
    return value


def deadline_replan(plan, completed_task_ids, *, available_seconds, worker_slots):
    """Return a deterministic retained-work decision under a new elapsed limit."""
    if (type(available_seconds) not in (int, float)
            or not math.isfinite(available_seconds) or available_seconds < 0):
        raise ValidationError("available_seconds must be finite and nonnegative")
    if type(worker_slots) is not int or worker_slots < 1:
        raise ValidationError("worker_slots must be a positive integer")
    tasks = {task["id"]: task for task in plan["tasks"]}
    completed = set(completed_task_ids)
    if completed - set(tasks):
        raise ValidationError("deadline replanning received an unknown completed task")
    mandatory = set(plan["completion"]["required_task_ids"])
    pending = list(mandatory - completed)
    while pending:
        task_id = pending.pop()
        for dependency in tasks[task_id]["depends_on"]:
            if dependency not in mandatory:
                mandatory.add(dependency)
                pending.append(dependency)
    required_remaining = mandatory - completed
    optional_remaining = set(tasks) - mandatory - completed

    def duration(task_ids):
        # Level scheduling is a safe deterministic approximation: dependencies
        # in a level are complete before the next level, while peers share slots.
        remaining, done, total = set(task_ids), set(completed), 0.0
        while remaining:
            ready = [tasks[item] for item in sorted(remaining)
                     if set(tasks[item]["depends_on"]).issubset(done)]
            if not ready:
                raise ValidationError("deadline replan cannot schedule the plan graph")
            for offset in range(0, len(ready), worker_slots):
                total += max(task["estimate_seconds"] for task in ready[offset:offset + worker_slots])
            identifiers = {task["id"] for task in ready}
            done.update(identifiers)
            remaining -= identifiers
        return total

    required_seconds = duration(required_remaining)
    if required_seconds > available_seconds:
        action = "retain_and_pause"
        selected = []
        reason = "required closure cannot fit the remaining deadline"
    else:
        selected_set = set(required_remaining)
        for task_id in sorted(optional_remaining):
            closure, pending = {task_id}, [task_id]
            while pending:
                current = pending.pop()
                for dependency in tasks[current]["depends_on"]:
                    if dependency not in completed and dependency not in closure:
                        closure.add(dependency); pending.append(dependency)
            candidate = selected_set | closure
            if duration(candidate) <= available_seconds:
                selected_set = candidate
        selected = sorted(selected_set)
        action = "continue" if selected_set == set(tasks) - completed else "continue_required_scope"
        reason = "required closure fits with reserved verification work"
    return {"action": action, "reason": reason, "available_seconds": float(available_seconds),
            "required_estimate_seconds": required_seconds, "selected_task_ids": selected,
            "deferred_optional_task_ids": sorted(optional_remaining - set(selected)),
            "completed_task_ids": sorted(completed)}


class ResumeController:
    """Reopen an existing run only after explicit evidence and accounting checks."""

    def __init__(self, control, store: ArtifactStore, *, repository_root):
        self.control, self.store = control, store
        self.root = Path(repository_root).resolve()
        self.tasks, self.budget = TaskManager(control), BudgetManager(control)

    def _stored_config(self):
        head = self.store.head("inputs/run-config")
        if head is None:
            raise ValidationError("existing run has no durable input configuration")
        return head, json.loads(self.store.read_body(head["body_hash"]))

    def _prior_manifest(self):
        head = self.store.head("inputs/source-manifest")
        if head is not None:
            return json.loads(self.store.read_body(head["body_hash"]))
        path = Path(self.control.dir) / "source-manifest.json"
        if path.is_file():
            return json.loads(path.read_text())
        raise ValidationError("existing run has no source manifest for safe resume")

    def prepare(self, config, policy, *, author="command.recovery"):
        validate_resume_policy(policy, wall_clock_seconds=config["limits"]["wall_clock_seconds"])
        config_ref, stored = self._stored_config()
        if canonical_bytes(stored) != canonical_bytes(config):
            raise ValidationError("resume configuration must exactly match the original run configuration")
        prior = self._prior_manifest()
        current = source_manifest(self.root)
        current.update(source_manifest(self.root, [path for path in prior if path not in current]))
        changed = sorted(path for path in set(prior) | set(current) if current.get(path) != prior.get(path))
        source_policy = policy["source_changes"]
        if changed and source_policy["mode"] != "reopen":
            raise ValidationError("executed source changed; resume requires explicit affected scopes")
        unknown_rows = self.control._conn.execute(
            "SELECT a.attempt_id, a.task_id, r.reservation_id, r.window_id"
            " FROM attempts a LEFT JOIN reservations r ON r.task_id=a.task_id AND r.state='reserved'"
            " WHERE a.state='result_unknown' ORDER BY a.attempt_id"
        ).fetchall()
        if unknown_rows and policy["unknown_outcomes"]["mode"] != "charge_and_retry":
            raise ValidationError("unknown external outcomes require explicit conservative reconciliation")
        reconciled = []
        for row in unknown_rows:
            usage = dict(policy["unknown_outcomes"]["usage_per_attempt"])
            self.tasks.finish_attempt(row["attempt_id"], "failed", usage=usage)
            if row["reservation_id"] is not None:
                self.budget.settle(window_id=row["window_id"], reservation_id=row["reservation_id"], actual=usage)
            reconciled.append({"attempt_id": row["attempt_id"], "task_id": row["task_id"],
                               "reservation_id": row["reservation_id"], "charged_usage": usage,
                               "disposition": "prior response discarded; a retry may duplicate provider cost"})
        serial = self.control._conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE logical_id LIKE 'command/resume-sessions/%'"
        ).fetchone()[0] + 1
        # An explicit reopen request is meaningful even when no source file
        # changed.  This is the recovery path for a known, retryable provider
        # failure (for example a 429) whose failed request was never promoted
        # into the retained search log.  Source drift still requires the same
        # explicit scope; this simply avoids conflating retry authority with
        # file-change detection.
        reopened_scopes = source_policy["reopen_scopes"] if source_policy["mode"] == "reopen" else []
        body = {"schema_version": "resume-session-1", "session": serial,
                "config_ref": config_ref["artifact_ref"], "source_changed_paths": changed,
                "reopened_scopes": reopened_scopes,
                "unknown_reconciliations": reconciled,
                "additional_seconds": float(policy["additional_seconds"]),
                "event_chain_before_resume": self.control.verify_chain()}
        record = self.store.publish_artifact(logical_id=f"command/resume-sessions/{serial}", artifact_type="report",
            author=author, body=canonical_bytes(body), media_type="application/json",
            inputs=[{"ref": config_ref["artifact_ref"], "purpose": "subject"}])
        with self.control.tx() as conn:
            self.control.append_event(conn, actor=author, event_type="run.resumed", payload={
                "resume_ref": record["artifact_ref"], "reconciled_unknown_calls": len(reconciled),
                "reopened_scopes": body["reopened_scopes"]})
        return {**body, "artifact_ref": record["artifact_ref"]}
