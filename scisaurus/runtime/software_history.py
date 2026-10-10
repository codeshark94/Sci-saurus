"""Project-owned controller observations for subsequent engineering work."""
from copy import deepcopy
import hashlib

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes, parse_ref
from scisaurus.runtime.software_workbench import receipt_catalog_entry

REVISION = "software-engineering-history-1"


def engineering_history(store, read_verified, workbench, *, project_id, stage_id, topic):
    """Resolve sealed operation receipts without inheriting a scientific verdict.

    A producer with an unknown final response can still own completed controller
    operations. Their individual receipt outcomes remain independent of the
    producer outcome; neither is changed or settled here.
    """
    refs = store.control._conn.execute(
        "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE ? "
        "AND (logical_id LIKE '%/receipt' OR logical_id LIKE '%/failure') "
        "ORDER BY logical_id, version",
        ("command/scientific-software-assessments/%",),
    ).fetchall()
    operations, owners, assessments = {}, {}, []
    keys = ("id", "research_question", "domain")
    for (ref,) in refs:
        manifest, _, retained = read_verified(ref)
        identity = retained.get("identity", {})
        prior_topic = identity.get("topic", {})
        if any(prior_topic.get(key) != topic.get(key) for key in keys):
            continue
        if retained.get("status") not in {"accepted", "blocked"}:
            raise ValidationError("software history assessment has an unsupported outcome")
        digest = hashlib.sha256(canonical_bytes(identity)).hexdigest()
        namespace, name, _ = parse_ref(ref)
        if (manifest.get("author") != "command.composer"
                or namespace + "/" + name != "command/scientific-software-assessments/" + digest + "/" +
                   ("receipt" if retained.get("status") == "accepted" else "failure")):
            raise ValidationError("software history assessment lost its immutable identity")
        producer_ref = retained.get("producer_execution_ref")
        _, _, producer = read_verified(producer_ref)
        binding = producer.get("input_ref", {})
        if (producer.get("project_id") != project_id
                or binding.get("kind") != "scientific_software_assessment"
                or binding.get("stage_id") != stage_id or binding.get("digest") != digest):
            raise ValidationError("software history producer belongs to another assignment")
        report = producer.get("report", {})
        if report.get("status") not in {"succeeded", "failed", "result_unknown"}:
            raise ValidationError("software history producer has no recorded terminal outcome")
        rows = report.get("software_tool_results", [])
        workbench.validate_retained_results(rows, verify_execution_state=False)
        for row in rows:
            operation_ref = row["receipt_ref"]
            sealed = {key: deepcopy(value) for key, value in row.items() if key != "reused"}
            if operation_ref in operations and operations[operation_ref] != sealed:
                raise ValidationError("software history operation has conflicting receipts")
            operations[operation_ref] = sealed
            owners.setdefault(operation_ref, set()).add(producer_ref)
        assessments.append({"assessment_ref": ref, "producer_execution_ref": producer_ref,
                            "assessment_status": retained["status"], "producer_status": report["status"],
                            "receipt_refs": sorted(row["receipt_ref"] for row in rows)})
    return {"revision": REVISION, "assessments": assessments,
            "operations": [{**receipt_catalog_entry(operations[ref]),
                            "producer_execution_refs": sorted(owners[ref])} for ref in sorted(operations)],
            "interpretation": "Historical controller observations, not current candidate admission. "
                "Read the sealed source/input/raw or error with read_receipt before claiming it is missing. "
                "A producer result_unknown remains unresolved. Selection revalidates current runtime and "
                "artifact state; changed scientific inputs require a fresh solver action."}
