"""Bounded calls to explicitly configured Ollama or compatible GPU servers."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from copy import deepcopy
from contextlib import closing
from email.utils import parsedate_to_datetime
import base64
import hashlib
import http.client
import ipaddress
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
import uuid

from scisaurus.runtime.execution_policy import enforce_model_cost_limits
from scisaurus.runtime.model_dispatch import ModelSlotTimeout, model_dispatch_slot
from scisaurus.core.errors import QuotaExceededError, ValidationError
from scisaurus.core.schema import json_object


SAMPLING_FIELDS = frozenset({
    "temperature", "top_p", "seed", "presence_penalty", "frequency_penalty",
})
DEFAULT_MODEL_RATE_LIMIT_COOLDOWN_SECONDS = 60.0
MAX_MODEL_RATE_LIMIT_COOLDOWN_SECONDS = 6 * 60 * 60
_MODEL_PROVIDER_COOLDOWN_LOCK = threading.Lock()
_MODEL_PROVIDER_COOLDOWNS = {}
_MODEL_PROVIDER_COOLDOWN_GENERATIONS = {}
OLLAMA_SAMPLING_FIELDS = frozenset({"temperature", "top_p", "seed"})
MODEL_CONFIG_FIELDS = frozenset({
    "base_url", "model", "protocol", "timeout_seconds", "max_output_tokens",
    "context_window_tokens", "max_input_tokens",
    "provider_quota_scope",
    "auth_env", "max_response_bytes", "reasoning_effort", "output_format",
    "max_image_bytes", "max_request_bytes", "max_retries", "retry_backoff_seconds",
    "cache_prompt", "model_call_budget_path", "model_call_budget_key",
    "model_call_budget_limit",
    "model_call_budget_scopes",
}) | SAMPLING_FIELDS
ROLE_ROUTE_FIELDS = frozenset({"id", "pool"}) | MODEL_CONFIG_FIELDS
MODEL_CALL_BUDGET_FIELDS = frozenset({
    "model_call_budget_path", "model_call_budget_key", "model_call_budget_limit",
})
MODEL_TOKEN_BUDGET_FIELD = "model_token_budget_limits"
MODEL_BUDGET_SCOPE_FIELDS = MODEL_CALL_BUDGET_FIELDS | {MODEL_TOKEN_BUDGET_FIELD}


def is_local_qwen_route(config):
    """Identify Qwen models routed to this host's loopback inference server."""
    if not isinstance(config, dict):
        return False
    model = config.get("model")
    base_url = config.get("base_url")
    if not isinstance(model, str) or "qwen" not in model.casefold():
        return False
    if not isinstance(base_url, str):
        return False
    hostname = urllib.parse.urlsplit(base_url).hostname
    if not hostname:
        return False
    hostname = hostname.rstrip(".").casefold()
    if (hostname in {"localhost", "localhost.localdomain"}
            or hostname.endswith((".localhost", ".localhost.localdomain"))):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(hostname))
        except OSError:
            try:
                resolved = socket.getaddrinfo(
                    hostname, None, type=socket.SOCK_STREAM)
            except OSError:
                return False
            return any(
                _is_local_inference_address(record[4][0])
                for record in resolved
                if len(record) > 4 and record[4]
            )
    return _is_local_inference_address(address)


def _is_local_inference_address(value):
    try:
        address = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        return False
    if address.is_loopback or address.is_unspecified:
        return True
    mapped_ipv4 = getattr(address, "ipv4_mapped", None)
    return bool(mapped_ipv4 and (mapped_ipv4.is_loopback or mapped_ipv4.is_unspecified))


def _reject_local_qwen_peer(model, peer_address):
    if (isinstance(model, str) and "qwen" in model.casefold()
            and _is_local_inference_address(peer_address)):
        raise ValidationError("local Qwen model routes are disabled")


def role_config_for(mapping, role, default=None):
    """Resolve the most specific exact or dotted-parent role configuration."""
    if not isinstance(mapping, dict) or not isinstance(role, str):
        return default
    if role in mapping:
        return mapping[role]
    parents = [key for key in mapping
               if isinstance(key, str) and role.startswith(f"{key}.")]
    if not parents:
        return default
    return mapping[max(parents, key=len)]


def load_model_config(model):
    """Materialize a shared routing file with explicit call-level overrides."""
    if not isinstance(model, dict):
        raise ValidationError("model configuration must be an object")
    if "config_path" not in model:
        return model
    path = model["config_path"]
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValidationError("model.config_path must be an absolute file path")
    overrides = {key: value for key, value in model.items() if key != "config_path"}
    allowed = (MODEL_CONFIG_FIELDS | SAMPLING_FIELDS) - {
        "model", "base_url", "protocol", "auth_env", "provider_quota_scope"}
    if set(overrides) - allowed:
        raise ValidationError("shared model routing cannot be overridden inline: "
                              + ", ".join(sorted(set(overrides) - allowed)))
    try:
        shared = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("model.config_path must name a readable JSON object") from exc
    if not isinstance(shared, dict) or "config_path" in shared:
        raise ValidationError("shared model configuration must be a direct JSON object")
    return merge_model_config(shared, overrides)


def role_routes_for(model, role):
    """Return inherited role routes after excluding local Qwen inference."""
    if not isinstance(model, dict):
        return []
    model = load_model_config(model)
    routes = role_config_for(model.get("role_routes"), role, [])
    if not isinstance(routes, list):
        return []
    base = {key: value for key, value in model.items()
            if key not in {"role_models", "role_model_fallbacks", "role_routes",
                           "role_profiles", "provider_cooldown_fallback"}}
    safe = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        candidate = dict(base)
        candidate.update({key: value for key, value in route.items()
                          if key not in {"id", "pool"}})
        if not is_local_qwen_route(candidate):
            safe.append(route)
    return safe


def reject_local_qwen_route(config):
    if is_local_qwen_route(config):
        raise ValidationError("local Qwen model routes are disabled")
# This is deliberately a conservative, tokenizer-independent preflight.  The
# runtime does not install a tokenizer for every configured provider, so it
# reserves three UTF-8 bytes per input token plus a small chat-template margin.
# The provider's reported ``prompt_tokens`` remains the authoritative observed
# usage after a request completes.
CONTEXT_ESTIMATOR_BYTES_PER_TOKEN = 3
CONTEXT_ESTIMATOR_OVERHEAD_TOKENS = 128
IMAGE_CONTEXT_TOKEN_RESERVE = 4096
MODEL_CONTINUATION_INSTRUCTION = (
    "Continue the preceding assistant response exactly from its final character. "
    "Return only the missing suffix; do not repeat or summarize any preceding text."
)


def effective_model_timeout(configured_timeout, *deadline_bounds):
    """Use the model route's timeout, bounded only by explicit task deadlines.

    A transport timeout configured for a route is the provider-specific limit.
    Callers may additionally bound it by a stage, mission, or assignment
    deadline.  A global fixed cap here would silently override those policies
    and turn slow-but-live generations into unknown outcomes.
    """
    values = [configured_timeout, *[item for item in deadline_bounds if item is not None]]
    if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
           for value in values):
        raise ValidationError("model request timeout bounds must be finite and positive")
    return min(float(value) for value in values)


def normalize_generated_string_list(value):
    """Repair only explicit, loss-preserving string-list serialization.

    Structured model responses sometimes encode one list item as a scalar or
    join list items with newlines/semicolons. Commas are deliberately not
    separators because they commonly occur inside prose. Invalid shapes are
    returned unchanged for the caller's schema validator to reject.
    """
    if isinstance(value, list):
        if not all(isinstance(item, str) and item.strip() for item in value):
            return value
        return list(dict.fromkeys(item.strip() for item in value))
    if not isinstance(value, str) or not value.strip():
        return value
    parts = [part.strip(" \t-*\u2022") for part in re.split(r"\r?\n+|;", value)
             if part.strip(" \t-*\u2022")]
    return list(dict.fromkeys(parts or [value.strip()]))


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
    tokens = config.get(MODEL_TOKEN_BUDGET_FIELD)
    if MODEL_TOKEN_BUDGET_FIELD in config and (
            not isinstance(tokens, dict) or set(tokens) != {"input_tokens", "output_tokens"}
            or any(type(value) is not int or value <= 0 for value in tokens.values())):
        raise ValidationError(f"{name} token limits require positive input_tokens and output_tokens")
    return config


def validate_model_budget_scope(scope):
    if (not isinstance(scope, dict) or not MODEL_CALL_BUDGET_FIELDS <= set(scope)
            or set(scope) - MODEL_BUDGET_SCOPE_FIELDS):
        raise ValidationError("model call budget scope requires a complete budget")
    return _validate_model_call_budget(scope)


def _budget_config(config):
    """Return the configured budget fields, or ``None`` when uncapped."""
    present = {
        key for key in MODEL_CALL_BUDGET_FIELDS
        if key in config and config[key] is not None
    }
    if not present:
        return None
    _validate_model_call_budget(config)
    value = {
        "path": config["model_call_budget_path"],
        "key": config["model_call_budget_key"],
        "limit": config["model_call_budget_limit"],
    }
    if MODEL_TOKEN_BUDGET_FIELD in config:
        value["token_limits"] = dict(config[MODEL_TOKEN_BUDGET_FIELD])
    return value


def _token_budget_tables(connection):
    connection.execute("CREATE TABLE IF NOT EXISTS model_token_budgets ("
        "budget_key TEXT PRIMARY KEY, max_input INTEGER NOT NULL, max_output INTEGER NOT NULL, "
        "used_input INTEGER NOT NULL, used_output INTEGER NOT NULL)")
    connection.execute("CREATE TABLE IF NOT EXISTS model_token_reservations ("
        "reservation_id TEXT PRIMARY KEY, budget_key TEXT NOT NULL, "
        "input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, state TEXT NOT NULL, "
        "observed_input INTEGER, observed_output INTEGER)")


def _effective_token_limits(connection, key, limits):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='model_token_capacity_grants'").fetchone():
        return dict(limits)
    rows = connection.execute("SELECT base_input,base_output,added_input,added_output "
        "FROM model_token_capacity_grants WHERE budget_key=?", (key,)).fetchall()
    expected = (limits["input_tokens"], limits["output_tokens"])
    if any(row[:2] != expected for row in rows):
        raise ValidationError("token capacity grant conflicts with the configured base allocation")
    return {dimension: limits[dimension] + sum(row[index+2] for row in rows)
            for index, dimension in enumerate(("input_tokens", "output_tokens"))}


def model_token_budget_limits(config):
    """Read the base allocation plus explicitly registered capacity grants."""
    budget = _budget_config(config)
    if not budget or "token_limits" not in budget:
        return {}
    path = Path(budget["path"])
    if not path.is_file():
        return dict(budget["token_limits"])
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)) as connection:
        return _effective_token_limits(connection, budget["key"], budget["token_limits"])


def grant_model_token_capacity(path, key, *, grant_id, input_tokens=0, output_tokens=0, reason):
    """Append an explicit operator grant without altering allocations or costs."""
    if (not isinstance(path, str) or not Path(path).is_absolute() or not Path(path).is_file()
            or any(not isinstance(value, str) or not value.strip() for value in (key, grant_id, reason))
            or any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens))
            or input_tokens + output_tokens == 0):
        raise ValidationError("capacity grant requires an existing absolute ledger, identity, reason and positive capacity")
    with closing(sqlite3.connect(path, timeout=30.0)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='model_token_budgets'").fetchone():
            raise ValidationError("capacity grant requires a registered token allocation")
        row = connection.execute("SELECT max_input,max_output,used_input,used_output "
                                 "FROM model_token_budgets WHERE budget_key=?", (key,)).fetchone()
        if row is None:
            raise ValidationError("capacity grant requires a registered budget owner")
        connection.execute("CREATE TABLE IF NOT EXISTS model_token_capacity_grants ("
            "grant_id TEXT PRIMARY KEY, budget_key TEXT NOT NULL, base_input INTEGER NOT NULL, "
            "base_output INTEGER NOT NULL, added_input INTEGER NOT NULL, added_output INTEGER NOT NULL, "
            "reason TEXT NOT NULL, created_at REAL NOT NULL, used_input INTEGER NOT NULL, used_output INTEGER NOT NULL)")
        expected = (key, row[0], row[1], input_tokens, output_tokens, reason)
        previous = connection.execute("SELECT budget_key,base_input,base_output,added_input,added_output,reason "
            "FROM model_token_capacity_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if previous is not None and previous != expected:
            raise ValidationError("capacity grant identity was already used for a different request")
        if previous is None:
            connection.execute("INSERT INTO model_token_capacity_grants VALUES (?,?,?,?,?,?,?,?,?,?)",
                (grant_id, *expected, time.time(), row[2], row[3]))
        receipt = connection.execute("SELECT * FROM model_token_capacity_grants WHERE grant_id=?", (grant_id,)).fetchone()
    return dict(zip(("grant_id", "budget_key", "base_input", "base_output", "added_input", "added_output",
                     "reason", "created_at", "used_input", "used_output"), receipt))


def register_model_token_budget(config, usage_floor=None):
    """Register immutable token ceilings and monotonically restore observed costs."""
    budget = _budget_config(config)
    if not budget or "token_limits" not in budget:
        return
    limits = budget["token_limits"]
    quantities = {key: (usage_floor or {}).get(key, 0) for key in limits}
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
           for value in quantities.values()):
        raise ValidationError("token budget usage floors must be nonnegative")
    floor = {key: math.ceil(value) for key, value in quantities.items()}
    Path(budget["path"]).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(budget["path"], timeout=30.0)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        _token_budget_tables(connection)
        row = connection.execute("SELECT max_input,max_output FROM model_token_budgets "
                                 "WHERE budget_key=?", (budget["key"],)).fetchone()
        if row and row != (limits["input_tokens"], limits["output_tokens"]):
            raise ValidationError("persisted model token-budget limit changed")
        connection.execute("INSERT INTO model_token_budgets VALUES (?,?,?,?,?) "
            "ON CONFLICT(budget_key) DO UPDATE SET used_input=MAX(used_input,excluded.used_input), "
            "used_output=MAX(used_output,excluded.used_output)",
            (budget["key"], limits["input_tokens"], limits["output_tokens"],
             floor["input_tokens"], floor["output_tokens"]))


def model_token_budget_usage(config):
    """Read observed token costs independently of outstanding reservations."""
    budget = _budget_config(config)
    if not budget or "token_limits" not in budget or not Path(budget["path"]).is_file():
        return {}
    with closing(sqlite3.connect(Path(budget["path"]).as_uri()+"?mode=ro", uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='model_token_budgets'").fetchone():
            return {}
        row = connection.execute("SELECT max_input,max_output,used_input,used_output "
                                 "FROM model_token_budgets WHERE budget_key=?", (budget["key"],)).fetchone()
    if row is None:
        return {}
    if row[:2] != tuple(budget["token_limits"][key] for key in ("input_tokens", "output_tokens")):
        raise ValidationError("persisted model token-budget limit changed")
    return dict(zip(("input_tokens", "output_tokens"), row[2:]))


def _settle_model_token_budgets(reserved, usage):
    """Settle a physical HTTP reservation once; unknown costs remain reserved."""
    for budget in reserved:
        reservation_id = budget.get("reservation_id")
        if reservation_id is None:
            continue
        with closing(sqlite3.connect(budget["path"], timeout=30.0)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state,observed_input,observed_output FROM model_token_reservations WHERE reservation_id=?",
                                     (reservation_id,)).fetchone()
            if row is None or row[0] == "settled":
                continue
            known = list(row[1:])
            for offset, dimension in enumerate(("input_tokens", "output_tokens")):
                value = (usage or {}).get(dimension)
                if value is None:
                    continue
                if type(value) is not int or value < 0:
                    raise ValidationError("observed model token usage must be nonnegative integers")
                if known[offset] is not None:
                    if known[offset] != value:
                        raise ValidationError("observed model token settlement is immutable")
                    continue
                suffix = "input" if offset == 0 else "output"
                connection.execute(f"UPDATE model_token_budgets SET used_{suffix}=used_{suffix}+? WHERE budget_key=?",
                                   (value, budget["key"]))
                connection.execute(f"UPDATE model_token_reservations SET observed_{suffix}=?, {dimension}=0 WHERE reservation_id=?",
                                   (value, reservation_id))
                known[offset] = value
            state = "settled" if all(value is not None for value in known) else "unknown"
            connection.execute("UPDATE model_token_reservations SET state=? WHERE reservation_id=?", (state,reservation_id))


def model_call_budget_remaining(config):
    """Read remaining durable call capacity without reserving or registering it.

    A missing ledger means no call has been reserved yet.  The atomic reserve
    operation remains authoritative when concurrent workers race at the cap.
    An uncapped configuration returns None.
    """
    budget = _budget_config(config)
    if budget is None:
        return None
    path = Path(budget["path"])
    if not path.exists():
        return budget["limit"] if enforce_model_cost_limits() else None
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("model_call_budgets",),
        ).fetchone()
        if table is None:
            return budget["limit"] if enforce_model_cost_limits() else None
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
        return budget["limit"] if enforce_model_cost_limits() else None
    if row[0] != budget["limit"]:
        raise ValidationError(
            f"{path} model-call budget limit conflicts with configured limit")
    if type(row[1]) is not int or row[1] < 0:
        raise ValidationError(f"{path} model-call budget usage is invalid")
    return max(0, row[0] - row[1]) if enforce_model_cost_limits() else None


def model_call_budget_available(config):
    """Check whether the durable call cap admits one more reservation."""
    remaining = model_call_budget_remaining(config)
    return remaining is None or remaining > 0


def _reserve_model_call_budgets(configs, *, token_reservation=None):
    """Reserve every applicable scope before I/O, undoing rejected admission."""
    owners = {}
    for config in configs:
        budget = _budget_config(config)
        if budget is None:
            continue
        identity = (str(Path(budget["path"]).resolve()), budget["key"])
        prior = owners.get(identity)
        if prior is not None:
            previous = _budget_config(prior)
            if previous["limit"] != budget["limit"] or (
                    previous.get("token_limits") is not None and budget.get("token_limits") is not None
                    and previous["token_limits"] != budget["token_limits"]):
                raise ModelCallError("model call budget scopes have conflicting limits", outcome_known=True)
            if budget.get("token_limits") is None:
                continue
        owners[identity] = config
    reserved = []
    try:
        for config in owners.values():
            receipt = _reserve_model_call_budget(config, token_reservation=token_reservation)
            reserved.append(receipt)
    except (ModelCallError, ValidationError):
        _release_model_call_budgets(reserved)
        raise
    return reserved


def _release_model_call_budgets(reserved):
    """Undo only reservations whose request was never submitted."""
    for budget in reversed(reserved):
        try:
            with closing(sqlite3.connect(budget["path"], timeout=30.0)) as connection:
                with connection:
                    connection.execute("UPDATE model_call_budgets SET used_calls=used_calls-1 "
                        "WHERE budget_key=? AND used_calls>0", (budget["key"],))
                    if budget.get("development_admission_id"):
                        connection.execute("DELETE FROM model_development_admissions WHERE admission_id=?",
                                           (budget["development_admission_id"],))
                    if budget.get("reservation_id"):
                        connection.execute("DELETE FROM model_token_reservations WHERE reservation_id=?",
                                           (budget["reservation_id"],))
        except (OSError, sqlite3.Error) as exc:
            raise ModelCallError("model call budget reservation could not be released", outcome_known=True) from exc


def _reserve_model_call_budget(config, *, token_reservation=None):
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
            row = (budget["limit"], 0)
            inserted = True
        else:
            inserted = False
        if row[0] != budget["limit"]:
            raise ModelCallError(
                "model call budget limit conflicts with the existing ledger",
                outcome_known=True,
            )
        enforce = enforce_model_cost_limits()
        updated = connection.execute(
            "UPDATE model_call_budgets SET used_calls=used_calls+1 "
            "WHERE budget_key=? AND (used_calls < max_calls OR ?)",
            (budget["key"], not enforce),
        ) if not inserted else None
        if updated is not None and updated.rowcount != 1:
            connection.rollback()
            raise ModelBudgetExceededError(
                f"model call budget exhausted: {budget['key']}",
                outcome_known=True,
                budget_admission={"path": str(path.resolve()), "key": budget["key"],
                    "dimension": "model_calls", "limit": row[0], "observed": row[1],
                    "reserved": 0, "requested": 1},
            )
        if "token_limits" in budget:
            if not isinstance(token_reservation, dict) or any(
                    type(token_reservation.get(key)) is not int or token_reservation[key] < 0
                    for key in ("input_tokens", "output_tokens")):
                raise ModelCallError("model token-budget admission requires request bounds", outcome_known=True)
            _token_budget_tables(connection)
            limits = budget["token_limits"]
            connection.execute("INSERT OR IGNORE INTO model_token_budgets VALUES (?,?,?,0,0)",
                (budget["key"], limits["input_tokens"], limits["output_tokens"]))
            tokens = connection.execute("SELECT max_input,max_output,used_input,used_output "
                "FROM model_token_budgets WHERE budget_key=?", (budget["key"],)).fetchone()
            if tokens[:2] != (limits["input_tokens"], limits["output_tokens"]):
                raise ModelCallError("model token-budget limit conflicts with existing ledger", outcome_known=True)
            effective = _effective_token_limits(connection, budget["key"], limits)
            pending = connection.execute("SELECT COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0) "
                "FROM model_token_reservations WHERE budget_key=? AND state!='settled'", (budget["key"],)).fetchone()
            for offset, dimension in enumerate(("input_tokens", "output_tokens")):
                if enforce and tokens[offset+2] + pending[offset] + token_reservation[dimension] > effective[dimension]:
                    raise ModelBudgetExceededError(f"model token budget exhausted: {budget['key']} {dimension}",
                        outcome_known=True, budget_admission={"path": str(path.resolve()), "key": budget["key"],
                            "dimension": dimension, "limit": effective[dimension], "observed": tokens[offset+2],
                            "reserved": pending[offset], "requested": token_reservation[dimension]})
            budget["reservation_id"] = uuid.uuid4().hex
            connection.execute("INSERT INTO model_token_reservations (reservation_id,budget_key,input_tokens,output_tokens,state) VALUES (?,?,?,?, 'reserved')",
                (budget["reservation_id"], budget["key"], token_reservation["input_tokens"],
                 token_reservation["output_tokens"]))
        if not enforce:
            connection.execute("CREATE TABLE IF NOT EXISTS model_development_admissions ("
                "admission_id TEXT PRIMARY KEY, budget_key TEXT NOT NULL, created_at REAL NOT NULL, "
                "observed_calls INTEGER NOT NULL)")
            budget["development_admission_id"] = uuid.uuid4().hex
            connection.execute("INSERT INTO model_development_admissions VALUES (?,?,?,?)",
                (budget["development_admission_id"], budget["key"], time.time(), row[1]+1))
        connection.commit()
        return budget
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
        if "provider_quota_scope" in selected and (
                not isinstance(selected["provider_quota_scope"], str)
                or not selected["provider_quota_scope"].strip()
                or len(selected["provider_quota_scope"]) > 160):
            raise ValidationError(
                f"model.role_models.{role_name}.provider_quota_scope is invalid")
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
    """Validate explicit per-role model alternatives for bounded failover."""
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
    ``max_input_tokens`` is an optional stricter input admission ceiling.  The
    latter is always local metadata; for the native Ollama protocol the former
    is also forwarded as ``options.num_ctx`` so the server allocates the same
    role-specific window.  OpenAI-compatible bridges keep it local because
    they do not have a portable per-request context field.
    """
    budget = model_context_budget(
        config, system=system, prompt=prompt, image_count=image_count)
    if budget["allowed_input_tokens"] is None or budget["fits"]:
        return None
    model_name = budget["model"]
    limit_text = f"{budget['allowed_input_tokens']} input tokens"
    window_text = (
        f"; context window {budget['context_window_tokens']} with max output "
        f"{budget['max_output_tokens']}")
    return (
        f"model context budget exceeded for {model_name}: conservative input estimate "
        f"{budget['estimated_input_tokens']} tokens exceeds {limit_text}{window_text}"
    )


def model_context_budget(config, *, system, prompt, image_count=0):
    """Return the resolved, tokenizer-independent input admission budget.

    Callers that build a structured packet before dispatch can use this to
    project the packet into a smaller role-specific view.  Keeping this
    calculation beside :func:`model_context_error` prevents the planner and
    the actual client from disagreeing about the effective input ceiling.
    """
    policy = _validate_context_policy(config)
    window = policy["context_window_tokens"]
    input_limit = policy["max_input_tokens"]
    if window is None and input_limit is None:
        return {
            "model": config.get("model", "configured model"),
            "estimated_input_tokens": None,
            "allowed_input_tokens": None,
            "context_window_tokens": window,
            "max_input_tokens": input_limit,
            "max_output_tokens": policy["max_output_tokens"],
            "image_count": image_count,
            "fits": True,
        }
    estimated = estimate_input_tokens(system, prompt, image_count=image_count)
    allowed = input_limit if input_limit is not None else None
    if window is not None:
        window_input = window - policy["max_output_tokens"]
        allowed = window_input if allowed is None else min(allowed, window_input)
    return {
        "model": config.get("model", "configured model"),
        "estimated_input_tokens": estimated,
        "allowed_input_tokens": int(allowed) if allowed is not None else None,
        "context_window_tokens": window,
        "max_input_tokens": input_limit,
        "max_output_tokens": policy["max_output_tokens"],
        "image_count": image_count,
        "fits": allowed is None or estimated <= allowed,
    }


def resumed_model_execution_config(retained, requested):
    """Update execution capacity without changing retained routes or quotas."""
    controls = frozenset({"max_output_tokens", "max_input_tokens", "reasoning_effort"})

    def without_controls(value):
        if isinstance(value, dict):
            return {key: without_controls(item) for key, item in value.items() if key not in controls}
        if isinstance(value, list):
            return [without_controls(item) for item in value]
        return value

    if not isinstance(retained, dict) or not isinstance(requested, dict):
        raise ValidationError("resumed model execution configuration must be an object")
    if without_controls(retained) != without_controls(requested):
        raise ValidationError("resumed model execution controls cannot change provider routes or quotas")
    roles = set().union(*(requested.get(key, {}) for key in (
        "role_models", "role_model_fallbacks", "role_routes", "role_profiles")))
    for role in sorted(roles):
        for candidate in model_route_candidates(requested, role=role):
            _validate_context_policy(candidate)
        base = resolve_model_config(requested, role=role)
        for route in role_routes_for(requested, role):
            _validate_context_policy(merge_model_config(base, {
                key: value for key, value in route.items() if key not in {"id", "pool"}}))
    return deepcopy(requested)


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
    base = dict(load_model_config(model))
    role_models = base.pop("role_models", {})
    _validate_role_models(role_models)
    role_model_fallbacks = base.pop("role_model_fallbacks", {})
    _validate_role_model_fallbacks(role_model_fallbacks)
    provider_cooldown_fallback = base.pop("provider_cooldown_fallback", None)
    if provider_cooldown_fallback is not None:
        if not isinstance(provider_cooldown_fallback, dict):
            raise ValidationError("model.provider_cooldown_fallback must be an object")
        fallback_model = {
            key: value for key, value in provider_cooldown_fallback.items()
            if key not in {"id", "pool"}
        }
        _validate_role_models({"provider_cooldown_fallback": fallback_model})
        pool = provider_cooldown_fallback.get("pool")
        if pool is not None and (not isinstance(pool, str) or not pool.strip()):
            raise ValidationError("model.provider_cooldown_fallback.pool is invalid")
        route_id = provider_cooldown_fallback.get("id")
        if route_id is not None and (not isinstance(route_id, str) or not route_id.strip()):
            raise ValidationError("model.provider_cooldown_fallback.id is invalid")
        fallback_route = dict(base)
        fallback_route.update(fallback_model)
        if is_local_qwen_route(fallback_route):
            provider_cooldown_fallback = None
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
    selected_model = role_config_for(role_models, role)
    alternatives = role_config_for(role_model_fallbacks, role, [])

    def resolved_selection(candidate):
        resolved = dict(base)
        if isinstance(candidate, dict):
            resolved.update(candidate)
        return resolved

    def safe_alternative():
        return next((alternative for alternative in alternatives
                     if model_call_budget_available(alternative)
                     and not is_local_qwen_route(resolved_selection(alternative))), None)

    if (selected_model is not None
            and is_local_qwen_route(resolved_selection(selected_model))):
        selected_model = safe_alternative()
    elif selected_model is not None and not model_call_budget_available(selected_model):
        selected_model = safe_alternative() or selected_model
    selected_sampling = {}
    if selected_model is not None:
        selected_model = dict(selected_model)
        selected_sampling = {
            key: selected_model.pop(key)
            for key in list(selected_model) if key in SAMPLING_FIELDS
        }
        base = merge_model_config(base, selected_model)
    sampling = dict(DEFAULT_ROLE_PROFILES.get(role, {}))
    sampling.update(global_sampling)
    sampling.update(selected_sampling)
    if role is not None:
        sampling.update(profiles.get(role, {}))
    if overrides:
        sampling.update(overrides)
    _validate_sampling_options(sampling)
    base.update(sampling)
    reject_local_qwen_route(base)
    return base


def merge_model_config(parent, overrides):
    """Route settings may add budget owners, never remove existing owners."""
    merged = dict(parent)
    merged.update(overrides)
    scopes = []
    for source in (parent, overrides):
        values = source.get("model_call_budget_scopes", [])
        if not isinstance(values, list):
            raise ValidationError("model call budget scopes must be a list")
        for value in values:
            if value not in scopes:
                scopes.append(value)
    parent_budget = _budget_config(parent)
    merged_budget = _budget_config(merged)
    if parent_budget is not None and parent_budget != merged_budget:
        scope = {field: parent[field] for field in MODEL_CALL_BUDGET_FIELDS}
        if scope not in scopes:
            scopes.append(scope)
    if scopes or "model_call_budget_scopes" in merged:
        merged["model_call_budget_scopes"] = scopes
    return merged


def with_runtime_cooldown_fallback(model, *, env=None):
    """Attach an owner-configured local model as an emergency-only route.

    The environment opt-in is separate from normal role routing: healthy
    cloud routes remain the normal assignments, while a known quota failure
    may use the configured local model. Explicit per-run fallbacks take
    precedence.
    """
    if not isinstance(model, dict):
        return model
    model = load_model_config(model)
    explicit_fallback = model.get("provider_cooldown_fallback")
    if isinstance(explicit_fallback, dict):
        effective_fallback = dict(model)
        effective_fallback.update(explicit_fallback)
        if is_local_qwen_route(effective_fallback):
            model = dict(model)
            model.pop("provider_cooldown_fallback", None)
    values = os.environ if env is None else env
    fallback_model = values.get("SCISAURUS_OLLAMA_COOLDOWN_FALLBACK_MODEL")
    if not isinstance(fallback_model, str) or not fallback_model.strip():
        return model
    if model.get("provider_cooldown_fallback") is not None:
        return model
    if str(values.get("SCISAURUS_OLLAMA_ONLY", "")).casefold() not in {
            "1", "true", "yes", "on"}:
        raise ValidationError(
            "SCISAURUS_OLLAMA_COOLDOWN_FALLBACK_MODEL requires SCISAURUS_OLLAMA_ONLY")

    configured_base = str(values.get("SCISAURUS_OLLAMA_BASE_URL") or "").rstrip("/")
    model_base = str(model.get("base_url") or "").rstrip("/")
    if not model_base or (configured_base and model_base != configured_base):
        return model
    fallback_base = str(
        values.get("SCISAURUS_OLLAMA_QWEN_BASE_URL") or configured_base or model_base
    ).rstrip("/")
    if not fallback_base:
        return model
    if is_local_qwen_route({"model": fallback_model.strip(), "base_url": fallback_base}):
        return model

    def positive_int(value):
        return value if type(value) is int and value > 0 else None

    context_window = positive_int(model.get("context_window_tokens"))
    max_output = positive_int(model.get("max_output_tokens"))
    max_input = positive_int(model.get("max_input_tokens"))
    if context_window is not None and max_output is not None:
        available_input = context_window - max_output
        if available_input <= 0:
            return model
        max_input = min(max_input, available_input) if max_input is not None else available_input

    fallback = {
        "id": "ollama-local-cooldown-recovery",
        "pool": "ollama",
        "protocol": model.get("protocol", "openai_compatible"),
        "base_url": fallback_base,
        "model": fallback_model.strip(),
        "auth_env": None,
        "provider_quota_scope": "ollama-local",
        "max_retries": 0,
    }
    for field, value in (
            ("context_window_tokens", context_window),
            ("max_input_tokens", max_input),
            ("max_output_tokens", max_output),
            ("timeout_seconds", model.get("timeout_seconds")),
            ("max_request_bytes", model.get("max_request_bytes")),
            ("max_response_bytes", model.get("max_response_bytes")),
            ("max_image_bytes", model.get("max_image_bytes")),
            ("output_format", model.get("output_format")),
            ("reasoning_effort", model.get("reasoning_effort"))):
        if value is not None:
            fallback[field] = value
    configured = dict(model)
    configured["provider_cooldown_fallback"] = fallback
    return configured


def model_route_candidates(model, *, role, prefer_fallback=False,
                           include_cooldown_fallback=False):
    """Resolve a role's primary route and configured model fallbacks.

    Fallbacks are explicit per-role alternatives. They are not consulted for
    ordinary validation failures; callers may use them after a provider
    rejects a request before returning any model output.
    """
    model = with_runtime_cooldown_fallback(model)
    if not isinstance(role, str) or not role.strip():
        raise ValidationError("model route role must be a nonempty string")
    primary = resolve_model_config(model, role=role)
    regular_candidates = [primary]
    seen = {(primary.get("protocol"), primary.get("base_url"),
             primary.get("model"), primary.get("auth_env"))}

    def append_alternative(alternative, destination):
        if not isinstance(alternative, dict):
            return
        if is_local_qwen_route({**model, **alternative}):
            return
        routed = dict(model)
        role_models = dict(routed.get("role_models", {}))
        role_models[role] = {
            key: value for key, value in alternative.items()
            if key not in {"id", "pool"}
        }
        routed["role_models"] = role_models
        role_fallbacks = dict(routed.get("role_model_fallbacks", {}))
        role_fallbacks.pop(role, None)
        routed["role_model_fallbacks"] = role_fallbacks
        role_routes = dict(routed.get("role_routes", {}))
        role_routes.pop(role, None)
        routed["role_routes"] = role_routes
        candidate = resolve_model_config(routed, role=role)
        identity = (candidate.get("protocol"), candidate.get("base_url"),
                    candidate.get("model"), candidate.get("auth_env"))
        if identity in seen or not model_call_budget_available(candidate):
            return
        seen.add(identity)
        destination.append(candidate)

    fallbacks = model.get("role_model_fallbacks", {}) if isinstance(model, dict) else {}
    alternatives = role_config_for(fallbacks, role, [])
    for alternative in alternatives:
        append_alternative(alternative, regular_candidates)

    cooldown_candidates = []
    if include_cooldown_fallback:
        append_alternative(model.get("provider_cooldown_fallback"), cooldown_candidates)
    if prefer_fallback and len(regular_candidates) > 1:
        regular_candidates = regular_candidates[1:] + regular_candidates[:1]
    # A cooldown-only route is opt-in so context selection cannot accidentally
    # make it the first or only candidate before an ordinary provider 429.
    return regular_candidates + cooldown_candidates


def model_provider_quota_scope(config):
    """Identify which configured route alternatives share a provider quota."""
    if not isinstance(config, dict):
        return "unknown-provider"
    explicit = config.get("provider_quota_scope")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return "|".join(str(config.get(key) or "") for key in (
        "protocol", "base_url", "auth_env"))


def record_model_provider_cooldown(config, *, retry_after_seconds=None):
    """Open a process-wide provider circuit after an exhausted quota scope."""
    scope = (config if isinstance(config, str)
             else model_provider_quota_scope(config))
    now = time.monotonic()
    with _MODEL_PROVIDER_COOLDOWN_LOCK:
        previous = _MODEL_PROVIDER_COOLDOWNS.get(scope)
        previous_until, previous_failures = (
            previous[:2] if previous else (0.0, 0))
        failures = previous_failures + 1
        delay = DEFAULT_MODEL_RATE_LIMIT_COOLDOWN_SECONDS * (
            2 ** min(failures - 1, 16))
        if (type(retry_after_seconds) in (int, float)
                and math.isfinite(retry_after_seconds)
                and retry_after_seconds > 0):
            delay = max(delay, float(retry_after_seconds))
        delay = min(MAX_MODEL_RATE_LIMIT_COOLDOWN_SECONDS, delay)
        until = max(previous_until, now + delay)
        generation = _MODEL_PROVIDER_COOLDOWN_GENERATIONS.get(scope, 0) + 1
        _MODEL_PROVIDER_COOLDOWN_GENERATIONS[scope] = generation
        _MODEL_PROVIDER_COOLDOWNS[scope] = (until, failures, generation)
        return max(0.0, until - now)


def model_provider_cooldown_remaining(config):
    """Return the remaining process-wide cooldown for a provider quota scope."""
    return model_provider_cooldown_snapshot(config)[0]


def model_provider_cooldown_snapshot(config):
    """Atomically read a quota scope's remaining cooldown and generation."""
    scope = (config if isinstance(config, str)
             else model_provider_quota_scope(config))
    now = time.monotonic()
    with _MODEL_PROVIDER_COOLDOWN_LOCK:
        state = _MODEL_PROVIDER_COOLDOWNS.get(scope)
        remaining = max(0.0, state[0] - now) if state is not None else 0.0
        generation = _MODEL_PROVIDER_COOLDOWN_GENERATIONS.get(scope, 0)
        return remaining, generation


def admit_model_provider_call(config):
    """Atomically admit one request unless its quota circuit is open.

    The returned generation belongs to the admission point. A circuit opened
    afterward treats this request as already in flight and cannot be cleared
    by its eventual success.
    """
    scope = (config if isinstance(config, str)
             else model_provider_quota_scope(config))
    now = time.monotonic()
    with _MODEL_PROVIDER_COOLDOWN_LOCK:
        state = _MODEL_PROVIDER_COOLDOWNS.get(scope)
        remaining = max(0.0, state[0] - now) if state is not None else 0.0
        generation = _MODEL_PROVIDER_COOLDOWN_GENERATIONS.get(scope, 0)
        if remaining > 0:
            return None, remaining
        return generation, 0.0


def model_provider_cooldown_generation(config):
    """Return a token for detecting a circuit opened during an in-flight call."""
    return model_provider_cooldown_snapshot(config)[1]


def clear_model_provider_cooldown(config, *, expected_generation=None):
    """Clear only the circuit observed by a successful request, if requested."""
    scope = (config if isinstance(config, str)
             else model_provider_quota_scope(config))
    with _MODEL_PROVIDER_COOLDOWN_LOCK:
        if (expected_generation is not None
                and _MODEL_PROVIDER_COOLDOWN_GENERATIONS.get(scope, 0)
                != expected_generation):
            return False
        _MODEL_PROVIDER_COOLDOWNS.pop(scope, None)
        return True


def complete_with_role_fallbacks(model, *, role, system, prompt, images=None,
                                 deadline=None, prefer_fallback=False,
                                 output_token_cap=None, output_format=None,
                                 continuation_text=None,
                                 client_factory=None,
                                 candidate_configs=None):
    """Dispatch once and return provider 429s to the workflow scheduler.

    A rate limit or exhausted quota is an availability failure, not a
    scientific result. Replaying the same prompt against another configured
    model on the same provider can spend more quota without changing the
    blocker, so the scheduler must pause the mission instead.
    """
    if output_format is not None and output_format != "json_object":
        raise ValidationError("role output_format must be json_object when configured")
    if continuation_text is not None and (
            not isinstance(continuation_text, str) or not continuation_text):
        raise ValidationError("continuation_text must be a nonempty string when supplied")
    request_prompt = prompt
    if continuation_text is not None:
        request_prompt += "\n\n" + continuation_text + "\n\n" + MODEL_CONTINUATION_INSTRUCTION
    regular_candidates = model_route_candidates(
        model, role=role, prefer_fallback=prefer_fallback)
    all_candidates = model_route_candidates(
        model, role=role, prefer_fallback=prefer_fallback,
        include_cooldown_fallback=True)
    cooldown_candidates = all_candidates[len(regular_candidates):]
    cooldown_identities = {
        tuple(candidate.get(key) for key in (
            "protocol", "base_url", "model", "auth_env"))
        for candidate in cooldown_candidates
    }
    if candidate_configs is None:
        candidates = [dict(candidate) for candidate in regular_candidates]
    else:
        if not isinstance(candidate_configs, list) or not candidate_configs:
            raise ValidationError("candidate_configs must be a nonempty route list")
        candidates = []
        seen_candidates = set()
        for candidate in candidate_configs:
            if not isinstance(candidate, dict):
                raise ValidationError("candidate_configs entries must be route objects")
            identity = tuple(candidate.get(key) for key in (
                "protocol", "base_url", "model", "auth_env"))
            if (identity in cooldown_identities or identity in seen_candidates
                    or not model_call_budget_available(candidate)):
                continue
            seen_candidates.add(identity)
            candidates.append(dict(candidate))
        if not candidates:
            raise ModelCallError("no configured model route is available", outcome_known=True)
    for cooldown_candidate in cooldown_candidates:
        identity = tuple(cooldown_candidate.get(key) for key in (
            "protocol", "base_url", "model", "auth_env"))
        if identity in {tuple(candidate.get(key) for key in (
                "protocol", "base_url", "model", "auth_env"))
                for candidate in candidates}:
            continue
        bounded = dict(cooldown_candidate)
        if candidates:
            # Respect the role-specific caps chosen by context projection and
            # review policy while retaining the fallback's own context window.
            preferred = candidates[0]
            for field in ("max_input_tokens", "max_output_tokens", "reasoning_effort"):
                if field in preferred:
                    bounded[field] = preferred[field]
            if type(output_token_cap) is int and output_token_cap > 0:
                bounded["max_output_tokens"] = min(
                    int(bounded["max_output_tokens"]), output_token_cap)
            timeout_bounds = [
                value for value in (
                    bounded.get("timeout_seconds"),
                    preferred.get("timeout_seconds"),
                )
                if type(value) in (int, float) and math.isfinite(value) and value > 0
            ]
            if timeout_bounds:
                bounded["timeout_seconds"] = min(timeout_bounds)
        bounded["max_retries"] = 0
        if (model_call_budget_available(bounded)
                and model_context_error(
                    bounded, system=system, prompt=request_prompt,
                    image_count=len(images or [])) is None):
            candidates.append(bounded)
    if not candidates:
        raise ModelCallError("no configured model route is available", outcome_known=True)
    context_candidates = []
    context_failures = []
    for candidate in candidates:
        bounded = dict(candidate)
        if type(output_token_cap) is int and output_token_cap > 0:
            bounded["max_output_tokens"] = min(
                int(bounded["max_output_tokens"]), output_token_cap)
        budget = model_context_budget(
            bounded, system=system, prompt=request_prompt,
            image_count=len(images or []))
        if budget["fits"]:
            context_candidates.append(candidate)
        else:
            context_failures.append(budget)
    if not context_candidates:
        budget = max(context_failures, key=lambda item: item["allowed_input_tokens"] or 0)
        raise ModelContextBudgetError(
            f"model context budget exceeded for {budget['model']}: conservative input estimate "
            f"{budget['estimated_input_tokens']} tokens exceeds "
            f"{budget['allowed_input_tokens']} input tokens; context window "
            f"{budget['context_window_tokens']} with max output {budget['max_output_tokens']}",
            model=budget["model"],
            estimated_input_tokens=budget["estimated_input_tokens"],
            allowed_input_tokens=budget["allowed_input_tokens"],
            context_window_tokens=budget["context_window_tokens"],
            max_input_tokens=budget["max_input_tokens"],
            max_output_tokens=budget["max_output_tokens"],
            image_count=budget["image_count"],
        )
    candidates = context_candidates
    make_client = client_factory or ModelClient
    route_history = []
    failed_request_attempts = 0
    retry_after_hints = []
    cooldown_skips = 0
    longest_cooldown = 0.0
    for index, config in enumerate(candidates):
        bounded = dict(config)
        if output_format is not None:
            bounded["output_format"] = output_format
        quota_scope = model_provider_quota_scope(bounded)
        global_cooldown = model_provider_cooldown_remaining(quota_scope)
        if global_cooldown > 0:
            cooldown_skips += 1
            longest_cooldown = max(longest_cooldown, global_cooldown)
            continue
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.2:
                raise ModelCallError(
                    "model route failover reached the stage deadline",
                    outcome_known=True, attempts=failed_request_attempts,
                )
            bounded["timeout_seconds"] = min(
                float(bounded["timeout_seconds"]), remaining)
        if type(output_token_cap) is int and output_token_cap > 0:
            bounded["max_output_tokens"] = min(
                int(bounded["max_output_tokens"]), output_token_cap)
        route_name = "primary" if index == 0 else f"fallback-{index}"
        cooldown_generation, admission_wait = admit_model_provider_call(quota_scope)
        if cooldown_generation is None:
            cooldown_skips += 1
            longest_cooldown = max(longest_cooldown, admission_wait)
            continue
        try:
            call_kwargs = {"system": system, "prompt": prompt, "images": images}
            if continuation_text is not None:
                call_kwargs["continuation_text"] = continuation_text
            result = make_client(**bounded).complete(**call_kwargs)
        except ModelCallError as exc:
            route_history.append({
                "route": route_name,
                "model": bounded.get("model"),
                "status_code": exc.status_code,
                "provider_error_kind": exc.provider_error_kind,
                "request_attempts": exc.attempts,
            })
            failed_request_attempts += exc.attempts
            if (type(exc.retry_after_seconds) in (int, float)
                    and math.isfinite(exc.retry_after_seconds)
                    and exc.retry_after_seconds > 0):
                retry_after_hints.append(float(exc.retry_after_seconds))
            if exc.status_code == 429:
                record_model_provider_cooldown(
                    quota_scope,
                    retry_after_seconds=max(retry_after_hints, default=0.0),
                )
            exc.attempts = failed_request_attempts
            exc.route_history = route_history
            if retry_after_hints:
                exc.retry_after_seconds = max(retry_after_hints)
            raise
        route_history.append({
            "route": route_name,
            "model": bounded.get("model"),
            "status_code": 200,
            "request_attempts": result.request_attempts,
        })
        clear_model_provider_cooldown(
            quota_scope, expected_generation=cooldown_generation)
        if failed_request_attempts:
            result = replace(
                result,
                request_attempts=result.request_attempts + failed_request_attempts,
            )
        return result, route_history
    if cooldown_skips:
        raise ModelCallError(
            "all configured model routes are inside a provider cooldown",
            outcome_known=True, attempts=0, status_code=429,
            retry_after_seconds=longest_cooldown,
        )
    raise ModelCallError("no configured model route is available", outcome_known=True)


class ModelCallError(RuntimeError):
    """An invocation failed; unknown outcomes must retain their reservation.

    ``status_code`` and ``retry_after_seconds`` are deliberately kept on the
    typed error instead of being inferred from the rendered message.  The
    orchestration layer can distinguish a rate-limit stop from a retryable
    transport failure without parsing provider-specific error text.
    """
    def __init__(self, message, *, outcome_known=False, attempts=0,
                 elapsed_seconds=None, status_code=None,
                 retry_after_seconds=None, provider_error_kind=None):
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
        self.provider_error_kind = (
            provider_error_kind if provider_error_kind in {
                "quota_exhausted", "rate_limited", "model_unavailable",
            } else None
        )

    def failure_details(self):
        """Serialize admission and provider facts across runner boundaries."""
        failure = {"kind": "model_call", "outcome_known": self.outcome_known,
                   "attempts": self.attempts, "elapsed_seconds": self.elapsed_seconds,
                   "status_code": self.status_code,
                   "retry_after_seconds": self.retry_after_seconds,
                   "provider_error_kind": self.provider_error_kind,
                   "usage": dict(getattr(self, "usage", {}))}
        if getattr(self, "budget_admission", None) is not None:
            failure["budget_admission"] = dict(self.budget_admission)
        return failure

    @staticmethod
    def from_failure(message, failure):
        """Reconstruct the same typed fence without parsing its message."""
        admission = failure.get("budget_admission")
        error_type = ModelBudgetExceededError if admission is not None else ModelCallError
        error = error_type(message, outcome_known=failure.get("outcome_known", False),
            attempts=failure.get("attempts", 0), elapsed_seconds=failure.get("elapsed_seconds"),
            status_code=failure.get("status_code"),
            retry_after_seconds=failure.get("retry_after_seconds"),
            provider_error_kind=failure.get("provider_error_kind"),
            **({"budget_admission": admission} if admission is not None else {}))
        error.usage = dict(failure.get("usage", {}))
        return error


class ModelBudgetExceededError(ModelCallError, QuotaExceededError):
    """A registered owner rejected a request before provider admission."""
    def __init__(self, message, *, budget_admission, **kwargs):
        fields = {"path", "key", "dimension", "limit", "observed", "reserved", "requested"}
        if (not isinstance(budget_admission, dict) or set(budget_admission) != fields
                or not isinstance(budget_admission["path"], str) or not Path(budget_admission["path"]).is_absolute()
                or not isinstance(budget_admission["key"], str) or not budget_admission["key"]
                or budget_admission["dimension"] not in {"model_calls", "input_tokens", "output_tokens"}
                or any(type(budget_admission[key]) is not int or budget_admission[key] < 0
                       for key in ("limit", "observed", "reserved", "requested"))
                or budget_admission["limit"] < 1):
            raise ValidationError("invalid model budget-admission fence")
        ModelCallError.__init__(self, message, **kwargs)
        self.budget_admission = dict(budget_admission)
        self.dimension = "max_" + budget_admission["dimension"]
        self.limit = budget_admission["limit"]
        self.observed = budget_admission["observed"]
        self.usage = {}
        self.diagnostics = [dict(budget_admission)]


class ModelContextBudgetError(ValidationError):
    """A request was rejected locally because its input cannot fit the route.

    This is a deterministic admission result, not a provider call failure.
    The structured measurements let an orchestrator change the projection or
    route instead of replaying the identical oversized packet.
    """

    def __init__(self, message, *, model, estimated_input_tokens,
                 allowed_input_tokens, context_window_tokens,
                 max_input_tokens, max_output_tokens, image_count=0):
        super().__init__(message)
        self.model = model
        self.estimated_input_tokens = estimated_input_tokens
        self.allowed_input_tokens = allowed_input_tokens
        self.context_window_tokens = context_window_tokens
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.image_count = image_count
        self.failure_class = "context_budget"
        self.outcome_known = True
        self.attempts = 0
        self.usage = {}


class _ProviderHTTPError(RuntimeError):
    """A provider response with an HTTP status other than 200."""
    def __init__(self, code, retry_after=None, provider_error_kind=None):
        super().__init__(f"model HTTP request failed with status {code}")
        self.code = code
        self.provider_error_kind = provider_error_kind
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                delay = None
        self.retry_after = max(0.0, delay) if delay is not None and math.isfinite(delay) else None


def _provider_http_error_kind(body):
    """Classify a bounded provider error body without retaining its contents."""
    if not isinstance(body, (bytes, bytearray)):
        return None
    try:
        value = json.loads(bytes(body[:8192]).decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        value = bytes(body[:8192]).decode("utf-8", errors="replace")
    if isinstance(value, dict):
        error = value.get("error", value)
        if isinstance(error, dict):
            fields = (error.get("code"), error.get("type"), error.get("message"),
                      error.get("detail"))
        else:
            fields = (error,)
        text = " ".join(str(field) for field in fields if field is not None).casefold()
    else:
        text = str(value).casefold()
    if any(marker in text for marker in (
            "insufficient_quota", "insufficient quota", "quota exceeded",
            "quota_exceeded", "weekly limit", "daily limit", "credits exhausted",
            "credit balance", "billing limit", "out of cloud credits")):
        return "quota_exhausted"
    if any(marker in text for marker in (
            "rate limit", "rate_limit", "too many requests", "throttl",
            "overload", "temporarily busy")):
        return "rate_limited"
    if any(marker in text for marker in ("model not found", "unknown model", "model unavailable")):
        return "model_unavailable"
    return None


def json_object_continuation_error(text):
    """Reject prefixes that cannot become one JSON object by appending a suffix."""
    def unfence(candidate):
        lines = candidate.splitlines(keepends=True)
        if lines and lines[0].strip().casefold() in {"```json", "```jsonc"}:
            return "".join(lines[1:]).lstrip()
        return candidate

    payload_prefix = unfence(text.lstrip())
    if not payload_prefix.startswith("{") and "</think>" in payload_prefix:
        payload_prefix = unfence(payload_prefix.split("</think>", 1)[1].lstrip())
    if not payload_prefix.startswith("{"):
        return "truncated structured response has no JSON object prefix"

    def reject_nonfinite(value):
        raise ValueError("nonfinite JSON")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            reject_nonfinite(value)
        return number

    try:
        json.JSONDecoder(parse_constant=reject_nonfinite,
                         parse_float=finite_float).raw_decode(payload_prefix)
    except json.JSONDecodeError as error:
        remaining = payload_prefix[error.pos:]
        if error.pos >= len(payload_prefix) or error.msg.startswith("Unterminated string"):
            return None
        if error.msg == "Expecting value" and (
                any(token.startswith(remaining) for token in ("true", "false", "null"))
                or remaining == "-"):
            return None
        if (error.msg.startswith("Invalid \\u") and remaining.startswith("u")
                and re.fullmatch(r"u[0-9a-fA-F]{0,3}", remaining)):
            return None
        if (error.msg.startswith("Expecting ',' delimiter")
                and error.pos > 0 and payload_prefix[error.pos - 1].isdigit()
                and re.fullmatch(r"(?:\.[0-9]+)?[eE][+-]?|\.[0-9]*", remaining)):
            return None
        return "truncated structured response has an invalid JSON object prefix"
    except ValueError:
        return "truncated structured response has an invalid JSON object prefix"
    return "structured response already contains a closed JSON object"


@dataclass(frozen=True)
class ModelResult:
    text: str
    model: str
    usage: dict
    elapsed_seconds: float
    finish_reason: str
    request_attempts: int = 1
    response_metadata: dict = field(default_factory=dict)

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
                 provider_quota_scope: str | None = None,
                 model_call_budget_path: str | None = None,
                 model_call_budget_key: str | None = None,
                 model_call_budget_limit: int | None = None,
                 model_call_budget_scopes: list | None = None):
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
        reject_local_qwen_route({"base_url": base_url, "model": model})
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
        if provider_quota_scope is not None and (
                not isinstance(provider_quota_scope, str)
                or not provider_quota_scope.strip()
                or len(provider_quota_scope) > 160):
            raise ValidationError("provider_quota_scope must be a nonempty string")
        _validate_model_call_budget({
            "model_call_budget_path": model_call_budget_path,
            "model_call_budget_key": model_call_budget_key,
            "model_call_budget_limit": model_call_budget_limit,
        }, name="model call budget")
        if model_call_budget_scopes is not None:
            if not isinstance(model_call_budget_scopes, list):
                raise ValidationError("model call budget scopes must be a list")
            for scope in model_call_budget_scopes:
                validate_model_budget_scope(scope)
        self.model_call_budget_scopes = [dict(scope) for scope in model_call_budget_scopes or []]
        sampling = {
            key: value for key, value in {
                "temperature": temperature, "top_p": top_p, "seed": seed,
                "presence_penalty": presence_penalty, "frequency_penalty": frequency_penalty,
            }.items() if value is not None
        }
        _validate_sampling_options(sampling)
        if protocol == "ollama" and reasoning_effort == "xhigh":
            raise ValidationError("native Ollama reasoning_effort requires none, low, medium, or high")
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

    def complete(self, *, system: str, prompt: str, images=None,
                 continuation_text: str | None = None,
                 dispatch_budget: dict | None = None) -> ModelResult:
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with model_dispatch_slot(deadline=deadline):
                return self._complete(system=system, prompt=prompt, images=images,
                    continuation_text=continuation_text, dispatch_budget=dispatch_budget,
                    deadline=deadline)
        except ModelSlotTimeout as exc:
            raise ModelCallError(str(exc), outcome_known=True, attempts=0) from exc

    def _complete(self, *, system, prompt, images, continuation_text, dispatch_budget,
                  deadline):
        from scisaurus.runtime.run_control import ensure_run_allowed, dispatch_permission, RunPausedError
        ensure_run_allowed()
        request_model = self.model
        request_base_url = self.base_url
        reject_local_qwen_route({"base_url": request_base_url, "model": request_model})
        if dispatch_budget is not None:
            _validate_model_call_budget(dispatch_budget, name="dispatch budget")
        if not isinstance(system, str) or not isinstance(prompt, str):
            raise ValidationError("model system and prompt content must be strings")
        if continuation_text is not None and (
                not isinstance(continuation_text, str) or not continuation_text):
            raise ValidationError("continuation_text must be a nonempty string when supplied")
        images = [] if images is None else images
        if not isinstance(images, list) or len(images) > 16:
            raise ValidationError("model images must be a list containing at most 16 items")
        context_config = {
            "model": request_model, "max_output_tokens": self.max_output_tokens,
            "context_window_tokens": self.context_window_tokens,
            "max_input_tokens": self.max_input_tokens,
        }
        budget_prompt = prompt
        if continuation_text is not None:
            budget_prompt += "\n\n" + continuation_text + "\n\n" + MODEL_CONTINUATION_INSTRUCTION
        context_budget = model_context_budget(
            context_config, system=system, prompt=budget_prompt, image_count=len(images))
        if not context_budget["fits"]:
            context_error = model_context_error(
                context_config, system=system, prompt=budget_prompt,
                image_count=len(images))
            raise ModelContextBudgetError(
                context_error,
                model=context_budget["model"],
                estimated_input_tokens=context_budget["estimated_input_tokens"],
                allowed_input_tokens=context_budget["allowed_input_tokens"],
                context_window_tokens=context_budget["context_window_tokens"],
                max_input_tokens=context_budget["max_input_tokens"],
                max_output_tokens=context_budget["max_output_tokens"],
                image_count=context_budget["image_count"],
            )
        parts, encoded_images, total = [{"type": "text", "text": prompt}], [], 0
        for descriptor in images:
            raw, media_type = self._read_image(descriptor)
            total += len(raw)
            if total > self.max_image_bytes:
                raise ValidationError("combined model images exceed the configured byte limit")
            encoded = base64.b64encode(raw).decode("ascii")
            encoded_images.append(encoded)
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{media_type};base64,{encoded}"}})
        user_message = {"role": "user", "content": prompt}
        if images:
            if self.protocol == "ollama":
                user_message["images"] = encoded_images
            else:
                user_message["content"] = parts
        messages = [{"role": "system", "content": system}, user_message]
        if continuation_text is not None:
            messages.extend([
                {"role": "assistant", "content": continuation_text},
                {"role": "user", "content": MODEL_CONTINUATION_INSTRUCTION},
            ])
        body = {"model": request_model, "messages": messages, "stream": False}
        sampling = {
            key: value for key, value in {
                "temperature": self.temperature, "top_p": self.top_p, "seed": self.seed,
                "presence_penalty": self.presence_penalty,
                "frequency_penalty": self.frequency_penalty,
            }.items() if value is not None
        }
        if self.protocol == "ollama":
            path = "/api/chat"
            if self.reasoning_effort is not None:
                body["think"] = (False if self.reasoning_effort == "none"
                                 else self.reasoning_effort)
            # Ollama's native options expose temperature/top-p/seed but not
            # the OpenAI presence/frequency penalty names.
            body["options"] = {
                "num_predict": self.max_output_tokens,
                **{key: value for key, value in sampling.items() if key in OLLAMA_SAMPLING_FIELDS},
            }
            if self.output_format is not None and continuation_text is None:
                body["format"] = "json"
            # ``num_ctx`` is a server-side context allocation, not merely an
            # admission hint.  Keep it role/model-specific by deriving it
            # from the selected route's context window.  The OpenAI-compatible
            # bridge below deliberately does not receive this field: its
            # contract has no portable per-request context parameter.
            if self.context_window_tokens is not None:
                body["options"]["num_ctx"] = self.context_window_tokens
        else:
            path = "/chat/completions"
            body["max_tokens"] = self.max_output_tokens
            body.update(sampling)
            if self.reasoning_effort is not None:
                body["reasoning_effort"] = self.reasoning_effort
            if self.output_format is not None and continuation_text is None:
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
        parsed_base = urllib.parse.urlsplit(request_base_url)
        connection_type = (http.client.HTTPSConnection
                           if parsed_base.scheme == "https" else http.client.HTTPConnection)
        request_path = parsed_base.path.rstrip("/") + path
        if not request_path.startswith("/"):
            request_path = "/" + request_path
        started = time.monotonic()
        # Account-wide backpressure belongs to the scheduler, which can retain
        # siblings and wait without submitting the same prompt again.
        retryable_statuses = {408, 425, 500, 502, 503, 504}
        attempt = 0
        attempts_made = 0
        parsed = None
        reserved = []
        reported_usage = {}

        def settle(usage):
            try:
                _settle_model_token_budgets(reserved, usage)
            except (OSError, sqlite3.Error, ValidationError) as exc:
                error = ValidationError("model token-budget settlement failed")
                error.usage = {"model_calls": attempts_made, **reported_usage}
                error.attempts = attempts_made
                error.outcome_known = all(key in reported_usage for key in ("input_tokens", "output_tokens"))
                raise error from exc

        def failure(message, *, outcome_known=False, status_code=None,
                    retry_after_seconds=None, provider_error_kind=None, budget_admission=None):
            settle(reported_usage or None)
            error_type = ModelBudgetExceededError if budget_admission is not None else ModelCallError
            error = error_type(
                message, outcome_known=outcome_known, attempts=attempts_made,
                elapsed_seconds=time.monotonic() - started,
                status_code=status_code,
                retry_after_seconds=retry_after_seconds,
                provider_error_kind=provider_error_kind,
                **({"budget_admission": budget_admission} if budget_admission is not None else {}),
            )
            error.usage = {"model_calls": attempts_made, **reported_usage} if attempts_made else {}
            return error

        while True:
            reported_usage = {}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise failure("model request deadline exceeded") from None
            try:
                with dispatch_permission():
                    reserved = _reserve_model_call_budgets([*self.model_call_budget_scopes, dispatch_budget or {}, {
                        "model_call_budget_path": self.model_call_budget_path,
                        "model_call_budget_key": self.model_call_budget_key,
                        "model_call_budget_limit": self.model_call_budget_limit,
                    }], token_reservation={"input_tokens": estimate_input_tokens(system, budget_prompt, image_count=len(images)),
                                           "output_tokens": self.max_output_tokens})
                    attempts_made += 1
            except RunPausedError as exc:
                exc.attempts = attempts_made
                exc.usage = {"model_calls": attempts_made} if attempts_made else {}
                raise
            except ModelCallError as exc:
                raise failure(str(exc), outcome_known=exc.outcome_known,
                    status_code=exc.status_code, retry_after_seconds=exc.retry_after_seconds,
                    provider_error_kind=exc.provider_error_kind,
                    budget_admission=getattr(exc, "budget_admission", None)) from exc
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
                connection.connect()
                if "qwen" in request_model.casefold():
                    peer = connection.sock.getpeername()[0]
                    _reject_local_qwen_peer(request_model, peer)
                with dispatch_permission():
                    connection.request("POST", request_path, wire,
                                       headers={**headers, "Connection": "close"})
                response = connection.getresponse()
                code = response.status
                if 300 <= code < 400:
                    raise failure(
                        "model endpoint redirected; configure the final endpoint explicitly",
                        outcome_known=True)
                if code != 200:
                    error_body = b""
                    try:
                        read_error = getattr(response, "read1", response.read)
                        error_body = read_error(8192)
                    except (AttributeError, OSError, TimeoutError, ValueError):
                        pass
                    raise _ProviderHTTPError(
                        code, response.getheader("Retry-After"),
                        provider_error_kind=_provider_http_error_kind(error_body),
                    )
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
            except RunPausedError as exc:
                _release_model_call_budgets(reserved)
                reserved = []
                attempts_made -= 1
                exc.attempts = attempts_made
                exc.usage = {"model_calls": attempts_made, **reported_usage} if attempts_made else {}
                raise
            except _ProviderHTTPError as exc:
                code = exc.code
                retry_after = exc.retry_after
                if code in retryable_statuses and attempt < self.max_retries:
                    settle(None)
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
                                      retry_after_seconds=retry_after,
                                      provider_error_kind=exc.provider_error_kind) from None
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise failure(f"model HTTP request failed with status {code}",
                              outcome_known=400 <= code < 500,
                              status_code=code,
                              retry_after_seconds=retry_after,
                              provider_error_kind=exc.provider_error_kind) from None
            except ModelCallError as exc:
                raise failure(
                    str(exc), outcome_known=exc.outcome_known,
                    status_code=exc.status_code,
                    retry_after_seconds=exc.retry_after_seconds,
                    provider_error_kind=exc.provider_error_kind,
                    budget_admission=getattr(exc, "budget_admission", None),
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
                if not isinstance(data, dict):
                    raise ValueError("response must be an object")
                usage_container = data if self.protocol == "ollama" else data.get("usage", {})
                token_fields = (("input_tokens", "prompt_eval_count"), ("output_tokens", "eval_count")) if self.protocol == "ollama" else (
                    ("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"))
                reported_usage = {key: usage_container[source] for key, source in token_fields
                                  if isinstance(usage_container, dict) and type(usage_container.get(source)) is int
                                  and usage_container[source] >= 0}
                reported_usage.update(_cache_usage(data))
                settle(reported_usage)
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
                if not isinstance(text, str) or (not text.strip() and reason != "length"):
                    raise ValueError("empty text")
                if any(type(value) is not int or value < 0 for value in usage.values()):
                    raise ValueError("invalid usage")
                if reason not in {"stop", "length", "load", "unload", "unknown"}:
                    raise ValueError("unsupported completion state")
                message = data.get("message", {}) if self.protocol == "ollama" else choice.get("message", {})
                thinking = message.get("thinking", message.get("reasoning_content", message.get("reasoning")))
                metadata = {"reasoning_effort": self.reasoning_effort,
                            "wire_reasoning": body.get("think", body.get("reasoning_effort")),
                            "max_output_tokens": self.max_output_tokens, "answer_bytes": len(text.encode()),
                            "thinking_bytes": len(thinking.encode()) if isinstance(thinking, str) else None}
                details = data.get("usage", {}).get("completion_tokens_details", {}) if isinstance(data.get("usage"), dict) else {}
                if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int and details["reasoning_tokens"] >= 0:
                    metadata["reasoning_tokens"] = details["reasoning_tokens"]
                parsed = (text, reason, usage, data.get("model", self.model), metadata)
            except (ValueError, TypeError, KeyError, IndexError):
                # A received HTTP 200 may already have consumed a complete
                # generation. Its unknown usage must not be hidden by a
                # transparent second generation of the same request.
                raise failure("model returned an invalid or incomplete response",
                              outcome_known=all(key in reported_usage for key in ("input_tokens", "output_tokens"))) from None
            break
        elapsed = time.monotonic() - started
        text, reason, usage, served_model, metadata = parsed
        settle(usage)
        return ModelResult(text, served_model,
                           {"model_calls": attempts_made, **usage}, elapsed, reason,
                           request_attempts=attempts_made, response_metadata=metadata)
