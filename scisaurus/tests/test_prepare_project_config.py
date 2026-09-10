"""Configuration preparation preserves user settings and pins installed inputs."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/prepare-project-config.py"
SPEC = importlib.util.spec_from_file_location("prepare_project_config", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class TestPrepareProjectConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-config-preparation-")
        self.repo = Path(self.temp.name).resolve()
        for directory in ("config", "scripts", ".venv/bin", ".venv/packages"):
            (self.repo / directory).mkdir(parents=True)
        self.template = {"live_dispatch_allowed": False, "model": {"base_url": "runtime_required", "model": "runtime_required", "auth_env": None},
                         "mcp_fetch_command": [".venv/bin/python", "-m", "mcp_server_fetch"], "operations": {"environment_files": []},
                         "document": {"title": "Preserved example"}}
        (self.repo / "config/project-run.example.json").write_text(json.dumps(self.template))
        (self.repo / ".venv/bin/python").symlink_to(sys.executable)
        (self.repo / ".venv/pyvenv.cfg").write_text("include-system-site-packages = false\n")
        self.versions = {"mcp-server-fetch": "2026.8.18", "mcp": "1.30.0", "readabilipy": "0.3.0"}
        (self.repo / "requirements-runtime.txt").write_text("\n".join(f"{name}=={version}" for name, version in self.versions.items()))
        self.lock = self.repo / "scripts/readabilipy-package-lock.json"
        self.lock.write_text('{"lockfileVersion":3}')
        self.installed_lock = self.repo / ".venv/packages/package-lock.json"
        self.installed_lock.write_bytes(self.lock.read_bytes())
        self.module = self.repo / ".venv/packages/server.py"
        self.module.write_text("# Installed source identity fixture\n")
        self.inspection = {"prefix": str(self.repo / ".venv"), "versions": self.versions,
                           "environment_files": [str(self.module), str(self.installed_lock)], "extractor_lock": str(self.installed_lock)}
        self.output = self.repo / "prepared.json"

    def tearDown(self):
        self.temp.cleanup()

    def subprocess_result(self, inspected=None):
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(inspected or self.inspection), stderr="")

    def test_preserves_model_and_dispatch_settings_and_venv_symlink_path(self):
        with patch.object(helper.subprocess, "run", return_value=self.subprocess_result()) as call:
            result = helper.prepare_config(self.output, repo_root=self.repo)
        prepared = json.loads(self.output.read_text())
        expected = deepcopy(self.template)
        expected["mcp_fetch_command"][0] = str(self.repo / ".venv/bin/python")
        expected["operations"]["environment_files"] = result["environment_files"]
        self.assertEqual(prepared, expected)
        self.assertEqual(call.call_args.args[0][0], str(self.repo / ".venv/bin/python"))
        self.assertIn("-I", call.call_args.args[0])
        self.assertNotEqual(result["interpreter"], str(Path(sys.executable).resolve()))
        self.assertTrue(all(Path(path).is_absolute() and Path(path).is_file() for path in result["environment_files"]))

    def test_existing_output_is_never_overwritten_or_inspected(self):
        self.output.write_text("existing configuration")
        with patch.object(helper.subprocess, "run") as call, self.assertRaises(FileExistsError):
            helper.prepare_config(self.output, repo_root=self.repo)
        call.assert_not_called()
        self.assertEqual(self.output.read_text(), "existing configuration")

    def test_missing_runtime_fails_without_creating_config(self):
        (self.repo / ".venv/bin/python").unlink()
        with self.assertRaisesRegex(ValueError, "setup-runtime.sh"):
            helper.prepare_config(self.output, repo_root=self.repo)
        self.assertFalse(self.output.exists())

    def test_wrong_installed_version_or_lock_fails_without_partial_output(self):
        inspected = deepcopy(self.inspection)
        inspected["versions"]["mcp-server-fetch"] = "unconfigured-version"
        with patch.object(helper.subprocess, "run", return_value=self.subprocess_result(inspected)), self.assertRaisesRegex(ValueError, "differs"):
            helper.prepare_config(self.output, repo_root=self.repo)
        self.assertFalse(self.output.exists())
        self.installed_lock.write_text('{"lockfileVersion":2}')
        with patch.object(helper.subprocess, "run", return_value=self.subprocess_result()), self.assertRaisesRegex(ValueError, "extractor lock"):
            helper.prepare_config(self.output, repo_root=self.repo)
        self.assertFalse(self.output.exists())

    @unittest.skipUnless((REPO / ".venv/bin/python").is_file(), "Installed project runtime is unavailable")
    def test_actual_installed_runtime_is_discovered_without_enabling_dispatch(self):
        completed = subprocess.run([sys.executable, str(SCRIPT), "--output", str(self.output)],
                                   text=True, capture_output=True, timeout=40)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        prepared = json.loads(self.output.read_text())
        example = json.loads((REPO / "config/project-run.example.json").read_text())
        self.assertEqual(prepared["model"], example["model"])
        self.assertEqual(prepared["live_dispatch_allowed"], example["live_dispatch_allowed"])
        self.assertEqual(prepared["mcp_fetch_command"][0], str(REPO / ".venv/bin/python"))
        files = prepared["operations"]["environment_files"]
        for ending in ("pyvenv.cfg", "requirements-runtime.txt", "readabilipy-package-lock.json", "package-lock.json",
                       "mcp_server_fetch/server.py", "mcp/types.py", "readabilipy/simple_json.py", "ExtractArticle.js"):
            self.assertTrue(any(path.endswith(ending) for path in files), ending)
        self.assertEqual(sum(path.endswith(".dist-info/METADATA") for path in files), 3)
        self.assertFalse(any("__pycache__" in path for path in files))
        self.assertTrue(all(Path(path).is_file() for path in files))


if __name__ == "__main__":
    unittest.main()
