"""Evidence-bound literature statements and scoped map updates."""
from copy import deepcopy
import re
import unicodedata

from scisaurus.core.errors import ModelContractError, ValidationError
from scisaurus.runtime.config import _text
from scisaurus.runtime.scores import exact
from scisaurus.core.source_spans import validate as validate_source_span


MAP_FIELDS = ("problem", "approach", "finding", "limitations")
SURVEY_CHECKS = ("coverage-accounting", "source-fidelity", "map-support")
GAP_CHECKS = ("closest-prior-work", "scope-comparability", "counterevidence", "full-text-support")
REVIEW_CHECK_FIELDS = frozenset({"check_id", "outcome", "method", "result"})
CRITIQUE_DISPOSITIONS = {
    "current_defect": "The critique identifies an unsupported or unverified current assertion.",
    "corrected": "The original defect was corrected in the current entry or relationships.",
    "rejected": "The hypothesis is not corroborated as a defect in the current assertions.",
    "nonassertion": "The disputed assertion is absent; the original scientific question may remain open.",
}


def survey_review_response_contract(current_map):
    """Expose the exact review envelope and immutable finding destinations."""
    return {
        "required_fields": ["checks", "rationale"],
        "optional_fields": ["findings"],
        "checks": [{"check_id": name, "required_fields": sorted(REVIEW_CHECK_FIELDS),
                    "additional_fields": False} for name in SURVEY_CHECKS],
        "findings": {
            "location": "top-level findings only; never inside a check row",
            "required_fields": ["check_id", "target_ref", "field", "quote", "rationale"],
            "entry_targets": {ref: ["inclusion", "reason", *MAP_FIELDS]
                              for ref in current_map["entry_refs"].values()},
            "relationship_targets": {ref: ["claim"] for ref in current_map["relationship_refs"]},
            "target_ref": "Copy an exact immutable artifact reference from the target catalog; a work ID is not a target reference.",
            "quote": "An exact substring of the named current field, not a source quotation or a historical assertion.",
            "check_id": "A non-passed required check; omit findings for supported assertions.",
        },
    }


BODY_SECTION_MARKERS = ("Introduction", "Background", "Methods", "Materials and Methods",
                        "Methodology", "Results", "Discussion", "Conclusions", "Conclusion")


def section_identity(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def has_section_heading(text, marker):
    expected = section_identity(marker)
    if not expected:
        return False
    for line in text.splitlines():
        candidate = re.sub(r"^\s*#{1,6}\s*", "", line).strip()
        candidate = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", candidate)
        if section_identity(candidate.strip(" -*_`")) == expected:
            return True
    return False


def authoritative_source(source):
    """Only identified captures can support work-specific scientific assertions."""
    if not isinstance(source, dict):
        return False
    if source.get("representation") == "abstract":
        return True
    identity = source.get("identity_checks")
    markers = identity.get("section_markers") if isinstance(identity, dict) else None
    return (source.get("representation") == "full_text"
            and source.get("identity_verified") is True
            and isinstance(identity, dict) and identity.get("title_match") is True
            and isinstance(markers, list) and bool(markers)
            and all(isinstance(marker, str) and marker.strip() for marker in markers)
            and any(section_identity(marker) not in {"", "abstract", "summary"} for marker in markers))


def normalize_check_envelope(value, required):
    """Project a model envelope to the checks actually assigned.

    Reviewers sometimes append question-specific diagnostics or repeat a
    required row after completing the requested checks. Those extra rows are
    outside the assignment contract and cannot affect admission. Projection is
    allowed only when every required ID occurs exactly once; missing required
    checks still fail normally. A passed critique has no affected failed check;
    its omitted empty list is structurally determined by that explicit verdict.
    Unresolved critique links are never inferred. Explicit links to passed
    ordinary checks can be removed only when a known non-passed link remains;
    check outcomes and scientific assertions are never changed.
    Typed critique dispositions distinguish rejecting an allegation from
    finding a current defect. Their canonical checks record admission status;
    the original dispositions remain in the immutable model execution.
    """
    if not isinstance(value, dict) or not isinstance(value.get("checks"), list):
        return value
    required = tuple(required)
    required_ids = frozenset(required)
    if "critique_adjudications" in value:
        if set(value) != {"checks", "rationale", "critique_adjudications"}:
            return value
        adjudications = value["critique_adjudications"]
        critique_ids = {key for key in required_ids if key.startswith("critique:")}
        fields = {"check_id", "disposition", "method", "result", "affected_check_ids"}
        if (not critique_ids or not isinstance(adjudications, list)
                or any(not isinstance(row, dict) or set(row) != fields
                       or not isinstance(row["check_id"], str) or row["check_id"] not in critique_ids
                       or not isinstance(row["disposition"], str) or row["disposition"] not in CRITIQUE_DISPOSITIONS
                       for row in adjudications)
                or len(adjudications) != len(critique_ids)
                or {row["check_id"] for row in adjudications} != critique_ids
                or any(isinstance(row, dict) and isinstance(row.get("check_id"), str)
                       and row["check_id"] in critique_ids for row in value["checks"])):
            return value
        value = {"rationale": value["rationale"], "checks": [*value["checks"], *[
            {"check_id": row["check_id"],
             "outcome": "failed" if row["disposition"] == "current_defect" else "passed",
             "method": row["method"], "result": row["result"],
             "affected_check_ids": row["affected_check_ids"]}
            for row in adjudications]]}
    rows = value["checks"]
    counts = {}
    for row in rows:
        if not isinstance(row, dict):
            return value
        check_id = row.get("check_id")
        if isinstance(check_id, str) and check_id in required_ids:
            counts[check_id] = counts.get(check_id, 0) + 1
    if set(counts) != required_ids or any(count != 1 for count in counts.values()):
        return value
    projected = dict(value)
    # Preserve the model's requested order.  The acceptance gate treats check
    # IDs as a set, while replay compares the completed envelope byte-for-byte
    # at the list level; reordering here would turn a valid response into a
    # false mismatch when the caller supplied a different but valid order.
    projected["checks"] = [row for row in rows
                            if isinstance(row.get("check_id"), str)
                            and row["check_id"] in required_ids]
    projected["checks"] = [
        {**row, "affected_check_ids": []}
        if row["check_id"].startswith("critique:") and row.get("outcome") == "passed"
        and "affected_check_ids" not in row else row
        for row in projected["checks"]]
    ordinary = {row["check_id"]: row.get("outcome") for row in projected["checks"]
                if not row["check_id"].startswith("critique:")}
    unresolved = {key for key, outcome in ordinary.items() if outcome != "passed"}
    for index, row in enumerate(projected["checks"]):
        links = row.get("affected_check_ids")
        if (row["check_id"].startswith("critique:") and row.get("outcome") != "passed"
                and isinstance(links, list) and all(isinstance(key, str) and key in ordinary for key in links)
                and len(set(links)) == len(links)
                and any(key in unresolved for key in links)):
            projected["checks"][index] = {**row, "affected_check_ids": [key for key in links if key in unresolved]}
    return projected


def normalize_gap_assessment_envelope(value, *, evidence_catalog=None,
                                     verified_full_text_refs=None,
                                     known_work_ids=None):
    """Canonicalize unambiguous response aliases without relaxing evidence gates.

    Normalize transport aliases and turn unreadable evidence selections into
    explicit uncertainty. No malformed or cross-work citation can become a
    positive finding; every normalized result still passes the strict
    assessment validator.
    """
    if not isinstance(value, dict):
        return value
    evidence_by_id = {}
    duplicate_ids = set()
    if isinstance(evidence_catalog, list):
        for item in evidence_catalog:
            if (not isinstance(item, dict)
                    or not isinstance(item.get("evidence_id"), str)
                    or not isinstance(item.get("work_id"), str)
                    or not isinstance(item.get("source_ref"), str)):
                continue
            evidence_id = item["evidence_id"]
            if evidence_id in evidence_by_id:
                duplicate_ids.add(evidence_id)
            else:
                evidence_by_id[evidence_id] = item
    for evidence_id in duplicate_ids:
        evidence_by_id.pop(evidence_id, None)
    verified_full_text_refs = set(
        item for item in verified_full_text_refs
        if isinstance(item, str)
    ) if isinstance(verified_full_text_refs, (list, tuple, set)) else set()

    raw_checks = value.get("checks")
    checks = []
    if isinstance(raw_checks, list):
        for item in raw_checks:
            if not isinstance(item, dict):
                checks.append(item)
                continue
            row = deepcopy(item)
            aliases = [row[key] for key in ("check", "name")
                       if isinstance(row.get(key), str)]
            check_id = row.get("check_id")
            if check_id is None and aliases and len(set(aliases)) == 1:
                check_id = aliases[0]
            if isinstance(check_id, str):
                row["check_id"] = check_id
            rationale = row.get("rationale")
            if isinstance(rationale, str):
                row.setdefault(
                    "method",
                    "Checked the cited source evidence against the retained survey map and coverage.",
                )
                row.setdefault("result", rationale)
            checks.append({key: row[key] for key in ("check_id", "outcome", "method", "result")
                           if key in row})
    check_rows = {}
    for row in checks:
        if isinstance(row, dict) and row.get("check_id") in GAP_CHECKS:
            check_rows.setdefault(row["check_id"], []).append(row)
    checks = []
    for check_id in GAP_CHECKS:
        candidates = check_rows.get(check_id, [])
        if len(candidates) == 1:
            row = candidates[0]
            outcome = row.get("outcome")
            method, result = row.get("method"), row.get("result")
            if (isinstance(outcome, str)
                    and outcome in {"passed", "failed", "insufficient_evidence", "check_failed"}
                    and isinstance(method, str) and method.strip()
                    and isinstance(result, str) and result.strip()):
                checks.append({"check_id": check_id, "outcome": outcome,
                               "method": method.strip(), "result": result.strip()})
                continue
        checks.append({
            "check_id": check_id,
            "outcome": "insufficient_evidence",
            "method": "The submitted gap-assessment check could not be read unambiguously.",
            "result": "Its evidence or result was missing, duplicated, or malformed, so this check remains unresolved.",
        })

    outcomes = [row.get("outcome") for row in checks if isinstance(row, dict)]
    check_ids = [row.get("check_id") for row in checks if isinstance(row, dict)]
    checks_complete = (len(checks) == len(GAP_CHECKS)
                       and set(check_ids) == set(GAP_CHECKS)
                       and len(set(check_ids)) == len(GAP_CHECKS))

    state = value.get("state")
    valid_states = ("refuted_by_prior_work", "insufficient_evidence", "eligible_for_experiment")
    if state not in valid_states:
        status = value.get("status")
        if status in valid_states:
            state = status
        elif status == "supported":
            state = ("eligible_for_experiment"
                     if checks_complete and outcomes and all(outcome == "passed" for outcome in outcomes)
                     else "insufficient_evidence")
        else:
            state = "insufficient_evidence"
    if (state in {"refuted_by_prior_work", "eligible_for_experiment"}
            and any(outcome != "passed" for outcome in outcomes)):
        state = "insufficient_evidence"

    rationale = value.get("rationale")
    if not isinstance(rationale, str):
        rationale = value.get("answer")
    if isinstance(rationale, str):
        uncertainty = value.get("uncertainty")
        if (isinstance(uncertainty, str) and uncertainty.strip()
                and uncertainty.casefold() not in {"none", "low", "no material uncertainty"}
                and uncertainty not in rationale):
            rationale = f"{rationale}\nUncertainty: {uncertainty}"
    else:
        rationale = "The supplied survey evidence did not support a decisive assessment; unresolved checks and source limits are retained explicitly."

    def evidence_selections(items):
        if not isinstance(items, list):
            return items
        selections = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str):
                selections.append({"evidence_id": item["evidence_id"]})
            elif isinstance(item, str) and item in evidence_by_id:
                selections.append({"evidence_id": item})
            elif (isinstance(item, dict)
                  and all(isinstance(item.get(key), str)
                          for key in ("work_id", "source_ref", "quote"))):
                selections.append({key: item[key] for key in (
                    "work_id", "source_ref", "quote", "start", "end", "quote_sha256"
                ) if key in item})
            else:
                selections.append(item)
        return selections

    comparisons = value.get("comparisons")
    if isinstance(comparisons, list):
        projected = []
        comparison_index_by_work = {}
        known_work_ids = (set(known_work_ids) if known_work_ids is not None else None)
        for item in comparisons:
            if not isinstance(item, dict):
                continue
            work_id = item.get("work_id")
            if (not isinstance(work_id, str)
                    or (known_work_ids is not None and work_id not in known_work_ids)):
                continue
            statement = item.get("statement")
            if not isinstance(statement, str):
                statement = item.get("note")
            if not isinstance(statement, str) or not statement.strip():
                statement = "The supplied evidence does not resolve this work's relationship to the nominated gap."
            raw_relationship = item.get("relationship")
            relationship = (raw_relationship if isinstance(raw_relationship, str)
                            and raw_relationship in {
                "solves", "partial", "different", "uncertain"
            } else "uncertain")
            raw_evidence = item.get("evidence")
            comparison_evidence = evidence_selections(raw_evidence)
            if not isinstance(comparison_evidence, list):
                comparison_evidence = []
            evidence_is_unresolved = (
                not isinstance(raw_evidence, list)
                or (relationship != "uncertain" and not comparison_evidence)
            )
            if isinstance(evidence_catalog, list):
                for proof in comparison_evidence:
                    if isinstance(proof, dict) and isinstance(proof.get("evidence_id"), str):
                        catalog_item = evidence_by_id.get(proof["evidence_id"])
                        if catalog_item is None or catalog_item.get("work_id") != work_id:
                            evidence_is_unresolved = True
                            break
                    elif (not isinstance(proof, dict)
                          or proof.get("work_id") != work_id):
                        evidence_is_unresolved = True
                        break
            if evidence_is_unresolved:
                normalized = {
                    "work_id": work_id,
                    "relationship": "uncertain",
                    "statement": (
                        "The supplied source evidence does not resolve this work's relationship "
                        "to the nominated gap."
                    ),
                    "evidence": [],
                }
            else:
                normalized = {
                    "work_id": work_id,
                    "relationship": relationship,
                    "statement": statement.strip(),
                    "evidence": comparison_evidence,
                }
            if work_id in comparison_index_by_work:
                projected[comparison_index_by_work[work_id]] = {
                    "work_id": work_id,
                    "relationship": "uncertain",
                    "statement": "Duplicate comparison rows could not be reconciled without choosing between conflicting assessments.",
                    "evidence": [],
                }
                continue
            comparison_index_by_work[work_id] = len(projected)
            projected.append(normalized)
        comparisons = projected

    evidence = evidence_selections(value.get("evidence"))
    if not isinstance(evidence, list):
        evidence = []
    if isinstance(evidence_catalog, list):
        evidence = [item for item in evidence
                    if not (isinstance(item, dict)
                            and isinstance(item.get("evidence_id"), str)
                            and item["evidence_id"] not in evidence_by_id)]

    has_verified_full_text = any(
        isinstance(item, dict)
        and ((isinstance(item.get("evidence_id"), str)
              and (catalog_item := evidence_by_id.get(item["evidence_id"])) is not None
              and catalog_item.get("source_ref") in verified_full_text_refs)
             or (isinstance(item.get("source_ref"), str)
                 and item["source_ref"] in verified_full_text_refs))
        for item in evidence if isinstance(evidence, list)
    )

    def comparison_has_verified_full_text(item):
        for proof in item.get("evidence", []):
            if not isinstance(proof, dict):
                continue
            evidence_id = proof.get("evidence_id")
            catalog_item = evidence_by_id.get(evidence_id) if isinstance(evidence_id, str) else None
            source_ref = (catalog_item.get("source_ref") if catalog_item is not None
                          else proof.get("source_ref"))
            proof_work_id = (catalog_item.get("work_id") if catalog_item is not None
                             else proof.get("work_id"))
            if (isinstance(source_ref, str) and source_ref in verified_full_text_refs
                    and proof_work_id == item.get("work_id")):
                return True
        return False

    if isinstance(comparisons, list):
        for index, item in enumerate(comparisons):
            if (isinstance(item, dict) and item.get("relationship") == "solves"
                    and not comparison_has_verified_full_text(item)):
                comparisons[index] = {
                    "work_id": item["work_id"],
                    "relationship": "uncertain",
                    "statement": (
                        "The abstract-level evidence does not establish that this work solves "
                        "the nominated problem; verified full text is required."
                    ),
                    "evidence": [],
                }

    if state in {"refuted_by_prior_work", "eligible_for_experiment"}:
        if not has_verified_full_text:
            state = "insufficient_evidence"
        elif (state == "eligible_for_experiment"
              and isinstance(comparisons, list)
              and (not comparisons or any(item.get("relationship") in {"uncertain", "solves"}
                      or not comparison_has_verified_full_text(item)
                      for item in comparisons if isinstance(item, dict)))):
            state = "insufficient_evidence"
        elif (state == "refuted_by_prior_work"
              and isinstance(comparisons, list)
              and not any(item.get("relationship") == "solves"
                          and comparison_has_verified_full_text(item)
                          for item in comparisons if isinstance(item, dict))):
            state = "insufficient_evidence"
    projected = {}
    if state is not None:
        projected["state"] = state
    if isinstance(rationale, str):
        projected["rationale"] = rationale
    if isinstance(comparisons, list):
        projected["comparisons"] = comparisons
    if isinstance(checks, list):
        projected["checks"] = checks
    if isinstance(evidence, list):
        projected["evidence"] = evidence
    return projected


def evidence(items, sources, *, required=False, require_spans=False, windows=None, require_authority=False):
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
            if require_authority and not authoritative_source(source):
                raise ValidationError("scientific statements require an abstract or identity-verified full text")
            validate_source_span(item, source, require_span=require_spans,
                                 window=(windows or {}).get(item["source_ref"]))
        except ValidationError as exc:
            errors.append(f"evidence[{index}]: {exc}")
    if errors:
        raise ValidationError("; ".join(errors))


def validate_follow_up_result(value, work_orders, sources, query_refs, *, windows):
    def response_exact(value, fields, name):
        try:
            exact(value, fields, name)
        except ValidationError as exc:
            raise ModelContractError(str(exc)) from exc

    def response_text(value, name):
        try:
            _text(value, name)
        except ValidationError as exc:
            raise ModelContractError(str(exc)) from exc

    response_exact(value, {"orders"}, "survey follow-up result")
    rows = value["orders"]
    if not isinstance(rows, list) or len(rows) != len(work_orders):
        raise ModelContractError("survey follow-up must account for every work order")
    expected, seen = {order["id"] for order in work_orders}, set()
    for row in rows:
        response_exact(row, {"id", "status", "rationale", "evidence", "query_refs", "limitation", "next_action"},
              "survey follow-up disposition")
        response_text(row["id"], "survey follow-up order ID")
        response_text(row["status"], "survey follow-up status")
        if row["id"] not in expected or row["id"] in seen:
            raise ModelContractError("survey follow-up has an unassigned or duplicate order")
        seen.add(row["id"])
        if row["status"] not in {"resolved", "limited", "unresolved"}:
            raise ModelContractError("survey follow-up status must be resolved, limited or unresolved")
        for field in ("rationale", "next_action"):
            response_text(row[field], f"survey follow-up {field}")
        if not isinstance(row["limitation"], str):
            raise ModelContractError("survey follow-up limitation must be text")
        if not isinstance(row["query_refs"], list) or any(not isinstance(ref, str) for ref in row["query_refs"]):
            raise ModelContractError("survey follow-up query_refs must be a list of references")
        if any(ref not in query_refs for ref in row["query_refs"]):
            raise ModelContractError("survey follow-up cites an unrecorded targeted search")
        if not isinstance(row["evidence"], list) or any(
                not isinstance(proof, dict)
                or set(proof) != {"work_id", "source_ref", "quote", "start", "end", "quote_sha256"}
                or not isinstance(proof["source_ref"], str) for proof in row["evidence"]):
            raise ModelContractError("survey follow-up requires evidence rows with exact source span fields")
        if any(proof["source_ref"] not in windows for proof in row["evidence"]):
            raise ModelContractError("survey follow-up requires exact displayed source spans")
        try:
            evidence(row["evidence"], sources, required=row["status"] == "resolved",
                     require_spans=True, windows=windows, require_authority=True)
        except ValidationError as exc:
            raise ModelContractError(str(exc)) from exc
        if row["status"] == "limited" and (not row["query_refs"] or not row["limitation"].strip()):
            raise ModelContractError("limited survey follow-up requires targeted searches and an explicit limitation")


def statement(value, sources, *, work_id=None, require_spans=False):
    exact(value, {"text", "evidence"}, "literature statement")
    if value["text"] is None:
        if value["evidence"] != []:
            raise ValidationError("an unknown statement cannot claim supporting evidence")
        return
    _text(value["text"], "statement text")
    evidence(value["evidence"], sources, required=True, require_spans=require_spans, require_authority=True)
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


def checks(value, names, *, critique_checks=()):
    if not isinstance(value, list):
        raise ValidationError("checks must be an explicit list")
    seen = set()
    for check in value:
        fields = set(REVIEW_CHECK_FIELDS)
        if isinstance(check, dict) and check.get("check_id") in critique_checks:
            fields.add("affected_check_ids")
        if not isinstance(check, dict) or set(check) != fields:
            missing = sorted(fields - set(check)) if isinstance(check, dict) else sorted(fields)
            extra = sorted(set(check) - fields) if isinstance(check, dict) else []
            raise ValidationError(f"check {check.get('check_id') if isinstance(check, dict) else None!r} "
                                  f"has missing fields {missing} and unexpected fields {extra}; "
                                  f"requires exactly {sorted(fields)}")
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


def validate_survey_review(value, *, current_map=None):
    exact(value, {"checks", "rationale", *({"findings"} if "findings" in value else set())}, "survey review")
    checks(value["checks"], SURVEY_CHECKS)
    _text(value["rationale"], "survey review rationale")
    if current_map is None:
        return
    failed = {row["check_id"] for row in value["checks"] if row["outcome"] != "passed"}
    findings = value.get("findings", [])
    if not isinstance(findings, list):
        raise ModelContractError("survey findings must be a list")
    targets = {}
    for entry in current_map["entries"]:
        ref = current_map["entry_refs"][entry["work_id"]]
        targets[ref] = {field: entry[field] if field in {"inclusion", "reason"} else entry[field]["text"]
                        for field in ("inclusion", "reason", *MAP_FIELDS)}
    for relation in current_map["relationships"]:
        targets[relation["artifact_ref"]] = {"claim": relation["claim"]["text"]}
    grounded = set()
    for finding in findings:
        exact(finding, {"check_id", "target_ref", "field", "quote", "rationale"}, "survey finding")
        check_id, ref, field, quote = (finding[key] for key in ("check_id", "target_ref", "field", "quote"))
        if not all(isinstance(item, str) for item in (check_id, ref, field)) or check_id not in failed:
            raise ModelContractError("survey finding must bind a non-passed required check")
        _text(quote, "current assertion quote")
        _text(finding["rationale"], "survey finding rationale")
        text = targets.get(ref, {}).get(field)
        if not isinstance(text, str) or quote not in text:
            raise ModelContractError("survey finding must quote an exact current target field")
        grounded.add(check_id)
    if not (failed & {"source-fidelity", "map-support"}) <= grounded:
        raise ModelContractError("negative scientific survey checks require exact current assertion findings")


def validate_work_review(value, relationship_refs, *, entry=None, review_obligations=()):
    from scisaurus.core.surveys import critique_check_id, work_review_checks
    exact(value, {"checks", "rationale"}, "work review")
    critique_ids = {critique_check_id(item) for item in review_obligations}
    checks(value["checks"], work_review_checks(relationship_refs, review_obligations), critique_checks=critique_ids)
    _text(value["rationale"], "work review rationale")
    failed_claims = {check["check_id"] for check in value["checks"]
                     if check["outcome"] != "passed" and check["check_id"] not in critique_ids}
    for check in value["checks"]:
        if check["check_id"] not in critique_ids:
            continue
        affected = check["affected_check_ids"]
        if (not isinstance(affected, list) or any(not isinstance(item, str) for item in affected)
                or len(set(affected)) != len(affected)
                or (check["outcome"] == "passed" and affected)
                or (check["outcome"] != "passed" and (not affected or not set(affected) <= failed_claims))):
            raise ModelContractError("Each unresolved critique must identify its affected current entry field or relationship checks; passed critiques require an empty affected_check_ids list")
    if isinstance(entry, dict):
        for check in value["checks"]:
            field = check["check_id"]
            if (field in MAP_FIELDS and entry[field] == {"text": None, "evidence": []}
                    and check["outcome"] != "passed"):
                raise ModelContractError(
                    f"{field} is an explicit unknown, not an assertion that the work has no {field}; "
                    "verify the absence of an admitted claim, not a nonexistent scientific assertion")


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
             require_spans=require_spans, windows=windows, require_authority=True)
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
                 require_spans=require_spans, windows=windows, require_authority=True)
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
