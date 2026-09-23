import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scisaurus.cli import _composer_progress_line
from scisaurus.runtime.composer import ComposerRunner, read_interim_report, validate_workflow
from scisaurus.runtime.departments import default_organization
from scisaurus.runtime.literature import ProviderCooldownError
from scisaurus.runtime.model_work import ModelWorkBlocked
from scisaurus.runtime.specialists import build_specialist_prompt
from scisaurus.core.errors import QuotaExceededError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.tests.test_research_program import topic_package


class ComposerWorkflowTests(unittest.TestCase):
    def test_interrupted_runner_result_propagates_process_stop_not_stage_retry(self):
        with self.assertRaises(KeyboardInterrupt):
            ComposerRunner._raise_stage_failure({"status": "paused", "error": "termination requested",
                                                 "failure": {"kind": "process_interrupted"}})

    def _workflow(self, root):
        config = root / "stage.json"
        config.write_text("{}")
        survey_dir = root / "survey"
        experiment_dir = root / "experiment"
        survey_dir.mkdir(); experiment_dir.mkdir()
        return {
            "schema_version": "composer-workflow-1",
            "id": "demo-workflow",
            "revision": 1,
            "project_id": str(root / "composer"),
            "objective": "Exercise a dependency-aware composer loop",
            "stages": [
                {"id": "survey", "kind": "survey", "config_path": str(config.resolve()),
                 "project_dir": str(survey_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                 "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                 "reuse_output_path": None},
                {"id": "experiment", "kind": "experiment", "config_path": str(config.resolve()),
                 "project_dir": str(experiment_dir.resolve()), "depends_on": ["survey"], "estimate_seconds": 1,
                 "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                 "reuse_output_path": None},
            ],
            "time_policy": {"first_result_seconds": 1, "target_seconds": 10,
                             "hard_seconds": 30, "checkpoint_seconds": 1},
            "completion": {"required_stage_ids": ["survey", "experiment"],
                            "release_requires_human": True},
        }

    def test_rejects_dependency_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            workflow["stages"][0]["depends_on"] = ["experiment"]
            with self.assertRaises(ValidationError):
                validate_workflow(workflow)

    def test_topic_to_experiment_requires_an_intervening_survey(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][2]["depends_on"] = ["topic"]
            with self.assertRaisesRegex(ValidationError, "requires a survey between"):
                validate_workflow(workflow)

    def test_independent_experiment_does_not_inherit_unrelated_topic_context(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "unrelated-topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "unrelated_topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            runner.context["unrelated_topic"] = {
                "kind": "topic_discovery",
                "admission_state": "provisional_for_survey",
                "topic": {
                    "id": "unrelated", "title": "Unrelated",
                    "research_question": "This must not bind to the experiment.",
                },
            }
            experiment_stage = next(
                item for item in workflow["stages"] if item["id"] == "experiment")
            config = {"sentinel": "unchanged"}
            self.assertIs(
                runner._apply_topic_to_experiment_config(experiment_stage, config), config)
            self.assertEqual(config, {"sentinel": "unchanged"})
            runner.close()

    def test_agenda_policy_is_strictly_validated(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            workflow["agenda_policy"] = {"mode": "adaptive"}
            self.assertEqual(
                validate_workflow(workflow)["agenda_policy"],
                {"mode": "adaptive"})
            workflow["agenda_policy"] = {"mode": "random"}
            with self.assertRaisesRegex(ValidationError, "agenda_policy"):
                validate_workflow(workflow)

    def test_omitted_agenda_policy_preserves_legacy_declaration_order(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            paper_dir = root / "paper"
            paper_dir.mkdir()
            paper = {
                "id": "paper", "kind": "paper",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(paper_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            survey = workflow["stages"][0]
            workflow["stages"] = [paper, survey]
            workflow["completion"]["required_stage_ids"] = ["paper", "survey"]
            runner = ComposerRunner(workflow)
            try:
                ordered = runner._agenda_order(
                    workflow["stages"], completed=set(),
                    by_id={item["id"]: item for item in workflow["stages"]})
                self.assertEqual(runner._agenda_policy(), {"mode": "ordered"})
                self.assertEqual([item["id"] for item in ordered], ["paper", "survey"])
            finally:
                runner.close()

    def test_custom_organization_must_cover_stage_owners(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            organization = default_organization()
            organization["departments"] = [item for item in organization["departments"] if item["id"] != "research"]
            workflow["organization"] = organization
            with self.assertRaisesRegex(ValidationError, "stage-owning departments"):
                validate_workflow(workflow)

    def test_computational_topic_preferences_are_validated_and_projected(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["topic_preferences"] = {
                "mode": "computational_native",
                "must_have": ["a quantitative estimand"],
                "avoid": ["a generic benchmark"],
            }
            validate_workflow(workflow)
            runner = ComposerRunner(workflow)
            try:
                context = runner._runtime_context({"protocol": "openai", "model": "test"})
                self.assertEqual(
                    context["topic_preferences"], workflow["topic_preferences"])
                self.assertEqual(context["research_feasibility"]["max_model_calls"], 0)
                self.assertEqual(context["research_feasibility"]["max_external_requests"], 0)
                self.assertEqual(context["research_feasibility"]["max_experiment_seconds"], 10)
                workflow["topic_preferences"]["mode"] = "unsupported"
                with self.assertRaisesRegex(ValidationError, "mode must be general"):
                    validate_workflow(workflow)
            finally:
                runner.close()

    def test_resume_reopens_legacy_topic_before_downstream_admission(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_model = root / "topic-model.json"
            topic_model.write_text("{}")
            topic_config = root / "topic.json"
            topic_config.write_text(json.dumps({
                "schema_version": "topic-discovery-config-1",
                "model_config_path": str(topic_model.resolve()),
                "output_path": str((topic_dir / "output" / "topic.json").resolve()),
                "candidate_count": 3, "max_attempts": 1,
            }))
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            validate_workflow(workflow)
            runner = ComposerRunner(workflow)
            try:
                candidate = {
                    "id": "direction_0",
                    "capability_requirements": {
                        "executables": [], "python_packages": [], "stage_kinds": [],
                    },
                }
                runner.stage_records = {"topic": {"status": "completed"}}
                runner.context = {
                    "topic": {
                        "kind": "topic_discovery", "status": "completed",
                        "selected_id": "direction_0", "candidates": [candidate],
                        "topic": candidate,
                    },
                }
                failure = runner._restored_topic_feasibility_failure({
                    stage["id"]: stage for stage in workflow["stages"]})
                self.assertIsNotNone(failure)
                self.assertIn("feasibility", failure[1])
                runner._queue_topic_feasibility_revalidation(*failure)
                completed = {"topic"}
                self.assertTrue(runner._begin_continuation(
                    completed, {stage["id"]: stage for stage in workflow["stages"]}))
                self.assertEqual(completed, set())
                self.assertIn("topic", runner.reopened_stage_ids)
                self.assertEqual(
                    runner.active_research_requests[0]["kind"], "topic_refinement")
            finally:
                runner.close()

    def test_runtime_env_files_load_nested_owner_credentials_before_dispatch(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            nested = root / "qwen.env"
            nested.write_text("SCISAURUS_TEST_NESTED=loaded\n")
            env_file = root / "runtime.env"
            env_file.write_text(
                f"SCISAURUS_QWEN_ENV_FILE={nested.name}\n"
                "SCISAURUS_TEST_RUNTIME=loaded\n")
            workflow["runtime_env_files"] = [str(env_file.resolve())]
            keys = ("SCISAURUS_QWEN_ENV_FILE", "SCISAURUS_TEST_RUNTIME",
                    "SCISAURUS_TEST_NESTED")
            previous = {key: os.environ.get(key) for key in keys}
            for key in keys:
                os.environ.pop(key, None)
            runner = ComposerRunner(workflow)
            try:
                self.assertEqual(os.environ.get("SCISAURUS_TEST_RUNTIME"), "loaded")
                self.assertEqual(os.environ.get("SCISAURUS_TEST_NESTED"), "loaded")
            finally:
                runner.close()
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_runs_stages_and_routes_feedback(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def fake_stage(stage):
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = fake_stage
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(list(result["stages"]), ["survey", "experiment"])
            self.assertEqual(len(result["feedback"]), 2)
            self.assertEqual(result["release_status"], "needs_human_approval")
            self.assertTrue((Path(workflow["project_id"]) / "output" / "run.json").is_file())
            self.assertEqual(result["feedback"][0]["to"], {"dept": "methods", "agent": "chief"})
            self.assertEqual(result["feedback"][1]["to"], {"dept": "executive-command", "agent": "intent-keeper"})
            with sqlite3.connect(Path(workflow["project_id"]) / "state" / "control.sqlite") as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM messages WHERE state='acknowledged'").fetchone()[0], 2)
            self.assertEqual(result["feedback"][0]["message_disposition"], "scheduled")
            self.assertEqual(result["feedback"][1]["message_disposition"], "scheduled")
            self.assertEqual(result["organization"]["backlog_counts"]["research"]["completed"], 1)
            self.assertEqual(result["organization"]["backlog_counts"]["methods"]["completed"], 1)

    def test_adaptive_agenda_treats_stage_order_as_dependencies_not_itinerary(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            paper_dir = root / "paper"
            paper_dir.mkdir()
            paper = {
                "id": "paper", "kind": "paper",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(paper_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            survey = workflow["stages"][0]
            workflow["stages"] = [paper, survey]
            workflow["completion"]["required_stage_ids"] = ["paper", "survey"]
            workflow["agenda_policy"] = {"mode": "adaptive"}
            workflow["exploration_seed"] = 7
            runner = ComposerRunner(workflow)
            calls = []

            def fake_stage(stage, **_kwargs):
                calls.append(stage["id"])
                output = root / f"{stage['id']}-adaptive.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {
                    "status": "completed", "output_path": str(output),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                }

            runner._run_stage = fake_stage
            result = runner.run()
            self.assertEqual(calls, ["survey", "paper"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["agenda_policy"], {"mode": "adaptive"})
            first = result["agenda_decisions"][0]
            self.assertEqual(first["selected_stage_id"], "survey")
            self.assertEqual(
                [item["stage_id"] for item in first["candidate_stages"]],
                ["survey", "paper"])
            self.assertEqual(result["research_state"]["phase"], "release_candidate")

    def test_adaptive_retry_yields_to_other_ready_work_before_replanning(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            paper_dir = root / "paper"
            paper_dir.mkdir()
            paper = {
                "id": "paper", "kind": "paper",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(paper_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            survey = workflow["stages"][0]
            workflow["stages"] = [paper, survey]
            workflow["completion"]["required_stage_ids"] = ["paper", "survey"]
            workflow["agenda_policy"] = {"mode": "adaptive"}
            workflow["retry_policy"] = {"mode": "until_deadline", "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def flaky_stage(stage, **_kwargs):
                calls.append(stage["id"])
                if stage["id"] == "survey" and calls.count("survey") == 1:
                    raise RuntimeError("survey provider failed once")
                output = root / f"{stage['id']}-{len(calls)}.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {
                    "status": "completed", "output_path": str(output),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                }

            runner._run_stage = flaky_stage
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["survey", "paper", "survey"])
            self.assertEqual(
                [item["selected_stage_id"] for item in result["agenda_decisions"][:3]],
                ["survey", "paper", "survey"],
            )
            self.assertTrue(any(
                item.get("action") == "yield_retry_to_agenda"
                for item in result["department_activity"]))
            self.assertEqual(result["retry_schedule"], {})

    def test_resume_restores_agenda_frontier_and_decision_history(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["agenda_policy"] = {"mode": "adaptive"}
            workflow["exploration_seed"] = 19
            runner = ComposerRunner(workflow)
            by_id = {stage["id"]: stage for stage in workflow["stages"]}
            ordered = runner._agenda_order(
                [workflow["stages"][0]], completed=set(), by_id=by_id)
            self.assertEqual(ordered[0]["id"], "survey")
            runner._checkpoint("agenda:test:selected", force=True)
            decision_ref = runner.agenda_decisions[0]["artifact_ref"]
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(len(resumed.agenda_decisions), 1)
                self.assertEqual(
                    resumed.agenda_decisions[0]["artifact_ref"], decision_ref)
                state = resumed._research_state()
                self.assertEqual(state["frontier_stage_ids"], ["survey"])
                self.assertEqual(
                    state["last_agenda_decision"]["selected_stage_id"], "survey")
            finally:
                resumed.close()

    def test_resume_preserves_persisted_adaptive_policy_for_legacy_workflow(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            self.assertNotIn("agenda_policy", workflow)
            runner = ComposerRunner(workflow)
            runner._restored_agenda_policy = {"mode": "adaptive"}
            runner._checkpoint("agenda:migrated", force=True)
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(resumed._agenda_policy(), {"mode": "adaptive"})
            finally:
                resumed.close()

    def test_newer_agenda_checkpoint_supersedes_stale_terminal_report(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["agenda_policy"] = {"mode": "adaptive"}
            runner = ComposerRunner(workflow)
            by_id = {stage["id"]: stage for stage in workflow["stages"]}
            runner._agenda_order([workflow["stages"][0]], completed=set(), by_id=by_id)
            runner._checkpoint("agenda:test:selected", force=True)
            checkpoint_revision = runner.state_revision
            runner._publish("command/composer/run", "report", {
                "schema_version": "composer-run-1",
                "workflow_id": workflow["id"],
                "run_id": runner.run_id,
                "status": "paused",
                "state_revision": checkpoint_revision - 1,
                "stages": {}, "context": {}, "feedback": [], "blockers": [],
                "usage": {}, "agenda_decisions": [],
            }, "command.composer")
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(resumed.state_revision, checkpoint_revision)
                self.assertEqual(len(resumed.agenda_decisions), 1)
                self.assertEqual(
                    resumed.agenda_decisions[0]["selected_stage_id"], "survey")
            finally:
                resumed.close()

    def test_fake_stage_end_to_end_records_specialist_activation_and_verdict(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def fake_stage(stage):
                output = root / f"{stage['id']}-specialist-result.json"
                output.write_text(json.dumps({"stage": stage["id"], "checked": True}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = fake_stage
            result = runner.run()
            self.assertEqual(result["organization"]["schema_version"], "project-organization-2")
            for stage_id in ("survey", "experiment"):
                record = result["stages"][stage_id]
                self.assertEqual(record["active_agents"], [])
                self.assertTrue(record["last_active_agents"])
                self.assertTrue(record["assignment_ids"])
                self.assertNotEqual(record["chief_agent"], record["verifier_agent"])
                self.assertTrue(record["verifier_artifact_ref"].startswith("artifact:"))
                self.assertEqual(record["verifier_outcome"], "accepted")
            self.assertEqual(result["organization"]["active_assignments"], [])
            assignment_count = sum(
                counts["completed"] for counts in result["organization"]["assignment_counts"].values())
            self.assertGreaterEqual(assignment_count, 2)
            with sqlite3.connect(root / "composer" / "state" / "control.sqlite") as conn:
                assignment_tasks = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE payload_json LIKE '%assignment_id%'").fetchone()[0]
            self.assertGreaterEqual(assignment_tasks, 2)

    def test_topic_specialists_review_generated_evidence_not_an_empty_frontier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"] = [{
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            runner = ComposerRunner(workflow)
            specialist_packets = []

            def fake_stage(stage, **_kwargs):
                output = root / "topic-result.json"
                output.write_text(json.dumps({"status": "completed"}))
                return {
                    "status": "completed", "output_path": str(output),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                    "topic": {"id": "direction-1", "research_question": "Does A change B?"},
                    "candidates": [{"id": "direction-1", "research_question": "Does A change B?"}],
                    "frontier_seed_plan": {"seeds": [{"id": "frontier-1", "domain": "A"}]},
                    "recent_papers": [{"work_id": "W1", "title": "A study"}],
                    "candidate_prior_work": [{"work_id": "W1", "title": "A study"}],
                    "feasibility_check": {"status": "feasible"},
                }

            def fake_pool(stage, assignment, descriptor, *, stage_result=None):
                specialist_packets.append(stage_result)
                return {"reports": [], "by_role": {}, "usage": {}, "model_enabled": False}

            runner._run_stage = fake_stage
            runner._run_specialist_pool = fake_pool
            result = runner.run()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(specialist_packets), 1)
            packet = runner._specialist_stage_result_projection(
                workflow["stages"][0], specialist_packets[0])
            self.assertEqual(packet["candidate_topics"][0]["id"], "direction-1")
            self.assertEqual(packet["frontier_seeds"][0]["id"], "frontier-1")
            self.assertEqual(packet["scholarly_records"][0]["work_id"], "W1")
            self.assertEqual(packet["prior_work"][0]["work_id"], "W1")
            runner.close()

    def test_provisional_topic_verifier_hold_becomes_survey_requirements(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            calls = []

            def fake_stage(stage, **_kwargs):
                calls.append(stage["id"])
                output = root / f"{stage['id']}-provisional.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                result = {
                    "status": "completed", "output_path": str(output),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                }
                if stage["id"] == "topic":
                    result.update({
                        "admission_state": "provisional_for_survey",
                        "next_evidence_action": "literature_survey",
                        "maturity_open_requirements": ["Ground the comparator."],
                        "topic": {
                            "id": "direction-1", "title": "A provisional direction",
                            "research_question": "Does A distinguish B from C?",
                        },
                    })
                return result

            def verifier(stage, *_args, **_kwargs):
                response = ({
                    "decision": "hold",
                    "rationale": "The reference dataset must be located.",
                    "critical_findings": ["The reference dataset is not pinned."],
                    "repair_scope": ["Locate and verify the reference dataset in the survey."],
                } if stage["id"] == "topic" else {
                    "decision": "accept", "rationale": "The bounded result is supported.",
                    "critical_findings": [], "repair_scope": [],
                })
                return {"status": "succeeded", "response": response, "usage": {}}

            runner._run_stage = fake_stage
            runner._run_specialist_pool = lambda *_args, **_kwargs: {
                "reports": [], "by_role": {}, "usage": {}, "model_enabled": False,
            }
            runner._publish_specialist_reports = lambda _stage, _assignment, bundle: bundle
            runner._run_specialist_verifier = verifier
            result = runner.run()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["topic", "survey", "experiment"])
            topic = result["context"]["topic"]
            self.assertIn(
                "Locate and verify the reference dataset in the survey.",
                topic["maturity_open_requirements"],
            )
            self.assertEqual(
                topic["provisional_adversarial_challenge"]["control_disposition"],
                "carried_to_literature_survey",
            )
            self.assertEqual(result["stages"]["topic"]["verifier_outcome"], "hold")
            runner.close()

    def test_candidate_release_is_forwarded_for_principal_review(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def fake_stage(stage):
                output = root / f"{stage['id']}-result.pdf"
                output.write_bytes(b"candidate")
                return {"status": "candidate_needs_review" if stage["id"] == "experiment" else "completed",
                        "output_path": str(output), "project_dir": stage["project_dir"],
                        "stage_id": stage["id"]}

            runner._run_stage = fake_stage
            result = runner.run()
            self.assertEqual(result["status"], "candidate_needs_review")
            self.assertEqual(result["release_status"], "candidate_needs_review")
            self.assertEqual(result["feedback"][-1]["status"], "candidate_needs_review")
            self.assertEqual(result["feedback"][-1]["action"], "advance")
            self.assertEqual(result["feedback"][-1]["to"], {"dept": "executive-command", "agent": "intent-keeper"})
            self.assertIn("principal review", result["feedback"][-1]["next_condition"])

    def test_paper_release_gate_fences_provisional_scientific_ancestors(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            argument_dir = root / "argument"
            paper_dir = root / "paper"
            argument_dir.mkdir()
            paper_dir.mkdir()
            config_path = workflow["stages"][0]["config_path"]
            workflow["stages"].extend([
                {
                    "id": "argument", "kind": "argument", "config_path": config_path,
                    "project_dir": str(argument_dir.resolve()), "depends_on": ["experiment"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
                {
                    "id": "paper", "kind": "paper", "config_path": config_path,
                    "project_dir": str(paper_dir.resolve()), "depends_on": ["argument"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
            ])
            runner = ComposerRunner(workflow)
            try:
                runner.stage_records = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "composer_decision": "advance_with_findings",
                        "release_blocking": False,
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                    "paper": {
                        "kind": "paper", "status": "candidate_needs_review",
                        "forward_progress": True,
                        "composer_decision": "advance_with_findings",
                        "release_blocking": False,
                        "failure_debt": {},
                    },
                }
                runner.context = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "verifier_outcome": "hold", "gap_state": "insufficient_evidence",
                        "topic_admission": "exploratory_pilot", "survey_current": False,
                        "assessment_current": False,
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                paper = by_id["paper"]
                blockers = runner._paper_release_blockers(paper, by_id)
                self.assertEqual([item["stage_id"] for item in blockers], ["survey"])
                self.assertIn("provisional_candidate", blockers[0]["reasons"])
                self.assertIn("verifier_hold", blockers[0]["reasons"])
                self.assertFalse(ComposerRunner._stage_releases_dependencies(
                    {"status": "candidate_needs_review", "composer_decision": "advance_with_findings"},
                    stage_kind="paper"))

                completed = {"survey", "experiment", "argument"}
                release_blocked = set()
                self.assertTrue(runner._refresh_paper_release_gate(
                    completed, by_id, release_blocked))
                self.assertIn("paper", release_blocked)
                self.assertEqual(
                    runner.stage_records["paper"]["release_gate"]["kind"],
                    "upstream_scientific_hold")

                runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
                runner.context["survey"] = {"kind": "survey", "status": "completed"}
                self.assertTrue(runner._refresh_paper_release_gate(
                    completed, by_id, release_blocked))
                self.assertNotIn("paper", release_blocked)
                self.assertEqual(runner.stage_records["paper"]["status"], "pending")
            finally:
                runner.close()

    def test_paper_is_not_dispatched_while_upstream_candidate_is_provisional(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            argument_dir = root / "argument"
            paper_dir = root / "paper"
            argument_dir.mkdir()
            paper_dir.mkdir()
            config_path = workflow["stages"][0]["config_path"]
            workflow["stages"].extend([
                {
                    "id": "argument", "kind": "argument", "config_path": config_path,
                    "project_dir": str(argument_dir.resolve()), "depends_on": ["experiment"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
                {
                    "id": "paper", "kind": "paper", "config_path": config_path,
                    "project_dir": str(paper_dir.resolve()), "depends_on": ["argument"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
            ])
            workflow["completion"]["required_stage_ids"] = ["paper"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 0}
            runner = ComposerRunner(workflow)
            calls = []
            try:
                runner.stage_records = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "composer_decision": "advance_with_findings",
                        "release_blocking": False,
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }
                runner.context = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "verifier_outcome": "hold",
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }

                def must_not_dispatch(stage, **_kwargs):
                    calls.append(stage["id"])
                    raise AssertionError("paper was dispatched behind a scientific hold")

                runner._run_stage = must_not_dispatch
                result = runner.run()
                self.assertEqual(result["status"], "candidate_needs_review")
                self.assertEqual(calls, [])
                self.assertEqual(
                    result["stages"]["paper"]["release_gate"]["kind"],
                    "upstream_scientific_hold")
                self.assertTrue(any(
                    item.get("stop_reason") == "upstream_scientific_hold"
                    for item in result["blockers"]))
            finally:
                runner.close()

    def test_paper_gate_admits_scoped_repair_before_returning_a_candidate(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            argument_dir = root / "argument"
            paper_dir = root / "paper"
            argument_dir.mkdir()
            paper_dir.mkdir()
            config_path = workflow["stages"][0]["config_path"]
            workflow["stages"].extend([
                {
                    "id": "argument", "kind": "argument", "config_path": config_path,
                    "project_dir": str(argument_dir.resolve()), "depends_on": ["experiment"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
                {
                    "id": "paper", "kind": "paper", "config_path": config_path,
                    "project_dir": str(paper_dir.resolve()), "depends_on": ["argument"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
            ])
            workflow["completion"]["required_stage_ids"] = ["paper"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            runner = ComposerRunner(workflow)
            calls = []
            try:
                runner.stage_records = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "composer_decision": "advance_with_findings",
                        "release_blocking": False,
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }
                runner.context = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "verifier_outcome": "hold", "gap_state": "insufficient_evidence",
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }

                def recover(stage, **_kwargs):
                    calls.append(stage["id"])
                    output = root / f"{stage['id']}-repaired.json"
                    output.write_text(json.dumps({"stage": stage["id"], "repaired": True}))
                    return {
                        "status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"],
                    }

                runner._run_stage = recover
                result = runner.run()
                self.assertEqual(result["status"], "completed")
                self.assertEqual(calls, ["survey", "experiment", "argument", "paper"])
                self.assertEqual(result["continuation_cycles"], 1)
                self.assertTrue(any(
                    item.get("action") == "paper_release_gate"
                    and item.get("repair_requests")
                    for item in result["department_activity"]))
            finally:
                runner.close()

    def test_scientific_hold_remains_visible_in_stage_task_backlog(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 0}
            runner = ComposerRunner(workflow)

            def held(stage, **kwargs):
                output = root / f"{stage['id']}-hold.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "research_expansion_required", "output_path": str(output),
                        "project_dir": stage["project_dir"], "research_expansion_requests": []}

            runner._run_stage = held
            result = runner.run()
            self.assertEqual(result["status"], "research_expansion_required")
            self.assertEqual(result["organization"]["backlog_counts"]["research"]["awaiting_review"], 1)

    def test_topic_verifier_hold_reopens_topic_before_admitting_survey(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "review_rejected",
                "specialist_verifier": {
                    "response": {
                        "decision": "hold",
                        "repair_scope": ["cite the decisive parameter source"],
                    },
                },
            }
            runner.stage_records["topic"] = {
                "kind": "topic_discovery", "status": "review_rejected",
            }

            requests = runner._continuation_requests()
            self.assertEqual([item["kind"] for item in requests], ["topic_refinement"])
            self.assertEqual(requests[0]["evidence_needed"], "cite the decisive parameter source")
            self.assertNotIn("manuscript_revision", {item["kind"] for item in requests})

            completed = {"topic"}
            by_id = {stage["id"]: stage for stage in workflow["stages"]}
            self.assertTrue(runner._begin_continuation(completed, by_id))
            self.assertNotIn("topic", completed)
            self.assertNotIn("survey", completed)
            self.assertEqual(runner.reopened_stage_ids,
                             {"topic", "survey", "experiment"})
            runner.close()

    def test_scientific_hold_without_request_gets_a_cycle_specific_recovery_strategy(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.context["experiment"] = {
                "kind": "experiment", "status": "research_expansion_required",
                "research_expansion_requests": [],
                "specialist_verifier": {
                    "response": {
                        "decision": "hold",
                        "critical_findings": ["The current control cannot separate the explanations."],
                    },
                },
            }

            first = runner._continuation_requests()
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["kind"], "additional_experiment")
            self.assertEqual(first[0]["owner"], "methods.validation")
            self.assertIn("discriminating", first[0]["objective"])
            runner._attempted_request_signatures.add(
                runner._research_request_signature(first[0]))

            runner.continuation_cycles = 1
            second = runner._continuation_requests()
            self.assertEqual(len(second), 1)
            self.assertNotEqual(first[0]["id"], second[0]["id"])
            self.assertNotEqual(first[0]["objective"], second[0]["objective"])
            self.assertIn("boundary", second[0]["objective"])
            runner.close()

    def test_blocked_scientific_assignment_admits_a_fresh_recovery_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            runner = ComposerRunner(workflow)
            try:
                runner.context["survey"] = {"kind": "survey", "status": "completed"}
                runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
                runner.stage_records["experiment"] = {"kind": "experiment", "status": "blocked"}
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = {"survey"}
                admitted = runner._admit_scientific_blocker_recovery(
                    workflow["stages"][1],
                    ModelWorkBlocked("independent review rejected the estimator"),
                    completed, by_id)
                # This fixture has no topic ancestor, so there is no changed
                # scientific frontier to reopen. Preserve the blocker instead
                # of manufacturing a same-stage repair loop.
                self.assertFalse(admitted)
                self.assertEqual(runner.continuation_cycles, 0)
                self.assertNotIn("experiment", completed)
                self.assertEqual(runner.active_research_requests, [])
            finally:
                runner.close()

    def test_pre_execution_capability_failure_pivots_instead_of_parking_a_candidate(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"].insert(0, topic_stage)
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            workflow["progression_policy"] = "forward_first"
            workflow["completion"]["required_stage_ids"] = ["topic", "survey", "experiment"]
            runner = ComposerRunner(workflow)
            try:
                error = "ModelWorkBlocked: capability foundry did not admit a program: metric is nan"
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "old", "title": "Old direction",
                              "research_question": "Does A change B?"},
                }
                runner.context["survey"] = {"kind": "survey", "status": "completed"}
                runner.context["experiment"] = {
                    "kind": "experiment", "status": "candidate_needs_review",
                    "results_status": "not_executed", "failure_debt": {
                    "attempts": 3, "error": error,
                    },
                }
                runner.stage_records["topic"] = {"kind": "topic_discovery", "status": "completed"}
                runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
                runner.stage_records["experiment"] = {
                    "kind": "experiment", "status": "candidate_needs_review",
                    "attempt_count": 3, "error": error,
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = {"topic", "survey"}
                self.assertTrue(runner._admit_scientific_blocker_recovery(
                    workflow["stages"][2], ModelWorkBlocked(error), completed, by_id))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertEqual(runner.context["topic"]["status"], "research_expansion_required")
                self.assertTrue(any(
                    item.get("action") == "pivot_topic_after_scientific_blocker"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_survey_evidence_contract_failure_pivots_instead_of_replaying_assessment(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"] = ["topic", "survey", "experiment"]
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "old", "title": "Old direction",
                              "research_question": "Does A change B?"},
                }
                runner.context["survey"] = {
                    "kind": "survey", "status": "blocked",
                    "error": "gap-assessment did not satisfy its evidence contract",
                }
                runner.stage_records["topic"] = {"kind": "topic_discovery", "status": "completed"}
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "blocked", "attempt_count": 2,
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = {"topic"}
                error = ModelWorkBlocked(
                    "survey-gap-assessment did not satisfy its evidence contract: "
                    "unknown or duplicate required check"
                )
                self.assertTrue(runner._admit_scientific_blocker_recovery(
                    workflow["stages"][1], error, completed, by_id))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertEqual(runner.context["topic"]["status"], "research_expansion_required")
                self.assertEqual(
                    runner.context["topic"]["research_expansion_requests"][0]["kind"],
                    "topic_refinement",
                )
                self.assertTrue(any(
                    item.get("action") == "pivot_topic_after_scientific_blocker"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_resume_reconciles_interrupted_survey_contract_before_dispatch(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"] = ["topic", "survey", "experiment"]
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 9
                runner.continuation_pending_stage_ids = {"survey", "experiment"}
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "old", "title": "Old direction",
                              "research_question": "Does A change B?"},
                }
                runner.context["survey"] = {
                    "kind": "survey", "status": "research_expansion_required",
                    "error": (
                        "Unchanged survey input failed 1 time(s): ModelWorkBlocked: "
                        "gap-assessment did not satisfy its evidence contract: "
                        "model generation did not finish normally: length"
                    ),
                }
                runner.stage_records["topic"] = {"kind": "topic_discovery", "status": "completed"}
                runner.stage_records["survey"] = {"kind": "survey", "status": "running"}
                runner.active_research_requests = []
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = {"topic"}
                self.assertTrue(runner._resume_stale_survey_contract_pivot(completed, by_id))
                self.assertEqual(runner.continuation_cycles, 10)
                self.assertEqual(runner.context["topic"]["status"], "research_expansion_required")
                self.assertEqual(
                    runner.context["topic"]["research_expansion_requests"][0]["kind"],
                    "topic_refinement",
                )
            finally:
                runner.close()

    def test_continuation_fences_stale_stage_aggregate_and_unknown_attempt(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                aggregate = runner._stage_task(workflow["stages"][0])
                aggregate_attempt = f"{aggregate['task_id']}-attempt"
                runner.tasks.start_attempt(
                    aggregate["task_id"], aggregate_attempt, owner="command.composer",
                    lease_ttl_seconds=60, payload={"stage_id": "survey"})
                specialist_id = "composer-demo-workflow-survey-specialist"
                runner.tasks.create(
                    specialist_id, "production",
                    {"stage_id": "survey", "assignment_id": "specialist-1"},
                    "research.search-strategist")
                runner.tasks.admit(specialist_id, "command.composer")
                specialist_attempt = f"{specialist_id}-attempt"
                runner.tasks.start_attempt(
                    specialist_id, specialist_attempt, owner="research.search-strategist",
                    lease_ttl_seconds=60, payload={"assignment_id": "specialist-1"})

                retired = runner._retire_superseded_stage_tasks(
                    {"survey"}, reason="superseded by a newly admitted continuation")

                self.assertEqual(retired, [{
                    "task_id": aggregate["task_id"],
                    "stage_id": "survey",
                    "state": "stale",
                }])
                self.assertEqual(runner.tasks.get(aggregate["task_id"])["state"], "stale")
                self.assertEqual(
                    runner.tasks.get_attempt(aggregate_attempt)["state"], "result_unknown")
                self.assertEqual(runner.tasks.get(specialist_id)["state"], "running")
                self.assertEqual(
                    runner.tasks.get_attempt(specialist_attempt)["state"], "started")
            finally:
                runner.close()

    def test_known_topic_contract_debt_is_eligible_for_one_resume_repair(self):
        stage = {"kind": "topic_discovery"}
        record = {
            "status": "candidate_needs_review",
            "failure_debt": {
                "failure_class": "mechanical_contract",
                "error": "ValidationError: feasibility_plan.project_artifact must declare a project_artifact input",
            },
        }
        self.assertTrue(
            ComposerRunner._release_blocked_topic_contract_retry_allowed(stage, record))
        record["contract_recovery_admitted"] = True
        self.assertFalse(
            ComposerRunner._release_blocked_topic_contract_retry_allowed(stage, record))

    def test_experiment_recovery_specialists_receive_a_bounded_scientific_brief(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "question": "Does the diagnostic survive the finite-size boundary?",
                    "feasibility_check": {"plan": {
                        "execution_mode": "foundry",
                        "required_executables": ["python3"],
                        "required_packages": ["numpy"],
                        "network_access": False,
                        "evidence_inputs": [{"source": "W1"}],
                    }},
                    "topic": {
                        "id": "direction_0",
                        "research_question": "Does the diagnostic survive the finite-size boundary?",
                        "hypothesis": "The contrast weakens below a boundary.",
                        "comparison": "diagnostic A versus diagnostic B",
                        "measurement": "difference in predictive power",
                        "disconfirmation_test": "No size-dependent difference.",
                        "resource_plan": "Deterministic finite-matrix simulation.",
                    },
                    "research_program": {"branches": [{
                        "id": "direction_0",
                        "hypothesis": "The contrast weakens below a boundary.",
                    }]},
                }
                stage = next(item for item in workflow["stages"] if item["id"] == "experiment")
                packet = runner._specialist_stage_packet(
                    stage, {"experiment": {"primary_outcomes": [{"id": "gap"}]}})
                assignment = {
                    "assigned_role": "methods.methodologist",
                    "model_role": "methods.methodologist",
                    "stage_id": "experiment",
                    "stage_kind": "experiment",
                    "role_id": "methodologist",
                    "system_contract": "Design a falsifiable experiment.",
                    "input_projection": ["research_question", "hypotheses",
                                          "method_constraints", "available_assets"],
                    "quota": {"max_input_tokens": 12000},
                }
                prompt = json.loads(build_specialist_prompt(assignment, packet))
                projected = prompt["projected_input"]
                self.assertEqual(projected["research_question"],
                                 "Does the diagnostic survive the finite-size boundary?")
                self.assertEqual(projected["hypotheses"],
                                 "The contrast weakens below a boundary.")
                self.assertEqual(projected["available_assets"]["required_packages"], ["numpy"])
                self.assertEqual(prompt["shared_stage_context"]["stage_kind"], "experiment")
            finally:
                runner.close()

    def test_experiment_specialist_brief_does_not_leak_an_unrelated_template(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                capability_path = root / "finite-size-capability.json"
                capability_path.write_text(json.dumps({
                    "schema_version": "experiment-capability-1",
                    "capability_id": "finite_size_winding_breakdown",
                    "experiment": {
                        "id": "finite_size_winding_breakdown",
                        "research_question": "Does the diagnostic survive the finite-size boundary?",
                        "study_type": "exploratory",
                        "method": "Seeded finite-matrix simulation.",
                        "parameters": {"sizes": [16, 32]},
                        "primary_outcomes": [{"id": "rho_l16", "unit": "dimensionless"}],
                        "stopping_rule": "Run the declared grid exactly once.",
                        "limitations": ["Only the declared finite-size grid is covered."],
                    },
                }))
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "question": "Does the diagnostic survive the finite-size boundary?",
                    "topic": {
                        "id": "direction_0",
                        "experiment_capability_id": "finite_size_winding_breakdown",
                        "research_question": "Does the diagnostic survive the finite-size boundary?",
                        "hypothesis": "The contrast weakens below a boundary.",
                        "comparison": "diagnostic A versus diagnostic B",
                        "measurement": "difference in predictive power",
                        "disconfirmation_test": "No size-dependent difference.",
                    },
                    "generated_capability": {
                        "capability_id": "finite_size_winding_breakdown",
                        "descriptor_path": str(capability_path.resolve()),
                    },
                }
                stage = next(item for item in workflow["stages"] if item["id"] == "experiment")
                packet = runner._specialist_stage_packet(stage, {
                    "experiment": {
                        "id": "robust_mean_pilot",
                        "research_question": "Does median-of-means reduce contaminated tail error?",
                        "primary_outcomes": [{"id": "contamination_p95_reduction_percent"}],
                    },
                })
                self.assertEqual(packet["analysis_plan"]["primary_outcomes"][0]["id"], "rho_l16")
                self.assertEqual(packet["analysis_plan"]["capability_source"],
                                 "admitted_topic_capability")
                self.assertEqual(packet["execution_manifest"]["capability_source"],
                                 "admitted_topic_capability")
            finally:
                runner.close()

    def test_invalid_continuation_request_is_rejected_without_blocking_composer(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            runner = ComposerRunner(workflow)

            def held_or_completed(stage, **kwargs):
                output = root / f"{stage['id']}-invalid-request.json"
                if stage["id"] == "survey":
                    payload = {"status": "completed"}
                else:
                    payload = {
                        "status": "research_expansion_required",
                        "research_expansion_requests": [{
                            "id": "bad-owner-request", "kind": "additional_experiment",
                            "owner": "unknown.department", "objective": "Do the control.",
                            "why": "The result is ambiguous.",
                            "success_condition": "The explanation is separated.",
                            "evidence_needed": "Validated raw output.",
                        }],
                    }
                output.write_text(json.dumps(payload))
                return {**payload, "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = held_or_completed
            result = runner.run()
            self.assertEqual(result["status"], "research_expansion_required")
            self.assertFalse(result["blockers"])
            self.assertTrue(any(item.get("action") == "reject_work_order"
                                for item in result["department_activity"]))

    def test_retries_failed_survey_in_its_durable_project_directory(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {"max_attempts": 3, "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def flaky_stage(stage, **kwargs):
                calls.append(stage["project_dir"])
                if len(calls) == 1:
                    raise RuntimeError("transient provider failure")
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"], "attempt": len(calls)}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = flaky_stage
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(result["stages"]["survey"]["attempts"]), 2)
            self.assertEqual(result["stages"]["survey"]["attempts"][0]["state"], "failed")
            self.assertEqual(result["stages"]["survey"]["attempts"][1]["state"], "succeeded")
            self.assertEqual(calls[1], calls[0])
            self.assertTrue(any(item["action"] == "retry_stage" for item in result["feedback"]))

    def test_survey_retry_migrates_to_latest_legacy_attempt_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            stage = self._workflow(root)["stages"][0]
            legacy = Path(stage["project_dir"]) / "attempts" / "attempt-7" / "state"
            legacy.mkdir(parents=True)
            (legacy / "control.sqlite").write_bytes(b"checkpoint")
            candidate = ComposerRunner._attempt_stage(stage, 8)
            self.assertEqual(
                Path(candidate["project_dir"]),
                legacy.parent,
            )

            base_state = Path(stage["project_dir"]) / "state"
            base_state.mkdir(parents=True)
            (base_state / "control.sqlite").write_bytes(b"newer-base")
            candidate = ComposerRunner._attempt_stage(stage, 9)
            self.assertEqual(Path(candidate["project_dir"]), Path(stage["project_dir"]).resolve())

    def test_survey_resume_scope_keeps_completed_milestones(self):
        self.assertEqual(ComposerRunner._survey_resume_scope({
            "survey_current": True, "survey_ref": "artifact:kb/surveys/current@4",
            "assessment_current": False, "status": "blocked"}), "gap_assessment")
        for prior in ({}, {"survey_current": True},
                      {"survey_current": False, "survey_ref": "artifact:kb/surveys/current@4"}):
            self.assertEqual(ComposerRunner._survey_resume_scope(prior), "focused_review")

    def test_exploratory_admission_is_bound_before_capability_authoring(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            self.addCleanup(runner.close)
            runner.workflow["progression_policy"] = "full_pass"
            runner.workflow["capability_foundry_config_path"] = "configured-foundry"
            runner.context["topic"] = {"kind": "topic_discovery", "topic": {
                "id": "direction", "domain": "physics", "research_question": "A bounded question?"}}
            runner.context["survey"] = {"kind": "survey", "topic_admission": "exploratory_pilot",
                "gap_state": "insufficient_evidence", "survey_current": True, "assessment_current": True}
            with patch.object(runner, "_materialize_topic_capability", side_effect=RuntimeError("authoring boundary")) as author:
                with self.assertRaisesRegex(RuntimeError, "authoring boundary"):
                    runner._apply_topic_to_experiment_config(runner.workflow["stages"][1], {"experiment": {}})
            self.assertEqual(author.call_args.kwargs["study_type"], "exploratory")

    def test_foundry_usage_is_charged_once_and_restored_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            self.addCleanup(runner.close)
            runner._publish("command/foundry-work/fixture", "note", {
                "status": "repairing", "usage": {"model_calls": 2, "input_tokens": 300, "output_tokens": 70}},
                "command.controller")
            self.assertTrue(runner._sync_foundry_usage())
            self.assertEqual(runner.usage["model_calls"], 2)
            self.assertFalse(runner._sync_foundry_usage())
            runner._checkpoint("experiment:capability_validation_failed", force=True)
            runner._publish("command/foundry-work/fixture", "note", {
                "status": "calling", "usage": {"model_calls": 3, "input_tokens": 300, "output_tokens": 70}},
                "command.controller")
            workflow = runner.workflow
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)
            self.addCleanup(resumed.close)
            self.assertEqual(resumed.usage["model_calls"], 3)
            self.assertEqual(resumed.usage["input_tokens"], 300)
            self.assertFalse(resumed._sync_foundry_usage())

    def test_legacy_survey_resume_reads_immutable_runner_config(self):
        with tempfile.TemporaryDirectory() as path:
            project = Path(path) / "survey"
            control = ControlStore(project)
            store = ArtifactStore(control)
            store.init_project(principal_note="legacy-survey")
            stored = {
                "project_id": str(project.resolve()),
                "limits": {"wall_clock_seconds": 1200},
                "survey": {"question": "the admitted question"},
            }
            store.publish_artifact(
                logical_id="inputs/run-config", artifact_type="note", author="principal",
                body=canonical_bytes(stored), media_type="application/json",
            )
            control.close()

            self.assertEqual(ComposerRunner._durable_stage_config(project), stored)

    def test_retry_policy_exhaustion_keeps_failures_and_blocks(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {"max_attempts": 2, "backoff_seconds": 0}
            runner = ComposerRunner(workflow)

            def failed_stage(stage, **kwargs):
                raise RuntimeError("persistent provider failure")

            runner._run_stage = failed_stage
            result = runner.run()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["stages"]["survey"]["attempt_count"], 2)
            self.assertEqual(len(result["stages"]["survey"]["attempts"]), 2)
            self.assertEqual(result["blockers"][0]["attempts"], 2)
            self.assertEqual(sum(item["action"] == "retry_stage" for item in result["feedback"]), 1)

    def test_provider_cooldown_pauses_without_spending_another_attempt(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {"max_attempts": 2, "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def cooldown_then_complete(stage, **kwargs):
                calls.append(stage["id"])
                if len(calls) == 1:
                    raise ProviderCooldownError(
                        "provider reset pending", retry_after_seconds=0.03,
                        rate_limit={"kind": "daily_budget"},
                    )
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = cooldown_then_complete
            result = runner.run()
            self.assertEqual(result["status"], "paused")
            self.assertEqual(calls, ["survey"])
            self.assertEqual(result["stages"]["survey"]["status"], "paused")
            blocker = result["blockers"][0]
            self.assertEqual(blocker["reason"], "provider_cooldown")
            self.assertEqual(blocker["retry_after_seconds"], 0.03)
            self.assertEqual(result["interim_report"]["stop_reason"], "provider_cooldown")

    def test_autonomous_provider_cooldown_waits_and_retries_before_deadline(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {
                "mode": "until_deadline", "backoff_seconds": 0,
            }
            runner = ComposerRunner(workflow)
            calls = []

            def cooldown_then_complete(stage, **kwargs):
                calls.append(stage["id"])
                if calls.count("survey") == 1:
                    raise ProviderCooldownError(
                        "provider reset pending", retry_after_seconds=0.03,
                        rate_limit={"kind": "daily_budget"},
                    )
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = cooldown_then_complete
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["survey", "survey", "experiment"])
            self.assertEqual(result["stages"]["survey"]["attempt_count"], 2)
            self.assertTrue(any(
                item.get("action") == "provider_cooldown_auto_retry"
                for item in result["department_activity"]
            ))
            self.assertTrue(any(
                item.get("action") == "retry_stage"
                and item.get("delay_seconds", 0) >= 0.03
                for item in result["feedback"]
            ))
            self.assertFalse(any(
                item.get("reason") == "provider_cooldown"
                for item in result["blockers"]
            ))

    def test_process_interrupt_persists_a_resumable_pause(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def interrupted(stage, **kwargs):
                raise KeyboardInterrupt("run cancellation requested")

            runner._run_stage = interrupted
            result = runner.run()
            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["interim_report"]["stop_reason"], "process_interrupted")
            self.assertTrue(any(item.get("stop_reason") == "process_interrupted"
                                for item in result["blockers"]))
            progress = json.loads((root / "composer" / "output" / "progress.json").read_text())
            self.assertEqual(progress["status"], "paused")
            self.assertEqual(progress["phase"], "paused_process_interruption")

    def test_specialist_live_cards_survive_an_ordinary_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            stage = workflow["stages"][0]
            runner.stage_records[stage["id"]] = {
                "kind": stage["kind"], "status": "running",
                "assignment_task_ids": ["specialist-survey-cataloger"],
            }
            runner._checkpoint("survey:admitted", force=True)
            runner._specialist_progress(stage["id"], {
                "event": "dispatched", "role": "research.cataloger",
                "role_id": "cataloger", "task_id": "specialist-survey-cataloger",
                "stage_id": stage["id"], "execution_mode": "model",
                "model": "qwen", "status": "running",
            })
            runner._checkpoint("survey:running", force=True)
            progress = json.loads((root / "composer" / "output" / "progress.json").read_text())
            live = progress["stages"][stage["id"]]["specialist_live"]
            self.assertEqual(live["research.cataloger"]["task_id"],
                             "specialist-survey-cataloger")
            runner.close()

    def test_blocker_projection_separates_recovered_history_from_live_stop(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                runner.stage_records["survey"] = {"kind": "survey", "status": "running"}
                runner.blockers = [
                    {"stage_id": "survey", "reason": "old scientific failure",
                     "recovery": "cycle_admitted"},
                    {"stage_id": "survey", "reason": "forwarded finding",
                     "gating": False, "release_blocking": False},
                ]
                self.assertEqual(runner._active_blockers(), [])

                runner.stage_records["survey"]["status"] = "blocked"
                runner.blockers.append({"stage_id": "survey", "reason": "current stop"})
                active = runner._active_blockers()
                self.assertEqual([item["reason"] for item in active], ["current stop"])
                runner._checkpoint("survey:blocked", force=True)
                progress = json.loads(
                    (root / "composer" / "output" / "progress.json").read_text())
                self.assertEqual(progress["blocker_counts"], {"active": 1, "historical": 3})
                self.assertEqual(progress["active_blockers"][0]["reason"], "current stop")
                progress["usage"] = {"model_calls": 7, "input_tokens": 11,
                                     "output_tokens": 13, "openalex_requests": 17}
                line = json.loads(_composer_progress_line(progress))
                self.assertEqual(line["usage"]["model_calls"], 7)
                self.assertEqual(line["blockers"], 1)
                self.assertEqual(line["historical_blockers"], 3)
            finally:
                runner.close()

    def test_resume_uses_the_process_interruption_checkpoint_at_equal_frontier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.stage_records["survey"] = {
                "kind": "survey", "status": "running", "attempt_count": 1,
                "attempt_number": 1, "project_dir": workflow["stages"][0]["project_dir"],
            }
            runner._checkpoint("survey:running", force=True)
            runner.status = "paused"
            runner.blockers.append({
                "stage_id": "workflow", "reason": "KeyboardInterrupt: ",
                "stop_reason": "process_interrupted",
            })
            runner._checkpoint("paused_process_interruption", force=True)
            runner._finish()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(resumed.status, "running")
                self.assertEqual(resumed._progress_snapshot["phase"],
                                 "paused_process_interruption")
                self.assertEqual(resumed.stage_records["survey"]["status"], "running")
            finally:
                resumed.close()

    def test_quota_exhaustion_does_not_recreate_a_stage_budget(self):
        class FastClock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                self.value += 1.0
                return self.value

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 5, "reuse_completed": False,
                "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["retry_policy"] = {"mode": "until_deadline", "backoff_seconds": 0}
            runner = ComposerRunner(workflow, clock=FastClock())
            calls = []

            def quota_exhausted(stage, **kwargs):
                calls.append(stage["id"])
                raise QuotaExceededError(
                    "topic budget exhausted", dimension="model_calls", limit=1, observed=1,
                    usage={"model_calls": 1, "input_tokens": 11,
                           "output_tokens": 7, "openalex_requests": 2},
                    diagnostics=[{"kind": "model", "status": "error"}],
                )

            runner._run_stage = quota_exhausted
            result = runner.run()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(calls, ["topic"])
            self.assertEqual(result["stages"]["topic"]["status"], "blocked")
            self.assertEqual(result["interim_report"]["stop_reason"], "blocked")
            self.assertEqual(result["usage"], {
                "model_calls": 1, "input_tokens": 11,
                "output_tokens": 7, "openalex_requests": 2,
            })
            self.assertEqual(result["stages"]["topic"]["attempts"][0]["usage"]["model_calls"], 1)

    def test_scientific_topic_intake_failure_pivots_until_deadline(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"] = [{
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            workflow["retry_policy"] = {"mode": "until_deadline", "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def pivot_then_complete(stage, **kwargs):
                calls.append(stage["project_dir"])
                if len(calls) == 1:
                    error = QuotaExceededError(
                        "topic discovery bounded intake exhausted",
                        dimension="topic_attempts", limit=6, observed=6,
                        usage={"model_calls": 4, "input_tokens": 40,
                               "output_tokens": 20, "openalex_requests": 3},
                        diagnostics=[],
                    )
                    error.retryable_topic_intake = True
                    error.topic_retry_reason = "scientific_candidate_rejected"
                    error.rejected_topic_history = [{
                        "topic_id": "rejected-direction",
                        "title": "A weak direction",
                        "domain": "test",
                        "research_question": "Does A change B?",
                    }]
                    raise error
                output = root / "topic-result.json"
                output.write_text(json.dumps({"status": "completed"}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = pivot_then_complete
            runner._run_specialist_pool = lambda *args, **kwargs: {
                "reports": [], "by_role": {}, "usage": {}, "model_enabled": False,
            }
            runner._publish_specialist_reports = lambda stage, assignment, bundle: bundle
            runner._run_specialist_verifier = lambda *args, **kwargs: None
            result = runner.run()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[1].endswith("attempts/attempt-2"))
            self.assertTrue(any(
                item.get("action") == "pivot_topic_direction"
                for item in result["department_activity"]
            ))
            self.assertTrue(any(
                item.get("retry_reason") == "scientific_candidate_rejected"
                for item in result["feedback"] if item.get("action") == "retry_stage"
            ))

    def test_unbudgeted_topic_contract_failure_keeps_typed_retry_metadata(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            model_path = root / "model.json"
            model_path.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            runner = ComposerRunner(workflow)
            error = ValidationError("topic candidate has an invalid shape")
            error.topic_intake_recoverable = True
            error.topic_retry_reason = "intake_contract_failure"
            error.candidate_attempt_trace = [{
                "status": "rejected", "error": str(error),
            }]
            try:
                with patch("scisaurus.runtime.topic_discovery.validate_topic_stage_config") as validate, \
                        patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as topic_runner:
                    validate.return_value = {
                        "model_config_path": str(model_path.resolve()),
                        "output_path": str((root / "topic-output.json").resolve()),
                        "candidate_count": 3, "max_attempts": 1,
                        "schema_version": "topic-discovery-config-1",
                    }
                    topic_runner.return_value.run.side_effect = error
                    with self.assertRaises(ValidationError) as raised:
                        runner._run_stage(stage)
                self.assertIs(raised.exception, error)
                self.assertTrue(raised.exception.retryable_topic_intake)
                self.assertEqual(raised.exception.topic_retry_reason,
                                 "intake_contract_failure")
                self.assertTrue(runner._is_topic_intake_retry(raised.exception, stage))
            finally:
                runner.close()

    def test_raw_topic_feasibility_boundary_failure_becomes_scientific_retry(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            runner = ComposerRunner(workflow)
            error = ValidationError(
                "feasibility_plan.estimated_compute_seconds must be an integer between 1 and 604800")
            try:
                runner._execute_stage = lambda *args, **kwargs: (_ for _ in ()).throw(error)
                with self.assertRaises(ValidationError) as raised:
                    runner._run_stage(stage)
                caught = raised.exception
                self.assertTrue(caught.retryable_topic_intake)
                self.assertTrue(caught.topic_intake_recoverable)
                self.assertEqual(caught.topic_retry_reason,
                                 "scientific_candidate_rejected")
                self.assertTrue(runner._is_topic_intake_retry(caught, stage))
            finally:
                runner.close()

    def test_topic_budget_is_cumulative_across_isolated_attempts(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.stage_records["topic"] = {"attempts": [{
                "state": "failed",
                "usage": {"model_calls": 3, "input_tokens": 100,
                           "output_tokens": 25, "openalex_requests": 2},
            }]}
            remaining = runner._topic_budgets_for_attempt("topic", {
                "max_model_calls": 8,
                "max_openalex_requests": 5,
                "max_input_tokens": 200,
                "max_output_tokens": 40,
            })
            self.assertEqual(remaining, {
                "max_model_calls": 5,
                "max_openalex_requests": 3,
                "max_input_tokens": 100,
                "max_output_tokens": 15,
            })
            runner.close()

    def test_topic_continuation_budget_isolated_per_admitted_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 2
            runner.stage_records["topic"] = {"attempts": [
                {"state": "failed", "cycle": 0,
                 "usage": {"model_calls": 8, "input_tokens": 100,
                            "output_tokens": 25, "openalex_requests": 2}},
                {"state": "succeeded", "cycle": 1,
                 "topic_usage": {"model_calls": 3, "input_tokens": 40,
                                  "output_tokens": 10, "openalex_requests": 1},
                 # Specialist usage belongs to the stage ledger but not the
                 # topic runner's continuation envelope.
                 "usage": {"model_calls": 7, "input_tokens": 500,
                            "output_tokens": 50, "openalex_requests": 1}},
                {"state": "failed", "cycle": 2,
                 "topic_usage": {"model_calls": 2, "input_tokens": 20,
                                  "output_tokens": 5, "openalex_requests": 1}},
            ]}
            initial = runner._topic_budgets_for_attempt(
                "topic", {"max_model_calls": 10}, scope="intake")
            continuation = runner._topic_budgets_for_attempt(
                "topic", {"max_model_calls": 6}, scope="continuation")
            self.assertEqual(initial["max_model_calls"], 2)
            self.assertEqual(continuation["max_model_calls"], 4)
            runner.close()

    def test_reopened_stage_quota_isolated_per_admitted_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"survey"}
            runner.stage_records["survey"] = {
                "kind": "survey",
                # This is the prior cycle snapshot. It must not consume the
                # fresh quota envelope before the reopened cycle dispatches.
                "usage": {"model_calls": 80, "input_tokens": 100,
                           "output_tokens": 20, "openalex_requests": 2},
                "attempts": [
                    {"state": "failed", "cycle": 0,
                     "usage": {"model_calls": 80, "input_tokens": 100,
                                "output_tokens": 20, "openalex_requests": 2}},
                    {"state": "succeeded", "cycle": 1,
                     "usage": {"model_calls": 12, "input_tokens": 40,
                                "output_tokens": 10, "openalex_requests": 1}},
                ],
            }
            survey = next(item for item in workflow["stages"] if item["id"] == "survey")
            survey["quota"] = {
                "max_model_calls": 24,
                "max_input_tokens": 1000,
                "max_output_tokens": 500,
                "max_openalex_requests": 12,
            }
            self.assertEqual(runner._stage_usage("survey"), {
                "model_calls": 12, "input_tokens": 40,
                "output_tokens": 10, "openalex_requests": 1,
            })
            self.assertIsNone(runner._stage_quota_error(survey))
            runner.close()

    def test_stage_quota_exhaustion_admits_narrow_scoped_recovery(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.context["survey"] = {
                    "kind": "survey", "status": "completed",
                    "survey_current": True,
                }
                by_id = {item["id"]: item for item in workflow["stages"]}
                error = QuotaExceededError(
                    "stage survey quota exhausted: model_calls=97 > 96",
                    dimension="max_model_calls", limit=96, observed=97,
                    usage={"model_calls": 97},
                )
                self.assertTrue(runner._admit_stage_quota_recovery(
                    by_id["survey"], error, set(), by_id))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertEqual(
                    runner.context["survey"]["quota_recovery"]["mode"],
                    "narrow_scope",
                )
                request = next(
                    item for item in runner.active_research_requests
                    if item.get("kind") == "literature_expansion")
                self.assertIn("narrower decisive gate", request["objective"])
                self.assertTrue(any(
                    item.get("action") == "stage_quota_recovery_admitted"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_openalex_cooldown_admits_bounded_crossref_fallback(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            descriptor = root / "survey-descriptor.json"
            descriptor.write_text(json.dumps({
                "survey": {
                    "bibliography_fallback": "disabled",
                    "identity": {"adapter": "crossref"},
                }
            }))
            workflow["stages"][0]["config_path"] = str(descriptor.resolve())
            runner = ComposerRunner(workflow)
            try:
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "paused",
                    "error": "ProviderCooldownError: OpenAlex survey retrieval is paused until the provider quota resets",
                }
                runner.retry_schedule["survey"] = {
                    "error": "ProviderCooldownError: OpenAlex daily quota",
                    "not_before_epoch": time.time() + 86400,
                }
                self.assertTrue(runner._resume_survey_provider_fallback({
                    item["id"]: item for item in workflow["stages"]
                }))
                self.assertNotIn("survey", runner.retry_schedule)
                self.assertEqual(
                    runner.context["survey"]["provider_fallback"]["mode"],
                    "crossref_metadata",
                )
                config = {
                    "project_id": str(root / "survey"),
                    "survey": {
                        "revision": 5,
                        "bibliography_fallback": "disabled",
                        "seed_work_ids": [],
                        "search": {
                            "max_works": 120, "max_analyzed_works": 20,
                            "challenge_reserve": 5, "queries_per_role": 3,
                            "results_per_query": 10, "max_api_calls": 900,
                            "max_full_texts": 40, "expansion_rounds": 3,
                            "expansion_seed_count": 3, "saturation_rounds": 3,
                        },
                    },
                }
                with patch.object(runner, "_augment_full_text_routes"):
                    adapted = runner._adapt_continuation_config(
                        workflow["stages"][0], config)
                self.assertEqual(
                    adapted["survey"]["bibliography_fallback"], "crossref_metadata")
                self.assertEqual(adapted["survey"]["search"]["max_works"], 20)
                self.assertEqual(adapted["survey"]["search"]["expansion_rounds"], 0)
                self.assertEqual(adapted["survey"]["search"]["max_api_calls"], 32)
            finally:
                runner.close()

    def test_stage_work_order_projection_does_not_cross_contaminate_reopened_stages(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            runner.active_research_requests = [
                {"id": "survey-repair", "source_stage_id": "survey",
                 "kind": "literature_expansion", "objective": "Refresh the literature map."},
                {"id": "experiment-repair", "source_stage_id": "experiment",
                 "kind": "additional_experiment", "objective": "Run a control."},
            ]
            runner.reopened_stage_ids = {"survey", "experiment"}
            self.assertEqual(
                [item["id"] for item in runner._requests_for_stage("survey")],
                ["survey-repair"],
            )
            self.assertEqual(
                [item["id"] for item in runner._requests_for_stage("experiment")],
                ["experiment-repair"],
            )
            runner.close()

    def test_topic_refinement_supersedes_downstream_work_orders(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            runner.context = {
                "topic": {
                    "kind": "topic_discovery", "status": "research_expansion_required",
                    "research_expansion_requests": [{
                        "id": "topic-repair", "kind": "topic_refinement",
                        "owner": "research.intelligence", "objective": "Change direction.",
                        "why": "The old direction was not supported.",
                        "success_condition": "A new admitted topic.",
                        "evidence_needed": "Source-grounded feasibility.",
                    }],
                },
                "experiment": {
                    "kind": "experiment", "status": "research_expansion_required",
                    "research_expansion_requests": [{
                        "id": "stale-experiment-repair", "kind": "additional_experiment",
                        "owner": "methods.validation", "objective": "Repair the old experiment.",
                        "why": "The previous frontier failed.",
                        "success_condition": "A valid result.",
                        "evidence_needed": "Raw output.",
                    }],
                },
            }
            requests = runner._continuation_requests()
            self.assertEqual([item["id"] for item in requests], ["topic-repair"])
            runner.close()

    def test_restored_requests_are_fenced_to_the_new_topic_frontier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "completed",
                "topic": {"id": "new-topic"},
                "topic_evolution": {"mode": "refinement", "cycle": 3},
            }
            runner.stage_records["topic"] = {"status": "completed"}
            requests = [
                {"id": "legacy", "source_stage_id": "experiment"},
                {"id": "current", "source_stage_id": "experiment",
                 "topic_id": "new-topic", "topic_cycle": 3},
            ]
            self.assertEqual(
                [item["id"] for item in runner._scope_active_research_requests(requests)],
                ["current"],
            )
            runner.close()

    def test_terminal_topic_failure_closes_active_revalidation_order(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            request = {
                "id": "topic-runtime-feasibility-revalidation",
                "kind": "topic_refinement",
                "owner": "research.intelligence",
                "objective": "Replan the selected direction.",
                "why": "The checkpoint predates the feasibility contract.",
                "success_condition": "A valid executable plan is admitted.",
                "evidence_needed": "Runtime inventory and bounded experiment plan.",
            }
            runner.active_research_requests = [request]
            active = runner.departments.activate_work_orders(runner.active_research_requests)
            self.assertEqual(active[0]["task_state"], "running")
            runner._resolve_terminal_stage_work_orders({"id": "topic", "kind": "topic_discovery"})
            task = runner.tasks.get(active[0]["task_id"])
            self.assertEqual(task["state"], "blocked")
            runner.close()

    def test_topic_budget_admission_snapshot_is_not_charged_twice(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            error = QuotaExceededError(
                "topic discovery mission quota exhausted",
                dimension="model_calls", limit=8, observed=8,
                usage={}, diagnostics=[{"kind": "composer_topic_budget"}],
            )
            error.usage_is_snapshot = True
            self.assertEqual(
                runner._record_failed_stage_usage(error),
                {"model_calls": 0, "input_tokens": 0,
                 "output_tokens": 0, "openalex_requests": 0})
            runner.close()

    def test_local_topic_budget_exhaustion_opens_a_fresh_continuation_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            workflow["stages"] = [{
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "status": "research_expansion_required",
                "topic": {"id": "old", "title": "Old", "research_question": "Old?"},
                "research_expansion_requests": [],
            }
            error = QuotaExceededError(
                "topic discovery quota exhausted: model_calls=10, limit=10",
                dimension="model_calls", limit=10, observed=10,
                usage={}, diagnostics=[{"kind": "composer_topic_budget"}],
            )
            error.topic_budget_scope = "continuation"
            self.assertTrue(runner._admit_scientific_blocker_recovery(
                workflow["stages"][0], error, set(), {"topic": workflow["stages"][0]}))
            self.assertEqual(runner.continuation_cycles, 2)
            self.assertIn("topic", runner.reopened_stage_ids)
            self.assertTrue(any(
                item.get("action") == "pivot_topic_after_budget_exhaustion"
                for item in runner.department_activity
            ))
            runner.close()

    def test_until_deadline_retry_mode_does_not_stop_at_attempt_counter(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {
                "mode": "until_deadline", "max_attempts": 1, "backoff_seconds": 0,
            }
            runner = ComposerRunner(workflow)
            calls = []

            def recovers_after_four_attempts(stage, **kwargs):
                calls.append(stage["project_dir"])
                if len(calls) < 4:
                    raise RuntimeError("transient validation failure")
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"], "attempt": len(calls)}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = recovers_after_four_attempts
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(calls), 5)  # four survey attempts, then experiment
            self.assertEqual(calls[3], calls[0])
            self.assertEqual(result["retry_policy"]["mode"], "until_deadline")
            self.assertTrue(all(item.get("retry_mode") == "until_deadline"
                                for item in result["feedback"] if item["action"] == "retry_stage"))

    def test_until_deadline_dispatches_a_residual_stage_window(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["stages"][1]["estimate_seconds"] = 20
            workflow["retry_policy"] = {
                "mode": "until_deadline", "backoff_seconds": 0,
            }
            runner = ComposerRunner(workflow)
            calls = []

            def residual_stage(stage, **kwargs):
                calls.append(stage["id"])
                if stage["id"] == "survey":
                    # Leave less time than the experiment forecast while
                    # retaining a safe control-plane dispatch margin.
                    runner.deadline = runner.clock() + 1.5
                    runner.deadline_epoch = time.time() + 1.5
                output = root / f"{stage['id']}-residual.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = residual_stage
            result = runner.run()
            self.assertEqual(calls, ["survey", "experiment"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["deadline_decisions"][0]["admission"], "residual_window")

    def test_stage_return_after_hard_wall_cannot_be_reported_as_completed(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def late_stage(stage, **kwargs):
                output = root / f"{stage['id']}-late.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                if stage["id"] == "experiment":
                    runner.deadline = runner.clock() - 1
                    runner.deadline_epoch = time.time() - 1
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = late_stage
            result = runner.run()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["interim_report"]["stop_reason"], "hard_deadline")
            self.assertTrue(any("deadline" in item.get("reason", "")
                                for item in result["blockers"]))

    def test_bounded_mode_pauses_when_the_required_stage_window_does_not_fit(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["stages"][1]["estimate_seconds"] = 20
            workflow["retry_policy"] = {
                "mode": "bounded", "max_attempts": 1, "backoff_seconds": 0,
            }
            runner = ComposerRunner(workflow)
            calls = []

            def strict_stage(stage, **kwargs):
                calls.append(stage["id"])
                if stage["id"] == "survey":
                    runner.deadline = runner.clock() + 1.5
                    runner.deadline_epoch = time.time() + 1.5
                output = root / f"{stage['id']}-strict.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = strict_stage
            result = runner.run()
            self.assertEqual(calls, ["survey"])
            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["interim_report"]["stop_reason"],
                             "required_stage_window_does_not_fit_remaining_deadline")
            self.assertEqual(result["deadline_decisions"][0]["admission"], "deferred")

    def test_ten_hour_composer_hard_wall_is_valid(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            workflow["time_policy"] = {"first_result_seconds": 600,
                                        "target_seconds": 18000,
                                        "hard_seconds": 36000,
                                        "checkpoint_seconds": 30}
            self.assertEqual(validate_workflow(workflow)["time_policy"]["hard_seconds"], 36000)

    def test_omitted_retry_policy_is_deadline_governed(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            runner = ComposerRunner(workflow)
            self.assertEqual(runner._retry_policy()["mode"], "until_deadline")
            self.assertIsNone(runner._retry_policy()["max_attempts"])
            runner.close()

    def test_topic_history_path_requires_an_absolute_path(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            workflow["topic_history_path"] = "relative-topic-history.json"
            with self.assertRaisesRegex(ValidationError, "absolute"):
                validate_workflow(workflow)

    def test_foundry_backed_topic_hides_templates_and_materializes_admitted_program(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            model_path = root / "model.json"
            model_path.write_text(json.dumps({
                "protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
                "model": "stub", "timeout_seconds": 60, "max_output_tokens": 128,
            }))
            requirements = root / "requirements.txt"
            requirements.write_text("numpy==2.5.2\n")
            descriptor_path = root / "generated-capability.json"
            descriptor_path.write_text(json.dumps({
                "schema_version": "experiment-capability-1", "capability_id": "generated_frontier",
                "experiment": {
                    "id": "generated_frontier", "revision": 1, "study_type": "exploratory",
                    "domain": "marine ecology", "research_question": "Does transport alter patch recovery?",
                    "hypothesis": "Transport alters recovery.", "method": "Run a seeded comparison.",
                    "parameters": {}, "seed": 3, "run_count": 5,
                    "stopping_rule": "Run five replicates.", "primary_outcomes": [],
                    "limitations": ["Synthetic boundary."], "literature_gate": None,
                    "execution": {}, "validation": {}, "required_assets": [], "reviewers": [],
                    "stage_seconds": {}, "max_observations": 5, "max_asset_bytes": 100,
                },
            }))
            foundry_config = root / "foundry.json"
            foundry_config.write_text(json.dumps({
                "schema_version": "capability-foundry-config-1",
                "model_config_path": str(model_path.resolve()),
                "runtime_python": str(Path(sys.executable).resolve()),
                "workspace_root": str((root / "foundry-workspace").resolve()),
                "registry_root": str((root / "registry").resolve()),
                "repo_root": str(root.resolve()),
                "requirements_file": str(requirements.resolve()),
                "runtime_packages": [{"name": "numpy", "version": "2.5.2"}],
                "max_attempts": 2, "timeout_seconds": 30,
            }))
            workflow["capability_foundry_config_path"] = str(foundry_config.resolve())
            workflow["experiment_catalog"] = [{
                "id": "fallback", "config_path": str(descriptor_path.resolve())}]
            validate_workflow(workflow)
            runner = ComposerRunner(workflow)
            runtime_context = runner._runtime_context(json.loads(model_path.read_text()))
            self.assertEqual(runtime_context["experiment_catalog"], [])
            self.assertEqual(len(runtime_context["fallback_experiment_catalog"]), 1)
            self.assertTrue(runtime_context["capability_foundry"]["enabled"])
            self.assertTrue(runtime_context["python_packages"]["numpy"])
            self.assertEqual(
                runtime_context["capability_foundry"]["runtime_packages"],
                [{"name": "numpy", "version": "2.5.2"}],
            )
            self.assertEqual(
                runtime_context["capability_foundry"]["allowed_evidence_modes"],
                ["analytical_derivation", "synthetic_simulation"],
            )
            self.assertEqual(runtime_context["capability_foundry"]["timeout_seconds"], 30)
            self.assertEqual(runtime_context["research_feasibility"]["max_model_calls"], 0)
            self.assertEqual(runtime_context["research_feasibility"]["max_external_requests"], 0)
            self.assertEqual(runtime_context["research_feasibility"]["max_experiment_seconds"], 10)
            result = {
                "status": "completed",
                "topic": {"id": "frontier", "title": "Patch recovery", "domain": "marine ecology",
                          "research_question": "Does transport alter patch recovery?", "scope": "Synthetic patches",
                          "disconfirmation_test": "No recovery difference.", "resource_plan": "Seeded simulation."},
                "candidates": [{"id": "frontier"}], "candidate_prior_work": [],
                "source_challenge": {"decision": "admit_to_survey"},
            }
            generated = {
                "status": "registered", "attempts": 1,
                "registration": {"capability_id": "generated_frontier",
                                 "descriptor_path": str(descriptor_path.resolve())},
                "admission": {"gates": ["static_scan", "independent_recalculation"]},
            }
            with patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate",
                       return_value=generated) as call:
                result = runner._materialize_topic_capability(result)
            self.assertEqual(result["topic"]["experiment_capability_id"], "generated_frontier")
            self.assertEqual(call.call_args.kwargs["required_intent"]["research_question"],
                             "Does transport alter patch recovery?")
            runner.context["topic"] = {"kind": "topic_discovery", **result}
            config = {"experiment": {"revision": 1, "literature_gate": {"required_state": "eligible_for_experiment"}},
                      "supplied_context": "base"}
            with patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate", return_value=generated):
                projected = runner._apply_topic_to_experiment_config(workflow["stages"][1], config)
            self.assertEqual(projected["experiment"]["id"], "generated_frontier")
            entry = {"id": "generated_frontier", "revision": 1,
                     "path": str(descriptor_path), "candidate_record_sha256": "a" * 64}
            admission_path = root / "admission.json"
            admission_path.write_text(json.dumps({"adversarial_review": {"status": "admitted", "findings": []}}))
            with patch("scisaurus.runtime.capability_registry.load_registry", return_value={"capabilities": [entry]}), \
                    patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate", return_value=generated) as upgrade:
                runner._materialize_topic_capability(result)
            self.assertEqual(upgrade.call_args.kwargs["required_intent"]["revision"], 2)
            from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests
            valid_review = {**CapabilityFoundryTests._review_payload(), "role": "review.methods",
                "review_method": "independent_model", "candidate_sha256": "a" * 64}
            for verdict in ({**valid_review, "status": "rejected"},
                            {name: value for name, value in valid_review.items() if name != "status"}):
                admission_path.write_text(json.dumps({"adversarial_review": verdict}))
                with patch("scisaurus.runtime.capability_registry.load_registry", return_value={"capabilities": [entry]}), \
                        patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate", return_value=generated) as upgrade:
                    runner._materialize_topic_capability(result)
                upgrade.assert_called_once()
            admission_path.write_text(json.dumps({"adversarial_review": valid_review}))
            with patch("scisaurus.runtime.capability_registry.load_registry", return_value={"capabilities": [entry]}), \
                    patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate") as regenerate_existing:
                checked = runner._materialize_topic_capability(result)
            regenerate_existing.assert_not_called()
            self.assertTrue(checked["generated_capability"]["reused"])
            lazy_result = json.loads(json.dumps(result))
            lazy_result.pop("generated_capability")
            lazy_result["topic"].pop("experiment_capability_id", None)
            runner.context["topic"] = {"kind": "topic_discovery", **lazy_result}
            with patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate",
                       return_value=generated), \
                    patch.object(runner, "_materialize_topic_capability",
                                 wraps=runner._materialize_topic_capability) as materialize:
                runner._apply_topic_to_experiment_config(workflow["stages"][1], {
                    "experiment": {"revision": 1,
                                   "literature_gate": {"required_state": "eligible_for_experiment"}},
                    "supplied_context": "base"})
            materialize.assert_called_once()
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"experiment"}
            runner.active_research_requests = [{
                "id": "run-control", "kind": "additional_experiment", "owner": "methods.validation",
                "objective": "Run a control that separates the mechanisms.",
                "why": "The first result left both explanations viable.",
                "success_condition": "The new result changes the mechanism decision.",
                "evidence_needed": "Raw observations and independent recalculation.",
            }]
            runner.context["topic"] = {"kind": "topic_discovery", "topic": result["topic"],
                                         "generated_capability": generated}
            # A fresh capability is warranted only after the prior capability
            # produced an observed result that the scoped work order is meant
            # to extend. Before first execution, the admitted capability is
            # reused so authoring failures cannot starve the actual experiment.
            runner.context["experiment"] = {
                "kind": "experiment", "status": "research_expansion_required",
                "results_package": {"schema_version": "results-package-1"},
            }
            regenerated = {
                **generated,
                "registration": {
                    "capability_id": "frontier-cycle-1",
                    "descriptor_path": str(descriptor_path.resolve()),
                },
            }
            with patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate",
                       return_value=regenerated) as regenerate:
                runner._apply_topic_to_experiment_config(workflow["stages"][1], {
                    "experiment": {"revision": 2,
                                   "literature_gate": {"required_state": "eligible_for_experiment"}},
                    "supplied_context": "base"})
            self.assertEqual(regenerate.call_args.kwargs["required_intent"]["id"], "frontier-cycle-1")
            self.assertEqual(regenerate.call_args.kwargs["required_intent"]["revision"], 2)
            runner.close()

    def test_topic_history_is_append_only_and_rotates_recent_capability(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            history_path = root / "shared" / "topic-history.json"
            capability_a = root / "cap-a.json"
            capability_b = root / "cap-b.json"
            for capability in (capability_a, capability_b):
                capability.write_text(json.dumps({"schema_version": "experiment-capability-1",
                                                   "capability_id": capability.stem,
                                                   "experiment": {}}))
            workflow["experiment_catalog"] = [
                {"id": "cap_a", "config_path": str(capability_a.resolve())},
                {"id": "cap_b", "config_path": str(capability_b.resolve())},
            ]
            workflow["topic_history_path"] = str(history_path.resolve())
            runner = ComposerRunner(workflow)
            topic = {"id": "chosen_topic_a", "title": "Direction A", "domain": "science",
                     "research_question": "Does mechanism A change the measured outcome?"}
            runner._record_topic_history({"topic": {**topic, "experiment_capability_id": "cap_a"}})
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            self.assertEqual(resumed.topic_history["entries"][0]["topic_id"], "chosen_topic_a")
            self.assertEqual(resumed._effective_topic_exclusions()["capability_ids"], ["cap_a"])
            self.assertIn("chosen_topic_a", resumed._effective_topic_exclusions()["topic_ids"])
            resumed.close()

    def test_legacy_topic_history_migration_separates_scientific_rejections(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            history_path = root / "shared" / "topic-history.json"
            workflow["topic_history_path"] = str(history_path.resolve())
            history_path.parent.mkdir(parents=True)
            history_path.write_text(json.dumps({
                "schema_version": "topic-history-1",
                "scopes": {"legacy": {"entries": [
                    {"topic_id": "old-novelty", "rejection_type": "intake_validation",
                     "rejection_reason": "selected topic is too similar to a previously attempted direction"},
                    {"topic_id": "old-maturity", "rejection_type": "intake_validation",
                     "rejection_reason": "topic maturity review requires substantive refinement: the comparison is too thin"},
                    {"topic_id": "old-contract", "rejection_type": "intake_validation",
                     "rejection_reason": "topic candidate has an invalid shape (missing=['search_queries'], unexpected=[])"},
                ]}},
            }))

            runner = ComposerRunner(workflow)
            try:
                document = json.loads(history_path.read_text())
                entries = [entry for scope in document["scopes"].values()
                           for entry in scope["entries"]]
                by_id = {entry["topic_id"]: entry for entry in entries}
                self.assertEqual(by_id["old-novelty"]["rejection_type"], "novelty")
                self.assertEqual(by_id["old-maturity"]["rejection_type"], "maturity")
                self.assertNotIn("old-contract", by_id)
                self.assertNotIn(
                    "intake_validation",
                    {entry.get("rejection_type") for entry in entries})
                self.assertEqual(
                    {entry["topic_id"] for entry in runner.topic_history["entries"]},
                    {"old-novelty", "old-maturity"})
            finally:
                runner.close()

    def test_generated_slot_history_id_does_not_become_exact_exclusion(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            history_path = root / "shared" / "topic-history.json"
            workflow["topic_history_path"] = str(history_path.resolve())
            workflow["topic_exclusions"] = {
                "capability_ids": [], "topic_ids": ["explicit_topic_id"]}
            runner = ComposerRunner(workflow)
            runner._record_topic_history({"topic": {
                "id": "direction_qft_topology_scaling",
                "title": "A generated slot label",
                "domain": "quantum science",
                "research_question": "Does a changed observable distinguish two mechanisms?",
            }})
            exclusions = runner._effective_topic_exclusions()
            self.assertNotIn("direction_qft_topology_scaling", exclusions["topic_ids"])
            self.assertIn("explicit_topic_id", exclusions["topic_ids"])
            runner.close()

    def test_rejected_topic_history_persists_across_fresh_missions(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            history_path = root / "shared" / "topic-history.json"
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir(); second_root.mkdir()
            first_workflow = self._workflow(first_root)
            first_workflow["topic_history_path"] = str(history_path.resolve())
            first = ComposerRunner(first_workflow)
            first._record_topic_rejection_history([{
                "topic_id": "rejected_direction",
                "title": "Rejected direction",
                "domain": "ecology",
                "research_question": "Does dispersal change recovery after disturbance?",
                "rejection_type": "maturity",
            }])
            first.close()

            second_workflow = self._workflow(second_root)
            second_workflow["topic_history_path"] = str(history_path.resolve())
            second = ComposerRunner(second_workflow)
            self.assertEqual(
                [item["topic_id"] for item in second.topic_history["entries"]],
                ["rejected_direction"],
            )
            self.assertEqual(second.topic_history["entries"][0]["history_status"], "rejected")
            self.assertIn(
                "rejected_direction", second._effective_topic_exclusions()["topic_ids"])
            second.close()

    def test_explicit_topic_history_survives_objective_wording_changes(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            history_path = root / "shared-topic-history.json"
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir(); second_root.mkdir()
            first_workflow = self._workflow(first_root)
            first_workflow["topic_history_path"] = str(history_path.resolve())
            first = ComposerRunner(first_workflow)
            first._record_topic_history({"topic": {
                "id": "prior_direction", "title": "Prior direction",
                "domain": "ecology",
                "research_question": "Does dispersal change recovery after disturbance?",
            }})
            first.close()

            second_workflow = self._workflow(second_root)
            second_workflow["objective"] = "Explore a newly worded scientific frontier"
            second_workflow["topic_history_path"] = str(history_path.resolve())
            second = ComposerRunner(second_workflow)
            self.assertEqual(
                [item["topic_id"] for item in second.topic_history["entries"]],
                ["prior_direction"],
            )
            self.assertIn("prior_direction", second._effective_topic_exclusions()["topic_ids"])
            second.close()

    def test_default_family_history_survives_objective_wording_changes(self):
        with tempfile.TemporaryDirectory() as path:
            family = Path(path)
            first_root = family / "run-1"
            second_root = family / "run-2"
            first_root.mkdir(); second_root.mkdir()
            first_workflow = self._workflow(first_root)
            first = ComposerRunner(first_workflow)
            first._record_topic_history({"topic": {
                "id": "prior_default_direction", "title": "Prior default direction",
                "domain": "ecology",
                "research_question": "Does dispersal change recovery after disturbance?",
            }})
            first.close()

            second_workflow = self._workflow(second_root)
            second_workflow["objective"] = "Explore a reworded frontier under the same mission"
            second = ComposerRunner(second_workflow)
            self.assertEqual(first.topic_history_path, second.topic_history_path)
            self.assertEqual(
                [item["topic_id"] for item in second.topic_history["entries"]],
                ["prior_default_direction"],
            )
            second.close()

    def test_exploration_seed_is_random_once_and_persisted_for_resume(self):
        with tempfile.TemporaryDirectory() as path, patch(
                "scisaurus.runtime.composer.secrets.randbits", return_value=123456):
            workflow = self._workflow(Path(path))
            runner = ComposerRunner(workflow)
            self.assertEqual(runner.exploration_seed, 123456)
            self.assertEqual(runner._topic_sampling_seed(), runner._topic_sampling_seed())
            self.assertNotEqual(
                runner._topic_sampling_seed(attempt_number=1),
                runner._topic_sampling_seed(attempt_number=2),
            )
            runner._checkpoint("seed-persisted", force=True)
            progress = json.loads((Path(workflow["project_id"]) / "output" / "progress.json").read_text())
            self.assertEqual(progress["status"], "running")
            self.assertEqual(progress["exploration_seed"], 123456)
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)
            self.assertEqual(resumed.exploration_seed, 123456)
            resumed.close()

    def test_free_topic_selection_projects_question_and_queries_without_manual_bindings(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str((root / "topic").resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            (root / "topic").mkdir()
            # The workflow's topic descriptor is not needed for this projection
            # test; the context is the same packet a completed topic stage emits.
            runner = ComposerRunner(workflow)
            # Catalog-backed free-topic runs must not promote the broad
            # discovery sampler's records to evidence seeds for the selected
            # question.
            runner.workflow["experiment_catalog"] = [{"id": "capability"}]
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "topic": {"research_question": "Does mechanism change the measured outcome?",
                          "search_queries": ["mechanism comparison", "controlled experiment", "public data"],
                          "recent_papers": [{"work_id": "W123456789"}]},
            }
            config = {"survey": {"question": "placeholder", "seed_queries": ["old query"],
                                  "seed_work_ids": ["W999"]}}
            projected = runner._apply_topic_to_survey_config(workflow["stages"][1], config)
            self.assertEqual(projected["survey"]["question"], "Does mechanism change the measured outcome?")
            self.assertEqual(projected["survey"]["seed_queries"],
                             ["mechanism comparison", "controlled experiment", "public data"])
            self.assertEqual(projected["survey"]["seed_work_ids"], [])
            self.assertEqual(projected["survey"]["bibliography_fallback"], "disabled")
            runner.close()

    def test_topic_query_projection_adds_exact_hyphenated_concept(self):
        queries = ComposerRunner._topic_search_queries({
            "title": "Contamination tail benefit of median-of-means",
            "research_question": "How does median-of-means behave under contamination?",
            "search_queries": [
                "median of means estimator contamination",
                "median of means finite sample comparison",
            ],
        })
        self.assertEqual(queries, [
            "median of means estimator contamination",
            "median of means finite sample comparison",
        ])

    def test_survey_activates_only_its_search_preflight_specialist(self):
        self.assertEqual(
            ComposerRunner._active_stage_role_ids({"kind": "survey"}),
            ["search-strategist"],
        )
        self.assertIsNone(
            ComposerRunner._active_stage_role_ids({"kind": "experiment"})
        )

    def test_free_topic_literature_hold_requests_question_refinement(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "topic": {"id": "direction_a", "title": "A direction",
                          "domain": "science",
                          "research_question": "Does mechanism A change the measured outcome?"},
            }
            gated = runner._gate_free_topic_survey({
                "status": "completed", "gap_state": "insufficient_evidence",
                "survey_ref": "survey-ref", "assessment_ref": "assessment-ref",
            })
            self.assertEqual(gated["status"], "research_expansion_required")
            self.assertEqual({item["kind"] for item in gated["research_expansion_requests"]},
                             {"literature_expansion"})
            self.assertEqual(gated["topic_admission"], "expand_literature_before_refine")
            # A second insufficient assessment after the scoped expansion is
            # the point at which the question is sent back for redesign.
            runner.context["survey"] = gated
            refined_hold = runner._gate_free_topic_survey({
                "status": "completed", "gap_state": "insufficient_evidence",
                "survey_ref": "survey-ref-2", "assessment_ref": "assessment-ref-2",
            })
            self.assertEqual({item["kind"] for item in refined_hold["research_expansion_requests"]},
                             {"literature_expansion", "topic_refinement"})
            self.assertEqual(refined_hold["topic_admission"], "refine_before_experiment")
            runner.active_research_requests = [
                {**item, "source_stage_id": "survey"}
                for item in refined_hold["research_expansion_requests"]
            ]
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"topic", "survey"}
            context = runner._topic_refinement_context(workflow["stages"][0])
            self.assertEqual(context["parent_topic_id"], "direction_a")
            self.assertEqual(context["mode"], "refinement")
            self.assertEqual(context["salvage_plan"]["mode"], "salvage")
            self.assertEqual(
                context["salvage_plan"]["active_branch"]["id"],
                "mechanism-observable",
            )
            # A completed salvage branch is carried in the immutable topic
            # lineage so the next continuation selects the next repair axis.
            runner.context["topic"]["topic_evolution"] = {
                "mode": "refinement",
                "salvage": {
                    "mode": "salvage",
                    "attempted_branch_ids": ["mechanism-observable"],
                },
            }
            next_context = runner._topic_refinement_context(workflow["stages"][0])
            self.assertEqual(
                next_context["salvage_plan"]["active_branch"]["id"],
                "comparison-baseline",
            )
            runner.context["topic"]["runtime_feasibility_revalidation"] = {
                "status": "required",
                "reason": "the restored topic exceeds the current execution boundary",
            }
            forced_context = runner._topic_refinement_context(workflow["stages"][0])
            self.assertEqual(forced_context["salvage_plan"]["mode"], "structural_pivot")
            self.assertTrue(forced_context["salvage_plan"]["forced"])
            self.assertIn("topic", runner._continuation_targets(
                runner.active_research_requests, {"topic": workflow["stages"][0],
                                                  "survey": workflow["stages"][1],
                                                  "experiment": workflow["stages"][2]}))
            runner.close()

    def test_eligible_survey_carries_provisional_topic_requirements_forward(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "admission_state": "provisional_for_survey",
                "maturity_open_requirements": ["Ground the mechanism."],
                "topic": {
                    "id": "direction_a", "title": "A direction",
                    "domain": "science",
                    "research_question": "Does mechanism A change the measured outcome?",
                },
            }
            gated = runner._gate_free_topic_survey({
                "status": "completed", "gap_state": "eligible_for_experiment",
                "survey_ref": "survey-ref", "assessment_ref": "assessment-ref",
            }, stage=workflow["stages"][1])
            self.assertEqual(
                gated["topic_admission"], "provisional_supported_for_experiment")
            self.assertEqual(
                gated["carried_maturity_requirements"], ["Ground the mechanism."])
            runner.close()

    def test_repeated_continuation_hold_pivots_until_the_deadline(self):
        """A repeated survey hold keeps changing strategy until the hard wall."""
        class FastClock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                self.value += 0.05
                return self.value

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 5,
                "reuse_completed": False, "reuse_output_path": None,
            })
            for stage in workflow["stages"]:
                stage["deadline_seconds"] = 5
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["time_policy"] = {
                "first_result_seconds": 1, "target_seconds": 2,
                "hard_seconds": 5, "checkpoint_seconds": 1,
            }
            workflow["retry_policy"] = {"mode": "until_deadline", "backoff_seconds": 0}
            runner = ComposerRunner(workflow, clock=FastClock())
            calls = []

            def staged(stage, **kwargs):
                calls.append(stage["id"])
                result = {
                    "status": "completed",
                    "output_path": str(Path(stage["project_dir"]) / "result.json"),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                }
                if stage["id"] == "topic":
                    result["topic"] = {
                        "id": "direction_x", "title": "Direction X",
                        "domain": "science", "research_question": "Does X change Y?",
                    }
                elif stage["id"] == "survey":
                    result.update({"gap_state": "insufficient_evidence"})
                    result = runner._gate_free_topic_survey(result, stage=stage)
                return result

            runner._run_stage = staged
            result = runner.run()
            self.assertEqual(result["status"], "paused")
            self.assertGreaterEqual(result["continuation_cycles"], 2)
            self.assertGreaterEqual(len(calls), 5)
            self.assertEqual(calls[:5], ["topic", "survey", "survey", "topic", "survey"])
            self.assertTrue(any(item.get("action") == "continue_research"
                                for item in result["feedback"]))
            self.assertTrue(any(
                request.get("id", "").startswith("auto-")
                for item in result["feedback"]
                if item.get("action") == "continue_research"
                for request in item.get("research_requests", [])
                if isinstance(request, dict)))
            runner.close()

    def test_free_topic_selection_switches_between_pinned_experiment_capabilities(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            catalog = root / "capability.json"
            catalog.write_text(json.dumps({
                "schema_version": "experiment-capability-1",
                "capability_id": "cap_b",
                "experiment": {
                    "id": "capability_b_study",
                    "revision": 1,
                    "study_type": "methods_validation",
                    "domain": "robust statistics",
                    "research_question": "Does the robust estimator reduce tail error under contamination?",
                    "hypothesis": "The robust estimator reduces contaminated tail error.",
                    "method": "Run a frozen paired simulation.",
                    "parameters": {}, "seed": 1, "run_count": 1,
                    "stopping_rule": "Run the declared simulation once.",
                    "primary_outcomes": [], "limitations": [], "literature_gate": None,
                    "execution": {}, "validation": {}, "required_assets": [], "reviewers": [],
                    "stage_seconds": {}, "max_observations": 1, "max_asset_bytes": 1,
                },
            }))
            workflow["experiment_catalog"] = [{"id": "cap_b", "config_path": str(catalog.resolve())}]
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            unrelated_dir = root / "unrelated-topic"
            unrelated_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "unrelated_topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(unrelated_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            next(item for item in workflow["stages"]
                 if item["id"] == "survey")["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            template = json.loads(Path("config/experiment-capabilities/free_quadrature_peak.json").read_text())
            config = {"experiment": template["experiment"], "supplied_context": "base"}
            runner.context["unrelated_topic"] = {
                "kind": "topic_discovery",
                "admission_state": "mature",
                "topic": {
                    "experiment_capability_id": "cap_b",
                    "research_question": "This unrelated branch must never be bound.",
                },
            }
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "admission_state": "provisional_for_survey",
                "maturity_open_requirements": ["Separate the competing mechanism."],
                "topic": {
                    "experiment_capability_id": "cap_b",
                    "research_question": "Does the robust estimator reduce tail error under contamination?",
                },
            }
            experiment_stage = next(
                item for item in workflow["stages"] if item["id"] == "experiment")
            with self.assertRaisesRegex(ValidationError, "provisional topic cannot enter"):
                runner._apply_topic_to_experiment_config(experiment_stage, config)
            runner.context["survey"] = {
                "kind": "survey",
                "status": "completed",
                "gap_state": "eligible_for_experiment",
                "topic_admission": "provisional_supported_for_experiment",
                "carried_maturity_requirements": ["Separate the competing mechanism."],
            }
            selected = runner._apply_topic_to_experiment_config(experiment_stage, config)
            self.assertEqual(selected["experiment"]["id"], "capability_b_study")
            self.assertEqual(selected["experiment"]["research_question"],
                             "Does the robust estimator reduce tail error under contamination?")
            self.assertIn(
                "Separate the competing mechanism.", selected["supplied_context"])
            self.assertIn(
                "literature gap decision does not by itself resolve them",
                selected["supplied_context"])
            self.assertEqual(
                selected["experiment"]["limitations"],
                config["experiment"]["limitations"],
                "deferred maturity requirements must not mutate the generated program contract",
            )
            runner.close()

    def test_design_driven_capability_injects_the_proposed_design(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            design = {
                "family": "monte_carlo_estimator_comparison",
                "data_process": {"kind": "gaussian_contamination", "sample_size": 150,
                                 "contamination_rate": 0.08, "contamination_scale": 12.0},
                "estimators": ["mean", "median", "median_of_means"],
                "primary": "median_of_means", "baseline": "mean", "block_size": 5, "seed": 99,
            }
            catalog = root / "capability.json"
            catalog.write_text(json.dumps({
                "schema_version": "experiment-capability-1", "capability_id": "design_driven",
                "experiment": {
                    "id": "design_driven_study", "revision": 1, "study_type": "methods_validation",
                    "domain": "computational statistics",
                    "research_question": "Default question.", "hypothesis": "Pinned hypothesis.",
                    "method": "Run the pinned design-driven engine.", "parameters": {"design_driven": True},
                    "seed": 1, "run_count": 1, "stopping_rule": "Run the declared design once.",
                    "primary_outcomes": [], "limitations": [], "literature_gate": None,
                    "execution": {"input": {"design": {}}}, "validation": {}, "required_assets": [],
                    "reviewers": [], "stage_seconds": {}, "max_observations": 1, "max_asset_bytes": 1,
                },
            }))
            workflow["experiment_catalog"] = [{"id": "design_driven", "config_path": str(catalog.resolve())}]
            runner = ComposerRunner(workflow)
            config = {"experiment": {"revision": 1, "literature_gate": None}, "supplied_context": "base"}
            runner.context["topic"] = {"kind": "topic_discovery", "topic": {
                "experiment_capability_id": "design_driven",
                "research_question": "Does the pinned engine reproduce the declared contrast?",
                "domain": "robust statistics", "experiment_design": design}}
            selected = runner._apply_topic_to_experiment_config(workflow["stages"][1], config)
            experiment = selected["experiment"]
            self.assertEqual(experiment["execution"]["input"]["design"], design)
            self.assertEqual(experiment["seed"], 99)
            self.assertEqual(experiment["domain"], "robust statistics")
            self.assertEqual(experiment["research_question"],
                             "Does the pinned engine reproduce the declared contrast?")
            self.assertTrue(experiment["parameters"]["design_driven"])
            with self.assertRaisesRegex(ValidationError, "requires a proposed experiment_design"):
                runner.context["topic"]["topic"].pop("experiment_design")
                runner._apply_topic_to_experiment_config(workflow["stages"][1], config)
            runner.close()


    def test_catalog_paper_projection_replaces_stale_scientific_identity(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            capability = Path("config/experiment-capabilities/robust_mean.json").resolve()
            workflow["experiment_catalog"] = [{"id": "robust_mean", "config_path": str(capability)}]
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "topic": {"title": "Robust tail behavior under contamination",
                          "research_question": "Does median-of-means reduce contaminated tail error?"},
            }
            packet = {
                "writer_contract": {"section_order": [
                    {"id": "introduction", "unit_ids": ["intro_p1"]},
                    {"id": "question", "unit_ids": ["question_p1"]},
                    {"id": "methods", "unit_ids": ["methods_p1"]},
                    {"id": "results", "unit_ids": ["results_p1"]},
                    {"id": "interpretation", "unit_ids": ["interpretation_p1"]},
                    {"id": "limitations", "unit_ids": ["limitations_p1"]},
                    {"id": "conclusion", "unit_ids": ["conclusion_p1"]},
                ]},
                "results_package": {
                    "id": "robust_mean_pilot",
                    "question": "Does median-of-means reduce contaminated tail error?",
                    "procedures": [{"id": "robust_protocol", "description":
                                    "Run seeded samples comparing the empirical mean and median-of-means under replacement contamination."}],
                    "metrics": [{"id": "tail_reduction", "presentation":
                                  "median-of-means reduced the contaminated 95th percentile error by 48.2 percent"}],
                    "findings": [{"id": "robustness_gain", "statement":
                                  "Under contamination, median-of-means reduced the 95th percentile error by 48.2 percent."}],
                    "limitations": ["The result applies only to the declared univariate simulation."],
                    "assets": [],
                },
            }
            paper_config = {
                "schema_version": "paper-release-score-3", "paper_id": "dynamic_test",
                "title": "old quadrature title", "revision": 1, "document_type": "research_paper",
                "manuscript_project_dir": str(root / "manuscript"),
                "survey_project_dir": str(root / "survey"), "survey_ref": "x", "assessment_ref": "y",
                "results_package": "z", "evidence": [], "claims": [], "references": [],
                "authors": ["Sci-saurus"], "keywords": ["test"],
                "storyline": {"id": "old", "revision": 1, "thesis": "old",
                              "beats": [{"id": "old", "role": "question", "proposition": "old"}]},
                "depth_profile": {"min_words": 1, "min_references": 1, "min_full_text_references": 0,
                                  "min_sections": 1, "required_section_titles": ["Results"],
                                  "max_numeric_repetitions": 4, "max_caveat_repetitions": 3},
                "interpretation_file": str(root / "interpretation.json"), "figure_arguments": [],
                "surface_policy": {"allow_control_patterns": [], "max_numeric_repetitions": 4,
                                   "max_caveat_repetitions": 3},
            }
            (root / "interpretation.json").write_text("{}")
            projected_paper, projected_packet = runner._synchronize_catalog_paper_inputs(
                paper_config, packet, {"argument": {"primary_argument": {
                    "thesis": "Median-of-means trades a small clean-data penalty for lower contaminated tail error."
                }}})
            self.assertEqual(projected_paper["title"], "Robust tail behavior under contamination")
            self.assertNotIn("quadrature", json.dumps(projected_paper).casefold())
            self.assertIn("median-of-means", json.dumps(projected_packet).casefold())
            self.assertEqual(len(projected_paper["storyline"]["beats"]), 7)
            runner.close()

    def test_reopens_only_requested_stage_in_a_bounded_continuation_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["continuation_policy"] = {"max_cycles": 2}
            runner = ComposerRunner(workflow)
            calls = []

            def staged(stage, **kwargs):
                calls.append((stage["id"], stage["project_dir"]))
                output = Path(stage["project_dir"]) / "result.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                if stage["id"] == "experiment" and len([item for item in calls if item[0] == "experiment"]) == 1:
                    return {
                        "status": "research_expansion_required",
                        "output_path": str(output),
                        "project_dir": stage["project_dir"],
                        "research_expansion_requests": [{
                            "id": "run_control", "kind": "additional_experiment", "owner": "methods.validation",
                            "objective": "Run a discriminating control.",
                            "why": "The current result does not separate the explanations.",
                            "success_condition": "The control separates the explanations.",
                            "evidence_needed": "Validated raw output.",
                        }],
                    }
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = staged
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["continuation_cycles"], 1)
            self.assertEqual([item[0] for item in calls], ["survey", "experiment", "experiment"])
            self.assertIn("continuations/cycle-1", calls[-1][1])
            self.assertEqual(result["stages"]["experiment"]["attempt_count"], 2)
            self.assertTrue(any(item["action"] == "continue_research" for item in result["feedback"]))
            self.assertTrue(any(item["action"] == "activate_work_orders"
                                for item in result["department_activity"]))
            self.assertTrue(any(item["action"] == "resolve_work_orders" and item["outcome"] == "completed"
                                for item in result["department_activity"]))

    def test_research_hold_never_admits_downstream_before_continuation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            argument_dir = root / "argument"
            argument_dir.mkdir()
            argument_config = root / "argument.json"
            argument_config.write_text("{}")
            workflow["stages"].append({
                "id": "argument", "kind": "argument", "config_path": str(argument_config.resolve()),
                "project_dir": str(argument_dir.resolve()), "depends_on": ["experiment"],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            calls = []

            def staged(stage, **kwargs):
                calls.append(stage["id"])
                output = Path(stage["project_dir"]) / "result.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                if stage["id"] == "experiment" and calls.count("experiment") == 1:
                    return {
                        "status": "research_expansion_required", "output_path": str(output),
                        "project_dir": stage["project_dir"],
                        "research_expansion_requests": [{
                            "id": "run_control", "kind": "additional_experiment", "owner": "methods.validation",
                            "objective": "Run a discriminating control.",
                            "why": "The current result does not separate the explanations.",
                            "success_condition": "The control separates the explanations.",
                            "evidence_needed": "Validated raw output.",
                        }],
                    }
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = staged
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["survey", "experiment", "experiment", "argument"])
            self.assertEqual(result["continuation_cycles"], 1)
            self.assertEqual(result["stages"]["experiment"]["status"], "completed")

    def test_resume_hold_reopens_before_dependency_scheduler(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.context["experiment"] = {
                "stage_id": "experiment", "kind": "experiment", "status": "research_expansion_required",
                "research_expansion_requests": [{
                    "id": "run_control", "kind": "additional_experiment", "owner": "methods.validation",
                    "objective": "Run a discriminating control.", "why": "The result is ambiguous.",
                    "success_condition": "The control separates the explanations.",
                    "evidence_needed": "Validated raw output.",
                }],
            }
            runner.context["survey"] = {"stage_id": "survey", "kind": "survey", "status": "completed"}
            runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
            runner.stage_records["experiment"] = {"kind": "experiment", "status": "research_expansion_required"}
            runner._checkpoint("held", force=True)
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)
            self.assertEqual(resumed._continuation_policy()["mode"], "until_deadline")
            calls = []

            def recovered(stage, **kwargs):
                calls.append(stage["id"])
                output = Path(stage["project_dir"]) / "result.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            resumed._run_stage = recovered
            result = resumed.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["experiment"])
            self.assertEqual(result["continuation_cycles"], 1)

    def test_continuation_interpretation_output_uses_a_new_cycle_namespace(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            original_output = root / "interpretation.json"
            input_path = root / "packet.json"
            model_path = root / "model.json"
            input_path.write_text(json.dumps({"results_package": {}}))
            model_path.write_text(json.dumps({}))
            original_output.write_text("old")
            stage = {
                "id": "interpretation", "kind": "interpretation",
                "config_path": str((root / "interpretation-config.json").resolve()),
                "project_dir": str((root / "interpretation").resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            stage["project_dir"] = str((root / "interpretation").resolve())
            Path(stage["project_dir"]).mkdir()
            Path(stage["config_path"]).write_text(json.dumps({
                "model_config_path": str(model_path.resolve()),
                "input_path": str(input_path.resolve()),
                "output_path": str(original_output.resolve()),
            }))
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"interpretation"}
            dispatch = runner._stage_for_cycle(stage)

            class FakeInterpretation:
                def __init__(self, *args, **kwargs):
                    pass

                def run(self, packet):
                    return {"research_question": "Does the measured rate change?", "result_patterns": [],
                            "competing_explanations": [], "discriminating_experiments": [],
                            "prioritization": {"primary_pattern_id": "none", "secondary_pattern_ids": [],
                                               "rationale": "No pattern was supplied."},
                            "conclusion": "No conclusion is available."}

            with patch("scisaurus.runtime.scientific_interpretation.ScientificInterpretationRunner",
                       FakeInterpretation):
                result = runner._run_stage(dispatch)
            self.assertIn("continuations/cycle-1", result["output_path"])
            self.assertTrue(Path(result["output_path"]).is_file())
            self.assertEqual(original_output.read_text(), "old")
            runner.close()

    def test_resume_preserves_the_persisted_hard_wall(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.started_epoch = 100.0
            runner.deadline_epoch = 110.0
            runner._checkpoint("paused", force=True)
            runner.close()
            with patch("scisaurus.runtime.composer.time.time", return_value=111.0):
                resumed = ComposerRunner(workflow, resume=True)
                self.assertEqual(resumed.deadline_epoch, 110.0)
                with self.assertRaisesRegex(ValidationError, "deadline"):
                    resumed._remaining()
                resumed.close()

    def test_hard_timeout_persists_a_concise_interim_report(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.deadline = runner.clock() - 1
            runner.deadline_epoch = time.time() - 1
            result = runner.run()
            report_path = root / "composer" / "output" / "interim_report.json"
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(Path(result["interim_report_path"]), report_path.resolve())
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text())
            self.assertEqual(report["schema_version"], "composer-interim-report-1")
            self.assertEqual(report["stop_reason"], "hard_deadline")
            self.assertIn("resume_hint", report)
            self.assertEqual(report["completed_stage_ids"], [])
            self.assertEqual([row["id"] for row in report["stages"]], ["survey", "experiment"])
            report_path.unlink()
            fallback = read_interim_report(root / "composer")
            self.assertEqual(fallback["stop_reason"], "process_interrupted")
            self.assertEqual(fallback["deadline_seconds"], 30)
            self.assertEqual(fallback["pending_stage_ids"], ["survey", "experiment"])
            self.assertEqual([(row["id"], row["kind"]) for row in fallback["stages"]],
                             [("survey", "survey"), ("experiment", "experiment")])

    def test_resume_can_extend_the_same_deadline_and_persist_the_decision(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.started_epoch = time.time() - 31
            runner.started = runner.clock() - 31
            runner.deadline_epoch = time.time() - 1
            runner.deadline = runner.clock() - 1
            runner._checkpoint("paused", force=True)
            runner.close()

            resumed = ComposerRunner(workflow, resume=True, additional_seconds=60)
            self.assertGreater(resumed.deadline_epoch, time.time() + 50)
            self.assertEqual(len(resumed.deadline_extensions), 1)
            progress = json.loads((root / "composer" / "output" / "progress.json").read_text())
            self.assertEqual(len(progress["deadline_extensions"]), 1)
            self.assertGreater(resumed._remaining(), 50)
            resumed.close()

            resumed_again = ComposerRunner(workflow, resume=True)
            self.assertEqual(len(resumed_again.deadline_extensions), 1)
            self.assertGreater(resumed_again._remaining(), 50)
            resumed_again.close()

    def test_new_composer_run_cannot_apply_a_deadline_extension(self):
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaisesRegex(ValidationError, "only valid when resuming"):
                ComposerRunner(self._workflow(Path(path)), additional_seconds=30)

    def test_finished_live_ticker_cannot_overwrite_a_newer_stage_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["time_policy"]["checkpoint_seconds"] = 0.5
            runner = ComposerRunner(workflow)
            finish = runner._start_live_progress(workflow["stages"][0])
            stale_epoch = runner._live_progress_epoch
            finish()
            runner._checkpoint("next-stage", force=True)
            before = (root / "composer" / "output" / "progress.json").read_bytes()
            runner._live_progress("survey:running", epoch=stale_epoch)
            self.assertEqual((root / "composer" / "output" / "progress.json").read_bytes(), before)
            runner.close()

    def test_resume_selects_an_older_checkpoint_with_ahead_attempt_frontier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            terminal = {
                "status": "paused",
                "stages": {
                    "survey": {"status": "retrying", "attempt_count": 51},
                    "experiment": {"status": "pending", "attempt_count": 0},
                },
            }

            def checkpoint(attempt_count):
                runner._publish(
                    f"command/composer/checkpoints/regression-{attempt_count}",
                    "progress_checkpoint",
                    {"workflow_id": workflow["id"], "status": "running", "stages": {
                        "survey": {"status": "retrying", "attempt_count": attempt_count},
                        "experiment": {"status": "pending", "attempt_count": 0},
                    }},
                    "command.composer",
                )

            # The stale checkpoint is newer, so a first-row-only lookup would
            # incorrectly discard the older checkpoint at the true frontier.
            checkpoint(53)
            checkpoint(51)
            selected = runner._latest_inflight_checkpoint(terminal)
            self.assertIsNotNone(selected)
            self.assertEqual(selected["stages"]["survey"]["attempt_count"], 53)
            self.assertTrue(ComposerRunner._checkpoint_advances_terminal_state(
                selected, terminal))
            runner.close()

    def test_routes_journal_editor_to_editor_in_chief(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            event = {
                "event_id": "paper-review-journal-editor",
                "kind": "review",
                "reviewer_id": "journal_editor",
                "stage": 6,
                "status": "needs_revision",
                "decision": "revise",
                "severity_counts": {"blocking": 0, "major": 2, "minor": 0},
                "finding_ids": ["journal_editor_reference_count"],
            }
            runner._record_internal_feedback(workflow["stages"][0], event)
            self.assertEqual(runner.feedback[0]["to"],
                             {"dept": "editorial", "agent": "editor-in-chief"})
            self.assertEqual(runner.feedback[0]["action"], "request_revision")
            runner.close()

    def test_routes_research_expansion_to_owning_departments(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            event = {
                "event_id": "paper-research-expansion",
                "kind": "research_gate",
                "reviewer_id": "journal_editor",
                "stage": 6,
                "status": "research_expansion_required",
                "decision": "research_expansion_required",
                "expansion_requests": [{
                    "id": "run_more", "kind": "additional_experiment", "owner": "methods.validation",
                    "objective": "Run a discriminating control.", "why": "The current data do not separate the hypotheses.",
                    "success_condition": "The control changes the decision.", "evidence_needed": "Validated raw output.",
                    "blocked_checks": ["figure_count"],
                }],
            }
            runner._record_internal_feedback(workflow["stages"][0], event)
            self.assertEqual(runner.feedback[0]["action"], "request_research_expansion")
            self.assertEqual(runner.feedback[0]["to"], {"dept": "methods", "agent": "chief"})
            runner.close()

    def test_custom_department_chief_receives_composer_handoff(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            organization = default_organization()
            for charter in organization["departments"]:
                if charter["id"] == "methods":
                    charter["chief"] = "methods-lead"
            workflow["organization"] = organization
            runner = ComposerRunner(workflow)
            runner._record_feedback(workflow["stages"][0], {
                "status": "completed", "output_path": str(root / "survey.json"),
            })
            self.assertEqual(runner.feedback[0]["to"], {"dept": "methods", "agent": "methods-lead"})
            runner.close()

    def test_live_progress_uses_plain_snapshot_outside_sqlite_thread(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.departments.snapshot = lambda: (_ for _ in ()).throw(
                AssertionError("background progress must not query SQLite"))
            error = []

            def tick():
                try:
                    runner._live_progress("survey:running")
                except Exception as exc:  # pragma: no cover - assertion below reports the failure
                    error.append(exc)

            thread = threading.Thread(target=tick)
            thread.start(); thread.join()
            self.assertEqual(error, [])
            self.assertEqual(json.loads((root / "composer" / "output" / "progress.json").read_text())["phase"],
                             "survey:running")
            runner.close()

    def test_resuming_running_stage_reconciles_task_attempt_before_retry(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            task = runner._stage_task(workflow["stages"][0])
            attempt_id = "interrupted-attempt"
            runner.tasks.start_attempt(task["task_id"], attempt_id, owner="command.composer", lease_ttl_seconds=30)
            runner.stage_records["survey"] = {
                "kind": "survey", "status": "running", "attempt_id": attempt_id,
                "attempt_number": 1, "project_dir": workflow["stages"][0]["project_dir"],
            }
            runner._checkpoint("survey:running", force=True)
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)

            def recovered(stage, **kwargs):
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            resumed._run_stage = recovered
            result = resumed.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["stages"]["survey"]["attempts"][0]["state"], "unknown")
            with sqlite3.connect(root / "composer" / "state" / "control.sqlite") as conn:
                self.assertEqual(conn.execute(
                    "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0],
                    "result_unknown")

    def test_resuming_after_terminal_task_crash_uses_recovery_generation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            task = runner._stage_task(workflow["stages"][0])
            attempt_id = "terminal-crash-attempt"
            runner.tasks.start_attempt(task["task_id"], attempt_id, owner="command.composer", lease_ttl_seconds=30)
            runner.tasks.finish_attempt(attempt_id, "succeeded")
            runner.tasks.transition(task["task_id"], "awaiting_review", "command.composer")
            runner.tasks.transition(task["task_id"], "completed", "command.composer")
            runner.stage_records["survey"] = {
                "kind": "survey", "status": "running", "attempt_id": attempt_id,
                "attempt_number": 1, "project_dir": workflow["stages"][0]["project_dir"],
                "task_id": task["task_id"],
            }
            runner._checkpoint("survey:running", force=True)
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)

            def recovered(stage, **kwargs):
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            resumed._run_stage = recovered
            result = resumed.run()
            self.assertEqual(result["status"], "completed")
            recovery_task = result["stages"]["survey"]["task_id"]
            self.assertIn("recovery-", recovery_task)
            with sqlite3.connect(root / "composer" / "state" / "control.sqlite") as conn:
                self.assertEqual(conn.execute(
                    "SELECT state FROM tasks WHERE task_id=?", (recovery_task,)).fetchone()[0],
                    "completed")

    def test_resuming_after_stale_continuation_task_uses_recovery_generation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 1
                runner.reopened_stage_ids = {"survey"}
                first = runner._stage_task(workflow["stages"][0])
                runner.tasks.transition(
                    first["task_id"], "stale", "command.composer",
                    reason="watchdog fenced the prior continuation",
                )
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "retrying",
                    "attempt_id": "prior-attempt",
                }
                recovered = runner._stage_task(workflow["stages"][0])
                self.assertNotEqual(recovered["task_id"], first["task_id"])
                self.assertIn("recovery-", recovered["task_id"])
                self.assertEqual(recovered["state"], "queued")
                self.assertEqual(runner.tasks.get(first["task_id"])["state"], "stale")
            finally:
                runner.close()

    def test_terminal_run_report_does_not_mask_a_more_advanced_live_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            task = runner._stage_task(workflow["stages"][0])
            attempt_id = "live-checkpoint-attempt"
            runner.tasks.start_attempt(attempt_id=attempt_id, task_id=task["task_id"],
                                       owner="command.composer", lease_ttl_seconds=30)
            runner.stage_records["survey"] = {
                "kind": "survey", "status": "completed", "attempt_id": attempt_id,
                "attempt_number": 1, "project_dir": workflow["stages"][0]["project_dir"],
                "task_id": task["task_id"],
            }
            runner.context["survey"] = {"status": "completed", "kind": "survey"}
            runner._checkpoint("survey:completed", force=True)
            runner.stage_records["survey"]["status"] = "blocked"
            runner.context = {}
            runner.status = "blocked"
            runner._finish()

            resumed = ComposerRunner(workflow, resume=True)
            self.assertEqual(resumed.stage_records["survey"]["status"], "completed")
            self.assertEqual(resumed.context["survey"]["status"], "completed")
            resumed.close()

    def test_deadline_prevents_continuation_activation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.deadline = runner.clock() - 1
            runner.active_research_requests = [{
                "id": "run-control", "kind": "additional_experiment", "owner": "methods.validation",
                "objective": "Run a discriminating control.", "why": "The result is ambiguous.",
                "success_condition": "The control separates the explanations.",
                "evidence_needed": "Validated raw output.", "source_stage_id": "experiment",
            }]
            with self.assertRaisesRegex(ValidationError, "deadline"):
                runner._begin_continuation(set(), {stage["id"]: stage for stage in workflow["stages"]})
            self.assertEqual(runner.continuation_cycles, 0)
            runner.close()

    def test_follow_up_requests_are_projected_into_fresh_interpretation_packet(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"interpretation"}
            runner.active_research_requests = [{
                "id": "interpret-more", "kind": "interpretation_expansion", "owner": "strategy.interpretation",
                "objective": "Compare the two plausible mechanisms.", "why": "The observed metrics diverge.",
                "success_condition": "The revised interpretation identifies a discriminating prediction.",
                "evidence_needed": "Metric-level comparison and sensitivity analysis.",
                "source_stage_id": "paper",
            }]
            packet = runner._project_continuation_requests({}, {"id": "interpretation"})
            self.assertEqual(packet["scientific_follow_up"][0]["objective"],
                             "Compare the two plausible mechanisms.")
            self.assertIn("follow_up_instruction", packet)
            runner.close()

    def test_continuation_materializes_interpretation_and_paper_packets(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"interpretation", "paper"}
            runner.active_research_requests = [{
                "id": "revise-meaning", "kind": "interpretation_expansion", "owner": "strategy.interpretation",
                "objective": "Compare the two plausible mechanisms.", "why": "The observed metrics diverge.",
                "success_condition": "The revised interpretation identifies a discriminating prediction.",
                "evidence_needed": "Metric-level comparison and sensitivity analysis.",
                "source_stage_id": "paper",
            }]
            source = root / "packet.json"
            source.write_text(json.dumps({"results_package": {}}))
            interpretation_stage = {"id": "interpretation", "kind": "interpretation",
                                    "project_dir": str((root / "interpretation").resolve())}
            interpretation_config = {"input_path": str(source.resolve())}
            runner._adapt_continuation_config(interpretation_stage, interpretation_config)
            self.assertNotEqual(interpretation_config["input_path"], str(source.resolve()))
            self.assertEqual(json.loads(Path(interpretation_config["input_path"]).read_text())[
                "scientific_follow_up"][0]["kind"], "interpretation_expansion")
            paper_stage = {"id": "paper", "kind": "paper",
                            "project_dir": str((root / "paper").resolve())}
            paper_config = {"packet_path": str(source.resolve())}
            runner._adapt_continuation_config(paper_stage, paper_config)
            self.assertNotEqual(paper_config["packet_path"], str(source.resolve()))
            self.assertTrue(Path(paper_config["packet_path"]).is_file())
            runner.close()

    def test_reopened_internal_events_get_cycle_specific_ids(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 2
            runner.reopened_stage_ids = {"survey"}
            event = {"event_id": "paper-research-admission", "kind": "research_gate",
                     "reviewer_id": "journal_editor", "status": "accepted", "decision": "proceed"}
            runner._record_internal_feedback(workflow["stages"][0], event)
            runner._record_internal_feedback(workflow["stages"][0], event)
            self.assertEqual(len(runner.feedback), 1)
            self.assertEqual(runner.feedback[0]["event_id"], "cycle-2-paper-research-admission")
            runner.close()

    def test_reuses_only_an_explicit_accepted_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            checkpoint = root / "accepted.json"
            checkpoint.write_text(json.dumps({"status": "accepted", "value": 7}))
            workflow["stages"][0]["reuse_completed"] = True
            workflow["stages"][0]["reuse_output_path"] = str(checkpoint.resolve())
            runner = ComposerRunner(workflow)
            reused = runner._run_stage(workflow["stages"][0])
            self.assertEqual(reused["value"], 7)
            runner.context["survey"] = reused
            runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
            # The second stage keeps its conventional output/run.json path;
            # it has no such file and is dispatched normally.
            calls = []

            def fake_stage(stage):
                calls.append(stage["id"])
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = fake_stage
            result = runner.run()
            self.assertEqual(calls, ["experiment"])

    def test_reopened_topic_cycle_does_not_reuse_blocked_stage_cache(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str((root / "stage.json").resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"].insert(0, topic_stage)
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 1
                runner.reopened_stage_ids = {"topic"}
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "review_status": "topic_budget_exhausted",
                }
                calls = []

                def blocked(stage, **kwargs):
                    calls.append("blocked")
                    raise ModelWorkBlocked("the previous topic envelope was exhausted")

                with patch.object(runner, "_execute_stage", side_effect=blocked):
                    with self.assertRaises(ModelWorkBlocked):
                        runner._run_stage(topic_stage)

                output = topic_dir / "output" / "topic.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"status": "completed"}))

                def recovered(stage, **kwargs):
                    calls.append("recovered")
                    return {
                        "status": "completed", "output_path": str(output.resolve()),
                    }

                with patch.object(runner, "_execute_stage", side_effect=recovered):
                    result = runner._run_stage(topic_stage)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(calls, ["blocked", "recovered"])
            finally:
                runner.close()

    def test_free_topic_checkpoint_reuse_is_opt_in(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text(json.dumps({"schema_version": "topic-discovery-config-1"}))
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": True,
                "reuse_output_path": str((root / "accepted-topic.json").resolve()),
            })
            checkpoint = root / "accepted-topic.json"
            checkpoint.write_text(json.dumps({"status": "accepted", "topic": {"research_question": "old"}}))
            runner = ComposerRunner(workflow)
            runner._remaining = lambda: 10.0
            with patch("scisaurus.runtime.topic_discovery.validate_topic_stage_config") as validate, \
                    patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as topic_runner:
                validate.return_value = {
                    "model_config_path": str((root / "model.json").resolve()),
                    "output_path": str((root / "topic-output.json").resolve()),
                    "candidate_count": 3, "max_attempts": 1, "schema_version": "topic-discovery-config-1",
                }
                (root / "model.json").write_text("{}")
                topic_runner.return_value.run.return_value = {
                    "status": "completed", "topic": {"research_question": "new"}}
                result = runner._run_stage(workflow["stages"][0])
            self.assertEqual(result["topic"]["research_question"], "new")
            runner.close()

    def test_topic_stage_materializes_research_program_artifact(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text(json.dumps({"schema_version": "topic-discovery-config-1"}))
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            runner._remaining = lambda: 10.0
            output_path = root / "topic-output.json"
            model_path = root / "model.json"
            model_path.write_text("{}")
            result = {**topic_package(), "status": "completed",
                      "topic": next(item for item in topic_package()["candidates"]
                                     if item["id"] == "branch_1")}
            with patch("scisaurus.runtime.topic_discovery.validate_topic_stage_config") as validate, \
                    patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as topic_runner:
                validate.return_value = {
                    "model_config_path": str(model_path.resolve()),
                    "output_path": str(output_path.resolve()),
                    "candidate_count": 3, "max_attempts": 1,
                    "schema_version": "topic-discovery-config-1",
                }
                topic_runner.return_value.run.return_value = result
                context = runner._run_stage(workflow["stages"][0])
            program_path = Path(context["research_program_path"])
            self.assertTrue(program_path.is_file())
            program = json.loads(program_path.read_text())
            self.assertEqual(program["schema_version"], "research-program-1")
            self.assertEqual(program["selected_id"], "branch_1")
            self.assertEqual(len(program["branches"]), 3)
            self.assertEqual(json.loads(output_path.read_text())["research_program"], program)
            runner.context["topic"] = {**context, "kind": "topic_discovery"}
            packet = {}
            runner._attach_topic_program(packet)
            self.assertEqual(packet["research_program"], program)
            runner.close()

    def test_journal_consumer_attaches_default_experiment_quality_contract(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            paper_config = root / "paper-config.json"
            paper_config.write_text(json.dumps({
                "schema_version": "paper-release-score-3", "document_type": "research_paper",
                "depth_profile": {"min_figures": 3},
            }))
            paper_dir = root / "paper"
            paper_dir.mkdir()
            workflow["stages"].append({
                "id": "paper", "kind": "paper", "config_path": str(paper_config.resolve()),
                "project_dir": str(paper_dir.resolve()), "depends_on": ["experiment"],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            config = {"experiment": {"id": "study"}}
            result = runner._ensure_journal_quality_contract(workflow["stages"][1], config)
            self.assertEqual(result["experiment"]["quality_contract"]["minimum_figures"], 3)
            runner.close()

    def test_journal_profile_resolves_nested_paper_descriptor(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            nested = root / "paper-config.json"
            nested.write_text(json.dumps({
                "schema_version": "paper-release-score-3",
                "document_type": "research_paper",
                "depth_profile": {"min_figures": 4},
            }))
            descriptor = root / "paper.json"
            descriptor.write_text(json.dumps({"paper_config_path": str(nested)}))
            paper_dir = root / "paper"
            paper_dir.mkdir()
            workflow["stages"].append({
                "id": "paper", "kind": "paper", "config_path": str(descriptor.resolve()),
                "project_dir": str(paper_dir.resolve()), "depends_on": ["experiment"],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            profile = runner._paper_depth_profile("experiment")
            self.assertIsNotNone(profile)
            self.assertEqual(profile[0]["min_figures"], 4)
            runner.close()

    def test_paper_figure_reconciliation_drops_stale_asset_bindings(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            paper_config = {"schema_version": "paper-release-score-3",
                            "figure_arguments": [{"asset_id": "old_figure", "unit_id": "results_p1",
                                                   "why": "old", "observation": "old"}]}
            packet = {"results_package": {"assets": [
                {"id": "new_figure", "role": "figure", "caption": "A new observed pattern."},
            ]}, "writer_contract": {"section_order": [{"id": "results", "unit_ids": ["results_p1"]}],
                                        "figure_readings": [
                {"asset_id": "old_figure", "unit_id": "results_p1", "why": "old", "observation": "old"},
            ]}}
            argument = {"figure_plan": [{"kind": "figure", "asset_id": "new_figure",
                                          "purpose": "A new display.", "readout": "A new observed pattern."}]}
            result = runner._synchronize_paper_figure_arguments(paper_config, packet, argument)
            self.assertEqual([item["asset_id"] for item in result["figure_arguments"]], ["new_figure"])
            self.assertEqual(packet["writer_contract"]["figure_readings"][0]["asset_id"], "new_figure")
            runner.close()

    def test_routes_internal_review_feedback_and_deduplicates_resume_replay(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            stage = workflow["stages"][0]
            event = {"event_id": "paper-review-r1-science", "kind": "review", "reviewer_id": "science",
                     "stage": 1, "status": "needs_revision", "decision": "revise",
                     "severity_counts": {"blocking": 0, "major": 1, "minor": 0},
                     "finding_ids": ["science_f1"]}
            runner._record_internal_feedback(stage, event)
            runner._record_internal_feedback(stage, event)
            self.assertEqual(len(runner.feedback), 1)
            self.assertEqual(runner.feedback[0]["to"], {"dept": "strategy", "agent": "chief"})
            self.assertEqual(runner.feedback[0]["action"], "request_revision")
            with sqlite3.connect(Path(workflow["project_id"]) / "state" / "control.sqlite") as conn:
                row = conn.execute("SELECT state, disposition FROM messages").fetchone()
                self.assertEqual(row, ("acknowledged", "scheduled"))

            failure = {"event_id": "paper-review-failure-r1-methods", "kind": "review_failure",
                       "reviewer_id": "methods", "status": "blocked", "error": "provider deadline"}
            runner._record_internal_feedback(stage, failure)
            self.assertEqual(len(runner.feedback), 2)
            self.assertEqual(runner.feedback[1]["to"], {"dept": "executive-command", "agent": "arbiter"})
            self.assertEqual(runner.feedback[1]["message_disposition"], "escalated")

    def test_projects_interpretation_package_binding_to_direct_artifact(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            interpretation = {
                "schema_version": "scientific-interpretation-1",
                "research_question": "Does placement change the observed rate?",
                "result_patterns": [{
                    "id": "rate_shift", "result_ref": "rate", "pattern": "The observed rate changes.",
                    "so_what": "The practical ranking can change.", "supporting_evidence": ["rate"],
                    "contradicting_evidence": [],
                }],
                "competing_explanations": [{
                    "id": "geometry", "mechanism": "Geometry changes the error pattern.", "status": "possible",
                    "supporting_evidence": ["rate"], "counterevidence": [],
                    "discriminating_test": "Repeat the sweep across placements.",
                }],
                "discriminating_experiments": [{
                    "id": "placement_sweep", "question": "Does placement matter?",
                    "design": "Sweep the placement parameter.",
                    "predictions": ["The rate varies."], "required_measurements": ["Observed rate"],
                }],
                "prioritization": {
                    "primary_pattern_id": "rate_shift", "secondary_pattern_ids": [],
                    "rationale": "The rate shift is the central observation.",
                },
                "conclusion": "Placement may change the observed rate.",
            }
            package_path = root / "interpretation-package.json"
            package_path.write_text(json.dumps({
                "schema_version": "scientific-interpretation-package-1",
                "status": "accepted", "interpretation": interpretation,
            }))
            bound = runner._interpretation_binding_path(str(package_path.resolve()))
            self.assertTrue(Path(bound).is_file())
            self.assertEqual(json.loads(Path(bound).read_text()), interpretation)
            runner.close()


if __name__ == "__main__":
    unittest.main()
