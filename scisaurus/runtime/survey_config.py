"""Explicit mission and resource boundaries for literature assessment."""
from copy import deepcopy
import json
import math
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.config import _text, configured_worker_slots, validate_common
from scisaurus.runtime.operation_adapters import get_adapter
from scisaurus.runtime.scores import exact, identifier
from scisaurus.runtime.time_policy import validate_time_policy


SEARCH_LIMITS = {"queries_per_role", "results_per_query", "max_works", "challenge_reserve", "expansion_rounds", "expansion_seed_count",
                 "references_per_work", "max_api_calls", "min_new_works", "saturation_rounds", "max_full_texts", "max_text_chars", "context_chars"}


def work_id(value):
    arguments = get_adapter("openalex").validate_arguments({
        "operation": "work", "query": None, "work_id": value, "limit": 1, "cursor": None})
    if arguments["work_id"] != value:
        raise ValidationError("work ID must be a canonical OpenAlex W identifier")
    return value


def search_query(value):
    get_adapter("openalex").validate_arguments({
        "operation": "search", "query": value, "work_id": None, "limit": 1, "cursor": None})
    return value


def _strings(value, name, *, empty=False):
    if not isinstance(value, list) or (not empty and not value):
        raise ValidationError(f"{name} must be an explicit list")
    for item in value:
        _text(item, name)
    if len(set(value)) != len(value):
        raise ValidationError(f"{name} must not repeat values")


def validate_survey_config(value):
    validate_common(value, {"survey", "time_policy"}, retrieval=False)
    repair_mode = value.get("limits", {}).get("repair_mode")
    if repair_mode is not None and repair_mode not in {"bounded", "until_deadline"}:
        raise ValidationError("limits.repair_mode must be bounded or until_deadline")
    survey = value.get("survey")
    fields = {"id", "revision", "question", "seed_queries", "seed_work_ids", "proposed_gap", "bibliography",
              "full_text", "full_text_sources", "search", "stage_seconds"}
    if isinstance(survey, dict) and "identity" in survey:
        fields.add("identity")
    if isinstance(survey, dict) and "provider_intervals" in survey:
        fields.add("provider_intervals")
    exact(survey, fields, "survey")
    identifier(survey["id"])
    if type(survey["revision"]) is not int or survey["revision"] < 1:
        raise ValidationError("survey revision must be positive")
    _text(survey["question"], "survey question")
    _strings(survey["seed_queries"], "seed_queries")
    for item in survey["seed_queries"]:
        search_query(item)
    _strings(survey["seed_work_ids"], "seed_work_ids", empty=True)
    for item in survey["seed_work_ids"]:
        work_id(item)
    if survey["proposed_gap"] is not None:
        exact(survey["proposed_gap"], {"id", "statement"}, "proposed gap")
        identifier(survey["proposed_gap"]["id"])
        _text(survey["proposed_gap"]["statement"], "gap statement")
    capability_ids = set()
    for name, adapter in (("bibliography", "openalex"), ("identity", "crossref"), ("full_text", "mcp_fetch")):
        cap = survey.get(name)
        if name != "bibliography" and cap is None:
            continue
        exact(cap, {"id", "adapter", "client", "representative", "environment_files"}, name)
        identifier(cap["id"])
        if cap["id"] in capability_ids or cap["adapter"] != adapter:
            raise ValidationError("survey capability identity or adapter is invalid")
        capability_ids.add(cap["id"])
        if not isinstance(cap["client"], dict) or "cwd" in cap["client"]:
            raise ValidationError("client must be an object; the runner owns capability workspaces")
        if not isinstance(cap["environment_files"], list):
            raise ValidationError("environment_files must be an explicit list")
        get_adapter(adapter).validate_arguments(cap["representative"])
    search_fields = set(survey["search"]) if isinstance(survey["search"], dict) else set()
    legacy_limits = SEARCH_LIMITS - {"challenge_reserve"}
    if (survey["revision"] >= 5 and search_fields != SEARCH_LIMITS) or (
            survey["revision"] < 5 and frozenset(search_fields) not in {
                frozenset(SEARCH_LIMITS), frozenset(legacy_limits)}):
        raise ValidationError(f"search limits requires exactly {sorted(SEARCH_LIMITS)}"
                              + ("" if survey["revision"] >= 5
                                 else " or the legacy revision fields"))
    for name, amount in survey["search"].items():
        minimum = 0 if name in {"expansion_rounds", "min_new_works"} else 1
        if type(amount) is not int or amount < minimum:
            raise ValidationError(f"search.{name} must be an integer at least {minimum}")
    intervals = survey.get("provider_intervals")
    if intervals is not None:
        if not isinstance(intervals, dict) or set(intervals) != {"bibliography", "identity", "full_text"}:
            raise ValidationError("provider_intervals requires bibliography, identity, and full_text")
        for name, amount in intervals.items():
            if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                raise ValidationError(f"provider_intervals.{name} must be finite and nonnegative")
    if survey["search"]["results_per_query"] > 100 or survey["search"]["max_text_chars"] > 999999:
        raise ValidationError("requested capture exceeds provider limits")
    if survey["search"]["context_chars"] > survey["search"]["max_text_chars"]:
        raise ValidationError("context window cannot exceed the capture limit")
    reserve = survey["search"].get("challenge_reserve", 0)
    if reserve >= survey["search"]["max_works"]:
        raise ValidationError("challenge reserve must leave at least one discovery work slot")
    if len(survey["seed_work_ids"]) > survey["search"]["max_works"] - reserve:
        raise ValidationError("seed works exceed the discovery work limit after challenge reserve")
    if not isinstance(survey["full_text_sources"], list):
        raise ValidationError("full_text_sources must be an explicit list")
    seen = set()
    for source in survey["full_text_sources"]:
        exact(source, {"work_id", "title", "url", "section_markers"}, "full text source")
        work_id(source["work_id"])
        _text(source["title"], "full text title")
        _strings(source["section_markers"], "section_markers")
        get_adapter("mcp_fetch").validate_arguments({"url": source["url"], "max_length": survey["search"]["max_text_chars"]})
        if source["work_id"] in seen:
            raise ValidationError("duplicate full text work mapping")
        seen.add(source["work_id"])
    if survey["full_text_sources"] and survey["full_text"] is None:
        raise ValidationError("full text mappings require an explicit capability")
    validate_time_policy(value.get("time_policy"), stage_seconds=survey["stage_seconds"],
                         unit_count=survey["search"]["max_works"],
                         worker_slots=configured_worker_slots(value["limits"]),
                         wall_clock_seconds=value["limits"]["wall_clock_seconds"])
    return deepcopy(value)


def load_survey_config(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("survey configuration must be a readable JSON file") from exc
    return validate_survey_config(value)
