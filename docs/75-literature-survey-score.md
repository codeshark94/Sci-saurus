# Literature Survey Before Research Selection

Status: implemented bounded public-literature workflow with local integration and evidence-gate tests. `run-survey` reuses the shared execution runtime, Operations Cell, immutable artifact store, independent review, capacity accounting, checkpoints, and time policy. A successful external live survey and research-quality accuracy require separate evidence.

## Mission boundary

A paper mission establishes the relevant prior-work landscape before selecting a contribution or producing a manuscript narrative. An existing survey remains applicable only while its exact governing scope and evidence are current. This requirement belongs to the paper mission; unrelated Scores retain their own deliverables and selected capabilities.

The implemented output is an evidence-backed assessment within a declared finite search boundary. It may refute a proposed gap, retain insufficient evidence, or find the gap eligible for an experiment under that boundary. Eligibility does not establish a new scientific result or publication-ready novelty. Missing abstracts, unavailable full text, incomplete pagination, and failure to locate a solution remain distinguishable.

## Prepare and run

Install the repository runtime, then prepare a new configuration:

```bash
sh scripts/setup-runtime.sh
python3 scripts/prepare-survey-config.py --output /tmp/survey-run.json
```

The helper reuses the project runtime inventory to verify the installed MCP Fetch package and extractor pins. It records the absolute interpreter command and scoped environment identity files, refuses to overwrite an existing file, and leaves `live_dispatch_allowed` false with model endpoint and name set to `runtime_required`. It performs no source retrieval or model call.

Set the authorized model endpoint, model name, optional authentication environment-variable name, and `live_dispatch_allowed` in the prepared file. The configuration requires public inputs and an explicitly authorized capacity pool. Use a new project directory:

```bash
python3 -m scisaurus.cli run-survey /tmp/attention-survey \
  --config /tmp/survey-run.json \
  --first-result-seconds 480 --target-seconds 720 --deadline-seconds 900
```

The CLI time options override the prepared policy within its configured wall-clock limit. The example in `config/survey-run.example.json` asks about attention-only sequence transduction and proposes an absence claim to challenge against public literature. `survey.proposed_gap` may instead be `null`; a model then nominates one bounded hypothesis only after a survey has been accepted. Neither form grants experiment execution or release authority.

## Implemented search and comparison cycle

1. Publish the versioned question, seed terms/work IDs, resource limits, and search protocol. Register the OpenAlex capability and optional MCP Fetch capability, and require representative execution plus independent operational readiness evidence. Bibliographic readiness failure blocks the run; unavailable full text remains a recorded access gap.
2. Dispatch separate Research and Methods topic-search planners without exposing the proposed gap. Capture configured seed works and queries, then run each planner's bounded queries. The model configuration is shared across roles; separate tasks do not establish model diversity or independent scientific ground truth.
3. Expand configured seed batches through referenced works and citing papers. Deduplicate by OpenAlex identity and normalized DOI while retaining provider aliases and the captured observations. Record query requests, returned IDs, new unique works, remaining cursors, and each expansion batch's coverage.
4. Fetch configured full-text routes for captured works through MCP Fetch. Match the configured and registered titles against the text, require configured section markers, and reject incomplete captures for decisive use. Failed identity or scope checks retain the capture as unverified text. These checks establish the configured text binding; they do not independently reconcile publication history.
5. Assign each mapping task one work and its outgoing conceptual relationships, and run bounded worker waves in parallel. New or changed source/metadata bases reopen the affected work; a changed relationship target or unavailable supporting source also reopens the relationship's owner. Each work receives an immutable analysis containing its screening decision, rationale, problem, approach, finding, and limitations. Asserted fields require exact captured quotes from that work; unknown fields use `null` with no supporting evidence. A task must return its assigned work once and cannot rewrite another work or another work's outgoing relationships.
6. Keep provider citation edges separate from conceptual relationships. `source extends target` means the source builds on or extends the target, never the reverse. `compares` is an analyst comparison supported by both works, without asserting that either paper explicitly cites the other; `related` asserts topical connection without inheritance. `contradicts` requires incompatible findings under comparable scope. Mapping and review share this contract, and acceptance rejects a focused review using different meanings. Claims that one work extends, contradicts, compares with, or relates to another require exact evidence from both works. A citation, shared term, date, or citation count alone cannot establish conceptual inheritance or superiority. Each work receives a separate independent review of its inclusion, rationale, four claim fields, and every outgoing relationship. A failed check grants repair authority only for its named field or relationship target; all other content remains exact. Repaired work is reviewed again. An independent survey-review task then checks coverage accounting, source fidelity, and map support before the exact survey is accepted.
7. Bind the configured or newly nominated gap to the accepted survey. A separate challenger plans and executes targeted counter-searches. New evidence remaps affected entries and receives a fresh independent survey review. The final independent assessment compares nearby work and returns `refuted_by_prior_work`, `insufficient_evidence`, or `eligible_for_experiment`.

Provider-reported metadata is preserved and labeled. The current normalizer checks its shape and identifiers; it does not confirm the reported title, year, DOI, or publication version against independent sources. A dated display therefore reports provider metadata, not verified scientific chronology.

Each mapping task receives its own source windows and available abstracts for comparison. Its quotes must come from the displayed source references and text. Validation reports field paths, work IDs, and evidence indices/source references for exact-quote failures so repair remains scoped to the rejected assignment. Valid work analyses and their relationships are published from a completed wave before rejected assignments are retried; unchanged successful assignments are retained. These records require both focused work reviews and independent survey review before acceptance.

## Exact evidence and authority gates

The `literature-survey-2` bundle requires exactly one current passed focused review for every registered work and its outgoing relationships. The gate binds each actual review prompt to the exact stored entry and relationship bodies. Review source windows must be exact slices of pinned source captures with matching work identity, representation, and length, and must contain every cited quotation. A missing work, omitted relationship check, fabricated context, or failed focused review blocks acceptance even when the global reviewer passes. Older survey bundles require new reviews under this schema.

Survey acceptance also requires all three registered checks exactly once: `coverage-accounting`, `source-fidelity`, and `map-support`. Gap assessment requires `closest-prior-work`, `scope-comparability`, `counterevidence`, and `full-text-support`. Unknown, duplicated, omitted, or malformed checks cannot imply approval. Decisive gap states require every check to pass.

Reviews bind the exact survey and recorded model response from a successful independent task attempt. The deterministic gate checks immutable artifact integrity, governing versions, and authoritative acceptance records. Gap nomination, targeted challenge, and final assessment recheck the current accepted survey immediately before worker dispatch. Survey and assessment commitment recheck current dependencies, capability applicability, and remaining time inside the adoption boundary. A new source, map, work, or other governing version invalidates the old survey's applicability.

Counter-search and assessment also name the exact `nomination_ref` and check its currentness at dispatch and assessment commitment. The assessment's authoritative record binds both the survey and the nomination. `require_current_assessment` revalidates those dependencies; replacing a candidate invalidates its previous verdict even when the underlying survey remains current. A verdict for one hypothesis cannot authorize a revised hypothesis.

Every assessment quote must occur in the identified captured source and name a work and source pinned by the accepted survey. A refutation must identify a `solves` comparison with verified full text from that comparison's own work. Eligibility requires resolved comparisons with no `solves` or `uncertain` entry and verified full text for every decisive comparison. Unrelated full text cannot satisfy another work's evidence requirement. The gate also checks the successful MCP execution, URL, captured bytes and hashes, title and section identity, and capture completeness. These mechanical checks bind evidence; independent review remains responsible for whether the quoted text supports the claim.

The runner blocks eligibility when recorded access/limit gaps or incomplete decisive source contexts remain. Bounded search coverage and pending pagination stay visible to the reviewer; saturation is not a proof that no relevant work exists. Retrieved text is supplied as untrusted source material and cannot change the mission or acceptance rules.

## Time, coverage, and recovery limits

The configuration explicitly bounds queries per planner, results per query, unique works, expansion rounds, expansion seeds, references per work, bibliographic calls, saturation thresholds, full-text attempts, captured text, and model context. Each search or citing query reads one finite page. It records `has_more` and `next_cursor`; it does not automatically exhaust pagination. Saturation counts complete expansion batches with fewer than the configured number of new works and applies only to those batches.

The implemented OpenAlex route uses stemmed search. Planner prompts expose its syntax; argument validation rejects `*` and `?` wildcards and requests whose percent-encoded URL exceeds 4094 bytes before HTTP dispatch. It does not silently change the search mode. These limits follow the [OpenAlex search specification](https://help.openalex.org/api/searching/).

`max_api_calls` bounds the discovery and expansion requests recorded in the search log. Operational readiness probes also use retrieval calls and appear in total usage. `max_rounds` bounds attempts for each checked assignment and focused semantic review/repair cycles. Exhaustion preserves the proposals and failed checks without approving a survey. Mapping retries contain only assignments rejected by validation; successful work is not regenerated with the failed assignment. Model inputs currently receive a configured prefix of each source, with the full available length and exact visible window reported in coverage.

Mapping uses at most `limits.concurrent_calls - 1` workers through the shared batch dispatcher, keeping one call slot outside the producer pool. The revision 3 example configures eleven concurrent calls and therefore ten mapping or focused-review workers. Initial time planning uses the configured `max_works` and this actual worker count. Each production or revision wave's admission includes the remaining review burden of already produced work.

Stage durations are planning assumptions updated with observed durations. Admission reserves time for review and stops discretionary work when it cannot fit the target or hard deadline. An initially infeasible hard limit blocks external dispatch. The first independently accepted survey marks the first verified result; capture counts and model calls alone do not. Deadline, cancellation, stale evidence, exhausted validation retries, or dispatch failure preserves available records and reports a blocked outcome. Unknown external-call outcomes retain conservative resource accounting.

The CLI requires a new project directory. Durable artifacts and checkpoints support inspection of an interrupted run, but automatic resume of its search frontier, in-memory mapping state, and pending stages is not implemented. Read retained survey and assessment references together with `survey_current` and `assessment_current`; a blocked run does not make stale evidence or a revised nomination's old verdict current.

## Outputs

| File under `output/` | Content |
|---|---|
| `survey.md` | Readable per-work assessment, gap rationale when accepted, coverage gaps, and release status |
| `run.json` | Run status, exact survey/nomination/assessment references, current applicability, gap state, time plan, usage, capabilities, and blockers |
| `works.json` | Deduplicated work register with original provider metadata and retained provider IDs |
| `coverage.json` | Actual queries and expansion batches, access/limit gaps, remaining pagination, and source windows |
| `literature-map.json` | Per-work entries, supported relationships, citation edges, and exact map/survey references, when a map exists |
| `gap-assessment.json` | Accepted comparison, checks, exact evidence, and model execution reference, when an assessment exists |

Immutable versions, source captures, execution records, and checkpoints remain in the project store. A `completed` run can conclude `insufficient_evidence` or refute the proposed gap. Exports remain `not_released`; a successful assessment does not automatically publish a paper or execute an experiment.

## Validation and next steps

Local tests cover inert runtime preparation, actual installed package-pin discovery, bibliographic HTTP fixtures, real MCP stdio transport to a fixture server, separately planned searches, bounded citation expansion, scoped map updates, and independent acceptance. Negative cases cover invented quotations, wrong-work evidence, abstract-only decisive comparisons, missing checks, stale survey dispatch, source/capture corruption, focused-review context tampering, omitted registered works, ungranted semantic edits, and deadline rollback. Simulated model replies and local provider fixtures establish workflow behavior; they do not establish a successful external live survey or expert-level novelty judgment.

### External execution evidence and limits

Run `36d8cd4dfb7b40aba362593ec1bd5037` used revision 3 of the example, the configured external Qwen model, OpenAlex, and installed MCP Fetch. It ended **blocked** after 745.53 seconds, within the 900-second hard limit but without meeting the 480-second first-result target. No survey, nomination, or gap assessment was accepted.

| Observation | Retained evidence |
|---|---|
| Acquisition | 10 works, 9 abstracts, one verified 43,780-character primary-text capture; finite pagination and one work-limit gap remain visible |
| Mapping | 10 per-work analyses; two rejected quotation assignments repaired without regenerating valid assignments |
| Focused review | Seven recorded replies: three with all checks passed, four with failed checks; three other review calls returned HTTP 502 |
| Unsupported content caught | Unproven inheritance, a claim imported from another abstract, title-derived content without a source, and an unsupported shared-property comparison |
| Authority | No survey acceptance, downstream hypothesis selection, or release; semantic repair did not run after the transport failures |
| Accounting | 21 completed model calls and 10 retrieval calls; three unknown-outcome model reservations retained separately |
| Integrity audit | 64 source files matched the executed snapshot at inspection; 289 object hashes, 293 manifests, 676 events, exact run export, and no active tasks verified |

Local evidence is under `.runs/axion-survey-20260910T142622Z/`, including `output/survey.md`, `output/run.json`, and `verification.json`. The run report SHA-256 is `1a59cb1b3cd60a2910a163c257a9a89ac33d26c7c71428c6aa47c6e69ebc83e5`. The source manifest identifies the executed build; later contract changes require their own checks. Directed relationship semantics now receive an explicit shared prompt contract and a deterministic acceptance check, covered by local regressions.

This run verifies real acquisition, scoped quotation repair, retained review evidence, and failure handling. It does **not** verify a completed external survey or successful live semantic repair. The configured one-minute work-review estimate was substantially optimistic: the slowest returned review took 520.79 seconds. Adaptive concurrency, source-context selection, and resumable execution remain necessary latency work; unlimited external GPU availability does not establish the throughput of a particular configured API route.

The captured provider record for `W1810943226` pairs *Generating Sequences With Recurrent Neural Networks* with a GNSS abstract, while the [primary arXiv abstract](https://arxiv.org/abs/1308.0850) describes text and handwriting generation. The original observation remains retained and provider-labeled; bibliographic identity reconciliation is not yet automated. Exact quotation checks cannot establish that a provider attached the correct abstract. Independent content inspection also found a residual false pass: the drug-review rationale asserted NMT origins that its abstract did not establish, and its focused reviewer acknowledged the omission but still passed the clause. Primary Transformer claims affected by HTTP 502 remained unreviewed. These development cases are not a held-out accuracy benchmark; model judgments remain fallible even with explicit every-clause review criteria.

The next priorities are:

1. Evaluate held-out expert-labeled solved and unresolved cases, including alternate terminology, missing full text, duplicate publication versions, and conflicting or non-comparable findings. Report false novelty claims, missed closest prior work, unsupported comparisons, and justified abstentions; approval counts are not an accuracy measure.
2. Discover alternate full-text routes, resolve relevant sections beyond prefix contexts, and add stable source-span pointers bound to exact captured versions. Current evidence requires exact captured quotes and does not yet expose stable span identifiers. Reconcile DOI/title/author/date/version conflicts while preserving original captures and provider observations. Explicitly configured URLs and title/section checks do not replace that work.
3. Add resumable execution with durable search-frontier reconstruction, currentness and capability revalidation, unknown-call reconciliation, and preserved resource accounting. Restart must not duplicate committed effects.
4. Integrate supplied-results intake, accepted surveys, claim and argument checks, scoped manuscript composition, LaTeX compilation, rendered-PDF inspection, and exact release artifacts into the complete paper mission. Reuse the shared project services; retain the independent behavior of non-paper Scores.
