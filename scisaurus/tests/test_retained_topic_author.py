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


class CapabilityContractTests(unittest.TestCase):
    def test_declarations_are_bound_to_actual_author_contract(self):
        from scisaurus.runtime.topic_discovery import (
            _validate_authored_capability_contract, capability_requirements_contract)
        request = {"output_contract": {"candidate": {
            "capability_requirements": capability_requirements_contract()}}}
        from scisaurus.tests.test_topic_discovery import package
        candidate = deepcopy(package("Scientific comparison")["candidates"][0])
        candidate.pop("capability_requirements")
        for response in ({"candidate": candidate}, candidate):
            with self.subTest(response=response), self.assertRaisesRegex(
                    ValidationError, "omits output_contract capability_requirements"):
                _validate_authored_capability_contract(response, request)
        portfolio = {"output_contract": {"candidates": {"items": request["output_contract"]["candidate"]}}}
        with self.assertRaisesRegex(ValidationError, "omits output_contract"):
            _validate_authored_capability_contract({"candidates": [candidate]}, portfolio)
        for response in ({"candidate": candidate}, candidate):
            with self.subTest(portfolio_response=response), self.assertRaisesRegex(
                    ValidationError, "omits output_contract"):
                _validate_authored_capability_contract(response, portfolio)
        _validate_authored_capability_contract({"candidates": [candidate]}, request)
        _validate_authored_capability_contract({"candidate": candidate},
            {"output_contract": {"candidate": {"id": "exact identifier"}}})
        authored = {"id": "direction", "capability_requirements": {
            "executables": [], "python_packages": ["numpy", "matplotlib"],
            "stage_kinds": ["experiment"], "runtime_labels": ["waves"]}}
        original = deepcopy(authored)
        _validate_authored_capability_contract({"candidate": authored}, request)
        self.assertEqual(authored, original)
        authored["capability_requirements"]["stage_kinds"] = ["homogenization"]
        with self.assertRaisesRegex(ValidationError, "unsupported stage kind"):
            _validate_authored_capability_contract({"candidate": authored}, request)

    def test_portfolio_refinement_and_review_share_dependency_contract(self):
        from scisaurus.runtime.topic_discovery import (capability_requirements_contract,
            topic_prompt, _topic_candidate_refinement_prompt)
        from scisaurus.runtime.specialists import build_verifier_prompt
        from scisaurus.tests.test_topic_discovery import package
        objective = "A bounded scientific comparison"
        original = package(objective)
        parent = original["candidates"][1]
        initial = json.loads(topic_prompt(objective, 3, runtime_context={}))
        repair = json.loads(_topic_candidate_refinement_prompt(objective, parent,
            base_package=original, target_shape={key: parent[key] for key in (
                "research_form", "evidence_mode", "comparison_type")},
            target_seed={"target_seed_id": "frontier_1", "eligible_seed_ids": ["frontier_1"], "target_work_ids": []},
            frontier_seeds=[], recent_papers=[], runtime_context={}, refinement_feedback={}))
        review = json.loads(build_verifier_prompt({"id": "topic", "kind": "topic_discovery"},
            {}, [], {"topic": parent}))
        expected = capability_requirements_contract()
        self.assertEqual(initial["output_contract"]["candidates"]["items"]["capability_requirements"], expected)
        self.assertEqual(repair["output_contract"]["candidate"]["capability_requirements"], expected)
        self.assertEqual(review["execution_capability_contract"], expected)
        self.assertFalse(any("do not emit it" in item for item in initial.get("constraints", [])))
        self.assertFalse(any("copied from the parent" in item for item in repair["constraints"]
                             if "capability_requirements" in item))
        self.assertIn("sealed", expected["python_packages"])
