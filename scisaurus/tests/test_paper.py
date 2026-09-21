"""Evidence-bound manuscript assembly and real PDF compilation tests."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from scisaurus.core.documents import Documents
from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.source_spans import locate
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.paper import PaperReleaseBuilder, _finding_supported, _latex, resolve_compile_script


try:
    COMPILE_SCRIPT = resolve_compile_script()
except ValidationError:
    COMPILE_SCRIPT = None


class PaperReleaseTests(unittest.TestCase):
    def setUp(self):
        from scisaurus.tests.test_surveys import TestSurveyGate
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.survey_fixture = TestSurveyGate(methodName="test_independent_acceptance_and_assessment_are_durable")
        self.survey_fixture.setUp()
        self.survey_fixture.accept()
        self.assessment = self.survey_fixture.assessment(state="eligible_for_experiment")
        self.survey_fixture.commit(self.assessment)
        self.manuscript_dir = self.root / "manuscript"
        control = ControlStore(self.manuscript_dir)
        store = ArtifactStore(control); store.init_project(principal_note="paper")
        docs = Documents(control, store)
        score = store.publish_artifact(logical_id="command/scores/paper", artifact_type="note",
            author="principal", body=canonical_bytes({"id": "paper"}), media_type="application/json")
        verification = store.publish_artifact(logical_id="methods/verifications/paper", artifact_type="verification",
            author="methods.reviewer", body=canonical_bytes({"outcome": "passed"}), media_type="application/json")
        groups = []
        for group_id, title, units in [
            ("introduction", "Introduction", [("prior", "The treatment also works in condition B. [[cite:known]]")]),
            ("methods", "Methods", [("method", "Evaluate the frozen fixture.")]),
            ("results", "Results", [("metric", "The experiment produced 91.2% accuracy under the frozen test condition. Observed accuracy was 91.2 percent.")]),
            ("limitations", "Limitations", [("limits", "The fixture does not establish external validity.")]),
        ]:
            heading = docs.publish_unit(logical_id=f"strategy/units/{group_id}", kind="heading", text=title, author="principal")
            children = []
            for unit_id, text in units:
                unit = docs.publish_unit(logical_id=f"strategy/units/{unit_id}", kind="paragraph", text=text,
                                         author="strategy.writer")
                children.append({"ref": unit["artifact_ref"], "children": []})
            groups.append({"ref": heading["artifact_ref"], "children": children})
        manifest = docs.publish_manifest(document_id="strategy/documents/paper", tree={"units": groups}, author="strategy.integrator")
        store.adopt(manifest["artifact_id"], target_version=manifest["version"], expected_accepted_version=None,
                    actor="strategy.integrator")
        store.publish_artifact(logical_id="command/results/final", artifact_type="report", author="command.controller",
            body=canonical_bytes({"status": "accepted", "incumbent_ref": manifest["artifact_ref"],
                                  "score_ref": score["artifact_ref"], "candidates": [{
                                      "candidate_ref": manifest["artifact_ref"],
                                      "verification_ref": verification["artifact_ref"]}]}), media_type="application/json")
        control.close()
        self.results = self.root / "results.json"
        self.results.write_text(json.dumps({"schema_version": "results-package-1", "id": "experiment", "revision": 1,
            "procedures": [{"id": "frozen_test", "description": "Evaluate the frozen fixture.", "source": "fixture.py"}],
            "metrics": [{"id": "accuracy", "value": 91.2, "unit": "percent", "conditions": "frozen test condition",
                         "source": "fixture-output.json", "presentation": "91.2% accuracy"}],
            "findings": [{"id": "observed_accuracy", "statement": "Observed accuracy was 91.2 percent.",
                          "metric_ids": ["accuracy"]}],
            "limitations": ["The fixture does not establish external validity."], "assets": []}))

    def tearDown(self):
        self.survey_fixture.tearDown()
        self.temp.cleanup()

    def config(self):
        return {"schema_version": "paper-release-score-1", "paper_id": "flagship_demo",
                "title": "Evidence-Bound Research Release Demonstration", "revision": 1,
                "document_type": "research_paper", "manuscript_project_dir": str(self.manuscript_dir),
                "survey_project_dir": self.survey_fixture.directory.name,
                "survey_ref": self.survey_fixture.survey, "assessment_ref": self.assessment,
                "results_package": str(self.results),
                "evidence": [{"id": "prior_result", "kind": "literature", "locator": self.survey_fixture.source,
                              "quote": "The treatment also works in condition B."},
                             {"id": "measured_accuracy", "kind": "result", "locator": "accuracy",
                              "quote": "91.2% accuracy"}],
                "claims": [{"id": "prior_claim", "statement": "The treatment also works in condition B.",
                            "unit_ids": ["prior"], "evidence_ids": ["prior_result"]},
                           {"id": "accuracy_claim", "statement": "91.2% accuracy",
                            "unit_ids": ["metric"], "evidence_ids": ["measured_accuracy"]}],
                "references": [{"key": "known", "title": "Known Treatment", "authors": "Fixture Author",
                                "year": "2026", "doi": None, "url": "https://example.org/known",
                                "source_ref": self.survey_fixture.source}],
                "authors": ["Sci-saurus Validation Team"], "keywords": ["provenance", "controlled revision"]}

    def test_finding_binding_accepts_equivalent_scientific_notation(self):
        finding = {"statement": "The maximum absolute error was 1.110e-16."}
        self.assertTrue(_finding_supported(finding, "The maximum absolute error was 1.110 × 10⁻¹⁶."))
        threshold = {"statement": "The rule first crossed 1e-06 at n=14."}
        self.assertTrue(_finding_supported(threshold, "The rule first crossed 10⁻⁶ at n = 14."))

    def test_latex_projection_preserves_common_scientific_glyphs(self):
        rendered = _latex("f(x)=exp(−100(x−c)^2), 1.110 × 10⁻¹⁶, a≤b, 1/√a—b")
        for glyph in ("−", "×", "⁻", "≤", "√", "—"):
            self.assertNotIn(glyph, rendered)
        self.assertIn(r"$\times$", rendered)
        self.assertIn(r"\textsuperscript{-16}", rendered)
        self.assertIn(r"$\sqrt{a}$", rendered)

    @unittest.skipUnless(os.environ.get("SCISAURUS_RUN_LATEX_INTEGRATION") == "1" and COMPILE_SCRIPT is not None
                         and shutil.which("pdfinfo") and shutil.which("pdftoppm"),
                         "set SCISAURUS_RUN_LATEX_INTEGRATION=1 for the real render integration")
    def test_builds_real_pdf_source_claim_index_and_unapproved_release_candidate(self):
        output = self.root / "release"
        result = PaperReleaseBuilder(output, self.config()).build(compile_script=COMPILE_SCRIPT)
        pdf = output / "output" / "pdf" / "flagship_demo.pdf"
        self.assertTrue(pdf.is_file())
        self.assertEqual(result["pdf_sha256"], hashlib.sha256(pdf.read_bytes()).hexdigest())
        self.assertEqual(result["status"], "needs_principal_approval")
        self.assertEqual(result["external_submission"], "excluded")
        self.assertTrue((output / "output" / "source" / "main.tex").is_file())
        self.assertTrue((output / "output" / "source" / "references.bib").is_file())
        self.assertTrue(result["visual_check"]["all_pages_rendered"])
        self.assertTrue(result["event_chain"][0])

    @unittest.skipUnless(os.environ.get("SCISAURUS_RUN_LATEX_INTEGRATION") == "1" and COMPILE_SCRIPT is not None
                         and shutil.which("pdfinfo") and shutil.which("pdftoppm"),
                         "set SCISAURUS_RUN_LATEX_INTEGRATION=1 for the real preview integration")
    def test_preview_renders_exact_draft_without_releasing_it(self):
        output = self.root / "preview"
        builder = PaperReleaseBuilder(output, self.config())
        manuscript = builder._manuscript()
        draft = {"title": manuscript["title"], "sections": manuscript["groups"]}
        snapshot = builder.render_preview(draft, compile_script=COMPILE_SCRIPT)
        self.assertEqual(snapshot["status"], "unreviewed")
        self.assertEqual(snapshot["manuscript_sha256"], hashlib.sha256(canonical_bytes(draft)).hexdigest())
        self.assertTrue(Path(snapshot["pdf"]).is_file())
        self.assertTrue(all(Path(page).stat().st_size > 1000 for page in snapshot["pages"]))
        self.assertFalse((output / "output" / "release.json").exists())

    def test_rejects_claim_text_or_evidence_that_is_not_in_its_exact_unit(self):
        value = self.config()
        value["claims"][1]["statement"] = "Accuracy generalized to all domains."
        builder = PaperReleaseBuilder(self.root / "bad-release", value)
        try:
            with self.assertRaisesRegex(ValidationError, "absent"):
                builder._bind(builder._manuscript(), builder._survey(), json.loads(self.results.read_text()))
        finally:
            builder.close()

    def test_research_paper_rejects_noneligible_gap_state(self):
        # The immutable release score cannot substitute a gap report for a research paper.
        self.survey_fixture.control._conn.execute("DELETE FROM accepted_heads WHERE logical_id LIKE 'strategy/assessment-%'")
        builder = PaperReleaseBuilder(self.root / "invalid-release", self.config())
        try:
            with self.assertRaises(ValidationError):
                builder._survey()
        finally:
            builder.close()

    def test_v3_claim_index_requires_and_retains_stable_literature_span(self):
        value = self.config()
        builder = PaperReleaseBuilder(self.root / "span-release", value)
        try:
            manuscript, survey = builder._manuscript(), builder._survey()
            survey["schema_version"] = "literature-survey-3"
            source = survey["sources"][self.survey_fixture.source]
            builder.config["evidence"][0].update(locate(source, builder.config["evidence"][0]["quote"]))
            claim_index = builder._bind(manuscript, survey, json.loads(self.results.read_text()))
            self.assertEqual(claim_index["schema_version"], "paper-claim-index-2")
            builder.config["evidence"][0]["quote_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValidationError, "SHA-256"):
                builder._bind(manuscript, survey, json.loads(self.results.read_text()))
        finally:
            builder.close()

    def test_doi_reference_requires_verified_reconciled_identity(self):
        value = self.config()
        value["references"][0].update(doi="10.1234/known", identity_ref="artifact:kb/identities/W1@1")
        builder = PaperReleaseBuilder(self.root / "identity-release", value)
        try:
            manuscript, survey = builder._manuscript(), builder._survey()
            source = survey["sources"][self.survey_fixture.source]
            survey["identities"] = {value["references"][0]["identity_ref"]: {
                "schema_version": "bibliographic-identity-1", "work_id": source["work_id"],
                "doi": "10.1234/known", "status": "verified", "checks": [
                    {"field": "doi", "outcome": "match", "openalex": "10.1234/known", "crossref": "10.1234/known"},
                    {"field": "title", "outcome": "match", "openalex": "Known Treatment", "crossref": "Known Treatment"},
                    {"field": "year", "outcome": "match", "openalex": 2026, "crossref": 2026}],
                "observation_refs": [], "lookup_execution_ref": None}}
            builder._bind(manuscript, survey, json.loads(self.results.read_text()))
            survey["identities"][value["references"][0]["identity_ref"]]["status"] = "conflicted"
            with self.assertRaisesRegex(ValidationError, "verified survey bibliographic identity"):
                builder._bind(manuscript, survey, json.loads(self.results.read_text()))
        finally:
            builder.close()

    def test_generated_bibliography_uses_unstretched_reference_spacing(self):
        builder = PaperReleaseBuilder(self.root / "reference-layout", self.config())
        try:
            tex = builder._tex(builder._manuscript())
            self.assertIn("\\begin{thebibliography}{99}\n\\footnotesize\n\\raggedright\n"
                          "\\setlength{\\itemsep}{0.25em}\n\\bibitem", tex)
        finally:
            builder.close()

    def test_generated_result_figure_is_rendered_from_the_copied_asset_path(self):
        builder = PaperReleaseBuilder(self.root / "figure-layout", self.config())
        try:
            manuscript = builder._manuscript()
            results = {"assets": [{"id": "figure_one", "path": "figures/result.png", "role": "figure",
                                    "media_type": "image/png", "caption": "Observed result."}]}
            tex = builder._tex(manuscript, results)
            self.assertIn(r"\includegraphics[width=0.78\linewidth]{\detokenize{../assets/figures/result.png}}", tex)
            self.assertIn(r"\caption{Observed result.}", tex)
        finally:
            builder.close()

    def test_score_three_figure_is_rendered_at_its_argument_unit(self):
        builder = PaperReleaseBuilder(self.root / "inline-figure-layout", self.config())
        try:
            builder.config["schema_version"] = "paper-release-score-3"
            builder.config["figure_arguments"] = [{
                "asset_id": "figure_one", "why": "The display tests the central comparison.",
                "observation": "The observed result is visible in the comparison.", "unit_id": "metric",
            }]
            manuscript = {"title": "Inline figure", "groups": [
                {"title": "Results", "units": [
                    {"id": "metric", "kind": "paragraph", "text": "The observed result is visible in the comparison."},
                ]},
                {"title": "Discussion", "units": [{"id": "discussion", "kind": "paragraph", "text": "The figure supports the comparison."}]},
            ]}
            results = {"assets": [{"id": "figure_one", "path": "figures/result.png", "role": "figure",
                                    "media_type": "image/png", "caption": "Observed result."}]}
            tex = builder._tex(manuscript, results)
            figure_position = tex.index(r"\begin{figure}[H]")
            next_section = tex.index(r"\section{Discussion}")
            self.assertLess(figure_position, next_section)
            self.assertNotIn(r"\section{Figures}", tex)
        finally:
            builder.close()

    def test_table_renderer_preserves_pipe_table_footnotes(self):
        builder = PaperReleaseBuilder(self.root / "table-layout", self.config())
        try:
            manuscript = {"title": "Table", "groups": [{"title": "Results", "units": [{
                "kind": "table",
                "text": "A compact table.\nRule | Error\nMidpoint | 1e-6\n(a) Value is the first threshold crossing: |error| <= tolerance.",
            }]}]}
            tex = builder._tex(manuscript)
            self.assertIn(r"\parbox{0.95\linewidth}", tex)
            self.assertIn("Value is the first threshold crossing: |error| <= tolerance.", tex)
            self.assertIn(r"\ifdim\wd\scisaurustablebox>\linewidth", tex)
            self.assertIn(r"\else\usebox{\scisaurustablebox}\fi", tex)
        finally:
            builder.close()

    def test_storyline_score_binds_every_ordered_beat_to_claims_and_evidence(self):
        value = self.config()
        value["schema_version"] = "paper-release-score-2"
        value["storyline"] = {"id": "fixture_story", "revision": 1,
            "thesis": "The fixture demonstrates evidence-bound assembly.", "beats": [
                {"id": "prior_beat", "role": "motivation",
                 "proposition": "The treatment also works in condition B."},
                {"id": "metric_beat", "role": "result", "proposition": "91.2% accuracy"},
                {"id": "observation_beat", "role": "conclusion",
                 "proposition": "Observed accuracy was 91.2 percent."}]}
        for evidence in value["evidence"]:
            evidence["relation"] = "support"
        value["claims"][0]["storyline_id"] = "prior_beat"
        value["claims"][1]["storyline_id"] = "metric_beat"
        value["claims"].append({"id": "observation_claim", "statement": "Observed accuracy was 91.2 percent.",
            "unit_ids": ["metric"], "evidence_ids": ["measured_accuracy"],
            "storyline_id": "observation_beat"})
        builder = PaperReleaseBuilder(self.root / "storyline-release", value)
        try:
            index = builder._bind(builder._manuscript(), builder._survey(), json.loads(self.results.read_text()))
            self.assertEqual(index["schema_version"], "paper-claim-index-3")
            self.assertEqual(index["storyline"], value["storyline"])
        finally:
            builder.close()
        value["claims"][-1]["storyline_id"] = "metric_beat"
        with self.assertRaisesRegex(ValidationError, "every storyline beat"):
            PaperReleaseBuilder(self.root / "missing-storyline-beat", value)

    def test_storyline_score_rejects_reordered_manuscript_beats(self):
        value = self.config()
        value["schema_version"] = "paper-release-score-2"
        value["storyline"] = {"id": "reordered_story", "revision": 1, "thesis": "Order is frozen.", "beats": [
            {"id": "metric_first", "role": "result", "proposition": "91.2% accuracy"},
            {"id": "prior_second", "role": "motivation", "proposition": "The treatment also works in condition B."},
            {"id": "observation_third", "role": "conclusion", "proposition": "Observed accuracy was 91.2 percent."}]}
        for evidence in value["evidence"]:
            evidence["relation"] = "support"
        value["claims"][0]["storyline_id"] = "prior_second"
        value["claims"][1]["storyline_id"] = "metric_first"
        value["claims"].append({"id": "observation_claim", "statement": "Observed accuracy was 91.2 percent.",
            "unit_ids": ["metric"], "evidence_ids": ["measured_accuracy"],
            "storyline_id": "observation_third"})
        builder = PaperReleaseBuilder(self.root / "reordered-storyline", value)
        try:
            with self.assertRaisesRegex(ValidationError, "ordered storyline"):
                builder._bind(builder._manuscript(), builder._survey(), json.loads(self.results.read_text()))
        finally:
            builder.close()

    def test_storyline_score_binds_method_finding_and_limitation_to_results_package(self):
        value = self.config()
        value["schema_version"] = "paper-release-score-2"
        value["storyline"] = {"id": "result_story", "revision": 1, "thesis": "The fixture is bounded.", "beats": [
            {"id": "method_beat", "role": "method", "proposition": "Evaluate the frozen fixture."},
            {"id": "finding_beat", "role": "result", "proposition": "Observed accuracy was 91.2 percent."},
            {"id": "limit_beat", "role": "limitation", "proposition": "The fixture does not establish external validity."}]}
        value["evidence"] = [
            {"id": "frozen_procedure", "kind": "procedure", "locator": "frozen_test",
             "quote": "Evaluate the frozen fixture.", "relation": "support"},
            {"id": "observed_finding", "kind": "finding", "locator": "observed_accuracy",
             "quote": "Observed accuracy was 91.2 percent.", "relation": "support"},
            {"id": "external_limit", "kind": "limitation", "locator": "limitation/0",
             "quote": "The fixture does not establish external validity.", "relation": "qualify"}]
        value["claims"] = [
            {"id": "method_claim", "statement": "Evaluate the frozen fixture.", "unit_ids": ["method"],
             "evidence_ids": ["frozen_procedure"], "storyline_id": "method_beat"},
            {"id": "finding_claim", "statement": "Observed accuracy was 91.2 percent.", "unit_ids": ["metric"],
             "evidence_ids": ["observed_finding"], "storyline_id": "finding_beat"},
            {"id": "limit_claim", "statement": "The fixture does not establish external validity.", "unit_ids": ["limits"],
             "evidence_ids": ["external_limit"], "storyline_id": "limit_beat"}]
        builder = PaperReleaseBuilder(self.root / "result-evidence", value)
        try:
            builder._bind(builder._manuscript(), builder._survey(), json.loads(self.results.read_text()))
            builder.config["evidence"][0]["quote"] = "Evaluate a different fixture."
            with self.assertRaisesRegex(ValidationError, "exact procedure"):
                builder._bind(builder._manuscript(), builder._survey(), json.loads(self.results.read_text()))
        finally:
            builder.close()


if __name__ == "__main__":
    unittest.main()
