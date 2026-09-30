"""Contracts for the pre-composition scientific argument stage."""

import json
import hashlib
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.research_argument import (
    ArgumentAdjudicator,
    ResearchArgumentRunner,
    _normalise_argument_candidate,
    _validate_argument_generation_budget,
    argument_response_repair_prompt,
    argument_evidence_packet,
    argument_prompt,
    evidence_ids_from_packet,
    validate_argument_review,
    validate_research_argument,
    review_prompt,
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
    def test_continuation_repair_orders_reach_argument_prompt(self):
        packet = argument_evidence_packet({
            "results_package": {"findings": [{"id": "e1"}], "limitations": []},
            "evidence_ids": ["e1"],
            "scientific_follow_up": [{
                "kind": "additional_experiment",
                "objective": "Run a threshold sensitivity sweep.",
                "success_condition": "The threshold is calibrated or bounded.",
            }],
            "follow_up_instruction": "Address the evidence-producing repair before adjudication.",
        })
        self.assertEqual(packet["scientific_follow_up"][0]["kind"], "additional_experiment")
        prompt = json.loads(argument_prompt(packet))
        self.assertEqual(
            prompt["scientific_repair_order"]["orders"][0]["objective"],
            "Run a threshold sensitivity sweep.",
        )

    def test_scientific_argument_prose_is_not_rejected_for_character_count(self):
        candidate = argument()
        candidate["primary_argument"]["thesis"] = (
            "The observed divergence is consistent with metric sensitivity, but the current aggregate "
            "does not distinguish that explanation from split-level sampling variation, so the claim is "
            "limited to the supplied dataset, model, metrics, and resampling design pending the specified "
            "per-observation decomposition and nested-resampling checks."
        )
        self.assertGreater(len(candidate["primary_argument"]["thesis"]), 280)
        _validate_argument_generation_budget(
            candidate, min_figures=2, min_tables=1, min_experiments=2)

        prompt = json.loads(argument_prompt({"evidence_ids": ["e1"]}))
        serialized_contract = json.dumps(
            prompt["generation_limits"], ensure_ascii=False).casefold()
        self.assertNotIn("character", serialized_contract)
        self.assertIn("concise, complete prose", serialized_contract)

    def test_adjudication_repairs_reach_the_next_argument_generation(self):
        review = {
            "schema_version": "research-argument-review-1",
            "decision": "revise",
            "checks": [{
                "id": "mechanisms", "outcome": "failed",
                "evidence": "The causal mechanism is not identified by the supplied comparison.",
            }],
            "required_repairs": [{
                "id": "mechanism", "target": "primary_argument",
                "problem": "The text claims a causal mechanism from an observational contrast.",
                "repair": "Narrow the claim to an association and identify the missing causal test.",
                "verification": "The revised thesis is bounded by the actual design.",
            }],
            "rationale": "The evidence supports the association but not the mechanism.",
        }
        payload = json.loads(argument_prompt(
            {"evidence_ids": ["e1"], "scientific_follow_up": []},
            validation_feedback={
                "error": "Independent adjudication requested a targeted argument revision.",
                "previous_response": argument(),
                "adjudication": review,
            },
        ))

        repair = payload["repair_request"]
        self.assertEqual(repair["adjudication"], review)
        self.assertIn("every failed check", repair["instructions"])
        self.assertIn("a format error", repair["instructions"])

    def test_argument_contract_repair_keeps_the_adjudication_repairs(self):
        review = {
            "decision": "revise",
            "checks": [{"id": "evidence", "outcome": "failed",
                        "evidence": "The current evidence does not identify causality."}],
            "required_repairs": [{
                "id": "bound-claim", "target": "primary_argument.thesis",
                "problem": "The thesis asserts a causal mechanism from an observational comparison.",
                "repair": "Narrow the thesis to the observed association.",
                "verification": "The revised thesis makes no causal claim.",
            }],
        }
        prompt = json.loads(argument_response_repair_prompt(
            {"evidence_ids": ["e1"]}, previous_response="partial JSON",
            validation_error="missing required field",
            validation_feedback={"adjudication": review},
        ))
        self.assertEqual(prompt["repair_request"]["adjudication"], review)
        self.assertIn("every failed check", prompt["repair_request"]["instructions"])
        self.assertNotIn("partial_response", prompt)

    def test_argument_normalizer_only_repairs_unambiguous_provider_formatting(self):
        candidate = {"argument": argument()}
        candidate["argument"]["figure_plan"][0]["kind"] = "plot"
        normalized, changes = _normalise_argument_candidate(
            candidate, available_asset_ids={"figure_metric", "figure_split"})
        validate_research_argument(normalized, evidence_ids={"e1", "e2", "e3"},
                                   asset_ids={"figure_metric", "figure_split"})
        self.assertEqual(normalized["figure_plan"][0]["kind"], "figure")
        self.assertTrue(any(item["action"] == "unwrap_provider_envelope" for item in changes))

    def test_argument_normalizer_restores_only_a_missing_authoritative_question(self):
        expected = argument()["research_question"]
        candidate = argument()
        candidate.pop("research_question")

        normalized, changes = _normalise_argument_candidate(
            {"argument": candidate}, expected_research_question=expected)

        self.assertEqual(normalized["research_question"], expected)
        self.assertTrue(any(
            item["field"] == "research_question"
            and item["action"] == "restore_from_authoritative_evidence_packet"
            for item in changes
        ))
        mismatched = argument()
        mismatched["research_question"] = "A different research question."
        with self.assertRaisesRegex(ValidationError, "conflicts with the authoritative"):
            _normalise_argument_candidate(
                mismatched, expected_research_question=expected)

    def test_interpretation_envelope_projects_authoritative_patterns_and_evidence(self):
        pattern = {
            "id": "pattern_dilation_activity",
            "pattern": "The dilation term remains active in every modeled cell.",
            "so_what": "The analytic model does not establish an observed physical mechanism.",
            "supporting_evidence": ["e1"],
            "contradicting_evidence": ["e2"],
        }
        packet = {
            "scientific_interpretation": {
                "schema_version": "interpretation-output-1",
                "interpretation": {
                    "research_question": "Does the operator change the onset slope?",
                    "result_patterns": [pattern],
                },
            },
            "results_package": {"findings": [{"id": "e3"}]},
        }

        projected = argument_evidence_packet(packet)

        self.assertEqual(
            projected["scientific_interpretation"]["result_patterns"], [pattern])
        self.assertEqual(evidence_ids_from_packet(packet), ["e1", "e2", "e3"])
        self.assertIn("e1", projected["evidence_ids"])
        self.assertIn("e2", projected["evidence_ids"])

    def test_argument_normalizer_restores_referenced_patterns_only_from_authoritative_source(self):
        value = argument()
        value["hypotheses"][0]["explains_pattern_ids"].append("pattern_dilation_activity")
        value["figure_plan"][0]["supports"].append("pattern_dilation_activity")
        authoritative = [{
            "id": "pattern_dilation_activity",
            "pattern": "The dilation term remains active in every modeled cell.",
            "so_what": "This is a model-internal result, not physical validation.",
            "supporting_evidence": ["e1"],
            "contradicting_evidence": ["e2"],
            "result_ref": "e1",
        }]

        normalized, changes = _normalise_argument_candidate(
            value, available_evidence_ids={"e1", "e2", "e3"},
            authoritative_result_patterns=authoritative)

        validate_research_argument(normalized, evidence_ids={"e1", "e2", "e3"})
        restored = next(item for item in normalized["observed_patterns"]
                        if item["id"] == "pattern_dilation_activity")
        self.assertEqual(restored["evidence_ids"], ["e1", "e2"])
        self.assertTrue(any(
            item["action"] == "restore_from_authoritative_interpretation"
            for item in changes))

        unsupported = argument()
        unsupported["hypotheses"][0]["explains_pattern_ids"].append("invented_pattern")
        with self.assertRaisesRegex(ValidationError, "unknown observed pattern"):
            candidate, _ = _normalise_argument_candidate(
                unsupported, available_evidence_ids={"e1", "e2", "e3"},
                authoritative_result_patterns=authoritative)
            validate_research_argument(candidate, evidence_ids={"e1", "e2", "e3"})

    def test_argument_normalizer_canonicalizes_generated_ids_and_updates_references(self):
        value = argument()
        original = "Sensitivity experiment: bootstrap over every gap and operator"
        value["discriminating_experiments"][0]["id"] = original
        value["figure_plan"][0]["supports"].append(original)

        normalized, changes = _normalise_argument_candidate(value)

        validate_research_argument(normalized, evidence_ids={"e1", "e2", "e3"})
        generated_id = normalized["discriminating_experiments"][0]["id"]
        self.assertRegex(generated_id, r"^[a-z][a-z0-9_-]{0,63}$")
        self.assertNotEqual(generated_id, original)
        self.assertIn(generated_id, normalized["figure_plan"][0]["supports"])
        self.assertTrue(any(
            item["action"] == "normalize_generated_identifier"
            for item in changes))

    def test_argument_normalizer_repairs_explicit_pattern_id_list_serialization(self):
        value = argument()
        value["hypotheses"][0]["explains_pattern_ids"] = "metric_divergence; metric_divergence"
        value["hypotheses"][1]["explains_pattern_ids"] = "- split_heterogeneity"

        normalized, changes = _normalise_argument_candidate(value)

        validate_research_argument(normalized, evidence_ids={"e1", "e2", "e3"})
        self.assertEqual(normalized["hypotheses"][0]["explains_pattern_ids"], ["metric_divergence"])
        self.assertEqual(normalized["hypotheses"][1]["explains_pattern_ids"], ["split_heterogeneity"])
        self.assertEqual(
            [item["field"] for item in changes if item["action"] == "normalize_generated_string_list"],
            ["hypotheses[0].explains_pattern_ids", "hypotheses[1].explains_pattern_ids"],
        )

    def test_argument_normalizer_does_not_split_comma_separated_pattern_prose(self):
        value = argument()
        value["hypotheses"][0]["explains_pattern_ids"] = "metric_divergence, split_heterogeneity"
        with self.assertRaisesRegex(ValidationError, "unknown observed pattern"):
            normalized, _ = _normalise_argument_candidate(value)
            validate_research_argument(normalized, evidence_ids={"e1", "e2", "e3"})

    def test_requires_competing_hypotheses_and_figure_jobs(self):
        validate_research_argument(argument(), evidence_ids={"e1", "e2", "e3"})

    def test_evidence_may_resolve_every_competing_hypothesis(self):
        value = argument()
        value["hypotheses"][0]["status"] = "supported"
        value["hypotheses"][0]["evidence_ids"] = ["e1"]
        value["hypotheses"][0]["counterevidence"] = ["e3"]
        value["hypotheses"][1]["status"] = "disfavored"
        value["hypotheses"][1]["evidence_ids"] = []
        value["hypotheses"][1]["counterevidence"] = ["e2"]

        validate_research_argument(value, evidence_ids={"e1", "e2", "e3"})

    def test_disfavored_hypothesis_requires_linked_counterevidence(self):
        value = argument()
        value["hypotheses"][1]["status"] = "disfavored"
        value["hypotheses"][1]["counterevidence"] = []

        with self.assertRaisesRegex(ValidationError, "disfavored hypothesis requires counterevidence"):
            validate_research_argument(value, evidence_ids={"e1", "e2", "e3"})

    def test_argument_review_must_judge_evidence_direction_for_each_status(self):
        prompt = json.loads(review_prompt(argument(), {"evidence_ids": ["e1", "e2", "e3"]}))
        self.assertIn("correct direction", prompt["questions"][1])
        self.assertIn("whether its status is warranted", prompt["questions"][1])

    def test_hypothesis_counterevidence_must_reference_supplied_evidence(self):
        value = argument()
        value["hypotheses"][0]["counterevidence"] = ["invented-evidence"]

        with self.assertRaisesRegex(ValidationError, "counterevidence references unknown evidence"):
            validate_research_argument(value, evidence_ids={"e1", "e2", "e3"})

    def test_argument_normalizer_materializes_missing_result_table(self):
        value = argument()
        value["figure_plan"] = [item for item in value["figure_plan"] if item["kind"] != "table"]
        normalized, changes = _normalise_argument_candidate(
            value, available_asset_ids={"figure_metric", "figure_split"})
        validate_research_argument(
            normalized, evidence_ids={"e1", "e2", "e3"},
            asset_ids={"figure_metric", "figure_split"}, min_tables=1)
        self.assertEqual(normalized["figure_plan"][-1]["kind"], "table")
        self.assertIsNone(normalized["figure_plan"][-1]["asset_id"])
        self.assertTrue(any(item["action"] == "materialize_result_table_from_bound_evidence"
                            for item in changes))

    def test_argument_normalizer_restores_existing_links_for_supported_hypothesis(self):
        value = argument()
        value["hypotheses"][0]["status"] = "candidate"
        value["hypotheses"][1]["status"] = "supported"
        value["hypotheses"][1]["evidence_ids"] = []
        value["hypotheses"][1]["counterevidence"] = ["e1"]
        normalized, changes = _normalise_argument_candidate(
            value,
            available_evidence_ids={"e1", "e2", "e3"},
        )
        validate_research_argument(normalized, evidence_ids={"e1", "e2", "e3"})
        self.assertEqual(normalized["hypotheses"][1]["evidence_ids"], ["e3"])
        self.assertTrue(any(item["action"] == "restore_existing_evidence_links"
                            and item["source"] == "observed_pattern" for item in changes))

    def test_disfavored_hypothesis_keeps_counterevidence_separate_from_support(self):
        value = argument()
        value["hypotheses"][0]["status"] = "candidate"
        value["hypotheses"][1]["status"] = "disfavored"
        value["hypotheses"][1]["counterevidence"] = ["e2"]
        value["hypotheses"][1]["evidence_ids"] = []
        normalized, changes = _normalise_argument_candidate(
            value,
            available_evidence_ids={"e1", "e2", "e3"},
        )
        validate_research_argument(normalized, evidence_ids={"e1", "e2", "e3"})
        self.assertEqual(normalized["hypotheses"][1]["evidence_ids"], [])
        self.assertEqual(normalized["hypotheses"][1]["counterevidence"], ["e2"])
        self.assertFalse(any(item["field"] == "hypotheses[1].evidence_ids"
                             for item in changes))

    def test_argument_normalizer_maps_unsupported_to_unresolved_without_promoting_claim(self):
        value = argument()
        value["hypotheses"][1]["status"] = "unsupported"
        normalized, changes = _normalise_argument_candidate(
            value, available_evidence_ids={"e1", "e2", "e3"})
        validate_research_argument(normalized, evidence_ids={"e1", "e2", "e3"})
        self.assertEqual(normalized["hypotheses"][1]["status"], "unresolved")
        self.assertTrue(any(
            item["action"] == "normalize_conservative_status"
            and item["from"] == "unsupported" and item["to"] == "unresolved"
            for item in changes
        ))

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
            configs = []

            def __init__(self, **config):
                self.config = config
                self.configs.append(config)

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
        self.assertEqual(len(FakeClient.configs), 2)
        self.assertTrue(all(config["output_format"] == "json_object"
                            for config in FakeClient.configs))

    def test_runner_restores_missing_question_without_format_repair_call(self):
        valid = argument()
        expected_question = valid.pop("research_question")
        assignments = []

        class MissingQuestionClient:
            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                request = json.loads(prompt)
                assignments.append(request["assignment"])
                if request["assignment"].startswith("Build a versioned"):
                    body = valid
                else:
                    body = {"schema_version": "research-argument-review-1", "decision": "accept",
                            "checks": [
                                {"id": "question", "outcome": "passed", "evidence": "Question is focused."},
                                {"id": "evidence", "outcome": "passed", "evidence": "Observations are bound."},
                                {"id": "mechanisms", "outcome": "passed", "evidence": "Mechanisms differ."},
                                {"id": "experiments", "outcome": "passed", "evidence": "Tests discriminate."},
                                {"id": "figures", "outcome": "passed", "evidence": "Figure jobs are explicit."},
                            ], "required_repairs": [], "rationale": "The argument is ready for composition."}
                return ModelResult(text=json.dumps(body), model="fake",
                                   usage={"model_calls": 1}, elapsed_seconds=0.01,
                                   finish_reason="stop")

        packet = {
            "research_question": expected_question,
            "results_package": {"findings": [{"id": "e1"}], "metrics": [{"id": "e2"}],
                                "limitations": ["A limitation."]},
            "evidence_ids": ["e1", "e2", "e3"],
            "asset_ids": ["figure_metric", "figure_split"],
        }
        with patch("scisaurus.runtime.research_argument.ModelClient", MissingQuestionClient):
            result = ResearchArgumentRunner({
                "base_url": "http://example.invalid", "model": "fake",
                "protocol": "openai_compatible", "timeout_seconds": 1,
                "max_output_tokens": 100,
            }).run(packet)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["argument"]["research_question"], expected_question)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(len(assignments), 2)

    def test_runner_preserves_nested_interpretation_pattern_without_extra_repair_call(self):
        candidate = argument()
        new_pattern_id = "pattern_dilation_activity"
        malformed_experiment_id = "Sensitivity experiment: bootstrap over every gap and operator"
        candidate["hypotheses"][0]["explains_pattern_ids"].append(new_pattern_id)
        candidate["figure_plan"][0]["supports"].extend(
            [new_pattern_id, malformed_experiment_id])
        candidate["discriminating_experiments"][0]["id"] = malformed_experiment_id
        requests = []

        class SourceBackedCandidateClient:
            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                request = json.loads(prompt)
                requests.append(request["assignment"])
                if request["assignment"].startswith("Build a versioned"):
                    body = candidate
                else:
                    body = {
                        "schema_version": "research-argument-review-1",
                        "decision": "accept",
                        "checks": [
                            {"id": "question", "outcome": "passed", "evidence": "Question is focused."},
                            {"id": "evidence", "outcome": "passed", "evidence": "Observations are bound."},
                            {"id": "mechanisms", "outcome": "passed", "evidence": "Mechanisms differ."},
                            {"id": "experiments", "outcome": "passed", "evidence": "Tests discriminate."},
                            {"id": "figures", "outcome": "passed", "evidence": "Figure jobs are explicit."},
                        ],
                        "required_repairs": [],
                        "rationale": "The argument remains bounded by supplied evidence.",
                    }
                return ModelResult(
                    text=json.dumps(body), model="fake", usage={"model_calls": 1},
                    elapsed_seconds=0.01, finish_reason="stop")

        packet = {
            "research_question": candidate["research_question"],
            "evidence_ids": ["e1", "e2", "e3"],
            "asset_ids": ["figure_metric", "figure_split"],
            "results_package": {
                "findings": [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}],
                "limitations": ["This is an analytic model without calibrated measurements."],
            },
            "scientific_interpretation": {
                "schema_version": "interpretation-output-1",
                "interpretation": {
                    "research_question": candidate["research_question"],
                    "result_patterns": [{
                        "id": new_pattern_id,
                        "pattern": "The dilation term remains active in every modeled cell.",
                        "so_what": "This model-internal result does not validate physical dilation.",
                        "supporting_evidence": ["e1"],
                        "contradicting_evidence": ["e2"],
                        "result_ref": "e1",
                    }],
                },
            },
        }
        with patch("scisaurus.runtime.research_argument.ModelClient", SourceBackedCandidateClient):
            result = ResearchArgumentRunner({
                "base_url": "http://example.invalid", "model": "fake",
                "protocol": "openai_compatible", "timeout_seconds": 1,
                "max_output_tokens": 100,
            }).run(packet)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(len(requests), 2)
        self.assertIn(new_pattern_id, {
            item["id"] for item in result["argument"]["observed_patterns"]})
        normalized_experiment_id = result["argument"]["discriminating_experiments"][0]["id"]
        self.assertRegex(normalized_experiment_id, r"^[a-z][a-z0-9_-]{0,63}$")
        self.assertIn(normalized_experiment_id, result["argument"]["figure_plan"][0]["supports"])

    def test_runner_recovers_a_length_finished_json_with_missing_outer_closer(self):
        valid = argument()

        class TruncatedClient:
            calls = 0

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                self.__class__.calls += 1
                packet = json.loads(prompt)
                if packet["assignment"].startswith("Build a versioned"):
                    return ModelResult(
                        text=json.dumps(valid)[:-1], model="fake",
                        usage={"model_calls": 1}, elapsed_seconds=0.01,
                        finish_reason="length")
                return ModelResult(
                    text=json.dumps({"schema_version": "research-argument-review-1",
                                     "decision": "accept",
                                     "checks": [
                                         {"id": "question", "outcome": "passed", "evidence": "focused"},
                                         {"id": "evidence", "outcome": "passed", "evidence": "bound"},
                                         {"id": "mechanisms", "outcome": "passed", "evidence": "different"},
                                         {"id": "experiments", "outcome": "passed", "evidence": "discriminating"},
                                         {"id": "figures", "outcome": "passed", "evidence": "covered"},
                                     ], "required_repairs": [], "rationale": "ready"}),
                    model="fake", usage={"model_calls": 1}, elapsed_seconds=0.01,
                    finish_reason="stop")

        packet = {"results_package": {"findings": [{"id": "e1"}],
                                       "metrics": [{"id": "e2"}],
                                       "limitations": ["A limitation."]},
                  "evidence_ids": ["e1", "e2", "e3"]}
        with patch("scisaurus.runtime.research_argument.ModelClient", TruncatedClient):
            result = ResearchArgumentRunner(
                {"base_url": "http://example.invalid", "model": "fake",
                 "protocol": "openai_compatible", "timeout_seconds": 1,
                 "max_output_tokens": 8192}).run(packet)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(TruncatedClient.calls, 2)

    def test_length_response_uses_compact_fallback_with_enough_output_budget(self):
        valid = argument()
        valid["research_question"] = "Does the measured sign cross?"
        review = {
            "schema_version": "research-argument-review-1", "decision": "accept",
            "checks": [
                {"id": "question", "outcome": "passed", "evidence": "Focused."},
                {"id": "evidence", "outcome": "passed", "evidence": "Bound."},
                {"id": "mechanisms", "outcome": "passed", "evidence": "Distinct."},
                {"id": "experiments", "outcome": "passed", "evidence": "Discriminating."},
                {"id": "figures", "outcome": "passed", "evidence": "Covered."},
            ], "required_repairs": [], "rationale": "The map is bounded and traceable.",
        }

        class RepairClient:
            configs = []
            prompts = []

            def __init__(self, **config):
                self.config = config
                self.configs.append(config)

            def complete(self, *, system, prompt, images=None):
                self.prompts.append(json.loads(prompt))
                assignment = self.prompts[-1]["assignment"]
                if assignment.startswith("Build a versioned"):
                    return ModelResult(
                        text=json.dumps({"schema_version": "research-argument-1"}),
                        model="primary", usage={"model_calls": 1},
                        elapsed_seconds=0.01, finish_reason="length")
                body = review if assignment.startswith("Independently challenge") else valid
                return ModelResult(text=json.dumps(body), model=self.config["model"],
                                   usage={"model_calls": 1}, elapsed_seconds=0.01,
                                   finish_reason="stop")

        packet = {
            "research_question": "Does the measured sign cross?",
            "results_package": {
                "findings": [{"id": "e1", "summary": "Observed result."}],
                "metrics": [{"id": "e2", "value": 0.0}],
                "limitations": ["Only the declared range was evaluated."],
            },
            "evidence_ids": ["e1", "e2", "e3"],
            "asset_ids": ["figure_metric", "figure_split"],
        }
        model = {
            "base_url": "http://example.invalid/v1", "model": "primary",
            "protocol": "openai_compatible", "timeout_seconds": 1,
            "max_output_tokens": 9000,
            "role_model_fallbacks": {"strategy.argument": [{
                "base_url": "http://example.invalid/v1", "model": "fallback",
                "protocol": "openai_compatible", "max_output_tokens": 9000,
            }]},
        }
        with patch("scisaurus.runtime.research_argument.ModelClient", RepairClient):
            result = ResearchArgumentRunner(model).run(packet)

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(RepairClient.configs[1]["model"], "fallback")
        self.assertEqual(RepairClient.configs[0]["max_output_tokens"], 6144)
        self.assertEqual(RepairClient.configs[1]["max_output_tokens"], 6144)
        generation_limits = RepairClient.prompts[0]["generation_limits"]
        self.assertIn("2-8 materially distinct", generation_limits["observed_patterns"])
        self.assertIn("exactly two", generation_limits["hypotheses"])
        self.assertIn("exactly 2 figures and 1 tables", generation_limits["figure_plan"])
        self.assertIn("3600 output tokens", RepairClient.prompts[0]["output_contract"]["response_size"])
        contract_text = json.dumps(
            RepairClient.prompts[0]["output_contract"], ensure_ascii=False).casefold()
        self.assertNotIn("character", contract_text)
        repair_prompt = RepairClient.prompts[1]
        self.assertNotIn("evidence_packet", repair_prompt)
        self.assertIn("requires exactly", repair_prompt["validation_error"])
        self.assertEqual(repair_prompt["grounding_summary"]["allowed_evidence_ids"],
                         ["e1", "e2", "e3"])

    def test_failed_format_repair_is_bounded_and_accounts_for_both_calls(self):
        class AlwaysTruncatedClient:
            calls = 0
            prompts = []
            configs = []

            def __init__(self, **config):
                self.config = config
                self.configs.append(config)

            def complete(self, *, system, prompt, images=None):
                self.__class__.calls += 1
                self.__class__.prompts.append(json.loads(prompt))
                return ModelResult(
                    text='{"schema_version":',
                    model=self.config["model"],
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="length",
                )

        model = {
            "base_url": "http://example.invalid/v1", "model": "primary",
            "protocol": "openai_compatible", "timeout_seconds": 1,
            "max_output_tokens": 8192,
            "role_model_fallbacks": {"strategy.argument": [{
                "base_url": "http://example.invalid/v1", "model": "fallback",
                "protocol": "openai_compatible", "max_output_tokens": 8192,
            }]},
        }
        packet = {
            "research_question": "Does the measured sign cross?",
            "results_package": {"findings": [{"id": "e1"}]},
            "evidence_ids": ["e1", "e2", "e3"],
            "asset_ids": ["figure_metric", "figure_split"],
        }
        with patch("scisaurus.runtime.research_argument.ModelClient", AlwaysTruncatedClient):
            with self.assertRaisesRegex(ValidationError, "candidate validation failed") as raised:
                ResearchArgumentRunner(model).run(packet)

        self.assertEqual(AlwaysTruncatedClient.calls, 2)
        self.assertEqual(raised.exception.usage["model_calls"], 2)
        self.assertEqual(raised.exception.usage["output_tokens"], 40)
        self.assertEqual(AlwaysTruncatedClient.configs[0]["max_output_tokens"], 6144)
        self.assertEqual(AlwaysTruncatedClient.configs[1]["max_output_tokens"], 6144)
        self.assertEqual(AlwaysTruncatedClient.prompts[1]["assignment"],
                         "Complete the interrupted research-argument JSON response.")
        self.assertEqual(
            AlwaysTruncatedClient.prompts[1]["truncated_response"],
            '{"schema_version":',
        )
        self.assertIn("Do not explain the repair",
                      AlwaysTruncatedClient.prompts[1]["instruction"])
        self.assertNotIn("partial_response", AlwaysTruncatedClient.prompts[1])
        self.assertIn("grounding_summary", AlwaysTruncatedClient.prompts[1])

    def test_truncated_adjudication_uses_fallback_and_bounded_repair(self):
        review = {"schema_version": "research-argument-review-1", "decision": "accept",
                  "checks": [{"id": "question", "outcome": "passed", "evidence": "Question is focused."},
                             {"id": "evidence", "outcome": "passed", "evidence": "Observations are bound."},
                             {"id": "mechanisms", "outcome": "passed", "evidence": "Mechanisms differ."},
                             {"id": "experiments", "outcome": "passed", "evidence": "Tests discriminate."},
                             {"id": "figures", "outcome": "passed", "evidence": "Figure jobs are explicit."}],
                  "required_repairs": [], "rationale": "The argument is ready for composition."}

        class FallbackClient:
            configs = []
            prompts = []
            responses = [
                ModelResult(text="{\"schema_version\":", model="primary",
                            usage={"model_calls": 1, "output_tokens": 4096},
                            elapsed_seconds=0.01, finish_reason="length"),
                ModelResult(text=json.dumps(review), model="fallback",
                            usage={"model_calls": 1, "output_tokens": 120},
                            elapsed_seconds=0.01, finish_reason="stop"),
            ]

            def __init__(self, **config):
                self.configs.append(config)

            def complete(self, *, system, prompt, images=None):
                self.prompts.append(prompt)
                return self.responses.pop(0)

        model = {
            "base_url": "http://example.invalid/v1", "model": "primary",
            "protocol": "openai_compatible", "timeout_seconds": 1,
            "max_output_tokens": 8192,
            "role_models": {"strategy.argument-reviewer": {
                "base_url": "http://example.invalid/v1", "model": "primary",
                "protocol": "openai_compatible",
            }},
            "role_model_fallbacks": {"strategy.argument-reviewer": [{
                "base_url": "http://example.invalid/v1", "model": "fallback",
                "protocol": "openai_compatible",
            }]},
        }
        evidence_packet = {
            "evidence_ids": ["e1", "e2", "e3"],
            "asset_ids": ["figure_metric", "figure_split"],
            "argument_defense": {"claim": "The thesis is bounded.", "basis": ["e1"]},
            "literature_evidence": [{"work_id": "W_REVIEW", "finding": "Independent evidence."}],
        }
        with patch("scisaurus.runtime.research_argument.ModelClient", FallbackClient):
            result, usage = ArgumentAdjudicator(model).run(
                argument(), evidence_packet,
                max_attempts=2)
        self.assertEqual(result["decision"], "accept")
        self.assertEqual(usage["model_calls"], 2)
        self.assertEqual(FallbackClient.configs[0]["model"], "primary")
        self.assertEqual(FallbackClient.configs[1]["model"], "fallback")
        self.assertEqual(FallbackClient.configs[0]["max_output_tokens"], 4096)
        self.assertLessEqual(FallbackClient.configs[1]["max_output_tokens"], 4096)
        repaired_prompt = json.loads(FallbackClient.prompts[1])
        self.assertEqual(repaired_prompt["evidence_packet"], evidence_packet)
        self.assertEqual(repaired_prompt["argument_defense"], evidence_packet["argument_defense"])
        self.assertIn("evidence", repaired_prompt["questions"][1].lower())

    def test_truncated_adjudication_stops_after_one_compact_repair(self):
        class AlwaysTruncatedReviewer:
            calls = 0
            configs = []

            def __init__(self, **config):
                self.config = config
                self.configs.append(config)

            def complete(self, *, system, prompt, images=None):
                self.__class__.calls += 1
                return ModelResult(
                    text='{"schema_version":', model=self.config["model"],
                    usage={"model_calls": 1, "output_tokens": 4096},
                    elapsed_seconds=0.01, finish_reason="length")

        model = {
            "base_url": "http://example.invalid/v1", "model": "primary",
            "protocol": "openai_compatible", "timeout_seconds": 1,
            "max_output_tokens": 8192,
        }
        with patch("scisaurus.runtime.research_argument.ModelClient", AlwaysTruncatedReviewer):
            with self.assertRaisesRegex(ValidationError, "truncated"):
                ArgumentAdjudicator(model).run(
                    argument(), {"evidence_ids": ["e1", "e2", "e3"]})

        self.assertEqual(AlwaysTruncatedReviewer.calls, 2)
        self.assertTrue(all(config["max_output_tokens"] == 4096
                            for config in AlwaysTruncatedReviewer.configs))

    def test_final_adjudication_is_preserved_when_argument_never_reaches_accept(self):
        valid = argument()
        revision = {"schema_version": "research-argument-review-1", "decision": "revise",
                    "checks": [{"id": "question", "outcome": "passed", "evidence": "Question is focused."},
                               {"id": "evidence", "outcome": "passed", "evidence": "Observations are bound."},
                               {"id": "mechanisms", "outcome": "failed", "evidence": "The causal bridge is unsupported."},
                               {"id": "experiments", "outcome": "passed", "evidence": "Tests discriminate."},
                               {"id": "figures", "outcome": "passed", "evidence": "Figure jobs are explicit."}],
                    "required_repairs": [{"id": "mechanism", "target": "primary_argument",
                                           "problem": "causal bridge", "repair": "downgrade the claim to association",
                                           "verification": "link the claim to the discriminating test"}],
                    "rationale": "The mechanism claim needs a bounded repair."}

        class AlwaysReviseClient:
            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                packet = json.loads(prompt)
                body = valid if packet["assignment"].startswith("Build a versioned") else revision
                return ModelResult(text=json.dumps(body), model="fake",
                                   usage={"model_calls": 1}, elapsed_seconds=0.01,
                                   finish_reason="stop")

        packet = {"results_package": {"findings": [{"id": "e1"}], "metrics": [{"id": "e2"}],
                                      "limitations": ["A limitation."]}, "evidence_ids": ["e1", "e2", "e3"]}
        with patch("scisaurus.runtime.research_argument.ModelClient", AlwaysReviseClient):
            with self.assertRaises(ValidationError) as raised:
                ResearchArgumentRunner({"base_url": "http://example.invalid", "model": "fake",
                                        "protocol": "openai_compatible", "timeout_seconds": 1,
                                        "max_output_tokens": 100}).run(packet)
        self.assertEqual(str(raised.exception), "research argument adjudication requires revision")
        self.assertEqual(raised.exception.research_review["decision"], "revise")
        self.assertEqual(raised.exception.research_review["required_repairs"][0]["id"], "mechanism")

    def test_generation_failure_preserves_the_review_for_the_candidate_it_describes(self):
        valid = argument()
        revision = {"schema_version": "research-argument-review-1", "decision": "revise",
                    "checks": [{"id": "question", "outcome": "passed", "evidence": "Question is focused."},
                               {"id": "evidence", "outcome": "passed", "evidence": "Observations are bound."},
                               {"id": "mechanisms", "outcome": "failed", "evidence": "The causal bridge is unsupported."},
                               {"id": "experiments", "outcome": "passed", "evidence": "Tests discriminate."},
                               {"id": "figures", "outcome": "passed", "evidence": "Figure jobs are explicit."}],
                    "required_repairs": [{"id": "mechanism", "target": "primary_argument",
                                           "problem": "causal bridge", "repair": "downgrade the claim to association",
                                           "verification": "link the claim to the discriminating test"}],
                    "rationale": "The mechanism claim needs a bounded repair."}

        class InvalidRevisionClient:
            def __init__(self, **config):
                self.config = config
                self.argument_generations = 0

            def complete(self, *, system, prompt, images=None):
                packet = json.loads(prompt)
                assignment = packet.get("assignment")
                if assignment.startswith("Build a versioned scientific argument"):
                    body = ({**valid, "unexpected_field": "must remain visible as a contract failure"}
                            if packet.get("repair_request") else valid)
                elif assignment == "Independently challenge the proposed research argument before manuscript composition.":
                    body = revision
                else:
                    body = {**valid, "unexpected_field": "must remain visible as a contract failure"}
                return ModelResult(text=json.dumps(body), model="fake",
                                   usage={"model_calls": 1}, elapsed_seconds=0.01,
                                   finish_reason="stop")

        packet = {"results_package": {"findings": [{"id": "e1"}], "metrics": [{"id": "e2"}],
                                       "limitations": ["A limitation."]},
                  "evidence_ids": ["e1", "e2", "e3"]}
        with patch("scisaurus.runtime.research_argument.ModelClient", InvalidRevisionClient):
            with self.assertRaises(ValidationError) as raised:
                ResearchArgumentRunner({"base_url": "http://example.invalid", "model": "fake",
                                        "protocol": "openai_compatible", "timeout_seconds": 1,
                                        "max_output_tokens": 100}).run(packet, max_attempts=2)
        error = raised.exception
        self.assertEqual(error.research_argument, valid)
        self.assertEqual(error.research_review, revision)
        self.assertEqual(error.research_feedback["adjudication"], revision)
        self.assertEqual(error.research_review_argument_sha256,
                         hashlib.sha256(canonical_bytes(valid)).hexdigest())


if __name__ == "__main__":
    unittest.main()
