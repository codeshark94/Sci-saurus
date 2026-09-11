"""Three-role manuscript review is location-bound and cannot self-approve defects."""

import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.manuscript_review import (
    ManuscriptReviewRunner,
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
    return {"schema_version": "manuscript-review-1", "reviewer_id": reviewer, "stage": {"science": 1, "methods": 2, "ai_smell": 3}[reviewer],
            "decision": "revise" if finding else "accept",
            "checks": [{"id": "scope", "outcome": "passed", "evidence": "The supplied document was inspected."}],
            "findings": findings, "protected_units": ["Results paragraph 1"],
            "rationale": "The assigned checks were applied to the supplied manuscript."}


class FakeClient:
    calls = []

    def __init__(self, **config):
        self.config = config

    def complete(self, *, system, prompt, images=None):
        import json
        packet = json.loads(prompt)
        FakeClient.calls.append(packet["assignment"])
        if packet["assignment"] == "independent_manuscript_review":
            reviewer = packet["reviewer"]
            return ModelResult(text=json.dumps(review_for(reviewer["id"])), model="fake", usage={"model_calls": 1},
                               elapsed_seconds=0.01, finish_reason="stop")
        return ModelResult(text=json.dumps({"schema_version": "manuscript-review-synthesis-1", "decision": "accept",
                                            "required_repairs": [], "accepted_reviewers": ["science", "methods", "ai_smell"],
                                            "rationale": "All three independent reviews passed.",
                                            "verification_contract": ["Re-render the final manuscript."]}),
                           model="fake", usage={"model_calls": 1}, elapsed_seconds=0.01, finish_reason="stop")


class ManuscriptReviewTests(unittest.TestCase):
    def test_validator_rejects_accepted_review_with_major_finding(self):
        with self.assertRaisesRegex(ValidationError, "accepted manuscript review"):
            validate_review(review_for("science", finding=True) | {"decision": "accept"}, "science", 1)

    def test_synthesis_requires_repairs_for_major_findings(self):
        review = review_for("science", finding=True)
        bad = {"schema_version": "manuscript-review-synthesis-1", "decision": "revise", "required_repairs": [],
               "accepted_reviewers": [], "rationale": "A repair is needed.",
               "verification_contract": ["Re-render"]}
        with self.assertRaisesRegex(ValidationError, "omitted"):
            validate_synthesis(bad, [review])

    def test_runner_executes_three_roles_then_final_gate(self):
        FakeClient.calls = []
        manuscript = {"title": "Calibration", "units": [{"id": "results-1", "text": "Held-out log loss changed."}]}
        with patch("scisaurus.runtime.manuscript_review.ModelClient", FakeClient):
            result = ManuscriptReviewRunner({"base_url": "http://example.invalid", "model": "fake",
                                             "protocol": "openai_compatible", "timeout_seconds": 1,
                                             "max_output_tokens": 10}).run(manuscript)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual([item["reviewer_id"] for item in result["reviews"]], ["science", "methods", "ai_smell"])
        self.assertEqual(FakeClient.calls.count("independent_manuscript_review"), 3)


if __name__ == "__main__":
    unittest.main()
