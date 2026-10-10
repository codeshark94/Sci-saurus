"""Durable producer history stays separate from independent validation."""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.development_session import DevelopmentSession, development_session_binding
from scisaurus.runtime.dsh_batch import DshBatchRunner, DshAuthorClient, DshValidatorClient, sha256
from scisaurus.runtime.program_sandbox import SANDBOX_EXEC, sandbox_profile


FAKE = '''import json,sys
from pathlib import Path
def emit(x):print(json.dumps(x),flush=True)
for line in sys.stdin:
 r=json.loads(line);p=r.get('params',{})
 if r['method']=='initialize':emit({'id':r['id'],'result':{}})
 elif r['method']=='session/resume':
  assert Path('session.txt').read_text()==p['sessionId']
  emit({'id':r['id'],'result':{'sessionId':p['sessionId']}})
 elif r['method']=='session/prompt':
  n=int(Path('count.txt').read_text())+1 if Path('count.txt').exists() else 1
  Path('count.txt').write_text(str(n));Path('session.txt').write_text(p['sessionId'])
  Path('answer.txt').write_text(str(n))
  sid=p['sessionId'];mid='message-'+str(n)
  def event(k,d):emit({'method':'session.event','params':{'sessionId':sid,'event':{'type':k,'data':d}}})
  emit({'id':r['id'],'result':{'messageId':mid}})
  event('agent/inbox/spliced',{'inserted':[{'id':mid}]})
  event('assistant/message',{'turn':n,'step':1,'usage':{'inputTokens':21,'outputTokens':8}})
  event('turn/end',{'turn':n,'reason':{'kind':'completed'}})
  emit({'method':'session.status','params':{'sessionId':sid,'status':'idle'}})
'''


class DevelopmentSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name).resolve()
        self.binding = development_session_binding(self.project)
        script = self.project / "transport.py"
        script.write_text(FAKE)
        composition = self.project / "composition.json"
        composition.write_text('{}')
        paths = [Path(sys.executable).absolute(), script, composition]
        self.config = {"schema_version": "dsh-batch-1", "command": [str(paths[0]), str(script)],
                       "pinned_files": {str(p): sha256(p) for p in paths}, "read_roots": [str(self.project)],
                       "composition": str(composition), "provider": "test", "model": "fixed-model",
                       "auth_env": "DSH_TEST_KEY", "timeout_seconds": 5, "max_output_tokens": 32768}
        self.environment = patch.dict(os.environ, {"DSH_TEST_KEY": "offline"})
        self.environment.start()
        os.environ.pop("SCISAURUS_RUN_CONTROL", None)
        os.environ.pop("SCISAURUS_RUN_GENERATION", None)

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def dispatch(self, stage="topic"):
        return DshBatchRunner(self.config, root=self.project / stage,
            development_session=self.binding).run("produce", inputs={"assignment.json": b'{}'}, outputs=["answer.txt"])

    def test_history_workspace_and_usage_continue_across_producer_boundaries(self):
        first, second = self.dispatch(), self.dispatch("author")
        a, b = [json.loads(Path(r["receipt"]).read_text()) for r in (first, second)]
        self.assertEqual(a["session_id"], b["session_id"])
        self.assertFalse(a["development_session"]["resumed"])
        self.assertTrue(b["development_session"]["resumed"])
        self.assertEqual(first["files"]["answer.txt"], b'1')
        self.assertEqual(second["files"]["answer.txt"], b'2')
        self.assertEqual(a["usage"], b["usage"])
        self.assertEqual(a["usage"]["model_calls"], 1)
        self.assertEqual((Path(first["receipt"]).parent / "outputs/answer.txt").read_bytes(), b'1')

    def test_changed_backend_and_receipt_fail_closed(self):
        first = self.dispatch()
        changed = dict(self.config, model="another-model")
        with self.assertRaisesRegex(ValidationError, "backend changed"):
            with DevelopmentSession(self.binding).lease(changed, time.monotonic()+1):pass
        Path(first["receipt"]).write_text('{}')
        with self.assertRaisesRegex(ValidationError, "receipt changed"):
            self.dispatch("author")

    def test_changed_archived_output_cannot_be_resumed(self):
        first = self.dispatch()
        archived = Path(first["receipt"]).parent / "outputs/answer.txt"
        archived.chmod(0o644)
        archived.write_text('tampered')
        with self.assertRaisesRegex(ValidationError, "history output changed"):
            self.dispatch("author")

    def test_unsettled_session_does_not_dispatch_another_producer(self):
        self.dispatch()
        p = Path(self.binding["root"]) / "session.json"
        d=json.loads(p.read_text());d['active_job']='unresolved';p.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValidationError, "unsettled"):
            self.dispatch("author")

    def test_independent_validator_never_receives_development_handle(self):
        author = DshAuthorClient(self.config, root=self.project/'author', runtime_python=sys.executable,
                                 development_session=self.binding)
        validator = DshValidatorClient(self.config, root=self.project/'validator', runtime_python=sys.executable)
        self.assertIsNotNone(author.runner.development_session)
        self.assertIsNone(validator.runner.development_session)

    def test_foreign_project_root_is_rejected(self):
        with self.assertRaises(ValidationError):
            DevelopmentSession({"project_id": str(self.project/'other'), "root": self.binding['root']})

    def test_controller_owned_external_foundry_receipt_retains_project_history(self):
        self.dispatch()
        with tempfile.TemporaryDirectory() as outside:
            result = DshBatchRunner(self.config, root=Path(outside),
                development_session=self.binding).run('author',inputs={'assignment.json':b'{}'},outputs=['answer.txt'])
            self.assertEqual(result['files']['answer.txt'], b'2')
            self.assertEqual(self.dispatch('next')['files']['answer.txt'], b'3')

    def test_copied_same_byte_receipt_has_no_job_owner(self):
        first = self.dispatch()
        p=Path(self.binding['root'])/'session.json'
        d=json.loads(p.read_text())
        foreign=self.project/'copied/receipt.json';foreign.parent.mkdir()
        foreign.write_bytes(Path(first['receipt']).read_bytes())
        d['calls'][0]['receipt']=str(foreign);p.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValidationError, 'settled, reaped'):
            self.dispatch('next')

    @unittest.skipUnless(SANDBOX_EXEC, "Seatbelt directory boundary required")
    def test_directory_sync_does_not_grant_ancestor_file_access(self):
        work = self.project / 'directory-proof'
        work.mkdir()
        (self.project / 'private.txt').write_text('private')
        profile = sandbox_profile(work, [sys.executable], directory_sync_paths=work.parents)
        program = ("import os\nf=os.open('..',os.O_RDONLY);os.fsync(f);os.close(f)\n"
                   "try:open('../private.txt').read()\n"
                   "except PermissionError:print('directory synced; file denied')\n"
                   "else:raise RuntimeError('ancestor file exposed')")
        result = subprocess.run([SANDBOX_EXEC, '-p', profile, sys.executable, '-c', program],
                                cwd=work, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'directory synced; file denied')
