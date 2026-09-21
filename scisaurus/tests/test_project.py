"""Project acceptance invariants with explicitly simulated local workers."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.changes import ChangeService
from scisaurus.core.documents import Documents
from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.project import ProjectRunner
from scisaurus.runtime.project_config import validate_project_config
from scisaurus.tests.test_runner import config as paragraph_config


def project_config(mode="pass", *, rounds=1):
    value = paragraph_config(mode)
    for key in ("paragraph", "preserved_neighbor", "required_literals"):
        del value[key]
    value["objective"] = "Preserve the separate values and accurately qualify the evidence horizon."
    value["supplied_context"] = "The first group has value 10 and the second group has value 20. Only immediate outcomes are known."
    value["limits"].update(concurrent_calls=3, max_rounds=rounds, checkpoint_seconds=0.05)
    value["claims"] = [{"id": "first-result", "statement": "The first group has value 10."},
                       {"id": "second-result", "statement": "The second group has value 20."}]
    value["operations"] = {"environment_files": []}
    value["document"] = {"title": "Parallel evidence qualification", "sections": [
        {"id": "results", "title": "Results", "paragraphs": [
            {"id": "left", "text": "The first group has value 10 and proves lasting improvement.",
             "editable": True, "objective": "Preserve 10 and qualify the first group's evidence horizon.",
             "required_literals": ["10"], "claim_ids": ["first-result"]}]},
        {"id": "discussion", "title": "Discussion", "paragraphs": [
            {"id": "right", "text": "The second group has value 20 and proves lasting improvement.",
             "editable": True, "objective": "Preserve 20 and qualify the second group's evidence horizon.",
             "required_literals": ["20"], "claim_ids": ["second-result"]},
            {"id": "locked", "text": "Only immediate outcomes were measured in both groups.",
             "editable": False, "objective": None, "required_literals": [], "claim_ids": []}]}]}
    if mode == "document-unit":
        value["document"]["sections"][0]["paragraphs"][0]["id"] = "document"
    return value


def project_worker(kind, params, channel):
    assignment = json.loads(params["prompt"])
    mode = params["client"]["model"]
    task_id = channel.path.parent.name
    (channel.path.parent / "started.json").write_text(json.dumps({"time": time.monotonic()}))
    if assignment["assignment"].startswith("Identify"):
        targets = [{"unit_id": unit["id"], "change_focus": "Preserve the result and qualify its time horizon."}
                   for unit in assignment["editable_units"]]
        if mode == "duplicate-target":
            targets.append(dict(targets[0]))
        elif mode in {"immutable-target", "foreign-target"}:
            targets[0]["unit_id"] = "locked" if mode == "immutable-target" else "foreign"
        output = {"allegation": "Both units overstate the duration of measured outcomes.",
                  "material_impact": "Readers could infer unmeasured durable improvement.",
                  "resolution_condition": "Keep each result and state that long-term outcomes remain unknown.",
                  "rationale": "Only immediate measurements are supplied.", "unit_focus": targets}
    elif assignment["assignment"].startswith("Revise"):
        time.sleep(30 if mode == "cancel" else 0.3)
        unit_id = assignment["unit_id"]
        if mode == "writer-failure" and unit_id == "left":
            channel.put({"ok": False, "error": "Explicitly simulated left producer failure", "outcome_known": True})
            return
        value = assignment["required_literals"][0]
        text = f"The {'first' if value == '10' else 'second'} group has value {value}; long-term outcomes remain unknown."
        if mode == "cross-contradiction" and unit_id == "right":
            text = "The second group has value 20; long-term outcomes were measured in both groups."
        if mode == "targeted" and task_id == "write-1-left":
            text = "The first group has value 10; outcomes require interpretation."
        output = {"unit_id": unit_id, "baseline_unit_ref": assignment["baseline_unit_ref"], "text": text, "support": []}
        if unit_id == "left":
            if mode == "foreign-proposal":
                output["unit_id"] = "right"
            elif mode == "stale-baseline":
                output["baseline_unit_ref"] = assignment["baseline_unit_ref"].replace("/left@", "/right@")
            elif mode == "other-unit-value":
                output["text"] = text.replace("10", "20")
    elif assignment["assignment"].startswith("Decide"):
        output = {"decision": "revise", "rationale": "Only the failed paragraph needs a clearer evidence horizon.",
                  "targets": [{"unit_id": unit_id, "change_focus": "State explicitly that long-term outcomes are unknown."}
                              for unit_id in assignment["failed_units"]]}
    else:
        if mode == "cancel-review" and assignment["scope"] != "whole composed document":
            time.sleep(30)
        elif mode in {"cancel-final", "stale-claim", "stale-source"} and assignment["scope"] == "whole composed document":
            time.sleep(0.3)
        checks = [{"check_id": check["check_id"], "kind": check["kind"], "outcome": "passed",
                   "method": "Compare the assigned exact candidate with the governing unit contracts and source captures.",
                   "result": "The assigned requirement is retained in the exact candidate."}
                  for check in assignment["required_checks"]]
        checks.append({"check_id": "evidence-horizon", "kind": "resolution", "outcome": "passed",
                       "method": "Compare the stated horizon with supplied measured outcomes.",
                       "result": "No unmeasured long-term result is inferred."})
        if mode == "spaced-check":
            checks.append({"check_id": "context and evidence alignment", "kind": "regression", "outcome": "passed",
                           "method": "Compare all qualifications with the supplied facts.",
                           "result": "The exact candidate retains every supplied qualification."})
        if mode == "targeted" and task_id == "verify-1-left":
            checks[-1].update(outcome="failed", result="The first paragraph still does not state that long-term outcomes are unknown.")
        if mode == "cross-contradiction" and assignment["scope"] == "whole composed document":
            next(check for check in checks if check["check_id"] == "document-consistency").update(
                outcome="failed", result="The second paragraph contradicts the first paragraph and immutable evidence status.")
        if mode == "missing-unit-check" and assignment["scope"] == "whole composed document":
            checks = [check for check in checks if check["check_id"] != "unit-right"]
        defect = mode.removesuffix("-integrated")
        defect_scope = (assignment["scope"] == "whole composed document" if mode.endswith("-integrated")
                        else assignment.get("unit_id") == "left")
        if defect_scope:
            if defect == "missing-objective-resolution":
                checks = [check for check in checks if check["check_id"] != "objective-resolution"]
            elif defect == "wrong-objective-kind":
                next(check for check in checks if check["check_id"] == "objective-resolution")["kind"] = "regression"
            elif defect == "missing-all-resolution":
                checks = [check for check in checks if check["kind"] != "resolution"]
        output = {"checks": checks, "regressions": [], "uncertainties": [], "observations": [],
                  "rationale": "The explicit simulated review compared the exact composed candidate with the assigned contract."}
    (channel.path.parent / "finished.json").write_text(json.dumps({"time": time.monotonic()}))
    channel.put({"ok": True, "result": {"text": json.dumps(output), "model": "explicit-simulation",
                 "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 5},
                 "elapsed_seconds": 0.01, "finish_reason": "stop"}})


class SimulatedOperationsProjectRunner(ProjectRunner):
    """Replace capability adapters with labeled artifacts for model-flow tests."""
    def _setup_operations(self):
        self._publish("command/operations/test-fixture", "note", {"simulation": True,
                      "purpose": "Local deterministic project-flow verification; no external retrieval performed."},
                      "operations.fixture")

    def _retrieve(self, role, label):
        text = "Explicitly simulated source: only immediate outcomes were measured."
        body = {"simulation": True, "source_url": "https://example.com/simulated-source", "text": text,
                "capture_sha256": sha256_hex(text.encode()), "metadata": {"representation": "extracted_text"}}
        record = self._publish(f"kb/captures/{label}-simulated", "source_capture", body, role)
        captures = [{"ref": record["artifact_ref"], "url": body["source_url"], "text": text,
                     "capture_sha256": body["capture_sha256"], "representation": "extracted_text"}]
        self.sources.extend(captures)
        return captures


class TestProjectRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-project-test-")
        self.root = Path(self.temp.name)
        self.path = self.root / "project"

    def tearDown(self):
        self.temp.cleanup()

    def run_mode(self, mode="pass", *, rounds=1, on_progress=None):
        self.path = self.root / mode
        updates = []
        def progress(value):
            updates.append(value)
            if on_progress:
                on_progress(value)
        with patch("scisaurus.runtime.project._invoke_worker", project_worker):
            runner = SimulatedOperationsProjectRunner(self.path, project_config(mode, rounds=rounds), on_progress=progress)
            result = runner.run()
        self.assertTrue(result["event_chain"][0])
        return result, updates

    def open_run(self):
        control = ControlStore(self.path)
        self.addCleanup(control.close)
        store = ArtifactStore(control)
        return control, store, Documents(control, store)

    @staticmethod
    def body(store, ref):
        return json.loads(store.read_body(store.get(ref)["body_hash"]))

    def assignment(self, store, task_id):
        return json.loads(self.body(store, store.head(f"command/contexts/{task_id}")["artifact_ref"])["prompt"])

    def test_parallel_unit_writes_receive_exact_contracts_and_preserve_nested_structure(self):
        result, updates = self.run_mode()
        self.assertEqual(result["status"], "accepted", result["error"])
        control, store, documents = self.open_run()
        intervals = []
        for unit_id, literal, claim in (("left", "10", "first-result"), ("right", "20", "second-result")):
            task_id = f"write-1-{unit_id}"
            assignment = self.assignment(store, task_id)
            self.assertEqual(assignment["unit_id"], unit_id)
            self.assertEqual(assignment["required_literals"], [literal])
            self.assertEqual([item["id"] for item in assignment["claims"]], [claim])
            self.assertEqual(assignment["baseline_unit_ref"], f"artifact:strategy/units/{unit_id}@1")
            intervals.append([json.loads((self.path / "runs" / task_id / filename).read_text())["time"]
                              for filename in ("started.json", "finished.json")])
        self.assertGreater(min(end for _, end in intervals) - max(start for start, _ in intervals), 0.15)
        self.assertTrue(any(len(update["active_tasks"]) == 2 for update in updates if update["phase"] == "executing"))
        tree = documents.get_tree(result["incumbent_ref"])
        baseline = documents.get_tree(result["baseline_ref"])
        self.assertEqual([node["ref"] for node in tree["units"]], [node["ref"] for node in baseline["units"]])
        self.assertEqual(tree["units"][1]["children"][1], baseline["units"][1]["children"][1])
        self.assertEqual(tree["assembly_dependencies"], baseline["assembly_dependencies"])
        for index, unit_id in enumerate(("left", "right")):
            content = documents.read_unit(tree["units"][index]["children"][0]["ref"])
            prior = documents.read_unit(baseline["units"][index]["children"][0]["ref"])
            self.assertEqual(content["claim_refs"], prior["claim_refs"])
        self.assertEqual(store.versions("strategy/units/locked"), [1])
        self.assertEqual(result["usage"]["reserved"], {})
        self.assertIn("Only immediate outcomes were measured in both groups.", (self.path / "output/manuscript.md").read_text())

    def test_all_scopes_review_exact_composed_candidate_before_single_atomic_adoption(self):
        result, updates = self.run_mode()
        self.assertEqual(result["status"], "accepted", result["error"])
        control, store, _ = self.open_run()
        candidate = result["candidates"][0]
        tasks = ["verify-1-left", "verify-1-right", "integrated-review-1"]
        for task_id in tasks:
            assignment = self.assignment(store, task_id)
            check_kinds = {check["check_id"]: check["kind"] for check in assignment["required_checks"]}
            self.assertEqual(check_kinds["objective-resolution"], "resolution")
            self.assertEqual(check_kinds["source-support"], "regression")
            self.assertEqual(check_kinds["reader-facing"], "regression")
            self.assertEqual(assignment["candidate_ref"], candidate["candidate_ref"])
            self.assertEqual(assignment["baseline_ref"], result["baseline_ref"])
            units = [unit for group in assignment["candidate_document"]["groups"] for unit in group["units"]]
            self.assertEqual([unit["unit_id"] for unit in units], ["left", "right", "locked"])
            for unit in units[:2]:
                self.assertEqual(unit["text"], result["proposals"][unit["unit_id"]]["text"])
        integrated = self.assignment(store, "integrated-review-1")
        self.assertTrue({"unit-left", "unit-right", "document-consistency"}.issubset(
            {check["check_id"] for check in integrated["required_checks"]}))
        criteria = self.body(store, store.head("command/criteria/project")["artifact_ref"])
        criteria_kinds = {check["check_id"]: check["kind"] for check in criteria["required_checks"]}
        self.assertEqual(criteria_kinds["objective-resolution"], "resolution")
        events = list(control.replay())
        adopted = [i for i, event in enumerate(events) if event["event_type"] == "artifact.accepted"]
        self.assertEqual(len(adopted), 2)
        verified = next(i for i, event in enumerate(events) if event["event_type"] == "verification.completed")
        self.assertLess(verified, adopted[-1])
        for task_id in tasks:
            completed = next(i for i, event in enumerate(events) if event["event_type"] == "attempt.finished"
                             and event["payload"]["task_id"] == task_id)
            self.assertLess(completed, verified)
        self.assertTrue(all(update["incumbent_ref"] == result["baseline_ref"] for update in updates
                            if update["phase"] == "candidate_staged"))

    def test_one_failed_writer_retains_successful_sibling_without_partial_adoption(self):
        result, _ = self.run_mode("writer-failure")
        self.assertEqual(result["status"], "unresolved", result["error"])
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertEqual(set(result["proposals"]), {"right"})
        self.assertEqual(result["candidates"], [])
        control, store, _ = self.open_run()
        self.assertEqual(store.get(result["proposals"]["right"]["proposal_ref"])["artifact_type"], "draft")
        self.assertEqual(control._conn.execute("SELECT COUNT(*) FROM tasks WHERE kind='verification'").fetchone()[0], 0)
        self.assertEqual(result["usage"]["reserved"], {})

    def test_targeted_round_reuses_successful_proposal_and_reverifies_every_scope(self):
        result, _ = self.run_mode("targeted", rounds=2)
        self.assertEqual(result["status"], "accepted", result["error"])
        self.assertEqual([item["requested_units"] for item in result["rounds"]], [["left", "right"], ["left"]])
        first, second = result["candidates"]
        self.assertEqual(first["proposal_refs"]["right"], second["proposal_refs"]["right"])
        self.assertNotEqual(first["proposal_refs"]["left"], second["proposal_refs"]["left"])
        self.assertFalse(first["adopted"])
        self.assertTrue(second["adopted"])
        control, store, _ = self.open_run()
        self.assertIsNone(control._conn.execute("SELECT 1 FROM tasks WHERE task_id='write-2-right'").fetchone())
        for number, candidate in ((1, first), (2, second)):
            for task_id in (f"verify-{number}-left", f"verify-{number}-right", f"integrated-review-{number}"):
                assignment = self.assignment(store, task_id)
                self.assertEqual(assignment["candidate_ref"], candidate["candidate_ref"])
        self.assertEqual(control._conn.execute("SELECT state FROM tasks WHERE task_id='write-1-left'").fetchone()[0], "stale")
        self.assertEqual(control._conn.execute("SELECT state FROM tasks WHERE task_id='write-1-right'").fetchone()[0], "completed")

    def test_cross_unit_contradiction_blocks_individually_passing_proposals(self):
        result, _ = self.run_mode("cross-contradiction")
        self.assertEqual(result["status"], "unresolved", result["error"])
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertFalse(result["candidates"][0]["resolved"])
        self.assertTrue(result["rounds"][0]["global_failed"])
        self.assertEqual(result["rounds"][0]["failed_units"], [])

    def test_missing_integrated_unit_check_prevents_adoption(self):
        result, _ = self.run_mode("missing-unit-check")
        self.assertEqual(result["status"], "unresolved", result["error"])
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertFalse(result["candidates"][0]["resolved"])
        self.assertTrue(any("unit-right" in reason for reason in result["candidates"][0]["uncertainties"]))

    def assert_review_registry_rejection(self, mode, expected_error):
        result, _ = self.run_mode(mode)
        self.assertEqual(result["status"], "unresolved", result["error"])
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertFalse(result["candidates"][0]["resolved"])
        self.assertFalse(result["candidates"][0]["adopted"])
        self.assertTrue(any(expected_error in reason for reason in result["candidates"][0]["uncertainties"]))
        self.assertEqual(result["usage"]["reserved"], {})
        _, store, _ = self.open_run()
        task_id = "integrated-review-1" if mode.endswith("-integrated") else "verify-1-left"
        execution = self.body(store, store.head(f"command/executions/{task_id}")["artifact_ref"])
        return json.loads(execution["text"])["checks"]

    def test_an_arbitrary_resolution_check_cannot_replace_the_mandatory_objective_check(self):
        for suffix in ("", "-integrated"):
            with self.subTest(scope=suffix or "local"):
                checks = self.assert_review_registry_rejection("missing-objective-resolution" + suffix,
                                                               "objective-resolution")
                self.assertTrue(any(check["kind"] == "resolution" for check in checks))
                self.assertNotIn("objective-resolution", {check["check_id"] for check in checks})

    def test_mandatory_objective_check_requires_its_declared_resolution_kind(self):
        for suffix in ("", "-integrated"):
            with self.subTest(scope=suffix or "local"):
                checks = self.assert_review_registry_rejection("wrong-objective-kind" + suffix,
                                                               "objective-resolution")
                self.assertEqual(next(check for check in checks if check["check_id"] == "objective-resolution")["kind"],
                                 "regression")
                self.assertTrue(any(check["kind"] == "resolution" for check in checks))

    def test_regression_only_review_cannot_establish_objective_resolution(self):
        for suffix in ("", "-integrated"):
            with self.subTest(scope=suffix or "local"):
                checks = self.assert_review_registry_rejection("missing-all-resolution" + suffix,
                                                               "both resolution and regression")
                self.assertTrue(checks)
                self.assertTrue(all(check["kind"] == "regression" for check in checks))

    def test_unit_named_document_has_distinct_local_and_integrated_review_evidence(self):
        result, _ = self.run_mode("document-unit")
        self.assertEqual(result["status"], "accepted", result["error"])
        control, store, _ = self.open_run()
        candidate = result["candidates"][0]
        verification = self.body(store, candidate["verification_ref"])
        ids = [check["check_id"] for check in verification["checks"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("unit/document:source-support", ids)
        self.assertIn("document:source-support", ids)
        self.assertEqual(self.assignment(store, "verify-1-document")["unit_id"], "document")

    def test_valid_extra_check_id_with_spaces_is_recorded_without_changing_meaning(self):
        result, _ = self.run_mode("spaced-check")
        self.assertEqual(result["status"], "accepted", result["error"])
        _, store, _ = self.open_run()
        verification = self.body(store, result["candidates"][0]["verification_ref"])
        ids = {check["check_id"] for check in verification["checks"]}
        self.assertIn("unit/left:context and evidence alignment", ids)
        self.assertIn("document:context and evidence alignment", ids)

    def test_supervisor_cannot_assign_duplicate_immutable_or_foreign_units(self):
        for mode in ("duplicate-target", "immutable-target", "foreign-target"):
            with self.subTest(mode=mode):
                result, _ = self.run_mode(mode)
                self.assertEqual(result["status"], "blocked")
                self.assertIn("supervised unit target", result["error"])
                self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
                self.assertEqual(result["proposals"], {})

    def test_returned_unit_baseline_and_values_are_bound_to_the_assignment(self):
        for mode in ("foreign-proposal", "stale-baseline", "other-unit-value"):
            with self.subTest(mode=mode):
                result, _ = self.run_mode(mode)
                self.assertEqual(result["status"], "unresolved", result["error"])
                self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
                self.assertEqual(set(result["proposals"]), {"right"})
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["worker_errors"][0]["unit_id"], "left")

    def test_cancellation_retains_unknown_cost_and_dispatches_no_later_stage(self):
        def cancel(update):
            paths = [self.root / "cancel" / "runs" / f"write-1-{key}" / "started.json" for key in ("left", "right")]
            if update["phase"] == "executing" and all(path.exists() for path in paths):
                raise KeyboardInterrupt("explicit project cancellation simulation")
        result, _ = self.run_mode("cancel", rounds=2, on_progress=cancel)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["failure"], {"kind": "process_interrupted"})
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertEqual(result["usage"]["reserved"], {"concurrent_calls": 2})
        control, _, _ = self.open_run()
        attempts = {row[0]: row[1] for row in control._conn.execute("SELECT task_id,state FROM attempts")}
        self.assertEqual(set(attempts), {"supervise-project", "write-1-left", "write-1-right"})
        self.assertEqual(attempts["write-1-left"], "result_unknown")
        self.assertEqual(attempts["write-1-right"], "result_unknown")
        self.assertEqual(result["candidates"], [])

    def test_cancelling_unit_reviews_does_not_dispatch_the_integrated_review(self):
        def cancel(update):
            paths = [self.root / "cancel-review" / "runs" / f"verify-1-{key}" / "started.json" for key in ("left", "right")]
            if update["phase"] == "executing" and all(path.exists() for path in paths):
                raise KeyboardInterrupt("explicit review cancellation simulation")
        result, _ = self.run_mode("cancel-review", rounds=2, on_progress=cancel)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["failure"], {"kind": "process_interrupted"})
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertEqual(result["usage"]["reserved"], {"concurrent_calls": 2})
        control, _, _ = self.open_run()
        attempts = {row[0]: row[1] for row in control._conn.execute("SELECT task_id,state FROM attempts")}
        self.assertNotIn("integrated-review-1", attempts)
        self.assertFalse(any(task.startswith("reassess") for task in attempts))
        self.assertEqual(attempts["verify-1-left"], "result_unknown")
        self.assertEqual(attempts["verify-1-right"], "result_unknown")

    def test_completed_final_review_does_not_override_cancellation(self):
        cancelled = False
        def cancel_after_result(update):
            nonlocal cancelled
            directory = self.root / "cancel-final" / "runs" / "integrated-review-1"
            if (not cancelled and update["phase"] == "executing" and (directory / "started.json").exists()):
                deadline = time.monotonic() + 2
                while not (directory / "result.json").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((directory / "result.json").exists())
                cancelled = True
                raise KeyboardInterrupt("explicit cancellation after final result publication")
        result, _ = self.run_mode("cancel-final", on_progress=cancel_after_result)
        self.assertTrue(cancelled)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["failure"], {"kind": "process_interrupted"})
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertFalse(result["candidates"][0]["adopted"])
        self.assertEqual(result["usage"]["reserved"], {})
        control, store, _ = self.open_run()
        self.assertEqual(control._conn.execute("SELECT state FROM attempts WHERE task_id='integrated-review-1'").fetchone()[0], "succeeded")
        self.assertIsNotNone(store.head("command/executions/integrated-review-1"))

    def test_claim_or_source_changed_during_review_requires_fresh_authority(self):
        for mode, logical in (("stale-claim", "strategy/claims/first-result"),
                              ("stale-source", "kb/captures/producer-simulated")):
            with self.subTest(mode=mode):
                changed = False
                def replace_governing_artifact(update):
                    nonlocal changed
                    directory = self.root / mode / "runs" / "integrated-review-1"
                    if changed or update["phase"] != "executing" or not (directory / "started.json").exists():
                        return
                    control = ControlStore(self.root / mode)
                    try:
                        store = ArtifactStore(control)
                        prior = store.head(logical)
                        body = self.body(store, prior["artifact_ref"])
                        if mode == "stale-claim":
                            body["statement"] += " This claim now requires additional qualification."
                        else:
                            body["text"] += " The captured source representation has changed."
                            body["capture_sha256"] = sha256_hex(body["text"].encode())
                        store.publish_artifact(logical_id=logical, artifact_type=prior["artifact_type"], author="principal",
                                               body=canonical_bytes(body), media_type="application/json")
                    finally:
                        control.close()
                    changed = True
                result, _ = self.run_mode(mode, on_progress=replace_governing_artifact)
                self.assertTrue(changed)
                self.assertEqual(result["status"], "blocked")
                self.assertIn("changed", result["error"])
                self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
                self.assertFalse(result["candidates"][0]["adopted"])
                self.assertEqual(result["usage"]["reserved"], {})

    def test_claim_head_changed_after_preflight_is_rejected_inside_atomic_adoption(self):
        original = ChangeService.accept_changeset
        old_ref = None
        def advance_claim_before_acceptance(service, *args, **kwargs):
            nonlocal old_ref
            logical = "strategy/claims/first-result"
            prior = service.store.head(logical)
            old_ref = prior["artifact_ref"]
            self.assertIn(old_ref, kwargs.get("expected_head_refs", []))
            body = self.body(service.store, old_ref)
            body["statement"] += " Additional evidence now governs this claim."
            service.store.publish_artifact(logical_id=logical, artifact_type="claim", author="principal",
                                          body=canonical_bytes(body), media_type="application/json")
            return original(service, *args, **kwargs)
        with patch.object(ChangeService, "accept_changeset", new=advance_claim_before_acceptance):
            result, _ = self.run_mode()
        self.assertIsNotNone(old_ref)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertFalse(result["candidates"][0]["adopted"])
        control, store, _ = self.open_run()
        self.assertNotEqual(store.head("strategy/claims/first-result")["artifact_ref"], old_ref)
        self.assertFalse(any(event["event_type"] == "changeset.integrated" for event in control.replay()))
        self.assertEqual(result["usage"]["reserved"], {})

    def test_mechanical_review_rejects_changed_claims_immutable_units_and_structure(self):
        for mutation in ("claim", "immutable", "structure", "assembly"):
            with self.subTest(mutation=mutation):
                runner = SimulatedOperationsProjectRunner(self.root / mutation, project_config())
                self.addCleanup(runner.control.close)
                runner._initialize()
                tree = deepcopy(runner.documents.get_tree(runner.baseline["artifact_ref"]))
                for section in tree["units"]:
                    for node in section["children"]:
                        unit_id = node["ref"].split("/units/")[1].split("@")[0]
                        if unit_id not in runner.editable:
                            continue
                        prior = runner.documents.read_unit(node["ref"])
                        text = prior["text"].replace("proves lasting improvement", "leaves long-term outcomes unknown")
                        claim_refs = prior["claim_refs"] if mutation != "claim" or unit_id != "left" else []
                        changed = runner.documents.publish_unit(logical_id=runner.unit_records[unit_id]["artifact_id"],
                            kind="paragraph", text=text, author="strategy.simulated-integrator", purpose=prior["purpose"],
                            claim_refs=claim_refs, citations=prior["citations"])
                        node["ref"] = changed["artifact_ref"]
                        runner.proposals[unit_id] = {"text": text}
                if mutation == "immutable":
                    changed = runner.documents.publish_unit(logical_id="strategy/units/locked", kind="paragraph",
                        text="Long-term outcomes were also measured.", author="strategy.simulated-integrator")
                    tree["units"][1]["children"][1]["ref"] = changed["artifact_ref"]
                elif mutation == "structure":
                    tree["units"].reverse()
                elif mutation == "assembly":
                    tree["assembly_dependencies"] = []
                candidate = runner.documents.publish_manifest(document_id="strategy/documents/manuscript", tree=tree,
                                                              author="strategy.simulated-integrator")
                checks = runner._preservation(candidate["artifact_ref"])
                self.assertTrue(any(check["outcome"] == "failed" for check in checks), checks)
                expected = "preservation-left" if mutation == "claim" else "mechanical-preservation"
                self.assertEqual(next(check for check in checks if check["check_id"] == expected)["outcome"], "failed")


class TestProjectConfig(unittest.TestCase):
    def test_duplicate_units_and_foreign_claims_are_rejected(self):
        value = project_config()
        duplicate = deepcopy(value)
        duplicate["document"]["sections"][1]["paragraphs"][0]["id"] = "left"
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            validate_project_config(duplicate)
        foreign = deepcopy(value)
        foreign["document"]["sections"][0]["paragraphs"][0]["claim_ids"] = ["foreign-claim"]
        with self.assertRaisesRegex(ValidationError, "foreign"):
            validate_project_config(foreign)

    def test_values_from_another_unit_cannot_satisfy_the_local_contract(self):
        value = project_config()
        value["document"]["sections"][0]["paragraphs"][0]["required_literals"] = ["20"]
        with self.assertRaisesRegex(ValidationError, "their own paragraph"):
            validate_project_config(value)

    def test_immutable_units_cannot_receive_revision_objectives(self):
        value = project_config()
        value["document"]["sections"][1]["paragraphs"][1]["objective"] = "Rewrite this protected context."
        with self.assertRaisesRegex(ValidationError, "immutable"):
            validate_project_config(value)


if __name__ == "__main__":
    unittest.main()
