"""Contracts for the pre-composition scientific argument stage."""

import json
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.research_argument import (
    ResearchArgumentRunner,
    _normalise_argument_candidate,
    validate_argument_review,
    validate_research_argument,
)


def argument():
    return {
        "schema_version": "research-argument-1",
        "research_question": "Does the observed metric divergence reflect a stable mechanism or sampling variation?",
        "observed_patterns": [
            {"id": "metric_divergence", "observation": "Two calibration summaries improved while the proper score moved in the opposite direction.",
             "implication": "The reported conclusion depends on which metric is used.", "evidence_ids": ["e1", "e2"]},
            {"id": "split_heterogeneity", "observation": "The split-level changes span both directions around their means.",
             "implication": "The average pattern may not describe every split.", "evidence_ids": ["e3"]},
        ],
        "hypotheses": [
            {"id": "metric_geometry", "statement": "Different metric sensitivities produce the opposing mean directions.",
             "mechanism": "The metrics weight probability magnitude and tail errors differently.", "status": "supported",
             "predictions": ["Tail observations contribute disproportionately to the proper-score change."],
             "counterevidence": [], "discriminating_test": "Decompose each metric by observation and probability stratum.",
             "evidence_ids": ["e1", "e2"], "explains_pattern_ids": ["metric_divergence"]},
            {"id": "sampling_variation", "statement": "Calibration-fold composition drives most of the observed divergence.",
             "mechanism": "Small calibration folds produce variable parameter estimates across splits.", "status": "unresolved",
             "predictions": ["Increasing the number of splits and sample size will reduce the spread."],
             "counterevidence": ["e3"], "discriminating_test": "Repeat the protocol with larger samples and nested resampling.",
             "evidence_ids": [], "explains_pattern_ids": ["split_heterogeneity"]},
        ],
        "primary_argument": {
            "thesis": "The observed divergence is a metric-sensitive pattern whose stability cannot be separated from sampling variation by the current design.",
            "primary_hypothesis_id": "metric_geometry",
            "rationale": "The opposing metric directions are directly observed, while the competing mechanisms remain distinguishable only through targeted decomposition and resampling.",
            "scope_boundary": "The conclusion applies to the supplied dataset, model, metrics, and resampling design.",
        },
        "discriminating_experiments": [
            {"id": "per_observation", "question": "Do tail observations account for the metric divergence?",
             "design": "Compute per-observation changes and stratify them by probability and correctness.",
             "controls": ["Use the unscaled predictions as a within-split baseline."],
             "predictions": ["A tail mechanism concentrates the proper-score change in confident errors."],
             "measurements": ["Per-observation score changes", "Probability-stratum summaries"],
             "tests_hypothesis_ids": ["metric_geometry"]},
            {"id": "nested_resampling", "question": "Does the pattern persist under larger and nested resampling?",
             "design": "Repeat the analysis with more splits and an outer held-out evaluation.",
             "controls": ["Keep the scoring definitions fixed across resampling regimes."],
             "predictions": ["Sampling variation predicts a narrower and less consistent mean pattern."],
             "measurements": ["Split-level distributions", "Outer-fold metric changes"],
             "tests_hypothesis_ids": ["sampling_variation"]},
        ],
        "figure_plan": [
            {"id": "metric_plot", "kind": "figure", "asset_id": "figure_metric", "purpose": "Show the direction and spread of each metric change.",
             "supports": ["metric_divergence"], "source_refs": ["e1", "e2"],
             "readout": "Readers can compare the metric directions without collapsing them into one score.",
             "placement": "Results immediately after the primary metric comparison."},
            {"id": "split_plot", "kind": "figure", "asset_id": "figure_split", "purpose": "Show whether split-level changes agree with their means.",
             "supports": ["split_heterogeneity", "sampling_variation"], "source_refs": ["e3"],
             "readout": "Readers can see the heterogeneity that limits the aggregate interpretation.",
             "placement": "Results after the distributional summary."},
            {"id": "evidence_table", "kind": "table", "asset_id": None, "purpose": "Map each mechanism to a prediction and a test.",
             "supports": ["metric_geometry", "sampling_variation"], "source_refs": ["e1", "e3"],
             "readout": "Readers can distinguish observed evidence from tests that remain to be run.",
             "placement": "Discussion before the limitations paragraph."},
        ],
        "limitations": ["The design covers one dataset and model family.", "The current aggregates do not identify a causal mechanism."],
    }


class ResearchArgumentTests(unittest.TestCase):
    def test_argument_normalizer_only_repairs_unambiguous_provider_formatting(self):
        candidate = {"argument": argument()}
        candidate["argument"]["figure_plan"][0]["kind"] = "plot"
        normalized, changes = _normalise_argument_candidate(
            candidate, available_asset_ids={"figure_metric", "figure_split"})
        validate_research_argument(normalized, evidence_ids={"e1", "e2", "e3"},
                                   asset_ids={"figure_metric", "figure_split"})
        self.assertEqual(normalized["figure_plan"][0]["kind"], "figure")
        self.assertTrue(any(item["action"] == "unwrap_provider_envelope" for item in changes))

    def test_requires_competing_hypotheses_and_figure_jobs(self):
        validate_research_argument(argument(), evidence_ids={"e1", "e2", "e3"})

    def test_rejects_single_hypothesis(self):
        value = argument()
        value["hypotheses"] = value["hypotheses"][:1]
        with self.assertRaisesRegex(ValidationError, "at least two competing"):
            validate_research_argument(value, evidence_ids={"e1", "e2", "e3"})

    def test_rejects_uncovered_observed_pattern(self):
        value = argument()
        value["figure_plan"][0]["supports"] = ["sampling_variation"]
        with self.assertRaisesRegex(ValidationError, "every observed pattern"):
            validate_research_argument(value, evidence_ids={"e1", "e2", "e3"})

    def test_rejects_unbound_rendered_figure(self):
        with self.assertRaisesRegex(ValidationError, "unknown result asset"):
            validate_research_argument(argument(), evidence_ids={"e1", "e2", "e3"},
                                        asset_ids={"figure_metric"})

    def test_rejects_internal_workflow_language(self):
        value = argument()
        value["primary_argument"]["thesis"] = "The frozen artifact was accepted."
        with self.assertRaisesRegex(ValidationError, "control-plane"):
            validate_research_argument(value, evidence_ids={"e1", "e2", "e3"})

    def test_argument_review_must_cover_all_reasoning_dimensions(self):
        review = {"schema_version": "research-argument-review-1", "decision": "accept",
                  "checks": [{"id": "question", "outcome": "passed", "evidence": "focused"}],
                  "required_repairs": [], "rationale": "ready"}
        with self.assertRaisesRegex(ValidationError, "cover question"):
            validate_argument_review(review)

    def test_runner_generates_and_adjudicates_before_returning(self):
        valid = argument()

        class FakeClient:
            assignments = []

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                packet = json.loads(prompt)
                self.assignments.append(packet["assignment"])
                if packet["assignment"].startswith("Build a versioned"):
                    body = valid
                else:
                    body = {"schema_version": "research-argument-review-1", "decision": "accept",
                            "checks": [{"id": "question", "outcome": "passed", "evidence": "Question is focused."},
                                       {"id": "evidence", "outcome": "passed", "evidence": "Observations are bound."},
                                       {"id": "mechanisms", "outcome": "passed", "evidence": "Mechanisms differ."},
                                       {"id": "experiments", "outcome": "passed", "evidence": "Tests discriminate."},
                                       {"id": "figures", "outcome": "passed", "evidence": "Figure jobs are explicit."}],
                            "required_repairs": [], "rationale": "The argument is ready for composition."}
                return ModelResult(text=json.dumps(body), model="fake", usage={"model_calls": 1},
                                   elapsed_seconds=0.01, finish_reason="stop")

        packet = {"results_package": {"findings": [{"id": "e1"}], "metrics": [{"id": "e2"}],
                                      "limitations": ["A limitation."]}, "evidence_ids": ["e1", "e2", "e3"]}
        with patch("scisaurus.runtime.research_argument.ModelClient", FakeClient):
            result = ResearchArgumentRunner({"base_url": "http://example.invalid", "model": "fake",
                                             "protocol": "openai_compatible", "timeout_seconds": 1,
                                             "max_output_tokens": 100}).run(packet)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["argument_defense"]["schema_version"], "argument-defense-1")
        self.assertEqual(len(result["argument_defense_sha256"]), 64)
        self.assertEqual(len(FakeClient.assignments), 2)


if __name__ == "__main__":
    unittest.main()
