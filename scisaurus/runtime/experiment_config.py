"""Configuration contract for bounded scientific execution and validation."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.config import _text, configured_worker_slots, validate_common
from scisaurus.runtime.programs import json_object
from scisaurus.runtime.research_quality import validate_quality_contract
from scisaurus.runtime.scores import exact, identifier
from scisaurus.runtime.time_policy import validate_time_policy


STUDY_TYPES = {"novel_research", "replication", "methods_validation", "exploratory"}
GAP_STATES = {"eligible_for_experiment", "refuted_by_prior_work", "insufficient_evidence"}
DIRECTIONS = {"higher", "lower", "descriptive"}
ASSET_MEDIA_TYPES = {"image/png", "image/jpeg", "application/pdf", "image/svg+xml"}
EXPERIMENT_WORK_ORDER_KINDS = frozenset({
    "additional_experiment", "analysis_display", "analysis_repair",
})
WORK_ORDER_REQUIRED_FIELDS = frozenset({
    "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed",
})
WORK_ORDER_OPTIONAL_FIELDS = frozenset({
    "failure_dossier_ref", "failure_input_sha256", "repair_commands", "acceptance_checks",
    "review_directives", "model_diagnostics", "recovery_mode", "target_stage_id",
    "target_stage_kind", "repair_priority", "experiment_repair_plan", "repair_strategy",
    "attempt_lineage", "topic_id", "topic_cycle", "topic_ids", "work_kind", "source_stage_id",
})


class ExperimentWorkOrderContractError(ValidationError):
    """A controller supplied a non-executable or malformed experiment work order."""

    failure_class = "harness_bug"


def project_executable_work_orders(requests):
    """Keep only science work orders and remove Composer-only control metadata."""
    if requests is None:
        return []
    if isinstance(requests, tuple):
        requests = list(requests)
    if not isinstance(requests, list):
        raise ExperimentWorkOrderContractError(
            "Composer experiment work-order projection must be a list")
    allowed = WORK_ORDER_REQUIRED_FIELDS | WORK_ORDER_OPTIONAL_FIELDS
    projected = []
    for request in requests:
        if not isinstance(request, dict):
            raise ExperimentWorkOrderContractError(
                "Composer experiment work-order projection contains a non-object")
        if request.get("kind") not in EXPERIMENT_WORK_ORDER_KINDS:
            continue
        missing = WORK_ORDER_REQUIRED_FIELDS - set(request)
        if missing:
            raise ExperimentWorkOrderContractError(
                "Composer experiment work order omits required fields: "
                + ", ".join(sorted(missing)))
        projected.append({key: deepcopy(value) for key, value in request.items()
                          if key in allowed})
    return validate_work_orders(projected)


def _positive_number(value, name):
    if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
        raise ValidationError(f"{name} must be finite and positive")
    return value


def _program(value, name):
    exact(value, {"id", "adapter", "client", "representative", "environment_files", "input"}, name)
    identifier(value["id"])
    if value["adapter"] != "local_program":
        raise ValidationError(f"{name} must use the local_program adapter")
    if not isinstance(value["client"], dict):
        raise ValidationError(f"{name}.client must be an object")
    if not isinstance(value["representative"], dict) or set(value["representative"]) != {"input"}:
        raise ValidationError(f"{name}.representative requires exactly input")
    try:
        json_object(value["representative"]["input"])
        json_object(value["input"])
    except (ValueError, RecursionError) as exc:
        raise ValidationError(str(exc)) from exc
    files = value["environment_files"]
    if not isinstance(files, list) or not files:
        raise ValidationError(f"{name}.environment_files must identify pinned program sources")
    for item in files:
        if not isinstance(item, str) or not Path(item).is_absolute() or not Path(item).is_file():
            raise ValidationError(f"{name}.environment_files must be existing absolute files")


def validate_work_orders(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ExperimentWorkOrderContractError("work_orders must be a list")
    seen = set()
    for order in value:
        if not isinstance(order, dict):
            raise ExperimentWorkOrderContractError("work order must be an object")
        missing = WORK_ORDER_REQUIRED_FIELDS - set(order)
        unexpected = set(order) - (
            WORK_ORDER_REQUIRED_FIELDS | WORK_ORDER_OPTIONAL_FIELDS)
        if missing or unexpected:
            details = []
            if missing:
                details.append("missing " + ", ".join(sorted(missing)))
            if unexpected:
                details.append("unexpected " + ", ".join(sorted(unexpected)))
            raise ExperimentWorkOrderContractError(
                "work order contract mismatch (" + "; ".join(details) + ")")
        if order["kind"] == "recovery":
            raise ExperimentWorkOrderContractError(
                "Composer recovery directives are control-plane work, not experiment work orders")
        for key in ("id", "kind"):
            identifier(order[key])
        if order["id"] in seen:
            raise ExperimentWorkOrderContractError(
                "work order IDs must be unique within an experiment")
        seen.add(order["id"])
        for key in WORK_ORDER_REQUIRED_FIELDS - {"id", "kind"}:
            try:
                _text(order[key], f"work_orders.{key}")
            except ValidationError as exc:
                raise ExperimentWorkOrderContractError(str(exc)) from exc
        for key in WORK_ORDER_OPTIONAL_FIELDS & set(order):
            canonical_bytes(order[key])
    return deepcopy(value)


def validate_experiment_config(config, *, require_literature_gate=True):
    validate_common(config, {"experiment", "time_policy", "work_orders"}, retrieval=False)
    work_orders = validate_work_orders(config.get("work_orders"))
    if work_orders:
        config = deepcopy(config)
        config["work_orders"] = work_orders
    repair_mode = config.get("limits", {}).get("repair_mode")
    if repair_mode is not None and repair_mode not in {"bounded", "until_deadline"}:
        raise ValidationError("limits.repair_mode must be bounded or until_deadline")
    if config["limits"]["concurrent_calls"] < 3:
        raise ValidationError("experiment capacity must cover two reviewers and final verification")
    experiment = config.get("experiment")
    experiment_fields = {
        "id", "revision", "study_type", "domain", "research_question", "hypothesis", "method",
        "parameters", "seed", "run_count", "stopping_rule", "primary_outcomes", "limitations",
        "literature_gate", "execution", "validation", "required_assets", "reviewers", "stage_seconds",
        "max_observations", "max_asset_bytes",
    }
    if (not isinstance(experiment, dict) or set(experiment) - (experiment_fields | {"quality_contract"})
            or not experiment_fields.issubset(experiment)):
        raise ValidationError(
            f"experiment score requires {sorted(experiment_fields)} and permits quality_contract")
    identifier(experiment["id"])
    if type(experiment["revision"]) is not int or experiment["revision"] < 1:
        raise ValidationError("experiment revision must be a positive integer")
    if experiment["study_type"] not in STUDY_TYPES:
        raise ValidationError("experiment study_type is unsupported")
    quality_contract = experiment.get("quality_contract")
    if quality_contract is None and experiment["study_type"] == "novel_research":
        raise ValidationError("novel research requires an explicit quality_contract")
    if quality_contract is not None:
        validate_quality_contract(quality_contract, study_type=experiment["study_type"])
    for key in ("domain", "research_question", "hypothesis", "method", "stopping_rule"):
        _text(experiment[key], f"experiment.{key}")
    try:
        json_object(experiment["parameters"])
    except (ValueError, RecursionError) as exc:
        raise ValidationError(str(exc)) from exc
    if type(experiment["seed"]) is not int or experiment["seed"] < 0:
        raise ValidationError("experiment.seed must be a non-negative integer")
    if type(experiment["run_count"]) is not int or experiment["run_count"] < 1:
        raise ValidationError("experiment.run_count must be a positive integer")
    for name in ("max_observations", "max_asset_bytes"):
        if type(experiment[name]) is not int or experiment[name] < 1:
            raise ValidationError(f"experiment.{name} must be a positive integer")

    outcomes = experiment["primary_outcomes"]
    if not isinstance(outcomes, list) or not outcomes:
        raise ValidationError("experiment.primary_outcomes must be nonempty")
    outcome_ids = set()
    for outcome in outcomes:
        exact(outcome, {"id", "definition", "unit", "direction", "threshold"}, "primary outcome")
        identifier(outcome["id"])
        if outcome["id"] in outcome_ids:
            raise ValidationError("duplicate primary outcome ID")
        outcome_ids.add(outcome["id"])
        _text(outcome["definition"], "primary outcome definition")
        _text(outcome["unit"], "primary outcome unit")
        if outcome["direction"] not in DIRECTIONS:
            raise ValidationError("primary outcome direction is unsupported")
        if outcome["threshold"] is not None and (
                type(outcome["threshold"]) not in (int, float) or not math.isfinite(outcome["threshold"])):
            raise ValidationError("primary outcome threshold must be finite or null")

    limitations = experiment["limitations"]
    if not isinstance(limitations, list) or not limitations:
        raise ValidationError("experiment requires explicit design limitations")
    for limitation in limitations:
        _text(limitation, "experiment limitation")

    gate = experiment["literature_gate"]
    if gate is None:
        if experiment["study_type"] == "novel_research" and require_literature_gate:
            raise ValidationError("novel research requires a current experiment-eligible literature gate assessment")
    else:
        exact(gate, {"project_dir", "survey_ref", "assessment_ref", "required_state"}, "literature gate")
        path = Path(gate["project_dir"])
        if not path.is_absolute() or not (path / "state" / "control.sqlite").is_file():
            raise ValidationError("literature gate project_dir must identify an existing survey project")
        for key in ("survey_ref", "assessment_ref"):
            _text(gate[key], f"literature_gate.{key}")
        if gate["required_state"] not in GAP_STATES:
            raise ValidationError("literature gate required_state is unsupported")
        if experiment["study_type"] == "novel_research" and gate["required_state"] != "eligible_for_experiment":
            raise ValidationError("novel research requires eligible_for_experiment")

    _program(experiment["execution"], "experiment execution")
    _program(experiment["validation"], "experiment validation")
    if experiment["execution"]["id"] == experiment["validation"]["id"]:
        raise ValidationError("execution and validation capabilities must be distinct")

    required_assets = experiment["required_assets"]
    if not isinstance(required_assets, list):
        raise ValidationError("experiment.required_assets must be a list")
    roles = set()
    for asset in required_assets:
        exact(asset, {"role", "media_types", "min_count"}, "required asset")
        identifier(asset["role"])
        if asset["role"] in roles:
            raise ValidationError("required asset roles must be unique")
        roles.add(asset["role"])
        if (not isinstance(asset["media_types"], list) or not asset["media_types"]
                or len(asset["media_types"]) != len(set(asset["media_types"]))
                or set(asset["media_types"]) - ASSET_MEDIA_TYPES):
            raise ValidationError("required asset media_types are invalid")
        if type(asset["min_count"]) is not int or asset["min_count"] < 1:
            raise ValidationError("required asset min_count must be positive")

    reviewers = experiment["reviewers"]
    if not isinstance(reviewers, list) or not 2 <= len(reviewers) <= 6:
        raise ValidationError("experiment requires two to six review perspectives")
    reviewer_ids = set()
    for reviewer in reviewers:
        exact(reviewer, {"id", "focus"}, "experiment reviewer")
        identifier(reviewer["id"])
        if reviewer["id"] in reviewer_ids:
            raise ValidationError("duplicate experiment reviewer ID")
        reviewer_ids.add(reviewer["id"])
        _text(reviewer["focus"], "experiment reviewer focus")

    for stage, value in experiment["stage_seconds"].items() if isinstance(experiment["stage_seconds"], dict) else ():
        _positive_number(value, f"experiment.stage_seconds.{stage}")
    validate_time_policy(config.get("time_policy"), stage_seconds=experiment["stage_seconds"],
                         unit_count=len(reviewers), worker_slots=configured_worker_slots(config["limits"]),
                         wall_clock_seconds=config["limits"]["wall_clock_seconds"])
    canonical_bytes(config)
    return deepcopy(config)


def load_experiment_config(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("experiment configuration must be a readable JSON file") from exc
    return validate_experiment_config(value)
