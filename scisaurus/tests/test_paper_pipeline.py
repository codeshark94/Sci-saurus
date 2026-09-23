"""Pure contract tests for the AI-native manuscript pipeline boundary."""

import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.paper_pipeline import (
    bind_claim_citations,
    compress_reader_surface,
    normalise_manuscript_draft,
    _review_input,
    project_writer_packet,
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

    def test_writer_content_survives_loose_wrapper_and_shape_normalization(self):
        packet = {"writer_contract": {"section_order": [
            {"id": "introduction", "title": "Introduction", "unit_ids": ["intro_p1"]},
            {"id": "results", "title": "Results", "unit_ids": ["results_p1"]},
        ]}}
        loose = {"manuscript": {"title": "Loose paper", "sections": {
            "Introduction": "The accepted question is tested under the declared boundary.",
            "Results": {"kind": "markdown", "content": "The measured result changes across the sweep."},
        }}}
        draft, audit = normalise_manuscript_draft(loose, packet=packet)
        checked = validate_manuscript_draft(draft)
        self.assertEqual(checked["title"], "Loose paper")
        self.assertEqual([item["id"] for item in checked["sections"]], ["introduction", "results"])
        self.assertEqual(checked["sections"][1]["units"][0]["id"], "results_p1")
        self.assertEqual(checked["sections"][1]["units"][0]["kind"], "paragraph")
        self.assertIn("unwrapped_response_envelope", audit["format_repairs"])
        self.assertIn("unit_kind:results_p1", audit["format_repairs"])

    def test_writer_plain_text_headings_are_content_not_a_format_failure(self):
        packet = {"writer_contract": {"section_order": [
            {"id": "introduction", "title": "Introduction", "unit_ids": ["intro_p1"]},
            {"id": "results", "title": "Results", "unit_ids": ["results_p1"]},
        ]}}
        draft, audit = normalise_manuscript_draft(
            "# Introduction\nThe question is bounded.\n# Results\nThe result is observed.",
            packet=packet,
        )
        validate_manuscript_draft(draft)
        self.assertEqual(len(draft["sections"]), 2)
        self.assertNotIn("no_parseable_sections", audit["fallback_fields"])

    def test_writer_accepts_content_with_loose_shape_without_format_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = PaperPipelineRunner.__new__(PaperPipelineRunner)
            runner.packet = {"writer_contract": {"section_order": [
                {"id": "introduction", "title": "Introduction", "unit_ids": ["intro_p1"]},
                {"id": "results", "title": "Results", "unit_ids": ["results_p1"]},
            ]}}
            runner.paper_config = {"title": "Loose paper", "references": []}
            runner.model_config = {
                "base_url": "http://127.0.0.1:1/v1", "protocol": "openai_compatible",
                "model": "writer", "timeout_seconds": 2, "max_output_tokens": 8192,
                "context_window_tokens": 65536, "max_input_tokens": 56000,
            }
            runner.output = Path(directory)
            runner.imported_draft = None
            runner.draft_before_research_review = True
            runner.current_stage = "composition"
            runner.deadline = 10**12
            client = Mock()
            client.complete.return_value = ModelResult(
                text=json.dumps({"manuscript": {"sections": {
                    "Introduction": "The accepted question is tested under the declared boundary.",
                    "Results": "The measured result changes across the sweep.",
                }}}),
                model="writer", usage={"model_calls": 1}, elapsed_seconds=0.01,
                finish_reason="stop",
            )
            with patch.object(runner, "_client", return_value=client), \
                    patch.object(runner, "_write_run_metadata"):
                draft, _ = runner._writer()
            self.assertEqual(client.complete.call_count, 1)
            self.assertEqual([item["id"] for item in draft["sections"]], ["introduction", "results"])
            self.assertTrue((Path(directory) / "writer-content-normalization-1.json").is_file())

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

    def test_writer_projection_keeps_every_reference_without_replaying_full_abstracts(self):
        cards = [{"source_ref": f"source-{index}", "work_id": f"W{index}",
                  "title": f"Work {index}", "authors": "Author", "year": 2025,
                  "representation": "abstract", "abstract": "x" * 1800,
                  "reader_use": "background"} for index in range(50)]
        packet = {
            "reference_cards": cards,
            "research_argument_review": {
                "schema_version": "research-argument-review-1",
                "decision": "accept", "checks": [], "findings": [],
                "rationale": "accepted",
            },
            "research_program": {"unprojected": "preserved when invalid"},
        }
        original = json.loads(json.dumps(packet))
        projected = project_writer_packet(
            packet,
            {"references": [{"key": "ref-1", "source_ref": "source-1"}]},
            abstract_chars=240,
            include_argument_review=False,
        )
        self.assertEqual(len(projected["reference_cards"]), 50)
        self.assertEqual(len(projected["reference_cards"][0]["abstract"]), 240)
        self.assertNotIn("research_argument_review", projected)
        self.assertEqual(projected["reference_cards"][1]["citation_key"], "ref-1")
        self.assertEqual(packet, original)

    def test_writer_context_projection_is_below_route_limit_before_dispatch(self):
        runner = PaperPipelineRunner.__new__(PaperPipelineRunner)
        runner.packet = {
            "reference_cards": [{"source_ref": f"source-{index}",
                                 "work_id": f"W{index}",
                                 "title": f"Work {index}",
                                 "abstract": "x" * 1800}
                                for index in range(50)],
            "research_argument": {"research_question": "Does X change Y?"},
            "research_program": {"unprojected": "invalid program is still bounded by card projection"},
            "research_argument_review": {"findings": ["finding"] * 100},
        }
        runner.paper_config = {"references": []}
        runner.model_config = {
            "base_url": "http://127.0.0.1:1/v1", "protocol": "openai_compatible",
            "model": "writer", "timeout_seconds": 2, "max_output_tokens": 8192,
            "context_window_tokens": 65536, "max_input_tokens": 56000,
        }
        prompt, audit = runner._writer_context_projection(
            {"writer_output_contract": {"schema_version": "manuscript-draft-2"}},
            "s" * 3500,
        )
        self.assertLess(audit["selected"]["estimated_input_tokens"], 56000)
        self.assertIn("reference_cards", json.loads(prompt))

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

    def test_editor_acceptance_does_not_repeat_an_unchanged_accepted_panel(self):
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
            first_pass = runner._editor_decision(package, [package])
            self.assertEqual(first_pass["decision"], "accept")
            rejected = runner._editor_decision(package, [])
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

    def test_precomposition_redteam_hold_blocks_writer_and_persists_requests(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            results_path = root / "results.json"
            results_path.write_text("{}")
            runner = PaperPipelineRunner(
                packet={"scientific_interpretation": {}},
                model_config={},
                paper_config={"schema_version": "paper-release-score-3",
                              "document_type": "research_paper"},
                output_dir=root / "output",
                pipeline_deadline_seconds=30,
                research_redteam_deadline_seconds=5,
                research_redteam_max_attempts=1,
            )
            events = []
            runner.feedback_callback = events.append
            package = {
                "schema_version": "research-red-team-package-1",
                "decision": "research_expansion_required",
                "reviews": [],
                "research_requests": [{
                    "id": "redteam_methods_run_control", "kind": "additional_experiment",
                    "owner": "methods.validation", "objective": "Run a discriminating control.",
                    "why": "The mechanisms remain confounded.",
                    "success_condition": "The control changes the mechanism decision.",
                    "evidence_needed": "Raw output and independent recalculation.",
                }],
                "reviewer_ids": ["methods", "mechanisms", "journal_editor"],
                "rationale": "The current evidence is insufficient for composition.",
                "usage": {"model_calls": 3, "input_tokens": 30, "output_tokens": 60},
                "elapsed_seconds": 0.1,
                "status": "research_expansion_required",
            }

            class FakeRedTeamRunner:
                def __init__(self, *args, **kwargs):
                    self.kwargs = kwargs

                def run(self, packet, *, artifact_dir):
                    self.packet = packet
                    self.artifact_dir = artifact_dir
                    return package

            preflight = {"decision": "proceed", "profile_id": "empirical_journal",
                         "expansion_requests": []}
            quality = {"decision": "proceed", "expansion_requests": []}
            results = {"question": "Does X change Y?", "assets": []}
            with patch("scisaurus.runtime.paper_pipeline.validate_paper_config",
                       return_value={"results_package": str(results_path)}), \
                    patch("scisaurus.runtime.paper_pipeline.validate_results_package",
                          return_value=results), \
                    patch("scisaurus.runtime.paper_pipeline.load_paper_survey",
                          return_value={"state": "eligible_for_experiment"}), \
                    patch("scisaurus.runtime.paper_pipeline.evaluate_scholarly_preflight",
                          return_value=preflight), \
                    patch("scisaurus.runtime.paper_pipeline.validate_scholarly_preflight",
                          side_effect=lambda value: value), \
                    patch("scisaurus.runtime.paper_pipeline.evaluate_result_package_quality",
                          return_value=quality), \
                    patch("scisaurus.runtime.paper_pipeline.ResearchRedTeamRunner",
                          FakeRedTeamRunner), \
                    patch("scisaurus.runtime.paper_pipeline.validate_redteam_package"):
                result = runner._research_admission(
                    {"primary_argument": {"thesis": "X changes Y."}},
                    {"decision": "accept"},
                )

            self.assertEqual(result["status"], "research_expansion_required")
            self.assertIsNone(result["manuscript_project_dir"])
            self.assertEqual(result["research_expansion_requests"][0]["kind"], "additional_experiment")
            self.assertTrue(Path(result["research_redteam_input_path"]).is_file())
            self.assertTrue(Path(result["research_redteam_path"]).is_file())
            self.assertEqual(events[-1]["event_id"], "paper-research-red-team")
            self.assertEqual(events[-1]["research_request_ids"], ["redteam_methods_run_control"])
            self.assertFalse((root / "output" / "manuscript-project").exists())


if __name__ == "__main__":
    unittest.main()
