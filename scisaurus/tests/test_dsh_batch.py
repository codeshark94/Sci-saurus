"""Offline contract and lifecycle checks for file-based engineering batches."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.dsh_batch import DshAuthorClient, DshValidatorClient, DshBatchError, DshBatchRunner, sha256, validate_batch_config
from scisaurus.runtime.models import ModelResult


FAKE = '''import json, os, subprocess, sys, time
from pathlib import Path
mode = json.loads(Path(os.environ['DSH_CORDIS_CONFIG']).read_text())['mode']
def emit(value):
 print(json.dumps(value), flush=True)
for line in sys.stdin:
 request=json.loads(line)
 if request['method']=='initialize':
  assert request['params']['model']=='fixed-model'
  emit({'jsonrpc':'2.0','id':request['id'],'result':{}})
 elif request['method']=='session/prompt':
  session=request['params']['sessionId']
  def event(kind,data):
   emit({'jsonrpc':'2.0','method':'session.event','params':{'sessionId':session,'event':{'type':kind,'data':data}}})
  event('agent/inbox/spliced',{'inserted':[{'id':'message-1'}]})
  emit({'jsonrpc':'2.0','id':request['id'],'result':{'messageId':'message-1'}})
  if mode=='wait':
   child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)
   Path('child.pid').write_text(str(child.pid))
   time.sleep(60)
  if mode=='escape':
   Path('answer.txt').symlink_to('/etc/hosts')
  elif mode!='missing':
   Path('answer.txt').write_text('real file output')
  event('assistant/message',{'turn':1,'step':1,'message':{'id':'m1','source':{'kind':'model'}},'usage':{'inputTokens':21,'outputTokens':8}})
  event('assistant/message',{'turn':1,'step':1,'message':{'id':'m1','source':{'kind':'model'}},'usage':{'inputTokens':21,'outputTokens':8}})
  event('turn/end',{'reason':{'kind':'max-tokens' if mode=='length' else 'completed'}})
  emit({'jsonrpc':'2.0','method':'session.status','params':{'sessionId':session,'status':'idle'}})
'''


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.script = self.root / "runtime.py"
        self.script.write_text(FAKE)
        self.composition = self.root / "composition.json"
        self.composition.write_text(json.dumps({"mode": "ok"}))
        self.env = patch.dict(os.environ, {"DSH_TEST_KEY": "offline-only"})
        self.env.start()
        self.grant = patch.dict(os.environ)
        self.grant.start()
        os.environ.pop("SCISAURUS_RUN_CONTROL", None)
        os.environ.pop("SCISAURUS_RUN_GENERATION", None)

    def tearDown(self):
        self.grant.stop()
        self.env.stop()
        self.tmp.cleanup()

    def config(self, mode="ok", seconds=10):
        self.composition.write_text(json.dumps({"mode": mode}))
        paths = [Path(sys.executable).absolute(), self.script, self.composition]
        return {"schema_version": "dsh-batch-1", "command": [str(paths[0]), str(self.script)],
                "pinned_files": {str(p): sha256(p) for p in paths}, "read_roots": [str(self.root)],
                "composition": str(self.composition), "provider": "test", "model": "fixed-model",
                "auth_env": "DSH_TEST_KEY", "timeout_seconds": seconds, "max_output_tokens": 1024}

    def run_job(self, config):
        return DshBatchRunner(config, root=self.root / "jobs").run(
            "Write answer.txt", inputs={"spec.json": '{"frozen":true}'}, outputs=["answer.txt"])

    def test_receipt_owned_session_and_usage(self):
        result = self.run_job(self.config())
        self.assertEqual(result["files"]["answer.txt"], b"real file output")
        self.assertEqual(result["usage"], {"model_calls": 1, "input_tokens": 21, "output_tokens": 8})
        receipt = json.loads(Path(result["receipt"]).read_text())
        self.assertTrue(receipt["process_reaped"])
        self.assertEqual(receipt["status"], "completed")

    def exchange_backend(self, *, omit_second_output=False):
        script = FAKE.replace("for line in sys.stdin:", "turn=0\nfor line in sys.stdin:")
        script = script.replace("session=request['params']['sessionId']", "turn+=1\n  session=request['params']['sessionId']")
        script = script.replace("'message-1'", "'message-'+str(turn)")
        script = script.replace("'turn':1", "'turn':turn")
        script = script.replace("event('turn/end',{'reason'", "event('turn/end',{'turn':turn,'reason'")
        script = script.replace("Path('answer.txt').write_text('real file output')",
            "\n   if turn==1 or not " + repr(omit_second_output) + ":\n    Path('answer.txt').write_text('turn '+str(turn))\n"
            "   if turn==2:\n"
            "    event('turn/end',{'turn':1,'reason':{'kind':'max-tokens'}})\n"
            "   emit({'method':'session.event','params':{'sessionId':'foreign','event':{'type':'assistant/message','data':{'turn':turn,'step':1,'usage':{'inputTokens':900,'outputTokens':900}}}}})")
        self.script.write_text(script)
        return self.config()

    def test_controller_exchange_keeps_session_and_archives_immutable_turns(self):
        seen = []
        def exchange(files, usage):
            seen.append((files, usage))
            if len(seen) == 1:
                return {"task": "Continue using the controller receipt", "inputs": {"receipt.json": '{"observed":true}'}}
        result = DshBatchRunner(self.exchange_backend(), root=self.root / "jobs").run(
            "Write answer.txt", inputs={"spec.json": "original"}, outputs=["answer.txt"], exchange=exchange)
        job = Path(result["receipt"]).parent
        receipt = json.loads(Path(result["receipt"]).read_text())
        self.assertEqual(result["usage"], {"model_calls": 2, "input_tokens": 42, "output_tokens": 16})
        self.assertEqual([row[0]["answer.txt"] for row in seen], [b"turn 1", b"turn 2"])
        self.assertEqual([row["usage"]["model_calls"] for row in receipt["turns"]], [1, 2])
        self.assertEqual((job / "turns/0/answer.txt").read_bytes(), b"turn 1")
        self.assertEqual((job / "turns/1/answer.txt").read_bytes(), b"turn 2")
        self.assertEqual((job / "input/spec.json").read_text(), "original")
        self.assertEqual((job / "input/turn-1/receipt.json").read_text(), '{"observed":true}')
        self.assertEqual(receipt["schema_version"], "dsh-controller-session-receipt-1")
        self.assertTrue(receipt["process_reaped"])
        prompts = [json.loads(line) for line in (job / "events.jsonl").read_text().splitlines()
                   if '"messageId"' in line]
        self.assertEqual(len(prompts), 2)
        self.assertNotEqual(receipt["turns"][0]["message_id"], receipt["turns"][1]["message_id"])

    def test_controller_exchange_cannot_accept_prior_turn_output(self):
        config = self.exchange_backend(omit_second_output=True)
        with self.assertRaises(DshBatchError) as raised:
            DshBatchRunner(config, root=self.root / "jobs").run(
                "Write answer.txt", inputs={"spec.json": "original"}, outputs=["answer.txt"],
                exchange=lambda files, usage: {"task": "Continue", "inputs": {"feedback.txt": "next"}})
        receipt = json.loads(Path(raised.exception.receipt).read_text())
        self.assertIn("contained regular file", receipt["error"])
        self.assertEqual(len(receipt["turns"]), 1)
        self.assertEqual(receipt["usage"]["model_calls"], 2)
        self.assertTrue(receipt["process_reaped"])

    def test_complete_receipt_files_are_locally_readable_and_bound_on_every_turn(self):
        config = self.exchange_backend()
        script = self.script.read_text().replace("Path('answer.txt').write_text('turn '+str(turn))",
            "task_text=request['params']['contentBlocks'][0]['text']\n"
            "    frozen=Path(task_text.split('Immutable task files: ',1)[1].split('\\n',1)[0])\n"
            "    entry=json.loads((frozen/'engineering-receipts.json').read_text())['entries'][0]\n"
            "    body=(frozen/entry['path']).read_bytes()\n"
            "    import hashlib\n"
            "    assert hashlib.sha256(body).hexdigest()==entry['body_sha256']\n"
            "    raw=json.loads(body)['result']['raw']\n"
            "    assert len(raw)==20000 and raw[-1]==19999\n"
            "    Path('answer.txt').write_text(str(len(raw)))")
        self.script.write_text(script)
        config = self.config()
        body = json.dumps({"outcome": "failed", "result": {"raw": list(range(20000))}}).encode()
        digest = hashlib.sha256(body).hexdigest()
        name = "engineering-receipts/" + digest + ".json"
        inputs = {name: body, "engineering-receipts.json": json.dumps({"entries": [
            {"path": name, "body_sha256": digest}]}).encode()}
        turns = []
        def exchange(files, usage):
            turns.append(files)
            if len(turns) == 1:
                return {"task": "Inspect the original receipt again locally", "inputs": inputs}
        result = DshBatchRunner(config, root=self.root / "jobs").run(
            "Read the complete receipt locally", inputs=inputs, outputs=["answer.txt"], exchange=exchange)
        receipt = json.loads(Path(result["receipt"]).read_text())
        self.assertEqual([turn["answer.txt"] for turn in turns], [b"20000", b"20000"])
        self.assertEqual(result["usage"]["model_calls"], 2)
        for turn in receipt["turns"]:
            self.assertEqual(turn["input_sha256"][name], digest)
            path = Path(result["receipt"]).parent / turn["input_directory"] / name
            self.assertEqual(path.read_bytes(), body)
            self.assertEqual(path.stat().st_mode & 0o222, 0)

    def test_controller_exchange_failure_preserves_paid_turn(self):
        for continuation in ({"task": "next", "inputs": {"../escape": "bad"}}, "invalid"):
            with self.subTest(continuation=continuation), self.assertRaises(DshBatchError) as raised:
                DshBatchRunner(self.exchange_backend(), root=self.root / "jobs").run(
                    "Write answer.txt", inputs={"spec.json": "original"}, outputs=["answer.txt"],
                    exchange=lambda files, usage: continuation)
            receipt = json.loads(Path(raised.exception.receipt).read_text())
            self.assertEqual(receipt["usage"]["model_calls"], 1)
            self.assertEqual(len(receipt["turns"]), 1)
            self.assertTrue(receipt["process_reaped"])
            self.assertEqual(receipt["status"], "failed")
            self.assertTrue(receipt["outcome_known"])

    def test_missing_and_exhausted_outputs_never_succeed(self):
        for mode in ("missing", "length", "escape"):
            with self.subTest(mode=mode), self.assertRaises(DshBatchError) as raised:
                self.run_job(self.config(mode))
            state = json.loads(Path(raised.exception.receipt).read_text())
            self.assertEqual(state["status"], "result_unknown")
            self.assertTrue(state["process_reaped"])

    def test_pin_change_refused_before_dispatch(self):
        config = self.config()
        self.script.write_text("raise RuntimeError('changed')")
        with self.assertRaisesRegex(ValidationError, "pin mismatch"):
            self.run_job(config)
        receipt = next((self.root / "jobs").glob("*/receipt.json"))
        state = json.loads(receipt.read_text())
        self.assertEqual(state["status"], "not_dispatched")
        self.assertEqual(state["usage"]["model_calls"], 0)

    def test_configuration_inspection_never_rehashes_runtime_files(self):
        config = self.config()
        with patch("scisaurus.runtime.dsh_batch.sha256", side_effect=AssertionError("runtime I/O")):
            self.assertEqual(validate_batch_config(config), config)
        config["pinned_files"][str(self.script)] = "invalid-digest"
        with self.assertRaisesRegex(ValidationError, "pin declaration"):
            validate_batch_config(config)

    def test_path_traversal_refused(self):
        with self.assertRaises(ValidationError):
            DshBatchRunner(self.config(), root=self.root / "jobs").run(
                "task", inputs={"../spec": "x"}, outputs=["answer.txt"])

    def test_deadline_reaps_new_process_groups(self):
        with self.assertRaises(DshBatchError) as raised:
            self.run_job(self.config("wait", seconds=0.7))
        receipt = Path(raised.exception.receipt)
        pid = int((receipt.parent / "work/child.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_deadline_includes_preparation_before_dispatch(self):
        from scisaurus.runtime.dsh_batch import sandbox_profile
        def slow_profile(*args, **kwargs):
            profile = sandbox_profile(*args, **kwargs)
            time.sleep(0.1)
            return profile
        with patch("scisaurus.runtime.dsh_batch.sandbox_profile", side_effect=slow_profile), \
                patch("scisaurus.runtime.dsh_batch.start_process") as launch:
            with self.assertRaises(DshBatchError):
                self.run_job(self.config(seconds=0.05))
        launch.assert_not_called()
        receipts = list((self.root / "jobs").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 1)
        state = json.loads(receipts[0].read_text())
        self.assertEqual(state["status"], "not_dispatched")
        self.assertEqual(state["usage"]["model_calls"], 0)

    def test_initial_running_receipt_failure_reaps_started_transport(self):
        from scisaurus.runtime.dsh_batch import terminate_tree
        replace = Path.replace
        reaped = []
        def fail_running_receipt(path, target):
            if path.name == "receipt.tmp" and json.loads(path.read_text()).get("status") == "running":
                raise OSError("initial receipt storage unavailable")
            return replace(path, target)
        def cleanup(process):
            terminate_tree(process)
            reaped.append(process.poll() is not None)
        with patch.object(Path, "replace", fail_running_receipt), \
                patch("scisaurus.runtime.dsh_batch.terminate_tree", side_effect=cleanup):
            with self.assertRaisesRegex(DshBatchError, "initial receipt storage unavailable") as failure:
                self.run_job(self.config())
        self.assertEqual(reaped, [True])
        state = json.loads(Path(failure.exception.receipt).read_text())
        self.assertEqual(state["status"], "result_unknown")
        self.assertTrue(state["process_reaped"])

    def test_launch_error_retains_zero_call_receipt(self):
        with patch("scisaurus.runtime.dsh_batch.start_process", side_effect=OSError("cannot spawn")):
            with self.assertRaises(DshBatchError) as failure:
                self.run_job(self.config())
        self.assertEqual(failure.exception.usage["model_calls"], 0)
        self.assertEqual(json.loads(Path(failure.exception.receipt).read_text())["status"], "not_dispatched")

    def test_managed_pause_reaps_runtime(self):
        control = self.root / "control"
        control.mkdir()
        path = control / "run-control.json"
        path.write_text(json.dumps({"generation": "g", "stop_requested": False}))
        os.environ.update(SCISAURUS_RUN_CONTROL=str(path), SCISAURUS_RUN_GENERATION="g")
        def stop():
            time.sleep(0.4)
            path.write_text(json.dumps({"generation": "replacement", "stop_requested": False}))
        thread = threading.Thread(target=stop)
        thread.start()
        with self.assertRaises(DshBatchError) as raised:
            self.run_job(self.config("wait"))
        thread.join()
        state = json.loads(Path(raised.exception.receipt).read_text())
        self.assertTrue(state["process_reaped"])
        self.assertIn("stopped or replaced", state["error"])

    def test_failed_receipt_is_durable_before_transport_cleanup(self):
        from scisaurus.runtime.dsh_batch import terminate_tree
        observed = []
        def inspect_then_cleanup(process):
            receipt = next((self.root / "jobs").glob("*/receipt.json"))
            state = json.loads(receipt.read_text())
            observed.append(state)
            self.assertEqual(state["status"], "result_unknown")
            self.assertEqual(state["usage"]["model_calls"], 1)
            self.assertEqual(state["usage"]["input_tokens"], 21)
            self.assertNotIn("process_reaped", state)
            terminate_tree(process)
        with patch("scisaurus.runtime.dsh_batch.terminate_tree", side_effect=inspect_then_cleanup):
            with self.assertRaises(DshBatchError):
                self.run_job(self.config("length"))
        self.assertEqual(len(observed), 1)

    def test_failed_precleanup_receipt_write_still_reaps_transport(self):
        from scisaurus.runtime.dsh_batch import terminate_tree
        replace = Path.replace
        reaped = []
        def fail_terminal_receipt(path, target):
            if path.name == "receipt.tmp" and json.loads(path.read_text()).get("status") == "result_unknown":
                raise OSError("receipt storage unavailable")
            return replace(path, target)
        def cleanup(process):
            terminate_tree(process)
            reaped.append(process.poll() is not None)
        with patch.object(Path, "replace", fail_terminal_receipt), \
                patch("scisaurus.runtime.dsh_batch.terminate_tree", side_effect=cleanup):
            with self.assertRaisesRegex(DshBatchError, "receipt storage unavailable") as caught:
                self.run_job(self.config("length"))
        self.assertEqual(reaped, [True])
        self.assertTrue(Path(caught.exception.receipt).is_file())
        self.assertEqual(caught.exception.usage, {"model_calls": 1, "input_tokens": 21, "output_tokens": 8})
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_cleanup_failure_retains_paid_receipt_and_reaped_process(self):
        from scisaurus.runtime.dsh_batch import terminate_tree
        for mode, expected_status in (("ok", "completed"), ("length", "result_unknown")):
            with self.subTest(mode=mode):
                observed = []
                def cleanup(process):
                    terminate_tree(process)
                    observed.append(process.poll() is not None)
                    raise OSError("cleanup observer failed")
                with patch("scisaurus.runtime.dsh_batch.terminate_tree", side_effect=cleanup):
                    with self.assertRaisesRegex(DshBatchError, "transport disposal failed") as caught:
                        self.run_job(self.config(mode))
                error = caught.exception
                state = json.loads(Path(error.receipt).read_text())
                self.assertEqual(observed, [True])
                self.assertTrue(state["process_reaped"])
                self.assertEqual(state["status"], expected_status)
                self.assertEqual(error.usage, {"model_calls": 1, "input_tokens": 21, "output_tokens": 8})
                self.assertEqual(state["usage"], error.usage)
                self.assertIn("cleanup observer failed", state["cleanup_error"])
                if mode == "length":
                    self.assertIn("files are not accepted", state["error"])

    def test_author_repairs_files_and_preserves_full_execution_input(self):
        from scisaurus.runtime.capability_foundry import _source_patch_context
        client = DshAuthorClient(self.config(), root=self.root / "jobs", runtime_python=sys.executable)
        exact_input = {"unusual": [1, 2], "instructions": "scientific input", "executor_source": "data label"}
        client.base_assignment = {"configured_input": exact_input, "required_intent_fields": {"id": "s"},
                                  "executor_output_exact_shapes": {"metrics": "list"}}
        prompt = {"current_candidate": {"experiment_intent": {"id": "s"},
                  "source_context": {"executor_source": _source_patch_context("print('old')")}}}
        captured = {}
        def run(task, **kw):
            captured.update(kw)
            return {"files": {"executor.py": b"print('new')", "intent.json": b'{"id":"s"}'},
                    "usage": {"model_calls": 2}, "receipt": "retained", "elapsed_seconds": 1}
        with patch.object(client.runner, "run", side_effect=run):
            result = client.complete(system="legacy JSON persona", prompt=json.dumps(prompt))
        self.assertEqual(captured["seed_files"]["executor.py"], "print('old')")
        execution = json.loads(captured["inputs"]["execution-contract.json"])
        self.assertEqual(execution["configured_input"], exact_input)
        self.assertEqual(set(json.loads(result.text)), {"executor_source", "experiment_intent"})
        self.assertEqual(result.usage["model_calls"], 2)

    def test_fixed_composition_identity_changes_foundry_configuration(self):
        config = self.config()
        self.assertEqual(validate_batch_config(config)["model"], "fixed-model")
        changed = deepcopy(config)
        changed["fallback_models"] = ["other"]
        with self.assertRaises(ValidationError):
            validate_batch_config(changed)

    def test_foundry_retains_batch_failure_and_interruption_without_redispatch(self):
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                root = self.root / str(interrupted)
                foundry = CapabilityFoundryTests._foundry(root)
                foundry.author_backend = self.config()
                cache = CapabilityFoundryTests._cache(self, root)
                client = DshAuthorClient(self.config(), root=root / "jobs", runtime_python=sys.executable)
                failure = DshBatchError("transport interrupted", receipt=root / "receipt.json",
                                        usage={"model_calls": 3, "input_tokens": 29})
                with patch("scisaurus.runtime.dsh_batch.DshAuthorClient", return_value=client), \
                        patch.object(client, "complete", side_effect=failure) as dispatch:
                    with self.assertRaises(DshBatchError):
                        foundry.generate("bounded comparison", work_cache=cache)
                    entry = cache.entries()[0]
                    if interrupted:
                        entry.update(status="calling", error=None)
                        entry["requests"][-1].update(status="started")
                        entry["requests"][-1].pop("batch_receipt", None)
                        cache.put(entry["cache_ref"].rsplit("/", 1)[-1].split("@")[0], entry)
                    with self.assertRaises(DshBatchError) as retained:
                        foundry.generate("bounded comparison", work_cache=cache)
                    self.assertEqual(dispatch.call_count, 1)
                    self.assertEqual(retained.exception.failure_class, "operational_recovery")
                    self.assertEqual(retained.exception.usage["model_calls"], 3)
                    self.assertEqual(cache.entries()[0]["requests"][-1]["status"], "result_unknown")

    def test_validator_exports_file_from_blinded_input_and_own_repair(self):
        client = DshValidatorClient(self.config(), root=self.root / "validator-jobs", runtime_python=sys.executable)
        source = "print('validator')"
        assignment = {"configured_input": {"instructions": "data label"}, "experiment_intent": {"id": "s"},
            "raw_observation_sample": [{"x": 1}], "runtime_request_shape": {"candidate": "object"},
            "validator_repair": {"prior_source": source, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                                 "diagnostic": "stdin key mismatch", "patch_contract": {"updates": {}}, "instructions": "return JSON"}}
        captured = {}
        def run(task, **kw):
            captured.update(kw)
            return {"files": {"validator.py": b"print('new validator')"}, "usage": {"model_calls": 2},
                    "receipt": "validator-receipt", "elapsed_seconds": 1}
        with patch.object(client.runner, "run", side_effect=run):
            result = client.complete(system="independent author", prompt=json.dumps(assignment))
        packet = json.loads(captured["inputs"]["assignment.json"])
        self.assertEqual(packet["configured_input"], assignment["configured_input"])
        self.assertEqual(packet["raw_observation_sample"], [{"x": 1}])
        self.assertNotIn("executor_source", packet)
        self.assertNotIn("patch_contract", packet["validator_repair"])
        self.assertEqual(captured["seed_files"], {"validator.py": source})
        self.assertEqual(set(json.loads(result.text)), {"validator_source"})
        self.assertEqual(result.usage["model_calls"], 2)

    def test_foundry_delegates_validator_and_retains_its_failed_batch(self):
        from scisaurus.runtime.capability_foundry import CapabilityFoundry
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests, MINI_EXECUTOR, INTENT
        base = CapabilityFoundryTests._foundry(self.root / "foundry")
        foundry = CapabilityFoundry(base.model_config, runtime_python=base.runtime_python,
            workspace_root=base.workspace_root, registry_root=base.registry_root, repo_root=base.repo_root,
            requirements_file=base.requirements_file, runtime_packages=base.runtime_packages,
            reviewer_client=base.reviewer_client, author_backend=self.config(), max_attempts=2)
        self.assertIsInstance(foundry.validator_client, DshValidatorClient)
        cache = CapabilityFoundryTests._cache(self, self.root / "foundry")
        author = DshAuthorClient(self.config(), root=self.root / "author", runtime_python=sys.executable)
        response = ModelResult(json.dumps({"executor_source": MINI_EXECUTOR, "experiment_intent": INTENT}),
                               "fixed-model", {"model_calls": 2}, 0, "stop")
        error = DshBatchError("validator transport interrupted", receipt=self.root / "validator-receipt",
                              usage={"model_calls": 3, "output_tokens": 12})
        with patch("scisaurus.runtime.dsh_batch.DshAuthorClient", return_value=author), \
                patch.object(author, "complete", return_value=response) as produce, \
                patch.object(foundry.validator_client, "complete", side_effect=error) as validate:
            with self.assertRaises(DshBatchError):
                foundry.generate("bounded comparison", work_cache=cache)
            with self.assertRaises(DshBatchError) as retained:
                foundry.generate("bounded comparison", work_cache=cache)
            self.assertEqual((produce.call_count, validate.call_count), (1, 1))
            self.assertEqual(retained.exception.usage["model_calls"], 5)
            entry = cache.entries()[0]
            self.assertEqual(entry["requests"][-1]["role"], "methods.validator-author")
            packet = json.loads(entry["requests"][-1]["prompt"])
            self.assertNotIn("executor_source", packet)
            self.assertNotIn("metrics", packet)
            entry.update(status="calling", error=None)
            entry["requests"][-1].update(status="started")
            entry["requests"][-1].pop("batch_receipt", None)
            cache.put(entry["cache_ref"].rsplit("/", 1)[-1].split("@")[0], entry)
            with self.assertRaises(DshBatchError) as interrupted:
                foundry.generate("bounded comparison", work_cache=cache)
            self.assertEqual((produce.call_count, validate.call_count), (1, 1))
            self.assertEqual(interrupted.exception.usage["model_calls"], 5)
            self.assertEqual(interrupted.exception.receipt, str(foundry.validator_client.runner.root))


if __name__ == "__main__":
    unittest.main()
