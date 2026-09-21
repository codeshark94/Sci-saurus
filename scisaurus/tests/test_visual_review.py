"""Multimodal visual-review workflow and acceptance boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.visual_review import (VERIFY_CHECKS, VisualReviewRunner,
                                              validate_assessment)
from scisaurus.runtime.visual_review_config import (load_visual_review_config,
                                                      validate_visual_review_config)


def visual_config(asset_path, *, objective="Assess the supplied figure at its delivery size."):
    return {
        "live_dispatch_allowed": True,
        "data_classification": "public",
        "allocation_mode": "capacity_pool",
        "project_id": "visual-fixture",
        "objective": objective,
        "supplied_context": "The image is a synthetic public layout fixture. No scientific conclusion is supplied.",
        "model": {"base_url": "http://127.0.0.1:1/v1", "model": "fixture-model",
                  "protocol": "openai_compatible", "timeout_seconds": 2,
                  "max_output_tokens": 4096, "output_format": "json_object"},
        "limits": {"max_rounds": 2, "max_result_bytes": 2_000_000,
                   "concurrent_calls": 3, "wall_clock_seconds": 30,
                   "checkpoint_seconds": 0.1},
        "time_policy": {"first_result_seconds": 20, "target_seconds": 25, "hard_seconds": 30},
        "visual_review": {
            "id": "synthetic-layout", "revision": 1, "mode": "academic_figure",
            "target_medium": "single-column PDF at final reading size",
            "intended_audience": "scientific readers",
            "assets": [{"id": "subject", "path": str(Path(asset_path).resolve()),
                        "media_type": "image/png", "role": "rendered", "label": "Rendered figure"}],
            "criteria": [
                {"id": "legibility", "requirement": "Text and marks remain legible at delivery size."},
                {"id": "alignment", "requirement": "Edges, panels, gutters, and labels align consistently."},
            ],
            "perspectives": [
                {"id": "information", "focus": "Information hierarchy, legibility, and scientific fidelity."},
                {"id": "composition", "focus": "Grid, spacing, balance, and visual rhythm."},
            ],
            "stage_seconds": {"setup": 1, "supervision": 1, "production": 1, "unit_review": 1,
                              "integrated_review": 1, "reassessment": 1},
        },
    }


def _checks(asset_id="subject"):
    return [{"criterion_id": criterion, "outcome": "passed", "asset_ids": [asset_id],
             "location": "entire rendered frame", "evidence": "The visible synthetic frame is consistent.",
             "impact": "No material reading defect is visible.",
             "recommendation": "Preserve the current geometry."}
            for criterion in ("legibility", "alignment")]


def simulated_visual_worker(kind, params, channel):
    assert kind == "model"
    assert len(params["images"]) == 1
    descriptor = params["images"][0]
    assert Path(descriptor["path"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assignment = json.loads(params["prompt"])
    phase = assignment["phase"]
    if phase == "visual_perspective_review":
        value = {"perspective_id": assignment["perspective"]["id"], "decision": "accept",
                 "summary": "The rendered fixture is visually coherent for the stated medium.",
                 "checks": _checks(),
                 "strengths": [{"asset_ids": ["subject"], "observation": "The frame is balanced.",
                                "evidence": "Visible margins are consistent around the subject."}],
                 "issues": [], "uncertainties": []}
    elif phase == "visual_assessment_synthesis":
        value = {"schema_version": "visual-assessment-1", "review_id": assignment["review_id"],
                 "decision": "accept", "summary": "Both perspectives support the current composition.",
                 "asset_refs": assignment["asset_refs_exact"],
                 "perspective_review_refs": assignment["perspective_review_refs_exact"],
                 "criteria": [{"criterion_id": criterion, "outcome": "passed", "asset_ids": ["subject"],
                               "evidence": "Both reviewers identified consistent visible geometry.",
                               "perspective_outcomes": [
                                   {"perspective_id": review["perspective_id"],
                                    "outcome": next(item["outcome"] for item in review["checks"]
                                                    if item["criterion_id"] == criterion)}
                                   for review in assignment["perspective_reviews"]]}
                              for criterion in ("legibility", "alignment")],
                 "strengths": [{"asset_ids": ["subject"], "observation": "The layout is balanced.",
                                "evidence": "Visible margins and alignment are consistent."}],
                 "issues": [], "prioritized_actions": [], "uncertainties": []}
    else:
        assert phase == "visual_assessment_verification"
        fail = "fail verification" in assignment["objective"]
        value = {"assessment_ref": assignment["assessment_ref"],
                 "checks": [{"check_id": check, "outcome": "failed" if fail and index == 0 else "passed",
                             "method": "Compared the exact assessment with the attached image.",
                             "result": "The assessment is grounded." if not (fail and index == 0)
                             else "The assessment is not grounded."}
                            for index, check in enumerate(sorted(VERIFY_CHECKS))],
                 "rationale": "The exact report was checked against the pinned visual input."}
    channel.put({"ok": True, "result": {"text": json.dumps(value), "model": "fixture-model",
                                          "usage": {"model_calls": 1, "input_tokens": 10,
                                                    "output_tokens": 20},
                                          "elapsed_seconds": 0.01, "finish_reason": "stop"}})


def simulated_visual_repair_worker(kind, params, channel):
    assignment = json.loads(params["prompt"])
    if (assignment["phase"] == "visual_perspective_review"
            and assignment["perspective"]["id"] == "composition"
            and "validation_feedback" not in assignment):
        value = {"perspective_id": "composition", "decision": "accept",
                 "summary": "The fixture is coherent.", "checks": _checks(), "strengths": [],
                 "issues": [], "uncertainties": [], "perspective": assignment["perspective"]}
        channel.put({"ok": True, "result": {"text": json.dumps(value), "model": "fixture-model",
                                              "usage": {"model_calls": 1, "input_tokens": 10,
                                                        "output_tokens": 20},
                                              "elapsed_seconds": 0.01, "finish_reason": "stop"}})
        return
    simulated_visual_worker(kind, params, channel)


class TestVisualReviewConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "figure.png"
        self.path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
                              + (10).to_bytes(4, "big") + (20).to_bytes(4, "big"))

    def test_requires_multiple_perspectives_and_supported_image_roles(self):
        value = visual_config(self.path)
        validated = validate_visual_review_config(value)
        validated["visual_review"]["criteria"][0]["requirement"] = "changed"
        self.assertNotEqual(validated, value)
        value = visual_config(self.path)
        value["visual_review"]["perspectives"] = value["visual_review"]["perspectives"][:1]
        with self.assertRaises(ValidationError):
            validate_visual_review_config(value)
        value = visual_config(self.path)
        value["visual_review"]["assets"][0]["role"] = "decoration"
        with self.assertRaises(ValidationError):
            validate_visual_review_config(value)

    def test_rejects_non_multimodal_protocol_before_asset_capture(self):
        value = visual_config(self.path)
        value["model"].update(protocol="ollama", reasoning_effort=None, output_format=None)
        with self.assertRaisesRegex(ValidationError, "openai_compatible multimodal"):
            validate_visual_review_config(value)

    def test_synthesis_cannot_rewrite_a_perspective_outcome_as_consensus(self):
        reviews = [
            {"perspective_id": "information", "checks": [
                {"criterion_id": "legibility", "outcome": "passed"}]},
            {"perspective_id": "composition", "checks": [
                {"criterion_id": "legibility", "outcome": "failed"}]},
        ]
        assessment = {
            "schema_version": "visual-assessment-1", "review_id": "synthetic-layout",
            "decision": "insufficient_evidence", "summary": "The reviewers disagree.",
            "asset_refs": ["artifact:inputs/visual-assets/subject@1"],
            "perspective_review_refs": ["artifact:review/information@1",
                                        "artifact:review/composition@1"],
            "criteria": [{"criterion_id": "legibility", "outcome": "failed",
                           "asset_ids": ["subject"], "evidence": "One reviewer found a defect.",
                           "perspective_outcomes": [
                               {"perspective_id": "information", "outcome": "passed"},
                               {"perspective_id": "composition", "outcome": "passed"}]}],
            "strengths": [], "issues": [], "prioritized_actions": [], "uncertainties": [],
        }
        with self.assertRaisesRegex(ValidationError, "copy each perspective outcome exactly"):
            validate_assessment(
                assessment, "synthetic-layout", {"legibility"}, {"subject"},
                assessment["asset_refs"], assessment["perspective_review_refs"], reviews)

        assessment["criteria"][0]["perspective_outcomes"][1]["outcome"] = "failed"
        assessment["criteria"][0]["outcome"] = "passed"
        with self.assertRaisesRegex(ValidationError, "cannot pass an unresolved"):
            validate_assessment(
                assessment, "synthetic-layout", {"legibility"}, {"subject"},
                assessment["asset_refs"], assessment["perspective_review_refs"], reviews)

    def test_loader_rejects_unreadable_or_malformed_json(self):
        missing = Path(self.temp.name) / "missing.json"
        with self.assertRaisesRegex(ValidationError, "readable JSON"):
            load_visual_review_config(missing)
        malformed = Path(self.temp.name) / "malformed.json"
        malformed.write_text("{")
        with self.assertRaisesRegex(ValidationError, "readable JSON"):
            load_visual_review_config(malformed)
        valid = Path(self.temp.name) / "valid.json"
        source = visual_config(self.path)
        valid.write_text(json.dumps(source))
        loaded = load_visual_review_config(valid)
        self.assertEqual(loaded, source)


class TestVisualReviewRunner(unittest.TestCase):
    def test_process_stop_is_exported_without_retry(self):
        runner = VisualReviewRunner(self.root / 'interrupted-run', visual_config(self.path))
        with patch.object(runner, '_perspective_reviews', side_effect=KeyboardInterrupt('termination requested')):
            result = runner.run()
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(result['failure'], {'kind': 'process_interrupted'})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "figure.png"
        self.path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
                              + (640).to_bytes(4, "big") + (480).to_bytes(4, "big"))

    def run_fixture(self, objective="Assess the supplied figure at its delivery size."):
        runner = VisualReviewRunner(self.root / "run", visual_config(self.path, objective=objective))
        runner.worker_target = simulated_visual_worker
        return runner.run()

    def test_independent_multimodal_reviews_are_verified_before_acceptance(self):
        result = self.run_fixture()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["decision"], "accept")
        self.assertTrue(result["assessment_current"])
        self.assertEqual(len(result["perspective_review_refs"]), 2)
        control = ControlStore(self.root / "run")
        self.addCleanup(control.close)
        store = ArtifactStore(control)
        accepted = store.accepted("editorial/visual-assessments/synthetic-layout")
        self.assertEqual(accepted["artifact_ref"], result["assessment_ref"])
        context_record = next(store.get(row[0]) for row in control._conn.execute(
            "SELECT artifact_ref FROM artifacts WHERE logical_id LIKE 'command/contexts/visual-synthesis-%'"))
        context = json.loads(store.read_body(context_record["body_hash"]))
        self.assertEqual(context["images"][0]["sha256"], store.get(result["asset_refs"][0])["body_hash"])
        self.assertNotIn("base64", json.dumps(context))
        self.assertTrue((self.root / "run/output/visual-assessment.md").is_file())

    def test_failed_independent_verification_keeps_assessment_unaccepted(self):
        result = self.run_fixture("Assess and fail verification for the fixture.")
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["assessment_ref"])
        control = ControlStore(self.root / "run")
        self.addCleanup(control.close)
        self.assertIsNone(ArtifactStore(control).accepted("editorial/visual-assessments/synthetic-layout"))

    def test_only_schema_rejected_perspective_is_retried(self):
        runner = VisualReviewRunner(self.root / "run", visual_config(self.path))
        runner.worker_target = simulated_visual_repair_worker
        result = runner.run()
        self.assertEqual(result["status"], "completed", result)
        control = ControlStore(self.root / "run")
        self.addCleanup(control.close)
        contexts = [row[0] for row in control._conn.execute(
            "SELECT logical_id FROM artifacts WHERE logical_id LIKE 'command/contexts/visual-review-%'")]
        self.assertEqual(sum("review-information" in item for item in contexts), 1)
        self.assertEqual(sum("review-composition" in item for item in contexts), 2)
        validation = control._conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE logical_id LIKE 'command/validation/visual-review-composition-%'"
        ).fetchone()[0]
        self.assertEqual(validation, 1)


if __name__ == "__main__":
    unittest.main()
