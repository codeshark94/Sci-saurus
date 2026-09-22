import tempfile
import unittest
from pathlib import Path

from scisaurus.core.errors import ValidationError
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

    def test_failed_stage_becomes_candidate_and_downstream_runs(self):
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
                self.assertIn("experiment", attempted)
                self.assertGreaterEqual(attempted.count("survey"), 2)
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertIn(result["status"], {"candidate_needs_review", "completed"})
                survey = runner.context["survey"]
                self.assertEqual(survey["status"], "candidate_needs_review")
                self.assertTrue(survey["forward_progress"])
                self.assertTrue(Path(survey["forward_progress_path"]).is_file())
                self.assertTrue(survey["failure_debt"]["release_blocking"])
                self.assertTrue(any(
                    item.get("action") == "forward_provisional_stage"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()


if __name__ == "__main__":
    unittest.main()
