"""Bounded calls to explicitly configured Ollama or compatible GPU servers."""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import base64
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import sqlite3
import threading
import time
import urllib.parse

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import json_object


SAMPLING_FIELDS = frozenset({
    "temperature", "top_p", "seed", "presence_penalty", "frequency_penalty",
})
OLLAMA_SAMPLING_FIELDS = frozenset({"temperature", "top_p", "seed"})
MODEL_CONFIG_FIELDS = frozenset({
    "base_url", "model", "protocol", "timeout_seconds", "max_output_tokens",
    "context_window_tokens", "max_input_tokens",
    "auth_env", "max_response_bytes", "reasoning_effort", "output_format",
    "max_image_bytes", "max_request_bytes", "max_retries", "retry_backoff_seconds",
    "cache_prompt", "model_call_budget_path", "model_call_budget_key",
    "model_call_budget_limit",
}) | SAMPLING_FIELDS
ROLE_ROUTE_FIELDS = frozenset({"id", "pool"}) | MODEL_CONFIG_FIELDS
MODEL_CALL_BUDGET_FIELDS = frozenset({
    "model_call_budget_path", "model_call_budget_key", "model_call_budget_limit",
})
# This is deliberately a conservative, tokenizer-independent preflight.  The
# runtime does not install a tokenizer for every configured provider, so it
# reserves three UTF-8 bytes per input token plus a small chat-template margin.
# The provider's reported ``prompt_tokens`` remains the authoritative observed
# usage after a request completes.
CONTEXT_ESTIMATOR_BYTES_PER_TOKEN = 3
CONTEXT_ESTIMATOR_OVERHEAD_TOKENS = 128
IMAGE_CONTEXT_TOKEN_RESERVE = 4096
# OpenAI-compatible providers commonly expose ``seed`` as a signed int64.
# Keep internally derived seeds inside that wire-level contract so a valid
# exploration hash cannot become a provider-side 400.
MAX_PROVIDER_SEED = (1 << 63) - 1

# Sampling changes how a role explores or checks a response; it does not
# replace the role's prompt or its validation contract.  These defaults are
# deliberately modest so a model can vary the search direction while the
# evidence and review roles remain conservative.  A model configuration may
# override any profile below through ``role_profiles``.
DEFAULT_ROLE_PROFILES = {
    "research.frontier-seed-planner": {"temperature": 1.35, "top_p": 0.97, "presence_penalty": 0.45},
    "topic_discovery": {"temperature": 1.1, "top_p": 0.95, "presence_penalty": 0.2},
    "research.topic-discovery": {"temperature": 1.1, "top_p": 0.95, "presence_penalty": 0.2},
    "research.search-planner": {"temperature": 1.0, "top_p": 0.95, "presence_penalty": 0.15},
    "methods.blind-search-planner": {"temperature": 1.05, "top_p": 0.95, "presence_penalty": 0.2},
    "research.literature-mapper": {"temperature": 0.25, "top_p": 0.9},
    "research.literature-reviewer": {"temperature": 0.25, "top_p": 0.9},
    "research.topic-maturity-reviewer": {"temperature": 0.2, "top_p": 0.9},
    "research.topic-source-challenger": {"temperature": 0.15, "top_p": 0.9},
    "strategy.interpretation": {"temperature": 0.75, "top_p": 0.92},
    "strategy.argument": {"temperature": 0.7, "top_p": 0.92},
    "strategy.argument-reviewer": {"temperature": 0.2, "top_p": 0.9},
    "editorial.writer": {"temperature": 0.65, "top_p": 0.92},
    "scientific-author": {"temperature": 0.65, "top_p": 0.92},
    "editorial.surgical-editor": {"temperature": 0.45, "top_p": 0.9},
    "review.science": {"temperature": 0.2, "top_p": 0.9},
    "review.methods": {"temperature": 0.2, "top_p": 0.9},
    "review.ai_smell": {"temperature": 0.8, "top_p": 0.95, "presence_penalty": 0.2},
    "review.human_scientist": {"temperature": 0.35, "top_p": 0.9},
    "review.editorial_compression": {"temperature": 0.25, "top_p": 0.9},
    "review.journal_editor": {"temperature": 0.2, "top_p": 0.9},
    "review.arbiter": {"temperature": 0.15, "top_p": 0.9},
    "review.synthesizer": {"temperature": 0.3, "top_p": 0.9},
}


def _validate_sampling_options(options, *, name="sampling options"):
    """Validate provider sampling controls before a request is dispatched."""
    if not isinstance(options, dict):
        raise ValidationError(f"{name} must be an object")
    unknown = set(options) - SAMPLING_FIELDS
    if unknown:
        raise ValidationError(f"{name} contains unsupported fields: {', '.join(sorted(unknown))}")
    for field in ("temperature", "top_p", "presence_penalty", "frequency_penalty"):
        if field not in options:
            continue
        value = options[field]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValidationError(f"{name}.{field} must be finite")
        if field == "temperature" and not 0 <= value <= 2:
            raise ValidationError(f"{name}.temperature must be between 0 and 2")
        if field == "top_p" and not 0 < value <= 1:
            raise ValidationError(f"{name}.top_p must be greater than 0 and at most 1")
        if field in {"presence_penalty", "frequency_penalty"} and not -2 <= value <= 2:
            raise ValidationError(f"{name}.{field} must be between -2 and 2")
    if "seed" in options and (
            type(options["seed"]) is not int
            or not 0 <= options["seed"] <= MAX_PROVIDER_SEED):
        raise ValidationError(
            f"{name}.seed must be an integer between 0 and {MAX_PROVIDER_SEED}")
    return options


def _validate_model_call_budget(config, *, name="model call budget"):
    """Validate an optional durable cap for one model-family call ledger."""
    present = {
        key for key in MODEL_CALL_BUDGET_FIELDS
        if key in config and config[key] is not None
    }
    if not present:
        return config
    if present != MODEL_CALL_BUDGET_FIELDS:
        raise ValidationError(
            f"{name} requires model_call_budget_path, model_call_budget_key, "
            "and model_call_budget_limit together")
    path = config["model_call_budget_path"]
    if (not isinstance(path, str) or not path.strip()
            or not Path(path).is_absolute()):
        raise ValidationError(f"{name} path must be an absolute file path")
    key = config["model_call_budget_key"]
    if not isinstance(key, str) or not key.strip():
        raise ValidationError(f"{name} key must be a nonempty string")
    limit = config["model_call_budget_limit"]
    if type(limit) is not int or limit <= 0:
        raise ValidationError(f"{name} limit must be a positive integer")
    return config


def _budget_config(config):
    """Return the configured budget fields, or ``None`` when uncapped."""
    present = {
        key for key in MODEL_CALL_BUDGET_FIELDS
        if key in config and config[key] is not None
    }
    if not present:
        return None
    _validate_model_call_budget(config)
    return {
        "path": config["model_call_budget_path"],
        "key": config["model_call_budget_key"],
        "limit": config["model_call_budget_limit"],
    }


def model_call_budget_available(config):
    """Check a durable model-call cap without reserving a call.

    A missing ledger means no call has been reserved yet.  The atomic reserve
    operation remains authoritative when concurrent workers race at the cap.
    """
    budget = _budget_config(config)
    if budget is None:
        return True
    path = Path(budget["path"])
    if not path.exists():
        return True
    connection = None
    try:
        connection = sqlite3.connect(str(path), timeout=5.0)
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("model_call_budgets",),
        ).fetchone()
        if table is None:
            return True
        row = connection.execute(
            "SELECT max_calls, used_calls FROM model_call_budgets WHERE budget_key=?",
            (budget["key"],),
        ).fetchone()
    except sqlite3.Error as exc:
        raise ValidationError(f"{path} model-call budget ledger is unreadable") from exc
    finally:
        if connection is not None:
            connection.close()
    if row is None:
        return True
    if row[0] != budget["limit"]:
        raise ValidationError(
            f"{path} model-call budget limit conflicts with configured limit")
    return row[1] < row[0]


def _reserve_model_call_budget(config):
    """Atomically spend one model-call budget before provider I/O."""
    budget = _budget_config(config)
    if budget is None:
        return
    path = Path(budget["path"])
    connection = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=30.0)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS model_call_budgets ("
            "budget_key TEXT PRIMARY KEY, max_calls INTEGER NOT NULL, "
            "used_calls INTEGER NOT NULL)"
        )
        # Serialize the read/insert-or-update decision.  Without an
        # immediate transaction, two workers can both observe a remaining
        # slot and race at the cap.
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT max_calls, used_calls FROM model_call_budgets WHERE budget_key=?",
            (budget["key"],),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO model_call_budgets(budget_key, max_calls, used_calls) "
                "VALUES (?, ?, 1)",
                (budget["key"], budget["limit"]),
            )
            connection.commit()
            return
        if row[0] != budget["limit"]:
            raise ModelCallError(
                "model call budget limit conflicts with the existing ledger",
                outcome_known=True,
            )
        updated = connection.execute(
            "UPDATE model_call_budgets SET used_calls=used_calls+1 "
            "WHERE budget_key=? AND used_calls < max_calls",
            (budget["key"],),
        )
        if updated.rowcount != 1:
            connection.rollback()
            raise ModelCallError(
                f"model call budget exhausted: {budget['key']}",
                outcome_known=True,
            )
        connection.commit()
    except ModelCallError:
        if connection is not None:
            connection.rollback()
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ModelCallError(
            f"model call budget ledger unavailable: {path}",
            outcome_known=True,
        ) from exc
    finally:
        if connection is not None:
            connection.close()


def _validate_role_models(role_models):
    """Validate partial provider configurations used by named runtime roles."""
    if not isinstance(role_models, dict):
        raise ValidationError("model.role_models must be an object")
    for role_name, selected in role_models.items():
        if not isinstance(role_name, str) or not role_name.strip():
            raise ValidationError("model.role_models keys must be nonempty strings")
        if not isinstance(selected, dict) or not selected:
            raise ValidationError(f"model.role_models.{role_name} must be a nonempty object")
        unknown = set(selected) - MODEL_CONFIG_FIELDS
        if unknown:
            raise ValidationError(
                f"model.role_models.{role_name} contains unsupported fields: "
                + ", ".join(sorted(unknown)))
        _validate_sampling_options(
            {key: value for key, value in selected.items() if key in SAMPLING_FIELDS},
            name=f"model.role_models.{role_name} sampling options")
        if "protocol" in selected and selected["protocol"] not in {"ollama", "openai_compatible"}:
            raise ValidationError(f"model.role_models.{role_name}.protocol is invalid")
        if "base_url" in selected:
            base_url = selected["base_url"]
            parsed = urllib.parse.urlsplit(base_url) if isinstance(base_url, str) else None
            if (parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValidationError(f"model.role_models.{role_name}.base_url is invalid")
        if "model" in selected and (
                not isinstance(selected["model"], str)
                or not selected["model"].strip()
                or selected["model"] == "runtime_required"):
            raise ValidationError(f"model.role_models.{role_name}.model must be explicit")
        for field in ("timeout_seconds", "retry_backoff_seconds"):
            if field in selected and (
                    type(selected[field]) not in (int, float)
                    or not math.isfinite(selected[field])
                    or selected[field] < 0
                    or (field == "timeout_seconds" and selected[field] == 0)):
                raise ValidationError(f"model.role_models.{role_name}.{field} is invalid")
        for field in ("max_output_tokens", "max_response_bytes", "max_image_bytes",
                      "max_request_bytes", "context_window_tokens", "max_input_tokens"):
            if field in selected and selected[field] is not None and (
                    type(selected[field]) is not int or selected[field] <= 0):
                raise ValidationError(f"model.role_models.{role_name}.{field} is invalid")
        if "max_retries" in selected and (
                type(selected["max_retries"]) is not int or not 0 <= selected["max_retries"] <= 8):
            raise ValidationError(f"model.role_models.{role_name}.max_retries is invalid")
        if "auth_env" in selected and selected["auth_env"] is not None and (
                not isinstance(selected["auth_env"], str) or not selected["auth_env"]):
            raise ValidationError(f"model.role_models.{role_name}.auth_env is invalid")
        if "cache_prompt" in selected and type(selected["cache_prompt"]) is not bool:
            raise ValidationError(f"model.role_models.{role_name}.cache_prompt must be boolean")
        _validate_model_call_budget(
            selected, name=f"model.role_models.{role_name} call budget")
        if "reasoning_effort" in selected and selected["reasoning_effort"] not in {
                None, "none", "low", "medium", "high", "xhigh"}:
            raise ValidationError(f"model.role_models.{role_name}.reasoning_effort is invalid")
        if "output_format" in selected and selected["output_format"] not in {None, "json_object"}:
            raise ValidationError(f"model.role_models.{role_name}.output_format is invalid")
    return role_models


def _validate_role_model_fallbacks(fallbacks):
    """Validate model alternatives used when a named model cap is exhausted."""
    if not isinstance(fallbacks, dict):
        raise ValidationError("model.role_model_fallbacks must be an object")
    for role_name, alternatives in fallbacks.items():
        if not isinstance(role_name, str) or not role_name.strip():
            raise ValidationError("model.role_model_fallbacks keys must be nonempty strings")
        if not isinstance(alternatives, list) or not alternatives:
            raise ValidationError(
                f"model.role_model_fallbacks.{role_name} must be a nonempty list")
        _validate_role_models({role_name: alternative for alternative in alternatives})
    return fallbacks


def _validate_role_routes(role_routes):
    """Validate explicit provider alternatives for one logical model role."""
    if not isinstance(role_routes, dict):
        raise ValidationError("model.role_routes must be an object")
    for role_name, routes in role_routes.items():
        if not isinstance(role_name, str) or not role_name.strip():
            raise ValidationError("model.role_routes keys must be nonempty strings")
        if not isinstance(routes, list) or not routes:
            raise ValidationError(f"model.role_routes.{role_name} must be a nonempty list")
        route_ids = set()
        for route in routes:
            if not isinstance(route, dict):
                raise ValidationError(f"model.role_routes.{role_name} entries must be objects")
            unknown = set(route) - ROLE_ROUTE_FIELDS
            if unknown:
                raise ValidationError(
                    f"model.role_routes.{role_name} contains unsupported fields: "
                    + ", ".join(sorted(unknown)))
            for field in ("id", "pool", "base_url", "model"):
                if (not isinstance(route.get(field), str) or not route[field].strip()
                        or route[field] == "runtime_required"):
                    raise ValidationError(
                        f"model.role_routes.{role_name}.{field} requires an explicit value")
            if route["id"] in route_ids:
                raise ValidationError(f"model.role_routes.{role_name} route IDs must be unique")
            route_ids.add(route["id"])
            _validate_role_models({role_name: {
                key: value for key, value in route.items()
                if key not in {"id", "pool"}
            }})
    return role_routes


def _validate_context_policy(config, *, name="model"):
    """Validate route/model context metadata after inheritance is resolved."""
    if not isinstance(config, dict):
        raise ValidationError(f"{name} configuration must be an object")
    window = config.get("context_window_tokens")
    input_limit = config.get("max_input_tokens")
    output_limit = config.get("max_output_tokens")
    if window is not None and (type(window) is not int or window <= 0):
        raise ValidationError(f"{name}.context_window_tokens must be a positive integer when configured")
    if input_limit is not None and (type(input_limit) is not int or input_limit <= 0):
        raise ValidationError(f"{name}.max_input_tokens must be a positive integer when configured")
    if window is not None:
        if type(output_limit) is not int or output_limit <= 0:
            raise ValidationError(f"{name}.max_output_tokens must be a positive integer")
        if window <= output_limit:
            raise ValidationError(
                f"{name}.context_window_tokens must leave room for max_output_tokens")
        if input_limit is not None and input_limit + output_limit > window:
            raise ValidationError(
                f"{name}.max_input_tokens plus max_output_tokens exceeds context_window_tokens")
    return {"context_window_tokens": window, "max_input_tokens": input_limit,
            "max_output_tokens": output_limit}


def estimate_input_tokens(system, prompt, *, image_count=0):
    """Return a conservative preflight estimate without provider tokenizers."""
    if not isinstance(system, str) or not isinstance(prompt, str):
        raise ValidationError("model system and prompt content must be strings")
    if type(image_count) is not int or image_count < 0:
        raise ValidationError("model image count must be a non-negative integer")
    text_bytes = len(system.encode("utf-8")) + len(prompt.encode("utf-8"))
    return (
        math.ceil(text_bytes / CONTEXT_ESTIMATOR_BYTES_PER_TOKEN)
        + CONTEXT_ESTIMATOR_OVERHEAD_TOKENS
        + image_count * IMAGE_CONTEXT_TOKEN_RESERVE
    )


def model_context_error(config, *, system, prompt, image_count=0):
    """Return a dispatch-blocking context error, or ``None`` when it fits.

    ``context_window_tokens`` is the provider's total input-plus-output window.
    ``max_input_tokens`` is an optional stricter input admission ceiling.  Both
    are metadata for admission control; they are not sent as unsupported
    provider request fields such as Ollama's ``num_ctx``.
    """
    policy = _validate_context_policy(config)
    window = policy["context_window_tokens"]
    input_limit = policy["max_input_tokens"]
    if window is None and input_limit is None:
        return None
    estimated = estimate_input_tokens(system, prompt, image_count=image_count)
    allowed = input_limit if input_limit is not None else float("inf")
    if window is not None:
        allowed = min(allowed, window - policy["max_output_tokens"])
    if estimated <= allowed:
        return None
    model_name = config.get("model", "configured model")
    limit_text = f"{int(allowed)} input tokens"
    window_text = f"; context window {window} with max output {policy['max_output_tokens']}"
    return (
        f"model context budget exceeded for {model_name}: conservative input estimate "
        f"{estimated} tokens exceeds {limit_text}{window_text}"
    )


def resolve_model_config(model, *, role=None, overrides=None):
    """Resolve a model config plus a role's provider and sampling profiles.

    The resolver keeps orchestration metadata out of the provider payload while
    allowing one shared model file to route named roles to different compatible
    endpoints or model aliases.  Role model selections inherit unspecified
    provider settings from the base config.  Explicit global sampling fields
    win over built-in defaults; the selected role model's sampling fields,
    named role profile, and call-site overrides then win in that order.
    """
    if not isinstance(model, dict):
        raise ValidationError("model configuration must be an object")
    base = dict(model)
    role_models = base.pop("role_models", {})
    _validate_role_models(role_models)
    role_model_fallbacks = base.pop("role_model_fallbacks", {})
    _validate_role_model_fallbacks(role_model_fallbacks)
    role_routes = base.pop("role_routes", {})
    _validate_role_routes(role_routes)
    profiles = base.pop("role_profiles", {})
    if not isinstance(profiles, dict):
        raise ValidationError("model.role_profiles must be an object")
    global_sampling = {key: base.pop(key) for key in list(base) if key in SAMPLING_FIELDS}
    for profile_name, profile in profiles.items():
        if not isinstance(profile_name, str) or not profile_name.strip():
            raise ValidationError("model.role_profiles keys must be nonempty strings")
        _validate_sampling_options(profile, name=f"model.role_profiles.{profile_name}")
    _validate_sampling_options(global_sampling, name="model sampling options")
    if overrides is not None:
        _validate_sampling_options(overrides, name="sampling overrides")
    selected_model = role_models.get(role) if role is not None else None
    if selected_model is not None and not model_call_budget_available(selected_model):
        alternatives = role_model_fallbacks.get(role, [])
        selected_model = next(
            (alternative for alternative in alternatives
             if model_call_budget_available(alternative)),
            selected_model,
        )
    selected_sampling = {}
    if selected_model is not None:
        selected_model = dict(selected_model)
        selected_sampling = {
            key: selected_model.pop(key)
            for key in list(selected_model) if key in SAMPLING_FIELDS
        }
        base.update(selected_model)
    sampling = dict(DEFAULT_ROLE_PROFILES.get(role, {}))
    sampling.update(global_sampling)
    sampling.update(selected_sampling)
    if role is not None:
        sampling.update(profiles.get(role, {}))
    if overrides:
        sampling.update(overrides)
    _validate_sampling_options(sampling)
    base.update(sampling)
    return base


class ModelCallError(RuntimeError):
    """An invocation failed; unknown outcomes must retain their reservation.

    ``status_code`` and ``retry_after_seconds`` are deliberately kept on the
    typed error instead of being inferred from the rendered message.  The
    orchestration layer can then distinguish a provider-wide 429 from a
    malformed response and reroute the same logical assignment safely.
    """
    def __init__(self, message, *, outcome_known=False, attempts=0,
                 elapsed_seconds=None, status_code=None,
                 retry_after_seconds=None):
        super().__init__(message)
        self.outcome_known = outcome_known
        self.attempts = attempts if type(attempts) is int and attempts >= 0 else 0
        self.elapsed_seconds = (
            float(elapsed_seconds)
            if type(elapsed_seconds) in (int, float) and math.isfinite(elapsed_seconds)
            and elapsed_seconds >= 0 else None
        )
        self.status_code = (
            status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        )
        self.retry_after_seconds = (
            float(retry_after_seconds)
            if type(retry_after_seconds) in (int, float)
            and math.isfinite(retry_after_seconds) and retry_after_seconds >= 0
            else None
        )


class _ProviderHTTPError(RuntimeError):
    """A provider response with an HTTP status other than 200."""
    def __init__(self, code, retry_after=None):
        super().__init__(f"model HTTP request failed with status {code}")
        self.code = code
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                delay = None
        self.retry_after = max(0.0, delay) if delay is not None and math.isfinite(delay) else None


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    usage: dict
    elapsed_seconds: float
    finish_reason: str
    request_attempts: int = 1

    def json_object(self, *, allow_missing_closers=False):
        return json_object(self.text, "model output", model_envelope=True,
                           allow_missing_closers=allow_missing_closers)


def _cache_usage(response):
    """Normalize prompt-cache counters from compatible provider responses."""
    if not isinstance(response, dict):
        return {}
    provider_usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    details = provider_usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = response.get("prompt_tokens_details")
    containers = [provider_usage, details, response]
    fields = {
        "cache_read_tokens": ("cached_tokens", "cache_read_input_tokens", "prompt_cache_hit_tokens"),
        "cache_write_tokens": ("created_cache_tokens", "cache_creation_input_tokens",
                                "cache_write_input_tokens", "prompt_cache_write_tokens"),
    }
    normalized = {}
    for target, names in fields.items():
        for container in containers:
            if not isinstance(container, dict):
                continue
            value = next((container[name] for name in names if name in container), None)
            if type(value) is int and value >= 0:
                normalized[target] = value
                break
    return normalized


class ModelClient:
    def __init__(self, *, base_url: str, model: str, protocol: str,
                 timeout_seconds: float, max_output_tokens: int,
                 context_window_tokens: int | None = None,
                 max_input_tokens: int | None = None,
                 auth_env: str | None = None, max_response_bytes: int = 2_000_000,
                 reasoning_effort: str | None = None, output_format: str | None = None,
                 max_image_bytes: int = 7_000_000, max_request_bytes: int = 10_000_000,
                 max_retries: int = 2, retry_backoff_seconds: float = 1.0,
                 temperature: float | None = None, top_p: float | None = None,
                 seed: int | None = None, presence_penalty: float | None = None,
                 frequency_penalty: float | None = None,
                 cache_prompt: bool | None = None,
                 model_call_budget_path: str | None = None,
                 model_call_budget_key: str | None = None,
                 model_call_budget_limit: int | None = None):
        if not isinstance(base_url, str):
            raise ValidationError("model base_url must be a URL string")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValidationError("model base_url must be an HTTP(S) URL without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValidationError("model base_url cannot contain query parameters or fragments")
        if protocol not in {"ollama", "openai_compatible"}:
            raise ValidationError("model protocol must be ollama or openai_compatible")
        if not isinstance(model, str) or not model.strip() or model == "runtime_required":
            raise ValidationError("an explicit model name is required")
        if type(max_output_tokens) is not int or max_output_tokens <= 0:
            raise ValidationError("max_output_tokens must be a positive integer")
        _validate_context_policy({
            "model": model,
            "max_output_tokens": max_output_tokens,
            "context_window_tokens": context_window_tokens,
            "max_input_tokens": max_input_tokens,
        })
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValidationError("model timeout must be finite and positive")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValidationError("response byte limit must be a positive integer")
        if type(max_image_bytes) is not int or max_image_bytes <= 0:
            raise ValidationError("image byte limit must be a positive integer")
        if type(max_request_bytes) is not int or max_request_bytes <= 0:
            raise ValidationError("request byte limit must be a positive integer")
        if max_image_bytes >= max_request_bytes:
            raise ValidationError("image byte limit must leave room inside the request byte limit")
        if type(max_retries) is not int or max_retries < 0 or max_retries > 8:
            raise ValidationError("max_retries must be an integer between 0 and 8")
        if type(retry_backoff_seconds) not in (int, float) or not math.isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0:
            raise ValidationError("retry_backoff_seconds must be finite and non-negative")
        if reasoning_effort is not None and (
            not isinstance(reasoning_effort, str)
            or reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}
        ):
            raise ValidationError("reasoning_effort must be none, low, medium, high, or xhigh when configured")
        if output_format is not None and output_format != "json_object":
            raise ValidationError("output_format must be json_object when configured")
        if cache_prompt is not None and type(cache_prompt) is not bool:
            raise ValidationError("cache_prompt must be boolean when configured")
        _validate_model_call_budget({
            "model_call_budget_path": model_call_budget_path,
            "model_call_budget_key": model_call_budget_key,
            "model_call_budget_limit": model_call_budget_limit,
        }, name="model call budget")
        sampling = {
            key: value for key, value in {
                "temperature": temperature, "top_p": top_p, "seed": seed,
                "presence_penalty": presence_penalty, "frequency_penalty": frequency_penalty,
            }.items() if value is not None
        }
        _validate_sampling_options(sampling)
        if protocol != "openai_compatible" and (reasoning_effort is not None or output_format is not None):
            raise ValidationError("reasoning_effort and output_format require the openai_compatible protocol")
        if auth_env is not None and (not isinstance(auth_env, str) or not auth_env or not os.environ.get(auth_env)):
            raise ValidationError("configured model authentication environment variable is absent")
        self.base_url, self.model, self.protocol = base_url.rstrip("/"), model, protocol
        self.timeout_seconds, self.max_output_tokens = timeout_seconds, max_output_tokens
        self.context_window_tokens, self.max_input_tokens = (
            context_window_tokens, max_input_tokens)
        self.max_response_bytes, self.auth_env = max_response_bytes, auth_env
        self.reasoning_effort, self.output_format = reasoning_effort, output_format
        self.max_image_bytes, self.max_request_bytes = max_image_bytes, max_request_bytes
        self.max_retries, self.retry_backoff_seconds = max_retries, float(retry_backoff_seconds)
        self.cache_prompt = cache_prompt
        self.temperature = temperature
        self.top_p = top_p
        self.seed = seed
        self.presence_penalty = presence_penalty
        self.frequency_penalty = frequency_penalty
        self.model_call_budget_path = model_call_budget_path
        self.model_call_budget_key = model_call_budget_key
        self.model_call_budget_limit = model_call_budget_limit

    @staticmethod
    def _read_image(image):
        if not isinstance(image, dict) or set(image) != {"path", "media_type", "sha256"}:
            raise ValidationError("each model image requires exactly path, media_type, and sha256")
        path, media_type, expected = image["path"], image["media_type"], image["sha256"]
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or media_type not in {"image/png", "image/jpeg"}
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValidationError("model image descriptor is invalid")
        try:
            resolved = Path(path).resolve(strict=True)
            if not resolved.is_file():
                raise OSError("not a regular file")
            body = resolved.read_bytes()
        except OSError as exc:
            raise ValidationError("model image is unavailable") from exc
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            raise ValidationError("model image content does not match its pinned SHA-256")
        if media_type == "image/png" and not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValidationError("model image media type does not match PNG bytes")
        if media_type == "image/jpeg" and not body.startswith(b"\xff\xd8\xff"):
            raise ValidationError("model image media type does not match JPEG bytes")
        return body, media_type

    def complete(self, *, system: str, prompt: str, images=None) -> ModelResult:
        if not isinstance(system, str) or not isinstance(prompt, str):
            raise ValidationError("model system and prompt content must be strings")
        images = [] if images is None else images
        if not isinstance(images, list) or len(images) > 16:
            raise ValidationError("model images must be a list containing at most 16 items")
        if images and self.protocol != "openai_compatible":
            raise ValidationError("multimodal image input requires the openai_compatible protocol")
        context_error = model_context_error(
            {"model": self.model, "max_output_tokens": self.max_output_tokens,
             "context_window_tokens": self.context_window_tokens,
             "max_input_tokens": self.max_input_tokens},
            system=system, prompt=prompt, image_count=len(images))
        if context_error:
            raise ValidationError(context_error)
        parts, total = [{"type": "text", "text": prompt}], 0
        for descriptor in images:
            raw, media_type = self._read_image(descriptor)
            total += len(raw)
            if total > self.max_image_bytes:
                raise ValidationError("combined model images exceed the configured byte limit")
            encoded = base64.b64encode(raw).decode("ascii")
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{media_type};base64,{encoded}"}})
        user_content = parts if images else prompt
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user_content}]
        body = {"model": self.model, "messages": messages, "stream": False}
        sampling = {
            key: value for key, value in {
                "temperature": self.temperature, "top_p": self.top_p, "seed": self.seed,
                "presence_penalty": self.presence_penalty,
                "frequency_penalty": self.frequency_penalty,
            }.items() if value is not None
        }
        if self.protocol == "ollama":
            path = "/api/chat"
            # Ollama's native options expose temperature/top-p/seed but not
            # the OpenAI presence/frequency penalty names.
            body["options"] = {
                "num_predict": self.max_output_tokens,
                **{key: value for key, value in sampling.items() if key in OLLAMA_SAMPLING_FIELDS},
            }
        else:
            path = "/chat/completions"
            body["max_tokens"] = self.max_output_tokens
            body.update(sampling)
            if self.reasoning_effort is not None:
                body["reasoning_effort"] = self.reasoning_effort
            if self.output_format is not None:
                body["response_format"] = {"type": self.output_format}
        if self.cache_prompt is not None:
            # Ollama/llama.cpp-compatible servers use this hint to reuse the
            # longest matching token prefix across requests. Other compatible
            # gateways may ignore it; the request remains semantically valid.
            body["cache_prompt"] = self.cache_prompt
        headers = {"Content-Type": "application/json"}
        if self.auth_env:
            key = os.environ.get(self.auth_env)
            if not key:
                raise ModelCallError("model authentication environment variable is absent", outcome_known=True)
            headers["Authorization"] = "Bearer " + key
        wire = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        if len(wire) > self.max_request_bytes:
            raise ValidationError("model request exceeds the configured byte limit")
        parsed_base = urllib.parse.urlsplit(self.base_url)
        connection_type = (http.client.HTTPSConnection
                           if parsed_base.scheme == "https" else http.client.HTTPConnection)
        request_path = parsed_base.path.rstrip("/") + path
        if not request_path.startswith("/"):
            request_path = "/" + request_path
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        # Account-wide backpressure belongs to the scheduler, which can retain
        # siblings and wait without submitting the same prompt again.
        retryable_statuses = {408, 425, 500, 502, 503, 504}
        attempt = 0
        attempts_made = 0
        parsed = None

        def failure(message, *, outcome_known=False, status_code=None,
                    retry_after_seconds=None):
            return ModelCallError(
                message, outcome_known=outcome_known, attempts=attempts_made,
                elapsed_seconds=time.monotonic() - started,
                status_code=status_code,
                retry_after_seconds=retry_after_seconds,
            )

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise failure("model request deadline exceeded") from None
            _reserve_model_call_budget({
                "model_call_budget_path": self.model_call_budget_path,
                "model_call_budget_key": self.model_call_budget_key,
                "model_call_budget_limit": self.model_call_budget_limit,
            })
            attempts_made += 1
            connection = connection_type(parsed_base.hostname, parsed_base.port,
                                         timeout=max(0.1, remaining))
            response = None
            timeout_timer = None
            expired = threading.Event()

            def expire_request():
                expired.set()
                transport_socket = connection.sock
                if transport_socket is None and response is not None:
                    raw = getattr(getattr(response, "fp", None), "raw", None)
                    transport_socket = getattr(raw, "_sock", None)
                    if transport_socket is None:
                        transport_socket = getattr(getattr(response, "fp", None), "_sock", None)
                if transport_socket is not None:
                    try:
                        transport_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                if response is not None:
                    try:
                        response.close()
                    except (OSError, ValueError):
                        pass
                connection.close()

            try:
                timeout_timer = threading.Timer(
                    max(0.01, deadline - time.monotonic()), expire_request)
                timeout_timer.daemon = True
                timeout_timer.start()
                connection.request("POST", request_path, wire,
                                   headers={**headers, "Connection": "close"})
                response = connection.getresponse()
                code = response.status
                if 300 <= code < 400:
                    raise failure(
                        "model endpoint redirected; configure the final endpoint explicitly",
                        outcome_known=True)
                if code != 200:
                    raise _ProviderHTTPError(code, response.getheader("Retry-After"))
                # ``HTTPResponse.read(n)`` can legally wait for the full
                # requested amount (or for EOF) when a provider sends a
                # response in small chunks.  ``read1`` returns one currently
                # available bounded chunk, while the timer above also covers
                # HTTP header parsing and chunk-framing reads performed inside
                # ``http.client``.  Together they keep the whole transaction
                # inside one absolute request budget.
                chunks = []
                total = 0
                read_chunk = getattr(response, "read1", response.read)
                while total <= self.max_response_bytes:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise failure("model request deadline exceeded")
                    transport_socket = connection.sock
                    if transport_socket is not None:
                        transport_socket.settimeout(max(0.1, remaining))
                    try:
                        chunk = read_chunk(min(65536, self.max_response_bytes + 1 - total))
                    except (AttributeError, OSError, TimeoutError, ValueError):
                        if expired.is_set() or time.monotonic() >= deadline:
                            raise failure("model request deadline exceeded") from None
                        raise
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                raw = b"".join(chunks)
                # A peer can close a partial body at the same instant as the
                # absolute timer.  Do not let the resulting truncated JSON
                # masquerade as a provider-format error; the deadline is the
                # authoritative outcome for this transaction.
                if expired.is_set() or time.monotonic() >= deadline:
                    raise failure("model request deadline exceeded") from None
            except _ProviderHTTPError as exc:
                code = exc.code
                retry_after = exc.retry_after
                if code in retryable_statuses and attempt < self.max_retries:
                    delay = self.retry_backoff_seconds * (2 ** attempt)
                    try:
                        if retry_after is not None:
                            delay = max(delay, min(60.0, float(retry_after)))
                    except (TypeError, ValueError):
                        pass
                    if time.monotonic() + delay >= deadline:
                        raise failure(f"model HTTP request failed with status {code}",
                                      outcome_known=400 <= code < 500,
                                      status_code=code,
                                      retry_after_seconds=retry_after) from None
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise failure(f"model HTTP request failed with status {code}",
                              outcome_known=400 <= code < 500,
                              status_code=code,
                              retry_after_seconds=retry_after) from None
            except ModelCallError as exc:
                raise failure(
                    str(exc), outcome_known=exc.outcome_known,
                    status_code=exc.status_code,
                    retry_after_seconds=exc.retry_after_seconds,
                ) from None
            except (http.client.HTTPException, TimeoutError, OSError, ValueError, AttributeError) as exc:
                if expired.is_set() or time.monotonic() >= deadline:
                    raise failure("model request deadline exceeded") from None
                raise failure(f"model transport failed: {type(exc).__name__}") from None
            finally:
                if timeout_timer is not None:
                    timeout_timer.cancel()
                if response is not None:
                    try:
                        response.close()
                    except (OSError, ValueError):
                        pass
                connection.close()
            if len(raw) > self.max_response_bytes:
                raise failure("model response exceeded the configured byte limit")
            try:
                data = json.loads(raw)
                if self.protocol == "ollama":
                    if data.get("done") is not True:
                        raise ValueError("incomplete response")
                    text = data["message"]["content"]
                    reason = data.get("done_reason", "unknown")
                    usage = {k: data[source] for k, source in
                             (("input_tokens", "prompt_eval_count"), ("output_tokens", "eval_count")) if source in data}
                else:
                    choice = data["choices"][0]
                    text, reason = choice["message"]["content"], choice["finish_reason"]
                    usage = {k: data["usage"][source] for k, source in
                             (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"))
                             if source in data.get("usage", {})}
                usage.update(_cache_usage(data))
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("empty text")
                if any(type(value) is not int or value < 0 for value in usage.values()):
                    raise ValueError("invalid usage")
                if reason not in {"stop", "length", "load", "unload", "unknown"}:
                    raise ValueError("unsupported completion state")
                parsed = (text, reason, usage, data.get("model", self.model))
            except (ValueError, TypeError, KeyError, IndexError):
                # A received HTTP 200 may already have consumed a complete
                # generation. Its unknown usage must not be hidden by a
                # transparent second generation of the same request.
                raise failure("model returned an invalid or incomplete response") from None
            break
        elapsed = time.monotonic() - started
        text, reason, usage, served_model = parsed
        return ModelResult(text, served_model,
                           {"model_calls": 1, **usage}, elapsed, reason,
                           request_attempts=attempts_made)
