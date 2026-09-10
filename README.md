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
| **Live retrieval** | Crossref bibliographic search + official MCP Fetch execution, pinned setup, source captures and hashes | Live search and source capture verified |
| **Project runtime** | Parallel paragraph proposals, per-unit claims/literals, independent unit and document checks, atomic integration | Live synthetic-project validation passed |
| **Bounded Scores** | Same project runner for text, JSON, and Python code units; declared capabilities, exact candidate program checks, configurable exports | Implemented; non-paper policy fixture and real checker tests |
| **Time policy** | First verified result target, completion target, hard deadline, observed duration updates, and reserved review time | Implemented; seeds remain planning estimates |
| **Operations Cell** | Project-bound profiles, real probes, independent readiness checks, drift invalidation, and declared workloads | Crossref, OpenAlex, official MCP Fetch, and fixed local-program adapters |
| **Bounded literature survey** | OpenAlex discovery and citation expansion, configured MCP full text, immutable per-work maps, independently checked survey acceptance and gap assessment | Implemented; local integration, evidence, and currentness tests |
| Paper workflow MVP | Four-department run over the fixture → LaTeX + PDF | 🔜 |

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

The runner obtains independently planned topic queries, captures OpenAlex records and bounded citation expansion, and reads configured full-text URLs through MCP Fetch. Parallel mapping tasks each own one work and its outgoing relationships. New or changed evidence reopens affected assignments; valid results are retained while only assignments rejected by validation are retried. The revision 3 example permits ten mapping or focused-review workers within an eleven-call capacity. Each work and outgoing relationship must pass a separate independent review; failed checks permit only named field or relationship repairs, and a global pass cannot override them. The runner then accepts an independently reviewed survey and challenges a configured or newly nominated gap. Decisive comparisons require exact quotes from verified full text for the compared work; gap dispatch and commitment require the current accepted survey. Counter-search and assessment also bind the exact nomination, so changing the candidate invalidates its prior verdict independently of the survey.

`output/survey.md`, `output/works.json`, `output/coverage.json`, and `output/run.json` expose the outcome. The literature map and accepted gap assessment are exported when available. A completed assessment can refute the gap or retain insufficient evidence. Searches read one finite page per query, metadata remains provider-reported, and full-text routes are explicitly configured. Local integration tests establish these workflow boundaries. The external 10-work pilot retained analyses and seven focused reviews but stopped on three HTTP 502 failures without accepting a survey; a successful complete live survey and held-out research accuracy remain unverified. See [survey execution, exports, and limits](docs/75-literature-survey-score.md).

## Design entry points

1. [System concept](docs/05-system-concept.md) — organizing model, compute policy, anytime progress
2. [Concept SSOT](docs/00-SSOT.md) — governing decisions D1–D39, authority, terminology
3. [Architecture](docs/20-architecture-v0.md) — responsibilities, execution, evidence, storage, gates
4. [Execution contract](docs/40-execution-contract.md) — exact identifiers, lifecycle rules, schema contracts
5. [P0 freeze record](docs/25-p0-freeze.md) — frozen principles, first fixture, deployment template
6. [Project organization](docs/15-project-organization.md) — per-project scope, Operations Cell, real execution
7. [Structured artifact changes](docs/45-artifact-change-control.md) — surgical revision contracts
8. [Web intelligence integration](docs/50-web-intelligence-integration.md) — active search and adapters
9. [Roadmap](docs/30-roadmap.md) — phases P0–P6 and acceptance scenarios
10. [Search campaign](docs/web-search-campaign.yaml) — illustrative configuration
11. [Scored project runtime](docs/70-scored-project-runtime.md) — generic artifacts, real program checks, and time planning
12. [Literature Survey Score](docs/75-literature-survey-score.md) — bounded discovery, per-work maps, and independent gap assessment

## Current state

The durable control plane and shared execution services implement scoped artifact revision, independent review, declared operational capabilities, and bounded Score/time contracts. The non-paper fixture has a real local JSON-schema program and runtime preparation tests. The literature runner adds a finite survey and gap-assessment workflow with local transport, evidence, and currentness tests. These establish program and workflow behavior; each complete live Score run needs its own evidence.

External GPU production and verification previously completed synthetic paragraph and multi-paragraph project runs; the latter used six model calls and six real retrieval operations, passed 15 checks, and adopted the exact combined candidate in 141 seconds. That evidence remains scoped to the [paragraph](docs/60-paragraph-runtime.md#live-validation) and [multi-paragraph](docs/65-project-runtime.md) milestones. A general autonomous task graph, automatic tool installation, a complete paper workflow, deployment, shared multi-project capacity coordination, and research-quality benchmarks remain outside the implemented slice.

The next general-purpose milestones are durable resume and deadline replanning, executable project plans, and on-demand tool acquisition, followed by measured judgment evaluation and a complete paper Score. See the [implementation order](docs/30-roadmap.md#10-immediate-implementation-order).

The next survey priorities are held-out expert evaluation, alternate full-text discovery with stable source-span pointers and bibliographic identity reconciliation, resumable execution with preserved evidence and accounting, and integration into the complete paper pipeline. Current evidence uses exact captured quotes; stable span pointers remain future work. Existing run directories remain inspectable; `run-survey` starts a new run rather than resuming one.
