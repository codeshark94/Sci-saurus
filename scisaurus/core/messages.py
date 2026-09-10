"""Message bus: outbox recovery, leases, fencing tokens, idempotent effects.

Implements docs/40-execution-contract.md §5: immutable envelope bodies, at-
least-once delivery with per-message acknowledgement, leases with fencing
tokens (stale workers cannot commit effects, T04), idempotency keys that
prevent duplicate delivery from committing the same effect twice, and the
transactional outbox (publish artifact + messages in one local transaction).
"""

from __future__ import annotations

import json
import time

from scisaurus.core.errors import (
    ConflictError,
    NotFoundError,
    StaleFenceError,
    StateError,
    ValidationError,
)
from scisaurus.core.schema import canonical_bytes, now_iso, validate_message
from scisaurus.core.events import ControlStore


class MessageBus:
    def __init__(self, control: ControlStore):
        self.control = control

    # -- publishing ------------------------------------------------------
    def publish(self, envelope: dict) -> str:
        from scisaurus.core.schema import validate_message

        validate_message(envelope)
        with self.control.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO messages(message_id, envelope_json, state)"
                " VALUES (?, ?, 'pending')",
                (
                    envelope["message_id"],
                    canonical_bytes(envelope).decode("utf-8"),
                ),
            )
            conn.execute(
                "INSERT INTO outbox(effect_key, message_id, envelope_json, created_at,"
                " dispatched) VALUES (?, ?, ?, ?, 1)",
                (
                    envelope.get("idempotency_key"),
                    envelope["message_id"],
                    canonical_bytes(envelope).decode("utf-8"),
                    now_iso(),
                ),
            )
            self.control.append_event(
                conn,
                actor=envelope.get("from", {}).get("agent", "system"),
                event_type="message.created",
                payload={"message_id": envelope["message_id"], "type": envelope["type"]},
            )
        return envelope["message_id"]

    def recover_outbox(self) -> int:
        """Dispatch undispatched outbox entries after a crash (T03)."""
        rows = self.control._conn.execute(
            "SELECT outbox_seq, effect_key, message_id, envelope_json FROM outbox"
            " WHERE dispatched = 0 ORDER BY outbox_seq"
        ).fetchall()
        count = 0
        for row in rows:
            with self.control.tx() as conn:
                still = conn.execute(
                    "SELECT dispatched FROM outbox WHERE outbox_seq = ?",
                    (row["outbox_seq"],),
                ).fetchone()
                if still is None or still["dispatched"]:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM messages WHERE message_id = ?", (row["message_id"],)
                ).fetchone()
                if exists is None:
                    envelope = json.loads(row["envelope_json"])
                    from scisaurus.core.schema import validate_message

                    validate_message(envelope)
                    conn.execute(
                        "INSERT OR IGNORE INTO messages(message_id, envelope_json, state)"
                        " VALUES (?, ?, 'pending')",
                        (row["message_id"], row["envelope_json"]),
                    )
                    self.control.append_event(
                        conn,
                        actor="system",
                        event_type="message.created",
                        payload={
                            "message_id": row["message_id"],
                            "type": envelope["type"],
                            "via": "outbox_recovery",
                        },
                    )
                conn.execute(
                    "UPDATE outbox SET dispatched = 1 WHERE outbox_seq = ?",
                    (row["outbox_seq"],),
                )
            count += 1
        return count

    def lease(self, message_id: str, owner: str, ttl_seconds: float = 30.0) -> int:
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT state, lease_owner, lease_expiry FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message_id}")
            if row["state"] == "acknowledged":
                raise StateError("message already acknowledged")
            if row["state"] == "leased":
                if row["lease_expiry"] is not None and row["lease_expiry"] > time.time():
                    raise ConflictError(
                        f"message {message_id} leased by {row['lease_owner']}"
                    )
            fence = self._next_fence(conn)
            conn.execute(
                "UPDATE messages SET state='leased', lease_owner=?, lease_fence=?,"
                " lease_expiry=? WHERE message_id = ?",
                (owner, fence, time.time() + ttl_seconds, message_id),
            )
        return fence

    def _next_fence(self, conn) -> int:
        """Monotonic fencing token, persisted in meta so expiry cannot reset it."""
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'fence_counter'"
        ).fetchone()
        current = int(row["value"]) if row else 0
        nxt = current + 1
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('fence_counter', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(nxt),),
        )
        return nxt

    def expire_stale_leases(self) -> int:
        now = time.time()
        with self.control.tx() as conn:
            cur = conn.execute(
                "UPDATE messages SET state='retry_pending', lease_owner=NULL,"
                " lease_fence=NULL, lease_expiry=NULL"
                " WHERE state='leased' AND lease_expiry <= ?",
                (now,),
            )
        return cur.rowcount

    def acknowledge(
        self, message_id: str, fence: int, disposition: str, actor: str
    ) -> tuple[str, bool]:
        """Commit a disposition under a valid fencing token; idempotent effect."""
        if disposition not in {"scheduled", "linked_existing", "deferred", "rejected", "escalated"}:
            raise ValidationError(f"unknown disposition: {disposition!r}")
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown message: {message_id}")
            if row["state"] != "leased":
                raise StateError(f"message not leased: state={row['state']}")
            if row["lease_fence"] != fence:
                raise StaleFenceError(
                    f"fence {fence} superseded by {row['lease_fence']}"
                )
            if row["lease_expiry"] is None or row["lease_expiry"] <= time.time():
                raise StaleFenceError("lease expired")
            envelope = json.loads(row["envelope_json"])
            effect_key = envelope.get("idempotency_key") or message_id
            created = conn.execute(
                "INSERT OR IGNORE INTO effects(effect_key, message_id, disposition, recorded_at)"
                " VALUES (?, ?, ?, ?)",
                (effect_key, message_id, disposition, now_iso()),
            ).rowcount == 1
            conn.execute(
                "UPDATE messages SET state='acknowledged', disposition=? WHERE message_id = ?",
                (disposition, message_id),
            )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="message.dispositioned",
                payload={
                    "message_id": message_id,
                    "disposition": disposition,
                    "effect_key": effect_key,
                    "effect_recorded": created,
                },
            )
        return effect_key, created

    # -- reads -----------------------------------------------------------
    def pending(self) -> list[str]:
        rows = self.control._conn.execute(
            "SELECT message_id FROM messages WHERE state = 'pending' ORDER BY message_id"
        ).fetchall()
        return [r["message_id"] for r in rows]

    def state_of(self, message_id: str) -> dict:
        row = self.control._conn.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown message: {message_id}")
        return dict(row)