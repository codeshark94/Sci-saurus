import json
import tempfile
import unittest
from pathlib import Path

from scisaurus.core.events import ControlStore
from scisaurus.core.messages import MessageBus
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager
from scisaurus.core.errors import ValidationError
from scisaurus.runtime.departments import (
    DepartmentRuntime, default_organization, validate_organization,
)


class DepartmentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.control = ControlStore(self.root)
        self.store = ArtifactStore(self.control)
        self.store.init_project(principal_note="department-test")
        self.messages = MessageBus(self.control)
        self.tasks = TaskManager(self.control)
        self.runtime = DepartmentRuntime(
            self.control, self.store, self.messages, self.tasks,
            project_id=str(self.root),
        )

    def tearDown(self):
        self.control.close()
        self.tmp.cleanup()

    def test_default_charters_are_valid_and_durable(self):
        organization = validate_organization(default_organization())
        self.assertTrue(organization["allow_dynamic_proposals"])
        self.assertEqual(set(self.runtime.charters), {"research", "methods", "strategy", "editorial", "operations"})
        self.assertTrue(self.store.head("command/organization"))
        self.assertTrue(self.store.head("command/departments/research/charter"))

    def test_proposal_creates_scoped_work_order_and_backlog(self):
        result = self.runtime.propose({
            "schema_version": "department-work-order-1", "id": "search-more",
            "kind": "literature_expansion", "owner": "research.intelligence",
            "objective": "Find decisive full text.", "why": "The current source set is incomplete.",
            "success_condition": "The gap assessment has enough verified full text.",
            "evidence_needed": "Exact source spans and identity records.",
        }, source_stage_id="survey")
        self.assertEqual(result["department"], "research")
        self.assertEqual(result["task_state"], "queued")
        snapshot = self.runtime.snapshot()
        self.assertEqual(snapshot["backlog_counts"]["research"]["running"], 0)
        self.assertEqual(snapshot["backlog_counts"]["research"]["queued"], 1)
        self.assertEqual(len(snapshot["open_work_orders"]), 1)

    def test_changed_objective_supersedes_the_prior_work_order_generation(self):
        base = {
            "schema_version": "department-work-order-1", "id": "search-more",
            "kind": "literature_expansion", "owner": "research.intelligence",
            "objective": "Find decisive full text.", "why": "The current source set is incomplete.",
            "success_condition": "The gap assessment has enough verified full text.",
            "evidence_needed": "Exact source spans and identity records.",
        }
        first = self.runtime.activate_work_orders([base])[0]
        self.tasks.start_attempt(
            first["task_id"], "active-generation-attempt", owner="chief",
            lease_ttl_seconds=60,
        )
        changed = {**base, "objective": "Find decisive full text and contradictory work."}
        second = self.runtime.propose(changed)
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(self.tasks.get(first["task_id"])["state"], "stale")
        self.assertEqual(self.tasks.get_attempt("active-generation-attempt")["state"], "result_unknown")
        self.assertEqual(self.tasks.get(second["task_id"])["state"], "queued")
        snapshot = self.runtime.snapshot()
        self.assertEqual(len(snapshot["open_work_orders"]), 1)
        self.assertEqual(snapshot["open_work_orders"][0]["work_order_ref"], second["work_order_ref"])
        logical = "command/departments/research/work-orders/search-more"
        versions = self.store.versions(logical)
        stale_manifest = self.store.get(f"artifact:{logical}@{versions[-2]}")
        stale_body = json.loads(self.store.read_body(stale_manifest["body_hash"]))
        self.assertEqual(stale_body["state"], "stale")

    def test_same_work_order_replay_ignores_provenance_and_preserves_artifact_state(self):
        request = {
            "schema_version": "department-work-order-1", "id": "stable-order",
            "kind": "additional_experiment", "owner": "methods.validation",
            "objective": "Run the discriminating control.", "why": "The result is ambiguous.",
            "success_condition": "The control separates the explanations.",
            "evidence_needed": "Validated raw output.",
        }
        first = self.runtime.propose(request, source_stage_id="survey")
        second = self.runtime.propose(request, source_stage_id="workflow")
        self.assertEqual(first["work_order_ref"], second["work_order_ref"])
        active = self.runtime.activate_work_orders([request])[0]
        running_ref = active["work_order_ref"]
        self.assertEqual(self.runtime.snapshot()["open_work_orders"][0]["work_order_ref"], running_ref)
        running_body = json.loads(self.store.read_body(self.store.head(
            "command/departments/methods/work-orders/stable-order")["body_hash"]))
        self.assertEqual(running_body["state"], "running")
        resolved = self.runtime.resolve_work_orders(
            [request], stage_kind="experiment", outcome="completed")
        self.assertEqual(resolved[0]["state"], "completed")
        completed_head = self.store.head("command/departments/methods/work-orders/stable-order")
        completed_body = json.loads(self.store.read_body(completed_head["body_hash"]))
        self.assertEqual(completed_body["state"], "completed")
        replay = self.runtime.propose(request, source_stage_id="paper")
        self.assertEqual(replay["work_order_ref"], completed_head["artifact_ref"])
        self.assertNotEqual(running_ref, completed_head["artifact_ref"])

    def test_changed_generation_fences_a_paused_prior_task(self):
        request = {
            "schema_version": "department-work-order-1", "id": "paused-order",
            "kind": "recovery", "owner": "operations.coordinator",
            "objective": "Repair the execution environment.", "why": "The provider stopped responding.",
            "success_condition": "A representative probe succeeds.",
            "evidence_needed": "The probe output and environment record.",
        }
        first = self.runtime.propose(request)
        self.tasks.transition(first["task_id"], "running", "command.composer")
        self.tasks.transition(first["task_id"], "paused", "command.composer")
        self.runtime.propose({**request, "objective": "Repair the execution environment and rerun the probe."})
        self.assertEqual(self.tasks.get(first["task_id"])["state"], "stale")

    def test_inbox_routes_typed_requests_and_is_idempotent(self):
        note = self.store.publish_artifact(
            logical_id="command/test-note", artifact_type="decision_note", author="command.composer",
            body=b"{}", media_type="application/json",
        )
        feedback = {
            "message_id": "feedback-1", "stage_id": "experiment", "event_id": "event-1",
            "action": "request_research_expansion", "to": {"dept": "research", "agent": "chief"},
            "research_expansion_requests": [{
                "id": "search-more", "kind": "literature_expansion", "owner": "research.intelligence",
                "objective": "Find decisive full text.", "why": "The current source set is incomplete.",
                "success_condition": "The gap assessment has enough verified full text.",
                "evidence_needed": "Exact source spans and identity records.",
            }],
        }
        first = self.runtime.receive_message("feedback-1", feedback, note["artifact_ref"])
        second = self.runtime.receive_message("feedback-1", feedback, note["artifact_ref"])
        self.assertEqual(first["inbox_ref"], second["inbox_ref"])
        self.assertEqual(first["proposals"][0]["task_id"], second["proposals"][0]["task_id"])
        self.assertEqual(self.runtime.snapshot()["backlog_counts"]["research"]["queued"], 1)

    def test_continuation_moves_work_order_through_running_to_completed(self):
        request = {
            "id": "run-control", "kind": "additional_experiment", "owner": "methods.validation",
            "objective": "Run a discriminating control.", "why": "The result is ambiguous.",
            "success_condition": "The control separates the explanations.",
            "evidence_needed": "Validated raw output.",
        }
        active = self.runtime.activate_work_orders([request])
        self.assertEqual(active[0]["task_state"], "running")
        resolved = self.runtime.resolve_work_orders(
            [request], stage_kind="experiment", outcome="completed")
        self.assertEqual(resolved[0]["state"], "completed")
        self.assertEqual(self.runtime.snapshot()["backlog_counts"]["methods"]["completed"], 1)

    def test_malformed_request_is_rejected_as_durable_workflow_state(self):
        note = self.store.publish_artifact(
            logical_id="command/malformed-note", artifact_type="decision_note", author="command.composer",
            body=b"{}", media_type="application/json",
        )
        result = self.runtime.receive_message(
            "feedback-malformed",
            {"to": {"dept": "research", "agent": "chief"},
             "research_requests": [{"id": "bad/request", "kind": "literature_expansion"}]},
            note["artifact_ref"],
        )
        self.assertEqual(len(result["rejected"]), 1)
        self.assertTrue(result["rejected"][0]["rejection_ref"])

    def test_unknown_target_department_is_durably_rejected(self):
        note = self.store.publish_artifact(
            logical_id="command/unrouted-note", artifact_type="decision_note", author="command.composer",
            body=b"{}", media_type="application/json",
        )
        result = self.runtime.receive_message(
            "feedback-unrouted",
            {"to": {"dept": "unlisted", "agent": "chief"}, "research_requests": []},
            note["artifact_ref"],
        )
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["rejected"][0]["request_id"], "unrouted-target")
        self.assertTrue(self.store.head("command/departments/unlisted/rejections/unrouted-target"))

    def test_non_list_request_container_is_not_silently_dropped(self):
        note = self.store.publish_artifact(
            logical_id="command/container-note", artifact_type="decision_note", author="command.composer",
            body=b"{}", media_type="application/json",
        )
        result = self.runtime.receive_message(
            "feedback-container",
            {"to": {"dept": "research", "agent": "chief"},
             "research_expansion_requests": {"id": "lost"}},
            note["artifact_ref"],
        )
        self.assertEqual(result["proposals"], [])
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("must be a list", result["rejected"][0]["reason"])

    def test_unknown_owner_is_rejected_without_a_backlog_entry(self):
        with self.assertRaises(ValidationError):
            self.runtime.propose({
                "schema_version": "department-work-order-1", "id": "bad-owner",
                "kind": "recovery", "owner": "unknown.department", "objective": "Recover.",
                "why": "A failure occurred.", "success_condition": "The task completes.",
                "evidence_needed": "A recorded verification.",
            })
        self.assertEqual(self.runtime.snapshot()["open_work_orders"], [])


if __name__ == "__main__":
    unittest.main()
