"""Real HTTP fixtures for bounded scholarly metadata and independent inspection."""
import base64
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager
from scisaurus.runtime.literature import DEFAULT_ENDPOINT, MAX_REQUEST_URL_BYTES, OpenAlexClient, request_url
from scisaurus.runtime.operation_adapters import get_adapter
from scisaurus.runtime.operations import OperationsCell


def work_fixture(identity="W123"):
    return {"id": "https://openalex.org/" + identity, "doi": "https://doi.org/10.1234/EXAMPLE",
            "title": "Measured association", "publication_year": 2025,
            "abstract_inverted_index": {"Observed": [0], "association": [1, 3], "is": [2]},
            "referenced_works": ["https://openalex.org/W456"],
            "related_works": ["https://openalex.org/W789"],
            "locations": [{"landing_page_url": "https://example.org/paper", "pdf_url": "https://example.org/paper.pdf",
                           "is_oa": True, "version": "acceptedVersion"}],
            "has_fulltext": True, "has_content": {"pdf": True},
            "content_urls": {"pdf": "https://content.openalex.org/works/W123.pdf"}}


class RecordedOpenAlexExecutor:
    """Record local HTTP executions in the real task and artifact stores."""
    def __init__(self, control, store):
        self.tasks, self.store, self.calls = TaskManager(control), store, []

    def __call__(self, task_id, kind, params, *, actor, task_kind):
        if kind != "openalex":
            raise ValueError("Unsupported fixture dispatch")
        self.calls.append((task_id, kind, params, actor, task_kind))
        self.tasks.create(task_id, task_kind, {"operation": kind}, actor)
        self.tasks.admit(task_id, "command.controller")
        attempt = task_id + "-attempt"
        self.tasks.start_attempt(task_id, attempt, owner=actor, lease_ttl_seconds=60)
        context = self.store.publish_artifact(logical_id=f"command/contexts/{task_id}", artifact_type="note",
                    author=actor, body=canonical_bytes(params), media_type="application/json")
        result = OpenAlexClient(**params["client"]).run(**{key: value for key, value in params.items() if key != "client"})
        report = self.store.publish_artifact(logical_id=f"command/executions/{task_id}", artifact_type="report", author=actor,
                    body=canonical_bytes(result), media_type="application/json", inputs=[{"ref": context["artifact_ref"], "purpose": "subject"}])
        self.tasks.finish_attempt(attempt, "succeeded", usage={"retrieval_calls": 1})
        self.tasks.transition(task_id, "awaiting_review", actor)
        return result, report["artifact_ref"]


class OpenAlexFixture(BaseHTTPRequestHandler):
    requests = []
    bodies = []
    rate_limit_count = 0
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        self.requests.append({"path": parsed.path, "query": query, "authorization": self.headers.get("Authorization")})
        mode = query.get("search", ["ok"])[0]
        if mode == "rate-limit-once" and self.rate_limit_count == 0:
            type(self).rate_limit_count += 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if mode == "interrupted":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"3\r\nabc\r\n5\r\nxy")
            self.wfile.flush()
            self.close_connection = True
            return
        if mode == "headers-trickle":
            try:
                for byte in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n":
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        work = work_fixture(parsed.path.rsplit("/", 1)[1] if parsed.path != "/works" else "W123")
        cursor = query.get("cursor", ["*"])[0]
        items = [] if mode == "empty" or cursor == "last-page" else [work]
        if mode == "no-abstract":
            del work["abstract_inverted_index"]
            work["doi"] = None
            work["publication_year"] = None
        elif mode == "bad-abstract-gap":
            work["abstract_inverted_index"] = {"missing": [1]}
        elif mode == "bad-abstract-collision":
            work["abstract_inverted_index"] = {"one": [0], "two": [0]}
        elif mode == "bad-abstract-bool":
            work["abstract_inverted_index"] = {"one": [False]}
        elif mode == "bad-abstract-empty":
            work["abstract_inverted_index"] = {}
        elif mode == "bad-work-id":
            work["id"] = "https://different.example/W123"
        elif mode == "bad-year":
            work["publication_year"] = True
        elif mode == "bad-location":
            work["locations"][0]["is_oa"] = 1
        elif mode == "bad-reference":
            work["referenced_works"] = [12]
        elif mode == "missing-references":
            del work["referenced_works"]
        elif mode == "duplicate-works":
            items.append(deepcopy(work))
        if query.get("filter") == ["cites:W999"]:
            work["referenced_works"] = []
        payload = {"meta": {"count": 0 if mode == "empty" else 3, "per_page": int(query.get("per_page", [5])[0]),
                            "next_cursor": "next-page" if items else None}, "results": items}
        if mode == "bad-count":
            payload["meta"]["count"] = True
        elif mode == "bad-cursor":
            payload["meta"]["next_cursor"] = 123
        elif mode == "repeated-cursor":
            payload["meta"]["next_cursor"] = cursor
        elif mode == "bad-limit":
            payload["meta"]["per_page"] += 1
        elif mode == "missing-results":
            del payload["results"]
        elif mode == "null-results":
            payload["results"] = None
        elif mode == "conflicting-empty":
            payload["results"] = []
            payload["meta"]["next_cursor"] = None
        elif parsed.path != "/works":
            payload = work
        status, body = 200, json.dumps(payload, indent=2).encode()
        if mode == "rate-limited":
            status, body = 429, b'{"error": "slow down"}'
        elif mode == "redirect":
            status, body = 302, b""
        elif mode == "malformed":
            body = b"upstream HTML"
        elif mode == "duplicate-keys":
            body = b'{"meta":{},"results":[],"results":[]}'
        elif mode == "nonfinite":
            body = b'{"meta":{},"results":[],"cost": NaN}'
        elif mode == "overflow":
            body = b'{"meta":{"count":0,"per_page":2,"next_cursor":null,"cost_usd":1e999},"results":[]}'
        elif mode == "surrogate":
            payload["results"][0]["title"] = "\ud800"
            body = json.dumps(payload).encode()
        elif mode == "huge":
            body = b"x" * 8192
        elif mode == "slow":
            time.sleep(0.25)
        self.bodies.append(body)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body) + (10 if mode == "short-body" else 0)))
            self.send_header("X-RateLimit-Remaining", "42")
            self.send_header("Retry-After", "7")
            if mode == "redirect":
                self.send_header("Location", "/works?search=redirect-target")
            if mode in {"short-body", "body-trickle"}:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if mode == "body-trickle":
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(0.02)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class TestOpenAlex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAlexFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/works"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def client(self, **kwargs):
        return OpenAlexClient(endpoint=self.endpoint, **kwargs)

    def arguments(self, query="A & B", **kwargs):
        return {"operation": "search", "query": query, "work_id": None, "limit": 2, "cursor": None, **kwargs}

    def inspect(self, result, arguments=None, representative=True):
        adapter = get_adapter("openalex")
        profile = {"adapter": "openalex", "environment_files": [], "client": {
            "endpoint": self.endpoint, "timeout": 2, "max_bytes": 1_048_576}}
        return adapter.inspect_result(profile, result, arguments or self.arguments(), representative=representative)[0]

    def test_exact_raw_capture_normalization_and_independent_inspection(self):
        arguments = self.arguments()
        result = self.client().run(**arguments)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["metadata"]["request"], arguments)
        self.assertEqual(OpenAlexFixture.requests[-1]["query"], {"search": ["A & B"], "per_page": ["2"], "cursor": ["*"]})
        raw = base64.b64decode(result["capture"]["body"])
        self.assertEqual(raw, OpenAlexFixture.bodies[-1])
        self.assertEqual(result["raw_response"], json.loads(raw))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result["capture_sha256"])
        self.assertEqual(result["works"][0], {
            "id": "W123", "doi": "10.1234/example", "title": "Measured association", "year": 2025,
            "abstract": "Observed association is association", "referenced_works": ["W456"], "related_works": ["W789"],
            "locations": [{"landing_page_url": "https://example.org/paper", "pdf_url": "https://example.org/paper.pdf",
                           "is_oa": True, "version": "acceptedVersion"}]})
        self.assertTrue(all(check["outcome"] == "passed" for check in self.inspect(result)))

    def test_transient_rate_limit_is_retried_inside_total_timeout(self):
        result = self.client(max_retries=1, retry_backoff_seconds=0).run(
            **self.arguments("rate-limit-once"))
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["metadata"]["attempts"], 2)
        self.assertEqual(result["metadata"]["retry_wait_seconds"], 0.0)
        self.assertEqual(OpenAlexFixture.rate_limit_count, 1)
        self.assertTrue(all(check["outcome"] == "passed" for check in self.inspect(result,
                                                                                       self.arguments("rate-limit-once"))))

    def test_operations_readiness_and_routine_workloads_use_full_execution_context(self):
        with tempfile.TemporaryDirectory(prefix="scisaurus-openalex-operations-") as directory:
            control = ControlStore(directory)
            try:
                store = ArtifactStore(control)
                store.init_project()
                cell = OperationsCell(control, store, project_id="openalex-integration")
                executor = RecordedOpenAlexExecutor(control, store)
                client = {"endpoint": self.endpoint, "timeout": 2, "max_bytes": 1_048_576}
                cell.register("papers", adapter="openalex", client=client, representative=self.arguments(),
                              engineer="operations.engineer", environment_files=[])
                ready = cell.ensure_ready("papers", executor, operator="operations.operator", verifier="operations.verifier")
                self.assertEqual(ready["state"], "ready")
                self.assertIsNotNone(ready["binding"])
                self.assertEqual(executor.calls[0][2]["client"], client)
                for arguments, outcome in ((self.arguments(None, operation="work", work_id="W456"), "ok"),
                                           (self.arguments("empty"), "empty")):
                    result, ref = cell.run(ready["binding"], arguments, executor, operator="research.searcher")
                    self.assertEqual(result["outcome"], outcome)
                    self.assertEqual(store.get(ref)["artifact_type"], "report")
                    self.assertEqual(cell.status("papers")["binding"], ready["binding"])
                self.assertEqual(len(executor.calls), 3)
                self.assertTrue(control.verify_chain()[0])
            finally:
                control.close()

    def test_inspector_accepts_bound_client_context_and_rejects_client_drift(self):
        result = self.client().run(**self.arguments())
        client = {"endpoint": self.endpoint, "timeout": 2, "max_bytes": 1_048_576}
        params = {"client": client, **self.arguments()}
        self.assertTrue(all(check["outcome"] == "passed" for check in self.inspect(result, params)))
        for field, value in (("endpoint", "https://api.openalex.org/works"), ("max_bytes", 100), ("timeout", True)):
            altered = deepcopy(params)
            altered["client"][field] = value
            checks = self.inspect(result, altered)
            self.assertEqual(next(check for check in checks if check["check_id"] == "openalex-request")["outcome"], "failed")

    def test_work_and_citing_are_distinct_requests_with_bound_id(self):
        arguments = self.arguments(None, operation="work", work_id="https://openalex.org/W456")
        result = self.client().run(**arguments)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["metadata"]["request"]["work_id"], "W456")
        self.assertEqual(result["metadata"]["count"], 1)
        self.assertFalse(result["metadata"]["has_more"])
        self.assertEqual(OpenAlexFixture.requests[-1]["path"], "/works/W456")
        self.assertEqual(OpenAlexFixture.requests[-1]["query"], {})
        self.assertTrue(all(check["outcome"] == "passed" for check in self.inspect(result, arguments)))
        arguments = self.arguments(None, operation="citing", work_id="W456", cursor="some-cursor")
        result = self.client().run(**arguments)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(OpenAlexFixture.requests[-1]["query"]["filter"], ["cites:W456"])
        self.assertEqual(OpenAlexFixture.requests[-1]["query"]["cursor"], ["some-cursor"])
        self.assertTrue(all(check["outcome"] == "passed" for check in self.inspect(result, arguments)))
        self.assertEqual(self.client().run(operation="citing", work_id="W999")["outcome"], "parse_error")

    def test_cursor_completion_and_routine_empty_are_not_readiness(self):
        result = self.client().run(**self.arguments())
        self.assertTrue(result["metadata"]["has_more"])
        self.assertEqual(result["metadata"]["next_cursor"], "next-page")
        for arguments in (self.arguments("empty"), self.arguments(cursor="last-page")):
            result = self.client().run(**arguments)
            self.assertEqual(result["outcome"], "empty")
            self.assertFalse(result["metadata"]["has_more"])
            self.assertEqual(result["text"], "")
            self.assertEqual(result["works"], [])
            self.assertTrue(all(check["outcome"] == "passed" for check in self.inspect(result, arguments, representative=False)))
            self.assertTrue(any(check["outcome"] == "failed" for check in self.inspect(result, arguments)))

    def test_location_and_abstract_metadata_do_not_claim_full_text(self):
        result = self.client().run(**self.arguments("no-abstract"))
        self.assertEqual(result["outcome"], "ok")
        self.assertIsNone(result["works"][0]["abstract"])
        self.assertIsNone(result["works"][0]["doi"])
        self.assertIsNone(result["works"][0]["year"])
        self.assertEqual(result["metadata"]["representation"], "scholarly_metadata")
        self.assertEqual(result["sources"][0]["representation"], "scholarly_metadata")
        self.assertNotIn("has_fulltext", result["works"][0])
        self.assertNotIn("Observed association", result["text"])
        self.assertTrue(all(check["outcome"] == "passed" for check in self.inspect(result, self.arguments("no-abstract"))))

    def test_bad_shapes_never_become_empty_success_or_partial_normalization(self):
        for mode in ("bad-work-id", "bad-year", "bad-location", "bad-reference", "missing-references",
                     "duplicate-works", "bad-count", "bad-cursor", "repeated-cursor", "bad-limit", "missing-results",
                     "null-results", "conflicting-empty", "malformed", "duplicate-keys", "nonfinite", "overflow", "surrogate"):
            with self.subTest(mode=mode):
                result = self.client().run(**self.arguments(mode))
                self.assertEqual(result["outcome"], "parse_error")
                self.assertEqual(result["works"], [])
                self.assertEqual(result["sources"], [])
                self.assertEqual(result["text"], "")
                self.assertIsNotNone(result["capture_sha256"])

    def test_malformed_provider_abstract_is_omitted_without_discarding_the_work(self):
        for mode in ("bad-abstract-gap", "bad-abstract-collision", "bad-abstract-bool", "bad-abstract-empty"):
            with self.subTest(mode=mode):
                result = self.client().run(**self.arguments(mode))
                self.assertEqual(result["outcome"], "ok")
                self.assertEqual(len(result["works"]), 1)
                self.assertIsNone(result["works"][0]["abstract"])
                self.assertEqual(result["metadata"]["abstract_gaps"],
                                 [{"work_id": "W123", "reason": "provider_abstract_index_invalid"}])
                self.assertTrue(all(check["outcome"] == "passed" for check in self.inspect(result, self.arguments(mode))))

    def test_retry_policy_can_disable_retries_and_redirects_remain_terminal(self):
        for mode, outcome in (("rate-limited", "rate_limited"), ("redirect", "provider_error")):
            before = len(OpenAlexFixture.requests)
            result = self.client(max_retries=0).run(operation="search", query=mode)
            self.assertEqual(result["outcome"], outcome)
            self.assertEqual(len(OpenAlexFixture.requests), before + 1)
            self.assertEqual(result["works"], [])
            self.assertEqual(result["metadata"]["headers"]["retry-after"], "7")

    def test_bytes_deadlines_and_interrupted_responses_are_bounded(self):
        result = self.client(max_bytes=100).run(operation="search", query="huge")
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["capture"]["bytes"], 100)
        self.assertTrue(result["metadata"]["capture_truncated"])
        self.assertEqual(result["works"], [])
        for mode in ("slow", "headers-trickle", "body-trickle"):
            with self.subTest(mode=mode):
                start = time.monotonic()
                result = self.client(timeout=0.08).run(operation="search", query=mode)
                self.assertEqual(result["outcome"], "timeout")
                self.assertLess(time.monotonic() - start, 0.4)
                self.assertTrue(result["metadata"]["capture_incomplete"])
        for mode in ("interrupted", "short-body"):
            result = self.client().run(operation="search", query=mode)
            self.assertEqual(result["outcome"], "provider_error")
            self.assertTrue(result["metadata"]["capture_incomplete"])
            self.assertEqual(result["works"], [])
            self.assertIsNotNone(result["capture_sha256"])

    def test_auth_env_uses_header_without_credentials_in_records(self):
        with patch.dict(os.environ, {"SCISAURUS_OPENALEX_TEST": "synthetic-credential"}):
            result = self.client(auth_env="SCISAURUS_OPENALEX_TEST").run(**self.arguments())
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(OpenAlexFixture.requests[-1]["authorization"], "Bearer synthetic-credential")
        self.assertNotIn("synthetic-credential", json.dumps(result))
        self.assertNotIn("api_key", OpenAlexFixture.requests[-1]["query"])
        before = len(OpenAlexFixture.requests)
        with patch.dict(os.environ, {}, clear=True):
            result = self.client(auth_env="SCISAURUS_OPENALEX_TEST").run(**self.arguments())
        self.assertEqual(result["outcome"], "auth_required")
        self.assertEqual(len(OpenAlexFixture.requests), before)

    def test_dns_deadline_returns_without_late_http_request(self):
        before = len(OpenAlexFixture.requests)
        resolver = socket.getaddrinfo
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def slow_resolve(*args, **kwargs):
            entered.set()
            release.wait(timeout=1)
            try:
                return resolver(*args, **kwargs)
            finally:
                finished.set()

        with patch("scisaurus.runtime.literature.socket.getaddrinfo", side_effect=slow_resolve):
            start = time.monotonic()
            result = self.client(timeout=0.03).run(**self.arguments())
            elapsed = time.monotonic() - start
        self.assertTrue(entered.is_set())
        self.assertEqual(result["outcome"], "timeout")
        self.assertLess(elapsed, 0.2)
        release.set()
        self.assertTrue(finished.wait(timeout=1))
        self.assertEqual(len(OpenAlexFixture.requests), before)

    def test_independent_inspector_rejects_tampered_works_raw_types_and_context(self):
        original = self.client().run(**self.arguments())
        def replace_capture(result, payload):
            raw = json.dumps(payload).encode()
            result["capture"].update(body=base64.b64encode(raw).decode(), bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            result["capture_sha256"] = result["capture"]["sha256"]
            result["raw_response"] = payload
        mutations = [
            lambda r: r["works"][0].update(abstract="Fabricated abstract"),
            lambda r: r["works"][0].update(referenced_works=["W999"]),
            lambda r: r["works"][0]["locations"][0].update(is_oa=1),
            lambda r: r["sources"][0].update(representation="extracted_text"),
            lambda r: r["sources"][0].update(title="Changed title"),
            lambda r: r.update(text="Changed text"),
            lambda r: r["raw_response"]["results"][0].update(title="Changed raw title"),
            lambda r: r["metadata"]["request"].update(query="different query"),
            lambda r: r["metadata"].update(count=True),
            lambda r: r["metadata"].update(has_more=1),
            lambda r: r["metadata"].update(http_status=True),
            lambda r: r["metadata"].update(next_cursor="invented"),
            lambda r: r["metadata"].update(capture_truncated=0),
            lambda r: r["capture"].update(bytes=True),
            lambda r: r["metadata"].update(final_url=self.endpoint + "?search=different"),
        ]
        for mutate in mutations:
            result = deepcopy(original)
            mutate(result)
            self.assertTrue(any(check["outcome"] == "failed" for check in self.inspect(result)))
        result = deepcopy(original)
        payload = deepcopy(result["raw_response"])
        payload["results"][0]["publication_year"] = True
        replace_capture(result, payload)
        result["works"][0]["year"] = True
        result["sources"][0]["year"] = True
        self.assertTrue(any(check["outcome"] == "failed" for check in self.inspect(result)))

    def test_input_and_profile_validation_precede_network(self):
        adapter = get_adapter("openalex")
        self.assertEqual((adapter.dispatch_kind, adapter.task_kind, adapter.usage_dimension),
                         ("openalex", "retrieval", "retrieval_calls"))
        before = len(OpenAlexFixture.requests)
        for changes in ({"operation": "other"}, {"limit": True}, {"limit": 101}, {"limit": 0},
                        {"work_id": "W123"}, {"query": ""}, {"cursor": ""}, {"cursor": False},
                        {"operation": "work", "query": None, "work_id": "https://evil.example/W123"},
                        {"operation": "work", "query": None, "work_id": "W123", "cursor": "page"}):
            arguments = {**self.arguments(), **changes}
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.client().run(**arguments)
                with self.assertRaises(ValidationError):
                    adapter.validate_arguments(arguments)
        with self.assertRaises(ValidationError):
            adapter.validate_arguments({"query": "x", "limit": 5})
        for client in ({"timeout": 1}, {"timeout": 1, "max_bytes": 1, "api_key": "not-allowed"},
                       {"timeout": 1, "max_bytes": 1, "endpoint": self.endpoint + "?api_key=x"},
                       {"timeout": 1, "max_bytes": 1, "endpoint": "https://api.openalex.org:bad/works"},
                       {"timeout": 1, "max_bytes": 1, "auth_env": "contains spaces"}):
            with self.assertRaises(ValidationError):
                adapter.validate_client(client, "/tmp", [])
        self.assertEqual(len(OpenAlexFixture.requests), before)

    def test_stemmed_search_rejects_wildcards_without_changing_the_query(self):
        adapter = get_adapter("openalex")
        before = len(OpenAlexFixture.requests)
        for query in ("transformer* AND limitation", "generaliz?tion", '"attention*"'):
            arguments = self.arguments(query)
            with self.subTest(query=query):
                with self.assertRaisesRegex(ValueError, "stemmed search requires a query without"):
                    self.client().run(**arguments)
                with self.assertRaisesRegex(ValidationError, "stemmed search requires a query without"):
                    adapter.validate_arguments(arguments)
                self.assertEqual(arguments["query"], query)
        self.assertEqual(len(OpenAlexFixture.requests), before)
        query = 'transformer AND (limitation OR "negative result")'
        result = self.client().run(**self.arguments(query))
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(OpenAlexFixture.requests[-1]["query"]["search"], [query])
        self.assertNotIn("search.exact", OpenAlexFixture.requests[-1]["query"])

    def test_percent_encoded_url_limit_precedes_http_for_queries_cursors_and_endpoint(self):
        adapter = get_adapter("openalex")
        before = len(OpenAlexFixture.requests)
        for arguments in (self.arguments("한" * 450), self.arguments(cursor="한" * 450),
                          self.arguments(None, operation="work", work_id="W" + "1" * 4100)):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, "4094-byte limit"):
                    self.client().run(**arguments)
                with self.assertRaisesRegex(ValidationError, "4094-byte limit"):
                    adapter.validate_arguments(arguments)
        arguments = self.arguments("x")
        baseline = len(request_url(DEFAULT_ENDPOINT, arguments).encode("utf-8"))
        arguments["cursor"] = "a" * (MAX_REQUEST_URL_BYTES - baseline + 3)
        self.assertEqual(len(request_url(DEFAULT_ENDPOINT, arguments).encode("utf-8")), MAX_REQUEST_URL_BYTES)
        self.assertEqual(adapter.validate_arguments(arguments), arguments)
        arguments["cursor"] += "a"
        with self.assertRaisesRegex(ValidationError, "4094-byte limit"):
            adapter.validate_arguments(arguments)
        arguments = self.arguments("한" * 300)
        self.assertEqual(adapter.validate_arguments(arguments), arguments)
        client = OpenAlexClient(endpoint=self.endpoint + "/" + "a" * 2000)
        with self.assertRaisesRegex(ValueError, "4094-byte limit"):
            client.run(**arguments)
        self.assertEqual(len(OpenAlexFixture.requests), before)


if __name__ == "__main__":
    unittest.main()
