"""Preserve controller-produced solver fields through deterministic analysis."""
from copy import deepcopy
import hashlib

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


def solver_observation_manifest(software):
    """Bind complete selected outputs when a controller-only solver is used."""
    if not isinstance(software, dict):
        raise ValidationError("scientific software must be an object")
    laboratory = software.get("laboratory") or {}
    kinds = {row["label"]: row["kind"] for row in laboratory.get("runtimes", [])}
    expected = set(software.get("selection", {}).get("computation_refs", []))
    from scisaurus.runtime.software_workbench import selected_receipt_closure
    closure = selected_receipt_closure(software.get("operations", []), sorted(expected))
    runs = [row for row in closure if row.get("action", {}).get("operation") == "run"]
    if not any(kinds.get(row.get("action", {}).get("arguments", {}).get("runtime")) == "container" for row in runs):
        return None
    by_ref = {}
    for row in runs:
        ref, result = row.get("receipt_ref"), row.get("result", {})
        if (not isinstance(ref, str) or ref in by_ref
                or row.get("outcome") != "ok" or result.get("purpose") not in {"scientific_computation", "upstream_example"}
                or not isinstance(result.get("output"), dict) or not result["output"]):
            raise ValidationError("controller solver observations require unique selected successful computation receipts")
        source, raw_input = result.get("source"), result.get("input")
        arguments = row["action"].get("arguments", {})
        if (not isinstance(source, str) or not source.strip() or not isinstance(raw_input, dict)
                or arguments.get("source") != source or arguments.get("input") != raw_input
                or arguments.get("runtime") != result.get("runtime")
                or result.get("source_sha256") != hashlib.sha256(source.encode()).hexdigest()
                or result.get("input_sha256") != hashlib.sha256(canonical_bytes(raw_input)).hexdigest()):
            raise ValidationError("controller solver observation source or input identity is invalid")
        stdout = result.get("execution", {}).get("stdout")
        if not isinstance(stdout, str) or result.get("stdout_sha256") != hashlib.sha256(stdout.encode()).hexdigest():
            raise ValidationError("controller solver observation lost its complete stdout identity")
        try:
            from scisaurus.runtime.programs import _parse_object
            raw_output = _parse_object(stdout.encode())
        except (ValueError, ValidationError) as exc:
            raise ValidationError("controller solver observation stdout is not complete JSON") from exc
        if canonical_bytes(raw_output) != canonical_bytes(result["output"]):
            raise ValidationError("controller solver observation differs from its captured stdout")
        by_ref[ref] = {"source_record_id": ref, "runtime": result.get("runtime"),
                      "source_sha256": result["source_sha256"], "input_sha256": result["input_sha256"],
                      "output_sha256": hashlib.sha256(canonical_bytes(result["output"])).hexdigest(),
                      "source_values": deepcopy(result["output"])}
    if not expected.issubset(by_ref):
        raise ValidationError("controller solver observations omit selected computations")
    return {"schema_version": "solver-observations-1", "provenance": "synthetic_controller_computation",
            "dependencies": [{"receipt_ref": row["receipt_ref"],
                              "action_sha256": hashlib.sha256(canonical_bytes(row["action"])).hexdigest(),
                              "result_sha256": hashlib.sha256(canonical_bytes(row["result"])).hexdigest()}
                             for row in runs],
            "records": [by_ref[ref] for ref in sorted(expected)]}


def validate_solver_observations(candidate, configured_input):
    """Reject substitution, omission, or duplication of upstream raw fields."""
    if configured_input is None:
        return
    software = configured_input.get("scientific_software")
    if software is None:
        return
    expected = solver_observation_manifest(software)
    manifest = software.get("solver_observations")
    if expected is None:
        if manifest is not None:
            raise ValidationError("solver observation bundle has no controller-only computation")
        return
    if manifest != expected:
        raise ValidationError("solver observation bundle differs from its selected source receipts")
    rows = candidate.get("observations")
    originals = {row["source_record_id"]: row["source_values"] for row in expected["records"]}
    if not isinstance(rows, list) or len(rows) != len(originals):
        raise ValidationError("solver observations must preserve every selected output exactly once")
    seen = set()
    for row in rows:
        ref = row.get("source_record_id") if isinstance(row, dict) else None
        if (not isinstance(ref, str) or ref not in originals or ref in seen
                or set(row) - {"source_record_id", "source_values", "condition", "replicate"}
                or canonical_bytes(row.get("source_values")) != canonical_bytes(originals[ref])):
            raise ValidationError("solver observation must retain its exact receipt identity and complete raw fields")
        if "condition" in row and (not isinstance(row["condition"], str) or not row["condition"].strip()):
            raise ValidationError("solver observation condition must be nonempty design metadata")
        if "replicate" in row and (type(row["replicate"]) is not int or row["replicate"] < 1):
            raise ValidationError("solver observation replicate must be a positive integer")
        seen.add(ref)
