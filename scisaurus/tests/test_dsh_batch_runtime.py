"""Exercise the real DSH runtime against a synthetic localhost provider."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scisaurus.runtime.dsh_batch import DshBatchError, DshBatchRunner, sha256
from scisaurus.runtime.development_session import development_session_binding


@unittest.skipUnless(os.environ.get("SCISAURUS_DSH_CONFIG"), "explicit pinned DSH deployment required")
class RuntimeTests(unittest.TestCase):
    def exercise(self, *, orphan=False, failure=False, persistent=False):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                if failure:
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"offline failure"}}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                def event(data):
                    self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
                    self.wfile.flush()
                event({"id": "mock-" + str(len(requests)), "model": "offline-model",
                       "choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]})
                if len(requests) == 1 or persistent and len(requests) == 3:
                    command = "printf 'offline proof' > answer.txt"
                    if orphan:
                        command = "sleep 60 >/dev/null 2>&1 & echo $! > child.pid; " + command
                    event({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call-1",
                        "type": "function", "function": {"name": "bash", "arguments": json.dumps({
                            "command": command, "description": "Produce file"})}}]}}]})
                    finish = "tool_calls"
                else:
                    event({"choices": [{"index": 0, "delta": {"content": "File produced."}}]})
                    finish = "stop"
                event({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                       "usage": {"prompt_tokens": 20, "completion_tokens": 10}})
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ):
                os.environ.pop("SCISAURUS_RUN_CONTROL", None)
                os.environ.pop("SCISAURUS_RUN_GENERATION", None)
                os.environ["DSH_OFFLINE_KEY"] = "synthetic-localhost-only"
                config = json.loads(Path(os.environ["SCISAURUS_DSH_CONFIG"]).read_text())
                composition = json.loads(Path(config["composition"]).read_text())
                profile = next(p["config"]["providers"][config["provider"]] for p in composition
                               if p.get("id") == "llm")
                profile.update(baseURL=f"http://127.0.0.1:{server.server_port}/v1", apiKeyEnv="DSH_OFFLINE_KEY")
                profile["models"][0]["id"] = "offline-model"
                path = Path(temp) / "composition.json"
                path.write_text(json.dumps(composition))
                config["pinned_files"].pop(config["composition"])
                config.update(composition=str(path), model="offline-model", auth_env="DSH_OFFLINE_KEY", timeout_seconds=30)
                config["pinned_files"][str(path)] = sha256(path)
                config["read_roots"].append(temp)
                runner = DshBatchRunner(config, root=Path(temp) / "jobs",
                    development_session=development_session_binding(temp) if persistent else None)
                kwargs = dict(inputs={"spec.json": '{"frozen":true}'}, outputs=["answer.txt"])
                if failure:
                    with self.assertRaises(DshBatchError) as raised:
                        runner.run("Use bash to produce answer.txt", **kwargs)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(raised.exception.usage["model_calls"], 1)
                    return
                result = runner.run("Use bash to produce answer.txt", **kwargs)
                self.assertEqual(result["files"]["answer.txt"], b"offline proof")
                self.assertEqual(result["usage"], {"model_calls": 2, "input_tokens": 40, "output_tokens": 20})
                self.assertEqual(len(requests), 2)
                self.assertTrue(all(r["model"] == "offline-model" for r in requests))
                self.assertTrue(all(r["thinking"] == {"type": "disabled"} for r in requests))
                if persistent:
                    second = runner.run("Continue the first design; use bash to produce answer.txt", **kwargs)
                    self.assertEqual(second["files"]["answer.txt"], b"offline proof")
                    self.assertEqual(second["usage"], result["usage"])
                    self.assertEqual(len(requests), 4)
                    history = json.dumps(requests[2]["messages"])
                    self.assertIn('Use bash to produce answer.txt', history)
                    self.assertIn('File produced.', history)
                    receipts = [json.loads(Path(v["receipt"]).read_text()) for v in (result, second)]
                    self.assertEqual(receipts[0]['session_id'], receipts[1]['session_id'])
                    self.assertNotEqual(receipts[0]['message_id'], receipts[1]['message_id'])
                    self.assertNotEqual(receipts[0]['pid'], receipts[1]['pid'])
                if orphan:
                    pid = int((Path(result["receipt"]).parent / "work/child.pid").read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
        finally:
            server.shutdown()
            server.server_close()

    def test_file_edit_and_fixed_reasoning_wire(self):
        self.exercise()

    def test_reparented_shell_descendant_reaped(self):
        self.exercise(orphan=True)

    def test_failed_provider_dispatch_charged_once(self):
        self.exercise(failure=True)

    def test_persisted_history_resumes_in_a_new_process_without_recounting(self):
        self.exercise(persistent=True)


if __name__ == "__main__":
    unittest.main()
