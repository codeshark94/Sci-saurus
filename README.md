# Sci-saurus

**A compute-rich research organization that turns sustained reasoning into evidence-backed progress under human direction.**

Sci-saurus explores competing approaches, investigates uncertainty, constructs artifacts, and independently checks what deserves to be retained. A reasoning supervisor continually redirects work toward the mission; a deterministic control plane enforces authority, provenance, and execution limits.

Deliverables have stable paragraph/structure identities and pinned evidence. Workers make purpose-scoped change proposals; verified integration preserves unaffected material. Active web retrieval and API/tool adapters supply external evidence throughout the workflow.

Each research project has its own organization and environment. An on-demand Operations Cell sets up, connects, runs, and verifies useful open-source programs and MCP/API services on actual project inputs.

The first application is a supplied results package and rough storyline becoming a defensible English manuscript, LaTeX sources, and a visually checked PDF. The organizing model supports other research deliverables through mission-specific contracts.

## Design entry points

1. [System concept](docs/05-system-concept.md): the organizing model, compute policy, progress over time, and first implementation.
2. [Concept SSOT](docs/00-SSOT.md): governing intent, decisions, authority, and scope.
3. [Architecture](docs/20-architecture-v0.md): responsibilities, execution, evidence, storage, and release.
4. [Execution contract](docs/40-execution-contract.md): shared records and lifecycle invariants.
5. [Roadmap](docs/30-roadmap.md): implementation order and acceptance scenarios.
6. [Structured artifact changes](docs/45-artifact-change-control.md): unit versions, scoped edits, references, and atomic integration.
7. [Web intelligence integration](docs/50-web-intelligence-integration.md): active search and API/plugin implementation contracts.
8. [Project organization and operations](docs/15-project-organization.md): per-project scope, conditional team activation, and actual program execution.
9. [P0 freeze record](docs/25-p0-freeze.md): frozen principles, the selected first fixture (`fixtures/p1-slice/`), and the deployment-configuration template (`config/deployment-template.yaml`).

[Search campaign](docs/web-search-campaign.yaml) is an illustrative configuration, not executable software or provider authorization.

## Current state

This repository contains design documents, the P0 freeze record, the selected P1 fixture set, and a deployment-configuration template. It does not yet contain an orchestrator, model adapter, runnable paper workflow, or benchmark result. External GPU capacity is a design premise; endpoints, models, measured capacity, and data permissions remain deployment configuration.
