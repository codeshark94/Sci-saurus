"""Shared explicit configuration for public-data integration runs."""
from __future__ import annotations
import json
import math
from pathlib import Path
from urllib.parse import urlsplit

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import resolve_model_config, ModelClient


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or value == "runtime_required":
        raise ValidationError(f"{field} requires an explicit nonempty value")
    return value


def _normalize_provider_url(value):
    return value.rstrip("/") if isinstance(value, str) else value


def validate_provider_pools(value):
    """Validate transient endpoint pools used by the parent dispatcher."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("limits.provider_pools must be an object")
    for pool_name, pool in value.items():
        _text(pool_name, "limits.provider_pools key")
        if not isinstance(pool, dict) or set(pool) != {"max_concurrent", "base_urls"}:
            raise ValidationError(
                f"limits.provider_pools.{pool_name} requires max_concurrent and base_urls")
        if type(pool["max_concurrent"]) is not int or pool["max_concurrent"] <= 0:
            raise ValidationError(
                f"limits.provider_pools.{pool_name}.max_concurrent must be a positive integer")
        urls = pool["base_urls"]
        if not isinstance(urls, list) or not urls:
            raise ValidationError(
                f"limits.provider_pools.{pool_name}.base_urls must be a nonempty list")
        normalized = []
        for url in urls:
            _text(url, f"limits.provider_pools.{pool_name}.base_urls")
            parsed = urlsplit(url)
            if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValidationError(
                    f"limits.provider_pools.{pool_name}.base_urls contains an invalid URL")
            normalized.append(_normalize_provider_url(url))
        if len(set(normalized)) != len(normalized):
            raise ValidationError(
                f"limits.provider_pools.{pool_name}.base_urls must not repeat URLs")
    return value


def load_config(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("runtime configuration must be a readable JSON file") from exc
    return validate_config(value)


def validate_config(value):
    validate_common(value, {"paragraph", "preserved_neighbor", "required_literals"})
    for key in ("paragraph", "preserved_neighbor"):
        _text(value.get(key), key)
    literals = value.get("required_literals")
    if not isinstance(literals, list):
        raise ValidationError("required_literals must be an explicit list")
    for literal in literals:
        _text(literal, "required_literals")
        if literal not in value["paragraph"]:
            raise ValidationError("required_literals must be present in the baseline paragraph")
    return value


def validate_common(value, extra_fields, *, retrieval=True):
    fields = {"live_dispatch_allowed", "data_classification", "allocation_mode", "project_id", "objective",
              "supplied_context", "model", "limits"}
    if retrieval:
        fields |= {"public_queries", "source_urls", "mcp_fetch_command"}
    fields |= set(extra_fields)
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
    for key in ("project_id", "objective", "supplied_context"):
        _text(value.get(key), key)
    if not isinstance(value.get("model"), dict):
        raise ValidationError("model configuration is required")
    try:
        # ``role_profiles`` is orchestration metadata, not a provider field.
        # Validate the base model after resolving it without a role so profile
        # definitions are checked but never forwarded as unknown kwargs.
        model = ModelClient(**resolve_model_config(value["model"]))
    except TypeError as exc:
        raise ValidationError("model configuration contains missing or unsupported fields") from exc
    limits = value.get("limits", {})
    if not isinstance(limits, dict):
        raise ValidationError("limits must be an object")
    provider_pools = validate_provider_pools(limits.get("provider_pools"))
    role_routes = value["model"].get("role_routes", {})
    for role_name, routes in role_routes.items():
        for route in routes:
            pool_name = route["pool"]
            pool = provider_pools.get(pool_name)
            if pool is None:
                raise ValidationError(
                    f"model.role_routes.{role_name} references an unknown provider pool: {pool_name}")
            if _normalize_provider_url(route["base_url"]) not in {
                    _normalize_provider_url(url) for url in pool["base_urls"]}:
                raise ValidationError(
                    f"model.role_routes.{role_name}.{route['id']} base_url is not registered in provider pool {pool_name}")
    integer_limits = ["max_rounds", "max_result_bytes", "concurrent_calls"]
    if retrieval:
        integer_limits += ["search_results", "max_capture_chars", "max_source_bytes"]
    for key in integer_limits:
        if type(limits.get(key)) is not int or limits[key] <= 0:
            raise ValidationError(f"limits.{key} must be a positive integer")
    time_limits = ["wall_clock_seconds", "checkpoint_seconds"]
    if retrieval:
        time_limits += ["retrieval_timeout_seconds"]
    for key in time_limits:
        if type(limits.get(key)) not in (int, float) or not math.isfinite(limits[key]) or limits[key] <= 0:
            raise ValidationError(f"limits.{key} must be finite and positive")
    if limits["concurrent_calls"] < 2:
        raise ValidationError("capacity must cover one worker and its independently reserved verification call")
    if "worker_concurrency" in limits:
        if type(limits["worker_concurrency"]) is not int or limits["worker_concurrency"] <= 0:
            raise ValidationError("limits.worker_concurrency must be a positive integer")
        if limits["worker_concurrency"] > limits["concurrent_calls"] - 1:
            raise ValidationError("limits.worker_concurrency cannot exceed worker capacity")
    if retrieval and model.timeout_seconds >= limits["wall_clock_seconds"]:
        raise ValidationError("model timeout must fit within the run deadline")
    if not retrieval:
        return value
    for key in ("public_queries", "source_urls"):
        if not isinstance(value.get(key), list) or not value[key]:
            raise ValidationError(f"{key} must be an explicit list")
        for item in value[key]:
            _text(item, key)
    mcp = value.get("mcp_fetch_command")
    if not isinstance(mcp, list) or not mcp or any(not isinstance(p, str) or not p for p in mcp):
        raise ValidationError("mcp_fetch_command must be an explicit executable argument list")
    return value


def configured_worker_slots(limits):
    """Return the effective worker count while preserving verification capacity.

    ``concurrent_calls`` includes one reserved slot for an independent
    verification call.  Deployments whose provider serializes requests can set
    ``worker_concurrency`` to one without falsifying that capacity reservation.
    """
    if not isinstance(limits, dict):
        raise ValidationError("limits must be an object")
    capacity = limits.get("concurrent_calls")
    if type(capacity) is not int or capacity < 2:
        raise ValidationError("limits.concurrent_calls must leave one worker and one verification slot")
    configured = limits.get("worker_concurrency", capacity - 1)
    if type(configured) is not int or configured <= 0 or configured > capacity - 1:
        raise ValidationError("limits.worker_concurrency must be between one and worker capacity")
    return configured
