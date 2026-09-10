# Sci-saurus — v1 Architecture (Architecture v0)

> This document concretizes the decisions (D1–D12) of `00-SSOT.md` to an implementable level. Design background is in `10-survey-precursors.md`.

---

## 1. Overall Composition

```
                    ┌───────────────────────────────────────────────┐
                    │                Composer (conductor)           │
                    │  brief→mission · conducting · arbitration ·   │
                    │  release declaration                          │
                    └──────┬────────────────┬───────────────┬───────┘
                 score     │                │ gates/arbitration
        ┌──────────────────▼───┐   ┌────────▼──────┐   ┌────▼──────────────┐
        │  Research &          │   │   Strategy &  │   │  Editorial Office │
        │  Intelligence (Dept) │   │   Writing     │   │                   │
        │  Research Chief      │   │  Strategy     │   │  Editor-in-Chief  │
        │  ├─ Literature Scout │   │  Chief        │   │  ├─ Format Editor │
        │  ├─ Genealogy &      │◀─▶│  ├─ Narrative │◀─▶│  ├─ Structural    │
        │  │  Trend Analyst    │◀─▶│  │  Architect │◀─▶│  │  Editor        │
        │  ├─ Cataloger        │   │  ├─ Feas. Red │   │  └─ Consistency   │
        │  └─ Fact Verifier    │   │  │  Team      │   │     QA            │
        │                      │   │  ├─ Planner   │   │                   │
        │                      │   │  └─ Section   │   │                   │
        │                      │   │     Writers   │   │                   │
        └──────────┬───────────┘   └───────┬───────┘   └─────────┬─────────┘
                   │   all share and record through the layers below      │
                   └────────────────┬────────┴─────────────────────┘
                                    ▼
            ┌────────────────────────────────────┐     ┌───────────────────────────┐
            │  Blackboard = project workspace    │     │  Archivist (cross-cutting)│
            │  kb/ · strategy/ · draft/ · ...    │◀───▶│  immutable store · ledger │
            │                                    │     │  · releases               │
            └────────────────────────────────────┘     └───────────────────────────┘
```

## 2. Component Specifications

### 2.1 Composer (conductor)
| Item | Content |
|---|---|
| Duties | Parse brief → publish mission, load score, assign department tasks, enforce gates, arbitrate disputes, declare releases, answer user queries |
| In/Out | In: brief, department reports, gate results / Out: missions, tasks, decisions, releases |
| Permissions | Blackboard **read-all**; **write** only `releases/*`; no artifact creation elsewhere (tags/pointers only) |
| Principle | The Composer **does not produce knowledge**; it conducts the flow of knowledge. Every verdict cites evidence artifacts |

### 2.2 Research & Intelligence — general function: "survey the world and systematize it"
| Agent | Duty | Outputs |
|---|---|---|
| Research Chief (chief) | Decompose survey needs, prioritize, integrate & judge quality, receive cross-dept requests | Survey plan, survey report |
| Literature Scout (scout) | Run web/academic searches, shortlist candidates | Raw reference bundles |
| Genealogy & Trend Analyst (genealogist) | Citation networks, school genealogy, timeline, research-gap mapping | Genealogy map, gap notes |
| Cataloger (librarian) | Normalize reference metadata, dedupe, index the KB, produce snapshots | Reference cards, KB snapshots |
| Fact Verifier (verifier) | Cross-check sources, claim–evidence correspondence, contradiction detection | Verification report |

- Generality principle: tool lists (web search, arXiv, S2, local files, …) are injected per mission. The dept performs "the survey the mission needs" — not "paper research".
- The key output is the **KB snapshot** (`kb/snapshots/`): a frozen view of the knowledge base at survey time → Strategy and Editorial cite this snapshot as evidence (reproducibility).

### 2.3 Strategy & Writing — general function: "turn research data into goal-achieving logic, story, and text"
| Agent | Duty | Outputs |
|---|---|---|
| Strategy Chief (chief) | Owns strategy, allocates writing, judges quality | Strategy briefing |
| Narrative Architect | Contribution positioning against the KB, argument skeleton (claim→evidence chains) | Storyline document |
| Feasibility Red Team | Attacks claim–evidence links, hunts counterexamples, weakness report | Feasibility report |
| Planner | Outline, section specs, work decomposition | Outline |
| Section Writers (×N) | Write section drafts in parallel, insert citations from the KB only | Section drafts |

### 2.4 Editorial Office — general function: "review and polish outputs to publishable quality"
| Agent | Duty | Outputs |
|---|---|---|
| Editor-in-Chief (chief) | Final verdict, editorial policy, loop termination | Verdict note |
| Format Editor | Venue LaTeX templates (.cls/.sty), citation styles (bib), metadata conformance | Format report |
| Structural Editor | Section logic, flow, duplication, aesthetic structure | Structural review |
| Consistency QA | Cross-checks terminology, numbers, citations, references | QA report |

### 2.5 Archivist — cross-cutting service (not a department)
- Storage engine: immutable artifact store (content-addressed), git commits/tags.
- Ledger: append-only recording of all events (JSONL).
- Releases: milestone snapshots + change summaries + evidence index.
- Audit: "who · when · what · based on what" is reconstructable at any time.

## 3. Communication — 3 Layers (implements D3)

| Layer | Role | Rules |
|---|---|---|
| **1. Blackboard** (shared files) | Sharing of data & outputs | Readable by all. Write only for the owning department (§7 permission matrix). Direct file handoff is forbidden — pass artifact references |
| **2. Message bus** (envelope JSONL queues) | Async exchange of opinions, requests, reviews, objections | Any dept ↔ any dept (chief-routed preferred; direct when urgent). All recorded in the ledger |
| **3. Arbitration** (Composer) | Deadlock, disputes, budget, gate appeals | Unresolved chief-to-chief → escalation → Composer issues an evidence-based `decision` message |

### Message envelope schema (v0)
```json
{
  "msg_id": "m-0007",
  "type": "critique",                      // request | review | data | critique | decision
  "from": { "dept": "editorial", "agent": "structure" },
  "to":   { "dept": "strategy",  "agent": "chief" },
  "project": "p-001",
  "subject": "S2 cites S4's result before it exists — circular logic",
  "body": "…concrete evidence…",
  "refs": ["artifact:draft/section-2@3", "artifact:editorial/reviews/r-002@1"],
  "action": { "kind": "revise", "payload": { "sections": ["section-2"] } },
  "created_at": "2025-09-10T00:00:00Z",
  "priority": "normal",                    // low | normal | high | blocking
  "expires_at": null
}
```
Design note: `type/from/to/refs/action` field composition mirrors A2A's Task/Message concepts → cheap A2A adapter migration in v2. Message bodies are **English by default** (D12), score-configurable.

## 4. Storage Layer (implements D5)

### 4.1 Project repository layout
```
projects/<project_id>/
  brief.md                       # user's verbatim input (immutable)
  mission.json                   # Composer mission (versioned)
  org/manifest.json              # departments/agents/model placement for this project
  kb/
    references/                  # reference cards (per-item md + json metadata)
    notes/                       # survey notes, genealogy maps, gap notes
    snapshots/                   # KB snapshots (frozen at survey time)
  strategy/
    storyline.md
    feasibility/                 # feasibility reports
    outline.md
    sections/                    # section drafts
  editorial/
    reviews/                     # review documents
    reports/                     # format/QA reports
  releases/                      # release notes
  ledger/events.jsonl            # ledger (append-only)
```
- The whole folder = a git repository. **Commit = artifact version finalized**, **tag = release (annotated)**. Large files: DVC slot in v2.

### 4.2 Artifact frontmatter (YAML)
```yaml
id: strategy/sections/section-2
type: section_draft        # note|reference_card|kb_snapshot|storyline|feasibility_report|outline|section_draft|review|qa_report|decision_note|release_note
version: 3
parents: ["strategy/sections/section-2@2"]
author: { dept: strategy, agent: writer-1 }
status: draft              # draft | proposed | approved | superseded | rejected
inputs: ["kb/snapshots/kb-001@1", "strategy/outline@2"]   # evidence (data genealogy)
refs: ["arxiv:2411.00816", "doi:10.48550/arXiv.2411.00816"]
created_at: "2025-09-10T03:21:00Z"
checksum: "sha256:…"
```

### 4.3 Ledger events (JSONL, append-only)
```json
{"seq": 42, "ts": "…", "project": "p-001",
 "actor": {"dept": "editorial", "agent": "structure"},
 "event": "artifact.created",
 "payload": {"artifact": "editorial/reviews/r-002", "version": 1, "checksum": "sha256:…"},
 "refs": ["msg:m-0007"]}
```
Initial event set: `project.created`, `mission.published`, `task.assigned`, `message.sent`, `artifact.created`, `gate.checked`, `gate.failed`, `arbitration.decided`, `release.tagged`.

### 4.4 Versioning rules
1. Storing the same `id` again always yields `version+1`; bodies are immutable (genealogy via parent links).
2. Concurrent edits → each branch version is created; merge/adoption is decided by the owning department's chief (or Composer) via `superseded` marking.
3. Release tags: `v0.1-brief`, `v0.2-survey`, `v0.3-outline`, `v0.4-draft`, `v0.5-edited`, `v1.0-final`.

## 5. The `paper` Score v0 — Pipeline

| Stage | Owner | Content | Outputs | Gate |
|---|---|---|---|---|
| **S0 Brief intake** | Composer | Parse brief → mission, success criteria, deliverable spec | `mission.json` | G0 mission review |
| **S1 Survey** | Research & Intelligence | Scout → genealogy/trends → cataloger normalization → verifier QA | KB snapshot + survey report | **G1** source integrity |
| **S2 Storyline** | Strategy & Writing | KB-based positioning & argument chains → red-team attack → (insufficient evidence → research re-request loop) | Storyline + feasibility report | **G3** evidence alignment |
| **S3 Outline** | Strategy (Planner) + Editorial (early structural review) | Section specs, work decomposition | Outline | **G5a** user approval |
| **S4 Writing** | Strategy (Section Writers ×N) | Parallel section writing; citations from KB only | Section drafts | **G2** citation–claim alignment |
| **S5 Editorial loop** | Editorial ↔ Strategy | Review → revise, repeat (default cap 3); Editor-in-Chief terminates | Reviews + revised drafts | **G4** format & structure |
| **S5.5 Assembly & rendering** | Editorial (Format) + Strategy | Assemble sections → generate LaTeX source → compile (pdflatex/xelatex) | `final.tex` + `final.pdf` | **G4b** compile success, zero reference errors |
| **S6 Release** | Composer + Archivist | Final verdict, release note, tag | `v1.0-final` (LaTeX source + PDF) | **G5b** user final approval |

### Quality gate definitions
- **G1 (source integrity):** every reference card carries an identifier (DOI/arXiv id/URL) and access metadata; zero duplicates, zero unverified items.
- **G2 (citation–claim alignment):** every in-draft citation exists in the KB snapshot and passes verifier/consistency QA correspondence checks.
- **G3 (storyline–evidence alignment):** zero blocking issues in the red-team report (otherwise research re-request loop).
- **G4 (format & structure):** template conformance, citation style, structural review pass.
- **G4b (rendering):** LaTeX compilation succeeds, zero reference/citation errors, PDF produced.
- **G5 (human gates):** G5a outline approval, G5b final approval (per Q5).

### Language policy (D11/D12)
- The score declares the writing language. `paper` score default: **English writing & rendering** (briefs may be Korean; internal notes may be Korean, citations keep source-language).
- Editorial reviews against the **academic register** of the target language.
- Release/progress summaries may be bilingual (Korean summary alongside). Korean-paper scores override with a kotex compile chain.

### Free-exchange scenarios (to make D3 tangible)
1. Strategy (red team) → Research: `request` "claim A-3 lacks evidence — re-survey post-2023 counterexample literature"
2. Research (genealogist) → Strategy: `data` "new finding: 2 papers in the X lineage, 2024 — may collide with the current storyline" (proactive push)
3. Editorial (structural) → Strategy: `critique` "S2→S4 circular logic" (loop)
4. Editorial (format) → Research (Cataloger): `request` "3 citations missing metadata — please complete"
5. If unresolved → chief-to-chief negotiation → Composer `decision` (ledger-recorded)

## 6. Agent Execution Abstraction (framework-agnostic interface)
```
Agent:
  id, dept, role, tools[], model_profile
  run(task, inbox: [Message], blackboard: Store) -> { artifacts: [Artifact], messages: [Message] }
Chief(Agent):    # same interface + decompose/verdict powers
Arbiter(Composer):
  decide(dispute, evidence_refs) -> Message(type=decision)
```
- Department runner adapters: `LocalAgentRunner` (default), later `LangGraphRunner`, `AutoGenRunner`, etc., all behind the same interface.

## 7. Permission Matrix (blackboard write permissions)

| Path | Research | Strategy | Editorial | Composer |
|---|---|---|---|---|
| `kb/*` | **write** | read | read | — |
| `strategy/*` | read (+requests) | **write** | review (proposals) | tag |
| `draft/*` (sections) | read | **write** | review | tag |
| `editorial/*` | read | read | **write** | tag |
| `releases/*` | read | read | read | **write** |
| `ledger/*` | — | — | — | append (via Archivist) |

## 8. Execution Mapping Options (evidence for Q1/D9)

| Option | Description | Pros | Cons | Verdict |
|---|---|---|---|---|
| **A. Standalone Python orchestrator** (core) | File + JSONL queue-based native core, direct LLM API | Precisely fits our requirements (versioning, free exchange, scores); minimal deps; easy debugging | LLM runtime conveniences implemented ourselves | **v1 core** |
| B. LangGraph runner adapter | Departments as subgraphs; checkpoints for interrupt–resume | Proven persistence, built-in human gates | Bus/blackboard/versioning still ours | v1.5 optional adapter |
| C. AutoGen 0.4 runner adapter | Actor event model for free exchange | Free comms layer for free | Org metaphor, storage, gates still ours | v1.5 optional adapter |

- Recommended path: **A validates principles (D1–D12) → swap department runners to B/C adapters where useful** (interface per §6).

## 9. Security · Cost · Operations Policy

1. **Model routing:** survey & normalization = economical models / storyline, writing, Editor-in-Chief = high-capability models; per-project overrides in the manifest.
2. **Cost caps:** per-project token/budget ceilings; overflow halts at the gate with a user notification.
3. **Loop caps:** editorial loop 3, research re-request loop 2; beyond → arbitration.
4. **Secrets:** environment variables / local secret files only; never recorded in the ledger.
5. **Web-survey logging:** every external query's URL, query, and timestamp is recorded in reference cards (reproducibility).
6. **Transparency:** final deliverables include an AI-generated-content notice + per-department/agent contribution summary.

## 10. v2 Expansion Slots (reserved in design)

- **Experiment execution:** `tasks/execute` tool + tree search (per AI Scientist v2), sandboxed execution behind gates.
- **Resource awareness:** boot-time environment inventory (CPU/GPU/tools/packages) → a `capabilities.json` artifact; Research adjusts survey scope to "what we have".
- **Tool discovery:** Research surveys open-source tool candidates → Strategy feasibility → install/run gate (user approval) → tool ledger in the Archivist.
- **External collaboration:** expose departments as A2A agents; connect tools via MCP.