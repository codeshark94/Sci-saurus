# DSH engineering batches

Sci-whale can delegate a file-based engineering work order to DSH over its
unattended JSON-RPC interface. Each batch uses one fresh session and one fixed
provider/model composition. File editing, shell execution and local repair are
owned by DSH. Sci-whale owns the job receipt, immutable inputs, admission and
independent scientific validation.

## Deployment

Build the DSH checkout's JSON-RPC runtime libraries first. Prepare a configuration
with `scripts/prepare-dsh-batch.py`: supply `--dsh-root`, `--output`, `--model`,
`--base-url`, `--auth-env`, `--max-output-tokens`, `--context-window` and
`--timeout-seconds`. `--reasoning` defaults to `off`. Credentials remain in the
named environment variable. The generated composition declares only one model,
disables provider retries and subagents, and treats token exhaustion as failure.
Runtime entry files, compiled workspace libraries, dependency lock and composition
are hash-pinned. Regenerate the configuration deliberately after upgrading DSH.

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
submitted sequentially by an external batch queue; sessions never share writable
files or conversation state.

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

The first admission adapter exports a self-contained executor. DSH may use
multiple files and install dependencies while developing, but their presence in
the work directory does not make them admitted runtime dependencies. Published
capabilities still use the configured pinned Python environment. General
multi-file capability packaging requires a separate admission contract.

The generated provider composition currently targets OpenAI-compatible chat
completions with the DeepSeek thinking dialect. A different provider's reasoning
wire contract must be explicitly configured and verified before deployment.

## Lifecycle and limitations

The batch inherits the active Sci-whale run generation. Pause or generation
replacement stops the owned runtime and descendants; the batch deadline is the
minimum of its configured time and the remaining mission time. Frozen task files
are read-only under `sandbox-exec`; only the disposable work directory is writable.
Network access is available for the provider and engineering acquisition. Inputs
should contain only the evidence needed by that worker. Validator workspaces must
never be included in the author read roots.

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
