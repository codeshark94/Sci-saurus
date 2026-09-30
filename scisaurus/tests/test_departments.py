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
    LEGACY_SCHEMA_VERSION, PREVIOUS_SCHEMA_VERSION, PRIOR_SCHEMA_VERSION,
    V2_SCHEMA_VERSION, V5_SCHEMA_VERSION,
    ROLE_MAX_CALLS, ROLE_OUTPUT_TOKENS_PER_CALL, ROLE_OUTPUT_TOKEN_ALLOWANCE,
    DepartmentRuntime, default_organization,
    stage_role, validate_charter, validate_organization,
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
        self.assertEqual(manifest["schema_version"], "project-organization-6")
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
        self.assertEqual(migrated["schema_version"], "project-organization-6")
        research = next(item for item in migrated["departments"] if item["id"] == "research")
        self.assertEqual(research["chief"], "research-lead")
        self.assertEqual(research["adversary"], "research-red-team")
        self.assertTrue(any(item["id"] == "source-acquirer" for item in research["agent_roles"]))

    def test_v2_organization_migrates_default_output_budgets_and_preserves_custom_ones(self):
        legacy = default_organization()
        legacy["schema_version"] = V2_SCHEMA_VERSION
        old_default = {
            "max_calls": 2, "max_input_tokens": 245760,
            "max_output_tokens": 4000, "max_seconds": 900,
        }
        for charter in legacy["departments"]:
            for role in charter["agent_roles"]:
                role["quota"] = dict(old_default)
        research = next(item for item in legacy["departments"] if item["id"] == "research")
        custom = next(item for item in research["agent_roles"] if item["id"] == "cataloger")
        custom_quota = {
            "max_calls": 1, "max_input_tokens": 32000,
            "max_output_tokens": 1800, "max_seconds": 120,
        }
        custom["quota"] = dict(custom_quota)

        migrated = validate_organization(legacy)

        self.assertEqual(migrated["schema_version"], "project-organization-6")
        migrated_research = next(
            item for item in migrated["departments"] if item["id"] == "research")
        migrated_cataloger = next(
            item for item in migrated_research["agent_roles"] if item["id"] == "cataloger")
        migrated_scout = next(
            item for item in migrated_research["agent_roles"] if item["id"] == "search-strategist")
        self.assertEqual(migrated_cataloger["quota"], {
            **custom_quota, "max_output_tokens": 1800,
            "max_output_tokens_per_call": 1800,
        })
        self.assertEqual(migrated_scout["quota"]["max_calls"], ROLE_MAX_CALLS)
        self.assertEqual(
            migrated_scout["quota"]["max_output_tokens"], ROLE_OUTPUT_TOKEN_ALLOWANCE)
        self.assertEqual(migrated_scout["quota"]["max_output_tokens_per_call"],
                         ROLE_OUTPUT_TOKENS_PER_CALL)

    def test_v3_organization_splits_cumulative_and_per_request_output_limits(self):
        legacy = default_organization()
        legacy["schema_version"] = PREVIOUS_SCHEMA_VERSION
        old_quota = {
            "max_calls": 3, "max_input_tokens": 245760,
            "max_output_tokens": 24576, "max_seconds": 900,
        }
        for charter in legacy["departments"]:
            for role in charter["agent_roles"]:
                role["quota"] = dict(old_quota)

        migrated = validate_organization(legacy)
        self.assertEqual(migrated["schema_version"], "project-organization-6")
        role = next(item for item in migrated["departments"]
                    if item["id"] == "methods")["agent_roles"][0]
        self.assertEqual(role["quota"]["max_output_tokens"], 24576)
        self.assertEqual(role["quota"]["max_output_tokens_per_call"], 8192)

    def test_v4_default_review_quota_migrates_for_length_continuation(self):
        legacy = default_organization()
        legacy["schema_version"] = PRIOR_SCHEMA_VERSION
        old_default = {
            "max_calls": 2, "max_input_tokens": 245760,
            "max_output_tokens": 16384, "max_output_tokens_per_call": 8192,
            "max_seconds": 900,
        }
        for charter in legacy["departments"]:
            for role in charter["agent_roles"]:
                role["quota"] = dict(old_default)
        research = next(item for item in legacy["departments"] if item["id"] == "research")
        custom = next(item for item in research["agent_roles"] if item["id"] == "cataloger")
        custom["quota"] = {
            "max_calls": 1, "max_input_tokens": 32000,
            "max_output_tokens": 1800, "max_output_tokens_per_call": 1800,
            "max_seconds": 120,
        }

        migrated = validate_organization(legacy)

        self.assertEqual(migrated["schema_version"], "project-organization-6")
        methods = next(item for item in migrated["departments"] if item["id"] == "methods")
        reviewer = next(item for item in methods["agent_roles"]
                        if item["id"] == "analysis-reviewer")
        self.assertEqual(reviewer["quota"]["max_calls"], 4)
        self.assertEqual(reviewer["quota"]["max_output_tokens"], 24576)
        migrated_cataloger = next(item for item in migrated["departments"]
                                  if item["id"] == "research")["agent_roles"]
        migrated_cataloger = next(item for item in migrated_cataloger
                                  if item["id"] == "cataloger")
        self.assertEqual(migrated_cataloger["quota"], {
            "max_calls": 1, "max_input_tokens": 32000,
            "max_output_tokens": 1800, "max_output_tokens_per_call": 1800,
            "max_seconds": 120,
        })

    def test_v5_default_review_quota_adds_completion_call_without_expanding_token_budget(self):
        legacy = default_organization()
        legacy["schema_version"] = V5_SCHEMA_VERSION
        old_default = {
            "max_calls": 3, "max_input_tokens": 245760,
            "max_output_tokens": 24576, "max_output_tokens_per_call": 8192,
            "max_seconds": 900,
        }
        for charter in legacy["departments"]:
            for role in charter["agent_roles"]:
                role["quota"] = dict(old_default)
        research = next(item for item in legacy["departments"] if item["id"] == "research")
        custom = next(item for item in research["agent_roles"] if item["id"] == "cataloger")
        custom["quota"] = {**old_default, "max_calls": 2, "max_output_tokens": 9000}

        migrated = validate_organization(legacy)

        self.assertEqual(migrated["schema_version"], "project-organization-6")
        methods = next(item for item in migrated["departments"] if item["id"] == "methods")
        reviewer = next(item for item in methods["agent_roles"]
                        if item["id"] == "analysis-reviewer")
        self.assertEqual(reviewer["quota"], {
            "max_calls": 4, "max_input_tokens": 245760,
            "max_output_tokens": 24576, "max_output_tokens_per_call": 8192,
            "max_seconds": 900,
        })
        migrated_cataloger = next(item for item in research["agent_roles"]
                                  if item["id"] == "cataloger")
        self.assertEqual(migrated_cataloger["quota"], custom["quota"])

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
            deadline_seconds=30, active_role_ids=["search-strategist", "technical-ecosystem-scout"],
        )
        self.assertEqual(plan["active_agents"], ["research.search-strategist", "research.technical-ecosystem-scout"])
        self.assertEqual(plan["verifier_agent"], "research.adversarial-reviewer")
        self.assertEqual(len(plan["assignments"]), 3)
        self.assertTrue(all(item["assigned_role"] != plan["verifier_agent"] for item in plan["assignments"][:2]))
        self.assertEqual(plan["assignments"][0]["input_ref"], {"ref": "artifact:input"})
        for assignment in plan["assignments"]:
            self.assertEqual(assignment["quota"]["max_calls"], ROLE_MAX_CALLS)
            self.assertEqual(
                assignment["quota"]["max_output_tokens"], ROLE_OUTPUT_TOKEN_ALLOWANCE)
            self.assertEqual(assignment["quota"]["max_output_tokens_per_call"],
                             ROLE_OUTPUT_TOKENS_PER_CALL)
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

    def test_unbound_service_role_is_rejected_before_stage_admission(self):
        route = self.runtime.stage_route("survey")
        service_role = next(
            role for role in self.runtime.charters["research"]["agent_roles"]
            if role["id"] == "source-acquirer")
        route["required_role_ids"].append(service_role["id"])
        route["required_roles"].append(service_role)
        route["required_agents"].append("research.source-acquirer")

        with patch.object(self.runtime, "stage_route", return_value=route):
            with self.assertRaisesRegex(ValidationError, "without a registered executor"):
                self.runtime.begin_stage(
                    "survey-service-preflight", "survey", attempt_number=1,
                    deadline_seconds=30, active_role_ids=["source-acquirer"],
                )

        self.assertEqual(self.runtime.snapshot()["active_assignments"], [])

    def test_stage_can_dispatch_only_its_internal_runner_and_verifier(self):
        plan = self.runtime.begin_stage(
            "survey-gap-only", "survey", attempt_number=1,
            deadline_seconds=30, active_role_ids=[],
        )
        self.assertEqual(plan["active_agents"], [])
        self.assertEqual(len(plan["assignments"]), 1)
        self.assertEqual(plan["assignments"][0]["assignment_phase"], "verifier")
        self.assertEqual(plan["verifier_agent"], "research.adversarial-reviewer")

    def test_stage_failure_preserves_known_specialist_outcomes(self):
        self.runtime.begin_stage(
            "survey", "survey", attempt_number=3,
            deadline_seconds=30, active_role_ids=["search-strategist", "technical-ecosystem-scout"],
        )
        result = self.runtime.finish_stage(
            "survey", "survey", attempt_number=3, outcome="blocked",
            output_ref=None, error=ValidationError("chief stage output was blocked"),
            specialist_results={
                "search-strategist": {"status": "succeeded", "usage": {}},
                "technical-ecosystem-scout": {"status": "result_unknown", "usage": {}},
            },
        )
        outcomes = {item["role_id"]: item["outcome"] for item in result["assignments"]}
        self.assertEqual(outcomes["search-strategist"], "succeeded")
        self.assertEqual(outcomes["technical-ecosystem-scout"], "result_unknown")

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
                                               "max_output_tokens": 1,
                                               "max_output_tokens_per_call": 1,
                                               "max_seconds": 1}},
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

    def test_interrupted_panel_reuses_durable_specialist_and_starts_verifier_once(self):
        stage_id = "experiment-repair-panel-resume"
        plan = self.runtime.begin_stage(
            stage_id, "experiment", attempt_number=1, deadline_seconds=30,
            active_role_ids=["methodologist"],
        )
        specialist = next(item for item in plan["assignments"]
                          if item["assignment_phase"] == "specialist")
        verifier = next(item for item in plan["assignments"]
                        if item["assignment_phase"] == "verifier")
        response = {"decision": "repair", "summary": "bounded repair", "findings": []}
        report = {"status": "succeeded", "response": response,
                  "usage": {"model_calls": 1, "input_tokens": 3, "output_tokens": 2}}
        self.tasks.finish_attempt(
            specialist["attempt_id"], "succeeded", usage=report["usage"])
        execution = self.runtime._publish_idempotent(
            f"{specialist['assignment_logical_id']}/execution", "report",
            {
                "schema_version": "specialist-execution-1",
                **{key: specialist[key] for key in (
                    "stage_id", "stage_kind", "attempt_number", "assignment_id",
                    "task_id", "role_id", "assigned_role")},
                "report": report,
            }, author=specialist["assigned_role"],
        )

        self.runtime.reconcile_interrupted_assignments()
        self.assertEqual(self.tasks.get(specialist["task_id"])["state"], "blocked")
        self.assertEqual(self.tasks.get(verifier["task_id"])["state"], "blocked")

        resumed = self.runtime.begin_stage(
            stage_id, "experiment", attempt_number=1, deadline_seconds=30,
            active_role_ids=["methodologist"],
        )
        specialist = next(item for item in resumed["assignments"]
                          if item["assignment_phase"] == "specialist")
        verifier = next(item for item in resumed["assignments"]
                        if item["assignment_phase"] == "verifier")
        self.assertEqual(specialist["task_state"], "running")
        self.assertEqual(specialist["attempt_state"], "succeeded")
        self.assertEqual(self.tasks.get_attempt(specialist["attempt_id"])["state"], "succeeded")

        started = self.runtime.start_verifier_attempt(verifier)
        self.assertEqual(started["state"], "started")
        result = self.runtime.finish_stage(
            stage_id, "experiment", attempt_number=1, outcome="completed",
            output_ref="artifact:test-result",
            specialist_results={"methodologist": {
                "status": "succeeded", "provider_call_reused": True,
                "artifact_ref": execution["artifact_ref"], "usage": {},
            }},
            verifier_result={"status": "succeeded", "response": {
                "decision": "accept", "rationale": "The repair plan is bounded.",
                "critical_findings": [],
            }, "usage": {"model_calls": 1}},
        )
        self.assertEqual(result["verifier_outcome"], "accepted")
        specialist_attempts = self.control._conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (specialist["task_id"],)
        ).fetchone()[0]
        verifier_attempts = self.control._conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (verifier["task_id"],)
        ).fetchone()[0]
        self.assertEqual(specialist_attempts, 1)
        self.assertEqual(verifier_attempts, 1)
        self.assertEqual(self.tasks.get(verifier["task_id"])["state"], "completed")

    def test_untracked_verifier_dispatch_is_unknown_and_retry_gets_a_new_assignment(self):
        stage_id = "experiment-repair-panel-legacy-dispatch"
        first = self.runtime.begin_stage(
            stage_id, "experiment", attempt_number=1, deadline_seconds=30,
            active_role_ids=[],
        )
        verifier = first["assignments"][0]
        with self.assertRaisesRegex(ValidationError, "does not match"):
            self.runtime.reconcile_dispatched_verifier_unknown(
                stage_id, "experiment", 1,
                dispatch_event={
                    "event": "dispatched", "stage_id": stage_id,
                    "task_id": "another-verifier-task", "role_id": verifier["role_id"],
                },
            )
        unknown = self.runtime.reconcile_dispatched_verifier_unknown(
            stage_id, "experiment", 1,
            dispatch_event={
                "event": "dispatched", "stage_id": stage_id,
                "task_id": verifier["task_id"], "role_id": verifier["role_id"],
            },
        )
        self.assertEqual(unknown["state"], "result_unknown")
        self.assertEqual(self.tasks.get_attempt(verifier["attempt_id"])["state"], "result_unknown")
        self.assertEqual(self.tasks.get(verifier["task_id"])["state"], "blocked")

        retry = self.runtime.begin_stage(
            stage_id, "experiment", attempt_number=2, deadline_seconds=30,
            active_role_ids=[],
        )
        retry_verifier = retry["assignments"][0]
        self.assertNotEqual(retry_verifier["task_id"], verifier["task_id"])
        self.assertEqual(self.tasks.get(verifier["task_id"])["state"], "blocked")
        self.assertEqual(self.runtime.start_verifier_attempt(retry_verifier)["state"], "started")

    def test_durable_verifier_result_is_re_admitted_without_a_second_call(self):
        stage_id = "experiment-repair-panel-verifier-recovery"
        plan = self.runtime.begin_stage(
            stage_id, "experiment", attempt_number=1, deadline_seconds=30,
            active_role_ids=[],
        )
        verifier = plan["assignments"][0]
        response = {
            "decision": "accept", "rationale": "The evidence-bound repair is sound.",
            "critical_findings": [],
        }
        report = {"status": "succeeded", "response": response,
                  "usage": {"model_calls": 1, "input_tokens": 5, "output_tokens": 4}}
        input_digest = "a" * 64
        self.runtime.start_verifier_attempt(verifier)
        execution = self.runtime._publish_idempotent(
            f"{verifier['assignment_logical_id']}/execution", "report",
            {
                "schema_version": "specialist-verifier-execution-1",
                **{key: verifier[key] for key in (
                    "stage_id", "stage_kind", "attempt_number", "assignment_id",
                    "task_id", "role_id", "assigned_role")},
                "input_digest": input_digest,
                "report": report,
            }, author=verifier["assigned_role"],
        )
        self.tasks.finish_attempt(
            verifier["attempt_id"], "succeeded", usage=report["usage"])
        self.runtime.reconcile_interrupted_assignments()
        self.assertEqual(self.tasks.get(verifier["task_id"])["state"], "blocked")
        retained = self.runtime.find_durable_verifier_report(
            stage_id, 1, input_digest=input_digest)
        self.assertEqual(retained["artifact_ref"], execution["artifact_ref"])
        self.assertEqual(retained["report"], report)
        self.assertIsNone(self.runtime.find_durable_verifier_report(
            stage_id, 1, input_digest="b" * 64))

        resumed = self.runtime.begin_stage(
            stage_id, "experiment", attempt_number=1, deadline_seconds=30,
            active_role_ids=[],
        )
        verifier = resumed["assignments"][0]
        self.assertEqual(verifier["task_state"], "running")
        self.assertEqual(verifier["attempt_state"], "succeeded")
        self.assertEqual(self.tasks.get_attempt(verifier["attempt_id"])["state"], "succeeded")

    def test_interrupted_specialist_with_durable_report_is_recovered_as_succeeded(self):
        plan = self.runtime.begin_stage(
            "survey", "survey", attempt_number=313, deadline_seconds=20,
            active_role_ids=["search-strategist"],
        )
        assignment = next(
            row for row in plan["assignments"]
            if row["assignment_phase"] == "specialist")
        report = {
            "status": "succeeded",
            "execution_mode": "model",
            "assigned_role": assignment["assigned_role"],
            "role_id": assignment["role_id"],
            "usage": {"model_calls": 1, "input_tokens": 1808, "output_tokens": 405},
            "response": {
                "decision": "repair", "summary": "Evidence inputs are missing.",
                "findings": ["The stated analytic expression is absent."],
                "evidence_gaps": [], "requested_actions": [],
            },
        }
        self.store.publish_artifact(
            logical_id=f"{assignment['assignment_logical_id']}/execution",
            artifact_type="report", author=assignment["assigned_role"],
            body=json.dumps({
                "schema_version": "specialist-execution-1",
                "project_id": str(self.root),
                "stage_id": "survey", "stage_kind": "survey",
                "attempt_number": 313,
                "assigned_role": assignment["assigned_role"],
                "role_id": assignment["role_id"],
                "assignment_id": assignment["assignment_id"],
                "task_id": assignment["task_id"],
                "input_ref": assignment["input_ref"],
                "input_digest": "packet-digest",
                "report": report,
            }, sort_keys=True).encode(),
            media_type="application/json",
        )
        interrupted_attempt = self.tasks.get_attempt(assignment["attempt_id"])
        self.assertIsNotNone(
            self.runtime._durable_execution_report({
                **interrupted_attempt["payload"], "task_id": assignment["task_id"],
            }))

        reconciled = self.runtime.reconcile_interrupted_assignments()

        self.assertEqual(reconciled[0]["outcome"], "succeeded")
        self.assertEqual(reconciled[0]["accounting"], "durable_execution_result_recovered")
        attempt = self.tasks.get_attempt(assignment["attempt_id"])
        self.assertEqual(attempt["state"], "succeeded")
        self.assertEqual(attempt["usage"]["actual"]["model_calls"], 1)
        self.assertEqual(self.tasks.get(assignment["task_id"])["state"], "blocked")
        verifier = next(
            item for item in plan["assignments"]
            if item["assignment_phase"] == "verifier")
        self.assertEqual(self.tasks.get(verifier["task_id"])["state"], "blocked")
        self.assertEqual(self.runtime.snapshot()["active_assignments"], [])

    def test_settled_attempt_does_not_leave_interrupted_assignment_running(self):
        plan = self.runtime.begin_stage(
            "experiment", "experiment", attempt_number=315, deadline_seconds=20,
            active_role_ids=["methodologist"],
        )
        assignment = next(
            row for row in plan["assignments"]
            if row["assignment_phase"] == "specialist")
        self.tasks.finish_attempt(
            assignment["attempt_id"], "succeeded", usage={"model_calls": 1})

        reconciled = self.runtime.reconcile_interrupted_assignments()

        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["outcome"], "succeeded")
        self.assertEqual(reconciled[0]["task_state"], "blocked")
        self.assertEqual(self.tasks.get(assignment["task_id"])["state"], "blocked")
        self.assertEqual(
            self.tasks.get_attempt(assignment["attempt_id"])["state"], "succeeded")
        self.assertEqual(self.runtime.snapshot()["active_assignments"], [])
        verifier = next(
            row for row in plan["assignments"]
            if row["assignment_phase"] == "verifier")
        self.assertEqual(self.tasks.get(verifier["task_id"])["state"], "blocked")

    def test_interrupted_assignment_rejects_mismatched_durable_report_identity(self):
        plan = self.runtime.begin_stage(
            "survey", "survey", attempt_number=314, deadline_seconds=20,
            active_role_ids=["search-strategist"],
        )
        assignment = next(
            row for row in plan["assignments"]
            if row["assignment_phase"] == "specialist")
        self.store.publish_artifact(
            logical_id=f"{assignment['assignment_logical_id']}/execution",
            artifact_type="report", author=assignment["assigned_role"],
            body=json.dumps({
                "schema_version": "specialist-execution-1",
                "project_id": str(self.root),
                "stage_id": "survey", "stage_kind": "survey",
                "attempt_number": 314,
                "assigned_role": assignment["assigned_role"],
                "role_id": assignment["role_id"],
                "assignment_id": "wrong-assignment",
                "task_id": assignment["task_id"],
                "report": {"status": "succeeded", "usage": {"model_calls": 1}},
            }, sort_keys=True).encode(),
            media_type="application/json",
        )

        reconciled = self.runtime.reconcile_interrupted_assignments()

        self.assertEqual(reconciled[0]["outcome"], "result_unknown")
        self.assertEqual(self.tasks.get_attempt(assignment["attempt_id"])["state"], "result_unknown")

    def test_invalid_quota_is_rejected_before_any_role_is_admitted(self):
        with self.assertRaises(ValidationError):
            self.runtime.begin_stage(
                "survey", "survey", deadline_seconds=20,
                active_role_ids=["search-strategist", "technical-ecosystem-scout"],
                quotas={
                    "research.search-strategist": {
                        "max_calls": 1, "max_input_tokens": 100,
                        "max_output_tokens": 100,
                        "max_output_tokens_per_call": 100, "max_seconds": 1,
                    },
                    "technical-ecosystem-scout": {
                        "max_calls": 0, "max_input_tokens": 100,
                        "max_output_tokens": 100,
                        "max_output_tokens_per_call": 100, "max_seconds": 1,
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

    def test_scoped_repair_metadata_is_persisted_without_expanding_public_schema(self):
        request = {
            "id": "argument-repair",
            "kind": "interpretation_expansion",
            "owner": "strategy.interpretation",
            "objective": "Repair the reviewed claim-evidence graph.",
            "why": "The argument adjudicator found a scoped scientific debt.",
            "success_condition": "The revised argument passes independent adjudication.",
            "evidence_needed": "Fresh evidence links and the prior argument.",
            "target_stage_id": "argument",
            "target_stage_kind": "argument",
            "repair_priority": "immediate",
        }
        active = self.runtime.activate_work_orders([request])[0]
        body = json.loads(self.store.read_body(self.store.head(
            "command/departments/strategy/work-orders/argument-repair")["body_hash"]))
        self.assertEqual(body["target_stage_id"], "argument")
        self.assertEqual(body["target_stage_kind"], "argument")
        self.assertEqual(body["repair_priority"], "immediate")
        self.assertEqual(self.tasks.get(active["task_id"])["state"], "running")

    def test_retire_superseded_work_orders_removes_old_generation_from_live_backlog(self):
        first = self.runtime.activate_work_orders([{
            "id": "old-order", "kind": "literature_expansion", "owner": "research.intelligence",
            "objective": "Search the first frontier.", "why": "The first frontier was inconclusive.",
            "success_condition": "A bounded source set is verified.",
            "evidence_needed": "Source identities and exact evidence spans.",
        }])[0]
        second = self.runtime.activate_work_orders([{
            "id": "current-order", "kind": "additional_experiment", "owner": "methods.validation",
            "objective": "Run the current discriminating control.", "why": "The current result is ambiguous.",
            "success_condition": "The control separates the explanations.",
            "evidence_needed": "Versioned raw output and an independent recalculation.",
        }])[0]

        retired = self.runtime.retire_superseded_work_orders(
            {"current-order"}, reason="new continuation superseded the old scope")

        self.assertEqual(retired[0]["task_id"], first["task_id"])
        self.assertEqual(self.tasks.get(first["task_id"])["state"], "stale")
        self.assertEqual(self.tasks.get(second["task_id"])["state"], "running")
        snapshot = self.runtime.snapshot()
        self.assertEqual(len(snapshot["open_work_orders"]), 1)
        self.assertEqual(snapshot["open_work_orders"][0]["task_id"], second["task_id"])
        stale = self.store.head("command/departments/research/work-orders/old-order")
        stale_body = json.loads(self.store.read_body(stale["body_hash"]))
        self.assertEqual(stale_body["state"], "stale")

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

    def test_candidate_needs_review_keeps_work_order_open(self):
        request = {
            "id": "review-held-experiment", "kind": "additional_experiment",
            "owner": "methods.validation", "objective": "Resolve the reviewed defect.",
            "why": "The candidate remains provisional.",
            "success_condition": "Independent reviewers verify the repair.",
            "evidence_needed": "A fresh replay and cited result evidence.",
        }
        active = self.runtime.activate_work_orders([request])
        result = self.runtime.resolve_work_orders(
            [request], stage_kind="experiment", outcome="candidate_needs_review")
        self.assertEqual(result[0]["state"], "running")
        self.assertEqual(self.tasks.get(active[0]["task_id"])["state"], "running")
        self.assertEqual(len(self.runtime.snapshot()["open_work_orders"]), 1)

    def test_reactivated_legacy_completed_order_gets_a_new_task_generation(self):
        request = {
            "id": "legacy-review-order", "kind": "additional_experiment",
            "owner": "methods.validation", "objective": "Resolve the reviewed defect.",
            "why": "An older Composer incorrectly closed provisional work.",
            "success_condition": "Independent reviewers verify the repair.",
            "evidence_needed": "A fresh replay and cited result evidence.",
        }
        first = self.runtime.activate_work_orders([request])[0]
        self.runtime.resolve_work_orders([request], stage_kind="experiment", outcome="completed")
        old_task = self.tasks.get(first["task_id"])
        self.assertEqual(old_task["state"], "completed")

        reopened = self.runtime.activate_work_orders([request])[0]
        self.assertNotEqual(reopened["task_id"], first["task_id"])
        self.assertEqual(self.tasks.get(first["task_id"])["state"], "completed")
        self.assertEqual(self.tasks.get(reopened["task_id"])["state"], "running")
        self.assertEqual(request["recovery_generation"], 1)

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
