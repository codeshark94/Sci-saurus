# Research Argument Runtime

> Implemented 2026-09-12. This stage changes the paper pipeline's admission
> boundary; it does not rewrite an existing manuscript.

## Why the stage exists

A result table is not a paper argument. A manuscript becomes persuasive when a
reader can follow a question that matters, see the pattern that was found,
understand the competing explanations, and tell which observations support the
chosen interpretation. When a writer is asked to invent that chain while also
assembling prose, it tends to produce a long execution report: numbers are
repeated, the Discussion restates the caveat, and figures become attachments
instead of evidence in an argument.

The runtime therefore treats the argument as an artifact with its own owner,
version, review, and hash. Writing is downstream of that artifact.

## Admission path

~~~text
accepted results + literature
        ↓
research-argument discovery
        ↓
argument contract validation
        ↓
independent argument adjudication
        ↓
figure/table production plan
        ↓
structured manuscript composition
        ↓
scoped repair → five-perspective review → PDF release
~~~

The writer receives the accepted argument and the evidence packet. It is not
allowed to choose a new thesis, turn a possible mechanism into a fact, or
silently remove an unresolved alternative. A supplied structured draft still
passes through this gate so that resuming a run cannot bypass the scientific
spine.

## research-argument-1

The artifact contains exactly:

| Field | Required meaning |
|---|---|
| research_question | One unresolved, reader-facing question |
| observed_patterns | At least two important observations, each with an implication and evidence IDs |
| hypotheses | At least two distinct mechanisms, predictions, status, counterevidence, a discriminating test, and the observed patterns each mechanism explains |
| primary_argument | A bounded thesis, selected hypothesis, rationale, and scope boundary |
| discriminating_experiments | At least two designs with controls, predictions, measurements, and the hypotheses each design tests |
| figure_plan | At least two figures and one table by default; every observed pattern has a visual job, source references, readout, placement, and a bound rendered asset for each figure |
| limitations | Limitations that change how the argument can be interpreted |

Candidate and unresolved hypotheses are first-class outcomes. A model cannot
mark a mechanism supported or disfavored without evidence IDs. The validator
checks the IDs against the supplied results and literature packet, rejects
control-plane vocabulary in reader-facing fields, and refuses a figure plan
that only decorates the manuscript without covering the observed patterns.

The minimums are configuration, not a hidden length cap. A visual or
non-paper mission may select another budget, while a research-paper run uses
the two-figure/one-table default unless its Score declares a stronger one.

## Independent adjudication

research-argument-review-1 is a separate model call with a fresh prompt. It
checks:

1. whether the question is genuinely unresolved and narrower than the
   procedure;
2. whether each observation is evidence-bound;
3. whether the mechanisms make different predictions;
4. whether the proposed experiments can distinguish them;
5. whether each figure or table changes what a reader can infer.

An argument with failed or insufficient checks cannot reach the writer.
Adjudication is bounded by its own deadline and retry count. Its exact review,
usage, and hash remain in the run directory.

## Durable outputs and references

run-paper writes:

- research-argument.json
- research-argument-review.json
- research-argument-package.json

The paper pipeline run contract is version 2 once this gate is enabled, and a
release carrying the argument uses the corresponding candidate and claim-index
versions. Older releases remain readable as historical artifacts.

The manuscript project's writer-packet artifact includes the argument. The
paper release publishes the argument as strategy/research-argument and its
adjudication as methods/verifications/research-argument; the release manifest
records both references and hashes. Existing manuscript units remain immutable
and later repairs still target only the unit IDs named by a review.

The standalone command is useful for any project that needs a scientific
argument before composition:

~~~bash
python3 -m scisaurus.cli run-argument \
  --input evidence-packet.json \
  --config model-config.json \
  --output research-argument-package.json
~~~

run-paper can resume an accepted argument package with --argument-package.
Without one, the pipeline generates and adjudicates the argument
automatically. --argument-deadline-seconds, --min-argument-figures,
--min-argument-tables, and --min-argument-experiments make the time and depth
budget explicit.

## Surface boundary

Evidence IDs, hashes, reviewer decisions, and acceptance records remain
available in the archive. The manuscript sees their scientific meaning:
observation, mechanism, prediction, limitation, and implication. The existing
scientific-surface and editorial-compression gates remain active after
composition, so the new stage adds explanatory structure without weakening
provenance or surgical revision.

## Verification

The contract tests cover missing competing hypotheses, indistinct mechanisms,
unknown evidence, missing figure coverage, leaked operational terms, and a
model-backed generate-then-adjudicate sequence. The full test suite must pass
before this stage is treated as part of the paper runner.
