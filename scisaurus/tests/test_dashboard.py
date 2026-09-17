import json
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote
from urllib.request import Request, urlopen

from scisaurus.dashboard.server import DashboardServer, DashboardService, DashboardSnapshot
from scisaurus.runtime.argument_defense import build_argument_defense
from scisaurus.runtime.research_program import build_research_program
from scisaurus.tests.test_argument_defense import argument
from scisaurus.tests.test_research_program import topic_package


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

    def test_snapshot_exposes_research_program_and_argument_defense(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        program = build_research_program(topic_package())
        defense = build_argument_defense(
            argument(), {"evidence_ids": ["e1", "e2", "e3"]}, research_program=program)
        live = json.loads((root / "output" / "progress.json").read_text(encoding="utf-8"))
        live["context"] = {
            "topic": {"research_program": program},
            "argument": {"argument_package": {"argument_defense": defense}},
        }
        (root / "output" / "progress.json").write_text(
            json.dumps(live), encoding="utf-8")

        snapshot = DashboardSnapshot(root).payload()
        research = snapshot["research"]
        self.assertEqual(research["research_program"]["schema_version"], "research-program-1")
        self.assertEqual(research["research_program"]["selected_id"], "branch_1")
        self.assertEqual(len(research["research_program"]["branches"]), 3)
        self.assertEqual(research["argument_defense"]["schema_version"], "argument-defense-1")
        self.assertEqual(research["argument_defense"]["claim_count"], 4)
        self.assertTrue(research["argument_defense"]["weak_points"])

    def test_snapshot_projects_real_model_calls_from_call_artifacts(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        objects = root / "objects" / "sha256"
        objects.mkdir(parents=True)

        def artifact(logical_id, body, task_id=None):
            encoded = json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
            body_hash = hashlib.sha256(encoded).hexdigest()
            (objects / body_hash).write_bytes(encoded)
            return {
                "root_key": "project", "logical_id": logical_id, "version": 1,
                "artifact_ref": f"artifact:{logical_id}@1", "artifact_type": "execution",
                "body_hash": body_hash, "file_ref": f"project::objects/sha256/{body_hash}",
                "task_id": task_id, "created_at": "2026-09-16T00:00:02+00:00",
            }

        running_id = "model-running"
        review_id = "model-review"
        completed_id = "model-completed"
        running_context = artifact(
            f"command/contexts/{running_id}",
            {"role": "research.literature-mapper", "provider_pool": "qwen", "route_id": "qwen-tailnet", "client": {
                "model": "fallback-model", "base_url": "http://127.0.0.1:11434/v1",
                "cache_prompt": True,
                "role_models": {"research.literature-mapper": {
                    "model": "qwen3.8-27b",
                    "base_url": "https://desktop-br7ukeg.taila57d41.ts.net/v1",
                }},
            }},
        )
        completed_context = artifact(
            f"command/contexts/{completed_id}",
            {"role": "review.methods", "client": {
                "model": "fallback-model", "base_url": "http://127.0.0.1:11434/v1",
                "cache_prompt": True,
            }},
        )
        review_context = artifact(
            f"command/contexts/{review_id}",
            {"role": "research.fact-verifier", "client": {
                "model": "review-model", "base_url": "http://127.0.0.1:11434/v1",
            }},
        )
        completed_execution = artifact(
            f"command/executions/{completed_id}",
            {"model": "glm-5.3-flash:cloud", "elapsed_seconds": 2.5,
             "finish_reason": "stop", "usage": {
                 "model_calls": 1, "input_tokens": 100, "output_tokens": 25,
                 "cache_read_tokens": 64,
             }},
        )
        db = {
            "tasks": [
                {"root_key": "project", "task_id": running_id, "state": "running",
                 "updated_at": "2026-09-16T00:00:03+00:00", "payload": {"operation": "model"}},
                {"root_key": "project", "task_id": review_id, "state": "awaiting_review",
                 "updated_at": "2026-09-16T00:00:02+00:00", "payload": {"operation": "model"}},
                {"root_key": "project", "task_id": completed_id, "state": "completed",
                 "updated_at": "2026-09-16T00:00:02+00:00", "payload": {"operation": "model"}},
            ],
            "attempts": [
                {"root_key": "project", "task_id": running_id, "attempt_id": "a-running",
                 "state": "started", "lease_owner": "research.literature-mapper",
                 "created_at": "2026-09-16T00:00:03+00:00", "finished_at": None, "usage": {}},
                {"root_key": "project", "task_id": review_id, "attempt_id": "a-review",
                 "state": "started", "lease_owner": "research.fact-verifier",
                 "created_at": "2026-09-16T00:00:02+00:00", "finished_at": None, "usage": {}},
                {"root_key": "project", "task_id": completed_id, "attempt_id": "a-completed",
                 "state": "succeeded", "lease_owner": "review.methods",
                 "created_at": "2026-09-16T00:00:01+00:00",
                 "finished_at": "2026-09-16T00:00:02+00:00", "usage": {}},
            ],
            "artifacts": [running_context, review_context, completed_context, completed_execution],
        }

        calls = DashboardSnapshot(root)._model_calls(db)

        self.assertEqual(calls["active"], 1)
        self.assertEqual(calls["review_pending"], 1)
        self.assertEqual([item["task_id"] for item in calls["items"]], [running_id])
        self.assertEqual([item["task_id"] for item in calls["review_items"]], [review_id])
        self.assertEqual([item["task_id"] for item in calls["recent_items"]], [completed_id])
        self.assertEqual(calls["items"][0]["model"], "qwen3.8-27b")
        self.assertEqual(calls["items"][0]["provider"], "Tailnet")
        self.assertEqual(calls["items"][0]["provider_pool"], "qwen")
        self.assertEqual(calls["items"][0]["route_id"], "qwen-tailnet")
        self.assertEqual(calls["items"][0]["cache"]["status"], "enabled · unreported")
        self.assertEqual(calls["recent_items"][0]["model"], "glm-5.3-flash:cloud")
        self.assertEqual(calls["recent_items"][0]["response_status"], "response recorded")
        self.assertEqual(calls["recent_items"][0]["cache"], {
            "requested": True, "status": "partial", "read_tokens": 64, "write_tokens": None,
            "read_ratio": 0.64,
        })
        self.assertEqual(calls["recent_items"][0]["usage"]["cache_read_tokens"], 64)
        self.assertTrue(calls["recent_items"][0]["response_ref"].startswith("project::objects/sha256/"))

    def test_snapshot_projects_live_composer_specialist_calls(self):
        temporary, root = self.make_project()
        self.addCleanup(temporary.cleanup)
        snapshot = DashboardSnapshot(root)
        live = {
            "stages": {"survey": {"specialist_live": {
                "research.cataloger": {
                    "event": "dispatched", "execution_mode": "model",
                    "task_id": "specialist-survey-cataloger", "stage_id": "survey",
                    "model": "qwen3.8-27b", "base_url": "https://desktop-br7ukeg.taila57d41.ts.net/v1",
                    "provider_pool": "qwen", "route_id": "qwen-bulk",
                    "cache_prompt": True, "observed_at": "2026-09-16T00:00:03+00:00",
                },
            }}}
        }
        calls = snapshot._model_calls({"tasks": [], "attempts": [], "artifacts": []}, live)

        self.assertEqual(calls["active"], 1)
        self.assertEqual(calls["total"], 1)
        self.assertEqual(calls["live_items"][0]["role"], "research.cataloger")
        self.assertEqual(calls["live_items"][0]["provider"], "Tailnet")
        self.assertEqual(calls["live_items"][0]["response_status"], "awaiting response")
        self.assertEqual(calls["live_items"][0]["cache"]["status"], "enabled · unreported")

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
