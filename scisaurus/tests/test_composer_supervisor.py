from contextlib import closing
import tempfile
import unittest
import json
import sqlite3
import time
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.composer_supervisor import ComposerSupervisor, supervise_composer



class _FixtureRunner:
    def close(self):
        pass


class SignalFixtureRunner(_FixtureRunner):
    def __init__(self, workflow, **kwargs):
        self.root = Path(workflow["project_id"])
        self.ignore = workflow["fixture_ignore_sigterm"]

    def run(self):
        if self.ignore:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            (self.root / "worker.pid").write_text(str(worker.pid))
            (self.root / "child.pid").write_text(str(os.getpid()))
            time.sleep(60)
            return {"status": "completed"}
        finally:
            worker.terminate()
            worker.wait(timeout=5)


class InitializingFixtureRunner(_FixtureRunner):
    def __init__(self, workflow, **kwargs):
        time.sleep(0.4)
        self.progress = Path(workflow["project_id"]) / "output/progress.json"
        self.deadline = workflow["fixture_deadline"] + kwargs["additional_seconds"]

    def run(self):
        result = {"status": "completed", "deadline_at_epoch": self.deadline,
                  "remaining_seconds": self.deadline - time.time()}
        self.progress.write_text(json.dumps(result))
        return result


class SlowFixtureRunner(_FixtureRunner):
    def __init__(self, workflow, **kwargs):
        self.delay = workflow.get("fixture_delay", 0.75)

    def run(self):
        time.sleep(self.delay)
        return {"status": "completed", "remaining_seconds": 60}


class CompletedFixtureRunner(_FixtureRunner):
    def __init__(self, value, *, resume, on_progress, **kwargs):
        self.stop_after_stage = kwargs.get("stop_after_stage")
        self.additional_seconds = kwargs.get("additional_seconds")
        self.on_progress = on_progress
        self.root = Path(value["project_id"])
        self.delay = value.get("fixture_delay", 0)

    def run(self):
        from scisaurus.runtime.models import _MODEL_PROVIDER_COOLDOWN_LOCK
        acquired = _MODEL_PROVIDER_COOLDOWN_LOCK.acquire(timeout=0.1)
        if acquired:
            _MODEL_PROVIDER_COOLDOWN_LOCK.release()
        self.on_progress({"fixture_pid": os.getpid(), "fresh_model_lock": acquired})
        time.sleep(self.delay)
        return {"status": "completed", "remaining_seconds": 10, "stages": {}, "blockers": [],
                "continuation_cycles": 0, "additional_seconds": self.additional_seconds,
                "stop_after_stage": self.stop_after_stage}


class ComposerSupervisorTests(unittest.TestCase):
    def test_stage_boundary_is_forwarded_in_inline_and_spawn_paths(self):
        for watchdog in (False, True):
            with self.subTest(watchdog=watchdog), tempfile.TemporaryDirectory() as path:
                workflow = {"id": "boundary-wire", "project_id": path,
                            "stages": [{"id": "topic"}]}
                with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", CompletedFixtureRunner):
                    result = supervise_composer(workflow, stop_after_stage="topic",
                                                process_watchdog=watchdog, poll_seconds=0)
                self.assertEqual(result["stop_after_stage"], "topic")

    def test_stage_boundary_stops_before_recoverable_requests(self):
        supervisor = ComposerSupervisor({"project_id": "/unused"})
        base = {"status": "paused", "remaining_seconds": 100,
                "active_research_requests": [{"objective": "Resolve evidence"}]}
        blocker = {"stop_reason": "operator_stage_boundary", "recoverable": True}
        self.assertFalse(supervisor._should_resume({**base, "active_blockers": [blocker]}))
        self.assertFalse(supervisor._should_resume({**base, "active_blockers": [],
                         "interim_report": {"stop_reason": "operator_stage_boundary"}}))
        self.assertTrue(supervisor._should_resume({**base, "active_blockers": [],
                                                  "blockers": [blocker]}))

    def test_unknown_stage_boundary_rejected_without_state_creation(self):
        with tempfile.TemporaryDirectory() as path:
            project = Path(path) / "unused"
            for target in ("unknown", 1, ""):
                with self.subTest(target=target), self.assertRaises(ValidationError):
                    ComposerSupervisor({"project_id": str(project), "stages": [{"id": "topic"}]},
                                       stop_after_stage=target)
            self.assertFalse(project.exists())

    def test_cli_forwards_stage_boundary_in_both_execution_modes(self):
        from scisaurus.cli import main
        with tempfile.TemporaryDirectory() as path:
            workflow = Path(path) / "workflow.json"
            workflow.write_text(json.dumps({"id": "cli-wire", "project_id": path}))
            result = {"status": "paused", "elapsed_seconds": 1,
                      "stages": {}, "release_status": "held"}
            for watch in (False, True):
                with self.subTest(watch=watch), patch("builtins.print"), \
                        patch("scisaurus.runtime.composer.ComposerRunner") as runner, \
                        patch("scisaurus.runtime.composer_supervisor.supervise_composer", return_value=result) as supervise:
                    runner.return_value.run.return_value = result
                    args = ["run-composer", "--workflow", str(workflow), "--stop-after-stage", "topic"]
                    if watch:
                        args.append("--watch")
                    self.assertEqual(main(args), 3)
                    call = supervise.call_args if watch else runner.call_args
                    self.assertEqual(call.kwargs["stop_after_stage"], "topic")

    def test_spawn_does_not_inherit_thread_locks_and_calls_progress_in_parent(self):
        from scisaurus.runtime.models import _MODEL_PROVIDER_COOLDOWN_LOCK
        with tempfile.TemporaryDirectory() as path:
            held, release = threading.Event(), threading.Event()

            def hold():
                with _MODEL_PROVIDER_COOLDOWN_LOCK:
                    held.set()
                    release.wait(10)

            thread = threading.Thread(target=hold)
            thread.start()
            self.assertTrue(held.wait(2))
            events = []
            supervisor = ComposerSupervisor({"project_id": path}, poll_seconds=0,
                on_progress=lambda state: events.append((os.getpid(), state)))
            try:
                with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", CompletedFixtureRunner):
                    self.assertEqual(supervisor._run_one_process(False)["status"], "completed")
            finally:
                release.set()
                thread.join(timeout=2)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][0], os.getpid())
            self.assertNotEqual(events[0][1]["fixture_pid"], os.getpid())
            self.assertTrue(events[0][1]["fresh_model_lock"])

    def test_progress_callback_failure_stops_owned_child(self):
        with tempfile.TemporaryDirectory() as path:
            observed = []

            def fail(state):
                observed.append(state["fixture_pid"])
                raise RuntimeError("progress callback failed")

            supervisor = ComposerSupervisor({"project_id": path, "fixture_delay": 60},
                                            poll_seconds=0, on_progress=fail)
            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", CompletedFixtureRunner):
                with self.assertRaisesRegex(RuntimeError, "progress callback failed"):
                    supervisor._run_one_process(False)
            self.assertEqual(len(observed), 1)
            with self.assertRaises(ProcessLookupError):
                os.kill(observed[0], 0)

    def test_diagnostic_database_failures_close_readers_without_creating_ledgers(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            (root / "state").mkdir()
            supervisor = ComposerSupervisor({"project_id": path})
            self.assertIsNone(supervisor._stage_progress_signature(path))
            self.assertFalse((root / "state/control.sqlite").exists())
            with closing(sqlite3.connect(root / "state/control.sqlite")) as connection:
                connection.execute("CREATE TABLE scaffold (id INTEGER)")
            acquired = []
            connect = sqlite3.connect

            def track(*args, **kwargs):
                connection = connect(*args, **kwargs)
                acquired.append(connection)
                return connection

            with patch("scisaurus.runtime.composer_supervisor.sqlite3.connect", side_effect=track):
                self.assertIsNone(supervisor._stage_progress_signature(path))
                supervisor._live_snapshot()
            self.assertEqual(len(acquired), 2)
            for connection in acquired:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
    def test_inventory_failure_still_terminates_child_and_reports_unverified_descendants(self):
        child = Mock()
        child.is_alive.return_value = True
        with patch.object(ComposerSupervisor, "_process_tree", side_effect=TimeoutError("inventory timeout")):
            with self.assertRaisesRegex(ValidationError, "descendant cleanup could not be verified"):
                ComposerSupervisor._stop_child(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.join.call_count, 2)

    def test_sigterm_stops_child_restores_handler_and_releases_project_lock(self):
        for ignores_termination in (False, True):
            with self.subTest(ignores_termination=ignores_termination), tempfile.TemporaryDirectory() as path:
                project = Path(path)
                pid_path = project / "child.pid"
                worker_path = project / "worker.pid"
                previous_handler = signal.getsignal(signal.SIGTERM)


                def request_stop():
                    until = time.monotonic() + 5
                    while not pid_path.is_file() and time.monotonic() < until:
                        time.sleep(0.01)
                    if pid_path.is_file():
                        os.kill(os.getpid(), signal.SIGTERM)

                supervisor = ComposerSupervisor({"id": "signal-test", "project_id": path, "fixture_ignore_sigterm": ignores_termination},
                                                poll_seconds=0.01)
                sender = threading.Thread(target=request_stop)
                sender.start()
                started = time.monotonic()
                with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", SignalFixtureRunner):
                    with self.assertRaisesRegex(KeyboardInterrupt, "termination requested"):
                        supervisor.run()
                sender.join(timeout=6)
                self.assertLess(time.monotonic() - started, 12)
                self.assertFalse(sender.is_alive())
                self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)
                with self.assertRaises(ProcessLookupError):
                    os.kill(int(pid_path.read_text()), 0)
                worker_pid = int(worker_path.read_text())
                until = time.monotonic() + 2
                while time.monotonic() < until:
                    worker_state = subprocess.run(["ps", "-o", "stat=", "-p", str(worker_pid)],
                                                  capture_output=True, text=True).stdout.strip()
                    if not worker_state or worker_state.startswith("Z"):
                        break
                    time.sleep(0.01)
                self.assertTrue(not worker_state or worker_state.startswith("Z"), worker_state)
                other = ComposerSupervisor({"project_id": path})
                try:
                    other._acquire_project_lock()
                finally:
                    other._release_project_lock()

    def test_authorized_extension_survives_expired_initial_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            project = Path(path)
            (project / "output").mkdir()
            progress = project / "output" / "progress.json"
            old_deadline = time.time() - 1
            progress.write_text(json.dumps({"status": "paused", "remaining_seconds": 0,
                                           "deadline_at_epoch": old_deadline}))


            supervisor = ComposerSupervisor({"project_id": path, "fixture_deadline": old_deadline}, poll_seconds=0)
            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", InitializingFixtureRunner):
                result = supervisor._run_one_process(True, additional_seconds=60)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["deadline_at_epoch"], old_deadline + 60)

    def test_legacy_aggregate_lease_is_bounded_by_stage_admission_time(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as path:
            project = Path(path)
            (project / "state").mkdir()
            admitted = time.time() - 120
            with closing(sqlite3.connect(project / "state" / "control.sqlite")) as connection, connection:
                connection.executescript(
                    "CREATE TABLE events (seq INTEGER);"
                    "CREATE TABLE attempts (task_id TEXT, state TEXT, created_at TEXT, lease_expiry REAL, payload_json TEXT);"
                )
                connection.execute("INSERT INTO attempts VALUES ('aggregate', 'started', ?, ?, ?)", (
                    datetime.fromtimestamp(admitted, timezone.utc).isoformat(), time.time() + 3600,
                    json.dumps({"stage_id": "experiment"})))
            supervisor = ComposerSupervisor({"project_id": str(project), "stages": [
                {"id": "experiment", "deadline_seconds": 60}]})
            snapshot = supervisor._live_snapshot()
            self.assertEqual(snapshot["leased_attempts"], [])
            self.assertAlmostEqual(snapshot["active_attempts"][0]["lease_expiry"], admitted + 60, delta=0.001)

    def test_only_unexpired_leases_protect_started_attempts(self):
        with tempfile.TemporaryDirectory() as path:
            project = Path(path)
            (project / "state").mkdir()
            with closing(sqlite3.connect(project / "state" / "control.sqlite")) as connection, connection:
                connection.executescript(
                    "CREATE TABLE events (seq INTEGER);"
                    "CREATE TABLE attempts (task_id TEXT, state TEXT, created_at TEXT, lease_expiry REAL, payload_json TEXT);"
                )
                connection.executemany("INSERT INTO attempts VALUES (?, 'started', '', ?, '{}')", [
                    ("expired", time.time() - 3600),
                    ("leased", time.time() + 3600),
                    ("missing-lease", None),
                ])
            supervisor = ComposerSupervisor({"project_id": str(project)})
            snapshot = supervisor._live_snapshot()
            self.assertEqual({a["task_id"] for a in snapshot["active_attempts"]},
                             {"expired", "leased", "missing-lease"})
            self.assertEqual([a["task_id"] for a in snapshot["leased_attempts"]], ["leased"])

    def test_process_watchdog_spares_valid_lease_and_bounds_expired_lease(self):
        with tempfile.TemporaryDirectory() as path:

            supervisor = ComposerSupervisor({"project_id": path}, poll_seconds=0)
            supervisor.watchdog_seconds = 0.05
            attempt = {"task_id": "provider-1", "lease_expiry": time.time() + 60}
            snapshot = {"signature": (1,), "progress": {"remaining_seconds": 60},
                        "active_attempts": [attempt], "leased_attempts": [attempt]}
            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", SlowFixtureRunner), \
                    patch.object(supervisor, "_live_snapshot", return_value=snapshot):
                self.assertEqual(supervisor._run_one_process(True)["status"], "completed")
                snapshot["leased_attempts"] = []
                attempt["lease_expiry"] = time.time() - 60
                result = supervisor._run_one_process(True)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["blockers"][0]["active_attempts"], ["provider-1"])
            self.assertTrue(supervisor._should_resume(result))

    def test_persisted_deadline_stops_hung_child_even_with_live_lease(self):
        with tempfile.TemporaryDirectory() as path:

            supervisor = ComposerSupervisor({"project_id": path, "fixture_delay": 10}, poll_seconds=0)
            attempt = {"task_id": "provider-1", "lease_expiry": time.time() + 60}
            snapshot = {"signature": (1,), "progress": {
                "remaining_seconds": 60, "deadline_at_epoch": time.time() + 0.1},
                "active_attempts": [attempt], "leased_attempts": [attempt]}
            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", SlowFixtureRunner), \
                    patch.object(supervisor, "_live_snapshot", return_value=snapshot):
                result = supervisor._run_one_process(True)
            self.assertEqual(result["remaining_seconds"], 0)
            self.assertEqual(result["blockers"][0]["reason"], "hard_deadline")
            self.assertFalse(supervisor._should_resume(result))

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


            with patch("scisaurus.runtime.composer_supervisor.ComposerRunner", CompletedFixtureRunner):
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
                 "blockers": [{"stage_id": "survey", "reason": "transient"}],
                 "active_research_requests": [{
                     "id": "survey-repair-1", "kind": "literature_expansion",
                     "objective": "Narrow the evidence search to the unresolved comparator.",
                     "target_stage_id": "survey",
                 }]},
                {"status": "completed", "remaining_seconds": 10,
                 "stages": {}, "blockers": []},
            ]
            calls = []

            class FakeRunner(_FixtureRunner):
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

    def test_provider_429_stops_supervisor_before_pending_requests_or_fallback(self):
        with tempfile.TemporaryDirectory() as path:
            supervisor = ComposerSupervisor({"id": "provider-rate-limit-stop-test", "project_id": str(Path(path) / "project")})
            base = {"status": "paused", "remaining_seconds": 3600,
                    "active_research_requests": [{"objective": "Review retained literature"}]}
            for blocker in (
                {"reason": "source quota exhausted", "stop_reason": "provider_rate_limit",
                 "provider": "full_text", "details": {"metadata": {"http_status": 429}}},
                {"reason": "provider_cooldown", "rate_limit": {"provider": "openalex", "status_code": 429}},
            ):
                with self.subTest(blocker=blocker):
                    self.assertFalse(supervisor._should_resume({**base, "active_blockers": [blocker]}))
                    self.assertTrue(supervisor._should_resume({**base, "active_blockers": [], "blockers": [blocker]}))
            self.assertFalse(supervisor._should_resume({**base, "stop_reason": "provider_rate_limit", "active_blockers": []}))

    def test_operational_state_stops_supervisor_before_pending_requests(self):
        with tempfile.TemporaryDirectory() as path:
            supervisor = ComposerSupervisor({"id": "state-stop-test", "project_id": str(Path(path) / "project")})
            base = {"status": "paused", "remaining_seconds": 3600,
                    "active_research_requests": [{"objective": "Review retained literature"}]}
            blocker = {"stage_id": "survey", "reason": "Dependency ownership mismatch",
                       "stop_reason": "operational_state"}
            self.assertFalse(supervisor._should_resume({**base, "active_blockers": [blocker]}))
            self.assertFalse(supervisor._should_resume({**base, "stop_reason": "operational_state"}))
            self.assertTrue(supervisor._should_resume({**base, "active_blockers": [], "blockers": [blocker]}))

    def test_model_429_stops_supervisor_without_replaying_the_mission(self):
        with tempfile.TemporaryDirectory() as path:
            supervisor = ComposerSupervisor({
                "id": "model-rate-limit-stop-test",
                "project_id": str(Path(path) / "project"),
            })
            limited = {
                "status": "paused",
                "remaining_seconds": 3600,
                "active_blockers": [{
                    "reason": "provider_cooldown",
                    "rate_limit": {
                        "provider": "model",
                        "status_code": 429,
                        "provider_error_kind": "quota_exhausted",
                    },
                }],
                "blockers": [],
            }
            self.assertFalse(supervisor._should_resume(limited))

            expired_cooldown = {
                **limited,
                "active_blockers": [{
                    **limited["active_blockers"][0],
                    "retry_after_epoch": time.time() - 1,
                    "retry_after_seconds": 3600,
                }],
            }
            self.assertFalse(supervisor._should_resume(expired_cooldown))

            active_cooldown = {
                **limited,
                "active_blockers": [{
                    **limited["active_blockers"][0],
                    "retry_after_epoch": time.time() + 3600,
                }],
            }
            self.assertFalse(supervisor._should_resume(active_cooldown))

            resolved_historical = {
                **limited,
                "active_blockers": [],
                "blockers": limited["active_blockers"],
                "stop_reason": "provider_cooldown",
            }
            self.assertFalse(supervisor._should_resume(resolved_historical))

    def test_candidate_with_an_active_scoped_repair_resumes_same_mission(self):
        with tempfile.TemporaryDirectory() as path:
            supervisor = ComposerSupervisor({
                "id": "candidate-repair-resume-test",
                "project_id": str(Path(path) / "project"),
            })
            self.assertFalse(supervisor._should_resume({
                "status": "candidate_needs_review",
                "remaining_seconds": 3600,
                "active_research_requests": [],
            }))
            self.assertTrue(supervisor._should_resume({
                "status": "candidate_needs_review",
                "remaining_seconds": 3600,
                "active_research_requests": [{
                    "id": "repair-result", "kind": "additional_experiment",
                    "objective": "Run the preregistered comparator sensitivity check.",
                    "target_stage_id": "experiment",
                }],
            }))

    def test_supervisor_stops_a_blocked_stage_without_an_actionable_order(self):
        supervisor = ComposerSupervisor({
            "id": "no-work-order-stop-test", "project_id": "/tmp/no-work-order-stop",
        }, poll_seconds=0)
        self.assertFalse(supervisor._should_resume({
            "status": "blocked", "remaining_seconds": 3600,
            "stages": {"argument": {
                "status": "blocked", "attempt_count": 11,
                "error": "research argument did not finish normally: length",
            }},
            "blockers": [{"stage_id": "argument", "reason": "same failure"}],
            "active_research_requests": [],
        }))

    def test_supervisor_resumes_durable_blocking_review_after_truncated_patch(self):
        supervisor = ComposerSupervisor({
            "id": "durable-scientific-repair-resume",
            "project_id": "/tmp/durable-scientific-repair-resume",
        })
        finding = {
            "severity": "blocking",
            "finding": "The proposed branch contrast is algebraically imposed.",
            "evidence": "The branch equation fixes the reported slope difference.",
            "required_change": "Derive a discriminating mechanism and independent test.",
        }
        experiment = {
            "status": "blocked",
            "failure_class": "model_contract",
            "failure_dossier_ref": (
                "artifact:command/composer/failure-recovery/experiment/attempt-9@1"),
            "format_recovery_dispatched": True,
            "error": (
                "capability foundry did not admit a program: program author response "
                "was incomplete (finish_reason=length): invalid JSON"),
            "capability_id": "current-capability",
            "project_dir": "/tmp/durable-scientific-repair-resume/attempt-9",
            "failure_recovery": {
                "failure_class": "model_contract",
                "recovery_mode": "format_repair_then_rerun",
                "dossier_ref": (
                    "artifact:command/composer/failure-recovery/experiment/attempt-9@1"),
                "model_diagnostics": {"repair_ledger": [{
                    "validation_feedback": {
                        "gate": "adversarial_review",
                        "decision": "rejected",
                        "findings": [finding],
                    },
                }]},
            },
        }
        failed_stage = {
            "status": "blocked",
            "attempts": [{
                "state": "unknown", "attempt_number": 341, "cycle": 684,
                "project_dir": "/tmp/durable-scientific-repair-resume/attempt-341",
            }, {
                "state": "failed", "failure_class": "model_contract",
                "attempt_number": 462,
                "project_dir": "/tmp/durable-scientific-repair-resume/attempt-9",
            }],
        }
        experiment["unresolved_prior_attempts"] = [{
            "attempt_id": "experiment-cycle-684-attempt-341",
            "attempt_number": 341, "cycle": 684, "state": "unknown",
            "project_dir": "/tmp/durable-scientific-repair-resume/attempt-341",
        }]
        result = {
            "status": "blocked", "remaining_seconds": 3600,
            "context": {"experiment": experiment},
            "stages": {"experiment": failed_stage},
            "active_research_requests": [], "active_blockers": [],
        }

        self.assertTrue(supervisor._should_resume(result))
        self.assertFalse(supervisor._should_resume({
            **result,
            "active_blockers": [{"reason": "provider_cooldown", "rate_limit": {
                "provider": "model", "status_code": 429,
                "provider_error_kind": "quota_exhausted",
            }}],
        }))
        supervisor.identical_exit_count = 1
        self.assertTrue(supervisor._should_resume(result))
        self.assertFalse(supervisor._should_resume({
            "status": "blocked", "remaining_seconds": 3600,
            "active_research_requests": [], "active_blockers": [],
        }))
        supervisor.identical_exit_count = 0

        malformed = json.loads(json.dumps(result))
        malformed["context"]["experiment"]["failure_recovery"][
            "model_diagnostics"]["repair_ledger"][0]["validation_feedback"][
                "findings"][0]["required_change"] = " "
        self.assertFalse(supervisor._should_resume(malformed))

        executed = json.loads(json.dumps(result))
        executed["context"]["experiment"]["metrics"] = [{
            "id": "slope_difference", "value": -0.2,
        }]
        self.assertFalse(supervisor._should_resume(executed))

    def test_supervisor_uses_topic_capability_not_study_id_for_result_veto(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            attempt_dir = root / "attempt-9"
            package = attempt_dir / "output" / "results-package" / "results-package.json"
            package.parent.mkdir(parents=True)
            package.write_text(json.dumps({
                "capability_id": "cap-b", "study_id": "study-b",
                "observations": [{"value": 0.7}],
            }))
            workflow = {
                "id": "topic-capability-result-veto",
                "project_id": str(root / "project"),
                "stages": [
                    {"id": "topic", "kind": "topic_discovery", "depends_on": []},
                    {"id": "experiment", "kind": "experiment", "depends_on": ["topic"]},
                ],
            }
            supervisor = ComposerSupervisor(workflow)
            result = {
                "status": "blocked", "remaining_seconds": 3600,
                "active_research_requests": [], "active_blockers": [],
                "context": {
                    "topic": {"topic": {
                        "experiment_capability_id": "cap-b",
                        "research_question": "Current lineage question",
                    }},
                    "experiment": {
                        "status": "blocked", "failure_class": "model_contract",
                        "failure_dossier_ref": (
                            "artifact:command/composer/failure-recovery/experiment/attempt-9@1"),
                        "format_recovery_dispatched": True,
                        "error": (
                            "capability foundry did not admit a program: program author response "
                            "was incomplete (finish_reason=length): invalid JSON"),
                        "study_id": "study-b",
                        "project_dir": str(root / "older-attempt"),
                        "results_package": "output/results-package/results-package.json",
                        "failure_recovery": {
                            "failure_class": "model_contract",
                            "recovery_mode": "format_repair_then_rerun",
                            "dossier_ref": (
                                "artifact:command/composer/failure-recovery/experiment/attempt-9@1"),
                            "model_diagnostics": {"repair_ledger": [{
                                "validation_feedback": {
                                    "gate": "adversarial_review", "decision": "rejected",
                                    "findings": [{
                                        "severity": "blocking",
                                        "finding": "The claim is fixed by the equation.",
                                        "evidence": "The down branch adds the only difference.",
                                        "required_change": "Use an independent discriminating derivation.",
                                    }],
                                },
                            }]},
                        },
                    },
                },
                "stages": {"experiment": {
                    "status": "blocked",
                    "attempts": [{
                        "state": "failed", "failure_class": "model_contract",
                        "attempt_number": 9, "project_dir": str(attempt_dir),
                    }],
                }},
            }

            self.assertFalse(supervisor._should_resume(result))

    def test_current_provider_failure_resumes_actionable_work_despite_historical_quota(self):
        supervisor = ComposerSupervisor({
            "id": "historical-quota-does-not-stop-current-work",
            "project_id": "/tmp/historical-quota-does-not-stop-current-work",
        }, poll_seconds=0)
        current_provider_failure = {
            "stage_id": "survey", "attempt_number": 388,
            "reason": "OpenAlex returned HTTP 504 (query_timeout)",
            "failure_class": "provider_error",
        }
        stale_quota = {
            "stage_id": "survey", "stop_reason": "stage_quota_exhausted",
            "recovery": "superseded_by_current_stage_state",
        }
        self.assertTrue(supervisor._should_resume({
            "status": "blocked",
            "remaining_seconds": 3600,
            "interim_report": {"stop_reason": "blocked"},
            "active_blockers": [current_provider_failure],
            "blockers": [stale_quota, current_provider_failure],
            "active_research_requests": [{
                "id": "survey-repair",
                "objective": "Continue with a distinct search query after the provider timeout.",
            }],
        }))

    def test_identical_failure_fingerprint_ignores_attempt_counter(self):
        first = {
            "status": "blocked", "stop_reason": "blocked", "stages": {
                "argument": {"status": "blocked", "attempt_count": 10,
                             "error": "argument attempt-10 failed: invalid JSON"},
            },
            "active_research_requests": [], "active_blockers": [],
        }
        second = {
            **first,
            "stages": {"argument": {"status": "blocked", "attempt_count": 11,
                                      "error": "argument attempt-11 failed: invalid JSON"}},
        }
        from scisaurus.runtime.composer_supervisor import _result_fingerprint

        self.assertEqual(_result_fingerprint(first), _result_fingerprint(second))

    def test_revised_repair_policy_is_a_new_semantic_work_order(self):
        from scisaurus.runtime.composer_supervisor import _research_request_fingerprint

        prior = {
            "kind": "recovery", "owner": "strategy.argument",
            "objective": "Repair the argument response under the old contract.",
            "success_condition": "Return a complete schema-valid argument.",
            "repair_policy_revision": "character-limited-v0",
        }
        revised = {
            **prior, "repair_policy_revision": "prose-without-character-ceilings-1",
        }
        self.assertNotEqual(
            _research_request_fingerprint(prior),
            _research_request_fingerprint(revised))

    def test_changed_scoped_order_is_semantic_progress_but_echoed_order_stops(self):
        supervisor = ComposerSupervisor({
            "id": "semantic-progress-test", "project_id": "/tmp/semantic-progress",
        }, poll_seconds=0)
        request = {
            "id": "repair-attempt-1", "kind": "additional_experiment",
            "objective": "Recalculate the declared baseline from the retained data.",
            "success_condition": "The independent calculation reproduces the reported value.",
            "target_stage_id": "experiment",
        }
        base = {
            "status": "blocked", "remaining_seconds": 3600,
            "stages": {"experiment": {"status": "blocked", "attempt_count": 4,
                                        "error": "experiment attempt-4 failed"}},
            "active_research_requests": [request], "active_blockers": [],
        }
        from scisaurus.runtime.composer_supervisor import _result_fingerprint

        self.assertTrue(supervisor._should_resume(base))
        supervisor.identical_exit_count = 1
        self.assertFalse(supervisor._should_resume(base))
        changed = {
            **base,
            "active_research_requests": [{
                **request, "id": "repair-attempt-5",
                "objective": "Repair the sampling frame and rerun the paired analysis.",
            }],
        }
        self.assertNotEqual(_result_fingerprint(base), _result_fingerprint(changed))

    def test_watchdog_and_child_exception_recovery_are_bounded(self):
        supervisor = ComposerSupervisor({
            "id": "bounded-process-recovery-test", "project_id": "/tmp/bounded-recovery",
        }, poll_seconds=0)
        watchdog = {
            "status": "blocked", "remaining_seconds": 3600,
            "active_research_requests": [], "active_blockers": [{
                "watchdog": True, "active_attempts": ["attempt-1"],
                "stage_id": "experiment", "reason": "watchdog timeout",
            }],
        }
        self.assertTrue(supervisor._should_resume(watchdog))
        supervisor.identical_exit_count = 1
        self.assertFalse(supervisor._should_resume(watchdog))
        supervisor.identical_exit_count = 0
        no_active_attempt = {
            **watchdog,
            "active_blockers": [{**watchdog["active_blockers"][0], "active_attempts": []}],
        }
        self.assertFalse(supervisor._should_resume(no_active_attempt))
        recoverable_exception = {
            "status": "blocked", "remaining_seconds": 3600,
            "active_research_requests": [],
            "active_blockers": [{"recoverable": True, "failure_class": "child_exception"}],
        }
        self.assertTrue(supervisor._should_resume(recoverable_exception))

    def test_harness_bug_stops_automatic_retries_even_with_open_work_orders(self):
        supervisor = ComposerSupervisor({
            "id": "harness-bug-stop-test", "project_id": "/tmp/harness-bug-stop",
        }, poll_seconds=0)
        result = {
            "status": "blocked", "remaining_seconds": 3600,
            "active_blockers": [{
                "stage_id": "experiment", "failure_class": "harness_bug",
                "reason": "AttributeError: NoneType.get",
            }],
            "active_research_requests": [{
                "id": "repair-order", "objective": "repair and rerun the experiment",
            }],
        }

        self.assertFalse(supervisor._should_resume(result))

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
                self.assertEqual(value["stop_reason"], "process_interrupted")
                self.assertTrue(any(
                    item.get("stop_reason") == "process_interrupted"
                    for item in value["blockers"]
                ))

    def test_interrupted_checkpoint_does_not_relabel_older_run_reports(self):
        with tempfile.TemporaryDirectory() as path:
            project = Path(path) / "project"
            output = project / "output"
            output.mkdir(parents=True)
            current = {"schema_version": "fixture", "status": "running",
                       "run_id": "current", "blockers": []}
            stale = {"schema_version": "fixture", "status": "completed",
                     "run_id": "previous", "blockers": []}
            (output / "progress.json").write_text(json.dumps(current))
            (output / "run.json").write_text(json.dumps(stale))
            (output / "interim_report.json").write_text(json.dumps(stale))
            supervisor = ComposerSupervisor({
                "id": "interrupt-stale-report-test", "project_id": str(project),
            })
            supervisor._mark_interrupted_checkpoint()
            progress = json.loads((output / "progress.json").read_text())
            self.assertEqual(progress["status"], "paused")
            self.assertEqual(progress["phase"], "paused")
            for name in ("run.json", "interim_report.json"):
                self.assertEqual(json.loads((output / name).read_text()), stale)

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
                    "active_research_requests": [{
                        "id": "topic-repair", "kind": "topic_refinement",
                        "objective": "Refine the same phenomenon into a falsifiable question.",
                        "target_stage_id": "topic",
                    }],
                },
                {"status": "completed", "remaining_seconds": 10,
                 "stages": {}, "blockers": [], "continuation_cycles": 2},
            ]
            calls = []

            class FakeRunner(_FixtureRunner):
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

            class InvalidRunner(_FixtureRunner):
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
