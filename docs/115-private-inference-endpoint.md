# Ollama Cloud model wiring

Sci-whale dispatches structured model work through Ollama's native API or
local OpenAI-compatible bridge. Both are provider adapters: Ollama resolves
the configured `:cloud` model and
owns the provider credentials. Sci-whale never silently falls back to a
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

Model reasoning controls are provider-specific. Query `/api/show` for each
configured alias before selecting a native thinking value. The 2026-10-05
cloud metadata and role settings are:

| Model | Supported native thinking | Provider default | Role setting |
|---|---|---|---|
| GLM 5.3 Flash | `low`, `high`, `max` | `max` | `high` for scientific methods, code and validator authoring; `low` for general work |
| DeepSeek v4.1 Flash | `false`, `low`, `high`, `max` | `high` | `high` for scientific reviews |
| Gemma 4 31B | `false`, `true` | `false` | `false` for source capture, cataloging and routine bulk work |

Primary and fallback routes declare their own controls; a stronger route's
setting must not leak into a model with boolean-only controls. Native
`reasoning_effort: "none"` represents `think: false`. Dispatch events and
request receipts record the resolved protocol and reasoning setting, rather
than relying on the provider default. These records exclude authentication
fields. Figure review and scientific admission remain independently required.

The generation limit includes thinking as well as final content. Native
`done_reason: "length"` with empty final content is a truncated generation,
not a successful JSON response or a transport failure. Preserve its usage
and route it through response-contract recovery. Development execution uses
the selected route's generation ceiling and context admission; operational
execution additionally enforces per-call and cumulative role cost ceilings.
The scientific high-thinking routes reserve up to 32,768 generated tokens;
this is a maximum, not a required generation length.

- Stage descriptors can reference a shared role table with
  `"model": {"config_path": "/absolute/path/model.json"}`. This file supplies
  provider selection, role assignments, routes, and fallbacks. Call-level
  settings such as output limits can be declared alongside the reference;
  inline routing overrides are rejected. The referenced file participates in
  stage input fingerprints, and model budget owners remain cumulative.
- Native Ollama routes send `reasoning_effort` as the API's `think` control:
  `none` becomes `false`, while `low`, `medium`, and `high` are sent as named
  levels. Select a level supported by the model's `/api/show` metadata.
  Structured replies use `format: "json"`; only `message.content` is treated
  as the final response, and total generated tokens remain in usage accounting.
- Image descriptors retain their pinned SHA-256, media type, byte limits,
  and context admission checks on both transports. Native Ollama receives
  base64 images in `messages[].images`; the compatible API receives image
  content parts. The chosen model must support vision for figure reviews.
- `model.role_models` provides explicit per-role provider/model overrides.
  Qwen and Gemma are assigned to the largest bulk workload: scholarly/web
  scouting, cataloging, citation mapping, source review, prose, and surface
  editing. DeepSeek v4.1 Flash and GLM 5.3 Flash are assigned to intermediate
  planning, methods, interpretation, and ordinary review work. Evidence-
  integrating roles such as survey synthesis, gap assessment, counter-search
  and adversarial review use an Ollama-only DeepSeek/GLM Flash route with a
  separate 128K-token admission profile; they never enter the Qwen/Gemma bulk
  pool.
- Impact-only `kimi-k3:cloud` and full `glm-5.3:cloud` are opt-in escalation
  lanes for topic maturity, experiment arbitration, journal editing, and final
  arbitration. They share the durable budget key `kimi-k3+glm-5.3`, whose hard
  ceiling is 20 total provider call attempts across the whole configured
  mission. When that budget is exhausted, the declared fallback is DeepSeek;
  no undeclared paid-model failover is allowed.
- Qwen authentication is scoped to Qwen routes only; Gemma routes explicitly
  suppress inherited credentials.
- Ollama runs reserve one independent verifier slot: the active configuration
  is `concurrent_calls = 4` and `worker_concurrency = 3`. This is a bounded
  reservation, not a claim that a provider quota is currently available.
- Compatible structured stages use `reasoning_effort = none` and `output_format =
  json_object`. The client rejects empty or non-JSON replies instead of
  fabricating a fallback.
- The client keeps its own timeout, byte, retry, and attempt accounting. A
  stage budget remains authoritative even when the provider retries. Premium
  call reservations are atomic and count each retry attempt before network I/O.
- Images are sent as bounded Base64 payloads in the selected transport's
  image fields. The configured image
  and request byte limits apply before dispatch.

## Verification

Check the local bridge and the exact model through Ollama's model listing:

```bash
curl -sS http://127.0.0.1:11434/v1/models
```

Then exercise Sci-whale's own adapter:

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
  a compatible structured route; current assignments come from the shared
  role table.
- The owner-private Tailnet endpoint returned HTTP 200 for its `/v1/models`
  probe on 2026-09-15 from this host. It is available as an explicitly
  authorized weak research route for scouting and citation work; its old public
  `:8443` exposure remains retired.
- `gemma4:31b-cloud` is present in Ollama's model listing and is configured for
  low-risk roles. An earlier adapter probe returned HTTP 429 from an exhausted
  usage window; that historical response does not establish current capacity.
- On 2026-10-05, `glm-5.3-flash:cloud` declared thinking levels `low`, `high`,
  and `max`. A matched structured smoke request through the compatible route
  returned reasoning prose, while native `think: "low"` returned valid JSON.
  Actual ModelClient probes also returned valid JSON and correctly identified
  a pinned image through the native route. These probes establish transport
  behavior, not completed mission work or scientific admission.

These are observed executions, not latency or provider-availability
guarantees. A provider failure remains visible in the run ledger.
