# Owner-private inference endpoint

This deployment may dispatch model work to an owner-operated
OpenAI-compatible endpoint instead of a shared public provider. It is one
authorized `openai_compatible` provider among others; nothing in the runtime
selects or fails over to it implicitly. Missing authorization still blocks
dispatch.

The endpoint is reachable only from the owner's host PC or an authenticated
Tailscale device. The tailnet URL is not publicly routable, and public Funnel
access is disabled. The retired public `:8443` endpoint must not be used.

## Credential handling

The API key is a credential and never enters the repository, a configuration
file, a run artifact, or the event ledger. Sci-saurus names the environment
variable through `model.auth_env`; the value is read from the process
environment at call time.

```bash
cd ~/Sci-saurus
set -a; . local-private/private-qwen.env; set +a   # owner-only file, mode 600
export PATH=/opt/homebrew/bin:$PATH                # Node.js for the source extractor
```

`local-private/` is excluded through `.git/info/exclude`, so it stays outside
git without modifying a tracked `.gitignore`. Rotate the key and update only
that file when the credential changes.

## Configuration

| Field | Value |
|---|---|
| `model.protocol` | `openai_compatible` |
| `model.base_url` | Owner-private base URL including `/v1`; no embedded credentials or query parameters |
| `model.model` | Exact deployed model id (currently `qwen3.8-27b`) |
| `model.auth_env` | `SCISAURUS_QWEN_API_KEY` |
| `model.timeout_seconds` | At least `1800`; long generations exceed short defaults |
| `model.reasoning_effort` | `none` for latency, or `high` where a stage already requests reasoning |
| `model.output_format` | `json_object` for stages that already require structured output |

Set the base URL and model id in `local-private/private-qwen.env`, then
regenerate the local run configurations:

```bash
./.venv/bin/python local-private/build_configs.py
```

The helper starts from the tracked templates, runs the repository's own
`scripts/prepare-*-config.py` where one exists, and applies the endpoint,
credential variable name, a 30-minute model timeout, a matching wall clock,
and `limits.worker_concurrency = 1`. `visual-review.json` still requires a real
absolute image path before it can run.

## Runtime characteristics

- **Serial backend.** The service serves one request at a time and queues the
  rest. Keep `limits.worker_concurrency = 1` so local dispatch matches the
  backend instead of building a queue. `model_concurrency` in a Composer
  workflow already defaults to `1`.
- **Context.** The window is large but finite and shared between prompt and
  completion. Large prompts reduce the usable output length.
- **Request body.** The complete JSON body must stay below 10 MiB. Base64
  images add roughly 33 percent, so keep combined unencoded image data below
  roughly 7 MiB. Sci-saurus already enforces `max_image_bytes` and
  `max_request_bytes`.
- **Images.** Send PNG or JPEG as Base64 `data:` URIs. The gateway does not
  fetch remote `http(s)` image URLs, and the optional OpenAI `detail` field is
  not interpreted. Audio, video, and PDF inputs are unsupported.
- **Streaming.** Supported by the provider, but the Sci-saurus model client
  issues bounded non-streaming requests and enforces its own byte and deadline
  limits. Reduce `max_output_tokens` or widen the wall when a single call is
  expected to be long.
- **Structured output and tools.** OpenAI-style `response_format`, `tools`,
  and tool-result messages are supported by the provider.

## Errors

| HTTP status | Meaning | Action |
|---|---|---|
| `400` | Invalid body, unsupported content type, remote image URL, or no usable user message | Validate message roles and content parts |
| `401` | Missing or invalid key | Check the Bearer token and `model.auth_env` |
| `404` | Wrong path or model id | Use `/v1/chat/completions` and the exact model id |
| `413` | Body above 10 MiB | Reduce image or request size |
| `502` | Backend temporarily unavailable | Retry after a short delay |
| `503` | Processing queue full | Reduce concurrency and retry |
| `504` | Waited too long for model assignment | Retry or reduce concurrent load |

Transient failures should use client backoff; the model client already retries
`408`, `425`, `429`, `500`, `502`, `503`, and `504` within its deadline.

## Verification

Do not put the key on a command line that lands in shell history or logs; read
it from the environment.

```bash
# 1. Identity and context limit
curl -sS -H "Authorization: Bearer $SCISAURUS_QWEN_API_KEY" \
  "$SCISAURUS_QWEN_BASE_URL/models"

# 2. Sci-saurus's own adapter
SCISAURUS_QWEN_API_KEY="$SCISAURUS_QWEN_API_KEY" ./.venv/bin/python - <<'PY'
from scisaurus.runtime.models import ModelClient
client = ModelClient(
    protocol="openai_compatible",
    base_url=__import__("os").environ["SCISAURUS_QWEN_BASE_URL"],
    model=__import__("os").environ["SCISAURUS_QWEN_MODEL"],
    timeout_seconds=1800, max_output_tokens=128,
    auth_env="SCISAURUS_QWEN_API_KEY", reasoning_effort="none")
print(client.complete(system="Answer concisely.", prompt="Reply with exactly: OK").text)
PY
```

### Verification log

- **2026-09-14, endpoint probe** — from the owner's Mac over Tailscale:
  `/v1/models` returned HTTP 200 listing the configured model with
  `max_model_len = 163840`; a non-streaming chat completion with
  `reasoning_effort = "none"` returned HTTP 200; Sci-saurus's `ModelClient`
  completed against the endpoint and reported `finish_reason = stop`. The
  loopback base URL was not reachable from this device, so the Tailnet base URL
  is the correct value here.
- **2026-09-14, reasoning-mode finding** — with reasoning unspecified (the
  provider default), the backend spent the whole token budget in
  `message.reasoning` and returned an **empty** `message.content` with
  `finish_reason = "length"`. Sci-saurus correctly rejected that as an
  incomplete response. Private-endpoint configurations must therefore set
  `reasoning_effort: "none"`; raising it again requires raising
  `max_output_tokens` together so the reasoning pass cannot starve the reply.
- **2026-09-14, strict-output finding** — with reasoning disabled, the
  independent verifier stage still returned a reply that was not a complete
  JSON object, because every Sci-saurus model stage parses strict JSON through
  `ModelResult.json_object()`. Setting `output_format: "json_object"` makes the
  provider constrain the reply. Both values are applied by
  `local-private/build_configs.py`.
- **2026-09-14, live acceptance run** — `run-paragraph` on the public synthetic
  sleep fixture completed end to end against the private endpoint: Crossref
  search, official MCP Fetch captures, supervised write, exact candidate
  staging, and independent verification. The run was **accepted** in 27.67
  seconds (exit 0) and adopted `artifact:strategy/documents/manuscript@2`,
  which removed the unsupported long-term-consolidation and exam-scheduling
  claims. This is one observed execution, not a latency guarantee.
- Provider-side per-minute limits and queue depth are not independently
  measured by this repository; observed behavior remains provider-reported.
