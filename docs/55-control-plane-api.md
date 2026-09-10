# Control-plane API boundaries

The Python services implement local transactional control and evidence records. The [paragraph runtime](60-paragraph-runtime.md) connects these services to configured model endpoints and live Crossref/MCP retrieval. A general scheduler, authenticated control-plane transport, and dynamic Operations Cell orchestration remain roadmap work. The core services validate records and do not themselves certify scientific validity.

## Structured changes

`ChangeService.apply_changeset` requires the grant's actor, its exact accepted baseline, and the matching expected accepted manifest version. Grant validation, operation planning, artifact publication, retirement, and adoption share one transaction. A changed accepted baseline requires a fresh grant; there is no automatic rebase.

Multiple `replace_body` operations use coordinates in the original unit. Disjoint replacements compose into one successor version; overlapping or ambiguously ordered insertions reject. Explicit citation reanchors use coordinates in the final composed unit. Unaffected citations shift with preceding edits.

A split preserves the exact source slices and assigns each citation to its corresponding slice. It cannot cross protected text or citation spans. Splitting a container requires first moving its children to explicit destinations. Retiring a subtree requires retirement authority for every removed unit. Split or retire cannot combine with another operation on the same unit. Assembly dependencies remain pinned unless explicitly replaced.

`stage_changeset` publishes candidate versions without changing accepted state. This path currently supports body replacements only. `accept_changeset` rechecks the grant and baseline and requires an exact, committed, independently passing issue verification for that candidate. Its optional `expected_head_refs` list pins governing artifact heads: each logical artifact may occur once, and every supplied ref must still be its current head inside the adoption transaction. Paragraph and project runners pass their governing context, criteria, source captures, and project claims through this precondition. A competing writer cannot change those inputs between the comparison and adoption. Candidate alternatives pin the same baseline; approval cannot transfer between them. `apply_changeset` remains the trusted one-shot integration API; external workers use staging through the runner.

## Issue closure

`IssueManager.verify` takes `response_ref`, `candidate_ref`, `baseline_ref`, `resolution_condition`, and `check_refs`, alongside the issue ID, verification ID, verifier, and rationale. The candidate must be a changed descendant version of the contested artifact. The response must be the currently submitted repair committed through the issue lifecycle.

Each check reference names an immutable `evidence_record` with:

- Exact `issue_id`, `critique_ref`, `response_ref`, `candidate_ref`, `baseline_ref`, and `resolution_condition` bindings.
- A distinct `check_id`, an executed `method`, and its observed `result`.
- `kind` equal to `resolution` or `regression`, and `outcome` equal to `passed`, `failed`, `insufficient_evidence`, or `check_failed`.
- Manifest subject inputs pinning the inspected baseline and candidate.

Closure requires both check kinds, all outcomes passing, and no reported regressions or uncertainties. Producer, critic, response, candidate, and inspected document-unit authors must recuse from adjudication or verification as applicable. Caller identities are compared as supplied; transport authentication belongs to the runner. Failed verification keeps the issue open and does not adopt the candidate.

Responses, adjudications, verification artifacts, and their state transitions commit together. `request_evidence` enters `needs_evidence` and permits a subsequent response. Legacy responses without a committed transition reference cannot support adjudication or verification. Such issues require explicit reconciliation and a newly recorded response; the service does not infer approval from orphaned artifacts.

## Capacity accounting

All allocation windows sharing a `policy_id` draw from the same finite concurrent-capacity pool, including windows under different delegations. A new window cannot expand that pool's capacity. Resource quantities must be numeric, finite, and nonnegative. Renewal closes the predecessor and creates the successor in one transaction, recording its rationale.

Outstanding reservations persist across renewal and restart. Settlement releases the reservation and records measured usage once in the shared pool ledger, including late settlements against closed windows. Actual overruns remain visible. `get_window` exposes live pool reservations and cumulative usage rather than a stale renewal snapshot. Existing window stores migrate from settlement events and initial usage seeds without summing copied renewal snapshots.

This is concurrent-capacity accounting. Full ResourcePolicy enforcement, metered expenditure ceilings, attempt reservation expiry, and provider-specific admission remain runner work. The ledger does not interpret a resource name such as `usd` as a policy mode.

## Recovery

Unknown external outcomes retain reservation estimates. Reconciliation can block active work but preserves terminal and paused task decisions. A later provider result may settle the attempt record without changing the parent task's terminal state.

Expired message leases return to the discoverable retry queue. Artifact publication validates its entire outgoing envelope batch before publication. Recovery quarantines malformed legacy outbox entries, retains their original undispatched envelopes, emits `outbox.quarantined`, and continues dispatching valid messages. `MessageBus.quarantined()` and CLI `status` expose these failures. Quarantine is not acknowledgement or successful delivery.

## Validation

```bash
python3 -m unittest discover -s scisaurus/tests -t . -v
```

The suite covers valid composition, rejected mutations with no committed effects, independent evidence bindings, lifecycle rollback, shared-pool concurrency, delayed settlement, legacy accounting migration, retry discovery, and quarantine recovery.
