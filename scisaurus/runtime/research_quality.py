"""Reusable quality contracts for substantive computational research.

The experiment runner can verify that a program was replayable without knowing
whether the study was scientifically informative.  This module keeps that
second question explicit and configurable.  A quality contract is frozen with
the experiment; the program must then emit the corresponding analysis ledger
before the result can enter a research-paper pipeline.
"""

from __future__ import annotations

from copy import deepcopy
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


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value.strip()


def _strings(value, name, *, allow_empty=False):
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ValidationError(f"{name} must be a nonempty string list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{name} must contain nonempty strings")
    if len(value) != len(set(value)):
        raise ValidationError(f"{name} must not contain duplicates")
    return value


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
    canonical_bytes(design)
    return design


def validate_analysis(value):
    """Validate the program's reader-facing analysis summary."""
    if not isinstance(value, dict) or set(value) != ANALYSIS_FIELDS:
        raise ValidationError(f"analysis requires exactly {sorted(ANALYSIS_FIELDS)}")
    _strings(value["conditions"], "analysis.conditions")
    seeds = value["independent_seeds"]
    if (not isinstance(seeds, list) or not seeds
            or len(seeds) != len(set(seeds))
            or any(type(seed) is not int or seed < 0 for seed in seeds)):
        raise ValidationError("analysis.independent_seeds must be unique nonnegative integers")
    _strings(value["controls"], "analysis.controls", allow_empty=True)
    comparisons = value["comparisons"]
    if not isinstance(comparisons, list):
        raise ValidationError("analysis.comparisons must be a list")
    comparison_ids = set()
    for comparison in comparisons:
        if not isinstance(comparison, dict) or set(comparison) != {"id", "description"}:
            raise ValidationError("analysis comparison has an invalid shape")
        _identifier(comparison["id"], "analysis comparison id")
        _text(comparison["description"], "analysis comparison description")
        if comparison["id"] in comparison_ids:
            raise ValidationError("analysis comparison IDs must be unique")
        comparison_ids.add(comparison["id"])
    for key in ("uncertainty", "effect_sizes", "sensitivity", "ablation", "raw_data"):
        _strings(value[key], f"analysis.{key}", allow_empty=True)
    canonical_bytes(value)
    return deepcopy(value)


def check_analysis_contract(analysis, contract, *, figure_count=0):
    """Return scientific-work deficits without turning them into prose claims."""
    validate_quality_contract(contract)
    validate_analysis(analysis)
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
        if not analysis[field]:
            deficits.append({"field": requirement, "observed": 0, "required": 1})
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
