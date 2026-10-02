"""Survey integration against local HTTP/stdio fixtures and simulated model workers."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scisaurus.core.errors import ModelContractError, ProviderRateLimitError, QuotaExceededError, StateError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.core.source_spans import bind, expand_evidence
from scisaurus.core.surveys import ABSTENTION_REASONS, SurveyGate, work_review_checks
from scisaurus.runtime.execution import SYSTEM, _invoke_worker
from scisaurus.runtime.bibliographic_identity import reconcile_result
from scisaurus.runtime.literature import ProviderCooldownError
from scisaurus.runtime.model_work import ModelWorkCache
from scisaurus.runtime.model_work import ModelWorkBlocked
from scisaurus.runtime.models import estimate_input_tokens
from scisaurus.runtime.survey import (SurveyRunner, apply_scoped_map_repair,
                                      countersearch_lineage,
                                      normalize_survey_repair_owners,
                                      normalize_gap_nomination,
                                      normalize_map_relationships,
                                      normalize_map_worker_response,
                                      overlay_post_checkpoint_relationships,
                                      source_fidelity_review_contract)
from scisaurus.runtime.survey_config import validate_survey_config
from scisaurus.runtime.survey_records import (GAP_CHECKS, MAP_FIELDS, SURVEY_CHECKS,
                                               normalize_check_envelope,
                                               normalize_gap_assessment_envelope,
                                               validate_assessment, validate_map, validate_work_review)
from scisaurus.runtime.time_policy import STAGES
from scisaurus.tests.test_retrieval import minimal_pdf


GAP = "No prior method solves delayed recall with a fixed observation budget."


def survey_work(wid):
    abstract = "Recall timing is examined. The observation budget is fixed."
    if wid == "W401":
        abstract += " This prior method solves delayed recall."
    words = {}
    for index, word in enumerate(abstract.split()):
        words.setdefault(word, []).append(index)
    return {"id": "https://openalex.org/" + wid, "doi": "https://doi.org/10.1234/" + wid,
            "title": "Recall study " + wid, "publication_year": 2020 + int(wid[-1]),
            "abstract_inverted_index": words,
            "referenced_works": ["https://openalex.org/W102"] if wid == "W101" else (
                ["https://openalex.org/W101"] if wid == "W301" else []),
            "related_works": [], "locations": []}


class SurveyHTTPFixture(BaseHTTPRequestHandler):
    identity_rate_limit_once = None
    requests = []
    refresh_target = False
    rate_limit_once = None
    locations_by_work = {}
    robots_disallow = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        self.requests.append({"path": path.path, "query": query})
        if path.path == "/robots.txt":
            disallow = type(self).robots_disallow or ""
            body = f"User-agent: *\nDisallow: {disallow}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path == "/denied.pdf":
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.path == "/gateway-error.pdf":
            body = b"upstream gateway unavailable"
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path == "/mcp-denied":
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.path == "/article":
            body = (b"<html><body><article><h1>Recall study W401</h1>"
                    b"<p>Methods and results describe recall timing and a fixed observation budget.</p>"
                    b"</article></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path in {"/paper.pdf", "/fallback.pdf", "/paper-download"}:
            body = minimal_pdf(
                "Recall study W401\nIntroduction\nMethods\nRecall timing is examined.\n"
                "Results\nThis prior method solves delayed recall.")
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path == "/works/W404":
            body = json.dumps({"error": "Work not found"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if (self.rate_limit_once is not None
                and query.get("search", [None])[0] == self.rate_limit_once):
            type(self).rate_limit_once = None
            body = json.dumps({"error": "Rate limit exceeded", "retryAfter": 1}).encode()
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if query.get("search", [None])[0] == "query-timeout":
            body = json.dumps({
                "error": "Gateway timeout",
                "message": "Your query took too long and was stopped. Please narrow the query.",
                "reason": "query_timeout",
            }).encode()
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if ("query.bibliographic" in query
                or query.get("filter", [""])[0].lower().startswith("doi:")):
            doi = query.get("query.bibliographic", query.get("filter"))[0].lower()
            doi = doi.removeprefix("doi:")
            if doi == type(self).identity_rate_limit_once:
                type(self).identity_rate_limit_once = None
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            wid = doi.rsplit("/", 1)[-1].upper()
            item = {"DOI": doi, "title": ["Recall study " + wid], "publisher": "Fixture Publisher",
                    "published": {"date-parts": [[2020 + int(wid[-1])]]}, "author": []}
            payload = {"status": "ok", "message-version": "1.0.0", "message": {
                "items": [item], "total-results": 1, "next-cursor": None}}
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path != "/works":
            payload = survey_work(path.path.rsplit("/", 1)[1])
            payload["locations"] = type(self).locations_by_work.get(payload["id"].rsplit("/", 1)[-1], [])
        else:
            term = query.get("search", [""])[0]
            ids = ["W401"] if term == "prior solution" else (
                ["W101", "W201", "W301"] if term == "candidate comparison" else
                ["W201"] if term == "independent terminology" else ["W101"])
            if "filter" in query:
                ids = ["W301"]
            if term == "prior solution" and self.refresh_target:
                ids.append("W102")
            results = [survey_work(wid) for wid in ids]
            for item in results:
                item["locations"] = type(self).locations_by_work.get(item["id"].rsplit("/", 1)[-1], [])
            payload = {"meta": {"count": len(ids), "per_page": int(query["per_page"][0]),
                                "next_cursor": None}, "results": results}
            if term == "prior solution" and self.refresh_target:
                payload["results"][-1]["abstract_inverted_index"]["Updated."] = [9]
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def check_rows(names, outcome="passed"):
    return [{"check_id": name, "outcome": outcome, "method": "Fixture checks captured source contracts.",
             "result": "The explicit fixture contract is satisfied.",
             **({"affected_check_ids": []} if name.startswith("critique:") else {})} for name in names]


def source_quote(source, quote="Recall timing is examined."):
    return {"work_id": source["work_id"], "source_ref": source["source_ref"], "quote": quote}


def simulated_survey_worker(kind, params, channel):
    """Only model decisions are simulated; transport operations execute real clients."""
    if kind != "model":
        return _invoke_worker(kind, params, channel)
    assignment = json.loads(params["prompt"])
    phase, mode = assignment["phase"], params["client"]["model"]
    started = time.monotonic()
    if phase == "blind_plan":
        value = {"queries": ["independent terminology"], "rationale": "Search neighboring terminology."}
    elif phase == "reading_selection":
        value = {"rationale": "Read the captured recall candidates relevant to the inquiry.",
                 "candidates": [{"work_id": candidate["work_id"], "decision": "read",
                                 "rationale": "Check the captured recall evidence."}
                                for candidate in assignment["candidates"]]}
    elif phase == "exploration_plan":
        parent = assignment["parents"][0]
        branches = []
        if parent["kind"] == "root":
            for query in assignment["suggestions"][:assignment["max_branches"]]:
                branches.append({"parent_id": parent["id"], "question": "Which studies examine recall timing?",
                    "rationale": "Establish the initial evidence corpus.", "operation": "search",
                    "query": query, "work_id": None, "evidence": []})
        elif parent["work_id"] == "W101":
            source = next(source for source in assignment["sources"] if source["work_id"] == "W101")
            for operation, query, wid in (("work", None, "W102"), ("citing", None, "W101"),
                                           ("search", "independent terminology", None)):
                branches.append({"parent_id": parent["id"], "question": "How do neighboring studies treat recall timing?",
                    "rationale": "Extend the checked recall evidence with references and distinct terminology.",
                    "operation": operation, "query": query, "work_id": wid, "evidence": [source_quote(source)]})
        value = {"decision": "expand" if branches else "stop", "rationale": "Retain bounded recall evidence.",
                 "branches": branches}
    elif phase == "survey_operation_completion":
        disposition = assignment["disposition"]
        value = {"outcome": "unmet" if mode in {"follow-up-capture-required", "follow-up-records-only"} else "met",
                 "rationale": "The exact operation's permitted availability record is present."}
        if mode == "follow-up-decision-required":
            context = assignment.get("evidence_context", {})
            complete = ("prediction" in disposition.get("next_action", "")
                        and bool(disposition.get("rationale")) and bool(context.get("searches"))
                        and bool(context.get("survey_inventory", {}).get("works")))
            value = {"outcome": "met" if complete else "unmet",
                     "rationale": "Check the declared scientific decision and captured bounded search."}
    elif phase == "survey_follow_up":
        value = {"orders": [{
            "id": order["id"], "status": "limited", "rationale": "The bounded evidence does not resolve this measurement.",
            "evidence": [], "query_refs": assignment["query_refs"],
            "limitation": "The configured sources lack the requested measurement.",
            "next_action": "Keep the measurement claim provisional and narrow the study.",
            "completion": {"outcome": "met", "rationale": "The bounded negative search is recorded as allowed."},
        } for order in assignment["work_orders"]]}
        if mode in {"follow-up-capture-required", "follow-up-conflicted-completion"}:
            for row in value["orders"]:
                row["completion"] = {"outcome": "unmet", "rationale": "The required numeric capture is absent."}
        if mode == "follow-up-decision-required":
            for row in value["orders"]:
                row["next_action"] = "Proceed as a prediction study; empirical confirmation remains unsupported."
                row["completion"] = {"outcome": "unmet", "rationale": "No measured result is established."}
        if mode == "follow-up-missing-completion":
            for row in value["orders"]:
                row.pop("completion")
        if mode in {"follow-up-records", "follow-up-records-only"}:
            for row in value["orders"]:
                if mode == "follow-up-records-only":
                    row["query_refs"] = []
                    row["status"] = "unresolved"
                    row["completion"] = {"outcome": "unmet", "rationale": "The required earlier projection remains absent."}
                row["record_evidence"] = [assignment["survey_inventory"]["works"][0]["work_ref"]]
                row["rationale"] = "Current catalog membership is established by the pinned record."
                row["limitation"] = "The earlier upstream projection is not captured."
                row["next_action"] = "Inspect the upstream projection without repeating acquisition."
        if mode == "follow-up-source-binding":
            for row in value["orders"]:
                if row["id"] != "evidence-2":
                    continue
                if "_contract_repair_boundary" in assignment:
                    row["evidence"] = [{"evidence_id": assignment["evidence_catalog"][0]["evidence_id"]}]
                else:
                    source = assignment["sources"][0]
                    row["evidence"] = [{"work_id": source["work_id"], "source_ref": source["source_ref"],
                                        "quote": "This quotation belongs to another captured document."}]
    elif phase == "counter_plan":
        value = {"queries": ["prior solution"], "rationale": "Search for an existing solution."}
    elif phase == "nomination":
        value = {"id": "Field_0to1T_" + "x" * 70 if mode == "nomination-id-drift" else "delayed-recall", "statement": GAP}
    elif phase == "map":
        if mode in {"map-repair", "map-reject"}:
            time.sleep(0.25)
        entries = []
        for wid in assignment["requested_work_ids"]:
            source = next(s for s in assignment["sources"] if s["work_id"] == wid)
            proof = source_quote(source)
            if mode == "forged-quote" or (wid == "W101" and (mode == "map-reject" or (
                    mode == "map-repair" and "validation_feedback" not in assignment))):
                proof["quote"] = "This sentence is absent from every source."
            fields = {field: {"text": None, "evidence": []} for field in MAP_FIELDS}
            fields["problem"] = {"text": "Recall timing is examined.", "evidence": [proof]}
            entries.append({"work_id": wid, "inclusion": "included", "reason": "Explicitly examines recall timing.", **fields})
        value = {"entries": entries, "relationships": []}
        if mode == "isolated-review-block" and assignment["requested_work_ids"] == ["W102"]:
            if assignment.get("semantic_feedback"):
                value = {"entry_updates": {"reason": "The captured study examines recall timing."}, "relationships": []}
            else:
                value["entries"][0]["reason"] = "The method generalizes to every task."
        if mode.startswith("semantic-") and (assignment["requested_work_ids"] == ["W101"] or mode == "semantic-many"):
            repair = assignment.get("semantic_feedback") is not None
            if repair:
                value = {"entry_updates": {
                    "reason": "The study examines recall timing." if mode != "semantic-exhaust" else "The method generalizes to every task."
                }, "relationships": []}
                if mode == "semantic-unscoped":
                    value["entry_updates"]["finding"] = deepcopy(entries[0]["problem"])
            else:
                value["entries"][0]["reason"] = "The method generalizes to every task."
        if mode in {"review-obligation-adversary", "aggregate-scoped-repair"} and assignment.get("semantic_feedback") is not None:
            value = {"entry_updates": {"reason": "The captured study examines recall timing."}, "relationships": []}
        if mode in {"aggregate-inclusion-quote", "aggregate-inclusion-selection"} and assignment.get("semantic_feedback") is not None:
            value = {"entry_updates": {"inclusion": "uncertain"}, "relationships": []}
        if mode.startswith("map-links") and assignment["requested_work_ids"] == ["W101"]:
            proofs = [source_quote(next(source for source in assignment["sources"] if source["work_id"] == wid))
                      for wid in ("W101", "W102")]
            value["relationships"] = [{"source": "W101", "target": "W102", "kind": "compares",
                                       "claim": {"text": "Both works examine recall timing.", "evidence": proofs}}]
            if mode == "map-links-rewrite" and assignment.get("entry_editable") is False:
                value["entries"][0]["reason"] = "A changed screening rationale without new evidence."
    elif phase == "survey_repair_plan":
        value = {"repairs": []}
        if mode in {"aggregate-scoped-repair", "aggregate-ungranted-repair"}:
            value["repairs"] = [{"entry_ref": assignment["entry_refs"]["W101"],
                "entry_fields": ["reason"] if mode == "aggregate-scoped-repair" else ["invented_field"],
                "relationship_refs": [], "rationale": "The retained screening qualifier is unsupported."}]
        if mode in {"aggregate-inclusion-quote", "aggregate-inclusion-selection"}:
            value["repairs"] = [{"entry_ref": assignment["entry_refs"]["W101"],
                "entry_fields": ["inclusion"], "relationship_refs": [],
                "rationale": "The inclusion decision needs a narrower screening state."}]
    elif phase == "survey_review":
        value = {"checks": check_rows(SURVEY_CHECKS), "rationale": "The map preserves unknown facts and bounded coverage."}
        if mode == "survey-review-malformed":
            value = {"checks": [], "rationale": "Incomplete review envelope."}
        elif mode == "aggregate-history-adversary" and any(
                key in assignment for key in ("review_obligations", "critique_contexts", "prior_review_response")):
            value["checks"][1].update(outcome="failed", result="A superseded allegation was mistaken for a current claim.")
        elif mode in {"aggregate-scoped-repair", "aggregate-ungranted-repair"}:
            if any(entry["work_id"] == "W101" and entry["reason"] == "Explicitly examines recall timing."
                   for entry in assignment["map"]["entries"]):
                value["checks"][1].update(outcome="failed", result="The W101 screening qualifier is unsupported.")
        elif mode == "survey-fails":
            value["checks"][1].update(outcome="failed", result="The independent fixture review rejects source fidelity.")
        elif (mode == "survey-coverage-insufficient"
              or mode.startswith("survey-coverage-insufficient-")):
            value["checks"][1].update(
                outcome="insufficient_evidence",
                method="Coverage is incomplete because some full texts and one identity are unresolved.",
                result="Retained assertions match their captured abstracts; missing full text limits corpus coverage.",
            )
            failed_check = mode.removeprefix("survey-coverage-insufficient-")
            if failed_check in {"coverage-accounting", "map-support"}:
                failed = next(row for row in value["checks"]
                              if row["check_id"] == failed_check)
                failed.update(outcome="failed", result="The fixture requires this gate to remain blocking.")
        elif (mode == "survey-review-fails-second"
              and int(assignment.get("survey_ref", "@1").rsplit("@", 1)[1]) >= 2
              and not assignment.get("resume_boundary")):
            value["checks"][2].update(
                outcome="failed",
                result="The second version has a deliberately unsupported included claim.",
            )
        if isinstance(value.get("checks"), list):
            target = next(iter(assignment["map"]["entries"]), None)
            value["findings"] = [{"check_id": row["check_id"],
                "target_ref": assignment["map"]["entry_refs"][target["work_id"]],
                "field": "reason", "quote": target["reason"],
                "rationale": row["result"]}
                for row in value["checks"] if target is not None
                and row["outcome"] != "passed" and row["check_id"] in {"source-fidelity", "map-support"}]
        if mode in {"aggregate-inclusion-quote", "aggregate-inclusion-selection"}:
            target = next(entry for entry in assignment["map"]["entries"] if entry["work_id"] == "W101")
            if target["inclusion"] == "included":
                next(row for row in value["checks"] if row["check_id"] == "map-support").update(
                    outcome="failed", result="Adjudicate the inclusion decision from its rationale.")
                value["findings"] = [{"check_id": "map-support",
                    "target_ref": assignment["map"]["entry_refs"]["W101"], "field": "inclusion",
                    "quote": target["reason"], "rationale": "The included screening state requires adjudication."}]
                if mode == "aggregate-inclusion-selection":
                    pin = next(row for row in assignment["response_contract"]["assertion_catalog"]
                               if row["target_ref"] == assignment["map"]["entry_refs"]["W101"] and row["field"] == "inclusion")
                    value["findings"] = [{"check_id": "map-support", "assertion_id": pin["assertion_id"],
                                          "rationale": "The included screening state requires adjudication."}]
    elif phase == "work_review":
        if mode in {"review-malformed", "isolated-review-block"} and assignment["entry"]["work_id"] == "W101":
            value = {"checks": [{"check_id": "duplicate-check", "outcome": "passed",
                                  "method": "Malformed fixture response.", "result": "Not a valid focused review."}],
                     "rationale": "Malformed fixture response."}
        else:
            value = {"checks": check_rows(assignment["required_checks"]), "rationale": "Each scoped claim is supported or explicitly unknown."}
        if mode == "isolated-review-block" and assignment["entry"]["work_id"] == "W102" and assignment["entry"]["reason"] == "The method generalizes to every task.":
            next(row for row in value["checks"] if row["check_id"] == "reason").update(
                outcome="failed", result="The screening rationale asserts unsupported generalization.")
        if mode in {"typed-critique-review", "missing-critique-section"} and assignment.get("review_obligations"):
            critique_rows = [row for row in value["checks"] if row["check_id"].startswith("critique:")]
            value["checks"] = [row for row in value["checks"] if not row["check_id"].startswith("critique:")]
            value["critique_adjudications"] = [{"check_id": row["check_id"], "disposition": "rejected",
                "method": "Compare the current entry with its captured source.",
                "result": "The current claim is supported; the disputed allegation is rejected.",
                "affected_check_ids": []} for row in critique_rows]
            if mode == "missing-critique-section" and not assignment.get("validation_feedback"):
                value.pop("critique_adjudications")
        if mode == "review-never-resolves" and assignment["entry"]["work_id"] == "W101":
            next(check for check in value["checks"] if check["check_id"] == "reason").update(
                outcome="insufficient_evidence", result="The screening rationale remains unresolved.")
        if mode == "review-forced-reason-failure":
            next(check for check in value["checks"] if check["check_id"] == "reason").update(
                outcome="failed", result="The screening rationale exceeds the captured evidence.")
        if (mode == "review-obligation-adversary" and assignment.get("review_obligations")
                and assignment["entry"]["reason"] == "Explicitly examines recall timing."):
            next(check for check in value["checks"] if check["check_id"] == "reason").update(
                outcome="failed", result="The independent critique identifies a qualification requiring a narrower rationale.")
        if mode in {"review-current-diagnostic", "review-current-diagnostic-failure"}:
            if mode.endswith("-failure"):
                next(check for check in value["checks"] if check["check_id"] == "reason").update(
                    outcome="failed", result="The screening rationale exceeds the captured evidence.")
            value["checks"].append({"check_id": "question-answer-coverage", "outcome": "insufficient_evidence",
                                    "method": "Separate question coverage diagnostic.",
                                    "result": "The research question remains for downstream gap assessment."})
        if mode == "review-question-adversary":
            contract = assignment.get("review_contract", {})
            if contract != source_fidelity_review_contract():
                next(check for check in value["checks"] if check["check_id"] == "finding").update(
                    outcome="insufficient_evidence", result="The supported finding does not answer the research question.")
        if mode == "screening-relevance-adversary":
            contract = assignment.get("review_contract", {})
            if "Every included work must have a source-supported connection" in contract.get("screening_scope", ""):
                if assignment["entry"]["reason"] == "The source's LED topic matches its own summary.":
                    for check in value["checks"]:
                        if check["check_id"] in {"inclusion", "reason"}:
                            check.update(outcome="failed", result="The source does not establish a connection to the declared work/coherence question.")
        if mode == "relationship-qualifier-adversary":
            if "qualifiers in relationship claims" in assignment.get("review_contract", {}).get("failure_basis", ""):
                for check in value["checks"]:
                    if check["check_id"].startswith("relationship:"):
                        check.update(outcome="failed", result="The cited source supports a topical connection but not the asserted fundamental qualifier.")
        if mode.startswith("semantic-") and "every task" in assignment["entry"]["reason"]:
            next(check for check in value["checks"] if check["check_id"] == "reason").update(
                outcome="failed", result="The abstract does not establish generalization to every task; narrow the reason to recall timing.")
    elif phase == "gap_assessment":
        decisive = mode in {"fulltext-refutes", "abstract-refutes"}
        sources = [s for s in assignment["sources"] if s["work_id"] == "W401"]
        source = next((s for s in sources if s["representation"] == "full_text"), sources[0])
        proof = source_quote(source, "This prior method solves delayed recall.")
        if mode == "catalog-evidence":
            proof = {"evidence_id": next(item["evidence_id"] for item in assignment["evidence_catalog"]
                                         if item["work_id"] == "W401")}
        value = {"state": "refuted_by_prior_work" if decisive else "insufficient_evidence",
                 "rationale": "The supplied full text establishes a prior solution." if decisive else "Full text is unavailable.",
                 "comparisons": [{"work_id": "W401", "relationship": "solves" if decisive else "uncertain",
                                  "statement": "Potential prior solution requires full-text confirmation.",
                                  "evidence": [proof]}],
                 "checks": check_rows(GAP_CHECKS), "evidence": [proof] if decisive else []}
        if not decisive:
            if any(source["representation"] == "full_text" for source in sources):
                value["rationale"] = "The closest-work comparison remains unresolved."
                value["checks"][1].update(outcome="insufficient_evidence", result="Fixture conditions are not resolved as comparable.")
            else:
                value["checks"][-1].update(outcome="insufficient_evidence", result="No verified full text was captured.")
    else:
        raise AssertionError(f"Unexpected fixture phase {phase}")
    timing = {"fixture": "explicitly-simulated-survey-worker", "started": started, "finished": time.monotonic(), "pid": os.getpid()}
    channel.put({"ok": True, "result": {"text": json.dumps(value), "model": json.dumps(timing),
        "usage": {"model_calls": 1, "input_tokens": 100, "output_tokens": 50},
        "elapsed_seconds": time.monotonic() - started,
        "finish_reason": ("content_filter" if mode == "follow-up-filtered" and phase == "survey_follow_up"
                          and "_contract_repair_boundary" not in assignment else "stop")}})


MCP_SURVEY_FIXTURE = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    if method == 'notifications/initialized':
        continue
    if method == 'initialize':
        result = {'protocolVersion': '2025-11-25', 'serverInfo': {'name': 'mcp-fetch', 'version': 'simulated-fixture-1'},
                  'capabilities': {'tools': {}}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'fetch', 'inputSchema': {'type': 'object', 'properties': {
            'url': {'type': 'string'}, 'max_length': {'type': 'integer'},
            'start_index': {'type': 'integer'}, 'raw': {'type': 'boolean'}}, 'required': ['url']}}]}
    elif method == 'tools/call':
        text = 'Recall study W401\nMethods\nRecall timing is examined. The observation budget is fixed.\nResults\nThis prior method solves delayed recall.'
        url = message['params']['arguments']['url']
        if url.endswith('/transport-error'):
            result = {'content': [{'type': 'text', 'text': 'Temporary fetch transport failure.'}], 'isError': True}
        elif url.endswith('/mcp-denied'):
            result = {'content': [{'type': 'text', 'text': f'Failed to fetch {url} - status code 403'}], 'isError': True}
        else:
            unavailable = url.endswith('/unavailable')
            raw = 'Content type text/html cannot be simplified to markdown, but here is the raw content:\nFixture route unavailable.'
            result = {'content': [{'type': 'text', 'text': raw if unavailable else text}], 'isError': False}
    else:
        raise AssertionError(method)
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
'''


def survey_config(endpoint, mode="pass"):
    return {"live_dispatch_allowed": True, "data_classification": "public", "allocation_mode": "capacity_pool",
        "project_id": "survey-fixture", "objective": "Map recall timing literature and challenge a bounded research gap.",
        "supplied_context": "All papers and model responses are explicit integration fixtures.",
        "model": {"base_url": "http://127.0.0.1:1", "model": mode, "protocol": "ollama",
                  "timeout_seconds": 5, "max_output_tokens": 4096},
        "limits": {"max_rounds": 1, "max_result_bytes": 1000000, "concurrent_calls": 3,
                   "wall_clock_seconds": 45, "checkpoint_seconds": 0.05},
        "time_policy": {"first_result_seconds": 25, "target_seconds": 35, "hard_seconds": 40},
        "survey": {"id": "recall-survey", "revision": 1, "question": "How has recall timing research developed?",
            "seed_queries": ["recall timing"], "seed_work_ids": ["W101"],
            "proposed_gap": {"id": "delayed-recall", "statement": GAP},
            "bibliography": {"id": "bibliography", "adapter": "openalex",
                "client": {"endpoint": endpoint, "timeout": 4, "max_bytes": 1000000},
                "representative": {"operation": "search", "query": "readiness", "work_id": None, "limit": 2, "cursor": None},
                "environment_files": []},
            "full_text": None, "full_text_sources": [],
            "search": {"queries_per_role": 1, "results_per_query": 2, "max_works": 10,
                "challenge_reserve": 1,
                "expansion_rounds": 1, "expansion_seed_count": 1, "references_per_work": 1,
                "max_api_calls": 10, "min_new_works": 1, "saturation_rounds": 1,
                "max_full_texts": 2, "max_text_chars": 10000, "context_chars": 10000},
            "stage_seconds": {stage: 0.1 for stage in STAGES}}}


class TestSurveyRunner(unittest.TestCase):
    def test_map_capacity_cannot_truncate_required_capture_or_target_catalog(self):
        from scisaurus.runtime.models import ModelContextBudgetError
        runner = self.runtime()
        runner.config["model"].update(max_input_tokens=2048, context_window_tokens=8192,
                                      max_output_tokens=4096)
        assignment = {"sources": [{"work_id": "W1", "text": "Complete owner evidence. " * 1500,
                                    "window": {"start": 0, "end": 36000}},
                                   {"work_id": "W2", "text": "Complete target evidence.",
                                    "window": {"start": 0, "end": 25}}],
                      "works": [{"id": "W1", "title": "Owner"}, {"id": "W2", "title": "Target"},
                                {"id": "W3", "title": "Optional " * 500}],
                      "previous_entries": [{"protected": "unchanged"}],
                      "previous_affected_relationships": [{"source": "W1", "target": "W2"}],
                      "semantic_feedback": {"protected": "unchanged"}}
        original = deepcopy(assignment)
        with patch.object(runner, "_project_map_sources", wraps=runner._project_map_sources) as projection:
            with self.assertRaises(ModelContextBudgetError) as blocked:
                runner._fit_map_assignment(assignment, owner_id="W1")
        self.assertEqual(blocked.exception.failure_class, "context_budget")
        self.assertGreater(blocked.exception.estimated_input_tokens, blocked.exception.allowed_input_tokens)
        self.assertEqual(projection.call_args.kwargs["required_ids"], {"W1", "W2"})
        self.assertEqual(assignment, original)
        with patch.object(runner, "_setup", side_effect=blocked.exception):
            report = runner.run()
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["failure"]["kind"], "context_budget")
        self.assertEqual(report["failure"]["attempts"], 0)
        self.assertEqual(report["usage"]["cumulative_usage"], {})

    def test_map_capacity_compacts_optional_context_preserving_required_sources(self):
        runner = self.runtime()
        runner.config["model"].update(max_input_tokens=4096, context_window_tokens=8192,
                                      max_output_tokens=4096)
        mandatory = [{"work_id": wid, "text": "Complete captured evidence. " * 30,
                      "window": {"start": 0, "end": 840}} for wid in ("W1", "W2")]
        assignment = {"sources": [*mandatory, {"work_id": "W3", "text": "Optional context. " * 3000,
                                               "window": {"start": 0, "end": 54000}}],
                      "works": [{"id": wid, "title": wid} for wid in ("W1", "W2", "W3")],
                      "previous_entries": [{"protected": "unchanged"}],
                      "previous_affected_relationships": [{"source": "W1", "target": "W2"}],
                      "semantic_feedback": {"protected": "unchanged"}}
        original = deepcopy(assignment)
        fitted = runner._fit_map_assignment(assignment, owner_id="W1")
        self.assertLessEqual(estimate_input_tokens(SYSTEM, json.dumps(fitted, ensure_ascii=False)), 4096)
        self.assertEqual([item for item in fitted["sources"] if item["work_id"] in {"W1", "W2"}], mandatory)
        self.assertTrue({"W1", "W2"}.issubset({work["id"] for work in fitted["works"]}))
        for key in ("previous_entries", "previous_affected_relationships", "semantic_feedback"):
            self.assertEqual(fitted[key], assignment[key])
        self.assertEqual(assignment, original)

    def test_focused_review_exposes_complete_capture_and_rejects_prefix_provenance(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner.bounds["context_chars"] = 80
        text = "Recall timing is examined.\n\n" + "Detailed results. " * 50 + "\n\nThe uncertainty is standard error at 298 K."
        source = {"work_id": "W101", "representation": "full_text", "identity_verified": True,
                  "identity_checks": {"title_match": True, "section_markers": ["Results"]}, "text": text}
        capture = runner._record("kb/full-text/W101", "source_capture", source, "methods.source-verifier")
        runner.source_docs[capture["artifact_ref"]] = source
        runner._map(); runner._review_work_claims()
        mapping = next(value for _, value in self.model_contexts(runner.control, runner.store)
                       if value.get("phase") == "map")
        mapped_source = next(item for item in mapping["sources"] if item["source_ref"] == capture["artifact_ref"])
        self.assertEqual(mapped_source["text"], text)
        self.assertEqual(mapped_source["window"], {"start": 0, "end": len(text)})
        review = runner._body(runner.work_reviews["W101"])
        context, prompt = next((manifest, value) for manifest, value in self.model_contexts(runner.control, runner.store)
                               if value.get("phase") == "work_review")
        presented = next(item for item in prompt["sources"] if item["source_ref"] == capture["artifact_ref"])
        self.assertEqual(presented["text"], text)
        self.assertEqual(presented["window"], {"start": 0, "end": len(text)})
        self.assertEqual(review["evidence_scope"]["full_text_windows"],
                         [{"source_ref": capture["artifact_ref"], "start": 0, "end": len(text)}])
        self.assertTrue(runner._work_review_current("W101"))
        prefix_prompt = deepcopy(prompt)
        prefix = next(item for item in prefix_prompt["sources"] if item["source_ref"] == capture["artifact_ref"])
        prefix.update(text=text[:80], window={"start": 0, "end": 80})
        _, _, _, reply = runner.gate._model_review_execution(review["execution_ref"], "methods.work-reviewer")
        execution = runner.store.get(review["execution_ref"])
        with patch.object(runner.gate, "_model_review_execution",
                          return_value=(execution, context, prefix_prompt, reply)):
            self.assertFalse(runner._review_protocol_matches(review))
            self.assertFalse(runner._work_review_current("W101"))
        self.assertEqual(runner.source_docs[capture["artifact_ref"]], source)

    def test_retained_critique_omission_uses_current_revalidation_diagnostic(self):
        runner = self.runtime(survey_config(self.endpoint, "missing-critique-section"))
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        obligation = self.review_obligation(runner, "W101")
        runner.review_obligations = runner._validate_review_obligations([obligation])
        with self.assertRaises(ModelWorkBlocked):
            runner._review_work_claims()
        contexts = self.model_contexts(runner.control, runner.store)
        manifest, _ = next((manifest, prompt) for manifest, prompt in reversed(contexts)
                           if prompt.get("phase") == "work_review")
        validation = runner.store.head(manifest["artifact_id"].replace(
            "command/contexts/", "command/validation/"))
        retained = runner._body(validation)
        retained["error"] = "required checks were omitted"
        historical = runner._publish(validation["artifact_id"], "note", retained,
                                     "command.controller",
                                     subjects=[item["ref"] for item in validation["inputs"]])
        runner.resume_session = {"session": 9}
        runner._review_work_claims()
        self.assertEqual(runner._body(historical)["error"], "required checks were omitted")
        prompt = [value for _, value in self.model_contexts(runner.control, runner.store)
                  if value.get("phase") == "work_review"][-1]
        self.assertIn("critique_adjudications", prompt["validation_feedback"]["error"])
        self.assertEqual(prompt["_contract_repair_boundary"], "model-contract-repair-9")
        self.assertTrue(runner._work_review_current("W101"))

    def test_missing_critique_section_repairs_with_exact_schema_feedback(self):
        config = survey_config(self.endpoint, "missing-critique-section")
        config["limits"]["max_rounds"] = 2
        runner = self.runtime(config)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        entry_ref = runner.analysis_records["W101"]["artifact_ref"]
        obligation = self.review_obligation(runner, "W101")
        runner.review_obligations = runner._validate_review_obligations([obligation])
        runner._review_work_claims()
        prompts = [value for _, value in self.model_contexts(runner.control, runner.store)
                   if value.get("phase") == "work_review"]
        self.assertEqual(len(prompts), 2)
        error = prompts[-1]["validation_feedback"]["error"]
        self.assertIn("critique_adjudications", error)
        self.assertIn(prompts[-1]["critique_contexts"][0]["check_id"], error)
        self.assertIn("Preserve the valid ordinary checks", error)
        self.assertEqual(prompts[0]["response_contract"], prompts[-1]["response_contract"])
        self.assertEqual(runner.analysis_records["W101"]["artifact_ref"], entry_ref)
        self.assertTrue(runner._work_review_current("W101"))
        runner._accept_survey()
        runner.gate.require_current(runner.survey_ref)

    def test_typed_critique_rejection_replays_through_independent_survey_gate(self):
        from scisaurus.runtime.models import ModelResult
        runner = self.runtime(survey_config(self.endpoint, "typed-critique-review"))
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        obligation = self.review_obligation(runner, "W101")
        runner.review_obligations = runner._validate_review_obligations([obligation])
        runner._review_work_claims()
        body = runner._body(runner.work_reviews["W101"])
        execution = runner._body(runner.store.get(body["execution_ref"]))
        raw = ModelResult(**execution).json_object(allow_missing_closers=True)
        self.assertEqual(raw["critique_adjudications"][0]["disposition"], "rejected")
        self.assertEqual(body["checks"][-1]["outcome"], "passed")
        prompt = [prompt for _, prompt in self.model_contexts(runner.control, runner.store)
                  if prompt.get("phase") == "work_review"][-1]
        self.assertIn("critique_adjudications", prompt["response_contract"]["top_level_fields"])
        self.assertTrue(all(not row["check_id"].startswith("critique:") for row in prompt["response_contract"]["checks"]))
        runner._accept_survey()
        runner.gate.require_current(runner.survey_ref)

    def test_malformed_review_does_not_block_valid_sibling_correction(self):
        config = survey_config(self.endpoint, "isolated-review-block")
        config["limits"]["max_rounds"] = 2
        runner = self.runtime(config)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        with self.assertRaises(ModelWorkBlocked):
            runner._review_work_claims()
        self.assertEqual(runner._body(runner.analysis_records["W102"])["reason"], "The captured study examines recall timing.")
        self.assertTrue(runner._work_review_current("W102"))
        self.assertFalse(runner._work_review_current("W101"))
        self.assertIsNone(runner.survey_ref)
        with self.assertRaises(ModelWorkBlocked):
            runner._accept_survey()
        prompts = [prompt for _, prompt in self.model_contexts(runner.control, runner.store)
                   if prompt.get("phase") == "work_review" and prompt["entry"]["work_id"] == "W101"]
        self.assertEqual(len(prompts), runner.config["limits"]["max_rounds"])

    def test_cached_unknown_dispatch_never_defers_with_prior_contract_feedback(self):
        config = survey_config(self.endpoint)
        config["limits"]["max_rounds"] = 2
        runner = self.runtime(config)
        runner._initialize()
        runner._complete = lambda task_id: None
        job = {"name": "work-review-W101", "actor": "methods.work-reviewer",
               "assignment": {"phase": "work_review"},
               "validator": lambda value: (_ for _ in ()).throw(ValidationError("invalid checks"))}
        unknown = {"ok": False, "error": "worker timeout", "outcome_known": False, "usage": {}}
        calls = []
        def dispatch(specs, **kwargs):
            calls.append(specs)
            if len(calls) == 1:
                task_id = specs[0]["task_id"]
                runner.tasks.create(task_id, "production", {}, job["actor"])
                runner.tasks.admit(task_id, job["actor"])
                runner.tasks.transition(task_id, "running", job["actor"])
                execution = runner._publish(f"command/executions/{task_id}", "report", {}, job["actor"])
                return {task_id: {"ok": True, "record_ref": execution["artifact_ref"],
                    "result": {"text": "{}", "model": "fixture", "usage": {"model_calls": 1},
                               "elapsed_seconds": 0.01, "finish_reason": "stop"}}}
            return {spec["task_id"]: deepcopy(unknown) for spec in specs}
        deferred = []
        with patch.object(runner, "_call_batch", side_effect=dispatch):
            with self.assertRaises(ModelWorkBlocked):
                runner._models_checked([deepcopy(job)], on_contract_blocked=lambda *args: deferred.append(args))
        self.assertEqual(deferred, [])
        retained = next(row for row in ModelWorkCache(runner.store, runner._publish).entries()
                        if row.get("dispatch_failure") == unknown)
        self.assertEqual(retained["failure_origin"], "dispatch")
        self.assertEqual(retained["feedback"]["error"], "invalid checks")
        sibling = {"name": "work-review-W102", "actor": job["actor"],
                   "assignment": {"phase": "work_review", "work_id": "W102"}, "validator": lambda value: None}
        for status in ("blocked", "repairing"):
            for legacy in (False, True):
                cache = ModelWorkCache(runner.store, runner._publish)
                body = {key: value for key, value in retained.items()
                        if key != "cache_ref" and (not legacy or key not in {"failure_origin", "dispatch_failure"})}
                body["status"] = status
                cache.put(retained["cache_ref"].split("/")[-1].split("@")[0], body)
                with self.subTest(status=status, legacy=legacy), patch.object(runner, "_call_batch") as resumed:
                    with self.assertRaises(ModelWorkBlocked):
                        runner._models_checked([deepcopy(job), sibling],
                                              on_contract_blocked=lambda *args: deferred.append(args))
                    resumed.assert_not_called()
        self.assertEqual(deferred, [])

        cache = ModelWorkCache(runner.store, runner._publish)
        base_key = retained["cache_ref"].split("/")[-1].split("@")[0]
        cache.put(base_key, {"status": "blocked", "failure_origin": "response_validation",
            "failure_class": "model_contract", "repair_attempts": 2,
            "error": f"{job['name']} did not satisfy its evidence contract: invalid checks",
            "feedback": {"error": "invalid checks"}})
        runner.resume_session = {"session": 7}
        repair_assignment = runner._follow_up_assignment(job["assignment"])
        repair_assignment["_contract_repair_boundary"] = "model-contract-repair-7"
        repair_key = cache.key(scope=f"survey:{job['name']}", role=job["actor"], system=SYSTEM,
                               prompt=repair_assignment, model=runner.config["model"])
        cache.put(repair_key, {"status": "repairing", "failure_origin": "dispatch",
            "dispatch_failure": unknown, "repair_attempts": 1,
            "error": f"{job['name']}: worker timeout", "feedback": {"error": "invalid checks"}})
        with patch.object(runner, "_call_batch") as resumed:
            with self.assertRaises(ModelWorkBlocked):
                runner._models_checked([deepcopy(job), sibling],
                                      on_contract_blocked=lambda *args: deferred.append(args))
            resumed.assert_not_called()
        self.assertEqual(deferred, [])

    def test_explicit_resume_retries_only_known_dispatch_failures_and_keeps_checked_siblings(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize()
        runner._complete = lambda task_id: None
        job = {"name": "settled-worker-failure", "actor": "methods.evidence-verifier",
               "assignment": {"phase": "fixture"}, "validator": lambda value: None}
        sibling = {**job, "name": "checked-sibling"}
        cache = ModelWorkCache(runner.store, runner._publish)
        key = cache.key(scope=f"survey:{job['name']}", role=job["actor"],
                        system=SYSTEM, prompt=runner._follow_up_assignment(job["assignment"]), model=runner.config["model"])
        sibling_key = cache.key(scope=f"survey:{sibling['name']}", role=sibling["actor"],
                                system=SYSTEM, prompt=runner._follow_up_assignment(sibling["assignment"]), model=runner.config["model"])
        execution = runner._publish("command/executions/checked-sibling", "report", {}, job["actor"])
        cache.put(sibling_key, {"status": "succeeded", "value": {}, "execution_ref": execution["artifact_ref"]})
        for status in ("blocked", "repairing"):
            for known in (False, None, True):
                failure = {"ok": False, "error": "worker startup failed"}
                if known is not None:
                    failure["outcome_known"] = known
                state = {"status": status, "failure_origin": "dispatch", "dispatch_failure": failure,
                         "repair_attempts": 1, "error": failure["error"]}
                cache.put(key, state)
                runner.resume_session = None
                with patch.object(runner, "_call_batch") as dispatch, self.assertRaises(ModelWorkBlocked):
                    runner._models_checked([deepcopy(job), deepcopy(sibling)])
                dispatch.assert_not_called()
                runner.resume_session = {"session": 1}
                if known is not True:
                    with patch.object(runner, "_call_batch") as dispatch, self.assertRaises(ModelWorkBlocked):
                        runner._models_checked([deepcopy(job), deepcopy(sibling)])
                    dispatch.assert_not_called()
                    continue
                def succeed(specs, **kwargs):
                    self.assertEqual(len(specs), 1)
                    task_id = specs[0]["task_id"]
                    self.assertIn(job["name"], task_id)
                    result = runner._publish(f"command/executions/{task_id}", "report", {}, job["actor"])
                    return {task_id: {"ok": True, "record_ref": result["artifact_ref"],
                        "result": {"text": "{}", "model": "fixture", "usage": {"model_calls": 1},
                                   "elapsed_seconds": .01, "finish_reason": "stop"}}}
                with patch.object(runner, "_call_batch", side_effect=succeed):
                    results = runner._models_checked([deepcopy(job), deepcopy(sibling)])
                self.assertEqual(results[sibling["name"]][1], execution["artifact_ref"])
                self.assertEqual(cache.get(key)["status"], "succeeded")

    def test_follow_up_source_binding_repair_preserves_checked_sibling_and_frontier(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.runtime.model_work import ModelWorkCache
        config = survey_config(self.endpoint, "follow-up-source-binding")
        config["limits"]["max_rounds"] = 2
        orders = [self.follow_up_order(), {**self.follow_up_order(), "id": "evidence-2"}]
        first = self.runtime(config, work_orders=orders)
        failed = first.run()
        self.assertEqual(failed["failure"]["failure_class"], "model_contract")
        typed = ModelWorkBlocked(failed["error"], failure_class="model_contract")
        self.assertFalse(ComposerRunner._is_survey_evidence_contract_blocker({"kind": "survey"}, typed))
        self.assertTrue(ComposerRunner._is_survey_evidence_contract_blocker({"kind": "survey"}, failed["error"]))
        control, store = self.open_store()
        def publish(logical_id, artifact_type, body, author, *, subjects=()):
            return store.publish_artifact(logical_id=logical_id, artifact_type=artifact_type,
                                          body=canonical_bytes(body), author=author)
        cache = ModelWorkCache(store, publish)
        siblings = [row for row in cache.entries() if row.get("status") == "succeeded"
                    and isinstance(row.get("value"), dict) and "orders" in row["value"]]
        self.assertEqual(len(siblings), 1)
        sibling_execution = siblings[0]["execution_ref"]
        for state in cache.entries():
            if state.get("status") == "blocked" and "follow-up-disposition" in state.get("error", ""):
                key = state.pop("cache_ref").split("/")[-1].split("@")[0]
                state["failure_class"] = None
                cache.put(key, state)
        policy = {"additional_seconds": config["limits"]["wall_clock_seconds"],
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["follow_up"]}}
        second = self.runtime(config, resume_policy=policy, work_orders=orders)
        with patch.object(second, "_prepare_follow_up", side_effect=AssertionError("accepted frontier must remain")), \
             patch.object(second, "_setup", side_effect=AssertionError("no acquisition needed")):
            completed = second.run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertEqual(completed["survey_ref"], failed["survey_ref"])
        self.assertEqual(completed["assessment_ref"], failed["assessment_ref"])
        report = json.loads(store.read_body(store.get(completed["follow_up_result"]["ref"])["body_hash"]))
        self.assertEqual(report["execution_refs"][0], sibling_execution)
        repair_execution = store.get(report["execution_refs"][1])
        context = json.loads(store.read_body(store.get(repair_execution["inputs"][0]["ref"])["body_hash"]))
        assignment = json.loads(context["prompt"])
        self.assertTrue(assignment["evidence_catalog"])
        self.assertTrue(all("text" not in source for source in assignment["sources"]))
        self.assertNotIn("previous_response", assignment["validation_feedback"])
        _, _, _, review_params = SurveyGate(control, store)._recorded_execution(
            report["completion_execution_refs"][1], "methods.evidence-verifier", operation="model",
            task_kinds={"review"})
        review_assignment = json.loads(review_params["prompt"])
        self.assertEqual(review_assignment["evidence_context"]["evidence_catalog"],
                         assignment["evidence_catalog"])
        for order in orders:
            self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", completed, order))

    def test_runner_budget_failure_preserves_typed_fence_into_composer(self):
        from scisaurus.runtime.models import ModelBudgetExceededError
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.core.errors import QuotaExceededError
        runner = self.runtime()
        admission = {"path": str(self.root / "owner.sqlite"), "key": "stage",
                     "dimension": "input_tokens", "limit": 100,
                     "observed": 70, "reserved": 20, "requested": 11}
        error = ModelBudgetExceededError("admission rejected", budget_admission=admission,
                                        outcome_known=True, attempts=0)
        error.usage = {"input_tokens": 7}
        with patch.object(runner, "_setup", side_effect=error):
            result = runner.run()
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["failure"], error.failure_details())
        with self.assertRaises(ModelBudgetExceededError) as raised:
            ComposerRunner._raise_stage_failure(result)
        self.assertIsInstance(raised.exception, QuotaExceededError)
        self.assertEqual(raised.exception.budget_admission, admission)
        self.assertEqual(raised.exception.attempts, 0)
        self.assertEqual(raised.exception.usage, result["usage"])
        self.assertEqual(raised.exception.stage_result, result)

    def test_actual_undispatched_quota_skips_scientific_cache_and_explicit_resume_uses_current_capacity(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._accept_survey()
        runner._nominate()
        runner.config["limits"]["max_model_calls"] = 126
        runner.model_calls_dispatched = 126
        with self.assertRaises(QuotaExceededError) as stopped:
            runner._counter_plan()
        self.assertEqual(stopped.exception.limit, 126)
        self.assertEqual(stopped.exception.observed, 126)
        self.assertEqual(runner.model_calls_dispatched, 126)
        cache = ModelWorkCache(runner.store, runner._publish)
        resources = [entry for entry in cache.entries() if entry.get("status") == "resource_blocked"]
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0]["dispatch_failure"]["error_type"], "quota")
        self.assertNotIn("repair_attempts", resources[0])
        with self.assertRaises(QuotaExceededError):
            runner._counter_plan()
        self.assertEqual(runner.model_calls_dispatched, 126)
        runner.resume_session = {"session": 1, "reopened_scopes": ["gap_assessment"]}
        runner.config["limits"]["max_model_calls"] = 640
        value, _ = runner._counter_plan()
        self.assertEqual(value["queries"], ["prior solution"])
        self.assertEqual(runner.model_calls_dispatched, 127)

    def test_required_model_manifest_tracks_current_review_basis_and_actual_downstream_roles(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        manifest = runner._required_model_work()
        self.assertIn("work-review-W101", [job["name"] for job in manifest])
        self.assertEqual(next(job for job in manifest if job["name"] == "gap-assessment")["actor"],
                         "methods.novelty-verifier")
        runner._review_work_claims()
        self.assertNotIn("work-review-W101", [job["name"] for job in runner._required_model_work()])
        with patch.object(runner, "_review_protocol_matches", return_value=False):
            self.assertIn("work-review-W101", [job["name"] for job in runner._required_model_work()])
        old = runner.analysis_records["W101"]
        changed = runner._body(old)
        changed["reason"] = "The evidence is relevant to the measurement method."
        runner.analysis_records["W101"] = runner._record("kb/work-analyses/W101", "note", changed,
            "research.literature-mapper", subjects=runner.analyzed_basis["W101"])
        self.assertIn("work-review-W101", [job["name"] for job in runner._required_model_work()])

    def test_required_manifest_reserves_one_actual_disposition_per_uncompleted_work_order(self):
        first = self.follow_up_order()
        second = {**first, "id": "evidence-2", "objective": "Locate the measurement uncertainty."}
        runner = self.runtime(work_orders=[first, second])
        runner._initialize()
        dispositions = [job for job in runner._required_model_work() if job["phase"] == "survey_follow_up"]
        self.assertEqual([job["work_order_id"] for job in dispositions], [first["id"], second["id"]])
        first_identity = hashlib.sha256(canonical_bytes(first)).hexdigest()
        self.assertEqual(dispositions[0]["name"], f"follow-up-disposition-{first_identity}")
        runner._follow_up_decisions.add(first_identity)
        dispositions = [job for job in runner._required_model_work() if job["phase"] == "survey_follow_up"]
        self.assertEqual([job["work_order_id"] for job in dispositions], [second["id"]])

    def test_development_mapping_does_not_create_budget_abstention(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner.config["limits"]["max_model_calls"] = 1
        runner.model_calls_dispatched = 100
        job = runner._map_job("W101", runner._analysis_basis("W101"))
        with patch.dict("os.environ", {"SCISAURUS_EXECUTION_POLICY": "development"}):
            self.assertEqual(runner._allocate_model_wave([job]), [job])
        self.assertIsNone(runner.store.head("command/survey-abstentions/W101"))
        self.assertIsNone(runner.store.head(f"command/survey-resource-debts/{job['name']}"))

    def test_optional_mapping_reserves_materialized_downstream_decisions_and_retains_sources(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        source_bytes = deepcopy(runner.source_docs)
        manifest = runner._required_model_work()
        runner.config["limits"]["max_model_calls"] = runner.model_calls_dispatched + len(manifest)
        job = runner._map_job("W101", runner._analysis_basis("W101"))
        before = runner.model_calls_dispatched
        self.assertEqual(runner._allocate_model_wave([job]), [])
        self.assertEqual(runner.model_calls_dispatched, before)
        self.assertEqual(runner.source_docs, source_bytes)
        entry = runner._body(runner.analysis_records["W101"])
        self.assertTrue(all(entry[field]["text"] is None for field in MAP_FIELDS))
        self.assertEqual(runner._body(runner.store.head("command/survey-abstentions/W101"))["scope"], "model_call_budget")
        self.assertTrue(runner._is_deferred_analysis("W101"))
        debt = runner._body(runner.store.head(f"command/survey-resource-debts/{job['name']}"))
        self.assertEqual(debt["required_calls"], len(manifest) + 1)
        self.assertEqual(debt["induced_review_work_ids"], ["W101"])
        self.assertTrue(all(item["input_tokens"] is None for item in debt["required_jobs"]))
        runner.config["limits"]["max_model_calls"] = before + len(runner._required_model_work()) + 2
        self.assertEqual(runner._allocate_model_wave([job]), [job])

    def test_budget_reservation_never_erases_existing_claims_or_double_counts_required_retry(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        old = deepcopy(runner.analysis_records["W101"])
        runner.config["limits"]["max_model_calls"] = runner.model_calls_dispatched
        job = runner._map_job("W101", runner._analysis_basis("W101"))
        with self.assertRaises(QuotaExceededError) as stopped:
            runner._allocate_model_wave([job], repairing={job["name"]})
        self.assertEqual(runner.analysis_records["W101"], old)
        self.assertTrue(stopped.exception.diagnostics[0]["required_jobs"])
        required = runner._required_model_work()
        runner.config["limits"]["max_model_calls"] = runner.model_calls_dispatched + len(required)
        review = {"name": "work-review-W101", "actor": "methods.work-reviewer", "assignment": {"phase": "work_review"}}
        self.assertEqual(runner._allocate_model_wave([review], repairing={review["name"]}), [review])

    def test_all_budget_deferred_claims_remain_resource_failure_without_masking_other_abstentions(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        sources = deepcopy(runner.source_docs)
        runner.config["limits"]["max_model_calls"] = runner.model_calls_dispatched + len(runner._required_model_work())
        before = runner.model_calls_dispatched
        with self.assertRaises(QuotaExceededError) as stopped:
            runner._accept_survey()
        self.assertEqual(runner.model_calls_dispatched, before)
        self.assertEqual(runner.source_docs, sources)
        self.assertTrue(stopped.exception.diagnostics[0]["budget_abstentions"][0]["debt_refs"])
        self.assertTrue(stopped.exception.diagnostics[0]["required_jobs"])
        runner._materialize_source_less_map("W101", runner._analysis_basis("W101"), scope="source_unavailable")
        with self.assertRaises(ModelWorkBlocked):
            runner._accept_survey()
        runner._materialize_source_less_map("W101", runner._analysis_basis("W101"), scope="review_exhausted")
        with self.assertRaises(ModelWorkBlocked):
            runner._accept_survey()
        with patch.object(runner, "_setup", side_effect=stopped.exception):
            result = runner.run()
        self.assertEqual(result["failure"]["kind"], "quota_exceeded")
        self.assertEqual(result["failure"]["diagnostics"], stopped.exception.diagnostics)
        self.assertEqual(result["status"], "blocked")

    def test_model_budget_admission_survives_checked_dispatch_and_run_result(self):
        from scisaurus.runtime.models import ModelBudgetExceededError
        runner = self.runtime()
        admission = {"path": str(self.root / "owner.sqlite"), "key": "stage", "dimension": "input_tokens", "limit": 100, "observed": 90, "reserved": 0, "requested": 11}
        failure = {"ok": False, "error": "owned token budget exhausted", "error_type": "ModelBudgetExceededError",
                   "budget_admission": admission, "outcome_known": True, "attempts": 0, "usage": {"input_tokens": 7}}
        def fail_setup():
            with patch.object(runner, "_call_batch", side_effect=lambda specs, **kwargs: {spec["task_id"]: deepcopy(failure) for spec in specs}):
                runner._model_checked("counter-plan", "methods.novelty-challenger", {"phase": "counter_plan"}, lambda value: None)
        with patch.object(runner, "_setup", side_effect=fail_setup):
            result = runner.run()
        self.assertEqual(result["failure"]["budget_admission"], admission)
        self.assertEqual(result["failure"]["kind"], "model_call")
        control, store = self.open_store()
        cache = ModelWorkCache(store, lambda *args, **kwargs: None)
        resource = next(entry for entry in cache.entries() if entry.get("status") == "resource_blocked")
        self.assertEqual(resource["dispatch_failure"], failure)
        self.assertNotIn("repair_attempts", resource)

    def test_owned_legacy_cache_recovers_exact_typed_failure_without_reusing_other_inputs(self):
        from scisaurus.runtime.models import ModelBudgetExceededError
        runner = self.runtime()
        runner._initialize()
        runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._accept_survey()
        runner._nominate()
        assignment = {"phase": "counter_plan", "nomination_ref": runner.nomination_record["artifact_ref"],
                      "gap": runner.nomination}
        assignment = runner._follow_up_assignment(assignment)
        role, name = "methods.novelty-challenger", "counter-plan"
        cache = ModelWorkCache(runner.store, runner._publish)
        key = cache.key(scope=f"survey:{name}", role=role, system=SYSTEM,
                        prompt=assignment, model=runner.config["model"])
        cache.put(key, {"status": "repairing", "repair_attempts": 1, "error": "invalid JSON"})
        context = runner._record("command/contexts/survey-counter-plan-999", "note", {
            "role": role, "client": runner.config["model"], "_routing_client": runner.config["model"],
            "prompt": json.dumps({**assignment, "validation_feedback": {"error": "invalid JSON"}})}, role)
        admission = {"path": str(self.root / "budget.sqlite"), "key": "stage", "dimension": "model_calls",
                     "limit": 96, "observed": 96, "reserved": 0, "requested": 1}
        failure = {"error": "model call budget exhausted: stage", "error_type": "ModelBudgetExceededError",
                   "budget_admission": admission, "outcome_known": True, "attempts": 0, "usage": {}}
        failure_record = runner._record("command/failures/survey-counter-plan-999", "report", failure, role,
                                       subjects=[context["artifact_ref"]])
        retained = cache.put(key, {"status": "blocked", "repair_attempts": 2,
            "feedback": {"error": "invalid JSON"}, "error": f"{name}: {failure['error']}"})
        recovered = runner._legacy_resource_dispatch_failure(retained, name)
        self.assertEqual(recovered["budget_admission"], admission)
        self.assertEqual(recovered["failure_ref"], failure_record["artifact_ref"])
        with patch.object(runner, "_call_batch") as dispatch:
            with self.assertRaises(ModelBudgetExceededError):
                runner._model_checked(name, role, assignment, runner._plan_validator)
            dispatch.assert_not_called()
        self.assertIsNone(runner._legacy_resource_dispatch_failure({**retained, "error": "another failure"}, name))
        mismatch_key = cache.key(scope=f"survey:{name}", role=role, system=SYSTEM,
                                 prompt={**assignment, "question": "Other question"}, model=runner.config["model"])
        mismatch = cache.put(mismatch_key, {"status": "blocked", "error": retained["error"]})
        self.assertIsNone(runner._legacy_resource_dispatch_failure(mismatch, name))
        runner.resume_session = {"session": 1, "reopened_scopes": ["gap_assessment"]}
        result, _ = runner._model_checked(name, role, assignment, runner._plan_validator)
        self.assertEqual(result["queries"], ["prior solution"])

    def test_unknown_field_cannot_be_reviewed_as_an_absence_claim(self):
        from scisaurus.runtime.survey_records import validate_work_review
        entry = {field: {"text": None, "evidence": []} for field in MAP_FIELDS}
        value = {"checks": check_rows(["inclusion", "reason", *MAP_FIELDS]), "rationale": "Only asserted facts are audited."}
        validate_work_review(value, [], entry=entry)
        next(check for check in value["checks"] if check["check_id"] == "limitations")["outcome"] = "insufficient_evidence"
        with self.assertRaises(ModelContractError):
            validate_work_review(value, [], entry=entry)

    def test_scientific_review_allowance_belongs_to_each_unchanged_work(self):
        runner = self.runtime()
        runner._initialize()
        runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        runner.config["model"]["model"] = "review-forced-reason-failure"
        def adverse_review(wid, number):
            entry_ref = runner.analysis_records[wid]["artifact_ref"]
            assignment = {"phase": "work_review", "entry_ref": entry_ref,
                          "entry": runner._body(runner.analysis_records[wid]),
                          "required_checks": list(work_review_checks([])),
                          "review_contract": source_fidelity_review_contract()}
            value, execution = runner._model_checked(f"scope-test-review-{wid}-{number}",
                "methods.work-reviewer", assignment, lambda value: validate_work_review(value, []),
                stage="unit_review", task_kind="verification")
            return {"entry_ref": entry_ref, "relationship_refs": [],
                    "review_protocol": source_fidelity_review_contract()["protocol"],
                    "evidence_scope": runner._review_evidence_scope(wid),
                    "execution_ref": execution, **value}
        for wid, count in (("W101", 3), ("W102", 1)):
            for number in range(count):
                runner._record(f"kb/work-reviews/{wid}", "note", adverse_review(wid, number), "methods.work-reviewer")
        self.assertEqual(runner._work_review_failure_count("W101"), 3)
        self.assertEqual(runner._work_review_failure_count("W102"), 1)
        old = runner.analysis_records["W102"]
        candidate = runner._body(old)
        candidate["reason"] = "A revised screening rationale."
        runner.analysis_records["W102"] = runner._record("kb/work-analyses/W102", "note", candidate,
            "research.literature-mapper", subjects=runner.analyzed_basis["W102"])
        self.assertEqual(runner._work_review_failure_count("W102"), 1)
        source = runner._record("kb/abstract/W102", "source_capture", {"work_id": "W102", "abstract": "New evidence."},
                                "research.cataloger")
        runner.analyzed_basis["W102"].append(source["artifact_ref"])
        self.assertEqual(runner._work_review_failure_count("W102"), 0)
        runner.analyzed_basis["W102"].pop()
        removed = next(ref for ref in runner.analyzed_basis["W102"]
                       if runner.store.get(ref)["artifact_type"] == "source_capture")
        runner.analyzed_basis["W102"].remove(removed)
        self.assertEqual(runner._work_review_failure_count("W102"), 0)
        runner.analyzed_basis["W102"].append(removed)
        relation = runner._record("kb/relationships/scope-test", "note", {"source": "W102", "target": "W101"},
            "research.literature-mapper", subjects=[runner.analysis_records["W102"]["artifact_ref"],
                                                  runner.work_records["W101"]["artifact_ref"]])
        runner.relationships["scope-test"] = {"source": "W102", "target": "W101", "artifact_ref": relation["artifact_ref"]}
        runner._record("kb/work-reviews/W102", "note", adverse_review("W102", "changed"), "methods.work-reviewer")
        self.assertEqual(runner._work_review_failure_count("W102"), 1)
        target_identity = runner._record("kb/identities/W101", "reference_card", {"work_id": "W101"}, "research.cataloger")
        runner.identity_records["W101"] = target_identity
        self.assertEqual(runner._work_review_failure_count("W102"), 0)

    def test_focused_review_audits_supported_claim_without_requiring_question_answer(self):
        runner = self.runtime(survey_config(self.endpoint, "review-question-adversary"))
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        entry = runner.analysis_records["W101"]
        retained = runner._body(entry)
        runner._review_work_claims()
        self.assertEqual(runner.analysis_records["W101"]["artifact_ref"], entry["artifact_ref"])
        self.assertEqual(runner._body(entry), retained)
        review = runner._body(runner.work_reviews["W101"])
        self.assertTrue(all(check["outcome"] == "passed" for check in review["checks"]))
        self.assertEqual(review["review_protocol"], source_fidelity_review_contract()["protocol"])
        self.assertTrue(runner._review_protocol_matches(review))
        self.assertEqual(runner._work_review_failure_count("W101"), 0)
        prompt = next(prompt for _, prompt in self.model_contexts(runner.control, runner.store)
                      if prompt.get("phase") == "work_review")
        self.assertEqual(prompt["review_contract"], source_fidelity_review_contract())
        self.assertIn("partial or general claim", prompt["review_contract"]["screening_scope"])
        self.assertIn("Fail unsupported minor clauses", prompt["review_contract"]["failure_basis"])
        self.assertEqual(prompt["question"], runner.score["question"])
        self.assertIsNone(runner.store.head("kb/gap-assessments/current"))

    def test_old_focused_review_protocol_cannot_gain_failure_credit_by_outer_retagging(self):
        runner = self.runtime(survey_config(self.endpoint, "review-forced-reason-failure"))
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        entry_ref = runner.analysis_records["W101"]["artifact_ref"]
        assignment = {"phase": "work_review", "entry_ref": entry_ref,
                      "entry": runner._body(runner.analysis_records["W101"]),
                      "required_checks": list(work_review_checks([]))}
        value, execution = runner._model_checked("legacy-review-protocol", "methods.work-reviewer", assignment,
            lambda value: validate_work_review(value, []), stage="unit_review", task_kind="verification")
        legacy_scope = runner._review_evidence_scope("W101")
        legacy_scope.pop("review_protocol")
        legacy = {"entry_ref": entry_ref, "relationship_refs": [], "execution_ref": execution,
                  "evidence_scope": legacy_scope, **value}
        runner._record("kb/work-reviews/W101", "note", legacy, "methods.work-reviewer")
        self.assertFalse(runner._review_protocol_matches(legacy))
        self.assertEqual(runner._work_review_failure_count("W101"), 0)
        forged = {**legacy, "review_protocol": source_fidelity_review_contract()["protocol"],
                  "evidence_scope": runner._review_evidence_scope("W101")}
        runner._record("kb/work-reviews/W101", "note", forged, "methods.work-reviewer")
        self.assertFalse(runner._review_protocol_matches(forged))
        self.assertEqual(runner._work_review_failure_count("W101"), 0)
        assignment["review_contract"] = source_fidelity_review_contract()
        value, execution = runner._model_checked("current-review-protocol", "methods.work-reviewer", assignment,
            lambda value: validate_work_review(value, []), stage="unit_review", task_kind="verification")
        current = {**forged, "execution_ref": execution, **value}
        runner._record("kb/work-reviews/W101", "note", current, "methods.work-reviewer")
        self.assertTrue(runner._review_protocol_matches(current))
        self.assertEqual(runner._work_review_failure_count("W101"), 1)
        runner.config["model"]["model"] = "pass"
        assignment.pop("review_contract")
        passed, old_execution = runner._model_checked("legacy-passed-review-protocol", "methods.work-reviewer", assignment,
            lambda value: validate_work_review(value, []), stage="unit_review", task_kind="verification")
        old_pass = {**legacy, "execution_ref": old_execution, **passed}
        self.assertTrue(all(check["outcome"] == "passed" for check in old_pass["checks"]))
        runner._record("kb/work-reviews/W101", "note", old_pass, "methods.work-reviewer",
                       subjects=[entry_ref, *runner.analyzed_basis["W101"]])
        retained_analysis = runner.analysis_records["W101"]["artifact_ref"]
        retained_sources = deepcopy(runner.source_docs)
        runner.resume_session = {"reopened_scopes": []}
        runner._restore()
        self.assertNotIn("W101", runner.reviewed_basis)
        self.assertEqual(runner.analysis_records["W101"]["artifact_ref"], retained_analysis)
        self.assertEqual(runner.source_docs, retained_sources)

    def test_current_review_protocol_credit_replays_normalized_extra_diagnostics(self):
        runner = self.runtime(survey_config(self.endpoint, "review-current-diagnostic"))
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        entry_ref = runner.analysis_records["W101"]["artifact_ref"]
        assignment = {"phase": "work_review", "entry_ref": entry_ref,
                      "entry": runner._body(runner.analysis_records["W101"]),
                      "required_checks": list(work_review_checks([])),
                      "review_contract": source_fidelity_review_contract()}
        for failed in (False, True):
            runner.config["model"]["model"] = "review-current-diagnostic-failure" if failed else "review-current-diagnostic"
            value, execution = runner._model_checked(f"diagnostic-review-{failed}", "methods.work-reviewer", assignment,
                lambda value: validate_work_review(value, []),
                normalizer=lambda value: normalize_check_envelope(value, work_review_checks([])),
                stage="unit_review", task_kind="verification")
            _, _, _, raw = runner.gate._model_review_execution(execution, "methods.work-reviewer")
            self.assertEqual(len(raw["checks"]), len(value["checks"]) + 1)
            body = {"entry_ref": entry_ref, "relationship_refs": [], "execution_ref": execution,
                    "review_protocol": source_fidelity_review_contract()["protocol"],
                    "evidence_scope": runner._review_evidence_scope("W101"), **value}
            runner._record("kb/work-reviews/W101", "note", body, "methods.work-reviewer",
                           subjects=[entry_ref, *runner.source_docs])
            self.assertTrue(runner._review_protocol_matches(body))
            self.assertEqual(runner._work_review_failure_count("W101"), int(failed))
            evidence = runner.gate._model_review_execution(execution, "methods.work-reviewer")
            for malformed in (raw["checks"][1:], [*raw["checks"], raw["checks"][0]]):
                invalid = {**raw, "checks": malformed}
                with patch.object(runner.gate, "_model_review_execution", return_value=(*evidence[:-1], invalid)):
                    self.assertFalse(runner._review_protocol_matches({**body, "checks": malformed}))
            if not failed:
                runner.resume_session = {"reopened_scopes": []}
                runner._restore()
                self.assertIn("W101", runner.reviewed_basis)
                self.assertEqual(runner.work_reviews["W101"]["artifact_ref"], runner.store.head("kb/work-reviews/W101")["artifact_ref"])

    def test_independent_model_and_admission_ignore_unrelated_retrieval_bindings(self):
        runner = SurveyRunner.__new__(SurveyRunner)
        runner.bindings = {"bibliography": "stale", "full_text": "degraded"}
        runner.operations = unittest.mock.Mock()
        runner._ensure_active = unittest.mock.Mock()
        runner._before_dispatch({"kind": "model", "params": {"prompt": '{"phase":"map"}'}})
        runner._admission_guard()
        runner.operations.authorize.assert_not_called()
        self.assertEqual(runner._ensure_active.call_count, 3)

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SurveyHTTPFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/works"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-survey-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        SurveyHTTPFixture.requests.clear()
        SurveyHTTPFixture.refresh_target = False
        SurveyHTTPFixture.rate_limit_once = None
        SurveyHTTPFixture.identity_rate_limit_once = None
        SurveyHTTPFixture.locations_by_work = {}
        SurveyHTTPFixture.robots_disallow = None

    def test_check_envelope_projection_drops_extra_rows_without_reordering(self):
        rows = [
            {"check_id": "source-fidelity", "outcome": "passed", "method": "m", "result": "r"},
            {"check_id": "question:stochastic_resonance_peak", "outcome": "passed",
             "method": "extra", "result": "extra"},
            {"check_id": "coverage-accounting", "outcome": "passed", "method": "m", "result": "r"},
            {"check_id": "map-support", "outcome": "passed", "method": "m", "result": "r"},
        ]
        value = {"checks": rows, "rationale": "bounded"}
        projected = normalize_check_envelope(value, SURVEY_CHECKS)
        self.assertEqual([row["check_id"] for row in projected["checks"]],
                         ["source-fidelity", "coverage-accounting", "map-support"])
        incomplete = {"checks": rows[:2], "rationale": "bounded"}
        self.assertIs(normalize_check_envelope(incomplete, SURVEY_CHECKS), incomplete)

    def test_gap_assessment_aliases_are_canonicalized_conservatively(self):
        value = {
            "status": "supported",
            "answer": "The available abstracts do not establish the proposed distinction.",
            "uncertainty": "full-text evidence is unavailable",
            "comparisons": [{
                "work_id": "W1", "relationship": "closest_prior_work",
                "note": "The source is related but does not test the proposed threshold.",
                "evidence": [{"evidence_id": "ev-1", "role": "support",
                              "note": "captured source", "work_id": "W1"}],
            }],
            "checks": [{
                "name": check_id,
                "outcome": "insufficient_evidence" if check_id == "full-text-support" else "passed",
                "rationale": f"Assessment for {check_id}.",
            } for check_id in GAP_CHECKS],
            "evidence": [{"evidence_id": "ev-1", "role": "primary", "note": "catalog pointer"}],
        }

        normalized = normalize_gap_assessment_envelope(value)

        self.assertEqual(normalized["state"], "insufficient_evidence")
        self.assertIn("Uncertainty: full-text evidence is unavailable", normalized["rationale"])
        self.assertEqual([row["check_id"] for row in normalized["checks"]], list(GAP_CHECKS))
        self.assertTrue(all(set(row) == {"check_id", "outcome", "method", "result"}
                            for row in normalized["checks"]))
        self.assertEqual(normalized["comparisons"][0]["relationship"], "uncertain")
        self.assertEqual(normalized["comparisons"][0]["statement"], value["comparisons"][0]["note"])
        self.assertEqual(normalized["comparisons"][0]["evidence"], [{"evidence_id": "ev-1"}])
        self.assertEqual(normalized["evidence"], [{"evidence_id": "ev-1"}])
        self.assertNotIn("check_id", value["checks"][0])

    def test_gap_assessment_cross_work_citation_becomes_uncertain_without_retry(self):
        catalog = [
            {"evidence_id": "ev-w1", "work_id": "W1", "source_ref": "source-w1"},
            {"evidence_id": "ev-w2", "work_id": "W2", "source_ref": "source-w2"},
        ]
        value = {
            "state": "eligible_for_experiment",
            "rationale": "The proposed comparison appears novel.",
            "comparisons": [{
                "work_id": "W1", "relationship": "partial",
                "statement": "This work partially addresses the mechanism.",
                "evidence": ["ev-w2"],
            }],
            "checks": [{
                "check_id": check_id, "outcome": "passed",
                "method": "Compared the displayed source record.",
                "result": "No direct solution was identified.",
            } for check_id in GAP_CHECKS],
            "evidence": [{"evidence_id": "ev-w2"}],
        }

        normalized = normalize_gap_assessment_envelope(
            value, evidence_catalog=catalog,
            verified_full_text_refs={"source-w2"},
            known_work_ids={"W1", "W2"},
        )

        self.assertEqual(normalized["state"], "insufficient_evidence")
        self.assertEqual(normalized["comparisons"], [{
            "work_id": "W1", "relationship": "uncertain",
            "statement": "The supplied source evidence does not resolve this work's relationship to the nominated gap.",
            "evidence": [],
        }])

    def test_gap_assessment_missing_or_duplicate_checks_are_explicitly_unresolved(self):
        normalized = normalize_gap_assessment_envelope({
            "status": "supported",
            "comparisons": [],
            "checks": [{
                "check_id": "coverage-accounting", "outcome": "passed",
                "method": "m", "result": "r",
            }, {
                "check_id": "coverage-accounting", "outcome": "passed",
                "method": "m2", "result": "r2",
            }],
        }, known_work_ids=set())
        self.assertEqual(normalized["state"], "insufficient_evidence")
        self.assertEqual([row["check_id"] for row in normalized["checks"]], list(GAP_CHECKS))
        self.assertTrue(all(row["outcome"] == "insufficient_evidence"
                            for row in normalized["checks"]))

    def test_gap_assessment_alias_normalizer_marks_malformed_checks_unresolved(self):
        malformed = {"status": "supported", "checks": [{"name": [], "outcome": "passed"}]}
        normalized = normalize_gap_assessment_envelope(malformed)
        self.assertEqual([row["check_id"] for row in normalized["checks"]], list(GAP_CHECKS))
        self.assertTrue(all(row["outcome"] == "insufficient_evidence"
                            for row in normalized["checks"]))
        self.assertEqual(normalized["state"], "insufficient_evidence")

    def test_balanced_query_limit_preserves_capacity_for_independent_families(self):
        from scisaurus.runtime.survey import SurveyRunner
        self.assertEqual(SurveyRunner._balanced_query_limit(21, 12, 50), 2)
        self.assertEqual(SurveyRunner._balanced_query_limit(10, 1, 2), 2)
        self.assertEqual(SurveyRunner._balanced_query_limit(0, 3, 50), 1)

    def test_map_projection_keeps_large_corpus_inside_declared_context(self):
        runner = self.runtime()
        runner.config["model"].update({
            "context_window_tokens": 65536,
            "max_input_tokens": 56000,
            "max_output_tokens": 8192,
        })
        runner.bounds["context_chars"] = 30000
        runner.bounds["max_text_chars"] = 400000
        runner.works = {}
        runner.source_docs = {}
        runner.aliases = {}
        runner.analysis_records = {}
        runner.analyzed_basis = {}
        runner.relationships = {}
        runner.identity_records = {}
        for index in range(80):
            wid = f"W{index:03d}"
            runner.works[wid] = {
                "id": wid, "title": f"Study {wid}", "year": 2020,
                "doi": None, "publication_metadata_status": "provider_reported",
                "referenced_works": [],
            }
            if index == 0:
                runner.source_docs[f"source-{wid}-full"] = {
                    "work_id": wid, "representation": "full_text", "identity_verified": True,
                    "identity_checks": {"title_match": True, "section_markers": ["Results"]},
                    "text": "Owner evidence. " * 2200,
                }
            else:
                runner.source_docs[f"source-{wid}-abstract"] = {
                    "work_id": wid, "representation": "abstract",
                    "text": f"Comparison evidence for {wid}. " * 300,
                }
        runner.works["W000"]["referenced_works"] = ["W070"]
        with patch.object(runner, "_analysis_selection", return_value={f"W{index:03d}" for index in range(5)}):
            assignment = runner._map_job("W000", ["artifact:work-W000@1"])["assignment"]
        estimate = estimate_input_tokens(SYSTEM, json.dumps(assignment, ensure_ascii=False))
        self.assertLessEqual(estimate, 56000)
        owner = next(source for source in assignment["sources"] if source["work_id"] == "W000")
        comparisons = [source for source in assignment["sources"] if source["work_id"] != "W000"]
        self.assertEqual(owner["text"], runner.source_docs["source-W000-full"]["text"])
        self.assertEqual(owner["window"], {"start": 0, "end": len(owner["text"])})
        self.assertEqual({source["work_id"] for source in comparisons}, {"W001", "W002", "W003", "W004", "W070"})
        self.assertEqual({work["id"] for work in assignment["works"]}, {"W000", "W001", "W002", "W003", "W004", "W070"})
        self.assertLessEqual(sum(len(source["text"]) for source in comparisons), 60000)
        runner.control.close()

    def test_map_projection_compacts_catalog_when_route_limit_is_reached(self):
        runner = self.runtime()
        runner.config["model"].update({
            "context_window_tokens": 65536,
            "max_input_tokens": 56000,
            "max_output_tokens": 8192,
        })
        runner.bounds["context_chars"] = 30000
        runner.bounds["max_text_chars"] = 400000
        runner.works = {}
        runner.source_docs = {}
        runner.aliases = {}
        runner.analysis_records = {}
        runner.analyzed_basis = {}
        runner.relationships = {}
        runner.identity_records = {}
        for index in range(165):
            wid = f"W{index:03d}"
            runner.works[wid] = {
                "id": wid, "title": f"Study {wid} " + ("verbose catalog metadata " * 24),
                "year": 2020, "doi": None, "publication_metadata_status": "provider_reported",
                "referenced_works": [],
            }
            if index == 0:
                runner.source_docs[f"source-{wid}-full"] = {
                    "work_id": wid, "representation": "full_text", "identity_verified": True,
                    "identity_checks": {"title_match": True, "section_markers": ["Results"]},
                    "text": "Owner evidence. " * 2200,
                }
            else:
                runner.source_docs[f"source-{wid}-abstract"] = {
                    "work_id": wid, "representation": "abstract",
                    "text": f"Comparison evidence for {wid}. " * 300,
                }
        assignment = runner._map_job("W000", ["artifact:work-W000@1"])["assignment"]
        estimate = estimate_input_tokens(SYSTEM, json.dumps(assignment, ensure_ascii=False))
        self.assertLessEqual(estimate, 56000)
        self.assertLess(len(assignment["works"]), 165)
        self.assertTrue(any(source["work_id"] == "W000" for source in assignment["sources"]))
        self.assertLessEqual(
            estimate_input_tokens(SYSTEM, json.dumps(assignment, ensure_ascii=False)), 56000)
        runner.control.close()

    def test_gap_assessment_projection_fits_large_source_inventory(self):
        runner = self.runtime()
        runner.config["model"].update({
            "context_window_tokens": 65536,
            "max_input_tokens": 56000,
            "max_output_tokens": 8192,
        })
        sources = [{
            "source_ref": f"source-{index}", "work_id": f"W{index:03d}",
            "representation": "abstract", "identity_verified": False,
            "text": (f"Abstract evidence {index}. " * 500),
            "available_chars": 12000, "window": {"start": 0, "end": 12000},
        } for index in range(165)]
        sources.append({
            "source_ref": "source-full", "work_id": "W000",
            "representation": "full_text", "identity_verified": True,
            "text": "Full text evidence. " * 10000,
            "available_chars": 200000, "window": {"start": 0, "end": 200000},
        })
        assignment = {
            "assignment": "assess", "phase": "gap_assessment", "question": "question",
            "gap": {"id": "gap", "statement": "statement"},
            "nomination_ref": "nomination", "survey_ref": "survey",
            "prerequisite_survey_ref": "survey", "map": {"entries": []},
            "coverage": {
                "unique_works": 165, "abstracts": 165, "verified_full_texts": 1,
                "access_and_limit_gaps": [],
                "searches": [{"request": {"query": "x"}, "new_work_ids": ["W001"]}]
                             * 100,
                "expansion": [],
            },
            "sources": sources, "verified_full_text_refs": ["source-full"],
            "required_checks": ["coverage"], "allowed_check_outcomes": ["passed"],
            "instructions": "return a bounded assessment",
        }
        projected = runner._fit_assessment_assignment(assignment)
        self.assertLessEqual(
            estimate_input_tokens(SYSTEM, json.dumps(projected, ensure_ascii=False)), 56000)
        self.assertTrue(projected["sources"])
        self.assertEqual(projected["verified_full_text_refs"], ["source-full"])
        runner.control.close()

    def test_gap_context_keeps_late_map_evidence_visible_under_budget(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        text = "Background. " * 1500 + "The late decisive observation." + " Continuation." * 1500
        runner.source_docs = {"source": {"work_id": "W1", "representation": "abstract", "text": text}}
        proof = {"work_id": "W1", "source_ref": "source", "quote": "The late decisive observation."}
        assignment = {"map": {"entries": [{"finding": {"text": "A bounded result.", "evidence": [proof]}}]},
                      "sources": runner._assessment_source_context(),
                      "coverage": {"source_windows": [{"window": {"start": 0, "end": 700}}]}}
        original = deepcopy(assignment)
        with patch.object(runner, "_map_input_limit", return_value=3500):
            projected = runner._fit_assessment_assignment(assignment)
        self.assertEqual(assignment, original)
        self.assertLessEqual(estimate_input_tokens(SYSTEM, json.dumps(projected, ensure_ascii=False)), 3500)
        source = projected["sources"][0]
        self.assertGreater(source["window"]["start"], 0)
        self.assertIn(proof["quote"], source["text"])
        self.assertEqual(source["text"], text[source["window"]["start"]:source["window"]["end"]])
        self.assertEqual(projected["coverage"]["source_windows"][0]["window"], source["window"])
        expanded = expand_evidence(projected["map"], projected["evidence_catalog"], runner.source_docs,
                                   windows={"source": source["window"]})
        self.assertEqual(expanded, bind(assignment["map"], runner.source_docs))
        assessment = {"state": "insufficient_evidence", "rationale": "Bounded source support.",
                      "comparisons": [], "checks": check_rows(GAP_CHECKS),
                      "evidence": expanded["entries"][0]["finding"]["evidence"]}
        validate_assessment(assessment, runner.source_docs, {"W1"}, require_spans=True,
                            windows={"source": source["window"]})
        with self.assertRaisesRegex(ValidationError, "outside"):
            validate_assessment(assessment, runner.source_docs, {"W1"}, require_spans=True,
                                windows={"source": {"start": 0, "end": 700}})
        clipped = runner._project_source_window(source, 10)
        self.assertEqual(clipped["window"]["start"], source["window"]["start"])
        self.assertEqual(clipped["text"], text[clipped["window"]["start"]:clipped["window"]["end"]])

    def test_gap_context_coverage_matches_unprojected_sources(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner.source_docs = {"source": {"work_id": "W1", "representation": "abstract", "text": "Short source."}}
        sources = runner._assessment_source_context()
        assignment = {"map": {}, "sources": sources, "coverage": {"source_windows": []}}
        with patch.object(runner, "_map_input_limit", return_value=None):
            projected = runner._fit_assessment_assignment(assignment)
        self.assertEqual(projected["coverage"]["source_windows"], [
            {key: sources[0][key] for key in ("source_ref", "available_chars", "window")}])

    def test_gap_context_does_not_cut_mandatory_evidence_to_fit(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        text = "start anchor. " + "Background " * 6000 + "end anchor."
        runner.source_docs = {"source": {"work_id": "W1", "representation": "abstract", "text": text}}
        proofs = [{"work_id": "W1", "source_ref": "source", "quote": quote}
                  for quote in ("start anchor.", "end anchor.")]
        assignment = {"map": {"evidence": proofs}, "sources": runner._assessment_source_context()}
        with patch.object(runner, "_map_input_limit", return_value=3500):
            with self.assertRaisesRegex(ValidationError, "required evidence exceeds"):
                runner._fit_assessment_assignment(assignment)

    def test_gap_evidence_ids_are_replayed_by_runtime_and_acceptance_gate(self):
        runner = self.runtime(survey_config(self.endpoint, "catalog-evidence"))
        self.addCleanup(runner.control.close)
        with patch.object(runner, "_model_checked", wraps=runner._model_checked) as checked:
            result = runner.run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        gap_calls = [call for call in checked.call_args_list
                     if call.args and call.args[0] == "gap-assessment"]
        self.assertEqual(len(gap_calls), 1)
        self.assertEqual(gap_calls[0].kwargs.get("model_overrides"), {"temperature": 0.0})
        control, store = self.open_store()
        record = store.head("kb/gap-assessments/current")
        body = json.loads(store.read_body(record["body_hash"]))
        self.assertEqual(body["state"], "insufficient_evidence")
        self.assertIn("quote_sha256", body["comparisons"][0]["evidence"][0])
        self.assertNotIn("evidence_id", body["comparisons"][0]["evidence"][0])
        self.assertTrue(control._conn.execute("SELECT 1 FROM events WHERE event_type='assessment.accepted'").fetchone())

    def test_role_specific_context_limit_is_not_clamped_by_global_model(self):
        runner = self.runtime()
        runner.config["model"].update({
            "context_window_tokens": 65536,
            "max_input_tokens": 56000,
            "max_output_tokens": 8192,
            "role_models": {
                "methods.novelty-verifier": {
                    "context_window_tokens": 131072,
                    "max_input_tokens": 112000,
                },
            },
            "role_routes": {
                "methods.novelty-verifier": [{
                    "id": "ollama-deepseek", "pool": "ollama",
                    "base_url": "http://127.0.0.1:1",
                    "model": "deepseek-v4.1-flash:cloud",
                    "context_window_tokens": 131072,
                    "max_input_tokens": 112000,
                }],
            },
        })
        self.assertEqual(runner._map_input_limit("methods.novelty-verifier"), 112000)
        self.assertEqual(runner._map_input_limit("unconfigured-role"), 56000)
        runner.control.close()

    def test_catalog_only_map_is_deterministic_and_does_not_call_a_model(self):
        runner = self.runtime()
        work = {
            "work_id": "W999", "title": "Catalog-only study", "year": 2025,
            "doi": None, "referenced_works": [], "related_works": [],
            "publication_metadata_status": "provider_reported",
        }
        record = runner._publish("kb/works/W999", "reference_card", work,
                                 "research.cataloger")
        runner.work_records = {"W999": record}
        runner.works = {"W999": work}
        runner.register_ref = record["artifact_ref"]
        with patch.object(runner, "_map_job", side_effect=AssertionError("model map must not run")):
            runner._map()
        entry = json.loads(runner.store.read_body(runner.analysis_records["W999"]["body_hash"]))
        self.assertEqual(entry["inclusion"], "uncertain")
        self.assertIsNone(entry["problem"]["text"])
        execution = runner.store.head("command/executions/survey-map-deterministic-W999")
        execution_body = json.loads(runner.store.read_body(execution["body_hash"]))
        self.assertEqual(execution_body["execution_kind"], "deterministic_abstention")
        self.assertEqual(execution_body["model_calls"], 0)
        contexts = list(runner.control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE 'command/contexts/%'"))
        self.assertEqual(contexts, [])
        runner.control.close()

    def runtime(self, config=None, *, on_progress=None, resume_policy=None, work_orders=None, review_obligations=None):
        runner = SurveyRunner(self.root / "run", config or survey_config(self.endpoint),
                              on_progress=on_progress, resume_policy=resume_policy, work_orders=work_orders,
                              review_obligations=review_obligations)
        self.addCleanup(runner.control.close)
        runner.worker_target = simulated_survey_worker
        return runner

    @staticmethod
    def follow_up_order(objective="Locate the instrument calibration measurement."):
        return {"id": "evidence-1", "kind": "literature_expansion", "owner": "research.intelligence",
                "objective": objective, "why": "The evidence needed by the experiment is absent.",
                "success_condition": "Attach primary measurements or document the bounded negative search.",
                "evidence_needed": "Captured measurements, units, uncertainty and search records."}

    def test_follow_up_reaches_search_analysis_assessment_and_pinned_disposition(self):
        order = self.follow_up_order()
        runner = self.runtime(work_orders=[order])
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["follow_up_result"]["orders"][0]["status"], "limited")
        control = ControlStore(self.root / "run")
        self.addCleanup(control.close)
        store = ArtifactStore(control)
        phases = set()
        for row in control._conn.execute("SELECT artifact_ref FROM artifacts WHERE logical_id LIKE 'command/contexts/%'"):
            body = json.loads(store.read_body(store.get(row[0])["body_hash"]))
            prompt = json.loads(body["prompt"]) if isinstance(body.get("prompt"), str) else {}
            if prompt.get("phase") in {"blind_plan", "map", "gap_assessment", "survey_follow_up"}:
                phases.add(prompt["phase"])
                self.assertEqual(prompt["work_orders"], [order])
                self.assertEqual(prompt["follow_up_ref"], result["follow_up_ref"])
                from scisaurus.runtime.evidence import scientific_input_recovery_contract
                self.assertEqual(prompt["scientific_input_recovery"], scientific_input_recovery_contract())
                if prompt["phase"] == "survey_follow_up":
                    survey = json.loads(store.read_body(store.get(result["survey_ref"])["body_hash"]))
                    inventory = prompt["survey_inventory"]
                    self.assertEqual(inventory["survey_ref"], result["survey_ref"])
                    self.assertEqual(inventory["map_ref"], survey["map_ref"])
                    self.assertEqual(inventory["coverage_ref"], survey["coverage_ref"])
                    self.assertEqual({work["work_ref"] for work in inventory["works"]},
                                     set(survey["work_refs"]))
                    for work in inventory["works"]:
                        entry = json.loads(store.read_body(store.get(work["map_entry_ref"])["body_hash"]))
                        self.assertEqual(work["work_id"], entry["work_id"])
                        self.assertEqual(work["screening"], entry["inclusion"])
                        for source in work["sources"]:
                            self.assertIn(source["source_ref"], survey["source_refs"])
        self.assertEqual(phases, {"blind_plan", "map", "gap_assessment", "survey_follow_up"})
        report = json.loads(store.read_body(store.get(result["follow_up_result"]["ref"])["body_hash"]))
        self.assertEqual(report["survey_ref"], result["survey_ref"])
        self.assertEqual(report["assessment_ref"], result["assessment_ref"])
        from scisaurus.runtime.composer import ComposerRunner
        self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))
        changed = {**order, "objective": "Locate the down-sweep measurement."}
        self.assertFalse(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, changed))
        changed = {**order, "acceptance_checks": ["Independent calibrated replication."]}
        self.assertFalse(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, changed))
        forged_body = deepcopy(report)
        forged_body["orders"][0]["status"] = "resolved"
        forged = store.publish_artifact(logical_id="command/survey-follow-up-results/forged",
            artifact_type="report", author="methods.evidence-verifier", body=canonical_bytes(forged_body),
            inputs=store.get(result["follow_up_result"]["ref"])["inputs"])
        forged_run = {**result, "follow_up_result": {"ref": forged["artifact_ref"], "orders": forged_body["orders"]}}
        self.assertFalse(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", forged_run, order))
        query_ref = report["orders"][0]["query_refs"][0]
        query = json.loads(store.read_body(store.get(query_ref)["body_hash"]))
        fake_query = store.publish_artifact(logical_id="kb/forged-follow-up-query", artifact_type="query_record",
            author=query["role"], body=canonical_bytes({"request": query["request"], "outcome": "empty",
                "role": query["role"], "plan_ref": query["plan_ref"], "returned_work_ids": []}))
        with self.assertRaises(ValidationError):
            SurveyGate(control, store).require_successful_follow_up_search(
                fake_query["artifact_ref"], {"query_refs": [fake_query["artifact_ref"]]}, result["follow_up_ref"])
        for field, replacement in (("returned_work_ids", ["W999"]), ("count", query["count"] + 99),
                                   ("next_cursor", "invented"), ("has_more", not query["has_more"])):
            forged_summary = store.publish_artifact(logical_id=f"kb/forged-follow-up-summary/{field}",
                artifact_type="query_record", author=query["role"], body=canonical_bytes({**query, field: replacement}),
                inputs=store.get(query_ref)["inputs"])
            with self.subTest(field=field), self.assertRaises(ValidationError):
                SurveyGate(control, store).require_successful_follow_up_search(
                    forged_summary["artifact_ref"], {"query_refs": [forged_summary["artifact_ref"]]},
                    result["follow_up_ref"])

    def test_operation_acceptance_is_independent_and_immutable(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.core.surveys import SurveyGate
        order = self.follow_up_order()
        runner = self.runtime(survey_config(self.endpoint, "follow-up-conflicted-completion"), work_orders=[order])
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        row = result["follow_up_result"]["orders"][0]
        self.assertEqual(row["status"], "limited")
        self.assertEqual(row["completion"]["outcome"], "met")
        self.assertTrue(row["limitation"])
        self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))
        control, store = self.open_store()
        body = json.loads(store.read_body(store.get(result["follow_up_result"]["ref"])["body_hash"]))
        completion_ref = body["completion_execution_refs"][0]
        recorded = SurveyGate._recorded_execution
        assignments = []
        def inspect(gate, ref, *args, **kwargs):
            value = recorded(gate, ref, *args, **kwargs)
            if ref == completion_ref:
                assignment = json.loads(value[3]["prompt"])
                assignments.append(assignment)
            return value
        with patch.object(SurveyGate, "_recorded_execution", new=inspect):
            self.assertTrue(ComposerRunner._survey_follow_up_was_replayed(self.root / "run", result, order))
        assignment = assignments[0]
        self.assertNotIn("scientific_input_recovery", assignment)
        self.assertNotIn("completion", assignment["disposition"])
        self.assertEqual(assignment["disposition"]["rationale"], row["rationale"])
        self.assertEqual(assignment["disposition"]["next_action"], row["next_action"])
        from scisaurus.runtime.survey_records import follow_up_completion_basis
        self.assertEqual(assignment["disposition"], follow_up_completion_basis(row))
        self.assertEqual(assignment["work_orders"], [order])
        disposition_execution = store.get(body["execution_ref"])
        context_ref = disposition_execution["inputs"][0]["ref"]
        context = json.loads(store.read_body(store.get(context_ref)["body_hash"]))
        disposition_assignment = json.loads(context["prompt"])
        from scisaurus.runtime.survey_records import follow_up_completion_context
        self.assertEqual(assignment["evidence_context"], follow_up_completion_context(disposition_assignment))
        self.assertTrue(assignment["evidence_context"]["searches"])
        for changed in ({"completion_execution_refs": []}, {"completion_execution_refs": [body["execution_ref"]]},
                        {"completion_execution_refs": None},
                        {"orders": [{**row, "completion": {"outcome": "unmet", "rationale": "Operator rewrite."}}]}):
            invalid = {**body, **changed}
            record = store.publish_artifact(logical_id="command/survey-follow-up-results/invalid-review",
                artifact_type="report", author="methods.evidence-verifier", body=canonical_bytes(invalid),
                inputs=[{"ref": ref, "purpose": "subject"} for ref in [body["follow_up_ref"], body["survey_ref"],
                    body["assessment_ref"], *body["execution_refs"], completion_ref]])
            forged = {**result, "follow_up_result": {"ref": record["artifact_ref"], "orders": invalid["orders"]}}
            with self.subTest(changed=changed):
                self.assertFalse(ComposerRunner._survey_follow_up_was_replayed(self.root / "run", forged, order))
        for field, value in (("disposition_execution_ref", "artifact:foreign/execution@1"),
                             ("work_orders", [{**order, "success_condition": "Different acceptance."}]),
                             ("survey_ref", "artifact:foreign/survey@1"),
                             ("disposition", {**assignment["disposition"], "limitation": ""}),
                             ("evidence_context", {**assignment["evidence_context"], "searches": []}),
                             ("evidence_context", {**assignment["evidence_context"], "sources": []}),
                             ("evidence_context", {**assignment["evidence_context"], "survey_inventory": {}})):
            def altered(gate, ref, *args, **kwargs):
                values = list(recorded(gate, ref, *args, **kwargs))
                if ref == completion_ref:
                    changed = json.loads(values[3]["prompt"])
                    changed[field] = value
                    values[3] = {**values[3], "prompt": json.dumps(changed)}
                return tuple(values)
            with self.subTest(field=field), patch.object(SurveyGate, "_recorded_execution", new=altered):
                self.assertFalse(ComposerRunner._survey_follow_up_was_replayed(self.root / "run", result, order))

    def test_prior_completion_review_contract_replays_its_full_pinned_disposition(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.runtime.survey_records import FOLLOW_UP_COMPLETION_REVIEW_LEGACY_CONTRACT
        order = self.follow_up_order()
        runner = self.runtime(survey_config(self.endpoint, "follow-up-conflicted-completion"), work_orders=[order])
        with patch("scisaurus.runtime.survey.FOLLOW_UP_COMPLETION_REVIEW_CONTRACT",
                   FOLLOW_UP_COMPLETION_REVIEW_LEGACY_CONTRACT):
            result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertTrue(ComposerRunner._survey_follow_up_was_replayed(self.root / "run", result, order))
        self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))
        runner.control, runner.store = self.open_store()
        runner.gate = SurveyGate(runner.control, runner.store)
        self.assertEqual(runner._prior_follow_up_completion_reviews(), [])

    def test_prior_evidence_only_completion_review_remains_replayable(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.runtime.survey_records import FOLLOW_UP_COMPLETION_REVIEW_EVIDENCE_CONTRACT
        order = self.follow_up_order()
        runner = self.runtime(survey_config(self.endpoint, "follow-up-conflicted-completion"), work_orders=[order])
        with patch("scisaurus.runtime.survey.FOLLOW_UP_COMPLETION_REVIEW_CONTRACT",
                   FOLLOW_UP_COMPLETION_REVIEW_EVIDENCE_CONTRACT):
            result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertTrue(ComposerRunner._survey_follow_up_was_replayed(self.root / "run", result, order))
        self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))
        runner.control, runner.store = self.open_store()
        runner.gate = SurveyGate(runner.control, runner.store)
        self.assertEqual(runner._prior_follow_up_completion_reviews(), [])

    def test_unmet_completion_feedback_reaches_the_disposition_author(self):
        order = {**self.follow_up_order(), "success_condition": "Capture the numeric measurement with units."}
        config = survey_config(self.endpoint, "follow-up-capture-required")
        first = self.runtime(config, work_orders=[order]).run()
        self.assertEqual(first["status"], "completed", first["error"])
        policy = {"additional_seconds": config["limits"]["wall_clock_seconds"],
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["gap_assessment"]}}
        runner = self.runtime(config, work_orders=[order], resume_policy=policy)
        reviews = runner._prior_follow_up_completion_reviews()
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["order_id"], order["id"])
        self.assertEqual(reviews[0]["review"]["outcome"], "unmet")
        self.assertNotIn("completion", reviews[0]["disposition"])
        self.assertEqual(reviews[0]["disposition"]["next_action"],
                         first["follow_up_result"]["orders"][0]["next_action"])
        self.assertTrue(reviews[0]["review_execution_ref"].startswith("artifact:command/executions/"))
        captured = []
        original = runner._model_checked
        def inspect(name, role, assignment, *args, **kwargs):
            if assignment.get("phase") == "survey_follow_up":
                captured.append(deepcopy(assignment))
            return original(name, role, assignment, *args, **kwargs)
        with patch.object(runner, "_model_checked", side_effect=inspect):
            second = runner.run()
        self.assertEqual(second["status"], "completed", second["error"])
        self.assertEqual(captured[0]["prior_completion_reviews"], reviews)
        runner.control, runner.store = self.open_store()
        runner.gate = SurveyGate(runner.control, runner.store)
        report = runner.store.get(second["follow_up_result"]["ref"])
        forged = runner._body(report)
        forged["orders"][0]["completion"]["rationale"] = "Unrecorded review content."
        runner.store.publish_artifact(logical_id="command/survey-follow-up-results/forged",
            artifact_type="report", author="methods.evidence-verifier", body=canonical_bytes(forged),
            inputs=report["inputs"])
        with self.assertRaisesRegex(StateError, "recorded independent review"):
            runner._prior_follow_up_completion_reviews()

    def test_completion_review_sees_scientific_decision_and_negative_search_provenance(self):
        from scisaurus.runtime.composer import ComposerRunner
        order = {**self.follow_up_order(), "success_condition":
                 "Document a bounded negative search, then explicitly reframe as a prediction study."}
        runner = self.runtime(survey_config(self.endpoint, "follow-up-decision-required"), work_orders=[order])
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        row = result["follow_up_result"]["orders"][0]
        self.assertEqual(row["status"], "limited")
        self.assertEqual(row["completion"]["outcome"], "met")
        self.assertTrue(row["limitation"])
        self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))

    def test_capture_required_follow_up_does_not_close_on_unavailable_evidence(self):
        from scisaurus.runtime.composer import ComposerRunner
        order = {**self.follow_up_order(), "success_condition": "Capture the numeric measurement with units."}
        runner = self.runtime(survey_config(self.endpoint, "follow-up-capture-required"), work_orders=[order])
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertTrue(ComposerRunner._survey_follow_up_was_replayed(self.root / "run", result, order))
        self.assertEqual(result["follow_up_result"]["orders"][0]["completion"]["outcome"], "unmet")
        self.assertFalse(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))

    def test_modern_disposition_replay_rejects_missing_completion(self):
        from scisaurus.runtime.composer import ComposerRunner
        order = self.follow_up_order()
        runner = self.runtime(survey_config(self.endpoint, "follow-up-missing-completion"), work_orders=[order])
        original = runner._validate_follow_up_result
        def legacy_validation(value, **kwargs):
            kwargs["require_completion"] = False
            return original(value, **kwargs)
        with patch.object(runner, "_validate_follow_up_result", side_effect=legacy_validation):
            result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["follow_up_result"]["orders"][0]["completion"]["outcome"], "met")
        self.assertFalse(ComposerRunner._survey_follow_up_was_replayed(self.root / "run", result, order))
        self.assertFalse(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))

    def test_legacy_disposition_replay_preserves_prior_status_semantics(self):
        from scisaurus.runtime.composer import ComposerRunner
        order = self.follow_up_order()
        runner = self.runtime(survey_config(self.endpoint, "follow-up-missing-completion"), work_orders=[order])
        original_validate = runner._validate_follow_up_result
        original_assignment = runner._follow_up_assignment
        def legacy_validation(value, **kwargs):
            kwargs["require_completion"] = False
            return original_validate(value, **kwargs)
        def legacy_assignment(assignment):
            value = original_assignment(assignment)
            value.pop("completion_contract", None)
            value.pop("completion_review_contract", None)
            return value
        with patch.object(runner, "_validate_follow_up_result", side_effect=legacy_validation), \
             patch.object(runner, "_follow_up_assignment", side_effect=legacy_assignment):
            result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        body = json.loads(store.read_body(store.get(result["follow_up_result"]["ref"])["body_hash"]))
        body.pop("completion_execution_refs")
        for row in body["orders"]:
            row.pop("completion")
        record = store.publish_artifact(logical_id="command/survey-follow-up-results/legacy-fixture",
            artifact_type="report", author="methods.evidence-verifier", body=canonical_bytes(body),
            inputs=[{"ref": ref, "purpose": "subject"} for ref in [body["follow_up_ref"], body["survey_ref"],
                body["assessment_ref"], *body["execution_refs"]]])
        result = {**result, "follow_up_result": {"ref": record["artifact_ref"], "orders": body["orders"]}}
        self.assertTrue(ComposerRunner._survey_follow_up_was_replayed(self.root / "run", result, order))
        self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))

    def test_follow_up_resume_keeps_completed_analysis_with_unchanged_claims(self):
        config = survey_config(self.endpoint)
        order = self.follow_up_order()
        first = self.runtime(config, work_orders=[order])
        initial = first.run()
        self.assertEqual(initial["status"], "completed", initial.get("error"))
        hashes = {wid: record["body_hash"] for wid, record in first.analysis_records.items()}
        changed = self.follow_up_order("Locate the independent instrument calibration measurement.")
        policy = {"additional_seconds": config["limits"]["wall_clock_seconds"],
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["follow_up"]}}
        second = self.runtime(config, resume_policy=policy, work_orders=[changed])
        refreshed = second.run()
        self.assertEqual(refreshed["status"], "completed", refreshed.get("error"))
        self.assertEqual({wid: record["body_hash"] for wid, record in second.analysis_records.items()}, hashes)
        control, store = self.open_store()
        completion = store.head("command/survey-analysis-completions/W101")
        checked = json.loads(store.read_body(completion["body_hash"]))
        self.assertNotIn(second.follow_up_ref, checked["basis"])
        self.assertEqual(checked["question"], second.score["question"])
        self.assertEqual(checked["entry_ref"], second.analysis_records["W101"]["artifact_ref"])
        third = self.runtime(config, resume_policy=policy, work_orders=[changed])
        self.assertEqual(third.analyzed_basis["W101"], third._analysis_basis("W101"))
        with patch.object(third, "_map_job", side_effect=AssertionError("unchanged analysis must be retained")):
            final = third.run()
        self.assertEqual(final["status"], "completed", final.get("error"))

    def test_follow_up_overlay_preserves_durable_config_and_changes_assignment_identity(self):
        config = survey_config(self.endpoint)
        first = self.runtime(config)
        original = first.store.head("inputs/run-config")
        first.run()
        policy = {"additional_seconds": config["limits"]["wall_clock_seconds"], "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["gap_assessment"]}}
        resumed = self.runtime(config, resume_policy=policy, work_orders=[self.follow_up_order()])
        self.addCleanup(resumed.control.close)
        self.assertEqual(resumed.store.head("inputs/run-config")["body_hash"], original["body_hash"])
        self.assertNotIn("work_orders", resumed.config)
        projection = resumed._follow_up_assignment({"phase": "blind_plan"})
        self.assertEqual(projection["work_orders"][0]["objective"], self.follow_up_order()["objective"])
        self.assertIsNotNone(projection["follow_up_ref"])
        result = resumed.run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertNotEqual(result["survey_ref"], first.survey_ref)
        from scisaurus.runtime.composer import ComposerRunner
        self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(
            self.root / "run", result, self.follow_up_order()))

    def test_follow_up_rejects_forged_passages_unrecorded_searches_and_missing_orders(self):
        runner = self.runtime(work_orders=[self.follow_up_order()])
        self.addCleanup(runner.control.close)
        runner.source_docs = {"source-1": {"work_id": "W101", "text": "Recorded exact text."}}
        runner.query_refs = ["query-1"]
        row = {"id": "evidence-1", "status": "resolved", "rationale": "Captured measurement.",
               "evidence": [{"work_id": "W101", "source_ref": "source-1", "quote": "Fabricated text."}],
               "query_refs": ["query-1"], "limitation": "", "next_action": "Use the measurement."}
        with self.assertRaises(ValidationError):
            runner._validate_follow_up_result({"orders": [row]})
        with self.assertRaises(ValidationError):
            runner._validate_follow_up_result({"orders": [{**row, "status": "limited", "evidence": [],
                "limitation": "Source unavailable.", "query_refs": ["unrecorded-query"]}]})
        with self.assertRaises(ValidationError):
            runner._validate_follow_up_result({"orders": []})
        for proof in ({"work_id": "W101", "source_ref": "source-1"},
                      {"work_id": "W101", "source_ref": "source-1", "quote": "Recorded exact text.", "extra": True},
                      {"work_id": "W101", "source_ref": "source-1", "quote": "Recorded exact text.",
                       "start": -1, "end": -1, "quote_sha256": "forged"}):
            with self.subTest(proof=proof), self.assertRaises(ValidationError):
                runner._validate_follow_up_result({"orders": [{**row, "query_refs": [], "evidence": [proof]}]})

    def test_follow_up_disposition_fits_context_without_changing_captures(self):
        runner = self.runtime(work_orders=[self.follow_up_order()])
        self.addCleanup(runner.control.close)
        runner.config["model"].update({"context_window_tokens": 65536, "max_input_tokens": 56000,
                                       "max_output_tokens": 8192})
        map_ref = runner._record("kb/map/context-fixture", "note",
            {"entries": [], "entry_refs": []}, "research.cataloger")["artifact_ref"]
        coverage_ref = runner._record("kb/coverage/context-fixture", "note",
            {"abstentions": []}, "research.cataloger")["artifact_ref"]
        runner.survey_ref = runner._record("kb/surveys/current", "note",
            {"map_ref": map_ref, "coverage_ref": coverage_ref, "work_refs": [], "source_refs": []},
            "research.cataloger")["artifact_ref"]
        runner.assessment_ref = runner._record("kb/assessment/context-fixture", "note",
            {"state": "insufficient_evidence", "rationale": "Measurements absent.", "checks": []},
            "methods.novelty-verifier")["artifact_ref"]
        runner.source_docs = {f"source-{i}": {"work_id": f"W{i}", "representation": "full_text",
            "identity_verified": True, "identity_checks": {"title_match": True, "section_markers": ["Results"]},
            "text": "Measured outcome. " * 22000} for i in range(10)}
        original = deepcopy(runner.source_docs)
        captured = []
        def capture(name, actor, assignment, *args, **kwargs):
            captured.append(assignment)
            raise RuntimeError("captured bounded assignment")
        with patch.object(runner, "_model_checked", side_effect=capture), self.assertRaises(RuntimeError):
            runner._resolve_follow_up()
        self.assertLessEqual(estimate_input_tokens(SYSTEM, json.dumps(captured[0], ensure_ascii=False)), 56000)
        self.assertEqual(captured[0]["work_orders"], [self.follow_up_order()])
        self.assertEqual(captured[0]["response_contract"]["required_order_fields"],
                         ["id", "status", "rationale", "evidence", "query_refs", "limitation", "next_action", "completion"])
        self.assertIn("no paper must itself declare", captured[0]["instructions"])
        self.assertEqual(runner.source_docs, original)

    def test_record_follow_up_is_consumed_only_with_recalculated_pinned_inventory(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.runtime.survey_records import follow_up_inventory
        order = self.follow_up_order("Check the registered membership of W101.")
        runner = self.runtime(survey_config(self.endpoint, "follow-up-records"), work_orders=[order])
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        disposition = result["follow_up_result"]["orders"][0]
        self.assertEqual(disposition["status"], "limited")
        self.assertTrue(disposition["query_refs"])
        self.assertTrue(disposition["record_evidence"])
        self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))
        def changed_inventory(store, ref):
            value = follow_up_inventory(store, ref)
            value["works"][0]["title"] = "A different catalog record"
            return value
        with patch("scisaurus.runtime.survey_records.follow_up_inventory", side_effect=changed_inventory):
            self.assertFalse(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))

    def test_record_only_follow_up_keeps_scientific_acquisition_order_open(self):
        from scisaurus.runtime.composer import ComposerRunner
        order = self.follow_up_order("Check the registered membership of W101.")
        runner = self.runtime(survey_config(self.endpoint, "follow-up-records-only"), work_orders=[order])
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        disposition = result["follow_up_result"]["orders"][0]
        self.assertEqual(disposition["status"], "unresolved")
        self.assertEqual(disposition["query_refs"], [])
        self.assertTrue(disposition["record_evidence"])
        self.assertFalse(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", result, order))

    def test_composer_resume_consumes_new_native_unresolved_receipt_before_stage_boundary(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.tests.test_composer import ComposerWorkflowTests, _ComposerTestSpecialistClient
        config = survey_config(self.endpoint, "follow-up-records-only")
        config["project_id"] = str((self.root / "run").resolve())
        order = self.follow_up_order("Check the registered membership of W101.")
        first = self.runtime(config, work_orders=[order]).run()
        self.assertEqual(first["status"], "completed", first["error"])
        root = self.root / "composer-fixture"; root.mkdir()
        workflow = ComposerWorkflowTests()._workflow(root)
        workflow["stages"] = workflow["stages"][:1]
        workflow["stages"][0]["project_dir"] = config["project_id"]
        workflow["completion"]["required_stage_ids"] = ["survey"]
        composer = ComposerRunner(workflow, stop_after_stage="survey")
        self.addCleanup(composer.close)
        initial = {**first, "stage_id": "survey", "kind": "survey", "project_dir": config["project_id"],
                   "output_path": str(self.root / "run/output/run.json")}
        with patch.object(composer, "_run_stage", return_value=initial), \
                patch("scisaurus.runtime.specialists.ModelClient", _ComposerTestSpecialistClient):
            stopped = composer.run()
        self.assertEqual(stopped["status"], "paused")
        native = self.runtime(config, work_orders=[order], resume_policy={"additional_seconds": config["limits"]["wall_clock_seconds"],
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reopen", "reopen_scopes": ["follow_up"]}}).run()
        self.assertEqual(native["status"], "completed", native["error"])
        self.assertNotEqual(native["run_id"], first["run_id"])
        self.assertEqual(native["survey_ref"], first["survey_ref"])
        self.assertEqual(native["assessment_ref"], first["assessment_ref"])
        self.assertTrue(ComposerRunner._survey_follow_up_was_replayed(self.root / "run", native))
        resumed = ComposerRunner(workflow, resume=True, stop_after_stage="survey")
        self.addCleanup(resumed.close)
        identity = {"topic_id": "selected", "topic_cycle": 0}
        saved = deepcopy(resumed.context["survey"])
        resumed.stage_records["survey"].update(topic_id="selected", topic_cycle=0,
            composer_decision="advance_with_findings", verifier_artifact_ref="artifact:old/verifier@1")
        for attempt in resumed.stage_records["survey"]["attempts"]:
            attempt.update(topic_id="selected", topic_cycle=0)
        with patch.object(resumed, "_current_topic_identity", return_value=identity):
            for wrong in ({"topic_lineage": {"topic_id": "foreign", "topic_cycle": 0}},
                          {"topic_lineage": {"topic_id": "selected", "topic_cycle": 1}},
                          {"topic_id": "foreign"}, {"superseded_topic_id": "selected"}):
                resumed.context["survey"] = {**saved, **wrong}
                self.assertEqual(resumed._reconcile_latest_survey_results(), [])
        resumed.context["survey"] = {**saved, **identity, "topic_lineage": identity}
        exported_path = self.root / "run/output/run.json"
        original_export = exported_path.read_bytes()
        exported_path.write_bytes(canonical_bytes({**native, "run_id": "forged"}))
        with self.assertRaisesRegex(StateError, "receipt"):
            resumed._reconcile_latest_survey_results()
        exported_path.write_bytes(original_export)
        with patch.object(resumed, "_current_topic_identity", return_value=identity), \
                patch.object(resumed, "_run_stage", side_effect=AssertionError("boundary must not dispatch")), \
                patch("scisaurus.runtime.specialists.ModelClient", side_effect=AssertionError("receipt replay must not dispatch")):
            updated = resumed.run()
        context = updated["context"]["survey"]
        self.assertEqual(context["run_id"], native["run_id"])
        self.assertEqual(context["follow_up_result"], native["follow_up_result"])
        self.assertEqual(context["status"], "candidate_needs_review")
        self.assertEqual(context["topic_lineage"], identity)
        self.assertTrue(context["release_blocking"])
        self.assertFalse(resumed._stage_releases_dependencies(updated["stages"]["survey"], stage_kind="survey"))
        self.assertEqual(updated["stages"]["survey"]["composer_decision"], "review_required")
        self.assertNotIn("verifier_artifact_ref", updated["stages"]["survey"])
        self.assertEqual(updated["deadline_at_epoch"], stopped["deadline_at_epoch"])
        self.assertEqual(updated["status"], "candidate_needs_review")
        self.assertNotEqual(updated.get("interim_report", {}).get("stop_reason"), "operator_stage_boundary")
        self.assertEqual(json.loads((self.root / "run/output/composer-gated-run.json").read_text()), context)
        self.assertFalse(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", native, order))
        self.assertEqual(resumed._reconcile_latest_survey_results(), [])
        resumed = ComposerRunner(workflow, resume=True, stop_after_stage="survey")
        self.addCleanup(resumed.close)
        prior_receipt = context["native_reconciliation_ref"]
        changed_order = {**order, "id": "new-source-review", "kind": "literature_expansion",
            "owner": "research.intelligence", "target_stage_id": "survey"}
        with patch.object(resumed, "_current_topic_identity", return_value=identity), \
                patch.object(resumed, "_requests_for_stage", return_value=[changed_order]):
            self.assertEqual(resumed._reconcile_latest_survey_results(), ["survey"])
            refreshed = resumed.context["survey"]
            self.assertEqual(refreshed["run_id"], native["run_id"])
            self.assertEqual(refreshed["survey_ref"], native["survey_ref"])
            self.assertNotEqual(refreshed["native_reconciliation_ref"], prior_receipt)
            self.assertEqual(refreshed["research_requests"], [changed_order])
            self.assertEqual(resumed._reconcile_latest_survey_results(), [])
        id_only_order = {**changed_order, "id": "replacement-source-review"}
        with patch.object(resumed, "_current_topic_identity", return_value=identity), \
                patch.object(resumed, "_requests_for_stage", return_value=[id_only_order]), \
                patch.object(resumed, "_survey_attempt_was_accepted", return_value=True):
            self.assertEqual(resumed._reconcile_latest_survey_results(), ["survey"])
            self.assertEqual(resumed.context["survey"]["research_requests"], [id_only_order])
            self.assertTrue(resumed.context["survey"]["release_blocking"])
            self.assertFalse(resumed._stage_releases_dependencies(resumed.stage_records["survey"], stage_kind="survey"))
            self.assertEqual(resumed._reconcile_latest_survey_results(), [])
        historical = {"stage_id": "survey", "stop_reason": "provider_rate_limit", "reason": "Captured HTTP429"}
        scientific = {"stage_id": "survey", "stop_reason": "scientific_evidence", "reason": "Input remains missing"}
        future = {"stage_id": "survey", "stop_reason": "operational_state", "attempt_number": 999, "reason": "Later failure"}
        resumed.blockers.extend([historical, scientific, future])
        with patch.object(resumed, "_current_topic_identity", return_value=identity), \
                patch.object(resumed, "_requests_for_stage", return_value=[id_only_order]), \
                patch.object(resumed.tasks, "get_attempt", return_value={"state": "failed"}):
            self.assertEqual(resumed._reconcile_latest_survey_results(), [])
            self.assertNotIn("recovery", historical)
        original_export = exported_path.read_bytes()
        exported_path.write_bytes(canonical_bytes({**native, "usage": {"model_calls": 99999}}))
        with patch.object(resumed, "_current_topic_identity", return_value=identity), \
                patch.object(resumed, "_requests_for_stage", return_value=[id_only_order]), \
                self.assertRaisesRegex(StateError, "completed native producer"):
            resumed._reconcile_latest_survey_results()
        exported_path.write_bytes(original_export)
        self.assertNotIn("recovery", historical)
        with patch.object(resumed, "_current_topic_identity", return_value=identity), \
                patch.object(resumed, "_requests_for_stage", return_value=[id_only_order]):
            self.assertEqual(resumed._reconcile_latest_survey_results(), [])
        self.assertEqual(historical["recovery"], "superseded_by_current_stage_state")
        self.assertEqual(historical["reason"], "Captured HTTP429")
        self.assertNotIn("recovery", scientific)
        self.assertNotIn("recovery", future)
        resumed.context["topic"] = {"topic": {"research_question": config["survey"]["question"]},
            "specialist_verifier": {"artifact_ref": "artifact:command/new-governing-review@1"}}
        with patch.object(resumed, "_current_topic_identity", return_value=identity), \
                patch.object(resumed, "_topic_stage_for_survey", return_value={"id": "topic"}), \
                patch.object(resumed, "_requests_for_stage", return_value=[id_only_order]), \
                patch.object(resumed, "_survey_request_was_fulfilled", return_value=True), \
                patch.object(resumed, "_survey_attempt_was_accepted", return_value=True), \
                patch.object(resumed, "_gate_free_topic_survey", side_effect=lambda value, **kw: value):
            self.assertEqual(resumed._reconcile_latest_survey_results(), ["survey"])
            self.assertEqual(resumed.context["survey"]["status"], "candidate_needs_review")
            self.assertTrue(resumed.context["survey"]["release_blocking"])
            self.assertFalse(resumed._stage_releases_dependencies(resumed.stage_records["survey"], stage_kind="survey"))
            self.assertEqual(resumed._reconcile_latest_survey_results(), [])

    def test_follow_up_inventory_projection_keeps_exact_named_ids_and_discloses_scope(self):
        inventory = {"survey_ref": "artifact:kb/surveys/current@1", "works": [
            {"work_id": "W1", "screening": "uncertain", "sources": [{"representation": "abstract"}]},
            {"work_id": "W10", "screening": "included"},
            {"work_id": "doi:10.123/example", "screening": "uncertain"}]}
        original = deepcopy(inventory)
        projected = SurveyRunner._project_follow_up_inventory(inventory, {"objective": "Check W1 membership."})
        self.assertEqual(projected["works"], [inventory["works"][0]])
        self.assertEqual(projected["catalog_work_count"], 3)
        self.assertEqual(projected["projection_scope"], "named_records")
        doi = SurveyRunner._project_follow_up_inventory(inventory, {"objective": "Check doi:10.123/example."})
        self.assertEqual(doi["works"], [inventory["works"][2]])
        all_records = SurveyRunner._project_follow_up_inventory(inventory, {"objective": "Check captured records."})
        self.assertEqual(all_records["works"], inventory["works"])
        self.assertEqual(all_records["projection_scope"], "catalog")
        self.assertEqual(inventory, original)

    def test_follow_up_inventory_is_pinned_to_accepted_membership_and_reading_status(self):
        runner = self.runtime(work_orders=[self.follow_up_order()])
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        runner.store = store
        before = runner._follow_up_inventory()
        survey = json.loads(store.read_body(store.get(result["survey_ref"])["body_hash"]))
        map_record = store.get(survey["map_ref"])
        newer = json.loads(store.read_body(map_record["body_hash"]))
        newer["entry_refs"] = []
        store.publish_artifact(logical_id=map_record["artifact_id"], artifact_type="note",
            author="research.cataloger", body=canonical_bytes(newer))
        self.assertEqual(runner._follow_up_inventory(), before)
        self.assertTrue(before["works"])
        for row in before["works"]:
            self.assertIsNotNone(row["map_entry_ref"])
            self.assertTrue(row["sources"])
            if row["abstention"] is not None:
                self.assertEqual(row["abstention"]["work_id"], row["work_id"])
        deferred = before["works"][0]
        entry_record = store.get(deferred["map_entry_ref"])
        entry = json.loads(store.read_body(entry_record["body_hash"]))
        entry.update(inclusion="uncertain", reason="Captured source retained; substantive reading deferred.")
        for field in MAP_FIELDS:
            entry[field] = {"text": None, "evidence": []}
        updated_entry = store.publish_artifact(logical_id=entry_record["artifact_id"], artifact_type="note",
            author="research.cataloger", body=canonical_bytes(entry))
        retained_map = json.loads(store.read_body(map_record["body_hash"]))
        retained_map["entry_refs"] = [updated_entry["artifact_ref"] if ref == deferred["map_entry_ref"] else ref
                                      for ref in retained_map["entry_refs"]]
        updated_map = store.publish_artifact(logical_id=map_record["artifact_id"], artifact_type="note",
            author="research.cataloger", body=canonical_bytes(retained_map))
        coverage = {"abstentions": [{"work_id": deferred["work_id"], "scope": "reading_deferred"}]}
        updated_coverage = store.publish_artifact(logical_id="kb/coverage", artifact_type="note",
            author="research.cataloger", body=canonical_bytes(coverage))
        runner.survey_ref = store.publish_artifact(logical_id="kb/surveys/current", artifact_type="note",
            author="research.cataloger", body=canonical_bytes({**survey,
                "map_ref": updated_map["artifact_ref"], "coverage_ref": updated_coverage["artifact_ref"]}))["artifact_ref"]
        inventory = runner._follow_up_inventory()
        retained = next(row for row in inventory["works"] if row["work_id"] == deferred["work_id"])
        self.assertEqual(retained["screening"], "uncertain")
        self.assertEqual(retained["abstention"]["scope"], "reading_deferred")
        self.assertEqual(retained["sources"], deferred["sources"])
        self.assertEqual(retained["map_entry_ref"], updated_entry["artifact_ref"])

    def test_follow_up_contract_resume_keeps_accepted_frontier_and_checked_orders(self):
        from scisaurus.runtime.composer import ComposerRunner
        config = survey_config(self.endpoint)
        orders = [self.follow_up_order(), {**self.follow_up_order(), "id": "evidence-2"}]
        first = self.runtime(config, work_orders=orders)
        validate = first._validate_follow_up_result
        def reject_second(value, **kwargs):
            if kwargs.get("work_orders", [{}])[0].get("id") == "evidence-2":
                raise ModelContractError("survey follow-up disposition requires all assigned fields")
            return validate(value, **kwargs)
        with patch.object(first, "_validate_follow_up_result", side_effect=reject_second):
            failed = first.run()
        self.assertEqual(failed["failure"], {"kind": "unchanged_assignment_exhausted",
                                            "failure_class": "model_contract"})
        self.assertTrue(failed["survey_current"])
        self.assertTrue(failed["assessment_current"])
        self.assertEqual(ComposerRunner._survey_resume_scope(failed), "follow_up")
        self.assertIsNotNone(ComposerRunner._survey_checkpoint(self.root / "run"))
        with self.assertRaises(ModelWorkBlocked) as error:
            ComposerRunner._raise_stage_failure(failed)
        self.assertEqual(error.exception.failure_class, "model_contract")
        from scisaurus.runtime.failure_recovery import classify_failure
        self.assertEqual(classify_failure("survey", error.exception, failed), "model_contract")
        policy = {"additional_seconds": config["limits"]["wall_clock_seconds"],
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["follow_up"]}}
        second = self.runtime(config, resume_policy=policy, work_orders=orders)
        with patch.object(second, "_prepare_follow_up", side_effect=AssertionError("accepted evidence must remain")), \
             patch.object(second, "_setup", side_effect=AssertionError("no retrieval setup needed")):
            completed = second.run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertEqual(completed["survey_ref"], failed["survey_ref"])
        self.assertEqual(completed["assessment_ref"], failed["assessment_ref"])
        for order in orders:
            self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", completed, order))
        control, store = self.open_store()
        report = json.loads(store.read_body(store.get(completed["follow_up_result"]["ref"])["body_hash"]))
        self.assertEqual(len(report["execution_refs"]), 2)
        records = []
        for ref in report["execution_refs"]:
            execution = store.get(ref)
            context = json.loads(store.read_body(store.get(execution["inputs"][0]["ref"])["body_hash"]))
            records.append(json.loads(context["prompt"])["work_orders"])
        self.assertEqual(records, [[orders[0]], [orders[1]]])
        forged = {**report, "execution_refs": [report["execution_refs"][0]]}
        record = store.publish_artifact(logical_id="command/survey-follow-up-results/missing-execution",
            artifact_type="report", author="methods.evidence-verifier", body=canonical_bytes(forged),
            inputs=store.get(completed["follow_up_result"]["ref"])["inputs"])
        bad_run = {**completed, "follow_up_result": {"ref": record["artifact_ref"], "orders": report["orders"]}}
        self.assertFalse(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", bad_run, orders[0]))

    def test_follow_up_filtered_response_is_repaired_without_adopting_old_execution(self):
        from scisaurus.runtime.composer import ComposerRunner
        config = survey_config(self.endpoint, "follow-up-filtered")
        order = self.follow_up_order()
        first = self.runtime(config, work_orders=[order])
        failed = first.run()
        self.assertEqual(failed["failure"]["failure_class"], "model_contract")
        control, store = self.open_store()
        old_tasks = [row[0] for row in control._conn.execute(
            "SELECT task_id FROM tasks WHERE task_id LIKE 'survey-follow-up-disposition-%'")]
        policy = {"additional_seconds": config["limits"]["wall_clock_seconds"],
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["follow_up"]}}
        second = self.runtime(config, resume_policy=policy, work_orders=[order])
        completed = second.run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertEqual(completed["survey_ref"], failed["survey_ref"])
        self.assertEqual(completed["assessment_ref"], failed["assessment_ref"])
        self.assertTrue(ComposerRunner._survey_work_order_was_fulfilled(self.root / "run", completed, order))
        for task_id in old_tasks:
            self.assertEqual(control._conn.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0], "blocked")

    def test_process_stop_is_not_a_survey_failure_retry(self):
        runner = self.runtime()
        with patch.object(runner, "_setup", side_effect=KeyboardInterrupt("termination requested")):
            result = runner.run()
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["failure"], {"kind": "process_interrupted"})

    def test_search_timeout_is_a_recorded_gap_and_does_not_block_later_queries(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize()
        runner._setup()
        before = len(SurveyHTTPFixture.requests)

        runner._search(["query-timeout", "independent terminology"], "research.searcher")

        sent = SurveyHTTPFixture.requests[before:]
        self.assertEqual([row["query"].get("search", [None])[0] for row in sent],
                         ["query-timeout", "independent terminology"])
        timeout = next(row for row in runner.search_log
                       if row["request"].get("query") == "query-timeout")
        later = next(row for row in runner.search_log
                     if row["request"].get("query") == "independent terminology")
        self.assertEqual(timeout["outcome"], "provider_error")
        self.assertEqual(timeout["provider_http_status"], 504)
        self.assertEqual(timeout["returned_work_ids"], [])
        self.assertEqual(later["outcome"], "ok")
        self.assertIn("W201", later["returned_work_ids"])
        self.assertTrue(any(gap.get("kind") == "bibliographic_failure"
                            and gap.get("http_status") == 504 for gap in runner.gaps))
        self.assertEqual(sum(1 for row in sent
                             if row["query"].get("search", [None])[0] == "query-timeout"), 1)

    def open_store(self):
        control = ControlStore(self.root / "run")
        self.addCleanup(control.close)
        return control, ArtifactStore(control)

    def model_contexts(self, control, store):
        contexts = []
        for row in control._conn.execute("SELECT artifact_ref FROM artifacts WHERE logical_id LIKE 'command/contexts/%' ORDER BY rowid"):
            manifest = store.get(row[0])
            body = json.loads(store.read_body(manifest["body_hash"]))
            if "prompt" in body:
                contexts.append((manifest, json.loads(body["prompt"])))
        return contexts

    def test_resume_reuses_latest_validation_feedback_only_for_the_same_assignment(self):
        runner = self.runtime()
        assignment = {"phase": "gap_assessment", "survey_ref": "artifact:kb/surveys/current@1"}
        prior = {"state": "insufficient_evidence", "evidence": []}
        context = runner._publish("command/contexts/survey-gap-assessment-1", "note", {
            "client": {"model": "fixture"},
            "prompt": json.dumps({**assignment, "validation_feedback": {"error": "older"}})},
            "methods.novelty-verifier")
        execution = runner._publish("command/executions/survey-gap-assessment-1", "report", {
            "text": json.dumps(prior)}, "methods.novelty-verifier", subjects=[context["artifact_ref"]])
        proposal = runner._publish("kb/model-proposals/survey-gap-assessment-1", "note", prior,
            "methods.novelty-verifier", subjects=[execution["artifact_ref"]])
        runner._publish("command/validation/survey-gap-assessment-1", "note", {
            "error": "$.comparisons[0].evidence[0]: invalid quote"}, "command.controller",
            subjects=[proposal["artifact_ref"]])
        runner.resume_session = True
        retained = runner._retained_validation_feedback("gap-assessment", assignment)
        self.assertEqual(retained["previous_response"], prior)
        self.assertIn("comparisons[0]", retained["error"])
        self.assertIsNone(runner._retained_validation_feedback(
            "gap-assessment", {**assignment, "survey_ref": "artifact:kb/surveys/current@2"}))
        runner.control.close()

    def test_resume_reuses_negative_aggregate_reply_after_quote_location_schema_migration(self):
        from scisaurus.runtime.survey_records import (
            normalize_survey_review_envelope, survey_review_response_contract,
            validate_survey_review, SURVEY_QUOTE_LOCATION_INSTRUCTION)
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        ref = "artifact:kb/work-analyses/W101@1"
        row = {"work_id": "W101", "inclusion": "included", "reason": "The study examines recall timing.",
               **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        current_map = {"entries": [row], "entry_refs": {"W101": ref}, "relationships": [], "relationship_refs": []}
        base = runner._follow_up_assignment({"phase": "survey_review", "map": current_map,
            "question": "Which mechanism explains recall timing?", "survey_ref": "artifact:kb/surveys/current@1",
            "instructions": "Inspect the current map.",
            "response_contract": survey_review_response_contract(current_map, legacy=True)})
        upgraded = {**base, "response_contract": survey_review_response_contract(current_map),
                    "instructions": base["instructions"] + " " + SURVEY_QUOTE_LOCATION_INSTRUCTION}
        raw = {"checks": check_rows(SURVEY_CHECKS), "rationale": "The inclusion decision needs adjudication.",
               "findings": [{"check_id": "map-support", "target_ref": ref, "field": "inclusion",
                             "quote": row["reason"], "rationale": "The rationale does not justify inclusion."}]}
        next(check for check in raw["checks"] if check["check_id"] == "map-support")["outcome"] = "failed"
        task_id = "survey-survey-review-retained"
        runner.tasks.create(task_id, "verification", {"operation": "model"}, "command.controller")
        runner.tasks.admit(task_id, "command.controller")
        runner.tasks.start_attempt(task_id, "attempt-review-retained", owner="methods.survey-reviewer", lease_ttl_seconds=30)
        context = runner._publish("command/contexts/" + task_id, "note", {
            "client": {"model": "fixture"}, "prompt": json.dumps(base)}, "methods.survey-reviewer")
        execution = runner._publish("command/executions/" + task_id, "report", {"text": json.dumps(raw)},
            "methods.survey-reviewer", subjects=[context["artifact_ref"]])
        runner.tasks.finish_attempt("attempt-review-retained", "succeeded", usage={"model_calls": 1})
        runner.tasks.transition(task_id, "blocked", "command.controller", reason="quote location requires validation")
        proposal = runner._publish("kb/model-proposals/" + task_id, "note", raw,
            "methods.survey-reviewer", subjects=[execution["artifact_ref"]])
        runner._publish("command/validation/" + task_id, "note", {"error": "quote differs from inclusion enum"},
            "command.controller", subjects=[proposal["artifact_ref"]])
        runner.resume_session = {"session": 2}
        job = {"name": "survey-review", "actor": "methods.survey-reviewer", "assignment": upgraded,
               "validator": lambda value: validate_survey_review(value, current_map=current_map),
               "normalizer": lambda value: normalize_survey_review_envelope(value, current_map=current_map)}
        with patch.object(runner, "_call_batch", side_effect=AssertionError("paid review was dispatched again")):
            value, execution_ref = runner._models_checked([job])["survey-review"]
        self.assertEqual(execution_ref, execution["artifact_ref"])
        self.assertEqual(value["checks"], raw["checks"])
        self.assertEqual(value["findings"][0]["quote_field"], "reason")
        self.assertEqual(runner._body(proposal), raw)
        self.assertEqual(len(runner.tasks.attempts_for_task(task_id)), 1)
        self.assertIsNone(runner._retained_validation_feedback("survey-review", {**upgraded, "question": "Changed question?"}))

    def test_resume_revalidates_parsed_gap_answer_across_transport_boundary_without_call(self):
        runner = self.runtime()
        base = {"phase": "gap_assessment", "survey_ref": "artifact:kb/surveys/current@1",
                "question": "Does the bounded model explain the transition?"}
        base = runner._follow_up_assignment(base)
        prior_assignment = {**base, "resume_boundary": "gap-assessment-resume-1"}
        current_assignment = {**base, "resume_boundary": "gap-assessment-resume-2"}
        prior_response = {"state": "insufficient_evidence", "evidence": []}
        task_id = "survey-gap-assessment-retained"
        runner.tasks.create(task_id, "verification", {"operation": "model"}, "command.controller")
        runner.tasks.admit(task_id, "command.controller")
        runner.tasks.start_attempt(task_id, "attempt-gap-retained", owner="methods.novelty-verifier",
                                   lease_ttl_seconds=30)
        context = runner._publish("command/contexts/survey-gap-assessment-retained", "note", {
            "client": {"model": "fixture"},
            "prompt": json.dumps(prior_assignment),
        }, "methods.novelty-verifier")
        execution = runner._publish("command/executions/survey-gap-assessment-retained", "report", {
            "text": json.dumps(prior_response),
        }, "methods.novelty-verifier", subjects=[context["artifact_ref"]])
        runner.tasks.finish_attempt("attempt-gap-retained", "succeeded", usage={"model_calls": 1})
        runner.tasks.transition(task_id, "blocked", "command.controller", reason="output requires revalidation")
        proposal = runner._publish("kb/model-proposals/survey-gap-assessment-retained", "note",
            prior_response, "methods.novelty-verifier", subjects=[execution["artifact_ref"]])
        runner._publish("command/validation/survey-gap-assessment-retained", "note", {
            "error": "old transport shape failed before normalization",
        }, "command.controller", subjects=[proposal["artifact_ref"]])
        runner.resume_session = {"session": 2}
        job = {
            "name": "gap-assessment", "actor": "methods.novelty-verifier",
            "assignment": current_assignment,
            "validator": lambda value: self.assertEqual(
                value, {"state": "insufficient_evidence", "evidence": []}),
        }

        with patch.object(runner, "_call_batch",
                          side_effect=AssertionError("retained answer should avoid provider call")):
            result = runner._models_checked([job])["gap-assessment"]

        self.assertEqual(result[0], prior_response)
        self.assertEqual(result[1], execution["artifact_ref"])
        self.assertEqual(runner.tasks.get(task_id)["state"], "completed")
        self.assertEqual(len(runner.tasks.attempts_for_task(task_id)), 1)
        runner.control.close()

    def test_resume_revalidates_grounded_plan_without_inactive_parameter_or_call(self):
        from scisaurus.runtime.literature_tree import normalize_plan, validate_plan
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        assignment = {"phase": "exploration_plan", "question": "Which mechanism explains recall timing?"}
        assignment = runner._follow_up_assignment(assignment)
        source = {"work_id": "W101", "text": "Recall changes with timing.", "representation": "abstract"}
        parents = {"read-node": {"kind": "read", "work_id": "W101", "source_refs": ["source"]}}
        sources = {"source": source}
        prior = {"decision": "expand", "rationale": "Resolve timing dependence.", "branches": [
            {"parent_id": "parent-0", "question": "Which mechanism explains timing dependence?",
             "rationale": "Follow measured timing dependence.", "operation": "search", "query": "recall timing",
             "evidence": [{"work_id": "W101", "source_ref": "source", "quote": source["text"]}]}]}
        task_id = "survey-exploration-retained"
        runner.tasks.create(task_id, "service", {"operation": "model"}, "command.controller")
        runner.tasks.admit(task_id, "command.controller")
        runner.tasks.start_attempt(task_id, "attempt-plan-retained", owner="research.search-planner",
                                   lease_ttl_seconds=30)
        context = runner._publish("command/contexts/" + task_id, "note", {
            "client": {"model": "fixture"}, "prompt": json.dumps(assignment)}, "research.search-planner")
        execution = runner._publish("command/executions/" + task_id, "report", {"text": json.dumps(prior)},
            "research.search-planner", subjects=[context["artifact_ref"]])
        runner.tasks.finish_attempt("attempt-plan-retained", "succeeded", usage={"model_calls": 1})
        runner.tasks.transition(task_id, "blocked", "command.controller", reason="output requires revalidation")
        proposal = runner._publish("kb/model-proposals/" + task_id, "note", prior,
            "research.search-planner", subjects=[execution["artifact_ref"]])
        runner._publish("command/validation/" + task_id, "note", {
            "error": "exploration branch has an invalid envelope"}, "command.controller",
            subjects=[proposal["artifact_ref"]])
        runner.resume_session = {"session": 2}
        job = {"name": "exploration", "actor": "research.search-planner", "assignment": assignment,
            "normalizer": lambda value: normalize_plan(value, {"parent-0": "read-node"}, sources),
            "validator": lambda value: validate_plan(value, parents, sources, max_branches=None)}
        with patch.object(runner, "_call_batch", side_effect=AssertionError("retained answer should avoid provider call")):
            result = runner._models_checked([job])["exploration"]
        self.assertEqual(result[1], execution["artifact_ref"])
        self.assertEqual(result[0]["branches"][0]["query"], "recall timing")
        self.assertIsNone(result[0]["branches"][0]["work_id"])
        self.assertEqual(runner.tasks.get(task_id)["state"], "completed")
        self.assertEqual(len(runner.tasks.attempts_for_task(task_id)), 1)
        self.assertNotIn("work_id", prior["branches"][0])

    def test_focused_semantic_repair_changes_only_failed_field_and_work(self):
        config = survey_config(self.endpoint)
        config["model"]["model"] = "semantic-repair"
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        contexts = [prompt for _, prompt in self.model_contexts(control, store)]
        repairs = [prompt for prompt in contexts if prompt["phase"] == "map" and prompt.get("semantic_feedback")]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["requested_work_ids"], ["W101"])
        self.assertEqual(repairs[0]["semantic_feedback"]["entry_fields"], ["reason"])
        self.assertEqual(repairs[0]["semantic_feedback"]["relationship_targets"], [])
        self.assertEqual(repairs[0]["response_contract"], "scoped_patch")
        self.assertEqual(store.head("kb/work-analyses/W101")["version"], 2)
        first = json.loads(store.read_body(store.get("artifact:kb/work-analyses/W101@1")["body_hash"]))
        second = json.loads(store.read_body(store.head("kb/work-analyses/W101")["body_hash"]))
        self.assertEqual({key: value for key, value in first.items() if key != "reason"},
                         {key: value for key, value in second.items() if key != "reason"})
        for wid in ("W102", "W201", "W301", "W401"):
            self.assertEqual(store.head("kb/work-analyses/"+wid)["version"], 1)
            self.assertEqual(store.head("kb/work-reviews/"+wid)["version"], 1)

    def test_exhausted_claim_is_withdrawn_and_independently_rechecked(self):
        config = survey_config(self.endpoint)
        config["model"]["model"] = "semantic-exhaust"
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        self.assertEqual(store.head("kb/work-reviews/W101")["version"], 2)
        self.assertIsNotNone(store.head("kb/claim-withdrawals/W101"))
        entry = json.loads(store.read_body(store.head("kb/work-analyses/W101")["body_hash"]))
        self.assertNotIn("every task", entry["reason"])
        self.assertIn("does not resolve", entry["reason"])

    def test_exhausted_scientific_review_excludes_work_without_blocking_valid_siblings(self):
        config = survey_config(self.endpoint, "review-never-resolves")
        config["survey"]["seed_work_ids"] = ["W101", "W102", "W201", "W301"]
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result)
        control, store = self.open_store()
        entry = json.loads(store.read_body(store.head("kb/work-analyses/W101")["body_hash"]))
        self.assertEqual(entry["inclusion"], "uncertain")
        self.assertTrue(all(entry[field]["text"] is None for field in MAP_FIELDS))
        exclusion = json.loads(store.read_body(store.head("kb/work-exclusions/W101")["body_hash"]))
        retained = json.loads(store.read_body(store.get(exclusion["retained_analysis_ref"])["body_hash"]))
        self.assertIsNotNone(retained["problem"]["text"])
        self.assertEqual(store.versions("kb/work-analyses/W201"), [1])

    def test_malformed_focused_review_blocks_and_preserves_scientific_claims(self):
        config = survey_config(self.endpoint, "review-malformed")
        config["survey"]["seed_work_ids"] = ["W101", "W102", "W201", "W301"]
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "blocked", result)
        self.assertFalse(result["survey_current"])
        self.assertEqual(result["failure"]["kind"], "unchanged_assignment_exhausted")
        _, store = self.open_store()
        entry = json.loads(store.read_body(store.head("kb/work-analyses/W101")["body_hash"]))
        self.assertEqual(entry["inclusion"], "included")
        self.assertIsNotNone(entry["problem"]["text"])
        self.assertIsNone(store.head("kb/work-exclusions/W101"))
        self.assertIsNone(store.head("kb/work-reviews/W101"))
        self.assertIsNone(store.head("command/work-review-repairs/W101"))
        self.assertIsNone(store.head("command/executions/survey-review-contract-exhausted-W101"))

    def test_critiqued_abstention_supplies_bound_status_and_exact_check_contract(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W102")
        runner._map()
        obligation = self.review_obligation(runner, "W101")
        runner.review_obligations = runner._validate_review_obligations([obligation])
        runner._materialize_source_less_map("W101", runner.analyzed_basis["W101"], scope="review_exhausted")
        runner._review_work_claims()
        prompt = [value for _, value in self.model_contexts(runner.control, runner.store)
                  if value.get("phase") == "work_review" and value["entry"]["work_id"] == "W101"][-1]
        receipt = runner.store.head("command/survey-abstentions/W101")
        self.assertEqual(prompt["controller_abstention"], {"ref": receipt["artifact_ref"],
            "body_hash": receipt["body_hash"], "body": runner._body(receipt)})
        self.assertIn(receipt["artifact_ref"], runner._work_review_basis("W101"))
        contracts = prompt["response_contract"]["checks"]
        adjudications = prompt["response_contract"]["critique_adjudications"]
        self.assertEqual([row["check_id"] for row in [*contracts, *adjudications]], prompt["required_checks"])
        for row in contracts:
            self.assertEqual(row["required_fields"], ["check_id", "outcome", "method", "result"])
        for row in adjudications:
            self.assertTrue(row["check_id"].startswith("critique:"))
            self.assertEqual(row["required_fields"], ["check_id", "disposition", "method", "result", "affected_check_ids"])
        old_review = runner._body(runner.work_reviews["W101"])
        self.assertTrue(runner._review_protocol_matches(old_review))
        renewed = runner._publish("command/survey-abstentions/W101", "note", runner._body(receipt),
                                 "command.controller", subjects=runner.analyzed_basis["W101"])
        self.assertNotEqual(renewed["artifact_ref"], receipt["artifact_ref"])
        self.assertFalse(runner._work_review_current("W101"))
        self.assertFalse(runner._review_protocol_matches(old_review))
        runner._review_work_claims()
        self.assertTrue(runner._work_review_current("W101"))

        runner._accept_survey()
        runner.gate.require_current(runner.survey_ref)
        original_execution = runner.gate._model_review_execution
        def false_status(execution_ref, actor):
            execution, context, prompt, reply = original_execution(execution_ref, actor)
            if prompt.get("controller_abstention") is not None:
                prompt = deepcopy(prompt)
                prompt["controller_abstention"]["body"]["reason"] = "No abstract was captured."
            return execution, context, prompt, reply
        with patch.object(runner.gate, "_model_review_execution", side_effect=false_status):
            with self.assertRaisesRegex(ValidationError, "controller abstention"):
                runner.gate.require_current(runner.survey_ref)

    def test_controller_abstention_context_rejects_false_status_and_surviving_relationship(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        runner._materialize_source_less_map("W101", runner.analyzed_basis["W101"], scope="review_exhausted")
        entry = runner.analysis_records["W101"]
        self.assertIsNotNone(runner._work_abstention_context(entry, []))
        self.assertIsNone(runner._work_abstention_context(entry, ["artifact:kb/relationships/retained@1"]))
        receipt = runner.store.head("command/survey-abstentions/W101")
        runner._publish("command/survey-abstentions/W101", "note",
                        {**runner._body(receipt), "scope": "source_unavailable"}, "command.controller")
        self.assertIsNone(runner._work_abstention_context(entry, []))

    def test_resume_does_not_replenish_exhausted_scientific_repair(self):
        config = survey_config(self.endpoint, "review-never-resolves")
        config["limits"]["max_rounds"] = 2
        with patch.object(SurveyRunner, "_exclude_unresolved_work", side_effect=KeyboardInterrupt("legacy final-review stop")):
            first = self.runtime(config).run()
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["failure"], {"kind": "process_interrupted"})
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["focused_review"]}}
        resumed = self.runtime(config, resume_policy=policy)
        self.addCleanup(resumed.control.close)
        self.assertTrue(resumed._work_review_exhausted("W101"))
        with patch.object(resumed, "_call_batch", side_effect=AssertionError("exhausted review was redispatched")):
            resumed._review_work_claims()
        self.assertIsNotNone(resumed.store.head("kb/work-exclusions/W101"))

    def test_semantic_repair_ignores_and_records_an_ungranted_field_change(self):
        config = survey_config(self.endpoint)
        config["model"]["model"] = "semantic-unscoped"
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        _, store = self.open_store()
        first = json.loads(store.read_body(store.get("artifact:kb/work-analyses/W101@1")["body_hash"]))
        current = json.loads(store.read_body(store.head("kb/work-analyses/W101")["body_hash"]))
        self.assertEqual({k: v for k, v in first.items() if k != "reason"},
                         {k: v for k, v in current.items() if k != "reason"})
        self.assertEqual(current["reason"], "The study examines recall timing.")
        self.assertEqual(current["finding"], first["finding"])
        refs = store.control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE ?",
            ("command/map-projections/W101-%",),
        ).fetchall()
        projections = [json.loads(store.read_body(store.get(row["artifact_ref"])["body_hash"]))
                       for row in refs]
        self.assertTrue(any(issue["kind"] == "ungranted_entry_field_ignored"
                            and issue["field"] == "finding"
                            for projection in projections for issue in projection["issues"]))

    def test_revision_waves_reserve_review_for_previously_repaired_work(self):
        config = survey_config(self.endpoint, "semantic-many")
        config["survey"]["seed_work_ids"] = ["W101", "W102", "W201", "W301"]
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        decisions = [item for item in result["time_decisions"] if item["stage"] == "revision"]
        self.assertTrue(any(item["pending_review_count"] > 0 for item in decisions))
        self.assertTrue(all(item["reserved_review_seconds"] >= item["pending_review_count"] * 0.1
                            for item in decisions))

    def test_abstract_only_run_retains_search_expansion_and_scoped_map(self):
        runner = self.runtime()
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["gap_state"], "insufficient_evidence")
        self.assertTrue(result["survey_current"])
        self.assertEqual(result["coverage"]["unique_works"], 5)
        self.assertEqual(result["coverage"]["verified_full_texts"], 0)
        self.assertTrue(any(row["seed_work_ids"] == ["W101"] and row["new_unique_works"] == 2
                            for row in result["coverage"]["expansion"]))
        searches = result["coverage"]["searches"]
        self.assertEqual(len(searches), 6)
        self.assertEqual(len(SurveyHTTPFixture.requests), 7)
        self.assertEqual(result["usage"]["cumulative_usage"]["retrieval_calls"], 7)
        self.assertEqual(sum(query["new_unique_works"] for query in searches), 5)
        self.assertTrue(any(request["path"] == "/works/W102" for request in SurveyHTTPFixture.requests))
        self.assertTrue(any(request["query"].get("filter") == ["cites:W101"] for request in SurveyHTTPFixture.requests))
        control, store = self.open_store()
        contexts = self.model_contexts(control, store)
        blind = [(record, prompt) for record, prompt in contexts if prompt["phase"] == "blind_plan"]
        self.assertEqual({record["author"] for record, _ in blind}, {"research.search-planner", "methods.blind-search-planner"})
        for _, prompt in blind:
            self.assertNotIn(GAP, json.dumps(prompt))
            self.assertNotIn("gap", prompt)
        maps = [prompt for _, prompt in contexts if prompt["phase"] == "map"]
        self.assertEqual({prompt["requested_work_ids"][0] for prompt in maps[:4]}, {"W101", "W102", "W201", "W301"})
        self.assertTrue(all(len(prompt["requested_work_ids"]) == 1 for prompt in maps))
        self.assertEqual(maps[4]["requested_work_ids"], ["W401"])
        serialized_maps = [json.dumps(prompt, ensure_ascii=False, separators=(",", ":")) for prompt in maps[:4]]
        self.assertTrue(all(prompt["question"] == runner.score["question"] for prompt in maps))
        self.assertLess(serialized_maps[0].index('"works"'), serialized_maps[0].index('"requested_work_ids"'))
        self.assertLess(serialized_maps[0].index('"instructions"'), serialized_maps[0].index('"requested_work_ids"'))
        for wid in ("W101", "W102", "W201", "W301"):
            self.assertEqual(store.head("kb/work-analyses/" + wid)["version"], 1)
        exported = json.loads((runner.dir / "output/literature-map.json").read_text())
        self.assertEqual(exported["survey_ref"], result["survey_ref"])
        self.assertTrue(exported["survey_current"])
        mapping = exported["map"]
        self.assertIn({"source": "W101", "target": "W102", "kind": "cites"}, mapping["citation_edges"])
        self.assertIn({"source": "W301", "target": "W101", "kind": "cites"}, mapping["citation_edges"])
        self.assertIsNotNone(result["time_plan"]["first_verified_result"])
        self.assertTrue(result["event_chain"][0])

    def test_missing_seed_work_is_recorded_without_pagination_key_error(self):
        config = survey_config(self.endpoint)
        config["survey"]["seed_work_ids"] = ["W404"]
        runner = self.runtime(config)
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        missing = [row for row in runner.search_log
                   if row["request"]["operation"] == "work"
                   and row["request"]["work_id"] == "W404"]
        self.assertEqual(len(missing), 1)
        self.assertEqual({key: missing[0][key] for key in ("count", "next_cursor", "has_more")},
                         {"count": 0, "next_cursor": None, "has_more": False})

    def test_countersearch_reserve_prevents_discovery_from_filling_work_budget(self):
        config = survey_config(self.endpoint)
        config["survey"]["search"].update(max_works=4, challenge_reserve=1)
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["coverage"]["unique_works"], 4)
        _, store = self.open_store()
        register = json.loads(store.read_body(store.head("kb/work-register")["body_hash"]))
        work_ids = {store.get(ref)["artifact_id"].removeprefix("kb/works/")
                    for ref in register["work_refs"]}
        self.assertIn("W401", work_ids)
        self.assertEqual(len(work_ids - {"W401"}), 3)
        self.assertTrue(any(node["kind"] == "acquisition" and node["state"] == "deferred"
                            for node in result["coverage"]["exploration_tree"]["nodes"]))

    def test_countersearch_api_tranche_survives_base_call_cap(self):
        """A saturated discovery budget cannot starve the falsification query."""
        config = survey_config(self.endpoint)
        config["survey"]["search"].update(max_api_calls=2, expansion_rounds=0)
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertTrue(result["survey_current"])
        self.assertTrue(any(request["query"].get("search") == ["prior solution"]
                            for request in SurveyHTTPFixture.requests))
        self.assertGreaterEqual(result["usage"]["cumulative_usage"]["retrieval_calls"], 3)

    def test_automatic_nomination_uses_current_accepted_survey(self):
        config = survey_config(self.endpoint)
        config["survey"]["proposed_gap"] = None
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["nomination"], {"id": "delayed-recall", "statement": GAP})
        control, store = self.open_store()
        nomination = next(prompt for _, prompt in self.model_contexts(control, store) if prompt["phase"] == "nomination")
        self.assertEqual(nomination["survey_ref"], nomination["prerequisite_survey_ref"])
        self.assertNotEqual(nomination["survey_ref"], result["survey_ref"])

    def test_nomination_resume_retains_accepted_discovery_for_exact_work_orders(self):
        config = survey_config(self.endpoint)
        config["survey"]["proposed_gap"] = None
        orders = [self.follow_up_order()]
        first = self.runtime(config, work_orders=orders)
        with patch.object(first, "_nominate", side_effect=ModelContractError("nomination response malformed")):
            failed = first.run()
        self.assertTrue(failed["survey_current"])
        self.assertFalse(failed["assessment_current"])
        self.assertIsNone(failed["nomination"])
        policy = {"additional_seconds": config["limits"]["wall_clock_seconds"],
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["gap_assessment"]}}
        second = self.runtime(config, resume_policy=policy, work_orders=orders)
        self.assertTrue(second.follow_up_discovery_current)
        self.assertFalse(second.counter_queries_complete)
        with patch.object(second, "_prepare_follow_up", side_effect=AssertionError("discovery already accepted")), \
             patch.object(second, "_explore", side_effect=AssertionError("discovery must not repeat")):
            completed = second.run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertTrue(completed["assessment_current"])
        changed = self.runtime(config, resume_policy=policy, work_orders=[{**orders[0], "id": "new-evidence"}])
        self.assertFalse(changed.follow_up_discovery_current)
        self.assertFalse(changed.counter_queries_complete)
        changed.control.close()

    def test_stale_accepted_map_retains_discovery_and_reopens_review_with_tree(self):
        config = survey_config(self.endpoint)
        config["survey"]["proposed_gap"] = None
        orders = [self.follow_up_order()]
        first = self.runtime(config, work_orders=orders)
        with patch.object(first, "_nominate", side_effect=ModelContractError("nomination response malformed")):
            failed = first.run()
        self.assertTrue(failed["survey_current"])
        policy = {"additional_seconds": config["limits"]["wall_clock_seconds"],
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["integrated_review"]}}
        editor = self.runtime(config, resume_policy=policy, work_orders=orders)
        entry = editor.analysis_records["W101"]
        editor._publish(entry["artifact_id"], "note", editor._body(entry), "research.literature-mapper")
        editor._tree_load(); editor._tree_save()
        editor.control.close()
        second = self.runtime(config, resume_policy=policy, work_orders=orders)
        self.assertIsNone(second.survey_ref)
        self.assertIsNotNone(second.exploration_tree)
        self.assertTrue(second.follow_up_discovery_current)
        with patch.object(second, "_prepare_follow_up", side_effect=AssertionError("accepted acquisition repeated")), \
             patch.object(second, "_explore", side_effect=AssertionError("review scope cannot reopen discovery")):
            completed = second.run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertTrue(completed["survey_current"])
        self.assertTrue(completed["assessment_current"])


    def test_nomination_identifier_drift_reaches_countersearch_and_assessment(self):
        config = survey_config(self.endpoint, "nomination-id-drift")
        config["survey"]["proposed_gap"] = None
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertTrue(result["survey_current"])
        self.assertTrue(result["assessment_current"])
        self.assertEqual(result["nomination"]["statement"], GAP)
        self.assertEqual(result["nomination"]["id"], normalize_gap_nomination({"id": "INVALID", "statement": GAP})["id"])
        control, store = self.open_store()
        prompts = self.model_contexts(control, store)
        self.assertEqual(sum(prompt["phase"] == "nomination" for _, prompt in prompts), 1)
        for _, prompt in prompts:
            if prompt["phase"] in {"counter_plan", "gap_assessment"}:
                self.assertEqual(prompt["gap"], result["nomination"])

    def test_parallel_mapping_with_invalid_quote_does_not_retry_or_rewrite_siblings(self):
        config = survey_config(self.endpoint, "map-repair")
        config["survey"]["seed_work_ids"] = ["W101", "W102", "W201", "W301"]
        config["limits"]["max_rounds"] = 2
        before_retry = []
        original = SurveyRunner._checkpoint
        def inspect_retry(runner, phase, **kwargs):
            original(runner, phase, **kwargs)
            if phase != "executing":
                return
            contexts = self.model_contexts(runner.control, runner.store)
            if contexts and contexts[-1][1].get("validation_feedback") and not before_retry:
                before_retry.append({wid: runner.store.head("kb/work-analyses/" + wid)["artifact_ref"]
                                     for wid in ("W201", "W102", "W301")})
        with patch.object(SurveyRunner, "_checkpoint", inspect_retry):
            result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(before_retry, [])
        control, store = self.open_store()
        maps = [(record, prompt) for record, prompt in self.model_contexts(control, store) if prompt["phase"] == "map"]
        counts = {wid: sum(prompt["requested_work_ids"] == [wid] for _, prompt in maps)
                  for wid in ("W101", "W201", "W102", "W301", "W401")}
        self.assertEqual(counts, {"W101": 1, "W201": 1, "W102": 1, "W301": 1, "W401": 1})
        self.assertFalse(any("validation_feedback" in prompt for _, prompt in maps))
        for wid in ("W201", "W102", "W301"):
            self.assertEqual(store.versions("kb/work-analyses/" + wid), [1])
        timings = []
        for record, _ in maps[:2]:
            task = record["artifact_id"].removeprefix("command/contexts/")
            execution = json.loads(store.read_body(store.head("command/executions/" + task)["body_hash"]))
            timings.append(json.loads(execution["model"]))
        self.assertNotEqual(timings[0]["pid"], timings[1]["pid"])
        self.assertGreater(min(row["finished"] for row in timings) - max(row["started"] for row in timings), 0.1)
        production = [decision for decision in result["time_decisions"] if decision["stage"] == "production"]
        self.assertEqual([(row["task_count"], row["pending_review_count"]) for row in production[:3]], [(2, 0), (2, 2), (1, 0)])
        self.assertTrue(all(row["worker_slots"] == 2 and row["reserved_review_seconds"] > 0 for row in production))

    def test_multi_provider_mapping_uses_a_bounded_rolling_dispatch_window(self):
        config = survey_config(self.endpoint)
        config["model"]["base_url"] = "http://127.0.0.1:1/v1"
        config["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 3, "base_urls": ["http://127.0.0.1:1/v1"]},
            "qwen": {"max_concurrent": 1, "base_urls": ["https://qwen.invalid/v1"]},
        }
        runner = self.runtime(config)
        self.addCleanup(runner.control.close)
        runner._initialize()
        runner._complete = lambda task_id: None
        batches = []

        def fake_call(specs, *, max_parallel=None):
            batches.append((len(specs), max_parallel))
            return {
                spec["task_id"]: {
                    "ok": True,
                    "result": {
                        "text": json.dumps({"accepted": True}), "model": "fixture",
                        "usage": {"model_calls": 1, "input_tokens": 1, "output_tokens": 1},
                        "elapsed_seconds": 0.01, "finish_reason": "stop",
                    },
                    "record_ref": "artifact:command/executions/" + spec["task_id"],
                }
                for spec in specs
            }

        jobs = [{"name": f"job-{index}", "actor": "research.literature-mapper",
                 "assignment": {"phase": "map", "job": index},
                 "validator": lambda value: None}
                for index in range(5)]
        with patch.object(runner, "_call_batch", side_effect=fake_call):
            result = runner._models_checked(jobs, stage="production", task_kind="production")
        self.assertEqual(set(result), {f"job-{index}" for index in range(5)})
        self.assertEqual(batches, [(4, 2), (1, 2)])

    def test_unsupported_map_claim_is_withdrawn_without_retry_or_sibling_rewrites(self):
        config = survey_config(self.endpoint, "map-reject")
        config["survey"]["seed_work_ids"] = ["W101", "W102", "W201", "W301"]
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        abstention = json.loads(store.read_body(store.head("kb/work-analyses/W101")["body_hash"]))
        self.assertEqual(abstention["inclusion"], "uncertain")
        self.assertTrue(all(abstention[field] == {"text": None, "evidence": []} for field in MAP_FIELDS))
        for wid in ("W201", "W102", "W301"):
            self.assertEqual(store.versions("kb/work-analyses/" + wid), [1])
        maps = [prompt for _, prompt in self.model_contexts(control, store) if prompt["phase"] == "map"]
        self.assertEqual(sum(prompt["requested_work_ids"] == ["W101"] for prompt in maps), 1)
        self.assertEqual(len(maps), 5)
        decision = json.loads(store.read_body(
            store.head("command/survey-abstentions/W101")["body_hash"]))
        self.assertEqual(decision["scope"], "unverified_map")
        self.assertIsNotNone(decision["execution_ref"])

    def test_updated_target_rechecks_its_directed_relationship_owner(self):
        SurveyHTTPFixture.refresh_target = True
        config = self.full_text_config("map-links")
        config["survey"]["seed_work_ids"] = ["W101", "W102", "W201", "W301"]
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        maps = [prompt for _, prompt in self.model_contexts(control, store) if prompt["phase"] == "map"]
        self.assertEqual({prompt["requested_work_ids"][0] for prompt in maps[4:]}, {"W401", "W102", "W101"})
        assigned_fulltext = next(prompt for prompt in maps[4:] if prompt["requested_work_ids"] == ["W401"])
        self.assertTrue(any(source["representation"] == "full_text" for source in assigned_fulltext["sources"]))
        for prompt in maps[4:]:
            if prompt["requested_work_ids"] != ["W401"]:
                self.assertTrue(all(source["representation"] == "abstract" for source in prompt["sources"]))
        relationship = json.loads(store.read_body(store.head("kb/relationships/W101-W102-compares")["body_hash"]))
        target_proof = next(proof for proof in relationship["claim"]["evidence"] if proof["work_id"] == "W102")
        self.assertEqual(target_proof["source_ref"], store.head("kb/abstracts/W102")["artifact_ref"])
        self.assertEqual(store.versions("kb/relationships/W101-W102-compares"), [1, 2])
        self.assertEqual(store.versions("kb/work-analyses/W101"), [1])
        self.assertEqual(store.versions("kb/work-analyses/W201"), [1])
        self.assertEqual(store.versions("kb/work-analyses/W301"), [1])

    def test_relationship_recheck_cannot_rewrite_unchanged_owner_entry(self):
        SurveyHTTPFixture.refresh_target = True
        config = survey_config(self.endpoint, "map-links-rewrite")
        config["survey"]["seed_work_ids"] = ["W101", "W102", "W201", "W301"]
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        _, store = self.open_store()
        self.assertEqual(store.versions("kb/work-analyses/W101"), [1])
        refs = store.control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE ?",
            ("command/map-projections/W101-%",),
        ).fetchall()
        projections = [json.loads(store.read_body(store.get(row["artifact_ref"])["body_hash"]))
                       for row in refs]
        self.assertTrue(any(issue["kind"] == "ungranted_entry_field_ignored"
                            and issue["field"] == "reason"
                            for projection in projections for issue in projection["issues"]))
        mapped = json.loads(store.read_body(store.head("kb/literature-map")["body_hash"]))
        self.assertEqual(len(mapped["relationship_refs"]), 1)
        self.assertEqual(store.versions("kb/relationships/W101-W102-compares"), [1, 2])

    def test_fabricated_quote_never_reaches_survey_acceptance(self):
        result = self.runtime(survey_config(self.endpoint, "forged-quote")).run()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("No substantive literature claim survived", result["error"])
        self.assertIsNone(result["survey_ref"])
        self.assertIsNone(result["assessment_ref"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])

    def test_model_selected_reading_ignores_legacy_paper_quota_and_retains_countersearch(self):
        config = survey_config(self.endpoint)
        config["survey"]["seed_work_ids"] = ["W101", "W102", "W201", "W301"]
        config["survey"]["search"]["max_analyzed_works"] = 3
        runner = self.runtime(config)
        setup = runner._setup
        def retained_catalog():
            setup()
            for wid in config["survey"]["seed_work_ids"]:
                runner._bibliographic_call("work", role="research.search-planner", work_id=wid,
                                          result_limit=1, plan_ref=runner.protocol["artifact_ref"])
        with patch.object(runner, "_setup", side_effect=retained_catalog):
            result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        contexts = [prompt for _, prompt in self.model_contexts(control, store)]
        maps = [prompt for prompt in contexts if prompt["phase"] == "map"]
        reviews = [prompt for prompt in contexts if prompt["phase"] == "work_review"]
        self.assertEqual(len(maps), 5)
        self.assertEqual(len(reviews), 5)
        self.assertEqual({p["requested_work_ids"][0] for p in maps}, {"W101", "W102", "W201", "W301", "W401"})
        self.assertIn(["W401"], [p["requested_work_ids"] for p in maps])
        self.assertIsNone(result["coverage"]["deep_analysis_limit"])
        self.assertEqual(result["coverage"]["abstentions"], [])

    def test_time_admission_counts_deep_analysis_not_catalog_size(self):
        config = survey_config(self.endpoint)
        config["survey"]["search"].update(max_works=120, max_analyzed_works=3)
        runner = self.runtime(config)
        self.addCleanup(runner.control.close)
        self.assertEqual(runner.time_policy.unit_count, 3)
        self.assertEqual(runner.bounds["max_works"], 120)

    def test_expanded_analysis_scope_can_promote_a_deferred_entry_once(self):
        config = survey_config(self.endpoint)
        config["survey"]["search"]["max_analyzed_works"] = 2
        runner = self.runtime(config)
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102", "W201"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        deferred = [wid for wid in runner.works if runner._is_deferred_analysis(wid)]
        self.assertEqual(len(deferred), 2)
        runner.bounds["max_analyzed_works"] = 4
        runner._map()
        for wid in deferred:
            self.assertEqual(runner._body(runner.analysis_records[wid])["problem"]["text"], "Recall timing is examined.")
            self.assertFalse(runner._is_deferred_analysis(wid))
        self.assertEqual(runner._coverage()["abstentions"], [])
        with patch.object(runner, "_call_batch") as dispatch:
            runner._map()
        dispatch.assert_not_called()

    def test_review_only_scope_retains_deferred_entries_and_allows_changed_source_basis(self):
        config = survey_config(self.endpoint)
        config["survey"]["search"]["max_analyzed_works"] = 2
        runner = self.runtime(config)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102", "W201"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        wid = next(wid for wid in runner.works if runner._is_deferred_analysis(wid))
        original = deepcopy(runner.analysis_records[wid])
        runner.resume_session = {"session": 2, "reopened_scopes": ["focused_review", "integrated_review", "gap_assessment"]}
        with patch.object(runner, "_analysis_selection", return_value={wid}), patch.object(runner, "_models_checked") as dispatch:
            runner._map()
            dispatch.assert_not_called()
        self.assertEqual(runner.analysis_records[wid], original)
        old_ref, old_source = next((ref, source) for ref, source in runner.source_docs.items() if source["work_id"] == wid)
        text = old_source["text"] + " A second captured observation is available."
        added = runner._record(f"kb/abstracts/{wid}", "source_capture", {"work_id": wid, "abstract": text},
                               "research.cataloger", subjects=[old_source["execution_ref"]])
        runner.source_docs[added["artifact_ref"]] = {**old_source, "text": text}
        with patch.object(runner, "_analysis_selection", return_value={wid}):
            runner._map()
        self.assertNotEqual(runner.analysis_records[wid]["artifact_ref"], original["artifact_ref"])
        self.assertIn(added["artifact_ref"], runner.analyzed_basis[wid])

    def test_countersearch_retains_hypothesis_across_accepted_planning_snapshots(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._accept_survey(); runner._nominate()
        nominee, hypothesis = deepcopy(runner.nomination_record), deepcopy(runner.nomination)
        origin = runner.survey_ref
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W102")
        runner._accept_survey()
        planning = runner.survey_ref
        self.assertNotEqual(origin, planning)
        runner._countersearch()
        self.assertEqual(runner.nomination_record, nominee)
        self.assertEqual(runner.nomination, hypothesis)
        self.assertTrue(runner.countersearch_complete)
        lineage = countersearch_lineage(runner.control, runner.store, score_ref=runner.score_ref,
            question=runner.score["question"], nomination_ref=nominee["artifact_ref"],
            plan_ref=runner.counter_plan_record["artifact_ref"], survey_ref=runner.survey_ref)
        self.assertEqual(lineage["origin_survey_ref"], origin)
        self.assertEqual(lineage["planning_survey_ref"], planning)
        self.assertTrue(lineage["complete"])
        self.assertTrue(lineage["survey_current"])
        with patch.object(runner, "_refresh_countersearch_state", side_effect=lambda: setattr(runner, "countersearch_complete", False)):
            with self.assertRaises(StateError):
                runner._countersearch()
        with patch.object(runner, "_countersearch", side_effect=StateError("Invalid owned completion dependency")):
            result = runner.run()
        self.assertEqual(result["failure"], {"kind": "operational_state"})

    def test_countersearch_lineage_rejects_wrong_question_owner_and_nomination(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._accept_survey(); runner._nominate(); runner._countersearch()
        arguments = {"score_ref": runner.score_ref, "question": runner.score["question"],
            "nomination_ref": runner.nomination_record["artifact_ref"], "plan_ref": runner.counter_plan_record["artifact_ref"],
            "survey_ref": runner.survey_ref}
        with self.assertRaises(StateError):
            countersearch_lineage(runner.control, runner.store, **{**arguments, "question": "A different question"})
        query = runner.store.get(runner.counter_query_refs[0])
        query_body = runner._body(query)
        wrong_query = runner._publish("kb/queries/wrong-owner", "query_record", query_body,
            "research.seed-reader", subjects=[item["ref"] for item in query["inputs"]])
        with self.assertRaises(StateError):
            countersearch_lineage(runner.control, runner.store, **arguments, query_refs=[wrong_query["artifact_ref"]])
        mismatched_query = runner._publish("kb/queries/wrong-request", "query_record",
            {**query_body, "request": {**query_body["request"], "query": "A search that was never executed"}},
            "methods.novelty-challenger", subjects=[item["ref"] for item in query["inputs"]])
        with self.assertRaises(StateError):
            countersearch_lineage(runner.control, runner.store, **arguments, query_refs=[mismatched_query["artifact_ref"]])
        body = runner._body(runner.counter_plan_record)
        wrong = runner._publish("kb/counter-search-plan", "note", body, "research.literature-mapper",
                                subjects=[item["ref"] for item in runner.counter_plan_record["inputs"]])
        with self.assertRaises(StateError):
            countersearch_lineage(runner.control, runner.store, **{**arguments, "plan_ref": wrong["artifact_ref"]})
        changed = runner._publish("kb/gap-nomination", "note", {**runner._body(runner.nomination_record),
            "statement": "A changed hypothesis"}, "research.gap-proposer", subjects=[runner.survey_ref])
        with self.assertRaises(StateError):
            countersearch_lineage(runner.control, runner.store, **{**arguments, "nomination_ref": changed["artifact_ref"]})

    def review_obligation(self, runner, wid):
        entry = runner.analysis_records[wid]
        relations = [value for value in runner.relationships.values() if value["source"] == wid]
        source_refs = {proof["source_ref"] for field in MAP_FIELDS
                       for proof in runner._body(entry)[field]["evidence"]}
        source_refs.update(proof["source_ref"] for value in relations for proof in value["claim"]["evidence"])
        return {"receipt_ref": "artifact:command/operator-review/critique@1", "receipt_body_sha256": "a" * 64,
                "work_id": wid, "entry_ref": entry["artifact_ref"], "entry_body_sha256": entry["body_hash"],
                "relationship_pins": [{"ref": value["artifact_ref"], "body_hash": runner.store.get(value["artifact_ref"])["body_hash"]}
                                      for value in relations],
                "source_pins": [{"ref": ref, "body_hash": runner.store.get(ref)["body_hash"]} for ref in sorted(source_refs)],
                "hypothesis": "Independently determine whether every qualification in this screening rationale is supported by the pinned source text."}

    def test_new_abstention_receipt_invalidates_old_deterministic_review_without_model_calls(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        runner._materialize_source_less_map("W102", runner.analyzed_basis["W102"], scope="reading_deferred")
        self.assertEqual(runner._body(runner.analysis_records["W102"])["reason"], ABSTENTION_REASONS["reading_deferred"])
        runner._review_work_claims()
        old_review = runner.work_reviews["W102"]["artifact_ref"]
        abstention = runner.store.head("command/survey-abstentions/W102")
        renewed = runner._publish("command/survey-abstentions/W102", "note", runner._body(abstention),
                                 "command.controller", subjects=runner.analyzed_basis["W102"])
        self.assertNotEqual(renewed["artifact_ref"], abstention["artifact_ref"])
        self.assertFalse(runner._work_review_current("W102"))
        calls = runner.model_calls_dispatched
        runner._review_work_claims()
        self.assertEqual(runner.model_calls_dispatched, calls)
        self.assertNotEqual(runner.work_reviews["W102"]["artifact_ref"], old_review)
        self.assertEqual(runner._body(runner.work_reviews["W102"])["execution_ref"], renewed["artifact_ref"])
        runner._accept_survey()
        self.assertIsNotNone(runner.survey_ref)
        runner.gate.require_current(runner.survey_ref)

    def test_scoped_read_reviews_only_completed_analysis_then_finalizes_remaining_map(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        old = runner.analysis_records["W102"]["artifact_ref"]
        runner.analyzed_basis.pop("W102")
        runner._tree_admitted_reads = {"W101"}
        runner._review_work_claims()
        self.assertIn("W101", runner.reviewed_basis)
        self.assertNotIn("W102", runner.work_reviews)
        self.assertEqual(runner.analysis_records["W102"]["artifact_ref"], old)
        runner._tree_admitted_reads = None
        with self.assertRaisesRegex(StateError, "completed current analysis"):
            runner._review_work_claims()
        runner._map(); runner._review_work_claims(); runner._accept_survey()
        self.assertIn("W102", runner.reviewed_basis)
        self.assertIsNotNone(runner.survey_ref)

    def test_critique_context_preserves_original_and_exact_current_revision(self):
        runner = self.runtime(survey_config(self.endpoint, "aggregate-history-adversary"))
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map(); runner._review_work_claims()
        obligation = self.review_obligation(runner, "W101")
        original = deepcopy(runner._body(runner.analysis_records["W101"]))
        runner.review_obligations = runner._validate_review_obligations([obligation])
        revised = deepcopy(original); revised["reason"] = "The captured study examines recall timing."
        runner.analysis_records["W101"] = runner._record("kb/work-analyses/W101", "note", revised,
            "research.literature-mapper", subjects=runner.analyzed_basis["W101"])
        runner._accept_survey()
        prompts = [prompt for _, prompt in self.model_contexts(runner.control, runner.store)]
        focused = next(prompt for prompt in reversed(prompts) if prompt.get("phase") == "work_review")
        context = focused["critique_contexts"][0]
        self.assertEqual(context["original_entry"]["body"], original)
        self.assertEqual(context["current_entry"]["body"], revised)
        self.assertEqual(context["current_entry"]["ref"], focused["entry_ref"])
        self.assertEqual(context["changed_entry_fields"], ["reason"])
        self.assertEqual(focused["review_obligations"], [obligation])
        self.assertIn(context["check_id"], focused["required_checks"])
        self.assertEqual(context["protocol"], "literature-critique-transition-3")
        aggregate = next(prompt for prompt in reversed(prompts) if prompt.get("phase") == "survey_review")
        self.assertNotIn("critique_contexts", aggregate)
        self.assertNotIn("review_obligations", aggregate)
        self.assertEqual(aggregate["independent_critique_receipts"], [obligation["receipt_ref"]])
        self.assertIn("W101", aggregate["map"]["entry_refs"])
        self.assertEqual(aggregate["map"]["entry_refs"]["W101"], focused["entry_ref"])
        self.assertTrue(any(source["source_ref"] == obligation["source_pins"][0]["ref"] for source in aggregate["sources"]))
        self.assertTrue(runner._review_protocol_matches(runner._body(runner.work_reviews["W101"])))

    def test_distinct_review_failures_preserve_each_durable_repair_allowance(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        scope = runner._review_evidence_scope("W101")
        def failure(field):
            checks = check_rows(work_review_checks([]))
            next(check for check in checks if check["check_id"] == field)["outcome"] = "failed"
            return {"entry_ref": runner.analysis_records["W101"]["artifact_ref"], "relationship_refs": [],
                    "checks": checks, "rationale": "A scoped test failure.", "evidence_scope": scope}
        a, b = failure("reason"), failure("problem")
        ar = runner._record("kb/work-reviews/W101", "note", a, "methods.work-reviewer")
        runner._record("kb/work-reviews/W101", "note", b, "methods.work-reviewer")
        with patch.object(runner, "_review_evidence_scope", side_effect=lambda wid, review=None: review.get("evidence_scope") if review else scope):
            runner._record("command/work-review-repairs/W101", "note", {
                "evidence_scope": scope, "repair_attempts": 3, "failure_keys": ["reason"], "review_ref": ar["artifact_ref"]}, "command.controller")
            runner._record("command/work-review-repairs/W101", "note", {
                "evidence_scope": scope, "repair_attempts": 1, "failure_keys": ["problem"], "review_ref": ar["artifact_ref"]}, "command.controller")
            self.assertEqual(runner._work_review_failure_count("W101", a), 4)
            self.assertEqual(runner._work_review_failure_count("W101", b), 2)
        relation = runner._record("kb/relationships/repair-target", "note", {"source": "W101", "target": "W102", "kind": "related", "claim": "First claim"}, "research.literature-mapper")
        revision = runner._record("kb/relationships/repair-target", "note", {"source": "W101", "target": "W102", "kind": "related"}, "research.literature-mapper")
        self.assertEqual(runner._review_failure_keys({"checks": [{"check_id": "relationship:"+relation["artifact_ref"], "outcome": "failed"}]}),
                         runner._review_failure_keys({"checks": [{"check_id": "relationship:"+revision["artifact_ref"], "outcome": "failed"}]}))

    def test_aggregate_critique_keeps_excluded_claimless_target_and_source_visible(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        obligation = self.review_obligation(runner, "W101")
        runner.review_obligations = runner._validate_review_obligations([obligation])
        entry = deepcopy(runner._body(runner.analysis_records["W101"]))
        entry.update(inclusion="excluded", reason="The connection to the declared question is disputed.")
        entry.update({field: {"text": None, "evidence": []} for field in MAP_FIELDS})
        runner.analysis_records["W101"] = runner._record("kb/work-analyses/W101", "note", entry,
            "research.literature-mapper", subjects=runner.analyzed_basis["W101"])
        packet = runner._survey_review_packet()
        self.assertEqual(packet["map"]["entries"], [entry])
        self.assertTrue({pin["ref"] for pin in obligation["source_pins"]}.issubset(
            {source["source_ref"] for source in packet["sources"]}))


    def test_exact_relationship_grant_preserves_other_kinds_for_same_target(self):
        previous = {"work_id": "W101", "reason": "A retained reason."}
        relations = [{"source": "W101", "target": "W102", "kind": kind,
                      "claim": {"text": kind, "evidence": []}, "artifact_ref": "artifact:kb/relationships/"+kind+"@1"}
                     for kind in ("extends", "related")]
        feedback = {"entry_fields": [], "relationship_targets": ["W102"],
                    "relationship_refs": [relations[0]["artifact_ref"]]}
        repaired = apply_scoped_map_repair("W101", previous, relations, feedback,
                                          {"entry_updates": {}, "relationships": []}, reject_ungranted_changes=True)
        self.assertEqual(repaired["relationships"], [{key:value for key,value in relations[1].items() if key != "artifact_ref"}])
        self.assertEqual(repaired["entries"], [previous])
        with self.assertRaisesRegex(ValidationError, "relationship pins"):
            apply_scoped_map_repair("W101", previous, relations,
                {**feedback, "relationship_refs": ["artifact:kb/relationships/extends@999"]},
                {"entry_updates": {}, "relationships": []}, reject_ungranted_changes=True)

    def test_disputed_diagnosis_allows_exact_retention_without_expanding_grants(self):
        previous = {"work_id": "W101", "reason": "A supported existing assertion."}
        feedback = {"entry_fields": ["reason"], "relationship_targets": [], "relationship_refs": []}
        normalized = normalize_map_worker_response(
            {"entry_updates": {"W101": {}}, "relationships": []}, work_id="W101",
            all_work_ids={"W101"}, sources=[], windows={}, previous=previous, review_feedback=feedback)
        self.assertEqual(normalized["entry_updates"], {})
        retained = apply_scoped_map_repair("W101", previous, [], feedback,
            normalized, reject_ungranted_changes=True)
        self.assertEqual(retained, {"entries": [previous], "relationships": []})
        partial = normalize_map_worker_response(
            {"entry_updates": {"W101": {"reason": "A narrower rationale."}}, "relationships": []},
            work_id="W101", all_work_ids={"W101"}, sources=[], windows={}, previous=previous,
            review_feedback={**feedback, "entry_fields": ["inclusion", "reason", "finding"]})
        self.assertEqual(partial["entry_updates"], {"reason": "A narrower rationale."})
        for malformed in ({}, {"entry_updates": None, "relationships": []},
                          {"entry_updates": {"W101": None}, "relationships": []},
                          {"entry_updates": {}, "relationships": None}):
            with self.subTest(malformed=malformed), self.assertRaises(ValidationError):
                normalize_map_worker_response(malformed, work_id="W101", all_work_ids={"W101"},
                    sources=[], windows={}, previous=previous, review_feedback=feedback)
        relation = {"source": "W101", "target": "W102", "kind": "related",
                    "claim": {"text": "A supported common mechanism.", "evidence": []},
                    "artifact_ref": "artifact:kb/relationships/one@1"}
        feedback = {"entry_fields": [], "relationship_targets": ["W102"],
                    "relationship_refs": [relation["artifact_ref"]]}
        projection = {key: value for key, value in relation.items() if key != "artifact_ref"}
        retained = apply_scoped_map_repair("W101", previous, [relation], feedback,
            {"entry_updates": {}, "relationships": [projection]}, reject_ungranted_changes=True)
        self.assertEqual(retained, {"entries": [previous], "relationships": [projection]})
        with self.assertRaisesRegex(ValidationError, "granted"):
            apply_scoped_map_repair("W101", previous, [],
                {"entry_fields": [], "relationship_targets": []},
                {"entry_updates": {}, "relationships": []}, reject_ungranted_changes=True)

    def test_mapper_job_empty_repair_preserves_exact_current_entry(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        before = deepcopy(runner._body(runner.analysis_records["W101"]))
        feedback = {"review_ref": runner.analysis_records["W101"]["artifact_ref"],
                    "entry_fields": ["inclusion", "reason", "problem", "finding"],
                    "relationship_targets": [], "relationship_refs": [], "checks": []}
        job = runner._map_job("W101", runner.analyzed_basis["W101"], review_feedback=feedback)
        value = job["normalizer"]({"entry_updates": {"W101": {}}, "relationships": []})
        self.assertEqual(value["entry_updates"], {})
        job["validator"](value)
        job["on_valid"](value, feedback["review_ref"])
        self.assertEqual(runner._body(runner.analysis_records["W101"]), before)
        self.assertIn("disputed diagnosis", job["assignment"]["instructions"])

    def test_aggregate_failure_routes_narrow_repair_and_preserves_siblings(self):
        runner = self.runtime(survey_config(self.endpoint, "aggregate-scoped-repair"))
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map(); runner._review_work_claims()
        old_scope = {"protocol": "literature-survey-repair-1", "question": runner.score["question"],
                     "sources": sorted(runner.source_docs),
                     "analysis_basis": {wid: sorted(runner._analysis_basis(wid)) for wid in sorted(runner.work_records)},
                     "review_obligations": runner.review_obligations,
                     "critique_context_protocol": "literature-critique-transition-1"}
        old_digest = hashlib.sha256(canonical_bytes(old_scope)).hexdigest()
        runner._record("command/survey-review-repairs/" + old_digest, "note",
                       {"scope": old_scope, "rounds": runner.config["limits"]["max_rounds"]}, "command.controller")
        before = {wid: deepcopy(record) for wid, record in runner.analysis_records.items()}
        requests = len(SurveyHTTPFixture.requests)
        runner._accept_survey()
        self.assertIsNotNone(runner.survey_ref)
        self.assertEqual(len(SurveyHTTPFixture.requests), requests)
        self.assertEqual(runner.analysis_records["W102"]["artifact_ref"], before["W102"]["artifact_ref"])
        prior = runner._body(before["W101"]); current = runner._body(runner.analysis_records["W101"])
        self.assertEqual({key:value for key,value in prior.items() if key != "reason"},
                         {key:value for key,value in current.items() if key != "reason"})
        self.assertEqual(current["reason"], "The captured study examines recall timing.")
        prompts = [prompt for _, prompt in self.model_contexts(runner.control, runner.store)]
        repair = next(prompt for prompt in prompts if prompt.get("phase") == "map" and prompt.get("semantic_feedback"))
        self.assertEqual(repair["editable_entry_fields"], ["reason"])
        aggregate = [prompt for prompt in prompts if prompt.get("phase") == "survey_review"]
        self.assertEqual(len(aggregate), 2)
        self.assertIn("prior_review_receipts", aggregate[-1])
        self.assertNotIn("prior_review_response", aggregate[-1])
        self.assertEqual(set(aggregate[-1]["prior_review_receipts"]), {"review_ref", "plan_ref"})
        ledgers = [runner._body(record) for record in runner._heads("command/survey-review-repairs/")]
        self.assertTrue(any(record["scope"]["critique_context_protocol"] == "literature-critique-transition-3"
                            and record["rounds"] == 1 for record in ledgers))

    def test_aggregate_inclusion_quote_routes_only_decision_repair_and_replays_admission(self):
        self.check_aggregate_inclusion_binding("aggregate-inclusion-quote")

    def test_aggregate_assertion_selection_routes_only_decision_repair_and_replays_admission(self):
        self.check_aggregate_inclusion_binding("aggregate-inclusion-selection")

    def check_aggregate_inclusion_binding(self, mode):
        runner = self.runtime(survey_config(self.endpoint, mode))
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map(); runner._review_work_claims()
        before = {wid: deepcopy(record) for wid, record in runner.analysis_records.items()}
        checked = runner._model_checked
        def inspect_catalog(name, actor, assignment, validator, **options):
            if name == "survey-repair-plan":
                ref = before["W101"]["artifact_ref"]
                self.assertEqual(assignment["repair_target_catalog"], [
                    {"entry_ref": ref, "entry_fields": ["inclusion"], "relationship_refs": []}])
                with self.assertRaises(ModelContractError) as caught:
                    validator({"repairs": [{"entry_ref": ref, "entry_fields": ["reason"],
                        "relationship_refs": [], "rationale": "Inspect the quoted reason."}]})
                self.assertIn("requested entry_fields=['reason'], allowed entry_fields=['inclusion']", str(caught.exception))
                self.assertIn("quote_field identifies supporting text and grants no additional field authority", str(caught.exception))
            return checked(name, actor, assignment, validator, **options)
        with patch.object(runner, "_model_checked", side_effect=inspect_catalog):
            runner._accept_survey()
        self.assertIsNotNone(runner.survey_ref)
        self.assertEqual(runner.analysis_records["W102"]["artifact_ref"], before["W102"]["artifact_ref"])
        old, current = runner._body(before["W101"]), runner._body(runner.analysis_records["W101"])
        self.assertEqual({key: val for key, val in old.items() if key != "inclusion"},
                         {key: val for key, val in current.items() if key != "inclusion"})
        self.assertEqual(current["inclusion"], "uncertain")
        reviews = [record for record in runner._heads("kb/survey-reviews/")
                   if any(check["outcome"] != "passed" for check in runner._body(record)["checks"])]
        self.assertEqual(len(reviews), 1)
        rejected = runner._body(reviews[0])
        if mode == "aggregate-inclusion-selection":
            _, _, _, reply = runner.gate._model_review_execution(rejected["execution_ref"], "methods.survey-reviewer")
            self.assertEqual(set(reply["findings"][0]), {"check_id", "assertion_id", "rationale"})
        self.assertEqual(rejected["findings"][0]["field"], "inclusion")
        self.assertEqual(rejected["findings"][0]["quote_field"], "reason")
        with self.assertRaises(ValidationError):
            runner.gate._review(runner.store.get(rejected["survey_ref"]), reviews[0]["artifact_ref"])
        with patch.object(runner.gate, "_passed_checks"):
            runner.gate._review(runner.store.get(rejected["survey_ref"]), reviews[0]["artifact_ref"])
        prompts = [prompt for _, prompt in self.model_contexts(runner.control, runner.store)]
        repair = next(prompt for prompt in prompts if prompt.get("phase") == "map" and prompt.get("semantic_feedback"))
        self.assertEqual(repair["editable_entry_fields"], ["inclusion"])

    def test_survey_repair_allowance_binds_assertions_and_retains_exhausted_unchanged_scopes(self):
        runner = self.runtime(survey_config(self.endpoint, "aggregate-inclusion-quote"))
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map(); runner._review_work_claims()
        rejected = runner._accept_survey_once()
        scope = runner._survey_repair_scope()
        logical = "command/survey-review-repairs/" + hashlib.sha256(canonical_bytes(scope)).hexdigest()
        runner._record(logical, "note", {"scope": scope, "rounds": runner.config["limits"]["max_rounds"]}, "command.controller")
        with self.assertRaisesRegex(ModelWorkBlocked, "after scoped repairs"):
            runner._repair_survey_review(rejected)
        before = runner.analysis_records["W101"]
        body = runner._body(before)
        updated = {**body, "inclusion": "uncertain"}
        runner.analysis_records["W101"] = runner._publish(before["artifact_id"], "note", updated, "research.literature-mapper")
        changed_scope = runner._survey_repair_scope()
        self.assertNotEqual(changed_scope["assertions_sha256"], scope["assertions_sha256"])
        self.assertEqual(changed_scope["sources"], scope["sources"])
        self.assertEqual(changed_scope["analysis_basis"], scope["analysis_basis"])
        changed_logical = "command/survey-review-repairs/" + hashlib.sha256(canonical_bytes(changed_scope)).hexdigest()
        self.assertIsNone(runner.store.head(changed_logical))
        runner.analysis_records = dict(reversed(list(runner.analysis_records.items())))
        self.assertEqual(runner._survey_repair_scope(), changed_scope)
        runner.analysis_records["W101"] = runner._publish(before["artifact_id"], "note", updated, "research.literature-mapper")
        self.assertEqual(runner._survey_repair_scope(), changed_scope)
        runner.analysis_records["W101"] = before
        self.assertEqual(runner._survey_repair_scope(), scope)
        with self.assertRaisesRegex(ModelWorkBlocked, "after scoped repairs"):
            runner._repair_survey_review(rejected)

    def test_aggregate_repair_rejects_ungranted_fields(self):
        runner = self.runtime(survey_config(self.endpoint, "aggregate-ungranted-repair"))
        result = runner.run()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["survey_current"])
        self.assertIn("scoped fields", result["error"])

    def test_durable_independent_critique_reopens_only_the_pinned_review(self):
        config = survey_config(self.endpoint, "review-obligation-adversary")
        config["limits"]["max_rounds"] = 2
        runner = self.runtime(config)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._accept_survey()
        unaffected = runner.work_reviews["W102"]["artifact_ref"]
        old_entry = runner.analysis_records["W101"]["artifact_ref"]
        obligation = {k:v for k,v in self.review_obligation(runner, "W101").items()
                      if k not in {"receipt_ref", "receipt_body_sha256"}}
        receipt = runner._record("command/independent-literature-critiques/audit", "decision_note",
            {"schema_version": "independent-literature-critique-1", "question": runner.score["question"],
             "obligations": [obligation]}, "command.operator", subjects=[old_entry])
        with self.assertRaisesRegex(ValidationError, "not adjudicated"):
            runner.gate.require_current(runner.survey_ref)
        requests = len(SurveyHTTPFixture.requests)
        runner.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["gap_assessment"]}}
        resumed = self.runtime(config, resume_policy=policy)
        self.assertEqual(resumed.review_obligations[0]["receipt_ref"], receipt["artifact_ref"])
        self.assertEqual(resumed.review_obligations[0]["receipt_body_sha256"], receipt["body_hash"])
        self.assertNotIn("W101", resumed.reviewed_basis)
        self.assertIsNone(resumed.survey_ref)
        self.assertIsNone(resumed.assessment_ref)
        resumed._initialize(); resumed._accept_survey()
        self.assertEqual(len(SurveyHTTPFixture.requests), requests)
        self.assertEqual(resumed.work_reviews["W102"]["artifact_ref"], unaffected)
        self.assertNotEqual(resumed.analysis_records["W101"]["artifact_ref"], old_entry)
        self.assertEqual(resumed._body(resumed.analysis_records["W101"])["reason"], "The captured study examines recall timing.")
        focused = [p for _,p in self.model_contexts(resumed.control, resumed.store)
                   if p.get("phase") == "work_review" and p.get("review_obligations")]
        self.assertEqual(len(focused), 2)
        self.assertTrue(all(p["entry"]["work_id"] == "W101" for p in focused))
        self.assertTrue(resumed._review_protocol_matches(resumed._body(resumed.work_reviews["W101"])))

        accepted = resumed.survey_ref
        reviewed = resumed.work_reviews["W101"]["artifact_ref"]
        before_calls = resumed.budget.get_window("run-window")["cumulative_usage"]["model_calls"]
        resumed.control.close()
        replay = self.runtime(config, resume_policy={**policy, "source_changes": {"mode": "reject", "reopen_scopes": []}})
        self.assertEqual(replay.survey_ref, accepted)
        self.assertEqual(replay.work_reviews["W101"]["artifact_ref"], reviewed)
        self.assertEqual(replay.budget.get_window("run-window")["cumulative_usage"]["model_calls"], before_calls)

    def test_exhausted_critiqued_abstention_blocks_without_recursive_redispatch(self):
        config = survey_config(self.endpoint, "review-never-resolves")
        config["limits"]["max_rounds"] = 2
        runner = self.runtime(config)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map(); runner._review_work_claims()
        obligation = self.review_obligation(runner, "W101")
        runner.review_obligations = runner._validate_review_obligations([obligation])
        runner.reviewed_basis.pop("W101", None)
        before = runner.model_calls_dispatched
        with self.assertRaisesRegex(ModelWorkBlocked, "remains unresolved"):
            runner._review_work_claims()
        self.assertLess(runner.model_calls_dispatched - before, 10)
        self.assertIsNone(runner.survey_ref)
        self.assertTrue(all(runner._body(runner.analysis_records["W101"])[field]["text"] is None for field in MAP_FIELDS))

    def test_durable_critique_rejects_changed_question_author_and_source(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._accept_survey()
        obligation = {k:v for k,v in self.review_obligation(runner, "W101").items()
                      if k not in {"receipt_ref", "receipt_body_sha256"}}
        body = {"schema_version": "independent-literature-critique-1", "question": runner.score["question"],
                "obligations": [obligation]}
        logical = "command/independent-literature-critiques/audit"
        for bad, author in (({**body, "question": "different question"}, "command.operator"),
                            (body, "research.literature-mapper")):
            runner._record(logical, "decision_note", bad, author)
            with self.assertRaises(StateError):
                runner.gate.independent_review_obligations(runner.score["question"])
        bad = deepcopy(body); bad["obligations"][0]["source_pins"][0]["body_hash"] = "b"*64
        runner._record(logical, "decision_note", bad, "command.operator")
        with self.assertRaises(StateError):
            runner._validate_review_obligations(runner.gate.independent_review_obligations(runner.score["question"]))

    def test_targeted_review_obligation_preserves_unaffected_reviews_and_adjudicates_hypothesis(self):
        config = survey_config(self.endpoint, "review-obligation-adversary")
        config["limits"]["max_rounds"] = 2
        runner = self.runtime(config)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._accept_survey()
        entry = runner.analysis_records["W101"]
        unaffected = runner.work_reviews["W102"]["artifact_ref"]
        obligation = self.review_obligation(runner, "W101")
        request_count = len(SurveyHTTPFixture.requests)
        runner.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["focused_review", "integrated_review", "gap_assessment"]}}
        resumed = self.runtime(config, resume_policy=policy, review_obligations=[obligation])
        self.assertEqual(resumed.work_reviews["W102"]["artifact_ref"], unaffected)
        self.assertNotIn("W101", resumed.reviewed_basis)
        self.assertEqual(resumed._work_review_failure_count("W101"), 0)
        resumed._initialize(); resumed._accept_survey()
        self.assertEqual(len(SurveyHTTPFixture.requests), request_count)
        self.assertEqual(resumed.work_reviews["W102"]["artifact_ref"], unaffected)
        self.assertNotEqual(resumed.analysis_records["W101"]["artifact_ref"], entry["artifact_ref"])
        self.assertEqual(resumed._body(resumed.analysis_records["W101"])["problem"], runner._body(entry)["problem"])
        self.assertEqual(resumed._body(resumed.analysis_records["W101"])["reason"], "The captured study examines recall timing.")
        prompts = [prompt for _, prompt in self.model_contexts(resumed.control, resumed.store)]
        focused = [prompt for prompt in prompts if prompt.get("phase") == "work_review" and prompt.get("review_obligations")]
        self.assertEqual(len(focused), 2)
        self.assertTrue(all(prompt["entry"]["work_id"] == "W101" and prompt["review_obligations"] == [obligation] for prompt in focused))
        self.assertTrue(all(any(source["source_ref"] == obligation["source_pins"][0]["ref"] for source in prompt["sources"]) for prompt in focused))
        self.assertEqual([prompt for prompt in prompts if prompt.get("phase") == "survey_review"][-1]["independent_critique_receipts"], [obligation["receipt_ref"]])
        self.assertEqual(resumed._work_review_failure_count("W101"), 2)
        self.assertEqual(sum(any(check["outcome"] != "passed" for check in resumed._body(resumed.store.get(
            f"artifact:kb/work-reviews/W101@{version}"))["checks"])
            for version in resumed.store.versions("kb/work-reviews/W101")), 1)
        self.assertTrue(resumed._review_protocol_matches(resumed._body(resumed.work_reviews["W101"])))

    def test_review_obligation_is_not_a_forced_verdict_and_rejects_changed_source_pins(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._accept_survey()
        retained = runner.analysis_records["W101"]["artifact_ref"]
        obligation = self.review_obligation(runner, "W101")
        runner.review_obligations = runner._validate_review_obligations([obligation])
        runner._accept_survey()
        self.assertEqual(runner.analysis_records["W101"]["artifact_ref"], retained)
        self.assertTrue(all(check["outcome"] == "passed" for check in runner._body(runner.work_reviews["W101"])["checks"]))
        bad = deepcopy(obligation); bad["source_pins"][0]["body_hash"] = "b" * 64
        with self.assertRaises(StateError):
            runner._validate_review_obligations([bad])
        with self.assertRaises(StateError):
            runner._validate_review_obligations([{**obligation, "source_pins": []}])
        with self.assertRaises(StateError):
            runner._validate_review_obligations([{**obligation, "entry_body_sha256": "c" * 64}])
        original_question = runner.score["question"]
        runner.score["question"] = "A different declared question"
        with self.assertRaises(StateError):
            runner._validate_review_obligations([obligation])
        runner.score["question"] = original_question
        source_ref = obligation["source_pins"][0]["ref"]
        runner.source_docs.pop(source_ref)
        with self.assertRaises(StateError):
            runner._review_obligations_for("W101")

    def test_pinned_comparison_reaches_review_repair_and_invalidates_stale_peer_context(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        obligation = self.review_obligation(runner, "W101")
        peer = runner.analysis_records["W102"]
        obligation["comparison_pins"] = [{"ref": peer["artifact_ref"], "body_hash": peer["body_hash"]}]
        peer_sources = {proof["source_ref"] for field in MAP_FIELDS
                        for proof in runner._body(peer)[field]["evidence"]}
        pinned = {pin["ref"] for pin in obligation["source_pins"]}
        obligation["source_pins"].extend({"ref": ref, "body_hash": runner.store.get(ref)["body_hash"]}
                                         for ref in sorted(peer_sources - pinned))
        runner.review_obligations = runner._validate_review_obligations([obligation])
        runner._review_work_claims()
        self.assertTrue(runner._work_review_current("W101"))
        self.assertNotIn("work-review-W101", {job["name"] for job in runner._required_model_work()})
        prompt = next(prompt for _, prompt in reversed(self.model_contexts(runner.control, runner.store))
                      if prompt.get("phase") == "work_review" and prompt["entry"]["work_id"] == "W101")
        comparison = prompt["critique_contexts"][0]["comparison_entries"][0]
        self.assertEqual(comparison["original_entry"]["ref"], peer["artifact_ref"])
        self.assertEqual(comparison["current_entry"]["body"], runner._body(peer))
        self.assertTrue(peer_sources <= {source["source_ref"] for source in prompt["sources"]})
        self.assertIn(peer["artifact_ref"], runner._work_review_basis("W101"))
        entries = {row["artifact_ref"]: (row, runner._body(row)) for row in runner.analysis_records.values()}
        visible = {source["source_ref"]: source for source in prompt["sources"]}
        runner.gate._work_review_comparisons(prompt, [obligation], entries, visible, require_spans=True)
        bad = deepcopy(prompt)
        bad["critique_contexts"][0]["comparison_entries"][0]["current_entry"]["body"]["reason"] = "Altered criterion."
        with self.assertRaisesRegex(ValidationError, "exact current peer"):
            runner.gate._work_review_comparisons(bad, [obligation], entries, visible, require_spans=True)
        with self.assertRaises(ValidationError):
            runner.gate._work_review_comparisons(prompt, [obligation], entries, {}, require_spans=True)
        feedback = {"entry_fields": ["inclusion", "reason"], "relationship_targets": [],
                    "relationship_refs": [], "checks": [], "rationale": "Reconcile the screening criterion."}
        repair = runner._map_job("W101", runner.analyzed_basis["W101"], review_feedback=feedback)
        self.assertEqual(repair["assignment"]["critique_contexts"], prompt["critique_contexts"])
        self.assertTrue(peer_sources <= {source["source_ref"] for source in repair["assignment"]["sources"]})
        changed = runner._body(peer); changed["reason"] = "The same bounded method with a revised criterion."
        runner.analysis_records["W102"] = runner._record("kb/work-analyses/W102", "note", changed,
                                                        "research.literature-mapper")
        self.assertFalse(runner._work_review_current("W101"))
        self.assertIn("work-review-W101", {job["name"] for job in runner._required_model_work()})
        with self.assertRaisesRegex(ValidationError, "exact current peer"):
            entries = {row["artifact_ref"]: (row, runner._body(row)) for row in runner.analysis_records.values()}
            runner.gate._work_review_comparisons(prompt, [obligation], entries, visible, require_spans=True)

    def test_comparison_pins_preserve_owner_question_and_source_integrity(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        obligation = self.review_obligation(runner, "W101")
        peer = runner.analysis_records["W102"]
        pin = {"ref": peer["artifact_ref"], "body_hash": peer["body_hash"]}
        with self.assertRaisesRegex(StateError, "omitted cited source"):
            runner._validate_review_obligations([{**obligation, "comparison_pins": [pin]}])
        for pins in ([{**pin, "body_hash": "0" * 64}],
                     [{"ref": obligation["entry_ref"], "body_hash": obligation["entry_body_sha256"]}],
                     [pin, pin], "invalid"):
            with self.subTest(pins=pins), self.assertRaises(StateError):
                runner._validate_review_obligations([{**obligation, "comparison_pins": pins}])
        read_artifact = runner.gate._artifact
        def different_question(ref, **kwargs):
            manifest, raw = read_artifact(ref, **kwargs)
            if ref == peer["artifact_ref"]:
                manifest = {**manifest, "score_ref": "artifact:command/scores/different@1"}
            return manifest, raw
        with patch.object(runner.gate, "_artifact", side_effect=different_question), self.assertRaises(StateError):
            runner._validate_review_obligations([{**obligation, "comparison_pins": [pin]}])

    def test_abstention_integrity_cannot_accept_scientific_prose(self):
        from scisaurus.core.schema import canonical_bytes, sha256_hex
        from scisaurus.core.surveys import ABSTENTION_REASONS, is_explicit_abstention
        entry = {"work_id": "W101", "inclusion": "uncertain", "reason": ABSTENTION_REASONS["deep_analysis_budget"],
                 **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        record = {"work_id": "W101", "scope": "deep_analysis_budget", "entry_sha256": sha256_hex(canonical_bytes(entry))}
        self.assertTrue(is_explicit_abstention(entry, record))
        self.assertFalse(is_explicit_abstention(entry, {**record, "scope": "source_unavailable"}))
        for replacement in ({"finding": {"text": "This proves superiority", "evidence": []}},
                            {"reason": "This proves superiority"}, {"inclusion": "included"}):
            changed = {**entry, **replacement}
            record["entry_sha256"] = sha256_hex(canonical_bytes(changed))
            self.assertFalse(is_explicit_abstention(changed, record))

    def test_resume_restores_work_committed_after_aggregate_checkpoint(self):
        runner = self.runtime()
        runner._initialize()
        runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        old = runner.analysis_records["W101"]
        body = runner._body(old)
        body["reason"] = "The captured abstract examines recall timing."
        latest = runner._record("kb/work-analyses/W101", "note", body, "research.literature-mapper")
        self.assertNotEqual(old["artifact_ref"], latest["artifact_ref"])
        runner.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["focused_review"]}}
        resumed = self.runtime(resume_policy=policy)
        self.addCleanup(resumed.control.close)
        self.assertEqual(resumed.analysis_records["W101"]["artifact_ref"], latest["artifact_ref"])

    def test_resume_does_not_credit_old_analysis_and_review_for_new_source(self):
        runner = self.runtime()
        runner._initialize()
        runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        runner._review_work_claims()
        self.assertIn("W101", runner.analyzed_basis)
        self.assertIn("W101", runner.reviewed_basis)
        source = {"work_id": "W101", "text": "Newly captured full text changes the result boundary.",
                  "representation": "full_text"}
        record = runner._record("kb/full-text/W101", "source_capture", source, "methods.source-verifier")
        runner.source_docs[record["artifact_ref"]] = source
        runner._update_register()
        runner.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["operations"]}}
        resumed = self.runtime(resume_policy=policy)
        self.addCleanup(resumed.control.close)
        self.assertIn("W101", resumed.analysis_records)
        self.assertNotIn("W101", resumed.analyzed_basis)
        self.assertNotIn("W101", resumed.reviewed_basis)

    def test_resume_invalidates_claims_even_with_unchanged_unverified_source_refs(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map()
        abstract = next(body for body in runner.source_docs.values() if body["work_id"] == "W101")
        unverified = {**abstract, "representation": "unverified_text", "identity_verified": False,
                      "identity_checks": {"title_match": False, "section_markers": []}}
        capture = runner._record("kb/full-text/W101", "source_capture", unverified, "methods.source-verifier")
        runner.source_docs[capture["artifact_ref"]] = unverified
        runner._update_register()
        basis = [runner.work_records["W101"]["artifact_ref"], *runner.source_docs]
        body = runner._body(runner.analysis_records["W101"])
        for field in MAP_FIELDS:
            for proof in body[field]["evidence"]:
                proof["source_ref"] = capture["artifact_ref"]
        record = runner._record("kb/work-analyses/W101", "note", body, "research.literature-mapper", subjects=basis)
        runner.analysis_records["W101"] = record
        runner.analyzed_basis["W101"] = basis
        runner.map_record = runner._record("kb/literature-map", "note", {
            "entry_refs": [record["artifact_ref"]], "relationship_refs": []}, "research.literature-mapper")
        runner._review_work_claims()
        runner.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["operations"]}}
        resumed = self.runtime(resume_policy=policy)
        self.addCleanup(resumed.control.close)
        self.assertIn(capture["artifact_ref"], resumed.source_docs)
        self.assertEqual(resumed.analysis_records["W101"]["artifact_ref"], record["artifact_ref"])
        self.assertNotIn("W101", resumed.analyzed_basis)
        self.assertNotIn("W101", resumed.reviewed_basis)
        self.assertNotIn("W101", resumed.work_reviews)

    def test_review_only_resume_does_not_repeat_initial_acquisition(self):
        config = survey_config(self.endpoint)
        with patch.object(SurveyRunner, "_review_work_claims", side_effect=KeyboardInterrupt("checkpoint before review")):
            first = self.runtime(config).run()
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["failure"], {"kind": "process_interrupted"})
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["focused_review"]}}
        resumed = self.runtime(config, resume_policy=policy)
        with patch.object(resumed, "_search") as search, patch.object(resumed, "_explore", wraps=resumed._explore) as explore, \
             patch.object(resumed, "_full_texts") as fetch, \
             patch.object(resumed, "_countersearch", side_effect=KeyboardInterrupt("after accepted survey")) as counter:
            result = resumed.run()
        self.assertIsNotNone(result["survey_ref"], result)
        search.assert_not_called(); explore.assert_called_once()
        counter.assert_called_once()

    def test_scoped_resume_restores_tree_selection_without_reopening_catalog(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._tree_load()
        runner.exploration_tree["nodes"].append({"id": "selection-fixture", "kind": "acquisition",
            "state": "read", "selected_work_ids": ["W101"], "follow_up_ref": runner.follow_up_ref})
        runner._tree_save(); runner._map()
        selection = runner._analysis_selection()
        self.assertEqual(selection, {"W101"})
        requests = len(SurveyHTTPFixture.requests)
        runner.control.close()
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["gap_assessment"]}}
        resumed = self.runtime(resume_policy=policy)
        self.addCleanup(resumed.control.close)
        self.assertIsNotNone(resumed.exploration_tree)
        self.assertEqual(resumed._analysis_selection(), selection)
        self.assertEqual(len(SurveyHTTPFixture.requests), requests)
        resumed.review_obligations = [{"work_id": "W102"}]
        self.assertEqual(resumed._analysis_selection(), {"W101", "W102"})

    def test_gap_only_resume_with_work_orders_does_not_repeat_completed_acquisition(self):
        config = survey_config(self.endpoint)
        orders = [self.follow_up_order()]
        with patch.object(SurveyRunner, "_assess", side_effect=KeyboardInterrupt("before gap assessment")):
            first = self.runtime(config, work_orders=orders).run()
        self.assertTrue(first["survey_current"], first.get("error"))
        before_requests = len(SurveyHTTPFixture.requests)
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["gap_assessment"]}}
        resumed = self.runtime(config, work_orders=orders, resume_policy=policy)
        self.assertTrue(resumed.counter_queries_complete)
        with patch.object(resumed, "_prepare_follow_up", side_effect=AssertionError("acquisition repeated")), \
             patch.object(resumed, "_accept_survey", side_effect=AssertionError("accepted survey repeated")), \
             patch.object(resumed, "_countersearch", side_effect=AssertionError("countersearch repeated")), \
             patch.object(resumed, "_setup", side_effect=AssertionError("operational probes repeated")):
            result = resumed.run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["survey_ref"], first["survey_ref"])
        self.assertTrue(result["assessment_current"])
        self.assertEqual(len(SurveyHTTPFixture.requests), before_requests)

    def test_follow_up_resume_after_counter_queries_sets_up_unfinished_acquisition(self):
        config = survey_config(self.endpoint)
        orders = [self.follow_up_order()]
        original = SurveyRunner._full_texts
        def interrupt_challenge(runner):
            if runner._countersearch_active:
                raise KeyboardInterrupt("after challenge query receipt")
            return original(runner)
        with patch.object(SurveyRunner, "_full_texts", interrupt_challenge):
            first = self.runtime(config, work_orders=orders).run()
        self.assertEqual(first["status"], "paused", first.get("error"))
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["gap_assessment"]}}
        resumed = self.runtime(config, work_orders=orders, resume_policy=policy)
        self.assertTrue(resumed.counter_queries_complete)
        self.assertFalse(resumed.countersearch_complete)
        with patch.object(resumed, "_prepare_follow_up", side_effect=AssertionError("discovery repeated")), \
             patch.object(resumed, "_setup", wraps=resumed._setup) as setup, \
             patch.object(resumed, "_full_texts", wraps=resumed._full_texts) as capture:
            result = resumed.run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        setup.assert_called_once(); capture.assert_called_once()
        self.assertTrue(result["assessment_current"])
        self.assertEqual(sum(request["query"].get("search") == ["prior solution"]
                             for request in SurveyHTTPFixture.requests), 1)

    def test_mapper_and_reviewer_share_the_evidence_and_screening_contract(self):
        from scisaurus.runtime.survey import source_fidelity_review_contract
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._map(); runner._review_work_claims()
        prompts = [prompt for _, prompt in self.model_contexts(runner.control, runner.store)]
        mapper = next(p for p in prompts if p["phase"] == "map")
        reviewer = next(p for p in prompts if p["phase"] == "work_review")
        self.assertEqual(mapper["source_fidelity_contract"], source_fidelity_review_contract())
        self.assertNotIn("at most 240 characters", mapper["instructions"])
        self.assertIn("conditions, uncertainty, and assumptions", mapper["instructions"])
        numerical = mapper["source_fidelity_contract"]["numerical_scope"]
        self.assertIn("temperature for a reaction-rate coefficient", numerical)
        self.assertIn("plus/minus magnitude alone", numerical)
        self.assertIn("reviewer rationale cannot supply conditions", numerical)
        for key, value in mapper["source_fidelity_contract"].items():
            self.assertEqual(reviewer["review_contract"][key], value)

    def test_aggregate_review_preserves_configured_output_capacity(self):
        config = survey_config(self.endpoint)
        runner = self.runtime(config)
        result = runner.run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        control, store = self.open_store()
        contexts = [context for context, prompt in self.model_contexts(control, store)
                    if prompt.get("phase") == "survey_review"]
        self.assertTrue(contexts)
        self.assertTrue(all(json.loads(store.read_body(context["body_hash"]))["client"]["max_output_tokens"]
                            == config["model"]["max_output_tokens"]
                            for context in contexts))

    def test_aggregate_review_initial_and_repair_share_exact_response_contract(self):
        from scisaurus.runtime.survey_records import survey_review_response_contract
        runner = self.runtime()
        result = runner.run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        control, store = self.open_store()
        prompt = next(prompt for _, prompt in self.model_contexts(control, store)
                      if prompt.get("phase") == "survey_review")
        contract = survey_review_response_contract(prompt["map"])
        self.assertEqual(prompt["response_contract"], contract)
        repaired = runner._repair_assignment({"assignment": prompt, "actor": "methods.survey-reviewer"},
                                            {"error": "unexpected findings inside a check", "finish_reason": "stop"})
        self.assertEqual(repaired["response_contract"], contract)
        self.assertEqual({row["target_ref"] for row in contract["assertion_catalog"]},
                         set(prompt["map"]["entry_refs"].values()) | set(prompt["map"]["relationship_refs"]))
        self.assertNotIn("Use quote_field=reason", prompt["instructions"])
        self.assertTrue(all(row["additional_fields"] is False for row in contract["checks"]))

    def test_aggregate_findings_use_refs_from_actual_relationship_projection(self):
        from scisaurus.runtime.survey_records import validate_survey_review
        runner = self.runtime(survey_config(self.endpoint, "map-links"))
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        packet = runner._survey_review_packet()
        self.assertTrue(packet["map"]["relationships"])
        definitions = packet["coverage"]["count_definitions"]
        self.assertIn("not a deduplicated study count", definitions["unique_works"])
        self.assertIn("not a count of independent studies", definitions["entry_inclusion_counts"])
        relation = packet["map"]["relationships"][0]
        self.assertIn(relation["artifact_ref"], packet["map"]["relationship_refs"])
        value = {"checks": check_rows(SURVEY_CHECKS), "rationale": "Audit the current relationship."}
        validate_survey_review(value, current_map=packet["map"])
        value["checks"][1]["outcome"] = "failed"
        value["findings"] = [{"check_id": "source-fidelity", "target_ref": relation["artifact_ref"],
                              "field": "claim", "quote": relation["claim"]["text"],
                              "rationale": "The shared pathway requires narrower evidence."}]
        validate_survey_review(value, current_map=packet["map"])

    def test_gap_only_resume_retains_accepted_survey_and_countersearch(self):
        config = survey_config(self.endpoint)
        with patch.object(SurveyRunner, "_assess", side_effect=KeyboardInterrupt("before gap assessment")):
            first = self.runtime(config).run()
        self.assertTrue(first["survey_current"], first.get("error"))
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["gap_assessment"]}}
        resumed = self.runtime(config, resume_policy=policy)
        with patch.object(resumed, "_accept_survey", side_effect=AssertionError("accepted survey repeated")), \
             patch.object(resumed, "_countersearch", side_effect=AssertionError("countersearch repeated")), \
             patch.object(resumed, "_setup", side_effect=AssertionError("operational probes repeated")):
            result = resumed.run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["survey_ref"], first["survey_ref"])
        self.assertTrue(result["assessment_current"])
        control, store = self.open_store()
        assessment = next(
            prompt for _, prompt in self.model_contexts(control, store)
            if prompt["phase"] == "gap_assessment"
        )
        self.assertEqual(assessment["resume_boundary"], "gap-assessment-resume-1")
        self.assertIn("same work_id", assessment["instructions"])
        self.assertIn("claim_index", assessment)
        self.assertNotIn("map", assessment)
        self.assertTrue(all("text" in source and "window" in source
                            for source in assessment["sources"]))

    def test_changed_review_policy_reopens_acceptance_without_discarding_sources(self):
        config = survey_config(self.endpoint)
        first = self.runtime(config).run()
        self.assertEqual(first["status"], "completed", first.get("error"))
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["gap_assessment"]}}
        contract = {**source_fidelity_review_contract(), "analyst_mapping_scope": "Require newly specified attribution checks."}
        with patch("scisaurus.runtime.survey.source_fidelity_review_contract", return_value=contract):
            resumed = self.runtime(config, resume_policy=policy)
            self.addCleanup(resumed.control.close)
            self.assertIsNone(resumed.survey_ref)
            self.assertIsNone(resumed.assessment_ref)
            self.assertTrue(resumed.source_docs)
            self.assertFalse(resumed.reviewed_basis)
            self.assertEqual(resumed.store.accepted("kb/surveys/current")["artifact_ref"], first["survey_ref"])

    def test_acceptance_retry_reuses_exact_survey_and_completed_review(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W102")
        with patch.object(runner.gate, "accept", side_effect=ValidationError("acceptance interrupted")):
            with self.assertRaisesRegex(ValidationError, "acceptance interrupted"):
                runner._accept_survey()
        survey_ref = runner.store.head("kb/surveys/current")["artifact_ref"]
        runner.work_reviews = dict(reversed(list(runner.work_reviews.items())))
        with patch.object(runner, "_call_batch", side_effect=AssertionError("unchanged review redispatched")):
            runner._accept_survey()
        self.assertEqual(runner.survey_ref, survey_ref)
        self.assertEqual(runner.store.accepted("kb/surveys/current")["artifact_ref"], survey_ref)
        review_prompt = next(
            prompt for _, prompt in self.model_contexts(runner.control, runner.store)
            if prompt.get("phase") == "survey_review"
        )
        self.assertIn("Do not require any captured source to answer",
                      review_prompt["review_contract"]["source_fidelity_scope"])
        self.assertIn("Use insufficient_evidence for source-fidelity only",
                      review_prompt["instructions"])

    def test_incomplete_aggregate_evidence_is_not_replaced_by_focused_positive_verdicts(self):
        result = self.runtime(
            survey_config(self.endpoint, "survey-coverage-insufficient")).run()
        self.assertEqual(result["status"], "blocked", result.get("error"))
        self.assertFalse(result["survey_current"])
        control, store = self.open_store()
        accepted = store.accepted("kb/surveys/current")
        self.assertIsNone(accepted)
        review_rows = control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE 'kb/survey-reviews/%' "
            "ORDER BY created_at DESC LIMIT 1").fetchone()
        review = json.loads(store.read_body(store.get(review_rows[0])["body_hash"]))
        self.assertNotIn("verification_kind", review)
        self.assertEqual(next(row["outcome"] for row in review["checks"] if row["check_id"] == "source-fidelity"), "insufficient_evidence")
        review_prompt_count = sum(
            prompt.get("phase") == "survey_review"
            for _, prompt in self.model_contexts(control, store))
        deterministic_count = control._conn.execute(
            "SELECT COUNT(*) FROM artifacts "
            "WHERE logical_id LIKE 'command/survey-review-deterministic/%'").fetchone()[0]
        self.assertEqual(review_prompt_count, 2)
        self.assertEqual(sum(prompt.get("phase") == "survey_repair_plan"
                             for _, prompt in self.model_contexts(control, store)), 1)
        self.assertEqual(sum(prompt.get("phase") == "gap_assessment"
                             for _, prompt in self.model_contexts(control, store)), 0)
        self.assertEqual(deterministic_count, 0)

    def test_independent_aggregate_negative_verdicts_remain_blocking(self):
        for failed_check in ("coverage-accounting", "map-support"):
            with self.subTest(failed_check=failed_check):
                run_root = self.root / failed_check
                runner = SurveyRunner(
                    run_root,
                    survey_config(self.endpoint, f"survey-coverage-insufficient-{failed_check}"),
                )
                runner.worker_target = simulated_survey_worker
                result = runner.run()
                self.assertEqual(result["status"], "blocked")
                self.assertFalse(result["survey_current"])
                control = ControlStore(run_root)
                deterministic_count = control._conn.execute(
                    "SELECT COUNT(*) FROM artifacts "
                    "WHERE logical_id LIKE 'command/survey-review-deterministic/%'"
                ).fetchone()[0]
                control.close()
                self.assertEqual(deterministic_count, 0)

    def test_aggregate_format_failure_cannot_create_semantic_acceptance(self):
        runner = self.runtime(survey_config(self.endpoint, "survey-review-malformed"))
        result = runner.run()
        self.assertEqual(result["status"], "blocked", result.get("error"))
        self.assertFalse(result["survey_current"])
        control, store = self.open_store()
        self.assertIsNone(store.accepted("kb/surveys/current"))
        self.assertEqual(control._conn.execute("SELECT COUNT(*) FROM artifacts WHERE logical_id LIKE 'command/survey-review-deterministic/%'").fetchone()[0], 0)

    def test_focused_relevance_contract_distinguishes_unrelated_summary_from_partial_support(self):
        runner = self.runtime(survey_config(self.endpoint, "screening-relevance-adversary"))
        runner._initialize()
        question = "Does work extraction scale linearly with relational coherence?"
        for index, (text, reason, expected) in enumerate((
                ("This review summarizes LED device architectures.", "The source's LED topic matches its own summary.", "failed"),
                ("Relational coherence contributes to thermodynamic work.", "The captured source connects coherence and thermodynamic work; linear scaling remains unestablished.", "passed"))):
            entry = {"work_id": f"W{index}", "inclusion": "included", "reason": reason,
                     **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
            value, _ = runner._model_checked(f"relevance-{index}", "methods.work-reviewer", {
                "phase": "work_review", "question": question, "entry": entry,
                "review_contract": source_fidelity_review_contract(), "required_checks": list(work_review_checks([])),
                "sources": [{"source_ref": f"fixture-source-{index}", "work_id": entry["work_id"], "representation": "abstract", "text": text}]},
                lambda value, entry=entry: validate_work_review(value, [], entry=entry), task_kind="verification")
            outcomes = {row["check_id"]: row["outcome"] for row in value["checks"]}
            self.assertEqual(outcomes["inclusion"], expected)
            self.assertEqual(outcomes["reason"], expected)
            self.assertEqual(outcomes["problem"], "passed")
        self.assertEqual(source_fidelity_review_contract()["protocol"], "literature-source-fidelity-5")

    def test_focused_relationship_qualifier_is_a_separate_entailed_clause(self):
        runner = self.runtime(survey_config(self.endpoint, "relationship-qualifier-adversary"))
        runner._initialize()
        ref = "artifact:kb/relationships/fixture@1"
        entry = {"work_id": "W1", "inclusion": "included", "reason": "The source connects coherence and work.",
                 **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        value, _ = runner._model_checked("qualifier-review", "methods.work-reviewer", {
            "phase": "work_review", "question": "How does coherence contribute to work?", "entry": entry,
            "review_contract": source_fidelity_review_contract(), "required_checks": list(work_review_checks([ref])),
            "relationships": [{"kind": "related", "claim": {"text": "Both works investigate the fundamental problem of converting coherence to work."}}],
            "sources": [{"source_ref": "fixture-source", "work_id": "W1", "representation": "abstract", "text": "We assess the problem of converting coherence to work."}]},
            lambda value: validate_work_review(value, [ref], entry=entry), task_kind="verification")
        self.assertEqual(next(row["outcome"] for row in value["checks"] if row["check_id"] == "relationship:" + ref), "failed")

    def test_integrated_source_packet_preserves_exact_disjoint_paragraph_spans_and_hashes(self):
        runner = self.runtime()
        runner._initialize()
        first, last = "First measurement: λ contributes to work.", "Last measurement: coupling changes the response."
        text = first + "\n\n" + "Uncited background. " * 8000 + "\n\n" + last
        source = {"work_id": "W1", "representation": "full_text", "text": text, "identity_verified": True,
                  "identity_checks": {"title_match": True, "section_markers": ["Methods"]}}
        record = runner._record("kb/full-text/W1", "source_capture", source, "methods.source-verifier")
        ref = record["artifact_ref"]
        runner.source_docs[ref] = source
        proofs = [{"work_id": "W1", "source_ref": ref, "quote": quote,
                   "start": text.index(quote), "end": text.index(quote) + len(quote),
                   "quote_sha256": hashlib.sha256(quote.encode()).hexdigest()} for quote in (first, last)]
        entry = {"work_id": "W1", "inclusion": "included", "reason": "Captured work measurements connect the declared mechanism.",
                 **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        entry["finding"] = {"text": "Both measurements are recorded.", "evidence": proofs}
        windows = runner._survey_review_source_windows([entry], [])
        self.assertEqual([window["text"] for window in windows], [first, last])
        for window in windows:
            start, end = window["window"]["start"], window["window"]["end"]
            self.assertEqual(window["text"], text[start:end])
            self.assertEqual(window["source_body_sha256"], record["body_hash"])
            self.assertEqual(window["source_text_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(window["window_sha256"], hashlib.sha256(window["text"].encode()).hexdigest())
        self.assertLess(sum(len(window["text"]) for window in windows), 200)
        forged = deepcopy(entry)
        forged["finding"]["evidence"][0]["quote_sha256"] = "0" * 64
        with self.assertRaises(ValidationError):
            runner._survey_review_source_windows([forged], [])
        source["text"] += "\n\nAn unrecorded addition."
        with self.assertRaisesRegex(ValidationError, "immutable capture"):
            runner._survey_review_source_windows([entry], [])

    def test_integrated_review_resume_reuses_map_without_retrieval(self):
        config = survey_config(self.endpoint, "survey-review-fails-second")
        first = self.runtime(config).run()
        self.assertEqual(first["status"], "blocked")
        self.assertIn("survey review did not pass every required check", first["error"])
        self.assertFalse(first["survey_current"])
        request_count_before_resume = len(SurveyHTTPFixture.requests)
        policy = {
            "additional_seconds": 40,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reopen", "reopen_scopes": ["integrated_review"]},
        }
        resumed = self.runtime(config, resume_policy=policy)
        with patch.object(resumed, "_search", side_effect=AssertionError("search repeated")), \
                patch.object(resumed, "_bibliographic_call",
                             side_effect=AssertionError("bibliography retrieval repeated")), \
                patch.object(resumed, "_full_texts", side_effect=AssertionError("full-text retrieval repeated")):
            result = resumed.run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertTrue(result["survey_current"])
        resume_requests = SurveyHTTPFixture.requests[request_count_before_resume:]
        self.assertTrue(
            all(request["query"].get("search") == ["readiness"] for request in resume_requests),
            msg=f"integrated-review resume made non-readiness literature requests: {resume_requests!r}",
        )
        control, store = self.open_store()
        prompts = [prompt for _, prompt in self.model_contexts(control, store)
                   if prompt.get("phase") == "survey_review"]
        resumed_prompt = next(prompt for prompt in prompts if prompt.get("resume_boundary"))
        self.assertEqual(resumed_prompt["resume_boundary"], "survey-review-resume-1")

    def test_aggregate_review_separates_screening_accounting_from_scientific_answers(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        runner._materialize_source_less_map("W102", runner.analyzed_basis["W102"],
            scope="review_exhausted")
        runner._record("command/survey-abstentions/W101", "note", {
            "work_id": "W101", "scope": "contract_exhausted", "withdrawn_fields": ["finding"],
            "entry_sha256": runner.analysis_records["W101"]["body_hash"],
        }, "command.controller")
        for wid, status in (("W101", "verified"), ("W102", "insufficient_evidence")):
            runner.identity_records[wid] = runner._record(f"kb/identity-fixture/{wid}", "note",
                {"work_id": wid, "status": status}, "methods.identity-verifier")
        packet = runner._survey_review_packet()
        coverage = packet["coverage"]
        identities = coverage["bibliographic_identities"]
        self.assertEqual(identities["checked"], 2)
        self.assertEqual(identities["verified"], 1)
        self.assertEqual(identities["unresolved"], 1)
        self.assertEqual(sum(identities["by_status"].values()), identities["checked"])
        self.assertEqual(coverage["entry_inclusion_counts"], {"included": 1, "uncertain": 1})
        self.assertEqual(coverage["claimless_entry_count"], 1)
        self.assertEqual(coverage["abstention_count"], 2)
        self.assertEqual(coverage["abstention_work_ids"], ["W101", "W102"])
        self.assertIn("partial withdrawals", coverage["count_definitions"]["abstention_count"])
        self.assertEqual(packet["deterministic_integrity"]["map_entry_count"], 2)
        self.assertEqual(packet["deterministic_integrity"]["source_inventory_work_count"], 2)
        self.assertTrue(packet["deterministic_integrity"]["all_relationship_endpoints_in_map_entries"])
        self.assertIn("abstract_work_count", coverage["count_definitions"])
        self.assertEqual(packet["map"]["projection"], packet["projection"])
        self.assertLessEqual(packet["projection"]["presented_entry_count"],
                             packet["projection"]["entry_count"])
        self.assertLessEqual(packet["projection"]["presented_source_count"],
                             packet["projection"]["source_record_count"])
        self.assertIn("not an established claim", packet["review_contract"]["question_status"])
        self.assertIn("experiments", packet["review_contract"]["downstream_decisions"])
        runner.source_docs = dict(reversed(list(runner.source_docs.items())))
        runner.analysis_records = dict(reversed(list(runner.analysis_records.items())))
        self.assertEqual(packet, runner._survey_review_packet())

    def test_failed_and_unknown_full_text_attempts_survive_review_resume(self):
        runner = self.runtime()
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._record("command/results/final", "report", {"coverage": {
            "access_and_limit_gaps": [{"kind": "full_text_failure", "work_id": "W101", "reason": "unavailable"}],
            "expansion": [{"seed_work_ids": ["W101"]}]}}, "command.controller")
        runner._record("command/source-attempts/full-text/W102", "note", {"work_id": "W102", "status": "reserved"}, "command.controller")
        runner._record("command/results/final", "report", {"coverage": {
            "access_and_limit_gaps": [], "expansion": [{"seed_work_ids": ["W101"]}]}}, "command.controller")
        runner.control.close()
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["focused_review"]}}
        resumed = self.runtime(resume_policy=policy)
        self.addCleanup(resumed.control.close)
        self.assertEqual(resumed.full_text_attempted, {"W101", "W102"})
        self.assertEqual(resumed.expanded, set())

    def test_cached_unsupported_map_is_abstained_without_repeated_calls(self):
        runner = self.runtime(survey_config(self.endpoint, "map-reject"))
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        basis = [runner.work_records["W101"]["artifact_ref"], *runner.source_docs]
        job = runner._map_job("W101", basis)
        runner._models_checked([job])
        self.assertEqual(runner._body(runner.analysis_records["W101"])["inclusion"], "uncertain")
        with patch.object(runner, "_call_batch") as call:
            runner._models_checked([job])
            runner._models_checked([job])
        call.assert_not_called()
        self.assertEqual(runner._body(runner.analysis_records["W101"])["problem"]["text"], None)

    def test_failed_independent_survey_review_prevents_gap_nomination(self):
        config = survey_config(self.endpoint, "survey-fails")
        config["survey"]["proposed_gap"] = None
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("review did not pass", result["error"])
        self.assertIsNone(result["survey_ref"])
        self.assertIsNone(result["nomination"])
        control, store = self.open_store()
        phases = [prompt["phase"] for _, prompt in self.model_contexts(control, store)]
        self.assertIn("survey_review", phases)
        self.assertNotIn("nomination", phases)
        self.assertNotIn("counter_plan", phases)

    def test_abstract_only_decisive_result_cannot_be_adopted(self):
        result = self.runtime(survey_config(self.endpoint, "abstract-refutes")).run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        control, store = self.open_store()
        assessment = json.loads(store.read_body(
            store.head("kb/gap-assessments/current")["body_hash"]))
        self.assertEqual(assessment["state"], "insufficient_evidence")
        self.assertTrue(all(row["relationship"] == "uncertain"
                            for row in assessment["comparisons"]))
        contexts = [prompt for _, prompt in self.model_contexts(control, store)
                    if prompt["phase"] == "gap_assessment"]
        self.assertEqual(len(contexts), 1)

    def test_hard_infeasible_stages_dispatch_nothing(self):
        config = survey_config(self.endpoint)
        config["survey"]["stage_seconds"] = {stage: 10 for stage in STAGES}
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["time_plan"]["initial_hard_limit_feasible"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])
        self.assertEqual(SurveyHTTPFixture.requests, [])
        self.assertEqual(result["usage"]["cumulative_usage"], {})

    def test_work_ceiling_is_reduced_when_minimum_survey_fits(self):
        config = survey_config(self.endpoint)
        config["survey"]["stage_seconds"] = {stage: 10 for stage in STAGES}
        config["limits"]["wall_clock_seconds"] = 70
        config["time_policy"] = {"first_result_seconds": 50, "target_seconds": 50, "hard_seconds": 70}
        runner = self.runtime(config)
        self.assertEqual(runner.bounds["max_works"], 2)
        self.assertEqual(runner.work_budget_adjustments[0]["requested_max_works"], 10)
        self.assertEqual(runner.work_budget_adjustments[0]["effective_max_works"], 2)
        self.assertTrue(runner.time_policy.snapshot()["initial_target_feasible"])

    def test_crossref_fallback_projection_preserves_source_identity(self):
        source = {"doi": "10.1000/Example", "title": "A bounded result",
                  "source_url": "https://doi.org/10.1000/Example",
                  "published": {"date-parts": [[2024]]},
                  "abstract": "<jats:p>Observed error decreases.</jats:p>",
                  "authors": [{"given": "Ada", "family": "Lovelace"}]}
        first = SurveyRunner._crossref_work(source)
        second = SurveyRunner._crossref_work(source)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["id"].startswith("W"))
        self.assertEqual(first["doi"], "10.1000/example")
        self.assertEqual(first["year"], 2024)
        self.assertEqual(first["abstract"], "Observed error decreases.")
        self.assertEqual(first["source_url"], source["source_url"])
        self.assertEqual(first["bibliography_provider"], "crossref")

    def test_crossref_fallback_rebudgets_large_discovery_pages(self):
        config = survey_config(self.endpoint)
        config["survey"]["search"]["max_works"] = 100
        runner = self.runtime(config)
        runner._activate_crossref_fallback("rate_limited")
        self.assertEqual(runner.bounds["max_works"], 20)
        self.assertEqual(runner.time_policy.unit_count, 20)
        self.assertEqual(runner.work_budget_adjustments[-1]["kind"], "provider_fallback_fit")

    def test_bibliography_fallback_can_be_disabled_for_novelty_sensitive_runs(self):
        config = survey_config(self.endpoint)
        config["survey"]["bibliography_fallback"] = "disabled"
        runner = self.runtime(config)
        with self.assertRaisesRegex(ValidationError, "fallback is disabled"):
            runner._activate_crossref_fallback("rate_limited")

    def test_survey_provider_failure_preserves_reset_boundary(self):
        detail = {
            "outcome": "rate_limited",
            "rate_limit": {"kind": "daily_budget", "retry_after_seconds": 432},
        }
        with self.assertRaises(ProviderCooldownError) as caught:
            SurveyRunner._raise_provider_cooldown(detail, "provider reset pending")
        self.assertEqual(caught.exception.retry_after_seconds, 432)

    def test_stale_survey_after_dispatch_checkpoint_blocks_gap_worker(self):
        original = SurveyRunner._checkpoint
        changed = []
        def stale(runner, phase, **kwargs):
            original(runner, phase, **kwargs)
            if phase == "executing" and runner.survey_ref and not changed:
                contexts = runner.control._conn.execute(
                    "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE 'command/contexts/%' ORDER BY rowid DESC LIMIT 1").fetchone()
                if contexts:
                    body = json.loads(runner.store.read_body(runner.store.get(contexts[0])["body_hash"]))
                    prompt = json.loads(body["prompt"]) if "prompt" in body else {}
                    if prompt.get("phase") == "counter_plan":
                        runner._publish("kb/work-register", "note", {"changed": True}, "research.cataloger")
                        changed.append(True)
        with patch.object(SurveyRunner, "_checkpoint", stale):
            result = self.runtime().run()
        self.assertEqual(changed, [True])
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["survey_current"])
        self.assertIsNone(result["assessment_ref"])
        control, _ = self.open_store()
        self.assertEqual(control._conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE logical_id LIKE 'command/executions/survey-counter-plan-%'").fetchone()[0], 0)
        self.assertFalse(any(request["query"].get("search") == ["prior solution"] for request in SurveyHTTPFixture.requests))

    def test_changed_nomination_after_checkpoint_blocks_gap_worker(self):
        original = SurveyRunner._checkpoint
        changed = []
        def revise(runner, phase, **kwargs):
            original(runner, phase, **kwargs)
            if phase == "executing" and hasattr(runner, "nomination_record") and not changed:
                context = runner.control._conn.execute(
                    "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE 'command/contexts/%' ORDER BY rowid DESC LIMIT 1").fetchone()
                if context:
                    body = json.loads(runner.store.read_body(runner.store.get(context[0])["body_hash"]))
                    prompt = json.loads(body["prompt"]) if "prompt" in body else {}
                    if prompt.get("phase") == "counter_plan":
                        runner._publish("kb/gap-nomination", "note", {
                            "survey_ref": runner.survey_ref, "id": "different-gap",
                            "statement": "A different research question."}, "research.gap-proposer")
                        changed.append(True)
        with patch.object(SurveyRunner, "_checkpoint", revise):
            result = self.runtime().run()
        self.assertEqual(changed, [True])
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["survey_current"])
        self.assertFalse(result["assessment_current"])
        self.assertIsNone(result["assessment_ref"])
        control, _ = self.open_store()
        self.assertEqual(control._conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE logical_id LIKE 'command/executions/survey-counter-plan-%'").fetchone()[0], 0)

    def test_deadline_after_review_prevents_survey_publication(self):
        original = SurveyRunner._model_checked
        expired = []
        def expire(runner, name, *args, **kwargs):
            outcome = original(runner, name, *args, **kwargs)
            if name == "survey-review":
                runner.deadline = time.monotonic() - 1
                expired.append(True)
            return outcome
        with patch.object(SurveyRunner, "_model_checked", expire):
            result = self.runtime().run()
        self.assertEqual(expired, [True])
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["survey_ref"])
        self.assertIsNone(result["assessment_ref"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])
        control, store = self.open_store()
        self.assertIsNone(store.accepted("kb/surveys/current"))
        self.assertEqual(control._conn.execute("SELECT COUNT(*) FROM events WHERE event_type='survey.accepted'").fetchone()[0], 0)

    def full_text_config(self, mode, *, source_url="https://example.org/W401"):
        fixture = self.root / "simulated_fetch_python"
        fixture.write_text("#!" + sys.executable + "\n" + MCP_SURVEY_FIXTURE)
        fixture.chmod(0o755)
        config = survey_config(self.endpoint, mode)
        config["survey"]["full_text"] = {"id": "full-text", "adapter": "mcp_fetch",
            "client": {"command": [str(fixture), "-m", "mcp_server_fetch"], "timeout": 4, "max_bytes": 1000000},
            "representative": {"url": "https://example.org/W401", "max_length": 10000},
            "environment_files": [str(fixture)]}
        config["survey"]["full_text_sources"] = [{"work_id": "W401", "title": "Recall study W401",
            "url": source_url, "section_markers": ["Methods", "Results"]}]
        return config

    def test_automatic_capture_uses_actual_body_headings_without_introduction(self):
        config = self.full_text_config("fulltext-refutes")
        config["survey"]["full_text_sources"] = []
        SurveyHTTPFixture.locations_by_work = {"W401": [{"is_oa": True,
            "landing_page_url": "https://example.org/W401", "pdf_url": None}]}
        runner = self.runtime(config)
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W401")
        runner._full_texts()
        capture = runner._body(runner.source_records["full_text/W401"])
        self.assertEqual(capture["representation"], "full_text")
        self.assertEqual(capture["identity_checks"]["section_markers"], ["Methods", "Results"])

    def test_explicit_missing_introduction_remains_unverified(self):
        config = self.full_text_config("fulltext-refutes")
        config["survey"]["full_text_sources"][0]["section_markers"] = ["Introduction"]
        runner = self.runtime(config)
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W401")
        runner._full_texts()
        capture = runner._body(runner.source_records["full_text/W401"])
        self.assertEqual(capture["representation"], "unverified_text")
        runner._revalidate_retained_full_texts()
        self.assertEqual(runner._body(runner.source_records["full_text/W401"]), capture)

    def test_unverified_text_does_not_crowd_out_usable_abstract(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner.source_docs = {
            "bad": {"work_id": "W1", "representation": "unverified_text", "identity_verified": False,
                    "identity_checks": {"title_match": False, "section_markers": []}, "text": "unverified " * 5000},
            "abstract": {"work_id": "W1", "representation": "abstract", "text": "Reliable abstract."}}
        sources = runner._map_sources("W1")
        self.assertEqual([source["source_ref"] for source in sources], ["abstract"])
        self.assertEqual([source["source_ref"] for source in runner._assessment_source_context()], ["abstract"])
        diagnostic = runner._source_context()[0]
        self.assertIs(diagnostic["identity_verified"], False)
        self.assertFalse(diagnostic["identity_checks"]["title_match"])

    def test_retained_auto_capture_revalidation_pins_original_bytes_and_invalidates_credit(self):
        config = self.full_text_config("fulltext-refutes")
        config["survey"]["full_text_sources"] = []
        SurveyHTTPFixture.locations_by_work = {"W401": [{"is_oa": True,
            "landing_page_url": "https://example.org/W401", "pdf_url": None}]}
        runner = self.runtime(config)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W401")
        runner._full_texts()
        valid = runner._body(runner.source_records["full_text/W401"])
        historical = {**valid, "representation": "unverified_text", "identity_verified": False,
                      "identity_checks": {"title_match": True, "section_markers": []}}
        old = runner._record("kb/full-text/W401", "source_capture", historical, "methods.source-verifier",
                             subjects=[valid["execution_ref"], runner.work_records["W401"]["artifact_ref"]])
        runner.source_docs = {ref: body for ref, body in runner.source_docs.items() if body["representation"] == "abstract"}
        runner.source_docs[old["artifact_ref"]] = historical
        runner.source_records["full_text/W401"] = old
        runner._update_register(); runner._map(); runner._review_work_claims()
        runner.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["operations"]}}
        before = len(SurveyHTTPFixture.requests)
        resumed = self.runtime(config, resume_policy=policy)
        self.addCleanup(resumed.control.close)
        record = resumed.source_records["full_text/W401"]
        self.assertNotEqual(record["artifact_ref"], old["artifact_ref"])
        self.assertEqual(resumed._body(record), valid)
        self.assertEqual(len(SurveyHTTPFixture.requests), before)
        self.assertNotIn(old["artifact_ref"], resumed.source_docs)
        self.assertTrue({old["artifact_ref"], valid["execution_ref"], resumed.work_records["W401"]["artifact_ref"]}.issubset(
            {item["ref"] for item in record["inputs"]}))
        self.assertNotIn("W401", resumed.analyzed_basis)
        self.assertNotIn("W401", resumed.reviewed_basis)

    def test_real_stdio_full_text_can_refute_prior_gap(self):
        result = self.runtime(self.full_text_config("fulltext-refutes")).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["gap_state"], "refuted_by_prior_work")
        self.assertEqual(result["coverage"]["verified_full_texts"], 1)
        self.assertTrue(result["survey_current"])
        self.assertIsNotNone(result["assessment_ref"])
        self.assertEqual(result["usage"]["cumulative_usage"]["retrieval_calls"], 9)
        _, store = self.open_store()
        capture = json.loads(store.read_body(store.head("kb/full-text/W401")["body_hash"]))
        execution = json.loads(store.read_body(store.get(capture["execution_ref"])["body_hash"]))
        self.assertEqual(execution["metadata"]["server_info"]["version"], "simulated-fixture-1")
        self.assertEqual(execution["metadata"]["process_returncode"], 0)
        self.assertEqual(execution["metadata"]["transport"], "mcp_stdio")

    def test_accessible_openalex_pdf_fallback_is_extracted_and_verified(self):
        SurveyHTTPFixture.locations_by_work = {"W401": [{
            "is_oa": True,
            "landing_page_url": "https://example.org/registered-article",
            "pdf_url": f"http://127.0.0.1:{self.server.server_port}/paper.pdf",
        }]}
        config = self.full_text_config("fulltext-refutes", source_url="https://example.org/unavailable")
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["gap_state"], "refuted_by_prior_work")
        self.assertEqual(result["coverage"]["verified_full_texts"], 1)
        control, store = self.open_store()
        capture = json.loads(store.read_body(store.head("kb/full-text/W401")["body_hash"]))
        self.assertTrue(capture["identity_verified"])
        self.assertTrue(capture["url"].endswith("/paper.pdf"))
        execution = json.loads(store.read_body(store.get(capture["execution_ref"])["body_hash"]))
        self.assertEqual(execution["metadata"]["representation"], "pdf_extracted_text")
        self.assertEqual(execution["metadata"]["pdf_extraction"]["http_status"], 200)
        self.assertIn("This prior method solves delayed recall.", capture["text"])
        workload = store.head("command/operations/full-text/workloads")
        checks = json.loads(store.read_body(workload["body_hash"]))["checks"]
        check_outcomes = {check["check_id"]: check["outcome"] for check in checks}
        self.assertEqual(check_outcomes["pdf-result-budget"], "passed")
        self.assertEqual(check_outcomes["pdf-extraction-reproducible"], "passed")
        gaps = [item for item in result["coverage"]["access_and_limit_gaps"]
                if item.get("kind") == "full_text_failure"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["source_url"], "https://example.org/unavailable")

    def test_failed_primary_oa_pdf_uses_next_distinct_oa_pdf(self):
        SurveyHTTPFixture.locations_by_work = {"W401": [
            {
                "is_oa": True,
                "landing_page_url": "https://example.org/aps-record",
                "pdf_url": f"http://127.0.0.1:{self.server.server_port}/gateway-error.pdf",
            },
            {
                "is_oa": True,
                "landing_page_url": "https://example.org/arxiv-record",
                "pdf_url": f"http://127.0.0.1:{self.server.server_port}/fallback.pdf",
            },
        ]}
        primary_url = f"http://127.0.0.1:{self.server.server_port}/gateway-error.pdf"
        config = self.full_text_config("fulltext-refutes", source_url=primary_url)
        result = self.runtime(config).run()

        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["coverage"]["verified_full_texts"], 1)
        requested_paths = [item["path"] for item in SurveyHTTPFixture.requests]
        self.assertIn("/gateway-error.pdf", requested_paths)
        self.assertIn("/fallback.pdf", requested_paths)
        _, store = self.open_store()
        capture = json.loads(store.read_body(store.head("kb/full-text/W401")["body_hash"]))
        self.assertTrue(capture["identity_verified"])
        self.assertTrue(capture["url"].endswith("/fallback.pdf"))

    def test_registered_openalex_pdf_uses_poppler_even_without_pdf_suffix(self):
        landing_page = "https://example.org/W401"
        pdf_url = f"http://127.0.0.1:{self.server.server_port}/paper-download"
        SurveyHTTPFixture.locations_by_work = {"W401": [{
            "is_oa": True, "landing_page_url": landing_page, "pdf_url": pdf_url,
        }]}
        config = self.full_text_config("fulltext-refutes", source_url=landing_page)
        config["survey"]["full_text_sources"][0]["route_policy"] = "auto"
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["coverage"]["verified_full_texts"], 1)
        self.assertIn("/paper-download", [item["path"] for item in SurveyHTTPFixture.requests])
        _, store = self.open_store()
        capture = json.loads(store.read_body(store.head("kb/full-text/W401")["body_hash"]))
        self.assertEqual(capture["url"], pdf_url)
        execution = json.loads(store.read_body(store.get(capture["execution_ref"])["body_hash"]))
        self.assertEqual(execution["metadata"]["representation"], "pdf_extracted_text")
        self.assertEqual(execution["metadata"]["pdf_extraction"]["parser"]["name"], "poppler-pdftotext")

    def test_legacy_route_without_policy_preserves_registered_landing_url(self):
        landing_page = "https://example.org/registered-article"
        pdf_url = f"http://127.0.0.1:{self.server.server_port}/paper-download"
        SurveyHTTPFixture.locations_by_work = {"W401": [{
            "is_oa": True, "landing_page_url": landing_page, "pdf_url": pdf_url,
        }]}
        config = self.full_text_config("fulltext-refutes", source_url=landing_page)
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["coverage"]["verified_full_texts"], 1)
        self.assertNotIn("/paper-download", [item["path"] for item in SurveyHTTPFixture.requests])
        _, store = self.open_store()
        capture = json.loads(store.read_body(store.head("kb/full-text/W401")["body_hash"]))
        self.assertEqual(capture["url"], landing_page)

    def test_installed_mcp_fetch_403_is_typed_without_pdf_fallback(self):
        server_spec = importlib.util.find_spec("mcp_server_fetch")
        if server_spec is None or not server_spec.origin:
            self.skipTest("Official MCP Fetch package is not installed in this Python environment")
        SurveyHTTPFixture.locations_by_work = {"W401": [{
            "is_oa": True,
            "landing_page_url": "https://example.org/registered-article",
            "pdf_url": f"http://127.0.0.1:{self.server.server_port}/paper.pdf",
        }]}
        config = self.full_text_config(
            "pass", source_url=f"http://127.0.0.1:{self.server.server_port}/mcp-denied")
        fetch = config["survey"]["full_text"]
        fetch["client"]["command"] = [sys.executable, "-m", "mcp_server_fetch"]
        fetch["environment_files"] = [server_spec.origin]
        fetch["representative"] = {
            "url": f"http://127.0.0.1:{self.server.server_port}/article",
            "max_length": 10000,
        }
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["coverage"]["verified_full_texts"], 0)
        requested_paths = [item["path"] for item in SurveyHTTPFixture.requests]
        self.assertIn("/mcp-denied", requested_paths)
        self.assertNotIn("/paper.pdf", requested_paths)
        failure = next(item for item in result["coverage"]["access_and_limit_gaps"]
                       if item.get("kind") == "full_text_failure")
        self.assertEqual(failure["outcome"], "access_denied")
        self.assertIn("status code 403", failure["reason"])

    def test_transient_mcp_error_uses_bounded_openalex_pdf_fallback(self):
        SurveyHTTPFixture.locations_by_work = {"W401": [{
            "is_oa": True,
            "landing_page_url": "https://example.org/registered-article",
            "pdf_url": f"http://127.0.0.1:{self.server.server_port}/paper.pdf",
        }]}
        config = self.full_text_config("fulltext-refutes", source_url="https://example.org/transport-error")
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["coverage"]["verified_full_texts"], 1)
        self.assertIn("/paper.pdf", [item["path"] for item in SurveyHTTPFixture.requests])
        _, store = self.open_store()
        capture = json.loads(store.read_body(store.head("kb/full-text/W401")["body_hash"]))
        self.assertTrue(capture["url"].endswith("/paper.pdf"))

    def test_mcp_http_denial_is_typed_and_does_not_fall_through_to_openalex_pdf(self):
        SurveyHTTPFixture.locations_by_work = {"W401": [{
            "is_oa": True,
            "landing_page_url": "https://example.org/registered-article",
            "pdf_url": f"http://127.0.0.1:{self.server.server_port}/paper.pdf",
        }]}
        config = self.full_text_config("pass", source_url="https://example.org/mcp-denied")
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["coverage"]["verified_full_texts"], 0)
        self.assertNotIn("/paper.pdf", [item["path"] for item in SurveyHTTPFixture.requests])
        failure = next(item for item in result["coverage"]["access_and_limit_gaps"]
                       if item.get("kind") == "full_text_failure")
        self.assertEqual(failure["outcome"], "access_denied")

    def test_http_denial_does_not_fall_through_to_openalex_pdf(self):
        denied_url = f"http://127.0.0.1:{self.server.server_port}/denied.pdf"
        SurveyHTTPFixture.locations_by_work = {"W401": [{
            "is_oa": True,
            "landing_page_url": "https://example.org/registered-article",
            "pdf_url": f"http://127.0.0.1:{self.server.server_port}/paper.pdf",
        }]}
        config = self.full_text_config("pass", source_url=denied_url)
        config["survey"]["full_text_sources"][0]["route_policy"] = "exact"
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["coverage"]["verified_full_texts"], 0)
        self.assertIn("/robots.txt", [item["path"] for item in SurveyHTTPFixture.requests])
        self.assertIn("/denied.pdf", [item["path"] for item in SurveyHTTPFixture.requests])
        self.assertNotIn("/paper.pdf", [item["path"] for item in SurveyHTTPFixture.requests])
        failure = next(item for item in result["coverage"]["access_and_limit_gaps"]
                       if item.get("kind") == "full_text_failure")
        self.assertEqual(failure["outcome"], "access_denied")

    def test_access_denial_uses_abstract_scoped_evidence_through_review_and_resume(self):
        denied_url = f"http://127.0.0.1:{self.server.server_port}/denied.pdf"
        config = self.full_text_config("pass", source_url=denied_url)
        order = self.follow_up_order()
        result = self.runtime(config, work_orders=[order]).run()
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(result["gap_state"], "insufficient_evidence")
        self.assertEqual(result["coverage"]["verified_full_texts"], 0)
        availability = next(row for row in result["coverage"]["source_availability"]
                            if row["work_id"] == "W401")
        self.assertEqual(availability["evidence_scope"], "abstract")
        self.assertEqual(availability["full_text_access"], "unavailable")
        self.assertTrue(availability["abstract_refs"])
        self.assertEqual(availability["verified_full_text_refs"], [])
        self.assertEqual(availability["full_text_failures"][0]["outcome"], "access_denied")
        self.assertTrue(availability["full_text_failures"][0]["execution_ref"])
        control, store = self.open_store()
        phases = set()
        for _, assignment in self.model_contexts(control, store):
            if assignment.get("phase") in {"map", "work_review", "survey_review", "gap_assessment", "survey_follow_up"}:
                phases.add(assignment["phase"])
                self.assertEqual(assignment["source_evidence_policy"], result["coverage"]["source_evidence_policy"])
                self.assertEqual(assignment["scientific_input_recovery"],
                                 result["coverage"]["scientific_input_recovery"])
                for source in assignment.get("sources", []):
                    if source.get("work_id") == "W401":
                        self.assertEqual(source["source_availability"], availability)
                        self.assertEqual(source["representation"], "abstract")
        self.assertEqual(phases, {"map", "work_review", "survey_review", "gap_assessment", "survey_follow_up"})
        policy = {"additional_seconds": 40,
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["follow_up"]}}
        resumed = self.runtime(config, resume_policy=policy, work_orders=[order])
        self.assertEqual(resumed._source_availability("W401"), availability)
        with patch.object(resumed.operations, "run", side_effect=AssertionError("inaccessible source must not be retried")):
            resumed._full_texts()
        self.assertTrue(all(source["representation"] == "abstract"
                            for source in resumed._assessment_source_context()))
        followup = resumed._follow_up_assignment({"sources": resumed._assessment_source_context()})
        self.assertEqual(followup["source_evidence_policy"], result["coverage"]["source_evidence_policy"])
        policy["source_changes"]["reopen_scopes"] = ["retrieval"]
        reopened = self.runtime(config, resume_policy=policy, work_orders=[order])
        with patch.object(reopened.operations, "run", side_effect=AssertionError("retrieval reopen must preserve terminal access unavailability")):
            reopened._full_texts()
        self.assertEqual(reopened._source_availability("W401"), availability)

    def test_full_text_robots_http429_stops_instead_of_using_abstract_fallback(self):
        runner = self.runtime(self.full_text_config("pass"))
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W401")
        execution = runner._record("command/rate-limit-fixture", "note", {}, "command.controller")["artifact_ref"]
        response = {"outcome": "robots_denied", "error": "Robots policy HTTP429",
                    "metadata": {"robots_policy": {"status": 429}}}
        with patch.object(runner.operations, "run", return_value=(response, execution)) as dispatch:
            with self.assertRaisesRegex(ProviderRateLimitError, "rate limit requires provider recovery") as stopped:
                runner._full_texts()
            self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(stopped.exception.provider, "full_text")
        self.assertEqual(stopped.exception.details["execution_ref"], execution)
        self.assertEqual(stopped.exception.details["metadata"], response["metadata"])
        availability = runner._source_availability("W401")
        self.assertEqual(availability["full_text_access"], "rate_limited")
        self.assertEqual(availability["full_text_failures"][0]["outcome"], "rate_limited")
        receipt = runner._body(runner.store.head("command/source-attempts/full-text/W401-1"))
        self.assertEqual(receipt["status"], "rate_limited")
        with patch.object(runner.operations, "run", side_effect=AssertionError("rate limit must stop later dispatch")):
            with self.assertRaisesRegex(ValidationError, "provider recovery"):
                runner._full_texts()

    def test_full_text_readiness_http429_is_typed_and_preserves_immutable_probe(self):
        runner = self.runtime(self.full_text_config("pass"))
        runner._initialize()
        metadata = {"robots_policy": {"status": 429}, "rate_limit": {"retry_after_seconds": 60}}
        execution = runner._record("command/executions/readiness-rate", "report", {
            "outcome": "robots_denied", "error": "Robots policy HTTP429", "metadata": metadata,
        }, "operations.operator")
        probe = runner._record("command/readiness-rate", "note", {
            "execution_ref": execution["artifact_ref"]}, "operations.verifier")
        full_text_id = runner.score["full_text"]["id"]
        def readiness(capability_id, *args, **kwargs):
            return ({"state": "degraded", "reason": "Robots policy HTTP429"} if capability_id == full_text_id
                    else {"state": "ready", "binding": "fixture-ready-binding"})
        with patch.object(runner.operations, "ensure_ready", side_effect=readiness) as dispatch, \
             patch.object(runner.operations, "idle"), \
             patch.object(runner.operations, "status", return_value={"probe_ref": probe["artifact_ref"]}):
            with self.assertRaises(ProviderRateLimitError) as stopped:
                runner._setup()
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(stopped.exception.provider, "full_text")
        self.assertEqual(stopped.exception.details["execution_ref"], execution["artifact_ref"])
        self.assertEqual(stopped.exception.details["metadata"], metadata)
        self.assertEqual(stopped.exception.details["kind"], "full_text_readiness_failure")
        self.assertNotIn("full_text", runner.bindings)
        self.assertFalse(any(gap.get("kind") == "full_text_unavailable" for gap in runner.gaps))

    def test_full_text_owned_http429_stops_before_recovery_and_source_admission(self):
        for path in ("exception", "recovery", "result"):
            config = self.full_text_config("pass")
            runner = SurveyRunner(self.root / f"full-text-{path}", config)
            self.addCleanup(runner.control.close)
            runner._initialize()
            runner.works = {"W401": {"id": "W401", "title": config["survey"]["full_text_sources"][0]["title"]}}
            runner.work_records["W401"] = runner._record("kb/works/W401", "reference_card", runner.works["W401"], "research.cataloger")
            runner.bindings["full_text"] = "fixture-text-binding"
            metadata = {"rate_limit": {"status_code": 429}}
            response = {"outcome": "ok" if path == "result" else "provider_error", "metadata": metadata, "error": "HTTP429"}
            if path == "result":
                metadata.clear()
                response.update(status_code=429, rate_limit={"retry_after_seconds": 60})
            execution = runner._record("command/executions/text-rate", "report", response, "operations.operator")
            failure = runner._record("command/text-rate", "note", {"execution_ref": execution["artifact_ref"]}, "operations.verifier")
            states = ([{} , {"failure_ref": failure["artifact_ref"]}] if path == "recovery"
                      else [{"failure_ref": failure["artifact_ref"]}])
            dispatch_args = ({"return_value": (response, execution["artifact_ref"])} if path == "result"
                             else {"side_effect": ValidationError("owned text operation failed")})
            with patch.object(runner.operations, "run", **dispatch_args) as dispatch, \
                 patch.object(runner.operations, "status", side_effect=states), \
                 patch.object(runner.operations, "ensure_ready", return_value={"state": "degraded", "reason": "HTTP429"}) as recovery:
                with self.assertRaises(ProviderRateLimitError) as stopped:
                    runner._full_texts()
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(recovery.call_count, int(path == "recovery"))
            self.assertEqual(stopped.exception.details["metadata"], metadata)
            self.assertEqual(stopped.exception.details["execution_ref"], execution["artifact_ref"])
            if path == "result":
                self.assertEqual(stopped.exception.details["rate_limit"], response["rate_limit"])
            self.assertEqual(runner.source_docs, {})

    def test_full_text_rate_limit_stays_paused_at_run_boundary_with_or_without_retry_after(self):
        for index, rate_limit in enumerate((None, {"retry_after_seconds": 60})):
            runner = SurveyRunner(self.root / f"rate-boundary-{index}", survey_config(self.endpoint))
            self.addCleanup(runner.control.close)
            details = {"kind": "full_text_failure", "outcome": "rate_limited",
                       "execution_ref": "artifact:command/executions/retained-rate@1",
                       "metadata": {"http_status": 429, "rate_limit": rate_limit}}
            error = ProviderRateLimitError("Full-text provider requires recovery", provider="full_text", details=details)
            with patch.object(runner, "_setup", side_effect=error), \
                 patch.object(runner, "_call", side_effect=AssertionError("rate limit must stop later dispatch")):
                result = runner.run()
            self.assertEqual(result["status"], "paused", result.get("error"))
            self.assertEqual(result["failure"], {"kind": "provider_rate_limit", "provider": "full_text", "details": details})

    def test_all_provider_readiness_http429_stops_before_fallback_or_later_probes(self):
        for key in ("bibliography", "identity", "full_text"):
            for index, retry_after in enumerate((None, 60)):
                config = self.full_text_config("pass")
                config["survey"]["identity"] = {
                    "id": "identity", "adapter": "crossref", "client": {"endpoint": self.endpoint, "timeout": 4, "max_bytes": 1000000, "mailto": "catalog@example.org"},
                    "representative": {"query": "readiness", "limit": 1}, "environment_files": []}
                runner = SurveyRunner(self.root / f"ready-{key}-{index}", config)
                self.addCleanup(runner.control.close)
                runner._initialize()
                metadata = {"rate_limit": {"status_code": 429, "retry_after_seconds": retry_after}}
                execution = runner._record("command/executions/readiness-rate", "report", {
                    "outcome": "provider_error", "metadata": metadata}, "operations.operator")
                probe = runner._record("command/readiness-rate", "note", {
                    "execution_ref": execution["artifact_ref"]}, "operations.verifier")
                target = runner.score[key]["id"]
                def readiness(capability_id, *args, **kwargs):
                    return ({"state": "degraded", "reason": "HTTP429"} if capability_id == target
                            else {"state": "ready", "binding": "fixture-ready"})
                with patch.object(runner.operations, "ensure_ready", side_effect=readiness) as dispatch, \
                     patch.object(runner.operations, "idle"), \
                     patch.object(runner.operations, "status", return_value={"probe_ref": probe["artifact_ref"]}):
                    with self.assertRaises(ProviderRateLimitError) as stopped:
                        runner._setup()
                self.assertEqual(dispatch.call_count, ("bibliography", "identity", "full_text").index(key) + 1)
                self.assertEqual(stopped.exception.provider, {"bibliography": "openalex", "identity": "crossref", "full_text": "full_text"}[key])
                self.assertEqual(stopped.exception.details["metadata"], metadata)
                self.assertEqual(stopped.exception.details["execution_ref"], execution["artifact_ref"])
                self.assertEqual(runner.bibliography_mode, "openalex")
                self.assertIn(stopped.exception.details, runner.gaps)

    def test_bibliographic_http429_stops_result_and_owned_exception_before_ingest(self):
        for provider in ("openalex", "crossref"):
            for path in ("result", "exception"):
                for index, retry_after in enumerate((None, 60)):
                    config = survey_config(self.endpoint)
                    config["survey"]["identity"] = {
                        "id": "identity", "adapter": "crossref", "client": {"endpoint": self.endpoint, "timeout": 4, "max_bytes": 1000000, "mailto": "catalog@example.org"},
                        "representative": {"query": "readiness", "limit": 1}, "environment_files": []}
                    runner = SurveyRunner(self.root / f"search-{provider}-{path}-{index}", config)
                    self.addCleanup(runner.control.close)
                    runner._initialize()
                    runner.bindings = {"bibliography": "fixture-openalex", "identity": "fixture-crossref"}
                    runner.bibliography_mode = provider
                    metadata = {"rate_limit": {"status_code": 429, "retry_after_seconds": retry_after}}
                    response = {"outcome": "provider_error", "error": "HTTP429", "works": [], "sources": [], "metadata": metadata}
                    if path == "exception":
                        metadata["rate_limit"].pop("status_code")
                        response["status_code"] = 429
                    execution = runner._record("command/executions/search-rate", "report", response, "operations.operator")
                    failure = runner._record("command/search-rate", "note", {"execution_ref": execution["artifact_ref"]}, "operations.verifier")
                    dispatch_args = ({"return_value": (response, execution["artifact_ref"])} if path == "result"
                                     else {"side_effect": ValidationError("owned provider execution failed")})
                    with patch.object(runner.operations, "run", **dispatch_args) as dispatch, \
                         patch.object(runner.operations, "status", return_value={"failure_ref": failure["artifact_ref"]}), \
                         patch.object(runner, "_activate_crossref_fallback", side_effect=AssertionError("no rate-limit fallback")), \
                         patch.object(runner, "_ingest", side_effect=AssertionError("no rate-limit ingestion")):
                        with self.assertRaises(ProviderRateLimitError) as stopped:
                            runner._bibliographic_call("search", role="research.searcher", query="bounded query")
                    self.assertEqual(dispatch.call_count, 1)
                    self.assertEqual(stopped.exception.provider, provider)
                    self.assertEqual(stopped.exception.details["metadata"], metadata)
                    self.assertEqual(stopped.exception.details["execution_ref"], execution["artifact_ref"])
                    self.assertIn(stopped.exception.details, runner.gaps)
                    self.assertEqual(runner.query_refs, [])
        runner = self.runtime()
        runner._initialize()
        runner._stop_provider_rate_limit({"outcome": "robots_denied", "metadata": {"status": 403}}, provider="full_text")
        runner._stop_provider_rate_limit({"outcome": "robots_unavailable", "metadata": {"status_code": 406}}, provider="full_text")
        with self.assertRaises(ProviderRateLimitError):
            runner._stop_provider_rate_limit({"outcome": "rate_limited"}, provider="openalex")

    def test_identity_workload_http429_does_not_become_an_optional_lookup_gap(self):
        for path in ("result", "exception"):
            for index, retry_after in enumerate((None, 60)):
                runner = SurveyRunner(self.root / f"identity-{path}-{index}", survey_config(self.endpoint))
                self.addCleanup(runner.control.close)
                runner._initialize()
                runner.score["identity"] = {"id": "fixture-identity"}
                runner.bindings["identity"] = "fixture-identity-binding"
                runner.works = {"W1": {"id": "W1", "doi": "10.1234/example"},
                                "W2": {"id": "W2", "doi": "10.1234/second"}}
                metadata = {"rate_limit": {"status_code": 429, "retry_after_seconds": retry_after}}
                response = {"outcome": "provider_error", "error": "HTTP429", "metadata": metadata}
                if path == "exception":
                    metadata["rate_limit"].pop("status_code")
                    response["status_code"] = 429
                execution = runner._record("command/executions/identity-rate", "report", response, "operations.operator")
                failure = runner._record("command/identity-rate", "note", {"execution_ref": execution["artifact_ref"]}, "operations.verifier")
                dispatch_args = ({"return_value": (response, execution["artifact_ref"])} if path == "result"
                                 else {"side_effect": ValidationError("owned identity execution failed")})
                with patch.object(runner, "_analysis_selection", return_value={"W1", "W2"}), \
                     patch.object(runner.operations, "run", **dispatch_args) as dispatch, \
                     patch.object(runner.operations, "status", return_value={"failure_ref": failure["artifact_ref"]}):
                    with self.assertRaises(ProviderRateLimitError) as stopped:
                        runner._reconcile_identities()
                self.assertEqual(dispatch.call_count, 1)
                self.assertEqual(stopped.exception.provider, "crossref")
                self.assertEqual(stopped.exception.details["metadata"], metadata)
                self.assertEqual(stopped.exception.details["execution_ref"], execution["artifact_ref"])
                self.assertEqual(stopped.exception.details["error"], "HTTP429")
                self.assertIn("identity", runner.bindings)
                self.assertEqual(runner.identity_records, {})

    def test_crossref_response_limits_pace_probe_and_workload_without_changing_config(self):
        runner = self.runtime()
        configured = deepcopy(runner.config)
        runner.provider_intervals = {"identity": 0.5, "bibliography": 0.25}
        runner.next_provider_at = {"identity": 0, "bibliography": 0}
        result = {"metadata": {"headers": {"X-Rate-Limit-Limit": "1", "X-Rate-Limit-Interval": "1s"}}}
        with patch("scisaurus.runtime.execution.ExecutionRuntime._call", return_value=(result, "execution")), \
             patch("scisaurus.runtime.survey.time.monotonic", return_value=100):
            self.assertEqual(runner._call("probe", "crossref", {}, actor="operations.operator",
                                         task_kind="retrieval"), (result, "execution"))
        self.assertEqual(runner.provider_intervals["identity"], 1)
        self.assertEqual(runner.next_provider_at["identity"], 101)
        self.assertEqual(runner.next_provider_at["bibliography"], 0)
        runner.bibliography_mode = "crossref"
        with patch("scisaurus.runtime.survey.time.monotonic", return_value=101):
            runner._observe_crossref_limits({"metadata": {"headers": {
                "x-rate-limit-limit": "3", "x-rate-limit-interval": "1s", "retry-after": "4"}}})
        self.assertEqual(runner.provider_intervals["identity"], 1)
        self.assertEqual(runner.next_provider_at["identity"], 105)
        self.assertEqual(runner.next_provider_at["bibliography"], 105)
        self.assertEqual(runner.config, configured)
        for limit, interval in [("nan", "1s"), ("0", "1s"), ("2", "nan"), ("inf", "1s")]:
            with patch("scisaurus.runtime.survey.time.monotonic", return_value=101):
                runner._observe_crossref_limits({"metadata": {"headers": {
                    "x-rate-limit-limit": limit, "x-rate-limit-interval": interval}}})
        self.assertEqual(runner.provider_intervals["identity"], 1)
        self.assertEqual(runner.next_provider_at["identity"], 105)

    def test_crossref_retry_after_is_honored_without_recurring_interval(self):
        runner = self.runtime()
        runner.provider_intervals = {}
        runner.next_provider_at = {"identity": 0}
        now = [100.0]
        def sleep(seconds):
            now[0] += seconds
        with patch("scisaurus.runtime.survey.time.monotonic", side_effect=lambda: now[0]), \
             patch("scisaurus.runtime.survey.time.sleep", side_effect=sleep):
            runner._observe_crossref_limits({"metadata": {"headers": {"retry-after": "2"}}})
            runner._wait_provider("identity")
        self.assertEqual(now[0], 102)
        self.assertEqual(runner.provider_waits[-1]["waited_seconds"], 2)
        self.assertEqual(runner.provider_intervals, {})

    def test_identity_rate_limit_resume_settles_recorded_failure_without_losing_usage(self):
        for missing_receipt in (False, True):
            with self.subTest(missing_receipt=missing_receipt):
                project = self.root / f"identity-resume-{missing_receipt}"
                config = survey_config(self.endpoint)
                config["survey"]["identity"] = {
                    "id": "identity", "adapter": "crossref",
                    "client": {"endpoint": self.endpoint, "timeout": 4, "max_bytes": 1000000,
                               "mailto": "catalog@example.org"},
                    "representative": {"query": "10.1234/W101", "limit": 1}, "environment_files": []}
                first = SurveyRunner(project, config)
                self.addCleanup(first.control.close)
                first._initialize(); first._setup()
                first._bibliographic_call("work", role="research.seed-reader", work_id="W101")
                SurveyHTTPFixture.identity_rate_limit_once = "10.1234/w101"
                original = first._record
                def record(logical, *args, **kwargs):
                    if missing_receipt and logical.startswith("command/identity-rate-limits/"):
                        return None
                    return original(logical, *args, **kwargs)
                with patch.object(first, "_analysis_selection", return_value={"W101"}), \
                     patch.object(first, "_record", side_effect=record):
                    with self.assertRaises(ProviderRateLimitError) as stopped:
                        first._reconcile_identities()
                charged = first.api_calls
                execution = stopped.exception.details["execution_ref"]
                with self.assertRaises(ValidationError):
                    first.gate._recorded_execution(execution, "research.identity-checker",
                        operation="crossref", task_kinds={"retrieval"})
                first.gate._recorded_execution(execution, "research.identity-checker",
                    operation="crossref", task_kinds={"retrieval"}, allow_blocked_retrieval=True)
                if missing_receipt:
                    first._reconcile_identity_rate_limits({"query": "10.1234/other", "limit": 3})
                    with patch.dict(first.score["identity"]["client"], {"endpoint": "http://wrong-provider"}):
                        first._reconcile_identity_rate_limits({"query": "10.1234/w101", "limit": 3})
                    self.assertIsNone(first.store.head(f"command/identity-rate-limits/{charged}"))
                first.control.close()
                policy = {"additional_seconds": 30,
                    "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                    "source_changes": {"mode": "reopen", "reopen_scopes": ["operations"]}}
                resumed = SurveyRunner(project, config, resume_policy=policy)
                self.addCleanup(resumed.control.close)
                self.assertEqual(resumed.api_calls, charged)
                resumed._setup()
                before = resumed.api_calls
                with patch.object(resumed, "_analysis_selection", return_value={"W101"}):
                    resumed._reconcile_identities()
                self.assertEqual(resumed.api_calls, before + 1)
                self.assertIn("W101", resumed.identity_records)
                receipt = resumed._body(resumed.store.head(f"command/identity-rate-limits/{charged}"))
                self.assertEqual(receipt["execution_ref"], execution)
                arguments = {"query": "10.1234/w101", "limit": 3}
                resumed._reserve_api_call("identity", arguments, "research.identity-checker")
                later = resumed.api_calls
                with self.assertRaises(ProviderRateLimitError):
                    resumed._stop_identity_rate_limit(stopped.exception.details, arguments)
                self.assertIsNone(resumed.store.head(f"command/identity-rate-limits/{later}"))
                with self.assertRaises(ValidationError):
                    resumed.gate._recorded_execution(execution, "research.identity-checker",
                        operation="model", task_kinds={"verification"}, allow_blocked_retrieval=True)

    def test_identity_unknown_reservation_still_prohibits_redispatch(self):
        config = survey_config(self.endpoint)
        config["survey"]["identity"] = {
            "id": "identity", "adapter": "crossref",
            "client": {"endpoint": self.endpoint, "timeout": 4, "max_bytes": 1000000,
                       "mailto": "catalog@example.org"},
            "representative": {"query": "10.1234/W101", "limit": 1}, "environment_files": []}
        first = self.runtime(config)
        first._initialize(); first._setup()
        first._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        first._reserve_api_call("identity", {"query": "10.1234/w101", "limit": 3}, "research.identity-checker")
        first.control.close()
        policy = {"additional_seconds": 30,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reopen", "reopen_scopes": ["operations"]}}
        resumed = self.runtime(config, resume_policy=policy)
        resumed._setup()
        before = resumed.api_calls
        with patch.object(resumed, "_analysis_selection", return_value={"W101"}), \
             patch.object(resumed.operations, "run") as dispatch:
            with self.assertRaisesRegex(StateError, "unresolved charged reservation"):
                resumed._reconcile_identities()
        dispatch.assert_not_called()
        self.assertEqual(resumed.api_calls, before)

    def test_owned_top_level_http429_survives_failure_projection_and_readiness(self):
        for field in ("status", "status_code", "http_status", "provider_http_status"):
            for index, retry_after in enumerate((None, 60)):
                runner = SurveyRunner(self.root / f"top-status-{field}-{index}", survey_config(self.endpoint))
                self.addCleanup(runner.control.close)
                runner._initialize()
                response = {"outcome": "provider_error", field: 429, "metadata": {},
                            "rate_limit": {"retry_after_seconds": retry_after}}
                execution = runner._record("command/executions/top-rate", "report", response, "operations.operator")
                failure = runner._record("command/top-rate", "note", {"execution_ref": execution["artifact_ref"]}, "operations.verifier")
                with patch.object(runner.operations, "ensure_ready", return_value={"state": "degraded", "reason": "provider failure"}) as dispatch, \
                     patch.object(runner.operations, "status", return_value={"failure_ref": failure["artifact_ref"]}):
                    with self.assertRaises(ProviderRateLimitError) as stopped:
                        runner._setup()
                self.assertEqual(dispatch.call_count, 1)
                self.assertEqual(stopped.exception.details[field], 429)
                self.assertEqual(stopped.exception.details["rate_limit"], response["rate_limit"])
                self.assertEqual(stopped.exception.details["execution_ref"], execution["artifact_ref"])
                self.assertEqual(runner.bibliography_mode, "openalex")

    def test_legacy_owned_top_level_http429_is_recovered_as_terminal_rate_limit(self):
        runner = self.runtime(self.full_text_config("pass"))
        runner._initialize()
        wid, url = "W401", "https://example.org/W401"
        runner.works[wid] = {"id": wid}
        work = runner._record(f"kb/works/{wid}", "reference_card", {"work_id": wid}, "research.cataloger")
        runner.work_records[wid] = work
        runner._record(f"command/source-attempts/full-text/{wid}-1", "note", {
            "work_id": wid, "url": url, "source_kind": "auto", "status": "reserved"},
            "command.controller", subjects=[work["artifact_ref"]])
        capability = runner.score["full_text"]["id"]
        context = runner._record(f"command/contexts/ops-work-{capability}-rate", "note", {
            "url": url, "source_kind": "auto"}, "research.full-text-reader")
        execution = runner._record(f"command/executions/ops-work-{capability}-rate", "report", {
            "outcome": "provider_error", "status_code": 429, "metadata": {}, "source_url": url},
            "research.full-text-reader", subjects=[context["artifact_ref"]])
        runner._restore_source_access_failures()
        self.assertEqual(len(runner.gaps), 1)
        failure = runner.gaps[0]
        self.assertEqual(failure["outcome"], "rate_limited")
        self.assertEqual(failure["status_code"], 429)
        self.assertEqual(failure["execution_ref"], execution["artifact_ref"])
        with patch.object(runner.operations, "run", side_effect=AssertionError("no source retry")):
            with self.assertRaises(ProviderRateLimitError):
                runner._full_texts()

    def test_legacy_reserved_access_failure_recovers_exact_owned_execution(self):
        config = self.full_text_config("pass", source_url=f"http://127.0.0.1:{self.server.server_port}/denied.pdf")
        runner = self.runtime(config)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W401")
        original = runner._record
        def omit_terminal(logical_id, artifact_type, body, author, **kwargs):
            if body.get("status") == "access_unavailable":
                return runner.store.head(logical_id)
            return original(logical_id, artifact_type, body, author, **kwargs)
        with patch.object(runner, "_record", side_effect=omit_terminal):
            runner._full_texts()
        self.assertEqual(runner._body(runner.store.head("command/source-attempts/full-text/W401-1"))["status"], "reserved")
        failure = deepcopy(runner.gaps[-1])
        runner.gaps = []
        runner._restore_source_access_failures()
        self.assertEqual(runner.gaps, [failure])
        runner._restore_source_access_failures()
        self.assertEqual(runner.gaps, [failure])
        self.assertEqual(runner._source_availability("W401")["evidence_scope"], "abstract")
        runner.full_text_attempted.clear()
        with patch.object(runner.operations, "run", side_effect=AssertionError("retained source attempt must not repeat")):
            runner._full_texts()
        execution = runner.store.get(failure["execution_ref"])
        report = runner._body(execution)
        capability_id = config["survey"]["full_text"]["id"]
        original(f"command/executions/ops-work-{capability_id}-duplicate", "report", report,
                 "research.full-text-reader", subjects=[execution["inputs"][0]["ref"]])
        with self.assertRaisesRegex(ValidationError, "ambiguous terminal executions"):
            runner._restore_source_access_failures()

    def test_access_unavailability_never_fabricates_abstract_or_demotes_verified_full_text(self):
        runner = self.runtime()
        for outcome in ("access_denied", "auth_required", "robots_denied", "robots_unavailable"):
            runner.gaps = [{"kind": "full_text_failure", "work_id": "W1", "outcome": outcome,
                            "source_url": "https://example.org/paper", "execution_ref": "execution"}]
            runner.source_docs = {"bad": {"work_id": "W1", "representation": "unverified_text", "text": "Body"}}
            self.assertEqual(runner._source_availability("W1")["evidence_scope"], "unavailable")
            self.assertEqual(runner._assessment_source_context(), [])
            runner.source_docs["abstract"] = {"work_id": "W1", "representation": "abstract", "text": "Captured abstract."}
            self.assertEqual(runner._source_availability("W1")["evidence_scope"], "abstract")
            runner.source_docs["full"] = {"work_id": "W1", "representation": "full_text", "text": "Methods\nBody",
                                          "identity_verified": True,
                                          "identity_checks": {"title_match": True, "section_markers": ["Methods"]}}
            availability = runner._source_availability("W1")
            self.assertEqual(availability["evidence_scope"], "full_text")
            self.assertEqual(availability["full_text_access"], "available")
            self.assertEqual(availability["verified_full_text_refs"], ["full"])
            self.assertEqual(availability["full_text_failures"][0]["outcome"], outcome)

    def test_robots_denial_does_not_fall_through_to_openalex_pdf(self):
        primary_url = f"http://127.0.0.1:{self.server.server_port}/paper.pdf"
        fallback_url = f"http://127.0.0.1:{self.server.server_port}/fallback.pdf"
        SurveyHTTPFixture.robots_disallow = "/paper.pdf"
        SurveyHTTPFixture.locations_by_work = {"W401": [{
            "is_oa": True, "landing_page_url": "https://example.org/registered-article",
            "pdf_url": fallback_url,
        }]}
        result = self.runtime(self.full_text_config("pass", source_url=primary_url)).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["coverage"]["verified_full_texts"], 0)
        requested_paths = [item["path"] for item in SurveyHTTPFixture.requests]
        self.assertIn("/robots.txt", requested_paths)
        self.assertNotIn("/paper.pdf", requested_paths)
        self.assertNotIn("/fallback.pdf", requested_paths)
        failure = next(item for item in result["coverage"]["access_and_limit_gaps"]
                       if item.get("kind") == "full_text_failure")
        self.assertEqual(failure["outcome"], "robots_denied")

    def test_optional_full_text_failure_preserves_abstract_survey_and_uncertainty(self):
        config = self.full_text_config("pass", source_url="https://example.org/unavailable")
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["gap_state"], "insufficient_evidence")
        self.assertTrue(result["survey_current"])
        self.assertIsNotNone(result["assessment_ref"])
        self.assertEqual(result["coverage"]["abstracts"], 5)
        self.assertEqual(result["coverage"]["verified_full_texts"], 0)
        failures = [gap for gap in result["coverage"]["access_and_limit_gaps"] if gap["kind"] == "full_text_failure"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["work_id"], "W401")
        self.assertEqual(result["usage"]["cumulative_usage"]["retrieval_calls"], 9)
        capability = result["capabilities"]["full-text"]
        self.assertEqual(capability["state"], "idle")
        control, store = self.open_store()
        readiness = json.loads(store.read_body(store.get(capability["verification_ref"])["body_hash"]))
        self.assertEqual(readiness["outcome"], "passed")
        self.assertIsNone(capability["failure_ref"])
        self.assertIsNone(store.head("kb/full-text/W401"))
        assessment = next(prompt for _, prompt in self.model_contexts(control, store) if prompt["phase"] == "gap_assessment")
        self.assertTrue(all(source["representation"] == "abstract" for source in assessment["sources"]))


    def test_crossref_identity_and_stable_source_spans_are_pinned_in_v3_survey(self):
        config = survey_config(self.endpoint)
        config["survey"]["search"]["max_api_calls"] = 30
        config["survey"]["identity"] = {
            "id": "identity", "adapter": "crossref",
            "client": {"endpoint": self.endpoint, "timeout": 4, "max_bytes": 1000000,
                       "mailto": "catalog@example.org"},
            "representative": {"query": "10.1234/W101", "limit": 1},
            "environment_files": []}
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        survey = json.loads(store.read_body(store.get(result["survey_ref"])["body_hash"]))
        self.assertEqual(survey["schema_version"], "literature-survey-3")
        self.assertTrue(survey["identity_refs"])
        SurveyGate(control, store)._bibliographic_identities(survey)
        for ref in survey["identity_refs"]:
            identity = json.loads(store.read_body(store.get(ref)["body_hash"]))
            self.assertEqual(identity["status"], "verified")
        identity = json.loads(store.read_body(store.get(survey["identity_refs"][0])["body_hash"]))
        identity["checks"][1]["crossref"] = "Forged title"
        forged = store.publish_artifact(logical_id="kb/forged-identities/W101", artifact_type="reference_card",
            author="fixture.forgery", body=json.dumps(identity).encode(), media_type="application/json")
        forged_survey = deepcopy(survey)
        forged_survey["identity_refs"] = [forged["artifact_ref"], *survey["identity_refs"][1:]]
        forged_survey["dependency_refs"] = [forged["artifact_ref"] if ref == survey["identity_refs"][0] else ref
                                             for ref in survey["dependency_refs"]]
        with self.assertRaisesRegex(ValidationError, "exact provider executions"):
            SurveyGate(control, store)._bibliographic_identities(forged_survey)
        mapped = json.loads(store.read_body(store.head("kb/work-analyses/W101")["body_hash"]))
        proof = mapped["problem"]["evidence"][0]
        self.assertEqual(set(proof), {"work_id", "source_ref", "quote", "start", "end", "quote_sha256"})
        source = json.loads(store.read_body(store.get(proof["source_ref"])["body_hash"]))
        self.assertEqual(source["text"][proof["start"]:proof["end"]], proof["quote"])

    def test_resume_reconciles_legacy_year_conflict_from_pinned_execution_without_calling_provider(self):
        runner = self.runtime(survey_config(self.endpoint))
        self.addCleanup(runner.control.close)
        work = {
            "work_id": "W1", "doi": "10.1234/example", "title": "Same title",
            "year": 2012, "abstract": "Abstract text.",
        }
        work_record = runner._publish(
            "kb/works/W1", "reference_card", work, "research.seed-searcher")
        result = {
            "outcome": "ok",
            "metadata": {"match_mode": "exact_doi"},
            "sources": [{
                "doi": "10.1234/example", "title": "Same title",
                "published": {"date-parts": [[2013]]},
            }],
        }
        execution = runner._publish(
            "command/executions/identity-fixture", "report", result,
            "research.identity-checker")
        identity = reconcile_result(
            work, work_record["artifact_ref"], result, execution["artifact_ref"])
        legacy = deepcopy(identity)
        legacy["status"] = "conflicted"
        year = next(check for check in legacy["checks"] if check["field"] == "year")
        year["outcome"] = "conflict"
        year.pop("variance_years", None)
        old_record = runner._publish(
            "kb/identities/W1", "reference_card", legacy,
            "research.identity-checker",
            subjects=[work_record["artifact_ref"], execution["artifact_ref"]])
        runner.work_records["W1"] = work_record
        runner.works["W1"] = work
        runner.identity_records["W1"] = old_record
        runner.gaps = [{
            "kind": "bibliographic_identity_conflicted", "work_id": "W1",
            "identity_ref": old_record["artifact_ref"],
        }]
        params = {"query": "10.1234/example", "limit": 3,
                  "client": {"endpoint": self.endpoint, "mailto": "catalog@example.org"}}

        class ValidCrossrefAdapter:
            @staticmethod
            def inspect_result(profile, _result, _params, representative=False):
                if profile.get("client") != params["client"]:
                    raise AssertionError("Identity replay lost its pinned client configuration")
                return ([{"outcome": "passed"}], {})

        with patch.object(runner, "_analysis_selection", return_value={"W1"}), \
                patch.object(runner.gate, "_recorded_execution",
                             return_value=(None, None, result, params)) as recorded, \
                patch("scisaurus.runtime.survey.get_adapter",
                      return_value=ValidCrossrefAdapter), \
                patch.object(runner, "_update_register"):
            runner._reconcile_identities()

        current = runner.identity_records["W1"]
        current_body = runner._body(current)
        self.assertEqual(current_body["status"], "verified_with_gaps")
        self.assertNotEqual(current["artifact_ref"], old_record["artifact_ref"])
        self.assertEqual(runner._body(old_record)["status"], "conflicted")
        self.assertEqual(runner.api_calls, 0)
        self.assertEqual(runner.gaps, [])
        recorded.assert_called_once()

    def test_resume_charges_api_reservations_even_without_provider_results(self):
        config = survey_config(self.endpoint)
        first = self.runtime(config)
        first._initialize()
        # Simulate a legacy run whose successful calls predate reservation
        # artifacts. New reservations must carry the cumulative baseline.
        first.api_calls = 10
        first.identity_calls = 3
        first._reserve_api_call("bibliography", {"operation": "search", "query": "failed"},
                                "research.search-planner")
        first._reserve_api_call("identity", {"query": "10.1234/unknown", "limit": 3},
                                "research.identity-checker")
        first.control.close()

        policy = {
            "additional_seconds": 20,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reject", "reopen_scopes": []},
        }
        resumed = SurveyRunner(self.root / "run", config, resume_policy=policy)
        self.addCleanup(resumed.control.close)
        self.assertEqual(resumed.api_calls, 12)
        self.assertEqual(resumed.identity_calls, 4)
        reservations = [resumed._body(record) for record in resumed._heads("command/api-calls/")]
        self.assertEqual([row["number"] for row in reservations], [11, 12])
        self.assertEqual([row["identity_number"] for row in reservations], [3, 4])
        self.assertEqual([row["capability"] for row in reservations], ["bibliography", "identity"])

    def test_resume_reuses_completed_blind_search_plans_before_any_work_was_captured(self):
        config = survey_config(self.endpoint)
        del config["survey"]["search"]["challenge_reserve"]
        first = self.runtime(config)
        first._initialize()
        original = first._initial_plans()
        task_count = first.control._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        first.control.close()

        policy = {
            "additional_seconds": 20,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reject", "reopen_scopes": []},
        }
        resumed = SurveyRunner(self.root / "run", config, resume_policy=policy)
        self.addCleanup(resumed.control.close)
        resumed.worker_target = lambda *_: (_ for _ in ()).throw(AssertionError("retained plans dispatched again"))
        retained = resumed._initial_plans()
        self.assertEqual(retained, original)
        self.assertEqual(resumed.control._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], task_count)
        self.assertEqual(resumed.time_decisions, [])

    def test_failed_first_planner_keeps_successful_sibling_on_resume(self):
        from scisaurus.runtime.literature import ProviderCooldownError
        config = survey_config(self.endpoint)
        first = self.runtime(config)
        first._initialize()
        first._complete = lambda task_id: None
        def partial(specs, **kwargs):
            good = specs[1]
            execution = first._publish("command/executions/fixture-plan", "report", {}, good["actor"])
            return {specs[0]["task_id"]: {"ok": False, "error": "quota exhausted", "status_code": 429,
                    "retry_after_seconds": 60}, good["task_id"]: {
                    "ok": True, "record_ref": execution["artifact_ref"], "result": {
                    "text": json.dumps({"queries": ["recall timing"], "rationale": "different terminology"}),
                    "model": "fake", "usage": {"model_calls": 1}, "elapsed_seconds": 0.01, "finish_reason": "stop"}}}
        with patch.object(first, "_call_batch", side_effect=partial):
            with self.assertRaises(ProviderCooldownError):
                first._initial_plans()
        retained = first._heads("kb/search-plans/")
        self.assertEqual(len(retained), 1)
        first.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        resumed = SurveyRunner(self.root / "run", config, resume_policy=policy)
        self.addCleanup(resumed.control.close)
        def inspect(jobs, **kwargs):
            self.assertEqual(len(jobs), 1)
            self.assertNotEqual(jobs[0]["actor"], retained[0]["author"])
        with patch.object(resumed, "_models_checked", side_effect=inspect):
            self.assertEqual(len(resumed._initial_plans()), 1)

    def test_schema_repair_allowance_is_durable_even_in_until_deadline_mode(self):
        from scisaurus.runtime.model_work import ModelWorkBlocked
        config = survey_config(self.endpoint)
        config["limits"].update(max_rounds=2, repair_mode="until_deadline")
        first = self.runtime(config)
        first._initialize()
        job = {"name": "invalid-plan", "actor": "research.search-planner",
               "assignment": {"phase": "blind_plan"}, "validator": first._plan_validator}
        # The valid fixture plan intentionally violates this assignment's validator.
        def reject(value):
            raise ValidationError("unresolved schema")
        job["validator"] = reject
        with self.assertRaises(ModelWorkBlocked):
            first._models_checked([job])
        count = first.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        self.assertEqual(count, 2)
        with patch.object(first, "_call_batch") as call:
            with self.assertRaises(ModelWorkBlocked):
                first._models_checked([job])
            call.assert_not_called()
        first.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        resumed = SurveyRunner(self.root / "run", config, resume_policy=policy)
        self.addCleanup(resumed.control.close)
        resumed.worker_target = simulated_survey_worker
        with self.assertRaises(ModelWorkBlocked):
            resumed._models_checked([job])
        self.assertEqual(resumed.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 4)
        with patch.object(resumed, "_call_batch") as call:
            with self.assertRaises(ModelWorkBlocked):
                resumed._models_checked([job])
            call.assert_not_called()
        fresh_job = {"name": "invalid-plan", "actor": "research.search-planner",
                     "assignment": {"phase": "blind_plan"}, "validator": reject}
        with patch.object(resumed, "_call_batch") as call:
            with self.assertRaises(ModelWorkBlocked):
                resumed._models_checked([fresh_job])
            call.assert_not_called()

    def test_repair_echo_is_bounded_without_truncating_source_assignment(self):
        from scisaurus.runtime.models import estimate_input_tokens
        from scisaurus.runtime.execution import SYSTEM
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        job = {"name": "scoped", "actor": "research.literature-mapper", "assignment": {"source": "exact evidence"}}
        with patch.object(runner, "_map_input_limit", return_value=2000):
            projected = runner._repair_assignment(job, {"error": "missing field", "previous_response": "x" * 20000})
        self.assertEqual(projected["source"], "exact evidence")
        self.assertTrue(projected["validation_feedback"]["previous_response_omitted"])
        self.assertLessEqual(estimate_input_tokens(SYSTEM, json.dumps(projected, ensure_ascii=False)), 2000)

    def test_output_exhaustion_repair_omits_reasoning_without_changing_evidence(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        job = {"name": "scoped", "actor": "methods.novelty-verifier",
               "assignment": {"sources": [{"text": "exact evidence"}], "evidence_catalog": []}}
        feedback = {"error": "generation length", "finish_reason": "length",
                    "previous_response": {"raw_text": "Unfinished reasoning. " * 1000}}
        with patch.object(runner, "_map_input_limit", return_value=None):
            projected = runner._repair_assignment(job, feedback)
        self.assertEqual(projected["sources"], job["assignment"]["sources"])
        self.assertNotIn("previous_response", projected["validation_feedback"])
        self.assertTrue(projected["validation_feedback"]["previous_response_omitted"])
        self.assertIn("final JSON object", projected["validation_feedback"]["scope"])
        self.assertIn("previous_response", feedback)

    def test_gap_assessment_repair_projects_to_catalog_and_omits_transcript(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        assignment = {
            "assignment": "Assess the bounded gap.", "phase": "gap_assessment",
            "question": "Does the proposed mechanism survive prior-work challenge?",
            "gap": {"id": "bounded-gap", "statement": "A testable unresolved distinction."},
            "nomination_ref": "artifact:kb/gap-nomination@1",
            "survey_ref": "artifact:kb/surveys/current@1",
            "prerequisite_survey_ref": "artifact:kb/surveys/current@1",
            "map": {"entries": [{"work_id": "W1", "inclusion": "included",
                                  "reason": "A captured claim.",
                                  "problem": {"text": "A claim", "evidence": [{
                                      "work_id": "W1", "source_ref": "artifact:kb/abstracts/W1@1",
                                      "quote": "Exact captured quote"}]}}],
                    "relationships": []},
            "coverage": {"unique_works": 1, "abstracts": 1, "verified_full_texts": 0,
                         "searches": [{"request": {"query": "bounded gap"}, "outcome": "ok"}],
                         "source_windows": []},
            "sources": [{"source_ref": "artifact:kb/abstracts/W1@1", "work_id": "W1",
                         "representation": "abstract", "available_chars": 20}],
            "verified_full_text_refs": [],
            "evidence_catalog": [{"evidence_id": "ev-1", "work_id": "W1",
                                   "source_ref": "artifact:kb/abstracts/W1@1",
                                   "quote": "Exact captured quote", "start": 0, "end": 20}],
            "required_checks": ["closest-prior-work", "scope-comparability",
                                 "counterevidence", "full-text-support"],
            "allowed_check_outcomes": ["passed", "failed", "insufficient_evidence", "check_failed"],
        }
        feedback = {"error": "comparison must cite its own work", "finish_reason": "length",
                    "previous_response": {"raw_text": "reasoning " * 5000}}
        projected = runner._repair_assignment(
            {"name": "gap-assessment", "actor": "methods.novelty-verifier",
             "assignment": assignment}, feedback)
        self.assertIn("evidence_catalog", projected)
        self.assertEqual(projected["claim_index"]["entries"][0]["evidence_by_field"], {"problem": ["ev-1"]})
        self.assertNotIn("map", projected)
        self.assertNotIn("raw_text", json.dumps(projected))
        self.assertNotIn("previous_response", projected["validation_feedback"])
        self.assertIn("same work_id", projected["instructions"])
        from scisaurus.runtime.evidence import scientific_input_recovery_contract
        self.assertEqual(projected["scientific_input_recovery"], scientific_input_recovery_contract())
        self.assertEqual(projected["claim_index"]["entries"][0]["statements"], {"problem": "A claim"})
        from scisaurus.runtime.survey import _GAP_ASSESSMENT_INSTRUCTIONS
        self.assertTrue(projected["instructions"].startswith(_GAP_ASSESSMENT_INSTRUCTIONS))
        for token in ("state:string", "rationale:string", "comparisons:", "checks:",
                      "evidence:", "refuted_by_prior_work", "eligible_for_experiment",
                      "solves/partial/different/uncertain"):
            self.assertIn(token, projected["instructions"])

    def test_gap_compaction_preserves_contract_and_current_claims_on_repeated_repairs(self):
        from copy import deepcopy
        from scisaurus.runtime.survey import _GAP_ASSESSMENT_INSTRUCTIONS
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        assignment = {
            "phase": "gap_assessment", "instructions": _GAP_ASSESSMENT_INSTRUCTIONS + " Scoped requirement.",
            "map": {"entries": [{"work_id": "W1", "inclusion": "included", "reason": "Bounded",
                                  "finding": {"text": "An explicitly bounded current finding", "evidence": []}}],
                    "relationships": [{"source": "W1", "target": "W2", "kind": "extends",
                                       "claim": {"text": "A current relationship", "evidence": []}}]},
            "evidence_catalog": [], "sources": [], "coverage": {},
            "required_checks": list(GAP_CHECKS), "allowed_check_outcomes": ["passed", "insufficient_evidence"],
            "resume_boundary": "gap-assessment-resume-8",
        }
        original = deepcopy(assignment)
        projected = runner._compact_gap_repair_assignment(assignment)
        repaired = runner._repair_assignment(
            {"name": "gap-assessment", "actor": "methods.novelty-verifier", "assignment": projected},
            {"error": "invalid JSON", "finish_reason": "length", "previous_response": {"raw_text": "unfinished"}})
        self.assertEqual(assignment, original)
        self.assertTrue(repaired["instructions"].startswith(original["instructions"]))
        self.assertEqual(repaired["required_checks"], original["required_checks"])
        self.assertEqual(repaired["allowed_check_outcomes"], original["allowed_check_outcomes"])
        self.assertEqual(repaired["resume_boundary"], original["resume_boundary"])
        self.assertEqual(repaired["claim_index"], projected["claim_index"])
        self.assertEqual(repaired["claim_index"]["relationships"][0]["claim"], "A current relationship")
        self.assertNotIn("previous_response", repaired["validation_feedback"])

    def test_retained_contract_blocker_gets_one_fresh_resume_cache_identity(self):
        from scisaurus.runtime.model_work import ModelWorkCache
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize()
        runner._complete = lambda task_id: None
        runner.resume_session = {"session": 7}
        job = {"name": "gap-assessment", "actor": "methods.novelty-verifier",
               "assignment": {"phase": "gap_assessment", "question": "bounded",
                              "gap": {"id": "g", "statement": "test"},
                              "evidence_catalog": [], "map": {"entries": [], "relationships": []},
                              "coverage": {}, "sources": [], "required_checks": [],
                              "allowed_check_outcomes": []},
               "validator": lambda value: None}
        job["assignment"] = runner._follow_up_assignment(job["assignment"])
        model = {**runner.config["model"]}
        cache = ModelWorkCache(runner.store, runner._publish)
        key = cache.key(scope="survey:gap-assessment", role=job["actor"], system=SYSTEM,
                        prompt=job["assignment"], model=model)
        cache.put(key, {"status": "blocked", "repair_attempts": 2,
                        "error": "gap-assessment did not satisfy its evidence contract: model output must contain valid JSON",
                        "feedback": {"error": "model output must contain valid JSON",
                                     "finish_reason": "length"}})
        calls = []

        def fake_call(specs, **kwargs):
            calls.append(json.loads(specs[0]["params"]["prompt"]))
            execution = runner._publish("command/executions/resume-repair", "report", {},
                                        "methods.novelty-verifier")
            return {specs[0]["task_id"]: {"ok": True, "record_ref": execution["artifact_ref"],
                    "result": {"text": '{"ok": true}', "model": "fixture",
                               "usage": {"model_calls": 1}, "elapsed_seconds": 0.01,
                               "finish_reason": "stop"}}}

        with patch.object(runner, "_call_batch", side_effect=fake_call):
            result = runner._models_checked([job])
            runner._models_checked([job])
        self.assertEqual(result["gap-assessment"][0]["ok"], True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["_contract_repair_boundary"], "model-contract-repair-7")
        self.assertEqual(calls[0]["source_evidence_policy"], job["assignment"]["source_evidence_policy"])

    def test_legacy_assignment_without_source_policy_does_not_alias_current_review_cache(self):
        runner = self.runtime()
        runner._initialize()
        runner._complete = lambda task_id: None
        runner.resume_session = {"session": 7}
        job = {"name": "gap-assessment", "actor": "methods.novelty-verifier",
               "assignment": {"phase": "gap_assessment", "sources": []},
               "validator": lambda value: None}
        cache = ModelWorkCache(runner.store, runner._publish)
        legacy_key = cache.key(scope="survey:gap-assessment", role=job["actor"], system=SYSTEM,
                               prompt=job["assignment"], model=runner.config["model"])
        cache.put(legacy_key, {"status": "blocked", "failure_class": "model_contract",
                              "error": "Old source-policy assignment failed its contract."})
        calls = []
        def fake_call(specs, **kwargs):
            calls.append(json.loads(specs[0]["params"]["prompt"]))
            execution = runner._publish("command/executions/current-source-policy", "report", {}, job["actor"])
            return {specs[0]["task_id"]: {"ok": True, "record_ref": execution["artifact_ref"],
                    "result": {"text": '{"ok": true}', "model": "fixture", "usage": {"model_calls": 1},
                               "elapsed_seconds": 0.01, "finish_reason": "stop"}}}
        with patch.object(runner, "_call_batch", side_effect=fake_call):
            runner._models_checked([job])
            runner._models_checked([job])
        self.assertEqual(len(calls), 1)
        self.assertIn("source_evidence_policy", calls[0])
        self.assertNotIn("_contract_repair_boundary", calls[0])
        self.assertEqual(cache.get(legacy_key)["status"], "blocked")

    def test_rate_limit_pauses_partial_survey_and_explicit_resume_retains_prior_search(self):
        config = survey_config(self.endpoint)
        config["survey"]["seed_queries"] = ["recall timing", "rate limited topic"]
        config["survey"]["search"]["queries_per_role"] = 2
        config["survey"]["search"]["expansion_rounds"] = 0
        # Explicit recovery preserves the successful query and the failed
        # physical request; neither is an empty search or a repair attempt.
        config["survey"]["bibliography"]["client"]["max_retries"] = 0
        SurveyHTTPFixture.rate_limit_once = "rate limited topic"
        first = self.runtime(config).run()
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["failure"]["kind"], "provider_rate_limit")
        self.assertEqual(first["coverage"]["unique_works"], 1)
        self.assertIn("provider recovery", first["error"])
        self.assertEqual(first["failure"]["provider"], "openalex")
        self.assertEqual(first["failure"]["details"]["metadata"]["http_status"], 429)
        first_searches = [request["query"].get("search", [None])[0]
                          for request in SurveyHTTPFixture.requests
                          if request["path"] == "/works"]
        self.assertEqual(first_searches.count("recall timing"), 1)
        self.assertEqual(first_searches.count("rate limited topic"), 1)

        policy = {
            "additional_seconds": 40,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reject", "reopen_scopes": []},
        }
        completed = self.runtime(config, resume_policy=policy).run()
        self.assertEqual(completed["status"], "completed", completed)
        successful = [row["request"]["query"] for row in completed["coverage"]["searches"]
                      if row["request"]["operation"] == "search"]
        self.assertEqual(successful.count("recall timing"), 1)
        self.assertEqual(successful.count("rate limited topic"), 1)
        control, store = self.open_store()
        blind_prompts = [prompt for _, prompt in self.model_contexts(control, store)
                         if prompt["phase"] == "blind_plan"]
        self.assertEqual(len(blind_prompts), 2)
        reservations = [json.loads(store.read_body(record[0])) for record in control._conn.execute(
            "SELECT body_hash FROM artifacts WHERE logical_id LIKE 'command/api-calls/%' ORDER BY version"
        ).fetchall()]
        failed = [row for row in reservations if row["request"].get("query") == "rate limited topic"]
        self.assertEqual(len(failed), 2)

    def test_resume_after_nomination_still_executes_countersearch(self):
        config = survey_config(self.endpoint)
        with patch.object(SurveyRunner, "_countersearch", side_effect=KeyboardInterrupt("fixture stop")):
            first = self.runtime(config).run()
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["failure"], {"kind": "process_interrupted"})
        self.assertIsNotNone(first["nomination_ref"])
        self.assertFalse(any(request["query"].get("search") == ["prior solution"]
                             for request in SurveyHTTPFixture.requests))

        policy = {
            "additional_seconds": 40,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reject", "reopen_scopes": []},
        }
        completed = self.runtime(config, resume_policy=policy).run()
        self.assertEqual(completed["status"], "completed", completed)
        self.assertEqual(sum(request["query"].get("search") == ["prior solution"]
                             for request in SurveyHTTPFixture.requests), 1)

    def test_resume_after_counter_query_reaccepts_before_assessment_without_repeating_query(self):
        config = survey_config(self.endpoint)
        original = SurveyRunner._accept_survey

        def stop_before_post_challenge_acceptance(runner):
            if (runner.nomination is not None and any(
                    row.get("role") == "methods.novelty-challenger" for row in runner.search_log)):
                raise KeyboardInterrupt("fixture stop after challenge capture")
            return original(runner)

        with patch.object(SurveyRunner, "_accept_survey", stop_before_post_challenge_acceptance):
            first = self.runtime(config).run()
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["failure"], {"kind": "process_interrupted"})
        self.assertFalse(first["survey_current"])
        self.assertEqual(sum(request["query"].get("search") == ["prior solution"]
                             for request in SurveyHTTPFixture.requests), 1)

        policy = {
            "additional_seconds": 40,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reject", "reopen_scopes": []},
        }
        completed = self.runtime(config, resume_policy=policy).run()
        self.assertEqual(completed["status"], "completed", completed)
        self.assertTrue(completed["survey_current"])
        self.assertTrue(completed["assessment_current"])
        self.assertEqual(sum(request["query"].get("search") == ["prior solution"]
                             for request in SurveyHTTPFixture.requests), 1)
        control, store = self.open_store()
        counter_plans = [prompt for _, prompt in self.model_contexts(control, store)
                         if prompt["phase"] == "counter_plan"]
        self.assertEqual(len(counter_plans), 1)
        plan = json.loads(store.read_body(store.head("kb/counter-search-plan")["body_hash"]))
        self.assertEqual(plan["nomination_ref"], completed["nomination_ref"])


class TestSurveyContracts(unittest.TestCase):
    def test_completion_context_retains_repaired_evidence_catalog(self):
        from scisaurus.runtime.survey_records import follow_up_completion_context
        assignment = {key: [] for key in ("sources", "query_refs", "searches")}
        assignment.update(question="Retained question", assessment={}, survey_inventory={},
                          evidence_catalog=[{"evidence_id": "ev-1", "quote": "Exact captured quote"}])
        context = follow_up_completion_context(assignment)
        self.assertEqual(context["evidence_catalog"], assignment["evidence_catalog"])
        context["evidence_catalog"][0]["quote"] = "Different quote"
        self.assertEqual(assignment["evidence_catalog"][0]["quote"], "Exact captured quote")

    def test_map_normalizer_binds_positioned_relationship_evidence_without_digests(self):
        texts = {'W101': 'The measured rate increased.', 'W102': 'The control rate stayed constant.'}
        sources = [{'work_id': wid, 'source_ref': 'source-' + wid, 'text': text, 'representation': 'abstract'}
                   for wid, text in texts.items()]
        proofs = [{'work_id': row['work_id'], 'source_ref': row['source_ref'], 'quote': row['text'],
                   'start': 0, 'end': len(row['text'])} for row in sources]
        entry = {'work_id': 'W101', 'inclusion': 'included', 'reason': 'A measured comparison.',
                 **{field: {'text': None, 'evidence': []} for field in MAP_FIELDS}}
        value = {'entries': [entry], 'relationships': [{'source': 'W101', 'target': 'W102',
            'kind': 'compares', 'claim': {'text': 'The reported rate responses differ.', 'evidence': proofs}}]}
        normalized = normalize_map_worker_response(value, work_id='W101', all_work_ids=set(texts),
            sources=sources, windows={})
        self.assertEqual(len(normalized['relationships']), 1)
        evidence = normalized['relationships'][0]['claim']['evidence']
        for original, bound in zip(proofs, evidence):
            self.assertEqual({key: bound[key] for key in original}, original)
            self.assertIn('quote_sha256', bound)
            self.assertNotIn('quote_sha256', original)
        validate_map(normalized, ['W101'], set(texts),
                     {row['source_ref']: row for row in sources}, require_spans=True)

    def test_operation_completion_is_separate_from_evidence_sufficiency(self):
        from scisaurus.runtime.survey_records import validate_follow_up_result, follow_up_completion_met
        from scisaurus.core.errors import ModelContractError
        order = {"id": "inputs", "success_condition": "Capture the value or record it as unavailable."}
        row = {"id": "inputs", "status": "unresolved", "rationale": "No captured measurement.",
               "evidence": [], "query_refs": ["query-1"], "limitation": "The measurement is unavailable.",
               "next_action": "Design a measurement.",
               "completion": {"outcome": "met", "rationale": "The permitted unavailable outcome is recorded."}}
        def validate(item, *, required=True):
            return validate_follow_up_result({"orders": [item]}, [order], {}, ["query-1"],
                                             windows={}, require_completion=required)
        validate(row)
        self.assertTrue(follow_up_completion_met(row, require_resolved=True))
        self.assertEqual(row["status"], "unresolved")
        unmet = {**row, "status": "limited", "completion": {
            "outcome": "unmet", "rationale": "A capture-required measurement is still missing."}}
        validate(unmet)
        self.assertFalse(follow_up_completion_met(unmet))
        legacy = {key: value for key, value in unmet.items() if key != "completion"}
        validate(legacy, required=False)
        self.assertTrue(follow_up_completion_met(legacy))
        self.assertFalse(follow_up_completion_met(legacy, require_resolved=True))
        with self.assertRaises(ModelContractError):
            validate(legacy)
        for outcome in ([], {}, None, True, "unknown", ""):
            with self.subTest(outcome=outcome), self.assertRaises(ModelContractError):
                validate({**row, "completion": {"outcome": outcome, "rationale": "Invalid outcome."}})
        for changes in ({"query_refs": []}, {"limitation": ""}, {"query_refs": ["foreign-query"]},
                        {"completion": {"outcome": "met", "rationale": ""}}):
            with self.subTest(changes=changes), self.assertRaises(ModelContractError):
                validate({**row, **changes})

    def test_nomination_identifier_normalization_preserves_the_hypothesis(self):
        statement = "A bounded model comparison remains unresolved."
        for label in ("Gap With Spaces", "field_0to1T", "x" * 100, "123"):
            raw = {"id": label, "statement": statement}
            result = normalize_gap_nomination(raw)
            from scisaurus.runtime.scores import identifier
            identifier(result["id"])
            self.assertEqual(result["statement"], statement)
            self.assertEqual(raw["id"], label)
            self.assertEqual(result, normalize_gap_nomination(raw))
            self.assertNotEqual(result["id"], normalize_gap_nomination({**raw, "statement": statement + " Different."})["id"])
        valid = {"id": "bounded-gap", "statement": statement}
        self.assertIs(normalize_gap_nomination(valid), valid)
        for invalid in ({"id": 3, "statement": statement}, {"id": "", "statement": statement},
                        {"id": "INVALID", "statement": ""}, {**valid, "decision": "accepted"}):
            self.assertIs(normalize_gap_nomination(invalid), invalid)

    def test_exact_relationship_target_grant_routes_to_owner_with_empty_entry_authority(self):
        entries = {"W1": "artifact:kb/entry/W1@1", "W2": "artifact:kb/entry/W2@1"}
        ref = "artifact:kb/relationship/W1-W2@1"
        relations = {ref: {"source": "W1", "target": "W2"}}
        raw = {"repairs": [{"entry_ref": ref, "relationship_refs": [ref], "rationale": "Inspect this relationship."}]}
        original = deepcopy(raw)
        result = normalize_survey_repair_owners(raw, entries, relations)
        self.assertEqual(result, {"repairs": [{"entry_ref": entries["W1"], "entry_fields": [],
            "relationship_refs": [ref], "rationale": "Inspect this relationship."}]})
        self.assertEqual(raw, original)
        self.assertEqual(normalize_survey_repair_owners(result, entries, relations), result)
        for changes in ({"entry_ref": ref.replace("@1", "@2")}, {"entry_fields": ["reason"]},
                        {"relationship_refs": []}, {"relationship_refs": None}):
            invalid = {"repairs": [{**raw["repairs"][0], **changes}]}
            self.assertIs(normalize_survey_repair_owners(invalid, entries, relations), invalid)

    def test_typed_critique_rejection_is_separate_from_claim_admission(self):
        obligations = [{"work_id": "W1", "hypothesis": "The current finding is unsupported."}]
        required = work_review_checks([], obligations)
        raw = {"checks": check_rows(work_review_checks([])), "rationale": "The hypothesis is rejected on source evidence.",
               "critique_adjudications": [{"check_id": required[-1], "disposition": "rejected",
                   "method": "Compare the current claim with captured evidence.",
                   "result": "The captured source supports the current finding; the allegation is not corroborated.",
                   "affected_check_ids": []}]}
        original = deepcopy(raw)
        normalized = normalize_check_envelope(raw, required)
        validate_work_review(normalized, [], review_obligations=obligations)
        self.assertEqual(normalized["checks"][-1]["outcome"], "passed")
        self.assertEqual(normalized["checks"][-1]["result"], raw["critique_adjudications"][0]["result"])
        self.assertEqual(raw, original)
        for disposition in ("corrected", "nonassertion"):
            raw["critique_adjudications"][0]["disposition"] = disposition
            validate_work_review(normalize_check_envelope(raw, required), [], review_obligations=obligations)

    def test_typed_critique_defects_require_explicit_current_failure_links(self):
        obligations = [{"work_id": "W1", "hypothesis": "The finding may omit its temperature."}]
        required = work_review_checks([], obligations)
        raw = {"checks": check_rows(work_review_checks([])), "rationale": "Check the missing condition.",
               "critique_adjudications": [{"check_id": required[-1], "disposition": "current_defect",
                   "method": "Inspect the measured source conditions.", "result": "The finding omits temperature.",
                   "affected_check_ids": ["finding"]}]}
        with self.assertRaises(ValidationError):
            validate_work_review(normalize_check_envelope(raw, required), [], review_obligations=obligations)
        next(row for row in raw["checks"] if row["check_id"] == "finding")["outcome"] = "insufficient_evidence"
        normalized = normalize_check_envelope(raw, required)
        validate_work_review(normalized, [], review_obligations=obligations)
        self.assertEqual(normalized["checks"][-1]["outcome"], "failed")
        for mutation in ("unknown", "missing", "duplicate", "mixed", "unlinked", "false_resolved"):
            value = deepcopy(raw)
            row = value["critique_adjudications"][0]
            if mutation == "unknown": row["disposition"] = "unsupported-alias"
            elif mutation == "missing": value["critique_adjudications"] = []
            elif mutation == "duplicate": value["critique_adjudications"].append(deepcopy(row))
            elif mutation == "mixed": value["checks"].append(check_rows([required[-1]])[0])
            elif mutation == "unlinked": row["affected_check_ids"] = []
            else: row["disposition"] = "rejected"
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                validate_work_review(normalize_check_envelope(value, required), [], review_obligations=obligations)

    def test_incomplete_critique_adjudications_name_exact_missing_ids(self):
        obligations = [{"work_id": "W1", "hypothesis": hypothesis}
                       for hypothesis in ("The result may omit temperature.", "The uncertainty meaning may be omitted.")]
        required = work_review_checks([], obligations)
        raw = {"checks": check_rows(work_review_checks([])), "rationale": "Check both allegations.",
               "critique_adjudications": [{"check_id": required[-2], "disposition": "rejected",
                   "method": "Inspect the source and current entry.", "result": "No current defect confirmed.",
                   "affected_check_ids": []}]}
        original = deepcopy(raw)
        with self.assertRaises(ModelContractError) as rejected:
            normalize_check_envelope(raw, required)
        self.assertIn("critique_adjudications", str(rejected.exception))
        self.assertIn(required[-1], str(rejected.exception))
        self.assertIn("affected_check_ids", str(rejected.exception))
        self.assertEqual(raw, original)

    def test_repair_entry_work_id_resolves_only_to_supplied_current_version(self):
        entries = {"W1": "artifact:kb/entry/W1@3"}
        raw = {"repairs": [{"entry_ref": "W1", "entry_fields": ["reason"], "relationship_refs": [], "rationale": "Narrow the current reason."}]}
        result = normalize_survey_repair_owners(raw, entries, {})
        self.assertEqual(result["repairs"][0]["entry_ref"], entries["W1"])
        self.assertEqual(result["repairs"][0]["entry_fields"], ["reason"])
        stale = {"repairs": [{**raw["repairs"][0], "entry_ref": "artifact:kb/entry/W1@2"}]}
        self.assertIs(normalize_survey_repair_owners(stale, entries, {}), stale)
        duplicate = {"repairs": [raw["repairs"][0], {**raw["repairs"][0], "entry_ref": entries["W1"]}]}
        self.assertEqual(normalize_survey_repair_owners(duplicate, entries, {}), result)
    def test_relationship_repair_routes_by_source_without_moving_entry_fields(self):
        entries = {"W1": "artifact:kb/entry/W1@1", "W2": "artifact:kb/entry/W2@1"}
        ref = "artifact:kb/relationship/W1-W2@1"
        relations = {ref: {"source": "W1", "target": "W2"}}
        raw = {"repairs": [{"entry_ref": entries["W2"], "entry_fields": ["finding"],
                            "relationship_refs": [ref], "rationale": "Narrow the supported scopes."}]}
        repaired = normalize_survey_repair_owners(raw, entries, relations)
        grants = {item["entry_ref"]: item for item in repaired["repairs"]}
        self.assertEqual(grants[entries["W1"]]["relationship_refs"], [ref])
        self.assertEqual(grants[entries["W1"]]["entry_fields"], [])
        self.assertEqual(grants[entries["W2"]]["entry_fields"], ["finding"])
        self.assertEqual(grants[entries["W2"]]["relationship_refs"], [])
        self.assertEqual(normalize_survey_repair_owners(repaired, entries, relations), repaired)
        self.assertEqual(raw["repairs"][0]["entry_ref"], entries["W2"])

    def test_relationship_owner_normalization_does_not_repair_stale_or_unknown_grants(self):
        entries = {"W1": "artifact:kb/entry/W1@1", "W2": "artifact:kb/entry/W2@1"}
        ref = "artifact:kb/relationship/W1-W2@1"
        relations = {ref: {"source": "W1", "target": "W2"}}
        item = {"entry_ref": entries["W2"], "entry_fields": [], "relationship_refs": [ref], "rationale": "Inspect the relationship."}
        for raw in ({"repairs": [{**item, "relationship_refs": [ref.replace("@1", "@2")]}]},
                    {"repairs": [{**item, "entry_ref": entries["W2"].replace("@1", "@2")}]},
                    {"repairs": [{**item, "entry_fields": ["unsupported"]}]}):
            self.assertIs(normalize_survey_repair_owners(raw, entries, relations), raw)

    def test_repeated_exact_relationship_permissions_are_unioned_by_current_owner(self):
        from copy import deepcopy
        entries = {"W1": "artifact:kb/entry/W1@1", "W2": "artifact:kb/entry/W2@1"}
        ref = "artifact:kb/relationship/W1-W2@1"
        relations = {ref: {"source": "W1", "target": "W2"}}
        raw = {"repairs": [
            {"entry_ref": entries["W2"], "entry_fields": ["reason"], "relationship_refs": [ref], "rationale": "Target scope."},
            {"entry_ref": entries["W1"], "entry_fields": ["finding"], "relationship_refs": [ref], "rationale": "Source scope."},
        ]}
        original = deepcopy(raw)
        result = normalize_survey_repair_owners(raw, entries, relations)
        grants = {row["entry_ref"]: row for row in result["repairs"]}
        self.assertEqual(grants[entries["W1"]]["relationship_refs"], [ref])
        self.assertEqual(grants[entries["W1"]]["entry_fields"], ["finding"])
        self.assertEqual(grants[entries["W2"]]["relationship_refs"], [])
        self.assertEqual(grants[entries["W2"]]["entry_fields"], ["reason"])
        self.assertIn("Target scope.", grants[entries["W1"]]["rationale"])
        self.assertIn("Source scope.", grants[entries["W1"]]["rationale"])
        self.assertEqual(normalize_survey_repair_owners(result, entries, relations), result)
        self.assertEqual(raw, original)

    def test_critique_checks_cannot_be_omitted_or_replace_narrow_failure_fields(self):
        obligations = [{"work_id": "W1", "hypothesis": "A current numerical claim may omit its conditions."}]
        required = work_review_checks([], obligations)
        value = {"checks": check_rows(work_review_checks([])), "rationale": "All ordinary fields checked."}
        with self.assertRaises(ValidationError):
            validate_work_review(value, [], review_obligations=obligations)
        value["checks"] = check_rows(required)
        value["checks"][-1].update(outcome="failed", result="A current finding omits source conditions.",
                                   affected_check_ids=["finding"])
        with self.assertRaisesRegex(ModelContractError, "affected current entry field"):
            validate_work_review(value, [], review_obligations=obligations)
        next(row for row in value["checks"] if row["check_id"] == "finding")["outcome"] = "failed"
        validate_work_review(value, [], review_obligations=obligations)

    def test_passed_critique_empty_links_are_canonical_without_changing_verdicts(self):
        obligation = {"receipt_ref": "artifact:command/critique/captured@1", "receipt_body_sha256": "a" * 64,
                      "work_id": "W101", "entry_ref": "artifact:kb/work-analyses/W101@1",
                      "entry_body_sha256": "b" * 64, "relationship_pins": [], "source_pins": [],
                      "hypothesis": "Check the retained assertion."}
        required = work_review_checks([], [obligation])
        value = {"checks": check_rows(required), "rationale": "All current assertions are supported."}
        critique = value["checks"][-1]
        critique.pop("affected_check_ids")
        before = deepcopy(value)
        normalized = normalize_check_envelope(value, required)
        validate_work_review(normalized, [], review_obligations=[obligation])
        self.assertEqual(value, before)
        self.assertEqual(normalized["checks"][-1], {**critique, "affected_check_ids": []})
        self.assertEqual(normalize_check_envelope(normalized, required), normalized)
        critique["outcome"] = "failed"
        with self.assertRaises(ValidationError):
            validate_work_review(normalize_check_envelope(value, required), [], review_obligations=[obligation])
        critique.update(outcome="passed", affected_check_ids=["reason"])
        with self.assertRaises(ValidationError):
            validate_work_review(normalize_check_envelope(value, required), [], review_obligations=[obligation])
        missing = {"checks": before["checks"][1:], "rationale": before["rationale"]}
        self.assertIs(normalize_check_envelope(missing, required), missing)

    def test_unresolved_critique_links_project_only_explicit_nonpassed_targets(self):
        from scisaurus.runtime.survey_records import normalize_check_envelope
        obligations = [{"work_id": "W1", "hypothesis": "Assess the current screening decision."}]
        required = work_review_checks([], obligations)
        value = {"checks": check_rows(required), "rationale": "The screening decision is unsupported."}
        value["checks"][0]["outcome"] = "failed"
        value["checks"][-1].update(outcome="failed", affected_check_ids=["inclusion", "reason"])
        before = deepcopy(value)
        normalized = normalize_check_envelope(value, required)
        self.assertEqual(normalized["checks"][-1]["affected_check_ids"], ["inclusion"])
        self.assertEqual([(row["check_id"], row["outcome"]) for row in normalized["checks"]],
                         [(row["check_id"], row["outcome"]) for row in before["checks"]])
        self.assertEqual(value, before)
        validate_work_review(normalized, [], review_obligations=obligations)
        for links in ([], ["reason"], ["unknown", "inclusion"], ["inclusion", "inclusion"],
                      ["inclusion", "reason", "reason"], None, "inclusion"):
            bad = deepcopy(value); bad["checks"][-1]["affected_check_ids"] = links
            with self.subTest(links=links), self.assertRaises(ValidationError):
                validate_work_review(normalize_check_envelope(bad, required), [], review_obligations=obligations)
        bad = deepcopy(value); bad["checks"][-1].pop("affected_check_ids")
        with self.assertRaises(ValidationError):
            validate_work_review(normalize_check_envelope(bad, required), [], review_obligations=obligations)

    def test_each_failed_critique_requires_its_own_nonpassed_claim_check(self):
        obligations = [{"work_id": "W1", "hypothesis": hypothesis} for hypothesis in ("Finding scope.", "Limitations scope.")]
        required = work_review_checks([], obligations)
        value = {"checks": check_rows(required), "rationale": "Two independent criticisms require separate corrections."}
        critique_rows = [row for row in value["checks"] if row["check_id"].startswith("critique:")]
        for row, field in zip(critique_rows, ("finding", "limitations")):
            row.update(outcome="failed", affected_check_ids=[field])
        next(row for row in value["checks"] if row["check_id"] == "finding")["outcome"] = "failed"
        with self.assertRaisesRegex(ModelContractError, "Each unresolved critique"):
            validate_work_review(value, [], review_obligations=obligations)
        next(row for row in value["checks"] if row["check_id"] == "limitations")["outcome"] = "failed"
        validate_work_review(value, [], review_obligations=obligations)

    def test_open_question_does_not_require_a_negative_critique_outcome(self):
        obligations = [{"work_id": "W1", "hypothesis": "The research question has not been answered."}]
        value = {"checks": check_rows(work_review_checks([], obligations)),
                 "rationale": "The current map makes no claim that the research question is answered."}
        value["checks"][-1]["result"] = "The question remains open; no unsupported current answer is admitted."
        validate_work_review(value, [], review_obligations=obligations)

    def test_follow_up_search_shares_only_exact_successful_current_discovery(self):
        runner = object.__new__(SurveyRunner)
        runner.bounds = {"results_per_query": 50}
        runner.bibliography_mode = "openalex"
        runner.work_orders = [{"id": "current"}]
        runner.follow_up_ref = "follow-up-current"
        plans = {"first": {"follow_up_ref": "follow-up-current"},
                 "second": {"follow_up_ref": "follow-up-current"},
                 "superseded": {"follow_up_ref": "follow-up-current"},
                 "old": {"follow_up_ref": "follow-up-old"}}
        runner.store = type("Store", (), {
            "get": lambda _self, ref: {**plans[ref], "artifact_id": ref},
            "head": lambda _self, logical: {"artifact_ref": "replacement" if logical == "superseded" else logical},
        })()
        runner._body = lambda value: value
        request = {"operation": "search", "query": "exact query", "cursor": None, "limit": 50}
        row = {"request": request, "plan_ref": "first", "role": "research.search-planner",
               "provider": "openalex", "outcome": "ok"}
        runner.search_log = [row]
        with patch.object(runner, "_bibliographic_call") as call:
            runner._search(["exact query"], "methods.blind-search-planner", "second")
        call.assert_not_called()
        self.assertEqual(runner.search_log, [row])
        for replacement, kwargs in (
                ({"plan_ref": "old"}, {}), ({"plan_ref": "superseded"}, {}), ({"outcome": "timeout"}, {}),
                ({"provider": "crossref"}, {}), ({"request": {**request, "cursor": "next"}}, {}),
                ({"request": {**request, "limit": 20}}, {}), ({}, {"admission": "challenge"})):
            with self.subTest(replacement=replacement, kwargs=kwargs):
                runner.search_log = [{**row, **replacement}]
                with patch.object(runner, "_bibliographic_call") as call:
                    runner._search(["exact query"], "methods.blind-search-planner", "second", **kwargs)
                self.assertEqual(call.call_count, 1)

    def test_query_identity_preserves_provider_syntax_and_deduplicates_current_run(self):
        from scisaurus.runtime.survey import query_identity
        self.assertNotEqual(query_identity('"long range order"'), query_identity('long range order'))
        self.assertNotEqual(query_identity('C++ simulation'), query_identity('C simulation'))
        runner = object.__new__(SurveyRunner)
        runner.search_log = []
        runner.resume_session = None
        runner.work_orders = []
        runner.bounds = {"results_per_query": 50}
        runner.bibliography_mode = "openalex"
        def record(operation, *, role, query, **kwargs):
            runner.search_log.append({"request": {"operation": operation, "query": query, "limit": 50},
                                      "outcome": "ok"})
        with patch.object(runner, "_bibliographic_call", side_effect=record) as call:
            runner._search(['"long range order"', 'long range order'], 'planner-a')
            runner._search(['"long range order"'], 'planner-b')
        self.assertEqual(call.call_count, 2)

    def test_cursor_coverage_tracks_unconsumed_successful_pages(self):
        from scisaurus.runtime.survey import acquisition_succeeded
        runner = object.__new__(SurveyRunner)
        first = {"request": {"operation": "search", "query": "critical phenomena", "cursor": None},
                 "outcome": "ok", "has_more": True, "next_cursor": "page-2"}
        runner.search_log = [first]
        self.assertEqual(len(runner._pending_bibliographic_pages()), 1)
        runner.search_log.append({"request": {**first["request"], "cursor": "page-2"},
                                  "outcome": "timeout", "has_more": False, "next_cursor": None})
        self.assertEqual(len(runner._pending_bibliographic_pages()), 1)
        self.assertFalse(acquisition_succeeded(runner.search_log[-1]))
        runner.search_log.append({"request": {**first["request"], "cursor": "page-2"},
                                  "outcome": "ok", "has_more": False, "next_cursor": None})
        self.assertEqual(runner._pending_bibliographic_pages(), [])

    def test_resume_overlays_only_validated_relationship_versions_after_map_checkpoint(self):
        old = {"source": "W101", "target": "W102", "kind": "related",
               "claim": {"text": "old", "evidence": []}, "artifact_ref": "artifact:kb/r@1"}
        new = {**old, "claim": {"text": "new", "evidence": []}}
        baseline = {"W101-W102-related": old}
        omitted = {"source": "W201", "target": "W102", "kind": "related",
                   "claim": {"text": "omitted", "evidence": []}}
        records = [
            ({"created_at": "2026-09-11T00:00:00+00:00", "artifact_ref": "artifact:kb/r@1"}, old),
            ({"created_at": "2026-09-11T00:00:00+00:00", "artifact_ref": "artifact:kb/r@2"}, new),
            ({"created_at": "2026-09-11T00:00:00+00:00", "artifact_ref": "artifact:kb/omitted@1"}, omitted),
        ]
        restored = overlay_post_checkpoint_relationships(
            baseline, "2026-09-11T00:00:01+00:00", records)
        self.assertEqual(restored["W101-W102-related"]["artifact_ref"], "artifact:kb/r@2")
        self.assertEqual(restored["W101-W102-related"]["claim"]["text"], "new")
        self.assertNotIn("W201-W102-related", restored)

    def test_scoped_map_patch_preserves_ungranted_relationship_without_artifact_metadata(self):
        previous = {"work_id": "W101", "inclusion": "included", "reason": "Old reason",
                    **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        relationship = {"source": "W101", "target": "W102", "kind": "related",
                        "claim": {"text": None, "evidence": []},
                        "artifact_ref": "artifact:kb/relationships/W101-W102-related@1"}
        repaired = apply_scoped_map_repair(
            "W101", previous, [relationship],
            {"entry_fields": ["reason"], "relationship_targets": []},
            {"entry_updates": {"reason": "Narrow reason"}, "relationships": []})
        self.assertEqual(repaired["entries"][0]["reason"], "Narrow reason")
        self.assertEqual(repaired["relationships"], [{key: relationship[key]
                         for key in ("source", "target", "kind", "claim")}])

    def test_scoped_map_patch_accepts_assigned_work_wrapper(self):
        previous = {"work_id": "W101", "inclusion": "included", "reason": "Old reason",
                    **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        repaired = apply_scoped_map_repair(
            "W101", previous, [],
            {"entry_fields": ["finding"], "relationship_targets": []},
            {"entry_updates": {"W101": {"finding": {
                "text": "A bounded finding.", "evidence": []}}}, "relationships": []},
            reject_ungranted_changes=True)
        self.assertEqual(repaired["entries"][0]["finding"]["text"], "A bounded finding.")
        self.assertEqual(repaired["entries"][0]["reason"], "Old reason")

    def test_scoped_map_patch_can_retain_unrepaired_failed_fields_for_recheck(self):
        previous = {"work_id": "W101", "inclusion": "uncertain", "reason": "Old reason",
                    **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        repaired = apply_scoped_map_repair(
            "W101", previous, [],
            {"entry_fields": ["inclusion", "reason", "finding"], "relationship_targets": []},
            {"entry_updates": {"W101": {"reason": "Corrected reason"}}, "relationships": []},
            reject_ungranted_changes=True)
        self.assertEqual(repaired["entries"][0]["reason"], "Corrected reason")
        self.assertEqual(repaired["entries"][0]["inclusion"], "uncertain")
        self.assertEqual(repaired["entries"][0]["finding"], previous["finding"])

    def test_scoped_map_patch_rejects_multiple_work_wrappers(self):
        previous = {"work_id": "W101", "inclusion": "included", "reason": "Old reason",
                    **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        with self.assertRaisesRegex(ValidationError, "ungranted work"):
            apply_scoped_map_repair(
                "W101", previous, [],
                {"entry_fields": ["finding"], "relationship_targets": []},
                {"entry_updates": {
                    "W101": {"finding": {"text": "A", "evidence": []}},
                    "W102": {"finding": {"text": "B", "evidence": []}}},
                 "relationships": []},
                reject_ungranted_changes=True)

    def test_scoped_map_patch_ignores_echoed_protected_entry_fields(self):
        previous = {"work_id": "W101", "inclusion": "included", "reason": "Old reason",
                    **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        repaired = apply_scoped_map_repair(
            "W101", previous, [],
            {"entry_fields": ["reason"], "relationship_targets": []},
            {"entry_updates": {
                "reason": "Narrow reason", "inclusion": "uncertain",
                "problem": {"text": "Out of scope", "evidence": []}},
             "relationships": []})
        self.assertEqual(repaired["entries"][0]["reason"], "Narrow reason")
        self.assertEqual(repaired["entries"][0]["inclusion"], "included")
        self.assertEqual(repaired["entries"][0]["problem"], previous["problem"])

    def test_unknown_statement_has_no_evidence_and_scope_is_exact(self):
        unknown = {"text": None, "evidence": []}
        entry = {"work_id": "W101", "inclusion": "uncertain", "reason": "No available text.",
                 **{field: deepcopy(unknown) for field in MAP_FIELDS}}
        value = {"entries": [entry], "relationships": []}
        validate_map(value, ["W101"], {"W101"}, {})
        entry["problem"]["evidence"] = [{"work_id": "W101", "source_ref": "missing", "quote": "invented"}]
        with self.assertRaisesRegex(ValidationError, "unknown statement"):
            validate_map(value, ["W101"], {"W101"}, {})
        entry["problem"] = deepcopy(unknown)
        with self.assertRaisesRegex(ValidationError, "unassigned"):
            validate_map(value, ["W102"], {"W101", "W102"}, {})

    def test_conceptual_connection_requires_both_works(self):
        entry = {"work_id": "W101", "inclusion": "included", "reason": "The source examines recall timing.",
                 **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        sources = {"source-one": {"work_id": "W101", "representation": "abstract", "text": "Recall timing is examined."},
                   "source-two": {"work_id": "W102", "representation": "abstract", "text": "Recall timing is examined."}}
        claim = {"text": "Both works examine recall timing.", "evidence": [
            {"work_id": "W101", "source_ref": "source-one", "quote": "Recall timing is examined."}]}
        value = {"entries": [entry], "relationships": [{"source": "W101", "target": "W102", "kind": "compares", "claim": claim}]}
        with self.assertRaisesRegex(ValidationError, "both works"):
            validate_map(value, ["W101"], {"W101", "W102"}, sources)
        claim["evidence"].append({"work_id": "W102", "source_ref": "source-two", "quote": "Recall timing is examined."})
        validate_map(value, ["W101"], {"W101", "W102"}, sources)

    def test_map_projection_keeps_exact_claims_and_abstains_on_bad_quotes(self):
        source_one = "The measured period increased by ten percent."
        source_two = "The basal control differed across the sample."
        displayed = [
            {"source_ref": "source-one", "work_id": "W101", "representation": "abstract", "text": source_one,
             "window": {"start": 0, "end": len(source_one)}},
            {"source_ref": "source-two", "work_id": "W102", "representation": "abstract", "text": source_two,
             "window": {"start": 0, "end": len(source_two)}},
        ]
        value = {
            "entries": [{
                "work_id": "W101", "inclusion": "included", "reason": "A bounded measurement is reported.",
                "problem": {"text": "The measured period increased.", "evidence": [{
                    "work_id": "W101", "source_ref": "source-one",
                    "quote": "The measured period increased", "decoration": "ignored",
                }]},
                "approach": {"text": "Unsupported wording.", "evidence": [{
                    "work_id": "W101", "source_ref": "source-one", "quote": "not in source",
                }]},
                "finding": {"text": "Cross-work citation.", "evidence": [{
                    "work_id": "W102", "source_ref": "source-two",
                    "quote": "The basal control differed",
                }]},
                "limitations": {"text": None, "evidence": []}, "extra": "ignored",
            }],
            "relationships": [
                {"source": "W101", "target": "W102", "kind": "related",
                 "claim": "Uncited relation."},
                {"source": "W101", "target": "W102", "kind": "compares",
                 "claim": {"text": "The conditions differ.", "evidence": [
                     {"work_id": "W101", "source_ref": "source-one",
                      "quote": "The measured period increased"},
                     {"work_id": "W102", "source_ref": "source-two",
                      "quote": "The basal control differed"},
                 ]}},
            ],
        }

        normalized = normalize_map_worker_response(
            value, work_id="W101", all_work_ids={"W101", "W102"},
            sources=displayed,
            windows={row["source_ref"]: row["window"] for row in displayed},
        )

        entry = normalized["entries"][0]
        self.assertEqual(entry["problem"]["text"], "The measured period increased.")
        self.assertEqual(entry["problem"]["evidence"][0]["start"], 0)
        self.assertEqual(entry["approach"], {"text": None, "evidence": []})
        self.assertEqual(entry["finding"], {"text": None, "evidence": []})
        self.assertEqual(len(normalized["relationships"]), 1)
        validate_map(
            normalized, ["W101"], {"W101", "W102"},
            {row["source_ref"]: row for row in displayed}, require_spans=True,
        )

    def test_scoped_map_projection_discards_ungranted_fields_and_relations(self):
        source_one = "The measured period increased by ten percent."
        source_two = "The basal control differed across the sample."
        displayed = [
            {"source_ref": "source-one", "work_id": "W101", "representation": "abstract", "text": source_one,
             "window": {"start": 0, "end": len(source_one)}},
            {"source_ref": "source-two", "work_id": "W102", "representation": "abstract", "text": source_two,
             "window": {"start": 0, "end": len(source_two)}},
        ]
        previous = {"work_id": "W101", "inclusion": "included", "reason": "Prior screening.",
                    **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        feedback = {"entry_fields": ["finding"], "relationship_targets": ["W102"]}
        response = {
            "entry_updates": {"W101": {
                "finding": {"text": "The measured period increased.", "evidence": [{
                    "work_id": "W101", "source_ref": "source-one",
                    "quote": "The measured period increased",
                }]},
                "reason": "ungranted edit",
            }, "W999": {"reason": "other work"}},
            "relationships": [
                {"source": "W101", "target": "W999", "kind": "related",
                 "claim": {"text": "ungranted", "evidence": []}},
                {"source": "W101", "target": "W102", "kind": "compares",
                 "claim": {"text": "The conditions differ.", "evidence": [
                     {"work_id": "W101", "source_ref": "source-one",
                      "quote": "The measured period increased"},
                     {"work_id": "W102", "source_ref": "source-two",
                      "quote": "The basal control differed"},
                 ]}},
            ],
        }

        normalized = normalize_map_worker_response(
            response, work_id="W101", all_work_ids={"W101", "W102"},
            sources=displayed,
            windows={row["source_ref"]: row["window"] for row in displayed},
            review_feedback=feedback,
        )
        repaired = apply_scoped_map_repair(
            "W101", previous, [], feedback, normalized,
            reject_ungranted_changes=True,
        )

        self.assertEqual(set(normalized["entry_updates"]), {"finding"})
        self.assertEqual(repaired["entries"][0]["reason"], "Prior screening.")
        self.assertEqual([row["target"] for row in repaired["relationships"]], ["W102"])
        validate_map(
            repaired, ["W101"], {"W101", "W102"},
            {row["source_ref"]: row for row in displayed}, require_spans=True,
        )

    def test_flat_relationship_statement_is_canonicalized_without_dropping_evidence(self):
        entry = {"work_id": "W101", "inclusion": "included", "reason": "The source examines recall timing.",
                 **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        sources = {"source-one": {"work_id": "W101", "representation": "abstract", "text": "Recall timing is examined."},
                   "source-two": {"work_id": "W102", "representation": "abstract", "text": "Recall timing is examined."}}
        evidence = [
            {"work_id": "W101", "source_ref": "source-one", "quote": "Recall timing is examined."},
            {"work_id": "W102", "source_ref": "source-two", "quote": "Recall timing is examined."},
        ]
        value = {"entries": [entry], "relationships": [{
            "source": "W101", "target": "W102", "kind": "compares",
            "claim": "Both works examine recall timing.", "evidence": evidence,
        }]}
        canonical = normalize_map_relationships(value)
        self.assertEqual(canonical["relationships"][0]["claim"], {
            "text": value["relationships"][0]["claim"], "evidence": evidence})
        self.assertNotIn("evidence", canonical["relationships"][0])
        validate_map(canonical, ["W101"], {"W101", "W102"}, sources)

    def test_invalid_seed_and_time_contract_rejected_before_run(self):
        config = survey_config("http://127.0.0.1:1/works")
        config["survey"]["seed_work_ids"] = ["https://openalex.org/W101"]
        with self.assertRaisesRegex(ValidationError, "canonical OpenAlex"):
            validate_survey_config(config)
        config["survey"]["seed_work_ids"] = []
        config["time_policy"]["hard_seconds"] = 5
        with self.assertRaisesRegex(ValidationError, "first_result_seconds"):
            validate_survey_config(config)

if __name__ == "__main__":
    unittest.main()
