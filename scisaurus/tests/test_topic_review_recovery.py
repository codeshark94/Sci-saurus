"""Independent review failures preserve their admitted producer input."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.topic_discovery import TopicDiscoveryRunner
from scisaurus.tests.test_concept_differentiation import review
from scisaurus.tests.test_material_development import concept_candidates
from scisaurus.tests.test_topic_discovery import package


class TopicReviewRecoveryTests(unittest.TestCase):
    def test_configured_role_routes_override_duplicate_legacy_fallbacks(self):
        config = {'base_url': 'http://offline.invalid', 'protocol': 'ollama', 'model': 'legacy',
            'timeout_seconds': 1, 'max_output_tokens': 1000,
            'role_models': {'research.topic-source-challenger': {'model': 'legacy'}},
            'role_model_fallbacks': {'research.topic-source-challenger': [{'model': 'legacy'}]},
            'role_routes': {'research.topic-source-challenger': [
                {'id': 'first', 'pool': 'offline', 'model': 'first', 'base_url': 'http://first.invalid', 'protocol': 'ollama'},
                {'id': 'second', 'pool': 'offline', 'model': 'second', 'base_url': 'http://second.invalid', 'protocol': 'ollama'}]}}
        runner = TopicDiscoveryRunner(config)
        self.assertEqual([runner._client('research.topic-source-challenger').model for _ in range(3)],
                         ['first', 'second', 'first'])

    def test_pending_review_blocks_forward_progress_and_restores_dependencies(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.tests.test_concept_differentiation import ConceptDifferentiationTests
        with tempfile.TemporaryDirectory() as tmp:
            runner = ComposerRunner(ConceptDifferentiationTests().workflow(Path(tmp)))
            try:
                stage = runner.workflow['stages'][0]
                scope = runner._topic_source_review_scope(stage, {'candidate_count': 3, 'intake_mode': 'concept'}, None)
                packet = {'objective': scope['objective'], 'candidate_count': 3, 'intake_mode': 'concept',
                          'status': 'pending', 'usage': {}}
                runner._record_topic_source_review(stage, scope, packet, attempt_number=4)
                error = ValidationError('incomplete review')
                self.assertFalse(runner._composer_can_advance_after_admission(stage, error))
                self.assertIsNone(runner._materialize_forward_progress(stage, stage, error, {}, {}, [], force_advance=True))
                runner.stage_records['topic'] = {'status': 'candidate_needs_review', 'attempts': []}
                runner.stage_records['survey'] = {'status': 'completed', 'attempts': []}
                runner.context['survey'] = {'raw': 'preserved'}
                prior_deadline, prior_usage = runner.deadline_epoch, deepcopy(runner.usage)
                runner._reconcile_pending_topic_reviews()
                self.assertEqual(runner.stage_records['topic']['status'], 'retrying')
                self.assertEqual(runner.stage_records['survey']['status'], 'retrying')
                self.assertEqual(runner.context['topic']['admission_state'], 'awaiting_source_review')
                self.assertNotIn('raw', runner.context['survey'])
                self.assertEqual(runner.deadline_epoch, prior_deadline)
                self.assertEqual(runner.usage, prior_usage)
                self.assertEqual(runner.continuation_cycles, 0)
                packet['status'] = 'resolved'
                runner._record_topic_source_review(stage, scope, packet, attempt_number=4)
                self.assertIsNone(runner._pending_topic_source_review(stage))
            finally: runner.close()

    def test_composer_checkpoint_settles_usage_once_and_rejects_scope_changes(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        from scisaurus.core.errors import StateError
        with tempfile.TemporaryDirectory() as tmp:
            runner = ComposerRunner(ComposerWorkflowTests()._workflow(Path(tmp)))
            try:
                stage = {'id': 'topic'}
                scope = {'objective': 'Develop a material', 'candidate_count': 3, 'intake_mode': 'concept'}
                packet = {'objective': scope['objective'], 'candidate_count': 3, 'intake_mode': 'concept',
                          'status': 'pending', 'usage': {'model_calls': 12, 'input_tokens': 200, 'output_tokens': 40}}
                baseline = deepcopy(runner.usage)
                for _ in range(2): runner._record_topic_source_review(stage, scope, packet, attempt_number=4)
                self.assertEqual(runner.usage['model_calls'], baseline['model_calls'] + 12)
                self.assertEqual(runner._load_topic_source_review(stage, scope), packet)
                with self.assertRaises(StateError): runner._load_topic_source_review(stage, {**scope, 'objective': 'Other'})
                error = ValidationError('source review length'); error.usage = packet['usage']; error.topic_checkpoint_usage = packet['usage']
                runner._record_failed_stage_usage(error, stage_id='topic')
                self.assertEqual(runner.usage['model_calls'], baseline['model_calls'] + 12)
            finally: runner.close()

    def test_truncated_review_never_regenerates_the_proposal_and_resumes_owned_gate(self):
        calls, checkpoints = [], []
        source = {'work_id': 'W1', 'title': 'Closest physical design', 'year': 2014, 'abstract': 'Reference mechanism.'}
        class Model:
            successful_review = False
            def __init__(self, **kwargs): self.model = kwargs.get('model', 'offline')
            def complete(self, *, system, prompt, images=None):
                payload = json.loads(prompt); calls.append(payload['assignment'])
                if payload['assignment'] == 'free_topic_discovery':
                    value = package(payload['principal_objective'])
                    for candidate, concept in zip(value['candidates'], concept_candidates()):
                        candidate.update(mechanism=concept['mechanism'], design_brief=concept['design_brief'],
                            research_form='theory_simulation', evidence_mode='synthetic_simulation', comparison_type='mechanism_ablation')
                    finish = 'stop'
                elif payload['assignment'] in {'concept_closest_design_challenge', 'repair_topic_source_challenge'}:
                    value = review(); value['selected_id'] = payload['selected_topic']['id']
                    finish = 'stop' if self.successful_review else 'length'
                else:
                    raise AssertionError('review failure dispatched an author repair: ' + payload['assignment'])
                return ModelResult(text=json.dumps(value), model=self.model, finish_reason=finish,
                                   usage={'input_tokens': 20, 'output_tokens': 20}, elapsed_seconds=.01)
        config = {'base_url': 'http://offline.invalid', 'model': 'offline', 'protocol': 'ollama', 'timeout_seconds': 1}
        with patch('scisaurus.runtime.topic_discovery.ModelClient', Model), patch.object(
                TopicDiscoveryRunner, '_recent_paper_sample', return_value=([source], 3, [])) as search:
            with self.assertRaises(ValidationError) as failure:
                TopicDiscoveryRunner(config, checkpoint_callback=checkpoints.append).run(
                    'Develop a useful material', candidate_count=3, intake_mode='concept', max_attempts=3,
                    bibliography={}, sampling_seed=3)
            error = failure.exception
            self.assertEqual(error.failure_gate, 'topic_source_review')
            self.assertEqual(error.failure_class, 'model_contract')
            self.assertFalse(error.topic_intake_recoverable)
            self.assertEqual(calls.count('free_topic_discovery'), 1)
            self.assertNotIn('repair_invalid_topic_discovery', calls)
            self.assertEqual(len(checkpoints[-1]['review_responses']), 3)
            self.assertEqual(checkpoints[-1]['candidate_prior_work'], [source])
            retained = deepcopy(checkpoints[-1])
            before = len(calls)
            with self.assertRaises(ValidationError):
                TopicDiscoveryRunner(config).run('Develop a useful material', candidate_count=3,
                    intake_mode='concept', max_attempts=3, bibliography={}, resume_review=retained)
            self.assertEqual(len(calls), before)
            self.assertEqual(search.call_count, 1)
            Model.successful_review = True
            result = TopicDiscoveryRunner({**config, 'max_output_tokens': 6000}).run(
                'Develop a useful material', candidate_count=3, intake_mode='concept', max_attempts=3,
                bibliography={}, resume_review=retained)
            self.assertEqual(calls.count('free_topic_discovery'), 1)
            self.assertEqual(search.call_count, 1)
            self.assertEqual(result['usage']['model_calls'], 1)
            self.assertEqual(result['closest_design_screen_status'], 'passed')
            self.assertEqual(result['novelty_status'], 'unverified')
            inherited = deepcopy(retained)
            inherited['candidate_prior_work'] = []
            inherited['author_response'] = {'receipt_path': '/owned/receipt.json', 'receipt': {'status': 'completed'}}
            captured = []
            TopicDiscoveryRunner({**config, 'max_output_tokens': 7000}, checkpoint_callback=captured.append).run(
                'Develop a useful material', candidate_count=3, intake_mode='concept',
                bibliography={}, resume_review=inherited)
            self.assertEqual(captured[-1]['author_response'], inherited['author_response'])
            invalid = deepcopy(retained)
            invalid['package'].pop('selection_rationale')
            from scisaurus.core.schema import canonical_bytes
            import hashlib
            invalid['package_sha256'] = hashlib.sha256(canonical_bytes(invalid['package'])).hexdigest()
            before = len(calls)
            with self.assertRaises(ValidationError) as invalid_failure:
                TopicDiscoveryRunner(config).run('Develop a useful material', candidate_count=3,
                    intake_mode='concept', bibliography={}, resume_review=invalid)
            self.assertEqual(invalid_failure.exception.failure_gate, 'topic_source_review')
            self.assertEqual(len(calls), before)
            retained['package']['selected_id'] = 'tampered'
            with self.assertRaisesRegex(ValidationError, 'does not own'):
                TopicDiscoveryRunner(config).run('Develop a useful material', candidate_count=3,
                    intake_mode='concept', bibliography={}, resume_review=retained)
