# Sci-saurus — v1 Architecture

> **Document version:** v0.8 · **Date:** 2026-09-10 · **Status:** proposed implementation architecture.
> The original filename is retained for continuity; “v0” in the filename is not this document's revision number.
> Normative concepts: `00-SSOT.md`, D1–D36. Organizing model: `05-system-concept.md`; project operations: `15-project-organization.md`. Shared contracts: `40-execution-contract.md`; surgical changes: `45-artifact-change-control.md`; active API/tool integration: `50-web-intelligence-integration.md`.

## 1. Architectural center

The system has an organizational surface and an executable core:

```text
Principal
   │  original instructions, approved intent, redirection, approval
   ▼
Executive Command
   Intent Keeper · Composer · Arbiter · Progress Controller
   │  mission, delegated authority, adaptive resource allocation
   ▼
Research & Intelligence ↔ Strategy & Writing ↔ Methods & Validation
              ↖────────── Editorial Office ──────────↗

Every department: chief + specialist roles + designated Adversarial Reviewer
Every organ: versioned evidence, recorded requests, explicit accountability

                   Native control plane
Score → dynamic activity graph → bounded ContextPackage → elastic workers
  ↑                  ↓                         ↓                    ↓
readiness       durable event bus        tool permissions      candidate artifact
  ↑                  ↑                                              ↓
  └── selection / verification / adjudication / challenge / response ─┘

Archivist: immutable objects + version manifests + transactional event history
           + project views + Git snapshots + release manifests
```

A department is a persistent responsibility, backlog, memory scope, and authority boundary. It is not a dedicated model process. An elastic worker pool executes the current activity graph; groups can cross departmental boundaries. The same Task records represent production, investigation, alternative construction, and evaluation. Non-LLM services perform storage, admission, deduplication, schema checks, accounting, and reproducible mechanical validation.

The primary feedback loop is observe mission state → compare possible activities → allocate concurrent work → independently verify useful changes → update the frontier and incumbent. Departmental stages constrain accepted commitment, while authorized provisional branches permit earlier exploration.

Instantiate this organization per research project. Command, knowledge, task graph, mutable environments, credentials, and artifact authority are project-scoped. Shared GPU/provider capacity has aggregate admission accounting plus project attribution. The Sci-saurus code repository is the framework, not the shared mutable workspace of all research projects.

## 2. Principal-facing command

### 2.1 Intent Keeper

Preserve the verbatim Brief and subsequent instructions. Produce a versioned interpretation with objectives, ordered priorities, constraints, non-goals, approval boundaries, and unresolved assumptions. Distinguish `explicit`, `inferred`, and `unresolved` statements; only approved interpretations become governing policy.

Every interpretation clause links to its originating human input. Present material ambiguities as choices instead of silently assigning utility weights. A faithful initial explicit instruction may authorize activation without an extra confirmation; additional inferred authority may not.

Monitor intent drift against task proposals and release candidates. A task may be technically interesting but rejected as outside the approved goal. Command can explain such a rejection without pretending the work is scientifically invalid.

### 2.2 Composer

Compile the approved mission and Score into readiness conditions, task templates, subscriptions, and departmental delegations. Maintain the changing activity graph, including provisional branches and competing approaches. Activate work, coordinate dependencies, request synthesis, and assemble release proposals. The Composer does not own departmental truth claims.

Composer may write `command/missions/*`, `command/plans/*`, and operational proposals. It does not rewrite `kb/*`, `strategy/*`, or `methods/*` to force agreement. It may propose a task or a change; the proper owner produces the resulting artifact.

The project-level `run-composer` entry point makes this control loop executable:
it admits the allowlisted topic-discovery, survey, experiment, interpretation,
argument, and paper stages; binds their exact outputs; records feedback and
checkpoints; and stops or resumes from the durable stage frontier. The specialist runners retain
their independent acceptance checks, so Composer controls procedure without
becoming the sole source of scientific truth.

### 2.3 Arbiter

Resolve contested objections, cross-department conflicts, and authority disputes using the applicable intent, evidence, and acceptance contract. Outcomes include upholding/rejecting an objection, requesting a specified investigation, selecting a permissible alternative, escalating, or acknowledging uncertainty.

A judgment cannot be justified merely by role seniority, majority agreement, or model confidence. Material decisions cite evidence and the governing criterion. If the Arbiter authored the contested artifact/decision, another qualified adjudicator or the Principal must handle the appeal. One appeal per issue is the default unless materially new evidence appears.

### 2.4 Progress Controller

Track accepted quality changes, decision-relevant discoveries, coverage, remaining risks, elapsed time, queue age, and spent/reserved resources. Publish a SupervisionDecision selecting the next work portfolio, continuation, reallocation, or stop; the scheduler admits and dispatches it under existing delegation. It does not invent scientific findings or modify the acceptance rubric to make progress appear positive.

Maintain a portfolio of actions: retrieve evidence, seek counterexamples, build an alternative, synthesize, repair a defect, verify a repair, inspect a regression, or compare candidates. Preserve materially different alternatives while their trade-offs remain unresolved. A plateau may require a different action or an independent diagnosis of the selection/evaluation policy. Publish wall-clock ProgressCheckpoints even when work is blocked or no result has improved.

### 2.5 Human control surface

Proposed commands cover intent inspection/amendment, pause/resume/cancel, task/status inspection, issue decisions, and scoped approvals. The Principal can intervene at any time. A mid-run change creates a new intent/mission revision and impact analysis; it never silently rewrites an active task's objective.

Human approval is bound to exact artifact hashes and governing versions. Silence is not approval. Approval of an outline does not authorize publication, paid providers, new data uploads, installations, or experiments unless these are separately delegated.

Outline approval is optional mission policy. Internal plans, outline alternatives, and revisions preserving the approved outcome can advance under delegation. Fresh approval is required for material scope/authority changes and the exact final release, not every compute window or internal replanning decision.

## 3. Department specifications

### 3.1 Research & Intelligence

| Role | Responsibility | Primary artifacts |
|---|---|---|
| Research Chief | Search coverage, synthesis, backlog prioritization, and formal evidence-acquisition quality | Survey plan/report; KB snapshot proposal |
| Search Strategist | Decompose information needs into search campaigns, query families, source classes, languages, and stopping criteria | SearchCampaign; coverage plan; query-family map |
| Academic Scout | Search papers, proceedings, theses, reviews, citation indexes, and scholarly repositories | Query records; scholarly discoveries |
| Open-Web Scout | Search official sites, institutional pages, technical reports, news, community/discussion surfaces, and general web sources for leads and context | Query records; web discoveries; lead bundles |
| Technical Ecosystem Scout | Search repositories, documentation, issue trackers, model cards, benchmarks, datasets, and software/tool ecosystems | Technical-source discoveries; ecosystem notes |
| Standards & Patent Scout | Search standards, regulations, patents, specifications, and other formal technical records when relevant | Standards/patent discoveries; status notes |
| Genealogy & Trend Analyst | Lineages, competing explanations, time-bounded gaps, citation/author/lab/project trails | Genealogy, trend, and counterevidence notes |
| Source Acquirer | Obtain the best authorized source representation available, preserve access/version/provenance, and record unavailable material | SourceCapture records; acquisition failures |
| Cataloger | Identifiers, deduplication, source/version metadata, source-class labeling | Reference cards; catalog projections |
| Fact Verifier | Claim/source correspondence, contradictions, and promotion eligibility into formal evidence | Evidence records; verification reports |
| **Research Adversary** | Attack coverage, selection bias, unsupported synthesis, omitted counterevidence through an independently planned retrieval route | Critiques; independent SearchCampaigns; review coverage |

Separate discovery, bibliographic identity, source acquisition, and scientific support. A search hit is a `DiscoveryRecord`; `DOI present` is not `claim verified`; a captured page or PDF is not automatically admissible evidence. Failed/empty queries are `QueryRecord`s, not fake references. A survey snapshot states search campaigns, source classes, languages, time bounds, exclusions, unavailable material, and known coverage gaps.

Research may proactively notify Strategy and Methods of counterevidence without waiting for a new survey stage. It cannot silently replace a source version in a snapshot already used by an approved draft.

### 3.2 Strategy & Writing

| Role | Responsibility | Primary artifacts |
|---|---|---|
| Strategy Chief | Contribution strategy, synthesis, writing allocation | Strategy briefing; candidate selection proposals |
| Narrative Architect | Claim/evidence argument and contribution positioning | Claim graph; storyline alternatives |
| Planner | Outline, section contracts, dependencies | Outline; section specifications |
| Section Writers | Purpose-scoped unit creation and revision under section contracts and edit grants | ContentUnit candidates and ChangeSets |
| **Strategy Adversary** | Attack overclaims, circularity, weak contribution logic, evasion of the mission | Argument critiques and counterexamples |

The original Feasibility Red Team becomes Strategy Adversary for argument-level attacks. Scientific-method review moves to the independent Methods department rather than disappearing.

Writers receive approved terminology, required claims, available evidence, limitations, section scope, a citation map, and exact edit grants. Broad read context does not enlarge write scope. They cannot create new result values or promote speculation to an observed finding. Alternative storylines are candidates under explicit structure/creation plans, not uncontrolled changes to the Principal's goal or a bypass around scoped edits.

### 3.3 Methods & Validation

| Role | Responsibility | Primary artifacts |
|---|---|---|
| Methods Chief | Decide validation scope and synthesize qualified conclusions | Validation plan; method assessment |
| Methodologist | Design/measurement assumptions, causal and logical validity | Method checks; missing-evidence requests |
| Statistical Reviewer | Examine supplied estimands, uncertainty, comparisons, and reported procedures | Statistical review; declared limitations |
| Reproducibility Reviewer | Trace supplied outputs to documented procedures/configuration | Reproducibility checklist; gap report |
| **Methods Adversary** | Attack the validity of Methods' own approval, rejection, or requested work | Meta-validation critiques; concrete counterexamples |

In v1 this department reviews supplied descriptions and results. It does not run new tests, fit models, estimate uncertainty, or execute experiments. It can request additional results from the Principal and mark a claim unsupported until they arrive.

It also guards against over-demanding reviews: an adversary may demonstrate that a proposed extra experiment is irrelevant to the approved claim, or that a rejection assumes an unstated requirement. Validation is not a license to expand the mission indefinitely.

### 3.4 Editorial Office

| Role | Responsibility | Primary artifacts |
|---|---|---|
| Editor-in-Chief | Editorial policy and synthesis; propose readiness | Editorial verdict |
| Structural Editor | Organization, reasoning flow, redundancy | Structural review; patch proposal |
| Format Editor | Approved venue assets, assembly, compilation | LaTeX bundle; build report; PDF candidate |
| Consistency QA | Terms, supplied numbers, claim/citation references | Consistency report |
| **Editorial Adversary** | Attack editorial decisions, semantic damage, and false format passes | Critiques of reviews/patches/build acceptance |

Editorial proposes semantic ChangeSets; Strategy owns narrative content and proposes adoption through the controlled integration service. Editorial owns assembly assets under separately scoped grants and may produce a rendered candidate from pinned units. The assembler cannot invent or polish prose. A semantic change introduced during assembly must return to Strategy/Methods for revalidation; compilation success cannot authorize it.

### 3.5 Organization-wide Web Intelligence Fabric

The web is an **organization capability**, while Research is the **evidence-acquisition authority**. Strategy, Methods, Editorial, and their adversaries may browse directly when a task requires external information; they do not have to wait for a Research stage. Their discoveries remain leads until the appropriate source/provenance checks promote them into the formal evidence base.

| Organ / role | Authorized retrieval purpose | Formal evidence authority |
|---|---|---|
| Research | Broad exploration, literature/systematic search, citation/author/project chasing, source acquisition, gap closure | Owns catalog/source-capture publication and proposes evidence promotion |
| Strategy | Novelty framing, competing explanations, terminology, comparable contributions, claim-specific checks | May submit discoveries; uses promoted evidence for material manuscript claims |
| Methods | Methods/statistical/reporting standards, benchmark practice, validation assumptions, reproducibility guidance | May submit discoveries and method evidence; formal promotion follows verification rules |
| Editorial | Venue instructions, official templates, reporting guidelines, style/citation requirements, current publication conventions | Owns verified venue/format requirement artifacts in Editorial; scientific facts still follow evidence rules |
| Department adversaries | Independent counter-search, alternative terminology/source routes, contradiction/counterexample discovery | May submit discoveries/critique evidence; cannot self-certify disputed promotion |
| Command / Arbiter | Targeted verification only when needed for a decision or to commission an investigation | Does not become the default research owner; material factual disputes use qualified evidence verification |

#### 3.5.1 Retrieval surfaces

The provider layer is adapter-based. A mission may authorize any subset of these **source classes** without hard-coding a vendor:

- scholarly indexes/repositories and publisher pages;
- general web search and direct site traversal;
- official institutional/government pages and technical reports;
- repositories, documentation, issue trackers, model cards, benchmarks, and datasets;
- standards, regulations, patents, and formal specifications;
- citation/reference graphs, author/lab/project trails, and related-work links;
- news, forums, social/community discussions, and other discovery-oriented surfaces;
- multilingual queries and original-language sources when relevant.

Community posts, news, snippets, and AI-generated/search summaries are useful **discovery surfaces**, but they do not automatically satisfy a scientific claim gate. The system attempts to trace consequential leads to the strongest accessible primary or authoritative source appropriate to the claim. Source class is recorded; it is not a universal quality score.

#### 3.5.2 SearchCampaign lifecycle

```text
information need
   ↓
Search Strategist / task owner
   ↓
SearchCampaign
   ├─ concept decomposition
   ├─ synonyms / historical terms / adjacent-field terms
   ├─ source classes + languages + time bounds
   ├─ positive / neutral / counterexample query families
   └─ explicit coverage targets / stopping conditions
   ↓
QueryRecords → DiscoveryRecords
   ↓                    ↓
query expansion      dedupe / prioritize
   ↓                    ↓
reference / author / project chasing
   ↓
SourceCapture / unavailable-source record
   ↓
verification + evidence promotion
   ↓
CoverageReport + KB snapshot
```

A campaign can reopen when a new contradiction, terminology shift, source lead, Principal instruction, or adversarial critique exposes a material coverage gap. Reopening increments the campaign generation and preserves the earlier search history.

#### 3.5.3 Coverage and stopping

Search does not stop at an arbitrary result count. `CoverageReport` records which concepts, query families, source classes, languages, date ranges, citation directions, and counterevidence routes were attempted; what remained inaccessible; and where new searches stopped producing decision-relevant discoveries. The scheduler balances expected information value against remaining resource budget.

Coverage saturation is evidence for stopping, not proof that nothing was missed. High-stakes novelty or contradiction checks may require additional independent routes before G1/G3 can pass. A search campaign can terminate as `coverage_met`, `diminishing_return`, `budget_exhausted`, `access_blocked`, or `needs_human_scope`. `budget_exhausted` means a hard applicable resource limit prevents continuation; exhaustion of a renewable window instead triggers an allocation checkpoint.

#### 3.5.4 Independent adversarial search

Each departmental adversary receives a reserved retrieval tranche distinct from the producer's discretionary search. The initial adversarial campaign may inspect the producer's cited evidence but must be free to create alternative terminology and query families, search different authorized source classes, and seek evidence that would falsify or narrow the producer's conclusion. The adversary is not rewarded for number of negative hits; unsupported leads remain investigation requests rather than confirmed defects.

#### 3.5.5 Discovery → evidence promotion

```text
QueryRecord → DiscoveryRecord → ReferenceCard → SourceCapture → EvidenceRecord
                                      │              │
                                      └─ identity     └─ exact accessible content/locator
```

A department outside Research may create `QueryRecord`/`DiscoveryRecord` artifacts in its own discovery namespace and request promotion. Research owns canonical deduplication and source capture for scientific evidence. Fact Verifier or another qualified verifier checks the exact support relation. Editorial may independently own official venue requirement captures because those are editorial requirements rather than scientific literature claims.

Every promoted evidence item retains the discovery path, source URL/identifier, access time, source version where obtainable, capture hash/locator, extraction method, and access limitations. Failed acquisition is retained so another agent does not repeatedly assume the full source was inspected.

#### 3.5.6 Active integration and source updates

Implement the capability registry and normalized API/tool adapters in `50-web-intelligence-integration.md`. Material questions and gaps can trigger direct search, source acquisition, citation chasing, independent counter-search, or a missing-capability integration task. Already authorized capabilities can be used without a new human checkpoint. Actual deployment bindings, schema compatibility, rate limits, and source access are tested; host-environment plugins are not assumed to exist in the standalone runtime.

New captures, correction/retraction signals, or source versions trigger claim/unit impact analysis. Scientific evidence is promoted only after checking the inspected content. Affected manuscript changes are proposed through ChangeRequests and EditGrants; no retrieval adapter can overwrite a paragraph or silently repoint a citation.

## 4. Organic collaboration and delegated initiative

### 4.0 On-demand practical operations

When an operational gap blocks or materially improves a task, activate the project's Operations Cell under `15-project-organization.md`. Its coordinator/engineer/operator roles prepare and run suitable programs or API/MCP services under project delegation. Independent operational probes establish usability; the consuming department verifies the domain result. The cell then idles, maintains the service, or tears it down according to project policy. Existing enabled capabilities remain directly usable by department workers.

Operations publishes environment/capability profiles, execution reports, and candidate outputs through the same store and scoped change service. Successful installation, service startup, or exit code does not establish a valid scientific claim or accepted manuscript change. All projects retain separate bindings and data even when they reuse the same program image or host.

### 4.1 Department state

Each department has a versioned charter, role manifests, a durable backlog, scoped subscriptions, reserved review capacity, and a bounded project memory. Memory is a referenced synthesis of accepted artifacts and unresolved issues, not an unbounded transcript.

Departments can self-propose work from a new source, a detected contradiction, a dependency change, a review request, an unmet mission requirement, or a web discovery produced by their own authorized retrieval. A proposal names the intended benefit, input versions, required outputs, capability needs, and estimated resource tranche.

The scheduler—not an unconstrained chat agent—checks authorization, duplicates, readiness, capacity, and budgets. An in-scope low-risk task can run under existing delegation; a scope expansion, paid provider change, or protected-data transfer requires command/human authorization.

Task proposals carry a question/construction objective, expected useful output, evaluation method, hypothesis/approach family, and reassessment horizon. Distinct families may run concurrently. Temporary groups do not gain additional authority or direct writes to another owner's namespace.

### 4.2 Exchange rules

Any department may address another through a durable message. Chiefs coordinate task commitments; direct specialist-to-specialist messages are allowed for precise questions and critical discoveries. No department may overwrite another's artifact. Requests and evidence references travel through the bus; large content is shared by immutable reference.

A request must receive a recorded disposition: accepted into a task, linked to existing work, deferred with reason, rejected as out of mandate, or escalated. Receiving a message is not accepting its factual assertion or its implied authority.

The default traffic policy limits redundant messages per issue and deduplicates identical task proposals. A repeat without new evidence refers to the existing issue rather than reopening a debate. A new severe finding may bypass normal queue order, but still needs admission checks and a reserved resource allowance.

### 4.3 Events and selective reopening

Readiness depends on approved artifact versions, not simply whether a stage once completed. When a source, claim, method assumption, intent revision, or template changes, use dependency links to mark affected approvals stale. Reopen the smallest necessary task set.

Historical approvals remain valid statements about the earlier versions. A new candidate awaiting adjudication does not automatically erase the incumbent. A verified counterexample or approved replacement dependency can invalidate it; merely receiving an unsupported critique cannot.

Each task reference distinguishes a premise from a subject of inspection. Premise edges state whether the task needs an accepted input or permits an exact provisional candidate. Provisional work may inform further exploration, but cannot satisfy commitment gates until its required premise closure is accepted and current. Review and verification inspect exact candidate subjects without requiring them to pass acceptance first; their judgments must have valid supporting premises. A changed input or subject can stale the downstream candidate or judgment; it never updates an active call's context silently. This separates exploratory readiness, inspection, and commitment without weakening evidence gates.

### 4.4 Example: a novel-claim collision

1. Research identifies a newly captured paper that appears to predate the proposed novelty claim. It sends a claim-specific finding to Strategy and Methods.
2. A validated issue triggers independent novelty verification. Strategy does not immediately remove the contribution or assume the new paper is equivalent.
3. Methods checks whether the prior result and the supplied result concern the same conditions and inference. Strategy may supply an evidence-backed rebuttal.
4. An adjudicator either rejects the collision, confirms it, or marks the evidence insufficient. Confirmed impact stales the affected storyline, outline approval, and introduction—not unrelated result transcriptions.
5. Strategy constructs an alternative contribution statement. Its adversary attacks that candidate; verification checks both the repair and retained mission coverage.
6. Command asks the Principal only if the viable alternative changes the approved contribution goal or another material trade-off.

The outcome can be a stronger paper, a narrower defensible paper, or an honest gap report. Producing text is not mandatory when the scientific premise fails.

## 5. Shared adversarial protocol

### 5.1 Role independence

Each department names one accountable adversary role in its manifest. The scheduler reserves its budget separately from the producer's discretionary budget. More adversarial invocations are allowed for independent checks; more standing agent processes are not required.

Initial review is blind to the producer's conversation and self-justification but includes the mandate, relevant evidence, target artifact, required coverage, and evaluation contract. Different system prompts do not make the same model epistemically independent. Critical disputed claims use different evidence routes, deterministic checks, a fresh qualified adjudicator, or human assessment where needed.

### 5.2 What the adversary may output

A review invocation returns an immutable ReviewCoverage record and one of four outcomes:

| Outcome | Meaning | Next action |
|---|---|---|
| `objections_found` | Specific, potentially valid defects found | Register/deduplicate critiques, obtain responses, adjudicate |
| `no_valid_objection_found` | Review completed; no justified objection found within stated coverage | Continue other gate checks; do not treat as proof of correctness |
| `insufficient_evidence` | Review cannot assess a material point with current inputs | Request specified evidence; block only the affected acceptance condition |
| `review_failed` | Tool, parsing, context, or runtime failure | Bounded operational retry; never convert into a pass |

The reviewer searches adversarially but does not have to emit a negative factual assertion when none is justified. Praise, personal attacks, vague demands, manufactured certainty, and duplicate issue inflation are invalid outputs.

### 5.3 Critique admissibility

A critique must identify the exact target/location; violated requirement or challenged claim; supporting evidence, counterexample, or reproducible test; impact; proposed severity; and a resolution/verification condition. A suggested fix is useful but not mandatory when the critic can specify what would establish validity.

“Run more experiments” without explaining the failed inference is not admissible. “The supplied result is an association, while claim C7 asserts causation; no causal identification procedure is supplied” can be admissible. Lack of evidence is a reason to qualify a claim, not proof that its negation is true.

### 5.4 Response, judgment, and closure

The producer may accept and repair, rebut with evidence, or request missing information. It cannot close an issue by declaring compliance. The original reviewer cannot be the sole final authority on a contested criticism.

A non-conflicted chief can adjudicate routine matters. Material scientific disputes go to an independent Methods adjudicator or command Arbiter; a dispute about Methods' own judgment requires another qualified adjudicator or the Principal. An appeal is bounded and must state its basis.

Closure requires a separate verification artifact and a recorded disposition. Distinguish `resolved_verified`, `rebutted`, `rejected_invalid`, `duplicate`, and `risk_accepted`. Only the first is a verified repair; `risk_accepted` is not scientific validation and cannot waive a blocking integrity condition.

For a research paper, this protocol is a bounded peer-review cycle rather than
a one-shot style pass. The same panel reviews the initial manuscript, the
author applies only the accepted surgical repairs, and the panel re-reviews the
new incumbent. Three rounds are required before the editor-in-chief decides.
Any reviewer may issue a structured request for new literature, a discriminating
experiment, an interpretation expansion, or an analysis display. Such a request
keeps the manuscript out of release until its success condition is met; an
unresolved request cannot be hidden by rewriting the affected paragraph.


At the workflow level, the Composer treats those requests as executable
continuations. It maps each request to the smallest owning stage closure,
creates a cycle-specific project namespace, reruns changed consumers, and
limits re-entry by `continuation_policy.max_cycles` and the original hard wall.
A free-topic workflow may begin with the `topic_discovery` stage. A horizon
scanner first creates cross-domain scientific seeds without seeing experiment
templates. Recent OpenAlex records are relevance-filtered, balanced across the
seed domains, and retained with the exploration seed and query/capture trace.
Each candidate must bind a real seed and supplied work IDs, while the candidate
portfolio must cover multiple seed groups and domains. The selected question
also receives a deterministic distance check against hidden fallback templates.
A targeted source challenge and maturity review must both admit the selected
question. With a configured capability foundry, the question is then copied
unchanged into a generated executor and independently authored validator; only
a sandboxed, deterministically replayed, digest-matched, independently
recalculated, readiness-probed, adversarially admitted descriptor becomes
executable. Its immutable registry graph is hash-verified on every load, and
the deny-by-default sandbox remains mandatory during actual execution. Topic
metadata remains provisional until the ordinary survey and evidence gates
promote it.

### 5.5 Candidate selection

The latest version never wins by recency. Compare the candidate with the incumbent under the same intent, evidence set, and evaluation contract. Run required coverage and non-regression checks. Record `promote`, `retain`, `incomparable`, or `needs_human_tradeoff` with supporting artifacts.

A rejected candidate is preserved for audit, but not used as the default basis of future writing. If new evidence invalidates the incumbent, mark it inadmissible and do not continue presenting it as an approved best result.

Maintain a candidate frontier for materially different approaches, including incomparable candidates with explicit trade-offs and validity states. It is a projection of existing artifacts and selection records, not another mutable authority. A promising provisional branch can receive more work without being promoted or replacing the incumbent.

## 6. Compute → progress control

### 6.1 ProgressRecord

A ProgressRecord binds an action and its actual cost to baseline/candidate versions, evaluation-contract version, issue dispositions, evidence gained, coverage/regression checks, and selection outcome. Separate artifact improvement from information gain. A newly discovered major flaw may be valuable information while leaving no improved deliverable yet.

A vector is preferable to an unexplained aggregate score:

```text
hard-constraint failures
required-claim / section coverage
independently validated defect status
claim-evidence support and uncertainty
intent-aligned comparative quality
human repair effort (when measured)
resources spent and remaining
```

The priority order comes from approved intent. No subjective 0.91 confidence is treated as a calibrated probability by default. An issue-count decrease is a diagnostic, not the optimizer's reward.

### 6.2 Allocation rule

Choose a portfolio using expected decision-relevant benefit, uncertainty, elapsed time, available capacity, verification/integration backlog, cost where applicable, and mission priorities. Start with transparent heuristic prioritization rather than an unvalidated reward model. Keep estimates distinct from observed gains. A SupervisionDecision records alternatives considered, allocation, expected useful observations, and a reassessment condition.

Useful choices include gathering a missing full text instead of debating its abstract; testing a specific counterexample instead of rewriting the entire paper; generating a second storyline instead of polishing an invalid first one; or using a stronger approved verifier only for a high-impact unresolved issue.

Deterministic tasks should stay in code. Role-specific model profiles route extraction, writing, critique, and adjudication separately. Use capable authorized models and substantial reasoning where useful; do not make a low-cost model the default merely to save tokens. Inference has finite elapsed time and capacity even when there is no per-token bill. No model family is assumed capable merely because it fits available VRAM.

Reserve capacity for independent review, evidence acquisition, and integration as well as production. Unverified backlog and queue age impose backpressure on new candidate generation. Exploration remains available through mission-defined coverage and diversity requirements, not arbitrary percentage weights. Existing task delegations admit routine work without serial supervisory inference per dispatch.

### 6.3 Stop and escalation

There are no universal review-round, reopen-count, message-count, or lifetime inference-call caps. Configure finite attempt deadlines, provider retry limits, lease durations, checkpoint intervals, and actual capacity/expenditure boundaries. An AllocationWindow can renew automatically under an active ResourcePolicy and recorded SupervisionDecision. A window ending is not a quality verdict or a request for renewed human permission.

Continuation requires a useful expected observation, a materially different investigation, or a justified intermediate milestone in a longer plan. Preserve cumulative usage, causal issue identity, and stagnation history across windows. Failed providers use bounded retry/backoff; conversational repetition is deduplicated; neither can be laundered into a new research budget by renaming a task.

Stop when the mission's completion condition is met; pause affected work when it requires missing evidence/authority or has no justified continuation; terminate at cancellation or an explicit hard limit. A mission may explicitly authorize continued improvement after an initial delivery. Stagnation redirects work or ends it—it does not silently relax requirements. Checks occur before every dispatch and include concurrent reservations.

### 6.4 Elapsed-time checkpoints and supervision of supervision

Checkpoint cadence is operational configuration, independent of model-call completion. The native scheduler produces a checkpoint even if a long-running task or supervisor call is late. It records a cutoff, governing versions, incumbent/current admissibility, useful alternatives, verified artifact/information deltas, blockers, execution health, cumulative usage, and the next reassessment. Missing judgments remain pending; the scheduler cannot invent progress to fill the report.

Information progress names the affected question, previous state, evidence, and changed decision. Deduplicate it across branches. Long activities declare intermediate milestones and a horizon for reassessing value; checkpoint silence alone does not justify terminating them. Report time to first usable result, periods without validated change, and critical-path queues separately from total compute.

Consequential allocation/closure decisions, contradicted predictions, and sampled routine decisions receive independent review. Reviewers can challenge the supervisor's policy or evaluation basis. The Arbiter must recuse from decisions it authored. Use existing adjudication and authority boundaries to resolve disputes; no recursive hierarchy of supervisory agents is required. Prompt/method changes within delegation may proceed, while changes to governing criteria or authority require the Principal.

## 7. Evidence model and research inputs

### 7.1 Results package

Preserve user inputs in `inputs/`. A v1 ResultsPackage contains figure/table files, reported metrics and units, experimental conditions, analysis/procedure descriptions, user-stated factual findings, limitations, and provenance references when available.

Every item distinguishes user-supplied/approved content from machine-derived interpretation. Missing information stays missing. Ingestion checks schemas, file integrity, declared units, and transcription consistency; it does not authorize new scientific analysis.

### 7.2 Literature evidence

A SearchCampaign defines a bounded information-acquisition objective and coverage plan. Each external call yields a QueryRecord, including empty/failure outcomes. A DiscoveryRecord is a candidate lead returned or reached from a query. A ReferenceCard canonicalizes source identity. A SourceCapture stores the best authorized source representation or bounded excerpt plus checksum and locator. An EvidenceRecord anchors an exact excerpt, table cell, figure, or supplied result and records extraction type and access limitations. A CoverageReport summarizes what was and was not searched.

A Claim links to evidence with `supports`, `partially_supports`, `contradicts`, or `context_only`, together with an explicit interpretation. A source's existence and an LLM-generated summary cannot stand in for a support link. Quantities must include conditions/units as needed to preserve meaning.

Abstract-only access cannot justify assertions requiring undisclosed methods or detailed numerical results. Unavailable evidence may remain in the catalog but cannot satisfy the claim's release gate.

### 7.3 Dependency and review coverage

Maintain version-pinned links from intent → mission → tasks → source/result evidence → claims → outline/sections → manuscript → rendered bundle → approvals. Shared terminology and symbol definitions are also versioned inputs.

Each substantive deliverable must have an adversarial coverage record for the material risk classes relevant to it. This does not require a full LLM review for every metadata row; deterministic checks and a review of the departmental bundle may cover those artifacts. The coverage record must list exact covered versions and exclusions so critical content cannot disappear into an unchecked bundle.

### 7.4 Structured deliverables and surgical changes

ContentUnit artifacts represent paragraphs, headings, and other meaningful blocks with stable identity, purpose, claim/citation links, and exact source/asset dependencies. A DocumentManifest pins their ordered containment structure and versions; generated LaTeX/PDF is derived from that manifest. Moves preserve identity, while split/merge operations preserve lineage. Namespace ownership alone grants no document-wide rewrite capability.

Every modification binds a ChangeRequest to a purpose, baseline, mutation scope, preserved requirements, and verification plan. EditGrants restrict a worker's units/spans/fields, structure, and dependency operations independently of its read context. The service computes complete mutation and impact sets, including shared definitions/macros/templates and reference bindings, then requires appropriate independent verification before adoption.

Local revisions can require broad read or review scope. Coupled claim/paragraph/abstract repairs are staged and adopted atomically. Concurrent disjoint text patches still require shared-premise and assembled-argument checks. Existing review results are reused only through explicit unchanged-unit/dependency applicability, never by copying approval to a new document hash. `45-artifact-change-control.md` defines the authoritative protocol.

## 8. Execution and adapter boundaries

The native control plane owns authorization, scheduling, budgets, durable task/message state, artifact publication, gates, and release. A runner performs one authorized role invocation and returns proposals/artifacts through the service boundary.

Conceptual interface:

```text
Runner.invoke(TaskAttempt, ContextPackage, CapabilitySet)
  → proposed_artifacts, proposed_messages, tool_call_records,
    observed_usage, completion_or_failure
```

A runner does not receive raw write access to the project, database, event log, or another department's files. It cannot grant itself tools, change budgets, satisfy its own independent review, or mark an arbitrary artifact approved.

Artifact mutation additionally requires a valid EditGrant; a runner returns a ChangeSet rather than a replacement assembled document. Retrieval/API/MCP adapters return normalized observations and captures through the same service boundary. Retrieval rights and artifact-edit/adoption rights are separate.

The first backend is an authorized inference endpoint, normally the external GPU pool; the same adapter contract supports local inference. Capability probes establish model identity, context limits, cancellation behavior, observed throughput, and supported concurrency before dispatch. LangGraphRunner/AutoGenRunner are later optional adapters. Their internal checkpoints do not replace Sci-saurus's intent/artifact/approval contracts. Remote workers use the orchestrator service, not shared mutable queue files.

### 8.1 ContextBuilder

Build a versioned ContextPackage from required task inputs, applicable intent clauses, source evidence, terminology, current issues, and relevant messages. Pin the versions used; record truncation/exclusion decisions. Never silently omit required evidence to fit a token limit—split the task or request a different authorized context budget.

Read authorization, retrieval selection, and actual model context are separate layers. Untrusted document text is evidence, not executable instruction. Private research data only goes to providers allowed by the project data policy. Store concise decision rationales; do not require hidden model chain-of-thought.

## 9. Storage, concurrency, and recoverability

### 9.1 Authority split

| Data | Canonical owner | Derived representation |
|---|---|---|
| Immutable content | Content-addressed objects | Readable workspace files |
| Version manifests, accepted lifecycle events, message bodies | Transactional control store | JSONL exports, status indexes, Git snapshot manifests |
| Current task/issue/head state | Reducers over events plus transactional projections | CLI status and department backlogs |
| Snapshot/release commit | Git + pinned release manifest | Human-readable release notes |

Use SQLite locally with one orchestrator-controlled writer and short transactions as the initial control-store design. Remote inference executes outside database transactions. Measure commit latency and queue delay as concurrency grows; a database migration requires evidence of a control-plane bottleneck, rather than GPU count alone.

### 9.2 Project layout

```text
projects/<project_id>/
  objects/sha256/                 # immutable body/source blobs
  manifests/artifacts/            # immutable version-manifest exports
  workspace/                     # materialized views; never model-writable directly
    inputs/                      # originals and supplied ResultsPackage
    command/                     # intent, missions, plans, decisions, progress
    kb/                          # source cards, evidence, surveys, snapshots
    strategy/                    # claims, storyline, outline, sections
    methods/                     # assessments, limitations, validation records
    editorial/                   # reviews, assembly assets, build reports
    operations/                  # project environment plans, bindings, run reports
    issues/                      # critique/response/adjudication/verification views
    releases/                    # pinned release manifests and summaries
  state/control.sqlite           # local operational store; excluded from Git
  ledger/events.jsonl            # generated audit export at a known sequence cutoff
  runs/                          # durable attempt/context/tool records
  .git/                          # curated immutable exports and snapshot history
```

Narrative ContentUnits use `strategy/units/*`, and DocumentManifests use `strategy/documents/*`. Section-oriented files are materialized views of that structure. Existing monolithic drafts are historical/import artifacts until a checked structural mapping exists. Build sandboxes live outside immutable project data; published candidates and their acceptance states remain distinct.

### 9.3 Publish transaction and crash boundaries

1. Validate role permissions and input/base-version preconditions. Compute canonical hashes.
2. Write new object bytes to a temporary local file, flush, and atomically publish the content-addressed blob. Existing matching objects are reused.
3. In one database transaction, allocate the version, append its manifest/event, update transactional projections, and record required outgoing work in an outbox.
4. After commit, materialize views/exports and make Git snapshots through retryable idempotent jobs. These derived steps may lag; they are not falsely claimed to be atomic with the database.

A crash before the database commit may leave an unreferenced blob, not a published artifact. A crash after commit leaves a recoverable outbox action. Garbage collection excludes objects with active publication leases and all referenced/released objects.

Concurrent candidates based on the same parent receive distinct versions and the same parent link. Automatic `version+1` does not imply acceptance. Adoption uses a compare-and-swap check against the expected incumbent and relevant governing versions. A losing candidate is preserved and must be reevaluated rather than silently merged.

For structured-document changes, publication also checks grant validity and exact unit/edge/binding preimages. Independently verified ChangeSets produce a candidate DocumentManifest; accepted-head CAS rechecks current scope, premises, and governing conditions. Coupled edits change one manifest atomically. Recovery or reversal follows scoped ChangeSets against the current version, preserving unrelated improvements. Direct edits to generated files, Git snapshots, or assembly assets cannot bypass publication or grant checks.

### 9.4 Immutability and status

Artifact content and version metadata never mutate. “Approved,” “superseded,” “rejected,” and “stale” are lifecycle events/projections, not editable frontmatter fields on an old immutable object. Status changes carry actor, reason, target version, governing references, and expected state.

A hash chain detects alteration against a trusted head. It does not detect an attacker who rewrites all local events and all local anchors consistently. Optional external signed/copied release manifests establish a separate trust boundary; plain Git history alone is not a tamper-proof audit system.

### 9.5 Message delivery and external calls

Delivery is at-least-once with per-message acknowledgement, bounded retries, idempotency keys, leases, and deduplicated effects. An atomic outbox couples accepted task results and outgoing messages. Exactly-once external execution is not promised.

A task attempt pins its inputs and records model configuration, tool permissions, external-call request IDs, available outputs, and measured usage. Replaying recorded events restores accepted state; it does not regenerate a missing LLM response. For a timed-out call with unknown completion, retain uncertainty and charge/reserve conservatively until reconciled. A retry may duplicate provider cost even when artifact publication is deduplicated.

### 9.6 Release transaction

Prepare a release manifest containing exact artifact hashes, intent/mission/Score/evaluation versions, approval hashes, and an event-sequence cutoff. Verify dependency closure and current acceptance. Export the matching snapshot and create its commit/tag idempotently. Before appending `release.finalized`, atomically recheck expected governing versions, premise validity, exact subject/review coverage, and approval applicability. If concurrent invalidation or redirection changed that basis, retain the prepared snapshot with stale applicability and require revalidation; a Git tag alone is not finalized release authority.

The manifest need not contain its own eventual finalization event; that would create a circular hash dependency. A failed Git step leaves the release in `prepared`, not a completed release. A newer candidate never inherits approval from an older hash.

## 10. Permissions

All writes below mean authorized publication through Archivist, not raw filesystem access.

Namespace authority is necessary but insufficient for a modification. Document writers, chiefs, editors, and command roles must also hold an applicable narrow EditGrant; acceptance still requires independent verification and selection. The deterministic integrator composes proposals without gaining creative rewrite authority.

| Namespace / action | Authorized author or authority |
|---|---|
| `inputs/*` original content | Principal/import service; all departments may reference allowed inputs |
| `command/intent/*` | Intent Keeper proposes; Principal authorizes activation |
| `command/missions/*`, `command/plans/*` | Composer within approved intent |
| `command/decisions/*` | Non-conflicted Arbiter; explicit Principal decisions preserved separately |
| `command/progress/*` | Progress Controller; supporting verification comes from qualified roles |
| `kb/*` | Research |
| `strategy/*` | Strategy |
| `methods/*` | Methods |
| `editorial/*` | Editorial |
| `issues/*` | Any authorized role may submit its own immutable record; transitions require lifecycle-specific authority |
| Accepted-head change | Owner proposes; policy checks independent verification and required approvals |
| `releases/*` | Command proposes, Principal approves, Archivist finalizes |
| Event log / state / release tags | Archivist/control plane only |

Adversaries may author critiques and investigation requests, not the target artifact. Read-all is a project default subject to sensitive-data restrictions, source access rights, and context policy. A role cannot self-approve by changing the apparent actor string.

## 11. Paper Score and gates

### 11.1 Milestones, not an irreversible conveyor belt

| Stage | Lead | Required output/readiness | Gate |
|---|---|---|---|
| S0 intake | Command + Methods | Approved intent/mission; ResultsPackage inventory; explicit provider/budget/data policy | G0 |
| S1 survey | Research | Sources, evidence, counterevidence, reviewed KB snapshot | G1 |
| S2 argument + validation | Strategy + Methods | Claim graph, alternative storyline where needed, Methods assessment | G3 |
| S3 outline | Strategy + early Editorial | Reviewed outline and section/evidence contracts | G5a |
| S4 drafting | Strategy | Reviewed sections and integrated manuscript candidate | G2 |
| S5 editorial | Editorial ↔ Strategy/Methods | Verified editorial changes and consistency report | G4 |
| S5.5 assembly/rendering | Editorial | Reproducible LaTeX bundle, PDF, build/visual inspection report | G4b |
| S6 release | Command + Archivist | Accepted dependency closure and Principal-approved release candidate | G5b |

Methods intake review may begin before the survey. Research subscriptions remain active during writing. All substantive departmental outputs use the same adversarial cycle. The Score defines which dependencies must be accepted before a stage may commit its result.

### 11.2 Gate classes

| Gate | Mechanical checks | Evidence-based judgment / human condition |
|---|---|---|
| G0 | Required configuration, authorized budget/capabilities, valid input inventory | Faithful intent interpretation; usable supplied results; activation authority |
| G1 | Source identities, deduplication, capture/access metadata, resolvable evidence links | Adequate survey coverage, counterevidence handling, stated access limits |
| G3 | Required claim/result/Methods references present | Conclusions defensible under supplied methods/results; no upheld unresolved blocker |
| G5a | Outline hash and dependencies current | Independent coverage review; Principal approval only if mission policy requires it or contribution/scope materially changes |
| G2 | Citation keys, evidence references, required claims/sections, number transcription | Citation–claim entailment and qualified scientific coverage |
| G4 | Terminology/reference consistency and required assets | Coherent structure; editorial edits preserve scientific meaning |
| G4b | Successful isolated compilation; no undefined citations/references; files complete | Visual inspection for clipping, unreadable figures/tables, misplaced material; venue policy applied |
| G5b | All required approvals current, dependency closure, budget and issue accounting complete | Principal approves exact final deliverable; disclosure and unresolved limitations visible |

A GateResult has `pass`, `fail`, `needs_evidence`, `needs_human`, or `stale`. Judgment results include their scope and uncertainty; deterministic pass is not scientific pass. Unused/unverified catalog entries need not block a paper, but unsupported material claims cannot enter a verified final release.

Final release includes LaTeX sources, bibliography, required permitted assets, PDF, build environment description, evidence/claim index, review/decision summary, contribution/disclosure summary, and pinned release manifest. External submission is a separate, excluded action in v1.

## 12. Security and operations

Retrieved web pages, search snippets, documents, PDFs, repository text, issue/forum content, metadata feeds, and LaTeX assets are untrusted input. They cannot change instructions, grant capabilities, request secret disclosure, or authorize tool execution. Apply source/text separation and validate requested actions outside the model.

Use sandboxed compilation with no network, no secrets, resource limits, restricted filesystem access, and shell escape disabled. Shell-escape disabling alone is not a full sandbox. Venue templates and bibliography assets must come from approved/pinned sources. Logs must avoid credentials and unauthorized source reproduction.

Unapproved external providers remain disabled. No automatic cloud fallback for private user data. Use narrow task scopes and backend-neutral role profiles; provider/version selection remains project configuration.

Operational observability covers queue age, blocked reasons, per-role cost, reserved budget, external calls of unknown outcome, issue precision, candidate rejection/regressions, and human interventions. Do not optimize “agents active” or “messages generated.”

## 13. Implementation boundary

This is a blueprint. The additional contract and example files define a coherent first implementation target; they are not proof that a scheduler, database, critic, or PDF compiler has been built or benchmarked. P0–P3 in `30-roadmap.md` make that distinction testable.
