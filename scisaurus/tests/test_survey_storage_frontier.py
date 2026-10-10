"""Storage continuation and scientific admission are separate boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import StateError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.composer import ComposerRunner
from scisaurus.tests import test_composer


class SurveyStorageFrontierTests(unittest.TestCase):
    def _frontier(self, root):
        control = ControlStore(root)
        store = ArtifactStore(control)
        store.init_project(principal_note="Survey")
        def publish(logical_id, body, author, kind="note"):
            return store.publish_artifact(logical_id=logical_id, body=canonical_bytes(body),
                artifact_type=kind, author=author, media_type="application/json")
        publish("inputs/run-config", {"survey": {"question": "Exact question",
            "proposed_gap": {"id": "topic-active-implementation-hypothesis"}}}, "principal")
        survey = publish("kb/surveys/current", {}, "research.literature-mapper")
        assessment = publish("kb/gap-assessments/current", {"survey_ref": survey["artifact_ref"]},
                             "methods.novelty-verifier")
        for artifact in (survey, assessment):
            store.adopt(artifact["artifact_id"], target_version=1,
                        expected_accepted_version=None, actor="command.controller")
        progress = publish("command/progress/current-1", {
            "cumulative_usage": {"actual": {"model_calls": 3}},
            "next_action": {"decision": "paused"}}, "command.controller", "progress_checkpoint")
        snapshot = {"project_dir": str(root.resolve()), "run_id": "current", "checkpoint": 1,
                    "phase": "paused", "cumulative_usage": {"model_calls": 3}}
        run = {"status": "completed", "survey_current": True, "assessment_current": True,
               "survey_ref": survey["artifact_ref"], "assessment_ref": assessment["artifact_ref"]}
        control.close()
        (root / "output").mkdir()
        (root / "output/run.json").write_text(json.dumps(run))
        (root / "output/progress.json").write_text(json.dumps(snapshot))
        return run, snapshot, progress

    def test_stale_completed_report_retains_newer_verified_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, snapshot, progress = self._frontier(root)
            original = (root / "output/run.json").read_bytes()
            with patch.object(ComposerRunner, "_survey_references_are_current", return_value=False):
                frontier = ComposerRunner._survey_storage_frontier(root, run)
                self.assertEqual(frontier["producer_checkpoint"]["ref"], progress["artifact_ref"])
                self.assertEqual(ComposerRunner._survey_partial_checkpoint(root), frontier)
                self.assertNotIn("survey_current", frontier)
                self.assertNotIn("assessment_ref", frontier)
                self.assertEqual(frontier["question"], "Exact question")
                (root / "output/progress.json").write_text(json.dumps({**snapshot,
                    "cumulative_usage": {"model_calls": 0}}))
                with self.assertRaisesRegex(StateError, "verified producer checkpoint"):
                    ComposerRunner._survey_storage_frontier(root, run)
            self.assertEqual((root / "output/run.json").read_bytes(), original)

    def test_current_report_and_foreign_refs_are_not_storage_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, _, _ = self._frontier(root)
            with patch.object(ComposerRunner, "_survey_references_are_current", return_value=True):
                self.assertIsNone(ComposerRunner._survey_storage_frontier(root, run))
            with patch.object(ComposerRunner, "_survey_references_are_current", return_value=False):
                with self.assertRaisesRegex(StateError, "integrity failed"):
                    ComposerRunner._survey_storage_frontier(root, {**run,
                        "assessment_ref": "artifact:kb/gap-assessments/current@2"})
                with self.assertRaisesRegex(StateError, "foreign accepted references"):
                    ComposerRunner._survey_storage_frontier(root, {**run,
                        "assessment_ref": run["survey_ref"]})

    def test_anchored_storage_precedes_older_pending_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workflow = test_composer.ComposerWorkflowTests()._workflow(root)
            runner = ComposerRunner(workflow)
            self.addCleanup(runner.close)
            stage = runner.workflow["stages"][0]
            runner.workflow["stages"].append({**deepcopy(stage), "id": "topic", "kind": "topic_discovery"})
            stage["depends_on"] = ["topic"]
            runner.reopened_stage_ids = {stage["id"]}
            runner.context["topic"] = {"topic": {"id": "active", "research_question": "Exact question"}}
            current, prior = root / "current", root / "prior"
            run, _, _ = self._frontier(current)
            self._frontier(prior)
            (prior / "output/run.json").write_text(json.dumps({"status": "blocked"}))
            for project in (current, prior):
                runner.tasks.create(project.name, "production", {}, "command.composer")
                runner.tasks.transition(project.name, "queued", "command.composer")
                runner.tasks.start_attempt(project.name, project.name + "-attempt", owner="command.composer",
                    lease_ttl_seconds=60, payload={"stage_id": stage["id"], "project_dir": str(project)})
            runner.stage_records[stage["id"]] = {"status": "retrying", "project_dir": str(prior),
                "attempt_id": "prior-attempt", "topic_id": "active", "topic_cycle": 0,
                "attempts": [{"project_dir": str(current), "attempt_id": "current-attempt",
                              "topic_id": "active", "topic_cycle": 0, "state": "succeeded"}]}
            runner.context[stage["id"]] = {**run, "project_dir": str(current), "topic_id": "active", "topic_cycle": 0}
            with patch.object(runner, "_current_topic_identity", return_value={"topic_id": "active", "topic_cycle": 0}), \
                    patch.object(ComposerRunner, "_survey_references_are_current", return_value=False):
                self.assertEqual(runner._latest_resumable_survey_project(stage), current)
                runner.context["topic"]["topic"]["research_question"] = "Different question"
                self.assertIsNone(runner._latest_resumable_survey_project(stage))
            runner.context["topic"]["topic"]["research_question"] = "Exact question"
            with patch.object(runner, "_current_topic_identity", return_value={"topic_id": "active", "topic_cycle": 0}), \
                    patch.object(ComposerRunner, "_survey_references_are_current", return_value=True):
                self.assertEqual(runner._latest_resumable_survey_project(stage), prior)
