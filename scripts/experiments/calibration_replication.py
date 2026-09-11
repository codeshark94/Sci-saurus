#!/usr/bin/env python3
"""Reproduce a post-hoc temperature-scaling study on a public tabular dataset.

The program is intentionally self-contained: it implements the logistic model,
temperature fit, scoring rules, and reliability diagnostic with NumPy rather
than importing a modelling library.  The experiment runner supplies a fixed
dataset path and SHA-256 through JSON stdin.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


DATA_URL = "https://archive.ics.uci.edu/static/public/17/breast+cancer+wisconsin+diagnostic.zip"


def sigmoid(z):
    z = np.clip(z, -40.0, 40.0)
    return 1.0 / (1.0 + np.exp(-z))


def load_wdbc(path, expected_sha256):
    body = Path(path).read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if digest != expected_sha256:
        raise ValueError("dataset SHA-256 does not match the frozen input")
    rows, labels = [], []
    for line in body.decode("utf-8").splitlines():
        fields = line.strip().split(",")
        if len(fields) != 32:
            raise ValueError("unexpected WDBC row width")
        rows.append([float(value) for value in fields[2:]])
        labels.append(1.0 if fields[1] == "M" else 0.0)
    x = np.asarray(rows, dtype=np.float64)
    y = np.asarray(labels, dtype=np.float64)
    if x.shape != (569, 30) or y.shape != (569,):
        raise ValueError(f"unexpected WDBC shape: {x.shape}, {y.shape}")
    return x, y, digest


def stratified_split(y, seed):
    rng = np.random.default_rng(seed)
    train, calibration, test = [], [], []
    for cls in (0.0, 1.0):
        indexes = np.flatnonzero(y == cls)
        rng.shuffle(indexes)
        n_train = int(round(len(indexes) * 0.60))
        n_calibration = int(round(len(indexes) * 0.20))
        train.extend(indexes[:n_train])
        calibration.extend(indexes[n_train:n_train + n_calibration])
        test.extend(indexes[n_train + n_calibration:])
    return tuple(np.asarray(part, dtype=np.int64) for part in (train, calibration, test))


def standardize(train_x, *others):
    mean = train_x.mean(axis=0)
    scale = train_x.std(axis=0)
    scale[scale < 1e-12] = 1.0
    return ((train_x - mean) / scale, *((part - mean) / scale for part in others))


def fit_logistic(x, y, l2=1e-2):
    design = np.column_stack((np.ones(len(x)), x))
    weights = np.zeros(design.shape[1], dtype=np.float64)
    penalty = np.eye(design.shape[1], dtype=np.float64)
    penalty[0, 0] = 0.0
    for _ in range(180):
        probabilities = sigmoid(design @ weights)
        gradient = (design.T @ (probabilities - y)) / len(y) + l2 * (penalty @ weights)
        curvature = probabilities * (1.0 - probabilities)
        hessian = (design.T * curvature) @ design / len(y) + l2 * penalty
        hessian += np.eye(hessian.shape[0]) * 1e-9
        step = np.linalg.solve(hessian, gradient)
        weights -= step
        if float(np.linalg.norm(step, ord=np.inf)) < 1e-8:
            break
    return weights


def logits(weights, x):
    return np.column_stack((np.ones(len(x)), x)) @ weights


def log_loss(probabilities, y):
    clipped = np.clip(probabilities, 1e-12, 1.0 - 1e-12)
    return float(-np.mean(y * np.log(clipped) + (1.0 - y) * np.log1p(-clipped)))


def brier(probabilities, y):
    return float(np.mean((probabilities - y) ** 2))


def ece(probabilities, y, bins=10):
    edges = np.linspace(0.0, 1.0, bins + 1)
    total = 0.0
    for index in range(bins):
        mask = ((probabilities >= edges[index]) &
                (probabilities < edges[index + 1] if index < bins - 1 else probabilities <= edges[index + 1]))
        if np.any(mask):
            total += float(mask.mean()) * abs(float(probabilities[mask].mean()) - float(y[mask].mean()))
    return float(total)


def fit_temperature(calibration_logits, calibration_y):
    def objective(log_temperature):
        temperature = math.exp(float(log_temperature))
        return log_loss(sigmoid(calibration_logits / temperature), calibration_y)

    grid = np.linspace(-3.0, 3.0, 121)
    values = np.asarray([objective(value) for value in grid])
    best = int(np.argmin(values))
    left = float(grid[max(0, best - 1)])
    right = float(grid[min(len(grid) - 1, best + 1)])
    phi = (1.0 + math.sqrt(5.0)) / 2.0
    for _ in range(70):
        first = right - (right - left) / phi
        second = left + (right - left) / phi
        if objective(first) < objective(second):
            right = second
        else:
            left = first
    return float(math.exp((left + right) / 2.0))


def summarize(observations):
    def mean(key):
        return float(np.mean([item[key] for item in observations]))

    def median(key):
        return float(np.median([item[key] for item in observations]))

    return {
        "test_log_loss_delta": mean("test_log_loss_delta"),
        "test_brier_delta": mean("test_brier_delta"),
        "test_ece_delta": mean("test_ece_delta"),
        "rank_invariance_delta": mean("rank_invariance_delta"),
        "temperature_median": median("temperature"),
        "test_log_loss_delta_p025": float(np.percentile([item["test_log_loss_delta"] for item in observations], 2.5)),
        "test_log_loss_delta_p975": float(np.percentile([item["test_log_loss_delta"] for item in observations], 97.5)),
        "test_brier_delta_p025": float(np.percentile([item["test_brier_delta"] for item in observations], 2.5)),
        "test_brier_delta_p975": float(np.percentile([item["test_brier_delta"] for item in observations], 97.5)),
        "test_ece_delta_p025": float(np.percentile([item["test_ece_delta"] for item in observations], 2.5)),
        "test_ece_delta_p975": float(np.percentile([item["test_ece_delta"] for item in observations], 97.5)),
    }


def statement_for_delta(name, value, unit):
    direction = "lower" if value < 0 else "higher"
    return f"Temperature scaling produced {abs(value):.4f} {unit} {direction} mean held-out {name} than the uncalibrated logistic model across the frozen repeated splits."


def statement_for_range(name, low, high, unit):
    return (f"Across the 30 frozen held-out splits, the {name} delta had an empirical "
            f"2.5th-97.5th percentile range of {low:.4f} to {high:.4f} {unit}; "
            "this is a descriptive split range, not an inferential confidence interval.")


def make_figure(observations, summary, path):
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.5), dpi=180)
    names = ["Log loss", "Brier", "ECE"]
    keys = ["test_log_loss_delta", "test_brier_delta", "test_ece_delta"]
    values = [np.asarray([item[key] for item in observations]) for key in keys]
    # Matplotlib 3.9 renamed ``labels`` to ``tick_labels``; keep the frozen
    # experiment runnable across the pinned and current plotting runtimes.
    try:
        axes[0].boxplot(values, tick_labels=names, showmeans=True, meanline=True)
    except TypeError:  # pragma: no cover - exercised only by older Matplotlib
        axes[0].boxplot(values, labels=names, showmeans=True, meanline=True)
    axes[0].axhline(0.0, color="#333333", linewidth=0.9)
    axes[0].set_ylabel("Calibrated minus raw score (lower is better)")
    axes[0].set_title("Held-out score changes")
    axes[0].grid(axis="y", alpha=0.25)

    raw_bins, calibrated_bins, empirical = [], [], []
    for bin_index in range(10):
        raw_values, calibrated_values, labels = [], [], []
        for item in observations:
            raw_values.extend(item["reliability"][bin_index]["raw_probabilities"])
            calibrated_values.extend(item["reliability"][bin_index]["calibrated_probabilities"])
            labels.extend(item["reliability"][bin_index]["labels"])
        raw_bins.append(float(np.mean(raw_values)) if raw_values else float("nan"))
        calibrated_bins.append(float(np.mean(calibrated_values)) if calibrated_values else float("nan"))
        empirical.append(float(np.mean(labels)) if labels else float("nan"))
    axes[1].plot(empirical, raw_bins, "o-", label="Raw logistic")
    axes[1].plot(empirical, calibrated_bins, "o-", label="Temperature scaled")
    axes[1].plot([0, 1], [0, 1], "--", color="#666666", label="Perfect calibration")
    axes[1].set_xlabel("Observed event frequency")
    axes[1].set_ylabel("Mean predicted probability")
    axes[1].set_title("Reliability across held-out folds")
    axes[1].legend(frameon=False, fontsize=8)
    axes[1].grid(alpha=0.25)
    fig.suptitle("Post-hoc temperature scaling on WDBC", fontsize=13)
    fig.tight_layout()
    fig.savefig(path, metadata={"Date": None})
    plt.close(fig)


def main():
    payload = json.load(sys.stdin)
    if payload.get("probe") is True:
        json.dump({"probe": "ok", "program": "calibration_replication"}, sys.stdout,
                  ensure_ascii=False, separators=(",", ":"))
        return
    configured = payload["configured_input"]
    experiment = payload["experiment"]
    x, y, dataset_sha = load_wdbc(configured["dataset_path"], configured["dataset_sha256"])
    observations = []
    for replicate in range(experiment["run_count"]):
        train, calibration, test = stratified_split(y, experiment["seed"] + replicate)
        train_x, calibration_x, test_x = standardize(x[train], x[calibration], x[test])
        weights = fit_logistic(train_x, y[train], l2=0.01)
        calibration_logits = logits(weights, calibration_x)
        test_logits = logits(weights, test_x)
        temperature = fit_temperature(calibration_logits, y[calibration])
        raw_probabilities = sigmoid(test_logits)
        calibrated_probabilities = sigmoid(test_logits / temperature)
        raw_loss = log_loss(raw_probabilities, y[test])
        calibrated_loss = log_loss(calibrated_probabilities, y[test])
        raw_brier = brier(raw_probabilities, y[test])
        calibrated_brier = brier(calibrated_probabilities, y[test])
        raw_ece = ece(raw_probabilities, y[test])
        calibrated_ece = ece(calibrated_probabilities, y[test])
        # Calibration changes scores but not their ranking. Compare logits rather
        # than clipped probabilities so saturation ties cannot create a false
        # order change in this numerical sanity check.
        raw_order = np.argsort(np.argsort(test_logits))
        calibrated_order = np.argsort(np.argsort(test_logits / temperature))
        rank_invariance_delta = float(np.mean(raw_order == calibrated_order) - 1.0)
        reliability = []
        edges = np.linspace(0.0, 1.0, 11)
        for bin_index in range(10):
            mask = ((raw_probabilities >= edges[bin_index]) &
                    (raw_probabilities < edges[bin_index + 1] if bin_index < 9 else raw_probabilities <= edges[bin_index + 1]))
            reliability.append({
                "raw_probabilities": raw_probabilities[mask].tolist(),
                "calibrated_probabilities": calibrated_probabilities[mask].tolist(),
                "labels": y[test][mask].tolist(),
            })
        observations.append({
            "replicate": replicate,
            "train_n": int(len(train)), "calibration_n": int(len(calibration)), "test_n": int(len(test)),
            "temperature": float(temperature),
            "test_log_loss_raw": raw_loss, "test_log_loss_calibrated": calibrated_loss,
            "test_log_loss_delta": calibrated_loss - raw_loss,
            "test_brier_raw": raw_brier, "test_brier_calibrated": calibrated_brier,
            "test_brier_delta": calibrated_brier - raw_brier,
            "test_ece_raw": raw_ece, "test_ece_calibrated": calibrated_ece,
            "test_ece_delta": calibrated_ece - raw_ece,
            "rank_invariance_delta": rank_invariance_delta,
            "reliability": reliability,
        })
    summary = summarize(observations)
    figure_path = Path("calibration-replication.png")
    make_figure(observations, summary, figure_path)
    findings = [
        {"id": "log_loss_change", "metric_ids": ["test_log_loss_delta"],
         "statement": statement_for_delta("log loss", summary["test_log_loss_delta"], "nats")},
        {"id": "brier_change", "metric_ids": ["test_brier_delta"],
         "statement": statement_for_delta("Brier score", summary["test_brier_delta"], "Brier points")},
        {"id": "ece_change", "metric_ids": ["test_ece_delta"],
         "statement": statement_for_delta("ECE", summary["test_ece_delta"], "ECE points")},
        {"id": "log_loss_split_range", "metric_ids": ["test_log_loss_delta", "test_log_loss_delta_p025", "test_log_loss_delta_p975"],
         "statement": statement_for_range("log loss", summary["test_log_loss_delta_p025"], summary["test_log_loss_delta_p975"], "nats")},
        {"id": "brier_split_range", "metric_ids": ["test_brier_delta", "test_brier_delta_p025", "test_brier_delta_p975"],
         "statement": statement_for_range("Brier score", summary["test_brier_delta_p025"], summary["test_brier_delta_p975"], "Brier points")},
        {"id": "ece_split_range", "metric_ids": ["test_ece_delta", "test_ece_delta_p025", "test_ece_delta_p975"],
         "statement": statement_for_range("ECE", summary["test_ece_delta_p025"], summary["test_ece_delta_p975"], "ECE points")},
    ]
    metrics = [
        {"id": "test_log_loss_delta", "value": summary["test_log_loss_delta"], "unit": "nats",
         "conditions": "Mean calibrated minus raw log loss across 30 held-out test splits; negative favors calibration.",
         "source": "aggregate of observations.test_log_loss_delta", "presentation": f"{summary['test_log_loss_delta']:.4f}"},
        {"id": "test_brier_delta", "value": summary["test_brier_delta"], "unit": "Brier points",
         "conditions": "Mean calibrated minus raw Brier score across 30 held-out test splits; negative favors calibration.",
         "source": "aggregate of observations.test_brier_delta", "presentation": f"{summary['test_brier_delta']:.4f}"},
        {"id": "test_ece_delta", "value": summary["test_ece_delta"], "unit": "ECE points",
         "conditions": "Mean calibrated minus raw 10-bin ECE across 30 held-out test splits; negative favors calibration.",
         "source": "aggregate of observations.test_ece_delta", "presentation": f"{summary['test_ece_delta']:.4f}"},
        {"id": "rank_invariance_delta", "value": summary["rank_invariance_delta"], "unit": "rank agreement delta",
         "conditions": "Mean rank-preservation sanity delta; temperature scaling should preserve ordering.",
         "source": "aggregate of observations.rank_invariance_delta", "presentation": f"{summary['rank_invariance_delta']:.4f}"},
        {"id": "temperature_median", "value": summary["temperature_median"], "unit": "temperature",
         "conditions": "Median fitted temperature across held-out calibration splits.",
         "source": "aggregate of observations.temperature", "presentation": f"{summary['temperature_median']:.4f}"},
        {"id": "test_log_loss_delta_p025", "value": summary["test_log_loss_delta_p025"], "unit": "nats",
         "conditions": "Empirical 2.5th percentile of split-level calibrated minus raw log-loss deltas across 30 held-out splits; descriptive range endpoint, not an inferential confidence interval.",
         "source": "percentile(2.5) of observations.test_log_loss_delta", "presentation": f"{summary['test_log_loss_delta_p025']:.4f}"},
        {"id": "test_log_loss_delta_p975", "value": summary["test_log_loss_delta_p975"], "unit": "nats",
         "conditions": "Empirical 97.5th percentile of split-level calibrated minus raw log-loss deltas across 30 held-out splits; descriptive range endpoint, not an inferential confidence interval.",
         "source": "percentile(97.5) of observations.test_log_loss_delta", "presentation": f"{summary['test_log_loss_delta_p975']:.4f}"},
        {"id": "test_brier_delta_p025", "value": summary["test_brier_delta_p025"], "unit": "Brier points",
         "conditions": "Empirical 2.5th percentile of split-level calibrated minus raw Brier-score deltas across 30 held-out splits; descriptive range endpoint, not an inferential confidence interval.",
         "source": "percentile(2.5) of observations.test_brier_delta", "presentation": f"{summary['test_brier_delta_p025']:.4f}"},
        {"id": "test_brier_delta_p975", "value": summary["test_brier_delta_p975"], "unit": "Brier points",
         "conditions": "Empirical 97.5th percentile of split-level calibrated minus raw Brier-score deltas across 30 held-out splits; descriptive range endpoint, not an inferential confidence interval.",
         "source": "percentile(97.5) of observations.test_brier_delta", "presentation": f"{summary['test_brier_delta_p975']:.4f}"},
        {"id": "test_ece_delta_p025", "value": summary["test_ece_delta_p025"], "unit": "ECE points",
         "conditions": "Empirical 2.5th percentile of split-level calibrated minus raw ECE deltas across 30 held-out splits; descriptive range endpoint, not an inferential confidence interval.",
         "source": "percentile(2.5) of observations.test_ece_delta", "presentation": f"{summary['test_ece_delta_p025']:.4f}"},
        {"id": "test_ece_delta_p975", "value": summary["test_ece_delta_p975"], "unit": "ECE points",
         "conditions": "Empirical 97.5th percentile of split-level calibrated minus raw ECE deltas across 30 held-out splits; descriptive range endpoint, not an inferential confidence interval.",
         "source": "percentile(97.5) of observations.test_ece_delta", "presentation": f"{summary['test_ece_delta_p975']:.4f}"},
    ]
    limitations = list(experiment["limitations"])
    additional_limit = ("The public WDBC features are diagnostic measurements, not a prospective clinical "
                        "deployment cohort; no clinical decision should be inferred.")
    if additional_limit not in limitations:
        limitations.append(additional_limit)
    output = {
        "schema_version": "experiment-program-output-1", "study_id": experiment["id"], "revision": experiment["revision"],
        "procedures": [
            {"id": "dataset_ingest", "description": f"Read the 569-row, 30-feature WDBC diagnostic dataset from the official UCI distribution; SHA-256 {dataset_sha}.", "source": DATA_URL},
            {"id": "repeated_holdout", "description": "For each of 30 fixed seeds, split each class into 60% training, 20% calibration, and 20% held-out test data; standardize using training statistics only.", "source": "calibration-replication.py::stratified_split"},
            {"id": "temperature_scaling", "description": "Fit L2-regularized logistic regression on training data, fit one positive temperature by calibration-set log loss, and score raw and scaled probabilities only on the untouched test split.", "source": "calibration-replication.py::fit_logistic,fit_temperature"},
        ],
        "observations": observations, "metrics": metrics, "findings": findings, "limitations": limitations,
        "assets": [{"id": "calibration_figure", "path": str(figure_path), "sha256": hashlib.sha256(figure_path.read_bytes()).hexdigest(),
                     "role": "figure", "media_type": "image/png", "caption": "Held-out score changes and reliability curves for raw versus temperature-scaled logistic predictions across repeated WDBC splits."}],
    }
    json.dump(output, sys.stdout, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
