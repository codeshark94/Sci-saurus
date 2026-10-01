"""Task and attempt lifecycle with leases, fencing, and unknown-outcome
reconciliation.

Implements docs/40-execution-contract.md §4.2/§4.3: lifecycle
``proposed → queued → running → awaiting_review → completed`` with typed
blocked/paused/failed/stale routes, cancellation from any nonterminal state,
and attempts ending as ``succeeded | failed | cancelled | result_unknown``.
An external call of unknown completion is never a zero-cost success (T05).
"""

from __future__ import annotations

import json
import math
import time

from scisaurus.core.errors import NotFoundError, StateError, ValidationError
from scisaurus.core.schema import TASK_KINDS, canonical_bytes, now_iso, validate_task_record
from scisaurus.core.events import ControlStore

TERMINAL_STATES = frozenset({"completed", "failed", "rejected", "cancelled", "stale"})

TRANSITIONS = {
    "proposed": {"queued", "rejected", "cancelled", "stale"},
    "queued": {"running", "blocked", "cancelled", "stale"},
    "running": {"awaiting_review", "blocked", "failed", "paused", "cancelled", "stale"},
    "awaiting_review": {"queued", "blocked", "stale", "completed", "cancelled"},
    "blocked": {"queued", "awaiting_review", "cancelled", "stale"},
    "paused": {"queued", "cancelled", "stale"},
}

ATTEMPT_OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "result_unknown"})


class TaskManager:
    def __init__(self, control: ControlStore):
        self.control = control

    # -- task lifecycle --------------------------------------------------
    def create(self, task_id: str, kind: str, payload: dict, actor: str, *, causal=None) -> dict:
        record = {"task_id": task_id, "kind": kind, "payload": payload, "created_at": now_iso()}
        validate_task_record(record)
        with self.control.tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if exists:
                raise StateError(f"task already exists: {task_id}")
            conn.execute(
                "INSERT INTO tasks(task_id, kind, state, generation, payload_json, updated_at)"
                " VALUES (?, ?, 'proposed', 1, ?, ?)",
                (task_id, kind, canonical_bytes(payload).decode("utf-8"), now_iso()),
            )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="task.proposed",
                payload={"task_id": task_id, "kind": kind},
                causal=causal or [],
            )
        return self.get(task_id)

    def admit(self, task_id: str, actor: str) -> dict:
        return self.transition(task_id, "queued", actor, reason="admitted")

    def transition(self, task_id: str, new_state: str, actor: str, reason: str | None = None) -> dict:
        if new_state not in TERMINAL_STATES | set(TRANSITIONS):
            raise ValidationError(f"unknown task state: {new_state!r}")
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown task: {task_id}")
            current = row["state"]
            allowed = TRANSITIONS.get(current, set())
            if current in TERMINAL_STATES:
                raise StateError(f"task {task_id} is terminal ({current})")
            if new_state != current and new_state not in allowed:
                raise StateError(f"illegal transition {current} -> {new_state}")
            if current == "blocked" and new_state == "awaiting_review":
                attempt = conn.execute(
                    "SELECT state FROM attempts WHERE task_id=? ORDER BY lease_fence DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                if attempt is None or attempt["state"] != "succeeded":
                    raise StateError("review resumption requires a successful recorded attempt")
            conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                (new_state, now_iso(), task_id),
            )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="task.state_changed",
                payload={"task_id": task_id, "from": current, "to": new_state, "reason": reason},
            )
        return self.get(task_id)

    # -- attempts --------------------------------------------------------
    def start_attempt(
        self,
        task_id: str,
        attempt_id: str,
        *,
        owner: str,
        lease_ttl_seconds: float,
        external_ref: str | None = None,
        reserved: dict | None = None,
        payload: dict | None = None,
    ) -> int:
        with self.control.tx() as conn:
            task = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if task is None:
                raise NotFoundError(f"unknown task: {task_id}")
            if task["state"] not in {"queued", "running"}:
                raise StateError(f"task {task_id} not dispatchable (state={task['state']})")
            active = conn.execute(
                "SELECT attempt_id, state FROM attempts WHERE task_id = ? "
                "AND state IN ('started', 'result_unknown') LIMIT 1",
                (task_id,),
            ).fetchone()
            if active is not None:
                raise StateError(
                    f"task {task_id} already has unresolved attempt "
                    f"{active['attempt_id']} (state={active['state']})"
                )
            fence_row = conn.execute(
                "SELECT COALESCE(MAX(lease_fence), 0) AS f FROM attempts"
            ).fetchone()
            fence = int(fence_row["f"] or 0) + 1
            conn.execute(
                "INSERT INTO attempts(attempt_id, task_id, state, lease_owner,"
                " lease_fence, lease_expiry, external_ref, usage_json, payload_json, created_at)"
                " VALUES (?, ?, 'started', ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    task_id,
                    owner,
                    fence,
                    time.time() + lease_ttl_seconds,
                    external_ref,
                    canonical_bytes({"reserved": reserved or {}, "settled": None}).decode("utf-8"),
                    canonical_bytes(payload or {}).decode("utf-8"),
                    now_iso(),
                ),
            )
            if task["state"] == "queued":
                conn.execute(
                    "UPDATE tasks SET state='running', updated_at=? WHERE task_id = ?",
                    (now_iso(), task_id),
                )
            self.control.append_event(
                conn,
                actor=owner,
                event_type="attempt.started",
                payload={"attempt_id": attempt_id, "task_id": task_id, "external_ref": external_ref},
            )
        return fence

    def finish_attempt(self, attempt_id: str, outcome: str, *, usage: dict | None = None,
                       accounting: str | None = None) -> None:
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise ValidationError(f"finish outcome must be succeeded|failed|cancelled: {outcome!r}")
        if accounting is not None and (not isinstance(accounting, str) or not accounting.strip()):
            raise ValidationError("attempt accounting note must be a nonempty string")
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT state, usage_json FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown attempt: {attempt_id}")
            if row["state"] not in {"started", "result_unknown"}:
                raise StateError(f"attempt already finished: {row['state']}")
            merged = json.loads(row["usage_json"])
            if usage:
                merged["actual"] = usage
            if accounting is not None:
                merged["accounting"] = accounting
            conn.execute(
                "UPDATE attempts SET state = ?, finished_at = ?, usage_json = ?"
                " WHERE attempt_id = ?",
                (outcome, now_iso(), canonical_bytes(merged).decode("utf-8"), attempt_id),
            )
            task_id = conn.execute(
                "SELECT task_id FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()["task_id"]
            self.control.append_event(
                conn,
                actor="system",
                event_type="attempt.finished",
                payload={"attempt_id": attempt_id, "task_id": task_id, "outcome": outcome,
                         **({"accounting": accounting} if accounting is not None else {})},
            )

    def reconcile_unknown(self, attempt_id: str, actor: str, *, observed_usage=None) -> dict:
        """Record uncertainty for an external call whose completion is unknown (T05).

        The attempt is marked ``result_unknown``; its reserved resources stay
        conservatively accounted (never zero-cost). Active tasks enter blocked
        when their lifecycle permits it; terminal and paused decisions remain.
        """
        if observed_usage is not None and (
                not isinstance(observed_usage, dict)
                or any(not isinstance(key, str) or not key
                       or type(value) not in (int, float)
                       or not math.isfinite(value) or value < 0
                       for key, value in observed_usage.items())):
            raise ValidationError("observed unknown usage must contain nonnegative finite quantities")
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown attempt: {attempt_id}")
            if row["state"] != "started":
                raise StateError(f"attempt not running: {row['state']}")
            usage = json.loads(row["usage_json"])
            usage["accounting"] = "conservative_pending_reconciliation"
            if observed_usage:
                usage["observed"] = dict(observed_usage)
            conn.execute(
                "UPDATE attempts SET state='result_unknown', usage_json = ?"
                " WHERE attempt_id = ?",
                (canonical_bytes(usage).decode("utf-8"), attempt_id),
            )
            task = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (row["task_id"],)
            ).fetchone()
            if "blocked" in TRANSITIONS.get(task["state"], set()):
                conn.execute(
                    "UPDATE tasks SET state='blocked', updated_at=? WHERE task_id = ?",
                    (now_iso(), row["task_id"]),
                )
                self.control.append_event(
                    conn, actor=actor, event_type="task.state_changed",
                    payload={"task_id": row["task_id"], "from": task["state"],
                             "to": "blocked", "reason": "unknown_external_outcome"},
                )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="attempt.finished",
                payload={
                    "attempt_id": attempt_id,
                    "task_id": row["task_id"],
                    "outcome": "result_unknown",
                    "accounting": "conservative_pending_reconciliation",
                },
            )
        return self.get_attempt(attempt_id)

    # -- reads -----------------------------------------------------------
    def get(self, task_id: str) -> dict:
        row = self.control._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown task: {task_id}")
        record = dict(row)
        record["payload"] = json.loads(record.pop("payload_json"))
        return record

    def get_attempt(self, attempt_id: str) -> dict:
        row = self.control._conn.execute(
            "SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown attempt: {attempt_id}")
        record = dict(row)
        record["usage"] = json.loads(record.pop("usage_json"))
        record["payload"] = json.loads(record.pop("payload_json"))
        return record

    def attempts_for_task(self, task_id: str) -> list[dict]:
        """Return the durable attempt history for a task in creation order."""
        rows = self.control._conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY created_at, attempt_id",
            (task_id,),
        ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            record["usage"] = json.loads(record.pop("usage_json"))
            record["payload"] = json.loads(record.pop("payload_json"))
            records.append(record)
        return records
