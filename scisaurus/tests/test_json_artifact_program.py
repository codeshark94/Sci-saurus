"""Real subprocess checks for the offline JSON policy program and preparation."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


REPO = Path(__file__).resolve().parents[2]
PYTHON = REPO / ".venv/bin/python"
CHECKER = REPO / "scripts/validate-json-artifact.py"
PREPARE = REPO / "scripts/prepare-operations-config.py"
EXAMPLE = REPO / "config/operations-run.example.json"


@unittest.skipUnless(PYTHON.is_file(), "Installed project runtime is unavailable")
class TestJSONArtifactProgram(unittest.TestCase):
    def call(self, request, *, raw=False):
        return subprocess.run([str(PYTHON), "-I", str(CHECKER)],
                              input=request if raw else json.dumps(request),
                              text=True, capture_output=True, timeout=10)

    def test_baseline_policy_fails_and_repaired_policy_passes_real_schema(self):
        config = json.loads(EXAMPLE.read_text())
        request = config["score"]["workloads"][0]["arguments"]["input"]
        failed = self.call(request)
        self.assertEqual(failed.returncode, 0, failed.stderr)
        result = json.loads(failed.stdout)
        self.assertFalse(result["valid"])
        paths = [item["path"] for item in result["errors"]]
        for path in (["retry", "max_attempts"], ["retry", "strategy"], ["rate_limit", "requests_per_minute"]):
            self.assertIn(path, paths)
        policy = json.loads(request["text"])
        policy["retry"] = {"max_attempts": 3, "strategy": "exponential", "base_delay_ms": 200,
                           "max_delay_ms": 2000, "jitter": "full"}
        policy["rate_limit"] = {"requests_per_minute": 120, "burst": 20}
        passed = self.call({"text": json.dumps(policy), "schema": request["schema"]})
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(json.loads(passed.stdout), {"valid": True, "errors": []})
        for changed in (dict(policy, service="other-service"), dict(policy, extra=True),
                        dict(policy, bind={"host": "0.0.0.0", "port": 8080})):
            failed = self.call({"text": json.dumps(changed), "schema": request["schema"]})
            self.assertEqual(failed.returncode, 0, failed.stderr)
            self.assertFalse(json.loads(failed.stdout)["valid"])

    def test_malformed_and_ambiguous_candidates_are_completed_failed_checks(self):
        for text in ("{", '{"value":1,"value":2}', '{"value":NaN}', '{"value":1e999}'):
            with self.subTest(text=text):
                completed = self.call({"text": text, "schema": {"type": "object"}})
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(completed.stdout)
                self.assertFalse(result["valid"])
                self.assertTrue(result["errors"][0]["message"])

    def test_invalid_request_and_invalid_schema_are_program_failures(self):
        requests = ("{", "[]", '{"text":"{}"}',
                    '{"text":"{}","text":"[]","schema":{}}',
                    '{"text":{},"schema":{}}',
                    '{"text":"{}","schema":[]}',
                    '{"text":"{}","schema":{"type":"not-a-json-type"}}')
        for request in requests:
            with self.subTest(request=request):
                completed = self.call(request, raw=True)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(completed.stdout, "")
                self.assertIn("JSON artifact check failed", completed.stderr)

    def test_fragment_references_work_without_external_resources(self):
        schema = {"$defs": {"port": {"type": "integer", "minimum": 1}}, "$ref": "#/$defs/port"}
        for text, valid in (("8080", True), ("0", False), ('"8080"', False)):
            completed = self.call({"text": text, "schema": schema})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIs(json.loads(completed.stdout)["valid"], valid)

    def test_remote_references_never_issue_http_requests(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"type":"object"}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for keyword in ("$ref", "$dynamicRef"):
                completed = self.call({"text": "{}", "schema": {
                    keyword: f"http://127.0.0.1:{server.server_port}/schema"}})
                self.assertEqual(completed.returncode, 2, completed.stdout)
                self.assertEqual(completed.stdout, "")
            self.assertEqual(requests, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_preparation_pins_real_runtime_and_keeps_dispatch_disabled(self):
        with tempfile.TemporaryDirectory(prefix="scisaurus-operations-config-") as directory:
            output = Path(directory) / "run.json"
            completed = subprocess.run([sys.executable, str(PREPARE), "--output", str(output)],
                                       text=True, capture_output=True, timeout=40)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            prepared = json.loads(output.read_text())
            expected = json.loads(EXAMPLE.read_text())
            capability = prepared["score"]["capabilities"][0]
            files = capability["environment_files"]
            self.assertEqual(capability["client"]["command"], [str(PYTHON), "-I", str(CHECKER)])
            self.assertFalse(prepared["live_dispatch_allowed"])
            self.assertEqual(prepared["model"], expected["model"])
            self.assertNotIn("public_queries", prepared)
            self.assertNotIn("source_urls", prepared)
            self.assertEqual(sum(path.endswith(".dist-info/METADATA") for path in files), 5)
            self.assertEqual(sum(path.endswith(".dist-info/RECORD") for path in files), 5)
            for suffix in ("validate-json-artifact.py", "pyvenv.cfg", "jsonschema/validators.py",
                           "referencing/_core.py", "attr/_make.py", "rpds/__init__.py",
                           "jsonschema_specifications/schemas/draft202012/metaschema.json"):
                self.assertTrue(any(path.endswith(suffix) for path in files), suffix)
            self.assertTrue(any("rpds/rpds." in path for path in files))
            self.assertTrue(all(Path(path).is_absolute() and Path(path).is_file() for path in files))
            restored = deepcopy(prepared)
            restored["score"]["capabilities"][0]["client"]["command"] = ["runtime_required"]
            restored["score"]["capabilities"][0]["environment_files"] = []
            self.assertEqual(restored, expected)
            representative = self.call(capability["representative"]["input"])
            self.assertEqual(representative.returncode, 0, representative.stderr)
            self.assertTrue(json.loads(representative.stdout)["valid"])
            existing = output.read_bytes()
            refused = subprocess.run([sys.executable, str(PREPARE), "--output", str(output)],
                                     text=True, capture_output=True, timeout=10)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("refusing to overwrite", refused.stderr)
            self.assertEqual(output.read_bytes(), existing)


if __name__ == "__main__":
    unittest.main()
