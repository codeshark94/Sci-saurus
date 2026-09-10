# Sci-saurus — Concept SSOT (Single Source of Truth)

> This document is the **single source of truth** for Sci-saurus. All other documents (`10-`, `20-`, `30-`) are **derived** from it; on conflict, this document prevails.
> Rules: (1) changes to concept/principles/terminology happen here first; (2) when a decision changes, a new versioned decision (D-x) is added — previous decisions are never deleted (records accumulate).

Doc version: v0.4 · Status: under review (D9–D12 decisions applied; unified in English per D12)

---

## 1. One-line Definition

**Sci-saurus** is a general-purpose research-production multi-agent system organized as a **multi-department organization** — "Research & Intelligence · Strategy & Writing · Editorial Office". Its flagship task today is *research data + a rough storyline → prior-work survey → paper draft*, but every department is a **general-purpose functional unit** not bound to papers: with a Composer's custom "score", the organization can pursue any production goal. All artifacts, decisions, and inter-department exchanges are **rigorously version-controlled and archived per project**.

## 2. Vision Roadmap (Anything → Anything)

| Phase | Capability | Description |
|---|---|---|
| **Now (v1)** | Paper-writing pipeline | Brief (research data + rough storyline) → Research & Intelligence (prior work · genealogy · trends) → Strategy (storyline · feasibility · outline) → draft → Editorial (format · structure) → release |
| **Next (v1.5)** | General-purpose goal switching | Composer swaps "score" presets to change deliverable type (research report, proposal, review article…). Department code is reused without modification |
| **Later (v2+)** | Resource awareness & execution | The organization discovers its own resources (this computer, available tools), finds, installs, and runs open-source tools needed for the goal. Strategy extends to experiment design. Ultimately **anything → anything** |
| **Endgame (v3)** | Autonomous organization operation | Cross-project org reuse, self-retrospective process improvement, collaboration with external agents (A2A/MCP) |

## 3. Core Decisions (Design Decisions — agreed)

- **D1 — Multi-department organization metaphor.** The system is an organization. Standing departments: Research & Intelligence / Strategy & Writing / Editorial Office. Standing cross-cutting organ: the Archivist (version control & storage). Standing conductor: the Composer.
- **D2 — Departments are general-purpose functional units.** The Research department does not do "paper research"; it does *"survey of the world for a given goal"*. Papers are just one mission among many. Same for Strategy and Editorial.
- **D3 — Free exchange between departments + full recording.** Any department may at any time send opinions, data, requests, or objections to any other (asynchronous). However, **every exchange is recorded as a message envelope**, and shared data moves only through the blackboard (project workspace). Free, yet nothing untraceable.
- **D4 — Departments are internally multi-agent.** Each department = one chief + multiple specialist agents. Chiefs decompose work and judge quality; specialists execute.
- **D5 — Rigorous per-project version control.** Project = one repository. Every artifact is stored as an **immutable version + provenance (who, when, based on what)** and tagged at milestones. Overwriting does not exist — only new versions.
- **D6 — The Composer defines purpose via swappable "scores".** A score = a declarative definition of pipeline, quality gates, and department invocation order per deliverable type. The default score is `paper` (academic paper); user-defined scores can change the purpose freely.
- **D7 — Initial pipeline (user request).** Input: research data + rough storyline. Processing: prior-work survey → storyline concretization & feasibility review → outline → draft → editing. Output: draft + research archive + decision record.
- **D8 — Long-term: resource-aware organization.** Research/Strategy departments become aware of the execution environment (local computer, available tools, open-source ecosystem) and can discover, combine, and run tools to achieve goals (opt-in capabilities, behind gates).
- **D9 — Hybrid execution harness.** The core (store, ledger, message bus, score, gates) is implemented natively as a standalone Python orchestrator; department/agent **runners sit behind an adapter interface**, so LangGraph, AutoGen, etc. can be mixed **per department** where useful (frameworks are adopted piecemeal, not all-or-nothing).
- **D10 — Deliverables down to rendered output.** The paper score's final deliverable is LaTeX source + compiled PDF. The Editorial Format Editor maintains venue templates (.cls/.sty) and citation styles; **successful compilation and zero reference errors** are release-gate conditions.
- **D11 — Paper language policy.** The final paper (writing · rendering) is **English by default**. Briefs, internal collaboration notes, and release summaries may be Korean. Language is declared in the score; overrides (e.g., Korean paper with a kotex compile chain) are allowed.
- **D12 — English-first development (supersedes the "internal docs may be Korean" clause of D11).** All development artifacts are unified in **English**: design documents, source code, comments, commit messages, CLI output, schema field names, identifiers, and inter-agent message bodies (default; score-configurable). Human-supplied inputs (briefs) may remain in any language.

## 4. Organization Summary (details in `20-architecture-v0.md`)

| Organ | Nature | One-line duty | Internal composition (summary) |
|---|---|---|---|
| **Composer** (conductor) | Orchestration | Turns briefs into missions, conducts departments, arbitrates, declares releases | Mission designer, arbiter, release steward |
| **Research & Intelligence** (Dept) | Producing dept | Surveys the world for the goal: collects and systematizes references, genealogy, trends | Research Chief, Literature Scout, Genealogy & Trend Analyst, Cataloger, Fact Verifier |
| **Strategy & Writing** (Dept) | Producing dept | Converts research data into goal-achieving storylines, stress-tests feasibility, writes the draft | Strategy Chief, Narrative Architect, Feasibility Red Team, Planner, Section Writers |
| **Editorial Office** (Dept) | Review/quality | Rigorously reviews format, aesthetic structure, logical consistency; judges publication readiness | Editor-in-Chief, Format Editor, Structural Editor, Consistency QA |
| **Archivist** (cross-cutting service) | Service | Stores, tags, and audits every artifact, message, and event in immutable versions | Storage engine, ledger, release/tag management |

## 5. Glossary — SSOT body

| Term | Definition |
|---|---|
| **Project** | Container for all artifacts, records, and versions of one goal. Owns a unique id and a repository (folder + git) |
| **Brief** | The user's initial input (preserved verbatim). E.g., research data + rough storyline |
| **Mission** | The executable goal statement the Composer derives from a brief (objective, constraints, success criteria, deliverable spec) |
| **Score** | A Composer preset: declarative definition of the pipeline (stages, owning departments, gates) per deliverable type. Default: `paper` |
| **Department** | A general-purpose functional unit; internally multi-agent; receives missions |
| **Agent** | A role unit inside a department, defined by manifest (name, role, permissions, tools, model profile) |
| **Blackboard** | The shared project workspace (files). Readable by all; writable only for one's own artifacts |
| **Artifact** | A versioned output unit (research note, reference card, storyline, outline, section draft, review, report, …) |
| **Message** | The inter-department envelope. Types: `request` / `review` / `data` / `critique` / `decision` |
| **Ledger** | Append-only event log of every action (artifact creation, messages, gate verdicts) |
| **Release** | A milestone snapshot tag, e.g., `v0.1-outline`, `v0.2-draft`, `v1.0-final` |
| **Gate** | A quality condition permitting stage progress; failure routes to dispute → arbitration |
| **Arbitration** | The Composer's evidence-based decision in inter-department deadlock/dispute (always recorded) |

Korean↔English term map (for conversation continuity): 부서=Department · 조사부=Research & Intelligence · 전략부=Strategy & Writing · 편집실=Editorial Office · 기록부=Archivist · 지휘자=Composer · 브리프=Brief · 미션=Mission · 악보=Score · 블랙보드=Blackboard · 아티팩트=Artifact · 원장=Ledger · 릴리스=Release · 게이트=Gate · 중재=Arbitration.

## 6. Data Model Summary (schemas in `20-architecture-v0.md` §5)

Core entities (7): `Project`, `Mission`, `Task`, `Artifact` (immutable versions), `Message`, `Event` (ledger), `Release`.
Immutability rule: artifact bodies are immutable once stored → edits are always a **new version + parent link**. The ledger is append-only.

## 7. Artifact & Versioning Rules (summary)

1. One project folder = one repository. Standard layout in architecture doc §6.
2. Every artifact: frontmatter (id, type, version, parents, author_role, status, checksum, refs) + body.
3. Release tags at milestones (brief accepted / survey done / outline approved / draft / editorial pass / final).
4. The user can inspect, diff, and roll back project outputs at any time.
5. Citations without evidence are blocked at gates (hallucination defense line).

## 8. Non-Goals (v1)

- Running experiment code or performing real data analysis (design slots only; opened in v2).
- Publishing under human authorship in place of humans — transparent AI-generated-content attribution is maintained.
- Single-vendor framework lock-in (department runners must remain replaceable).
- UI polish (streaming UI etc.) — core and storage layer first.

## 9. Open Questions (user decisions pending)

| # | Question | Default proposal |
|---|---|---|
| Q1 | ~~v1 execution harness~~ | ✅ **Decided (D9)**: hybrid — native Python core; department runners as adapters (LangGraph/AutoGen mixable) |
| Q2 | ~~First target deliverable format~~ | ✅ **Decided (D10, D11)**: paper = English LaTeX source + compiled PDF |
| Q3 | Search/tool access scope (which web-search API? citation metadata sources: arXiv/CrossRef/S2?) | arXiv + Semantic Scholar + web search combination |
| Q4 | Model placement (per-department model tiers) and per-project cost cap | Research = economical models, writing = high-capability models; caps per project |
| Q5 | Human-in-the-loop checkpoint locations (outline approval? post-draft?) | Two points: outline approval + before final release |

## 10. Document Map

- `00-SSOT.md` — this document; origin of concept, principles, terminology.
- `10-survey-precursors.md` — precedent systems & framework survey (with sources) and design implications.
- `20-architecture-v0.md` — v1 architecture concretization (organization, comms, schemas, pipeline, execution mapping).
- `30-roadmap.md` — implementation roadmap (P0–P4), risks, immediate next actions.

All project documentation is maintained in **English** per D12.

## 11. Change Log

| Version | Content |
|---|---|
| v0.1 | Initial draft: multi-department concept fixed, decisions D1–D8, glossary, open questions Q1–Q5 (written in Korean) |
| v0.2 | Q1·Q2 decided: D9 hybrid harness, D10 LaTeX-rendered deliverable |
| v0.3 | D11 language policy: paper written & rendered in English by default |
| v0.4 | D12: unify all development artifacts in English — design docs translated to English |