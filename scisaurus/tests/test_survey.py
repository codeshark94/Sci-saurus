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
from scisaurus.core.surveys import SurveyGate
from scisaurus.runtime.execution import _invoke_worker
from scisaurus.runtime.survey import (SurveyRunner, apply_scoped_map_repair,
                                      overlay_post_checkpoint_relationships)
from scisaurus.runtime.survey_config import validate_survey_config
from scisaurus.runtime.survey_records import GAP_CHECKS, MAP_FIELDS, SURVEY_CHECKS, validate_map
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
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        self.requests.append({"path": path.path, "query": query})
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
        value = {"checks": check_rows(assignment["required_checks"]), "rationale": "Each scoped claim is supported or explicitly unknown."}
        if mode.startswith("semantic-") and "every task" in assignment["entry"]["reason"]:
            next(check for check in value["checks"] if check["check_id"] == "reason").update(
                outcome="failed", result="The abstract does not establish generalization to every task; narrow the reason to recall timing.")
    elif phase == "gap_assessment":
        decisive = mode in {"fulltext-refutes", "abstract-refutes"}
        sources = [s for s in assignment["sources"] if s["work_id"] == "W401"]
        source = next((s for s in sources if s["representation"] == "full_text"), sources[0])
        proof = source_quote(source, "This prior method solves delayed recall.")
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

    def runtime(self, config=None, *, on_progress=None):
        runner = SurveyRunner(self.root / "run", config or survey_config(self.endpoint), on_progress=on_progress)
        runner.worker_target = simulated_survey_worker
        return runner

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

    def test_focused_failure_cannot_be_overridden_by_global_reviewer(self):
        config = survey_config(self.endpoint)
        config["model"]["model"] = "semantic-exhaust"
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("focused literature review remains unresolved", result["error"])
        self.assertIsNone(result["survey_ref"])
        control, store = self.open_store()
        self.assertEqual(store.head("kb/work-reviews/W101")["version"], 2)
        self.assertFalse(any(prompt["phase"] == "survey_review" for _, prompt in self.model_contexts(control, store)))

    def test_semantic_repair_rejects_an_ungranted_field_change(self):
        config = survey_config(self.endpoint)
        config["model"]["model"] = "semantic-unscoped"
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("exactly the granted entry fields", result["error"])
        self.assertIsNone(result["survey_ref"])
        _, store = self.open_store()
        self.assertEqual(store.head("kb/work-analyses/W101")["version"], 1)

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

    def test_exhausted_work_repair_keeps_other_valid_work_artifacts(self):
        config = survey_config(self.endpoint, "map-reject")
        config["limits"]["max_rounds"] = 2
        result = self.runtime(config).run()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("map-W101", result["error"])
        self.assertIsNone(result["survey_ref"])
        control, store = self.open_store()
        self.assertIsNone(store.head("kb/work-analyses/W101"))
        for wid in ("W201", "W102", "W301"):
            self.assertEqual(store.versions("kb/work-analyses/" + wid), [1])
        maps = [prompt for _, prompt in self.model_contexts(control, store) if prompt["phase"] == "map"]
        self.assertEqual(sum(prompt["requested_work_ids"] == ["W101"] for prompt in maps), 2)
        self.assertEqual(len(maps), 5)

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
        self.assertEqual(result["status"], "blocked")
        self.assertIn("unchanged work W101", result["error"])
        self.assertFalse(result["survey_current"])
        _, store = self.open_store()
        self.assertEqual(store.versions("kb/work-analyses/W101"), [1])

    def test_fabricated_quote_never_reaches_survey_acceptance(self):
        result = self.runtime(survey_config(self.endpoint, "forged-quote")).run()
        self.assertEqual(result["status"], "blocked")
        self.assertIn("exact captured text", result["error"])
        self.assertIsNone(result["survey_ref"])
        self.assertIsNone(result["assessment_ref"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])

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
