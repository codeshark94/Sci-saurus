"""Unbounded missions retain bounded executions and cumulative support time."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import StateError, ValidationError
from scisaurus.runtime.composer import ComposerRunner, ComposerStageDeadlineExceeded
from scisaurus.runtime.composer_supervisor import ComposerSupervisor, _remaining
from scisaurus.runtime.mission_time import MissionTimeLedger, validate_mission_time_policy
from scisaurus.tests import test_concept_differentiation as concept_fixtures

POLICY = {"mode": "unbounded", "support_seconds": 18000, "design_fraction_target": .95}

class MissionTimeTests(unittest.TestCase):
    def test_allocation_is_cumulative_and_excludes_pause(self):
        now = [0.0]; clock = lambda: now[0]
        ledger = MissionTimeLedger(POLICY, clock=clock)
        ledger.switch('survey'); now[0] = 20; ledger.switch('experiment')
        now[0] = 50; ledger.switch(None); now[0] = 1000
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot['used_seconds'], {'design': 30.0, 'support': 20.0})
        restored = MissionTimeLedger(POLICY, clock=clock, restored=snapshot['used_seconds'])
        restored.switch('paper'); now[0] += 10
        self.assertEqual(restored.snapshot()['used_seconds']['support'], 30)
        self.assertEqual(snapshot['design_fraction_observed'], .6)

    def test_rejects_invalid_allocation_and_nonfinite(self):
        for delta in ({'support_seconds': True}, {'support_seconds': float('inf')},
                      {'design_fraction_target': 0}, {'mode': 'anything'}, {'extra': 1}):
            with self.assertRaises(ValidationError): validate_mission_time_policy({**POLICY, **delta})

    def stopped(self, root, clock):
        workflow = concept_fixtures.ConceptDifferentiationTests().workflow(root)
        runner = ComposerRunner(workflow, clock=clock)
        runner.status = 'paused'; runner._checkpoint('paused', force=True); runner.close()
        (root/'composer/output/run-control.json').write_text('{"stop_requested":true}')
        return workflow, ComposerRunner(workflow, resume=True, control_only=True, clock=clock)

    def test_operator_policy_resume_preserves_usage_input_and_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); now = [100.]; clock = lambda: now[0]
            workflow, runner = self.stopped(root, clock)
            original = deepcopy(workflow); usage = deepcopy(runner.usage)
            result = runner.set_mission_time_policy(POLICY)
            self.assertIsNone(runner.deadline_epoch)
            self.assertEqual(workflow, original); self.assertEqual(usage, runner.usage)
            runner.mission_time_ledger.switch('survey'); now[0] += 30
            runner.status = 'paused'; runner._checkpoint('paused', force=True); runner.close()
            now[0] += 500
            runner = ComposerRunner(workflow, resume=True, control_only=True, clock=clock)
            try:
                self.assertEqual(runner.mission_time_ledger.snapshot()['used_seconds']['support'], 30)
                self.assertEqual(runner.set_mission_time_policy(POLICY)['policy_ref'], result['policy_ref'])
                self.assertEqual(runner.mission_time_ledger.snapshot()['used_seconds']['support'], 30)
                with self.assertRaises(StateError): runner.set_mission_time_policy({**POLICY,'support_seconds':19000})
                self.assertIsNone(runner._mission_remaining_snapshot())
                self.assertFalse(runner._deadline_exhausted())
                self.assertGreater(runner._remaining(), 0)
                self.assertLess(runner._remaining(), float('inf'))
                runner._checkpoint('paused', force=True)
                state = json.loads((root/'composer/output/progress.json').read_text())
                self.assertIsNone(state['remaining_seconds']); self.assertIsNone(state['deadline_at_epoch'])
                json.dumps(state, allow_nan=False)
            finally: runner.close()

    def test_initial_policy_accepts_only_exact_supervisor_pause_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); workflow=concept_fixtures.ConceptDifferentiationTests().workflow(root)
            runner=ComposerRunner(workflow)
            runner.status='running'; runner._checkpoint('survey:running', force=True); runner.close()
            supervisor=ComposerSupervisor.__new__(ComposerSupervisor)
            supervisor.workflow=workflow
            supervisor._mark_interrupted_checkpoint()
            (root/'composer/output/run-control.json').write_text('{"stop_requested":true}')
            runner=ComposerRunner(workflow, resume=True, control_only=True)
            try:
                self.assertEqual(runner.set_mission_time_policy(POLICY)['policy'], POLICY)
                self.assertEqual(runner.mission_time_ledger.snapshot()['used_seconds'], {'design':0.,'support':0.})
            finally: runner.close()

    def test_time_control_and_concept_reselection_share_the_same_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[0.]; root=Path(tmp)
            workflow, runner = self.stopped(root, lambda: now[0])
            ref=runner.set_mission_time_policy(POLICY)['policy_ref']; runner.close()
            runner=ComposerRunner(workflow, resume=True, control_only=True, clock=lambda:now[0])
            try:
                runner.reselect_concepts('Compare different computable mechanisms with a small first test.')
                self.assertEqual(runner.mission_time_policy_ref, ref)
                self.assertIsNone(runner.deadline_epoch)
                self.assertEqual(runner._topic_refinement_context(workflow['stages'][0])['mode'], 'concept_reselection')
            finally: runner.close()

    def test_support_attempt_clamped_but_design_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.]; workflow, runner = self.stopped(Path(tmp), lambda: now[0])
            try:
                runner.set_mission_time_policy({**POLICY, 'support_seconds': 5})
                stage = next(s for s in workflow['stages'] if s['kind']=='survey')
                self.assertLessEqual(runner._stage_remaining(stage), 5)
                now[0] += 6
                with self.assertRaises(ComposerStageDeadlineExceeded): runner._stage_remaining(stage)
                topic = workflow['stages'][0]
                self.assertGreater(runner._stage_remaining(topic), 5)
                self.assertEqual(runner.mission_time_ledger.snapshot()['used_seconds']['support'], 6)
            finally: runner.close()

    def test_interrupted_active_interval_uses_admitted_fence_even_when_marked_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [0.]; root = Path(tmp)
            workflow, runner = self.stopped(root, lambda: now[0])
            runner.set_mission_time_policy(POLICY)
            runner.status = 'running'
            runner.stage_records['survey'] = {'status':'running','attempt_deadline_at_epoch':300.}
            runner.mission_time_ledger.switch('survey')
            with patch('scisaurus.runtime.composer.time.time', return_value=100.):
                runner._checkpoint('survey:running', force=True)
            state = deepcopy(runner._progress_snapshot)
            self.assertEqual(state['mission_time_allocation']['accounting_until_epoch'], 300.)
            state['status'] = 'paused'; state['stop_reason']='process_interrupted'
            state['updated_at_epoch'] = 150.
            runner._restored_mission_time_state = state
            runner.mission_time_ledger = None
            with patch('scisaurus.runtime.composer.time.time', return_value=450.):
                runner._restore_mission_time_policy()
            self.assertEqual(runner.mission_time_ledger.snapshot()['used_seconds']['support'], 50.)
            runner.close()

    def test_supervisor_unbounded_null_is_not_zero_or_original_deadline(self):
        progress = {'remaining_seconds':None,'deadline_at_epoch':None,'mission_time_policy':POLICY}
        self.assertEqual(_remaining(progress), float('inf'))
        supervisor = ComposerSupervisor.__new__(ComposerSupervisor)
        supervisor.workflow = {'time_policy':{'hard_seconds':1}}
        self.assertIsNone(supervisor._watchdog_remaining({'progress':progress}))
        self.assertFalse(supervisor._deadline_expired({'progress':progress}))
        self.assertEqual(_remaining({'remaining_seconds':0}), 0)
        self.assertTrue(supervisor._deadline_expired({'progress':{'remaining_seconds':0}}))
