# Sci-saurus — Fixtures

Test fixtures for the P1 vertical slice and later acceptance suites. Fixtures are **immutable test inputs**: any change creates a new fixture version; the P0 freeze record pins the selected set.

## Rules

1. Fixtures are **synthetic**. Any resemblance to real studies or real data is coincidental, and fixture values must never be cited as real-world evidence.
2. `results-package.md` represents *user-supplied* material: the organization ingests it as `inputs/*` and never re-analyzes it (v1 boundary, SSOT D17/§9).
3. `ground-truth.md` is for test harnesses only. It is not an input to producers, reviewers, or adjudicators during evaluation runs.
4. Fixture IDs are stable: `SP-001` (results package), `SB-001` (brief), `CC-001` (claims/acceptance contract), `GT-001` (ground truth). A revised fixture gets a new version suffix, never an in-place edit.

## Selected set (P0 freeze)

| Fixture | File | Seeded conditions |
|---|---|---|
| SP-001 | `p1-slice/results-package.md` | One sound claim (F1), one genuine overclaim (F2: long-term consolidation + "up to 15%"), one missing-evidence condition (F3, 7-day retention unverified) |
| SB-001 | `p1-slice/storyline-brief.md` | Rough storyline; no extra authority beyond the brief |
| CC-001 | `p1-slice/claims-contract.md` | Required claims C1–C4, required sections, hard transcription/non-invention rules |
| GT-001 | `p1-slice/ground-truth.md` | Seeded-defect map: SD1–SD3 defects, SC1 preservation target, SD5 inadmissible-criticism distractor |

Mapped acceptance tests: T01–T03 (core store), T06–T09 (critique validity, repair, regression), T15–T17 (intake gap, citation–claim mismatch, counterevidence visibility), T41–T44 (renewal, stagnation, long investigation, provisional exploration), T56–T58 (scoped changes).