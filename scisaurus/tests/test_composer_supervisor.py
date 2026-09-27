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
                def __init__(self, value, *, resume, on_progress, **kwargs):
                    self.additional_seconds = kwargs.get("additional_seconds")

                def run(self):
                    return {"status": "completed", "remaining_seconds": 10,
                            "stages": {}, "blockers": [], "continuation_cycles": 0,
                            "additional_seconds": self.additional_seconds}

            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", FakeRunner):
                result = supervise_composer(
                    workflow, initial_resume=True, initial_additional_seconds=3600,
                    poll_seconds=0.01, process_watchdog=True,
                    watchdog_seconds=30)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["additional_seconds"], 3600)
            state = json.loads((root / "project" / "output" / "supervisor-state.json").read_text())
            self.assertEqual(state["schema_version"], "composer-supervisor-3")
            self.assertEqual(state["status"], "stopped")
            self.assertEqual(state["action"], "stop")

    def test_initial_deadline_extension_is_applied_once_across_watchdog_restarts(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = {"id": "watch-extension-test", "project_id": str(root / "project")}
            results = [
                {"status": "blocked", "remaining_seconds": 20,
                 "stages": {"survey": {"status": "blocked"}},
                 "blockers": [{"stage_id": "survey", "reason": "transient"}]},
                {"status": "completed", "remaining_seconds": 10,
                 "stages": {}, "blockers": []},
            ]
            calls = []

            class FakeRunner:
                def __init__(self, value, *, resume, on_progress, **kwargs):
                    calls.append({"resume": resume,
                                  "additional_seconds": kwargs.get("additional_seconds")})

                def run(self):
                    return results.pop(0)

            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", FakeRunner):
                result = supervise_composer(
                    workflow, initial_resume=True, initial_additional_seconds=86400,
                    poll_seconds=0, process_watchdog=False)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, [
                {"resume": True, "additional_seconds": 86400},
                {"resume": True, "additional_seconds": None},
            ])
            self.assertEqual(len(results), 0)

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

    def test_watchdog_bounds_unrepresentable_remaining_time_by_workflow_deadline(self):
        with tempfile.TemporaryDirectory() as path:
            supervisor = ComposerSupervisor({
                "id": "overflowing-watchdog-time-test",
                "project_id": str(Path(path) / "project"),
                "time_policy": {"hard_seconds": 120},
            })
            snapshot = {"progress": {"remaining_seconds": 10 ** 1000}}

            self.assertEqual(supervisor._watchdog_remaining(snapshot), 120.0)

    def test_admitted_crossref_fallback_resumes_without_waiting_for_openalex_reset(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            project = root / "project"
            supervisor = ComposerSupervisor({
                "id": "admitted-fallback-resume-test",
                "project_id": str(project),
                }, poll_seconds=60)
            result = {
                "status": "paused",
                "remaining_seconds": 3600,
                "blockers": [{
                    "stage_id": "survey",
                    "reason": "provider_cooldown",
                    "provider_error": "OpenAlex daily quota cooldown until reset",
                    "retry_after_seconds": 1800,
                }],
                "context": {"survey": {"provider_fallback": {
                    "status": "admitted",
                    "mode": "crossref_metadata",
                    "source_provider": "openalex",
                }}},
                "stages": {"survey": {"kind": "survey", "status": "paused"}},
            }

            with patch(
                "scisaurus.runtime.composer_supervisor.time.sleep",
                side_effect=AssertionError("approved fallback must not sleep for OpenAlex reset"),
            ):
                self.assertTrue(supervisor._wait_before_resume(result))

            state = json.loads((project / "output" / "supervisor-state.json").read_text())
            self.assertEqual(state["action"], "resume_admitted_provider_fallback")

    def test_historical_survey_fallback_does_not_bypass_active_topic_cooldown(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            supervisor = ComposerSupervisor({
                "id": "historical-fallback-is-scoped-test",
                "project_id": str(root / "project"),
            }, poll_seconds=0)
            result = {
                "status": "paused",
                "remaining_seconds": 60,
                "blockers": [{
                    "stage_id": "survey",
                    "reason": "provider_cooldown",
                    "provider_error": "OpenAlex daily quota cooldown until reset",
                    "retry_after_seconds": 30,
                }],
                "active_blockers": [{
                    "stage_id": "topic",
                    "reason": "provider_cooldown",
                    "provider_error": "OpenAlex daily quota cooldown until reset",
                    "retry_after_seconds": 0.02,
                }],
                "context": {"survey": {"provider_fallback": {
                    "status": "admitted",
                    "mode": "crossref_metadata",
                    "source_provider": "openalex",
                }}},
                "stages": {
                    "survey": {"kind": "survey", "status": "paused"},
                    "topic": {"kind": "topic", "status": "paused"},
                },
            }

            with patch(
                "scisaurus.runtime.composer_supervisor.time.sleep",
                wraps=time.sleep,
            ) as sleeper:
                self.assertTrue(supervisor._wait_before_resume(result))

            sleeper.assert_called()
            state = json.loads((root / "project" / "output" / "supervisor-state.json").read_text())
            self.assertEqual(state["action"], "waiting_to_resume")

    def test_ollama_cooldown_with_incidental_openalex_text_keeps_its_wait(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            supervisor = ComposerSupervisor({
                "id": "unrelated-provider-cooldown-test",
                "project_id": str(root / "project"),
            }, poll_seconds=0)
            result = {
                "status": "paused",
                "remaining_seconds": 60,
                "active_blockers": [{
                    "stage_id": "survey",
                    "reason": "provider_cooldown",
                    "provider_error": "OpenAlex metadata retry is unavailable; Ollama model endpoint returned 429",
                    "retry_after_seconds": 0.02,
                    "rate_limit": {"provider": "ollama", "message": "OpenAlex is also in this report"},
                }],
                "context": {"survey": {"provider_fallback": {
                    "status": "admitted",
                    "mode": "crossref_metadata",
                    "source_provider": "openalex",
                }}},
                "stages": {"survey": {"kind": "survey", "status": "paused"}},
            }

            with patch(
                "scisaurus.runtime.composer_supervisor.time.sleep",
                wraps=time.sleep,
            ) as sleeper:
                self.assertTrue(supervisor._wait_before_resume(result))

            sleeper.assert_called()
            state = json.loads((root / "project" / "output" / "supervisor-state.json").read_text())
            self.assertEqual(state["action"], "waiting_to_resume")

    def test_provider_wait_stops_when_remaining_time_cannot_convert_to_float(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            supervisor = ComposerSupervisor({
                "id": "invalid-remaining-time-test",
                "project_id": str(root / "project"),
            }, poll_seconds=0)
            result = {
                "status": "paused",
                "remaining_seconds": 10 ** 1000,
                "active_blockers": [],
            }

            self.assertFalse(supervisor._wait_before_resume(result))

    def test_malformed_active_blockers_cannot_use_historical_fallback_fast_path(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            supervisor = ComposerSupervisor({
                "id": "malformed-active-blockers-test",
                "project_id": str(root / "project"),
            }, poll_seconds=0)
            result = {
                "status": "paused",
                "remaining_seconds": 60,
                "blockers": [{
                    "stage_id": "survey",
                    "reason": "provider_cooldown",
                    "provider_error": "OpenAlex daily quota cooldown until reset",
                    "retry_after_seconds": 30,
                    "retry_after_epoch": 1000.025,
                }],
                "active_blockers": None,
                "context": {"survey": {"provider_fallback": {
                    "status": "admitted",
                    "mode": "crossref_metadata",
                    "source_provider": "openalex",
                }}},
                "stages": {"survey": {"kind": "survey", "status": "paused"}},
            }
            monotonic = [50.0]
            slept = []

            def fake_sleep(seconds):
                slept.append(seconds)
                monotonic[0] += seconds

            with (
                patch("scisaurus.runtime.composer_supervisor.time.time", return_value=1000.0),
                patch("scisaurus.runtime.composer_supervisor.time.monotonic",
                      side_effect=lambda: monotonic[0]),
                patch("scisaurus.runtime.composer_supervisor.time.sleep", side_effect=fake_sleep),
            ):
                self.assertTrue(supervisor._wait_before_resume(result))

            self.assertAlmostEqual(sum(slept), 0.025, places=6)
            state = json.loads((root / "project" / "output" / "supervisor-state.json").read_text())
            self.assertEqual(state["action"], "waiting_to_resume")

    def test_openalex_cooldown_without_admitted_fallback_keeps_retry_wait(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            supervisor = ComposerSupervisor({
                "id": "unadmitted-fallback-wait-test",
                "project_id": str(root / "project"),
                }, poll_seconds=0)
            result = {
                "status": "paused",
                "remaining_seconds": 10,
                "blockers": [{
                    "stage_id": "survey",
                    "reason": "provider_cooldown",
                    "provider_error": "OpenAlex daily quota cooldown until reset",
                    "retry_after_seconds": 0.02,
                }],
                "context": {},
                "stages": {"survey": {"kind": "survey", "status": "paused"}},
            }

            with patch(
                "scisaurus.runtime.composer_supervisor.time.sleep",
                wraps=time.sleep,
            ) as sleeper:
                self.assertTrue(supervisor._wait_before_resume(result))

            sleeper.assert_called()

    def test_provider_wait_uses_active_blocker_epoch_not_stale_history_delay(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            supervisor = ComposerSupervisor({
                "id": "active-provider-retry-test",
                "project_id": str(root / "project"),
            }, poll_seconds=0)
            result = {
                "status": "paused",
                "remaining_seconds": 3600,
                "blockers": [{
                    "stage_id": "survey",
                    "reason": "provider_cooldown",
                    "retry_after_seconds": 15,
                    "retry_after_epoch": 1015.0,
                }],
                "active_blockers": [{
                    "stage_id": "topic",
                    "reason": "provider_cooldown",
                    "retry_after_seconds": 9,
                    "retry_after_epoch": 1000.025,
                }],
                "context": {},
                "stages": {"topic": {"kind": "topic", "status": "paused"}},
            }
            monotonic = [50.0]
            slept = []

            def fake_sleep(seconds):
                slept.append(seconds)
                monotonic[0] += seconds

            with (
                patch("scisaurus.runtime.composer_supervisor.time.time", return_value=1000.0),
                patch("scisaurus.runtime.composer_supervisor.time.monotonic",
                      side_effect=lambda: monotonic[0]),
                patch("scisaurus.runtime.composer_supervisor.time.sleep", side_effect=fake_sleep),
            ):
                self.assertTrue(supervisor._wait_before_resume(result))

            self.assertAlmostEqual(sum(slept), 0.025, places=6)

    def test_provider_wait_tolerates_null_legacy_blocker_list(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            supervisor = ComposerSupervisor({
                "id": "null-legacy-blockers-test",
                "project_id": str(root / "project"),
            }, poll_seconds=0)
            result = {
                "status": "paused",
                "remaining_seconds": 10,
                "blockers": None,
                "context": {},
                "stages": {},
            }
            with patch(
                "scisaurus.runtime.composer_supervisor.time.sleep",
                side_effect=AssertionError("empty legacy blocker list must not wait"),
            ):
                self.assertTrue(supervisor._wait_before_resume(result))

    def test_provider_wait_ignores_numeric_values_that_overflow_float(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            supervisor = ComposerSupervisor({
                "id": "malformed-provider-retry-test",
                "project_id": str(root / "project"),
            }, poll_seconds=0)
            result = {
                "status": "paused",
                "remaining_seconds": 3600,
                "active_blockers": [{
                    "stage_id": "topic",
                    "reason": "provider_cooldown",
                    "retry_after_epoch": 10 ** 1000,
                    "retry_after_seconds": 0.025,
                }, {
                    "stage_id": "survey",
                    "reason": "provider_cooldown",
                    "retry_after_seconds": 10 ** 1000,
                }],
                "context": {},
                "stages": {"topic": {"kind": "topic", "status": "paused"}},
            }
            monotonic = [50.0]
            slept = []

            def fake_sleep(seconds):
                slept.append(seconds)
                monotonic[0] += seconds

            with (
                patch("scisaurus.runtime.composer_supervisor.time.time", return_value=1000.0),
                patch("scisaurus.runtime.composer_supervisor.time.monotonic",
                      side_effect=lambda: monotonic[0]),
                patch("scisaurus.runtime.composer_supervisor.time.sleep", side_effect=fake_sleep),
            ):
                self.assertTrue(supervisor._wait_before_resume(result))

            self.assertAlmostEqual(sum(slept), 0.025, places=6)

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
