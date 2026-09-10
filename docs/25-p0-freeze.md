# Sci-saurus — P0 Contract Freeze Record

> **Version:** v0.8 · **Date:** 2026-09-10 · **Status:** freeze record for [Roadmap §3](30-roadmap.md) (P0). Part of the v0.8 package; introduces no new decisions.
> This record freezes user-facing principles, selects the first acceptance fixture, and fixes the deployment-configuration template. Principal sign-off (§6) is distinct from evidence of effective model behavior.

## 1. Frozen principles (with decision authority)

| # | Principle | Decision |
|---|---|---|
| 1 | Principal-directed command; verbatim instruction preserved; only the Principal changes objectives, prohibitions, priorities, or delegated authority | D14 |
| 2 | Four departments — Research & Intelligence, Strategy & Writing, Methods & Validation, Editorial Office — under an Executive Command of Intent Keeper, Composer, Arbiter, Progress Controller | D15 |
| 3 | Organization-wide Web Intelligence Fabric; Research owns formal evidence acquisition and promotion | D22 |
| 4 | Every department has a designated Adversarial Reviewer with an independently reserved retrieval allowance | D13, D23 |
| 5 | Grounded criticism with explicit rebuttal and independent adjudication; four review outcomes; no free pass from failed reviews | D13, D20 |
| 6 | Verified progress against a fixed acceptance contract; incumbent preservation; no monotonic-quality fiction | D18 |
| 7 | Immutable artifacts, version manifests, transactional control store; git as snapshot/release history only | D5, D19 |
| 8 | Bounded authority: deterministic checks, evidence-based judgment, and human approval are separate gate classes | D20 |
| 9 | Purpose-directed surgical revision: stable unit identities, EditGrants, ChangeSets, verified atomic integration | D31–D33 |
| 10 | Active external intelligence with adapter-backed capabilities; discovery is not evidence | D34 |
| 11 | One operating organization per research project; project-scoped authority and data | D35 |
| 12 | On-demand Operations Cell carrying practical setup/execution inside v1; new experiments remain separately scoped | D36 |

## 2. Explicitly superseded or unresolved disagreements

Superseded (with later decision authority — see SSOT §3.3 for the full interpretation table):

| Earlier default | Superseded by |
|---|---|
| JSONL queue as live multi-writer store | D19 (transactional control store; JSONL = audit export) |
| Fixed review-round/message/lifetime caps | D27 (renewable windows, finite leases, explicit limits only) |
| Standalone Composer's concentrated authority | D15 (Executive Command) |
| Strictly sequential stage reading | D16, D26 (delegated initiative, activity graph) |
| Tool execution deferred beyond v1 | D36 (practical operations in v1) |
| Whole-document editing rights | D31–D33 (surgical revision) |

Unresolved **by design** — these block live dispatch, not P0 exit (roadmap §3; SSOT §10):

| Open item | Disposition |
|---|---|
| Q3 retrieval providers, credentials, source rights | Contract fixed in `50-web-intelligence-integration.md`; actual providers/credentials are deployment configuration |
| Q4 endpoints, measured model capacity, budget mode and ceilings | Deployment configuration; `runtime_required` in the template |
| First evaluation dataset freeze | Required before any efficacy claim (roadmap §7.1); dev fixture selected in §3, held-out set still to be frozen |
| Extra independent-review model/provider | Optional per SSOT §10; no cloud fallback without authorization |

## 3. Selected first fixture: `fixtures/p1-slice/`

A synthetic, self-contained **supplied-results** fixture authored for the P1 vertical slice (roadmap §4.2). It contains exactly the three required conditions: one genuine overclaim, one intentionally sound claim, and one missing-evidence condition.

| File | Content | Seeded purpose |
|---|---|---|
| `fixtures/p1-slice/results-package.md` | Supplied results SP-001: design, table, statistics, user findings F1–F3, limitations | Input for intake (G0) and transcription |
| `fixtures/p1-slice/storyline-brief.md` | Principal's verbatim brief (rough storyline, constraints) | Intent/mission input (G0) |
| `fixtures/p1-slice/claims-contract.md` | Required claims C1–C4, required sections, hard requirements, non-waivable rules | Acceptance contract for the slice |
| `fixtures/p1-slice/ground-truth.md` | Seeded-defect map and expected outcomes | Internal scoring aid; not visible to producers |
| `fixtures/README.md` | Fixture index and rules | — |

Rules: fixtures are immutable inputs (edits create new fixture versions); the package values are **synthetic** and must never be cited as real findings; fixture ground truth is used only in test harnesses.

## 4. First acceptance contract (CC-001)

`claims-contract.md` pins: exact transcription of supplied values (C1), bounded interpretation language (C2), the prohibition boundary for long-term/consolidation claims and the "up to 15%" figure (C3), and the missing-evidence handling of the 7-day retention claim (C4). Gates exercised in the slice: G0 (intake/authority), G2 (citation–claim alignment with mock sources), G3 (evidence alignment), plus the deterministic checks of G4b at fixture scale. Test IDs exercised per roadmap §4.2–§4.5: T01–T03, T06–T09, T41–T44, T56–T58 as the slice subset; full suites remain P1 acceptance requirements.

## 5. Deployment configuration template

`config/deployment-template.yaml` instantiates the open configuration (SSOT §10; roadmap §3): ResourcePolicy binding, capability bindings for the initial provider matrix, data policy, sandbox constraints, checkpoint cadence, and release policy. Every field still requiring a human decision is marked `runtime_required`; the configuration validator must block live dispatch while any such field is unresolved, and `null` must not read as "unlimited". Values fixed in the template (sandbox isolation, human release approval) reflect contracts, not measured capacity.

## 6. P0 acceptance state

| Roadmap §3 condition | State |
|---|---|
| All original design disagreements explicitly superseded or marked unresolved | **Met** (SSOT §3.3 + §10; §2 above) |
| Example fields conform to the contract | **Met** (`web-search-campaign.yaml`, fixtures, template conform to 40/45/50) |
| Test IDs defined | **Met** (roadmap §4.3–§4.5, §5, §6: T01–T80) |
| Unimplemented pieces labeled | **Met** (README "Current state"; roadmap §11; 40 §12) |
| Live-operation authority resolved | Not required for P0 exit; blocked until template fields are configured |
| Document approval by the Principal | **Pending** |

**Approval note:** P0 document approval establishes the testable contract. It is not evidence that a scheduler, critic, verifier, or compiler exists or works (roadmap §11; 40 §12). The first behavioral evidence is the P1 vertical slice against the fixture selected here.