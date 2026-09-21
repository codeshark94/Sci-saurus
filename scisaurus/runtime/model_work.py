"""Durable, input-addressed model work owned by the control-store writer."""
from copy import deepcopy
import hashlib
import json

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


class ModelWorkBlocked(ValidationError):
    """An unchanged assignment has exhausted its scoped repair allowance."""


class ModelWorkCache:
    """Retain checked results and repair state, never provider credentials.

    Keys include the actual bounded prompt, system contract, role and routing
    configuration. Attempt IDs and elapsed time are not scientific inputs.
    Callers must revalidate retained values before adopting them.
    """

    def __init__(self, store, publish, *, namespace="command/model-work"):
        self.store, self.publish = store, publish
        self.namespace = namespace

    @staticmethod
    def key(*, scope, role, system, prompt, model):
        return hashlib.sha256(canonical_bytes({
            "schema": "model-work-1", "scope": scope, "role": role,
            "system": system, "prompt": prompt, "model": model,
        })).hexdigest()

    def get(self, key):
        record = self.store.head(f"{self.namespace}/{key}")
        if record is None:
            return None
        body = json.loads(self.store.read_body(record["body_hash"]))
        return {**body, "cache_ref": record["artifact_ref"]}

    def entries(self):
        rows = self.store.control._conn.execute(
            "SELECT a.artifact_ref,a.body_hash FROM artifacts a JOIN "
            "(SELECT logical_id,MAX(version) version FROM artifacts WHERE logical_id LIKE ? GROUP BY logical_id) h "
            "ON a.logical_id=h.logical_id AND a.version=h.version ORDER BY a.created_at DESC",
            (self.namespace + "/%",))
        return [{**json.loads(self.store.read_body(row["body_hash"])), "cache_ref": row["artifact_ref"]}
                for row in rows]

    def put(self, key, body, *, subjects=()):
        record = self.publish(f"{self.namespace}/{key}", "note", deepcopy(body),
                              "command.controller", subjects=subjects)
        return {**deepcopy(body), "cache_ref": record["artifact_ref"]}
