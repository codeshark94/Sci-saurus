"""Process-level dispatch evidence using explicit, local simulated workers."""
from __future__ import annotations
from contextlib import closing

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import multiprocessing
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import StateError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.config import MIN_WORKER_RESULT_BYTES, validate_config
from scisaurus.runtime.execution import (
    ExecutionRuntime, _NO_PROVIDER_CAPACITY, _ResultFile, _invoke_worker,
    _worker_error_payload,
)
from scisaurus.runtime.models import (
    DEFAULT_MODEL_RATE_LIMIT_COOLDOWN_SECONDS, ModelCallError, ModelClient,
    model_provider_quota_scope, resolve_model_config,
)
from scisaurus.tests.test_runner import config


def execution_worker(kind, params, channel):
    assignment = json.loads(params["prompt"])
    started = time.monotonic()
    if assignment.get("descendant"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Path(assignment["descendant"]).write_text(json.dumps({"worker": os.getpid(), "child": child.pid}))
    time.sleep(assignment["delay"])
    if assignment.get("budget_admission"):
        from scisaurus.runtime.models import ModelBudgetExceededError
        error = ModelBudgetExceededError("request cannot fit", outcome_known=True,
                                         budget_admission=assignment["budget_admission"],
                                         attempts=assignment.get("attempts", 0))
        error.usage = assignment.get("usage", {})
        channel.put(_worker_error_payload(error, kind))
        return
    if assignment.get("failure"):
        channel.put({"ok": False, "error": "simulated worker failure",
                     "outcome_known": "false" if assignment["failure"] == "malformed" else assignment["failure"] == "known"})
        return
    usage = {"model_calls": 1, "input_tokens": 10, "output_tokens": 5}
    if assignment.get("missing_usage"):
        usage = {"model_calls": 1}
    result = {"text": json.dumps({"started": started, "ended": time.monotonic(), "pid": os.getpid(),
                                   "provider_pool": params.get("provider_pool"),
                                   "route_id": params.get("route_id"),
                                   "model": params.get("client", {}).get("model"),
                                   "timeout_seconds": params.get("client", {}).get("timeout_seconds")}),
              "model": "simulated", "usage": usage, "elapsed_seconds": time.monotonic() - started,
              "finish_reason": "stop"}
    if assignment.get("malformed"):
        del result["usage"]
    channel.put({"ok": True, "result": result})


def provider_retry_worker(kind, params, channel):
    if params.get("provider_pool") == "ollama":
        channel.put({"ok": False, "error": "provider quota exhausted",
                     "outcome_known": True, "status_code": 429})
        return
    started = time.monotonic()
    time.sleep(0.15)
    result = {"text": json.dumps({
        "provider_pool": params.get("provider_pool"),
        "route_id": params.get("route_id"),
    }), "model": params.get("client", {}).get("model", "simulated"),
               "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 5},
               "elapsed_seconds": time.monotonic() - started, "finish_reason": "stop"}
    channel.put({"ok": True, "result": result})


def provider_exhaustion_worker(kind, params, channel):
    channel.put({"ok": False, "error": "all configured providers exhausted",
                 "outcome_known": True, "status_code": 429})


def same_pool_cooldown_fallback_worker(kind, params, channel):
    client = params.get("client", {})
    if client.get("model") != "gemma-local":
        channel.put({"ok": False, "error": "Ollama Cloud quota exhausted",
                     "outcome_known": True, "status_code": 429})
        return
    result = {"text": json.dumps({
        "provider_pool": params.get("provider_pool"),
        "route_id": params.get("route_id"),
        "model": client.get("model"),
        "max_input_tokens": client.get("max_input_tokens"),
        "max_output_tokens": client.get("max_output_tokens"),
    }), "model": client.get("model"),
              "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 5},
              "elapsed_seconds": 0.01, "finish_reason": "stop"}
    channel.put({"ok": True, "result": result})


def same_pool_independent_quota_worker(kind, params, channel):
    client = params.get("client", {})
    if client.get("model") == "quota-a-model":
        channel.put({"ok": False, "error": "quota A exhausted",
                     "outcome_known": True, "status_code": 429})
        return
    result = {"text": json.dumps({
        "provider_pool": params.get("provider_pool"),
        "route_id": params.get("route_id"),
        "model": client.get("model"),
    }), "model": client.get("model"),
              "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 5},
              "elapsed_seconds": 0.01, "finish_reason": "stop"}
    channel.put({"ok": True, "result": result})


def retrieval_usage_worker(kind, params, channel):
    channel.put({"ok": True, "result": params["result"]})


class TestExecutionRuntime(unittest.TestCase):
    def test_openalex_http_costs_and_cycle_ownership_survive_resume(self):
        from scisaurus.runtime.literature import openalex_request_usage
        runtime = self.runtime()
        runtime.worker_target = retrieval_usage_worker
        def owner(key):
            scope = {"model_call_budget_key": key, "model_call_budget_path": str(self.root / "parent.sqlite"),
                     "model_call_budget_limit": 10}
            runtime._publish("command/model-budget-delegations/" + key.split(":")[-1], "note",
                             {"delegation": {"scope": scope}}, "command.controller")
            return {"scope": scope}
        owner("stage:survey:cycle:11")
        for task, count in (("prior", 3), ("current", 2), ("cooldown", 0), ("unknown", None)):
            if task == "current":
                runtime.model_budget_delegation = owner("stage:survey:cycle:12")
            result = {"metadata": {} if count is None else {"attempts": count}}
            outcome = runtime._call_batch([{"task_id": task, "kind": "openalex",
                "actor": "research.searcher", "task_kind": "service",
                "params": {"client": {"timeout": 2}, "result": result}}])[task]
            self.assertTrue(outcome["ok"], outcome)
        usage = openalex_request_usage(runtime.control._conn, runtime.store.read_body)
        self.assertEqual(usage["openalex_requests"], 5)
        self.assertEqual(usage["unreported_task_ids"], ["unknown"])
        for cycle, expected in ((11, 3), (12, 2)):
            scoped = openalex_request_usage(runtime.control._conn, runtime.store.read_body,
                                            owner_key=f"stage:survey:cycle:{cycle}")
            self.assertTrue(scoped["owner_registered"])
            self.assertEqual(scoped["openalex_requests"], expected)
        pool = runtime.budget.get_window("run-window")["cumulative_usage"]
        self.assertEqual(pool["openalex_requests"], 5)
        with runtime.control.tx() as c:
            c.execute("UPDATE resource_pools SET cumulative_usage_json=?",
                      (json.dumps({k:v for k,v in pool.items() if k != "openalex_requests"}),))
        runtime.control.close()
        policy = {"additional_seconds": 10, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        for _ in range(2):
            restored = ExecutionRuntime(runtime.dir, runtime.config, worker_target=retrieval_usage_worker,
                                        resume_policy=policy)
            self.runtimes.append(restored)
            self.assertEqual(restored.budget.get_window("run-window")["cumulative_usage"]["openalex_requests"], 5)
            restored.control.close()

    def test_failed_resume_closes_constructor_owned_control(self):
        value = validate_config(config())
        project = self.root / "rejected-resume"
        original = ExecutionRuntime(project, value, worker_target=execution_worker)
        original.control.close()
        acquired = []

        def acquire(*args, **kwargs):
            store = ControlStore(*args, **kwargs)
            acquired.append(store._conn)
            return store

        with patch("scisaurus.runtime.execution.ControlStore", side_effect=acquire), \
                patch("scisaurus.runtime.execution.ResumeController.prepare",
                      side_effect=ValidationError("rejected resume")):
            with self.assertRaisesRegex(ValidationError, "rejected resume"):
                ExecutionRuntime(project, value, worker_target=execution_worker, resume_policy={})
        self.assertEqual(len(acquired), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            acquired[0].execute("SELECT 1")

    def test_progress_projects_exact_active_operations_and_settled_costs(self):
        events = []
        runtime = self.runtime(on_progress=events.append)
        outcomes = runtime._call_batch([self.spec("live-task", delay=0.15)])
        self.assertTrue(outcomes["live-task"]["ok"])
        active = [event for event in events if event.get("active_operations")]
        self.assertTrue(active)
        self.assertEqual(active[-1]["active_operations"], [
            {"task_id": "live-task", "actor": "strategy.worker", "kind": "model"}])
        settled = events[-1]
        self.assertEqual(settled["phase"], "calls_settled")
        self.assertEqual(settled["active_operations"], [])
        self.assertEqual(settled["cumulative_usage"]["model_calls"], 1)
        self.assertEqual(settled["run_id"], runtime.run_id)
        self.assertEqual(settled["project_dir"], str(runtime.dir.resolve()))

    def test_delegated_http_window_preserves_costs_and_same_cycle_reservations(self):
        import sqlite3
        from scisaurus.runtime.models import _reserve_model_call_budgets
        runtime = self.runtime()
        runtime.config["limits"]["max_model_calls"] = 3
        path = (self.root / "parent.sqlite").resolve()
        scope = {"model_call_budget_path": str(path), "model_call_budget_key": "stage:survey:cycle:2",
                 "model_call_budget_limit": 3}
        with closing(sqlite3.connect(path)) as owner, owner:
            owner.execute("CREATE TABLE model_call_budgets (budget_key TEXT PRIMARY KEY, max_calls INTEGER, used_calls INTEGER)")
            owner.executemany("INSERT INTO model_call_budgets VALUES (?, 3, ?)",
                              [(scope["model_call_budget_key"], 1), ("stage:survey:cycle:3", 0)])
        runtime.model_call_budget_scopes = [scope]
        runtime.model_budget_delegation = {"scope": scope, "used_calls": 1, "superseded_scopes": []}
        runtime.budget.reserve(window_id="run-window", reservation_id="history", task_id="history",
                               amount={"concurrent_calls": 1})
        runtime.budget.settle(window_id="run-window", reservation_id="history", actual={"model_calls": 95})
        first = runtime._model_dispatch_budget()
        _reserve_model_call_budgets([first])
        runtime.model_budget_delegation["used_calls"] = 0
        repeated = runtime._model_dispatch_budget()
        self.assertEqual(first, repeated)
        _reserve_model_call_budgets([repeated])
        with self.assertRaises(ModelCallError):
            _reserve_model_call_budgets([runtime._model_dispatch_budget()])
        next_scope = {**scope, "model_call_budget_key": "stage:survey:cycle:3"}
        runtime.model_call_budget_scopes = [next_scope]
        runtime.model_budget_delegation = {"scope": next_scope, "used_calls": 0,
                                           "superseded_scopes": [scope]}
        next_window = runtime._model_dispatch_budget()
        self.assertNotEqual(first["model_call_budget_key"], next_window["model_call_budget_key"])
        _reserve_model_call_budgets([next_window])
        self.assertEqual(runtime.budget.get_window("run-window")["cumulative_usage"]["model_calls"], 95)
        other = {**scope, "model_call_budget_key": "role:reviewer"}
        with self.assertRaises(ValidationError):
            runtime._validate_model_budget_delegation({"scope": next_scope, "superseded_scopes": [other]})
        runtime.config["limits"].pop("max_model_calls")
        self.assertEqual(runtime._model_dispatch_budget()["model_call_budget_limit"], 3)
        runtime.model_budget_delegation["superseded_scopes"] = [other]
        with self.assertRaises(ValidationError):
            runtime._model_dispatch_budget()
        runtime.model_budget_delegation["superseded_scopes"] = [scope]
        routed = runtime._delegated_model_config({"model_call_budget_scopes": [scope, other],
            "role_models": {"reviewer": {"model_call_budget_scopes": [scope, other]}}})
        self.assertEqual(routed["model_call_budget_scopes"], [other])
        self.assertEqual(routed["role_models"]["reviewer"]["model_call_budget_scopes"], [other])
        upgraded = {**scope, "model_token_budget_limits": {"input_tokens": 1000, "output_tokens": 1000}}
        runtime.model_budget_delegation["superseded_scopes"] = [upgraded]
        migrated = runtime._delegated_model_config({"model_call_budget_scopes": [scope, other]})
        self.assertEqual(migrated["model_call_budget_scopes"], [other])
        runtime.model_budget_delegation["superseded_scopes"] = [scope]
        spec = self.spec("delegated-route")
        spec["params"]["client"] = {**runtime.config["model"], "model_call_budget_scopes": [scope, other]}
        route = {"model_call_budget_scopes": [{**other, "model_call_budget_key": "route:alternate"}]}
        selected = runtime._route_model_config(spec, route)
        self.assertNotIn(scope, selected["model_call_budget_scopes"])
        self.assertIn(next_scope, selected["model_call_budget_scopes"])
        self.assertIn(other, selected["model_call_budget_scopes"])
        self.assertIn(route["model_call_budget_scopes"][0], selected["model_call_budget_scopes"])
        runtime.config["model"]["model_call_budget_scopes"] = [other]
        spec["params"]["client"] = {"max_output_tokens": 20, "model_call_budget_scopes": []}
        self.assertIn(other, runtime._base_model_config(spec)["model_call_budget_scopes"])

    def test_dispatch_lifecycle_failure_preserves_type(self):
        runtime = ExecutionRuntime.__new__(ExecutionRuntime)
        with self.assertRaises(StateError):
            runtime._raise_dispatch_failures([{"error_type": "StateError", "error": "stale binding"}], "model")

    def test_http_call_limit_covers_continuation_and_survives_resume(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps({"model": "fixture", "choices": [{"message": {
                    "content": '{"decision":'}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            value = config()
            value["model"].update(protocol="openai_compatible", model="fixture",
                base_url=f"http://127.0.0.1:{server.server_port}/v1", timeout_seconds=5)
            value["limits"].update(max_model_calls=1, wall_clock_seconds=15)
            run_dir = self.root / "http-budget"
            runtime = ExecutionRuntime(run_dir, validate_config(value), worker_target=_invoke_worker)
            self.runtimes.append(runtime)
            spec = self.spec("bounded-http")
            spec["params"]["client"] = value["model"]
            outcome = runtime._call_batch([spec])[spec["task_id"]]
            self.assertFalse(outcome["ok"])
            self.assertEqual(len(requests), 1)
            usage = runtime.budget.get_window("run-window")["cumulative_usage"]
            self.assertEqual(usage["model_calls"], 1)
            self.assertEqual(usage["input_tokens"], 5)
            self.assertEqual(usage["output_tokens"], 2)
            journal = json.loads((run_dir / "runs" / spec["task_id"] / "model-continuation.json").read_text())
            self.assertEqual(journal["status"], "incomplete")
            self.assertEqual(journal["response"], '{"decision":')
            runtime.control.close()
            resumed = ExecutionRuntime(run_dir, validate_config(value), worker_target=_invoke_worker,
                resume_policy={"additional_seconds": 10, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                               "source_changes": {"mode": "reject", "reopen_scopes": []}})
            self.runtimes.append(resumed)
            spec = self.spec("after-resume")
            spec["params"]["client"] = value["model"]
            self.assertFalse(resumed._call_batch([spec])[spec["task_id"]]["ok"])
            self.assertEqual(len(requests), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_worker_failure_envelope_preserves_model_rate_limit_metadata(self):
        error = ModelCallError(
            "model HTTP request failed with status 429",
            outcome_known=True,
            attempts=2,
            elapsed_seconds=1.25,
            status_code=429,
            retry_after_seconds=3600,
            provider_error_kind="quota_exhausted",
        )
        self.assertEqual(_worker_error_payload(error, "model"), {
            "ok": False,
            "error": "model HTTP request failed with status 429",
            "error_type": "ModelCallError",
            "outcome_known": True,
            "status_code": 429,
            "retry_after_seconds": 3600.0,
            "provider_error_kind": "quota_exhausted",
            "attempts": 2,
            "elapsed_seconds": 1.25,
        })

    def test_model_worker_continues_truncated_json_and_journals_each_provider_call(self):
        from scisaurus.runtime.models import ModelResult

        class Channel:
            def __init__(self):
                self.value = None

            def put(self, value):
                self.value = value

        class StubClient:
            calls = []
            responses = [
                ModelResult(text='{"reviewer_id":"claims",', model="fixture",
                                usage={"model_calls": 1, "input_tokens": 20,
                                       "output_tokens": 10},
                                elapsed_seconds=0.1, finish_reason="length",
                                request_attempts=1),
                ModelResult(text='"decision":"accepted"}', model="fixture",
                                usage={"model_calls": 1, "input_tokens": 25,
                                       "output_tokens": 8},
                                elapsed_seconds=0.2, finish_reason="stop",
                                request_attempts=1),
            ]

            def __init__(self, **config):
                self.model = config["model"]

            def complete(self, **kwargs):
                self.calls.append(kwargs)
                return self.responses.pop(0)

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "continuation.json"
            channel = Channel()
            params = {
                "client": {
                    "protocol": "openai_compatible",
                    "base_url": "https://models.example/v1",
                    "model": "fixture", "timeout_seconds": 2,
                    "max_output_tokens": 64,
                },
                "prompt": "Return one JSON object.",
                "_continuation_journal_path": str(journal),
            }
            with patch("scisaurus.runtime.execution.ModelClient", StubClient):
                _invoke_worker("model", params, channel)

            self.assertTrue(channel.value["ok"])
            result = channel.value["result"]
            self.assertEqual(result["text"], '{"reviewer_id":"claims","decision":"accepted"}')
            self.assertEqual(result["finish_reason"], "stop")
            self.assertEqual(result["usage"], {
                "model_calls": 2, "input_tokens": 45, "output_tokens": 18,
            })
            self.assertEqual(len(StubClient.calls), 2)
            self.assertEqual(StubClient.calls[0]["prompt"], StubClient.calls[1]["prompt"])
            self.assertEqual(StubClient.calls[1]["continuation_text"],
                             '{"reviewer_id":"claims",')
            saved = json.loads(journal.read_text())
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(len(saved["segments"]), 2)
            self.assertEqual(saved["response"], result["text"])

    def test_http_budget_fence_survives_worker_and_dispatch_boundaries(self):
        from scisaurus.core.errors import QuotaExceededError
        from scisaurus.runtime.models import ModelBudgetExceededError
        admission = {"path": str((self.root/"budget.sqlite").resolve()), "key": "stage:survey:cycle:1",
                     "dimension": "output_tokens", "limit": 30, "observed": 7,
                     "reserved": 0, "requested": 24}
        error = ModelBudgetExceededError("request cannot fit", outcome_known=True, budget_admission=admission)
        self.assertIsInstance(error, QuotaExceededError)
        runtime = self.runtime()
        usage = {"model_calls": 1, "input_tokens": 17, "output_tokens": 7}
        outcome = runtime._call_batch([self.spec("budget-fence", delay=0.01,
                                               budget_admission=admission,
                                               attempts=1, usage=usage)])["budget-fence"]
        self.assertEqual(outcome["budget_admission"], admission)
        self.assertEqual(outcome["usage"], usage)
        record = runtime.store.head("command/failures/budget-fence")
        failure = json.loads(runtime.store.read_body(record["body_hash"]))
        self.assertEqual(failure["budget_admission"], admission)
        self.assertEqual(failure["usage"], usage)
        with self.assertRaises(QuotaExceededError) as caught:
            runtime._raise_dispatch_failures([outcome], "review")
        self.assertEqual(caught.exception.budget_admission, admission)
        self.assertEqual(caught.exception.dimension, "max_output_tokens")
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.usage, usage)
        with self.assertRaises(QuotaExceededError) as single:
            runtime._raise_model_failure(outcome)
        self.assertEqual(single.exception.budget_admission, admission)
        self.assertEqual(single.exception.usage, usage)

    def test_undispatched_call_quota_remains_typed_through_batch_and_single_dispatch(self):
        from scisaurus.core.errors import QuotaExceededError
        runtime = self.runtime()
        runtime.config["limits"]["max_model_calls"] = 1
        runtime.model_calls_dispatched = 1
        outcome = runtime._call_batch([self.spec("quota-exhausted")])["quota-exhausted"]
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["error_type"], "quota")
        self.assertEqual(outcome.get("usage", {}).get("model_calls", 0), 0)
        with self.assertRaises(QuotaExceededError) as caught:
            runtime._raise_dispatch_failures([outcome], "review")
        self.assertEqual(caught.exception.dimension, "max_model_calls")
        self.assertEqual(caught.exception.limit, 1)
        self.assertEqual(caught.exception.observed, 1)
        self.assertEqual(caught.exception.dispatch_failures, [outcome])
        with self.assertRaises(QuotaExceededError) as single:
            runtime._call("single-quota-exhausted", "model", self.spec("unused")["params"],
                          actor="strategy.worker", task_kind="production")
        self.assertEqual(single.exception.dimension, "max_model_calls")
        self.assertEqual(runtime.model_calls_dispatched, 1)

    def test_failed_suffix_preserves_known_tokens_without_duplicate_calls(self):
        from scisaurus.runtime.execution import _complete_model_with_continuation
        from scisaurus.runtime.models import ModelResult
        class Client:
            output_format = "json_object"
            calls = 0
            def complete(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return ModelResult('{"value": ', "test", {
                        "model_calls": 1, "input_tokens": 10, "output_tokens": 5}, 0.1, "length")
                error = ModelCallError("invalid response", outcome_known=True, attempts=1)
                error.usage = {"model_calls": 1, "input_tokens": 17,
                               "output_tokens": 9, "cache_read_tokens": 3}
                raise error
        journal = self.root / "known-suffix.json"
        with self.assertRaises(ModelCallError) as failure:
            _complete_model_with_continuation(Client(), system="Return JSON.", prompt="Review.",
                                              journal_path=str(journal))
        expected = {"model_calls": 2, "input_tokens": 27, "output_tokens": 14, "cache_read_tokens": 3}
        self.assertEqual(failure.exception.usage, expected)
        recorded = json.loads(journal.read_text())
        self.assertEqual(recorded["usage"], expected)
        self.assertEqual(recorded["request_attempts"], 2)
        self.assertEqual(recorded["status"], "incomplete")

    def test_structured_continuation_requires_payload_and_preserves_observed_cost(self):
        from scisaurus.runtime.execution import _complete_model_with_continuation
        from scisaurus.runtime.models import ModelResult

        class Client:
            model = "fixture"
            output_format = "json_object"

            def __init__(self, first):
                self.first, self.calls = first, []

            def complete(self, **kwargs):
                self.calls.append(kwargs)
                return ModelResult(self.first if len(self.calls) == 1 else '"passed"}',
                                   self.model, {"model_calls": 1, "output_tokens": 8},
                                   0.1, "length" if len(self.calls) == 1 else "stop")

        for prefix in ('Let me carefully parse this assignment.', '<think>Still reasoning',
                       '```json\n', '{"verdict":"passed"}', '{"verdict":"passed"} trailing prose',
                       '```json\n{"ok":true}\n```\n```json\n{"extra":',
                       '{"text":"literal </think> marker","verdict":',
                       '```json\n{"text":"literal </think> marker","verdict":',
                       '{"verdict":', '```json\n{"verdict":',
                       '<think>Reasoning</think>\n{"verdict":'):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                client = Client(prefix)
                path = Path(directory) / "journal.json"
                result = _complete_model_with_continuation(
                    client, system="Return JSON.", prompt="Review.", journal_path=str(path))
                continues = prefix.endswith('"verdict":')
                self.assertEqual(len(client.calls), 2 if continues else 1)
                self.assertEqual(result.usage["model_calls"], len(client.calls))
                journal = json.loads(path.read_text())
                self.assertEqual(journal["response"], result.text)
                self.assertEqual(journal["usage"], result.usage)
                if not continues:
                    self.assertEqual(result.text, prefix)
                    self.assertEqual(result.finish_reason, "length")
                    self.assertEqual(journal["status"], "incomplete")
                    self.assertIn("closed JSON object" if prefix.startswith('{"verdict":"passed"}')
                                  or prefix.startswith('```json\n{"ok":true}')
                                  else "no JSON object prefix", journal["error"])

    def test_continuation_preserves_provider_response_metadata(self):
        from scisaurus.runtime.execution import _complete_model_with_continuation
        from scisaurus.runtime.models import ModelResult

        class Client:
            model = "fixture"
            output_format = "json_object"

            def __init__(self, chunks):
                self.chunks = iter(chunks)

            def complete(self, **kwargs):
                return next(self.chunks)

        def result(text, finish, metadata):
            return ModelResult(text=text, model="fixture", usage={"model_calls": 1},
                               elapsed_seconds=0.01, finish_reason=finish, response_metadata=metadata)

        first = {"wire_reasoning": "low", "thinking_bytes": 17, "max_output_tokens": 32768}
        final = {"wire_reasoning": "low", "thinking_bytes": 0, "max_output_tokens": 32768}
        cases = [
            [result('{"ok":true}', "stop", first)],
            [result('Let me analyze.', "length", first)],
            [result('{"ok":', "length", first), result('true}', "stop", final)],
        ]
        for chunks in cases:
            with self.subTest(chunks=len(chunks)), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "journal.json"
                observed = _complete_model_with_continuation(Client(chunks), system="Return JSON.",
                    prompt="Review.", journal_path=str(path))
                journal = json.loads(path.read_text())
                self.assertEqual(observed.response_metadata, chunks[-1].response_metadata)
                self.assertEqual([row["response_metadata"] for row in journal["segments"]],
                                 [chunk.response_metadata for chunk in chunks])
                self.assertEqual(observed.usage["model_calls"], len(chunks))

    def test_resumed_structured_reasoning_is_rejected_before_provider_admission(self):
        from scisaurus.runtime.execution import _complete_model_with_continuation
        from types import SimpleNamespace
        client = SimpleNamespace(model="fixture", output_format="json_object")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            with self.assertRaisesRegex(ValidationError, "no JSON object prefix") as caught:
                _complete_model_with_continuation(client, system="Return JSON.", prompt="Review.",
                                                  initial_prefix="Let me analyze.", journal_path=str(path))
            payload = _worker_error_payload(caught.exception, "model")
            self.assertTrue(payload["outcome_known"])
            self.assertEqual(payload["attempts"], 0)
            self.assertEqual(payload["usage"], {})
            journal = json.loads(path.read_text())
            self.assertEqual(journal["response"], "Let me analyze.")
            self.assertEqual(journal["request_attempts"], 0)
            self.assertEqual(journal["segments"], [])

    def test_rate_limit_during_continuation_preserves_prefix_and_does_not_retry(self):
        from scisaurus.runtime.models import ModelResult

        class Channel:
            def __init__(self):
                self.value = None

            def put(self, value):
                self.value = value

        class StubClient:
            calls = []

            def __init__(self, **config):
                self.model = config["model"]

            def complete(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) > 1:
                    raise ModelCallError(
                        "model HTTP request failed with status 429",
                        outcome_known=True, attempts=1, status_code=429,
                        retry_after_seconds=3600, provider_error_kind="quota_exhausted",
                    )
                return ModelResult(
                    text='{"decision":', model="fixture",
                    usage={"model_calls": 1, "input_tokens": 20, "output_tokens": 10},
                    elapsed_seconds=0.1, finish_reason="length", request_attempts=1,
                )

        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "continuation.json"
            channel = Channel()
            params = {
                "client": {"protocol": "openai_compatible",
                           "base_url": "https://models.example/v1",
                           "model": "fixture", "timeout_seconds": 2,
                           "max_output_tokens": 64},
                "prompt": "Return JSON.",
                "_continuation_journal_path": str(journal),
            }
            with patch("scisaurus.runtime.execution.ModelClient", StubClient):
                _invoke_worker("model", params, channel)

            self.assertFalse(channel.value["ok"])
            self.assertEqual(channel.value["status_code"], 429)
            self.assertEqual(channel.value["provider_error_kind"], "quota_exhausted")
            self.assertEqual(channel.value["partial_output_journal_path"], str(journal))
            self.assertEqual(len(StubClient.calls), 2)
            saved = json.loads(journal.read_text())
            self.assertEqual(saved["status"], "incomplete")
            self.assertEqual(saved["response"], '{"decision":')
            self.assertEqual(len(saved["segments"]), 1)

    def test_worker_result_limit_always_fits_its_own_overflow_envelope(self):
        path = self.root / "result.json"
        with self.assertRaisesRegex(ValueError, "minimum failure envelope"):
            _ResultFile(path, MIN_WORKER_RESULT_BYTES - 1)
        channel = _ResultFile(path, MIN_WORKER_RESULT_BYTES)
        channel.put({"ok": True, "result": {"text": "x" * 1000}})
        result = channel.read()
        self.assertEqual(path.stat().st_size, MIN_WORKER_RESULT_BYTES)
        self.assertFalse(result["ok"])
        self.assertFalse(result["outcome_known"])

        invalid = config()
        invalid["limits"]["max_result_bytes"] = MIN_WORKER_RESULT_BYTES - 1
        with self.assertRaisesRegex(ValidationError, "minimum worker IPC failure envelope"):
            validate_config(invalid)

    def test_crash_created_empty_scaffold_is_initialized_as_a_fresh_run(self):
        value = validate_config(config())
        run_dir = self.root / "empty-scaffold"
        control = ControlStore(run_dir)
        ArtifactStore(control).init_project(principal_note="crash-before-run-config")
        control.close()

        runtime = ExecutionRuntime(run_dir, value, worker_target=execution_worker)
        self.runtimes.append(runtime)
        self.assertIsNotNone(runtime.store.head("inputs/run-config"))
        self.assertIsNone(runtime.resume_session)

    def test_single_pool_rate_limit_stops_pending_dispatch_and_survives_resume(self):
        value = config()
        value["limits"].update(concurrent_calls=2, wall_clock_seconds=300, checkpoint_seconds=1)
        value["model"].update(base_url="http://127.0.0.1:1/v1", protocol="openai_compatible", timeout_seconds=5)
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 1, "base_urls": [value["model"]["base_url"]]}}
        run_dir = self.root / "rate-limited"
        runtime = ExecutionRuntime(run_dir, validate_config(value), worker_target=provider_exhaustion_worker)
        specs = [self.spec(f"call-{index}") for index in range(3)]
        for spec in specs:
            spec["params"]["client"] = value["model"]
        outcomes = runtime._call_batch(specs)
        self.assertTrue(all(outcome.get("status_code") == 429 for outcome in outcomes.values()))
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        quota_scope = model_provider_quota_scope(value["model"])
        cooldown_remaining = runtime.provider_cooldowns[quota_scope] - time.monotonic()
        self.assertGreater(cooldown_remaining, 0)
        self.assertLessEqual(cooldown_remaining, DEFAULT_MODEL_RATE_LIMIT_COOLDOWN_SECONDS + 1)
        runtime.control.close()
        policy = {"additional_seconds": 20, "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        resumed = ExecutionRuntime(run_dir, validate_config(value), worker_target=provider_exhaustion_worker,
                                   resume_policy=policy)
        self.runtimes.append(resumed)
        spec = self.spec("after-resume")
        spec["params"]["client"] = value["model"]
        resumed_route = resumed._provider_route(spec)
        self.assertEqual(resumed_route, _NO_PROVIDER_CAPACITY)
        self.assertGreater(resumed.provider_cooldowns[quota_scope], time.monotonic())
        self.assertEqual(resumed._call_batch([spec])["after-resume"]["status_code"], 429)
        self.assertEqual(resumed.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-execution-test-")
        self.root = Path(self.temp.name)
        self.runtimes = []

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.control.close()
        self.temp.cleanup()

    def runtime(self, *, capacity=2, wall=15, on_progress=None):
        value = config()
        value["limits"].update(concurrent_calls=capacity, wall_clock_seconds=wall, checkpoint_seconds=0.05)
        value["model"]["timeout_seconds"] = min(value["model"]["timeout_seconds"], wall / 2)
        runtime = ExecutionRuntime(self.root / f"project-{len(self.runtimes)}", validate_config(value),
                                   worker_target=execution_worker, on_progress=on_progress)
        self.runtimes.append(runtime)
        return runtime

    @staticmethod
    def spec(task_id, *, delay=0.3, timeout=8, reservation_id=None, **assignment):
        return {"task_id": task_id, "kind": "model", "actor": "strategy.worker", "task_kind": "production",
                "reservation_id": reservation_id,
                "params": {"client": {"timeout_seconds": timeout},
                           "prompt": json.dumps({"delay": delay, **assignment})}}

    @staticmethod
    def peak(outcomes):
        events = []
        timings = []
        for outcome in outcomes.values():
            if outcome["ok"]:
                timing = json.loads(outcome["result"]["text"])
                timings.append(timing)
                events.extend([(timing["started"], 1), (timing["ended"], -1)])
        running = peak = 0
        for _, delta in sorted(events):
            running += delta
            peak = max(peak, running)
        return peak, timings

    def assert_pids_stopped(self, paths):
        pids = [pid for path in paths for pid in json.loads(path.read_text()).values()]
        deadline = time.monotonic() + 2
        while True:
            living = []
            for pid in pids:
                result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
                if result.stdout.strip() and not result.stdout.lstrip().startswith("Z"):
                    living.append(pid)
            if not living or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        self.assertEqual(living, [], f"worker descendants still running: {living}")

    def test_spawned_workers_overlap_and_respect_parallel_cap(self):
        updates = []
        runtime = self.runtime(capacity=3, on_progress=updates.append)
        outcomes = runtime._call_batch([self.spec(f"work-{i}", delay=0.6) for i in range(3)], max_parallel=2)
        self.assertTrue(all(outcome["ok"] for outcome in outcomes.values()))
        peak, timings = self.peak(outcomes)
        self.assertEqual(peak, 2)
        self.assertEqual(len({timing["pid"] for timing in timings}), 3)
        elapsed = max(t["ended"] for t in timings) - min(t["started"] for t in timings)
        individual = sum(t["ended"] - t["started"] for t in timings)
        self.assertLess(elapsed, individual - 0.25)
        self.assertTrue(any(len(update["active_tasks"]) == 2 for update in updates))
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {})
        self.assertEqual(runtime.budget.get_window("run-window")["cumulative_usage"]["model_calls"], 3)
        self.assertTrue(runtime.control.verify_chain()[0])

    def test_route_timeout_is_not_shortened_by_a_global_operation_cap(self):
        value = config()
        value["limits"].update(concurrent_calls=2, wall_clock_seconds=1800, checkpoint_seconds=0.05)
        value["model"]["timeout_seconds"] = 900
        runtime = ExecutionRuntime(self.root / "route-timeout", validate_config(value),
                                   worker_target=execution_worker)
        self.runtimes.append(runtime)
        outcome = runtime._call_batch([
            self.spec("slow-route", delay=0.01, timeout=900),
        ])["slow-route"]
        self.assertTrue(outcome["ok"], outcome)
        observed = json.loads(outcome["result"]["text"])
        self.assertEqual(observed["timeout_seconds"], 900.0)

    def test_parent_timeout_uses_resolved_role_model_timeout(self):
        value = config()
        value["limits"].update(concurrent_calls=2, wall_clock_seconds=4, checkpoint_seconds=0.05)
        value["model"]["timeout_seconds"] = 0.12
        value["model"]["role_models"] = {
            "strategy.worker": {"timeout_seconds": 0.8},
        }
        runtime = ExecutionRuntime(
            self.root / "role-route-timeout", validate_config(value),
            worker_target=execution_worker,
        )
        self.runtimes.append(runtime)
        outcome = runtime._call_batch([
            self.spec("role-timeout", delay=0.3, timeout=0.12),
        ])["role-timeout"]
        self.assertTrue(outcome["ok"], outcome)
        observed = json.loads(outcome["result"]["text"])
        self.assertEqual(observed["timeout_seconds"], 0.8)

    def test_failed_worker_does_not_discard_valid_siblings(self):
        for known in ("known", "unknown"):
            with self.subTest(known=known):
                runtime = self.runtime()
                outcomes = runtime._call_batch([self.spec("failure", delay=0.05, failure=known),
                                               self.spec("success-one"), self.spec("success-two")])
                self.assertFalse(outcomes["failure"]["ok"])
                self.assertTrue(outcomes["success-one"]["ok"])
                self.assertTrue(outcomes["success-two"]["ok"])
                self.assertEqual(runtime.tasks.get("success-one")["state"], "awaiting_review")
                expected = {} if known == "known" else {"concurrent_calls": 1}
                self.assertEqual(runtime.budget.get_window("run-window")["reserved"], expected)

    def test_reserved_review_capacity_remains_available_to_its_owner(self):
        runtime = self.runtime(capacity=3)
        runtime.budget.reserve(window_id="run-window", reservation_id="review-capacity", task_id="review",
                               amount={"concurrent_calls": 1})
        outcomes = runtime._call_batch([self.spec(f"writer-{i}") for i in range(4)])
        self.assertEqual(self.peak(outcomes)[0], 2)
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})
        review = runtime._call_batch([self.spec("review", reservation_id="review-capacity")])
        self.assertTrue(review["review"]["ok"])
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {})

    def test_held_reservation_can_dispatch_behind_unreserved_pending_work(self):
        runtime = self.runtime()
        for task in ("review-one", "review-two"):
            runtime.budget.reserve(window_id="run-window", reservation_id=task, task_id=task,
                                   amount={"concurrent_calls": 1})
        outcomes = runtime._call_batch([self.spec("writer"), self.spec("review-one")])
        self.assertTrue(all(outcome["ok"] for outcome in outcomes.values()))
        writer = json.loads(outcomes["writer"]["result"]["text"])
        review = json.loads(outcomes["review-one"]["result"]["text"])
        self.assertGreater(writer["started"], review["ended"])
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})

    def test_exhausted_external_reservations_block_without_waiting(self):
        runtime = self.runtime()
        for i in range(2):
            runtime.budget.reserve(window_id="run-window", reservation_id=f"external-{i}", task_id=f"external-{i}",
                                   amount={"concurrent_calls": 1})
        started = time.monotonic()
        outcomes = runtime._call_batch([self.spec("blocked")])
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(outcomes["blocked"]["ok"])
        self.assertTrue(outcomes["blocked"]["outcome_known"])
        self.assertEqual(runtime.tasks.get("blocked")["state"], "blocked")
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 2})

    def test_missing_token_usage_stays_explicit_and_malformed_result_retains_capacity(self):
        runtime = self.runtime()
        outcomes = runtime._call_batch([self.spec("unreported", missing_usage=True),
                                       self.spec("malformed", malformed=True)])
        self.assertTrue(outcomes["unreported"]["ok"])
        self.assertFalse(outcomes["malformed"]["ok"])
        self.assertFalse(outcomes["malformed"]["outcome_known"])
        self.assertEqual(runtime.usage_gaps, [{"task_id": "unreported",
                                              "unreported_dimensions": ["input_tokens", "output_tokens"]}])
        self.assertEqual(runtime.budget.get_window("run-window")["cumulative_usage"], {"model_calls": 1})
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})

    def test_provider_routes_mix_endpoints_without_exceeding_pool_caps(self):
        value = config()
        value["model"].update(
            base_url="http://127.0.0.1:1/v1", protocol="openai_compatible", model="base-model")
        value["model"]["role_routes"] = {
            "strategy.worker": [
                {"id": "ollama-route", "pool": "ollama", "base_url": "http://127.0.0.1:1/v1",
                 "protocol": "openai_compatible", "model": "ollama-model", "auth_env": None},
                {"id": "qwen-route", "pool": "qwen", "base_url": "https://qwen.invalid/v1",
                 "protocol": "openai_compatible", "model": "route-model", "auth_env": None},
            ]
        }
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 3, "base_urls": ["http://127.0.0.1:1/v1"]},
            "qwen": {"max_concurrent": 1, "base_urls": ["https://qwen.invalid/v1"]},
        }
        runtime = ExecutionRuntime(self.root / "provider-pool-project", validate_config(value),
                                   worker_target=execution_worker)
        self.runtimes.append(runtime)
        specs = []
        for i in range(6):
            spec = self.spec(f"route-{i}", delay=0.35, timeout=8)
            spec["params"]["client"] = value["model"]
            specs.append(spec)
        outcomes = runtime._call_batch(specs, max_parallel=3)
        self.assertTrue(all(outcome["ok"] for outcome in outcomes.values()))
        records = [json.loads(outcome["result"]["text"]) for outcome in outcomes.values()]
        self.assertEqual(runtime.provider_active, {"ollama": 0, "qwen": 0})
        self.assertEqual({record["provider_pool"] for record in records}, {"ollama", "qwen"})
        self.assertEqual({record["route_id"] for record in records}, {"ollama-route", "qwen-route"})
        def provider_peak(pool_name):
            events = []
            for record in records:
                if record["provider_pool"] == pool_name:
                    events.extend(((record["started"], 1), (record["ended"], -1)))
            running = peak = 0
            for _timestamp, delta in sorted(events):
                running += delta
                peak = max(peak, running)
            return peak
        self.assertLessEqual(provider_peak("ollama"), 3)
        self.assertLessEqual(provider_peak("qwen"), 1)
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 6)

    def test_nested_reviewer_roles_inherit_safe_parent_routes(self):
        endpoint = "http://127.0.0.1:11434/v1"
        value = config()
        value["model"].update(
            base_url=endpoint, protocol="openai_compatible", model="deepseek-base",
            role_routes={"methods.experiment-reviewer": [
                {"id": "local-qwen", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "qwen3.8:27b-mlx",
                 "auth_env": None},
                {"id": "gemma", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "gemma4:31b-cloud",
                 "auth_env": None},
                {"id": "deepseek", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "deepseek-v4.1-flash:cloud",
                 "auth_env": None},
            ]},
        )
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 3, "base_urls": [endpoint]},
        }
        runtime = ExecutionRuntime(
            self.root / "nested-reviewer-routing", validate_config(value),
            worker_target=execution_worker)
        self.runtimes.append(runtime)
        role = "methods.experiment-reviewer.statistical_method"
        spec = self.spec("nested-reviewer")
        spec["actor"] = role
        spec["params"].update(client=value["model"], role=role)
        spec["_provider_route_override"] = {
            "id": "stale-local-qwen", "pool": "ollama",
            "_effective": {
                "protocol": "openai_compatible", "base_url": endpoint,
                "model": "qwen3.8:27b-mlx",
            },
        }

        selected = runtime._provider_route(spec)

        self.assertEqual(selected["id"], "gemma")
        self.assertEqual(selected["_effective"]["model"], "gemma4:31b-cloud")

    def test_provider_429_fences_run_without_replaying_on_a_healthy_route(self):
        value = config()
        value["model"].update(
            base_url="http://127.0.0.1:1/v1", protocol="openai_compatible", model="base-model")
        value["model"]["role_routes"] = {
            "strategy.worker": [
                {"id": "qwen-route", "pool": "qwen", "base_url": "http://127.0.0.1:1/v1",
                 "protocol": "openai_compatible", "model": "route-model", "auth_env": None},
                {"id": "gemma-route", "pool": "ollama", "base_url": "http://127.0.0.1:2/v1",
                 "protocol": "openai_compatible", "model": "gemma-model", "auth_env": None},
            ]
        }
        value["limits"]["provider_pools"] = {
            "qwen": {"max_concurrent": 1, "base_urls": ["http://127.0.0.1:1/v1"]},
            "ollama": {"max_concurrent": 1, "base_urls": ["http://127.0.0.1:2/v1"]},
        }
        runtime = ExecutionRuntime(
            self.root / "provider-retry-project", validate_config(value),
            worker_target=provider_retry_worker)
        self.runtimes.append(runtime)
        specs = []
        for index in range(2):
            spec = self.spec(f"provider-retry-{index}", delay=0.1)
            spec["params"]["client"] = value["model"]
            specs.append(spec)
        outcomes = runtime._call_batch(specs, max_parallel=2)
        self.assertTrue(any(item.get("status_code") == 429 for item in outcomes.values()))
        self.assertTrue(any(item["ok"] for item in outcomes.values()))
        technical = runtime.control._conn.execute(
            "SELECT task_id FROM tasks WHERE task_id LIKE '%-provider-retry-%'"
        ).fetchall()
        retry_attempts = runtime.control._conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE attempt_id LIKE '%-provider-retry-%'"
        ).fetchone()[0]
        self.assertEqual(technical, [])
        self.assertEqual(retry_attempts, 0)
        self.assertIsNotNone(runtime.model_rate_limit_fence)
        self.assertEqual(runtime.provider_active, {"qwen": 0, "ollama": 0})

    def test_known_429_stops_same_pool_fallback_until_explicit_resume_after_cooldown(self):
        value = config()
        endpoint = "http://127.0.0.1:11434/v1"
        value["model"].update(
            base_url=endpoint, protocol="openai_compatible", model="cloud-model",
            context_window_tokens=262144, max_input_tokens=245760,
            max_output_tokens=8192,
            role_routes={"methods.methodologist": [
                {"id": "ollama-deepseek", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "deepseek-v4.1-flash:cloud",
                 "auth_env": None, "context_window_tokens": 262144,
                 "max_input_tokens": 1024, "max_output_tokens": 256},
                {"id": "ollama-glm", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "glm-5.3-flash:cloud",
                 "auth_env": None, "context_window_tokens": 262144,
                 "max_input_tokens": 1024, "max_output_tokens": 256},
            ]},
            provider_cooldown_fallback={
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                "protocol": "openai_compatible", "base_url": endpoint,
                "model": "gemma-local", "auth_env": None,
                "context_window_tokens": 262144, "max_input_tokens": 4096,
                "max_output_tokens": 1024, "provider_quota_scope": "ollama-local",
            },
        )
        value["limits"].update(concurrent_calls=2, max_model_calls=10,
                               wall_clock_seconds=300, checkpoint_seconds=1)
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 1, "base_urls": [endpoint]}}
        runtime = ExecutionRuntime(
            self.root / "same-pool-cooldown-fallback", validate_config(value),
            worker_target=same_pool_cooldown_fallback_worker)
        self.runtimes.append(runtime)

        def model_task(task_id):
            spec = self.spec(task_id, delay=0.01)
            spec["actor"] = "methods.methodologist"
            spec["params"]["client"] = runtime.config["model"]
            spec["params"]["role"] = "methods.methodologist"
            return spec

        first = runtime._call_batch([model_task("fallback-first")])
        self.assertFalse(first["fallback-first"]["ok"])
        self.assertEqual(first["fallback-first"]["status_code"], 429)
        self.assertEqual(runtime.control._conn.execute(
            "SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        failed_scope = model_provider_quota_scope({
            "protocol": "openai_compatible", "base_url": endpoint,
            "auth_env": None,
        })
        self.assertGreater(runtime.provider_cooldowns[failed_scope], time.monotonic())
        self.assertFalse(runtime.provider_cooldown_fallback_allowed[failed_scope])

        retry_spec = model_task("fallback-client-construction")
        selected_route = runtime._provider_route(retry_spec)
        self.assertIs(selected_route, _NO_PROVIDER_CAPACITY)

        second = runtime._call_batch([model_task("fallback-during-cooldown")])
        self.assertFalse(second["fallback-during-cooldown"]["ok"])
        self.assertEqual(second["fallback-during-cooldown"]["status_code"], 429)
        self.assertEqual(runtime.control._conn.execute(
            "SELECT COUNT(*) FROM attempts").fetchone()[0], 1)
        self.assertEqual(runtime.provider_active, {"ollama": 0})

        runtime.control.close()
        resume_policy = {
            "additional_seconds": 20,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reject", "reopen_scopes": []},
        }
        resumed = ExecutionRuntime(
            self.root / "same-pool-cooldown-fallback", validate_config(value),
            worker_target=same_pool_cooldown_fallback_worker,
            resume_policy=resume_policy)
        self.runtimes.append(resumed)
        resumed_spec = model_task("fallback-after-resume")
        resumed_spec["params"]["client"] = resumed.config["model"]
        resumed_route = resumed._provider_route(resumed_spec)
        self.assertIs(resumed_route, _NO_PROVIDER_CAPACITY)

    def test_local_qwen_cooldown_fallback_is_removed_before_dispatch(self):
        value = config()
        endpoint = "http://127.0.0.1:11434/v1"
        value["model"].update(
            base_url=endpoint, protocol="openai_compatible", model="cloud-model",
            provider_cooldown_fallback={
                "id": "local-qwen", "pool": "ollama",
                "base_url": endpoint, "protocol": "openai_compatible",
                "model": "qwen3.8:27b-mlx", "auth_env": None,
            },
        )
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 1, "base_urls": [endpoint]}}

        runtime = ExecutionRuntime(
            self.root / "local-qwen-cooldown-disabled", validate_config(value),
            worker_target=execution_worker)
        self.runtimes.append(runtime)
        spec = self.spec("local-qwen-cooldown-disabled")
        spec["actor"] = "methods.methodologist"
        spec["params"].update(
            client=runtime.config["model"], role="methods.methodologist")

        self.assertIsNone(runtime._provider_cooldown_fallback_route(spec))

    def test_cooldown_fallback_is_limited_to_known_429(self):
        value = config()
        endpoint = "http://127.0.0.1:11434/v1"
        value["model"].update(
            base_url=endpoint, protocol="openai_compatible", model="cloud-model",
            role_routes={"methods.methodologist": [
                {"id": "ollama-cloud", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "deepseek-cloud",
                 "auth_env": None},
            ]},
            provider_cooldown_fallback={
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                "protocol": "openai_compatible", "base_url": endpoint,
                "model": "gemma-local", "auth_env": None,
                "provider_quota_scope": "ollama-local",
            },
        )
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 1, "base_urls": [endpoint]}}
        runtime = ExecutionRuntime(
            self.root / "cooldown-fallback-classification", validate_config(value),
            worker_target=execution_worker)
        self.runtimes.append(runtime)
        spec = self.spec("cooldown-fallback-classification")
        spec["actor"] = "methods.methodologist"
        spec["params"].update(client=runtime.config["model"], role="methods.methodologist")

        route_config = runtime._base_model_config(spec)
        runtime._mark_provider_cooldown(
            "ollama", {"status_code": 503, "outcome_known": True}, route_config)
        self.assertIs(runtime._provider_route(spec), _NO_PROVIDER_CAPACITY)

        runtime.provider_cooldowns.clear()
        runtime.provider_cooldown_fallback_allowed.clear()
        spec["params"]["provider_pool"] = "ollama"
        entry = {"spec": spec}
        retry = runtime._provider_retry_spec(
            entry, {"status_code": 429, "outcome_known": False,
                    "error": "ambiguous transport outcome"})
        self.assertIsNone(retry)
        self.assertIsNone(runtime._provider_retry_spec(
            entry, {"status_code": 503, "outcome_known": True,
                    "partial_output_journal_path": "/tmp/model-continuation.json",
                    "error": "suffix request unavailable after partial output"}))
        scope = model_provider_quota_scope(runtime._base_model_config(spec))
        self.assertFalse(runtime.provider_cooldown_fallback_allowed[scope])
        self.assertIs(runtime._provider_route(spec), _NO_PROVIDER_CAPACITY)

        runtime.control.close()
        resume_policy = {
            "additional_seconds": 20,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reject", "reopen_scopes": []},
        }
        resumed = ExecutionRuntime(
            self.root / "cooldown-fallback-classification", validate_config(value),
            worker_target=execution_worker, resume_policy=resume_policy)
        self.runtimes.append(resumed)
        resumed_spec = self.spec("cooldown-fallback-classification-after-resume")
        resumed_spec["actor"] = "methods.methodologist"
        resumed_spec["params"].update(
            client=resumed.config["model"], role="methods.methodologist")
        self.assertIs(resumed._provider_route(resumed_spec), _NO_PROVIDER_CAPACITY)
        self.assertFalse(resumed.provider_cooldown_fallback_allowed[scope])

    def test_same_pool_429_does_not_retry_an_independent_quota_scope(self):
        value = config()
        endpoint = "http://127.0.0.1:11434/v1"
        value["model"].update(
            base_url=endpoint, protocol="openai_compatible", model="quota-a-model",
            role_routes={"methods.methodologist": [
                {"id": "quota-a", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "quota-a-model",
                 "auth_env": None, "provider_quota_scope": "quota-a"},
                {"id": "quota-b", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "quota-b-model",
                 "auth_env": None, "provider_quota_scope": "quota-b"},
            ]},
            provider_cooldown_fallback={
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                "protocol": "openai_compatible", "base_url": endpoint,
                "model": "gemma-local", "auth_env": None,
                "provider_quota_scope": "ollama-local",
            },
        )
        value["limits"].update(concurrent_calls=2, max_model_calls=10,
                               wall_clock_seconds=300, checkpoint_seconds=1)
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 1, "base_urls": [endpoint]}}
        runtime = ExecutionRuntime(
            self.root / "same-pool-independent-quota",
            validate_config(value), worker_target=same_pool_independent_quota_worker)
        self.runtimes.append(runtime)
        spec = self.spec("same-pool-independent-quota")
        spec["actor"] = "methods.methodologist"
        spec["params"].update(
            client=runtime.config["model"], role="methods.methodologist")

        outcome = runtime._call_batch([spec])["same-pool-independent-quota"]
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["status_code"], 429)
        self.assertIn("quota-a", runtime.provider_cooldowns)
        self.assertNotIn("quota-b", runtime.provider_cooldowns)
        self.assertIsNotNone(runtime.model_rate_limit_fence)
        self.assertIs(runtime._provider_route(spec), _NO_PROVIDER_CAPACITY)
        self.assertEqual(runtime.control._conn.execute(
            "SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_model_429_fences_role_model_before_cooldown_fallback(self):
        value = config()
        endpoint = "http://127.0.0.1:11434/v1"
        value["model"].update(
            base_url=endpoint, protocol="openai_compatible", model="cloud-default",
            context_window_tokens=262144, max_input_tokens=245760,
            max_output_tokens=8192,
            role_models={"methods.methodologist": {
                "base_url": endpoint, "protocol": "openai_compatible",
                "model": "deepseek-cloud", "auth_env": None,
                "provider_quota_scope": "role-cloud-account",
                "context_window_tokens": 262144, "max_input_tokens": 1024,
                "max_output_tokens": 256,
            }},
            provider_cooldown_fallback={
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                "protocol": "openai_compatible", "base_url": endpoint,
                "model": "gemma-local", "auth_env": None,
                "provider_quota_scope": "ollama-local",
                "context_window_tokens": 262144, "max_input_tokens": 4096,
            },
        )
        value["model"].pop("max_input_tokens", None)
        value["model"].pop("max_output_tokens", None)
        value["model"]["max_output_tokens"] = 8192
        value["model"]["provider_cooldown_fallback"].pop("max_input_tokens", None)
        value["limits"].update(concurrent_calls=2, max_model_calls=10,
                               wall_clock_seconds=300, checkpoint_seconds=1)
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 1, "base_urls": [endpoint]}}
        run_dir = self.root / "role-model-cooldown-limits"
        runtime = ExecutionRuntime(
            run_dir, validate_config(value),
            worker_target=same_pool_cooldown_fallback_worker)
        self.runtimes.append(runtime)

        def model_task(task_id, model_config):
            spec = self.spec(task_id, delay=0.01)
            spec["actor"] = "methods.methodologist"
            spec["params"].update(
                client=model_config, role="methods.methodologist")
            return spec

        first = runtime._call_batch([
            model_task("role-model-fallback-first", runtime.config["model"])
        ])
        self.assertFalse(first["role-model-fallback-first"]["ok"])
        self.assertEqual(first["role-model-fallback-first"]["status_code"], 429)
        self.assertEqual(runtime.control._conn.execute(
            "SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

        later = model_task("role-model-fallback-later", runtime.config["model"])
        selected = runtime._provider_route(later)
        self.assertIs(selected, _NO_PROVIDER_CAPACITY)

    def test_cooldown_fallback_cannot_reuse_resolved_or_role_route_quota_scope(self):
        endpoint = "http://127.0.0.1:11434/v1"
        for mode in ("role_models", "role_routes"):
            with self.subTest(mode=mode):
                value = config()
                model = value["model"]
                model.update(
                    base_url=endpoint, protocol="openai_compatible",
                    model="global-model", provider_quota_scope="global-scope",
                    provider_cooldown_fallback={
                        "id": "same-quota-recovery", "pool": "ollama",
                        "protocol": "openai_compatible", "base_url": endpoint,
                        "model": "different-model-same-quota", "auth_env": None,
                        "provider_quota_scope": "role-quota-a",
                    },
                )
                if mode == "role_models":
                    model["role_models"] = {"methods.methodologist": {
                        "base_url": endpoint, "protocol": "openai_compatible",
                        "model": "role-model", "auth_env": None,
                        "provider_quota_scope": "role-quota-a",
                    }}
                else:
                    model["role_routes"] = {"methods.methodologist": [{
                        "id": "role-route", "pool": "ollama",
                        "base_url": endpoint, "protocol": "openai_compatible",
                        "model": "role-model", "auth_env": None,
                        "provider_quota_scope": "role-quota-a",
                    }]}
                value["limits"]["provider_pools"] = {
                    "ollama": {"max_concurrent": 1, "base_urls": [endpoint]}}
                runtime = ExecutionRuntime(
                    self.root / f"same-quota-fallback-{mode}",
                    validate_config(value), worker_target=execution_worker)
                self.runtimes.append(runtime)
                route_config = (runtime._base_model_config({
                    "params": {"client": runtime.config["model"],
                               "role": "methods.methodologist"},
                    "actor": "methods.methodologist",
                }) if mode == "role_models" else {
                    **runtime.config["model"]["role_routes"]["methods.methodologist"][0],
                })
                runtime._mark_provider_cooldown(
                    "ollama", {"status_code": 429, "outcome_known": True},
                    route_config)
                spec = self.spec(f"same-quota-fallback-{mode}")
                spec["actor"] = "methods.methodologist"
                spec["params"].update(
                    client=runtime.config["model"], role="methods.methodologist")

                selected = runtime._provider_route(spec)
                self.assertIs(selected, _NO_PROVIDER_CAPACITY)

    def test_unscoped_legacy_cooldown_does_not_select_recovery_for_other_scope(self):
        value = config()
        endpoint = "http://127.0.0.1:11434/v1"
        value["model"].update(
            base_url=endpoint, protocol="openai_compatible", model="quota-a-model",
            role_routes={"methods.methodologist": [
                {"id": "quota-a", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "quota-a-model",
                 "auth_env": None, "provider_quota_scope": "quota-a"},
                {"id": "quota-b", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "quota-b-model",
                 "auth_env": None, "provider_quota_scope": "quota-b"},
            ]},
            provider_cooldown_fallback={
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                "protocol": "openai_compatible", "base_url": endpoint,
                "model": "gemma-local", "auth_env": None,
                "provider_quota_scope": "ollama-local",
            },
        )
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 1, "base_urls": [endpoint]}}
        run_dir = self.root / "legacy-pool-cooldown"
        runtime = ExecutionRuntime(
            run_dir, validate_config(value), worker_target=execution_worker)
        self.runtimes.append(runtime)
        runtime._publish("command/provider-cooldowns/ollama", "note", {
            "pool": "ollama", "status_code": 429,
            "fallback_eligible": True,
            "not_before_epoch": time.time() + 60,
        }, "command.controller")
        runtime._publish("command/provider-cooldowns/scope-quota-a", "note", {
            "pool": "scope-quota-a", "status_code": 429,
            "fallback_eligible": True,
            "not_before_epoch": time.time() + 60,
        }, "command.controller")
        runtime.control.close()

        resume_policy = {
            "additional_seconds": 20,
            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
            "source_changes": {"mode": "reject", "reopen_scopes": []},
        }
        resumed = ExecutionRuntime(
            run_dir, validate_config(value), worker_target=execution_worker,
            resume_policy=resume_policy)
        self.runtimes.append(resumed)
        spec = self.spec("legacy-pool-cooldown-resume")
        spec["actor"] = "methods.methodologist"
        spec["params"].update(
            client=resumed.config["model"], role="methods.methodologist")

        selected = resumed._provider_route(spec)
        self.assertEqual(selected["id"], "quota-a")

    def test_wrong_scope_cooldown_record_cannot_poison_scope_lookup(self):
        value = config()
        endpoint = "http://127.0.0.1:11434/v1"
        value["model"].update(
            base_url=endpoint, protocol="openai_compatible", model="quota-a-model",
            role_routes={"methods.methodologist": [
                {"id": "quota-a", "pool": "ollama", "base_url": endpoint,
                 "protocol": "openai_compatible", "model": "quota-a-model",
                 "auth_env": None, "provider_quota_scope": "quota-a"},
            ]},
        )
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 1, "base_urls": [endpoint]}}
        runtime = ExecutionRuntime(
            self.root / "wrong-scope-cooldown-note", validate_config(value),
            worker_target=execution_worker)
        self.runtimes.append(runtime)
        runtime._publish(
            runtime._provider_cooldown_artifact_id("quota-a"), "note", {
                "pool": "ollama", "quota_scope": "quota-b",
                "status_code": 429, "fallback_eligible": True,
                "fallback_basis": {"status_code": 429, "outcome_known": True},
                "not_before_epoch": time.time() + 60,
            }, "command.controller")
        spec = self.spec("wrong-scope-cooldown-note")
        spec["actor"] = "methods.methodologist"
        spec["params"].update(
            client=runtime.config["model"], role="methods.methodologist")

        selected = runtime._provider_route(spec)
        self.assertEqual(selected["id"], "quota-a")
        self.assertNotIn("quota-a", runtime.provider_cooldowns)

    def test_scoped_cooldown_requires_known_429_basis_for_fallback(self):
        value = config()
        endpoint = "http://127.0.0.1:11434/v1"
        value["model"].update(
            base_url=endpoint, protocol="openai_compatible", model="quota-a-model",
            role_routes={"methods.methodologist": [{
                "id": "quota-a", "pool": "ollama", "base_url": endpoint,
                "protocol": "openai_compatible", "model": "quota-a-model",
                "auth_env": None, "provider_quota_scope": "quota-a",
            }]},
            provider_cooldown_fallback={
                "id": "recovery", "pool": "ollama", "base_url": endpoint,
                "protocol": "openai_compatible", "model": "gemma-local",
                "auth_env": None, "provider_quota_scope": "quota-b",
            },
        )
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 1, "base_urls": [endpoint]}}
        runtime = ExecutionRuntime(
            self.root / "unproven-scoped-cooldown-note", validate_config(value),
            worker_target=execution_worker)
        self.runtimes.append(runtime)
        runtime._publish(
            runtime._provider_cooldown_artifact_id("quota-a"), "note", {
                "pool": "ollama", "quota_scope": "quota-a",
                "status_code": 429, "fallback_eligible": True,
                "not_before_epoch": time.time() + 60,
            }, "command.controller")

        runtime._load_provider_cooldown("quota-a")

        self.assertGreater(runtime.provider_cooldowns["quota-a"], time.monotonic())
        self.assertFalse(runtime.provider_cooldown_fallback_allowed["quota-a"])

    def test_provider_route_exhaustion_returns_failure_for_original_logical_task(self):
        value = config()
        value["model"].update(
            base_url="http://127.0.0.1:1/v1", protocol="openai_compatible", model="base-model")
        value["model"]["role_routes"] = {
            "strategy.worker": [
                {"id": "qwen-route", "pool": "qwen", "base_url": "http://127.0.0.1:1/v1",
                 "protocol": "openai_compatible", "model": "route-model", "auth_env": None},
                {"id": "gemma-route", "pool": "ollama", "base_url": "http://127.0.0.1:2/v1",
                 "protocol": "openai_compatible", "model": "gemma-model", "auth_env": None},
            ]
        }
        value["limits"]["provider_pools"] = {
            "qwen": {"max_concurrent": 1, "base_urls": ["http://127.0.0.1:1/v1"]},
            "ollama": {"max_concurrent": 1, "base_urls": ["http://127.0.0.1:2/v1"]},
        }
        runtime = ExecutionRuntime(
            self.root / "provider-exhaustion-project", validate_config(value),
            worker_target=provider_exhaustion_worker)
        self.runtimes.append(runtime)
        spec = self.spec("provider-exhausted", delay=0.01)
        spec["params"]["client"] = value["model"]
        outcomes = runtime._call_batch([spec], max_parallel=1)
        self.assertEqual(set(outcomes), {"provider-exhausted"})
        self.assertFalse(outcomes["provider-exhausted"]["ok"])
        self.assertEqual(outcomes["provider-exhausted"]["status_code"], 429)
        self.assertEqual(runtime.tasks.get("provider-exhausted")["state"], "failed")
        self.assertEqual(runtime.provider_active, {"qwen": 0, "ollama": 0})

    def test_provider_route_skips_a_too_small_model_context(self):
        value = config()
        value["model"].update(
            base_url="http://127.0.0.1:1/v1", protocol="openai_compatible", model="base-model")
        value["model"]["role_routes"] = {
            "strategy.worker": [
                {"id": "small-route", "pool": "ollama", "base_url": "http://127.0.0.1:1/v1",
                 "protocol": "openai_compatible", "model": "small-model", "auth_env": None,
                 "context_window_tokens": 3000, "max_input_tokens": 300},
                {"id": "large-route", "pool": "qwen", "base_url": "https://qwen.invalid/v1",
                 "protocol": "openai_compatible", "model": "large-model", "auth_env": None,
                 "context_window_tokens": 9000, "max_input_tokens": 6000},
            ]
        }
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 3, "base_urls": ["http://127.0.0.1:1/v1"]},
            "qwen": {"max_concurrent": 1, "base_urls": ["https://qwen.invalid/v1"]},
        }
        runtime = ExecutionRuntime(self.root / "context-route-project", validate_config(value),
                                   worker_target=execution_worker)
        self.runtimes.append(runtime)
        spec = self.spec("context-route", payload="x" * 2000)
        spec["params"]["client"] = value["model"]
        outcomes = runtime._call_batch([spec], max_parallel=1)
        self.assertTrue(outcomes["context-route"]["ok"])
        record = json.loads(outcomes["context-route"]["result"]["text"])
        self.assertEqual(record["route_id"], "large-route")
        self.assertEqual(record["model"], "large-model")

    def test_context_overflow_is_scoped_to_task_without_worker_attempt(self):
        value = config()
        value["model"].update(
            base_url="http://127.0.0.1:1/v1", protocol="openai_compatible", model="base-model")
        value["model"]["role_routes"] = {
            "strategy.worker": [{
                "id": "small-route", "pool": "ollama", "base_url": "http://127.0.0.1:1/v1",
                "protocol": "openai_compatible", "model": "small-model", "auth_env": None,
                "context_window_tokens": 3000, "max_input_tokens": 300,
            }]
        }
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 3, "base_urls": ["http://127.0.0.1:1/v1"]},
        }
        runtime = ExecutionRuntime(self.root / "context-block-project", validate_config(value),
                                   worker_target=execution_worker)
        self.runtimes.append(runtime)
        spec = self.spec("context-block", payload="x" * 2000)
        spec["params"]["client"] = value["model"]
        outcomes = runtime._call_batch([spec], max_parallel=1)
        self.assertFalse(outcomes["context-block"]["ok"])
        self.assertTrue(outcomes["context-block"]["outcome_known"])
        self.assertIn("context budget", outcomes["context-block"]["error"])
        self.assertEqual(runtime.tasks.get("context-block")["state"], "blocked")
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(runtime.provider_active, {"ollama": 0})

    def test_implicit_fallback_context_selection_reaches_worker(self):
        for pooled in (True, False):
            with self.subTest(pooled=pooled):
                value = config()
                value["model"].update(
                    base_url="http://127.0.0.1:1/v1", protocol="openai_compatible",
                    model="primary", context_window_tokens=3000, max_input_tokens=300)
                value["model"]["role_model_fallbacks"] = {
                    "strategy.worker": [{
                        "base_url": "http://127.0.0.1:2/v1", "protocol": "openai_compatible",
                        "model": "fallback", "context_window_tokens": 9000,
                        "max_input_tokens": 6000,
                    }]
                }
                value["limits"]["provider_pools"] = {
                    "primary": {"max_concurrent": 1, "base_urls": ["http://127.0.0.1:1/v1"]},
                    **({"fallback": {"max_concurrent": 1, "base_urls": ["http://127.0.0.1:2/v1"]}}
                       if pooled else {}),
                }
                runtime = ExecutionRuntime(self.root / f"implicit-context-{pooled}", validate_config(value),
                                           worker_target=execution_worker)
                self.runtimes.append(runtime)
                spec = self.spec(f"implicit-context-{pooled}", payload="x" * 2000)
                spec["params"]["client"] = value["model"]
                outcomes = runtime._call_batch([spec], max_parallel=1)
                self.assertTrue(outcomes[spec["task_id"]]["ok"], outcomes)
                record = json.loads(outcomes[spec["task_id"]]["result"]["text"])
                self.assertEqual(record["model"], "fallback")
                self.assertEqual(record["provider_pool"], "fallback" if pooled else None)
                self.assertEqual(runtime.provider_active, {key: 0 for key in value["limits"]["provider_pools"]})
                self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_implicit_context_selection_preserves_primary_preference_and_cooldown_scope(self):
        value = config()
        value["model"].update(
            base_url="http://127.0.0.1:1/v1", protocol="openai_compatible",
            model="primary", context_window_tokens=9000, max_input_tokens=6000)
        value["model"]["role_model_fallbacks"] = {
            "strategy.worker": [{
                "base_url": "http://127.0.0.1:2/v1", "protocol": "openai_compatible",
                "model": "fallback", "context_window_tokens": 9000, "max_input_tokens": 6000,
            }]
        }
        value["limits"]["provider_pools"] = {
            "primary": {"max_concurrent": 1, "base_urls": ["http://127.0.0.1:1/v1"]},
            "fallback": {"max_concurrent": 1, "base_urls": ["http://127.0.0.1:2/v1"]},
        }
        runtime = ExecutionRuntime(self.root / "implicit-context-preference", validate_config(value),
                                   worker_target=execution_worker)
        self.runtimes.append(runtime)
        spec = self.spec("implicit-context-preference", payload="x" * 2000)
        spec["params"]["client"] = value["model"]
        runtime.provider_active["primary"] = 1
        self.assertIs(runtime._provider_route(spec), _NO_PROVIDER_CAPACITY)
        runtime.provider_active["primary"] = 0
        self.assertEqual(runtime._provider_route(spec)["_effective"]["model"], "primary")
        spec["params"]["client"] = dict(value["model"], max_input_tokens=300)
        selected = runtime._provider_route(spec)["_effective"]
        self.assertEqual(selected["model"], "fallback")
        scope = model_provider_quota_scope(selected)
        runtime._loaded_provider_cooldown_scopes.add(scope)
        runtime.provider_cooldowns[scope] = time.monotonic() + 30
        self.assertIs(runtime._provider_route(spec), _NO_PROVIDER_CAPACITY)
        self.assertGreater(runtime._pending_cooldowns(spec)[0], 0)

    def test_implicit_context_overflow_preserves_all_routes_and_charges_no_attempt(self):
        value = config()
        value["model"].update(
            base_url="http://127.0.0.1:1/v1", protocol="openai_compatible",
            model="primary", context_window_tokens=3000, max_input_tokens=300)
        value["model"]["role_model_fallbacks"] = {
            "strategy.worker": [{"model": "fallback", "max_input_tokens": 400}]
        }
        value["limits"]["provider_pools"] = {
            "primary": {"max_concurrent": 1, "base_urls": ["http://127.0.0.1:1/v1"]},
        }
        runtime = ExecutionRuntime(self.root / "implicit-context-overflow", validate_config(value),
                                   worker_target=execution_worker)
        self.runtimes.append(runtime)
        spec = self.spec("implicit-context-overflow", payload="x" * 2000)
        spec["params"]["client"] = value["model"]
        outcome = runtime._call_batch([spec])[spec["task_id"]]
        self.assertFalse(outcome["ok"])
        self.assertIn("primary", outcome["error"])
        self.assertIn("fallback", outcome["error"])
        self.assertTrue(outcome["outcome_known"])
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup uses POSIX process sessions")
    def test_interrupt_keeps_unknown_cost_releases_undispatched_review_and_stops_descendants(self):
        paths = [self.root / f"pid-{i}.json" for i in range(2)]
        def interrupt_when_running(update):
            if len(update["active_tasks"]) == 2 and all(path.exists() for path in paths):
                raise KeyboardInterrupt("simulated cancellation")
        runtime = self.runtime(capacity=3, on_progress=interrupt_when_running)
        runtime.budget.reserve(window_id="run-window", reservation_id="pending-review", task_id="pending-review",
                               amount={"concurrent_calls": 1})
        prior_children = {child.pid for child in multiprocessing.active_children()}
        specs = [self.spec(f"active-{i}", delay=30, descendant=str(paths[i])) for i in range(2)]
        outcomes = runtime._call_batch([*specs, self.spec("pending-review")], max_parallel=2)
        self.assertFalse(outcomes["active-0"]["outcome_known"])
        self.assertFalse(outcomes["active-1"]["outcome_known"])
        self.assertTrue(outcomes["pending-review"]["outcome_known"])
        self.assertEqual(runtime.tasks.get_attempt("active-0-attempt")["state"], "result_unknown")
        self.assertEqual(runtime.tasks.get("pending-review")["state"], "blocked")
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 2})
        self.assertEqual(runtime.budget.get_reservation("pending-review")["state"], "settled")
        self.assertEqual(runtime.active_tasks, [])
        self.assertIsNone(runtime.active_task)
        self.assertTrue(runtime.cancelled)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, prior_children)
        self.assert_pids_stopped(paths)
        later = runtime._call_batch([self.spec("later")])
        self.assertTrue(later["later"]["outcome_known"])
        self.assertFalse(later["later"]["ok"])
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup uses POSIX process sessions")
    def test_process_termination_cancellation_propagates_after_reconciliation(self):
        callback_count = 0

        def terminate_when_running(update):
            nonlocal callback_count
            callback_count += 1
            if callback_count > 1 and update["active_tasks"]:
                raise KeyboardInterrupt("termination requested")

        runtime = self.runtime(capacity=3, on_progress=terminate_when_running)
        with self.assertRaisesRegex(KeyboardInterrupt, "termination requested"):
            runtime._call_batch([self.spec("terminated", delay=30)], max_parallel=1)
        self.assertEqual(runtime.active_tasks, [])
        self.assertTrue(runtime.cancelled)
        self.assertEqual(runtime.tasks.get_attempt("terminated-attempt")["state"], "result_unknown")
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})

    @unittest.skipUnless(os.name == "posix", "process-group cleanup uses POSIX process sessions")
    def test_deadline_stops_owned_workers_and_blocks_pending_work(self):
        runtime = self.runtime(wall=0.65)
        paths = [self.root / f"deadline-pid-{i}.json" for i in range(2)]
        specs = [self.spec(f"active-{i}", delay=30, descendant=str(paths[i])) for i in range(2)]
        started = time.monotonic()
        outcomes = runtime._call_batch([*specs, self.spec("pending")])
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(outcomes["active-0"]["outcome_known"])
        self.assertFalse(outcomes["active-1"]["outcome_known"])
        self.assertTrue(outcomes["pending"]["outcome_known"])
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 2})
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)
        self.assert_pids_stopped(paths)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup uses POSIX process sessions")
    def test_operation_timeout_keeps_other_workers_and_stops_descendants(self):
        runtime = self.runtime()
        path = self.root / "timeout-pids.json"
        outcomes = runtime._call_batch([
            self.spec("timeout", delay=30, timeout=0.4, descendant=str(path)),
            self.spec("sibling", delay=0.6), self.spec("pending", delay=0.1)])
        self.assertFalse(outcomes["timeout"]["outcome_known"])
        self.assertTrue(outcomes["sibling"]["ok"])
        self.assertTrue(outcomes["pending"]["ok"])
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})
        self.assert_pids_stopped([path])

    def test_malformed_known_flag_cannot_release_uncertain_capacity(self):
        runtime = self.runtime()
        outcome = runtime._call_batch([self.spec("malformed", delay=0.01, failure="malformed")])["malformed"]
        self.assertFalse(outcome["outcome_known"])
        self.assertEqual(runtime.tasks.get_attempt("malformed-attempt")["state"], "result_unknown")
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})

    def test_existing_runtime_requires_explicit_recovery_and_reconciles_unknown_attempt(self):
        value = config()
        value["limits"].update(concurrent_calls=2, wall_clock_seconds=15, checkpoint_seconds=1)
        value["model"]["timeout_seconds"] = 5
        source_root = self.root / "source"
        (source_root / "scisaurus").mkdir(parents=True)
        (source_root / "scisaurus" / "worker.py").write_text("VERSION = 1\n")
        run_dir = self.root / "recoverable"
        runtime = ExecutionRuntime(run_dir, validate_config(value), worker_target=execution_worker,
                                   repository_root=source_root)
        self.runtimes.append(runtime)
        outcome = runtime._call_batch([self.spec("unknown", delay=0.01, failure="unknown")])["unknown"]
        self.assertFalse(outcome["outcome_known"])
        runtime.control.close()
        self.runtimes.remove(runtime)
        with self.assertRaisesRegex(ValidationError, "new project directory"):
            ExecutionRuntime(run_dir, validate_config(value), worker_target=execution_worker,
                             repository_root=source_root)
        policy = {"additional_seconds": 10,
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        resumed = ExecutionRuntime(run_dir, validate_config(value), worker_target=execution_worker,
                                   repository_root=source_root, resume_policy=policy)
        self.runtimes.append(resumed)
        self.assertEqual(resumed.tasks.get_attempt("unknown-attempt")["state"], "failed")
        self.assertEqual(resumed.budget.get_window("run-window")["reserved"], {})
        self.assertEqual(resumed.resume_session["unknown_reconciliations"][0]["task_id"], "unknown")

    def test_completed_result_is_recorded_but_single_call_still_propagates_cancellation(self):
        count = 0
        def cancel_after_publication(update):
            nonlocal count
            if update["phase"] != "executing":
                return
            count += 1
            if count == 2:
                path = runtime.dir / "runs" / "final-review" / "result.json"
                deadline = time.monotonic() + 2
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(path.exists())
                raise KeyboardInterrupt("cancel after completed result")
        runtime = self.runtime(on_progress=cancel_after_publication)
        spec = self.spec("final-review", delay=0.3)
        with self.assertRaisesRegex(KeyboardInterrupt, "cancel after completed result"):
            runtime._call("final-review", "model", spec["params"], actor="methods.verifier", task_kind="verification")
        self.assertTrue(runtime.cancelled)
        self.assertEqual(runtime.tasks.get_attempt("final-review-attempt")["state"], "succeeded")
        self.assertEqual(runtime.tasks.get("final-review")["state"], "awaiting_review")
        self.assertIsNotNone(runtime.store.head("command/executions/final-review"))
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {})
        self.assertEqual(runtime.budget.get_window("run-window")["cumulative_usage"],
                         {"model_calls": 1, "input_tokens": 10, "output_tokens": 5})

    def test_successful_single_call_cannot_continue_past_the_run_deadline(self):
        count = 0
        def delay_parent_after_dispatch(update):
            nonlocal count
            count += 1
            if count == 2:
                while time.monotonic() <= runtime.deadline + 0.01:
                    time.sleep(0.01)
        runtime = self.runtime(wall=0.7, on_progress=delay_parent_after_dispatch)
        spec = self.spec("expired", delay=0.2)
        with self.assertRaisesRegex(ValidationError, "deadline"):
            runtime._call("expired", "model", spec["params"], actor="methods.verifier", task_kind="verification")
        self.assertEqual(runtime.tasks.get_attempt("expired-attempt")["state"], "succeeded")
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {})

    def test_reservation_cannot_be_reused_for_another_task(self):
        runtime = self.runtime()
        runtime.budget.reserve(window_id="run-window", reservation_id="review", task_id="review",
                               amount={"concurrent_calls": 1})
        with self.assertRaises(ValidationError):
            runtime._call_batch([self.spec("writer", reservation_id="review")])
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
