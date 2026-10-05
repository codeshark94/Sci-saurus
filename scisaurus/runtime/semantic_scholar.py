"""Captured Semantic Scholar Graph requests with account-wide pacing."""
from __future__ import annotations

from copy import deepcopy
import base64
import hashlib
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
import math
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import quote, urlencode, urlsplit

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.bibliographic_identity import normalize_doi
from scisaurus.runtime.literature import (
    ProviderCooldownError, _locked_provider_request, _locked_rate_state, _write_rate_state,
)
from scisaurus.runtime.retrieval import CrossrefClient, _capture, _now, _url, _set_response_read_timeout
from scisaurus.runtime.run_control import dispatch_permission, ensure_run_allowed

ADAPTER_VERSION = "1"
SCHEMA_VERSION = "semantic-scholar-papers-1"
DEFAULT_ENDPOINT = "https://api.semanticscholar.org/graph/v1"
AUTH_ENV = "SCISAURUS_SEMANTIC_SCHOLAR_API_KEY"
FIELDS = "paperId,externalIds,title,abstract,year,authors,venue,citationCount,referenceCount,openAccessPdf,url"
STATE_SCHEMA = "semantic-scholar-rate-state-1"


def _text(value, field, maximum=2048):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"{field} must be a bounded nonempty string")
    return value


def paper_id(value):
    value = _text(value, "paper_id")
    if re.fullmatch(r"[0-9a-f]{40}|CorpusId:[1-9][0-9]*|ARXIV:[^\s]+", value):
        return value
    if value.startswith("DOI:"):
        return "DOI:" + normalize_doi(value[4:])
    raise ValueError("paper_id requires a Semantic Scholar hash, CorpusId, DOI or ARXIV identifier")


def validate_arguments(value):
    if not isinstance(value, dict):
        raise ValueError("Semantic Scholar arguments must be an object")
    operation = value.get("operation")
    required = {"search": {"operation", "query"}, "batch": {"operation", "paper_ids"},
                "paper": {"operation", "paper_id"}, "references": {"operation", "paper_id"},
                "citations": {"operation", "paper_id"}}.get(operation)
    optional = {"search": {"token"}, "references": {"offset", "limit"},
                "citations": {"offset", "limit"}}.get(operation, set())
    if required is None or not required <= set(value) or set(value) - required - optional:
        raise ValueError("Semantic Scholar operation has missing or unsupported arguments")
    result = deepcopy(value)
    if operation == "search":
        _text(value["query"], "query")
        if "token" in value:
            _text(value["token"], "token", 8192)
    if "paper_id" in value:
        result["paper_id"] = paper_id(value["paper_id"])
    if operation == "batch":
        ids = value["paper_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
            raise ValueError("paper_ids must contain 1 to 500 identifiers")
        result["paper_ids"] = [paper_id(item) for item in ids]
        if len(set(result["paper_ids"])) != len(ids):
            raise ValueError("paper_ids must be unique after canonicalization")
    if operation in {"references", "citations"}:
        for name, default, minimum, maximum in (("offset", 0, 0, 1000000000), ("limit", 1000, 1, 1000)):
            result[name] = value.get(name, default)
            if type(result[name]) is not int or not minimum <= result[name] <= maximum:
                raise ValueError(f"{name} is outside the Graph API range")
    return result


def request_spec(endpoint, arguments):
    args = validate_arguments(arguments)
    operation = args["operation"]
    params = {"fields": FIELDS}
    body = None
    if operation == "search":
        path = "/paper/search/bulk"
        params.update({key: args[key] for key in ("query", "token") if key in args})
    elif operation == "batch":
        path = "/paper/batch"
        body = canonical_bytes({"ids": args["paper_ids"]})
    else:
        path = "/paper/" + quote(args["paper_id"], safe="")
        if operation in {"references", "citations"}:
            path += "/" + operation
            params.update(offset=args["offset"], limit=args["limit"])
    return ("POST" if body is not None else "GET", endpoint + path + "?" + urlencode(params), body)


def decode_response(raw):
    def pairs(items):
        if len({key for key, _ in items}) != len(items):
            raise ValueError("duplicate response keys")
        return dict(items)
    def constant(value):
        raise ValueError("nonfinite response number")
    def finite(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("nonfinite response number")
        return parsed
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite)


def project_response(payload, arguments):
    operation = arguments["operation"]
    missing = []
    pagination = {}
    if operation == "batch":
        if not isinstance(payload, list) or len(payload) != len(arguments["paper_ids"]):
            raise ValueError("batch response must retain the requested identifier positions")
        missing = [identifier for identifier, row in zip(arguments["paper_ids"], payload) if row is None]
        rows = [row for row in payload if row is not None]
    elif operation == "paper":
        rows = [payload]
    else:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Graph response requires a data array")
        rows = payload["data"]
        keys = ("token", "total") if operation == "search" else ("next", "offset")
        pagination = {key: payload[key] for key in keys if key in payload}
        if operation != "search":
            edge = "citedPaper" if operation == "references" else "citingPaper"
            if any(not isinstance(row, dict) or edge not in row for row in rows):
                raise ValueError("citation response requires paper edges")
            rows = [row[edge] for row in rows if row[edge] is not None]
    works = []
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(row.get("paperId", ""))):
            raise ValueError("paper response requires a canonical Semantic Scholar paperId")
        title = _text(row.get("title"), "paper title", 100000)
        abstract = row.get("abstract")
        if abstract is not None and not isinstance(abstract, str):
            raise ValueError("paper abstract must be a string or null")
        external = row.get("externalIds") or {}
        if not isinstance(external, dict):
            raise ValueError("externalIds must be an object")
        doi = normalize_doi(external["DOI"]) if external.get("DOI") else None
        year = row.get("year")
        if year is not None and (type(year) is not int or not 1 <= year <= 9999):
            raise ValueError("paper year must be an integer or null")
        pid = row["paperId"]
        works.append({"provider": "semantic_scholar", "work_id": "S2:" + pid,
                      "paper_id": pid, "doi": doi, "external_ids": deepcopy(external),
                      "identity_key": "doi:" + doi if doi else (
                          "arxiv:" + str(external["ArXiv"]).lower() if external.get("ArXiv") else "s2:" + pid),
                      "title": title, "abstract": abstract, "year": year,
                      "authors": deepcopy(row.get("authors") or []), "venue": row.get("venue"),
                      "citation_count": row.get("citationCount"), "reference_count": row.get("referenceCount"),
                      "source_url": "https://www.semanticscholar.org/paper/" + pid,
                      "open_access_pdf": deepcopy(row.get("openAccessPdf")),
                      "evidence_scope": "abstract" if abstract else "metadata",
                      "full_text_acquired": False})
    sources = [{"provider": work["provider"], "work_id": work["work_id"], "doi": work["doi"],
                "title": work["title"], "abstract": work["abstract"], "source_url": work["source_url"],
                "representation": "scholarly_metadata"} for work in works]
    text = "\n\n".join(work["title"] + " — " + work["source_url"] +
                        ("\n" + work["abstract"] if work["abstract"] else "") for work in works)
    return {"works": works, "sources": sources, "text": text, "missing_ids": missing, "pagination": pagination}


class SemanticScholarClient:
    def __init__(self, *, timeout=30, max_bytes=10000000, endpoint=DEFAULT_ENDPOINT,
                 auth_env=AUTH_ENV, min_interval_seconds=1, rate_state_path=None):
        for name, value in (("timeout", timeout), ("min_interval_seconds", min_interval_seconds)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if min_interval_seconds < 1:
            raise ValueError("Semantic Scholar pacing must respect the standard one-second key rate")
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        parsed = urlsplit(_url(endpoint))
        if parsed.query or parsed.fragment or endpoint.endswith("/"):
            raise ValueError("Graph endpoint must omit query, fragment and trailing slash")
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} and (parsed.scheme != "https" or parsed.hostname != "api.semanticscholar.org"):
            raise ValueError("Graph authentication requires the official HTTPS host or a loopback fixture")
        _text(auth_env, "auth_env", 256)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth_env):
            raise ValueError("auth_env must name an environment variable")
        if rate_state_path is None:
            rate_state_path = Path.home() / ".config/scisaurus/provider-state/semantic-scholar.json"
        path = Path(rate_state_path)
        if not path.is_absolute():
            raise ValueError("rate_state_path must be absolute")
        self.timeout, self.max_bytes, self.endpoint = float(timeout), max_bytes, endpoint
        self.auth_env, self.min_interval_seconds = auth_env, float(min_interval_seconds)
        self.rate_state_path = path

    def _read_state(self):
        if not self.rate_state_path.exists():
            return {"schema_version": STATE_SCHEMA, "scopes": {}}
        try:
            state = decode_response(self.rate_state_path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ValidationError("Semantic Scholar rate state is unreadable") from exc
        if (not isinstance(state, dict) or set(state) != {"schema_version", "scopes"}
                or state["schema_version"] != STATE_SCHEMA or not isinstance(state["scopes"], dict)):
            raise ValidationError("Semantic Scholar rate state has an unsupported schema")
        for scope in state["scopes"].values():
            if not isinstance(scope, dict) or set(scope) != {"next_request_epoch", "blocked_until_epoch", "failures"}:
                raise ValidationError("Semantic Scholar rate scope is malformed")
            if any(type(scope[name]) not in (int, float) or not math.isfinite(scope[name]) or scope[name] < 0
                   for name in ("next_request_epoch", "blocked_until_epoch")) or type(scope["failures"]) is not int or scope["failures"] < 0:
                raise ValidationError("Semantic Scholar rate scope is invalid")
        return state

    def run(self, **arguments):
        ensure_run_allowed()
        args = validate_arguments(arguments)
        secret = os.environ.get(self.auth_env)
        if not secret or any(ord(char) < 32 for char in secret):
            raise ValidationError("Semantic Scholar credential is unavailable or malformed")
        method, url, body = request_spec(self.endpoint, args)
        scope_key = hashlib.sha256((self.endpoint + "\0" + secret).encode()).hexdigest()
        deadline = time.monotonic() + self.timeout
        with _locked_provider_request(self.rate_state_path, deadline) as reserved:
            if not reserved:
                raise TimeoutError("Semantic Scholar account pacing lock timed out")
            with _locked_rate_state(self.rate_state_path):
                state = self._read_state()
                scope = state["scopes"].get(scope_key, {"next_request_epoch": 0, "blocked_until_epoch": 0, "failures": 0})
            delay = scope["blocked_until_epoch"] - time.time()
            if delay > 0:
                raise ProviderCooldownError("Semantic Scholar account is cooling down", retry_after_seconds=delay,
                                            rate_limit={"kind": "request_rate", "provider": "semantic_scholar"})
            delay = max(0, scope["next_request_epoch"] - time.time())
            if delay >= deadline - time.monotonic():
                raise ProviderCooldownError("Semantic Scholar pacing exceeds the request timeout", retry_after_seconds=delay)
            if delay:
                time.sleep(delay)
            ensure_run_allowed()
            parsed = urlsplit(url)
            factory = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
            connection = factory(parsed.hostname, parsed.port, timeout=max(.001, deadline - time.monotonic()))
            raw, status, headers, truncated = b"", None, {}, False
            attempts = 0
            incomplete = False
            chunks = []
            try:
                request_headers = {"Accept": "application/json", "x-api-key": secret, "User-Agent": "Sci-saurus/1"}
                if body is not None:
                    request_headers["Content-Type"] = "application/json"
                with dispatch_permission():
                    attempts = 1
                    connection.request(method, parsed.path + "?" + parsed.query, body=body, headers=request_headers)
                    scope["next_request_epoch"] = time.time() + self.min_interval_seconds
                    with _locked_rate_state(self.rate_state_path):
                        state["scopes"][scope_key] = scope
                        _write_rate_state(self.rate_state_path, state)
                response = connection.getresponse()
                status = response.status
                headers = {key.lower(): value for key, value in response.getheaders()
                           if key.lower() in {"content-type", "retry-after"}}
                chunks = []
                size = 0
                while size <= self.max_bytes:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("response deadline expired")
                    _set_response_read_timeout(response, remaining)
                    chunk = response.read1(min(65536, self.max_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                raw = b"".join(chunks)
                incomplete = bool(response.length is not None and response.length > 0)
                truncated = len(raw) > self.max_bytes
                raw = raw[:self.max_bytes]
                outcome = "provider_error" if incomplete else "ok" if status == 200 else {401: "auth_required", 403: "access_denied", 404: "not_found", 429: "rate_limited"}.get(status, "provider_error")
            except (OSError, HTTPException, TimeoutError, socket.timeout):
                raw = b"".join(chunks)
                incomplete = True
                outcome = "timeout" if time.monotonic() >= deadline else "provider_error"
            finally:
                connection.close()
            if status == 429 or (status is not None and status >= 500):
                scope["failures"] += 1
                delay = CrossrefClient._retry_after_seconds(headers) or min(3600, 60 * 2 ** min(scope["failures"] - 1, 6))
                scope["blocked_until_epoch"] = time.time() + delay
            elif status == 200:
                scope.update(failures=0, blocked_until_epoch=0)
            with _locked_rate_state(self.rate_state_path):
                state["scopes"][scope_key] = scope
                _write_rate_state(self.rate_state_path, state)
        redacted = secret.encode() in raw
        if redacted:
            raw = raw.replace(secret.encode(), b"[REDACTED]")
            outcome = "provider_error"
        captured = _capture(raw, headers.get("content-type", ""))
        result = {"outcome": outcome, "source_url": url, "capture": captured, "capture_sha256": captured["sha256"],
                  "raw_response": None, "works": [], "sources": [], "text": "", "missing_ids": [], "pagination": {},
                  "metadata": {"provider": "semantic_scholar", "transport": "http_api", "representation": "scholarly_metadata",
                               "adapter_version": ADAPTER_VERSION, "schema_version": SCHEMA_VERSION,
                               "request": args, "method": method, "http_status": status, "headers": {key: value.replace(secret, "[REDACTED]") for key, value in headers.items()},
                               "final_url": url, "capture_truncated": truncated, "capture_redacted": redacted,
                               "capture_incomplete": incomplete or status is None, "attempts": attempts,
                               "completed_at": _now()}}
        if scope["blocked_until_epoch"] > time.time():
            result["metadata"]["retry_after_seconds"] = scope["blocked_until_epoch"] - time.time()
        if outcome == "ok" and not truncated:
            try:
                payload = decode_response(raw)
                projection = project_response(payload, args)
                result.update(projection, raw_response=payload)
                result["outcome"] = "ok" if result["works"] else "empty"
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                result.update(outcome="provider_error", error="Semantic Scholar response violates the paper contract")
        elif truncated:
            result.update(outcome="provider_error", error="Semantic Scholar response exceeded the capture limit")
        elif outcome != "ok":
            result["error"] = "Semantic Scholar request did not produce a complete successful response"
        return result


def inspect_result(profile, result, params, *, representative=True):
    checks = []
    def check(name, valid):
        checks.append({"check_id": name, "outcome": "passed" if valid else "failed", "result": name.replace("-", " ")})
    metadata = result.get("metadata") or {}
    valid = False
    try:
        capture = result["capture"]
        raw = base64.b64decode(capture["body"], validate=True)
        valid = capture["encoding"] == "base64" and type(capture["bytes"]) is int and capture["bytes"] == len(raw) and hashlib.sha256(raw).hexdigest() == capture["sha256"] == result["capture_sha256"] and len(raw) <= profile["client"]["max_bytes"]
    except (KeyError, TypeError, ValueError):
        raw = b""
    check("capture-integrity", valid)
    request_valid = False
    try:
        args = validate_arguments({key: value for key, value in params.items() if key != "client"})
        method, url, _ = request_spec(profile["client"]["endpoint"], args)
        request_valid = (params.get("client", profile["client"]) == profile["client"] and metadata["request"] == args
                         and metadata["method"] == method and result["source_url"] == metadata["final_url"] == url)
    except (KeyError, TypeError, ValueError):
        args = {}
    check("request-binding", request_valid)
    check("representation", all(metadata.get(key) == value for key, value in {
        "provider": "semantic_scholar", "transport": "http_api", "representation": "scholarly_metadata",
        "adapter_version": ADAPTER_VERSION, "schema_version": SCHEMA_VERSION}.items()))
    response_valid = False
    if valid and request_valid and metadata.get("http_status") == 200:
        try:
            payload = decode_response(raw)
            projection = project_response(payload, args)
            response_valid = payload == result["raw_response"] and all(result.get(key) == value for key, value in projection.items())
        except (KeyError, TypeError, ValueError, UnicodeError):
            pass
    successful = result.get("outcome") in {"ok", "empty"}
    check("response-binding", response_valid if successful else not representative and result.get("outcome") == {401: "auth_required", 403: "access_denied", 404: "not_found", 429: "rate_limited"}.get(metadata.get("http_status"), "provider_error") and result.get("works") == [] and result.get("sources") == [] and result.get("text") == "")
    check("completeness", not any(metadata.get(key) for key in ("capture_truncated", "capture_incomplete", "capture_redacted")))
    check("usable-output", response_valid and (bool(result.get("works")) or not representative) if successful else not representative)
    return checks, {"schema_version": SCHEMA_VERSION}
