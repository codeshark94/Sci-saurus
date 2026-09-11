<div align="center">

# 🦖 Sci-saurus

**A general-purpose project organization that turns sustained reasoning into evidence-backed progress under human direction.**

[![tests](https://github.com/codeshark94/Sci-saurus/actions/workflows/tests.yml/badge.svg)](https://github.com/codeshark94/Sci-saurus/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)
![phase](https://img.shields.io/badge/phase-P1%20durable%20core-F7A41D)
![license](https://img.shields.io/badge/license-proprietary-lightgrey)

</div>

---

## What it is

Sci-saurus is a **general-purpose, multi-department project organization** implemented as a multi-agent control plane. It pursues a human Principal's goal through investigation, alternative construction, synthesis, criticism, verification, and replanning. Each project declares its deliverable, acceptance requirements, tool capabilities, and time policy in a versioned Score.

> **Flagship mission:** a user-supplied results package + rough storyline → broad external intelligence gathering → defensible argument → English manuscript → LaTeX source + rendered PDF.

The same artifact and execution services also support operational configurations, guides, reports, and Python source units. Academic retrieval and paper-specific checks apply when the mission selects them.

The central design hypothesis: **compute → verified progress** toward the Principal's goal. Extra messages and versions are not progress; only independently verified improvements and decision-relevant information gains count.

## The organization

| Organ | Role |
|---|---|
| **Principal** (human) | Defines outcomes, prohibitions, trade-offs, delegation; approves releases |
| **Executive Command** | Intent Keeper · Composer · Arbiter · Progress Controller |
| **Research & Intelligence** | Evidence acquisition authority; search campaigns; source capture |
| **Strategy & Writing** | Plan and compose the requested artifact from requirements and evidence |
| **Methods & Validation** | Check exact outputs, supporting evidence, and mission-specific acceptance requirements |
| **Editorial Office** | Accuracy, structure, assembly, and delivery; LaTeX/PDF for the paper mission |
| **Operations Cell** (on demand) | Makes required open-source programs / APIs / MCP services actually work |
| **Archivist** (cross-cutting) | Immutable artifacts, event ledger, releases — provenance is a first-class citizen |

Every department carries a designated **Adversarial Reviewer** with an independently reserved retrieval allowance: produce → blind challenge → respond/rebut → adjudicate → verify → select.

## What is implemented (P1, in progress)

| Slice | Contents | Status |
|---|---|---|
| **Durable core** | Transactional control store (SQLite) + event hash chain · immutable content-addressed artifacts + adoption CAS · message bus (outbox, leases, fencing, idempotent effects) · task/attempt lifecycle | ✅ `T01–T05, T13` |
| **Structured change** | Stable content units · document manifests · scoped EditGrants · ChangeSets with complete-mutation validation (scope, preimages, protected spans, citation anchors) | ✅ `T56–T58` |
| **Review & issues** | Admissible critiques · stable issue identity · rebuttal · independent adjudication · verification closure · review gate that never converts failure into a pass | ✅ `T06–T10` |
| **Capacity & checkpoints** | Renewable allocation windows, finite reservations, causal stagnation tracking, wall-clock progress checkpoints | ✅ `T41–T45, T52` |
| **Paragraph runtime** | Bounded supervisor → writer → independent verifier → conditional adoption; exact staged candidates, reservations, native checkpoints, cancellation and retained failure evidence | Live synthetic-paragraph validation passed |
| **Live retrieval** | Crossref bibliographic search and identity checks + official MCP Fetch execution, pinned setup, source captures and hashes | Live search and source capture verified |
| **Project runtime** | Parallel paragraph proposals, per-unit claims/literals, independent unit and document checks, atomic integration | Live synthetic-project validation passed |
| **Bounded Scores** | Same project runner for text, JSON, and Python code units; declared capabilities, exact candidate program checks, configurable exports | Implemented; non-paper policy fixture and real checker tests |
| **Time policy** | First verified result target, completion target, hard deadline, observed duration updates, and reserved review time | Implemented; seeds remain planning estimates |
| **Operations Cell** | Project-bound profiles, real probes, independent readiness checks, drift invalidation, and declared workloads | Crossref, OpenAlex, official MCP Fetch, and fixed local-program adapters |
| **Bounded literature survey** | OpenAlex discovery, optional Crossref identity reconciliation, configured MCP full text, stable source spans, immutable per-work maps, independently checked survey acceptance and gap assessment | Implemented; local regressions and one live v3 identity/span path verified |
| **Multimodal visual review** | Hash-pinned PNG/JPEG inputs, independent visual perspectives, exact outcome reconciliation, scoped repair actions, and independent image-grounded verification | Implemented; live rendered-page assessment and bounded bibliography repair verified |
| **Scientific experiment execution** | Frozen method/seed/stopping contract, separate pinned executor and calculator, exact replay, raw observations, figures, model claim review, and generated results package | Implemented; bounded robust-mean pilot completed and independently reviewed |
| **Durable resume and plans** | Source/config comparison, unknown-call reconciliation, selective reuse, dependency-aware deadline replanning, executable task graphs | Implemented and regression-tested |
| **On-demand acquisition** | Allowlists, official MCP registry discovery, pinned local-wheel provisioning, representative execution, independent binding verification | Implemented; local-program integration verified |
| **Judgment evaluation** | Frozen corpus hash, label-free inference packet, per-task accuracy/coverage, decisive false-positive gate | Implemented; expert-held-out corpus not yet supplied |
| **Paper release candidate** | Frozen storyline + accepted survey + results + reviewed structured manuscript → relational claim index, bibliography, figures, LaTeX, rendered PDF, release manifest | Implemented; real deterministic PDF path verified |

The CI workflow runs the acceptance and regression suite. Its cases include stale grants, subtree authorization, composed edits, exact verification evidence, concurrent reservations, renewal rollback, and restart recovery. See [implemented API contracts](docs/55-control-plane-api.md) for supported behavior and upgrade boundaries.

## Quickstart

```bash
# initialize a project control store
python3 -m scisaurus.cli init /tmp/my-project

# publish an artifact (immutable, versioned, content-addressed)
python3 -m scisaurus.cli publish /tmp/my-project strategy/notes/demo brief.md --type note

# verify the event chain
python3 -m scisaurus.cli verify /tmp/my-project
```

Run the acceptance suite:

```bash
python3 -m unittest discover -s scisaurus/tests -t . -v
```

## Run a paragraph revision

Install the official MCP server and its pinned source extractor:

```bash
sh scripts/setup-runtime.sh
```

Configure a copy of [the paragraph run template](config/paragraph-run.example.json) with the authorized GPU endpoint, model, and public inputs, then run from the repository root:

```bash
python3 -m scisaurus.cli run-paragraph /tmp/research-paragraph --config /path/to/run.json
```

The template blocks dispatch until configured. Results, retained candidates, source records, and checkpoints live in the project directory; `output/report.md` and `output/run.json` expose the outcome. See [runtime setup and limits](docs/60-paragraph-runtime.md).

## Run a multi-paragraph project

Prepare a configuration against the installed runtime, then set the authorized model endpoint, model name, optional credential environment-variable name, and `live_dispatch_allowed`:

```bash
python3 scripts/prepare-project-config.py --output /tmp/project-run.json
python3 -m scisaurus.cli run-project /tmp/research-project --config /tmp/project-run.json
```

The project runner gives each producer one paragraph, composes immutable proposals centrally, and requires both independent paragraph reviews and a review of the exact combined document. It activates and verifies the configured API/MCP capabilities before research work. See [project execution and setup](docs/65-project-runtime.md).

## Run a non-paper project

After installing the repository runtime with `sh scripts/setup-runtime.sh`, prepare the operating-policy example:

```bash
python3 scripts/prepare-operations-config.py --output /tmp/operations-run.json
```

Set the authorized model connection and `live_dispatch_allowed` in that file. The helper records the installed checker and dependency identities and leaves dispatch disabled. Then use a new project directory:

```bash
python3 -m scisaurus.cli run-project /tmp/operating-policy \
  --config /tmp/operations-run.json \
  --first-result-seconds 300 --target-seconds 450 --deadline-seconds 600
```

The example revises `policy.json` and `operations-guide.md`, executes an offline JSON-schema checker on the exact candidate, and requires independent unit and combined-artifact review. It selects no academic or network tool workloads; configured model calls remain separate. Outputs and truthful timing state appear under `output/`. A retained baseline is distinguishable from a newly verified result. See [Score configuration, time policy, and acceptance boundaries](docs/70-scored-project-runtime.md).

## Run a bounded literature survey

After installing the repository runtime, prepare the public-literature example:

```bash
python3 scripts/prepare-survey-config.py --output /tmp/survey-run.json
```

The helper verifies the installed MCP Fetch runtime and records its package and extractor identity files. It refuses to overwrite an existing configuration and leaves model settings unset and dispatch disabled. Set the authorized model connection and `live_dispatch_allowed` in the prepared file, then use a new project directory:

```bash
python3 -m scisaurus.cli run-survey /tmp/attention-survey \
  --config /tmp/survey-run.json \
  --first-result-seconds 480 --target-seconds 720 --deadline-seconds 900
```

The runner obtains independently planned topic queries, captures OpenAlex records and bounded citation expansion, optionally reconciles DOI-bearing works against Crossref, and reads configured full-text URLs through MCP Fetch. Reconciliation preserves provider observations and field-level title/year/DOI conflicts; metadata never becomes claim evidence. Parallel mapping tasks each own one work and its outgoing relationships. New or changed evidence reopens affected assignments; valid results are retained while only assignments rejected by validation are retried. The revision 5 example permits ten mapping or focused-review workers within an eleven-call capacity and reserves one work slot for targeted counter-search. The control plane binds every unique model quotation to the immutable source reference, exact character range, and quote SHA-256 before it can enter a `literature-survey-3` bundle. It may restore a whitespace-only rendering difference to the unique source substring, but any changed non-whitespace character still fails. Failed assignments receive exact JSON locations and their prior response on an exact-input resume, allowing a scoped repair without regenerating accepted work. Each work and outgoing relationship must pass a separate independent review; failed checks permit only named field or relationship repairs, and a global pass cannot override them. The runner then accepts an independently reviewed survey and challenges a configured or newly nominated gap. Decisive comparisons require exact spans from verified full text for the compared work; gap dispatch and commitment require the current accepted survey. Counter-search and assessment also bind the exact nomination, so changing the candidate invalidates its prior verdict independently of the survey.

Interrupted survey runs can be reopened with `resume-survey`. Recovery requires the original configuration, an explicit additional deadline, conservative reconciliation of unknown calls, and named scopes when the executed source changed. Provider workload reservations are committed before dispatch, so failed or interrupted API calls remain inside the cumulative `max_api_calls` bound after recovery. Captured queries and source material are retained; only invalidated interpretation and review stages run again.

`output/survey.md`, `output/works.json`, `output/bibliographic-identities.json`, `output/coverage.json`, and `output/run.json` expose the outcome. The literature map and accepted gap assessment are exported when available. A completed assessment can refute the gap or retain insufficient evidence. Searches read one finite page per query, metadata remains provider-reported unless a separate identity record verifies it, and full-text routes are explicitly configured. Local integration tests establish these workflow boundaries. The external 10-work pilot was resumed from its retained captures and partial reviews, accepted a current survey, ran targeted counter-search, and concluded `refuted_by_prior_work` from verified Transformer full text. This is live recovery evidence for one case, not a held-out research-accuracy result. See [survey execution, exports, and limits](docs/75-literature-survey-score.md).

For figures, rendered pages, and aesthetic concept comparisons, copy [the visual-review template](config/visual-review.example.json) and run `python -m scisaurus.cli run-visual-review PROJECT_DIR --config CONFIG.json`. The runtime sends hash-pinned PNG/JPEG assets to an explicitly configured OpenAI-compatible multimodal model, retains independent perspectives, retries only malformed judgments, mechanically preserves each perspective outcome, and accepts a synthesized assessment only after a fresh image-grounded verification. See [multimodal visual review](docs/85-multimodal-visual-review.md).

For an authorized computational study, install the isolated numerical runtime with `sh scripts/setup-experiment-runtime.sh`, prepare [the experiment template](config/experiment-run.example.json) with `python3 scripts/prepare-experiment-config.py --output /tmp/experiment.json`, then run `python3 -m scisaurus.cli run-experiment PROJECT_DIR --config /tmp/experiment.json`. The command runs the frozen program twice, requires byte-equivalent structured output and asset hashes, invokes a separate pinned calculator, sends result figures to independent scoped reviewers, and adopts only a provenance-complete `results-package-2`. See [scientific experiment execution](docs/90-experiment-runtime.md).

## Design entry points

1. [System concept](docs/05-system-concept.md) — organizing model, compute policy, anytime progress
2. [Concept SSOT](docs/00-SSOT.md) — governing decisions D1–D47, authority, terminology
3. [Architecture](docs/20-architecture-v0.md) — responsibilities, execution, evidence, storage, gates
4. [Execution contract](docs/40-execution-contract.md) — exact identifiers, lifecycle rules, schema contracts
5. [P0 freeze record](docs/25-p0-freeze.md) — frozen principles, first fixture, deployment template
6. [Project organization](docs/15-project-organization.md) — per-project scope, Operations Cell, real execution
7. [Structured artifact changes](docs/45-artifact-change-control.md) — surgical revision contracts
8. [Multimodal visual review](docs/85-multimodal-visual-review.md) — figures, rendered pages, concepts, and scoped visual repair
8. [Web intelligence integration](docs/50-web-intelligence-integration.md) — active search and adapters
9. [Roadmap](docs/30-roadmap.md) — phases P0–P6 and acceptance scenarios
10. [Search campaign](docs/web-search-campaign.yaml) — illustrative configuration
11. [Scored project runtime](docs/70-scored-project-runtime.md) — generic artifacts, real program checks, and time planning
12. [Literature Survey Score](docs/75-literature-survey-score.md) — bounded discovery, per-work maps, and independent gap assessment
13. [Completion runtime](docs/80-completion-runtime.md) — resume, executable plans, tool acquisition, blinded evaluation, and paper/PDF release candidates
14. [Scientific experiment runtime](docs/90-experiment-runtime.md) — frozen methods, replay, independent recalculation, result review, and generated result packages

## Current state

The durable control plane and shared execution services implement scoped artifact revision, independent review, declared and acquired operational capabilities, bounded Score/time contracts, recovery, executable dependency plans, blinded judgment evaluation, and a complete paper release-candidate builder. The non-paper fixture has a real local JSON-schema program. The literature runner adds a finite survey and gap-assessment workflow with local transport, evidence, currentness, and restart tests. A paper Score now freezes its thesis and ordered storyline before prose and requires every beat to appear in its assigned unit with supporting or qualifying evidence. These establish program and workflow behavior; each complete live scientific run and expert evaluation needs its own evidence.

External GPU production and verification completed synthetic paragraph and multi-paragraph project runs; the latter used six model calls and six real retrieval operations, passed 15 checks, and adopted the exact combined candidate in 141 seconds. The interrupted literature pilot also completed through explicit resume, with a current accepted survey and refuted gap assessment. A separate ResNet run exercised `literature-survey-3`, exact Crossref DOI filtering, nine distinct stable source ranges, assessment-only resume, and a `paper-claim-index-2` PDF candidate.

The first complete validation-report mission used OpenAlex and MCP Fetch to accept a five-work survey while retaining `insufficient_evidence`, executed 10,000 clean and 10,000 contaminated robust-mean replicates twice, recalculated every primary metric with a distinct program, and accepted two scoped multimodal result reviews. Six model calls then produced and independently reviewed only the two editable manuscript units around frozen literature, method, results, and limitations. A storyline-first release assembled a two-page PDF, and a separate five-call rendered-page assessment accepted all five visual criteria. The experiment, structured manuscript, and visual assessment completed in 43.05, 154.55, and 111.60 seconds respectively. These observations remain case-specific; the experiment is a finite seeded simulation and the report makes no novelty claim.

The remaining scientific priorities are an expert-adjudicated held-out corpus, alternate full-text discovery, repeated complete live paper missions, and measured quality-versus-compute comparisons. Deployment, shared multi-project capacity accounting, and external submission also remain outside this slice. See the [implemented completion contracts and limits](docs/80-completion-runtime.md).
