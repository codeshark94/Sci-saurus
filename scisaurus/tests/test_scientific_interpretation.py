import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.scientific_interpretation import validate_interpretation


def interpretation():
    return {
        "schema_version": "scientific-interpretation-1",
        "research_question": "Does recalibration improve all probability scores on a small tabular sample?",
        "result_patterns": [{
            "id": "mixed_scores", "result_ref": "result-1",
            "pattern": "A proper score worsened while two calibration summaries improved.",
            "so_what": "Calibration quality cannot be represented by one summary alone.",
            "supporting_evidence": ["finding-1"], "contradicting_evidence": [],
        }],
        "competing_explanations": [{
            "id": "tail_reweighting", "mechanism": "A scalar map may improve central bins while amplifying errors in a small number of extreme cases.",
            "status": "possible", "supporting_evidence": ["finding-1"], "counterevidence": [],
            "discriminating_test": "Compare per-observation loss changes and calibration strata across more datasets.",
        }],
        "discriminating_experiments": [{
            "id": "stratified_replication", "question": "Does the same tradeoff recur across datasets and model families?",
            "design": "Repeat the comparison across prespecified datasets, models, and resampling schemes.",
            "predictions": ["A tail-driven mechanism predicts concentrated log-loss increases."],
            "required_measurements": ["Per-observation log-loss changes", "Calibration curves by score range"],
        }],
        "prioritization": {"primary_pattern_id": "mixed_scores", "secondary_pattern_ids": [],
                            "rationale": "The disagreement between proper and calibration summaries determines the practical interpretation."},
        "conclusion": "The observed tradeoff motivates metric-specific evaluation and does not establish universal benefit.",
    }


class ScientificInterpretationTests(unittest.TestCase):
    def test_validates_explicit_pattern_mechanism_and_test(self):
        value = interpretation()
        validate_interpretation(value, evidence_ids={"finding-1"})

    def test_rejects_internal_vocabulary(self):
        value = interpretation()
        value["conclusion"] = "The frozen artifact was accepted."
        with self.assertRaisesRegex(ValidationError, "control-plane"):
            validate_interpretation(value)

    def test_rejects_unknown_evidence(self):
        value = interpretation()
        value["result_patterns"][0]["supporting_evidence"] = ["unknown"]
        with self.assertRaisesRegex(ValidationError, "unknown evidence"):
            validate_interpretation(value, evidence_ids={"finding-1"})


if __name__ == "__main__":
    unittest.main()

