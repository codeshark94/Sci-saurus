# Ollama Cloud model wiring

Sci-saurus dispatches structured model work through Ollama's local
OpenAI-compatible bridge. The bridge is a provider adapter, not a second
inference implementation: Ollama resolves the configured `:cloud` model and
owns the provider credentials. Sci-saurus never silently falls back to a
different model.

## Provider settings

The owner-only file `local-private/ollama-cloud.env` contains model settings,
not credentials. OpenAlex authentication, when enabled, is kept separately in
`local-private/openalex.env` with mode `600`:

```bash
SCISAURUS_OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
SCISAURUS_OLLAMA_MODEL=deepseek-v4.1-flash:cloud
```

The generated run configurations use:

| Field | Value |
|---|---|
| `model.protocol` | `openai_compatible` |
| `model.base_url` | `http://127.0.0.1:11434/v1` |
| `model.model` | `deepseek-v4.1-flash:cloud` by default |
| `model.auth_env` | omitted; Ollama owns authentication |
| `model.timeout_seconds` | at least `1800` for long stages |
| `model.reasoning_effort` | `none` |
| `model.output_format` | `json_object` |

To select another Ollama Cloud alias, change `SCISAURUS_OLLAMA_MODEL` and
regenerate the private configurations:

```bash
cd "$(git rev-parse --show-toplevel)"
set -a; . local-private/ollama-cloud.env; set +a
./.venv/bin/python local-private/build_configs.py
```

Do not point a run at a local Qwen model, a retired Tailnet endpoint, or a
provider-specific model credential variable. The launchers automatically load
`local-private/openalex.env` when present; only the variable name is stored in
descriptors.

## Runtime policy

- Dispatch is configured serially (`worker_concurrency = 1`) so one long model
  request cannot create an unbounded local queue.
- Structured stages use `reasoning_effort = none` and `output_format =
  json_object`. The client rejects empty or non-JSON replies instead of
  fabricating a fallback.
- The client keeps its own timeout, byte, retry, and attempt accounting. A
  stage budget remains authoritative even when the provider retries.
- Images are sent only as bounded Base64 `data:` parts. The configured image
  and request byte limits apply before dispatch.

## Verification

Check the local bridge and the exact model through Ollama's model listing:

```bash
curl -sS http://127.0.0.1:11434/v1/models
```

Then exercise Sci-saurus's own adapter:

```bash
./.venv/bin/python - <<'PY'
from scisaurus.runtime.models import ModelClient

client = ModelClient(
    protocol="openai_compatible",
    base_url="http://127.0.0.1:11434/v1",
    model="deepseek-v4.1-flash:cloud",
    timeout_seconds=180,
    max_output_tokens=128,
    reasoning_effort="none",
    output_format="json_object",
)
result = client.complete(system="Return a JSON object.", prompt='Return exactly {"status":"ok"}.')
print(result.json_object())
print({"finish_reason": result.finish_reason, "attempts": result.request_attempts})
PY
```

## Observed model selection

- `deepseek-v4.1-flash:cloud` passed the adapter preflight and an actual long
  topic-discovery JSON call with a terminating response and valid JSON. It is
  the default for structured Sci-saurus stages.
- `glm-5.3-flash:cloud` is available through Ollama, but the strict adapter
  rejected even a short response because a `<think>` marker preceded the JSON;
  the same long structured topic/maturity workload also exhausted its
  completion budget. It is therefore not selected as a production fallback
  until it passes that workload.

These are observed executions, not latency or provider-availability
guarantees. A provider failure remains visible in the run ledger.
