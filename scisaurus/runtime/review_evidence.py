"""Lossless transport encodings for complete scientific review evidence."""
import hashlib
import base64
from copy import deepcopy

from scisaurus.core.schema import canonical_bytes
from scisaurus.core.errors import ValidationError


REALIZATION_REVIEW_RULE = (
    "Independently compare requested constraints with the constructed geometry and actual "
    "solver material assignments, domain measures, normalization and material budgets. "
    "A reported realized value is not a measurement merely because it repeats the input or "
    "a geometry formula. Inspect the construction and measurement code separately; account "
    "for overlaps, boundaries, discretization and unequal cell or quadrature weights. "
    "Design names and equal requested parameters do not prove equivalent physical geometry "
    "across solvers. Use receipt-bound phase fields, masks, region measures or equivalent "
    "independent geometry evidence when needed. Check all relevant solver case errors even "
    "when the process exits successfully. Treat homogeneous calibration controls separately "
    "from equal-constraint candidate comparisons. Missing or inconsistent realization "
    "evidence invalidates the dependent comparison, not the fact that the software ran. "
    "Record it in the existing method, calculation or claim check and require the specific "
    "measurement, source correction and fresh affected solve before supporting that claim; "
    "do not require a full optimization or final robustness study for the first pilot."
)


def review_execution_evidence(records, expected_input, candidate, *, normalize_output):
    """Bind review sources and input to captured execution bytes, never workspace files."""
    from scisaurus.runtime.programs import _parse_object

    def capture(value):
        if not isinstance(value, dict) or value.get("encoding") != "base64":
            raise ValidationError("execution review requires a complete byte capture")
        try:
            body = base64.b64decode(value["body"], validate=True)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError("execution review byte capture is invalid") from exc
        if (hashlib.sha256(body).hexdigest() != value.get("sha256")
                or len(body) != value.get("bytes")):
            raise ValidationError("execution review capture differs from its digest or length")
        return body

    executions = []
    for ref, result in records:
        metadata = result.get("metadata", {})
        if (result.get("outcome") != "ok" or metadata.get("process_returncode") != 0
                or metadata.get("capture_truncated") is not False
                or metadata.get("capture_incomplete") is not False):
            raise ValidationError("execution review requires a complete successful program receipt")
        stdin, stdout = capture(result.get("input_capture")), capture(result.get("capture"))
        if (stdin != canonical_bytes(expected_input)
                or canonical_bytes(result.get("input")) != stdin
                or result.get("input_sha256") != hashlib.sha256(stdin).hexdigest()):
            raise ValidationError("execution review input differs from the frozen study input")
        document = _parse_object(stdout)
        if (result.get("capture_sha256") != hashlib.sha256(stdout).hexdigest()
                or canonical_bytes(document) != canonical_bytes(result.get("document"))
                or canonical_bytes(normalize_output(deepcopy(document))) != canonical_bytes(candidate)):
            raise ValidationError("execution review output differs from the current candidate")
        identity = metadata.get("command_identity", {})
        details = identity.get("details", {})
        if hashlib.sha256(canonical_bytes(details)).hexdigest() != identity.get("sha256"):
            raise ValidationError("execution review command identity is invalid")
        sources = []
        for source in details.get("source_files", []):
            body = capture(source.get("capture"))
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError("execution review source is not UTF-8") from exc
            sources.append({"path": source["path"], "sha256": hashlib.sha256(body).hexdigest(),
                            "source": text})
        if metadata.get("sandbox_required") is True and (
                not sources or metadata.get("source_dispatch_mode") != "private_read_only_snapshot"):
            raise ValidationError("execution review has no executed immutable source snapshot")
        executions.append({"execution_ref": ref, "command_identity_sha256": identity["sha256"],
                           "input_sha256": hashlib.sha256(stdin).hexdigest(),
                           "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                           "source_files": sources, "source_available": bool(sources),
                           "sandbox_mode": metadata.get("sandbox_mode")})
    if not executions:
        raise ValidationError("execution review requires current execution receipts")
    return {"schema_version": "executed-study-review-1",
            "candidate_sha256": hashlib.sha256(canonical_bytes(candidate)).hexdigest(),
            "configured_input": deepcopy(expected_input["configured_input"]),
            "configured_input_sha256": hashlib.sha256(
                canonical_bytes(expected_input["configured_input"])).hexdigest(),
            "executions": executions, "realization_review_rule": REALIZATION_REVIEW_RULE}


def review_observation_table(observations):
    """Encode every observation without repeating column names or losing missing keys."""
    schemas, schema_ids, rows = [], [], []
    for observation in observations:
        fields = sorted(observation)
        if fields not in schemas:
            schemas.append(fields)
        schema_ids.append(schemas.index(fields))
        rows.append([observation[field] for field in fields])
    table = {"encoding": "observation-table-1", "complete": True,
             "row_count": len(rows), "schemas": schemas, "rows": rows,
             "observations_sha256": hashlib.sha256(canonical_bytes(observations)).hexdigest(),
             "decoding": "For row i, zip schemas[schema_ids[i]] with rows[i] to reconstruct the exact observation object. "
                         "When schema_ids is absent use schema 0 for every row. Row order and all values are preserved; "
                         "fields absent from a schema are missing, not null."}
    if len(schemas) > 1:
        table["schema_ids"] = schema_ids
    return table

