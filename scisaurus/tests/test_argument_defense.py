"""Contracts for evidence-bound argumentation and weak-point handling."""

import copy
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.argument_defense import (
    build_argument_defense,
    validate_argument_defense,
)


def argument():
    return {
        "schema_version": "research-argument-1",
        "research_question": "Does the observed divergence reflect a stable mechanism or sampling variation?",
        "observed_patterns": [
            {"id": "divergence", "observation": "The two metrics move in opposite directions.",
             "implication": "The conclusion depends on metric geometry.", "evidence_ids": ["e1", "e2"]},
            {"id": "heterogeneity", "observation": "Split-level changes span both directions.",
             "implication": "The average does not describe every split.", "evidence_ids": ["e3"]},
        ],
        "hypotheses": [
            {"id": "metric_geometry", "statement": "Metric sensitivities produce the divergence.",
             "mechanism": "The metrics weight tail errors differently.", "status": "supported",
             "predictions": ["Tail errors contribute disproportionately."], "counterevidence": [],
             "discriminating_test": "Decompose the metric by probability stratum.",
             "evidence_ids": ["e1", "e2"], "explains_pattern_ids": ["divergence"]},
            {"id": "sampling_variation", "statement": "Fold composition drives the divergence.",
             "mechanism": "Small folds produce variable parameter estimates.", "status": "unresolved",
             "predictions": ["More samples narrow the spread."], "counterevidence": ["e3"],
             "discriminating_test": "Repeat the protocol with nested resampling.",
             "evidence_ids": [], "explains_pattern_ids": ["heterogeneity"]},
        ],
        "primary_argument": {
            "thesis": "The divergence is metric-sensitive and cannot yet be separated from sampling variation.",
            "primary_hypothesis_id": "metric_geometry",
            "rationale": "The opposing directions are observed, while the mechanisms require decomposition and resampling.",
            "scope_boundary": "The conclusion applies only to the supplied data and resampling design.",
        },
        "discriminating_experiments": [],
        "figure_plan": [],
        "limitations": ["The design covers one dataset and model family."],
    }


class ArgumentDefenseTests(unittest.TestCase):
    def setUp(self):
        self.packet = {"evidence_ids": ["e1", "e2", "e3"]}

    def test_separates_observation_inference_and_unresolved_test(self):
        ledger = build_argument_defense(argument(), self.packet)
        validate_argument_defense(ledger, evidence_ids=self.packet["evidence_ids"])
        observed = next(item for item in ledger["claim_postures"] if item["id"] == "observed-divergence")
        unresolved = next(item for item in ledger["claim_postures"] if item["id"] == "hypothesis-sampling_variation")
        self.assertEqual(observed["posture"], "observed")
        self.assertEqual(observed["evidence_ids"], ["e1", "e2"])
        self.assertEqual(unresolved["posture"], "provisional")
        self.assertEqual(unresolved["evidence_ids"], [])
        self.assertNotIn("results", unresolved["allowed_sections"])
        self.assertTrue(any(item["id"] == "unresolved-sampling_variation" for item in ledger["weak_points"]))

    def test_interpretation_cannot_be_allowed_in_results_and_evidence_must_exist(self):
        ledger = build_argument_defense(argument(), self.packet)
        broken = copy.deepcopy(ledger)
        broken["claim_postures"][-1]["allowed_sections"].append("results")
        with self.assertRaisesRegex(ValidationError, "cannot be allowed in Results"):
            validate_argument_defense(broken, evidence_ids=self.packet["evidence_ids"])

        broken = copy.deepcopy(ledger)
        broken["claim_postures"][0]["evidence_ids"] = ["not-an-evidence-id"]
        with self.assertRaisesRegex(ValidationError, "unknown evidence"):
            validate_argument_defense(broken, evidence_ids=self.packet["evidence_ids"])


if __name__ == "__main__":
    unittest.main()
