import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.research_quality import (
    default_research_quality_contract,
    ensure_minimum_quality_contract,
    evaluate_result_package_quality,
    validate_analysis,
)


class ResearchQualityTests(unittest.TestCase):
    def _analysis(self):
        return {
            "conditions": ["control", "treatment"],
            "independent_seeds": [17],
            "controls": ["control"],
            "comparisons": [
                {"id": "primary", "description": "Primary comparison."},
                {"id": "sensitivity", "description": "Sensitivity comparison."},
            ],
            "uncertainty": ["Descriptive interval."],
            "effect_sizes": ["Difference in the primary outcome."],
            "sensitivity": ["Prespecified condition sensitivity."],
            "ablation": [],
            "raw_data": ["Every replicate is retained."],
        }

    def test_missing_contract_is_a_research_hold(self):
        decision = evaluate_result_package_quality({"assets": []})
        self.assertEqual(decision["decision"], "research_expansion_required")
        self.assertEqual(decision["deficits"][0]["field"], "quality_contract")

    def test_complete_contract_is_admitted(self):
        contract = default_research_quality_contract()
        result = evaluate_result_package_quality({
            "quality_contract": contract,
            "analysis": self._analysis(),
            "assets": [{"id": f"figure_{index}", "role": "figure"} for index in range(3)],
        })
        self.assertEqual(result["decision"], "proceed")
        self.assertEqual(result["deficits"], [])

    def test_string_comparisons_normalize_to_stable_evidence_records(self):
        analysis = self._analysis()
        analysis["comparisons"] = [
            "down-sweep minus up-sweep slope",
            "beta zero control",
            "down-sweep minus up-sweep slope",
        ]

        normalized = validate_analysis(analysis)
        repeated = validate_analysis(analysis)

        self.assertEqual(normalized, repeated)
        self.assertEqual(len(normalized["comparisons"]), 2)
        self.assertEqual(
            [item["description"] for item in normalized["comparisons"]],
            ["down-sweep minus up-sweep slope", "beta zero control"],
        )
        self.assertTrue(all(item["id"].startswith("comparison-")
                            for item in normalized["comparisons"]))
        decision = evaluate_result_package_quality({
            "quality_contract": default_research_quality_contract(),
            "analysis": analysis,
            "assets": [{"id": f"figure_{index}", "role": "figure"}
                       for index in range(3)],
        })
        self.assertEqual(decision["decision"], "proceed")

    def test_numeric_bootstrap_interval_is_preserved_inside_uncertainty_record(self):
        analysis = self._analysis()
        interval = {
            "id": "bootstrap_slope_difference",
            "description": "Mean per-phi OLS slope difference; percentile bootstrap, 2000 resamples.",
            "estimate": 0.25,
            "lower": 0.11,
            "upper": 0.39,
            "n_resamples": 2000,
            "seed": 17,
        }
        analysis["uncertainty"] = [interval]

        normalized = validate_analysis(analysis)

        self.assertEqual(normalized["uncertainty"], [interval])
        decision = evaluate_result_package_quality({
            "quality_contract": default_research_quality_contract(),
            "analysis": analysis,
            "assets": [{"id": f"figure_{index}", "role": "figure"}
                       for index in range(3)],
        })
        self.assertEqual(decision["decision"], "proceed")

    def test_numeric_bootstrap_interval_rejects_invalid_bounds_and_nonfinite_values(self):
        invalid_records = [
            {"id": "interval", "description": "Missing upper", "estimate": 0.2,
             "lower": 0.1},
            {"id": "interval", "description": "Missing point estimate", "lower": 0.1,
             "upper": 0.3},
            {"id": "interval", "description": "Reversed bounds", "lower": 0.4,
             "upper": 0.1},
            {"id": "interval", "description": "Nonfinite bound", "lower": float("nan"),
             "upper": 0.3},
        ]
        for record in invalid_records:
            with self.subTest(record=record):
                analysis = self._analysis()
                analysis["uncertainty"] = [record]
                with self.assertRaises(ValidationError):
                    validate_analysis(analysis)

    def test_metric_specific_analysis_keys_remain_rejected(self):
        analysis = self._analysis()
        analysis["bootstrap_slope_difference"] = {
            "estimate": 0.25,
            "lower": 0.11,
            "upper": 0.39,
        }

        with self.assertRaisesRegex(ValidationError, "unknown fields"):
            validate_analysis(analysis)

    def test_rejected_scientific_result_is_not_admitted_despite_complete_analysis(self):
        package = {
            "quality_contract": default_research_quality_contract(),
            "analysis": self._analysis(),
            "assets": [{"id": f"figure_{index}", "role": "figure"} for index in range(3)],
            "validation": {"decision": "rejected"},
        }

        rejected = evaluate_result_package_quality(
            package, minimum_contract=default_research_quality_contract())

        self.assertEqual(rejected["decision"], "research_expansion_required")
        self.assertEqual([item["field"] for item in rejected["deficits"]], ["validation.decision"])
        self.assertEqual(rejected["expansion_requests"][0]["id"], "repair_rejected_experiment_result")
        self.assertEqual(rejected["expansion_requests"][0]["kind"], "additional_experiment")

        package["validation"]["decision"] = "accepted_with_limitations"
        admitted = evaluate_result_package_quality(
            package, minimum_contract=default_research_quality_contract())
        self.assertEqual(admitted["decision"], "proceed")

    def test_missing_display_and_sensitivity_are_separate_requests(self):
        contract = default_research_quality_contract()
        analysis = self._analysis()
        analysis["sensitivity"] = []
        result = evaluate_result_package_quality({
            "quality_contract": contract,
            "analysis": analysis,
            "assets": [{"id": "figure_1", "role": "figure"}],
        })
        self.assertEqual(result["decision"], "research_expansion_required")
        fields = {item["field"] for item in result["deficits"]}
        self.assertEqual(fields, {"figures", "sensitivity"})
        self.assertTrue(any(item["kind"] == "analysis_display" for item in result["expansion_requests"]))

    def test_journal_floor_rejects_a_weaker_declared_contract(self):
        weak = {
            "minimum_conditions": 1,
            "minimum_independent_seeds": 1,
            "minimum_controls": 0,
            "minimum_comparisons": 1,
            "required_analyses": ["raw_data"],
            "minimum_figures": 1,
        }
        analysis = self._analysis()
        result = evaluate_result_package_quality({
            "quality_contract": weak,
            "analysis": analysis,
            "assets": [{"id": f"figure_{index}", "role": "figure"} for index in range(3)],
        }, minimum_contract=default_research_quality_contract())
        self.assertEqual(result["decision"], "research_expansion_required")
        self.assertTrue(any(item["field"].startswith("quality_contract.") for item in result["deficits"]))

    def test_minimum_contract_upgrade_is_monotone(self):
        weak = {
            "minimum_conditions": 1,
            "minimum_independent_seeds": 1,
            "minimum_controls": 0,
            "minimum_comparisons": 1,
            "required_analyses": ["ablation"],
            "minimum_figures": 4,
        }
        upgraded = ensure_minimum_quality_contract(weak, study_type="methods_validation")
        floor = default_research_quality_contract()
        self.assertGreaterEqual(upgraded["minimum_conditions"], floor["minimum_conditions"])
        self.assertGreaterEqual(upgraded["minimum_controls"], floor["minimum_controls"])
        self.assertGreaterEqual(upgraded["minimum_figures"], 4)
        self.assertEqual(set(upgraded["required_analyses"]), {"ablation", *floor["required_analyses"]})


if __name__ == "__main__":
    unittest.main()
