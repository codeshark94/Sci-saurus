# Project integration runtime

The project runner revises multiple assigned paragraphs concurrently while preserving an ordered section tree, immutable paragraphs, per-unit values, and claim bindings. It composes candidates centrally and adopts only the exact document that passed every required independent review. This is a bounded public-data integration workflow, not a complete paper or release workflow.

## Setup

Install the runtime described in [paragraph setup](60-paragraph-runtime.md#setup), then prepare a local configuration:

```bash
sh scripts/setup-runtime.sh
python3 scripts/prepare-project-config.py --output /tmp/project-run.json
```

The helper uses the [synthetic project template](../config/project-run.example.json) and records the installed public software identity files. It preserves the virtual environment interpreter path, sets no credentials, performs no installation, and refuses to overwrite an existing output file. Set the authorized endpoint, deployed model name, optional `model.auth_env`, and `live_dispatch_allowed: true` in the generated configuration. The compatible API supports explicit reasoning effort and structured JSON output when the deployed server implements them.

```bash
python3 -m scisaurus.cli run-project /tmp/research-project --config /tmp/project-run.json
python3 -m scisaurus.cli status /tmp/research-project
python3 -m scisaurus.cli verify /tmp/research-project
```

Use a new project directory. Exit codes are `0` for adoption, `2` for invalid configuration, and `3` for blocked, paused, or unresolved work. Keys belong only in the named process environment variable, never in JSON or committed files.

## Assignment and integration contracts

| Object | Enforced boundary |
|---|---|
| Section | Unique stable ID, heading, ordered paragraph membership |
| Paragraph | Unique ID, baseline text, explicit editable flag, own objective, literals and claim IDs |
| Immutable paragraph | No revision objective; exact original reference and position retained |
| Claim | Known project-local ID, supplied statement, immutable version tied to governing context |
| Producer proposal | Assigned unit and exact baseline ref only; one paragraph; author, execution, criteria, context, claim and source refs retained |
| Composed candidate | Central scoped ChangeSet containing all required paragraph proposals; accepted head unchanged during review |
| Independent review | Every changed unit plus the exact whole document; explicit objective-resolution, source-support, reader-facing, per-unit coverage and cross-document consistency |
| Adoption | All checks pass, no regression/material uncertainty, governing inputs still current, run active, accepted baseline unchanged |

The supervisor identifies a concrete defect and per-unit change focus. Producers run in separate processes and receive other paragraphs as read-only context. A producer cannot choose another unit, move text between sections, or satisfy its required value using another paragraph. The integrator retains each proposal's actual author; it does not attribute all reasoning to itself.

Unit reviewers inspect the same composed candidate with the full document visible. A separate document reviewer checks cross-paragraph consistency and every assigned unit contract independently. Every check binds the same candidate, baseline, critique, response, and resolution condition. The required-check registry names both resolution and regression checks explicitly; missing IDs or a wrong check kind block acceptance. A passing local review cannot substitute for a passing document review. Deterministic checks additionally compare unit values, claim/citation bindings, exact immutable references, section order, and assembly dependencies.

If one producer fails, successful sibling proposals remain available. The supervisor may request another targeted revision with a concrete rationale; successful unrelated proposals are reused. Any new composition receives fresh unit and whole-document reviews. A changed baseline, governing context, criterion, claim, or captured source invalidates reuse and requires fresh work. Unknown external outcomes stop automatic continuation and retain reservations for explicit reconciliation.

## On-demand Operations Cell

The configured catalog currently contains [Crossref search](https://github.com/CrossRef/rest-api-doc) and the [official MCP Fetch server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch). Registration records an explicit adapter profile, executable, software identity hashes, safe environment, project identity, and representative operation. It does not establish readiness by itself.

```mermaid
flowchart LR
  A[Registered inactive profile] --> B[Activate for project need]
  B --> C[Execute representative operation]
  C --> D[Independent operational checks]
  D --> E[Verified project binding]
  E --> F[Idle cell, capability usable]
  F --> G[Routine research workload]
  G --> F
  F --> H[Drift or failed operation]
  H --> I[Degraded, binding invalidated]
```

The engineer, operator, and operational verifier have separate recorded roles. Verification inspects real local execution evidence, task/attempt success, captured bytes and hash, provider identity, and schema. MCP verification also inspects `initialize`, `tools/list`, and `tools/call` transcript records. The [stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) uses the project-owned subprocess workspace; extractor temporary files stay there. Installed public software is reused, while mutable scratch and capability bindings belong to one project instance.

Routine research uses the verified binding without repeatedly activating the full cell. A valid empty Crossref search remains a successful no-match result; it never creates a reference or downgrades the transport merely for finding no items. Partial captures, failed output checks, changed pinned files, or schema drift invalidate readiness. A new session cannot silently reuse an old live binding. Cleanup removes only the owned scratch directory and binding, preserving evidence and installed software.

Operational readiness says that a tool executed correctly. It does not establish that its output supports a scientific claim. Producer and Methods retrieval execute separately; claim support and factual interpretation are checked later against exact source captures and supplied facts.

## Capacity, progress and cancellation

The shared execution engine admits real concurrent subprocesses within a finite project capacity pool. At least three slots are required: two parallel workers and an independently reserved integrated-review slot. Larger documents use as many admitted workers as the configured pool allows; queued work cannot consume capacity reserved for verification. Parent-owned transactions record tasks, attempts, usage, results, and checkpoints.

Completed sibling results and measured usage survive another worker's failure or cancellation. Cancellation blocks subsequent staging, dispatch, and adoption, including when the final reviewer wrote a result immediately before the cancellation was observed. Local worker-group termination does not prove remote provider cancellation. Unknown invocations retain their reservations; missing token dimensions remain explicit accounting gaps. The wall-clock deadline also fences later staging and adoption.

## Outputs and supported scope

- `output/manuscript.md`: the accepted document only; a blocked run retains its previous baseline.
- `output/report.md`: proposal authors, candidate/adoption refs, Operations readiness, sources and blockers.
- `output/run.json`: exact proposal/candidate/verification refs, targeted rounds, failures, capability state, usage and event-chain result.
- The project store retains immutable versions, claim/source relationships, task/attempt evidence, all checks, and checkpoints.

The current workflow uses configured public queries and URLs. The supervisor and reviewers have separate contexts but may use the same model. General tool discovery, arbitrary program installation, model/provider diversity, automatic crash resume, shared quotas across projects, figure/LaTeX/PDF assembly, human release, and research-quality evaluation are separate work. Project profiles record configured software pins; they are not a container sandbox or a claim that every transitive installed dependency has been attested.

## Validation

The regression suite exercises genuine concurrent local workers with explicitly simulated model outputs, exact combined-candidate verification, targeted reuse, rejected foreign/immutable assignments, stale inputs, claim/structure preservation, and cancellation boundaries. Operations tests exercise captured-output integrity, independent readiness roles, project/session isolation, schema/file drift, valid empty searches, and cleanup. Live external evidence is recorded separately from these deterministic tests.

```bash
python3 -m unittest discover -s scisaurus/tests -t . -v
```

## Live validation

On 2026-09-10, the synthetic fixture completed through the configured external `qwen3.8-27b` endpoint with high reasoning effort and JSON-object output. The retained run is `.runs/axion-project-20260910T124027Z`, run ID `d23e58b4b8d84972a87b9e67f6fa59dc`. The source manifest records 46 source, fixture, setup and lockfile hashes, all matching the verified checkout.

| Measurement | Observed result |
|---|---|
| Final state | Composed `manuscript@2` adopted after all required reviews |
| Elapsed time | 141.06 seconds |
| Model calls | 6: supervisor, two producers, two unit reviewers, one document reviewer |
| Reported tokens | 18,801 input; 19,279 output including provider reasoning usage |
| Live retrieval | 3 Crossref requests and 3 official MCP Fetch requests, including readiness probes |
| Parallel work | Producer task intervals overlapped for 11.20 seconds; unit-review intervals for 26.41 seconds |
| Verification | 15 passing bound checks; no regressions or material uncertainties |
| Preservation | Both unit contracts/claim bindings, immutable paragraph, section order and assembly dependencies retained |
| Progress | 26 checkpoints; maximum observed interval 15.06 seconds, rounded upward |
| Accounting | All reservations settled; no unreported usage dimensions |
| Retained evidence | 135 artifact versions; 297 hash-chained events |
| Regression suite | 210 tests passed |

The overlap measurement is based on local invocation start/completion records; it does not assert simultaneous physical GPU kernel execution. Both capabilities passed independent operational checks and returned to idle while remaining usable. The revised prose contains supplied facts and permitted interpretation, so both captured-source support lists are explicitly empty; separate source-support checks verified that no external assertion lacked evidence.

`output/run.json` has SHA-256 `a1474083b0ca7864b3dce1dfc7dd20518f2e1a57f89a5ae1cc11ec4358ec2f92`. `output/manuscript.md` exports the accepted three-paragraph document. An earlier run at `.runs/axion-project-20260910T123442Z` remains paused because a unit reviewer omitted a resolution check; its passing document review did not bypass the missing unit evidence or move the baseline. The explicit required-check registry and regression coverage address that failure without weakening acceptance.

These observations establish bounded synthetic integration behavior, not general scientific quality. Release status remains `not_released`; full-paper assembly, arbitrary tool installation, multi-project scheduling and quality evaluation remain outside this run.
