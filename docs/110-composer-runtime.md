# Composer-controlled research workflow

The project-level entry point is `run-composer`. It is the operational control
loop for an autonomous research mission. A workflow is a versioned directed
graph of specialist stages; the Composer admits only stages whose dependencies
are complete, binds exact outputs into the next ContextPackage, records a
checkpoint, and routes the returned status back into the next decision.

```text
Principal Intent + Score
          ↓
Executive Command / Composer
          ↓
Topic intake → Research → Methods → Interpretation → Argument → Composition
          ↑        ↓                  ↓            ↓
     evidence   checks            hypotheses   reviews/PDF
          └────────────── feedback and reallocation ─────┘
```

The Composer owns procedure and portfolio control: stage order, dependency
binding, time admission, resource accounting, pause/resume state, deadline-
governed continuation, and the release proposal. It does not author accepted evidence, convert an unresolved
judgment into a fact, or grant itself final publication authority. Research,
Methods, and Editorial workers retain their own checks; an Arbiter handles a
material dispute and the Principal retains material scope and release control.

## Durable project organization

Every Composer project opens a `DepartmentRuntime` with Research, Methods,
Strategy, Editorial, and Operations charters. The charters are versioned
defaults, not a fixed script. The runtime publishes the organization and each
charter under `command/`, persists every handoff in the receiving department's
inbox, and admits a request as a typed work-order task with a stable identity.
The optional workflow `organization` field can replace the default charter
while preserving the same schema and authority checks.

The command desk turns valid specialist requests into work. A research,
experiment, interpretation, or manuscript request is activated when its scoped
continuation closure is admitted and is resolved only after the owning stage
returns an accepted result. Replayed messages reuse the same artifact and task
identity; changed objectives create a new immutable work-order generation. A
malformed or unauthorized request is retained as a rejection record, so a bad
model response becomes a recoverable state instead of taking down the whole
mission. Checkpoints and `output/run.json` include the current charters,
inboxes' work-order backlog, and department activity.

This is the control plane for organic operation: the Composer can choose and
reopen in-scope work from observed evidence, but it cannot invent authority or
execute an arbitrary shell command. Operations work orders still have to pass
the project-scoped Operations Cell's configured adapter, environment, and
independent probe. Templates provide safe starting responsibilities; the
current mission, evidence, and capability state decide what happens next.

Each stage is an allowlisted runner (`topic_discovery`, `survey`, `experiment`,
`interpretation`, `argument`, or `paper`). A workflow cannot inject an arbitrary
shell command.
Operational programs and MCP services remain behind their project-scoped
Operations Cell. A binding such as `paper_config.survey_ref ← survey.survey_ref`
is recorded in the Composer run and applied to a fresh stage input; stale or
missing dependencies block admission rather than silently falling back.

The minimal stage descriptor is shown in
[`config/topic-discovery.example.json`](../config/topic-discovery.example.json);
replace its two absolute paths before workflow validation.

For a free-topic mission, `topic_discovery` runs before the survey. It queries
OpenAlex with objective-derived and broad recent-literature searches, filters to
a four-year recent publication window (falling back to the newest returned
records only when the window is empty), and makes an entropy-backed seeded random
sample so provider ordering does not become an accidental ranking. The seed is
written to the Composer run input and every checkpoint; resuming repeats the same
exploration, while a newly created mission gets a different seed. The output preserves sampled
work IDs, URLs, abstracts, DOI observations, query trace, response capture
hashes, and the seed. An intake model proposes distinct testable questions and
selects one against a redacted runtime capability inventory (Python packages,
executables, configured stage kinds, project inputs, and model protocol). The
selected question also declares structured executable, Python-package, and
stage-kind requirements; Composer rejects the selection when any required
capability is absent. The question and search strings can be bound into the
survey; metadata is never treated as evidence or a novelty claim. A topic stage
with `reuse_completed: true` is an explicit replay of the pinned output and does
not perform new topic generation; set it to `false` for a fresh exploration. If the
scholarly provider fails, topic admission fails rather than fabricating a topic.

An exploratory workflow may declare an `experiment_catalog` containing two to
sixteen pinned capability descriptors. Each candidate then names one exact
capability ID, the candidate portfolio must cover the available capabilities,
and Composer replaces the experiment descriptor with the selected capability
before dispatch. `topic_exclusions` carries explicit project-local exclusions;
excluded directions may be retained for comparison but can never be selected.
The selected topic gets a fresh survey identity and its own search
question. Broad discovery records are not copied as evidence seeds. During
initial retrieval, Composer divides the work budget across the seed and blind
search families and adds a compact exact-concept query when the topic exposes a
distinctive phrase, so one high-recall query cannot starve independent search
families.

A free-topic family also has an append-only topic history. Set
`topic_history_path` to share that history across fresh Composer project
directories; when omitted, Composer stores it beside the run-family root. The
history is scoped by objective and capability portfolio, records every admitted
direction, rotates the most recently used capability when another one is
available, and rejects an exact or near-identical selected question before the
survey begins. It is an execution-memory guard, not a novelty claim: scholarly
novelty still requires the survey and review gates.

Model generation profiles are role-specific. Exploratory topic and blind-search
roles receive higher temperature and presence diversity by default; interpretation
and writing use a middle setting; literature mapping, arbitration, and journal
review use conservative sampling; the adversarial AI-surface reviewer receives a
separate creative setting. A shared model file can override these defaults with
`model.role_profiles`; resolved values remain internal execution metadata and do
not enter reader-facing manuscript prose.

Every stage declares both an `estimate_seconds` planning reservation and a
`deadline_seconds` execution fence. When a completed stage is intentionally
carried forward, `reuse_completed: true` records that decision; an optional
`reuse_output_path` pins the exact checkpoint file. The Composer never infers
reuse from a nearby file and never marks a failed attempt successful. A
stage's feedback record includes its role, action (`advance`, `retry_stage`, or
`reconcile_blocker`), scientific state, output path, elapsed budget, and the
condition required before downstream admission.

The paper descriptor may also carry a validated `draft_path` or matching
`initial_review_package_path`. These are resume checkpoints for composition
and review, not a bypass: the imported draft is revalidated, the review hash
must match it, and the normal downstream PDF checks still run.

The run writes a separate Composer control project containing the immutable
workflow, stage task/attempt records, feedback decisions, progress checkpoints,
and `output/run.json`. Specialist projects retain their own artifacts and
event chains. This separation lets a single failed stage be retried or resumed
without rewriting unrelated paragraphs or losing upstream evidence.

## Organizational feedback loop

The Composer is also the command desk, not a passive scheduler. Every stage
handoff creates two linked records: a `decision_note` in the command namespace
and an immutable message envelope in the project `MessageBus`. The envelope
names the sending organ, receiving department chief, stage result, scientific
state, dependency set, elapsed budget, and the condition for the next action. A
successful handoff is a decision message to the consuming department; a failed
or unresolved handoff is a critique message to the Arbiter and Progress
Controller. The same body and artifact reference are used in both records, so
the organization cannot silently acknowledge a handoff that is missing from
the audit trail.

Blockers do not trigger a whole-project restart. The Composer records a scoped
reconciliation request, keeps incumbent artifacts, and leaves the blocked task
visible until an Arbiter decision or a deadline-governed continuation reopens
that scope. Usage from each completed stage is accumulated in the command
ledger, and the next admission sees the remaining deadline and the transitive
downstream reservation. This gives the workflow a human division of
responsibility: the department produces, independent checks challenge, the
Arbiter handles contested validity, and the Progress Controller decides
whether another action can still buy verified progress.

Paper production uses the same desk inside the paper stage. The review runner
emits one compact event for each scientific, methods, adversarial, human-
scientist, and editorial perspective, followed by a synthesis event. Surgical
repair and release events are emitted from the paper runner. The Composer maps
these events to the owning department, sends a blocking dispute to the Arbiter,
and records a stable event ID so a resumed cached review is acknowledged once.
Review text and replacement text remain in the manuscript project's immutable
unit versions; the command ledger carries routing metadata and references only.
Consequently no reviewer can edit the manuscript, no writer can self-accept a
finding, and no single worker can silently rewrite the document while the
organization still makes progress.

The paper stage first runs a deterministic `research_admission` gate before a
writer or manuscript project is admitted. It checks the selected scholarly
profile against the accepted inputs: reference count, full-text support,
argument-linked figures, and planned tables. A named profile such as
`empirical_journal` declares the desk floor (20 references, 5 full-text
references, 3 figures, and 1 table); the gate records every observed count and
emits explicit work orders for literature retrieval, additional discriminating
experiments, or analysis displays when a floor is missed. The stage returns
`research_expansion_required` and creates no draft or PDF, so a proposal or
plot package cannot masquerade as a paper by passing factual binding.

The same admission gate checks the experiment's substantive quality contract.
For a score-3 research paper, the Composer binds or monotonically upgrades a
contract requiring at least
two conditions, a declared control, two comparisons, an uncertainty statement,
an effect-size statement, a sensitivity analysis, raw-data provenance, and
three figure assets. The experiment first emits a pre-analysis design, then
the program's analysis summary is checked against that design before the
results package is accepted. A replayable package that lacks these components
creates Methods work orders and is held at research admission; the writer is
never asked to manufacture the missing scientific content.

After composition, the paper uses a real bounded peer-review cycle. The same
reviewer panel inspects the incumbent in round one, the author applies only
the named surgical repairs, and the same panel receives the revised incumbent
for re-review. Empirical journal papers require three rounds; the final round
must pass the journal editor and the deterministic editor-in-chief decision.
Reviewers can add first-class `research_requests` for an additional experiment,
literature expansion, interpretation expansion, or analysis repair. Such a
request cannot be discharged by rewriting prose: the paper stage stops with
`research_expansion_required`, routes the request to the owning department,
and refuses PDF release. If the panel still has material findings at the end
of the cycle, the editor records `review_rejected` rather than publishing a
review-limit candidate.

Survey mapping and focused-review repairs are a separate control loop. A
long-running autonomous survey may set `limits.repair_mode` to
`until_deadline`; malformed model output is then returned to the same scoped
assignment until the survey wall or its admission policy closes. When the
Composer retry mode is `until_deadline`, it projects the same repair setting
into experiment validation and topic intake, so legacy `max_rounds` and
`max_attempts` values cannot end a deadline-governed repair loop early. The
three-round limit above remains the independent manuscript peer-review rule.

Score-3 research-paper descriptors use `empirical_journal` when they do not
carry an explicit profile, so an omitted field cannot silently lower the desk
floor, and they cannot explicitly select the shorter `validation_report`
profile. Validation reports retain that shorter profile. A paper stage may
finish its bounded execution while returning `candidate_needs_review`,
`research_expansion_required`, or `review_rejected`; the Composer propagates
that state to its top-level run and never treats it as an accepted release.
`completed` is reserved for a workflow whose declared stages produced an
accepted release candidate subject only to the configured Principal approval
boundary.

The same distinction applies at every dependency edge. Only `completed`,
`accepted`, and `candidate_needs_review` satisfy a downstream prerequisite.
`research_expansion_required` and `review_rejected` are scientific holds: the
Composer starts their owning continuation closure immediately when policy and
the hard wall permit it, and otherwise returns the hold without dispatching a
consumer. A restart reconstructs missing packets from immutable stage outputs
before making that admission decision, so an older checkpoint cannot turn a
proposal into evidence merely because a process was interrupted.

When material findings conflict, the paper stage can activate a dedicated
Arbiter call before synthesis. It emits a finding-level adjudication that keeps
compatible critiques, rejects an unsupported competing directive, or merges
compatible repairs. The editor receives only retained repairs; rejected
alternatives and the Arbiter rationale remain available for audit. A malformed
or unavailable arbitration response conservatively leaves every material
finding open and cannot manufacture acceptance.

The review packet includes a bounded projection of the frozen procedures,
metrics, findings, limitations, claims, and references. A deterministic
evidence audit runs before arbitration. If a proposed surgical fix introduces
a decimal value absent from both its target unit and the frozen evidence, the
Composer splits that resolution and records an evidence-guard rejection; the
number is not allowed to overwrite an evidence-backed value. The finding stays
in the review archive and can be reopened only after a fact-verification or
calculation artifact is promoted. This keeps the human-like dispute loop from
degrading into model majority voting.

Before release, an editorial projection binds every literature-backed claim to
the paragraph that makes it. The projection removes orphan numeric citation
placeholders, adds only reference keys already pinned in the paper descriptor,
and writes a content-addressed audit for each pass. These changes are ordinary
unit replacements in the manuscript project; the source evidence, claim map,
and reviewer-facing numeric citation surface remain separately inspectable.
Evidence relations are honored at this boundary: `support` and `qualify` must
be expressed in their bound units, whereas `context` can remain a cross-section
summary whose detailed support is already bound elsewhere. Context is retained
in the claim index and cannot satisfy a storyline claim by itself.

During a provider call, the Composer emits an atomic live checkpoint to the
active stage's `output/progress.json` at the configured checkpoint interval.
The heartbeat is deliberately outside the SQLite control ledger, so a slow
remote response cannot create ledger contention. A resumed run can therefore
show elapsed time, current phase, and the last heartbeat without inventing a
completed stage or duplicating an event.

When a paper or specialist returns a structured research request, the Composer
continues automatically instead of only sending a notification. By default it
admits cycles until the hard wall, maps each request to the smallest owning
stage closure, and dispatches that closure in a cycle-specific project
namespace. A small deterministic workflow can opt into
`continuation_policy.mode: bounded` with `max_cycles` from zero through eight.
Survey continuations expand search, full-text, and API capacity together and
carry forward open-access routes; experiment continuations append the work
order to the methods context; paper continuations synchronize the reference
set to the newly accepted survey. Downstream interpretation, argument, and
review consumers rerun. Earlier attempts, manifests, references, and review
decisions remain intact. The hard wall remains the termination condition for
retry and continuation work.

Interpretation and composition stages receive the same scoped requests in a
fresh evidence packet as `scientific_follow_up`: each objective, rationale,
success condition, and evidence requirement is visible to the specialist that
must act on it. Their strict stage descriptors do not need ad-hoc fields, and
the packet instruction requires the new pass to address the work or preserve
the unresolved boundary rather than merely rewrite unchanged prose. Internal
request IDs and routing metadata remain in the Composer ledger.

```bash
python3 -m scisaurus.cli run-composer --workflow composer-workflow.json
python3 -m scisaurus.cli run-composer --workflow composer-workflow.json --resume
# Continue the same mission after a hard stop, adding only the time you intend
python3 -m scisaurus.cli run-composer --workflow composer-workflow.json \
  --resume --extend-deadline-seconds 36000
# Inspect a stopped run without opening the full ledger
python3 -m scisaurus.cli composer-interim-report /path/to/composer-project
```

`time_policy.first_result_seconds`, `target_seconds`, and
`hard_seconds` are explicit workflow fields; a ten-hour run is represented by
`hard_seconds: 36000` and stage deadlines that fit inside it. An optional
`retry_policy` supplies a backoff and may use `mode: until_deadline` for a
long autonomous mission. In that mode a failed stage is retried in fresh
isolated directories for as long as the stage and workflow deadlines admit;
there is no arbitrary attempt-count stop. This deadline-governed mode is the
default when no retry policy is declared. A small deterministic job that needs
a finite retry budget must opt into `mode: bounded` explicitly; that mode
retains the one-through-eight attempt contract.
`continuation_policy.mode: until_deadline` likewise keeps research-driven
re-entry open by default; `mode: bounded` with `max_cycles` is available for
small deterministic workflows. This is not a retry count. A retry never
overwrites the failed attempt and never turns an exception into success. If the
hard deadline is exhausted, the run becomes `blocked` with every attempt and
the next recovery condition recorded. Checkpoints persist the wall-clock start
and deadline, so a process restart cannot reset the mission's hard window. A
checkpoint keeps the latest completed stage visible throughout; completion is
never reported merely because a provider returned text.

### Compute-rich execution and bounded stopping

The default Composer policy spends the available inference budget on verified
progress: failed provider calls, malformed structured responses, surgical
repairs, scientific interpretation, and independent review remain eligible for
retry or scoped continuation until the hard wall. The runtime does not lower a
configured review reasoning level on later rounds or use an attempt counter as
an implicit stopping condition. Role-specific output-token settings are unset
by default and inherit the model configuration, so the pipeline does not hide
an additional 8k, 24k, or 4k ceiling inside a specialist. A stage still stops
early when its scientific contract is satisfied; more GPU does not justify
inventing work after the mission is complete.

The endpoint's measured protocol remains authoritative. For example, a private
single-worker vLLM service must use `model_concurrency: 1`; setting a larger
number only queues or overloads requests and does not create useful GPU
parallelism. Services with measured independent capacity may raise the
paper-stage `model_concurrency` and the specialist `concurrent_calls`
explicitly. Retrieval quotas, request-size limits, and data-policy boundaries
remain hard constraints even in compute-rich mode.

When the wall is reached, or a run is paused or blocked, Composer writes the
small projection `output/interim_report.json` and publishes the same report in
the command ledger. It lists the last phase, completed/active/pending stages,
attempt counts, held work orders, recent blockers, remaining time, and a
resume command. The report is written before `output/run.json`, so an operator
can inspect it after a timeout and resume the same immutable workflow with
`--extend-deadline-seconds N`; the extension is itself a durable decision and
appears in every subsequent checkpoint and run report.
