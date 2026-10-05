# Scientific software runtime

Experiment authoring begins with a Methods software assessment. The agent
searches public repositories from the research question, reads the selected
source and documentation, checks scientific applicability, and chooses reuse,
a source-supported custom model, or an explicit unavailable result.

## Execution flow

1. Probe the host runtime and deny-by-default sandbox.
2. Search, inspect an exact commit, and read its license and example.
3. Acquire source into a project-private environment and install or build it.
4. Reproduce a documented example with explicit numerical tolerances.
5. Execute computations for the current question, source data and work orders.
6. Have a separate reviewer inspect the source, inputs, outputs, license,
   dependencies, applicability and adapter behavior.
7. Bind admitted computations to the generated experiment's frozen input.

Tool responses return to the same producer. Installation, execution and parsing
errors retain stdout, stderr, exit status and input identity. Producers can
correct their calls or choose another implementation from the observed error.
Reviewers receive evidence and cannot execute software tools.

## Supported environments

| Runtime | Provisioning | Execution |
| --- | --- | --- |
| Python | Private virtual environment; exact PyPI requirements; recorded wheel hashes; offline package build and install | Isolated interpreter with JSON stdin/stdout |
| R | Private package library; separately inspected source dependency receipts | Rscript with the private library and the host base library |
| C, C++, Fortran | Private CMake, Make or configure build; recursively verified native dependency environments | Python adapter invoking the pinned executable in the same sandbox |

Network source retrieval does not execute repository code. The trusted Python
wheel downloader uses the public PyPI index. Installation hooks, builds and
programs execute without network access or inherited credentials. Global
installation is unavailable. Missing compilers, MPI, GPU or other prerequisites
are reported; presence of a command alone does not establish readiness.

The controller collects the environment receipt before the assessor's first
model call. It records logical and physical CPU capacity, load, physical
memory and reclaimable-memory estimates, workspace filesystem capacity,
and macOS GPU/Metal inventory. A sandboxed baseline measures a
single-process math workload and small file write/fsync/cached-read times.
These observations are not solver throughput predictions. Acquired examples
and computations record their measured wall duration separately. The producer
and reviewer assess compatibility and feasible experiment scale from both.
Repository discovery exposes GitHub's actual field semantics: default name,
description and topics, with `in:readme` for documentation. Search receipts
retain the exact query and coverage limits; an empty result does not establish
that no scientifically suitable software exists.
GPU scientific runtime readiness requires a candidate-specific execution;
GPU inventory alone is insufficient. Requested POSIX limits and observed child
soft/hard limits are reported separately alongside host capacity and the
parent's output/wall bounds. Null child limits mean unlimited; OS limits may
be unsupported or clipped by inherited limits. POSIX limits apply per process
and do not reserve or bound aggregate job resources. Successful host checks
remain receipt-bound inputs for experiment implementation as well as review.

Source archives must contain regular files and directories with safe relative
paths. Archive links, submodule retrieval, containers, distributed execution and
remote HPC provisioning are not supported by this adapter.

## Evidence and reuse

`scientific-software/receipts` stores content-addressed action results. An
environment binds the commit, archive, license, interpreter, package inventory,
installed files and dependency receipts. Builds and execution recursively
verify dependency environments; cached executions also verify the current
environment before returning an old result.

Assessment identity includes the question, prior-work challenge, current
source-data manifest, executable work orders, study type and quality contract.
New factual rows or a new computation scope require another assessment.
Matching acquisition and execution actions reuse verified receipts. Identical
failed actions cannot manufacture progress. Interrupted operations remain
unknown rather than being silently redispatched.

The producer and reviewer share the parent experiment's durable model budget.
An auxiliary assignment records its budget owner before dispatch and invoices
actual usage once. Development execution preserves usage without adding a cost
ceiling. The remaining stage capacity governs tool operations independently of
the program-authoring call ceiling. When a stage declares no call quota, the
tool producer records a null call ceiling and remains bounded by the original
deadline, configured token limits and any shared provider budget.

## Scientific admission

Installing software or matching an upstream example establishes operational
reproduction. It does not establish scientific fitness or an experimental
conclusion. The independent Methods reviewer must check species, mechanism,
units, conventions, calibration domain and the adapter's actual use of the
package. A custom model needs successful discovery and scientific sources; a
failed search is not evidence that no suitable implementation exists.

Admitted software outputs become source-bound inputs to the generated
experiment. New parameters require new recorded software computations. This
does not grant generated programs unrestricted imports or subprocess access.
Ordinary experiment replay, separately authored recalculation, methodological
review and scientific admission remain mandatory.
