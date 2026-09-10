"""Capacity windows, reservations, settlement, and causal stagnation tracking.

Implements docs/40-execution-contract.md §2.1 and SSOT D27/D29: authorized
resources, measured capacity, and finite task/planning leases are separate.
Renewal creates a new linked window under existing delegation — it never
mutates the old one, never erases cumulative usage or stagnation history
(T41/T42), and cannot raise a hard limit.
"""

from __future__ import annotations

import json

from scisaurus.core.errors import ConflictError, NotFoundError, StateError, ValidationError
from scisaurus.core.schema import canonical_bytes, now_iso
from scisaurus.core.events import ControlStore

WINDOW_STATES = {"open", "draining", "closed", "revoked"}


class BudgetManager:
    def __init__(self, control: ControlStore):
        self.control = control

    # -- windows ---------------------------------------------------------
    def open_window(
        self,
        *,
        window_id: str,
        policy_id: str,
        delegation_ref: str,
        capacity: dict,
        prior_window: str | None = None,
        carried_usage: dict | None = None,
        actor: str = "progress-controller",
    ) -> dict:
        if not capacity or any(v < 0 for v in capacity.values()):
            raise ValidationError(f"window capacity must be finite and non-negative: {capacity!r}")
        with self.control.tx() as conn:
            conn.execute(
                "INSERT INTO allocation_windows(window_id, policy_id, delegation_ref,"
                " prior_window, state, capacity_json, reserved_json, cumulative_usage_json, opened_at)"
                " VALUES (?, ?, ?, ?, 'open', ?, '{}', ?, ?)",
                (
                    window_id,
                    policy_id,
                    delegation_ref,
                    prior_window,
                    canonical_bytes(capacity).decode("utf-8"),
                    canonical_bytes(carried_usage or {}).decode("utf-8"),
                    now_iso(),
                ),
            )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="allocation.opened",
                payload={
                    "window_id": window_id,
                    "policy_id": policy_id,
                    "renewal_of": prior_window,
                },
            )
        return self.get_window(window_id)

    def renew(
        self,
        *,
        prior_window_id: str,
        new_window_id: str,
        rationale: str,
        actor: str = "progress-controller",
        capacity_delta: dict | None = None,
    ) -> dict:
        """Automatic renewal inside an existing delegation (T41).

        Requires a recorded continuation rationale; cannot raise the hard
        capacity ceiling; cumulative usage and stagnation history carry over.
        """
        prior = self.get_window(prior_window_id)
        if rationale is None or not rationale.strip():
            raise ValidationError("renewal requires a recorded continuation rationale")
        if prior["state"] not in {"open", "draining"}:
            raise StateError(f"window {prior_window_id} not renewable: {prior['state']}")
        capacity = dict(_merge(prior["capacity"], capacity_delta or {}))
        for key, value in capacity.items():
            base = prior["capacity"].get(key)
            if base is not None and value > base:
                raise ValidationError(
                    f"renewal cannot raise the hard limit for {key!r}"
                    f" ({base} -> {value})"
                )
        with self.control.tx() as conn:
            conn.execute(
                "UPDATE allocation_windows SET state='closed', closed_at = ?"
                " WHERE window_id = ?",
                (now_iso(), prior_window_id),
            )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="allocation.closed",
                payload={"window_id": prior_window_id, "renewed_as": new_window_id},
            )
        return self.open_window(
            window_id=new_window_id,
            policy_id=prior["policy_id"],
            delegation_ref=prior["delegation_ref"],
            capacity=capacity,
            prior_window=prior_window_id,
            carried_usage=prior["cumulative_usage"],
            actor=actor,
        )

    def reserve(
        self,
        *,
        window_id: str,
        reservation_id: str,
        task_id: str,
        amount: dict,
    ) -> dict:
        """Atomically reserve finite capacity; pool-wide limits are respected."""
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT * FROM allocation_windows WHERE window_id = ?", (window_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown window: {window_id}")
            if row["state"] != "open":
                raise StateError(f"window {window_id} not reservable: {row['state']}")
            capacity = json.loads(row["capacity_json"])
            reserved = json.loads(row["reserved_json"])
            for key, value in amount.items():
                avail = capacity.get(key, 0) - reserved.get(key, 0)
                if value > avail:
                    raise ConflictError(
                        f"insufficient {key}: requested {value}, available {avail}"
                        " (atomic reservation; T53)"
                    )
                reserved[key] = reserved.get(key, 0) + value
            conn.execute(
                "UPDATE allocation_windows SET reserved_json = ? WHERE window_id = ?",
                (canonical_bytes(reserved).decode("utf-8"), window_id),
            )
            conn.execute(
                "INSERT INTO reservations(reservation_id, window_id, task_id, amount_json,"
                " state, created_at) VALUES (?, ?, ?, ?, 'reserved', ?)",
                (
                    reservation_id,
                    window_id,
                    task_id,
                    canonical_bytes(amount).decode("utf-8"),
                    now_iso(),
                ),
            )
            self.control.append_event(
                conn,
                actor="scheduler",
                event_type="budget.reserved",
                payload={"reservation_id": reservation_id, "window_id": window_id, "amount": amount},
            )
        return self.get_reservation(reservation_id)

    def settle(self, *, window_id: str, reservation_id: str, actual: dict) -> dict:
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id,)
            ).fetchone()
            if row is None or row["window_id"] != window_id:
                raise NotFoundError(f"unknown reservation: {reservation_id}")
            if row["state"] != "reserved":
                raise StateError(f"reservation already {row['state']}")
            window = self.get_window(window_id)
            usage = dict(window["cumulative_usage"])
            for key, value in actual.items():
                usage[key] = usage.get(key, 0) + value
            conn.execute(
                "UPDATE reservations SET state='settled' WHERE reservation_id = ?",
                (reservation_id,),
            )
            conn.execute(
                "UPDATE allocation_windows SET cumulative_usage_json = ? WHERE window_id = ?",
                (canonical_bytes(usage).decode("utf-8"), window_id),
            )
            self.control.append_event(
                conn,
                actor="scheduler",
                event_type="budget.settled",
                payload={"reservation_id": reservation_id, "actual": actual},
            )
        return self.get_window(window_id)

    # -- stagnation (T42) -------------------------------------------------
    def mark_unproductive(self, *, cause_key: str, window_id: str) -> dict:
        """Record a causal stagnation observation. The cause key persists across
        window renewals — renaming tasks or rolling windows cannot reset it."""
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT * FROM stagnation WHERE cause_key = ?", (cause_key,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO stagnation(cause_key, observed_count, windows_spanned,"
                    " first_seen, last_seen) VALUES (?, 1, ?, ?, ?)",
                    (
                        cause_key,
                        canonical_bytes([window_id]).decode("utf-8"),
                        now_iso(),
                        now_iso(),
                    ),
                )
            else:
                spanned = json.loads(row["windows_spanned"])
                if window_id not in spanned:
                    spanned.append(window_id)
                conn.execute(
                    "UPDATE stagnation SET observed_count = observed_count + 1,"
                    " windows_spanned = ?, last_seen = ? WHERE cause_key = ?",
                    (canonical_bytes(spanned).decode("utf-8"), now_iso(), cause_key),
                )
        return self.stagnation_state(cause_key)

    def stagnation_state(self, cause_key: str) -> dict:
        row = self.control._conn.execute(
            "SELECT * FROM stagnation WHERE cause_key = ?", (cause_key,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no stagnation record: {cause_key}")
        record = dict(row)
        record["windows_spanned"] = json.loads(record.pop("windows_spanned"))
        return record

    # -- reads ------------------------------------------------------------
    def get_window(self, window_id: str) -> dict:
        row = self.control._conn.execute(
            "SELECT * FROM allocation_windows WHERE window_id = ?", (window_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown window: {window_id}")
        window = dict(row)
        window["capacity"] = json.loads(window.pop("capacity_json"))
        window["reserved"] = json.loads(window.pop("reserved_json"))
        window["cumulative_usage"] = json.loads(window.pop("cumulative_usage_json"))
        return window

    def get_reservation(self, reservation_id: str) -> dict:
        row = self.control._conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?", (reservation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown reservation: {reservation_id}")
        record = dict(row)
        record["amount"] = json.loads(record.pop("amount_json"))
        return record


def _merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    out.update(extra)
    return out