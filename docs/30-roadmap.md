# Sci-saurus — Implementation Roadmap

> A derived document of `00-SSOT.md`. Phase completion means conformance to SSOT decisions (D1–D12) and the architecture (`20-architecture-v0.md`).

---

## 1. Phase Overview

| Phase | Name | Outputs | Completion criteria | Status |
|---|---|---|---|---|
| **P0** | Design freeze | SSOT · survey · architecture · roadmap (4 docs) | Docs approved (Q1–Q5 decided) | **Draft complete this session** |
| **P1** | Storage core | `scisaurus` Python package: project store + ledger + CLI | Immutable artifacts, append-only ledger, release tags all pass tests | Pending |
| **P2** | MVP pipeline | `paper` score v0 execution (brief → survey → storyline → outline → draft) | One real brief produces draft + KB snapshot + compiled PDF + release | Pending |
| **P3** | Free exchange complete | Message bus, dispute/arbitration, editorial loop cap, full audit report | 3 cross-department review scenarios pass | Pending |
| **P4** | General-purpose expansion | Custom score SDK · resource awareness · experiment-execution slot · A2A/MCP adapters | A non-paper deliverable (e.g., report) produced by swapping scores only | Pending |

## 2. P1 — Storage Core Details (next target)

### 2.1 Directory/module plan
```
scisaurus/
  core/
    schema.py        # dataclasses + JSON validation: Project, Mission, Task, Artifact, Message, Event
    store.py         # project create / artifact write (immutable, version+1, parents) / read / HEAD / diff
    ledger.py        # events.jsonl append-only / replay / per-project filter
    release.py       # milestone snapshot + git tag + release note generation
  org/
    manifest.py      # department/agent manifest loader + validation
    bus.py           # message queues (JSONL) send/receive + ledger recording
  agents/
    base.py          # Agent abstraction (run interface), LocalAgentRunner, model routing
  scores/
    paper.yaml       # paper score v0 (filled in P2)
  cli.py             # scisaurus new|status|show|diff|release
  tests/
    test_store.py    # immutability, parent links, concurrent branching
    test_ledger.py   # append-only, replay reproduction
    test_bus.py      # envelope validation, recording
```

### 2.2 CLI (v0 spec)
```bash
scisaurus new <project_id> --brief brief.md       # create project + repo, store brief immutably
scisaurus status <project_id>                     # artifact tree, gate states, recent ledger
scisaurus show <project_id> <artifact_id>[@ver]   # inspect artifact (+provenance)
scisaurus diff <project_id> <artifact_id> v1 v3   # compare versions
scisaurus release <project_id> v0.2-survey        # snapshot + tag + release note
scisaurus run <project_id> --score paper          # (P2) run the pipeline
```

### 2.3 P1 test criteria (completion conditions)
1. 3 writes to the same id → 3 versions, all 3 bodies preserved, parent chain exact.
2. Tampering with the ledger file → detected by replay verification (checksum chain).
3. Release tag → snapshot reproducible (same checksums).
4. Permission-matrix-violating writes → rejected (a `gate.failed` event recorded in the ledger).

## 3. P2 — MVP Pipeline Details

1. `scores/paper.yaml` declaration (stages S0–S6, gates G0–G5b per architecture §2/§5).
2. Tool adapters: web search / arXiv / Semantic Scholar (optional) / local files. Every query must produce a reference card.
3. Research execution: scout (parallel search) → genealogy analysis → cataloger normalization → verifier QA → **KB snapshot**.
4. Strategy execution: storyline → red team → (research re-request loop) → outline → parallel section writing.
5. Editorial loop: review → revise → max 3 rounds → Editor-in-Chief verdict.
6. Assembly & rendering: sections → LaTeX assembly (Format Editor) → compile (**G4b**) → PDF.
7. Release: `v1.0-final` (LaTeX source + PDF) + research archive + decision summary.

## 4. Risks & Mitigations

| Risk | Impact | Mitigation | Precedent |
|---|---|---|---|
| Citation hallucination | Paper credibility collapse | Fact Verifier + G1/G2 gates (mandatory identifiers) | Deep Research precedents |
| Endless review loop | Cost & delay | Loop cap 3 + Editor-in-Chief termination + arbitration | CycleResearcher lesson |
| Cost blowup | Operations | Per-department model routing + per-project budget gates | — |
| Chaotic inter-department exchange | Quality & traceability loss | Enforced envelope schema + full ledger recording + permission matrix | Blackboard-family lessons |
| Execution-state loss (interruption) | Restart cost | Ledger replay + git checkpoints (layer separation) | LangGraph precedent |
| Framework lock-in | Expansion limits | Fixed runner-adapter interface | SSOT D2/D6/D9 |
| Irreproducible data | Verification impossible | KB snapshots as artifacts + query logging | §5-E precedents |

## 5. Immediate Next Actions (start right after decisions)

- [x] Q1 (hybrid harness) · Q2 (LaTeX rendering) decisions reflected
- [x] D11/D12 language policy reflected — all development artifacts in English
- [ ] User's document review feedback applied
- [ ] P1 skeleton creation (git repo + package scaffolding)
- [ ] Implement `core/schema.py`, `core/store.py`, `core/ledger.py` + tests
- [ ] Draft `scores/paper.yaml`
- [ ] Verify one tool adapter (web search) — automatic reference-card generation confirmed