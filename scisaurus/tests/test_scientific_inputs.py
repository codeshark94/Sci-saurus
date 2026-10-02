import json
from pathlib import Path
import tempfile
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.scientific_inputs import input_artifact, input_readiness, scientific_topic_sha256


class ScientificInputTests(unittest.TestCase):
    def test_availability_requires_materialized_topic_bound_input_and_captured_dependencies(self):
        with tempfile.TemporaryDirectory() as path:
            control = ControlStore(path); self.addCleanup(control.close)
            store = ArtifactStore(control); store.init_project(principal_note="Input provenance")
            source = store.publish_artifact(logical_id="kb/source", artifact_type="note", author="research.chief",
                body=canonical_bytes({"text": "A captured coefficient is 2."}), media_type="application/json")
            identity = {"id": "selected", "research_question": "Evaluate the stated coefficient.", "scope": "Analytical model", "feasibility_plan": {"evidence_inputs": [{"kind": "analytical_parameters", "status": "available", "source": "Published coefficients"}]}}
            value = {"topic_sha256": scientific_topic_sha256(identity), "schema_version": "scientific-input-1", "topic_id": "selected", "kind": "analytical_parameters",
                     "payload": {"coefficient": 2}, "source_refs": [source["artifact_ref"]]}
            record = store.publish_artifact(logical_id="kb/scientific-inputs/parameters", artifact_type="note", author="research.chief",
                body=canonical_bytes(value), media_type="application/json", inputs=[{"ref": source["artifact_ref"], "purpose": "premise", "required_state": "provisional_allowed"}])
            topic = {**identity, "feasibility_plan": {"evidence_inputs": [{"kind": "analytical_parameters",
                "status": "available", "source": "Published coefficients", "artifact_refs": [record["artifact_ref"]]}]}}
            self.assertEqual(input_readiness(topic, [])[0]["status"], "unverified")
            captured = input_artifact(store, record["artifact_ref"])
            ready = input_readiness(topic, [captured])[0]
            self.assertEqual(ready["status"], "verified")
            self.assertEqual(ready["artifact_hashes"], {record["artifact_ref"]: record["body_hash"]})
            self.assertEqual(input_readiness({**topic, "id": "other"}, [captured])[0]["status"], "unverified")
            for changed in ({"research_question": "Evaluate a different question."}, {"scope": "Empirical observations"}, {"disconfirmation_test_note": "Different numerical null exponent."},
                            {"feasibility_plan": {**topic["feasibility_plan"], "execution_mode": "different_model"}}):
                self.assertEqual(input_readiness({**topic, **changed}, [captured])[0]["status"], "unverified")
            altered = Path(path) / "objects/sha256" / record["body_hash"]
            original = altered.read_bytes(); altered.write_bytes(b'{}')
            with self.assertRaises(ValidationError): input_artifact(store, record["artifact_ref"])
            altered.write_bytes(original)
            malformed = store.publish_artifact(logical_id="kb/scientific-inputs/unbound", artifact_type="note", author="research.chief",
                body=canonical_bytes(value), media_type="application/json")
            with self.assertRaisesRegex(ValidationError, "dependencies"): input_artifact(store, malformed["artifact_ref"])

    def test_declared_available_and_planned_generation_remain_explicit(self):
        topic = {"id": "selected", "feasibility_plan": {"evidence_inputs": [
            {"kind": "analytical_parameters", "status": "available", "source": "Claimed publication"},
            {"kind": "synthetic", "status": "available", "source": "Planned evaluation grid"}]}}
        rows = input_readiness(topic, [])
        self.assertEqual([row["status"] for row in rows], ["unverified", "generation_required"])
        self.assertEqual([row["declared_status"] for row in rows], ["available", "available"])

    def test_methods_receive_only_materialized_inputs_for_the_exact_current_topic(self):
        from unittest.mock import patch
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(ComposerWorkflowTests()._workflow(Path(path)))
            self.addCleanup(runner.close)
            item = {"kind": "analytical_parameters", "status": "available", "source": "Captured coefficient"}
            topic = {"id": "selected", "research_question": "Evaluate the coefficient.",
                     "feasibility_plan": {"evidence_inputs": [item]}}
            context = {"topic": topic, "feasibility_check": {"plan": {"evidence_inputs": [{"source": "Stale plan"}]}}}
            stage = next(row for row in runner.workflow["stages"] if row["kind"] == "experiment")
            with patch.object(runner, "_topic_context_for_stage", return_value=(None, context)):
                unavailable = runner._specialist_experiment_projection(stage, {})
                self.assertEqual(unavailable["available_assets"]["evidence_inputs"], [])
                self.assertEqual(unavailable["available_assets"]["required_inputs"], [item])
                source = runner.store.publish_artifact(logical_id="kb/source", artifact_type="note", author="research.chief",
                    body=canonical_bytes({"text": "The coefficient equals 2."}), media_type="application/json")
                value = {"schema_version": "scientific-input-1", "topic_id": topic["id"],
                    "topic_sha256": scientific_topic_sha256(topic), "kind": item["kind"],
                    "payload": {"coefficient": 2}, "source_refs": [source["artifact_ref"]]}
                record = runner.store.publish_artifact(logical_id="kb/scientific-inputs/parameters", artifact_type="note",
                    author="research.chief", body=canonical_bytes(value), media_type="application/json",
                    inputs=[{"ref": source["artifact_ref"], "purpose": "premise", "required_state": "provisional_allowed"}])
                item["artifact_refs"] = [record["artifact_ref"]]
                available = runner._specialist_experiment_projection(stage, {})
                self.assertEqual(available["available_assets"]["evidence_inputs"], [item])
                self.assertEqual(available["method_constraints"]["input_readiness"][0]["status"], "verified")
                topic["research_question"] = "Evaluate a different coefficient."
                self.assertEqual(runner._specialist_experiment_projection(stage, {})["available_assets"]["evidence_inputs"], [])
