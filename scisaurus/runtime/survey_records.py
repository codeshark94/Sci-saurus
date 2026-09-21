"""Evidence-bound literature statements and scoped map updates."""
from scisaurus.core.errors import ValidationError
from scisaurus.runtime.config import _text
from scisaurus.runtime.scores import exact
from scisaurus.core.source_spans import validate as validate_source_span


MAP_FIELDS = ("problem", "approach", "finding", "limitations")
SURVEY_CHECKS = ("coverage-accounting", "source-fidelity", "map-support")
GAP_CHECKS = ("closest-prior-work", "scope-comparability", "counterevidence", "full-text-support")


def evidence(items, sources, *, required=False, require_spans=False, windows=None):
    if not isinstance(items, list) or (required and not items):
        raise ValidationError("asserted statements require explicit source evidence")
    errors = []
    for index, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                raise ValidationError("evidence must be an object")
            _text(item["work_id"], "evidence work ID")
            _text(item["source_ref"], "evidence source ref")
            _text(item["quote"], "evidence quote")
            source = sources.get(item["source_ref"])
            if source is None or source["work_id"] != item["work_id"]:
                raise ValidationError(f"must quote the exact captured text of its identified work ({item['source_ref']})")
            validate_source_span(item, source, require_span=require_spans,
                                 window=(windows or {}).get(item["source_ref"]))
        except ValidationError as exc:
            errors.append(f"evidence[{index}]: {exc}")
    if errors:
        raise ValidationError("; ".join(errors))


def statement(value, sources, *, work_id=None, require_spans=False):
    exact(value, {"text", "evidence"}, "literature statement")
    if value["text"] is None:
        if value["evidence"] != []:
            raise ValidationError("an unknown statement cannot claim supporting evidence")
        return
    _text(value["text"], "statement text")
    evidence(value["evidence"], sources, required=True, require_spans=require_spans)
    if work_id and any(item["work_id"] != work_id for item in value["evidence"]):
        raise ValidationError("a work assessment must use evidence from that work")


def validate_map(value, requested, all_work_ids, sources, *, require_spans=False):
    exact(value, {"entries", "relationships"}, "map response")
    if not isinstance(value["entries"], list) or not isinstance(value["relationships"], list):
        raise ValidationError("map entries and relationships must be lists")
    errors = []

    def check(path, function, *args, **kwargs):
        try:
            function(*args, **kwargs)
            return True
        except ValidationError as exc:
            errors.append(f"{path}: {exc}")
            return False

    seen = set()
    for index, entry in enumerate(value["entries"]):
        path = f"entries[{index}]"
        if not check(path, exact, entry, {"work_id", "inclusion", "reason", *MAP_FIELDS}, "map entry"):
            continue
        wid = entry["work_id"]
        if not isinstance(wid, str) or wid not in requested or wid in seen:
            errors.append(f"{path}.work_id: map update changed an unassigned or duplicate work")
            continue
        seen.add(wid)
        path += f" ({wid})"
        if entry["inclusion"] not in ("included", "excluded", "uncertain"):
            errors.append(f"{path}.inclusion: unknown screening decision")
        check(f"{path}.reason", _text, entry["reason"], "screening rationale (string)")
        for field in MAP_FIELDS:
            check(f"{path}.{field}", statement, entry[field], sources, work_id=wid, require_spans=require_spans)
    if seen != set(requested):
        errors.append("entries: map update omitted an assigned work: " + ", ".join(sorted(set(requested) - seen)))
    relations = set()
    for index, relation in enumerate(value["relationships"]):
        path = f"relationships[{index}]"
        if not check(path, exact, relation, {"source", "target", "kind", "claim"}, "relationship"):
            continue
        if (not isinstance(relation["source"], str) or not isinstance(relation["target"], str)
                or relation["source"] not in all_work_ids or relation["target"] not in all_work_ids
                or relation["source"] == relation["target"]
                or not {relation["source"], relation["target"]}.intersection(requested)):
            errors.append(f"{path}: relationship must link known works and involve an assigned work")
            continue
        if relation["kind"] not in ("extends", "contradicts", "compares", "related"):
            errors.append(f"{path}.kind: unsupported conceptual relationship")
            continue
        key = (relation["source"], relation["target"], relation["kind"])
        if key in relations:
            errors.append(f"{path}: duplicate conceptual relationship")
        relations.add(key)
        if check(f"{path}.claim", statement, relation["claim"], sources, require_spans=require_spans):
            if relation["claim"]["text"] is None or not {relation["source"], relation["target"]}.issubset(
                    {item["work_id"] for item in relation["claim"]["evidence"]}):
                errors.append(f"{path}.claim: conceptual relationship requires evidence from both works")
    if errors:
        raise ValidationError("\n".join(errors))


def checks(value, names):
    if not isinstance(value, list):
        raise ValidationError("checks must be an explicit list")
    seen = set()
    for check in value:
        exact(check, {"check_id", "outcome", "method", "result"}, "check")
        if check["check_id"] not in names or check["check_id"] in seen:
            raise ValidationError("unknown or duplicate required check")
        seen.add(check["check_id"])
        if check["outcome"] not in ("passed", "failed", "insufficient_evidence", "check_failed"):
            raise ValidationError(
                f"check {check['check_id']} outcome {check['outcome']!r} must be exactly passed, failed, "
                "insufficient_evidence, or check_failed")
        _text(check["method"], "check method")
        _text(check["result"], "check result")
    if seen != set(names):
        raise ValidationError("required checks were omitted")


def validate_survey_review(value):
    exact(value, {"checks", "rationale"}, "survey review")
    checks(value["checks"], SURVEY_CHECKS)
    _text(value["rationale"], "survey review rationale")


def validate_work_review(value, relationship_refs):
    from scisaurus.core.surveys import work_review_checks
    exact(value, {"checks", "rationale"}, "work review")
    checks(value["checks"], work_review_checks(relationship_refs))
    _text(value["rationale"], "work review rationale")


def validate_assessment(value, sources, works, *, require_spans=False, windows=None):
    exact(value, {"state", "rationale", "comparisons", "checks", "evidence"}, "gap assessment")
    if value["state"] not in ("refuted_by_prior_work", "insufficient_evidence", "eligible_for_experiment"):
        raise ValidationError("unsupported gap decision")
    _text(value["rationale"], "assessment rationale")
    checks(value["checks"], GAP_CHECKS)
    if value["state"] != "insufficient_evidence" and any(
            check["outcome"] != "passed" for check in value["checks"]):
        raise ValidationError("decisive gap assessment requires every check to pass")
    evidence(value["evidence"], sources, required=value["state"] != "insufficient_evidence",
             require_spans=require_spans, windows=windows)
    if not isinstance(value["comparisons"], list):
        raise ValidationError("comparisons must be an explicit list")
    seen = set()
    for item in value["comparisons"]:
        exact(item, {"work_id", "relationship", "statement", "evidence"}, "prior work comparison")
        if not isinstance(item["work_id"], str) or item["work_id"] not in works or item["work_id"] in seen:
            raise ValidationError("comparison cites unknown or duplicate work")
        seen.add(item["work_id"])
        if item["relationship"] not in ("solves", "partial", "different", "uncertain"):
            raise ValidationError("unknown comparison relationship")
        _text(item["statement"], "comparison statement")
        evidence(item["evidence"], sources, required=item["relationship"] != "uncertain",
                 require_spans=require_spans, windows=windows)
        if any(proof["work_id"] != item["work_id"] for proof in item["evidence"]):
            raise ValidationError("comparison must cite its own work")
        decisive = value["state"] == "eligible_for_experiment" or (
            value["state"] == "refuted_by_prior_work" and item["relationship"] == "solves")
        if decisive and not any(sources[proof["source_ref"]].get("representation") == "full_text"
                                and sources[proof["source_ref"]].get("identity_verified") is True for proof in item["evidence"]):
            raise ValidationError("decisive comparison requires verified full text from the compared work")
    if value["state"] == "refuted_by_prior_work" and not any(
            row["relationship"] == "solves" for row in value["comparisons"]):
        raise ValidationError("refutation must identify a prior solution")
    if value["state"] == "eligible_for_experiment" and (not value["comparisons"] or any(
            row["relationship"] in ("solves", "uncertain") for row in value["comparisons"])):
        raise ValidationError("unresolved or solved comparisons cannot authorize experiment eligibility")
