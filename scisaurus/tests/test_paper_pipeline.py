"""Pure contract tests for the AI-native manuscript pipeline boundary."""

import unittest
from pathlib import Path
import tempfile

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.paper_pipeline import (
    bind_claim_citations,
    compress_reader_surface,
    _review_input,
    validate_argument_projection,
    validate_manuscript_draft,
    draft_depth_report,
    validate_draft_depth,
    PaperPipelineRunner,
)
from scisaurus.tests.test_research_argument import argument


class ManuscriptDraftContractTests(unittest.TestCase):
    def draft(self):
        return {"schema_version": "manuscript-draft-2", "title": "A paper", "citation": "markers",
                "sections": [{"id": "intro", "title": "Introduction",
                              "units": [{"id": "intro_p1", "kind": "paragraph", "text": "A sentence."}]}]}

    def test_accepts_structured_draft(self):
        self.assertEqual(validate_manuscript_draft(self.draft())["title"], "A paper")

    def test_rejects_duplicate_unit_identity(self):
        value = self.draft()
        value["sections"].append({"id": "methods", "title": "Methods",
                                   "units": [{"id": "intro_p1", "kind": "paragraph", "text": "Other."}]})
        with self.assertRaisesRegex(ValidationError, "unique"):
            validate_manuscript_draft(value)

    def test_rejects_unknown_unit_kind(self):
        value = self.draft(); value["sections"][0]["units"][0]["kind"] = "markdown"
        with self.assertRaisesRegex(ValidationError, "kind"):
            validate_manuscript_draft(value)

    def test_writer_must_project_question_thesis_patterns_and_discussion(self):
        value = {"schema_version": "manuscript-draft-2", "title": "A paper", "citation": "markers",
                 "sections": [
                     {"id": "introduction", "title": "Introduction", "units": [{"id": "intro_p1", "kind": "paragraph",
                       "text": argument()["research_question"]}]},
                     {"id": "results", "title": "Results", "units": [{"id": "results_p1", "kind": "paragraph",
                       "text": "Two calibration summaries improved while the proper score moved in the opposite direction. The reported conclusion depends on which metric is used. The split-level changes span both directions around their means. The average pattern may not describe every split."}]},
                     {"id": "discussion", "title": "Discussion", "units": [{"id": "discussion_p1", "kind": "paragraph",
                       "text": argument()["primary_argument"]["thesis"]}]},
                 ]}
        validate_argument_projection(validate_manuscript_draft(value), argument())

    def test_writer_cannot_drop_primary_argument(self):
        value = self.draft()
        value["sections"][0]["units"][0]["text"] = argument()["research_question"]
        value["sections"].append({"id": "results", "title": "Results",
                                   "units": [{"id": "results_p1", "kind": "paragraph",
                                              "text": "Two calibration summaries improved while the proper score moved in the opposite direction. The reported conclusion depends on which metric is used. The split-level changes span both directions around their means. The average pattern may not describe every split."}]})
        value["sections"].append({"id": "discussion", "title": "Discussion",
                                   "units": [{"id": "discussion_p1", "kind": "paragraph", "text": "The findings are reported."}]})
        with self.assertRaisesRegex(ValidationError, "primary argument"):
            validate_argument_projection(validate_manuscript_draft(value), argument())

    def test_surface_compression_removes_internal_ledger_duplicates(self):
        procedure = "Evaluate midpoint rules over the fixed grid and compare each estimate with the analytic integral."
        metric = "midpoint first crossed 1e-06 absolute error at n=14"
        draft = {"schema_version": "manuscript-draft-2", "title": "A paper", "citation": "markers",
                 "sections": [
                     {"id": "methods", "title": "Methods", "units": [{
                         "id": "methods_p1", "kind": "paragraph",
                         "text": "We evaluated the rule on the fixed grid. " + procedure}]},
                     {"id": "results", "title": "Results", "units": [{
                         "id": "results_p1", "kind": "paragraph",
                         "text": "The midpoint reached the threshold at n=14. " + metric + ". " + metric + "."}]},
                 ]}
        packet = {"results_package": {
            "procedures": [{"description": procedure}],
            "metrics": [{"presentation": metric}],
            "findings": [], "limitations": [],
        }}
        config = {"storyline": {"beats": []}, "claims": [], "figure_arguments": []}
        compressed, replacements, audit = compress_reader_surface(draft, packet, config)
        methods = compressed["sections"][0]["units"][0]["text"]
        results = compressed["sections"][1]["units"][0]["text"]
        self.assertNotIn(procedure, methods)
        self.assertEqual(results.casefold().count(metric.casefold()), 0)
        self.assertIn("reached the threshold at n=14", results)
        self.assertEqual(sorted(replacements), ["methods_p1", "results_p1"])
        self.assertGreaterEqual(len(audit["removed"]), 2)

    def test_review_projection_hides_internal_citation_tokens(self):
        draft = {"schema_version": "manuscript-draft-2", "title": "A paper", "citation": "markers",
                 "sections": [{"id": "introduction", "title": "Introduction", "units": [
                     {"id": "intro_p1", "kind": "paragraph", "text": "Prior work [[cite:alpha]]."}]}]}
        projected = _review_input(draft, references=[{"key": "alpha"}])
        self.assertEqual(projected["sections"][0]["units"][0]["text"], "Prior work [1].")

    def test_claim_citation_binding_projects_literature_to_claim_unit(self):
        draft = {"schema_version": "manuscript-draft-2", "title": "A paper", "citation": "markers",
                 "sections": [{"id": "introduction", "title": "Introduction", "units": [
                     {"id": "intro_p1", "kind": "paragraph", "text": "Prior work frames the question. [1]"},
                     {"id": "intro_p2", "kind": "paragraph", "text": "The study asks a bounded question."},
                 ]}]}
        config = {
            "references": [{"key": "talvila2012", "source_ref": "artifact:kb/abstracts/W123@1"}],
            "evidence": [{"id": "lit-1", "kind": "literature", "locator": "artifact:kb/abstracts/W123@1"}],
            "claims": [{"id": "claim-question", "unit_ids": ["intro_p2"], "evidence_ids": ["lit-1"]}],
        }
        bound, replacements, audit = bind_claim_citations(draft, config)
        self.assertNotIn("[1]", bound["sections"][0]["units"][0]["text"])
        self.assertTrue(bound["sections"][0]["units"][1]["text"].endswith("[[cite:talvila2012]]"))
        self.assertEqual(sorted(replacements), ["intro_p1", "intro_p2"])
        self.assertEqual(audit["added"][0]["reference_key"], "talvila2012")

    def test_editor_decision_requires_three_rounds_and_full_panel(self):
        runner = PaperPipelineRunner.__new__(PaperPipelineRunner)
        runner.empirical_profile = True
        runner.review_panel_ids = ["science", "methods", "ai_smell", "human_scientist", "editorial_compression", "journal_editor"]
        runner.reviewers = None
        runner._emit_feedback = lambda event: None
        with tempfile.TemporaryDirectory() as path:
            runner.output = Path(path)
            package = {
                "reviews": [{"reviewer_id": reviewer, "decision": "accept"}
                            for reviewer in runner.review_panel_ids],
                "synthesis": {"decision": "accept", "research_requests": []},
                "research_requests": [],
            }
            decision = runner._editor_decision(package, [package, package, package])
            self.assertEqual(decision["decision"], "accept")
            self.assertTrue((Path(path) / "editor-decision.json").is_file())
            rejected = runner._editor_decision(package, [package, package])
            self.assertEqual(rejected["decision"], "reject")

    def test_draft_depth_uses_the_paper_descriptor_as_canonical_contract(self):
        draft = {"schema_version": "manuscript-draft-2", "title": "A paper", "citation": "markers",
                 "sections": [{"id": "intro", "title": "Introduction",
                               "units": [{"id": "intro_p1", "kind": "paragraph",
                                           "text": "A short sentence."}]}]}
        config = {"schema_version": "paper-release-score-3", "depth_profile": {
            "min_words": 10, "min_references": 1, "min_full_text_references": 1,
            "min_sections": 2, "required_section_titles": ["Introduction", "Discussion"],
            "max_numeric_repetitions": 3, "max_caveat_repetitions": 3}}
        report = draft_depth_report(draft, config)
        self.assertEqual(report["word_count"], 3)
        self.assertEqual(report["missing_section_titles"], ["Discussion"])
        with self.assertRaisesRegex(ValidationError, "declared depth"):
            validate_draft_depth(draft, config)

    def test_draft_depth_accepts_complete_declared_structure(self):
        draft = {"schema_version": "manuscript-draft-2", "title": "A paper", "citation": "markers",
                 "sections": [{"id": "intro", "title": "Introduction",
                               "units": [{"id": "intro_p1", "kind": "paragraph",
                                           "text": "A sufficiently long scientific sentence with evidence."}]},
                              {"id": "discussion", "title": "Discussion",
                               "units": [{"id": "discussion_p1", "kind": "paragraph",
                                           "text": "A second sufficiently long scientific sentence."}]}]}
        config = {"schema_version": "paper-release-score-3", "depth_profile": {
            "min_words": 10, "min_references": 1, "min_full_text_references": 1,
            "min_sections": 2, "required_section_titles": ["Introduction", "Discussion"],
            "max_numeric_repetitions": 3, "max_caveat_repetitions": 3}}
        self.assertTrue(validate_draft_depth(draft, config)["applicable"])


if __name__ == "__main__":
    unittest.main()
