"""Source-grounded concept selection and explicit stopped reselection."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import StateError, ValidationError
from scisaurus.runtime.composer import ComposerRunner
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.topic_discovery import (
    TopicDiscoveryRunner, validate_source_challenge, _source_challenge_prompt,
    _topic_repeat_score, topic_signature,
)
from scisaurus.tests.test_topic_discovery import package, frontier_plan
from scisaurus.tests.test_material_development import concept_candidates


def review(assessment='substantive_hypothesis'):
    admitted = assessment == 'substantive_hypothesis'
    return {
        'schema_version': 'topic-source-challenge-2', 'selected_id': 'direction_1',
        'decision': 'admit_to_survey' if admitted else 'refine',
        'source_relevance': 4, 'template_independence': 4,
        'prior_work_risk': 'medium' if admitted else 'high',
        'direct_comparison_match': False, 'closest_work_ids': ['W1'],
        'rationale': 'Specific response differs from the supplied reference mechanism.' if admitted else 'The function is already known.',
        'required_changes': [] if admitted else ['Change the useful physical function or address an unresolved tradeoff.'],
        'concept_differentiation': {
            'assessment': assessment, 'known_function': 'Established reference transport.',
            'proposed_departure': 'Different useful boundary response.',
            'closest_design_comparison': 'Equal dimensions and input energy against W1.',
            'strongest_alternative': 'Ordinary parameter tuning.',
            'discriminating_test': 'Matched reference calculation and mechanism ablation.',
        },
    }


class ConceptDifferentiationTests(unittest.TestCase):
    def test_physical_identity_survives_question_relabeling(self):
        original = concept_candidates()[0]
        original.update(title='Heat routing', research_question='Can directed heat transport isolate a boundary?', domain='thermal')
        candidate = deepcopy(original)
        candidate.update(id='different', title='Different label', research_question='Different words', domain='Other')
        candidate['design_brief']['parameters'][0]['upper'] = 999
        self.assertEqual(_topic_repeat_score(candidate, {'signature': topic_signature(original)}), 1.0)
        different = concept_candidates()[1]
        different.update(title='Optical selection', research_question='Can interference reject unwanted wavelengths?', domain='optical')
        self.assertLess(_topic_repeat_score(different, {'signature': topic_signature(original)}), .78)

    def test_known_variant_cannot_admit_by_unmatched_comparison(self):
        value = review('known_design_variant')
        validate_source_challenge(value, selected_id='direction_1', work_ids=['W1'], concept=True)
        value.update(decision='admit_to_survey', required_changes=[])
        with self.assertRaisesRegex(ValidationError, 'substantive source-grounded'):
            validate_source_challenge(value, selected_id='direction_1', work_ids=['W1'], concept=True)

    def test_evidence_and_exact_assessment_required(self):
        for alter in (lambda x: x.pop('concept_differentiation'),
                      lambda x: x.update(closest_work_ids=[]),
                      lambda x: x.update(closest_work_ids=['W999']),
                      lambda x: x['concept_differentiation'].update(proposed_departure='')):
            value = review(); alter(value)
            with self.assertRaises(ValidationError):
                validate_source_challenge(value, selected_id='direction_1', work_ids=['W1'], concept=True)

    def test_concept_prompt_has_no_automatic_admit_instruction(self):
        payload = json.loads(_source_challenge_prompt({'id': 'direction_1'}, [], {'topic_intake_mode': 'concept'}))
        self.assertIn('concept_differentiation', payload['output_contract'])
        self.assertNotIn('If direct_comparison_match is false and source_relevance', ' '.join(payload['output_constraints']))

    def test_proposal_then_screen_then_source_informed_full_comparison(self):
        calls = []
        source = {'work_id': 'W1', 'title': 'Known reference physical design', 'year': 2014, 'abstract': 'Known mechanism.'}
        class Model:
            def __init__(self, **kwargs): self.model = 'offline'
            def complete(self, *, system, prompt, images=None):
                payload = json.loads(prompt); calls.append(payload)
                if payload['assignment'] in {'concept_closest_design_challenge', 'repair_topic_source_challenge'}:
                    value = review('known_design_variant' if len([x for x in calls if x['assignment'] == 'concept_closest_design_challenge']) == 1 else 'substantive_hypothesis')
                    value['selected_id'] = payload['selected_topic']['id']
                else:
                    value = package(payload['principal_objective'])
                    for candidate, concept in zip(value['candidates'], concept_candidates()):
                        candidate.update(mechanism=concept['mechanism'], design_brief=concept['design_brief'],
                            research_form='theory_simulation', evidence_mode='synthetic_simulation', comparison_type='mechanism_ablation')
                    if payload['assignment'] == 'refine_topic_discovery':
                        for candidate in value['candidates'][:1]:
                            candidate['research_question'] = 'Does a distinct boundary response enable a different useful transport function?'
                            candidate['id'] = 'boundary_switch'
                            value['selected_id'] = 'boundary_switch'
                            candidate['title'] = 'Boundary-controlled transport reversal'
                            candidate['comparison'] = 'Boundary reversal versus matched transport controls.'
                            candidate['measurement'] = 'Net reversed energy transport.'
                            candidate['mechanism'] = 'Boundary exchange changes the direction of net energy transfer.'
                return ModelResult(text=json.dumps(value), model='offline', finish_reason='stop', usage={'input_tokens': 20, 'output_tokens': 20}, elapsed_seconds=.01)
        with patch('scisaurus.runtime.topic_discovery.ModelClient', Model), patch.object(
                TopicDiscoveryRunner, '_recent_paper_sample', return_value=([source], 3, [{'query': 'bounded closest designs'}])) as sampling:
            result = TopicDiscoveryRunner({'base_url': 'http://offline.invalid', 'model': 'offline', 'protocol': 'ollama', 'timeout_seconds': 1}).run(
                'Develop a useful material', candidate_count=3, intake_mode='concept', max_attempts=2, bibliography={}, sampling_seed=3)
        self.assertEqual([c['assignment'] for c in calls], ['free_topic_discovery', 'concept_closest_design_challenge', 'refine_topic_discovery', 'concept_closest_design_challenge'])
        self.assertEqual(calls[2]['closest_design_evidence'], [source])
        self.assertEqual(calls[2]['repair_specification']['action'], 'replace_known_mechanism')
        self.assertNotIn('required_changes', calls[2]['repair_specification'])
        self.assertEqual(calls[2]['source_challenge']['required_changes'], review('known_design_variant')['required_changes'])
        self.assertIn('Discard the retired', calls[2]['refinement_instruction'])
        self.assertNotIn('Address every required_changes', calls[2]['refinement_instruction'])
        rules = ' '.join(calls[2]['constraints'])
        self.assertNotIn('explicitly address every item', rules)
        self.assertNotIn('keep the parent', rules)
        self.assertNotIn('repair the reviewed direction', rules)
        self.assertIn('new concept selection', rules)
        self.assertEqual(calls[3]['retired_concept']['candidate'], calls[2]['retired_direction'])
        self.assertEqual(calls[3]['retired_concept']['source_challenge']['concept_differentiation']['assessment'], 'known_design_variant')
        self.assertEqual(len(result['candidates']), 3)
        self.assertEqual(result['source_challenge']['concept_differentiation']['assessment'], 'substantive_hypothesis')
        self.assertEqual(result['novelty_status'], 'unverified')
        self.assertEqual(sampling.call_count, 2)
        self.assertFalse(sampling.call_args.kwargs['prefer_recent'])

    def test_concept_combination_does_not_require_seed_evidence_before_proposal(self):
        from scisaurus.runtime.topic_discovery import topic_prompt
        payload = json.loads(topic_prompt('A useful multifunctional design', 3, intake_mode='concept'))
        rules = ' '.join(payload['constraints'])
        self.assertNotIn('do not couple several mechanisms', rules)
        self.assertIn('field or parameter interfaces', rules)
        self.assertIn('single-function controls or mechanism ablations', rules)
        ordinary = json.loads(topic_prompt('A scientific comparison', 3))
        self.assertIn('do not couple several mechanisms', ' '.join(ordinary['constraints']))

    def test_retired_mechanism_is_bound_to_current_challenge_prompt(self):
        retired = {'candidate': {'id': 'prior', 'mechanism': 'Known mechanism'}, 'source_challenge': review('known_design_variant')}
        context = {'topic_intake_mode': 'concept', 'retired_concept': retired}
        payload = json.loads(_source_challenge_prompt({'id': 'replacement'}, [], context))
        self.assertEqual(payload['retired_concept'], retired)
        self.assertIn('return known_design_variant', ' '.join(payload['output_constraints']))
        retired['candidate']['mechanism'] = 'tampered'
        self.assertNotEqual(payload['retired_concept'], retired)

    def test_targeted_search_retains_classic_closest_design(self):
        class Bibliography:
            def __init__(self, **kwargs): pass
            def run(self, *, query, **kwargs):
                return {'outcome': 'ok', 'works': [{'id': 'W1', 'title': query, 'year': 2014}, {'id': 'W2', 'title': query, 'year': 2025}]}
        with patch('scisaurus.runtime.topic_discovery.OpenAlexClient', Bibliography):
            records, _, _ = TopicDiscoveryRunner._recent_paper_sample('Design', sampling_seed=3,
                frontier_seed_plan=frontier_plan(1), minimum_seed_groups=1, prefer_recent=False)
        self.assertEqual({x['year'] for x in records}, {2014, 2025})

    def workflow(self, root):
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        workflow = ComposerWorkflowTests()._workflow(root)
        topic = root/'topic'; topic.mkdir()
        config = root/'topic.json'; config.write_text(json.dumps({'intake_mode': 'concept'}))
        stage = deepcopy(workflow['stages'][0]); stage.update(id='topic', kind='topic_discovery', config_path=str(config), project_dir=str(topic))
        workflow['stages'].insert(0, stage); workflow['stages'][1]['depends_on'] = ['topic']
        return workflow

    def test_reselection_preserves_usage_deadline_and_requires_operator_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); workflow = self.workflow(root)
            runner = ComposerRunner(workflow)
            runner.context['topic'] = {'kind': 'topic_discovery', 'status': 'completed', 'topic': {'id': 'prior'}}
            runner.stage_records['topic'] = {'status': 'completed', 'project_dir': str(root/'topic'), 'attempts': []}
            runner.context['survey'] = {'status': 'running', 'topic_id': 'prior', 'raw': 'retained'}
            runner.status = 'paused'; runner._checkpoint('paused', force=True)
            control = root/'composer/output/run-control.json'; control.write_text('{"stop_requested":true}')
            runner.close()
            runner = ComposerRunner(workflow, resume=True, control_only=True)
            try:
                deadline, usage = runner.deadline_epoch, deepcopy(runner.usage)
                result = runner.reselect_concepts('Prioritize substantive new useful functions within attested tools.')
                self.assertEqual(result['deadline_at_epoch'], deadline)
                self.assertEqual(runner.status, 'paused')
                self.assertEqual(runner.usage, usage)
                self.assertIn('survey', result['reopened_stage_ids'])
                context = runner._topic_refinement_context(workflow['stages'][0])
                self.assertEqual(context['mode'], 'concept_reselection')
                self.assertNotIn('parent_topic', context)
                request = runner.active_research_requests[0]
                original = deepcopy(request)
                for altered in ({'objective': 'tampered'}, {'recovery_mode': 'ordinary_refinement'},
                                {'arbitrary_unbound': True}, {'target_stage_id': 'survey'},
                                {'topic_cycle': 999}):
                    request.clear(); request.update(original); request.update(altered)
                    with self.assertRaises(StateError): runner._topic_refinement_context(workflow['stages'][0])
                request.clear(); request.update(original)
                runner.continuation_cycles += 1
                with self.assertRaises(StateError): runner._topic_refinement_context(workflow['stages'][0])
            finally: runner.close()

    def test_reselection_rejects_running_frontier(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ComposerRunner(self.workflow(Path(tmp)))
            try:
                with self.assertRaises(StateError): runner.reselect_concepts('New selection criteria')
            finally: runner.close()
