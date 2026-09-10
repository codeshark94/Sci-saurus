# Sci-saurus — Execution Contract

> **Version:** v0.8 · **Date:** 2026-09-10 · **Status:** normative proposed interface specification, not a software implementation.
> Concepts and decision authority come from `00-SSOT.md`. This file defines the shared field vocabulary and lifecycle rules used by the architecture and examples. Changing an interface requires a `schema_version` change and a documented migration.

## 1. Common conventions

All records include `schema_version`, a stable ID, `project_id` when project-scoped, an authenticated actor where applicable, and an ISO 8601 timestamp with timezone. Runtime IDs must be unique; readable IDs in examples are fixtures, not a production ID-generation algorithm.

An **ArtifactRef** has the form `artifact:<namespace>/<logical_name>@<positive_integer_version>`. A manifest carries the corresponding SHA-256 body hash. Inputs use exact references; `latest`/HEAD may be resolved when planning but never remain an unpinned execution dependency. Human approvals use an exact artifact reference and hash.

ArtifactRef resolution always includes the authenticated `project_id`; the string alone is not a globally accessible key. All project-scoped records, grants, capability bindings, contexts, environments, and calls bind that identity. A cross-project import requires explicit data authority and a new local artifact with source provenance; sharing a worker or tool binary does not grant cross-project access.

An artifact version has immutable metadata and bytes. A new version can share its parent's bytes but change immutable descriptive metadata; that is still a new version. Accepted-head/status changes are events, not artifact body mutation.

`must` indicates a contract requirement; `may` indicates permitted behavior. An unset budget, capability, or authorization is not infinite permission. Example values are proposed configurations, not approval to spend or use a service.

Canonical structured record hashing uses a declared serializer/version. Initial proposal: UTF-8 JSON with sorted keys, no insignificant whitespace, and no non-finite numbers; use integer resource counters or decimal strings for exact quantities. Hash original uploaded/source file bytes as received, without normalizing their content. The serialized record excludes its own hash field when computing that hash.

## 2. Authority and intent records

| Entity | Required content | Invariants |
|---|---|---|
| `PrincipalIntent` | `intent_id`, `revision`, original instruction references, interpretation clauses with source/evidence, objectives, ordered priorities, hard constraints, non-goals, delegation, unresolved assumptions | Proposed and active states are distinct; only the Principal can authorize material intent changes |
| `Mission` | `mission_id`, exact `intent_ref`, deliverable contract, input refs, acceptance-contract ref, resource envelope, capabilities, Score ref | Must not grant authority absent from approved intent; must state missing information |
| `HumanApproval` | `approval_id`, approving identity, approval kind, exact target refs/hashes, intent/mission/Score refs, decision, scope, timestamp | Non-transferable to changed targets; declined/pending is not approval |
| `Delegation` | delegating authority, permitted task classes/namespaces, resource-policy ref, data/provider constraints, escalation triggers | Does not permit changing facts, expanding resource authority, or modifying itself; may authorize automatic window renewal |

An intent interpretation clause records `basis = explicit | inferred | unresolved`. Inferred clauses cannot become hard constraints or new authorities silently. The current intent is a pointer to an approved immutable revision, not an editable text file.

Each Project instantiates the organization rather than sharing one mutable research organization across all projects. [Project Organization and Operations](15-project-organization.md) defines the project ExecutionProfile, on-demand Operations Cell, environment/service ownership, and actual execution-evidence requirements. Profiles may delegate pinned project-local installation/build and use of approved operation classes. Setup, domain analysis, artifact mutation, and external effects remain distinct authorities.

### 2.1 ResourcePolicy and AllocationWindow

A ResourcePolicy pins the authorized endpoint/model pool, data policy, `allocation_mode = capacity_pool | metered`, finite attempt/context/lease limits, measured capacity-profile ref, checkpoint interval, renewal authority, and mission completion policy. The legacy `resource_envelope` field now contains this policy binding rather than mandatory lifetime token/call caps. Missing configuration is an error; explicit `not_applicable` differs from unknown.

For `capacity_pool`, capacity reservations are finite and can renew automatically within delegation. A mission-wide token/call ceiling is optional and represented explicitly as `lifetime_limit = absent_by_policy | bounded`, with values required for `bounded`. For `metered`, an explicit finite expenditure ceiling is mandatory. Both modes can include explicit elapsed-time, provider, query, storage, and mission limits. A paid tool used by a capacity-pool mission still has its own metered envelope.

An AllocationWindow contains `window_id`, policy/delegation refs, prior-window ref when renewed, start/end times, available capacity, reserved independent verification capacity, committed task reservations, cumulative mission usage, and the authorizing SupervisionDecision. States are `open`, `draining`, `closed`, or `revoked`. Renewal creates a new window; it does not mutate the old one or erase stagnation/accounting history.

Reservations have unique IDs, finite quantities, expiry, and attempt bindings. Before dispatch, atomically check active policy, pool-wide capacity including overlapping windows, scoped provider limits, and outstanding reservations. A task may cross a planning checkpoint only with an explicit still-valid reservation. Window renewal cannot revive an expired task lease. Provider work of unknown completion retains capacity/cost uncertainty until cancellation or reconciliation is confirmed.

Renewal requires a recorded continuation rationale and reassessment condition under current delegation. It cannot raise a hard limit or grant another provider/data permission. When a window ends and renewal is not yet decided, admission waits while the scheduler publishes a truthful pending checkpoint; absence of a supervisor response cannot silently authorize more work.

## 3. Artifact and evidence contracts

### 3.1 ArtifactVersion

Required fields:

| Field | Meaning |
|---|---|
| `artifact_id`, `version`, `artifact_ref` | Logical identity and unique committed version |
| `artifact_type` | Registered type; no unknown critical type accepted by default |
| `owner`, `author` | Namespace owner and authenticated producing role |
| `body_hash`, `body_media_type`, `body_size_bytes` | Exact immutable object identity |
| `parents` | Prior versions in the artifact's revision/branch lineage |
| `inputs` | Version-pinned references used to produce the content, classified as `premise` or `subject` under section 4.1 |
| `intent_ref`, `mission_ref`, `score_ref` | Governing context |
| `task_id`, `attempt_id`, `context_ref` | Execution provenance; may be explicit import/human records rather than model attempts |
| `created_at`, `schema_version` | Time and contract version |

`parents` is not interchangeable with `inputs`: a draft's parent is its previous draft; its inputs include evidence and outline. Versions are allocated transactionally and monotonically per logical ID, including concurrent branches. Version numbering does not imply acceptance order.

Lifecycle projections include `candidate`, `accepted`, `rejected`, `superseded`, and `stale`. No role edits a historical manifest to change these labels.

### 3.2 EvidenceRecord and Claim

An EvidenceRecord includes `evidence_id`, `source_kind = user_result | literature | declared_requirement`, exact source artifact ref/hash, location, excerpt/result value, extraction method, scope/conditions, access limitations, and any already existing source/extraction-check refs with their scope. A numeric value includes units when meaningful. A figure can be evidence with a figure locator; a visual interpretation must state that extraction method.

The evidence record does not require backlinks to its future acceptance verdict. Separate Verification/ReviewCoverage records name the exact EvidenceRecord as a subject; append-only events and a derived index expose the evidence-to-check association. Accepting evidence changes its lifecycle projection, not its bytes/hash. Earlier source/extraction checks do not replace required exact-target evidence review.

A Claim includes `claim_id`, exact statement, scope/conditions, claim kind (`observation`, `interpretation`, `hypothesis`, `background`), evidence links, limitations, and required verification. Each evidence link carries `relation = supports | partially_supports | contradicts | context_only` and an interpretation justification.

A Claim does not certify its own support. A verifier assesses the relation against the exact source/result. Metadata-only or abstract-only access must remain identifiable. Speculation cannot silently become an observed result in a later section.

A ResultsPackage is a manifest of supplied figures, tables, metric definitions, procedures, findings, provenance, and missing fields. Intake acceptance means the input is usable within the stated scope—not that all experimental conclusions are independently proven.

### 3.3 Web intelligence records

A `SearchCampaign` includes `campaign_id`, generation, owner department/role, objective, governing task/issue/claim refs, concept decomposition, query families, authorized source classes, languages, date bounds, positive/counterexample routes, citation/author/project chasing policy, coverage targets, stopping conditions, resource binding, and status. `resource_binding` resolves to the active policy/window; actual calls require finite reservations before dispatch. An illustrative template may use `runtime_required` for unresolved bindings, which blocks live dispatch. It is versioned; reopening creates a new generation linked to the prior campaign.

A `QueryRecord` includes the exact normalized query, query family, provider/adapter class, source class, locale/language, filters, execution timestamp, request/result identifiers where available, result count/continuation state, resource usage, and outcome (`results`, `empty`, `failed`, `blocked`, `rate_limited`). Empty and failed searches remain first-class records.

A `DiscoveryRecord` includes the discovering query/referrer, URL or stable identifier when present, title/summary metadata, source class, discovery rank/route, access status, deduplication candidate, and the task/claim/issue that made it potentially relevant. A discovery is a lead, not certified evidence. Search snippets and generated summaries must be labeled as such.

A `ReferenceCard` canonicalizes identity and aliases. A `SourceCapture` records the authorized representation actually inspected: source ref, resolved URL/identifier, retrieval timestamp, media type, version/date when observable, body/excerpt hash, locator map, access/license limitations, and capture method. If full content is unavailable, the capture declares `abstract_only`, `metadata_only`, `snippet_only`, `partial`, or `unavailable`.

A `CoverageReport` binds a SearchCampaign generation to attempted/completed query families, source classes, languages, time ranges, citation directions, unavailable routes, saturation observations, known gaps, and termination reason (`coverage_met`, `diminishing_return`, `budget_exhausted`, `access_blocked`, `needs_human_scope`). `budget_exhausted` refers to an applicable hard limit, not a renewable-window checkpoint. Saturation is a stopping observation, not proof of exhaustiveness.

Promotion path for scientific evidence is `DiscoveryRecord → ReferenceCard → SourceCapture → EvidenceRecord`. The Research namespace owns canonical scientific reference/source publication; other departments may publish scoped discoveries and request promotion. Editorial may own captured official venue requirements. A material Claim cannot cite a discovery or search snippet as though it were a verified source capture.

### 3.4 Structured artifacts and revision

`ContentUnit`, `DocumentManifest`, `ChangeRequest`, and `ChangeSet` use the existing ArtifactVersion envelope. `EditGrant` is an authenticated control-plane capability with an event-backed lifecycle. Their required fields and application rules are defined in [Structured Artifacts and Surgical Change Control](45-artifact-change-control.md); this is part of the execution contract.

Units have stable identity independent of location; manifests pin exact unit versions, topology, and assembly dependencies. A modification requires an exact purpose/baseline/scope grant and complete before/after mutation validation. Paragraph prose, structural edges, citation/claim bindings, unit contracts, and shared assets are separate grant targets. Candidate staging does not confer acceptance. Old approval cannot be transferred without the exact dependency/coverage conditions in the change contract.

## 4. Task and invocation contracts

### 4.1 Task

Required fields are `task_id`, `kind`, `owner_dept`, `assigned_role`, objective, governing refs, exact input refs, output type/namespace requirements, acceptance criteria, dependencies, capability set, resource reservation, context policy, retry policy, idempotency key, and causal origin.

Model-using tasks also state `mission_contribution`, `approach_family`, `expected_useful_output`, `evaluation_method`, `reassess_at`, and `activity_mode = exploratory | commitment`. A long investigation declares intermediate milestones and a reassessment horizon; estimates are not evidence of achieved progress. Synthesis and alternative construction are production tasks; supervisory diagnosis is a review/selection task. These do not require a second workflow engine.

Every task reference pins a version and `purpose = premise | subject`. A `premise` supplies a factual or governing basis and has `required_state = accepted | provisional_allowed`. Only an authorized exploratory task may rely on a `provisional_allowed` premise; its substantive outputs remain provisional candidates. Commitment requires the accepted, current premise closure or recorded revalidation against replacement inputs.

For a coupled atomic change, acceptance evaluates the transaction's proposed post-state: currently accepted external premises plus independently verified member versions that will be accepted together. Factual-premise closure must be acyclic and grounded in admissible evidence; structural/expression links and subject-review backlinks cannot supply self-support. The pre-state need not already accept those group members. Commit member acceptance, relevant heads, the DocumentManifest, and events atomically, or leave all unchanged. This does not allow a running task to consume unstated provisional inputs.

A `subject` is the material being examined, such as a candidate, critique, or contested decision. It must be accessible and version-pinned, but does not need prior acceptance. Review, response, adjudication, verification, and selection tasks may inspect an unaccepted subject and produce an authoritative, independently supported judgment about it. They cannot silently use its disputed assertions as accepted premises. Accepting a judgment does not itself accept its subject; promotion still requires the appropriate selection and gate transitions. All reference changes can stale affected judgments, while premise validity determines support admissibility. Candidate-frontier membership never implies acceptance. Publishing a later version does not transfer prior review or approval.

`kind` is one of `production`, `retrieval`, `review`, `response`, `adjudication`, `verification`, `selection`, `human`, or `service`. A commitment production task's deliverable requires acceptance. Exploratory and protocol/service tasks complete when they have validly published their scoped output; that publication does not certify the output's substantive judgment. This distinction prevents an infinite reviewer-of-every-review recursion.

A task proposal must describe the desired effect; a message expressing an opinion is not automatically a task. Task admission checks intent alignment, authority, capabilities, duplicate cause, budget, and readiness.

Artifact-modifying tasks also pin `change_request_ref`, `edit_grant_ref`, expected base manifest/unit refs, allowed operations, preservation requirements, and independent verification conditions. Pure retrieval/review tasks do not receive edit grants by default. Creation/structural alternatives follow explicit unit plans; a new artifact ID cannot bypass the modification/adoption policy. The service rechecks grants at application and acceptance, not only dispatch.

Retrieval tasks additionally pin their `SearchCampaign` generation or create one under an authorized role. Tool permissions specify allowed retrieval adapters/source classes, outbound query/data policy, follow-link/citation-chase depth, download/capture permissions, language/region settings, and separate model/tool budgets. The scheduler prevents a role from silently using an unapproved provider or uploading protected context to a retrieval service.

### 4.2 Task lifecycle

```text
proposed → queued → running → awaiting_review → completed
    │         │        │              │
    └─ rejected        ├─ blocked      ├─ queued (authorized revision)
                       ├─ failed      ├─ blocked
                       └─ paused      └─ stale
```

`cancelled` is available from any nonterminal state. Rejected/failed/cancelled task records are not reused as new tasks. Reopening creates an explicitly linked task generation. A stale artifact/approval may require a new task while retaining the original completed task history.

`blocked` includes a typed reason: missing evidence, missing dependency, missing authorization, resource limit, or unresolved material dispute. `paused` represents user/operational suspension. Neither is success. Commitment production cannot reach `completed` merely because the provider returned text. Exploratory completion requires its scoped output/evaluation record, with provisional status intact.

### 4.3 TaskAttempt

An attempt pins its task generation, inputs, ContextPackage, model/backend configuration, prompt/role manifest version, allowed tools, resource reservation, lease, and external request IDs. Attempts end as `succeeded`, `failed`, `cancelled`, or `result_unknown`; a succeeded invocation may still produce a rejected candidate.

Record measured token/call/time usage and distinguish estimates from actuals. Unknown external completion is not a zero-cost failure. Do not automatically rerun a non-idempotent external effect without reconciliation or authorization.

Program/MCP attempts also record the effective environment manifest, program/package/commit/image versions, entry point and redacted parameters, host/runtime identity, input/output hashes, process/request state, exit/error observations, and operational/domain validation refs. An `EnvironmentManifest` and execution report use ArtifactVersion; operational setup/execution reuse Task/TaskAttempt rather than a second workflow system. Tool output is staged and cannot directly change accepted artifacts.

### 4.4 ContextPackage

Fields include intent clauses, task objective, exact input/evidence refs with premise/subject classification, required coverage, relevant issue/message refs, role policy, retrieval selection, active SearchCampaign/coverage refs when applicable, token limit, and all exclusions/truncations. The supplied context labels provisional premises and disputed subjects explicitly. For initial adversarial review, exclude the producer's private conversation while preserving the technical context needed for a fair review.

The ContextBuilder either produces a complete authorized package or reports a context insufficiency. It cannot hide a required conflicting source to fit the budget. Record a digest of the actual material supplied to the model so review coverage is auditable.

For modifications, include unit purposes, exact target versions, neighboring/structural context, required claim/evidence/definition refs, protected content, grant scope, and inspection-only material. Distinguish broad consulted context from relied-on premises and mutable targets; reading a source or the whole manuscript cannot expand write authority. Missing context triggers additional authorized retrieval or task decomposition, not a broad rewrite fallback.

## 5. Message contract and effect semantics

### 5.1 Envelope fields

`message_id`, `project_id`, `type`, `from`, `to`, `subject`, concise body, artifact/evidence refs, `task_id` if applicable, `issue_id` if applicable, `correlation_id`, `causation_id`, `reply_to`, `idempotency_key`, priority, created timestamp, and optional expiry are required by type or explicitly null.

Retain the original types: `request`, `review`, `data`, `critique`, `decision`. An action payload is a proposal to an allowlisted handler, never an arbitrary executable command. Message bodies default to English.

### 5.2 Delivery state

The immutable message body is separate from per-recipient delivery state:

```text
pending → leased → acknowledged
             ├─ retry_pending → leased
             ├─ dead_letter
             └─ expired
```

A lease has an owner, fencing token, and expiry. A stale worker cannot commit effects with an expired token. Acknowledgement means an accepted disposition/effect has been durably recorded, not merely that the message was read.

An acknowledged request has `disposition = scheduled | linked_existing | deferred | rejected | escalated`. Deferred requests remain visible with a reason and next review condition. Critical findings cannot disappear by silently expiring.

At-least-once delivery is the contract. A unique effect key plus transactional state transition prevents repeated delivery from committing the same effect twice. This does not make an external API call exactly once.

### 5.3 Outbox and causality

Publish an artifact/transition and its required outgoing messages in one local transaction. Dispatch from the outbox after commit. Recover by retrying undispatched entries. Per-issue causal order and expected-state checks prevent stale replies from resolving a newer issue generation; global delivery order is not assumed.

## 6. Review, issue, and adjudication records

### 6.1 ReviewCoverage

Required fields: `review_id`, reviewer role/context ref, exact target refs, intent/mission/acceptance refs, checks attempted, checks completed, exclusions/access failures, outcome, critique refs, actual usage. An empty critique list is allowed only with an explicit reason-bearing outcome.

Valid outcome values: `objections_found`, `no_valid_objection_found`, `insufficient_evidence`, `review_failed`. Coverage cannot include a target version the reviewer did not receive or inspect through a logged tool call.

### 6.2 Critique

| Field | Required meaning |
|---|---|
| `critique_id`, `issue_id` | Immutable critique identity and stable underlying defect identity |
| `target_ref`, `target_location` | Exact contested artifact version/location |
| `criterion_ref` | Violated approved requirement or challenged claim |
| `allegation` | Specific defect, not generic dislike |
| `basis` | Evidence refs and/or explicit counterexample/reproducible check |
| `material_impact` | Why the defect matters for the mission |
| `proposed_severity` | `blocking`, `major`, or `minor`; adjudication may differ |
| `resolution_condition` | What would resolve or refute the objection |
| `verification_method` | How the condition can be checked |
| `uncertainty` | Known gaps; no implied calibrated probability |

A source citation is not obligatory for an internally demonstrable logical contradiction. The burden is specificity and valid support. An investigation request that has not established a defect remains uncertain and cannot be counted as a confirmed flaw.

### 6.3 Stable Issue identity

Deduplicate across revisions using claim/requirement, location/concept, and causal defect; artifact version alone must not generate a new issue. Preserve distinct critics' records while linking duplicates to a single issue. Material new evidence creates an issue generation/reopening event, not deletion of earlier decisions.

Proposed lifecycle:

```text
registered → triaged → awaiting_response → adjudication_pending
                                       → upheld_open → repair_submitted
                                                     → verification_pending
                                                     → resolved_verified
```

Other adjudicated outcomes: `rejected_invalid`, `duplicate`, `rebutted`, `needs_evidence`, and `risk_accepted`. A verification failure returns the same issue to `upheld_open` with a new evidence record. A closed issue reopens only with a new affected version or materially new evidence and a documented reason.

Triage checks admissibility; it is not proof of truth. Pending potentially blocking issues can hold the affected gate for bounded adjudication, but no reviewer obtains an unlimited veto simply by choosing `blocking`.

### 6.4 Response, Adjudication, Verification

A Response references the critique, target version, stance (`accept`, `rebut`, `request_evidence`), supporting refs, and any candidate patch/version. It is authored by the responsible producer or designated responder.

An Adjudication references both sides, the applicable criterion, evidence considered, validity/severity decision, required next action, and concise rationale. It states conflict-of-interest checks. A decision of uncertainty is allowed; a vote count is not a scientific justification.

A Verification references the upheld issue, agreed resolution condition, candidate and baseline, exact checks executed, results, regressions, remaining uncertainties, and verifier independence. Closure can reference it only if it passes the required condition. High-severity disputed closure cannot rest solely on the producer or original critic.

## 7. Progress and candidate selection

A ProgressRecord includes `action_id`, causal task/issue refs, `baseline_ref`, `candidate_ref` when present, governing intent/evidence/evaluation versions, resource estimates and actuals, validated changes, newly discovered issues, required-coverage assessment, regression results, uncertainty changes, and selection outcome.

`progress_kind = artifact | information | both | none`. `selection = promote | retain | incomparable | needs_human_tradeoff | not_applicable`. `artifact`/`both` requires supporting verification and an accepted improvement; a positive self-score cannot satisfy it.

Rebutting an invalid objection may improve review accuracy but does not necessarily change the artifact. Accepting a risk or lowering severity does not count as repairing it. Multiple textual edits that resolve one underlying defect count as one causal repair, not many rewards.

Candidate comparison uses the same acceptance contract for both versions. If new evidence or intent changes the contract, reevaluate both and record the new basis; do not compare scores from incompatible evaluations. Preserve required coverage so “remove every substantive claim” cannot win by eliminating criticism.

Information progress additionally requires an affected question/decision ref, previous state, new evidence/verification refs, resulting change, and causal discovery identity for deduplication. `none` is valid and must be recorded when an activity has produced no verified change. Execution health is a separate diagnostic, never a third scientific-progress reward.

### 7.1 SupervisionDecision

Required fields: exact mission/intent/Score/acceptance refs, observed state cutoff, frontier/input refs, candidate activities considered, chosen portfolio, rejected/deferred alternatives with reasons, expected useful observations, resource-policy/window refs, rationale, `reassess_at`, independence/conflict checks, and `disposition = continue | reallocate | investigate | pause | complete_proposed`.

The decision may authorize work only under existing delegation. The scheduler validates it mechanically before admitting tasks or renewing a window. Review triggers include consequential commitment/closure, contradictory observations, repeated failed allocation predictions, and sampled routine decisions. Sample policy and escalation horizon are explicit mission configuration; the supervisor cannot exempt itself. Mandatory substantive review cannot be replaced by sampling.

### 7.2 ProgressCheckpoint and frontier

A ProgressCheckpoint contains checkpoint ID, scheduled/actual times, event cutoff, governing versions, incumbent ref or explicit absence, admissibility, material alternatives and their status, verified ProgressRecord refs since the previous cutoff, deduplicated information changes, unchanged intervals, remaining required coverage, blockers, cumulative resource actuals/reservations, queue/worker health, next SupervisionDecision ref or pending status, and the next checkpoint time.

The native service publishes checkpoints independently of model-call completion. A missed or late supervisory judgment remains visible and cannot be replaced by an invented decision. Checkpoints are durable status artifacts; human notification cadence is a separate mission preference.

The candidate frontier is a derived view of artifact versions, approach families, evaluations, and selection events. Retain materially different unresolved alternatives with a reason; do not rank uncalibrated self-scores as truth. Only an accepted, current candidate may be the incumbent. Newly invalidated evidence can remove incumbent admissibility without erasing its history.

Stagnation is assessed against mission-configured observation intervals and long-activity milestones using artifact and information progress. Continuation records what will change, or why the existing investigation remains justified through its next milestone. Task IDs, window rollover, and approach renaming cannot reset the underlying causal history. Repeated unsupported continuation triggers independent diagnosis, then reallocation/pause under the existing authority rules.

## 8. Score contract

The examples use a small declarative vocabulary rather than an arbitrary executable expression language.

### 8.1 Required top-level fields

| Field | Meaning |
|---|---|
| `schema_version`, `score_id`, `revision` | Exact Score identity |
| `status` | Proposed/approved configuration state |
| `language` | Development, writing, and source-preservation policy |
| `department_ids` | Active functional departments |
| `command_roles` | Intent Keeper, Composer, Arbiter, Progress Controller |
| `principal_policy` | Intent binding and human approval requirements |
| `runtime` | Measured worker-capacity profile, finite execution limits, and context policy |
| `resource_envelope` | ResourcePolicy binding; renewable capacity or metered mode; explicit hard limits and renewal authority |
| `capabilities` | Tool, network, provider, source-class, web-intelligence, data-transfer, capture, and execution boundaries |
| `review_policy` | Required adversary map, blindness, validity/rebuttal/verification rules |
| `loop_policy` | Finite operational retries/leases, causal deduplication, stagnation reassessment, continuation and escalation conditions |
| `progress_policy` | Wall-clock checkpoint cadence, long-activity milestones, decision review triggers, completion and notification policy |
| `revision_policy` | Stable unit types, purpose-scoped grants, protected content, structural operations, impact review, and atomic integration |
| `acceptance_contract` | Required outputs/coverage, non-waivable failures, comparisons |
| `stages` | Readiness dependencies, owners, required outputs, gates, and failure routes |
| `event_handlers` | Allowlisted reactive task proposals with budgets/deduplication |
| `release_policy` | Exact artifact/approval closure and release contents |

Every model-using Task must inherit a finite reservation and deadline within the active resource policy. Lifetime inference limits are optional for an explicitly authorized capacity pool; per-attempt and capacity limits are not optional. Unknown provider authorization or model profile blocks dispatch even if the YAML parses.

### 8.2 Stage semantics

The YAML identifier `S5_5` corresponds to the display milestone `S5.5` (assembly/rendering).

A stage declares its ID, owner, contributing departments, prerequisite stage IDs, required output types, gate ID, human approval requirement, and typed failure route. The prerequisite graph is acyclic for initial planning. Reactive reopening is handled by explicit events and generation counters, not hidden dependency cycles.

A stage becomes ready to commit only when prerequisites' applicable outputs remain accepted/current. Authorized exploratory tasks can begin earlier using exact provisional dependencies. Gate failure invokes a registered repair/review/evidence request or pause handler. An exhausted hard resource limit or unsupported continuation routes to a gap report/escalation, never automatic pass. A renewable window boundary alone does not close the investigation.

The `paper` sequence is a milestone skeleton. It does not prohibit earlier Methods intake, Research notifications during drafting, or direct interdepartmental requests. `stages` governs commitment/readiness; `event_handlers` governs initiative and selective rework.

### 8.3 Reactive policy semantics

Handlers match a typed event and propose an allowlisted action. Initial actions are `triage_issue`, `propose_counterevidence_check`, `revalidate_dependents`, `replan_affected_tasks`, `reassess_allocation`, `publish_progress_checkpoint`, and `pause_for_budget_review`. Each supplies an owner, a deduplication key template, and a finite reservation under the active resource policy.

Event payloads cannot inject a handler name or executable expression. Repeated matching does not create unlimited tasks: deduplication, causal-issue limits, and transactional budget reservations apply before admission. An active task with changed inputs is either allowed to finish as a stale candidate or cancelled; it cannot silently adopt new input versions mid-call.

## 9. Web intelligence capability contract

### 9.1 Organization-wide permission model

A Score defines `web_intelligence` separately from generic network access. It declares whether each department/role may perform `discover`, `follow`, `citation_chase`, `acquire`, and `submit_for_promotion`; which source classes/providers/languages are permitted; whether protected project text may be transmitted as queries; and the maximum call/token/time/cost/download envelope. `acquire` never means permission to bypass authentication, access controls, licensing restrictions, robots/terms constraints, or other provider rules.

Research roles normally receive the broadest discovery/acquisition scope. Strategy, Methods, and Editorial receive task-scoped direct discovery rights. Adversaries receive an independently reserved retrieval budget and cannot be forced to use only the producer's SearchCampaign. Command may commission a retrieval task; it does not automatically gain broad evidence-publication authority.

### 9.2 Source classes and permitted use

Source classes are descriptive, not a universal quality ranking. Initial vocabulary: `scholarly_primary`, `scholarly_synthesis`, `official_institutional`, `standard_regulation`, `patent`, `technical_repository`, `technical_documentation`, `dataset_benchmark`, `news`, `community_discussion`, `general_web`, and `search_or_generated_summary`. A project may extend this vocabulary through a schema revision or registered profile.

A Score or acceptance contract can state which classes may satisfy which evidence requirements. Discovery-oriented classes can trigger investigation without being admissible final support. The verifier judges the exact source/claim relation; source class alone cannot certify truth.

### 9.3 Independent search and coverage

A material novelty, contradiction, or validity check can require multiple independent query families or source routes. Independence means a separately generated search plan or route with documented overlap; it does not imply statistical independence merely because another role name or model prompt was used.

Coverage requirements may include required source classes, languages, date ranges, backward/forward citation directions, author/project chasing, and explicit counterevidence query families. Search completion is based on the campaign's coverage contract and resource policy, not a fixed hit count.

### 9.4 Retrieval safety and provenance

External content is data, never authority. Retrieved instructions cannot change PrincipalIntent, Task, capability policy, secrets policy, or tool permissions. URL following and downloads use allowlisted adapters with size/type limits, redirect tracking, content hashing, and provenance. Search/AI snippets are preserved as discovery metadata rather than silently copied into evidence.

Provider request/response identifiers, query text, timestamps, adapter version, and measured usage are recorded when available and permitted. Sensitive queries can use local/redacted retrieval or be blocked according to mission policy.

### 9.5 Executable integrations

[Active Web Intelligence and Integrations](50-web-intelligence-integration.md) defines the capability profile/binding lifecycle and normalized API/browser/MCP request/response contract. Binding selection requires verified actual availability and existing authority. Explicit failure/completeness states map to QueryRecord outcomes without losing their cause; unavailable or throttled retrieval is not an empty successful search.

Provider limits apply across all relevant workers/accounts/operator scopes. Source captures preserve exact inspected versions and access scope. Verified source changes route through evidence/claim/unit impact analysis and ChangeRequests. Neither a provider response nor plugin output can mutate an accepted artifact or grant additional permissions.

## 10. Gates and releases

A GateResult records gate ID/class, exact targets, governing refs, checker/judge, check artifacts, `pass | fail | needs_evidence | needs_human | stale`, rationale, and unresolved issues. Gate classes are `deterministic`, `judgment`, and `human`; a named gate may require multiple results across classes.

Acceptance is scoped to the exact dependency closure. Resolve support through accepted, current premises, including independently verified members accepted in the same atomic post-state under section 4.1. Subjects must match the inspected versions but do not require prior acceptance. A review verdict cannot create its own factual support by pointing back to its subject. A change to a dependency does not mutate an old pass; an invalidation event marks its applicability stale for the new candidate.

A Release includes manifest hash, exact included artifacts, intent/mission/Score/evaluation refs, gate/approval records, event cutoff, Git commit/tag reference after finalization, and disclosure/limitations. Release lifecycle is `proposed → approved → prepared → finalized`; failures remain recoverable in their last completed state.

Finalization atomically compares the expected governing versions, current premise validity, exact subject coverage, and applicable approval state after snapshot/Git work completes. Concurrent invalidation or redirection rejects finalization and records stale applicability while retaining the prepared snapshot. That snapshot cannot be advertised as a current finalized release. Revalidation and any necessary new approval precede another attempt.

A noncompliant artifact may be exported as a visibly incomplete preview, not labeled a verified final release. Final release does not perform external publication.

## 11. Event/replay contract

An Event includes `event_id`, monotonic per-project `seq`, timestamp, authenticated actor, `event_type`, versioned payload, causal references, `prev_event_hash`, and `event_hash`. The first event uses a declared genesis hash. Ordering and sequence allocation are transactional; an agent cannot choose its own authoritative sequence.

Initial event vocabulary includes:

```text
project.created          intent.proposed          intent.activated
mission.published        task.proposed            task.queued
attempt.started          attempt.finished         task.state_changed
message.created          message.dispositioned    artifact.published
change.requested         edit_grant.issued        edit_grant.revoked
changeset.staged         changeset.rejected       changeset.integrated
artifact.accepted        artifact.invalidated     search_campaign.created
query.executed            discovery.captured        source.added
source.captured           coverage.reported         evidence.accepted
critique.registered       issue.transitioned
review.completed         verification.completed   candidate.selected
gate.checked             approval.recorded        dependency.invalidated
progress.recorded        progress.checkpointed    supervision.decided
allocation.opened        allocation.closed        allocation.revoked
budget.reserved          budget.settled           budget.threshold_reached
release.prepared         release.finalized
```

Each event type has a versioned payload schema and reducer. Record all data required for the accepted transition, including idempotency/lease decisions and external-call outcome references. Replaying events reconstructs domain state; restoring message leases/reservations additionally uses their recorded lifecycle and a declared recovery time policy.

Recovery expires old leases, requeues authorized unfinished work, reconciles uncertain external calls, and replays the outbox. It must not reinterpret old facts using a newly upgraded model. Reducer/schema upgrades need migration tests and an export of the original events.

## 12. Minimum conformance suite

The roadmap names test IDs for immutable branching, crash recovery, stale approvals, invalid criticism, verified repairs, semantic regressions, intent drift, compute accounting, message deduplication, and missing evidence. Passing a file syntax check is not passing this suite.

No runtime, schema validator, or automated conformance suite is currently included in this checkout. Markdown consistency and YAML parsing do not execute a Score, call a model, implement event reducers, or certify scientific quality.

## 13. v0.6 → v0.7 migration

Existing artifact bytes and event history remain immutable. New records use `schema_version: '0.7'`. An implementation must migrate active configuration through an explicit revision: map resource envelopes to ResourcePolicy; add AllocationWindows and progress policy; classify factual/governing input references as accepted premises and inspected targets as subjects; and explicitly authorize provisional exploration where desired. Ambiguous reference roles require resolution before dispatch. Never infer renewable authority from a null v0.6 limit.

Existing finite metered limits remain binding until amended. Review/reopen/message-count defaults are no longer global cognitive limits; operational failure limits and causal deduplication remain. Outline approval becomes mission-configurable. Old approvals remain scoped to their original exact targets and governing versions. The illustrative search campaign references runtime resource configuration rather than inventing a fixed lifetime search allowance.

## 14. v0.7 → v0.8 migration

New records use `schema_version: '0.8'`; historical records keep their original schema and bytes. Add structured artifact/change types and edit-grant enforcement without changing ArtifactRef identity syntax. Existing monolithic documents require reviewed segmentation, source mapping, and faithful reconstruction before scoped edits; do not auto-approve inferred unit identities. Previously broad namespace grants do not imply document-wide edit rights.

Add revision policy and verified capability bindings to active Score revisions. Preserve source identity/capture distinctions and map existing citations to exact unit/claim/source versions; unresolved mappings remain gaps. Record optional review reuse as explicit applicability evidence. Adapter configuration never inherits credentials, data permissions, or runtime availability solely from an authoring-environment plugin listing.
