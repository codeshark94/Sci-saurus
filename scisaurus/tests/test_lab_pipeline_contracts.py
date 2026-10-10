"""Experimental production preserves tool authority and observed raw values."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_foundry import CapabilityFoundry, candidate_prompt
from scisaurus.runtime.composer import ComposerRunner
from scisaurus.runtime.experiment import validate_program_output
from scisaurus.runtime.solver_observations import solver_observation_manifest, validate_solver_observations
from scisaurus.runtime.specialists import build_repair_adjudication_prompt, build_repair_evidence_prompt, _verifier_repair_packet
from scisaurus.runtime.software_workbench import software_computation_identity, selected_receipt_closure, SoftwareWorkbench
from scisaurus.tests import test_composer as composer_fixtures
from scisaurus.tests import test_program_foundry as fixtures


class LabPipelineContractTests(unittest.TestCase):
    def software(self, *, kind='container'):
        source, inp, output = 'print(7)', {'geometry': [1, 2]}, {'tensor': [[1, 2], [3, 4]], 'unit': 'Pa'}
        result = {'runtime': 'solver', 'purpose': 'scientific_computation', 'source': source,
                  'source_sha256': hashlib.sha256(source.encode()).hexdigest(), 'input': inp,
                  'input_sha256': hashlib.sha256(canonical_bytes(inp)).hexdigest(), 'output': output}
        result['execution'] = {'stdout': json.dumps(output)}
        result['stdout_sha256'] = hashlib.sha256(result['execution']['stdout'].encode()).hexdigest()
        return {'selection': {'strategy': 'reuse', 'computation_refs': ['receipt-a'], 'scientific_source_refs': []},
                'laboratory': {'runtimes': [{'label': 'solver', 'kind': kind}]},
                'operations': [{'receipt_ref': 'receipt-a', 'outcome': 'ok', 'result': result,
                                'action': {'operation': 'run', 'arguments': {'source': source, 'input': inp, 'runtime': 'solver'}}}]}

    def candidate(self, software):
        software = deepcopy(software)
        software['solver_observations'] = solver_observation_manifest(software)
        record = software['solver_observations']['records'][0]
        candidate = fixtures.output_document()
        candidate['observations'] = [{k: deepcopy(record[k]) for k in ('source_record_id', 'source_values')}]
        candidate['observations'][0]['condition'] = 'declared geometry'
        candidate['assets'] = []
        return candidate, {'scientific_software': software}

    def test_controller_fields_are_preserved_in_analysis_and_shared_admission(self):
        candidate, configured = self.candidate(self.software())
        intent = deepcopy(fixtures.INTENT); intent.update(run_count=1, required_assets=[])
        validate_program_output(candidate, intent, configured_input=configured)
        self.assertEqual(CapabilityFoundry._review({'experiment_intent': intent}, candidate, {})['status'], 'admitted')
        assessment = {'artifact_ref': 'owned', 'review': {}, 'evidence': {
            'selection': self.software()['selection'], 'selected_operations': self.software()['operations'],
            'laboratory': self.software()['laboratory']}}
        projected = ComposerRunner._scientific_software_projection(assessment)
        self.assertEqual(projected['solver_observations'], configured['scientific_software']['solver_observations'])
        self.assertIn('Container physics runs through the controller Workbench', projected['execution_contract'])

    def test_raw_mutation_omission_duplication_and_foreign_bundle_fail(self):
        candidate, configured = self.candidate(self.software())
        mutations = [[], candidate['observations'] * 2,
                     [{'source_record_id': 'foreign', 'source_values': {}}],
                     [{'source_record_id': 'receipt-a', 'source_values': {'tensor': [[99]]}}]]
        for rows in mutations:
            with self.assertRaises(ValidationError):
                validate_solver_observations({**candidate, 'observations': rows}, configured)
        foreign = deepcopy(configured)
        foreign['scientific_software']['solver_observations']['records'][0]['source_values']['tensor'][0][0] = 99
        with self.assertRaisesRegex(ValidationError, 'bundle differs'):
            validate_solver_observations(candidate, foreign)

    def test_source_input_runtime_and_receipt_ownership_fail_closed(self):
        for field, value in [('source_sha256', '0' * 64), ('input_sha256', '0' * 64), ('runtime', 'foreign')]:
            software = self.software(); software['operations'][0]['result'][field] = value
            with self.assertRaises(ValidationError): solver_observation_manifest(software)
        software = self.software(); other = deepcopy(software['operations'][0]); other['result']['source'] = 'foreign'
        software['operations'].append(other)
        with self.assertRaises(ValidationError): solver_observation_manifest(software)
        software = self.software(); software['operations'][0]['outcome'] = 'failed'
        with self.assertRaises(ValidationError): solver_observation_manifest(software)

    def extraction(self):
        software = self.software()
        parent = software['operations'][0]
        artifact = 'software-artifact:sha256:' + '1' * 64
        parent['result']['outputs'] = [{'name': 'field.nc', 'artifact_ref': artifact}]
        extraction = deepcopy(parent)
        extraction['receipt_ref'] = 'extraction'
        extraction['result']['runtime'] = extraction['action']['arguments']['runtime'] = 'extractor'
        extraction['action']['arguments']['inputs'] = [{'name': 'field.nc', 'artifact_ref': artifact}]
        extraction['result']['inputs'] = deepcopy(extraction['action']['arguments']['inputs'])
        extraction['result']['outputs'] = []
        software['operations'].append(extraction)
        software['laboratory']['runtimes'].append({'label': 'extractor', 'kind': 'venv'})
        software['selection']['computation_refs'] = ['extraction']
        return software

    def test_extraction_retains_container_ancestry_and_exact_fields(self):
        software = self.extraction()
        manifest = solver_observation_manifest(software)
        self.assertEqual([r['source_record_id'] for r in manifest['records']], ['extraction'])
        self.assertEqual([r['receipt_ref'] for r in manifest['dependencies']], ['receipt-a', 'extraction'])
        candidate, configured = self.candidate(software)
        validate_solver_observations(candidate, configured)
        retained = {'artifact_ref': 'owned', 'review': {}, 'evidence': {
            'selection': software['selection'], 'laboratory': software['laboratory'],
            'selected_operations': [software['operations'][1]],
            'discovery_and_diagnostics': [software['operations'][0]]}}
        self.assertEqual(ComposerRunner._scientific_software_projection(retained)['solver_observations'], manifest)
        self.assertEqual(len(retained['evidence']['selected_operations']), 1)
        candidate['observations'][0]['source_values'] = {'fabricated_measurement': 999}
        with self.assertRaises(ValidationError): validate_solver_observations(candidate, configured)
        software['operations'][0]['result']['input']['geometry'] = [3, 4]
        with self.assertRaises(ValidationError): solver_observation_manifest(software)

    def test_dependency_owner_missing_ambiguous_foreign_failed_or_cyclic_rejected(self):
        for outcome in ('missing', 'failed', 'ambiguous', 'foreign', 'cycle'):
            software = self.extraction()
            parent, extraction = software['operations']
            if outcome == 'missing': software['operations'] = [extraction]
            elif outcome == 'failed': parent['outcome'] = 'failed'
            elif outcome == 'ambiguous':
                other = deepcopy(parent); other['receipt_ref'] = 'other'; software['operations'].append(other)
            elif outcome == 'foreign': extraction['action']['arguments']['inputs'][0]['source_receipt_ref'] = 'foreign'
            elif outcome == 'cycle': parent['action']['arguments']['inputs'] = deepcopy(extraction['action']['arguments']['inputs'])
            with self.assertRaises(ValidationError): solver_observation_manifest(software)
        software = self.extraction(); other = deepcopy(software['operations'][0]); other['receipt_ref'] = 'unused'
        other['result']['runtime'] = 'unrelated-runtime'; software['operations'].append(other)
        software['operations'][1]['action']['arguments']['inputs'][0]['source_receipt_ref'] = 'receipt-a'
        manifest = solver_observation_manifest(software)
        self.assertNotIn('unused', [r['receipt_ref'] for r in manifest['dependencies']])

    def test_declared_input_producer_is_validated_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            bench = SoftwareWorkbench(directory, deadline=None)
            artifact = bench._retain_artifact(b'fields')
            declaration = {'name': 'field.nc', 'artifact_ref': artifact, 'source_receipt_ref': 'owner'}
            owner = {'result': {'outputs': [{'artifact_ref': artifact}]}}
            with patch.object(bench, '_receipt', return_value=owner) as resolve:
                declared = bench._declared_inputs([declaration], bench._effective_limits())
                self.assertEqual(declared[0]['source_receipt_ref'], 'owner')
                resolve.assert_called_once_with('owner', 'run')
            with patch.object(bench, '_receipt', return_value={'result': {'outputs': []}}):
                with self.assertRaises(ValidationError): bench._declared_inputs([declaration], bench._effective_limits())

    def test_native_scripts_do_not_acquire_an_upstream_field_restriction(self):
        software = self.software(kind='venv')
        self.assertIsNone(solver_observation_manifest(software))
        validate_solver_observations({'observations': [{'native_result': 3}]}, {'scientific_software': software})

    def test_prompt_uses_complete_fields_and_no_implicit_figure_floor(self):
        _, configured = self.candidate(self.software())
        prompt = candidate_prompt({}, [], configured)
        self.assertEqual(prompt['output_contract']['experiment_intent']['required_assets'], [])
        self.assertIn('source_values', prompt['executor_output_exact_shapes']['observations'][0])
        self.assertNotIn('at least three', json.dumps(prompt))
        explicit = candidate_prompt({}, [], {}, required_intent={'required_assets': [{'role': 'figure', 'media_types': ['image/png'], 'min_count': 2}]})
        self.assertEqual(explicit['output_contract']['experiment_intent']['required_assets'][0]['min_count'], 2)
        intent = deepcopy(fixtures.INTENT); intent['required_assets'][0]['min_count'] = 2
        with self.assertRaisesRegex(ValidationError, 'required asset'):
            validate_program_output(fixtures.output_document(), intent)

    def test_repair_lead_evidence_and_verifier_keep_full_laboratory(self):
        lab = {'runtimes': [{'label': f'solver-{i}', 'limitations': ['exact' * 1000]} for i in range(20)]}
        packet = {'laboratory': lab}
        lead = json.loads(build_repair_adjudication_prompt({}, packet, []))
        evidence = json.loads(build_repair_evidence_prompt({}, packet, {}))
        self.assertEqual(lead['repair_adjudication_packet']['laboratory'], lab)
        self.assertEqual(evidence['laboratory'], lab)
        for detail in ('full', 'compact', 'minimal', 'focused'):
            self.assertEqual(_verifier_repair_packet(packet, detail=detail)['laboratory'], lab)
        changed = deepcopy(packet); changed['laboratory']['runtimes'][0]['label'] = 'other'
        self.assertNotEqual(ComposerRunner._capability_repair_review_input_sha256(packet),
                            ComposerRunner._capability_repair_review_input_sha256(changed))

    def test_topic_boundary_matches_native_orchestration_policy(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path); workflow = composer_fixtures.ComposerWorkflowTests()._workflow(root)
            foundry = root/'foundry.json'; foundry.write_text(json.dumps({'runtime_packages': [], 'max_attempts': 2, 'timeout_seconds': 30}))
            runner = ComposerRunner(workflow)
            runner.workflow['capability_foundry_config_path'] = str(foundry)
            runner.laboratory_binding = SimpleNamespace(context=lambda: {'inventory': 'sealed'},
                feasibility=lambda: {'sealed': True, 'runtimes': []})
            context = runner._runtime_context({})
            boundary = context['capability_foundry']['execution_boundary']
            self.assertIn('bounded subprocess access', boundary)
            self.assertIn('Container solvers', boundary)
            self.assertNotIn('no network or subprocess access', boundary)

    def test_repair_diagnostics_do_not_change_new_scientific_orders(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path); runner = ComposerRunner(composer_fixtures.ComposerWorkflowTests()._workflow(root))
            stage = runner.workflow['stages'][1]
            with patch.object(runner, '_route_unexecuted_experiment_to_survey', return_value=None), \
                    patch.object(runner, '_experiment_attempt_reconciliation', return_value={'rule': 'inspect original receipts'}):
                orders = [runner._autonomous_experiment_repair_request(stage, {}, error, 1)
                          for error in ('transport failed at gate A', 'environment failed at gate B')]
            self.assertEqual(software_computation_identity({'work_orders': [orders[0]]}),
                             software_computation_identity({'work_orders': [orders[1]]}))
