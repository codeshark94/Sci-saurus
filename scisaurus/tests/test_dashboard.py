import json
import re
import hashlib
from pathlib import Path
import tempfile
import threading
import shutil
import subprocess
import unittest
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import Request, urlopen

from scisaurus.core.schema import canonical_bytes
from scisaurus.dashboard.server import DashboardServer, DashboardService, DashboardSnapshot
from scisaurus.runtime.argument_defense import build_argument_defense
from scisaurus.runtime.research_program import build_research_program
from scisaurus.tests.test_argument_defense import argument
from scisaurus.tests.test_research_program import topic_package


class DashboardTests(unittest.TestCase):
    def test_partial_execution_keeps_the_complete_research_lifecycle(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        original = (root / "workflow.json").read_bytes()
        data = DashboardSnapshot(root).payload()
        self.assertEqual([stage["kind"] for stage in data["stage_results"]],
                         ["topic", "survey", "experiment", "interpretation", "argument", "paper"])
        self.assertEqual(len(data["pipeline"]["stages"]), 2)
        self.assertEqual(data["pipeline"]["total"], 2)
        future = data["stage_results"][2:]
        self.assertTrue(all(stage["status"] == "not_scheduled" for stage in future))
        self.assertTrue(all(not stage["scheduled"] and not stage["outputs"]
                            and not stage["review"]["bound"] for stage in future))
        self.assertEqual((root / "workflow.json").read_bytes(), original)

    def test_lifecycle_retains_custom_execution_stage_ids(self):
        from scisaurus.dashboard.server import _research_lifecycle_results
        original = [{"id": "lab_a", "kind": "experiment", "status": "completed"},
                    {"id": "lab_b", "kind": "experiment", "status": "pending"}]
        before = deepcopy(original)
        result = _research_lifecycle_results(original)
        experiments = [stage for stage in result if stage["kind"] == "experiment"]
        self.assertEqual([stage["id"] for stage in experiments], ["lab_a", "lab_b"])
        self.assertTrue(all(stage["scheduled"] for stage in experiments))
        self.assertEqual(original, before)

    def make_literature_project(self):
        from scisaurus.core.events import ControlStore
        from scisaurus.core.store import ArtifactStore
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        survey = root / "survey"
        survey.mkdir()
        workflow = json.loads((root / "workflow.json").read_text())
        workflow["stages"][1]["project_dir"] = str(survey)
        (root / "workflow.json").write_text(json.dumps(workflow))
        control = ControlStore(survey)
        self.addCleanup(control.close)
        store = ArtifactStore(control)
        store.init_project()
        def publish(logical, body, kind="note"):
            return store.publish_artifact(logical_id=logical, artifact_type=kind, author="research.cataloger",
                body=canonical_bytes(body), media_type="application/json")["artifact_ref"]
        works, sources = [], []
        for index in range(4):
            wid = f"W{index}"
            works.append(publish(f"kb/works/{wid}", {"work_id": wid, "title": f"Paper {index}",
                "doi": f"10.1000/paper{index}", "year": 2020 + index}))
            if index in {0, 1, 3}:
                sources.append(publish(f"kb/abstracts/{wid}", {"work_id": wid, "representation": "abstract",
                    "text": f"Captured abstract {index} α\r\nβ", "identity_verified": False,
                    "url": f"https://example.org/{wid}"}, "source_capture"))
            if index == 0:
                sources.append(publish(f"kb/full-text/{wid}", {"work_id": wid, "representation": "full_text",
                    "text": "Full Methods\nActual source text.", "identity_verified": True,
                    "url": f"https://example.org/{wid}.pdf"}, "source_capture"))
        publish("kb/work-register", {"work_refs": works, "source_refs": sources})
        return root, survey, control, store, publish

    def test_literature_stage_not_started_has_no_cross_mission_fallback(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        (root / "survey").mkdir()
        workflow = json.loads((root / "workflow.json").read_text())
        workflow["stages"][1]["project_dir"] = str(root / "survey")
        (root / "workflow.json").write_text(json.dumps(workflow))
        old = root / "old-mission" / "survey" / "objects" / "sha256"
        old.mkdir(parents=True)
        (old / ("a" * 64)).write_text('{"text":"Historical source"}')
        snapshot = DashboardSnapshot(root)
        data = snapshot.literature()
        self.assertEqual((data["status"], data["total"], data["items"]), ("not_started", 0, []))
        projected = snapshot.payload()
        stage = next(item for item in projected["stage_results"] if item["id"] == "survey")
        self.assertEqual(stage["outputs"], [])
        self.assertEqual(projected["literature"]["total"], 0)

    def test_literature_index_paginates_registered_sources_beyond_recent_artifacts(self):
        root, survey, control, store, publish = self.make_literature_project()
        for index in range(305):
            publish(f"command/unrelated/{index}", {"event": index})
        snapshot = DashboardSnapshot(root)
        def durable_hashes():
            return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in survey.rglob("*")
                    if path.is_file() and not path.name.endswith("-shm")}
        before = durable_hashes()
        with patch("subprocess.Popen", side_effect=AssertionError("provider process forbidden")):
            page = snapshot.literature(limit=2)
            next_page = snapshot.literature(offset=2, limit=2)
            full = snapshot.literature(evidence="full_text")
            searched = snapshot.literature(q="PAPER1")
            detail = snapshot.literature(work_id="W1")
        self.assertEqual(page["total"], 4)
        self.assertTrue(page["has_more"])
        self.assertFalse(next_page["has_more"])
        self.assertEqual(page["counts"], {"full_text": 1, "abstract_only": 2, "no_abstract": 1, "unknown": 0})
        self.assertEqual([item["work_id"] for item in full["items"]], ["W0"])
        self.assertEqual([item["work_id"] for item in searched["items"]], ["W1"])
        ref = detail["item"]["abstract_ref"]
        capture = json.loads(snapshot.file_payload(ref)["text"])
        self.assertEqual(capture["text"], "Captured abstract 1 α\r\nβ")
        self.assertFalse(detail["item"]["abstracts"][0]["identity_verified"])
        self.assertEqual(before, durable_hashes())
        with self.assertRaises(FileNotFoundError):
            snapshot.literature(work_id="W999")
        for arguments in ({"offset": -1}, {"limit": 101}, {"evidence": "verified"}):
            with self.assertRaises(ValueError):
                snapshot.literature(**arguments)

    def test_literature_access_and_invalid_source_provenance_are_not_success(self):
        root, survey, control, store, publish = self.make_literature_project()
        failure = {"kind": "full_text_failure", "work_id": "W1", "outcome": "access_denied",
                   "metadata": {"http_status": 403}, "execution_ref": "artifact:command/fetch@1"}
        publish("command/source-attempts/full-text/W1-1", {"work_id": "W1", "status": "access_unavailable", "failure": failure})
        publish("command/source-attempts/full-text/W2-1", {"work_id": "W2", "status": "reserved"})
        record = store.head("kb/abstracts/W3")
        (survey / "objects/sha256" / record["body_hash"]).write_text('{"text":"Tampered text"}')
        snapshot = DashboardSnapshot(root)
        items = {item["work_id"]: item for item in snapshot.literature()["items"]}
        self.assertEqual(items["W1"]["evidence_status"], "abstract_only")
        self.assertEqual(items["W1"]["access_status"], "access_denied")
        self.assertEqual(items["W1"]["access_failures"], [failure])
        self.assertEqual(items["W2"]["access_status"], "reserved")
        self.assertEqual(items["W2"]["access_failures"], [])
        self.assertEqual(items["W3"]["evidence_status"], "unknown")
        self.assertIsNone(items["W3"]["abstract_ref"])
        outside = root / "outside.json"
        outside.write_text('{"text":"Outside source"}')
        path = survey / "objects/sha256" / record["body_hash"]
        path.unlink()
        path.symlink_to(outside)
        self.assertIsNone(snapshot.literature(work_id="W3")["item"]["abstract_ref"])

    def test_literature_review_separates_claim_support_screening_and_stale_basis(self):
        root, survey, control, store, publish = self.make_literature_project()
        entry = publish("kb/work-analyses/W1", {"work_id": "W1", "inclusion": "included", "reason": "Evidence-based connection."})
        publish("kb/literature-map", {"question": "A bounded question", "entry_refs": [entry], "relationship_refs": []})
        checks = [{"check_id": key, "outcome": outcome, "method": "Inspect exact sources", "result": key}
                  for key, outcome in (("inclusion", "failed"), ("reason", "failed"), ("finding", "passed"))]
        review = publish("kb/work-reviews/W1", {"entry_ref": entry, "relationship_refs": [], "checks": checks,
            "evidence_scope": {"review_protocol": "literature-source-fidelity-2", "question": "A bounded question",
                               "owner_basis": [entry], "targets": {}},
            "rationale": "Claim support does not establish relevance.", "review_protocol": "literature-source-fidelity-2"})
        item = DashboardSnapshot(root).literature(work_id="W1")["item"]
        self.assertEqual(item["review"]["status"], "held")
        self.assertEqual(item["review"]["question_relevance_checks"], checks[:2])
        self.assertEqual(item["review"]["source_fidelity_checks"], checks[2:])
        self.assertEqual(item["review_ref"], item["review_output"]["file_ref"])
        publish("kb/work-analyses/W1", {"work_id": "W1", "inclusion": "uncertain", "reason": "Later revised evidence."})
        item = DashboardSnapshot(root).literature(work_id="W1")["item"]
        self.assertEqual(item["review"]["status"], "stale")
        self.assertFalse(item["analysis"]["current"])
        self.assertEqual(item["review"]["artifact_ref"], review)

    def test_catalog_verifies_each_exact_artifact_once_and_preserves_historical_sources(self):
        root, survey, control, store, publish = self.make_literature_project()
        entry = publish("kb/work-analyses/W1", {"work_id": "W1", "inclusion": "included"})
        publish("kb/literature-map", {"question": "Bounded question", "entry_refs": [entry]})
        publish("kb/work-reviews/W1", {"entry_ref": entry, "checks": [],
            "evidence_scope": {"review_protocol": "fixture", "question": "Bounded question",
                               "owner_basis": [entry] * 50, "targets": {}}, "review_protocol": "fixture"})
        old_source = store.head("kb/abstracts/W1")["artifact_ref"]
        publish("kb/abstracts/W1", {"work_id": "W1", "representation": "abstract",
                                  "text": "A newer capture", "identity_verified": False}, "source_capture")
        snapshot = DashboardSnapshot(root)
        with patch.object(snapshot, "_owned_artifact", wraps=snapshot._owned_artifact) as verify:
            data = snapshot.literature(work_id="W1")
        refs = [call.args[1]["artifact_ref"] for call in verify.call_args_list if call.args[1] is not None]
        self.assertEqual(len(refs), len(set(refs)))
        self.assertEqual(data["item"]["abstracts"][0]["artifact_ref"], old_source)
        self.assertIn("Captured abstract 1", snapshot.file_payload(data["item"]["abstract_ref"])["text"])

    def test_literature_accepted_pins_revoke_currentness_when_governing_head_changes(self):
        from scisaurus.tests.test_surveys import TestSurveyGate
        fixture = TestSurveyGate()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        survey = fixture.publish("kb/surveys/current", fixture.survey_body)
        review = fixture.review_for(survey)
        fixture.gate.accept(survey, review, author="strategy.survey-integrator")
        fixture.publish("kb/work-register", {"work_refs": [fixture.work], "source_refs": [fixture.source]})
        snapshot = DashboardSnapshot(fixture.directory.name)
        data = snapshot.literature()
        self.assertTrue(data["survey_current"])
        self.assertEqual(data["survey_output"]["artifact_ref"], survey)
        self.assertTrue(data["survey_output"]["file_ref"])
        fixture.publish("kb/map", {"entry_refs": [fixture.entry], "relationship_refs": []})
        updated = snapshot.literature()
        self.assertFalse(updated["survey_current"])
        self.assertEqual(updated["survey_ref"], survey)
        self.assertEqual(updated["survey_output"]["status"], "historical")

    def test_literature_http_contract_and_stage_files_are_owned(self):
        root, survey, control, store, publish = self.make_literature_project()
        (survey / "output").mkdir()
        (survey / "output" / "survey.json").write_text('{"status":"candidate_needs_review"}')
        server = DashboardServer(("127.0.0.1", 0), DashboardService(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(base + "/api/literature?evidence=abstract_only&limit=1") as response:
            page = json.loads(response.read())
        self.assertEqual(page["filtered_total"], 2)
        with urlopen(base + "/api/literature/detail?work_id=W0") as response:
            detail = json.loads(response.read())
        self.assertEqual(detail["item"]["evidence_status"], "full_text")
        with urlopen(base + "/api/snapshot") as response:
            snapshot = json.loads(response.read())
        stage = next(stage for stage in snapshot["stage_results"] if stage["id"] == "survey")
        output = next(item for item in stage["outputs"] if item["label"] == "survey.json")
        self.assertEqual(output["status"], "candidate_needs_review")
        self.assertEqual(output["file_ref"], "stage:survey::output/survey.json")
        self.assertIsNone(output["current"])

    def make_owned_topic_projection(self):
        from scisaurus.core.events import ControlStore
        from scisaurus.core.store import ArtifactStore
        from scisaurus.runtime.specialists import VERIFIER_SYSTEM, build_verifier_prompt
        root, survey, _, _, _ = self.make_literature_project()
        topic = root / "topic"
        (topic / "output").mkdir(parents=True)
        workflow = json.loads((root / "workflow.json").read_text())
        workflow["stages"][0].update(kind="topic_discovery", project_dir=str(topic))
        workflow["stages"].extend([{"id": "experiment", "kind": "experiment"},
                                    {"id": "interpretation", "kind": "interpretation"}])
        (root / "workflow.json").write_text(json.dumps(workflow))
        excerpt = "Exact retained excerpt α\r\n" * 50
        paper = {"work_id": "T1", "title": "Topic candidate paper", "abstract": excerpt, "authors": ["A. Author"],
                 "doi": "10.1000/topic", "year": 2024, "source_url": "https://openalex.org/T1", "matched_query": "A bounded query"}
        value = {"schema_version": "topic-discovery-1", "status": "completed", "selected_id": "selected",
                 "topic": {"id": "selected", "question": "Does A affect B?"},
                 "recent_papers": [paper, {"work_id": "T2", "title": "Metadata only", "year": 2020}],
                 "candidate_prior_work": [paper], "usage": {"model_calls": 1},
                 "sampling_trace": [{"returned_work_ids": ["T1", "T2", "T3"], "relevant_work_ids": ["T1", "T2"]}],
                 "candidate_sampling_trace": [{"returned_work_ids": ["T1", "T4"], "relevant_work_ids": ["T1", "T4"]}]}
        output = topic / "output/topic-discovery.json"
        output.write_bytes(canonical_bytes(value))
        child = {**value, "stage_id": "topic", "kind": "topic_discovery", "project_dir": str(topic), "output_path": str(output)}
        obligations = [{"target_stage_id": sid, "requirement": "Exact requirement " + sid,
                        "completion_check": "Independently check complete evidence.", "evidence_needed": ["Captured source and measurement."]}
                       for sid in ("survey", "experiment", "interpretation")]
        contract = {"current_stage_id": "topic", "downstream_stage_ids": ["survey", "experiment", "interpretation"],
                    "acceptance_target": "A bounded searchable question."}
        prompt = build_verifier_prompt({"id": "topic", "kind": "topic_discovery"},
            {"stage_acceptance_contract": contract}, [], child)
        execution = {"stage_id": "topic", "stage_kind": "topic_discovery", "project_id": str(root), "attempt_number": 1,
            "assigned_role": "research.adversarial-reviewer", "chief_result": child,
            "initial_review_input": {"prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "system": VERIFIER_SYSTEM},
            "report": {"status": "succeeded", "response": {"decision": "accept", "rationale": "Current stage reviewed.",
                "deferred_obligations": obligations}, "request_inputs": [{"input": {"prompt": prompt, "system": VERIFIER_SYSTEM}}]}}
        control = ControlStore(root)
        self.addCleanup(control.close)
        store = ArtifactStore(control)
        store.init_project()
        store.publish_artifact(logical_id="command/model-work/" + "a" * 64, artifact_type="note", author="command.controller",
            body=canonical_bytes({"status": "succeeded", "result": child,
                                  "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}), media_type="application/json")
        logical = "command/departments/research/assignments/topic/attempt-1/verifier-adversarial-reviewer/execution"
        def publish(body):
            return store.publish_artifact(logical_id=logical, artifact_type="report", author="research.adversarial-reviewer",
                body=canonical_bytes(body), media_type="application/json")["artifact_ref"]
        ref = publish(execution)
        progress_path = root / "output/progress.json"
        progress = json.loads(progress_path.read_text())
        progress["stages"]["topic"].update(status="completed", attempt_number=1, verifier_agent="research.adversarial-reviewer")
        progress["context"] = {"topic": child}
        progress_path.write_text(json.dumps(progress))
        return root, output, value, obligations, execution, publish, ref

    def test_owned_topic_papers_and_target_obligations_keep_excerpt_scope(self):
        root, output, original, obligations, execution, publish, ref = self.make_owned_topic_projection()
        data = DashboardSnapshot(root).payload()
        stages = {stage["id"]: stage for stage in data["stage_results"]}
        self.assertEqual(stages["topic"]["open_obligations"], obligations)
        self.assertEqual(stages["topic"]["review"]["deferred_obligations"], obligations)
        self.assertEqual(stages["topic"]["review"]["artifact_ref"], ref)
        for obligation in obligations:
            self.assertEqual(stages[obligation["target_stage_id"]]["open_obligations"], [obligation])
        papers = data["topic_papers"]
        self.assertEqual(papers["total"], 2)
        self.assertEqual(papers["search_summary"], {"query_count": 2, "returned_work_count": 4, "keyword_matched_work_count": 3})
        self.assertEqual(papers["items"][0]["origins"], ["recent_papers", "candidate_prior_work"])
        self.assertEqual(papers["items"][0]["abstract"], original["recent_papers"][0]["abstract"])
        self.assertTrue(papers["items"][0]["abstract_is_excerpt"])
        self.assertEqual(papers["items"][0]["evidence_status"], "abstract_excerpt")
        self.assertEqual(papers["items"][1]["evidence_status"], "metadata_only")
        self.assertEqual(papers["output_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(data["literature"]["total"], 4)
        self.assertEqual(papers["items"][0]["source_stage_kind"], "topic_discovery")
        self.assertNotIn("full_text_ref", papers["items"][0])

    def test_topic_obligations_and_papers_reject_unpaired_or_malformed_owned_review(self):
        root, output, original, obligations, execution, publish, ref = self.make_owned_topic_projection()
        for mutation in ("science", "wire", "target", "contradiction"):
            with self.subTest(mutation=mutation):
                bad = deepcopy(execution)
                if mutation == "science":
                    bad["chief_result"]["topic"]["question"] = "A different scientific question."
                elif mutation == "wire":
                    bad["initial_review_input"]["prompt_sha256"] = "0" * 64
                elif mutation == "target":
                    bad["report"]["response"]["deferred_obligations"][0]["target_stage_id"] = "unowned"
                else:
                    bad["report"]["response"]["blocking_findings"] = ["Unresolved current-stage defect."]
                publish(bad)
                data = DashboardSnapshot(root).payload()
                self.assertEqual(data["topic_papers"]["total"], 0)
                self.assertTrue(all(not stage["open_obligations"] for stage in data["stage_results"]))
                self.assertFalse(data["stage_results"][0]["review"]["bound"])
        publish(execution)
        for tampered in ({**original, "selected_id": "different"}, {key: value for key, value in original.items() if key != "recent_papers"}):
            output.write_bytes(canonical_bytes(tampered))
            data = DashboardSnapshot(root).payload()
            self.assertEqual(data["topic_papers"]["total"], 0)
            self.assertEqual(data["topic_papers"]["status"], "unavailable")

    def test_owned_topic_hold_keeps_producer_papers_and_deferred_obligations(self):
        root, output, original, obligations, execution, publish, ref = self.make_owned_topic_projection()
        held = deepcopy(execution)
        verdict = held["report"]["response"]
        verdict.update(decision="hold", rationale="The current proposal needs further review.",
                       blocking_findings=["Clarify the proposed scope."])
        ref = publish(held)
        path = root / "output/progress.json"
        progress = json.loads(path.read_text())
        progress["stages"]["topic"]["status"] = "candidate_needs_review"
        progress["context"]["topic"].update(status="candidate_needs_review",
            deferred_review_findings=deepcopy(verdict),
            review_revalidation={"admission_ref": "artifact:command/admission@1",
                                 "producer_calls_replayed": 0, "peer_calls_replayed": 0,
                                 "prior_verifier_execution_ref": execution["stage_id"]})
        path.write_text(json.dumps(progress))

        data = DashboardSnapshot(root).payload()
        stages = {stage["id"]: stage for stage in data["stage_results"]}
        self.assertEqual(stages["topic"]["status"], "candidate_needs_review")
        self.assertTrue(stages["topic"]["review"]["bound"])
        self.assertEqual(stages["topic"]["review"]["decision"], "hold")
        self.assertEqual(stages["topic"]["review"]["artifact_ref"], ref)
        self.assertEqual(stages["topic"]["review"]["blocking_findings"], verdict["blocking_findings"])
        self.assertEqual(stages["topic"]["open_obligations"], obligations)
        for obligation in obligations:
            self.assertEqual(stages[obligation["target_stage_id"]]["open_obligations"], [obligation])
        self.assertEqual(data["topic_papers"]["total"], 2)
        self.assertEqual(data["topic_papers"]["items"][0]["abstract"], original["recent_papers"][0]["abstract"])
        self.assertEqual(json.loads(output.read_bytes())["status"], "completed")

        for mutation in ("question", "candidate", "papers", "output_path"):
            with self.subTest(mutation=mutation):
                changed = deepcopy(progress)
                current = changed["context"]["topic"]
                if mutation == "question":
                    current["topic"]["question"] = "A different question."
                elif mutation == "candidate":
                    current["selected_id"] = "different"
                elif mutation == "papers":
                    current["recent_papers"][0]["abstract"] = "Different captured content."
                else:
                    current["output_path"] = str(output.parent / "unowned.json")
                path.write_text(json.dumps(changed))
                invalid = DashboardSnapshot(root).payload()
                self.assertFalse(invalid["stage_results"][0]["review"]["bound"])
                self.assertEqual(invalid["topic_papers"]["total"], 0)
                self.assertTrue(all(not stage["open_obligations"] for stage in invalid["stage_results"]))

    def test_resumed_owner_does_not_revive_closed_specialist_calls(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        snapshot = DashboardSnapshot(root)
        path = root / "output/progress.json"
        progress = json.loads(path.read_text())
        progress["stages"]["survey"]["specialist_live"] = {
            "research.search-strategist": {
                "event": "dispatched", "status": "running", "task_id": "task-survey-1",
                "role": "research.search-strategist", "model": "fixture"},
            "research.reviewer": {
                "event": "dispatched", "status": "running", "task_id": "current-call",
                "role": "research.reviewer", "model": "fixture"}}
        path.write_text(json.dumps(progress))
        original = path.read_bytes()
        db = snapshot._db_records()
        db["tasks"] = [{"task_id": "task-survey-1", "root_key": "project",
                        "state": "blocked", "payload": {"assignment_id": "assignment-1",
                        "assigned_role": "research.search-strategist", "stage_id": "survey"}},
                       {"task_id": "current-call", "root_key": "project",
                        "state": "running", "payload": {"assignment_id": "assignment-2"}}]
        recorded_db = deepcopy(db)
        with patch.object(snapshot, "_db_records", return_value=db), \
                patch.object(snapshot, "_processes", return_value=[{"pid": 123, "owns_execution": True}]):
            payload = snapshot.payload()
        self.assertTrue(payload["runtime"]["execution_observed"])
        self.assertEqual(payload["model_calls"]["active"], 1)
        self.assertEqual(payload["model_calls"]["live_items"][0]["task_id"], "current-call")
        historical = next(item for item in payload["model_calls"]["recent_items"]
                          if item["task_id"] == "task-survey-1")
        self.assertEqual(historical["state"], "blocked")
        self.assertIsNone(historical["finished_at"])
        self.assertEqual(payload["specialists_active"], 0)
        self.assertEqual(payload["organization"]["active_assignments"], [])
        historical_work = next(item for item in payload["recent_work"]
                               if item["task_id"] == "task-survey-1")
        self.assertFalse(historical_work["current"])
        self.assertEqual(payload["pipeline"]["stages"][1]["active_agents"], [])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(db, recorded_db)

    def test_inactive_owner_projects_all_activity_without_changing_recorded_evidence(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        snapshot = DashboardSnapshot(root)
        path = root / "output/progress.json"
        original = json.loads(path.read_text())
        original["usage"] = {"model_calls": 7}
        original["stages"]["survey"]["specialist_live"] = {
            "reviewer": {"event": "dispatched", "status": "running", "task_id": "checkpoint-call",
                         "role": "research.reviewer", "model": "fixture", "execution_mode": "model"}}
        db = snapshot._db_records()
        db["tasks"] = [
            {"task_id": "model", "root_key": "project", "state": "running", "payload": {"operation": "model"}},
            {"task_id": "search", "root_key": "project", "state": "running", "payload": {"operation": "crossref"}},
            {"task_id": "task-survey-1", "root_key": "project", "state": "running", "payload": {
                "assignment_id": "assignment-1", "assigned_role": "research.search-strategist",
                "stage_id": "survey", "assignment_phase": "specialist"}},
        ]
        db["counts"]["active_tasks"] = 3
        recorded_db = deepcopy(db)
        for status, processes in (("paused", []), ("paused", [{"pid": 123}]),
                                  ("completed", [{"pid": 123}]), ("blocked", []), ("running", [])):
            with self.subTest(status=status, process_observed=bool(processes)):
                progress = {**original, "status": status}
                path.write_text(json.dumps(progress))
                recorded = path.read_bytes()
                with patch.object(snapshot, "_db_records", return_value=db), \
                        patch.object(snapshot, "_processes", return_value=processes):
                    payload = snapshot.payload()
                self.assertFalse(payload["runtime"]["execution_observed"])
                self.assertEqual(payload["model_calls"]["active"], 0)
                self.assertEqual(payload["model_calls"]["history_total"], 2)
                self.assertTrue(all(item["finished_at"] is None for item in payload["model_calls"]["recent_items"]))
                self.assertEqual(payload["provider_work"]["active"], 0)
                self.assertEqual(payload["specialists_active"], 0)
                self.assertEqual(payload["counts"]["active_tasks"], 0)
                self.assertEqual(payload["counts"]["unfinished_tasks"], 3)
                self.assertEqual(payload["execution"]["role_assignments"]["active"], 0)
                self.assertEqual(payload["execution"]["provider"]["running_tasks"], 0)
                self.assertIsNone(payload["live"]["current_activity"])
                self.assertTrue(all(not row["current"] for row in payload["recent_work"]))
                self.assertEqual(payload["pipeline"]["stages"][0]["status"], "completed")
                self.assertEqual(payload["pipeline"]["stages"][1]["active_agents"], [])
                self.assertEqual(payload["live"]["usage"], {"model_calls": 7})
                self.assertEqual(path.read_bytes(), recorded)
                self.assertEqual(db, recorded_db)

    def test_owner_process_requires_runner_entrypoint_and_exact_project_binding(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        snapshot = DashboardSnapshot(root)
        workflow = root / "workflow.json"
        for command in (
            f"/usr/bin/python3 -u -m scisaurus.cli run-composer --workflow {workflow} --resume --watch",
            f"/usr/bin/python3 -m scisaurus run-composer --workflow={workflow}",
            f"/usr/local/bin/scisaurus run-composer --workflow '{workflow}' --watch",
        ):
            self.assertTrue(snapshot._command_owns_execution(command), command)
        for command in (
            f"/usr/bin/python3 -c 'import time; time.sleep(5)' {root}",
            f"/usr/bin/python3 -c 'print(\"run-composer --workflow {workflow}\")'",
            f"/usr/bin/python3 -m scisaurus.cli dashboard {root}",
            f"/usr/bin/python3 -m scisaurus.cli run-composer --workflow {workflow}.other",
            f"/usr/bin/python3 -m scisaurus.cli run-composer --workflow {root / 'other.json'} {workflow}",
        ):
            self.assertFalse(snapshot._command_owns_execution(command), command)

    def make_project(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "output").mkdir()
        (root / "workflow.json").write_text(json.dumps({
            "workflow_id": "dashboard-test",
            "objective": "Check a bounded research mission.",
            "project_id": str(root),
            "stages": [
                {"id": "topic", "kind": "topic"},
                {"id": "survey", "kind": "survey"},
            ],
        }), encoding="utf-8")
        (root / "README.md").write_text("# Dashboard fixture\n", encoding="utf-8")
        (root / "run.log").write_text(json.dumps({
            "phase": "survey:specialists_admitted", "status": "running",
        }) + "\n", encoding="utf-8")
        (root / "output" / "progress.json").write_text(json.dumps({
            "status": "running",
            "phase": "survey:specialists_admitted",
            "elapsed_seconds": 42,
            "organization": {
                "schema_version": "project-organization-2",
                "departments": [{"id": "research", "label": "Research", "chief": "chief", "adversary": "adversary"}],
                "agents": [
                    {"id": "research.chief", "department": "research", "appointment": "chief", "label": "Chief"},
                    {"id": "research.search-strategist", "department": "research", "appointment": "specialist", "label": "Search strategist"},
                ],
                "active_assignments": [{
                    "assigned_role": "research.search-strategist", "task_id": "task-survey-1",
                    "stage_id": "survey", "task_state": "running", "attempt_state": "started",
                }],
            },
            "stages": {
                "topic": {"status": "completed", "attempt_count": 1},
                "survey": {
                    "status": "running",
                    "attempt_count": 2,
                    "active_agents": ["research.search-strategist"],
                    "required_agents": ["research.search-strategist"],
                    "verifier_agent": "research.fact-verifier",
                    "assignment_task_ids": ["task-survey-1"],
                },
            },
        }), encoding="utf-8")
        return temporary, root

    def test_live_usage_prefers_observed_costs_and_parent_stage_authority(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        progress_path = root / "output" / "progress.json"
        progress = json.loads(progress_path.read_text())
        progress["usage"] = {"model_calls": 141}
        progress["observed_usage"] = {"model_calls": 193}
        progress_path.write_text(json.dumps(progress))
        snapshot = DashboardSnapshot(root)
        payload = snapshot.payload()
        self.assertEqual(payload["live"]["usage"]["model_calls"], 193)
        from unittest.mock import patch
        live = {"value": progress, "modified": 1, "root_key": "project"}
        snapshot.roots["stage:survey"] = root / "survey"
        newer_child = {"root_key": "stage:survey", "modified": 2,
                       "value": {"phase": "work-review", "active_tasks": ["work-review"]}}
        with patch.object(snapshot, "_checkpoint_files", return_value=[newer_child]):
            stage = snapshot._stage_summary({"id": "survey", "kind": "survey"}, live)
        self.assertEqual(stage["status"], "running")
        self.assertEqual(stage["active_agents"], ["research.search-strategist"])
        from scisaurus.cli import _composer_progress_line
        self.assertEqual(json.loads(_composer_progress_line(progress))["usage"]["model_calls"], 193)

    def test_snapshot_is_read_only_and_tracks_live_checkpoint(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)

        with patch.object(DashboardSnapshot, "_processes", return_value=[{"pid": 123, "command": "fixture owner", "owns_execution": True}]):
            snapshot = DashboardSnapshot(root).payload()

        self.assertEqual(snapshot["live"]["status"], "running")
        self.assertEqual(snapshot["live"]["current_stage"], "survey")
        self.assertEqual(snapshot["pipeline"]["completed"], 1)
        self.assertEqual(snapshot["pipeline"]["total"], 2)
        self.assertEqual(snapshot["specialists"][0]["role"], "research.search-strategist")
        self.assertEqual(snapshot["specialists_roster"], 2)
        self.assertEqual(snapshot["specialists_active"], 1)
        self.assertTrue(snapshot["integrity"]["read_only"])
        self.assertTrue(any(item["kind"] == "runtime_log" for item in snapshot["logs"]))
        self.assertTrue(any(item["path"] == "output" for item in snapshot["structure"]["directories"]))
        self.assertTrue(any(item["ref"] == "project::output/progress.json"
                            for item in snapshot["checkpoints"]))
        self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "# Dashboard fixture\n")

    def test_snapshot_does_not_present_recovered_blocker_history_as_live(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        progress_path = root / "output" / "progress.json"
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        progress["blockers"] = [
            {"stage_id": "survey", "reason": "old failure", "recovery": "cycle_admitted"},
            {"stage_id": "survey", "reason": "forwarded finding",
             "gating": False, "release_blocking": False},
        ]
        progress["stages"]["survey"]["status"] = "running"
        progress_path.write_text(json.dumps(progress), encoding="utf-8")

        snapshot = DashboardSnapshot(root).payload()

        self.assertEqual(snapshot["live"]["blockers"], [])
        self.assertEqual(snapshot["live"]["blocker_counts"], {"active": 0, "historical": 2})
        overview = DashboardService(root).workspace()
        self.assertEqual(overview["projects"][0]["blocker_count"], 0)
        self.assertEqual(overview["projects"][0]["historical_blocker_count"], 2)

    def test_snapshot_discovers_latest_retry_workspace_from_stage_attempt_ledger(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        active = root / "survey-attempt-2"
        active.mkdir()
        live = json.loads((root / "output" / "progress.json").read_text(encoding="utf-8"))
        live["stages"]["survey"]["status"] = "retrying"
        live["stages"]["survey"]["attempts"] = [{
            "attempt_number": 2, "state": "failed", "project_dir": str(active),
        }]
        for status in ("retrying", "paused", "completed"):
            with self.subTest(status=status):
                live["stages"]["survey"]["status"] = status
                (root / "output" / "progress.json").write_text(json.dumps(live), encoding="utf-8")
                snapshot = DashboardSnapshot(root).payload()
                self.assertTrue(any(item["key"] == "stage:survey:active"
                                    and item["path"] == str(active.resolve())
                                    for item in snapshot["project"]["roots"]))
                survey_stage = next(item for item in snapshot["pipeline"]["stages"] if item["id"] == "survey")
                self.assertEqual(survey_stage["project_dir"], str(active.resolve()))

    def test_snapshot_exposes_research_program_and_argument_defense(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        program = build_research_program(topic_package())
        defense = build_argument_defense(
            argument(), {"evidence_ids": ["e1", "e2", "e3"]}, research_program=program)
        live = json.loads((root / "output" / "progress.json").read_text(encoding="utf-8"))
        live["context"] = {
            "topic": {"research_program": program},
            "argument": {"argument_package": {"argument_defense": defense}},
        }
        (root / "output" / "progress.json").write_text(
            json.dumps(live), encoding="utf-8")

        snapshot = DashboardSnapshot(root).payload()
        research = snapshot["research"]
        self.assertEqual(research["research_program"]["schema_version"], "research-program-1")
        self.assertEqual(research["research_program"]["selected_id"], "branch_1")
        self.assertEqual(len(research["research_program"]["branches"]), 3)
        self.assertEqual(research["argument_defense"]["schema_version"], "argument-defense-1")
        self.assertEqual(research["argument_defense"]["claim_count"], 4)
        self.assertTrue(research["argument_defense"]["weak_points"])

    def test_snapshot_projects_real_model_calls_from_call_artifacts(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        objects = root / "objects" / "sha256"
        objects.mkdir(parents=True)

        def artifact(logical_id, body, task_id=None):
            encoded = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            body_hash = hashlib.sha256(encoded).hexdigest()
            (objects / body_hash).write_bytes(encoded)
            return {
                "root_key": "project", "logical_id": logical_id, "version": 1,
                "artifact_ref": f"artifact:{logical_id}@1", "artifact_type": "execution",
                "body_hash": body_hash, "file_ref": f"project::objects/sha256/{body_hash}",
                "task_id": task_id, "created_at": "2026-09-16T00:00:02+00:00",
            }

        running_id = "model-running"
        review_id = "model-review"
        completed_id = "model-completed"
        running_context = artifact(
            f"command/contexts/{running_id}",
            {"role": "research.literature-mapper", "provider_pool": "qwen", "route_id": "qwen-tailnet", "client": {
                "model": "fallback-model", "base_url": "http://127.0.0.1:11434/v1",
                "cache_prompt": True,
                "role_models": {"research.literature-mapper": {
                    "model": "qwen3.8-27b",
                    "base_url": "https://desktop-br7ukeg.taila57d41.ts.net/v1",
                }},
            }},
        )
        completed_context = artifact(
            f"command/contexts/{completed_id}",
            {"role": "review.methods", "client": {
                "model": "fallback-model", "base_url": "http://127.0.0.1:11434/v1",
                "cache_prompt": True,
            }},
        )
        review_context = artifact(
            f"command/contexts/{review_id}",
            {"role": "research.fact-verifier", "client": {
                "model": "review-model", "base_url": "http://127.0.0.1:11434/v1",
            }},
        )
        completed_execution = artifact(
            f"command/executions/{completed_id}",
            {"model": "glm-5.3-flash:cloud", "elapsed_seconds": 2.5,
             "finish_reason": "stop", "usage": {
                 "model_calls": 1, "input_tokens": 100, "output_tokens": 25,
                 "cache_read_tokens": 64,
             }},
        )
        db = {
            "tasks": [
                {"root_key": "project", "task_id": running_id, "state": "running",
                 "updated_at": "2026-09-16T00:00:03+00:00", "payload": {"operation": "model"}},
                {"root_key": "project", "task_id": review_id, "state": "awaiting_review",
                 "updated_at": "2026-09-16T00:00:02+00:00", "payload": {"operation": "model"}},
                {"root_key": "project", "task_id": completed_id, "state": "completed",
                 "updated_at": "2026-09-16T00:00:02+00:00", "payload": {"operation": "model"}},
            ],
            "attempts": [
                {"root_key": "project", "task_id": running_id, "attempt_id": "a-running",
                 "state": "started", "lease_owner": "research.literature-mapper",
                 "created_at": "2026-09-16T00:00:03+00:00", "finished_at": None, "usage": {}},
                {"root_key": "project", "task_id": review_id, "attempt_id": "a-review",
                 "state": "started", "lease_owner": "research.fact-verifier",
                 "created_at": "2026-09-16T00:00:02+00:00", "finished_at": None, "usage": {}},
                {"root_key": "project", "task_id": completed_id, "attempt_id": "a-completed",
                 "state": "succeeded", "lease_owner": "review.methods",
                 "created_at": "2026-09-16T00:00:01+00:00",
                 "finished_at": "2026-09-16T00:00:02+00:00", "usage": {}},
            ],
            "artifacts": [running_context, review_context, completed_context, completed_execution],
        }

        calls = DashboardSnapshot(root)._model_calls(db)

        self.assertEqual(calls["active"], 1)
        self.assertEqual(calls["review_pending"], 1)
        self.assertEqual([item["task_id"] for item in calls["items"]], [running_id])
        self.assertEqual([item["task_id"] for item in calls["review_items"]], [review_id])
        self.assertEqual([item["task_id"] for item in calls["recent_items"]], [completed_id])
        self.assertEqual(calls["items"][0]["model"], "qwen3.8-27b")
        self.assertEqual(calls["items"][0]["provider"], "Tailnet")
        self.assertEqual(calls["items"][0]["provider_pool"], "qwen")
        self.assertEqual(calls["items"][0]["route_id"], "qwen-tailnet")
        self.assertEqual(calls["items"][0]["cache"]["status"], "enabled · unreported")
        self.assertEqual(calls["recent_items"][0]["model"], "glm-5.3-flash:cloud")
        self.assertEqual(calls["recent_items"][0]["response_status"], "response recorded")
        self.assertEqual(calls["recent_items"][0]["cache"], {
            "requested": True, "status": "partial", "read_tokens": 64, "write_tokens": None,
            "read_ratio": 0.64,
        })
        self.assertEqual(calls["recent_items"][0]["usage"]["cache_read_tokens"], 64)
        self.assertTrue(calls["recent_items"][0]["response_ref"].startswith("project::objects/sha256/"))

    def test_snapshot_projects_live_composer_specialist_calls(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        snapshot = DashboardSnapshot(root)
        live = {
            "stages": {"survey": {"specialist_live": {
                "research.cataloger": {
                    "event": "dispatched", "execution_mode": "model",
                    "task_id": "specialist-survey-cataloger", "stage_id": "survey",
                    "model": "qwen3.8-27b", "base_url": "https://desktop-br7ukeg.taila57d41.ts.net/v1",
                    "provider_pool": "qwen", "route_id": "qwen-bulk",
                    "cache_prompt": True, "observed_at": "2026-09-16T00:00:03+00:00",
                },
            }}}
        }
        calls = snapshot._model_calls({"tasks": [], "attempts": [], "artifacts": []}, live)

        self.assertEqual(calls["active"], 1)
        self.assertEqual(calls["total"], 1)
        self.assertEqual(calls["live_items"][0]["role"], "research.cataloger")
        self.assertEqual(calls["live_items"][0]["provider"], "Tailnet")
        self.assertEqual(calls["live_items"][0]["response_status"], "awaiting response")
        self.assertEqual(calls["live_items"][0]["cache"]["status"], "enabled · unreported")

    def test_snapshot_projects_live_non_model_provider_work(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        snapshot = DashboardSnapshot(root)
        db = {
            "tasks": [
                {
                    "root_key": "stage:survey:active",
                    "task_id": "crossref-running",
                    "kind": "retrieval",
                    "state": "running",
                    "updated_at": "2026-09-16T00:00:03+00:00",
                    "payload": {
                        "operation": "crossref",
                        "objective": "Reconcile the DOI identity for the selected work.",
                        "work_id": "W123",
                    },
                },
                {
                    "root_key": "stage:survey:active",
                    "task_id": "logical-assignment",
                    "kind": "production",
                    "state": "running",
                    "updated_at": "2026-09-16T00:00:04+00:00",
                    "payload": {
                        "operation": "crossref",
                        "assignment_id": "assignment-1",
                    },
                },
            ],
            "attempts": [{
                "root_key": "stage:survey:active",
                "task_id": "crossref-running",
                "attempt_id": "attempt-crossref",
                "state": "started",
                "lease_owner": "research.identity-checker",
                "created_at": "2026-09-16T00:00:01+00:00",
                "finished_at": None,
                "usage": {"retrieval_calls": 1},
            }],
        }

        work = snapshot._provider_work(db)

        self.assertEqual(work["active"], 1)
        self.assertEqual(work["running"], 1)
        self.assertEqual(work["live_items"][0]["operation"], "crossref")
        self.assertEqual(work["live_items"][0]["label"], "Crossref identity")
        self.assertEqual(work["live_items"][0]["role"], "research.identity-checker")
        self.assertEqual(work["live_items"][0]["stage_id"], "survey")
        self.assertEqual(work["live_items"][0]["target"], "W123")

        execution = snapshot._execution_view(
            [{"id": "survey", "kind": "survey", "status": "running"}], db, {})
        self.assertEqual(execution["provider"]["running_tasks"], 1)
        self.assertEqual(execution["provider"]["queued_tasks"], 0)

    def test_workspace_overview_is_lightweight_and_project_scoped(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)

        overview = DashboardService(root).workspace()

        self.assertEqual(overview["schema_version"], "dashboard-workspace-1")
        self.assertEqual(overview["workspace"]["name"], "Sci-saurus")
        self.assertEqual(overview["summary"]["total_projects"], 1)
        self.assertEqual(overview["summary"]["stale_projects"], 1)
        self.assertEqual(overview["projects"][0]["current_stage"], "survey")
        self.assertEqual(overview["projects"][0]["completed_stages"], 1)
        self.assertEqual(overview["projects"][0]["total_stages"], 2)
        self.assertEqual(overview["active_runs"], [])
        self.assertEqual(overview["recent_projects"][0]["ref"], ".")
        self.assertNotIn("artifacts", overview)

    def test_http_snapshot_file_preview_and_path_boundary(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        object_path = root / "objects" / "sha256" / ("a" * 64)
        object_path.parent.mkdir(parents=True)
        object_path.write_text(json.dumps({"role": "research.search-strategist", "outcome": None}), encoding="utf-8")
        server = DashboardServer(("127.0.0.1", 0), DashboardService(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        base_url = f"http://127.0.0.1:{server.server_port}"

        with urlopen(f"{base_url}/api/snapshot", timeout=2) as response:
            snapshot = json.loads(response.read())
        self.assertEqual(snapshot["schema_version"], "dashboard-snapshot-1")
        self.assertEqual(snapshot["project"]["name"], root.name)

        with urlopen(f"{base_url}/api/workspace", timeout=2) as response:
            workspace = json.loads(response.read())
        self.assertEqual(workspace["schema_version"], "dashboard-workspace-1")
        self.assertEqual(workspace["projects"][0]["ref"], ".")

        with urlopen(f"{base_url}/api/file?ref={quote('project::README.md')}", timeout=2) as response:
            preview = json.loads(response.read())
        self.assertEqual(preview["text"], "# Dashboard fixture\n")
        self.assertTrue(preview["is_text"])

        object_preview = DashboardService(root).file_payload(
            f"project::objects/sha256/{'a' * 64}"
        )
        self.assertTrue(object_preview["is_text"])
        self.assertEqual(object_preview["media_type"], "application/json")

        with self.assertRaises(ValueError):
            DashboardService(root).file_payload("project::../outside.txt")

    def test_workspace_root_discovers_direct_projects_and_uses_default_template(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)

        def write_project(name):
            project = root / name
            (project / "composer").mkdir(parents=True)
            (project / "projects" / "survey").mkdir(parents=True)
            (project / "stage.json").write_text("{}\n", encoding="utf-8")
            workflow = {
                "schema_version": "composer-workflow-1",
                "id": f"workflow-{name}",
                "revision": 1,
                "project_id": str((project / "composer").resolve()),
                "objective": f"Objective for {name}",
                "stages": [{
                    "id": "survey", "kind": "survey",
                    "config_path": str((project / "stage.json").resolve()),
                    "project_dir": str((project / "projects" / "survey").resolve()),
                    "depends_on": [], "estimate_seconds": 1, "bindings": [],
                    "deadline_seconds": 10, "reuse_completed": False,
                    "reuse_output_path": None,
                }],
                "time_policy": {"first_result_seconds": 1, "target_seconds": 10,
                                 "hard_seconds": 30, "checkpoint_seconds": 1},
                "completion": {"required_stage_ids": ["survey"],
                                "release_requires_human": True},
            }
            (project / "workflow.json").write_text(
                json.dumps(workflow), encoding="utf-8")

        write_project("autolab")
        write_project("autolab-rerun-current")
        service = DashboardService(root)
        refs = {item["ref"] for item in service.projects()["projects"]}
        self.assertEqual(refs, {"autolab", "autolab-rerun-current"})
        self.assertNotIn("stage.json", refs)

        created = service.create_project({
            "template": ".", "slug": "fixture-run",
            "objective": "Create a project from the workspace template.",
            "hard_seconds": 3600,
        })
        self.assertEqual(created["project"], "missions/fixture-run")
        self.assertTrue((root / "missions" / "fixture-run" / "workflow.json").is_file())

    def test_project_manager_creates_isolated_validated_workflow(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        stage_config = root / "stage.json"
        stage_dir = root / "stage"
        stage_config.write_text(json.dumps({
            "limits": {"max_rounds": 2},
            "survey": {"search": {
                "max_analyzed_works": 20, "max_full_texts": 40,
                "expansion_rounds": 3, "saturation_rounds": 3,
            }},
        }) + "\n", encoding="utf-8")
        stage_dir.mkdir()
        template = {
            "schema_version": "composer-workflow-1",
            "id": "dashboard-template",
            "revision": 1,
            "project_id": str((root / "composer").resolve()),
            "objective": "A dashboard fixture template.",
            "stages": [{
                "id": "survey", "kind": "survey",
                "config_path": str(stage_config.resolve()),
                "project_dir": str(stage_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }],
            "time_policy": {"first_result_seconds": 1, "target_seconds": 10,
                             "hard_seconds": 30, "checkpoint_seconds": 1},
            "completion": {"required_stage_ids": ["survey"],
                            "release_requires_human": True},
        }
        (root / "workflow.json").write_text(
            json.dumps(template), encoding="utf-8")

        service = DashboardService(root)
        result = service.create_project({
            "template": ".",
            "slug": "fixture-run",
            "objective": "Test an isolated Composer project lifecycle.",
            "hard_seconds": 259200,
        })

        target = root / "missions" / "fixture-run"
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["project"], "missions/fixture-run")
        self.assertTrue((target / "workflow.json").is_file())
        self.assertTrue((target / "composer").is_dir())
        workflow = json.loads((target / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow["id"], "mission-fixture-run")
        self.assertEqual(workflow["objective"], "Test an isolated Composer project lifecycle.")
        self.assertEqual(Path(workflow["project_id"]).resolve(), (target / "composer").resolve())
        self.assertEqual(workflow["progression_policy"], "forward_first")
        self.assertEqual(workflow["agenda_policy"], {"mode": "adaptive"})
        self.assertEqual(workflow["retry_policy"]["max_attempts"], 2)
        self.assertEqual(workflow["continuation_policy"]["max_cycles"], 2)
        self.assertTrue(all(Path(stage["project_dir"]).is_dir() for stage in workflow["stages"]))
        self.assertTrue(all(Path(stage["config_path"]).is_file() for stage in workflow["stages"]))
        self.assertTrue(all(
            Path(stage["config_path"]).resolve().is_relative_to(target.resolve())
            for stage in workflow["stages"]
        ))
        copied_config = json.loads(Path(workflow["stages"][0]["config_path"]).read_text(encoding="utf-8"))
        self.assertEqual(copied_config["limits"]["max_rounds"], 1)
        self.assertEqual(copied_config["survey"]["search"]["max_analyzed_works"], 12)
        self.assertEqual(copied_config["survey"]["search"]["max_full_texts"], 12)
        self.assertEqual(copied_config["survey"]["search"]["expansion_rounds"], 1)
        self.assertEqual(copied_config["survey"]["search"]["saturation_rounds"], 1)
        self.assertFalse(any(path.name.startswith(".workflow-") for path in target.iterdir()))

        projects = service.projects()["projects"]
        created = next(item for item in projects if item["ref"] == "missions/fixture-run")
        self.assertEqual(created["status"], "draft")

        server = DashboardServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/actions",
            data=json.dumps({
                "action": "create_project",
                "template": ".",
                "slug": "http-run",
                "objective": "Exercise the local project action boundary.",
                "hard_seconds": 259200,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            action = json.loads(response.read())
        self.assertEqual(response.status, 201)
        self.assertEqual(action["project"], "missions/http-run")

    def test_project_manager_rehashes_relocated_capability_registry(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        capability_dir = root / "registry" / "capabilities" / "fixture_capability" / "r1"
        capability_dir.mkdir(parents=True)
        (root / "stage.json").write_text("{}\n", encoding="utf-8")
        executor = "print('executor')\n"
        validator = "print('validator')\n"
        (capability_dir / "executor.py").write_text(executor, encoding="utf-8")
        (capability_dir / "validator.py").write_text(validator, encoding="utf-8")
        candidate = {"source": str(root / "stage.json")}
        admission = {"source": str(root / "stage.json")}
        descriptor = {"source": str(root / "stage.json")}
        for name, value in (("candidate.json", candidate), ("admission.json", admission),
                            ("capability.json", descriptor)):
            (capability_dir / name).write_bytes(canonical_bytes(value))

        def digest_json(value):
            return hashlib.sha256(canonical_bytes(value)).hexdigest()

        index = {
            "schema_version": "experiment-capability-registry-1",
            "capabilities": [{
                "id": "fixture_capability", "revision": 1,
                "path": str((capability_dir / "capability.json").resolve()),
                "descriptor_sha256": digest_json(descriptor),
                "executor_sha256": hashlib.sha256(executor.encode()).hexdigest(),
                "validator_sha256": hashlib.sha256(validator.encode()).hexdigest(),
                "candidate_record_sha256": digest_json(candidate),
                "admission_sha256": digest_json(admission),
            }],
        }
        index_path = root / "registry" / "capabilities" / "index.json"
        index_path.write_bytes(canonical_bytes(index))
        (root / "workflow.json").write_text(json.dumps({
            "schema_version": "composer-workflow-1", "id": "dashboard-template", "revision": 1,
            "project_id": str((root / "composer").resolve()), "objective": "registry fixture",
            "stages": [{"id": "survey", "kind": "survey", "config_path": str((root / "stage.json").resolve()),
                        "project_dir": str((root / "projects" / "survey").resolve()), "depends_on": [],
                        "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                        "reuse_completed": False, "reuse_output_path": None}],
            "time_policy": {"first_result_seconds": 1, "target_seconds": 10,
                             "hard_seconds": 30, "checkpoint_seconds": 1},
            "completion": {"required_stage_ids": ["survey"], "release_requires_human": True},
        }), encoding="utf-8")

        service = DashboardService(root)
        service.create_project({"template": ".", "slug": "registry-run",
                                "objective": "Test registry relocation integrity.", "hard_seconds": 3600})
        target = root / "missions" / "registry-run"
        cloned_index = json.loads((target / "registry" / "capabilities" / "index.json").read_text())
        entry = cloned_index["capabilities"][0]
        revision = target / "registry" / "capabilities" / "fixture_capability" / "r1"
        self.assertEqual(entry["descriptor_sha256"], digest_json(json.loads((revision / "capability.json").read_text())))
        self.assertEqual(entry["candidate_record_sha256"], digest_json(json.loads((revision / "candidate.json").read_text())))
        self.assertEqual(entry["admission_sha256"], digest_json(json.loads((revision / "admission.json").read_text())))
        self.assertEqual(entry["executor_sha256"], hashlib.sha256((revision / "executor.py").read_bytes()).hexdigest())
        self.assertEqual(entry["validator_sha256"], hashlib.sha256((revision / "validator.py").read_bytes()).hexdigest())
        self.assertTrue(str(target) in entry["path"])


class DashboardFrontendContractTests(unittest.TestCase):
    def test_result_reader_preserves_content_and_escapes_markup(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js unavailable")
        reader = Path(__file__).parents[1] / "dashboard/static/output-view.js"
        script = """
const assert = require('node:assert/strict');
require(process.argv[1]);
const view = globalThis.SciwhaleOutputView;
const output = view.render({schema_version:'v1',question:'Does hydration matter?',
  deferred_obligations:[{requirement:'Find numeric bounds',target_stage_id:'survey'}],
  abstract:'<img src=x onerror=alert(1)>',analysis:{checks:[]}});
assert(output.includes('Does hydration matter?'));
assert(output.includes('Find numeric bounds'));
assert(output.includes('Target Stage Id'));
assert(output.includes('&lt;img'));
assert(!output.includes('<img'));
assert(output.includes('None recorded'));
assert(output.includes('<summary>Schema Version</summary>'));
assert(view.render('{"decision":"hold"}').includes('hold'));
assert(view.render('ordinary text').includes('ordinary text'));
const wrapped = view.document({input:{prompt:'Internal transport'},report:{response:{decision:'hold',deferred_obligations:[{requirement:'Find source'}]}}});
assert(wrapped.indexOf('hold') < wrapped.indexOf('Provenance and execution record'));
assert(wrapped.indexOf('Find source') < wrapped.indexOf('Provenance and execution record'));
assert(wrapped.includes('Internal transport'));
"""
        subprocess.run([node, "-e", script, str(reader)], check=True, capture_output=True, text=True)

    def test_result_reader_defaults_to_readable_view(self):
        static = Path(__file__).parents[1] / "dashboard/static"
        html = (static / "index.html").read_text()
        javascript = (static / "app.js").read_text()
        self.assertIn('id="inspector-content" hidden', html)
        self.assertIn('id="inspector-readable"', html)
        self.assertLess(html.index('/assets/output-view.js'), html.index('/assets/app.js'))
        self.assertEqual(javascript.count('$("#inspector-content").textContent ='), 1)
        self.assertIn('setInspectorView(false)', javascript)

    def test_inspector_requests_preserve_latest_selection(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js unavailable")
        script = Path(__file__).with_name("dashboard_inspector_regression.js")
        root = Path(__file__).parents[2]
        completed = subprocess.run([node, str(script), str(root)], check=True, capture_output=True, text=True)
        self.assertEqual(json.loads(completed.stdout)["cases"], 5)

    def test_sidebar_routes_show_exclusive_pages(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js unavailable")
        script = Path(__file__).with_name("dashboard_navigation_regression.js")
        root = Path(__file__).parents[2]
        completed = subprocess.run([node, str(script), str(root)], check=True, capture_output=True, text=True)
        self.assertGreaterEqual(json.loads(completed.stdout)["cases"], 11)

    def test_required_controls_exist_once(self):
        static = Path(__file__).parents[1] / "dashboard/static"
        html = (static / "index.html").read_text()
        javascript = (static / "app.js").read_text()
        ids = re.findall(r'\bid="([^\"]+)"', html)
        required = set(re.findall(r'\$\("#([\w-]+)"\)', javascript))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(required - set(ids), set())

    def test_run_poll_preserves_pending_response_and_releases_after_failure(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable")
        path = Path(__file__).parents[1] / "dashboard/static/app.js"
        script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const text = require('node:fs').readFileSync(process.argv[1], 'utf8');
const source = text.slice(text.indexOf('  let runController = null;'), text.indexOf('  async function fetchProjects()'));
const requests = [], elements = {};
const state = {view: 'project', projectRef: 'a', runRequest: 0};
const context = {state, AbortController, encodeURIComponent, renderRunControls() {},
  $(id) {return elements[id] ||= {};},
  fetch(url, options) {return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));}};
vm.runInNewContext(source + '\nthis.poll = fetchRun;', context);
const answer = (index, value) => requests[index].resolve({ok: true, async json() {return value;}});
(async () => {
  const first = context.poll();
  await context.poll();
  assert.equal(requests.length, 1);
  answer(0, {status: 'stopped'});
  await first;
  assert.equal(state.run.status, 'stopped');
  const previous = context.poll();
  state.projectRef = 'b';
  const current = context.poll();
  assert.equal(requests[1].options.signal.aborted, true);
  answer(2, {status: 'ready'});
  await current;
  answer(1, {status: 'running'});
  await previous;
  assert.equal(state.run.status, 'ready');
  const pending = context.poll();
  const forced = context.poll(true);
  assert.equal(requests[3].options.signal.aborted, true);
  requests[4].reject(new Error('offline'));
  await forced;
  assert.equal(elements['#run-status'].textContent, 'Unavailable');
  answer(3, {status: 'running'});
  await pending;
  const retried = context.poll();
  answer(5, {status: 'stopped'});
  await retried;
  assert.equal(state.run.status, 'stopped');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        subprocess.run([node, "-e", script, str(path)], check=True, capture_output=True, text=True)

    def test_stage_navigation_and_interface_language(self):
        static = Path(__file__).parents[1] / "dashboard/static"
        html = (static / "index.html").read_text()
        javascript = (static / "app.js").read_text()
        stylesheet = (static / "styles.css").read_text()
        self.assertIn('<html lang="en">', html)
        self.assertEqual(html.count('id="stage-navigation"'), 1)
        self.assertNotIn('id="stage-strip"', html)
        self.assertNotRegex(html + javascript, r'[가-힣]')
        self.assertNotRegex(stylesheet, r'\.project-nav\s*\{[^}]*display\s*:\s*none')

    def test_brand_and_execution_controls_share_aligned_geometry(self):
        static = Path(__file__).parents[1] / "dashboard/static"
        html = (static / "index.html").read_text()
        stylesheet = (static / "styles.css").read_text()
        brand = re.search(r'<div class="sidebar-context">(.*?)</div>', html).group(1)
        self.assertIn("<span>Sci-whale</span>", brand)
        self.assertNotIn("<br>", brand)
        self.assertNotIn("<small>", brand)
        for selector in (".run-control-settings select", ".run-control-buttons .button"):
            rule = re.search(re.escape(selector) + r'\s*\{([^}]+)\}', stylesheet).group(1)
            self.assertIn("height: var(--control-height)", rule)
        buttons = re.search(r'\.run-control-buttons\s*\{([^}]+)\}', stylesheet).group(1)
        self.assertIn("align-self: end", buttons)


if __name__ == "__main__":
    unittest.main()
