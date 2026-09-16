import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote
from urllib.request import Request, urlopen

from scisaurus.dashboard.server import DashboardServer, DashboardService, DashboardSnapshot


class DashboardTests(unittest.TestCase):
    def make_project(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "output").mkdir()
        (root / "workflow.json").write_text(json.dumps({
            "workflow_id": "dashboard-test",
            "objective": "Check a bounded research mission.",
            "project_id": str(root),
            "stages": [
                {"id": "topic", "kind": "topic"},
                {"id": "survey", "kind": "survey"},
            ],
        }), encoding="utf-8")
        (root / "README.md").write_text("# Dashboard fixture\n", encoding="utf-8")
        (root / "run.log").write_text(json.dumps({
            "phase": "survey:specialists_admitted", "status": "running",
        }) + "\n", encoding="utf-8")
        (root / "output" / "progress.json").write_text(json.dumps({
            "status": "running",
            "phase": "survey:specialists_admitted",
            "elapsed_seconds": 42,
            "organization": {
                "schema_version": "project-organization-2",
                "departments": [{"id": "research", "label": "Research", "chief": "chief", "adversary": "adversary"}],
                "agents": [
                    {"id": "research.chief", "department": "research", "appointment": "chief", "label": "Chief"},
                    {"id": "research.search-strategist", "department": "research", "appointment": "specialist", "label": "Search strategist"},
                ],
                "active_assignments": [{
                    "assigned_role": "research.search-strategist", "task_id": "task-survey-1",
                    "stage_id": "survey", "task_state": "running", "attempt_state": "started",
                }],
            },
            "stages": {
                "topic": {"status": "completed", "attempt_count": 1},
                "survey": {
                    "status": "running",
                    "attempt_count": 2,
                    "active_agents": ["research.search-strategist"],
                    "required_agents": ["research.search-strategist"],
                    "verifier_agent": "research.fact-verifier",
                    "assignment_task_ids": ["task-survey-1"],
                },
            },
        }), encoding="utf-8")
        return temporary, root

    def test_snapshot_is_read_only_and_tracks_live_checkpoint(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)

        snapshot = DashboardSnapshot(root).payload()

        self.assertEqual(snapshot["live"]["status"], "running")
        self.assertEqual(snapshot["live"]["current_stage"], "survey")
        self.assertEqual(snapshot["pipeline"]["completed"], 1)
        self.assertEqual(snapshot["pipeline"]["total"], 2)
        self.assertEqual(snapshot["specialists"][0]["role"], "research.search-strategist")
        self.assertEqual(snapshot["specialists_roster"], 2)
        self.assertEqual(snapshot["specialists_active"], 1)
        self.assertTrue(snapshot["integrity"]["read_only"])
        self.assertTrue(any(item["kind"] == "runtime_log" for item in snapshot["logs"]))
        self.assertTrue(any(item["path"] == "output" for item in snapshot["structure"]["directories"]))
        self.assertTrue(any(item["ref"] == "project::output/progress.json"
                            for item in snapshot["checkpoints"]))
        self.assertEqual((root / "README.md").read_text(encoding="utf-8"), "# Dashboard fixture\n")

    def test_workspace_overview_is_lightweight_and_project_scoped(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)

        overview = DashboardService(root).workspace()

        self.assertEqual(overview["schema_version"], "dashboard-workspace-1")
        self.assertEqual(overview["workspace"]["name"], "Sci-saurus")
        self.assertEqual(overview["summary"]["total_projects"], 1)
        self.assertEqual(overview["summary"]["stale_projects"], 1)
        self.assertEqual(overview["projects"][0]["current_stage"], "survey")
        self.assertEqual(overview["projects"][0]["completed_stages"], 1)
        self.assertEqual(overview["projects"][0]["total_stages"], 2)
        self.assertEqual(overview["active_runs"], [])
        self.assertEqual(overview["recent_projects"][0]["ref"], ".")
        self.assertNotIn("artifacts", overview)

    def test_http_snapshot_file_preview_and_path_boundary(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        object_path = root / "objects" / "sha256" / ("a" * 64)
        object_path.parent.mkdir(parents=True)
        object_path.write_text(json.dumps({"role": "research.search-strategist", "outcome": None}), encoding="utf-8")
        server = DashboardServer(("127.0.0.1", 0), DashboardService(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        base_url = f"http://127.0.0.1:{server.server_port}"

        with urlopen(f"{base_url}/api/snapshot", timeout=2) as response:
            snapshot = json.loads(response.read())
        self.assertEqual(snapshot["schema_version"], "dashboard-snapshot-1")
        self.assertEqual(snapshot["project"]["name"], root.name)

        with urlopen(f"{base_url}/api/workspace", timeout=2) as response:
            workspace = json.loads(response.read())
        self.assertEqual(workspace["schema_version"], "dashboard-workspace-1")
        self.assertEqual(workspace["projects"][0]["ref"], ".")

        with urlopen(f"{base_url}/api/file?ref={quote('project::README.md')}", timeout=2) as response:
            preview = json.loads(response.read())
        self.assertEqual(preview["text"], "# Dashboard fixture\n")
        self.assertTrue(preview["is_text"])

        object_preview = DashboardService(root).file_payload(
            f"project::objects/sha256/{'a' * 64}"
        )
        self.assertTrue(object_preview["is_text"])
        self.assertEqual(object_preview["media_type"], "application/json")

        with self.assertRaises(ValueError):
            DashboardService(root).file_payload("project::../outside.txt")

    def test_project_manager_creates_isolated_validated_workflow(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        template_path = Path(__file__).resolve().parents[2] / "local-private" / "autolab" / "workflow.json"
        (root / "workflow.json").write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")

        service = DashboardService(root)
        result = service.create_project({
            "template": ".",
            "slug": "fixture-run",
            "objective": "Test an isolated Composer project lifecycle.",
            "hard_seconds": 259200,
        })

        target = root / "missions" / "fixture-run"
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["project"], "missions/fixture-run")
        self.assertTrue((target / "workflow.json").is_file())
        self.assertTrue((target / "composer").is_dir())
        workflow = json.loads((target / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow["id"], "mission-fixture-run")
        self.assertEqual(workflow["objective"], "Test an isolated Composer project lifecycle.")
        self.assertEqual(Path(workflow["project_id"]).resolve(), (target / "composer").resolve())
        self.assertTrue(all(Path(stage["project_dir"]).is_dir() for stage in workflow["stages"]))
        self.assertFalse(any(path.name.startswith(".workflow-") for path in target.iterdir()))

        projects = service.projects()["projects"]
        created = next(item for item in projects if item["ref"] == "missions/fixture-run")
        self.assertEqual(created["status"], "draft")

        server = DashboardServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        request = Request(
            f"http://127.0.0.1:{server.server_port}/api/actions",
            data=json.dumps({
                "action": "create_project",
                "template": ".",
                "slug": "http-run",
                "objective": "Exercise the local project action boundary.",
                "hard_seconds": 259200,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            action = json.loads(response.read())
        self.assertEqual(response.status, 201)
        self.assertEqual(action["project"], "missions/http-run")


if __name__ == "__main__":
    unittest.main()
