# Critical review articles

Review articles have a separate evidence product and publication contract. They
do not need a fabricated experiment, results package, or empirical novelty gate.
The supported genre is a **critical review**. A systematic review, scoping review,
or meta-analysis requires a separately audited search, screening and analysis
protocol and is not advertised as supported by this route.

## Workflow

1. Propose multiple timely themes and candidate journals from a research brief.
2. Search for competing reviews and primary studies using OpenAlex. Select a
   bounded corpus that includes contrary evidence, then acquire accessible text.
3. Capture the candidate journals' scope and author instructions. Record whether
   unsolicited manuscripts, a proposal, or an invitation is required. Unknown
   policies remain unknown; a venue cannot be selected without captured evidence.
4. Benchmark at least two existing reviews: scope, section structure, organizing
   argument, visual strategy, omissions and the boundary against copying.
5. Build a comparative evidence matrix and derive an original synthesis: a
   taxonomy, reconciliation, boundary condition, testable hypothesis or research
   agenda. Each insight must trace to two distinct primary works and explain its
   difference from the benchmark reviews, counterevidence, uncertainty and a
   possible disconfirmation test.
6. Construct the storyline and venue-specific synopsis; write a source-bound
   manuscript including a comparative table.
7. Render a PDF and page images before each independent evidence, contribution
   and visual-layout review. Retain both rejected and accepted rounds. A rejected
   final round returns scoped manuscript work to Composer.

The capture is a bounded text window, not a claim that an entire paper was read.
Unavailable sources and non-exhaustive coverage are retained as limitations.
Exact quote checks establish provenance, not entailment or scientific truth;
independent critique still has to judge the synthesis. Benchmark prose and
figures are not copied into the new article.

Journal policies are acquired per mission, not hardcoded by prestige. For
example, [Nature Communications](https://www.nature.com/ncomms/submit/commissioned-content)
describes synopsis-based review proposals, while
[Annual Reviews](https://www.annualreviews.org/page/authors/author-instructions/unsolicited-authors)
describes its proposal and invitation process. A prepared synopsis or manuscript
does not authorize contacting an editor, submitting, or claiming an invitation.

## Prepare and run

Import provider settings from an existing workflow without making model calls:

```sh
.venv/bin/python -m scisaurus.cli prepare-review-article \
  --from-workflow /absolute/path/to/existing/workflow.json \
  --output-dir /absolute/path/to/new-review-project \
  --brief "Identify a timely critical-review question in the declared research area"
```

Inspect `review.json`, confirm authorship, and supply the local credential file
at launch if it was not already part of the source workflow:

```sh
.venv/bin/python -m scisaurus.cli run-composer \
  --workflow /absolute/path/to/new-review-project/workflow.json \
  --env-file /absolute/path/to/provider.env
```

This is a normal Composer `paper` stage with a `review-article-config-1`
descriptor; the existing empirical path is unchanged. The runner also supports
`run-review-article --config /absolute/path/to/review.json` for direct execution.
An optional `evidence_path` accepts a hash-checked, previously captured
`review-evidence-1` packet, preserving the same synthesis and peer-review checks.
Its department specialists run after the self-contained review product exists.
They receive role-specific manuscript, source, venue and argument projections;
the outer adversary receives quoted support, panel verdicts and render identity.
Budget compaction preserves readable unit excerpts and their section/unit IDs,
while explicitly marking omitted scope.

## Operational boundaries

- Model-call, token, API and wall-clock limits are explicit. The review runner
  is sequential; it does not add another unmanaged parallel provider pool.
- Each actual model call has its own task, attempt and input-addressed retained
  result. Successful calls are reused. Two schema attempts at most are admitted
  for an unchanged assignment, including across restart.
- An interrupted call is `result_unknown`, not automatically repeated. HTTP 429
  records provider cooldown. Budget state and the original deadline survive
  restart and Composer continuation attempts.
- Planning, corpus, synthesis, draft, rendered pages, peer findings and usage
  remain inspectable. API calls are distinct from model calls.
- Source capture identity survives restart; rereading a retained capture does
  not reset synthesis retries. Transport-level OpenAlex retries are disabled
  inside this runner so each admitted request has a separate budget charge.
- A rejected round retains its actual manuscript, plan and review findings.
  Composer continuation pins that revision context to the work order and sends
  it to both synthesis and writing; an unchanged restart does not create a new
  revision allowance.
- `candidate_needs_review` means the independent panel accepted a candidate
  awaiting Principal review. It does not mean publication readiness, expert
  adjudication, journal acceptance or permission to submit.

The generated configuration defaults to 24 review-runner model calls, two
manuscript review rounds, and a one-day wall. Composer's separate department
assignment work is accounted in its own ledger; these are not a claim of a
24-call total for the enclosing mission.
