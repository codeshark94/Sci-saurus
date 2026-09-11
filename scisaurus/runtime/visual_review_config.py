"""Configuration contract for versioned multimodal visual assessment."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.config import _text, validate_common
from scisaurus.runtime.scores import exact, identifier
from scisaurus.runtime.time_policy import validate_time_policy


VISUAL_MODES = {"academic_figure", "aesthetic_concept", "general_visual"}
ASSET_ROLES = {"subject", "reference", "source", "rendered"}
MEDIA_TYPES = {"image/png", "image/jpeg"}


def validate_visual_review_config(config):
    validate_common(config, {"visual_review", "time_policy"}, retrieval=False)
    if config["model"]["protocol"] != "openai_compatible":
        raise ValidationError("visual review requires an openai_compatible multimodal model")
    review = config.get("visual_review")
    exact(review, {"id", "revision", "mode", "target_medium", "intended_audience",
                   "assets", "criteria", "perspectives", "stage_seconds"}, "visual review")
    identifier(review["id"])
    if type(review["revision"]) is not int or review["revision"] < 1:
        raise ValidationError("visual review revision must be a positive integer")
    if review["mode"] not in VISUAL_MODES:
        raise ValidationError("visual review mode is unsupported")
    _text(review["target_medium"], "visual_review.target_medium")
    _text(review["intended_audience"], "visual_review.intended_audience")

    assets = review["assets"]
    if not isinstance(assets, list) or not 1 <= len(assets) <= 16:
        raise ValidationError("visual review requires between one and sixteen assets")
    asset_ids = set()
    for asset in assets:
        exact(asset, {"id", "path", "media_type", "role", "label"}, "visual asset")
        identifier(asset["id"])
        if asset["id"] in asset_ids:
            raise ValidationError("duplicate visual asset ID")
        asset_ids.add(asset["id"])
        if (not isinstance(asset["path"], str) or not Path(asset["path"]).is_absolute()
                or asset["media_type"] not in MEDIA_TYPES or asset["role"] not in ASSET_ROLES):
            raise ValidationError("visual asset path, media type, or role is invalid")
        _text(asset["label"], "visual asset label")

    criteria = review["criteria"]
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 24:
        raise ValidationError("visual review requires between one and twenty-four criteria")
    criterion_ids = set()
    for criterion in criteria:
        exact(criterion, {"id", "requirement"}, "visual criterion")
        identifier(criterion["id"])
        if criterion["id"] in criterion_ids:
            raise ValidationError("duplicate visual criterion ID")
        criterion_ids.add(criterion["id"])
        _text(criterion["requirement"], "visual criterion requirement")

    perspectives = review["perspectives"]
    if not isinstance(perspectives, list) or not 2 <= len(perspectives) <= 6:
        raise ValidationError("visual review requires two to six independent perspectives")
    perspective_ids = set()
    for perspective in perspectives:
        exact(perspective, {"id", "focus"}, "visual perspective")
        identifier(perspective["id"])
        if perspective["id"] in perspective_ids:
            raise ValidationError("duplicate visual perspective ID")
        perspective_ids.add(perspective["id"])
        _text(perspective["focus"], "visual perspective focus")

    validate_time_policy(config.get("time_policy"), stage_seconds=review["stage_seconds"],
                         unit_count=len(perspectives), worker_slots=config["limits"]["concurrent_calls"] - 1,
                         wall_clock_seconds=config["limits"]["wall_clock_seconds"])
    return deepcopy(config)


def load_visual_review_config(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("visual-review configuration must be a readable JSON file") from exc
    return validate_visual_review_config(value)
