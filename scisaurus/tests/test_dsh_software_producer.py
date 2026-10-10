"""Offline routing and evidence contracts for the DSH software producer."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.schema import canonical_bytes
from scisaurus.core.errors import ValidationError
from scisaurus.runtime.dsh_batch import DshBatchError, sha256
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.software_workbench import SoftwareWorkbench, selection_contract, tool_contract
from scisaurus.runtime.specialists import SpecialistDispatcher


class DshSoftwareProducerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        composition = self.root / "composition.json"
        composition.write_text('{"provider":"offline"}')
        executable = Path(sys.executable).absolute()
        self.backend = {
            "schema_version": "dsh-batch-1", "command": [str(executable)],
            "pinned_files": {str(path): sha256(path) for path in (executable, composition)},
            "read_roots": [str(self.root)], "composition": str(composition),
            "provider": "offline", "model": "dsh-engineer",
            "auth_env": "DSH_OFFLINE_TEST_KEY", "timeout_seconds": 30,
            "max_output_tokens": 2000,
        }
        self.model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "scientific-reviewer", "timeout_seconds": 30,
            "max_input_tokens": 24000, "max_output_tokens": 2000,
        }
        self.prompt = {
            "question": "Is the declared solver applicable?",
            "scientific_software_tools": tool_contract(),
            "software_assessment_request": {"evidence_catalog": []},
            "author_backend": {"internal": "controller configuration"},
        }
        self.assignment = {
            "assigned_role": "methods.methodologist", "role_id": "methodologist",
            "_software_tools": True, "_response_contract": "software_selection",
            "_prompt": json.dumps(self.prompt),
            "quota": {"max_calls": None, "max_input_tokens": 24000,
                      "max_output_tokens": 20000, "max_seconds": 30},
        }
        environment = patch.dict(os.environ, {"SCISAURUS_EXECUTION_POLICY": "operational"})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("SCISAURUS_RUN_CONTROL", "SCISAURUS_RUN_GENERATION"):
            os.environ.pop(name, None)
        check = patch.object(SoftwareWorkbench, "_check_environment", return_value={"offline": True})
        check.start()
        self.addCleanup(check.stop)

    def dispatcher(self, *, deadline=None, laboratory=True, backend=None):
        return SpecialistDispatcher(
            self.model, max_parallel=1,
            deadline=time.monotonic() + 20 if deadline is None else deadline,
            software_workspace=self.root / "software",
            software_laboratory=object() if laboratory else None,
            software_author_backend=self.backend if backend is None else backend,
        )

    @staticmethod
    def final_response():
        response = selection_contract()
        response.update(decision="hold", summary="Solver prerequisites remain unresolved.")
        response["software_selection"].update(strategy="unavailable", rationale="No admitted solver runtime.")
        return response

    def batch_result(self, response, *, usage=None, status="completed"):
        receipt = self.root / ("batch-" + str(len(list(self.root.glob("batch-*.json")))) + ".json")
        receipt.write_text(json.dumps({"status": status}))
        return {"files": {"response.json": json.dumps(response).encode()},
                "usage": usage or {"model_calls": 3, "input_tokens": 41, "output_tokens": 17},
                "receipt": str(receipt), "elapsed_seconds": 0.01}

    def test_controller_tools_continue_inside_one_engineering_session(self):
        turns = []
        action = {"tool_action": {"operation": "search_evidence", "arguments": {"terms": ["solver"]}}}
        def run(task, **kwargs):
            turns.append(json.loads(kwargs["inputs"]["assignment.json"]))
            continuation = kwargs["exchange"]({"response.json": json.dumps(action).encode()},
                                               {"model_calls": 3, "input_tokens": 41, "output_tokens": 17})
            turns.append(json.loads(continuation["inputs"]["assignment.json"]))
            self.assertEqual(turns[0]["runtime_python"], turns[1]["runtime_python"])
            self.assertTrue(Path(turns[1]["runtime_python"]).is_absolute())
            self.assertIsNone(kwargs["exchange"]({"response.json": json.dumps(self.final_response()).encode()},
                                                 {"model_calls": 4, "input_tokens": 51, "output_tokens": 23}))
            return self.batch_result(self.final_response(), usage={"model_calls": 4, "input_tokens": 51, "output_tokens": 23})
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run) as transport, \
                patch("scisaurus.runtime.specialists.ModelClient") as direct:
            report = self.dispatcher().dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(report["usage"], {"model_calls": 4, "input_tokens": 51, "output_tokens": 23})
        self.assertEqual(turns[0]["question"], turns[1]["question"])
        self.assertEqual(turns[1]["software_tool_results"][-1]["action"], action["tool_action"])
        self.assertIn("scientific_source_reference_contract", turns[1])
        self.assertEqual(report["request_inputs"][0]["controller_exchanges"][0]["tool_result"]["receipt_ref"],
                         turns[1]["software_tool_results"][-1]["receipt_ref"])
        self.assertEqual(len(report["software_tool_results"]), 2)
        direct.assert_not_called()

    def test_owned_history_files_follow_controller_and_format_repair_turns(self):
        workbench = SoftwareWorkbench(self.root / "software", deadline=time.monotonic() + 30)
        retained = workbench.execute({"operation": "search_evidence", "arguments": {"terms": ["solver"]}})
        assignment = deepcopy(self.assignment)
        assignment["_software_history_refs"] = [retained["receipt_ref"]]
        seen = []
        def run(task, **kwargs):
            seen.append(kwargs["inputs"])
            index = json.loads(kwargs["inputs"]["engineering-receipts.json"])
            entry, = index["entries"]
            self.assertEqual(entry["receipt_ref"], retained["receipt_ref"])
            self.assertEqual(hashlib.sha256(kwargs["inputs"][entry["path"]]).hexdigest(), entry["body_sha256"])
            self.assertIn("Parse those JSON files locally", task)
            correction = kwargs["exchange"]({"response.json": json.dumps({"response": self.final_response()}).encode()},
                                              {"model_calls": 1, "output_tokens": 10})
            seen.append(correction["inputs"])
            action = {"tool_action": {"operation": "search_evidence", "arguments": {"terms": ["different"]}}}
            continuation = kwargs["exchange"]({"response.json": json.dumps(action).encode()},
                                                {"model_calls": 2, "output_tokens": 20})
            seen.append(continuation["inputs"])
            self.assertIsNone(kwargs["exchange"]({"response.json": json.dumps(self.final_response()).encode()},
                                                  {"model_calls": 3, "output_tokens": 30}))
            return self.batch_result(self.final_response())
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run) as transport:
            report = self.dispatcher().dispatch([assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(transport.call_count, 1)
        for packet in seen[1:]:
            self.assertEqual({key: value for key, value in packet.items() if key.startswith("engineering-receipts")},
                             {key: value for key, value in seen[0].items() if key.startswith("engineering-receipts")})

    def test_receipt_inputs_cannot_replace_scientific_contract(self):
        from scisaurus.runtime.dsh_batch import DshSoftwareProducerClient
        for name in ("assignment.json", "system-contract.txt"):
            with self.subTest(name=name), self.assertRaisesRegex(ValidationError, "cannot replace"):
                DshSoftwareProducerClient(self.backend, root=self.root / "jobs", runtime_python=sys.executable,
                                          receipt_inputs={name: b"{}"})

    def test_cached_history_observation_is_exposed_once_without_new_execution(self):
        workbench = SoftwareWorkbench(self.root / "software", deadline=time.monotonic() + 30)
        action = {"tool_action": {"operation": "search_evidence", "arguments": {"terms": ["solver"]}}}
        retained = workbench.execute(action["tool_action"])
        assignment = deepcopy(self.assignment)
        assignment["_software_history_refs"] = [retained["receipt_ref"]]
        def run(task, **kwargs):
            initial = json.loads(kwargs["inputs"]["assignment.json"])
            historical, = [r for r in initial["software_tool_results"] if r.get("receipt_ref") == retained["receipt_ref"]]
            self.assertNotIn("result", historical)
            continuation = kwargs["exchange"]({"response.json": json.dumps(action).encode()},
                                                {"model_calls": 1, "output_tokens": 10})
            packet = json.loads(continuation["inputs"]["assignment.json"])
            exposed, = [r for r in packet["software_tool_results"] if r.get("receipt_ref") == retained["receipt_ref"]]
            self.assertEqual(exposed["result"], retained["result"])
            self.assertEqual(exposed["outcome"], retained["outcome"])
            with self.assertRaisesRegex(ValidationError, "repeated without new input"):
                kwargs["exchange"]({"response.json": json.dumps(action).encode()},
                                   {"model_calls": 2, "output_tokens": 20})
            return self.batch_result(self.final_response())
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run), \
                patch.object(SoftwareWorkbench, "_search_evidence", side_effect=AssertionError("no new retrieval")):
            report = self.dispatcher().dispatch([assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(len([r for r in report["software_tool_results"] if r.get("receipt_ref") == retained["receipt_ref"]]), 0)
        self.assertIn(retained["receipt_ref"], report["historical_software_tool_refs"])

    def test_controller_session_retains_duplicate_action_guard(self):
        action = {"tool_action": {"operation": "search_evidence", "arguments": {"terms": ["solver"]}}}
        def run(task, **kwargs):
            files = {"response.json": json.dumps(action).encode()}
            kwargs["exchange"](files, {"model_calls": 1, "input_tokens": 10, "output_tokens": 5})
            with self.assertRaisesRegex(ValidationError, "repeated without new input"):
                kwargs["exchange"](files, {"model_calls": 2, "input_tokens": 20, "output_tokens": 10})
            return self.batch_result(self.final_response())
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run):
            report = self.dispatcher().dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(len(report["software_tool_results"]), 2)

    def test_final_contract_repair_continues_in_same_session(self):
        def run(task, **kwargs):
            initial = json.loads(kwargs["inputs"]["assignment.json"])
            invalid = {"response": self.final_response()}
            repair = kwargs["exchange"]({"response.json": json.dumps(invalid).encode()},
                                         {"model_calls": 3, "input_tokens": 41, "output_tokens": 17})
            packet = json.loads(repair["inputs"]["assignment.json"])
            self.assertEqual(packet["evidence_packet"], initial)
            self.assertFalse(packet["response_format_repair"]["stage_failure_evidence"])
            self.assertEqual(packet["runtime_python"], initial["runtime_python"])
            self.assertIsNone(kwargs["exchange"](
                {"response.json": json.dumps(self.final_response()).encode()},
                {"model_calls": 4, "input_tokens": 51, "output_tokens": 23}))
            return self.batch_result(self.final_response(), usage={"model_calls": 4, "input_tokens": 51, "output_tokens": 23})
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run) as transport:
            report = self.dispatcher().dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(report["validation_retries"], 1)
        self.assertEqual(report["usage"]["model_calls"], 4)
        self.assertEqual(len(report["request_inputs"][0]["controller_response_repairs"]), 1)

    def test_repeated_final_contract_failure_stops_without_new_session(self):
        invalid = {"response": self.final_response()}
        def run(task, **kwargs):
            files = {"response.json": json.dumps(invalid).encode()}
            self.assertIsNotNone(kwargs["exchange"](files, {"model_calls": 3, "output_tokens": 17}))
            self.assertIsNone(kwargs["exchange"](files, {"model_calls": 4, "output_tokens": 23}))
            return self.batch_result(invalid, usage={"model_calls": 4, "input_tokens": 51, "output_tokens": 23})
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run) as transport:
            report = self.dispatcher().dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "failed", report)
        self.assertEqual(report["failure"]["kind"], "output_contract")
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(report["usage"]["model_calls"], 4)

    def test_in_session_contract_repair_then_tool_restores_current_evidence(self):
        def run(task, **kwargs):
            initial = json.loads(kwargs["inputs"]["assignment.json"])
            kwargs["exchange"]({"response.json": json.dumps({"response": self.final_response()}).encode()},
                               {"model_calls": 3, "output_tokens": 17})
            action = {"tool_action": {"operation": "search_evidence", "arguments": {"terms": ["solver"]}}}
            continuation = kwargs["exchange"]({"response.json": json.dumps(action).encode()},
                                               {"model_calls": 4, "output_tokens": 23})
            packet = json.loads(continuation["inputs"]["assignment.json"])
            self.assertEqual(packet["question"], initial["question"])
            self.assertNotIn("response_format_repair", packet)
            self.assertNotIn("evidence_packet", packet)
            self.assertEqual(packet["software_tool_results"][-1]["action"], action["tool_action"])
            self.assertIsNone(kwargs["exchange"]({"response.json": json.dumps(self.final_response()).encode()},
                                                 {"model_calls": 5, "output_tokens": 29}))
            return self.batch_result(self.final_response(), usage={"model_calls": 5, "output_tokens": 29})
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run) as transport:
            report = self.dispatcher().dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(report["validation_retries"], 0)
        self.assertEqual(report["usage"]["model_calls"], 5)

    def test_controller_exchange_after_format_repair_restores_base_assignment(self):
        calls = []
        def run(task, **kwargs):
            assignment = json.loads(kwargs["inputs"]["assignment.json"])
            calls.append(assignment)
            if len(calls) == 1:
                return self.batch_result({"response": self.final_response()})
            self.assertIn("response_format_repair", assignment)
            action = {"tool_action": {"operation": "search_evidence", "arguments": {"terms": ["solver"]}}}
            continuation = kwargs["exchange"]({"response.json": json.dumps(action).encode()},
                                               {"model_calls": 3, "input_tokens": 41, "output_tokens": 17})
            updated = json.loads(continuation["inputs"]["assignment.json"])
            self.assertNotIn("response_format_repair", updated)
            self.assertNotIn("evidence_packet", updated)
            self.assertEqual(updated["question"], self.prompt["question"])
            self.assertEqual(len(updated["software_tool_results"]), 2)
            self.assertIn("scientific_software_tools", updated)
            return self.batch_result(self.final_response(), usage={"model_calls": 4, "input_tokens": 51, "output_tokens": 23})
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run) as transport:
            report = self.dispatcher().dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(report["validation_retries"], 0)
        self.assertEqual(report["usage"]["model_calls"], 7)

    def test_concept_author_delegates_full_comparison_and_charges_batch_calls(self):
        from scisaurus.runtime.topic_discovery import TopicDiscoveryRunner
        from scisaurus.tests.test_topic_discovery import package
        from scisaurus.tests.test_material_development import concept_candidates
        objective = "Develop a useful material"
        response = package(objective)
        for candidate, concept in zip(response["candidates"], concept_candidates()):
            candidate.update(mechanism=concept["mechanism"], design_brief=concept["design_brief"],
                             research_form="theory_simulation", evidence_mode="synthetic_simulation",
                             comparison_type="mechanism_ablation")
        usage = {"model_calls": 4, "input_tokens": 231, "output_tokens": 119}
        with patch.dict(os.environ, {"SCISAURUS_EXECUTION_POLICY": "development"}), \
                patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", return_value=self.batch_result(response, usage=usage)) as transport, \
                patch("scisaurus.runtime.topic_discovery.ModelClient") as direct:
            runner = TopicDiscoveryRunner(self.model, deadline_seconds=10,
                author_backend=self.backend, author_root=self.root / "concept", runtime_python=sys.executable)
            result = runner.run(objective, intake_mode="concept", candidate_count=3, max_attempts=1)
            direct.assert_not_called()
            self.assertEqual(result["usage"], {**usage, "openalex_requests": 0})
            self.assertEqual(len(result["candidates"]), 3)
            evidence = result["candidate_attempt_trace"][0]["model_response"]
            self.assertEqual(evidence["backend"]["backend"], "dsh-batch-1")
            self.assertEqual(evidence["model"], "dsh-engineer")
            frozen = json.loads(transport.call_args.kwargs["inputs"]["assignment.json"])
            self.assertEqual(frozen["output_contract"]["candidates"]["minItems"], 3)
            self.assertNotIn("tool_action", transport.call_args.args[0])
            self.assertLessEqual(transport.call_args.kwargs["deadline"] - time.monotonic(), 10)
            with patch("scisaurus.runtime.topic_discovery.ModelClient", return_value=object()) as reviewer:
                runner._client("research.topic-source-challenger")
                reviewer.assert_called_once()

    def test_concept_backend_failure_preserves_actual_usage_without_direct_fallback(self):
        from scisaurus.runtime.topic_discovery import TopicDiscoveryRunner
        receipt = self.root / "failed-batch.json"
        receipt.write_text('{"status":"failed"}')
        usage = {"model_calls": 3, "input_tokens": 80, "output_tokens": 21}
        with patch.dict(os.environ, {"SCISAURUS_EXECUTION_POLICY": "development"}), \
                patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=DshBatchError("blocked", receipt=receipt, usage=usage)), \
                patch("scisaurus.runtime.topic_discovery.ModelClient") as direct:
            runner = TopicDiscoveryRunner(self.model, author_backend=self.backend,
                author_root=self.root / "concept", runtime_python=sys.executable)
            with self.assertRaises(DshBatchError) as failure:
                runner.run("Develop a useful material", intake_mode="concept", candidate_count=3)
            direct.assert_not_called()
            self.assertEqual(failure.exception.usage, {**usage, "openalex_requests": 0})
            self.assertEqual(failure.exception.topic_budget["usage"], {**usage, "openalex_requests": 0})
            self.assertEqual(failure.exception.candidate_attempt_trace[0]["backend_receipt"], str(receipt))

    def test_batch_budget_overflow_retains_all_paid_usage(self):
        from scisaurus.runtime.topic_discovery import TopicBudget
        from scisaurus.core.errors import QuotaExceededError
        budget = TopicBudget({"max_model_calls": 2})
        budget.before_model_call("topic_discovery", "dsh-engineer")
        with self.assertRaises(QuotaExceededError):
            budget.record_model_result(ModelResult('{}', 'dsh-engineer',
                {"model_calls": 3, "input_tokens": 100, "output_tokens": 40}, .01, 'stop',
                response_metadata={"backend": "dsh-batch-1"}))
        self.assertEqual(budget.snapshot()["usage"],
                         {"model_calls": 3, "input_tokens": 100, "output_tokens": 40, "openalex_requests": 0})

    def test_structured_producer_preserves_contract_and_strict_deliverable(self):
        from scisaurus.runtime.dsh_batch import DshStructuredProducerClient
        assignment = {"assignment": "repair_methods_plan", "output_contract": {"decision": "repair|hold"},
                      "input": {"raw": [1, 2, 3]}}
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", return_value=self.batch_result({"decision": "hold"})) as transport:
            client = DshStructuredProducerClient(self.backend, root=self.root / "structured", runtime_python=sys.executable)
            response = client.complete(system="Exact plan contract", prompt=json.dumps(assignment))
        self.assertEqual(response.json_object(), {"decision": "hold"})
        frozen = json.loads(transport.call_args.kwargs["inputs"]["assignment.json"])
        self.assertEqual(frozen["input"], assignment["input"])
        self.assertEqual(frozen["output_contract"], assignment["output_contract"])
        self.assertEqual(transport.call_args.kwargs["outputs"], ["response.json"])

    def test_methods_plan_uses_structured_backend_without_software_operations(self):
        assignment = {**self.assignment, "_response_contract": "repair_adjudication", "_prompt": '{}'}
        assignment.pop("_software_tools")
        plan = {"schema_version": "experiment-repair-adjudication-1", "topic_id": "direction_3",
                "disposition": "repair", "root_cause": {"statement": "The estimand is an input.",
                "evidence": ["The source fixes both branch slopes."]},
                "required_changes": [{"target": "estimand", "instruction": "Use a signed slope difference.",
                    "scientific_basis": "The null must be reachable.", "source_refs": []}],
                "acceptance_checks": [{"phase": "execution", "check": "Equal slopes contain zero."}]}
        response = {"decision": "repair", "summary": "The contrast is planted.", "findings": [],
                    "evidence_gaps": [], "requested_actions": [], "repair_plan": plan}
        with patch("scisaurus.runtime.dsh_batch.DshStructuredProducerClient.complete",
                   return_value=ModelResult(json.dumps(response), 'dsh-engineer', {"model_calls": 2}, .01, 'stop')) as producer, \
                patch("scisaurus.runtime.specialists.ModelClient") as direct:
            report = self.dispatcher().dispatch([assignment], {"topic_id": "direction_3"})[0]
            producer.assert_called_once()
            direct.assert_not_called()
            self.assertEqual(report["status"], "succeeded", report.get("error"))
            self.assertEqual(report["execution_mode"], "dsh_structured_producer")
            self.assertEqual(report["response"]["raw"]["repair_plan"], plan)
            self.assertEqual(report["usage"]["model_calls"], 2)

    def test_dispatch_uses_actual_dsh_model_and_entire_batch_usage(self):
        usage = {"model_calls": 4, "input_tokens": 101, "output_tokens": 33,
                 "cached_input_tokens": 19, "reasoning_tokens": 12}
        result = self.batch_result(self.final_response(), usage=usage)
        dispatcher = self.dispatcher()
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", return_value=result) as transport, \
                patch("scisaurus.runtime.specialists.ModelClient") as direct:
            report = dispatcher.dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(report["execution_mode"], "dsh_software_producer")
        self.assertEqual(report["model"], "dsh-engineer")
        self.assertEqual(report["usage"], usage)
        direct.assert_not_called()
        request = report["request_inputs"][0]
        self.assertEqual(request["generation_config"]["protocol"], "dsh_batch")
        self.assertEqual(request["generation_config"]["model"], "dsh-engineer")
        self.assertEqual(request["backend"]["receipt"], result["receipt"])
        self.assertEqual(request["backend"]["configuration_sha256"], dispatcher.software_author_backend_sha256)
        frozen = json.loads(transport.call_args.kwargs["inputs"]["assignment.json"])
        self.assertEqual(frozen["question"], self.prompt["question"])
        self.assertEqual(frozen["scientific_software_tools"], tool_contract())
        self.assertNotIn("author_backend", frozen)
        self.assertEqual(frozen["runtime_python"], str(Path(sys.executable).absolute()))
        self.assertEqual(transport.call_args.kwargs["outputs"], ["response.json"])
        self.assertIn(b"scientific software", transport.call_args.kwargs["inputs"]["system-contract.txt"])

    def test_independent_reviewer_uses_model_client_with_backend_bound(self):
        reviewer = {"assigned_role": "research.adversarial-reviewer", "role_id": "reviewer",
                    "model_role": "review.arbiter", "_prompt": "{}",
                    "quota": {"max_calls": 1, "max_input_tokens": 24000, "max_output_tokens": 2000}}
        result = ModelResult('{"decision":"hold","rationale":"Needs independent evidence."}',
                             "scientific-reviewer", {"model_calls": 1}, 0.01, "stop")
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run") as transport, \
                patch("scisaurus.runtime.specialists.ModelClient") as direct:
            direct.return_value.complete.return_value = result
            report = self.dispatcher().dispatch([reviewer], {}, verifier=True)[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(report["execution_mode"], "model")
        self.assertEqual(report["model"], "scientific-reviewer")
        self.assertEqual(direct.call_args.kwargs["model"], "scientific-reviewer")
        transport.assert_not_called()
        self.assertNotIn("backend_config_sha256", report)

    def test_backend_requires_bound_laboratory(self):
        result = ModelResult(json.dumps(self.final_response()), "scientific-reviewer",
                             {"model_calls": 1}, 0.01, "stop")
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run") as transport, \
                patch("scisaurus.runtime.specialists.ModelClient") as direct:
            direct.return_value.complete.return_value = result
            report = self.dispatcher(laboratory=False).dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(report["execution_mode"], "model")
        transport.assert_not_called()
        self.assertEqual(direct.return_value.complete.call_count, 1)

    def test_structured_tool_continuation_and_format_repair_share_deadline(self):
        now = [100.0]
        responses = [{"response": self.final_response()},
                     {"tool_action": {"operation": "search_evidence", "arguments": {"terms": ["solver"]}}},
                     self.final_response()]
        captured = []
        def run(task, **kwargs):
            captured.append(deepcopy(kwargs))
            response = responses[len(captured) - 1]
            now[0] += 3
            return self.batch_result(response)
        with patch("scisaurus.runtime.specialists.time.monotonic", side_effect=lambda: now[0]), \
                patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run), \
                patch("scisaurus.runtime.specialists.ModelClient") as direct:
            report = self.dispatcher(deadline=110).dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(report["usage"], {"model_calls": 9, "input_tokens": 123, "output_tokens": 51})
        self.assertEqual(len(captured), 3)
        self.assertTrue(all(row["deadline"] <= 110 for row in captured))
        repair = json.loads(captured[1]["inputs"]["assignment.json"])
        self.assertEqual(repair["evidence_packet"]["question"], self.prompt["question"])
        self.assertFalse(repair["response_format_repair"]["stage_failure_evidence"])
        continuation = json.loads(captured[2]["inputs"]["assignment.json"])
        self.assertNotIn("response_format_repair", continuation)
        self.assertEqual(continuation["software_tool_results"][-1]["action"]["operation"], "search_evidence")
        self.assertEqual(continuation["software_tool_results"][-1]["result"]["catalog_sources"], 0)
        self.assertEqual(report["request_inputs"][1]["tool_result"]["receipt_ref"],
                         continuation["software_tool_results"][-1]["receipt_ref"])
        self.assertEqual(report["retry_history"][0]["kind"], "validation")
        direct.assert_not_called()

    def test_tool_continuation_cannot_dispatch_after_stage_deadline(self):
        now = [100.0]
        def run(task, **kwargs):
            self.assertLessEqual(kwargs["deadline"], 110)
            now[0] = 109.9
            return self.batch_result({"tool_action": {"operation": "search_evidence",
                                                       "arguments": {"terms": ["solver"]}}})
        with patch("scisaurus.runtime.specialists.time.monotonic", side_effect=lambda: now[0]), \
                patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run) as transport, \
                patch("scisaurus.runtime.specialists.ModelClient") as direct:
            report = self.dispatcher(deadline=110).dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "failed", report)
        self.assertIn("deadline exceeded", report["error"])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(report["usage"]["model_calls"], 3)
        self.assertEqual(len(report["software_tool_results"]), 2)
        direct.assert_not_called()

    def test_role_time_allowance_bounds_entire_producer_tool_loop(self):
        now = [100.0]
        deadlines = []
        def run(task, **kwargs):
            deadlines.append(kwargs["deadline"])
            if len(deadlines) == 1:
                now[0] += 3
                return self.batch_result({"response": self.final_response()})
            now[0] = 104.9
            return self.batch_result({"tool_action": {"operation": "search_evidence",
                                                       "arguments": {"terms": ["solver"]}}})
        assignment = deepcopy(self.assignment)
        assignment["quota"]["max_seconds"] = 5
        with patch("scisaurus.runtime.specialists.time.monotonic", side_effect=lambda: now[0]), \
                patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=run) as transport:
            report = self.dispatcher(deadline=200).dispatch([assignment], {})[0]
        self.assertEqual(report["status"], "failed", report)
        self.assertIn("role deadline exceeded", report["error"])
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(deadlines, [105.0, 105.0])
        self.assertEqual(report["usage"]["model_calls"], 6)
        self.assertEqual(len(report["software_tool_results"]), 2)

    def test_repeated_software_format_failure_gets_only_one_repair(self):
        result = self.batch_result({"response": self.final_response()})
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", return_value=result) as transport:
            report = self.dispatcher().dispatch([self.assignment], {})[0]
        self.assertEqual(report["status"], "failed", report)
        self.assertEqual(report["failure"]["kind"], "output_contract")
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(report["usage"]["model_calls"], 6)
        self.assertEqual(len(report["retry_history"]), 1)

    def test_invalid_file_deliverable_preserves_completed_batch_evidence(self):
        for content in (b'{"decision":', b'{}', b'{"decision":"hold"} trailing text'):
            with self.subTest(content=content):
                result = self.batch_result({})
                result["files"]["response.json"] = content
                with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", return_value=result) as transport:
                    report = self.dispatcher().dispatch([self.assignment], {})[0]
                self.assertEqual(report["status"], "failed", report)
                self.assertEqual(report["failure"], {"kind": "operational_recovery", "outcome_known": True})
                self.assertEqual(report["dsh_receipt"], result["receipt"])
                self.assertEqual(report["usage"], result["usage"])
                self.assertEqual(transport.call_count, 1)

    def test_batch_failure_retains_receipt_usage_and_outcome_without_redispatch(self):
        for receipt_status, expected_status, known in (
                ("completed", "failed", True), ("result_unknown", "result_unknown", False)):
            with self.subTest(receipt_status=receipt_status):
                first = self.batch_result({"tool_action": {"operation": "search_evidence",
                                                           "arguments": {"terms": ["solver"]}}})
                receipt = self.root / (receipt_status + ".json")
                receipt.write_text(json.dumps({"status": receipt_status}))
                usage = {"model_calls": 5, "input_tokens": 71, "output_tokens": 23, "reasoning_tokens": 8}
                error = DshBatchError("retained batch failure", receipt=receipt, usage=usage)
                with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=[first, error]) as transport, \
                        patch("scisaurus.runtime.specialists.ModelClient") as direct:
                    report = self.dispatcher().dispatch([self.assignment], {})[0]
                self.assertEqual(report["status"], expected_status, report)
                self.assertEqual(report["failure"], {"kind": "operational_recovery", "outcome_known": known})
                self.assertEqual(report["dsh_receipt"], str(receipt))
                self.assertEqual(report["usage"], {"model_calls": 8, "input_tokens": 112,
                                                   "output_tokens": 40, "reasoning_tokens": 8})
                request = report["request_inputs"][-1]
                self.assertEqual(request["backend_receipt"], str(receipt))
                self.assertEqual(request["backend_usage"], usage)
                self.assertIs(request["outcome_known"], known)
                self.assertEqual(transport.call_count, 2)
                direct.assert_not_called()

    def test_composer_reuses_unknown_batch_without_redispatch_or_usage_recharge(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.runtime.model_work import ModelWorkCache
        from scisaurus.tests.test_composer import ComposerWorkflowTests

        runner = ComposerRunner(ComposerWorkflowTests()._workflow(self.root))
        self.addCleanup(runner.close)
        receipt = self.root / "unknown-receipt.json"
        receipt.write_text(json.dumps({"status": "result_unknown", "process_reaped": True}))
        receipt_bytes = receipt.read_bytes()
        usage = {"model_calls": 4, "input_tokens": 61, "output_tokens": 29}
        failure = DshBatchError("transport interrupted", receipt=receipt, usage=usage)
        assignment = {**self.assignment, "stage_id": "experiment", "attempt_number": 1}
        cache = ModelWorkCache(runner.store, runner._publish)
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", side_effect=failure) as transport, \
                patch("scisaurus.runtime.specialists.ModelClient") as direct:
            first = runner._dispatch_specialist_work(self.dispatcher(), [assignment], {})[0]
            entry = cache.entries()[0]
            manifest = runner.store.get(entry["cache_ref"])
            cached_bytes = runner.store.read_body(manifest["body_hash"])
            retained = runner._dispatch_specialist_work(
                self.dispatcher(), [{**assignment, "attempt_number": 2}], {})[0]
        self.assertEqual(transport.call_count, 1)
        direct.assert_not_called()
        self.assertEqual(first["status"], "result_unknown", first)
        self.assertTrue(first["dsh_backend_terminal"])
        self.assertEqual(first["usage"], usage)
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(entry["report"]["usage"], usage)
        self.assertEqual(retained["status"], "result_unknown")
        self.assertEqual(retained["execution_mode"], "retained_failure")
        self.assertEqual(retained["usage"], {})
        self.assertEqual(retained["request_attempts"], 0)
        self.assertEqual(retained["reused_from"], entry["cache_ref"])
        self.assertEqual(retained["dsh_receipt"], str(receipt))
        self.assertEqual(retained["request_inputs"][0]["backend_usage"], usage)
        self.assertEqual(cache.entries(), [entry])
        self.assertEqual(runner.store.read_body(manifest["body_hash"]), cached_bytes)
        self.assertEqual(receipt.read_bytes(), receipt_bytes)

    def test_cache_identity_binds_entire_backend_configuration(self):
        legacy = SpecialistDispatcher(self.model)
        self.assertEqual(legacy.cache_identity(), legacy.model_config)
        bound = self.dispatcher()
        digest = hashlib.sha256(canonical_bytes(self.backend)).hexdigest()
        self.assertEqual(bound.cache_identity()["software_author_backend_sha256"], digest)
        for key, replacement in (("model", "other-engineer"), ("timeout_seconds", 15),
                                 ("max_output_tokens", 1000)):
            changed = deepcopy(self.backend)
            changed[key] = replacement
            with self.subTest(key=key):
                self.assertNotEqual(bound.cache_identity(), self.dispatcher(backend=changed).cache_identity())
        changed = deepcopy(self.backend)
        changed["pinned_files"][changed["composition"]] = "a" * 64
        self.assertNotEqual(bound.cache_identity(), self.dispatcher(backend=changed).cache_identity())
        changed_runtime = self.dispatcher()
        changed_runtime.software_author_runtime_python = self.root / "other-python"
        self.assertNotEqual(bound.cache_identity(), changed_runtime.cache_identity())
        self.assertEqual(changed_runtime.cache_identity()["software_author_runtime_python"],
                         str(self.root / "other-python"))
        identity = bound.cache_identity()
        identity["model"] = "mutated"
        self.assertEqual(bound.model_config["model"], "scientific-reviewer")


if __name__ == "__main__":
    unittest.main()
