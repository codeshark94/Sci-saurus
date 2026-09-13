import unittest

from scisaurus.runtime.research_quality import (
    default_research_quality_contract,
    ensure_minimum_quality_contract,
    evaluate_result_package_quality,
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
