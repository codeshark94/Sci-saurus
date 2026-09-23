# Agent, Department, and Stage Flow

> **Version:** v2.0 · **Date:** 2026-09-15 · **Status:** runtime contract for project-scoped Composer workflows and bounded specialist assignments.

This document defines how a project turns a specialist assignment into a
durable stage result, a departmental handoff, or a scoped continuation. It is
the operational companion to [project organization](15-project-organization.md)
and [Composer runtime](110-composer-runtime.md).

## 1. Three different identities

The runtime keeps these identities separate:

| Identity | Meaning | Example | Durable location |
|---|---|---|---|
| Functional role | The responsibility required by a stage | `methods.validation` | Stage task, feedback, route |
| Department | The project accountability boundary | `methods` | Charter, inbox, backlog |
| Concrete agent appointment | The current department appointment receiving work | `methods.chief` or a configured custom chief | Task owner, inbox address, organization snapshot |
| Specialist appointment | An on-demand role selected for one stage attempt | `research.source-acquirer` | Assignment task, role contract, assignment artifact |

A functional role is not a persistent model process. A department may use a
new worker for each attempt while retaining the same charter and artifact
authority. A custom chief changes the concrete appointment, not the scientific
meaning of `methods.validation`.

The route and roster are defined in
[`scisaurus/runtime/departments.py`](../scisaurus/runtime/departments.py):
`DEFAULT_STAGE_ROUTES` is the single functional ownership map, `stage_route()`
projects a route through the live charter, and `agent_roster()` projects the
eligible chief, specialist, and adversary appointments. The Composer consumes
these projections instead of maintaining a second ownership definition.

## 2. Project organization

| Department | Owns | Stages | Chief appointment | Independent adversary |
|---|---|---|---|---|
| Research | Topic intake, source discovery, identity, evidence and literature gaps | `topic_discovery`, `survey` | `research.chief` | `research.adversarial-reviewer` |
| Methods | Experimental execution, controls, analysis, replay and validation | `experiment` | `methods.chief` | `methods.adversarial-reviewer` |
| Strategy | Scientific interpretation, alternatives, thesis and argument structure | `interpretation`, `argument` | `strategy.chief` | `strategy.adversarial-reviewer` |
| Editorial | Manuscript composition, review cycle, rendering and release proposal | `paper` | `editorial.editor-in-chief` | `editorial.human-scientist-reviewer` |
| Operations | Project-scoped programs, APIs, MCP services and environment health | none | `operations.coordinator` | `operations.operational-adversary` |

The default Operations adversary is `operations.operational-adversary`; the
specialist `operations.operational-verifier` is a separate deterministic role.
Custom v1 chief/adversary names are preserved during migration.

The executive command addresses are outside the departmental roster:

| Address | Authority |
|---|---|
| `executive-command.progress-controller` | Time admission, retry, continuation and stagnation decisions |
| `executive-command.arbiter` | Material disputes, failed handoffs and conflicting findings |
| `executive-command.intent-keeper` | Principal-intent and release-boundary projection |

Command can coordinate and route, but it cannot turn a model response into
accepted evidence. Operations can make an authorized capability usable, but it
cannot set the research question or accept a scientific claim. A producing
agent cannot supply its own independent adversarial verdict.

## 3. Default adaptive route

The normal research-paper mission has hard artifact dependencies and reversible
scientific decisions. It is not a department-by-department queue:

```text
Principal intent / Score
        |
        v
executive-command.progress-controller
        |
        v
candidate portfolio --> survey probe <------> topic refinement
                            |
                            v
      methods experiment <------> evidence or control work order
                            |
                            v
 strategy interpretation <------> discriminating experiment
                            |
                            v
       evidence argument <------> literature / analysis repair
                            |
                            v
          paper + review <------> scoped scientific continuation
                            |
                            v
             executive-command.intent-keeper
```

The arrows are dependency admissions, not unconditional transfers. A stage
can advance only when its declared dependencies are current and its own
acceptance contract passes. When more than one stage is ready, the Composer's
adaptive agenda records all candidates and selects by information value,
active work orders, downstream unlocks, and seeded exploration. For every
provisional result, the independent verifier evaluates admission to the named
next evidence action rather than final-paper maturity, while every unresolved
finding remains attached to the research state. At topic intake, a provisional
hold is carried into the literature brief rather than discarded or falsely
marked resolved; experiment admission remains closed until the survey verdict.

When a survey or experiment exposes a repairable scientific weakness, the
Composer admits a bounded salvage ladder before abandoning the direction. The
ladder changes one repair family at a time across mechanism/observable,
comparison/baseline, and evidence boundary. Each branch has its own continuation
artifacts and lineage. Only after the permitted branches are exhausted, or an
independent source/feasibility check makes the parent unsafe, does the Composer
route a structural pivot. Model reviewers recommend repairability; deterministic
checks and the Composer record the disposition, and no historical candidate is
deleted.

An adaptive failure does not own the worker until the deadline. Composer
persists its retry boundary, yields to any other ready work, and re-scores the
frontier before the next isolated attempt. A one-item frontier is still valid
when a hard artifact dependency leaves no scientifically safe alternative.

For every stage, the runtime records:

| Field | Purpose |
|---|---|
| `role` | Functional owner, such as `methods.validation` |
| `department` | Charter and inbox that own the work |
| `owner_agent` | Concrete chief appointment for the producing stage |
| `adversary_agent` | Independent project appointment used for challenge/verification |
| `depends_on` | Exact upstream stage IDs |
| `output_path` | Inspectable stage result or failure artifact |
| `next_condition` | Evidence required before the next admission |

The v2 stage admission additionally records:

| Field | Purpose |
|---|---|
| `required_agents` | Concrete specialist appointments required by the route |
| `active_agents` | Specialists actually activated for this attempt |
| `verifier_agent` | Independent adversarial appointment; never the chief |
| `assignment_ids` / `assignment_task_ids` | Role-isolated durable task identities |
| `assignment_plan_ref` | Quota/deadline/input-projection reservation artifact |
| `chief_synthesis_ref` | Synthesis artifact authored by the department chief |
| `verifier_artifact_ref` | Independent verdict artifact authored by the verifier |

An exploratory mission may start with `topic_discovery`; a supplied research
object may start at `survey` or `experiment` according to its workflow. The
route does not permit a later stage to bypass a missing upstream acceptance.

## 4. Handoff and continuation protocol

Every stage result passes through the same control-plane sequence:

```text
Composer stage admission
    -> bounded role plan and quota/deadline reservation
    -> one isolated task per active specialist
    -> existing runner/model/tool execution
    -> specialist success/failure/unknown artifacts
    -> chief synthesis
    -> independent adversarial verdict
    -> stage acceptance / hold / failure
    -> Composer decision note
    -> MessageBus envelope
    -> receiving department inbox
    -> typed work-order validation
    -> dependency admission or scoped continuation
```

The handoff is complete only after both the immutable decision note and the
message/inbox records exist. A malformed or unauthorized request is retained as
a rejection artifact; it is not silently dropped and it does not become a
shell command.

Scientific holds use a named closure rather than a whole-project reset:

```text
paper or review finds a gap
    -> research work order (literature / experiment / analysis / interpretation)
    -> owning department receives and activates it
    -> affected stage plus downstream consumers reopen
    -> fresh artifacts are produced and independently checked
    -> argument and paper are rebuilt from the new accepted inputs
```

For a methods request, this means `experiment → interpretation → argument →
paper`; a literature request reopens `survey → experiment → interpretation →
argument → paper`; an interpretation request starts at `interpretation` and
does not repeat unrelated retrieval. Prior artifacts remain immutable
incumbents. A changed work-order objective creates a new task generation and
fences the old one; an exact replay reuses its identity.

## 5. Lifecycle states

### Stage task

```text
created -> queued -> running -> awaiting_review -> completed
                                      |                 |
                                      +-> blocked       +-> stale (superseded)
```

`failed`, `paused`, `cancelled`, and `stale` are terminal or externally
controlled states according to the task contract. `research_expansion_required`
and `review_rejected` are scientific holds at the Composer level: they are not
successful dependencies and cannot release a paper.

The Composer may still let `argument` produce an explicitly provisional,
exploratory draft when the configured progression policy permits it. That
does not weaken publication admission. The `paper` stage has a transitive
scientific release fence: an upstream candidate, verifier hold, provisional
topic or survey admission, unexecuted experiment, or release-blocking/backfill
debt prevents paper dispatch and keeps the paper candidate visibly blocked.
This is a repair boundary, not an automatic discard: the Composer preserves
the current branch and evidence, creates one scoped work order for the
earliest unresolved ancestor, and reruns that closure within the remaining
continuation budget. Only after the affected upstream closure is repaired and
re-verified can the same workflow resume the paper stage; a paper candidate
itself never releases another dependency. Topic refinement uses the bounded
salvage ladder before a structural pivot unless an independent source or
feasibility finding makes the parent direction unsafe.

### Department work order

```text
proposed -> queued -> running -> awaiting_review -> completed
                 \-> blocked / paused ----------------^
```

A work order remains visible while its owning stage is held. It is resolved only
when the owning stage returns an accepted result. A request cannot be closed by
rewriting the manuscript without the requested evidence or analysis.

## 6. Authority and independence matrix

| Action | Producing department | Adversary/reviewer | Composer / Command | Principal |
|---|---:|---:|---:|---:|
| Propose scoped work | yes | yes | route only | may override scope |
| Produce a candidate artifact | yes | no | no | no |
| Challenge a candidate | no | yes | no | no |
| Reopen an affected closure | request | recommend | execute scoped admission | may restrict/stop |
| Adopt scientific evidence | stage contract + independent checks | verify | no | release authority remains |
| Release externally | no | no | propose | yes |

The red-team and manuscript-review paths therefore have two distinct jobs:

1. Before composition, independent scientific reviewers decide whether the
   result/interpretation/argument package is sufficient. A missing result is a
   first-class work order and blocks the writer.
2. After composition, the manuscript panel checks the reader-facing argument,
   requests surgical repairs where appropriate, and can still demand new
   science. Those requests re-enter the same department/work-order protocol.

Neither path is a positivity quota. `accept` requires the declared checks to
pass; disagreement or missing evidence remains visible and routes to the
smallest relevant closure.

## 7. Runtime evidence

The Composer's `output/run.json` and checkpoints expose the current
`organization` projection, including:

- `departments`: validated charters and proposal scopes;
- `agents`: concrete chief and adversary appointments;
- `stage_routes`: functional role, owner/reviewer appointments, and addresses;
- `command_agents`: executive routing addresses;
- `backlog_counts` and `open_work_orders`: live task state rather than a static
  organization diagram;
- `department_activity`: activation and resolution history.

The command namespace also retains the organization/charter manifests,
inboxes, work-order generations, rejection records, decision notes, and linked
message envelopes. These records make a resumed run auditable without treating
the last progress message or a saved `ready` flag as proof of completion.
