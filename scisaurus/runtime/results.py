"""Versioned, evidence-bound scientific result packages."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _ref(value, name, *, nullable=False):
    if value is None and nullable:
        return value
    _text(value, name)
    if not value.startswith("artifact:"):
        raise ValidationError(f"{name} must be an artifact reference")
    return value


def _core(value, *, asset_version, base_dir):
    identifiers = set()
    for procedure in value["procedures"]:
        _exact(procedure, {"id", "description", "source"}, "procedure")
        _identifier(procedure["id"], "procedure id")
        _text(procedure["description"], "procedure description")
        _text(procedure["source"], "procedure source")
        if procedure["id"] in identifiers:
            raise ValidationError("results package identifiers must be unique")
        identifiers.add(procedure["id"])
    metric_ids = set()
    for metric in value["metrics"]:
        _exact(metric, {"id", "value", "unit", "conditions", "source", "presentation"}, "metric")
        _identifier(metric["id"], "metric id")
        for key in ("unit", "conditions", "source", "presentation"):
            _text(metric[key], f"metric {key}")
        if (isinstance(metric["value"], bool) or not isinstance(metric["value"], (str, int, float))
                or isinstance(metric["value"], float) and not math.isfinite(metric["value"])):
            raise ValidationError("metric value must be an exact finite JSON scalar")
        if metric["id"] in identifiers:
            raise ValidationError("results package identifiers must be unique")
        identifiers.add(metric["id"])
        metric_ids.add(metric["id"])
    for finding in value["findings"]:
        _exact(finding, {"id", "statement", "metric_ids"}, "finding")
        _identifier(finding["id"], "finding id")
        _text(finding["statement"], "finding statement")
        if (not isinstance(finding["metric_ids"], list) or not finding["metric_ids"]
                or len(finding["metric_ids"]) != len(set(finding["metric_ids"]))
                or set(finding["metric_ids"]) - metric_ids):
            raise ValidationError("finding must bind exact package metrics")
        if finding["id"] in identifiers:
            raise ValidationError("results package identifiers must be unique")
        identifiers.add(finding["id"])
    if not isinstance(value["limitations"], list) or not value["limitations"]:
        raise ValidationError("results package requires explicit limitations")
    for limitation in value["limitations"]:
        _text(limitation, "result limitation")
    if not isinstance(value["assets"], list):
        raise ValidationError("results assets must be an explicit list")
    for asset in value["assets"]:
        if asset_version == 1:
            _exact(asset, {"path", "sha256", "role"}, "result asset")
        else:
            _exact(asset, {"id", "path", "sha256", "role", "media_type", "caption"}, "result asset")
            _identifier(asset["id"], "result asset id")
            if asset["id"] in identifiers:
                raise ValidationError("results package identifiers must be unique")
            identifiers.add(asset["id"])
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
        _exact(value, core | {"study_type", "question", "hypothesis", "provenance", "validation"},
               "results package")
        asset_version = 2
        if value["study_type"] not in {"novel_research", "replication", "methods_validation", "exploratory"}:
            raise ValidationError("results package study_type is unsupported")
        _text(value["question"], "results package question")
        _text(value["hypothesis"], "results package hypothesis")
        provenance = value["provenance"]
        _exact(provenance, {"score_ref", "literature_survey_ref", "literature_assessment_ref",
                            "execution_refs", "validator_execution_ref", "execution_profile_ref",
                            "validation_profile_ref", "replay_sha256"}, "results provenance")
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
    else:
        raise ValidationError("unsupported results package schema")
    _identifier(value["id"], "results package id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("results package revision must be positive")
    _core(value, asset_version=asset_version, base_dir=base_dir)
    canonical_bytes(value)
    return value
