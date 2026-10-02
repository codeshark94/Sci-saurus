"""Availability of materialized scientific inputs, distinct from proposed inputs."""
import hashlib
import json
import re

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


def scientific_topic_sha256(topic):
    """Bind inputs to the exact topic, excluding self-referential artifact links."""
    identity = dict(topic)
    if isinstance(identity.get("feasibility_plan"), dict):
        identity["feasibility_plan"] = {**identity["feasibility_plan"], "evidence_inputs": [
            {key: value for key, value in item.items() if key != "artifact_refs"}
            for item in identity["feasibility_plan"].get("evidence_inputs", [])]}
    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def input_artifact(store, ref):
    from scisaurus.runtime.topic_discovery import FEASIBILITY_INPUT_KINDS
    record = store.get(ref)
    raw = store.read_body(record["body_hash"])
    if hashlib.sha256(raw).hexdigest() != record["body_hash"]:
        raise ValidationError("scientific input artifact body hash mismatch")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("schema_version") != "scientific-input-1"
            or set(value) != {"schema_version", "topic_id", "topic_sha256", "kind", "payload", "source_refs"}
            or not isinstance(value["topic_id"], str) or not value["topic_id"]
            or not isinstance(value["topic_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", value["topic_sha256"]) is None
            or not isinstance(value["payload"], dict) or not value["payload"]
            or value.get("kind") not in FEASIBILITY_INPUT_KINDS
            or not isinstance(value["source_refs"], list)
            or any(not isinstance(item, str) for item in value["source_refs"])
            or len(set(value["source_refs"])) != len(value["source_refs"])
            or (value["kind"] != "synthetic" and not value["source_refs"])):
        raise ValidationError("scientific input requires a materialized payload and explicit provenance")
    if not set(value["source_refs"]) <= {item["ref"] for item in record["inputs"]}:
        raise ValidationError("scientific input sources must be immutable artifact dependencies")
    for source in value["source_refs"]:
        parent = store.get(source)
        data = store.read_body(parent["body_hash"])
        if hashlib.sha256(data).hexdigest() != parent["body_hash"]:
            raise ValidationError("scientific input source body hash mismatch")
    return {"artifact_ref": ref, "body_sha256": record["body_hash"], **value}


def input_readiness(topic, inventory):
    """A model's availability declaration cannot establish materialized input readiness."""
    artifacts = {item["artifact_ref"]: item for item in inventory}
    rows = []
    for item in topic["feasibility_plan"]["evidence_inputs"]:
        refs = item.get("artifact_refs", [])
        checked = [artifacts[ref] for ref in refs if ref in artifacts
                   and artifacts[ref]["topic_id"] == topic["id"] and artifacts[ref]["kind"] == item["kind"]
                   and artifacts[ref]["topic_sha256"] == scientific_topic_sha256(topic)]
        verified = bool(refs) and len(checked) == len(refs)
        rows.append({"kind": item["kind"], "declared_status": item["status"],
                     "status": "verified" if verified else "generation_required" if item["kind"] == "synthetic" else "unverified",
                     "artifact_refs": refs,
                     "artifact_hashes": {x["artifact_ref"]: x["body_sha256"] for x in checked}})
    canonical_bytes(rows)
    return rows
