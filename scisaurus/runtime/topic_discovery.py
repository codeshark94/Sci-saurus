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
    MAX_PROVIDER_SEED, ModelCallError, ModelClient, estimate_input_tokens,
    model_call_budget_available, resolve_model_config,
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
# A bounded intake may need several source-challenge and maturity repairs
# before a candidate is eligible.  This is an admission ceiling, not a
# permission to run until the mission deadline; the stage budget remains the
# hard limit on model calls, tokens, and provider requests.
MAX_BOUNDED_TOPIC_ATTEMPTS = 12
FRONTIER_SEED_SCHEMA_VERSION = "topic-frontier-seeds-1"
SOURCE_CHALLENGE_SCHEMA_VERSION = "topic-source-challenge-2"
FRONTIER_SEED_FIELDS = {
    "id", "domain", "phenomenon", "mechanism", "unit_of_analysis", "search_queries",
}
SOURCE_CHALLENGE_FIELDS = {
    "schema_version", "decision", "selected_id", "source_relevance",
    "template_independence", "prior_work_risk", "closest_work_ids", "rationale",
    "required_changes",
}
SOURCE_CHALLENGE_OPTIONAL_FIELDS = {
    "direct_comparison_match", "risk_calibration",
}
SOURCE_CHALLENGE_LEGACY_SCHEMA_VERSIONS = {"topic-source-challenge-1"}
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
    "phenomenon", "disconfirmation_test_note", "research_form", "evidence_mode",
    "comparison_type",
}
GROUNDING_FIELDS = {"frontier_seed_id", "prior_work_ids"}
LEGACY_CANDIDATE_FIELDS = CANDIDATE_FIELDS - {"capability_requirements"}
CATALOG_CANDIDATE_FIELDS = CANDIDATE_FIELDS | {"experiment_capability_id"}
CATALOG_LEGACY_CANDIDATE_FIELDS = LEGACY_CANDIDATE_FIELDS | {"experiment_capability_id"}
CAPABILITY_FIELDS = {"executables", "python_packages", "stage_kinds"}
KNOWN_STAGE_KINDS = {"topic_discovery", "survey", "experiment", "interpretation", "argument", "paper"}

# These fields describe the epistemic shape of a candidate, not its subject
# matter.  A diverse frontier seed list is insufficient when every candidate
# is still a two-model simulation.  Keeping the vocabulary bounded makes the
# portfolio contract machine-checkable and gives retries a durable negative
# memory without pretending that lexical novelty proves scientific novelty.
RESEARCH_FORM_VALUES = (
    "theory_simulation", "observational_reanalysis", "experimental_design",
    "methodological_benchmark", "replication_null_test", "scaling_boundary",
)
EVIDENCE_MODE_VALUES = (
    "analytical_derivation", "synthetic_simulation", "published_observations",
    "public_dataset", "cross_source_synthesis", "controlled_measurement",
)
COMPARISON_TYPE_VALUES = (
    "mechanism_ablation", "model_selection", "cross_method", "scaling_transition",
    "replication", "null_test", "causal_contrast", "measurement_design",
)
PORTFOLIO_DIMENSIONS = ("research_form", "evidence_mode", "comparison_type")
REFINEMENT_COMPARE_FIELDS = (
    "research_question", "domain", "scope", "phenomenon", "mechanism", "data_regime",
    "comparison", "measurement", "theory_target", "research_form", "evidence_mode",
    "comparison_type", "experiment_capability_id", "frontier_seed_id",
)

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
    "research_form", "evidence_mode", "comparison_type",
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


def _materialize_foundry_capability_requirements(package, runtime_context):
    """Add only the deterministic baseline for a foundry-backed selection.

    Capability requirements describe the execution boundary, not the scientific
    idea.  Asking a model to repeat that same inventory for every portfolio
    candidate wastes the bounded response budget and made otherwise valid topic
    packages fail when the selected candidate was the last object emitted.  A
    model-supplied requirement remains authoritative and is still validated;
    this helper only fills an omitted selected-candidate baseline from the live
    runtime inventory.
    """
    if not isinstance(package, dict) or not isinstance(runtime_context, dict):
        return package
    foundry = runtime_context.get("capability_foundry")
    if not isinstance(foundry, dict) or foundry.get("enabled") is not True:
        return package
    selected_id = package.get("selected_id")
    candidates = package.get("candidates")
    if not isinstance(selected_id, str) or not isinstance(candidates, list):
        return package
    selected = next((item for item in candidates
                     if isinstance(item, dict) and item.get("id") == selected_id), None)
    if selected is None or "capability_requirements" in selected:
        return package
    executables = runtime_context.get("executables")
    configured_stages = runtime_context.get("configured_stage_kinds")
    # The foundry's execution boundary is seeded Python.  Do not claim any
    # optional package merely because it happens to be installed on the host.
    selected["capability_requirements"] = {
        "executables": (["python3"] if isinstance(executables, dict)
                         and executables.get("python3") is True else []),
        "python_packages": [],
        "stage_kinds": (["experiment"] if isinstance(configured_stages, list)
                         and "experiment" in configured_stages else []),
    }
    return package


_MODEL_CANDIDATE_FIELD_ALIASES = {
    # Some providers turn the schema's prose marker into a field name. This
    # is a lossless compatibility repair because the target field is the
    # declared candidate field and the alias is never a second datum.
    "disconfirmation_test_note_optional": "disconfirmation_test_note",
}


def _repair_known_candidate_field_aliases(package):
    """Canonicalize only explicit, lossless aliases from model JSON."""
    if not isinstance(package, dict) or not isinstance(package.get("candidates"), list):
        return []
    repairs = []
    for candidate in package["candidates"]:
        if not isinstance(candidate, dict):
            continue
        for alias, target in _MODEL_CANDIDATE_FIELD_ALIASES.items():
            if alias not in candidate or target in candidate:
                continue
            candidate[target] = candidate.pop(alias)
            repairs.append({
                "candidate_id": candidate.get("id"),
                "from": alias,
                "to": target,
                "source": "known_model_field_alias",
            })
    return repairs


def _materialize_foundry_feasibility(package, runtime_context):
    """Fill an omitted operational feasibility note from the foundry boundary.

    ``feasibility`` is required on every candidate, but it is not evidence of
    a result or of novelty.  In a foundry-backed mission its non-substantive
    baseline is determined by the supplied local runtime; filling only an
    omitted note prevents a provider from losing a valid candidate at the end
    of a long JSON response.  Scientific claims and resource requirements
    remain model-supplied and are still validated separately.
    """
    if not isinstance(package, dict) or not isinstance(runtime_context, dict):
        return []
    foundry = runtime_context.get("capability_foundry")
    if not isinstance(foundry, dict) or foundry.get("enabled") is not True:
        return []
    candidates = package.get("candidates")
    if not isinstance(candidates, list):
        return []
    repairs = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or "feasibility" in candidate:
            continue
        candidate["feasibility"] = (
            "The supplied local runtime provides the bounded experiment baseline; "
            "any additional resource must be declared explicitly."
        )
        repairs.append({
            "candidate_id": candidate.get("id"),
            "field": "feasibility",
            "source": "foundry_runtime_boundary",
        })
    return repairs


def _materialize_topic_objective(package, objective):
    """Restore the immutable mission objective on a parsed model package."""
    if (isinstance(package, dict) and isinstance(package.get("objective"), str)
            and isinstance(objective, str)):
        package["objective"] = objective
    return package


def _materialize_seed_domains(package, frontier_seeds):
    """Fill an omitted candidate domain from its declared frontier seed.

    The domain is already fixed by the candidate's required
    ``frontier_seed_id``. Deriving only this redundant field repairs a common
    structured-output omission without guessing a scientific attribute or
    accepting an ungrounded candidate. Explicit, nonempty model values remain
    untouched and every repair is returned for the attempt audit trail.
    """
    if not isinstance(package, dict) or not isinstance(package.get("candidates"), list):
        return []
    seeds = {
        item.get("id"): item for item in (frontier_seeds or [])
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("domain"), str)
        and item.get("domain").strip()
    }
    repairs = []
    for candidate in package["candidates"]:
        if not isinstance(candidate, dict) or "domain" in candidate:
            continue
        seed_id = candidate.get("frontier_seed_id")
        seed = seeds.get(seed_id)
        if seed is None:
            continue
        candidate["domain"] = seed["domain"]
        repairs.append({
            "candidate_id": candidate.get("id"),
            "field": "domain",
            "source": "frontier_seed_id",
            "frontier_seed_id": seed_id,
        })
    return repairs


def _materialize_seed_bindings(package, frontier_seeds, recent_papers, *,
                               rejected_frontier_seed_ids=None):
    """Recover only unambiguous seed/evidence links from supplied records.

    Refinement responses occasionally omit the two redundant grounding fields
    while preserving the candidate's scientific prose.  An exact domain match
    to a unique frontier seed and a positive token overlap with records from
    that seed are sufficient to restore the link without inventing a citation.
    Ambiguous links are left for the bounded model repair path or rejected by
    the normal package validator.
    """
    if not isinstance(package, dict) or not isinstance(package.get("candidates"), list):
        return []
    seeds = [
        item for item in (frontier_seeds or [])
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("domain"), str)
    ]
    seed_by_id = {item["id"]: item for item in seeds}
    rejected = {
        value for value in (rejected_frontier_seed_ids or [])
        if isinstance(value, str) and value.strip()
    }
    records_by_seed = {}
    record_by_id = {}
    for record in recent_papers or []:
        if not isinstance(record, dict):
            continue
        work_id = record.get("work_id")
        seed_id = record.get("frontier_seed_id")
        if not isinstance(work_id, str) or not isinstance(seed_id, str):
            continue
        record_by_id[work_id] = record
        records_by_seed.setdefault(seed_id, []).append(record)

    def normalized(value):
        return " ".join(value.casefold().split()) if isinstance(value, str) else ""

    repairs = []
    for candidate in package["candidates"]:
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("id")
        seed_id = candidate.get("frontier_seed_id")
        if not isinstance(seed_id, str) or not seed_id.strip():
            # A model-supplied work link is stronger than a free-form domain
            # label, so use it first when all cited records share one seed.
            cited = candidate.get("prior_work_ids")
            cited_records = [record_by_id[item] for item in (cited if isinstance(cited, list) else [])
                             if isinstance(item, str) and item in record_by_id]
            cited_seed_ids = {
                record.get("frontier_seed_id") for record in cited_records
                if isinstance(record.get("frontier_seed_id"), str)
            }
            if len(cited_seed_ids) == 1:
                only_seed = next(iter(cited_seed_ids))
                if only_seed in seed_by_id and only_seed not in rejected:
                    candidate["frontier_seed_id"] = only_seed
                    seed_id = only_seed
                    repairs.append({
                        "candidate_id": candidate_id,
                        "field": "frontier_seed_id",
                        "source": "supplied_prior_work_seed",
                    })
        if not isinstance(seed_id, str) or seed_id not in seed_by_id or seed_id in rejected:
            domain = normalized(candidate.get("domain"))
            domain_matches = [
                seed for seed in seeds
                if seed["id"] not in rejected
                and normalized(seed.get("domain")) == domain
            ] if domain else []
            if len(domain_matches) == 1:
                candidate["frontier_seed_id"] = domain_matches[0]["id"]
                seed_id = domain_matches[0]["id"]
                repairs.append({
                    "candidate_id": candidate_id,
                    "field": "frontier_seed_id",
                    "source": "exact_frontier_domain",
                })
        if not isinstance(seed_id, str) or seed_id not in seed_by_id or seed_id in rejected:
            continue
        prior_work_ids = candidate.get("prior_work_ids")
        if isinstance(prior_work_ids, list) and prior_work_ids:
            continue
        candidate_tokens = set().union(*(
            _topic_tokens(candidate.get(field, ""))
            for field in (
                "title", "domain", "research_question", "phenomenon", "mechanism",
                "data_regime", "comparison", "measurement", "theory_target", "scope",
            )
        )) - _MISSION_BOILERPLATE
        scored = []
        for record in records_by_seed.get(seed_id, []):
            record_tokens = _topic_tokens(
                " ".join(str(record.get(field, "") or "")
                          for field in ("title", "abstract"))
            ) - _MISSION_BOILERPLATE
            overlap = len(candidate_tokens.intersection(record_tokens))
            if overlap >= 2:
                scored.append((-overlap, record.get("work_id"), record))
        if not scored:
            continue
        scored.sort(key=lambda item: (item[0], item[1] or ""))
        candidate["prior_work_ids"] = [item[1] for item in scored[:3]]
        repairs.append({
            "candidate_id": candidate_id,
            "field": "prior_work_ids",
            "source": "seed_record_token_overlap",
            "work_ids": candidate["prior_work_ids"],
        })
    return repairs


_TOPIC_REPAIRABLE_TEXT_FIELDS = (
    "title", "domain", "research_question", "scope", "why_promising",
    "disconfirmation_test", "feasibility", "resource_plan",
)
_TOPIC_REPAIR_CONTEXT_FIELDS = (
    "id", "title", "domain", "research_question", "phenomenon", "mechanism",
    "data_regime", "comparison", "measurement", "theory_target", "scope",
    "research_form", "evidence_mode", "comparison_type", "disconfirmation_test_note",
    "feasibility", "resource_plan", "frontier_seed_id", "prior_work_ids",
    "experiment_capability_id",
)


def _topic_missing_field_repair_prompt(package, targets):
    """Build a field-only repair request for an otherwise intact package.

    The model has already supplied the scientific direction.  A repair turn
    therefore receives only the affected candidate fields and may fill only
    the explicitly missing required text fields.  This prevents a formatting
    omission such as a missing title from causing a new portfolio, source
    selection, or research-shape decision.
    """
    context = []
    for index, candidate in enumerate(targets):
        context.append({
            "candidate_index": candidate["candidate_index"],
            "id": candidate["id"],
            "missing_fields": candidate["missing_fields"],
            "candidate_fields": {
                key: candidate["candidate"].get(key)
                for key in _TOPIC_REPAIR_CONTEXT_FIELDS
                if key in candidate["candidate"]
            },
        })
    return json.dumps({
        "assignment": "repair_missing_topic_fields",
        "candidate_context": context,
        "output_contract": {
            "candidate_patches": [{
                "id": "copy the exact candidate id",
                "fields": "object containing exactly the listed missing_fields and one concise text value per field",
            }],
        },
        "constraints": [
            "return exactly one patch for every candidate_context item and no other candidate",
            "copy each id exactly; do not rename, reorder, or omit a candidate",
            "fields must contain exactly the missing_fields listed for that candidate; do not return any existing field",
            "derive every repaired value only from the supplied candidate_fields; do not introduce a new domain, mechanism, dataset, result, citation, or claim of novelty",
            "keep repaired prose concise and consistent with the candidate's research form, evidence mode, comparison type, and source grounding",
            "return only the JSON object; do not echo candidate_context or add metadata",
        ],
    }, ensure_ascii=False, sort_keys=True)


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

    def before_model_call(self, role=None, model=None, *, system=None, prompt=None):
        self._check_available("max_model_calls", "model_calls")
        estimated_input_tokens = None
        if system is not None or prompt is not None:
            if not isinstance(system, str) or not isinstance(prompt, str):
                raise ValidationError(
                    "topic model quota preflight requires system and prompt strings")
            estimated_input_tokens = estimate_input_tokens(system, prompt)
            limit = self.limits.get("max_input_tokens")
            observed = self.usage.get("input_tokens", 0)
            if limit is not None and observed + estimated_input_tokens > limit:
                self.events.append({
                    "sequence": len(self.events) + 1,
                    "kind": "quota",
                    "status": "blocked",
                    "dimension": "input_tokens",
                    "role": role,
                    "model": model,
                    "estimated_input_tokens": estimated_input_tokens,
                    "observed_input_tokens": observed,
                    "projected_input_tokens": observed + estimated_input_tokens,
                    "limit": limit,
                })
                self._raise("input_tokens", limit, observed + estimated_input_tokens)
        # Count the dispatch before the provider call.  A transport timeout or
        # other model error still consumed an external call and must not be
        # invisible to the intake quota.
        self.usage["model_calls"] = self.usage.get("model_calls", 0) + 1
        event = {
            "sequence": len(self.events) + 1, "kind": "model",
            "role": role, "model": model, "status": "dispatched",
            "model_call_number": self.usage["model_calls"],
        }
        if estimated_input_tokens is not None:
            event["estimated_input_tokens"] = estimated_input_tokens
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
            self._active_event = None
        for key in ("input_tokens", "output_tokens"):
            value = result.usage.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                value = 0
            self.usage[key] = self.usage.get(key, 0) + value
            limit = self.limits.get(f"max_{key}")
            if limit is not None and self.usage[key] > limit:
                self._raise(key, limit, self.usage[key])

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
                authenticated=metadata.get("authenticated"),
                http_status=metadata.get("http_status"),
                retry_wait_seconds=metadata.get("retry_wait_seconds"),
                pacing_wait_seconds=metadata.get("pacing_wait_seconds"),
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
    allowed = fields | {
        "repair_mode", "maturity_review_rounds", "bibliography", "budgets",
        "continuation_budgets",
    }
    if (not isinstance(value, dict) or set(value) - allowed
            or not fields.issubset(value)):
        raise ValidationError(
            f"topic discovery config requires {sorted(fields)} and permits repair_mode, bibliography, budgets, continuation_budgets")
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
    if (type(value["max_attempts"]) is not int
            or not 1 <= value["max_attempts"] <= MAX_BOUNDED_TOPIC_ATTEMPTS):
        raise ValidationError(
            "topic discovery max_attempts must be between 1 and "
            f"{MAX_BOUNDED_TOPIC_ATTEMPTS}")
    repair_mode = value.get("repair_mode", "bounded")
    if repair_mode not in {"bounded", "until_deadline"}:
        raise ValidationError("topic discovery repair_mode must be bounded or until_deadline")
    maturity_rounds = value.get("maturity_review_rounds", 0)
    if type(maturity_rounds) is not int or not 0 <= maturity_rounds <= 4:
        raise ValidationError("topic discovery maturity_review_rounds must be between 0 and 4")
    if "budgets" in value:
        _validate_topic_budgets(value["budgets"])
    if "continuation_budgets" in value:
        _validate_topic_budgets(value["continuation_budgets"])
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


def _anchor_frontier_seed_queries(value):
    """Repair a query that lost its seed anchor without inventing a direction."""
    if not isinstance(value, dict) or not isinstance(value.get("seeds"), list):
        return value
    for seed in value["seeds"]:
        if not isinstance(seed, dict) or not isinstance(seed.get("search_queries"), list):
            continue
        seed_terms = set().union(*(
            _topic_tokens(seed.get(key, ""))
            for key in ("domain", "phenomenon", "mechanism", "unit_of_analysis")
        )) - _MISSION_BOILERPLATE
        if not seed_terms:
            continue
        anchored = []
        for query in seed["search_queries"]:
            if isinstance(query, str):
                query_terms = set(_topic_tokens(query)) - _MISSION_BOILERPLATE
                if not query_terms.intersection(seed_terms):
                    # Keep the model's terminology, but append exact seed
                    # anchors so the provider search cannot drift into another
                    # domain. Validation still rejects malformed/non-text rows.
                    query = " ".join((query, seed["domain"], seed["phenomenon"])).strip()
            anchored.append(query)
        seed["search_queries"] = anchored
    return value


def _anchor_topic_candidate_queries(value, *, frontier_seeds=None):
    """Repair drifted candidate queries using candidate and seed vocabulary."""
    if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
        return 0
    seeds_by_id = {
        item.get("id"): item for item in (frontier_seeds or [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    repaired_count = 0
    for candidate in value["candidates"]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("search_queries"), list):
            continue
        anchor_terms = []
        seen = set()
        for field in ("domain", "title", "research_question", "scope", "mechanism", "measurement"):
            text = candidate.get(field)
            if not isinstance(text, str):
                continue
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9]*", text.casefold()):
                if token in _TOPIC_STOPWORDS or token in _MISSION_BOILERPLATE or len(token) <= 2:
                    continue
                if token not in seen:
                    seen.add(token)
                    anchor_terms.append(token)
        seed = seeds_by_id.get(candidate.get("frontier_seed_id"))
        if isinstance(seed, dict):
            for field in ("domain", "phenomenon", "mechanism", "unit_of_analysis"):
                text = seed.get(field)
                if not isinstance(text, str):
                    continue
                for token in re.findall(r"[a-zA-Z][a-zA-Z0-9]*", text.casefold()):
                    if token in _TOPIC_STOPWORDS or token in _MISSION_BOILERPLATE or len(token) <= 2:
                        continue
                    if token not in seen:
                        seen.add(token)
                        anchor_terms.append(token)
        if not anchor_terms:
            continue
        repaired = []
        for query in candidate["search_queries"]:
            if not isinstance(query, str):
                repaired.append(query)
                continue
            query_terms = set(_topic_tokens(query)) - _MISSION_BOILERPLATE
            if len(query_terms) < 2 or not query_terms.intersection(seen):
                additions = [term for term in anchor_terms if term not in query_terms][:2]
                if len(additions) < 2:
                    additions = [term for term in anchor_terms if term not in query_terms]
                query = " ".join((query, *additions)).strip()
                repaired_count += 1
            repaired.append(query)
        candidate["search_queries"] = repaired
    return repaired_count


def validate_source_challenge(value, *, selected_id, work_ids):
    """Validate the pre-survey relevance and template-independence challenge."""
    allowed_fields = SOURCE_CHALLENGE_FIELDS | SOURCE_CHALLENGE_OPTIONAL_FIELDS
    if (not isinstance(value, dict)
            or not SOURCE_CHALLENGE_FIELDS.issubset(value)
            or set(value) - allowed_fields):
        raise ValidationError(
            f"topic source challenge requires exactly {sorted(SOURCE_CHALLENGE_FIELDS)}")
    if value["schema_version"] not in (
            {SOURCE_CHALLENGE_SCHEMA_VERSION} | SOURCE_CHALLENGE_LEGACY_SCHEMA_VERSIONS):
        raise ValidationError("topic source challenge schema version is unsupported")
    if (value["schema_version"] == SOURCE_CHALLENGE_SCHEMA_VERSION
            and "direct_comparison_match" not in value):
        raise ValidationError(
            "current topic source challenge must report direct_comparison_match")
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
    if ("direct_comparison_match" in value
            and type(value["direct_comparison_match"]) is not bool):
        raise ValidationError("topic source challenge direct_comparison_match must be boolean")
    if ("risk_calibration" in value
            and (not isinstance(value["risk_calibration"], str)
                 or not value["risk_calibration"].strip())):
        raise ValidationError("topic source challenge risk_calibration must be a nonempty string")
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


def _source_challenge_requires_frontier_seed_pivot(review):
    """Return whether weak or template-near high-risk evidence needs a new seed.

    A high-risk direction with strong targeted relevance and a distinct
    execution shape may still be repaired by a genuinely different mechanism
    or measurement. Weak source relevance or template-near structure means
    that keeping the same frontier seed would only reward cosmetic rewrites
    and repeatedly spend provider quota on the same unsupported direction.
    """
    return (
        isinstance(review, dict)
        and review.get("prior_work_risk") == "high"
        and (
            type(review.get("source_relevance")) is int
            and review["source_relevance"] <= 2
            or type(review.get("template_independence")) is int
            and review["template_independence"] <= 1
        )
    )


def topic_portfolio_profile(candidates):
    """Summarize the research forms represented by one candidate portfolio."""
    if not isinstance(candidates, list) or not candidates:
        raise ValidationError("topic candidate portfolio must be a nonempty list")
    allowed_values = {
        "research_form": RESEARCH_FORM_VALUES,
        "evidence_mode": EVIDENCE_MODE_VALUES,
        "comparison_type": COMPARISON_TYPE_VALUES,
    }
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValidationError("topic candidate portfolio entries must be objects")
        for field, values_for_field in allowed_values.items():
            if candidate.get(field) not in values_for_field:
                raise ValidationError(
                    f"topic candidate {field} must be one of {list(values_for_field)}")
    values = {
        field: [candidate.get(field) for candidate in candidates]
        for field in PORTFOLIO_DIMENSIONS
    }
    counts = {
        field: {
            value: values[field].count(value)
            for value in sorted(set(values[field]))
        }
        for field in PORTFOLIO_DIMENSIONS
    }
    archetypes = [
        {field: candidate.get(field) for field in PORTFOLIO_DIMENSIONS}
        for candidate in candidates
    ]
    return {
        "candidate_count": len(candidates),
        "distinct": {
            field: len(set(values[field])) for field in PORTFOLIO_DIMENSIONS
        },
        "counts": counts,
        "archetypes": archetypes,
    }


def validate_topic_portfolio(candidates, *, minimum_research_forms=None,
                             minimum_evidence_modes=None,
                             minimum_comparison_types=None):
    """Reject a candidate set that collapses onto one research archetype.

    This is a portfolio-shape gate, not a novelty claim.  It prevents the
    intake model from presenting six differently worded versions of the same
    computational comparison while leaving the later literature stage to
    judge whether any admitted direction is actually new.
    """
    if not isinstance(candidates, list) or not candidates:
        raise ValidationError("topic candidate portfolio must be a nonempty list")
    count = len(candidates)
    minimums = {
        "research_form": min(4, count) if minimum_research_forms is None
        else minimum_research_forms,
        "evidence_mode": min(3, count) if minimum_evidence_modes is None
        else minimum_evidence_modes,
        "comparison_type": min(3, count) if minimum_comparison_types is None
        else minimum_comparison_types,
    }
    for field, minimum in minimums.items():
        if type(minimum) is not int or not 1 <= minimum <= count:
            raise ValidationError(f"minimum {field} diversity is invalid")
    profile = topic_portfolio_profile(candidates)
    for field, minimum in minimums.items():
        observed = profile["distinct"][field]
        if observed < minimum:
            raise ValidationError(
                f"topic candidate portfolio must span at least {minimum} distinct {field} values"
            )

    # Four forms across six candidates should produce a genuine portfolio,
    # not four labels attached to a dominant familiar form.  The bound scales
    # conservatively for smaller or larger configured portfolios.
    maximum_per_form = max(2, math.ceil(count / 4))
    if max(profile["counts"]["research_form"].values()) > maximum_per_form:
        raise ValidationError(
            "topic candidate portfolio collapses onto one research_form"
        )

    archetypes = [tuple(item[field] for field in PORTFOLIO_DIMENSIONS)
                  for item in profile["archetypes"]]
    if len(archetypes) != len(set(archetypes)):
        raise ValidationError(
            "topic candidate portfolio repeats the same research archetype"
        )
    return profile


def _refinement_value(value):
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    return value


def topic_refinement_dimensions(parent, candidate):
    """Return substantive fields that changed between two topic candidates."""
    if not isinstance(parent, dict) or not isinstance(candidate, dict):
        raise ValidationError("topic refinement comparison requires two candidate objects")
    return [
        field for field in REFINEMENT_COMPARE_FIELDS
        if _refinement_value(parent.get(field)) != _refinement_value(candidate.get(field))
    ]


def validate_topic_refinement(parent, candidate, *, require_structural_pivot=False,
                              require_frontier_seed_pivot=False,
                              minimum_changed_dimensions=2):
    """Require an actual, bounded change when a topic is being refined."""
    if type(require_structural_pivot) is not bool:
        raise ValidationError("require_structural_pivot must be boolean")
    if type(require_frontier_seed_pivot) is not bool:
        raise ValidationError("require_frontier_seed_pivot must be boolean")
    if type(minimum_changed_dimensions) is not int or not 1 <= minimum_changed_dimensions <= len(REFINEMENT_COMPARE_FIELDS):
        raise ValidationError("minimum_changed_dimensions is invalid")
    changed = topic_refinement_dimensions(parent, candidate)
    if require_structural_pivot and len(changed) < minimum_changed_dimensions:
        raise ValidationError(
            "current topic refinement must change at least two substantive dimensions")
    if require_structural_pivot and not set(changed).intersection(PORTFOLIO_DIMENSIONS):
        raise ValidationError(
            "current topic refinement must change a research-shape dimension")
    if require_frontier_seed_pivot:
        parent_seed = parent.get("frontier_seed_id")
        candidate_seed = candidate.get("frontier_seed_id")
        if (not isinstance(parent_seed, str) or not parent_seed.strip()
                or not isinstance(candidate_seed, str) or not candidate_seed.strip()
                or parent_seed == candidate_seed):
            raise ValidationError(
                "source challenge requires a different frontier seed for this refinement")
    return changed


def validate_topic_package(value, *, objective=None, candidate_count=None,
                           experiment_capability_ids=None,
                           require_capability_coverage=False,
                           excluded_capability_ids=None,
                           excluded_topic_ids=None, topic_history=None,
                           design_driven_capability_ids=None,
                           frontier_seeds=None, recent_papers=None,
                           require_grounding=False, fallback_templates=None,
                           enforce_portfolio_diversity=False):
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
    if type(enforce_portfolio_diversity) is not bool:
        raise ValidationError("enforce_portfolio_diversity must be boolean")
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
        for key, values in (
                ("research_form", RESEARCH_FORM_VALUES),
                ("evidence_mode", EVIDENCE_MODE_VALUES),
                ("comparison_type", COMPARISON_TYPE_VALUES)):
            if key in candidate and candidate[key] not in values:
                raise ValidationError(
                    f"topic candidate {key} must be one of {list(values)}")
        if enforce_portfolio_diversity:
            missing_dimensions = [
                field for field in PORTFOLIO_DIMENSIONS if field not in candidate
            ]
            if missing_dimensions:
                raise ValidationError(
                    "current topic candidates require research-shape fields: "
                    + ", ".join(missing_dimensions))
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
    if enforce_portfolio_diversity:
        validate_topic_portfolio(candidates)
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
    structure = {field: candidate.get(field) for field in PORTFOLIO_DIMENSIONS}
    structure_fingerprint = None
    if all(isinstance(structure[field], str) and structure[field].strip()
           for field in PORTFOLIO_DIMENSIONS):
        structure_fingerprint = hashlib.sha256(canonical_bytes(structure)).hexdigest()
    return {
        "question": normalized_question,
        "title": normalized_title,
        "question_tokens": sorted(_topic_tokens(question)),
        "title_tokens": sorted(_topic_tokens(title)),
        "content_tokens": sorted(_topic_tokens(combined)),
        "fingerprint": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
        "structure": structure,
        "structure_fingerprint": structure_fingerprint,
    }


def _is_generated_slot_topic_id(value):
    """Return whether a model used a reusable portfolio slot ID.

    Models often turn a portfolio slot into a subject-shaped identifier such
    as ``direction_qft_topology_scaling``.  That is still an unstable label,
    not a durable scientific identity: a substantive repair may retain it
    while changing the question. Cross-mission novelty therefore relies on
    the question, domain, and structural fingerprint for these generated
    labels rather than treating the model's naming choice as an exclusion
    key.
    """
    if not isinstance(value, str):
        return False
    normalized = value.casefold()
    return normalized == "direction" or normalized.startswith((
        "direction_", "direction-", "dir_", "dir-", "topic_", "topic-",
    ))


def _candidate_attempt_record(package, *, attempt, status="parsed", error=None,
                              outcome_known=None):
    """Keep bounded signatures for every candidate package an attempt saw."""
    candidates = package.get("candidates", []) if isinstance(package, dict) else []
    signatures = []
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            signature = topic_signature(candidate)
            record = {
                "id": candidate.get("id") if isinstance(candidate.get("id"), str) else None,
                "domain": candidate.get("domain") if isinstance(candidate.get("domain"), str) else None,
                "fingerprint": signature["fingerprint"],
                "structure": signature["structure"],
                "structure_fingerprint": signature["structure_fingerprint"],
                "title_tokens": signature["title_tokens"],
                "question_tokens": signature["question_tokens"],
            }
            signatures.append(record)
    record = {
        "attempt": attempt,
        "status": status,
        "selected_id": package.get("selected_id") if isinstance(package, dict) else None,
        "candidate_signatures": signatures,
    }
    selected_id = record["selected_id"]
    selected = next(
        (candidate for candidate in candidates
         if isinstance(candidate, dict) and candidate.get("id") == selected_id),
        None,
    ) if isinstance(candidates, list) else None
    if isinstance(selected, dict):
        record["selected_topic"] = {
            key: selected.get(key) for key in (
                "id", "title", "domain", "research_question", "research_form",
                "evidence_mode", "comparison_type", "experiment_capability_id")
        }
    if isinstance(error, str) and error:
        record["error"] = error[:2048]
    if type(outcome_known) is bool:
        record["outcome_known"] = outcome_known
    if isinstance(candidates, list) and candidates and all(
            isinstance(candidate, dict)
            and all(candidate.get(field) in values for field, values in (
                ("research_form", RESEARCH_FORM_VALUES),
                ("evidence_mode", EVIDENCE_MODE_VALUES),
                ("comparison_type", COMPARISON_TYPE_VALUES)))
            for candidate in candidates):
        record["portfolio_profile"] = topic_portfolio_profile(candidates)
    return record


def _merge_candidate_source_records(selected, recent_papers, targeted_records,
                                    *, maximum=12):
    """Give the source challenger both cited evidence and fresh target hits.

    Candidate validation requires ``prior_work_ids`` to come from the
    science-first sample, but the old handoff sent only the later query hits
    to the challenger. That made a candidate's cited evidence invisible to
    the independent gate and could turn a grounded direction into a false
    weak-source rejection. Cited records are retained first, followed by
    unique targeted records, under a bounded packet size.
    """
    if type(maximum) is not int or not 1 <= maximum <= 64:
        raise ValidationError("candidate source record maximum is invalid")
    selected_ids = selected.get("prior_work_ids", []) if isinstance(selected, dict) else []
    if not isinstance(selected_ids, list):
        selected_ids = []
    by_id = {}
    for record in recent_papers or []:
        if not isinstance(record, dict) or not isinstance(record.get("work_id"), str):
            continue
        if record["work_id"] in selected_ids:
            by_id.setdefault(record["work_id"], deepcopy(record))
    for record in targeted_records or []:
        if not isinstance(record, dict) or not isinstance(record.get("work_id"), str):
            continue
        by_id.setdefault(record["work_id"], deepcopy(record))
        if len(by_id) >= maximum:
            break
    return list(by_id.values())[:maximum]


def _topic_history_entries(topic_history):
    if isinstance(topic_history, dict):
        entries = topic_history.get("entries", [])
    else:
        entries = topic_history
    return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []


def _topic_validation_history(topic_history, rejected_candidate_directions):
    """Add same-intake rejected directions to the novelty guard.

    Durable topic history records completed selections, while a bounded intake
    also needs negative memory: a direction rejected by its own source or
    maturity gate must not re-enter the next fresh proposal under a new title.
    Keep this projection local to the intake; a failed proposal is not a
    cross-mission scientific novelty claim.
    """
    rejected = [item for item in (rejected_candidate_directions or [])
                if isinstance(item, dict)]
    if not rejected:
        return topic_history
    entries = _topic_history_entries(topic_history)
    return {
        "schema_version": (topic_history.get("schema_version", TOPIC_HISTORY_SCHEMA_VERSION)
                           if isinstance(topic_history, dict) else TOPIC_HISTORY_SCHEMA_VERSION),
        "entries": [*deepcopy(entries), *deepcopy(rejected)],
    }


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
    current_structure = current.get("structure", {})
    previous_structure = previous.get("structure", {})
    if (not isinstance(previous_structure, dict)
            or not any(previous_structure.get(field) for field in PORTFOLIO_DIMENSIONS)):
        previous_structure = {
            field: prior.get(field) if isinstance(prior, dict) else None
            for field in PORTFOLIO_DIMENSIONS
        }
    if (current.get("structure_fingerprint")
            and previous.get("structure_fingerprint")
            and current["structure_fingerprint"] == previous["structure_fingerprint"]):
        # The same research-form/evidence/comparison tuple is a repeated
        # archetype even when the domain vocabulary is completely different.
        return 0.92
    structure_matches = sum(
        current_structure.get(field) is not None
        and current_structure.get(field) == previous_structure.get(field)
        for field in PORTFOLIO_DIMENSIONS
    )
    if structure_matches == len(PORTFOLIO_DIMENSIONS):
        return 0.92
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
    same_domain = (
        isinstance(candidate, dict) and isinstance(prior, dict)
        and isinstance(candidate.get("domain"), str)
        and isinstance(prior.get("domain"), str)
        and candidate["domain"].strip().casefold() == prior["domain"].strip().casefold()
    )
    if structure_matches >= 2 and same_domain:
        return max(content_score, 0.84)
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
        if (current_id and current_id == prior.get("topic_id")
                and not _is_generated_slot_topic_id(current_id)):
            raise ValidationError("selected topic repeats a previously attempted direction")
        score = _topic_repeat_score(candidate, prior)
        if score >= threshold:
            raise ValidationError(
                "selected topic is too similar to a previously attempted direction")
    return True


def _repair_topic_novelty_selection(package, topic_history, *, excluded_topic_ids=None,
                                    excluded_capability_ids=None):
    """Choose a valid portfolio member when the model selected a repeated one.

    The portfolio is already required to contain orthogonal candidates. If its
    selector keeps choosing the same rejected direction, spending another
    generation call only to rediscover that rejection is not useful progress.
    Select the first candidate that crosses the same history guard and leave
    all scientific validation gates in place for the caller.
    """
    if not isinstance(package, dict) or not isinstance(package.get("candidates"), list):
        return None
    selected_id = package.get("selected_id")
    selected = next(
        (item for item in package["candidates"]
         if isinstance(item, dict) and item.get("id") == selected_id),
        None,
    )
    if not isinstance(selected, dict):
        return None
    excluded_topics = set(excluded_topic_ids or [])
    excluded_capabilities = set(excluded_capability_ids or [])
    selection_error = None
    if selected.get("id") in excluded_topics:
        selection_error = ValidationError(
            "selected topic is excluded by the exploration history")
    elif selected.get("experiment_capability_id") in excluded_capabilities:
        selection_error = ValidationError(
            "selected experiment capability is excluded by the exploration history")
    try:
        if selection_error is None:
            validate_topic_novelty(selected, topic_history)
            return None
    except ValidationError as exc:
        selection_error = exc
    alternatives = []
    for index, candidate in enumerate(package["candidates"]):
        if not isinstance(candidate, dict) or candidate.get("id") == selected_id:
            continue
        if candidate.get("id") in excluded_topics:
            continue
        if candidate.get("experiment_capability_id") in excluded_capabilities:
            continue
        try:
            validate_topic_novelty(candidate, topic_history)
        except ValidationError:
            continue
        alternatives.append((index, candidate))
    if not alternatives:
        return None
    index, replacement = alternatives[0]
    package["selected_id"] = replacement["id"]
    package["selection_rationale"] = (
        f"The portfolio selector chose {replacement['id']} after the initially selected "
        f"direction {selected_id} was rejected by the attempted-direction history guard. "
        "The replacement is independently reassessed below."
    )
    return {
        "from_selected_id": selected_id,
        "to_selected_id": replacement["id"],
        "candidate_index": index,
        "reason": str(selection_error),
    }


def _grounding_eligible_frontier_seeds(frontier_seeds, recent_papers, candidate_count):
    """Limit candidate generation to seeds with inspectable source support.

    Frontier discovery may legitimately produce a direction whose OpenAlex
    search returns no works. That seed remains in the audit plan, but it cannot
    support a grounded candidate in the same intake. When enough grounded
    groups exist for the portfolio gate, hide unsupported seeds from the
    generation prompt so the model cannot select an impossible citation
    binding and burn every repair attempt on it.
    """
    seeds = [item for item in (frontier_seeds or []) if isinstance(item, dict)]
    work_seed_ids = {
        item.get("frontier_seed_id") for item in (recent_papers or [])
        if isinstance(item, dict)
        and isinstance(item.get("frontier_seed_id"), str)
        and isinstance(item.get("work_id"), str)
        and item.get("work_id")
    }
    grounded = [item for item in seeds if item.get("id") in work_seed_ids]
    minimum_groups = min(3, candidate_count) if type(candidate_count) is int else 3
    return grounded if len(grounded) >= minimum_groups else seeds


def _repair_foundry_selection(package, runtime_context):
    """Select an already-proposed candidate that fits a closed foundry boundary.

    The model may rank a portfolio member whose evidence mode cannot execute
    in the current foundry.  That is a selection error, not a scientific
    reason to discard every other validated candidate.  Re-select only from
    the existing portfolio, re-run the full capability check, and leave the
    source and maturity gates to assess the repaired selection independently.
    """
    if not isinstance(package, dict) or not isinstance(runtime_context, dict):
        return None
    foundry = runtime_context.get("capability_foundry")
    allowed = (foundry.get("allowed_evidence_modes")
               if isinstance(foundry, dict) and foundry.get("enabled") is True
               else None)
    if not isinstance(allowed, list) or not allowed:
        return None
    allowed = {mode for mode in allowed if mode in EVIDENCE_MODE_VALUES}
    selected_id = package.get("selected_id")
    candidates = package.get("candidates")
    if not isinstance(selected_id, str) or not isinstance(candidates, list):
        return None
    selected = next((item for item in candidates
                     if isinstance(item, dict) and item.get("id") == selected_id), None)
    if isinstance(selected, dict) and selected.get("evidence_mode") in allowed:
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("evidence_mode") not in allowed:
            continue
        trial = deepcopy(package)
        trial["selected_id"] = candidate.get("id")
        _materialize_foundry_capability_requirements(trial, runtime_context)
        try:
            feasibility = validate_topic_feasibility(trial, runtime_context)
        except ValidationError:
            continue
        replacement_id = candidate.get("id")
        if not isinstance(replacement_id, str):
            continue
        original_rationale = package.get("selection_rationale")
        package["selected_id"] = replacement_id
        _materialize_foundry_capability_requirements(package, runtime_context)
        package["selection_rationale"] = (
            f"The execution-eligible portfolio member {replacement_id} uses "
            f"{candidate['evidence_mode']} within the declared evidence boundary. "
            f"The originally ranked member {selected_id} was outside that boundary."
        )
        return {
            "from_selected_id": selected_id,
            "to_selected_id": replacement_id,
            "evidence_mode": candidate["evidence_mode"],
            "allowed_evidence_modes": sorted(allowed),
            "original_selection_rationale": original_rationale,
            "feasibility": feasibility,
        }
    return None


def _repair_topic_refinement_selection(package, parent_topic, runtime_context=None,
                                       *, require_frontier_seed_pivot=False,
                                       rejected_frontier_seed_ids=None):
    """Re-select a genuinely different member when refinement kept the parent shape.

    The shape plan is advisory input to the model, but models may reorder or
    ignore its candidate slots.  If the submitted selected candidate still has
    the parent's actual shape, choose an already validated portfolio member
    whose shape and at least one other substantive field differ.  The new
    selection is sent through the source and maturity gates again; no candidate
    prose is relabeled by this repair.
    """
    if (not isinstance(package, dict) or not isinstance(parent_topic, dict)
            or not isinstance(package.get("candidates"), list)):
        return None
    if type(require_frontier_seed_pivot) is not bool:
        raise ValidationError("require_frontier_seed_pivot must be boolean")
    rejected_frontier_seed_ids = {
        value for value in (rejected_frontier_seed_ids or [])
        if isinstance(value, str) and value.strip()
    }
    selected_id = package.get("selected_id")
    selected = next((item for item in package["candidates"]
                     if isinstance(item, dict) and item.get("id") == selected_id), None)
    if not isinstance(selected, dict):
        return None
    parent_shape = tuple(parent_topic.get(field) for field in PORTFOLIO_DIMENSIONS)
    selected_shape = tuple(selected.get(field) for field in PORTFOLIO_DIMENSIONS)
    parent_seed_id = parent_topic.get("frontier_seed_id")
    selected_seed_id = selected.get("frontier_seed_id")
    selected_seed_is_allowed = (
        not require_frontier_seed_pivot
        or (
            isinstance(selected_seed_id, str)
            and selected_seed_id != parent_seed_id
            and selected_seed_id not in rejected_frontier_seed_ids
        )
    )
    if selected_shape != parent_shape and selected_seed_is_allowed:
        return None
    alternatives = []
    for index, candidate in enumerate(package["candidates"]):
        if not isinstance(candidate, dict) or candidate.get("id") == selected_id:
            continue
        candidate_seed_id = candidate.get("frontier_seed_id")
        if (require_frontier_seed_pivot and (
                not isinstance(candidate_seed_id, str)
                or candidate_seed_id == parent_seed_id
                or candidate_seed_id in rejected_frontier_seed_ids)):
            continue
        candidate_shape = tuple(candidate.get(field) for field in PORTFOLIO_DIMENSIONS)
        if candidate_shape == parent_shape:
            continue
        changed = topic_refinement_dimensions(parent_topic, candidate)
        if len(changed) < 2 or not set(changed).intersection(PORTFOLIO_DIMENSIONS):
            continue
        trial = deepcopy(package)
        trial["selected_id"] = candidate.get("id")
        if isinstance(runtime_context, dict):
            _materialize_foundry_capability_requirements(trial, runtime_context)
            try:
                validate_topic_feasibility(trial, runtime_context)
            except ValidationError:
                continue
        alternatives.append((-len(changed), index, candidate, changed))
    if not alternatives:
        return None
    _, index, replacement, changed = sorted(alternatives, key=lambda item: (item[0], item[1]))[0]
    original_rationale = package.get("selection_rationale")
    package["selected_id"] = replacement["id"]
    package["selection_rationale"] = (
        f"The portfolio member {replacement['id']} is selected for refinement because it changes "
        f"the research shape and substantive fields ({', '.join(changed)}); the independent source "
        "and maturity gates must reassess it before survey admission."
    )
    return {
        "from_selected_id": selected_id,
        "to_selected_id": replacement["id"],
        "candidate_index": index,
        "changed_dimensions": changed,
        "parent_shape": dict(zip(PORTFOLIO_DIMENSIONS, parent_shape)),
        "replacement_shape": {
            field: replacement.get(field) for field in PORTFOLIO_DIMENSIONS
        },
        "original_selection_rationale": original_rationale,
        "require_frontier_seed_pivot": require_frontier_seed_pivot,
        "rejected_frontier_seed_ids": sorted(rejected_frontier_seed_ids),
    }


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
    foundry = runtime_context.get("capability_foundry")
    if isinstance(foundry, dict) and foundry.get("enabled") is True:
        allowed_evidence_modes = foundry.get("allowed_evidence_modes")
        if isinstance(allowed_evidence_modes, list) and allowed_evidence_modes:
            if selected.get("evidence_mode") not in set(allowed_evidence_modes):
                raise ValidationError(
                    "foundry-backed topic requires an analytical or synthetic evidence mode")
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


def validate_topic_maturity_review(value, *, candidate_ids=None,
                                   require_structural_pivot=False):
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
    if type(require_structural_pivot) is not bool:
        raise ValidationError("require_structural_pivot must be boolean")
    if value["decision"] == "refine" and not value["required_changes"]:
        raise ValidationError("topic maturity review refinement requires required_changes")
    if value["decision"] == "refine" and require_structural_pivot:
        if len(changed) < 2:
            raise ValidationError(
                "current topic refinement must change at least two substantive dimensions")
        if not set(changed).intersection(PORTFOLIO_DIMENSIONS):
            raise ValidationError(
                "current topic refinement must change a research-shape dimension")
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
    "domain terminology that a scholarly index can retrieve. Every query must reuse at least one exact "
    "content term from its own domain, phenomenon, mechanism, or unit_of_analysis. Do not claim novelty "
    "or results. Return JSON only."
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
    "Explore orthogonal directions before selecting: vary the research form, evidence mode, mechanism, data regime, comparison, "
    "or measurement rather than producing near-duplicate variants. Preserve one high-risk/high-upside "
    "direction when it is still feasible, alongside safer directions, so the selector can compare novelty "
    "risk against evidence and execution cost. "
    "Do not claim novelty, truth, or empirical results before the literature and methods stages run. "
    "Scientific questions must originate in the frontier evidence, not in the wording of an executable template. "
    "Only after defining the phenomenon and discriminating test should you check whether it can be investigated "
    "with public sources and a bounded reproducible experiment using the declared runtime capabilities. "
    "Do not collapse the portfolio into simulation merely because an instrument, specimen, or dataset is harder to access: "
    "record the exact capability requirement and keep a high-risk direction available for comparison. "
    "When runtime_context includes an experiment_catalog, every candidate must name one exact capability ID "
    "and align its phenomenon, comparison, data boundary, method, and primary outcomes with that capability. "
    "When runtime_context includes an experiment_contract, the selected candidate must be directly executable "
    "under that contract rather than silently proposing a different study. "
    "An executable question is only a starting point for a journal-oriented mission: give it a meaningful "
    "mechanism or boundary to discriminate, a comparison that can change the interpretation, and a result "
    "that could distinguish competing explanations. A single fixed parameter point or a two-method toy "
    "comparison must be treated as provisional unless it tests a nontrivial mechanism, a sensitivity frontier, "
    "or a theory-versus-observation discrepancy. "
    "When a refinement_context is supplied, preserve useful evidence but do not preserve the parent's central question automatically: "
    "materially change at least two dimensions and prefer a different research form or comparison type. "
    "Keep scope explicit, include a way the idea could be disproved, and select one candidate only after "
    "comparing the alternatives. Use reader-facing scientific language; do not mention workflow state, "
    "artifacts, validators, hashes, acceptance, or internal control terms. Return JSON only. "
    "For capability_requirements, copy exact names from the supplied runtime inventory and leave a list empty "
    "when a requirement is unnecessary. This field is optional on portfolio alternatives; for a selected "
    "candidate in a foundry-backed mission, the runtime supplies only the deterministic Python/experiment "
    "baseline when the field is omitted. If topic_exclusions are supplied, retain an excluded direction only as "
    "a rejected alternative and never select it."
)


TOPIC_FIELD_REPAIR_SYSTEM = (
    "You repair one missing structured field in a scientific topic portfolio. "
    "Use only the supplied candidate fields, preserve the candidate identity and research shape, "
    "and do not invent evidence, citations, results, or novelty. Return JSON only."
)


def _portfolio_shape_plan(candidate_count, seed=None, *, avoid_shape_by_index=None,
                          required_evidence_modes=None):
    """Create a stable, seeded shape plan for one candidate portfolio.

    The plan constrains only the epistemic shape labels. It does not choose a
    domain, mechanism, observable, or question for the model. Supplying the
    labels as explicit slots prevents a model from satisfying a long prose
    diversity instruction with several variants of its first familiar form.
    """
    if type(candidate_count) is not int or candidate_count < 1:
        raise ValidationError("portfolio shape plan requires a positive candidate count")
    rng = Random(seed if type(seed) is int and seed >= 0 else 0)
    forms = list(RESEARCH_FORM_VALUES)
    modes = list(EVIDENCE_MODE_VALUES)
    comparisons = list(COMPARISON_TYPE_VALUES)
    rng.shuffle(forms)
    rng.shuffle(modes)
    rng.shuffle(comparisons)

    # The admission gate requires four forms for a four-candidate portfolio,
    # and permits at most ceil(n / 4) candidates per form. Start with four
    # distinct forms, then cycle only when a larger configured portfolio needs
    # more slots.
    distinct_form_count = min(4, candidate_count)
    selected_forms = forms[:distinct_form_count]
    assigned_forms = [selected_forms[index % distinct_form_count]
                      for index in range(candidate_count)]
    distinct_mode_count = min(3, candidate_count)
    selected_modes = modes[:distinct_mode_count]
    assigned_modes = [selected_modes[index % distinct_mode_count]
                      for index in range(candidate_count)]
    distinct_comparison_count = min(3, candidate_count)
    selected_comparisons = comparisons[:distinct_comparison_count]
    assigned_comparisons = [selected_comparisons[index % distinct_comparison_count]
                            for index in range(candidate_count)]
    plan = [
        {
            "candidate_index": index,
            "research_form": assigned_forms[index],
            "evidence_mode": assigned_modes[index],
            "comparison_type": assigned_comparisons[index],
        }
        for index in range(candidate_count)
    ]
    allowed_modes = [mode for mode in (required_evidence_modes or [])
                     if mode in EVIDENCE_MODE_VALUES]
    if allowed_modes and not any(
            item["evidence_mode"] in set(allowed_modes) for item in plan):
        # A foundry-backed mission has a closed evidence boundary. Preserve
        # the portfolio's required diversity, but guarantee that at least one
        # candidate can enter that boundary instead of making selection
        # infeasible by construction.
        plan[-1]["evidence_mode"] = allowed_modes[0]
    # A structural refinement keeps the candidate IDs stable where possible,
    # but it must not pin the selected candidate to the same epistemic slot on
    # every turn.  Swap a complete slot with its neighbour when the caller
    # identifies the parent's position; this preserves every portfolio
    # diversity invariant while making the required shape pivot executable.
    if isinstance(avoid_shape_by_index, dict) and len(plan) > 1:
        for raw_index, forbidden in avoid_shape_by_index.items():
            if (type(raw_index) is not int or not 0 <= raw_index < len(plan)
                    or not isinstance(forbidden, dict)):
                continue
            current = tuple(plan[raw_index].get(field) for field in PORTFOLIO_DIMENSIONS)
            forbidden_tuple = tuple(forbidden.get(field) for field in PORTFOLIO_DIMENSIONS)
            if current != forbidden_tuple:
                continue
            other = (raw_index + 1) % len(plan)
            for field in PORTFOLIO_DIMENSIONS:
                plan[raw_index][field], plan[other][field] = (
                    plan[other][field], plan[raw_index][field])
    return plan


def _topic_prompt_clip(value, limit):
    """Clip reader-facing prompt prose without changing structured meaning."""
    if not isinstance(value, str):
        return value
    value = value.strip()
    return value if len(value) <= limit else value[:max(0, limit - 1)].rstrip() + "…"


def _topic_prompt_paper_projection(value):
    """Keep source identity and a small evidence window for topic generation."""
    if not isinstance(value, dict):
        return value
    return {
        key: _topic_prompt_clip(value.get(key), limit)
        for key, limit in (
            ("work_id", 80), ("title", 260), ("abstract", 900),
            ("authors", 240), ("doi", 160), ("frontier_domain", 180),
            ("frontier_seed_id", 100), ("matched_query", 260),
            ("year", 12), ("source_url", 500),
        ) if value.get(key) is not None
    }


def _topic_prompt_seed_projection(value):
    """Bound frontier seed prose while preserving its grounding identity."""
    if not isinstance(value, dict):
        return value
    return {
        key: _topic_prompt_clip(value.get(key), limit)
        for key, limit in (
            ("id", 100), ("domain", 180), ("phenomenon", 700),
            ("mechanism", 700), ("unit_of_analysis", 500),
        ) if value.get(key) is not None
    } | {
        "search_queries": [
            _topic_prompt_clip(item, 260)
            for item in value.get("search_queries", [])[:8]
            if isinstance(item, str)
        ]
    }


def _topic_prompt_candidate_projection(value):
    """Project one prior candidate attempt without replaying token-heavy traces."""
    if not isinstance(value, dict):
        return value
    selected = value.get("selected_topic")
    selected_projection = None
    if isinstance(selected, dict):
        selected_projection = {
            key: _topic_prompt_clip(selected.get(key), limit)
            for key, limit in (
                ("id", 100), ("title", 260), ("domain", 180),
                ("research_question", 900), ("research_form", 80),
                ("evidence_mode", 80), ("comparison_type", 80),
                ("frontier_seed_id", 100),
            ) if selected.get(key) is not None
        }
    result = {
        key: value.get(key) for key in ("attempt", "status", "selected_id", "outcome_known")
        if key in value
    }
    if selected_projection is not None:
        result["selected_topic"] = selected_projection
    if value.get("error") is not None:
        result["error"] = _topic_prompt_clip(value.get("error"), 900)
    for label in ("source_challenge", "maturity_review"):
        review = value.get(label)
        if not isinstance(review, dict):
            continue
        result[label] = {
            key: (_topic_prompt_clip(review.get(key), 1200)
                  if isinstance(review.get(key), str)
                  else [str(item)[:400] for item in review.get(key, [])[:6]]
                  if isinstance(review.get(key), list)
                  else review.get(key))
            for key in ("decision", "prior_work_risk", "direct_comparison_match",
                        "rationale", "required_changes", "evidence_gaps")
            if key in review
        }
    if isinstance(value.get("portfolio_profile"), dict):
        profile = value["portfolio_profile"]
        result["portfolio_profile"] = {
            key: profile.get(key) for key in ("candidate_count", "counts", "distinct")
            if key in profile
        }
    return result


def _topic_prompt_rejection_projection(value):
    """Keep rejection causes while dropping duplicate signatures and token lists."""
    if not isinstance(value, dict):
        return value
    result = {
        key: _topic_prompt_clip(value.get(key), limit)
        for key, limit in (
            ("topic_id", 100), ("title", 260), ("domain", 180),
            ("research_question", 900), ("research_form", 80),
            ("evidence_mode", 80), ("comparison_type", 80),
            ("rejection_type", 80), ("rejection_reason", 900),
        ) if value.get(key) is not None
    }
    if isinstance(value.get("required_changes"), list):
        result["required_changes"] = [str(item)[:400] for item in value["required_changes"][:6]]
    return result


def _topic_prompt_specialist_projection(value):
    """Pass findings to a refinement without echoing raw provider responses."""
    if not isinstance(value, dict):
        return value
    result = {
        key: _topic_prompt_clip(value.get(key), 1400)
        for key in ("assigned_role", "role_id", "decision", "summary")
        if value.get(key) is not None
    }
    for key in ("findings", "evidence_gaps", "requested_actions"):
        items = value.get(key)
        if isinstance(items, list):
            result[key] = [str(item)[:500] for item in items[:4]]
    return result


def _topic_prompt_refinement_projection(value):
    """Bound a continuation handoff while retaining every repair obligation."""
    if not isinstance(value, dict):
        return value
    result = {
        key: value.get(key) for key in (
            "mode", "cycle", "parent_topic_id", "parent_candidate_index",
            "changed_dimensions", "require_frontier_seed_pivot",
            "rejected_frontier_seed_ids",
        )
        if key in value
    }
    parent = value.get("parent_topic")
    if isinstance(parent, dict):
        result["parent_topic"] = {
            key: _topic_prompt_clip(parent.get(key), limit)
            for key, limit in (
                ("id", 100), ("title", 260), ("domain", 180),
                ("research_question", 900), ("research_form", 80),
                ("evidence_mode", 80), ("comparison_type", 80),
                ("frontier_seed_id", 100), ("prior_work_ids", 300),
                ("mechanism", 700), ("data_regime", 700),
                ("comparison", 700), ("measurement", 700),
                ("theory_target", 700), ("scope", 700),
                ("resource_plan", 700), ("disconfirmation_test", 700),
            ) if parent.get(key) is not None
        }
    feedback = value.get("refinement_feedback")
    if isinstance(feedback, dict):
        result["refinement_feedback"] = {
            key: (_topic_prompt_clip(feedback.get(key), 2200)
                  if isinstance(feedback.get(key), str)
                  else [str(item)[:900] for item in feedback.get(key, [])[:8]]
                  if isinstance(feedback.get(key), list)
                  else feedback.get(key))
            for key in ("review_type", "decision", "rationale", "required_changes",
                        "critical_findings", "changed_dimensions",
                        "require_frontier_seed_pivot", "rejected_frontier_seed_ids")
            if key in feedback
        }
    survey = value.get("survey_feedback")
    if isinstance(survey, dict):
        survey_result = {
            key: (_topic_prompt_clip(survey.get(key), 1600)
                  if isinstance(survey.get(key), str) else survey.get(key))
            for key in ("gap_state", "nomination", "assessment_ref")
            if key in survey
        }
        evidence = survey.get("evidence")
        if isinstance(evidence, dict):
            survey_result["evidence"] = {
                str(key): _topic_prompt_clip(item, 6000)
                for key, item in list(evidence.items())[:2]
                if isinstance(item, str)
            }
        result["survey_feedback"] = survey_result
    if isinstance(value.get("specialist_feedback"), list):
        result["specialist_feedback"] = [
            _topic_prompt_specialist_projection(item)
            for item in value["specialist_feedback"][:2]
            if isinstance(item, dict)
        ]
    if value.get("reason") is not None:
        result["reason"] = _topic_prompt_clip(value.get("reason"), 1200)
    return result


def _topic_prompt_runtime_projection(value):
    """Keep executable constraints and remove environment-sized bookkeeping."""
    if not isinstance(value, dict):
        return value
    result = deepcopy(value)
    project_files = result.get("project_files")
    if isinstance(project_files, list):
        projected_files = []
        for item in project_files[:40]:
            if isinstance(item, str):
                projected_files.append(_topic_prompt_clip(item, 500))
            elif isinstance(item, dict):
                projected_files.append({
                    key: (_topic_prompt_clip(item.get(key), 500)
                          if isinstance(item.get(key), str) else item.get(key))
                    for key in ("path", "kind", "label", "size", "sha256")
                    if item.get(key) is not None
                })
        result["project_files"] = projected_files
    history = result.get("topic_history")
    if isinstance(history, dict) and isinstance(history.get("entries"), list):
        result["topic_history"] = {
            **{key: history.get(key) for key in ("schema_version", "scope_key", "capability_counts")
               if key in history},
            "entries": [
                _topic_prompt_rejection_projection(item)
                for item in history["entries"][-8:]
                if isinstance(item, dict)
            ],
        }
    reports = result.get("independent_specialist_reports")
    if isinstance(reports, list):
        result["independent_specialist_reports"] = [
            _topic_prompt_specialist_projection(item)
            for item in reports[:2] if isinstance(item, dict)
        ]
    catalog = result.get("experiment_catalog")
    if isinstance(catalog, list):
        for item in catalog:
            if isinstance(item, dict) and isinstance(item.get("design_template"), dict):
                encoded = json.dumps(item["design_template"], ensure_ascii=False, sort_keys=True)
                item["design_template"] = encoded[:5000]
    return result


def topic_prompt(objective, candidate_count, *, recent_papers=None, frontier_seeds=None,
                 runtime_context=None, refinement_context=None, candidate_history=None,
                 rejected_candidate_directions=None, portfolio_seed=None):
    def reader_projection(value):
        """Keep internal labels out of the model's reader-facing topic prose."""
        if isinstance(value, dict):
            return {key: reader_projection(item) for key, item in value.items()}
        if isinstance(value, list):
            return [reader_projection(item) for item in value]
        if isinstance(value, str):
            return project_internal_language(value)
        return value

    runtime_context = _topic_prompt_runtime_projection(reader_projection(runtime_context or {}))
    # Frozen fallback templates are retained for the independent challenge,
    # but a foundry-backed candidate generator must never see them as idea
    # seeds before it has defined the scientific question.
    runtime_context.pop("fallback_experiment_catalog", None)
    recent_papers = [
        _topic_prompt_paper_projection(reader_projection(item))
        for item in (recent_papers or [])[:TOPIC_SAMPLE_LIMIT] if isinstance(item, dict)
    ]
    frontier_seeds = [
        _topic_prompt_seed_projection(reader_projection(item))
        for item in (frontier_seeds or []) if isinstance(item, dict)
    ]
    candidate_history = [
        _topic_prompt_candidate_projection(reader_projection(item))
        for item in (candidate_history or [])[-4:] if isinstance(item, dict)
    ]
    rejected_candidate_directions = [
        _topic_prompt_rejection_projection(reader_projection(item))
        for item in (rejected_candidate_directions or [])[-6:] if isinstance(item, dict)
    ]
    refinement_context = _topic_prompt_refinement_projection(
        reader_projection(refinement_context)) if refinement_context else None
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
        "domain": "research domain; required even when it repeats the frontier seed domain",
        "research_question": "one testable question",
        "research_form": f"one of {list(RESEARCH_FORM_VALUES)}; the epistemic form of the study",
        "evidence_mode": f"one of {list(EVIDENCE_MODE_VALUES)}; the primary evidence source",
        "comparison_type": f"one of {list(COMPARISON_TYPE_VALUES)}; the structural comparison",
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
    }
    constraints = [
        "use recent_papers as inspiration and retain their provided source identifiers in the candidate rationale when relevant",
        "anchor every candidate to a supplied frontier seed and its matching scholarly records",
        "domain is mandatory on every candidate; copy the exact domain from the candidate's frontier_seed_id",
        "define the scientific question before choosing an execution capability; never paraphrase a capability template as a topic",
        "keep one primary phenomenon, one main mechanism or boundary, and one primary observable; do not couple several mechanisms or statistical tests unless the supplied seed records support each component",
        "do not introduce a mechanism, population, observable, model family, or statistical test that is absent from the supplied seed/evidence without a concrete bounded source query and resource plan",
        "candidate questions must differ in research form, evidence mode, mechanism, or empirical comparison, not just wording",
        "treat the candidate list as a research portfolio: cover the requested distinct research forms, evidence modes, and comparison types",
        "never repeat the same research_form/evidence_mode/comparison_type tuple for two candidates",
        "cover at least three distinct axes across the candidates when the objective and runtime permit: mechanism, data regime, comparison, measurement, or theory",
        "do not collapse every candidate onto the first familiar method merely because it is easiest to explain; preserve at least one non-simulation evidence mode when the frontier seeds support it",
        "search queries must be usable as ordinary scholarly search strings",
        "capability_requirements is derived from the declared runtime boundary; do not emit it in candidate objects",
        "never invent a citation, dataset, result, or prior-work claim",
        "keep every narrative field concise (at most 45 words), keep each search query under 12 words, and fit the complete JSON package within 5000 output tokens",
        "include every required top-level key and every required candidate key; never stop after a partial candidate list",
        "emit every required field described by the candidate contract; research_question is mandatory",
        "return only the JSON object with no preface, commentary, markdown, or trailing explanation",
        "candidate prose is reader-facing: do not use the words frozen, validator, accepted artifact, model calls, release candidate, or SHA-256; say prespecified or independent recalculation where scientifically appropriate",
    ]
    portfolio_requirements = {
        "minimum_distinct_research_forms": min(4, candidate_count),
        "minimum_distinct_evidence_modes": min(3, candidate_count),
        "minimum_distinct_comparison_types": min(3, candidate_count),
        "maximum_candidates_per_research_form": max(2, math.ceil(candidate_count / 4)),
    }
    avoid_shape_by_index = None
    if isinstance(refinement_context, dict):
        parent_topic = refinement_context.get("parent_topic")
        parent_index = refinement_context.get("parent_candidate_index")
        feedback = refinement_context.get("refinement_feedback")
        if type(parent_index) is not int and isinstance(feedback, dict):
            parent_index = feedback.get("parent_candidate_index")
        if (type(parent_index) is int and isinstance(parent_topic, dict)
                and 0 <= parent_index < candidate_count):
            avoid_shape_by_index = {parent_index: {
                field: parent_topic.get(field) for field in PORTFOLIO_DIMENSIONS
            }}
    foundry = runtime_context.get("capability_foundry")
    required_evidence_modes = (
        foundry.get("allowed_evidence_modes")
        if isinstance(foundry, dict) and foundry.get("enabled") is True
        else None
    )
    portfolio_shape_plan = _portfolio_shape_plan(
        candidate_count, portfolio_seed,
        avoid_shape_by_index=avoid_shape_by_index,
        required_evidence_modes=required_evidence_modes)
    constraints.append(
        "satisfy portfolio_requirements exactly; if the available evidence cannot support a diverse executable portfolio, return the best diverse package and let the admission gate reject it rather than duplicating one form"
    )
    constraints.append(
        "realize portfolio_shape_plan exactly: create one candidate for every slot, copy each slot's "
        "research_form, evidence_mode, and comparison_type literally, and keep the scientific prose "
        "consistent with those labels; do not omit, merge, or invent slots"
    )
    if rejected_candidate_directions:
        constraints.append(
            "do not select a direction that repeats any rejected_candidate_directions entry; change the "
            "scientific mechanism, phenomenon, boundary, or measurement, not only the title or threshold"
        )
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
                "avoid repeating any previously attempted direction or research archetype in topic_history; select a materially different question, research form, evidence mode, or comparison type")
        design_driven = [item for item in catalog
                         if isinstance(item, dict) and item.get("design_driven")]
        if design_driven:
            ids = sorted(item.get("id") for item in design_driven if isinstance(item.get("id"), str))
            candidate_contract["experiment_design"] = (
                "REQUIRED for any candidate whose experiment_capability_id is one of "
                f"{ids}: a bounded declarative study design for the pinned design-driven engine "
                "(family, data_process, estimators, primary, baseline, seed, and optional "
                "trim_fraction/block_size). This is data the reviewed engine executes, never code.")
            constraints.extend([
                "for a design-driven capability, experiment_design must copy the family and data_process "
                "shape from the supplied template and choose declared estimators/primary/baseline that "
                "the capability actually supports",
                "experiment_design must remain executable under the frozen engine: no new estimator names, "
                "no new data-process kinds, and no field outside the declared schema",
                "include experiment_design only for a candidate using a design-driven capability; omit that key "
                "for every other capability",
            ])
    topic_preferences = runtime_context.get("topic_preferences") or {}
    if isinstance(topic_preferences, dict):
        mode = topic_preferences.get("mode")
        must_have = [item for item in topic_preferences.get("must_have", [])
                     if isinstance(item, str) and item.strip()]
        avoid = [item for item in topic_preferences.get("avoid", [])
                 if isinstance(item, str) and item.strip()]
        if mode == "computational_native":
            constraints.extend([
                "prefer a computational-native scientific question: computation must be the primary instrument for testing a substantive mechanism, boundary, scaling relation, or theory discrepancy, not merely a software or workflow demonstration",
                "the selected candidate must satisfy every item in topic_preferences.must_have; alternatives may vary their epistemic shape, but the selected direction must remain executable inside the declared deterministic Python boundary",
            ])
        if must_have:
            constraints.append(
                "the selected candidate must satisfy these declared topic preferences: "
                + "; ".join(must_have))
        if avoid:
            constraints.append(
                "avoid these topic patterns unless the supplied evidence makes them necessary: "
                + "; ".join(avoid))
    foundry = runtime_context.get("capability_foundry")
    if isinstance(foundry, dict) and foundry.get("enabled") is True:
        allowed_evidence_modes = foundry.get("allowed_evidence_modes") or []
        if allowed_evidence_modes:
            constraints.extend([
                "the selected candidate must use one of the foundry's allowed evidence modes: "
                f"{allowed_evidence_modes}; the generated executor has no network access or undeclared external data",
                "for the selected candidate, make the executable evidence a mechanism-specific analytic or synthetic simulation with a state variable, scaling relation, or distributional observable; do not select a generic estimator benchmark, data-fitting exercise, or confidence-interval restatement",
                "alternatives may retain other evidence modes for portfolio diversity, but they must not be selected unless their data boundary is explicitly available in the foundry project files",
            ])
    if refinement_context:
        constraints.extend([
            "this is a topic refinement pass, not a cosmetic rewrite: use the parent topic and the supplied survey feedback as constraints",
            "change at least two substantive dimensions and include research_form, evidence_mode, or comparison_type among changed_dimensions when the reviewer asks for refinement",
            "repair the reviewed direction when it remains viable; otherwise select another candidate from this portfolio only when that candidate is structurally independent and directly addresses the supplied feedback",
            "do not merely retain the parent's central phenomenon with a new parameter; pivot to an orthogonal question when the parent remains too close to an existing direction",
            "do not select a direction that the supplied evidence already refutes; if the parent is refuted, pivot to a discriminating unresolved question",
            "treat the supplied survey evidence and source spans as the reason for the redesign; do not invent a gap that is absent from them",
        ])
        rejected_seed_ids = refinement_context.get("rejected_frontier_seed_ids")
        if (refinement_context.get("require_frontier_seed_pivot") is True
                and isinstance(rejected_seed_ids, list) and rejected_seed_ids):
            constraints.extend([
                "the following frontier seeds have already failed the independent source challenge in this intake; do not select any of them again: "
                + ", ".join(str(item) for item in rejected_seed_ids),
                "choose a supplied seed outside that rejected set and cite a work from that seed; a new title or parameter on a rejected seed is not a repair",
            ])
        parent_topic = refinement_context.get("parent_topic")
        if isinstance(parent_topic, dict):
            parent_shape = {
                field: parent_topic.get(field) for field in PORTFOLIO_DIMENSIONS
            }
            alternative_shape_values = {
                field: [value for value in values if value != parent_shape.get(field)]
                for field, values in (
                    ("research_form", RESEARCH_FORM_VALUES),
                    ("evidence_mode", EVIDENCE_MODE_VALUES),
                    ("comparison_type", COMPARISON_TYPE_VALUES),
                )
            }
            constraints.append(
                "the selected candidate must change at least one of the parent's research-shape fields; "
                f"parent tuple is {parent_shape}; allowed alternative values are {alternative_shape_values}"
            )
        feedback = refinement_context.get("refinement_feedback")
        if isinstance(feedback, dict):
            required_changes = feedback.get("required_changes")
            if isinstance(required_changes, list) and required_changes:
                constraints.extend([
                    "the selected candidate must explicitly address every item in repair_specification.required_changes; do not merely acknowledge them in selection_rationale",
                    "make each repair observable in phenomenon, mechanism, data_regime, comparison, measurement, theory_target, disconfirmation_test, disconfirmation_test_note, or resource_plan",
                    "when a reviewer requests quantitative executability, state the measurable variable or estimand, the scaling/equation or operational definition, the bounded comparison range or baseline, and the result that would disconfirm the direction",
                ])
        if refinement_context.get("require_frontier_seed_pivot") is True:
            constraints.extend([
                "the source challenge found weak grounding together with high prior-work risk; abandon the parent's frontier seed rather than rewriting the same direction",
                "the selected candidate must use a different frontier_seed_id from the parent and cite at least one scholarly record from that new seed",
                "do not rescue the parent's seed by changing only a statistic, threshold, title, or wording; select a structurally independent direction supported by another supplied seed",
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
        "refinement_shape": {
            "selected_id_rule": "keep the parent ID when repairing it; a different portfolio candidate is allowed only when it is structurally independent and addresses the feedback",
            "minimum_changed_dimensions": 2,
            "required_shape_pivot": "change research_form, evidence_mode, or comparison_type",
            "frontier_seed_pivot_required": bool(
                refinement_context.get("require_frontier_seed_pivot") is True
            ) if refinement_context else False,
        } if refinement_context else {},
        "portfolio_requirements": portfolio_requirements,
        "portfolio_shape_plan": portfolio_shape_plan,
        "previous_candidate_directions": candidate_history,
        "rejected_candidate_directions": rejected_candidate_directions,
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


def _refinement_target_shape(package, parent_candidate_id, runtime_context, seed=None):
    """Choose an unused research shape for a bounded single-candidate repair."""
    if not isinstance(package, dict) or not isinstance(package.get("candidates"), list):
        return None
    candidates = package["candidates"]
    parent = next(
        (item for item in candidates
         if isinstance(item, dict) and item.get("id") == parent_candidate_id),
        None,
    )
    if not isinstance(parent, dict):
        return None
    parent_shape = tuple(parent.get(field) for field in PORTFOLIO_DIMENSIONS)
    occupied = {
        tuple(item.get(field) for field in PORTFOLIO_DIMENSIONS)
        for item in candidates
        if isinstance(item, dict) and item.get("id") != parent_candidate_id
    }
    foundry = (runtime_context or {}).get("capability_foundry")
    allowed_modes = {
        item for item in (foundry.get("allowed_evidence_modes") or [])
        if item in EVIDENCE_MODE_VALUES
    } if isinstance(foundry, dict) and foundry.get("enabled") is True else set()
    slots = _portfolio_shape_plan(len(candidates), seed)
    slots.extend(
        dict(zip(PORTFOLIO_DIMENSIONS, values))
        for values in itertools.product(
            RESEARCH_FORM_VALUES, EVIDENCE_MODE_VALUES, COMPARISON_TYPE_VALUES)
    )
    for slot in slots:
        shape = tuple(slot.get(field) for field in PORTFOLIO_DIMENSIONS)
        if shape == parent_shape or shape in occupied:
            continue
        if allowed_modes and slot.get("evidence_mode") not in allowed_modes:
            continue
        trial = deepcopy(candidates)
        replacement = deepcopy(parent)
        replacement.update(slot)
        trial[trial.index(parent)] = replacement
        try:
            validate_topic_portfolio(trial)
        except ValidationError:
            continue
        return {field: slot[field] for field in PORTFOLIO_DIMENSIONS}
    return None


def _refinement_target_seed(frontier_seeds, recent_papers, parent_seed_id,
                            refinement_feedback, *, sampling_seed=None,
                            occupied_seed_ids=None):
    """Choose a grounded seed for a single-candidate repair.

    A source challenge that marks the selected direction high-risk must move
    to a different supplied seed.  The choice is seeded and restricted to
    seeds with at least one supplied record, so the repair cannot manufacture
    a new evidence link.
    """
    seeds = [
        item for item in (frontier_seeds or [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    record_seed_ids = {
        item.get("frontier_seed_id") for item in (recent_papers or [])
        if isinstance(item, dict) and isinstance(item.get("frontier_seed_id"), str)
    }
    rejected = {
        item for item in (refinement_feedback or {}).get("rejected_frontier_seed_ids", [])
        if isinstance(item, str) and item.strip()
    } if isinstance(refinement_feedback, dict) else set()
    occupied = {
        item for item in (occupied_seed_ids or [])
        if isinstance(item, str) and item.strip() and item != parent_seed_id
    }
    force_pivot = (
        isinstance(refinement_feedback, dict)
        and (
            refinement_feedback.get("require_frontier_seed_pivot") is True
            or refinement_feedback.get("prior_work_risk") == "high"
        )
    )
    eligible = [
        item for item in seeds
        if item["id"] not in rejected
        and item["id"] in record_seed_ids
        and (not force_pivot or item["id"] != parent_seed_id)
    ]
    if not eligible and force_pivot:
        eligible = [
            item for item in seeds
            if item["id"] not in rejected and item["id"] != parent_seed_id
            and item["id"] in record_seed_ids
        ]
    if not eligible:
        eligible = [
            item for item in seeds
            if item["id"] not in rejected and item["id"] in record_seed_ids
        ]
    if not eligible:
        return None
    unoccupied = [item for item in eligible if item["id"] not in occupied]
    if unoccupied:
        eligible = unoccupied
    if not force_pivot:
        preferred = next((item for item in eligible if item["id"] == parent_seed_id), None)
        if preferred is not None:
            eligible = [preferred] + [item for item in eligible if item is not preferred]
    else:
        rng = Random(sampling_seed if type(sampling_seed) is int and sampling_seed >= 0 else 0)
        rng.shuffle(eligible)
    target = eligible[0]
    return {
        "target_seed_id": target["id"],
        "eligible_seed_ids": [item["id"] for item in eligible],
        "force_pivot": force_pivot,
        "target_work_ids": [
            item["work_id"] for item in (recent_papers or [])
            if isinstance(item, dict)
            and item.get("frontier_seed_id") == target["id"]
            and isinstance(item.get("work_id"), str)
        ][:5],
    }


def _topic_candidate_refinement_prompt(objective, parent_candidate, *,
                                       base_package, target_shape, target_seed,
                                       frontier_seeds, recent_papers, runtime_context,
                                       refinement_feedback):
    """Build a compact contract for repairing only the rejected selection."""
    def reader_projection(value):
        if isinstance(value, dict):
            return {key: reader_projection(item) for key, item in value.items()}
        if isinstance(value, list):
            return [reader_projection(item) for item in value]
        if isinstance(value, str):
            return project_internal_language(value)
        return value

    runtime_projection = _topic_prompt_runtime_projection(
        reader_projection(runtime_context or {}))
    runtime_projection.pop("fallback_experiment_catalog", None)
    seeds_projection = reader_projection(frontier_seeds or [])
    records_projection = [
        _topic_prompt_paper_projection(reader_projection(item))
        for item in (recent_papers or []) if isinstance(item, dict)
    ]
    parent_projection = reader_projection(parent_candidate)
    feedback_source = reader_projection(refinement_feedback or {})
    feedback_projection = {
        key: (_topic_prompt_clip(feedback_source.get(key), 2200)
              if isinstance(feedback_source.get(key), str)
              else [str(item)[:900] for item in feedback_source.get(key, [])[:8]]
              if isinstance(feedback_source.get(key), list)
              else feedback_source.get(key))
        for key in ("review_type", "decision", "rationale", "required_changes",
                    "critical_findings", "changed_dimensions",
                    "require_frontier_seed_pivot", "rejected_frontier_seed_ids")
        if key in feedback_source
    }
    catalog = runtime_projection.get("experiment_catalog") or []
    candidate_contract = {
        "id": "copy candidate_id_to_copy_exactly",
        "title": "short working title",
        "domain": "copy the exact domain of the selected frontier seed",
        "research_question": "one testable question",
        "research_form": f"copy required_shape.research_form exactly from {list(RESEARCH_FORM_VALUES)}",
        "evidence_mode": f"copy required_shape.evidence_mode exactly from {list(EVIDENCE_MODE_VALUES)}",
        "comparison_type": f"copy required_shape.comparison_type exactly from {list(COMPARISON_TYPE_VALUES)}",
        "phenomenon": "one concrete phenomenon",
        "mechanism": "one mechanism or explanatory variable to discriminate",
        "data_regime": "bounded data, boundary, or population regime",
        "comparison": "the comparison that separates explanations",
        "measurement": "primary observable and operational measurement",
        "theory_target": "the relation, theory, or boundary under test",
        "scope": "explicit system, population, or data boundary",
        "search_queries": "3 to 5 scholarly search strings",
        "why_promising": "why the question is worth investigating without claiming novelty",
        "disconfirmation_test": "result or prior evidence that would disconfirm the direction",
        "disconfirmation_test_note": "optional operational detail for the disconfirmation test",
        "feasibility": "why the declared runtime can execute this bounded study",
        "resource_plan": "data, programs, tools, and compute used",
        "frontier_seed_id": "copy target_frontier_seed_id exactly",
        "prior_work_ids": "one to three work_id values from target_seed_records only",
    }
    if catalog:
        candidate_contract["experiment_capability_id"] = (
            "copy the parent candidate's exact experiment_capability_id")
        design_ids = {
            item.get("id") for item in catalog
            if isinstance(item, dict) and item.get("design_driven")
        }
        parent_capability = parent_candidate.get("experiment_capability_id")
        if parent_capability in design_ids:
            candidate_contract["experiment_design"] = (
                "copy the parent's valid bounded design and change only values allowed by its template")
    return json.dumps({
        "assignment": "repair_selected_topic_candidate",
        "principal_objective": objective,
        "candidate_id_to_copy_exactly": parent_candidate.get("id"),
        "parent_candidate": parent_projection,
        "portfolio_shape_occupied_by_other_candidates": [
            {field: item.get(field) for field in PORTFOLIO_DIMENSIONS}
            for item in (base_package.get("candidates") or [])
            if isinstance(item, dict) and item.get("id") != parent_candidate.get("id")
        ],
        "required_shape": target_shape,
        "target_frontier_seed_id": target_seed.get("target_seed_id"),
        "allowed_frontier_seed_ids": target_seed.get("eligible_seed_ids", []),
        "target_seed_records": [
            record for record in records_projection
            if isinstance(record, dict)
            and record.get("frontier_seed_id") == target_seed.get("target_seed_id")
        ],
        "frontier_seeds": seeds_projection,
        "targeted_feedback": feedback_projection,
        "runtime_context": runtime_projection,
        "output_contract": {
            "candidate": candidate_contract,
        },
        "constraints": [
            "Return exactly one object with exactly one top-level key: candidate.",
            "Copy the candidate id exactly; do not create a new id and do not return package metadata.",
            "Copy target_frontier_seed_id exactly and cite only target_seed_records; never invent a work id.",
            "Copy required_shape exactly; the replacement must not use the parent's research shape.",
            "Change at least one substantive scientific field in addition to the required shape: "
            "research_question, mechanism, data_regime, comparison, measurement, theory_target, or scope.",
            "Keep one phenomenon, one main mechanism, one comparison, and one primary observable.",
            "Keep every narrative field under 45 words and every query under 12 words.",
            "Do not claim novelty, a result, or a literature gap; those require later evidence.",
            "Do not include capability_requirements unless it is a complete object copied from the parent.",
            "Return only the JSON object with no markdown or explanation.",
        ],
    }, ensure_ascii=False, sort_keys=True)


SOURCE_CHALLENGE_SYSTEM = (
    "You are an independent intake challenger. Decide only whether a selected question is relevantly grounded "
    "enough and sufficiently independent from any supplied executable templates to deserve a full literature "
    "survey. This is not a novelty verdict. Reject a question that merely restates a template, whose targeted "
    "search results are mostly irrelevant, or whose closest supplied work already answers the same comparison "
    "without a meaningful changed mechanism, boundary, or measurement. Broad topical adjacency is not the same "
    "as an answered comparison: if no supplied work directly answers the selected comparison, use medium rather "
    "than high prior_work_risk when the source and template scores pass. Do not reject merely because a review "
    "or neighboring application mentions the same domain. Cite only supplied work IDs. Return JSON only."
)


def _source_challenge_prompt(selected, works, runtime_context):
    catalog = ((runtime_context or {}).get("experiment_catalog")
               or (runtime_context or {}).get("fallback_experiment_catalog") or [])
    templates = [{key: item.get(key) for key in (
        "id", "domain", "research_question", "method", "primary_outcomes")}
        for item in catalog if isinstance(item, dict)]
    allowed_work_ids = [
        item["work_id"] for item in works
        if isinstance(item, dict) and isinstance(item.get("work_id"), str)
    ]
    return json.dumps({
        "assignment": "topic_source_and_template_challenge",
        "selected_topic": selected,
        "targeted_scholarly_records": works,
        "allowed_work_ids": allowed_work_ids,
        "executable_templates": templates,
        "output_contract": {
            "schema_version": SOURCE_CHALLENGE_SCHEMA_VERSION,
            "decision": "admit_to_survey or refine",
            "selected_id": "copy selected_topic.id exactly",
            "source_relevance": "integer 0 through 4",
            "template_independence": "integer 0 through 4; 0 means a template paraphrase",
            "prior_work_risk": "low, medium, or high",
            "direct_comparison_match": (
                "boolean; true only when a supplied work directly answers the selected comparison, "
                "not merely the same domain or neighboring mechanism"
            ),
            "closest_work_ids": "zero to eight IDs copied from targeted_scholarly_records",
            "rationale": "specific evidence-grounded rationale without a novelty claim",
            "required_changes": "empty when admitted; otherwise a unique JSON array of at most eight substantive scientific changes",
        },
        "admission_rule": {
            "minimum_source_relevance": 2,
            "minimum_template_independence": 3,
            "high_prior_work_risk_requires_refinement": True,
            "high_prior_work_risk_definition": (
                "Use high only when a supplied work directly answers the same comparison or the selected "
                "question is a close template paraphrase; broad domain overlap is medium."
            ),
        },
        "output_constraints": [
            "Return exactly one JSON object with exactly the ten keys in output_contract.",
            "Do not echo assignment, selected_topic, targeted_scholarly_records, executable_templates, or any other metadata.",
            "closest_work_ids must be a literal subset of allowed_work_ids; never copy a prior_work_id from selected_topic unless it is also in allowed_work_ids.",
            "If no targeted record is close enough, return an empty closest_work_ids array and decision refine.",
            "Do not set prior_work_risk to high for broad topical adjacency; high requires a supplied work that "
            "directly answers the same comparison or a close executable-template paraphrase.",
            "If direct_comparison_match is false and source_relevance is at least 2 and "
            "template_independence is at least 3, admit_to_survey with medium prior_work_risk; "
            "the full survey, not this intake gate, decides whether a literature gap exists.",
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
    "name the smallest substantive changes needed and identify which dimensions must change. A refinement must "
    "change at least two substantive dimensions and should change the research form, evidence mode, or comparison "
    "type when the weakness is structural. Copy the selected_id "
    "from the supplied topic package exactly; never invent or normalize a new identifier. Return JSON only."
)


def _maturity_review_prompt(objective, package, *, refinement_context=None,
                            require_structural_pivot=False):
    if type(require_structural_pivot) is not bool:
        raise ValidationError("require_structural_pivot must be boolean")
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
            "require_structural_pivot_on_refine": require_structural_pivot,
        },
        "output_constraints": [
            "Return exactly one JSON object with exactly the six keys in output_contract.",
            "Do not echo assignment, dimensions, score_scale, admission_rule, topic_package, or any other metadata.",
            "Keep rationale under 45 words and each required_changes item under 12 words.",
            "Use at most four required_changes items and return no markdown.",
            "Keep the complete response below 500 output tokens.",
        ],
        "identity_rule": "selected_id must equal topic_package.selected_id exactly",
    }, ensure_ascii=False, sort_keys=True)


class TopicDiscoveryRunner:
    """Generate one bounded, validated free-topic proposal."""

    def __init__(self, model, *, deadline_seconds=None):
        self.model_config = deepcopy(model)
        self._route_cursors = {}
        if (deadline_seconds is not None and
                (type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds)
                 or deadline_seconds <= 0)):
            raise ValidationError("topic discovery deadline must be finite and positive")
        self.deadline_seconds = float(deadline_seconds) if deadline_seconds is not None else None

    def _client(self, role, *, seed=None, deadline=None, sampling_overrides=None,
                max_output_tokens=None, route_index=None):
        if route_index is not None and type(route_index) is not int:
            raise ValidationError("topic model route_index must be an integer when supplied")
        role_models = self.model_config.get("role_models", {})
        role_fallbacks = self.model_config.get("role_model_fallbacks", {})
        candidates = []
        if isinstance(role_models, dict) and isinstance(role_models.get(role), dict):
            candidates.append(deepcopy(role_models[role]))
        if isinstance(role_fallbacks, dict) and isinstance(role_fallbacks.get(role), list):
            candidates.extend(deepcopy(item) for item in role_fallbacks[role]
                              if isinstance(item, dict))
        if candidates:
            cursor = (route_index % len(candidates)
                      if route_index is not None
                      else self._route_cursors.get(role, 0))
            selected = None
            selected_index = None
            for offset in range(len(candidates)):
                index = (cursor + offset) % len(candidates)
                if model_call_budget_available(candidates[index]):
                    selected, selected_index = candidates[index], index
                    break
            if selected is None:
                raise ModelCallError(
                    f"all configured topic model routes are unavailable for {role}",
                    outcome_known=True)
            # Normal bulk work rotates through its declared provider pool.
            # A caller may pin one dispatch to a specific fallback lane for a
            # repair; that must not mutate the bulk cursor or turn a fallback
            # into the next normal primary route.
            if route_index is None:
                self._route_cursors[role] = (selected_index + 1) % len(candidates)
            routed_model = deepcopy(self.model_config)
            routed_model["role_models"] = {role: selected}
            routed_model["role_model_fallbacks"] = {}
            overrides = dict(sampling_overrides or {})
            if seed is not None:
                overrides["seed"] = seed
            config = resolve_model_config(
                routed_model, role=role,
                overrides=overrides or None,
            )
        else:
            overrides = dict(sampling_overrides or {})
            if seed is not None:
                overrides["seed"] = seed
            config = resolve_model_config(
                self.model_config, role=role,
                overrides=overrides or None,
            )
        if type(max_output_tokens) is int and max_output_tokens > 0:
            configured_output = config.get("max_output_tokens")
            config["max_output_tokens"] = min(configured_output, max_output_tokens) \
                if type(configured_output) is int else max_output_tokens
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

    def _repair_missing_topic_fields(self, package, *, deadline, budget):
        """Fill omitted required text fields while keeping all other fields immutable."""
        if not isinstance(package, dict) or not isinstance(package.get("candidates"), list):
            return []
        targets = [
            {
                "candidate_index": index,
                "id": candidate.get("id"),
                "candidate": candidate,
                "missing_fields": [
                    field for field in _TOPIC_REPAIRABLE_TEXT_FIELDS
                    if field not in candidate
                ],
            }
            for index, candidate in enumerate(package["candidates"])
            if isinstance(candidate, dict)
            and any(field not in candidate for field in _TOPIC_REPAIRABLE_TEXT_FIELDS)
        ]
        if not targets:
            return []
        client = self._client(
            "topic_discovery",
            deadline=deadline,
            sampling_overrides={"temperature": 0.2, "top_p": 0.85, "presence_penalty": 0.0},
            max_output_tokens=2400,
        )
        prompt = _topic_missing_field_repair_prompt(package, targets)
        budget.before_model_call(
            "topic_discovery", getattr(client, "model", None),
            system=TOPIC_FIELD_REPAIR_SYSTEM, prompt=prompt)
        try:
            result = client.complete(system=TOPIC_FIELD_REPAIR_SYSTEM, prompt=prompt)
        except ModelCallError as exc:
            budget.record_model_error(exc)
            raise ValidationError(f"topic field repair model call failed: {exc}") from exc
        budget.record_model_result(result)
        if result.finish_reason != "stop":
            error = ValidationError(
                f"topic field repair did not finish normally: {result.finish_reason}")
            budget.record_validation_error(error)
            raise error
        try:
            patch = result.json_object()
            if not isinstance(patch, dict) or set(patch) != {"candidate_patches"}:
                raise ValidationError(
                    "topic field repair requires exactly ['candidate_patches']")
            patches = patch["candidate_patches"]
            target_ids = [candidate["id"] for candidate in targets]
            if (not isinstance(patches, list) or len(patches) != len(targets)
                    or len({item.get("id") for item in patches if isinstance(item, dict)})
                    != len(targets)):
                raise ValidationError(
                    "topic field repair must return one unique patch per target")
            by_id = {}
            for item in patches:
                if not isinstance(item, dict) or set(item) != {"id", "fields"}:
                    raise ValidationError(
                        "topic field repair patch has an invalid shape")
                identifier = item["id"]
                if identifier not in target_ids:
                    raise ValidationError(
                        "topic field repair returned an unknown candidate id")
                fields = item["fields"]
                target = next(target for target in targets if target["id"] == identifier)
                expected_fields = set(target["missing_fields"])
                if not isinstance(fields, dict) or set(fields) != expected_fields:
                    raise ValidationError(
                        "topic field repair returned fields outside the missing-field contract")
                for field, value in fields.items():
                    _text(value, f"topic candidate {field}")
                by_id[identifier] = fields
            if set(by_id) != set(target_ids):
                raise ValidationError(
                    "topic field repair did not cover every target candidate")
            repairs = []
            for candidate in targets:
                identifier = candidate["id"]
                for field, value in by_id[identifier].items():
                    candidate["candidate"][field] = value
                    repairs.append({
                        "candidate_id": identifier,
                        "field": field,
                        "source": "targeted_model_field_repair",
                    })
            return repairs
        except (TypeError, AttributeError, KeyError) as exc:
            error = ValidationError(
                f"topic field repair could not be parsed: {exc}")
            budget.record_validation_error(error)
            raise error from exc
        except ValidationError as exc:
            budget.record_validation_error(exc)
            raise

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
            prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if budget is not None:
                budget.before_model_call(
                    "research.frontier-seed-planner", getattr(client, "model", None),
                    system=FRONTIER_SYSTEM, prompt=prompt)
            try:
                result = client.complete(
                    system=FRONTIER_SYSTEM,
                    prompt=prompt,
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
                plan = _anchor_frontier_seed_queries(result.json_object())
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
        previous = None
        last_error = None
        allowed_work_ids = [
            item["work_id"] for item in works
            if isinstance(item, dict) and isinstance(item.get("work_id"), str)
        ]
        for repair_attempt in range(3):
            reviewer = self._client(
                "research.topic-source-challenger",
                seed=(seed + repair_attempt) % MAX_PROVIDER_SEED,
                deadline=deadline,
            )
            payload = json.loads(_source_challenge_prompt(selected, works, runtime_context))
            if previous is not None and last_error is not None:
                payload["assignment"] = "repair_topic_source_challenge"
                payload["previous_response"] = previous[:12000]
                payload["validation_error"] = str(last_error)
                payload["allowed_work_ids"] = allowed_work_ids
                payload["repair_instruction"] = (
                    "Return a complete replacement challenge object. Keep the same selected_id and supplied "
                    "work IDs, but repair the response against validation_error. Include direct_comparison_match. "
                    "required_changes must be a "
                    "unique JSON array of at most eight substantive strings. closest_work_ids must be copied "
                    "literally from allowed_work_ids and may not contain any other ID."
                )
            prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            if budget is not None:
                budget.before_model_call(
                    "research.topic-source-challenger", getattr(reviewer, "model", None),
                    system=SOURCE_CHALLENGE_SYSTEM, prompt=prompt)
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
                # The challenge rationale is reader-facing scientific prose.
                # Normalize known internal terms before the surface contract
                # rejects an otherwise usable independent assessment.
                for key in ("rationale", "required_changes"):
                    if key == "rationale" and isinstance(review.get(key), str):
                        review[key] = project_internal_language(review[key])
                    elif key == "required_changes" and isinstance(review.get(key), list):
                        review[key] = [project_internal_language(item)
                                       if isinstance(item, str) else item
                                       for item in review[key]]
                validate_source_challenge(
                    review, selected_id=selected["id"],
                    work_ids=[item["work_id"] for item in works],
                )
                if (
                    review.get("direct_comparison_match") is False
                    and review["source_relevance"] >= 2
                    and review["template_independence"] >= 3
                    and review["closest_work_ids"]
                ):
                    review["decision"] = "admit_to_survey"
                    review["prior_work_risk"] = "medium"
                    review["required_changes"] = []
                    review["risk_calibration"] = (
                        "Broad topical overlap was separated from a direct answer to the same comparison; "
                        "full-survey novelty review remains required."
                    )
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
            refinement_context=None, budgets=None, specialist_reports=None):
        _text(objective, "topic objective", public=False)
        if type(candidate_count) is not int or not 3 <= candidate_count <= 8:
            raise ValidationError("topic discovery candidate_count must be between 3 and 8")
        if repair_mode not in {"bounded", "until_deadline"}:
            raise ValidationError("topic discovery repair_mode must be bounded or until_deadline")
        if repair_mode == "until_deadline" and self.deadline_seconds is None:
            raise ValidationError("topic discovery until_deadline mode requires a stage deadline")
        if (type(max_attempts) is not int
                or not 1 <= max_attempts <= MAX_BOUNDED_TOPIC_ATTEMPTS):
            raise ValidationError(
                "topic discovery max_attempts must be between 1 and "
                f"{MAX_BOUNDED_TOPIC_ATTEMPTS}")
        if type(maturity_review_rounds) is not int or not 0 <= maturity_review_rounds <= 4:
            raise ValidationError("topic discovery maturity_review_rounds must be between 0 and 4")
        if refinement_context is not None and not isinstance(refinement_context, dict):
            raise ValidationError("topic discovery refinement_context must be an object when supplied")
        if specialist_reports is not None and not isinstance(specialist_reports, list):
            raise ValidationError("topic discovery specialist_reports must be a list when supplied")
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
        candidate_frontier_seeds = _grounding_eligible_frontier_seeds(
            (frontier_seed_plan or {}).get("seeds", []),
            recent_papers,
            candidate_count,
        )
        previous = None
        last_error = None
        refinement_feedback = None
        source_refinement_count = 0
        refinement_parent = (
            deepcopy(refinement_context.get("parent_topic"))
            if isinstance(refinement_context, dict)
            and isinstance(refinement_context.get("parent_topic"), dict)
            else None
        )
        refinement_round = 0
        maturity_reviews = []
        maturity_review_history = []
        candidate_attempt_trace = []
        rejected_topic_history = []
        refinement_base_package = None
        refinement_parent_index = None
        attempts = itertools.count() if repair_mode == "until_deadline" else range(max_attempts)
        catalog_ids = {
            item.get("id") for item in (runtime_context or {}).get("experiment_catalog", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        prompt_runtime_context = runtime_context
        if specialist_reports:
            prompt_runtime_context = deepcopy(runtime_context or {})
            prompt_runtime_context["independent_specialist_reports"] = deepcopy(specialist_reports[:16])
        coverage_plan = _capability_coverage_plan(
            runtime_context, candidate_count, sampling_seed)
        if coverage_plan:
            # Preserve independent specialist feedback when the capability
            # planner is also active. Replacing this projection with the raw
            # runtime context silently dropped review findings on refinement
            # passes and made the next topic generation repeat the rejected
            # direction.
            prompt_runtime_context = deepcopy(prompt_runtime_context or runtime_context or {})
            prompt_runtime_context["candidate_capability_plan"] = [
                {"candidate_index": index, "experiment_capability_id": capability_id}
                for index, capability_id in enumerate(coverage_plan)
            ]
        for attempt in attempts:
            generation_seed = (sampling_seed + attempt) % MAX_PROVIDER_SEED if sampling_seed is not None else None
            # Preserve temperature for the first portfolio proposal so the
            # frontier remains diverse.  Once a source or maturity gate has
            # supplied repair instructions, use a deterministic sampling
            # profile that is less likely to emit an extra field or partial
            # JSON package.  This only changes the repair turn, not the
            # scientific selection criteria or its validation gates.
            repair_sampling = None
            if refinement_feedback is not None or previous is not None:
                repair_sampling = {
                    "temperature": 0.35,
                    "top_p": 0.9,
                    "presence_penalty": 0.0,
                }
            preferred_route_index = None
            single_candidate_refinement = False
            if (refinement_feedback is not None
                    and refinement_base_package is not None
                    and refinement_parent is not None):
                target_shape = _refinement_target_shape(
                    refinement_base_package,
                    refinement_parent.get("id"),
                    runtime_context,
                    seed=((generation_seed + 7919) % MAX_PROVIDER_SEED
                          if generation_seed is not None else None),
                )
                target_seed = _refinement_target_seed(
                    candidate_frontier_seeds,
                    recent_papers,
                    refinement_parent.get("frontier_seed_id"),
                    refinement_feedback,
                    sampling_seed=((generation_seed + 104729) % MAX_PROVIDER_SEED
                                   if generation_seed is not None else None),
                    occupied_seed_ids={
                        item.get("frontier_seed_id")
                        for item in (refinement_base_package.get("candidates") or [])
                        if isinstance(item, dict)
                        and item.get("id") != refinement_parent.get("id")
                    },
                )
                if target_shape is not None and target_seed is not None:
                    single_candidate_refinement = True
                    refinement_payload = json.loads(
                        _topic_candidate_refinement_prompt(
                            objective,
                            refinement_parent,
                            base_package=refinement_base_package,
                            target_shape=target_shape,
                            target_seed=target_seed,
                            frontier_seeds=candidate_frontier_seeds,
                            recent_papers=recent_papers,
                            runtime_context=prompt_runtime_context,
                            refinement_feedback=refinement_feedback,
                        )
                    )
                    if previous is not None and last_error is not None:
                        refinement_payload["previous_response"] = previous[:12000]
                        refinement_payload["validation_error"] = str(last_error)
                        refinement_payload["repair_instruction"] = (
                            "Return one complete replacement candidate object that satisfies "
                            "output_contract. Keep the exact candidate id, required shape, target "
                            "seed, and allowed work IDs; repair only validation_error."
                        )
                    prompt = json.dumps(
                        refinement_payload, ensure_ascii=False, sort_keys=True)
            if refinement_feedback is not None and not single_candidate_refinement:
                # Refinement must keep the same package contract as the first
                # generation.  A looser prompt here makes a capable model
                # return a convenient wrapper such as ``selected_candidate``
                # that cannot cross the validation boundary.
                refinement_payload = json.loads(topic_prompt(
                    objective, candidate_count,
                    recent_papers=recent_papers,
                    frontier_seeds=candidate_frontier_seeds,
                    runtime_context=prompt_runtime_context,
                    refinement_context={
                        **(refinement_context or {}),
                        "parent_topic": refinement_parent,
                        "refinement_feedback": refinement_feedback,
                    },
                    candidate_history=candidate_attempt_trace[-24:],
                    rejected_candidate_directions=rejected_topic_history,
                    portfolio_seed=sampling_seed))
                refinement_payload["assignment"] = "refine_topic_discovery"
                refinement_payload["refinement_instruction"] = (
                    "Return the complete package described by output_contract. Preserve useful evidence from "
                    "the parent, but change at least two substantive dimensions, including research_form, "
                    "evidence_mode, or comparison_type. The selected direction must be an orthogonal pivot, "
                    "not a cosmetic rewrite."
                )
                if isinstance(refinement_feedback, dict) and refinement_feedback.get(
                        "require_frontier_seed_pivot") is True:
                    refinement_payload["refinement_instruction"] += (
                        " Because the source challenge found weak grounding and high prior-work risk, "
                        "the selected candidate must use a different frontier_seed_id from the parent and "
                        "cite a supplied work from that new seed; do not repair the rejected seed in place."
                    )
                if isinstance(refinement_feedback, dict):
                    refinement_payload["repair_specification"] = {
                        key: deepcopy(refinement_feedback[key])
                        for key in ("review_type", "decision", "rationale", "required_changes",
                                    "changed_dimensions")
                        if key in refinement_feedback
                    }
                    if refinement_payload["repair_specification"].get("required_changes"):
                        refinement_payload["refinement_instruction"] += (
                            " Address every required_changes item explicitly in the selected candidate. "
                            "For any request for quantitative executability, include an operational variable "
                            "or estimand, a scaling/equation or defined calculation, a bounded range/baseline, "
                            "and a disconfirmation criterion in the candidate fields."
                        )
                if previous is not None and last_error is not None:
                    refinement_payload["previous_response"] = previous[:40000]
                    refinement_payload["validation_error"] = str(last_error)
                    refinement_payload["refinement_instruction"] += (
                        " Repair the previous response against validation_error. In particular, include every "
                        "required experiment_design field for design-driven candidates and omit experiment_design "
                        "from all other capabilities. Do not repeat the failed selected candidate: change at "
                        "least two actual candidate fields, including one of research_form, evidence_mode, or "
                        "comparison_type, and make the prose consistent with the changed shape."
                    )
                prompt = json.dumps(refinement_payload, ensure_ascii=False, sort_keys=True)
            elif not single_candidate_refinement:
                prompt = topic_prompt(objective, candidate_count,
                    recent_papers=recent_papers,
                    frontier_seeds=candidate_frontier_seeds,
                    runtime_context=prompt_runtime_context,
                    refinement_context=refinement_context,
                    candidate_history=candidate_attempt_trace[-24:],
                    rejected_candidate_directions=rejected_topic_history,
                    portfolio_seed=sampling_seed)
            if previous is not None and refinement_feedback is None:
                # Invalid-output repair also uses the full contract so repair
                # cannot drift into a different response shape.
                repair_payload = json.loads(topic_prompt(
                    objective, candidate_count,
                    recent_papers=recent_papers,
                    frontier_seeds=candidate_frontier_seeds,
                    runtime_context=prompt_runtime_context,
                    refinement_context=refinement_context,
                    candidate_history=candidate_attempt_trace[-24:],
                    rejected_candidate_directions=rejected_topic_history,
                    portfolio_seed=sampling_seed))
                repair_payload["assignment"] = "repair_invalid_topic_discovery"
                repair_payload["candidate_response"] = previous[:40000]
                repair_payload["validation_error"] = str(last_error)
                repair_payload["repair_instruction"] = (
                    "Return a complete package satisfying output_contract. Preserve valid candidates and repair "
                    "only the reported violations; do not return a wrapper object or a partial candidate list."
                )
                if (last_error is not None
                        and str(last_error).startswith("topic candidate portfolio")):
                    observed_profile = (
                        candidate_attempt_trace[-1].get("portfolio_profile")
                        if candidate_attempt_trace
                        and isinstance(candidate_attempt_trace[-1], dict)
                        else None
                    )
                    repair_payload["portfolio_repair"] = {
                        "required_shape_slots": deepcopy(
                            repair_payload.get("portfolio_shape_plan", [])),
                        "observed_profile": deepcopy(observed_profile),
                        "instruction": (
                            "Rewrite the candidate objects in list order so each candidate receives exactly "
                            "the research_form, evidence_mode, and comparison_type from its corresponding "
                            "required_shape_slots entry. Keep each candidate's scientific prose coherent "
                            "with its assigned shape; do not merely relabel a mechanism or measurement."
                        ),
                    }
                    repair_payload["repair_instruction"] += (
                        " This is a portfolio-shape failure. Use portfolio_repair.required_shape_slots "
                        "literally in candidate list order, and rewrite any affected candidate prose so the "
                        "research form, evidence mode, and comparison type are scientifically consistent."
                    )
                prompt = json.dumps(repair_payload, ensure_ascii=False, sort_keys=True)
            if single_candidate_refinement:
                # A reviewer-directed repair gets the strongest declared bulk
                # lane. Fresh proposals continue to rotate through Qwen,
                # Gemma, and DeepSeek according to the configured cursor.
                preferred_route_index = -1
            client = self._client(
                "topic_discovery", seed=generation_seed, deadline=deadline,
                sampling_overrides=repair_sampling,
                route_index=preferred_route_index)
            budget.before_model_call(
                "topic_discovery", getattr(client, "model", None),
                system=SYSTEM, prompt=prompt)
            try:
                result = client.complete(system=SYSTEM, prompt=prompt)
            except ModelCallError as exc:
                budget.record_model_error(exc)
                last_error = ValidationError(f"topic discovery model call failed: {exc}")
                outcome_known = bool(getattr(exc, "outcome_known", False))
                candidate_attempt_trace.append(_candidate_attempt_record(
                    {}, attempt=attempt + 1,
                    status="model_error" if outcome_known else "result_unknown",
                    error=str(last_error), outcome_known=outcome_known))
                continue
            budget.record_model_result(result)
            previous = result.text
            if result.finish_reason != "stop":
                last_error = ValidationError(f"topic discovery did not finish normally: {result.finish_reason}")
                budget.record_validation_error(last_error)
                candidate_attempt_trace.append(_candidate_attempt_record(
                    {}, attempt=attempt + 1, status="incomplete", error=str(last_error),
                    outcome_known=True))
                continue
            attempt_record = None
            try:
                parsed_package = result.json_object()
                if single_candidate_refinement:
                    if (not isinstance(parsed_package, dict)
                            or set(parsed_package) != {"candidate"}
                            or not isinstance(parsed_package.get("candidate"), dict)):
                        raise ValidationError(
                            "selected topic repair requires exactly one candidate object")
                    replacement = parsed_package["candidate"]
                    if replacement.get("id") != refinement_parent.get("id"):
                        raise ValidationError(
                            "selected topic repair must preserve the candidate id")
                    package = deepcopy(refinement_base_package)
                    replacement_index = refinement_parent_index
                    if type(replacement_index) is not int:
                        replacement_index = next(
                            index for index, item in enumerate(package["candidates"])
                            if isinstance(item, dict)
                            and item.get("id") == refinement_parent.get("id")
                        )
                    package["candidates"][replacement_index] = replacement
                    package["selected_id"] = replacement["id"]
                    package["selection_rationale"] = (
                        "The selected direction is retained after comparing its evidence, "
                        "testability, and disconfirmation risk."
                    )
                else:
                    package = parsed_package
                alias_repairs = _repair_known_candidate_field_aliases(package)
                grounding_repairs = _materialize_seed_bindings(
                    package,
                    (frontier_seed_plan or {}).get("seeds", []),
                    recent_papers,
                    rejected_frontier_seed_ids=(
                        refinement_feedback.get("rejected_frontier_seed_ids", [])
                        if isinstance(refinement_feedback, dict)
                        and refinement_feedback.get("require_frontier_seed_pivot") is True
                        else []
                    ),
                )
                missing_field_repairs = self._repair_missing_topic_fields(
                    package, deadline=deadline, budget=budget)
                objective_normalized = (
                    isinstance(package, dict)
                    and isinstance(package.get("objective"), str)
                    and package.get("objective") != objective
                )
                package = _materialize_topic_objective(package, objective)
                domain_repairs = _materialize_seed_domains(
                    package, (frontier_seed_plan or {}).get("seeds", []))
                feasibility_repairs = _materialize_foundry_feasibility(
                    package, runtime_context)
                package = _materialize_foundry_capability_requirements(
                    package, runtime_context)
                query_anchor_repairs = _anchor_topic_candidate_queries(
                    package, frontier_seeds=(frontier_seed_plan or {}).get("seeds", []))
                attempt_record = _candidate_attempt_record(
                    package, attempt=attempt + 1, outcome_known=True)
                if objective_normalized:
                    attempt_record["objective_normalized"] = True
                if query_anchor_repairs:
                    attempt_record["search_query_anchor_repairs"] = query_anchor_repairs
                if domain_repairs:
                    attempt_record["derived_field_repairs"] = domain_repairs
                if grounding_repairs:
                    attempt_record.setdefault("derived_field_repairs", []).extend(
                        grounding_repairs)
                if feasibility_repairs:
                    attempt_record.setdefault("derived_field_repairs", []).extend(
                        feasibility_repairs)
                if missing_field_repairs:
                    attempt_record.setdefault("derived_field_repairs", []).extend(
                        missing_field_repairs)
                if alias_repairs:
                    attempt_record.setdefault("derived_field_repairs", []).extend(
                        alias_repairs)
                candidate_attempt_trace.append(attempt_record)
                validation_history = _topic_validation_history(
                    (runtime_context or {}).get("topic_history"),
                    rejected_topic_history)
                novelty_selection_repair = None
                if not single_candidate_refinement:
                    novelty_selection_repair = _repair_topic_novelty_selection(
                        package,
                        validation_history,
                        excluded_topic_ids=(runtime_context or {}).get(
                            "topic_exclusions", {}).get("topic_ids", []),
                        excluded_capability_ids=(runtime_context or {}).get(
                            "topic_exclusions", {}).get("capability_ids", []),
                    )
                if novelty_selection_repair is not None:
                    attempt_record["novelty_selection_repair"] = novelty_selection_repair
                    replacement = next(
                        candidate for candidate in package["candidates"]
                        if candidate.get("id") == package["selected_id"]
                    )
                    attempt_record["selected_id"] = package["selected_id"]
                    attempt_record["selected_topic"] = {
                        key: replacement.get(key) for key in (
                            "id", "title", "domain", "research_question", "research_form",
                            "evidence_mode", "comparison_type", "experiment_capability_id")
                    }
                validate_topic_package(
                    package, objective=objective, candidate_count=candidate_count,
                    experiment_capability_ids=catalog_ids,
                    require_capability_coverage=bool(catalog_ids),
                    excluded_capability_ids=(runtime_context or {}).get("topic_exclusions", {}).get("capability_ids", []),
                    excluded_topic_ids=(runtime_context or {}).get("topic_exclusions", {}).get("topic_ids", []),
                    topic_history=validation_history,
                    design_driven_capability_ids=_design_driven_ids(runtime_context),
                    frontier_seeds=(frontier_seed_plan or {}).get("seeds", []),
                    recent_papers=recent_papers,
                    require_grounding=frontier_seed_plan is not None,
                    fallback_templates=(runtime_context or {}).get(
                        "fallback_experiment_catalog", []),
                    enforce_portfolio_diversity=frontier_seed_plan is not None)
                selection_repair = _repair_foundry_selection(package, runtime_context)
                if selection_repair is not None:
                    attempt_record["selection_repair"] = selection_repair
                refinement_selection_repair = None
                if frontier_seed_plan is not None and refinement_parent is not None:
                    refinement_selection_repair = _repair_topic_refinement_selection(
                        package, refinement_parent, runtime_context,
                        require_frontier_seed_pivot=(
                            isinstance(refinement_feedback, dict)
                            and refinement_feedback.get("require_frontier_seed_pivot") is True
                        ),
                        rejected_frontier_seed_ids=(
                            refinement_feedback.get("rejected_frontier_seed_ids", [])
                            if isinstance(refinement_feedback, dict) else []
                        ))
                if refinement_selection_repair is not None:
                    attempt_record["refinement_selection_repair"] = refinement_selection_repair
                if selection_repair is not None or refinement_selection_repair is not None:
                    # Selection repairs mutate only selected_id and the
                    # rationale, but the final choice must cross every same
                    # package gate again before it is sent to literature or
                    # maturity review.
                    validate_topic_package(
                        package, objective=objective, candidate_count=candidate_count,
                        experiment_capability_ids=catalog_ids,
                        require_capability_coverage=bool(catalog_ids),
                        excluded_capability_ids=(runtime_context or {}).get("topic_exclusions", {}).get("capability_ids", []),
                        excluded_topic_ids=(runtime_context or {}).get("topic_exclusions", {}).get("topic_ids", []),
                        topic_history=validation_history,
                        design_driven_capability_ids=_design_driven_ids(runtime_context),
                        frontier_seeds=(frontier_seed_plan or {}).get("seeds", []),
                        recent_papers=recent_papers,
                        require_grounding=frontier_seed_plan is not None,
                        fallback_templates=(runtime_context or {}).get(
                            "fallback_experiment_catalog", []),
                        enforce_portfolio_diversity=frontier_seed_plan is not None)
                    # Re-resolve the object after a selection mutation.  The
                    # pre-repair local reference may still point at the old
                    # candidate and would otherwise make a valid structural
                    # pivot fail against stale shape fields.
                    selected = next(
                        item for item in package["candidates"]
                        if item["id"] == package["selected_id"])
                    feasibility = validate_topic_feasibility(package, runtime_context)
                if runtime_context is not None:
                    feasibility = validate_topic_feasibility(package, runtime_context)
                    if feasibility["status"] == "legacy_unchecked":
                        raise ValidationError(
                            "current topic discovery output must include capability_requirements")
            except ValidationError as exc:
                budget.record_validation_error(exc)
                last_error = exc
                if attempt_record is None:
                    candidate_attempt_trace.append(_candidate_attempt_record(
                        {}, attempt=attempt + 1, status="rejected", error=str(exc),
                        outcome_known=True))
                else:
                    attempt_record["status"] = "rejected"
                    attempt_record["error"] = str(exc)[:2048]
                continue
            selected = next(item for item in package["candidates"] if item["id"] == package["selected_id"])
            feasibility = validate_topic_feasibility(package, runtime_context)
            portfolio_profile = (
                topic_portfolio_profile(package["candidates"])
                if frontier_seed_plan is not None else None
            )
            try:
                refinement_changed_dimensions = (
                    validate_topic_refinement(
                        refinement_parent, selected,
                        require_structural_pivot=frontier_seed_plan is not None,
                        require_frontier_seed_pivot=(
                            isinstance(refinement_feedback, dict)
                            and refinement_feedback.get("require_frontier_seed_pivot") is True
                        ))
                    if refinement_parent is not None else []
                )
            except ValidationError as exc:
                budget.record_validation_error(exc)
                last_error = exc
                attempt_record["status"] = "refinement_rejected"
                attempt_record["error"] = str(exc)[:2048]
                # Keep the rejected package for the next refinement turn.
                # Clearing it forced the model to regenerate from the same
                # feedback without seeing the concrete fields that failed,
                # which repeatedly produced another one-dimension rewrite.
                # The next prompt now contains the exact response and error
                # while retaining the same portfolio shape plan.
                continue
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
                            # The broad frontier sample already supplied the
                            # candidate's grounding records. Two targeted
                            # queries are enough for an independent challenge
                            # and leave request budget for a bounded repair.
                            "search_queries": selected["search_queries"][:2],
                        }],
                    }
                    targeted_prior_work, _, candidate_sampling_trace = self._recent_paper_sample(
                        objective, bibliography=bibliography, deadline=deadline,
                        sampling_seed=(generation_seed + 32452843) % MAX_PROVIDER_SEED,
                        frontier_seed_plan=targeted, minimum_seed_groups=1, budget=budget)
                    candidate_prior_work = _merge_candidate_source_records(
                        selected, recent_papers, targeted_prior_work)
                    source_challenge = self._challenge_selected_topic(
                        selected, candidate_prior_work, runtime_context,
                        seed=(generation_seed + 49979687) % MAX_PROVIDER_SEED,
                        deadline=deadline, usage=usage, budget=budget)
                    attempt_record["source_challenge"] = deepcopy(source_challenge)
                except ProviderCooldownError:
                    raise
                except ValidationError as exc:
                    budget.record_validation_error(exc)
                    last_error = exc
                    attempt_record["status"] = "source_challenge_error"
                    attempt_record["error"] = str(exc)[:2048]
                    continue
                if not _source_challenge_admitted(source_challenge):
                    last_error = ValidationError(
                        "topic source challenge requires substantive refinement: "
                        + source_challenge["rationale"])
                    budget.record_validation_error(last_error)
                    attempt_record["status"] = "source_challenge_refine"
                    attempt_record["error"] = str(last_error)[:2048]
                    # The challenger is an independent gate, but its result
                    # is also the most useful repair specification.  Carry it
                    # into the next bounded generation so the model changes
                    # the rejected mechanism/boundary instead of restarting
                    # from an uninformed random proposal.
                    source_refinement_count += 1
                    rejected_frontier_seed_ids = set()
                    if isinstance(refinement_feedback, dict):
                        rejected_frontier_seed_ids.update(
                            value for value in refinement_feedback.get(
                                "rejected_frontier_seed_ids", [])
                            if isinstance(value, str) and value.strip()
                        )
                    selected_seed_id = selected.get("frontier_seed_id")
                    if isinstance(selected_seed_id, str) and selected_seed_id.strip():
                        rejected_frontier_seed_ids.add(selected_seed_id)
                    force_seed_pivot = (
                        source_refinement_count >= 2
                        or source_challenge.get("prior_work_risk") == "high"
                        or _source_challenge_requires_frontier_seed_pivot(source_challenge)
                    )
                    refinement_parent_index = next(
                        index for index, item in enumerate(package["candidates"])
                        if item["id"] == selected["id"]
                    )
                    refinement_feedback = {
                        "review_type": "source_challenge",
                        **deepcopy(source_challenge),
                        "parent_candidate_index": refinement_parent_index,
                        "rejected_frontier_seed_ids": sorted(rejected_frontier_seed_ids),
                    }
                    if force_seed_pivot:
                        refinement_feedback["require_frontier_seed_pivot"] = True
                    refinement_parent = deepcopy(selected)
                    refinement_base_package = deepcopy(package)
                    previous = None
                    continue
            if maturity_review_rounds:
                review_seed = ((generation_seed if generation_seed is not None else 0)
                               + 104729 * (attempt + 1)) % MAX_PROVIDER_SEED
                review = None
                review_previous = None
                review_error = None
                for review_attempt in range(2):
                    reviewer = self._client(
                        "research.topic-maturity-reviewer",
                        seed=(review_seed + review_attempt) % MAX_PROVIDER_SEED,
                        deadline=deadline,
                        # The primary DeepSeek route is used first; GLM is a
                        # genuine fallback for an incomplete or invalid
                        # review, not an alternating second reviewer.
                        route_index=review_attempt,
                        sampling_overrides=(
                            {"temperature": 0.1, "top_p": 0.85}
                            if review_attempt else None
                        ),
                        # The maturity contract is six compact JSON fields.
                        # A large cap made the flash reviewer spend its
                        # response on prose and terminate at length, forcing
                        # an avoidable second call on every candidate.
                        max_output_tokens=900,
                    )
                    maturity_payload = json.loads(_maturity_review_prompt(
                        objective, package, refinement_context=refinement_context,
                        require_structural_pivot=frontier_seed_plan is not None))
                    if review_previous is not None and review_error is not None:
                        maturity_payload["assignment"] = "repair_topic_maturity_review"
                        maturity_payload["previous_response"] = review_previous[:12000]
                        maturity_payload["validation_error"] = str(review_error)
                        maturity_payload["repair_instruction"] = (
                            "Return one complete replacement JSON object that satisfies output_contract. "
                            "Do not include commentary, markdown, or an incomplete object. Copy the exact "
                            "selected_id from selected_id_to_copy_exactly and keep rationale concise."
                        )
                    maturity_prompt = json.dumps(
                        maturity_payload, ensure_ascii=False, sort_keys=True)
                    budget.before_model_call(
                        "research.topic-maturity-reviewer", getattr(reviewer, "model", None),
                        system=MATURITY_SYSTEM, prompt=maturity_prompt)
                    try:
                        review_result = reviewer.complete(
                            system=MATURITY_SYSTEM,
                            prompt=maturity_prompt,
                        )
                    except ModelCallError as exc:
                        budget.record_model_error(exc)
                        review_error = ValidationError(
                            f"topic maturity review model call failed: {exc}")
                        break
                    budget.record_model_result(review_result)
                    review_previous = review_result.text
                    if review_result.finish_reason != "stop":
                        review_error = ValidationError(
                            f"topic maturity review did not finish normally: {review_result.finish_reason}")
                        budget.record_validation_error(review_error)
                        continue
                    try:
                        parsed_review = review_result.json_object()
                        validate_topic_maturity_review(
                            parsed_review,
                            candidate_ids=[item["id"] for item in package["candidates"]],
                            require_structural_pivot=frontier_seed_plan is not None)
                        if parsed_review["selected_id"] != package["selected_id"]:
                            raise ValidationError(
                                "topic maturity review must assess the package's selected_id")
                        review = parsed_review
                        break
                    except ValidationError as exc:
                        review_error = exc
                        budget.record_validation_error(exc)
                        continue
                if review is None:
                    last_error = review_error or ValidationError(
                        "topic maturity review did not produce a valid review")
                    attempt_record["status"] = "maturity_review_error"
                    attempt_record["error"] = str(last_error)[:2048]
                    continue
                maturity_review_history.append({
                    "attempt": attempt,
                    "selected_id": package["selected_id"],
                    "topic_title": selected["title"],
                    "topic_research_question": selected["research_question"],
                    "review": deepcopy(review),
                })
                attempt_record["maturity_review"] = deepcopy(review)
                maturity_reviews.append(review)
                if topic_maturity_admitted(review):
                    attempt_record["status"] = "admitted"
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
                        "portfolio_profile": deepcopy(portfolio_profile),
                        "candidate_attempt_trace": deepcopy(candidate_attempt_trace),
                        "rejected_topic_history": deepcopy(rejected_topic_history),
                        "usage": usage,
                        "budget": budget.snapshot(),
                    }
                    if refinement_context:
                        output["topic_evolution"] = {
                            "mode": "refinement",
                            "cycle": refinement_context.get("cycle"),
                            "parent_topic_id": refinement_context.get("parent_topic_id"),
                            "changed_dimensions": sorted(set(
                                evolution_dimensions + refinement_changed_dimensions
                            )) or refinement_context.get("changed_dimensions", []),
                            "reason": refinement_context.get("reason"),
                        }
                    return output
                if refinement_round >= maturity_review_rounds:
                    last_error = ValidationError(
                        "topic maturity review requires substantive refinement: "
                        + review["rationale"])
                    attempt_record["status"] = "maturity_rejected"
                    attempt_record["error"] = str(last_error)[:2048]
                    rejected_topic_history.append({
                        "topic_id": selected.get("id"),
                        "title": selected.get("title"),
                        "domain": selected.get("domain"),
                        "research_question": selected.get("research_question"),
                        "research_form": selected.get("research_form"),
                        "evidence_mode": selected.get("evidence_mode"),
                        "comparison_type": selected.get("comparison_type"),
                        "signature": topic_signature(selected),
                        "rejection_type": "maturity",
                        "rejection_reason": review.get("rationale"),
                        "required_changes": deepcopy(review.get("required_changes", [])),
                        "changed_dimensions": deepcopy(review.get("changed_dimensions", [])),
                    })
                    # A portfolio that remains thin after its allowed
                    # refinement passes is abandoned as a whole.  The next
                    # attempt starts a fresh exploration seed rather than
                    # polishing the same weak direction until the deadline.
                    refinement_feedback = None
                    refinement_parent = None
                    refinement_base_package = None
                    refinement_parent_index = None
                    refinement_round = 0
                    source_refinement_count = 0
                    maturity_reviews = []
                    previous = None
                    continue
                refinement_round += 1
                attempt_record["status"] = "maturity_refine"
                attempt_record["error"] = (
                    "topic maturity review requires substantive refinement: "
                    + review["rationale"]
                )[:2048]
                refinement_feedback = review
                refinement_parent_index = next(
                    index for index, item in enumerate(package["candidates"])
                    if item["id"] == selected["id"]
                )
                refinement_parent = deepcopy(selected)
                refinement_feedback["parent_candidate_index"] = refinement_parent_index
                refinement_base_package = deepcopy(package)
                previous = None
                continue
            attempt_record["status"] = "admitted"
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
                "portfolio_profile": deepcopy(portfolio_profile),
                "candidate_attempt_trace": deepcopy(candidate_attempt_trace),
                "rejected_topic_history": deepcopy(rejected_topic_history),
                "usage": usage,
                "budget": budget.snapshot(),
            }
            if refinement_context:
                output["topic_evolution"] = {
                    "mode": "refinement",
                    "cycle": refinement_context.get("cycle"),
                    "parent_topic_id": refinement_context.get("parent_topic_id"),
                    "changed_dimensions": refinement_changed_dimensions or refinement_context.get(
                        "changed_dimensions", []),
                    "reason": refinement_context.get("reason"),
                }
            return output
        error = last_error or ValidationError("topic discovery did not produce a valid package")
        snapshot = budget.snapshot()
        # Some package gates reject a selected direction for similarity or
        # structural validity before the maturity reviewer can emit its
        # normal rejection object. Preserve every concrete selected direction
        # that is not already in the rejection list so the next Composer pivot
        # has durable exclusion targets instead of only an opaque error string.
        rejected_ids = {
            item.get("topic_id") for item in rejected_topic_history
            if isinstance(item, dict) and isinstance(item.get("topic_id"), str)
        }
        for trace in reversed(candidate_attempt_trace):
            selected = trace.get("selected_topic") if isinstance(trace, dict) else None
            if (not isinstance(selected, dict)
                    or not isinstance(selected.get("id"), str)
                    or selected["id"] in rejected_ids):
                continue
            rejected_topic_history.append({
                "topic_id": selected["id"],
                "title": selected.get("title"),
                "domain": selected.get("domain"),
                "research_question": selected.get("research_question"),
                "research_form": selected.get("research_form"),
                "evidence_mode": selected.get("evidence_mode"),
                "comparison_type": selected.get("comparison_type"),
                "rejection_type": "intake_validation",
                "rejection_reason": str(error)[:2048],
            })
            rejected_ids.add(selected["id"])
            if len(rejected_topic_history) >= 24:
                break
        setattr(error, "topic_budget", snapshot)
        setattr(error, "candidate_attempt_trace", deepcopy(candidate_attempt_trace))
        setattr(error, "maturity_review_history", deepcopy(maturity_review_history))
        setattr(error, "rejected_topic_history", deepcopy(rejected_topic_history))
        # A bounded intake can exhaust its local proposal/repair passes while
        # still having a scientifically actionable next move.  The Composer
        # uses this durable trace to distinguish a candidate-quality failure
        # (pivot and continue) from an environmental or provider failure
        # (pause/block). Empty traces deliberately remain non-recoverable:
        # missing credentials and failures before a candidate was examined
        # must not become autonomous retry loops.
        setattr(
            error,
            "topic_intake_recoverable",
            bool(candidate_attempt_trace or maturity_review_history or rejected_topic_history),
        )
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
    "topic_portfolio_profile", "validate_topic_portfolio", "topic_refinement_dimensions",
    "validate_topic_refinement",
    "validate_frontier_seed_plan", "validate_source_challenge", "validate_topic_stage_config",
    "validate_topic_package", "validate_topic_feasibility", "topic_prompt",
]
