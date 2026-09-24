"""Transport fixtures test failures; live providers are exercised separately."""

import base64
from email.message import Message
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scisaurus.runtime.retrieval import CrossrefClient, MCPFetchClient, _robots_policy
from scisaurus.runtime.pdf_text import pdf_capture_budget


def minimal_pdf(text="Parser fixture text"):
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    lines = text.splitlines() or [text]
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("ascii")
               for line in lines]
    content = b"BT /F1 12 Tf 72 720 Td " + b" Tj 0 -16 Td ".join(
        b"(" + line + b")" for line in escaped) + b" Tj ET"
    objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(pdf)
    pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(pdf)


class FixturePDFResponse:
    def __init__(self, body, *, url, status=200, content_type="application/pdf"):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.body.read(size)

    def read1(self, size=-1):
        return self.body.read(size)

    def close(self):
        self.body.close()


class CrossrefFixture(BaseHTTPRequestHandler):
    queries = []
    rate_limit_once = True
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        self.queries.append(query)
        term = query.get("query.bibliographic", query.get("filter", [""]))[0]
        status, content = 200, b""
        if term == "interrupted":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"3\r\nabc\r\n5\r\nxy")
            self.wfile.flush()
            self.close_connection = True
            return
        elif term == "rate limited":
            status, content = 429, b'{"message":"slow down"}'
        elif term == "rate limited once" and type(self).rate_limit_once:
            type(self).rate_limit_once = False
            status, content = 429, b'{"message":"retry once"}'
        elif term == "malformed":
            content = b"upstream HTML error"
        elif term == "huge":
            content = b"x" * 8192
        else:
            if term == "slow":
                time.sleep(0.25)
            content = json.dumps({
                "status": "ok", "message-version": "1.0.0", "message": {
                    "total-results": 0 if term == "empty" else 12, "next-cursor": "page-two",
                    "items": [] if term == "empty" else [{
                        "DOI": "10.1234/fixture", "title": ["Measured association"],
                        "author": [{"given": "A", "family": "Researcher"}],
                        "published": {"date-parts": [[2025]]},
                    }],
                },
            }).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Rate-Limit-Limit", "1")
        self.send_header("Retry-After", "5")
        self.end_headers()
        try:
            self.wfile.write(content)
        except BrokenPipeError:
            pass


class TestCrossrefRetrieval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CrossrefFixture)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.server.server_port}/works"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_search_retains_exact_capture_and_pagination(self):
        result = CrossrefClient(endpoint=self.endpoint, mailto="research@example.org").search("A & B", limit=1)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["sources"][0]["doi"], "10.1234/fixture")
        self.assertEqual(result["sources"][0]["representation"], "metadata")
        self.assertEqual(result["metadata"]["next_cursor"], "page-two")
        self.assertFalse(result["metadata"]["result_set_complete"])
        self.assertEqual(result["metadata"]["headers"]["x-rate-limit-limit"], "1")
        raw = base64.b64decode(result["capture"]["body"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result["capture_sha256"])
        self.assertEqual(json.loads(raw), result["raw_response"])
        self.assertEqual(CrossrefFixture.queries[-1]["query.bibliographic"], ["A & B"])
        self.assertEqual(CrossrefFixture.queries[-1]["sort"], ["score"])
        self.assertEqual(CrossrefFixture.queries[-1]["mailto"], ["research@example.org"])
        CrossrefClient(endpoint=self.endpoint).search("A & B", cursor="page-two")
        self.assertEqual(CrossrefFixture.queries[-1]["cursor"], ["page-two"])

    def test_doi_query_uses_exact_provider_filter(self):
        result = CrossrefClient(endpoint=self.endpoint).search("https://doi.org/10.1234/EXAMPLE", limit=1)
        self.assertEqual(result["metadata"]["match_mode"], "exact_doi")
        self.assertEqual(CrossrefFixture.queries[-1]["filter"], ["doi:10.1234/example"])

    def test_empty_results_are_distinct_from_rate_limits_and_bad_responses(self):
        client = CrossrefClient(endpoint=self.endpoint, max_retries=0)
        self.assertEqual(client.search("empty")["outcome"], "empty")
        limited = client.search("rate limited")
        self.assertEqual(limited["outcome"], "rate_limited")
        self.assertEqual(limited["metadata"]["headers"]["retry-after"], "5")
        self.assertEqual(limited["sources"], [])
        self.assertEqual(client.search("malformed")["outcome"], "parse_error")

    def test_transient_rate_limit_retries_inside_one_call_budget(self):
        CrossrefFixture.rate_limit_once = True
        with patch("scisaurus.runtime.retrieval.time.sleep") as sleep:
            result = CrossrefClient(
                endpoint=self.endpoint, timeout=10, max_retries=2,
                retry_backoff_seconds=0,
            ).search("rate limited once")
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["metadata"]["attempts"], 2)
        self.assertEqual(result["metadata"]["retry_wait_seconds"], 5.0)
        sleep.assert_called_once_with(5.0)

    def test_bytes_and_network_wait_are_bounded(self):
        result = CrossrefClient(endpoint=self.endpoint, max_bytes=100).search("huge")
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["capture"]["bytes"], 100)
        self.assertTrue(result["metadata"]["capture_truncated"])
        self.assertEqual(result["sources"], [])
        start = time.monotonic()
        result = CrossrefClient(endpoint=self.endpoint, timeout=0.05).search("slow")
        self.assertEqual(result["outcome"], "timeout")
        self.assertLess(time.monotonic() - start, 0.5)

    def test_interrupted_chunked_response_retains_failure_and_partial_capture(self):
        result = CrossrefClient(endpoint=self.endpoint, timeout=1).search("interrupted")
        self.assertEqual(result["outcome"], "provider_error")
        self.assertTrue(result["metadata"]["capture_incomplete"])
        self.assertEqual(result["sources"], [])
        self.assertIn("IncompleteRead", result["error"])
        body = base64.b64decode(result["capture"]["body"])
        self.assertTrue(body.startswith(b"abc"))
        self.assertEqual(hashlib.sha256(body).hexdigest(), result["capture_sha256"])


MCP_PROTOCOL_FIXTURE = r'''
import json, os, sys, time, subprocess
from pathlib import Path
mode, log_path = sys.argv[1:3]
Path(log_path + '.pid').write_text(str(os.getpid()))
ready = False
def emit(message):
    print(json.dumps(message), flush=True)
for line in sys.stdin:
    incoming = json.loads(line)
    with open(log_path, 'a') as log:
        log.write(json.dumps(incoming) + '\n')
    method = incoming.get('method')
    if method == 'notifications/initialized':
        ready = True
        continue
    if method == 'initialize':
        result = {'protocolVersion': '2099-01-01' if mode == 'version' else '2025-11-25',
                  'serverInfo': {'name': 'test-protocol-fixture', 'version': '1'},
                  'capabilities': {'tools': {}}}
    elif method == 'tools/list':
        assert ready, 'tools/list before initialization notification'
        if mode == 'pagination' and not incoming['params'].get('cursor'):
            result = {'tools': [], 'nextCursor': 'more-tools'}
        else:
            result = {'tools': [{'name': 'other' if mode == 'missing' else 'fetch',
                      'inputSchema': {'type': 'object', 'properties': {
                          'url': {'type': 'string'}, 'max_length': {'type': 'integer'},
                          'start_index': {'type': 'integer'}, 'raw': {'type': 'boolean'}},
                          'required': ['url']}}]}
    elif method == 'tools/call':
        if mode == 'hang':
            time.sleep(30)
        if mode == 'descendant':
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
            Path(log_path + '.child').write_text(str(child.pid))
            time.sleep(30)
        if mode == 'malformed':
            print('npm installer output on the protocol stream', flush=True)
            time.sleep(30)
        if mode == 'flood':
            print('x' * 20000, flush=True)
            time.sleep(30)
        text = 'Measured association; causal interpretation is not established.'
        if mode == 'partial':
            text += '<error>Content truncated.</error>'
        if mode == 'pdf':
            text = 'Content type application/pdf cannot be simplified to markdown, but here is the raw content:\nContents of https://example.org/source:\n%PDF-1.4'
        if mode == 'environment':
            text = json.dumps({'ambient': os.environ.get('SCISAURUS_TEST_API_SECRET'),
                               'explicit': os.environ.get('SCISAURUS_TEST_EXPLICIT')})
        if mode == 'workspace':
            import tempfile
            text = json.dumps({'cwd': os.getcwd(), 'temp': tempfile.gettempdir()})
        result = {'content': [{'type': 'text', 'text': text}], 'isError': mode == 'tool_error'}
        if mode == 'rpc_error':
            emit({'jsonrpc': '2.0', 'id': incoming['id'], 'error': {'code': -32000, 'message': 'source denied'}})
            continue
    else:
        raise AssertionError(method)
    emit({'jsonrpc': '2.0', 'id': incoming['id'] + (1 if mode == 'wrong_id' else 0), 'result': result})
'''


class TestMCPRetrieval(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="scisaurus-mcp-test-")
        self.addCleanup(self.directory.cleanup)
        self.script = Path(self.directory.name, "protocol_fixture.py")
        self.script.write_text(MCP_PROTOCOL_FIXTURE)
        self.log = Path(self.directory.name, "requests.jsonl")

    def client(self, mode="ok", **kwargs):
        return MCPFetchClient([sys.executable, str(self.script), mode, str(self.log)], **kwargs)

    def test_actual_stdio_lifecycle_and_capture_hash(self):
        result = self.client().fetch("https://example.org/source")
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["metadata"]["server_info"]["name"], "test-protocol-fixture")
        self.assertEqual(result["metadata"]["protocol_version"], "2025-11-25")
        self.assertEqual(result["metadata"]["process_returncode"], 0)
        self.assertEqual(hashlib.sha256(result["text"].encode()).hexdigest(), result["capture_sha256"])
        self.assertEqual(base64.b64decode(result["capture"]["body"]).decode(), result["text"])
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([c["method"] for c in calls], ["initialize", "notifications/initialized", "tools/list", "tools/call"])
        self.assertEqual(calls[-1]["params"]["arguments"]["url"], "https://example.org/source")

    def test_tool_pagination_is_followed_before_execution(self):
        result = self.client("pagination").fetch("https://example.org/source")
        self.assertEqual(result["outcome"], "ok")
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(calls[3]["params"]["cursor"], "more-tools")

    def test_tool_and_protocol_failures_are_not_empty_success(self):
        for mode, outcome in (
            ("tool_error", "provider_error"), ("rpc_error", "provider_error"),
            ("wrong_id", "parse_error"), ("missing", "unsupported_capability"),
            ("version", "unsupported_capability"), ("partial", "partial"),
        ):
            with self.subTest(mode=mode):
                self.assertEqual(self.client(mode).fetch("https://example.org/source")["outcome"], outcome)

    def test_malformed_stdout_is_rejected_and_process_is_reaped(self):
        result = self.client("malformed", timeout=1).fetch("https://example.org/source")
        self.assertEqual(result["outcome"], "parse_error")
        self.assertIsNotNone(result["metadata"]["process_returncode"])
        self.assertEqual(result["sources"], [])

    def test_raw_pdf_from_mcp_is_fetched_and_parsed_with_source_provenance(self):
        url = "https://example.org/source"
        pdf = minimal_pdf()
        robots = FixturePDFResponse(b"User-agent: *\nAllow: /\n",
                                    url="https://example.org/robots.txt", content_type="text/plain")
        response = FixturePDFResponse(pdf, url=url)
        with patch("scisaurus.runtime.retrieval._open_http", side_effect=[robots, response]):
            result = self.client("pdf").fetch(url, max_length=1000)
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["metadata"]["representation"], "pdf_extracted_text")
        self.assertEqual(result["metadata"]["pdf_extraction"]["mcp_probe"]["reported_media_types"], ["application/pdf"])
        self.assertIn("Parser fixture text", result["text"])
        self.assertEqual(base64.b64decode(result["metadata"]["pdf_extraction"]["source_capture"]["body"]), pdf)
        self.assertEqual(base64.b64decode(result["capture"]["body"]).decode(), result["text"])
        self.assertEqual(result["sources"], [{"source_url": url, "representation": "pdf_extracted_text"}])

    def test_pdf_capture_is_bounded_by_worker_result_envelope(self):
        url = "https://example.org/large.pdf"
        pdf = minimal_pdf() + b" " * 600_000
        client = self.client("ok", result_max_bytes=300_000)
        with patch("scisaurus.runtime.retrieval._open_http") as fetch:
            result = client.fetch(url, max_length=25_000)
        fetch.assert_not_called()
        self.assertEqual(result["outcome"], "unsupported_capability")
        self.assertEqual(result["metadata"]["pdf_extraction"]["effective_byte_limit"], 0)
        self.assertIn("worker result limit", result["error"])
        self.assertLess(len(json.dumps({"ok": True, "result": result}).encode()), 300_000)

    def test_pdf_over_effective_limit_is_a_bounded_source_gap(self):
        url = "https://example.org/large.pdf"
        pdf = minimal_pdf() + b" " * 1_500_000
        robots = FixturePDFResponse(b"User-agent: *\nAllow: /\n",
                                    url="https://example.org/robots.txt", content_type="text/plain")
        response = FixturePDFResponse(pdf, url=url)
        client = self.client("ok", result_max_bytes=2_000_000)
        with patch("scisaurus.runtime.retrieval._open_http", side_effect=[robots, response]):
            result = client.fetch(url, max_length=10_000)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["sources"], [])
        record = result["metadata"]["pdf_extraction"]
        self.assertLess(record["effective_byte_limit"], len(pdf))
        self.assertTrue(result["metadata"]["capture_truncated"])
        self.assertLess(len(json.dumps({"ok": True, "result": result}).encode()), 2_000_000)

    def test_pdf_base64_budget_rounding_never_exceeds_reserved_bytes(self):
        for result_limit in range(300_000, 300_016):
            with self.subTest(result_limit=result_limit):
                effective = pdf_capture_budget(result_limit, 1, 10_000)
                available = result_limit - 12 - 262_144
                self.assertLessEqual(len(base64.b64encode(b"x" * effective)), available)

    def test_output_and_process_lifetime_are_bounded(self):
        overflow = self.client("flood", max_bytes=4096).fetch("https://example.org/source")
        self.assertEqual(overflow["outcome"], "partial")
        self.assertEqual(overflow["sources"], [])
        started = time.monotonic()
        timeout = self.client("hang", timeout=0.15).fetch("https://example.org/source")
        self.assertEqual(timeout["outcome"], "timeout")
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNotNone(timeout["metadata"]["process_returncode"])

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX")
    def test_timeout_terminates_mcp_descendants(self):
        result = self.client("descendant", timeout=0.3).fetch("https://example.org/source")
        self.assertEqual(result["outcome"], "timeout")
        child = int(Path(str(self.log) + ".child").read_text())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            self.fail("MCP descendant remained after timeout")

    def test_ambient_secrets_are_not_inherited_by_server(self):
        with patch.dict(os.environ, {"SCISAURUS_TEST_API_SECRET": "synthetic-test-secret"}):
            result = self.client("environment", env={"SCISAURUS_TEST_EXPLICIT": "allowed"}).fetch("https://example.org/source")
        self.assertEqual(json.loads(result["text"]), {"ambient": None, "explicit": "allowed"})

    def test_unavailable_command_is_reported_as_failure(self):
        result = MCPFetchClient([str(Path(self.directory.name, "not-installed"))]).fetch("https://example.org/source")
        self.assertEqual(result["outcome"], "provider_error")
        self.assertIsNone(result["capture_sha256"])

    def test_server_and_temporary_files_use_project_workspace(self):
        workspace = Path(self.directory.name, "project-workspace")
        workspace.mkdir()
        result = self.client("workspace", cwd=str(workspace)).fetch("https://example.org/source")
        self.assertEqual(result["outcome"], "ok")
        actual = json.loads(result["text"])
        self.assertEqual(Path(actual["cwd"]).resolve(), workspace.resolve())
        self.assertEqual(Path(actual["temp"]).resolve(), workspace.resolve())
        self.assertEqual(result["metadata"]["cwd"], str(workspace))

    def test_input_validation_precedes_execution(self):
        with self.assertRaises(ValueError):
            self.client().fetch("file:///private/source")
        with self.assertRaises(ValueError):
            self.client().fetch("https://user:password@example.org/source")
        with self.assertRaises(ValueError):
            MCPFetchClient("python server.py")
        with self.assertRaises(ValueError):
            self.client(timeout=float("inf"))
        self.assertFalse(self.log.exists())


class BinarySourceFixture(BaseHTTPRequestHandler):
    requests = []
    host_requests = []
    robots_disallow = None
    robots_disallow_by_port = {}
    robots_status = 200
    redirect_target = "/paper.pdf"

    def log_message(self, *_):
        pass

    def do_GET(self):
        type(self).requests.append(self.path)
        type(self).host_requests.append(f"{self.headers.get('Host')}{self.path}")
        if self.path == "/robots.txt":
            if type(self).robots_status != 200:
                self.send_response(type(self).robots_status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            disallow = type(self).robots_disallow_by_port.get(
                self.server.server_port, type(self).robots_disallow) or ""
            body = f"User-agent: *\nDisallow: {disallow}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/slow.pdf":
            body = minimal_pdf("Slow PDF fixture")
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[:12])
            self.wfile.flush()
            time.sleep(0.3)
            try:
                self.wfile.write(body[12:])
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path == "/denied.pdf":
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/redirect.pdf":
            self.send_response(302)
            self.send_header("Location", type(self).redirect_target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = minimal_pdf("Official server PDF fixture")
        media_type = "application/pdf"
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestOfficialMCPFetch(unittest.TestCase):
    def test_robots_cache_normalizes_default_port_and_rechecks_path(self):
        cache = {}
        response = FixturePDFResponse(
            b"User-agent: *\nDisallow: /blocked\n",
            url="https://example.org/robots.txt", content_type="text/plain")
        with patch("scisaurus.runtime.retrieval._open_http", return_value=response) as open_http:
            allowed = _robots_policy("https://example.org/allowed.pdf",
                                     deadline=time.monotonic() + 2, cache=cache)
            denied = _robots_policy("https://EXAMPLE.org:443/blocked.pdf",
                                    deadline=time.monotonic() + 2, cache=cache)
        self.assertEqual(allowed["outcome"], "allowed")
        self.assertEqual(denied["outcome"], "robots_denied")
        self.assertEqual(open_http.call_count, 1)

    def test_unavailable_robots_policy_fails_closed(self):
        BinarySourceFixture.requests = []
        BinarySourceFixture.robots_status = 503
        server = ThreadingHTTPServer(("127.0.0.1", 0), BinarySourceFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = MCPFetchClient(["/usr/bin/true"], timeout=2).fetch(
                f"http://127.0.0.1:{server.server_port}/paper.pdf")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            BinarySourceFixture.robots_status = 200
        self.assertEqual(result["outcome"], "robots_unavailable")
        self.assertEqual(BinarySourceFixture.requests, ["/robots.txt"])

    def test_pdf_redirect_target_has_its_own_robots_check(self):
        BinarySourceFixture.requests = []
        BinarySourceFixture.robots_disallow = "/paper.pdf"
        server = ThreadingHTTPServer(("127.0.0.1", 0), BinarySourceFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = MCPFetchClient(["/usr/bin/true"], timeout=2).fetch(
                f"http://127.0.0.1:{server.server_port}/redirect.pdf")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            BinarySourceFixture.robots_disallow = None
        self.assertEqual(result["outcome"], "robots_denied")
        self.assertEqual(BinarySourceFixture.requests, ["/robots.txt", "/redirect.pdf"])
        self.assertEqual(len(result["metadata"]["pdf_extraction"]["robots_checks"]), 2)

    def test_cross_origin_pdf_redirect_fetches_target_robots_before_target(self):
        BinarySourceFixture.requests = []
        BinarySourceFixture.host_requests = []
        primary = ThreadingHTTPServer(("127.0.0.1", 0), BinarySourceFixture)
        target = ThreadingHTTPServer(("127.0.0.1", 0), BinarySourceFixture)
        BinarySourceFixture.robots_disallow = None
        BinarySourceFixture.robots_disallow_by_port = {target.server_port: "/paper.pdf"}
        BinarySourceFixture.redirect_target = f"http://127.0.0.1:{target.server_port}/paper.pdf"
        servers = (primary, target)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        try:
            result = MCPFetchClient(["/usr/bin/true"], timeout=2).fetch(
                f"http://127.0.0.1:{primary.server_port}/redirect.pdf")
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()
            BinarySourceFixture.robots_disallow_by_port = {}
            BinarySourceFixture.redirect_target = "/paper.pdf"
        self.assertEqual(result["outcome"], "robots_denied")
        self.assertEqual(BinarySourceFixture.host_requests, [
            f"127.0.0.1:{primary.server_port}/robots.txt",
            f"127.0.0.1:{primary.server_port}/redirect.pdf",
            f"127.0.0.1:{target.server_port}/robots.txt",
        ])
        self.assertEqual([item["outcome"] for item in
                          result["metadata"]["pdf_extraction"]["robots_checks"]],
                         ["allowed", "robots_denied"])

    def test_robots_disallow_prevents_pdf_request(self):
        BinarySourceFixture.requests = []
        BinarySourceFixture.robots_disallow = "/paper.pdf"
        server = ThreadingHTTPServer(("127.0.0.1", 0), BinarySourceFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = MCPFetchClient(["/usr/bin/true"], timeout=2).fetch(
                f"http://127.0.0.1:{server.server_port}/paper.pdf")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            BinarySourceFixture.robots_disallow = None
        self.assertEqual(result["outcome"], "robots_denied")
        self.assertEqual(BinarySourceFixture.requests, ["/robots.txt"])
        self.assertEqual(result["metadata"]["pdf_extraction"]["robots_checks"][0]["outcome"], "robots_denied")

    def test_http_access_denial_remains_explicit_after_robots_allow(self):
        BinarySourceFixture.requests = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), BinarySourceFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = MCPFetchClient(["/usr/bin/true"], timeout=2).fetch(
                f"http://127.0.0.1:{server.server_port}/denied.pdf")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(result["outcome"], "access_denied")
        self.assertEqual(BinarySourceFixture.requests, ["/robots.txt", "/denied.pdf"])

    def test_pdf_response_reads_obey_one_total_deadline(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), BinarySourceFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            result = MCPFetchClient(["/usr/bin/true"], timeout=0.1).fetch(
                f"http://127.0.0.1:{server.server_port}/slow.pdf")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(result["outcome"], "timeout")
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertTrue(result["metadata"]["capture_incomplete"])

    def test_official_server_pdf_url_is_extracted_locally(self):
        python = Path(__file__).absolute().parents[2] / ".venv" / "bin" / "python"
        if not python.exists():
            if importlib.util.find_spec("mcp_server_fetch") is None:
                self.skipTest("official mcp-server-fetch is not installed; run scripts/setup-runtime.sh")
            python = Path(sys.executable)
        server = ThreadingHTTPServer(("127.0.0.1", 0), BinarySourceFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = MCPFetchClient([str(python), "-m", "mcp_server_fetch"], timeout=10).fetch(
                f"http://127.0.0.1:{server.server_port}/paper.pdf",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(result["outcome"], "ok", result.get("error"))
        self.assertEqual(result["metadata"]["representation"], "pdf_extracted_text")
        self.assertEqual(result["metadata"]["transport"], "http_pdf")
        self.assertIn("Official server PDF fixture", result["text"])
        self.assertEqual(result["metadata"]["pdf_extraction"]["http_status"], 200)
        self.assertEqual(result["metadata"]["pdf_extraction"]["source_sha256"],
                         result["metadata"]["pdf_extraction"]["source_capture"]["sha256"])
        self.assertEqual(base64.b64decode(result["capture"]["body"]).decode(), result["text"])
        self.assertEqual(result["sources"][0]["representation"], "pdf_extracted_text")


if __name__ == "__main__":
    unittest.main()
