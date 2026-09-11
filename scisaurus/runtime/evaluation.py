"""Frozen, label-blinded evaluation for scientific judgment tasks."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes, sha256_hex


LABELS = {
    "literature_entailment": {"supported", "unsupported", "insufficient_evidence"},
    "relationship": {"extends", "contradicts", "compares", "related", "none"},
    "gap_state": {"refuted_by_prior_work", "insufficient_evidence", "eligible_for_experiment"},
}


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def validate_corpus(value):
    _exact(value, {"schema_version", "corpus_id", "revision", "split", "label_status",
                   "frozen_at", "cases"}, "evaluation corpus")
    if value["schema_version"] != "judgment-corpus-1":
        raise ValidationError("unsupported evaluation corpus schema")
    if not isinstance(value["corpus_id"], str) or not value["corpus_id"].strip():
        raise ValidationError("evaluation corpus requires an identity")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("evaluation corpus revision must be positive")
    if value["split"] not in {"development", "held_out"}:
        raise ValidationError("evaluation split must be development or held_out")
    if value["label_status"] not in {"development", "expert_adjudicated"}:
        raise ValidationError("evaluation labels must declare their adjudication status")
    if value["split"] == "held_out" and value["label_status"] != "expert_adjudicated":
        raise ValidationError("held-out evaluation requires expert-adjudicated labels")
    if not isinstance(value["frozen_at"], str) or not value["frozen_at"].strip():
        raise ValidationError("evaluation corpus requires a freeze timestamp")
    if not isinstance(value["cases"], list) or not value["cases"]:
        raise ValidationError("evaluation corpus requires cases")
    identifiers = set()
    for case in value["cases"]:
        _exact(case, {"id", "task", "evidence", "label", "rationale"}, "evaluation case")
        if not isinstance(case["id"], str) or not case["id"].strip() or case["id"] in identifiers:
            raise ValidationError("evaluation case identities must be unique and nonempty")
        identifiers.add(case["id"])
        if case["task"] not in LABELS or case["label"] not in LABELS[case["task"]]:
            raise ValidationError("evaluation case task or label is unsupported")
        if not isinstance(case["evidence"], dict) or not case["evidence"]:
            raise ValidationError("evaluation evidence must be an explicit object")
        if not isinstance(case["rationale"], str) or not case["rationale"].strip():
            raise ValidationError("evaluation label requires an adjudication rationale")
    canonical_bytes(value)
    return value


def load_corpus(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("evaluation corpus must be readable JSON") from exc
    return validate_corpus(value)


class JudgmentEvaluation:
    """Keep labels out of inference packets and score only exact frozen cases."""

    def __init__(self, corpus):
        self.corpus = validate_corpus(corpus)
        self.corpus_sha256 = sha256_hex(canonical_bytes(self.corpus))

    def blind_packet(self):
        return {"schema_version": "judgment-blind-packet-1", "corpus_id": self.corpus["corpus_id"],
                "revision": self.corpus["revision"], "corpus_sha256": self.corpus_sha256,
                "cases": [{"id": case["id"], "task": case["task"], "evidence": case["evidence"]}
                          for case in self.corpus["cases"]]}

    def score(self, submission, *, thresholds):
        _exact(submission, {"schema_version", "corpus_sha256", "predictions"}, "evaluation submission")
        if submission["schema_version"] != "judgment-predictions-1":
            raise ValidationError("unsupported prediction schema")
        if submission["corpus_sha256"] != self.corpus_sha256:
            raise ValidationError("predictions do not target the exact frozen corpus")
        _exact(thresholds, {"min_accuracy", "min_coverage", "max_decisive_false_positive_rate"},
               "evaluation thresholds")
        if any(type(thresholds[key]) not in (int, float) or not 0 <= thresholds[key] <= 1 for key in thresholds):
            raise ValidationError("evaluation thresholds must lie between zero and one")
        if not isinstance(submission["predictions"], list):
            raise ValidationError("predictions must be a list")
        predictions = {}
        for row in submission["predictions"]:
            _exact(row, {"case_id", "prediction"}, "prediction")
            if row["case_id"] in predictions:
                raise ValidationError("prediction case identities must be unique")
            predictions[row["case_id"]] = row["prediction"]
        expected = {case["id"] for case in self.corpus["cases"]}
        if set(predictions) != expected:
            raise ValidationError("submission must predict every frozen case exactly once")
        correct = decided = decisive_false_positives = decisive_negative_cases = 0
        per_task = defaultdict(lambda: Counter(total=0, decided=0, correct=0))
        cases = []
        for case in self.corpus["cases"]:
            prediction = predictions[case["id"]]
            if prediction != "abstain" and prediction not in LABELS[case["task"]]:
                raise ValidationError("prediction is outside the task label set")
            is_decided = prediction != "abstain"
            is_correct = prediction == case["label"]
            decisive = case["task"] == "gap_state" and prediction in {
                "refuted_by_prior_work", "eligible_for_experiment"}
            negative = case["task"] == "gap_state" and case["label"] == "insufficient_evidence"
            correct += is_correct
            decided += is_decided
            decisive_false_positives += decisive and negative
            decisive_negative_cases += negative
            per_task[case["task"]]["total"] += 1
            per_task[case["task"]]["decided"] += int(is_decided)
            per_task[case["task"]]["correct"] += int(is_correct)
            cases.append({"case_id": case["id"], "task": case["task"], "prediction": prediction,
                          "label": case["label"], "correct": is_correct,
                          "adjudication_rationale": case["rationale"]})
        total = len(cases)
        metrics = {"accuracy": correct / total, "coverage": decided / total,
                   "decisive_false_positive_rate": (
                       decisive_false_positives / decisive_negative_cases if decisive_negative_cases else 0.0),
                   "correct": correct, "decided": decided, "total": total,
                   "per_task": {task: {**counts, "accuracy": counts["correct"] / counts["total"],
                                        "coverage": counts["decided"] / counts["total"]}
                                for task, counts in sorted(per_task.items())}}
        checks = {"accuracy": metrics["accuracy"] >= thresholds["min_accuracy"],
                  "coverage": metrics["coverage"] >= thresholds["min_coverage"],
                  "decisive_false_positive_rate": metrics["decisive_false_positive_rate"]
                  <= thresholds["max_decisive_false_positive_rate"]}
        eligible = self.corpus["split"] == "held_out" and self.corpus["label_status"] == "expert_adjudicated"
        return {"schema_version": "judgment-evaluation-1", "corpus_id": self.corpus["corpus_id"],
                "corpus_revision": self.corpus["revision"], "corpus_sha256": self.corpus_sha256,
                "split": self.corpus["split"], "label_status": self.corpus["label_status"],
                "metrics": metrics, "thresholds": thresholds, "checks": checks,
                "status": "passed" if eligible and all(checks.values()) else (
                    "failed" if eligible else "development_only"), "cases": cases}
