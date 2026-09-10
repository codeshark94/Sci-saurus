"""Bounded OpenAlex acquisitions of scholarly metadata and citation links.

Each call retrieves one page or one work. Locations and reconstructed abstracts
remain metadata; neither proves that source full text has been acquired.
"""
from __future__ import annotations

from http.client import HTTPConnection, HTTPSConnection, HTTPException, IncompleteRead
import json
import math
import os
import queue
import re
import socket
import threading
import time
from urllib.parse import urlencode, urlsplit

from scisaurus.runtime.retrieval import _capture, _limits, _now, _url


ADAPTER_VERSION = "1"
SCHEMA_VERSION = "openalex-works-v1"
DEFAULT_ENDPOINT = "https://api.openalex.org/works"
# Provider request-URL limit: https://help.openalex.org/api/searching/
MAX_REQUEST_URL_BYTES = 4094
SEARCH_SYNTAX = "OpenAlex stemmed search: use search terms or quoted phrases, without '*' or '?' wildcards. The percent-encoded request URL must fit 4094 bytes."
ARGUMENT_KEYS = {"operation", "query", "work_id", "limit", "cursor"}


def work_id(value):
    """Canonical short work identifier, preserving identity across URL forms."""
    if not isinstance(value, str):
        raise ValueError("OpenAlex work ID must be a string")
    match = re.fullmatch(r"(?:https://openalex\.org/(?:works/)?|works/)?(W[1-9][0-9]*)", value)
    if not match:
        raise ValueError("OpenAlex work ID must be a W identifier or its OpenAlex URL")
    return match[1]


def validate_arguments(arguments):
    if not isinstance(arguments, dict) or set(arguments) != ARGUMENT_KEYS:
        raise ValueError("OpenAlex arguments require exactly operation, query, work_id, limit and cursor")
    operation, query, identity, limit, cursor = (arguments[key] for key in
                                               ("operation", "query", "work_id", "limit", "cursor"))
    if operation not in ("search", "work", "citing"):
        raise ValueError("OpenAlex operation must be search, work or citing")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("OpenAlex limit must be an integer between 1 and 100")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 8192
                               or any(ord(c) < 32 for c in cursor)):
        raise ValueError("OpenAlex cursor must be a bounded nonempty string or null")
    if operation == "search":
        if (not isinstance(query, str) or not query.strip() or len(query) > 2048
                or any(ord(c) < 32 for c in query) or identity is not None):
            raise ValueError("OpenAlex search requires a bounded query and null work_id")
        if "*" in query or "?" in query:
            raise ValueError("OpenAlex stemmed search requires a query without '*' or '?' wildcards")
    else:
        if query is not None:
            raise ValueError("OpenAlex work and citing operations require null query")
        identity = work_id(identity)
        if operation == "work" and cursor is not None:
            raise ValueError("OpenAlex work operation requires null cursor")
    normalized = {"operation": operation, "query": query, "work_id": identity, "limit": limit, "cursor": cursor}
    request_url(DEFAULT_ENDPOINT, normalized)
    return normalized


def request_url(endpoint, arguments):
    if arguments["operation"] == "work":
        url = endpoint + "/" + arguments["work_id"]
    else:
        params = {"per_page": arguments["limit"], "cursor": arguments["cursor"] or "*"}
        if arguments["operation"] == "search":
            params["search"] = arguments["query"]
        else:
            params["filter"] = "cites:" + arguments["work_id"]
        url = endpoint + "?" + urlencode(params)
    if len(url.encode("utf-8")) > MAX_REQUEST_URL_BYTES:
        raise ValueError("OpenAlex percent-encoded request URL exceeds the 4094-byte limit")
    return url


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("OpenAlex response contains duplicate JSON keys")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("OpenAlex response contains nonfinite JSON numbers")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("OpenAlex response contains an overflowing JSON number")
    return number


def _resolve(address, deadline):
    """Bound DNS waiting without allowing a late lookup to issue an HTTP request."""
    replies = queue.Queue(maxsize=1)

    def lookup():
        try:
            replies.put((socket.getaddrinfo(*address, type=socket.SOCK_STREAM), None))
        except OSError as exc:
            replies.put((None, exc))

    threading.Thread(target=lookup, daemon=True).start()
    try:
        addresses, error = replies.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty as exc:
        raise TimeoutError("OpenAlex DNS deadline exceeded") from exc
    if error is not None:
        raise error
    return addresses


def _abstract(index):
    if index is None:
        return None
    if not isinstance(index, dict) or not index:
        raise ValueError("OpenAlex abstract index must be a nonempty object or null")
    positions = {}
    for token, offsets in index.items():
        if not isinstance(token, str) or not token.strip() or not isinstance(offsets, list) or not offsets:
            raise ValueError("OpenAlex abstract contains an invalid token or positions list")
        for offset in offsets:
            if type(offset) is not int or offset < 0 or offset in positions:
                raise ValueError("OpenAlex abstract positions must be distinct nonnegative integers")
            positions[offset] = token
    if sorted(positions) != list(range(len(positions))):
        raise ValueError("OpenAlex abstract positions are incomplete")
    return " ".join(positions[i] for i in range(len(positions)))


def _doi(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("OpenAlex DOI must be a string or null")
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    if not re.fullmatch(r"10\.[0-9]+/\S+", value):
        raise ValueError("OpenAlex DOI is malformed")
    return value.lower()


def normalize_work(item):
    if not isinstance(item, dict):
        raise ValueError("OpenAlex work must be an object")
    required = {"id", "title", "publication_year", "referenced_works", "related_works", "locations"}
    if not required.issubset(item):
        raise ValueError("OpenAlex work is missing required metadata fields")
    if not isinstance(item["title"], str) or not item["title"].strip():
        raise ValueError("OpenAlex work title must be a nonempty string")
    year = item["publication_year"]
    if year is not None and (type(year) is not int or not 1 <= year <= 9999):
        raise ValueError("OpenAlex publication year must be an integer year or null")
    relationships = {}
    for key in ("referenced_works", "related_works"):
        if not isinstance(item[key], list):
            raise ValueError("OpenAlex work relationships must be lists")
        relationships[key] = [work_id(value) for value in item[key]]
    if not isinstance(item["locations"], list):
        raise ValueError("OpenAlex locations must be a list")
    locations = []
    for location in item["locations"]:
        if not isinstance(location, dict) or type(location.get("is_oa")) is not bool:
            raise ValueError("OpenAlex location must have a Boolean is_oa")
        if location.get("version") not in {None, "publishedVersion", "acceptedVersion", "submittedVersion"}:
            raise ValueError("OpenAlex location version is invalid")
        normalized = {"is_oa": location["is_oa"], "version": location.get("version")}
        for key in ("landing_page_url", "pdf_url"):
            value = location.get(key)
            normalized[key] = _url(value) if value is not None else None
        locations.append(normalized)
    return {"id": work_id(item["id"]), "doi": _doi(item.get("doi")), "title": item["title"],
            "year": year, "abstract": _abstract(item.get("abstract_inverted_index")),
            **relationships, "locations": locations}


def _page(payload, arguments):
    if not isinstance(payload, dict):
        raise ValueError("OpenAlex response must be an object")
    if arguments["operation"] == "work":
        works = [normalize_work(payload)]
        if works[0]["id"] != arguments["work_id"]:
            raise ValueError("OpenAlex returned a different work identifier")
        return works, {"count": 1, "next_cursor": None, "has_more": False}
    meta, items = payload.get("meta"), payload.get("results")
    if not isinstance(meta, dict) or not isinstance(items, list):
        raise ValueError("OpenAlex list response requires meta and results")
    if (type(meta.get("count")) is not int or meta["count"] < len(items)
            or type(meta.get("per_page")) is not int or meta["per_page"] != arguments["limit"]
            or len(items) > arguments["limit"] or "next_cursor" not in meta):
        raise ValueError("OpenAlex list pagination does not match the requested page")
    cursor = meta["next_cursor"]
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 8192
                               or any(ord(c) < 32 for c in cursor)):
        raise ValueError("OpenAlex next_cursor must be a bounded string or null")
    if cursor is not None and (not items or cursor == (arguments["cursor"] or "*")):
        raise ValueError("OpenAlex cursor did not advance")
    if not items and arguments["cursor"] in (None, "*") and meta["count"] != 0:
        raise ValueError("OpenAlex initial empty page conflicts with result count")
    works = [normalize_work(item) for item in items]
    if len({item["id"] for item in works}) != len(works):
        raise ValueError("OpenAlex page contains duplicate work identifiers")
    if arguments["operation"] == "citing" and any(
            arguments["work_id"] not in item["referenced_works"] for item in works):
        raise ValueError("OpenAlex citing result does not reference the requested work")
    return works, {"count": meta["count"], "next_cursor": cursor, "has_more": cursor is not None}


def _sources(works):
    return [{"work_id": work["id"], "doi": work["doi"], "title": work["title"], "year": work["year"],
             "abstract": work["abstract"], "source_url": "https://openalex.org/" + work["id"],
             "representation": "scholarly_metadata"} for work in works]


def _text(works):
    return "\n\n".join(work["title"] + " — https://openalex.org/" + work["id"]
                       + ("\n" + work["abstract"] if work["abstract"] is not None else "") for work in works)


class OpenAlexClient:
    """One HTTP transaction, with explicit byte and wall-clock limits, without retries."""

    def __init__(self, *, timeout=30, max_bytes=1_048_576, endpoint=DEFAULT_ENDPOINT, auth_env=None):
        _limits(timeout, max_bytes)
        endpoint = _url(endpoint)
        parsed = urlsplit(endpoint)
        if parsed.port == 0:
            raise ValueError("OpenAlex endpoint port must be between 1 and 65535")
        if parsed.query or parsed.fragment or endpoint.endswith("/"):
            raise ValueError("OpenAlex endpoint must omit query, fragment and trailing slash")
        if auth_env is not None and (not isinstance(auth_env, str)
                                     or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth_env)):
            raise ValueError("OpenAlex auth_env must name an environment variable")
        self.timeout, self.max_bytes, self.endpoint, self.auth_env = timeout, max_bytes, endpoint, auth_env

    def run(self, *, operation, query=None, work_id=None, limit=5, cursor=None):
        arguments = validate_arguments({"operation": operation, "query": query, "work_id": work_id,
                                        "limit": limit, "cursor": cursor})
        url = request_url(self.endpoint, arguments)
        result = {"outcome": "provider_error", "source_url": url, "text": "", "sources": [], "works": [],
                  "capture_sha256": None, "capture": None, "raw_response": None, "gaps": [],
                  "metadata": {"provider": "openalex", "transport": "http_api", "adapter_version": ADAPTER_VERSION,
                               "schema_version": SCHEMA_VERSION, "representation": "scholarly_metadata",
                               "request": arguments, "started_at": _now(),
                               "capture_truncated": False, "capture_incomplete": False}}
        headers = {"User-Agent": "Sci-saurus/0.8 (scholarly metadata client)",
                   "Accept": "application/json", "Accept-Encoding": "identity"}
        if self.auth_env is not None:
            credential = os.environ.get(self.auth_env)
            if not credential or any(ord(c) < 33 or ord(c) > 126 for c in credential):
                result.update(outcome="auth_required", error="Configured OpenAlex credential is unavailable or invalid")
                result["metadata"]["completed_at"] = _now()
                return result
            headers["Authorization"] = "Bearer " + credential
        parsed = urlsplit(url)
        connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=self.timeout)
        deadline = time.monotonic() + self.timeout
        expired = threading.Event()

        active_socket = None

        def connect(address, timeout, source_address=None):
            nonlocal active_socket
            addresses = _resolve(address, deadline)
            error = None
            for family, kind, protocol, _, sockaddr in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or expired.is_set():
                    raise TimeoutError()
                transport_socket = socket.socket(family, kind, protocol)
                active_socket = transport_socket
                try:
                    transport_socket.settimeout(remaining)
                    if source_address is not None:
                        transport_socket.bind(source_address)
                    transport_socket.connect(sockaddr)
                    if expired.is_set():
                        raise TimeoutError()
                    return transport_socket
                except OSError as exc:
                    error = exc
                    transport_socket.close()
            raise error or OSError("OpenAlex host has no resolved addresses")

        # Replace only socket establishment; HTTP parsing and TLS retain their
        # standard-library implementations and the original hostname identity.
        connection._create_connection = connect

        def expire():
            expired.set()
            transport_socket = active_socket if active_socket is not None else connection.sock
            if transport_socket is not None:
                try:
                    transport_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        timer = threading.Timer(self.timeout, expire)
        timer.daemon = True
        response, body = None, bytearray()
        timer.start()
        try:
            connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers=headers)
            active_socket = connection.sock
            response = connection.getresponse()
            metadata = result["metadata"]
            metadata.update({"http_status": response.status, "final_url": url, "headers": {
                key.lower(): value for key, value in response.getheaders()
                if key.lower().startswith("x-ratelimit-") or key.lower() in {"retry-after", "content-type", "content-length"}
            }})
            while len(body) <= self.max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or expired.is_set():
                    raise TimeoutError()
                if active_socket is not None:
                    active_socket.settimeout(remaining)
                chunk = response.read1(min(65536, self.max_bytes + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if expired.is_set():
                raise TimeoutError()
            if len(body) > self.max_bytes:
                del body[self.max_bytes:]
                metadata["capture_truncated"] = True
                result["outcome"] = "partial"
                result["gaps"].append("OpenAlex response exceeded the capture byte limit; no works parsed.")
                return result
            if response.length not in (None, 0):
                raise IncompleteRead(b"", response.length)
            if response.status != 200:
                result["outcome"] = {401: "auth_required", 403: "access_denied", 404: "not_found",
                                     429: "rate_limited"}.get(response.status, "provider_error")
                result["error"] = f"OpenAlex returned HTTP {response.status}"
                return result
            payload = json.loads(body, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float)
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            result["raw_response"] = payload
            works, page = _page(payload, arguments)
            result.update(works=works, sources=_sources(works), text=_text(works), outcome="ok" if works else "empty")
            metadata.update(page)
            result["gaps"].append("Scholarly metadata, abstracts and location URLs do not establish acquired full text or claim support.")
        except (TimeoutError, OSError, HTTPException) as exc:
            result["outcome"] = "timeout" if expired.is_set() or isinstance(exc, TimeoutError) else "provider_error"
            result["error"] = f"OpenAlex HTTP transaction failed: {type(exc).__name__}"
            result["metadata"]["capture_incomplete"] = True
            if isinstance(exc, IncompleteRead):
                body.extend(exc.partial[:max(0, self.max_bytes - len(body))])
        except (ValueError, TypeError, RecursionError) as exc:
            result["outcome"] = "parse_error"
            result["error"] = f"OpenAlex response failed validation: {type(exc).__name__}"
        finally:
            timer.cancel()
            connection.close()
            if response is not None:
                response.close()
                result["capture"] = _capture(bytes(body), response.getheader("Content-Type", "application/json"))
                result["capture_sha256"] = result["capture"]["sha256"]
            result["metadata"]["completed_at"] = _now()
        return result
