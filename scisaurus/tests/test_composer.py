import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scisaurus.runtime.composer import ComposerRunner, read_interim_report, validate_workflow
from scisaurus.runtime.departments import default_organization
from scisaurus.core.errors import ValidationError


class ComposerWorkflowTests(unittest.TestCase):
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

    def test_custom_organization_must_cover_stage_owners(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            organization = default_organization()
            organization["departments"] = [item for item in organization["departments"] if item["id"] != "research"]
            workflow["organization"] = organization
            with self.assertRaisesRegex(ValidationError, "stage-owning departments"):
                validate_workflow(workflow)

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

    def test_invalid_continuation_request_is_rejected_without_blocking_composer(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
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

    def test_retries_failed_stage_in_a_fresh_attempt_directory(self):
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
            self.assertTrue(calls[1].endswith("attempts/attempt-2"))
            self.assertTrue(any(item["action"] == "retry_stage" for item in result["feedback"]))

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
            self.assertTrue(calls[3].endswith("attempts/attempt-4"))
            self.assertEqual(result["retry_policy"]["mode"], "until_deadline")
            self.assertTrue(all(item.get("retry_mode") == "until_deadline"
                                for item in result["feedback"] if item["action"] == "retry_stage"))

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
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "topic": {"research_question": "Does mechanism change the measured outcome?",
                          "search_queries": ["mechanism comparison", "controlled experiment", "public data"]},
            }
            config = {"survey": {"question": "placeholder", "seed_queries": ["old query"]}}
            projected = runner._apply_topic_to_survey_config(workflow["stages"][1], config)
            self.assertEqual(projected["survey"]["question"], "Does mechanism change the measured outcome?")
            self.assertEqual(projected["survey"]["seed_queries"],
                             ["mechanism comparison", "controlled experiment", "public data"])
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
            self.assertEqual(result["department_activity"][0]["action"], "activate_work_orders")
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
