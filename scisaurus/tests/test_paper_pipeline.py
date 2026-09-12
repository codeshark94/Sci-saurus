"""Pure contract tests for the AI-native manuscript pipeline boundary."""

import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.paper_pipeline import validate_argument_projection, validate_manuscript_draft
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


if __name__ == "__main__":
    unittest.main()
