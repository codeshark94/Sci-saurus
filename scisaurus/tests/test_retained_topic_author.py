"""Receipt ownership and deterministic recovery boundaries for topic revisions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.composer import ComposerRunner
from scisaurus.runtime.dsh_batch import DshStructuredProducerClient, read_completed_structured_producer
from scisaurus.runtime.topic_discovery import TopicDiscoveryRunner


class ReceiptTests(unittest.TestCase):
    def test_settled_receipt_and_tamper_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'input').mkdir(); (root / 'work').mkdir()
            assignment = {'assignment': 'repair_selected_topic_candidate', 'value': 1}
            inputs = {'assignment.json': json.dumps(assignment), 'system-contract.txt': 'Scientific contract'}
            for name, text in inputs.items():
                (root / 'input' / name).write_text(text)
            output = b'{"candidate":{}}'
            (root / 'work/response.json').write_bytes(output)
            receipt = {'schema_version': 'dsh-batch-receipt-1', 'status': 'completed', 'process_reaped': True,
                       'model': 'fixed', 'config_sha256': 'a' * 64,
                       'task_sha256': hashlib.sha256(DshStructuredProducerClient.task(None, assignment).encode()).hexdigest(),
                       'input_sha256': {name: hashlib.sha256(text.encode()).hexdigest() for name, text in inputs.items()},
                       'outputs': {'response.json': hashlib.sha256(output).hexdigest()},
                       'usage': {'model_calls': 4, 'input_tokens': 100, 'output_tokens': 50}}
            def write(value):
                (root / 'receipt.json').write_text(json.dumps(value))
            write(receipt)
            self.assertEqual(read_completed_structured_producer(root, config_sha256='a' * 64)[1], assignment)
            for key, value in [('status', 'result_unknown'), ('process_reaped', False), ('model', ''),
                               ('config_sha256', 'b' * 64), ('task_sha256', 'b' * 64),
                               ('usage', {'model_calls': True, 'input_tokens': 100, 'output_tokens': 50})]:
                altered = deepcopy(receipt); altered[key] = value; write(altered)
                with self.subTest(key=key), self.assertRaises(ValidationError):
                    read_completed_structured_producer(root, config_sha256='a' * 64)
            write(receipt)
            for file in ['input/assignment.json', 'input/system-contract.txt', 'work/response.json']:
                path = root / file; original = path.read_bytes(); path.write_bytes(original + b' ')
                with self.subTest(file=file), self.assertRaises(ValidationError):
                    read_completed_structured_producer(root, config_sha256='a' * 64)
                path.write_bytes(original)
            (root / 'work/response.json').unlink()
            (root / 'work/response.json').symlink_to(root / 'input/assignment.json')
            with self.assertRaisesRegex(ValidationError, 'symlink'):
                read_completed_structured_producer(root, config_sha256='a' * 64)


class ScopeTests(unittest.TestCase):
    def test_foreign_pivot_never_restores_old_frontier(self):
        runner = ComposerRunner.__new__(ComposerRunner)
        runner.stage_records = {'topic': {'verifier_outcome': 'not_evaluated', 'failure_class': 'scientific_review'}}
        runner.context = {'topic': {'candidate_attempt_trace': [{'status': 'refinement_rejected',
            'rejection_type': 'refinement', 'error': 'old deterministic rejection', 'selected_topic': {'id': 'x'}}],
            'topic_pivot': {'status': 'required', 'source_stage_id': 'survey',
                            'scientific_basis': {'kind': 'source_review'}}}}
        runner.active_research_requests = [{'id': 'independent-science-order'}]
        with patch('scisaurus.runtime.topic_discovery.validate_retained_topic_author_response',
                   side_effect=AssertionError('must reject before receipt recovery')):
            self.assertFalse(runner._resume_disproven_topic_validation(set(),
                {'topic': {'id': 'topic', 'kind': 'topic_discovery'}}))

    def test_retained_response_rejects_changed_objective_order_or_runtime_before_dispatch(self):
        parent = {'id': 'x', 'research_question': 'q', 'phenomenon': 'p', 'domain': 'd'}
        evidence = {'request': {'parent_candidate': parent, 'principal_objective': 'original',
            'composer_repair_context': {'work_orders': [], 'review_owner_evidence': None},
            'runtime_context': {'laboratory': {'identity': 'old'}}}}
        runner = TopicDiscoveryRunner({'base_url': 'http://example.invalid', 'model': 'fake',
            'protocol': 'ollama', 'timeout_seconds': 1, 'max_output_tokens': 32768})
        cases = [dict(objective='different', orders=[], runtime={'identity': 'old'}),
                 dict(objective='original', orders=[{'id': 'foreign'}], runtime={'identity': 'old'}),
                 dict(objective='original', orders=[], runtime={'identity': 'new'})]
        with patch('scisaurus.runtime.topic_discovery.validate_retained_topic_author_response'), \
                patch('scisaurus.runtime.topic_discovery.ModelClient.complete',
                      side_effect=AssertionError('no provider dispatch')):
            for case in cases:
                with self.subTest(case=case), self.assertRaisesRegex(ValidationError, 'different scientific assignment'):
                    runner.run(case['objective'], sampling_seed=1, candidate_count=1,
                        intake_mode='concept', runtime_context={'laboratory': case['runtime']},
                        refinement_context={'parent_topic': parent, 'work_orders': case['orders']},
                        resume_author_response=evidence)
