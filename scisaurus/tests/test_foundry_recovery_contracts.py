import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scisaurus.runtime.capability_foundry import IndependentValidatorContractError
from scisaurus.runtime.capability_foundry import CapabilityDeadlineError, CapabilityModelBudgetExceeded
from scisaurus.runtime.composer import ComposerRunner
from scisaurus.runtime.failure_recovery import classify_failure
from scisaurus.runtime.models import ModelCallError, ModelResult
from scisaurus.runtime.specialists import research_question_alignment, build_repair_adjudication_prompt
from scisaurus.tests import test_capability_foundry as foundry_fixtures
from scisaurus.tests import test_composer as composer_fixtures
from scisaurus.tests.test_capability_foundry import StubClient, MINI_VALIDATOR, MINI_EXECUTOR


class FoundryRecoveryContractTests(unittest.TestCase):
    def _response_owner_fixture(self, runner):
        stage = runner.workflow['stages'][1]
        work = {'status': 'blocked', 'last_failure_class': 'model_contract',
                'last_failure_gate': 'author_response_format', 'last_attempt': None,
                'last_response': {'text': '{"executor":'}, 'attempts': 4,
                'usage': {'model_calls': 4}}
        published = runner._publish('command/foundry-work/retained', 'decision_note', work, 'command.foundry')
        snapshot = {'cache_ref': published['artifact_ref'], 'cache_body_sha256': published['body_hash'],
                    'cache_body_verified': True, 'last_attempt': None}
        dossiers = []
        for number, digest, failure_class in ((8, 'a' * 64, 'model_contract'),
                                               (11, 'b' * 64, 'experiment_contract')):
            dossier = {'schema_version': 'composer-failure-recovery-1', 'stage_id': stage['id'],
                       'attempt_number': number, 'input_sha256': digest, 'failure_class': failure_class,
                       'foundry_work_snapshot': deepcopy(snapshot), 'model_diagnostics': {},
                       'program_snapshot': [], 'observed_result': {'status': 'blocked',
                                                                  'usage': {'model_calls': 0}}}
            ref = runner._publish(f'command/composer/failure-recovery/{stage["id"]}/{number}',
                                  'decision_note', dossier, 'command.composer')['artifact_ref']
            dossiers.append((ref, dossier))
        request = {'id': 'retained-response', 'kind': 'recovery', 'owner': 'methods.validation',
                   'objective': 'Repair the retained response.', 'why': 'The response failed its contract.',
                   'success_condition': 'A valid response under the same assignment.',
                   'evidence_needed': 'The exact failed response.', 'target_stage_id': stage['id'],
                   'target_stage_kind': stage['kind'], 'source_stage_id': stage['id'],
                   'recovery_mode': 'format_repair_then_rerun', 'failure_dossier_ref': dossiers[0][0],
                   'failure_input_sha256': 'a' * 64, 'foundry_work_ref': published['artifact_ref']}
        runner.feedback.append({'action': 'continue_research', 'research_requests': [deepcopy(request)]})
        runner.context[stage['id']] = {'status': 'research_expansion_required',
            'failure_dossier_ref': dossiers[1][0], 'failure_input_sha256': 'b' * 64,
            'research_requests': [deepcopy(request)]}
        runner.stage_records[stage['id']] = {'status': 'retrying', 'attempts': [
            {'attempt_number': number, 'failure_dossier_ref': ref}
            for number, (ref, _) in zip((8, 11), dossiers)]}
        return stage, request, dossiers, work

    def test_response_owner_survives_only_unexecuted_same_response_wrappers(self):
        cases = ('same', 'current_native', 'new_program', 'new_usage', 'unknown_usage',
                 'new_diagnostics', 'changed_snapshot', 'changed_head', 'wrong_input', 'unadmitted',
                 'new_observations', 'new_execution_refs', 'new_derived_results')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as path:
                runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(Path(path)))
                try:
                    stage, request, dossiers, work = self._response_owner_fixture(runner)
                    current = deepcopy(dossiers[1][1])
                    if case == 'current_native': current['repair_subject'] = {'source': 'native'}
                    if case == 'new_program': current['program_snapshot'] = [{'source_sha256': 'c' * 64}]
                    if case == 'new_usage': current['observed_result']['usage']['model_calls'] = 1
                    if case == 'unknown_usage': current['observed_result']['usage'] = {}
                    if case == 'new_diagnostics': current['model_diagnostics'] = {'repair_feedback': {}}
                    if case == 'new_observations': current['observed_result']['raw_results'] = {'observed_samples': [2, 4]}
                    if case == 'new_execution_refs': current['observed_result']['execution_refs'] = ['artifact:experiment/results@1']
                    if case == 'new_derived_results': current['observed_result']['derived_results'] = {'measurement_count': 2}
                    if case == 'changed_snapshot': current['foundry_work_snapshot']['cache_body_sha256'] = 'd' * 64
                    if case == 'changed_head': runner._publish('command/foundry-work/retained', 'decision_note',
                                                             {**work, 'attempts': 5}, 'command.foundry')
                    if case == 'wrong_input': request['failure_input_sha256'] = 'e' * 64
                    if case == 'unadmitted': runner.feedback = []
                    if current != dossiers[1][1]:
                        new_ref = runner._publish(f'command/composer/failure-recovery/{stage["id"]}/11',
                                                  'decision_note', current, 'command.composer')['artifact_ref']
                        runner.context[stage['id']]['failure_dossier_ref'] = new_ref
                        runner.stage_records[stage['id']]['attempts'][1]['failure_dossier_ref'] = new_ref
                    context = runner.context[stage['id']]
                    self.assertEqual(runner._unresolved_response_owner(request, context) is not None, case == 'same')
                    self.assertEqual(runner._recovery_input_matches(stage, request, context, 'b' * 64), case == 'same')
                    self.assertFalse(runner._recovery_input_matches(stage, request, context, 'f' * 64))
                    runner.active_research_requests = [request]
                    retired = runner._reconcile_superseded_response_recovery_orders()
                    self.assertEqual(bool(retired), case not in {'same', 'wrong_input', 'unadmitted', 'current_native'})
                finally:
                    runner.close()

    def test_restore_retired_response_order_preserves_terminal_generation_and_inputs(self):
        for case in ('restore', 'running', 'completed', 'unadmitted', 'foreign_retirement', 'changed_head',
                     'invalid_container', 'changed_order', 'foreign_order_author'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as path:
                runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(Path(path)))
                try:
                    stage, request, dossiers, work = self._response_owner_fixture(runner)
                    note = runner._publish('command/departments/methods/inbox/response', 'decision_note',
                        {'feedback': runner.feedback[-1]}, 'command.composer')['artifact_ref']
                    proposal = {key: request[key] for key in ('id', 'kind', 'owner', 'objective', 'why',
                                                             'success_condition', 'evidence_needed')}
                    proposal['schema_version'] = 'department-work-order-1'
                    task = runner.departments.propose(proposal, source_stage_id='workflow',
                                                       note_ref=note, controller_metadata=request)
                    runner.departments.retire_superseded_work_orders(set(), reason='newer failure owner')
                    runner.active_research_requests = []
                    runner.context[stage['id']]['research_requests'] = []
                    runner.department_activity.append({'action': 'retire_superseded_response_recovery_orders',
                        'work_orders': [{'request_id': request['id'], 'stage_id': stage['id'],
                            'prior_failure_dossier_ref': dossiers[0][0],
                            'current_failure_dossier_ref': 'artifact:foreign@1' if case == 'foreign_retirement' else dossiers[1][0]}]})
                    if case in {'running', 'completed'}: runner.stage_records[stage['id']]['status'] = case
                    if case == 'unadmitted': runner.feedback = []
                    if case == 'changed_head': runner._publish('command/foundry-work/retained', 'decision_note',
                                                             {**work, 'attempts': 5}, 'command.foundry')
                    if case == 'invalid_container': runner.context[stage['id']]['research_requests'] = None
                    if case in {'changed_order', 'foreign_order_author'}:
                        logical = 'command/departments/methods/work-orders/' + request['id']
                        _, _, body = runner._read_verified_artifact_json(runner.store.head(logical)['artifact_ref'])
                        if case == 'changed_order': body['objective'] = 'A different admitted scope.'
                        runner._publish(logical, 'decision_note', body,
                                        'foreign' if case == 'foreign_order_author' else 'command.composer')
                    before = deepcopy(runner.stage_records)
                    restored = runner._restore_retained_response_recovery_orders()
                    self.assertEqual(bool(restored), case == 'restore')
                    self.assertEqual(runner.stage_records, before)
                    self.assertEqual(runner.departments.tasks.get(task['task_id'])['state'], 'stale')
                    if restored:
                        fresh = runner.active_research_requests[0]
                        self.assertEqual({key: value for key, value in fresh.items()
                                          if key not in {'recovery_generation', 'source_note_ref'}}, request)
                        self.assertEqual(fresh['recovery_generation'], 1)
                        head = runner.store.head('command/departments/methods/work-orders/' + request['id'])
                        _, _, body = runner._read_verified_artifact_json(head['artifact_ref'])
                        self.assertEqual(body['source_note_ref'], note)
                        self.assertEqual(runner.context[stage['id']]['failure_input_sha256'], 'b' * 64)
                        self.assertEqual(runner._restore_retained_response_recovery_orders(), [])
                        self.assertEqual(len(runner.active_research_requests), 1)
                finally:
                    runner.close()

    def test_settled_empty_panel_invoice_does_not_reserve_the_panel_again(self):
        from scisaurus.core.errors import ValidationError
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(Path(path)))
            try:
                runner.workflow['capability_foundry_config_path'] = str(Path(path)/'foundry.json')
                stage = runner.workflow['stages'][1]
                stage['quota'] = {'max_model_calls': 24}
                with patch.object(runner, '_stage_usage', return_value={'model_calls': 3}), \
                        patch.object(runner, '_capability_repair_panel_required', return_value=True), \
                        patch.object(runner, '_capability_repair_panel_model_call_reserve', return_value=20):
                    self.assertEqual(runner._foundry_model_call_budget(stage), 0)
                    self.assertEqual(runner._foundry_model_call_budget(stage, repair_panel_usage={}), 12)
                    self.assertEqual(runner._foundry_model_call_budget(stage,
                                     repair_panel_usage={'model_calls': 0}), 12)
                    self.assertEqual(runner._foundry_model_call_budget(stage,
                                     repair_panel_usage={'model_calls': 11}), 8)
                    for usage in ([], {'model_calls': -1}, {'model_calls': True}, {'model_calls': None}):
                        with self.subTest(usage=usage), self.assertRaises(ValidationError):
                            runner._foundry_model_call_budget(stage, repair_panel_usage=usage)
            finally:
                runner.close()

    def test_methods_response_diagnostic_identifies_exact_contract_defects(self):
        from scisaurus.core.errors import ValidationError
        from scisaurus.runtime.specialists import _validate_repair_adjudication_response
        valid = {'decision': 'hold', 'summary': 'Evidence is unresolved.', 'findings': [],
                 'evidence_gaps': [], 'requested_actions': [], 'repair_plan': None}
        _validate_repair_adjudication_response(valid)
        invalid = {**valid, 'analysis': {}, 'summary': None, 'findings': [False]}
        del invalid['requested_actions']
        with self.assertRaises(ValidationError) as caught:
            _validate_repair_adjudication_response(invalid)
        message = str(caught.exception)
        for diagnostic in ('missing fields: requested_actions', 'unexpected fields: analysis',
                           'summary must be a string', 'findings must be a string list'):
            self.assertIn(diagnostic, message)

    def test_response_repair_lifecycle_retires_only_verified_older_owners(self):
        cases = ('older', 'same', 'newer', 'wrong_input', 'foreign_stage',
                 'missing', 'unbound', 'unadmitted', 'scientific', 'scientific_kind')
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as path:
                runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(Path(path)))
                try:
                    stage = runner.workflow['stages'][1]
                    def publish(number, stage_id, digest):
                        dossier = {'schema_version': 'composer-failure-recovery-1',
                                   'stage_id': stage_id, 'attempt_number': number,
                                   'failure_class': 'model_contract', 'input_sha256': digest}
                        return runner._publish(f'command/composer/failure-recovery/{stage_id}/{number}', 'decision_note', dossier,
                                               'command.composer')['artifact_ref']
                    old_number = 12 if case == 'newer' else 8
                    old_ref = publish(old_number, 'foreign' if case == 'foreign_stage' else stage['id'], 'a' * 64)
                    current_ref = old_ref if case == 'same' else publish(11, stage['id'], 'b' * 64)
                    order = {'id': 'response-owner', 'kind': 'additional_experiment' if case == 'scientific_kind' else 'recovery', 'owner': 'methods.validation',
                             'target_stage_id': stage['id'], 'source_stage_id': stage['id'],
                             'recovery_mode': 'repair_then_rerun' if case == 'scientific' else 'format_repair_then_rerun',
                             'failure_dossier_ref': 'artifact:missing@1' if case == 'missing' else old_ref,
                             'failure_input_sha256': 'x' * 64 if case == 'wrong_input' else 'a' * 64}
                    unrelated = {'id': 'survey-owner', 'kind': 'literature_expansion', 'target_stage_id': 'survey'}
                    runner.active_research_requests = [order, unrelated]
                    if case != 'unadmitted':
                        runner.feedback.append({'action': 'continue_research', 'research_requests': [order]})
                    current_request = {'id': 'current-scientific-order', 'kind': 'additional_experiment',
                                       'target_stage_id': stage['id'], 'failure_dossier_ref': current_ref}
                    runner.context[stage['id']] = {'status': 'research_expansion_required',
                        'failure_dossier_ref': current_ref, 'research_requests': [order, current_request]}
                    runner.stage_records[stage['id']] = {'status': 'retrying', 'attempts': [
                        {'attempt_number': old_number, 'failure_dossier_ref': old_ref},
                        {'attempt_number': old_number if case == 'same' else 11,
                         'failure_dossier_ref': current_ref}]}
                    if case == 'unbound':
                        runner.stage_records[stage['id']]['attempts'] = []
                    retired = runner._reconcile_superseded_response_recovery_orders()
                    self.assertEqual(bool(retired), case == 'older')
                    self.assertEqual(len(runner.active_research_requests), 1 if case == 'older' else 2)
                    self.assertIn(unrelated, runner.active_research_requests)
                    self.assertEqual(len(runner.context[stage['id']]['research_requests']), 1 if case == 'older' else 2)
                    self.assertIn(current_request, runner.context[stage['id']]['research_requests'])
                    self.assertEqual(runner.stage_records[stage['id']]['status'], 'retrying')
                    self.assertEqual(len(runner.stage_records[stage['id']]['attempts']), 0 if case == 'unbound' else 2)
                finally:
                    runner.close()

    def test_unowned_hold_admits_current_work_before_reusing_paid_stage_scope(self):
        for status in ('blocked', 'retrying'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as path:
                runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(Path(path)))
                try:
                    stage = runner.workflow['stages'][1]
                    request = {'id': 'current-methods-order', 'kind': 'additional_experiment',
                        'owner': 'methods.validation', 'objective': 'Resolve the current failure.',
                        'why': 'The current work failed.', 'success_condition': 'Independent validation.',
                        'evidence_needed': 'Current execution receipts.', 'target_stage_id': stage['id']}
                    runner.context[stage['id']] = {'status': 'research_expansion_required',
                                                   'research_requests': [request]}
                    runner.stage_records[stage['id']] = {'status': status, 'attempts': []}
                    runner.stage_usage_totals['stage:' + stage['id']] = {'model_calls': 24}
                    before = deepcopy(runner.stage_usage_totals)
                    by_id = {s['id']: s for s in runner.workflow['stages']}
                    self.assertEqual(runner._reconcile_pending_stage_holds(by_id), [stage['id']])
                    self.assertEqual(runner.stage_records[stage['id']]['status'], 'research_expansion_required')
                    self.assertTrue(runner._begin_continuation(set(), by_id))
                    self.assertEqual(runner.continuation_cycles, 1)
                    self.assertEqual(runner.active_research_requests[0]['id'], request['id'])
                    self.assertEqual(runner.stage_usage_totals, before)
                    self.assertEqual(runner._reconcile_pending_stage_holds(by_id), [])
                    runner.active_research_requests[0]['source_stage_id'] = 'survey'
                    runner.feedback.append({'action': 'continue_research',
                                            'research_requests': deepcopy(runner.active_research_requests)})
                    runner.stage_records[stage['id']]['status'] = 'retrying'
                    self.assertEqual(runner._reconcile_pending_stage_holds(by_id), [])
                    runner.feedback = []
                    runner.active_research_requests = [{'id': 'foreign-response', 'kind': 'recovery',
                        'target_stage_id': stage['id'], 'recovery_mode': 'format_repair_then_rerun',
                        'failure_dossier_ref': 'artifact:foreign@1'}]
                    self.assertEqual(runner._reconcile_pending_stage_holds(by_id), [])
                finally:
                    runner.close()

    def test_response_recovery_signature_preserves_exact_failure_owner(self):
        order = {'kind': 'recovery', 'recovery_mode': 'format_repair_then_rerun',
                 'target_stage_id': 'experiment', 'objective': 'Repair the response contract.',
                 'failure_dossier_ref': 'artifact:command/composer/failure-recovery/experiment/attempt-8@1',
                 'failure_input_sha256': 'a' * 64}
        signature = ComposerRunner._research_request_signature(order)
        for field, value in [('failure_dossier_ref', 'artifact:command/composer/failure-recovery/experiment/attempt-9@1'),
                             ('failure_input_sha256', 'b' * 64)]:
            changed = {**order, field: value}
            self.assertNotEqual(signature, ComposerRunner._research_request_signature(changed))
        self.assertEqual(signature, ComposerRunner._research_request_signature({**order, 'id': 'other-wrapper'}))
        ordinary = {**order, 'recovery_mode': 'repair_then_rerun'}
        self.assertEqual(ComposerRunner._research_request_signature(ordinary),
            ComposerRunner._research_request_signature({**ordinary, 'failure_input_sha256': 'b' * 64}))

    def test_continuation_replaces_admitted_old_response_owner(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(Path(path)))
            try:
                stage = runner.workflow['stages'][1]
                order = {'id': 'old-response-repair', 'kind': 'recovery', 'owner': 'methods.validation',
                         'objective': 'Repair the response.', 'why': 'The role contract failed.',
                         'success_condition': 'A valid response is captured.', 'evidence_needed': 'Original assignment.',
                         'source_stage_id': stage['id'], 'target_stage_id': stage['id'],
                         'recovery_mode': 'format_repair_then_rerun',
                         'failure_dossier_ref': 'artifact:failure/attempt-8@1', 'failure_input_sha256': 'a' * 64}
                current = {**order, 'id': 'current-response-repair',
                           'failure_dossier_ref': 'artifact:failure/attempt-9@1', 'failure_input_sha256': 'b' * 64}
                runner.active_research_requests = [order]
                runner._attempted_request_signatures.add(runner._research_request_signature(order))
                runner.feedback.append({'action': 'continue_research', 'research_requests': [order]})
                runner.context[stage['id']] = {'status': 'research_expansion_required',
                    'research_requests': [current], 'format_recovery': True, 'format_recovery_dispatched': False}
                self.assertTrue(runner._begin_continuation(set(), {s['id']: s for s in runner.workflow['stages']}))
                self.assertEqual([q['id'] for q in runner.active_research_requests], ['current-response-repair'])
                self.assertEqual(runner.active_research_requests[0]['failure_dossier_ref'], current['failure_dossier_ref'])
            finally:
                runner.close()

    def test_additional_owner_allowance_does_not_count_retained_calls_twice(self):
        from scisaurus.runtime.models import _reserve_model_call_budgets, _settle_model_token_budgets
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(root))
            try:
                stage = runner.workflow['stages'][1]
                stage['quota'] = {'max_model_calls': 6, 'max_input_tokens': 100000,
                                  'max_output_tokens': 100000, 'max_openalex_requests': 0}
                runner.workflow['capability_foundry_config_path'] = 'configured-by-test'
                scope = runner._stage_model_config(stage, {})['model_call_budget_scopes'][0]
                foundry = foundry_fixtures.CapabilityFoundryTests._foundry(root)
                cache = foundry_fixtures.CapabilityFoundryTests._cache(self, root)
                author = StubClient(foundry_fixtures.CapabilityFoundryTests._payload())
                for model in (author, foundry.validator_client, foundry.reviewer_client):
                    original = model.complete
                    def complete(*, system, prompt, original=original):
                        receipt = _reserve_model_call_budgets([scope], token_reservation={'input_tokens': 10, 'output_tokens': 4})
                        result = original(system=system, prompt=prompt)
                        _settle_model_token_budgets(receipt, {'input_tokens': 10, 'output_tokens': 4})
                        return result
                    model.complete = complete
                execute = foundry._execute
                def interrupt(source, payload):
                    if source == MINI_VALIDATOR and not json.loads(payload).get('readiness_probe'):
                        raise CapabilityDeadlineError('validator protocol deadline')
                    return execute(source, payload)
                with patch.object(foundry, '_execute', side_effect=interrupt):
                    with self.assertRaises(CapabilityDeadlineError):
                        foundry.generate('bounded comparison', client=author, work_cache=cache,
                            model_call_allowance=runner._foundry_model_call_budget(stage, repair_panel_usage={}))
                self.assertEqual(runner._stage_usage(stage['id'])['model_calls'], 2)
                with self.assertRaises(CapabilityModelBudgetExceeded):
                    foundry.generate('bounded comparison', client=author, work_cache=cache, model_call_allowance=0)
                outcome = foundry.generate('bounded comparison', client=author, work_cache=cache,
                    model_call_allowance=runner._foundry_model_call_budget(stage, repair_panel_usage={}))
                self.assertEqual(outcome['status'], 'registered')
                self.assertEqual((author.calls, foundry.validator_client.calls, foundry.reviewer_client.calls), (1, 1, 1))
                self.assertEqual(runner._stage_usage(stage['id'])['model_calls'], 3)
                self.assertEqual(cache.entries()[0]['usage']['model_calls'], 3)
                self.assertEqual(foundry.generate('bounded comparison', client=author, work_cache=cache,
                    model_call_allowance=0)['status'], 'registered')
                self.assertEqual(runner._stage_usage(stage['id'])['model_calls'], 3)
            finally:
                runner.close()

    def test_validator_format_budget_boundary_and_resume_do_not_reauthor_executor(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = foundry_fixtures.CapabilityFoundryTests._foundry(root)
            author = StubClient(foundry_fixtures.CapabilityFoundryTests._payload())
            cache = foundry_fixtures.CapabilityFoundryTests._cache(self, root)
            prompts = []
            class Validator:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    prompts.append(json.loads(prompt))
                    if inner.calls == 1:
                        return ModelResult('{' + 'repeated_helper' * 5000, 'independent',
                                           {'model_calls': 1}, 0, 'length')
                    return ModelResult(json.dumps({'validator_source': MINI_VALIDATOR}),
                                       'independent', {'model_calls': 1}, 0, 'stop')
            foundry.validator_client = Validator()
            with self.assertRaises(IndependentValidatorContractError) as caught:
                foundry.generate('bounded comparison', client=author, work_cache=cache,
                                 model_call_budget=2)
            self.assertEqual(classify_failure('experiment', caught.exception), 'model_contract')
            entry = cache.entries()[0]
            self.assertEqual(entry['last_attempt']['executor_source'], MINI_EXECUTOR)
            self.assertEqual(entry['repair_owner'], 'methods.validator-author')
            outcome = foundry.generate('bounded comparison', client=author, work_cache=cache,
                                      model_call_budget=3, resume_work_ref=entry['cache_ref'])
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual((author.calls, foundry.validator_client.calls), (1, 2))
            repair = prompts[1]['validator_repair']
            self.assertNotIn('text', repair['prior_response'])
            self.assertEqual(len(repair['prior_response']['response_sha256']), 64)
            self.assertNotIn('executor_source', prompts[1])

    def test_typed_contract_is_not_overridden_by_budget_diagnostic_but_429_is(self):
        error = IndependentValidatorContractError('prior model-call budget exhausted')
        self.assertEqual(classify_failure('experiment', error), 'model_contract')
        for diagnostic in ('independent program validator is invalid', 'independent recalculation checks must be objects'):
            owned = IndependentValidatorContractError('independent validator technical repair exhausted: ' + diagnostic)
            self.assertEqual(classify_failure('experiment', owned), 'model_contract')
        rate_limit = ModelCallError('429', status_code=429, outcome_known=True, attempts=1)
        rate_limit.failure_class = 'model_contract'
        self.assertEqual(classify_failure('experiment', rate_limit), 'resource_fence')

    def test_format_repair_does_not_reserve_inactive_methods_panel(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = composer_fixtures.ComposerWorkflowTests()._workflow(Path(path))
            runner = ComposerRunner(workflow)
            try:
                stage = next(s for s in runner.workflow['stages'] if s['kind'] == 'experiment')
                stage['quota'] = {'max_model_calls': 24}
                runner.workflow['capability_foundry_config_path'] = 'configured-by-test'
                runner.context[stage['id']] = {
                    'status': 'research_expansion_required',
                    'review_status': 'scientific_assignment_blocked',
                    'failure_class': 'model_contract',
                    'error': 'independent validator technical repair deferred: response incomplete',
                    'failure_recovery': {'requires_capability_repair': True},
                }
                self.assertFalse(runner._capability_repair_panel_required(stage))
                self.assertEqual(runner._foundry_model_call_budget(stage), 12)
            finally:
                runner.close()

    def test_completed_panel_cost_is_reserved_only_once_after_scope_settlement(self):
        from scisaurus.runtime.models import _reserve_model_call_budgets, _settle_model_token_budgets
        for native in (False, True):
            with self.subTest(native=native), tempfile.TemporaryDirectory() as path:
                runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(Path(path)))
                try:
                    stage = runner.workflow['stages'][1]
                    stage['quota'] = {'max_model_calls': 14, 'max_input_tokens': 1000,
                                      'max_output_tokens': 1000, 'max_openalex_requests': 0}
                    runner.workflow['capability_foundry_config_path'] = 'configured-by-test'
                    scope = runner._stage_model_config(stage, {})['model_call_budget_scopes'][0]
                    def dispatch():
                        if native:
                            for _ in range(3):
                                receipt = _reserve_model_call_budgets([scope], token_reservation={'input_tokens': 10, 'output_tokens': 4})
                                _settle_model_token_budgets(receipt, {'input_tokens': 10, 'output_tokens': 4})
                    panel = composer_fixtures.ComposerWorkflowTests._owned_legacy_repair_invoice(
                        runner, stage, {'model_calls': 3, 'input_tokens': 30, 'output_tokens': 12},
                        before_finish=dispatch, owner_scope=scope if native else None)
                    runner._record_capability_repair_panel_usage(stage, panel)
                    runner._stage_model_config(stage, {})
                    self.assertEqual(runner._stage_usage(stage['id'])['model_calls'], 3)
                    self.assertEqual(runner._foundry_model_call_budget(stage, repair_panel_usage={}), 9)
                    self.assertEqual(runner.usage['model_calls'], 3)
                finally:
                    runner.close()

    @staticmethod
    def _alignment_fixture():
        topic = {'id': 'topic', 'research_question': 'How does the maximum change?',
                 'disconfirmation_test': 'The difference of maxima is below tolerance.'}
        intent = {'primary_outcomes': [{'id': 'contrast', 'definition': 'Maximum of pointwise differences.'}]}
        packet = {'topic': topic, 'question_alignment': research_question_alignment(topic, intent),
                  'failure_lineage': {'identity_verified': True, 'stage_id': 'experiment',
                                      'failure_dossier_ref': 'artifact:failure@1',
                                      'attempt_number': 1, 'failure_input_sha256': 'a' * 64}}
        plan = {'disposition': 'repair', 'root_cause': {'statement': 'Clarify the estimand.',
                    'evidence': ['The two definitions differ.']},
                'required_changes': [{'target': 'executor', 'instruction': 'Preserve the declared quantity.',
                    'scientific_basis': 'Operator ordering changes meaning.', 'source_refs': []}],
                'acceptance_checks': [{'phase': 'execution', 'check': 'Independently recalculate.'}],
                'residual_uncertainties': [],
                'decision_alignment': {'original_question': topic['research_question'],
                    'original_decision_rule': topic['disconfirmation_test'], 'primary_outcome_id': 'contrast',
                    'quantity_definition': intent['primary_outcomes'][0]['definition'],
                    'baseline': 'Declared reference', 'aggregation': 'Pointwise difference then maximum',
                    'interpretation_limit': 'Conditional sensitivity only', 'changes_estimand': False,
                    'scientific_justification': 'Preserve the frozen definition.'},
                'evidence_checks': [{'claim': 'The current outcome is pointwise.',
                    'pointer': '/repair_adjudication_packet/question_alignment/candidate/primary_outcomes/0/definition',
                    'quote': intent['primary_outcomes'][0]['definition'], 'disposition': 'supported',
                    'explanation': 'Must reconcile with the original decision rule.'}]}
        return packet, plan

    def test_alignment_and_review_quote_checks_preserve_agent_owned_judgment(self):
        packet, plan = self._alignment_fixture()
        def validate(value):
            return ComposerRunner._validated_capability_repair_plan(
                {'status': 'succeeded', 'response': {'decision': 'repair', 'raw': {'repair_plan': value}}}, packet)
        self.assertIsNone(validate(plan)[1])
        for field, value in [('original_question', 'Another question'), ('quantity_definition', 'Invented meaning'),
                             ('changes_estimand', 'false')]:
            changed = deepcopy(plan)
            changed['decision_alignment'][field] = value
            self.assertIsNotNone(validate(changed)[1])
        for field, value in [('quote', 'Unrecentered difference.'), ('pointer', '/missing'),
                             ('disposition', 'accepted')]:
            changed = deepcopy(plan)
            changed['evidence_checks'][0][field] = value
            self.assertIsNotNone(validate(changed)[1])
        contradictory = deepcopy(plan)
        contradictory['required_changes'][0]['target'] = 'estimand'
        self.assertIsNotNone(validate(contradictory)[1])
        changed = deepcopy(plan)
        changed['decision_alignment']['changes_estimand'] = True
        changed['decision_alignment']['quantity_definition'] = 'Difference of maxima.'
        self.assertIsNotNone(validate(changed)[1])
        changed['required_changes'][0]['target'] = 'estimand'
        self.assertIsNone(validate(changed)[1])
        prompt = json.loads(build_repair_adjudication_prompt({}, packet, []))
        self.assertEqual(prompt['repair_adjudication_packet']['question_alignment'], packet['question_alignment'])
        self.assertIn('evidence_checks', prompt['decision_contract']['output_schema']['repair_plan'])
