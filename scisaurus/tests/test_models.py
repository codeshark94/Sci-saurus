"""Wire-level model protocol checks against an explicit local test server."""
import json
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import ModelClient, ModelCallError


class TestModelClient(unittest.TestCase):
    def setUp(self):
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.path = self.path
                outer.request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.calls += 1
                status = outer.status[min(outer.calls - 1, len(outer.status) - 1)] if isinstance(outer.status, list) else outer.status
                if status != 200:
                    self.send_response(status)
                    if status == 429:
                        self.send_header('Retry-After', '0')
                    self.end_headers()
                    self.wfile.write(b'private provider failure content')
                    return
                self.send_response(200)
                self.end_headers()
                response = (outer.response_sequence[min(outer.calls - 1, len(outer.response_sequence) - 1)]
                            if outer.response_sequence else outer.response)
                body = response if isinstance(response, (bytes, bytearray)) else json.dumps(response).encode()
                if outer.slow_body:
                    self.wfile.write(body[:1])
                    self.wfile.flush()
                    time.sleep(0.25)
                    self.wfile.write(body[1:])
                    self.wfile.flush()
                else:
                    self.wfile.write(body)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.status = 200
        self.calls = 0
        self.response_sequence = None
        self.slow_body = False
        self.response = {'model':'served-model','message':{'content':'{"value": 4}'}, 'done':True,
                         'done_reason':'stop', 'prompt_eval_count':10, 'eval_count':5}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def client(self, protocol='ollama', **kwargs):
        return ModelClient(base_url=self.url + ('/v1' if protocol=='openai_compatible' else ''),
                           protocol=protocol, model='configured-model', timeout_seconds=2, max_output_tokens=4096, **kwargs)

    def test_ollama_round_trip_and_reported_usage(self):
        result=self.client().complete(system='instruction', prompt='data')
        self.assertEqual(self.path, '/api/chat')
        self.assertEqual(self.request['options']['num_predict'],4096)
        self.assertFalse(self.request['stream'])
        self.assertEqual(result.json_object(), {'value':4})
        self.assertEqual(result.usage, {'model_calls':1,'input_tokens':10,'output_tokens':5})
        self.assertEqual(result.model,'served-model')

    def test_compatible_protocol_and_missing_usage_remains_unknown(self):
        self.response={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}]}
        result=self.client('openai_compatible').complete(system='instruction',prompt='data')
        self.assertEqual(self.path,'/v1/chat/completions')
        self.assertEqual(self.request['max_tokens'],4096)
        self.assertNotIn('reasoning_effort', self.request)
        self.assertNotIn('response_format', self.request)
        self.assertEqual(result.usage,{'model_calls':1})
        self.assertNotIn('input_tokens',result.usage)

    def test_compatible_reasoning_and_json_output_are_sent_exactly(self):
        self.response = {
            'choices': [{'message': {'content': '{"revised_text":"Association observed."}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 14, 'completion_tokens': 28, 'completion_tokens_details': {'reasoning_tokens': 12}},
        }
        result = self.client(
            'openai_compatible', reasoning_effort='high', output_format='json_object',
        ).complete(system='Return a JSON object.', prompt='Revise the claim.')
        self.assertEqual(self.path, '/v1/chat/completions')
        self.assertEqual(self.request['reasoning_effort'], 'high')
        self.assertEqual(self.request['response_format'], {'type': 'json_object'})
        self.assertEqual(self.request['max_tokens'], 4096)
        self.assertEqual(result.json_object(), {'revised_text': 'Association observed.'})
        self.assertEqual(result.usage, {'model_calls': 1, 'input_tokens': 14, 'output_tokens': 28})

    def test_explicit_reasoning_none_is_transmitted(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        self.client('openai_compatible', reasoning_effort='none').complete(system='Return JSON.', prompt='Inspect.')
        self.assertEqual(self.request['reasoning_effort'], 'none')
        self.assertNotIn('response_format', self.request)

    def test_compatible_multimodal_images_are_hash_pinned_and_embedded(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        png = b'\x89PNG\r\n\x1a\n' + b'fixture-image'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'figure.png'
            path.write_bytes(png)
            digest = hashlib.sha256(png).hexdigest()
            result = self.client('openai_compatible').complete(
                system='Return JSON.', prompt='Inspect the figure.', images=[{
                    'path': str(path.resolve()), 'media_type': 'image/png', 'sha256': digest,
                }])
        content = self.request['messages'][1]['content']
        self.assertEqual(content[0], {'type': 'text', 'text': 'Inspect the figure.'})
        self.assertEqual(content[1]['type'], 'image_url')
        url = content[1]['image_url']['url']
        self.assertTrue(url.startswith('data:image/png;base64,'))
        self.assertEqual(base64.b64decode(url.split(',', 1)[1]), png)
        self.assertEqual(result.json_object(), {'ok': True})

    def test_multimodal_input_rejects_drift_mismatch_and_unsupported_protocol(self):
        png = b'\x89PNG\r\n\x1a\n' + b'fixture-image'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'figure.png'
            path.write_bytes(png)
            valid = {'path': str(path.resolve()), 'media_type': 'image/png',
                     'sha256': hashlib.sha256(png).hexdigest()}
            with self.assertRaises(ValidationError):
                self.client().complete(system='x', prompt='x', images=[valid])
            with self.assertRaises(ValidationError):
                self.client('openai_compatible').complete(
                    system='x', prompt='x', images=[{**valid, 'sha256': '0' * 64}])
            with self.assertRaises(ValidationError):
                self.client('openai_compatible').complete(
                    system='x', prompt='x', images=[{**valid, 'media_type': 'image/jpeg'}])

    def test_multimodal_request_limits_are_enforced_before_network(self):
        png = b'\x89PNG\r\n\x1a\n' + b'x' * 64
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'figure.png'
            path.write_bytes(png)
            image = {'path': str(path.resolve()), 'media_type': 'image/png',
                     'sha256': hashlib.sha256(png).hexdigest()}
            with self.assertRaises(ValidationError):
                self.client('openai_compatible', max_image_bytes=32,
                            max_request_bytes=128).complete(system='x', prompt='x', images=[image])

    def test_invalid_or_unsupported_generation_options_fail_before_network(self):
        for value in ('', 'ultra', True, ['high']):
            with self.subTest(reasoning_effort=value), self.assertRaises(ValidationError):
                self.client('openai_compatible', reasoning_effort=value)
        for value in ('', 'json_schema', True, {'type': 'json_object'}):
            with self.subTest(output_format=value), self.assertRaises(ValidationError):
                self.client('openai_compatible', output_format=value)
        for options in ({'reasoning_effort': 'none'}, {'reasoning_effort': 'high'}, {'output_format': 'json_object'}):
            with self.subTest(ollama_options=options), self.assertRaises(ValidationError):
                self.client('ollama', **options)
        self.assertFalse(hasattr(self, 'request'))

    def test_json_output_mode_does_not_repair_non_json_model_text(self):
        self.response = {'choices': [{'message': {'content': '```json\n{"ok":true}\n```'}, 'finish_reason': 'stop'}]}
        result = self.client('openai_compatible', output_format='json_object').complete(system='Return JSON.', prompt='Inspect.')
        with self.assertRaises(ValidationError):
            result.json_object()

    def test_failure_does_not_expose_provider_body(self):
        self.status=401
        with self.assertRaises(ModelCallError) as error:
            self.client().complete(system='x',prompt='x')
        self.assertTrue(error.exception.outcome_known)
        self.assertNotIn('private',str(error.exception))
        self.status=503
        with self.assertRaises(ModelCallError) as error:
            self.client().complete(system='x',prompt='x')
        self.assertFalse(error.exception.outcome_known)

    def test_retryable_rate_limit_is_retried_within_one_request_budget(self):
        self.status = [429, 200]
        self.response = {'choices': [{'message': {'content': '{"value": 4}'}, 'finish_reason': 'stop'}]}
        result = self.client('openai_compatible', max_retries=2, retry_backoff_seconds=0).complete(
            system='x', prompt='x')
        self.assertEqual(self.calls, 2)
        self.assertEqual(result.json_object(), {'value': 4})

    def test_rate_limit_exhaustion_reports_status_without_provider_body(self):
        self.status = 429
        with self.assertRaises(ModelCallError) as error:
            self.client('openai_compatible', max_retries=2, retry_backoff_seconds=0).complete(
                system='x', prompt='x')
        self.assertEqual(self.calls, 3)
        self.assertTrue(error.exception.outcome_known)
        self.assertNotIn('private', str(error.exception))

    def test_malformed_response_is_retried_within_one_request_budget(self):
        self.response_sequence = [
            b'{"done":true}',
            {'model': 'served-model', 'message': {'content': '{"value": 4}'},
             'done': True, 'done_reason': 'stop'},
        ]
        result = self.client(max_retries=1, retry_backoff_seconds=0).complete(system='x', prompt='x')
        self.assertEqual(self.calls, 2)
        self.assertEqual(result.json_object(), {'value': 4})

    def test_incomplete_or_oversized_response_is_not_success(self):
        self.response['done']=False
        with self.assertRaises(ModelCallError):
            self.client().complete(system='x',prompt='x')
        self.response['done']=True
        with self.assertRaises(ModelCallError):
            self.client(max_response_bytes=10).complete(system='x',prompt='x')

    def test_body_transfer_is_bounded_by_absolute_timeout(self):
        self.slow_body = True
        client = ModelClient(base_url=self.url + '/v1', protocol='openai_compatible',
                             model='configured-model', timeout_seconds=0.08, max_output_tokens=5,
                             max_retries=0)
        started = time.monotonic()
        with self.assertRaises(ModelCallError) as error:
            client.complete(system='x', prompt='x')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('deadline', str(error.exception))

    def test_secrets_cannot_be_embedded_in_url_and_absent_key_is_rejected(self):
        with self.assertRaises(ValidationError):
            ModelClient(base_url='https://user:secret@example.com',protocol='ollama',model='x',timeout_seconds=2,max_output_tokens=5)
        with patch.dict('os.environ',{},clear=True), self.assertRaises(ValidationError):
            self.client(auth_env='TEST_MODEL_KEY')
