import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.errors import StateError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.composer import ComposerRunner
from scisaurus.tests import test_composer as composer_fixtures


class StageAssignmentOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(Path(temporary.name)))
        self.addCleanup(self.runner.close)
        self.stage = self.runner.workflow['stages'][0]

    def admit(self, number, owner):
        return self.runner.departments.begin_stage('survey', 'survey', attempt_number=number,
            input_ref=owner, deadline_seconds=20, active_role_ids=['search-strategist'])

    def test_input_collision_rejected_before_any_mutation(self):
        owner = {'kind': 'composer_stage_task', 'ref': 'old', 'digest': 'old'}
        first = self.admit(2, owner)
        before = self.runner.control._conn.total_changes
        with self.assertRaisesRegex(StateError, 'different stage input'):
            self.admit(2, {**owner, 'ref': 'new', 'digest': 'new'})
        self.assertEqual(before, self.runner.control._conn.total_changes)
        self.assertEqual(first['assignments'][0]['input_ref'], owner)
        replay = self.admit(2, owner)
        self.assertEqual(first['task_ids'], replay['task_ids'])
        with self.assertRaisesRegex(StateError, 'different stage input'):
            self.runner.departments.begin_stage('survey', 'experiment', attempt_number=2,
                input_ref=owner, deadline_seconds=20, active_role_ids=[])

    def test_sparse_history_and_durable_admissions_allocate_monotonically(self):
        self.admit(8, {'ref': 'prior'})
        self.runner.stage_records['survey'] = {'attempts': [{'attempt_number': 1}], 'attempt_count': 1}
        self.assertEqual(self.runner._next_stage_attempt_number('survey'), 9)
        self.runner.tasks.create('aggregate', 'production', {}, 'command.composer')
        self.runner.tasks.admit('aggregate', 'command.composer')
        self.runner.tasks.start_attempt('aggregate', 'aggregate-attempt', owner='command.composer',
            lease_ttl_seconds=20, payload={'stage_id': 'survey', 'attempt_number': 12})
        self.assertEqual(self.runner._next_stage_attempt_number('survey'), 13)
        self.assertEqual(self.runner._next_stage_attempt_number('experiment'), 1)
        self.runner.stage_records['survey']['attempt_number'] = 19
        self.assertEqual(self.runner._next_stage_attempt_number('survey', [{'attempt_number': 3}]), 20)

    def legacy_collision(self):
        runner = self.runner
        task_id = 'aggregate-current'
        runner.tasks.create(task_id, 'production', {}, 'command.composer')
        runner.tasks.admit(task_id, 'command.composer')
        runner.tasks.start_attempt(task_id, 'aggregate-current-attempt', owner='command.composer',
            lease_ttl_seconds=20, payload={'stage_id': 'survey', 'attempt_number': 2,
                'model_budget_cycle': 0, 'project_dir': self.stage['project_dir']})
        runner.tasks.finish_attempt('aggregate-current-attempt', 'failed')
        runner.tasks.transition(task_id, 'blocked', 'command.composer')
        owner = {'kind': 'composer_stage_task', 'ref': task_id,
            'digest': hashlib.sha256(canonical_bytes({'stage_id': 'survey', 'attempt_number': 2,
                'project_dir': self.stage['project_dir']})).hexdigest()}
        self.admit(2, {'kind': 'composer_stage_task', 'ref': 'aggregate-prior', 'digest': 'old'})
        # Construct the admission emitted before input ownership was checked.
        with patch.object(runner.departments, '_assignment_task_rows', return_value=[]):
            plan = self.admit(2, owner)
        row = plan['assignments'][0]
        execution = runner._publish(row['assignment_logical_id'] + '/execution', 'report', {
            'schema_version': 'specialist-execution-1', 'project_id': runner.workflow['project_id'],
            'stage_id': 'survey', 'stage_kind': 'survey', 'attempt_number': 2,
            **{key: row[key] for key in ('task_id', 'assignment_id', 'assigned_role', 'role_id', 'input_ref')},
            'report': {'status': 'succeeded', 'usage': {'model_calls': 1}}}, row['assigned_role'])
        record = {'status': 'candidate_needs_review', 'task_id': task_id,
            'attempt_id': 'aggregate-current-attempt', 'attempt_number': 2,
            'assignment_plan_ref': plan['plan_ref'], 'failure_debt': {'failure_class': 'mechanical_contract'}}
        runner.stage_records['survey'] = record
        return plan, execution

    def test_legacy_collision_recovers_once_without_payment_or_scientific_release(self):
        plan, execution = self.legacy_collision()
        self.runner.stage_records['survey']['composer_decision'] = 'advance_with_findings'
        self.runner.context['survey'] = {'status': 'research_expansion_required',
            'preserved_source_ref': 'original-source', 'composer_decision': 'advance_with_findings'}
        with self.assertRaisesRegex(StateError, 'exact admitted assignment'):
            self.runner._stage_specialist_payment_proof(self.stage, plan['plan_ref'], execution['artifact_ref'])
        usage = deepcopy(self.runner.usage)
        deadline = self.runner.deadline_epoch
        self.assertEqual(self.runner._reconcile_stage_assignment_ownership(), ['survey'])
        self.assertEqual(self.runner.stage_records['survey']['status'], 'retrying')
        self.assertEqual(self.runner._reconcile_stage_assignment_ownership(), [])
        self.assertEqual(self.runner.usage, usage)
        self.assertEqual(self.runner.deadline_epoch, deadline)
        self.assertEqual(self.runner.continuation_cycles, 0)
        self.assertEqual(self.runner.context['survey']['status'], 'retrying')
        self.assertEqual(self.runner.context['survey']['preserved_source_ref'], 'original-source')
        self.assertNotIn('composer_decision', self.runner.stage_records['survey'])
        self.assertEqual(self.runner._reconcile_pending_stage_holds({'survey': self.stage}), [])
        self.assertEqual(self.runner._next_stage_attempt_number('survey'), 3)
        self.assertFalse(self.runner._stage_releases_dependencies(self.runner.stage_records['survey'], stage_kind='survey'))

    def test_wrong_owner_cycle_or_scientific_rejection_cannot_recover(self):
        self.legacy_collision()
        record = self.runner.stage_records['survey']
        for key, value in [('task_id', 'other'), ('attempt_number', 8),
                           ('failure_debt', {'failure_class': 'scientific'})]:
            old = record[key]
            record[key] = value
            self.assertEqual(self.runner._reconcile_stage_assignment_ownership(), [])
            record[key] = old
        self.runner.continuation_cycles = 1
        self.runner.reopened_stage_ids.add('survey')
        self.assertEqual(self.runner._reconcile_stage_assignment_ownership(), [])

    def test_new_assignment_payment_matches_and_is_charged_once(self):
        self.legacy_collision()
        self.runner._reconcile_stage_assignment_ownership()
        number = self.runner._next_stage_attempt_number('survey')
        plan = self.admit(number, {'kind': 'composer_stage_task', 'ref': 'fresh-owner'})
        row = plan['assignments'][0]
        execution = self.runner._publish(row['assignment_logical_id'] + '/execution', 'report', {
            'schema_version': 'specialist-execution-1', 'project_id': self.runner.workflow['project_id'],
            'stage_id': 'survey', 'stage_kind': 'survey', 'attempt_number': number,
            **{key: row[key] for key in ('task_id', 'assignment_id', 'assigned_role', 'role_id', 'input_ref')},
            'report': {'status': 'succeeded', 'usage': {'model_calls': 1, 'input_tokens': 500}}}, row['assigned_role'])
        before = self.runner.usage['model_calls']
        for _ in range(2):
            self.runner._settle_stage_specialist_payment(self.stage, plan['plan_ref'], execution['artifact_ref'])
        self.assertEqual(self.runner.usage['model_calls'], before + 1)


if __name__ == '__main__':
    unittest.main()
