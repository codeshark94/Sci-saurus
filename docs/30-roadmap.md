# Sci-saurus — Implementation Roadmap

> **Version:** v1.1 · **Date:** 2026-09-11 · **Status:** implementation plan with evidence scoped to completed runtime slices.
> Governed by `00-SSOT.md`, implemented through `20-architecture-v0.md` and `40-execution-contract.md`.

## 1. Delivery principle

Build the smallest organization that can demonstrate **useful, verified improvement over elapsed time under a fixed Principal-approved goal**. The first slice includes abundant-capacity allocation, alternative approaches, rational supervision, and progress checkpoints. Do not build the full organization chart first and postpone the hard question of whether it helps.

The core serves general projects. Paper production is the flagship application; a non-paper artifact must use the same control, task, capability, review, and integration services. Domain-specific requirements belong to the selected Score.

Three independent success conditions must be met: operational correctness, scientific/output usefulness, and alignment with the Principal's intent. A compiled PDF only demonstrates part of the first. A plausible critic is not proof of the second. A busy organization is not proof of any of them.

## 2. Phase overview

| Phase | Goal | Deliverables | Exit condition | Status |
|---|---|---|---|---|
| P0 | Freeze a testable contract, not every future feature | This document set; fixture definitions; explicit defaults/open configuration | Schemas and ownership agree; first acceptance contract/fixtures selected; no unresolved live-operation authority | Bounded contracts selected; configured external model and public retrieval milestones authorized and exercised |
| P1 | Durable, adaptive improvement loop | Structured units/scoped changes; artifact/task/event core; parallel approaches; renewable capacity; independent verification; checkpoints; remote runner; initial live web adapters | Recovery, scoped-edit, review, and retrieval tests pass; stagnation redirects work; exact progress and retained results are inspectable | In progress: durable core + structured-change + review/issue + capacity/checkpoint slices landed (T01–T03, T04–T05, T06–T10, T13, T41–T42, T45, T52, T56–T58 passing); bounded paragraph runner and model protocol adapters implemented; live Crossref/MCP retrieval verified; external GPU paragraph production, verification, and adoption validated on a synthetic fixture; project-scoped parallel paragraph composition and configured Operations Cell lifecycle live-validated on a synthetic fixture (see 65-project-runtime.md) |
| P2 | Four-department paper MVP | Supplied-results intake → survey → validated argument → reviewed manuscript → rendered candidate | One real case completes with traceable evidence and no unwaived integrity blocker; human approves exact output | Deterministic evidence-bound release pipeline implemented and rendered; a complete real scientific case and exact human approval remain outstanding |
| P3 | Scale and evaluate the adaptive organization | Broader concurrent workload, multi-provider capacity, deeper policy evaluation, review/integration backpressure | Disruption tests pass; equal-resource and equal-time comparisons reported honestly | Not implemented |
| P4 | Broaden Score composition | Additional deliverables and richer custom-Score interfaces | New mission contracts reuse the same department and control-plane services | In progress: bounded non-paper Score, executable policy checker, and reusable multimodal visual-review Score implemented |
| P5 | New scientific analysis and experiments | Analysis/experiment scope, method execution, and scientific result-validation contracts | Authorized experiment execution, isolation, provenance, cancellation, and domain-validation tests pass | Bounded local execution complete; remote scheduling and expert scientific adjudication remain |
| P6 | Broad interoperability and process reuse | Cross-agent/A2A/framework interoperability; evaluated organizational memory | Protocol, authority, privacy, and cross-project isolation tests pass | Deferred; useful API/MCP mission tools belong in P1/P2 |

P1 establishes the adaptive control loop with a small role set; P2 applies it across the complete paper workflow; P3 tests its scaling behavior. Dynamic tasks, supervision, selective invalidation, progress checkpoints, and minimal evaluation belong to the first implementation. Additional departments and framework integrations do not substitute for those capabilities.

Stable unit identity, purpose-scoped edits, and a real web-to-evidence path also belong in P1. Whole-document rewriting and metadata-only retrieval are insufficient substitutes for these capabilities.

P1 also instantiates project identity and the minimal on-demand Operations Cell. It must make one suitable program/service work on actual project inputs and hand back verified execution evidence. General tool installation/connectivity cannot be deferred to P5 when it is required for retrieval, extraction, or rendering in the first paper workflow.

The bounded Score slice now selects capabilities explicitly, supports text/JSON/Python units, runs machine checks on exact staged candidates, and plans around first-result targets, completion targets, and hard deadlines. Its implemented contract is documented in `70-scored-project-runtime.md`; the broader adaptive Score design remains a separate roadmap commitment.

The bounded literature slice reuses the same execution, Operations, artifact, and time services for OpenAlex discovery, configured full-text capture, per-work map updates, accepted-survey prerequisites, and independent gap assessment. Its local integration and gate tests are described in `75-literature-survey-score.md`. This is the first paper-specific mission stage; general-purpose project Scores keep their own selected capabilities and acceptance contracts.

## 3. P0 — Scope and contract freeze

Freeze the user-facing principles: Principal-directed command, four departments, organization-wide Web Intelligence Fabric, Research-owned formal evidence acquisition, a designated adversary in every department with independent retrieval allowance, grounded criticism with rebuttal, verified progress, immutable provenance, and bounded authority.

Before a live run, choose a real supplied-results package; define required claims/sections and scientific boundaries; record intent authorization and provider/data policy; configure capacity-pool or metered allocation, finite execution leases, checkpoint cadence, and actual hard limits; and pin the first Score and evaluation contract. Plentiful external GPUs are the default premise, not an invented endpoint or measurement. Credentials and exact model selection remain configuration, not secrets in design documents.

Open provider/budget values may remain unresolved during mock-based development. The configuration validator must block the corresponding live operations; developers may not treat null as “unlimited.” No fresh confirmation is needed merely to restate an already explicit authorization.

**P0 acceptance:** all original design disagreements are explicitly superseded or marked unresolved; example fields conform to the contract; test IDs are defined; unimplemented pieces are labeled. Document approval is distinct from evidence of effective model behavior.

## 4. P1 — Durable core and an adaptive improvement cycle

### 4.1 Module plan

```text
scisaurus/
  core/
    schema.py              # typed domain records, versioned validation
    store.py               # immutable blobs/manifests, branching, adoption CAS
    documents.py           # stable units, ordered manifests, source/claim bindings
    changes.py             # requests, scoped grants, mutation checks, atomic integration
    execution.py           # project profiles/environments, program runs, operational evidence
    events.py              # transactional append, reducers, integrity chain
    messages.py            # outbox, leases, acknowledgements, deduplication
    tasks.py               # task/attempt lifecycle and dependency invalidation
    budget.py              # capacity windows, reservations, settlement, unknown calls
    context.py             # bounded version-pinned ContextPackage
    gates.py               # deterministic/judgment/human gate contracts
    release.py             # prepared snapshot → Git tag → finalized event
  command/
    intent.py              # interpretations, approval binding, impact analysis
    scheduler.py           # activity graph, admission, leases, dispatch, backpressure
    arbitration.py         # authority and conflict checks
    progress.py            # frontier, allocation decisions, progress checkpoints
  review/
    protocol.py            # challenge, response, judgment, verification
    issues.py              # stable issue identity and lifecycle
  retrieval/
    capabilities.py        # provider profiles, verified deployment bindings, health
    campaign.py            # SearchCampaign, coverage state, query-family expansion
    adapters.py            # provider-neutral search/fetch/citation/source adapters
    capture.py             # DiscoveryRecord → ReferenceCard/SourceCapture
    promotion.py           # dedupe, provenance checks, evidence-promotion requests
  org/
    manifest.py            # charters, role profiles, adversary requirements
  runners/
    base.py                # constrained runner interface
    endpoint.py            # first approved external GPU or local inference endpoint
    fake.py                # deterministic recorded fixtures
  scores/
    loader.py              # registered stages/handlers, no arbitrary eval
  cli.py
  tests/
```

Separate modules indicate responsibility, not a demand to implement elaborate class hierarchies. Start with one local control plane and a small concurrent slice of the authorized GPU pool. Grow active workers after observing throughput and review capacity. Do not add AutoGen/LangGraph until a concrete missing capability justifies an adapter.

### 4.2 First vertical slice

Use a supplied, self-contained two-page results/storyline fixture containing one genuine overclaim, one intentionally sound claim, and a missing-evidence condition. Run materially different candidate approaches, an independent evidence check, adversarial review, response, adjudication, verification, and selection using role-scoped context. Reuse worker processes across roles; other departments may be represented by fixtures until the core loop works.

The required demonstration is that a real defect is accepted as valid, an unreasonable criticism is rejected, a repair is independently checked, a regression is detected, and the correct candidate/retained incumbent is recorded. An unproductive branch triggers reallocation; renewable windows continue inside existing delegation; wall-clock checkpoints expose both progress and lack of progress. Pause/restart mid-cycle must not duplicate committed effects or lose governing intent. Connect one authorized inference endpoint early enough to verify remote worker cancellation and stale-output fencing.

Represent the fixture as stable paragraphs/claims in a DocumentManifest. Repair the overclaim through an EditGrant and ChangeSet while preserving unrelated text, findings, references, and structure. Reject an attempted full-document replacement and a conflicting concurrent edit. Exercise one captured external source through evidence verification and a scoped repair. Add general-web search/source acquisition early enough to demonstrate coverage beyond scholarly metadata, following `50-web-intelligence-integration.md`.

### 4.3 P1 acceptance tests

| ID | Test | Required result |
|---|---|---|
| T01 | Three writes to one logical artifact | Three immutable versions; exact body hashes and parent links |
| T02 | Two candidates created from the same parent | Distinct versions; no lost update; stale adoption CAS rejected |
| T03 | Crash before/after object publication and database commit | Only committed artifacts visible; orphan blobs harmless; outbox recovers |
| T04 | Duplicate delivery and lease expiry | One accepted effect; stale worker fenced; visible disposition |
| T05 | Restart during model/tool call of unknown outcome | Recorded uncertainty and conservative budget accounting; no false zero-cost success |
| T06 | Reviewer invents a requirement or repeats vague criticism | Invalid critique rejected with reason; no forced producer edit |
| T07 | Producer rebuts a mistaken objection with supplied evidence | Independent adjudicator records rebuttal; no fake artifact-progress credit |
| T08 | Producer claims a defect is fixed but patch does not resolve it | Verification fails; issue stays open; candidate not promoted |
| T09 | Patch resolves an issue by deleting a required finding | Coverage/regression check fails or explicit scope-change approval is required |
| T10 | Review returns empty due to tool failure | `review_failed`/`insufficient_evidence`, never implicit pass |
| T11 | Lower-priority user preference conflicts with a hard constraint | Constraint preserved; command explains the trade-off |
| T12 | Actor attempts a cross-namespace write or self-approval | Rejected outside the model; attempted action recorded without secrets |
| T13 | Event alteration/truncation against a trusted saved head | Integrity failure detected; whole-chain-rewrite limitation documented |
| T14 | Prepared release interrupted before tag/finalization | Recovery completes the same manifest or remains visibly incomplete; no false release |

Tests use fixtures first. Passing them establishes workflow behavior, not LLM scientific ability.

### 4.4 P1 adaptive-control acceptance scenarios

These complement T01–T14; identifiers continue after the existing paper/retrieval scenarios.

| ID | Scenario | Required result |
|---|---|---|
| T41 | A capacity allocation window ends while justified work remains | Automatic renewal under existing delegation; no implicit authority expansion; cumulative usage retained |
| T42 | Identical unproductive work is renamed across window renewals | Causal stagnation persists; independent diagnosis or reallocation occurs; no fake progress |
| T43 | A valuable long investigation crosses multiple checkpoints | Intermediate milestones and pending state remain visible; absence of immediate improvement alone does not cancel it |
| T44 | An exploratory outline uses a provisional claim candidate | Exploration runs under delegation; commitment remains blocked until exact dependencies are accepted/revalidated |
| T45 | More GPU capacity creates an unverified candidate backlog | Admission shifts capacity to review/integration; reserved independent checks are not starved |
| T46 | A supervisor repeatedly selects work on unsupported benefit predictions | Independent scrutiny checks the policy/evaluator; it cannot lower the acceptance contract to report success |
| T47 | A new evidence item invalidates the incumbent | Checkpoint shows inadmissibility or no usable incumbent; no monotonic-quality fiction |
| T48 | Several branches rediscover the same decision-relevant fact | One causal information change; all supporting records preserved |
| T49 | Internal outline revision preserves scope; later revision changes required contribution | First can proceed under delegation; second requires scope authority; exact approval binding remains |
| T50 | A remote worker times out and later returns after its lease expires | Stale result cannot commit; provider/capacity uncertainty stays visible until reconciled |
| T51 | Supervisor or producer call stalls across a checkpoint | Native checkpoint still records current state and pending judgment; no fabricated decision or silent renewal |
| T52 | Mission reaches its completion contract with unused capacity | Release proposed and discretionary work stops, unless continued improvement is explicitly authorized |
| T53 | Old and renewed windows overlap while metered tools approach a hard limit | Pool-wide capacity and tool expenditure checked atomically; renewal cannot double-allocate or remove the limit |
| T54 | A candidate or first EvidenceRecord needs review before acceptance | Exact subject review accepts the same immutable evidence hash through separate verdict/events; no future-verdict backlink rewrite or prior subject acceptance is required; disputed assertions cannot be smuggled in as facts |
| T55 | Intent or supporting evidence changes during release snapshot/Git preparation | Finalization CAS fails; prepared snapshot remains visibly stale; new validation/approval is required before a current release can finalize |

Start minimal evaluation here: compare a capable single workflow and independently verified alternatives with the adaptive loop on the same supplied fixture. These are implementation diagnostics; held-out efficacy evaluation follows the protocol in section 7.

### 4.5 Structured change and active-retrieval acceptance scenarios

The current suite covers the core paragraph/grant/manifest, retrieval-failure, capability-binding, project-isolation, and rendered-paper boundaries in this table. The remaining rows continue to define conformance work for broader provider fleets, coupled cross-unit repairs, and shared multi-project deployment; a scenario is not considered implemented merely because an adjacent mechanism exists.

| ID | Scenario | Required result |
|---|---|---|
| T56 | A paragraph repair includes unrelated rewrites, new document IDs, or replacement of its whole parent | Complete mutation set exceeds the grant; reject without changing the accepted manifest |
| T57 | A writer reads the entire manuscript but has a narrow span grant | Read access remains sufficient; out-of-span/protected changes fail; no implicit write expansion |
| T58 | A paragraph moves, splits, or merges | Stable IDs/lineage and explicit topology changes are preserved; broken current anchors/citations block acceptance |
| T59 | A small macro, template, or citation-alias edit changes otherwise untouched content | Separate scope and broad impact review are required; active directives cannot bypass the grant |
| T60 | Two disjoint paragraph patches share a changed definition or premise | Dependency/conflict and composed-argument checks prevent silent last-writer-wins integration |
| T61 | A coupled staged claim, Discussion, and abstract repair is valid, partly fails, or has circular support | A valid group succeeds against its proposed atomic post-state; member acceptance/heads/manifest/events commit together; partial failure or factual-premise cycle leaves all unchanged |
| T62 | A new document combines reviewed and unchanged units | Review reuse needs explicit unchanged-unit/premise/structural proof; exact human release approval never transfers |
| T63 | A correction is reversed after unrelated improvements were accepted | Scoped inverse change preserves later unrelated work; no blanket head reset |
| T64 | Repeated small requests or shared-asset edits cumulatively replace the manuscript | Causal scope history triggers non-conflicted reassessment; new IDs do not reset scope authority |
| T65 | Importing a monolithic document produces an uncertain paragraph map | Faithful reconstruction/mapping check fails visibly; guessed identities grant no editing authority |
| T66 | A reviewer/integrator attempts raw file/Git/database changes or its own scope expansion | Publication boundary rejects the action; accepted content remains unchanged |
| T67 | Same DOI/URL returns corrected or new-version material | Preserve old capture and citation binding; revalidate exact affected claims/units before scoped repair |
| T68 | Retrieval returns 429, incomplete pagination, missing credentials, or a parse failure | Precise cause and coverage gap retained; no successful empty-search or full-coverage claim |
| T69 | Only an abstract/snippet is accessible for a full-text-dependent claim | Evidence remains insufficient; capture scope is visible and claim cannot be verified from missing content |
| T70 | Multiple GPU workers call a shared externally limited API | Provider/account/operator-wide rate and concurrency limits hold; duplicate calls and stale caches do not create duplicate information progress |
| T71 | A connected plugin/MCP schema or credential scope changes | Binding is revalidated or degraded; discovered/host-installed capability alone cannot authorize standalone execution |
| T72 | A late retrieval result arrives after cancellation or its claim version changes | Observation may be retained for audit; evidence/coverage/manuscript adoption requires a current task and validation |
| T73 | New external counterevidence affects one claim and several occurrences | Impact closure identifies exact units, issues a scoped change group, and independently verifies the integrated result |
| T74 | General web capability is configured but only metadata endpoints actually work | Capability remains incomplete/degraded; a real general search and inspected source are required for acceptance |
| T75 | Project B resolves A's artifact/grant/binding/private cache through a shared host | Project identity and authority checks reject access absent an explicit authorized import/binding |
| T76 | Operations installs a program or lists MCP schemas but its representative run fails | Capability stays incomplete/degraded; installation or discovery is not completed operation |
| T77 | A converter exits successfully after changing protected values or unrelated units | Domain/scope checks reject publication despite operational success |
| T78 | A tool package/environment/schema changes or a saved service has stopped | Probe/binding applicability is rechecked; saved readiness cannot authorize stale or different execution |
| T79 | A practical extraction task tries to execute new raw-data estimation | Scientific mission/capability boundary applies; Operations cannot self-authorize new analysis |
| T80 | One project's operations task ends or is cancelled on a shared runtime | Only project-owned resources are stopped/reconciled; evidence is retained and other projects remain unaffected |

### 4.6 Bounded Score and time-contract acceptance scenarios

These scenarios cover the shared project runner, the local-program adapter, and explicit time planning. Automated checks are in `test_scores.py`, `test_scored_project.py`, `test_programs.py`, `test_json_artifact_program.py`, and `test_time_policy.py`. Real program checks and model-response fixtures establish different parts of the contract; they do not by themselves establish a complete live-model mission.

| ID | Scenario | Required result |
|---|---|---|
| T81 | A non-paper mission produces a JSON policy and operating guide | The same project runner preserves identity, immutable units, scoped proposals, and atomic integration; output names and content follow the Score |
| T82 | A mission selects only local-program workloads | Zero unselected scholarly-search, Fetch, or other network-tool workloads; configured model traffic is accounted for separately |
| T83 | A program exits successfully but reports an invalid candidate | Operational transport succeeds; the exact candidate machine check fails and cannot be overridden by favorable model reviews |
| T84 | Score, capability identity, or governing input changes after proposal | Existing evidence cannot authorize adoption against the changed basis |
| T85 | Initial stage estimates exceed the hard elapsed cap | Block before external dispatch, preserve the reason, and do not report the supplied baseline as a newly verified result |
| T86 | New production would consume the required review time or exceed the completion target | Defer the work, preserve independent review requirements, and retain the current incumbent |
| T87 | Observed stage duration exceeds its seed, or the first-result target is missed | Update estimates with visible provenance; expose pending, missed, or late verified-result status without inventing progress |

**Generality regression rule:** adding or changing a Score must not inject academic assumptions or tool workloads that the mission did not select. A local-only non-paper fixture passes only when it produces its declared artifact forms through the shared runner and records zero unselected academic/network tool calls.

## 5. P2 — Four-department paper MVP

The [Literature Survey Score](75-literature-survey-score.md) implements a bounded first stage through `run-survey`: separately planned topic searches, OpenAlex work/reference/citing requests, configured MCP full-text routes, immutable per-work analyses and supported conceptual links, required focused reviews with field/relationship-scoped repair, independent survey acceptance, gap nomination, targeted counter-search, and independent assessment. Source or work changes remap only affected entries. Deterministic gates require the current accepted survey before gap-related dispatch and assessment commitment, exact recorded model and source evidence, and verified full text from every decisive comparison's own work. Targeted counter-search and assessment also bind the exact nomination; its revision invalidates the prior verdict separately from the survey. Focused reviews bind actual entry/relationship bodies and exact source windows; every registered work must be covered, and a global pass cannot override a failed focused check. Local fixtures exercise failure boundaries, and a bounded external ResNet path demonstrates one successful v3 survey, exact DOI reconciliation, scoped assessment resume, and rendered gap-report candidate. That case does not establish research-judgment accuracy.

Discovery reads one finite page per query and records remaining pagination. Provider-reported titles, years, and identifiers are retained; an optional exact-DOI Crossref route separately reconciles DOI, title, and year without replacing either provider observation. Author, edition, and publication-version reconciliation remains open. Full-text routes are configured; source context is bounded and its visible window is reported. The runner preserves audit artifacts when blocked and can resume under an explicit additional deadline, unknown-outcome policy, and source-change scope. General full-text discovery and an exhaustive literature campaign remain outside this slice.

### Next implementation priorities

1. **Evaluate gap decisions on held-out expert-labeled cases.** Include solved and unresolved questions, alternate terminology, misleading abstracts, missing decisive text, publication duplicates, and non-comparable conditions. Measure false novelty claims, missed closest work, unsupported comparisons, and justified abstentions before unattended scientific use.
2. **Expand source discovery and identity reconciliation.** Resolve alternate OA/full-text versions through verified capabilities, preserve original metadata and captures, and explicitly reconcile conflicting DOI, title, author, date, and version evidence. Add source-location retrieval beyond the current configured routes and bounded prefix contexts.
3. **Exercise recovery across interruption points.** The runner reconstructs the search frontier, source/map basis, accepted dependencies, outstanding calls, and resource accounting from durable records. Retain external evidence from repeated interruption stages and extend the same policy to other mission runners.
4. **Run complete paper cases.** The supplied-results, accepted-survey, structured-manuscript, LaTeX/PDF, and exact release-candidate closure is implemented. Exercise real scientific inputs, qualified review, stable source spans, and exact Principal approval without treating deterministic compilation as scientific validation.

The implemented release path ingests a `ResultsPackage`, emits a gap report when prior work refutes the nominated gap, and binds paper claims to exact result phrases or stable literature spans. The implemented web slice includes SearchCampaign planning, OpenAlex discovery and citation expansion, exact Crossref DOI lookup, MCP source acquisition, query/source/coverage records, and discovery-to-evidence gates. Empty queries, snippets, metadata, and identifiers cannot impersonate scientific evidence. Multilingual planning, author/project chasing, general search, and automatic alternate full-text discovery remain provider-expansion work.

Research, Strategy, Methods, and Editorial roles operate through task-scoped producers, reviewers, and deterministic gates in the project and paper runners. Research owns canonical source capture and catalog promotion; gap challenge uses a separately planned counter-search after survey acceptance. The literature Score now reserves a configured part of `max_works` for that challenge so broad discovery cannot fill the entire register first. Producer rebuttal, independent contested adjudication, and verified closure are preserved by the shared review service. Provider-wide rate coordination and reserved retrieval budgets across concurrent projects remain deployment policy rather than implicit authority.

The paper Score now runs as a controlled dependency closure: structured units and narrow change grants compose through an accepted manifest, current survey and assessment state gate the build, exact claims and citations populate the claim index, and LaTeX compilation is followed by full-page rendering and visual review. OpenAlex, Crossref, MCP Fetch, and allowlisted local-program acquisition use the shared Operations contract. Additional APIs and MCP recipes are added per project only after representative execution and independent verification.

The Principal sees a digest of contribution, evidence limitations, unresolved material issues, actual resources, and approval targets. A failed scientific premise produces a gap report or incomplete draft—not a polished artifact falsely labeled final.

### P2 acceptance tests

| ID | Test | Required result |
|---|---|---|
| T15 | Raw data with no interpretable supplied results | Explicit intake-gap report; no invented methods or values |
| T16 | A real citation does not support the attached claim | G2 judgment fails despite identifier/key checks passing |
| T17 | Counterevidence exists in retrieved material | It remains visible and affects claim assessment rather than being suppressed |
| T18 | Methods Adversary challenges an unwarranted Methods rejection | Qualified separate adjudication; no department immunity |
| T19 | Editorial patch subtly changes a technical conclusion | Semantic revalidation required; compilation does not authorize the change |
| T20 | Approved outline/manuscript dependency changes | Approval becomes inapplicable; new hash requires appropriate reapproval |
| T21 | PDF compiles with clipped or unreadable material | Visual check fails or requires remediation despite compile success |
| T22 | Final release reconstructed from manifest | Required files/hashes, evidence links, decisions, and approval closure match |

One real successful case is an integration milestone, not an efficacy benchmark. Record human interventions and remaining weaknesses rather than hiding them from the evaluation.

## 6. P3 — Scale and evaluate adaptive collaboration

Scale the durable subscriptions, self-proposed tasks, and delegation implemented in P1 across the complete organization. A department can pursue a new finding without waiting for the Composer to dictate every call, but scheduling still verifies purpose, ownership, resources, and duplicate cause.

Stress-test selective dependency invalidation, independent review reservations, priority/fairness controls, cross-department issue routing, and Progress Controller portfolio selection. Begin with a transparent heuristic: unresolved mission-critical evidence and high-impact defects outrank cosmetic rewrites. Compare alternative allocation policies only after collecting trustworthy measurements, including supervisory and integration delay.

### P3 acceptance scenarios

| ID | Scenario | Required result |
|---|---|---|
| T23 | Research finds novelty-colliding evidence during writing | Independent check; affected work reopens; unrelated accepted results remain intact |
| T24 | Strategy requests Methods validation and Methods requests Research evidence | Dependency graph avoids deadlock; each request receives a disposition |
| T25 | Principal changes the goal mid-run | New approved intent revision; affected tasks/approvals stale; past history preserved |
| T26 | Two departments repeatedly request the same investigation | Stable issue/task deduplication; no loop-cap evasion by new IDs |
| T27 | Extra review discovers additional genuine flaws | Useful information gain may be recorded; issue-count increase is not automatically failure |
| T28 | Cosmetic rewrites close minor issues but harm core coverage | No verified progress/promoted incumbent unless accepted trade-off is explicit |
| T29 | Plateau or missing source prevents useful next action | Switch to targeted evidence/alternative, pause, or stop; do not burn the remaining budget automatically |
| T30 | Concurrent tasks compete for the last resource tranche | Atomic reservation; hard budget respected; review budget cannot be silently consumed by producers |
| T31 | Adversary disputes a command decision made by the current Arbiter | Conflict identified; another qualified judge or Principal handles the appeal |
| T32 | Retrieved text asks the agent to change goals or expose private files | Treated as untrusted data; no instruction/permission escalation |
| T33 | Strategy directly discovers a potentially colliding prior work outside the current Research snapshot | Discovery is recorded and routed for Research acquisition/verification; it is not silently cited as evidence |
| T34 | Research query returns zero results or provider failure | QueryRecord preserves the empty/failure outcome; no fake ReferenceCard or implicit coverage claim |
| T35 | Adversary is given the producer's search corpus but an independent-search requirement | It creates a distinct query family/source route within its reserved budget; overlap is recorded rather than pretending independence |
| T36 | Community/news/search-snippet lead alleges a material counterexample | Lead can trigger investigation; final scientific support requires an authorized SourceCapture/EvidenceRecord or remains unresolved |
| T37 | SearchCampaign reaches a fixed hit count while one required source class/language/counterevidence route is untouched | Coverage gate remains unmet; result count alone cannot terminate the campaign |
| T38 | New terminology discovered during citation/author/project chasing opens a distinct lineage | Campaign generation expands/reopens with causal provenance; prior search history remains immutable |
| T39 | A department attempts an unapproved retrieval provider or transmits protected context in a query | Dispatch blocked before external call; policy violation recorded without leaking the protected content |
| T40 | Broad web search shows diminishing returns under finite budget | CoverageReport records attempted routes/gaps and a justified stop/pause; scheduler reallocates rather than looping blindly |

## 7. Evaluation: demonstrate compute → progress

### 7.1 Baselines and fairness

Evaluate the same tasks, evidence availability, approved model pool, and resource ceilings with three initial conditions:

| Condition | Purpose |
|---|---|
| A: Simple supplied-evidence writer | Establish output quality and human repair effort without organizational overhead |
| B: Same-budget writer with generic feedback/refinement or independent candidate selection | Test whether improvement comes from extra compute alone |
| C: Sci-saurus review/adjudication/verification/selection | Test the incremental value of the proposed mechanism |

Use the same frozen retrieval corpus where testing review mechanics; separately test retrieval policies when evaluating web intelligence. For retrieval evaluation, compare fixed-corpus search, broad multi-surface search, and broad search plus independent adversarial retrieval under explicit resource envelopes. Record input/output tokens, model calls, elapsed time, monetary cost when applicable, and human effort. Equal call counts are not equal compute if context length/model size differs; report the resource vector rather than hiding that mismatch.

### 7.2 Metrics

Primary outcomes are expert-checked unsupported/incorrect claim rate, preservation of required scientific content, and human repair effort. Secondary outcomes include valid-critique precision, recall on seeded known defects, false closure rate, regression frequency, source-trace completeness, retrieval coverage by required route, counterevidence discovery rate on seeded/known cases, source-promotion precision, task completion, and resource use.

For factual validation, use externally inspectable evidence and a human-checked sample. Self-reported evaluator scores are diagnostics, not final ground truth. Predefine what counts as a claim, a material defect, and a repair before comparing systems. Missing evidence is distinct from evidence of falsehood.

Compare several resource tiers to produce quality-versus-compute and quality-versus-elapsed-time curves. Separately compare equal-resource effectiveness and equal-wall-clock usefulness with different concurrency. Include supervision, retrieval, verification, and integration overhead. Report time to first usable result, checkpoint lateness, longest interval without validated change, review backlog, and retained required coverage. There is no assumption that every additional tier improves quality. Record information discovery separately so a more rigorous review is not penalized simply for exposing hidden problems.

### 7.3 Experimental hygiene

Use multiple real briefs and repeated runs appropriate to the task variability; a single successful run is insufficient. Separate development fixtures from a frozen held-out evaluation set. Avoid repeatedly tuning prompts/gates on held-out outcomes. Blind expert comparison to candidate order and system identity where feasible.

Report per-task results and uncertainty, not only an average. Preserve negative outcomes and human interventions. Fixed-model and mixed-model comparisons answer different questions and should not be conflated.

### 7.4 Decision to scale

Expand worker capacity when measured evidence indicates a useful quality or elapsed-time benefit and verification/integration capacity can keep up. Keep controlled comparisons against B to assess whether additional coordination helps beyond abundant compute itself. The Principal sets acceptable trade-offs; the blueprint does not invent a threshold. Adding organizational layers requires a concrete benefit, not just spare GPUs.

If the mechanism does not help, inspect evidence availability, critique validity, judge calibration, context construction, and acceptance criteria before adding agents. Simplifying the organization is a valid result.

## 8. P4–P6 expansion boundaries

**P4 — Score generality.** Demonstrate a research report or proposal using the same functional contracts. New templates and mission-specific tools are allowed; editing department implementation to special-case the new deliverable invalidates the “Score-only” claim.

**P5 — New scientific execution.** The bounded experiment runtime extends the existing Operations Cell with explicit question, hypothesis, method, parameter, seed, stopping, expected-outcome, asset, and validation contracts. It executes a pinned program twice, preserves raw observations and figures, requires a distinct pinned calculator to recalculate every primary outcome, and obtains scoped method/claim review before adopting a generated result package. Manuscript composition now consumes a separately generated and adjudicated research-argument artifact: observed patterns, competing mechanisms, discriminating tests, and figure/table jobs must be present before prose. The complete robust-mean validation mission exercised this path with real numerical execution, exact replay, deterministic recalculation, two multimodal result reviews, storyline-first manuscript assembly, rendered PDF production, and independent page review. Capability inventory, suitable program installation/build, MCP/API connection, source processing, and document generation already operate in P1/P2. Remote multi-machine experiments, restart recovery, parameter-sweep scheduling, and expert scientific adjudication remain later P5 work.

**P6 — Interoperability and reuse.** Extend cross-agent/A2A/framework interoperability for concrete needs. Useful API/MCP retrieval services are already P1/P2 mission capabilities, not blocked on this phase. Test cancellation, authentication, artifact mapping, data boundaries, and authority propagation. Organizational memory must distinguish verified lessons from anecdotes; it cannot self-authorize policy changes.

## 9. Main risks and mitigations

| Risk | Concrete mitigation | Tests/measurement |
|---|---|---|
| Arbitrary negative reviewing | Admissibility, no-objection outcome, rebuttal, separate judge | T06–T10; critique precision |
| Same-model consensus mistaken for truth | External evidence, deterministic checks, independent critical verification | T16; expert sample |
| Metric gaming by deleting claims | Frozen coverage and comparison contract | T09, T28 |
| Chief suppresses critique | Reserved reviewer budget, durable records, direct escalation | T30–T31 |
| Principal intent drifts | Source-linked interpretations, explicit activation, change impact analysis | T11, T25 |
| Busy but unproductive organization | Costed action outcomes, equal-budget baselines, plateau policy | T27–T29; quality/cost curves |
| Lost events/double execution | Transactional outbox, leases, idempotency, unknown-call reconciliation | T03–T05 |
| Stale scientific/approval basis | Pinned inputs and dependency invalidation | T20, T23 |
| False finalization | Exact approval closure and idempotent release stages | T14, T22 |
| Untrusted tool/document instructions | Capability enforcement outside the model; sandboxed compilation | T12, T32 |
| Shallow or biased web coverage | SearchCampaign coverage contract, query-family/source-language diversity, explicit gaps | T34–T38 |
| Discovery mistaken for evidence | Discovery→SourceCapture→Evidence promotion boundary | T33, T36 |
| Adversary anchored to producer retrieval | Independent reserved retrieval route and overlap reporting | T35 |
| Search cost explosion / endless browsing | Actual hard limits, renewable windows, causal stagnation checks, Progress Controller allocation | T37, T40–T42, T53 |
| Retrieval leaks protected context or bypasses provider policy | Per-role provider/data policy enforced before dispatch | T39 |

## 10. Immediate implementation order

Retain each bounded live milestone as separate evidence. The non-paper Score's external model production, real program checks, exact combined-artifact adoption, and measured timing are recorded in [70-scored-project-runtime.md](70-scored-project-runtime.md). The literature stage requires its own execution and semantic evidence; neither milestone establishes an autonomous general organization.

1. **Broaden recovery evidence.** The durable resume controller and survey reconstruction are implemented, and one interrupted external survey completed through retained-state continuation. Exercise repeated interruption points across project and paper runners.
2. **Scale executable plans.** Versioned dependency graphs, selective reuse, injected execution handlers, independent verification, and deadline closure are implemented. Add portfolio scheduling and measured concurrency only where evaluation shows a benefit.
3. **Expand approved capability recipes.** Allowlists, registry discovery, pinned local-wheel provisioning, representative execution, and independent bindings are implemented. Add project-approved recipes for concrete missions without turning registry presence into trust.
4. **Supply held-out expert labels.** The blinded, hash-pinned evaluation runner and novelty safety metrics are implemented. Freeze an expert-adjudicated corpus and report repeated results before claiming research judgment quality.
5. **Repeat complete scientific paper missions.** One live robust-mean validation mission now covers accepted survey, executed results, structured manuscript, storyline-first claim index, LaTeX/PDF release candidate, and multimodal page review. Repeat across materially different domains and extend identity checks to authors, editions, and publication versions.

Expand organizational scale and provider coverage after measuring these workflows. Shared GPU and provider capacity requires aggregate accounting across projects without implicit data or credential sharing.

## 11. Package status

This checkout includes the durable artifact/task/event core, scoped document changes, review/issue services, capacity/checkpoint accounting, and bounded paragraph/project/survey runners. It also includes source-aware recovery, executable versioned plan graphs, on-demand approved capability acquisition, blinded judgment evaluation, and the evidence-bound paper release builder described in `80-completion-runtime.md`.

External GPU paragraph and multi-paragraph milestones are recorded in `60-paragraph-runtime.md` and `65-project-runtime.md`. `70-scored-project-runtime.md` defines the generic Score implementation; `75-literature-survey-score.md` records the bounded survey workflow, stable spans, and DOI/title/year reconciliation; `80-completion-runtime.md` records the completion slice and evidence limits. An expert-held-out corpus, broader identity and source-version reconciliation, portfolio scheduling, deployment, external submission, shared multi-project capacity coordination, and measured research-quality improvement remain outside this slice.
