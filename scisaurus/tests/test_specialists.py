import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.runtime.models import ModelCallError, ModelResult, estimate_input_tokens
from scisaurus.runtime.specialists import (
    SPECIALIST_SYSTEM, VERIFIER_SYSTEM, SpecialistDispatcher,
    build_specialist_prompt, build_verifier_prompt,
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
        # Keep the first Qwen reservation occupied while the second logical
        # assignment is forced onto Gemma.  The retry must then wait for the
        # single Qwen slot and return to it after Gemma's 429.
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
        prompt = build_specialist_prompt(assignment, packet)
        self.assertLessEqual(estimate_input_tokens(SPECIALIST_SYSTEM, prompt), 12000)
        self.assertNotIn("dependencies", json.loads(prompt)["shared_stage_context"])
        self.assertEqual(json.loads(prompt)["projected_input"]["topic"]["question"], "A bounded question")

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

    def test_provider_pool_capacity_is_real_and_reports_are_role_scoped(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SpecialistHandler)
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
                    "role-a": [route("qwen", "qwen", "qwen") , route("gemma", "ollama", "gemma")],
                    "role-b": [route("qwen", "qwen", "qwen") , route("gemma", "ollama", "gemma")],
                    "role-c": [route("qwen", "qwen", "qwen") , route("gemma", "ollama", "gemma")],
                    "role-d": [route("qwen", "qwen", "qwen") , route("gemma", "ollama", "gemma")],
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
                    "qwen": {"max_concurrent": 1, "base_urls": [base]},
                    "ollama": {"max_concurrent": 3, "base_urls": [base]},
                }, max_parallel=4, deadline=time.monotonic() + 5,
            ).dispatch(assignments, {"objective": "test"})
            self.assertEqual(len(results), 4)
            self.assertTrue(all(item["status"] == "succeeded" for item in results))
            self.assertEqual(server.requests, 4)
            self.assertEqual(server.peak, 4)
            self.assertEqual(sum(item["provider_pool"] == "qwen" for item in results), 1)
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

    def test_specialist_repairs_provider_truncation_once_when_quota_has_repair_slot(self):
        model = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1",
            "model": "fallback", "timeout_seconds": 5.0,
            "max_output_tokens": 100, "context_window_tokens": 4096,
            "max_input_tokens": 2048,
        }
        assignment = {
            "assigned_role": "methods.methodologist", "role_id": "methodologist",
            "model_role": "methods.methodologist", "execution_kind": "model",
            "stage_id": "repair", "stage_kind": "experiment",
            "quota": {"max_calls": 2, "max_input_tokens": 1000,
                      "max_output_tokens": 100, "max_seconds": 5},
            "_prompt": json.dumps({"objective": "bounded repair"}),
        }
        truncated = ModelResult('{"decision":"repair"', "fake", {"model_calls": 1}, 0.01, "length", 1)
        complete = ModelResult(json.dumps({
            "decision": "repair", "summary": "repaired", "findings": [],
            "evidence_gaps": [], "requested_actions": [],
        }), "fake", {"model_calls": 1}, 0.01, "stop", 1)
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect = [truncated, complete]
            result = SpecialistDispatcher(
                model, max_parallel=1, deadline=time.monotonic() + 10,
            ).dispatch([assignment], {"objective": "bounded repair"})[0]
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["validation_retries"], 1)
        self.assertEqual(result["usage"]["model_calls"], 2)

    def test_provider_429_reroutes_same_assignment_to_healthy_qwen(self):
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
                        route("qwen-route", "qwen", "qwen"),
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
                    "qwen": {"max_concurrent": 1, "base_urls": [base]},
                    "ollama": {"max_concurrent": 1, "base_urls": [base]},
                }, max_parallel=2, deadline=time.monotonic() + 5,
                on_progress=events.append,
            ).dispatch(assignments, {"objective": "test"})
            self.assertEqual(len(results), 2)
            self.assertTrue(all(item["status"] == "succeeded" for item in results))
            self.assertEqual({item["provider_pool"] for item in results}, {"qwen"})
            self.assertEqual(sum(item["provider_retries"] for item in results), 1)
            self.assertEqual(server.models.count("gemma"), 1)
            self.assertEqual(server.models.count("qwen"), 2)
            self.assertTrue(any(event.get("event") == "provider_route_failed"
                                and event.get("status_code") == 429 for event in events))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

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
                "max_calls": 1, "max_input_tokens": 1000,
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


if __name__ == "__main__":
    unittest.main()
