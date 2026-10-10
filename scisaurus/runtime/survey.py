"""Bounded literature discovery, scoped mapping, and independent gap assessment."""
from copy import deepcopy
from collections import Counter
import hashlib
import itertools
import json
import math
import re
import signal
import threading
import time
import unicodedata

from scisaurus.core.errors import (ContractError, ModelContractError, ProviderConfigurationError,
                                   ProviderRateLimitError, QuotaExceededError, StateError, ValidationError)
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.source_spans import (bind as bind_source_spans, contains_legacy,
                                        expand_evidence, index_evidence, index_source_windows,
                                        source_window_ranges)
from scisaurus.core.surveys import (ABSTENTION_REASONS, RELATIONSHIP_SEMANTICS, SurveyGate,
                                   critique_check_id, is_explicit_abstention, work_review_checks)
from scisaurus.runtime.execution import SYSTEM, ExecutionRuntime, _invoke_worker
from scisaurus.runtime.evidence import scientific_input_recovery_contract
from scisaurus.runtime.literature_tree import LiteratureTree, SEARCH_PLANNERS, exploration_response_contract
from scisaurus.runtime.config import configured_worker_slots
from scisaurus.runtime.bibliographic_identity import normalize_doi, project_crossref_work, reconcile_result
from scisaurus.runtime.execution_policy import enforce_model_cost_limits
from scisaurus.runtime.models import (
    ModelCallError, ModelContextBudgetError, ModelResult, estimate_input_tokens, is_local_qwen_route,
    model_call_budget_remaining, model_token_budget_usage, model_token_budget_limits, role_config_for, role_routes_for,
    resumed_model_execution_config, with_runtime_cooldown_fallback,
)
from scisaurus.runtime.model_work import ModelWorkBlocked, ModelWorkCache
from scisaurus.runtime.literature import (
    SEARCH_SYNTAX, ProviderCooldownError, full_text_url_candidates,
    preferred_oa_pdf_url, provider_cooldown_seconds,
)
from scisaurus.runtime.operations import OperationsCell
from scisaurus.runtime.operation_adapters import get_adapter
from scisaurus.runtime.scores import exact, identifier
from scisaurus.runtime.survey_config import query_identity, search_plan_response_contract, validate_search_plan
from scisaurus.runtime.survey_config import validate_survey_config, search_query
from scisaurus.runtime.survey_records import (
    FOLLOW_UP_COMPLETION_CONTRACT, FOLLOW_UP_COMPLETION_REVIEW_CONTRACT,
    FOLLOW_UP_COMPLETION_REVIEW_LEGACY_CONTRACT, FOLLOW_UP_COMPLETION_REVIEW_EVIDENCE_CONTRACT,
    validate_follow_up_completion,
    follow_up_completion_basis, follow_up_completion_context, replay_follow_up_response,
    MAP_FIELDS, SURVEY_CHECKS, GAP_CHECKS, CRITIQUE_DISPOSITIONS, normalize_check_envelope,
    BODY_SECTION_MARKERS, authoritative_source, has_section_heading as _has_section_heading,
    normalize_gap_assessment_envelope, validate_map,
    validate_survey_review, validate_assessment, validate_work_review, survey_review_response_contract,
    normalize_survey_review_envelope, survey_review_assignment_identity,
    CURRENT_MAP_REVIEW_PROTOCOL,
    named_reference_ids, index_work_review_batch,
)
from scisaurus.runtime.time_policy import TimePolicy


_GAP_ASSESSMENT_INSTRUCTIONS = (
    "Return only the final JSON object, with no analysis transcript or preamble: {state:string,rationale:string,comparisons:[{work_id:string,relationship:string,statement:string,evidence:[{evidence_id:string}]}],checks:[{check_id:string,outcome:string,method:string,result:string}],evidence:[{evidence_id:string}]}. "
    "Select evidence_id values from evidence_catalog; their exact source quotations and offsets are attached deterministically. Never rewrite those quotations or reproduce the catalog in the response. "
    "If an additional passage is essential, an evidence item may instead contain {work_id,source_ref,quote}, quoting exact visible source text. "
    "Compare only decision-relevant closest prior works, not the entire work inventory. Keep findings concise and do not repeat the same evidence in explanatory prose. "
    "Each additional quote must be unique in its displayed source window. relationship is solves/partial/different/uncertain. "
    "state is refuted_by_prior_work, insufficient_evidence, or eligible_for_experiment. Run exactly all required checks. "
    "For every check, copy outcome from allowed_check_outcomes exactly; words such as pass, incomplete, inconclusive, or partial are invalid. "
    "A prior solution supported by decisive full-text quotes refutes the gap even if global search is incomplete. "
    "For decisive states all checks must pass and evidence must include verified full_text sources. Abstracts alone cannot authorize a decisive state. "
    "Use insufficient_evidence if access, source windows, missing closest work, or incomparable conditions prevent the judgment. "
    "A decisive state is valid only when every required check has outcome=passed. If any check is insufficient_evidence, failed, or check_failed, state must be insufficient_evidence. "
    "For insufficient_evidence, keep comparisons different or uncertain as warranted by the captured text; do not relabel an abstract sentence as full_text. "
    "Assess whether each coverage gap or omitted source window can change the nominated comparison. "
    "Use insufficient_evidence for decision-critical omissions; peripheral access failures or bounded source windows alone do not veto a supported comparison. "
    "Explain material coverage limits in the checks and rationale. Never infer support from undisplayed text. "
    "When only an abstract is captured, distinguish a comparison not reported in that abstract from a comparison not performed by the full study. Abstract silence cannot establish the latter. "
    "When state is decisive, every comparison evidence item for that comparison must remain attached to an exact verified full_text quotation; prefer short contiguous prose spans over rendered equations. "
    "For refuted_by_prior_work or eligible_for_experiment, cite only the listed verified_full_text_refs for any decisive comparison; "
    "if no listed full-text quote directly supports the comparison, set the state to insufficient_evidence and use relationship=uncertain. "
    "Eligibility requires meaningful, testable distinction, no prior solution or unresolved comparison, and adequate search coverage. "
    "It authorizes an experiment under the stated scope, never publication-ready novelty. Do not force a positive finding to finish the task. "
    "Every comparison evidence item must have the same work_id as that comparison. "
    "If no same-work evidence is available, omit it or use relationship=uncertain with evidence=[]."
)



def normalized(text):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def acquisition_succeeded(record):
    return isinstance(record, dict) and (
        record.get("outcome") in {"ok", "empty"}
        or (record.get("outcome") == "not_found"
            and record.get("request", {}).get("operation") == "work"
            and record.get("provider_http_status") == 404))


def countersearch_lineage(control, store, *, score_ref, question, nomination_ref,
                          plan_ref, survey_ref=None, query_refs=None):
    """Replay hypothesis, planning snapshot, and acquired-query ownership without dispatch."""
    gate = SurveyGate(control, store)
    def artifact(ref):
        manifest, raw = gate._artifact(ref, current=False)
        return manifest, gate._json(raw, ref)
    def accepted_snapshot(ref):
        manifest, body = artifact(ref)
        if (body.get("schema_version") not in {"literature-survey-2", "literature-survey-3"}
                or body.get("score_ref") != score_ref):
            raise StateError("counter-search snapshot belongs to another survey score")
        acceptance = gate._accepted_event("survey.accepted", "survey_ref", ref)
        if {"ref": ref, "body_hash": manifest["body_hash"]} not in acceptance.get("evidence_pins", []):
            raise StateError("counter-search snapshot has no matching historical acceptance")
        _, mapped = artifact(body["map_ref"])
        _, score = artifact(score_ref)
        if mapped.get("question") != question or score.get("survey", {}).get("question") != question:
            raise StateError("counter-search snapshot changed the declared question")
        return body
    try:
        nominee = gate.require_current_nomination(nomination_ref)
        nominee_manifest, _ = artifact(nomination_ref)
        if nominee_manifest.get("score_ref") != score_ref or nominee_manifest["author"] != "research.gap-proposer":
            raise StateError("counter-search nomination has a different owner or score")
        accepted_snapshot(nominee["survey_ref"])
        plan, body = artifact(plan_ref)
        if (store.head(plan["artifact_id"])["artifact_ref"] != plan_ref
                or plan["author"] != "methods.novelty-challenger" or plan.get("score_ref") != score_ref
                or body.get("nomination_ref") != nomination_ref):
            raise StateError("counter-search plan has a different owner, score, or nomination")
        accepted_snapshot(body["survey_ref"])
        proposal = {key: body.get(key) for key in ("queries", "rationale")}
        exact(proposal, {"queries", "rationale"}, "counter-search plan")
        if (not isinstance(proposal["queries"], list) or not proposal["queries"]
                or any(not isinstance(query, str) or not query.strip() for query in proposal["queries"])
                or not isinstance(proposal["rationale"], str) or not proposal["rationale"].strip()):
            raise StateError("counter-search plan has no valid search proposal")
        subjects = {item["ref"] for item in plan["inputs"] if item.get("purpose") == "subject"}
        executions = [ref for ref in subjects if ref.startswith("artifact:command/executions/")]
        if not {nomination_ref, body["survey_ref"]}.issubset(subjects) or len(executions) != 1:
            raise StateError("counter-search plan omitted its owned execution or prerequisites")
        execution, result = artifact(executions[0])
        if execution["author"] != plan["author"] or execution.get("score_ref") != score_ref or len(execution["inputs"]) != 1:
            raise StateError("counter-search plan execution has a different owner or score")
        context, params = artifact(execution["inputs"][0]["ref"])
        assignment = json.loads(params["prompt"])
        if (context["author"] != plan["author"] or context.get("score_ref") != score_ref
                or params.get("role") != plan["author"] or assignment.get("phase") != "counter_plan"
                or assignment.get("question") != question or assignment.get("nomination_ref") != nomination_ref
                or assignment.get("survey_ref") != body["survey_ref"]
                or assignment.get("prerequisite_survey_ref") != body["survey_ref"]
                or assignment.get("gap") != {key: nominee[key] for key in ("id", "statement")}
                or ModelResult(**result).json_object(allow_missing_closers=True) != proposal):
            raise StateError("counter-search plan changed its admitted hypothesis or planning input")
        current_body = accepted_snapshot(survey_ref) if survey_ref else None
        survey_current = False
        if survey_ref:
            try:
                gate.require_current(survey_ref)
            except ContractError:
                pass
            else:
                survey_current = True
        refs = query_refs if query_refs is not None else (current_body or {}).get("query_refs", [])
        successful = {}
        for ref in refs:
            receipt, row = artifact(ref)
            request = row.get("request", {})
            query = request.get("query")
            if row.get("plan_ref") != plan_ref or request.get("operation") != "search" or not query or not acquisition_succeeded(row):
                continue
            inputs = {item["ref"] for item in receipt["inputs"] if item.get("purpose") == "subject"}
            if (receipt["artifact_type"] != "query_record" or receipt["author"] != plan["author"]
                    or receipt.get("score_ref") != score_ref or row.get("role") != plan["author"]
                    or not {plan_ref, row.get("execution_ref")}.issubset(inputs)):
                raise StateError("counter-search query receipt has a different execution owner")
            produced, _ = artifact(row["execution_ref"])
            if produced["author"] != plan["author"] or produced.get("score_ref") != score_ref:
                raise StateError("counter-search query execution has a different owner or score")
            adapter = "crossref" if row.get("provider") == "crossref" else "openalex"
            _, _, captured, params = gate._recorded_execution(
                row["execution_ref"], plan["author"], operation=adapter, task_kinds={"retrieval"})
            expected = row.get("provider_request") if adapter == "crossref" else request
            if ({key: value for key, value in params.items() if key != "client"} != expected
                    or captured.get("outcome") != row.get("outcome")
                    or adapter == "crossref" and (params.get("query") != query
                        or params.get("cursor") != request.get("cursor"))):
                raise StateError("counter-search query differs from its exact owned retrieval execution")
            successful[query_identity(query)] = ref
        required = [query_identity(query) for query in proposal["queries"]]
        complete_queries = all(query in successful for query in required)
        acquired = [successful[query] for query in required] if complete_queries else []
        return {"nomination_ref": nomination_ref, "origin_survey_ref": nominee["survey_ref"],
                "planning_survey_ref": body["survey_ref"], "survey_ref": survey_ref, "query_refs": acquired,
                "queries_complete": complete_queries, "survey_current": survey_current,
                "complete": bool(current_body and complete_queries and set(acquired).issubset(current_body["query_refs"]))}
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        raise StateError(f"counter-search dependency lineage is invalid: {exc}") from exc


_SEARCH_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "by", "can", "does", "for", "from",
    "how", "if", "in", "into", "is", "of", "on", "or", "that", "the", "this",
    "to", "under", "versus", "what", "when", "which", "with", "without", "across",
    "between", "within", "using", "based", "model", "models", "study", "studies",
})

_SOURCE_ACCESS_UNAVAILABLE = frozenset({
    "access_denied", "auth_required", "robots_denied", "robots_unavailable",
})
_SOURCE_EVIDENCE_POLICY = (
    "Retained verified full text remains authoritative. When full-text access is unavailable, "
    "use the registered captured abstract for claims supported by its exact text, with abstract scope. "
    "Do not request the same inaccessible full text again or count an abstract as verified full text. "
    "Missing full text alone does not invalidate a bounded abstract-supported survey or map. "
    "An abstract's silence cannot establish what the full paper did not evaluate. "
    "Decision-critical full-text requirements and scientific novelty gates remain unresolved without "
    "their required evidence. Rate limits require provider recovery, not an abstract fallback."
)


_CRITIQUE_CONTEXT_PROTOCOL = "literature-critique-transition-3"


def source_fidelity_review_contract():
    return {
        "protocol": "literature-source-fidelity-5",
        "analyst_mapping_scope": "Separate article-reported facts from analyst relevance judgments. A bounded relevance or method-analogy judgment must identify a captured source-supported property, the specific question component it informs, and limits on transfer across species, phases, conditions, or models. The article need not discuss the future question itself. Shared terms or an unrelated accurate summary alone are insufficient. Do not attribute a proposed experiment, parameter transfer, or analyst inference to the article. Background citing prior work is not a measurement or investigation performed by the current study.",
        "question_status": "A research question is not an established claim or a required survey conclusion. Its answer remains undecided by this acceptance decision.",
        "source_fidelity_scope": "Assess whether every clause reported as an article fact is entailed by its cited source. Assess analyst relevance judgments separately under analyst_mapping_scope, including the source entailment of their stated premises. Do not require any captured source to answer or directly address the research question. An unanswered question is a downstream gap, not a failure of an accurately represented source-supported claim.",
        "screening_scope": "Check inclusion and reason separately from claim entailment. Every included work must have a source-supported connection to the declared question's mechanism, phenomenon, or method, and the reason must identify that connection. Matching the work's own topic, shared terminology, or a correctly summarized but unrelated source does not establish relevance. A relevant source may support a partial or general claim without answering the research question; do not reject it solely for that absence. Exclude or defer a work whose evidentiary connection cannot be established from its captured source.",
        "non_assertions": "Uncertain screening is an unresolved decision, not a claim that a work is irrelevant or lacks sources. Excluded, deferred, and null fields do not assert scientific support. Missing support for an absent assertion is not a source-fidelity failure. An exclusion reason that asserts irrelevance still requires evidence-based screening review.",
        "controller_status_scope": "A hash-bound controller_abstention records procedural non-admission, not a scientific exclusion or source-unavailability claim. Verify its entry binding and retained status against that receipt, not article text. Do not require an uncertain non-admitted entry to supply an affirmative relevance claim. Independently adjudicate every critique: withdrawal can remove an unsupported current assertion, but does not resolve the original scientific question or prove the original assertion correct. Fail a remaining assertion, false status, or unsupported exclusion; do not fail merely because an assertion remains unmade.",
        "claim_coverage_scope": "Evaluate the assertions actually retained. A source-supported limitation does not fail because other limitations are omitted unless the retained text claims completeness. Omitted qualifiers or exceptions that alter a retained assertion remain entailment defects. Non-null scientific fields require source support even on excluded or uncertain entries. A general mechanism or method analogy may establish bounded relevance despite differences in species or experimental conditions; those differences restrict transferability and must not be converted into an exact-question answer.",
        "numerical_scope": "Retained numerical results must preserve the source-defined conditions and assumptions needed to interpret them, such as temperature for a reaction-rate coefficient, phase/species, and assumptions behind inferred yields or budgets. Preserve the meaning of reported uncertainty (standard deviation, standard error, confidence interval, or explicitly unspecified); a plus/minus magnitude alone does not identify that convention. These qualifiers may be in any retained entry field with captured support. A reviewer rationale cannot supply conditions missing from the entry. Do not require every protocol detail or infer a false pressure dependence merely because a pressure range is omitted. If captured evidence leaves an essential condition unknown, state that limit explicitly rather than inventing it.",
        "downstream_decisions": "Gap nomination and counter-search assess novelty and question coverage; experiments test the research question; manuscript peer review judges the final contribution.",
        "failure_basis": "Identify a specific unsupported assertion, misrepresented source, evidence-based screening error, or inconsistent accounting. Fail unsupported minor clauses as well as unsupported main claims, including qualifiers in relationship claims; each relationship clause must be supported by its cited works. Record incomplete coverage honestly without requiring exhaustive retrieval or an answer to the research question.",
    }


def normalize_gap_nomination(value):
    """Bind malformed transport identifiers to the unchanged hypothesis text."""
    if (not isinstance(value, dict) or set(value) != {"id", "statement"}
            or not isinstance(value.get("id"), str) or not value["id"].strip()
            or not isinstance(value.get("statement"), str) or not value["statement"].strip()):
        return value
    try:
        identifier(value["id"])
    except ValidationError:
        return {**value, "id": "gap-" + hashlib.sha256(canonical_bytes(value["statement"])).hexdigest()[:60]}
    return value


def normalize_survey_repair_owners(value, entries, relationships):
    """Route exact relationship grants by their immutable source ownership."""
    if not isinstance(value, dict) or set(value) != {"repairs"} or not isinstance(value["repairs"], list):
        return value
    original = value
    value = deepcopy(value)
    for item in value["repairs"]:
        if not isinstance(item, dict):
            return original
        if "entry_fields" not in item and isinstance(item.get("relationship_refs"), list) and item["relationship_refs"]:
            item["entry_fields"] = []
        if "relationship_refs" not in item and isinstance(item.get("entry_fields"), list) and item["entry_fields"]:
            item["relationship_refs"] = []
        if (not isinstance(item, dict) or set(item) != {"entry_ref", "entry_fields", "relationship_refs", "rationale"}
                or not isinstance(item["entry_ref"], str) or not isinstance(item["rationale"], str)
                or not item["rationale"].strip()):
            return original
        entry_ref = entries.get(item["entry_ref"], item["entry_ref"])
        if (not item["entry_fields"] and isinstance(item["relationship_refs"], list) and entry_ref in relationships
                and entry_ref in item["relationship_refs"]):
            entry_ref = entries.get(relationships[entry_ref]["source"], entry_ref)
            item["entry_ref"] = entry_ref
        if entry_ref not in entries.values():
            return original
        fields, refs = item["entry_fields"], item["relationship_refs"]
        if (not isinstance(fields, list) or not all(isinstance(field, str) for field in fields)
                or not set(fields) <= {"inclusion", "reason", *MAP_FIELDS}
                or not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs)
                or not fields and not refs
                or any(ref not in relationships
                       or relationships[ref]["source"] not in entries for ref in refs)):
            return original
    grants = {}
    def grant(ref, rationale):
        item = grants.setdefault(ref, {"entry_ref": ref, "entry_fields": [], "relationship_refs": [], "rationales": []})
        if rationale not in item["rationales"]:
            item["rationales"].append(rationale)
        return item
    for item in value["repairs"]:
        if item["entry_fields"]:
            grant(entries.get(item["entry_ref"], item["entry_ref"]), item["rationale"])["entry_fields"].extend(item["entry_fields"])
        for ref in item["relationship_refs"]:
            owner = entries[relationships[ref]["source"]]
            grant(owner, item["rationale"])["relationship_refs"].append(ref)
    return {"repairs": [{"entry_ref": ref, "entry_fields": sorted(set(item["entry_fields"])),
                         "relationship_refs": sorted(set(item["relationship_refs"])),
                         "rationale": "\n".join(item["rationales"])} for ref, item in sorted(grants.items())]}


_HTTP_STATUS_FIELDS = frozenset({"status", "status_code", "http_status", "provider_http_status"})


def _has_http_429(value):
    if isinstance(value, dict):
        return any((key in _HTTP_STATUS_FIELDS
                    and type(item) is int and item == 429) or _has_http_429(item)
                   for key, item in value.items())
    return isinstance(value, list) and any(_has_http_429(item) for item in value)


def _content_tokens(text):
    return {
        token for token in normalized(str(text)).split()
        if len(token) >= 3 and token not in _SEARCH_STOP_WORDS and not token.isdigit()
    }


def _normalize_scoped_entry_updates(wid, updates):
    """Normalize the per-work wrapper used by model repair responses."""
    if not isinstance(updates, dict) or wid not in updates:
        return updates
    if set(updates) != {wid}:
        raise ValidationError("scoped map repair changed an ungranted work")
    nested = updates[wid]
    if not isinstance(nested, dict):
        raise ModelContractError("scoped map repair work updates must be an explicit object")
    return nested


def _map_withdrawn_fields(fields):
    """A screening decision cannot survive withdrawal of its rationale."""
    return list(dict.fromkeys([*fields, *(["inclusion"] if "reason" in fields else [])]))


def apply_scoped_map_repair(wid, previous, old_relationships, feedback, patch, *,
                            reject_ungranted_changes=False):
    """Compose a narrow repair without asking a worker to reproduce protected state.

    A repair may update a subset of the fields that failed review; omitted
    fields remain pinned to the previous entry and are independently reviewed
    again. A live provider response must use ``reject_ungranted_changes=True``
    so an actual out-of-scope edit is a validation failure rather than a
    silently accepted response.
    """
    exact(patch, {"entry_updates", "relationships"}, "scoped map repair")
    updates = _normalize_scoped_entry_updates(wid, patch["entry_updates"])
    relations = patch["relationships"]
    granted_fields = set(feedback["entry_fields"])
    if not isinstance(updates, dict):
        raise ValidationError("scoped map repair entry updates must be an object")
    ungranted_fields = set(updates) - granted_fields
    if ungranted_fields and reject_ungranted_changes:
        raise ValidationError("scoped map repair changed an ungranted entry field")
    if ungranted_fields:
        updates = {field: updates[field] for field in updates if field in granted_fields}
    # A repair worker sometimes echoes the protected fields from the previous
    # entry along with the requested patch.  The control plane owns those
    # fields, so project the response to the explicit grant before composing
    # it.  No out-of-scope value can affect the accepted entry.
    updates = {field: updates[field] for field in updates if field in granted_fields}
    if not isinstance(relations, list):
        raise ValidationError("scoped map repair relationships must be a list")
    targets = set(feedback["relationship_targets"])
    if not granted_fields and not targets:
        raise ValidationError("scoped map repair requires a granted entry field or relationship")
    if any(not isinstance(relation, dict) or relation.get("source") != wid
           or relation.get("target") not in targets for relation in relations):
        raise ValidationError("scoped map repair changed an ungranted relationship")
    entry = deepcopy(previous)
    entry.update(updates)
    granted_refs = feedback.get("relationship_refs")
    if granted_refs is not None:
        known = {relation.get("artifact_ref"): relation for relation in old_relationships}
        if (not isinstance(granted_refs, list) or any(ref not in known for ref in granted_refs)
                or {known[ref]["target"] for ref in granted_refs} != targets):
            raise ValidationError("scoped map repair has stale or inconsistent relationship pins")
    retained = [{key: relation[key] for key in ("source", "target", "kind", "claim")}
                for relation in old_relationships
                if (relation.get("artifact_ref") not in granted_refs if granted_refs is not None
                    else relation["target"] not in targets)]
    return {"entries": [entry], "relationships": [*retained, *deepcopy(relations)]}


def normalize_map_relationships(value):
    """Canonicalize the one legacy flat relationship shape emitted by workers.

    The public map contract stores ``claim`` as a Statement. Some model
    responses place that Statement's ``evidence`` beside a string ``claim``
    instead. This conversion is deliberately exact-shape and lossless; all
    evidence and the strict map validator remain in force after conversion.
    Unknown fields or other malformed shapes are left untouched and must fail
    validation rather than being silently discarded.
    """
    if not isinstance(value, dict) or not isinstance(value.get("relationships"), list):
        return value
    normalized = deepcopy(value)
    relationships = []
    for relation in normalized["relationships"]:
        if (isinstance(relation, dict)
                and set(relation) == {"source", "target", "kind", "claim", "evidence"}
                and isinstance(relation["claim"], str)
                and isinstance(relation["evidence"], list)):
            relationships.append({
                "source": relation["source"],
                "target": relation["target"],
                "kind": relation["kind"],
                "claim": {"text": relation["claim"], "evidence": relation["evidence"]},
            })
        else:
            relationships.append(relation)
    normalized["relationships"] = relationships
    return normalized


def normalize_map_worker_response(value, *, work_id, all_work_ids, sources,
                                 windows, entry_editable=True, previous=None,
                                 review_feedback=None, projection_issues=None):
    """Project a worker response onto one safe map assignment.

    Map relationships are optional and statement fields can be unknown. A
    malformed optional relation is therefore dropped, while a field whose
    quotation cannot be bound to the displayed source window becomes an
    explicit unknown. This preserves independently verifiable work without
    admitting unsupported claims or paying for a whole-assignment retry.
    """
    null_statement = {"text": None, "evidence": []}
    issues = projection_issues if isinstance(projection_issues, list) else None

    def report_issue(kind, **details):
        if issues is not None:
            issues.append({"kind": kind, "work_id": work_id, **details})

    source_lookup = {item["source_ref"]: item for item in sources
                     if isinstance(item, dict)
                     and isinstance(item.get("source_ref"), str)}

    def safe_statement(raw, *, required_work_ids=(), field="statement"):
        if not isinstance(raw, dict):
            if raw is not None:
                report_issue("malformed_statement_withdrawn", field=field)
            return deepcopy(null_statement)
        text = raw.get("text")
        proofs = raw.get("evidence")
        if not isinstance(text, str) or not text.strip() or not isinstance(proofs, list) or not proofs:
            if text is not None or proofs not in ([], None):
                report_issue("unbound_statement_withdrawn", field=field)
            return deepcopy(null_statement)
        projected = []
        cited_work_ids = set()
        for proof in proofs:
            if (not isinstance(proof, dict)
                    or not all(isinstance(proof.get(key), str) and proof[key]
                               for key in ("work_id", "source_ref", "quote"))):
                report_issue("malformed_citation_withdrawn", field=field)
                return deepcopy(null_statement)
            source = source_lookup.get(proof["source_ref"])
            if (source is None or proof["work_id"] not in all_work_ids
                    or source.get("work_id") != proof["work_id"]):
                report_issue("unbound_citation_withdrawn", field=field)
                return deepcopy(null_statement)
            cited_work_ids.add(proof["work_id"])
            projected.append({key: proof[key] for key in (
                "work_id", "source_ref", "quote", "start", "end", "quote_sha256"
            ) if key in proof})
        if not set(required_work_ids).issubset(cited_work_ids):
            report_issue("wrong_work_citation_withdrawn", field=field)
            return deepcopy(null_statement)
        try:
            bound = bind_source_spans(
                {"evidence": projected},
                {ref: source for ref, source in ((item["source_ref"], item)
                                                  for item in sources)},
                windows=windows,
            )
        except (ValidationError, KeyError, TypeError, ValueError):
            report_issue("source_span_not_verified", field=field)
            return deepcopy(null_statement)
        return {"text": text.strip(), "evidence": bound["evidence"]}

    def safe_relationships(raw_relations, *, allowed_targets=None):
        if not isinstance(raw_relations, list):
            return []
        allowed_targets = ({target for target in allowed_targets if isinstance(target, str)}
                           if allowed_targets is not None else None)
        output, seen = [], set()
        for relation in raw_relations:
            if not isinstance(relation, dict):
                continue
            source, target, kind = (relation.get("source"), relation.get("target"),
                                    relation.get("kind"))
            if (source != work_id or not isinstance(target, str)
                    or target not in all_work_ids or target == work_id
                    or (allowed_targets is not None and target not in allowed_targets)
                    or not isinstance(kind, str)
                    or kind not in {"extends", "contradicts", "compares", "related"}):
                report_issue("ungranted_relationship_dropped")
                continue
            key = (source, target, kind)
            if key in seen:
                continue
            claim = safe_statement(relation.get("claim"),
                                   required_work_ids=(source, target),
                                   field="relationship")
            if claim["text"] is None:
                report_issue("unsupported_relationship_dropped", target=target, relation_kind=kind)
                continue
            seen.add(key)
            output.append({"source": source, "target": target,
                           "kind": kind, "claim": claim})
        return output

    value = normalize_map_relationships(value)
    if review_feedback is not None:
        exact(value, {"entry_updates", "relationships"}, "scoped map repair")
        raw_updates = value["entry_updates"]
        if isinstance(raw_updates, dict) and work_id in raw_updates:
            if not isinstance(raw_updates[work_id], dict):
                raise ModelContractError("scoped map repair work updates must be an explicit object")
            for other_work in set(raw_updates) - {work_id}:
                report_issue("ungranted_work_ignored", work_id=other_work)
            raw_updates = raw_updates[work_id]
        if not isinstance(raw_updates, dict):
            raise ModelContractError("scoped map repair entry updates must be an explicit object")
        if not isinstance(value["relationships"], list):
            raise ModelContractError("scoped map repair relationships must be an explicit list")
        granted = set(review_feedback.get("entry_fields", []))
        for field in set(raw_updates) - granted:
            report_issue("ungranted_entry_field_ignored", field=str(field))
        updates = {}
        fallback_reason = (
            "The captured source text did not support a verifiable screening rationale."
        )
        for field in review_feedback.get("entry_fields", []):
            if field not in raw_updates:
                continue
            raw = raw_updates.get(field)
            if field == "inclusion":
                updates[field] = (raw if isinstance(raw, str)
                                  and raw in {"included", "excluded", "uncertain"}
                                  else "uncertain")
            elif field == "reason":
                updates[field] = raw.strip() if isinstance(raw, str) and raw.strip() else fallback_reason
            elif field in MAP_FIELDS:
                updates[field] = safe_statement(
                    raw, required_work_ids=(work_id,), field=field)
        targets = review_feedback.get("relationship_targets", [])
        raw_relations = value.get("relationships", []) if isinstance(value, dict) else []
        return {"entry_updates": updates,
                "relationships": safe_relationships(raw_relations, allowed_targets=targets)}

    entry_rows = value.get("entries") if isinstance(value, dict) else None
    matches = [row for row in entry_rows if isinstance(row, dict)
               and row.get("work_id") == work_id] if isinstance(entry_rows, list) else []
    raw_entry = matches[0] if len(matches) == 1 else {}
    if len(matches) != 1:
        report_issue("assigned_entry_missing_or_duplicated")
    if not entry_editable and isinstance(previous, dict):
        changed_fields = [key for key in set(raw_entry) | set(previous)
                          if raw_entry.get(key) != previous.get(key)]
        for field in sorted(changed_fields):
            report_issue("ungranted_entry_field_ignored", field=str(field))
        entry = deepcopy(previous)
    else:
        allowed_entry_fields = {"work_id", "inclusion", "reason", *MAP_FIELDS}
        for field in set(raw_entry) - allowed_entry_fields:
            report_issue("ungranted_entry_field_ignored", field=str(field))
        entry = {
            "work_id": work_id,
            "inclusion": (raw_entry.get("inclusion")
                          if isinstance(raw_entry.get("inclusion"), str)
                          and raw_entry["inclusion"] in {"included", "excluded", "uncertain"}
                          else "uncertain"),
            "reason": (raw_entry.get("reason").strip()
                       if isinstance(raw_entry.get("reason"), str)
                       and raw_entry["reason"].strip()
                       else "The captured source text did not support a verifiable screening rationale."),
        }
        for field in MAP_FIELDS:
            entry[field] = safe_statement(
                raw_entry.get(field), required_work_ids=(work_id,), field=field)
    raw_relations = value.get("relationships", []) if isinstance(value, dict) else []
    relationships = safe_relationships(raw_relations)
    if (entry_editable and entry["inclusion"] == "included"
            and all(entry[field]["text"] is None for field in MAP_FIELDS)
            and not relationships):
        entry["inclusion"] = "uncertain"
        entry["reason"] = ABSTENTION_REASONS["unverified_map"]
        report_issue("claimless_screening_withdrawn")
    return {"entries": [entry], "relationships": relationships}


def overlay_post_checkpoint_relationships(relationships, checkpoint_created_at, candidates):
    """Recover validated relationship versions newer than an aggregate checkpoint."""
    restored = dict(relationships)
    for record, relation in candidates:
        key = "-".join(relation[field] for field in ("source", "target", "kind"))
        # A checkpoint that still names this relationship cannot knowingly
        # delete it; advance it to its validated head even when a later restart
        # accidentally wrote another aggregate checkpoint from the stale ref.
        # A relationship absent from the checkpoint is added only when its
        # validated version postdates that checkpoint, preserving deletions.
        if key not in restored and record["created_at"] <= checkpoint_created_at:
            continue
        restored[key] = {**relation, "artifact_ref": record["artifact_ref"]}
    return restored


class SurveyRunner(LiteratureTree, ExecutionRuntime):
    def __init__(self, project_dir, config, *, on_progress=None, resume_policy=None,
                 provider_fallback=None, model_call_budget_scopes=None, model_budget_delegation=None,
                 work_orders=None, review_obligations=None, model_execution_config=None):
        if provider_fallback is not None and provider_fallback != "crossref_metadata":
            raise ValidationError("unsupported survey provider fallback: " + str(provider_fallback))
        self.control = None
        try:
            self._initialize_survey(project_dir, config, on_progress=on_progress, resume_policy=resume_policy,
                provider_fallback=provider_fallback, model_call_budget_scopes=model_call_budget_scopes,
                model_budget_delegation=model_budget_delegation, work_orders=work_orders,
                review_obligations=review_obligations, model_execution_config=model_execution_config)
        except BaseException:
            if self.control is not None:
                self.control.close()
            raise

    def _initialize_survey(self, project_dir, config, *, on_progress, resume_policy,
                           provider_fallback, model_call_budget_scopes, model_budget_delegation, work_orders,
                           review_obligations, model_execution_config):
        from scisaurus.runtime.survey_config import validate_survey_work_orders
        orders = validate_survey_work_orders(
            config.get("work_orders") if work_orders is None else work_orders)
        self.work_orders = [order for order in orders if order["kind"] == "literature_expansion"]
        self.follow_up_ref = None
        self.follow_up_result = None
        self._follow_up_decisions = set()
        validated = validate_survey_config(config)
        execution_model = None
        if model_execution_config is not None:
            candidate = resumed_model_execution_config(
                with_runtime_cooldown_fallback(validated["model"]),
                with_runtime_cooldown_fallback(model_execution_config))
            execution_model = validate_survey_config({**validated, "model": candidate})["model"]
        super().__init__(project_dir, validated, worker_target=_invoke_worker,
                         on_progress=on_progress, resume_policy=resume_policy,
                         model_call_budget_scopes=model_call_budget_scopes,
                         model_budget_delegation=model_budget_delegation)
        self.score = self.config["survey"]
        self._model_execution_config = execution_model
        self.bounds = self.score["search"]
        self.declared_search_bounds = deepcopy(self.bounds)
        self.operations = OperationsCell(self.control, self.store, project_id=self.config["project_id"])
        self.gate = SurveyGate(self.control, self.store)
        self.review_obligations = self._validate_review_obligations([
            *(review_obligations or []), *self.gate.independent_review_obligations(self.score["question"])])
        self.worker_slots = configured_worker_slots(self.config["limits"])
        self.bibliography_mode = "openalex"
        self.bibliography_fallback_policy = self.score.get(
            "bibliography_fallback", "crossref_metadata")
        if provider_fallback is not None:
            # A resumed run must pass ResumeController's exact immutable
            # config check.  Provider recovery is therefore an explicit
            # runtime route, not a mutation of the accepted run input.
            self.bibliography_fallback_policy = provider_fallback
        self.bibliography_fallback_reason = None
        self.bibliography_fallback_capability = None
        self.work_budget_adjustments = []
        self.time_policy = self._plan_work_budget()
        self.time_policy.started_at = self.started
        self.deadline = min(self.deadline, self.started + self.time_policy.hard_seconds)
        # Provider pacing is independent from request retries.  The former
        # spaces successful calls across a provider's rate window, while the
        # latter handles transient failures for one request.  Keeping a
        # monotonic schedule means a slow response naturally consumes the
        # interval instead of adding another unnecessary sleep.
        self.provider_intervals = {
            name: float(value)
            for name, value in self.score.get("provider_intervals", {}).items()
        }
        self.next_provider_at = {name: self.started for name in self.provider_intervals}
        self.provider_waits = []
        self.work_records, self.works, self.source_docs, self.source_records = {}, {}, {}, {}
        self.identity_records = {}
        self.aliases, self.dois, self.analysis_records, self.analyzed_basis = {}, {}, {}, {}
        self.relationships, self.bindings, self.capability_ids = {}, {}, []
        self.work_reviews, self.reviewed_basis = {}, {}
        self._survey_acceptance_pending = False
        self._countersearch_active = False
        self.query_refs, self.search_log, self.expansion_log, self.gaps, self.time_decisions = [], [], [], [], []
        self.api_calls, self.identity_calls, self.serial, self.survey_revision = 0, 0, 0, 0
        self.expanded, self.full_text_attempted = set(), set()
        self.exploration_tree = None
        self._tree_admitted_reads = None
        self._tree_read_order = None
        self._active_tree_action = None
        self.survey_ref, self.assessment_ref, self.register_ref = None, None, None
        self.map_record = None
        self.nomination = None
        self.nomination_record = None
        self.counter_plan_record = None
        self.counter_query_refs = []
        self.counter_queries_complete = False
        self.countersearch_complete = False
        self.follow_up_discovery_current = False
        if self.work_orders:
            digest = hashlib.sha256(canonical_bytes(self.work_orders)).hexdigest()
            logical = f"command/survey-follow-up/{digest}"
            packet = self.store.head(logical)
            if packet is None:
                packet = self._publish(logical, "note", {
                    "schema_version": "survey-follow-up-1", "work_orders": self.work_orders,
                    "run_config_ref": self.store.head("inputs/run-config")["artifact_ref"],
                }, "command.controller", subjects=[self.store.head("inputs/run-config")["artifact_ref"]])
            self.follow_up_ref = packet["artifact_ref"]
        if self.resume_session:
            self.time_policy.hard_seconds = min(
                self.time_policy.hard_seconds, self.resume_session["additional_seconds"])
            self.time_policy.target_seconds = min(self.time_policy.target_seconds, self.time_policy.hard_seconds)
            self.time_policy.first_result_seconds = min(
                self.time_policy.first_result_seconds, self.time_policy.target_seconds)
            self._restore()

    def _follow_up_assignment(self, assignment):
        brief = self.score.get("design_brief")
        if brief is not None:
            from scisaurus.runtime.material_development import implementation_evidence_scope
            assignment = {**assignment, "implementation_evidence_scope": implementation_evidence_scope(brief)}
        if assignment.get("phase") != "survey_operation_completion":
            assignment = {**assignment, "scientific_input_recovery": scientific_input_recovery_contract()}
        if "sources" in assignment or "coverage" in assignment:
            assignment = {**assignment, "source_evidence_policy": _SOURCE_EVIDENCE_POLICY}
        if not getattr(self, "work_orders", None):
            return assignment
        orders = assignment.get("work_orders", self.work_orders)
        if not isinstance(orders, list) or any(order not in self.work_orders for order in orders):
            raise ValidationError("model assignment contains an unassigned survey work order")
        return {**assignment, "work_orders": deepcopy(orders),
                "follow_up_ref": self.follow_up_ref,
                **({"named_reference_inventory": self._named_reference_inventory(orders)}
                   if assignment.get("phase") in {"blind_plan", "counter_plan", "exploration_plan",
                       "survey_follow_up", "survey_operation_completion", "gap_assessment"} else {}),
                "follow_up_instruction": (
                    "Address the exact requested evidence and success conditions within the declared question. "
                    "Use only this assignment's response fields; retain unsupported requirements in its declared "
                    "rationale or limitations field. Planning does not constitute execution or fulfillment. "
                    "Never invent measurements or source passages.")}

    def _named_reference_inventory(self, orders=None):
        """Separate registered evidence, observed hits and resource exclusions."""
        names = sorted({wid for order in (self.work_orders if orders is None else orders)
                        for wid in named_reference_ids(order, known_ids=set(self.works) | set(self.aliases))})
        rows = []
        for observed in names:
            wid = self.aliases.get(observed, observed)
            hits = [ref for ref, query in zip(self.query_refs, self.search_log)
                    if acquisition_succeeded(query) and observed in query.get("returned_work_ids", [])]
            sources = [{"source_ref": ref, "representation": source["representation"],
                        "identity_verified": source.get("identity_verified") is True}
                       for ref, source in sorted(self.source_docs.items()) if source["work_id"] == wid]
            rows.append({"work_id": observed, "canonical_work_id": wid,
                "work_ref": self.work_records.get(wid, {}).get("artifact_ref"),
                "map_entry_ref": self.analysis_records.get(wid, {}).get("artifact_ref"),
                "sources": sources, "returned_by_query_refs": hits,
                "catalog_admission_blocked": any(gap.get("kind") == "work_limit"
                    and gap.get("work_id") == observed for gap in self.gaps),
                "source_availability": self._source_availability(wid)})
        return {"schema_version": "named-reference-inventory-1", "follow_up_ref": self.follow_up_ref,
            "scope": "Current child ledger only. Named references are addresses, not mandatory acquisition targets. "
                     "Missing registration does not mean a provider or upstream stage lacks the source. "
                     "A returned query hit is not a registered capture or a scientific comparison.",
            "declared_catalog_limit": self.declared_search_bounds["max_works"],
            "catalog_limit": self.bounds["max_works"], "catalog_work_count": len(self.works),
            "work_budget_adjustments": deepcopy(self.work_budget_adjustments),
            "remaining_catalog_slots": max(0, self.bounds["max_works"] - len(self.works)), "references": rows}

    def _require_follow_up_catalog_capacity(self):
        """Refuse a new acquisition campaign whose admission fence is exhausted."""
        if not self.work_orders or len(self.works) < self.bounds["max_works"]:
            return
        raise QuotaExceededError(
            "The retained catalog has no admission capacity for a new follow-up search campaign; "
            "reconcile acquisition scope or resources before planning more searches",
            dimension="max_works", limit=self.bounds["max_works"], observed=len(self.works),
            diagnostics=[self._named_reference_inventory()])

    def _prepare_follow_up(self):
        """Run targeted evidence work while retaining the existing source ledger."""
        if not self.work_orders:
            return
        self.assessment_ref = None
        self.counter_plan_record = None
        self.counter_queries_complete = False
        self.countersearch_complete = False
        retained_tree = self.store.head("kb/exploration-tree")
        if retained_tree is not None or self.score.get("design_brief") is not None:
            self._explore()
        else:
            plans = self._initial_plans()
            for role, queries, ref in plans:
                self._search(queries, role, ref)
            self._complete_search_pages()
            self._full_texts()
        self._accept_survey()
        if self.nomination is not None:
            self.nomination_record = self._publish("kb/gap-nomination", "note", {
                "survey_ref": self.survey_ref, **self.nomination},
                "research.gap-proposer", subjects=[self.survey_ref])

    def _validate_follow_up_result(self, value, *, sources=None, work_orders=None, record_inventory=None,
                                   require_completion=False):
        from scisaurus.runtime.survey_records import validate_follow_up_result
        displayed = self._assessment_source_context() if sources is None else sources
        windows = {source["source_ref"]: source["window"] for source in displayed}
        validate_follow_up_result(value, self.work_orders if work_orders is None else work_orders, self.source_docs,
                                  self._follow_up_query_refs(), windows=windows, record_inventory=record_inventory,
                                  require_completion=require_completion)
        _, survey = self.gate._note(self.survey_ref)
        works = {}
        for ref in survey["work_refs"]:
            _, raw = self.gate._artifact(ref)
            work = json.loads(raw)
            works[work["work_id"]] = work
        for row in value["orders"]:
            self.gate._evidence(row["evidence"], works, survey, required=row["status"] == "resolved")
            for ref in row["query_refs"]:
                self.gate.require_successful_follow_up_search(ref, survey, self.follow_up_ref)

    def _follow_up_repair_catalog(self, assignment):
        """Select retained, exact map spans visible in the original assignment."""
        from scisaurus.core.source_spans import validate as validate_span
        _, catalog = index_evidence(self._map_body(), self.source_docs)
        windows = {source["source_ref"]: source["window"] for source in assignment["sources"]}
        visible = []
        for item in catalog:
            if item["source_ref"] not in windows:
                continue
            try:
                validate_span({key: value for key, value in item.items() if key != "evidence_id"},
                              self.source_docs[item["source_ref"]], require_span=True,
                              window=windows[item["source_ref"]])
            except ValidationError:
                continue
            visible.append(item)
        return visible

    def _normalize_follow_up_result(self, value, assignment):
        windows = {source["source_ref"]: source["window"] for source in assignment["sources"]}
        try:
            value = expand_evidence(value, assignment.get("evidence_catalog", []),
                                    self.source_docs, windows=windows)
            return bind_source_spans(value, self.source_docs, windows=windows)
        except ValidationError as exc:
            raise ModelContractError(str(exc)) from exc

    def _current_follow_up_plan(self, plan_ref):
        if not isinstance(plan_ref, str):
            return False
        record = self.store.get(plan_ref)
        return (self.store.head(record["artifact_id"])["artifact_ref"] == plan_ref
                and self._body(record).get("follow_up_ref") == self.follow_up_ref)

    def _follow_up_query_refs(self):
        refs = []
        for ref, row in zip(self.query_refs, self.search_log):
            if row.get("request", {}).get("operation") != "search" or not acquisition_succeeded(row):
                continue
            plan_ref = row.get("plan_ref")
            if (isinstance(plan_ref, str)
                    and self._body(self.store.get(plan_ref)).get("follow_up_ref") == self.follow_up_ref):
                survey = self._body(self.store.get(self.survey_ref))
                self.gate.require_successful_follow_up_search(ref, survey, self.follow_up_ref)
                refs.append(ref)
        return refs

    def _follow_up_inventory(self):
        from scisaurus.runtime.survey_records import follow_up_inventory
        return follow_up_inventory(self.store, self.survey_ref)

    @staticmethod
    def _project_follow_up_inventory(inventory, order):
        from scisaurus.runtime.survey_records import project_follow_up_inventory
        return project_follow_up_inventory(inventory, order)

    def _review_follow_up_completion(self, order, disposition, execution_ref):
        """Assess operation acceptance against an immutable scientific disposition."""
        _, _, _, params = self.gate._recorded_execution(
            execution_ref, "methods.evidence-verifier", operation="model", task_kinds={"review"})
        evidence_assignment = json.loads(params["prompt"])
        assignment = {
            "phase": "survey_operation_completion", "completion_review_contract": FOLLOW_UP_COMPLETION_REVIEW_CONTRACT,
            "work_orders": [order], "follow_up_ref": self.follow_up_ref,
            "survey_ref": self.survey_ref, "assessment_ref": self.assessment_ref,
            "disposition_execution_ref": execution_ref,
            "disposition": follow_up_completion_basis(disposition, contract=FOLLOW_UP_COMPLETION_REVIEW_CONTRACT),
            "evidence_context": follow_up_completion_context(evidence_assignment),
            "instructions": (
                "Return only {outcome:met|unmet,rationale:string}. Independently evaluate the assigned order's "
                "exact success_condition against the validated disposition and its captured evidence_context, "
                "not the producer's completion verdict. Rationale and next_action are scientific deliverables to "
                "evaluate, not an acceptance verdict. Check their claims against the captured sources, inventory "
                "and bounded search records. Distinguish a declared scientific decision from a suggestion to decide later. "
                "The objective describes desired scientific inputs; success_condition controls operation acceptance. "
                "Explain how each requested deliverable meets or fails that condition. Honor its explicit alternatives. "
                "If it permits recording unavailable inputs, the disposition's itemized, scoped availability record "
                "backed by retained source, inventory or search provenance can meet that branch without obtaining values. "
                "Do not require a paper to assert global unavailability. If it requires an actual capture or measurement, "
                "an absence record cannot meet that requirement. Do not alter scientific status, limitations, source "
                "proofs or the assessment; operation completion establishes neither novelty nor a scientific answer. "
                "Return unmet if the exact requested deliverables or permitted availability records are absent.")}
        identity = hashlib.sha256(canonical_bytes({"order": order, "execution_ref": execution_ref})).hexdigest()
        return self._model_checked(f"follow-up-acceptance-{identity}", "methods.evidence-verifier", assignment,
                                   validate_follow_up_completion, stage="supervision", task_kind="review")

    def _prior_follow_up_completion_reviews(self, *, include_completed=False):
        """Return recorded completion decisions without losing accepted evidence."""
        rows = self.control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE ? ORDER BY created_at DESC",
            ("command/survey-follow-up-results/%",)).fetchall()
        outcomes = {"met", "unmet"} if include_completed else {"unmet"}
        pending = {(order["id"], outcome) for order in self.work_orders for outcome in outcomes}
        feedback = []
        for record in rows:
            manifest, raw_report = self.gate._artifact(record["artifact_ref"], current=False)
            report = json.loads(raw_report)
            historical_ref = report.get("follow_up_ref")
            if not isinstance(historical_ref, str):
                continue
            packet, raw_packet = self.gate._artifact(historical_ref, current=False)
            packet_body = json.loads(raw_packet)
            if (packet.get("author") != "command.controller" or packet["artifact_type"] != "note"
                    or packet_body.get("schema_version") != "survey-follow-up-1"
                    or not isinstance(packet_body.get("work_orders"), list)
                    or historical_ref not in {item["ref"] for item in manifest["inputs"]}):
                raise StateError("follow-up completion feedback lacks its exact historical work packet")
            matching_orders = {order["id"]: order for order in self.work_orders
                               if order in packet_body["work_orders"]}
            if not matching_orders:
                continue
            completion_refs = report.get("completion_execution_refs")
            if completion_refs is None:
                continue
            execution_refs = report.get("execution_refs", [report.get("execution_ref")])
            if (manifest.get("author") != "methods.evidence-verifier"
                    or not isinstance(completion_refs, list) or not isinstance(execution_refs, list)
                    or not isinstance(report.get("orders"), list)
                    or len(completion_refs) != len(report["orders"])
                    or len(execution_refs) != len(completion_refs)):
                raise StateError("follow-up completion feedback lacks its independent review provenance")
            for index, row in enumerate(report.get("orders", [])):
                if not isinstance(row, dict) or not isinstance(row.get("completion"), dict):
                    raise StateError("follow-up completion feedback requires explicit disposition and review objects")
                key = (row.get("id"), row["completion"].get("outcome"))
                if key not in pending or row.get("id") not in matching_orders:
                    continue
                review_ref = completion_refs[index]
                _, _, reviewed, params = self.gate._recorded_execution(
                    review_ref, "methods.evidence-verifier", operation="model", task_kinds={"review"})
                review_assignment = json.loads(params["prompt"])
                writer_ref = execution_refs[index]
                _, _, written, writer_params = self.gate._recorded_execution(
                    writer_ref, "methods.evidence-verifier", operation="model", task_kinds={"review"})
                writer_assignment = json.loads(writer_params["prompt"])
                if writer_assignment.get("question") != self.score["question"]:
                    continue
                captured_sources = {source["source_ref"]: json.loads(
                    self.gate._artifact(source["source_ref"], current=False)[1])
                    for source in writer_assignment["sources"]}
                written_rows = replay_follow_up_response(written, writer_assignment, captured_sources)["orders"]
                if (writer_ref not in {item["ref"] for item in manifest["inputs"]}
                        or writer_assignment.get("phase") != "survey_follow_up"
                        or any(writer_assignment.get(key) != report.get(key)
                               for key in ("survey_ref", "assessment_ref"))
                        or writer_assignment.get("follow_up_ref") != historical_ref
                        or writer_assignment.get("work_orders") != [matching_orders[row["id"]]]
                        or len(written_rows) != 1 or written_rows[0].get("id") != row["id"]
                        or follow_up_completion_basis(written_rows[0]) != follow_up_completion_basis(row)
                        or review_assignment.get("disposition_execution_ref") != writer_ref):
                    raise StateError("follow-up completion feedback differs from its recorded scientific disposition")
                completion = ModelResult(text=reviewed["text"], model="retained", usage={}, elapsed_seconds=0,
                    finish_reason=reviewed.get("finish_reason", "stop")).json_object(allow_missing_closers=True)
                validate_follow_up_completion(completion)
                review_contract = review_assignment.get("completion_review_contract")
                prior_basis = review_assignment.get("disposition")
                expected_basis = follow_up_completion_basis(row, contract=review_contract)
                if review_contract == FOLLOW_UP_COMPLETION_REVIEW_LEGACY_CONTRACT:
                    prior_basis = follow_up_completion_basis(prior_basis)
                    expected_basis = follow_up_completion_basis(row)
                if (review_ref not in {item["ref"] for item in manifest["inputs"]}
                        or review_assignment.get("phase") != "survey_operation_completion"
                        or review_contract not in {FOLLOW_UP_COMPLETION_REVIEW_CONTRACT,
                            FOLLOW_UP_COMPLETION_REVIEW_LEGACY_CONTRACT,
                            FOLLOW_UP_COMPLETION_REVIEW_EVIDENCE_CONTRACT}
                        or writer_assignment.get("completion_review_contract") != review_contract
                        or any(review_assignment.get(key) != report.get(key)
                               for key in ("survey_ref", "assessment_ref"))
                        or (review_contract == FOLLOW_UP_COMPLETION_REVIEW_CONTRACT
                            and review_assignment.get("evidence_context") != follow_up_completion_context(writer_assignment))
                        or review_assignment.get("follow_up_ref") != historical_ref
                        or review_assignment.get("work_orders") != [matching_orders[row["id"]]]
                        or prior_basis != expected_basis
                        or completion != row.get("completion")):
                    raise StateError("follow-up completion feedback differs from its recorded independent review")
                pending.remove(key)
                if row.get("completion", {}).get("outcome") in outcomes:
                    feedback.append({"report_ref": record["artifact_ref"], "follow_up_ref": historical_ref,
                        "order_id": row["id"],
                        "disposition": follow_up_completion_basis(written_rows[0]),
                        "review": deepcopy(row["completion"]),
                        "review_execution_ref": review_ref})
            if not pending:
                break
        return feedback

    def _resolve_follow_up(self):
        if not self.work_orders:
            return
        sources = self._assessment_source_context()
        assignment = {
            "phase": "survey_follow_up", "question": self.score["question"],
            "completion_contract": FOLLOW_UP_COMPLETION_CONTRACT,
            "completion_review_contract": FOLLOW_UP_COMPLETION_REVIEW_CONTRACT,
            "response_contract": {
                "envelope": {"orders": "one disposition for the assigned order"},
                "required_order_fields": ["id", "status", "rationale", "evidence", "query_refs",
                                          "limitation", "next_action", "completion"],
                "completion": {"outcome": ["met", "unmet"], "rationale": "exact acceptance evaluation"},
            },
            "survey_ref": self.survey_ref, "assessment_ref": self.assessment_ref,
            "assessment": {key: self._body(self.store.get(self.assessment_ref)).get(key)
                           for key in ("state", "rationale", "checks")},
            "survey_inventory": self._follow_up_inventory(),
            "sources": sources, "query_refs": self._follow_up_query_refs(),
            "searches": [{"query_ref": ref, "request": row["request"], "outcome": row["outcome"],
                          "returned_work_ids": row.get("returned_work_ids", []), "has_more": row.get("has_more")}
                         for ref, row in zip(self.query_refs, self.search_log)
                         if ref in self._follow_up_query_refs()],
            "instructions": (
                "Return {orders:[{id,status,rationale,evidence:[{work_id,source_ref,quote}],query_refs,limitation,next_action,"
                "completion:{outcome:met|unmet,rationale:string}}]}. "
                "Account for each assigned order exactly once against its success condition. "
                "completion evaluates that exact success condition separately from scientific evidence status. "
                "Honor its alternatives and scope: if it explicitly permits recording an input as unavailable, "
                "a source-backed availability record can meet that operation while the scientific input remains "
                "unresolved. An unavailable outcome is a scoped ledger finding recorded by this disposition "
                "from the retained acquisition failures, source availability, inventory and bounded searches. "
                "Identify each missing requested input explicitly in limitation and completion.rationale; "
                "no paper must itself declare that an uncaptured value is unavailable. This record describes "
                "what the survey could obtain, not global nonexistence of a value or relation. "
                "If it requires capturing a value or executing a measurement, unavailable evidence "
                "does not meet it. Explain each requested deliverable and any permitted unavailable outcome in "
                "completion.rationale. Do not replace the stated acceptance rule with a stricter requirement "
                "or treat completion as evidence of novelty, experimental readiness or a resolved research question. "
                "resolved requires captured quotations supporting fulfillment; limited requires a bounded recorded "
                "search, an explicit remaining evidence limitation, "
                "and a justified next scientific action. Optional record_evidence is a list of exact work_ref "
                "or map_entry_ref values from survey_inventory. These references establish only membership "
                "and reading status; resolved scientific requirements still require captured quotations. "
                "When only current record provenance is available and an earlier projection remains absent, "
                "use unresolved with record_evidence; do not invent a targeted search or treat current membership "
                "as proof that the earlier projection was restored. "
                "Use unresolved when neither condition holds. Quote only exact displayed source passages. "
                "Use survey_inventory to check current catalog, map membership, retained source availability, "
                "and explicit reading abstentions. A retained unread entry is distinct from an unavailable "
                "record or an absent map entry. These records establish current survey membership, not "
                "what an earlier topic projection contained or a scientific claim from an unread source. "
                "An insufficient gap assessment never establishes novelty or fulfills a measurement request by itself. "
                "Return all eight fields for the single assigned order, including empty evidence/query_refs lists "
                "and an empty limitation string when appropriate. Keep rationale and next_action concise.")}
        limit = self._map_input_limit("methods.evidence-verifier")
        orders, executions, completion_executions = [], [], []
        for order in self.work_orders:
            scoped = self._follow_up_assignment({**assignment, "work_orders": [order]})
            scoped["prior_completion_reviews"] = [review for review in self._prior_follow_up_completion_reviews(include_completed=True)
                                                   if review["order_id"] == order["id"]]
            scoped["instructions"] += (
                " Address the exact deficiencies in any prior_completion_reviews against the current captured "
                "evidence. These records include prior accepted availability dispositions as well as failed reviews. "
                "Earlier packet query references in these historical records document past work; do not list "
                "them as current query_refs unless they also appear in this assignment's allowed query_refs. "
                "Preserve their valid itemized findings, quotations and search evidence where the current sources "
                "still support them; explain any evidence-based revision. A prior completion establishes only "
                "that operation's acceptance, and must not be promoted to empirical support or novelty. "
                "Preserve valid findings and unresolved science; do not repeat the same disposition "
                "without addressing its independent review. A requested scientific decision must be explicitly "
                "stated and supported rather than deferred as a future action.")
            scoped["survey_inventory"] = self._project_follow_up_inventory(
                assignment["survey_inventory"], order)
            for chars in (12000, 6000, 3000, 1000, 300):
                scoped["sources"] = self._project_assessment_sources(
                    sources, full_text_chars=chars, abstract_chars=min(chars, 2000), unverified_chars=0)
                if limit is None or estimate_input_tokens(SYSTEM, json.dumps(scoped, ensure_ascii=False)) <= limit:
                    break
            else:
                raise ModelWorkBlocked("survey follow-up evidence cannot fit its configured input budget")
            displayed = scoped["sources"]
            identity = hashlib.sha256(canonical_bytes(order)).hexdigest()
            value, execution = self._model_checked(
                f"follow-up-disposition-{identity}", "methods.evidence-verifier", scoped,
                lambda value: self._validate_follow_up_result(value, sources=displayed, work_orders=[order],
                                                             record_inventory=scoped["survey_inventory"],
                                                             require_completion=True),
                normalizer=self._normalize_follow_up_result, normalizer_uses_assignment=True,
                stage="supervision", task_kind="review")
            row = value["orders"][0]
            completion, completion_execution = self._review_follow_up_completion(order, row, execution)
            row = {**row, "completion": completion}
            self._validate_follow_up_result({"orders": [row]}, sources=displayed, work_orders=[order],
                                            record_inventory=scoped["survey_inventory"], require_completion=True)
            orders.append(row)
            completion_executions.append(completion_execution)
            executions.append(execution)
            self._follow_up_decisions.add(identity)
        value = {"orders": orders}
        record = self._publish(f"command/survey-follow-up-results/{self.run_id}", "report", {
            "schema_version": "survey-follow-up-result-1", "follow_up_ref": self.follow_up_ref,
            "execution_ref": executions[-1], "execution_refs": executions,
            "completion_execution_refs": completion_executions,
            "survey_ref": self.survey_ref, "assessment_ref": self.assessment_ref, **value,
        }, "methods.evidence-verifier", subjects=[self.follow_up_ref, self.survey_ref,
                                                   self.assessment_ref, *executions, *completion_executions])
        self.follow_up_result = {"ref": record["artifact_ref"], **value}

    def _plan_work_budget(self):
        """Fit the discoverable work budget to the configured review reserve.

        Survey estimates include production, per-work review, and integrated
        review.  A large discovery ceiling can therefore be infeasible even
        when a useful minimum survey would fit.  Reduce only the bounded
        workload, preserving the configured seed and challenge reserve, and
        retain the decision in the run artifact.  An impossible minimum still
        remains a hard admission failure; this never fabricates time or
        silently drops required seeds.
        """
        kwargs = {
            "stage_seconds": self.score["stage_seconds"],
            "worker_slots": self.worker_slots,
            "wall_clock_seconds": self.config["limits"]["wall_clock_seconds"],
            "policy": self.config.get("time_policy"),
        }
        budget_field = "max_analyzed_works" if "max_analyzed_works" in self.bounds else "max_works"
        requested = self.bounds[budget_field]
        initial = TimePolicy(unit_count=requested, **kwargs)
        snapshot = initial.snapshot()
        if snapshot["initial_hard_limit_feasible"] and snapshot["initial_target_feasible"]:
            return initial

        reserve = self.bounds.get("challenge_reserve", 0)
        floor = (reserve + 1 if budget_field == "max_analyzed_works" else
                 max(reserve + 1, len(self.score["seed_work_ids"]) + reserve))
        if floor > requested:
            return initial

        # The schedule is monotone in unit_count.  Binary search keeps the
        # planning pass constant-time even when a caller requests a very large
        # discovery ceiling.
        low, high, feasible = floor, requested, None
        while low <= high:
            candidate = (low + high) // 2
            policy = TimePolicy(unit_count=candidate, **kwargs)
            candidate_snapshot = policy.snapshot()
            if (candidate_snapshot["initial_hard_limit_feasible"]
                    and candidate_snapshot["initial_target_feasible"]):
                feasible = candidate
                low = candidate + 1
            else:
                high = candidate - 1
        if feasible is None:
            return initial

        self.bounds[budget_field] = feasible
        self.work_budget_adjustments.append({
            "kind": "deadline_fit",
            "requested_" + budget_field: requested,
            "effective_" + budget_field: feasible,
            "minimum_preserved": floor,
            "reason": "configured review schedule exceeded target or hard deadline",
        })
        return TimePolicy(unit_count=feasible, **kwargs)

    def _body(self, record):
        return json.loads(self.store.read_body(record["body_hash"]))

    def _validate_review_obligations(self, values):
        if values is None:
            return []
        if not isinstance(values, list):
            raise StateError("survey review obligations must be an explicit controller-owned list")
        required = {"receipt_ref", "receipt_body_sha256", "work_id", "entry_ref", "entry_body_sha256",
                    "relationship_pins", "source_pins", "hypothesis"}
        result = []
        for value in values:
            if (not isinstance(value, dict) or not required <= set(value)
                    or set(value) - required - {"comparison_pins"}
                    or any(not isinstance(value[key], str) or not value[key].strip()
                           for key in ("receipt_ref", "work_id", "entry_ref", "hypothesis"))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(value["receipt_body_sha256"]))):
                raise StateError("survey review obligation omitted its exact receipt or hypothesis")
            entry, raw = self.gate._artifact(value["entry_ref"], current=False)
            body = self.gate._json(raw, value["entry_ref"])
            _, score_raw = self.gate._artifact(entry["score_ref"], current=False)
            if (entry["author"] != "research.literature-mapper"
                    or entry["body_hash"] != value["entry_body_sha256"] or body.get("work_id") != value["work_id"]
                    or self.gate._json(score_raw, entry["score_ref"]).get("survey", {}).get("question") != self.score["question"]):
                raise StateError("survey review obligation changed its entry, question, or source identity")
            owners = {value["work_id"]}
            cited = {proof["source_ref"] for field in MAP_FIELDS for proof in body[field]["evidence"]}
            comparisons = value.get("comparison_pins", [])
            if not isinstance(comparisons, list):
                raise StateError("survey review comparison pins must be an explicit list")
            for pin in comparisons:
                if not isinstance(pin, dict) or set(pin) != {"ref", "body_hash"}:
                    raise StateError("survey review obligation has a malformed comparison pin")
                manifest, peer_raw = self.gate._artifact(pin["ref"], current=False)
                peer = self.gate._json(peer_raw, pin["ref"])
                peer_id = peer.get("work_id")
                if (manifest["author"] != "research.literature-mapper"
                        or not manifest["artifact_id"].startswith("kb/work-analyses/")
                        or manifest["body_hash"] != pin["body_hash"]
                        or manifest.get("score_ref") != entry.get("score_ref")
                        or not isinstance(peer_id, str) or peer_id in owners):
                    raise StateError("survey review comparison changed its identity, hash, question, or owner")
                owners.add(peer_id)
                cited.update(proof["source_ref"] for field in MAP_FIELDS for proof in peer[field]["evidence"])
            for group in ("relationship_pins", "source_pins"):
                if not isinstance(value[group], list):
                    raise StateError("survey review obligation pins must be explicit lists")
                for pin in value[group]:
                    if not isinstance(pin, dict) or set(pin) != {"ref", "body_hash"}:
                        raise StateError("survey review obligation has a malformed immutable pin")
                    manifest, pinned_raw = self.gate._artifact(pin["ref"], current=False)
                    pinned = self.gate._json(pinned_raw, pin["ref"])
                    if manifest["body_hash"] != pin["body_hash"] or manifest.get("score_ref") != entry.get("score_ref"):
                        raise StateError("survey review obligation source or relationship hash mismatch")
                    if group == "relationship_pins":
                        if manifest["author"] != "research.literature-mapper" or pinned.get("source") != value["work_id"]:
                            raise StateError("survey review obligation relationship has a different owner")
                        owners.add(pinned["target"])
                        cited.update(proof["source_ref"] for proof in pinned["claim"]["evidence"])
                    elif manifest["artifact_type"] != "source_capture" or pinned.get("work_id") not in owners:
                        raise StateError("survey review obligation source has a different owner")
            if not cited.issubset({pin["ref"] for pin in value["source_pins"]}):
                raise StateError("survey review obligation omitted cited source pins")
            result.append(deepcopy(value))
        return result

    def _review_obligations_for(self, wid):
        values = [deepcopy(value) for value in getattr(self, "review_obligations", []) if value["work_id"] == wid]
        for value in values:
            if not {pin["ref"] for pin in value["source_pins"]}.issubset(self.source_docs):
                raise StateError("survey review obligation no longer has its pinned captured sources")
        return values

    def _review_comparison_ids(self, wid):
        return {self._body(self.store.get(pin["ref"]))["work_id"]
                for value in self._review_obligations_for(wid)
                for pin in value.get("comparison_pins", [])}

    def _review_critique_contexts(self, wid, *, entry_ref=None, relationship_refs=None):
        """Expose both immutable critique targets and the claims being judged."""
        obligations = self._review_obligations_for(wid)
        if not obligations:
            return []
        current = self.store.get(entry_ref) if entry_ref else self.analysis_records[wid]
        current_body = self._body(current)
        refs = (relationship_refs if relationship_refs is not None else
                [value["artifact_ref"] for value in self.relationships.values() if value["source"] == wid])
        def snapshot(ref):
            record = self.store.get(ref)
            return {"ref": ref, "body_hash": record["body_hash"], "body": self._body(record)}
        contexts = []
        for obligation in obligations:
            original = snapshot(obligation["entry_ref"])
            context = {
                "protocol": _CRITIQUE_CONTEXT_PROTOCOL,
                "check_id": critique_check_id(obligation),
                "receipt_ref": obligation["receipt_ref"], "work_id": wid,
                "original_entry": original,
                "current_entry": snapshot(current["artifact_ref"]),
                "changed_entry_fields": sorted(key for key in current_body
                                               if current_body[key] != original["body"].get(key)),
                "original_relationships": [snapshot(pin["ref"]) for pin in obligation["relationship_pins"]],
                "current_relationships": [snapshot(ref) for ref in refs],
            }
            if obligation.get("comparison_pins"):
                context["comparison_entries"] = []
                for pin in obligation["comparison_pins"]:
                    peer = snapshot(pin["ref"])
                    peer_id = peer["body"]["work_id"]
                    if peer_id not in self.analysis_records:
                        raise StateError("survey review comparison has no current mapped entry")
                    context["comparison_entries"].append({
                        "original_entry": peer,
                        "current_entry": snapshot(self.analysis_records[peer_id]["artifact_ref"]),
                    })
            contexts.append(context)
        return contexts

    def _review_failure_keys(self, review):
        """Relationship revisions retain the same owner/target repair identity."""
        keys = []
        for check in review["checks"]:
            if check["outcome"] == "passed":
                continue
            key = check["check_id"]
            if key.startswith("relationship:"):
                relation = self._body(self.store.get(key[len("relationship:"):]))
                key = "relationship:" + relation["target"] + ":" + relation["kind"]
            keys.append(key)
        return sorted(keys)

    def _wait_provider(self, capability):
        """Wait until the next paced provider slot, bounded by the run deadline."""
        interval = self.provider_intervals.get(capability, 0.0)
        due = self.next_provider_at.get(capability, self.started)
        if interval <= 0 and due <= time.monotonic():
            return
        waited = 0.0
        while True:
            self._ensure_active()
            remaining = due - time.monotonic()
            if remaining <= 0:
                break
            if time.monotonic() + remaining >= self.deadline:
                raise ValidationError(f"provider interval for {capability} exceeds run deadline")
            time.sleep(min(remaining, 0.25))
            waited += min(remaining, 0.25)
        self._ensure_active()
        self.next_provider_at[capability] = time.monotonic() + interval
        self.provider_waits.append({"capability": capability, "waited_seconds": round(waited, 3),
                                    "interval_seconds": interval})

    def _call(self, task_id, kind, params, *, actor, task_kind, reservation_id=None):
        result, execution = super()._call(task_id, kind, params, actor=actor,
            task_kind=task_kind, reservation_id=reservation_id)
        if kind == "crossref":
            self._observe_crossref_limits(result)
        return result, execution

    def _observe_crossref_limits(self, result):
        from scisaurus.runtime.retrieval import CrossrefClient
        metadata = result.get("metadata", {}) if isinstance(result, dict) else {}
        raw_headers = metadata.get("headers", {}) if isinstance(metadata, dict) else {}
        headers = {key.lower(): value for key, value in raw_headers.items()} if isinstance(raw_headers, dict) else {}
        interval = None
        try:
            window = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)s\s*", str(headers.get("x-rate-limit-interval", "")))
            limit = float(headers.get("x-rate-limit-limit", ""))
            if window and math.isfinite(limit) and limit > 0:
                interval = float(window[1]) / limit
                if not math.isfinite(interval) or interval <= 0:
                    interval = None
        except (TypeError, ValueError, OverflowError):
            pass
        cooldown = CrossrefClient._retry_after_seconds(headers) or 0.0
        keys = ["identity", *(["bibliography"] if self.bibliography_mode == "crossref" else [])]
        for key in keys:
            if interval is not None:
                self.provider_intervals[key] = max(self.provider_intervals.get(key, 0.0), interval)
            delay = max(self.provider_intervals.get(key, 0.0), cooldown)
            self.next_provider_at[key] = max(self.next_provider_at.get(key, self.started),
                                            time.monotonic() + delay)

    def _heads(self, prefix):
        rows = self.control._conn.execute(
            "SELECT logical_id, MAX(version) AS version FROM artifacts "
            "WHERE logical_id LIKE ? GROUP BY logical_id ORDER BY logical_id", (prefix + "%",),
        ).fetchall()
        return [self.store.get(f"artifact:{row['logical_id']}@{row['version']}") for row in rows]

    def _restore(self):
        """Reconstruct only durable, content-addressed survey state.

        Completed provider effects are represented by their recorded execution
        artifacts and query records. A source-policy scope can deliberately
        invalidate later interpretation without discarding those captures.
        """
        score = self.store.head(f"command/scores/{self.score['id']}")
        protocol = self.store.head("kb/search-protocol")
        if score:
            self.score_ref = score["artifact_ref"]
        if protocol:
            self.protocol = protocol
        register = self.store.head("kb/work-register")
        register_body = self._body(register) if register else {"aliases": {}, "source_refs": [], "query_refs": []}
        self.register_ref = register["artifact_ref"] if register else None
        self.aliases = dict(register_body.get("aliases", {}))
        for record in self._heads("kb/works/"):
            body = self._body(record)
            wid = body["work_id"]
            self.work_records[wid], self.works[wid] = record, body
            for provider_id in body.get("provider_ids", []):
                self.aliases[provider_id] = wid
            if body.get("doi"):
                self.dois[body["doi"]] = wid
        identity_refs = list(dict.fromkeys([*register_body.get("identity_refs", []),
            *[r["artifact_ref"] for r in self._heads("kb/identities/")]]))
        for ref in identity_refs:
            record = self.store.get(ref)
            self.identity_records[self._body(record)["work_id"]] = record
        source_heads = {self.store.get(ref)["artifact_id"]: ref for ref in register_body.get("source_refs", [])}
        source_heads.update({r["artifact_id"]: r["artifact_ref"] for prefix in ("kb/abstracts/", "kb/full-text/")
                             for r in self._heads(prefix)})
        source_refs = list(source_heads.values())
        for ref in source_refs:
            record = self.store.get(ref)
            body = self._body(record)
            self.source_docs[ref] = body
            key = "full_text" if record["artifact_id"].startswith("kb/full-text/") else "abstract"
            self.source_records[f"{key}/{body['work_id']}"] = record
            if key == "full_text":
                self.full_text_attempted.add(body["work_id"])
        self.query_refs = list(dict.fromkeys([*register_body.get("query_refs", []),
            *[r["artifact_ref"] for r in sorted(self._heads("kb/queries/"), key=lambda r: r["created_at"])]]))
        self.search_log = [self._body(self.store.get(ref)) for ref in self.query_refs]
        reservations = [self._body(record) for record in self._heads("command/api-calls/")]
        self.identity_calls = max(
            len(self.identity_records),
            max((row.get("identity_number", 0) for row in reservations), default=0),
        )
        # Reservations are committed before dispatch, so failed and
        # result-unknown calls remain charged after a restart. The fallback
        # preserves cumulative accounting for runs created before reservations
        # were introduced.
        self.api_calls = max(
            len(self.query_refs) + len(self.identity_records),
            max((row["number"] for row in reservations), default=0),
        )
        coverage = self.store.head("kb/coverage")
        final = self.store.head("command/results/final")
        coverage_body = self._body(coverage) if coverage else None
        if final and (not coverage or final["created_at"] > coverage["created_at"]):
            coverage_body = self._body(final).get("coverage", coverage_body)
        if coverage_body:
            self.expansion_log = list(coverage_body.get("expansion", []))
            self.gaps = list(coverage_body.get("access_and_limit_gaps", []))
            self.expanded = {wid for row in self.expansion_log
                             for wid in row.get("completed_seed_work_ids",
                                                row.get("seed_work_ids", []) if row.get("completed") is True else [])}
        if "retrieval" not in self.resume_session["reopened_scopes"]:
            # Known failed routes and uncertain reservations consumed the same
            # acquisition allowance as successful captures. A review-only
            # resume must not silently start a new retrieval campaign.
            self.full_text_attempted.update(row["work_id"] for row in self.gaps
                if row.get("kind") in {"full_text_failure", "full_text_identity_or_scope"} and row.get("work_id") in self.works)
            # Older checkpoints did not merge acquisition gaps on resume.
            # Their immutable final reports still identify consumed routes.
            for version in self.store.versions("command/results/final"):
                report = self._body(self.store.get(f"artifact:command/results/final@{version}"))
                self.full_text_attempted.update(row["work_id"]
                    for row in report.get("coverage", {}).get("access_and_limit_gaps", [])
                    if row.get("kind") in {"full_text_failure", "full_text_identity_or_scope"}
                    and row.get("work_id") in self.works)
            for record in self._heads("command/source-attempts/full-text/"):
                body = self._body(record)
                self.full_text_attempted.add(body["work_id"])
                failure = body.get("failure")
                if isinstance(failure, dict) and failure not in self.gaps:
                    self.gaps.append(deepcopy(failure))
        self._revalidate_retained_full_texts()
        self._restore_source_access_failures()
        map_record = self.store.head("kb/literature-map")
        if map_record:
            map_body = self._body(map_record)
            self.map_record = map_record
            for ref in map_body.get("entry_refs", []):
                record = self.store.get(ref)
                wid = self._body(record)["work_id"]
                self.analysis_records[wid] = record
            for ref in map_body.get("relationship_refs", []):
                record = self.store.get(ref)
                relation = self._body(record)
                key = "-".join(relation[field] for field in ("source", "target", "kind"))
                self.relationships[key] = {**relation, "artifact_ref": ref}
            # A validated per-work update can be committed immediately before a
            # process stops and therefore postdate the aggregate map checkpoint.
            # Overlay only those later versions. Absence is not interpreted as a
            # deletion, so an interrupted removal is conservatively retried.
            candidates = [(record, self._body(record)) for record in self._heads("kb/relationships/")]
            self.relationships = overlay_post_checkpoint_relationships(
                self.relationships, map_record["created_at"], candidates)
            for record in self._heads("kb/work-analyses/"):
                body = self._body(record)
                if record["created_at"] > map_record["created_at"] and body["work_id"] in self.works:
                    self.analysis_records[body["work_id"]] = record
        if map_record is None:
            for record in self._heads("kb/work-analyses/"):
                body = self._body(record)
                if body["work_id"] in self.works:
                    self.analysis_records[body["work_id"]] = record
            for record in self._heads("kb/relationships/"):
                relation = self._body(record)
                key = "-".join(relation[field] for field in ("source", "target", "kind"))
                self.relationships[key] = {**relation, "artifact_ref": record["artifact_ref"]}
        scopes = set(self.resume_session["reopened_scopes"])
        if "mapping" not in scopes:
            for wid, record in self.analysis_records.items():
                basis = self._analysis_basis(wid)
                subjects = {item["ref"] for item in record.get("inputs", []) if item.get("purpose") == "subject"}
                relations = [{key: value for key, value in relation.items() if key != "artifact_ref"}
                             for relation in self.relationships.values() if relation["source"] == wid]
                try:
                    validate_map({"entries": [self._body(record)], "relationships": relations},
                                 [wid], set(self.works), self.source_docs)
                except ValidationError:
                    continue
                completion = self.store.head(f"command/survey-analysis-completions/{wid}")
                checked = self._body(completion) if completion else {}
                current_relationships = sorted(relation["artifact_ref"]
                                               for relation in self.relationships.values()
                                               if relation["source"] == wid)
                if (set(basis).issubset(subjects) or (
                        checked.get("basis") == basis
                        and checked.get("question") == self.score["question"]
                        and checked.get("entry_ref") == record["artifact_ref"]
                        and checked.get("relationship_refs") == current_relationships)):
                    self.analyzed_basis[wid] = basis
        if "mapping" not in scopes and ("focused_review" not in scopes or self.review_obligations):
            for record in self._heads("kb/work-reviews/"):
                body = self._body(record)
                wid = self._body(self.store.get(body["entry_ref"]))["work_id"]
                checks = body.get("checks", [])
                relations = [relation for relation in self.relationships.values() if relation["source"] == wid]
                refs = [relation["artifact_ref"] for relation in relations]
                basis = self._work_review_basis(wid)
                subjects = {item["ref"] for item in record.get("inputs", []) if item.get("purpose") == "subject"}
                if (wid in self.analyzed_basis
                        and self._review_evidence_scope(wid, review=body) == self._review_evidence_scope(wid)
                        and body.get("entry_ref") == self.analysis_records.get(wid, {}).get("artifact_ref")
                        and body.get("relationship_refs") == refs and checks
                        and set(basis).issubset(subjects)
                        and all(check.get("outcome") == "passed" for check in checks)):
                    self.work_reviews[wid] = record
                    self.reviewed_basis[wid] = basis
        self.survey_revision = self.control._conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE logical_id LIKE 'kb/survey-reviews/%'",
        ).fetchone()[0]
        task_ids = [row[0] for row in self.control._conn.execute(
            "SELECT task_id FROM tasks WHERE task_id LIKE 'survey-%'"
        ).fetchall()]
        suffixes = [int(match.group(1)) for task_id in task_ids if (match := re.search(r"-([0-9]+)$", task_id))]
        self.serial = max(suffixes, default=0)
        reviews_current = all(wid in self.reviewed_basis for wid in self.analysis_records)
        if (reviews_current and "integrated_review" not in scopes
                and "mapping" not in scopes and "focused_review" not in scopes):
            accepted = self.store.accepted("kb/surveys/current")
            if accepted:
                try:
                    self.gate.require_current(accepted["artifact_ref"])
                    events = self.control._conn.execute(
                        "SELECT payload_json FROM events WHERE event_type='survey.accepted' ORDER BY seq DESC").fetchall()
                    acceptance = next((json.loads(row[0]) for row in events
                                       if json.loads(row[0]).get("survey_ref") == accepted["artifact_ref"]), None)
                    if acceptance is None:
                        raise ValidationError("accepted survey has no current review contract")
                    review = self._body(self.store.get(acceptance["review_ref"]))
                    _, _, prompt, _ = self.gate._model_review_execution(review["execution_ref"], "methods.survey-reviewer")
                    if prompt.get("review_contract") != self._survey_review_packet()["review_contract"]:
                        raise ValidationError("accepted survey requires the current review contract")
                    self.survey_ref = self.incumbent = accepted["artifact_ref"]
                    self.time_policy.mark_retained_result(self.survey_ref)
                except Exception:
                    pass
        nomination = self.store.head("kb/gap-nomination")
        if nomination:
            self.nomination_record, self.nomination = nomination, {
                key: self._body(nomination)[key] for key in ("id", "statement")}
        self.follow_up_discovery_current = self._retained_follow_up_discovery()
        if self.store.head("kb/exploration-tree") is not None:
            self._tree_load()
        self._refresh_countersearch_state()
        if self.survey_ref and "gap_assessment" not in scopes:
            accepted = self.store.accepted("kb/gap-assessments/current")
            if accepted:
                try:
                    self.gate.require_current_assessment(accepted["artifact_ref"])
                    self.assessment_ref = accepted["artifact_ref"]
                except Exception:
                    pass

    def _retained_follow_up_discovery(self):
        if self.follow_up_ref is None:
            return False
        accepted = self.store.accepted("kb/surveys/current")
        if accepted is None:
            return False
        try:
            receipt = self.gate._accepted_event("survey.accepted", "survey_ref", accepted["artifact_ref"])
            if {"ref": accepted["artifact_ref"], "body_hash": accepted["body_hash"]} not in receipt.get("evidence_pins", []):
                return False
            review = self._body(self.store.get(receipt["review_ref"]))
            if review.get("survey_ref") != accepted["artifact_ref"]:
                return False
            _, _, prompt, _ = self.gate._model_review_execution(review["execution_ref"], "methods.survey-reviewer")
            manifest, raw = self.gate._artifact(accepted["artifact_ref"], current=False)
            survey = self.gate._json(raw, accepted["artifact_ref"])
            _, map_raw = self.gate._artifact(survey["map_ref"], current=False)
            if (manifest.get("score_ref") != self.score_ref
                    or survey.get("score_ref") != self.score_ref
                    or self.gate._json(map_raw, survey["map_ref"]).get("question") != self.score["question"]
                    or prompt.get("question") != self.score["question"]):
                return False
            if (prompt.get("follow_up_ref") == self.follow_up_ref
                    and prompt.get("work_orders") == self.work_orders):
                return True
            # Retiring an order does not invalidate acquisition for unchanged
            # surviving orders. This retains discovery only; current survey,
            # counter-search, and disposition gates still apply independently.
            retained_orders = prompt.get("work_orders")
            if not isinstance(retained_orders, list) or not retained_orders:
                return False
            _, follow_up_raw = self.gate._artifact(prompt["follow_up_ref"], current=False)
            retained_packet = self.gate._json(follow_up_raw, prompt["follow_up_ref"])
            if retained_packet.get("work_orders") != retained_orders:
                return False
            retained_bodies = {canonical_bytes(order) for order in retained_orders}
            return bool(self.work_orders) and all(
                canonical_bytes(order) in retained_bodies for order in self.work_orders)
        except (KeyError, TypeError, ValueError, ValidationError, StateError):
            return False

    def _refresh_countersearch_state(self):
        """Reconstruct challenge completion from exact durable dependencies."""
        self.counter_plan_record = None
        self.counter_query_refs = []
        self.counter_queries_complete = False
        self.countersearch_complete = False
        if self.nomination_record is None:
            return
        plan = self.store.head("kb/counter-search-plan")
        if plan is None:
            return
        body = self._body(plan)
        if getattr(self, "work_orders", None) and body.get("follow_up_ref") != self.follow_up_ref:
            return
        nomination_body = self._body(self.nomination_record)
        if body.get("nomination_ref") != self.nomination_record["artifact_ref"]:
            return
        proposal = {key: body.get(key) for key in ("queries", "rationale")}
        try:
            self._plan_validator(proposal)
        except ValidationError:
            return
        if self.nomination != {key: nomination_body[key] for key in ("id", "statement")}:
            raise StateError("counter-search retained nomination differs from its immutable hypothesis")
        lineage = countersearch_lineage(self.control, self.store, score_ref=self.score_ref,
            question=self.score["question"], nomination_ref=self.nomination_record["artifact_ref"],
            plan_ref=plan["artifact_ref"], survey_ref=self.survey_ref, query_refs=self.query_refs)
        self.counter_plan_record = plan
        self.counter_query_refs = lineage["query_refs"]
        self.counter_queries_complete = lineage["queries_complete"]
        self.countersearch_complete = (lineage["complete"] and lineage["survey_current"]
                                       and self._counter_acquisition_complete())

    def _counter_acquisition_id(self):
        digest = hashlib.sha256(self.counter_plan_record["artifact_ref"].encode()).hexdigest()
        return "command/counter-search-acquisitions/" + digest

    def _counter_acquisition_complete(self):
        if self.counter_plan_record is None:
            return False
        record = self.store.head(self._counter_acquisition_id())
        if record is None:
            return False
        manifest, raw = self.gate._artifact(record["artifact_ref"], current=False)
        body = self.gate._json(raw, record["artifact_ref"])
        if (manifest["author"] != "command.controller"
                or body.get("schema_version") != "counter-search-acquisition-1"):
            raise StateError("counter-search acquisition receipt has an invalid owner or schema")
        return (body.get("question") == self.score["question"]
                and body.get("plan_ref") == self.counter_plan_record["artifact_ref"]
                and body.get("nomination_ref") == self.nomination_record["artifact_ref"]
                and body.get("query_refs") == self.counter_query_refs)

    def _record(self, logical, kind, body, author, *, subjects=()):
        head = self.store.head(logical)
        if head and self.store.read_body(head["body_hash"]) == canonical_bytes(body):
            return head
        return self._publish(logical, kind, body, author, subjects=subjects)

    def _reserve_api_call(self, capability, request, actor):
        """Durably charge a provider call before its outcome can become unknown."""
        number = self.api_calls + 1
        identity_number = self.identity_calls + (capability == "identity")
        self._publish(
            f"command/api-calls/{number}",
            "note",
            {"number": number, "identity_number": identity_number,
             "capability": capability, "request": request,
             **({"tree_action_id": self._active_tree_action} if self._active_tree_action else {})},
            actor,
            subjects=[self.score_ref],
        )
        self.api_calls = number
        self.identity_calls = identity_number

    def _tick(self, stage, *, count=1, pending_review_count=0):
        self._ensure_active()
        decision = self.time_policy.admit(stage, task_count=count, pending_review_count=pending_review_count)
        decision.update(task_count=count, worker_slots=self.worker_slots, pending_review_count=pending_review_count)
        self.time_decisions.append(decision)
        self._publish(f"command/time-decisions/{len(self.time_decisions)}", "note", decision,
                      "command.controller", subjects=[self.score_ref])
        if not decision["allowed"]:
            raise ValidationError(f"time admission deferred {stage}: {decision['reason']}")

    def _before_dispatch(self, spec):
        super()._before_dispatch(spec)
        if spec["kind"] == "model":
            prompt = json.loads(spec["params"]["prompt"])
            if "prerequisite_survey_ref" in prompt:
                required = prompt["prerequisite_survey_ref"]
                if required != prompt.get("survey_ref"):
                    raise ValidationError("gap task has inconsistent survey prerequisites")
                self.gate.require_current(required)
            if prompt.get("phase") in {"counter_plan", "gap_assessment"}:
                nomination = self.gate.require_current_nomination(prompt.get("nomination_ref"))
                if canonical_bytes(prompt.get("gap")) != canonical_bytes({
                        key: nomination[key] for key in ("id", "statement")}):
                    raise ValidationError("gap task does not match its exact nomination")
        self._ensure_active()

    def _model_checked(self, name, actor, assignment, validator, *, normalizer=None,
                       normalizer_uses_assignment=False, model_overrides=None,
                       stage="production", task_kind="production"):
        job = {"name": name, "actor": actor, "assignment": assignment,
               "validator": validator}
        if normalizer:
            job["normalizer"] = normalizer
            job["normalizer_uses_assignment"] = normalizer_uses_assignment
        if isinstance(model_overrides, dict):
            job["model_overrides"] = deepcopy(model_overrides)
        return self._models_checked([job], stage=stage, task_kind=task_kind)[name]

    @staticmethod
    def _response_assignment_identity(assignment):
        value = deepcopy(assignment)
        for field in ("validation_feedback", "resume_boundary", "_contract_repair_boundary"):
            value.pop(field, None)
        return canonical_bytes(survey_review_assignment_identity(value))

    def _retained_settled_response(self, name, actor, assignment, *, execution_ref=None,
                                   model=None):
        """Verify task ownership and immutable input of an already paid response."""
        if not self.resume_session:
            return None
        rows = self.control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE ? ORDER BY created_at DESC",
            (f"command/executions/survey-{name}-%",)).fetchall()
        for row in rows:
            execution = self.store.get(row["artifact_ref"])
            if execution_ref is not None and execution["artifact_ref"] != execution_ref:
                continue
            task_id = execution["artifact_id"].removeprefix("command/executions/")
            if (not task_id.startswith(f"survey-{name}-")
                    or execution["author"] != actor or execution.get("score_ref") != getattr(self, "score_ref", None)
                    or len(execution["inputs"]) != 1):
                continue
            task_row = self.control._conn.execute("SELECT state,payload_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task_row is None or task_row["state"] not in {"awaiting_review", "blocked"}:
                continue
            if json.loads(task_row["payload_json"]).get("operation") != "model":
                continue
            # A failed validation already has its own bounded repair lineage.
            if execution_ref is None and self.store.head("command/validation/" + task_id):
                continue
            attempts = self.tasks.attempts_for_task(task_id)
            if not attempts:
                continue
            receipt = attempts[-1]
            if (receipt["state"] != "succeeded" or receipt["lease_owner"] != actor
                    or not receipt.get("finished_at")
                    or not receipt["created_at"] <= execution["created_at"] <= receipt["finished_at"]):
                continue
            context = self.store.get(execution["inputs"][0]["ref"])
            if (context["artifact_id"] != "command/contexts/" + task_id or context["author"] != actor
                    or context.get("score_ref") != execution.get("score_ref")):
                continue
            bodies = []
            for record in (context, execution):
                raw = self.store.read_body(record["body_hash"])
                if hashlib.sha256(raw).hexdigest() != record["body_hash"]:
                    raise StateError("retained dispatch body does not match its immutable hash")
                bodies.append(json.loads(raw))
            params, body = bodies
            if params.get("role") != actor or not isinstance(params.get("prompt"), str):
                continue
            prior_assignment = json.loads(params["prompt"])
            if self._response_assignment_identity(prior_assignment) != self._response_assignment_identity(assignment):
                continue
            model_matches = True
            if model is not None:
                spec = {"kind": "model", "actor": actor, "params": {"role": actor, "client": model}}
                route_id = params.get("route_id")
                if route_id is not None:
                    routes = [route for route in role_routes_for(model, actor) if route["id"] == route_id]
                    current_model = (self._route_model_config(spec, routes[0])
                                     if len(routes) == 1 else None)
                else:
                    current_model = self._base_model_config(spec)
                prior_spec = {"kind": "model", "actor": actor,
                              "params": {"role": actor, "client": params["client"]}}
                prior_model = self._base_model_config(prior_spec)
                def model_identity(value):
                    return ModelWorkCache.key(scope="settled-model", role=actor, system=SYSTEM,
                                              prompt={}, model=value)
                model_matches = (current_model is not None
                                 and model_identity(current_model) == model_identity(prior_model))
            result = ModelResult(**body)
            if result.usage != receipt["usage"].get("actual"):
                continue
            return {"previous_response": {"raw_text": result.text}, "finish_reason": result.finish_reason,
                    "execution_ref": execution["artifact_ref"], "model_matches": model_matches,
                    "scope": "Validate the settled original response before any new dispatch."}
        return None

    def _retained_unreviewed_response(self, name, actor, assignment):
        """Recover a settled dispatch interrupted before its scoped validation."""
        return self._retained_settled_response(name, actor, assignment)

    def _retained_validation_feedback(self, name, assignment):
        """Reuse a failed response only when its original assignment is still exact."""
        if not self.resume_session:
            return None
        prefix = f"command/validation/survey-{name}-"
        rows = self.control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE ? ORDER BY created_at DESC",
            (prefix + "%",)).fetchall()
        for row in rows:
            validation = self.store.get(row["artifact_ref"])
            try:
                validation_body = self._body(validation)
                error = validation_body["error"]
                proposal = self.store.get(validation["inputs"][0]["ref"])
                previous_response = self._body(proposal)
                execution = self.store.get(proposal["inputs"][0]["ref"])
                context = self._body(self.store.get(execution["inputs"][0]["ref"]))
                prior_assignment = json.loads(context["prompt"])
                prior_assignment.pop("validation_feedback", None)
            except (IndexError, KeyError, TypeError, ValueError):
                continue
            # These fields create a new transport/cache boundary, not a new
            # scientific assignment.  Ignoring them lets the corrected
            # normalizer revalidate the already-paid response after resume.
            if self._response_assignment_identity(prior_assignment) == self._response_assignment_identity(assignment):
                return {"error": error, "previous_response": previous_response,
                    "finish_reason": validation_body.get("finish_reason", "stop"),
                    "execution_ref": execution["artifact_ref"],
                    "scope": "Repair only this assignment's recorded contract violations; preserve every valid field. "
                             "Use every requested field name and enum value exactly as specified; do not substitute synonyms."}
        return None

    def _repair_assignment(self, job, feedback):
        assignment = deepcopy(job["assignment"])
        if not feedback:
            return assignment
        assignment["validation_feedback"] = deepcopy(feedback)
        repair = assignment["validation_feedback"]
        if assignment.get("phase") == "exploration_plan":
            repair["response_contract"] = exploration_response_contract()
        elif assignment.get("phase") in {"blind_plan", "counter_plan"}:
            repair["response_contract"] = search_plan_response_contract(self.bounds["queries_per_role"])
        if feedback.get("finish_reason") == "length":
            # An unfinished reasoning transcript is not a partially valid
            # answer. Echoing it consumes context and encourages continuation.
            repair.pop("previous_response", None)
            repair["previous_response_omitted"] = True
            repair["scope"] = ("The response exhausted its output limit before completion. "
                "Return only the final JSON object using the required fields and enums. "
                "Keep explanations concise; omit analysis transcripts, preambles and repeated input. "
                "Preserve the evidence contract; use supplied evidence IDs when available.")
        if assignment.get("phase") == "gap_assessment":
            # Gap assessment is an aggregate decision over a large packet.  A
            # contract repair does not need to replay source windows, retrieval
            # logs, and the prior transcript: the evidence catalog already
            # contains the exact selectable spans and their owning work IDs.
            # Keeping the repair packet small prevents the verifier from
            # spending its output budget narrating the input instead of
            # returning the required object.
            assignment = self._compact_gap_repair_assignment(assignment)
        elif assignment.get("phase") == "survey_follow_up":
            assignment["evidence_catalog"] = self._follow_up_repair_catalog(assignment)
            assignment["sources"] = [
                {key: value for key, value in source.items() if key != "text"}
                for source in assignment["sources"]]
            assignment["instructions"] += (
                " In this repair, select evidence as [{evidence_id:ID}] from evidence_catalog. "
                "The captured quotation, owning work and exact offsets are attached deterministically. "
                "Never reconstruct quotations or change source references. Retain explicit limitations "
                "when the catalog does not support fulfillment.")
            previous = repair.pop("previous_response", None)
            if isinstance(previous, dict) and isinstance(previous.get("orders"), list):
                repair["previous_dispositions"] = [
                    {key: deepcopy(row[key]) for key in (
                        "id", "status", "rationale", "query_refs", "limitation", "next_action", "completion") if key in row}
                    for row in previous["orders"] if isinstance(row, dict)]
            repair.pop("previous_response_excerpt", None)
            repair["previous_response_omitted"] = True
        assignment = self._follow_up_assignment(assignment)
        limit = self._map_input_limit(job["actor"])
        if limit is None or estimate_input_tokens(SYSTEM, json.dumps(assignment, ensure_ascii=False)) <= limit:
            return assignment
        # Prior output is diagnostic context, not source evidence. Keep the
        # exact source assignment and bound only this optional repair echo.
        prior = json.dumps(repair.pop("previous_response", None), ensure_ascii=False)
        repair["previous_response_excerpt"] = ""
        repair["previous_response_omitted"] = True
        low, high = 0, len(prior)
        while low < high:
            middle = (low + high + 1) // 2
            repair["previous_response_excerpt"] = prior[:middle]
            if estimate_input_tokens(SYSTEM, json.dumps(assignment, ensure_ascii=False)) <= limit:
                low = middle
            else:
                high = middle - 1
        repair["previous_response_excerpt"] = prior[:low]
        if estimate_input_tokens(SYSTEM, json.dumps(assignment, ensure_ascii=False)) > limit:
            raise ModelWorkBlocked(f"{job['name']} repair contract cannot fit its configured input budget")
        return assignment

    @staticmethod
    def _assessment_evidence_ids(proofs, catalog):
        """Project map evidence to the stable IDs available to a repair call."""
        if not isinstance(proofs, list):
            return []
        ids = []
        for proof in proofs:
            if not isinstance(proof, dict):
                continue
            matches = [item for item in catalog if (
                item.get("work_id") == proof.get("work_id")
                and item.get("source_ref") == proof.get("source_ref")
                and item.get("quote") == proof.get("quote"))]
            if len(matches) == 1 and matches[0].get("evidence_id") not in ids:
                ids.append(matches[0]["evidence_id"])
        return ids

    def _compact_gap_repair_assignment(self, assignment):
        """Build a bounded, evidence-addressable packet for gap repairs.

        The initial assessment may legitimately include many source windows.
        Replaying that packet after a transport or cross-citation failure is
        counterproductive: it gives a reasoning-heavy model thousands of
        irrelevant tokens to restate.  This projection retains the question,
        coverage limits, claim index, exact evidence catalog, and validation
        contract while removing source bodies and prior model prose.
        """
        catalog = [deepcopy(item) for item in assignment.get("evidence_catalog", [])
                   if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)]
        map_body = assignment.get("map") if isinstance(assignment.get("map"), dict) else {}
        entries = []
        for entry in map_body.get("entries", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("work_id"), str):
                continue
            projected = {
                "work_id": entry["work_id"],
                "inclusion": entry.get("inclusion"),
                "reason": str(entry.get("reason") or "")[:320],
                "evidence_by_field": {},
                "statements": {},
            }
            for field in MAP_FIELDS:
                statement = entry.get(field)
                if not isinstance(statement, dict) or statement.get("text") is None:
                    continue
                ids = self._assessment_evidence_ids(statement.get("evidence"), catalog)
                projected["evidence_by_field"][field] = ids
                projected["statements"][field] = statement["text"]
            entries.append(projected)
        relationships = []
        for relation in map_body.get("relationships", []):
            if not isinstance(relation, dict):
                continue
            claim = relation.get("claim") if isinstance(relation.get("claim"), dict) else {}
            relationships.append({
                "source": relation.get("source"), "target": relation.get("target"),
                "kind": relation.get("kind"), "claim": claim.get("text"),
                "evidence_ids": self._assessment_evidence_ids(claim.get("evidence"), catalog),
            })
        coverage = self._compact_assessment_coverage(assignment.get("coverage", {}))
        # Search rows and source windows are useful bounds, but their detailed
        # payload is not needed to choose a same-work evidence ID.
        coverage["searches"] = [
            {key: row.get(key) for key in ("request", "outcome", "provider", "has_more")
             if key in row}
            for row in coverage.get("searches", []) if isinstance(row, dict)
        ]
        cited_source_refs = {
            item.get("source_ref") for item in catalog
            if isinstance(item.get("source_ref"), str)
        }
        cited_source_refs.update(
            ref for ref in assignment.get("verified_full_text_refs", [])
            if isinstance(ref, str)
        )
        sources = [
            {key: source.get(key) for key in (
                "source_ref", "work_id", "representation", "identity_verified",
                "available_chars", "window", "text", "source_availability")
             if key in source}
            for source in assignment.get("sources", [])
            if isinstance(source, dict)
            and source.get("source_ref") in cited_source_refs
        ]
        feedback = assignment.get("validation_feedback")
        if isinstance(feedback, dict):
            feedback = {key: deepcopy(value) for key, value in feedback.items()
                        if key not in {"previous_response", "previous_response_excerpt"}}
            if "previous_response" in assignment.get("validation_feedback", {}):
                feedback["previous_response_omitted"] = True
        return {
            "assignment": assignment.get("assignment"),
            "phase": "gap_assessment",
            "question": deepcopy(assignment.get("question")),
            "gap": deepcopy(assignment.get("gap")),
            "nomination_ref": assignment.get("nomination_ref"),
            "survey_ref": assignment.get("survey_ref"),
            "prerequisite_survey_ref": assignment.get("prerequisite_survey_ref"),
            "claim_index": (deepcopy(assignment["claim_index"])
                            if isinstance(assignment.get("claim_index"), dict) and "map" not in assignment
                            else {"entries": entries, "relationships": relationships}),
            "coverage": coverage,
            "sources": sources,
            "verified_full_text_refs": deepcopy(assignment.get("verified_full_text_refs", [])),
            "evidence_catalog": catalog,
            "required_checks": deepcopy(assignment.get("required_checks", [])),
            "allowed_check_outcomes": deepcopy(assignment.get("allowed_check_outcomes", [])),
            "instructions": assignment.get("instructions", _GAP_ASSESSMENT_INSTRUCTIONS),
            "source_evidence_policy": _SOURCE_EVIDENCE_POLICY,
            **({"validation_feedback": feedback} if feedback else {}),
            "scientific_input_recovery": scientific_input_recovery_contract(),
            **({"resume_boundary": assignment["resume_boundary"]}
               if "resume_boundary" in assignment else {}),
            **({"_contract_repair_boundary": assignment["_contract_repair_boundary"]}
               if "_contract_repair_boundary" in assignment else {}),
        }

    @staticmethod
    def _is_contract_failure(state):
        """Return whether a retained blocker is safe for one scoped model repair."""
        if not isinstance(state, dict):
            return False
        text = " ".join(str(state.get(key, "")) for key in ("error", "feedback")).casefold()
        return any(marker in text for marker in (
            "valid json", "evidence contract", "generation length",
            "did not finish normally", "unknown or duplicate required check",
        ))

    @staticmethod
    def _is_resource_dispatch_failure(outcome):
        return (isinstance(outcome, dict) and (
            outcome.get("error_type") in {"quota", "ModelCallError", "ModelBudgetExceededError", "StateError"}
            or outcome.get("budget_admission") is not None or outcome.get("status_code") == 429))

    def _legacy_resource_dispatch_failure(self, retained, name):
        """Recover discarded typed failure data from its exact owned dispatch."""
        if not isinstance(retained, dict) or retained.get("status") not in {"blocked", "repairing"}:
            return None
        cache = self.store.get(retained["cache_ref"])
        key = cache["artifact_id"].removeprefix("command/model-work/")
        if (cache["author"] != "command.controller" or not cache.get("score_ref")
                or not cache["artifact_id"].startswith("command/model-work/") or len(key) != 64):
            return None
        def verified_body(record):
            raw = self.store.read_body(record["body_hash"])
            if hashlib.sha256(raw).hexdigest() != record["body_hash"]:
                raise ValidationError("retained model-work provenance body hash mismatch")
            return json.loads(raw)
        verified_body(cache)
        lower = max((self.store.get(ref)["created_at"] for ref in cache["parents"]), default="")
        rows = self.control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE ? AND created_at>? AND created_at<=? ORDER BY created_at DESC",
            (f"command/failures/survey-{name}-%", lower, cache["created_at"]))
        for row in rows:
            failure = self.store.get(row["artifact_ref"])
            if len(failure["inputs"]) != 1:
                continue
            context = self.store.get(failure["inputs"][0]["ref"])
            expected_context = failure["artifact_id"].replace("command/failures/", "command/contexts/", 1)
            if (context["artifact_id"] != expected_context or not lower < context["created_at"] <= failure["created_at"]
                    or failure.get("score_ref") != cache.get("score_ref")
                    or context.get("score_ref") != cache.get("score_ref")):
                continue
            params = verified_body(context)
            if params.get("role") != failure["author"] or context["author"] != failure["author"]:
                continue
            try:
                assignment = json.loads(params["prompt"])
                assignment.pop("validation_feedback", None)
                model = params.get("_routing_client", params["client"])
                original_key = ModelWorkCache.key(scope=f"survey:{name}", role=params["role"],
                    system=SYSTEM, prompt=assignment, model=model)
            except (KeyError, TypeError, ValueError):
                continue
            if original_key != key:
                continue
            body = verified_body(failure)
            if retained.get("error") != f"{name}: {body.get('error')}":
                return None
            if not self._is_resource_dispatch_failure(body):
                return None
            return {**body, "failure_ref": failure["artifact_ref"], "context_ref": context["artifact_ref"],
                    "legacy_cache_ref": cache["artifact_ref"]}
        return None

    def _required_model_work(self):
        """Describe uncompleted first decisions without promising a positive verdict."""
        required = []
        if self._survey_acceptance_pending or not self.survey_ref:
            for wid, record in self.analysis_records.items():
                entry = self._body(record)
                relations = [relation for relation in self.relationships.values() if relation["source"] == wid]
                abstention = self.store.head(f"command/survey-abstentions/{wid}")
                if abstention and not relations and is_explicit_abstention(entry, self._body(abstention)):
                    continue
                if self._work_review_current(wid):
                    continue
                required.append({"name": f"work-review-{wid}", "phase": "work_review", "actor": "methods.work-reviewer", "work_id": wid})
            required.append({"name": "survey-review", "phase": "survey_review", "actor": "methods.survey-reviewer"})
        if not self.assessment_ref:
            if self.nomination is None and self.score.get("proposed_gap") is None:
                required.append({"name": "nominate", "phase": "nomination", "actor": "research.gap-proposer"})
            if not self.countersearch_complete:
                if self.counter_plan_record is None:
                    required.append({"name": "counter-plan", "phase": "counter_plan", "actor": "methods.novelty-challenger"})
                if not self._countersearch_active:
                    required.append({"name": "survey-review:countersearch", "phase": "survey_review", "actor": "methods.survey-reviewer"})
            required.append({"name": "gap-assessment", "phase": "gap_assessment", "actor": "methods.novelty-verifier"})
        if self.work_orders and self.follow_up_result is None:
            for order in self.work_orders:
                identity = hashlib.sha256(canonical_bytes(order)).hexdigest()
                if identity not in self._follow_up_decisions:
                    required.append({"name": f"follow-up-disposition-{identity}", "phase": "survey_follow_up",
                                     "actor": "methods.evidence-verifier", "work_order_id": order["id"]})
        return required

    def _remaining_model_capacity(self, roles):
        limits, scopes = [], []
        local = self.config["limits"].get("max_model_calls")
        if type(local) is int and enforce_model_cost_limits():
            limits.append(max(0, local - self.model_calls_dispatched))
        candidates = [self.dispatch_budget or {}, *self.model_call_budget_scopes]
        for role in sorted(set(roles)):
            spec = {"actor": role, "params": {"client": self.model_config, "role": role}}
            effective = self._base_model_config(spec)
            candidates.extend([effective, *effective.get("model_call_budget_scopes", [])])
            for route in role_routes_for(self.model_config, role):
                routed = self._route_model_config(spec, route)
                candidates.extend([routed, *routed.get("model_call_budget_scopes", [])])
        seen = set()
        for config in candidates:
            remaining = model_call_budget_remaining(config)
            if remaining is None:
                continue
            identity = (config["model_call_budget_path"], config["model_call_budget_key"])
            if identity in seen:
                continue
            seen.add(identity)
            limits.append(remaining)
            scopes.append({"path": identity[0], "key": identity[1], "remaining_calls": remaining,
                           "charged_tokens": model_token_budget_usage(config),
                           "token_limits": model_token_budget_limits(config)})
        return (min(limits) if limits else None), scopes

    def _allocate_model_wave(self, wave, *, repairing=()):
        required = self._required_model_work()
        for item in required:
            job = next((job for job in wave if job["name"] == item["name"]), None)
            item["input_tokens"] = (estimate_input_tokens(SYSTEM, json.dumps(job["assignment"], ensure_ascii=False)) if job else None)
        remaining, scopes = self._remaining_model_capacity([item["actor"] for item in required] + [job["actor"] for job in wave])
        if remaining is None:
            return wave
        admitted, induced = [], set()
        required_names = {item["name"] for item in required}
        pending_reviews = {item.get("work_id") for item in required if item["phase"] == "work_review"}
        for job in wave:
            optional = job["assignment"].get("phase") in {"map", "reading_selection", "exploration_plan"} or job["name"] in repairing
            if not optional:
                admitted.append(job)
                continue
            work_ids = job["assignment"].get("requested_work_ids", [])
            next_induced = induced | (set(work_ids) - pending_reviews)
            current_optional = sum((item["assignment"].get("phase") in {"map", "reading_selection", "exploration_plan"} or item["name"] in repairing)
                                   and item["name"] not in required_names for item in admitted)
            required_calls = len(required) + len(next_induced)
            incremental_call = int(job["name"] not in required_names)
            if remaining >= required_calls + current_optional + incremental_call:
                admitted.append(job)
                induced = next_induced
                continue
            plan = {"remaining_calls": remaining, "required_calls": required_calls,
                    "required_jobs": required, "induced_review_work_ids": sorted(next_induced),
                    "owned_scopes": scopes, "deferred_job": job["name"],
                    "repairing": job["name"] in repairing}
            debt = self._record(f"command/survey-resource-debts/{job['name']}", "note", plan, "command.controller")
            can_defer = len(work_ids) == 1
            if can_defer and work_ids[0] in self.analysis_records:
                entry = self._body(self.analysis_records[work_ids[0]])
                abstention = self.store.head(f"command/survey-abstentions/{work_ids[0]}")
                can_defer = bool(abstention and is_explicit_abstention(entry, self._body(abstention)))
            if not can_defer:
                raise QuotaExceededError("model-call capacity is reserved for required downstream decisions",
                    dimension="max_model_calls", limit=self.config["limits"].get("max_model_calls"),
                    observed=self.model_calls_dispatched, diagnostics=[{**plan, "debt_ref": debt["artifact_ref"]}])
            wid = work_ids[0]
            self._materialize_source_less_map(wid, self._analysis_basis(wid), scope="model_call_budget")
            self.gaps.append({"kind": "model_call_budget", "work_id": wid, "debt_ref": debt["artifact_ref"],
                              "remaining_calls": remaining, "required_calls": required_calls})
        self._record(f"command/survey-resource-plans/{self.serial}", "note", {
            "remaining_calls": remaining, "required_jobs": required, "owned_scopes": scopes,
            "induced_review_work_ids": sorted(induced), "requested_jobs": [job["name"] for job in wave],
            "allocated_jobs": [job["name"] for job in admitted],
        }, "command.controller")
        return admitted

    def _models_checked(self, jobs, *, stage="production", task_kind="production", on_contract_blocked=None):
        """Retain checked siblings and bound repairs of each exact assignment."""
        def normalize_response(job, value, *, assignment=None, execution_ref=None):
            if not job.get("normalizer"):
                return value
            if not job.get("normalizer_uses_assignment"):
                return job["normalizer"](value)
            if assignment is None:
                execution = self.store.get(execution_ref)
                context = self._body(self.store.get(execution["inputs"][0]["ref"]))
                assignment = json.loads(context["prompt"])
            return job["normalizer"](value, assignment)

        cache = ModelWorkCache(self.store, self._publish)
        pending, results, keys, generation_keys, states, feedback = [], {}, {}, {}, {}, {}

        def generation_failure(execution_ref):
            execution = self.store.get(execution_ref)
            result = ModelResult(**self._body(execution))
            if result.finish_reason != "length" or result.text.strip():
                raise StateError("generation capacity receipt does not bind an empty truncated response")
            params = self._body(self.store.get(execution["inputs"][0]["ref"]))
            limit = result.response_metadata.get("max_output_tokens", params["client"].get("max_output_tokens"))
            return QuotaExceededError(
                "Model generation exhausted its output capacity without an answer; change the assignment "
                "or execution capacity before retrying. An empty response cannot receive JSON format repair",
                dimension="generation_output_tokens", limit=limit,
                observed=result.usage.get("output_tokens"), diagnostics=[{
                    "failure_class": "resource_fence", "execution_ref": execution_ref,
                    "execution_sha256": execution["body_hash"], "finish_reason": result.finish_reason,
                    "response_metadata": deepcopy(result.response_metadata), "usage": deepcopy(result.usage)}])
        def response_validation_failure(job, state):
            origin = state.get("failure_origin")
            if origin is not None:
                return origin == "response_validation"
            return ("dispatch_failure" not in state and (
                state.get("failure_class") == "model_contract"
                or state.get("error", "").startswith(
                    f"{job['name']} did not satisfy its evidence contract: ")))

        def contract_blocked(job, state):
            error = ModelWorkBlocked.from_states([state])
            if on_contract_blocked is None or not response_validation_failure(job, state):
                raise error
            on_contract_blocked(job["name"], error)

        def retained_work(job, key):
            retained = cache.get(key)
            resource = retained.get("dispatch_failure", retained) if isinstance(retained, dict) else None
            if retained and not self._is_resource_dispatch_failure(resource):
                resource = self._legacy_resource_dispatch_failure(retained, job["name"]) or resource
            if self._is_resource_dispatch_failure(resource):
                if self.resume_session is None:
                    self._raise_dispatch_failures([resource], "retained model resource failure")
                return None
            if (self.resume_session is not None and retained
                    and retained.get("status") in {"blocked", "repairing"}
                    and retained.get("failure_origin") == "dispatch"
                    and isinstance(retained.get("dispatch_failure"), dict)
                    and retained["dispatch_failure"].get("ok") is False
                    and retained["dispatch_failure"].get("outcome_known") is True):
                # An explicit resume may retry a settled dispatch failure.
                # Unknown outcomes and checked responses retain their own
                # reconciliation and evidence-validation boundaries.
                return None
            if (retained and retained.get("status") in {"blocked", "repairing"}
                    and not response_validation_failure(job, retained)):
                raise ModelWorkBlocked.from_states([retained])
            return retained

        def abstain(job, state):
            handler = job.get("on_exhausted")
            if not handler or not state.get("feedback") or "did not satisfy its evidence contract" not in state.get("error", ""):
                return False
            value, execution = handler(state)
            job["validator"](value)
            job["on_valid"](value, execution)
            cache.put(keys[job["name"]], {"status": "abstained", "value": value, "execution_ref": execution,
                "error": state["error"], "repair_attempts": state.get("repair_attempts", 0)})
            results[job["name"]] = (value, execution)
            return True

        def recover_retained_response(job, retained_feedback):
            """Adopt a prior parsed answer if the current strict validator accepts it."""
            if not isinstance(retained_feedback, dict):
                return None
            if retained_feedback.get("finish_reason", "stop") not in {"stop", "length"}:
                return None
            previous = retained_feedback.get("previous_response")
            execution_ref = retained_feedback.get("execution_ref")
            if not isinstance(execution_ref, str):
                return None
            if (retained_feedback.get("finish_reason") == "length"
                    and isinstance(previous, dict) and isinstance(previous.get("raw_text"), str)
                    and not previous["raw_text"].strip()):
                model = {**self.model_config, **job.get("model_overrides", {})}
                owned = self._retained_settled_response(
                    job["name"], job["actor"], job["assignment"],
                    execution_ref=execution_ref, model=model)
                if owned is None:
                    raise StateError("empty retained generation lacks an exact settled dispatch owner")
                if owned["model_matches"]:
                    error = generation_failure(execution_ref)
                    cache.put(generation_keys[job["name"]], {
                        "status": "generation_capacity_exhausted", "execution_ref": execution_ref,
                        "error": str(error)}, subjects=[execution_ref])
                    raise error
            try:
                if isinstance(previous, dict):
                    raw_text = previous.get("raw_text")
                    recovered = (ModelResult(
                        text=raw_text, model="retained", usage={},
                        elapsed_seconds=0.0, finish_reason="stop",
                    ).json_object(allow_missing_closers=True)
                        if isinstance(raw_text, str) else deepcopy(previous))
                elif isinstance(previous, str):
                    recovered = ModelResult(
                        text=previous, model="retained", usage={},
                        elapsed_seconds=0.0, finish_reason="stop",
                    ).json_object(allow_missing_closers=True)
                else:
                    return None
                recovered = normalize_response(job, recovered, execution_ref=execution_ref)
                job["validator"](recovered)
            except (ValidationError, TypeError, ValueError, KeyError) as exc:
                retained_feedback["error"] = str(exc)
                retained_feedback["failure_class"] = getattr(exc, "failure_class", None) or "model_contract"
                return None
            execution = self.store.get(execution_ref)
            task_id = execution["artifact_id"].removeprefix("command/executions/")
            task = self.tasks.get(task_id)
            if task["state"] == "blocked":
                self.tasks.transition(task_id, "awaiting_review", "command.controller",
                                      reason="retained output passed current scoped validation")
            cache.put(keys[job["name"]], {
                "status": "succeeded", "value": recovered,
                "execution_ref": execution_ref,
                "recovered_from_retained_execution": True,
            }, subjects=[execution_ref])
            if job.get("on_valid"):
                job["on_valid"](recovered, execution_ref)
            if self.tasks.get(task_id)["state"] == "awaiting_review":
                self._complete(task_id)
            results[job["name"]] = (recovered, execution_ref)
            return recovered

        for job in jobs:
            job["assignment"] = self._follow_up_assignment(job["assignment"])
            model = {**self.model_config, **job.get("model_overrides", {})}
            generation_key = cache.key(scope=f"survey-generation:{job['name']}", role=job["actor"],
                system=SYSTEM, prompt=json.loads(self._response_assignment_identity(job["assignment"])), model=model)
            generation_keys[job["name"]] = generation_key
            capacity = cache.get(generation_key)
            if capacity is not None:
                raise generation_failure(capacity["execution_ref"])
            key = cache.key(scope=f"survey:{job['name']}", role=job["actor"],
                            system=SYSTEM, prompt=job["assignment"], model=model)
            keys[job["name"]] = key
            retained = retained_work(job, key)
            if retained and retained.get("status") in {"succeeded", "abstained"}:
                value = deepcopy(retained["value"])
                try:
                    job["validator"](value)
                except (ValidationError, TypeError, ValueError, KeyError):
                    retained = None
                else:
                    if job.get("on_valid"):
                        job["on_valid"](value, retained["execution_ref"])
                    results[job["name"]] = (value, retained["execution_ref"])
                    continue
            if retained is None and self.resume_session:
                retained_feedback = (self._retained_unreviewed_response(job["name"], job["actor"], job["assignment"])
                    or self._retained_validation_feedback(job["name"], job["assignment"]))
                if recover_retained_response(job, retained_feedback) is not None:
                    continue
                if retained_feedback is not None:
                    # A changed transport boundary is eligible for one fresh
                    # scoped repair only after the retained answer fails the
                    # current normalizer/validator.
                    states[job["name"]] = {"feedback": retained_feedback}
                    feedback[job["name"]] = retained_feedback
                    pending.append(job)
                    continue
            if retained and retained.get("status") == "blocked":
                # A provider can return a semantically complete JSON object
                # with a duplicated outer closing tail.  If the immutable
                # proposal is now parseable under the bounded model envelope
                # parser, adopt it from the retained execution rather than
                # paying for the same assignment again on resume.  Validator
                # failure still leaves the blocker intact.
                retained_feedback = self._retained_validation_feedback(
                    job["name"], job["assignment"])
                recovery = retained_feedback or retained.get("feedback")
                if recover_retained_response(job, recovery) is not None:
                    continue
                if abstain(job, retained):
                    continue
                # A Composer retry may reopen a stage while retaining the
                # same durable runner.  Do not replay an exhausted model-work
                # key forever: give this exact resume session one fresh cache
                # identity and a compact repair packet.  A second blocker on
                # that identity remains terminal for the session and is then
                # visible to the Composer's scoped recovery policy.
                if (isinstance(self.resume_session, dict)
                        and isinstance(self.resume_session.get("session"), int)
                        and (retained.get("failure_class") == "model_contract"
                             or isinstance(recovery, dict) and recovery.get("failure_class") == "model_contract"
                             or job["assignment"].get("phase") == "gap_assessment"
                             and self._is_contract_failure(retained))
                        and "_contract_repair_boundary" not in job["assignment"]):
                    repair_assignment = deepcopy(job["assignment"])
                    repair_assignment["_contract_repair_boundary"] = (
                        f"model-contract-repair-{self.resume_session['session']}")
                    job["assignment"] = repair_assignment
                    repair_key = cache.key(
                        scope=f"survey:{job['name']}", role=job["actor"],
                        system=SYSTEM, prompt=job["assignment"], model=model)
                    keys[job["name"]] = repair_key
                    repair_retained = retained_work(job, repair_key)
                    if repair_retained and repair_retained.get("status") in {"succeeded", "abstained"}:
                        value = deepcopy(repair_retained["value"])
                        try:
                            value = normalize_response(job, value, execution_ref=repair_retained["execution_ref"])
                            job["validator"](value)
                        except (ValidationError, TypeError, ValueError, KeyError) as exc:
                            raise ModelWorkBlocked(
                                f"{job['name']} retained repair failed validation: {exc}") from exc
                        if job.get("on_valid"):
                            job["on_valid"](value, repair_retained["execution_ref"])
                        results[job["name"]] = (value, repair_retained["execution_ref"])
                        continue
                    if repair_retained and repair_retained.get("status") == "blocked":
                        if abstain(job, repair_retained):
                            continue
                        contract_blocked(job, repair_retained)
                        continue
                    states[job["name"]] = repair_retained or {}
                    pending.append(job)
                    feedback[job["name"]] = recovery or retained.get("feedback")
                    continue
                contract_blocked(job, retained)
                continue
            states[job["name"]] = retained or {}
            pending.append(job)
        if not pending:
            return results
        for job in pending:
            if job["name"] in feedback:
                continue
            retained = (states[job["name"]].get("feedback") or
                        self._retained_validation_feedback(job["name"], job["assignment"]))
            if retained is not None:
                feedback[job["name"]] = retained
        rounds = range(self.config["limits"]["max_rounds"])
        # A multi-provider run uses a small rolling dispatch buffer.  The
        # execution runtime already backfills a returned slot immediately;
        # keeping the buffer bounded avoids unbounded prompt retention while
        # preventing a slow provider from imposing a full-wave barrier.
        dispatch_window = self.worker_slots * 2 if len(self.provider_pools) > 1 else self.worker_slots
        for _ in rounds:
            self._ensure_active()
            rejected = []
            for offset in range(0, len(pending), dispatch_window):
                wave = pending[offset:offset + dispatch_window]
                wave = self._allocate_model_wave(wave, repairing=set(feedback))
                if not wave:
                    continue
                self._tick(stage, count=len(wave), pending_review_count=len(results) if stage in {"production", "revision"} else 0)
                specs = []
                for job in wave:
                    self.serial += 1
                    repair = feedback.get(job["name"])
                    assignment = self._repair_assignment(job, repair)
                    client = deepcopy(self.model_config)
                    if isinstance(job.get("model_overrides"), dict):
                        client.update(job["model_overrides"])
                    specs.append({"task_id": f"survey-{job['name']}-{self.serial}", "kind": "model",
                        "actor": job["actor"], "task_kind": task_kind,
                        "params": {"client": client, "role": job["actor"],
                                   "prompt": json.dumps(assignment, ensure_ascii=False)}})
                outcomes = self._call_batch(specs, max_parallel=self.worker_slots)
                failures, generation_failures = [], []
                for job, spec in zip(wave, specs):
                    task_id, actor = spec["task_id"], spec["actor"]
                    outcome = outcomes[task_id]
                    if not outcome["ok"]:
                        if self._is_resource_dispatch_failure(outcome):
                            cache.put(keys[job["name"]], {
                                "status": "resource_blocked", "dispatch_failure": deepcopy(outcome),
                                "error": f"{job['name']}: {outcome['error']}",
                            })
                        else:
                            attempts = states[job["name"]].get("repair_attempts", 0) + 1
                            states[job["name"]] = cache.put(keys[job["name"]], {
                                "status": "blocked" if attempts >= self.config["limits"]["max_rounds"] else "repairing",
                                "failure_origin": "dispatch", "dispatch_failure": deepcopy(outcome),
                                "repair_attempts": attempts, "feedback": feedback.get(job["name"]),
                                "error": f"{job['name']}: {outcome['error']}",
                            })
                        failures.append(outcome)
                        continue
                    result = ModelResult(**outcome["result"])
                    execution = outcome["record_ref"]
                    self.time_policy.observe(stage, result.elapsed_seconds)
                    try:
                        value = result.json_object(allow_missing_closers=True)
                    except ValidationError:
                        value = {"raw_text": result.text}
                    proposal = self._publish(f"kb/model-proposals/{task_id}", "note", value, actor, subjects=[execution])
                    if result.finish_reason == "length" and not result.text.strip():
                        error = generation_failure(execution)
                        self.tasks.transition(task_id, "blocked", "command.controller", reason=str(error))
                        self._publish(f"command/validation/{task_id}", "note", {
                            "error": str(error), "finish_reason": result.finish_reason,
                            "failure_class": "resource_fence", "dimension": error.dimension},
                            "command.controller", subjects=[proposal["artifact_ref"]])
                        cache.put(generation_keys[job["name"]], {
                            "status": "generation_capacity_exhausted", "execution_ref": execution,
                            "error": str(error)}, subjects=[execution])
                        generation_failures.append(error)
                        continue
                    try:
                        transport_recovered = False
                        if result.finish_reason not in {"stop", "length"}:
                            raise ModelContractError(f"model generation did not finish normally: {result.finish_reason}")
                        try:
                            value = result.json_object(allow_missing_closers=True)
                        except ValidationError as exc:
                            raise ModelContractError(str(exc)) from exc
                        transport_recovered = result.finish_reason == "length"
                        value = normalize_response(job, value, assignment=json.loads(spec["params"]["prompt"]))
                        job["validator"](value)
                    except (ValidationError, TypeError, ValueError, KeyError) as exc:
                        failure_class = getattr(exc, "failure_class", None) or "model_contract"
                        partial = (job["on_validation_failure"](execution)
                                   if job.get("on_validation_failure") else False)
                        feedback[job["name"]] = {"error": str(exc), "previous_response": value,
                            "finish_reason": result.finish_reason,
                            "scope": "Repair only this assignment's contract violations; preserve every valid field. "
                                     "Use every requested field name and enum value exactly as specified; do not substitute synonyms."}
                        self.tasks.transition(task_id, "blocked", "command.controller", reason=str(exc))
                        self._publish(f"command/validation/{task_id}", "note",
                                      {"error": str(exc), "finish_reason": result.finish_reason,
                                       "failure_class": failure_class},
                                      "command.controller", subjects=[proposal["artifact_ref"]])
                        attempts = states[job["name"]].get("repair_attempts", 0) + 1
                        exhausted = partial or attempts >= self.config["limits"]["max_rounds"]
                        states[job["name"]] = cache.put(keys[job["name"]], {
                            "status": "blocked" if exhausted else "repairing",
                            "failure_origin": "response_validation",
                            "repair_attempts": attempts, "feedback": feedback[job["name"]],
                            "failure_class": failure_class,
                            "error": f"{job['name']} did not satisfy its evidence contract: {exc}",
                        }, subjects=[proposal["artifact_ref"]])
                        rejected.append(job)
                        continue
                    self._ensure_active()
                    cache.put(keys[job["name"]], {
                        "status": "succeeded", "value": value, "execution_ref": execution,
                        "transport_recovered": transport_recovered,
                    }, subjects=[execution])
                    if job.get("on_valid"):
                        job["on_valid"](value, execution)
                    self._complete(task_id)
                    results[job["name"]] = (value, execution)
                if generation_failures:
                    error = generation_failures[0]
                    error.diagnostics = [item for failure in generation_failures for item in failure.diagnostics]
                    error.dispatch_failures = deepcopy(failures)
                    raise error
                if failures:
                    if any(self._is_resource_dispatch_failure(item) for item in failures):
                        self._raise_dispatch_failures(failures, "model resource dispatch failed")
                    else:
                        exhausted = [states[job["name"]] for job in wave
                                     if states[job["name"]].get("status") == "blocked"]
                        if exhausted:
                            raise ModelWorkBlocked.from_states(exhausted)
                    self._raise_dispatch_failures(failures, "model dispatch failed")
            if not rejected:
                return results
            exhausted = [job for job in rejected if states[job["name"]].get("status") == "blocked"]
            resolved = {job["name"] for job in exhausted if abstain(job, states[job["name"]])}
            exhausted = [job for job in exhausted if job["name"] not in resolved]
            rejected = [job for job in rejected if job["name"] not in resolved]
            if exhausted:
                if on_contract_blocked is None:
                    raise ModelWorkBlocked.from_states(states[job["name"]] for job in exhausted)
                for job in exhausted:
                    contract_blocked(job, states[job["name"]])
                exhausted_names = {job["name"] for job in exhausted}
                rejected = [job for job in rejected if job["name"] not in exhausted_names]
            if not rejected:
                return results
            pending = rejected
        if on_contract_blocked is None:
            raise ModelWorkBlocked.from_states(states[job["name"]] for job in pending)
        for job in pending:
            contract_blocked(job, states[job["name"]])
        return results

    def _plan_validator(self, value):
        validate_search_plan(value, self.bounds["queries_per_role"])

    @property
    def model_config(self):
        return self._model_execution_config if self._model_execution_config is not None else self.config["model"]

    def _record_model_execution_controls(self):
        if self.model_config != self.config["model"]:
            controls = frozenset({"max_output_tokens", "max_input_tokens", "reasoning_effort"})
            def projection(value):
                if isinstance(value, dict):
                    return {key: (item if key in controls else projection(item))
                            for key, item in value.items()
                            if key in controls or isinstance(item, (dict, list))}
                if isinstance(value, list):
                    return [projection(item) for item in value]
                return value
            body = {"model_config_sha256": hashlib.sha256(canonical_bytes(self.model_config)).hexdigest(),
                    "execution_controls": projection(self.model_config)}
            digest = hashlib.sha256(canonical_bytes(body)).hexdigest()
            self._record(f"command/model-execution-controls/{digest}", "note", body, "command.controller")

    def _initialize(self):
        self._record_model_execution_controls()
        score = self._publish(f"command/scores/{self.score['id']}", "note",
            {"schema_version": "literature-survey-score-1", "survey": self.score,
             "time_policy": self.config.get("time_policy")}, "principal")
        self.score_ref = score["artifact_ref"]
        if self.review_obligations:
            digest = hashlib.sha256(canonical_bytes(self.review_obligations)).hexdigest()
            self._record(f"command/survey-review-obligations/{digest}", "note",
                {"obligations": self.review_obligations}, "command.controller",
                subjects=[ref for obligation in self.review_obligations
                          for ref in [obligation["entry_ref"],
                              *[pin["ref"] for pin in obligation["relationship_pins"]],
                              *[pin["ref"] for pin in obligation.get("comparison_pins", [])],
                              *[pin["ref"] for pin in obligation["source_pins"]]]])
        self.protocol = self._publish("kb/search-protocol", "search_campaign", {
            "question": self.score["question"], "seed_queries": self.score["seed_queries"],
            "seed_work_ids": self.score["seed_work_ids"], "bounds": self.bounds,
            "scope": "Configured providers and finite search/citation batches; not exhaustive scholarly coverage",
            "publication_metadata": "Provider-reported, not independently confirmed publication history",
            "independence": "Separately planned topic searches before the nominated gap is exposed; later targeted counter-search",
        }, "command.search-coordinator", subjects=[self.score_ref])
        self._checkpoint("survey_initialized", force=True)

    def _setup(self):
        self._tick("setup")
        started = time.monotonic()
        for key in ("bibliography", "identity", "full_text"):
            definition = self.score.get(key)
            if definition is None:
                continue
            client = deepcopy(definition["client"])
            if definition["adapter"] == "mcp_fetch":
                client["result_max_bytes"] = self.config["limits"]["max_result_bytes"]
                client.update(cwd=str(self.operations.workspace_dir(definition["id"])), own_process_group=False)
            if self.resume_session:
                self.operations.reconcile_interrupted(definition["id"], resume_ref=self.resume_session["artifact_ref"])
            self.operations.register(definition["id"], adapter=definition["adapter"], client=client,
                representative=definition["representative"], environment_files=definition["environment_files"],
                engineer="operations.engineer")
            self.capability_ids.append(definition["id"])
        for key in ("bibliography", "identity", "full_text"):
            definition = self.score.get(key)
            if definition is None:
                continue
            self._wait_provider(key)
            state = self.operations.ensure_ready(definition["id"], self._call, operator="operations.operator",
                verifier="operations.verifier", purpose=self.score["question"])
            if state["state"] != "ready":
                detail = self._failure_detail(definition["id"])
                self._stop_provider_rate_limit(
                    {"kind": key + "_readiness_failure", "capability_id": definition["id"],
                     "reason": state.get("reason"), **detail},
                    provider="openalex" if key == "bibliography" else "crossref" if key == "identity" else key)
                if key == "bibliography":
                    if self.bibliography_fallback_policy == "disabled":
                        if detail.get("outcome") in {"auth_required", "access_denied"}:
                            client = definition.get("client", {})
                            raise ProviderConfigurationError(
                                "OpenAlex bibliography is not authenticated; configure "
                                f"{client.get('auth_env') or 'the configured credential'} before retrying",
                                provider="openalex",
                                credential_env=client.get("auth_env"),
                            )
                        self._raise_provider_cooldown(
                            detail,
                            "OpenAlex survey readiness is paused until the provider quota resets",
                        )
                        raise ValidationError(
                            "OpenAlex readiness failed while bibliography fallback is disabled: "
                            + str(state.get("reason") or "unknown provider failure"))
                    # A provider outage or exhausted quota must not strand a
                    # whole run when the configured Crossref identity service
                    # can still supply bounded metadata.  The fallback is
                    # activated only after its own independent readiness check
                    # below; the degraded OpenAlex state remains recorded.
                    self.bibliography_mode = "crossref"
                    self.bibliography_fallback_reason = state.get("reason") or "OpenAlex readiness failed"
                    self.gaps.append({"kind": "bibliography_fallback", "from": "openalex", "to": "crossref",
                                      "reason": self.bibliography_fallback_reason})
                    continue
                self.gaps.append({"kind": key + "_unavailable", "reason": state["reason"]})
                continue
            self.bindings[key] = state["binding"]
            self.operations.idle(definition["id"])
        if self.bibliography_mode == "crossref":
            if "identity" not in self.bindings:
                raise ValidationError("bibliographic capability unavailable and Crossref fallback is not ready")
            self.bibliography_fallback_capability = self.score["identity"]["id"]

    _crossref_work = staticmethod(project_crossref_work)

    def _failure_detail(self, capability_id):
        """Read provider status from either a failed probe or routine workload."""
        try:
            state = self.operations.status(capability_id)
            execution_ref = None
            failure_ref = state.get("failure_ref")
            if failure_ref:
                body = self._body(self.store.get(failure_ref))
                execution_ref = body.get("execution_ref")
            if not execution_ref and state.get("verification_ref"):
                verification = self._body(self.store.get(state["verification_ref"]))
                execution_ref = verification.get("execution_ref")
            if not execution_ref and state.get("probe_ref"):
                probe = self._body(self.store.get(state["probe_ref"]))
                execution_ref = probe.get("execution_ref")
            if execution_ref:
                execution = self._body(self.store.get(execution_ref))
                metadata = execution.get("metadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                return {
                    "outcome": execution.get("outcome") or metadata.get("http_status"),
                    "http_status": metadata.get("http_status"),
                    "rate_limit": deepcopy(execution.get("rate_limit", metadata.get("rate_limit"))),
                    "execution_ref": execution_ref,
                    "metadata": deepcopy(metadata),
                    "error": execution.get("error"),
                    **{key: deepcopy(execution[key]) for key in _HTTP_STATUS_FIELDS if key in execution},
                }
            return {"outcome": None, "http_status": None, "rate_limit": None,
                    "execution_ref": None}
        except (KeyError, TypeError, ValueError, ContractError):
            return {"outcome": None, "http_status": None, "rate_limit": None,
                    "execution_ref": None}

    def _failure_outcome(self, capability_id):
        return self._failure_detail(capability_id)["outcome"]

    @staticmethod
    def _raise_provider_cooldown(detail, context):
        if not isinstance(detail, dict) or detail.get("outcome") not in {"rate_limited", 429}:
            return
        delay = provider_cooldown_seconds(detail.get("rate_limit"))
        if delay is not None:
            raise ProviderCooldownError(
                context, retry_after_seconds=delay,
                rate_limit=detail.get("rate_limit"),
            )

    def _activate_crossref_fallback(self, reason):
        if self.bibliography_fallback_policy == "disabled":
            raise ValidationError(
                "OpenAlex failed while bibliography fallback is disabled: " + str(reason))
        if self.bibliography_mode == "crossref":
            return
        self.bibliography_mode = "crossref"
        # Routing switches to the independently admitted Crossref binding.
        # The degraded OpenAlex capability remains in the operations ledger.
        self.bindings.pop("bibliography", None)
        self.bibliography_fallback_reason = str(reason)
        reserve = self.bounds.get("challenge_reserve", 0)
        minimum = max(reserve + 1, len(self.score["seed_work_ids"]) + reserve)
        fallback_cap = max(minimum, 20)
        requested = self.bounds["max_works"]
        if requested > fallback_cap:
            self.bounds["max_works"] = fallback_cap
            self.work_budget_adjustments.append({
                "kind": "provider_fallback_fit",
                "requested_max_works": requested,
                "effective_max_works": fallback_cap,
                "minimum_preserved": minimum,
                "reason": "Crossref metadata fallback is bounded to the single-worker review window",
            })
            self.time_policy.rebudget(fallback_cap)
        self.gaps.append({"kind": "bibliography_fallback", "from": "openalex", "to": "crossref",
                          "reason": self.bibliography_fallback_reason})
        if "identity" in self.bindings:
            self.bibliography_fallback_capability = self.score["identity"]["id"]

    def _crossref_bibliographic_call(self, arguments, *, role, plan_ref=None, admission="discovery"):
        """Run one bounded Crossref search through the verified identity cell."""
        capability = self.bibliography_fallback_capability or self.score.get("identity", {}).get("id")
        binding = self.bindings.get("identity") if capability else None
        if binding is None:
            raise ValidationError("Crossref fallback binding is unavailable")
        query = arguments.get("query")
        if arguments.get("operation") == "work":
            known = self.works.get(arguments.get("work_id"), {})
            query = known.get("doi") or arguments.get("work_id")
        if not isinstance(query, str) or not query.strip():
            self.gaps.append({"kind": "bibliography_fallback_unqueryable", "request": arguments})
            return None
        admission_limit = self.bounds["max_works"] - (
            self.bounds.get("challenge_reserve", 0) if admission == "discovery" else 0)
        remaining = admission_limit - len(self.works)
        if remaining <= 0 and admission == "discovery":
            self.gaps.append({"kind": "work_limit", "request": arguments, "provider": "crossref"})
            return None
        provider_arguments = {"query": query, "limit": min(arguments["limit"], max(1, remaining))}
        if arguments.get("cursor") is not None:
            provider_arguments["cursor"] = arguments["cursor"]
        self._wait_provider("identity")
        self._reserve_api_call("bibliography", arguments, role)
        try:
            result, execution = self.operations.run(binding, provider_arguments, self._call, operator=role)
        except ProviderRateLimitError:
            raise
        except Exception as exc:
            self._stop_provider_rate_limit(
                {"kind": "bibliographic_failure", "request": arguments, "provider": "crossref",
                 "error": str(exc), **self._failure_detail(capability)}, provider="crossref")
            raise
        if isinstance(result, dict):
            self._stop_provider_rate_limit(
                {**result, "kind": "bibliographic_failure", "request": arguments, "provider": "crossref",
                 "execution_ref": execution}, provider="crossref")
        works = [self._crossref_work(source) for source in result.get("sources", [])]
        if result.get("outcome") not in {"ok", "empty"}:
            works = []
            self.gaps.append({"kind": "bibliographic_failure", "request": arguments,
                              "provider": "crossref", "outcome": result.get("outcome"),
                              "error": result.get("error")})
        added = self._ingest(works, execution, admission=admission)
        metadata = result.get("metadata", {})
        body = {
            "request": arguments, "provider": "crossref", "provider_request": provider_arguments,
            "role": role, "execution_ref": execution, "plan_ref": plan_ref,
            "returned_work_ids": [work["id"] for work in works], "new_unique_works": added,
            "count": len(works), "provider_total_results": metadata.get("total_results"),
            "outcome": result.get("outcome"),
            "provider_http_status": metadata.get("http_status"),
            "next_cursor": metadata.get("next_cursor"),
            "has_more": result.get("outcome") == "ok" and (
                metadata.get("result_set_complete") is False),
        }
        if self._active_tree_action is not None:
            body["tree_action_id"] = self._active_tree_action
        record = self._publish(f"kb/queries/{self.api_calls}", "query_record", body, role,
                               subjects=[execution, *([plan_ref] if plan_ref else [])])
        self.query_refs.append(record["artifact_ref"])
        self.search_log.append(body)
        self._update_register()
        self._checkpoint("literature_captured", force=True)
        return body
        self.time_policy.observe("setup", time.monotonic() - started)

    def _initial_plan_id(self, role):
        identity = role.replace(".", "-")
        if self.work_orders:
            identity += "-" + hashlib.sha256(canonical_bytes(self.work_orders)).hexdigest()
        return identity

    def _initial_plans(self):
        plans, jobs = [], []
        for role in self._initial_search_roles():
            plan_id = self._initial_plan_id(role)
            retained = self.store.head(f"kb/search-plans/{plan_id}") if self.resume_session else None
            if retained is not None:
                value = self._body(retained)
                value = {key: value[key] for key in ("queries", "rationale")}
                self._plan_validator(value)
                plans.append((role, value["queries"], retained["artifact_ref"]))
                continue
            assignment = {"assignment": "Plan a topic search without assuming a particular research gap.",
                "phase": "blind_plan", "question": self.score["question"],
                "seed_terms": self.score["seed_queries"], "max_queries": self.bounds["queries_per_role"],
                "response_contract": search_plan_response_contract(self.bounds["queries_per_role"]),
                "search_syntax": SEARCH_SYNTAX,
                "instructions": "Return {queries:[search strings],rationale:string}. Use a distinct terminology or neighboring method family. Do not assert novelty."}
            def integrate(value, execution, *, role=role, plan_id=plan_id):
                record = self._publish(f"kb/search-plans/{plan_id}", "note", {
                    **value, **({"follow_up_ref": self.follow_up_ref} if self.work_orders else {})},
                    role, subjects=[execution, *([self.follow_up_ref] if self.work_orders else [])])
                plans.append((role, value["queries"], record["artifact_ref"]))
            jobs.append({"name": plan_id, "actor": role, "assignment": assignment,
                         "validator": self._plan_validator, "on_valid": integrate})
        if not jobs:
            return plans
        self._models_checked(jobs, stage="supervision", task_kind="service")
        return plans

    def _initial_search_roles(self):
        # Concept queries are authored with the design. The evidence-tree
        # planner still prioritizes actual acquisition and checked-source gaps.
        return () if self.score.get("design_brief") is not None else SEARCH_PLANNERS

    def _ingest(self, works, execution, *, admission):
        if admission not in {"discovery", "challenge"}:
            raise ValidationError("literature admission phase is invalid")
        admission_limit = self.bounds["max_works"]
        if admission == "discovery":
            admission_limit -= self.bounds.get("challenge_reserve", 0)
        added = 0
        for observed in works:
            wid = self.aliases.get(observed["id"]) or self.dois.get(observed["doi"]) or observed["id"]
            if wid not in self.works and len(self.works) >= admission_limit:
                self.gaps.append({"kind": "work_limit", "work_id": observed["id"],
                                  "admission": admission,
                                  "reserved_challenge_slots": self.bounds.get("challenge_reserve", 0)})
                continue
            self.aliases[observed["id"]] = wid
            if observed["doi"]:
                self.dois[observed["doi"]] = wid
            old = self.works.get(wid)
            item = deepcopy(observed)
            item.update(work_id=wid, id=wid, provider_ids=sorted({observed["id"], *(old or {}).get("provider_ids", [])}),
                        publication_metadata_status="provider_reported")
            # Duplicate identities retain their first record and every observed provider ID.
            if old and observed["id"] != wid:
                item = {**old, "provider_ids": item["provider_ids"]}
            if not old:
                added += 1
            if old != item:
                self.works[wid] = item
                record = self._record(f"kb/works/{wid}", "reference_card", item, "research.cataloger", subjects=[execution])
                self.work_records[wid] = record
            if item.get("abstract"):
                current = self.source_records.get(f"abstract/{wid}")
                previous = self.source_docs.get(current["artifact_ref"]) if current else None
                if previous is None or previous["text"] != item["abstract"]:
                    source_url = item.get("source_url") or ("https://openalex.org/" + wid)
                    body = {"work_id": wid, "representation": "abstract", "text": item["abstract"],
                            "url": source_url, "execution_ref": execution, "identity_verified": False}
                    source = self._publish(f"kb/abstracts/{wid}", "source_capture", body,
                                           "research.cataloger", subjects=[record["artifact_ref"] if old != item else self.work_records[wid]["artifact_ref"], execution])
                    if current:
                        self.source_docs.pop(current["artifact_ref"], None)
                    self.source_records[f"abstract/{wid}"] = source
                    self.source_docs[source["artifact_ref"]] = body
        return added

    def _bibliographic_call(self, operation, *, role, query=None, work_id=None, cursor=None,
                            plan_ref=None, admission="discovery", result_limit=None):
        self._ensure_active()
        # The discovery/expansion budget must not consume the calls reserved
        # for the independent challenge.  ``challenge_reserve`` already
        # protects work admission; the same revision-5 policy reserves one
        # full challenge query tranche so that a saturated discovery campaign
        # can still execute the counter-search.  Challenge calls are therefore
        # allowed through the base cap plus the configured planner width,
        # while every call remains charged in the immutable reservation ledger.
        api_limit = self.bounds["max_api_calls"]
        if admission == "challenge":
            api_limit += max(1, self.bounds.get("queries_per_role", 1))
        if self.api_calls >= api_limit:
            self.gaps.append({"kind": "api_call_limit", "operation": operation, "query": query, "work_id": work_id})
            return None
        if result_limit is None:
            result_limit = self.bounds["results_per_query"]
        if type(result_limit) is not int or result_limit < 1:
            raise ValidationError("bibliographic result_limit must be a positive integer")
        result_limit = min(result_limit, self.bounds["results_per_query"])
        arguments = {"operation": operation, "query": query, "work_id": work_id,
                     "limit": result_limit, "cursor": cursor}
        if self.bibliography_mode == "crossref":
            if operation == "citing":
                self.gaps.append({"kind": "bibliography_fallback_unsupported", "operation": operation,
                                  "work_id": work_id, "provider": "crossref"})
                return None
            return self._crossref_bibliographic_call(arguments, role=role, plan_ref=plan_ref, admission=admission)
        self._wait_provider("bibliography")
        self._reserve_api_call("bibliography", arguments, role)
        try:
            result, execution = self.operations.run(self.bindings["bibliography"], arguments, self._call, operator=role)
        except ProviderRateLimitError:
            raise
        except Exception as exc:
            self._ensure_active()
            self.gaps.append({"kind": "bibliographic_failure", "request": arguments, "error": str(exc)})
            detail = self._failure_detail(self.score["bibliography"]["id"])
            self._stop_provider_rate_limit(
                {"kind": "bibliographic_failure", "request": arguments, "provider": "openalex",
                 "error": str(exc), **detail}, provider="openalex")
            outcome = detail["outcome"]
            fallback_outcomes = {"rate_limited", "timeout", "provider_error", "auth_required", "access_denied", 408, 425, 429, 500, 502, 503, 504}
            if (operation in {"search", "work"} and self.score.get("identity")
                    and outcome in fallback_outcomes
                    and self.bibliography_fallback_policy == "crossref_metadata"):
                self._activate_crossref_fallback(outcome)
                if operation == "search" or self.works.get(work_id, {}).get("doi"):
                    return self._crossref_bibliographic_call(
                        {**arguments, "cursor": None}, role=role, plan_ref=plan_ref, admission=admission)
                self.gaps.append({"kind": "bibliography_fallback_unsupported", "operation": operation,
                                  "work_id": work_id, "provider": "crossref"})
                return None
            if (operation in {"work", "citing"} and outcome in fallback_outcomes
                    and self.bibliography_fallback_policy == "crossref_metadata"):
                return None
            if self.bibliography_fallback_policy == "disabled":
                try:
                    self._raise_provider_cooldown(
                        detail,
                        "OpenAlex survey retrieval is paused until the provider quota resets",
                    )
                except ProviderCooldownError as cooldown:
                    raise cooldown from exc
            raise
        if isinstance(result, dict):
            self._stop_provider_rate_limit(
                {**result, "kind": "bibliographic_failure", "request": arguments, "provider": "openalex",
                 "execution_ref": execution}, provider="openalex")
        if not isinstance(result, dict) or not isinstance(result.get("works"), list):
            raise ValidationError("bibliographic capability returned no normalized works list")
        metadata = result.get("metadata")
        if not isinstance(metadata, dict):
            raise ValidationError("bibliographic capability returned no metadata object")
        outcome = result.get("outcome")
        provider_failure = outcome in {
            "provider_error", "timeout", "auth_required", "access_denied",
            "rate_limited",
        } or (outcome == "not_found" and operation != "work")
        if provider_failure:
            detail = {
                "outcome": outcome,
                "http_status": metadata.get("http_status"),
                "rate_limit": metadata.get("rate_limit"),
                "execution_ref": execution,
            }
            self.gaps.append({
                "kind": "bibliographic_failure",
                "request": arguments,
                "provider": "openalex",
                "outcome": outcome,
                "http_status": metadata.get("http_status"),
                "execution_ref": execution,
                "error": result.get("error"),
            })
            if outcome in {"auth_required", "access_denied"}:
                raise ValidationError(
                    f"OpenAlex {outcome} is not a query-level gap; stop bibliography dispatch"
                )
        # A concrete OpenAlex work lookup can legitimately return a verified
        # 404.  That negative result has no list-pagination ``meta`` block,
        # but it still needs a durable query record so citation expansion can
        # continue.  Search and citing responses must retain the full page
        # contract; do not turn a malformed successful response into a zero-
        # result record.
        if (operation == "work" and result.get("outcome") == "not_found"
                and metadata.get("http_status") == 404 and not result["works"]):
            page = {"count": 0, "next_cursor": None, "has_more": False}
        elif provider_failure:
            page = {"count": 0, "next_cursor": None, "has_more": False}
        else:
            required = ("count", "next_cursor", "has_more")
            if any(key not in metadata for key in required):
                raise ValidationError(
                    "bibliographic capability returned incomplete pagination metadata "
                    f"for {operation} ({result.get('outcome')})")
            page = {key: metadata[key] for key in required}
        added = self._ingest(result["works"], execution, admission=admission)
        body = {"request": arguments, "role": role, "execution_ref": execution, "plan_ref": plan_ref,
                "returned_work_ids": [w["id"] for w in result["works"]], "new_unique_works": added,
                "count": page["count"], "next_cursor": page["next_cursor"], "has_more": page["has_more"],
                "outcome": outcome, "provider_error": result.get("error") if provider_failure else None,
                "provider_http_status": metadata.get("http_status")}
        if self._active_tree_action is not None:
            body["tree_action_id"] = self._active_tree_action
        record = self._publish(f"kb/queries/{self.api_calls}", "query_record", body, role,
                               subjects=[execution, *([plan_ref] if plan_ref else [])])
        self.query_refs.append(record["artifact_ref"])
        self.search_log.append(body)
        self._update_register()
        self._checkpoint("literature_captured", force=True)
        return body

    @staticmethod
    def _balanced_query_limit(work_slots, query_count, provider_limit):
        """Reserve discovery capacity for every independent query family.

        A provider's first broad query can return enough records to fill the
        whole work budget.  Later terminology families then appear to have
        found nothing even when they returned distinct records.  A per-query
        ceiling gives each family a bounded chance to contribute; remaining
        capacity can still be used by expansion and challenge passes.
        """
        if type(provider_limit) is not int or provider_limit < 1:
            raise ValidationError("provider_limit must be a positive integer")
        if type(work_slots) is not int or work_slots < 1:
            return 1
        if type(query_count) is not int or query_count < 1:
            return provider_limit
        return max(1, min(provider_limit, (work_slots + query_count - 1) // query_count))

    def _search(self, queries, role, plan_ref=None, *, admission="discovery", result_limit=None):
        if result_limit is not None and (type(result_limit) is not int or result_limit < 1):
            raise ValidationError("bibliographic result_limit must be a positive integer")
        limit = min(self.bounds["results_per_query"] if result_limit is None else result_limit,
                    self.bounds["results_per_query"])
        current_plans = set()
        if admission != "challenge" and self.work_orders:
            for row in self.search_log:
                recorded_plan = row.get("plan_ref")
                if (acquisition_succeeded(row) and isinstance(recorded_plan, str)
                        and row.get("request", {}).get("operation") == "search"
                        and row.get("role") != "methods.novelty-challenger"
                        and self._current_follow_up_plan(recorded_plan)):
                    current_plans.add(recorded_plan)
        seen = {query_identity(row["request"]["query"]) for row in self.search_log
                 if (row.get("request", {}).get("operation") == "search"
                     and row["request"].get("query")
                     and row["request"].get("cursor") is None
                     and row["request"].get("limit") == limit
                     and row.get("provider", "openalex") == self.bibliography_mode
                     and acquisition_succeeded(row)
                     and (admission != "challenge" and not self.work_orders
                          or row.get("plan_ref") == plan_ref
                          or row.get("plan_ref") in current_plans))}
        for query in queries:
            if query_identity(query) in seen:
                continue
            seen.add(query_identity(query))
            self._bibliographic_call("search", role=role, query=query, plan_ref=plan_ref,
                                     admission=admission, result_limit=result_limit)

    def _pending_bibliographic_pages(self):
        successful_pages = {
            (row.get("provider", "openalex"), row.get("request", {}).get("operation"),
             row.get("request", {}).get("query"), row.get("request", {}).get("work_id"),
             row.get("request", {}).get("cursor"), row.get("request", {}).get("limit"))
            for row in self.search_log if acquisition_succeeded(row)}
        pending = {}
        for row in self.search_log:
            request = row.get("request", {})
            cursor = row.get("next_cursor")
            key = (row.get("provider", "openalex"), request.get("operation"),
                   request.get("query"), request.get("work_id"), cursor, request.get("limit"))
            if (acquisition_succeeded(row) and row.get("has_more")
                    and (cursor is None or key not in successful_pages)):
                pending[key] = row
        return list(pending.values())

    def _complete_search_pages(self, *, admission="discovery"):
        """Advance finite cursor chains after every query family gets a first page."""
        attempted = set()
        while True:
            rows = [row for row in self._pending_bibliographic_pages()
                    if row.get("request", {}).get("operation") == "search"
                    and row.get("provider", "openalex") == self.bibliography_mode
                    and (row.get("role") == "methods.novelty-challenger") == (admission == "challenge")]
            if not rows:
                break
            progressed = False
            for row in rows:
                if row.get("provider", "openalex") != self.bibliography_mode:
                    continue
                request = row["request"]
                key = (row.get("provider", "openalex"), request.get("query"), row.get("next_cursor"))
                if key in attempted or row.get("next_cursor") is None:
                    continue
                attempted.add(key)
                limit = self.bounds["max_works"] - (
                    self.bounds.get("challenge_reserve", 0) if admission == "discovery" else 0)
                remaining = limit - len(self.works)
                if remaining <= 0:
                    return
                page = self._bibliographic_call(
                    "search", role=row["role"], query=request["query"],
                    cursor=row["next_cursor"], plan_ref=row.get("plan_ref"),
                    admission=admission, result_limit=min(self.bounds["results_per_query"], remaining))
                progressed |= acquisition_succeeded(page)
            if not progressed:
                break

    def _full_texts(self):
        for gap in self.gaps:
            if (gap.get("kind") == "full_text_failure"
                    and (gap.get("outcome") == "rate_limited" or _has_http_429(gap))):
                self._stop_full_text_rate_limit(gap)
        self.full_text_attempted.update(
            gap["work_id"] for gap in self.gaps
            if gap.get("kind") == "full_text_failure"
            and gap.get("outcome") in _SOURCE_ACCESS_UNAVAILABLE
            and gap.get("work_id") in self.works)
        self.full_text_attempted.update(
            source["work_id"] for source in self.source_docs.values()
            if source.get("representation") == "full_text" and authoritative_source(source))
        if "full_text" not in self.bindings:
            return
        routes = []
        for configured in self.score["full_text_sources"]:
            route = deepcopy(configured)
            wid = self.aliases.get(route["work_id"], route["work_id"])
            locations = self.works.get(wid, {}).get("locations", [])
            candidates = full_text_url_candidates(locations)
            registered = next((item for item in candidates if item["url"] == route["url"]), None)
            open_pdf = next((item for item in candidates
                             if item["is_oa"] and item["kind"] == "pdf"), None)
            # Exact routes preserve a caller-selected locator; auto routes
            # resolve a registered landing page to a registered OA PDF.
            if (route.get("route_policy", "exact") == "auto" and registered
                    and registered["kind"] == "landing_page" and open_pdf):
                route["url"] = open_pdf["url"]
                route["source_kind"] = "pdf"
            elif registered and registered["kind"] == "pdf":
                route["source_kind"] = "pdf"
            else:
                route["source_kind"] = "auto"
            fallback = preferred_oa_pdf_url(locations, exclude_urls=(route["url"],))
            route["fallback_urls"] = [fallback] if fallback and fallback != route["url"] else []
            routes.append((route, False))
        selected_ids = self._analysis_selection()
        if self._tree_admitted_reads is not None:
            selected_ids &= self._tree_admitted_reads
        analysis_limit = self.bounds.get("max_analyzed_works", self.bounds["max_works"])
        auto_full_text_limit = (len(selected_ids) if self.exploration_tree is not None else min(
            self.bounds["max_full_texts"], max(1, int(analysis_limit) // 2)))
        # A free-topic run may discover Crossref records after its initial
        # route list was authored.  Use the verified catalog URLs as additional
        # candidates so a stale seed route cannot cap the entire full-text
        # campaign.  The route remains a normal bounded source capture; only
        # its provenance is marked locally as auto-discovered.
        if selected_ids:
            known = {route.get("work_id") for route, _ in routes if isinstance(route, dict)}
            ranked = sorted(
                (wid for wid in selected_ids if wid in self.works),
                key=lambda wid: (self._work_relevance(wid), wid), reverse=True)
            if self._tree_read_order is not None:
                priority = {wid: index for index, wid in enumerate(self._tree_read_order)}
                ranked.sort(key=lambda wid: priority.get(wid, len(priority)))
            for wid in ranked:
                work = self.works[wid]
                if not isinstance(wid, str) or wid in known:
                    continue
                candidates = full_text_url_candidates(work.get("locations"))
                preferred = candidates[0] if candidates else None
                url = preferred["url"] if preferred else None
                if self.bibliography_mode == "crossref":
                    url = url or work.get("source_url")
                if (not isinstance(url, str) or not url.strip()) and self.bibliography_mode == "crossref":
                    doi = work.get("doi")
                    url = "https://doi.org/" + doi if isinstance(doi, str) and doi.strip() else None
                if not isinstance(url, str) or not url.strip():
                    continue
                pdf_fallback = preferred_oa_pdf_url(
                    work.get("locations"), exclude_urls=(url,))
                routes.append(({
                    "work_id": wid,
                    "title": work.get("title") or wid,
                    "url": url,
                    "route_policy": "auto",
                    "source_kind": "pdf" if preferred and preferred["kind"] == "pdf" else "auto",
                    "fallback_urls": [pdf_fallback] if pdf_fallback and pdf_fallback != url else [],
                    "section_markers": [],
                }, True))
                known.add(wid)
                if sum(1 for _, auto in routes if auto) >= auto_full_text_limit:
                    break
        for index, (route, auto_discovered) in enumerate(routes):
            wid = self.aliases.get(route["work_id"], route["work_id"])
            # Automatic routes are only a second-pass deep-analysis surface;
            # never spend the full-text budget on the long-tailed catalog.
            if (auto_discovered or self.exploration_tree is not None) and wid not in selected_ids:
                continue
            if wid not in self.works or wid in self.full_text_attempted:
                continue
            if self.exploration_tree is None and len(self.full_text_attempted) >= self.bounds["max_full_texts"]:
                self.gaps.append({"kind": "full_text_limit", "work_id": wid})
                break
            self.full_text_attempted.add(wid)
            candidate_sources = [(route["url"], route.get("source_kind", "auto"))]
            candidate_sources.extend((url, "pdf") for url in route.get("fallback_urls", []))
            candidate_sources = list(dict.fromkeys(candidate_sources))[:2]
            verified_source = None
            unverified_source = None
            for source_index, (source_url, source_kind) in enumerate(candidate_sources):
                attempt_id = f"command/source-attempts/full-text/{wid}-{source_index + 1}"
                self._record(attempt_id, "note", {
                    "work_id": wid, "url": source_url, "status": "reserved",
                    "source_kind": source_kind,
                    "route": "primary" if source_index == 0 else "open_access_pdf_fallback",
                    "scope": "One bounded source attempt; access denials are retained without bypass.",
                }, "command.controller", subjects=[self.work_records[wid]["artifact_ref"]])
                try:
                    self._wait_provider("full_text")
                    result, execution = self.operations.run(self.bindings["full_text"],
                        {"url": source_url, "max_length": self.bounds["max_text_chars"],
                         "source_kind": source_kind}, self._call,
                        operator="research.full-text-reader")
                except ProviderRateLimitError:
                    raise
                except Exception as exc:
                    self._ensure_active()
                    detail = self._failure_detail(self.score["full_text"]["id"])
                    if detail.get("outcome") in {"rate_limited", 429} or _has_http_429(detail):
                        failure = {"kind": "full_text_failure", "work_id": wid,
                                   "source_url": source_url, "reason": str(exc), **detail}
                        self._record(attempt_id, "note", {"work_id": wid, "url": source_url,
                            "status": "rate_limited", "failure": failure}, "command.controller",
                            subjects=[self.work_records[wid]["artifact_ref"], detail["execution_ref"]])
                        self._stop_full_text_rate_limit(failure)
                    self.gaps.append({"kind": "full_text_failure", "work_id": wid,
                                      "source_url": source_url, "reason": str(exc)})
                    self.bindings.pop("full_text", None)
                    try:
                        capability_id = self.score["full_text"]["id"]
                        state = self.operations.ensure_ready(
                            capability_id, self._call,
                            operator="research.full-text-reader.recovery",
                            verifier="operations.verifier.full-text-recovery",
                            purpose="Recover the text-fetch capability after a bounded source attempt",
                        )
                        if state.get("state") != "ready":
                            self._stop_provider_rate_limit(
                                {"kind": "full_text_readiness_failure", "capability_id": capability_id,
                                 "work_id": wid, "reason": state.get("reason"),
                                 **self._failure_detail(capability_id)}, provider="full_text")
                            break
                        self.bindings["full_text"] = state["binding"]
                        self.operations.idle(capability_id)
                    except ProviderRateLimitError:
                        raise
                    except Exception as recovery_exc:
                        self._ensure_active()
                        self._stop_provider_rate_limit(
                            {"kind": "full_text_recovery_failure", "work_id": wid,
                             "reason": str(recovery_exc), **self._failure_detail(capability_id)}, provider="full_text")
                        self.gaps.append({"kind": "full_text_recovery_failure", "work_id": wid,
                                          "reason": str(recovery_exc)})
                        break
                    continue
                if result.get("outcome") == "rate_limited" or _has_http_429(result):
                    failure = self._full_text_failure(wid, source_url, result, execution)
                    self._record(attempt_id, "note", {"work_id": wid, "url": source_url,
                        "status": "rate_limited", "failure": failure}, "command.controller",
                        subjects=[self.work_records[wid]["artifact_ref"], execution])
                    self._stop_full_text_rate_limit(failure)
                if result.get("outcome") != "ok":
                    failure = self._full_text_failure(wid, source_url, result, execution)
                    self.gaps.append(failure)
                    if failure["outcome"] in _SOURCE_ACCESS_UNAVAILABLE | {"rate_limited"}:
                        self._record(attempt_id, "note", {
                            "work_id": wid, "url": source_url,
                            "status": "rate_limited" if failure["outcome"] == "rate_limited" else "access_unavailable",
                            "failure": failure,
                        }, "command.controller", subjects=[self.work_records[wid]["artifact_ref"], execution])
                    if failure["outcome"] == "rate_limited":
                        self._stop_full_text_rate_limit(failure)
                    if failure["outcome"] in _SOURCE_ACCESS_UNAVAILABLE:
                        break
                    continue
                text = result["text"]
                title_match = (normalized(self.works[wid]["title"]) == normalized(route["title"])
                               and normalized(route["title"]) in normalized(text))
                section_markers = ([marker for marker in BODY_SECTION_MARKERS
                                    if _has_section_heading(text, marker)]
                                   if auto_discovered else route["section_markers"])
                body_markers = [marker for marker in section_markers
                                if normalized(marker) not in {"abstract", "summary"}]
                sections_match = bool(body_markers) and all(
                    _has_section_heading(text, marker) for marker in section_markers)
                complete = not any(result["metadata"].get(key)
                                   for key in ("capture_truncated", "capture_incomplete"))
                source_body = {"work_id": wid,
                    "representation": "full_text" if title_match and sections_match and complete else "unverified_text",
                    "text": text, "url": source_url, "execution_ref": execution,
                    "identity_verified": bool(title_match and sections_match and complete),
                    "identity_checks": {"title_match": title_match,
                                        "section_markers": section_markers if sections_match else []}}
                if source_body["identity_verified"]:
                    verified_source = source_body
                    break
                self.gaps.append({"kind": "full_text_identity_or_scope", "work_id": wid,
                                  "source_url": source_url})
                unverified_source = source_body
            body = verified_source or unverified_source
            if body is None:
                continue
            record = self._publish(f"kb/full-text/{wid}", "source_capture", body, "methods.source-verifier",
                                   subjects=[body["execution_ref"], self.work_records[wid]["artifact_ref"]])
            self.source_records[f"full_text/{wid}"] = record
            self.source_docs[record["artifact_ref"]] = body
            self._update_register()

    @staticmethod
    def _full_text_failure(wid, url, result, execution):
        metadata = result.get("metadata", {})
        limited = result.get("outcome") == "rate_limited" or _has_http_429(result)
        return {
            "kind": "full_text_failure", "work_id": wid, "source_url": url,
            "outcome": "rate_limited" if limited else result.get("outcome"),
            "reason": result.get("error") or "Source did not yield complete extracted text",
            "execution_ref": execution,
            "metadata": (deepcopy(metadata) if limited else
                         {"http_status": metadata.get("http_status"), "rate_limit": deepcopy(metadata.get("rate_limit"))}),
            **({"rate_limit": deepcopy(result["rate_limit"])} if "rate_limit" in result else {}),
            **{key: deepcopy(result[key]) for key in _HTTP_STATUS_FIELDS if key in result},
        }

    def _restore_source_access_failures(self):
        """Recover terminal access results omitted by an interrupted aggregate checkpoint."""
        for attempt in self._heads("command/source-attempts/full-text/"):
            reservation = self._body(attempt)
            if reservation.get("status") != "reserved" or attempt.get("author") != "command.controller":
                continue
            wid, url = reservation.get("work_id"), reservation.get("url")
            if wid not in self.works or not isinstance(url, str):
                continue
            work_subjects = [item["ref"] for item in attempt.get("inputs", [])
                             if item.get("purpose") == "subject"]
            if len(work_subjects) != 1 or self._body(self.store.get(work_subjects[0])).get("work_id") != wid:
                raise ValidationError("full-text source reservation has no exact owned work subject")
            score_ref = attempt.get("score_ref")
            if not score_ref:
                continue
            score = self._body(self.store.get(score_ref)).get("survey", {})
            capability = score.get("full_text")
            if not isinstance(capability, dict) or not isinstance(capability.get("id"), str):
                continue
            matched = []
            for execution in self._heads(f"command/executions/ops-work-{capability['id']}-"):
                if (execution.get("author") != "research.full-text-reader"
                        or execution.get("score_ref") != score_ref
                        or execution["created_at"] < attempt["created_at"]):
                    continue
                contexts = [item["ref"] for item in execution.get("inputs", [])
                            if item.get("purpose") == "subject"]
                if len(contexts) != 1:
                    continue
                context = self.store.get(contexts[0])
                if (context.get("author") != execution["author"] or context.get("score_ref") != score_ref
                        or not context["artifact_id"].startswith(f"command/contexts/ops-work-{capability['id']}-")
                        or not attempt["created_at"] <= context["created_at"] <= execution["created_at"]):
                    continue
                arguments = self._body(context)
                result = self._body(execution)
                if (arguments.get("url") != url or result.get("source_url") != url
                        or arguments.get("source_kind", "auto") != reservation.get("source_kind", "auto")):
                    continue
                if result.get("outcome") in _SOURCE_ACCESS_UNAVAILABLE | {"rate_limited"} or _has_http_429(result):
                    matched.append(self._full_text_failure(wid, url, result, execution["artifact_ref"]))
            if len(matched) > 1:
                raise ValidationError("full-text source reservation has ambiguous terminal executions")
            if matched:
                previous = next((gap for gap in self.gaps if gap.get("kind") == "full_text_failure"
                                 and gap.get("work_id") == wid and gap.get("source_url") == url), None)
                if previous is None:
                    self.gaps.append(matched[0])
                elif previous.get("outcome") != matched[0]["outcome"] and matched[0]["outcome"] != "rate_limited":
                    raise ValidationError("full-text access failure conflicts with its immutable execution")
                else:
                    previous.update(matched[0])

    def _stop_provider_rate_limit(self, failure, *, provider):
        if failure.get("outcome") not in {"rate_limited", 429} and not _has_http_429(failure):
            return
        if failure not in self.gaps:
            self.gaps.append(deepcopy(failure))
        raise ProviderRateLimitError(
            "Provider rate limit requires provider recovery before further dispatch",
            provider=provider, details=failure,
        )

    def _stop_full_text_rate_limit(self, failure):
        self._stop_provider_rate_limit(failure, provider="full_text")

    def _revalidate_retained_full_texts(self):
        """Replay source authority before restoring downstream analysis credit."""
        configured = {self.aliases.get(route["work_id"], route["work_id"]): route
                      for route in self.score["full_text_sources"]}
        for ref, previous in list(self.source_docs.items()):
            wid = previous["work_id"]
            representation = previous.get("representation")
            if wid not in self.work_records or representation not in {"full_text", "unverified_text"}:
                continue
            execution = previous.get("execution_ref")
            if representation == "full_text":
                try:
                    self.gate._full_text(previous, self.works[wid], {"dependency_refs": [execution]})
                    continue
                except (KeyError, TypeError, ValueError, ValidationError) as exc:
                    candidate = {**previous, "representation": "unverified_text", "identity_verified": False}
                    self.gaps.append({"kind": "full_text_identity_or_scope", "work_id": wid,
                                      "source_url": previous.get("url"), "reason": str(exc)})
            else:
                if wid in configured:
                    continue
                text = previous.get("text", "")
                markers = [marker for marker in BODY_SECTION_MARKERS if _has_section_heading(text, marker)]
                candidate = {**previous, "representation": "full_text", "identity_verified": True,
                             "identity_checks": {"title_match": True, "section_markers": markers}}
                try:
                    self.gate._full_text(candidate, self.works[wid], {"dependency_refs": [execution]})
                except (KeyError, TypeError, ValueError, ValidationError):
                    continue
            record = self._publish(f"kb/full-text/{wid}", "source_capture", candidate,
                                   "methods.source-verifier", subjects=[ref,
                                       *([execution] if isinstance(execution, str) else []),
                                       self.work_records[wid]["artifact_ref"]])
            del self.source_docs[ref]
            self.source_docs[record["artifact_ref"]] = candidate
            self.source_records[f"full_text/{wid}"] = record
            self._update_register()

    def _reconcile_retained_identity(self, wid, work, record):
        """Rebuild changed provider observations from their pinned DOI lookup.

        A changed work card or reconciliation rule changes the observation
        basis. The recorded Crossref response remains reusable for the same
        DOI without another provider call.
        """
        previous = self._body(record)
        if (previous.get("status") != "conflicted"
                and self.work_records.get(wid, {}).get("artifact_ref") in previous.get("observation_refs", [])):
            return None
        lookup_ref = previous.get("lookup_execution_ref")
        work_record = self.work_records.get(wid)
        if not isinstance(lookup_ref, str) or not isinstance(work_record, dict):
            return None
        try:
            _, _, result, params = self.gate._recorded_execution(
                lookup_ref, "research.identity-checker", operation="crossref",
                task_kinds={"retrieval"},
            )
            if (normalize_doi(params.get("query")) != normalize_doi(work.get("doi"))
                    or result.get("metadata", {}).get("match_mode") != "exact_doi"):
                return None
            checks, _ = get_adapter("crossref").inspect_result(
                {"adapter": "crossref", "client": params.get("client", {})},
                result, params, representative=False,
            )
            if not all(check.get("outcome") == "passed" for check in checks):
                return None
            return reconcile_result(
                work, work_record["artifact_ref"], result, lookup_ref,
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            return None

    def _recover_identity_lookup(self, arguments):
        if not self.resume_session:
            return None
        capability = self.score.get("identity")
        if not capability:
            return None
        for record in reversed(self._heads("command/executions/ops-work-" + capability["id"] + "-")):
            try:
                _, _, result, params = self.gate._recorded_execution(
                    record["artifact_ref"], "research.identity-checker", operation="crossref", task_kinds={"retrieval"})
                if ({key: value for key, value in params.items() if key != "client"} != arguments
                        or params.get("client") != capability["client"]):
                    continue
                checks, _ = get_adapter("crossref").inspect_result(
                    {"adapter": "crossref", "client": params["client"]}, result, params, representative=False)
                if checks and all(check["outcome"] == "passed" for check in checks):
                    return result, record["artifact_ref"]
            except (KeyError, TypeError, ValueError, ValidationError, StateError):
                continue
        return None

    def _reconcile_identity_rate_limits(self, arguments):
        capability = self.score.get("identity")
        if not capability:
            return
        reservations = sorted((record for record in self._heads("command/api-calls/")
            if self._body(record).get("capability") == "identity"
            and self._body(record).get("request") == arguments), key=lambda record: record["created_at"])
        if not reservations:
            return
        executions = []
        for record in self._heads("command/executions/ops-work-" + capability["id"] + "-"):
            try:
                _, context, result, params = self.gate._recorded_execution(
                    record["artifact_ref"], "research.identity-checker", operation="crossref",
                    task_kinds={"retrieval"}, allow_blocked_retrieval=True)
                if ({key: value for key, value in params.items() if key != "client"} == arguments
                        and params.get("client") == capability["client"]
                        and (result.get("outcome") in {"rate_limited", 429} or _has_http_429(result))):
                    executions.append((record, context, result))
            except (KeyError, TypeError, ValueError, ValidationError, StateError):
                continue
        for index, reservation in enumerate(reservations):
            number = self._body(reservation)["number"]
            if self.store.head(f"command/identity-rate-limits/{number}"):
                continue
            end = reservations[index + 1]["created_at"] if index + 1 < len(reservations) else None
            matching = [(execution, result) for execution, context, result in executions
                if reservation.get("author") == "research.identity-checker"
                and reservation.get("score_ref") == context.get("score_ref") == execution.get("score_ref")
                and reservation["created_at"] <= context["created_at"] <= execution["created_at"]
                and (end is None or execution["created_at"] < end)]
            if len(matching) > 1:
                raise ValidationError("identity reservation has ambiguous terminal executions")
            if matching:
                execution, result = matching[0]
                self._record(f"command/identity-rate-limits/{number}", "note", {
                    "number": number, "request": arguments, "execution_ref": execution["artifact_ref"],
                    "outcome": "rate_limited"}, "research.identity-checker",
                    subjects=[reservation["artifact_ref"], execution["artifact_ref"]])

    def _stop_identity_rate_limit(self, failure, arguments):
        if failure.get("outcome") in {"rate_limited", 429} or _has_http_429(failure):
            self._reconcile_identity_rate_limits(arguments)
        self._stop_provider_rate_limit(failure, provider="crossref")

    def _reconcile_identities(self):
        if "identity" not in self.bindings:
            retained = True
        else:
            retained = False
        # Identity reconciliation is a verification input for substantive
        # map/review work, not a requirement for every catalog hit.  The
        # catalog may contain up to ``max_works`` records while the declared
        # deep-analysis budget is intentionally much smaller.  Running
        # Crossref for the entire catalog made the survey spend its first pass
        # on hundreds of metadata lookups before any scientific assessment.
        identity_scope = self._analysis_selection()
        if self._tree_admitted_reads is not None:
            identity_scope &= self._tree_admitted_reads
        for wid, work in list(self.works.items()):
            if wid not in identity_scope:
                continue
            if not work.get("doi"):
                continue
            existing = self.identity_records.get(wid)
            if existing is not None:
                if (self._body(existing).get("status") == "conflicted"
                        or self.work_records[wid]["artifact_ref"] not in self._body(existing).get("observation_refs", [])):
                    revised = self._reconcile_retained_identity(wid, work, existing)
                    if revised is not None and revised != self._body(existing):
                        prior_ref = existing["artifact_ref"]
                        record = self._record(
                            f"kb/identities/{wid}", "reference_card", revised,
                            "research.identity-checker",
                            subjects=[self.work_records[wid]["artifact_ref"],
                                      revised["lookup_execution_ref"], prior_ref],
                        )
                        self.identity_records[wid] = record
                        if revised["status"] not in {"conflicted", "insufficient_evidence"}:
                            self.gaps = [gap for gap in self.gaps
                                         if not (gap.get("work_id") == wid
                                                 and gap.get("identity_ref") == prior_ref
                                                 and isinstance(gap.get("kind"), str)
                                                 and gap["kind"].startswith(
                                                     "bibliographic_identity_"))]
                        elif not any(gap.get("identity_ref") == record["artifact_ref"]
                                     for gap in self.gaps):
                            self.gaps.append({
                                "kind": "bibliographic_identity_" + revised["status"],
                                "work_id": wid, "identity_ref": record["artifact_ref"],
                            })
                continue
            if retained or wid in self.identity_records:
                continue
            arguments = {"query": work["doi"], "limit": 3}
            recovered = self._recover_identity_lookup(arguments)
            if recovered is not None:
                result, execution = recovered
            else:
                self._reconcile_identity_rate_limits(arguments)
                failed = {self._body(record)["number"] for record in self._heads("command/identity-rate-limits/")}
                uncertain = [self._body(record) for record in self._heads("command/api-calls/")
                             if self._body(record).get("capability") == "identity"
                             and self._body(record).get("request") == arguments
                             and self._body(record)["number"] not in failed]
                if uncertain:
                    raise StateError("identity lookup has an unresolved charged reservation; redispatch prohibited")
                if self.api_calls >= self.bounds["max_api_calls"]:
                    self.gaps.append({"kind": "identity_call_limit", "work_id": wid})
                    break
                self._wait_provider("identity")
                self._reserve_api_call("identity", arguments, "research.identity-checker")
                try:
                    result, execution = self.operations.run(
                        self.bindings["identity"], arguments, self._call,
                        operator="research.identity-checker")
                except ProviderRateLimitError as exc:
                    self._stop_identity_rate_limit(exc.details, arguments)
                    raise
                except Exception as exc:
                    self._ensure_active()
                    self._stop_identity_rate_limit(
                        {"kind": "identity_lookup_failure", "work_id": wid, "reason": str(exc),
                         **self._failure_detail(self.score["identity"]["id"])}, arguments)
                    self.gaps.append({"kind": "identity_lookup_failure", "work_id": wid, "reason": str(exc)})
                    self.bindings.pop("identity", None)
                    break
            if isinstance(result, dict):
                self._stop_identity_rate_limit(
                    {**result, "kind": "identity_lookup_failure", "work_id": wid,
                     "execution_ref": execution}, arguments)
            identity = reconcile_result(work, self.work_records[wid]["artifact_ref"], result, execution)
            record = self._record(f"kb/identities/{wid}", "reference_card", identity,
                                  "research.identity-checker",
                                  subjects=[self.work_records[wid]["artifact_ref"], execution])
            self.identity_records[wid] = record
            status = identity["status"]
            if status in {"conflicted", "insufficient_evidence"}:
                self.gaps.append({"kind": "bibliographic_identity_" + status, "work_id": wid,
                                  "identity_ref": record["artifact_ref"]})
        self._update_register()

    def _analysis_selection(self):
        """Choose the bounded set that receives substantive analysis.

        Discovery and counter-search retain their full catalog accounting, but
        identity reconciliation, source-grounded mapping, and model review
        must honor the explicit deep-analysis budget. Existing substantive
        entries remain in scope on resume so a narrower config cannot silently
        invalidate accepted work.
        """
        analysis_limit = self.bounds.get("max_analyzed_works", self.bounds["max_works"])
        existing = {wid for wid, record in self.analysis_records.items()
                    if any(self._body(record).get(field, {}).get("text") is not None
                           for field in MAP_FIELDS)}
        if self.exploration_tree is not None:
            selected = {self.aliases.get(wid, wid) for action in self.exploration_tree["nodes"]
                        if action["kind"] == "acquisition"
                        for wid in action.get("selected_work_ids", [])}
            if self._tree_admitted_reads is not None:
                selected = set(self._tree_admitted_reads)
            if self._countersearch_active:
                selected.update(self.aliases.get(wid, wid) for row in self.search_log
                                if row.get("role") == "methods.novelty-challenger"
                                for wid in row.get("returned_work_ids", []))
            selected.update(item["work_id"] for item in self.review_obligations)
            return (existing | selected) & set(self.work_records)
        def priority(wid):
            work = self.works[wid]
            return (
                *self._work_relevance(wid),
                any(source["work_id"] == wid and source["representation"] == "full_text"
                    for source in self.source_docs.values()),
            )

        # Reserve part of the declared analysis budget for the independent
        # counter-search even when its terminology differs from the question.
        challenge_ids = {
            self.aliases.get(wid, wid)
            for row in self.search_log
            if row.get("role") == "methods.novelty-challenger"
            for wid in row.get("returned_work_ids", [])
        } & set(self.work_records)
        reserve = self.bounds.get("challenge_reserve", 0)
        # Keep provider/discovery order as the final tie-breaker.  A work ID is
        # an opaque provider identifier, not a scientific relevance signal;
        # sorting equal-scoring records by it used to pick arbitrary seeds and
        # made fixture/live runs drift between unrelated citation branches.
        discovery_order = {wid: index for index, wid in enumerate(self.works)}
        ranked = lambda ids: sorted(
            ids, key=lambda wid: (priority(wid), -discovery_order.get(wid, 0)), reverse=True)
        discovery = ranked(set(self.work_records) - existing - challenge_ids)
        challenges = ranked(challenge_ids - existing)
        selected = existing | set(
            discovery[:max(0, analysis_limit - reserve - len(existing))])
        selected.update(challenges[:max(0, analysis_limit - len(selected))])
        return selected

    def _survey_search_terms(self):
        values = [self.score.get("question", ""), *self.score.get("seed_queries", [])]
        values.extend(order[field] for order in getattr(self, "work_orders", [])
                      for field in ("objective", "evidence_needed"))
        return _content_tokens(" ".join(str(value) for value in values))

    def _survey_search_phrases(self):
        """Return declared two-to-four-token anchors for relevance ranking."""
        phrases = []
        values = [self.score.get("question", ""), *self.score.get("seed_queries", [])]
        values.extend(order[field] for order in getattr(self, "work_orders", [])
                      for field in ("objective", "evidence_needed"))
        for value in values:
            tokens = [token for token in normalized(str(value)).split()
                      if len(token) >= 3 and token not in _SEARCH_STOP_WORDS
                      and not token.isdigit()]
            for width in range(min(4, len(tokens)), 1, -1):
                for start in range(0, len(tokens) - width + 1):
                    phrase = " ".join(tokens[start:start + width])
                    if phrase not in phrases:
                        phrases.append(phrase)
        return phrases[:48]

    def _work_relevance(self, wid):
        work = self.works[wid]
        title = normalized(work.get("title", ""))
        text = normalized(" ".join(
            str(work.get(key, "")) for key in ("title", "abstract")))
        title_tokens = set(title.split())
        text_tokens = set(text.split())
        terms = self._survey_search_terms()
        phrase_hits = sum(phrase in text for phrase in self._survey_search_phrases())
        title_hits = len(terms & title_tokens)
        text_hits = len(terms & text_tokens)
        return (phrase_hits, title_hits, text_hits, bool(work.get("abstract")))

    def _update_register(self):
        body = {"work_refs": [r["artifact_ref"] for r in self.work_records.values()],
                "source_refs": list(self.source_docs), "query_refs": list(self.query_refs),
                "identity_refs": [r["artifact_ref"] for r in self.identity_records.values()], "aliases": self.aliases}
        self.register_ref = self._record("kb/work-register", "note", body, "research.cataloger")["artifact_ref"]

    def _source_availability(self, wid):
        sources = [(ref, source) for ref, source in self.source_docs.items()
                   if source["work_id"] == wid and authoritative_source(source)]
        full_text = [ref for ref, source in sources if source["representation"] == "full_text"]
        abstracts = [ref for ref, source in sources if source["representation"] == "abstract"]
        failures = [gap for gap in self.gaps
                    if gap.get("kind") == "full_text_failure" and gap.get("work_id") == wid]
        rate_limited = any(gap.get("outcome") == "rate_limited" or _has_http_429(gap)
                           for gap in failures)
        unavailable = any(gap.get("outcome") in _SOURCE_ACCESS_UNAVAILABLE for gap in failures)
        return {
            "work_id": wid,
            "evidence_scope": "full_text" if full_text else "abstract" if abstracts else "unavailable",
            "verified_full_text_refs": full_text, "abstract_refs": abstracts,
            "full_text_access": ("rate_limited" if rate_limited else "available" if full_text
                                 else "unavailable" if unavailable else "not_captured"),
            "full_text_failures": [{key: deepcopy(gap[key]) for key in (
                "outcome", "source_url", "execution_ref", "reason") if key in gap}
                for gap in failures],
        }

    def _source_context(self):
        return [{"source_ref": ref, "work_id": value["work_id"], "representation": value["representation"],
                 "source_availability": self._source_availability(value["work_id"]),
                 "identity_verified": value.get("identity_verified", False),
                 "identity_checks": deepcopy(value.get("identity_checks")), "url": value.get("url"),
                 "text": value["text"][:self.bounds["context_chars"]], "available_chars": len(value["text"]),
                 "window": {"start": 0, "end": min(len(value["text"]), self.bounds["context_chars"])}}
                for ref, value in self.source_docs.items()]

    @staticmethod
    def _project_source_window(source, limit):
        """Return a source window with its evidence boundary moved to the projection."""
        projected = deepcopy(source)
        text = source.get("text", "") if isinstance(source, dict) else ""
        limit = max(0, int(limit))
        projected["text"] = text[:limit]
        start = source.get("window", {}).get("start", 0)
        projected["window"] = {"start": start, "end": start + len(projected["text"])}
        return projected

    def _map_input_limit(self, role, *, include_route=False):
        """Return the strictest input limit any route for ``role`` permits.

        A map job is serialized before the execution runtime selects a
        provider lane.  Route selection can therefore reject an otherwise
        valid assignment when one of the configured fallbacks has a smaller
        context budget.  Project against the strictest resolved route so
        capacity rotation cannot turn prompt size into a late dispatch
        failure.
        """
        base = self.model_config
        if not isinstance(base, dict):
            return None
        candidates = []

        def add(overrides):
            effective = dict(base)
            if isinstance(overrides, dict):
                effective.update(overrides)
            if is_local_qwen_route(effective):
                return
            window = effective.get("context_window_tokens")
            input_limit = effective.get("max_input_tokens")
            output_limit = effective.get("max_output_tokens")
            allowed = input_limit
            if isinstance(window, int) and isinstance(output_limit, int):
                window_limit = window - output_limit
                allowed = window_limit if allowed is None else min(allowed, window_limit)
            if isinstance(allowed, int) and allowed > 0:
                candidates.append((allowed, effective))

        role_models = base.get("role_models", {})
        selected = role_config_for(role_models, role)
        fallbacks = base.get("role_model_fallbacks", {})
        role_fallbacks = role_config_for(fallbacks, role, [])
        routes = role_routes_for(base, role)

        # An explicit role selection is the admission contract for that role.
        # The global model is only a candidate when no role-specific model,
        # fallback, or route exists; otherwise it would silently force every
        # high-context role back down to the base model's smaller window.
        if selected is None and not role_fallbacks and not routes:
            add(None)
        if selected is not None:
            add(selected)
        if isinstance(fallbacks, dict):
            for fallback in role_fallbacks:
                add(fallback)
        for route in routes:
            if not isinstance(route, dict):
                continue
            effective = dict(selected) if isinstance(selected, dict) else {}
            effective.update({key: value for key, value in route.items()
                              if key not in {"id", "pool"}})
            add(effective)
        if not candidates:
            return None
        strictest = min(candidates, key=lambda item: item[0])
        return strictest if include_route else strictest[0]

    @staticmethod
    def _project_map_catalog(works, *, visible_ids, target_ids, max_items):
        """Keep catalog metadata for displayed sources and scoped targets."""
        required_ids = set(visible_ids) | set(target_ids)
        required = [work for work in works if work.get("id") in required_ids]
        optional = [work for work in works if work.get("id") not in required_ids]
        if max_items is None or len(required) >= max_items:
            return required
        return [*required, *optional[:max_items - len(required)]]

    def _project_map_sources(self, sources, *, required_ids,
                             comparison_chars, comparison_count):
        """Preserve required captures while reducing optional comparison context."""
        required = [deepcopy(source) for source in sources if source.get("work_id") in required_ids]
        comparison_sources = [source for source in sources if source.get("work_id") not in required_ids]
        selected = comparison_sources[:max(0, int(comparison_count))]
        if selected and comparison_chars > 0:
            each = max(1, int(comparison_chars) // len(selected))
            projected_comparisons = [
                self._project_source_window(source, each)
                for source in selected if source.get("text", "")[:each]
            ]
        else:
            projected_comparisons = []
        return [*required, *projected_comparisons]

    def _fit_map_assignment(self, assignment, *, owner_id):
        """Fit a map assignment to every possible provider route.

        The normal map projection bounds source text, but the catalog and
        scoped repair state also grow with a survey.  This second, exact
        admission pass preserves the owner first, keeps relevant comparison
        records, and progressively reduces optional context until the same
        conservative estimator used by the execution runtime fits.  If the
        immutable contract itself cannot fit, fail closed before a provider
        request is created.
        """
        limit = self._map_input_limit("research.literature-mapper")
        if limit is None:
            return assignment

        def fits(candidate):
            prompt = json.dumps(candidate, ensure_ascii=False)
            return estimate_input_tokens(SYSTEM, prompt) <= limit

        if fits(assignment):
            return assignment

        sources = assignment.get("sources", [])
        works = assignment.get("works", [])
        target_ids = {
            relation.get("target") for relation in assignment.get(
                "previous_affected_relationships", [])
            if isinstance(relation, dict) and isinstance(relation.get("target"), str)
        }
        target_ids.update(
            target for target in assignment.get("editable_relationship_targets", [])
            if isinstance(target, str)
        )
        required_ids = {owner_id, *target_ids, *self._review_comparison_ids(owner_id)}
        optional_sources = [source for source in sources if source.get("work_id") not in required_ids]
        comparison_count = len(optional_sources)
        comparison_chars = sum(len(source.get("text", "")) for source in optional_sources)
        max_items = len(works)
        while True:
            projected_sources = self._project_map_sources(
                sources, required_ids=required_ids,
                comparison_chars=comparison_chars, comparison_count=comparison_count)
            visible_ids = {source["work_id"] for source in projected_sources}
            projected_works = self._project_map_catalog(
                works, visible_ids=visible_ids | required_ids, target_ids=target_ids,
                max_items=max_items)
            candidate = {**assignment, "works": projected_works, "sources": projected_sources}
            if fits(candidate):
                return candidate
            if comparison_count == comparison_chars == max_items == 0:
                break
            comparison_count //= 2
            comparison_chars //= 2
            max_items //= 2

        allowed, route = self._map_input_limit("research.literature-mapper", include_route=True)
        raise ModelContextBudgetError(
            "source-complete literature-map assignment exceeds the strictest configured route",
            model=route.get("model"),
            estimated_input_tokens=estimate_input_tokens(SYSTEM, json.dumps(candidate, ensure_ascii=False)),
            allowed_input_tokens=allowed, context_window_tokens=route.get("context_window_tokens"),
            max_input_tokens=route.get("max_input_tokens"), max_output_tokens=route.get("max_output_tokens"))

    def _map_sources(self, wid, *, old_relationships=None, review_feedback=None):
        """Expose complete assigned captures and optional comparison excerpts.

        A map worker owns one work, while comparison abstracts are only needed
        to justify an optional outgoing relationship. Sending every captured
        abstract at the full survey context limit makes the same assignment
        exceed a provider window as the corpus grows. Owner and existing
        relationship-target captures remain complete; optional comparisons
        share a separate context allocation. Exact route admission may remove
        optional context but cannot hide part of a required capture.
        """
        all_sources = self._assessment_source_context()

        target_ids = set()
        for relation in old_relationships or []:
            if isinstance(relation, dict) and isinstance(relation.get("target"), str):
                target_ids.add(self.aliases.get(relation["target"], relation["target"]))
        if isinstance(review_feedback, dict):
            for target in review_feedback.get("relationship_targets", []):
                if isinstance(target, str):
                    target_ids.add(self.aliases.get(target, target))
        referenced_ids = {
            self.aliases.get(reference, reference)
            for reference in self.works.get(wid, {}).get("referenced_works", [])
            if isinstance(reference, str)
        }
        required_ids = {wid, *target_ids, *self._review_comparison_ids(wid)}
        required_sources = [source for source in all_sources if source["work_id"] in required_ids]
        comparison_sources = [source for source in all_sources
                              if source["work_id"] not in required_ids and source["representation"] == "abstract"]
        comparison_ids = self._analysis_selection() | target_ids | referenced_ids
        comparison_sources = [source for source in comparison_sources
                              if source["work_id"] in comparison_ids]
        order = {source["source_ref"]: index for index, source in enumerate(comparison_sources)}
        comparison_sources.sort(key=lambda source: (
            0 if source["work_id"] in target_ids else
            1 if source["work_id"] in referenced_ids else 2,
            order[source["source_ref"]],
        ))

        context_chars = self.bounds["context_chars"]
        # This budget is independent of the number of captured works. Dividing
        # it across the comparison set keeps map prompts bounded while still
        # exposing every candidate work to the model for optional linking.
        comparison_budget = min(self.bounds["max_text_chars"], context_chars * 2)
        comparison_limit = (comparison_budget // len(comparison_sources)
                            if comparison_sources else 0)
        projected_comparisons = [
            self._project_source_window(source, comparison_limit)
            for source in comparison_sources
            if comparison_limit > 0 and source["text"][:comparison_limit]
        ]
        return [*required_sources, *projected_comparisons]

    def _assessment_source_context(self):
        """Expose authoritative captures before evidence-aware context budgeting."""
        result = []
        for ref in sorted(self.source_docs):
            value = self.source_docs[ref]
            if not authoritative_source(value):
                continue
            representation = value["representation"]
            text = value["text"]
            result.append({"source_ref": ref, "work_id": value["work_id"],
                           "source_availability": self._source_availability(value["work_id"]),
                           "representation": representation,
                           "identity_verified": value.get("identity_verified", False),
                           "identity_checks": deepcopy(value.get("identity_checks")), "url": value.get("url"),
                           "text": text,
                           "available_chars": len(value["text"]),
                           "window": {"start": 0, "end": len(text)}})
        return result

    @staticmethod
    def _compact_assessment_coverage(coverage):
        """Keep coverage decisions while removing bulky retrieval payloads."""
        if not isinstance(coverage, dict):
            return {}
        compact = {
            key: deepcopy(coverage.get(key))
            for key in ("unique_works", "abstracts", "verified_full_texts",
                        "bibliographic_identities", "pagination_remaining", "saturated", "scope",
                        "source_evidence_policy", "source_availability")
            if key in coverage
        }
        compact["searches"] = []
        for row in coverage.get("searches", [])[-64:]:
            if not isinstance(row, dict):
                continue
            request = row.get("request")
            request_projection = None
            if isinstance(request, dict):
                request_projection = {
                    key: request.get(key) for key in ("operation", "query", "cursor")
                    if request.get(key) is not None
                }
            new_work_ids = row.get("new_work_ids", [])
            if not isinstance(new_work_ids, list):
                new_work_ids = []
            compact["searches"].append({
                "request": request_projection,
                "outcome": row.get("outcome"),
                "provider": row.get("provider"),
                "has_more": row.get("has_more"),
                "new_work_ids": [item for item in new_work_ids[:16]
                                 if isinstance(item, str)],
            })
        compact["expansion"] = []
        for row in coverage.get("expansion", [])[-32:]:
            if not isinstance(row, dict):
                continue
            compact["expansion"].append({
                key: deepcopy(row.get(key)) for key in (
                    "round", "seed_work_ids", "requested_work_ids", "new_work_ids",
                    "quiet_rounds", "remaining_pages") if key in row
            })
        compact["access_and_limit_gaps"] = deepcopy(
            coverage.get("access_and_limit_gaps", [])[-64:])
        compact["source_windows"] = [
            {key: item.get(key) for key in ("source_ref", "available_chars", "window")}
            for item in coverage.get("source_windows", [])[-256:]
            if isinstance(item, dict)
        ]
        return compact

    @staticmethod
    def _project_assessment_sources(sources, *, full_text_chars,
                                    abstract_chars, unverified_chars, evidence=(),
                                    disjoint_evidence=False):
        """Shrink background context without cutting any supplied quotation."""
        anchors = {}
        for proof in evidence:
            anchors.setdefault(proof["source_ref"], []).append(proof)
        projected = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            representation = source.get("representation")
            if representation == "abstract":
                limit = abstract_chars
            elif representation == "unverified_text":
                limit = unverified_chars
            else:
                limit = full_text_chars
            proofs = anchors.get(source["source_ref"], [])
            if not proofs:
                projected.append(SurveyRunner._project_source_window(source, limit))
                continue
            offset = source["window"]["start"]
            start = min(proof["start"] for proof in proofs)
            end = max(proof["end"] for proof in proofs)
            if not offset <= start < end <= source["window"]["end"]:
                raise ValidationError("assessment source omits a required evidence span")
            if disjoint_evidence:
                ranges = source_window_ranges(
                    [{"start": proof["start"], "end": proof["end"]} for proof in proofs],
                    source["window"]["end"])
                projected.extend({**source, "text": source["text"][span["start"]-offset:span["end"]-offset],
                                  "window": span} for span in ranges)
                continue
            # Mandatory evidence is never truncated to meet a nominal profile.
            # The enclosing admission check decides whether the packet fits.
            spare = max(0, limit - (end - start))
            start = max(offset, start - spare // 2)
            end = min(source["window"]["end"], max(end, start + limit))
            projected.append({**source, "text": source["text"][start-offset:end-offset],
                              "window": {"start": start, "end": end}})
        return projected

    def _fit_assessment_assignment(self, assignment):
        """Fit the aggregate gap decision to the strictest model route."""
        limit = self._map_input_limit("methods.novelty-verifier")
        sources = assignment.get("sources", [])
        source_lookup = {source["source_ref"]: source for source in sources}
        # Production captures use absolute offsets. A caller may supply a
        # windowed source, so use its full retained capture for binding.
        source_lookup.update({ref: value for ref, value in self.source_docs.items()
                              if ref in source_lookup})
        mapped, catalog = index_evidence(assignment.get("map", {}), source_lookup)
        assignment = {**assignment, "map": mapped, "evidence_catalog": catalog,
                      "coverage": self._compact_assessment_coverage(assignment.get("coverage", {}))}
        assignment["coverage"]["source_windows"] = [
            {key: source[key] for key in ("source_ref", "available_chars", "window")}
            for source in sources]

        def fits(candidate):
            prompt = json.dumps(candidate, ensure_ascii=False)
            return limit is None or estimate_input_tokens(SYSTEM, prompt) <= limit

        if fits(assignment):
            return assignment
        coverage = self._compact_assessment_coverage(assignment.get("coverage", {}))
        def project(full_text_chars, abstract_chars, unverified_chars, *, disjoint=False):
            candidate = {**assignment, "coverage": deepcopy(coverage),
                "sources": self._project_assessment_sources(
                    sources, full_text_chars=full_text_chars,
                    abstract_chars=abstract_chars, unverified_chars=unverified_chars,
                    evidence=catalog, disjoint_evidence=disjoint)}
            candidate["coverage"]["source_windows"] = [
                {key: source[key] for key in ("source_ref", "available_chars", "window")}
                for source in candidate["sources"]]
            if disjoint:
                candidate["source_projection"] = "disjoint_quoted_windows"
                candidate["instructions"] = assignment.get("instructions", _GAP_ASSESSMENT_INSTRUCTIONS) + (
                    " Source rows may show disjoint exact quotation windows of the same capture. "
                    "Every mapped quotation is retained; intervening source text is not displayed. "
                    "Omitted passages cannot establish absence of a result or exhaustive full-text review.")
            return candidate
        profiles = (
            (100000, 2500, 40000),
            (60000, 1600, 24000),
            (30000, 1000, 12000),
            (16000, 700, 8000),
            (8000, 450, 4000),
            (4000, 250, 2000),
            (2000, 128, 512),
            (0, 0, 0),
        )
        for full_text_chars, abstract_chars, unverified_chars in profiles:
            candidate = project(full_text_chars, abstract_chars, unverified_chars)
            if fits(candidate):
                return candidate
        candidate = project(0, 0, 0, disjoint=True)
        if fits(candidate):
            return candidate
        raise ValidationError(
            "literature gap-assessment assignment cannot fit any configured provider context budget: "
            f"required evidence exceeds {limit} tokens; split the assessment scope rather than truncate quotations")

    @staticmethod
    def _assessment_statement(statement):
        if not isinstance(statement, dict):
            return {"text": None, "evidence": []}
        return {"text": statement.get("text"), "evidence": [
            {key: proof.get(key) for key in ("work_id", "source_ref", "quote", "start", "end", "quote_sha256")
             if proof.get(key) is not None}
            for proof in statement.get("evidence", []) if isinstance(proof, dict)
        ]}

    def _assessment_map_context(self):
        """Project map claims to evidence needed for gap reasoning."""
        entries = []
        for record in self.analysis_records.values():
            entry = self._body(record)
            entries.append({"work_id": entry.get("work_id"),
                            "inclusion": entry.get("inclusion"),
                            "reason": entry.get("reason"),
                            **{field: self._assessment_statement(entry.get(field))
                               for field in MAP_FIELDS}})
        relationships = []
        for relation in self.relationships.values():
            relationships.append({"source": relation.get("source"),
                                  "target": relation.get("target"),
                                  "kind": relation.get("kind"),
                                  "claim": self._assessment_statement(relation.get("claim"))})
        return {"entries": entries, "relationships": relationships}

    def _bind_assessment_spans(self, value, sources):
        """Bind quotes and repair an unambiguous abstract/full-text mismatch.

        A map quote can be copied from an abstract while a reviewer selects
        the same work's full-text source reference.  Rebinding is allowed only
        when the exact quote occurs once in another displayed source for that
        work; unsupported or ambiguous quotes still fail the normal evidence
        validator and are sent back for a scoped retry.
        """
        source_values = {source["source_ref"]: self.source_docs[source["source_ref"]]
                         for source in sources}
        repaired = SurveyGate._rebind_assessment_sources(
            deepcopy(value), source_values, index_source_windows(sources))
        return self._bind_visible_spans(repaired, sources)

    def _bind_visible_spans(self, value, sources):
        windows = index_source_windows(sources)
        return bind_source_spans(value, self.source_docs, windows=windows)

    def _coverage(self):
        abstentions = []
        for record in self._heads("command/survey-abstentions/"):
            body = self._body(record)
            current = self.analysis_records.get(body["work_id"])
            if current and body["entry_sha256"] == current["body_hash"]:
                abstentions.append(body)
        identity_counts = Counter(self._body(record)["status"] for record in self.identity_records.values())
        abstract_count = sum(w.get("abstract") is not None for w in self.works.values())
        return {"unique_works": len(self.works), "abstracts": abstract_count,
            "source_inventory_work_records": len(self.works),
            "map_entry_count": len(self.analysis_records),
            "abstract_work_count": abstract_count,
            "verified_full_texts": sum(s["representation"] == "full_text" for s in self.source_docs.values()),
            "bibliographic_identities": {"checked": len(self.identity_records),
                "verified": identity_counts["verified"] + identity_counts["verified_with_gaps"],
                "conflicted": identity_counts["conflicted"],
                "unresolved": sum(count for status, count in identity_counts.items()
                                  if status not in {"verified", "verified_with_gaps", "conflicted"}),
                "by_status": dict(sorted(identity_counts.items()))},
            "searches": self.search_log, "expansion": self.expansion_log, "access_and_limit_gaps": self.gaps,
            "exploration_tree": self.exploration_tree,
            "exploration_tree_ref": ((self.store.head("kb/exploration-tree") or {}).get("artifact_ref")),
            "pagination_remaining": bool(self._pending_bibliographic_pages()),
            "saturated": bool(self.expansion_log and self.expansion_log[-1]["quiet_rounds"] >= self.bounds["saturation_rounds"]),
            "scope": "Recorded finite queries and citation expansion; no exhaustive-coverage claim",
            "source_evidence_policy": _SOURCE_EVIDENCE_POLICY,
            "scientific_input_recovery": scientific_input_recovery_contract(),
            "source_availability": [self._source_availability(wid) for wid in sorted({
                gap["work_id"] for gap in self.gaps if gap.get("kind") == "full_text_failure"
                and gap.get("work_id") in self.works})],
            "deep_analysis_limit": (None if self.exploration_tree is not None else
                                    self.bounds.get("max_analyzed_works", self.bounds["max_works"])),
            "reading_selection_policy": ("model_decisions_with_call_token_and_time_limits" if self.exploration_tree is not None else
                                         "legacy_analysis_limit"),
            "abstentions": abstentions,
            "source_windows": [{"source_ref": item["source_ref"], "available_chars": item["available_chars"], "window": item["window"]}
                               for item in sorted(self._source_context(), key=lambda item: item["source_ref"])]}

    @staticmethod
    def _source_less_reason():
        return ABSTENTION_REASONS["source_unavailable"]

    def _materialize_source_less_map(self, wid, basis, *, scope="source_unavailable"):
        """Commit hash-bound non-admission for the declared procedural scope."""
        reason = ABSTENTION_REASONS[scope]
        null_statement = {"text": None, "evidence": []}
        value = {
            "entries": [{
                "work_id": wid,
                "inclusion": "uncertain",
                "reason": reason,
                "problem": deepcopy(null_statement),
                "approach": deepcopy(null_statement),
                "finding": deepcopy(null_statement),
                "limitations": deepcopy(null_statement),
            }],
            "relationships": [],
        }
        validate_map(value, [wid], set(self.works), self.source_docs, require_spans=True)
        execution = self._record(
            f"command/executions/survey-map-deterministic-{wid}", "report", {
                "operation": "literature-map",
                "outcome": "ok",
                "execution_kind": "deterministic_abstention",
                "work_id": wid,
                "source_refs": [ref for ref, source in self.source_docs.items() if source["work_id"] == wid],
                "model_calls": 0,
                "reason": reason,
            }, "command.controller", subjects=basis)
        self.analysis_records[wid] = self._record(
            f"kb/work-analyses/{wid}", "note", value["entries"][0],
            "research.literature-mapper", subjects=[execution["artifact_ref"], *basis])
        self.analyzed_basis[wid] = list(basis)
        self._record(f"command/survey-abstentions/{wid}", "note", {
            "work_id": wid, "reason": reason, "entry_sha256": hashlib.sha256(canonical_bytes(value["entries"][0])).hexdigest(),
            "scope": scope, "model_calls": 0,
        }, "command.controller", subjects=[execution["artifact_ref"]])
        self.relationships = {
            key: relation for key, relation in self.relationships.items()
            if relation["source"] != wid
        }
        self._record_analysis_completion(wid, basis, execution["artifact_ref"])

    def _analysis_basis(self, wid):
        return [self.work_records[wid]["artifact_ref"],
                *([self.identity_records[wid]["artifact_ref"]] if wid in self.identity_records else []),
                *sorted(ref for ref, source in self.source_docs.items() if source["work_id"] == wid)]

    def _record_analysis_completion(self, wid, basis, execution):
        """Retain checked dependencies independently of unchanged claim bodies."""
        relationships = sorted(relation["artifact_ref"] for relation in self.relationships.values()
                               if relation["source"] == wid)
        self._record(f"command/survey-analysis-completions/{wid}", "note", {
            "work_id": wid, "question": self.score["question"], "basis": list(basis),
            "entry_ref": self.analysis_records[wid]["artifact_ref"],
            "relationship_refs": relationships, "execution_ref": execution,
        }, "command.controller", subjects=[execution, *basis,
                                            self.analysis_records[wid]["artifact_ref"], *relationships])

    def _map(self):
        requested = []
        basis = {}
        selected = self._analysis_selection()
        promotion_allowed = (not self.resume_session or "mapping" in self.resume_session["reopened_scopes"]
                             or self._countersearch_active or bool(self._tree_admitted_reads))
        for wid, work in self.work_records.items():
            basis[wid] = self._analysis_basis(wid)
            previous = self._body(self.analysis_records[wid]) if wid in self.analysis_records else None
            relationships = [relation for relation in self.relationships.values() if relation["source"] == wid]
            reopened = promotion_allowed and wid in selected and self._is_deferred_analysis(wid)
            if self._tree_admitted_reads is not None and wid not in self._tree_admitted_reads:
                reopened = False
                if previous is None or wid not in selected:
                    continue
            if (self.analyzed_basis.get(wid) != basis[wid] or previous is None or reopened
                    or contains_legacy(previous) or contains_legacy(relationships)):
                requested.append(wid)
        changed = set(requested)
        for relationship in self.relationships.values():
            if (relationship["source"] not in requested and (relationship["target"] in changed or any(
                    proof["source_ref"] not in self.source_docs for proof in relationship["claim"]["evidence"]))):
                requested.append(relationship["source"])
        if requested:
            model_requested = []
            for wid in requested:
                if wid not in selected:
                    scope = "reading_deferred" if self.exploration_tree is not None else "deep_analysis_budget"
                    self._materialize_source_less_map(wid, basis[wid], scope=scope)
                    continue
                if any(source["work_id"] == wid and authoritative_source(source)
                       for source in self.source_docs.values()):
                    model_requested.append(wid)
                else:
                    self._materialize_source_less_map(wid, basis[wid])
            if model_requested:
                if self._tree_read_order is not None:
                    priority = {wid: index for index, wid in enumerate(self._tree_read_order)}
                    model_requested.sort(key=lambda wid: priority.get(wid, len(priority)))
                self._models_checked([self._map_job(wid, basis[wid]) for wid in model_requested])
        edges = []
        for wid, work in sorted(self.works.items()):
            for other in sorted(work["referenced_works"]):
                target = self.aliases.get(other, other)
                if target in self.works:
                    edges.append({"source": wid, "target": target, "kind": "cites"})
        self.map_record = self._record("kb/literature-map", "note", {
            "question": self.score["question"], "entry_refs": sorted(r["artifact_ref"] for r in self.analysis_records.values()),
            "relationship_refs": sorted(r["artifact_ref"] for r in self.relationships.values()), "citation_edges": edges,
            "publication_metadata_status": "provider_reported_with_separate_identity_reconciliation",
        }, "research.literature-mapper", subjects=[self.register_ref])

    def _is_deferred_analysis(self, wid):
        record = self.store.head(f"command/survey-abstentions/{wid}")
        current = self.analysis_records.get(wid)
        return bool(record and current and self._body(record).get("scope") in {"deep_analysis_budget", "model_call_budget", "reading_deferred"}
                    and self._body(record).get("entry_sha256") == current["body_hash"])

    def _map_job(self, wid, basis, *, review_feedback=None):
        previous = json.loads(self.store.read_body(self.analysis_records[wid]["body_hash"])) if wid in self.analysis_records else None
        old_relationships = [relation for relation in self.relationships.values() if relation["source"] == wid]
        sources = self._map_sources(wid, old_relationships=old_relationships,
                                    review_feedback=review_feedback)
        entry_editable = (self.analyzed_basis.get(wid) != basis or contains_legacy(previous)
                          or contains_legacy(old_relationships) or self._is_deferred_analysis(wid))
        if review_feedback is not None:
            entry_editable = bool(review_feedback["entry_fields"])
        assignment = {
            "assignment": "Assess the single assigned work and propose supported outgoing conceptual connections.", "phase": "map",
            "question": self.score["question"], "requested_work_ids": [wid],
            "works": [{**{key: work[key] for key in ("id", "title", "year", "doi", "publication_metadata_status")},
                       "identity_ref": self.identity_records[work["id"]]["artifact_ref"] if work["id"] in self.identity_records else None,
                       "identity_status": self._body(self.identity_records[work["id"]])["status"] if work["id"] in self.identity_records else "not_checked"}
                      for work in sorted(self.works.values(), key=lambda item: item["id"])],
            "previous_entries": [previous] if previous is not None else [], "entry_editable": entry_editable,
            "previous_affected_relationships": old_relationships,
            "semantic_feedback": review_feedback,
            "relationship_semantics": RELATIONSHIP_SEMANTICS,
            "source_fidelity_contract": source_fidelity_review_contract(),
            "sources": sources,
            "instructions": "Return exactly {entries:[{work_id:string,inclusion:string,reason:string,problem:Statement,approach:Statement,finding:Statement,limitations:Statement}],"
                "relationships:[{source:string,target:string,kind:string,claim:Statement}]}. entries must contain exactly the assigned work. "
                "inclusion is included/excluded/uncertain. reason is a plain string, never an evidence object. "
                "Only problem, approach, finding, limitations, and relationship claim use Statement={text:string|null,evidence:[{work_id:string,source_ref:string,quote:string}]}. "
                "For unknown facts return {text:null,evidence:[]}. Every substantive statement needs a short contiguous exact quote, usually 3-12 words, from the displayed text. "
                "Each quote must occur exactly once in its displayed source window; make it longer when repeated text would be ambiguous. "
                "Every clause must be supported. Report limitations only when the source states them explicitly; an abstract's silence cannot establish an untested domain or omitted comparison. "
                "Copy quote characters exactly, including whitespace, Markdown, punctuation, and literal escaped newline characters; do not paraphrase, normalize, or repair quotations. "
                "Use only displayed source_ref values. Each entry statement must cite the assigned work. "
                "Each relationship source must be the assigned work; target must be a different known work. "
                "kind is extends/contradicts/compares/related; claim needs evidence from BOTH works. Omit unsupported relationships. "
                "Other works are supplied for comparison, not for editing. Preserve supported previous fields and outgoing relationships. "
                "If entry_editable is false, copy the previous entry exactly without changing any field; only its outgoing relationships may change. "
                "When semantic_feedback is present, change only its entry_fields and relationships with its relationship_targets. "
                "Preserve every other field and relationship exactly. Correct unsupported claims by narrowing them to the evidence, recording unknown facts, or removing unsupported relationships. "
                "Titles, years, and citation links are provider-reported catalog metadata, not textual evidence for substantive claims. "
                "Do not infer confirmed chronology, conceptual inheritance, identity claims, or superiority from metadata or shared terminology alone. "
                "When no captured source belongs to the assigned work, set inclusion to uncertain and make reason a narrow availability note: state that the catalog record is relevant by metadata but no abstract or verified full text was available, so substantive content could not be assessed. Do not put other work IDs, quotations, chronology, evolution, extension, comparison, or superiority in that reason. "
                "Keep statement text concise while retaining the conditions, uncertainty, and assumptions required by source_fidelity_contract. "
                "Return at most one outgoing relationship, and omit any relationship that is not directly supported by both displayed works. Return only the requested JSON object."
        }
        # ModelClient sends this assignment JSON verbatim. Keep the stable
        # corpus and contract before per-work state so provider prefix caches
        # can reuse the large common token sequence across map jobs. The
        # requested work, prior entry, feedback, and source window are
        # intentionally placed after that stable prefix because they vary by
        # assignment or repair attempt.
        assignment = {key: assignment[key] for key in (
            "assignment", "phase", "question", "works", "relationship_semantics", "source_fidelity_contract", "instructions",
            "requested_work_ids", "previous_entries", "entry_editable",
            "previous_affected_relationships", "semantic_feedback", "sources",
        )}
        assignment["works"] = self._project_map_catalog(
            assignment["works"],
            visible_ids={wid, *(source["work_id"] for source in sources)},
            target_ids={relation["target"] for relation in old_relationships},
            max_items=None)
        if review_feedback is not None:
            editable_fields = list(review_feedback["entry_fields"])
            editable_targets = list(review_feedback["relationship_targets"])
            assignment.update({
                "response_contract": "scoped_patch",
                "editable_entry_fields": editable_fields,
                "editable_relationship_targets": editable_targets,
                "instructions":
                    "Return exactly {entry_updates:{assigned_work_id:{field:value}},relationships:[{source,target,kind,claim}]}. "
                    "entry_updates must contain exactly one key, the assigned work ID, whose nested object contains a subset of editable_entry_fields; an empty object retains every entry field for independent re-review. Omit unchanged entry fields; do not return a complete entry. "
                    "For inclusion use included/excluded/uncertain; reason is a plain string; problem, approach, finding, and limitations use "
                    "Statement={text:string|null,evidence:[{work_id:string,source_ref:string,quote:string}]}. "
                    "relationships contains only replacements for editable_relationship_targets. Return the exact unchanged editable relationship to retain it; omit an editable relationship only to deliberately withdraw it. Protected relationships are retained by the control plane. "
                    "The negative review is a disputed diagnosis, not proof. Retain source-supported assertions when the diagnosis is unsupported; every retention is independently re-reviewed and does not establish scientific acceptance. "
                    "Every substantive statement needs a short contiguous exact quote from the displayed source, with every clause supported. "
                    "Relationship source must be the assigned work, target must be editable, kind is extends/contradicts/compares/related, "
                    "and its claim needs evidence from both works. Use only displayed source_ref values. "
                    "If no captured source belongs to the assigned work, the only valid repair for inclusion/reason is inclusion=uncertain with a narrow note that catalog metadata is relevant but no abstract or verified full text was available, so substantive content could not be assessed; remove other work IDs, quotations, chronology, evolution, extension, comparison, and superiority from that reason. "
                "Correct the failed checks narrowly by grounding, narrowing, setting unknown, or deleting an unsupported relationship."
            })

        if review_feedback is not None and "relationship_refs" in review_feedback:
            assignment["editable_relationship_refs"] = list(review_feedback["relationship_refs"])
            assignment["instructions"] += (
                " editable_relationship_refs pins the exact old relations that may change. "
                "Other old relations remain protected even when they share an editable target; the controller retains them. "
                "Return replacements only for the granted old relations, and omit one to withdraw it.")
        if review_feedback is not None and self._review_comparison_ids(wid):
            assignment["critique_contexts"] = self._review_critique_contexts(wid)
            assignment["instructions"] += (
                " Comparison entries are independently pinned context for checking a consistent screening criterion. "
                "Their inclusion is not a required verdict for the assigned work. Use current comparisons and captured sources; "
                "retain or revise only granted fields and relationships, with an evidence-grounded rationale.")
        assignment = self._fit_map_assignment(assignment, owner_id=wid)
        sources = assignment["sources"]
        own_sources = [source for source in sources if source["work_id"] == wid]
        projection_issues = []

        # A catalog-only record cannot support substantive prose.  Asking a
        # model to restate that negative evidence repeatedly creates a
        # pointless retry loop: the model can invent a broader availability
        # explanation even though the validator must reject it.  Project the
        # deterministic, metadata-only state locally and reserve model calls
        # for assignments that actually contain source text.
        source_less_reason = self._source_less_reason()
        null_statement = {"text": None, "evidence": []}

        def source_less_value(_value):
            if review_feedback is not None:
                updates = {}
                for field in review_feedback["entry_fields"]:
                    if field == "inclusion":
                        updates[field] = "uncertain"
                    elif field == "reason":
                        updates[field] = source_less_reason
                    elif field in MAP_FIELDS:
                        updates[field] = deepcopy(null_statement)
                    else:
                        raise ValidationError(f"unsupported source-less repair field: {field}")
                return {"entry_updates": updates, "relationships": []}
            return {
                "entries": [{
                    "work_id": wid,
                    "inclusion": "uncertain",
                    "reason": source_less_reason,
                    "problem": deepcopy(null_statement),
                    "approach": deepcopy(null_statement),
                    "finding": deepcopy(null_statement),
                    "limitations": deepcopy(null_statement),
                }],
                "relationships": [],
            }

        def normalize(value):
            projection_issues.clear()
            value = normalize_map_relationships(value)
            if not own_sources:
                return source_less_value(value)
            windows = {source["source_ref"]: source["window"]
                       for source in sources if isinstance(source.get("window"), dict)}
            return normalize_map_worker_response(
                value, work_id=wid, all_work_ids=set(self.works),
                sources=sources, windows=windows,
                entry_editable=entry_editable, previous=previous,
                review_feedback=review_feedback,
                projection_issues=projection_issues,
            )

        effective = {}
        def validate(value, *, controller_withdrawal=False):
            if review_feedback is not None:
                value = apply_scoped_map_repair(
                    wid, previous, old_relationships, review_feedback, value,
                    reject_ungranted_changes=True)
                if controller_withdrawal and "reason" in review_feedback["entry_fields"]:
                    value["entries"][0]["inclusion"] = "uncertain"
            validate_map(value, [wid], set(self.works), self.source_docs, require_spans=True)
            if not own_sources:
                entry = value["entries"][0]
                reason = entry["reason"].lower()
                forbidden = set(self.works) - {wid}
                if entry["inclusion"] != "uncertain":
                    raise ValidationError("a work without captured source text must be marked uncertain")
                if ("abstract" not in reason or "full text" not in reason
                        or not any(token in reason for token in ("could not", "cannot", "not available", "unavailable"))
                        or any(other.lower() in reason for other in forbidden)
                        or any(token in reason for token in ("evolved", "extends", "superior", "foundational", "chronolog"))):
                    raise ValidationError("a source-less work reason must report only metadata relevance and unavailable abstract/full text")
            if not entry_editable and canonical_bytes(value["entries"][0]) != canonical_bytes(previous):
                raise ValidationError(f"unchanged work {wid} must preserve its exact previous entry; only outgoing relationships may change")
            if any(relation["source"] != wid for relation in value["relationships"]):
                raise ValidationError(f"relationship source must be its assigned owner {wid}")
            effective["value"] = value

        def integrate(value, execution):
            value = effective["value"]
            entry = value["entries"][0]
            self.analysis_records[wid] = self._record(f"kb/work-analyses/{wid}", "note", entry,
                "research.literature-mapper", subjects=[execution, *basis])
            self.analyzed_basis[wid] = basis
            self.relationships = {key: relation for key, relation in self.relationships.items() if relation["source"] != wid}
            for relationship in value["relationships"]:
                key = "-".join(relationship[field] for field in ("source", "target", "kind"))
                record = self._record(f"kb/relationships/{key}", "note", relationship,
                    "research.literature-mapper", subjects=[execution, *[proof["source_ref"] for proof in relationship["claim"]["evidence"]]])
                self.relationships[key] = {**relationship, "artifact_ref": record["artifact_ref"]}
            self._record_analysis_completion(wid, basis, execution)
            projection_ref = None
            if projection_issues:
                execution_key = hashlib.sha256(execution.encode("utf-8")).hexdigest()[:20]
                projection = self._record(
                    f"command/map-projections/{wid}-{execution_key}", "decision_note", {
                        "work_id": wid,
                        "execution_ref": execution,
                        "outcome": "unsupported or ungranted response content was excluded",
                        "issues": deepcopy(projection_issues),
                    }, "command.controller", subjects=[execution, *basis])
                projection_ref = projection["artifact_ref"]
            if (entry["inclusion"] == "uncertain"
                    and entry["reason"] == ABSTENTION_REASONS["unverified_map"]
                    and all(entry[field]["text"] is None for field in MAP_FIELDS)):
                entry_hash = hashlib.sha256(canonical_bytes(entry)).hexdigest()
                self._record(f"command/survey-abstentions/{wid}", "note", {
                    "work_id": wid,
                    "reason": entry["reason"],
                    "scope": "unverified_map",
                    "entry_sha256": entry_hash,
                    "execution_ref": execution,
                    "projection_ref": projection_ref,
                }, "command.controller", subjects=[execution, *([projection_ref] if projection_ref else [])])

        def abstain(state):
            null = {"text": None, "evidence": []}
            if review_feedback is not None:
                updates = {field: deepcopy(null) if field in MAP_FIELDS else
                           "uncertain" if field == "inclusion" else
                           ABSTENTION_REASONS["screening_unresolved"]
                           for field in review_feedback["entry_fields"]}
                value = {"entry_updates": updates, "relationships": []}
            else:
                entry = (deepcopy(previous) if previous is not None and not entry_editable else {
                    "work_id": wid, "inclusion": "uncertain",
                    "reason": ABSTENTION_REASONS["contract_exhausted"],
                    **{field: deepcopy(null) for field in MAP_FIELDS}})
                value = {"entries": [entry], "relationships": []}
            value = normalize(value)
            validate(value, controller_withdrawal=True)
            execution = self._record(f"command/survey-abstentions/{wid}", "note", {
                "work_id": wid, "reason": state["error"], "scope": "contract_exhausted",
                "withdrawn_fields": _map_withdrawn_fields(review_feedback["entry_fields"]) if review_feedback else list(MAP_FIELDS),
                "entry_sha256": hashlib.sha256(canonical_bytes(effective["value"]["entries"][0])).hexdigest(),
                "model_calls": 0,
            }, "command.controller", subjects=basis)
            gap = {"work_id": wid, "kind": "claim_contract_exhausted", "artifact_ref": execution["artifact_ref"]}
            if gap not in self.gaps:
                self.gaps.append(gap)
            return value, execution["artifact_ref"]

        return {"name": f"map-{wid}", "actor": "research.literature-mapper", "assignment": assignment,
                # Preserve the mission's configured inference profile for
                # source binding as well.  A compute-rich run may deliberately
                # spend the same reasoning and output budget on every
                # scientific role; a provider that does not expose a profile
                # simply receives no override here.
                "model_overrides": {
                    "max_output_tokens": int(self.model_config["max_output_tokens"]),
                    **({"reasoning_effort": self.model_config["reasoning_effort"]}
                       if self.model_config.get("reasoning_effort") is not None else {}),
                },
                "normalizer": normalize,
                "validator": validate,
                "withdrawal_validator": lambda value: validate(value, controller_withdrawal=True),
                "on_valid": integrate, "on_exhausted": abstain}

    def _map_body(self):
        return {"entries": [json.loads(self.store.read_body(r["body_hash"])) for r in
                            sorted(self.analysis_records.values(), key=lambda record: record["artifact_ref"])],
                "relationships": sorted(self.relationships.values(), key=lambda record: record["artifact_ref"]),
                **json.loads(self.store.read_body(self.map_record["body_hash"]))}

    def _survey_review_source_windows(self, entries, relationships):
        """Expose complete abstracts and paragraph context for every cited span."""
        from scisaurus.core.source_spans import validate as validate_span
        anchors = {}
        for statement in [*(entry[field] for entry in entries for field in MAP_FIELDS),
                          *(relation["claim"] for relation in relationships)]:
            for proof in statement.get("evidence", []):
                ref = proof.get("source_ref")
                source = self.source_docs.get(ref)
                if source is None:
                    raise ValidationError("integrated review citation has no retained captured source")
                validate_span(proof, source, require_span=True)
                anchors.setdefault(ref, []).append((proof["start"], proof["end"]))
        visible_ids = {entry["work_id"] for entry in entries}
        critique_sources = {pin["ref"] for obligation in self.review_obligations
                            for pin in obligation["source_pins"]}
        projected = []
        represented_ids = set()
        for source in self._assessment_source_context():
            ref, text = source["source_ref"], source["text"]
            if source["work_id"] not in visible_ids and ref not in anchors and ref not in critique_sources:
                continue
            manifest = self.store.get(ref)
            captured = self._body(manifest)
            if (captured.get("text") != text or captured.get("work_id") != source["work_id"]
                    or captured.get("representation") != source["representation"]):
                raise ValidationError("integrated source window does not match its immutable capture")
            spans = anchors.get(ref, [])
            if source["representation"] == "abstract":
                windows = [(0, len(text))]
            elif spans:
                breaks = [(match.start(), match.end()) for match in re.finditer(r"\n[ \t]*\n", text)]
                windows = []
                for start, end in spans:
                    left = max((boundary_end for _, boundary_end in breaks if boundary_end <= start), default=0)
                    right = min((boundary_start for boundary_start, _ in breaks if boundary_start >= end), default=len(text))
                    windows.append((left, right))
                if ref in critique_sources:
                    windows.append((0, min(len(text), self.bounds["context_chars"])))
            elif ref in critique_sources:
                windows = [(0, min(len(text), self.bounds["context_chars"]))]
            else:
                continue
            merged = []
            for start, end in sorted(windows):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            source_hash = manifest["body_hash"]
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            for start, end in merged:
                window_text = text[start:end]
                projected.append({**source, "text": window_text, "window": {"start": start, "end": end},
                                  "source_body_sha256": source_hash, "source_text_sha256": text_hash,
                                  "window_sha256": hashlib.sha256(window_text.encode("utf-8")).hexdigest()})
            represented_ids.add(source["work_id"])
        if any(entry["inclusion"] == "included" and entry["work_id"] not in represented_ids for entry in entries):
            raise ValidationError("included work has no captured source text for independent relevance review")
        return projected

    def _survey_review_packet(self):
        """Build an independently auditable map and cited-source review packet."""
        def compact_statement(statement):
            if not isinstance(statement, dict):
                return {"text": None, "evidence": []}
            proofs = []
            for proof in statement.get("evidence", []):
                if not isinstance(proof, dict):
                    continue
                proofs.append({key: proof.get(key) for key in ("work_id", "source_ref", "quote", "start", "end", "quote_sha256")
                               if proof.get(key) is not None})
            return {"text": statement.get("text"), "evidence": proofs}

        entries = []
        for wid in sorted(self.analysis_records):
            record = self.analysis_records[wid]
            entry = json.loads(self.store.read_body(record["body_hash"]))
            entries.append({
                "work_id": entry.get("work_id"),
                "inclusion": entry.get("inclusion"),
                "reason": entry.get("reason"),
                **{field: compact_statement(entry.get(field)) for field in MAP_FIELDS},
            })

        relationships = []
        for key in sorted(self.relationships):
            relation = self.relationships[key]
            claim = relation.get("claim") or {}
            relationships.append({
                "artifact_ref": relation["artifact_ref"],
                "source": relation.get("source"),
                "target": relation.get("target"),
                "kind": relation.get("kind"),
                "claim": compact_statement(claim),
            })

        coverage = self._coverage()
        search_counts = {}
        for row in coverage.get("searches", []):
            if not isinstance(row, dict):
                continue
            outcome = row.get("outcome", "unknown")
            search_counts[outcome] = search_counts.get(outcome, 0) + 1
        gap_counts = {}
        for row in coverage.get("access_and_limit_gaps", []):
            if not isinstance(row, dict):
                continue
            kind = row.get("kind", "unknown")
            gap_counts[kind] = gap_counts.get(kind, 0) + 1
        coverage_summary = {
            key: coverage.get(key) for key in (
                "unique_works", "abstracts", "verified_full_texts", "bibliographic_identities",
                "pagination_remaining", "saturated",
                "source_inventory_work_records", "map_entry_count", "abstract_work_count",
            )
        }
        coverage_summary.update({
            "deep_analysis_limit": coverage["deep_analysis_limit"],
            "abstention_count": len(coverage["abstentions"]),
            "deferred_analysis_count": sum(row.get("scope") == "deep_analysis_budget" for row in coverage["abstentions"]),
            "search_count": len(coverage.get("searches", [])),
            "search_outcomes": search_counts,
            "gap_count": len(coverage.get("access_and_limit_gaps", [])),
            "gap_kinds": gap_counts,
            "entry_inclusion_counts": dict(sorted(Counter(entry["inclusion"] for entry in entries).items())),
            "claimless_entry_count": sum(all(entry[field]["text"] is None for field in MAP_FIELDS) for entry in entries),
            "count_definitions": {
                "abstention_count": "Current hash-bound controller abstention records, including partial withdrawals; not all uncertain entries or all claimless entries.",
                "entry_inclusion_counts": "Screening decisions for every current catalog record; independent of controller abstention records. Publication versions may describe the same study, so included records are not a count of independent studies.",
                "claimless_entry_count": "Entries with no problem, approach, finding, or limitation assertion; these do not provide scientific support.",
                "bibliographic_identities": "checked equals verified plus conflicted plus unresolved; by_status is the complete partition of checked records.",
                "unique_works": "Distinct catalog work IDs captured by the finite search; this is the source inventory size, not a deduplicated study count. Preprints and final publications may have different catalog IDs.",
                "source_inventory_work_records": "The number of distinct catalog work IDs in the source inventory, equal to unique_works.",
                "captured_source_work_count": "Distinct work IDs with captured source documents, including abstracts and full text. Catalog records without captured source text are excluded; this can be smaller than unique_works.",
                "map_entry_count": "The number of current literature-map entries, including explicit uncertain or deferred entries.",
                "abstract_work_count": "The number of catalog records with a non-null abstract; it is not the number of map entries or source records.",
                "abstracts": "Legacy label for abstract_work_count; it must not be compared to map_entry_count as if they were the same partition.",
            },
        })

        all_sources = []
        for source in sorted(self._source_context(), key=lambda source: source["source_ref"]):
            all_sources.append({key: source.get(key) for key in (
                "source_ref", "work_id", "representation", "identity_verified", "identity_checks", "url", "available_chars",
                "source_availability",
            )})

        entry_work_ids = {entry["work_id"] for entry in entries}
        source_work_ids = {source["work_id"] for source in all_sources}
        coverage_summary["captured_source_work_count"] = len(source_work_ids)
        relationship_endpoint_ids = {
            endpoint
            for relationship in relationships
            for endpoint in (relationship.get("source"), relationship.get("target"))
            if isinstance(endpoint, str)
        }
        deterministic_integrity = {
            "map_entry_count": len(entry_work_ids),
            "catalog_work_count": len(self.works),
            "captured_source_work_count": len(source_work_ids),
            "abstract_work_count": coverage_summary["abstract_work_count"],
            "relationship_count": len(relationships),
            "all_relationship_endpoints_in_map_entries": relationship_endpoint_ids.issubset(entry_work_ids),
            "all_source_work_ids_in_map_entries": source_work_ids.issubset(entry_work_ids),
            "relationship_endpoint_count": len(relationship_endpoint_ids),
        }

        # The aggregate reviewer does not need every abstention or every
        # claimless catalog row: those are already represented exactly by the
        # controller-computed counts above.  Passing them back through a model
        # makes it recount long lists and can exhaust the response budget
        # before it returns its three required checks.  Keep every claim-bearing
        # row and relationship endpoint visible, and disclose the projection.
        visible_work_ids = {
            entry["work_id"] for entry in entries
            if entry["inclusion"] == "included" or any(entry[field]["text"] is not None for field in MAP_FIELDS)
        } | relationship_endpoint_ids | {item["work_id"] for item in self.review_obligations}
        visible_entries = [entry for entry in entries if entry["work_id"] in visible_work_ids]
        sources = self._survey_review_source_windows(visible_entries, relationships)
        presented_source_count = len({source["source_ref"] for source in sources})

        all_focused_reviews = []
        for wid in sorted(self.work_reviews):
            record = self.work_reviews[wid]
            review = self._body(record)
            all_focused_reviews.append({
                "work_id": wid,
                "review_ref": record["artifact_ref"],
                "review_protocol": review.get("review_protocol"),
                "verification_kind": review.get("verification_kind", "source_bound_model_review"),
            })
        focused_reviews = [review for review in all_focused_reviews
                           if review["work_id"] in visible_work_ids
                           or review["verification_kind"] != "deterministic_abstention"]
        projection = {
            "entry_count": len(entries),
            "presented_entry_count": len(visible_entries),
            "omitted_claimless_entry_count": len(entries) - len(visible_entries),
            "source_record_count": len(all_sources),
            "presented_source_count": presented_source_count,
            "presented_source_window_count": len(sources),
            "omitted_source_record_count": len(all_sources) - presented_source_count,
            "focused_review_count": len(all_focused_reviews),
            "presented_focused_review_count": len(focused_reviews),
            "omitted_deterministic_abstention_review_count": sum(
                review["verification_kind"] == "deterministic_abstention"
                for review in all_focused_reviews if review not in focused_reviews
            ),
        }
        return {
            "map": {"entries": visible_entries, "relationships": relationships,
                    "entry_refs": {wid: self.analysis_records[wid]["artifact_ref"] for wid in sorted(visible_work_ids)},
                    "relationship_refs": {value["artifact_ref"]: {"source": value["source"], "target": value["target"], "kind": value["kind"]}
                                          for value in self.relationships.values()},
                    "projection": projection},
            "coverage": coverage_summary,
            "sources": sources,
            "focused_review_summary": focused_reviews,
            "review_contract": {
                **source_fidelity_review_contract(),
                "context_protocol": CURRENT_MAP_REVIEW_PROTOCOL,
                "history_scope": "Historical critique texts and superseded claims are adjudicated by focused work reviews. Assess only current map assertions and captured source windows; review references are provenance, not semantic support or a reason to pass.",
                "decision": "Whether the retained evidence map faithfully represents the captured sources and its disclosed limitations.",
                "upstream_checks": "Prior focused review references are provenance only, not semantic evidence or a reason to pass. Independently judge every retained claim, relationship clause, and included work's relevance using the supplied captured source windows and exact cited spans.",
                "projection_scope": "The map and source lists are claim-bearing projections; omitted counts are explicit in map.projection and full inventory counts are in coverage and deterministic_integrity. Do not invent a missing map entry or relationship endpoint that contradicts deterministic_integrity.",
            },
            "deterministic_integrity": deterministic_integrity,
            "projection": projection,
        }

    def _work_review_exhausted(self, wid, feedback=None):
        """An unchanged, already-withdrawn work does not gain new repair rounds."""
        withdrawal = self.store.head(f"kb/claim-withdrawals/{wid}")
        if withdrawal is None:
            return False
        entry = self.analysis_records[wid]
        inputs = {item["ref"] for item in entry.get("inputs", [])}
        review = self._body(self.store.get(self._body(withdrawal)["review_ref"]))
        try:
            validate_work_review({key: review[key] for key in ("checks", "rationale")},
                                 review["relationship_refs"], entry=self._body(self.store.get(review["entry_ref"])),
                                 review_obligations=self._review_obligations_for(wid))
        except ValidationError:
            return False
        return (self._review_evidence_scope(wid) == self._review_evidence_scope(wid, review=review)
                and (feedback is None or self._review_failure_keys(feedback) == self._review_failure_keys(review))
                and (withdrawal["artifact_ref"] in inputs or review["entry_ref"] == entry["artifact_ref"]))

    def _work_review_sources(self, wid):
        owners = {wid, *[relation["target"] for relation in self.relationships.values()
                        if relation["source"] == wid], *self._review_comparison_ids(wid)}
        pinned = {pin["ref"] for obligation in self._review_obligations_for(wid)
                  for pin in obligation["source_pins"]}
        return [source for source in self._assessment_source_context()
                if source["work_id"] in owners or source["source_ref"] in pinned]

    def _review_source_projection_matches(self, wid, sources):
        full_texts = [source for source in self._work_review_sources(wid)
                      if source["representation"] == "full_text"]
        if not full_texts:
            return True
        if not isinstance(sources, list):
            return False
        for expected in full_texts:
            presented = [source for source in sources if isinstance(source, dict)
                         and source.get("source_ref") == expected["source_ref"]]
            if (len(presented) != 1 or presented[0].get("text") != expected["text"]
                    or presented[0].get("window") != expected["window"]):
                return False
        return True

    def _review_protocol_matches(self, review):
        contract = source_fidelity_review_contract()
        if review.get("review_protocol") != contract["protocol"]:
            return False
        if review.get("verification_kind") == "deterministic_abstention":
            wid = self._body(self.store.get(review["entry_ref"]))["work_id"]
            abstention = self.store.head(f"command/survey-abstentions/{wid}")
            return (not self._review_obligations_for(wid) and abstention is not None
                    and review.get("execution_ref") == abstention["artifact_ref"]
                    and is_explicit_abstention(self._body(self.store.get(review["entry_ref"])), self._body(abstention)))
        try:
            wid = self._body(self.store.get(review["entry_ref"]))["work_id"]
            obligations = self._review_obligations_for(wid)
            execution_ref = review["execution_ref"]
            _, _, prompt, reply = self.gate._model_review_execution(execution_ref, "methods.work-reviewer")
            from scisaurus.runtime.survey_records import project_work_review_batch
            prompt, reply = project_work_review_batch(prompt, reply, entry_ref=review["entry_ref"], work_id=wid)
            reply = normalize_check_envelope(reply, work_review_checks(review["relationship_refs"], obligations))
            validate_work_review(reply, review["relationship_refs"], entry=prompt.get("entry"), review_obligations=obligations)
            return (prompt.get("phase") == "work_review" and prompt.get("review_contract") == contract
                    and self._review_source_projection_matches(wid, prompt.get("sources"))
                    and prompt.get("entry_ref") == review.get("entry_ref")
                    and prompt.get("controller_abstention") == self._work_abstention_context(
                        self.store.get(review["entry_ref"]), review["relationship_refs"])
                    and prompt.get("review_obligations", []) == obligations
                    and prompt.get("critique_contexts", []) == self._review_critique_contexts(
                        wid, entry_ref=review["entry_ref"], relationship_refs=review["relationship_refs"])
                    and reply.get("checks") == review.get("checks") and reply.get("rationale") == review.get("rationale"))
        except (KeyError, TypeError, ValueError, ValidationError):
            return False

    def _review_evidence_scope(self, wid, *, review=None):
        """Claim revisions share an allowance; changed evidence gets a new one."""
        if review is not None:
            if not self._review_protocol_matches(review):
                return None
            scope = review.get("evidence_scope")
            return scope if isinstance(scope, dict) and scope.get("review_protocol") == review["review_protocol"] else None
        targets = {relation["target"] for relation in self.relationships.values() if relation["source"] == wid}
        scope = {"review_protocol": source_fidelity_review_contract()["protocol"],
                "question": self.score["question"], "owner_basis": sorted(self.analyzed_basis[wid]),
                "targets": {target: sorted([
                    self.work_records[target]["artifact_ref"],
                    *([self.identity_records[target]["artifact_ref"]] if target in self.identity_records else []),
                    *[ref for ref, source in self.source_docs.items() if source["work_id"] == target]])
                    for target in sorted(targets)}}
        obligations = self._review_obligations_for(wid)
        full_text_windows = [{"source_ref": source["source_ref"], **source["window"]}
                             for source in self._work_review_sources(wid)
                             if source["representation"] == "full_text"]
        if full_text_windows:
            scope["full_text_windows"] = full_text_windows
        comparison_ids = self._review_comparison_ids(wid)
        if comparison_ids:
            scope["comparison_entries"] = {peer_id: self.analysis_records[peer_id]["artifact_ref"]
                                           for peer_id in sorted(comparison_ids)}
        if obligations:
            scope["review_obligations_sha256"] = hashlib.sha256(canonical_bytes(obligations)).hexdigest()
            scope["critique_context_protocol"] = _CRITIQUE_CONTEXT_PROTOCOL
        return scope

    def _work_review_failure_count(self, wid, feedback=None):
        """Count valid adverse reviews of this work's unchanged evidence basis."""
        scope = self._review_evidence_scope(wid)
        count = 0
        keys = self._review_failure_keys(feedback) if feedback is not None else None
        for version in self.store.versions(f"kb/work-reviews/{wid}"):
            review = self._body(self.store.get(f"artifact:kb/work-reviews/{wid}@{version}"))
            entry = self.store.get(review["entry_ref"])
            if scope != self._review_evidence_scope(wid, review=review):
                continue
            value = {key: review[key] for key in ("checks", "rationale")}
            try:
                validate_work_review(value, review["relationship_refs"], entry=self._body(entry),
                                     review_obligations=self._review_obligations_for(wid))
            except ValidationError:
                continue
            if keys is None or self._review_failure_keys(review) == keys:
                count += any(check["outcome"] != "passed" for check in review["checks"])
        for version in self.store.versions(f"command/work-review-repairs/{wid}"):
            ledger = self._body(self.store.get(f"artifact:command/work-review-repairs/{wid}@{version}"))
            if ledger.get("evidence_scope") != scope or not count:
                continue
            recorded_keys = ledger.get("failure_keys")
            if recorded_keys is None and keys is not None:
                recorded_keys = self._review_failure_keys(self._body(self.store.get(ledger["review_ref"])))
            if keys is None or recorded_keys == keys:
                count = max(count, ledger["repair_attempts"] + 1)
        return count

    def _reserve_work_review_repair(self, wid, feedback, operation):
        """An unchanged cached repair still consumes its scientific opportunity."""
        number = self._work_review_failure_count(wid, feedback)
        self._record(f"command/work-review-repairs/{wid}", "note", {
            "work_id": wid, "evidence_scope": self._review_evidence_scope(wid),
            "repair_attempts": number, "operation": operation,
            "failure_keys": self._review_failure_keys(feedback),
            "review_ref": feedback["review_ref"],
        }, "command.controller", subjects=[*self.analyzed_basis[wid], feedback["review_ref"]])

    def _exclude_unresolved_work(self, wid, feedback):
        previous = self.analysis_records[wid]
        self._materialize_source_less_map(wid, self.analyzed_basis[wid], scope="review_exhausted")
        self._record(f"kb/work-exclusions/{wid}", "note", {
            "work_id": wid, "retained_analysis_ref": previous["artifact_ref"],
            "review_ref": feedback["review_ref"], "failed_checks": feedback["checks"],
            "abstention_ref": self.analysis_records[wid]["artifact_ref"],
            "reason": "The bounded scientific review did not converge; prior work is retained but not admitted as evidence.",
        }, "command.controller", subjects=[previous["artifact_ref"], feedback["review_ref"],
                                           self.analysis_records[wid]["artifact_ref"]])

    def _work_abstention_context(self, entry_record, relationship_refs):
        entry = self._body(entry_record)
        abstention = self.store.head(f"command/survey-abstentions/{entry['work_id']}")
        if (relationship_refs or abstention is None or abstention["author"] != "command.controller"
                or not is_explicit_abstention(entry, self._body(abstention))
                or self._body(abstention).get("reason") != entry["reason"]
                or ABSTENTION_REASONS.get(self._body(abstention).get("scope")) != entry["reason"]):
            return None
        return {"ref": abstention["artifact_ref"], "body_hash": abstention["body_hash"],
                "body": self._body(abstention)}

    def _work_review_basis(self, wid):
        entry = self.analysis_records.get(wid)
        if entry is None:
            return None
        relations = [relation for relation in self.relationships.values() if relation["source"] == wid]
        comparison_ids = self._review_comparison_ids(wid)
        owners = {wid, *[relation["target"] for relation in relations], *comparison_ids}
        abstention = self._work_abstention_context(entry, [relation["artifact_ref"] for relation in relations])
        return [entry["artifact_ref"], *[relation["artifact_ref"] for relation in relations],
                *[self.analysis_records[peer_id]["artifact_ref"] for peer_id in sorted(comparison_ids)],
                *[ref for ref, source in self.source_docs.items() if source["work_id"] in owners],
                *([abstention["ref"]] if abstention is not None else [])]

    def _work_review_current(self, wid):
        review = self.work_reviews.get(wid)
        if (review is None or wid not in self.analyzed_basis or wid not in self.reviewed_basis
                or self.reviewed_basis[wid] != self._work_review_basis(wid)):
            return False
        return (self._review_evidence_scope(wid, review=self._body(review))
                == self._review_evidence_scope(wid))

    def _dispatch_work_reviews(self, jobs, contract_blocks):
        """One independent batch retains separate source-bound entry verdicts."""
        if self.score.get("design_brief") is None:
            return self._models_checked(jobs, stage="unit_review", task_kind="verification",
                on_contract_blocked=lambda name, error: contract_blocks.append(error))
        def assignment_for(entries):
            return {"phase": "implementation_evidence_review_batch",
                "assignment": "Audit the supplied short implementation context in one batch.",
                "entries": entries,
                "response_contract": {"top_level_fields": ["reviews"],
                    "reviews": "One object per exact work_id, with work_id plus that entry's exact response_contract fields."},
                "instructions": "Return only {reviews:[{work_id,checks,rationale}]} (include critique_adjudications only where the entry requires it). "
                    "Execute each entry's source-bound checks using its captured text. Keep each result to one short sentence. "
                    "This establishes source fidelity of implementation context, not novelty or experimental success. "
                    "Do not demand a complete bibliography, final optimized design or results before the first pilot."}

        expected_assignment = self._follow_up_assignment(assignment_for([job["assignment"] for job in jobs]))
        def settle(execution_ref):
            try:
                _, _, prompt, reply = self.gate._model_review_execution(execution_ref, "methods.work-reviewer")
            except ValidationError:
                return False
            if prompt.get("phase") != "implementation_evidence_review_batch":
                return False
            current_scope = {key: value for key, value in expected_assignment.items() if key != "entries"}
            prior_scope = {key: value for key, value in prompt.items() if key != "entries"}
            if self._response_assignment_identity(current_scope) != self._response_assignment_identity(prior_scope):
                return False
            try:
                rows = index_work_review_batch(prompt, reply)
            except ValidationError:
                return False
            entries = {item["entry"]["work_id"]: item for item in prompt["entries"]}
            admitted, invalid = [], {}
            for job in jobs:
                wid = job["assignment"]["entry"]["work_id"]
                if self._work_review_current(wid) or entries.get(wid) != job["assignment"] or wid not in rows:
                    continue
                try:
                    value = job["normalizer"]({key: item for key, item in rows[wid].items() if key != "work_id"})
                    job["validator"](value)
                except (ValidationError, TypeError, ValueError, KeyError) as exc:
                    invalid[wid] = str(exc)
                    continue
                job["on_valid"](value, execution_ref)
                admitted.append(wid)
            if admitted:
                execution = self.store.get(execution_ref)
                task_id = execution["artifact_id"].removeprefix("command/executions/")
                self._record(f"command/batch-settlements/{task_id}", "note", {
                    "execution_ref": execution_ref, "admitted_work_ids": admitted,
                    "missing_work_ids": sorted(set(entries) - set(rows)), "invalid_rows": invalid,
                    "scope": "Each retained row passed its own exact source-bound contract. Missing and invalid rows remain unresolved.",
                }, "command.controller", subjects=[execution_ref])
            return bool(admitted)

        if self.resume_session:
            receipts = self.control._conn.execute(
                "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE ? ORDER BY created_at DESC",
                ("command/executions/survey-implementation-evidence-review-%",)).fetchall()
            for row in receipts:
                execution_ref = row["artifact_ref"]
                execution = self.store.get(execution_ref)
                context = self._body(self.store.get(execution["inputs"][0]["ref"]))
                prompt = json.loads(context["prompt"])
                owned = self._retained_settled_response("implementation-evidence-review", "methods.work-reviewer",
                                                      prompt, execution_ref=execution_ref)
                if owned is not None:
                    settle(execution_ref)
            jobs = [job for job in jobs if not self._work_review_current(job["assignment"]["entry"]["work_id"])]
        if not jobs:
            return

        assignment = assignment_for([job["assignment"] for job in jobs])
        def normalize(value):
            rows = index_work_review_batch(assignment, value, require_complete=True)
            normalized = []
            for job in jobs:
                wid = job["assignment"]["entry"]["work_id"]
                try:
                    row = job["normalizer"]({key: item for key, item in rows[wid].items() if key != "work_id"})
                    job["validator"](row)
                except ValidationError as exc:
                    raise ValidationError(f"implementation evidence review {wid}: {exc}") from exc
                normalized.append({"work_id": wid, **row})
            return {"reviews": normalized}
        def validate(value):
            for job, row in zip(jobs, value["reviews"]):
                job["validator"]({key: item for key, item in row.items() if key != "work_id"})
        def integrate(value, execution):
            for job, row in zip(jobs, value["reviews"]):
                job["on_valid"]({key: item for key, item in row.items() if key != "work_id"}, execution)
        self._models_checked([{"name": "implementation-evidence-review", "actor": "methods.work-reviewer",
            "assignment": assignment, "normalizer": normalize, "validator": validate,
            "on_valid": integrate, "on_validation_failure": settle}],
            stage="unit_review", task_kind="verification",
            on_contract_blocked=lambda name, error: contract_blocks.append(error))

    def _review_work_claims(self):
        repair_rounds = self.config["limits"]["max_rounds"]
        while True:
            self._ensure_active()
            jobs, rejected, contract_blocks = [], [], []
            for wid, entry_record in self.analysis_records.items():
                if self.analyzed_basis.get(wid) != self._analysis_basis(wid):
                    if self._tree_admitted_reads is not None:
                        continue
                    raise StateError("focused review requires a completed current analysis for every mapped work")
                relations = [relation for relation in self.relationships.values() if relation["source"] == wid]
                refs = [relation["artifact_ref"] for relation in relations]
                obligations = self._review_obligations_for(wid)
                sources = self._work_review_sources(wid)
                basis = self._work_review_basis(wid)
                if self._work_review_current(wid):
                    continue
                entry = json.loads(self.store.read_body(entry_record["body_hash"]))
                abstention = self.store.head(f"command/survey-abstentions/{wid}")
                if (abstention and not refs and not self._review_obligations_for(wid)
                        and is_explicit_abstention(entry, self._body(abstention))):
                    checks = [{"check_id": check, "outcome": "passed", "method": "deterministic abstention integrity",
                               "result": "No substantive statement or relationship is admitted; the recorded limitation remains explicit."}
                              for check in work_review_checks([])]
                    record = self._record(f"kb/work-reviews/{wid}", "note", {
                        "entry_ref": entry_record["artifact_ref"], "relationship_refs": [],
                        "review_protocol": source_fidelity_review_contract()["protocol"],
                        "evidence_scope": self._review_evidence_scope(wid),
                        "verification_kind": "deterministic_abstention",
                        "execution_ref": abstention["artifact_ref"], "checks": checks,
                        "rationale": "Contract-only verification of an explicit abstention, not scientific support.",
                    }, "methods.work-reviewer", subjects=basis)
                    self.work_reviews[wid] = record
                    self.reviewed_basis[wid] = basis
                    continue
                assignment = {
                    "phase": "work_review", "assignment": "Independently audit the entailment of each individual literature claim.",
                    "review_contract": source_fidelity_review_contract(),
                    "question": self.score["question"], "entry_ref": entry_record["artifact_ref"],
                    "entry": entry, "relationship_refs": refs, "relationships": relations,
                    "relationship_semantics": RELATIONSHIP_SEMANTICS,
                    "sources": sources, "required_checks": list(work_review_checks(refs, obligations)),
                    "allowed_check_outcomes": ["passed", "failed", "insufficient_evidence", "check_failed"],
                    "response_contract": {
                        "top_level_fields": ["checks", "rationale"],
                        "checks": [{"check_id": key,
                                    "required_fields": ["check_id", "outcome", "method", "result",
                                        *(["affected_check_ids"] if key.startswith("critique:") else [])]}
                                   for key in work_review_checks(refs, obligations)],
                        "outcome_enum": ["passed", "failed", "insufficient_evidence", "check_failed"],
                        "text_fields": ["method", "result", "rationale"],
                        "affected_check_ids": "Only critique rows contain this field: [] if passed, otherwise a nonempty list of affected ordinary check IDs whose outcomes are non-passed.",
                    },
                    "instructions": "Return one JSON object with exactly the fields and row structures specified in response_contract. "
                        "The checks value must be an array, never an object keyed by check ID. The top-level rationale string is required. "
                        "Ordinary field and relationship checks use check_id, outcome, method, result only. "
                        "Return only this final JSON object, without preamble or extra fields. "
                        "Run each ordinary check separately; outcome is passed/failed/insufficient_evidence/check_failed. "
                        "Judge whether the supplied text entails the ENTIRE claim, not whether its quotation merely exists or the topic sounds plausible. "
                        "A passed check requires support for every clause. Fail unsupported minor clauses too; a correct main point does not excuse them. "
                        "For limitations require an explicit source statement; reject a claim about missing evaluation or excluded scope inferred only from an abstract's silence. "
                        "An abstract's silence cannot establish what a full paper did not evaluate, include, compare, or generalize. "
                        "Attention-based does not entail attention-only or absence of recurrence/convolution. "
                        "A newer date, citation, shared terminology, or similar application does not establish extension, conceptual inheritance, or superiority. "
                        "For a claimed extends relationship require text establishing the specific dependency; otherwise fail it and identify the unsupported part. "
                        "Do not fill missing text from model memory or titles. A proceedings preface is not architectural research evidence. "
                        "Check included or scientifically excluded entries against actual scope and source content. "
                        "Uncertain is non-admission, not evidence of irrelevance or unavailable sources. "
                        "For a supplied controller_abstention, verify the procedural reason against that hash-bound receipt; no article quotation establishes controller status. "
                        "Statement {text:null,evidence:[]} means no fact is known or asserted for that field. "
                        "It never asserts absence of limitations, problems, approaches, or findings in the paper. "
                        "For such a field verify the empty nonassertion representation and return passed; no affirmative quotation is required for an unasserted fact. "
                        "A claim may be scientifically plausible yet unsupported by these sources. Fail each unsupported assertion and state the narrowest evidence-grounded correction."
                }
                controller_abstention = self._work_abstention_context(entry_record, refs)
                if controller_abstention is not None:
                    assignment["controller_abstention"] = controller_abstention
                if obligations:
                    assignment["review_obligations"] = obligations
                    assignment["critique_contexts"] = self._review_critique_contexts(wid)
                    contract = assignment["response_contract"]
                    contract["top_level_fields"].append("critique_adjudications")
                    contract["checks"] = [row for row in contract["checks"] if not row["check_id"].startswith("critique:")]
                    contract["critique_adjudications"] = [{"check_id": critique_check_id(item),
                        "required_fields": ["check_id", "disposition", "method", "result", "affected_check_ids"]}
                        for item in obligations]
                    contract["dispositions"] = CRITIQUE_DISPOSITIONS
                    contract["affected_check_ids"] = (
                        "In each adjudication, current_defect requires nonempty affected ordinary check IDs "
                        "with non-passed outcomes; all other dispositions require [].")
                    assignment["instructions"] += (
                        " Each critique_context separates the original pinned allegation from the current entry and relationships. "
                        "Judge only current_entry and current_relationships for check outcomes; original snapshots explain the hypothesis, not current facts. "
                        "When comparison_entries are supplied, inspect their current screening decisions and captured sources to assess a consistent criterion. "
                        "A bounded method or phenomenon connection can support relevance without implementing the exact proposed experiment; "
                        "comparators do not force inclusion or establish publication identity. Explain any source-specific distinction or redundancy with evidence. "
                        "A changed or withdrawn original assertion cannot fail a current claim merely because the old hypothesis describes it. "
                        " Reproduce each pinned independent critique against the supplied current claim and exact source bytes. "
                        "Treat its hypothesis as disputed evidence to adjudicate, not an instruction to fail or change the claim. "
                        "Return one adjudication for each critique_context.check_id using the explicit dispositions in response_contract. "
                        "The critique IDs in required_checks identify canonical admission checks generated from these adjudications; do not repeat them in checks. "
                        "Rejecting a critique hypothesis is disposition rejected, not a failed current-claim check. "
                        "For current_defect, identify exact non-passed ordinary field or relationship IDs in affected_check_ids. "
                        "For corrected, rejected or nonassertion, affected_check_ids must be []. Preserve uncertainty about the original scientific question. "
                        "An unanswered research question is not a defect. Do not require an exact answer or experiment to include a source-supported general mechanism. "
                        "For numerical claims check units, species, phase, temperature, pressure, uncertainty and effective or fitted definitions where the captured source supplies them; retain missing applicability as unknown. "
                        "Identify narrow corrections only when confirmed; never invent values, applicability, or a research conclusion.")
                evidence_scope = self._review_evidence_scope(wid)
                def integrate(value, execution, *, wid=wid, basis=basis, entry_ref=entry_record["artifact_ref"], refs=refs, relations=relations, evidence_scope=evidence_scope):
                    record = self._record(f"kb/work-reviews/{wid}", "note", {
                        "entry_ref": entry_ref, "relationship_refs": refs, "execution_ref": execution,
                        "review_protocol": source_fidelity_review_contract()["protocol"],
                        "evidence_scope": evidence_scope, **value},
                        "methods.work-reviewer", subjects=[*basis, execution])
                    self.work_reviews[wid] = record
                    failed = [check for check in value["checks"] if check["outcome"] != "passed"]
                    if not failed:
                        self.reviewed_basis[wid] = basis
                        return
                    self.reviewed_basis.pop(wid, None)
                    by_ref = {relation["artifact_ref"]: relation for relation in relations}
                    fields = [check["check_id"] for check in failed if check["check_id"] in {"inclusion", "reason", *MAP_FIELDS}]
                    targets = sorted({by_ref[check["check_id"][len("relationship:"):]]["target"]
                                      for check in failed if check["check_id"].startswith("relationship:")})
                    rejected.append((wid, {"review_ref": record["artifact_ref"], "entry_fields": fields,
                                           "relationship_refs": [check["check_id"][len("relationship:"):] for check in failed
                                                                 if check["check_id"].startswith("relationship:")],
                                           "relationship_targets": targets, "checks": failed, "rationale": value["rationale"]}))

                jobs.append({"name": f"work-review-{wid}", "actor": "methods.work-reviewer", "assignment": assignment,
                             "normalizer": lambda value, refs=refs, obligations=obligations: normalize_check_envelope(
                                 value, work_review_checks(refs, obligations)),
                             "validator": lambda value, refs=refs, entry=entry, obligations=obligations: validate_work_review(
                                 value, refs, entry=entry, review_obligations=obligations),
                             "on_valid": integrate})
            if jobs:
                self._dispatch_work_reviews(jobs, contract_blocks)
            if not rejected:
                if contract_blocks:
                    raise contract_blocks[0]
                return
            failure_counts = {wid: self._work_review_failure_count(wid, feedback) for wid, feedback in rejected}
            exhausted = [(wid, feedback) for wid, feedback in rejected
                         if (failure_counts[wid] > repair_rounds
                             or self._work_review_exhausted(wid, feedback))]
            if exhausted:
                for wid, feedback in exhausted:
                    abstention = self.store.head(f"command/survey-abstentions/{wid}")
                    if (self._review_obligations_for(wid) and abstention
                            and is_explicit_abstention(self._body(self.analysis_records[wid]), self._body(abstention))):
                        raise ModelWorkBlocked(
                            f"independent critique for {wid} remains unresolved after substantive review",
                            failure_class="scientific_review")
                    self._exclude_unresolved_work(wid, feedback)
                self._map()
                excluded_ids = {wid for wid, _ in exhausted}
                rejected = [(wid, feedback) for wid, feedback in rejected if wid not in excluded_ids]
                if not rejected:
                    self._review_work_claims()
                    return
            withdrawals = [(wid, feedback) for wid, feedback in rejected if failure_counts[wid] == repair_rounds]
            revisions = [(wid, feedback) for wid, feedback in rejected if failure_counts[wid] < repair_rounds]
            if withdrawals:
                # Stop trying to invent a stronger statement from unchanged
                # sources. Withdraw disputed claims, then independently check
                # the narrowed map before accepting it.
                for wid, feedback in withdrawals:
                    self._reserve_work_review_repair(wid, feedback, "withdraw_disputed_assertions")
                    job = self._map_job(wid, self.analyzed_basis[wid], review_feedback=feedback)
                    updates = {}
                    for field in feedback["entry_fields"]:
                        updates[field] = ({"text": None, "evidence": []} if field in MAP_FIELDS else
                                          "uncertain" if field == "inclusion" else
                                          "The captured evidence does not resolve the screening rationale.")
                    value = {"entry_updates": updates, "relationships": []}
                    value = job["normalizer"](value)
                    job["withdrawal_validator"](value)
                    record = self._publish(f"kb/claim-withdrawals/{wid}", "note", {
                        "review_ref": feedback["review_ref"], "withdrawn_fields": _map_withdrawn_fields(feedback["entry_fields"]),
                        "withdrawn_relationship_targets": feedback["relationship_targets"],
                    }, "command.controller", subjects=[feedback["review_ref"]])
                    job["on_valid"](value, record["artifact_ref"])
            if revisions:
                for wid, feedback in revisions:
                    self._reserve_work_review_repair(wid, feedback, "revise_disputed_assertions")
                self._models_checked([self._map_job(wid, self.analyzed_basis[wid], review_feedback=feedback)
                                      for wid, feedback in revisions], stage="revision")
            self._map()

    def _survey_repair_scope(self):
        assertions = {
            "entries": {wid: self._body(record) for wid, record in self.analysis_records.items()},
            "relationships": sorted(
                [{key: value for key, value in relation.items() if key != "artifact_ref"}
                 for relation in self.relationships.values()], key=canonical_bytes),
        }
        return {"protocol": "literature-survey-repair-2", "question": self.score["question"],
                "sources": sorted(self.source_docs),
                "analysis_basis": {wid: sorted(self._analysis_basis(wid)) for wid in sorted(self.work_records)},
                "assertions_sha256": hashlib.sha256(canonical_bytes(assertions)).hexdigest(),
                "review_obligations": self.review_obligations,
                "critique_context_protocol": _CRITIQUE_CONTEXT_PROTOCOL,
                "review_context_protocol": CURRENT_MAP_REVIEW_PROTOCOL,
                "review_contract": source_fidelity_review_contract()}

    def _repair_survey_review(self, review_record):
        """Route aggregate criticisms through the same scoped mapper contract."""
        review = self._body(review_record)
        entries = {wid: record["artifact_ref"] for wid, record in self.analysis_records.items()}
        relations = {value["artifact_ref"]: value for value in self.relationships.values()}
        permitted_fields, permitted_relations = {}, set()
        for finding in review.get("findings", []):
            if finding["target_ref"] in relations:
                permitted_relations.add(finding["target_ref"])
            else:
                permitted_fields.setdefault(finding["target_ref"], set()).add(finding["field"])
        scope = self._survey_repair_scope()
        digest = hashlib.sha256(canonical_bytes(scope)).hexdigest()
        logical = "command/survey-review-repairs/" + digest
        retained = self.store.head(logical)
        rounds = self._body(retained)["rounds"] if retained else 0
        if rounds >= self.config["limits"]["max_rounds"]:
            raise ModelWorkBlocked("survey review did not pass every required check after scoped repairs",
                                   failure_class="scientific_review")
        assignment = {
            "phase": "survey_repair_plan",
            "assignment": "Locate the current claims implicated by the failed aggregate review and grant narrow repairs.",
            "question": self.score["question"], "review_ref": review_record["artifact_ref"], "review": review,
            "map": {**self._map_body(), "entries": [
                {**self._body(record), "artifact_ref": record["artifact_ref"]}
                for record in self.analysis_records.values()]}, "entry_refs": entries,
            "review_contract": source_fidelity_review_contract(),
            "sources": self._survey_review_packet()["sources"],
            "review_context_protocol": CURRENT_MAP_REVIEW_PROTOCOL,
            "repair_target_catalog": [{"entry_ref": ref,
                "entry_fields": sorted(permitted_fields.get(ref, set())),
                "relationship_refs": sorted(target for target in permitted_relations
                    if relations[target]["source"] == wid)} for wid, ref in entries.items()
                if ref in permitted_fields or any(relations[target]["source"] == wid for target in permitted_relations)],
            "instructions": "Return exactly {repairs:[{entry_ref,entry_fields,relationship_refs,rationale}]}. "
                "Each entry_ref must be a current supplied entry; entry_fields is a list drawn from inclusion, reason, problem, approach, finding, limitations. "
                "Copy the artifact_ref value, not the work_id key, for entry_ref. A known work ID can be resolved only to its supplied current entry; an explicit stale artifact_ref is never advanced. "
                "relationship_refs must be exact current outgoing relationship refs for that entry. "
                "Grant only the fields and relationships implicated by a concrete failed check; preserve all other assertions. "
                "Scientific grants must be a subset of review.findings target_ref/field bindings. A relationship grant belongs to that relationship's source work. Coverage-only diagnoses may return no claim repairs. "
                "Choose only entry_ref, entry_fields, and relationship_refs from repair_target_catalog. "
                "Finding quote_field locates evidence and does not grant edits; a reason quote under field=inclusion permits only inclusion edits. "
                "The failed review is a disputed diagnosis, not proof. Compare its concrete allegations against the supplied current map; do not treat a withdrawn historical assertion as current. "
                "If no current claim needs correction, return repairs:[] and let an independent aggregate reviewer reconsider the current packet. "
                "A repair grant permits the mapper to narrow, substantiate, or withdraw the disputed assertion; it does not prescribe a scientific verdict."
        }
        def validate(value):
            exact(value, {"repairs"}, "aggregate review repair plan")
            if not isinstance(value["repairs"], list):
                raise ModelContractError("aggregate review repairs must be a list")
            seen = set()
            by_ref = {ref: wid for wid, ref in entries.items()}
            scope_errors = []
            for index, item in enumerate(value["repairs"]):
                exact(item, {"entry_ref", "entry_fields", "relationship_refs", "rationale"}, "aggregate review repair")
                ref = item["entry_ref"]
                if not isinstance(ref, str) or ref not in by_ref or ref in seen:
                    raise ModelContractError("aggregate repair must pin one unique current entry")
                seen.add(ref)
                fields, refs = item["entry_fields"], item["relationship_refs"]
                if (not isinstance(fields, list) or not all(isinstance(field, str) for field in fields)
                        or len(set(fields)) != len(fields) or not set(fields) <= {"inclusion", "reason", *MAP_FIELDS}
                        or not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs)
                        or len(set(refs)) != len(refs) or not fields and not refs):
                    raise ModelContractError("aggregate repair requires explicit unique scoped fields or relationships")
                if any(ref not in relations or relations[ref]["source"] != by_ref[item["entry_ref"]] for ref in refs):
                    raise ModelContractError("aggregate repair relationship has a different owner or version")
                if not set(fields) <= permitted_fields.get(ref, set()) or not set(refs) <= permitted_relations:
                    scope_errors.append(f"repairs[{index}] entry_ref={ref!r}: aggregate repair must bind the rejected review's exact current findings; "
                        f"requested entry_fields={fields!r}, allowed entry_fields={sorted(permitted_fields.get(ref, set()))!r}; "
                        f"requested relationship_refs={refs!r}, allowed relationship_refs="
                        f"{sorted(target for target in permitted_relations if relations[target]['source'] == by_ref[ref])!r}. "
                        "quote_field identifies supporting text and grants no additional field authority.")
                if not isinstance(item["rationale"], str) or not item["rationale"].strip():
                    raise ModelContractError("aggregate repair needs a concrete diagnosis")
            if scope_errors:
                raise ModelContractError("\n".join(scope_errors))
        value, execution = self._model_checked("survey-repair-plan", "research.literature-mapper",
                                              assignment, validate, stage="revision", task_kind="selection",
                                              normalizer=lambda value: normalize_survey_repair_owners(value, entries, relations))
        plan = self._record("kb/survey-review-repair-plan", "decision_note", {
            "review_ref": review_record["artifact_ref"], "execution_ref": execution, **value,
        }, "research.literature-mapper", subjects=[review_record["artifact_ref"], execution, *entries.values(), *relations])
        self._record(logical, "note", {"scope": scope, "rounds": rounds + 1,
                     "review_ref": review_record["artifact_ref"], "plan_ref": plan["artifact_ref"]},
                     "command.controller", subjects=[review_record["artifact_ref"], plan["artifact_ref"]])
        jobs = []
        for item in value["repairs"]:
            wid = self._body(self.store.get(item["entry_ref"]))["work_id"]
            feedback = {"review_ref": review_record["artifact_ref"], "entry_fields": item["entry_fields"],
                        "relationship_targets": sorted({relations[ref]["target"] for ref in item["relationship_refs"]}),
                        "relationship_refs": item["relationship_refs"],
                        "checks": [check for check in review["checks"] if check["outcome"] != "passed"],
                        "rationale": item["rationale"]}
            jobs.append(self._map_job(wid, self.analyzed_basis[wid], review_feedback=feedback))
        if jobs:
            self._models_checked(jobs, stage="revision")
            self._map()
        return {"review_ref": review_record["artifact_ref"], "plan_ref": plan["artifact_ref"],
                "repairs": value["repairs"], "current_entry_refs": {
                    wid: record["artifact_ref"] for wid, record in self.analysis_records.items()}}

    def _retry_retained_survey_admission(self):
        """Retry deterministic admission of an unchanged, paid, passing review."""
        scopes = set((self.resume_session or {}).get("reopened_scopes", []))
        if "integrated_review" not in scopes or scopes - {"integrated_review", "assessment"}:
            return False
        bundle = self.store.head("kb/surveys/current")
        if bundle is None or self.map_record is None:
            return False
        accepted = self.store.accepted(bundle["artifact_id"])
        if accepted is not None and accepted["version"] == bundle["version"]:
            return False
        body = self._body(bundle)
        expected = {
            "score_ref": self.score_ref, "map_ref": self.map_record["artifact_ref"],
            "work_refs": sorted(record["artifact_ref"] for record in self.work_records.values()),
            "source_refs": sorted(self.source_docs), "query_refs": sorted(self.query_refs),
            "work_review_refs": sorted(record["artifact_ref"] for record in self.work_reviews.values()),
        }
        for key, value in expected.items():
            actual = body.get(key)
            if (sorted(actual) if isinstance(actual, list) else actual) != value:
                return False
        rows = self.control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE 'kb/survey-reviews/%' "
            "ORDER BY created_at DESC LIMIT 1").fetchall()
        if not rows:
            return False
        review = self.store.get(rows[0]["artifact_ref"])
        response = self._body(review)
        checks = response.get("checks", [])
        if (review.get("author") != "methods.survey-reviewer" or response.get("survey_ref") != bundle["artifact_ref"]
                or not checks or any(check.get("outcome") != "passed" for check in checks)):
            return False
        adopted = self.gate.accept(bundle["artifact_ref"], review["artifact_ref"],
            author="strategy.survey-integrator", expected_version=accepted["version"] if accepted else None,
            guard=self._admission_guard)
        self._record_survey_admission(adopted)
        return True

    def _accept_survey(self):
        if self._retry_retained_survey_admission():
            return
        response = None
        while True:
            rejected = self._accept_survey_once(response)
            if rejected is None:
                return
            response = self._repair_survey_review(rejected)

    def _accept_survey_once(self, prior_review_response=None):
        self._survey_acceptance_pending = True
        if not self.works:
            raise ValidationError("no works were captured; a survey cannot be fabricated")
        self._reconcile_identities()
        self._map()
        self._review_work_claims()
        if not any(self._body(record)[field]["text"] is not None
                   for record in self.analysis_records.values() for field in MAP_FIELDS):
            budget_abstentions = []
            for wid, record in self.analysis_records.items():
                abstention = self.store.head(f"command/survey-abstentions/{wid}")
                if (abstention and self._body(abstention).get("scope") == "model_call_budget"
                        and is_explicit_abstention(self._body(record), self._body(abstention))):
                    budget_abstentions.append({"work_id": wid, "entry_ref": record["artifact_ref"],
                        "abstention_ref": abstention["artifact_ref"], "debt_refs": [gap["debt_ref"] for gap in self.gaps
                            if gap.get("kind") == "model_call_budget" and gap.get("work_id") == wid and gap.get("debt_ref")]})
            if budget_abstentions and len(budget_abstentions) == len(self.analysis_records):
                raise QuotaExceededError("required downstream model-call reservations prevented every source claim extraction",
                    dimension="max_model_calls", limit=self.config["limits"].get("max_model_calls"),
                    observed=self.model_calls_dispatched, diagnostics=[{"budget_abstentions": budget_abstentions,
                        "required_jobs": self._required_model_work()}])
            raise ModelWorkBlocked("No substantive literature claim survived bounded extraction; all entries remain explicit abstentions")
        coverage = self._record("kb/coverage", "coverage_report", self._coverage(), "command.search-coordinator",
                                subjects=[self.register_ref, *self.query_refs])
        dependencies = [self.score_ref, self.protocol["artifact_ref"], self.map_record["artifact_ref"], coverage["artifact_ref"],
            self.register_ref, *([self.store.head("kb/exploration-tree")["artifact_ref"]]
                                if self.exploration_tree is not None else []),
            *[r["artifact_ref"] for r in self.work_records.values()], *self.source_docs, *self.query_refs,
            *[r["artifact_ref"] for r in self.identity_records.values()],
            *[ref for r in self.identity_records.values() for ref in self._body(r).get("observation_refs", [])],
            *[self._body(r)["lookup_execution_ref"] for r in self.identity_records.values()
              if self._body(r).get("lookup_execution_ref")],
            *[r["artifact_ref"] for r in self.analysis_records.values()], *[r["artifact_ref"] for r in self.relationships.values()],
            *[ref for wid in self.work_reviews for ref in self._work_review_basis(wid)],
            *[r["artifact_ref"] for r in self.work_reviews.values()],
            *[json.loads(self.store.read_body(r["body_hash"]))["execution_ref"] for r in self.work_reviews.values()],
            *[source["execution_ref"] for source in self.source_docs.values()]]
        body = {"schema_version": "literature-survey-3", "score_ref": self.score_ref, "protocol_ref": self.protocol["artifact_ref"],
                "map_ref": self.map_record["artifact_ref"], "coverage_ref": coverage["artifact_ref"],
                "source_refs": sorted(self.source_docs), "work_refs": sorted(r["artifact_ref"] for r in self.work_records.values()),
                "query_refs": list(self.query_refs),
                "identity_refs": sorted(r["artifact_ref"] for r in self.identity_records.values()),
                "dependency_refs": sorted(set(dependencies))}
        body["work_review_refs"] = sorted(r["artifact_ref"] for r in self.work_reviews.values())
        local_critiques = self.gate.independent_review_obligations(self.score["question"])
        body["dependency_refs"] = sorted(set(body["dependency_refs"]) | {item["receipt_ref"] for item in local_critiques})
        bundle = self._record("kb/surveys/current", "note", body, "research.literature-mapper", subjects=body["dependency_refs"])
        self.survey_revision += 1
        review_packet = self._survey_review_packet()
        review_assignment = {
            "assignment": "Independently check this exact survey, including honest reporting of incomplete coverage.",
            "phase": "survey_review", "survey_ref": bundle["artifact_ref"], "question": self.score["question"],
            "map": review_packet["map"], "coverage": review_packet["coverage"],
            "sources": review_packet["sources"],
            "focused_review_summary": review_packet["focused_review_summary"],
            "deterministic_integrity": review_packet["deterministic_integrity"],
            "review_contract": review_packet["review_contract"],
            "response_contract": survey_review_response_contract(review_packet["map"], indexed=True),
            "relationship_semantics": RELATIONSHIP_SEMANTICS,
            "required_checks": sorted(SURVEY_CHECKS),
            "allowed_check_outcomes": ["passed", "failed", "insufficient_evidence", "check_failed"],
            "instructions": "Return exactly one JSON object {checks:[{check_id,outcome,method,result}],rationale,findings:[{check_id,assertion_id,rationale}]}; no preamble, markdown, or analysis transcript. Execute exactly the required checks. "
                "For each non-passed source-fidelity or map-support check supply at least one finding selecting an exact assertion_id from response_contract.assertion_catalog. The catalog indexes the current target and affected field; read its complete assertion text in map at target_ref/quote_field. Do not reconstruct quotations or source references. Explain the concrete defect against the captured sources. Selecting an item locates your allegation and grants only its named field; it does not prove the allegation. Reassess the whole current assertion and withdraw a diagnosis that its actual qualifiers contradict. Do not invent a current statement or screening status. Passed checks have no findings. Coverage-accounting may be explained in its check result. "
                "Outcomes passed/failed/insufficient_evidence/check_failed. Passing approves a faithful bounded survey, not novelty or exhaustive coverage. "
                "Check accurate coverage/accounting, faithful quotations and source scope, and support for every asserted map claim. "
                "The question is a hypothesis for later investigation, not a claim that this survey must prove or disprove. "
                "A lack of an answer to it is not a failed map-support check or source-fidelity failure when all retained map assertions are supported. Source-fidelity assesses the truth and scope of existing assertions, not whether this corpus answers the research question; an unanswered question belongs to downstream gap nomination and counter-search. Use insufficient_evidence for source-fidelity only when support for a retained assertion itself cannot be determined from its cited captured source window. Use the explicit count_definitions rather than equating different counters. The deterministic_integrity block is a controller-computed checksum of the exact packet: treat its endpoint and count booleans as authoritative for packet accounting, and do not report a missing map entry when the corresponding boolean is true. "
                "Independently inspect the supplied captured source windows and exact cited spans for every clause, relationship qualifier, and inclusion decision. "
                "The focused-review references are provenance only; do not inherit their positive verdicts or treat reference integrity as entailment. "
                "Every included work requires a source-supported connection to the declared question mechanism, phenomenon, or method, even when it does not answer the question. "
                "Null or withdrawn fields do not need content-level support for an absent assertion. "
                "A partial withdrawal exempts only its absent fields; any surviving assertion still requires source support. "
                "Included, excluded and uncertain are screening states, not assertion states: audit every non-null scientific field in all three states. "
                "Unknown facts must stay unknown. Unverified provider metadata is not itself a false assertion if explicitly labeled; "
                "fail unsupported chronology or superiority inferred from it. The map entries and sources are deliberate claim-bearing projections; use their projection counts and deterministic_integrity rather than recounting omitted rows. "
                "Keep each method/result to one short sentence and rationale under 120 words; cite specific problems instead of enumerating the corpus."
        }
        if prior_review_response is not None:
            review_assignment["prior_review_receipts"] = {key: prior_review_response[key] for key in ("review_ref", "plan_ref")}
            review_assignment["instructions"] += (
                " A prior rejected review has been routed through a scoped correction plan. "
                "Independently inspect the exact current map; do not repeat a historical defect that the current claims no longer assert. "
                "The repair plan and prior reviewer are provenance, not proof of either support or failure.")
        if self.review_obligations:
            review_assignment["independent_critique_receipts"] = sorted({item["receipt_ref"] for item in self.review_obligations})
            review_assignment["instructions"] += (
                " Independent critique receipts have been adjudicated by separate, source-bound focused reviews. "
                "Their targets and captured source windows remain visible in this packet. "
                "Independently judge the exact current claims and source bytes; focused verdicts and receipt identities are not scientific support. "
                "Report a current unsupported clause rather than inferring one from a historical allegation.")
        if (self.resume_session
                and "integrated_review" in self.resume_session.get("reopened_scopes", [])):
            session = self.resume_session.get("session")
            if type(session) is int and session >= 1:
                review_assignment["resume_boundary"] = f"survey-review-resume-{session}"
            review_assignment["instructions"] += (
                " This is a focused re-review of the same retained map, not a new literature search. "
                "Do not treat missing full text, incomplete pagination, or an unresolved question as "
                "a source-fidelity failure when every included assertion remains supported by its "
                "captured source and has an evidenced connection to the declared question. Independently review the source bytes; prior verdicts are not support. Report a concrete unsupported assertion, "
                "identity conflict in an included work, or accounting inconsistency if one exists; "
                "otherwise pass the map and retain corpus limits as explicit coverage limitations."
            )
        limit = self._map_input_limit("methods.survey-reviewer")
        if limit is not None and estimate_input_tokens(SYSTEM, json.dumps(self._follow_up_assignment(review_assignment), ensure_ascii=False)) > limit:
            raise ValidationError("integrated survey review cannot fit all mandatory source evidence within its configured input budget")
        value, execution = self._model_checked(
            "survey-review", "methods.survey-reviewer", review_assignment,
            lambda value: validate_survey_review(value, current_map=review_packet["map"]),
            normalizer=lambda value: normalize_survey_review_envelope(value, current_map=review_packet["map"]),
            model_overrides={"temperature": 0.1},
            stage="unit_review", task_kind="verification")
        review_body = {"survey_ref": bundle["artifact_ref"], "execution_ref": execution, **value}
        review = self._publish(f"kb/survey-reviews/{self.survey_revision}", "note", review_body,
                               "methods.survey-reviewer", subjects=[bundle["artifact_ref"], execution])
        if any(check["outcome"] != "passed" for check in value["checks"]):
            return review
        accepted = self.store.accepted(bundle["artifact_id"])
        adopted = self.gate.accept(bundle["artifact_ref"], review["artifact_ref"], author="strategy.survey-integrator",
                                  expected_version=accepted["version"] if accepted else None, guard=self._admission_guard)
        self._record_survey_admission(adopted)

    def _record_survey_admission(self, adopted):
        self.survey_ref = adopted["artifact_ref"]
        self._survey_acceptance_pending = False
        self.incumbent = self.survey_ref
        self.time_policy.mark_first_verified_result(self.survey_ref)
        self.verified_changes.append({"kind": "accepted_literature_survey", "ref": self.survey_ref})
        self._checkpoint("survey_accepted", force=True)

    def _admission_guard(self):
        self._ensure_active()

    def _nominate(self):
        self.gate.require_current(self.survey_ref)
        if self.score["proposed_gap"] is not None:
            self.nomination = deepcopy(self.score["proposed_gap"])
        else:
            def validate(value):
                exact(value, {"id", "statement"}, "gap nomination")
                identifier(value["id"])
                if not isinstance(value["statement"], str) or not value["statement"].strip():
                    raise ValidationError("nomination needs an explicit bounded statement")
            self.nomination, _ = self._model_checked("nominate", "research.gap-proposer", {
                "phase": "nomination", "assignment": "Nominate one bounded and testable research-gap hypothesis from the accepted map.",
                "question": self.score["question"], "map": self._map_body(), "coverage": self._coverage(),
                "survey_ref": self.survey_ref, "prerequisite_survey_ref": self.survey_ref,
                "instructions": "Return {id:lowercase_identifier,statement:string}. This is a hypothesis to challenge, not an established novelty claim."
            }, validate, normalizer=normalize_gap_nomination, task_kind="selection")
        self.nomination_record = self._publish("kb/gap-nomination", "note", {
            "survey_ref": self.survey_ref, **self.nomination}, "research.gap-proposer", subjects=[self.survey_ref])

    def _counter_plan(self):
        self._refresh_countersearch_state()
        if self.counter_plan_record is not None:
            body = self._body(self.counter_plan_record)
            return {key: body[key] for key in ("queries", "rationale")}, self.counter_plan_record
        self._require_follow_up_catalog_capacity()
        plan, execution = self._model_checked("counter-plan", "methods.novelty-challenger", {
            "assignment": "Find searches most likely to disprove the nominated gap by locating an existing solution or alternate terminology.",
            "phase": "counter_plan", "question": self.score["question"], "gap": self.nomination,
            "map": self._map_body(), "max_queries": self.bounds["queries_per_role"],
            "response_contract": search_plan_response_contract(self.bounds["queries_per_role"]),
            "search_syntax": SEARCH_SYNTAX,
            "nomination_ref": self.nomination_record["artifact_ref"],
            "survey_ref": self.survey_ref, "prerequisite_survey_ref": self.survey_ref,
            "instructions": "Return {queries:[search strings],rationale:string}. Seek prior solutions, incompatible assumptions, and decisive counterevidence."
        }, self._plan_validator, stage="supervision", task_kind="selection")
        record = self._publish("kb/counter-search-plan", "note", {
            **plan, "survey_ref": self.survey_ref,
            **({"follow_up_ref": self.follow_up_ref} if self.work_orders else {}),
            "nomination_ref": self.nomination_record["artifact_ref"],
        }, "methods.novelty-challenger", subjects=[execution, self.survey_ref,
                                                   self.nomination_record["artifact_ref"]])
        self.counter_plan_record = record
        return plan, record

    def _countersearch(self):
        self._countersearch_active = True
        plan, record = self._counter_plan()
        self._search(plan["queries"], "methods.novelty-challenger", record["artifact_ref"],
                     admission="challenge")
        self._complete_search_pages(admission="challenge")
        self._full_texts()
        self._refresh_countersearch_state()
        self._record(self._counter_acquisition_id(), "note", {
            "schema_version": "counter-search-acquisition-1",
            "question": self.score["question"], "plan_ref": record["artifact_ref"],
            "nomination_ref": self.nomination_record["artifact_ref"],
            "query_refs": self.counter_query_refs, "source_refs": sorted(self.source_docs),
        }, "command.controller", subjects=[record["artifact_ref"], *self.counter_query_refs,
                                             *sorted(self.source_docs)])
        self._accept_survey()
        self._refresh_countersearch_state()
        self._countersearch_active = False
        if not self.countersearch_complete:
            raise StateError("counter-search completion could not be bound to the accepted survey")

    def _assess(self):
        assessment_sources = self._assessment_source_context()
        resume_gap_assessment = bool(
            self.resume_session
            and "gap_assessment" in self.resume_session.get("reopened_scopes", [])
        )
        assessment_assignment = {
            "assignment": "Independently determine the status of the nominated gap using the current accepted survey and targeted counter-search.",
            "phase": "gap_assessment", "question": self.score["question"], "gap": self.nomination,
            "nomination_ref": self.nomination_record["artifact_ref"],
            "survey_ref": self.survey_ref, "prerequisite_survey_ref": self.survey_ref,
            "map": self._assessment_map_context(), "coverage": self._coverage(),
            "sources": assessment_sources,
            "verified_full_text_refs": [source["source_ref"] for source in assessment_sources
                                        if source["representation"] == "full_text"
                                        and source.get("identity_verified") is True],
            "required_checks": sorted(GAP_CHECKS),
            "allowed_check_outcomes": ["passed", "failed", "insufficient_evidence", "check_failed"],
            "instructions": _GAP_ASSESSMENT_INSTRUCTIONS,
        }
        if resume_gap_assessment:
            # A Composer continuation is an explicit new assessment attempt.
            # Keep the accepted survey and source catalogue, but give the
            # model-work cache a new assignment identity so a previous
            # evidence-contract failure cannot be replayed forever without a
            # call.  The boundary is transport metadata only; it does not
            # change the scientific question or evidence set.
            session = self.resume_session.get("session")
            if type(session) is int and session >= 1:
                assessment_assignment["resume_boundary"] = f"gap-assessment-resume-{session}"
            assessment_assignment["instructions"] += (
                " Each comparison is independently scoped: for a comparison with work_id=W, "
                "every evidence item in that comparison must resolve to the same W. "
                "If the supplied evidence belongs to another work or cannot be mapped unambiguously, "
                "omit that comparison or mark it uncertain with an empty evidence list; never cross-cite."
            )
        assessment_assignment = self._fit_assessment_assignment(
            self._follow_up_assignment(assessment_assignment))
        assessment_sources = assessment_assignment["sources"]
        assessment_source_lookup = {source["source_ref"]: self.source_docs[source["source_ref"]]
                                    for source in assessment_sources}
        windows = index_source_windows(assessment_sources)
        assessment_evidence_catalog = deepcopy(assessment_assignment["evidence_catalog"])
        verified_full_text_refs = assessment_assignment["verified_full_text_refs"]
        if resume_gap_assessment:
            # A resumed assessment is a repair of the decision, not a new
            # literature pass. Reuse the same evidence IDs and claim index,
            # but omit repeated source windows and retrieval detail from the
            # model prompt. Validation still uses the full retained captures
            # and exact source windows assembled above.
            assessment_assignment = self._compact_gap_repair_assignment(assessment_assignment)
        def validate_gap_assessment(value):
            validate_assessment(value, assessment_source_lookup, self.works,
                                require_spans=True, windows=windows)

        value, execution = self._model_checked("gap-assessment", "methods.novelty-verifier",
            assessment_assignment, validate_gap_assessment,
            normalizer=lambda value: self._bind_assessment_spans(expand_evidence(
                normalize_gap_assessment_envelope(
                    value, evidence_catalog=assessment_evidence_catalog,
                    verified_full_text_refs=verified_full_text_refs,
                    known_work_ids=set(self.works)),
                assessment_evidence_catalog, self.source_docs,
                windows=windows), assessment_sources),
            # Keep the configured output budget for this evidence-heavy
            # decision. A 2k override caused reasoning-oriented models to hit
            # finish_reason=length before emitting the required JSON object;
            # the subsequent schema repair inherited the same cap and failed
            # identically. Temperature remains deterministic without shrinking
            # the actual response envelope.
            model_overrides={"temperature": 0.0},
            stage="integrated_review", task_kind="verification")
        record = self._publish("kb/gap-assessments/current", "note", {
            "survey_ref": self.survey_ref, "nomination_ref": self.nomination_record["artifact_ref"],
            "execution_ref": execution, **value}, "methods.novelty-verifier",
            subjects=[self.survey_ref, execution, self.nomination_record["artifact_ref"]])
        adopted = self.gate.commit_assessment(record["artifact_ref"], survey_ref=self.survey_ref,
                                             author="strategy.survey-integrator", guard=self._admission_guard)
        self.assessment_ref = adopted["artifact_ref"]
        self.verified_changes.append({"kind": "accepted_gap_assessment", "ref": self.assessment_ref})
        return value["state"]

    def run(self):
        if threading.current_thread() is not threading.main_thread():
            raise ValidationError("survey run requires the main thread")
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(signum, frame):
            raise KeyboardInterrupt("termination requested")
        signal.signal(signal.SIGTERM, terminate)
        try:
            return self._run()
        finally:
            signal.signal(signal.SIGTERM, previous)

    def _run(self):
        status, error, decision = "blocked", None, "insufficient_evidence"
        failure = None
        try:
            if self.resume_session:
                self._record_model_execution_controls()
            if not self.resume_session:
                self._initialize()
            if not self.resume_session and not self.time_policy.snapshot()["initial_hard_limit_feasible"]:
                raise ValidationError("configured survey stages do not fit the hard deadline; no external work dispatched")
            retained_follow_up_discovery = (
                bool(self.work_orders) and self.resume_session is not None
                and (self.counter_queries_complete or self.follow_up_discovery_current)
                and not {"retrieval", "production"}.intersection(self.resume_session["reopened_scopes"])
            )
            needs_operations = (bool(self.work_orders) and not retained_follow_up_discovery
                                or not self.assessment_ref and (
                                    not self.survey_ref or self.nomination is None or not self.countersearch_complete))
            if self.resume_session and "operations" in self.resume_session["reopened_scopes"]:
                needs_operations = True
            if needs_operations:
                self._setup()
            if self.work_orders and self.resume_session and not retained_follow_up_discovery:
                self._prepare_follow_up()
            if self.assessment_ref:
                decision = self._body(self.store.get(self.assessment_ref))["state"]
            else:
                if not self.survey_ref:
                    review_checkpoint = (self.resume_session and self.map_record is not None
                                         and "retrieval" not in self.resume_session["reopened_scopes"]
                                         and ("integrated_review" in self.resume_session["reopened_scopes"]
                                              or self.store.head("kb/exploration-tree") is None))
                    if self.nomination is not None or review_checkpoint:
                        self._accept_survey()
                        self._refresh_countersearch_state()
                    else:
                        self._explore()
                        self._accept_survey()
                if self.nomination is None:
                    self._nominate()
                if not self.countersearch_complete:
                    self._countersearch()
                decision = self._assess()
            self._resolve_follow_up()
            status = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.blockers.append({"reason": error})
            if isinstance(exc, KeyboardInterrupt):
                status = "paused"
                failure = {"kind": "process_interrupted"}
            elif isinstance(exc, ProviderRateLimitError):
                status = "paused"
                failure = {"kind": "provider_rate_limit", "provider": exc.provider, "details": deepcopy(exc.details)}
            elif isinstance(exc, ProviderCooldownError):
                status = "paused"
                failure = {"kind": "provider_cooldown", "retry_after_seconds": exc.retry_after_seconds,
                           "rate_limit": exc.rate_limit}
            elif isinstance(exc, ProviderConfigurationError):
                status = "paused"
                failure = {"kind": "provider_configuration", "provider": exc.provider,
                           "credential_env": exc.credential_env}
            elif isinstance(exc, ModelCallError):
                failure = exc.failure_details()
            elif isinstance(exc, ModelContractError):
                failure = {"kind": "model_contract", "failure_class": exc.failure_class,
                           "recovery_mode": exc.recovery_mode}
            elif isinstance(exc, ModelContextBudgetError):
                failure = {"kind": "context_budget", "failure_class": exc.failure_class,
                           "model": exc.model, "estimated_input_tokens": exc.estimated_input_tokens,
                           "allowed_input_tokens": exc.allowed_input_tokens,
                           "context_window_tokens": exc.context_window_tokens,
                           "max_input_tokens": exc.max_input_tokens, "max_output_tokens": exc.max_output_tokens,
                           "outcome_known": True, "attempts": 0}
            elif isinstance(exc, QuotaExceededError):
                failure = {"kind": "quota_exceeded", "dimension": exc.dimension,
                           "limit": exc.limit, "observed": exc.observed,
                           "diagnostics": deepcopy(exc.diagnostics),
                           "dispatch_failures": deepcopy(getattr(exc, "dispatch_failures", [])),
                           "usage": deepcopy(exc.usage)}
            elif isinstance(exc, ModelWorkBlocked):
                failure = {"kind": "unchanged_assignment_exhausted"}
                if getattr(exc, "failure_class", None):
                    failure["failure_class"] = exc.failure_class
            elif isinstance(exc, StateError):
                failure = {"kind": "operational_state"}
        finally:
            for row in self.control._conn.execute("SELECT task_id FROM tasks WHERE state='awaiting_review'").fetchall():
                self.tasks.transition(row[0], "blocked", "command.controller", reason="Run ended without an accepted scoped output")
            current, assessment_current = False, False
            if self.survey_ref:
                try:
                    self.gate.require_current(self.survey_ref)
                    current = True
                except Exception:
                    pass
            if self.assessment_ref:
                try:
                    self.gate.require_current_assessment(self.assessment_ref)
                    assessment_current = True
                except Exception:
                    pass
            self._checkpoint(status, force=True)
        result = {"run_id": self.run_id, "project_id": self.config["project_id"], "status": status, "error": error,
            "failure": failure,
            "work_orders": deepcopy(self.work_orders), "follow_up_ref": self.follow_up_ref,
            "follow_up_result": deepcopy(self.follow_up_result),
            "incumbent_ref": self.incumbent,
            "score_ref": getattr(self, "score_ref", None), "survey_ref": self.survey_ref, "survey_current": current,
            "assessment_ref": self.assessment_ref, "assessment_current": assessment_current,
            "gap_state": decision, "nomination": self.nomination,
            "nomination_ref": self.nomination_record["artifact_ref"] if self.nomination_record else None,
            "coverage": self._coverage(), "time_plan": self.time_policy.snapshot(), "time_decisions": self.time_decisions,
            "provider_intervals": self.provider_intervals, "provider_waits": self.provider_waits,
            "work_budget_adjustments": self.work_budget_adjustments,
            "bibliography_mode": self.bibliography_mode,
            "bibliography_fallback_reason": self.bibliography_fallback_reason,
            "bibliography_fallback_capability": self.bibliography_fallback_capability,
            "usage": self.budget.get_window("run-window"), "unreported_usage": self.usage_gaps,
            "capabilities": {key: self.operations.status(key) for key in self.capability_ids},
            "blockers": self.blockers, "event_chain": self.control.verify_chain(), "release_status": "not_released"}
        self._publish("command/results/final", "report", result, "command.controller")
        self._export(result)
        self.control.close()
        return result

    def _export(self, result):
        output = self.dir / "output"
        output.mkdir(exist_ok=True)
        (output / "run.json").write_bytes(canonical_bytes(result))
        (output / "works.json").write_bytes(canonical_bytes(list(self.works.values())))
        (output / "bibliographic-identities.json").write_bytes(canonical_bytes([
            self._body(record) for record in self.identity_records.values()]))
        (output / "coverage.json").write_bytes(canonical_bytes(self._coverage()))
        if self.map_record is not None:
            (output / "literature-map.json").write_bytes(canonical_bytes({
                "survey_ref": self.survey_ref, "survey_current": result["survey_current"],
                "map_ref": self.map_record["artifact_ref"], "map": self._map_body()}))
        if self.assessment_ref:
            body = self.store.read_body(self.store.get(self.assessment_ref)["body_hash"])
            (output / "gap-assessment.json").write_bytes(body)
        lines = ["# Literature assessment", "", self.score["question"], "",
            f"Run status: **{result['status']}**. Gap state: **{result['gap_state']}**.", "",
            f"Current accepted survey: {result['survey_ref'] or 'none'}. Current applicability: {result['survey_current']}.", "",
            "This assessment covers the recorded search protocol. It does not establish exhaustive coverage or publication-ready novelty.", "",
            "## Works and comparisons", "", "Publication years below remain provider observations; separate identity records report any Crossref reconciliation.", ""]
        for wid, work in self.works.items():
            lines.extend([f"### {work['title']}", "", f"[Provider record](https://openalex.org/{wid}); reported year: {work['year'] or 'unknown'}.", ""])
            record = self.analysis_records.get(wid)
            if record:
                entry = json.loads(self.store.read_body(record["body_hash"]))
                lines.extend([f"Screening: {entry['inclusion']}. {entry['reason']}", ""])
                for field in MAP_FIELDS:
                    claim = entry[field]
                    lines.extend([f"**{field.capitalize()}:** {claim['text'] or 'Not established by available text.'}", ""])
        if self.assessment_ref:
            assessment = json.loads(self.store.read_body(self.store.get(self.assessment_ref)["body_hash"]))
            lines.extend(["## Gap assessment", "", f"Hypothesis: {self.nomination['statement']}", "",
                          f"Nomination: {result['nomination_ref']}. Current assessment applicability: {result['assessment_current']}.", "",
                          assessment["rationale"], ""])
            for comparison in assessment["comparisons"]:
                lines.extend([f"- **{self.works[comparison['work_id']]['title']}** ({comparison['relationship']}): {comparison['statement']}"])
        lines.extend(["", "## Coverage and unresolved access", "", f"Unique works: {len(self.works)}. Bibliographic calls: {self.api_calls}.", ""])
        for gap in self.gaps + self.blockers:
            lines.append("- " + json.dumps(gap, ensure_ascii=False))
        lines.extend(["", "Release status: not_released.", ""])
        if self.exploration_tree is not None:
            (output / "exploration-tree.json").write_text(json.dumps(self.exploration_tree, indent=2) + "\n", encoding="utf-8")
        (output / "survey.md").write_text("\n".join(lines), encoding="utf-8", newline="")
