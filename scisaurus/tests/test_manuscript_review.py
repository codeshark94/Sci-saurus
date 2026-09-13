"""Role-separated manuscript review is location-bound and cannot self-approve defects."""

import unittest
import json
import time
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.manuscript_review import (
    ManuscriptReviewRunner,
    DEFAULT_REVIEWERS,
    _review_prompt,
    _namespace_review_findings,
    _normalise_review_candidate,
    _normalise_adjudication_candidate,
    _normalise_synthesis_candidate,
    apply_numeric_evidence_guard,
    audit_numeric_repair_support,
    validate_adjudication,
    validate_review,
    validate_synthesis,
)
from scisaurus.runtime.models import ModelResult


def review_for(reviewer, *, finding=False):
    findings = []
    if finding:
        findings.append({"id": f"{reviewer}-finding", "severity": "major", "location": "Results paragraph 2",
                         "problem": "The paragraph implies external validity beyond the tested dataset.",
                         "surgical_fix": "Qualify that sentence in the same paragraph.",
                         "protected": ["reported metric values"],
                         "verification": "Re-read the paragraph against the limitation list."})
    return {"schema_version": "manuscript-review-1", "reviewer_id": reviewer, "stage": {
            "science": 1, "methods": 2, "ai_smell": 3, "human_scientist": 4,
                "editorial_compression": 5, "journal_editor": 6}[reviewer],
            "decision": "revise" if finding else "accept",
            "checks": [{"id": "scope", "outcome": "passed", "evidence": "The supplied document was inspected."}],
            "findings": findings, "protected_units": ["Results paragraph 1"],
            "rationale": "The assigned checks were applied to the supplied manuscript."}


class FakeClient:
    calls = []
    configs = []

    def __init__(self, **config):
        self.config = config
        FakeClient.configs.append(config)

    def complete(self, *, system, prompt, images=None):
        import json
        packet = json.loads(prompt)
        FakeClient.calls.append(packet["assignment"])
        if packet["assignment"] == "independent_manuscript_review":
            reviewer = packet["reviewer"]
            return ModelResult(text=json.dumps(review_for(reviewer["id"])), model="fake", usage={"model_calls": 1},
                               elapsed_seconds=0.01, finish_reason="stop")
        if packet["assignment"] == "independent_review_arbitration":
            return ModelResult(text=json.dumps({"schema_version": "manuscript-review-arbitration-1",
                                                "decision": "resolved", "resolutions": [],
                                                "rationale": "No material findings were raised."}),
                               model="fake", usage={"model_calls": 1}, elapsed_seconds=0.01,
                               finish_reason="stop")
        return ModelResult(text=json.dumps({"schema_version": "manuscript-review-synthesis-1", "decision": "accept",
                                            "required_repairs": [], "accepted_reviewers": ["science", "methods", "ai_smell",
                                            "human_scientist", "editorial_compression", "journal_editor"],
                                           "rationale": "All independent reviews passed.",
                                           "verification_contract": ["Re-render the final manuscript."]}),
                           model="fake", usage={"model_calls": 1}, elapsed_seconds=0.01, finish_reason="stop")


class SlowClient(FakeClient):
    def complete(self, *, system, prompt, images=None):
        time.sleep(0.15)
        return super().complete(system=system, prompt=prompt, images=images)


class MissingStageClient(FakeClient):
    def complete(self, *, system, prompt, images=None):
        result = super().complete(system=system, prompt=prompt, images=images)
        payload = json.loads(result.text)
        if payload.get("schema_version") == "manuscript-review-1":
            payload.pop("stage", None)
        return ModelResult(text=json.dumps(payload), model=result.model, usage=result.usage,
                           elapsed_seconds=result.elapsed_seconds, finish_reason=result.finish_reason)


class ManuscriptReviewTests(unittest.TestCase):
    def test_review_accepts_provider_xhigh_reasoning_effort(self):
        runner = ManuscriptReviewRunner({"base_url": "http://example.invalid", "model": "fake",
                                         "protocol": "openai_compatible", "timeout_seconds": 1,
                                         "max_output_tokens": 10}, reasoning_effort="xhigh")
        self.assertEqual(runner.reasoning_effort, "xhigh")

    def test_namespaces_local_finding_ids_before_synthesis(self):
        first = review_for("science", finding=True)
        second = review_for("methods", finding=True)
        first["findings"][0]["id"] = "f1"
        second["findings"][0]["id"] = "f1"
        self.assertEqual(_namespace_review_findings(first)["findings"][0]["id"], "science_f1")
        self.assertEqual(_namespace_review_findings(second)["findings"][0]["id"], "methods_f1")

    def test_ai_smell_review_derives_failures_from_complete_surface(self):
        manuscript = {
            "title": "A candidate study",
            "sections": [
                {"id": "introduction", "title": "Introduction",
                 "units": [{"id": "introduction_p1", "text": "The question is explicit."}]},
                {"id": "discussion", "title": "Discussion",
                 "units": [{"id": "discussion_p1", "text": "The result has two possible explanations."}]},
            ],
        }
        reviewer = next(item for item in DEFAULT_REVIEWERS if item["id"] == "ai_smell")
        packet = json.loads(_review_prompt(manuscript, reviewer))
        self.assertEqual([section["id"] for section in packet["manuscript"]["sections"]],
                         ["introduction", "discussion"])
        focus = packet["reviewer"]["focus"].casefold()
        self.assertIn("open-ended", focus)
        self.assertIn("predefined symptom list", focus)
        self.assertNotIn("boilerplate transitions", focus)

    def test_validator_rejects_accepted_review_with_major_finding(self):
        with self.assertRaisesRegex(ValidationError, "accepted manuscript review"):
            validate_review(review_for("science", finding=True) | {"decision": "accept"}, "science", 1)

    def test_reviewer_research_request_is_a_first_class_blocker(self):
        review = review_for("methods")
        review["decision"] = "revise"
        review["research_requests"] = [{
            "id": "run_control_experiment", "kind": "additional_experiment", "owner": "methods.validation",
            "objective": "Run a control that separates the two live mechanisms.",
            "why": "The supplied observations do not distinguish the explanations.",
            "success_condition": "The control produces a validated comparison for both hypotheses.",
            "evidence_needed": "Raw outputs, deterministic checks, and an interpretable figure.",
        }]
        validate_review(review, "methods", 2)
        with self.assertRaisesRegex(ValidationError, "accepted manuscript review"):
            validate_review(review | {"decision": "accept"}, "methods", 2)

    def test_synthesis_cannot_drop_research_request(self):
        review = review_for("methods")
        review["decision"] = "revise"
        review["research_requests"] = [{
            "id": "methods_run_control", "kind": "additional_experiment", "owner": "methods.validation",
            "objective": "Run a control experiment.", "why": "The current data are non-discriminating.",
            "success_condition": "The control separates the hypotheses.", "evidence_needed": "Validated raw output.",
        }]
        synthesis = {
            "schema_version": "manuscript-review-synthesis-1", "decision": "revise", "required_repairs": [],
            "research_requests": [], "accepted_reviewers": [], "rationale": "New evidence is needed.",
            "verification_contract": ["Re-run the review after the control."],
        }
        with self.assertRaisesRegex(ValidationError, "research request"):
            validate_synthesis(synthesis, [review])
        synthesis["research_requests"] = list(review["research_requests"])
        validate_synthesis(synthesis, [review])

    def test_normalizer_binds_assignment_metadata_and_empty_protection_to_unit(self):
        candidate = review_for("science", finding=True)
        candidate.pop("schema_version")
        candidate.pop("reviewer_id")
        candidate.pop("stage")
        candidate["findings"][0]["protected"] = []
        candidate["findings"][0]["location"] = "results_p1"
        manuscript = {"sections": [{"id": "results", "units": [{"id": "results_p1"}]}]}
        normalized, changes = _normalise_review_candidate(
            candidate, {"id": "science", "stage": 1}, manuscript)
        validate_review(normalized, "science", 1)
        self.assertEqual(normalized["findings"][0]["protected"], ["results_p1"])
        self.assertGreaterEqual(len(changes), 4)

    def test_normalizer_removes_provider_echoed_review_metadata_without_touching_finding(self):
        candidate = review_for("science", finding=True)
        candidate.pop("schema_version")
        candidate.pop("reviewer_id")
        candidate.pop("stage")
        candidate["size_limit"] = "at most four highest-impact findings"
        finding = candidate["findings"][0]
        finding["protected_units"] = list(finding["protected"])
        manuscript = {"sections": [{"id": "results", "units": [{"id": "results_p1"}]}]}
        normalized, changes = _normalise_review_candidate(
            candidate, {"id": "science", "stage": 1}, manuscript)
        validate_review(normalized, "science", 1)
        self.assertNotIn("size_limit", normalized)
        self.assertNotIn("protected_units", normalized["findings"][0])
        self.assertIn("findings.protected_units", {change["field"] for change in changes})

    def test_normalizer_maps_provider_review_aliases_without_inventing_severity(self):
        candidate = review_for("science", finding=True)
        finding = candidate["findings"][0]
        finding["repair"] = finding.pop("surgical_fix")
        finding["protected_content"] = finding.pop("protected")
        finding["verification_check"] = finding.pop("verification")
        finding["unit_id"] = "results_p1"
        finding.pop("severity")
        for check in candidate["checks"]:
            check["name"] = check["id"]
            check.pop("id")
        normalized, changes = _normalise_review_candidate(
            candidate, {"id": "science", "stage": 1},
            {"sections": [{"id": "results", "units": [{"id": "results_p1"}]}]})
        self.assertNotIn("repair", normalized["findings"][0])
        self.assertEqual(normalized["findings"][0]["protected"], ["reported metric values"])
        self.assertTrue(any(change["field"] == "checks.id" for change in changes))
        with self.assertRaisesRegex(ValidationError, "invalid shape"):
            validate_review(normalized, "science", 1)

    def test_normalizer_maps_suggested_fix_to_surgical_fix(self):
        candidate = review_for("editorial_compression", finding=True)
        finding = candidate["findings"][0]
        finding["suggested_fix"] = finding.pop("surgical_fix")
        normalized, changes = _normalise_review_candidate(
            candidate, {"id": "editorial_compression", "stage": 5},
            {"sections": [{"id": "results", "units": [{"id": "results_p1"}]}]})
        validate_review(normalized, "editorial_compression", 5)
        self.assertNotIn("suggested_fix", normalized["findings"][0])
        self.assertIn("surgical_fix", normalized["findings"][0])
        self.assertTrue(any(change["field"] == "findings.surgical_fix" for change in changes))

    def test_normalizer_maps_science_provider_aliases_without_dropping_rationale(self):
        candidate = review_for("science", finding=True)
        finding = candidate["findings"][0]
        finding["fix"] = finding.pop("surgical_fix")
        finding["rationale"] = finding.pop("verification")
        candidate["checks"][0]["status"] = candidate["checks"][0].pop("outcome")
        normalized, changes = _normalise_review_candidate(
            candidate, {"id": "science", "stage": 1},
            {"sections": [{"id": "results", "units": [{"id": "results_p1"}]}]})
        validate_review(normalized, "science", 1)
        self.assertEqual(normalized["findings"][0]["surgical_fix"], "Qualify that sentence in the same paragraph.")
        self.assertEqual(normalized["findings"][0]["verification"], "Re-read the paragraph against the limitation list.")
        self.assertEqual(normalized["checks"][0]["outcome"], "passed")
        self.assertTrue(any(change["field"] == "findings.verification" for change in changes))

    def test_normalizer_removes_check_level_rationale_after_preserving_evidence(self):
        candidate = review_for("ai_smell", finding=False)
        check = candidate["checks"][0]
        check["name"] = check.pop("id")
        check["status"] = check.pop("outcome")
        check["rationale"] = "The supplied document was inspected independently."
        normalized, changes = _normalise_review_candidate(
            candidate, {"id": "ai_smell", "stage": 3},
            {"sections": [{"id": "results", "units": [{"id": "results_p1"}]}]})
        validate_review(normalized, "ai_smell", 3)
        self.assertNotIn("rationale", normalized["checks"][0])
        self.assertEqual(normalized["checks"][0]["evidence"], "The supplied document was inspected.")
        self.assertTrue(any(change["field"] == "checks.rationale" for change in changes))

    def test_normalizer_removes_check_description_alias(self):
        candidate = review_for("methods", finding=False)
        check = candidate["checks"][0]
        check["check"] = "Independent methods check."
        normalized, changes = _normalise_review_candidate(
            candidate, {"id": "methods", "stage": 2},
            {"sections": [{"id": "results", "units": [{"id": "results_p1"}]}]})
        validate_review(normalized, "methods", 2)
        self.assertNotIn("check", normalized["checks"][0])
        self.assertTrue(any(change["field"] == "checks.check" for change in changes))

    def test_normalizer_maps_methods_protection_check_and_boolean_passed_aliases(self):
        candidate = review_for("methods", finding=True)
        finding = candidate["findings"][0]
        finding["protection"] = finding.pop("protected")
        finding["repair"] = finding.pop("surgical_fix")
        finding["check"] = finding.pop("verification")
        check = candidate["checks"][0]
        check["passed"] = True
        check.pop("outcome")
        normalized, changes = _normalise_review_candidate(
            candidate, {"id": "methods", "stage": 2},
            {"sections": [{"id": "results", "units": [{"id": "results_p1"}]}]})
        validate_review(normalized, "methods", 2)
        self.assertEqual(normalized["findings"][0]["protected"], ["reported metric values"])
        self.assertEqual(normalized["checks"][0]["outcome"], "passed")
        self.assertTrue(any(change["field"] == "checks.outcome" for change in changes))

    def test_normalizer_binds_synthesis_scope_unit_list(self):
        candidate = {
            "schema_version": "manuscript-review-synthesis-1",
            "decision": "revise",
            "required_repairs": [{"finding_id": "science_f1", "owner": "science",
                                   "scope": ["results_p1", "results_table"],
                                   "verification": "Re-read the affected units."}],
            "accepted_reviewers": [], "rationale": "A scoped repair is required.",
            "verification_contract": ["Re-run independent review."],
        }
        normalized, changes = _normalise_synthesis_candidate(candidate)
        self.assertEqual(normalized["required_repairs"][0]["scope"], "results_p1; results_table")
        self.assertTrue(any(change["field"] == "required_repairs.scope" for change in changes))

    def test_normalizer_removes_provider_resolution_protection_metadata(self):
        candidate = {
            "schema_version": "manuscript-review-arbitration-1",
            "decision": "resolved",
            "resolutions": [{"id": "res_1", "finding_ids": ["science_f1"],
                              "decision": "retain", "rationale": "Keep the supported finding.",
                              "directive": "Retain the finding.", "protected": ["results_p1"]}],
            "rationale": "The finding was adjudicated.",
        }
        normalized, changes = _normalise_adjudication_candidate(candidate)
        self.assertNotIn("protected", normalized["resolutions"][0])
        self.assertTrue(any(change["field"] == "resolutions.protected" for change in changes))

    def test_adjudication_can_resolve_by_rejecting_a_competing_material_finding(self):
        review = review_for("science", finding=True)
        adjudication = {
            "schema_version": "manuscript-review-arbitration-1",
            "decision": "resolved",
            "resolutions": [{
                "id": "arbiter_science_finding",
                "finding_ids": [review["findings"][0]["id"]],
                "decision": "reject",
                "rationale": "The supplied evidence does not support this competing repair.",
                "directive": "Preserve the current text and retain the rationale in provenance.",
            }],
            "rationale": "The material critique received an explicit rejection with a recorded reason.",
        }
        self.assertEqual(validate_adjudication(adjudication, [review])["decision"], "resolved")

    def test_numeric_evidence_guard_rejects_an_ungrounded_correction(self):
        review = review_for("science", finding=True)
        finding = review["findings"][0]
        finding["protected"] = ["results_p1"]
        finding["location"] = "results_p1"
        finding["surgical_fix"] = "Replace 0.166 with 0.038 after recalculating the peak width."
        manuscript = {"sections": [{"id": "results", "units": [
            {"id": "results_p1", "text": "The reported width is 0.166."},
        ]}]}
        evidence = {"results_package": {"procedures": [
            {"id": "p", "description": "Use exp(-100(x-c)^2) over [0,1]."},
        ], "metrics": [], "findings": [], "limitations": []}}
        audit = audit_numeric_repair_support([review], manuscript, evidence)
        self.assertEqual(audit[finding["id"]]["unsupported_decimal_tokens"], ["0.038"])
        adjudication = {
            "schema_version": "manuscript-review-arbitration-1", "decision": "resolved",
            "resolutions": [{"id": "res_width", "finding_ids": [finding["id"]],
                             "decision": "retain", "rationale": "Keep the correction.",
                             "directive": "Replace the value."}],
            "rationale": "The finding was considered.",
        }
        guarded = apply_numeric_evidence_guard(adjudication, audit)
        self.assertEqual(guarded["resolutions"][0]["decision"], "reject")
        validate_adjudication(guarded, [review])

    def test_synthesis_requires_repairs_for_major_findings(self):
        review = review_for("science", finding=True)
        bad = {"schema_version": "manuscript-review-synthesis-1", "decision": "revise", "required_repairs": [],
               "accepted_reviewers": [], "rationale": "A repair is needed.",
               "verification_contract": ["Re-render"]}
        with self.assertRaisesRegex(ValidationError, "omitted"):
            validate_synthesis(bad, [review])

    def test_runner_executes_scientific_and_editorial_roles_then_final_gate(self):
        FakeClient.calls = []
        FakeClient.configs = []
        manuscript = {"title": "Calibration", "units": [{"id": "results-1", "text": "Held-out log loss changed."}]}
        feedback = []
        with patch("scisaurus.runtime.manuscript_review.ModelClient", FakeClient):
            result = ManuscriptReviewRunner({"base_url": "http://example.invalid", "model": "fake",
                                             "protocol": "openai_compatible", "timeout_seconds": 1,
                                             "max_output_tokens": 10}).run(
                                                 manuscript, feedback_callback=feedback.append)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["model_calls"], 7)
        self.assertEqual([item["reviewer_id"] for item in result["reviews"]], ["science", "methods", "ai_smell",
                                                                                       "human_scientist", "editorial_compression", "journal_editor"])
        self.assertEqual(FakeClient.calls.count("independent_manuscript_review"), 6)
        self.assertEqual([event["kind"] for event in feedback], ["review"] * 6 + ["synthesis"])
        self.assertEqual(feedback[-1]["status"], "accepted")
        self.assertTrue(FakeClient.configs)
        self.assertEqual({config["max_output_tokens"] for config in FakeClient.configs}, {10})
        self.assertEqual({config["reasoning_effort"] for config in FakeClient.configs}, {"xhigh"})

    def test_default_review_budget_inherits_the_model_configuration(self):
        runner = ManuscriptReviewRunner({"base_url": "http://example.invalid", "model": "fake",
                                         "protocol": "openai_compatible", "timeout_seconds": 1,
                                         "max_output_tokens": 32768})
        self.assertIsNone(runner.max_output_tokens)
        self.assertEqual(runner.reasoning_effort, "xhigh")

    def test_runner_binds_provider_review_that_omits_assignment_stage(self):
        manuscript = {"title": "Calibration", "units": [{"id": "results-1", "text": "Held-out log loss changed."}]}
        with patch("scisaurus.runtime.manuscript_review.ModelClient", MissingStageClient):
            result = ManuscriptReviewRunner({"base_url": "http://example.invalid", "model": "fake",
                                             "protocol": "openai_compatible", "timeout_seconds": 1,
                                             "max_output_tokens": 10}).run(manuscript)
        self.assertEqual(result["status"], "accepted")
        self.assertTrue(all("stage" in review for review in result["reviews"]))

    def test_composer_mode_adjudicates_material_review_feedback_before_synthesis(self):
        manuscript = {"title": "Calibration", "units": [{"id": "results-1", "text": "Held-out log loss changed."}]}
        feedback = []
        with patch("scisaurus.runtime.manuscript_review.ModelClient", FakeClient):
            result = ManuscriptReviewRunner({"base_url": "http://example.invalid", "model": "fake",
                                             "protocol": "openai_compatible", "timeout_seconds": 1,
                                             "max_output_tokens": 10}, arbiter_enabled=True).run(
                                                 manuscript, feedback_callback=feedback.append)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["adjudication"]["decision"], "resolved")
        self.assertEqual(result["model_calls"], 8)
        self.assertEqual(feedback[-2]["kind"], "arbitration")
        self.assertEqual(feedback[-1]["kind"], "synthesis")

    def test_runner_stops_a_slow_review_batch_at_the_deadline(self):
        manuscript = {"title": "Calibration", "units": [{"id": "results-1", "text": "Held-out log loss changed."}]}
        with patch("scisaurus.runtime.manuscript_review.ModelClient", SlowClient):
            with self.assertRaisesRegex(ValidationError, "deadline exceeded"):
                ManuscriptReviewRunner({"base_url": "http://example.invalid", "model": "fake",
                                        "protocol": "openai_compatible", "timeout_seconds": 1,
                                        "max_output_tokens": 10}, deadline_seconds=0.03).run(manuscript)

    def test_scientific_roles_receive_metric_section_for_cross_reference_checks(self):
        manuscript = {"title": "Calibration", "sections": [
            {"id": "metrics", "title": "Evaluation metrics",
             "units": [{"id": "met_p1", "text": "Metric definitions."}]},
            {"id": "results", "title": "Results",
             "units": [{"id": "res_p1", "text": "Results."}]},
        ]}
        for reviewer_id, stage in (("science", 1), ("human_scientist", 4)):
            packet = json.loads(_review_prompt(
                manuscript, {"id": reviewer_id, "stage": stage, "focus": "inspect"}))
            self.assertIn("metrics", {section["id"] for section in packet["manuscript"]["sections"]})


if __name__ == "__main__":
    unittest.main()
