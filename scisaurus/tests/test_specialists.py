import json
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.models import (
    ModelCallError, ModelResult, admit_model_provider_call,
    clear_model_provider_cooldown,
    estimate_input_tokens, model_provider_cooldown_remaining,
    record_model_provider_cooldown,
)
from scisaurus.runtime.specialists import (
    REPAIR_ADJUDICATION_SYSTEM, REPAIR_EVIDENCE_SYSTEM, SPECIALIST_SYSTEM, VERIFIER_SYSTEM,
    SpecialistDispatcher,
    _normalise_report, _normalise_verdict,
    _specialist_repair_prompt, _verifier_repair_prompt,
    build_specialist_prompt, build_verifier_prompt, redact_sensitive_text,
)


class _SpecialistHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        server = self.server
        with server.lock:
            server.active += 1
            server.peak = max(server.peak, server.active)
            server.requests += 1
            request_number = server.requests
        if hasattr(server, "request_barrier") and request_number <= server.request_barrier.parties:
            server.request_barrier.wait(timeout=5)
        else:
            time.sleep(0.04)
        body = json.dumps({
            "model": "fake-specialist",
            "choices": [{"message": {"content": json.dumps({
                "decision": "pass", "summary": "checked", "findings": [],
                "evidence_gaps": [], "requested_actions": [],
            })}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        with server.lock:
            server.active -= 1

    def log_message(self, *_args):
        return


class SpecialistServiceTests(unittest.TestCase):
    def test_unbound_service_role_is_not_reported_as_successful_work(self):
        events = []
        dispatcher = SpecialistDispatcher({
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "unused", "timeout_seconds": 1, "max_output_tokens": 32,
        }, on_progress=events.append)
        report = dispatcher._execute({
            "assigned_role": "research.source-acquirer",
            "role_id": "source-acquirer", "model_role": "research.source-acquirer",
            "execution_kind": "service", "quota": {"max_calls": 1},
        }, {"stage_result": {"status": "completed"}})
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["execution_mode"], "service_unavailable")
        self.assertEqual(report["failure_class"], "service_unavailable")
        self.assertEqual(events[-1]["event"], "failed")
        self.assertEqual(report["usage"], {})


class _VerifierRetryHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        server = self.server
        with server.lock:
            server.requests += 1
            request_number = server.requests
        content = '{"decision":"hold"' if request_number == 1 else json.dumps({
            "decision": "accept", "rationale": "checked", "critical_findings": [],
            "repair_scope": [],
        })
        body = json.dumps({
            "model": "fake-verifier",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class _ProviderFallbackHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        server = self.server
        model = request.get("model")
        with server.lock:
            server.models.append(model)
        if model == "gemma":
            body = b'{"error":"quota exhausted"}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Keep the first primary reservation occupied while the second logical
        # assignment is forced onto Gemma. The retry must then wait for the
        # single primary slot and return to it after Gemma's 429.
        time.sleep(0.15)
        body = json.dumps({
            "model": model,
            "choices": [{"message": {"content": json.dumps({
                "decision": "pass", "summary": "checked", "findings": [],
                "evidence_gaps": [], "requested_actions": [],
            })}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class SpecialistDispatcherTests(unittest.TestCase):
    def test_repair_evidence_preserves_complete_scope_and_rejects_unbound_sources(self):
        from scisaurus.runtime.specialists import REPAIR_EVIDENCE_SYSTEM, build_repair_evidence_prompt
        source = "def execute():\n    return 1\n" * 300
        packet = {"topic": {"id": "topic", "research_question": "Does C change eta?"},
                  "prior_foundry_work": {"last_attempt": {"experiment_intent": {"primary_outcomes": ["eta"]}}},
                  "exact_candidate_sources": {"executor": {"available": True, "source_chunks": [source],
                      "prompt_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                      "prompt_source_characters": len(source)}}}
        request = {"requested_actions": ["Derive and delimit the source-bound claim. " * 100],
                   "source_ref_catalog": ["candidate_program.executor"]}
        assignment = {"assigned_role": "methods.methodologist", "model_role": "methods.methodologist",
                      "role_id": "methodologist", "task_id": "evidence-task", "stage_id": "evidence",
                      "_response_contract": "repair_evidence", "execution_kind": "review",
                      "quota": {"max_input_tokens": 24000, "max_output_tokens": 2000,
                                "max_calls": 1, "max_seconds": 10}}
        prompt = build_repair_evidence_prompt(assignment, packet, request)
        from scisaurus.runtime.evidence import scientific_input_recovery_contract
        self.assertEqual(json.loads(prompt)["scientific_input_recovery"], scientific_input_recovery_contract())
        payload = json.loads(prompt)
        self.assertEqual(payload["repair_evidence_request"], request)
        self.assertEqual("".join(payload["candidate_program"]["exact_execution_sources"]["executor"]["source_chunks"]), source)
        with self.assertRaisesRegex(ValidationError, "cannot preserve"):
            build_repair_evidence_prompt({**assignment, "quota": {"max_input_tokens": 1000}}, packet, request)
        model = {"protocol": "openai_compatible", "base_url": "http://fake/v1", "model": "fake",
                 "max_input_tokens": 24000, "max_output_tokens": 512, "timeout_seconds": 5}
        for bad_source in (False, True):
            note = {"title": "Analytic note", "content": "Exact complete derivation. " * 100,
                    "source_refs": ["forged-source" if bad_source else "candidate_program.executor"], "limitations": [],
                    "action_disposition": "fulfilled"}
            response = {"decision": "pass", "summary": "Produced.", "findings": [], "evidence_gaps": [],
                        "requested_actions": [], "evidence_note": note}
            with patch("scisaurus.runtime.specialists.ModelClient") as client:
                client.return_value.complete.return_value = ModelResult(json.dumps(response), "fake",
                    {"model_calls": 1}, 0, "stop")
                report = SpecialistDispatcher(model, max_parallel=1).dispatch([{**assignment, "_prompt": prompt}], {})[0]
                self.assertEqual(client.return_value.complete.call_args.kwargs["system"], REPAIR_EVIDENCE_SYSTEM)
            self.assertEqual(report["status"], "failed" if bad_source else "succeeded")
            self.assertEqual(report["usage"]["model_calls"], 1)
            if not bad_source:
                self.assertEqual(report["response"]["evidence_note"], note)

    def test_all_progress_events_preserve_admitted_assignment_identity(self):
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                 "model": "fixture", "timeout_seconds": 5.0, "max_output_tokens": 512,
                 "reasoning_effort": "high"}
        assignment = {"assigned_role": "methods.analysis-reviewer", "role_id": "analysis-reviewer",
                      "model_role": "methods.analysis-reviewer", "execution_kind": "review",
                      "task_id": "scoped-review", "stage_id": "repair", "attempt_number": 7,
                      "quota": {"max_calls": 2, "max_input_tokens": 10000,
                                "max_output_tokens": 1024, "max_seconds": 5}, "_prompt": "{}"}
        events = []
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = [
                ModelResult("Narration.", "fixture", {"model_calls": 1}, .01, "length"),
                ModelResult('{"decision":"repair","summary":"source defect"}', "fixture",
                            {"model_calls": 1}, .01, "stop"),
            ]
            report = SpecialistDispatcher(model, max_parallel=1,
                deadline=time.monotonic() + 10, on_progress=events.append).dispatch([assignment], {})[0]
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual([item["event"] for item in events],
                         ["dispatched", "retrying", "dispatched", "completed"])
        for event in events:
            self.assertEqual(event["task_id"], assignment["task_id"])
            self.assertEqual(event["stage_id"], assignment["stage_id"])
            self.assertEqual(event["role"], assignment["assigned_role"])
            self.assertEqual(event["assignment_attempt_number"], 7)
        for event in events:
            if event["event"] == "dispatched":
                self.assertEqual(event["protocol"], "openai_compatible")
                self.assertEqual(event["reasoning_effort"], "high")
        for request in report["request_inputs"]:
            self.assertEqual(request["generation_config"]["reasoning_effort"], "high")
            self.assertEqual(request["generation_config"]["protocol"], "openai_compatible")
            self.assertNotIn("auth_env", request["generation_config"])

    def test_json_continuation_classifies_incomplete_tokens_and_invalid_prefixes(self):
        from scisaurus.runtime.models import json_object_continuation_error
        for prefix in ('{"a":', '{"a":tr', '{"a":-', '{"a":1.', '{"a":1e',
                       '{"a":1.2e+', '{"a":"unterminated', '{"a":"\\u12'):
            with self.subTest(prefix=prefix):
                self.assertIsNone(json_object_continuation_error(prefix))
        for prefix in ('{not JSON', '{"a":truX', '{"a":1,]', '{"a":1.e',
                       '{"a":"\\uZZ', '{"a":false nope', '{"a":t\n', '{"a":1e ',
                       '{"a":"x\n', '{"a":"x\t', '```json\n{"a":t\n',
                       '```json\n{"a":"x\n', '{"a":NaN', '{"a":Infinity',
                       '{"a":-Infinity', '{"a":1e999'):
            with self.subTest(prefix=prefix):
                self.assertIn("invalid JSON object prefix", json_object_continuation_error(prefix))

    def test_verifier_records_each_actual_repair_input(self):
        from copy import deepcopy
        import hashlib
        captured = []
        replies = ['{"decision":"hold"', json.dumps({"decision": "accept", "rationale": "Checked.",
                   "critical_findings": [], "repair_scope": []})]
        def complete(_client, **kwargs):
            captured.append(deepcopy(kwargs))
            return ModelResult(text=replies[len(captured)-1], model="fake", usage={"model_calls": 1,
                               "input_tokens": 10, "output_tokens": 20}, elapsed_seconds=0, finish_reason="stop")
        dispatcher = SpecialistDispatcher({"protocol": "ollama", "base_url": "http://127.0.0.1:11434",
                                           "model": "fake", "timeout_seconds": 1, "max_output_tokens": 512})
        assignment = {"assigned_role": "review.arbiter", "role_id": "adversary", "model_role": "review.arbiter",
                      "execution_kind": "model", "_prompt": '{"stage":"survey"}',
                      "quota": {"max_calls": 2, "max_input_tokens": 10000, "max_output_tokens": 1024}}
        with patch("scisaurus.runtime.specialists.ModelClient.complete", complete):
            result = dispatcher.dispatch([assignment], {}, verifier=True)[0]
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual([item["input"] for item in result["request_inputs"]], captured)
        self.assertNotEqual(captured[0]["prompt"], captured[1]["prompt"])
        for item in result["request_inputs"]:
            encoded = json.dumps(item["input"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(item["input_sha256"], hashlib.sha256(encoded).hexdigest())
            self.assertEqual(item["request_attempts"], 1)

    def test_verifier_preflight_error_preserves_original_context_failure_without_dispatch(self):
        dispatcher = SpecialistDispatcher({"protocol": "ollama", "base_url": "http://127.0.0.1:11434",
                                           "model": "fake", "timeout_seconds": 1, "max_output_tokens": 512})
        assignment = {"assigned_role": "review.arbiter", "role_id": "adversary", "model_role": "review.arbiter",
                      "execution_kind": "model", "_prompt": '{}',
                      "quota": {"max_calls": 2, "max_input_tokens": 100, "max_output_tokens": 1024}}
        with patch("scisaurus.runtime.specialists.ModelClient.complete") as complete:
            result = dispatcher.dispatch([assignment], {}, verifier=True)[0]
        complete.assert_not_called()
        self.assertEqual(result["status"], "failed")
        self.assertIn("context budget", result["error"])
        self.assertNotIn("UnboundLocalError", result["error"])
        self.assertEqual(result["usage"], {})
        self.assertEqual(result["request_inputs"], [])

    def test_survey_verifier_keeps_current_searches_counts_and_exact_disposition_spans(self):
        from scisaurus.core.source_spans import bind
        source = {"work_id": "W1", "text": "Captured primary evidence. " * 90}
        proof = bind({"work_id": "W1", "source_ref": "artifact:source@1", "quote": source["text"]},
                     {"artifact:source@1": source})
        searches = [{"role": "research.novelty-challenger", "execution_ref": f"artifact:search-{i}@1",
                     "request": {"operation": "search", "query": f"counterquery {i}"}, "outcome": "ok"}
                    for i in range(12)]
        availability = {"work_id": "W1", "evidence_scope": "abstract",
                        "full_text_access": "unavailable", "abstract_refs": ["artifact:source@1"],
                        "verified_full_text_refs": [], "full_text_failures": [{
                            "outcome": "access_denied", "source_url": "https://example.org/paper.pdf",
                            "execution_ref": "artifact:fetch@1"}]}
        coverage = {**{f"counter_{i}": i for i in range(15)}, "unique_works": 170,
                    "verified_full_texts": 4, "searches": searches,
                    "source_evidence_policy": "Use captured abstracts within their quoted scope.",
                    "source_availability": [availability],
                    "source_windows": [{"source_ref": "artifact:source@1", "work_id": "W1",
                                        "source_availability": availability,
                                        "window": {"start": 0, "end": len(source["text"])}}]}
        chief = {"status": "completed", "survey_ref": "artifact:survey@3", "assessment_ref": "artifact:assessment@1",
                 "gap_state": "insufficient_evidence", "coverage": coverage,
                 "revalidation": {"ref": "artifact:revalidation@1", "body_sha256": "a" * 64},
                 "follow_up_result": {"ref": "artifact:disposition@1", "orders": [{"id": "one", "status": "limited",
                    "rationale": "Coverage remains bounded.", "evidence": [proof], "query_refs": ["artifact:query@1"],
                    "limitation": "No independent measurement.", "next_action": "Test an exploratory model."}]}}
        orders = [{"id": "one", "objective": "Exact evidence obligation α\r\n" * 100,
                   "completion_check": "Compare every source-supported clause.", "source_refs": ["artifact:source@1"]}]
        chief["work_orders"] = orders
        from scisaurus.runtime.specialists import _verifier_chief_result
        for detail in ("full", "compact", "minimal", "focused"):
            with self.subTest(detail=detail):
                projected = _verifier_chief_result(chief, detail=detail)
                self.assertEqual(projected["coverage"]["unique_works"], 170)
                self.assertEqual(projected["coverage"]["verified_full_texts"], 4)
                self.assertEqual(projected["survey_evidence"]["searches"], searches)
                self.assertEqual(projected["survey_evidence"]["follow_up_result"], chief["follow_up_result"])
                self.assertEqual(projected["survey_evidence"]["work_orders"], orders)
                self.assertEqual(projected["survey_evidence"]["work_orders_sha256"],
                                 hashlib.sha256(canonical_bytes(orders)).hexdigest())
                packet = json.loads(build_verifier_prompt({"id": "survey", "kind": "survey"},
                    {"work_orders": orders}, [], chief))
                self.assertEqual(packet["work_orders"], orders)
                self.assertEqual(packet["work_orders_sha256"], hashlib.sha256(canonical_bytes(orders)).hexdigest())
                self.assertEqual(projected["survey_evidence"]["revalidation"], chief["revalidation"])
                self.assertEqual(projected["survey_evidence"]["source_evidence_policy"],
                                 coverage["source_evidence_policy"])
                self.assertEqual(projected["survey_evidence"]["source_availability"], [availability])
                self.assertEqual(projected["survey_evidence"]["source_windows"][0]["source_availability"],
                                 availability)
        with self.assertRaisesRegex(ValidationError, "input quota"):
            build_verifier_prompt({"id": "survey", "kind": "survey"}, {}, [], chief, max_input_tokens=100)

    def test_verifier_report_exposes_declared_input_scope_without_replacing_current_counts(self):
        report = {"role_id": "search-strategist", "assigned_role": "research.search-strategist", "status": "succeeded",
                  "input_scope": {"declared_fields": ["objective", "topic", "known_gaps", "source_classes"],
                                  "assignment_phase": "specialist"},
                  "response": {"decision": "hold", "summary": "Historical inventory has 6 full texts and 120 works."}}
        chief = {"survey_ref": "artifact:survey@3", "coverage": {"unique_works": 170, "verified_full_texts": 4}}
        value = json.loads(build_verifier_prompt({"id": "survey", "kind": "survey"}, {}, [report], chief))
        self.assertEqual(value["specialist_reports"][0]["input_scope"], report["input_scope"])
        self.assertEqual(value["chief_result"]["coverage"], chief["coverage"])

    def setUp(self):
        clear_model_provider_cooldown({
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "auth_env": None,
        })

    def test_verifier_prompt_is_stable_when_only_runtime_usage_changes(self):
        stage = {"id": "experiment-panel", "kind": "experiment"}
        packet = {"objective": "Review the evidence-bound repair plan."}
        chief = {
            "decision": "repair", "summary": "The calibration target is missing.",
            "usage": {"model_calls": 2, "input_tokens": 4000, "output_tokens": 600},
        }
        replayed_chief = {**chief, "usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}}
        original_usage = {"model_calls": 2, "input_tokens": 1200, "output_tokens": 240}
        report = {
            "assigned_role": "methods.methodologist", "role_id": "methodologist",
            "status": "succeeded", "model_role": "methods.methodologist",
            "model": "deepseek-v4.1-flash:cloud", "usage": original_usage,
            "response": {"decision": "repair", "summary": "Add an external calibration target.",
                         "findings": ["The closure currently supplies its own constraint."],
                         "evidence_gaps": [], "requested_actions": ["Bind to a published value."]},
        }
        replayed = {
            **report, "usage": {}, "reused_prior_usage": original_usage,
            "provider_call_reused": True, "execution_mode": "retained_model_result",
        }

        original = build_verifier_prompt(stage, packet, [report], chief)
        resumed = build_verifier_prompt(stage, packet, [replayed], replayed_chief)

        self.assertEqual(original, resumed)
        self.assertNotIn('"usage"', original)
        self.assertEqual(
            original, build_verifier_prompt(stage, packet, [report], replayed_chief))
        changed_evidence = {
            **replayed,
            "response": {**report["response"], "findings": [
                "A different material finding changes the review payload."]},
        }
        self.assertNotEqual(
            resumed, build_verifier_prompt(stage, packet, [changed_evidence], chief))

    def test_ollama_route_context_supersedes_small_role_context_ceiling(self):
        base = "http://ollama.example/v1"
        model = {
            "protocol": "openai_compatible", "base_url": base,
            "model": "deepseek-flash", "context_window_tokens": 262144,
            "max_input_tokens": 245760, "max_output_tokens": 6000,
            "timeout_seconds": 30,
            "role_routes": {
                "methods.methodologist": [{
                    "id": "ollama-deepseek", "pool": "ollama",
                    "protocol": "openai_compatible", "base_url": base,
                    "model": "deepseek-flash", "context_window_tokens": 262144,
                    "max_input_tokens": 245760, "max_output_tokens": 6000,
                }],
                "research.qwen-role": [{
                    "id": "qwen", "pool": "qwen",
                    "protocol": "openai_compatible", "base_url": "https://qwen.example/v1",
                    "model": "qwen", "context_window_tokens": 65536,
                    "max_input_tokens": 56000,
                }],
            },
        }
        dispatcher = SpecialistDispatcher(model, provider_pools={
            "ollama": {"max_concurrent": 3, "base_urls": [base]},
            "qwen": {"max_concurrent": 1, "base_urls": ["https://qwen.example/v1"]},
        })

        self.assertEqual(dispatcher.input_limit_for_role("methods.methodologist", 16000), 245760)
        self.assertEqual(dispatcher.input_limit_for_role("research.qwen-role", 12000), 12000)

        result = ModelResult(
            json.dumps({"decision": "pass", "summary": "ok", "findings": [],
                        "evidence_gaps": [], "requested_actions": []}),
            "fake", {"model_calls": 1, "input_tokens": 20000, "output_tokens": 5},
            0.01, "stop", 1,
        )
        assignment = {
            "assigned_role": "methods.methodologist", "role_id": "methodologist",
            "model_role": "methods.methodologist", "execution_kind": "model",
            "stage_id": "experiment", "stage_kind": "experiment",
            "quota": {"max_calls": 1, "max_input_tokens": 16000,
                      "max_output_tokens": 1000, "max_seconds": 10},
            "_prompt": "evidence " * 7000,
        }
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.return_value = result
            reports = dispatcher.dispatch([assignment], {"objective": "audit"})
        self.assertEqual(reports[0]["status"], "succeeded", reports[0].get("error"))
        self.assertEqual(reports[0]["context_window_tokens"], 262144)
        self.assertEqual(reports[0]["max_input_tokens"], 245760)
        self.assertEqual(client.call_args.kwargs["max_input_tokens"], 245760)
        self.assertEqual(client.call_args.kwargs["output_format"], "json_object")

    def test_role_quota_clamps_route_timeout_before_provider_dispatch(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 120.0,
            "max_output_tokens": 100, "context_window_tokens": 4096,
            "max_input_tokens": 2048,
        }
        assignment = {
            "assigned_role": "research.role", "role_id": "role",
            "model_role": "research.role", "execution_kind": "model",
            "stage_id": "topic", "stage_kind": "topic_discovery",
            "quota": {"max_calls": 1, "max_input_tokens": 1000,
                      "max_output_tokens": 100, "max_seconds": 5},
            "_prompt": json.dumps({"objective": "bounded"}),
        }
        result = ModelResult(
            json.dumps({"decision": "pass", "summary": "ok", "findings": [],
                        "evidence_gaps": [], "requested_actions": []}),
            "fake", {"model_calls": 1, "input_tokens": 3, "output_tokens": 2},
            0.01, "stop", 1,
        )
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.return_value = result
            reports = SpecialistDispatcher(
                model, max_parallel=1, deadline=time.monotonic() + 10,
            ).dispatch([assignment], {"objective": "bounded"})
        self.assertEqual(reports[0]["status"], "succeeded")
        self.assertEqual(client.call_args.kwargs["timeout_seconds"], 5.0)

    def test_route_timeout_uses_role_quota_without_global_five_minute_cap(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 1800.0,
            "max_output_tokens": 100, "context_window_tokens": 4096,
            "max_input_tokens": 2048,
        }
        assignment = {
            "assigned_role": "research.role", "role_id": "role",
            "model_role": "research.role", "execution_kind": "model",
            "stage_id": "topic", "stage_kind": "topic_discovery",
            "quota": {"max_calls": 1, "max_input_tokens": 1000,
                      "max_output_tokens": 100, "max_seconds": 1200},
            "_prompt": json.dumps({"objective": "bounded"}),
        }
        result = ModelResult(
            json.dumps({"decision": "pass", "summary": "ok", "findings": [],
                        "evidence_gaps": [], "requested_actions": []}),
            "fake", {"model_calls": 1, "input_tokens": 3, "output_tokens": 2},
            0.01, "stop", 1,
        )
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.return_value = result
            reports = SpecialistDispatcher(
                model, max_parallel=1, deadline=time.monotonic() + 1500,
            ).dispatch([assignment], {"objective": "bounded"})
        self.assertEqual(reports[0]["status"], "succeeded")
        self.assertEqual(client.call_args.kwargs["timeout_seconds"], 1200.0)

    def test_specialist_prompt_uses_declared_projection_and_fits_role_quota(self):
        assignment = {
            "assigned_role": "research.cataloger", "model_role": "research.literature-mapper",
            "role_id": "cataloger", "stage_id": "survey", "stage_kind": "survey",
            "system_contract": "Normalize source evidence.", "input_projection": ["topic"],
            "quota": {"max_input_tokens": 12000},
        }
        packet = {
            "objective": "Study the declared question.", "stage_id": "survey", "stage_kind": "survey",
            "topic": {"question": "A bounded question"},
            "dependencies": {"survey": {"topic": "x" * 40000}, "experiment": {"results": "y" * 40000}},
            "runtime_context": {"project_files": [f"file-{i}" for i in range(100)]},
        }
        from scisaurus.runtime.evidence import scientific_input_recovery_contract
        packet["stage_acceptance_contract"] = {
            "scientific_input_recovery": scientific_input_recovery_contract()}
        prompt = build_specialist_prompt(assignment, packet)
        self.assertLessEqual(estimate_input_tokens(SPECIALIST_SYSTEM, prompt), 12000)
        self.assertNotIn("dependencies", json.loads(prompt)["shared_stage_context"])
        self.assertEqual(json.loads(prompt)["shared_stage_context"]["stage_acceptance_contract"],
                         packet["stage_acceptance_contract"])
        self.assertEqual(json.loads(prompt)["projected_input"]["topic"]["question"], "A bounded question")
        contract = json.loads(prompt)["output_contract"]
        self.assertIn("ranked findings naming supplied evidence and its consequence",
                      contract["findings"][0])
        self.assertIn("falsifiable completion check", contract["requested_actions"][0])

    def test_completed_producer_evidence_reaches_each_peer_and_verifier_losslessly(self):
        evidence = {"candidate_sha256": "candidate", "observation_count": 12474,
            "metrics": [{"id": str(i), "value": i} for i in range(20)],
            "limitations": ["bounded assumption " + str(i) for i in range(24)],
            "assessment": {"decision": "accepted_with_limitations", "rationale": "x" * 9000},
            "evidence_scope": "current reviewed producer result"}
        packet = {"objective": "bounded", "completed_producer_evidence": evidence}
        assignment = {"stage_id": "experiment", "stage_kind": "experiment",
            "role_id": "methodologist", "input_projection": [],
            "quota": {"max_input_tokens": 20000}}
        peer = json.loads(build_specialist_prompt(assignment, packet))
        self.assertEqual(peer["shared_stage_context"]["completed_producer_evidence"], evidence)
        verifier = json.loads(build_verifier_prompt({"id": "experiment", "kind": "experiment"},
            packet, [], {"status": "completed"}, max_input_tokens=20000))
        self.assertEqual(verifier["completed_producer_evidence"], evidence)
        with self.assertRaises(ValidationError):
            build_specialist_prompt({**assignment, "quota": {"max_input_tokens": 100}}, packet)
        with self.assertRaises(ValidationError):
            build_verifier_prompt({"id": "experiment", "kind": "experiment"}, packet, [],
                {"status": "completed"}, max_input_tokens=100)

    def test_length_limited_review_continues_the_same_response_until_third_call(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 5.0,
            "max_output_tokens": 8192, "context_window_tokens": 1048576,
            "max_input_tokens": 1040384,
        }
        assignment = {
            "assigned_role": "methods.analysis-reviewer",
            "role_id": "analysis-reviewer", "model_role": "methods.analysis-reviewer",
            "execution_kind": "model", "stage_id": "experiment",
            "stage_kind": "experiment",
            "quota": {"max_calls": 3, "max_input_tokens": 1040384,
                      "max_output_tokens": 24576,
                      "max_output_tokens_per_call": 8192, "max_seconds": 30},
            "_prompt": json.dumps({"objective": "Review the independent recalculation."}),
        }
        complete = json.dumps({
            "decision": "repair",
            "summary": "Independent recalculation found a mismatch in the reported estimate.",
            "findings": ["The archived observations do not reproduce the reported estimate."],
            "evidence_gaps": [],
            "requested_actions": [
                "Recompute the statistic from archived observations and record the interval."
            ],
        })
        first, second = complete[:30], complete[30:75]
        third = complete[75:]
        results = [
            ModelResult(first, "fake", {"model_calls": 1, "output_tokens": 100},
                        0.01, "length", 1),
            ModelResult(second, "fake", {"model_calls": 1, "output_tokens": 100},
                        0.01, "length", 1),
            ModelResult(third, "fake", {"model_calls": 1, "output_tokens": 100},
                        0.01, "stop", 1),
        ]

        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = results
            report = SpecialistDispatcher(
                model, max_parallel=1, deadline=time.monotonic() + 30,
            ).dispatch([assignment], {"objective": "Review the independent recalculation."})[0]

        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["response"]["decision"], "repair")
        self.assertEqual(report["validation_retries"], 2)
        self.assertEqual(report["usage"]["model_calls"], 3)
        self.assertEqual(client.return_value.complete.call_count, 3)
        calls = client.return_value.complete.call_args_list
        self.assertEqual(calls[1].kwargs["continuation_text"], first)
        self.assertEqual(calls[2].kwargs["continuation_text"], first + second)

    def test_review_finishes_fourth_continuation_inside_existing_token_budget(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 5.0,
            "max_output_tokens": 8192, "context_window_tokens": 1048576,
            "max_input_tokens": 1040384,
        }
        assignment = {
            "assigned_role": "methods.analysis-reviewer",
            "role_id": "analysis-reviewer", "model_role": "methods.analysis-reviewer",
            "execution_kind": "model", "stage_id": "experiment",
            "stage_kind": "experiment",
            "quota": {"max_calls": 4, "max_input_tokens": 245760,
                      "max_output_tokens": 24576,
                      "max_output_tokens_per_call": 8192, "max_seconds": 30},
            "_prompt": json.dumps({"objective": "Review the independent recalculation."}),
        }
        complete = json.dumps({
            "decision": "repair",
            "summary": "The independent recalculation disagrees with the reported estimate.",
            "findings": ["The archived observations do not reproduce the reported estimate."],
            "evidence_gaps": [],
            "requested_actions": ["Recompute the estimate directly from the archived observations."],
        })
        chunks = [complete[:25], complete[25:51], complete[51:79], complete[79:]]
        results = [
            ModelResult(chunks[index], "fake", {"model_calls": 1, "output_tokens": tokens},
                        0.01, "length" if index < 3 else "stop", 1)
            for index, tokens in enumerate((7000, 7000, 7000, 3576))
        ]
        events = []
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = results
            report = SpecialistDispatcher(
                model, max_parallel=1, deadline=time.monotonic() + 30,
                on_progress=events.append,
            ).dispatch([assignment], {"objective": "Review the independent recalculation."})[0]

        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["response"]["decision"], "repair")
        self.assertEqual(report["usage"]["output_tokens"], 24576)
        self.assertEqual(client.return_value.complete.call_count, 4)
        calls = client.return_value.complete.call_args_list
        self.assertEqual(calls[3].kwargs["continuation_text"], "".join(chunks[:3]))
        self.assertEqual([call.kwargs["max_output_tokens"] for call in client.call_args_list],
                         [8192, 8192, 8192, 3576])
        self.assertEqual(sum(event.get("event") == "continuing" for event in events), 3)

    def test_repair_adjudication_uses_a_nonconflicting_response_contract(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 5.0,
            "max_output_tokens": 2048, "context_window_tokens": 65536,
            "max_input_tokens": 60000,
        }
        repair_plan = {
            "schema_version": "experiment-repair-adjudication-1",
            "topic_id": "direction_3", "disposition": "repair",
            "root_cause": {"statement": "The estimand is an input.",
                            "evidence": ["The source fixes both branch slopes."]},
            "required_changes": [{
                "target": "estimand", "instruction": "Use a signed slope difference.",
                "scientific_basis": "The null must be reachable.", "source_refs": [],
            }],
            "acceptance_checks": [{"phase": "execution", "check": "Equal slopes produce an interval containing zero."}],
        }
        result = ModelResult(
            json.dumps({
                "decision": "repair", "summary": "The branch contrast is planted.",
                "findings": ["The input fixes the observed contrast."],
                "evidence_gaps": [], "requested_actions": [],
                "repair_plan": repair_plan,
            }),
            "fake", {"model_calls": 1, "output_tokens": 300}, 0.01, "stop", 1,
        )
        assignment = {
            "assigned_role": "methods.methodologist", "role_id": "methodologist",
            "model_role": "methods.methodologist", "execution_kind": "model",
            "stage_id": "experiment-repair-panel", "stage_kind": "experiment",
            "quota": {"max_calls": 1, "max_input_tokens": 60000,
                      "max_output_tokens": 2048, "max_seconds": 10},
            "_response_contract": "repair_adjudication",
            "_prompt": json.dumps({"decision_contract": "repair adjudication"}),
        }
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.return_value = result
            report = SpecialistDispatcher(
                model, max_parallel=1, deadline=time.monotonic() + 10,
            ).dispatch([assignment], {"topic_id": "direction_3"})[0]

        self.assertEqual(report["status"], "succeeded", report.get("error"))
        dispatched_call = client.return_value.complete.call_args
        self.assertEqual(dispatched_call.kwargs["system"], REPAIR_ADJUDICATION_SYSTEM)
        self.assertNotEqual(dispatched_call.kwargs["system"], SPECIALIST_SYSTEM)
        self.assertEqual(report["response"]["raw"]["repair_plan"], repair_plan)

    def test_repair_adjudication_format_error_is_repaired_before_scientific_review(self):
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                 "model": "fixture", "timeout_seconds": 5, "max_output_tokens": 2048,
                 "context_window_tokens": 65536, "max_input_tokens": 60000}
        packet = {"failure_dossier_ref": "artifact:failure@1", "scientific_evidence": "unchanged"}
        assignment = {"assigned_role": "methods.methodologist", "role_id": "methodologist",
                      "model_role": "methods.methodologist", "execution_kind": "model",
                      "stage_id": "experiment-repair-panel", "stage_kind": "experiment",
                      "quota": {"max_calls": 2, "max_input_tokens": 60000,
                                "max_output_tokens": 4096, "max_seconds": 10},
                      "_response_contract": "repair_adjudication", "_prompt": json.dumps(packet)}
        response = {"decision": "repair", "summary": "Inspect the estimand.", "findings": [],
                    "evidence_gaps": [], "requested_actions": [], "repair_plan": {
                        "disposition": "repair", "root_cause": {"statement": "unchanged", "evidence": []},
                        "required_changes": [], "acceptance_checks": [{"phase": "plan",
                            "check": "Inspect the declared estimand.", "is_falsifiable": True,
                            "phase_owner": "methods.methodologist"}]}}
        original = json.dumps(response)
        calls = []
        def reply(*, system, prompt, **kwargs):
            calls.append(json.loads(prompt))
            if len(calls) > 1:
                self.assertEqual(calls[-1]["evidence_packet"], packet)
                self.assertEqual(calls[-1]["response_format_repair"]["diagnostic"]["kind"], "output_contract")
                response["repair_plan"]["acceptance_checks"][0] = {
                    "phase": "plan", "check": "Inspect the declared estimand."}
            return ModelResult(json.dumps(response), "fixture", {"model_calls": 1, "output_tokens": 100}, .01, "stop", 1)
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = reply
            report = SpecialistDispatcher(model, max_parallel=1, deadline=time.monotonic() + 10).dispatch(
                [assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report.get("error"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(report["validation_retries"], 1)
        self.assertEqual(report["retry_history"][0]["response_text"], original)
        self.assertEqual(report["retry_history"][0]["response_sha256"], hashlib.sha256(original.encode()).hexdigest())
        self.assertEqual(report["response"]["raw"]["repair_plan"], response["repair_plan"])

    def test_exhausted_repair_adjudication_format_is_an_output_contract_failure(self):
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                 "model": "fixture", "timeout_seconds": 5, "max_output_tokens": 2048}
        response = {"decision": "repair", "summary": "Inspect.", "findings": [],
                    "evidence_gaps": [], "requested_actions": [], "repair_plan": {
                        "disposition": "repair", "root_cause": {"statement": "unchanged", "evidence": []},
                        "required_changes": [], "acceptance_checks": [{"phase": "execution",
                            "check": "Replay.", "extra": "ambiguous"}]}}
        assignment = {"assigned_role": "methods.methodologist", "model_role": "methods.methodologist",
                      "execution_kind": "model", "stage_id": "panel", "stage_kind": "experiment",
                      "quota": {"max_calls": 2, "max_output_tokens": 4096, "max_seconds": 10},
                      "_response_contract": "repair_adjudication", "_prompt": "{}"}
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.return_value = ModelResult(json.dumps(response), "fixture",
                {"model_calls": 1, "output_tokens": 100}, .01, "stop", 1)
            report = SpecialistDispatcher(model, max_parallel=1, deadline=time.monotonic() + 10).dispatch(
                [assignment], {})[0]
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failure"]["kind"], "output_contract")
        self.assertNotIn("response", report)
        self.assertEqual(json.loads(report["partial_response"]), response)

    def test_review_response_contracts_preserve_complete_evidence_without_word_ceilings(self):
        self.assertIn("at most three", SPECIALIST_SYSTEM)
        self.assertIn("a longer item is preferable to omitting material support", SPECIALIST_SYSTEM)
        self.assertIn("blocking_findings, required_revisions, deferred_gates", VERIFIER_SYSTEM)
        self.assertIn("without omitting material support or applying word-count limits", VERIFIER_SYSTEM)
        for normalise, payload in (
            (_normalise_report, {
                "decision": "repair", "summary": "One short reason.",
                "findings": ["The evidence field shows the stated defect."],
                "evidence_gaps": [], "requested_actions": [],
            }),
            (_normalise_verdict, {
                "decision": "hold", "rationale": "The result is not independently supported.",
                "critical_findings": ["The result omits the recalculated interval."],
                "repair_scope": [],
            }),
        ):
            normalized = normalise(payload)
            self.assertLessEqual(len(normalized.get("findings", normalized.get("critical_findings", []))), 3)
            self.assertLessEqual(len(normalized.get("requested_actions", normalized.get("repair_scope", []))), 3)
        detailed = {
            "decision": "repair",
            "summary": "A supported explanation. " * 30,
            "findings": ["Evidence and consequence. " * 8, "Second finding.",
                         "Third finding.", "Fourth finding.", "Fifth finding.",
                         "Sixth finding.", "Seventh finding.", "Eighth finding.",
                         "Ninth finding."],
            "evidence_gaps": [], "requested_actions": [],
        }
        normalized = _normalise_report(detailed)
        self.assertGreater(len(normalized["summary"].split()), 80)
        self.assertEqual(len(normalized["findings"]), 9)
        self.assertIn("Ninth finding", normalized["findings"][-1])
        self.assertIn("Ninth finding", normalized["raw"]["findings"][-1])
        self.assertEqual(normalized["normalization_warnings"], [])

        long_item = {
            "decision": "repair", "summary": "Short.",
            "findings": ["evidence " * 400], "evidence_gaps": [],
            "requested_actions": [],
        }
        normalized_long_item = _normalise_report(long_item)
        self.assertEqual(len(normalized_long_item["findings"][0]), len(long_item["findings"][0]))
        self.assertEqual(normalized_long_item["raw"]["findings"][0], long_item["findings"][0])

        detailed_verdict = {
            "decision": "hold", "rationale": "The evidence is incomplete. " * 21,
            "critical_findings": ["Evidence-linked defect. " * 8, "Second.", "Third.",
                                 "Fourth."],
            "repair_scope": [],
        }
        normalized_verdict = _normalise_verdict(detailed_verdict)
        self.assertGreater(len(normalized_verdict["rationale"].split()), 80)
        self.assertEqual(len(normalized_verdict["blocking_findings"]), 4)
        self.assertEqual(normalized_verdict["critical_findings"],
                         detailed_verdict["critical_findings"])

        revisable = _normalise_verdict({
            "decision": "accept", "rationale": "The current plan is safe.",
            "blocking_findings": [],
            "required_revisions": [],
            "deferred_gates": ["Verify generated source hashes before execution."],
            "repair_scope": ["Clarify the reporting note in the manuscript."],
        })
        self.assertEqual(revisable["blocking_findings"], [])
        self.assertEqual(revisable["deferred_gates"],
                         ["Verify generated source hashes before execution."])

    def test_specialist_prompt_redacts_credentials_embedded_in_source_text(self):
        source = (
            "export SCISAURUS_OPENALEX_API_KEY=TEST-OPENALEX-KEY-0123456789ABCDEF\n"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"
            "Authorization: Basic dXNlcjpwYXNzd29yZA==\n"
            "client_secret='sk-proj-123456789012345678901234567890'\n"
            'config = {"password": "json-secret-value-for-test"}\n'
            "aws_access_key=AKIA1234567890ABCDEF\n"
            "https://user:fake-password@example.test/private\n"
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n"
            "-----END PRIVATE KEY-----"
        )
        safe_source = redact_sensitive_text(source)
        self.assertNotIn("TEST-OPENALEX-KEY-0123456789ABCDEF", safe_source)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz012345", safe_source)
        self.assertNotIn("dXNlcjpwYXNzd29yZA==", safe_source)
        self.assertNotIn("sk-proj-123456789012345678901234567890", safe_source)
        self.assertNotIn("json-secret-value-for-test", safe_source)
        self.assertNotIn("AKIA1234567890ABCDEF", safe_source)
        self.assertNotIn("user:fake-password", safe_source)
        self.assertNotIn("private-material", safe_source)
        self.assertEqual(redact_sensitive_text(safe_source), safe_source)

        prompt = json.loads(build_specialist_prompt({
            "assigned_role": "methods.methodologist",
            "model_role": "methods.methodologist",
            "stage_id": "experiment",
            "stage_kind": "experiment",
            "role_id": "methodologist",
            "system_contract": "Inspect the supplied executable source.",
            "input_projection": ["failure_evidence"],
            "quota": {"max_input_tokens": 245760},
        }, {
            "failure_evidence": {
                "source_files": {"executor": {"source_chunks": [source]}},
            },
        }))
        encoded_prompt = json.dumps(prompt, ensure_ascii=False)
        self.assertNotIn("TEST-OPENALEX-KEY-0123456789ABCDEF", encoded_prompt)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz012345", encoded_prompt)
        self.assertNotIn("dXNlcjpwYXNzd29yZA==", encoded_prompt)
        self.assertNotIn("sk-proj-123456789012345678901234567890", encoded_prompt)
        self.assertNotIn("json-secret-value-for-test", encoded_prompt)
        self.assertNotIn("AKIA1234567890ABCDEF", encoded_prompt)
        self.assertNotIn("user:fake-password", encoded_prompt)
        self.assertNotIn("private-material", encoded_prompt)
        self.assertIn("[redacted private key]", encoded_prompt)

    def test_specialist_prompt_preserves_deeply_nested_evidence_ids(self):
        assignment = {
            "assigned_role": "strategy.evidence-linker",
            "model_role": "strategy.argument-reviewer",
            "role_id": "evidence-linker", "stage_id": "argument",
            "stage_kind": "argument", "system_contract": "Audit source links.",
            "input_projection": ["evidence_records"],
            "quota": {"max_input_tokens": 12000},
        }
        packet = {
            "objective": "Check the exact evidence IDs.", "stage_id": "argument",
            "stage_kind": "argument",
            "evidence_records": [{
                "id": "record-1",
                "metadata": {"source": {"record": {"evidence_ids": ["evidence-1"]}}},
            }],
        }
        projected = json.loads(build_specialist_prompt(assignment, packet))["projected_input"]
        self.assertEqual(
            projected["evidence_records"][0]["metadata"]["source"]["record"]["evidence_ids"],
            ["evidence-1"],
        )

    def test_tight_specialist_quota_preserves_named_scientific_inputs(self):
        assignment = {
            "assigned_role": "strategy.evidence-linker",
            "model_role": "strategy.argument-reviewer",
            "role_id": "evidence-linker", "stage_id": "argument",
            "stage_kind": "argument", "system_contract": "Audit claim links.",
            "input_projection": ["claims", "evidence_records", "argument_plan"],
            "quota": {"max_input_tokens": 1800},
        }
        packet = {
            "objective": "Audit the supplied claims.", "stage_id": "argument",
            "stage_kind": "argument",
            "claims": [{"id": f"claim-{i}", "text": "claim text " * 100,
                        "evidence_ids": [f"evidence-{i}"]} for i in range(80)],
            "evidence_records": [{"id": f"evidence-{i}", "description": "record " * 100,
                                  "status": "observed"} for i in range(80)],
            "argument_plan": {"research_question": "Does the measured sign cross?",
                              "hypotheses": [{"id": "h1", "mechanism": "A"},
                                             {"id": "h2", "mechanism": "B"}],
                              "notes": ["large context " * 100 for _ in range(40)]},
        }

        prompt = build_specialist_prompt(assignment, packet)
        body = json.loads(prompt)
        projected = body["projected_input"]
        self.assertLessEqual(estimate_input_tokens(SPECIALIST_SYSTEM, prompt), 1800)
        self.assertNotIn("bounded_context", body)
        self.assertIsInstance(projected["claims"], list)
        self.assertIsInstance(projected["evidence_records"], list)
        self.assertEqual(projected["claims"][0]["id"], "claim-0")
        self.assertEqual(projected["claims"][0]["evidence_ids"], ["evidence-0"])
        self.assertEqual(projected["evidence_records"][0]["id"], "evidence-0")
        self.assertEqual(projected["argument_plan"]["research_question"],
                         "Does the measured sign cross?")

    def test_specialist_projection_fails_closed_if_contract_cannot_fit(self):
        assignment = {
            "assigned_role": "research.cataloger", "model_role": "research.literature-mapper",
            "role_id": "cataloger", "stage_id": "survey", "stage_kind": "survey",
            "system_contract": "Normalize source evidence.", "input_projection": ["topic"],
            "quota": {"max_input_tokens": 32},
        }
        with self.assertRaisesRegex(ValidationError, "cannot preserve its declared fields"):
            build_specialist_prompt(assignment, {
                "objective": "o", "stage_id": "survey", "stage_kind": "survey",
                "topic": {"question": "A bounded question"},
            })

    def test_topic_maturity_prompt_preserves_scientific_records_under_role_quota(self):
        assignment = {
            "assigned_role": "research.topic-maturity-reviewer",
            "model_role": "research.topic-maturity-reviewer",
            "role_id": "topic-maturity-reviewer", "stage_id": "topic",
            "stage_kind": "topic_discovery", "system_contract": "Review the topic.",
            "input_projection": ["candidate_topics", "frontier_seeds", "prior_work",
                                  "experiment_feasibility"],
            "quota": {"max_input_tokens": 12000},
        }
        candidates = [{
            "id": f"direction-{index}", "title": f"Question {index}",
            "domain": "Computational ecology", "research_question": "Does mechanism A change B?",
            "phenomenon": "A bounded phenomenon", "mechanism": "A testable mechanism",
            "comparison": "Mechanism versus matched null", "comparison_type": "causal_contrast",
            "disconfirmation_test": "Reject if the effect is absent.",
            "measurement": "A predeclared estimand", "scope": "A bounded scope",
            "research_form": "experimental_design", "evidence_mode": "synthetic_simulation",
            "data_regime": "A fixed synthetic regime", "theory_target": "A threshold",
            "feasibility": "Runs with numpy", "resource_plan": "Seeded simulation",
            "why_promising": "Separates two mechanisms", "frontier_seed_id": "seed-1",
            "prior_work_ids": ["W1"], "search_queries": ["mechanism A matched null"],
            "capability_requirements": {"executables": ["python3"],
                                         "python_packages": ["numpy"], "stage_kinds": ["experiment"]},
        } for index in range(4)]
        seeds = [{
            "id": f"seed-{index}", "domain": "Computational ecology",
            "phenomenon": "A frontier phenomenon", "mechanism": "A frontier mechanism",
            "unit_of_analysis": "A network", "search_queries": ["frontier search terms"],
        } for index in range(6)]
        prior_work = [{
            "work_id": f"W{index}", "title": f"Prior study {index}", "year": 2020,
            "authors": ["Author"], "doi": f"10.1000/{index}",
            "source_url": f"https://openalex.org/W{index}",
            "abstract": "source-grounded evidence " * 300,
            "matched_query": "frontier search terms", "frontier_domain": "Computational ecology",
            "frontier_seed_id": "seed-1",
        } for index in range(12)]
        packet = {
            "objective": "Study the declared question.", "stage_id": "topic",
            "stage_kind": "topic_discovery", "stage_result": {
                "candidate_topics": candidates, "frontier_seeds": seeds,
                "prior_work": prior_work,
                "experiment_feasibility": {"status": "feasible",
                                            "requirements": {"executables": ["python3"],
                                                              "python_packages": ["numpy"],
                                                              "stage_kinds": ["experiment"]},
                                            "unavailable": []},
            },
        }
        prompt = build_specialist_prompt(assignment, packet)
        self.assertLessEqual(estimate_input_tokens(SPECIALIST_SYSTEM, prompt), 12000)
        self.assertNotIn("[truncated]", prompt)
        projected = json.loads(prompt)["projected_input"]
        self.assertEqual([item["id"] for item in projected["candidate_topics"]],
                         [f"direction-{index}" for index in range(4)])
        self.assertEqual([item["id"] for item in projected["frontier_seeds"]],
                         [f"seed-{index}" for index in range(6)])
        self.assertEqual([item["work_id"] for item in projected["prior_work"]],
                         [f"W{index}" for index in range(12)])
        self.assertEqual(projected["experiment_feasibility"]["status"], "feasible")

    def test_verifier_prompt_fits_adversary_quota_with_large_stage_result(self):
        chief_result = {
            "status": "completed", "question": "A bounded question",
            "topic": {
                "id": "candidate-1", "title": "A source-grounded topic",
                "research_question": "Can the measured signal distinguish two mechanisms?",
                "disconfirmation_test": "The model fails across the held-out condition.",
            },
            "source_challenge": {
                "decision": "admit_to_survey",
                "rationale": "The anchor source supports the phenomenon but not the quantitative test.",
            },
            "candidates": [{"id": str(i), "research_question": "q" * 3000} for i in range(20)],
            "recent_papers": [{"work_id": str(i), "abstract": "a" * 3000} for i in range(20)],
            "runtime_context": {"dependencies": "x" * 40000},
        }
        reports = [{
            "assigned_role": "research.cataloger", "status": "succeeded",
            "response": {"summary": "checked", "findings": ["f" * 2000 for _ in range(10)],
                         "raw": "do not forward" * 1000},
        }]
        prompt = build_verifier_prompt(
            {"id": "survey", "kind": "survey"},
            {"objective": "Study the declared question."}, reports, chief_result,
            max_input_tokens=16000)
        self.assertLessEqual(estimate_input_tokens(VERIFIER_SYSTEM, prompt), 16000)
        parsed = json.loads(prompt)
        self.assertNotIn("runtime_context", parsed["chief_result"])
        self.assertNotIn("raw", parsed["specialist_reports"][0]["response"])
        self.assertEqual(parsed["chief_result"]["topic"]["id"], "candidate-1")
        self.assertEqual(
            parsed["chief_result"]["source_challenge"]["decision"], "admit_to_survey")
        self.assertNotIn("[truncated]", prompt)
        self.assertIn("f" * 900, prompt)

    def test_repair_verifier_compacts_duplicate_failure_dossier_before_dispatch(self):
        failure = (
            "adversarial review rejected: failed_checks include a degenerate estimand; "
            "the validator tests a synthetic arm; executor and declared method disagree. "
        ) + ("critical failure evidence " * 120)
        packet = {
            "schema_version": "capability-repair-packet-1",
            "stage_id": "experiment", "continuation_cycle": 339,
            "input_sha256": "a" * 64,
            "topic": {
                "id": "direction-1", "title": "A bounded research question",
                "research_question": "Does the intervention change the ordering?",
                "comparison": "Full rate versus ablated rate",
                "disconfirmation_test": "The ordering remains invariant under intervention.",
            },
            "failure": {
                "error": failure,
                "failure_debt": {"attempts": 141, "error": failure,
                                 "failure_class": "experiment_failure",
                                 "failure_dossier_ref": "artifact:failure@1"},
                "prior_status": "research_expansion_required",
                "review_status": "scientific_assignment_blocked",
            },
            "failure_observed_result": {"error": failure, "status": "blocked"},
            "failure_recovery": {
                "failure_class": "experiment_failure", "recovery_mode": "repair_then_rerun",
                "acceptance_checks": ["independent recalculation " * 80 for _ in range(5)],
                "repair_commands": ["inspect source " * 100 for _ in range(5)],
                "review_directives": ["change mechanism " * 100 for _ in range(12)],
            },
            "prior_foundry_work": {
                "status": "blocked", "attempts": 2,
                "feedback": failure,
                "last_attempt": {"executor_source": "executor-source " * 450,
                                 "validator_source": "validator-source " * 450},
                "validation_context": {"observation_count": 40,
                                       "metrics": [{"id": "rank", "value_repr": "0.98"}]},
                "validation_feedback": {
                    "gate": "independent_recalculation", "decision": "rejected",
                    "failed_checks": [{"id": "estimand_sensitivity", "outcome": "failed",
                                       "evidence": "The recorded estimand is invariant."}],
                },
            },
            "observed_result": {"assessment_ref": "artifact:assessment@1",
                                "results_package": "results.json"},
            "program_snapshot": [{"path": "executor.py", "sha256": "b" * 64,
                                  "size_bytes": 8000, "source": "def execute():\n" + "source " * 900,
                                  "source_truncated": False}],
            "prior_specialist_reviews": [{
                "role_id": f"reviewer-{i}", "assigned_role": f"methods.reviewer-{i}",
                "status": "succeeded", "decision": "repair",
                "summary": "Review summary " * 100,
                "findings": ["Finding " * 200],
                "requested_actions": ["Action " * 200],
            } for i in range(4)],
            "prior_verifier": {"status": "succeeded", "decision": "accept",
                               "critical_findings": ["Prior critical finding " * 100],
                               "repair_scope": ["Change the estimand " * 100]},
            "repair_contract": {
                "must_preserve": ["the research question"],
                "must_change": ["the scientific mechanism"],
                "must_prove": ["the estimand responds to intervention"],
                "prohibited": ["threshold relabeling"],
            },
        }
        chief_result = {
            "research_question": "Does the intervention change the ordering?",
            "claims": [{"claim": "The ablation changes order", "evidence": "rank"}],
            "capability_repair_packet": packet,
        }
        reports = [{
            "assigned_role": f"methods.reviewer-{i}", "status": "succeeded",
            "response": {"decision": "repair", "summary": "Independent review " * 120,
                         "findings": ["Current finding " * 100 for _ in range(4)],
                         "evidence_gaps": ["Evidence gap " * 80],
                         "requested_actions": ["Rebuild estimator " * 80]},
        } for i in range(4)]

        prompt = build_verifier_prompt(
            {"id": "experiment-repair-panel", "kind": "experiment"},
            {"objective": "Assess the executable repair and evidence.",
             "repair_panel": True, "capability_repair_packet": packet},
            reports, chief_result, max_input_tokens=16000)
        self.assertLessEqual(estimate_input_tokens(VERIFIER_SYSTEM, prompt), 16000)
        body = json.loads(prompt)
        repair = body["capability_repair_packet"]
        self.assertEqual(repair["prior_foundry_work"]["validation_feedback"]["failed_checks"][0]["id"],
                         "estimand_sensitivity")
        self.assertIn("scientific mechanism", repair["repair_contract"]["must_change"][0])
        self.assertEqual(repair["program_snapshot"][0]["sha256"], "b" * 64)
        self.assertNotIn("failure_observed_result", repair)
        self.assertNotIn("last_attempt", repair["prior_foundry_work"])

    def test_pre_execution_repair_verifier_reviews_the_plan_not_missing_results(self):
        plan = {
            "schema_version": "experiment-repair-adjudication-2",
            "topic_id": "direction_3",
            "failure_lineage": {"stage_id": "experiment", "attempt_number": 4},
            "disposition": "repair",
            "root_cause": {"statement": "The intervention cancels.",
                            "evidence": ["The output is constant."]},
            "required_changes": [{"target": "executor", "instruction": "Change the state update."}],
            "acceptance_checks": [{"phase": "plan", "check": "Identify the declared primary estimand."},
                                  {"phase": "execution", "check": "Recalculate independently."}],
        }
        prompt = build_verifier_prompt(
            {"id": "experiment-repair-panel", "kind": "experiment"},
            {
                "objective": "Review a proposed repair.",
                "repair_panel": True,
                "repair_verification_scope": "pre_execution_plan",
                "capability_repair_packet": {
                    "failure_lineage": {"stage_id": "experiment", "attempt_number": 4},
                    "repair_contract": {"must_preserve": ["question"]},
                },
            }, [], {"repair_adjudication": plan}, max_input_tokens=16000)
        payload = json.loads(prompt)
        self.assertEqual(payload["chief_result"]["repair_adjudication"]["topic_id"],
                         "direction_3")
        self.assertEqual(payload["verifier_contract"]["acceptance_target"],
                         "the scoped methods repair plan before source authoring or execution")
        self.assertIn("Do not hold solely", payload["verifier_contract"]["repair_panel_rule"])
        from scisaurus.runtime.specialists import SCIENTIFIC_REPAIR_ACCEPTANCE_RULE, REPAIR_CHECK_PHASE_RULE
        self.assertEqual(payload["chief_result"]["repair_adjudication"], plan)
        self.assertIn(REPAIR_CHECK_PHASE_RULE, payload["verifier_contract"]["repair_panel_rule"])
        self.assertNotIn("must be checked before execution", payload["verifier_contract"]["repair_panel_rule"])
        self.assertIn("during or after execution", payload["verifier_contract"]["repair_panel_rule"])
        self.assertIn("estimand ambiguous", payload["verifier_contract"]["repair_panel_rule"])
        self.assertIn(SCIENTIFIC_REPAIR_ACCEPTANCE_RULE,
                      payload["verifier_contract"]["repair_panel_rule"])
        self.assertIn(SCIENTIFIC_REPAIR_ACCEPTANCE_RULE, REPAIR_ADJUDICATION_SYSTEM)

    def test_current_plan_review_scopes_historical_verdict_without_losing_requirements(self):
        plan = {"disposition": "repair", "required_changes": [
            {"target": "estimand", "instruction": "Declare one estimator."}],
            "acceptance_checks": [{"phase": "execution", "check": "Recalculate the declared estimator."}]}
        prior_plan = {"disposition": "repair", "required_changes": ["ambiguous estimator"]}
        prior = {"prior_plan": prior_plan, "lead_review": {"summary": "Historical prose." * 2000},
            "verifier_review": {"decision": "hold", "rationale": "Old judgment." * 2000,
                "blocking_findings": ["Declare one estimator."], "required_revisions": ["Bind its recalculation."],
                "artifact_ref": "artifact:prior-verifier@1"}, "source_scope_matches": True,
            "evidence_dependencies": [{"ref": "artifact:prior-evidence@1"}]}
        chief = {"repair_adjudication": plan, "prior_plan_review": prior}
        packet = {"repair_panel": True, "repair_verification_scope": "pre_execution_plan",
            "capability_repair_packet": {"plan_review_failure": {
                "error": "old plan hold", "source_authority": "The current plan review failed"}}}
        from scisaurus.runtime.capability_foundry import candidate_prompt, validator_output_contract
        contract = validator_output_contract()
        self.assertEqual(candidate_prompt('comparison', [], {})['validator_output_exact_shapes'], contract)
        packet['capability_repair_packet']['repair_contract'] = {
            'executable_validator_output': contract, 'protocol_ownership': 'Program protocol, not model-report protocol.'}
        for limit in (32000, 8000):
            prompt = build_verifier_prompt({"id": "repair", "kind": "experiment"}, packet, [], chief,
                                          max_input_tokens=limit)
            value = json.loads(prompt)
            self.assertEqual(value['chief_result']['repair_adjudication'], plan)
            self.assertEqual(value['verifier_contract']['review_subject']['sha256'],
                             hashlib.sha256(canonical_bytes(plan)).hexdigest())
            history = value['chief_result']['prior_plan_review']
            self.assertEqual(history['prior_plan_sha256'], hashlib.sha256(canonical_bytes(prior_plan)).hexdigest())
            self.assertEqual(history['requirements_to_reassess'], {
                'blocking_findings': ['Declare one estimator.'], 'required_revisions': ['Bind its recalculation.']})
            self.assertEqual(history['evidence_dependencies'], prior['evidence_dependencies'])
            for field in ('prior_plan', 'lead_review', 'verifier_review', 'decision'):
                self.assertNotIn(field, history)
            failure = value['capability_repair_packet']['plan_review_failure']
            self.assertEqual(value['capability_repair_packet']['repair_contract']['executable_validator_output'], contract)
            self.assertEqual(contract['decision'], 'accepted|rejected')
            self.assertEqual(set(contract['checks'][0]), {'id', 'outcome', 'evidence'})
            self.assertEqual(failure['temporal_scope'], 'historical_prior_plan_review')
            self.assertNotIn('current plan review failed', failure['source_authority'])
            repaired = json.loads(_verifier_repair_prompt(prompt, 'schema error', '{}',
                max_input_tokens=limit))
            self.assertEqual(repaired['evidence_packet'], value)
        self.assertEqual(chief['prior_plan_review'], prior)
        from scisaurus.runtime.specialists import _historical_plan_requirements
        for review in ({'blocking_findings': ['Current required design.'], 'required_revisions': ['Revise the comparator.']},
                       {**prior, 'blocking_findings': ['Current required design.'],
                        'required_revisions': ['Revise the comparator.']}):
            history = _historical_plan_requirements(review)
            self.assertIn('Current required design.', history['requirements_to_reassess']['blocking_findings'])
            self.assertIn('Revise the comparator.', history['requirements_to_reassess']['required_revisions'])
            if 'verifier_review' in review:
                self.assertIn('Declare one estimator.', history['requirements_to_reassess']['blocking_findings'])

    def test_repair_verifier_retains_the_leads_source_evidence_at_every_detail(self):
        from scisaurus.runtime.specialists import (
            _verifier_repair_packet, build_repair_adjudication_prompt,
        )
        sources = {
            "executor": "def measure(state):\n    denominator = state['normalizer']\n    return None if denominator == 0 else state['work'] / denominator\n",
            "validator": "def recalculate(rows):\n    return [row['work'] / row['normalizer'] if row['normalizer'] != 0 else None for row in rows]\n",
        }
        packet = {"program_snapshot": [], "exact_candidate_sources": {},
                  "failure_lineage": {"stage_id": "experiment", "attempt_number": 18},
                  "repair_subject_lineage": {"stage_id": "experiment", "attempt_number": 14},
                  "plan_review_failure": {"error": "plan rejected"},
                  "prior_foundry_work": {"validation_context": {
                      "observation_count": 70, "numeric_observation_fields": {
                          "W": {"finite_count": 70, "max": 0.0}}},
                      "last_attempt": {"experiment_intent": {
                      "question": "Does the intervention change extraction efficiency?",
                      "primary_outcomes": ["correlation"]}}}}
        for name, source in sources.items():
            packet["exact_candidate_sources"][name] = {
                "available": True, "source_chunks": [source[:35], source[35:]],
                "prompt_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "prompt_source_characters": len(source),
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "source_characters": len(source), "redaction_applied": False,
            }
        lead = json.loads(build_repair_adjudication_prompt(
            {"quota": {"max_input_tokens": 24000}}, packet, []))
        from scisaurus.runtime.evidence import scientific_input_recovery_contract
        self.assertEqual(lead["scientific_input_recovery"], scientific_input_recovery_contract())
        self.assertNotIn("topic_id", lead["decision_contract"]["output_schema"]["repair_plan"])
        expected = lead["repair_adjudication_packet"]["candidate_program"]
        self.assertEqual(lead["repair_adjudication_packet"]["validation_context"]["observation_count"], 70)
        self.assertEqual(lead["repair_adjudication_packet"]["validation_context"]["numeric_observation_fields"]["W"]["max"], 0.0)
        for detail in ("full", "compact", "minimal", "focused"):
            projected = _verifier_repair_packet(packet, detail=detail)
            self.assertEqual(projected["candidate_program"], expected)
            self.assertEqual(expected["repair_subject_lineage"]["attempt_number"], 14)
            self.assertEqual(projected["failure_lineage"]["attempt_number"], 18)
            self.assertEqual(projected["plan_review_failure"]["error"], "plan rejected")
            self.assertEqual(projected["prior_foundry_work"]["validation_context"]["observation_count"], 70)
            for name, source in sources.items():
                evidence = projected["candidate_program"]["exact_execution_sources"][name]
                self.assertTrue(evidence["complete"])
                self.assertEqual("".join(evidence["source_chunks"]), source)
        packet["exact_candidate_sources"]["executor"]["source_chunks"][0] += "modified"
        evidence = _verifier_repair_packet(packet)["candidate_program"]["exact_execution_sources"]["executor"]
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["source_chunks"], [])

    def test_repair_verifier_preserves_the_entire_selected_plan_at_every_detail(self):
        from scisaurus.runtime.specialists import _verifier_chief_result
        from scisaurus.core.schema import canonical_bytes
        plan = {"topic_id": "topic", "disposition": "repair", "root_cause": {
            "statement": "Source diagnostics. " * 120, "evidence": ["Exact measurement. " * 100]},
            "required_changes": [{"target": "operator", "instruction": "Physical basis. " * 180 + "K = X + g*(X@Z+Z@X); g=0.5.",
                "scientific_basis": "The intervention must be independently identifiable. " * 50,
                "source_refs": ["executor"]}] * 6,
            "acceptance_checks": [{"phase": "plan" if index == 0 else "execution",
                                   "check": f"check-{index}: " + "independent calculation. " * 70}
                                  for index in range(16)],
            "residual_uncertainties": ["uncertainty. " * 100] * 8}
        chief = {"decision": "repair", "repair_adjudication": plan}
        expected = hashlib.sha256(canonical_bytes(plan)).hexdigest()
        for detail in ("full", "compact", "minimal", "focused"):
            projected = _verifier_chief_result(chief, detail=detail)
            self.assertEqual(projected["repair_adjudication"], plan)
            self.assertEqual(projected["repair_adjudication_sha256"], expected)
        with self.assertRaisesRegex(ValidationError, "input quota"):
            build_verifier_prompt({"id": "repair", "kind": "experiment"},
                {"repair_panel": True, "repair_verification_scope": "pre_execution_plan", "capability_repair_packet": {}},
                [], chief, max_input_tokens=1200)

    def test_repair_verifier_rejects_quota_overflow_without_discarding_exact_source(self):
        source = "def observe(rows):\n    return [row['raw_measurement'] for row in rows]\n" * 1000
        packet = {"program_snapshot": [], "exact_candidate_sources": {
            "executor": {"available": True, "source_chunks": [source],
                         "prompt_source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                         "prompt_source_characters": len(source)}}}
        with self.assertRaisesRegex(ValidationError, "projection exceeds its input quota"):
            build_verifier_prompt(
                {"id": "repair", "kind": "experiment"},
                {"repair_panel": True, "capability_repair_packet": packet},
                [], {}, max_input_tokens=2000)

    def test_topic_verifier_judges_provisional_result_against_survey_admission(self):
        chief_result = {
            "status": "completed",
            "admission_state": "provisional_for_survey",
            "next_evidence_action": "literature_survey",
            "maturity_open_requirements": [
                "Verify whether the comparison is unresolved in prior work.",
            ],
            "topic": {
                "id": "candidate-1",
                "title": "A searchable provisional question",
                "research_question": "Does condition A separate mechanisms B and C?",
                "search_queries": ["condition A mechanism B mechanism C"],
            },
        }
        prompt = build_verifier_prompt(
            {"id": "topic", "kind": "topic_discovery"},
            {"objective": "Find a testable research direction."}, [], chief_result,
            max_input_tokens=16000)
        parsed = json.loads(prompt)
        projected = parsed["chief_result"]
        self.assertEqual(projected["admission_state"], "provisional_for_survey")
        self.assertEqual(projected["next_evidence_action"], "literature_survey")
        self.assertEqual(
            projected["maturity_open_requirements"],
            ["Verify whether the comparison is unresolved in prior work."],
        )
        contract = parsed["verifier_contract"]
        self.assertIn("literature survey", contract["acceptance_target"])
        self.assertIn("not by themselves grounds to hold", contract["provisional_rule"])

    def test_topic_acceptance_scope_is_stage_owned_without_producer_admission_label(self):
        declared = {"current_stage_id": "question-design", "downstream_stage_ids": ["source-audit", "measurement"],
                    "acceptance_target": "A bounded searchable research question for source audit.",
                    "current_requirements": ["Preserve the question and bounded feasibility plan."],
                    "downstream_requirements": [{"target_stage_id": "source-audit", "requirement": "Corroborate baseline provenance."}]}
        from scisaurus.runtime.specialists import _verifier_body
        for label in (None, "provisional_for_survey", "another producer label"):
            chief = {"status": "completed", "admission_state": label,
                     "topic": {"id": "bounded", "research_question": "Does A alter B?"}}
            for detail in ("full", "compact", "minimal", "focused"):
                value = _verifier_body({"id": "question-design", "kind": "topic_discovery"},
                    {"stage_acceptance_contract": declared}, [], chief, detail=detail)
                contract = value["verifier_contract"]
                self.assertEqual(contract["stage_acceptance_contract"], declared)
                self.assertEqual(contract["acceptance_target"], declared["acceptance_target"])
                self.assertIn("parameter files, capability admission, execution", contract["provisional_rule"])
                self.assertIn("Hold when current evidence", contract["provisional_rule"])
            unlabelled = json.loads(build_verifier_prompt({"id": "question-design", "kind": "topic_discovery"}, {}, [], chief))
            self.assertIn("literature survey", unlabelled["verifier_contract"]["acceptance_target"])

    def test_typed_deferred_obligations_keep_complete_scope_and_reject_invalid_owners(self):
        obligation = {"target_stage_id": "source-audit", "requirement": "Exact later requirement λ\r\n" * 120,
                      "completion_check": "Verify every required clause." * 80,
                      "evidence_needed": ["Hash-bound source spans." * 80, "Captured provenance."]}
        verdict = {"decision": "accept", "rationale": "Current stage is supported.",
                   "deferred_gates": ["Legacy later gate."], "repair_scope": ["Nonblocking note."],
                   "deferred_obligations": [obligation]}
        normalized = _normalise_verdict(verdict, current_stage_id="question-design", valid_target_stage_ids=["source-audit", "measurement"])
        self.assertEqual(normalized["deferred_obligations"], [obligation])
        self.assertEqual(normalized["deferred_gates"], verdict["deferred_gates"])
        from scisaurus.runtime.specialists import _verifier_body
        for detail in ("full", "compact", "minimal", "focused"):
            body = _verifier_body({"id": "question-design", "kind": "topic_discovery"}, {},
                [{"response": normalized}], normalized, detail=detail)
            self.assertEqual(body["chief_result"]["deferred_obligations"], [obligation])
            self.assertEqual(body["specialist_reports"][0]["response"]["deferred_obligations"], [obligation])
        prompt = json.dumps({"verdict": normalized}, ensure_ascii=False)
        repaired = json.loads(_verifier_repair_prompt(prompt, "Own schema diagnostic", "", max_input_tokens=20000))
        self.assertEqual(repaired["evidence_packet"]["verdict"]["deferred_obligations"], [obligation])
        self.assertIn("deferred_obligations", repaired["response_format_repair"]["instruction"])
        with self.assertRaisesRegex(ValidationError, "input quota"):
            build_verifier_prompt({"id": "question-design", "kind": "topic_discovery"}, {}, [], normalized, max_input_tokens=100)
        for bad in ({**obligation, "target_stage_id": "question-design"},
                    {**obligation, "target_stage_id": "unowned"}, {**obligation, "requirement": ""},
                    {**obligation, "completion_check": None}, {**obligation, "evidence_needed": []},
                    {**obligation, "unexpected": "field"}, "legacy string"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _normalise_verdict({**verdict, "deferred_obligations": [bad]},
                        current_stage_id="question-design", valid_target_stage_ids=["source-audit"])

    def test_deferred_branch_and_work_kind_are_checked_against_the_owned_contract(self):
        scope = {"topic_ids": ["selected", "retained"],
                 "stage_work_kinds": {"survey": ["evidence"], "experiment": ["calculation"]}}
        order = {"target_stage_id": "survey", "topic_ids": ["retained"], "work_kind": "evidence",
                 "requirement": "Capture the retained branch's source.",
                 "completion_check": "A cited source is captured.", "evidence_needed": "Source receipt."}
        def validate(order):
            return _normalise_verdict({"decision": "accept", "deferred_obligations": [order]},
                current_stage_id="topic", valid_target_stage_ids=["survey", "experiment"], obligation_scope=scope)
        self.assertEqual(validate(order)["deferred_obligations"], [order])
        calculation = {**order, "target_stage_id": "experiment", "work_kind": "calculation"}
        self.assertEqual(validate(calculation)["deferred_obligations"], [calculation])
        for invalid in ({**order, "work_kind": "calculation"}, {**order, "work_kind": "provenance"},
                        {**order, "topic_ids": ["foreign"]}, {**order, "topic_ids": []},
                        {**order, "topic_ids": ["selected", "selected"]},
                        {key: value for key, value in order.items() if key not in {"topic_ids", "work_kind"}}):
            with self.subTest(order=invalid), self.assertRaises(ValidationError):
                validate(invalid)

    def test_unconfigured_future_gates_cannot_bypass_branch_or_work_ownership(self):
        scope = {"topic_ids": ["selected"], "stage_work_kinds": {"survey": ["evidence"]},
                 "deferred_gate_work_kinds": {"experiment": ["calculation"]}}
        gate = {"target_stage_kind": "experiment", "topic_ids": ["selected"], "work_kind": "calculation",
                "requirement": "Propagate the uncertainty band and define the evaluation grid.",
                "completion_check": "The numerical bounds trace to checked inputs and independent recalculation.",
                "evidence_needed": ["Source-bound parameter file", "Independent recalculation"]}
        def validate(value):
            return _normalise_verdict({"decision": "accept", "deferred_gates": [value]},
                current_stage_id="topic", valid_target_stage_ids=["survey"], obligation_scope=scope)
        self.assertEqual(validate(gate)["deferred_gates"], [gate])
        for invalid in (json.dumps(gate), "Calculate the band in survey", {**gate, "target_stage_kind": "survey"},
                        {**gate, "target_stage_id": "survey"}, {**gate, "work_kind": "evidence"},
                        {**gate, "topic_ids": ["foreign"]}):
            with self.subTest(gate=invalid), self.assertRaises(ValidationError):
                validate(invalid)

    def test_verifier_deferral_schema_is_derived_from_declared_stage_owners(self):
        stage = {"id": "source-audit", "kind": "survey"}
        for targets in ([], ["calculation"]):
            declared = {"current_stage_id": stage["id"], "downstream_stage_ids": targets,
                        "acceptance_target": "Bounded captured source audit."}
            packet = {"stage_acceptance_contract": declared}
            prompt = json.loads(build_verifier_prompt(stage, packet, [], {"status": "completed"}))
            contract = prompt["verifier_contract"]
            self.assertEqual(contract["deferred_obligation_ownership"]["allowed_target_stage_ids"], targets)
            self.assertEqual(contract["deferred_obligation_ownership"]["current_stage_id"], stage["id"])
            self.assertEqual(contract["stage_acceptance_contract"], declared)
            if not targets:
                self.assertEqual(contract["deferred_obligations"], [])
            else:
                self.assertTrue(contract["deferred_obligations"])
            incoming = {"target_stage_id": stage["id"], "requirement": "Capture values or record unavailable.",
                        "completion_check": "Each value has evidence or a scoped unavailable record.",
                        "evidence_needed": "Captured sources and search records."}
            with self.assertRaisesRegex(ValidationError, "current-stage requirements"):
                _normalise_verdict({"decision": "accept", "deferred_obligations": [incoming]},
                    current_stage_id=stage["id"], valid_target_stage_ids=targets)

    def test_typed_deferred_owner_validation_survives_generic_response_retry(self):
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1", "model": "fixture",
                 "timeout_seconds": 5, "max_output_tokens": 1000, "context_window_tokens": 16000, "max_input_tokens": 15000}
        declared = {"current_stage_id": "question-design", "downstream_stage_ids": ["source-audit"],
                    "acceptance_target": "Bounded question."}
        prompt = build_verifier_prompt({"id": "question-design", "kind": "topic_discovery"},
            {"stage_acceptance_contract": declared}, [], {"status": "completed"})
        assignment = {"assigned_role": "research.adversarial-reviewer", "model_role": "review.arbiter",
                      "stage_id": "question-design", "quota": {"max_calls": 2, "max_input_tokens": 15000,
                      "max_output_tokens": 2000, "max_seconds": 5}, "_prompt": prompt}
        for target in ("question-design", "unowned"):
            obligation = {"target_stage_id": target, "requirement": "Corroborate sources.",
                          "completion_check": "Check exact claims.", "evidence_needed": "Captured source spans."}
            bad = {"decision": "accept", "rationale": "Current support.", "deferred_obligations": [obligation]}
            good = {**bad, "deferred_obligations": [{**obligation, "target_stage_id": "source-audit"}]}
            with patch("scisaurus.runtime.specialists.ModelClient") as client:
                client.return_value.complete.side_effect = [ModelResult(json.dumps(value), "fixture",
                    {"model_calls": 1, "input_tokens": 10, "output_tokens": 20}, .01, "stop") for value in (bad, good)]
                report = SpecialistDispatcher(model, deadline=time.monotonic()+10).dispatch([assignment], {}, verifier=True)[0]
            self.assertEqual(report["status"], "succeeded", report.get("error"))
            self.assertEqual(report["response"]["deferred_obligations"], good["deferred_obligations"])
            self.assertEqual(report["usage"], {"model_calls": 2, "input_tokens": 20, "output_tokens": 40})
            retry = json.loads(report["request_inputs"][1]["input"]["prompt"])
            self.assertEqual(retry["evidence_packet"], json.loads(prompt))
            self.assertIs(retry["response_format_repair"]["stage_failure_evidence"], False)

    def test_provider_pool_capacity_is_real_and_reports_are_role_scoped(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SpecialistHandler)
        server.request_barrier = threading.Barrier(3)
        server.lock = threading.Lock()
        server.active = server.peak = server.requests = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            route = lambda route_id, pool, model: {
                "id": route_id, "pool": pool, "protocol": "openai_compatible",
                "base_url": base, "model": model,
            }
            model = {
                "protocol": "openai_compatible", "base_url": base,
                "model": "fallback", "timeout_seconds": 5.0,
                "max_output_tokens": 100, "context_window_tokens": 4096,
                "max_input_tokens": 2048, "role_routes": {
                    "role-a": [route("primary", "primary", "primary") , route("gemma", "ollama", "gemma")],
                    "role-b": [route("primary", "primary", "primary") , route("gemma", "ollama", "gemma")],
                    "role-c": [route("primary", "primary", "primary") , route("gemma", "ollama", "gemma")],
                    "role-d": [route("primary", "primary", "primary") , route("gemma", "ollama", "gemma")],
                },
            }
            assignments = [{
                "assigned_role": f"research.role-{letter}", "role_id": f"role-{letter}",
                "model_role": f"role-{letter}", "execution_kind": "model",
                "system_contract": "Check the bounded packet.",
                "input_projection": ["objective"], "stage_id": "topic", "stage_kind": "topic_discovery",
                "quota": {"max_calls": 1, "max_input_tokens": 1000,
                          "max_output_tokens": 100, "max_seconds": 5},
            } for letter in "abcd"]
            # Rename model roles to match the route keys while keeping the
            # public assignment IDs distinct.
            for index, assignment in enumerate(assignments):
                assignment["model_role"] = f"role-{chr(97 + index)}"
            results = SpecialistDispatcher(
                model, provider_pools={
                    "primary": {"max_concurrent": 1, "base_urls": [base]},
                    "ollama": {"max_concurrent": 3, "base_urls": [base]},
                }, max_parallel=4, deadline=time.monotonic() + 5,
            ).dispatch(assignments, {"objective": "test"})
            self.assertEqual(len(results), 4)
            self.assertTrue(all(item["status"] == "succeeded" for item in results))
            self.assertEqual(server.requests, 4)
            self.assertEqual(server.peak, 3)
            self.assertEqual(sum(item["provider_pool"] == "primary" for item in results), 1)
            self.assertEqual(sum(item["provider_pool"] == "ollama" for item in results), 3)
            self.assertEqual({item["role_id"] for item in results}, {"role-a", "role-b", "role-c", "role-d"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_verifier_repairs_malformed_json_on_next_route_and_aggregates_usage(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _VerifierRetryHandler)
        server.lock = threading.Lock()
        server.requests = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        events = []
        try:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            route = lambda route_id, model: {
                "id": route_id, "pool": "ollama", "protocol": "openai_compatible",
                "base_url": base, "model": model,
            }
            assignment = {
                "assigned_role": "research.adversarial-reviewer",
                "role_id": "adversarial-reviewer", "model_role": "review.arbiter",
                "execution_kind": "review", "stage_id": "topic",
                "stage_kind": "topic_discovery", "quota": {
                    "max_calls": 2, "max_input_tokens": 2048,
                    "max_output_tokens": 100, "max_seconds": 5,
                },
                "_prompt": json.dumps({"stage": "test"}),
            }
            model = {
                "protocol": "openai_compatible", "base_url": base,
                "model": "fallback", "timeout_seconds": 5.0,
                "max_output_tokens": 100, "context_window_tokens": 4096,
                "max_input_tokens": 2048, "role_routes": {
                    "review.arbiter": [
                        route("glm", "glm"), route("deepseek", "deepseek"),
                    ],
                },
            }
            result = SpecialistDispatcher(
                model, provider_pools={"ollama": {"max_concurrent": 2, "base_urls": [base]}},
                max_parallel=1, deadline=time.monotonic() + 5,
                on_progress=events.append,
            ).dispatch([assignment], {"stage": "test"}, verifier=True)[0]
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["response"]["decision"], "accept")
            self.assertEqual(result["validation_retries"], 1)
            self.assertEqual(result["route_id"], "deepseek")
            self.assertEqual(result["usage"]["model_calls"], 2)
            self.assertEqual(result["usage"]["input_tokens"], 20)
            self.assertEqual(result["usage"]["output_tokens"], 10)
            self.assertEqual(result["request_attempts"], 2)
            self.assertEqual(server.requests, 2)
            self.assertTrue(any(event.get("event") == "retrying" for event in events))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_specialist_continues_provider_truncation_until_stop_within_call_quota(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 5.0,
            "max_output_tokens": 8192, "context_window_tokens": 262144,
            "max_input_tokens": 245760,
        }
        assignment = {
            "assigned_role": "methods.methodologist", "role_id": "methodologist",
            "model_role": "methods.methodologist", "execution_kind": "model",
            "stage_id": "repair", "stage_kind": "experiment",
            "quota": {"max_calls": 2, "max_input_tokens": 245760,
                      "max_output_tokens": 16384,
                      "max_output_tokens_per_call": 8192, "max_seconds": 5},
            "_prompt": json.dumps({"objective": "bounded repair"}),
        }
        truncated = ModelResult(
            '{"decision":"repair"', "fake",
            {"model_calls": 1, "output_tokens": 7000}, 0.01, "length", 1)
        suffix = ModelResult(
            ',"summary":"repaired","findings":[],"evidence_gaps":[],"requested_actions":[]}',
            "fake", {"model_calls": 1, "output_tokens": 7000}, 0.01, "stop", 1)
        events = []
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = [truncated, suffix]
            result = SpecialistDispatcher(
                model, max_parallel=1, deadline=time.monotonic() + 10,
                on_progress=events.append,
            ).dispatch([assignment], {"objective": "bounded repair"})[0]
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["validation_retries"], 1)
        self.assertEqual(result["usage"]["model_calls"], 2)
        self.assertEqual(result["usage"]["output_tokens"], 14000)
        self.assertEqual(client.return_value.complete.call_count, 2)
        self.assertEqual(
            [call.kwargs["max_output_tokens"] for call in client.call_args_list],
            [8192, 8192],
        )
        first_call, continuation_call = client.return_value.complete.call_args_list
        self.assertNotIn("continuation_text", first_call.kwargs)
        self.assertEqual(continuation_call.kwargs["continuation_text"], truncated.text)
        self.assertEqual(continuation_call.kwargs["prompt"], first_call.kwargs["prompt"])
        self.assertEqual([item["kind"] for item in result["retry_history"]],
                         ["length_continuation"])
        self.assertEqual(sum(event.get("event") == "continuing" for event in events), 1)

    def test_non_json_and_closed_specialist_outputs_regenerate_without_suffix(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fixture", "timeout_seconds": 5.0,
            "max_output_tokens": 8192, "context_window_tokens": 262144,
            "max_input_tokens": 245760,
        }
        assignment = {
            "assigned_role": "methods.analysis-reviewer", "role_id": "analysis-reviewer",
            "model_role": "methods.analysis-reviewer", "execution_kind": "review",
            "stage_id": "repair", "stage_kind": "experiment",
            "quota": {"max_calls": 4, "max_input_tokens": 245760,
                      "max_output_tokens": 24576,
                      "max_output_tokens_per_call": 8192, "max_seconds": 5},
            "_prompt": json.dumps({"objective": "bounded repair"}),
        }
        for verifier in (False, True):
            for prefix in (
                    "Let me carefully analyze this packet.", "<think>Still reasoning",
                    '```json\n', '{not JSON', '{"decision":truX', '{"decision":1,]',
                    '{"decision":"repair"}',
                    '```json\n{"decision":"repair"}\n```\ntrailing text'):
                with self.subTest(verifier=verifier, prefix=prefix):
                    response = ({"decision": "accept", "rationale": "evidence checked"}
                                if verifier else {"decision": "repair", "summary": "source defect"})
                    results = [
                        ModelResult(prefix, "fixture", {"model_calls": 1,
                                    "input_tokens": 23, "output_tokens": 7000}, .01, "length"),
                        ModelResult(json.dumps(response), "fixture", {"model_calls": 1,
                                    "input_tokens": 31, "output_tokens": 100}, .01, "stop"),
                    ]
                    with patch("scisaurus.runtime.specialists.ModelClient") as client:
                        client.return_value.complete.side_effect = results
                        report = SpecialistDispatcher(model, max_parallel=1,
                            deadline=time.monotonic() + 10).dispatch(
                                [assignment], {}, verifier=verifier)[0]
                    self.assertEqual(report["status"], "succeeded")
                    self.assertEqual(report["usage"], {"model_calls": 2,
                                      "input_tokens": 54, "output_tokens": 7100})
                    self.assertEqual(report["request_attempts"], 2)
                    self.assertEqual([item["kind"] for item in report["retry_history"]],
                                     ["validation"])
                    self.assertEqual(len(report["request_inputs"]), 2)
                    for call in client.return_value.complete.call_args_list:
                        self.assertNotIn("continuation_text", call.kwargs)
                    repaired = json.loads(client.return_value.complete.call_args_list[1].kwargs["prompt"])
                    self.assertNotIn("repair_instruction", repaired)
                    self.assertNotIn("validation_error", repaired)
                    self.assertEqual(repaired["evidence_packet"], {"objective": "bounded repair"})
                    self.assertEqual(repaired["response_format_repair"]["response_owner"], {
                        "kind": "verifier" if verifier else "specialist",
                        "role": assignment["assigned_role"],
                    })

    def test_response_repair_transport_preserves_original_errors_and_contracts(self):
        original = {"chief_result": {"status": "failed", "error": "Captured executor failure"},
                    "validation_error": "Genuine stage validation error",
                    "repair_instruction": "An original evidence field", "source": "α\r\nβ\u2028γ"}
        prompt = json.dumps(original, ensure_ascii=False)
        diagnostic = "Own output schema error " + "δ" * 600
        for kind, contract in (("verifier", None), ("specialist", None),
                               ("specialist", "repair_evidence"), ("specialist", "repair_adjudication")):
            with self.subTest(kind=kind, contract=contract):
                if kind == "verifier":
                    repaired = _verifier_repair_prompt(prompt, diagnostic, "irrelevant prior output",
                        max_input_tokens=20000, output_role="research.adversarial-reviewer")
                else:
                    repaired = _specialist_repair_prompt(prompt, diagnostic, "irrelevant prior output",
                        max_input_tokens=20000, response_contract=contract, output_role="methods.methodologist")
                payload = json.loads(repaired)
                self.assertEqual(payload["evidence_packet"], original)
                self.assertEqual(set(payload), {"evidence_packet", "response_format_repair"})
                transport = payload["response_format_repair"]
                self.assertEqual(transport["diagnostic"], {"kind": "output_contract", "message": diagnostic})
                self.assertEqual(transport["response_owner"]["kind"], kind)
                self.assertEqual(transport["subject"], "previous_model_response")
                self.assertIs(transport["stage_failure_evidence"], False)
                self.assertEqual(transport["previous_response"], "irrelevant prior output")
                self.assertEqual(transport["previous_response_sha256"], hashlib.sha256(
                    b"irrelevant prior output").hexdigest())
                if contract == "repair_adjudication":
                    self.assertIn("repair_plan", transport["instruction"])
                if contract == "repair_evidence":
                    self.assertIn("evidence_note", transport["instruction"])
        for system in (SPECIALIST_SYSTEM, VERIFIER_SYSTEM, REPAIR_EVIDENCE_SYSTEM, REPAIR_ADJUDICATION_SYSTEM):
            self.assertIn("only your own previous model response", system)
            self.assertIn("Assess genuine errors", system)

    def test_response_repair_quota_fence_retains_original_packet_without_crop(self):
        for original in ({"chief_result": {"status": "completed", "source": "λ" * 500}},
                         ["complete source", {"validation_error": "original evidence"}], "raw source packet"):
            prompt = json.dumps(original, ensure_ascii=False)
            for repair in (_verifier_repair_prompt, _specialist_repair_prompt):
                with self.assertRaisesRegex(ValidationError, "identical request"):
                    repair(prompt, "Own output error", "", max_input_tokens=1)
                payload = json.loads(repair(prompt, "Own output error", "", max_input_tokens=20000))
                self.assertEqual(payload["evidence_packet"], original)

    def test_response_repair_omits_only_prior_response_when_context_cannot_fit_it(self):
        original = {"source": "complete evidence", "validation_error": "real stage error"}
        prompt = json.dumps(original)
        previous = "prior answer " * 20000
        for repair in (_verifier_repair_prompt, _specialist_repair_prompt):
            payload = json.loads(repair(prompt, "Own output error", previous, max_input_tokens=3000))
            self.assertEqual(payload["evidence_packet"], original)
            subject = payload["response_format_repair"]
            self.assertNotIn("previous_response", subject)
            self.assertEqual(subject["previous_response_omitted"]["characters"], len(previous))
            self.assertEqual(subject["previous_response_sha256"], hashlib.sha256(previous.encode()).hexdigest())
            self.assertEqual(subject["diagnostic"]["message"], "Own output error")

    def test_evidence_quote_error_names_its_exact_check_and_source_pointer(self):
        from scisaurus.runtime.specialists import validate_decision_alignment
        definition = "Exact current quantity."
        evidence = {"question_alignment": {
            "original": {"research_question": "Question?", "disconfirmation_test": "Rule."},
            "candidate": {"primary_outcomes": [{"id": "quantity", "definition": definition}]},
        }}
        pointer = "/repair_adjudication_packet/question_alignment/candidate/primary_outcomes/0/definition"
        check = {"claim": "Claim.", "pointer": pointer, "quote": "A paraphrase.",
                 "explanation": "Consequence.", "disposition": "supported"}
        plan = {"decision_alignment": {
            "original_question": "Question?", "original_decision_rule": "Rule.",
            "primary_outcome_id": "quantity", "quantity_definition": definition,
            "baseline": "Baseline.", "aggregation": "Aggregation.",
            "interpretation_limit": "Limit.", "scientific_justification": "Basis.",
            "changes_estimand": False,
        }, "required_changes": [], "evidence_checks": [check]}
        with self.assertRaisesRegex(ValidationError, r"evidence_checks\[0\]\.quote") as error:
            validate_decision_alignment(plan, evidence)
        self.assertIn(pointer, str(error.exception))
        check["quote"] = definition
        self.assertEqual(validate_decision_alignment(plan, evidence)["evidence_checks"], [check])
        evidence["source/chunks"] = ["first source", "second source"]
        plan["evidence_checks"] = [
            {**check, "pointer": "/repair_adjudication_packet/source~1chunks/0", "quote": "second source"},
            {**check, "pointer": "/repair_adjudication_packet/missing", "quote": "first source"},
            {**check, "quote": "Invented source"},
        ]
        with self.assertRaises(ValidationError) as error:
            validate_decision_alignment(plan, evidence)
        message = str(error.exception)
        self.assertIn("evidence_checks[0].quote", message)
        self.assertIn("evidence_checks[1].pointer", message)
        self.assertIn("evidence_checks[2].quote", message)
        self.assertIn('"/repair_adjudication_packet/source~1chunks/1"', message)
        self.assertIn('"/repair_adjudication_packet/source~1chunks/0"', message)
        self.assertIn("exact quote locations: []", message)
        plan["evidence_checks"] = [
            {**check, "pointer": "/repair_adjudication_packet/source~1chunks/1", "quote": "second source"},
            {**check, "pointer": "/repair_adjudication_packet/source~1chunks/0", "quote": "first source"},
        ]
        self.assertEqual(validate_decision_alignment(plan, evidence)["evidence_checks"], plan["evidence_checks"])

    def test_verifier_retry_owns_its_diagnostic_without_excusing_stage_errors(self):
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                 "model": "fixture", "timeout_seconds": 5, "max_output_tokens": 1000,
                 "context_window_tokens": 16000, "max_input_tokens": 15000}
        own_error = "truncated structured response has no JSON object prefix"
        for genuine_failure in (False, True):
            with self.subTest(genuine_failure=genuine_failure):
                original = {"chief_result": {"status": "completed", "topic": {"id": "selected", "question": "A bounded question"}}}
                if genuine_failure:
                    original["chief_result"]["error"] = "The captured producer output did not validate"
                assignment = {"assigned_role": "research.adversarial-reviewer", "role_id": "adversarial-reviewer",
                              "model_role": "review.arbiter", "stage_id": "topic", "stage_kind": "topic_discovery",
                              "quota": {"max_calls": 2, "max_input_tokens": 15000,
                                        "max_output_tokens": 2000, "max_seconds": 5},
                              "_prompt": json.dumps(original)}
                calls = []
                def evidence_error(packet):
                    evidence = packet.get("evidence_packet", packet)
                    return evidence.get("validation_error") or evidence["chief_result"].get("error")
                def reply(*, system, prompt, **kwargs):
                    calls.append(prompt)
                    if len(calls) == 1:
                        return ModelResult("Let me reason before writing JSON", "fixture",
                            {"model_calls": 1, "input_tokens": 10, "output_tokens": 20}, .01, "length")
                    packet = json.loads(prompt)
                    self.assertEqual(packet["evidence_packet"], original)
                    self.assertEqual(packet["response_format_repair"]["response_owner"],
                                     {"kind": "verifier", "role": assignment["assigned_role"]})
                    self.assertEqual(packet["response_format_repair"]["diagnostic"]["message"], own_error)
                    self.assertNotIn("validation_error", packet)
                    self.assertIn("Never cite that diagnostic as evidence against the stage", system)
                    captured_error = evidence_error(packet)
                    value = {"decision": "hold" if captured_error else "accept", "rationale": "Original stage evidence independently reviewed.",
                             "blocking_findings": [captured_error] if captured_error else [],
                             "required_revisions": [], "deferred_gates": [], "repair_scope": []}
                    return ModelResult(json.dumps(value), "fixture", {"model_calls": 1, "input_tokens": 11,
                        "output_tokens": 21}, .01, "stop")
                # An unowned top-level diagnostic reproduces the misleading stage-failure premise.
                legacy = {**original, "validation_error": own_error}
                self.assertEqual(evidence_error(legacy), own_error)
                self.assertEqual(evidence_error({"evidence_packet": original}),
                                 original["chief_result"].get("error"))
                with patch("scisaurus.runtime.specialists.ModelClient") as client:
                    client.return_value.complete.side_effect = reply
                    result = SpecialistDispatcher(model, max_parallel=1, deadline=time.monotonic() + 10).dispatch(
                        [assignment], {}, verifier=True)[0]
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["response"]["decision"], "hold" if genuine_failure else "accept")
                self.assertEqual(result["usage"], {"model_calls": 2, "input_tokens": 21, "output_tokens": 41})
                self.assertEqual(len(result["request_inputs"]), 2)
                self.assertEqual(result["retry_history"][0]["error"], own_error)

    def test_accepted_verdict_cannot_carry_unresolved_material_findings(self):
        accepted = {"decision": "accept", "rationale": "Current evidence checked.",
                    "blocking_findings": [], "required_revisions": [], "critical_findings": [],
                    "deferred_gates": ["Recalculate the observation after execution."],
                    "repair_scope": ["Clarify the reporting note."]}
        normalized = _normalise_verdict(accepted)
        self.assertEqual(normalized["decision"], "accept")
        self.assertEqual(normalized["deferred_gates"], accepted["deferred_gates"])
        self.assertEqual(normalized["repair_scope"], accepted["repair_scope"])
        for field in ("blocking_findings", "required_revisions", "critical_findings"):
            for value in (["Unresolved source-supported defect."], "Unresolved defect.", False):
                with self.subTest(field=field, value=value):
                    contradictory = {**accepted, field: value}
                    with self.assertRaisesRegex(ValidationError, "unresolved " + field):
                        _normalise_verdict(contradictory)
            held = {**accepted, "decision": "hold", field: ["Unresolved defect."]}
            self.assertEqual(_normalise_verdict(held)["decision"], "hold")

    def test_contradictory_verifier_acceptance_uses_owned_bounded_format_repair(self):
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                 "model": "fixture", "timeout_seconds": 5, "max_output_tokens": 1000,
                 "context_window_tokens": 16000, "max_input_tokens": 15000}
        original = {"chief_result": {"status": "completed", "evidence": "Exact stage packet λ"}}
        assignment = {"assigned_role": "research.adversarial-reviewer", "role_id": "adversarial-reviewer",
                      "model_role": "review.arbiter", "stage_id": "topic", "stage_kind": "topic_discovery",
                      "quota": {"max_calls": 2, "max_input_tokens": 15000,
                                "max_output_tokens": 2000, "max_seconds": 5},
                      "_prompt": json.dumps(original, ensure_ascii=False)}
        for field in ("blocking_findings", "required_revisions", "critical_findings"):
            for final_decision in ("accept", "hold"):
                with self.subTest(field=field, final_decision=final_decision):
                    bad = {"decision": "accept", "rationale": "Checked.", field: ["Material defect."]}
                    corrected = {"decision": final_decision, "rationale": "Independently reconsidered.",
                                 "blocking_findings": ["Material defect."] if final_decision == "hold" else [],
                                 "required_revisions": [], "deferred_gates": ["Verify later observations."],
                                 "repair_scope": []}
                    results = [ModelResult(json.dumps(value), "fixture", usage, .01, "stop")
                               for value, usage in ((bad, {"model_calls": 1, "input_tokens": 10, "output_tokens": 20}),
                                                    (corrected, {"model_calls": 1, "input_tokens": 11, "output_tokens": 21}))]
                    with patch("scisaurus.runtime.specialists.ModelClient") as client:
                        client.return_value.complete.side_effect = results
                        report = SpecialistDispatcher(model, max_parallel=1, deadline=time.monotonic() + 10).dispatch(
                            [assignment], {}, verifier=True)[0]
                    self.assertEqual(report["status"], "succeeded")
                    self.assertEqual(report["response"]["decision"], final_decision)
                    self.assertEqual(report["usage"], {"model_calls": 2, "input_tokens": 21, "output_tokens": 41})
                    self.assertEqual(client.return_value.complete.call_count, 2)
                    self.assertEqual(report["validation_retries"], 1)
                    diagnostic = "accepted verifier response cannot contain unresolved " + field
                    self.assertEqual(report["retry_history"][0]["error"], diagnostic)
                    repaired = json.loads(report["request_inputs"][1]["input"]["prompt"])
                    self.assertEqual(repaired["evidence_packet"], original)
                    self.assertEqual(repaired["response_format_repair"]["diagnostic"]["message"], diagnostic)
                    self.assertEqual(repaired["response_format_repair"]["response_owner"],
                                     {"kind": "verifier", "role": assignment["assigned_role"]})
                    self.assertIs(repaired["response_format_repair"]["stage_failure_evidence"], False)

    def test_repeated_prose_truncation_stops_after_one_schema_repair(self):
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
                 "model": "fixture", "timeout_seconds": 5.0, "max_output_tokens": 8192,
                 "context_window_tokens": 262144, "max_input_tokens": 245760}
        assignment = {
            "assigned_role": "methods.analysis-reviewer", "role_id": "analysis-reviewer",
            "model_role": "methods.analysis-reviewer", "execution_kind": "review",
            "stage_id": "repair", "stage_kind": "experiment",
            "quota": {"max_calls": 4, "max_input_tokens": 245760,
                      "max_output_tokens": 24576, "max_output_tokens_per_call": 8192,
                      "max_seconds": 5}, "_prompt": "{}",
        }
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = [
                ModelResult(text, "fixture", {"model_calls": 1, "output_tokens": 7000},
                            .01, "length")
                for text in ("Let me analyze the packet.", "I will continue reasoning.")
            ]
            report = SpecialistDispatcher(model, max_parallel=1,
                deadline=time.monotonic() + 10).dispatch([assignment], {})[0]
        self.assertEqual(report["status"], "failed")
        self.assertIn("no JSON object prefix", report["error"])
        self.assertEqual(report["usage"], {"model_calls": 2, "output_tokens": 14000})
        self.assertEqual(client.return_value.complete.call_count, 2)
        self.assertEqual(report["validation_retries"], 1)
        self.assertEqual(report["partial_response"], "I will continue reasoning.")
        self.assertTrue(all("continuation_text" not in item["input"]
                            for item in report["request_inputs"]))

    def test_selected_reasoning_output_cap_respects_execution_policy(self):
        model = {'protocol': 'ollama', 'base_url': 'http://127.0.0.1:1',
                 'model': 'fixture', 'timeout_seconds': 5.0, 'max_output_tokens': 32768,
                 'context_window_tokens': 262144, 'max_input_tokens': 229376,
                 'reasoning_effort': 'high'}
        assignment = {'assigned_role': 'methods.methodologist', 'role_id': 'methodologist',
                      'model_role': 'methods.methodologist', 'execution_kind': 'model',
                      'stage_id': 'repair', 'quota': {'max_calls': 1, 'max_input_tokens': 245760,
                      'max_output_tokens': 24576, 'max_output_tokens_per_call': 8192,
                      'max_seconds': 5}, '_prompt': '{}'}
        for policy, expected in (('operational', 8192), ('development', 32768)):
            with self.subTest(policy=policy), patch.dict('os.environ', {
                    'SCISAURUS_EXECUTION_POLICY': policy}), patch(
                    'scisaurus.runtime.specialists.ModelClient') as client:
                client.return_value.complete.return_value = ModelResult(
                    '{"decision":"repair","summary":"checked"}', 'fixture',
                    {'model_calls': 1, 'output_tokens': 100}, .01, 'stop')
                report = SpecialistDispatcher(model, max_parallel=1,
                    deadline=time.monotonic() + 10).dispatch([deepcopy(assignment)], {})[0]
                self.assertEqual(report['status'], 'succeeded', report)
                self.assertEqual(client.call_args.kwargs['max_output_tokens'], expected)
                self.assertEqual(report['request_inputs'][0]['generation_config']['max_output_tokens'], expected)

    def test_development_continues_past_cumulative_call_and_output_limits(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 5.0,
            "max_output_tokens": 8192, "context_window_tokens": 262144,
            "max_input_tokens": 245760,
        }
        assignment = {
            "assigned_role": "methods.methodologist", "role_id": "methodologist",
            "model_role": "methods.methodologist", "execution_kind": "model",
            "stage_id": "repair", "stage_kind": "experiment",
            "quota": {"max_calls": 1, "max_input_tokens": 245760,
                      "max_output_tokens": 1000,
                      "max_output_tokens_per_call": 8192, "max_seconds": 5},
            "_prompt": json.dumps({"objective": "bounded repair"}),
        }
        results = [
            ModelResult('{"decision":', "fake",
                        {"model_calls": 1, "output_tokens": 700}, 0.01, "length", 1),
            ModelResult('"repair"', "fake",
                        {"model_calls": 1, "output_tokens": 300}, 0.01, "length", 1),
            ModelResult(',"summary":"checked","findings":[],"evidence_gaps":[],"requested_actions":[]}',
                        "fake", {"model_calls": 1, "output_tokens": 200}, 0.01, "stop", 1),
        ]
        with patch.dict("os.environ", {"SCISAURUS_EXECUTION_POLICY": "development"}), patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = results
            report = SpecialistDispatcher(model, max_parallel=1, deadline=time.monotonic()+10).dispatch(
                [assignment], {"objective": "bounded repair"})[0]
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(report["usage"]["model_calls"], 3)
        self.assertEqual(report["usage"]["output_tokens"], 1200)
        self.assertEqual([call.kwargs["max_output_tokens"] for call in client.call_args_list], [8192]*3)

    def test_specialist_continuation_never_exceeds_cumulative_output_budget(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 5.0,
            "max_output_tokens": 8192, "context_window_tokens": 262144,
            "max_input_tokens": 245760,
        }
        assignment = {
            "assigned_role": "methods.methodologist", "role_id": "methodologist",
            "model_role": "methods.methodologist", "execution_kind": "model",
            "stage_id": "repair", "stage_kind": "experiment",
            "quota": {"max_calls": 3, "max_input_tokens": 245760,
                      "max_output_tokens": 1000,
                      "max_output_tokens_per_call": 8192, "max_seconds": 5},
            "_prompt": json.dumps({"objective": "bounded repair"}),
        }
        results = [
            ModelResult('{"decision":', "fake",
                        {"model_calls": 1, "output_tokens": 700}, 0.01, "length", 1),
            ModelResult('"repair"', "fake",
                        {"model_calls": 1, "output_tokens": 300}, 0.01, "length", 1),
        ]
        events = []
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = results
            report = SpecialistDispatcher(
                model, max_parallel=1, deadline=time.monotonic() + 10,
                on_progress=events.append,
            ).dispatch([assignment], {"objective": "bounded repair"})[0]

        self.assertEqual(report["status"], "failed")
        self.assertIn("cumulative output-token budget", report["error"])
        self.assertEqual(report["usage"]["output_tokens"], 1000)
        self.assertEqual(client.return_value.complete.call_count, 2)
        self.assertEqual(
            [call.kwargs["max_output_tokens"] for call in client.call_args_list],
            [1000, 300],
        )
        self.assertEqual(sum(event.get("event") == "output_budget_exhausted"
                             for event in events), 1)

    def test_provider_429_fences_parallel_pool_without_route_retry(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderFallbackHandler)
        server.lock = threading.Lock()
        server.models = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        events = []
        try:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            route = lambda route_id, pool, model: {
                "id": route_id, "pool": pool, "protocol": "openai_compatible",
                "base_url": base, "model": model,
            }
            model = {
                "protocol": "openai_compatible", "base_url": base,
                "model": "fallback", "timeout_seconds": 5.0,
                "max_output_tokens": 100, "context_window_tokens": 4096,
                "max_input_tokens": 2048, "role_routes": {
                    "research.search-planner": [
                        route("primary-route", "primary", "primary"),
                        route("gemma-route", "ollama", "gemma"),
                    ],
                },
            }
            assignments = [{
                "assigned_role": f"research.search-planner-{index}",
                "role_id": f"search-planner-{index}",
                "model_role": "research.search-planner",
                "execution_kind": "model", "stage_id": "survey",
                "stage_kind": "survey", "quota": {
                    "max_calls": 1, "max_input_tokens": 1000,
                    "max_output_tokens": 100, "max_seconds": 5,
                },
                "_prompt": json.dumps({"stage": "survey", "assignment": index}),
            } for index in range(2)]
            results = SpecialistDispatcher(
                model, provider_pools={
                    "primary": {"max_concurrent": 1, "base_urls": [base]},
                    "ollama": {"max_concurrent": 1, "base_urls": [base]},
                }, max_parallel=2, deadline=time.monotonic() + 5,
                on_progress=events.append,
            ).dispatch(assignments, {"objective": "test"})
            self.assertEqual(len(results), 2)
            self.assertEqual({item["status"] for item in results}, {"succeeded", "failed"})
            limited = next(item for item in results if item["status_code"] == 429)
            self.assertEqual(limited["provider_retries"], 0)
            self.assertTrue(any(item.get("status_code") == 429 for item in results))
            self.assertEqual(sum(item["provider_retries"] for item in results), 0)
            self.assertEqual(server.models.count("gemma"), 1)
            self.assertEqual(server.models.count("primary"), 1)
            self.assertEqual(len(server.models), 2)
            self.assertFalse(any(event.get("event") == "provider_route_failed"
                                 and event.get("status_code") == 429 for event in events))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_failed_response_output_exhaustion_preserves_operational_fence(self):
        model = {"protocol": "openai_compatible", "base_url": "http://fake/v1",
                 "model": "output-failure-fallback", "timeout_seconds": 5,
                 "max_output_tokens": 100, "role_routes": {"review.arbiter": [
                     {"id": "first", "pool": "isolated", "model": "output-failed"},
                     {"id": "second", "pool": "isolated", "model": "output-healthy"}]}}
        assignment = {"assigned_role": "research.adversarial-reviewer", "role_id": "adversarial-reviewer",
                      "model_role": "review.arbiter", "execution_kind": "review",
                      "stage_id": "experiment", "stage_kind": "experiment",
                      "quota": {"max_calls": 2, "max_input_tokens": 2000,
                                "max_output_tokens": 100, "max_output_tokens_per_call": 100,
                                "max_seconds": 5}, "_prompt": "{}"}
        error = ModelCallError("transient failed response", outcome_known=True, attempts=1, status_code=500)
        error.usage = {"model_calls": 1, "input_tokens": 23, "output_tokens": 100}
        calls = []
        class FailedResponseClient:
            def __init__(self, **config):
                pass
            def complete(self, **request):
                calls.append(request)
                raise error
        try:
            with patch("scisaurus.runtime.specialists.ModelClient", FailedResponseClient):
                report = SpecialistDispatcher(model).dispatch([assignment], {}, verifier=True)[0]
            self.assertEqual(len(calls), 1)
            self.assertEqual(report["usage"], error.usage)
            self.assertEqual(report["failure"]["status_code"], 500)
            self.assertEqual(report["error_type"], "ModelCallError")
            self.assertIn("cumulative output-token budget", report["error"])
        finally:
            for name in ("output-failure-fallback", "output-failed", "output-healthy"):
                clear_model_provider_cooldown({**model, "model": name})

    def test_specialist_invoices_known_failed_response_usage_once(self):
        for status_code in (400, 429):
            with self.subTest(status_code=status_code):
                error = ModelCallError("failed response", outcome_known=True,
                                       attempts=1, status_code=status_code)
                error.usage = {"model_calls": 1, "input_tokens": 23, "output_tokens": 7}
                class FailedResponseClient:
                    def __init__(self, **config):
                        pass
                    def complete(self, **request):
                        raise error
                assignment = {"assigned_role": "methods.methodologist", "role_id": "methodologist",
                              "model_role": "methods.methodologist", "execution_kind": "model",
                              "stage_id": "repair-panel", "stage_kind": "experiment",
                              "quota": {"max_calls": 1, "max_input_tokens": 1000,
                                        "max_output_tokens": 100, "max_seconds": 5}, "_prompt": "{}"}
                model = {"protocol": "openai_compatible", "base_url": "http://fake/v1",
                         "model": f"known-failed-usage-{status_code}", "timeout_seconds": 5,
                         "max_output_tokens": 100}
                try:
                    with patch("scisaurus.runtime.specialists.ModelClient", FailedResponseClient):
                        report = SpecialistDispatcher(model).dispatch([assignment], {})[0]
                    self.assertEqual(report["usage"], error.usage)
                    self.assertEqual(report["failure"]["usage"], error.usage)
                    self.assertEqual(report["status_code"], status_code)
                finally:
                    clear_model_provider_cooldown(model)

    def test_specialist_preserves_typed_budget_admission_without_provider_usage(self):
        from scisaurus.runtime.models import ModelBudgetExceededError
        admission = {"path": "/tmp/budget.sqlite", "key": "stage:experiment:cycle:30",
                     "dimension": "model_calls", "limit": 24, "observed": 24,
                     "reserved": 0, "requested": 1}
        class BudgetClient:
            def __init__(self, **config):
                pass
            def complete(self, **request):
                raise ModelBudgetExceededError("owner model call budget exhausted",
                                               budget_admission=admission, outcome_known=True)
        assignment = {"assigned_role": "methods.methodologist", "role_id": "methodologist",
                      "model_role": "methods.methodologist", "execution_kind": "model",
                      "stage_id": "repair-panel", "stage_kind": "experiment",
                      "quota": {"max_calls": 1, "max_input_tokens": 1000,
                                "max_output_tokens": 100, "max_seconds": 5}, "_prompt": "{}"}
        model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1",
                 "model": "test-budget", "timeout_seconds": 5, "max_output_tokens": 100}
        with patch("scisaurus.runtime.specialists.ModelClient", BudgetClient):
            report = SpecialistDispatcher(model).dispatch([assignment], {})[0]
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error_type"], "ModelBudgetExceededError")
        self.assertEqual(report["budget_admission"], admission)
        self.assertEqual(report["usage"], {})

    def test_provider_429_stops_specialist_assignments_still_queued(self):
        quota_scope = "specialist-queued-429-test"
        model = {
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "auth_env": None,
            "provider_quota_scope": quota_scope,
            "model": "deepseek-cloud",
            "timeout_seconds": 5.0,
            "max_output_tokens": 100,
            "context_window_tokens": 4096,
            "max_input_tokens": 2048,
            "role_routes": {"research.search-planner": [{
                "id": "cloud", "pool": "cloud",
                "protocol": "openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "deepseek-cloud", "auth_env": None,
            }]},
        }
        assignments = [{
            "assigned_role": f"research.search-planner-{index}",
            "role_id": f"search-planner-{index}",
            "model_role": "research.search-planner",
            "execution_kind": "model", "stage_id": "survey",
            "stage_kind": "survey", "quota": {
                "max_calls": 1, "max_input_tokens": 1000,
                "max_output_tokens": 100, "max_seconds": 5,
            },
            "_prompt": json.dumps({"assignment": index}),
        } for index in range(4)]
        calls = []

        class RateLimitedClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                raise ModelCallError(
                    "provider quota exhausted", outcome_known=True,
                    attempts=1, status_code=429,
                    provider_error_kind="quota_exhausted",
                )

        try:
            with patch("scisaurus.runtime.specialists.ModelClient", RateLimitedClient):
                results = SpecialistDispatcher(
                    model, provider_pools={
                        "cloud": {"max_concurrent": 1,
                                  "base_urls": [model["base_url"]]},
                    }, max_parallel=1, deadline=time.monotonic() + 10,
                ).dispatch(assignments, {"objective": "bounded test"})
        finally:
            clear_model_provider_cooldown(quota_scope)

        self.assertEqual(len(results), len(assignments))
        self.assertEqual(calls, ["deepseek-cloud"])
        self.assertTrue(all(result.get("status_code") == 429 for result in results))
        self.assertTrue(all(result.get("provider_retries") == 0 for result in results))

    def test_nested_specialist_role_inherits_safe_parent_routes(self):
        endpoint = "http://127.0.0.1:11434/v1"
        model = {
            "protocol": "openai_compatible", "base_url": endpoint,
            "model": "deepseek-v4.1-flash:cloud", "timeout_seconds": 5.0,
            "max_output_tokens": 100, "context_window_tokens": 4096,
            "max_input_tokens": 2048,
            "role_routes": {"methods.experiment-reviewer": [
                {"id": "local-qwen", "pool": "ollama", "base_url": endpoint,
                 "model": "qwen3.8:27b-mlx"},
                {"id": "gemma", "pool": "ollama", "base_url": endpoint,
                 "model": "gemma4:31b-cloud"},
            ]},
        }
        dispatcher = SpecialistDispatcher(model, max_parallel=1)

        routes = dispatcher._routes(
            "methods.experiment-reviewer.statistical_method")

        self.assertEqual([route_id for route_id, _pool, _route in routes], ["gemma"])
        self.assertEqual(routes[0][2]["model"], "gemma4:31b-cloud")

    def test_provider_500_reroutes_within_shared_ollama_capacity_pool(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 5.0,
            "max_output_tokens": 100, "context_window_tokens": 4096,
            "max_input_tokens": 2048, "role_routes": {
                "review.arbiter": [
                    {"id": "ollama-glm", "pool": "ollama", "model": "glm"},
                    {"id": "ollama-deepseek", "pool": "ollama", "model": "deepseek"},
                ],
            },
        }
        assignment = {
            "assigned_role": "research.adversarial-reviewer",
            "role_id": "adversarial-reviewer", "model_role": "review.arbiter",
            "execution_kind": "review", "stage_id": "experiment",
            "stage_kind": "experiment", "quota": {
                "max_calls": 2, "max_input_tokens": 1000,
                "max_output_tokens": 100, "max_seconds": 5,
            },
            "_prompt": json.dumps({"stage": "experiment"}),
        }
        success = ModelResult(json.dumps({
            "decision": "accept", "rationale": "healthy fallback",
            "critical_findings": [], "repair_scope": [],
        }), "deepseek", {"model_calls": 1, "input_tokens": 10,
                          "output_tokens": 5}, 0.01, "stop", 1)
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = [
                ModelCallError("transient server error", outcome_known=False,
                               status_code=500, attempts=1),
                success,
            ]
            result = SpecialistDispatcher(
                model, provider_pools={"ollama": {"max_concurrent": 3, "base_urls": []}},
                max_parallel=1, deadline=time.monotonic() + 10,
            ).dispatch([assignment], {"objective": "test"}, verifier=True)[0]
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["route_id"], "ollama-deepseek")
        self.assertEqual(result["provider_retries"], 1)

    def test_specialist_429_fences_cloud_and_does_not_use_local_cooldown_route(self):
        base = "http://127.0.0.1:11434/v1"
        cloud = {
            "protocol": "openai_compatible", "base_url": base,
            "auth_env": None, "context_window_tokens": 262144,
            "max_input_tokens": 245760,
        }
        model = {
            **cloud, "model": "deepseek-v4.1-flash:cloud",
            "timeout_seconds": 5.0, "max_output_tokens": 100,
            "role_routes": {"review.arbiter": [
                {"id": "ollama-deepseek", "pool": "ollama",
                 **cloud, "model": "deepseek-v4.1-flash:cloud"},
                {"id": "ollama-glm", "pool": "ollama",
                 **cloud, "model": "glm-5.3-flash:cloud"},
            ], "research.search-planner": [
                {"id": "search-cloud", "pool": "cloud", **cloud,
                 "model": "deepseek-v4.1-flash:cloud"},
            ]},
            "role_model_fallbacks": {"review.arbiter": [
                {**cloud, "model": "gemma4:31b-cloud"},
            ]},
            "provider_cooldown_fallback": {
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                **cloud, "model": "gemma-local",
                "provider_quota_scope": "ollama-local",
            },
        }
        assignment = {
            "assigned_role": "research.adversarial-reviewer",
            "role_id": "adversarial-reviewer", "model_role": "review.arbiter",
            "execution_kind": "review", "stage_id": "experiment",
            "stage_kind": "experiment", "quota": {
                "max_calls": 1, "max_input_tokens": 2000,
                "max_output_tokens": 100, "max_seconds": 5,
            },
            "_prompt": json.dumps({"stage": "experiment"}),
        }
        calls = []
        success = ModelResult(json.dumps({
            "decision": "accept", "rationale": "recovered after cloud quota",
            "critical_findings": [], "repair_scope": [],
        }), "gemma-local", {"model_calls": 1, "input_tokens": 10,
                                "output_tokens": 5}, 0.01, "stop", 1)

        class StubClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                if self.route["model"] == "gemma-local":
                    if system != VERIFIER_SYSTEM:
                        return ModelResult(json.dumps({
                            "decision": "observe", "summary": "continued after shared quota cooldown",
                            "findings": [], "evidence_gaps": [], "requested_actions": [],
                        }), "gemma-local", {"model_calls": 1, "input_tokens": 10,
                                                "output_tokens": 5}, 0.01, "stop", 1)
                    return success
                raise ModelCallError(
                    "model HTTP request failed with status 429",
                    outcome_known=True, attempts=1, status_code=429,
                )

        with patch("scisaurus.runtime.specialists.ModelClient", StubClient):
            result = SpecialistDispatcher(
                model, provider_pools={
                    "cloud": {"max_concurrent": 3, "base_urls": [base]},
                    "ollama": {"max_concurrent": 3, "base_urls": [base]},
                }, max_parallel=1, deadline=time.monotonic() + 10,
            ).dispatch([assignment], {"objective": "test"}, verifier=True)[0]

        self.assertEqual(calls, ["deepseek-v4.1-flash:cloud"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["status_code"], 429)
        self.assertEqual(result["provider_retries"], 0)
        self.assertGreater(model_provider_cooldown_remaining(cloud), 0)

        later_assignment = {
            "assigned_role": "research.search-planner",
            "role_id": "search-planner", "model_role": "research.search-planner",
            "execution_kind": "research", "stage_id": "survey",
            "stage_kind": "survey", "quota": {
                "max_calls": 1, "max_input_tokens": 2000,
                "max_output_tokens": 100, "max_seconds": 5,
            },
            "_prompt": json.dumps({"stage": "survey"}),
        }
        calls_before_later_role = len(calls)
        with patch("scisaurus.runtime.specialists.ModelClient", StubClient):
            later_result = SpecialistDispatcher(
                model, provider_pools={
                    "cloud": {"max_concurrent": 3, "base_urls": [base]},
                    "ollama": {"max_concurrent": 3, "base_urls": [base]},
                }, max_parallel=1, deadline=time.monotonic() + 10,
            ).dispatch([later_assignment], {"objective": "test"})[0]
        self.assertEqual(calls[calls_before_later_role:], [])
        self.assertEqual(later_result["status"], "failed", repr(later_result))
        self.assertEqual(later_result["status_code"], 429)

        clear_model_provider_cooldown(cloud)
        healthy_calls = []
        healthy = ModelResult(json.dumps({
            "decision": "accept", "rationale": "primary route healthy",
            "critical_findings": [], "repair_scope": [],
        }), "deepseek-v4.1-flash:cloud", {"model_calls": 1, "input_tokens": 10,
                                         "output_tokens": 5}, 0.01, "stop", 1)

        class HealthyClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                healthy_calls.append(self.route["model"])
                return healthy

        with patch("scisaurus.runtime.specialists.ModelClient", HealthyClient):
            healthy_result = SpecialistDispatcher(
                model, provider_pools={"ollama": {
                    "max_concurrent": 3, "base_urls": [base],
                }}, max_parallel=1, deadline=time.monotonic() + 10,
            ).dispatch([dict(assignment)], {"objective": "test"}, verifier=True)[0]
        self.assertEqual(healthy_result["status"], "succeeded")
        self.assertEqual(healthy_calls, ["deepseek-v4.1-flash:cloud"])

    def test_shared_429_during_admission_does_not_retry_on_another_route(self):
        base = "http://127.0.0.1:11434/v1"
        cloud_scope = "cloud-account-race"
        model = {
            "protocol": "openai_compatible", "base_url": base,
            "auth_env": None, "provider_quota_scope": cloud_scope,
            "model": "deepseek-cloud", "timeout_seconds": 5.0,
            "max_output_tokens": 100, "context_window_tokens": 4096,
            "max_input_tokens": 2048,
            "provider_cooldown_fallback": {
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                "protocol": "openai_compatible", "base_url": base,
                "auth_env": None, "model": "gemma-local",
                "provider_quota_scope": "ollama-local",
                "context_window_tokens": 4096, "max_input_tokens": 2048,
                "max_output_tokens": 100,
            },
        }
        assignment = {
            "assigned_role": "methods.statistical-reviewer",
            "role_id": "statistical-reviewer", "model_role": "review.methods",
            "execution_kind": "review", "stage_id": "experiment",
            "stage_kind": "experiment", "quota": {
                "max_calls": 1, "max_input_tokens": 1024,
                "max_output_tokens": 100, "max_seconds": 5,
            },
            "_prompt": json.dumps({"stage": "experiment"}),
        }
        calls = []
        class StubClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                raise AssertionError("provider cooldown must block before dispatch")

        cooldown_inserted = False

        def admit_with_racing_cooldown(config):
            nonlocal cooldown_inserted
            if config.get("provider_quota_scope") == cloud_scope and not cooldown_inserted:
                cooldown_inserted = True
                record_model_provider_cooldown(config, retry_after_seconds=30)
            return admit_model_provider_call(config)

        try:
            with patch("scisaurus.runtime.specialists.admit_model_provider_call",
                       side_effect=admit_with_racing_cooldown), \
                    patch("scisaurus.runtime.specialists.ModelClient", StubClient):
                result = SpecialistDispatcher(
                    model, provider_pools={
                        "ollama": {"max_concurrent": 1, "base_urls": [base]},
                    }, max_parallel=1, deadline=time.monotonic() + 10,
                ).dispatch([assignment], {"objective": "test"}, verifier=True)[0]
        finally:
            clear_model_provider_cooldown({"provider_quota_scope": cloud_scope})

        self.assertTrue(cooldown_inserted)
        self.assertEqual(calls, [])
        self.assertEqual(result["status"], "failed", repr(result))
        self.assertEqual(result["status_code"], 429)
        self.assertEqual(result["provider_retries"], 0)

    def test_single_known_429_stops_before_cooldown_route(self):
        base = "http://127.0.0.1:11434/v1"
        cloud = {
            "protocol": "openai_compatible", "base_url": base,
            "auth_env": None, "context_window_tokens": 262144,
            "max_input_tokens": 245760,
        }
        model = {
            **cloud, "model": "deepseek-v4.1-flash:cloud",
            "timeout_seconds": 5.0, "max_output_tokens": 100,
            "role_routes": {"research.search-planner": [{
                "id": "search-cloud", "pool": "cloud", **cloud,
                "model": "deepseek-v4.1-flash:cloud",
            }]},
            "provider_cooldown_fallback": {
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                **cloud, "model": "gemma-local",
                "provider_quota_scope": "ollama-local",
            },
        }
        assignment = {
            "assigned_role": "research.search-planner",
            "role_id": "search-planner", "model_role": "research.search-planner",
            "execution_kind": "research", "stage_id": "survey",
            "stage_kind": "survey", "quota": {
                "max_calls": 1, "max_input_tokens": 2000,
                "max_output_tokens": 100, "max_seconds": 5,
            },
            "_prompt": json.dumps({"stage": "survey"}),
        }
        calls = []
        class RouteClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                raise ModelCallError(
                    "model HTTP request failed with status 429",
                    outcome_known=True, attempts=1, status_code=429,
                )

        with patch("scisaurus.runtime.specialists.ModelClient", RouteClient):
            result = SpecialistDispatcher(
                model, provider_pools={
                    "cloud": {"max_concurrent": 3, "base_urls": [base]},
                    "ollama": {"max_concurrent": 3, "base_urls": [base]},
                }, max_parallel=1, deadline=time.monotonic() + 10,
            ).dispatch([assignment], {"objective": "test"})[0]

        self.assertEqual(calls, ["deepseek-v4.1-flash:cloud"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["status_code"], 429)
        self.assertEqual(result["provider_retries"], 0)

    def test_emergency_local_route_is_not_used_for_transient_server_failures(self):
        base = "http://127.0.0.1:11434/v1"
        cloud = {
            "protocol": "openai_compatible", "base_url": base,
            "auth_env": None, "context_window_tokens": 262144,
            "max_input_tokens": 245760,
        }
        model = {
            **cloud, "model": "deepseek-v4.1-flash:cloud",
            "timeout_seconds": 5.0, "max_output_tokens": 100,
            "role_routes": {"review.arbiter": [
                {"id": "cloud-primary", "pool": "cloud", **cloud,
                 "model": "deepseek-v4.1-flash:cloud"},
                {"id": "cloud-peer", "pool": "cloud", **cloud,
                 "model": "glm-5.3-flash:cloud"},
            ]},
            "provider_cooldown_fallback": {
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                **cloud, "model": "gemma-local",
                "provider_quota_scope": "ollama-local",
            },
        }
        assignment = {
            "assigned_role": "research.adversarial-reviewer",
            "role_id": "adversarial-reviewer", "model_role": "review.arbiter",
            "execution_kind": "review", "stage_id": "experiment",
            "stage_kind": "experiment", "quota": {
                "max_calls": 1, "max_input_tokens": 2000,
                "max_output_tokens": 100, "max_seconds": 5,
            },
            "_prompt": json.dumps({"stage": "experiment"}),
        }
        calls = []

        for status_code, max_calls, expected_models in (
                (503, 1, ["deepseek-v4.1-flash:cloud"]),
                (503, 2, ["deepseek-v4.1-flash:cloud", "glm-5.3-flash:cloud"]),
                (429, 2, ["deepseek-v4.1-flash:cloud"])):
            with self.subTest(status_code=status_code, max_calls=max_calls):
                calls.clear()
                current_assignment = dict(assignment)
                current_assignment["quota"] = {
                    **assignment["quota"], "max_calls": max_calls,
                }

                class UnavailableCloud:
                    def __init__(self, **route):
                        self.route = route

                    def complete(self, *, system, prompt, images=None):
                        calls.append(self.route["model"])
                        raise ModelCallError(
                            "temporary provider failure with quota-like body",
                            outcome_known=False, attempts=1, status_code=status_code,
                            provider_error_kind="quota_exhausted",
                        )

                with patch("scisaurus.runtime.specialists.ModelClient", UnavailableCloud):
                    report = SpecialistDispatcher(
                        model, provider_pools={
                            "cloud": {"max_concurrent": 3, "base_urls": [base]},
                            "ollama": {"max_concurrent": 3, "base_urls": [base]},
                        }, max_parallel=1, deadline=time.monotonic() + 10,
                    ).dispatch([current_assignment], {"objective": "test"})[0]

                self.assertEqual(calls, expected_models)
                self.assertEqual(report["status"], "result_unknown")


if __name__ == "__main__":
    unittest.main()
