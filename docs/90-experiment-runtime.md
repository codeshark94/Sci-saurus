# Scientific Experiment Runtime

> Implemented 2026-09-11. The runtime generates new scientific results only under an explicit versioned experiment Score.

## 1. Boundary

`run-experiment` turns a frozen method into a reviewable `results-package-2`. It does not let a writer invent results, treat a program exit as scientific acceptance, or silently expand a study after seeing an effect. A novel-research Score requires the current `eligible_for_experiment` assessment from a completed literature survey. Replication, methods-validation, and exploratory Scores may use another declared current survey outcome, but the outcome and the experiment remain distinct evidence.

The current adapter runs project-configured JSON programs. Their executable, argv, package/source identity files, working directory, complete stdin/stdout, exit status, and resource call are recorded by the existing Operations Cell. Inputs remain public in this slice. Remote experimental backends can be added as explicit adapters; a model-authentication route is not implicitly usable as a scientific execution route.

## 2. Acceptance path

1. Freeze the question, hypothesis, method, parameters, seed, run count, stopping rule, primary outcomes, limitations, required assets, and time policy.
2. Check the declared literature gate when present.
3. Register and independently probe separate execution and validation programs. Their pinned operational identities must differ.
4. Run the exact experiment twice. The complete program output and every declared asset hash must reproduce exactly.
5. Capture observations as `raw-data.json` and capture program-generated figures as immutable project artifacts.
6. Give the result to the distinct validation program. It must bind the exact candidate SHA-256, execute explicit checks, and recalculate every primary outcome.
7. Send the summarized result, deterministic validation, and PNG/JPEG figures to at least two bounded model-review perspectives. They assess method alignment, calculation trace, inference scope, limitation coverage, and every proposed finding.
8. A final arbiter copies exact reviewer outcomes and evidence references. Acceptance cannot hide a rejected review or omit a finding from review.
9. Emit and adopt `results-package-2` only after the replay, deterministic recalculation, and model assessment all accept. External publication remains outside this command.

Exact replay establishes deterministic behavior for the pinned environment and input. It does not prove that the implementation is mathematically correct. Independent recalculation from recorded replicate outputs addresses summary arithmetic, while its stated limitation preserves the fact that omitted sample matrices are not reconstructed.

## 3. Files and commands

Install the isolated open-source numerical and plotting runtime, then prepare an inert configuration:

```bash
sh scripts/setup-experiment-runtime.sh
python3 scripts/prepare-experiment-config.py --output /tmp/experiment.json
```

Set the authorized model connection, change `live_dispatch_allowed` to `true`, and optionally bind a completed literature survey. Use a new project directory:

```bash
python3 -m scisaurus.cli run-experiment /tmp/experiment-run \
  --config /tmp/experiment.json \
  --first-result-seconds 720 --target-seconds 960 --deadline-seconds 1200
```

The example executes `scripts/experiments/robust_mean_study.py` with NumPy and Matplotlib pinned by `requirements-experiment.txt`. `scripts/experiments/validate_robust_mean.py` uses a separate standard-library implementation of the summary calculations. The output directory contains `experiment.md`, `run.json`, and a `results-package/` directory with the package, raw replicate outputs, and figure.

## 4. Paper integration

The paper builder accepts both supplied `results-package-1` inputs and generated `results-package-2` inputs. A generated package retains its experiment Score, literature basis, two execution records, separate program profiles, replay hash, deterministic validation, model reviews, and assessment. If the package declares a literature basis, the paper configuration must use those same accepted survey and assessment references. Figure assets are copied into the candidate and rendered in the PDF with their package captions.

`validation_report` is available for a real replication or methods-validation outcome whose contribution is not a novelty claim. `research_paper` still requires an experiment-eligible survey assessment. This prevents a successful simulation from being relabeled as novel research merely because it produced a polished PDF.

## 5. Live execution evidence

Run `65d6e8c6e86e427dbf02ffcc54ba8f38` completed the frozen robust-mean example in 43.05 seconds. The pinned program generated 10,000 clean and 10,000 contaminated samples of size 200, ran twice with byte-equivalent structured output and matching asset hashes, and passed five checks from a distinct standard-library calculator. Two scoped multimodal reviewers and a final assessment accepted the result package.

Within the frozen symmetric replacement-contamination simulation, median-of-means reduced the 95th-percentile absolute error by 57.58% and had lower paired absolute error in 78.51% of contaminated replicates. Its median clean-data absolute error was 22.83% higher. The package retains four exact limitations, including that this is one seeded finite simulation rather than a general proof, and that it does not cover adversarial placement, asymmetry, dependence, multivariate estimation, or real data.

The adopted `artifact:methods/experiment-results/package@1` and its output are under `.runs/axion-experiment-20260911T031515Z/`. The run used three model calls and five program calls, and `scisaurus.cli verify` reports `ok`. A later storyline-first validation report consumed this exact package; the scientific claims were not regenerated by manuscript writers.

## 6. Current limits

The first runtime supports one frozen local-program experiment, exact deterministic replay, bounded image assets, two or more same-model procedural review roles, and one separate deterministic calculator. It does not yet resume an interrupted experiment, schedule a parameter sweep across several machines, preserve full sample matrices unless the study program emits them, provide statistically independent model reviewers, or establish correctness against expert adjudication. Those are explicit follow-on evaluation and scaling tasks, not hidden fallbacks.
