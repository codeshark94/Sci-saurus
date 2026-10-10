"""Account exhaustion retains paid evidence without retrying science work."""
import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ProviderRateLimitError
from scisaurus.runtime.composer import ComposerRunner
from scisaurus.runtime.composer_supervisor import ComposerSupervisor
from scisaurus.runtime.dsh_batch import DshBatchError, dsh_provider_failure
from scisaurus.runtime.models import ModelCallError, _provider_http_error_kind


class QuotaTests(unittest.TestCase):
    def test_account_windows_and_transient_throttles_are_distinct(self):
        for body in [b'{"message":"you have reached your weekly usage limit"}',
                     b'{"error":"monthly cloud usage limit reached"}',
                     b'{"error":{"code":"insufficient_quota"}}']:
            self.assertEqual(_provider_http_error_kind(body), 'quota_exhausted')
        self.assertEqual(_provider_http_error_kind(b'{"error":"too many requests; retry later"}'), 'rate_limited')
        self.assertIsNone(_provider_http_error_kind(b'{"error":"request input limit exceeded"}'))

    def test_http_weekly_usage_message_is_paid_once_and_raw_prefix_is_retained(self):
        from scisaurus.tests.test_models import TestModelClient
        fixture = TestModelClient()
        fixture.setUp()
        try:
            fixture.status = 429
            fixture.failure_body = b'{"message":"weekly usage limit reached; add credits"}'
            with self.assertRaises(ModelCallError) as caught:
                fixture.client('openai_compatible', max_retries=2, retry_backoff_seconds=0).complete(system='x', prompt='y')
            error = caught.exception
            self.assertEqual(fixture.calls, 1)
            self.assertEqual(error.provider_error_kind, 'quota_exhausted')
            self.assertEqual(error.usage['model_calls'], 1)
            self.assertEqual(base64.b64decode(error.provider_response['body_prefix_base64']), fixture.failure_body)
            self.assertEqual(error.provider_response['body_prefix_sha256'], hashlib.sha256(fixture.failure_body).hexdigest())
        finally:
            fixture.tearDown()
            fixture.doCleanups()

    def test_dsh_typed_terminal_preserves_unknown_and_usage(self):
        reason = {'kind': 'error', 'error': {'code': 'RATE_LIMIT',
            'message': '429: {"message":"weekly usage limit reached"}'}}
        failure = dsh_provider_failure(reason)
        error = DshBatchError('files not accepted', receipt='/owned/receipt.json',
            usage={'model_calls': 39, 'output_tokens': 53608}, provider_failure=failure)
        stopped = ComposerRunner._provider_quota_stop(error)
        self.assertIsInstance(stopped, ProviderRateLimitError)
        self.assertEqual(stopped.usage, error.usage)
        self.assertEqual(error.failure_details()['details']['terminal_reason'], reason)
        self.assertEqual(error.failure_details()['details']['backend_receipt'], error.receipt)
        self.assertIsNone(dsh_provider_failure({'kind': 'completed', 'error': reason['error']}))
        self.assertIsNone(dsh_provider_failure({'kind': 'error', 'error': {'code': 'OTHER', 'message': reason['error']['message']}}))
        transient = DshBatchError('throttled', receipt='/owned/receipt.json', usage={},
            provider_failure=dsh_provider_failure({'kind': 'error', 'error': {'code': 'RATE_LIMIT', 'message': 'too many requests'}}))
        self.assertIs(ComposerRunner._provider_quota_stop(transient), transient)

    def test_bounded_provider_response_roundtrips_without_exposing_message(self):
        raw = b'{"message":"weekly usage limit reached"}'
        evidence = {'body_prefix_base64': base64.b64encode(raw).decode(),
            'body_prefix_sha256': hashlib.sha256(raw).hexdigest(), 'captured_bytes': len(raw)}
        error = ModelCallError('HTTP 429', status_code=429, outcome_known=True, attempts=1,
            provider_error_kind='quota_exhausted', provider_response=evidence)
        error.usage = {'model_calls': 1}
        restored = ModelCallError.from_failure(str(error), error.failure_details())
        self.assertEqual(restored.failure_details(), error.failure_details())
        self.assertNotIn('weekly', str(restored))
        self.assertEqual(base64.b64decode(restored.provider_response['body_prefix_base64']), raw)

    def test_composer_and_supervisor_stop_without_a_reset(self):
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        for source in ('http', 'dsh'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                workflow = ComposerWorkflowTests()._workflow(Path(temp))
                workflow['retry_policy'] = {'mode':'until_deadline', 'backoff_seconds':0}
                runner = ComposerRunner(workflow)
                self.addCleanup(runner.close)
                calls = []
                error = ModelCallError('HTTP 429', status_code=429, provider_error_kind='quota_exhausted', outcome_known=True, attempts=1)
                if source == 'dsh':
                    error = DshBatchError('unknown final', receipt='/owned/receipt.json', usage={}, provider_failure={
                        'status_code':429, 'provider_error_kind':'quota_exhausted', 'retry_after_known':False})
                error.usage = {'model_calls':1, 'input_tokens':12, 'output_tokens':3}
                def reject(stage, **kwargs):
                    calls.append(stage['id'])
                    raise error
                runner._run_stage = reject
                result = runner.run()
                self.assertEqual(calls, ['survey'])
                self.assertEqual(result['status'], 'paused')
                self.assertEqual(result['usage']['model_calls'], 1)
                self.assertEqual(result['active_blockers'][0]['stop_reason'], 'provider_rate_limit')
                self.assertFalse(result['retry_schedule'])
                self.assertFalse(runner._continuation_requests())
                supervisor = ComposerSupervisor(workflow)
                with patch.object(supervisor, '_execution_allowed', return_value=True):
                    self.assertFalse(supervisor._should_resume(result))

    def test_stage_cache_wrapper_preserves_dsh_quota_before_generic_failure(self):
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        with tempfile.TemporaryDirectory() as temp:
            workflow = ComposerWorkflowTests()._workflow(Path(temp))
            runner = ComposerRunner(workflow)
            self.addCleanup(runner.close)
            error = DshBatchError('unknown final', receipt='/owned/receipt.json',
                usage={'model_calls':39, 'output_tokens':53608}, provider_failure={
                    'provider_error_kind':'quota_exhausted', 'status_code':429})
            with patch.object(runner, '_execute_stage', side_effect=error):
                with self.assertRaises(ProviderRateLimitError) as caught:
                    runner._run_stage(workflow['stages'][0])
            self.assertEqual(caught.exception.usage, error.usage)
            self.assertEqual(caught.exception.details['backend_receipt'], error.receipt)

    def test_owned_terminal_quota_does_not_accept_partial_files(self):
        from scisaurus.tests.test_dsh_batch import BatchTests, FAKE
        fixture = BatchTests()
        fixture.setUp()
        try:
            reason = {'kind':'error', 'error':{'code':'RATE_LIMIT', 'message':'429: weekly usage limit reached'}}
            script = FAKE.replace("{'kind':'max-tokens' if mode=='length' else 'completed'}", repr(reason))
            fixture.script.write_text(script)
            with self.assertRaises(DshBatchError) as caught:
                fixture.run_job(fixture.config())
            receipt = json.loads(Path(caught.exception.receipt).read_text())
            self.assertEqual(receipt['status'], 'result_unknown')
            self.assertTrue(receipt['process_reaped'])
            self.assertEqual(receipt['usage']['model_calls'], 1)
            self.assertEqual(receipt['provider_failure']['terminal_reason'], reason)
            self.assertEqual(caught.exception.failure_details()['kind'], 'provider_rate_limit')
            self.assertNotIn('output_directory', receipt)
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
