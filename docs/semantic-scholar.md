# Semantic Scholar Graph adapter

The `semantic_scholar` Operations adapter exposes the Academic Graph API alongside
OpenAlex and Crossref. Its default endpoint is
`https://api.semanticscholar.org/graph/v1`. Credentials are resolved from
`SCISAURUS_SEMANTIC_SCHOLAR_API_KEY` and sent only in the `x-api-key` header.
Owner-local Composer runs load `~/.config/scisaurus/semantic-scholar.env` when it
exists. Credential files must remain outside artifact identities and repositories.

## Operations

The configured client accepts `timeout`, `max_bytes`, `endpoint`, `auth_env`,
`min_interval_seconds` and an absolute `rate_state_path`. The defaults use the
standard one-second key rate and a shared owner-local account ledger. Production
requests require the official HTTPS host; local HTTP is limited to loopback.

Argument objects select one operation:

| Operation | Required arguments | Optional arguments |
| --- | --- | --- |
| `search` | `query` | `token` |
| `batch` | `paper_ids` | none |
| `paper` | `paper_id` | none |
| `references` | `paper_id` | `offset`, `limit` |
| `citations` | `paper_id` | `offset`, `limit` |

Search uses `/paper/search/bulk` and retains the provider continuation token.
Batch lookup submits up to 500 distinct identifiers in one POST and preserves
missing identifier positions. IDs accept Semantic Scholar paper hashes,
`CorpusId:`, `DOI:` and `ARXIV:` forms. Reference and citation pages preserve their
edge direction and continuation offset.

Register the adapter through `OperationsCell.register`, then probe and independently
verify it through the existing Operations lifecycle before execution. Profiles
store environment variable names, never credential values. The execution worker
supports the corresponding `semantic_scholar` dispatch kind. Registration and an
available credential do not establish live API readiness.

## Evidence and provider limits

Exact response bytes, SHA-256, request arguments, HTTP status and pagination remain
in the execution result. Successful rows retain their native `S2:` identifier and
canonical DOI identity key. DOI matching permits linking to OpenAlex records while
preserving both observations and any disagreement; titles alone do not establish
identity. The adapter does not manufacture OpenAlex identifiers.

Abstracts remain abstract evidence. An `openAccessPdf` location is a retrieval lead,
not verified full text. Empty searches, inaccessible papers and authentication
failures remain explicit and do not produce fabricated sources.

All operations share pacing and cooldown state for the same endpoint and key.
HTTP 429 respects `Retry-After`; absent that header, successive failures use
increasing cooldowns. Server failures also receive a cooldown. The adapter does
not retry anonymously or issue inline request loops. Managed execution checks
stop permission before submission. Cached provider cooldown blocks dispatch even
from a separately instantiated client.

The installed survey's immutable bibliography contract still selects OpenAlex.
Adding this adapter does not rewrite existing mission inputs or automatically
repeat completed searches. A workflow must explicitly select the complementary
route through Operations before consuming its results.

Protocol reference: [Semantic Scholar API tutorial](https://webflow.semanticscholar.org/product/api/tutorial).
