"""Survey integration against local HTTP/stdio fixtures and simulated model workers."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.source_spans import bind, expand_evidence
from scisaurus.core.surveys import ABSTENTION_REASONS, SurveyGate
from scisaurus.runtime.execution import SYSTEM, _invoke_worker
from scisaurus.runtime.literature import ProviderCooldownError
from scisaurus.runtime.models import estimate_input_tokens
from scisaurus.runtime.survey import (SurveyRunner, apply_scoped_map_repair,
                                      normalize_map_relationships,
                                      overlay_post_checkpoint_relationships)
from scisaurus.runtime.survey_config import validate_survey_config
from scisaurus.runtime.survey_records import (GAP_CHECKS, MAP_FIELDS, SURVEY_CHECKS,
                                               normalize_check_envelope, validate_assessment, validate_map)
from scisaurus.runtime.time_policy import STAGES


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
    requests = []
    refresh_target = False
    rate_limit_once = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        self.requests.append({"path": path.path, "query": query})
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
        if ("query.bibliographic" in query
                or query.get("filter", [""])[0].lower().startswith("doi:")):
            doi = query.get("query.bibliographic", query.get("filter"))[0].lower()
            doi = doi.removeprefix("doi:")
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
        else:
            term = query.get("search", [""])[0]
            ids = ["W401"] if term == "prior solution" else (
                ["W201"] if term == "independent terminology" else ["W101"])
            if "filter" in query:
                ids = ["W301"]
            if term == "prior solution" and self.refresh_target:
                ids.append("W102")
            payload = {"meta": {"count": len(ids), "per_page": int(query["per_page"][0]),
                                "next_cursor": None}, "results": [survey_work(wid) for wid in ids]}
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
             "result": "The explicit fixture contract is satisfied."} for name in names]


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
    elif phase == "counter_plan":
        value = {"queries": ["prior solution"], "rationale": "Search for an existing solution."}
    elif phase == "nomination":
        value = {"id": "delayed-recall", "statement": GAP}
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
        if mode.startswith("map-links") and assignment["requested_work_ids"] == ["W101"]:
            proofs = [source_quote(next(source for source in assignment["sources"] if source["work_id"] == wid))
                      for wid in ("W101", "W102")]
            value["relationships"] = [{"source": "W101", "target": "W102", "kind": "compares",
                                       "claim": {"text": "Both works examine recall timing.", "evidence": proofs}}]
            if mode == "map-links-rewrite" and assignment.get("entry_editable") is False:
                value["entries"][0]["reason"] = "A changed screening rationale without new evidence."
    elif phase == "survey_review":
        value = {"checks": check_rows(SURVEY_CHECKS), "rationale": "The map preserves unknown facts and bounded coverage."}
        if mode == "survey-fails":
            value["checks"][1].update(outcome="failed", result="The independent fixture review rejects source fidelity.")
    elif phase == "work_review":
        if mode == "review-malformed" and assignment["entry"]["work_id"] == "W101":
            value = {"checks": [{"check_id": "duplicate-check", "outcome": "passed",
                                  "method": "Malformed fixture response.", "result": "Not a valid focused review."}],
                     "rationale": "Malformed fixture response."}
        else:
            value = {"checks": check_rows(assignment["required_checks"]), "rationale": "Each scoped claim is supported or explicitly unknown."}
        if mode == "review-never-resolves" and assignment["entry"]["work_id"] == "W101":
            next(check for check in value["checks"] if check["check_id"] == "reason").update(
                outcome="insufficient_evidence", result="The screening rationale remains unresolved.")
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
        "elapsed_seconds": time.monotonic() - started, "finish_reason": "stop"}})


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
            key: {} for key in ('url', 'max_length', 'start_index', 'raw')}}}]}
    elif method == 'tools/call':
        text = 'Recall study W401\nMethods\nRecall timing is examined. The observation budget is fixed.\nResults\nThis prior method solves delayed recall.'
        unavailable = message['params']['arguments']['url'].endswith('/unavailable')
        result = {'content': [{'type': 'text', 'text': 'Fixture source unavailable.' if unavailable else text}],
                  'isError': unavailable}
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
                    "work_id": wid, "representation": "full_text",
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
        owner = next(source for source in assignment["sources"] if source["work_id"] == "W000")
        comparisons = [source for source in assignment["sources"] if source["work_id"] != "W000"]
        self.assertEqual(len(owner["text"]), 30000)
        self.assertEqual(len(comparisons), 79)
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
                    "work_id": wid, "representation": "full_text",
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
        result = self.runtime(survey_config(self.endpoint, "catalog-evidence")).run()
        self.assertEqual(result["status"], "completed", result.get("error"))
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

    def runtime(self, config=None, *, on_progress=None, resume_policy=None):
        runner = SurveyRunner(self.root / "run", config or survey_config(self.endpoint),
                              on_progress=on_progress, resume_policy=resume_policy)
        runner.worker_target = simulated_survey_worker
        return runner

    def test_process_stop_is_not_a_survey_failure_retry(self):
        runner = self.runtime()
        with patch.object(runner, "_setup", side_effect=KeyboardInterrupt("termination requested")):
            result = runner.run()
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["failure"], {"kind": "process_interrupted"})

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
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result)
        _, store = self.open_store()
        entry = json.loads(store.read_body(store.head("kb/work-analyses/W101")["body_hash"]))
        self.assertEqual(entry["inclusion"], "uncertain")
        self.assertTrue(all(entry[field]["text"] is None for field in MAP_FIELDS))
        exclusion = json.loads(store.read_body(store.head("kb/work-exclusions/W101")["body_hash"]))
        retained = json.loads(store.read_body(store.get(exclusion["retained_analysis_ref"])["body_hash"]))
        self.assertIsNotNone(retained["problem"]["text"])
        self.assertEqual(store.versions("kb/work-analyses/W201"), [1])

    def test_malformed_focused_review_withdraws_one_work_without_blocking_survey(self):
        config = survey_config(self.endpoint, "review-malformed")
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result)
        _, store = self.open_store()
        entry = json.loads(store.read_body(store.head("kb/work-analyses/W101")["body_hash"]))
        self.assertEqual(entry["inclusion"], "uncertain")
        self.assertTrue(all(entry[field]["text"] is None for field in MAP_FIELDS))
        self.assertIsNotNone(store.head("kb/work-exclusions/W101"))
        self.assertIsNotNone(store.head("kb/work-reviews/W201"))

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

    def test_semantic_repair_rejects_an_ungranted_field_change(self):
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
        self.assertIn("does not resolve", current["reason"])
        self.assertIn("ungranted entry field", json.loads(store.read_body(
            store.head("command/survey-abstentions/W101")["body_hash"]))["reason"])

    def test_revision_waves_reserve_review_for_previously_repaired_work(self):
        config = survey_config(self.endpoint, "semantic-many")
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        decisions = [item for item in result["time_decisions"] if item["stage"] == "revision"]
        self.assertEqual([(item["task_count"], item["pending_review_count"]) for item in decisions[:2]], [(2, 0), (2, 2)])
        self.assertGreater(decisions[1]["reserved_review_seconds"], decisions[0]["reserved_review_seconds"])

    def test_abstract_only_run_retains_search_expansion_and_scoped_map(self):
        runner = self.runtime()
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["gap_state"], "insufficient_evidence")
        self.assertTrue(result["survey_current"])
        self.assertEqual(result["coverage"]["unique_works"], 5)
        self.assertEqual(result["coverage"]["verified_full_texts"], 0)
        self.assertEqual(result["coverage"]["expansion"][0]["new_unique_works"], 2)
        searches = result["coverage"]["searches"]
        self.assertEqual(len(searches), 7)
        self.assertEqual(len(SurveyHTTPFixture.requests), 8)
        self.assertEqual(result["usage"]["cumulative_usage"]["retrieval_calls"], 8)
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
        stable_prefix = serialized_maps[0].split('"requested_work_ids"', 1)[0]
        self.assertTrue(all(serialized.startswith(stable_prefix) for serialized in serialized_maps))
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
        self.assertNotIn("W301", work_ids)
        self.assertTrue(any(gap.get("work_id") == "W301"
                            and gap.get("admission") == "discovery"
                            and gap.get("reserved_challenge_slots") == 1
                            for gap in result["coverage"]["access_and_limit_gaps"]))

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

    def test_parallel_mapping_repairs_only_failed_work_and_preserves_valid_versions(self):
        config = survey_config(self.endpoint, "map-repair")
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
        self.assertEqual(len(before_retry), 1)
        control, store = self.open_store()
        maps = [(record, prompt) for record, prompt in self.model_contexts(control, store) if prompt["phase"] == "map"]
        counts = {wid: sum(prompt["requested_work_ids"] == [wid] for _, prompt in maps)
                  for wid in ("W101", "W201", "W102", "W301", "W401")}
        self.assertEqual(counts, {"W101": 2, "W201": 1, "W102": 1, "W301": 1, "W401": 1})
        retried = next(prompt for _, prompt in maps if "validation_feedback" in prompt)
        self.assertEqual(retried["requested_work_ids"], ["W101"])
        self.assertIn("W101", retried["validation_feedback"]["error"])
        for wid, ref in before_retry[0].items():
            self.assertEqual(store.head("kb/work-analyses/" + wid)["artifact_ref"], ref)
            self.assertEqual(store.versions("kb/work-analyses/" + wid), [1])
        timings = []
        for record, _ in maps[:2]:
            task = record["artifact_id"].removeprefix("command/contexts/")
            execution = json.loads(store.read_body(store.head("command/executions/" + task)["body_hash"]))
            timings.append(json.loads(execution["model"]))
        self.assertNotEqual(timings[0]["pid"], timings[1]["pid"])
        self.assertGreater(min(row["finished"] for row in timings) - max(row["started"] for row in timings), 0.1)
        production = [decision for decision in result["time_decisions"] if decision["stage"] == "production"]
        self.assertEqual([(row["task_count"], row["pending_review_count"]) for row in production[:3]], [(2, 0), (2, 1), (1, 3)])
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

    def test_exhausted_work_repair_keeps_other_valid_work_artifacts(self):
        config = survey_config(self.endpoint, "map-reject")
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
        self.assertEqual(sum(prompt["requested_work_ids"] == ["W101"] for prompt in maps), 2)
        self.assertEqual(len(maps), 6)

    def test_updated_target_rechecks_its_directed_relationship_owner(self):
        SurveyHTTPFixture.refresh_target = True
        result = self.runtime(self.full_text_config("map-links")).run()
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
        result = self.runtime(survey_config(self.endpoint, "map-links-rewrite")).run()
        self.assertEqual(result["status"], "completed", result["error"])
        _, store = self.open_store()
        self.assertEqual(store.versions("kb/work-analyses/W101"), [1])
        self.assertIn("unchanged work W101", json.loads(store.read_body(
            store.head("command/survey-abstentions/W101")["body_hash"]))["reason"])
        mapped = json.loads(store.read_body(store.head("kb/literature-map")["body_hash"]))
        self.assertEqual(mapped["relationship_refs"], [])

    def test_fabricated_quote_never_reaches_survey_acceptance(self):
        result = self.runtime(survey_config(self.endpoint, "forged-quote")).run()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("No substantive literature claim survived", result["error"])
        self.assertIsNone(result["survey_ref"])
        self.assertIsNone(result["assessment_ref"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])

    def test_deep_analysis_budget_reserves_countersearch_without_reviewing_deferrals(self):
        config = survey_config(self.endpoint)
        config["survey"]["search"]["max_analyzed_works"] = 3
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        contexts = [prompt for _, prompt in self.model_contexts(control, store)]
        maps = [prompt for prompt in contexts if prompt["phase"] == "map"]
        reviews = [prompt for prompt in contexts if prompt["phase"] == "work_review"]
        self.assertEqual(len(maps), 3)
        self.assertEqual(len(reviews), 3)
        self.assertIn(["W401"], [p["requested_work_ids"] for p in maps])
        self.assertEqual(result["coverage"]["deep_analysis_limit"], 3)
        self.assertEqual(len(result["coverage"]["abstentions"]), 2)
        for abstention in result["coverage"]["abstentions"]:
            review = json.loads(store.read_body(store.head("kb/work-reviews/" + abstention["work_id"])["body_hash"]))
            self.assertEqual(review["verification_kind"], "deterministic_abstention")

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

    def test_abstention_integrity_cannot_accept_scientific_prose(self):
        from scisaurus.core.schema import canonical_bytes, sha256_hex
        from scisaurus.core.surveys import ABSTENTION_REASONS, is_explicit_abstention
        entry = {"work_id": "W101", "inclusion": "uncertain", "reason": ABSTENTION_REASONS["deep_analysis_budget"],
                 **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        record = {"work_id": "W101", "scope": "deep_analysis_budget", "entry_sha256": sha256_hex(canonical_bytes(entry))}
        self.assertTrue(is_explicit_abstention(entry, record))
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

    def test_review_only_resume_does_not_repeat_initial_acquisition(self):
        config = survey_config(self.endpoint)
        with patch.object(SurveyRunner, "_review_work_claims", side_effect=KeyboardInterrupt("checkpoint before review")):
            first = self.runtime(config).run()
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["failure"], {"kind": "process_interrupted"})
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["focused_review"]}}
        resumed = self.runtime(config, resume_policy=policy)
        with patch.object(resumed, "_search") as search, patch.object(resumed, "_expand") as expand, \
             patch.object(resumed, "_full_texts") as fetch, \
             patch.object(resumed, "_countersearch", side_effect=KeyboardInterrupt("after accepted survey")) as counter:
            result = resumed.run()
        self.assertIsNotNone(result["survey_ref"], result)
        search.assert_not_called(); expand.assert_not_called(); fetch.assert_not_called()
        counter.assert_called_once()

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

    def test_aggregate_review_separates_screening_accounting_from_scientific_answers(self):
        runner = self.runtime()
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        for wid in ("W101", "W102"):
            runner._bibliographic_call("work", role="research.seed-reader", work_id=wid)
        runner._map()
        runner._materialize_source_less_map("W102", runner.analyzed_basis["W102"],
            scope="review_exhausted", reason=ABSTENTION_REASONS["review_exhausted"])
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
        self.assertEqual(resumed.expanded, {"W101"})

    def test_cached_exhausted_map_becomes_abstention_without_new_calls(self):
        from scisaurus.runtime.model_work import ModelWorkBlocked
        runner = self.runtime(survey_config(self.endpoint, "map-reject"))
        self.addCleanup(runner.control.close)
        runner._initialize(); runner._setup()
        runner._bibliographic_call("work", role="research.seed-reader", work_id="W101")
        basis = [runner.work_records["W101"]["artifact_ref"], *runner.source_docs]
        job = runner._map_job("W101", basis)
        handler = job.pop("on_exhausted")
        with self.assertRaises(ModelWorkBlocked):
            runner._models_checked([job])
        job["on_exhausted"] = handler
        with patch.object(runner, "_call_batch") as call:
            runner._models_checked([job])
            runner._models_checked([job])
        call.assert_not_called()
        self.assertEqual(runner._body(runner.analysis_records["W101"])["finding"]["text"], None)

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
        self.assertEqual(result["status"], "blocked")
        self.assertIn("full", result["error"].lower())
        self.assertIsNone(result["assessment_ref"])

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

    def test_real_stdio_full_text_can_refute_prior_gap(self):
        result = self.runtime(self.full_text_config("fulltext-refutes")).run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertEqual(result["gap_state"], "refuted_by_prior_work")
        self.assertEqual(result["coverage"]["verified_full_texts"], 1)
        self.assertTrue(result["survey_current"])
        self.assertIsNotNone(result["assessment_ref"])
        self.assertEqual(result["usage"]["cumulative_usage"]["retrieval_calls"], 10)
        _, store = self.open_store()
        capture = json.loads(store.read_body(store.head("kb/full-text/W401")["body_hash"]))
        execution = json.loads(store.read_body(store.get(capture["execution_ref"])["body_hash"]))
        self.assertEqual(execution["metadata"]["server_info"]["version"], "simulated-fixture-1")
        self.assertEqual(execution["metadata"]["process_returncode"], 0)
        self.assertEqual(execution["metadata"]["transport"], "mcp_stdio")

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
        self.assertEqual(result["usage"]["cumulative_usage"]["retrieval_calls"], 10)
        capability = result["capabilities"]["full-text"]
        self.assertEqual(capability["state"], "degraded")
        control, store = self.open_store()
        readiness = json.loads(store.read_body(store.get(capability["verification_ref"])["body_hash"]))
        self.assertEqual(readiness["outcome"], "passed")
        self.assertIsNotNone(capability["failure_ref"])
        self.assertIsNone(store.head("kb/full-text/W401"))
        assessment = next(prompt for _, prompt in self.model_contexts(control, store) if prompt["phase"] == "gap_assessment")
        self.assertTrue(all(source["representation"] == "abstract" for source in assessment["sources"]))


    def test_crossref_identity_and_stable_source_spans_are_pinned_in_v3_survey(self):
        config = survey_config(self.endpoint)
        config["survey"]["search"]["max_api_calls"] = 30
        config["survey"]["identity"] = {
            "id": "identity", "adapter": "crossref",
            "client": {"endpoint": self.endpoint, "timeout": 4, "max_bytes": 1000000},
            "representative": {"query": "10.1234/W101", "limit": 1},
            "environment_files": []}
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.open_store()
        survey = json.loads(store.read_body(store.get(result["survey_ref"])["body_hash"]))
        self.assertEqual(survey["schema_version"], "literature-survey-3")
        self.assertTrue(survey["identity_refs"])
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
        first.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        resumed = SurveyRunner(self.root / "run", config, resume_policy=policy)
        self.addCleanup(resumed.control.close)
        with patch.object(resumed, "_call_batch") as call:
            with self.assertRaises(ModelWorkBlocked):
                resumed._models_checked([job])
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

    def test_resume_finishes_pending_searches_after_partial_capture_and_rate_limit(self):
        config = survey_config(self.endpoint)
        config["survey"]["seed_queries"] = ["recall timing", "rate limited topic"]
        config["survey"]["search"]["expansion_rounds"] = 0
        # Exercise recovery from a provider failure explicitly; the live
        # default now retries transient responses within one request budget.
        config["survey"]["bibliography"]["client"]["max_retries"] = 0
        SurveyHTTPFixture.rate_limit_once = "rate limited topic"
        first = self.runtime(config).run()
        self.assertEqual(first["status"], "blocked")
        self.assertEqual(first["coverage"]["unique_works"], 1)
        self.assertIn("Workload output or provider schema failed", first["error"])

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
        sources = {"source-one": {"work_id": "W101", "text": "Recall timing is examined."},
                   "source-two": {"work_id": "W102", "text": "Recall timing is examined."}}
        claim = {"text": "Both works examine recall timing.", "evidence": [
            {"work_id": "W101", "source_ref": "source-one", "quote": "Recall timing is examined."}]}
        value = {"entries": [entry], "relationships": [{"source": "W101", "target": "W102", "kind": "compares", "claim": claim}]}
        with self.assertRaisesRegex(ValidationError, "both works"):
            validate_map(value, ["W101"], {"W101", "W102"}, sources)
        claim["evidence"].append({"work_id": "W102", "source_ref": "source-two", "quote": "Recall timing is examined."})
        validate_map(value, ["W101"], {"W101", "W102"}, sources)

    def test_flat_relationship_statement_is_canonicalized_without_dropping_evidence(self):
        entry = {"work_id": "W101", "inclusion": "included", "reason": "The source examines recall timing.",
                 **{field: {"text": None, "evidence": []} for field in MAP_FIELDS}}
        sources = {"source-one": {"work_id": "W101", "text": "Recall timing is examined."},
                   "source-two": {"work_id": "W102", "text": "Recall timing is examined."}}
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
