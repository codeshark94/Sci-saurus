import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scisaurus.core.schema import canonical_bytes
from scisaurus.dashboard.server import DashboardServer, DashboardUnixServer, DashboardService, DashboardSnapshot
from scisaurus.runtime.composer import validate_workflow


def fixture_workflow(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "projects/survey").mkdir(parents=True, exist_ok=True)
    (root / "stage.json").write_text("{}")
    return {"schema_version": "composer-workflow-1", "id": root.name.replace(" ", "-"), "revision": 1,
            "project_id": str(root / "composer"), "objective": "Local process-control fixture",
            "stages": [{"id": "survey", "kind": "survey", "config_path": str(root / "stage.json"),
                        "project_dir": str(root / "projects/survey"), "depends_on": [], "estimate_seconds": 1,
                        "bindings": [], "deadline_seconds": 3600, "reuse_completed": False, "reuse_output_path": None}],
            "time_policy": {"first_result_seconds": 1, "target_seconds": 3600, "hard_seconds": 3600, "checkpoint_seconds": 1},
            "completion": {"required_stage_ids": ["survey"], "release_requires_human": True}}


class DashboardControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="desktop controls ")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workflow = fixture_workflow(self.root)
        (self.root / "workflow.json").write_text(json.dumps(self.workflow))
        self.service = DashboardService(self.root)

    def wait_for(self, predicate, timeout=12):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            time.sleep(.1)
        self.fail("process lifecycle did not settle before timeout")

    def test_real_supervisor_start_stop_resume_and_reconnect(self):
        site = self.root / "hooks"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import scisaurus.runtime.composer as c\n"
            "import scisaurus.runtime.composer_supervisor as s\n"
            "from scisaurus.tests.desktop_fixture import DesktopFixtureRunner\n"
            "c.ComposerRunner = s.ComposerRunner = DesktopFixtureRunner\n")
        repository = Path(__file__).resolve().parents[2]
        with patch.dict(os.environ, {"PYTHONPATH": str(site) + os.pathsep + str(repository)}):
            result = self.service.start_composer(settings={"development": True, "stop_after_stage": "survey"})
            pid = result["pid"]
            self.addCleanup(lambda: self._cleanup_pid(pid))
            output = self.root / "composer/output"
            self.wait_for(lambda: (output / "fixture-worker.pid").exists())
            worker = int((output / "fixture-worker.pid").read_text())
            self.addCleanup(lambda: self._cleanup_pid(worker))
            self.assertIn("--watch", result["command"])
            other = DashboardService(self.root)
            self.assertEqual(other.run_status()["status"], "running")
            self.assertEqual(other.start_composer()["status"], "already_running")
            deadline = json.loads((output / "progress.json").read_text())["deadline_at_epoch"]
            self.assertEqual(other.stop_composer()["status"], "stopping")
            self.wait_for(lambda: not other.run_status()["processes"])
            self.assertEqual(other.run_status()["status"], "stopped")
            self.assertEqual(json.loads((output / "progress.json").read_text())["deadline_at_epoch"], deadline)
            with self.assertRaises(ProcessLookupError):
                os.kill(worker, 0)
            self.assertEqual(other.stop_composer()["status"], "already_stopped")
            resumed = other.start_composer(resume=True)
            self.addCleanup(lambda: self._cleanup_pid(resumed["pid"]))
            self.assertIn("--resume", resumed["command"])
            self.assertEqual(resumed["command"][resumed["command"].index("--stop-after-stage") + 1], "survey")
            self.wait_for(lambda: json.loads((output / "progress.json").read_text()).get("resume_count") == 1)
            self.assertEqual(json.loads((output / "progress.json").read_text())["deadline_at_epoch"], deadline)
            other.stop_composer()
            self.wait_for(lambda: not other.run_status()["processes"])

    @staticmethod
    def _cleanup_pid(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def test_stop_rechecks_identity_and_rejects_foreign_or_unsupervised_owner(self):
        owner = {"pid": 12345, "started": "a", "parent_pid": 1, "command": "python --watch", "supervised": True}
        for second in ({**owner, "started": "b"}, {**owner, "command": "different"}):
            with patch.object(self.service, "_composer_processes", side_effect=[[owner], [second]]), patch("os.kill") as kill:
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    self.service.stop_composer()
                kill.assert_not_called()
        with patch.object(self.service, "_composer_processes", return_value=[{**owner, "supervised": False}]), patch("os.kill") as kill:
            with self.assertRaisesRegex(ValueError, "no checkpoint supervisor"):
                self.service.stop_composer()
            kill.assert_not_called()

    def test_completed_expired_and_uninitialized_resume_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "initialized"):
            self.service.start_composer(resume=True)
        output = self.root / "composer/output"
        output.mkdir(parents=True, exist_ok=True)
        state = self.root / "composer/state"
        state.mkdir()
        conn = sqlite3.connect(state / "control.sqlite")
        conn.execute("CREATE TABLE artifacts(logical_id TEXT, version INT, body_hash TEXT)")
        conn.close()
        for progress in ({"status": "completed"}, {"status": "paused", "deadline_at_epoch": time.time() - 1}):
            (output / "progress.json").write_text(json.dumps(progress))
            with patch.object(self.service, "_composer_processes", return_value=[]), patch("subprocess.Popen") as launch:
                self.assertFalse(self.service.run_status()["can_resume"])
                with self.assertRaisesRegex(ValueError, "complete or.*elapsed"):
                    self.service.start_composer(resume=True)
                launch.assert_not_called()

    def test_current_immutable_workflow_selects_matching_revision(self):
        current = {**self.workflow, "revision": 2, "objective": "Revised local fixture"}
        stored = validate_workflow(current)
        cp = self.root / "composer"
        (cp / "state").mkdir(parents=True)
        (cp / "objects/sha256").mkdir(parents=True)
        raw = canonical_bytes(stored)
        digest = hashlib.sha256(raw).hexdigest()
        (cp / "objects/sha256" / digest).write_bytes(raw)
        with closing(sqlite3.connect(cp / "state/control.sqlite")) as conn:
            conn.execute("CREATE TABLE artifacts(logical_id TEXT, version INT, body_hash TEXT)")
            conn.execute("INSERT INTO artifacts VALUES(?,?,?)", ("command/composer/workflow", 2, digest))
            conn.commit()
        revised = self.root / "workflow-revised.json"
        revised.write_text(json.dumps(current))
        snapshot = DashboardSnapshot(self.root)
        self.assertEqual(snapshot.workflow_path, revised)
        self.assertEqual(snapshot.workflow, stored)
        revised.unlink()
        snapshot = DashboardSnapshot(self.root)
        self.assertEqual(snapshot.workflow, stored)
        self.assertIn("No local workflow descriptor", snapshot.workflow_control_error)
        self.assertEqual(len(self.service.projects()["projects"]), 1)
        self.assertFalse(self.service.run_status()["can_resume"])
        with self.assertRaisesRegex(ValueError, "No local workflow descriptor"):
            self.service.start_composer(resume=True)
        owner = {"pid": 12345, "started": "a", "parent_pid": 1,
                 "command": "python --watch", "supervised": True}
        with patch.object(self.service, "_composer_processes", return_value=[owner]), patch("os.kill") as kill:
            self.assertTrue(self.service.run_status()["can_stop"])
            self.assertEqual(self.service.stop_composer()["status"], "stopping")
            kill.assert_called_once_with(owner["pid"], signal.SIGTERM)

    def test_failed_start_admission_stops_owned_supervisor_and_worker(self):
        site = self.root / "hooks"
        site.mkdir()
        (site / "sitecustomize.py").write_text(
            "import scisaurus.runtime.composer as c\n"
            "import scisaurus.runtime.composer_supervisor as s\n"
            "from scisaurus.tests.desktop_fixture import DesktopFixtureRunner\n"
            "c.ComposerRunner = s.ComposerRunner = DesktopFixtureRunner\n")
        repository = Path(__file__).resolve().parents[2]
        discover = self.service._composer_processes
        calls = []

        def missing_identity(path):
            owners = discover(path)
            calls.extend(owners)
            return []

        with patch.dict(os.environ, {"PYTHONPATH": str(site) + os.pathsep + str(repository)}):
            with patch.object(self.service, "_composer_processes", side_effect=missing_identity):
                with self.assertRaisesRegex(ValueError, "identity could not be verified"):
                    self.service.start_composer(settings={"development": True})
        self.assertTrue(calls)
        self.assertEqual(discover(self.root / "workflow.json"), [])
        self.assertEqual(self.service._owned_processes, {})
        worker_path = self.root / "composer/output/fixture-worker.pid"
        if worker_path.exists():
            with self.assertRaises(ProcessLookupError):
                os.kill(int(worker_path.read_text()), 0)

    def test_http_actions_reject_cross_origin_host_and_form_requests(self):
        server = DashboardServer(("127.0.0.1", 0), self.service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/api/actions"
        for headers, code in (({"Content-Type": "application/json", "Origin": "https://foreign.test"}, 403),
                              ({"Content-Type": "application/json", "Host": "foreign.test"}, 403),
                              ({"Content-Type": "text/plain"}, 415)):
            request = Request(url, data=b'{"action":"stop_composer"}', headers=headers)
            with self.assertRaises(HTTPError) as error:
                urlopen(request, timeout=2)
            self.assertEqual(error.exception.code, code)
            error.exception.close()
        request = Request(url, data=b'{"action":"stop_composer"}', headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            self.assertEqual(json.loads(response.read())["status"], "already_stopped")

    def test_private_socket_serves_resources_and_rejects_other_origins(self):
        path = self.root / "backend.sock"
        server = DashboardUnixServer(str(path), self.service)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual(server.socket.family, socket.AF_UNIX)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        def request(method, route, *, origin=None, content_type="application/json"):
            body = b'{"action":"stop_composer"}' if method == "POST" else b""
            headers = f"Host: localhost\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
            if origin:
                headers += f"Origin: {origin}\r\n"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(3)
                stream.connect(str(path))
                stream.sendall(f"{method} {route} HTTP/1.0\r\n{headers}\r\n".encode() + body)
                reply = http.client.HTTPResponse(stream)
                reply.begin()
                return reply.status, reply.read()

        status, body = request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Mission controls", body)
        self.assertEqual(request("POST", "/api/actions", origin="http://localhost")[0], 403)
        status, body = request("POST", "/api/actions", origin="scisaurus://localhost")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "already_stopped")
        self.assertEqual(request("POST", "/api/actions")[0], 200)
        self.assertEqual(request("POST", "/api/actions", origin="scisaurus://localhost", content_type="text/plain")[0], 415)

    def test_settings_are_allowlisted_and_project_is_confined(self):
        for settings in ({"shell": "anything"}, {"development": "yes"}, {"stop_after_stage": "missing"}):
            with patch.object(self.service, "_composer_processes", return_value=[]), patch("subprocess.Popen") as launch:
                with self.assertRaises(ValueError):
                    self.service.start_composer(settings=settings)
                launch.assert_not_called()
        with self.assertRaises(ValueError):
            self.service.stop_composer("../foreign")
