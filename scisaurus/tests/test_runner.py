"""Control-flow integration with explicitly simulated external workers.

Real provider wiring is covered by model/retrieval tests and the live smoke
report; these deterministic workers test acceptance and failure invariants.
"""
from pathlib import Path
import json
import os
import signal
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.config import validate_config
from scisaurus.runtime.runner import ParagraphRunner


def simulated_worker(kind, params, channel):
    if kind != 'model':
        channel.put({'ok':True,'result':{'outcome':'ok','text':'Sleep supports memory.', 'sources':[],
            'source_url':params.get('url','https://example.com/search'), 'capture_sha256':'fixture-capture-hash',
            'metadata':{'representation':'extracted_text'},'capture':{},'raw_response':{},'gaps':[]}})
        return
    assignment=json.loads(params['prompt'])
    mode=params['client']['model']
    if mode=='partial-result':
        Path(str(channel.path)+'.partial').write_bytes(b'{"ok": true, "result":')
        os._exit(1)
    if mode=='timeout':
        time.sleep(5)
        return
    if 'Identify' in assignment['assignment']:
        output={'allegation':'Long-term outcome was not measured.', 'material_impact':'Overstates supplied evidence.',
                'resolution_condition':'Preserve the result and declare unknown long-term outcomes.',
                'rationale':'Only immediate recall is available.'}
    elif 'Decide' in assignment['assignment']:
        output={'decision':'revise','rationale':'A targeted revision can address the failed check.','change_focus':'Qualify the time horizon.'}
        if mode in {'pause-empty-focus', 'pause-without-focus'}:
            output={'decision':'pause','rationale':'The unresolved source question requires evidence before another edit.'}
            if mode == 'pause-empty-focus':
                output['change_focus']=''
    elif 'Rewrite' in assignment['assignment']:
        value={'numeric-drift':'1.01','signed-drift':'-1.0','exponent-drift':'1.0e7'}.get(mode,'1.0')
        output={'text':f'The supplied result remains {value}; long-term outcomes are unknown.',
                'support':[{'source_ref':assignment['sources'][0]['ref'],'quote':'Sleep supports memory.',
                            'supports':'General background only.'}]}
        if mode in {'no-external-claims', 'unsupported-external-claim'}:
            output['support']=[]
        if mode == 'unsupported-external-claim':
            output['text'] += ' All adults benefit from an additional sleep period.'
        if mode == 'internal-control-context':
            output['text'] += ' The user-stated claim F2 is unverified under the acceptance contract.'
    else:
        output={'checks':[{'check_id':'scope','kind':'resolution','outcome':'failed' if mode=='reject' else 'passed',
                           'method':'Compare asserted time horizon with supplied results.','result':'Outcome remains qualified.'},
                          {'check_id':'data','kind':'regression','outcome':'passed','method':'Compare supplied data.',
                           'result':'No new measurements found.'},
                          {'check_id':'source-support','kind':'regression',
                           'outcome':'failed' if mode=='unsupported-external-claim' else 'passed',
                           'method':'Compare every assertion with supplied facts and claimed source support.',
                           'result':'Uncited population claim is unsupported.' if mode=='unsupported-external-claim'
                                    else 'Assertions are supported by supplied facts or the captured source.'},
                          {'check_id':'reader-facing','kind':'regression',
                           'outcome':'failed' if mode=='internal-control-context' else 'passed',
                           'method':'Check for control context or internal identifiers in manuscript prose.',
                           'result':'Internal claim IDs and user instructions are exposed.' if mode=='internal-control-context'
                                    else 'The paragraph expresses the scientific finding and limitation directly.'}],
                'regressions':[], 'uncertainties':[], 'observations':[],
                'rationale':'Candidate checked against baseline and sources.'}
        if mode == 'missing-source-check':
            output['checks']=[check for check in output['checks'] if check['check_id']!='source-support']
        if mode == 'missing-prose-check':
            output['checks']=[check for check in output['checks'] if check['check_id']!='reader-facing']
        if mode in {'observations', 'material-uncertainty', 'incomplete-evidence', 'unjustified-observation'}:
            output['observations']=[{'observation':'The source uses different punctuation.',
                                     'reason_nonblocking':'Punctuation does not change the preserved value or qualified time horizon.'}]
        if mode in {'material-uncertainty', 'pause-empty-focus', 'pause-without-focus'}:
            output['uncertainties']=['The source capture cannot establish which outcome was measured.']
        if mode == 'incomplete-evidence':
            output['checks'][0]['outcome']='insufficient_evidence'
        if mode == 'unjustified-observation':
            del output['observations'][0]['reason_nonblocking']
        if mode=='incomplete-review':
            del output['uncertainties']
    if mode=='paragraph-break' and 'Rewrite' in assignment['assignment']:
        output['text'] += '\n \nThis is another paragraph.'
    text='not JSON' if mode=='invalid-json' else json.dumps(output)
    channel.put({'ok':True,'result':{'text':text,'model':mode,'usage':{'model_calls':1,'input_tokens':100,'output_tokens':50},
                                   'elapsed_seconds':0.01,'finish_reason':'stop'}})


def config(mode='pass'):
    return {'live_dispatch_allowed':True,'data_classification':'public','allocation_mode':'capacity_pool',
            'project_id':'test-project','objective':'Preserve 1.0 and qualify unmeasured long-term outcomes.',
            'paragraph':'The supplied value is 1.0 and proves long-term improvement.',
            'preserved_neighbor':'This paragraph must remain unchanged.',
            'supplied_context':'Only immediate recall was measured; the value was 1.0.',
            'required_literals':['1.0'],'public_queries':['sleep memory'],'source_urls':['https://example.com/source'],
            'mcp_fetch_command':['test-fixture-server'],
            'model':{'base_url':'http://127.0.0.1:1','model':mode,'protocol':'ollama',
                     'timeout_seconds':0.8 if mode=='timeout' else 8,'max_output_tokens':2048},
            'limits':{'max_rounds':2,'search_results':2,'max_capture_chars':1000,'max_source_bytes':10000,'max_result_bytes':100000,'concurrent_calls':2,
                      'wall_clock_seconds':30,'checkpoint_seconds':0.15,'retrieval_timeout_seconds':4}}


class TestParagraphRunner(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='scisaurus-runner-test-')
        self.path=Path(self.temp.name)/'project'
    def tearDown(self):
        self.temp.cleanup()
    def run_mode(self,mode):
        updates=[]
        with patch('scisaurus.runtime.runner._invoke_worker',simulated_worker):
            result=ParagraphRunner(self.path,config(mode),on_progress=updates.append).run()
        self.assertTrue(result['event_chain'][0])
        return result,updates

    def test_candidate_stays_unaccepted_until_independent_verification(self):
        result,updates=self.run_mode('pass')
        self.assertEqual(result['status'],'accepted')
        staged=[item for item in updates if item['phase']=='candidate_staged']
        self.assertEqual(staged[0]['incumbent_ref'],result['baseline_ref'])
        self.assertNotEqual(result['incumbent_ref'],result['baseline_ref'])
        self.assertTrue(result['candidates'][0]['adopted'])
        self.assertEqual(result['usage']['reserved'],{})
        self.assertEqual(len(result['source_captures']),2)
        self.assertTrue((self.path/'output'/'report.md').is_file())
        control=ControlStore(self.path)
        store=ArtifactStore(control)
        events=list(control.replay())
        verification_index=next(i for i,e in enumerate(events) if e['event_type']=='verification.completed')
        adoption_index=max(i for i,e in enumerate(events) if e['event_type']=='artifact.accepted')
        self.assertLess(verification_index,adoption_index)
        self.assertEqual(store.versions('strategy/units/neighbor'),[1])
        control.close()

    def test_failed_reviews_revise_within_limit_and_preserve_baseline(self):
        result,_=self.run_mode('reject')
        self.assertEqual(result['status'],'unresolved')
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])
        self.assertEqual(len(result['candidates']),2)
        self.assertFalse(any(c['adopted'] for c in result['candidates']))
        self.assertEqual(result['usage']['reserved'],{})

    def test_pause_without_revision_focus_preserves_unresolved_candidate(self):
        for mode in ('pause-empty-focus', 'pause-without-focus'):
            self.path=Path(self.temp.name)/mode
            with self.subTest(mode=mode):
                result,_=self.run_mode(mode)
                self.assertEqual(result['status'],'paused')
                self.assertIsNone(result['error'])
                self.assertEqual(result['incumbent_ref'],result['baseline_ref'])
                self.assertEqual(len(result['candidates']),1)
                self.assertEqual(result['usage']['reserved'],{})
                control=ControlStore(self.path)
                store=ArtifactStore(control)
                record=store.head('command/supervision/reassess-1')
                decision=json.loads(store.read_body(record['body_hash']))
                self.assertEqual(decision['decision'],'pause')
                self.assertFalse(decision.get('change_focus'))
                state=control._conn.execute("SELECT state FROM tasks WHERE task_id='reassess-1'").fetchone()[0]
                self.assertEqual(state,'completed')
                control.close()

    def test_nonblocking_observations_are_retained_with_verifier_provenance(self):
        result,_=self.run_mode('observations')
        self.assertEqual(result['status'],'accepted')
        candidate=result['candidates'][0]
        self.assertEqual(candidate['uncertainties'],[])
        self.assertEqual(len(candidate['observations']),1)
        exported=json.loads((self.path/'output/run.json').read_text())
        self.assertEqual(exported['candidates'][0]['observations'],candidate['observations'])
        report=(self.path/'output/report.md').read_text()
        observation=candidate['observations'][0]
        self.assertIn(observation['observation'],report)
        self.assertIn(observation['reason_nonblocking'],report)
        control=ControlStore(self.path)
        store=ArtifactStore(control)
        verdict=store.get(candidate['verdict_ref'])
        execution=json.loads(store.read_body(verdict['body_hash']))
        self.assertEqual(json.loads(execution['text'])['observations'],candidate['observations'])
        control.close()

    def test_material_uncertainties_still_block_with_nonblocking_observations(self):
        result,_=self.run_mode('material-uncertainty')
        self.assertEqual(result['status'],'unresolved')
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])
        self.assertFalse(any(candidate['adopted'] for candidate in result['candidates']))
        self.assertTrue(all(candidate['uncertainties'] and candidate['observations'] for candidate in result['candidates']))

    def test_insufficient_evidence_is_not_overridden_by_observations(self):
        result,_=self.run_mode('incomplete-evidence')
        self.assertEqual(result['status'],'unresolved')
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])
        self.assertFalse(any(candidate['resolved'] for candidate in result['candidates']))

    def test_observation_without_nonblocking_basis_cannot_adopt(self):
        result,_=self.run_mode('unjustified-observation')
        self.assertEqual(result['status'],'blocked')
        self.assertIn('reason_nonblocking',result['error'])
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])

    def test_supplied_facts_do_not_require_inserting_external_claims(self):
        result,_=self.run_mode('no-external-claims')
        self.assertEqual(result['status'],'accepted')
        self.assertEqual(result['candidates'][0]['support'],[])
        self.assertEqual(len(result['source_captures']),2)
        control=ControlStore(self.path)
        store=ArtifactStore(control)
        verification=store.get(result['candidates'][0]['verification_ref'])
        checks=json.loads(store.read_body(verification['body_hash']))['checks']
        source_check=next(check for check in checks if check['check_id']=='source-support')
        self.assertEqual(source_check['outcome'],'passed')
        control.close()

    def test_external_claim_without_support_keeps_baseline(self):
        result,_=self.run_mode('unsupported-external-claim')
        self.assertEqual(result['status'],'unresolved')
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])
        self.assertFalse(any(candidate['adopted'] for candidate in result['candidates']))

    def test_source_coverage_check_cannot_be_omitted(self):
        result,_=self.run_mode('missing-source-check')
        self.assertEqual(result['status'],'blocked')
        self.assertIn('source-support',result['error'])
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])

    def test_internal_control_context_does_not_enter_accepted_prose(self):
        result,_=self.run_mode('internal-control-context')
        self.assertEqual(result['status'],'unresolved')
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])
        self.assertFalse(any(candidate['adopted'] for candidate in result['candidates']))

    def test_reader_facing_check_cannot_be_omitted(self):
        result,_=self.run_mode('missing-prose-check')
        self.assertEqual(result['status'],'blocked')
        self.assertIn('reader-facing',result['error'])
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])

    def test_numeric_drift_cannot_pass_even_if_model_approves(self):
        result,_=self.run_mode('numeric-drift')
        self.assertEqual(result['status'],'unresolved')
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])

    def test_incomplete_review_cannot_close_or_adopt(self):
        result,_=self.run_mode('incomplete-review')
        self.assertEqual(result['status'],'blocked')
        self.assertIn('uncertainties',result['error'])
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])

    def test_timeout_keeps_unknown_cost_and_emits_live_checkpoints(self):
        result,updates=self.run_mode('timeout')
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['usage']['reserved'],{'concurrent_calls':1})
        self.assertGreaterEqual(len([u for u in updates if u['phase']=='executing']),3)
        control=ControlStore(self.path)
        self.assertEqual(control._conn.execute("SELECT state FROM attempts WHERE task_id='supervise'").fetchone()[0],'result_unknown')
        control.close()

    def test_invalid_model_output_is_visible_and_does_not_change_incumbent(self):
        result,_=self.run_mode('invalid-json')
        self.assertEqual(result['status'],'blocked')
        self.assertIn('JSON',result['error'])
        self.assertEqual(result['incumbent_ref'],result['baseline_ref'])

    def test_unconfigured_or_private_run_creates_no_project(self):
        value=config();value['live_dispatch_allowed']=False
        with self.assertRaises(ValidationError):
            ParagraphRunner(self.path,value)
        self.assertFalse(self.path.exists())
        value=config();value['data_classification']='private'
        with self.assertRaises(ValidationError):
            validate_config(value)

    def test_existing_project_is_not_overwritten(self):
        self.run_mode('pass')
        with self.assertRaises(ValidationError):
            ParagraphRunner(self.path,config())

    def test_interrupt_reconciles_dispatched_attempt_and_clears_active_task(self):
        original = ParagraphRunner._checkpoint
        count = 0
        def interrupt(runner, phase, **kwargs):
            nonlocal count
            if phase == "executing" and runner.active_task == "supervise":
                count += 1
                if count == 2:
                    raise KeyboardInterrupt()
            return original(runner, phase, **kwargs)
        with patch.object(ParagraphRunner, "_checkpoint", interrupt):
            result, _ = self.run_mode("timeout")
        self.assertEqual(result["status"], "blocked")
        control = ControlStore(self.path)
        self.assertEqual(control._conn.execute("SELECT state FROM attempts WHERE task_id='supervise'").fetchone()[0], "result_unknown")
        self.assertEqual(control._conn.execute("SELECT state FROM tasks WHERE task_id='supervise'").fetchone()[0], "blocked")
        store = ArtifactStore(control)
        checkpoint = store.head(next(row[0] for row in control._conn.execute(
            "SELECT logical_id FROM artifacts WHERE artifact_type='progress_checkpoint' ORDER BY rowid DESC")))
        body = json.loads(store.read_body(checkpoint["body_hash"]))
        self.assertIsNone(body["next_action"]["active_task"])
        control.close()

    def test_pre_dispatch_failure_releases_capacity_without_unknown_outcome(self):
        original = ParagraphRunner._checkpoint
        def fail_before_dispatch(runner, phase, **kwargs):
            if phase == "executing" and runner.active_task == "supervise":
                raise RuntimeError("checkpoint observer failed")
            return original(runner, phase, **kwargs)
        with patch.object(ParagraphRunner, "_checkpoint", fail_before_dispatch):
            result, _ = self.run_mode("pass")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["usage"]["reserved"], {})
        control = ControlStore(self.path)
        self.assertEqual(control._conn.execute("SELECT state FROM attempts WHERE task_id='supervise'").fetchone()[0], "failed")
        control.close()

    def test_signed_and_exponent_changes_are_not_preserved_numbers(self):
        for mode in ("signed-drift", "exponent-drift"):
            self.path = Path(self.temp.name) / mode
            with self.subTest(mode=mode):
                result, _ = self.run_mode(mode)
                self.assertEqual(result["status"], "unresolved")
                self.assertEqual(result["incumbent_ref"], result["baseline_ref"])

    def test_whitespace_separated_paragraphs_are_rejected(self):
        result, _ = self.run_mode("paragraph-break")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertEqual(result["usage"]["reserved"], {})

    def test_partial_worker_result_cannot_block_parent_deadline(self):
        started = time.monotonic()
        result, _ = self.run_mode("partial-result")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["usage"]["reserved"], {"concurrent_calls": 1})

    def test_sigterm_uses_reconciliation_and_restores_callers_handler(self):
        original = ParagraphRunner._checkpoint
        previous_handler = signal.getsignal(signal.SIGTERM)
        count = 0
        def terminate_during_call(runner, phase, **kwargs):
            nonlocal count
            if phase == "executing" and runner.active_task == "supervise":
                count += 1
                if count == 2:
                    os.kill(os.getpid(), signal.SIGTERM)
            return original(runner, phase, **kwargs)
        with patch.object(ParagraphRunner, "_checkpoint", terminate_during_call):
            result, _ = self.run_mode("timeout")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous_handler)
        control = ControlStore(self.path)
        self.assertEqual(control._conn.execute("SELECT state FROM attempts WHERE task_id='supervise'").fetchone()[0], "result_unknown")
        self.assertEqual(control._conn.execute("SELECT state FROM tasks WHERE task_id='supervise'").fetchone()[0], "blocked")
        control.close()
