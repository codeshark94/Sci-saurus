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
            topic = {"id": "direction_a", "title": "Direction A", "domain": "science",
                     "research_question": "Does mechanism A change the measured outcome?"}
            runner._record_topic_history({"topic": {**topic, "experiment_capability_id": "cap_a"}})
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            self.assertEqual(resumed.topic_history["entries"][0]["topic_id"], "direction_a")
            self.assertEqual(resumed._effective_topic_exclusions()["capability_ids"], ["cap_a"])
            self.assertIn("direction_a", resumed._effective_topic_exclusions()["topic_ids"])
            resumed.close()

    def test_exploration_seed_is_random_once_and_persisted_for_resume(self):
        with tempfile.TemporaryDirectory() as path, patch(
                "scisaurus.runtime.composer.secrets.randbits", return_value=123456):
            workflow = self._workflow(Path(path))
            runner = ComposerRunner(workflow)
            self.assertEqual(runner.exploration_seed, 123456)
            self.assertEqual(runner._topic_sampling_seed(), runner._topic_sampling_seed())
            runner._checkpoint("seed-persisted", force=True)
            progress = json.loads((Path(workflow["project_id"]) / "output" / "progress.json").read_text())
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
        self.assertEqual(queries[0], '"median of means"')
        self.assertEqual(len(queries), 3)

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
            self.assertIn("topic", runner._continuation_targets(
                runner.active_research_requests, {"topic": workflow["stages"][0],
                                                  "survey": workflow["stages"][1],
                                                  "experiment": workflow["stages"][2]}))
            runner.close()

    def test_identical_continuation_request_cannot_hot_loop_until_deadline(self):
        """A repeated survey hold must stop after its new work order is attempted."""
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
            self.assertEqual(result["status"], "research_expansion_required")
            self.assertEqual(result["continuation_cycles"], 2)
            self.assertEqual(calls, ["topic", "survey", "survey", "topic", "survey"])
            self.assertFalse(result["blockers"])
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
            runner = ComposerRunner(workflow)
            template = json.loads(Path("config/experiment-capabilities/free_quadrature_peak.json").read_text())
            config = {"experiment": template["experiment"], "supplied_context": "base"}
            runner.context["topic"] = {"kind": "topic_discovery", "topic": {
                "experiment_capability_id": "cap_b",
                "research_question": "Does the robust estimator reduce tail error under contamination?",
            }}
            selected = runner._apply_topic_to_experiment_config(workflow["stages"][1], config)
            self.assertEqual(selected["experiment"]["id"], "capability_b_study")
            self.assertEqual(selected["experiment"]["research_question"],
                             "Does the robust estimator reduce tail error under contamination?")
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
