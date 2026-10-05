"""Wire-level model protocol checks against an explicit local test server."""
from contextlib import closing
import json
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import (
    ModelClient, ModelCallError, ModelContextBudgetError, ModelResult,
    clear_model_provider_cooldown, complete_with_role_fallbacks, effective_model_timeout,
    estimate_input_tokens, model_context_budget, model_context_error,
    is_local_qwen_route, model_provider_cooldown_remaining, model_route_candidates, role_config_for,
    role_routes_for, resolve_model_config, with_runtime_cooldown_fallback, load_model_config,
)


class TestModelClient(unittest.TestCase):
    def test_shared_routing_is_reloaded_without_stale_inline_roles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            shared = {"model": "primary", "base_url": "https://example.test/v1",
                      "role_models": {"methods.methodologist": {"model": "glm"}},
                      "role_model_fallbacks": {"methods.methodologist": [{"model": "deepseek"}]},
                      "role_routes": {"research.author": [{"id": "primary", "pool": "cloud",
                          "model": "glm", "base_url": "https://example.test/v1"}]}}
            path.write_text(json.dumps(shared))
            reference = {"config_path": str(path), "max_output_tokens": 1234,
                         "model_call_budget_scopes": [{"model_call_budget_path": str(Path(directory)/"budget.sqlite"),
                             "model_call_budget_key": "owner", "model_call_budget_limit": 5}]}
            resolved = resolve_model_config(reference, role="methods.methodologist")
            self.assertEqual(resolved["model"], "glm")
            self.assertEqual(resolved["max_output_tokens"], 1234)
            self.assertEqual(resolved["model_call_budget_scopes"], reference["model_call_budget_scopes"])
            self.assertNotIn("config_path", resolved)
            self.assertEqual(role_routes_for(reference, "research.author")[0]["model"], "glm")
            self.assertEqual([item["model"] for item in model_route_candidates(
                reference, role="methods.methodologist")], ["glm", "deepseek"])
            shared["role_models"]["methods.methodologist"]["model"] = "changed"
            path.write_text(json.dumps(shared))
            self.assertEqual(resolve_model_config(reference, role="methods.methodologist")["model"], "changed")
            self.assertEqual(reference["config_path"], str(path))

    def test_shared_model_preserves_both_budget_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            owner = {"model_call_budget_path": str(Path(directory)/"budget.sqlite"),
                     "model_call_budget_key": "shared", "model_call_budget_limit": 5}
            stage = {**owner, "model_call_budget_key": "stage"}
            path.write_text(json.dumps({"model": "glm", "base_url": "https://example.test/v1",
                                        "model_call_budget_scopes": [owner]}))
            configured = load_model_config({"config_path": str(path), "model_call_budget_scopes": [stage]})
            self.assertEqual(configured["model_call_budget_scopes"], [owner, stage])

    def test_shared_routing_rejects_conflicts_missing_files_and_indirection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_text(json.dumps({"model": "glm", "base_url": "https://example.test/v1"}))
            for field in ("role_models", "role_routes", "role_model_fallbacks", "model", "base_url"):
                with self.subTest(field=field), self.assertRaises(ValidationError):
                    load_model_config({"config_path": str(path), field: "stale"})
            for bad in ("relative.json", str(Path(directory)/"missing.json")):
                with self.assertRaises(ValidationError):
                    load_model_config({"config_path": bad})
            for bad in ([], {"config_path": str(path)}):
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValidationError):
                    load_model_config({"config_path": str(path)})

    def test_development_dispatch_preserves_costs_without_admission_ceiling(self):
        from scisaurus.runtime.models import (_reserve_model_call_budgets, _settle_model_token_budgets,
            model_call_budget_remaining, register_model_token_budget, model_token_budget_usage,
            ModelBudgetExceededError)
        with tempfile.TemporaryDirectory() as directory:
            scope = {"model_call_budget_path": str(Path(directory)/"owner.sqlite"),
                     "model_call_budget_key": "stage", "model_call_budget_limit": 1,
                     "model_token_budget_limits": {"input_tokens": 1, "output_tokens": 1}}
            register_model_token_budget(scope)
            with self.assertRaises(ModelBudgetExceededError):
                _reserve_model_call_budgets([scope], token_reservation={"input_tokens": 10, "output_tokens": 5})
            with patch.dict("os.environ", {"SCISAURUS_EXECUTION_POLICY": "development"}):
                self.assertIsNone(model_call_budget_remaining(scope))
                for _ in range(3):
                    reserved = _reserve_model_call_budgets([scope], token_reservation={"input_tokens": 10, "output_tokens": 5})
                    _settle_model_token_budgets(reserved, {"input_tokens": 7, "output_tokens": 2})
                self.assertIsNone(model_call_budget_remaining(scope))
                self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 21, "output_tokens": 6})
            self.assertEqual(model_call_budget_remaining(scope), 0)
            with self.assertRaises(ModelBudgetExceededError):
                _reserve_model_call_budgets([scope], token_reservation={"input_tokens": 1, "output_tokens": 1})
            with sqlite3.connect(scope["model_call_budget_path"]) as c:
                self.assertEqual(c.execute("SELECT * FROM model_call_budgets").fetchone(), ("stage", 1, 3))
                self.assertEqual(c.execute("SELECT COUNT(*) FROM model_development_admissions").fetchone()[0], 3)
                self.assertEqual(c.execute("SELECT max_input,max_output FROM model_token_budgets").fetchone(), (1, 1))
            with patch.dict("os.environ", {"SCISAURUS_EXECUTION_POLICY": "invalid"}):
                with self.assertRaises(ValidationError):
                    model_call_budget_remaining(scope)

    def test_rejected_development_scope_rolls_back_accounting_and_receipt(self):
        from scisaurus.runtime.models import _reserve_model_call_budgets, ModelCallError
        with tempfile.TemporaryDirectory() as directory:
            scope = {"model_call_budget_path": str(Path(directory)/"owner.sqlite"),
                     "model_call_budget_key": "stage", "model_call_budget_limit": 1}
            bad = {**scope, "model_call_budget_path": str(Path(directory)/"bad.sqlite")}
            with sqlite3.connect(bad["model_call_budget_path"]) as c:
                c.execute("CREATE TABLE model_call_budgets(budget_key TEXT PRIMARY KEY,max_calls INTEGER,used_calls INTEGER)")
                c.execute("INSERT INTO model_call_budgets VALUES ('stage',2,0)")
            with patch.dict("os.environ", {"SCISAURUS_EXECUTION_POLICY": "development"}):
                with self.assertRaises(ModelCallError):
                    _reserve_model_call_budgets([scope, bad])
            with sqlite3.connect(scope["model_call_budget_path"]) as c:
                self.assertEqual(c.execute("SELECT used_calls FROM model_call_budgets").fetchone()[0], 0)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM model_development_admissions").fetchone()[0], 0)

    def test_explicit_token_capacity_preserves_costs_and_base_allocation(self):
        from scisaurus.runtime.models import (grant_model_token_capacity, register_model_token_budget,
            model_token_budget_limits, model_token_budget_usage, _reserve_model_call_budgets,
            _settle_model_token_budgets, ModelBudgetExceededError)
        with tempfile.TemporaryDirectory() as directory:
            scope = {"model_call_budget_path": str(Path(directory)/"owner.sqlite"),
                "model_call_budget_key": "stage", "model_call_budget_limit": 5,
                "model_token_budget_limits": {"input_tokens": 20, "output_tokens": 30}}
            register_model_token_budget(scope, {"input_tokens": 19, "output_tokens": 3})
            with self.assertRaises(ModelBudgetExceededError):
                _reserve_model_call_budgets([scope], token_reservation={"input_tokens": 10, "output_tokens": 2})
            receipt = grant_model_token_capacity(scope["model_call_budget_path"], "stage",
                grant_id="repair", input_tokens=20, reason="Verified runtime repair")
            self.assertEqual(grant_model_token_capacity(scope["model_call_budget_path"], "stage",
                grant_id="repair", input_tokens=20, reason="Verified runtime repair"), receipt)
            self.assertEqual(model_token_budget_limits(scope), {"input_tokens": 40, "output_tokens": 30})
            self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 19, "output_tokens": 3})
            reservations = _reserve_model_call_budgets([scope], token_reservation={"input_tokens": 10, "output_tokens": 2})
            _settle_model_token_budgets(reservations, {"input_tokens": 7, "output_tokens": 1})
            register_model_token_budget(scope)
            self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 26, "output_tokens": 4})
            with self.assertRaises(ValidationError):
                grant_model_token_capacity(scope["model_call_budget_path"], "stage",
                    grant_id="repair", input_tokens=21, reason="Verified runtime repair")
            changed = {**scope, "model_token_budget_limits": {"input_tokens": 40, "output_tokens": 30}}
            with self.assertRaises(ValidationError):
                register_model_token_budget(changed)
            with sqlite3.connect(scope["model_call_budget_path"]) as connection:
                self.assertEqual(connection.execute("SELECT * FROM model_token_budgets").fetchone(), ("stage", 20, 30, 26, 4))
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM model_token_capacity_grants").fetchone()[0], 1)

    def test_remaining_call_capacity_is_read_only_and_matches_atomic_reservations(self):
        from scisaurus.runtime.models import (
            _reserve_model_call_budgets, model_call_budget_available, model_call_budget_remaining,
        )
        self.assertIsNone(model_call_budget_remaining({}))
        self.assertTrue(model_call_budget_available({}))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "budget.sqlite"
            scope = {"model_call_budget_path": str(path), "model_call_budget_key": "stage",
                     "model_call_budget_limit": 3}
            self.assertEqual(model_call_budget_remaining(scope), 3)
            self.assertFalse(path.exists())
            _reserve_model_call_budgets([scope])
            before = path.read_bytes()
            self.assertEqual(model_call_budget_remaining(scope), 2)
            self.assertTrue(model_call_budget_available(scope))
            self.assertEqual(path.read_bytes(), before)
            _reserve_model_call_budgets([scope])
            _reserve_model_call_budgets([scope])
            self.assertEqual(model_call_budget_remaining(scope), 0)
            self.assertFalse(model_call_budget_available(scope))
            with self.assertRaises(ValidationError):
                model_call_budget_remaining({**scope, "model_call_budget_limit": 4})
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE model_call_budgets SET used_calls=4 WHERE budget_key='stage'")
            self.assertEqual(model_call_budget_remaining(scope), 0)
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE model_call_budgets SET used_calls=-1 WHERE budget_key='stage'")
            with self.assertRaises(ValidationError):
                model_call_budget_remaining(scope)

    def test_model_failure_round_trip_preserves_admission_and_provider_facts(self):
        from scisaurus.runtime.models import ModelBudgetExceededError
        from scisaurus.core.errors import QuotaExceededError
        admission = {"path": "/tmp/owner.sqlite", "key": "stage", "dimension": "input_tokens",
                     "limit": 100, "observed": 70, "reserved": 20, "requested": 11}
        error = ModelBudgetExceededError("admission rejected", budget_admission=admission,
            outcome_known=True, elapsed_seconds=0.25)
        error.usage = {"input_tokens": 7}
        for original in (error, ModelCallError("rate limited", status_code=429,
                retry_after_seconds=60, provider_error_kind="rate_limited", attempts=1)):
            details = original.failure_details()
            restored = ModelCallError.from_failure(str(original), json.loads(json.dumps(details)))
            self.assertEqual(restored.failure_details(), details)
            self.assertEqual(type(restored), type(original))
        self.assertIsInstance(ModelCallError.from_failure(str(error), error.failure_details()), QuotaExceededError)
        with self.assertRaises(ValidationError):
            ModelCallError.from_failure("bad fence", {"budget_admission": {"dimension": "input_tokens"}})

    def test_token_reservations_are_atomic_and_settle_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from scisaurus.runtime.models import (_reserve_model_call_budgets,
            _settle_model_token_budgets, model_token_budget_usage, register_model_token_budget)
        with tempfile.TemporaryDirectory() as path:
            scope = {"model_call_budget_path": str(Path(path)/"budget.sqlite"),
                     "model_call_budget_key": "stage", "model_call_budget_limit": 20,
                     "model_token_budget_limits": {"input_tokens": 30, "output_tokens": 30}}
            register_model_token_budget(scope)
            def reserve(_):
                try:
                    legacy = {key: value for key, value in scope.items() if key != "model_token_budget_limits"}
                    return _reserve_model_call_budgets([legacy, scope, scope],
                        token_reservation={"input_tokens": 20, "output_tokens": 20})
                except ModelCallError:
                    return None
            with ThreadPoolExecutor(max_workers=8) as pool:
                receipts = [value for value in pool.map(reserve, range(8)) if value]
            self.assertEqual(len(receipts), 1)
            self.assertEqual(len(receipts[0]), 1)
            _settle_model_token_budgets(receipts[0], None)
            self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 0, "output_tokens": 0})
            self.assertIsNone(reserve(0))
            _settle_model_token_budgets(receipts[0], {"input_tokens": 7, "output_tokens": 4})
            _settle_model_token_budgets(receipts[0], {"input_tokens": 7, "output_tokens": 4})
            register_model_token_budget(scope, {"input_tokens": 3, "output_tokens": 2})
            self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 7, "output_tokens": 4})
            self.assertIsNotNone(reserve(0))
            with closing(sqlite3.connect(scope["model_call_budget_path"])) as connection:
                self.assertEqual(connection.execute("SELECT used_calls FROM model_call_budgets").fetchone()[0], 2)

    def test_rejected_owner_releases_call_and_token_reservations(self):
        from scisaurus.runtime.models import _reserve_model_call_budgets, register_model_token_budget
        with tempfile.TemporaryDirectory() as path:
            primary = {"model_call_budget_path": str(Path(path)/"budget.sqlite"),
                       "model_call_budget_key": "stage", "model_call_budget_limit": 10,
                       "model_token_budget_limits": {"input_tokens": 100, "output_tokens": 100}}
            owner = {**primary, "model_call_budget_key": "owner", "model_call_budget_limit": 1}
            register_model_token_budget(primary)
            register_model_token_budget(owner)
            _reserve_model_call_budgets([owner], token_reservation={"input_tokens": 1, "output_tokens": 1})
            with self.assertRaisesRegex(ModelCallError, "budget exhausted"):
                _reserve_model_call_budgets([primary, owner],
                    token_reservation={"input_tokens": 50, "output_tokens": 50})
            with closing(sqlite3.connect(primary["model_call_budget_path"])) as connection:
                self.assertEqual(connection.execute("SELECT used_calls FROM model_call_budgets WHERE budget_key='stage'").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM model_token_reservations WHERE budget_key='stage'").fetchone()[0], 0)

    def test_http_token_cost_restores_and_blocks_before_provider(self):
        from scisaurus.runtime.models import model_token_budget_usage, register_model_token_budget
        with tempfile.TemporaryDirectory() as path:
            scope = {"model_call_budget_path": str(Path(path)/"budget.sqlite"),
                     "model_call_budget_key": "stage", "model_call_budget_limit": 10,
                     "model_token_budget_limits": {"input_tokens": 500, "output_tokens": 100}}
            client = ModelClient(base_url=self.url, protocol="ollama", model="test", timeout_seconds=1,
                                 max_output_tokens=64, model_call_budget_scopes=[scope])
            result = client.complete(system="instruction", prompt="data")
            self.assertEqual(result.usage, {"model_calls": 1, "input_tokens": 10, "output_tokens": 5})
            self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 10, "output_tokens": 5})
            register_model_token_budget(scope, {"input_tokens": 501, "output_tokens": 5})
            with self.assertRaisesRegex(ModelCallError, "token budget exhausted") as failure:
                client.complete(system="instruction", prompt="data")
            self.assertEqual(self.calls, 1)
            self.assertEqual(failure.exception.attempts, 0)
            from scisaurus.core.errors import QuotaExceededError
            self.assertIsInstance(failure.exception, QuotaExceededError)
            self.assertEqual(failure.exception.budget_admission["dimension"], "input_tokens")
            self.assertTrue(failure.exception.outcome_known)

    def test_unknown_http_token_reservation_survives_restart(self):
        from scisaurus.runtime.models import model_token_budget_usage
        with tempfile.TemporaryDirectory() as path:
            scope = {"model_call_budget_path": str(Path(path)/"budget.sqlite"),
                     "model_call_budget_key": "stage", "model_call_budget_limit": 10,
                     "model_token_budget_limits": {"input_tokens": 500, "output_tokens": 100}}
            def client():
                return ModelClient(base_url=self.url, protocol="ollama", model="test", timeout_seconds=1,
                                   max_output_tokens=64, model_call_budget_scopes=[scope])
            self.response = b"broken"
            with self.assertRaises(ModelCallError):
                client().complete(system="instruction", prompt="data")
            self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 0, "output_tokens": 0})
            with self.assertRaisesRegex(ModelCallError, "token budget exhausted"):
                client().complete(system="instruction", prompt="data")
            self.assertEqual(self.calls, 1)
            with closing(sqlite3.connect(scope["model_call_budget_path"])) as connection:
                self.assertEqual(connection.execute("SELECT state FROM model_token_reservations").fetchone()[0], "unknown")

    def test_invalid_output_retains_known_token_costs(self):
        from scisaurus.runtime.models import model_token_budget_usage
        for content, reason in (("", "stop"), ("valid", "unsupported")):
            with self.subTest(content=content, reason=reason), tempfile.TemporaryDirectory() as path:
                scope = {"model_call_budget_path": str(Path(path)/"budget.sqlite"),
                         "model_call_budget_key": "stage", "model_call_budget_limit": 10,
                         "model_token_budget_limits": {"input_tokens": 500, "output_tokens": 100}}
                self.response = {"model": "test", "done": True, "done_reason": reason,
                                 "message": {"content": content}, "prompt_eval_count": 17, "eval_count": 9}
                client = ModelClient(base_url=self.url, protocol="ollama", model="test", timeout_seconds=1,
                                     max_output_tokens=64, model_call_budget_scopes=[scope])
                with self.assertRaisesRegex(ModelCallError, "invalid or incomplete") as error:
                    client.complete(system="instruction", prompt="data")
                self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 17, "output_tokens": 9})
                self.assertEqual(error.exception.usage, {"model_calls": 1, "input_tokens": 17, "output_tokens": 9})
                self.assertTrue(error.exception.outcome_known)

    def test_partial_token_usage_preserves_only_unknown_reservations(self):
        from scisaurus.runtime.models import (_reserve_model_call_budgets,
            _settle_model_token_budgets, model_token_budget_usage)
        with tempfile.TemporaryDirectory() as path:
            scope = {"model_call_budget_path": str(Path(path)/"budget.sqlite"),
                     "model_call_budget_key": "stage", "model_call_budget_limit": 10,
                     "model_token_budget_limits": {"input_tokens": 100, "output_tokens": 100}}
            reservation = _reserve_model_call_budgets([scope],
                token_reservation={"input_tokens": 80, "output_tokens": 70})
            _settle_model_token_budgets(reservation, {"input_tokens": 11})
            _settle_model_token_budgets(reservation, {"input_tokens": 11})
            self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 11, "output_tokens": 0})
            with closing(sqlite3.connect(scope["model_call_budget_path"])) as connection:
                self.assertEqual(connection.execute("SELECT input_tokens,output_tokens,state FROM model_token_reservations").fetchone(), (0,70,"unknown"))
            with self.assertRaisesRegex(ValidationError, "immutable"):
                _settle_model_token_budgets(reservation, {"input_tokens": 12})
            _settle_model_token_budgets(reservation, {"input_tokens": 11, "output_tokens": 9})
            self.assertEqual(model_token_budget_usage(scope), {"input_tokens": 11, "output_tokens": 9})

    def test_route_primary_budget_preserves_parent_http_owner(self):
        from scisaurus.runtime.models import merge_model_config, _reserve_model_call_budgets
        with tempfile.TemporaryDirectory() as directory:
            parent = {"model_call_budget_path": str(Path(directory) / "owners.sqlite"),
                      "model_call_budget_key": "owner:base", "model_call_budget_limit": 1}
            route = {**parent, "model_call_budget_key": "owner:route", "model_call_budget_limit": 3}
            merged = merge_model_config(parent, route)
            self.assertIn(parent, merged["model_call_budget_scopes"])
            _reserve_model_call_budgets([parent])
            connections = []
            connect = sqlite3.connect

            def track(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connections.append(connection)
                return connection

            with patch("scisaurus.runtime.models.sqlite3.connect", side_effect=track):
                with self.assertRaisesRegex(ModelCallError, "budget exhausted"):
                    _reserve_model_call_budgets([merged, *merged["model_call_budget_scopes"]])
            self.assertEqual(len(connections), 3)
            for connection in connections:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            with closing(sqlite3.connect(parent["model_call_budget_path"])) as connection, connection:
                self.assertEqual(connection.execute("SELECT used_calls FROM model_call_budgets WHERE budget_key=?",
                                                   (route["model_call_budget_key"],)).fetchone()[0], 0)

    def test_local_qwen_routes_are_rejected_but_remote_qwen_is_allowed(self):
        for base_url in (
                "http://127.0.0.1:11434/v1", "http://localhost:11434/v1",
                "http://[::1]:11434/v1", "http://127.1:11434/v1",
                "http://0.0.0.0:11434/v1", "http://[::]:11434/v1",
                "http://localhost.localdomain:11434/v1",
                "http://[::ffff:127.0.0.1]:11434/v1"):
            with self.subTest(base_url=base_url):
                self.assertTrue(is_local_qwen_route({
                    "base_url": base_url, "model": "qwen3.8:27b-mlx"}))
                with self.assertRaisesRegex(ValidationError, "local Qwen"):
                    ModelClient(
                        base_url=base_url, protocol="openai_compatible",
                        model="qwen3.8:27b-mlx", timeout_seconds=1,
                        max_output_tokens=64)

        public_record = (
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
            ("203.0.113.9", 0),
        )
        with patch("scisaurus.runtime.models.socket.getaddrinfo",
                   return_value=[public_record]):
            remote = ModelClient(
                base_url="https://qwen.example/v1", protocol="openai_compatible",
                model="qwen3.8-27b", timeout_seconds=1, max_output_tokens=64)
        self.assertEqual(remote.model, "qwen3.8-27b")
        self.assertFalse(is_local_qwen_route({
            "base_url": "https://qwen.example/v1", "model": "qwen3.8-27b"}))

    def test_qwen_hostname_alias_resolving_to_loopback_is_rejected(self):
        loopback_record = (
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
            ("127.0.0.1", 0),
        )
        with patch("scisaurus.runtime.models.socket.getaddrinfo",
                   return_value=[loopback_record]):
            self.assertTrue(is_local_qwen_route({
                "base_url": "http://localhost6:11434/v1",
                "model": "qwen3.8:27b-mlx",
            }))

            with self.assertRaisesRegex(ValidationError, "local Qwen"):
                ModelClient(
                    base_url="http://operator-alias:11434/v1",
                    protocol="openai_compatible", model="qwen3.8:27b-mlx",
                    timeout_seconds=1, max_output_tokens=64)

    def test_qwen_connected_loopback_peer_is_rejected_before_request_body(self):
        public_record = (
            socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
            ("203.0.113.9", 0),
        )
        client_args = {
            "base_url": "http://qwen.example/v1",
            "protocol": "openai_compatible",
            "model": "qwen3.8-remote", "timeout_seconds": 2,
            "max_output_tokens": 64,
        }

        class LoopbackPeer:
            def getpeername(self):
                return ("127.0.0.1", 11434)

            def close(self):
                pass

        def connect_to_loopback(connection):
            connection.sock = LoopbackPeer()

        with patch("scisaurus.runtime.models.socket.getaddrinfo",
                   return_value=[public_record]), \
                patch("http.client.HTTPConnection.connect", connect_to_loopback), \
                patch("http.client.HTTPConnection.request") as request:
            client = ModelClient(**client_args)
            with self.assertRaisesRegex(ValidationError, "local Qwen"):
                client.complete(system="instruction", prompt="must not be sent")
            request.assert_not_called()

    def test_request_uses_model_and_endpoint_snapshot(self):
        from scisaurus.runtime import models

        client = self.client("openai_compatible")
        self.response = {
            "choices": [{"message": {"content": "{\"ok\":true}"},
                         "finish_reason": "stop"}],
        }
        original_budget = models.model_context_budget

        def mutate_client_after_admission(config, **kwargs):
            client.model = "qwen3.8:27b-mlx"
            client.base_url = "http://127.0.0.1:1/v1"
            return original_budget(config, **kwargs)

        with patch("scisaurus.runtime.models.model_context_budget",
                   side_effect=mutate_client_after_admission):
            client.complete(system="instruction", prompt="snapshot test")

        self.assertEqual(self.calls, 1)
        self.assertEqual(self.path, "/v1/chat/completions")
        self.assertEqual(self.request["model"], "configured-model")

    def test_mutated_qwen_client_is_rejected_before_http_dispatch(self):
        client = self.client("openai_compatible")
        client.base_url = self.url + "/v1"
        client.model = "qwen3.8:27b-mlx"

        with self.assertRaisesRegex(ValidationError, "local Qwen"):
            client.complete(system="instruction", prompt="must not be sent")

        self.assertEqual(self.calls, 0)

    def test_role_resolution_skips_local_qwen_for_safe_default(self):
        resolved = resolve_model_config({
            "base_url": "http://127.0.0.1:11434/v1",
            "protocol": "openai_compatible", "model": "safe-default",
            "role_models": {"methods.methodologist": {
                "model": "qwen3.8:27b-mlx",
            }},
        }, role="methods.methodologist")
        self.assertEqual(resolved["model"], "safe-default")

    def test_dotted_subroles_inherit_the_most_specific_parent_route(self):
        model = {
            "base_url": "https://models.example/v1",
            "protocol": "openai_compatible",
            "model": "base",
            "role_models": {
                "methods.experiment-reviewer": {"model": "glm-flash"},
                "methods.experiment-reviewer.statistical": {"model": "gemma"},
            },
            "role_model_fallbacks": {
                "methods.experiment-reviewer": [{"model": "deepseek-flash"}],
            },
            "role_routes": {
                "methods.experiment-reviewer": [
                    {"id": "glm", "pool": "ollama", "protocol": "openai_compatible",
                     "base_url": "https://models.example/v1", "model": "glm-flash"},
                    {"id": "deepseek", "pool": "ollama", "protocol": "openai_compatible",
                     "base_url": "https://models.example/v1", "model": "deepseek-flash"},
                ],
            },
        }
        role = "methods.experiment-reviewer.statistical_method"

        self.assertEqual(
            role_config_for(model["role_models"], role)["model"], "glm-flash")
        self.assertEqual(
            resolve_model_config(model, role=role)["model"], "glm-flash")
        self.assertEqual(
            [route["model"] for route in role_routes_for(model, role)],
            ["glm-flash", "deepseek-flash"],
        )
        self.assertEqual(
            [route["model"] for route in model_route_candidates(model, role=role)],
            ["glm-flash", "deepseek-flash"],
        )

    def test_local_qwen_is_removed_from_inherited_fallbacks_and_routes(self):
        local_qwen = {
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "qwen3.8:27b-mlx",
        }
        model = {
            **local_qwen,
            "model": "deepseek-flash",
            "role_model_fallbacks": {
                "methods.experiment-reviewer": [
                    {**local_qwen, "model": "qwen3.8:27b-mlx"},
                    {"model": "gemma4:31b-cloud"},
                ],
            },
            "role_routes": {
                "methods.experiment-reviewer": [
                    {"id": "local-qwen", "pool": "ollama", **local_qwen},
                    {"id": "gemma", "pool": "ollama",
                     "protocol": "openai_compatible",
                     "base_url": "http://127.0.0.1:11434/v1",
                     "model": "gemma4:31b-cloud"},
                ],
            },
        }
        role = "methods.experiment-reviewer.statistical_method"

        self.assertEqual(
            [route["model"] for route in model_route_candidates(model, role=role)],
            ["deepseek-flash", "gemma4:31b-cloud"],
        )
        self.assertEqual(
            [route["id"] for route in role_routes_for(model, role)], ["gemma"])

    def test_effective_timeout_uses_route_and_explicit_deadline_bounds(self):
        self.assertEqual(effective_model_timeout(1800), 1800.0)
        self.assertEqual(effective_model_timeout(1800, 1200, 900), 900.0)
        with self.assertRaisesRegex(ValidationError, "timeout bounds"):
            effective_model_timeout(1800, 0)

    def setUp(self):
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.path = self.path
                outer.request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.calls += 1
                if outer.slow_headers:
                    time.sleep(0.5)
                status = outer.status[min(outer.calls - 1, len(outer.status) - 1)] if isinstance(outer.status, list) else outer.status
                if status != 200:
                    self.send_response(status)
                    if status == 429:
                        self.send_header('Retry-After', '0')
                    self.end_headers()
                    self.wfile.write(outer.failure_body)
                    return
                self.send_response(200)
                if outer.trickle_chunked:
                    self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                response = (outer.response_sequence[min(outer.calls - 1, len(outer.response_sequence) - 1)]
                            if outer.response_sequence else outer.response)
                body = response if isinstance(response, (bytes, bytearray)) else json.dumps(response).encode()
                if outer.slow_body:
                    self.wfile.write(body[:1])
                    self.wfile.flush()
                    time.sleep(0.25)
                    self.wfile.write(body[1:])
                    self.wfile.flush()
                elif outer.trickle_body:
                    try:
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.02)
                    except BrokenPipeError:
                        pass
                elif outer.trickle_chunked:
                    try:
                        header = b'1;' + (b'x' * 64) + b'\r\n'
                        for byte in header:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.02)
                        self.wfile.write(b'x\r\n0\r\n\r\n')
                        self.wfile.flush()
                    except BrokenPipeError:
                        pass
                else:
                    try:
                        self.wfile.write(body)
                    except BrokenPipeError:
                        pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.status = 200
        self.calls = 0
        self.response_sequence = None
        self.failure_body = b'private provider failure content'
        self.slow_body = False
        self.slow_headers = False
        self.trickle_body = False
        self.trickle_chunked = False
        self.response = {'model':'served-model','message':{'content':'{"value": 4}'}, 'done':True,
                         'done_reason':'stop', 'prompt_eval_count':10, 'eval_count':5}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def client(self, protocol='ollama', **kwargs):
        return ModelClient(base_url=self.url + ('/v1' if protocol=='openai_compatible' else ''),
                           protocol=protocol, model='configured-model', timeout_seconds=2, max_output_tokens=4096, **kwargs)

    def test_ollama_round_trip_and_reported_usage(self):
        result=self.client().complete(system='instruction', prompt='data')
        self.assertEqual(self.path, '/api/chat')
        self.assertEqual(self.request['options']['num_predict'],4096)
        self.assertFalse(self.request['stream'])
        self.assertEqual(result.json_object(), {'value':4})
        self.assertEqual(result.usage, {'model_calls':1,'input_tokens':10,'output_tokens':5})
        self.assertEqual(result.model,'served-model')

    def test_native_ollama_receives_role_specific_context_window(self):
        result = self.client('ollama', context_window_tokens=32768).complete(
            system='instruction', prompt='data')
        self.assertEqual(result.json_object(), {'value': 4})
        self.assertEqual(self.request['options']['num_ctx'], 32768)

    def test_native_ollama_json_mode_is_sent_as_format_json(self):
        result = self.client('ollama', output_format='json_object').complete(
            system='Return one JSON object.', prompt='data')
        self.assertEqual(result.json_object(), {'value': 4})
        self.assertEqual(self.request['format'], 'json')

    def test_native_ollama_json_continuation_disables_whole_document_mode(self):
        self.response = {
            'model': 'served-model', 'message': {'content': '4}'}, 'done': True,
            'done_reason': 'stop', 'prompt_eval_count': 20, 'eval_count': 2,
        }
        result = self.client('ollama', output_format='json_object').complete(
            system='Return one JSON object.', prompt='Build the declared object.',
            continuation_text='{"value": ',
        )
        self.assertEqual(result.text, '4}')
        self.assertNotIn('format', self.request)
        self.assertEqual(self.request['messages'][-2], {
            'role': 'assistant', 'content': '{"value": ',
        })
        self.assertIn('missing suffix', self.request['messages'][-1]['content'])

    def test_openai_compatible_bridge_does_not_claim_dynamic_ollama_context(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        self.client('openai_compatible', context_window_tokens=32768).complete(
            system='instruction', prompt='data')
        self.assertNotIn('num_ctx', self.request)
        self.assertNotIn('options', self.request)

    def test_compatible_protocol_and_missing_usage_remains_unknown(self):
        self.response={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]}
        result=self.client('openai_compatible').complete(system='instruction',prompt='data')
        self.assertEqual(self.path,'/v1/chat/completions')
        self.assertEqual(self.request['max_tokens'],4096)
        self.assertNotIn('reasoning_effort', self.request)
        self.assertNotIn('response_format', self.request)
        self.assertEqual(result.usage,{'model_calls':1})
        self.assertNotIn('input_tokens',result.usage)

    def test_json_response_continuation_preserves_prefix_and_disables_json_mode(self):
        self.response_sequence = [{
            'model': 'served-model',
            'choices': [{'message': {'content': '4}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 20, 'completion_tokens': 2},
        }]
        result = self.client(
            'openai_compatible', output_format='json_object',
            context_window_tokens=8192, max_input_tokens=2048,
        ).complete(
            system='Return a JSON object.', prompt='Build the declared object.',
            continuation_text='{"value": ',
        )
        self.assertEqual(result.text, '4}')
        self.assertEqual(result.usage['model_calls'], 1)
        self.assertEqual([message['role'] for message in self.request['messages']],
                         ['system', 'user', 'assistant', 'user'])
        self.assertEqual(self.request['messages'][1]['content'], 'Build the declared object.')
        self.assertEqual(self.request['messages'][2]['content'], '{"value": ')
        self.assertIn('missing suffix', self.request['messages'][3]['content'])
        self.assertNotIn('response_format', self.request)

    def test_compatible_reasoning_and_json_output_are_sent_exactly(self):
        self.response = {
            'choices': [{'message': {'content': '{"revised_text":"Association observed."}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 14, 'completion_tokens': 28, 'completion_tokens_details': {'reasoning_tokens': 12}},
        }
        result = self.client(
            'openai_compatible', reasoning_effort='high', output_format='json_object',
        ).complete(system='Return a JSON object.', prompt='Revise the claim.')
        self.assertEqual(self.path, '/v1/chat/completions')
        self.assertEqual(self.request['reasoning_effort'], 'high')
        self.assertEqual(self.request['response_format'], {'type': 'json_object'})
        self.assertEqual(self.request['max_tokens'], 4096)
        self.assertEqual(result.json_object(), {'revised_text': 'Association observed.'})
        self.assertEqual(result.usage, {'model_calls': 1, 'input_tokens': 14, 'output_tokens': 28})

    def test_role_fallback_dispatch_applies_json_mode_to_the_selected_route(self):
        calls = []

        class StructuredClient:
            def __init__(self, **config):
                calls.append(config)

            def complete(self, *, system, prompt, images=None):
                return ModelResult(
                    text='{"ok":true}', model="deepseek-cloud",
                    usage={"model_calls": 1}, elapsed_seconds=0.01,
                    finish_reason="stop",
                )

        result, routes = complete_with_role_fallbacks(
            {
                "protocol": "openai_compatible",
                "base_url": "http://structured-json.test/v1",
                "model": "deepseek-cloud", "timeout_seconds": 2,
                "max_output_tokens": 128,
            },
            role="strategy.argument", system="Return JSON.", prompt="Inspect.",
            output_format="json_object", client_factory=StructuredClient,
        )
        self.assertEqual(result.json_object(), {"ok": True})
        self.assertEqual(routes[0]["status_code"], 200)
        self.assertEqual(calls[0]["output_format"], "json_object")

    def test_continuation_uses_a_route_that_fits_the_accumulated_prefix(self):
        calls = []

        class ContinuationClient:
            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None, continuation_text=None):
                calls.append((self.config["model"], prompt, continuation_text))
                return ModelResult(
                    text='"complete":true}', model=self.config["model"],
                    usage={"model_calls": 1}, elapsed_seconds=0.01,
                    finish_reason="stop",
                )

        prefix = '{"complete":' + ("x" * 1800)
        routes = [
            {"protocol": "openai_compatible", "base_url": "https://models.test/v1",
             "model": "gemma-small", "max_output_tokens": 64,
             "context_window_tokens": 512, "max_input_tokens": 300},
            {"protocol": "openai_compatible", "base_url": "https://models.test/v1",
             "model": "deepseek-large", "max_output_tokens": 64,
             "context_window_tokens": 8192, "max_input_tokens": 7000},
        ]
        result, history = complete_with_role_fallbacks(
            {"model": "base"}, role="editorial.writer",
            system="Continue JSON.", prompt="Return a JSON object.",
            continuation_text=prefix, candidate_configs=routes,
            client_factory=ContinuationClient,
        )

        self.assertEqual([item[0] for item in calls], ["deepseek-large"])
        self.assertEqual(calls[0][1], "Return a JSON object.")
        self.assertEqual(calls[0][2], prefix)
        self.assertEqual(history[0]["model"], "deepseek-large")
        self.assertEqual(result.text, '"complete":true}')

    def test_prompt_cache_hint_and_usage_are_preserved(self):
        self.response = {
            'model': 'qwen3.8-27b',
            'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
            'usage': {
                'prompt_tokens': 100,
                'completion_tokens': 28,
                'prompt_tokens_details': {'cached_tokens': 80},
                'created_cache_tokens': 20,
            },
        }
        result = self.client('openai_compatible', cache_prompt=True).complete(
            system='Stable instruction.', prompt='Different assignment.')
        self.assertTrue(self.request['cache_prompt'])
        self.assertEqual(result.usage, {
            'model_calls': 1,
            'input_tokens': 100,
            'output_tokens': 28,
            'cache_read_tokens': 80,
            'cache_write_tokens': 20,
        })

    def test_sampling_controls_are_sent_to_compatible_provider(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        self.client(
            'openai_compatible', temperature=1.1, top_p=0.95, seed=17,
            presence_penalty=0.2, frequency_penalty=-0.1,
        ).complete(system='Explore.', prompt='Propose a direction.')
        self.assertEqual({key: self.request[key] for key in (
            'temperature', 'top_p', 'seed', 'presence_penalty', 'frequency_penalty',
        )}, {
            'temperature': 1.1, 'top_p': 0.95, 'seed': 17,
            'presence_penalty': 0.2, 'frequency_penalty': -0.1,
        })

    def test_sampling_controls_are_nested_in_ollama_options(self):
        self.client(temperature=0.25, top_p=0.9, seed=3,
                    presence_penalty=0.2, frequency_penalty=0.1).complete(
            system='Check.', prompt='Validate.')
        self.assertEqual(self.request['options']['temperature'], 0.25)
        self.assertEqual(self.request['options']['top_p'], 0.9)
        self.assertEqual(self.request['options']['seed'], 3)
        self.assertNotIn('presence_penalty', self.request['options'])
        self.assertNotIn('frequency_penalty', self.request['options'])

    def test_role_profile_resolution_keeps_metadata_out_of_provider_config(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'configured-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_profiles': {
                'topic_discovery': {'temperature': 1.3, 'top_p': 0.98},
            },
        }
        resolved = resolve_model_config(base, role='topic_discovery', overrides={'seed': 19})
        self.assertEqual(resolved['temperature'], 1.3)
        self.assertEqual(resolved['top_p'], 0.98)
        self.assertEqual(resolved['seed'], 19)
        self.assertNotIn('role_profiles', resolved)

    def test_role_model_resolution_routes_provider_and_inherits_base_config(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 4096,
            'role_models': {
                'research.literature-mapper': {
                    'model': 'bounded-model', 'max_output_tokens': 512,
                    'temperature': 0.2,
                },
            },
        }
        resolved = resolve_model_config(base, role='research.literature-mapper')
        self.assertEqual(resolved['model'], 'bounded-model')
        self.assertEqual(resolved['base_url'], self.url)
        self.assertEqual(resolved['protocol'], 'openai_compatible')
        self.assertEqual(resolved['max_output_tokens'], 512)
        self.assertEqual(resolved['temperature'], 0.2)
        self.assertNotIn('role_models', resolved)

    def test_context_policy_resolves_per_role_and_is_not_sent_to_provider(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'context_window_tokens': 8192, 'max_input_tokens': 4000,
            'role_models': {
                'short-context-role': {
                    'model': 'small-model', 'context_window_tokens': 4096,
                    'max_input_tokens': 3000,
                },
            },
        }
        resolved = resolve_model_config(base, role='short-context-role')
        self.assertEqual(resolved['model'], 'small-model')
        self.assertEqual(resolved['context_window_tokens'], 4096)
        self.assertEqual(resolved['max_input_tokens'], 3000)
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        self.client('openai_compatible', context_window_tokens=8192,
                    max_input_tokens=4000).complete(system='Return JSON.', prompt='Inspect.')
        self.assertNotIn('context_window_tokens', self.request)
        self.assertNotIn('max_input_tokens', self.request)

    def test_context_budget_is_conservative_and_blocks_before_network(self):
        self.assertGreater(estimate_input_tokens('x', 'y' * 3000), 1000)
        client = self.client('openai_compatible', context_window_tokens=8192,
                             max_input_tokens=1000)
        with self.assertRaisesRegex(ModelContextBudgetError, 'context budget exceeded') as caught:
            client.complete(system='x', prompt='y' * 3000)
        self.assertEqual(caught.exception.failure_class, 'context_budget')
        self.assertGreater(caught.exception.estimated_input_tokens,
                           caught.exception.allowed_input_tokens)
        self.assertFalse(hasattr(self, 'request'))
        self.assertIsNone(model_context_error(
            {'model': 'fits', 'max_output_tokens': 64,
             'context_window_tokens': 4096, 'max_input_tokens': 3000},
            system='x', prompt='short'))

    def test_context_budget_projection_reports_the_same_effective_limit(self):
        value = {'model': 'fits', 'max_output_tokens': 512,
                 'context_window_tokens': 4096, 'max_input_tokens': 3000}
        budget = model_context_budget(value, system='x', prompt='y' * 9000)
        self.assertEqual(budget['allowed_input_tokens'], 3000)
        self.assertFalse(budget['fits'])
        self.assertEqual(
            model_context_error(value, system='x', prompt='y' * 9000),
            'model context budget exceeded for fits: conservative input estimate '
            f"{budget['estimated_input_tokens']} tokens exceeds 3000 input tokens; "
            'context window 4096 with max output 512')

    def test_context_policy_must_leave_output_room(self):
        with self.assertRaisesRegex(ValidationError, 'leave room'):
            self.client('openai_compatible', context_window_tokens=4096)
        with self.assertRaisesRegex(ValidationError, 'exceeds context_window_tokens'):
            self.client('openai_compatible', context_window_tokens=8192,
                        max_input_tokens=5000)

    def test_role_route_metadata_is_validated_and_not_forwarded_to_provider(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_routes': {
                'research.literature-mapper': [
                    {'id': 'ollama-route', 'pool': 'ollama', 'base_url': self.url,
                     'protocol': 'openai_compatible', 'model': 'ollama-model', 'auth_env': None},
                    {'id': 'qwen-route', 'pool': 'qwen', 'base_url': self.url,
                     'protocol': 'openai_compatible', 'model': 'route-model', 'auth_env': None},
                ],
            },
        }
        resolved = resolve_model_config(base, role='research.literature-mapper')
        self.assertEqual(resolved['model'], 'strong-model')
        self.assertNotIn('role_routes', resolved)

    def test_invalid_role_route_is_rejected_before_network(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_routes': {'research.cataloger': [
                {'id': 'broken', 'pool': 'qwen', 'base_url': self.url,
                 'protocol': 'openai_compatible', 'model': 'runtime_required'},
            ]},
        }
        with self.assertRaisesRegex(ValidationError, 'requires an explicit value'):
            resolve_model_config(base)

    def test_invalid_role_model_config_fails_before_network(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_models': {'research.cataloger': {'unsupported': 'value'}},
        }
        with self.assertRaisesRegex(ValidationError, 'unsupported fields'):
            resolve_model_config(base, role='research.cataloger')

    def test_role_model_can_suppress_inherited_credentials(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'auth_env': 'OWNER_PRIVATE_KEY', 'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_models': {
                'low-risk-draft': {
                    'base_url': self.url, 'model': 'weak-model', 'auth_env': None,
                },
            },
        }
        resolved = resolve_model_config(base, role='low-risk-draft')
        self.assertEqual(resolved['model'], 'weak-model')
        self.assertIsNone(resolved['auth_env'])

    def test_invalid_sampling_controls_fail_before_network(self):
        for field, value in (
                ('temperature', -0.1), ('temperature', 2.1), ('top_p', 0),
                ('top_p', 1.1), ('seed', -1), ('presence_penalty', 2.1),
                ('frequency_penalty', -2.1)):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                self.client('openai_compatible', **{field: value})
        self.assertFalse(hasattr(self, 'request'))

    def test_invalid_prompt_cache_hint_fails_before_network(self):
        for value in ("true", 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.client('openai_compatible', cache_prompt=value)
        self.assertFalse(hasattr(self, 'request'))

    def test_explicit_reasoning_none_is_transmitted(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        self.client('openai_compatible', reasoning_effort='none').complete(system='Return JSON.', prompt='Inspect.')
        self.assertEqual(self.request['reasoning_effort'], 'none')
        self.assertNotIn('response_format', self.request)

    def test_compatible_multimodal_images_are_hash_pinned_and_embedded(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        png = b'\x89PNG\r\n\x1a\n' + b'fixture-image'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'figure.png'
            path.write_bytes(png)
            digest = hashlib.sha256(png).hexdigest()
            result = self.client('openai_compatible').complete(
                system='Return JSON.', prompt='Inspect the figure.', images=[{
                    'path': str(path.resolve()), 'media_type': 'image/png', 'sha256': digest,
                }])
        content = self.request['messages'][1]['content']
        self.assertEqual(content[0], {'type': 'text', 'text': 'Inspect the figure.'})
        self.assertEqual(content[1]['type'], 'image_url')
        url = content[1]['image_url']['url']
        self.assertTrue(url.startswith('data:image/png;base64,'))
        self.assertEqual(base64.b64decode(url.split(',', 1)[1]), png)
        self.assertEqual(result.json_object(), {'ok': True})

    def test_native_multimodal_images_are_hash_pinned_and_embedded(self):
        self.response = {'message': {'content': '{"ok":true}', 'thinking': 'Internal trace.'},
                         'done': True, 'done_reason': 'stop', 'eval_count': 40}
        png = b'\x89PNG\r\n\x1a\n' + b'fixture-image'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'figure.png'
            path.write_bytes(png)
            result = self.client('ollama', reasoning_effort='low').complete(
                system='Return JSON.', prompt='Inspect the figure.', images=[{
                    'path': str(path.resolve()), 'media_type': 'image/png',
                    'sha256': hashlib.sha256(png).hexdigest(),
                }], continuation_text='{"partial":')
        message = self.request['messages'][1]
        self.assertEqual(message['content'], 'Inspect the figure.')
        self.assertEqual(base64.b64decode(message['images'][0]), png)
        self.assertNotIn('data:', message['images'][0])
        self.assertEqual(self.request['messages'][2]['content'], '{"partial":')
        self.assertEqual(result.json_object(), {'ok': True})

    def test_multimodal_input_rejects_drift_and_mismatch_on_both_protocols(self):
        png = b'\x89PNG\r\n\x1a\n' + b'fixture-image'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'figure.png'
            path.write_bytes(png)
            valid = {'path': str(path.resolve()), 'media_type': 'image/png',
                     'sha256': hashlib.sha256(png).hexdigest()}
            for protocol in ('ollama', 'openai_compatible'):
                for invalid in ({**valid, 'sha256': '0' * 64},
                                {**valid, 'media_type': 'image/jpeg'},
                                {**valid, 'path': str(path.resolve()) + '.missing'}):
                    with self.subTest(protocol=protocol, invalid=invalid), self.assertRaises(ValidationError):
                        self.client(protocol).complete(system='x', prompt='x', images=[invalid])
        self.assertFalse(hasattr(self, 'request'))

    def test_multimodal_request_limits_are_enforced_before_network(self):
        png = b'\x89PNG\r\n\x1a\n' + b'x' * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'figure.png'
            path.write_bytes(png)
            image = {'path': str(path.resolve()), 'media_type': 'image/png',
                     'sha256': hashlib.sha256(png).hexdigest()}
            for protocol in ('ollama', 'openai_compatible'):
                for limits, images in (({'max_image_bytes': 32}, [image]),
                                       ({'max_image_bytes': 100000, 'max_request_bytes': 128}, [image]),
                                       ({}, [image] * 17)):
                    with self.subTest(protocol=protocol, limits=limits), self.assertRaises(ValidationError):
                        self.client(protocol, **limits).complete(system='x', prompt='x', images=images)
        self.assertFalse(hasattr(self, 'request'))

    def test_invalid_or_unsupported_generation_options_fail_before_network(self):
        for value in ('', 'ultra', True, ['high']):
            with self.subTest(reasoning_effort=value), self.assertRaises(ValidationError):
                self.client('openai_compatible', reasoning_effort=value)
        for value in ('', 'json_schema', True, {'type': 'json_object'}):
            with self.subTest(output_format=value), self.assertRaises(ValidationError):
                self.client('openai_compatible', output_format=value)
        for options in ({'reasoning_effort': 'xhigh'},):
            with self.subTest(ollama_options=options), self.assertRaises(ValidationError):
                self.client('ollama', **options)
        with self.assertRaises(ValidationError):
            self.client('ollama', output_format='json_schema')
        self.assertFalse(hasattr(self, 'request'))

    def test_native_reasoning_controls_use_think_and_keep_only_final_content(self):
        self.response = {'model': 'served-model', 'message': {
            'content': '{"ok":true}', 'thinking': 'Internal model trace.'},
            'done': True, 'done_reason': 'stop', 'prompt_eval_count': 20, 'eval_count': 40}
        for effort, wire_value in (('none', False), ('low', 'low'), ('medium', 'medium'), ('high', 'high')):
            with self.subTest(effort=effort):
                result = self.client('ollama', reasoning_effort=effort,
                                     output_format='json_object').complete(system='Return JSON.', prompt='Inspect.')
                self.assertEqual(self.request['think'], wire_value)
                self.assertNotIn('reasoning_effort', self.request)
                self.assertEqual(self.request['format'], 'json')
                self.assertEqual(result.json_object(), {'ok': True})
                self.assertEqual(result.usage['output_tokens'], 40)

    def test_json_output_mode_accepts_an_exact_json_markdown_fence(self):
        self.response = {'choices': [{'message': {'content': '```json\n{"ok":true}\n```'}, 'finish_reason': 'stop'}]}
        result = self.client('openai_compatible', output_format='json_object').complete(system='Return JSON.', prompt='Inspect.')
        self.assertEqual(result.json_object(), {'ok': True})

    def test_json_output_mode_still_rejects_prose_around_json(self):
        self.response = {'choices': [{'message': {'content': 'Here is the JSON:\n```json\n{"ok":true}\n```'}, 'finish_reason': 'stop'}]}
        result = self.client('openai_compatible', output_format='json_object').complete(system='Return JSON.', prompt='Inspect.')
        with self.assertRaises(ValidationError):
            result.json_object()

    def test_json_output_mode_accepts_explicit_reasoning_terminator_suffix(self):
        self.response = {'choices': [{'message': {
            'content': 'provider reasoning text</think>{"ok":true}',
        }, 'finish_reason': 'stop'}]}
        result = self.client('openai_compatible', output_format='json_object').complete(
            system='Return JSON.', prompt='Inspect.')
        self.assertEqual(result.json_object(), {'ok': True})

    def test_failure_does_not_expose_provider_body(self):
        self.status=401
        with self.assertRaises(ModelCallError) as error:
            self.client().complete(system='x',prompt='x')
        self.assertTrue(error.exception.outcome_known)
        self.assertNotIn('private',str(error.exception))
        self.status=503
        with self.assertRaises(ModelCallError) as error:
            self.client().complete(system='x',prompt='x')
        self.assertFalse(error.exception.outcome_known)

    def test_rate_limit_returns_to_scheduler_without_replaying_request(self):
        self.status = [429, 200]
        self.response = {'choices': [{'message': {'content': '{"value": 4}'}, 'finish_reason': 'stop'}]}
        with self.assertRaises(ModelCallError) as error:
            self.client('openai_compatible', max_retries=2, retry_backoff_seconds=0).complete(
                system='x', prompt='x')
        self.assertEqual(self.calls, 1)
        self.assertEqual(error.exception.status_code, 429)

    def test_rate_limit_exhaustion_reports_status_without_provider_body(self):
        self.status = 429
        with self.assertRaises(ModelCallError) as error:
            self.client('openai_compatible', max_retries=2, retry_backoff_seconds=0).complete(
                system='x', prompt='x')
        self.assertEqual(self.calls, 1)
        self.assertTrue(error.exception.outcome_known)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(error.exception.retry_after_seconds, 0.0)
        self.assertEqual(error.exception.attempts, 1)
        self.assertIsNotNone(error.exception.elapsed_seconds)
        self.assertNotIn('private', str(error.exception))

    def test_provider_429_classifies_quota_and_rate_limit_without_exposing_body(self):
        self.status = 429
        self.failure_body = json.dumps({"error": {
            "type": "insufficient_quota",
            "message": "private detail: weekly cloud usage limit reached",
        }}).encode()
        with self.assertRaises(ModelCallError) as caught:
            self.client("openai_compatible").complete(system="x", prompt="y")
        self.assertEqual(caught.exception.provider_error_kind, "quota_exhausted")
        self.assertNotIn("private detail", str(caught.exception))

        self.calls = 0
        self.failure_body = json.dumps({"error": {
            "type": "rate_limit_error", "message": "too many requests; retry shortly",
        }}).encode()
        with self.assertRaises(ModelCallError) as caught:
            self.client("openai_compatible").complete(system="x", prompt="y")
        self.assertEqual(caught.exception.provider_error_kind, "rate_limited")

    def test_malformed_provider_envelope_does_not_repeat_unknown_generation(self):
        self.response_sequence = [
            b'{"done":true}',
            {'model': 'served-model', 'message': {'content': '{"value": 4}'},
             'done': True, 'done_reason': 'stop'},
        ]
        with self.assertRaises(ModelCallError) as error:
            self.client(max_retries=1, retry_backoff_seconds=0).complete(system='x', prompt='x')
        self.assertEqual(self.calls, 1)
        self.assertFalse(error.exception.outcome_known)

    def test_shared_model_call_budget_counts_retries_and_blocks_before_network(self):
        self.status = [503, 200]
        self.response = {'choices': [{'message': {'content': '{"value": 4}'}, 'finish_reason': 'stop'}]}
        with tempfile.TemporaryDirectory() as directory:
            ledger = str((Path(directory) / 'model-budgets.sqlite').resolve())
            client = self.client(
                'openai_compatible', max_retries=1, retry_backoff_seconds=0,
                model_call_budget_path=ledger,
                model_call_budget_key='kimi-k3+glm-5.3',
                model_call_budget_limit=2,
            )
            result = client.complete(system='x', prompt='x')
            self.assertEqual(result.request_attempts, 2)
            self.assertEqual(self.calls, 2)
            connection = sqlite3.connect(ledger)
            try:
                self.assertEqual(
                    connection.execute(
                        'SELECT max_calls, used_calls FROM model_call_budgets WHERE budget_key=?',
                        ('kimi-k3+glm-5.3',),
                    ).fetchone(),
                    (2, 2),
                )
            finally:
                connection.close()
            with self.assertRaisesRegex(ModelCallError, 'budget exhausted'):
                client.complete(system='x', prompt='x')
            self.assertEqual(self.calls, 2)

    def test_exhausted_named_model_resolves_to_unbudgeted_fallback(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        with tempfile.TemporaryDirectory() as directory:
            ledger = str((Path(directory) / 'model-budgets.sqlite').resolve())
            client = self.client(
                'openai_compatible', max_retries=0,
                model_call_budget_path=ledger,
                model_call_budget_key='kimi-k3+glm-5.3',
                model_call_budget_limit=1,
            )
            client.complete(system='x', prompt='x')
            resolved = resolve_model_config({
                'base_url': self.url + '/v1', 'protocol': 'openai_compatible',
                'model': 'base-model', 'timeout_seconds': 2, 'max_output_tokens': 64,
                'role_models': {
                    'impact-review': {
                        'model': 'kimi-k3:cloud',
                        'model_call_budget_path': ledger,
                        'model_call_budget_key': 'kimi-k3+glm-5.3',
                        'model_call_budget_limit': 1,
                    },
                },
                'role_model_fallbacks': {
                    'impact-review': [{'model': 'deepseek-v4.1-flash:cloud'}],
                },
            }, role='impact-review')
            self.assertEqual(resolved['model'], 'deepseek-v4.1-flash:cloud')

    def test_incomplete_or_oversized_response_is_not_success(self):
        self.response['done']=False
        with self.assertRaises(ModelCallError):
            self.client().complete(system='x',prompt='x')
        self.response['done']=True
        with self.assertRaises(ModelCallError):
            self.client(max_response_bytes=10).complete(system='x',prompt='x')

    def test_body_transfer_is_bounded_by_absolute_timeout(self):
        self.slow_body = True
        client = ModelClient(base_url=self.url + '/v1', protocol='openai_compatible',
                             model='configured-model', timeout_seconds=0.08, max_output_tokens=5,
                             max_retries=0)
        started = time.monotonic()
        with self.assertRaises(ModelCallError) as error:
            client.complete(system='x', prompt='x')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('deadline', str(error.exception))

    def test_trickled_body_cannot_evade_absolute_timeout(self):
        self.trickle_body = True
        client = ModelClient(base_url=self.url + '/v1', protocol='openai_compatible',
                             model='configured-model', timeout_seconds=0.08, max_output_tokens=5,
                             max_retries=0)
        started = time.monotonic()
        with self.assertRaises(ModelCallError) as error:
            client.complete(system='x', prompt='x')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('deadline', str(error.exception))

    def test_trickled_chunk_header_cannot_evade_absolute_timeout(self):
        self.trickle_chunked = True
        client = ModelClient(base_url=self.url + '/v1', protocol='openai_compatible',
                             model='configured-model', timeout_seconds=0.08, max_output_tokens=5,
                             max_retries=0)
        started = time.monotonic()
        with self.assertRaises(ModelCallError) as error:
            client.complete(system='x', prompt='x')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('deadline', str(error.exception))

    def test_slow_headers_cannot_evade_absolute_timeout(self):
        self.slow_headers = True
        client = ModelClient(base_url=self.url + '/v1', protocol='openai_compatible',
                             model='configured-model', timeout_seconds=0.08, max_output_tokens=5,
                             max_retries=0)
        started = time.monotonic()
        with self.assertRaises(ModelCallError) as error:
            client.complete(system='x', prompt='x')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('deadline', str(error.exception))

    def test_secrets_cannot_be_embedded_in_url_and_absent_key_is_rejected(self):
        with self.assertRaises(ValidationError):
            ModelClient(base_url='https://user:secret@example.com',protocol='ollama',model='x',timeout_seconds=2,max_output_tokens=5)
        with patch.dict('os.environ',{},clear=True), self.assertRaises(ValidationError):
            self.client(auth_env='TEST_MODEL_KEY')

    def test_model_429_stops_route_fallback_and_opens_provider_cooldown(self):
        role = "editorial.writer"
        clear_model_provider_cooldown({
            "protocol": "openai_compatible", "base_url": "http://cloud.test/v1",
            "auth_env": None,
        })
        model = {
            "protocol": "openai_compatible", "base_url": "http://cloud.test/v1",
            "model": "deepseek-cloud", "auth_env": None,
            "context_window_tokens": 8192, "max_input_tokens": 7168,
            "max_output_tokens": 512, "timeout_seconds": 5,
            "provider_cooldown_fallback": {
                "id": "qwen-cooldown", "pool": "ollama",
                "protocol": "openai_compatible", "base_url": "http://local.test/v1",
                "model": "gemma-local", "auth_env": None,
                "context_window_tokens": 8192, "max_input_tokens": 7168,
                "max_output_tokens": 512, "timeout_seconds": 5,
                "provider_quota_scope": "ollama-local",
            },
        }
        normal = model_route_candidates(model, role=role)
        self.assertEqual([route["model"] for route in normal], ["deepseek-cloud"])
        self.assertEqual(
            [route["model"] for route in model_route_candidates(
                model, role=role, include_cooldown_fallback=True)],
            ["deepseek-cloud", "gemma-local"],
        )
        calls = []

        class StubClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                if self.route["model"] == "deepseek-cloud":
                    raise ModelCallError(
                        "known pre-generation rate limit", outcome_known=True,
                        status_code=429, attempts=1,
                    )
                return ModelResult(
                    text='{"decision":"pass","summary":"ok","findings":[],"evidence_gaps":[],"requested_actions":[]}',
                    model="gemma-local", usage={"model_calls": 1},
                    elapsed_seconds=0.01, finish_reason="stop",
                )

        with self.assertRaises(ModelCallError) as caught:
            complete_with_role_fallbacks(
                model, role=role, system="review", prompt="bounded prompt",
                candidate_configs=normal, output_token_cap=64,
                client_factory=StubClient,
            )
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(calls, ["deepseek-cloud"])
        self.assertGreater(model_provider_cooldown_remaining({
            "protocol": "openai_compatible", "base_url": "http://cloud.test/v1",
            "auth_env": None,
        }), 0)

        calls.clear()
        clear_model_provider_cooldown({
            "protocol": "openai_compatible", "base_url": "http://cloud.test/v1",
            "auth_env": None,
        })

        class TransientClient(StubClient):
            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                raise ModelCallError(
                    "temporary server error", outcome_known=True,
                    status_code=503, attempts=1,
                )

        with self.assertRaises(ModelCallError):
            complete_with_role_fallbacks(
                model, role=role, system="review", prompt="bounded prompt",
                candidate_configs=normal, client_factory=TransientClient,
            )
        self.assertEqual(calls, ["deepseek-cloud"])

    def test_owner_ollama_local_qwen_recovery_route_is_filtered(self):
        model = {
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "deepseek-v4.1-flash:cloud",
            "context_window_tokens": 262144,
            "max_input_tokens": 245760,
            "max_output_tokens": 8192,
            "timeout_seconds": 1800,
        }
        environment = {
            "SCISAURUS_OLLAMA_ONLY": "1",
            "SCISAURUS_OLLAMA_BASE_URL": "http://127.0.0.1:11434/v1",
            "SCISAURUS_OLLAMA_COOLDOWN_FALLBACK_MODEL": "qwen3.8:27b-mlx",
        }
        configured = with_runtime_cooldown_fallback(model, env=environment)
        self.assertNotIn("provider_cooldown_fallback", configured)

        remote = with_runtime_cooldown_fallback(
            {**model, "base_url": "https://remote.test/v1"}, env=environment)
        self.assertNotIn("provider_cooldown_fallback", remote)
        with self.assertRaisesRegex(ValidationError, "requires SCISAURUS_OLLAMA_ONLY"):
            with_runtime_cooldown_fallback(
                model, env={**environment, "SCISAURUS_OLLAMA_ONLY": "0"})
