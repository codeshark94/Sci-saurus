# Paragraph integration runtime

The first executable integration operates on one public-data paragraph and an immutable neighbor. It connects scholarly search and source capture to an explicitly configured GPU endpoint, preserves candidate versions, independently checks a repair, and adopts only an exact passing candidate. It does not release a manuscript.

## Setup

Run from the repository root with Python 3.14, Node.js, and npm available:

```bash
sh scripts/setup-runtime.sh
```

The script creates `.venv`, installs the pinned [official MCP Fetch server](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch), and installs ReadabiliPy's JavaScript dependencies from the committed lockfile. Extraction is probed before MCP starts, because first-use installation can otherwise write non-protocol output to stdout.

Copy `config/paragraph-run.example.json` to a local configuration file. The example is a synthetic sleep-study fixture, explicitly labeled in its supplied context. Configure:

| Field | Contract |
|---|---|
| `live_dispatch_allowed` | Explicitly `true` after the runtime configuration is resolved |
| `data_classification` | `public`; private input dispatch is unsupported in this slice |
| `allocation_mode` | `capacity_pool`; expenditure policies are not inferred from resource names |
| `model.protocol` | `ollama` or `openai_compatible` |
| `model.base_url` | Explicit authorized endpoint; no embedded credentials or query parameters |
| `model.model` | Exact deployed model name |
| `model.auth_env` | Environment-variable name, or `null`; never a plaintext key |
| `model.reasoning_effort` | Optional `none`, `low`, `medium`, `high`, or `xhigh` for compatible servers that support it |
| `model.output_format` | Optional `json_object` for compatible servers that support structured output |
| `model.cache_prompt` | Optional boolean provider hint for compatible servers that reuse the longest matching prompt prefix; omitted unless explicitly enabled |
| `model.temperature`, `model.top_p` | Optional provider sampling controls; Composer role defaults apply when omitted |
| `model.seed` | Optional non-negative replay seed; an autonomous Composer mission supplies and persists its exploration seed |
| `model.presence_penalty`, `model.frequency_penalty` | Optional provider repetition controls in the range `-2` to `2` |
| `model.role_profiles` | Optional map from role name to sampling overrides; this orchestration metadata is never sent to the provider |
| `mcp_fetch_command` | Executable and argument list; the example uses `.venv/bin/python -m mcp_server_fetch` |
| `public_queries`, `source_urls` | Explicit public search and fetch routes; model output cannot invoke arbitrary tools |
| `required_literals` | Baseline values or phrases that must remain present |
| `limits` | Finite rounds, request/deadline/checkpoint times, output/capture/IPC sizes |

For [Ollama's native API](https://docs.ollama.com/api/chat), the base URL excludes `/api/chat`. For a [vLLM-compatible server](https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html), provide its API base including `/v1`; the adapter appends `/chat/completions`. The protocol name does not select or authorize a different provider. No alternate endpoint is used on failure. Explicit reasoning/output-format settings are rejected for the native Ollama adapter; unset settings are omitted. `cache_prompt` is deliberately opt-in because it is provider-specific; when enabled, the request hint is sent unchanged and reported cache-read/write counters are retained in the execution artifact. vLLM's automatic prefix cache must also be enabled on the server (for example, `--enable-prefix-caching`), and `--enable-prompt-tokens-details` is required for hit/write counters; a successful request with zero counters is not treated as a cache hit. Reported completion tokens already include provider reasoning tokens and are counted once.

```bash
python3 -m scisaurus.cli run-paragraph /tmp/research-paragraph --config /path/to/run.json
python3 -m scisaurus.cli status /tmp/research-paragraph
python3 -m scisaurus.cli verify /tmp/research-paragraph
```

The project directory must be new. Existing runs remain inspectable and cannot be overwritten by this command. Exit code `0` means the exact candidate was adopted, `2` rejects configuration, and `3` reports a blocked, paused, or unresolved run.

## Execution and retained evidence

1. Publish the supplied context, acceptance criteria, baseline paragraph, and neighboring unit. Accept only this initial baseline.
2. Search [Crossref's public API](https://github.com/Crossref/rest-api-doc), preserving query records, bibliographic discoveries, reference identities, capture bytes, and hashes. Search metadata is not treated as claim evidence.
3. Execute the official MCP `initialize → initialized → tools/list → tools/call` sequence for configured source URLs. Preserve the source representation, server metadata, tool schema hash, transcript, gaps, and capture hash. Unsupported binary extraction cannot become text evidence.
4. Ask the supervisor to specify the defect and resolution condition under the supplied objective. Reserve verification capacity before requesting a producer revision.
5. Publish a scoped change request and grant. Stage the paragraph candidate without moving the accepted head. Preserve its captured-source support separately from the producer's generation record. An explicit empty support list is allowed when every assertion uses supplied facts or an interpretation permitted by the acceptance contract; external background is not an insertion quota.
6. Give the verifier a fresh model context, the baseline, candidate, acceptance contract, and separately retrieved source captures. Do not pass the producer's private conversation. Require resolution and regression check records, explicit uncertainties, and exact artifact bindings. Separate material unresolved uncertainty from nonblocking observations about style or work outside the assigned scope; retain both for inspection.
7. Require the verifier's `source-support` regression check to inspect all assertions, including external claims absent from the producer's support list. Missing or insufficient support blocks adoption; fetching a related source alone is not evidence. Require a `reader-facing` regression check to keep task history, internal claim IDs, and control instructions out of manuscript prose while allowing legitimate scientific subjects. Mechanically compare complete numeric tokens, required phrases, and the unchanged neighbor. A producer output must be a single-line paragraph. Independently passing verification permits transactional adoption; otherwise retain the candidate and baseline.
8. After a failed review, ask the supervisor whether a concrete further revision is justified. A revision decision requires a substantive change focus; a pause requires a rationale without inventing further edits. A pause decision or exhausted round/deadline limit stops production with the unresolved result visible. The supervisor cannot override failed verification or adopt a candidate directly.

The supervisor, producer, and verifier use the configured model in separate calls. The current route lists are shared, and the verifier fetches them independently. This supplies context separation and independent execution, not model diversity or an independent literature-search strategy. Broader route generation and parallel candidate portfolios remain separate milestones.

Supplied facts and the principal objective govern the supervisor's proposed resolution condition. Verification must challenge a condition that contradicts those inputs, even when the candidate follows it. Collection, measurement, processing, verification, and reporting are distinct states: unverified results cannot be described as uncollected or unmeasured. A contradictory condition remains a material uncertainty and cannot authorize adoption.

## Progress, cancellation, and accounting

The parent process emits durable checkpoints while provider work runs in isolated processes. Worker results use bounded atomic JSON files, so a worker dying during publication cannot block the deadline loop. Native request and run deadlines bound execution independently of model completion.

The runner requires the main thread to route `SIGTERM` and keyboard interruption through worker-group cleanup and task reconciliation. A dispatched call with unknown completion remains `result_unknown` and retains its reservation. A known failure before dispatch releases capacity. Termination signals do not imply remote provider cancellation; a reservation cannot be released on that assumption. Forced termination such as `SIGKILL` requires external process supervision and explicit reconciliation after restart.

Token counts are recorded only when reported. Missing dimensions remain listed under `unreported_usage`; cumulative totals cover reported actuals. A completed invocation is distinct from an accepted research result. The configured concurrent-call pool must cover a producer and an independently reserved verification call, while this first flow dispatches sequentially. It does not yet enforce mission expenditure ceilings or coordinate a shared GPU pool across multiple projects.

Automatic crash resume and unknown-call retries are unsupported. Inspect the retained task/attempt, provider execution, and source records before explicitly reconciling an interrupted run.

## Outputs and verification scope

`output/run.json` records status, exact baseline/candidate/incumbent refs, verification refs, source captures, resource accounting, blockers, and event-chain validity. Each validated verdict has separate `regressions`, `uncertainties`, and `observations` lists. An observation requires both its text and a substantive `reason_nonblocking`; its presence cannot override an unsuccessful check or a material uncertainty. The exact model verdict remains linked by `verdict_ref`. `output/report.md` displays the baseline, candidate alternatives, adoption result, source links, and review observations. All candidate versions and check evidence remain in the project artifact store.

Deterministic tests use explicitly simulated external workers for lifecycle and acceptance failure cases. Model wire tests use loopback HTTP; MCP transport tests use real subprocesses, including the installed official server for binary-extraction coverage when available. Live Crossref search, official MCP HTML capture, and full paragraph production and verification through an external GPU server have been executed. These integration results cover the synthetic fixture and do not establish general research-quality improvement.

```bash
python3 -m unittest discover -s scisaurus/tests -t . -v
```

## Live validation

On 2026-09-10, `qwen3.8-27b` completed the synthetic paragraph fixture through the compatible API with `reasoning_effort=high` and JSON-object output. The reviewed run is retained locally at `.runs/axion-20260910T121004Z` (run ID `c810b29973724c919ac042fc6cd315d1`). Its `source-manifest.json` records the exact source, fixture, and dependency-lock hashes used for execution.

| Measurement | Observed result |
|---|---|
| Final state | Exact candidate `manuscript@2` adopted after verification |
| Elapsed time | 149.38 seconds |
| Model calls | 3: supervisor, producer, verifier |
| Reported tokens | 9,022 input; 10,996 output, including provider reasoning usage |
| Live retrieval | 2 Crossref searches and 2 official MCP Fetch captures |
| Verification | 7 passing bound checks; no regressions or material uncertainties |
| Preservation | Required values retained; neighboring unit and assembly dependencies unchanged |
| Progress | 18 checkpoints; maximum observed interval 15.05 seconds, rounded upward |
| Accounting | All reservations settled; no unreported usage dimensions |
| Regression suite | 145 tests passed |

The output contains only supplied study facts and permitted interpretation, so its external-support list is explicitly empty. The verifier checked every assertion and confirmed that none depends on the captured general-background page. Retrieval evidence remains available independently of whether a source is ultimately cited.

`output/run.json` has SHA-256 `a5fa173ef969962056e5aedb0dcc236b57bdef46ff71b1d3caa8cb7f54651dd7`. Earlier blocked and calibration runs remain separate, including evidence of malformed review contracts, unnecessary citation requirements, leaked control labels, and confusion between collected and verified data. Those cases informed explicit acceptance checks; a model's passing judgment alone is not a research-quality benchmark. This validation does not cover a full manuscript, parallel candidate selection, shared capacity across projects, or dynamic Operations Cell activation. Release status remains `not_released`.
