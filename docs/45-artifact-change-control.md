# Sci-saurus — Structured Artifacts and Surgical Change Control

> **Version:** v0.8 · **Date:** 2026-09-10 · **Status:** proposed execution contract; not an implemented editor.
> Extends [Execution Contract](40-execution-contract.md) under [SSOT](00-SSOT.md) D31–D33. Applies to all generated deliverables, with paper-specific block kinds in the paper Score.

## 1. Governing rule

An edit must solve a named problem in an explicitly authorized region while preserving the rest of the artifact. A worker receives enough context to understand the result, but may only submit changes covered by its edit grant. Artifact ownership, model capability, and responsibility for integration do not grant permission to rewrite an entire deliverable.

The system enforces this rule in the publication service. A prompt asking for careful editing is not the enforcement mechanism. Purpose, exact versions, permitted operations, preserved requirements, impact, and acceptance evidence travel together through the change lifecycle.

## 2. Canonical structure and identity

Reuse the existing immutable ArtifactVersion store for two artifact types:

| Type | Content and responsibility |
|---|---|
| `ContentUnit` | A stable logical unit with a kind, body, owner, structural/argumentative purpose, required claim refs, citation occurrences, asset/definition refs, and preservation requirements |
| `DocumentManifest` | The exact ordered containment tree of unit refs; document contract; pinned rendering/bibliography configuration; predecessor manifest; and change/integration provenance |

The smallest default textual unit is a paragraph. Other kinds include document/section headings, list items, equations, tables, figures, and captions. Containers hold their own heading/contract metadata, not a second embedded copy of descendant prose. The DocumentManifest owns containment and order; each live unit appears once in that tree. Semantic relations such as a paragraph expressing several claims are separate typed links, not duplicated tree ownership.

Use the ArtifactVersion logical ID as unit identity. Titles, paragraph numbers, source line numbers, and PDF coordinates are display locators, never identity. A unit preserves its ID when moved or edited; edits create a new immutable version. A document version pins every included unit version. Moving an unchanged unit changes the manifest topology without inventing a new body version.

Split and merge operations create explicit lineage links from the old units to the new units. Removed units remain in history and receive retirement events; IDs are never reused to hide a replacement. All current tree entries, anchors, and semantic/reference links must remain resolvable. A link to a historical retired unit remains valid for audit but cannot silently stand in for its current replacement.

Unit purpose and preservation requirements are governing inputs. A body-edit grant cannot alter them. Their amendment requires its own scoped decision under the mission's authority rules.

The assembled Markdown/LaTeX and PDF are derived artifacts. A renderer deterministically assembles the pinned structure and produces a source map from units to generated source spans and rendered locators where available. Regeneration does not rewrite the unit store. Unsupported imported formats remain immutable source artifacts until an explicit structure-import task establishes and checks the unit map; segmentation uncertainty cannot silently assign write authority.

## 3. Claims, citations, and reference materials

Each material claim occurrence links its stable claim identity and exact Claim version to the unit and a version-local span or structured locator. Text-span anchors include the unit version and preimage hash; a citation must be reanchored or explicitly retained after its surrounding text changes.

A citation occurrence binds:

- a stable occurrence ID and the exact ContentUnit version/span;
- a ReferenceCard version identifying the cited work;
- the exact SourceCapture and EvidenceRecord versions and source locator actually inspected;
- the associated claim version and support relation, or an explicit contextual-reference role.

The containing unit manifest supplies the occurrence's enclosing unit/version; occurrence metadata does not embed its own enclosing content hash. This avoids a circular hash dependency while preserving an exact version-local locator.

DOI/URL identity, citation-key aliases, captured source content, and verified support are distinct. A refreshed source or metadata record creates a new version. Updating a bibliography alias must not repoint a citation to another work, edition, capture, or claim relation. Source acquisition failure leaves a visible gap; no reference placeholder is promoted as support.

Supplied datasets, plots, tables, reported values, and original sources remain immutable. A derived crop, formatted table, caption, or interpretation has its own artifact/version and provenance. Changes to a caption or surrounding prose do not authorize changes to the underlying data or asset. A source update triggers impact assessment for affected claims and units; it does not edit the manuscript automatically.

## 4. ChangeRequest and EditGrant

Every modification starts with an immutable ChangeRequest. It contains:

| Field group | Required content |
|---|---|
| Purpose | Mission/intent refs; originating issue, requirement, or justified improvement opportunity; expected effect and acceptance condition |
| Baseline | Exact DocumentManifest and target unit/asset/contract refs and hashes |
| Scope | Explicit unit IDs and permitted fields/spans; allowed operations; structural edges/anchors where needed |
| Preservation | Required findings, values, claim qualifications, citation support, unit purposes, and unchanged-region guarantees |
| Evaluation | Required premise/context refs; proposed impact set; mechanical, semantic, and rendered checks |
| Authority | Owning departments; existing delegation; scope decision; any required human approval |

An `EditGrant` is a control-plane capability binding this request and baseline to an authenticated actor, task attempt, permitted operations, expiry, and fencing token. Read access and edit rights are separate. A reviewer can read the full permitted manuscript and propose a repair without obtaining write access to it.

Operations are typed: replace a specified body/span or field; insert a new unit at an exact structural anchor; move a unit between exact parents/anchors; split; merge; retire; or change an explicitly named citation/asset/definition/contract binding. Operations carry expected preimages and new content refs. A parent/section grant does not recursively authorize replacing its descendants. Structural operations enumerate affected membership/order edges and unit IDs.

The default repair grant covers the smallest semantically sufficient scope. It grants no wildcard document replacement, raw filesystem writes, direct accepted-head changes, or permission to create and swap in a substitute manuscript. First creation and genuine alternative structures use an explicit creation/structure plan listing the intended units and purposes; the service allocates IDs and validates membership before generating content.

The producer may request expanded scope with impact evidence. A non-conflicted owner/command role can approve an expansion within existing delegation; material changes to the goal or preserved requirements need the Principal. The producer, reviewer, or integrating service cannot silently expand its own authority. Related requests retain a causal change-family ID, allowing supervision to detect incremental scope creep across agents or task IDs.

## 5. ChangeSet and complete mutation scope

The worker submits an immutable ChangeSet: request/grant refs, base manifest, expected unit/edge/binding preimages, exact proposed operations, replacement object hashes, supporting premises, consulted context refs, predicted impact, and a concise purpose-based explanation. Its proposed tests or self-review do not count as independent verification.

The control plane computes the actual mutation set from the before/after structured representation. It compares bodies, unit contracts, topology, claim/citation bindings, and assembly dependencies against the grant. Bytes and bindings outside that set must remain identical; generated source offsets or PDF pagination may change as a consequence of authorized assembly. Such consequences are reported and checked, not represented as unit edits.

Within a target paragraph, protected spans/fields must retain their exact values. Other edits still have to serve the request's purpose. Replacing the entire paragraph payload cannot conceal unrelated rewriting: the review displays the complete textual and semantic diff within the granted unit. No arbitrary changed-character percentage substitutes for this assessment.

Shared macros, terminology, bibliography configuration, templates, style assets, and section order can affect many unchanged paragraphs. They are separately versioned dependencies with their own edit scope and impact checks. Literal body fields cannot introduce global definitions, includes, scripts, or directives that bypass those boundaries. Unsupported active constructs are rejected or handled through an explicitly authorized assembly change.

All model-authored content, including a newly invented alternative artifact, remains staged until the same publication and adoption rules pass. Namespace ownership cannot bypass this service. A model never receives authoritative write access through a Git checkout, generated LaTeX file, database, export, or rendering directory.

## 6. Impact, verification, and acceptance

Before review, the service derives a conservative impact closure using known premise/subject, claim, citation, definition, structural-neighbor, and rendering dependencies. The independent reviewer may expand that closure. The producer's declared impact is a proposal, not the sole authority. Missing semantic dependencies require broader inspection or remain an explicit uncertainty.

Three scopes are kept distinct:

1. **Read scope:** context necessary for a correct decision, potentially the whole permitted document.
2. **Write scope:** exact units, spans, edges, and bindings the worker may change.
3. **Verification scope:** the changed material and anything its meaning or presentation may affect.

A local edit can therefore require document-wide consistency or render inspection without granting document-wide rewrite rights. An affected but unchanged abstract may be flagged for a separate repair. Until required dependent repairs pass, the candidate can be staged but cannot become the accepted manuscript.

Review verifies that the named defect/improvement condition is resolved, unaffected findings and unit purposes survive, evidence supports the exact revised claim, and the assembled argument remains coherent. Mechanical checks verify scope, hashes, tree validity, cross-references, protected values, and required coverage. Rendering checks follow the actual layout impact and always apply to final release.

A review record names exact units, dependencies, structure, and checks. Unchanged results may be reused only through an explicit proof that their units, relevant premises, structural context, and governing criteria remain applicable. A new document hash does not inherit a whole-document approval. Human approval of an exact final artifact is never transferred by review reuse.

Only the control-plane integration service composes operations and publishes the resulting candidate manifest. The service does not invent prose or resolve scientific disputes. Acceptance requires valid independent verification and a recorded selection, then atomically compares the expected incumbent, grant applicability, governing refs, and dependency validity before changing the accepted head.

## 7. Concurrency, atomic changes, and recovery

Parallel workers may propose edits to different units from the same baseline. Disjoint write sets are insufficient for automatic composition: shared definitions, premise versions, citation bindings, section order, and argument interactions can conflict.

If the document head advanced, the service can construct a rebased candidate only after checking the read/premise and structural preconditions against the current head. It records the new base, mapping, grant revalidation under the original scope, and any reused evidence explicitly. A relevant changed premise, overlapping mutation, or ambiguous semantic dependency requires renewed review or a fresh proposal. There is no last-writer-wins policy or unreviewed automatic LLM merge.

Broad consulted context is recorded separately from relied-on premises. Review establishes whether intervening changes affect the candidate; uncertain dependence prevents automatic review reuse. The exact composed document receives the required integration checks, even if each local patch previously passed.

One purpose may need coupled edits across a claim, discussion paragraph, abstract, and citation. Such an atomic change group has an explicit shared request and scoped sub-grants for each responsible worker. Stage all candidates, verify their combined result, and adopt all changes in one manifest transition. Partial results can remain visible as candidates; a half-applied argument cannot become the incumbent.

Validate acceptance against the proposed transaction post-state: accepted external evidence/premises plus independently verified group members accepted together. A revised claim need not be accepted before its linked paragraphs can join that same transaction. Require acyclic factual support and prohibit using a group's own assertions or subject-review links as their evidence. Member acceptance, relevant heads, manifest, and events commit together; failure leaves all unchanged.

A failed application or crash leaves the old accepted manifest intact. Recover staged objects using existing outbox/idempotency/fencing contracts. Repairing an accepted regression creates a scoped inverse ChangeSet against the current manifest, preserving unrelated later improvements. Do not reset the entire document head to erase an old mistake. Whole-snapshot replacement requires an explicit coordinated decision with impact review.

## 8. Reviewable history and example

The artifact view must support document outline and unit IDs; a unit's purpose, versions, exact source/claim links, issues, and review coverage; before/after text and structural changes; protected-region comparison; pending changes and conflicts; and composition of an exact release. This is an output contract for CLI/structured reports first, not a prerequisite to build a polished editor UI.

Example: a Discussion paragraph asserts causation while its supplied evidence establishes association. A repair request permits changing that inference and its claim binding, while preserving the result values, evidence identity, and adjacent findings. The worker reads Methods, Results, and the surrounding argument but can write only the named paragraph span and Claim candidate. If the abstract repeats the overclaim, the impact review adds a linked, separately granted abstract repair. Both enter the accepted manifest only when their combined inference and reference checks pass.

The history shows which defect was corrected, which unit versions changed, the exact supporting captures, which protected material stayed identical, who proposed and verified the change, and the manifest in which it was adopted. A global stylistic rewrite cannot be included in that request.

## 9. v1 implementation boundary

Implement stable units, manifests, scoped grants, deterministic ChangeSet application, and conflicting/stale-patch rejection in P1. Exercise a paragraph repair and structural/reference conflict before adding the full writing organization. P2 adds richer paper units, source maps, bibliography assembly, and rendered impact checks. Reuse existing artifact, task, issue, verification, and event services; no independent document database or second approval hierarchy is required.

Existing v0.7 monolithic artifacts remain immutable historical objects. Import them through a reviewed segmentation task with source-locator mapping and verified faithful reconstruction before scoped editing begins. If segmentation cannot preserve the original structure/meaning, record the gap; do not infer identities by line numbers and proceed as if reliable.
