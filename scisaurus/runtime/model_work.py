"""Durable, input-addressed model work owned by the control-store writer."""
from copy import deepcopy
import hashlib
import json
import math

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


class ModelWorkProvenanceError(ValidationError):
    """A controller receipt cannot be bound to its immutable request history."""

    failure_class = "harness_bug"
    diagnostic_kind = "checkpoint_provenance"


def validate_foundry_usage_inheritance(body, record, source):
    inheritance = body.get("usage_inheritance")
    count = inheritance.get("source_request_count") if isinstance(inheritance, dict) else None
    if (not isinstance(source.get("requests"), list)
            or not isinstance(body.get("requests"), list)
            or record.get("author") != "command.controller"
            or not record.get("artifact_ref", "").startswith("artifact:command/foundry-work/")
            or type(count) is not int or count < 1
            or len(source.get("requests", [])) != count
            or body.get("requests", [])[:count] != source["requests"]
            or source.get("assignment") != body.get("assignment")):
        raise ModelWorkProvenanceError("foundry usage inheritance does not bind the captured request prefix")
    inherited = source.get("usage", {})
    cumulative = body.get("usage", {})
    if not isinstance(inherited, dict) or not isinstance(cumulative, dict):
        raise ModelWorkProvenanceError("foundry usage must be a numeric charge mapping")
    if any(type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0
           or type(cumulative.get(key, 0)) not in (int, float)
           or not math.isfinite(cumulative.get(key, 0))
           or cumulative.get(key, 0) < amount for key, amount in inherited.items()):
        raise ModelWorkProvenanceError("foundry inherited usage exceeds its cumulative assignment history")
    return inherited


def reconcile_inherited_request_outcomes(body, record, source, *, target_ref):
    """Recover legacy in-place unknown-outcome annotations as separate receipts."""
    corrected = deepcopy(body)
    inheritance = body.get("usage_inheritance")
    count = inheritance.get("source_request_count") if isinstance(inheritance, dict) else None
    if (not isinstance(body.get("requests"), list)
            or not isinstance(source.get("requests"), list)):
        raise ModelWorkProvenanceError("inherited request history must be an ordered request list")
    if type(count) is not int or count < 1 or len(body.get("requests", [])) < count:
        raise ModelWorkProvenanceError("inherited request history has no complete prefix")
    if len(body["requests"]) != count or body.get("usage") != source.get("usage"):
        raise ModelWorkProvenanceError("request history recovery cannot discard later dispatched work")
    outcomes = []
    for index, (original, observed) in enumerate(zip(source.get("requests", []), body["requests"][:count])):
        if original == observed:
            continue
        if (not isinstance(original, dict) or not isinstance(observed, dict)
                or original.get("error") is not None
                or original.get("status") != "started" or observed.get("status") != "result_unknown"
                or not isinstance(observed.get("error"), str) or not observed["error"].strip()
                or {key: value for key, value in original.items() if key not in {"status", "error"}}
                   != {key: value for key, value in observed.items() if key not in {"status", "error"}}):
            raise ModelWorkProvenanceError("inherited request history changes dispatched request identity")
        outcomes.append({"request_index": index, "request_sha256": hashlib.sha256(canonical_bytes(original)).hexdigest(),
                         "status": "result_unknown", "error": observed["error"]})
        corrected["requests"][index] = deepcopy(original)
    validate_foundry_usage_inheritance(corrected, record, source)
    if not outcomes:
        raise ModelWorkProvenanceError("inherited request history has no reconcilable outcome transition")
    corrected["inherited_request_history_reconciliation"] = {
        "prior_ref": target_ref, "source_ref": body["usage_inheritance"]["source_ref"],
        "outcomes": outcomes, "excluded_from_candidate_recovery": True}
    return corrected


class ModelWorkBlocked(ValidationError):
    """An unchanged assignment has exhausted its scoped repair allowance."""

    def __init__(self, message, *, failure_class=None):
        super().__init__(message)
        if failure_class is not None:
            self.failure_class = failure_class

    @classmethod
    def from_states(cls, states):
        states = list(states)
        failure_class = ("model_contract" if states and all(
            state.get("failure_class") == "model_contract" for state in states) else None)
        return cls("; ".join(state["error"] for state in states),
                   failure_class=failure_class)


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
        model = deepcopy(model)
        for field in ("model_call_budget_scopes", "model_call_budget_path",
                      "model_call_budget_key", "model_call_budget_limit"):
            model.pop(field, None)
        return hashlib.sha256(canonical_bytes({
            "schema": "model-work-1", "scope": scope, "role": role,
            "system": system, "prompt": prompt, "model": model,
        })).hexdigest()

    def get(self, key):
        record = self.store.head(f"{self.namespace}/{key}")
        if record is None:
            return None
        return self._verified_body(record)

    def _verified_body(self, record):
        raw = self.store.read_body(record["body_hash"])
        if hashlib.sha256(raw).hexdigest() != record["body_hash"]:
            raise ModelWorkProvenanceError("model work body differs from its immutable digest")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ModelWorkProvenanceError("model work body must be an object")
        return {**body, "cache_ref": record["artifact_ref"]}

    def inherited_usage(self, body):
        inheritance = body.get("usage_inheritance")
        if inheritance is None:
            return {}
        if not isinstance(inheritance, dict):
            raise ModelWorkProvenanceError("foundry usage inheritance requires an immutable source")
        record = self.store.get(inheritance["source_ref"])
        raw = self.store.read_body(record["body_hash"])
        if hashlib.sha256(raw).hexdigest() != record["body_hash"]:
            raise ModelWorkProvenanceError("foundry usage source differs from its immutable digest")
        return validate_foundry_usage_inheritance(body, record, json.loads(raw))

    def recovery_entry(self, body):
        receipt = body.get("inherited_request_history_reconciliation")
        if receipt is None:
            return body
        if not isinstance(receipt, dict) or receipt.get("excluded_from_candidate_recovery") is not True:
            raise ModelWorkProvenanceError("invalid inherited request recovery receipt")
        record = self.store.get(body["cache_ref"])
        prior_record = self.store.get(receipt["prior_ref"])
        source_record = self.store.get(receipt["source_ref"])
        if (record.get("author") != "command.controller"
                or prior_record.get("author") != "command.controller"
                or record.get("artifact_id") != prior_record.get("artifact_id")):
            raise ModelWorkProvenanceError("request recovery receipt has no immutable controller owner")
        def read(manifest):
            raw = self.store.read_body(manifest["body_hash"])
            if hashlib.sha256(raw).hexdigest() != manifest["body_hash"]:
                raise ModelWorkProvenanceError("request recovery body differs from its immutable digest")
            return json.loads(raw)
        captured = {key: value for key, value in body.items() if key != "cache_ref"}
        if read(record) != captured:
            raise ModelWorkProvenanceError("request recovery does not bind the captured controller body")
        prior, source = read(prior_record), read(source_record)
        expected = reconcile_inherited_request_outcomes(
            prior, source_record, source, target_ref=receipt["prior_ref"])
        if expected != captured:
            raise ModelWorkProvenanceError("request recovery receipt changed fields beyond request outcomes")
        return {**source, "cache_ref": source_record["artifact_ref"]}

    def entries(self):
        rows = self.store.control._conn.execute(
            "SELECT a.artifact_ref,a.body_hash FROM artifacts a JOIN "
            "(SELECT logical_id,MAX(version) version FROM artifacts WHERE logical_id LIKE ? GROUP BY logical_id) h "
            "ON a.logical_id=h.logical_id AND a.version=h.version ORDER BY a.created_at DESC",
            (self.namespace + "/%",))
        return [self._verified_body(dict(row)) for row in rows]

    def put(self, key, body, *, subjects=()):
        record = self.publish(f"{self.namespace}/{key}", "note", deepcopy(body),
                              "command.controller", subjects=subjects)
        return {**deepcopy(body), "cache_ref": record["artifact_ref"]}
