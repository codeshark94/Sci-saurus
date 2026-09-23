import tempfile
import unittest
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.model_work import ModelWorkBlocked
from scisaurus.runtime.composer import ComposerRunner, validate_workflow


class ForwardProgressTests(unittest.TestCase):
    def _workflow(self, root):
        config = root / "stage.json"
        config.write_text("{}")
        survey_dir = root / "survey"
        experiment_dir = root / "experiment"
        survey_dir.mkdir()
        experiment_dir.mkdir()
        return {
            "schema_version": "composer-workflow-1",
            "id": "forward-progress-workflow",
            "revision": 1,
            "project_id": str(root / "composer"),
            "objective": "Move past bounded local failures and backfill them later",
            "stages": [
                {
                    "id": "survey", "kind": "survey", "config_path": str(config),
                    "project_dir": str(survey_dir), "depends_on": [],
                    "estimate_seconds": 1, "deadline_seconds": 20,
                    "bindings": [], "reuse_completed": False,
                    "reuse_output_path": None,
                },
                {
                    "id": "experiment", "kind": "experiment", "config_path": str(config),
                    "project_dir": str(experiment_dir), "depends_on": ["survey"],
                    "estimate_seconds": 1, "deadline_seconds": 20,
                    "bindings": [], "reuse_completed": False,
                    "reuse_output_path": None,
                },
            ],
            "time_policy": {
                "first_result_seconds": 1, "target_seconds": 10,
                "hard_seconds": 60, "checkpoint_seconds": 1,
            },
            "agenda_policy": {"mode": "adaptive"},
            "progression_policy": "forward_first",
            "retry_policy": {"mode": "bounded", "max_attempts": 2, "backoff_seconds": 0},
            "continuation_policy": {"mode": "bounded", "max_cycles": 1},
            "completion": {
                "required_stage_ids": ["survey", "experiment"],
                "release_requires_human": True,
            },
        }

    def test_forward_first_has_bounded_adaptive_runtime(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            validate_workflow(workflow)
            runner = ComposerRunner(workflow)
            try:
                self.assertEqual(runner._retry_policy()["mode"], "bounded")
                self.assertEqual(runner._retry_policy()["max_attempts"], 2)
                self.assertEqual(runner._continuation_policy()["max_cycles"], 1)
                self.assertEqual(runner._agenda_policy(), {"mode": "adaptive"})
                runner.workflow["retry_policy"] = {
                    "mode": "bounded", "max_attempts": 8, "backoff_seconds": 0,
                }
                runner.workflow["continuation_policy"] = {
                    "mode": "bounded", "max_cycles": 8,
                }
                self.assertEqual(runner._retry_policy()["max_attempts"], 2)
                self.assertEqual(runner._continuation_policy()["max_cycles"], 2)
                debt = {
                    "stage_id": "survey", "kind": "survey",
                    "failure_class": "mechanical_contract",
                    "error": "response contract was malformed", "attempts": 2,
                }
                request = runner._forward_debt_work_order("survey", debt)
                self.assertEqual(request["kind"], "literature_expansion")
                self.assertEqual(request["owner"], "research.intelligence")
                self.assertTrue(request["id"].startswith("forward-"))
            finally:
                runner.close()

    def test_pre_execution_failure_is_non_gating_after_composer_admission(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                stage = workflow["stages"][1]
                attempt_dir = root / "experiment-attempt"
                error = ModelWorkBlocked(
                    "capability foundry did not admit a program: model output must contain valid JSON"
                )
                candidate = runner._materialize_forward_progress(
                    stage,
                    {"project_dir": str(attempt_dir)},
                    error,
                    {"status": "blocked", "results_status": "not_executed"},
                    {"reports": [{"role_id": "methodologist", "status": "failed",
                                  "error": "length"}], "usage": {}},
                    [{"attempt_number": 1, "state": "failed"}],
                    force_advance=True,
                )
                self.assertEqual(candidate["composer_decision"], "advance_with_findings")
                self.assertFalse(candidate["release_blocking"])
                self.assertFalse(candidate["failure_debt"]["release_blocking"])
                self.assertEqual(candidate["results_status"], "not_executed")
                self.assertTrue(candidate["backfill_required"])
                self.assertTrue(ComposerRunner._stage_releases_dependencies({
                    "status": candidate["status"],
                    "composer_decision": candidate["composer_decision"],
                    "release_blocking": candidate["release_blocking"],
                    "failure_debt": candidate["failure_debt"],
                }))
            finally:
                runner.close()

    def test_composer_admission_releases_downstream_with_visible_debt(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            attempted = []

            def fake_run_stage(stage, *args, **kwargs):
                attempted.append(stage["id"])
                if stage["id"] == "survey":
                    raise ValidationError("malformed structured response")
                output = Path(stage["project_dir"]) / "output" / "experiment.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("{}")
                return {
                    "status": "completed", "kind": stage["kind"],
                    "output_path": str(output), "usage": {},
                }

            runner._run_stage = fake_run_stage
            runner._run_specialist_pool = lambda *args, **kwargs: {
                "reports": [], "by_role": {}, "usage": {},
            }
            runner._publish_specialist_reports = lambda stage, assignment, bundle: bundle
            runner._run_specialist_verifier = lambda *args, **kwargs: None
            try:
                result = runner.run()
                self.assertGreaterEqual(attempted.count("survey"), 2)
                self.assertIn("experiment", attempted)
                # The first pass must finish the dependent stage before the
                # bounded backfill cycle reopens the failed survey scope.
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertGreaterEqual(attempted.count("experiment"), 2)
                self.assertEqual(runner.continuation_pending_stage_ids, set())
                self.assertEqual(result["status"], "candidate_needs_review")
                survey = runner.context["survey"]
                self.assertEqual(survey["status"], "candidate_needs_review")
                self.assertTrue(survey["forward_progress"])
                self.assertTrue(Path(survey["forward_progress_path"]).is_file())
                self.assertEqual(survey["composer_decision"], "advance_with_findings")
                self.assertFalse(survey["failure_debt"]["release_blocking"])
                self.assertFalse(survey["release_blocking"])
                self.assertTrue(survey["backfill_required"])
                self.assertTrue(any(
                    item.get("action") == "forward_provisional_stage"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()


if __name__ == "__main__":
    unittest.main()
