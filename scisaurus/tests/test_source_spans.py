"""Stable source-span contract tests."""
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.core.source_spans import bind, locate, validate


class SourceSpanTests(unittest.TestCase):
    def test_binds_and_validates_an_unambiguous_character_span(self):
        source = {"work_id": "W1", "text": "alpha exact quotation omega"}
        value = {"evidence": [{"work_id": "W1", "source_ref": "source", "quote": "exact quotation"}]}
        bound = bind(value, {"source": source})
        proof = bound["evidence"][0]
        self.assertEqual(source["text"][proof["start"]:proof["end"]], "exact quotation")
        validate(proof, source, require_span=True)

    def test_rejects_ambiguous_or_hash_mismatched_quotation(self):
        source = {"work_id": "W1", "text": "repeat and repeat"}
        with self.assertRaisesRegex(ValidationError, "more than once"):
            locate(source, "repeat")
        proof = {"work_id": "W1", "source_ref": "source", "quote": "repeat",
                 "start": 0, "end": 6, "quote_sha256": "0" * 64}
        with self.assertRaisesRegex(ValidationError, "SHA-256"):
            validate(proof, source, require_span=True)

    def test_span_must_stay_inside_the_displayed_window(self):
        source = {"work_id": "W1", "text": "hidden visible"}
        proof = {"work_id": "W1", "source_ref": "source", "quote": "hidden", **locate(source, "hidden")}
        with self.assertRaisesRegex(ValidationError, "outside"):
            validate(proof, source, require_span=True, window={"start": 7, "end": 14})


if __name__ == "__main__":
    unittest.main()
