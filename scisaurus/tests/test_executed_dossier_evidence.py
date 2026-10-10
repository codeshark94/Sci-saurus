"""Lossless repair evidence from a failed experiment's completed child executions."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from copy import deepcopy

from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_registry import experiment_program_payload, experiment_validation_payload
from scisaurus.runtime.composer import ComposerRunner
from scisaurus.runtime.specialists import (
    build_specialist_prompt, repair_adjudication_evidence_document, _verifier_repair_packet)
from scisaurus.tests.test_execution_review_evidence import capture


class ExecutedDossierEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'experiment'
        self.attempt = self.root / 'continuations/cycle-2/attempts/attempt-3'
        for name in ('state', 'objects/sha256', 'output'):
            (self.attempt / name).mkdir(parents=True)
        self.connection = sqlite3.connect(self.attempt / 'state/control.sqlite')
        self.addCleanup(self.connection.close)
        self.connection.execute('CREATE TABLE artifacts (artifact_ref TEXT PRIMARY KEY, body_hash TEXT)')
        self.runner = object.__new__(ComposerRunner)
        self.runner.workflow = {'stages': [{'id': 'experiment', 'kind': 'experiment', 'project_dir': str(self.root)}]}
        self.intent = {'id': 'cell', 'revision': 2, 'study_type': 'simulation', 'domain': 'physics',
                       'research_question': 'Which declared contrast is reproducible?', 'hypothesis': 'A contrast exists.',
                       'method': 'Solve the cell.', 'parameters': {'fraction': .25}, 'seed': 1, 'run_count': 1,
                       'stopping_rule': 'One pilot.', 'primary_outcomes': [], 'limitations': ['Finite pilot.']}
        self.configured = {'work_orders': [], 'scientific_software': {'selection': {'strategy': 'reuse'}}}
        self.candidate = {'schema_version': 'experiment-program-output-1', 'study_id': 'cell', 'revision': 2,
                          'procedures': ['Solve.'], 'metrics': [], 'findings': [], 'limitations': ['Finite pilot.'],
                          'assets': [], 'observations': [{'id': str(i), 'value': i, **({'mask': [0, 1]} if i % 2 else {})}
                                                       for i in range(90)]}
        self.sha = self.write(self.attempt / 'output/raw-results.json', self.candidate)
        self.sources = {'executor': '# immutable geometry\n' * 1800, 'validator': '# independent reduction\n' * 1250}
        self.payload = experiment_program_payload(self.intent, self.configured)
        self.validation_input = experiment_validation_payload(self.intent, self.configured, self.candidate, self.sha)
        self.verdict = {'schema_version': 'experiment-validation-1', 'candidate_sha256': self.sha,
                        'study_id': 'cell', 'decision': 'rejected', 'checks': [], 'metric_recalculations': [],
                        'limitations': ['Scalar-only reduction.']}
        self.records = {}
        for role, payload, document in [('executor', self.payload, self.candidate),
                                         ('validator', self.validation_input, self.verdict)]:
            ref = f'artifact:command/executions/{role}@1'
            source_path = str(self.attempt / 'workspace' / role / 'execution.py')
            details = {'command': ['python', source_path], 'source_files': [
                {'path': source_path, 'capture': capture(self.sources[role].encode())}]}
            stdin, stdout = canonical_bytes(payload), canonical_bytes(document)
            self.records[role] = {'outcome': 'ok', 'input': payload, 'document': document,
                                 'input_capture': capture(stdin), 'input_sha256': hashlib.sha256(stdin).hexdigest(),
                                 'capture': capture(stdout), 'capture_sha256': hashlib.sha256(stdout).hexdigest(),
                                 'metadata': {'process_returncode': 0, 'capture_truncated': False,
                                              'capture_incomplete': False, 'sandbox_required': True,
                                              'source_dispatch_mode': 'private_read_only_snapshot',
                                              'command_identity': {'details': details,
                                                  'sha256': hashlib.sha256(canonical_bytes(details)).hexdigest()}}}
            self.publish(ref, self.records[role])
        self.publish('artifact:methods/validation@1', {**self.verdict,
                     'execution_ref': 'artifact:command/executions/validator@1'})
        self.run = {'status': 'blocked', 'study_id': 'cell', 'research_question': self.intent['research_question'],
                    'raw_results': str(self.attempt / 'output/raw-results.json'), 'raw_results_sha256': self.sha,
                    'execution_refs': ['artifact:command/executions/executor@1'],
                    'deterministic_validation_ref': 'artifact:methods/validation@1'}
        self.run_path = self.attempt / 'output/run.json'
        self.run_sha = self.write(self.run_path, self.run)
        self.observed = {**self.run, 'output_path': str(self.run_path), 'output_path_sha256': self.run_sha}

    @staticmethod
    def write(path, value):
        body = canonical_bytes(value)
        path.write_bytes(body)
        return hashlib.sha256(body).hexdigest()

    def publish(self, ref, value):
        body = canonical_bytes(value); sha = hashlib.sha256(body).hexdigest()
        (self.attempt / 'objects/sha256' / sha).write_bytes(body)
        self.connection.execute('INSERT OR REPLACE INTO artifacts VALUES (?,?)', (ref, sha))
        self.connection.commit()
        return sha

    def evidence(self, observed=None, number=3):
        return self.runner._executed_dossier_evidence('experiment', number, observed or self.observed)

    def test_complete_sources_observations_and_rejected_validator_remain_inspectable(self):
        evidence = self.evidence()
        self.assertTrue(evidence['available'], evidence)
        self.assertFalse(evidence['admissible_as_verified_claims'])
        self.assertEqual(evidence['validator_verdict']['decision'], 'rejected')
        for role, source in self.sources.items():
            self.assertEqual(''.join(evidence['source_files'][role]['source_chunks']), source)
            self.assertEqual(evidence['source_files'][role]['expected_sha256'], hashlib.sha256(source.encode()).hexdigest())
        table = evidence['program_output']['observations']
        decoded = [dict(zip(table['schemas'][table.get('schema_ids', [0] * 90)[i]], row))
                   for i, row in enumerate(table['rows'])]
        self.assertEqual(decoded, self.candidate['observations'])

    def test_full_evidence_survives_every_repair_prompt_projection(self):
        evidence = self.evidence()
        packet = {'executed_study_evidence': {k: v for k, v in evidence.items() if k != 'source_files'},
                  'prior_foundry_work': {'last_attempt': {'experiment_intent': self.intent}},
                  'exact_candidate_sources': {role: {**record, 'source_sha256': record['expected_sha256']}
                                              for role, record in evidence['source_files'].items()}}
        lead = repair_adjudication_evidence_document(packet)
        peer = json.loads(build_specialist_prompt({'role_id': 'reproducibility-reviewer'},
            {'repair_panel': True, 'capability_repair_packet': packet}))['projected_input']['capability_repair_packet']
        verifier = _verifier_repair_packet(packet, detail='minimal')
        author = ComposerRunner._capability_authoring_repair_projection({'packet': packet})
        for document in (lead, peer, verifier, author):
            self.assertEqual(document['executed_study_evidence'], packet['executed_study_evidence'])
        for document in (lead, verifier):
            self.assertTrue(document['candidate_program']['exact_execution_sources']['executor']['complete'])
            self.assertEqual(document['candidate_program']['experiment_intent'], self.intent)

    def test_foreign_attempt_question_refs_and_file_digest_are_rejected(self):
        for key, value in [('study_id', 'foreign'), ('research_question', 'Other question'),
                           ('execution_refs', ['artifact:command/executions/foreign@1']),
                           ('raw_results_sha256', '0' * 64), ('output_path_sha256', '0' * 64),
                           ('deterministic_validation_ref', 'artifact:methods/foreign@1')]:
            observed = {**self.observed, key: value}
            self.assertFalse(self.evidence(observed)['available'], key)
        self.assertFalse(self.evidence(number=4)['available'])

    def test_modified_capture_failed_transport_and_foreign_validator_input_are_rejected(self):
        for role, change in [('executor', 'source'), ('executor', 'input'), ('executor', 'truncated'),
                              ('validator', 'source'), ('validator', 'input'), ('validator', 'document')]:
            with self.subTest(role=role, change=change):
                bad = deepcopy(self.records[role])
                if change == 'source':
                    bad['metadata']['command_identity']['details']['source_files'][0]['capture']['body'] = 'invalid'
                elif change == 'input':
                    bad['input']['configured_input']['changed'] = True
                elif change == 'truncated':
                    bad['metadata']['capture_truncated'] = True
                else:
                    bad['document']['decision'] = 'accepted'
                self.publish(f'artifact:command/executions/{role}@1', bad)
                self.assertFalse(self.evidence()['available'])
                self.publish(f'artifact:command/executions/{role}@1', self.records[role])

    def test_no_receipt_or_changed_current_run_does_not_substitute_workspace_source(self):
        self.connection.execute("DELETE FROM artifacts WHERE artifact_ref LIKE '%validator@1'")
        self.connection.commit()
        self.assertFalse(self.evidence()['available'])

    def test_unvalidated_execution_retains_executor_evidence_without_inventing_verdict(self):
        self.run.pop('deterministic_validation_ref')
        self.observed.pop('deterministic_validation_ref')
        self.observed['output_path_sha256'] = self.write(self.run_path, self.run)
        evidence = self.evidence()
        self.assertTrue(evidence['available'], evidence)
        self.assertEqual(set(evidence['source_files']), {'executor'})
        self.assertFalse(evidence['validator_verdict']['available'])
        self.assertIsNone(evidence['validator_execution'])
        self.write(self.run_path, {**self.run, 'status': 'completed'})
        self.assertFalse(self.evidence()['available'])
