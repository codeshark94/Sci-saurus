"""Bounded, model-assisted topic discovery for a free-topic Composer run.

Topic selection is an intake operation, not a novelty claim.  The stage turns a
broad Principal objective into several testable research questions, chooses one
with an explicit rationale, and hands only the selected question and search
seeds to the literature stage.  The survey, counter-search, experiment, and
review gates remain the authorities for evidence and release.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import itertools
import json
import math
from random import Random
from pathlib import Path
import re
import time

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.models import ModelClient
from scisaurus.runtime.literature import OpenAlexClient
from scisaurus.runtime.retrieval import CrossrefClient
from scisaurus.runtime.scientific_surface import find_control_leaks


SCHEMA_VERSION = "topic-discovery-1"
STAGE_CONFIG_SCHEMA_VERSION = "topic-discovery-config-1"
RECENT_YEAR_WINDOW = 4
CANDIDATE_FIELDS = {
    "id", "title", "domain", "research_question", "scope", "search_queries",
    "why_promising", "disconfirmation_test", "feasibility", "resource_plan",
    "capability_requirements",
}
LEGACY_CANDIDATE_FIELDS = CANDIDATE_FIELDS - {"capability_requirements"}
CAPABILITY_FIELDS = {"executables", "python_packages", "stage_kinds"}
KNOWN_STAGE_KINDS = {"topic_discovery", "survey", "experiment", "interpretation", "argument", "paper"}


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


def validate_topic_stage_config(value):
    """Validate the descriptor consumed by the Composer topic stage."""
    fields = {"schema_version", "model_config_path", "output_path", "candidate_count", "max_attempts"}
    allowed = fields | {"repair_mode"}
    if (not isinstance(value, dict) or set(value) - allowed
            or not fields.issubset(value)):
        raise ValidationError(f"topic discovery config requires {sorted(fields)} and permits repair_mode")
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
    canonical_bytes(value)
    return deepcopy(value)


def validate_topic_package(value, *, objective=None, candidate_count=None):
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
    for candidate in candidates:
        if (not isinstance(candidate, dict)
                or set(candidate) not in (CANDIDATE_FIELDS, LEGACY_CANDIDATE_FIELDS)):
            raise ValidationError("topic candidate has an invalid shape")
        _identifier(candidate["id"], "topic candidate id")
        if candidate["id"] in ids:
            raise ValidationError("topic candidate IDs must be unique")
        ids.add(candidate["id"])
        for key in ("title", "domain", "research_question", "scope", "why_promising",
                    "disconfirmation_test", "feasibility", "resource_plan"):
            _text(candidate[key], f"topic candidate {key}")
        _strings(candidate["search_queries"], "topic candidate search_queries", minimum=3, maximum=8)
        if "capability_requirements" in candidate:
            _validate_capability_requirements(candidate["capability_requirements"])
    _identifier(value["selected_id"], "selected topic id")
    if value["selected_id"] not in ids:
        raise ValidationError("selected topic is not one of the candidates")
    _text(value["selection_rationale"], "topic selection rationale")
    canonical_bytes(value)
    return deepcopy(value)


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


SYSTEM = (
    "You are the intake research strategist for a general-purpose scientific organization. "
    "Use the supplied recent scholarly records as prompts, then turn a broad objective into several "
    "genuinely different, testable research questions. "
    "Do not claim novelty, truth, or empirical results before the literature and methods stages run. "
    "Prefer questions that can be investigated with public sources and a bounded reproducible experiment "
    "using the declared runtime capabilities. Reject directions that require unavailable instruments, "
    "private cohorts, or unconfigured software. "
    "When runtime_context includes an experiment_contract, the selected candidate must be directly executable "
    "under that contract: align its phenomenon, comparison, data boundary, method, and primary outcomes with "
    "the declared experiment rather than silently proposing a different study. "
    "Keep scope explicit, include a way the idea could be disproved, and select one candidate only after "
    "comparing the alternatives. Use reader-facing scientific language; do not mention workflow state, "
    "artifacts, validators, hashes, acceptance, or internal control terms. Return JSON only. "
    "For capability_requirements, copy exact names from the supplied runtime inventory and leave a list empty "
    "when a requirement is unnecessary."
)


def topic_prompt(objective, candidate_count, *, recent_papers=None, runtime_context=None):
    return json.dumps({
        "assignment": "free_topic_discovery",
        "principal_objective": objective,
        "candidate_count": candidate_count,
        "recent_papers": recent_papers or [],
        "runtime_context": runtime_context or {},
        "output_contract": {
            "schema_version": SCHEMA_VERSION,
            "objective": "copy principal_objective exactly",
            "candidates": "list of distinct candidate objects",
            "candidate": {
                "id": "lowercase identifier",
                "title": "short working title",
                "domain": "research domain",
                "research_question": "one testable question",
                "scope": "population, system, data, or phenomenon boundary",
                "search_queries": "3 to 8 concrete literature search strings",
                "why_promising": "why this is worth investigating without claiming novelty",
                "disconfirmation_test": "what result or prior work would make this direction unhelpful",
                "feasibility": "why the declared runtime can execute the study within the mission budget",
                "resource_plan": "data, programs, tools, and compute the study would use",
                "capability_requirements": {
                    "executables": "exact names from runtime_context.executables",
                    "python_packages": "exact names from runtime_context.python_packages",
                    "stage_kinds": "stage kinds from runtime_context.configured_stage_kinds",
                },
            },
            "selected_id": "one candidate id",
            "selection_rationale": "compare evidence availability, testability, and disconfirmation risk",
        },
        "constraints": [
            "use recent_papers as inspiration and retain their provided source identifiers in the candidate rationale when relevant",
            "candidate questions must differ in mechanism or empirical comparison, not just wording",
            "search queries must be usable as ordinary scholarly search strings",
            "capability_requirements must list only exact available executable, Python package, and stage-kind names",
            "if experiment_contract is present, select the candidate that can be answered by it without changing the declared experiment",
            "never invent a citation, dataset, result, or prior-work claim",
        ],
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

    def run(self, objective, *, candidate_count=4, max_attempts=3,
            repair_mode="bounded", recent_papers=None, runtime_context=None,
            bibliography=None, sampling_seed=None):
        _text(objective, "topic objective", public=False)
        if type(candidate_count) is not int or not 3 <= candidate_count <= 8:
            raise ValidationError("topic discovery candidate_count must be between 3 and 8")
        if repair_mode not in {"bounded", "until_deadline"}:
            raise ValidationError("topic discovery repair_mode must be bounded or until_deadline")
        if repair_mode == "until_deadline" and self.deadline_seconds is None:
            raise ValidationError("topic discovery until_deadline mode requires a stage deadline")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 8:
            raise ValidationError("topic discovery max_attempts must be between 1 and 8")
        deadline = time.monotonic() + self.deadline_seconds if self.deadline_seconds is not None else None
        recent_papers = list(recent_papers or [])
        sampling_trace = []
        if bibliography is not False and not recent_papers:
            recent_papers, sampling_seed, sampling_trace = self._recent_paper_sample(
                objective, bibliography=bibliography, deadline=deadline, sampling_seed=sampling_seed)
        previous = None
        last_error = None
        usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
        attempts = itertools.count() if repair_mode == "until_deadline" else range(max_attempts)
        for attempt in attempts:
            config = deepcopy(self.model_config)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0.2:
                    raise ValidationError("topic discovery deadline exceeded")
                timeout_seconds = config.get("timeout_seconds")
                if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                    raise ValidationError("topic discovery model config requires a finite positive timeout_seconds")
                config["timeout_seconds"] = min(float(timeout_seconds), remaining)
            client = ModelClient(**config)
            prompt = topic_prompt(objective, candidate_count,
                                  recent_papers=recent_papers,
                                  runtime_context=runtime_context)
            if previous is not None:
                prompt = json.dumps({
                    "assignment": "repair_invalid_topic_discovery",
                    "principal_objective": objective,
                    "candidate_count": candidate_count,
                    "recent_papers": recent_papers,
                    "runtime_context": runtime_context or {},
                    "candidate_response": previous[:40000],
                    "validation_error": str(last_error),
                    "instruction": "Return a complete package satisfying the exact contract; preserve valid candidates and repair only the violations.",
                }, ensure_ascii=False, sort_keys=True)
            result = client.complete(system=SYSTEM, prompt=prompt)
            usage["model_calls"] += 1
            for key in ("input_tokens", "output_tokens"):
                usage[key] += result.usage.get(key, 0)
            previous = result.text
            if result.finish_reason != "stop":
                last_error = ValidationError(f"topic discovery did not finish normally: {result.finish_reason}")
                continue
            try:
                package = result.json_object()
                validate_topic_package(package, objective=objective, candidate_count=candidate_count)
                if runtime_context is not None:
                    feasibility = validate_topic_feasibility(package, runtime_context)
                    if feasibility["status"] == "legacy_unchecked":
                        raise ValidationError(
                            "current topic discovery output must include capability_requirements")
            except ValidationError as exc:
                last_error = exc
                continue
            selected = next(item for item in package["candidates"] if item["id"] == package["selected_id"])
            feasibility = validate_topic_feasibility(package, runtime_context)
            return {
                **package,
                "status": "completed",
                "topic": selected,
                "question": selected["research_question"],
                "search_queries": selected["search_queries"],
                "proposed_gap": selected["why_promising"],
                "feasibility_check": feasibility,
                "recent_papers": recent_papers,
                "sampling_seed": sampling_seed,
                "sampling_trace": sampling_trace,
                "usage": usage,
            }
        raise last_error or ValidationError("topic discovery did not produce a valid package")

    @staticmethod
    def _recent_paper_sample(objective, *, bibliography=None, deadline=None, sampling_seed=None):
        """Search OpenAlex and retain a reproducible recent-paper sample.

        The topic stage is intentionally exploratory: it uses a small recent
        sample to seed candidate generation, while the survey stage performs
        the authoritative multi-query search and evidence mapping.
        """
        if deadline is not None and deadline - time.monotonic() <= 0.2:
            raise ValidationError("topic discovery deadline exceeded before literature sampling")
        client_config = {
            "timeout": 20, "max_bytes": 2_000_000,
            "endpoint": "https://api.openalex.org/works",
            "auth_env": None,
            "max_retries": 3, "retry_backoff_seconds": 1.0,
        }
        if isinstance(bibliography, dict):
            client_config.update({key: bibliography[key] for key in client_config if key in bibliography})
        client = OpenAlexClient(**client_config)
        # Keep Unicode terms so a Korean or mixed-language objective can seed
        # the same scholarly search path.  A broad fallback is preferable to
        # silently skipping topic discovery when punctuation or short tokens
        # consume the user's wording.
        words = [word for word in re.findall(r"[^\W_]{2,}(?:[-'][^\W_]+)*", objective, flags=re.UNICODE)][:10]
        if not words:
            words = ["scientific method", "empirical study"]
        queries = [" ".join(words), " ".join(words[:5] + ["method"]),
                   " ".join(words[-5:] + ["experiment"]), "recent scientific research"]
        records = []
        seen = set()
        provider_errors = []
        sampling_trace = []
        for query in queries:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0.2:
                    raise ValidationError("topic discovery deadline exceeded during literature sampling")
                client_config["timeout"] = min(20.0, remaining)
                client = OpenAlexClient(**client_config)
            result = client.run(operation="search", query=query, limit=10, cursor=None)
            if not isinstance(result, dict):
                provider_errors.append({"query": query, "outcome": "malformed_response"})
                continue
            outcome = result.get("outcome")
            works = result.get("works", []) if isinstance(result.get("works", []), list) else []
            metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            sampling_trace.append({
                "query": query,
                "outcome": outcome,
                "returned_work_ids": [item.get("id") for item in works
                                      if isinstance(item, dict) and isinstance(item.get("id"), str)],
                "source_url": result.get("source_url"),
                "capture_sha256": result.get("capture_sha256"),
                "provider": metadata.get("provider"),
                "http_status": metadata.get("http_status"),
                "request": metadata.get("request"),
            })
            if outcome not in {"ok", "empty"}:
                provider_errors.append({"query": query, "outcome": outcome, "error": result.get("error")})
                continue
            for work in works:
                if not isinstance(work, dict) or not isinstance(work.get("id"), str):
                    continue
                title = work.get("title")
                if not isinstance(title, str) or not title.strip():
                    # A provider response that is useful for discovery must
                    # still have a reader-facing identity.  Skip malformed
                    # rows and preserve the request trace rather than letting
                    # one bad item abort an otherwise valid sample.
                    continue
                if work["id"] in seen:
                    continue
                seen.add(work["id"])
                records.append({
                    "work_id": work["id"], "title": title,
                    "year": work.get("year") if type(work.get("year")) is int else None,
                    "abstract": ((work.get("abstract") or "")[:5000] or None),
                    "doi": work.get("doi"), "locations": work.get("locations", []),
                    **({"authors": work["authors"]} if isinstance(work.get("authors"), list) else {}),
                    "source_url": "https://openalex.org/" + work["id"],
                })
        if provider_errors and not records:
            # OpenAlex's keyless service can exhaust a daily credit pool even
            # while the rest of the research stack is healthy.  Topic intake
            # is allowed to switch to Crossref metadata for inspiration; the
            # authoritative survey still records and verifies its own source
            # route before any claim is admitted.
            fallback_client = CrossrefClient(timeout=20, max_bytes=2_000_000)
            fallback_seen = set()
            for query in queries:
                fallback = fallback_client.search(query, limit=10)
                fallback_meta = fallback.get("metadata") if isinstance(fallback.get("metadata"), dict) else {}
                sampling_trace.append({
                    "query": query,
                    "outcome": fallback.get("outcome"),
                    "returned_source_ids": [item.get("doi") for item in fallback.get("sources", [])
                                             if isinstance(item, dict) and isinstance(item.get("doi"), str)],
                    "source_url": fallback.get("source_url"),
                    "capture_sha256": fallback.get("capture_sha256"),
                    "provider": fallback_meta.get("provider"),
                    "http_status": fallback_meta.get("http_status"),
                    "request": {"query": query, "limit": 10},
                })
                if fallback.get("outcome") not in {"ok", "empty"}:
                    continue
                for source in fallback.get("sources", []):
                    if not isinstance(source, dict) or not isinstance(source.get("doi"), str):
                        continue
                    source_id = source["doi"].lower()
                    if source_id in fallback_seen:
                        continue
                    fallback_seen.add(source_id)
                    published = source.get("published") or {}
                    parts = published.get("date-parts", [[]]) if isinstance(published, dict) else [[]]
                    year = parts[0][0] if parts and parts[0] and type(parts[0][0]) is int else None
                    title = source.get("title")
                    if not isinstance(title, str) or not title.strip():
                        continue
                    records.append({
                        "work_id": "doi:" + source["doi"], "title": title,
                        "year": year, "abstract": (source.get("abstract") or "")[:5000] or None,
                        "doi": source["doi"], "locations": [],
                        "source_url": source.get("source_url") or "https://doi.org/" + source["doi"],
                        "provider": "crossref",
                    })
            if not records:
                outcomes = ", ".join(str(item.get("outcome")) for item in provider_errors)
                raise ValidationError(f"recent scholarly topic sampling failed across OpenAlex and Crossref queries: {outcomes}")
        if not records:
            raise ValidationError("OpenAlex returned no scholarly records for topic discovery")
        current_year = time.gmtime().tm_year
        recent_cutoff = current_year - RECENT_YEAR_WINDOW
        recent = [item for item in records if isinstance(item.get("year"), int)
                  and item["year"] >= recent_cutoff]
        # If the objective is niche and the recent window is empty, preserve
        # the newest provider records instead of fabricating a topic.
        pool = recent or sorted(records, key=lambda item: item.get("year") if type(item.get("year")) is int else 0,
                                 reverse=True)
        if sampling_seed is None:
            sampling_seed = int(hashlib.sha256(objective.encode("utf-8")).hexdigest()[:16], 16)
        if type(sampling_seed) is not int or sampling_seed < 0:
            raise ValidationError("topic sampling_seed must be a nonnegative integer")
        rng = Random(sampling_seed)
        if len(pool) > 24:
            pool = rng.sample(pool, 24)
        else:
            # Shuffle even a short pool so provider ordering does not become
            # an implicit ranking signal; the seed keeps the run reproducible.
            pool = list(pool)
            rng.shuffle(pool)
        return pool, sampling_seed, sampling_trace


__all__ = [
    "SCHEMA_VERSION", "STAGE_CONFIG_SCHEMA_VERSION", "RECENT_YEAR_WINDOW", "TopicDiscoveryRunner",
    "validate_topic_stage_config", "validate_topic_package", "validate_topic_feasibility", "topic_prompt",
]
