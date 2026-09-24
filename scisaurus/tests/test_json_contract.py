"""Model parsing and replay validation share one lossless JSON contract."""

import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import json_object
from scisaurus.core.surveys import SurveyGate
from scisaurus.runtime.models import ModelResult


class TestJSONContract(unittest.TestCase):
    def test_model_wrappers_decode_identically_on_execution_and_replay(self):
        for text in ('{"ok":true}', '```json\n{"ok":true}\n```',
                     'reasoning</think>{"ok":true}',
                     'reasoning</think>```json\n{"ok":true}\n```',
                     'analysis transcript before the final answer\n{"ok":true}',
                     '{"ok":true}"}'):
            with self.subTest(text=text):
                result = ModelResult(text, "fixture", {}, 0, "stop")
                self.assertEqual(result.json_object(), {"ok": True})
                self.assertEqual(result.json_object(), json_object(text, model_envelope=True))

    def test_ambiguous_or_incomplete_model_json_is_never_repaired_by_parser(self):
        invalid = ('{"x":1,"x":2}', '{"nested":{"x":1,"x":2}}',
                   '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e999}', '{"x":-1e999}',
                   '{"x":', '{"x":1} trailing', '[]', 'null',
                   '{"x":1,}')
        for raw in invalid:
            for text in (raw, f'```json\n{raw}\n```', f'reasoning</think>{raw}'):
                with self.subTest(text=text), self.assertRaises(ValidationError):
                    ModelResult(text, "fixture", {}, 0, "stop").json_object()
        self.assertEqual(
            ModelResult('{"x":1', "fixture", {}, 0, "stop").json_object(
                allow_missing_closers=True),
            {"x": 1},
        )
        with self.assertRaises(ValidationError):
            ModelResult('{"x":', "fixture", {}, 0, "stop").json_object(
                allow_missing_closers=True)
        self.assertEqual(
            ModelResult('// comment\n{"x":1}', "fixture", {}, 0, "stop").json_object(),
            {"x": 1},
        )
        for text in ('Here is JSON:\n```json\n{"x":1}\n```',
                     '```json\n{"x":1}\n```\nmore',
                     '```json\n{"x":1}\n```\n```json\n{"x":2}\n```'):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                json_object(text, model_envelope=True)

    def test_reasoning_delimiter_inside_json_string_is_preserved(self):
        body = '{"text":"A literal </think> appears here."}'
        for text in (body, 'reasoning</think>' + body,
                     'reasoning</think>```json\n' + body + '\n```'):
            with self.subTest(text=text):
                self.assertEqual(ModelResult(text, "fixture", {}, 0, "stop").json_object(),
                                 {"text": "A literal </think> appears here."})

    def test_control_records_remain_plain_strict_json(self):
        self.assertEqual(SurveyGate._json(b'{"ok":true}', "record"), {"ok": True})
        for raw in ('```json\n{"ok":true}\n```', 'reasoning</think>{"ok":true}',
                    '{"x":1,"x":2}', '{"x":NaN}', None, b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                SurveyGate._json(raw, "record")
