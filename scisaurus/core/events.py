"""Transactional control store and append-only event hash chain.

Implements docs/40-execution-contract.md §11: every event carries a monotonic
per-project ``seq``, causal references, ``prev_event_hash`` and ``event_hash``;
the first event uses the declared genesis hash. Replaying events reconstructs
domain state; a trusted head hash detects alteration or truncation (T13).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

from scisaurus.core.schema import (
    GENESIS_HASH,
    SCHEMA_VERSION,
    canonical_bytes,
    now_iso,
    sha256_hex,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  actor TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  causal_json TEXT NOT NULL,
  prev_event_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS objects(
  hash TEXT PRIMARY KEY,
  size_bytes INTEGER NOT NULL,
  media_type TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts(
  logical_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  artifact_ref TEXT NOT NULL,
  artifact_type TEXT NOT NULL,
  owner TEXT NOT NULL,
  author TEXT NOT NULL,
  body_hash TEXT,
  body_media_type TEXT,
  body_size_bytes INTEGER NOT NULL,
  parents_json TEXT NOT NULL,
  inputs_json TEXT NOT NULL,
  governing_json TEXT NOT NULL,
  task_id TEXT,
  attempt_id TEXT,
  created_at TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  manifest_json TEXT NOT NULL,
  PRIMARY KEY (logical_id, version)
);
CREATE TABLE IF NOT EXISTS accepted_heads(
  logical_id TEXT PRIMARY KEY,
  accepted_version INTEGER NOT NULL,
  accepted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox(
  outbox_seq INTEGER PRIMARY KEY AUTOINCREMENT,
  effect_key TEXT,
  message_id TEXT NOT NULL,
  envelope_json TEXT NOT NULL,
  dispatched INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  message_id TEXT PRIMARY KEY,
  envelope_json TEXT NOT NULL,
  state TEXT NOT NULL,
  disposition TEXT,
  lease_owner TEXT,
  lease_fence INTEGER,
  lease_expiry REAL
);
CREATE TABLE IF NOT EXISTS effects(
  effect_key TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  disposition TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts(
  attempt_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  state TEXT NOT NULL,
  lease_owner TEXT,
  lease_fence INTEGER,
  lease_expiry REAL,
  external_ref TEXT,
  usage_json TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS edit_grants(
  grant_id TEXT PRIMARY KEY,
  request_ref TEXT NOT NULL,
  baseline_manifest_ref TEXT NOT NULL,
  actor TEXT NOT NULL,
  units_json TEXT NOT NULL,
  task_id TEXT,
  attempt_id TEXT,
  issued_at TEXT NOT NULL,
  expires_at REAL,
  state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retired_units(
  logical_id TEXT PRIMARY KEY,
  retired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issues(
  issue_id TEXT PRIMARY KEY,
  causal_key TEXT NOT NULL,
  state TEXT NOT NULL,
  generation INTEGER NOT NULL DEFAULT 1,
  critique_ref TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
"""


class ControlStore:
    """One transactional control store per project (40 §9.1)."""

    def __init__(self, project_dir):
        self.dir = os.fspath(project_dir)
        os.makedirs(os.path.join(self.dir, "state"), exist_ok=True)
        self.path = os.path.join(self.dir, "state", "control.sqlite")
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA_SQL)
        if self._conn.execute(
            "SELECT 1 FROM meta WHERE key='genesis'"
        ).fetchone() is None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES ('genesis', ?)",
                (GENESIS_HASH,),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('event_head', ?)",
                (GENESIS_HASH,),
            )

    # -- transactions ----------------------------------------------------
    @contextmanager
    def tx(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    # -- events ----------------------------------------------------------
    def append_event(self, conn, *, actor, event_type, payload, causal=None, ts=None):
        """Append one event inside an open transaction; updates the trusted head."""
        prev = self._conn.execute(
            "SELECT value FROM meta WHERE key='event_head'"
        ).fetchone()["value"]
        seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events"
        ).fetchone()["next_seq"]
        record = {
            "schema_version": SCHEMA_VERSION,
            "seq": seq,
            "ts": ts or now_iso(),
            "actor": actor,
            "event_type": event_type,
            "payload": payload,
            "causal": causal or [],
        }
        event_hash = sha256_hex(canonical_bytes({**record, "prev_event_hash": prev}))
        conn.execute(
            "INSERT INTO events(seq, ts, actor, event_type, payload_json,"
            " causal_json, prev_event_hash, event_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                seq,
                record["ts"],
                actor,
                event_type,
                canonical_bytes(payload).decode("utf-8"),
                canonical_bytes(causal or []).decode("utf-8"),
                prev,
                event_hash,
            ),
        )
        conn.execute(
            "UPDATE meta SET value=? WHERE key='event_head'", (event_hash,)
        )
        return seq, event_hash

    def append(self, *, actor, event_type, payload, causal=None):
        with self.tx() as conn:
            return self.append_event(
                conn, actor=actor, event_type=event_type, payload=payload, causal=causal
            )

    def replay(self, until_seq=None):
        sql = "SELECT * FROM events"
        params: tuple = ()
        if until_seq is not None:
            sql += " WHERE seq <= ?"
            params = (until_seq,)
        sql += " ORDER BY seq"
        out = []
        for row in self._conn.execute(sql, params):
            out.append(
                {
                    "seq": row["seq"],
                    "ts": row["ts"],
                    "actor": row["actor"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "causal": json.loads(row["causal_json"]),
                    "prev_event_hash": row["prev_event_hash"],
                    "event_hash": row["event_hash"],
                }
            )
        return out

    def trusted_head(self) -> str:
        return self._conn.execute(
            "SELECT value FROM meta WHERE key='event_head'"
        ).fetchone()["value"]

    def verify_chain(self, expected_head: str | None = None):
        """Recompute the whole chain; return (ok, reason). Detects T13 tampering."""
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY seq"
        ).fetchall()
        prev = GENESIS_HASH
        for row in rows:
            if row["seq"] < 1 or row["prev_event_hash"] != prev:
                return False, f"broken chain at seq={row['seq']}"
            record = {
                "schema_version": SCHEMA_VERSION,
                "seq": row["seq"],
                "ts": row["ts"],
                "actor": row["actor"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "causal": json.loads(row["causal_json"]),
            }
            if row["event_hash"] != sha256_hex(
                canonical_bytes({**record, "prev_event_hash": prev})
            ):
                return False, f"hash mismatch at seq={row['seq']}"
            prev = row["event_hash"]
        head = prev
        expected = expected_head if expected_head is not None else self.trusted_head()
        if head != expected:
            return False, (
                f"trusted head mismatch: chain ends at {head[:12]},"
                f" trusted head is {expected[:12]} (truncation or rewrite)"
            )
        return True, "ok"

    def close(self):
        self._conn.close()