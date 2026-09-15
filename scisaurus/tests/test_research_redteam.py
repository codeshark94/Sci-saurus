import json
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.research_redteam import (
    ResearchRedTeamRunner,
    research_redteam_packet,
    validate_redteam_review,
)


MODEL = {
    "protocol": "openai_compatible",
    "base_url": "https://example.invalid/v1",
    "model": "stub",
    "timeout_seconds": 10,
    "max_output_tokens": 512,
}


def _review(reviewer_id, *, expand=False):
    checks = [{"id": check_id, "outcome": "passed", "evidence": f"checked {check_id}"}
              for check_id in sorted({
                  "question", "evidence", "result_coverage", "controls",
                  "alternatives", "reproducibility", "argument",
              })]
    requests = []
    findings = []
    decision = "accept"
    if expand:
        decision = "expand"
        checks[0]["outcome"] = "failed"
        findings = [{
            "id": "missing_control",
            "severity": "major",
            "problem": "The two mechanisms remain observationally confounded.",
            "required_action": "Run a control that changes only the measurement coupling.",
            "verification": "The new control must alter the mechanism decision with uncertainty reported.",
        }]
        requests = [{
            "id": "run_control",
            "kind": "additional_experiment",
            "owner": "methods.validation",
            "objective": "Run a control that separates the mechanisms.",
            "why": "The current result is compatible with both explanations.",
            "success_condition": "The control produces a validated result that changes the mechanism decision.",
            "evidence_needed": "Raw observations, uncertainty, and an independent recalculation.",
        }]
    return {
        "schema_version": "research-red-team-1",
        "reviewer_id": reviewer_id,
        "decision": decision,
        "checks": checks,
        "findings": findings,
        "research_requests": requests,
        "rationale": "The supplied checks were inspected against the pinned research packet.",
    }


class _FakeModelClient:
    decisions = {}

    def __init__(self, **config):
        self.config = config

    def complete(self, *, system, prompt, images=None):
        assignment = json.loads(prompt)
        reviewer_id = assignment["reviewer"]["id"]
        value = _review(reviewer_id, expand=self.decisions.get(reviewer_id, False))
        return ModelResult(
            text=json.dumps(value), model=self.config["model"],
            usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
            elapsed_seconds=0.01, finish_reason="stop",
        )


class ResearchRedTeamTests(unittest.TestCase):
    def test_packet_projects_scientific_inputs(self):
        packet = research_redteam_packet(
            results={"schema_version": "results-package-2", "question": "Does X change Y?",
                     "findings": [], "assets": []},
            interpretation={"patterns": []},
            argument={"primary_argument": {"thesis": "X changes Y."}},
            paper_evidence=[{"claim_id": "c1", "support": "result"}],
            paper_claims=[{"id": "c1"}],
            references=[{"key": "r1", "title": "Prior work", "year": 2025, "source_ref": "s1"}],
        )
        self.assertEqual(packet["research_question"], "Does X change Y?")
        self.assertEqual(packet["references"][0]["key"], "r1")
        self.assertEqual(packet["paper_claims"], [{"id": "c1"}])

    def test_three_independent_reviews_can_admit_composition(self):
        _FakeModelClient.decisions = {}
        with patch("scisaurus.runtime.research_redteam.ModelClient", _FakeModelClient):
            package = ResearchRedTeamRunner(MODEL, deadline_seconds=10, max_attempts=1).run(
                {"research_question": "Does X change Y?", "results_package": {},
                 "scientific_interpretation": {}, "research_argument": {},
                 "paper_evidence": [], "paper_claims": [], "references": []})
        self.assertEqual(package["status"], "accept")
        self.assertEqual(package["decision"], "accept")
        self.assertEqual(package["reviewer_ids"], ["methods", "mechanisms", "journal_editor"])
        self.assertEqual(package["usage"]["model_calls"], 3)

    def test_one_blocking_review_creates_namespaced_work_order(self):
        _FakeModelClient.decisions = {"mechanisms": True}
        with patch("scisaurus.runtime.research_redteam.ModelClient", _FakeModelClient):
            package = ResearchRedTeamRunner(MODEL, deadline_seconds=10, max_attempts=1).run(
                {"research_question": "Does X change Y?", "results_package": {},
                 "scientific_interpretation": {}, "research_argument": {},
                 "paper_evidence": [], "paper_claims": [], "references": []})
        self.assertEqual(package["status"], "research_expansion_required")
        self.assertEqual(len(package["research_requests"]), 1)
        self.assertTrue(package["research_requests"][0]["id"].startswith("redteam_mechanisms_"))
        self.assertEqual(package["research_requests"][0]["kind"], "additional_experiment")

    def test_accepting_review_cannot_hide_a_failed_check(self):
        value = _review("methods")
        value["checks"][0]["outcome"] = "failed"
        with self.assertRaisesRegex(ValidationError, "accepted red-team review"):
            validate_redteam_review(value, "methods")
