"""Wire-level model protocol checks against an explicit local test server."""
import json
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import (
    ModelClient, ModelCallError, ModelContextBudgetError, estimate_input_tokens,
    model_context_budget, model_context_error,
    resolve_model_config,
)


class TestModelClient(unittest.TestCase):
    def setUp(self):
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                outer.path = self.path
                outer.request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.calls += 1
                if outer.slow_headers:
                    time.sleep(0.5)
                status = outer.status[min(outer.calls - 1, len(outer.status) - 1)] if isinstance(outer.status, list) else outer.status
                if status != 200:
                    self.send_response(status)
                    if status == 429:
                        self.send_header('Retry-After', '0')
                    self.end_headers()
                    self.wfile.write(b'private provider failure content')
                    return
                self.send_response(200)
                if outer.trickle_chunked:
                    self.send_header('Transfer-Encoding', 'chunked')
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
                elif outer.trickle_body:
                    try:
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.02)
                    except BrokenPipeError:
                        pass
                elif outer.trickle_chunked:
                    try:
                        header = b'1;' + (b'x' * 64) + b'\r\n'
                        for byte in header:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.02)
                        self.wfile.write(b'x\r\n0\r\n\r\n')
                        self.wfile.flush()
                    except BrokenPipeError:
                        pass
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
        self.slow_headers = False
        self.trickle_body = False
        self.trickle_chunked = False
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

    def test_native_ollama_receives_role_specific_context_window(self):
        result = self.client('ollama', context_window_tokens=32768).complete(
            system='instruction', prompt='data')
        self.assertEqual(result.json_object(), {'value': 4})
        self.assertEqual(self.request['options']['num_ctx'], 32768)

    def test_openai_compatible_bridge_does_not_claim_dynamic_ollama_context(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        self.client('openai_compatible', context_window_tokens=32768).complete(
            system='instruction', prompt='data')
        self.assertNotIn('num_ctx', self.request)
        self.assertNotIn('options', self.request)

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

    def test_prompt_cache_hint_and_usage_are_preserved(self):
        self.response = {
            'model': 'qwen3.8-27b',
            'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
            'usage': {
                'prompt_tokens': 100,
                'completion_tokens': 28,
                'prompt_tokens_details': {'cached_tokens': 80},
                'created_cache_tokens': 20,
            },
        }
        result = self.client('openai_compatible', cache_prompt=True).complete(
            system='Stable instruction.', prompt='Different assignment.')
        self.assertTrue(self.request['cache_prompt'])
        self.assertEqual(result.usage, {
            'model_calls': 1,
            'input_tokens': 100,
            'output_tokens': 28,
            'cache_read_tokens': 80,
            'cache_write_tokens': 20,
        })

    def test_sampling_controls_are_sent_to_compatible_provider(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        self.client(
            'openai_compatible', temperature=1.1, top_p=0.95, seed=17,
            presence_penalty=0.2, frequency_penalty=-0.1,
        ).complete(system='Explore.', prompt='Propose a direction.')
        self.assertEqual({key: self.request[key] for key in (
            'temperature', 'top_p', 'seed', 'presence_penalty', 'frequency_penalty',
        )}, {
            'temperature': 1.1, 'top_p': 0.95, 'seed': 17,
            'presence_penalty': 0.2, 'frequency_penalty': -0.1,
        })

    def test_sampling_controls_are_nested_in_ollama_options(self):
        self.client(temperature=0.25, top_p=0.9, seed=3,
                    presence_penalty=0.2, frequency_penalty=0.1).complete(
            system='Check.', prompt='Validate.')
        self.assertEqual(self.request['options']['temperature'], 0.25)
        self.assertEqual(self.request['options']['top_p'], 0.9)
        self.assertEqual(self.request['options']['seed'], 3)
        self.assertNotIn('presence_penalty', self.request['options'])
        self.assertNotIn('frequency_penalty', self.request['options'])

    def test_role_profile_resolution_keeps_metadata_out_of_provider_config(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'configured-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_profiles': {
                'topic_discovery': {'temperature': 1.3, 'top_p': 0.98},
            },
        }
        resolved = resolve_model_config(base, role='topic_discovery', overrides={'seed': 19})
        self.assertEqual(resolved['temperature'], 1.3)
        self.assertEqual(resolved['top_p'], 0.98)
        self.assertEqual(resolved['seed'], 19)
        self.assertNotIn('role_profiles', resolved)

    def test_role_model_resolution_routes_provider_and_inherits_base_config(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 4096,
            'role_models': {
                'research.literature-mapper': {
                    'model': 'bounded-model', 'max_output_tokens': 512,
                    'temperature': 0.2,
                },
            },
        }
        resolved = resolve_model_config(base, role='research.literature-mapper')
        self.assertEqual(resolved['model'], 'bounded-model')
        self.assertEqual(resolved['base_url'], self.url)
        self.assertEqual(resolved['protocol'], 'openai_compatible')
        self.assertEqual(resolved['max_output_tokens'], 512)
        self.assertEqual(resolved['temperature'], 0.2)
        self.assertNotIn('role_models', resolved)

    def test_context_policy_resolves_per_role_and_is_not_sent_to_provider(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'context_window_tokens': 8192, 'max_input_tokens': 4000,
            'role_models': {
                'short-context-role': {
                    'model': 'small-model', 'context_window_tokens': 4096,
                    'max_input_tokens': 3000,
                },
            },
        }
        resolved = resolve_model_config(base, role='short-context-role')
        self.assertEqual(resolved['model'], 'small-model')
        self.assertEqual(resolved['context_window_tokens'], 4096)
        self.assertEqual(resolved['max_input_tokens'], 3000)
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        self.client('openai_compatible', context_window_tokens=8192,
                    max_input_tokens=4000).complete(system='Return JSON.', prompt='Inspect.')
        self.assertNotIn('context_window_tokens', self.request)
        self.assertNotIn('max_input_tokens', self.request)

    def test_context_budget_is_conservative_and_blocks_before_network(self):
        self.assertGreater(estimate_input_tokens('x', 'y' * 3000), 1000)
        client = self.client('openai_compatible', context_window_tokens=8192,
                             max_input_tokens=1000)
        with self.assertRaisesRegex(ModelContextBudgetError, 'context budget exceeded') as caught:
            client.complete(system='x', prompt='y' * 3000)
        self.assertEqual(caught.exception.failure_class, 'context_budget')
        self.assertGreater(caught.exception.estimated_input_tokens,
                           caught.exception.allowed_input_tokens)
        self.assertFalse(hasattr(self, 'request'))
        self.assertIsNone(model_context_error(
            {'model': 'fits', 'max_output_tokens': 64,
             'context_window_tokens': 4096, 'max_input_tokens': 3000},
            system='x', prompt='short'))

    def test_context_budget_projection_reports_the_same_effective_limit(self):
        value = {'model': 'fits', 'max_output_tokens': 512,
                 'context_window_tokens': 4096, 'max_input_tokens': 3000}
        budget = model_context_budget(value, system='x', prompt='y' * 9000)
        self.assertEqual(budget['allowed_input_tokens'], 3000)
        self.assertFalse(budget['fits'])
        self.assertEqual(
            model_context_error(value, system='x', prompt='y' * 9000),
            'model context budget exceeded for fits: conservative input estimate '
            f"{budget['estimated_input_tokens']} tokens exceeds 3000 input tokens; "
            'context window 4096 with max output 512')

    def test_context_policy_must_leave_output_room(self):
        with self.assertRaisesRegex(ValidationError, 'leave room'):
            self.client('openai_compatible', context_window_tokens=4096)
        with self.assertRaisesRegex(ValidationError, 'exceeds context_window_tokens'):
            self.client('openai_compatible', context_window_tokens=8192,
                        max_input_tokens=5000)

    def test_role_route_metadata_is_validated_and_not_forwarded_to_provider(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_routes': {
                'research.literature-mapper': [
                    {'id': 'ollama-route', 'pool': 'ollama', 'base_url': self.url,
                     'protocol': 'openai_compatible', 'model': 'ollama-model', 'auth_env': None},
                    {'id': 'qwen-route', 'pool': 'qwen', 'base_url': self.url,
                     'protocol': 'openai_compatible', 'model': 'qwen-model', 'auth_env': None},
                ],
            },
        }
        resolved = resolve_model_config(base, role='research.literature-mapper')
        self.assertEqual(resolved['model'], 'strong-model')
        self.assertNotIn('role_routes', resolved)

    def test_invalid_role_route_is_rejected_before_network(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_routes': {'research.cataloger': [
                {'id': 'broken', 'pool': 'qwen', 'base_url': self.url,
                 'protocol': 'openai_compatible', 'model': 'runtime_required'},
            ]},
        }
        with self.assertRaisesRegex(ValidationError, 'requires an explicit value'):
            resolve_model_config(base)

    def test_invalid_role_model_config_fails_before_network(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_models': {'research.cataloger': {'unsupported': 'value'}},
        }
        with self.assertRaisesRegex(ValidationError, 'unsupported fields'):
            resolve_model_config(base, role='research.cataloger')

    def test_role_model_can_suppress_inherited_credentials(self):
        base = {
            'base_url': self.url, 'protocol': 'openai_compatible', 'model': 'strong-model',
            'auth_env': 'OWNER_PRIVATE_KEY', 'timeout_seconds': 2, 'max_output_tokens': 64,
            'role_models': {
                'low-risk-draft': {
                    'base_url': self.url, 'model': 'weak-model', 'auth_env': None,
                },
            },
        }
        resolved = resolve_model_config(base, role='low-risk-draft')
        self.assertEqual(resolved['model'], 'weak-model')
        self.assertIsNone(resolved['auth_env'])

    def test_invalid_sampling_controls_fail_before_network(self):
        for field, value in (
                ('temperature', -0.1), ('temperature', 2.1), ('top_p', 0),
                ('top_p', 1.1), ('seed', -1), ('presence_penalty', 2.1),
                ('frequency_penalty', -2.1)):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                self.client('openai_compatible', **{field: value})
        self.assertFalse(hasattr(self, 'request'))

    def test_invalid_prompt_cache_hint_fails_before_network(self):
        for value in ("true", 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.client('openai_compatible', cache_prompt=value)
        self.assertFalse(hasattr(self, 'request'))

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
        for options in ({'reasoning_effort': 'none'}, {'reasoning_effort': 'high'},
                        {'reasoning_effort': 'xhigh'}, {'output_format': 'json_object'}):
            with self.subTest(ollama_options=options), self.assertRaises(ValidationError):
                self.client('ollama', **options)
        self.assertFalse(hasattr(self, 'request'))

    def test_json_output_mode_accepts_an_exact_json_markdown_fence(self):
        self.response = {'choices': [{'message': {'content': '```json\n{"ok":true}\n```'}, 'finish_reason': 'stop'}]}
        result = self.client('openai_compatible', output_format='json_object').complete(system='Return JSON.', prompt='Inspect.')
        self.assertEqual(result.json_object(), {'ok': True})

    def test_json_output_mode_still_rejects_prose_around_json(self):
        self.response = {'choices': [{'message': {'content': 'Here is the JSON:\n```json\n{"ok":true}\n```'}, 'finish_reason': 'stop'}]}
        result = self.client('openai_compatible', output_format='json_object').complete(system='Return JSON.', prompt='Inspect.')
        with self.assertRaises(ValidationError):
            result.json_object()

    def test_json_output_mode_accepts_explicit_reasoning_terminator_suffix(self):
        self.response = {'choices': [{'message': {
            'content': 'provider reasoning text</think>{"ok":true}',
        }, 'finish_reason': 'stop'}]}
        result = self.client('openai_compatible', output_format='json_object').complete(
            system='Return JSON.', prompt='Inspect.')
        self.assertEqual(result.json_object(), {'ok': True})

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

    def test_rate_limit_returns_to_scheduler_without_replaying_request(self):
        self.status = [429, 200]
        self.response = {'choices': [{'message': {'content': '{"value": 4}'}, 'finish_reason': 'stop'}]}
        with self.assertRaises(ModelCallError) as error:
            self.client('openai_compatible', max_retries=2, retry_backoff_seconds=0).complete(
                system='x', prompt='x')
        self.assertEqual(self.calls, 1)
        self.assertEqual(error.exception.status_code, 429)

    def test_rate_limit_exhaustion_reports_status_without_provider_body(self):
        self.status = 429
        with self.assertRaises(ModelCallError) as error:
            self.client('openai_compatible', max_retries=2, retry_backoff_seconds=0).complete(
                system='x', prompt='x')
        self.assertEqual(self.calls, 1)
        self.assertTrue(error.exception.outcome_known)
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(error.exception.retry_after_seconds, 0.0)
        self.assertEqual(error.exception.attempts, 1)
        self.assertIsNotNone(error.exception.elapsed_seconds)
        self.assertNotIn('private', str(error.exception))

    def test_malformed_provider_envelope_does_not_repeat_unknown_generation(self):
        self.response_sequence = [
            b'{"done":true}',
            {'model': 'served-model', 'message': {'content': '{"value": 4}'},
             'done': True, 'done_reason': 'stop'},
        ]
        with self.assertRaises(ModelCallError) as error:
            self.client(max_retries=1, retry_backoff_seconds=0).complete(system='x', prompt='x')
        self.assertEqual(self.calls, 1)
        self.assertFalse(error.exception.outcome_known)

    def test_shared_model_call_budget_counts_retries_and_blocks_before_network(self):
        self.status = [503, 200]
        self.response = {'choices': [{'message': {'content': '{"value": 4}'}, 'finish_reason': 'stop'}]}
        with tempfile.TemporaryDirectory() as directory:
            ledger = str((Path(directory) / 'model-budgets.sqlite').resolve())
            client = self.client(
                'openai_compatible', max_retries=1, retry_backoff_seconds=0,
                model_call_budget_path=ledger,
                model_call_budget_key='kimi-k3+glm-5.3',
                model_call_budget_limit=2,
            )
            result = client.complete(system='x', prompt='x')
            self.assertEqual(result.request_attempts, 2)
            self.assertEqual(self.calls, 2)
            connection = sqlite3.connect(ledger)
            try:
                self.assertEqual(
                    connection.execute(
                        'SELECT max_calls, used_calls FROM model_call_budgets WHERE budget_key=?',
                        ('kimi-k3+glm-5.3',),
                    ).fetchone(),
                    (2, 2),
                )
            finally:
                connection.close()
            with self.assertRaisesRegex(ModelCallError, 'budget exhausted'):
                client.complete(system='x', prompt='x')
            self.assertEqual(self.calls, 2)

    def test_exhausted_named_model_resolves_to_unbudgeted_fallback(self):
        self.response = {'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
        with tempfile.TemporaryDirectory() as directory:
            ledger = str((Path(directory) / 'model-budgets.sqlite').resolve())
            client = self.client(
                'openai_compatible', max_retries=0,
                model_call_budget_path=ledger,
                model_call_budget_key='kimi-k3+glm-5.3',
                model_call_budget_limit=1,
            )
            client.complete(system='x', prompt='x')
            resolved = resolve_model_config({
                'base_url': self.url + '/v1', 'protocol': 'openai_compatible',
                'model': 'base-model', 'timeout_seconds': 2, 'max_output_tokens': 64,
                'role_models': {
                    'impact-review': {
                        'model': 'kimi-k3:cloud',
                        'model_call_budget_path': ledger,
                        'model_call_budget_key': 'kimi-k3+glm-5.3',
                        'model_call_budget_limit': 1,
                    },
                },
                'role_model_fallbacks': {
                    'impact-review': [{'model': 'deepseek-v4.1-flash:cloud'}],
                },
            }, role='impact-review')
            self.assertEqual(resolved['model'], 'deepseek-v4.1-flash:cloud')

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

    def test_trickled_body_cannot_evade_absolute_timeout(self):
        self.trickle_body = True
        client = ModelClient(base_url=self.url + '/v1', protocol='openai_compatible',
                             model='configured-model', timeout_seconds=0.08, max_output_tokens=5,
                             max_retries=0)
        started = time.monotonic()
        with self.assertRaises(ModelCallError) as error:
            client.complete(system='x', prompt='x')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('deadline', str(error.exception))

    def test_trickled_chunk_header_cannot_evade_absolute_timeout(self):
        self.trickle_chunked = True
        client = ModelClient(base_url=self.url + '/v1', protocol='openai_compatible',
                             model='configured-model', timeout_seconds=0.08, max_output_tokens=5,
                             max_retries=0)
        started = time.monotonic()
        with self.assertRaises(ModelCallError) as error:
            client.complete(system='x', prompt='x')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn('deadline', str(error.exception))

    def test_slow_headers_cannot_evade_absolute_timeout(self):
        self.slow_headers = True
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
