import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import (
    ModelCallError, ModelResult, admit_model_provider_call,
    clear_model_provider_cooldown,
    estimate_input_tokens, model_provider_cooldown_remaining,
    record_model_provider_cooldown,
)
from scisaurus.runtime.specialists import (
    SPECIALIST_SYSTEM, VERIFIER_SYSTEM, SpecialistDispatcher,
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
    def setUp(self):
        clear_model_provider_cooldown({
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "auth_env": None,
        })

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
        prompt = build_specialist_prompt(assignment, packet)
        self.assertLessEqual(estimate_input_tokens(SPECIALIST_SYSTEM, prompt), 12000)
        self.assertNotIn("dependencies", json.loads(prompt)["shared_stage_context"])
        self.assertEqual(json.loads(prompt)["projected_input"]["topic"]["question"], "A bounded question")

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
            self.assertEqual(server.peak, 4)
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

    def test_provider_429_reroutes_same_assignment_to_healthy_primary_route(self):
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
            self.assertTrue(all(item["status"] == "succeeded" for item in results))
            self.assertEqual({item["provider_pool"] for item in results}, {"primary"})
            self.assertEqual(sum(item["provider_retries"] for item in results), 1)
            self.assertEqual(server.models.count("gemma"), 1)
            self.assertEqual(server.models.count("primary"), 2)
            self.assertTrue(any(event.get("event") == "provider_route_failed"
                                and event.get("status_code") == 429 for event in events))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

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

    def test_specialist_uses_local_cooldown_route_only_after_all_cloud_routes_429(self):
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

        self.assertEqual(calls, [
            "deepseek-v4.1-flash:cloud", "glm-5.3-flash:cloud",
            "gemma4:31b-cloud", "gemma-local",
        ])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["model"], "gemma-local")
        self.assertEqual(result["provider_retries"], 3)
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
        self.assertEqual(calls[calls_before_later_role:], ["gemma-local"])
        self.assertEqual(later_result["status"], "succeeded", repr(later_result))
        self.assertEqual(later_result["provider_pool"], "ollama")

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

    def test_shared_429_arriving_during_admission_retries_on_qwen(self):
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
        success = ModelResult(json.dumps({
            "decision": "accept", "rationale": "retried after shared cooldown",
            "critical_findings": [], "repair_scope": [],
        }), "gemma-local", {"model_calls": 1, "input_tokens": 10,
                           "output_tokens": 5}, 0.01, "stop", 1)

        class StubClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                return success

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
        self.assertEqual(calls, ["gemma-local"])
        self.assertEqual(result["status"], "succeeded", repr(result))
        self.assertEqual(result["route_id"], "ollama-local-cooldown-recovery")
        self.assertEqual(result["usage"]["model_calls"], 1)
        self.assertEqual(result["retry_history"][0]["kind"],
                         "provider_cooldown_admission")
        self.assertEqual(result["retry_history"][0]["request_attempts"], 0)

    def test_single_known_429_can_reach_cooldown_route(self):
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
        success = ModelResult(json.dumps({
            "decision": "observe", "summary": "continued on independent provider",
            "findings": [], "evidence_gaps": [], "requested_actions": [],
        }), "gemma-local", {"model_calls": 1, "input_tokens": 10,
                                "output_tokens": 5}, 0.01, "stop", 1)

        class RouteClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                if self.route["model"] == "gemma-local":
                    return success
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

        self.assertEqual(calls, [
            "deepseek-v4.1-flash:cloud", "gemma-local",
        ])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["provider_pool"], "ollama")
        self.assertEqual(result["provider_retries"], 1)

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

        for status_code in (503, 429):
            with self.subTest(status_code=status_code):
                calls.clear()

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
                    ).dispatch([dict(assignment)], {"objective": "test"})[0]

                self.assertEqual(calls, [
                    "deepseek-v4.1-flash:cloud", "glm-5.3-flash:cloud",
                ])
                self.assertEqual(report["status"], "result_unknown")


if __name__ == "__main__":
    unittest.main()
