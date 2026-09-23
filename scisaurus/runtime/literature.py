"""Bounded OpenAlex acquisitions of scholarly metadata and citation links.

Each call retrieves one page or one work. Locations and reconstructed abstracts
remain metadata; neither proves that source full text has been acquired.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from email.utils import parsedate_to_datetime
from http.client import HTTPConnection, HTTPSConnection, HTTPException, IncompleteRead
import hashlib
import errno
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import threading
import time
import uuid
from urllib.parse import urlencode, urlsplit

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.retrieval import _capture, _limits, _now, _url


ADAPTER_VERSION = "1"
SCHEMA_VERSION = "openalex-works-v1"
DEFAULT_ENDPOINT = "https://api.openalex.org/works"
# Provider request-URL limit: https://help.openalex.org/api/searching/
MAX_REQUEST_URL_BYTES = 4094
SEARCH_SYNTAX = "OpenAlex stemmed search: use search terms or quoted phrases, without '*' or '?' wildcards. The percent-encoded request URL must fit 4094 bytes."
ARGUMENT_KEYS = {"operation", "query", "work_id", "limit", "cursor"}
TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
PROVIDER_THROTTLE_KINDS = frozenset({
    "anonymous_search_load", "daily_budget", "request_rate",
})
RATE_STATE_SCHEMA_VERSION = "openalex-rate-state-1"
# The provider's credit balance is account-wide even when the workflow has
# separate topic and survey projects. Composer sets this path for an
# owner-local run; direct library users can opt in with the same variable.
SHARED_RATE_STATE_ENV = "SCISAURUS_OPENALEX_SHARED_RATE_STATE_PATH"
# Keep a small provider-side reserve. OpenAlex does not expose a reliable
# per-operation cost in every response, so admitting the next request at
# exactly zero is too late to prevent an overspend race.
DEFAULT_MIN_REMAINING_CREDITS = 2
# OpenAlex daily budgets reset at midnight UTC. A two-day ceiling accepts a
# complete daily reset window plus clock skew without allowing malformed
# provider metadata to freeze a client indefinitely.
MAX_PROVIDER_COOLDOWN_SECONDS = 2 * 24 * 3600
_RATE_STATE_THREAD_LOCK = threading.RLock()
_PROVIDER_REQUEST_LOCKS_GUARD = threading.Lock()
_PROVIDER_REQUEST_LOCKS = {}


class ProviderCooldownError(ValidationError):
    """A provider supplied a concrete future time for a safe retry."""

    def __init__(self, message, *, retry_after_seconds, rate_limit=None):
        if (type(retry_after_seconds) not in (int, float)
                or not math.isfinite(retry_after_seconds) or retry_after_seconds <= 0):
            raise ValueError("provider cooldown must be finite and positive")
        super().__init__(message)
        self.retry_after_seconds = float(retry_after_seconds)
        self.rate_limit = dict(rate_limit) if isinstance(rate_limit, dict) else None


@contextmanager
def _locked_rate_state(path):
    """Serialize state updates across threads and local worker processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _RATE_STATE_THREAD_LOCK:
        lock = lock_path.open("a+")
        flock = None
        try:
            try:
                import fcntl
                flock = fcntl
                flock.flock(lock.fileno(), flock.LOCK_EX)
            except ImportError:
                flock = None
            yield
        finally:
            if flock is not None:
                try:
                    flock.flock(lock.fileno(), flock.LOCK_UN)
                except OSError:
                    pass
            lock.close()


@contextmanager
def _locked_provider_request(path, deadline):
    """Reserve one provider transaction across local clients and processes."""
    if path is None:
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".request.lock")
    key = str(lock_path)
    with _PROVIDER_REQUEST_LOCKS_GUARD:
        thread_lock = _PROVIDER_REQUEST_LOCKS.setdefault(key, threading.Lock())
    remaining = max(0.0, deadline - time.monotonic())
    if not thread_lock.acquire(timeout=remaining):
        yield False
        return
    lock = None
    flock = None
    locked = False
    try:
        lock = lock_path.open("a+")
        try:
            try:
                import fcntl
            except ImportError as exc:
                raise ValueError(
                    "OpenAlex persistent pacing requires an interprocess file lock") from exc
            flock = fcntl
            while time.monotonic() < deadline:
                try:
                    flock.flock(lock.fileno(), flock.LOCK_EX | flock.LOCK_NB)
                    locked = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                        raise
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            if not locked:
                yield False
                return
            yield True
        finally:
            if locked and flock is not None:
                flock.flock(lock.fileno(), flock.LOCK_UN)
            if lock is not None:
                lock.close()
    finally:
        thread_lock.release()


def _read_rate_state(path):
    if not path.exists():
        return {"schema_version": RATE_STATE_SCHEMA_VERSION, "scopes": {}}
    if not path.is_file():
        raise ValueError("OpenAlex rate_state_path must name a file")
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("OpenAlex rate state is unreadable") from exc
    if (not isinstance(document, dict) or set(document) != {"schema_version", "scopes"}
            or document.get("schema_version") != RATE_STATE_SCHEMA_VERSION
            or not isinstance(document.get("scopes"), dict)):
        raise ValueError("OpenAlex rate state has an unsupported schema")
    return document


def _write_rate_state(path, document):
    encoded = json.dumps(
        document, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


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


def _authors(value):
    """Extract bounded display names when the provider supplies authorships.

    Author metadata is optional in OpenAlex responses.  Omitting it is safer
    than manufacturing an attribution, while preserving real names lets a
    later paper continuation produce conventional references.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("OpenAlex authorships must be a list or null")
    names = []
    for authorship in value:
        if not isinstance(authorship, dict):
            raise ValueError("OpenAlex authorship must be an object")
        author = authorship.get("author")
        if not isinstance(author, dict):
            continue
        name = author.get("display_name")
        if not isinstance(name, str) or not name.strip() or len(name) > 512:
            continue
        if name not in names:
            names.append(name)
        if len(names) >= 64:
            break
    return names or None


def normalize_work(item, *, tolerate_invalid_abstract=False, abstract_gaps=None):
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
    identity = work_id(item["id"])
    try:
        abstract = _abstract(item.get("abstract_inverted_index"))
    except ValueError:
        if not tolerate_invalid_abstract:
            raise
        abstract = None
        if abstract_gaps is not None:
            abstract_gaps.append({"work_id": identity, "reason": "provider_abstract_index_invalid"})
    authors = _authors(item.get("authorships"))
    return {"id": identity, "doi": _doi(item.get("doi")), "title": item["title"],
            "year": year, "abstract": abstract,
            **relationships, "locations": locations,
            **({"authors": authors} if authors else {})}


def _is_omittable_work(item):
    """Identify a provider row that cannot form a reader-facing reference.

    The exception is deliberately narrow: only list responses may omit a row
    with a valid provider identifier and no title.  Other malformed fields
    remain fatal so schema drift cannot be silently normalized.
    """
    return (isinstance(item, dict) and item.get("title") in {None, ""}
            and isinstance(item.get("id"), str))


def _page(payload, arguments):
    if not isinstance(payload, dict):
        raise ValueError("OpenAlex response must be an object")
    abstract_gaps = []
    if arguments["operation"] == "work":
        works = [normalize_work(payload, tolerate_invalid_abstract=True, abstract_gaps=abstract_gaps)]
        if works[0]["id"] != arguments["work_id"]:
            raise ValueError("OpenAlex returned a different work identifier")
        return works, {"count": 1, "next_cursor": None, "has_more": False,
                       "abstract_gaps": abstract_gaps}
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
    works = []
    omitted_work_gaps = []
    for index, item in enumerate(items):
        # OpenAlex occasionally emits a bibliographic row whose title is an
        # empty string while the rest of the page remains well formed.  That
        # row cannot become a reader-facing reference, but it must not poison
        # the valid records in the same page.  Keep the omission explicit in
        # the page metadata; all other schema violations remain fatal.
        if _is_omittable_work(item):
            omitted_work_gaps.append({"index": index, "work_id": item["id"],
                                      "reason": "provider_work_title_empty"})
            continue
        works.append(normalize_work(item, tolerate_invalid_abstract=True, abstract_gaps=abstract_gaps))
    if len({item["id"] for item in works}) != len(works):
        raise ValueError("OpenAlex page contains duplicate work identifiers")
    if arguments["operation"] == "citing" and any(
            arguments["work_id"] not in item["referenced_works"] for item in works):
        raise ValueError("OpenAlex citing result does not reference the requested work")
    return works, {"count": meta["count"], "next_cursor": cursor, "has_more": cursor is not None,
                   "abstract_gaps": abstract_gaps, "omitted_work_gaps": omitted_work_gaps}


def _sources(works):
    return [{"work_id": work["id"], "doi": work["doi"], "title": work["title"], "year": work["year"],
             "abstract": work["abstract"], "source_url": "https://openalex.org/" + work["id"],
             "representation": "scholarly_metadata",
             **({"authors": work["authors"]} if work.get("authors") else {})}
            for work in works]


def _text(works):
    return "\n\n".join(work["title"] + " — https://openalex.org/" + work["id"]
                       + ("\n" + work["abstract"] if work["abstract"] is not None else "") for work in works)


def _retry_after_seconds(value, *, now=None):
    """Parse both legal Retry-After forms without inventing a provider delay."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 0 else None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                return None
            seconds = target.timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _header_number(headers, name):
    value = headers.get(name)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _rate_limit_metadata(headers, payload, *, authenticated):
    """Normalize OpenAlex quota evidence and classify only explicit signals."""
    payload = payload if isinstance(payload, dict) else {}
    message = payload.get("message") or payload.get("error")
    message = message if isinstance(message, str) else None
    retry_after = _retry_after_seconds(headers.get("retry-after"))
    if retry_after is None:
        retry_after = _retry_after_seconds(payload.get("retryAfter"))
    remaining = _header_number(headers, "x-ratelimit-remaining")
    limit = _header_number(headers, "x-ratelimit-limit")
    reset = _header_number(headers, "x-ratelimit-reset")
    credits_used = _header_number(headers, "x-ratelimit-credits-used")
    lower = (message or "").casefold()
    if (not authenticated and "anonymous" in lower
            and ("temporarily" in lower or "elevated load" in lower
                 or "heavy load" in lower or "search is paused" in lower
                 or "cluster recovers" in lower)):
        kind = "anonymous_search_load"
    elif (remaining == 0 or "daily budget" in lower or "daily quota" in lower
            or "credits exhausted" in lower or "insufficient budget" in lower
            or "resets at midnight" in lower):
        kind = "daily_budget"
    elif "per second" in lower or "too many requests" in lower:
        kind = "request_rate"
    else:
        kind = "unknown"
    return {
        "kind": kind,
        "authenticated": authenticated,
        "retry_after_seconds": retry_after,
        "limit": limit,
        "remaining": remaining,
        "reset": reset,
        "credits_used": credits_used,
        "message": message,
    }


def provider_cooldown_seconds(rate_limit, *, now=None):
    """Return the strongest bounded cooldown stated by OpenAlex metadata."""
    if not isinstance(rate_limit, dict):
        return None
    delays = []
    retry_after = rate_limit.get("retry_after_seconds")
    if (type(retry_after) in (int, float) and math.isfinite(retry_after)
            and retry_after > 0):
        delays.append(float(retry_after))
    reset = rate_limit.get("reset")
    if (rate_limit.get("kind") == "daily_budget" and type(reset) in (int, float)
            and math.isfinite(reset)):
        reset_delay = (float(reset) - (time.time() if now is None else now)
                       if reset > 10_000_000 else float(reset))
        if reset_delay > 0:
            delays.append(reset_delay)
    if not delays:
        return None
    return min(max(delays), float(MAX_PROVIDER_COOLDOWN_SECONDS))


class OpenAlexClient:
    """Bounded OpenAlex transactions with provider-aware retry handling."""

    def __init__(self, *, timeout=30, max_bytes=1_048_576, endpoint=DEFAULT_ENDPOINT, auth_env=None,
                 max_retries=3, retry_backoff_seconds=1.0, min_interval_seconds=0.0,
                 rate_state_path=None, allow_anonymous_fallback=False):
        _limits(timeout, max_bytes)
        if type(max_retries) is not int or max_retries < 0 or max_retries > 8:
            raise ValueError("max_retries must be an integer between 0 and 8")
        if type(retry_backoff_seconds) not in (int, float) or not math.isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be finite and non-negative")
        if (type(min_interval_seconds) not in (int, float)
                or not math.isfinite(min_interval_seconds) or min_interval_seconds < 0):
            raise ValueError("min_interval_seconds must be finite and non-negative")
        if type(allow_anonymous_fallback) is not bool:
            raise ValueError("allow_anonymous_fallback must be a Boolean")
        endpoint = _url(endpoint)
        parsed = urlsplit(endpoint)
        if parsed.port == 0:
            raise ValueError("OpenAlex endpoint port must be between 1 and 65535")
        if parsed.query or parsed.fragment or endpoint.endswith("/"):
            raise ValueError("OpenAlex endpoint must omit query, fragment and trailing slash")
        if auth_env is not None and (not isinstance(auth_env, str)
                                     or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", auth_env)):
            raise ValueError("OpenAlex auth_env must name an environment variable")
        if rate_state_path is not None:
            if not isinstance(rate_state_path, (str, os.PathLike)):
                raise ValueError("OpenAlex rate_state_path must be an absolute file path")
            rate_state_path = Path(rate_state_path)
            if (not rate_state_path.is_absolute()
                    or rate_state_path.exists() and not rate_state_path.is_file()):
                raise ValueError("OpenAlex rate_state_path must be an absolute file path")
            rate_state_path = rate_state_path.resolve()
        shared_rate_state_path = os.environ.get(SHARED_RATE_STATE_ENV)
        if shared_rate_state_path is not None:
            shared_rate_state_path = Path(shared_rate_state_path)
            if (not shared_rate_state_path.is_absolute()
                    or shared_rate_state_path.exists() and not shared_rate_state_path.is_file()):
                raise ValueError(f"{SHARED_RATE_STATE_ENV} must be an absolute file path")
            shared_rate_state_path = shared_rate_state_path.resolve()
        self.timeout, self.max_bytes, self.endpoint, self.auth_env = timeout, max_bytes, endpoint, auth_env
        self.max_retries, self.retry_backoff_seconds = max_retries, float(retry_backoff_seconds)
        self.min_interval_seconds = float(min_interval_seconds)
        self.legacy_rate_state_path = rate_state_path
        self.rate_state_path = shared_rate_state_path or rate_state_path
        self.allow_anonymous_fallback = allow_anonymous_fallback
        self._pacing_lock = threading.Lock()
        self._next_request_at = 0.0

    @staticmethod
    def _request_class(arguments):
        return {"search": "search", "citing": "filter", "work": "singleton"}[
            arguments["operation"]]

    def _rate_scope_key(self, request_class, principal):
        return hashlib.sha256(json.dumps({
            "endpoint": self.endpoint,
            "principal": principal,
            "request_class": request_class,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _legacy_anonymous_scope_key(self, request_class):
        return hashlib.sha256(json.dumps({
            "endpoint": self.endpoint,
            "auth_env": None,
            "request_class": request_class,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def _merge_legacy_rate_state(self, principal):
        """Promote a stage-local state file into the account-wide ledger.

        Older immutable descriptors put state below ``projects/topic`` or
        ``projects/survey``. Reading that state on first use makes an old
        checkpoint safe to resume without rewriting its descriptor. Daily
        budget observations are promoted to the global request class so a
        legacy search observation also fences singleton/citation requests.
        """
        legacy = self.legacy_rate_state_path
        target = self.rate_state_path
        if legacy is None or target is None or legacy == target or not legacy.is_file():
            return
        with _locked_rate_state(legacy):
            source = _read_rate_state(legacy)
        changed = False
        allowed_keys = {
            self._rate_scope_key(scope, principal)
            for scope in ("global", "search", "filter", "singleton")
        }
        if principal == "anonymous":
            allowed_keys.update({
                self._legacy_anonymous_scope_key("global"),
                self._legacy_anonymous_scope_key("search"),
                self._legacy_anonymous_scope_key("filter"),
                self._legacy_anonymous_scope_key("singleton"),
            })
        with _locked_rate_state(target):
            document = _read_rate_state(target)
            for key, entry in source.get("scopes", {}).items():
                if (key not in allowed_keys or not isinstance(entry, dict)
                        or not isinstance(entry.get("rate_limit"), dict)):
                    continue
                old_until = entry.get("blocked_until_epoch")
                if (not isinstance(old_until, (int, float))
                        or not math.isfinite(old_until)):
                    continue
                target_key = key
                if entry["rate_limit"].get("kind") == "daily_budget":
                    target_key = self._rate_scope_key("global", principal)
                existing = document["scopes"].get(target_key)
                new_until = existing.get("blocked_until_epoch") if isinstance(existing, dict) else None
                if (not isinstance(new_until, (int, float))
                        or not math.isfinite(new_until) or old_until > new_until):
                    document["scopes"][target_key] = deepcopy(entry)
                    changed = True
            if changed:
                _write_rate_state(target, document)

    @staticmethod
    def _remaining_credit_reserve():
        value = os.environ.get("SCISAURUS_OPENALEX_MIN_REMAINING_CREDITS")
        if value is None:
            return DEFAULT_MIN_REMAINING_CREDITS
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return DEFAULT_MIN_REMAINING_CREDITS
        return parsed if parsed >= 0 else DEFAULT_MIN_REMAINING_CREDITS

    def _active_persistent_cooldown(self, request_class, principal):
        if self.rate_state_path is None:
            return None
        self._merge_legacy_rate_state(principal)
        with _locked_rate_state(self.rate_state_path):
            document = _read_rate_state(self.rate_state_path)
            keys = [self._rate_scope_key("global", principal),
                    self._rate_scope_key(request_class, principal)]
            if principal == "anonymous":
                # Preserve cooldowns written by the previous anonymous scope
                # format while never reusing a key-specific legacy scope.
                keys.extend([self._legacy_anonymous_scope_key("global"),
                             self._legacy_anonymous_scope_key(request_class)])
            now = time.time()
            active = []
            changed = False
            for key in keys:
                entry = document["scopes"].get(key)
                if entry is None:
                    continue
                valid = (
                    isinstance(entry, dict)
                    and set(entry) == {"blocked_until_epoch", "recorded_at", "rate_limit"}
                    and type(entry.get("blocked_until_epoch")) in (int, float)
                    and math.isfinite(entry["blocked_until_epoch"])
                    and type(entry.get("recorded_at")) in (int, float)
                    and math.isfinite(entry["recorded_at"])
                    and entry["blocked_until_epoch"] > entry["recorded_at"]
                    and entry["blocked_until_epoch"] - entry["recorded_at"]
                    <= MAX_PROVIDER_COOLDOWN_SECONDS + 1
                    and isinstance(entry.get("rate_limit"), dict)
                )
                if not valid or entry["blocked_until_epoch"] <= now:
                    document["scopes"].pop(key, None)
                    changed = True
                    continue
                active.append(entry)
            if changed:
                _write_rate_state(self.rate_state_path, document)
            if not active:
                return None
            entry = max(active, key=lambda item: item["blocked_until_epoch"])
            rate_limit = dict(entry["rate_limit"])
            remaining = entry["blocked_until_epoch"] - now
            rate_limit["retry_after_seconds"] = remaining
            if rate_limit.get("kind") == "daily_budget":
                rate_limit["reset"] = remaining
            return {
                "blocked_until_epoch": entry["blocked_until_epoch"],
                "retry_after_seconds": remaining,
                "rate_limit": rate_limit,
            }

    def _persist_cooldown(self, rate_limit, seconds, request_class, principal):
        if self.rate_state_path is None or seconds is None or seconds <= 0:
            return
        now = time.time()
        bounded = min(float(seconds), float(MAX_PROVIDER_COOLDOWN_SECONDS))
        preserved = {
            key: value for key, value in rate_limit.items()
            if key in {"kind", "authenticated", "retry_after_seconds", "limit",
                       "remaining", "reset", "credits_used", "message"}
            and (value is None or isinstance(value, (str, int, float, bool)))
        }
        if isinstance(preserved.get("message"), str):
            preserved["message"] = preserved["message"][:2048]
        with _locked_rate_state(self.rate_state_path):
            document = _read_rate_state(self.rate_state_path)
            scope_class = ("global" if rate_limit.get("kind") in {
                               "request_rate", "unknown", "daily_budget"}
                           else request_class)
            scope_key = self._rate_scope_key(scope_class, principal)
            existing = document["scopes"].get(scope_key)
            blocked_until = now + bounded
            if (isinstance(existing, dict)
                    and type(existing.get("blocked_until_epoch")) in (int, float)
                    and math.isfinite(existing["blocked_until_epoch"])):
                blocked_until = max(blocked_until, float(existing["blocked_until_epoch"]))
            document["scopes"][scope_key] = {
                "blocked_until_epoch": blocked_until,
                "recorded_at": now,
                "rate_limit": preserved,
            }
            _write_rate_state(self.rate_state_path, document)

    def _cooldown_result(self, arguments, url, cooldown, principal):
        rate_limit = cooldown["rate_limit"]
        message = rate_limit.get("message")
        detail = f": {message}" if isinstance(message, str) and message.strip() else ""
        timestamp = _now()
        return {
            "outcome": "rate_limited", "source_url": url, "text": "", "sources": [],
            "works": [], "capture_sha256": None, "capture": None, "raw_response": None,
            "gaps": [],
            "error": "OpenAlex request suppressed until the recorded provider cooldown expires" + detail,
            "metadata": {
                "provider": "openalex", "transport": "http_api",
                "adapter_version": ADAPTER_VERSION, "schema_version": SCHEMA_VERSION,
                "representation": "scholarly_metadata", "request": arguments,
                "authenticated": principal != "anonymous",
                "attempts": 0, "retry_wait_seconds": 0.0, "pacing_wait_seconds": 0.0,
                "retry_budget_exhausted": True,
                "retry_suppressed_by_persistent_cooldown": True,
                "cooldown_cache_hit": True,
                "blocked_until_epoch": cooldown["blocked_until_epoch"],
                "rate_limit": rate_limit,
                "capture_truncated": False, "capture_incomplete": False,
                "started_at": timestamp, "completed_at": timestamp,
            },
        }

    def preflight(self, *, operation="search", query=None, work_id=None,
                  limit=5, cursor=None):
        """Check the persisted provider fence without making an HTTP call.

        Composer uses this before topic model work. A provider reset should
        not consume a proposal call merely to discover that the subsequent
        literature request is already inadmissible.
        """
        arguments = validate_arguments({"operation": operation, "query": query,
                                        "work_id": work_id, "limit": limit, "cursor": cursor})
        credential = os.environ.get(self.auth_env) if self.auth_env is not None else None
        credential_ready = self.auth_env is None or bool(
            credential and all(33 <= ord(character) <= 126 for character in credential))
        anonymous_fallback = self.auth_env is not None and not credential_ready \
            and self.allow_anonymous_fallback
        if anonymous_fallback:
            credential = None
        if self.auth_env is not None and not credential_ready and not anonymous_fallback:
            return None
        principal = ("anonymous" if self.auth_env is None or anonymous_fallback else
                     "key:" + hashlib.sha256(credential.encode("utf-8")).hexdigest())
        return self._active_persistent_cooldown(self._request_class(arguments), principal)

    def _reserve_request_slot(self, deadline):
        """Serialize this client's requests and honor any provider cooldown."""
        with self._pacing_lock:
            now = time.monotonic()
            scheduled = max(now, self._next_request_at)
            if scheduled >= deadline:
                return None
            self._next_request_at = scheduled + self.min_interval_seconds
        delay = scheduled - now
        if delay > 0:
            time.sleep(delay)
        return delay

    def _extend_cooldown(self, seconds):
        if seconds is None or seconds <= 0:
            return
        with self._pacing_lock:
            self._next_request_at = max(self._next_request_at, time.monotonic() + seconds)

    def run(self, *, operation, query=None, work_id=None, limit=5, cursor=None):
        """Serialize persistent-state users before rechecking quota state."""
        arguments = validate_arguments({"operation": operation, "query": query,
                                        "work_id": work_id, "limit": limit, "cursor": cursor})
        url = request_url(self.endpoint, arguments)
        started = time.monotonic()
        deadline = started + self.timeout
        credential = os.environ.get(self.auth_env) if self.auth_env is not None else None
        credential_ready = self.auth_env is None or bool(
            credential and all(33 <= ord(character) <= 126 for character in credential))
        anonymous_fallback = self.auth_env is not None and not credential_ready \
            and self.allow_anonymous_fallback
        if anonymous_fallback:
            credential = None
        principal = ("anonymous" if self.auth_env is None or anonymous_fallback else
                     "key:" + hashlib.sha256(credential.encode("utf-8")).hexdigest()
                     if credential_ready else None)
        auth_required = self.auth_env is not None and not anonymous_fallback
        with _locked_provider_request(self.rate_state_path, deadline) as reserved:
            if not reserved:
                timestamp = _now()
                return {
                    "outcome": "timeout", "source_url": url, "text": "", "sources": [],
                    "works": [], "capture_sha256": None, "capture": None,
                    "raw_response": None, "gaps": [],
                    "error": "OpenAlex request budget expired while waiting for the provider reservation",
                    "metadata": {
                        "provider": "openalex", "transport": "http_api",
                        "adapter_version": ADAPTER_VERSION, "schema_version": SCHEMA_VERSION,
                        "representation": "scholarly_metadata", "request": arguments,
                        "authenticated": credential is not None,
                        "attempts": 0, "retry_wait_seconds": 0.0,
                        "pacing_wait_seconds": 0.0, "retry_budget_exhausted": True,
                        "capture_truncated": False, "capture_incomplete": False,
                        "started_at": timestamp, "completed_at": timestamp,
                    },
                }
            return self._run_serialized(
                operation=operation, query=query, work_id=work_id, limit=limit,
                cursor=cursor, credential=credential, principal=principal,
                credential_ready=credential_ready or anonymous_fallback,
                auth_required=auth_required, started=started, deadline=deadline,
            )

    def _run_serialized(self, *, operation, query=None, work_id=None, limit=5,
                        cursor=None, credential=None, principal=None,
                        credential_ready=True, auth_required=None, started=None, deadline=None):
        """Retry transient provider responses inside one operation budget.

        The timeout is a total budget for the call, so backoff cannot silently
        turn a nominally bounded request into an unbounded sequence of calls.
        """
        arguments = validate_arguments({"operation": operation, "query": query, "work_id": work_id,
                                        "limit": limit, "cursor": cursor})
        if auth_required is None:
            auth_required = self.auth_env is not None
        url = request_url(self.endpoint, arguments)
        request_class = self._request_class(arguments)
        if credential_ready:
            cooldown = self._active_persistent_cooldown(request_class, principal)
            if cooldown is not None:
                self._extend_cooldown(cooldown["retry_after_seconds"])
                return self._cooldown_result(arguments, url, cooldown, principal)

        started = time.monotonic() if started is None else started
        deadline = started + self.timeout if deadline is None else deadline
        last = None
        retry_wait_seconds = 0.0
        pacing_wait_seconds = 0.0
        for attempt in range(self.max_retries + 1):
            waited = self._reserve_request_slot(deadline)
            if waited is None:
                break
            pacing_wait_seconds += waited
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            last = self._run_once(operation=operation, query=query, work_id=work_id,
                                  limit=limit, cursor=cursor, timeout=remaining,
                                  credential=credential, auth_required=auth_required)
            last.setdefault("metadata", {})["attempts"] = attempt + 1
            status = (last.get("metadata") or {}).get("http_status")
            provider_throttle = last.get("outcome") == "rate_limited"
            delay = self.retry_backoff_seconds * (2 ** attempt)
            if provider_throttle:
                rate_limit = (last.get("metadata") or {}).get("rate_limit") or {}
                provider_delay = provider_cooldown_seconds(rate_limit)
                if provider_delay is not None:
                    delay = max(delay, provider_delay)
                # Preserve the provider's last cooldown even when no retry fits
                # this call, so another query on the same client cannot hammer.
                self._extend_cooldown(delay)
                try:
                    self._persist_cooldown(rate_limit, delay, request_class, principal)
                except (OSError, ValueError) as exc:
                    last.setdefault("metadata", {})["rate_state_error"] = (
                        f"{type(exc).__name__}: {exc}")
            if status == 200:
                rate_limit = (last.get("metadata") or {}).get("rate_limit") or {}
                remaining_credits = rate_limit.get("remaining")
                request_credits = rate_limit.get("credits_used")
                reserve = self._remaining_credit_reserve()
                known_cost = (request_credits if type(request_credits) in (int, float)
                               and request_credits > 0 else 0)
                if (type(remaining_credits) in (int, float)
                        and remaining_credits <= max(reserve, known_cost)):
                    preventive = {**rate_limit, "kind": "daily_budget"}
                    delay = provider_cooldown_seconds(preventive)
                    if delay is not None:
                        try:
                            self._persist_cooldown(preventive, delay, request_class, principal)
                            last.setdefault("metadata", {})["preventive_cooldown_seconds"] = delay
                        except (OSError, ValueError) as exc:
                            last.setdefault("metadata", {})["rate_state_error"] = (
                                f"{type(exc).__name__}: {exc}")
            if status not in TRANSIENT_HTTP_STATUSES or attempt >= self.max_retries:
                last.setdefault("metadata", {})["retry_wait_seconds"] = retry_wait_seconds
                last["metadata"]["pacing_wait_seconds"] = pacing_wait_seconds
                if status in TRANSIENT_HTTP_STATUSES:
                    last["metadata"]["retry_budget_exhausted"] = True
                return last
            if time.monotonic() + delay >= deadline:
                last.setdefault("metadata", {})["retry_wait_seconds"] = retry_wait_seconds
                last["metadata"]["pacing_wait_seconds"] = pacing_wait_seconds
                last["metadata"]["retry_budget_exhausted"] = True
                return last
            # The shared client pacing slot owns 429 sleeps. Other transient
            # failures have no provider cooldown and use the local backoff.
            if not provider_throttle:
                retry_wait_seconds += delay
                time.sleep(delay)
        if last is None:
            last = {"outcome": "timeout", "source_url": None, "text": "", "sources": [],
                    "works": [], "capture_sha256": None, "capture": None, "raw_response": None,
                    "gaps": [], "error": "OpenAlex request budget expired before the next paced request",
                    "metadata": {"provider": "openalex", "transport": "http_api",
                                 "adapter_version": ADAPTER_VERSION, "schema_version": SCHEMA_VERSION,
                                 "attempts": 0, "retry_budget_exhausted": True,
                                 "started_at": _now(), "completed_at": _now()}}
        last.setdefault("metadata", {})["retry_wait_seconds"] = retry_wait_seconds
        last["metadata"]["pacing_wait_seconds"] = pacing_wait_seconds
        return last

    def _run_once(self, *, operation, query=None, work_id=None, limit=5, cursor=None,
                  timeout=None, credential=None, auth_required=None):
        request_timeout = self.timeout if timeout is None else timeout
        arguments = validate_arguments({"operation": operation, "query": query, "work_id": work_id,
                                        "limit": limit, "cursor": cursor})
        url = request_url(self.endpoint, arguments)
        result = {"outcome": "provider_error", "source_url": url, "text": "", "sources": [], "works": [],
                  "capture_sha256": None, "capture": None, "raw_response": None, "gaps": [],
                  "metadata": {"provider": "openalex", "transport": "http_api", "adapter_version": ADAPTER_VERSION,
                               "schema_version": SCHEMA_VERSION, "representation": "scholarly_metadata",
                               "request": arguments, "started_at": _now(),
                               "authenticated": credential is not None,
                               "capture_truncated": False, "capture_incomplete": False}}
        if auth_required is None:
            auth_required = self.auth_env is not None
        headers = {"User-Agent": "Sci-saurus/0.8 (scholarly metadata client)",
                   "Accept": "application/json", "Accept-Encoding": "identity"}
        if auth_required:
            if not credential or any(ord(c) < 33 or ord(c) > 126 for c in credential):
                result.update(outcome="auth_required", error="Configured OpenAlex credential is unavailable or invalid")
                result["metadata"]["completed_at"] = _now()
                return result
            headers["Authorization"] = "Bearer " + credential
        parsed = urlsplit(url)
        connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=request_timeout)
        deadline = time.monotonic() + request_timeout
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

        timer = threading.Timer(request_timeout, expire)
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
            metadata["rate_limit"] = _rate_limit_metadata(
                metadata["headers"], None, authenticated=credential is not None)
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
                payload = None
                try:
                    payload = json.loads(body, object_pairs_hook=_object, parse_constant=_constant,
                                         parse_float=_float) if body else None
                except (ValueError, TypeError, RecursionError):
                    payload = None
                if payload is not None and response.status == 429:
                    result["raw_response"] = payload
                metadata["rate_limit"] = _rate_limit_metadata(
                    metadata["headers"], payload, authenticated=credential is not None)
                rate_kind = metadata["rate_limit"].get("kind")
                result["outcome"] = (
                    "rate_limited" if response.status == 429 or rate_kind in PROVIDER_THROTTLE_KINDS
                    else {401: "auth_required", 403: "access_denied", 404: "not_found"}.get(
                        response.status, "provider_error"))
                provider_message = payload.get("message") if isinstance(payload, dict) else None
                result["error"] = (provider_message if isinstance(provider_message, str) and provider_message.strip()
                                   else f"OpenAlex returned HTTP {response.status}")
                return result
            payload = json.loads(body, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float)
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            result["raw_response"] = payload
            works, page = _page(payload, arguments)
            result.update(works=works, sources=_sources(works), text=_text(works), outcome="ok" if works else "empty")
            metadata.update(page)
            result["gaps"].append("Scholarly metadata, abstracts and location URLs do not establish acquired full text or claim support.")
            if page.get("abstract_gaps"):
                result["gaps"].append("One or more provider abstract indexes were malformed; affected abstracts were omitted.")
            if page.get("omitted_work_gaps"):
                result["gaps"].append("One or more provider work rows had empty titles and were omitted.")
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
