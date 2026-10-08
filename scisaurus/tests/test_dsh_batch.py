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
        self.assertFalse((self.root / "jobs").exists())

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
