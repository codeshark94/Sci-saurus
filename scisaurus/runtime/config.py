"""Explicit configuration for a public-data paragraph integration run."""
from __future__ import annotations
import json
import math
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import ModelClient


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or value == "runtime_required":
        raise ValidationError(f"{field} requires an explicit nonempty value")
    return value


def load_config(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("runtime configuration must be a readable JSON file") from exc
    return validate_config(value)


def validate_config(value):
    fields = {"live_dispatch_allowed", "data_classification", "allocation_mode", "project_id", "objective",
              "paragraph", "preserved_neighbor", "supplied_context", "required_literals", "public_queries",
              "source_urls", "mcp_fetch_command", "model", "limits"}
    if not isinstance(value, dict):
        raise ValidationError("runtime configuration must be an object")
    if set(value) - fields:
        raise ValidationError("runtime configuration contains unsupported fields")
    if value.get("live_dispatch_allowed") is not True:
        raise ValidationError("live_dispatch_allowed must be explicitly true")
    if value.get("data_classification") != "public":
        raise ValidationError("this integration runner accepts public input only")
    if value.get("allocation_mode") != "capacity_pool":
        raise ValidationError("this runner requires an explicitly authorized capacity pool")
    for key in ("project_id", "objective", "paragraph", "preserved_neighbor", "supplied_context"):
        _text(value.get(key), key)
    if not isinstance(value.get("model"), dict):
        raise ValidationError("model configuration is required")
    try:
        model = ModelClient(**value["model"])
    except TypeError as exc:
        raise ValidationError("model configuration contains missing or unsupported fields") from exc
    limits = value.get("limits", {})
    if not isinstance(limits, dict):
        raise ValidationError("limits must be an object")
    for key in ("max_rounds", "search_results", "max_capture_chars", "max_source_bytes", "max_result_bytes", "concurrent_calls"):
        if type(limits.get(key)) is not int or limits[key] <= 0:
            raise ValidationError(f"limits.{key} must be a positive integer")
    for key in ("wall_clock_seconds", "checkpoint_seconds", "retrieval_timeout_seconds"):
        if type(limits.get(key)) not in (int, float) or not math.isfinite(limits[key]) or limits[key] <= 0:
            raise ValidationError(f"limits.{key} must be finite and positive")
    if limits["concurrent_calls"] < 2:
        raise ValidationError("capacity must cover one worker and its independently reserved verification call")
    if model.timeout_seconds >= limits["wall_clock_seconds"]:
        raise ValidationError("model timeout must fit within the run deadline")
    for key in ("public_queries", "source_urls", "required_literals"):
        if not isinstance(value.get(key), list) or (key != "required_literals" and not value[key]):
            raise ValidationError(f"{key} must be an explicit list")
        for item in value[key]:
            _text(item, key)
    for literal in value["required_literals"]:
        if literal not in value["paragraph"]:
            raise ValidationError("required_literals must be present in the baseline paragraph")
    mcp = value.get("mcp_fetch_command")
    if not isinstance(mcp, list) or not mcp or any(not isinstance(p, str) or not p for p in mcp):
        raise ValidationError("mcp_fetch_command must be an explicit executable argument list")
    return value
