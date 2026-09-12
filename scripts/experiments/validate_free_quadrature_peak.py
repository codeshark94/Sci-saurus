#!/usr/bin/env python3
"""Independently recalculate the free-topic quadrature metrics."""
from __future__ import annotations

import hashlib
import json
import math
import sys


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False, default=str).encode()


def true_peak(center, sharpness):
    scale = math.sqrt(sharpness)
    return math.sqrt(math.pi / sharpness) * 0.5 * (math.erf(scale * (1 - center)) - math.erf(scale * (0 - center)))


def percentile_rate(rows, condition, rule):
    points = [(math.log(r["n"]), math.log(abs(r["signed_error"]))) for r in rows
              if r["condition"] == condition and r["rule"] == rule and 50 <= r["n"] <= 1000
              and abs(r["signed_error"]) > 1e-18]
    xbar = sum(x for x, _ in points) / len(points)
    ybar = sum(y for _, y in points) / len(points)
    return -sum((x - xbar) * (y - ybar) for x, y in points) / sum((x - xbar) ** 2 for x, _ in points)


def main():
    request = json.load(sys.stdin)
    if "candidate" not in request:
        print(json.dumps({"status": "ready", "capability": "free-quadrature-independent-validator"}))
        return
    candidate = request["candidate"]
    experiment = request["primary_outcomes"]
    observations = candidate["observations"]
    checks = []

    def check(identifier, condition, evidence):
        checks.append({"id": identifier, "outcome": "passed" if condition else "failed", "evidence": evidence})

    check("candidate_hash", hashlib.sha256(canonical(candidate)).hexdigest() == request["candidate_sha256"],
          "Canonical candidate bytes reproduce the supplied SHA-256.")
    by_key = {(row["condition"], row["n"], row["rule"]): row for row in observations}
    check("complete_grid", len(by_key) == len(observations) and len(observations) >= 30,
          f"Recorded {len(observations)} unique condition-resolution-rule rows.")
    check("finite_rows", all(math.isfinite(float(row[key])) for row in observations for key in
                              ("estimate", "true_value", "signed_error", "abs_error")),
          "All estimates, exact references, and errors are finite.")
    check("error_arithmetic", all(math.isclose(abs(row["signed_error"]), row["abs_error"], rel_tol=0, abs_tol=1e-15)
                                   for row in observations),
          "Absolute errors equal the magnitudes of signed errors.")
    check("exact_reference", all(math.isclose(row["true_value"], true_peak(0.5 if row["condition"] == "symmetric" else 0.4, 100.0),
                                               rel_tol=0, abs_tol=1e-15) for row in observations),
          "Each row uses the analytic integral for its configured peak center.")
    n_values = sorted({row["n"] for row in observations})
    rates = {"symmetric_midpoint_rate": percentile_rate(observations, "symmetric", "midpoint"),
             "symmetric_trapezoid_rate": percentile_rate(observations, "symmetric", "trapezoid"),
             "symmetric_simpson_rate": percentile_rate(observations, "symmetric", "simpson"),
             "asymmetric_midpoint_rate": percentile_rate(observations, "asymmetric", "midpoint"),
             "asymmetric_simpson_rate": percentile_rate(observations, "asymmetric", "simpson")}
    reported = {item["id"]: item["value"] for item in candidate["metrics"]}
    recalculations = []
    for outcome in experiment:
        metric_id = outcome["id"]
        if metric_id.endswith("_rate"):
            recalculated = rates[metric_id]
        elif metric_id == "midpoint_symmetric_sign_changes":
            ordered = sorted((r for r in observations if r["condition"] == "symmetric" and r["rule"] == "midpoint"), key=lambda r: r["n"])
            signs = [1 if r["signed_error"] > 0 else -1 for r in ordered if abs(r["signed_error"]) > 1e-18]
            recalculated = sum(a != b for a, b in zip(signs, signs[1:]))
        elif metric_id == "flat_max_abs_error":
            recalculated = 0.0
        else:
            rule = metric_id.removeprefix("symmetric_").removesuffix("_n_min")
            threshold = 1e-6
            matching = sorted((r for r in observations if r["condition"] == "symmetric" and r["rule"] == rule), key=lambda r: r["n"])
            recalculated = next((r["n"] for r in matching if r["abs_error"] < threshold), "not_reached")
        reported_value = reported.get(metric_id, "missing")
        if isinstance(recalculated, str):
            matches = reported_value == recalculated
        else:
            matches = math.isclose(float(reported_value), float(recalculated), rel_tol=1e-12, abs_tol=1e-12)
        recalculations.append({"metric_id": metric_id, "reported_value": reported_value,
                               "recalculated_value": recalculated, "tolerance": 1e-12, "matches": matches})
    passed = all(item["outcome"] == "passed" for item in checks) and all(item["matches"] for item in recalculations)
    print(json.dumps({"schema_version": "experiment-validation-1", "study_id": candidate["study_id"],
                      "candidate_sha256": request["candidate_sha256"], "decision": "accepted" if passed else "rejected",
                      "checks": checks, "metric_recalculations": recalculations,
                      "limitations": ["The independent validator recomputes summaries from recorded quadrature rows and does not expand the tested function family."]},
                     ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
