<div align="center">

# 🦖 Sci-saurus

**A compute-rich research organization that turns sustained reasoning into evidence-backed progress under human direction.**

[![tests](https://github.com/codeshark94/Sci-saurus/actions/workflows/tests.yml/badge.svg)](https://github.com/codeshark94/Sci-saurus/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)
![phase](https://img.shields.io/badge/phase-P1%20durable%20core-F7A41D)
![license](https://img.shields.io/badge/license-proprietary-lightgrey)

</div>

---

## What it is

Sci-saurus is a **multi-department research organization** implemented as a multi-agent control plane. It exists to realize a human Principal's intent: investigation, alternative construction, synthesis, criticism, verification, and replanning are complementary reasoning activities — organized into departments with explicit authority, not an endless chat.

> **Flagship mission:** a user-supplied results package + rough storyline → broad external intelligence gathering → defensible argument → English manuscript → LaTeX source + rendered PDF.

The central design hypothesis: **compute → verified progress** toward the Principal's goal. Extra messages and versions are not progress; only independently verified improvements and decision-relevant information gains count.

## The organization

| Organ | Role |
|---|---|
| **Principal** (human) | Defines outcomes, prohibitions, trade-offs, delegation; approves releases |
| **Executive Command** | Intent Keeper · Composer · Arbiter · Progress Controller |
| **Research & Intelligence** | Evidence acquisition authority; search campaigns; source capture |
| **Strategy & Writing** | Claim/evidence argument, outline, section drafting |
| **Methods & Validation** | Whether conclusions follow from the supplied methods/results |
| **Editorial Office** | Accuracy, structure, LaTeX assembly, rendering |
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
| Operations Cell orchestration | Dynamic capability discovery, environment selection, operator activation | 🔜 |
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

## Design entry points

1. [System concept](docs/05-system-concept.md) — organizing model, compute policy, anytime progress
2. [Concept SSOT](docs/00-SSOT.md) — governing decisions D1–D36, authority, terminology
3. [Architecture](docs/20-architecture-v0.md) — responsibilities, execution, evidence, storage, gates
4. [Execution contract](docs/40-execution-contract.md) — exact identifiers, lifecycle rules, schema contracts
5. [P0 freeze record](docs/25-p0-freeze.md) — frozen principles, first fixture, deployment template
6. [Project organization](docs/15-project-organization.md) — per-project scope, Operations Cell, real execution
7. [Structured artifact changes](docs/45-artifact-change-control.md) — surgical revision contracts
8. [Web intelligence integration](docs/50-web-intelligence-integration.md) — active search and adapters
9. [Roadmap](docs/30-roadmap.md) — phases P0–P6 and acceptance tests T01–T80
10. [Search campaign](docs/web-search-campaign.yaml) — illustrative configuration

## Current state

Design documents, the P0 freeze record, the P1 fixture set, and a deployment-configuration template — plus the `scisaurus` control-plane slices listed above. A bounded paragraph runner and external-model protocol adapters are present, with live Crossref/MCP retrieval. **Not yet present:** a general autonomous scheduler, dynamic Operations Cell orchestration, a runnable paper workflow, or research-quality benchmarks. A synthetic paragraph has completed external GPU production, independent verification, and conditional adoption; see the [live validation scope](docs/60-paragraph-runtime.md#live-validation). External GPU capacity is a design premise; endpoints, models, measured capacity, and data permissions remain deployment configuration.
