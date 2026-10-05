"""Local Graph API transport, provenance and pacing contracts."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime import semantic_scholar as s2
from scisaurus.runtime.operation_adapters import get_adapter
from scisaurus.runtime.run_control import RunPausedError

PAPER = {"paperId": "a" * 40, "title": "Example paper", "abstract": "Captured abstract.",
         "externalIds": {"DOI": "10.1234/EXAMPLE"}, "year": 2024,
         "openAccessPdf": {"url": "https://example.org/paper.pdf", "status": "GREEN"}}


class GraphFixture:
    def __enter__(self):
        self.requests = []
        self.status = 200
        self.payload = {"data": [PAPER], "total": 1, "token": "next-page"}
        self.headers = {}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                owner.requests.append({"method": self.command, "path": self.path,
                                       "key": self.headers.get("x-api-key"), "body": None})
                self.reply()
            def do_POST(self):
                owner.requests.append({"method": self.command, "path": self.path,
                                       "key": self.headers.get("x-api-key"),
                                       "body": json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
                self.reply()
            def reply(self):
                raw = json.dumps(owner.payload).encode()
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in owner.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.directory = tempfile.TemporaryDirectory()
        self.client = s2.SemanticScholarClient(endpoint=f"http://127.0.0.1:{self.server.server_port}/graph/v1",
                                              rate_state_path=str(Path(self.directory.name) / "rate.json"))
        self.environment = patch.dict(os.environ, {s2.AUTH_ENV: "fixture-secret"})
        self.environment.start()
        return self
    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.environment.stop()
        self.directory.cleanup()
    def profile(self):
        return {"client": {"endpoint": self.client.endpoint, "max_bytes": self.client.max_bytes}}


class SemanticScholarTests(unittest.TestCase):
    def test_bulk_search_preserves_abstract_provenance_and_pagination(self):
        with GraphFixture() as fixture:
            args = {"operation": "search", "query": "isotope calibration", "token": "cursor"}
            result = fixture.client.run(**args)
            self.assertIn("/paper/search/bulk?", fixture.requests[0]["path"])
            self.assertEqual(fixture.requests[0]["key"], "fixture-secret")
            self.assertNotIn("fixture-secret", json.dumps(result))
            self.assertEqual(result["pagination"], {"token": "next-page", "total": 1})
            self.assertEqual(result["works"][0]["identity_key"], "doi:10.1234/example")
            self.assertFalse(result["works"][0]["full_text_acquired"])
            checks, _ = s2.inspect_result(fixture.profile(), result, args)
            self.assertTrue(all(check["outcome"] == "passed" for check in checks))
            altered = deepcopy(result)
            altered["works"][0]["title"] = "Invented paper"
            checks, _ = s2.inspect_result(fixture.profile(), altered, args)
            self.assertTrue(any(check["outcome"] == "failed" for check in checks))

    def test_batch_is_one_request_with_positioned_missing_ids(self):
        with GraphFixture() as fixture:
            fixture.payload = [PAPER, None]
            args = {"operation": "batch", "paper_ids": ["DOI:10.1234/EXAMPLE", "CorpusId:123"]}
            result = fixture.client.run(**args)
            self.assertEqual(len(fixture.requests), 1)
            self.assertEqual(fixture.requests[0]["method"], "POST")
            self.assertEqual(fixture.requests[0]["body"], {"ids": ["DOI:10.1234/example", "CorpusId:123"]})
            self.assertEqual(result["missing_ids"], ["CorpusId:123"])

    def test_citations_and_references_are_explicit_edges(self):
        for operation, edge in (("citations", "citingPaper"), ("references", "citedPaper")):
            with self.subTest(operation=operation), GraphFixture() as fixture:
                fixture.payload = {"data": [{edge: PAPER}], "offset": 0, "next": 10}
                result = fixture.client.run(operation=operation, paper_id="a" * 40, limit=10)
                self.assertEqual(result["works"][0]["paper_id"], PAPER["paperId"])
                self.assertEqual(result["pagination"], {"offset": 0, "next": 10})

    def test_rate_limit_capture_and_account_cooldown_prevent_next_request(self):
        with GraphFixture() as fixture:
            fixture.status, fixture.payload = 429, {"message": "Too many requests"}
            fixture.headers = {"Retry-After": "120"}
            result = fixture.client.run(operation="search", query="calibration")
            self.assertEqual(result["outcome"], "rate_limited")
            self.assertGreater(result["metadata"]["retry_after_seconds"], 119)
            second = s2.SemanticScholarClient(endpoint=fixture.client.endpoint, rate_state_path=str(fixture.client.rate_state_path))
            with self.assertRaises(s2.ProviderCooldownError):
                second.run(operation="batch", paper_ids=["a" * 40])
            self.assertEqual(len(fixture.requests), 1)
            self.assertNotIn("fixture-secret", fixture.client.rate_state_path.read_text())
            checks, _ = s2.inspect_result(fixture.profile(), result, {"operation": "search", "query": "calibration"}, representative=False)
            self.assertTrue(all(check["outcome"] == "passed" for check in checks))

    def test_empty_result_does_not_pass_readiness(self):
        with GraphFixture() as fixture:
            fixture.payload = {"data": [], "total": 0}
            args = {"operation": "search", "query": "empty"}
            result = fixture.client.run(**args)
            self.assertEqual(result["outcome"], "empty")
            self.assertEqual(result["sources"], [])
            checks, _ = s2.inspect_result(fixture.profile(), result, args)
            self.assertTrue(any(row["outcome"] == "failed" for row in checks))
            checks, _ = s2.inspect_result(fixture.profile(), result, args, representative=False)
            self.assertTrue(all(row["outcome"] == "passed" for row in checks))

    def test_authentication_failure_never_falls_back_to_anonymous(self):
        with GraphFixture() as fixture:
            fixture.status = 401
            fixture.payload = {"message": "unauthorized"}
            result = fixture.client.run(operation="paper", paper_id="a" * 40)
            self.assertEqual(result["outcome"], "auth_required")
            self.assertEqual(len(fixture.requests), 1)
            self.assertEqual(result["sources"], [])

    def test_echoed_secret_is_redacted_and_not_admitted(self):
        with GraphFixture() as fixture:
            fixture.payload = {"message": "fixture-secret"}
            result = fixture.client.run(operation="search", query="test")
            self.assertNotIn("fixture-secret", json.dumps(result))
            self.assertEqual(result["outcome"], "provider_error")
            self.assertTrue(result["metadata"]["capture_redacted"])

    def test_pause_precedes_transport(self):
        with GraphFixture() as fixture, patch("scisaurus.runtime.semantic_scholar.ensure_run_allowed", side_effect=RunPausedError("paused")):
            with self.assertRaises(RunPausedError):
                fixture.client.run(operation="search", query="calibration")
            self.assertEqual(fixture.requests, [])

    def test_credentials_and_transport_options_are_validated_without_network(self):
        with GraphFixture() as fixture, patch.dict(os.environ, {s2.AUTH_ENV: ""}):
            with self.assertRaises(ValidationError):
                fixture.client.run(operation="search", query="calibration")
            self.assertEqual(fixture.requests, [])
        for kwargs in ({"endpoint": "https://example.org/graph/v1"}, {"min_interval_seconds": .1},
                       {"rate_state_path": "relative"}, {"auth_env": "literal-key-value!"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                s2.SemanticScholarClient(**kwargs)
        profile = get_adapter("semantic_scholar").validate_client({"timeout": 30, "max_bytes": 10000000}, "/tmp", [])
        self.assertEqual(profile["auth_env"], s2.AUTH_ENV)
        self.assertEqual(get_adapter("semantic_scholar").dispatch_kind, "semantic_scholar")

    def test_malformed_and_ambiguous_payloads_are_rejected(self):
        for payload in (b'{"data":[],"data":[]}', b'{"total": NaN}', b'{"total":1e999}'):
            with self.assertRaises(ValueError):
                s2.decode_response(payload)
        with self.assertRaises(ValueError):
            s2.validate_arguments({"operation": "batch", "paper_ids": ["DOI:10.1234/A", "DOI:10.1234/a"]})
        with self.assertRaises(ValueError):
            s2.project_response([PAPER], {"operation": "batch", "paper_ids": ["a"*40, "b"*40]})

    def test_worker_dispatch_uses_registered_client(self):
        from scisaurus.runtime.execution import _invoke_worker
        channel = unittest.mock.Mock()
        with patch.object(s2.SemanticScholarClient, "run", return_value={"outcome": "ok"}) as run:
            _invoke_worker("semantic_scholar", {"client": {}, "operation": "search", "query": "test"}, channel)
        run.assert_called_once_with(operation="search", query="test")
        channel.put.assert_called_once_with({"ok": True, "result": {"outcome": "ok"}})
