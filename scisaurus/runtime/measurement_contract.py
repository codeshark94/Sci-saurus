"""Declared scientific decision inputs and their independent verification."""
from copy import deepcopy
import math
import operator

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.model_work import ModelWorkBlocked
from scisaurus.runtime.scores import IDENTIFIER_PATTERN, exact, identifier



class ModelDefinitionError(ModelWorkBlocked):
    """A scientific specification requires adjudication rather than format repair."""
    failure_class = "experiment_capability_repair"
    repair_gate = "model_definition"
    repair_owner = "methods_adjudication"
    recovery_mode = "repair_then_rerun"
    next_action = "methods_adjudication_before_source_repair"


INTENT_EXTENSIONS = frozenset({"decision_outcomes", "decision_rules", "model_definition", "evidence_plan"})
COMPARATORS = {"<": operator.lt, "<=": operator.le, ">": operator.gt,
               ">=": operator.ge, "==": operator.eq}


def model_definition_contract():
    return {
        "equations": [{"id": f"equation_id matching {IDENTIFIER_PATTERN}", "expression": "equation or algorithm",
                       "status": "source_bound | design_assumption | estimated",
                       "source_ref": "exact declared source ref or null"}],
        "variables": [{"id": f"variable_id matching {IDENTIFIER_PATTERN}", "unit": "physical or dimensionless unit",
                       "reference_scale": "reference system and conversion"}],
        "parameters": [{"id": f"parameter_id matching {IDENTIFIER_PATTERN}", "value": "finite non-boolean number",
                        "unit": "unit", "status": "source_bound | design_assumption | estimated",
                        "source_ref": "exact declared source ref or null", "reason": "basis and applicability"}],
        "source_refs": ["exact captured scientific source ref; no annotations; source_bound rows require a ref from this list"],
        "applicability": "regime and exclusions", "claim_scope": "assumed pilot versus empirical inference",
        "question_alignment": "which question the mechanism can answer",
    }


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be nonempty text")


def _finite(value, name):
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValidationError(f"{name} must be a finite non-boolean number")


def recalculation_outcomes(intent):
    primary = intent.get("primary_outcomes", [])
    derived = intent.get("decision_outcomes", [])
    if not isinstance(primary, list) or not isinstance(derived, list):
        raise ValidationError("measurement outcomes must be arrays")
    seen = set()
    for row in primary:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValidationError("primary measurement has an invalid identity or unit")
        identifier(row["id"])
        if "unit" in row:
            _text(row["unit"], "primary measurement unit")
        if row["id"] in seen:
            raise ValidationError("primary measurement duplicates an identity")
        seen.add(row["id"])
    for row in derived:
        exact(row, {"id", "definition", "unit", "parents"}, "decision outcome")
        identifier(row["id"])
        if row["id"] in seen:
            raise ValidationError("decision outcome duplicates another metric")
        for key in ("definition", "unit"):
            _text(row[key], "decision outcome " + key)
        parents = row["parents"]
        if (not isinstance(parents, list) or not parents or any(not isinstance(parent, str) for parent in parents) or len(set(parents)) != len(parents)
                or any(parent not in seen for parent in parents)):
            raise ValidationError("decision parents must reference preceding declared outcomes")
        seen.add(row["id"])
    outcomes = [*primary, *derived]
    by_id = {row["id"]: row for row in outcomes}
    rules = intent.get("decision_rules", [])
    if not isinstance(rules, list):
        raise ValidationError("decision rules must be an array")
    rule_ids = set()
    for rule in rules:
        exact(rule, {"id", "metric_id", "unit", "operator", "threshold", "claim"}, "decision rule")
        identifier(rule["id"])
        if not isinstance(rule["metric_id"], str) or rule["id"] in rule_ids or rule["metric_id"] not in by_id:
            raise ValidationError("decision rule has duplicated identity or undeclared metric")
        rule_ids.add(rule["id"])
        if not isinstance(rule["operator"], str) or rule["operator"] not in COMPARATORS:
            raise ValidationError("decision rule comparator is unsupported")
        _finite(rule["threshold"], "decision threshold")
        if rule["unit"] != by_id[rule["metric_id"]].get("unit"):
            raise ValidationError("decision rule and metric units differ")
        _text(rule["claim"], "conditional decision claim")
    from scisaurus.runtime.study_evidence import validate_evidence_plan
    validate_evidence_plan(intent)
    return deepcopy(outcomes)


def verified_decisions(intent, verdict):
    """Evaluate only independently matched current values; null remains undefined."""
    recalculation_outcomes(intent)
    if not intent.get("decision_rules"):
        return []
    if verdict.get("decision") != "accepted":
        raise ValidationError("decision requires an accepted independent verdict")
    metrics = {row["metric_id"]: row for row in verdict["metric_recalculations"]}
    for row in intent.get("decision_outcomes", []):
        if row["id"] not in metrics or any(parent not in metrics for parent in row["parents"]):
            raise ValidationError("decision outcome or parent is missing from independent evidence")
        if any(metrics[parent]["recalculated_value"] is None for parent in row["parents"]) and metrics[row["id"]]["recalculated_value"] is not None:
            raise ValidationError("decision outcome is defined despite an undefined parent")
    decisions = []
    for rule in intent.get("decision_rules", []):
        metric = metrics.get(rule["metric_id"])
        if metric is None or metric["matches"] is not True:
            raise ValidationError("decision depends on an unverified metric")
        value = metric["recalculated_value"]
        if value is not None:
            _finite(value, "verified decision value")
        decisions.append({"rule": deepcopy(rule), "value": value,
                          "candidate_sha256": verdict["candidate_sha256"],
                          "outcome": "not_estimable" if value is None else (
                              "satisfied" if COMPARATORS[rule["operator"]](value, rule["threshold"])
                              else "not_satisfied")})
    return decisions


def validate_model_definition(intent, *, source_refs=None, required=False):
    definition = intent.get("model_definition")
    if definition is None:
        if required:
            raise ValidationError("scientific implementation requires an explicit model_definition")
        return
    exact(definition, {"equations", "variables", "parameters", "source_refs", "applicability",
                       "claim_scope", "question_alignment"}, "model definition")
    for key in ("applicability", "claim_scope", "question_alignment"):
        _text(definition[key], "model " + key)
    refs = definition["source_refs"]
    if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
            or len(set(refs)) != len(refs)):
        raise ValidationError("model definition requires distinct recorded source references")
    if source_refs is not None and not set(refs).issubset(set(source_refs)):
        raise ModelDefinitionError("model definition refers to unacquired sources")
    identity_errors = []
    for collection in ("equations", "variables", "parameters"):
        rows = definition[collection]
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or "id" not in row:
                continue
            try:
                identifier(row["id"])
            except ValidationError:
                identity_errors.append(
                    f"/model_definition/{collection}/{index}/id={row['id']!r} "
                    f"must match {IDENTIFIER_PATTERN}")
    if identity_errors:
        raise ValidationError("; ".join(identity_errors))
    for collection, fields in (
            ("equations", {"id", "expression", "status", "source_ref"}),
            ("variables", {"id", "unit", "reference_scale"}),
            ("parameters", {"id", "value", "unit", "status", "source_ref", "reason"})):
        rows = definition[collection]
        if not isinstance(rows, list) or (collection != "parameters" and not rows):
            raise ValidationError(f"model {collection} must be nonempty")
        seen = set()
        for row in rows:
            exact(row, fields, "model " + collection)
            identifier(row["id"])
            if row["id"] in seen:
                raise ValidationError("model definition duplicates an identity")
            seen.add(row["id"])
            if collection == "variables":
                _text(row["unit"], "variable unit")
                _text(row["reference_scale"], "variable reference scale")
                continue
            if not isinstance(row["status"], str) or row["status"] not in {"source_bound", "design_assumption", "estimated"}:
                raise ValidationError("model quantity provenance status is invalid")
            if row["source_ref"] is not None and row["source_ref"] not in refs:
                raise ValidationError("model quantity has an undeclared source")
            if row["status"] == "source_bound" and row["source_ref"] is None:
                raise ValidationError("source-bound model quantity requires its source")
            if collection == "equations":
                _text(row["expression"], "equation expression")
            else:
                _finite(row["value"], "model parameter")
                _text(row["unit"], "parameter unit")
                _text(row["reason"], "parameter basis")
                if row["id"] in intent.get("parameters", {}) and intent["parameters"][row["id"]] != row["value"]:
                    raise ModelDefinitionError("model parameter differs from the frozen execution parameter")
