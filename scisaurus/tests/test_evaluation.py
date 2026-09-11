"""Label blinding, corpus pinning, and decision-calibration tests."""

import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.evaluation import JudgmentEvaluation


def corpus(*, split="held_out", label_status="expert_adjudicated"):
    return {"schema_version": "judgment-corpus-1", "corpus_id": "literature-judgment",
            "revision": 1, "split": split, "label_status": label_status,
            "frozen_at": "2026-09-11T00:00:00Z", "cases": [
        {"id": "entails", "task": "literature_entailment", "evidence": {
            "claim": "The study reports a controlled experiment.",
            "source": "We conducted a controlled experiment."},
         "label": "supported", "rationale": "The source explicitly states the claimed method."},
        {"id": "silence", "task": "literature_entailment", "evidence": {
            "claim": "The work did not evaluate robustness.", "source": "We report mean accuracy."},
         "label": "unsupported", "rationale": "Silence about robustness does not establish its absence."},
        {"id": "gap-unknown", "task": "gap_state", "evidence": {
            "coverage": "abstract only", "closest_work": "full text unavailable"},
         "label": "insufficient_evidence", "rationale": "A decisive novelty state needs resolved closest-work evidence."},
        {"id": "gap-refuted", "task": "gap_state", "evidence": {
            "coverage": "verified full text", "closest_work": "implements the proposed method under the same conditions"},
         "label": "refuted_by_prior_work", "rationale": "The pinned work contains the proposed solution."},
    ]}


class JudgmentEvaluationTests(unittest.TestCase):
    def test_blind_packet_contains_no_labels_or_rationales(self):
        evaluation = JudgmentEvaluation(corpus())
        packet = evaluation.blind_packet()
        self.assertNotIn("label", str(packet))
        self.assertNotIn("rationale", str(packet))
        self.assertEqual(packet["corpus_sha256"], evaluation.corpus_sha256)

    def test_exact_frozen_predictions_pass_with_calibrated_abstention(self):
        evaluation = JudgmentEvaluation(corpus())
        submission = {"schema_version": "judgment-predictions-1",
                      "corpus_sha256": evaluation.corpus_sha256,
                      "predictions": [{"case_id": case["id"], "prediction": case["label"]}
                                      for case in corpus()["cases"]]}
        report = evaluation.score(submission, thresholds={"min_accuracy": 1, "min_coverage": 1,
            "max_decisive_false_positive_rate": 0})
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["metrics"]["decisive_false_positive_rate"], 0)

    def test_decisive_guess_on_insufficient_evidence_fails_safety_threshold(self):
        evaluation = JudgmentEvaluation(corpus())
        predictions = [{"case_id": case["id"], "prediction": case["label"]} for case in corpus()["cases"]]
        predictions[2]["prediction"] = "eligible_for_experiment"
        report = evaluation.score({"schema_version": "judgment-predictions-1",
            "corpus_sha256": evaluation.corpus_sha256, "predictions": predictions},
            thresholds={"min_accuracy": .5, "min_coverage": 1, "max_decisive_false_positive_rate": 0})
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["checks"]["decisive_false_positive_rate"])

    def test_development_labels_cannot_clear_a_release_gate(self):
        value = corpus(split="development", label_status="development")
        evaluation = JudgmentEvaluation(value)
        report = evaluation.score({"schema_version": "judgment-predictions-1",
            "corpus_sha256": evaluation.corpus_sha256,
            "predictions": [{"case_id": case["id"], "prediction": case["label"]} for case in value["cases"]]},
            thresholds={"min_accuracy": 0, "min_coverage": 0, "max_decisive_false_positive_rate": 1})
        self.assertEqual(report["status"], "development_only")

    def test_corpus_hash_or_case_set_drift_is_rejected(self):
        evaluation = JudgmentEvaluation(corpus())
        with self.assertRaisesRegex(ValidationError, "exact frozen corpus"):
            evaluation.score({"schema_version": "judgment-predictions-1", "corpus_sha256": "0" * 64,
                "predictions": []}, thresholds={"min_accuracy": 0, "min_coverage": 0,
                                                "max_decisive_false_positive_rate": 1})


if __name__ == "__main__":
    unittest.main()
