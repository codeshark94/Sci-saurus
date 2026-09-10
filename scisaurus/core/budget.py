"""Capacity windows, reservations, settlement, and causal stagnation tracking.

Implements docs/40-execution-contract.md §2.1 and SSOT D27/D29: authorized
resources, measured capacity, and finite task/planning leases are separate.
Renewal atomically closes the prior window and creates a linked successor.
All windows sharing a policy use one capacity pool and usage ledger, so late
settlement and outstanding reservations survive renewal (T41/T42).
"""

from __future__ import annotations

import json
import math

from scisaurus.core.errors import ConflictError, NotFoundError, StateError, ValidationError
from scisaurus.core.schema import canonical_bytes, now_iso
from scisaurus.core.events import ControlStore

WINDOW_STATES = {"open", "draining", "closed", "revoked"}


class BudgetManager:
    def __init__(self, control: ControlStore):
        self.control = control
        self._migrate_pools()

    def _migrate_pools(self):
        """Recover legacy window accounting from immutable settlement events.

        A renewal snapshot includes its ancestors' usage, so snapshots cannot
        be summed. Root snapshots supply only pre-ledger usage; every recorded
        settlement is then counted once, including late ancestor settlements.
        """
        with self.control.tx() as conn:
            policies = conn.execute(
                "SELECT DISTINCT policy_id FROM allocation_windows WHERE policy_id"
                " NOT IN (SELECT policy_id FROM resource_pools)"
            ).fetchall()
            for policy in policies:
                windows = conn.execute(
                    "SELECT * FROM allocation_windows WHERE policy_id=? ORDER BY rowid",
                    (policy["policy_id"],),
                ).fetchall()
                settlements = conn.execute(
                    "SELECT r.window_id, e.payload_json FROM events e"
                    " JOIN reservations r ON r.reservation_id ="
                    " json_extract(e.payload_json, '$.reservation_id')"
                    " JOIN allocation_windows w ON w.window_id=r.window_id"
                    " WHERE e.event_type='budget.settled' AND w.policy_id=?",
                    (policy["policy_id"],),
                ).fetchall()
                local_usage = {}
                usage = {}
                for settlement in settlements:
                    actual = json.loads(settlement["payload_json"])["actual"]
                    _quantities(actual, "recorded usage")
                    _add(usage, actual)
                    _add(local_usage.setdefault(settlement["window_id"], {}), actual)
                for window in windows:
                    if window["prior_window"] is None:
                        for key, value in json.loads(window["cumulative_usage_json"]).items():
                            seed = value - local_usage.get(window["window_id"], {}).get(key, 0)
                            if seed < 0 and not math.isclose(seed, 0, abs_tol=1e-12):
                                raise ValidationError("window accounting contradicts settlement ledger")
                            if seed > 0 and not math.isclose(seed, 0, abs_tol=1e-12):
                                usage[key] = usage.get(key, 0) + seed
                capacity = json.loads(windows[0]["capacity_json"])
                _quantities(capacity, "capacity", nonempty=True)
                _quantities(usage, "cumulative usage")
                conn.execute(
                    "INSERT INTO resource_pools VALUES (?, ?, ?)",
                    (policy["policy_id"], canonical_bytes(capacity).decode(),
                     canonical_bytes(usage).decode()),
                )

    # -- windows ---------------------------------------------------------
    def open_window(
        self, *, window_id: str, policy_id: str, delegation_ref: str,
        capacity: dict, prior_window: str | None = None,
        carried_usage: dict | None = None, actor: str = "progress-controller",
    ) -> dict:
        """Open a window against the shared policy pool.

        Only renew() may link a successor. Initial usage may seed a new pool;
        later windows always read its durable ledger.
        """
        _quantities(capacity, "capacity", nonempty=True)
        _quantities(carried_usage if carried_usage is not None else {}, "carried usage")
        if prior_window is not None:
            raise ValidationError("linked windows must be created through renew()")
        with self.control.tx() as conn:
            pool = conn.execute(
                "SELECT * FROM resource_pools WHERE policy_id=?", (policy_id,)
            ).fetchone()
            if pool is None:
                conn.execute(
                    "INSERT INTO resource_pools VALUES (?, ?, ?)",
                    (policy_id, canonical_bytes(capacity).decode(),
                     canonical_bytes(carried_usage or {}).decode()),
                )
            else:
                _within(capacity, json.loads(pool["capacity_json"]))
                if carried_usage:
                    raise ValidationError("existing pool usage cannot be reseeded")
            self._open_in(conn, window_id=window_id, policy_id=policy_id,
                          delegation_ref=delegation_ref, capacity=capacity,
                          prior_window=None, actor=actor)
        return self.get_window(window_id)

    def _open_in(self, conn, *, window_id, policy_id, delegation_ref, capacity,
                 prior_window, actor):
        usage = conn.execute(
            "SELECT cumulative_usage_json FROM resource_pools WHERE policy_id=?",
            (policy_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO allocation_windows(window_id, policy_id, delegation_ref,"
            " prior_window, state, capacity_json, reserved_json, cumulative_usage_json, opened_at)"
            " VALUES (?, ?, ?, ?, 'open', ?, '{}', ?, ?)",
            (window_id, policy_id, delegation_ref, prior_window,
             canonical_bytes(capacity).decode(), usage, now_iso()),
        )
        self.control.append_event(
            conn, actor=actor, event_type="allocation.opened",
            payload={"window_id": window_id, "policy_id": policy_id,
                     "renewal_of": prior_window},
        )

    def renew(
        self, *, prior_window_id: str, new_window_id: str, rationale: str,
        actor: str = "progress-controller", capacity_delta: dict | None = None,
    ) -> dict:
        """Atomically close and replace a window without resetting pool state."""
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValidationError("renewal requires a recorded continuation rationale")
        _quantities(capacity_delta if capacity_delta is not None else {}, "capacity delta")
        with self.control.tx() as conn:
            prior = conn.execute(
                "SELECT * FROM allocation_windows WHERE window_id=?", (prior_window_id,)
            ).fetchone()
            if prior is None:
                raise NotFoundError(f"unknown window: {prior_window_id}")
            if prior["state"] not in {"open", "draining"}:
                raise StateError(f"window {prior_window_id} not renewable: {prior['state']}")
            base = json.loads(prior["capacity_json"])
            capacity = {**base, **(capacity_delta or {})}
            _within(capacity, base)
            self._open_in(conn, window_id=new_window_id, policy_id=prior["policy_id"],
                          delegation_ref=prior["delegation_ref"], capacity=capacity,
                          prior_window=prior_window_id, actor=actor)
            conn.execute(
                "UPDATE allocation_windows SET state='closed', closed_at=? WHERE window_id=?",
                (now_iso(), prior_window_id),
            )
            self.control.append_event(
                conn, actor=actor, event_type="allocation.closed",
                payload={"window_id": prior_window_id, "renewed_as": new_window_id,
                         "rationale": rationale},
            )
        return self.get_window(new_window_id)

    def _reserved_in(self, conn, policy_id):
        reserved = {}
        for row in conn.execute(
            "SELECT r.amount_json FROM reservations r JOIN allocation_windows w"
            " ON w.window_id=r.window_id WHERE w.policy_id=? AND r.state='reserved'",
            (policy_id,),
        ):
            amount = json.loads(row[0])
            _quantities(amount, "outstanding reservation", nonempty=True)
            _add(reserved, amount)
        _quantities(reserved, "outstanding reservations")
        return reserved

    def reserve(self, *, window_id: str, reservation_id: str, task_id: str, amount: dict) -> dict:
        """Reserve concurrent capacity across all windows sharing a policy."""
        _quantities(amount, "reservation", nonempty=True)
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT * FROM allocation_windows WHERE window_id=?", (window_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown window: {window_id}")
            if row["state"] != "open":
                raise StateError(f"window {window_id} not reservable: {row['state']}")
            capacity = json.loads(row["capacity_json"])
            pool = conn.execute(
                "SELECT capacity_json FROM resource_pools WHERE policy_id=?", (row["policy_id"],)
            ).fetchone()
            ceiling = json.loads(pool[0])
            reserved = self._reserved_in(conn, row["policy_id"])
            for key, value in amount.items():
                if key not in capacity or key not in ceiling:
                    raise ValidationError(f"unallocated resource dimension: {key!r}")
                available = min(capacity[key], ceiling[key]) - reserved.get(key, 0)
                if value > available:
                    raise ConflictError(f"insufficient {key}: requested {value}, available {available}")
            conn.execute(
                "INSERT INTO reservations(reservation_id, window_id, task_id, amount_json,"
                " state, created_at) VALUES (?, ?, ?, ?, 'reserved', ?)",
                (reservation_id, window_id, task_id, canonical_bytes(amount).decode(), now_iso()),
            )
            self.control.append_event(
                conn, actor="scheduler", event_type="budget.reserved",
                payload={"reservation_id": reservation_id, "window_id": window_id, "amount": amount},
            )
        return self.get_reservation(reservation_id)

    def settle(self, *, window_id: str, reservation_id: str, actual: dict) -> dict:
        """Release the reservation and record actuals once, even after renewal.

        Measured overruns remain visible; they cannot be discarded to make the
        original estimate appear accurate. Policy expenditure limits belong to
        metered admission, separate from this concurrent-capacity ledger.
        """
        _quantities(actual, "actual usage")
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT r.*, w.policy_id FROM reservations r JOIN allocation_windows w"
                " ON w.window_id=r.window_id WHERE r.reservation_id=?", (reservation_id,)
            ).fetchone()
            if row is None or row["window_id"] != window_id:
                raise NotFoundError(f"unknown reservation: {reservation_id}")
            if row["state"] != "reserved":
                raise StateError(f"reservation already {row['state']}")
            pool = conn.execute(
                "SELECT cumulative_usage_json FROM resource_pools WHERE policy_id=?", (row["policy_id"],)
            ).fetchone()
            usage = json.loads(pool[0])
            _add(usage, actual)
            _quantities(usage, "cumulative usage")
            conn.execute("UPDATE reservations SET state='settled' WHERE reservation_id=?", (reservation_id,))
            conn.execute(
                "UPDATE resource_pools SET cumulative_usage_json=? WHERE policy_id=?",
                (canonical_bytes(usage).decode(), row["policy_id"]),
            )
            self.control.append_event(
                conn, actor="scheduler", event_type="budget.settled",
                payload={"reservation_id": reservation_id, "window_id": window_id, "actual": actual},
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
        window.pop("reserved_json")
        window.pop("cumulative_usage_json")
        window["reserved"] = self._reserved_in(self.control._conn, window["policy_id"])
        pool = self.control._conn.execute(
            "SELECT cumulative_usage_json FROM resource_pools WHERE policy_id=?", (window["policy_id"],)
        ).fetchone()
        window["cumulative_usage"] = json.loads(pool[0])
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


def _quantities(values, label, *, nonempty=False):
    if not isinstance(values, dict) or (nonempty and not values):
        raise ValidationError(f"{label} must be a resource quantity mapping")
    for key, value in values.items():
        if not isinstance(key, str) or not key or type(value) not in (int, float):
            raise ValidationError(f"{label} requires named numeric quantities")
        if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
            raise ValidationError(f"{label} must be finite and non-negative")


def _within(capacity, ceiling):
    for key, value in capacity.items():
        if key not in ceiling or value > ceiling[key]:
            raise ValidationError(f"window cannot raise the hard limit for {key!r}")


def _add(total, amount):
    for key, value in amount.items():
        total[key] = total.get(key, 0) + value
