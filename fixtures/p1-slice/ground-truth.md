# Seeded Defect Map — GT-001 (P1 slice fixture)

> **Test-harness-only.** Never supplied to producers, reviewers, or adjudicators during evaluation runs. Maps the fixture's ground truth to acceptance tests (roadmap §4.3–§4.5, §5).

| Seed | Location in SP-001 | Nature | Expected outcome | Tests |
|---|---|---|---|---|
| **SD1** | F2 — "long-term memory consolidation" | Overclaim: only immediate recall was measured; consolidation/long-term processes were not | Blocking issue upheld; repair qualifies the claim to the immediate-recall effect; verification passes | T08, T09, T16 |
| **SD2** | F2 — "reducing retention by up to 15%" | Unsupported derived figure: the supplied difference is 1.7 items (≈ 12% of B's mean), about immediate recall, not retention | Blocking issue; the figure must not survive as an observed value | T16, T21-class transcription/entailment check |
| **SD3** | F3 — 7-day retention claim | Missing evidence: follow-up collected but unverified, no numbers | Claim remains unsupported; appears only as limitation/gap report; no fabricated data | T15-class intake/missing-evidence handling |
| **SC1** | F1 — direct recall comparison | Intentionally **sound** claim | Preserved through all repairs; coverage check confirms survival | T07, T09 |
| **SD5** | Distractor criticism (not in SP-001; introduced by a test harness) | Inadmissible criticism: "the study should have used a delayed-recall design, so the findings are invalid" | Rejected as outside the approved mission boundary (supplied results only); no forced producer edit | T06 |

## Scoring hints

- **Recall/precision:** SD1, SD2, SD3 should each be surfaced (by review or verification) with the correct location and criterion.
- **False-critique rejection:** SD5 must not produce a producer edit or a blocking gate failure.
- **Preservation:** SC1 and all SP-001 §2 values must remain exactly transcribed in the accepted manuscript.
- **Repair verification:** the accepted C1/C2 wording must still satisfy CC-001; a repair that deletes required findings fails (T09).