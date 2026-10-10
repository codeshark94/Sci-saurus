"""Immutable solver images, isolation arguments and daemon-owned cleanup."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.container_runtime import container_command, image_manifest, run_container, validate_container
from scisaurus.runtime.program_sandbox import SandboxResult


def runtime():
    return {"kind": "container", "executable": "/usr/local/bin/docker", "container": {
        "image_id": "sha256:" + "a" * 64, "platform": "linux/arm64", "interpreter": "/usr/bin/python3",
        "cpu_count": 2, "memory_bytes": 8 * 1024 ** 3, "daemon_socket": "/tmp/docker.sock"}}


class ContainerRuntimeTests(unittest.TestCase):
    def test_mutable_or_extra_container_configuration_is_rejected(self):
        for change in ({"image_id": "solver:latest"}, {"platform": "darwin/arm64"},
                       {"cpu_count": True}, {"memory_bytes": 0}, {"mount": "/"},
                       {"interpreter": "/usr/../bin/python"}):
            r = runtime()["container"]
            r.update(change)
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_container(r)

    def test_workspace_only_and_fixed_isolation_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / "program.py"
            program.write_text("print('ok')")
            _, command = container_command(runtime(), program, root)
            self.assertIn("--network=none", command)
            self.assertIn("--read-only", command)
            self.assertIn("--cap-drop=ALL", command)
            self.assertIn("--security-opt=no-new-privileges", command)
            self.assertIn("--pull=never", command)
            self.assertEqual(command.count("--mount"), 1)
            self.assertEqual(command[-2:], ["/usr/bin/python3", "/work/program.py"])
            self.assertIn("unix:///tmp/docker.sock", command)
            self.assertIn("/usr/bin/timeout", command)
            self.assertNotIn("target=/var/run/docker.sock", " ".join(command))
            with tempfile.NamedTemporaryFile() as outside, self.assertRaises(ValidationError):
                container_command(runtime(), outside.name, root)

    def test_image_identity_and_architecture_are_required(self):
        row = {"Id": "sha256:" + "a" * 64, "Os": "linux", "Architecture": "arm64",
               "RootFS": {"Layers": ["sha256:layer"]}, "Config": {}, "Size": 123}
        result = subprocess.CompletedProcess([], 0, json.dumps([row]).encode(), b"")
        with patch("scisaurus.runtime.container_runtime.subprocess.run", return_value=result):
            self.assertTrue(image_manifest(runtime())["complete"])
            wrong = runtime()
            wrong["container"]["platform"] = "linux/amd64"
            with self.assertRaises(ValidationError):
                image_manifest(wrong)

    def test_timeout_removes_daemon_owned_container(self):
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "program.py"
            program.write_text("pass")
            with patch("scisaurus.runtime.container_runtime.image_manifest"), \
                    patch("scisaurus.runtime.run_control.start_process"), \
                    patch("scisaurus.runtime.container_runtime.capture_process", return_value=SandboxResult(None, b"partial", b"diagnostic", True, False, "container")), \
                    patch("scisaurus.runtime.container_runtime.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as mock:
                result = run_container(runtime(), program, workspace=directory, timeout_seconds=1)
                self.assertTrue(result.timed_out)
                self.assertEqual(result.stdout, b"partial")
                self.assertEqual(mock.call_args.args[0][3:5], ["rm", "--force"])

    def test_truncated_output_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "program.py"
            program.write_text("pass")
            with patch("scisaurus.runtime.container_runtime.image_manifest"), \
                    patch("scisaurus.runtime.run_control.start_process"), \
                    patch("scisaurus.runtime.container_runtime.subprocess.run"), \
                    patch("scisaurus.runtime.container_runtime.capture_process", return_value=SandboxResult(0, b"x" * 10, b"", False, True, "container")):
                result = run_container(runtime(), program, workspace=directory, max_bytes=10)
                self.assertTrue(result.truncated)
                self.assertEqual(len(result.stdout), 10)


class CleanupFailureTests(unittest.TestCase):
    def test_cleanup_failure_keeps_transport_and_cannot_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "program.py"
            program.write_text("pass")
            for cleanup in (subprocess.CompletedProcess([], 1, b"", b"daemon error"),
                            subprocess.TimeoutExpired("docker rm", 30)):
                with self.subTest(cleanup=cleanup), patch("scisaurus.runtime.container_runtime.image_manifest"), \
                     patch("scisaurus.runtime.run_control.start_process"), \
                     patch("scisaurus.runtime.container_runtime.capture_process", return_value=SandboxResult(0,b"raw",b"log",False,False,"container")), \
                     patch("scisaurus.runtime.container_runtime.subprocess.run", side_effect=[cleanup,subprocess.CompletedProcess([],1,b"",b"unavailable")] if not isinstance(cleanup,Exception) else cleanup):
                    result = run_container(runtime(),program,workspace=directory)
                    self.assertFalse(result.cleanup["completed"])
                    self.assertEqual(result.stdout,b"raw")

    def test_cancel_retains_original_error_and_partial_output(self):
        from scisaurus.runtime.run_control import RunPausedError
        error=RunPausedError("paused")
        error.process_result=SandboxResult(-9,b"raw",b"diagnostic",False,False,"container")
        with tempfile.TemporaryDirectory() as directory:
            program=Path(directory)/"program.py"; program.write_text("pass")
            with patch("scisaurus.runtime.container_runtime.image_manifest"), patch("scisaurus.runtime.run_control.start_process"), \
                 patch("scisaurus.runtime.container_runtime.capture_process",side_effect=error), \
                 patch("scisaurus.runtime.container_runtime.subprocess.run",side_effect=subprocess.TimeoutExpired("cleanup",30)), \
                 self.assertRaises(RunPausedError) as raised:
                run_container(runtime(),program,workspace=directory)
            self.assertIs(raised.exception,error)
            self.assertEqual(error.process_result.stdout,b"raw")
            self.assertFalse(error.container_cleanup["completed"])
