# Sci-saurus — Active Web Intelligence and Integrations

> **Version:** v0.8 · **Date:** 2026-09-10 · **Status:** implementation design; adapters and live credentials are not provided by this document.
> Implements SSOT D22–D24 and D34. Shared records follow [Execution Contract](40-execution-contract.md); artifact modifications follow [Change Control](45-artifact-change-control.md).

Required installation, adapter work, service setup, and operational repair are performed by the project's on-demand [Operations Cell](15-project-organization.md). Existing enabled capabilities remain directly callable by authorized departments.

## 1. Operating policy

The web is an active source of evidence throughout the mission. Search begins when a material question, proposed claim, method assumption, novelty judgment, venue requirement, contradiction, source update, or stalled investigation would benefit from external information. Departments and adversaries can initiate those activities under existing delegation; they do not wait for a dedicated survey stage to reopen.

Use compute to generate and assess different query families, follow primary sources deeply, examine original-language and historical terminology, compare competing explanations, and investigate counterexamples. Follow promising citations, authors, institutions, repositories, and datasets when they can change a decision. Broad exploration and independent retrieval remain available after a first plausible answer appears.

Expected information value and coverage gaps govern continuation. Provider access and query limits still apply across the worker pool; abundant GPUs cannot remove an external API's rate limit. A missing source or tool is an explicit capability/evidence gap that can trigger another authorized route or a concrete integration task.

## 2. Tools, APIs, and plugins share an execution boundary

Prefer an existing working capability, then implement a direct API adapter where it gives reliable structured access, and use a connected plugin/MCP service when its distinctive corpus or operations materially help. Different transports use the same task, policy, provenance, failure, and usage contracts. No plugin output is automatically more authoritative than a direct response.

A tool available in the current authoring environment is not automatically installed or callable in the deployed Sci-saurus runtime. An integration plan distinguishes the service API, client implementation, actual deployment binding, credentials, authorized data, and successful capability check. Discovery of a plugin or registry listing does not establish any of those later states.

An already authorized public retrieval capability can be used without repeated human confirmation. A new integration can be developed and tested under existing development/tool delegation. Installation, account connection, paid expenditure, and protected-data transfer follow actual authority; broad integration ambition does not invent credentials or bypass those boundaries.

## 3. Capability records and lifecycle

A versioned capability profile records provider/service identity, transport (`http_api`, `browser`, `mcp`, or supported host tool), supported operations, endpoint/schema/protocol versions, authentication mechanism, permitted source classes, data handling, quota model, request/capture limits, freshness policy, and verification evidence. Profiles are artifacts in the existing store; the registry is their derived catalog.

A deployment binding joins a profile to a credential reference, environment, allowed operations/data, resource policy, and health state. Credentials remain outside artifacts and prompts. States distinguish `discovered`, `configured`, `verified`, `enabled`, `degraded`, and `disabled`. Enabling requires both authority and a successful scoped probe; an authenticated but unauthorized capability stays unavailable to that mission.

Bindings and credentials are project-scoped. Reuse of a public profile or installed server binary does not enable another project's binding or expose its sources, private cache, or session. Shared provider quotas remain globally accounted for without merging project data.

Probe actual schema compatibility, a representative read, pagination/completeness, rate responses, and source access. Re-probe after schema/protocol or credential-scope changes. Untrusted server descriptions and tool annotations cannot grant authority. Degradation can route to another already authorized capability, with the changed route and gaps recorded; it cannot silently report an equivalent completed search.

## 4. Normalized adapter contract

Initial operation vocabulary:

```text
search              resolve_identity       fetch_source
follow_links        citation_neighbors     check_updates
```

A profile advertises only the operations it implements. A service without full-text access must not present its summaries as `fetch_source` success for a full-text requirement.

Each request contains request ID, task/attempt and campaign/generation refs, operation, purpose/query family, exact target refs or query, language/date/source constraints, pagination cursor, capability binding, outbound-data classification, and reservation/deadline refs.

Each response records provider request ID where available, profile/adapter/schema versions, start/end times, outcome, raw response/capture ref and hash where retention is permitted, normalized discoveries/captures, pagination and completeness, observed quota/usage/cost, retry conditions, and access/extraction gaps. If raw retention is disallowed, retain permitted metadata and declare the reproducibility limitation.

Outcomes are `ok`, `empty`, `partial`, `rate_limited`, `auth_required`, `access_denied`, `not_found`, `timeout`, `provider_error`, `parse_error`, or `unsupported_capability`. The adapter maps these into the shared QueryRecord outcome while retaining the precise cause. `empty` is a successful search with no matches; it cannot represent a 429 response, missing credentials, unsupported operation, or a parsing failure.

The scheduler coordinates provider/account/operator-wide concurrency and rate limits, not just limits per worker. It deduplicates active identical retrievals, keys caches by provider/version/query/access scope, honors cache freshness, and uses finite retry/backoff within the task deadline. Old cached content cannot satisfy a current-status requirement without a valid freshness check. Unknown completion remains accountable under the existing reservation contract.

Transport redirects and acquired content are validated under the authorized fetch policy. Returned HTML, documents, repositories, or MCP output are source data, never task instructions. Cancelled/stale tasks may retain a late result for audit, but it cannot advance evidence, coverage, or manuscript state automatically.

## 5. Initial integration matrix

These are proposed implementation choices based on official documentation checked on 2026-09-10. Authentication and limits are deployment configuration, verified against current provider documentation, account plan, and response headers; numeric quotas are not permanent design constants.

| Need | Initial API/adapter | What it establishes and what remains to verify |
|---|---|---|
| DOI identity and bibliography | [Crossref REST](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/) | Public metadata access and optional identified access pool. Metadata/reference lists help identity and discovery; they do not establish claim support. Observe pool-specific rate/concurrency headers |
| Broad scholarly discovery and citation/author relations | [OpenAlex](https://help.openalex.org/api/authentication/) | Scholarly graph and search routes. Basic keyless access and key-backed capacity depend on the current service policy; configure credential references and inspect usage headers |
| Independent scholarly route | [Semantic Scholar Graph](https://www.semanticscholar.org/product/api) | Search and citation graph enrichment. Public access shares capacity; key-based limits differ. A separate index is an alternative route, not proof of statistically independent evidence |
| Open-access source location | [Unpaywall](https://data.unpaywall.org/products/api) | DOI lookup returns available OA locations and requires a contact email. The source fetcher must still acquire and inspect the actual authorized representation |
| Preprint search and versioned sources | [arXiv API](https://info.arxiv.org/help/api/user-manual.html) | Search metadata and versioned source discovery. Apply [arXiv's API access limits](https://info.arxiv.org/help/api/tou.html) across all workers/machines operated by the system |
| General and original-language web search | [Brave Search API](https://api-dashboard.search.brave.com/api-reference/web/search/get) | A concrete initial general-search adapter with a configured subscription token. Honor [plan and rate limits](https://api-dashboard.search.brave.com/documentation/guides/rate-limiting); snippets remain discoveries until source inspection |
| Code, methods documentation, issues, and reproducibility material | [GitHub REST](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) | Public/authenticated access and search-specific limits differ. Code search may require authentication. Capture repository content at a commit SHA with path/locator, not only a mutable branch URL |
| Useful additional service tools | [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) | Discover schemas through tool listing and normalize tool-call outputs. Pin a supported protocol/schema and verify the deployed server; a [registry listing](https://registry.modelcontextprotocol.io/docs) is discovery metadata, not installation or execution approval |

Start with a small working set and add another provider when it fills a coverage or capability gap. The general-search adapter and source acquisition must be functional early; a metadata-only stack does not meet the active-web requirement. Browser acquisition handles authorized dynamic pages when simple HTTP extraction is insufficient, with access failures kept explicit.

## 6. From retrieval to evidence to a local repair

```text
material question / source-update signal
    → SearchCampaign and independent query routes
    → QueryRecords and DiscoveryRecords
    → ReferenceCard and exact SourceCapture
    → verified EvidenceRecord and claim impact
    → scoped ChangeRequest and unit-specific EditGrants
    → ChangeSets and independent verification
    → atomic DocumentManifest acceptance
```

Acquisition records whether the accessible material is full text, abstract, metadata, snippet, or partial, along with exact source version, retrieval time, locator, extraction method, and retention/access limits. A full-text-required check remains incomplete when only an abstract is available. Citation chasing records direction, seed, depth/frontier, and unavailable branches rather than just accumulating hit counts.

Source corrections, retractions, new editions, and changed repository commits create new records and targeted investigation events. Source publication time, provider metadata-update time, and retrieval time are distinct. A preprint and journal article may be related versions with different claims; linking their identities cannot assert equivalent content.

Impact analysis maps the affected evidence to exact Claim and ContentUnit versions, including captions, tables, and shared definitions. The owning department then proposes a purpose-scoped repair, supported rebuttal, or evidence-gap report. New search results cannot rewrite a manuscript, replace a cited source, or mark a claim accepted directly. Independent adversaries can discover and challenge the evidence-promotion decision itself.

## 7. First implementation and operational evidence

P1 includes the adapter interface, capability binding, pool-wide limiting, explicit outcomes, immutable source capture, and one working retrieval-to-evidence path. Implement Crossref/OpenAlex identity/discovery and HTTP capture, then connect general web search and a browser capture path as part of the same initial capability milestone. If a selected service is unconfigured, expose that gap and use only an authorized alternative; do not report a completed general-web capability from scholarly metadata alone.

P2 adds OA lookup, arXiv and citation/author chasing, GitHub, independent scholarly search, and the first useful MCP service when it supplies an otherwise missing capability. These are mission tools, not deferred general interoperability research. P6 remains reserved for broad cross-agent interoperability and organization-wide process reuse.

Adapter acceptance requires a real authorized request and inspected captured content, alongside deterministic failure fixtures. Trace one retrieved fact through source/citation identity, exact evidence location, claim assessment, and a paragraph-scoped repair. Demonstrate failed access, rate limiting, incomplete pagination, stale source updates, and late cancelled responses. Report actual provider/model usage and human configuration interventions.

Measure completed coverage routes, primary-source acquisition, independent counterevidence, promotion precision, freshness, duplicate information, and time to a decision-relevant source. Tool count, searches per second, and downloaded bytes are diagnostics. Neither a plugin installation nor a large result list counts as research progress.
