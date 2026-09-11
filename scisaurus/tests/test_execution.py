"""Process-level dispatch evidence using explicit, local simulated workers."""
from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.config import validate_config
from scisaurus.runtime.execution import ExecutionRuntime
from scisaurus.tests.test_runner import config


def execution_worker(kind, params, channel):
    assignment = json.loads(params["prompt"])
    started = time.monotonic()
    if assignment.get("descendant"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Path(assignment["descendant"]).write_text(json.dumps({"worker": os.getpid(), "child": child.pid}))
    time.sleep(assignment["delay"])
    if assignment.get("failure"):
        channel.put({"ok": False, "error": "simulated worker failure",
                     "outcome_known": "false" if assignment["failure"] == "malformed" else assignment["failure"] == "known"})
        return
    usage = {"model_calls": 1, "input_tokens": 10, "output_tokens": 5}
    if assignment.get("missing_usage"):
        usage = {"model_calls": 1}
    result = {"text": json.dumps({"started": started, "ended": time.monotonic(), "pid": os.getpid()}),
              "model": "simulated", "usage": usage, "elapsed_seconds": time.monotonic() - started,
              "finish_reason": "stop"}
    if assignment.get("malformed"):
        del result["usage"]
    channel.put({"ok": True, "result": result})


class TestExecutionRuntime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-execution-test-")
        self.root = Path(self.temp.name)
        self.runtimes = []

    def tearDown(self):
        for runtime in self.runtimes:
            runtime.control.close()
        self.temp.cleanup()

    def runtime(self, *, capacity=2, wall=15, on_progress=None):
        value = config()
        value["limits"].update(concurrent_calls=capacity, wall_clock_seconds=wall, checkpoint_seconds=0.05)
        value["model"]["timeout_seconds"] = min(value["model"]["timeout_seconds"], wall / 2)
        runtime = ExecutionRuntime(self.root / f"project-{len(self.runtimes)}", validate_config(value),
                                   worker_target=execution_worker, on_progress=on_progress)
        self.runtimes.append(runtime)
        return runtime

    @staticmethod
    def spec(task_id, *, delay=0.3, timeout=8, reservation_id=None, **assignment):
        return {"task_id": task_id, "kind": "model", "actor": "strategy.worker", "task_kind": "production",
                "reservation_id": reservation_id,
                "params": {"client": {"timeout_seconds": timeout},
                           "prompt": json.dumps({"delay": delay, **assignment})}}

    @staticmethod
    def peak(outcomes):
        events = []
        timings = []
        for outcome in outcomes.values():
            if outcome["ok"]:
                timing = json.loads(outcome["result"]["text"])
                timings.append(timing)
                events.extend([(timing["started"], 1), (timing["ended"], -1)])
        running = peak = 0
        for _, delta in sorted(events):
            running += delta
            peak = max(peak, running)
        return peak, timings

    def assert_pids_stopped(self, paths):
        pids = [pid for path in paths for pid in json.loads(path.read_text()).values()]
        deadline = time.monotonic() + 2
        while True:
            living = []
            for pid in pids:
                result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
                if result.stdout.strip() and not result.stdout.lstrip().startswith("Z"):
                    living.append(pid)
            if not living or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        self.assertEqual(living, [], f"worker descendants still running: {living}")

    def test_spawned_workers_overlap_and_respect_parallel_cap(self):
        updates = []
        runtime = self.runtime(capacity=3, on_progress=updates.append)
        outcomes = runtime._call_batch([self.spec(f"work-{i}", delay=0.6) for i in range(3)], max_parallel=2)
        self.assertTrue(all(outcome["ok"] for outcome in outcomes.values()))
        peak, timings = self.peak(outcomes)
        self.assertEqual(peak, 2)
        self.assertEqual(len({timing["pid"] for timing in timings}), 3)
        elapsed = max(t["ended"] for t in timings) - min(t["started"] for t in timings)
        individual = sum(t["ended"] - t["started"] for t in timings)
        self.assertLess(elapsed, individual - 0.25)
        self.assertTrue(any(len(update["active_tasks"]) == 2 for update in updates))
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {})
        self.assertEqual(runtime.budget.get_window("run-window")["cumulative_usage"]["model_calls"], 3)
        self.assertTrue(runtime.control.verify_chain()[0])

    def test_failed_worker_does_not_discard_valid_siblings(self):
        for known in ("known", "unknown"):
            with self.subTest(known=known):
                runtime = self.runtime()
                outcomes = runtime._call_batch([self.spec("failure", delay=0.05, failure=known),
                                               self.spec("success-one"), self.spec("success-two")])
                self.assertFalse(outcomes["failure"]["ok"])
                self.assertTrue(outcomes["success-one"]["ok"])
                self.assertTrue(outcomes["success-two"]["ok"])
                self.assertEqual(runtime.tasks.get("success-one")["state"], "awaiting_review")
                expected = {} if known == "known" else {"concurrent_calls": 1}
                self.assertEqual(runtime.budget.get_window("run-window")["reserved"], expected)

    def test_reserved_review_capacity_remains_available_to_its_owner(self):
        runtime = self.runtime(capacity=3)
        runtime.budget.reserve(window_id="run-window", reservation_id="review-capacity", task_id="review",
                               amount={"concurrent_calls": 1})
        outcomes = runtime._call_batch([self.spec(f"writer-{i}") for i in range(4)])
        self.assertEqual(self.peak(outcomes)[0], 2)
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})
        review = runtime._call_batch([self.spec("review", reservation_id="review-capacity")])
        self.assertTrue(review["review"]["ok"])
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {})

    def test_held_reservation_can_dispatch_behind_unreserved_pending_work(self):
        runtime = self.runtime()
        for task in ("review-one", "review-two"):
            runtime.budget.reserve(window_id="run-window", reservation_id=task, task_id=task,
                                   amount={"concurrent_calls": 1})
        outcomes = runtime._call_batch([self.spec("writer"), self.spec("review-one")])
        self.assertTrue(all(outcome["ok"] for outcome in outcomes.values()))
        writer = json.loads(outcomes["writer"]["result"]["text"])
        review = json.loads(outcomes["review-one"]["result"]["text"])
        self.assertGreater(writer["started"], review["ended"])
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})

    def test_exhausted_external_reservations_block_without_waiting(self):
        runtime = self.runtime()
        for i in range(2):
            runtime.budget.reserve(window_id="run-window", reservation_id=f"external-{i}", task_id=f"external-{i}",
                                   amount={"concurrent_calls": 1})
        started = time.monotonic()
        outcomes = runtime._call_batch([self.spec("blocked")])
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(outcomes["blocked"]["ok"])
        self.assertTrue(outcomes["blocked"]["outcome_known"])
        self.assertEqual(runtime.tasks.get("blocked")["state"], "blocked")
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 2})

    def test_missing_token_usage_stays_explicit_and_malformed_result_retains_capacity(self):
        runtime = self.runtime()
        outcomes = runtime._call_batch([self.spec("unreported", missing_usage=True),
                                       self.spec("malformed", malformed=True)])
        self.assertTrue(outcomes["unreported"]["ok"])
        self.assertFalse(outcomes["malformed"]["ok"])
        self.assertFalse(outcomes["malformed"]["outcome_known"])
        self.assertEqual(runtime.usage_gaps, [{"task_id": "unreported",
                                              "unreported_dimensions": ["input_tokens", "output_tokens"]}])
        self.assertEqual(runtime.budget.get_window("run-window")["cumulative_usage"], {"model_calls": 1})
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})

    @unittest.skipUnless(os.name == "posix", "process-group cleanup uses POSIX process sessions")
    def test_interrupt_keeps_unknown_cost_releases_undispatched_review_and_stops_descendants(self):
        paths = [self.root / f"pid-{i}.json" for i in range(2)]
        def interrupt_when_running(update):
            if len(update["active_tasks"]) == 2 and all(path.exists() for path in paths):
                raise KeyboardInterrupt("simulated cancellation")
        runtime = self.runtime(capacity=3, on_progress=interrupt_when_running)
        runtime.budget.reserve(window_id="run-window", reservation_id="pending-review", task_id="pending-review",
                               amount={"concurrent_calls": 1})
        prior_children = {child.pid for child in multiprocessing.active_children()}
        specs = [self.spec(f"active-{i}", delay=30, descendant=str(paths[i])) for i in range(2)]
        outcomes = runtime._call_batch([*specs, self.spec("pending-review")], max_parallel=2)
        self.assertFalse(outcomes["active-0"]["outcome_known"])
        self.assertFalse(outcomes["active-1"]["outcome_known"])
        self.assertTrue(outcomes["pending-review"]["outcome_known"])
        self.assertEqual(runtime.tasks.get_attempt("active-0-attempt")["state"], "result_unknown")
        self.assertEqual(runtime.tasks.get("pending-review")["state"], "blocked")
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 2})
        self.assertEqual(runtime.budget.get_reservation("pending-review")["state"], "settled")
        self.assertEqual(runtime.active_tasks, [])
        self.assertIsNone(runtime.active_task)
        self.assertTrue(runtime.cancelled)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, prior_children)
        self.assert_pids_stopped(paths)
        later = runtime._call_batch([self.spec("later")])
        self.assertTrue(later["later"]["outcome_known"])
        self.assertFalse(later["later"]["ok"])
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup uses POSIX process sessions")
    def test_deadline_stops_owned_workers_and_blocks_pending_work(self):
        runtime = self.runtime(wall=0.65)
        paths = [self.root / f"deadline-pid-{i}.json" for i in range(2)]
        specs = [self.spec(f"active-{i}", delay=30, descendant=str(paths[i])) for i in range(2)]
        started = time.monotonic()
        outcomes = runtime._call_batch([*specs, self.spec("pending")])
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(outcomes["active-0"]["outcome_known"])
        self.assertFalse(outcomes["active-1"]["outcome_known"])
        self.assertTrue(outcomes["pending"]["outcome_known"])
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 2})
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 2)
        self.assert_pids_stopped(paths)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup uses POSIX process sessions")
    def test_operation_timeout_keeps_other_workers_and_stops_descendants(self):
        runtime = self.runtime()
        path = self.root / "timeout-pids.json"
        outcomes = runtime._call_batch([
            self.spec("timeout", delay=30, timeout=0.4, descendant=str(path)),
            self.spec("sibling", delay=0.6), self.spec("pending", delay=0.1)])
        self.assertFalse(outcomes["timeout"]["outcome_known"])
        self.assertTrue(outcomes["sibling"]["ok"])
        self.assertTrue(outcomes["pending"]["ok"])
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})
        self.assert_pids_stopped([path])

    def test_malformed_known_flag_cannot_release_uncertain_capacity(self):
        runtime = self.runtime()
        outcome = runtime._call_batch([self.spec("malformed", delay=0.01, failure="malformed")])["malformed"]
        self.assertFalse(outcome["outcome_known"])
        self.assertEqual(runtime.tasks.get_attempt("malformed-attempt")["state"], "result_unknown")
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})

    def test_existing_runtime_requires_explicit_recovery_and_reconciles_unknown_attempt(self):
        value = config()
        value["limits"].update(concurrent_calls=2, wall_clock_seconds=15, checkpoint_seconds=1)
        value["model"]["timeout_seconds"] = 5
        source_root = self.root / "source"
        (source_root / "scisaurus").mkdir(parents=True)
        (source_root / "scisaurus" / "worker.py").write_text("VERSION = 1\n")
        run_dir = self.root / "recoverable"
        runtime = ExecutionRuntime(run_dir, validate_config(value), worker_target=execution_worker,
                                   repository_root=source_root)
        self.runtimes.append(runtime)
        outcome = runtime._call_batch([self.spec("unknown", delay=0.01, failure="unknown")])["unknown"]
        self.assertFalse(outcome["outcome_known"])
        runtime.control.close()
        self.runtimes.remove(runtime)
        with self.assertRaisesRegex(ValidationError, "new project directory"):
            ExecutionRuntime(run_dir, validate_config(value), worker_target=execution_worker,
                             repository_root=source_root)
        policy = {"additional_seconds": 10,
                  "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"model_calls": 1}},
                  "source_changes": {"mode": "reject", "reopen_scopes": []}}
        resumed = ExecutionRuntime(run_dir, validate_config(value), worker_target=execution_worker,
                                   repository_root=source_root, resume_policy=policy)
        self.runtimes.append(resumed)
        self.assertEqual(resumed.tasks.get_attempt("unknown-attempt")["state"], "failed")
        self.assertEqual(resumed.budget.get_window("run-window")["reserved"], {})
        self.assertEqual(resumed.resume_session["unknown_reconciliations"][0]["task_id"], "unknown")

    def test_completed_result_is_recorded_but_single_call_still_propagates_cancellation(self):
        count = 0
        def cancel_after_publication(update):
            nonlocal count
            if update["phase"] != "executing":
                return
            count += 1
            if count == 2:
                path = runtime.dir / "runs" / "final-review" / "result.json"
                deadline = time.monotonic() + 2
                while not path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(path.exists())
                raise KeyboardInterrupt("cancel after completed result")
        runtime = self.runtime(on_progress=cancel_after_publication)
        spec = self.spec("final-review", delay=0.3)
        with self.assertRaisesRegex(KeyboardInterrupt, "cancel after completed result"):
            runtime._call("final-review", "model", spec["params"], actor="methods.verifier", task_kind="verification")
        self.assertTrue(runtime.cancelled)
        self.assertEqual(runtime.tasks.get_attempt("final-review-attempt")["state"], "succeeded")
        self.assertEqual(runtime.tasks.get("final-review")["state"], "awaiting_review")
        self.assertIsNotNone(runtime.store.head("command/executions/final-review"))
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {})
        self.assertEqual(runtime.budget.get_window("run-window")["cumulative_usage"],
                         {"model_calls": 1, "input_tokens": 10, "output_tokens": 5})

    def test_successful_single_call_cannot_continue_past_the_run_deadline(self):
        count = 0
        def delay_parent_after_dispatch(update):
            nonlocal count
            count += 1
            if count == 2:
                while time.monotonic() <= runtime.deadline + 0.01:
                    time.sleep(0.01)
        runtime = self.runtime(wall=0.7, on_progress=delay_parent_after_dispatch)
        spec = self.spec("expired", delay=0.2)
        with self.assertRaisesRegex(ValidationError, "deadline"):
            runtime._call("expired", "model", spec["params"], actor="methods.verifier", task_kind="verification")
        self.assertEqual(runtime.tasks.get_attempt("expired-attempt")["state"], "succeeded")
        self.assertEqual(runtime.budget.get_window("run-window")["reserved"], {})

    def test_reservation_cannot_be_reused_for_another_task(self):
        runtime = self.runtime()
        runtime.budget.reserve(window_id="run-window", reservation_id="review", task_id="review",
                               amount={"concurrent_calls": 1})
        with self.assertRaises(ValidationError):
            runtime._call_batch([self.spec("writer", reservation_id="review")])
        self.assertEqual(runtime.control._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
