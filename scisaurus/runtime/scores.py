"""Versioned contracts for bounded, domain-independent artifact revision."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import PurePosixPath
import re
import unicodedata

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.config import _text, validate_common
from scisaurus.runtime.contracts import preserves_literals
from scisaurus.runtime.time_policy import validate_time_policy

FORMATS = {"paragraph", "list_item", "code", "json"}
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError("identifier must be a bounded lowercase name")
    return value


def exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def output_path(value):
    _text(value, "output_file")
    path = PurePosixPath(value)
    if (path.is_absolute() or any(part in {".", ".."} for part in value.split("/"))
            or "\\" in value or str(path) != value or any(ord(c) < 32 for c in value)):
        raise ValidationError("output_file must be a normalized relative file path")
    return value


def output_key(value):
    """Reserve the same file identity on case-insensitive Unicode filesystems."""
    return unicodedata.normalize("NFC", output_path(value)).casefold()


def strict_json(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    canonical_bytes(value)
    return value


def validate_text(kind, text):
    _text(text, "unit.text")
    if not isinstance(kind, str) or kind not in FORMATS:
        raise ValidationError("unsupported output unit kind")
    try:
        if kind in {"paragraph", "list_item"} and any(c in text for c in "\r\n\u2028\u2029"):
            raise ValueError("text unit must occupy one structural line")
        if kind == "json":
            strict_json(text)
        elif kind == "code":
            compile(text, "<output-unit>", "exec", dont_inherit=True)
    except (ValueError, SyntaxError, RecursionError) as exc:
        raise ValidationError(f"invalid {kind} unit: {exc}") from exc


def all_units(deliverable):
    return [unit for group in deliverable["groups"] for unit in group["units"]]


def validate_scored_config(config):
    validate_common(config, {"score", "deliverable", "claims", "time_policy"}, retrieval=False)
    deliverable, score = config.get("deliverable"), config.get("score")
    exact(deliverable, {"id", "title", "output_file", "groups"}, "deliverable")
    identifier(deliverable["id"])
    _text(deliverable["title"], "deliverable.title")
    names = {"run.json", "report.md", "progress.json"}
    output = output_key(deliverable["output_file"])
    if output in names:
        raise ValidationError("deliverable output collides with a runtime report")
    names.add(output)
    if not isinstance(config.get("claims"), list):
        raise ValidationError("claims must be an explicit list")
    claim_ids = set()
    for claim in config["claims"]:
        exact(claim, {"id", "statement"}, "claim")
        identifier(claim["id"])
        _text(claim["statement"], "claim.statement")
        if claim["id"] in claim_ids:
            raise ValidationError("duplicate claim ID")
        claim_ids.add(claim["id"])
    groups, ids = deliverable["groups"], set()
    if not isinstance(groups, list) or not groups:
        raise ValidationError("deliverable requires at least one group")
    for group in groups:
        exact(group, {"id", "title", "units"}, "group")
        identifier(group["id"])
        _text(group["title"], "group.title")
        if group["id"] in ids:
            raise ValidationError("duplicate structural ID")
        ids.add(group["id"])
        if not isinstance(group["units"], list) or not group["units"]:
            raise ValidationError("each group requires output units")
        for unit in group["units"]:
            exact(unit, {"id", "kind", "text", "editable", "objective", "required_literals", "claim_ids", "output_file"}, "unit")
            identifier(unit["id"])
            if unit["id"] in ids:
                raise ValidationError("duplicate unit or assignment")
            ids.add(unit["id"])
            validate_text(unit["kind"], unit["text"])
            if type(unit["editable"]) is not bool:
                raise ValidationError("editable must be a Boolean")
            if unit["editable"]:
                _text(unit["objective"], "unit.objective")
            elif unit["objective"] is not None:
                raise ValidationError("immutable unit cannot have a revision objective")
            if not isinstance(unit["required_literals"], list):
                raise ValidationError("required_literals must be a list")
            for literal in unit["required_literals"]:
                _text(literal, "required_literal")
            if not preserves_literals(unit["text"], unit["required_literals"]):
                raise ValidationError("required literals must be present in their own baseline unit")
            refs = unit["claim_ids"]
            if (not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
                    or len(set(refs)) != len(refs) or set(refs) - claim_ids):
                raise ValidationError("unknown or duplicate unit claim binding")
            if unit["output_file"] is not None:
                path = output_key(unit["output_file"])
                if path in names:
                    raise ValidationError("duplicate or reserved output path")
                names.add(path)
    if any(a != b and PurePosixPath(a) in PurePosixPath(b).parents for a in names for b in names):
        raise ValidationError("output file paths cannot contain other output files")
    editable = {u["id"] for u in all_units(deliverable) if u["editable"]}
    if not editable:
        raise ValidationError("revision requires at least one editable unit")
    exact(score, {"id", "revision", "domain", "checks", "capabilities", "workloads", "candidate_checks", "stage_seconds"}, "score")
    identifier(score["id"])
    if type(score["revision"]) is not int or score["revision"] < 1:
        raise ValidationError("score revision must be a positive integer")
    _text(score["domain"], "score.domain")
    for name in ("checks", "capabilities", "workloads", "candidate_checks"):
        if not isinstance(score[name], list):
            raise ValidationError(f"score.{name} must be an explicit list")
    seen = set()
    for check in score["checks"]:
        exact(check, {"check_id", "requirement"}, "score check")
        identifier(check["check_id"])
        _text(check["requirement"], "check.requirement")
        if check["check_id"] in seen:
            raise ValidationError("duplicate score check")
        seen.add(check["check_id"])
    capability_ids, adapters = set(), {}
    for capability in score["capabilities"]:
        exact(capability, {"id", "adapter", "client", "representative", "environment_files"}, "capability")
        identifier(capability["id"])
        if capability["id"] in capability_ids:
            raise ValidationError("duplicate capability ID")
        capability_ids.add(capability["id"])
        from scisaurus.runtime.operation_adapters import ADAPTERS
        if not isinstance(capability["adapter"], str) or capability["adapter"] not in ADAPTERS:
            raise ValidationError("unsupported configured capability")
        adapters[capability["id"]] = capability["adapter"]
        if not isinstance(capability["client"], dict) or "cwd" in capability["client"]:
            raise ValidationError("capability client must be an object; workspace is owned by the project runner")
        if not isinstance(capability["representative"], dict) or not isinstance(capability["environment_files"], list):
            raise ValidationError("capability must declare representative arguments and public software pins")
    for collection, fields in (("workloads", {"id", "capability_id", "arguments"}),
                               ("candidate_checks", {"id", "capability_id", "unit_id", "input", "text_field", "result_path", "expected"})):
        seen = set()
        for item in score[collection]:
            exact(item, fields, collection)
            identifier(item["id"])
            identifier(item["capability_id"])
            if item["id"] in seen or item["capability_id"] not in capability_ids:
                raise ValidationError("duplicate workload/check or undeclared capability")
            seen.add(item["id"])
            if collection == "workloads":
                if not isinstance(item["arguments"], dict):
                    raise ValidationError("workload arguments must be an object")
            else:
                identifier(item["unit_id"])
                if item["unit_id"] not in editable or adapters[item["capability_id"]] != "local_program":
                    raise ValidationError("candidate program check must target a declared editable unit")
                if not isinstance(item["input"], dict):
                    raise ValidationError("candidate check input must be an object")
                _text(item["text_field"], "text_field")
                if item["text_field"] in item["input"]:
                    raise ValidationError("candidate text field cannot be prefilled")
                if not isinstance(item["result_path"], list) or any(
                        type(k) not in {str, int} or (type(k) is int and k < 0) for k in item["result_path"]):
                    raise ValidationError("result_path must contain object keys or array indices")
    canonical_bytes(score)
    validate_time_policy(config.get("time_policy"), stage_seconds=score["stage_seconds"], unit_count=len(editable),
                         worker_slots=config["limits"]["concurrent_calls"] - 1,
                         wall_clock_seconds=config["limits"]["wall_clock_seconds"])
    return config


def project_contract(config):
    """Normalize the original paper format once at its compatibility boundary."""
    if "score" in config:
        validate_scored_config(config)
        return deepcopy(config["score"]), deepcopy(config["deliverable"])
    from scisaurus.runtime.project_config import validate_project_config
    validate_project_config(config)
    limits = config["limits"]
    groups = [{"id": s["id"], "title": s["title"], "units": [
        {**u, "kind": "paragraph", "output_file": None} for u in s["paragraphs"]]} for s in config["document"]["sections"]]
    search = {"id": "scholarly-search", "adapter": "crossref", "client": {
        "timeout": limits["retrieval_timeout_seconds"], "max_bytes": limits["max_source_bytes"]},
        "representative": {"query": config["public_queries"][0], "limit": limits["search_results"]}, "environment_files": []}
    fetch = {"id": "source-fetch", "adapter": "mcp_fetch", "client": {
        "timeout": limits["retrieval_timeout_seconds"], "max_bytes": limits["max_source_bytes"],
        "command": config["mcp_fetch_command"], "own_process_group": False},
        "representative": {"url": config["source_urls"][0], "max_length": limits["max_capture_chars"]},
        "environment_files": config["operations"]["environment_files"]}
    workloads = [{"id": f"search-{i}", "capability_id": search["id"], "arguments": {"query": q, "limit": limits["search_results"]}}
                 for i, q in enumerate(config["public_queries"])]
    workloads += [{"id": f"fetch-{i}", "capability_id": fetch["id"], "arguments": {"url": u, "max_length": limits["max_capture_chars"]}}
                  for i, u in enumerate(config["source_urls"])]
    return ({"id": "paper-revision", "revision": 1, "domain": "scientific manuscript revision",
             "checks": [], "capabilities": [search, fetch], "workloads": workloads,
             "candidate_checks": [], "stage_seconds": None},
            {"id": "manuscript", "title": config["document"]["title"], "output_file": "manuscript.md", "groups": groups})
