<div align="center">

# 🦖 Sci-saurus

**A compute-rich research organization that turns sustained reasoning into evidence-backed progress under human direction.**

[![tests](https://github.com/codeshark94/Sci-saurus/actions/workflows/tests.yml/badge.svg)](https://github.com/codeshark94/Sci-saurus/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)
![phase](https://img.shields.io/badge/phase-P1%20durable%20core-F7A41D)
![tests](https://img.shields.io/badge/acceptance%20tests-28%2F28-2EA043)
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
| Scheduler / capacity windows / progress checkpoints | Renewable allocation windows, stagnation diagnosis, wall-clock checkpoints | 🔜 next |
| Retrieval adapters & Operations Cell | Capability registry, provider adapters, execution evidence | 🔜 |
| Paper workflow MVP | Four-department run over the fixture → LaTeX + PDF | 🔜 |

**28/28 acceptance tests passing** (`python3 -m unittest discover -s scisaurus/tests`).

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

Design documents, the P0 freeze record, the P1 fixture set, and a deployment-configuration template — plus the `scisaurus` control-plane slices listed above. **Not yet present:** a scheduler/runner, model adapter, retrieval adapters, a runnable paper workflow, or benchmark results. External GPU capacity is a design premise; endpoints, models, measured capacity, and data permissions remain deployment configuration.