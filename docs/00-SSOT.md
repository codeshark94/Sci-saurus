# Sci-saurus — Concept SSOT

> **Version:** v1.5 · **Date:** 2026-09-13 · **Status:** normative design; the project-scoped department runtime and Composer work-order loop are implemented, with operational capability use remaining explicitly configured.
> This is the normative source for purpose, authority, terminology, and design decisions. [System Concept](05-system-concept.md) explains the organizing model. Architecture and execution contracts implement these decisions.
> Preserve prior decisions. Amend them with explicit superseding decisions rather than rewriting history. Historical decision entries below are retained; this checkout does not contain a separate historical document archive.

## 1. Definition and primary objective

**Sci-saurus is a Principal-directed, compute-rich, general-purpose project organization that pursues evidence-backed progress through flexible, rationally supervised activity.** Investigation, alternative construction, synthesis, criticism, verification, and replanning are complementary reasoning activities. Its reusable core organizes goals, tasks, evidence, artifact versions, tools, independent checks, and time. The mission selects the deliverable and domain requirements.

The flagship mission is **a user-supplied results package + rough storyline → broad external intelligence gathering → defensible argument → English manuscript → LaTeX source and rendered PDF**. Research & Intelligence, Strategy & Writing, Methods & Validation, and the Editorial Office retain distinct responsibilities while collaborating through a shared workspace, an organization-wide Web Intelligence Fabric, and recorded, event-driven exchanges.

The central design objective is **compute → verified progress toward the Principal's goal**. This is a hypothesis to test, not a guarantee that additional inference produces a better artifact. Spending more compute must buy useful search, stronger evidence, independently verified repairs, better candidates, or reduced decision-relevant uncertainty. Extra messages and additional versions are not, by themselves, progress.

External GPU capacity is assumed plentiful. The default policy spends generously on capable reasoning, depth, diverse approaches, and verification. It optimizes useful outcomes over elapsed time rather than minimizing tokens. Actual backend capacity and authorization are configured and measured; abundant inference does not create missing external evidence or an infallible evaluator.

## 2. Vision roadmap

| Horizon | Capability | Boundary |
|---|---|---|
| v1 | General-purpose project organization with mission-specific Scores, elastic inference, functional departments, on-demand Operations Cell, selected web/tools, scoped revisions, time accounting, and human release approval | Paper is the flagship; non-paper artifacts must exercise the same runtime. No new experiments or substantive analysis of raw research data |
| v1.5 | Broader deliverables and custom Score composition, reusing department contracts | Generality beyond the implemented bounded revision contract must be demonstrated on additional missions |
| v2 | Explicitly authorized scientific analysis/experimental execution beyond supplied results | Bounded local-program execution, deterministic replay, result provenance, and independent calculation/review are implemented; broader remote experiments and expert validation remain open |
| v3 | Reusable organizational experience and external-agent collaboration | Process improvements are versioned, evaluated, and approved; no self-amendment of the Principal's intent |

An organic organization does not imply continuously running all agents. Persistent responsibilities, a durable backlog, delegated initiative, and reliable feedback matter more than simulating employee chatter.

## 3. Decision register

### 3.1 Historical decisions D1–D12 — preserved verbatim

The following records describe the original design. Where they conflict with a later decision, the later decision's explicitly named scope prevails.

- **D1 — Multi-department organization metaphor.** The system is an organization. Standing departments: Research & Intelligence / Strategy & Writing / Editorial Office. Standing cross-cutting organ: the Archivist (version control & storage). Standing conductor: the Composer.
- **D2 — Departments are general-purpose functional units.** The Research department does not do "paper research"; it does *"survey of the world for a given goal"*. Papers are just one mission among many. Same for Strategy and Editorial.
- **D3 — Free exchange between departments + full recording.** Any department may at any time send opinions, data, requests, or objections to any other (asynchronous). However, **every exchange is recorded as a message envelope**, and shared data moves only through the blackboard (project workspace). Free, yet nothing untraceable.
- **D4 — Departments are internally multi-agent.** Each department = one chief + multiple specialist agents. Chiefs decompose work and judge quality; specialists execute.
- **D5 — Rigorous per-project version control.** Project = one repository. Every artifact is stored as an **immutable version + provenance (who, when, based on what)** and tagged at milestones. Overwriting does not exist — only new versions.
- **D6 — The Composer defines purpose via swappable "scores".** A score = a declarative definition of pipeline, quality gates, and department invocation order per deliverable type. The default score is `paper` (academic paper); user-defined scores can change the purpose freely.
- **D7 — Initial pipeline (user request).** Input: research data + rough storyline. Processing: prior-work survey → verified result interpretation → research-question and competing-hypothesis map → discriminating-test and figure plan → outline → draft → editing. Output: draft + research archive + argument map + decision record.
- **D8 — Long-term: resource-aware organization.** Research/Strategy departments become aware of the execution environment (local computer, available tools, open-source ecosystem) and can discover, combine, and run tools to achieve goals (opt-in capabilities, behind gates).
- **D9 — Hybrid execution harness.** The core (store, ledger, message bus, score, gates) is implemented natively as a standalone Python orchestrator; department/agent **runners sit behind an adapter interface**, so LangGraph, AutoGen, etc. can be mixed **per department** where useful (frameworks are adopted piecemeal, not all-or-nothing).
- **D10 — Deliverables down to rendered output.** The paper score's final deliverable is LaTeX source + compiled PDF. The Editorial Format Editor maintains venue templates (.cls/.sty) and citation styles; **successful compilation and zero reference errors** are release-gate conditions.
- **D11 — Paper language policy.** The final paper (writing · rendering) is **English by default**. Briefs, internal collaboration notes, and release summaries may be Korean. Language is declared in the score; overrides (e.g., Korean paper with a kotex compile chain) are allowed.
- **D12 — English-first development (supersedes the "internal docs may be Korean" clause of D11).** All development artifacts are unified in **English**: design documents, source code, comments, commit messages, CLI output, schema field names, identifiers, and inter-agent message bodies (default; score-configurable). Human-supplied inputs (briefs) may remain in any language.

### 3.2 New decisions incorporated in this revision

**Decision status:** D13–D15 and D22–D24 retain the established direction. D25 records the compute and supervision premise; D31–D36 specify surgical revision, structured management, active external intelligence, project instances, and practical operations. D37–D38 establish mission-independent execution and explicit result/time contracts. Engineering mechanisms do not establish measured efficacy or independently authorize runtime operations. Later decisions supersede only the explicitly named scope.

**D13 — Adversarial progress by default.** Every department has a designated independent Adversarial Reviewer. Its mandate is to find substantive, justified reasons an output should not advance. Its output contains objections, investigation requests, or an explicit no-valid-objection-found result—not praise or ceremonial approval. It must not invent flaws to meet a negativity quota. Producers may rebut. An adjudicator other than the producer or reviewer decides disputed validity; repairs require verification. No substantive departmental deliverable advances without recorded adversarial coverage. This extends D4.

**D14 — Principal-aligned command.** The organization exists to realize the Principal's intent. Preserve the original human instruction separately from the command's interpretation. Only the Principal can approve changes to objectives, prohibitions, priority ordering, or delegated authority. Command translates intent into missions, allocates resources, resolves trade-offs, and reports candidly. Alignment governs direction and choices, not factual conclusions. No role may conceal contradictory evidence or manufacture agreement with a desired conclusion.

**D15 — Four departments and an Executive Command.** Add **Methods & Validation** as a department independent of Strategy & Writing. Replace the standalone Composer's combined responsibilities with **Executive Command**: Intent Keeper, Composer, Arbiter, and Progress Controller. The Composer remains the orchestration role within command. Archivist remains a cross-cutting deterministic service, not a creative agent. This supersedes the membership list in D1 and the Composer's concentration of authority; it preserves the organization metaphor.

**D16 — Delegated initiative within a Score.** A Score specifies mission constraints, deliverables, stage readiness, capability limits, quality gates, and reactive policies. It is not a fixed conveyor belt. Departments may propose or execute in-scope tasks within delegated budgets, request another department's help, and challenge upstream assumptions. Events can reopen affected work without restarting unrelated work. Requests do not authorize themselves. This extends D3 and supersedes an exclusively sequential reading of D6–D7.

**D17 — Storyline before prose; evidence before commitment.** A paper begins with a versioned argument contract: thesis, ordered storyline beats, and the intended role of each claim. Results and literature remain distinct, and every committed claim links to specific evidence spans, procedures, findings, metrics, or limitations as support, qualification, or context. Identifiers and bibliographic metadata establish source identity, not support for a claim. Contradictory evidence must qualify, reject, or create a new version of the affected storyline beat; it cannot be omitted merely to preserve narrative coherence. This captures the argument-led form of human papers while keeping acceptance evidence-bound. D46 separately authorizes new experiments under a frozen Score.

**D18 — Verified progress and incumbent preservation.** Progress is a documented improvement against a fixed, intent-aligned acceptance contract, or separately recorded decision-relevant information gain. Fewer open issues alone is not enough. Changes must preserve required coverage, pass non-regression checks, and survive independent verification. Keep the best currently admissible artifact as an incumbent; a new version is only a candidate until accepted. New evidence can invalidate an incumbent. There is no unconditional monotonic-quality guarantee.

**D19 — Durable control plane; immutable artifacts.** Use a native Python orchestrator with a transactional local state/event/message store as the v1 default. SQLite is the proposed local implementation; JSONL is an audit export, not a multi-writer queue. Immutable content objects and version manifests are the artifact authority. Git is the inspectable snapshot/release history, not a second live transaction coordinator. This clarifies D5 and D9; it supersedes the original JSONL-queue implementation default.

**D20 — Bounded authority and independent judgment.** Deterministic checks, evidence-based judgments, and human approvals are separate gate classes. Reviewers cannot unilaterally rewrite artifacts or block work indefinitely; chiefs cannot erase critiques or grant themselves final disputed approval. Scientific-integrity failures cannot be waived into a verified final release. The Principal may redirect, pause, or accept a visibly incomplete draft, but recorded facts and verdict histories remain unchanged.

**D21 — Minimum organization before organizational scale.** Agents are role specifications executed by a bounded worker pool, not a required process per role. Establish one complete produce–challenge–respond–judge–verify–select loop and measure its value before expanding concurrency, frameworks, or the number of agents. No new framework or department is justified solely by the organization metaphor.

**D22 — Organization-wide Web Intelligence Fabric.** Web retrieval is a shared organizational capability, not a Research-only tool. Any department may perform authorized, task-scoped discovery when external information can materially improve its work. Research & Intelligence owns broad search strategy, source acquisition quality, provenance, catalog normalization, and promotion of discoveries into the formal evidence base. A web discovery is not automatically evidence; publication into the project evidence registry requires source capture, provenance, and the appropriate verification.

**D23 — Independent adversarial retrieval.** Each departmental Adversarial Reviewer receives a separately reserved retrieval allowance and may construct independent query families, terminology, source routes, and counterexample searches. An adversary must not be restricted to the producer's search history or selected corpus when the review criterion requires external verification. Independence is bounded by the same data, provider, cost, and capability policy as the rest of the mission.

**D24 — Coverage-oriented search, not result-count search.** Search is an iterative evidence-acquisition process. For material questions, the organization may decompose concepts, expand historical/adjacent terminology, search multiple source classes and languages, chase backward/forward references where available, follow authors/labs/projects, and explicitly seek contradictory evidence. Search stops because the approved coverage target is met, additional search has low expected decision value, access is exhausted, or the resource envelope is reached—not because an arbitrary number of results was collected. Search coverage and known gaps are first-class records.

**D25 — Compute-rich, time-accountable reasoning.** Plentiful external GPU capacity is the default operating premise. Reasoning should be generous, flexible, and rationally supervised. Prefer useful progress over elapsed time to token thrift or activity volume. No fixed number of model calls defines sufficient thought. This changes the allocation emphasis of D21 while retaining a minimal implementation and measured justification for organizational complexity.

**D26 — Activity graph as the execution center.** Departments are responsibility and knowledge boundaries; work is a changing graph of questions, candidates, evidence, checks, and decisions. Cross-department groups may form and dissolve within delegation. A Score defines commitment conditions and permitted activity, not a fixed reasoning sequence. Provisional exploration may precede upstream acceptance, but substantive promotion requires the full current dependency closure. This extends D16 and clarifies D6–D7 and D13.

**D27 — Renewable capacity, finite execution leases.** Separate authorized resources, measured capacity, and finite task/planning leases. Existing delegation may automatically renew an allocation window; no human reconfirmation is required solely because a window ends. Metered expenditure and explicit mission limits remain hard boundaries. Missing policy is not unlimited permission. This replaces universal lifetime call/round caps and fixed review/message-count defaults while preserving D19's durable accounting and D20's authority boundaries.

**D28 — Rational supervision with independent recourse.** Every activity has a mission contribution, expected useful output, and evaluation or reassessment condition. Executive Command supervises the portfolio and its bottlenecks. Routine delegated work uses deterministic admission; consequential decisions and failed predictions receive independent scrutiny. The supervisor cannot change acceptance criteria to improve its own scores. Disputes have finite escalation paths, not an infinite hierarchy of reviewers. This extends D15 and D20.

**D29 — Anytime results and durable learning.** At configured wall-clock checkpoints, expose the best admissible result, material alternatives, independently supported artifact/information changes, resources, gaps, and next action. Long investigations may span checkpoints with declared intermediate milestones. Deduplicate discoveries and retain stagnation history across renewals. New evidence can invalidate accepted work; never promise monotonic scientific improvement. This extends D18.

**D30 — Adaptation before organizational scale.** The first vertical slice must already demonstrate parallel alternatives, supervised reallocation, renewable capacity, verification, and elapsed-time checkpoints. Add the complete department catalog after this loop works. Reserve review and integration capacity as production scales. Internal plan and outline revisions within approved scope need no new Principal checkpoint unless the mission explicitly requires one; final release and material scope/authority changes retain human control. This supersedes D21's narrow serial-slice reading and the previous mandatory outline-approval default.

**D31 — Purpose-directed surgical revision.** Every edit names a defect, requirement, or justified improvement; the exact baseline; permitted units/fields/operations; preserved properties; and verification conditions. A worker may read broad context while holding narrow edit rights. Ownership, integration responsibility, or an instruction to improve quality does not grant whole-document rewrite authority. Scope expands only through a recorded, non-conflicted decision within delegation or the Principal's authority where needed. This strengthens D16, D18, and D20.

**D32 — Structured, versioned deliverables and references.** Manage sections, paragraphs, and other meaningful blocks by stable identity and immutable versions, composed through a DocumentManifest. Keep order/containment, unit purpose, claim links, citation occurrences, captured reference versions, and rendering dependencies explicit. Moving content preserves identity; split/merge/retirement preserves lineage. Derived files are not an alternate mutable source of truth. This specializes D5 and D19.

**D33 — Scoped proposals; verified atomic integration.** Workers submit ChangeSets under service-enforced EditGrants. The control plane checks the complete mutation set, including structure and shared assets, independently verifies semantic impact, and atomically adopts a current manifest. Concurrent textual separation is not proof of semantic independence. Coupled changes commit together; scoped inverse changes preserve unrelated later work. A reviewer or integrating agent cannot bypass these rules. This extends D13, D18, and D20 without requiring human permission for every routine correction.

**D34 — Active external intelligence and tool integration.** All departments and adversaries actively seek external information when it can change a material decision. Search, original-source acquisition, citation/reference following, repository/documentation inspection, and independent counter-search are executable capabilities backed by usable API/tool adapters. Discover missing capabilities and implement/configure useful integrations early; record actual availability, credentials/rights, and measured failures. Already authorized tools may be selected autonomously. Plugin discovery or generated adapter code does not itself grant installation, credentials, or new data permissions. External findings enter evidence verification and scoped change control rather than rewriting accepted content. This operationalizes D22–D24.

**D35 — One operating organization per research project.** Each project instantiates its own intent, missions, Command, departmental context, task graph, environment, capabilities, evidence, and artifact history. The Sci-saurus implementation repository and reusable organization templates are distinct from these research-project instances. Shared inference/services/public software caches do not imply shared private data, credentials, write authority, or accepted results. Artifact resolution and every execution/grant are bound to authenticated project identity. This operationalizes D5 and the project-local scope of D14–D20.

**D36 — On-demand Operations Cell with actual execution responsibility.** Activate a practical support cell when a project needs program setup, API/MCP connection, adapter implementation, execution, repair, or packaging. It must carry useful open-source programs and services through a real project-runtime run and inspected outputs, not stop at recommendations, installation logs, or schema listing. Reuse existing workers/tasks; enabled tools remain directly usable by departments. The cell prepares and operates the environment under existing project delegation, while independent operational checks and qualified departmental review decide acceptance. This brings practical setup/execution into v1, superseding D8's deferral for that scope; substantive new analysis/experiments remain a separate mission capability.

**D37 — General-purpose core; mission-specific Scores.** Project organization, immutable artifacts, scoped proposals, review, operational readiness, and resource accounting are shared across deliverables. Paper production is the flagship application. A versioned Score declares the domain, output units/files, additional acceptance checks, selected capabilities, and workloads; an operational guide or configuration must not inherit academic retrieval, scientific terminology, or manuscript output names implicitly. Real programs may validate exact candidate artifacts through the same task and evidence services. This strengthens D2 and D6, broadens D35's research-project terminology to all authorized project instances, and supersedes deferring the first non-paper execution proof to v1.5/P4. It does not authorize arbitrary tool execution or imply that every Score design feature is implemented.

**D38 — Explicit result targets and protected verification time.** A mission can define a target for its first newly verified result, a completion target, and a hard elapsed-time cap. Initial stage durations are declared planning assumptions; completed work updates their estimates with observed durations and visible provenance. Admission reserves time and capacity for independent unit and integrated review. Reaching a completion target stops discretionary production/reassessment while already required verification remains subject to the hard cap. An infeasible initial hard cap blocks external work. Missing a target remains visible; neither an inherited baseline nor a successful program exit establishes a new verified result. This extends D25, D27, D29, and D30 without promising completion by a guessed duration or lowering acceptance requirements.

**D39 — Literature assessment precedes paper contribution selection.** A paper mission first establishes a current, versioned prior-work survey with abstract screening, citation expansion, supported research lineages, comparable results, and unresolved questions. The bounded [Literature Survey Score](75-literature-survey-score.md) implements OpenAlex discovery, optional Crossref DOI/title/year reconciliation, configured MCP full-text capture, immutable per-work mapping, mandatory focused claim and relationship reviews with scoped repair, independent survey review, and an independently challenged gap assessment through the shared execution services. Every new survey claim binds an immutable source version, exact character span, and quote hash; ambiguous repeated quotations are rejected. Gap nomination, targeted challenge, and assessment require a current accepted survey at dispatch; survey and assessment commitment recheck exact evidence, governing versions, and time/capability authority. Targeted challenge and assessment also bind the exact nomination; a candidate revision invalidates its prior verdict independently of survey currentness. Decisive comparisons require verified full-text evidence from the compared work. Failure to find a solution is not proof of novelty; insufficient access, identity, or coverage requires abstention. The implemented search uses finite query and citation batches and explicitly configured identity/full-text routes. Local workflow tests establish those boundaries; held-out expert-level accuracy and complete live scientific missions remain separate requirements. Recovery and release-candidate mechanics are specified by D40 and D44. Non-paper Scores remain unaffected.

**D40 — Recovery preserves evidence and reopens named interpretation scopes.** A restart compares the exact stored run configuration and source manifest, reconciles each unknown external outcome under an explicit conservative charging policy, and keeps the original cumulative ledger. Captured external effects remain immutable and are not repeated merely because a later mapping, review, or assembly scope changed. The continuation receives a finite additional elapsed window; dependency-aware replanning retains required closure and defers optional work when the whole plan no longer fits. This implements recovery mechanics left open by D39 without asserting that every historical run can complete successfully after provider failure.

**D41 — General plans are versioned dependency graphs.** A mission may publish an acyclic project plan whose tasks declare owner, objective, exact inputs, capability requirements, outputs, dependencies, and time estimate. A revised plan reuses a result only when the complete task contract and dependency-result references remain identical. Execution publishes only the declared outputs, requires an independent verification artifact, and preserves the human release boundary. Mission-specific handlers perform domain work; the shared scheduler does not infer hidden side effects from prose.

**D42 — Tool discovery is separate from tool trust.** Operations may query the official MCP Registry and use other approved catalogs to locate candidates. A project can activate only one explicitly allowlisted recipe compatible with the task adapter, tags, and data classification. Registry presence or installation success does not create a capability binding. Local programs, APIs, and MCP services must still pass a representative execution and independent output, protocol, identity, and accounting verification. Python package provisioning accepts local wheels with pinned hashes and no dependency resolution during installation.

**D43 — Journal-readiness is a separate desk decision.** Factual, methodological, and prose reviewers do not establish conventional scholarly depth. A deterministic Journal Editor applies the project's named publication profile to the candidate's observed reference count, full-text support, in-text citation distribution, figures, and tables. A score-3 research-paper descriptor with no explicit profile resolves to the empirical-journal floor and cannot select the shorter validation floor; a validation report resolves to that shorter floor. A missed floor requests scoped literature or experiment work and leaves the candidate visibly incomplete; padding references, duplicating displays, or lowering the profile after review is not a repair. A Composer run propagates this candidate state instead of reporting an accepted completion.

**D43a — Scientific judgment claims require frozen external labels.** The evaluation runner removes labels and adjudication rationales from inference packets, pins submissions to the exact frozen corpus hash, and reports task-level accuracy, coverage, and decisive false positives on insufficient-evidence cases. Development labels can diagnose behavior but cannot clear a release gate. A held-out release verdict requires expert-adjudicated labels; the repository does not claim expert-level accuracy until such a corpus and results exist.

**D44 — The paper release candidate is a dependency closure.** A paper build requires a current accepted survey and assessment, an independently accepted structured manuscript, a validated supplied-results package, exact paragraph-level claim/evidence bindings, and citations pinned to accepted survey sources. Literature claims from v3 surveys retain exact source spans and quote hashes in the claim index. DOI references require a verified reconciled identity whose source work, DOI, title, and year match the bibliography entry. The builder emits English LaTeX, a bibliography, claim index, rendered PDF, visual-render report, and exact release manifest. A research-paper candidate requires an experiment-eligible accepted gap assessment; a replication or methods-validation outcome may instead use a `validation_report` that preserves the actual gap state and avoids a novelty claim. Compilation and deterministic checks produce a candidate only. Final Principal approval and external submission remain separate.

**D45 — Visual judgment uses the same evidence and authority discipline.** An academic figure, rendered page, or aesthetic concept comparison is captured as immutable image bytes with media type, dimensions, role, and SHA-256. Independent visual perspectives inspect the same pinned images against a versioned Score. Their per-criterion outcomes are copied exactly into synthesis, and an independent verifier reopens the images before assessment acceptance. Every corrective action names its issue, asset, purpose, smallest sufficient allowed change, protected elements, and rerender condition. A reference, source, and rendered candidate remain distinct roles. Visual polish cannot establish scientific truth, and visual review cannot authorize content changes outside its explicit scope.

**D46 — New results require frozen execution, replay, and distinct validation.** An authorized experiment Score fixes the question, hypothesis, method, parameters, seed, run count, stopping rule, primary outcomes, limitations, assets, and deadline before execution. Novel-research execution requires a current experiment-eligible literature assessment; other declared study types preserve their actual survey state without converting it into novelty. A pinned execution program runs twice and must reproduce the complete structured output and asset hashes. A distinct pinned program binds that exact output and recalculates every primary outcome. Independent model roles may inspect method, claim scope, and figures, but same-model agreement is procedural evidence rather than independent scientific ground truth. Only the exact replay, passed calculations, reviews, and final assessment can form an adopted `results-package-2`; negative and mixed results remain in the package. Publication still requires the paper and Principal release boundaries.

**D47 — A paper starts from a frozen storyline contract.** The paper Score fixes an ordered thesis and storyline beats before prose production. Every accepted paper claim names the beat it performs, and every beat maps to at least one exact manuscript proposition and evidence-bound claim. Evidence is attached as support, qualification, or context; context alone cannot justify a claim. This reflects the argument-led structure of human papers without authorizing motivated fabrication: conflicting evidence must qualify, reject, or version the affected beat rather than being omitted or forced into support. Paragraph writers receive the frozen storyline as governing context and retain unit-level edit scopes. The claim index records the storyline version together with evidence relations so a polished narrative cannot conceal a changed evidentiary basis.

**D48 — Exploration may branch, commitment may not.** An open-ended research mission can retain a bounded hypothesis tree with explicit parentage, stage, seed, metrics, and evidence. Promotion requires independent verification and preserves materially different alternatives for later inspection. A tree-search proposal never changes an accepted artifact, bypasses the literature gate, or grants model-generated code unrestricted execution. The reusable implementation and benchmark comparison are documented in `95-ai-scientist-benchmark.md`.

**D49 — Interpretation is a scientific stage, not a writing side effect.** After evidence and deterministic results are assembled, a separate interpretation artifact records the important result patterns, their practical meaning, competing mechanisms, evidence for and against each mechanism, and experiments that would distinguish them. Possible explanations remain explicitly possible until evidence supports or refutes them. A bounded conclusion is projected from this artifact into the manuscript; a writer cannot jump directly from a number to a conclusion by omitting the explanatory step.

**D50 — Control-plane state is projected into public scientific language.** Hashes, reservations, acceptance states, repair scopes, model-call accounting, and internal gap enums remain complete in the ledger and appendices. The manuscript surface is screened for operational vocabulary and must express the corresponding scientific meaning in terms a human researcher would publish. This projection is explicit and reviewable; it is not a silent text scrub.

**D51 — Editorial compression is a release gate.** The assembly pass separates observation from interpretation, prevents repeated numerical facts and duplicated caveats, and prioritizes limitations by their effect on the conclusion. A limitation that does not change how the conclusion should be read is omitted from the main text or moved to a reproducibility record. Compression reduces reader burden without weakening evidence, provenance, or uncertainty reporting.

**D52 — Human-scientist review is independent of factual QA.** In addition to method and accuracy checks, every manuscript release receives an adversarial scientific-communication review of the question, narrative importance, mechanism discussion, explanatory value, exposed pipeline language, repetition, figure argument, and section function. The reviewer may request a scoped repair or state that no justified objection was found; it may not invent objections to satisfy a negativity quota.

**D53 — Provider pacing is part of the execution contract.** A provider has an explicit inter-request interval, request-level transient retry policy, and total request deadline. Pacing is scheduled independently for bibliography, identity, and full-text capabilities; `Retry-After` and exponential backoff are honored inside the same deadline. Waits, retries, and exhausted budgets remain visible in the run report, and a delay that cannot fit the hard wall blocks before dispatch rather than creating an unbounded timeout.

**D54 — Composer feedback is a first-class organizational exchange.** Every
stage handoff produces a linked command decision note and message-bus envelope
addressed to the responsible department chief. A successful handoff advances
the dependency graph; an unresolved or failed handoff routes a scoped critique
to the Arbiter and Progress Controller. The message body records the stage,
role, scientific state, dependency set, elapsed/deadline budget, output
reference, and next condition. Blocker reconciliation may reopen only the
affected scope and must preserve the incumbent; a department cannot silently
acknowledge its own output as an independent review. This is the Composer's
control loop for human-like organizational collaboration, while scientific
acceptance remains with the stage's independent checks and the Principal's
release boundary.

Internal paper reviews, synthesis decisions, surgical repairs, and release
events use that same exchange rather than a private sub-pipeline. Stable event
IDs make a resumed checkpoint idempotent; review bodies and replacement text
stay in the manuscript project's immutable unit history, while the Composer
routes only bounded control summaries.

**D55 — Evidence-aware adjudication.** Every review role receives a bounded
projection of the frozen evidence needed to test the target claim. Before an
Arbiter can authorize a surgical repair, deterministic guards compare proposed
numeric corrections with the target text and evidence registry. A novel
numeric value without an evidence or calculation artifact is preserved as an
explicit unresolved alternative and rejected as a repair directive; it cannot
replace a supported value by model assertion. The project may reopen the
finding after a qualified fact-verification artifact is promoted. This
preserves rational oversight while keeping the full review and dispute history
immutable.
When material review findings conflict, a dedicated Arbiter reconciliation is
recorded before synthesis; only retained finding IDs may become repair
instructions, while rejected alternatives remain visible and cannot be silently
converted into acceptance.

**D56 — Research admission precedes manuscript composition.** A declared
research-paper profile must clear its literature, full-text, figure, and table
floors before a writer is admitted. A missed floor creates a versioned
`research_expansion_required` request with an owning department and a concrete
success condition; it creates no draft, manuscript project, or PDF. A
validation report may use its separately declared shorter profile, but a thin
proposal or plot package cannot be released by relabeling it.

**D57 — Peer review can demand new science.** Reviewer contracts distinguish
surgical manuscript findings from first-class requests for an additional
experiment, literature expansion, interpretation expansion, or analysis repair.
The editor must carry every unresolved request into the decision; prose edits
cannot discharge a missing-evidence request. The same reviewer panel receives
the revised incumbent, and a research-paper release requires three bounded
rounds followed by an editor-in-chief decision. Unresolved material findings
produce `review_rejected`, never a review-limit release candidate.

**D58 — AI-surface review is adaptive, not a vocabulary filter.** The
AI-adversarial reviewer infers machine-like failure modes from the complete
argument and human scholarly norms, then records only location-specific,
reader-impacting objections with a minimal repair or research request. Fixed
phrase lists and authorship accusations are not acceptance criteria; the
deterministic control-vocabulary check remains limited to preventing internal
provenance state from leaking into the public manuscript.

**D59 — Composer retries are isolated and deadline-governed.** A workflow may
declare a `retry_policy` with a backoff. Its `mode: until_deadline` setting
keeps retrying a failed stage in fresh attempt directories while the same hard
deadline remains in force; it has no arbitrary attempt-count stop. When the
full downstream forecast no longer fits, this autonomous mode may admit the
next stage into the residual window until a small control-plane margin remains;
the specialist's own deadline and acceptance contract still decide whether a
useful result is produced, and an unfinished closure is never reported as a
release. This deadline-governed mode is the default for legacy workflows. The
`forward_first` mission policy clamps retries to at most two attempts, carries
actionable failure debt as a release-blocking provisional artifact, and lets
the agenda return later through a bounded work order. A small deterministic job
may opt into `mode: bounded` with a maximum of one to eight attempts; bounded
jobs retain the full-closure reservation and pause when it cannot fit. Every
attempt, error, retry decision, provisional artifact, and residual-window
admission is persisted, and a ten-hour mission is an explicit
`hard_seconds: 36000` policy. The hard wall, provider outcome, or a genuine
control-plane failure remains the termination condition.


**D60 — Free-topic intake is sampled, recent, and capability-aware.** When a
mission does not supply a research object, the Composer first queries the
configured scholarly index (OpenAlex by default) with objective-derived and
broad recent-literature searches. It filters to a four-year recent window,
falling back to the newest returned records only when that window is empty, and
selects a reproducible seeded random sample, preserving query, work, URL,
abstract, DOI observation, response capture hash, and sampling seed. A bounded
intake model proposes distinct questions from that sample and a redacted
runtime capability inventory, including available programs, Python packages,
configured stage kinds, and project inputs. It may select only a question that
can be tested with declared project resources. The selected candidate carries
structured executable, Python-package, and stage-kind requirements; an
unavailable requirement blocks that candidate rather than being hidden inside
a feasibility paragraph. It may not turn metadata into a novelty or evidence
claim. Provider failure blocks intake rather than fabricating a topic.

**D61 — Research requests re-enter the Composer through deadline-governed continuation.**
A structured literature, full-text, experiment, interpretation, analysis, or
manuscript request is an executable work order. After the graph reaches its
current terminal state, Composer admits only the affected stage closure until
the same hard deadline by default. A small deterministic job may opt into
`continuation_policy.mode: bounded` with `max_cycles` from zero through eight.
Each cycle receives a fresh project namespace; survey capacity and routes are
expanded when needed, newly accepted sources are synchronized into the paper
reference set, and every downstream consumer is rerun. Prior attempts and
artifacts remain immutable. The hard deadline remains the termination condition,
so automatic research cannot become an infinite loop. An unchanged work order
echoed after its owning stage has been attempted is fenced for that Composer
invocation and leaves the hold visible; an explicit resume creates a fresh
attempt and can retry the unresolved order.
An unresolved research or review hold never satisfies a dependency while the
current graph is being scheduled: the owning closure is reopened before any
consumer can read that hold's packet, or the workflow returns the hold when no
authorized continuation remains.

In `forward_first`, a mechanical or scientific blocker is allowed to satisfy
the scheduling dependency only as a provisional, release-blocking candidate;
the exact failure debt is converted into a typed backfill order after the
first graph pass. The downstream stages may inspect that candidate and record
their own limitations, but cannot release it as accepted evidence. Resource
fences and unknown external outcomes remain hard stops rather than provisional
science.

**D62 — Templates seed a live project organization; they do not prescribe its work.**
Each Composer project materializes a versioned department charter, durable
department inbox, and typed work-order backlog. The default template supplies
safe responsibility and capability scopes, while observed stage results,
evidence gaps, review objections, and capability state generate the next
admitted work. A valid request is activated and resolved through the owning
stage closure; malformed or unauthorized requests become explicit rejection
artifacts. Replayed requests are idempotent, changed objectives create new
immutable work-order generations, and all department activity is included in
checkpoints and run reports. This realizes organic operation without granting
any department unrestricted artifact overwrite, host execution, or authority to
change the Principal's intent.

**D63 — Free-topic exploration remembers attempted directions.** A Composer
family maintains an append-only topic history across fresh project directories.
The intake receives prior selected questions and capability usage, rotates the
most recently used executable capability when an alternative is available, and
rejects exact or near-identical selected directions before literature work is
admitted. This memory controls repeated execution only; it never substitutes
for a scholarly novelty claim or the survey and review gates.

**D64 — A selected topic is provisional until it survives maturation and
evidence feedback.** Journal-oriented topic→survey missions independently
score the question's specificity, explanatory depth, comparison design,
contribution potential, and falsifiability before admitting it to the survey.
A weak direction is regenerated with a substantive change. If the first survey
is insufficient, the survey receives one scoped evidence-expansion pass before
the question is redesigned; if the expanded evidence still cannot establish
an experiment-worthy distinction, or prior work already answers the question,
the Composer holds the downstream experiment, issues a typed topic-refinement
work order, and reopens the topic→survey closure with the parent question and
assessment attached. Every refinement records its lineage and changed
scientific dimension; a cosmetic rewrite cannot satisfy the loop.

**D65 — Scientific sufficiency is red-teamed before composition.** After the
research argument and deterministic admission floors pass, a paper must receive
independent pre-composition reviews from methods, mechanism, and journal-editor
perspectives. The fixed checks cover the question, evidence, result coverage,
controls, alternatives, reproducibility, and argument. A missing experiment,
analysis, literature basis, or defensible interpretation becomes a typed
research work order and blocks the writer; prose cannot discharge it. The
red-team package, per-review artifacts, model usage, deadline, and aggregate
decision are immutable inputs to the later manuscript review. This gate is a
scientific sufficiency check, not a positivity or disagreement quota.

**D66 — Functional roles and concrete appointments are separate.** The
project organization keeps one stage-route table for functional ownership and
projects each validated charter into concrete chief and independent-adversary
appointments. A custom chief changes routing identity without changing stage
semantics; the producing appointment and adversary must be distinct. Composer
tasks, handoffs, checkpoints, and organization manifests expose the role,
department, owner, adversary, and executive command addresses separately.

**D67 — Specialist pools are bounded, role-isolated, and independently checked.**
The v2 project organization publishes an eligible specialist pool but activates
only the roles admitted for the current stage and quota. Each activation has a
separate task, bounded input projection, role contract, artifact namespace, and
call/token/time reservation. The stage records its required and active agents,
then a chief synthesis and an adversarial verdict authored by a different
appointment. Specialist failure or unknown external outcome remains scoped to
that assignment and is never collapsed into a false whole-workflow success.

### 3.3 Effective interpretation of earlier rules

| Earlier wording | Effective interpretation |
|---|---|
| Composer writes only releases | Command may author intent interpretations, missions, tasks, decisions, and release proposals in its own namespaces; it may not rewrite departmental knowledge |
| Every edit is a new artifact version | Immutable content and metadata are preserved; status, approval, and adoption are append-only events, not in-place edits |
| Every exchange is recorded | Structured requests, outputs, decisions, and concise evidence-based rationales are recorded; hidden model reasoning and secrets are not required |
| Any department may read the blackboard | Access remains permissioned; each model call receives a bounded ContextPackage, not the whole project |
| A gate failure routes to arbitration | Mechanical errors first follow bounded repair; factual uncertainty may remain blocked; authority conflicts and contested judgments escalate |
| Final paper is English | Development artifacts and inter-agent communication default to English under D12; original human/source material stays in its original language |
| Finite compute budget | Finite dispatch reservations and renewable capacity windows; hard lifetime limits only where explicitly configured |
| Stage prerequisites | Required for accepted commitment; separately authorized provisional exploration may start earlier |
| Every output needs review | Substantive accepted outputs require exact review coverage; exploratory proposals do not each trigger a full approval cycle |
| A department owns an artifact namespace | Ownership permits responsibility and scoped proposals; it does not confer unrestricted whole-document editing |
| Every draft is an immutable artifact | Accepted document versions compose exact structural-unit versions and pinned references; generated files are derived views |
| Resource-aware tool execution is long-term | Practical project-local OSS/API/MCP setup and use are v1; new scientific experiments/estimation remain separately scoped |
| Project means research project; other deliverables follow later | The core serves all authorized projects. Paper is the flagship, and the first non-paper Score proof belongs to the initial shared runtime |
| More time or an existing baseline implies progress | First-result and completion targets are explicit; only a newly independently accepted artifact fulfills the first verified-result target |

## 4. Organization and authority

| Organ | Mission | Core roles | Independent challenge |
|---|---|---|---|
| **Principal** | Defines desired outcomes, prohibitions, trade-offs, and delegation | Human owner | Can question, redirect, pause, or terminate any mission |
| **Executive Command** | Preserves intent and makes the organization act toward it | Intent Keeper; Composer; Arbiter; Progress Controller | Any departmental adversary may challenge its decisions; conflicted arbitration goes to another qualified adjudicator or the Principal |
| **Research & Intelligence** | Establishes what the outside world supports and disputes; owns formal evidence acquisition and registry quality | Chief; Search Strategist; Academic Scout; Open-Web Scout; Technical Ecosystem Scout; Standards & Patent Scout; Genealogy & Trend Analyst; Source Acquirer; Cataloger; Fact Verifier | Research Adversary |
| **Strategy & Writing** | Develops the strongest useful argument and deliverable allowed by the evidence | Chief; Narrative Architect; Planner; Section Writers | Strategy Adversary; absorbs the original Feasibility Red Team's argumentative role |
| **Methods & Validation** | Assesses whether the proposed conclusions follow from the supplied methods/results | Chief; Methodologist; Statistical Reviewer; Reproducibility Reviewer | Methods Adversary |
| **Editorial Office** | Makes the deliverable accurate, coherent, and appropriately rendered without altering its meaning | Editor-in-Chief; Structural Editor; Format Editor; Consistency QA | Editorial Adversary reviews the editorial work, not only the manuscript |
| **Operations Cell — on demand** | Makes the project's required programs, environments, APIs, and MCP services actually work | Coordinator; Tool/Environment Engineer; Execution Operator, activated as needed | Independent operational verifier and the qualified consuming department; no scientific acceptance authority |
| **Archivist** | Preserves objects, provenance, decisions, events, and releases | Storage, audit, and recovery services | Deterministic integrity checks and sampled audit; no recursive LLM bureaucracy |

A chief owns coordination and synthesis. The department's adversary has an independently reserved review budget and direct access to the escalation channel; a chief cannot suppress an inconvenient review. Distinct roles may use the same model, but this provides procedural separation, not statistically independent reasoning.

## 5. Principal Intent, mission, and delegation

**Principal Intent** is a versioned policy: verbatim instruction references; approved interpretation; objectives; ordered priorities; hard constraints; non-goals; resource/data boundaries; and the scope of autonomous action. Do not fabricate numeric utility weights from conversational wording.

**Mission** is a project-specific implementation of an approved intent revision. Every Task, gate result, and release pins the applicable intent, mission, and Score versions. An inferred preference is marked `assumed`, not `approved`.

The Principal decides ends and material trade-offs. Departments decide specialist matters within their authority. Command can choose between scientifically permissible routes; it cannot vote a false proposition into truth. An unresolved scientific question produces uncertainty, a scoped claim, an investigation request, or a pause—not an invented answer.

A new principal instruction creates an intent revision with a recorded interpretation delta. An explicit instruction can itself authorize a faithful delta; ask only for unresolved material interpretation or additional authority. Impact analysis marks affected tasks/approvals stale. Prior approvals do not silently transfer to changed content.

## 6. Adversarial working contract

A valid objection names the target version and location, the violated requirement or precise claim, evidence or a reproducible contradiction, material impact, and a resolution/verification condition. Logical defects may be grounded in an explicit counterexample rather than an external citation. Style criticism must cite an actual communication requirement and demonstrate the problem, not a personal preference.

Review results are `objections_found`, `no_valid_objection_found`, `insufficient_evidence`, or `review_failed`. The last three have different meanings. A lack of access or a failed tool call cannot count as successful review.

The common cycle is:

```text
produce candidate → blind challenge → respond/rebut/request evidence
                  → adjudicate validity → revise if needed
                  → verify resolution + test regressions → select/retain/pause
```

The first challenge sees the relevant mandate, evidence, rubric, and target artifact, not the producer's conversational history. It must still receive necessary technical context. Later rebuttal is visible to the adjudicator. High-severity disputed validity or closure requires another qualified role or the Principal when no credible independent check is available.

The reviewer is not rewarded for raw issue count. The producer is not rewarded for mechanically accepting all edits. A successfully rebutted objection is a corrected review, not automatically an improved manuscript.

## 7. Progress and compute policy

Maintain two separate records:

- **Artifact progress:** verified improvement to an accepted deliverable while preserving essential requirements and passing regression checks.
- **Information progress:** new, supported information that materially changes a decision or exposes a risk, even when it makes the current artifact look worse.

Neither activity volume, reviewer satisfaction, issue closure count, nor uncalibrated model confidence is the objective. Risk acceptance is not a repair. Deleting a difficult required claim does not count as progress unless the Principal authorizes a scope change or the replacement demonstrably still meets the approved mission.

The Progress Controller allocates renewable compute tranches across a portfolio of justified actions: targeted retrieval, counterexample search, alternative construction, synthesis, repair, verification, or comparison. It tracks elapsed time, observed capacity, review/integration backlog, cost, verified outcomes, coverage, regressions, and uncertainty. Valuable alternatives can survive without immediately beating the incumbent. When evidence is missing, changing the wording repeatedly is not a substitute for obtaining evidence.

Each wall-clock checkpoint records the current admissible artifact or its absence, verified artifact and information changes, unchanged state, blockers, and the next allocation. A long investigation may continue across checkpoints with a supported reassessment horizon. Stagnation triggers diagnosis and redirection; it cannot be erased by new task IDs or lease renewal. The system guarantees truthful accounting, not discovery on demand.

Hard capability, capacity, expenditure, and explicit mission limits coexist with generous allocation. Window exhaustion triggers allocation review and may renew automatically under existing delegation. Missing evidence, unresolved authority, repeated unsupported continuation, or exhausted hard limits can pause affected work. Satisfying the mission's completion policy stops discretionary work; available GPUs are not a reason to perpetuate a completed mission.

## 8. Core entities and versioning

Core control entities: `Project`, `PrincipalIntent`, `Mission`, `Score`, `ResourcePolicy`, `AllocationWindow`, `Task`, `TaskAttempt`, `EditGrant`, `ContextPackage`, `Message`, `Event`, `GateResult`, `HumanApproval`, `Release`.

Research/quality entities are immutable artifact types: `ResultsPackage`, `ResearchArgument`, `SearchCampaign`, `QueryRecord`, `DiscoveryRecord`, `ReferenceCard`, `SourceCapture`, `EvidenceRecord`, `CoverageReport`, `Claim`, `Critique`, `Response`, `Adjudication`, `Verification`, `ReviewCoverage`, `ProgressRecord`, `SupervisionDecision`, `ProgressCheckpoint`, and ordinary drafts/reports. Their status histories are event projections. A separate `Issue` identity tracks a defect across target revisions. The activity graph and candidate frontier are projections over these records, not additional independent stores.

Every substantive artifact is immutable, version-pinned, owned, hashed, and linked to inputs. An `ArtifactRef` identifies both a logical artifact and its exact version. A release pins all included artifacts and the approval/evaluation conditions under which they were accepted. Dependency changes invalidate affected acceptance decisions without deleting earlier evidence.

Structured deliverables additionally use `ContentUnit`, `DocumentManifest`, `ChangeRequest`, and `ChangeSet` as ArtifactVersion types. Their exact contracts are in `45-artifact-change-control.md`. Each unit's purpose, claim/evidence links, revision history, and review coverage remain inspectable. New whole-document candidates cannot evade edit grants by changing artifact IDs, replacing a parent, or modifying shared macros and references.

## 9. v1 scope and non-goals

**Included:** per-project organization/environment/capabilities; on-demand practical operations; delegated project-local program installation/build/connection and execution; supplied-results ingestion; organization-wide task-scoped web discovery; active API/tool-backed search campaigns; multilingual/source-class search where configured; source acquisition; reference normalization; evidence promotion and mapping; method/argument critique; writing; stable paragraph/structure management; purpose-scoped changes and independent integration; rendering; human approval; bounded event-driven rework; durable storage; recovery; and progress/cost measurement.

**Excluded:** new experimental execution, new statistical estimation, installation/execution outside project delegation, external submission/publication, self-modification of governing intent, implicit cross-project private context, and polished UI. Authorized source extraction, conversion, validation, API/MCP operation, and document compilation are permitted. Actual program use does not expand the scientific mission into unapproved analysis.

A raw dataset without interpretable, supplied results is insufficient for a final paper in v1. Return an intake-gap report rather than silently performing analysis or fabricating results.

## 10. Open configuration and working defaults

| Item | Current disposition |
|---|---|
| Q1 harness | D9 retained; D19 gives a proposed transactional implementation |
| Q2 deliverable | Mission-selected artifact and evidence/review archive; the paper flagship requires English LaTeX source + rendered PDF |
| Q3 retrieval providers | Web Intelligence Fabric contract fixed; exact search/index/full-text/patent/standards/code/dataset providers, credentials, source rights, robots/terms constraints, and outbound-data policy remain project configuration |
| Q4 models/resources | Capable role profiles and plentiful external inference are the default direction. Endpoints, measured capacity, data policy, finite leases, and metered limits remain deployment configuration. Authorized capacity windows renew automatically; missing authorization blocks dispatch |
| Q5 human checkpoints | Faithful explicit instructions can activate intent/mission. Internal plans and outlines advance within delegation; an optional mission policy may require outline approval. Final release, material goal/trade-off changes, and out-of-mandate actions require the Principal |
| Review independence | Separate role/context mandatory; additional model/provider optional and subject to data permission. No cloud fallback without authorization |
| Time policy | Finite elapsed cap required by the bounded runtime; optional first-result and completion targets derive from declared stage estimates when omitted. Estimates and measured timings remain distinct |
| First evaluation dataset | Must be defined and frozen before claiming improved quality or scaling benefit |

These defaults make the blueprint internally concrete; they do not falsely mark the user's outstanding provider/budget choices as decided. Project configuration must resolve them before the corresponding live operation.

## 11. Document map and change log

| File | Responsibility |
|---|---|
| `00-SSOT.md` | Intent, authority, decisions, concepts, and scope |
| `05-system-concept.md` | Organizing model, compute-rich supervision, anytime progress, and research rationale |
| `15-project-organization.md` | Project instances, case-activated Operations Cell, tool environments, real execution evidence, and isolation |
| `20-architecture-v0.md` | Organization, event-driven execution, evidence, storage, gates, and operational policies |
| `30-roadmap.md` | Phases, acceptance tests, evaluation plan, and implementation priorities |
| `40-execution-contract.md` | Exact identifiers, lifecycle rules, schema field contracts, and Score semantics |
| `45-artifact-change-control.md` | Stable structural units, references, surgical revision, edit grants, and verified integration |
| `50-web-intelligence-integration.md` | Active retrieval, API/plugin adapters, capability state, and evidence-to-change routing |
| `70-scored-project-runtime.md` | Implemented bounded Score, selected operational workloads, non-paper fixture, exact candidate checks, and result/time contracts |
| `75-literature-survey-score.md` | Implemented bounded literature survey, focused reviews, independent gap challenge, evidence gates, and evaluation contract |
| `80-completion-runtime.md` | Implemented recovery, executable plans, capability acquisition, blinded evaluation, and paper release-candidate contracts |
| `90-experiment-runtime.md` | Implemented bounded scientific execution, exact replay, independent recalculation, result review, and generated-result provenance |
| `100-research-argument-runtime.md` | Pre-composition question, hypothesis, discriminating-test, and figure/table argument contract |
| `16-agent-department-flow.md` | Functional stage routes, concrete appointments, handoffs, lifecycle, and scoped continuation |
| `85-multimodal-visual-review.md` | Hash-pinned image inputs, independent visual judgment, exact synthesis, and scoped repair actions |
| `web-search-campaign.yaml` | Illustrative search campaign; not a working runtime |
| `25-p0-freeze.md` | P0 contract-freeze record: frozen principles, selected first fixture, deployment-configuration template, acceptance state |
| `fixtures/`, `config/` | Non-normative test assets: the P1 slice fixture set and the deployment template pinned by the P0 freeze record |

| Version | Content |
|---|---|
| v0.1 | Original D1–D8 and initial organization |
| v0.2 | D9 hybrid harness and D10 rendered deliverable |
| v0.3 | D11 paper language |
| v0.4 | D12 English-first development |
| v0.5 | D13–D21; Principal-aligned command; four departments; adversarial protocol; evidence-level traceability; verified progress; durable execution contracts; phased evaluation |
| v0.6 | D22–D24; organization-wide Web Intelligence Fabric; specialized Research retrieval roles; independent adversarial search; coverage-oriented search campaigns and discovery→evidence promotion |
| v0.7 | D25–D30; compute-rich activity graph; renewable capacity; rational supervision; provisional exploration; elapsed-time checkpoints; adaptation in the first implementation |
| v0.8 | D31–D36; surgical revision; paragraph/structure/reference versioning; scoped grants and atomic integration; active API/plugin intelligence; per-project organization and on-demand practical operations |
| v0.9 | D37–D39; general-purpose project core, mission-selected artifacts/tools, explicit result/time contracts, and a literature-assessment prerequisite for paper contribution selection |
| v1.0 | D40–D44; source-aware recovery, dependency plans, approved capability acquisition, frozen-label evaluation, and an evidence-bound rendered paper candidate |
| v1.1 | D45; reusable hash-pinned multimodal review, exact perspective reconciliation, and purpose-scoped visual repair |
| v1.2 | D46–D47; frozen experiment execution with replay and distinct validation, plus storyline-first paper assembly with explicit evidence relations |
| v1.3 | D48; research-argument discovery and adjudication become a mandatory pre-composition gate with evidence-bound hypotheses, discriminating tests, and figure/table jobs |
| v1.4 | D60–D61; sampled capability-aware free-topic intake and deadline-governed Composer continuation for executable research requests |
| v1.5 | D62; durable project department charters, inbox/work-order backlog, autonomous activation/resolution, and template-as-default organization runtime |
| v1.6 | D63; append-only cross-run topic memory, capability rotation, and selected-direction novelty gate |
| v1.7 | D64; topic-maturity admission, evidence-driven topic refinement, and lineage-preserving re-entry |
| v1.8 | D65–D66; pre-composition scientific red-team gate and single-source functional routing with concrete agent roster |
| v1.9 | D67; project-organization-2 bounded specialist pool, role-isolated Composer assignments, independent verdict artifacts, and v1 migration |
