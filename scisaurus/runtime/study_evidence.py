"""Prospective evidence obligations bound to a frozen computational study."""
from copy import deepcopy

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.scores import exact, identifier


EVIDENCE_KINDS = frozenset({
    "baseline_reproduction", "uncertainty", "control", "sensitivity", "external_validation",
})
PLAN_FIELDS = frozenset({
    "id", "kind", "status", "metric_ids", "condition_ids", "source_refs",
    "validator_check_id", "method", "acceptance_rule", "claim_limit",
})


def study_evidence_contract():
    return {
        "revision": "computational-study-evidence-1",
        "plan_field": "experiment_intent.evidence_plan",
        "entry_shape": {
            "id": "unique bounded lowercase identifier",
            "kind": "baseline_reproduction | uncertainty | control | sensitivity | external_validation",
            "status": "planned | not_applicable",
            "metric_ids": ["exact declared primary or decision outcome id"],
            "condition_ids": ["exact observation.condition labels; [] for an unstratified analysis"],
            "source_refs": ["exact acquired source or software receipt identifier"],
            "validator_check_id": "unique deterministic validator check id, or null for not_applicable",
            "method": "reproducible procedure, or the scientific reason this obligation does not apply",
            "acceptance_rule": "outcome-independent falsifiable criterion, or why none is applicable",
            "claim_limit": "scope retained even after numerical verification",
        },
        "rules": [
            "Cover all five evidence kinds; multiple entries per kind are allowed. The agent chooses the methods and criteria from the admitted question and acquired evidence.",
            "Start with a source-bound baseline: reproduce published code/data or a source-bound analytical benchmark before interpreting a new contrast. An installation, launch check or copied expected answer is not baseline reproduction.",
            "Plan uncertainty from actual measurement, parameter, sampling or numerical error sources. Grid nodes and deterministic replay are not independent samples; resampling must preserve the declared sampling unit and dependence.",
            "Controls and sensitivity must produce actual comparison observations and recompute the same decision metric. Document mathematical identities and imposed effects as model-internal consequences, not independent discoveries.",
            "External validation uses acquired observations with compatible species, units, reference scales and scope. Missing suitable observations may justify not_applicable with a bounded synthetic claim, never invented data or automatic empirical validity.",
            "A planned entry names existing outcomes and a unique validator check. Emit the corresponding observations with condition labels and independently verify its acceptance rule. Text in analysis summaries does not prove execution.",
            "Declare numerical contrasts, uncertainty bounds or detection thresholds used to decide a claim as primary or decision outcomes with exact aggregation, units and decision rules. Independent validation must recalculate those quantities; an undeclared narrative number cannot decide robustness.",
            "For not_applicable, metric_ids and condition_ids are empty and validator_check_id is null. Preserve method and claim_limit in final limitations. This disposition is a limitation, not a completed quantitative analysis.",
            "Pre-execution review assesses whether this plan is executable and scientifically appropriate; it does not require the future results. Post-execution review assesses the current raw data, checks, uncertainty and claim scope.",
            "Preserve accepted baseline/data/receipt evidence and reuse it when source, inputs and scope are unchanged. A repair must identify the material design/source change and the fresh execution that tests it; unchanged reviews or metadata edits are not experimental progress.",
        ],
    }


def evidence_source_refs(configured_input):
    """Read the source catalog already bound to the controller-owned input."""
    if not isinstance(configured_input, dict):
        raise ValidationError("study evidence configured_input must be an object")
    refs = set()
    software = configured_input.get("scientific_software", {})
    if not isinstance(software, dict):
        raise ValidationError("study evidence scientific_software must be an object")
    selection = software.get("selection", {})
    if not isinstance(selection, dict):
        raise ValidationError("study evidence software selection must be an object")
    sources = selection.get("scientific_source_refs", [])
    if (not isinstance(sources, list)
            or any(not isinstance(ref, str) or not ref.strip() for ref in sources)):
        raise ValidationError("study evidence scientific_source_refs must be nonempty source strings")
    refs.update(sources)
    for field in ("operations", "host_environment_checks"):
        rows = software.get(field, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValidationError("study evidence software receipts must be object lists")
        for row in rows:
            if row.get("outcome") == "ok" and isinstance(row.get("receipt_ref"), str) and row["receipt_ref"].strip():
                refs.add(row["receipt_ref"])
    manifest = configured_input.get("source_data_manifest", {})
    if not isinstance(manifest, dict):
        raise ValidationError("study evidence source_data_manifest must be an object")
    datasets = manifest.get("datasets", [])
    if (not isinstance(datasets, list) or any(not isinstance(row, dict)
            or not isinstance(row.get("artifact_ref"), str) or not row["artifact_ref"].strip() for row in datasets)):
        raise ValidationError("study evidence datasets require recorded artifact references")
    refs.update(row["artifact_ref"] for row in datasets)
    return sorted(refs)


def validate_evidence_plan(intent, *, required=False, source_refs=None):
    plan = intent.get("evidence_plan")
    if plan is None:
        if required:
            raise ValidationError("new computational study requires experiment_intent.evidence_plan")
        return []
    if not isinstance(plan, list) or not plan:
        raise ValidationError("evidence_plan must be a nonempty list")
    outcomes = {row["id"] for row in [*intent.get("primary_outcomes", []),
                                       *intent.get("decision_outcomes", [])]}
    ids, checks, kinds = set(), set(), set()
    for entry in plan:
        exact(entry, PLAN_FIELDS, "study evidence obligation")
        identifier(entry["id"])
        if entry["id"] in ids:
            raise ValidationError("evidence_plan obligation ids must be unique")
        ids.add(entry["id"])
        if not isinstance(entry["kind"], str) or entry["kind"] not in EVIDENCE_KINDS:
            raise ValidationError("evidence_plan kind is invalid")
        kinds.add(entry["kind"])
        if entry["status"] not in ("planned", "not_applicable"):
            raise ValidationError("evidence_plan status is invalid")
        for field in ("method", "acceptance_rule", "claim_limit"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise ValidationError(f"evidence_plan.{field} must be nonempty text")
        for field in ("metric_ids", "condition_ids", "source_refs"):
            values = entry[field]
            if (not isinstance(values, list)
                    or any(not isinstance(value, str) or not value.strip() for value in values)
                    or len(values) != len(set(values))):
                raise ValidationError(f"evidence_plan.{field} requires unique nonempty strings")
        if source_refs is not None and set(entry["source_refs"]) - set(source_refs):
            raise ValidationError("evidence_plan source_refs are absent from the current acquired source catalog")
        if entry["status"] == "not_applicable":
            if entry["metric_ids"] or entry["condition_ids"] or entry["validator_check_id"] is not None:
                raise ValidationError("not_applicable evidence must not claim measurements or validator checks")
            continue
        if not entry["metric_ids"] or set(entry["metric_ids"]) - outcomes:
            raise ValidationError("planned evidence must reference declared outcomes")
        identifier(entry["validator_check_id"])
        if entry["validator_check_id"] in checks:
            raise ValidationError("planned evidence requires distinct validator check ids")
        checks.add(entry["validator_check_id"])
        if entry["kind"] in {"baseline_reproduction", "external_validation"} and not entry["source_refs"]:
            raise ValidationError("baseline and external validation require recorded source references")
    if kinds != EVIDENCE_KINDS:
        raise ValidationError("evidence_plan must cover every evidence kind; missing="
                              + ",".join(sorted(EVIDENCE_KINDS - kinds)))
    return deepcopy(plan)


def validate_evidence_checks(intent, verdict):
    plan = validate_evidence_plan(intent)
    checks = {row["id"] for row in verdict["checks"]}
    missing = [entry["validator_check_id"] for entry in plan
               if entry["status"] == "planned" and entry["validator_check_id"] not in checks]
    if missing:
        raise ValidationError("independent validator omitted planned evidence checks: " + ",".join(missing))


def bind_evidence_observations(intent, candidate, verdict):
    """Check observable coverage without substituting for scientific review."""
    plan = validate_evidence_plan(intent)
    if not plan:
        return
    validate_evidence_checks(intent, verdict)
    conditions = {row["condition"] for row in candidate["observations"]
                  if isinstance(row.get("condition"), str)}
    for entry in plan:
        if entry["claim_limit"] not in candidate["limitations"]:
            raise ValidationError(f"evidence obligation {entry['id']} lost its claim limitation")
        if entry["status"] == "planned":
            missing = set(entry["condition_ids"]) - conditions
            if missing:
                raise ValidationError(f"evidence obligation {entry['id']} has no observation rows for "
                                      + ",".join(sorted(missing)))
        else:
            if entry["method"] not in candidate["limitations"]:
                raise ValidationError(f"unperformed evidence obligation {entry['id']} lost its non-applicability reason")
