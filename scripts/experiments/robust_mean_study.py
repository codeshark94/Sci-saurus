#!/usr/bin/env python3
"""Run the frozen robust-mean simulation and emit one JSON result object."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


def percentile(values, probability):
    values = sorted(float(value) for value in values)
    position = (len(values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] * (1 - fraction) + values[upper] * fraction


def main():
    request = json.load(sys.stdin)
    if "experiment" not in request:
        print(json.dumps({"status": "ready", "capability": "robust-mean-simulation"}))
        return
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    experiment = request["experiment"]
    parameters = experiment["parameters"]
    n = int(parameters["sample_size"])
    blocks = int(parameters["blocks"])
    rate = float(parameters["contamination_rate"])
    scale = float(parameters["contamination_scale"])
    if n < 2 or blocks < 2 or n % blocks or not 0 <= rate < 0.5 or scale <= 1:
        raise ValueError("invalid frozen robust-mean parameters")
    block_size = n // blocks
    scenarios = ("clean", "contaminated")
    seed_sequence = np.random.SeedSequence(experiment["seed"])
    observations = []
    curves = {}
    for scenario, child_seed in zip(scenarios, seed_sequence.spawn(len(scenarios))):
        rng = np.random.default_rng(child_seed)
        samples = rng.normal(0.0, 1.0, size=(experiment["run_count"], n))
        if scenario == "contaminated":
            mask = rng.random((experiment["run_count"], n)) < rate
            samples[mask] = rng.normal(0.0, scale, size=int(mask.sum()))
            outliers = mask.sum(axis=1)
        else:
            outliers = np.zeros(experiment["run_count"], dtype=int)
        empirical = samples.mean(axis=1)
        mom = np.median(samples.reshape(experiment["run_count"], blocks, block_size).mean(axis=2), axis=1)
        mean_error = np.abs(empirical)
        mom_error = np.abs(mom)
        curves[scenario] = (mean_error, mom_error)
        observations.extend({"scenario": scenario, "replicate": index + 1,
            "empirical_mean": float(empirical[index]), "median_of_means": float(mom[index]),
            "mean_abs_error": float(mean_error[index]), "mom_abs_error": float(mom_error[index]),
            "outlier_count": int(outliers[index])} for index in range(experiment["run_count"]))

    by_scenario = {scenario: [row for row in observations if row["scenario"] == scenario]
                   for scenario in scenarios}
    def errors(scenario, estimator):
        return [row[f"{estimator}_abs_error"] for row in by_scenario[scenario]]
    contaminated_mean_p95 = percentile(errors("contaminated", "mean"), .95)
    contaminated_mom_p95 = percentile(errors("contaminated", "mom"), .95)
    values = {
        "contamination_mean_median_abs_error": percentile(errors("contaminated", "mean"), .5),
        "contamination_mom_median_abs_error": percentile(errors("contaminated", "mom"), .5),
        "contamination_mean_p95_abs_error": contaminated_mean_p95,
        "contamination_mom_p95_abs_error": contaminated_mom_p95,
        "contamination_p95_reduction_percent": 100 * (contaminated_mean_p95 - contaminated_mom_p95) / contaminated_mean_p95,
        "contamination_mom_win_rate_percent": 100 * sum(
            row["mom_abs_error"] < row["mean_abs_error"] for row in by_scenario["contaminated"]) / experiment["run_count"],
        "clean_mean_median_abs_error": percentile(errors("clean", "mean"), .5),
        "clean_mom_median_abs_error": percentile(errors("clean", "mom"), .5),
    }
    values["clean_mom_penalty_percent"] = 100 * (
        values["clean_mom_median_abs_error"] - values["clean_mean_median_abs_error"]
    ) / values["clean_mean_median_abs_error"]
    definitions = {item["id"]: item for item in experiment["primary_outcomes"]}
    presentations = {
        "contamination_p95_reduction_percent": f"{values['contamination_p95_reduction_percent']:.2f}% lower 95th-percentile absolute error under contamination",
        "contamination_mom_win_rate_percent": f"median-of-means had lower absolute error in {values['contamination_mom_win_rate_percent']:.2f}% of contaminated replicates",
        "clean_mom_penalty_percent": f"{values['clean_mom_penalty_percent']:.2f}% higher median absolute error for median-of-means on clean data",
    }
    metrics = []
    conditions = {
        "contamination_p95_reduction_percent": f"n={n}, {rate:.0%} N(0,{scale:g}^2) replacement contamination, {blocks} blocks",
        "contamination_mom_win_rate_percent": f"n={n}, {rate:.0%} N(0,{scale:g}^2) replacement contamination, paired replicates",
        "clean_mom_penalty_percent": f"n={n}, no contamination, {blocks} blocks",
    }
    for metric_id in definitions:
        metrics.append({"id": metric_id, "value": values[metric_id], "unit": definitions[metric_id]["unit"],
                        "conditions": conditions[metric_id], "source": "raw-data.json",
                        "presentation": presentations[metric_id]})

    figure_path = Path("robust-mean-errors.png")
    colors = {"Empirical mean": "#31688e", "Median of means": "#d1495b"}
    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.2), sharey=True, constrained_layout=True)
    for axis, scenario in zip(axes, scenarios):
        for label, data in (("Empirical mean", curves[scenario][0]), ("Median of means", curves[scenario][1])):
            ordered = np.sort(data)
            cumulative = np.arange(1, len(ordered) + 1) / len(ordered)
            axis.plot(ordered, cumulative, label=label, color=colors[label], linewidth=2.1)
        axis.set_xscale("log")
        axis.set_title("Clean samples" if scenario == "clean" else "5% replacement contamination")
        axis.set_xlabel("Absolute estimation error (log scale)")
        axis.grid(True, which="both", alpha=.22, linewidth=.7)
        axis.axhline(.95, color="#555555", linestyle="--", linewidth=.9)
    axes[0].set_ylabel("Empirical cumulative probability")
    axes[1].legend(frameon=False, loc="lower right")
    fig.suptitle(f"Mean estimation across {experiment['run_count']:,} seeded replicates (n={n})", fontsize=12)
    fig.savefig(figure_path, dpi=180, metadata={"Software": "Sci-saurus robust mean pilot"})
    plt.close(fig)
    figure = figure_path.read_bytes()

    result = {"schema_version": "experiment-program-output-1", "study_id": experiment["id"],
        "revision": experiment["revision"],
        "procedures": [{"id": "simulation_protocol", "description": experiment["method"],
                        "source": "scripts/experiments/robust_mean_study.py"}],
        "observations": observations, "metrics": metrics,
        "findings": [
            {"id": "contaminated_tail_result",
             "statement": f"Under the frozen contamination model, median-of-means produced {presentations['contamination_p95_reduction_percent']}.",
             "metric_ids": ["contamination_p95_reduction_percent"]},
            {"id": "clean_tradeoff_result",
             "statement": f"The robustness gain had a clean-data tradeoff: {presentations['clean_mom_penalty_percent']}.",
             "metric_ids": ["clean_mom_penalty_percent"]},
            {"id": "paired_win_result",
             "statement": f"Across paired contaminated samples, {presentations['contamination_mom_win_rate_percent']}.",
             "metric_ids": ["contamination_mom_win_rate_percent"]}],
        "limitations": [*experiment["limitations"],
            "The simulation records estimator outputs per replicate rather than every generated sample value."],
        "assets": [{"id": "error_ecdf_figure", "path": str(figure_path),
                    "sha256": hashlib.sha256(figure).hexdigest(), "role": "figure", "media_type": "image/png",
                    "caption": "Empirical cumulative distributions of absolute estimation error for the empirical mean and 20-block median-of-means estimator under clean and symmetric replacement-contamination scenarios."}]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
