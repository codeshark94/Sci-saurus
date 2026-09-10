"""Actual bounded subprocess checks using explicitly configured local fixtures."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.runtime.programs import LocalProgramClient, command_identity


class TestLocalPrograms(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-program-test-")
        self.root = Path(self.temp.name).resolve()
        self.script = self.root / "program.py"
        self.script.write_text("import json, sys\nprint(json.dumps(json.load(sys.stdin)))\n")
        self.options = {"command": [str(Path(sys.executable).absolute()), str(self.script)],
                        "timeout": 2, "max_bytes": 4096, "cwd": str(self.root),
                        "env": {"PATH": os.defpath, "LANG": "C.UTF-8"}}

    def tearDown(self):
        self.temp.cleanup()

    def run_program(self, source=None, input=None, **options):
        if source is not None:
            self.script.write_text(source)
        return LocalProgramClient(**{**self.options, **options}).run({} if input is None else input)

    def test_exact_input_stdout_stderr_and_fixed_command_identity(self):
        input = {"z": [4, False], "title": "데이터", "command": "$(touch should-not-exist)"}
        result = self.run_program(
            "import json, sys\nvalue = json.load(sys.stdin)\nsys.stderr.write('diagnostic\\n')\n"
            "print(json.dumps({'valid': False, 'input': value, 'argv': sys.argv[1:]}, indent=2, ensure_ascii=False))\n",
            input=input, command=[*self.options["command"], "--configured-option"])
        self.assertEqual(result["outcome"], "ok", result)
        self.assertFalse(result["document"]["valid"])
        self.assertEqual(result["document"]["input"], input)
        self.assertEqual(result["document"]["argv"], ["--configured-option"])
        self.assertEqual(result["text"], canonical_bytes(result["document"]).decode())
        self.assertEqual(base64.b64decode(result["input_capture"]["body"]), canonical_bytes(input))
        self.assertEqual(result["input_sha256"], sha256_hex(canonical_bytes(input)))
        self.assertEqual(result["metadata"]["input_bytes_written"], len(canonical_bytes(input)))
        self.assertEqual(result["metadata"]["process_returncode"], 0)
        for capture, digest in (("capture", "capture_sha256"), ("stderr_capture", "stderr_sha256")):
            raw = base64.b64decode(result[capture]["body"])
            self.assertEqual(result[capture]["bytes"], len(raw))
            self.assertEqual(result[digest], sha256_hex(raw))
        self.assertEqual(base64.b64decode(result["stderr_capture"]["body"]), b"diagnostic\n")
        command = [*self.options["command"], "--configured-option"]
        self.assertEqual(result["metadata"]["command_identity"], command_identity(command, str(self.root), self.options["env"]))
        self.assertFalse(result["metadata"]["own_process_group"])
        self.assertFalse((self.root / "should-not-exist").exists())

    def test_empty_object_is_a_complete_program_response(self):
        result = self.run_program(input={})
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["document"], {})
        self.assertEqual(result["text"], "{}")

    def test_nonzero_exit_is_failure_even_with_valid_stdout(self):
        result = self.run_program("import sys\nsys.stdin.read()\nprint('{\"valid\":true}')\nsys.exit(7)\n")
        self.assertEqual(result["outcome"], "program_error")
        self.assertEqual(result["metadata"]["process_returncode"], 7)
        self.assertIsNone(result["document"])
        self.assertIn(b"valid", base64.b64decode(result["capture"]["body"]))

    def test_malformed_multiple_nonobject_and_ambiguous_json_are_rejected(self):
        for output in ("", "not JSON", "[]", "{} {}", '{"x":NaN}', '{"x":1e999}', '{"x":1,"x":2}', '{"partial":'):
            with self.subTest(output=output):
                result = self.run_program(f"import sys\nsys.stdin.read()\nsys.stdout.write({output!r})\n")
                self.assertEqual(result["outcome"], "parse_error", result)
                self.assertIsNone(result["document"])
                self.assertEqual(result["metadata"]["process_returncode"], 0)

    def test_stdout_and_stderr_share_the_capture_bound(self):
        for source in (
            "import sys\nsys.stdin.read()\nsys.stdout.write('x' * 100000)\nsys.stdout.flush()\n",
            "import sys\nsys.stdin.read()\nprint('{}', flush=True)\nsys.stderr.write('x' * 100000)\nsys.stderr.flush()\n",
        ):
            with self.subTest(source=source):
                result = self.run_program(source, max_bytes=64)
                self.assertEqual(result["outcome"], "partial", result)
                self.assertTrue(result["metadata"]["capture_truncated"])
                self.assertTrue(result["metadata"]["capture_incomplete"])
                self.assertEqual(result["capture"]["bytes"] + result["stderr_capture"]["bytes"], 64)
                self.assertIsNone(result["document"])

    def test_exact_capture_limit_is_not_truncation(self):
        result = self.run_program("import sys\nsys.stdin.read()\nsys.stdout.write('{}')\nsys.stderr.write('xx')\n", max_bytes=4)
        self.assertEqual(result["outcome"], "ok", result)
        self.assertFalse(result["metadata"]["capture_truncated"])

    def test_timeout_captures_partial_stdout_and_terminates_process(self):
        before = time.monotonic()
        result = self.run_program("import sys, time\nsys.stdin.read()\nsys.stdout.write('{')\nsys.stdout.flush()\ntime.sleep(30)\n", timeout=0.15)
        self.assertLess(time.monotonic() - before, 3)
        self.assertEqual(result["outcome"], "timeout")
        self.assertTrue(result["metadata"]["capture_incomplete"])
        self.assertIsNotNone(result["metadata"]["process_returncode"])
        self.assertEqual(base64.b64decode(result["capture"]["body"]), b"{")

    def test_timeout_also_bounds_blocked_input_and_closed_output_pipes(self):
        cases = [
            ("import time\ntime.sleep(30)\n", {"large": "x" * 1000000}),
            ("import os, sys, time\nsys.stdin.read()\nos.close(1)\nos.close(2)\ntime.sleep(30)\n", {}),
        ]
        for source, input in cases:
            with self.subTest(source=source):
                before = time.monotonic()
                result = self.run_program(source, input=input, timeout=0.15)
                self.assertEqual(result["outcome"], "timeout", result)
                self.assertLess(time.monotonic() - before, 3)

    def test_environment_is_explicit_and_does_not_inherit_private_values(self):
        with patch.dict(os.environ, {"PROGRAM_SECRET": "never-inherit"}):
            result = self.run_program("import json, os, sys\nsys.stdin.read()\nprint(json.dumps({'secret_present': 'PROGRAM_SECRET' in os.environ}))\n")
        self.assertEqual(result["document"], {"secret_present": False})
        with self.assertRaises(ValueError):
            LocalProgramClient(**{**self.options, "env": {"PROGRAM_SECRET": "never-publish"}})

    def test_invalid_python_values_are_rejected_before_execution(self):
        for input in ([], {1: "ambiguous"}, {"nested": {None: "key"}}, {"x": float("nan")}, {"x": (1, 2)}):
            with self.subTest(input=input), self.assertRaises(ValueError):
                LocalProgramClient(**self.options).run(input)

    def test_invalid_process_configuration_is_rejected(self):
        for changed in ({"command": ["python3", str(self.script)]}, {"command": ["/missing/executable"]},
                        {"timeout": float("inf")}, {"timeout": True}, {"max_bytes": 2.1},
                        {"cwd": "relative"}, {"own_process_group": "no"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                LocalProgramClient(**{**self.options, **changed})


if __name__ == "__main__":
    unittest.main()
