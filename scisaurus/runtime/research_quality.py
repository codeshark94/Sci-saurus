"""Reusable quality contracts for substantive computational research.

The experiment runner can verify that a program was replayable without knowing
whether the study was scientifically informative.  This module keeps that
second question explicit and configurable.  A quality contract is frozen with
the experiment; the program must then emit the corresponding analysis ledger
before the result can enter a research-paper pipeline.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import re

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


QUALITY_CONTRACT_FIELDS = {
    "minimum_conditions", "minimum_independent_seeds", "minimum_controls",
    "minimum_comparisons", "required_analyses", "minimum_figures",
}
ANALYSIS_FIELDS = {
    "conditions", "independent_seeds", "controls", "comparisons",
    "uncertainty", "effect_sizes", "sensitivity", "ablation", "raw_data",
}
ANALYSIS_KINDS = {"uncertainty", "effect_size", "sensitivity", "ablation", "raw_data"}


class AnalysisContractError(ValidationError):
    """A generated analysis summary violated its declared output schema."""


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value.strip()


def _analysis_strings(value, name):
    """Canonicalize blank or repeated display entries as absent evidence."""
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be a string list")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError(f"{name} must contain only strings")
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return result


def _validate_quantitative_evidence(record, name, *, metric_ids=None):
    """Distinguish unavailable analysis quantities from finite computed estimates."""
    numeric_fields = {"mean", "estimate", "lower", "upper"} & record.keys()
    unavailable = record.get("status") == "not_estimable"
    if unavailable:
        _text(record.get("reason"), f"{name}.reason")
        references = record.get("metric_ids")
        if (not isinstance(references, list) or not references
                or any(not isinstance(item, str) for item in references)):
            raise ValidationError(f"{name}.metric_ids must name emitted metrics")
        for reference in references:
            _identifier(reference, f"{name}.metric_ids")
        if len(references) != len(set(references)):
            raise ValidationError(f"{name}.metric_ids must be unique")
        if metric_ids is not None and set(references) - set(metric_ids):
            raise ValidationError(f"{name}.metric_ids reference unknown emitted metrics")
        if not {"mean", "estimate"} & numeric_fields:
            raise ValidationError(f"{name} not_estimable requires a null mean or estimate")
        if any(record[field] is not None for field in numeric_fields):
            raise ValidationError(f"{name} not_estimable numeric fields must be null")
    else:
        for field in numeric_fields:
            value = record[field]
            if (type(value) not in (int, float)
                    or (type(value) is float and not math.isfinite(value))):
                raise ValidationError(
                    f"{name}.{field} must be a finite number; unavailable quantities require "
                    "status=not_estimable, reason, metric_ids, and null numeric fields")
    has_lower = "lower" in record
    has_upper = "upper" in record
    if has_lower != has_upper:
        raise ValidationError(f"{name} interval requires both lower and upper")
    if has_lower and not ({"mean", "estimate"} & record.keys()):
        raise ValidationError(f"{name} interval requires a machine-readable mean or estimate")
    if has_lower and not unavailable and record["lower"] > record["upper"]:
        raise ValidationError(f"{name} lower bound must not exceed upper bound")


def _analysis_evidence(value, name, *, records=True, metric_ids=None):
    """Preserve concise evidence entries while enforcing a shared record shape."""
    if not isinstance(value, list):
        raise ValidationError(f"{name} must be a list")
    result = []
    seen = set()
    identifiers = set()
    for item in value:
        if isinstance(item, str):
            normalized = item.strip()
            if not normalized:
                continue
            identity = canonical_bytes(normalized)
        elif records and isinstance(item, dict):
            if not {"id", "description"}.issubset(item):
                raise ValidationError(
                    f"{name} evidence records require id and description")
            _identifier(item["id"], f"{name} evidence id")
            _text(item["description"], f"{name} evidence description")
            normalized = deepcopy(item)
            if name in {"analysis.uncertainty", "analysis.effect_sizes", "analysis.sensitivity", "analysis.ablation"}:
                _validate_quantitative_evidence(normalized, name, metric_ids=metric_ids)
            identity = canonical_bytes(normalized)
            if normalized["id"] in identifiers:
                raise ValidationError(f"{name} evidence IDs must be unique")
            identifiers.add(normalized["id"])
        else:
            expected = "strings or {id,description} records" if records else "strings"
            raise ValidationError(f"{name} must contain only {expected}")
        if identity not in seen:
            result.append(normalized)
            seen.add(identity)
    return result


def analysis_output_contract():
    """Describe the exact authoring shapes enforced by :func:`validate_analysis`."""
    return {
        "conditions": "list of nonempty strings naming observed conditions",
        "independent_seeds": "list of unique nonnegative integers actually executed",
        "controls": "list of nonempty strings or {id,description} evidence records",
        "comparisons": (
            "list of nonempty strings or {id,description} evidence records; strings are "
            "normalized to records and additional JSON evidence fields are preserved"
        ),
        "uncertainty": (
            "list of nonempty strings or {id,description} records; additional JSON evidence fields are preserved. "
            "For a numeric estimate or interval, include finite machine-readable mean or estimate, lower, and upper "
            "values in the same evidence record; lower must not exceed upper. "
            "Unavailable quantities use status=not_estimable, nonempty reason, unique metric_ids "
            "naming emitted metrics, and null mean or estimate; any lower/upper must both be null. "
            "These records preserve unresolved evidence and do not satisfy a quantitative analysis floor"
        ),
        "effect_sizes": (
            "list of nonempty strings or {id,description} records; additional JSON evidence fields are preserved. "
            "Any mean/estimate/lower/upper follow the uncertainty numeric or not_estimable contract"
        ),
        "sensitivity": (
            "list of nonempty strings or {id,description} records; additional JSON evidence fields are preserved. "
            "Any mean/estimate/lower/upper follow the uncertainty numeric or not_estimable contract"
        ),
        "ablation": (
            "list of nonempty strings or {id,description} records; additional JSON evidence fields are preserved. "
            "Any mean/estimate/lower/upper follow the uncertainty numeric or not_estimable contract"
        ),
        "raw_data": (
            "list of nonempty strings or {id,description} records identifying emitted observations"
        ),
    }


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def validate_quality_contract(value, *, study_type=None):
    """Validate the declared substantive-analysis floor.

    The contract is intentionally domain-neutral.  A deterministic numerical
    study may use one seed, while a stochastic study can declare several; the
    program, rather than this module, supplies the scientific definitions.
    """
    if not isinstance(value, dict) or set(value) != QUALITY_CONTRACT_FIELDS:
        raise ValidationError(
            f"quality_contract requires exactly {sorted(QUALITY_CONTRACT_FIELDS)}")
    for key in ("minimum_conditions", "minimum_independent_seeds", "minimum_controls",
                "minimum_comparisons", "minimum_figures"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValidationError(f"quality_contract.{key} must be a nonnegative integer")
    if value["minimum_conditions"] < 1:
        raise ValidationError("quality_contract.minimum_conditions must be positive")
    if value["minimum_independent_seeds"] < 1:
        raise ValidationError("quality_contract.minimum_independent_seeds must be positive")
    required = value["required_analyses"]
    if (not isinstance(required, list) or not required
            or len(required) != len(set(required))
            or set(required) - ANALYSIS_KINDS):
        raise ValidationError("quality_contract.required_analyses is invalid")
    if study_type == "novel_research":
        minimums = {
            "minimum_conditions": 2,
            "minimum_controls": 1,
            "minimum_comparisons": 2,
            "minimum_figures": 3,
        }
        for key, floor in minimums.items():
            if value[key] < floor:
                raise ValidationError(
                    f"novel_research quality_contract.{key} must be at least {floor}")
        required_floor = {"uncertainty", "effect_size", "sensitivity", "raw_data"}
        if not required_floor.issubset(required):
            raise ValidationError(
                "novel_research quality_contract must require uncertainty, effect_size, "
                "sensitivity, and raw_data")
    canonical_bytes(value)
    return deepcopy(value)


def default_research_quality_contract():
    """Return the default floor used when a journal paper has no explicit one."""
    return {
        "minimum_conditions": 2,
        "minimum_independent_seeds": 1,
        "minimum_controls": 1,
        "minimum_comparisons": 2,
        "required_analyses": ["uncertainty", "effect_size", "sensitivity", "raw_data"],
        "minimum_figures": 3,
    }


def ensure_minimum_quality_contract(value=None, *, study_type=None, minimum=None):
    """Return a contract that preserves local requirements and meets a floor.

    An experiment may declare stricter requirements than the journal default,
    but a downstream research-paper consumer must not inherit a weaker
    contract merely because an older descriptor already contained one.  This
    helper performs that monotone upgrade before execution and keeps every
    caller-supplied analysis requirement intact.
    """
    floor = default_research_quality_contract() if minimum is None else deepcopy(minimum)
    validate_quality_contract(floor)
    current = {} if value is None else validate_quality_contract(value, study_type=study_type)
    merged = {
        "minimum_conditions": max(current.get("minimum_conditions", 0), floor["minimum_conditions"]),
        "minimum_independent_seeds": max(current.get("minimum_independent_seeds", 0), floor["minimum_independent_seeds"]),
        "minimum_controls": max(current.get("minimum_controls", 0), floor["minimum_controls"]),
        "minimum_comparisons": max(current.get("minimum_comparisons", 0), floor["minimum_comparisons"]),
        "required_analyses": list(dict.fromkeys([
            *current.get("required_analyses", []), *floor["required_analyses"]
        ])),
        "minimum_figures": max(current.get("minimum_figures", 0), floor["minimum_figures"]),
    }
    return validate_quality_contract(merged, study_type=study_type)


def build_research_design(experiment):
    """Materialize the pre-analysis plan that is frozen before execution."""
    design = {
        "schema_version": "research-design-1",
        "experiment_id": experiment["id"],
        "revision": experiment["revision"],
        "study_type": experiment["study_type"],
        "question": experiment["research_question"],
        "hypothesis": experiment["hypothesis"],
        "method": experiment["method"],
        "parameters": deepcopy(experiment["parameters"]),
        "seed": experiment["seed"],
        "run_count": experiment["run_count"],
        "stopping_rule": experiment["stopping_rule"],
        "primary_outcomes": deepcopy(experiment["primary_outcomes"]),
        "limitations": list(experiment["limitations"]),
        "quality_contract": deepcopy(experiment.get("quality_contract")),
    }
    from scisaurus.runtime.measurement_contract import INTENT_EXTENSIONS
    for field in sorted(INTENT_EXTENSIONS & experiment.keys()):
        design[field] = deepcopy(experiment[field])
    canonical_bytes(design)
    return design


def validate_analysis(value, *, metric_ids=None):
    """Validate the program's reader-facing analysis summary."""
    if not isinstance(value, dict):
        raise AnalysisContractError("analysis must be an object")
    unexpected = set(value) - ANALYSIS_FIELDS
    if unexpected:
        raise AnalysisContractError(
            f"analysis contains unknown fields: {sorted(unexpected)}")

    try:
        # This summary is evidence supplied by the author, not the experiment's
        # execution contract. Missing entries therefore mean "not reported" and
        # must become explicit empty values so quality admission can issue scoped
        # research work orders instead of rejecting otherwise replayable output.
        value = {field: deepcopy(value.get(field, [])) for field in ANALYSIS_FIELDS}
        value["conditions"] = _analysis_strings(value["conditions"], "analysis.conditions")
        for key in ("controls", "uncertainty", "effect_sizes", "sensitivity",
                    "ablation", "raw_data"):
            value[key] = _analysis_evidence(value[key], f"analysis.{key}", metric_ids=metric_ids)
        seeds = value["independent_seeds"]
        if (not isinstance(seeds, list)
                or any(type(seed) is not int or seed < 0 for seed in seeds)):
            raise ValidationError("analysis.independent_seeds must be nonnegative integers")
        value["independent_seeds"] = list(dict.fromkeys(seeds))
        comparisons = value["comparisons"]
        if not isinstance(comparisons, list):
            raise ValidationError("analysis.comparisons must be a list")
        comparison_records = _analysis_evidence(
            comparisons, "analysis.comparisons", records=True)
        normalized_comparisons = []
        comparison_ids = set()
        for item in comparison_records:
            if isinstance(item, str):
                description = item.strip()
                digest = hashlib.sha256(description.encode("utf-8")).hexdigest()[:16]
                item = {"id": f"comparison-{digest}", "description": description}
            if item["id"] in comparison_ids:
                raise ValidationError("analysis.comparisons evidence IDs must be unique")
            comparison_ids.add(item["id"])
            normalized_comparisons.append(item)
        value["comparisons"] = normalized_comparisons
        canonical_bytes(value)
        return deepcopy(value)
    except AnalysisContractError:
        raise
    except ValidationError as exc:
        raise AnalysisContractError(str(exc)) from exc


def check_analysis_contract(analysis, contract, *, figure_count=0):
    """Return scientific-work deficits without turning them into prose claims."""
    validate_quality_contract(contract)
    analysis = validate_analysis(analysis)
    deficits = []
    checks = (
        ("conditions", len(analysis["conditions"]), contract["minimum_conditions"]),
        ("independent_seeds", len(analysis["independent_seeds"]), contract["minimum_independent_seeds"]),
        ("controls", len(analysis["controls"]), contract["minimum_controls"]),
        ("comparisons", len(analysis["comparisons"]), contract["minimum_comparisons"]),
        ("figures", int(figure_count), contract["minimum_figures"]),
    )
    for name, observed, required in checks:
        if observed < required:
            deficits.append({"field": name, "observed": observed, "required": required})
    analysis_by_requirement = {
        "uncertainty": "uncertainty",
        "effect_size": "effect_sizes",
        "sensitivity": "sensitivity",
        "ablation": "ablation",
        "raw_data": "raw_data",
    }
    for requirement in contract["required_analyses"]:
        field = analysis_by_requirement[requirement]
        available = [item for item in analysis[field]
                     if not (isinstance(item, dict) and item.get("status") == "not_estimable")]
        if not available:
            deficit = {"field": requirement, "observed": 0, "required": 1}
            if analysis[field]:
                deficit["unresolved_evidence_ids"] = [item["id"] for item in analysis[field]]
            deficits.append(deficit)
    return deficits


def evaluate_result_package_quality(results, *, minimum_contract=None):
    """Create a bounded admission decision for a result package.

    ``minimum_contract`` is supplied by publication gates so an older or
    locally weaker experiment contract cannot lower the journal floor.
    """
    contract = results.get("quality_contract") if isinstance(results, dict) else None
    if contract is None:
        return {
            "schema_version": "research-quality-admission-1",
            "decision": "research_expansion_required",
            "contract": None,
            "observed": {},
            "deficits": [{"field": "quality_contract", "observed": 0, "required": 1}],
            "expansion_requests": [{
                "id": "upgrade_experiment_design",
                "kind": "additional_experiment",
                "owner": "methods.validation",
                "objective": "Re-run the study with a declared condition, control, uncertainty, effect-size, sensitivity, and display plan.",
                "why": "A replayable result package does not by itself establish that the experiment examined competing explanations or quantified its uncertainty.",
                "success_condition": "The result package carries a validated quality contract and an analysis summary satisfying every declared floor.",
                "evidence_needed": "A frozen design record, raw observations, analysis summary, independent recalculation, and claim-linked figures.",
            }],
        }
    deficits = []
    try:
        validate_quality_contract(contract)
        if minimum_contract is not None:
            validate_quality_contract(minimum_contract)
            for field in ("minimum_conditions", "minimum_independent_seeds", "minimum_controls",
                          "minimum_comparisons", "minimum_figures"):
                if contract[field] < minimum_contract[field]:
                    deficits.append({"field": f"quality_contract.{field}",
                                     "observed": contract[field],
                                     "required": minimum_contract[field]})
            missing_requirements = sorted(set(minimum_contract["required_analyses"])
                                          - set(contract["required_analyses"]))
            if missing_requirements:
                deficits.append({"field": "quality_contract.required_analyses",
                                 "observed": contract["required_analyses"],
                                 "required": minimum_contract["required_analyses"],
                                 "missing": missing_requirements})
        validation = results.get("validation")
        if isinstance(validation, dict) and validation.get("decision") == "rejected":
            deficits.append({"field": "validation.decision", "observed": "rejected",
                             "required": ["accepted", "accepted_with_limitations"]})
        analysis = results.get("analysis")
        if analysis is None:
            raise ValidationError("result package requires analysis for its quality contract")
        figures = sum(1 for asset in results.get("assets", [])
                      if isinstance(asset, dict) and asset.get("role") == "figure")
        deficits.extend(check_analysis_contract(analysis, contract, figure_count=figures))
    except ValidationError as exc:
        if not deficits:
            deficits = [{"field": "analysis", "observed": 0, "required": 1, "reason": str(exc)}]
        else:
            deficits.append({"field": "analysis", "observed": 0, "required": 1, "reason": str(exc)})
    requests = []
    for deficit in deficits:
        field = deficit["field"]
        if field == "validation.decision":
            requests.append({
                "id": "repair_rejected_experiment_result",
                "kind": "additional_experiment",
                "owner": "methods.validation",
                "objective": "Address the independent scientific review findings and rerun or repair the affected study before publication.",
                "why": "A rejected result package may be retained for interpretation, but its findings are not admissible as paper evidence.",
                "success_condition": "A fresh evidence-bound assessment accepts the repaired result package, with its limitations preserved.",
                "evidence_needed": "The reviewer findings, targeted repair or rerun outputs, and an updated independently checked result package.",
            })
            continue
        requests.append({
            "id": f"research_quality_{field}",
            "kind": "analysis_display" if field in {"figures", "comparisons", "uncertainty", "effect_size", "sensitivity", "ablation"} else "additional_experiment",
            "owner": "methods.validation",
            "objective": f"Resolve the research-quality deficit in {field} ({deficit.get('observed', 0)} observed, {deficit.get('required', 1)} required).",
            "why": "The result package does not yet support a comparative, uncertainty-aware scientific argument.",
            "success_condition": f"The accepted analysis summary satisfies the declared {field} floor.",
            "evidence_needed": "Preserve raw observations, the calculation trace, and a figure or table that exposes the comparison.",
        })
    return {
        "schema_version": "research-quality-admission-1",
        "decision": "proceed" if not deficits else "research_expansion_required",
        "contract": deepcopy(contract),
        "observed": {
            "conditions": len(results.get("analysis", {}).get("conditions", [])) if isinstance(results.get("analysis"), dict) else 0,
            "independent_seeds": len(results.get("analysis", {}).get("independent_seeds", [])) if isinstance(results.get("analysis"), dict) else 0,
            "controls": len(results.get("analysis", {}).get("controls", [])) if isinstance(results.get("analysis"), dict) else 0,
            "comparisons": len(results.get("analysis", {}).get("comparisons", [])) if isinstance(results.get("analysis"), dict) else 0,
            "figures": sum(1 for asset in results.get("assets", []) if isinstance(asset, dict) and asset.get("role") == "figure"),
        },
        "deficits": deficits,
        "expansion_requests": requests,
    }
