import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scisaurus.runtime.composer import ComposerRunner, validate_workflow
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
