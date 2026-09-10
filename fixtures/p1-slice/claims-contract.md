# Claims & Acceptance Contract — CC-001 (P1 slice fixture)

> Governing acceptance contract for the P1 slice run over SP-001/SB-001. Synthetic; frozen at P0 (25-p0-freeze §4).

## Required claims

| ID | Kind | Statement (required meaning) | Support requirement |
|---|---|---|---|
| **C1** | observation | Under the tested conditions, normal-sleep participants recalled more word pairs than sleep-restricted participants. | Exact transcription from SP-001 §2 (14.1 vs 12.4; SDs; *t*(38) = 2.31, *p* = 0.026, *d* = 0.73), with conditions and test type. Must survive all repairs (SC1). |
| **C2** | interpretation | The immediate-recall difference is **consistent with** a role of sleep in memory processes shortly after encoding. | Interpretation language only ("consistent with"). Must not assert consolidation, long-term retention, or any magnitude for them. |
| **C3** | boundary | SP-001 does **not** support claims about long-term retention or consolidation magnitude. | The phrase "long-term memory consolidation" and the "up to 15%" figure must not appear as observed results. F2 may appear only as a qualified immediate-recall effect or in a repaired form. |
| **C4** | gap | The 7-day retention difference (F3) has no verified data. | Must appear only as a pending-data limitation or gap report. Never as a supported finding; never with invented numbers. |

## Required sections

Abstract · Introduction · Methods (transcription) · Results (transcription) · Discussion · Limitations.

## Hard requirements (non-waivable)

1. Every numeric value is transcribed exactly, with units and test types (G2 mechanical class).
2. Causal language is permitted **only** for the immediate-recall effect (randomized assignment supports it); no causal claims about retention or consolidation.
3. No invented citations, no fabricated values, no unverified follow-up data (v1 boundary; D17).
4. Deleting or softening C1/C1's required values fails coverage checks (T09).
5. External literature, when cited, must follow the discovery → capture → evidence path (`50-web-intelligence-integration.md`); snippets are not support.

## Gates exercised in the slice

G0 (intake/authority over the fixture) → G2 (transcription/citation–claim alignment) → G3 (conclusion defensibility under supplied results; C3/C4 boundary) — with editorial/rendering gates exercised later in P2 (roadmap §5).