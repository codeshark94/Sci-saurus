# Ollama Cloud model wiring

Sci-saurus dispatches structured model work through Ollama's local
OpenAI-compatible bridge. The bridge is a provider adapter, not a second
inference implementation: Ollama resolves the configured `:cloud` model and
owns the provider credentials. Sci-saurus never silently falls back to a
different model.

The owner-operated Qwen endpoint remains an authorized private alternative
when reached from an authenticated Tailnet device. The retired path was the
old public `:8443` exposure, not the private Tailnet route. The two routes
must remain separately configured and health-checked.

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

The local Ollama bridge is the default. The owner-private Tailnet endpoint may
be selected explicitly with its own `base_url`, exact model, and `auth_env`.
Never use the retired public `:8443` exposure, embed credentials in a
configuration, or silently fall back between providers. The launchers
automatically load `local-private/openalex.env` when present; only the
variable name is stored in descriptors.

## Runtime policy

- `model.role_models` provides explicit per-role provider/model overrides.
  Unlisted roles use DeepSeek; independent reviews, methods judgments, and
  adversarial checks use GLM.
- Bounded scholarly/web scouting, cataloging, and citation mapping use the
  owner-private Qwen route. Simple section and manuscript drafts use
  `gemma4:31b-cloud`.
- Qwen authentication is scoped to Qwen routes only; Gemma routes explicitly
  suppress inherited credentials.
- Ollama runs reserve one independent verifier slot: the active configuration
  is `concurrent_calls = 4` and `worker_concurrency = 3`. This is a bounded
  reservation, not a claim that a provider quota is currently available.
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
- The owner-private Tailnet endpoint returned HTTP 200 for its `/v1/models`
  probe on 2026-09-15 from this host. It is available as an explicitly
  authorized weak research route for scouting and citation work; its old public
  `:8443` exposure remains retired.
- `gemma4:31b-cloud` is present in Ollama's model listing and is configured for
  low-risk roles, but a fresh adapter probe currently returns HTTP 429 because
  Ollama's five-hour usage window is exhausted. It is not treated as accepted
  live capacity until a later probe succeeds.
- `glm-5.3-flash:cloud` is configured for independent reviews, methods
  judgments, and adversarial checks. An older probe exposed a provider
  reasoning wrapper and exhausted its completion budget; the adapter now
  accepts only the exact JSON suffix after that wrapper, but a fresh live
  GLM acceptance probe is still pending the Ollama quota reset.

These are observed executions, not latency or provider-availability
guarantees. A provider failure remains visible in the run ledger.
