"""Versioned executable project-plan contracts."""

import json
import tempfile
import unittest

from scisaurus.core.errors import StateError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.plans import PLAN_SCHEMA, PlanService, task_contract_hash, validate_plan
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.control = ControlStore(self.temp.name)
        self.store = ArtifactStore(self.control)
        self.store.init_project(principal_note="plan test")
        self.service = PlanService(self.control, self.store)
        self.input = self.publish("inputs/brief", "principal")
        self.plan = {"schema_version": PLAN_SCHEMA, "id": "mission", "revision": 1,
            "objective": "Produce and independently verify an evidence-backed result.", "tasks": [
                {"id": "investigate", "kind": "retrieval", "owner": "research.worker",
                 "objective": "Capture the relevant evidence.", "depends_on": [],
                 "input_refs": [self.input], "capability_requirements": ["web-search"],
                 "output_logical_ids": ["kb/evidence/mission"], "estimate_seconds": 30},
                {"id": "compose", "kind": "production", "owner": "strategy.writer",
                 "objective": "Compose the bounded result.", "depends_on": ["investigate"],
                 "input_refs": [self.input], "capability_requirements": [],
                 "output_logical_ids": ["strategy/result/mission"], "estimate_seconds": 45}],
            "completion": {"required_task_ids": ["investigate", "compose"],
                           "required_output_logical_ids": ["kb/evidence/mission", "strategy/result/mission"],
                           "release_requires_human": True}}

    def tearDown(self):
        self.control.close()
        self.temp.cleanup()

    def publish(self, logical, author, body=None, kind="note"):
        return self.store.publish_artifact(logical_id=logical, artifact_type=kind, author=author,
            body=canonical_bytes(body or {"ok": True}), media_type="application/json")["artifact_ref"]

    def finish(self, plan_ref, task_id, logical):
        plan = json.loads(self.store.read_body(self.store.get(plan_ref)["body_hash"]))
        task = next(item for item in plan["tasks"] if item["id"] == task_id)
        physical = self.service._physical_id(plan, task)
        manager = TaskManager(self.control)
        attempt = physical + "-attempt"
        manager.start_attempt(physical, attempt, owner=task["owner"], lease_ttl_seconds=10)
        manager.finish_attempt(attempt, "succeeded", usage={})
        manager.transition(physical, "awaiting_review", task["owner"])
        output = self.store.publish_artifact(logical_id=logical, artifact_type="note", author=task["owner"],
            body=canonical_bytes({"ok": True}), media_type="application/json",
            task_id=physical, attempt_id=attempt)["artifact_ref"]
        review = self.store.publish_artifact(logical_id=f"methods/reviews/{task_id}", artifact_type="verification",
            author="methods.reviewer", body=canonical_bytes({"author": "methods.reviewer",
                "checks": [{"outcome": "passed"}]}), media_type="application/json",
            inputs=[{"ref": output, "purpose": "subject"}], task_id=physical, attempt_id=attempt)["artifact_ref"]
        return self.service.record_result(plan_ref, task_id, output_refs=[output], verification_refs=[review])

    def test_dependency_activation_and_human_release_boundary(self):
        plan_ref = self.service.publish(self.plan)["artifact_ref"]
        state = self.service.activate(plan_ref)
        by_id = {item["task_id"]: item for item in state["tasks"]}
        self.assertEqual(by_id["investigate"]["state"], "queued")
        self.assertEqual(by_id["compose"]["state"], "proposed")
        self.finish(plan_ref, "investigate", "kb/evidence/mission")
        self.assertEqual({item["task_id"]: item for item in self.service.state(plan_ref)["tasks"]}["compose"]["state"], "queued")
        self.finish(plan_ref, "compose", "strategy/result/mission")
        self.assertEqual(self.service.completion(plan_ref)["completion_state"], "needs_human")
        state = self.service.state(plan_ref)
        result_refs = {item["task_id"]: item["result_ref"] for item in state["tasks"]}
        output_refs = sorted(ref for result_ref in result_refs.values() for ref in json.loads(
            self.store.read_body(self.store.get(result_ref)["body_hash"]))["output_refs"])
        approval = self.publish("inputs/approval/mission", "principal", {
            "schema_version": "plan-release-approval-1", "plan_ref": plan_ref,
            "result_refs": result_refs, "output_refs": output_refs}, kind="decision_note")
        self.assertTrue(self.service.completion(plan_ref, approval_ref=approval)["release_authorized"])

    def test_human_approval_must_bind_exact_plan_results_and_outputs(self):
        plan_ref = self.service.publish(self.plan)["artifact_ref"]
        self.service.activate(plan_ref)
        self.finish(plan_ref, "investigate", "kb/evidence/mission")
        self.finish(plan_ref, "compose", "strategy/result/mission")
        unrelated = self.publish("inputs/approval/unrelated", "principal", {
            "schema_version": "plan-release-approval-1", "plan_ref": plan_ref,
            "result_refs": {}, "output_refs": []}, kind="decision_note")
        with self.assertRaisesRegex(ValidationError, "exact plan results"):
            self.service.completion(plan_ref, approval_ref=unrelated)

    def test_replan_reuses_unchanged_result_and_reopens_changed_descendant(self):
        first = self.service.publish(self.plan)["artifact_ref"]
        self.service.activate(first)
        first_result = self.finish(first, "investigate", "kb/evidence/mission")
        revised = json.loads(json.dumps(self.plan))
        revised["revision"] = 2
        revised["tasks"][1]["objective"] = "Compose a narrower evidence-backed result."
        second = self.service.publish(revised)["artifact_ref"]
        state = {item["task_id"]: item for item in self.service.activate(second)["tasks"]}
        self.assertEqual(state["investigate"]["result_ref"], first_result["artifact_ref"])
        self.assertEqual(state["compose"]["state"], "queued")
        self.assertNotEqual(state["compose"]["physical_task_id"],
                            self.service._physical_id(self.plan, self.plan["tasks"][1]))

    def test_changed_dependency_result_invalidates_downstream_result(self):
        first = self.service.publish(self.plan)["artifact_ref"]
        self.service.activate(first)
        self.finish(first, "investigate", "kb/evidence/mission")
        self.finish(first, "compose", "strategy/result/mission")
        revised = json.loads(json.dumps(self.plan)); revised["revision"] = 2
        revised["tasks"][0]["objective"] = "Capture corrected relevant evidence."
        second = self.service.publish(revised)["artifact_ref"]
        state = {item["task_id"]: item for item in self.service.activate(second)["tasks"]}
        self.assertIsNone(state["investigate"]["result_ref"])
        self.assertIsNone(state["compose"]["result_ref"])
        self.assertFalse(state["compose"]["ready"])

    def test_self_review_and_out_of_scope_outputs_are_rejected(self):
        ref = self.service.publish(self.plan)["artifact_ref"]
        self.service.activate(ref)
        task = self.plan["tasks"][0]; physical = self.service._physical_id(self.plan, task)
        manager = TaskManager(self.control)
        manager.start_attempt(physical, physical + "-attempt", owner=task["owner"], lease_ttl_seconds=10)
        manager.finish_attempt(physical + "-attempt", "succeeded", usage={})
        manager.transition(physical, "awaiting_review", task["owner"])
        output = self.publish("kb/wrong", task["owner"])
        review = self.publish("methods/reviews/self", task["owner"], kind="verification")
        with self.assertRaisesRegex(ValidationError, "exactly match"):
            self.service.record_result(ref, "investigate", output_refs=[output], verification_refs=[review])
        correct = self.store.publish_artifact(logical_id="kb/evidence/mission", artifact_type="note",
            author=task["owner"], body=canonical_bytes({"ok": True}), media_type="application/json",
            task_id=physical, attempt_id=physical + "-attempt")["artifact_ref"]
        review = self.store.publish_artifact(logical_id="methods/reviews/self-bound", artifact_type="verification",
            author=task["owner"], body=canonical_bytes({"author": task["owner"],
                "checks": [{"outcome": "passed"}]}), media_type="application/json",
            inputs=[{"ref": correct, "purpose": "subject"}], task_id=physical,
            attempt_id=physical + "-attempt")["artifact_ref"]
        with self.assertRaisesRegex(ValidationError, "independent"):
            self.service.record_result(ref, "investigate", output_refs=[correct], verification_refs=[review])

    def test_cycle_unknown_dependency_and_bad_completion_fail(self):
        for mutation in ("cycle", "unknown", "output"):
            value = json.loads(json.dumps(self.plan))
            if mutation == "cycle": value["tasks"][0]["depends_on"] = ["compose"]
            if mutation == "unknown": value["tasks"][0]["depends_on"] = ["missing"]
            if mutation == "output": value["completion"]["required_output_logical_ids"] = ["strategy/not-owned"]
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                validate_plan(value)


if __name__ == "__main__":
    unittest.main()
