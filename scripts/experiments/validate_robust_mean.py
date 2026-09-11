#!/usr/bin/env python3
"""Independently recalculate the robust-mean study metrics from recorded rows."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import sys


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=str).encode()


def percentile(values, probability):
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def main():
    request = json.load(sys.stdin)
    if "candidate" not in request:
        print(json.dumps({"status": "ready", "capability": "robust-mean-independent-validator"}))
        return
    candidate = request["candidate"]
    observations = candidate["observations"]
    run_count = len(observations) // 2
    checks = []
    def check(name, condition, evidence):
        checks.append({"id": name, "outcome": "passed" if condition else "failed", "evidence": evidence})
    check("candidate_hash", hashlib.sha256(canonical(candidate)).hexdigest() == request["candidate_sha256"],
          "Canonical candidate bytes reproduce the supplied SHA-256.")
    scenarios = {name: [row for row in observations if row.get("scenario") == name]
                 for name in ("clean", "contaminated")}
    check("balanced_rows", all(len(rows) == run_count for rows in scenarios.values()) and run_count > 0,
          f"Recorded {len(scenarios['clean'])} clean and {len(scenarios['contaminated'])} contaminated rows.")
    check("replicate_identity", all(sorted(row.get("replicate") for row in rows) == list(range(1, run_count + 1))
                                    for rows in scenarios.values()),
          "Each scenario has one row for every configured replicate index.")
    error_consistent = all(
        math.isclose(abs(float(row["empirical_mean"])), float(row["mean_abs_error"]), rel_tol=0, abs_tol=1e-15)
        and math.isclose(abs(float(row["median_of_means"])), float(row["mom_abs_error"]), rel_tol=0, abs_tol=1e-15)
        for row in observations)
    check("row_arithmetic", error_consistent, "Absolute errors equal the magnitudes of the recorded estimates.")
    check("finite_values", all(all(math.isfinite(float(row[key])) for key in (
        "empirical_mean", "median_of_means", "mean_abs_error", "mom_abs_error")) for row in observations),
        "All recorded estimator values and errors are finite.")

    contaminated = scenarios["contaminated"]
    clean = scenarios["clean"]
    contaminated_mean_p95 = percentile([row["mean_abs_error"] for row in contaminated], .95)
    contaminated_mom_p95 = percentile([row["mom_abs_error"] for row in contaminated], .95)
    clean_mean_median = statistics.median(row["mean_abs_error"] for row in clean)
    clean_mom_median = statistics.median(row["mom_abs_error"] for row in clean)
    calculated = {
        "contamination_p95_reduction_percent": 100 * (contaminated_mean_p95 - contaminated_mom_p95) / contaminated_mean_p95,
        "contamination_mom_win_rate_percent": 100 * sum(
            row["mom_abs_error"] < row["mean_abs_error"] for row in contaminated) / len(contaminated),
        "clean_mom_penalty_percent": 100 * (clean_mom_median - clean_mean_median) / clean_mean_median,
    }
    reported = {metric["id"]: metric["value"] for metric in candidate["metrics"]}
    recalculations = []
    for outcome in request["primary_outcomes"]:
        metric_id = outcome["id"]
        recalculated = calculated[metric_id]
        matches = metric_id in reported and math.isclose(float(reported[metric_id]), recalculated,
                                                          rel_tol=1e-12, abs_tol=1e-12)
        recalculations.append({"metric_id": metric_id, "reported_value": reported.get(metric_id, "missing"),
            "recalculated_value": recalculated, "tolerance": 1e-12, "matches": matches})
    passed = all(item["outcome"] == "passed" for item in checks) and all(
        item["matches"] for item in recalculations)
    result = {"schema_version": "experiment-validation-1", "study_id": candidate["study_id"],
        "candidate_sha256": request["candidate_sha256"], "decision": "accepted" if passed else "rejected",
        "checks": checks, "metric_recalculations": recalculations,
        "limitations": ["The independent validator recalculates summaries from recorded replicate outputs; it does not reconstruct omitted sample matrices."]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
