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

    def _unavailable(self):
        return {"id": "crossing_uncertainty", "description": "No interval is identifiable.",
                "status": "not_estimable", "reason": "Local estimator has no variation.",
                "metric_ids": ["crossing"], "estimate": None, "lower": None, "upper": None}

    def test_unavailable_quantitative_records_are_preserved_and_normalization_is_stable(self):
        for field in ("uncertainty", "effect_sizes", "sensitivity", "ablation"):
            with self.subTest(field=field):
                record = self._unavailable()
                analysis = self._analysis()
                analysis[field] = [record]
                normalized = validate_analysis(analysis, metric_ids={"crossing"})
                self.assertEqual(normalized[field], [record])
                self.assertEqual(validate_analysis(normalized, metric_ids={"crossing"}), normalized)
                self.assertEqual(analysis[field], [record])

    def test_unavailable_records_require_explicit_status_reason_and_binding(self):
        mutations = [lambda r: r.pop("status"), lambda r: r.update(status="undefined"),
                     lambda r: r.pop("reason"), lambda r: r.update(reason=" "),
                     lambda r: r.pop("metric_ids"), lambda r: r.update(metric_ids=[]),
                     lambda r: r.update(metric_ids=["crossing", "crossing"]),
                     lambda r: r.update(metric_ids=["unknown"]),
                     lambda r: r.update(metric_ids=[True]),
                     lambda r: r.pop("estimate"), lambda r: r.pop("upper")]
        for field in ("uncertainty", "effect_sizes", "sensitivity", "ablation"):
            for mutate in mutations:
                with self.subTest(field=field, mutate=mutate):
                    record = self._unavailable()
                    mutate(record)
                    analysis = self._analysis()
                    analysis[field] = [record]
                    with self.assertRaises(ValidationError):
                        validate_analysis(analysis, metric_ids={"crossing"})

    def test_unavailable_record_cannot_carry_numeric_placeholders(self):
        for field in ("uncertainty", "effect_sizes", "sensitivity", "ablation"):
            for value in (0, -1, 0.0, True, "0", float("nan"), float("inf")):
                for numeric_field in ("estimate", "lower", "upper", "mean"):
                    with self.subTest(field=field, value=value, numeric_field=numeric_field):
                        record = self._unavailable()
                        record[numeric_field] = value
                        analysis = self._analysis()
                        analysis[field] = [record]
                        with self.assertRaises(ValidationError):
                            validate_analysis(analysis, metric_ids={"crossing"})

    def test_finite_quantitative_records_keep_zero_negative_and_zero_containing_intervals(self):
        for field in ("uncertainty", "effect_sizes", "sensitivity", "ablation"):
            for estimate in (0, -0.5, 0.5):
                with self.subTest(field=field, estimate=estimate):
                    record = {"id": "interval", "description": "Computed interval.",
                              "estimate": estimate, "lower": -1, "upper": 1}
                    analysis = self._analysis()
                    analysis[field] = [record]
                    self.assertEqual(validate_analysis(analysis)[field], [record])

    def test_shared_quantitative_contract_rejects_invalid_numeric_fields(self):
        for field in ("uncertainty", "effect_sizes", "sensitivity", "ablation"):
            for value in (None, True, "0.2", float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    analysis = self._analysis()
                    analysis[field] = [{"id": "interval", "description": "Computed interval.",
                                        "estimate": value, "lower": -1, "upper": 1}]
                    with self.assertRaises(ValidationError):
                        validate_analysis(analysis)

    def test_unavailable_uncertainty_is_quality_debt_until_quantified_evidence_exists(self):
        analysis = self._analysis()
        analysis["uncertainty"] = [self._unavailable()]
        package = {"quality_contract": default_research_quality_contract(), "analysis": analysis,
                   "assets": [{"id": f"figure_{i}", "role": "figure"} for i in range(3)]}
        decision = evaluate_result_package_quality(package)
        self.assertEqual(decision["decision"], "research_expansion_required")
        self.assertEqual(decision["deficits"], [{"field": "uncertainty", "observed": 0,
                          "required": 1, "unresolved_evidence_ids": ["crossing_uncertainty"]}])
        analysis["uncertainty"].append({"id": "exponent_interval", "description": "Bootstrap.",
                                        "estimate": 0, "lower": -0.1, "upper": 0.1})
        self.assertEqual(evaluate_result_package_quality(package)["decision"], "proceed")
        self.assertIsNone(analysis["uncertainty"][0]["estimate"])

    def test_program_normalization_binds_unavailable_analysis_to_actual_metrics(self):
        from scisaurus.runtime.experiment import normalize_program_output
        from copy import deepcopy
        analysis = self._analysis()
        analysis["uncertainty"] = [self._unavailable()]
        output = {"analysis": analysis, "metrics": [{"id": "crossing", "value": None},
                                                    {"id": "exponent", "value": 0.2}]}
        normalized = normalize_program_output(deepcopy(output))
        self.assertEqual(normalized["analysis"]["uncertainty"], [self._unavailable()])
        self.assertEqual(normalize_program_output(deepcopy(normalized)), normalized)
        output["analysis"]["uncertainty"][0]["metric_ids"] = ["absent"]
        with self.assertRaisesRegex(ValidationError, "unknown emitted metrics"):
            normalize_program_output(output)

    def test_finite_primary_estimate_can_have_unavailable_uncertainty(self):
        from scisaurus.runtime.experiment import normalize_program_output
        analysis = self._analysis()
        analysis["uncertainty"] = [self._unavailable()]
        output = {"analysis": analysis, "metrics": [{"id": "crossing", "value": 150.0}]}
        self.assertIsNone(normalize_program_output(output)["analysis"]["uncertainty"][0]["estimate"])

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
