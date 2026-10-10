"""Explicit accounting release preserves interrupted development history."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager
from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.development_session import DevelopmentSession
from scisaurus.tests import test_development_session as fixtures


class DevelopmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DevelopmentSessionTests()
        self.fixture.setUp()
        self.project, self.config, self.binding = self.fixture.project, self.fixture.config, self.fixture.binding
        self.path = Path(self.fixture.dispatch()['receipt'])
        receipt = json.loads(self.path.read_bytes())
        receipt.update(status='result_unknown', outcome_known=False)
        self.path.write_bytes(canonical_bytes(receipt))
        self.original = self.path.read_bytes()
        state_path = Path(self.binding['root']) / 'session.json'
        state = json.loads(state_path.read_bytes())
        state.update(calls=[], active_job=str(self.path))
        state_path.write_bytes(canonical_bytes(state))
        self.control = ControlStore(self.project)
        self.store = ArtifactStore(self.control)
        self.store.init_project()
        tasks = TaskManager(self.control)
        tasks.create('parent', 'service', {'operation': 'model', 'stage_id': 'experiment'}, 'command.composer')
        tasks.admit('parent', 'command.composer')
        tasks.start_attempt('parent', 'attempt', owner='command.composer', lease_ttl_seconds=30, payload={'model_budget_cycle': 1, 'stage_id': 'experiment'})
        usage = {k: receipt['usage'].get(k, 0) for k in ('model_calls', 'input_tokens', 'output_tokens', 'openalex_requests')}
        identity = {'topic': {'id': 'concept', 'research_question': 'question', 'domain': 'physics'}}
        digest = hashlib.sha256(canonical_bytes(identity)).hexdigest()
        logical = f'command/scientific-software-assessments/{digest}'
        request = self.publish(logical+'/request', {'schema_version': 'scientific-software-request-1', **identity})
        owner = {'attempt_id': 'attempt', 'cycle': 1}
        budget = self.publish('command/capability-repair-budget/owned', {'evidence_request_ref': request,
            'model_budget_owner': owner, 'panel_stage_id': 'experiment-repair-panel-fixture', 'assignment_attempt_number': 1})
        producer = self.publish('command/departments/methods/producer/execution', {
            'project_id': str(self.project), 'assigned_role': 'methods.methodologist',
            'stage_id': 'experiment-repair-panel-fixture', 'attempt_number': 1,
            'input_ref': {'ref': budget, 'digest': digest, 'kind': 'scientific_software_assessment', 'stage_id': 'experiment'},
            'report': {'status': 'result_unknown', 'dsh_receipt': str(self.path),
                       'usage': {key: value for key, value in usage.items() if key != 'openalex_requests'}}}, 'methods.methodologist')
        panel_id = 'experiment-repair-panel-fixture'
        self.plan = self.publish('command/departments/methods/plan', {'schema_version': 'department-stage-assignment-1',
            'project_id': str(self.project), 'stage_kind': 'experiment', 'stage_id': panel_id, 'attempt_number': 1})
        chief = self.publish('command/departments/methods/chief', {'schema_version': 'department-chief-synthesis-1',
            'project_id': str(self.project), 'stage_kind': 'experiment', 'stage_id': panel_id, 'attempt_number': 1, 'usage': usage})
        invoice_id = hashlib.sha256(canonical_bytes({'stage_id': 'experiment', 'assignment_plan_ref': self.plan})).hexdigest()
        invoice = self.publish('command/capability-repair-usage/'+invoice_id, {'schema_version': 'capability-repair-usage-1', 'stage_id': 'experiment',
            'chief_synthesis_ref': chief, 'input_sha256': digest, 'assignment_plan_ref': self.plan, 'usage': usage, 'model_budget_owner': owner})
        self.assessment = self.publish(logical+'/failure', {'status': 'blocked', 'identity': identity,
            'producer_execution_ref': producer, 'usage_invoice_ref': invoice, 'evidence': {'request_ref': request, 'request': {'schema_version': 'scientific-software-request-1', **identity}}, 'ledger': {'assignment_plan_ref': self.plan}})
        self.checkpoint_body = {'schema_version': 'composer-checkpoint-1', 'workflow_id': 'mission', 'stages': {'experiment': {'attempt_id': 'attempt', 'attempts': [{'attempt_id': 'attempt', 'cycle': 1}]}}, 'status': 'blocked', 'stage_usage_totals': {'repair-panel:experiment:'+invoice_id: usage},
            'pending_stage_usage': {}}
        self.checkpoint = self.publish('command/composer/checkpoints/1', self.checkpoint_body)
        tasks.finish_attempt('attempt', 'failed', usage=usage)
        self.development = DevelopmentSession(self.binding)

    def publish(self, logical, body, author='command.composer'):
        return self.store.publish_artifact(logical_id=logical, artifact_type='note', author=author,
            body=canonical_bytes(body), media_type='application/json')['artifact_ref']

    def recover(self):
        return self.development.reconcile_unknown(self.config, self.store,
            assessment_ref=self.assessment, checkpoint_ref=self.checkpoint, receipt_sha256=hashlib.sha256(self.original).hexdigest())

    def tearDown(self):
        self.control.close()
        self.fixture.tearDown()

    def test_explicit_paid_recovery_preserves_unknown_and_resumes_history(self):
        first, second = self.recover(), self.recover()
        self.assertEqual(first['artifact_ref'], second['artifact_ref'])
        self.assertEqual(self.path.read_bytes(), self.original)
        with self.development.lease(self.config, time.monotonic()+1) as development:
            self.assertTrue(development.resume)
            self.assertEqual(len(development.state['calls']), 1)
            self.assertIsNone(development.state['active_job'])
            self.assertEqual(development.transport_history[0]['status'], 'result_unknown')
        self.assertEqual(self.fixture.dispatch('next')['files']['answer.txt'], b'2')
        self.assertEqual(self.control._conn.execute('select state from attempts where attempt_id=?', ('attempt',)).fetchone()[0], 'failed')

    def test_unaccounted_namespace_or_live_process_is_rejected(self):
        bad = deepcopy(self.checkpoint_body)
        bad['stage_usage_totals'] = {}
        self.checkpoint = self.publish('command/composer/checkpoints/2', bad)
        with self.assertRaisesRegex(ValidationError, 'paid ownership'):
            self.recover()
        self.checkpoint = self.publish('command/composer/checkpoints/3', self.checkpoint_body)
        with patch('scisaurus.runtime.development_session.os.kill', return_value=None):
            with self.assertRaisesRegex(ValidationError, 'still alive'):
                self.recover()
        self.assertEqual(json.loads(self.development.path.read_bytes())['active_job'], str(self.path))

    def test_foreign_producer_and_changed_archived_source_are_rejected(self):
        manifest = self.store.get(self.assessment)
        assessment = json.loads(self.store.read_body(manifest['body_hash']))
        producer = json.loads(self.store.read_body(self.store.get(assessment['producer_execution_ref'])['body_hash']))
        producer['project_id'] += '-foreign'
        assessment['producer_execution_ref'] = self.publish('command/departments/methods/foreign/execution', producer, 'methods.methodologist')
        original = self.assessment
        self.assessment = self.publish(self.store.get(original)['artifact_id'], assessment)
        with self.assertRaisesRegex(ValidationError, 'paid ownership'):
            self.recover()
        self.assessment = original
        source = self.path.parent / 'input/assignment.json'
        source.chmod(0o644)
        source.write_bytes(b'{"altered":true}')
        with self.assertRaisesRegex(ValidationError, 'evidence changed'):
            self.recover()

    def test_reconciliation_or_paid_evidence_tampering_fails_on_next_lease(self):
        record = self.recover()
        body = Path(self.store.objects_dir) / record['body_hash']
        body.write_bytes(b'{}')
        with self.assertRaisesRegex(ValidationError, 'reconciliation proof'):
            with self.development.lease(self.config, time.monotonic()+1):
                pass

    def test_original_receipt_hash_prevents_transport_boundary_substitution(self):
        receipt = json.loads(self.original)
        receipt['transport_turns'] = [999999]
        self.path.write_bytes(canonical_bytes(receipt))
        with self.assertRaisesRegex(ValidationError, 'original receipt changed'):
            self.recover()

    def test_foreign_parent_stage_and_pending_payment_do_not_release_dispatch(self):
        self.control._conn.execute("UPDATE tasks SET payload_json=? WHERE task_id='parent'",
                                   (canonical_bytes({'stage_id': 'foreign'}).decode(),))
        with self.assertRaisesRegex(ValidationError, 'parent.*ownership'):
            self.recover()
        self.control._conn.execute("UPDATE tasks SET payload_json=? WHERE task_id='parent'",
                                   (canonical_bytes({'stage_id': 'experiment'}).decode(),))
        bad = deepcopy(self.checkpoint_body)
        namespace = next(iter(bad['stage_usage_totals']))
        bad['pending_stage_usage'][namespace] = {'delta': bad['stage_usage_totals'][namespace]}
        self.checkpoint = self.publish('command/composer/checkpoints/2', bad)
        with self.assertRaisesRegex(ValidationError, 'paid ownership'):
            self.recover()

    def test_sparse_invoice_cannot_omit_recorded_model_usage(self):
        self.control._conn.execute("UPDATE attempts SET usage_json=? WHERE attempt_id='attempt'",
            (canonical_bytes({'actual': {'model_calls': 1, 'input_tokens': 21, 'output_tokens': 8}}).decode(),))
        assessment = json.loads(self.store.read_body(self.store.get(self.assessment)['body_hash']))
        invoice = json.loads(self.store.read_body(self.store.get(assessment['usage_invoice_ref'])['body_hash']))
        invoice['usage'] = {'openalex_requests': 0}
        assessment['usage_invoice_ref'] = self.publish(self.store.get(assessment['usage_invoice_ref'])['artifact_id'], invoice)
        self.assessment = self.publish(self.store.get(self.assessment)['artifact_id'], assessment)
        with self.assertRaises(ValidationError):
            self.recover()

    def test_producer_attempt_cannot_borrow_another_paid_panel_attempt(self):
        assessment = json.loads(self.store.read_body(self.store.get(self.assessment)['body_hash']))
        producer = json.loads(self.store.read_body(self.store.get(assessment['producer_execution_ref'])['body_hash']))
        budget = json.loads(self.store.read_body(self.store.get(producer['input_ref']['ref'])['body_hash']))
        producer['attempt_number'] = 2
        budget['assignment_attempt_number'] = 2
        producer['input_ref']['ref'] = self.publish(self.store.get(producer['input_ref']['ref'])['artifact_id'], budget)
        assessment['producer_execution_ref'] = self.publish(self.store.get(assessment['producer_execution_ref'])['artifact_id'], producer, 'methods.methodologist')
        self.assessment = self.publish(self.store.get(self.assessment)['artifact_id'], assessment)
        with self.assertRaisesRegex(ValidationError, 'paid ownership'):
            self.recover()
