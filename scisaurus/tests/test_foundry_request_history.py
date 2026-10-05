"""Inherited dispatch history remains immutable through technical recovery."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.runtime.composer import ComposerRunner
from scisaurus.runtime.model_work import (ModelWorkCache, ModelWorkProvenanceError,
    reconcile_inherited_request_outcomes, validate_foundry_usage_inheritance)
from scisaurus.runtime.models import ModelResult
from scisaurus.tests import test_capability_foundry as foundry_tests, test_composer as composer_tests


class FoundryRequestHistoryTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runner = ComposerRunner(composer_tests.ComposerWorkflowTests()._workflow(Path(temporary.name)))
        self.addCleanup(runner.close)
        source_body = {'assignment': {'question': 'fixed'},
            'requests': [{'role': 'validator', 'status': 'started', 'usage': {'model_calls': 1}}],
            'usage': {'model_calls': 1, 'input_tokens': 10},
            'last_attempt': {'executor_source': 'immutable source'}, 'status': 'response_received'}
        source = runner._publish('command/foundry-work/source', 'note', source_body, 'command.controller')
        runner._sync_foundry_usage()
        target = deepcopy(source_body)
        target['usage_inheritance'] = {'source_ref': source['artifact_ref'], 'source_request_count': 1}
        target['requests'][0].update(status='result_unknown', error='process exited')
        target.update(status='repairing', feedback='billing provenance failure')
        return runner, source, source_body, target

    def test_legacy_annotation_reconciles_without_charge_or_scientific_changes(self):
        runner, source, source_body, target = self.fixture()
        prior = runner._publish('command/foundry-work/target', 'note', target, 'command.controller')
        with self.assertRaises(ModelWorkProvenanceError):
            runner._foundry_inherited_usage(prior['artifact_id'], target)
        self.assertFalse(runner._sync_foundry_usage())
        head = runner.store.head(prior['artifact_id'])
        corrected = json.loads(runner.store.read_body(head['body_hash']))
        self.assertNotEqual(head['artifact_ref'], prior['artifact_ref'])
        self.assertEqual(corrected['requests'], source_body['requests'])
        self.assertEqual(corrected['last_attempt'], target['last_attempt'])
        self.assertEqual(corrected['usage'], target['usage'])
        self.assertEqual(json.loads(runner.store.read_body(prior['body_hash'])), target)
        cache = ModelWorkCache(runner.store, runner._publish, namespace='command/foundry-work')
        recovered = cache.recovery_entry({**corrected, 'cache_ref': head['artifact_ref']})
        self.assertEqual(recovered, {**source_body, 'cache_ref': source['artifact_ref']})
        self.assertEqual(runner.usage['model_calls'], 1)
        self.assertFalse(runner._sync_foundry_usage())
        self.assertEqual(runner.store.head(prior['artifact_id'])['artifact_ref'], head['artifact_ref'])
        changed = deepcopy(corrected)
        changed['last_attempt']['executor_source'] = 'different source'
        with self.assertRaises(ModelWorkProvenanceError):
            cache.recovery_entry({**changed, 'cache_ref': head['artifact_ref']})

    def test_reconciler_rejects_non_outcome_changes_and_unowned_history(self):
        runner, source, source_body, target = self.fixture()
        mutations = [
            lambda value: value['requests'][0].update(role='different'),
            lambda value: value['requests'][0].update(status='succeeded'),
            lambda value: value['requests'][0].update(error=''),
            lambda value: value['requests'][0]['usage'].update(model_calls=0),
            lambda value: value['assignment'].update(question='changed'),
            lambda value: value['usage'].update(model_calls=0),
            lambda value: value['usage'].update(model_calls=True),
            lambda value: value['usage'].update(model_calls=float('nan')),
            lambda value: value['usage_inheritance'].update(source_request_count=True),
            lambda value: value['requests'].append({'status': 'started', 'role': 'new'}),
            lambda value: value['usage'].update(model_calls=2),
        ]
        for mutate in mutations:
            changed = deepcopy(target)
            mutate(changed)
            with self.subTest(mutation=mutate), self.assertRaises(ModelWorkProvenanceError):
                reconcile_inherited_request_outcomes(changed, source, source_body, target_ref='target')
        bad = runner._publish('command/foundry-work/unowned', 'note', target, 'research.author')
        with self.assertRaises(ModelWorkProvenanceError):
            runner._sync_foundry_usage()
        self.assertEqual(runner.store.head(bad['artifact_id'])['artifact_ref'], bad['artifact_ref'])

    def test_validator_resume_uses_real_composer_prefix_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            foundry = foundry_tests.IndependentValidatorAuthorshipTests().routed_foundry(root)
            cache = foundry_tests.CapabilityFoundryTests._cache(self, root)
            runner = object.__new__(ComposerRunner)
            runner.store = cache.store
            producer = foundry_tests.StubClient(foundry_tests.CapabilityFoundryTests._payload())
            producer.model = 'stub'
            phases = []
            def progress(phase, state):
                runner._foundry_inherited_usage('command/foundry-work/fixture', state)
                phases.append(phase)
                if phase == 'independent_validator_authoring':
                    raise KeyboardInterrupt()
            seen = []
            class Validator:
                def __init__(inner, **config):
                    inner.model = config['model']
                def complete(inner, **request):
                    seen.append(inner.model)
                    return ModelResult(json.dumps({'validator_source': foundry_tests.MINI_VALIDATOR}),
                        inner.model, {'model_calls': 1}, 0, 'stop')
            with patch('scisaurus.runtime.capability_foundry.ModelClient', Validator):
                with self.assertRaises(KeyboardInterrupt):
                    foundry.generate('bounded comparison', client=producer, work_cache=cache, on_progress=progress)
                original = deepcopy(cache.entries()[0])
                foundry.model_config['temperature'] = 0.25
                def check(phase, state):
                    runner._foundry_inherited_usage('command/foundry-work/fixture', state)
                result = foundry.generate('bounded comparison', client=producer,
                    work_cache=cache, on_progress=check)
            self.assertEqual(result['status'], 'registered')
            self.assertEqual((producer.calls, seen), (1, ['peer']))
            latest = cache.entries()[0]
            count = latest['usage_inheritance']['source_request_count']
            self.assertEqual(latest['requests'][:count], original['requests'])
            self.assertEqual(latest['request_outcome_reconciliations'][0]['status'], 'result_unknown')
            self.assertEqual(len(latest['request_outcome_reconciliations']), 1)

    def test_publication_validation_error_never_becomes_source_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            foundry = foundry_tests.CapabilityFoundryTests._foundry(Path(directory))
            cache = foundry_tests.CapabilityFoundryTests._cache(self, Path(directory))
            producer = foundry_tests.StubClient(foundry_tests.CapabilityFoundryTests._payload())
            def reject(phase, state):
                if phase == 'sandbox_execution_recorded':
                    from scisaurus.core.errors import ValidationError
                    raise ValidationError('immutable billing history differs')
            with self.assertRaises(ModelWorkProvenanceError) as error:
                foundry.generate('bounded comparison', client=producer, work_cache=cache, on_progress=reject)
            self.assertEqual(error.exception.failure_class, 'harness_bug')
            self.assertEqual(error.exception.diagnostic_kind, 'checkpoint_provenance')
            from scisaurus.runtime.failure_recovery import classify_failure
            self.assertEqual(classify_failure('experiment', error.exception), 'harness_bug')
            self.assertEqual(producer.calls, 1)
            latest = cache.entries()[0]
            self.assertNotEqual(latest.get('last_failure_class'), 'experiment_capability_repair')
            self.assertNotIn('immutable billing', latest.get('feedback', ''))

    def test_review_resume_uses_real_composer_prefix_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            foundry = foundry_tests.CapabilityFoundryTests._foundry(root)
            foundry.reviewer_client = None
            foundry.model_config['role_models'] = {'review.methods': {'model': 'primary'}}
            foundry.model_config['role_model_fallbacks'] = {'review.methods': [{'model': 'peer'}]}
            cache = foundry_tests.CapabilityFoundryTests._cache(self, root)
            runner = object.__new__(ComposerRunner)
            runner.store = cache.store
            producer = foundry_tests.StubClient(foundry_tests.CapabilityFoundryTests._payload())
            producer.model = 'stub'
            def interrupt(phase, state):
                runner._foundry_inherited_usage('command/foundry-work/fixture', state)
                if phase == 'scientific_review':
                    raise KeyboardInterrupt()
            seen = []
            class Reviewer:
                def __init__(inner, **config):
                    inner.model = config['model']
                def complete(inner, **request):
                    seen.append(inner.model)
                    return ModelResult(json.dumps(foundry_tests.CapabilityFoundryTests._review_payload()),
                        inner.model, {'model_calls': 1}, 0, 'stop')
            with patch('scisaurus.runtime.capability_foundry.ModelClient', Reviewer):
                with self.assertRaises(KeyboardInterrupt):
                    foundry.generate('bounded comparison', client=producer, work_cache=cache, on_progress=interrupt)
                original = deepcopy(cache.entries()[0])
                foundry.model_config['temperature'] = 0.25
                def check(phase, state):
                    runner._foundry_inherited_usage('command/foundry-work/fixture', state)
                result = foundry.generate('bounded comparison', client=producer, work_cache=cache, on_progress=check)
            self.assertEqual(result['status'], 'registered')
            self.assertEqual((producer.calls, seen, foundry.validator_client.calls), (1, ['peer'], 1))
            latest = cache.entries()[0]
            self.assertEqual(latest['requests'][:len(original['requests'])], original['requests'])
            self.assertEqual(len(latest['request_outcome_reconciliations']), 1)

    def test_review_capacity_boundary_retains_the_healthy_suffix(self):
        from scisaurus.runtime.capability_foundry import CapabilityModelBudgetExceeded
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            foundry = foundry_tests.CapabilityFoundryTests._foundry(root)
            cache = foundry_tests.CapabilityFoundryTests._cache(self, root)
            producer = foundry_tests.StubClient(foundry_tests.CapabilityFoundryTests._payload())
            complete = json.dumps(foundry_tests.CapabilityFoundryTests._review_payload())
            class Reviewer:
                model = 'reviewer'
                calls = 0
                max_output_tokens = 1024
                timeout_seconds = 30
                output_format = 'json'
                def complete(inner, **request):
                    inner.calls += 1
                    return ModelResult(complete[:-1] if inner.calls == 1 else '}', inner.model,
                        {'model_calls': 1}, 0, 'length' if inner.calls == 1 else 'stop')
            foundry.reviewer_client = reviewer = Reviewer()
            with self.assertRaises(CapabilityModelBudgetExceeded):
                foundry.generate('bounded comparison', client=producer, work_cache=cache, model_call_budget=4)
            retained = next(iter(cache.entries()[0]['scientific_reviews'].values()))
            self.assertEqual(retained['response_continuation']['status'], 'pending')
            self.assertFalse(retained.get('response_exhausted_routes'))
            result = foundry.generate('bounded comparison', client=producer, work_cache=cache, model_call_budget=6)
            self.assertEqual(result['status'], 'registered')
            self.assertEqual((producer.calls, reviewer.calls, foundry.validator_client.calls), (1, 2, 1))
