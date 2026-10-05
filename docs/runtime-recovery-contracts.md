# Runtime recovery contracts

## Execution permission

Managed runs use `output/run-control.json` and `run-control.lock`. An explicit
start or resume creates a generation bound to a validated workflow. Stop persists
`stop_requested: true`, including when the process has already exited. A failed
launch revokes its grant.

CLI resumes require the existing active grant. Worker processes inherit its path
and generation. Embedded Composer runners bind to the managed project grant.
Before provider submission or sandbox process creation, the worker takes a shared
lock and verifies that the generation is current and not stopped. Stop uses the
exclusive lock. The lock covers submission, not the response wait. Supervisors
recheck permission during cooldown and while monitoring their child.

Standalone API callers remain responsible for their own admission. They must bind
managed execution through the workflow or project permission context.

## Independently authored programs

The independent validator author receives the frozen intent, configured input,
observation schema and sample, output shapes, and exact five-key runtime envelope.
The executor implementation and its computed answer are not supplied.

Accepted program transports are:

- One strict JSON object containing `validator_source`.
- One complete Python code block with no accompanying prose or additional block.
- For repairs, exact source edits against that author's recorded validator.

Transport extraction preserves source bytes, including the final body newline.
Response hash, source hash, transport, and extraction span are retained separately.
Transport acceptance does not admit a program. Static scan, sandbox readiness,
current candidate recalculation, replay and scientific review still apply.

A truncated valid JSON prefix can use the existing bounded suffix-continuation
contract. Every segment is bound to its exact preceding prefix and captured
request. The latest provider response remains distinct from assembled bytes.
Unknown dispatches cannot be replaced with an older succeeded response.

Execution errors carry the current independent source and diagnostic into that
author's patch request. Identical source/input failures do not trigger another
blind rewrite. Recalculation disagreements retain Methods adjudication ownership.

## Captured response ownership

Reuse requires exact role, assignment, provider outcome, model, finish reason,
elapsed time, usage, prompt hash and response hash. Seed inheritance carries the
original request and immutable source artifact identity; it does not add a call or
bill the historical usage again.

Legacy prompt hashes can be derived only from a verified immutable controller
artifact containing the paired response and its latest succeeded request. The
migration has its own provenance marker. Missing, stale, changed or unknown
receipts do not authorize reuse. Inherited chains are read back from their original
artifacts and checked for cycles and body changes.

## Measurement and decisions

`primary_outcomes` retains its existing schema. An intent can also declare:

```json
{
  "decision_outcomes": [
    {"id": "contrast", "definition": "Exact formula, aggregation and scope",
     "unit": "rate", "parents": ["declared_primary"]}
  ],
  "decision_rules": [
    {"id": "contrast_limit", "metric_id": "contrast", "unit": "rate",
     "operator": ">", "threshold": 0.1,
     "claim": "Conditional claim within the declared model scope"}
  ]
}
```

Decision parents reference preceding declared outcomes. Rules require an exact
metric identity, matching unit, finite non-boolean threshold and explicit
comparison operator. All primary and decision outcomes reach the independent
validator and must be recalculated exactly once. Reported values are bound to the
current candidate; they cannot serve as recalculation answers.

An undefined parent cannot produce a defined derived decision value. A null value
produces `not_estimable`, never zero or a supported negative/positive conclusion.
Equality follows the declared comparator. Admission records and result packages
preserve the verified decision assessments and their current candidate identity.
Package validation checks the assessments against the retained independent verdict.

## Scientific model definition

A custom model selection must first record and review:

- Equations or algorithms, with source references and provenance status.
- Variables, units and reference scales.
- Numerical coefficients, units, source/assumption/estimate status and basis.
- Recorded source references, applicability, claim scope and question alignment.

Source references must belong to acquired evidence. The implementation preserves
the admitted definition and frozen parameter values. An absent or changed admitted
specification returns to Methods before code execution. Source-bound parameters
and declared design assumptions remain distinct. A scoped analytic pilot is not
empirical calibration or evidence for an unsupported physical mechanism.

Scientific review compares the definition with the current source and recorded
evidence, and checks decision claims against independently recalculated values.
Robustness requires evidence about the decision statistic itself. Software
selection does not require the final experiment result.

## Generation and progress observations

Scientific deliberation retains its configured reasoning policy. Configured high or xhigh
reasoning is reduced to medium for independent implementation; local source
repair uses low when reasoning is enabled. An unspecified level retains the provider default. Empty exhausted output selects disabled
reasoning on a remaining route. Explicitly disabled reasoning stays disabled.
These are task policies, not claims of empirically optimal provider settings.

Response observations record the sent reasoning setting, output reservation,
answer bytes and provider-reported thinking bytes or reasoning tokens where
available. Private reasoning text is not retained in those observations; missing
reasoning token counts are not estimated.

Sandbox receipts record source, input, stdout/stderr and, for parsed executor
output, observation and metric hashes. Evidence deltas distinguish source changes
from changed observations or computed metrics. Neither a call count nor a technical
delta establishes scientific admission.
