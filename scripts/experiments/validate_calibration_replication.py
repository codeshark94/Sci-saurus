#!/usr/bin/env python3
"""Independently recalculate calibration-replication aggregate metrics."""
from __future__ import annotations

import hashlib
import json
import math
import sys

import numpy as np


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def mean(rows, key):
    return sum(float(row[key]) for row in rows) / len(rows)


def percentile(rows, key, fraction):
    values = sorted(float(row[key]) for row in rows)
    if not values:
        raise ValueError("cannot calculate a percentile of an empty observation set")
    position = (len(values) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] + weight * (values[upper] - values[lower])


def shared_raw_bin_ece_delta(row):
    """Recalculate the shared raw-probability-bin ECE delta independently."""
    total = int(row["test_n"])
    if total <= 0:
        raise ValueError("test_n must be positive")
    raw_ece = calibrated_ece = 0.0
    for group in row["reliability"]:
        labels = group["labels"]
        raw = group["raw_probabilities"]
        calibrated = group["calibrated_probabilities"]
        if not (len(labels) == len(raw) == len(calibrated)):
            raise ValueError("reliability group lengths do not match")
        if labels:
            weight = len(labels) / total
            observed = sum(labels) / len(labels)
            raw_ece += weight * abs(sum(raw) / len(raw) - observed)
            calibrated_ece += weight * abs(sum(calibrated) / len(calibrated) - observed)
    return calibrated_ece - raw_ece


def bootstrap_mean_interval(values, rng, draws=100000):
    """Recalculate the percentile bootstrap interval without experiment helpers."""
    values = np.asarray(values, dtype=np.float64)
    means = np.empty(draws, dtype=np.float64)
    chunk = 2000
    for start in range(0, draws, chunk):
        stop = min(start + chunk, draws)
        indexes = rng.integers(0, len(values), size=(stop - start, len(values)))
        means[start:stop] = values[indexes].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def main():
    raw = json.load(sys.stdin)
    if raw.get("probe") is True:
        json.dump({"probe": "ok", "program": "validate_calibration_replication"}, sys.stdout,
                  ensure_ascii=False, separators=(",", ":"))
        return
    payload = raw
    candidate = payload["candidate"]
    candidate_sha = payload["candidate_sha256"]
    expected = {item["id"] for item in payload["primary_outcomes"]}
    observations = candidate["observations"]
    temperatures = sorted(float(row["temperature"]) for row in observations)
    midpoint = len(temperatures) // 2
    temperature_median = (temperatures[midpoint] if len(temperatures) % 2
                          else (temperatures[midpoint - 1] + temperatures[midpoint]) / 2.0)
    metric_values = {
        "test_log_loss_delta": mean(observations, "test_log_loss_delta"),
        "test_brier_delta": mean(observations, "test_brier_delta"),
        "test_ece_delta": mean(observations, "test_ece_delta"),
        "test_ece_raw_reference_delta": sum(shared_raw_bin_ece_delta(row) for row in observations) / len(observations),
        "rank_invariance_delta": mean(observations, "rank_invariance_delta"),
        "temperature_median": temperature_median,
    }
    for key in ("test_log_loss_delta", "test_brier_delta", "test_ece_delta"):
        metric_values[f"{key}_p025"] = percentile(observations, key, 0.025)
        metric_values[f"{key}_p975"] = percentile(observations, key, 0.975)
    fixed_deltas = [shared_raw_bin_ece_delta(row) for row in observations]
    metric_values["test_ece_raw_reference_delta_p025"] = percentile(
        [{"value": value} for value in fixed_deltas], "value", 0.025)
    metric_values["test_ece_raw_reference_delta_p975"] = percentile(
        [{"value": value} for value in fixed_deltas], "value", 0.975)
    bootstrap_rng = np.random.Generator(np.random.PCG64(20260912))
    for key in ("test_log_loss_delta", "test_brier_delta", "test_ece_delta", "test_ece_raw_reference_delta"):
        values = fixed_deltas if key == "test_ece_raw_reference_delta" else [row[key] for row in observations]
        low, high = bootstrap_mean_interval(values, bootstrap_rng)
        metric_values[f"{key}_mean_ci_low"] = low
        metric_values[f"{key}_mean_ci_high"] = high
    reported = {item["id"]: float(item["value"]) for item in candidate["metrics"]}
    checks = []
    def check(identifier, outcome, evidence):
        checks.append({"id": identifier, "outcome": "passed" if outcome else "failed", "evidence": evidence})

    check("candidate_hash", hashlib.sha256(canonical(candidate)).hexdigest() == candidate_sha,
          "Recomputed the candidate SHA-256 with a separate standard-library canonical JSON implementation.")
    check("replicate_count", len(observations) == 30 and all(row.get("test_n") == 115 for row in observations),
          "Found 30 observations with the declared 115 held-out rows per split.")
    check("finite_metrics", all(math.isfinite(value) for value in metric_values.values()),
          "All independently recalculated aggregate values are finite.")
    check("ranking_invariant", all(abs(float(row["rank_invariance_delta"])) < 1e-12 for row in observations),
          "Temperature scaling preserved score ordering in every held-out split.")
    recalculations = []
    for metric_id in sorted(expected):
        value = metric_values[metric_id]
        reported_value = reported[metric_id]
        matches = abs(value - reported_value) <= 1e-12
        recalculations.append({"metric_id": metric_id, "reported_value": reported_value,
                               "recalculated_value": value, "tolerance": 1e-12, "matches": matches})
    accepted = all(item["outcome"] == "passed" for item in checks) and all(item["matches"] for item in recalculations)
    output = {"schema_version": "experiment-validation-1", "study_id": candidate["study_id"],
              "candidate_sha256": candidate_sha, "decision": "accepted" if accepted else "rejected",
              "checks": checks, "metric_recalculations": recalculations,
              "limitations": candidate["limitations"]}
    json.dump(output, sys.stdout, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
