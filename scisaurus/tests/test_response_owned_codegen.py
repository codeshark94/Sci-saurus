"""Offline regressions for response identity, repair scope and scientific claims."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_foundry import (
    _author_response_format_failure_signature, _author_prefix_contract_error,
    candidate_prompt, program_review_evidence,
)
from scisaurus.runtime.measurement_contract import validate_model_definition, model_definition_contract
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.software_workbench import software_computation_identity, software_assessment_prompt, selection_contract
from scisaurus.runtime.specialists import SpecialistDispatcher, retained_software_response_failure_identity
from scisaurus.tests import test_capability_foundry as fixtures
from scisaurus.tests import test_harness_recovery as measurement_fixtures


class ResponseOwnedCodegenTests(unittest.TestCase):
    def test_interrupted_suffix_retains_known_prefix_and_unknown_dispatch_across_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixtures.CapabilityFoundryTests()
            self.addCleanup(helper.doCleanups)
            foundry, cache = helper._foundry(root), helper._cache(root)
            complete = json.dumps(helper._payload())
            prefix = complete[:-80]
            prompts = []

            class Author:
                model = "author"
                max_output_tokens = 24000
                reasoning_effort = "none"
                output_format = "json_object"

                def complete(self, *, system, prompt):
                    prompts.append(json.loads(prompt))
                    texts = [prefix, complete[len(prefix):len(prefix)+30], complete]
                    index = len(prompts)-1
                    return ModelResult(texts[index], self.model, {"model_calls": 1}, 0,
                                       "length" if index < 2 else "stop")

            class Interruption(RuntimeError):
                pass

            def stop(phase, state):
                if phase == "author_response_continuation_calling" and len(state["requests"]) == 3:
                    raise Interruption()

            author = Author()
            with self.assertRaises(Interruption):
                foundry.generate("bounded comparison", client=author, work_cache=cache, on_progress=stop)
            original = cache.entries()[0]
            result = foundry.generate("bounded comparison", client=author, work_cache=cache,
                                      resume_work_ref=original["cache_ref"])
            self.assertEqual(result["status"], "registered")
            current = next(row for row in cache.entries() if row["status"] == "succeeded")
            self.assertEqual(current["requests"][:3], original["requests"])
            receipt = current["request_outcome_reconciliations"][0]
            self.assertEqual(receipt["source_ref"], original["cache_ref"])
            self.assertEqual(receipt["request_index"], 2)
            self.assertEqual(receipt["status"], "result_unknown")
            self.assertEqual(len(prompts), 3)
            self.assertNotIn("partial_response", prompts[-1])

    def test_prefix_schema_guard_preserves_nested_fields_and_source_strings(self):
        for text in [
            '{"experiment_intent":{"extra":"allowed inside intent"},"executor_source":"incomplete',
            '{"executor_source":"metadata \\"extra\\": inside source", "experiment_intent":{',
            '{"updates":{"executor_source":{"edits":[{"old":"x","new":"y"}]}}}',
            '{"experiment_intent":{},"executor_source":"x","runtime":{},"test_input":{',
        ]:
            with self.subTest(text=text):
                self.assertIsNone(_author_prefix_contract_error(text))
        self.assertIn("unsupported top-level field", _author_prefix_contract_error(
            '{"experiment_intent":{},"executor_source":"x","executor_source_metadata":'))
        self.assertIn("duplicate top-level field", _author_prefix_contract_error(
            '{"experiment_intent":{},"executor_source":"x","executor_source":'))

    def test_irreversible_suffix_routes_to_format_repair_without_another_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixtures.CapabilityFoundryTests()
            self.addCleanup(helper.doCleanups)
            foundry = helper._foundry(root)
            cache = helper._cache(root)
            complete = json.dumps(helper._payload())
            prefix = complete[:-40]
            responses = [prefix, complete[len(prefix):-1] + ', "executor_source_metadata":', complete]
            prompts = []

            class Author:
                model = "author"
                max_output_tokens = 24000
                reasoning_effort = "none"
                output_format = "json_object"

                def complete(self, *, system, prompt):
                    prompts.append(json.loads(prompt))
                    i = len(prompts) - 1
                    return ModelResult(responses[i], self.model, {"model_calls": 1}, 0,
                                       "length" if i < 2 else "stop")

            result = foundry.generate("bounded comparison", client=Author(), work_cache=cache)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(len(prompts), 3)
            self.assertIn("original_response_contract", prompts[1])
            self.assertIn("output_contract", prompts[1]["original_response_contract"])
            self.assertIn("unsupported top-level field", prompts[2]["format_repair"]["previous_error"])
            state = cache.entries()[0]
            self.assertEqual(sum(r.get("operation") == "continue_truncated_response"
                                 for r in state["requests"]), 1)

    def test_empty_malformed_and_truncated_responses_have_distinct_failures(self):
        empty = _author_response_format_failure_signature(None, "length", text="")
        extra = _author_response_format_failure_signature(None, "stop", text='{"a":1}}')
        truncated = _author_response_format_failure_signature(None, "length", text='{"source":"abc')
        self.assertEqual(len({empty, extra, truncated}), 3)
        self.assertEqual(extra, _author_response_format_failure_signature(None, "stop", text='{"b":2}}'))

    def test_empty_response_recovery_survives_malformed_peer_and_new_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixtures.CapabilityFoundryTests()
            self.addCleanup(helper.doCleanups)
            foundry = helper._foundry(root)
            foundry.max_attempts = 5
            cache = helper._cache(root)
            complete = json.dumps(helper._payload())
            prefix = complete[:-40]
            efforts, prompts = [], []

            class Author:
                model = "author"
                reasoning_effort = "medium"
                max_output_tokens = 24000
                output_format = "json_object"

                def complete(self, *, system, prompt):
                    efforts.append(self.reasoning_effort)
                    prompts.append(json.loads(prompt))
                    texts = ["", '{"bad":1}}', prefix, complete[len(prefix):]]
                    index = len(efforts) - 1
                    text = texts[index]
                    return ModelResult(text, self.model, {"model_calls": 1}, 0,
                        "length" if index in {0, 2} else "stop", response_metadata={
                            "reasoning_effort": self.reasoning_effort, "answer_bytes": len(text.encode()),
                        })

            result = foundry.generate("bounded comparison", client=Author(), work_cache=cache)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(efforts, ["medium", "none", "none", "none"])
            self.assertEqual(prompts[-1]["partial_response"], prefix)
            state = cache.entries()[0]
            self.assertEqual(len(state["retired_author_response_continuations"]), 1)
            self.assertEqual(state["author_response_continuation"]["status"], "completed")
            self.assertEqual(len([row for row in state["requests"] if row.get("operation") == "continue_truncated_response"]), 1)

    def test_interface_description_change_preserves_owned_partial_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = fixtures.CapabilityFoundryTests()
            self.addCleanup(helper.doCleanups)
            foundry, cache = helper._foundry(root), helper._cache(root)
            complete = json.dumps(helper._payload())
            prefix = complete[:-40]
            calls = []

            class Author:
                model = "author"
                max_output_tokens = 24000
                output_format = "json_object"

                def complete(self, *, system, prompt):
                    calls.append(json.loads(prompt))
                    return ModelResult(prefix if len(calls) == 1 else complete[len(prefix):],
                                       self.model, {"model_calls": 1}, 0,
                                       "length" if len(calls) == 1 else "stop")

            class Interruption(RuntimeError):
                pass

            def stop(phase, state):
                if phase == "author_response_continuation_pending":
                    raise Interruption()

            author = Author()
            with self.assertRaises(Interruption):
                foundry.generate("bounded comparison", test_input={"probe": True}, client=author, work_cache=cache, on_progress=stop)
            reference = cache.entries()[0]["cache_ref"]
            with self.assertRaises(ValidationError):
                foundry.generate("bounded comparison", test_input={"probe": 1}, client=author,
                                 work_cache=cache, resume_work_ref=reference)
            self.assertEqual(len(calls), 1)
            def updated(*args, **kwargs):
                prompt = candidate_prompt(*args, **kwargs)
                prompt["optional_intent_fields"]["model_definition"]["variables"][0]["id"] += " clarified"
                return prompt
            with patch("scisaurus.runtime.capability_foundry.candidate_prompt", side_effect=updated):
                result = foundry.generate("bounded comparison", test_input={"probe": True}, client=author,
                                          work_cache=cache, resume_work_ref=reference)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[-1]["partial_response"], prefix)

    def test_identifier_contract_reports_every_invalid_path_without_mutation(self):
        definition = measurement_fixtures.model_definition()
        definition["variables"][0]["id"] = "T_C"
        definition["parameters"][0]["id"] = "A"
        before = deepcopy(definition)
        with self.assertRaises(ValidationError) as caught:
            validate_model_definition({"model_definition": definition})
        message = str(caught.exception)
        self.assertIn("/model_definition/variables/0/id='T_C'", message)
        self.assertIn("/model_definition/parameters/0/id='A'", message)
        self.assertIn("[a-z][a-z0-9_-]{0,63}", message)
        self.assertEqual(definition, before)
        self.assertIn("[a-z][a-z0-9_-]{0,63}", model_definition_contract()["variables"][0]["id"])

    def test_software_identity_preserves_science_but_excludes_ownership(self):
        scope = {"work_orders": [{"id": "a", "owner": "methods", "objective": "Measure contrast",
                                  "kind": "analysis_repair", "failure_dossier_ref": "artifact:old"}],
                 "source_data_manifest": {"rows_sha256": "a" * 64}}
        changed = deepcopy(scope)
        changed["work_orders"][0].update(id="b", owner="research", failure_dossier_ref="artifact:new")
        self.assertEqual(software_computation_identity(scope), software_computation_identity(changed))
        changed["work_orders"][0]["objective"] = "Measure sensitivity"
        self.assertNotEqual(software_computation_identity(scope), software_computation_identity(changed))
        changed = deepcopy(scope)
        changed["source_data_manifest"]["rows_sha256"] = "b" * 64
        self.assertNotEqual(software_computation_identity(scope), software_computation_identity(changed))
        request = {"computation_scope": scope, "evidence_catalog": [{"source_ref": "artifact:source",
                   "title": "Basis", "representation": "abstract", "lineage": {"irrelevant": "history"}}]}
        projection = software_assessment_prompt(request)
        self.assertEqual(projection["evidence_catalog"][0]["representation"], "abstract")
        self.assertNotIn("lineage", projection["evidence_catalog"][0])
        self.assertIn("lineage", request["evidence_catalog"][0])

    def test_review_receives_current_threshold_outcome_without_forcing_success(self):
        helper = measurement_fixtures.MeasurementContractTests()
        intent = helper.intent()
        verdict = helper.verdict(intent, 2)
        document = {"observations": [{"x": 1}], "metrics": [
            {"id": row["metric_id"], "value": 2} for row in verdict["metric_recalculations"]]}
        verdict["candidate_sha256"] = hashlib.sha256(canonical_bytes(document)).hexdigest()
        candidate = {"experiment_intent": intent, "executor_source": "executor", "validator_source": "validator",
                     "test_input": {"probe": True}}
        evidence = program_review_evidence(candidate, document, verdict)
        self.assertEqual(evidence["decision_assessments"][0]["outcome"], "not_satisfied")
        self.assertEqual(evidence["independent_validation"]["decision"], "accepted")
        self.assertEqual(evidence["raw_observations"], document["observations"])
        self.assertEqual(evidence["decision_assessments"][0]["candidate_sha256"], verdict["candidate_sha256"])

    def test_retained_schema_and_parse_errors_each_receive_one_owned_repair(self):
        response = selection_contract()
        response["decision"] = "pass"
        response["software_selection"]["strategy"] = "unsupported"
        scenarios = [(json.dumps(response), "ValidationError: scientific software selection has an unsupported decision"),
                     ("invalid JSON", "ValidationError: model output is not a JSON object")]
        for text, error in scenarios:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                dispatcher = SpecialistDispatcher({"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                    "model": "fixture", "timeout_seconds": 10, "max_output_tokens": 1000},
                    deadline=time.monotonic()+60, software_workspace=directory)
                assignment = {"assigned_role": "methods.methodologist", "role_id": "methodologist",
                    "_software_tools": True, "_response_contract": "software_selection",
                    "_prompt": json.dumps({"software_assessment_request": {"source_ref_catalog": []}}),
                    "_response_format_recovery": {"execution_ref": "artifact:failed@1", "previous_text": text, "error": error},
                    "quota": {"max_calls": None, "max_output_tokens": 1000}}
                with patch("scisaurus.runtime.specialists.ModelClient") as client:
                    client.return_value.complete.return_value = ModelResult(text, "fixture", {"model_calls": 1}, 0, "stop")
                    report = dispatcher._execute(assignment, {})
                    self.assertEqual(client.return_value.complete.call_count, 1)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["usage"]["model_calls"], 1)
                self.assertEqual(report["response_format_failure_identity"],
                                 retained_software_response_failure_identity({"partial_response": text, "error": error}))


if __name__ == "__main__":
    unittest.main()
