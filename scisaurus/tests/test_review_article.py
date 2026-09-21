"""Review-article contracts, independent critique, and retained-call accounting."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.composer import ComposerRunner, validate_workflow
from scisaurus.runtime.model_work import ModelWorkBlocked
from scisaurus.runtime.models import ModelCallError, ModelResult
from scisaurus.runtime.paper import ManuscriptRenderer
from scisaurus.runtime.review_article import (
    ReviewArticleRunner, validate_review_article_config, validate_evidence,
    validate_review_plan, validate_review_draft, prepare_review_workflow,
)


def corpus():
    discovery = {
        "themes": [{"id": f"theme-{i}", "question": "When do the two mechanisms disagree?", "why_now": "New contrasting results",
                    "reader": "Mechanism researchers", "possible_insight": "Regime-dependent reconciliation", "queries": ["mechanism review"]} for i in (1, 2)],
        "journals": [{"id": f"journal-{i}", "name": f"Journal {i}", "guidelines_url": f"https://example.org/j{i}/guidelines",
                      "scope_url": f"https://example.org/j{i}/scope"} for i in (1, 2)]}
    sources = []
    for i in (1, 2):
        sources.append({"id": f"policy-{i}", "kind": "policy", "journal_id": f"journal-{i}",
                        "url": f"https://example.org/j{i}/guidelines", "text": "Reviews are considered after a synopsis. We cover competing mechanisms."})
    for i in range(1, 5):
        sources.append({"id": f"work-{i}", "kind": "article", "purpose": "benchmark" if i < 3 else "primary",
            "url": f"https://example.org/article/{i}", "text": "Introduction. We compare mechanisms. Results differ across regimes. Conclusion. References.",
            "work": {"id": f"W{i}", "title": f"Mechanism study {i}", "year": 2025, "doi": None, "authors": ["Researcher"]}})
    for source in sources:
        source.update(text_sha256=hashlib.sha256(source["text"].encode()).hexdigest(), captured_at="2026-09-21T00:00:00+00:00")
    return {"schema_version": "review-evidence-1", "discovery": discovery, "sources": sources,
            "searches": [{"query": "mechanism review", "outcome": "ok"}], "gaps": [], "coverage": "Bounded critical review"}


def plan():
    return {"theme_id": "theme-1", "journal_id": "journal-1", "title": "Regime boundaries in competing mechanisms",
        "question": "When do mechanisms disagree?", "why_now": "Recent contrasting results", "reader": "Mechanism researchers",
        "thesis": "The apparent conflict is conditional on regime", "coverage_limits": "A bounded illustrative corpus, not a systematic search",
        "venues": [{"journal_id": f"journal-{i}", "entry_route": "proposal_required", "scope_fit": "Mechanism researchers",
                    "article_type": "Review", "format_limits": "Not established", "evidence": [{"source_id": f"policy-{i}", "quote": "Reviews are considered after a synopsis."}]} for i in (1, 2)],
        "benchmarks": [{"source_id": f"work-{i}", "scope": "Mechanisms", "story": "Comparison", "structure": "Introduction to comparison to conclusion",
                        "visual_strategy": "Not visible in this capture", "what_it_misses": "Regime reconciliation remains to be established",
                        "reuse_boundary": "Compare organization, never copy prose or figures", "evidence": [{"source_id": f"work-{i}", "quote": "We compare mechanisms."}]} for i in (1, 2)],
        "evidence_matrix": [{"id": f"row-{i}", "comparison_axis": "Regime", "finding": "Results differ across regimes",
                             "limitation": "Direction is not resolved", "evidence": {"source_id": f"work-{i}", "quote": "Results differ across regimes."}} for i in (3, 4)],
        "insights": [{"id": "boundary", "kind": "boundary_condition", "statement": "Regime boundaries may reconcile the conflict",
                      "derivation": "Both independent reports vary with regime", "added_value": "Organizes disagreements by regime rather than publication",
                      "counterevidence": "Agreement within regimes remains unknown", "uncertainty": "A proposed synthesis, not an established mechanism",
                      "falsification": "Persistent disagreement within matched regimes would refute the reconciliation",
                      "premises": [{"source_id": f"work-{i}", "quote": "Results differ across regimes."} for i in (3, 4)],
                      "compared_reviews": ["work-1", "work-2"]}],
        "outline": [{"id": "introduction", "title": "Question and scope", "purpose": "Explain the review need", "argument_step": "Pose the conflict", "insight_ids": []},
                    {"id": "synthesis", "title": "Comparative synthesis", "purpose": "Reconcile regimes", "argument_step": "Derive the proposed boundary", "insight_ids": ["boundary"]},
                    {"id": "limitations", "title": "Limitations and outlook", "purpose": "Bound inference", "argument_step": "Specify disconfirmation", "insight_ids": []}],
        "synopsis": "We propose a critical synthesis of regime-dependent disagreement, with a comparative table and testable reconciliation."}


def draft_packet():
    units = [
        {"id": "intro_p1", "kind": "paragraph", "text": "Existing accounts compare mechanisms. [[cite:work-1]] [[cite:work-2]]"},
        {"id": "synthesis_p1", "kind": "paragraph", "text": "We propose a regime-boundary explanation, not a newly observed effect. [[cite:work-3]] [[cite:work-4]]"},
        {"id": "comparison", "kind": "table", "text": "Comparison across reports\nSource | Regime finding\nFirst [[cite:work-3]] | Varies\nSecond [[cite:work-4]] | Varies"},
        {"id": "limitations_p1", "kind": "paragraph", "text": "The bounded search does not resolve within-regime disagreement. [[cite:work-3]] [[cite:work-4]]"},
    ]
    return {"draft": {"schema_version": "manuscript-draft-2", "title": plan()["title"], "citation": "source markers",
                      "sections": [{"id": s["id"], "title": s["title"], "units": selected}
                                   for s, selected in zip(plan()["outline"], ([units[0]], units[1:3], [units[3]]))]},
            "unit_sources": {u["id"]: ["work-1", "work-2"] if u["id"] == "intro_p1" else ["work-3", "work-4"] for u in units},
            "insight_units": {"boundary": ["synthesis_p1"]}}


class ReviewArticleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.model = {"protocol": "openai_compatible", "base_url": "http://127.0.0.1:1/v1", "model": "fake",
                      "max_output_tokens": 4096, "timeout_seconds": 10, "output_format": "json_object"}
        (self.root / "model.json").write_text(json.dumps(self.model))
        (self.root / "evidence.json").write_text(json.dumps(corpus()))
        self.config = {"schema_version": "review-article-config-1", "brief": "Find a useful mechanism review",
                       "article_type": "critical_review", "model_config_path": str(self.root / "model.json"),
                       "evidence_path": str(self.root / "evidence.json"), "output_dir": str(self.root / "output"),
                       "authors": ["Test Researcher"], "limits": {"wall_clock_seconds": 60, "model_calls": 24,
                       "input_tokens": 1000000, "output_tokens": 100000, "api_requests": 40,
                       "search_results": 8, "source_characters": 10000, "max_review_rounds": 2}}

    @staticmethod
    def response(**kwargs):
        assignment = json.loads(kwargs["prompt"])
        contract = assignment.get("output_contract", {})
        if "venues" in contract:
            value = plan()
        elif "draft" in contract:
            value = draft_packet()
        elif assignment.get("assignment") == "independent_manuscript_review":
            reviewer = assignment["reviewer"]
            checks = reviewer["focus"].split(" Required check IDs: ")[1].split(", ")
            value = {"schema_version": "manuscript-review-2", "reviewer_id": reviewer["id"], "stage": reviewer["stage"],
                     "decision": "accept", "checks": [{"id": k, "outcome": "passed", "evidence": "Grounded in supplied sources and pages"} for k in checks],
                     "findings": [], "research_requests": [], "protected_units": [], "rationale": "The bounded review satisfies the assigned criteria"}
        else:
            value = {"ok": True}
        return ModelResult(text=json.dumps(value), model="fake", usage={"input_tokens": 10, "output_tokens": 20}, elapsed_seconds=0.1, finish_reason="stop")

    def renderer(self, renderer, draft, **kwargs):
        renderer.dir.mkdir(parents=True, exist_ok=True)
        pdf, page = renderer.dir / "test.pdf", renderer.dir / "page.png"
        pdf.write_bytes(b"fake PDF for contract test")
        page.write_bytes(b"fake image for mocked model")
        return {"schema_version": "manuscript-review-render-1", "status": "unreviewed", "pdf": str(pdf), "pages": [str(page)],
                "manuscript_sha256": "fixture", "pdf_sha256": "fixture"}

    def test_plan_and_draft_are_source_bound(self):
        validate_evidence(corpus())
        validate_review_plan(plan(), corpus())
        validate_review_draft(draft_packet(), plan(), corpus())

    def test_systematic_review_cannot_be_claimed_without_a_protocol(self):
        self.config["article_type"] = "systematic_review"
        with self.assertRaisesRegex(ValidationError, "audited protocol"):
            validate_review_article_config(self.config)

    def test_invented_policy_quote_is_rejected(self):
        value = plan(); value["venues"][0]["evidence"][0]["quote"] = "All unsolicited manuscripts accepted."
        with self.assertRaisesRegex(ValidationError, "absent"):
            validate_review_plan(value, corpus())

    def test_unknown_selected_venue_is_rejected(self):
        value = plan(); value["venues"][0].update(entry_route="unknown", evidence=[])
        with self.assertRaisesRegex(ValidationError, "selected venue"):
            validate_review_plan(value, corpus())

    def test_synthesis_requires_distinct_primary_sources(self):
        value = plan(); value["insights"][0]["premises"][1]["source_id"] = "work-3"
        with self.assertRaisesRegex(ValidationError, "distinct primary"):
            validate_review_plan(value, corpus())

    def test_insight_must_be_compared_with_existing_reviews(self):
        value = plan(); value["insights"][0]["compared_reviews"] = ["work-4"]
        with self.assertRaisesRegex(ValidationError, "benchmark reviews"):
            validate_review_plan(value, corpus())

    def test_synthesis_insight_cannot_disappear_from_story(self):
        value = plan(); value["outline"][1]["insight_ids"] = []
        with self.assertRaisesRegex(ValidationError, "storyline"):
            validate_review_plan(value, corpus())

    def test_citation_and_capture_tampering_are_rejected(self):
        value = corpus(); value["sources"][0]["text"] = "Changed"
        with self.assertRaisesRegex(ValidationError, "hash mismatch"):
            validate_evidence(value)
        value = draft_packet(); value["unit_sources"]["intro_p1"] = ["work-3"]
        with self.assertRaisesRegex(ValidationError, "citation markers"):
            validate_review_draft(value, plan(), corpus())

    def test_fake_model_full_course_emits_render_and_independent_verdicts(self):
        with patch("scisaurus.runtime.review_article.ModelClient.complete", side_effect=self.response) as call, \
             patch("scisaurus.runtime.review_article.validate_render_environment", return_value=Path("adapter")), \
             patch.object(ManuscriptRenderer, "render_preview", autospec=True, side_effect=self.renderer):
            result = ReviewArticleRunner(self.config).run()
        self.assertEqual(result["status"], "candidate_needs_review", result)
        self.assertEqual(result["release_status"], "pending_principal_review")
        self.assertEqual(call.call_count, 5)
        self.assertEqual(result["usage"]["model_calls"], 5)
        self.assertTrue(Path(result["pdf"]).is_file())
        reviews = json.loads((self.root / "output/peer-review-1.json").read_text())["reviews"]
        self.assertEqual(len(reviews), 3)
        self.assertEqual({r["reviewer_id"] for r in reviews}, {"review_evidence", "review_contribution", "editorial_compression"})

    def test_successful_model_work_survives_resume(self):
        def invoke():
            runner = ReviewArticleRunner(self.config)
            try:
                return runner._call("probe", "editorial.writer", {"test": 1}, lambda v: v)
            finally:
                runner.close()
        with patch("scisaurus.runtime.review_article.ModelClient.complete", side_effect=self.response) as call:
            self.assertEqual(invoke(), {"ok": True})
            self.assertEqual(invoke(), {"ok": True})
        self.assertEqual(call.call_count, 1)

    def test_invalid_identical_work_does_not_reset_attempt_allowance(self):
        def invoke():
            runner = ReviewArticleRunner(self.config)
            try:
                runner._call("probe", "editorial.writer", {"test": 1}, lambda v: (_ for _ in ()).throw(ValidationError("bad schema")))
            finally:
                runner.close()
        with patch("scisaurus.runtime.review_article.ModelClient.complete", side_effect=self.response) as call:
            for _ in range(2):
                with self.assertRaises(ModelWorkBlocked):
                    invoke()
        self.assertEqual(call.call_count, 2)

    def test_call_cap_and_deadline_prevent_dispatch(self):
        self.config["limits"]["model_calls"] = 1
        runner = ReviewArticleRunner(self.config)
        self.addCleanup(runner.close)
        with patch("scisaurus.runtime.review_article.ModelClient.complete", side_effect=self.response) as call:
            runner._call("first", "editorial.writer", {"n": 1}, lambda v: v)
            with self.assertRaisesRegex(ModelWorkBlocked, "model_calls"):
                runner._call("second", "editorial.writer", {"n": 2}, lambda v: v)
            runner.deadline = 0
            with self.assertRaisesRegex(ModelWorkBlocked, "deadline"):
                runner._call("third", "editorial.writer", {"n": 3}, lambda v: v)
        self.assertEqual(call.call_count, 1)

    def test_provider_429_persists_cooldown_without_fanout(self):
        runner = ReviewArticleRunner(self.config)
        with patch("scisaurus.runtime.review_article.ModelClient.complete", side_effect=ModelCallError("quota", status_code=429, outcome_known=True, retry_after_seconds=300)) as call:
            with self.assertRaises(ModelCallError):
                runner._call("probe", "editorial.writer", {"n": 1}, lambda v: v)
        runner.close()
        resumed = ReviewArticleRunner(self.config)
        self.addCleanup(resumed.close)
        with patch("scisaurus.runtime.review_article.ModelClient.complete") as call:
            with self.assertRaisesRegex(ValidationError, "cooldown"):
                resumed._call("other", "review.science", {"n": 2}, lambda v: v)
            call.assert_not_called()

    def test_unknown_model_outcome_is_reconciled_and_not_redispatched(self):
        runner = ReviewArticleRunner(self.config)
        self.addCleanup(runner.close)
        with patch("scisaurus.runtime.review_article.ModelClient.complete", side_effect=ModelCallError("uncertain timeout", outcome_known=False)) as call:
            with self.assertRaisesRegex(ModelCallError, "uncertain timeout"):
                runner._call("probe", "editorial.writer", {"n": 1}, lambda v: v)
            with self.assertRaises(ModelWorkBlocked):
                runner._call("probe", "editorial.writer", {"n": 1}, lambda v: v)
            self.assertEqual(call.call_count, 1)
        self.assertEqual(runner.control._conn.execute("SELECT state FROM attempts").fetchone()[0], "result_unknown")
        self.assertEqual(runner.control._conn.execute("SELECT state FROM tasks").fetchone()[0], "blocked")
        self.assertEqual(runner.budget["model_calls"], 1)

    def test_nested_source_cooldown_and_transport_attempt_budget_are_preserved(self):
        from scisaurus.runtime.literature import ProviderCooldownError
        self.config["bibliography"] = {"max_retries": 3}
        self.config["limits"]["api_requests"] = 1
        runner = ReviewArticleRunner(self.config)
        self.addCleanup(runner.close)
        before = time.time()
        with patch("scisaurus.runtime.review_article.OpenAlexClient") as client:
            client.return_value.run.return_value = {"outcome": "rate_limited", "metadata": {"rate_limit": {"retry_after_seconds": 2}}}
            with self.assertRaises(ProviderCooldownError) as raised:
                runner._request("search", {"query": "mechanism", "limit": 4})
            self.assertEqual(raised.exception.retry_after_seconds, 2)
            self.assertEqual(client.call_args.kwargs["max_retries"], 0)
        self.assertLess(runner.budget["cooldown_epoch"] - before, 3)
        self.assertEqual(runner.budget["api_requests"], 1)
        runner.budget["cooldown_epoch"] = 0
        with patch("scisaurus.runtime.review_article.OpenAlexClient") as client:
            with self.assertRaisesRegex(ModelWorkBlocked, "retrieval allowance"):
                runner._request("search", {"query": "other", "limit": 4})
            client.assert_not_called()

    def test_acquisition_identity_does_not_refresh_synthesis_repair_budget(self):
        self.config.update(bibliography={}, fetch={"command": ["fixture"]})
        selection = {"works": [{"work_id": f"W{i}", "purpose": "benchmark" if i < 3 else "primary", "reason": "source comparison"} for i in range(1, 5)]}
        works = [{"id": f"W{i}", "title": f"Study {i}", "locations": [{"is_oa": True, "landing_page_url": f"https://example.org/{i}"}]} for i in range(1, 5)]
        def invalid(value):
            raise ValidationError("invalid synthesis")
        packets = []
        with patch("scisaurus.runtime.review_article.OpenAlexClient.run", return_value={"outcome": "ok", "works": works}) as search, \
             patch("scisaurus.runtime.review_article.MCPFetchClient.fetch", side_effect=lambda **kwargs: {"outcome": "ok", "metadata": {"representation": "extracted_text"}, "text": "Captured source text"}) as fetch, \
             patch("scisaurus.runtime.review_article.ModelClient.complete", side_effect=self.response) as model:
            for _ in range(2):
                runner = ReviewArticleRunner(self.config)
                try:
                    with patch.object(runner, "_call", return_value=selection):
                        packet = runner._collect(corpus()["discovery"])
                    packets.append(packet)
                    with self.assertRaises(ModelWorkBlocked):
                        runner._call("synthesis", "review.synthesizer", {"evidence": packet}, invalid)
                finally:
                    runner.close()
            self.assertEqual(search.call_count, 1)
            self.assertEqual(fetch.call_count, 8)
            self.assertEqual(model.call_count, 2)
        self.assertEqual(packets[0], packets[1])

    def test_continuation_receives_exact_findings_and_prior_draft_once(self):
        self.config["limits"]["max_review_rounds"] = 1
        prompts = []
        def rejected(**kwargs):
            prompts.append(json.loads(kwargs["prompt"]))
            result = self.response(**kwargs)
            value = json.loads(result.text)
            if value.get("reviewer_id") == "review_contribution":
                value.update(decision="revise", findings=[{
                    "id": "boundary-scope", "severity": "major", "location": "synthesis_p1",
                    "problem": "The boundary explanation omits the incompatible third regime.",
                    "surgical_fix": "Narrow the explanation to matched regimes.", "protected": ["citations"],
                    "verification": "Compare the qualification with the exact sources."}])
            return ModelResult(text=json.dumps(value), model=result.model, usage=result.usage,
                               elapsed_seconds=result.elapsed_seconds, finish_reason=result.finish_reason)
        with patch("scisaurus.runtime.review_article.ModelClient.complete", side_effect=rejected) as call, \
             patch("scisaurus.runtime.review_article.validate_render_environment", return_value=Path("adapter")), \
             patch.object(ManuscriptRenderer, "render_preview", autospec=True, side_effect=self.renderer):
            first = ReviewArticleRunner(self.config).run()
            self.assertEqual(first["status"], "review_rejected", first)
            self.config["work_orders"] = first["research_requests"]
            prompts.clear()
            second = ReviewArticleRunner(self.config).run()
            self.assertEqual(second["status"], "review_rejected", second)
            synthesis = next(p for p in prompts if "venues" in p.get("output_contract", {}))
            writer = next(p for p in prompts if "draft" in p.get("output_contract", {}))
            self.assertIn("incompatible third regime", json.dumps(synthesis["revision_context"]))
            self.assertIn("incompatible third regime", json.dumps(writer["reviews"]))
            self.assertEqual(writer["prior_draft"], draft_packet()["draft"])
            total = call.call_count
            ReviewArticleRunner(self.config).run()
            self.assertEqual(call.call_count, total)

    def test_prepare_imports_providers_without_model_dispatch(self):
        survey = {"model": self.model, "survey": {"bibliography": {"client": {}}, "full_text": {"client": {"command": ["fetch"]}}}}
        (self.root / "survey.json").write_text(json.dumps(survey))
        source = {"stages": [{"kind": "survey", "config_path": str(self.root / "survey.json")}]}
        (self.root / "source.json").write_text(json.dumps(source))
        with patch("scisaurus.runtime.review_article.ModelClient.complete") as call:
            result = prepare_review_workflow(self.root / "source.json", self.root / "prepared", "Critical review")
            call.assert_not_called()
        workflow = validate_workflow(json.loads(Path(result["workflow_path"]).read_text()))
        self.assertEqual([s["kind"] for s in workflow["stages"]], ["paper"])
        composer = ComposerRunner(workflow)
        self.addCleanup(composer.close)
        with patch.object(ReviewArticleRunner, "run", return_value={"status": "review_rejected", "output_path": str(self.root / "output/run.json")}) as run:
            composer._execute_stage(workflow["stages"][0])
        run.assert_called_once()

    def test_composer_specialists_and_adversary_receive_generated_review_evidence(self):
        from scisaurus.runtime.departments import default_organization
        from scisaurus.runtime.specialists import build_specialist_prompt, build_verifier_prompt
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        workflow = ComposerWorkflowTests()._workflow(self.root)
        config_path = self.root / "review.json"
        config_path.write_text(json.dumps(self.config))
        stage = {**workflow["stages"][0], "id": "review", "kind": "paper", "config_path": str(config_path)}
        workflow["stages"] = [stage]
        workflow["completion"]["required_stage_ids"] = ["review"]
        product = {"plan": plan(), "draft": draft_packet()["draft"], "sources": corpus()["sources"],
                   "unit_sources": draft_packet()["unit_sources"], "peer_reviews": [{"decision": "accept", "rationale": "source fidelity checked"}],
                   "coverage": "bounded critical review", "render": {"pdf": str(self.root / "review.pdf"), "pages": []}}
        context = {"status": "completed", "schema_version": "review-article-run-1", "output_path": str(config_path), "review_product": product}
        runner = ComposerRunner(workflow)
        self.addCleanup(runner.close)
        order = []
        def produce(*args, **kwargs):
            order.append("production")
            return deepcopy(context)
        def inspect(stage, assignment, descriptor, *, stage_result=None):
            order.append("specialists")
            self.assertEqual(stage_result["review_product"], product)
            packet = runner._specialist_stage_packet(stage, descriptor, stage_result=stage_result)
            department = next(d for d in default_organization()["departments"] if d["id"] == "editorial")
            for role in department["agent_roles"]:
                if role["id"] in {"chief", "adversary"}:
                    continue
                prompt = json.loads(build_specialist_prompt({**role, "role_id": role["id"]}, packet))
                self.assertTrue(prompt["projected_input"], role["id"])
                if set(role["input_projection"]) & {"draft", "manuscript", "manuscript_source"}:
                    self.assertIn(draft_packet()["draft"]["sections"][0]["units"][0]["text"], json.dumps(prompt["projected_input"]))
                    self.assertIn("Comparison across reports", json.dumps(prompt["projected_input"]))
            verifier = json.loads(build_verifier_prompt(stage, packet, [], stage_result))
            projected = verifier["chief_result"]["review_product"]
            self.assertIn("regime", projected["thesis"].lower())
            self.assertIn("Results differ across regimes.", json.dumps(projected["plan"]))
            self.assertTrue(projected["manuscript"]["sections"])
            self.assertTrue(projected["source_inventory"])
            self.assertTrue(projected["peer_reviews"])
            self.assertEqual(projected["render"]["pdf"], product["render"]["pdf"])
            return {"reports": [], "by_role": {}, "usage": {}, "model_enabled": False}
        with patch.object(runner, "_run_stage", side_effect=produce), \
             patch.object(runner, "_run_specialist_pool", side_effect=inspect), \
             patch.object(runner, "_run_specialist_verifier", return_value=None):
            result = runner.run()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(order, ["production", "specialists"])

    def test_over_quota_manuscript_keeps_unit_text_and_identity(self):
        from scisaurus.runtime.models import estimate_input_tokens
        from scisaurus.runtime.specialists import SPECIALIST_SYSTEM, build_specialist_prompt
        draft = deepcopy(draft_packet()["draft"])
        draft["sections"] = [{"id": "synthesis", "title": "Synthesis", "units": [
            {"id": f"unit_{i}", "kind": "paragraph", "text": "Specific mechanistic evidence: " + "bounded comparison " * 200}
            for i in range(34)]}]
        for field in ("draft", "manuscript", "manuscript_source"):
            assignment = {"role_id": "structural-editor", "input_projection": [field], "quota": {"max_input_tokens": 12000}}
            prompt = build_specialist_prompt(assignment, {"stage_result": {field: draft}})
            self.assertLessEqual(estimate_input_tokens(SPECIALIST_SYSTEM, prompt), 12000)
            projected = json.loads(prompt)["projected_input"][field]
            self.assertEqual(projected["unit_count"], 34)
            units = [u for u in projected["units"] if isinstance(u, dict)]
            self.assertTrue(units)
            for unit in units:
                self.assertTrue(unit["id"].startswith("unit_"))
                self.assertEqual(unit["section_id"], "synthesis")
                self.assertTrue(unit["text"].startswith("Specific mechanistic evidence:"))

    @unittest.skipUnless(os.environ.get("SCISAURUS_RUN_LATEX_INTEGRATION") == "1", "opt-in real PDF rendering")
    def test_review_manuscript_renders_without_experiment_results(self):
        refs = [{"key": s["id"], "title": s["work"]["title"], "authors": "Researcher", "year": "2025", "doi": None, "url": s["url"]}
                for s in corpus()["sources"] if s["kind"] == "article"]
        renderer = ManuscriptRenderer(self.root / "render", {"paper_id": "review", "authors": ["Test Researcher"], "keywords": ["critical synthesis"], "references": refs})
        rendered = renderer.render_preview(draft_packet()["draft"])
        self.assertTrue(Path(rendered["pdf"]).is_file())
        self.assertTrue(rendered["pages"])
