# Bounded Scores and Project Time Policy

The shared `run-project` runtime revises mission-specific artifacts under a versioned Score. It supports operational JSON, Python source, and prose through the same task, capability, proposal, review, and atomic integration services used by the paragraph project workflow. Paper production remains the flagship mission.

This is the implemented `bounded-artifact-score-1` contract. The richer activity graph, reactive handlers, and full organizational Score described in [the execution design](40-execution-contract.md#8-score-contract) remain separate development work.

## Execution contract

A run requires a new project directory, public inputs, an explicitly configured model connection, an authorized capacity pool, finite limits, and `live_dispatch_allowed: true`. Preparation helpers leave that flag disabled. The supplied baseline is recorded before external work; its presence does not establish that the mission has produced an improved or verified result.

The runner performs the following bounded cycle:

1. Pin the Score, supplied context, claims, acceptance requirements, baseline units, and document manifest.
2. Check the initial time plan. Register and verify only capabilities used by declared workloads or candidate checks.
3. Execute declared evidence workloads separately for producer and verifier contexts. The routes and inputs are configured; separate execution does not imply different underlying evidence.
4. Obtain a supervisory repair plan and dispatch scoped unit producers with independent review capacity reserved.
5. Validate returned unit formats, preserved literals, source-support records, and scope. Compose the exact candidate through the existing ChangeSet service.
6. Run declared machine checks against exact candidate unit text. Obtain independent unit reviews and a review of the exact combined artifact.
7. Adopt only when required checks pass and governing inputs, capability bindings, and the expected incumbent remain current. Otherwise retain the candidate and baseline, then reassess within the time and round limits.

The Score is an immutable governing artifact. Model output cannot change its domain, capabilities, checks, protected values, or deliverable structure. A favorable review cannot override a failed machine check. A program's successful exit establishes operational completion; its substantive result still has to satisfy the mission's acceptance requirements.

## Configuration

The [operating-policy example](../config/operations-run.example.json) is the complete configuration reference for this slice. Common fields select project identity, objective, supplied context, model connection, and finite limits. `claims` contains explicit required statements, and each unit binds the relevant claim IDs; an empty list is permitted when no claims are declared.

The `deliverable` object has `id`, `title`, `output_file`, and `groups`. Each group has an ID, title, and ordered `units`. Each unit declares:

| Field | Contract |
|---|---|
| `id`, `kind`, `text` | Stable identity, supported format, and baseline content |
| `editable`, `objective` | Explicit write scope and repair purpose; immutable units have a null objective |
| `required_literals`, `claim_ids` | Values and meanings preserved within that unit |
| `output_file` | Optional separate file derived from the accepted unit; null for embedded-only content |

Supported kinds are `paragraph`, `list_item`, `json`, and `code`. Paragraphs/list items occupy one structural line. JSON is parsed with duplicate-key and non-finite-value rejection. Code is compiled as a standalone Python module without executing it; this rejects invalid top-level statements but does not prove behavior. A mission requiring executable tests must declare an appropriate configured program check.

At least one unit must be editable. Groups, membership, order, immutable units, and their bindings are preserved by this revision contract. The combined export is Markdown with appropriate code fences; JSON and Python units can also be exported as separate files. Individual files preserve the verified UTF-8 unit bytes without adding a newline. Output paths are normalized relative paths under `output/` and cannot collide with one another or runtime reports, including case and Unicode equivalents.

The `score` object contains:

| Field | Meaning |
|---|---|
| `id`, `revision`, `domain` | Stable mission-contract identity and domain context |
| `checks` | Additional independent review requirements, each with `check_id` and `requirement` |
| `capabilities` | Explicit adapter profiles, representative probe arguments, and public software identity files |
| `workloads` | Configured capability calls that supply execution evidence |
| `candidate_checks` | Local-program predicates evaluated on exact staged unit text |
| `stage_seconds` | Initial duration estimates for the six supported execution stages |

For a candidate check, the runner copies its declared `input`, inserts the staged unit's exact text at `text_field`, executes the selected capability, follows `result_path` in the returned JSON object, and compares the observed value with `expected` using canonical JSON. The check pins candidate, unit, Score, and execution evidence. The operating-policy example requires the schema checker's `valid` field to equal Boolean `true`.

The earlier `document.sections[].paragraphs` configuration remains supported through normalization to the `paper-revision` Score. It retains its original output and retrieval behavior. Time-planning CLI options require an explicit versioned Score; the earlier format retains its existing elapsed limit.

## Selected capabilities and real programs

The shared adapter registry contains `crossref`, `mcp_fetch`, `local_program`, and the literature workflow's `openalex` adapter. This Score selects its declared supported workloads. A declaration is selected only when a workload or candidate check refers to it. Profiles and verified bindings remain project-scoped; changed software identity or an invalidated session requires fresh operational readiness.

`local_program` executes a configured, fixed argument vector without a shell. It accepts one JSON object on stdin and requires one JSON object on stdout. The adapter records input/output captures and hashes, command identity, exit status, and timing; byte and elapsed limits bound execution. The runner supplies a project-owned working directory and a restricted process environment.

This process arrangement is **not an operating-system sandbox**. A configured executable runs with the host account's permissions. The declaration selects an already authorized program; it does not authorize arbitrary model-generated commands, create isolation guarantees, or automatically install missing software.

The included checker uses `jsonschema==4.26.0` and `Draft202012Validator`. It accepts `{ "text": "...", "schema": { ... } }` and returns `{ "valid": true, "errors": [] }` or a failed-check result with structured errors. Invalid candidate JSON or schema violations return exit 0 with `valid: false`. Invalid request envelopes, invalid schemas, and unresolved external references fail the program with exit 2. Its empty, non-retrieving registry permits embedded references without fetching external resources. The validator interface and error iteration follow the [jsonschema validation documentation](https://python-jsonschema.readthedocs.io/en/stable/validate/).

**Generality regression rule:** a non-paper mission must produce its declared artifact forms without implicitly acquiring paper terminology, academic checks, or unselected academic/network tool workloads. The local-only operating-policy fixture must record zero Crossref, MCP Fetch, or other undeclared network-tool calls. Configured model-provider traffic is distinct from tool workloads and remains governed by the model connection.

## Result targets and hard deadlines

`stage_seconds` requires positive initial estimates for `setup`, `supervision`, `production`, `unit_review`, `integrated_review`, and `reassessment`. Production and unit-review estimates use the number of editable units and available worker slots to estimate batches. The initial schedule includes one production/review pass; reassessment is conditional.

An optional `time_policy` specifies:

| Field | Meaning |
|---|---|
| `first_result_seconds` | Target elapsed time for the first newly verified and adopted combined result |
| `target_seconds` | Completion target; new production and reassessment must fit together with required review |
| `hard_seconds` | Execution cap, no greater than `limits.wall_clock_seconds` |

The fields must satisfy `first_result_seconds <= target_seconds <= hard_seconds`. Omitted values derive from the stage schedule and configured elapsed limit. The operating-policy example initially estimates 240 seconds for one pass and has a 600-second hard cap. These are configured assumptions, not measured service-level guarantees.

Each stage admission reserves estimated time for the proposed batch and its required independent reviews. New production includes the existing unverified backlog. At or beyond the completion target, the runner declines new production/reassessment while necessary verification can continue if it fits within the hard cap. Task concurrency reservations and time forecasts are separate controls.

If the complete initial schedule exceeds the hard cap, the run records a blocker before dispatching external work. A target that is shorter than the estimate is visible as initially infeasible; later admission can defer work that cannot fit. Deferred work never weakens the review contract or converts the baseline into a completed result. The native runtime enforces the hard deadline and reconciles cancellation and unfinished work.

Observed durations update each stage estimate to the larger of its configured seed and observed maximum. Reports preserve configured values, observation counts/durations, and estimate provenance. Observed maxima are not upper bounds, so time admission is conservative planning rather than a completion guarantee.

Unit-review seeds must include the declared machine-check workload. Candidate program checks currently execute serially before model reviews, and their cost is not learned as a separate timing stage. The hard deadline still bounds their execution. Acceptance rechecks runtime admission and grant expiry after evidence validation and before committing the accepted-head transaction; expiry rolls the transaction back.

The first-result state is `pending`, `missed`, `available_on_time`, or `available_late`. Only adoption after the required machine, unit, and integrated checks marks a first newly verified result. Recording the supplied baseline, finishing a producer call, or passing a tool probe does not mark it. A target miss stays visible even if a result becomes available later.

## Prepare and run the operating-policy example

Install the repository's pinned runtime once, then prepare a new configuration:

```bash
sh scripts/setup-runtime.sh
python3 scripts/prepare-operations-config.py --output /tmp/operations-run.json
```

The helper inspects installed package metadata, fixes the checker command to the repository `.venv` interpreter, and records the checker, virtual-environment configuration, and public runtime files from `jsonschema`, `referencing`, `attrs`, `rpds-py`, and `jsonschema-specifications`. These scoped identity pins do not attest the whole host. The helper installs nothing, refuses overwrite, and preserves the inert model and dispatch settings.

Configure the authorized endpoint, model name, optional credential environment-variable name, and `live_dispatch_allowed`. Use a new project directory:

```bash
python3 -m scisaurus.cli run-project /tmp/operating-policy \
  --config /tmp/operations-run.json \
  --first-result-seconds 300 \
  --target-seconds 450 \
  --deadline-seconds 600
```

CLI values override their corresponding `time_policy` fields for that run. `--deadline-seconds` sets `hard_seconds`; it cannot expand the configured wall-clock ceiling. These example targets are illustrative configuration, not a measured prediction.

The fixture repairs a policy for `artifact-gateway` at `127.0.0.1:8080`: three total attempts, exponential backoff from 200 ms to a 2000 ms maximum with full jitter, 120 requests per minute, and burst capacity 20. Independent review also checks that the guide expresses the same values. An immutable paragraph preserves the deployment boundary.

The output directory contains:

| File | Contents |
|---|---|
| `policy.json` | Current incumbent JSON unit |
| `operations-guide.md` | Combined current artifact, including the policy and guide |
| `run.json` | Exact refs, candidates, verdicts, capability states, time plan, usage, and blockers |
| `report.md` | Readable run, proposal, operations, and timing report |
| `progress.json` | Latest native checkpoint with current verified changes, blockers, and time state |

Inspect `status`, candidate adoption, and `time_plan.first_verified_result` together. A blocked run can export the preserved baseline; file existence alone is not success. CLI exit 0 means accepted, exit 2 means rejected configuration, and exit 3 means the run did not accept a candidate. Release status remains `not_released`; this workflow does not deploy the service or measure live enforcement.

## Verification boundary

Automated coverage includes Score validation, non-paper runner integration, real local-program execution, invalid candidate/schema handling, offline references, inert preparation, capability drift, and time-admission behavior. The real checker tests distinguish schema validity from execution success; model-response fixtures exercise orchestration without proving a provider-backed result.

### Observed operating-policy run, 2026-09-10

Run `5220ba95ddd7454298dde5fe02ff6117` used Score revision 2, Qwen `qwen3.8-27b` through an authorized external compatible endpoint, and the installed local JSON schema checker. It accepted the exact combined candidate in 108.16 seconds, within the configured 240-second first-result/completion targets and 600-second hard cap. This is one observed execution, not a latency guarantee.

| Evidence | Observed result |
|---|---|
| Candidate | `artifact:strategy/documents/operations-guide@2`; one accepted round |
| Actual calls | 6 model calls, 4 local-program calls, 0 retrieval calls |
| Model usage | 18,153 input tokens; 18,404 output tokens |
| Machine validation | Baseline: 7 schema errors; accepted policy: 0 |
| Integration | 23 passing checks; unchanged protected unit, structure, and dependencies |
| Integrity | 54 matching source fingerprints, 113 verified artifact bodies, valid 242-event chain |
| Export | Policy bytes exactly match the accepted unit; guide explicitly includes the initial attempt in the total-attempt limit |
| Completion | First verified result available on time; no remaining reservations; not released or deployed |

Local evidence is retained under `.runs/axion-scored-20260910T131609Z/`, including `output/run.json`, the two delivered files, and `independent-validation.json`. The run report SHA-256 is `905839d6a9bf9b9f86ec48849f9d2a527c81cad6324a5f1be60b6f76b18c5a9b`.

The runtime regression suite passed 270 tests; the six real checker/preparation tests were rerun after clarifying the mission's total-attempt semantics. An additional CLI execution with a one-second hard cap rejected the infeasible plan before any model or program call and left the first verified result unset. Independent checks reproduced and verified fixes for post-verification export mutation, expiry during adoption, JSON type confusion in execution evidence, case-insensitive path collisions, and invalid standalone Python statements.

Independent roles in this run used the same model, and their passing verdicts do not establish expert judgment. An earlier candidate's ambiguous use of “retry attempts” survived model review and was identified during separate semantic inspection. Score revision 2 requires explicit total-attempt wording. This evidence supports controlled non-paper execution and its timing/traceability contracts; it does not validate autonomous research novelty assessment, whose [survey prerequisite](75-literature-survey-score.md) remains planned.

```bash
python3 -m unittest scisaurus.tests.test_scores \
  scisaurus.tests.test_scored_project scisaurus.tests.test_programs \
  scisaurus.tests.test_json_artifact_program scisaurus.tests.test_time_policy -v
```

A live model mission needs its own retained execution and acceptance evidence. The previously measured paragraph and multi-paragraph runs remain documented in [60-paragraph-runtime.md](60-paragraph-runtime.md) and [65-project-runtime.md](65-project-runtime.md). This slice does not implement arbitrary task graphs, automatic program installation, deployment, full workflow plugins, or the complete paper-to-PDF pipeline.
