"""Stable source-span contract tests."""
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.core.source_spans import (bind, expand_evidence, index_evidence,
                                        index_source_windows, locate, validate)


class SourceSpanTests(unittest.TestCase):
    def test_disjoint_windows_preserve_absolute_spans_and_hide_gaps(self):
        source = {"work_id": "W1", "text": "head quote. hidden result. tail quote."}
        windows = {"source": [{"start": 0, "end": 11}, {"start": 27, "end": 38}]}
        value = {"evidence": [{"work_id": "W1", "source_ref": "source", "quote": quote}
                              for quote in ("head quote.", "tail quote.")]}
        bound = bind(value, {"source": source}, windows=windows)
        self.assertEqual([proof["start"] for proof in bound["evidence"]], [0, 27])
        indexed, catalog = index_evidence(bound, {"source": source})
        self.assertEqual(expand_evidence(indexed, catalog, {"source": source}, windows=windows), bound)
        hidden = bind({"work_id": "W1", "source_ref": "source", "quote": "hidden result."}, {"source": source})
        with self.assertRaisesRegex(ValidationError, "outside"):
            bind(hidden, {"source": source}, windows=windows)
        crossing = bind({"work_id": "W1", "source_ref": "source", "quote": source["text"]}, {"source": source})
        with self.assertRaisesRegex(ValidationError, "outside"):
            validate(crossing, source, require_span=True, window=windows["source"])

    def test_union_location_and_transport_restoration_reject_ambiguity(self):
        source = {"work_id": "W1", "text": "first\nquote. gap first\nquote."}
        windows = [{"start": 0, "end": 12}, {"start": 17, "end": 29}]
        for quote in ("first\nquote.", "first quote."):
            with self.subTest(quote=quote), self.assertRaises(ValidationError):
                bind({"work_id": "W1", "source_ref": "source", "quote": quote},
                     {"source": source}, windows={"source": windows})
        bound = bind({"work_id": "W1", "source_ref": "source", "quote": "first quote."},
                     {"source": source}, windows={"source": windows[:1]})
        self.assertEqual(bound["quote"], "first\nquote.")
        self.assertEqual(bound["start"], 0)

    def test_window_index_merges_overlap_without_bridging_gaps(self):
        rows = [{"source_ref": "source", "window": window} for window in
                [{"start": 20, "end": 30}, {"start": 0, "end": 8},
                 {"start": 5, "end": 10}, {"start": 10, "end": 12}]]
        self.assertEqual(index_source_windows(rows),
                         {"source": [{"start": 0, "end": 12}, {"start": 20, "end": 30}]})
        source = {"work_id": "W1", "text": "unique quotation"}
        self.assertEqual(locate(source, "unique", window=[{"start": 0, "end": 12},
                                                         {"start": 0, "end": 16}])["start"], 0)

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

    def test_bind_restores_rendered_math_aliases_and_standalone_variable_duplication(self):
        source = {"work_id": "W1", "text": (
            "Unless we know something about ff beyond continuity of its\n"
            "derivatives, it is impossible to say what ξ\\xi is.")}
        value = {"work_id": "W1", "source_ref": "artifact:source@1",
                 "quote": "Unless we know something about f beyond continuity of its derivatives, "
                          "it is impossible to say what ξ is."}
        bound = bind(value, {"artifact:source@1": source})
        self.assertEqual(source["text"][bound["start"]:bound["end"]], bound["quote"])
        self.assertIn("ff", bound["quote"])
        self.assertIn("ξ\\xi", bound["quote"])
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

    def test_bind_completes_a_digest_without_changing_a_supplied_span(self):
        source = {"work_id": "W1", "text": "repeat then repeat"}
        value = {"work_id": "W1", "source_ref": "source", "quote": "repeat", "start": 12, "end": 18}
        bound = bind({"evidence": [value]}, {"source": source})['evidence'][0]
        self.assertEqual({key: bound[key] for key in value}, value)
        self.assertNotIn('quote_sha256', value)
        validate(bound, source, require_span=True)
        self.assertEqual(bind(bound, {"source": source}), bound)

    def test_positioned_quote_without_digest_rejects_invalid_or_hidden_locators(self):
        source = {"work_id": "W1", "text": "repeat then repeat"}
        proof = {"work_id": "W1", "source_ref": "source", "quote": "repeat", "start": 0, "end": 6}
        for invalid in ({**proof, 'end': 5}, {**proof, 'start': True}, {**proof, 'quote': 'changed'},
                        {**proof, 'work_id': 'W2'}, {**proof, 'source_ref': 'unavailable'}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                bind(invalid, {"source": source})
        with self.assertRaisesRegex(ValidationError, 'outside'):
            bind(proof, {"source": source}, windows={'source': {'start': 12, 'end': 18}})

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
        with self.assertRaisesRegex(ValidationError, "outside"):
            bind(proof, {"source": source}, windows={"source": {"start": 7, "end": 14}})

    def test_evidence_catalog_deduplicates_and_replays_exact_source_spans(self):
        sources = {"source": {"work_id": "W1", "text": "prefix exact quotation suffix"}}
        proof = {"work_id": "W1", "source_ref": "source", "quote": "exact quotation"}
        value = {"evidence": [proof], "comparison": {"evidence": [proof]}}
        projected, catalog = index_evidence(value, sources)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(projected["evidence"], projected["comparison"]["evidence"])
        self.assertEqual(expand_evidence(projected, catalog, sources), bind(value, sources))
        self.assertEqual(index_evidence(bind(value, sources), sources), (projected, catalog))

    def test_evidence_catalog_accepts_bare_ids_inside_evidence_arrays(self):
        sources = {"source": {"work_id": "W1", "text": "prefix exact quotation suffix"}}
        proof = {"work_id": "W1", "source_ref": "source", "quote": "exact quotation"}
        projected, catalog = index_evidence({"evidence": [proof]}, sources)
        selected = {"evidence": [catalog[0]["evidence_id"]]}
        expanded = expand_evidence(selected, catalog, sources)
        self.assertEqual(expanded, bind({"evidence": [proof]}, sources))

    def test_valid_hidden_span_cannot_relocate_to_a_visible_duplicate(self):
        text = "exact quotation hidden separator exact quotation"
        source = {"work_id": "W1", "text": text}
        visible_start = text.rindex("exact quotation")
        proof = {"work_id": "W1", "source_ref": "source", "quote": "exact quotation",
                 **locate(source, "exact quotation", window={"start": 0, "end": 15})}
        with self.assertRaisesRegex(ValidationError, "outside"):
            bind(proof, {"source": source}, windows={"source": {"start": visible_start, "end": len(text)}})

    def test_assessment_rebinding_keeps_hidden_positioned_evidence_on_its_source(self):
        from scisaurus.core.surveys import SurveyGate
        sources = {"hidden": {"work_id": "W1", "text": "hidden quotation. tail."},
                   "visible": {"work_id": "W1", "text": "hidden quotation."}}
        windows = {"hidden": {"start": 18, "end": 23},
                   "visible": {"start": 0, "end": 17}}
        positioned = {"work_id": "W1", "source_ref": "hidden", "quote": "hidden quotation.",
                      "start": 0, "end": 17}
        hashed = bind(positioned, sources)
        for proof in (positioned, hashed):
            with self.subTest(has_digest="quote_sha256" in proof):
                value = {"evidence": [proof]}
                rebound = SurveyGate._rebind_assessment_sources(value, sources, windows)
                self.assertEqual(rebound, value)
                with self.assertRaisesRegex(ValidationError, "outside"):
                    bind(rebound, sources, windows=windows)

    def test_evidence_catalog_rejects_forgery_unknown_ids_and_hidden_spans(self):
        sources = {"source": {"work_id": "W1", "text": "hidden visible"}}
        proof = {"work_id": "W1", "source_ref": "source", "quote": "hidden"}
        selected, catalog = index_evidence(proof, sources)
        for value in ({"evidence_id": "unknown"}, {**selected, "quote": "invented"}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                expand_evidence(value, catalog, sources)
        with self.assertRaisesRegex(ValidationError, "does not match"):
            expand_evidence(selected, [{**catalog[0], "quote": "visible"}], sources)
        with self.assertRaisesRegex(ValidationError, "duplicated"):
            expand_evidence(selected, catalog * 2, sources)
        with self.assertRaisesRegex(ValidationError, "different work"):
            expand_evidence(selected, catalog, {"source": {**sources["source"], "work_id": "W2"}})
        with self.assertRaisesRegex(ValidationError, "outside"):
            expand_evidence(selected, catalog, sources, windows={"source": {"start": 7, "end": 14}})


if __name__ == "__main__":
    unittest.main()
