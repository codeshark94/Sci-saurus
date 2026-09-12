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
Research → Methods → Interpretation → Argument → Composition
          ↑        ↓                  ↓            ↓
     evidence   checks            hypotheses   reviews/PDF
          └────────────── feedback and reallocation ─────┘
```

The Composer owns procedure and portfolio control: stage order, dependency
binding, time admission, resource accounting, pause/resume state, and the
release proposal. It does not author accepted evidence, convert an unresolved
judgment into a fact, or grant itself final publication authority. Research,
Methods, and Editorial workers retain their own checks; an Arbiter handles a
material dispute and the Principal retains material scope and release control.

Each stage is an allowlisted runner (`survey`, `experiment`, `interpretation`,
`argument`, or `paper`). A workflow cannot inject an arbitrary shell command.
Operational programs and MCP services remain behind their project-scoped
Operations Cell. A binding such as `paper_config.survey_ref ← survey.survey_ref`
is recorded in the Composer run and applied to a fresh stage input; stale or
missing dependencies block admission rather than silently falling back.

Every stage declares both an `estimate_seconds` planning reservation and a
`deadline_seconds` execution fence. When a completed stage is intentionally
carried forward, `reuse_completed: true` records that decision; an optional
`reuse_output_path` pins the exact checkpoint file. The Composer never infers
reuse from a nearby file and never turns a failed stage into a success. A
stage's feedback record includes its role, action (`advance` or
`reconcile_blocker`), scientific state, output path, elapsed budget, and the
condition required before downstream admission.

The paper descriptor may also carry a validated `draft_path` or matching
`initial_review_package_path`. These are resume checkpoints for composition
and review, not a bypass: the imported draft is revalidated, the review hash
must match it, and the normal downstream PDF checks still run.

The run writes a separate Composer control project containing the immutable
workflow, stage task/attempt records, feedback decisions, progress checkpoints,
and `output/run.json`. Specialist projects retain their own artifacts and
event chains. This separation lets a single failed stage be resumed or
reconciled without rewriting unrelated paragraphs or losing upstream evidence.

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
visible until an Arbiter decision or a bounded continuation reopens that scope.
Usage from each completed stage is accumulated in the command ledger, and the
next admission sees the remaining deadline and the transitive downstream
reservation. This gives the workflow a human division of responsibility: the
department produces, independent checks challenge, the Arbiter handles
contested validity, and the Progress Controller decides whether another
bounded action can still buy verified progress.

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

The paper stage also runs a deterministic `journal_editor` desk gate. It is
separate from prose review and checks the selected scholarly profile against
the actual candidate: reference count, full-text support, in-text citation
density and section coverage, figures, and tables. A named profile such as
`empirical_journal` declares the desk floor (20 references, 5 full-text
references, 12 citation bindings, 3 cited sections, 3 figures, and 1 table);
the gate records every observed count and requests a scoped literature or
experiment expansion when a floor is missed. It prevents a short validation
note from being mistaken for a conventional journal article merely because
the manuscript has enough words and passes factual binding.

Score-3 research-paper descriptors use `empirical_journal` when they do not
carry an explicit profile, so an omitted field cannot silently lower the desk
floor, and they cannot explicitly select the shorter `validation_report`
profile. Validation reports retain that shorter profile. A
paper stage may therefore finish its bounded execution while returning
`candidate_needs_review`; the Composer propagates that state to its top-level
run and uses `candidate_needs_review` as the release status. `completed` is
reserved for a workflow whose declared stages produced an accepted release
candidate subject only to the configured Principal approval boundary.

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

```bash
python3 -m scisaurus.cli run-composer --workflow composer-workflow.json
python3 -m scisaurus.cli run-composer --workflow composer-workflow.json --resume
```

`time_policy.first_result_seconds`, `target_seconds`, and
`hard_seconds` are explicit workflow fields. The hard deadline fences new
admission, while a checkpoint keeps the latest completed stage and the next
condition visible. A deadline or stage failure produces `paused` or `blocked`
with the incumbent stage outputs preserved; it never reports completion merely
because a provider returned text.
