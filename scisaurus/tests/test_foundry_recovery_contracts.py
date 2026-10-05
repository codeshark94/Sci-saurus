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
