"""Immutable content-addressed objects, artifact versions, and adoption CAS.

Implements docs/40-execution-contract.md §3.1 and the publish transaction of
docs/20-architecture-v0.md §9.3: blob published before the database commit
(a crash leaves a harmless orphan, T03), version allocation and events in one
transaction, and accepted-head changes via compare-and-swap (T02).
"""

from __future__ import annotations

import json
import os
import tempfile

from scisaurus.core.errors import ConflictError, NotFoundError, ValidationError
from scisaurus.core.schema import (
    SCHEMA_VERSION,
    ARTIFACT_TYPES,
    canonical_bytes,
    format_ref,
    now_iso,
    parse_ref,
    sha256_hex,
    validate_artifact_manifest,
)
from scisaurus.core.events import ControlStore

# Initial namespace authority map (20 §10). "command" covers command/*.
NAMESPACE_OWNERS = {
    "inputs": "principal",
    "command": "command",
    "kb": "research",
    "strategy": "strategy",
    "methods": "methods",
    "editorial": "editorial",
    "fixtures": "test",
    "releases": "archivist",
}


class ArtifactStore:
    def __init__(self, control):
        self.control = control
        self.objects_dir = os.path.join(control.dir, "objects", "sha256")

    # -- project ---------------------------------------------------------
    def init_project(self, *, principal_note: str | None = None) -> None:
        for sub in (
            "objects/sha256",
            "manifests/artifacts",
            "workspace",
            "state",
            "ledger",
            "runs",
        ):
            os.makedirs(os.path.join(self.control.dir, sub), exist_ok=True)
        with self.control.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('project_init', ?)",
                (principal_note or "",),
            )
            self.control.append_event(
                conn,
                actor="system",
                event_type="project.created",
                payload={"project_dir": os.path.basename(self.control.dir)},
            )

    # -- objects ---------------------------------------------------------
    def publish_object(self, body: bytes, media_type: str) -> str:
        """Write a content-addressed blob; reuse existing objects (dedupe)."""
        h = sha256_hex(body)
        os.makedirs(self.objects_dir, exist_ok=True)
        path = os.path.join(self.objects_dir, h)
        if not os.path.exists(path):
            fd, tmp = tempfile.mkstemp(dir=self.objects_dir)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(body)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)  # atomic publish
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        with self.control.tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO objects(hash, size_bytes, media_type, created_at)"
                " VALUES (?, ?, ?, ?)",
                (h, len(body), media_type, now_iso()),
            )
        return h

    def read_body(self, body_hash: str) -> bytes:
        path = os.path.join(self.objects_dir, body_hash)
        if not os.path.exists(path):
            raise NotFoundError(f"object {body_hash} missing")
        with open(path, "rb") as fh:
            return fh.read()

    # -- artifacts -------------------------------------------------------
    def publish_artifact(
        self,
        *,
        logical_id: str,
        artifact_type: str,
        author: str,
        body: bytes | None = None,
        media_type: str = "application/octet-stream",
        parents: list[str] | None = None,
        inputs: list[dict] | None = None,
        intent_ref: str | None = None,
        mission_ref: str | None = None,
        score_ref: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        owner: str | None = None,
        created_at: str | None = None,
        messages: list[dict] | None = None,
    ) -> dict:
        ns, name = logical_id.split("/", 1) if "/" in logical_id else ("", logical_id)
        if not ns:
            raise ValidationError("logical_id must be '<namespace>/<name>'")
        owner = owner or NAMESPACE_OWNERS.get(ns)
        if owner is None:
            raise ValidationError(f"namespace has no authorized owner: {ns!r}")
        if artifact_type not in ARTIFACT_TYPES:
            raise ValidationError(f"unregistered artifact type: {artifact_type!r}")

        # 1. publish blob before the transaction (crash leaves an orphan, T03)
        body_hash = None
        if body is not None:
            body_hash = self.publish_object(body, media_type)

        # 2. one transaction: version allocation + event + outbox
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT version, artifact_ref FROM artifacts"
                " WHERE logical_id = ? ORDER BY version DESC LIMIT 1",
                (logical_id,),
            ).fetchone()
            version = (row["version"] if row else 0) + 1
            if parents is None:
                parents = [format_ref(logical_id, row["version"])] if row else []
            inputs = list(inputs or [])
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "artifact_ref": format_ref(logical_id, version),
                "artifact_id": logical_id,
                "version": version,
                "artifact_type": artifact_type,
                "owner": owner,
                "author": author,
                "body_hash": body_hash,
                "body_media_type": media_type if body is not None else None,
                "body_size_bytes": len(body) if body is not None else 0,
                "parents": parents,
                "inputs": inputs,
                "intent_ref": intent_ref,
                "mission_ref": mission_ref,
                "score_ref": score_ref,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "context_ref": None,
                "created_at": created_at or now_iso(),
            }
            validate_artifact_manifest(manifest)
            manifest_hash = sha256_hex(canonical_bytes(manifest))
            conn.execute(
                "INSERT INTO artifacts(logical_id, version, artifact_ref,"
                " artifact_type, owner, author, body_hash, body_media_type,"
                " body_size_bytes, parents_json, inputs_json, governing_json,"
                " task_id, attempt_id, created_at, manifest_hash, manifest_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    logical_id,
                    version,
                    manifest["artifact_ref"],
                    artifact_type,
                    owner,
                    author,
                    body_hash,
                    manifest["body_media_type"],
                    manifest["body_size_bytes"],
                    canonical_bytes(manifest["parents"]).decode("utf-8"),
                    canonical_bytes(manifest["inputs"]).decode("utf-8"),
                    canonical_bytes(
                        {
                            "intent_ref": intent_ref,
                            "mission_ref": mission_ref,
                            "score_ref": score_ref,
                        }
                    ).decode("utf-8"),
                    task_id,
                    attempt_id,
                    manifest["created_at"],
                    manifest_hash,
                    canonical_bytes(manifest).decode("utf-8"),
                ),
            )
            self.control.append_event(
                conn,
                actor=author,
                event_type="artifact.published",
                payload={
                    "artifact_ref": manifest["artifact_ref"],
                    "manifest_hash": manifest_hash,
                    "body_hash": body_hash,
                    "artifact_type": artifact_type,
                },
                causal=[task_id] if task_id else [],
            )
            for envelope in messages or []:
                conn.execute(
                    "INSERT INTO outbox(effect_key, message_id, envelope_json, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        envelope.get("idempotency_key"),
                        envelope["message_id"],
                        canonical_bytes(envelope).decode("utf-8"),
                        now_iso(),
                    ),
                )
        return manifest

    # -- reads -----------------------------------------------------------
    def get(self, ref: str) -> dict:
        ns, name, version = parse_ref(ref)
        logical_id = f"{ns}/{name}"
        row = self._conn_artifact(logical_id, version)
        if row is None:
            raise NotFoundError(f"artifact not found: {ref}")
        return json.loads(row["manifest_json"])

    def head(self, logical_id: str) -> dict | None:
        row = self.control._conn.execute(
            "SELECT manifest_json FROM artifacts WHERE logical_id = ?"
            " ORDER BY version DESC LIMIT 1",
            (logical_id,),
        ).fetchone()
        return json.loads(row["manifest_json"]) if row else None

    def accepted(self, logical_id: str) -> dict | None:
        row = self.control._conn.execute(
            "SELECT accepted_version FROM accepted_heads WHERE logical_id = ?",
            (logical_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get(format_ref(logical_id, row["accepted_version"]))

    def versions(self, logical_id: str) -> list[int]:
        rows = self.control._conn.execute(
            "SELECT version FROM artifacts WHERE logical_id = ? ORDER BY version",
            (logical_id,),
        ).fetchall()
        return [r["version"] for r in rows]

    # -- adoption (CAS) --------------------------------------------------
    def adopt(
        self,
        logical_id: str,
        *,
        target_version: int,
        expected_accepted_version: int | None,
        actor: str,
    ) -> dict:
        """Accepted-head compare-and-swap (T02). expected=None means 'no head yet'."""
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT accepted_version FROM accepted_heads WHERE logical_id = ?",
                (logical_id,),
            ).fetchone()
            current = row["accepted_version"] if row else None
            if current != expected_accepted_version:
                raise ConflictError(
                    f"head moved for {logical_id}: expected {expected_accepted_version!r},"
                    f" current {current!r}"
                )
            artifact = conn.execute(
                "SELECT artifact_ref FROM artifacts WHERE logical_id = ? AND version = ?",
                (logical_id, target_version),
            ).fetchone()
            if artifact is None:
                raise NotFoundError(f"no version {target_version} of {logical_id}")
            conn.execute(
                "INSERT INTO accepted_heads(logical_id, accepted_version, accepted_at)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(logical_id) DO UPDATE SET accepted_version = excluded.accepted_version,"
                " accepted_at = excluded.accepted_at",
                (logical_id, target_version, now_iso()),
            )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="artifact.accepted",
                payload={
                    "artifact_ref": format_ref(logical_id, target_version),
                    "previous_accepted": current,
                },
            )
        return self.get(format_ref(logical_id, target_version))

    # -- internal --------------------------------------------------------
    def _conn_artifact(self, logical_id, version):
        return self.control._conn.execute(
            "SELECT * FROM artifacts WHERE logical_id = ? AND version = ?",
            (logical_id, version),
        ).fetchone()