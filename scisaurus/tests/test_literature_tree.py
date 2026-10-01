"""Read-driven exploration, lineage contracts, and interruption recovery."""
from copy import deepcopy
import json
import unittest

from scisaurus.core.errors import ValidationError, StateError
from scisaurus.core.source_spans import bind
from scisaurus.runtime.literature_tree import validate_plan, validate_reading_selection, reading_selection_parts
from scisaurus.tests import test_survey as fixtures
from scisaurus.tests.test_survey import simulated_survey_worker, source_quote, survey_config


def chain_worker(kind, params, channel):
    if kind != "model":
        return simulated_survey_worker(kind, params, channel)
    assignment = json.loads(params["prompt"])
    if assignment.get("phase") == "reading_selection" and params["client"]["model"] == "select-independent":
        value = {"rationale": "Compare alternatives before reading the independent study.",
                 "candidates": [{"work_id": item["work_id"], "decision": "read" if item["work_id"] == "W201" else "defer",
                                 "rationale": "Prioritize independent terminology for this inquiry."}
                                for item in assignment["candidates"]]}
        channel.put({"ok": True, "result": {"text": json.dumps(value), "model": params["client"]["model"],
            "usage": {"model_calls": 1, "input_tokens": 1, "output_tokens": 1}, "elapsed_seconds": 0.01, "finish_reason": "stop"}})
        return
    if assignment.get("phase") != "exploration_plan":
        return simulated_survey_worker(kind, params, channel)
    parent = assignment["parents"][0]
    branches = []
    if params["client"]["model"] == "local-stop" and parent["kind"] == "read":
        parent = next((item for item in assignment["parents"] if item["work_id"] == "W201"), parent)
        if parent["work_id"] == "W201":
            source = next(s for s in assignment["sources"] if s["work_id"] == "W201")
            branches = [{"parent_id": parent["id"], "question": "Which studies cite this checked recall study?",
                "rationale": "Examine follow-up evidence.", "operation": "citing",
                "query": None, "work_id": "W201", "evidence": [source_quote(source)]}]
        value = {"decision": "expand" if branches else "stop", "rationale": "Close only the assigned inquiry.",
                 "branches": branches}
        channel.put({"ok": True, "result": {"text": json.dumps(value), "model": params["client"]["model"],
            "usage": {"model_calls": 1, "input_tokens": 1, "output_tokens": 1},
            "elapsed_seconds": 0.01, "finish_reason": "stop"}})
        return
    if parent["kind"] == "root":
        branches = [{"parent_id": parent["id"], "question": "Which studies examine recall timing?",
            "rationale": "Collect initial recall evidence.", "operation": "search",
            "query": "recall timing", "work_id": None, "evidence": []}]
        if params["client"]["model"] == "queued-search":
            branches.append({**branches[0], "query": "independent terminology"})
        if params["client"]["model"] == "direct-intake":
            branches[0].update(operation="work", query=None, work_id="W101")
        if params["client"]["model"] == "select-independent":
            branches[0]["query"] = "candidate comparison"
    elif parent["work_id"] in {"W101", "W102"}:
        # Citation metadata is supplied by the actual fixture work response.
        source = next(s for s in assignment["sources"] if s["work_id"] == parent["work_id"])
        target = parent["referenced_works"][0]
        branches = [{"parent_id": parent["id"], "question": "How does the referenced study treat recall timing?",
            "rationale": "Follow a checked study's reference.", "operation": "work",
            "query": None, "work_id": target, "evidence": [source_quote(source)]}]
    value = {"decision": "expand" if branches else "stop", "rationale": "The bounded reference inquiry is complete.",
             "branches": branches}
    channel.put({"ok": True, "result": {"text": json.dumps(value), "model": params["client"]["model"],
        "usage": {"model_calls": 1, "input_tokens": 1, "output_tokens": 1}, "elapsed_seconds": 0.01, "finish_reason": "stop"}})


class TestExplorationContract(unittest.TestCase):
    def setUp(self):
        self.source = {"work_id": "W1", "text": "A measured mechanism changes with humidity.",
                       "representation": "abstract"}
        self.parents = {"parent": {"kind": "read", "work_id": "W1", "source_refs": ["source"],
                                   "referenced_works": ["W2"]}}
        self.sources = {"source": self.source}
        self.plan = {"decision": "expand", "rationale": "Test a mechanism in the declared question.",
            "branches": [{"parent_id": "parent", "question": "What sets this humidity dependence?",
                "rationale": "Investigate the measured humidity dependence.", "operation": "work",
                "query": None, "work_id": "W2", "evidence": [{"work_id": "W1", "source_ref": "source",
                "quote": self.source["text"]}]}]}

    def validate(self, value):
        validate_plan(bind(value, self.sources), self.parents, self.sources, max_branches=2)

    def test_grounded_reference_branch(self):
        self.validate(self.plan)

    def test_rejects_unassigned_parent(self):
        self.plan["branches"][0]["parent_id"] = "other"
        with self.assertRaises(ValidationError): self.validate(self.plan)

    def test_rejects_foreign_source(self):
        self.plan["branches"][0]["evidence"][0]["source_ref"] = "foreign"
        with self.assertRaises(ValidationError): self.validate(self.plan)

    def test_rejects_fabricated_quote(self):
        self.plan["branches"][0]["evidence"][0]["quote"] = "An invented result."
        with self.assertRaises(ValidationError): self.validate(self.plan)

    def test_rejects_nonreference_lookup(self):
        self.plan["branches"][0]["work_id"] = "W3"
        with self.assertRaises(ValidationError): self.validate(self.plan)

    def test_rejects_ungrounded_read_branch(self):
        self.plan["branches"][0]["evidence"] = []
        with self.assertRaises(ValidationError): self.validate(self.plan)

    def test_root_cannot_request_null_citation(self):
        self.parents["parent"] = {"kind": "root", "source_refs": []}
        self.plan["branches"][0].update(operation="citing", work_id=None, evidence=[])
        with self.assertRaises(ValidationError): self.validate(self.plan)

    def test_root_direct_work_lookup_is_valid_acquisition(self):
        self.parents["parent"] = {"kind": "root", "source_refs": []}
        self.plan["branches"][0].update(work_id="W2", evidence=[])
        self.validate(self.plan)

    def test_root_work_lookup_requires_canonical_provider_identifier(self):
        self.parents["parent"] = {"kind": "root", "source_refs": []}
        self.plan["branches"][0].update(work_id="invented-paper", evidence=[])
        with self.assertRaises(ValidationError): self.validate(self.plan)

    def test_stop_cannot_contain_acquisition(self):
        self.plan["decision"] = "stop"
        with self.assertRaises(ValidationError): self.validate(self.plan)

    def test_reading_selection_accounts_for_candidates_without_paper_quota(self):
        value = {"rationale": "Investigate each unresolved mechanism.", "candidates": [
            {"work_id": wid, "decision": "read", "rationale": "Resolve an independent evidence gap."}
            for wid in ("W1", "W2", "W3", "W4", "W5")]}
        validate_reading_selection(value, {"W1", "W2", "W3", "W4", "W5"})

    def test_reading_selection_rejects_foreign_or_missing_candidates(self):
        value = {"rationale": "Compare the candidates.", "candidates": [
            {"work_id": "W3", "decision": "read", "rationale": "Read evidence."}]}
        with self.assertRaises(ValidationError): validate_reading_selection(value, {"W1", "W2"})
        value["candidates"][0]["work_id"] = "W1"
        with self.assertRaises(ValidationError): validate_reading_selection(value, {"W1", "W2"})

    def test_selection_preserves_valid_siblings_and_reports_all_identity_errors(self):
        choice = lambda wid, decision="read": {"work_id": wid, "decision": decision, "rationale": "Compare evidence."}
        value = {"rationale": "Investigate mechanisms.", "candidates": [
            choice("W1"), choice("W2"), choice("W2", "defer"), choice("W_BAD"), choice("W4")]}
        valid, issues = reading_selection_parts(value, {"W1", "W2", "W3", "W4"})
        self.assertEqual([row["work_id"] for row in valid], ["W1", "W4"])
        unresolved = {row["work_id"]: row for row in issues["unresolved"]}
        self.assertEqual(unresolved["W2"]["reason"], "duplicate")
        self.assertEqual([row["choice"]["decision"] for row in unresolved["W2"]["rows"]], ["read", "defer"])
        self.assertEqual(unresolved["W3"]["reason"], "missing")
        self.assertEqual(issues["unknown"][0]["choice"]["work_id"], "W_BAD")
        with self.assertRaisesRegex(ValidationError, "W2"):
            validate_reading_selection(value, {"W1", "W2", "W3", "W4"})

    def test_malformed_duplicate_cannot_resolve_a_conflicting_decision(self):
        value = {"rationale": "Investigate evidence.", "candidates": [
            {"work_id": "W1", "decision": "read", "rationale": "Read."},
            {"work_id": "W1", "decision": "defer", "rationale": "Defer.", "extra": True}]}
        valid, issues = reading_selection_parts(value, {"W1"})
        self.assertEqual(valid, [])
        self.assertEqual(issues["unresolved"][0]["reason"], "duplicate")
        with self.assertRaises(ValidationError): validate_reading_selection(value, {"W1"})

    def test_reading_selection_can_defer_every_candidate(self):
        value = {"rationale": "These candidates do not address the inquiry.", "candidates": [
            {"work_id": "W1", "decision": "defer", "rationale": "Different mechanism."}]}
        validate_reading_selection(value, {"W1"})


class TestExplorationExecution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.TestSurveyRunner.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixtures.TestSurveyRunner.tearDownClass()

    def setUp(self):
        from unittest.mock import patch
        from scisaurus.tests.test_survey import survey_work
        self.fixture = fixtures.TestSurveyRunner("test_abstract_only_run_retains_search_expansion_and_scoped_map")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        def work(wid):
            result = survey_work(wid)
            if wid == "W102":
                result["referenced_works"] = ["https://openalex.org/W103"]
            if wid == "W301" and self.config["model"]["model"] == "local-stop":
                result["referenced_works"].append("https://openalex.org/W201")
            return result
        self.metadata = patch("scisaurus.tests.test_survey.survey_work", side_effect=work)
        self.metadata_mock = self.metadata.start()
        self.addCleanup(self.metadata.stop)
        self.config = survey_config(self.fixture.endpoint)
        self.config["survey"]["search"].update(expansion_rounds=3, expansion_seed_count=3)

    def runner(self, *, resume_policy=None):
        runner = self.fixture.runtime(self.config, resume_policy=resume_policy)
        runner.worker_target = chain_worker
        return runner

    def test_checked_read_drives_child_and_grandchild(self):
        runner = self.runner()
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        control, store = self.fixture.open_store()
        tree = json.loads((runner.dir / "output/exploration-tree.json").read_text())
        reads = {node["work_id"]: node for node in tree["nodes"] if node["kind"] == "read"}
        self.assertTrue({"W101", "W102", "W103"} <= set(reads))
        for wid in ("W102", "W103"):
            child = reads[wid]
            acquisition = next(node for node in tree["nodes"] if node["id"] == child["parent_id"])
            parent = next(node for node in tree["nodes"] if node["id"] == acquisition["parent_id"])
            self.assertEqual(parent["kind"], "read")
            plan = store.get(acquisition["plan_ref"])
            query = store.get(acquisition["query_ref"])
            self.assertLess(store.get(parent["review_ref"])["created_at"], plan["created_at"])
            self.assertLess(plan["created_at"], query["created_at"])
            self.assertTrue(acquisition["evidence"])
            self.assertEqual(child["question"], acquisition["question"])
            self.assertEqual(child["inquiry_evidence"], acquisition["evidence"])
        self.assertFalse(tree["termination"]["exhaustive_coverage"])

    def test_resume_after_child_plan_reuses_parent_and_pending_query(self):
        from unittest.mock import patch
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        first = self.runner()
        original = first._checkpoint
        def checkpoint(phase, *args, **kwargs):
            original(phase, *args, **kwargs)
            if phase == "exploration_branches_planned" and first.exploration_tree["round"] == 2:
                raise KeyboardInterrupt()
        with patch.object(first, "_checkpoint", side_effect=checkpoint):
            paused = first.run()
        self.assertEqual(paused["status"], "paused", paused["error"])
        before = list(SurveyHTTPFixture.requests)
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        completed = self.runner(resume_policy=policy).run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertEqual(sum(row["path"] == "/works/W101" for row in SurveyHTTPFixture.requests), 1)
        self.assertEqual(sum(row["path"] == "/works/W102" for row in SurveyHTTPFixture.requests), 1)
        self.assertGreater(len(SurveyHTTPFixture.requests), len(before))
        control, store = self.fixture.open_store()
        maps = [prompt for _, prompt in self.fixture.model_contexts(control, store) if prompt["phase"] == "map"]
        self.assertEqual(sum(prompt["requested_work_ids"] == ["W101"] for prompt in maps), 1)

    def test_local_stop_keeps_other_reviewed_frontier(self):
        self.config["model"]["model"] = "local-stop"
        self.config["survey"]["seed_work_ids"] = ["W101", "W201"]
        self.config["survey"]["search"]["expansion_seed_count"] = 1
        runner = self.runner()
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        tree = result["coverage"]["exploration_tree"]
        stopped = next(node for node in tree["nodes"] if node["kind"] == "read" and node["work_id"] == "W101")
        self.assertEqual(stopped["decision"], "stop")
        self.assertTrue(any(node["kind"] == "read" and node["work_id"] == "W301" for node in tree["nodes"]))

    def test_committed_query_before_register_is_recovered_without_redispatch(self):
        from unittest.mock import patch
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        first = self.runner()
        update = first._update_register
        def interrupt():
            if len(first.query_refs) == 2:
                raise KeyboardInterrupt()
            return update()
        with patch.object(first, "_update_register", side_effect=interrupt):
            paused = first.run()
        self.assertEqual(paused["status"], "paused", paused["error"])
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        completed = self.runner(resume_policy=policy).run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertEqual(sum(row["query"].get("search") == ["recall timing"] for row in SurveyHTTPFixture.requests), 1)

    def test_reopened_focused_review_refreshes_pending_read_before_branching(self):
        from unittest.mock import patch
        first = self.runner()
        checkpoint = first._checkpoint
        def interrupt(phase, *args, **kwargs):
            checkpoint(phase, *args, **kwargs)
            if phase == "exploration_read_reviewed" and first.exploration_tree["round"] == 2:
                raise KeyboardInterrupt()
        with patch.object(first, "_checkpoint", side_effect=interrupt):
            paused = first.run()
        self.assertEqual(paused["status"], "paused", paused["error"])
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["focused_review"]}}
        completed = self.runner(resume_policy=policy).run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertTrue(any(node["kind"] == "read" and node["work_id"] == "W103"
                            for node in completed["coverage"]["exploration_tree"]["nodes"]))

    def test_identity_committed_before_register_is_retained(self):
        from unittest.mock import patch
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        self.config["survey"]["search"]["max_api_calls"] = 30
        self.config["survey"]["identity"] = {
            "id": "identity", "adapter": "crossref",
            "client": {"endpoint": self.fixture.endpoint, "timeout": 4, "max_bytes": 1000000,
                       "mailto": "catalog@example.org"},
            "representative": {"query": "10.1234/W101", "limit": 1}, "environment_files": []}
        first = self.runner()
        update = first._update_register
        def interrupt():
            if first.store.head("kb/identities/W101") is not None:
                raise KeyboardInterrupt()
            return update()
        with patch.object(first, "_update_register", side_effect=interrupt):
            paused = first.run()
        self.assertEqual(paused["status"], "paused", paused["error"])
        before = sum(row["query"].get("filter") == ["doi:10.1234/w101"] and row["query"].get("rows") == ["3"]
                     for row in SurveyHTTPFixture.requests)
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        completed = self.runner(resume_policy=policy).run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertEqual(before, 1)
        after = sum(row["query"].get("filter") == ["doi:10.1234/w101"] and row["query"].get("rows") == ["3"]
                    for row in SurveyHTTPFixture.requests)
        self.assertEqual(after, before)
        _, store = self.fixture.open_store()
        self.assertEqual(store.versions("kb/identities/W101"), [1])

    def test_completed_identity_execution_before_card_is_recovered(self):
        from unittest.mock import patch
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        self.config["survey"]["search"]["max_api_calls"] = 30
        self.config["survey"]["identity"] = {
            "id": "identity", "adapter": "crossref",
            "client": {"endpoint": self.fixture.endpoint, "timeout": 4, "max_bytes": 1000000,
                       "mailto": "catalog@example.org"},
            "representative": {"query": "10.1234/W101", "limit": 1}, "environment_files": []}
        first = self.runner()
        record = first._record
        def interrupt(logical, *args, **kwargs):
            if logical == "kb/identities/W101":
                raise KeyboardInterrupt()
            return record(logical, *args, **kwargs)
        with patch.object(first, "_record", side_effect=interrupt):
            paused = first.run()
        self.assertEqual(paused["status"], "paused", paused["error"])
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        completed = self.runner(resume_policy=policy).run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        self.assertEqual(sum(row["query"].get("filter") == ["doi:10.1234/w101"]
                             and row["query"].get("rows") == ["3"] for row in SurveyHTTPFixture.requests), 1)

    def test_new_follow_up_order_has_new_owned_root_and_search(self):
        completed = self.runner().run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["follow_up"]}}
        second = self.fixture.runtime(self.config, resume_policy=policy,
                                      work_orders=[self.fixture.follow_up_order()])
        second.worker_target = chain_worker
        result = second.run()
        self.assertEqual(result["status"], "completed", result["error"])
        _, store = self.fixture.open_store()
        tree = result["coverage"]["exploration_tree"]
        self.assertEqual(len([node for node in tree["nodes"] if node["kind"] == "root"]), 2)
        follow_up = tree["follow_up_ref"]
        roots = [node for node in tree["nodes"] if node["kind"] == "root" and node.get("follow_up_ref") == follow_up]
        self.assertEqual(len(roots), 1)
        searches = [node for node in tree["nodes"] if node["kind"] == "acquisition"
                    and node["parent_id"] == roots[0]["id"] and node.get("query_ref")
                    and node["request"]["operation"] == "search"]
        self.assertTrue(searches)
        for node in searches:
            query = json.loads(store.read_body(store.get(node["query_ref"])["body_hash"]))
            plan = json.loads(store.read_body(store.get(query["plan_ref"])["body_hash"]))
            self.assertEqual(plan["follow_up_ref"], follow_up)

    def test_updated_source_head_overlays_stale_register_capture(self):
        from unittest.mock import patch
        first = self.runner()
        result = first.run()
        self.assertEqual(result["status"], "completed", result["error"])
        policy = {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reopen", "reopen_scopes": ["mapping", "focused_review", "retrieval"]}}
        update = self.runner(resume_policy=policy)
        update._setup()
        original = update._update_register
        def interrupt():
            if update.store.head("kb/abstracts/W102")["version"] > 1:
                raise KeyboardInterrupt()
            return original()
        metadata = self.metadata_mock.side_effect
        def changed(wid):
            work = metadata(wid)
            if wid == "W102":
                work["abstract_inverted_index"]["Updated."] = [9]
            return work
        with patch("scisaurus.tests.test_survey.survey_work", side_effect=changed), \
             patch.object(update, "_update_register", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                update._bibliographic_call("work", role="research.search-planner", work_id="W102", result_limit=1)
        newest = update.store.head("kb/abstracts/W102")["artifact_ref"]
        update.control.close()
        restored = self.runner(resume_policy=policy)
        sources = [ref for ref, source in restored.source_docs.items() if source["work_id"] == "W102"]
        self.assertEqual(sources, [newest])
        recovered = restored.run()
        self.assertEqual(recovered["status"], "completed", recovered["error"])

    def policy(self):
        return {"additional_seconds": 40, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                "source_changes": {"mode": "reject", "reopen_scopes": []}}

    def test_full_catalog_recovers_committed_receipt_before_capacity_stop(self):
        from unittest.mock import patch
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        self.config["survey"]["search"].update(max_works=2)
        first = self.runner()
        update = first._update_register
        def interrupt():
            if first.query_refs: raise KeyboardInterrupt()
            return update()
        with patch.object(first, "_update_register", side_effect=interrupt): paused = first.run()
        self.assertEqual(paused["status"], "paused", paused["error"])
        result = self.runner(resume_policy=self.policy()).run()
        self.assertEqual(result["status"], "completed", result["error"])
        tree = result["coverage"]["exploration_tree"]
        self.assertTrue(any(n["kind"] == "read" and n["work_id"] == "W101" for n in tree["nodes"]))
        self.assertEqual(sum(r["path"] == "/works/W101" for r in SurveyHTTPFixture.requests), 1)

    def test_full_catalog_cannot_hide_unresolved_ingestion_charge(self):
        from unittest.mock import patch
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        self.config["survey"]["search"].update(max_works=2)
        first = self.runner()
        publish = first._publish
        def interrupt(logical, *args, **kwargs):
            if logical.startswith("kb/queries/"): raise KeyboardInterrupt()
            return publish(logical, *args, **kwargs)
        with patch.object(first, "_publish", side_effect=interrupt): paused = first.run()
        self.assertEqual(paused["status"], "paused", paused["error"])
        result = self.runner(resume_policy=self.policy()).run()
        self.assertNotEqual(result["status"], "completed")
        self.assertIn("unresolved charged reservation", result["error"])
        self.assertEqual(sum(r["path"] == "/works/W101" for r in SurveyHTTPFixture.requests), 1)

    def test_depth_limit_does_not_discard_planned_search_queue(self):
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        self.config["model"]["model"] = "queued-search"
        self.config["survey"]["seed_work_ids"] = []
        self.config["survey"]["search"].update(queries_per_role=2, results_per_query=1, expansion_rounds=0)
        result = self.runner().run()
        self.assertEqual(result["status"], "completed", result["error"])
        self.assertTrue(any(r["query"].get("search") == ["independent terminology"] for r in SurveyHTTPFixture.requests))
        self.assertTrue(any(n["kind"] == "read" and n["work_id"] == "W201"
                            for n in result["coverage"]["exploration_tree"]["nodes"]))

    def test_changed_follow_up_recovers_original_committed_search(self):
        from unittest.mock import patch
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        self.config["survey"]["seed_work_ids"] = []
        first = self.runner()
        update = first._update_register
        def interrupt():
            if first.query_refs: raise KeyboardInterrupt()
            return update()
        with patch.object(first, "_update_register", side_effect=interrupt): paused = first.run()
        self.assertEqual(paused["status"], "paused", paused["error"])
        second = self.fixture.runtime(self.config, resume_policy=self.policy(), work_orders=[self.fixture.follow_up_order()])
        second.worker_target = chain_worker
        result = second.run()
        self.assertEqual(result["status"], "completed", result["error"])
        tree = result["coverage"]["exploration_tree"]
        old = [n for n in tree["nodes"] if n["kind"] == "acquisition" and n.get("follow_up_ref") is None]
        self.assertTrue(old and all(n["state"] == "read" and n.get("query_ref") for n in old))
        self.assertEqual(sum(r["query"].get("search") == ["recall timing"] for r in SurveyHTTPFixture.requests), 2)

    def test_same_batch_reuses_receipt_and_does_not_credit_new_works_twice(self):
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        runner = self.runner();runner._initialize();runner._setup();runner._tree_load()
        root = runner.exploration_tree["nodes"][0]
        request = {"operation": "work", "query": None, "work_id": "W101", "cursor": None, "limit": 1}
        actions = [{"kind": "acquisition", "id": identity, "parent_id": root["id"], "depth": 1,
                    "state": "pending", "request": request, "question": "Read the recall study.",
                    "rationale": "Check the captured evidence.", "plan_ref": runner.protocol["artifact_ref"],
                    "follow_up_ref": None} for identity in ["first", "second"]]
        runner.exploration_tree["nodes"].extend(actions)
        runner._tree_acquire(actions)
        self.assertEqual(sum(r["path"] == "/works/W101" for r in SurveyHTTPFixture.requests), 1)
        self.assertEqual(sum(n["new_unique_works"] for n in actions), 1)
        self.assertEqual(actions[0]["query_ref"], actions[1]["query_ref"])
        # A distinct pending action's unknown charge cannot be settled by that cached receipt.
        third = {**actions[1], "id": "third", "state": "pending"}
        runner._active_tree_action = "third"
        runner._reserve_api_call("bibliography", request, "research.search-planner")
        runner._active_tree_action = None
        with self.assertRaises(StateError): runner._tree_recover_action(third)
        runner.control.close()

    def test_direct_intake_lookup_enters_checked_reference_exploration(self):
        self.config["model"]["model"] = "direct-intake"
        self.config["survey"]["seed_work_ids"] = []
        result = self.runner().run()
        self.assertEqual(result["status"], "completed", result["error"])
        nodes = result["coverage"]["exploration_tree"]["nodes"]
        self.assertTrue({"W101", "W102", "W103"} <= {n["work_id"] for n in nodes if n["kind"] == "read"})
        root = next(n for n in nodes if n["kind"] == "root")
        self.assertTrue(any(n["kind"] == "acquisition" and n["parent_id"] == root["id"]
                            and n["request"]["operation"] == "work" for n in nodes))

    def test_candidate_page_is_independent_of_reading_and_tree_count_limits(self):
        from scisaurus.tests.test_survey import SurveyHTTPFixture
        self.config["model"]["model"] = "select-independent"
        self.config["survey"]["seed_work_ids"] = []
        self.config["survey"]["search"].update(results_per_query=10, max_analyzed_works=2,
                                              expansion_rounds=0, expansion_seed_count=1)
        runner = self.runner()
        result = runner.run()
        self.assertEqual(result["status"], "completed", result["error"])
        queries = [r for r in SurveyHTTPFixture.requests if r["query"].get("search") == ["candidate comparison"]]
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]["query"]["per_page"], ["10"])
        control, store = self.fixture.open_store()
        assignments = [prompt for _, prompt in self.fixture.model_contexts(control, store)]
        selection = next(item for item in assignments if item["phase"] == "reading_selection")
        self.assertEqual({item["work_id"] for item in selection["candidates"]}, {"W101", "W201", "W301"})
        maps = [item["requested_work_ids"][0] for item in assignments if item["phase"] == "map"]
        self.assertIn("W201", maps)
        self.assertNotIn("W101", maps)
        self.assertNotIn("W301", maps)
        self.assertIsNone(result["coverage"]["deep_analysis_limit"])
        action = next(node for node in result["coverage"]["exploration_tree"]["nodes"]
                      if node["kind"] == "acquisition" and node["request"].get("query") == "candidate comparison")
        self.assertEqual(action["selected_work_ids"], ["W201"])
        self.assertTrue(action["selection_ref"])

    def test_reading_conflict_repair_preserves_valid_decisions(self):
        from unittest.mock import patch
        self.config["limits"]["max_rounds"] = 2
        runner = self.runner(); runner._initialize(); runner._setup(); runner._tree_load()
        root = runner.exploration_tree["nodes"][0]
        ids = ["W101", "W201", "W301"]
        for wid in ids:
            runner._bibliographic_call("work", role="research.search-planner", work_id=wid,
                                      result_limit=1, plan_ref=runner.protocol["artifact_ref"])
        action = {"kind": "acquisition", "id": "conflict", "parent_id": root["id"], "depth": 1,
                  "state": "captured", "question": "Compare recall studies.", "rationale": "Find relevant evidence.",
                  "query_ref": runner.query_refs[-1], "returned_work_ids": ids}
        runner.exploration_tree["nodes"].append(action)
        assignments = []
        def choose(name, role, assignment, validator, **kwargs):
            assignments.append(deepcopy(assignment))
            candidates = assignment["candidates"]
            choices = [{"work_id": row["work_id"], "decision": "read", "rationale": "Investigate evidence."} for row in candidates]
            if len(candidates) == 3:
                choices.append({"work_id": "W201", "decision": "defer", "rationale": "Conflicting initial decision."})
            result = {"rationale": "Resolve inquiry coverage.", "candidates": choices}
            if "retained_decisions" in assignment:
                result["read_priority"] = ["W201", "W101", "W301"]
            validator(result)
            return result, runner.protocol["artifact_ref"]
        with patch.object(runner, "_model_checked", side_effect=choose):
            runner._tree_select_reads([action])
        self.assertEqual(len(assignments), 2)
        self.assertEqual([row["work_id"] for row in assignments[1]["candidates"]], ["W201"])
        self.assertEqual({row["work_id"] for row in assignments[1]["retained_decisions"]}, {"W101", "W301"})
        self.assertEqual(action["selected_work_ids"], ["W201", "W101", "W301"])
        action.pop("selection_ref")
        with patch.object(runner, "_model_checked", side_effect=AssertionError("paid decisions replayed")):
            runner._tree_select_reads([action])
        runner.control.close()

    def test_interrupted_selection_recovers_checked_choice_without_another_call(self):
        from unittest.mock import patch
        first = self.runner()
        record = first._record
        def interrupt(logical, *args, **kwargs):
            result = record(logical, *args, **kwargs)
            if logical.startswith("kb/reading-selections/"):
                raise KeyboardInterrupt()
            return result
        with patch.object(first, "_record", side_effect=interrupt): paused = first.run()
        self.assertEqual(paused["status"], "paused", paused["error"])
        completed = self.runner(resume_policy=self.policy()).run()
        self.assertEqual(completed["status"], "completed", completed["error"])
        control, store = self.fixture.open_store()
        selections = [prompt for _, prompt in self.fixture.model_contexts(control, store) if prompt["phase"] == "reading_selection"]
        initial = [prompt for prompt in selections if {item["work_id"] for item in prompt["candidates"]} == {"W101"}]
        self.assertEqual(len(initial), 1)

    def test_checked_chain_ignores_legacy_depth_parent_and_paper_counts(self):
        self.config["survey"]["search"].update(max_analyzed_works=2, expansion_rounds=0, expansion_seed_count=1)
        result = self.runner().run()
        self.assertEqual(result["status"], "completed", result["error"])
        reads = {node["work_id"]: node for node in result["coverage"]["exploration_tree"]["nodes"] if node["kind"] == "read"}
        self.assertTrue({"W101", "W102", "W103"} <= set(reads))
        self.assertGreater(reads["W103"]["depth"], 1)

    def test_partially_deferred_page_is_reconsidered_with_fresh_inquiry(self):
        from unittest.mock import patch
        runner = self.runner(); runner._initialize(); runner._setup(); runner._tree_load()
        root = runner.exploration_tree["nodes"][0]
        def action(identity, ids):
            receipts = [runner._bibliographic_call("work", role="research.search-planner", work_id=wid,
                       result_limit=1, plan_ref=runner.protocol["artifact_ref"]) for wid in ids]
            node = {"kind": "acquisition", "id": identity, "parent_id": root["id"], "depth": 1,
                    "state": "captured", "request": {}, "question": "Investigate recall evidence.",
                    "rationale": "Compare the scientific candidates.", "query_ref": runner.query_refs[-1],
                    "plan_ref": runner.protocol["artifact_ref"], "follow_up_ref": None, "returned_work_ids": ids}
            runner.exploration_tree["nodes"].append(node)
            return node
        first = action("first", ["W101", "W201"])
        def choose(name, role, assignment, validator, **kwargs):
            value = {"rationale": "Prioritize the inquiry evidence.", "candidates": [
                {"work_id": item["work_id"], "decision": "defer" if item["work_id"] == "W201" else "read",
                 "rationale": "Retain candidates for the next inquiry."} for item in assignment["candidates"]]}
            validator(value)
            return value, runner.protocol["artifact_ref"]
        with patch.object(runner, "_model_checked", side_effect=choose): runner._tree_select_reads([first])
        first["state"] = "read"
        self.assertEqual(first["deferred_work_ids"], ["W201"])
        second = action("second", ["W301"])
        captured = []
        def reconsider(name, role, assignment, validator, **kwargs):
            captured.extend(item["work_id"] for item in assignment["candidates"])
            value = {"rationale": "Reassess alternatives using the fresh inquiry.", "candidates": [
                {"work_id": item["work_id"], "decision": "read", "rationale": "Advance the inquiry."}
                for item in assignment["candidates"]]}
            validator(value)
            return value, runner.protocol["artifact_ref"]
        with patch.object(runner, "_model_checked", side_effect=reconsider), \
             patch.object(runner, "_full_texts"), patch.object(runner, "_reconcile_identities"), \
             patch.object(runner, "_map"), patch.object(runner, "_review_work_claims"):
            runner._tree_read([second])
        self.assertIn("W201", captured)
        self.assertIn("W201", first["selected_work_ids"])
        runner.control.close()
