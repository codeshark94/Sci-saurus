import tempfile
import unittest
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.composer_supervisor import ComposerSupervisor, supervise_composer


class ComposerSupervisorTests(unittest.TestCase):
    def test_supervisor_rejects_a_second_owner_of_the_same_project(self):
        with tempfile.TemporaryDirectory() as path:
            project = Path(path) / "project"
            first = ComposerSupervisor({"id": "lock-test", "project_id": str(project)})
            second = ComposerSupervisor({"id": "lock-test", "project_id": str(project)})
            try:
                first._acquire_project_lock()
                with self.assertRaises(ValidationError):
                    second._acquire_project_lock()
            finally:
                first._release_project_lock()
                second._release_project_lock()

    def test_process_watchdog_returns_child_result(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = {"id": "watch-process-test", "project_id": str(root / "project")}

            class FakeRunner:
                def __init__(self, value, *, resume, on_progress):
                    pass

                def run(self):
                    return {"status": "completed", "remaining_seconds": 10,
                            "stages": {}, "blockers": [], "continuation_cycles": 0}

            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", FakeRunner):
                result = supervise_composer(
                    workflow, poll_seconds=0.01, process_watchdog=True,
                    watchdog_seconds=30)

            self.assertEqual(result["status"], "completed")
            state = json.loads((root / "project" / "output" / "supervisor-state.json").read_text())
            self.assertEqual(state["schema_version"], "composer-supervisor-3")

    def test_heartbeat_only_does_not_count_as_semantic_progress(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            project = root / "project"
            stage = project / "stages" / "survey"
            (project / "output").mkdir(parents=True)
            (project / "state").mkdir(parents=True)
            (stage / "output").mkdir(parents=True)
            (stage / "state").mkdir(parents=True)
            progress = {
                "phase": "survey:running", "status": "running", "state_revision": 3,
                "continuation_cycles": 0, "usage": {"model_calls": 1},
                "stages": {"survey": {"status": "running", "attempt_count": 1,
                                         "project_dir": str(stage)}},
            }
            (project / "output" / "progress.json").write_text(json.dumps(progress))
            (stage / "output" / "progress.json").write_text(json.dumps({
                "phase": "executing", "checkpoint": 4, "active_tasks": ["task-1"],
                "information_changes": [], "verified_changes": [], "blockers": [],
            }))
            for database in (project / "state" / "control.sqlite", stage / "state" / "control.sqlite"):
                connection = sqlite3.connect(database)
                connection.executescript(
                    "CREATE TABLE events (seq INTEGER);"
                    "CREATE TABLE tasks (task_id TEXT, state TEXT, updated_at TEXT);"
                )
                connection.execute("INSERT INTO events VALUES (1)")
                connection.commit()
                connection.close()
            supervisor = ComposerSupervisor({"id": "signal-test", "project_id": str(project)})
            first = supervisor._live_snapshot()
            (project / "output" / "progress.json").write_text(json.dumps(progress) + "\n")
            second = supervisor._live_snapshot()
            self.assertEqual(first["signature"], second["signature"])
            progress["state_revision"] = 4
            (project / "output" / "progress.json").write_text(json.dumps(progress))
            third = supervisor._live_snapshot()
            self.assertNotEqual(second["signature"], third["signature"])

    def test_scheduled_provider_retry_is_not_killed_as_watchdog_stall(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            project = root / "project"
            (project / "output").mkdir(parents=True)
            (project / "output" / "progress.json").write_text(json.dumps({
                "phase": "survey:retry_wait",
                "status": "running",
                "state_revision": 8,
                "retry_schedule": {
                    "survey": {"not_before_epoch": time.time() + 3600}
                },
                "stages": {},
            }))
            supervisor = ComposerSupervisor({
                "id": "scheduled-wait-test",
                "project_id": str(project),
            })
            snapshot = supervisor._live_snapshot()
            self.assertTrue(supervisor._scheduled_retry_wait(snapshot))

    def test_interrupted_checkpoint_is_not_left_visibly_running(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            project = root / "project"
            output = project / "output"
            output.mkdir(parents=True)
            for name in ("progress.json", "run.json", "interim_report.json"):
                (output / name).write_text(json.dumps({
                    "schema_version": "fixture",
                    "status": "running",
                    "blockers": [],
                }))
            supervisor = ComposerSupervisor({
                "id": "interrupt-checkpoint-test",
                "project_id": str(project),
            })
            supervisor._mark_interrupted_checkpoint()
            for name in ("progress.json", "run.json", "interim_report.json"):
                value = json.loads((output / name).read_text())
                self.assertEqual(value["status"], "paused")
                self.assertTrue(any(
                    item.get("stop_reason") == "process_interrupted"
                    for item in value["blockers"]
                ))

    def test_restarts_from_durable_state_after_recoverable_exit(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = {"id": "watch-test", "project_id": str(root / "project")}
            results = [
                {
                    "status": "blocked", "remaining_seconds": 20,
                    "stages": {"topic": {"status": "blocked", "error": "scientific blocker"}},
                    "blockers": [{"stage_id": "topic", "reason": "scientific blocker"}],
                    "continuation_cycles": 1,
                },
                {"status": "completed", "remaining_seconds": 10,
                 "stages": {}, "blockers": [], "continuation_cycles": 2},
            ]
            calls = []

            class FakeRunner:
                def __init__(self, value, *, resume, on_progress):
                    calls.append(resume)

                def run(self):
                    return results.pop(0)

            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", FakeRunner):
                result = supervise_composer(
                    workflow, poll_seconds=0, process_watchdog=False)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, [False, True])
            self.assertEqual(len(results), 0)
            self.assertEqual(
                ComposerSupervisor(workflow, poll_seconds=0)._should_resume(
                    {"status": "paused", "remaining_seconds": 0}),
                False,
            )

    def test_child_keyboard_interrupt_is_terminal_for_supervisor(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = {"id": "interrupt-test", "project_id": str(root / "project")}
            supervisor = ComposerSupervisor(workflow, poll_seconds=0)
            message = {"kind": "exception", "type": "KeyboardInterrupt",
                       "error": "termination requested"}
            with patch.object(supervisor, "_write_state") as write_state:
                with self.assertRaises(KeyboardInterrupt):
                    supervisor._child_exception_text(message)
            write_state.assert_called_once_with(
                child_status="interrupted", action="stop", error="termination requested")

    def test_validation_error_is_a_terminal_workflow_stop(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = {"id": "validation-stop-test", "project_id": str(root / "project")}

            class InvalidRunner:
                def __init__(self, value, *, resume, on_progress):
                    pass

                def run(self):
                    raise ValidationError("workflow artifact is invalid")

            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", InvalidRunner):
                result = supervise_composer(
                    workflow, poll_seconds=0, process_watchdog=False)

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["stop_reason"], "workflow_validation")
            self.assertEqual(result["blockers"][0]["recoverable"], False)


if __name__ == "__main__":
    unittest.main()
