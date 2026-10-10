import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.errors import ModelContractError
from scisaurus.runtime.composer import ComposerRunner
from scisaurus.runtime.literature_tree import exploration_response_contract, normalize_plan, validate_plan
from scisaurus.runtime.survey import SurveyRunner
from scisaurus.runtime.model_work import ModelWorkCache
from scisaurus.tests import test_composer as fixtures
from scisaurus.tests.test_survey import survey_config


class ResponseRecoveryOwnershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        workflow = fixtures.ComposerWorkflowTests()._workflow(root)
        topic = root / "topic"
        topic.mkdir()
        workflow["stages"].insert(0, {**workflow["stages"][0],
            "id": "topic", "kind": "topic_discovery", "project_dir": str(topic)})
        workflow["stages"][1]["depends_on"] = ["topic"]
        self.runner = ComposerRunner(workflow)
        self.addCleanup(self.runner.close)
        self.stage = self.runner.workflow["stages"][1]
        self.identity = {"topic_id": "current-topic", "topic_cycle": 4}
        self.runner.context["topic"] = {"kind": "topic_discovery", "status": "completed",
            "topic": {"id": "current-topic"}, "topic_cycle": 4}
        self.stale = {"stage_id": "survey", "kind": "survey", "status": "blocked",
            "superseded_topic_id": "previous-topic", "lineage_state": "awaiting_topic_admission",
            "lineage_transition_ref": "artifact:old-transition@1", "topic_pivot_cycle": 4}
        self.runner.context["survey"] = deepcopy(self.stale)
        self.runner.stage_records["survey"] = {"kind": "survey", "status": "running",
            "attempt_id": "current-attempt", "attempt_number": 4,
            "topic_id": "current-topic", "topic_cycle": 4}

    def record_failure(self, attempt_id="current-attempt", identity=None):
        return self.runner._record_failure_recovery(self.stage, self.stage,
            ModelContractError("branch 0 has invalid fields: missing ['evidence'], extra []"),
            None, None, None, 4, attempt_id=attempt_id,
            topic_identity=self.identity if identity is None else identity)

    def restored_failure(self):
        dossier = self.record_failure()
        context = self.runner.context["survey"]
        context.update({key: value for key, value in self.stale.items()
                        if key in {"superseded_topic_id", "lineage_state", "lineage_transition_ref"}})
        attempt = {"attempt_id": "current-attempt", "attempt_number": 4, "state": "failed",
            "failure_class": "model_contract", "failure_dossier_ref": dossier["artifact_ref"],
            "repair_order_issued": False, **self.identity}
        self.runner.stage_records["survey"].update(status="blocked", attempts=[attempt])
        return context, attempt

    def test_current_contract_failure_preserves_own_topic_and_autonomous_repair(self):
        self.record_failure()
        context = self.runner.context["survey"]
        self.assertNotIn("superseded_topic_id", context)
        self.assertEqual(context["topic_lineage"], self.identity)
        self.assertTrue(context["release_blocking"])
        requests = self.runner._continuation_requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["recovery_mode"], "format_repair_then_rerun")
        by_id = {stage["id"]: stage for stage in self.runner.workflow["stages"]}
        self.assertTrue(self.runner._admit_scientific_blocker_recovery(
            self.stage, ModelContractError(context["error"]), {"topic"}, by_id))
        self.assertEqual(self.runner.reopened_stage_ids, {"survey", "experiment"})
        self.assertEqual(self.runner.context["topic"]["topic"]["id"], "current-topic")
        signature = self.runner.context["survey"]["format_recovery_signature"]
        self.assertEqual(self.runner.format_recovery_ledger[signature]["status"], "dispatched")

    def test_wrong_active_attempt_cannot_clear_retirement(self):
        self.record_failure(attempt_id="different-attempt")
        self.assertEqual(self.runner._continuation_requests(), [])
        self.assertEqual(self.runner.context["survey"]["superseded_topic_id"], "previous-topic")

    def test_response_failure_signature_binds_assignment_not_wrapper_or_opaque_digest(self):
        base = "exploration-" + "a" * 64 + " did not satisfy its evidence contract: missing ['evidence']"
        original = self.runner._format_recovery_signature(self.stage, base)
        wrapped = "Unchanged survey input failed 9 time(s): ModelWorkBlocked: ModelWorkBlocked: " + base
        self.assertEqual(original, self.runner._format_recovery_signature(self.stage, wrapped))
        different = base.replace("a" * 64, "b" * 64)
        self.assertNotEqual(original, self.runner._format_recovery_signature(self.stage, different))
        self.assertNotEqual(original, self.runner._format_recovery_signature(
            self.stage, base.replace("evidence", "rationale")))

    def test_same_assignment_repair_cannot_renew_an_exhausted_recovery(self):
        first = self.record_failure()
        context = self.runner.context["survey"]
        signature = context["format_recovery_signature"]
        self.runner.format_recovery_ledger[signature]["status"] = "dispatched"
        self.record_failure()
        self.assertEqual(self.runner.format_recovery_ledger[signature]["status"], "exhausted")
        self.assertEqual(self.runner.context["survey"]["review_status"], "format_recovery_exhausted")
        self.assertEqual(self.runner.context["survey"]["research_requests"], [])

    def test_wrong_topic_cycle_cannot_clear_retirement(self):
        self.record_failure(identity={**self.identity, "topic_cycle": 3})
        self.assertEqual(self.runner._continuation_requests(), [])

    def test_unbound_attempt_cannot_stamp_current_topic(self):
        self.runner.stage_records["survey"].pop("attempt_id")
        self.assertFalse(self.runner._refresh_stage_topic_lineage(
            "survey", self.runner.context["survey"], attempt_id=None, topic_identity=self.identity))
        self.assertIn("superseded_topic_id", self.runner.context["survey"])

    def test_restored_format_failure_requires_immutable_owned_dossier(self):
        context, _ = self.restored_failure()
        self.assertEqual(self.runner._reconcile_restored_stage_topic_lineage(), ["survey"])
        self.assertNotIn("superseded_topic_id", context)
        self.assertEqual(context["topic_lineage"], self.identity)
        self.assertEqual(len(self.runner._continuation_requests()), 1)

    def test_restored_failure_rejects_owner_and_class_mutations(self):
        context, attempt = self.restored_failure()
        original_context, original_attempt = deepcopy(context), deepcopy(attempt)
        variations = [
            ("attempt", "topic_id", "foreign"), ("attempt", "topic_cycle", 3),
            ("attempt", "attempt_number", 5), ("attempt", "state", "unknown"),
            ("attempt", "failure_class", "scientific_review"),
            ("attempt", "failure_dossier_ref", "artifact:missing@1"),
            ("context", "stage_id", "experiment"), ("context", "failure_class", "scientific_review"),
            ("context", "failure_dossier_ref", "artifact:missing@1"),
        ]
        for target, key, value in variations:
            with self.subTest(target=target, key=key):
                context.clear(); context.update(deepcopy(original_context))
                attempt.clear(); attempt.update(deepcopy(original_attempt))
                (context if target == "context" else attempt)[key] = value
                self.assertEqual(self.runner._reconcile_restored_stage_topic_lineage(), [])
                self.assertIn("superseded_topic_id", context)

    def test_foreign_or_missing_dossier_cannot_authorize_restore(self):
        context, attempt = self.restored_failure()
        for stage, number in (("experiment", 4), ("survey", 5)):
            record = self.runner._publish(f"command/foreign/{stage}/{number}", "report",
                {"stage_id": stage, "attempt_number": number, "failure_class": "model_contract"},
                "command.composer")
            context["failure_dossier_ref"] = record["artifact_ref"]
            attempt["failure_dossier_ref"] = record["artifact_ref"]
            self.assertEqual(self.runner._reconcile_restored_stage_topic_lineage(), [])
        context["failure_dossier_ref"] = attempt["failure_dossier_ref"] = "artifact:missing@1"
        self.assertEqual(self.runner._reconcile_restored_stage_topic_lineage(), [])

    def test_scientific_repair_flag_cannot_bypass_contract_dossier_validation(self):
        context, attempt = self.restored_failure()
        attempt["repair_order_issued"] = True
        context["failure_dossier_ref"] = attempt["failure_dossier_ref"] = "artifact:missing@1"
        self.assertEqual(self.runner._reconcile_restored_stage_topic_lineage(), [])
        self.assertIn("superseded_topic_id", context)

    def test_exploration_repair_exposes_required_fields_without_manufacturing_evidence(self):
        assignment = {"phase": "exploration_plan", "question": "Original scientific question",
            "parents": [{"id": "parent-0", "kind": "root"}], "evidence_catalog": []}
        feedback = {"error": "missing ['evidence']", "previous_response": {
            "decision": "expand", "rationale": "Find evidence", "branches": [{
                "parent_id": "parent-0", "question": "Inquiry", "rationale": "Relevant",
                "operation": "search", "query": "physical mechanism", "work_id": None}]}}
        runner = SurveyRunner.__new__(SurveyRunner)
        runner.score = {}
        runner.work_orders = []
        runner._map_input_limit = lambda actor: None
        assignment = runner._follow_up_assignment(assignment)
        repaired = runner._repair_assignment({"assignment": assignment, "actor": "research.search-planner"}, feedback)
        self.assertEqual(repaired["validation_feedback"]["response_contract"], exploration_response_contract())
        self.assertEqual({k: v for k, v in repaired.items() if k != "validation_feedback"}, assignment)
        self.assertEqual(repaired["validation_feedback"]["previous_response"], feedback["previous_response"])
        parents = {"root": {"kind": "root"}}
        with self.assertRaisesRegex(ModelContractError, "missing.*evidence"):
            normalize_plan(feedback["previous_response"], {"parent-0": "root"}, {}, parents=parents)
        authored = deepcopy(feedback["previous_response"])
        authored["branches"][0]["evidence"] = []
        normalized = normalize_plan(authored, {"parent-0": "root"}, {}, parents=parents)
        validate_plan(normalized, parents, {}, max_branches=None)


class SettledResponseRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runner = SurveyRunner(Path(directory.name), survey_config("http://127.0.0.1:1"))
        self.addCleanup(self.runner.control.close)
        self.runner._initialize()
        self.runner.resume_session = {"session": 2}
        self.actor = "research.search-planner"
        self.assignment = self.runner._follow_up_assignment({"phase": "fixture", "question": "A bounded question?"})
        self.task_id = "survey-settled-1"
        self.runner.tasks.create(self.task_id, "service", {"operation": "model"}, "command.controller")
        self.runner.tasks.admit(self.task_id, "command.controller")
        self.runner.tasks.start_attempt(self.task_id, "settled-attempt", owner=self.actor, lease_ttl_seconds=30)
        self.context = self.runner._publish("command/contexts/" + self.task_id, "note", {
            "role": self.actor, "client": self.runner.config["model"], "prompt": json.dumps(self.assignment)}, self.actor)
        self.usage = {"model_calls": 1, "input_tokens": 2, "output_tokens": 1}
        self.execution = self.runner._publish("command/executions/" + self.task_id, "report", {
            "text": '{"decision":"retain"}', "model": "fixture", "usage": self.usage,
            "elapsed_seconds": 1.0, "finish_reason": "stop"}, self.actor, subjects=[self.context["artifact_ref"]])
        self.runner.tasks.finish_attempt("settled-attempt", "succeeded", usage=self.usage)
        self.runner.tasks.transition(self.task_id, "blocked", "command.controller", reason="process interrupted")

    def retained(self, assignment=None, actor=None):
        return self.runner._retained_unreviewed_response("settled", actor or self.actor, assignment or self.assignment)

    def test_settled_unreviewed_response_is_validated_without_a_second_dispatch(self):
        job = {"name": "settled", "actor": self.actor, "assignment": deepcopy(self.assignment),
               "validator": lambda value: self.assertEqual(value, {"decision": "retain"})}
        before = self.runner.tasks.attempts_for_task(self.task_id)
        with patch.object(self.runner, "_call_batch", side_effect=AssertionError("settled response redispatched")):
            value, ref = self.runner._models_checked([job])["settled"]
        self.assertEqual(ref, self.execution["artifact_ref"])
        self.assertEqual(value, {"decision": "retain"})
        self.assertEqual(self.runner.tasks.get(self.task_id)["state"], "completed")
        self.assertEqual(self.runner.tasks.attempts_for_task(self.task_id), before)
        self.assertEqual(self.runner.model_calls_dispatched, 0)

    def test_invalid_response_stays_feedback_for_the_agent(self):
        job = {"name": "settled", "actor": self.actor, "assignment": deepcopy(self.assignment),
               "validator": lambda value: (_ for _ in ()).throw(ModelContractError("required metric absent"))}
        captured = []
        def dispatch(specs, **kwargs):
            captured.extend(specs)
            raise KeyboardInterrupt()
        with patch.object(self.runner, "_call_batch", side_effect=dispatch):
            with self.assertRaises(KeyboardInterrupt):
                self.runner._models_checked([job])
        feedback = json.loads(captured[0]["params"]["prompt"])["validation_feedback"]
        self.assertEqual(feedback["error"], "required metric absent")
        self.assertEqual(feedback["execution_ref"], self.execution["artifact_ref"])
        self.assertNotEqual(self.runner.tasks.get(self.task_id)["state"], "completed")

    def test_interruption_before_success_cache_does_not_complete_or_redispatch(self):
        job = {"name": "settled", "actor": self.actor, "assignment": deepcopy(self.assignment),
               "validator": lambda value: self.assertEqual(value, {"decision": "retain"})}
        with patch.object(ModelWorkCache, "put", side_effect=KeyboardInterrupt()), \
             patch.object(self.runner, "_call_batch", side_effect=AssertionError("settled response redispatched")):
            with self.assertRaises(KeyboardInterrupt): self.runner._models_checked([deepcopy(job)])
        self.assertEqual(self.runner.tasks.get(self.task_id)["state"], "awaiting_review")
        with patch.object(self.runner, "_call_batch", side_effect=AssertionError("settled response redispatched")):
            result = self.runner._models_checked([deepcopy(job)])["settled"]
        self.assertEqual(result[1], self.execution["artifact_ref"])
        self.assertEqual(self.runner.tasks.get(self.task_id)["state"], "completed")

    def test_unknown_failed_and_foreign_receipts_are_not_reused(self):
        for field, value in (("state", "result_unknown"), ("state", "failed"),
                             ("lease_owner", "foreign"), ("finished_at", None)):
            with self.subTest(field=field, value=value):
                original = self.runner.control._conn.execute("SELECT " + field + " FROM attempts").fetchone()[0]
                self.runner.control._conn.execute("UPDATE attempts SET " + field + "=?", (value,))
                self.assertIsNone(self.retained())
                self.runner.control._conn.execute("UPDATE attempts SET " + field + "=?", (original,))
        self.runner.control._conn.execute('UPDATE attempts SET usage_json=?', (json.dumps({"actual": {"model_calls": 0}}),))
        self.assertIsNone(self.retained())

    def test_changed_assignment_actor_and_score_cannot_reuse_the_response(self):
        self.assertIsNone(self.retained({**self.assignment, "question": "Different question?"}))
        self.assertIsNone(self.retained(actor="methods.evidence-verifier"))
        self.runner.score_ref = "artifact:protocols/foreign@1"
        self.assertIsNone(self.retained())

    def test_only_transport_boundary_changes_preserve_assignment(self):
        self.assertIsNotNone(self.retained({**self.assignment, "resume_boundary": "changed",
                                          "_contract_repair_boundary": "changed"}))
        self.assertIsNone(self.retained({**self.assignment, "configured_input": {"value": 1}}))

    def test_checked_failure_keeps_its_bounded_validation_lineage(self):
        self.runner._publish("command/validation/" + self.task_id, "note", {"error": "scientific contract"},
                             "command.controller", subjects=[self.execution["artifact_ref"]])
        self.assertIsNone(self.retained())


if __name__ == "__main__":
    unittest.main()
