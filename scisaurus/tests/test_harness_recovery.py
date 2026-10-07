"""Offline execution-control, program transport and measurement-contract regressions."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_foundry import (
    _artifact_generation_config, _validator_program_artifact,
    _captured_validator_request, _assembled_validator_response,
    _author_continuation_prompt, ScientificDefinitionError,
)
from scisaurus.runtime.capability_registry import experiment_program_payload, experiment_validation_payload
from scisaurus.runtime.experiment import validate_deterministic_validation, bind_deterministic_validation
from scisaurus.runtime.measurement_contract import recalculation_outcomes, verified_decisions, validate_model_definition
from scisaurus.runtime.run_control import (authorized_control, control_lock, dispatch_permission,
    read_control, save_control, workflow_permission, RunPausedError, start_process)


def model_definition(ref='captured-source'):
    return {'equations': [{'id': 'rate', 'expression': 'y = a*x', 'status': 'design_assumption', 'source_ref': None}],
            'variables': [{'id': 'x', 'unit': '1', 'reference_scale': 'dimensionless'}],
            'parameters': [{'id': 'a', 'value': 2, 'unit': '1', 'status': 'design_assumption',
                            'source_ref': None, 'reason': 'analytic fixture assumption'}],
            'source_refs': [ref], 'applicability': 'analytic fixture only',
            'claim_scope': 'assumed relation, no empirical inference', 'question_alignment': 'check arithmetic'}


def receipt(assignment, text, status='succeeded'):
    prompt = json.dumps(assignment, sort_keys=True)
    response = {'model': 'fixture', 'text': text, 'finish_reason': 'stop',
                'usage': {'model_calls': 1}, 'elapsed_seconds': 0}
    identity = hashlib.sha256(canonical_bytes(assignment)).hexdigest()
    request = {key: value for key, value in response.items() if key != 'text'}
    request.update(role='methods.validator-author', assignment_sha256=identity, prompt=prompt,
                   status=status, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                   response_sha256=hashlib.sha256(text.encode()).hexdigest())
    return identity, response, request


class RunPermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.workflow = self.root / 'workflow.json'
        self.workflow.write_text(json.dumps({'project_id': str(self.root)}))

    def test_missing_or_paused_resume_is_denied_without_a_dispatch(self):
        with self.assertRaises(RunPausedError), workflow_permission(self.workflow):
            self.fail('missing resume was allowed')
        save_control(self.output, {**authorized_control(self.workflow), 'stop_requested': True})
        with self.assertRaises(RunPausedError), workflow_permission(self.workflow, initialize=True):
            self.fail('initialization replaced a human pause')

    def test_grant_stops_stale_worker_and_restores_environment(self):
        before = {key: os.environ.get(key) for key in ('SCISAURUS_RUN_CONTROL', 'SCISAURUS_RUN_GENERATION')}
        with workflow_permission(self.workflow, initialize=True):
            with dispatch_permission():
                pass
            with control_lock(self.output):
                grant = read_control(self.output)
                save_control(self.output, {**grant, 'stop_requested': True})
            with patch('subprocess.Popen') as spawn:
                with self.assertRaises(RunPausedError):
                    start_process(['unused'])
                spawn.assert_not_called()
            save_control(self.output, authorized_control(self.workflow, previous=grant))
            with self.assertRaises(RunPausedError), dispatch_permission():
                self.fail('stale generation was allowed')
        self.assertEqual(before, {key: os.environ.get(key) for key in before})

    def test_paused_worker_and_local_program_cannot_enter_their_adapter(self):
        from scisaurus.runtime.execution import _invoke_worker
        from scisaurus.runtime.programs import LocalProgramClient
        import sys
        class Channel:
            messages = []
            def put(self, value): self.messages.append(value)
        with workflow_permission(self.workflow, initialize=True):
            save_control(self.output, {**read_control(self.output), 'stop_requested': True})
            channel = Channel()
            with patch('scisaurus.runtime.literature.OpenAlexClient') as scholarly:
                _invoke_worker('openalex', {'client': {}}, channel)
                scholarly.assert_not_called()
            self.assertFalse(channel.messages[0]['ok'])
            with patch('subprocess.Popen') as spawn, self.assertRaises(RunPausedError):
                LocalProgramClient(command=[sys.executable, '-c', 'print("{}")'], timeout=1,
                                   max_bytes=1024, cwd=str(self.root.resolve()), env={}).run('{}')
            spawn.assert_not_called()

    def test_dispatch_and_stop_are_serialized(self):
        grant = authorized_control(self.workflow)
        save_control(self.output, grant)
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def dispatch():
            with dispatch_permission():
                entered.set()
                release.wait(3)
        def stop():
            with control_lock(self.output):
                save_control(self.output, {**grant, 'stop_requested': True})
            stopped.set()
        with patch.dict(os.environ, {'SCISAURUS_RUN_CONTROL': str(self.output / 'run-control.json'),
                                    'SCISAURUS_RUN_GENERATION': grant['generation']}):
            worker = threading.Thread(target=dispatch)
            worker.start()
            self.assertTrue(entered.wait(2))
            stopper = threading.Thread(target=stop)
            stopper.start()
            self.assertFalse(stopped.wait(.05))
            release.set()
            worker.join(3); stopper.join(3)
            self.assertTrue(stopped.is_set())
            with self.assertRaises(RunPausedError), dispatch_permission():
                self.fail('post-stop request was allowed')

    def test_pause_between_connection_and_submission_spends_no_budget(self):
        from scisaurus.runtime.models import ModelClient
        import http.client
        with workflow_permission(self.workflow, initialize=True):
            def revoke(connection):
                with control_lock(self.output):
                    control = read_control(self.output)
                    save_control(self.output, {**control, 'stop_requested': True})
            client = ModelClient(protocol='openai_compatible', base_url='http://127.0.0.1:1', model='fixture', timeout_seconds=1,
                                 max_output_tokens=8, model_call_budget_path=str(self.root / 'budget.sqlite'),
                                 model_call_budget_key='fixture', model_call_budget_limit=10)
            with patch.object(http.client.HTTPConnection, 'connect', revoke), \
                    patch.object(http.client.HTTPConnection, 'request') as submit:
                with self.assertRaises(RunPausedError):
                    client.complete(system='fixture', prompt='fixture')
                submit.assert_not_called()
                import sqlite3
                with sqlite3.connect(self.root / 'budget.sqlite') as database:
                    self.assertEqual(database.execute('SELECT used_calls FROM model_call_budgets WHERE budget_key=?', ('fixture',)).fetchone()[0], 0)

    def test_pause_before_provider_retry_preserves_the_submitted_attempt(self):
        from scisaurus.runtime.models import ModelClient
        import http.client
        import sqlite3
        class Response:
            status = 502
            def read1(self, size): return b'{}'
            def getheader(self, name): return None
            def close(self): pass
        def received(connection):
            with control_lock(self.output):
                control = read_control(self.output)
                save_control(self.output, {**control, 'stop_requested': True})
            return Response()
        with workflow_permission(self.workflow, initialize=True):
            client = ModelClient(protocol='openai_compatible', base_url='http://127.0.0.1:1',
                model='fixture', timeout_seconds=1, max_output_tokens=8, max_retries=1,
                retry_backoff_seconds=0, model_call_budget_path=str(self.root / 'budget.sqlite'),
                model_call_budget_key='fixture', model_call_budget_limit=10)
            with (
                patch.object(http.client.HTTPConnection, 'connect'),
                patch.object(http.client.HTTPConnection, 'request') as submit,
                patch.object(http.client.HTTPConnection, 'getresponse', received),
            ):
                with self.assertRaises(RunPausedError) as caught:
                    client.complete(system='fixture', prompt='fixture')
                self.assertEqual(submit.call_count, 1)
                self.assertEqual(caught.exception.attempts, 1)
                self.assertEqual(caught.exception.usage, {'model_calls': 1})
                with sqlite3.connect(self.root / 'budget.sqlite') as database:
                    self.assertEqual(database.execute('SELECT used_calls FROM model_call_budgets WHERE budget_key=?',
                                                     ('fixture',)).fetchone()[0], 1)

    def test_corrupt_or_foreign_workflow_is_denied(self):
        (self.output / 'run-control.json').write_text('{')
        with self.assertRaises(RunPausedError), workflow_permission(self.workflow):
            pass
        save_control(self.output, authorized_control(self.root / 'other.json'))
        with self.assertRaises(RunPausedError), workflow_permission(self.workflow):
            pass

    def test_stop_persists_when_owner_is_already_absent(self):
        from scisaurus.tests.test_dashboard_control import fixture_workflow
        from scisaurus.dashboard.server import DashboardService
        workflow = fixture_workflow(self.root)
        self.workflow.write_text(json.dumps(workflow))
        service = DashboardService(self.root)
        with patch.object(service, '_composer_processes', return_value=[]):
            self.assertEqual(service.stop_composer()['status'], 'already_stopped')
        self.assertTrue(read_control(self.root / 'composer/output')['stop_requested'])
        with self.assertRaises(RunPausedError), workflow_permission(self.workflow):
            pass


class ProgramArtifactTests(unittest.TestCase):
    def test_single_program_fence_preserves_newline_span_and_bytes(self):
        source = 'import json\nprint(json.dumps({"ok": True}))\n'
        text = '```python\n' + source + '```\n'
        artifact = _validator_program_artifact({'text': text, 'finish_reason': 'stop'})
        self.assertEqual(artifact['source'], source)
        self.assertEqual(text[slice(*artifact['source_span'])], source)
        self.assertEqual(artifact['source_sha256'], hashlib.sha256(source.encode()).hexdigest())
        for bad in ('explanation\n'+text, text+text, text+'prose', text[:-5], '```python\nprint(\n```'):
            with self.subTest(text=bad), self.assertRaises(ValidationError):
                _validator_program_artifact({'text': bad, 'finish_reason': 'stop'})

    def test_missing_hash_unknown_latest_and_changed_prompt_are_not_owned(self):
        identity, response, request = receipt({'design': 'frozen'}, '{"validator_source":"pass"}')
        self.assertEqual(_captured_validator_request({'requests': [request]}, identity, response), request)
        for changed in ({'prompt_sha256': None}, {'response_sha256': None}, {'prompt': '{}'},
                        {'status': 'started'}, {'status': 'result_unknown'}, {'model': 'other'}):
            with self.subTest(changed=changed):
                self.assertIsNone(_captured_validator_request({'requests': [request, {**request, **changed}]}, identity, response))

    def test_continuation_is_prefix_bound_and_does_not_rewrite_receipt(self):
        assignment = {'design': 'frozen'}
        identity, root, request = receipt(assignment, '{"validator_source":"import json\\n')
        root['finish_reason'] = request['finish_reason'] = 'length'
        marker, digest, prompt = _author_continuation_prompt(root['text'])
        _, segment, segment_request = receipt({**assignment, 'validator_continuation': json.loads(prompt)}, 'print(1)\\n"}')
        segment_request['assignment_sha256'] = identity
        partial = root['text'] + segment['text']
        retained = {'response': segment, 'continuation': {'root_response': root, 'root_request': request,
                    'partial_sha256': hashlib.sha256(partial.encode()).hexdigest(),
                    'segments': [{'request': segment_request, 'response': segment, 'prefix_sha256': digest}]}}
        self.assertEqual(_assembled_validator_response(retained, identity)['text'], partial)
        self.assertEqual(retained['response']['text'], segment['text'])
        retained['continuation']['segments'][0]['prefix_sha256'] = '0'*64
        with self.assertRaises(ValidationError):
            _assembled_validator_response(retained, identity)

    def test_profiles_preserve_scientific_input_and_explicit_disabled_reasoning(self):
        config = {'model': 'fixture', 'reasoning_effort': 'high', 'max_output_tokens': 32768}
        self.assertEqual(_artifact_generation_config(config)['reasoning_effort'], 'medium')
        self.assertEqual(_artifact_generation_config(config, repair=True)['reasoning_effort'], 'low')
        self.assertEqual(_artifact_generation_config(config, empty_output=True)['reasoning_effort'], 'none')
        self.assertEqual(config['reasoning_effort'], 'high')
        self.assertEqual(_artifact_generation_config({**config, 'reasoning_effort': 'none'}, repair=True)['reasoning_effort'], 'none')


class MeasurementContractTests(unittest.TestCase):
    def intent(self):
        from scisaurus.tests.test_capability_foundry import INTENT
        intent = deepcopy(INTENT)
        primary = intent['primary_outcomes'][0]
        intent['decision_outcomes'] = [{'id': 'contrast', 'definition': 'twice tail_error from raw observations',
                                        'unit': primary['unit'], 'parents': [primary['id']]}]
        intent['decision_rules'] = [{'id': 'contrast_limit', 'metric_id': 'contrast', 'unit': primary['unit'],
                                     'operator': '>', 'threshold': 2, 'claim': 'contrast exceeds the declared limit'}]
        return intent

    def verdict(self, intent, value=2):
        return {'schema_version': 'experiment-validation-1', 'study_id': intent['id'],
                'candidate_sha256': 'a'*64, 'decision': 'accepted',
                'checks': [{'id': 'raw_rows', 'outcome': 'passed', 'evidence': 'fixture recalculation'}],
                'metric_recalculations': [{'metric_id': row['id'], 'reported_value': value, 'recalculated_value': value,
                                          'tolerance': 0, 'matches': True} for row in recalculation_outcomes(intent)],
                'limitations': []}

    def test_decision_metric_reaches_executor_validator_and_consumer(self):
        intent = self.intent(); verdict = self.verdict(intent)
        execution = experiment_program_payload(intent, {'seed': 7})
        candidate = {'metrics': [{'id': row['id'], 'value': 2} for row in recalculation_outcomes(intent)]}
        validation = experiment_validation_payload(intent, {'seed': 7}, candidate, 'a'*64)
        self.assertEqual(execution['experiment']['decision_rules'], intent['decision_rules'])
        self.assertEqual([row['id'] for row in validation['primary_outcomes']], ['tail_error', 'contrast'])
        validate_deterministic_validation(verdict, intent, 'a'*64)
        bind_deterministic_validation(verdict, candidate, intent)
        self.assertEqual(verified_decisions(intent, verdict)[0]['outcome'], 'not_satisfied')
        intent['decision_rules'][0]['operator'] = '>='
        self.assertEqual(verified_decisions(intent, verdict)[0]['outcome'], 'satisfied')
        with self.assertRaises(ValidationError):
            validate_deterministic_validation({**verdict, 'metric_recalculations': verdict['metric_recalculations'][:1]}, intent, 'a'*64)

    def test_null_and_invalid_parents_units_thresholds_are_not_silent_numbers(self):
        intent = self.intent(); verdict = self.verdict(intent, None)
        self.assertEqual(verified_decisions(intent, verdict)[0]['outcome'], 'not_estimable')
        verdict['metric_recalculations'][1].update(recalculated_value=2, reported_value=2)
        with self.assertRaises(ValidationError):
            verified_decisions(intent, verdict)
        for mutate in (lambda x: x['decision_outcomes'][0].update(parents=['contrast']),
                       lambda x: x['decision_outcomes'][0].update(parents=[{}]),
                       lambda x: x['decision_rules'][0].update(unit='wrong'),
                       lambda x: x['decision_rules'][0].update(threshold=True),
                       lambda x: x['decision_rules'][0].update(threshold=float('nan'))):
            changed = self.intent(); mutate(changed)
            with self.assertRaises(ValidationError):
                recalculation_outcomes(changed)

    def test_model_definition_binds_sources_units_and_parameter_values(self):
        intent = {'model_definition': model_definition(), 'parameters': {'a': 2}}
        validate_model_definition(intent, source_refs=['captured-source'], required=True)
        for change in ({'parameters': {'a': 3}}, {'model_definition': None}):
            with self.assertRaises(ValidationError):
                validate_model_definition({**intent, **change}, source_refs=['captured-source'], required=True)
        with self.assertRaises(ValidationError):
            validate_model_definition(intent, source_refs=['other'], required=True)
        intent['model_definition']['parameters'][0].update(status='source_bound', source_ref=None)
        with self.assertRaises(ValidationError):
            validate_model_definition(intent, required=True)

    def test_result_package_revalidates_decision_evidence_and_rejects_tampering(self):
        from scisaurus.runtime.results import validate_results_package
        intent = self.intent()
        verdict = self.verdict(intent)
        ref = lambda name: 'artifact:command/' + name + '@1'
        package = {
            'schema_version': 'results-package-2', 'id': intent['id'], 'revision': 1,
            'study_type': 'methods_validation', 'question': 'Does the contract preserve decisions?',
            'hypothesis': 'The decision follows the declared comparison.',
            'procedures': [{'id': 'calculation', 'description': 'Fixture calculation', 'source': 'fixture'}],
            'metrics': [{'id': row['id'], 'value': 2, 'unit': row['unit'],
                         'conditions': 'fixture', 'source': 'raw fixture', 'presentation': '2'}
                        for row in recalculation_outcomes(intent)],
            'findings': [{'id': 'bounded_result', 'statement': 'The declared strict threshold is not exceeded.',
                          'metric_ids': ['contrast']}],
            'limitations': ['Fixture arithmetic establishes contract behavior only.'], 'assets': [],
            'provenance': {'score_ref': ref('score'), 'literature_survey_ref': None,
                'literature_assessment_ref': None, 'execution_refs': [ref('execution_one'), ref('execution_two')],
                'validator_execution_ref': ref('validator'), 'execution_profile_ref': ref('execution_profile'),
                'validation_profile_ref': ref('validation_profile'), 'replay_sha256': 'a'*64},
            'validation': {'decision': 'accepted', 'deterministic_validation_ref': ref('verdict'),
                'model_review_refs': [ref('review_one'), ref('review_two')], 'assessment_ref': ref('assessment')},
            'decision_evidence': {'contract': intent, 'deterministic_validation': verdict,
                                  'assessments': verified_decisions(intent, verdict)},
        }
        validate_results_package(package)
        for mutate in (
                lambda value: value['decision_evidence']['assessments'][0].update(outcome='satisfied'),
                lambda value: value['decision_evidence']['deterministic_validation'].update(candidate_sha256='b'*64),
                lambda value: value['metrics'][1].update(value=3),
                lambda value: value['metrics'][1].update(unit='wrong'),
                lambda value: value['decision_evidence']['contract'].update(revision=2),
                lambda value: value['decision_evidence']['contract']['decision_rules'][0].update(operator='>=')):
            altered = deepcopy(package)
            mutate(altered)
            with self.assertRaises(ValidationError):
                validate_results_package(altered)

    def test_missing_custom_definition_returns_to_methods_before_authoring(self):
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests, StubClient
        with tempfile.TemporaryDirectory() as path:
            foundry = CapabilityFoundryTests._foundry(Path(path)); producer = StubClient(CapabilityFoundryTests._payload())
            with self.assertRaises(ScientificDefinitionError) as caught:
                foundry.generate('declared question', test_input={'scientific_software': {'selection': {'strategy': 'custom_model'}}}, client=producer)
            self.assertEqual(caught.exception.repair_owner, 'methods_adjudication')
            self.assertEqual(producer.calls, 0)

    def test_changed_model_parameter_preserves_candidate_and_methods_ownership(self):
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests, StubClient
        from scisaurus.runtime.measurement_contract import ModelDefinitionError
        from scisaurus.runtime.failure_recovery import classify_failure
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            cache = CapabilityFoundryTests._cache(self, root)
            payload = CapabilityFoundryTests._payload()
            payload['experiment_intent']['model_definition'] = model_definition()
            payload['experiment_intent']['parameters']['a'] = 3
            producer = StubClient(payload)
            with self.assertRaises(ModelDefinitionError) as caught:
                foundry.generate('declared question', client=producer, work_cache=cache)
            error = caught.exception
            self.assertEqual(classify_failure('experiment', error), 'experiment_failure')
            self.assertEqual(error.recovery_mode, 'repair_then_rerun')
            self.assertEqual(error.repair_feedback['repair_owner'], 'methods_adjudication')
            self.assertTrue(error.repair_feedback['foundry_work_ref'].startswith('artifact:'))
            state = cache.entries()[0]
            self.assertEqual(state['status'], 'blocked')
            self.assertEqual(state['last_failure_gate'], 'model_definition')
            self.assertEqual(state['last_attempt']['experiment_intent']['parameters']['a'], 3)
            self.assertEqual(producer.calls, 1)

    def test_admitted_definition_is_required_and_omission_is_a_contract_error(self):
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests, StubClient
        from scisaurus.runtime.model_work import ModelWorkBlocked
        from scisaurus.runtime.failure_recovery import classify_failure
        selection = {'strategy': 'custom_model', 'model_definition': model_definition(),
                     'scientific_source_refs': ['captured-source']}
        with tempfile.TemporaryDirectory() as path:
            root = Path(path); foundry = CapabilityFoundryTests._foundry(root)
            foundry.max_attempts = 1
            cache = CapabilityFoundryTests._cache(self, root)
            payload = CapabilityFoundryTests._payload()
            payload['test_input'] = {'scientific_software': {'selection': selection}}
            author = StubClient(payload)
            required = {'domain': payload['experiment_intent']['domain']}; original = deepcopy(required)
            with self.assertRaises(ModelWorkBlocked) as caught, patch.object(foundry, '_execute') as execution:
                foundry.generate('declared question', test_input={'scientific_software': {'selection': selection}},
                                 required_intent=required, client=author, work_cache=cache)
            self.assertNotIsInstance(caught.exception, ScientificDefinitionError)
            self.assertEqual(classify_failure('experiment', caught.exception), 'model_contract')
            execution.assert_not_called()
            state = cache.entries()[0]; assignment = state['assignment']
            self.assertEqual(assignment['required_intent_fields']['model_definition'], selection['model_definition'])
            self.assertEqual(assignment['output_contract']['experiment_intent']['model_definition'], selection['model_definition'])
            self.assertNotIn('model_definition', assignment['optional_intent_fields'])
            self.assertEqual(required, original)

    def test_partial_admitted_definition_is_an_author_contract_error(self):
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests, StubClient
        from scisaurus.runtime.model_work import ModelWorkBlocked
        from scisaurus.runtime.failure_recovery import classify_failure
        selection = {'strategy': 'custom_model', 'model_definition': model_definition(),
                     'scientific_source_refs': ['captured-source']}
        for definition in ({}, {'equations': []}, {**model_definition(), 'parameters': 'invalid'}):
            with self.subTest(definition=definition), tempfile.TemporaryDirectory() as path:
                root = Path(path); foundry = CapabilityFoundryTests._foundry(root)
                foundry.max_attempts = 1
                cache = CapabilityFoundryTests._cache(self, root)
                payload = CapabilityFoundryTests._payload()
                payload['test_input'] = {'scientific_software': {'selection': selection}}
                payload['experiment_intent']['model_definition'] = definition
                with self.assertRaises(ModelWorkBlocked) as caught, patch.object(foundry, '_execute') as execution:
                    foundry.generate('declared question', test_input=payload['test_input'],
                                     client=StubClient(payload), work_cache=cache)
                self.assertEqual(classify_failure('experiment', caught.exception), 'model_contract')
                self.assertNotIsInstance(caught.exception, ScientificDefinitionError)
                execution.assert_not_called()
                self.assertEqual(cache.entries()[0]['last_attempt']['experiment_intent']['model_definition'], definition)

    def test_changed_admitted_definition_still_requires_methods(self):
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests, StubClient
        selection = {'strategy': 'custom_model', 'model_definition': model_definition(),
                     'scientific_source_refs': ['captured-source']}
        payload = CapabilityFoundryTests._payload()
        payload['test_input'] = {'scientific_software': {'selection': selection}}
        payload['experiment_intent']['model_definition'] = model_definition()
        payload['experiment_intent']['model_definition']['equations'][0]['expression'] = 'y = a*x*x'
        with tempfile.TemporaryDirectory() as path:
            root = Path(path); foundry = CapabilityFoundryTests._foundry(root)
            with self.assertRaises(ScientificDefinitionError), patch.object(foundry, '_execute') as execution:
                foundry.generate('declared question', test_input={'scientific_software': {'selection': selection}},
                                 client=StubClient(payload))
            execution.assert_not_called()

class IndependentProgramRecoveryTests(unittest.TestCase):
    def fixture(self, root):
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests, StubClient
        return CapabilityFoundryTests._foundry(root), StubClient(CapabilityFoundryTests._payload())

    def test_python_fence_reaches_replay_recalculation_and_review(self):
        from scisaurus.tests.test_capability_foundry import MINI_VALIDATOR
        from scisaurus.runtime.models import ModelResult
        class Independent:
            calls = 0
            def complete(inner, **kwargs):
                inner.calls += 1
                return ModelResult('```python\n'+MINI_VALIDATOR+'```\n', 'fixture', {'model_calls': 1}, 0, 'stop')
        with tempfile.TemporaryDirectory() as path:
            foundry, producer = self.fixture(Path(path)); foundry.validator_client = Independent()
            outcome = foundry.generate('bounded comparison', client=producer)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual(outcome['candidate']['validator_source'], MINI_VALIDATOR)
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 1))
            self.assertEqual(outcome['admission']['replay_runs'], 3)
            self.assertEqual(outcome['admission']['independent_recalculation']['decision'], 'accepted')

    def test_truncated_validator_uses_exact_suffix_then_all_gates(self):
        from scisaurus.tests.test_capability_foundry import MINI_VALIDATOR
        from scisaurus.runtime.models import ModelResult
        text = json.dumps({'validator_source': MINI_VALIDATOR})
        cutoff = len(text)//2
        class Independent:
            calls = 0
            def complete(inner, *, system, prompt):
                inner.calls += 1
                packet = json.loads(prompt)
                if inner.calls == 1:
                    return ModelResult(text[:cutoff], 'fixture', {'model_calls': 1}, 0, 'length')
                self.assertIn('validator_continuation', packet)
                self.assertNotIn('validator_repair', packet)
                return ModelResult(text[cutoff:], 'fixture', {'model_calls': 1}, 0, 'stop')
        with tempfile.TemporaryDirectory() as path:
            foundry, producer = self.fixture(Path(path)); foundry.validator_client = Independent()
            outcome = foundry.generate('bounded comparison', client=producer)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual(outcome['candidate']['validator_source'], MINI_VALIDATOR)
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 2))

    def test_runtime_failure_is_repaired_only_by_independent_source_patch(self):
        from scisaurus.tests.test_capability_foundry import MINI_VALIDATOR
        from scisaurus.runtime.models import ModelResult
        bad = MINI_VALIDATOR.replace('matches = abs(', 'matches = missing_name + abs(')
        class Independent:
            calls = 0
            def complete(inner, *, system, prompt):
                inner.calls += 1
                packet = json.loads(prompt)
                if inner.calls == 1:
                    value = {'validator_source': bad}
                else:
                    self.assertEqual(packet['validator_repair']['prior_source'], bad)
                    self.assertEqual(packet['validator_repair']['failure_kind'], 'validator_execution')
                    self.assertIn('missing_name', packet['validator_repair']['diagnostic'])
                    self.assertNotIn('executor_source', packet)
                    value = {'updates': {'validator_source': {'edits': [{'old': 'missing_name + ', 'new': ''}]}}}
                return ModelResult(json.dumps(value), 'fixture', {'model_calls': 1}, 0, 'stop')
        with tempfile.TemporaryDirectory() as path:
            foundry, producer = self.fixture(Path(path)); foundry.validator_client = Independent()
            outcome = foundry.generate('bounded comparison', client=producer)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual(outcome['candidate']['validator_source'], MINI_VALIDATOR)
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 2))

    def test_invalid_cli_workflow_creates_no_execution_grant(self):
        from scisaurus.cli import main
        with tempfile.TemporaryDirectory() as directory:
            workflow = Path(directory) / "workflow.json"
            workflow.write_text(json.dumps({"id": "invalid", "project_id": directory}))
            with patch("builtins.print"):
                self.assertEqual(main(["run-composer", "--workflow", str(workflow)]), 2)
            self.assertFalse((Path(directory) / "output/run-control.json").exists())

    def test_legacy_receipt_migration_is_hash_bound_and_inheritance_is_not_billed(self):
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests
        from scisaurus.runtime.capability_foundry import _migrate_validator_receipt
        from scisaurus.runtime.model_work import ModelWorkProvenanceError
        with tempfile.TemporaryDirectory() as path:
            cache = CapabilityFoundryTests._cache(self, Path(path))
            identity, response, request = receipt({'design': 'frozen'}, '{"validator_source":"pass"}')
            request.pop('prompt_sha256')
            original = {'requests': [request], 'usage': {'model_calls': 1, 'output_tokens': 10},
                        'validator_authorship': {identity: {'response': response}}}
            cache.put('original', original)
            source = cache.get('original')
            migrated = _migrate_validator_receipt(source, identity, response, cache.store)
            self.assertIsNotNone(migrated)
            inherited = {'requests': [], 'usage': {}, 'validator_authorship': {identity: {'inherited_dispatch': migrated}}}
            self.assertEqual(_captured_validator_request(inherited, identity, response, store=cache.store), migrated['request'])
            self.assertEqual(inherited['usage'], {})
            for update in ({'source_body_sha256': '0'*64}, {'source_ref': migrated['source_ref'], 'request': {**migrated['request'], 'status': 'started'}}):
                bad = deepcopy(inherited)
                bad['validator_authorship'][identity]['inherited_dispatch'].update(update)
                try:
                    result = _captured_validator_request(bad, identity, response, store=cache.store)
                except ModelWorkProvenanceError:
                    result = None
                self.assertIsNone(result)
            cache.put('unknown', {**original, 'requests': [request, {**request, 'status': 'result_unknown'}]})
            self.assertIsNone(_migrate_validator_receipt(cache.get('unknown'), identity, response, cache.store))
