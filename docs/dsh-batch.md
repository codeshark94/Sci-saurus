# DSH engineering batches

Sci-whale can delegate a file-based engineering work order to DSH over its
unattended JSON-RPC interface. A project development session uses one fixed
provider/model composition. File editing, shell execution and local repair are
owned by DSH. Sci-whale owns the job receipt, immutable inputs, admission and
independent scientific validation.

## Deployment

Project continuation requires the SDK's explicit `session/resume` interface.
The compatible patch is [session-resume.patch](../scripts/dsh/session-resume.patch),
based on DSH commit `cffa619a9a861ca305e6d64ba8ee29afc0630550` (MIT license).
In that checkout, inspect and apply the patch with `git apply --check` followed
by `git apply`; other revisions require a reviewed adaptation. Run the SDK
client/server tests and build with `pnpm run build:lib` before preparing pins.
Do not rebuild a pinned backend while a managed worker is running.

Build the DSH checkout's JSON-RPC runtime libraries first. Prepare a configuration
with `scripts/prepare-dsh-batch.py`: supply `--dsh-root`, `--output`, `--model`,
`--base-url`, `--auth-env`, `--max-output-tokens`, `--context-window` and
`--timeout-seconds`. `--reasoning` defaults to `off`. `--vision` declares image
input and enables the local attachment service used by `read_image`; enable it
only for a verified image-capable provider route. Credentials remain in the
named environment variable. The generated composition declares only one model,
disables provider retries and subagents, and treats token exhaustion as failure.
Runtime entry files, compiled workspace libraries, dependency lock and composition
are hash-pinned. Regenerate the configuration deliberately after upgrading DSH.
Configuration inspection validates declarations; the explicit check and every
batch dispatch verify all pinned file contents before creating a job.

Check without agent dispatch:

```sh
./sci-whale run-engineering-batch \
  --config /absolute/path/dsh-batch.json --job /absolute/path/job.json \
  --output /absolute/path/batches --check
```

A work order has exactly `task` (text), `inputs` (relative filenames to text),
`seed_files` (initial writable files), and `outputs` (required relative filenames).
Omit `--check` to execute it. The controller stores the JSON-RPC event stream,
stderr, source/input/output hashes, token usage and terminal receipt beside a
disposable writable workspace. `model_calls` counts started model steps, including
pre-dispatch preparation failures; it is not a billing or HTTP-request count.
No GUI is required. Multiple work orders can be
submitted sequentially by an external batch queue. Standalone batches start
fresh; managed development assignments use the project history described below.

## Project development history

Managed concept production, source-definition repair, software selection and
executor authoring use one serialized project session and writable workspace.
Each assignment still has its own immutable inputs, output archive, transport
receipt and usage delta. The SDK's explicit `session/resume` restores persisted
history after a process boundary; reusing a session ID without resuming is not
equivalent. The project manifest pins backend configuration and every settled
receipt. Altered inputs or archived outputs, another project owner, configuration
drift and unsettled dispatches block continuation.

Only prior producer evidence is added to the worker's read roots. Independent
validator sessions receive their blinded assignments in separate workspaces and
never receive the development handle. Scientific reviews remain independent.
An interrupted dispatch preserves its unknown outcome and blocks automatic
continuation; explicit reconciliation must establish its settled ownership before
the history can advance. No stage transition refunds or recounts earlier usage.

## Production responsibilities

With a pinned laboratory author backend, DSH produces concept comparisons,
source-definition repairs, scientific software and Methods repair plans as
file-based assignments. Ordinary models coordinate and independently review.
The controller validates returned files against the current assignment and retains
its receipt; it does not fill missing scientific values or rewrite agent results.
A definition repair preserves topic identity and existing literature. A completed
response-format repair closes separately from any remaining scientific hold.

Scientific software controller operations continue within the same DSH session,
workspace and dispatch slot. Each completed turn exports either a tool request or
the final structured selection. The controller executes the declared operation,
then provides a new read-only assignment containing its bound receipts. Workspace
scripts and session history survive the exchange and subsequent development
assignments; validator sessions remain isolated.
Final selection contract corrections also continue in that session. Each distinct
diagnostic permits one correction; an unchanged failure terminates with its paid
response and usage retained. Corrections preserve the current scientific evidence
and restore the canonical assignment when another tool observation is requested.

Controller sessions use `dsh-controller-session-receipt-1`. Their receipts retain
each message owner, immutable assignment and task hashes, archived output bytes
and cumulative paid usage. Previous response files are removed before the next
turn. Single-turn jobs keep `dsh-batch-receipt-1`. A settled malformed response
remains a known transport result; an interrupted subsequent turn remains unknown.
Tool ownership, duplicate-action checks and final selection admission use the
same controller contracts as ordinary model producers.

## Foundry integration

Set `author_backend` in `capability-foundry-config-1` configuration to the generated JSON
configuration object. The author writes `executor.py` and `intent.json` rather
than escaped code in a chat response. The controller imports those files, then
runs the existing static checks, exact-input sandbox and deterministic replay.
A separate fresh DSH session authors `validator.py` from the frozen intent,
stdin/output contracts and raw row samples, without the executor implementation
or reported calculation values. The controller executes its recalculation against
the current complete raw output and commissions scientific reviews. Scientific failures can
generate a new work order with current evidence; transport failures stop at the
retained receipt instead of entering a JSON continuation or model-fallback loop.

Scientific reviews receive every observation as an ordered, lossless row table.
The projection declares its column schemas, missing-field semantics, row count
and canonical observations digest. Candidate and execution digests continue to
identify the original output. Review cache identity includes the projection;
changing its representation does not admit a scientific result.

Unobserved dispatch reservations remain conservatively charged. A proven local
rejection can release its reservation only through controller-owned immutable
request history and the matching paid Composer checkpoint. Observed or unknown
dispatch outcomes cannot be refunded. Provider retries retain their physical
attempt counts even when followed by a rate limit or pause.

The first admission adapter exports a self-contained executor. DSH may use
multiple files and install dependencies while developing, but their presence in
the work directory does not make them admitted runtime dependencies. Published
capabilities still use the configured pinned Python environment. General
multi-file capability packaging requires a separate admission contract.

The generated provider composition currently targets OpenAI-compatible chat
completions with the DeepSeek thinking dialect. A different provider's reasoning
wire contract must be explicitly configured and verified before deployment.

## Lifecycle and limitations

All Sci-whale model clients and DSH batches share three host-wide dispatch
slots across threads and processes. A single-session DSH batch holds one slot
until its process tree has been reaped, including tool execution between model
steps. This conservatively reserves capacity without admitting more than three
concurrent workers. Waiting does not consume a model call and remains subject
to pause and the original deadline. DSH inherits its slot descriptor, retaining
the reservation if its controller exits before the child. Kernel locks release
when the last owning descriptor closes; keep
`~/.local/state/sci-whale/model-slots` intact while workers run.

The batch inherits the active Sci-whale run generation. Pause or generation
replacement stops the owned runtime and descendants; the batch deadline is the
minimum of its configured time and the remaining mission time. Frozen task files
are read-only under `sandbox-exec`; only the disposable work directory is writable.
Network access is available for the provider and engineering acquisition. Inputs
should contain only the evidence needed by that worker. Validator workspaces must
never be included in the author read roots.

Durable image attachments require syncing their directory entries. The DSH
sandbox permits read-only handles to exact ancestor directories for that
operation; it does not grant reads of files or writes outside the workspace.
Visual inspection supports geometry, mesh and figure diagnostics. Quantitative
material fractions and scientific outcomes still require solver-bound raw data
and independent recalculation.

Successful SDK transport and an agent's final answer do not establish scientific
success. Missing output, token exhaustion, transport interruption and gate rejection
remain separate outcomes. Interrupted jobs preserve partial usage and unknown
outcomes; they are not automatically redispatched. Standalone CLI callers must
provide an execution grant in `SCISAURUS_RUN_CONTROL` and
`SCISAURUS_RUN_GENERATION` when operating a managed mission.

An explicit per-model-call allowance is currently refused by the Foundry adapter:
DSH owns multiple calls within one job, and a controller-enforced provider relay is
needed to enforce such an allowance before each dispatch. Existing allowances are
never silently relaxed. No new cost cap is imposed by this backend.
