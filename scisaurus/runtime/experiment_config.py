"""Configuration contract for bounded scientific execution and validation."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.config import _text, validate_common
from scisaurus.runtime.programs import json_object
from scisaurus.runtime.scores import exact, identifier
from scisaurus.runtime.time_policy import validate_time_policy


STUDY_TYPES = {"novel_research", "replication", "methods_validation", "exploratory"}
GAP_STATES = {"eligible_for_experiment", "refuted_by_prior_work", "insufficient_evidence"}
DIRECTIONS = {"higher", "lower", "descriptive"}
ASSET_MEDIA_TYPES = {"image/png", "image/jpeg", "application/pdf", "image/svg+xml"}


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


def validate_experiment_config(config):
    validate_common(config, {"experiment", "time_policy"}, retrieval=False)
    if config["model"]["protocol"] != "openai_compatible":
        raise ValidationError("experiment review requires an openai_compatible multimodal model")
    if config["limits"]["concurrent_calls"] < 3:
        raise ValidationError("experiment capacity must cover two reviewers and final verification")
    experiment = config.get("experiment")
    exact(experiment, {
        "id", "revision", "study_type", "domain", "research_question", "hypothesis", "method",
        "parameters", "seed", "run_count", "stopping_rule", "primary_outcomes", "limitations",
        "literature_gate", "execution", "validation", "required_assets", "reviewers", "stage_seconds",
        "max_observations", "max_asset_bytes",
    }, "experiment score")
    identifier(experiment["id"])
    if type(experiment["revision"]) is not int or experiment["revision"] < 1:
        raise ValidationError("experiment revision must be a positive integer")
    if experiment["study_type"] not in STUDY_TYPES:
        raise ValidationError("experiment study_type is unsupported")
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
        if experiment["study_type"] == "novel_research":
            raise ValidationError("novel research requires a current experiment-eligible literature assessment")
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
                         unit_count=len(reviewers), worker_slots=config["limits"]["concurrent_calls"] - 1,
                         wall_clock_seconds=config["limits"]["wall_clock_seconds"])
    canonical_bytes(config)
    return deepcopy(config)


def load_experiment_config(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("experiment configuration must be a readable JSON file") from exc
    return validate_experiment_config(value)
