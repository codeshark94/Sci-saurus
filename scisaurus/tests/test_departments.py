import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.events import ControlStore
from scisaurus.core.messages import MessageBus
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager
from scisaurus.core.errors import QuotaExceededError, ValidationError
from scisaurus.runtime.departments import (
    LEGACY_SCHEMA_VERSION, DepartmentRuntime, default_organization, stage_role,
    validate_charter, validate_organization,
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
        manifest = json.loads(self.store.read_body(
            self.store.head("command/organization")["body_hash"]))
        self.assertGreater(len(manifest["agents"]), 30)
        self.assertEqual(manifest["schema_version"], "project-organization-2")
        self.assertTrue(any(item["id"] == "research.source-acquirer" for item in manifest["agents"]))
        self.assertEqual(manifest["active_assignments"], [])
        self.assertEqual(
            [item["stage_kind"] for item in manifest["stage_routes"]],
            ["topic_discovery", "survey", "experiment", "interpretation", "argument", "paper"],
        )
        self.assertEqual(manifest["command_agents"]["arbiter"]["agent"], "arbiter")

    def test_charter_cannot_assign_the_same_agent_to_chief_and_adversary(self):
        charter = default_organization()["departments"][0]
        with self.assertRaisesRegex(ValidationError, "distinct"):
            validate_charter({**charter, "adversary": charter["chief"]})

    def test_snapshot_exposes_concrete_agents_and_functional_stage_routes(self):
        snapshot = self.runtime.snapshot()
        self.assertGreater(len(snapshot["agents"]), 30)
        self.assertEqual(snapshot["role_pool"], snapshot["agents"])
        experiment = next(item for item in snapshot["stage_routes"]
                          if item["stage_kind"] == "experiment")
        self.assertEqual(experiment["role"], "methods.validation")
        self.assertEqual(experiment["owner_address"], {"dept": "methods", "agent": "chief"})
        self.assertTrue(any(item["appointment"] == "adversary"
                            and item["department"] == "methods"
                            for item in snapshot["agents"]))

    def test_v1_organization_is_migrated_and_preserves_named_appointments(self):
        legacy = default_organization()
        legacy["schema_version"] = LEGACY_SCHEMA_VERSION
        for charter in legacy["departments"]:
            charter.pop("agent_roles", None)
        legacy["departments"][0]["chief"] = "research-lead"
        legacy["departments"][0]["adversary"] = "research-red-team"
        migrated = validate_organization(legacy)
        self.assertEqual(migrated["schema_version"], "project-organization-2")
        research = next(item for item in migrated["departments"] if item["id"] == "research")
        self.assertEqual(research["chief"], "research-lead")
        self.assertEqual(research["adversary"], "research-red-team")
        self.assertTrue(any(item["id"] == "source-acquirer" for item in research["agent_roles"]))

    def test_specialist_owner_is_recorded_and_functional_owner_remains_chief(self):
        with self.assertRaisesRegex(ValidationError, "does not admit topic_refinement"):
            self.runtime.propose({
                "schema_version": "department-work-order-1", "id": "wrong-specialist-scope",
                "kind": "topic_refinement", "owner": "research.source-acquirer",
                "objective": "Refine the question.", "why": "The selected question is broad.",
                "success_condition": "A falsifiable question is recorded.",
                "evidence_needed": "A cited gap and boundary condition.",
            })
        specialist = self.runtime.propose({
            "schema_version": "department-work-order-1", "id": "acquire-more",
            "kind": "full_text_retrieval", "owner": "research.source-acquirer",
            "objective": "Acquire the decisive source.", "why": "The survey has metadata only.",
            "success_condition": "A point-in-time full-text capture is inspectable.",
            "evidence_needed": "Source identity, transport, and capture digest.",
        })
        self.assertEqual(specialist["assigned_role"], "research.source-acquirer")
        self.assertEqual(specialist["assigned_agent"], "source-acquirer")
        task = self.tasks.get(specialist["task_id"])
        self.assertEqual(task["payload"]["assignment_kind"], "specialist")
        chief = self.runtime.propose({
            "schema_version": "department-work-order-1", "id": "refine-topic",
            "kind": "topic_refinement", "owner": "research",
            "objective": "Refine the question.", "why": "The gap is broad.",
            "success_condition": "A falsifiable question is recorded.",
            "evidence_needed": "A cited gap and boundary condition.",
        })
        self.assertEqual(chief["assigned_role"], "research.chief")

    def test_stage_dispatch_isolated_pool_and_adversarial_artifact(self):
        plan = self.runtime.begin_stage(
            "survey", "survey", attempt_number=2, input_ref={"ref": "artifact:input", "secret": "not copied"},
            deadline_seconds=30, active_role_ids=["search-strategist", "source-acquirer"],
        )
        self.assertEqual(plan["active_agents"], ["research.search-strategist", "research.source-acquirer"])
        self.assertEqual(plan["verifier_agent"], "research.adversarial-reviewer")
        self.assertEqual(len(plan["assignments"]), 3)
        self.assertTrue(all(item["assigned_role"] != plan["verifier_agent"] for item in plan["assignments"][:2]))
        self.assertEqual(plan["assignments"][0]["input_ref"], {"ref": "artifact:input"})
        result = self.runtime.finish_stage(
            "survey", "survey", attempt_number=2, outcome="completed",
            output_ref="/tmp/survey.json", usage={"model_calls": 2},
        )
        self.assertEqual(result["verifier_outcome"], "accepted")
        self.assertNotEqual(result["chief_agent"], result["verifier_agent"])
        verdict_logical = result["verifier_artifact_ref"].split("@", 1)[0].removeprefix("artifact:")
        verdict = json.loads(self.store.read_body(
            self.store.head(verdict_logical)["body_hash"]))
        self.assertTrue(verdict["independence_check"])
        self.assertEqual(verdict["verifier_agent"], "research.adversarial-reviewer")
        self.assertEqual(self.runtime.snapshot()["active_assignments"], [])

    def test_stage_failure_preserves_known_specialist_outcomes(self):
        self.runtime.begin_stage(
            "survey", "survey", attempt_number=3,
            deadline_seconds=30, active_role_ids=["search-strategist", "source-acquirer"],
        )
        result = self.runtime.finish_stage(
            "survey", "survey", attempt_number=3, outcome="blocked",
            output_ref=None, error=ValidationError("chief stage output was blocked"),
            specialist_results={
                "search-strategist": {"status": "succeeded", "usage": {}},
                "source-acquirer": {"status": "result_unknown", "usage": {}},
            },
        )
        outcomes = {item["role_id"]: item["outcome"] for item in result["assignments"]}
        self.assertEqual(outcomes["search-strategist"], "succeeded")
        self.assertEqual(outcomes["source-acquirer"], "result_unknown")

    def test_stage_failure_does_not_mark_undispatched_specialists_failed(self):
        plan = self.runtime.begin_stage(
            "topic", "topic_discovery", attempt_number=4,
            deadline_seconds=30, active_role_ids=["frontier-scout", "search-strategist"],
        )
        result = self.runtime.finish_stage(
            "topic", "topic_discovery", attempt_number=4, outcome="blocked",
            output_ref=None, error=ValidationError("topic package contract failed"),
            failure_scope="stage",
        )
        outcomes = {item["role_id"]: item["outcome"] for item in result["assignments"]}
        self.assertEqual(outcomes, {
            "frontier-scout": "not_evaluated",
            "search-strategist": "not_evaluated",
        })
        self.assertEqual(result["verifier_outcome"], "not_evaluated")
        self.assertTrue(all(
            self.tasks.get(item["task_id"])["state"] == "blocked"
            for item in result["assignments"]
        ))
        attempt_states = {
            row["state"] for row in self.control._conn.execute(
                "SELECT state FROM attempts WHERE task_id IN (?, ?)",
                (plan["task_ids"][0], plan["task_ids"][1]),
            ).fetchall()
        }
        self.assertEqual(attempt_states, {"cancelled"})

    def test_stage_pool_quota_and_deadline_are_enforced(self):
        route = self.runtime.stage_route("survey")
        route["max_active_agents"] = 1
        with patch.object(self.runtime, "stage_route", return_value=route):
            with self.assertRaises(QuotaExceededError):
                self.runtime.begin_stage(
                    "survey", "survey", deadline_seconds=20,
                    active_role_ids=["search-strategist", "academic-scout"],
                )
        with self.assertRaises(ValidationError):
            self.runtime.begin_stage(
                "survey", "survey", deadline_seconds=20,
                active_role_ids=["search-strategist"],
                quotas={"search-strategist": {"max_calls": 0, "max_input_tokens": 1,
                                               "max_output_tokens": 1, "max_seconds": 1}},
            )

    def test_interrupted_specialist_attempt_is_reconciled_as_unknown(self):
        plan = self.runtime.begin_stage(
            "experiment", "experiment", deadline_seconds=20,
            active_role_ids=["methodologist"],
        )
        reconciled = self.runtime.reconcile_interrupted_assignments()
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["outcome"], "result_unknown")
        assignment = self.runtime.snapshot()["agent_activity"][0]
        self.assertEqual(assignment["attempt_state"], "result_unknown")
        self.assertEqual(assignment["task_state"], "blocked")
        self.assertEqual(self.runtime.snapshot()["active_assignments"], [])

    def test_invalid_quota_is_rejected_before_any_role_is_admitted(self):
        with self.assertRaises(ValidationError):
            self.runtime.begin_stage(
                "survey", "survey", deadline_seconds=20,
                active_role_ids=["search-strategist", "source-acquirer"],
                quotas={
                    "research.search-strategist": {
                        "max_calls": 1, "max_input_tokens": 100,
                        "max_output_tokens": 100, "max_seconds": 1,
                    },
                    "source-acquirer": {
                        "max_calls": 0, "max_input_tokens": 100,
                        "max_output_tokens": 100, "max_seconds": 1,
                    },
                },
            )
        self.assertEqual(self.runtime.snapshot()["agent_activity"], [])

    def test_stage_role_is_shared_with_composer_functional_mapping(self):
        self.assertEqual(stage_role("topic_discovery"), "research.intelligence")
        self.assertEqual(stage_role("paper"), "editorial.composer")

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
