# Recovery, executable plans, capability acquisition, evaluation, and paper release

> Implemented 2026-09-11. This document describes executable contracts and their current evidence boundary.

## Durable resume and deadlines

`ExecutionRuntime` records `inputs/run-config` and `inputs/source-manifest` for each new run. Opening an existing control store requires a resume policy. The policy names an additional elapsed window, states whether unknown provider outcomes remain blocked or are conservatively charged before retry, and either rejects changed source or names the affected scopes to reopen. Configuration drift is rejected. The original allocation ledger and captured artifacts survive.

`SurveyRunner` reconstructs works, source captures, query records, maps, focused reviews, accepted surveys, nominations, and accepted assessments from exact artifact versions. Stored search queries are not repeated. Bibliography and identity workload slots are durably reserved before provider dispatch; resume restores their cumulative sequence counters, including failed or interrupted calls and legacy runs whose earlier successful requests predate that ledger. A source change may invalidate mapping or later review while leaving external captures intact. `deadline_replan` computes the required dependency closure and defers optional tasks rather than starting work that cannot leave time for required completion.

```bash
python3 -m scisaurus.cli resume-survey /path/to/existing-run \
  --config /path/to/original-config.json \
  --additional-seconds 600 \
  --reconcile-unknown \
  --reopen-scope mapping \
  --reopen-scope focused_review \
  --reopen-scope integrated_review \
  --reopen-scope gap_assessment
```

The `--reconcile-unknown` flag records one conservative model call per unknown prior attempt. A deployment with different metering should call the recovery API with its own explicit usage vector.

## Executable general-purpose plans

`project-plan-1` is an acyclic graph of tasks. Every task declares its owner, objective, exact inputs, capability requirements, output logical IDs, dependencies, and estimate. `PlanService` versions the plan and uses a task-contract hash plus exact dependency-result refs to decide reuse. `PlanRunner` executes ready tasks through injected domain handlers, publishes only the declared outputs, and requires a verifier distinct from the producer. A changed upstream result invalidates descendants while unrelated completed tasks remain reusable.

The runner stops before dispatch when the required closure cannot fit the remaining deadline. Outputs must come from one successful owned task attempt, and independent verification binds those exact output references with passed checks. When a plan requires human release, the Principal's decision note must bind the exact plan, required result references, and output references; an unrelated approval artifact cannot transfer.

## On-demand Operations acquisition

`CapabilityAcquirer` turns a task requirement into one allowlisted recipe. Selection binds adapter, functional tags, data classification, and permitted recipe IDs. The official preview MCP Registry API can provide a current discovery record for exact server name and version matching. Registry data is discovery evidence only.

Local tools are reused from explicit profiles. Python packages may be provisioned only from local wheel files whose SHA-256 hashes match the recipe; installation runs with `--no-deps` inside the project-owned Operations workspace. Every route then passes the existing Operations representative probe and an independent verifier before the project receives a reusable binding. Readiness does not establish scientific validity.

## Judgment evaluation

`prepare-evaluation` exports a packet containing case IDs, task types, and evidence without labels or adjudication rationales. `score-evaluation` accepts exactly one prediction for every case and requires the frozen corpus hash. It reports accuracy, coverage, per-task results, and the rate at which a system makes a decisive gap claim on expert-labeled insufficient-evidence cases.

```bash
python3 -m scisaurus.cli prepare-evaluation --corpus held-out.json --output blind.json
python3 -m scisaurus.cli score-evaluation --corpus held-out.json \
  --predictions predictions.json --output evaluation.json
```

Development corpora always return `development_only`. Only an expert-adjudicated `held_out` corpus can return `passed`. No expert corpus ships in this repository, so the implementation is tested but research-level judgment accuracy is not claimed.

## Paper release candidate

`PaperReleaseBuilder` accepts a `paper-release-score-1` configuration. It checks the current accepted survey and gap assessment through `SurveyGate`, the accepted document manifest and its integrated verification, a `results-package-1`, paragraph-level claim/evidence bindings, exact result phrases, and citation keys backed by accepted survey sources. For a v3 survey, literature evidence must reproduce its pinned character span and quote SHA-256. A DOI-bearing reference must name a `verified` OpenAlex/Crossref identity record whose DOI, title, year, and source work match; conflict or missing metadata blocks the release candidate. A research-paper build is blocked unless the accepted gap state is `eligible_for_experiment`.

The build emits:

- `output/source/main.tex`
- `output/source/references.bib`
- `output/source/claim-index.json`
- `output/pdf/<paper_id>.pdf`
- `output/rendered/page-*.png`
- `output/visual-review.json`
- `output/release-manifest.json`

```bash
python3 -m scisaurus.cli build-paper /path/to/new-release \
  --config /path/to/paper-score.json
```

The manifest status is `needs_principal_approval`; external submission is excluded. `scripts/build-paper-pipeline-demo.py` constructs a deterministic validation evidence chain and runs the real LaTeX/PDF path. Its document explicitly limits every statement to software-path validation.

The v3 integration path also produced `paper-claim-index-2` from a current accepted public survey. The index retains the exact source character range and quote hash plus the verified bibliographic identity reference. The rendered validation candidate is under `.runs/resnet-v3-paper-release-verified-20260911/`; it remains a software validation artifact with status `needs_principal_approval`.

## Verification boundary

The unit and integration suite covers selective reuse, configuration and source drift, unknown-outcome accounting, optional-task deadline closures, capability selection and real local-program invocation, label blinding, decisive false positives, exact claim/evidence binding, and release prerequisites. The real render integration compiles with the bundled Tectonic runner and uses `pdfinfo` plus `pdftoppm` to render every page. A rendered PDF still requires human visual approval for final release.

One interrupted external ten-work survey was continued from retained artifacts through bounded resume windows. It accepted a current survey, executed targeted counter-search, and accepted a `refuted_by_prior_work` assessment against verified Transformer full text. The live path also demonstrated conservative unknown-call accounting, scoped semantic patches, post-checkpoint relationship recovery, exact enum repair feedback, and a no-work replay that labels its incumbent `retained_from_prior_run`. This is one control-path case, not an efficacy benchmark.
