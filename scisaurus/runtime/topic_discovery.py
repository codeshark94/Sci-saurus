"""Bounded, model-assisted topic discovery for a free-topic Composer run.

Topic selection is an intake operation, not a novelty claim.  The stage turns a
broad Principal objective into several testable research questions, screens the
selected direction for research maturity, and can regenerate it when the
question is too thin.  It hands only the selected question and search seeds to
the literature stage.  The survey, counter-search, experiment, and review
gates remain the authorities for evidence and release.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import itertools
import json
import math
import os
from random import Random
from pathlib import Path
import re
import time
import uuid

from scisaurus.core.errors import QuotaExceededError, ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.models import (
    MAX_PROVIDER_SEED, ModelCallError, ModelClient, resolve_model_config,
)
from scisaurus.runtime.literature import (
    OpenAlexClient, ProviderCooldownError, provider_cooldown_seconds,
)
from scisaurus.runtime.scientific_surface import find_control_leaks, project_internal_language


SCHEMA_VERSION = "topic-discovery-1"
STAGE_CONFIG_SCHEMA_VERSION = "topic-discovery-config-1"
TOPIC_HISTORY_SCHEMA_VERSION = "topic-history-1"
RECENT_YEAR_WINDOW = 4
# Topic intake only needs a compact inspiration set.  The survey stage owns
# authoritative retrieval, so carrying full abstracts for dozens of records
# into the first model call wastes the mission's response budget.
TOPIC_SAMPLE_LIMIT = 12
TOPIC_ABSTRACT_CHARS = 1200
FRONTIER_SEED_SCHEMA_VERSION = "topic-frontier-seeds-1"
SOURCE_CHALLENGE_SCHEMA_VERSION = "topic-source-challenge-1"
FRONTIER_SEED_FIELDS = {
    "id", "domain", "phenomenon", "mechanism", "unit_of_analysis", "search_queries",
}
SOURCE_CHALLENGE_FIELDS = {
    "schema_version", "decision", "selected_id", "source_relevance",
    "template_independence", "prior_work_risk", "closest_work_ids", "rationale",
    "required_changes",
}
TOPIC_BIBLIOGRAPHY_CLIENT_FIELDS = {
    "timeout", "max_bytes", "endpoint", "auth_env", "max_retries",
    "retry_backoff_seconds", "min_interval_seconds", "rate_state_path",
    "allow_anonymous_fallback",
}
TOPIC_BIBLIOGRAPHY_FIELDS = TOPIC_BIBLIOGRAPHY_CLIENT_FIELDS | {
    "cache_path", "cache_ttl_seconds",
}
TOPIC_BUDGET_FIELDS = {
    "max_model_calls", "max_openalex_requests", "max_input_tokens", "max_output_tokens",
}
CANDIDATE_FIELDS = {
    "id", "title", "domain", "research_question", "scope", "search_queries",
    "why_promising", "disconfirmation_test", "feasibility", "resource_plan",
    "capability_requirements",
}
# These dimensions are optional for compatibility with earlier topic packages,
# but are part of the current research-direction contract when supplied.  The
# prompt explicitly asks the model to vary them, so rejecting them as unknown
# fields turns a scientifically richer answer into an intake failure.
CANDIDATE_DIMENSION_FIELDS = {
    "mechanism", "data_regime", "comparison", "measurement", "theory_target",
    "phenomenon", "disconfirmation_test_note",
}
GROUNDING_FIELDS = {"frontier_seed_id", "prior_work_ids"}
LEGACY_CANDIDATE_FIELDS = CANDIDATE_FIELDS - {"capability_requirements"}
CATALOG_CANDIDATE_FIELDS = CANDIDATE_FIELDS | {"experiment_capability_id"}
CATALOG_LEGACY_CANDIDATE_FIELDS = LEGACY_CANDIDATE_FIELDS | {"experiment_capability_id"}
CAPABILITY_FIELDS = {"executables", "python_packages", "stage_kinds"}
KNOWN_STAGE_KINDS = {"topic_discovery", "survey", "experiment", "interpretation", "argument", "paper"}

# Design-driven experiment capabilities accept a bounded declarative study
# DESIGN as data (never code).  This is the shared schema: the topic stage
# proposes a design, the Composer injects it, and the pinned engine validates it
# again at execution time.
EXPERIMENT_DESIGN_FAMILY = "monte_carlo_estimator_comparison"
DESIGN_ESTIMATORS = ("mean", "median", "trimmed_mean", "winsorized_mean", "median_of_means")
DESIGN_PROCESSES = ("gaussian_contamination", "student_t", "lognormal_shifted")
DESIGN_PROCESS_FIELDS = {
    "gaussian_contamination": {"kind", "sample_size", "contamination_rate", "contamination_scale"},
    "student_t": {"kind", "sample_size", "df"},
    "lognormal_shifted": {"kind", "sample_size", "mu", "sigma"},
}
DESIGN_OPTIONAL_FIELDS = {"trim_fraction", "block_size"}


def _design_driven_ids(runtime_context):
    """Return the capability ids whose templates accept a proposed design."""
    catalog = (runtime_context or {}).get("experiment_catalog") or []
    return {item.get("id") for item in catalog
            if isinstance(item, dict) and item.get("design_driven")
            and isinstance(item.get("id"), str)}


def validate_experiment_design(value):
    """Validate one bounded declarative experiment design."""
    required = {"family", "data_process", "estimators", "primary", "baseline", "seed"}
    if not isinstance(value, dict) or set(value) - (required | DESIGN_OPTIONAL_FIELDS) \
            or not required.issubset(value):
        raise ValidationError(
            f"experiment_design requires {sorted(required)} and permits {sorted(DESIGN_OPTIONAL_FIELDS)}")
    if value["family"] != EXPERIMENT_DESIGN_FAMILY:
        raise ValidationError("experiment_design family is unsupported")
    process = value["data_process"]
    kind = process.get("kind") if isinstance(process, dict) else None
    if kind not in DESIGN_PROCESSES or set(process) != DESIGN_PROCESS_FIELDS[kind]:
        raise ValidationError("experiment_design data_process is unsupported or incomplete")
    if type(process["sample_size"]) is not int or not 8 <= process["sample_size"] <= 20000:
        raise ValidationError("experiment_design sample_size must be an integer between 8 and 20000")
    if kind == "gaussian_contamination":
        if not 0.0 <= float(process["contamination_rate"]) <= 0.9:
            raise ValidationError("experiment_design contamination_rate must be within [0, 0.9]")
        if not 0.5 <= float(process["contamination_scale"]) <= 1e6:
            raise ValidationError("experiment_design contamination_scale must be within [0.5, 1e6]")
    if kind == "student_t" and not 1.0 < float(process["df"]) <= 200.0:
        raise ValidationError("experiment_design student_t df must be within (1, 200]")
    if kind == "lognormal_shifted":
        if not 0.05 <= float(process["sigma"]) <= 5.0:
            raise ValidationError("experiment_design lognormal sigma must be within [0.05, 5.0]")
        float(process["mu"])
    estimators = value["estimators"]
    if (not isinstance(estimators, list) or not 2 <= len(estimators) <= len(DESIGN_ESTIMATORS)
            or len(set(estimators)) != len(estimators)
            or any(item not in DESIGN_ESTIMATORS for item in estimators)):
        raise ValidationError("experiment_design estimators must be a unique supported list")
    for key in ("primary", "baseline"):
        if value[key] not in estimators:
            raise ValidationError(f"experiment_design {key} must name one of the selected estimators")
    if value["primary"] == value["baseline"]:
        raise ValidationError("experiment_design primary and baseline must differ")
    if type(value["seed"]) is not int or value["seed"] < 0:
        raise ValidationError("experiment_design seed must be a non-negative integer")
    if "median_of_means" in estimators:
        block = value.get("block_size")
        if type(block) is not int or not 2 <= block <= int(process["sample_size"]):
            raise ValidationError("experiment_design block_size must be in [2, sample_size] for median_of_means")
    if {"trimmed_mean", "winsorized_mean"} & set(estimators):
        fraction = value.get("trim_fraction")
        if not isinstance(fraction, (int, float)) or not 0.0 <= float(fraction) < 0.5:
            raise ValidationError("experiment_design trim_fraction must be within [0, 0.5)")
    canonical_bytes(value)
    return deepcopy(value)
MATURITY_DIMENSIONS = (
    "question_specificity", "mechanism_depth", "comparison_design",
    "contribution_potential", "falsifiability",
)
REFINEMENT_DIMENSIONS = (
    "mechanism", "data_regime", "comparison", "measurement", "theory",
)
MATURITY_REVIEW_FIELDS = {
    "decision", "selected_id", "scores", "rationale", "required_changes",
    "changed_dimensions",
}
MATURITY_MIN_TOTAL = 15
MATURITY_MIN_DIMENSION = 2

_TOPIC_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "does", "do", "for", "from",
    "how", "in", "into", "is", "of", "on", "or", "relative", "the", "their", "this", "to",
    "under", "versus", "what", "when", "which", "with", "without", "using", "across", "between",
    "within", "will", "may", "than", "that", "these", "those", "over", "same", "one", "two",
}

_MISSION_BOILERPLATE = {
    "autonomous", "workflow", "evidence", "capability", "capabilities", "human", "release",
    "gate", "gates", "installed", "executable", "research", "scientific", "question", "novel",
    "testable", "experiment", "paper", "review", "reviewed", "explicit", "selected", "select",
}


def _text(value, name, *, public=True):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    if public and find_control_leaks(value):
        raise ValidationError(f"{name} exposes control-plane vocabulary")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _strings(value, name, *, minimum=1, maximum=8, public=True):
    if (not isinstance(value, list) or not minimum <= len(value) <= maximum
            or any(not isinstance(item, str) for item in value)):
        raise ValidationError(f"{name} must be a unique list of {minimum} to {maximum} items")
    if len(value) != len(set(value)):
        raise ValidationError(f"{name} must be a unique list of {minimum} to {maximum} items")
    for item in value:
        _text(item, name, public=public)
        if len(item) > 2048:
            raise ValidationError(f"{name} items are too long")
    return value


def _validate_capability_requirements(value, name="capability_requirements"):
    if not isinstance(value, dict) or set(value) != CAPABILITY_FIELDS:
        raise ValidationError(f"{name} requires exactly {sorted(CAPABILITY_FIELDS)}")
    for key in ("executables", "python_packages"):
        _strings(value[key], f"{name}.{key}", minimum=0, maximum=16)
    _strings(value["stage_kinds"], f"{name}.stage_kinds", minimum=0, maximum=8)
    if set(value["stage_kinds"]) - KNOWN_STAGE_KINDS:
        raise ValidationError(f"{name}.stage_kinds contains an unsupported stage kind")
    return value


def _validate_topic_budgets(value):
    """Validate finite intake quotas before a provider or model is called."""
    if (not isinstance(value, dict) or not value
            or set(value) - TOPIC_BUDGET_FIELDS):
        raise ValidationError(
            f"topic discovery budgets require at least one of {sorted(TOPIC_BUDGET_FIELDS)}")
    maximums = {
        "max_model_calls": 128,
        "max_openalex_requests": 128,
        "max_input_tokens": 2_000_000,
        "max_output_tokens": 2_000_000,
    }
    for key, maximum in maximums.items():
        if key not in value:
            continue
        limit = value[key]
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValidationError(
                f"topic discovery budgets.{key} must be an integer between 1 and {maximum}")
    canonical_bytes(value)
    return deepcopy(value)


class TopicBudget:
    """Account for the finite external work allowed by one topic intake."""

    def __init__(self, limits=None, usage=None):
        self.limits = _validate_topic_budgets(limits) if limits is not None else {}
        self.usage = usage if isinstance(usage, dict) else {}
        self.usage.setdefault("openalex_requests", 0)
        self.events = []
        self._active_event = None

    def _raise(self, dimension, limit, observed):
        snapshot = self.snapshot()
        raise QuotaExceededError(
            f"topic discovery quota exhausted: {dimension}={observed}, limit={limit}",
            dimension=dimension, limit=limit, observed=observed,
            usage=snapshot["usage"], diagnostics=snapshot["events"])

    def _check_available(self, key, dimension):
        limit = self.limits.get(key)
        observed = self.usage.get(dimension, 0)
        if limit is not None and observed >= limit:
            self._raise(dimension, limit, observed)

    def before_model_call(self, role=None, model=None):
        self._check_available("max_model_calls", "model_calls")
        # Count the dispatch before the provider call.  A transport timeout or
        # other model error still consumed an external call and must not be
        # invisible to the intake quota.
        self.usage["model_calls"] = self.usage.get("model_calls", 0) + 1
        event = {
            "sequence": len(self.events) + 1, "kind": "model",
            "role": role, "model": model, "status": "dispatched",
            "model_call_number": self.usage["model_calls"],
        }
        self.events.append(event)
        self._active_event = event

    def record_model_result(self, result):
        if self._active_event is not None:
            self._active_event.update(
                status="response", finish_reason=result.finish_reason,
                elapsed_seconds=result.elapsed_seconds,
                request_attempts=getattr(result, "request_attempts", 1),
                reported_usage=deepcopy(result.usage),
            )
        for key in ("input_tokens", "output_tokens"):
            value = result.usage.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                value = 0
            self.usage[key] = self.usage.get(key, 0) + value
            limit = self.limits.get(f"max_{key}")
            if limit is not None and self.usage[key] > limit:
                self._raise(key, limit, self.usage[key])
        self._active_event = None

    def record_model_error(self, error):
        if self._active_event is not None:
            self._active_event.update(
                status="error", error=str(error)[:2048],
                request_attempts=getattr(error, "attempts", 0),
                elapsed_seconds=getattr(error, "elapsed_seconds", None),
            )
            self._active_event = None

    def record_validation_error(self, error):
        if self._active_event is not None:
            self._active_event.update(status="rejected", error=str(error)[:2048])
            self._active_event = None
            return
        # Validation can happen after the external event has already been
        # closed, for example when an independent source challenger rejects a
        # valid JSON response.  Keep that decision in the bounded-intake
        # trace instead of silently turning it into an unexplained retry.
        self.events.append({
            "sequence": len(self.events) + 1,
            "kind": "validation",
            "status": "rejected",
            "error": str(error)[:2048],
        })

    def before_openalex_request(self, query=None):
        self._check_available("max_openalex_requests", "openalex_requests")
        self.usage["openalex_requests"] = self.usage.get("openalex_requests", 0) + 1
        event = {
            "sequence": len(self.events) + 1, "kind": "openalex",
            "query": query[:512] if isinstance(query, str) else None,
            "status": "dispatched",
            "request_number": self.usage["openalex_requests"],
        }
        self.events.append(event)
        self._active_event = event

    def record_openalex_result(self, result):
        if self._active_event is not None:
            metadata = result.get("metadata") if isinstance(result, dict) else {}
            metadata = metadata if isinstance(metadata, dict) else {}
            rate_limit = metadata.get("rate_limit")
            self._active_event.update(
                status="response", outcome=result.get("outcome") if isinstance(result, dict) else None,
                error=(str(result.get("error"))[:2048]
                       if isinstance(result, dict) and result.get("error") else None),
                attempts=metadata.get("attempts"),
                rate_limit_kind=(rate_limit.get("kind") if isinstance(rate_limit, dict) else None),
            )
            self._active_event = None

    def snapshot(self):
        return {"limits": deepcopy(self.limits),
                "usage": {key: self.usage.get(key, 0) for key in (
                    "model_calls", "input_tokens", "output_tokens", "openalex_requests")},
                "events": deepcopy(self.events)}


def validate_topic_stage_config(value):
    """Validate the descriptor consumed by the Composer topic stage."""
    fields = {"schema_version", "model_config_path", "output_path", "candidate_count", "max_attempts"}
    allowed = fields | {"repair_mode", "maturity_review_rounds", "bibliography", "budgets"}
    if (not isinstance(value, dict) or set(value) - allowed
            or not fields.issubset(value)):
        raise ValidationError(
            f"topic discovery config requires {sorted(fields)} and permits repair_mode, bibliography, budgets")
    if value["schema_version"] != STAGE_CONFIG_SCHEMA_VERSION:
        raise ValidationError("topic discovery config schema version is unsupported")
    model_path = Path(value["model_config_path"])
    if not model_path.is_absolute() or not model_path.is_file():
        raise ValidationError("topic discovery model_config_path must be an existing absolute file")
    output_path = Path(value["output_path"])
    if not output_path.is_absolute():
        raise ValidationError("topic discovery output_path must be absolute")
    if type(value["candidate_count"]) is not int or not 3 <= value["candidate_count"] <= 8:
        raise ValidationError("topic discovery candidate_count must be between 3 and 8")
    if type(value["max_attempts"]) is not int or not 1 <= value["max_attempts"] <= 8:
        raise ValidationError("topic discovery max_attempts must be between 1 and 8")
    repair_mode = value.get("repair_mode", "bounded")
    if repair_mode not in {"bounded", "until_deadline"}:
        raise ValidationError("topic discovery repair_mode must be bounded or until_deadline")
    maturity_rounds = value.get("maturity_review_rounds", 0)
    if type(maturity_rounds) is not int or not 0 <= maturity_rounds <= 4:
        raise ValidationError("topic discovery maturity_review_rounds must be between 0 and 4")
    if "budgets" in value:
        _validate_topic_budgets(value["budgets"])
    bibliography = value.get("bibliography")
    if bibliography is not None:
        if not isinstance(bibliography, dict) or set(bibliography) - TOPIC_BIBLIOGRAPHY_FIELDS:
            raise ValidationError("topic discovery bibliography contains unsupported fields")
        client = {key: item for key, item in bibliography.items()
                  if key in TOPIC_BIBLIOGRAPHY_CLIENT_FIELDS}
        try:
            OpenAlexClient(**client)
        except (TypeError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc
        cache_path = bibliography.get("cache_path")
        if cache_path is not None and (
                not isinstance(cache_path, str) or not Path(cache_path).is_absolute()
                or Path(cache_path).exists() and not Path(cache_path).is_file()):
            raise ValidationError("topic discovery bibliography cache_path must be an absolute file path")
        ttl = bibliography.get("cache_ttl_seconds", 7 * 24 * 3600)
        if type(ttl) not in (int, float) or not math.isfinite(ttl) or ttl <= 0:
            raise ValidationError("topic discovery bibliography cache_ttl_seconds must be finite and positive")
    canonical_bytes(value)
    return deepcopy(value)


def validate_frontier_seed_plan(value, *, seed_count=None):
    """Validate science-first search directions before capability matching."""
    fields = {"schema_version", "seeds"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"frontier seed plan requires exactly {sorted(fields)}")
    if value["schema_version"] != FRONTIER_SEED_SCHEMA_VERSION:
        raise ValidationError("frontier seed plan schema version is unsupported")
    seeds = value["seeds"]
    if (not isinstance(seeds, list) or not 4 <= len(seeds) <= 8
            or seed_count is not None and len(seeds) != seed_count):
        raise ValidationError("frontier seed plan requires the configured four to eight seeds")
    ids, domains, queries = set(), set(), set()
    for seed in seeds:
        if not isinstance(seed, dict) or set(seed) != FRONTIER_SEED_FIELDS:
            raise ValidationError("frontier seed has an invalid shape")
        identifier = _identifier(seed["id"], "frontier seed id")
        if identifier in ids:
            raise ValidationError("frontier seed IDs must be unique")
        ids.add(identifier)
        for key in ("domain", "phenomenon", "mechanism", "unit_of_analysis"):
            # Frontier seeds are internal scientific search scaffolding, not
            # reader-facing prose.  Applying the manuscript surface gate here
            # made a single model word such as "validator" discard an
            # otherwise useful seed before candidate generation could project
            # it into clean scientific language.
            _text(seed[key], f"frontier seed {key}", public=False)
        domains.add(seed["domain"].strip().casefold())
        seed_terms = set().union(*(
            _topic_tokens(seed[key]) for key in (
                "domain", "phenomenon", "mechanism", "unit_of_analysis")
        )) - _MISSION_BOILERPLATE
        _strings(seed["search_queries"], "frontier seed search_queries",
                 minimum=2, maximum=3, public=False)
        for query in seed["search_queries"]:
            content = set(_topic_tokens(query)) - _MISSION_BOILERPLATE
            if len(content) < 2:
                raise ValidationError("frontier search query is mission boilerplate rather than scientific terminology")
            if not content.intersection(seed_terms):
                raise ValidationError(
                    "frontier search query is not anchored to its declared scientific seed")
            normalized = " ".join(query.casefold().split())
            if normalized in queries:
                raise ValidationError("frontier search queries must be unique across seeds")
            queries.add(normalized)
    if len(domains) < min(4, len(seeds)):
        raise ValidationError("frontier seed plan must span at least four distinct scientific domains")
    canonical_bytes(value)
    return deepcopy(value)


def validate_source_challenge(value, *, selected_id, work_ids):
    """Validate the pre-survey relevance and template-independence challenge."""
    if not isinstance(value, dict) or set(value) != SOURCE_CHALLENGE_FIELDS:
        raise ValidationError(
            f"topic source challenge requires exactly {sorted(SOURCE_CHALLENGE_FIELDS)}")
    if value["schema_version"] != SOURCE_CHALLENGE_SCHEMA_VERSION:
        raise ValidationError("topic source challenge schema version is unsupported")
    if value["decision"] not in {"admit_to_survey", "refine"}:
        raise ValidationError("topic source challenge decision must be admit_to_survey or refine")
    if value["selected_id"] != selected_id:
        raise ValidationError("topic source challenge selected_id does not match the selected topic")
    for key in ("source_relevance", "template_independence"):
        if type(value[key]) is not int or not 0 <= value[key] <= 4:
            raise ValidationError(f"topic source challenge {key} must be an integer from 0 to 4")
    if value["prior_work_risk"] not in {"low", "medium", "high"}:
        raise ValidationError("topic source challenge prior_work_risk is invalid")
    _strings(value["closest_work_ids"], "topic source challenge closest_work_ids",
             minimum=0, maximum=8)
    if set(value["closest_work_ids"]) - set(work_ids):
        raise ValidationError("topic source challenge cites work IDs outside its supplied evidence")
    _text(value["rationale"], "topic source challenge rationale")
    _strings(value["required_changes"], "topic source challenge required_changes",
             minimum=0, maximum=8)
    if value["decision"] == "refine" and not value["required_changes"]:
        raise ValidationError("refined topic source challenge requires substantive changes")
    if value["decision"] == "admit_to_survey" and value["required_changes"]:
        raise ValidationError("admitted topic source challenge cannot retain required changes")
    if value["decision"] == "admit_to_survey" and work_ids and not value["closest_work_ids"]:
        raise ValidationError(
            "admitted topic source challenge must identify at least one closest supplied work")
    canonical_bytes(value)
    return deepcopy(value)


def _source_challenge_admitted(review):
    """Return whether a validated source challenge crosses the intake gate."""
    return (
        review["decision"] == "admit_to_survey"
        and review["source_relevance"] >= 2
        and review["template_independence"] >= 3
        and review["prior_work_risk"] != "high"
    )


def validate_topic_package(value, *, objective=None, candidate_count=None,
                           experiment_capability_ids=None,
                           require_capability_coverage=False,
                           excluded_capability_ids=None,
                           excluded_topic_ids=None, topic_history=None,
                           design_driven_capability_ids=None,
                           frontier_seeds=None, recent_papers=None,
                           require_grounding=False, fallback_templates=None):
    """Validate a complete topic proposal before it enters the survey stage."""
    fields = {"schema_version", "objective", "candidates", "selected_id", "selection_rationale"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"topic discovery package requires exactly {sorted(fields)}")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError("topic discovery package schema version is unsupported")
    _text(value["objective"], "topic objective")
    if objective is not None and value["objective"] != objective:
        raise ValidationError("topic discovery package changed the Principal objective")
    candidates = value["candidates"]
    if (not isinstance(candidates, list) or not 3 <= len(candidates) <= 8
            or candidate_count is not None and len(candidates) != candidate_count):
        raise ValidationError("topic discovery requires the configured number of candidates")
    ids = set()
    capability_ids = set(experiment_capability_ids or [])
    design_driven = set(design_driven_capability_ids or [])
    excluded_capabilities = set(excluded_capability_ids or [])
    excluded_topics = set(excluded_topic_ids or [])
    seed_records = {
        item.get("id"): item for item in (frontier_seeds or [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    work_records = {
        item.get("work_id"): item for item in (recent_papers or [])
        if isinstance(item, dict) and isinstance(item.get("work_id"), str)
    }
    grounded_seed_ids = set()
    grounded_domains = set()
    if require_grounding and (not seed_records or not work_records):
        raise ValidationError(
            "current topic candidates require supplied frontier seeds and scholarly records")
    for candidate in candidates:
        shapes = (CANDIDATE_FIELDS, LEGACY_CANDIDATE_FIELDS,
                  CATALOG_CANDIDATE_FIELDS, CATALOG_LEGACY_CANDIDATE_FIELDS)
        candidate_keys = set(candidate) if isinstance(candidate, dict) else set()
        shape_options = [
            (shape, extra, shape | CANDIDATE_DIMENSION_FIELDS | extra)
            for shape in shapes
            for extra in ({"experiment_design"}, GROUNDING_FIELDS,
                          GROUNDING_FIELDS | {"experiment_design"}, set())
        ]
        shape_valid = any(
            shape.issubset(candidate_keys) and candidate_keys <= allowed
            for shape, _extra, allowed in shape_options
        )
        if not isinstance(candidate, dict) or not shape_valid:
            if not isinstance(candidate, dict):
                detail = f"type={type(candidate).__name__}"
            else:
                _shape, _extra, allowed = min(
                    shape_options,
                    key=lambda item: (
                        len(item[0] - candidate_keys) + len(candidate_keys - item[2]),
                        len(candidate_keys - item[2]),
                    ),
                )
                missing = sorted(_shape - candidate_keys)
                unexpected = sorted(candidate_keys - allowed)
                detail = f"missing={missing[:8]}, unexpected={unexpected[:8]}"
            raise ValidationError(f"topic candidate has an invalid shape ({detail})")
        _identifier(candidate["id"], "topic candidate id")
        if candidate["id"] in ids:
            raise ValidationError("topic candidate IDs must be unique")
        ids.add(candidate["id"])
        for key in ("title", "domain", "research_question", "scope", "why_promising",
                    "disconfirmation_test", "feasibility", "resource_plan"):
            _text(candidate[key], f"topic candidate {key}")
        for key in CANDIDATE_DIMENSION_FIELDS.intersection(candidate):
            _text(candidate[key], f"topic candidate {key}")
        _strings(candidate["search_queries"], "topic candidate search_queries", minimum=3, maximum=8)
        if require_grounding:
            if not GROUNDING_FIELDS.issubset(candidate):
                raise ValidationError(
                    "current topic candidate must bind a frontier_seed_id and prior_work_ids")
            seed_id = _identifier(candidate["frontier_seed_id"], "topic candidate frontier_seed_id")
            if seed_id not in seed_records:
                raise ValidationError("topic candidate frontier_seed_id is outside the supplied seed plan")
            prior_ids = _strings(
                candidate["prior_work_ids"], "topic candidate prior_work_ids",
                minimum=1, maximum=5,
            )
            if set(prior_ids) - set(work_records):
                raise ValidationError(
                    "topic candidate prior_work_ids cite records outside the supplied evidence")
            if not any(work_records[work_id].get("frontier_seed_id") == seed_id
                       for work_id in prior_ids):
                available_ids = sorted(
                    work_id for work_id, record in work_records.items()
                    if record.get("frontier_seed_id") == seed_id
                )[:8]
                raise ValidationError(
                    "topic candidate "
                    f"{candidate['id']} must cite a supplied work from frontier seed "
                    f"{seed_id}; available work IDs for that seed are {available_ids}")
            candidate_tokens = set().union(*(
                _topic_tokens(candidate[key]) for key in
                ("title", "domain", "research_question", "scope")
            )) - _MISSION_BOILERPLATE
            seed_tokens = set().union(*(
                _topic_tokens(seed_records[seed_id].get(key, "")) for key in
                ("domain", "phenomenon", "mechanism", "unit_of_analysis")
            )) - _MISSION_BOILERPLATE
            source_tokens = set().union(*(
                _topic_tokens(str(work_records[work_id].get("title", "")) + " "
                              + str(work_records[work_id].get("abstract", "") or ""))
                for work_id in prior_ids
            )) - _MISSION_BOILERPLATE
            if len(candidate_tokens.intersection(seed_tokens | source_tokens)) < 2:
                raise ValidationError(
                    "topic candidate prose is not grounded in its seed or cited scholarly records")
            query_anchors = candidate_tokens | seed_tokens
            for query in candidate["search_queries"]:
                query_tokens = set(_topic_tokens(query)) - _MISSION_BOILERPLATE
                if len(query_tokens) < 2 or not query_tokens.intersection(query_anchors):
                    raise ValidationError(
                        "topic candidate search query is not anchored to its scientific direction")
            grounded_seed_ids.add(seed_id)
            grounded_domains.add(candidate["domain"].strip().casefold())
        if "capability_requirements" in candidate:
            _validate_capability_requirements(candidate["capability_requirements"])
        if capability_ids:
            selected_capability = candidate.get("experiment_capability_id")
            if not isinstance(selected_capability, str) or selected_capability not in capability_ids:
                raise ValidationError(
                    "topic candidate must select one configured experiment capability")
            if "experiment_design" in candidate:
                validate_experiment_design(candidate["experiment_design"])
                if selected_capability not in design_driven:
                    raise ValidationError(
                        "topic candidate supplies an experiment_design for a capability that does not accept one")
            elif selected_capability in design_driven:
                raise ValidationError(
                    "design-driven experiment capability requires an experiment_design for its candidate")
        elif "experiment_design" in candidate:
            raise ValidationError("topic candidate cannot supply an experiment_design without a capability catalog")
    if capability_ids and require_capability_coverage:
        observed = {candidate.get("experiment_capability_id") for candidate in candidates}
        required = min(len(capability_ids), len(candidates))
        if len(observed) < required:
            missing = sorted(capability_ids - observed)
            raise ValidationError(
                f"topic candidates must cover {required} distinct experiment capabilities; "
                f"missing={missing}")
    if require_grounding:
        required_groups = min(3, len(candidates), len(seed_records))
        if len(grounded_seed_ids) < required_groups:
            raise ValidationError(
                f"topic candidates must cover at least {required_groups} frontier seed groups")
        if len(grounded_domains) < required_groups:
            raise ValidationError(
                f"topic candidates must span at least {required_groups} scientific domains")
        # A model can satisfy the seed/domain floor while repeating one generic
        # question skeleton.  Reject that collapse when two candidates share
        # a domain or capability; distinct domains may legitimately reuse a
        # short interrogative form while testing different phenomena.
        for index, left in enumerate(candidates):
            left_tokens = _topic_tokens(left["research_question"])
            for right in candidates[index + 1:]:
                question_overlap = _jaccard(
                    left_tokens, _topic_tokens(right["research_question"]))
                same_domain = (
                    left["domain"].strip().casefold()
                    == right["domain"].strip().casefold())
                same_capability = (
                    left.get("experiment_capability_id") is not None
                    and left.get("experiment_capability_id")
                    == right.get("experiment_capability_id"))
                if question_overlap >= 0.9 and (same_domain or same_capability):
                    raise ValidationError(
                        "topic candidate portfolio contains near-duplicate research questions")
    _identifier(value["selected_id"], "selected topic id")
    if value["selected_id"] not in ids:
        raise ValidationError("selected topic is not one of the candidates")
    selected = next(candidate for candidate in candidates if candidate["id"] == value["selected_id"])
    if selected["id"] in excluded_topics:
        raise ValidationError("selected topic is excluded by the exploration history")
    if excluded_capabilities and selected.get("experiment_capability_id") in excluded_capabilities:
        raise ValidationError("selected experiment capability is excluded by the exploration history")
    if topic_history:
        validate_topic_novelty(selected, topic_history)
    for template in fallback_templates or []:
        if not isinstance(template, dict) or not isinstance(template.get("research_question"), str):
            continue
        prior = {
            "title": str(template.get("id") or template.get("domain") or "template"),
            "domain": str(template.get("domain") or ""),
            "research_question": template["research_question"],
            "experiment_capability_id": template.get("id"),
        }
        if _topic_repeat_score(selected, prior) >= 0.72:
            raise ValidationError(
                "selected topic is too similar to a fallback experiment template")
    _text(value["selection_rationale"], "topic selection rationale")
    canonical_bytes(value)
    return deepcopy(value)


def _topic_tokens(value):
    """Return content-bearing tokens used for a conservative repeat check.

    This is deliberately a small lexical guard, not a novelty or plagiarism
    detector.  It catches a model reusing the same question with a new title
    while leaving scientific novelty to the literature and reviewer stages.
    Numbers are normalized so changing a threshold or a split count does not
    make an otherwise identical direction look new.
    """
    text = value if isinstance(value, str) else ""
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9]*", text.casefold())
    return {"<number>" if token.isdigit() else token
            for token in tokens if token not in _TOPIC_STOPWORDS and len(token) > 2}


def topic_signature(candidate):
    """Create a stable, reader-independent signature for a topic direction."""
    if not isinstance(candidate, dict):
        raise ValidationError("topic signature requires a candidate object")
    title = candidate.get("title", "")
    question = candidate.get("research_question", "")
    domain = candidate.get("domain", "")
    normalized_question = " ".join(str(question).casefold().split())
    normalized_title = " ".join(str(title).casefold().split())
    combined = "\n".join((normalized_title, normalized_question, str(domain).casefold()))
    return {
        "question": normalized_question,
        "title": normalized_title,
        "question_tokens": sorted(_topic_tokens(question)),
        "title_tokens": sorted(_topic_tokens(title)),
        "content_tokens": sorted(_topic_tokens(combined)),
        "fingerprint": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
    }


def _topic_history_entries(topic_history):
    if isinstance(topic_history, dict):
        entries = topic_history.get("entries", [])
    else:
        entries = topic_history
    return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []


def _jaccard(left, right):
    left, right = set(left), set(right)
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _topic_repeat_score(candidate, prior):
    """Score likely reuse of a previous selected direction in [0, 1]."""
    current = topic_signature(candidate)
    previous = prior.get("signature") if isinstance(prior, dict) else None
    if not isinstance(previous, dict):
        previous = topic_signature(prior)
    if current["fingerprint"] == previous.get("fingerprint"):
        return 1.0
    if current["question"] and current["question"] == previous.get("question"):
        return 1.0
    question_score = _jaccard(current["question_tokens"], previous.get("question_tokens", []))
    title_score = _jaccard(current["title_tokens"], previous.get("title_tokens", []))
    content_score = _jaccard(current["content_tokens"], previous.get("content_tokens", []))
    same_capability = (
        candidate.get("experiment_capability_id") is not None
        and candidate.get("experiment_capability_id") == prior.get("experiment_capability_id")
    )
    # A repeated method/comparison tends to retain several content anchors
    # even when the model changes the prose.  Require both a high question
    # overlap or several shared anchors and the same pinned capability; this
    # avoids rejecting genuinely different questions in one capability.
    shared_anchors = len(set(current["title_tokens"]) & set(previous.get("title_tokens", [])))
    if question_score >= 0.78:
        return question_score
    if same_capability and shared_anchors >= 3 and content_score >= 0.32:
        return max(content_score, 0.78)
    return max(question_score, title_score * 0.85, content_score * 0.55)


def validate_topic_novelty(candidate, topic_history, *, threshold=0.78):
    """Reject a selected direction that repeats a recorded project direction.

    History is an execution-memory guard.  It never proves novelty and it does
    not inspect external literature; it only prevents a free-topic Composer
    from silently spending another mission on the same direction.
    """
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 < threshold <= 1:
        raise ValidationError("topic novelty threshold must be finite and in (0, 1]")
    current_id = candidate.get("id") if isinstance(candidate, dict) else None
    for prior in _topic_history_entries(topic_history):
        if current_id and current_id == prior.get("topic_id"):
            raise ValidationError("selected topic repeats a previously attempted direction")
        score = _topic_repeat_score(candidate, prior)
        if score >= threshold:
            raise ValidationError(
                "selected topic is too similar to a previously attempted direction")
    return True


def validate_topic_feasibility(package, runtime_context):
    """Check the selected direction against the Composer's declared tools.

    Capability requirements are intentionally structured so the admission
    decision does not depend on trusting a free-form feasibility paragraph.
    Legacy topic packages remain readable, but a current Composer prompt must
    provide the structure whenever it supplies a runtime inventory.
    """
    if not isinstance(runtime_context, dict):
        return {"status": "not_checked", "unavailable": []}
    selected = next(item for item in package["candidates"] if item["id"] == package["selected_id"])
    catalog = runtime_context.get("experiment_catalog") or []
    if catalog:
        allowed = {item.get("id") for item in catalog if isinstance(item, dict)}
        if selected.get("experiment_capability_id") not in allowed:
            raise ValidationError(
                "selected topic must bind to one configured experiment capability")
        excluded = set((runtime_context.get("topic_exclusions") or {}).get("capability_ids", []))
        if selected.get("experiment_capability_id") in excluded:
            raise ValidationError("selected topic uses a capability excluded by the exploration history")
    requirements = selected.get("capability_requirements")
    if requirements is None:
        return {"status": "legacy_unchecked", "unavailable": []}
    _validate_capability_requirements(requirements)
    unavailable = []
    for name in requirements["executables"]:
        if not (runtime_context.get("executables") or {}).get(name, False):
            unavailable.append({"kind": "executable", "name": name})
    for name in requirements["python_packages"]:
        if not (runtime_context.get("python_packages") or {}).get(name, False):
            unavailable.append({"kind": "python_package", "name": name})
    configured = set(runtime_context.get("configured_stage_kinds") or [])
    for name in requirements["stage_kinds"]:
        if name not in configured:
            unavailable.append({"kind": "stage_kind", "name": name})
    if unavailable:
        raise ValidationError(
            "selected topic requires unavailable capabilities: "
            + ", ".join(f"{item['kind']}={item['name']}" for item in unavailable))
    return {"status": "feasible", "unavailable": [], "requirements": deepcopy(requirements)}


def validate_topic_maturity_review(value, *, candidate_ids=None):
    """Validate the independent pre-admission review of a topic portfolio.

    The review is a quality screen, not a novelty verdict.  Its purpose is to
    stop an executable but scientifically thin direction from entering the
    survey or experiment unchanged.  A later literature assessment remains
    authoritative about prior work and the existence of a defensible gap.
    """
    if not isinstance(value, dict) or set(value) != MATURITY_REVIEW_FIELDS:
        raise ValidationError(
            f"topic maturity review requires exactly {sorted(MATURITY_REVIEW_FIELDS)}")
    if value["decision"] not in {"admit", "refine"}:
        raise ValidationError("topic maturity review decision must be admit or refine")
    _identifier(value["selected_id"], "topic maturity review selected_id")
    if candidate_ids is not None and value["selected_id"] not in set(candidate_ids):
        raise ValidationError("topic maturity review selected_id is not a candidate")
    scores = value["scores"]
    if (not isinstance(scores, dict) or set(scores) != set(MATURITY_DIMENSIONS)):
        raise ValidationError(
            f"topic maturity review scores require exactly {list(MATURITY_DIMENSIONS)}")
    for dimension in MATURITY_DIMENSIONS:
        score = scores[dimension]
        if type(score) is not int or not 0 <= score <= 4:
            raise ValidationError(f"topic maturity review {dimension} must be an integer from 0 to 4")
    _text(value["rationale"], "topic maturity review rationale")
    _strings(value["required_changes"], "topic maturity review required_changes", minimum=0, maximum=8)
    changed = value["changed_dimensions"]
    if (not isinstance(changed, list) or len(changed) != len(set(changed))
            or any(item not in REFINEMENT_DIMENSIONS for item in changed)):
        raise ValidationError("topic maturity review changed_dimensions is invalid")
    if value["decision"] == "refine" and not value["required_changes"]:
        raise ValidationError("topic maturity review refinement requires required_changes")
    if value["decision"] == "admit" and value["required_changes"]:
        raise ValidationError("admitted topic maturity review cannot retain required_changes")
    canonical_bytes(value)
    return deepcopy(value)


def topic_maturity_admitted(review, *, minimum_total=MATURITY_MIN_TOTAL,
                            minimum_dimension=MATURITY_MIN_DIMENSION):
    """Return whether a validated review clears the generic research floor."""
    validate_topic_maturity_review(review)
    if type(minimum_total) is not int or not 0 <= minimum_total <= 20:
        raise ValidationError("topic maturity minimum_total must be between 0 and 20")
    if type(minimum_dimension) is not int or not 0 <= minimum_dimension <= 4:
        raise ValidationError("topic maturity minimum_dimension must be between 0 and 4")
    scores = review["scores"]
    total = sum(scores.values())
    return review["decision"] == "admit" and total >= minimum_total and all(
        score >= minimum_dimension for score in scores.values())


FRONTIER_SYSTEM = (
    "You are a scientific horizon scanner. Generate independent, science-first search seeds before any "
    "experiment capability is shown. Deliberately span remote domains and combine a concrete phenomenon, "
    "a plausible mechanism, and an observable unit. Avoid generic AI, workflow, research-method, and "
    "autonomous-laboratory topics. Do not copy familiar textbook demonstrations. Search strings must use "
    "domain terminology that a scholarly index can retrieve. Do not claim novelty or results. Return JSON only."
)


def _frontier_seed_prompt(objective, seed_count, sampling_seed):
    return json.dumps({
        "assignment": "science_first_frontier_seed_generation",
        "principal_boundary": objective,
        "exploration_seed": sampling_seed,
        "seed_count": seed_count,
            "output_contract": {
            "schema_version": FRONTIER_SEED_SCHEMA_VERSION,
            "seeds": [{
                "id": "bounded lowercase identifier such as frontier_1; never use spaces or uppercase",
                "domain": "specific scientific domain; domains must differ across the portfolio",
                "phenomenon": "concrete phenomenon or empirical regularity",
                "mechanism": "competing mechanism or boundary worth discriminating",
                "unit_of_analysis": "observable or simulated unit",
                "search_queries": "two or three distinct scholarly queries using field terminology",
            }],
        },
        "constraints": [
            "produce exactly seed_count seeds spanning at least four distinct scientific domains",
            "derive scientific directions before considering available software or experiment templates",
            "include at least two deliberately remote domains that do not usually appear together",
            "each query must contain at least two domain-specific content terms and no workflow boilerplate",
            "prefer unresolved mechanism, boundary, scaling, transition, or measurement questions over method demos",
            "do not mention capabilities, agents, papers to be written, release gates, or internal workflow state",
            "return only the complete JSON object",
        ],
    }, ensure_ascii=False, sort_keys=True)


SYSTEM = (
    "You are the intake research strategist for a general-purpose scientific organization. "
    "Use the supplied science-first frontier seeds and their scholarly records as prompts, then turn a broad objective into several "
    "genuinely different, testable research questions. "
    "Explore orthogonal directions before selecting: vary the mechanism, data regime, comparison, "
    "or measurement rather than producing near-duplicate variants. Preserve one high-risk/high-upside "
    "direction when it is still feasible, alongside safer directions, so the selector can compare novelty "
    "risk against evidence and execution cost. "
    "Do not claim novelty, truth, or empirical results before the literature and methods stages run. "
    "Scientific questions must originate in the frontier evidence, not in the wording of an executable template. "
    "Only after defining the phenomenon and discriminating test should you check whether it can be investigated "
    "with public sources and a bounded reproducible experiment using the declared runtime capabilities. "
    "Reject directions that require unavailable instruments, "
    "private cohorts, or unconfigured software. "
    "When runtime_context includes an experiment_catalog, every candidate must name one exact capability ID "
    "and align its phenomenon, comparison, data boundary, method, and primary outcomes with that capability. "
    "When runtime_context includes an experiment_contract, the selected candidate must be directly executable "
    "under that contract rather than silently proposing a different study. "
    "An executable question is only a starting point for a journal-oriented mission: give it a meaningful "
    "mechanism or boundary to discriminate, a comparison that can change the interpretation, and a result "
    "that could distinguish competing explanations. A single fixed parameter point or a two-method toy "
    "comparison must be treated as provisional unless it tests a nontrivial mechanism, a sensitivity frontier, "
    "or a theory-versus-observation discrepancy. "
    "When a refinement_context is supplied, preserve the useful parent idea but materially change at least "
    "one of mechanism, data regime, comparison, measurement, or theoretical target in response to the evidence. "
    "Keep scope explicit, include a way the idea could be disproved, and select one candidate only after "
    "comparing the alternatives. Use reader-facing scientific language; do not mention workflow state, "
    "artifacts, validators, hashes, acceptance, or internal control terms. Return JSON only. "
    "For capability_requirements, copy exact names from the supplied runtime inventory and leave a list empty "
    "when a requirement is unnecessary. If topic_exclusions are supplied, retain an excluded direction only as "
    "a rejected alternative and never select it."
)


def topic_prompt(objective, candidate_count, *, recent_papers=None, frontier_seeds=None,
                 runtime_context=None, refinement_context=None):
    def reader_projection(value):
        """Keep internal labels out of the model's reader-facing topic prose."""
        if isinstance(value, dict):
            return {key: reader_projection(item) for key, item in value.items()}
        if isinstance(value, list):
            return [reader_projection(item) for item in value]
        if isinstance(value, str):
            return project_internal_language(value)
        return value

    runtime_context = reader_projection(runtime_context or {})
    # Frozen fallback templates are retained for the independent challenge,
    # but a foundry-backed candidate generator must never see them as idea
    # seeds before it has defined the scientific question.
    runtime_context.pop("fallback_experiment_catalog", None)
    recent_papers = reader_projection(recent_papers or [])
    frontier_seeds = reader_projection(frontier_seeds or [])
    evidence_by_seed = {}
    for paper in recent_papers:
        if not isinstance(paper, dict):
            continue
        seed_id = paper.get("frontier_seed_id")
        if isinstance(seed_id, str) and seed_id:
            evidence_by_seed.setdefault(seed_id, []).append(paper)
    candidate_contract = {
        "id": "lowercase identifier such as direction_1; use only a-z, 0-9, _ or - with no spaces",
        "title": "short working title",
        "domain": "research domain",
        "research_question": "one testable question",
        "phenomenon": "the concrete phenomenon being measured",
        "mechanism": "mechanism or explanatory variable to discriminate",
        "data_regime": "data, boundary condition, or population regime",
        "comparison": "the comparison that could separate competing explanations",
        "measurement": "primary observable and how it is measured",
        "theory_target": "theory, scaling relation, or boundary under test",
        "scope": "population, system, data, or phenomenon boundary",
        "search_queries": "3 to 8 concrete literature search strings",
        "why_promising": "why this is worth investigating without claiming novelty",
        "disconfirmation_test": "what result or prior work would make this direction unhelpful",
        "disconfirmation_test_note": "optional detail about how the disconfirmation test separates explanations",
        "feasibility": "why the declared runtime can execute the study within the mission budget",
        "resource_plan": "data, programs, tools, and compute the study would use",
        "capability_requirements": {
            "executables": "exact names from runtime_context.executables",
            "python_packages": "exact names from runtime_context.python_packages",
            "stage_kinds": "exact names from runtime_context.configured_stage_kinds",
        },
    }
    constraints = [
        "use recent_papers as inspiration and retain their provided source identifiers in the candidate rationale when relevant",
        "anchor every candidate to a supplied frontier seed and its matching scholarly records",
        "define the scientific question before choosing an execution capability; never paraphrase a capability template as a topic",
        "candidate questions must differ in mechanism or empirical comparison, not just wording",
        "cover at least three distinct axes across the candidates when the objective and runtime permit: mechanism, data regime, comparison, measurement, or theory",
        "do not collapse every candidate onto the first familiar method merely because it is easiest to explain",
        "search queries must be usable as ordinary scholarly search strings",
        "capability_requirements must list only exact names from the supplied runtime inventory",
        "never invent a citation, dataset, result, or prior-work claim",
        "keep every narrative field concise (at most 45 words), keep each search query under 12 words, and fit the complete JSON package within 3000 output tokens",
        "include every required top-level key and every required candidate key; never stop after a partial candidate list",
        "return only the JSON object with no preface, commentary, markdown, or trailing explanation",
        "candidate prose is reader-facing: do not use the words frozen, validator, accepted artifact, model calls, release candidate, or SHA-256; say prespecified or independent recalculation where scientifically appropriate",
    ]
    if frontier_seeds and recent_papers:
        candidate_contract["frontier_seed_id"] = (
            "exact id copied from frontier_seeds; choose this before selecting evidence")
        candidate_contract["prior_work_ids"] = (
            "one to five work_id values copied from scholarly_records_by_frontier_seed under the "
            "candidate's exact frontier_seed_id")
        constraints.append(
            "every candidate must cite its exact frontier_seed_id and one to five supplied prior_work_ids; "
            "at least one cited work must belong to that same seed")
        constraints.append(
            "for each candidate, choose prior_work_ids only from scholarly_records_by_frontier_seed[frontier_seed_id]; "
            "never mix a work ID from another frontier seed")
    catalog = runtime_context.get("experiment_catalog") or []
    if catalog:
        candidate_contract["experiment_capability_id"] = (
            "exact id copied from runtime_context.experiment_catalog")
        constraints.append(
            "every candidate must copy one exact experiment_capability_id from runtime_context.experiment_catalog and remain executable under that capability")
        capability_ids = [item.get("id") for item in catalog
                          if isinstance(item, dict) and isinstance(item.get("id"), str)]
        if len(capability_ids) > 1:
            constraints.append(
                "cover every listed experiment capability at least once when candidate_count allows; "
                "do not put all candidates in the first or most familiar capability")
            constraints.append(
                "the candidate list is a portfolio: preserve distinct capabilities even when the recent-paper sample favors one domain")
        coverage_plan = runtime_context.get("candidate_capability_plan")
        if isinstance(coverage_plan, list) and coverage_plan:
            constraints.append(
                "assign candidate positions to the exact experiment_capability_id values in "
                "candidate_capability_plan; keep the scientific question distinct within each assignment")
        exclusions = runtime_context.get("topic_exclusions") or {}
        excluded_caps = exclusions.get("capability_ids", []) if isinstance(exclusions, dict) else []
        excluded_topics = exclusions.get("topic_ids", []) if isinstance(exclusions, dict) else []
        if excluded_caps or excluded_topics:
            constraints.append(
                "do not select any capability or topic listed in topic_exclusions; excluded directions may remain only as alternatives")
        if (runtime_context.get("topic_history") or {}).get("entries"):
            constraints.append(
                "avoid repeating any previously attempted direction in topic_history; select a materially different question or capability")
        design_driven = [item for item in catalog
                         if isinstance(item, dict) and item.get("design_driven")]
        if design_driven:
            ids = sorted(item.get("id") for item in design_driven if isinstance(item.get("id"), str))
            candidate_contract["experiment_design"] = (
                "REQUIRED for any candidate whose experiment_capability_id is one of "
                f"{ids}: a bounded declarative study design for the pinned design-driven engine "
                "(family, data_process, estimators, primary, baseline, seed, and optional "
                "trim_fraction/block_size). This is data the reviewed engine executes, never code.")
            for item in design_driven:
                template = item.get("design_template")
                if isinstance(template, dict):
                    candidate_contract[f"experiment_design_template[{item.get('id')}]"] = template
            constraints.extend([
                "for a design-driven capability, experiment_design must copy the family and data_process "
                "shape from the supplied template and choose declared estimators/primary/baseline that "
                "the capability actually supports",
                "experiment_design must remain executable under the frozen engine: no new estimator names, "
                "no new data-process kinds, and no field outside the declared schema",
                "include experiment_design only for a candidate using a design-driven capability; omit that key "
                "for every other capability",
            ])
    if refinement_context:
        constraints.extend([
            "this is a topic refinement pass, not a cosmetic rewrite: use the parent topic and the supplied survey feedback as constraints",
            "change at least one substantive dimension (mechanism, data_regime, comparison, measurement, or theory) and explain that change in why_promising",
            "do not select a direction that the supplied evidence already refutes; if the parent is refuted, pivot to a discriminating unresolved question",
            "treat the supplied survey evidence and source spans as the reason for the redesign; do not invent a gap that is absent from them",
        ])
    elif runtime_context.get("experiment_contract"):
        constraints.append(
            "the selected candidate must be directly executable under experiment_contract without changing the declared experiment")
    return json.dumps({
        "assignment": "free_topic_discovery",
        "principal_objective": objective,
        "candidate_count": candidate_count,
        "frontier_seeds": frontier_seeds,
        "recent_papers": recent_papers or [],
        "scholarly_records_by_frontier_seed": evidence_by_seed,
        "runtime_context": runtime_context,
        "refinement_context": refinement_context or {},
        "output_contract": {
            "schema_version": SCHEMA_VERSION,
            "objective": "copy principal_objective exactly",
            "candidates": "list of distinct candidate objects",
            "candidate": candidate_contract,
            "selected_id": "one candidate id",
            "selection_rationale": "compare evidence availability, testability, and disconfirmation risk",
        },
        "constraints": constraints,
        "output_constraints": [
            "Return exactly one JSON object with exactly the five top-level keys in output_contract.",
            "Each candidate may contain only the fields described by candidate and the explicitly required grounding or design fields.",
            "Do not echo assignment, constraints, runtime_context, frontier_seeds, or any other metadata.",
        ],
    }, ensure_ascii=False, sort_keys=True)


SOURCE_CHALLENGE_SYSTEM = (
    "You are an independent intake challenger. Decide only whether a selected question is relevantly grounded "
    "enough and sufficiently independent from any supplied executable templates to deserve a full literature "
    "survey. This is not a novelty verdict. Reject a question that merely restates a template, whose targeted "
    "search results are mostly irrelevant, or whose closest supplied work already answers the same comparison "
    "without a meaningful changed mechanism, boundary, or measurement. Cite only supplied work IDs. Return JSON only."
)


def _source_challenge_prompt(selected, works, runtime_context):
    catalog = ((runtime_context or {}).get("experiment_catalog")
               or (runtime_context or {}).get("fallback_experiment_catalog") or [])
    templates = [{key: item.get(key) for key in (
        "id", "domain", "research_question", "method", "primary_outcomes")}
        for item in catalog if isinstance(item, dict)]
    return json.dumps({
        "assignment": "topic_source_and_template_challenge",
        "selected_topic": selected,
        "targeted_scholarly_records": works,
        "executable_templates": templates,
        "output_contract": {
            "schema_version": SOURCE_CHALLENGE_SCHEMA_VERSION,
            "decision": "admit_to_survey or refine",
            "selected_id": "copy selected_topic.id exactly",
            "source_relevance": "integer 0 through 4",
            "template_independence": "integer 0 through 4; 0 means a template paraphrase",
            "prior_work_risk": "low, medium, or high",
            "closest_work_ids": "zero to eight IDs copied from targeted_scholarly_records",
            "rationale": "specific evidence-grounded rationale without a novelty claim",
            "required_changes": "empty when admitted; otherwise a unique JSON array of at most eight substantive scientific changes",
        },
        "admission_rule": {
            "minimum_source_relevance": 2,
            "minimum_template_independence": 3,
            "high_prior_work_risk_requires_refinement": True,
        },
        "output_constraints": [
            "Return exactly one JSON object with exactly the nine keys in output_contract.",
            "Do not echo assignment, selected_topic, targeted_scholarly_records, executable_templates, or any other metadata.",
        ],
    }, ensure_ascii=False, sort_keys=True)


def _capability_coverage_plan(runtime_context, candidate_count, seed):
    """Return a seeded portfolio plan for a catalog-backed intake.

    The model still invents the scientific question and comparison.  The plan
    only prevents a deterministic provider from collapsing every candidate
    onto the first executable template, which was the failure mode that made a
    supposedly free-topic mission repeat one old domain.
    """
    catalog = (runtime_context or {}).get("experiment_catalog") or []
    ids = [item.get("id") for item in catalog
           if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if len(ids) < 2:
        return []
    rng = Random(seed if type(seed) is int and seed >= 0 else 0)
    rng.shuffle(ids)
    return [ids[index % len(ids)] for index in range(candidate_count)]


MATURITY_SYSTEM = (
    "You are an independent scientific-program reviewer at the intake boundary. "
    "Assess whether the selected direction is developed enough to justify a serious literature survey "
    "and a journal-oriented experiment. Do not decide novelty or claim that a gap exists; the literature "
    "stage must establish those points. Score the selected question on five dimensions from 0 to 4: "
    "question specificity, mechanism or explanatory depth, comparison design, contribution potential, "
    "and falsifiability. An executable but single-point descriptive simulation should usually be refined "
    "unless it tests a nontrivial mechanism, a sensitivity frontier, or a theory-versus-observation discrepancy. "
    "Admit only when the question has a concrete phenomenon, a meaningful competing explanation or boundary, "
    "a result that would change the interpretation, and a credible disconfirmation route. If it is thin, "
    "name the smallest substantive changes needed and identify which dimensions must change. Copy the selected_id "
    "from the supplied topic package exactly; never invent or normalize a new identifier. Return JSON only."
)


def _maturity_review_prompt(objective, package, *, refinement_context=None):
    return json.dumps({
        "assignment": "topic_maturity_review",
        "principal_objective": objective,
        "topic_package": package,
        "selected_id_to_copy_exactly": package.get("selected_id"),
        "refinement_context": refinement_context or {},
        "dimensions": list(MATURITY_DIMENSIONS),
        "score_scale": "integer 0 through 4",
        "output_contract": {
            "decision": "admit or refine",
            "selected_id": "candidate id under review",
            "scores": {dimension: "integer 0 through 4" for dimension in MATURITY_DIMENSIONS},
            "rationale": "reader-facing scientific rationale",
            "required_changes": "empty for admit; one to eight substantive changes for refine",
            "changed_dimensions": f"unique subset of {list(REFINEMENT_DIMENSIONS)}",
        },
        "admission_rule": {
            "minimum_total": MATURITY_MIN_TOTAL,
            "minimum_each_dimension": MATURITY_MIN_DIMENSION,
            "do_not_reward_feasibility_alone": True,
        },
        "output_constraints": [
            "Return exactly one JSON object with exactly the six keys in output_contract.",
            "Do not echo assignment, dimensions, score_scale, admission_rule, topic_package, or any other metadata.",
        ],
        "identity_rule": "selected_id must equal topic_package.selected_id exactly",
    }, ensure_ascii=False, sort_keys=True)


class TopicDiscoveryRunner:
    """Generate one bounded, validated free-topic proposal."""

    def __init__(self, model, *, deadline_seconds=None):
        self.model_config = deepcopy(model)
        if (deadline_seconds is not None and
                (type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds)
                 or deadline_seconds <= 0)):
            raise ValidationError("topic discovery deadline must be finite and positive")
        self.deadline_seconds = float(deadline_seconds) if deadline_seconds is not None else None

    def _client(self, role, *, seed=None, deadline=None):
        config = resolve_model_config(
            self.model_config, role=role,
            overrides=({"seed": seed} if seed is not None else None),
        )
        # TopicBudget charges one logical dispatch.  Do not hide additional
        # provider requests inside ModelClient retries; bounded repair is
        # owned by this runner and is recorded as a separate event.
        config["max_retries"] = 0
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.2:
                raise ValidationError("topic discovery deadline exceeded")
            timeout_seconds = config.get("timeout_seconds")
            if (type(timeout_seconds) not in (int, float)
                    or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
                raise ValidationError(
                    "topic discovery model config requires a finite positive timeout_seconds")
            config["timeout_seconds"] = min(float(timeout_seconds), remaining)
        return ModelClient(**config)

    def _generate_frontier_seed_plan(self, objective, *, seed_count, sampling_seed,
                                     deadline, usage, budget=None, max_attempts=3):
        last_error = None
        previous = None
        for attempt in range(max_attempts):
            client = self._client(
                "research.frontier-seed-planner",
                seed=(sampling_seed + attempt) % MAX_PROVIDER_SEED,
                deadline=deadline,
            )
            payload = json.loads(_frontier_seed_prompt(objective, seed_count, sampling_seed))
            if previous is not None:
                payload["previous_response"] = previous[:30000]
                payload["validation_error"] = str(last_error)
                payload["repair_instruction"] = (
                    "Return a complete replacement seed plan. Preserve valid scientific seeds, remove mission "
                    "boilerplate, restore cross-domain diversity, and satisfy the exact output contract."
                )
            if budget is not None:
                budget.before_model_call(
                    "research.frontier-seed-planner", getattr(client, "model", None))
            try:
                result = client.complete(
                    system=FRONTIER_SYSTEM,
                    prompt=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                )
            except ModelCallError as exc:
                if budget is not None:
                    budget.record_model_error(exc)
                last_error = ValidationError(
                    f"frontier seed planner model call failed: {exc}")
                continue
            if budget is not None:
                budget.record_model_result(result)
            else:
                usage["model_calls"] += 1
                for key in ("input_tokens", "output_tokens"):
                    usage[key] += result.usage.get(key, 0)
            previous = result.text
            if result.finish_reason != "stop":
                last_error = ValidationError(
                    f"frontier seed planner did not finish normally: {result.finish_reason}")
                if budget is not None:
                    budget.record_validation_error(last_error)
                continue
            try:
                plan = result.json_object()
                return validate_frontier_seed_plan(plan, seed_count=seed_count)
            except ValidationError as exc:
                if budget is not None:
                    budget.record_validation_error(exc)
                last_error = exc
        error = last_error or ValidationError("frontier seed planner did not produce a valid plan")
        if budget is not None:
            setattr(error, "topic_budget", budget.snapshot())
        raise error

    def _challenge_selected_topic(self, selected, works, runtime_context, *, seed, deadline, usage,
                                  budget=None):
        reviewer = self._client("research.topic-source-challenger", seed=seed, deadline=deadline)
        previous = None
        last_error = None
        for repair_attempt in range(2):
            payload = json.loads(_source_challenge_prompt(selected, works, runtime_context))
            if previous is not None and last_error is not None:
                payload["assignment"] = "repair_topic_source_challenge"
                payload["previous_response"] = previous[:12000]
                payload["validation_error"] = str(last_error)
                payload["repair_instruction"] = (
                    "Return a complete replacement challenge object. Keep the same selected_id and supplied "
                    "work IDs, but repair the response against validation_error. required_changes must be a "
                    "unique JSON array of at most eight substantive strings."
                )
            prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if budget is not None:
                budget.before_model_call(
                    "research.topic-source-challenger", getattr(reviewer, "model", None))
            try:
                result = reviewer.complete(
                    system=SOURCE_CHALLENGE_SYSTEM,
                    prompt=prompt,
                )
            except ModelCallError as exc:
                if budget is not None:
                    budget.record_model_error(exc)
                raise ValidationError(f"topic source challenge model call failed: {exc}") from exc
            if budget is not None:
                budget.record_model_result(result)
            else:
                usage["model_calls"] += 1
                for key in ("input_tokens", "output_tokens"):
                    usage[key] += result.usage.get(key, 0)
            if result.finish_reason != "stop":
                last_error = ValidationError(
                    f"topic source challenge did not finish normally: {result.finish_reason}")
                if budget is not None:
                    budget.record_validation_error(last_error)
                previous = result.text
                continue
            try:
                review = result.json_object()
                # Duplicate references or repeated repair instructions carry
                # no scientific meaning.  Normalize those harmless formatting
                # slips before applying the strict challenge contract.
                for key in ("closest_work_ids", "required_changes"):
                    if isinstance(review.get(key), list) and all(
                            isinstance(item, str) for item in review[key]):
                        review[key] = list(dict.fromkeys(review[key]))
                validate_source_challenge(
                    review, selected_id=selected["id"],
                    work_ids=[item["work_id"] for item in works],
                )
                return review
            except ValidationError as exc:
                last_error = exc
                if budget is not None:
                    budget.record_validation_error(exc)
                previous = result.text
        raise last_error or ValidationError("topic source challenge did not produce a valid review")

    def run(self, objective, *, candidate_count=4, max_attempts=3,
            repair_mode="bounded", recent_papers=None, runtime_context=None,
            bibliography=None, sampling_seed=None, maturity_review_rounds=0,
            refinement_context=None, budgets=None):
        _text(objective, "topic objective", public=False)
        if type(candidate_count) is not int or not 3 <= candidate_count <= 8:
            raise ValidationError("topic discovery candidate_count must be between 3 and 8")
        if repair_mode not in {"bounded", "until_deadline"}:
            raise ValidationError("topic discovery repair_mode must be bounded or until_deadline")
        if repair_mode == "until_deadline" and self.deadline_seconds is None:
            raise ValidationError("topic discovery until_deadline mode requires a stage deadline")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 8:
            raise ValidationError("topic discovery max_attempts must be between 1 and 8")
        if type(maturity_review_rounds) is not int or not 0 <= maturity_review_rounds <= 4:
            raise ValidationError("topic discovery maturity_review_rounds must be between 0 and 4")
        if refinement_context is not None and not isinstance(refinement_context, dict):
            raise ValidationError("topic discovery refinement_context must be an object when supplied")
        deadline = time.monotonic() + self.deadline_seconds if self.deadline_seconds is not None else None
        if sampling_seed is None:
            sampling_seed = int(hashlib.sha256(objective.encode("utf-8")).hexdigest()[:16], 16) % MAX_PROVIDER_SEED
        if type(sampling_seed) is not int or not 0 <= sampling_seed <= MAX_PROVIDER_SEED:
            raise ValidationError(
                f"topic sampling_seed must be an integer between 0 and {MAX_PROVIDER_SEED}")
        usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
        budget = TopicBudget(budgets, usage)
        recent_papers = list(recent_papers or [])
        sampling_trace = []
        frontier_seed_plan = None
        if bibliography is not False and not recent_papers:
            frontier_seed_plan = self._generate_frontier_seed_plan(
                objective, seed_count=max(6, candidate_count), sampling_seed=sampling_seed,
                deadline=deadline, usage=usage, budget=budget)
            recent_papers, sampling_seed, sampling_trace = self._recent_paper_sample(
                objective, bibliography=bibliography, deadline=deadline, sampling_seed=sampling_seed,
                frontier_seed_plan=frontier_seed_plan, budget=budget)
        previous = None
        last_error = None
        refinement_feedback = None
        refinement_parent = None
        refinement_round = 0
        maturity_reviews = []
        maturity_review_history = []
        attempts = itertools.count() if repair_mode == "until_deadline" else range(max_attempts)
        catalog_ids = {
            item.get("id") for item in (runtime_context or {}).get("experiment_catalog", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        prompt_runtime_context = runtime_context
        coverage_plan = _capability_coverage_plan(
            runtime_context, candidate_count, sampling_seed)
        if coverage_plan:
            prompt_runtime_context = deepcopy(runtime_context or {})
            prompt_runtime_context["candidate_capability_plan"] = [
                {"candidate_index": index, "experiment_capability_id": capability_id}
                for index, capability_id in enumerate(coverage_plan)
            ]
        for attempt in attempts:
            generation_seed = (sampling_seed + attempt) % MAX_PROVIDER_SEED if sampling_seed is not None else None
            client = self._client("topic_discovery", seed=generation_seed, deadline=deadline)
            if refinement_feedback is not None:
                # Refinement must keep the same package contract as the first
                # generation.  A looser prompt here makes a capable model
                # return a convenient wrapper such as ``selected_candidate``
                # that cannot cross the validation boundary.
                refinement_payload = json.loads(topic_prompt(
                    objective, candidate_count,
                    recent_papers=recent_papers,
                    frontier_seeds=(frontier_seed_plan or {}).get("seeds", []),
                    runtime_context=prompt_runtime_context,
                    refinement_context={
                        **(refinement_context or {}),
                        "parent_topic": refinement_parent,
                        "refinement_feedback": refinement_feedback,
                    }))
                refinement_payload["assignment"] = "refine_topic_discovery"
                refinement_payload["refinement_instruction"] = (
                    "Return the complete package described by output_contract. Preserve useful evidence from "
                    "the parent, but make a substantive change in at least one of mechanism, data regime, "
                    "comparison, measurement, or theory. The selected direction must not be a cosmetic rewrite."
                )
                if previous is not None and last_error is not None:
                    refinement_payload["previous_response"] = previous[:40000]
                    refinement_payload["validation_error"] = str(last_error)
                    refinement_payload["refinement_instruction"] += (
                        " Repair the previous response against validation_error. In particular, include every "
                        "required experiment_design field for design-driven candidates and omit experiment_design "
                        "from all other capabilities."
                    )
                prompt = json.dumps(refinement_payload, ensure_ascii=False, sort_keys=True)
            else:
                prompt = topic_prompt(objective, candidate_count,
                                      recent_papers=recent_papers,
                                      frontier_seeds=(frontier_seed_plan or {}).get("seeds", []),
                                      runtime_context=prompt_runtime_context,
                                      refinement_context=refinement_context)
            if previous is not None and refinement_feedback is None:
                # Invalid-output repair also uses the full contract so repair
                # cannot drift into a different response shape.
                repair_payload = json.loads(topic_prompt(
                    objective, candidate_count,
                    recent_papers=recent_papers,
                    frontier_seeds=(frontier_seed_plan or {}).get("seeds", []),
                    runtime_context=prompt_runtime_context,
                    refinement_context=refinement_context))
                repair_payload["assignment"] = "repair_invalid_topic_discovery"
                repair_payload["candidate_response"] = previous[:40000]
                repair_payload["validation_error"] = str(last_error)
                repair_payload["repair_instruction"] = (
                    "Return a complete package satisfying output_contract. Preserve valid candidates and repair "
                    "only the reported violations; do not return a wrapper object or a partial candidate list."
                )
                prompt = json.dumps(repair_payload, ensure_ascii=False, sort_keys=True)
            budget.before_model_call("topic_discovery", getattr(client, "model", None))
            try:
                result = client.complete(system=SYSTEM, prompt=prompt)
            except ModelCallError as exc:
                budget.record_model_error(exc)
                last_error = ValidationError(f"topic discovery model call failed: {exc}")
                continue
            budget.record_model_result(result)
            previous = result.text
            if result.finish_reason != "stop":
                last_error = ValidationError(f"topic discovery did not finish normally: {result.finish_reason}")
                budget.record_validation_error(last_error)
                continue
            try:
                package = result.json_object()
                validate_topic_package(
                    package, objective=objective, candidate_count=candidate_count,
                    experiment_capability_ids=catalog_ids,
                    require_capability_coverage=bool(catalog_ids),
                    excluded_capability_ids=(runtime_context or {}).get("topic_exclusions", {}).get("capability_ids", []),
                    excluded_topic_ids=(runtime_context or {}).get("topic_exclusions", {}).get("topic_ids", []),
                    topic_history=(runtime_context or {}).get("topic_history"),
                    design_driven_capability_ids=_design_driven_ids(runtime_context),
                    frontier_seeds=(frontier_seed_plan or {}).get("seeds", []),
                    recent_papers=recent_papers,
                    require_grounding=frontier_seed_plan is not None,
                    fallback_templates=(runtime_context or {}).get(
                        "fallback_experiment_catalog", []))
                if runtime_context is not None:
                    feasibility = validate_topic_feasibility(package, runtime_context)
                    if feasibility["status"] == "legacy_unchecked":
                        raise ValidationError(
                            "current topic discovery output must include capability_requirements")
            except ValidationError as exc:
                budget.record_validation_error(exc)
                last_error = exc
                continue
            selected = next(item for item in package["candidates"] if item["id"] == package["selected_id"])
            feasibility = validate_topic_feasibility(package, runtime_context)
            candidate_prior_work = []
            source_challenge = None
            candidate_sampling_trace = []
            if bibliography is not False:
                try:
                    targeted = {
                        "schema_version": FRONTIER_SEED_SCHEMA_VERSION,
                        "seeds": [{
                            "id": "selected_direction",
                            "domain": selected["domain"],
                            "phenomenon": selected["title"],
                            "mechanism": selected["research_question"],
                            "unit_of_analysis": selected["scope"],
                            "search_queries": selected["search_queries"][:3],
                        }],
                    }
                    candidate_prior_work, _, candidate_sampling_trace = self._recent_paper_sample(
                        objective, bibliography=bibliography, deadline=deadline,
                        sampling_seed=(generation_seed + 32452843) % MAX_PROVIDER_SEED,
                        frontier_seed_plan=targeted, minimum_seed_groups=1, budget=budget)
                    source_challenge = self._challenge_selected_topic(
                        selected, candidate_prior_work, runtime_context,
                        seed=(generation_seed + 49979687) % MAX_PROVIDER_SEED,
                        deadline=deadline, usage=usage, budget=budget)
                except ProviderCooldownError:
                    raise
                except ValidationError as exc:
                    budget.record_validation_error(exc)
                    last_error = exc
                    continue
                if not _source_challenge_admitted(source_challenge):
                    last_error = ValidationError(
                        "topic source challenge requires substantive refinement: "
                        + source_challenge["rationale"])
                    budget.record_validation_error(last_error)
                    # The challenger is an independent gate, but its result
                    # is also the most useful repair specification.  Carry it
                    # into the next bounded generation so the model changes
                    # the rejected mechanism/boundary instead of restarting
                    # from an uninformed random proposal.
                    refinement_feedback = {
                        "review_type": "source_challenge",
                        **deepcopy(source_challenge),
                    }
                    refinement_parent = deepcopy(selected)
                    previous = None
                    continue
            if maturity_review_rounds:
                review_seed = ((generation_seed if generation_seed is not None else 0)
                               + 104729 * (attempt + 1)) % MAX_PROVIDER_SEED
                reviewer = self._client(
                    "research.topic-maturity-reviewer", seed=review_seed, deadline=deadline)
                budget.before_model_call(
                    "research.topic-maturity-reviewer", getattr(reviewer, "model", None))
                try:
                    review_result = reviewer.complete(
                        system=MATURITY_SYSTEM,
                        prompt=_maturity_review_prompt(
                            objective, package, refinement_context=refinement_context),
                    )
                except ModelCallError as exc:
                    budget.record_model_error(exc)
                    last_error = ValidationError(
                        f"topic maturity review model call failed: {exc}")
                    continue
                budget.record_model_result(review_result)
                if review_result.finish_reason != "stop":
                    last_error = ValidationError(
                        f"topic maturity review did not finish normally: {review_result.finish_reason}")
                    budget.record_validation_error(last_error)
                    continue
                try:
                    review = review_result.json_object()
                    validate_topic_maturity_review(
                        review, candidate_ids=[item["id"] for item in package["candidates"]])
                    if review["selected_id"] != package["selected_id"]:
                        raise ValidationError(
                            "topic maturity review must assess the package's selected_id")
                except ValidationError as exc:
                    budget.record_validation_error(exc)
                    last_error = exc
                    continue
                maturity_review_history.append({
                    "attempt": attempt,
                    "selected_id": package["selected_id"],
                    "topic_title": selected["title"],
                    "topic_research_question": selected["research_question"],
                    "review": deepcopy(review),
                })
                maturity_reviews.append(review)
                if topic_maturity_admitted(review):
                    evolution_dimensions = sorted({dimension
                                                   for item in maturity_reviews
                                                   for dimension in item.get("changed_dimensions", [])})
                    output = {
                        **package,
                        "status": "completed",
                        "topic": selected,
                        "question": selected["research_question"],
                        "search_queries": selected["search_queries"],
                        "proposed_gap": selected["why_promising"],
                        "feasibility_check": feasibility,
                        "recent_papers": recent_papers,
                        "frontier_seed_plan": frontier_seed_plan,
                        "candidate_prior_work": candidate_prior_work,
                        "candidate_sampling_trace": candidate_sampling_trace,
                        "source_challenge": source_challenge,
                        "sampling_seed": sampling_seed,
                        "generation_seed": generation_seed,
                            "sampling_trace": sampling_trace,
                            "maturity_reviews": deepcopy(maturity_reviews),
                            "maturity_review_history": deepcopy(maturity_review_history),
                            "maturity_score": sum(review["scores"].values()),
                        "usage": usage,
                        "budget": budget.snapshot(),
                    }
                    if refinement_context:
                        output["topic_evolution"] = {
                            "mode": "refinement",
                            "cycle": refinement_context.get("cycle"),
                            "parent_topic_id": refinement_context.get("parent_topic_id"),
                            "changed_dimensions": evolution_dimensions or refinement_context.get("changed_dimensions", []),
                            "reason": refinement_context.get("reason"),
                        }
                    return output
                if refinement_round >= maturity_review_rounds:
                    last_error = ValidationError(
                        "topic maturity review requires substantive refinement: "
                        + review["rationale"])
                    # A portfolio that remains thin after its allowed
                    # refinement passes is abandoned as a whole.  The next
                    # attempt starts a fresh exploration seed rather than
                    # polishing the same weak direction until the deadline.
                    refinement_feedback = None
                    refinement_parent = None
                    refinement_round = 0
                    maturity_reviews = []
                    previous = None
                    continue
                refinement_round += 1
                refinement_feedback = review
                refinement_parent = deepcopy(selected)
                previous = None
                continue
            output = {
                **package,
                "status": "completed",
                "topic": selected,
                "question": selected["research_question"],
                "search_queries": selected["search_queries"],
                "proposed_gap": selected["why_promising"],
                "feasibility_check": feasibility,
                "recent_papers": recent_papers,
                "frontier_seed_plan": frontier_seed_plan,
                "candidate_prior_work": candidate_prior_work,
                "candidate_sampling_trace": candidate_sampling_trace,
                "source_challenge": source_challenge,
                "sampling_seed": sampling_seed,
                "generation_seed": generation_seed,
                "sampling_trace": sampling_trace,
                "usage": usage,
                "budget": budget.snapshot(),
            }
            if refinement_context:
                output["topic_evolution"] = {
                    "mode": "refinement",
                    "cycle": refinement_context.get("cycle"),
                    "parent_topic_id": refinement_context.get("parent_topic_id"),
                    "changed_dimensions": refinement_context.get("changed_dimensions", []),
                    "reason": refinement_context.get("reason"),
                }
            return output
        error = last_error or ValidationError("topic discovery did not produce a valid package")
        snapshot = budget.snapshot()
        setattr(error, "topic_budget", snapshot)
        raise error

    @staticmethod
    def _recent_paper_sample(objective, *, bibliography=None, deadline=None, sampling_seed=None,
                             frontier_seed_plan=None, minimum_seed_groups=4, budget=None):
        """Search OpenAlex from science-first seeds and retain a balanced sample.

        Crossref is deliberately not a topic-source fallback: its metadata-only
        search cannot establish citation relationships and previously admitted
        unrelated records after an OpenAlex 429. A recent OpenAlex cache may be
        reused transparently; otherwise source coverage fails closed.
        """
        if deadline is not None and deadline - time.monotonic() <= 0.2:
            raise ValidationError("topic discovery deadline exceeded before literature sampling")
        if type(sampling_seed) is not int or not 0 <= sampling_seed <= MAX_PROVIDER_SEED:
            raise ValidationError(
                f"topic sampling_seed must be an integer between 0 and {MAX_PROVIDER_SEED}")
        seeds = frontier_seed_plan.get("seeds") if isinstance(frontier_seed_plan, dict) else None
        if not isinstance(seeds, list) or not seeds:
            raise ValidationError("topic literature sampling requires a science-first frontier seed plan")
        if type(minimum_seed_groups) is not int or not 1 <= minimum_seed_groups <= len(seeds):
            raise ValidationError("minimum_seed_groups must fit the frontier seed plan")

        bibliography = bibliography if isinstance(bibliography, dict) else {}
        client_config = {
            "timeout": 120, "max_bytes": 2_000_000,
            "endpoint": "https://api.openalex.org/works", "auth_env": None,
            "max_retries": 5, "retry_backoff_seconds": 1.0,
            "min_interval_seconds": 1.05,
        }
        client_config.update({key: bibliography[key] for key in TOPIC_BIBLIOGRAPHY_CLIENT_FIELDS
                              if key in bibliography})
        if bibliography.get("auth_env") is None:
            # Prefer an available OpenAlex key without forcing a secret into
            # the checked-in descriptor.  The explicit anonymous fallback is
            # still retained for hosts that intentionally have no key.
            for candidate in ("SCISAURUS_OPENALEX_API_KEY", "OPENALEX_API_KEY"):
                if os.environ.get(candidate):
                    client_config["auth_env"] = candidate
                    break
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.2:
                raise ValidationError("topic discovery deadline exceeded before OpenAlex setup")
            client_config["timeout"] = min(float(client_config["timeout"]), remaining)
        client = OpenAlexClient(**client_config)

        cache_path = Path(bibliography["cache_path"]) if bibliography.get("cache_path") else None
        cache_ttl = float(bibliography.get("cache_ttl_seconds", 7 * 24 * 3600))
        cache = {"schema_version": "topic-openalex-cache-1", "entries": {}}
        if cache_path is not None and cache_path.is_file():
            try:
                loaded = json.loads(cache_path.read_text())
                if (isinstance(loaded, dict) and loaded.get("schema_version") == cache["schema_version"]
                        and isinstance(loaded.get("entries"), dict)):
                    cache = loaded
            except (OSError, ValueError, TypeError):
                cache = {"schema_version": "topic-openalex-cache-1", "entries": {}}

        def persist_cache():
            if cache_path is None:
                return
            if len(cache["entries"]) > 512:
                newest = sorted(
                    cache["entries"].items(),
                    key=lambda item: float(item[1].get("stored_at", 0))
                    if isinstance(item[1], dict) else 0,
                    reverse=True)[:512]
                cache["entries"] = dict(newest)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(canonical_bytes(cache))
            os.replace(temporary, cache_path)

        # One query per independent frontier preserves domain balance. A
        # selected-candidate challenge has one seed and may use up to three
        # formulations to reduce wording sensitivity.
        query_specs = []
        for seed in seeds:
            queries = seed.get("search_queries") if isinstance(seed, dict) else None
            if not isinstance(queries, list) or not queries:
                raise ValidationError("frontier seed is missing scholarly search queries")
            selected_queries = queries[:3] if len(seeds) == 1 else queries[:1]
            for query in selected_queries:
                query_specs.append({"seed_id": seed.get("id"), "seed_domain": seed.get("domain"),
                                    "query": query})

        grouped, seen = {}, set()
        provider_errors, sampling_trace = [], []
        for spec in query_specs:
            if deadline is not None and deadline - time.monotonic() <= 0.2:
                raise ValidationError("topic discovery deadline exceeded during literature sampling")
            query = spec["query"]
            cache_key = hashlib.sha256(canonical_bytes({
                "endpoint": client_config["endpoint"], "query": query, "limit": 10,
            })).hexdigest()
            cached = cache["entries"].get(cache_key)
            cache_hit = bool(
                isinstance(cached, dict)
                and cached.get("query") == query
                and isinstance(cached.get("stored_at"), (int, float))
                and time.time() - float(cached["stored_at"]) <= cache_ttl
                and isinstance(cached.get("works"), list)
            )
            if cache_hit:
                result = {
                    "outcome": "ok" if cached["works"] else "empty",
                    "works": deepcopy(cached["works"]),
                    "source_url": cached.get("source_url"),
                    "capture_sha256": cached.get("capture_sha256"),
                    "metadata": {"provider": "openalex", "http_status": 200,
                                 "request": {"operation": "search", "query": query,
                                             "work_id": None, "limit": 10, "cursor": None}},
                }
            else:
                if budget is not None:
                    budget.before_openalex_request(query)
                try:
                    result = client.run(operation="search", query=query, limit=10, cursor=None)
                except (TypeError, ValueError) as exc:
                    raise ValidationError(f"invalid frontier scholarly query: {exc}") from exc
                if budget is not None:
                    budget.record_openalex_result(result)
                if result.get("outcome") in {"ok", "empty"}:
                    cache["entries"][cache_key] = {
                        "query": query, "stored_at": time.time(),
                        "works": deepcopy(result.get("works", [])),
                        "source_url": result.get("source_url"),
                        "capture_sha256": result.get("capture_sha256"),
                    }
                    persist_cache()
            if not isinstance(result, dict):
                provider_errors.append({"query": query, "outcome": "malformed_response"})
                continue
            outcome = result.get("outcome")
            works = result.get("works", []) if isinstance(result.get("works"), list) else []
            metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            relevant_ids, irrelevant_ids = [], []
            query_tokens = set(_topic_tokens(query)) - _MISSION_BOILERPLATE
            required_token_matches = min(2, len(query_tokens))
            relevance_matches = {}
            accepted = []
            for work in works:
                if not isinstance(work, dict) or not isinstance(work.get("id"), str):
                    continue
                title = work.get("title")
                if not isinstance(title, str) or not title.strip():
                    continue
                work_tokens = set(_topic_tokens(title + " " + str(work.get("abstract") or "")))
                matched_tokens = sorted(query_tokens.intersection(work_tokens))
                relevance_matches[work["id"]] = matched_tokens
                if len(matched_tokens) < required_token_matches:
                    irrelevant_ids.append(work["id"])
                    continue
                relevant_ids.append(work["id"])
                accepted.append(work)
            sampling_trace.append({
                "seed_id": spec["seed_id"], "seed_domain": spec["seed_domain"],
                "query": query, "outcome": outcome, "cache_hit": cache_hit,
                "returned_work_ids": [item.get("id") for item in works if isinstance(item, dict)],
                "relevant_work_ids": relevant_ids, "irrelevant_work_ids": irrelevant_ids,
                "required_query_token_matches": required_token_matches,
                "matched_query_tokens": relevance_matches,
                "source_url": result.get("source_url"),
                "capture_sha256": result.get("capture_sha256"),
                "provider": metadata.get("provider"), "http_status": metadata.get("http_status"),
                "request": metadata.get("request"), "rate_limit": metadata.get("rate_limit"),
                "attempts": metadata.get("attempts"),
                "retry_wait_seconds": metadata.get("retry_wait_seconds"),
                "pacing_wait_seconds": metadata.get("pacing_wait_seconds"),
            })
            if outcome not in {"ok", "empty"}:
                provider_errors.append({
                    "query": query, "outcome": outcome, "error": result.get("error"),
                    "rate_limit": metadata.get("rate_limit"),
                })
                # A rate limit applies to the provider, not just this wording.
                # Stop issuing fresh queries; cached coverage below may still
                # satisfy the explicit diversity floor.
                if outcome == "rate_limited":
                    break
                continue
            for work in accepted:
                if work["id"] in seen:
                    continue
                seen.add(work["id"])
                grouped.setdefault(spec["seed_id"], []).append({
                    "work_id": work["id"], "title": work["title"],
                    "year": work.get("year") if type(work.get("year")) is int else None,
                    "abstract": ((work.get("abstract") or "")[:TOPIC_ABSTRACT_CHARS] or None),
                    "doi": work.get("doi"), "locations": work.get("locations", []),
                    "frontier_seed_id": spec["seed_id"],
                    "frontier_domain": spec["seed_domain"], "matched_query": query,
                    **({"authors": work["authors"]} if isinstance(work.get("authors"), list) else {}),
                    "source_url": "https://openalex.org/" + work["id"],
                })

        populated_groups = [key for key, values in grouped.items() if values]
        if len(populated_groups) < minimum_seed_groups:
            detail = provider_errors[-1] if provider_errors else {"outcome": "insufficient_relevant_records"}
            limited = next((item for item in reversed(provider_errors)
                            if item.get("outcome") == "rate_limited"), None)
            delay = provider_cooldown_seconds(
                limited.get("rate_limit") if isinstance(limited, dict) else None)
            if delay is not None:
                error = ProviderCooldownError(
                    "OpenAlex topic sampling is paused until the provider quota resets "
                    f"({len(populated_groups)}/{minimum_seed_groups} frontier groups)",
                    retry_after_seconds=delay,
                    rate_limit=limited.get("rate_limit"),
                )
                if budget is not None:
                    setattr(error, "topic_budget", budget.snapshot())
                raise error
            error = ValidationError(
                "OpenAlex topic sampling did not meet the source-diversity floor "
                f"({len(populated_groups)}/{minimum_seed_groups} frontier groups): {detail}")
            if budget is not None:
                setattr(error, "topic_budget", budget.snapshot())
            raise error

        current_year = time.gmtime().tm_year
        recent_cutoff = current_year - RECENT_YEAR_WINDOW
        rng = Random(sampling_seed)
        for key, values in list(grouped.items()):
            recent = [item for item in values if isinstance(item.get("year"), int)
                      and item["year"] >= recent_cutoff]
            selected_pool = recent or sorted(
                values, key=lambda item: item.get("year") if type(item.get("year")) is int else 0,
                reverse=True)
            rng.shuffle(selected_pool)
            grouped[key] = selected_pool

        # Round-robin across seeds so one broad query cannot dominate the
        # model context merely because the provider ranked it first.
        pool = []
        ordered_groups = list(populated_groups)
        rng.shuffle(ordered_groups)
        while len(pool) < TOPIC_SAMPLE_LIMIT:
            advanced = False
            for key in ordered_groups:
                values = grouped.get(key, [])
                if values:
                    pool.append(values.pop(0))
                    advanced = True
                    if len(pool) >= TOPIC_SAMPLE_LIMIT:
                        break
            if not advanced:
                break
        return pool, sampling_seed, sampling_trace


__all__ = [
    "SCHEMA_VERSION", "STAGE_CONFIG_SCHEMA_VERSION", "TOPIC_HISTORY_SCHEMA_VERSION",
    "RECENT_YEAR_WINDOW", "TopicDiscoveryRunner", "topic_signature", "validate_topic_novelty",
    "validate_frontier_seed_plan", "validate_source_challenge", "validate_topic_stage_config",
    "validate_topic_package", "validate_topic_feasibility", "topic_prompt",
]
