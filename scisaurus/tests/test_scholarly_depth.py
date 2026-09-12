"""Deterministic journal-editor desk checks."""

import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.scholarly_depth import (
    evaluate_scholarly_depth,
    profile_for_paper,
    validate_profile_id,
    validate_scholarly_depth_review,
)


class ScholarlyDepthTests(unittest.TestCase):
    def manuscript(self, *, citation_count=12, figures=True):
        cites = " ".join(f"[[cite:ref{i % 20}]]" for i in range(citation_count))
        sections = [
            {"id": "introduction", "units": [{"id": "intro", "text": cites}]},
            {"id": "background", "units": [{"id": "background", "text": cites}]},
            {"id": "discussion", "units": [{"id": "discussion", "text": cites}]},
        ]
        if figures:
            sections[2]["units"].append({"id": "table", "kind": "table", "text": "Caption\nA | B\n1 | 2"})
        return {"sections": sections}

    def config(self, count=20):
        return {"scholarly_profile": "empirical_journal",
                "references": [{"key": f"ref{i}", "source_ref": f"source-{i}"} for i in range(count)]}

    def results(self, count=3):
        return {"assets": [{"id": f"fig{i}", "role": "figure"} for i in range(count)]}

    def survey(self, count=20):
        return {"sources": {f"source-{i}": {"representation": "full_text" if i < 5 else "abstract"}
                             for i in range(count)}}

    def test_empirical_journal_floor_accepts_sufficient_candidate(self):
        review = evaluate_scholarly_depth(self.manuscript(), self.config(), self.results(), self.survey())
        self.assertEqual(review["decision"], "accept")
        self.assertEqual(review["observed"]["references"], 20)

    def test_document_manifest_shape_is_counted_like_review_shape(self):
        review_shape = self.manuscript()
        manifest_shape = {"groups": [
            {"title": "Introduction", "units": review_shape["sections"][0]["units"]},
            {"title": "Background", "units": review_shape["sections"][1]["units"]},
            {"title": "Discussion", "units": review_shape["sections"][2]["units"]},
        ]}
        review = evaluate_scholarly_depth(manifest_shape, self.config(), self.results(), self.survey())
        self.assertEqual(review["observed"]["intext_citations"], 36)
        self.assertEqual(review["observed"]["tables"], 1)

    def test_empirical_journal_floor_rejects_thin_candidate(self):
        config = self.config(5)
        config["references"] = config["references"][:5]
        review = evaluate_scholarly_depth(self.manuscript(citation_count=3), config, self.results(2), self.survey(5))
        self.assertEqual(review["decision"], "revise")
        self.assertEqual({item["id"] for item in review["findings"]},
                         {"journal_editor_reference_count", "journal_editor_citation_density",
                          "journal_editor_figure_count"})

    def test_unknown_profile_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_profile_id("conference_note")

    def test_research_paper_without_profile_cannot_fall_back_to_report_floor(self):
        config = self.config()
        config.pop("scholarly_profile")
        config["document_type"] = "research_paper"
        self.assertEqual(profile_for_paper(config), "empirical_journal")
        config["scholarly_profile"] = "validation_report"
        with self.assertRaises(ValidationError):
            profile_for_paper(config)
        config["document_type"] = "validation_report"
        self.assertEqual(profile_for_paper(config), "validation_report")

    def test_depth_review_rejects_unmatched_failed_checks(self):
        review = evaluate_scholarly_depth(self.manuscript(), self.config(5), self.results(2), self.survey(5))
        review["findings"] = []
        with self.assertRaises(ValidationError):
            validate_scholarly_depth_review(review)
