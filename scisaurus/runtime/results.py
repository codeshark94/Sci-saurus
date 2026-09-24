"""Versioned, evidence-bound scientific result packages."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.research_quality import (
    evaluate_result_package_quality,
    validate_analysis,
    validate_quality_contract,
)


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValidationError(f"{name} {value!r} must match '[a-z][a-z0-9_-]{{0,63}}'; "
                              "keep the declared ID, executor metric ID and validator references identical")
    return value


def _ref(value, name, *, nullable=False):
    if value is None and nullable:
        return value
    _text(value, name)
    if not value.startswith("artifact:"):
        raise ValidationError(f"{name} must be an artifact reference")
    return value


def _core(value, *, asset_version, base_dir):
    identifiers = {}

    def reserve_id(identifier, location):
        if identifier in identifiers:
            raise ValidationError(
                f"results package identifiers must be unique: {identifier!r} at {location} "
                f"duplicates {identifiers[identifier]}; use a distinct ID for each condition "
                "and update primary outcomes and finding references consistently")
        identifiers[identifier] = location

    for index, procedure in enumerate(value["procedures"]):
        _exact(procedure, {"id", "description", "source"}, "procedure")
        _identifier(procedure["id"], "procedure id")
        _text(procedure["description"], "procedure description")
        _text(procedure["source"], "procedure source")
        reserve_id(procedure["id"], f"procedures[{index}]")
    metric_ids = set()
    for index, metric in enumerate(value["metrics"]):
        _exact(metric, {"id", "value", "unit", "conditions", "source", "presentation"}, "metric")
        _identifier(metric["id"], "metric id")
        for key in ("unit", "conditions", "source", "presentation"):
            _text(metric[key], f"metric {key}")
        # ``None`` is a first-class censored/undefined estimand. It must stay
        # explicit in the result package; callers may not replace it with a
        # boundary or zero merely to satisfy a numeric transport contract.
        if (metric["value"] is not None
                and (isinstance(metric["value"], bool)
                     or not isinstance(metric["value"], (str, int, float))
                     or isinstance(metric["value"], float) and not math.isfinite(metric["value"]))):
            raise ValidationError(
                f"metric value must be an exact finite JSON scalar or explicit null: metrics[{index}] "
                f"id={metric['id']!r} has {metric['value']!r}; check estimator assumptions and "
                "retain censoring/undefined status and never replace it with an invented number")
        reserve_id(metric["id"], f"metrics[{index}]")
        metric_ids.add(metric["id"])
    for index, finding in enumerate(value["findings"]):
        _exact(finding, {"id", "statement", "metric_ids"}, "finding")
        _identifier(finding["id"], "finding id")
        _text(finding["statement"], "finding statement")
        if (not isinstance(finding["metric_ids"], list) or not finding["metric_ids"]
                or len(finding["metric_ids"]) != len(set(finding["metric_ids"]))
                or set(finding["metric_ids"]) - metric_ids):
            raise ValidationError("finding must bind exact package metrics")
        reserve_id(finding["id"], f"findings[{index}]")
    if not isinstance(value["limitations"], list) or not value["limitations"]:
        raise ValidationError("results package requires explicit limitations")
    for limitation in value["limitations"]:
        _text(limitation, "result limitation")
    if not isinstance(value["assets"], list):
        raise ValidationError("results assets must be an explicit list")
    for index, asset in enumerate(value["assets"]):
        if asset_version == 1:
            _exact(asset, {"path", "sha256", "role"}, "result asset")
        else:
            _exact(asset, {"id", "path", "sha256", "role", "media_type", "caption"}, "result asset")
            _identifier(asset["id"], "result asset id")
            reserve_id(asset["id"], f"assets[{index}]")
            _text(asset["media_type"], "result asset media_type")
            if asset["caption"] is not None:
                _text(asset["caption"], "result asset caption")
            if asset["role"] == "figure" and (
                    asset["media_type"] not in {"image/png", "image/jpeg", "application/pdf"}
                    or asset["caption"] is None):
                raise ValidationError("figure assets require a renderable media type and caption")
        _text(asset["path"], "asset path")
        _text(asset["role"], "asset role")
        if (Path(asset["path"]).is_absolute() or ".." in Path(asset["path"]).parts
                or not re.fullmatch(r"[A-Za-z0-9._/-]+", asset["path"])):
            raise ValidationError("result assets must use package-relative paths")
        if not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
            raise ValidationError("result asset requires SHA-256")
        if base_dir is not None:
            path = Path(base_dir) / asset["path"]
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise ValidationError("result asset is unavailable or changed")


def validate_results_package(value, *, base_dir=None):
    if not isinstance(value, dict):
        raise ValidationError("results package must be an object")
    schema = value.get("schema_version")
    core = {"schema_version", "id", "revision", "procedures", "metrics", "findings", "limitations", "assets"}
    if schema == "results-package-1":
        _exact(value, core, "results package")
        asset_version = 1
    elif schema == "results-package-2":
        required = core | {"study_type", "question", "hypothesis", "provenance", "validation"}
        allowed = required | {"analysis", "quality_contract", "quality_admission"}
        if (set(value) - allowed) or not required.issubset(value):
            raise ValidationError(
                f"results package requires {sorted(required)} and permits analysis, quality_contract")
        asset_version = 2
        if value["study_type"] not in {"novel_research", "replication", "methods_validation", "exploratory"}:
            raise ValidationError("results package study_type is unsupported")
        _text(value["question"], "results package question")
        _text(value["hypothesis"], "results package hypothesis")
        provenance = value["provenance"]
        provenance_fields = {"score_ref", "literature_survey_ref", "literature_assessment_ref",
                             "execution_refs", "validator_execution_ref", "execution_profile_ref",
                             "validation_profile_ref", "replay_sha256"}
        if (set(provenance) - (provenance_fields | {"design_ref"})
                or not provenance_fields.issubset(provenance)):
            raise ValidationError(
                f"results provenance requires {sorted(provenance_fields)} and permits design_ref")
        _ref(provenance["score_ref"], "results score_ref")
        _ref(provenance["literature_survey_ref"], "literature survey_ref", nullable=True)
        _ref(provenance["literature_assessment_ref"], "literature assessment_ref", nullable=True)
        if (not isinstance(provenance["execution_refs"], list) or len(provenance["execution_refs"]) < 2
                or len(provenance["execution_refs"]) != len(set(provenance["execution_refs"]))):
            raise ValidationError("results provenance requires two distinct replay execution refs")
        for ref in provenance["execution_refs"]:
            _ref(ref, "results execution ref")
        for key in ("validator_execution_ref", "execution_profile_ref", "validation_profile_ref"):
            _ref(provenance[key], f"results {key}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(provenance["replay_sha256"])):
            raise ValidationError("results replay_sha256 is invalid")
        validation = value["validation"]
        _exact(validation, {"decision", "deterministic_validation_ref", "model_review_refs", "assessment_ref"},
               "results validation")
        if validation["decision"] not in {"accepted", "accepted_with_limitations"}:
            raise ValidationError("results package requires an accepted validation decision")
        _ref(validation["deterministic_validation_ref"], "deterministic validation ref")
        _ref(validation["assessment_ref"], "result assessment ref")
        if (not isinstance(validation["model_review_refs"], list) or len(validation["model_review_refs"]) < 2
                or len(validation["model_review_refs"]) != len(set(validation["model_review_refs"]))):
            raise ValidationError("results package requires distinct model review refs")
        for ref in validation["model_review_refs"]:
            _ref(ref, "model review ref")
        if "design_ref" in provenance:
            _ref(provenance["design_ref"], "results design_ref")
    else:
        raise ValidationError("unsupported results package schema")
    _identifier(value["id"], "results package id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("results package revision must be positive")
    _core(value, asset_version=asset_version, base_dir=base_dir)
    if schema == "results-package-2":
        if "analysis" in value:
            validate_analysis(value["analysis"])
        if "quality_contract" in value:
            validate_quality_contract(value["quality_contract"], study_type=value["study_type"])
            # The package validator checks the declared contract and the
            # analysis schema, but does not turn a publication-quality deficit
            # into an execution failure.  ``quality_admission`` is the durable
            # decision surface; the paper gate recomputes it before release.
            # This distinction lets valid raw observations reach interpretation
            # while still making missing analyses an explicit research work
            # order rather than silently treating them as publication-ready.
            if "quality_admission" in value:
                admission = value["quality_admission"]
                expected = evaluate_result_package_quality(
                    {key: item for key, item in value.items()
                     if key != "quality_admission"})
                if canonical_bytes(admission) != canonical_bytes(expected):
                    raise ValidationError("results quality_admission does not match the declared package")
        elif "quality_admission" in value:
            raise ValidationError("results quality_admission requires quality_contract")
    canonical_bytes(value)
    return value
