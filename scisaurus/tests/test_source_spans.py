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

    def test_bind_restores_one_whitespace_only_transport_change_to_exact_source(self):
        source = {"work_id": "W1", "text": "A robust estimator works\nunder finite variance."}
        value = {"work_id": "W1", "source_ref": "artifact:source@1",
                 "quote": "estimator works under finite variance"}
        bound = bind(value, {"artifact:source@1": source})
        self.assertEqual(bound["quote"], "estimator works\nunder finite variance")
        self.assertEqual(source["text"][bound["start"]:bound["end"]], bound["quote"])
        validate(bound, source, require_span=True)

    def test_bind_does_not_repair_changed_words_or_punctuation(self):
        source = {"work_id": "W1", "text": "The sample mean is optimal, under Gaussian data."}
        with self.assertRaisesRegex(ValidationError, "exact captured text"):
            bind({"work_id": "W1", "source_ref": "artifact:source@1",
                  "quote": "The sample median is optimal under Gaussian data."},
                 {"artifact:source@1": source})

    def test_bind_relocates_a_valid_quote_with_stale_model_offsets(self):
        source = {"work_id": "W1", "text": "prefix; exact quotation; suffix"}
        value = {"work_id": "W1", "source_ref": "source", "quote": "exact quotation",
                 "start": 0, "end": 15, "quote_sha256": "0" * 64}
        bound = bind(value, {"source": source})
        self.assertEqual(source["text"][bound["start"]:bound["end"]], bound["quote"])
        validate(bound, source, require_span=True)

    def test_bind_keeps_a_valid_span_unchanged(self):
        source = {"work_id": "W1", "text": "prefix; exact quotation; suffix"}
        value = {"work_id": "W1", "source_ref": "source", "quote": "exact quotation",
                 **locate(source, "exact quotation")}
        self.assertEqual(bind(value, {"source": source}), value)

    def test_bind_reports_every_invalid_quote_location_for_targeted_repair(self):
        source = {"work_id": "W1", "text": "alpha beta gamma"}
        value = {"first": {"work_id": "W1", "source_ref": "source", "quote": "missing one"},
                 "second": [{"work_id": "W1", "source_ref": "source", "quote": "missing two"}]}
        with self.assertRaises(ValidationError) as caught:
            bind(value, {"source": source})
        self.assertIn("$.first", str(caught.exception))
        self.assertIn("$.second[0]", str(caught.exception))

    def test_span_must_stay_inside_the_displayed_window(self):
        source = {"work_id": "W1", "text": "hidden visible"}
        proof = {"work_id": "W1", "source_ref": "source", "quote": "hidden", **locate(source, "hidden")}
        with self.assertRaisesRegex(ValidationError, "outside"):
            validate(proof, source, require_span=True, window={"start": 7, "end": 14})


if __name__ == "__main__":
    unittest.main()
