import hashlib
import json
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.errors import QuotaExceededError, ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.models import ModelCallError, ModelGenerationCapacityError, ModelResult
from scisaurus.runtime.specialists import SpecialistDispatcher, build_verifier_prompt


class GenerationCapacityTests(unittest.TestCase):
    def assignment(self):
        return {"assigned_role": "methods.methodologist", "role_id": "methodologist",
                "model_role": "methods.methodologist", "execution_kind": "model",
                "stage_id": "repair", "stage_kind": "experiment",
                "quota": {"max_calls": 3, "max_output_tokens": 98304,
                          "max_output_tokens_per_call": 32768, "max_seconds": 5},
                "_prompt": json.dumps({"objective": "review supplied solver evidence"})}

    def test_empty_length_is_paid_capacity_failure_without_retry(self):
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                 "model": "fixture", "timeout_seconds": 5, "max_output_tokens": 32768}
        for text in ("", " \n "):
            for verifier in (False, True):
                with self.subTest(text=text, verifier=verifier), \
                        patch("scisaurus.runtime.specialists.ModelClient") as client:
                    usage = {"model_calls": 1, "input_tokens": 183000, "output_tokens": 32768}
                    metadata = {"thinking_bytes": 18000, "wire_reasoning": "low"}
                    client.return_value.complete.return_value = ModelResult(
                        text, "fixture", usage, .1, "length", 1, metadata)
                    result = SpecialistDispatcher(model, max_parallel=1,
                        deadline=time.monotonic()+10).dispatch([self.assignment()], {}, verifier=verifier)[0]
                self.assertEqual(client.return_value.complete.call_count, 1)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["usage"], usage)
                self.assertEqual(result["validation_retries"], 0)
                self.assertEqual(result["retry_history"], [])
                error = ModelCallError.from_failure(result["error"], result["failure"])
                self.assertIsInstance(error, QuotaExceededError)
                self.assertEqual(error.dimension, "generation_output_tokens")
                self.assertEqual(error.observed, 32768)
                proof = error.generation_capacity
                self.assertEqual(proof["response_metadata"], metadata)
                self.assertEqual(proof["response_sha256"], hashlib.sha256(text.encode()).hexdigest())
                self.assertEqual(proof["input_sha256"], result["request_inputs"][0]["input_sha256"])
                self.assertEqual(result["request_inputs"][0]["response_metadata"], metadata)

    def test_capacity_roundtrip_rejects_unknown_or_invalid_proof(self):
        proof = {"finish_reason": "length", "answer_chars": 0, "limit": 32768,
                 "observed": 32768, "input_sha256": "a"*64, "response_sha256": "b"*64}
        original = ModelGenerationCapacityError("exhausted", generation_capacity=proof, attempts=1)
        original.usage = {"model_calls": 1, "output_tokens": 32768}
        self.assertEqual(ModelCallError.from_failure(str(original), original.failure_details()).failure_details(),
                         original.failure_details())
        for field, value in (("finish_reason", "stop"), ("answer_chars", 1), ("answer_chars", False),
                             ("input_sha256", "bad"), ("limit", 0)):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                ModelGenerationCapacityError("bad", generation_capacity={**proof, field: value}, attempts=1)
        with self.assertRaises(ValidationError):
            ModelCallError.from_failure("unknown", {**original.failure_details(), "outcome_known": False})

    def test_empty_continuation_does_not_create_another_generation(self):
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                 "model": "fixture", "timeout_seconds": 5, "max_output_tokens": 32768}
        prefix = '{"decision":"repair"'
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = [
                ModelResult(prefix, "fixture", {"model_calls": 1, "output_tokens": 10}, .1, "length", 1),
                ModelResult("", "fixture", {"model_calls": 1, "output_tokens": 32768}, .1, "length", 1)]
            result = SpecialistDispatcher(model, max_parallel=1, deadline=time.monotonic()+10).dispatch(
                [self.assignment()], {})[0]
        self.assertEqual(client.return_value.complete.call_count, 2)
        self.assertEqual(result["partial_response"], prefix)
        self.assertEqual(result["usage"], {"model_calls": 2, "output_tokens": 32778})
        self.assertEqual(result["failure"]["generation_capacity"]["answer_chars"], 0)

    def test_methods_evidence_capacity_receipt_cannot_replenish_calls(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        fixture = ComposerWorkflowTests()
        for failed_role in ("producer", "verifier"):
            with self.subTest(failed_role=failed_role), tempfile.TemporaryDirectory() as path:
                runner = ComposerRunner(fixture._workflow(Path(path)))
                try:
                    topic, _, _, current, _ = fixture._repair_subject_fixture(runner)
                    runner.context["topic"] = {"kind": "topic_discovery", "topic": topic}
                    stage = runner.workflow["stages"][1]
                    descriptor = {"model": {"protocol": "openai_compatible", "base_url": "http://fake/v1",
                        "model": "fake", "max_input_tokens": 24000, "max_output_tokens": 4000,
                        "context_window_tokens": 32000, "timeout_seconds": 5}}
                    action = "Produce a bounded source-bound derivation note."
                    calls = []

                    def complete(*, system, prompt):
                        value = json.loads(prompt)
                        calls.append(value)
                        is_note_author = "repair_evidence_request" in value
                        is_note_review = "repair_evidence_note" in value.get("chief_result", {})
                        if ((failed_role == "producer" and is_note_author)
                                or (failed_role == "verifier" and is_note_review)):
                            return ModelResult("", "fake", {"model_calls": 1, "output_tokens": 4000}, .1, "length", 1)
                        if is_note_author:
                            response = {"decision": "pass", "summary": "bounded note", "findings": [],
                                "evidence_gaps": [], "requested_actions": [], "evidence_note": {
                                    "title": "Analytic note", "content": "Bounded source argument.",
                                    "source_refs": ["candidate_program.executor"], "limitations": ["No execution."],
                                    "action_disposition": "fulfilled"}}
                        elif system.startswith("You are an independent adversarial verifier"):
                            response = {"decision": "hold", "rationale": "Primary design unresolved.",
                                        "critical_findings": ["Source-bound gap."], "repair_scope": [action]}
                        else:
                            response = {"decision": "hold" if "repair_adjudication_packet" in value else "repair",
                                "summary": "Design unresolved.", "findings": ["Source-bound gap."],
                                "evidence_gaps": [], "requested_actions": [action], "repair_plan": None}
                        return ModelResult(json.dumps(response), "fake", {"model_calls": 1}, .1, "stop", 1)

                    with patch("scisaurus.runtime.specialists.ModelClient") as client:
                        client.return_value.complete.side_effect = complete
                        first = runner._run_capability_repair_panel(stage, descriptor, runner.context["topic"],
                            {"kind": "experiment", "failure_dossier_ref": current["artifact_ref"], "attempt_number": 2}, "hold")
                        dependency = runner._run_capability_repair_evidence(stage, descriptor, first["packet"], first, {})
                        before = len(calls)
                        retained = runner._run_capability_repair_evidence(stage, descriptor, first["packet"], first, {})
                        self.assertEqual(len(calls), before)
                        self.assertEqual(retained["artifact_ref"], dependency["artifact_ref"])
                        self.assertEqual(retained["dispatch_usage"], {})
                        self.assertEqual(retained["status"], "blocked")
                        self.assertTrue(retained["model_failure"]["failure"]["generation_capacity"])
                finally:
                    runner.close()

    def test_cached_capacity_is_not_replenished_by_outer_attempt(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        proof = {"finish_reason": "length", "answer_chars": 0, "limit": 32768,
                 "observed": 32768, "input_sha256": "a"*64, "response_sha256": "b"*64}
        error = ModelGenerationCapacityError("exhausted", generation_capacity=proof, attempts=1)

        class Dispatcher:
            model_config = {"model": "fixture", "max_output_tokens": 32768}
            provider_cooldowns = {}
            calls = 0

            def dispatch(self, assignments, packet, *, verifier, on_result):
                if not assignments:
                    return []
                self.calls += 1
                report = {"role_id": assignments[0]["role_id"], "status": "failed",
                          "error_type": "ModelGenerationCapacityError", "error": str(error),
                          "failure": error.failure_details(), "usage": {"model_calls": 1, "output_tokens": 32768}}
                on_result(report)
                return [report]

        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(ComposerWorkflowTests()._workflow(Path(path)))
            try:
                dispatcher = Dispatcher()
                first = runner._dispatch_specialist_work(dispatcher, [self.assignment()], {})
                again = runner._dispatch_specialist_work(dispatcher, [{**self.assignment(), "attempt_number": 99}], {})
                self.assertEqual(dispatcher.calls, 1)
                self.assertEqual(first[0]["usage"]["model_calls"], 1)
                self.assertEqual(again[0]["usage"], {})
                restored = ModelCallError.from_failure(again[0]["error"], again[0]["failure"])
                self.assertFalse(runner._should_run_failure_specialist_review(restored, {"kind": "experiment"}))
                dispatcher.model_config = {**dispatcher.model_config, "max_output_tokens": 65536}
                runner._dispatch_specialist_work(dispatcher, [self.assignment()], {})
                self.assertEqual(dispatcher.calls, 2)
                runner._dispatch_specialist_work(dispatcher, [{**self.assignment(), "_prompt": "changed evidence"}], {})
                self.assertEqual(dispatcher.calls, 3)
            finally:
                runner.close()

    def test_review_projection_retains_science_and_exact_source_hashes(self):
        request = {"topic": {"research_question": "declared question"},
                   "evidence_catalog": [{"source_ref": "source", "title": "title", "work_id": "W1",
                                         "origin_lineage": {"events": ["retrieval"]}}],
                   "computation_scope": {"work_orders": [{"objective": "periodic matched stiffness",
                        "experiment_repair_plan": {"lineage": {"cycle": 17}, "required_changes": ["exact physical correction"]}}]},
                   "engineering_history": {"operations": [{"receipt_ref": "prior", "error": "actual failure"}]}}
        raw = {"source": "exact solver wrapper", "input": {"fraction": .25}, "output": {"realized": .234375}}
        evidence = {"request": request, "selected_operations": [raw], "discovery_and_diagnostics": [raw],
                    "selection": {"strategy": "reuse"}}
        before = deepcopy(evidence)
        prompt = json.loads(build_verifier_prompt({"id": "software", "kind": "experiment"},
            {"repair_verification_scope": "scientific_software_fitness"}, [], {"software_assessment": evidence}))
        chief = prompt["chief_result"]
        projected = chief["software_assessment"]
        self.assertEqual(evidence, before)
        self.assertEqual(projected["selected_operations"], [raw])
        self.assertEqual(projected["discovery_and_diagnostics"], [raw])
        self.assertEqual(projected["request"]["topic"], request["topic"])
        self.assertEqual(projected["request"]["engineering_history"], request["engineering_history"])
        self.assertNotIn("origin_lineage", projected["request"]["evidence_catalog"][0])
        self.assertEqual(chief["software_assessment_sha256"], hashlib.sha256(canonical_bytes(evidence)).hexdigest())
        subject = prompt["verifier_contract"]["review_subject"]
        self.assertEqual(subject["sha256"], hashlib.sha256(canonical_bytes(projected)).hexdigest())
        self.assertEqual(subject["source_sha256"], chief["software_assessment_sha256"])
