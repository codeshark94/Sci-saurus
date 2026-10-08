"""Shared dispatch admission across ordinary clients and engineering batches."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.runtime.dsh_batch import DshBatchRunner
from scisaurus.runtime.model_dispatch import ModelSlotTimeout, model_dispatch_slot
from scisaurus.runtime.models import ModelCallError, ModelClient
from scisaurus.runtime.run_control import RunPausedError


def _hold_process(root, entered):
    with patch("scisaurus.runtime.model_dispatch._slot_root", return_value=Path(root)):
        with model_dispatch_slot(deadline=time.monotonic() + 15):
            entered.set()
            time.sleep(15)


def _hold_with_child(root, child_pid):
    with patch("scisaurus.runtime.model_dispatch._slot_root", return_value=Path(root)):
        with model_dispatch_slot(deadline=time.monotonic() + 15) as slot:
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"],
                                     pass_fds=(slot.fileno(),), start_new_session=True)
            Path(child_pid).write_text(str(child.pid))
            child.wait()


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("scisaurus.runtime.model_dispatch._slot_root", return_value=self.root))
        self.stack.enter_context(patch.dict(os.environ))
        os.environ.pop("SCISAURUS_RUN_CONTROL", None)
        os.environ.pop("SCISAURUS_RUN_GENERATION", None)

    def occupy(self):
        return self.stack.enter_context(model_dispatch_slot(deadline=time.monotonic() + 5))

    def test_fourth_thread_waits_and_exception_releases_slot(self):
        with ExitStack() as holders:
            for _ in range(3):
                holders.enter_context(model_dispatch_slot(deadline=time.monotonic() + 5))
            with self.assertRaises(ModelSlotTimeout):
                with model_dispatch_slot(deadline=time.monotonic() + 0.1):
                    self.fail("fourth slot was admitted")
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            with model_dispatch_slot(deadline=time.monotonic() + 5):
                raise RuntimeError("worker failed")
        for _ in range(3):
            self.occupy()

    def test_processes_share_slots_and_killed_owner_releases(self):
        context = multiprocessing.get_context("spawn")
        entered = [context.Event() for _ in range(4)]
        workers = [context.Process(target=_hold_process, args=(str(self.root), event))
                   for event in entered]
        try:
            for worker, event in zip(workers[:3], entered):
                worker.start()
                self.assertTrue(event.wait(5))
            workers[3].start()
            self.assertFalse(entered[3].wait(0.2))
            workers[0].kill()
            workers[0].join(5)
            self.assertTrue(entered[3].wait(5))
        finally:
            for worker in workers:
                if worker.pid is not None:
                    if worker.is_alive():
                        worker.kill()
                    worker.join(5)

    def test_queued_pause_prevents_dispatch(self):
        for _ in range(3):
            self.occupy()
        control = self.root / "run-control.json"
        control.write_text(json.dumps({"generation": "g", "stop_requested": False}))
        os.environ.update(SCISAURUS_RUN_CONTROL=str(control), SCISAURUS_RUN_GENERATION="g")
        def stop():
            time.sleep(0.1)
            control.write_text(json.dumps({"generation": "g", "stop_requested": True}))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stop)
            with self.assertRaises(RunPausedError):
                with model_dispatch_slot(deadline=time.monotonic() + 5):
                    self.fail("paused request was dispatched")
            future.result()

    def test_inherited_worker_retains_slot_after_controller_is_killed(self):
        for _ in range(2):
            self.occupy()
        pid_path = self.root / "child.pid"
        worker = multiprocessing.get_context("spawn").Process(
            target=_hold_with_child, args=(str(self.root), str(pid_path)))
        child_pid = None
        worker.start()
        try:
            bound = time.monotonic() + 5
            while not pid_path.exists() and time.monotonic() < bound:
                time.sleep(0.01)
            self.assertTrue(pid_path.exists())
            child_pid = int(pid_path.read_text())
            worker.kill()
            worker.join(5)
            with self.assertRaises(ModelSlotTimeout):
                with model_dispatch_slot(deadline=time.monotonic() + 0.15):
                    self.fail("orphan worker lost its reservation")
            os.kill(child_pid, 9)
            with model_dispatch_slot(deadline=time.monotonic() + 5):
                pass
        finally:
            if worker.is_alive():
                worker.kill()
                worker.join(5)
            if child_pid is not None:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    def test_waiting_client_preserves_budget_and_never_reaches_transport(self):
        for _ in range(3):
            self.occupy()
        client = ModelClient(base_url="http://127.0.0.1:1", protocol="ollama", model="offline",
                             timeout_seconds=0.1, max_output_tokens=32)
        with patch("scisaurus.runtime.models._reserve_model_call_budgets") as reserve:
            with self.assertRaises(ModelCallError) as raised:
                client.complete(system="system", prompt="prompt")
        self.assertEqual(raised.exception.attempts, 0)
        self.assertTrue(raised.exception.outcome_known)
        reserve.assert_not_called()

    def test_dsh_queue_timeout_never_launches_worker(self):
        for _ in range(3):
            self.occupy()
        runner = DshBatchRunner.__new__(DshBatchRunner)
        runner.config = {"timeout_seconds": 0.1}
        with patch.object(runner, "_run") as launch:
            with self.assertRaises(ModelSlotTimeout):
                runner.run("task", inputs={}, outputs=["answer"])
        launch.assert_not_called()

    def test_dsh_reservation_and_real_http_clients_share_three_slots(self):
        state = {"http_active": 0, "batch_active": 0, "peak": 0, "requests": 0}
        lock = threading.Lock()
        entered, release = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                with lock:
                    state["http_active"] += 1
                    state["requests"] += 1
                    state["peak"] = max(state["peak"], state["http_active"] + state["batch_active"])
                try:
                    time.sleep(0.15)
                    body = json.dumps({"done": True, "message": {"content": "ok"},
                                       "done_reason": "stop", "prompt_eval_count": 1, "eval_count": 1}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    with lock:
                        state["http_active"] -= 1

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runner = DshBatchRunner.__new__(DshBatchRunner)
        runner.config = {"timeout_seconds": 10}
        def batch(*args, **kwargs):
            with lock:
                state["batch_active"] = 1
            entered.set()
            try:
                if not release.wait(10):
                    raise TimeoutError("test worker not released")
            finally:
                with lock:
                    state["batch_active"] = 0
        try:
            with patch.object(runner, "_run", side_effect=batch), ThreadPoolExecutor(max_workers=7) as pool:
                job = pool.submit(runner.run, "task", inputs={}, outputs=["answer"])
                self.assertTrue(entered.wait(5))
                client = ModelClient(base_url=f"http://127.0.0.1:{server.server_port}",
                                     protocol="ollama", model="offline", timeout_seconds=5,
                                     max_output_tokens=32)
                futures = [pool.submit(client.complete, system="system", prompt="prompt") for _ in range(6)]
                try:
                    self.assertTrue(all(future.result().text == "ok" for future in futures))
                finally:
                    release.set()
                job.result()
            self.assertEqual(state["peak"], 3)
            self.assertEqual(state["requests"], 6)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
