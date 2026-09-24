"""Bounded Crossref search and configured MCP Fetch execution.

Search metadata and fetched source text are acquisitions, not verified evidence.
The MCP adapter implements the stdio tools lifecycle against an installed server;
it never substitutes a local extraction routine when the server fails.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from urllib.robotparser import RobotFileParser

from scisaurus.runtime import pdf_text

ADAPTER_VERSION = "4"
MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {MCP_PROTOCOL_VERSION, "2025-06-18", "2025-03-26", "2024-11-05"}
CROSSREF_TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
SAFE_PROCESS_ENV = {
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT",
    "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "LANG", "LC_ALL",
}
PDF_USER_AGENT = "Sci-saurus/0.8 (scholarly source retrieval)"
ROBOTS_MAX_BYTES = 512 * 1024
MAX_HTTP_REDIRECTS = 5
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _open_http(request, *, timeout):
    """Open one HTTP response without crossing an unchecked redirect."""
    opener = build_opener(_NoRedirect())
    try:
        return opener.open(request, timeout=timeout)
    except HTTPError as response:
        return response


def _http_status(response):
    return getattr(response, "status", getattr(response, "code", None))


def _redirect_record(from_url, to_url, status):
    def stamp(value):
        return {"prefix": value[:256],
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
    return {"from": stamp(from_url), "to": stamp(to_url), "status": status}


def _bounded_http_body(response, *, byte_limit, deadline, body=None):
    body = body if body is not None else bytearray()
    while len(body) <= byte_limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP response exceeded its total request deadline")
        read_size = min(65536, byte_limit + 1 - len(body))
        reader = getattr(response, "read1", response.read)
        _set_response_read_timeout(response, remaining)
        chunk = reader(read_size)
        if not chunk:
            return bytes(body), False
        body.extend(chunk)
    return bytes(body), True


def _robots_policy(url, *, deadline, cache):
    """Fetch and apply one origin's robots rules; uncertain policies fail closed."""
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    port = parsed.port
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    key = (parsed.scheme.casefold(), parsed.hostname.casefold(),
           None if port in (None, default_port) else port)
    if key in cache:
        cached = cache[key]
        policy = dict(cached["policy"])
        parser = cached.get("parser")
        if parser is not None:
            allowed = parser.can_fetch(PDF_USER_AGENT, url)
            policy["outcome"] = "allowed" if allowed else "robots_denied"
            if not allowed:
                policy["reason"] = "Robots policy disallows this PDF URL"
        return policy
    initial_url = origin + "/robots.txt"
    current_url, redirects = initial_url, []
    policy = None
    parser = None
    for redirect_count in range(MAX_HTTP_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            policy = {"outcome": "robots_unavailable", "robots_url": initial_url,
                      "status": None, "reason": "Robots policy request exceeded its deadline"}
            break
        try:
            response = _open_http(Request(current_url, headers={"User-Agent": PDF_USER_AGENT}),
                                  timeout=remaining)
        except (OSError, URLError, HTTPException, IncompleteRead, TimeoutError) as exc:
            policy = {"outcome": "robots_unavailable", "robots_url": initial_url,
                      "status": None, "reason": f"Robots policy could not be fetched: {type(exc).__name__}"}
            break
        with response:
            status = _http_status(response)
            headers = response.headers
            if status in REDIRECT_STATUSES:
                location = headers.get("Location") if hasattr(headers, "get") else None
                if not isinstance(location, str) or not location.strip() or redirect_count >= MAX_HTTP_REDIRECTS:
                    policy = {"outcome": "robots_unavailable", "robots_url": initial_url,
                              "status": status, "reason": "Robots policy redirect is invalid or exceeds five hops",
                              "redirects": redirects}
                    break
                next_url = urljoin(current_url, location)
                try:
                    _url(next_url)
                except ValueError:
                    policy = {"outcome": "robots_unavailable", "robots_url": initial_url,
                              "status": status, "reason": "Robots policy redirect is not HTTP(S)",
                              "redirects": redirects}
                    break
                redirects.append(_redirect_record(current_url, next_url, status))
                current_url = next_url
                continue
            if status is not None and 200 <= status < 300:
                try:
                    body, truncated = _bounded_http_body(
                        response, byte_limit=ROBOTS_MAX_BYTES, deadline=deadline)
                except (OSError, URLError, HTTPException, IncompleteRead, TimeoutError) as exc:
                    policy = {"outcome": "robots_unavailable", "robots_url": initial_url,
                              "final_url_sha256": hashlib.sha256(current_url.encode("utf-8")).hexdigest(),
                              "status": status,
                              "reason": f"Robots policy body could not be read: {type(exc).__name__}",
                              "redirects": redirects}
                    break
                if truncated:
                    policy = {"outcome": "robots_unavailable", "robots_url": initial_url,
                              "final_url_sha256": hashlib.sha256(current_url.encode("utf-8")).hexdigest(),
                              "status": status, "bytes": len(body),
                              "sha256": hashlib.sha256(body).hexdigest(),
                              "reason": "Robots policy exceeds the 512 KiB parser limit",
                              "redirects": redirects}
                    break
                parser = RobotFileParser(initial_url)
                parser.parse(body.decode("utf-8-sig", errors="replace").splitlines())
                allowed = parser.can_fetch(PDF_USER_AGENT, url)
                policy = {"outcome": "allowed" if allowed else "robots_denied",
                          "robots_url": initial_url,
                          "final_url_sha256": hashlib.sha256(current_url.encode("utf-8")).hexdigest(),
                          "status": status, "bytes": len(body),
                          "sha256": hashlib.sha256(body).hexdigest(), "redirects": redirects}
                if not allowed:
                    policy["reason"] = "Robots policy disallows this PDF URL"
                break
            if status in {404, 410}:
                policy = {"outcome": "allowed", "robots_url": initial_url,
                          "final_url_sha256": hashlib.sha256(current_url.encode("utf-8")).hexdigest(),
                          "status": status,
                          "bytes": 0, "redirects": redirects,
                          "reason": "Origin has no robots policy file"}
            elif status in {401, 403, 429}:
                policy = {"outcome": "robots_denied", "robots_url": initial_url,
                          "final_url_sha256": hashlib.sha256(current_url.encode("utf-8")).hexdigest(),
                          "status": status,
                          "redirects": redirects,
                          "reason": "Robots policy access was denied or rate limited"}
            else:
                policy = {"outcome": "robots_unavailable", "robots_url": initial_url,
                          "final_url_sha256": hashlib.sha256(current_url.encode("utf-8")).hexdigest(),
                          "status": status,
                          "redirects": redirects,
                          "reason": "Robots policy is unavailable; retrieval is withheld"}
            break
    if policy is None:
        policy = {"outcome": "robots_unavailable", "robots_url": initial_url,
                  "status": None, "redirects": redirects,
                  "reason": "Robots policy could not be resolved"}
    cache[key] = {"policy": dict(policy), "parser": parser}
    return policy


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _limits(timeout, max_bytes):
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")


def _url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
        raise ValueError("source URL must be a bounded HTTP(S) URL")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URL must be HTTP(S) without embedded credentials")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("source URL must contain a valid port") from exc
    return url


def _capture(body: bytes, media_type: str) -> dict:
    return {
        "encoding": "base64", "body": base64.b64encode(body).decode("ascii"),
        "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "media_type": media_type,
    }


def _set_response_read_timeout(response, timeout):
    """Apply the remaining total request budget to urllib's active socket."""
    file_pointer = getattr(response, "fp", None)
    candidates = (file_pointer, getattr(file_pointer, "raw", None))
    for candidate in candidates:
        sock = getattr(candidate, "_sock", None)
        if sock is not None and callable(getattr(sock, "settimeout", None)):
            sock.settimeout(timeout)
            return


def _result(provider: str, transport: str, source_url: str) -> dict:
    return {
        "outcome": "provider_error", "source_url": source_url, "text": "", "sources": [],
        "capture_sha256": None, "capture": None, "raw_response": None, "gaps": [],
        "metadata": {
            "provider": provider, "transport": transport, "adapter_version": ADAPTER_VERSION,
            "started_at": _now(),
        },
    }


class CrossrefClient:
    """Public bibliographic search with a bounded, provider-aware retry loop."""

    def __init__(
        self, *, mailto: str | None = None, mailto_env: str | None = None,
        timeout: float = 30, max_bytes: int = 1_048_576,
        endpoint: str = "https://api.crossref.org/works", max_retries: int = 2,
        retry_backoff_seconds: float = 1.0,
    ):
        _limits(timeout, max_bytes)
        if type(max_retries) is not int or not 0 <= max_retries <= 8:
            raise ValueError("max_retries must be an integer between 0 and 8")
        if (type(retry_backoff_seconds) not in (int, float)
                or not math.isfinite(retry_backoff_seconds)
                or retry_backoff_seconds < 0):
            raise ValueError("retry_backoff_seconds must be finite and non-negative")
        if mailto is not None and (
                not isinstance(mailto, str) or not mailto.strip() or len(mailto) > 254):
            raise ValueError("mailto must be a bounded nonempty contact address")
        if mailto_env is not None and (
                not isinstance(mailto_env, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", mailto_env)):
            raise ValueError("mailto_env must name an environment variable")
        if mailto is None and mailto_env:
            configured = os.environ.get(mailto_env)
            mailto = configured.strip() if isinstance(configured, str) and configured.strip() else None
        self.timeout, self.max_bytes, self.endpoint = timeout, max_bytes, _url(endpoint)
        self.mailto, self.mailto_env = mailto, mailto_env
        self.max_retries = max_retries
        self.retry_backoff_seconds = float(retry_backoff_seconds)

    @staticmethod
    def _retry_after_seconds(headers):
        value = headers.get("retry-after") if hasattr(headers, "get") else None
        if value is None:
            return None
        try:
            delay = float(str(value).strip())
        except (TypeError, ValueError):
            try:
                target = parsedate_to_datetime(str(value))
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                delay = target.timestamp() - time.time()
            except (TypeError, ValueError, OverflowError, IndexError):
                return None
        return delay if math.isfinite(delay) and delay > 0 else None

    def search(self, query: str, *, limit: int = 5, cursor: str | None = None) -> dict:
        """Run one logical lookup inside one total timeout and retry budget.

        Crossref's response headers are part of the operational result. A
        transient 429/5xx is retried only when the same bounded call still has
        time left; permanent access and parse failures are returned immediately.
        """
        started = time.monotonic()
        deadline = started + self.timeout
        last = None
        retry_wait_seconds = 0.0
        for attempt in range(self.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            last = self._search_once(query, limit=limit, cursor=cursor,
                                     request_timeout=remaining)
            metadata = last.setdefault("metadata", {})
            metadata["attempts"] = attempt + 1
            status = metadata.get("http_status")
            if status not in CROSSREF_TRANSIENT_HTTP_STATUSES or attempt >= self.max_retries:
                metadata["retry_wait_seconds"] = retry_wait_seconds
                if status in CROSSREF_TRANSIENT_HTTP_STATUSES:
                    metadata["retry_budget_exhausted"] = True
                return last
            headers = metadata.get("headers") or {}
            delay = max(self.retry_backoff_seconds * (2 ** attempt),
                        self._retry_after_seconds(headers) or 0.0)
            if time.monotonic() + delay >= deadline:
                metadata["retry_wait_seconds"] = retry_wait_seconds
                metadata["retry_budget_exhausted"] = True
                return last
            if delay:
                time.sleep(delay)
                retry_wait_seconds += delay
        if last is not None:
            last.setdefault("metadata", {}).update(
                retry_wait_seconds=retry_wait_seconds, retry_budget_exhausted=True)
            last["outcome"] = "timeout"
            last["error"] = "Crossref request budget expired before a successful response"
            return last
        result = _result("crossref", "http_api", self.endpoint)
        result.update(outcome="timeout", error="Crossref request budget expired before dispatch")
        result["metadata"].update({"query": query, "rows": limit, "attempts": 0,
                                    "retry_wait_seconds": retry_wait_seconds,
                                    "retry_budget_exhausted": True, "completed_at": _now()})
        return result

    def _search_once(self, query: str, *, limit: int = 5, cursor: str | None = None,
                     request_timeout: float | None = None) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 2048:
            raise ValueError("search query must be nonempty and at most 2048 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Crossref rows must be between 1 and 1000")
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", query.strip(), flags=re.IGNORECASE).lower()
        exact_doi = bool(re.fullmatch(r"10\.[0-9]{4,9}/\S+", doi))
        params = ({"filter": "doi:" + doi, "rows": limit, "cursor": "*"} if exact_doi else
                  {"query.bibliographic": query, "rows": limit, "sort": "score", "order": "desc", "cursor": "*"})
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 8192:
                raise ValueError("Crossref cursor must be a bounded nonempty string")
            params["cursor"] = cursor
        if self.mailto:
            params["mailto"] = self.mailto
        url = self.endpoint + ("&" if "?" in self.endpoint else "?") + urlencode(params)
        result = _result("crossref", "http_api", url)
        result["metadata"].update({"query": query, "rows": limit, "representation": "metadata",
                                   "match_mode": "exact_doi" if exact_doi else "relevance"})
        request_timeout = self.timeout if request_timeout is None else request_timeout
        deadline = time.monotonic() + request_timeout
        response = None
        body = bytearray()
        try:
            try:
                response = urlopen(Request(url, headers={
                    "User-Agent": "Sci-saurus/0.8 (research metadata client)",
                    "Accept": "application/json",
                }), timeout=request_timeout)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.status
                result["metadata"].update({
                    "http_status": status, "final_url": response.geturl(),
                    "headers": {k.lower(): v for k, v in response.headers.items() if (
                        k.lower().startswith("x-rate-limit") or k.lower() in {
                            "retry-after", "content-type", "x-concurrency-limit", "x-api-pool",
                        }
                    )},
                })
                while len(body) <= self.max_bytes:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Crossref response exceeded the request deadline")
                    chunk = response.read1(min(65536, self.max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
            if len(body) > self.max_bytes:
                del body[self.max_bytes:]
                result["outcome"] = "partial"
                result["gaps"].append("Crossref response exceeded the capture byte limit; no items parsed.")
                result["metadata"]["capture_truncated"] = True
                return result
            if status != 200:
                result["outcome"] = {
                    401: "auth_required", 403: "access_denied", 404: "not_found", 429: "rate_limited",
                }.get(status, "provider_error")
                result["error"] = f"Crossref returned HTTP {status}"
                return result
            payload = json.loads(body)
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise ValueError("Crossref did not return a successful response envelope")
            message = payload.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("items"), list):
                raise ValueError("Crossref response is missing its items list")
            result["raw_response"] = payload
            result["metadata"].update({
                "schema_version": payload.get("message-version"),
                "total_results": message.get("total-results"), "next_cursor": message.get("next-cursor"),
                "result_set_complete": len(message["items"]) < limit,
            })
            for item in message["items"]:
                if not isinstance(item, dict) or not isinstance(item.get("DOI"), str):
                    raise ValueError("Crossref returned an item without a DOI")
                titles = item.get("title", [])
                if not isinstance(titles, list) or any(not isinstance(t, str) for t in titles):
                    raise ValueError("Crossref item title must be a list of strings")
                result["sources"].append({
                    "doi": item["DOI"], "title": "; ".join(titles),
                    "source_url": "https://doi.org/" + item["DOI"],
                    "representation": "metadata", "publisher": item.get("publisher"),
                    "published": item.get("published"), "authors": item.get("author", []),
                    "abstract": item.get("abstract"),
                })
            result["outcome"] = "ok" if result["sources"] else "empty"
            result["text"] = "\n".join(f"{s['title']} — {s['source_url']}" for s in result["sources"])
            result["gaps"].append("Bibliographic metadata does not establish source full text or claim support.")
        except (TimeoutError, URLError) as exc:
            result["outcome"] = "timeout" if isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError) else "provider_error"
            result["error"] = str(exc)
        except (ValueError, UnicodeDecodeError) as exc:
            result["outcome"], result["error"] = "parse_error", str(exc)
            result["sources"] = []
        except HTTPException as exc:
            if isinstance(exc, IncompleteRead):
                body.extend(exc.partial[:max(0, self.max_bytes - len(body))])
            result["outcome"], result["error"] = "provider_error", str(exc)
            result["gaps"].append("HTTP response did not complete; retained bytes are an incomplete capture.")
            result["metadata"]["capture_incomplete"] = True
        except OSError as exc:
            result["outcome"], result["error"] = "provider_error", str(exc)
        finally:
            if response is not None:
                result["capture"] = _capture(bytes(body), response.headers.get("Content-Type", "application/json"))
                result["capture_sha256"] = result["capture"]["sha256"]
            result["metadata"]["completed_at"] = _now()
        return result


class _RetrievalFailure(Exception):
    def __init__(self, outcome: str, message: str):
        super().__init__(message)
        self.outcome = outcome


class _StdioMCP:
    """One bounded MCP session; no shell and no ambient API credentials."""

    def __init__(self, command, timeout, max_bytes, env, own_process_group, cwd=None):
        self.command, self.timeout, self.max_bytes = command, timeout, max_bytes
        self.own_process_group = own_process_group
        self.cwd = cwd
        self.env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_PROCESS_ENV}
        self.env.update(env or {})
        self.env["PYTHONIOENCODING"] = "utf-8"
        if cwd is not None:
            self.env["TMPDIR"] = cwd
        self.messages = queue.Queue(maxsize=8)
        self.stop = threading.Event()
        self.expired = threading.Event()
        self.buffer = bytearray()
        self.stderr = bytearray()
        self.unparsed_stdout = b""
        self.bytes_read = 0
        self.request_id = 0
        self.transcript = []
        self.threads = []

    def __enter__(self):
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, env=self.env, shell=False, cwd=self.cwd,
            start_new_session=self.own_process_group and os.name == "posix",
        )
        self.deadline = time.monotonic() + self.timeout
        self.timer = threading.Timer(self.timeout, self._expire)
        self.timer.daemon = True
        self.timer.start()
        for name, stream in (("stdout", self.process.stdout), ("stderr", self.process.stderr)):
            thread = threading.Thread(target=self._reader, args=(name, stream), daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def _kill(self):
        try:
            if self.own_process_group and os.name == "posix":
                os.killpg(self.process.pid, signal.SIGKILL)
            elif self.process.poll() is None:
                self.process.kill()
        except ProcessLookupError:
            pass

    def _expire(self):
        self.expired.set()
        self._kill()

    def _reader(self, name, stream):
        try:
            while not self.stop.is_set():
                chunk = stream.read(4096)
                while not self.stop.is_set():
                    try:
                        self.messages.put((name, chunk), timeout=0.05)
                        break
                    except queue.Full:
                        pass
                if not chunk:
                    break
        except (OSError, ValueError):
            pass

    def send(self, message):
        data = _json_bytes(message) + b"\n"
        self.transcript.append({"direction": "sent", "message": message})
        try:
            pending = memoryview(data)
            while pending:
                written = self.process.stdin.write(pending)
                if not written:
                    raise BrokenPipeError("MCP server closed stdin")
                pending = pending[written:]
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise _RetrievalFailure("timeout" if self.expired.is_set() else "provider_error", "MCP server closed stdin") from exc

    def receive(self):
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or self.expired.is_set():
                raise _RetrievalFailure("timeout", "MCP session exceeded its deadline")
            if b"\n" in self.buffer:
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    self.unparsed_stdout = bytes(line[:8192])
                    raise _RetrievalFailure("parse_error", "MCP stdout was not newline-delimited JSON") from exc
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise _RetrievalFailure("parse_error", "MCP stdout contained an invalid JSON-RPC envelope")
                if "id" in message and type(message["id"]) not in {int, str}:
                    raise _RetrievalFailure("parse_error", "MCP message ID must be an integer or string")
                self.transcript.append({"direction": "received", "message": message})
                return message
            try:
                name, chunk = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise _RetrievalFailure("timeout", "MCP server did not respond before its deadline") from exc
            if name == "stdout" and not chunk:
                raise _RetrievalFailure("timeout" if self.expired.is_set() else "provider_error", "MCP server exited before a complete response")
            self.bytes_read += len(chunk)
            if self.bytes_read > self.max_bytes:
                raise _RetrievalFailure("partial", "MCP session exceeded its output byte limit")
            if name == "stdout":
                self.buffer.extend(chunk)
            else:
                self.stderr.extend(chunk)
                del self.stderr[:-8192]

    def request(self, method, params=None):
        self.request_id += 1
        request_id = self.request_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        while True:
            message = self.receive()
            if "method" in message:
                if "id" in message:
                    reply = {"jsonrpc": "2.0", "id": message["id"]}
                    if message["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": "Client capability not supported"}
                    self.send(reply)
                continue
            if message.get("id") != request_id:
                raise _RetrievalFailure("parse_error", "MCP response did not match the outstanding request")
            if "error" in message:
                raise _RetrievalFailure("provider_error", json.dumps(message["error"], ensure_ascii=False))
            if not isinstance(message.get("result"), dict):
                raise _RetrievalFailure("parse_error", "MCP response is missing an object result")
            return message["result"]

    def __exit__(self, *_):
        self.timer.cancel()
        self.stop.set()
        # The expiry timer can kill the process while the parent still owns
        # the pipe. Cleanup must not turn the recorded timeout into a generic
        # provider error by surfacing a close race.
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self._kill()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._kill()
        finally:
            # A child may outlive a normally exiting server while holding its pipes.
            if self.own_process_group and os.name == "posix":
                self._kill()
            for stream in (self.process.stdout, self.process.stderr):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            for thread in self.threads:
                thread.join(timeout=0.2)


class MCPFetchClient:
    """Fetch scholarly text through MCP, with bounded Poppler PDF extraction.

    Set ``own_process_group=False`` only inside a supervisor that owns and reaps
    the enclosing process group after every invocation, including normal exits.
    """

    def __init__(
        self, command: list[str], *, timeout: float = 30, max_bytes: int = 1_048_576,
        env: dict | None = None, own_process_group: bool = True, cwd: str | None = None,
        pdf_max_bytes: int = pdf_text.DEFAULT_MAX_PDF_BYTES,
        result_max_bytes: int = pdf_text.DEFAULT_RESULT_MAX_BYTES,
    ):
        _limits(timeout, max_bytes)
        if type(pdf_max_bytes) is not int or not 1 <= pdf_max_bytes <= pdf_text.MAX_PDF_BYTES:
            raise ValueError(f"pdf_max_bytes must be between 1 and {pdf_text.MAX_PDF_BYTES}")
        if type(result_max_bytes) is not int or result_max_bytes < 1:
            raise ValueError("result_max_bytes must be a positive integer")
        if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
            raise ValueError("MCP command must be an executable and an explicit argument list")
        if env is not None and (not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items())):
            raise ValueError("MCP environment must map names to string values")
        if type(own_process_group) is not bool:
            raise ValueError("own_process_group must be a Boolean")
        if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd) or not os.path.isdir(cwd)):
            raise ValueError("MCP cwd must be an existing absolute directory")
        self.command, self.timeout, self.max_bytes, self.env = list(command), timeout, max_bytes, env
        self.own_process_group = own_process_group
        self.cwd = cwd
        self.pdf_max_bytes = pdf_max_bytes
        self.result_max_bytes = result_max_bytes

    @staticmethod
    def _is_pdf_url(url):
        return urlsplit(url).path.casefold().endswith(".pdf")

    def _fetch_pdf(self, url, *, max_length, mcp_probe=None):
        """Retrieve one source as PDF bytes and return verified local text extraction."""
        result = _result("scholarly-pdf", "http_pdf", url)
        result["metadata"].update({
            "adapter_version": ADAPTER_VERSION,
            "representation": "pdf_extracted_text",
            "capture_truncated": False,
            "capture_incomplete": False,
        })
        result["capture"] = _capture(b"", "text/plain; charset=utf-8")
        result["capture_sha256"] = result["capture"]["sha256"]
        extractor = pdf_text.parser_identity()
        record = {
            "request_url": url,
            "final_url": None,
            "http_status": None,
            "content_type": None,
            "content_length": None,
            "download_bytes": 0,
            "source_sha256": None,
            "parser": extractor,
            "mcp_probe": mcp_probe,
        }
        byte_limit = pdf_text.pdf_capture_budget(
            self.result_max_bytes, max_length, self.pdf_max_bytes)
        record.update(result_max_bytes=self.result_max_bytes, effective_byte_limit=byte_limit)
        result["metadata"]["pdf_extraction"] = record
        if byte_limit < 1:
            result.update(outcome="unsupported_capability",
                          error="PDF and extracted text cannot fit the configured worker result limit")
            result["gaps"].append("The PDF was not requested because the bounded result envelope is too small.")
            return result
        if extractor is None:
            record["parser_missing"] = True
            result.update(outcome="unsupported_capability",
                          error="Poppler pdftotext is not installed or could not be identified")
            result["gaps"].append("PDF text extraction is unavailable; no source bytes were requested.")
            return result

        response = None
        body = bytearray()
        deadline = time.monotonic() + float(self.timeout)
        robots_cache = {}
        redirects = []
        try:
            current_url = url
            record["robots_checks"] = []
            for redirect_count in range(MAX_HTTP_REDIRECTS + 1):
                policy = _robots_policy(current_url, deadline=deadline, cache=robots_cache)
                record["robots_checks"].append(policy)
                if policy["outcome"] != "allowed":
                    result.update(outcome=policy["outcome"],
                                  error=policy.get("reason", "Robots policy withheld PDF retrieval"))
                    result["gaps"].append("PDF retrieval was withheld by robots policy or an unavailable policy check.")
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("PDF response exceeded its request deadline")
                request = Request(current_url, headers={
                    "User-Agent": PDF_USER_AGENT,
                    "Accept": "application/pdf, application/octet-stream;q=0.8",
                })
                response = _open_http(request, timeout=remaining)
                status = _http_status(response)
                headers = response.headers
                if status in REDIRECT_STATUSES:
                    location = headers.get("Location") if hasattr(headers, "get") else None
                    if (not isinstance(location, str) or not location.strip()
                            or redirect_count >= MAX_HTTP_REDIRECTS):
                        result.update(outcome="provider_error",
                                      error="PDF redirect is invalid or exceeds five hops")
                        record["redirects"] = redirects
                        response.close()
                        response = None
                        break
                    next_url = urljoin(current_url, location)
                    try:
                        _url(next_url)
                    except ValueError:
                        result.update(outcome="provider_error",
                                      error="PDF redirect target is not an authorized HTTP(S) URL")
                        record["redirects"] = redirects
                        response.close()
                        response = None
                        break
                    redirects.append(_redirect_record(current_url, next_url, status))
                    response.close()
                    response = None
                    current_url = next_url
                    continue

                with response:
                    final_url = _url(response.geturl())
                    content_type = (headers.get_content_type() if hasattr(headers, "get_content_type")
                                    else str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower())
                    content_length = headers.get("Content-Length")
                    try:
                        content_length = int(content_length) if content_length is not None else None
                    except (TypeError, ValueError):
                        content_length = None
                    record.update({
                        "final_url": final_url,
                        "http_status": status,
                        "content_type": content_type or None,
                        "content_length": content_length,
                        "redirects": redirects,
                        "headers": {key.lower(): value for key, value in headers.items()
                                    if key.lower() in {"content-type", "content-length", "last-modified", "etag"}},
                    })
                    response_byte_limit = byte_limit if status == 200 else min(byte_limit, 65536)
                    if content_length is not None and content_length > response_byte_limit:
                        result.update(outcome="partial", error="PDF response exceeds the configured byte limit")
                        result["metadata"]["capture_truncated"] = True
                        record["download_truncated"] = True
                    else:
                        _, truncated = _bounded_http_body(
                            response, byte_limit=response_byte_limit, deadline=deadline, body=body)
                        if truncated:
                            del body[response_byte_limit:]
                            result.update(outcome="partial", error="PDF response exceeds the configured byte limit")
                            result["metadata"]["capture_truncated"] = True
                            record["download_truncated"] = True
                response = None
                break

            captured = bytes(body)
            media_type = record["content_type"] or "application/octet-stream"
            result["capture"] = _capture(captured, media_type)
            result["capture_sha256"] = result["capture"]["sha256"]
            status = record.get("http_status")
            final_url = record.get("final_url")
            if status is not None or captured:
                record["source_capture"] = dict(result["capture"])
                record.update(download_bytes=len(captured), source_sha256=result["capture_sha256"])
            if status != 200:
                if status is not None:
                    result["outcome"] = {
                        401: "auth_required", 403: "access_denied", 404: "not_found", 429: "rate_limited",
                    }.get(status, "provider_error")
                    result["error"] = f"PDF source returned HTTP {status}"
            elif result.get("outcome") == "partial":
                result["sources"] = []
            elif media_type not in {"application/pdf", "application/x-pdf", "application/octet-stream"}:
                result.update(outcome="unsupported_capability",
                              error=f"PDF URL returned non-PDF media type {media_type}")
            else:
                extraction_timeout = deadline - time.monotonic()
                if extraction_timeout <= 0:
                    raise TimeoutError("PDF extraction exceeded the source request deadline")
                extraction = pdf_text.extract_pdf_text(
                    captured, max_chars=max_length, timeout=extraction_timeout, executable=extractor["path"])
                result.update(outcome=extraction["outcome"], text=extraction["text"])
                result["metadata"].update(
                    capture_truncated=extraction["metadata"].get("capture_truncated", False),
                    capture_incomplete=extraction["metadata"].get("capture_incomplete", False))
                record["text_sha256"] = extraction["metadata"].get("text_sha256")
                record["output_bytes"] = extraction["metadata"].get("output_bytes")
                record["parser_returncode"] = extraction["metadata"].get("parser_returncode")
                record["parser_stderr"] = extraction["metadata"].get("parser_stderr")
                if extraction.get("error"):
                    result["error"] = extraction["error"]
                if extraction["outcome"] == "ok":
                    result["sources"] = [{"source_url": final_url,
                                           "representation": "pdf_extracted_text"}]
                else:
                    result["gaps"].append(extraction.get("error") or "PDF text extraction did not complete")
        except (OSError, URLError, TimeoutError, HTTPException, IncompleteRead) as exc:
            result["outcome"] = "timeout" if isinstance(exc, TimeoutError) else "provider_error"
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["gaps"].append("The PDF source did not return a complete HTTP response.")
            captured = bytes(body)
            record["download_incomplete"] = True
            result["metadata"]["capture_incomplete"] = True
            if captured:
                media_type = record["content_type"] or "application/octet-stream"
                result["capture"] = _capture(captured, media_type)
                result["capture_sha256"] = result["capture"]["sha256"]
                record["source_capture"] = dict(result["capture"])
                record.update(download_bytes=len(captured), source_sha256=result["capture_sha256"])
        finally:
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
        text_capture = _capture(result.get("text", "").encode("utf-8"), "text/plain; charset=utf-8")
        result["capture"] = text_capture
        result["capture_sha256"] = text_capture["sha256"]
        return result

    def fetch(self, url: str, *, max_length: int = 20000, start_index: int = 0, raw: bool = False) -> dict:
        _url(url)
        if type(max_length) is not int or not 0 < max_length < 1_000_000 or type(start_index) is not int or start_index < 0 or type(raw) is not bool:
            raise ValueError("fetch requires bounded max_length, nonnegative start_index, and a Boolean raw flag")
        if not raw and self._is_pdf_url(url):
            return self._fetch_pdf(url, max_length=max_length)
        result = _result("mcp-fetch", "mcp_stdio", url)
        result["metadata"].update({
            "command": self.command, "representation": "unclassified",
            "own_process_group": self.own_process_group,
            "cwd": self.cwd,
        })
        session = _StdioMCP(self.command, self.timeout, self.max_bytes, self.env, self.own_process_group, self.cwd)
        try:
            with session:
                initialized = session.request("initialize", {
                    "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "scisaurus", "version": "0.8"},
                })
                version = initialized.get("protocolVersion")
                capabilities = initialized.get("capabilities")
                server_info = initialized.get("serverInfo")
                if not isinstance(version, str) or not isinstance(capabilities, dict) or not isinstance(server_info, dict):
                    raise _RetrievalFailure("parse_error", "MCP initialize returned malformed version or capability metadata")
                if not all(isinstance(server_info.get(k), str) and server_info[k] for k in ("name", "version")):
                    raise _RetrievalFailure("parse_error", "MCP server identity or version is missing")
                if version not in SUPPORTED_PROTOCOL_VERSIONS or "tools" not in capabilities:
                    raise _RetrievalFailure("unsupported_capability", "MCP server has no supported tools protocol")
                result["metadata"].update({"protocol_version": version, "server_info": initialized.get("serverInfo")})
                session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
                cursor, seen = None, set()
                while True:
                    listed = session.request("tools/list", {"cursor": cursor} if cursor else {})
                    if not isinstance(listed.get("tools"), list):
                        raise _RetrievalFailure("parse_error", "MCP tools/list did not return a tools list")
                    tool = next((t for t in listed["tools"] if isinstance(t, dict) and t.get("name") == "fetch"), None)
                    if tool is not None:
                        break
                    cursor = listed.get("nextCursor")
                    if not isinstance(cursor, str) or not cursor or cursor in seen:
                        raise _RetrievalFailure("unsupported_capability", "Configured MCP server does not advertise fetch")
                    seen.add(cursor)
                schema = tool.get("inputSchema")
                if (
                    not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict)
                    or not {"url", "max_length", "start_index", "raw"}.issubset(schema["properties"])
                ):
                    raise _RetrievalFailure("unsupported_capability", "Advertised fetch input schema is incompatible")
                schema_wire = _json_bytes(schema)
                result["metadata"].update({
                    "tool_schema_sha256": hashlib.sha256(schema_wire).hexdigest(),
                    "tool_schema_wire_json": schema_wire.decode("utf-8"),
                })
                reply = session.request("tools/call", {"name": "fetch", "arguments": {
                    "url": url, "max_length": max_length, "start_index": start_index, "raw": raw,
                }})
                result["raw_response"] = reply
                content = reply.get("content")
                if not isinstance(content, list) or any(not isinstance(c, dict) for c in content):
                    raise _RetrievalFailure("parse_error", "MCP fetch did not return content blocks")
                if type(reply.get("isError", False)) is not bool:
                    raise _RetrievalFailure("parse_error", "MCP isError field must be a Boolean")
                text_blocks = [c["text"] for c in content if c.get("type") == "text" and isinstance(c.get("text"), str)]
                result["text"] = "\n".join(text_blocks)
                result["capture"] = _capture(result["text"].encode("utf-8"), "text/plain; charset=utf-8")
                result["capture_sha256"] = result["capture"]["sha256"]
                # The official Fetch server's fetch_url function puts the
                # original media type in this prefix when extraction is skipped.
                # A JSON-RPC text block alone therefore does not prove extraction.
                raw_prefix = "Content type "
                raw_suffix = " cannot be simplified to markdown, but here is the raw content:"
                reported_types = []
                for block in text_blocks:
                    first_line = block.partition("\n")[0]
                    if first_line.startswith(raw_prefix):
                        if not first_line.endswith(raw_suffix):
                            raise _RetrievalFailure("parse_error", "Malformed MCP Fetch representation metadata")
                        reported_types.append(first_line[len(raw_prefix):-len(raw_suffix)])
                result["metadata"]["reported_media_types"] = sorted(set(reported_types))
                result["metadata"]["representation"] = (
                    "raw_tool_text" if raw or reported_types else "extracted_text"
                )
                if reply.get("isError"):
                    result["outcome"], result["error"] = "provider_error", result["text"]
                elif not text_blocks:
                    result["outcome"] = "unsupported_capability" if content else "empty"
                    result["metadata"]["representation"] = "unclassified"
                elif reported_types and not raw:
                    result["outcome"] = "unsupported_capability"
                    result["error"] = "MCP Fetch returned a raw representation without performing the requested extraction"
                    result["gaps"].append("The returned source requires a compatible extractor before it can supply text evidence.")
                else:
                    result["outcome"] = "ok"
                    if len(text_blocks) != len(content) or "<error>" in result["text"]:
                        result["outcome"] = "partial"
                        result["gaps"].append("Fetch returned an extraction/truncation marker or unsupported content block.")
                    result["sources"] = [{"source_url": url, "representation": result["metadata"]["representation"]}]
                result["gaps"].append("Capture contains MCP tool output; original HTTP bytes, final redirect URL, and response headers are not exposed by this server.")
        except _RetrievalFailure as exc:
            result["outcome"], result["error"] = exc.outcome, str(exc)
        except OSError as exc:
            result["outcome"], result["error"] = "provider_error", str(exc)
        finally:
            result["metadata"].update({
                "completed_at": _now(), "transcript": session.transcript,
                "process_returncode": session.process.returncode if hasattr(session, "process") else None,
                "output_bytes": session.bytes_read, "stderr_tail": session.stderr.decode("utf-8", errors="replace"),
                "unparsed_stdout_tail": session.unparsed_stdout.decode("utf-8", errors="replace"),
            })
        reported = result.get("metadata", {}).get("reported_media_types", [])
        if (not raw and result.get("outcome") == "unsupported_capability"
                and any(str(value).split(";", 1)[0].strip().lower() in {
                    "application/pdf", "application/x-pdf"} for value in reported)):
            probe = {
                "outcome": result["outcome"],
                "capture_sha256": result.get("capture_sha256"),
                "capture_bytes": (result.get("capture") or {}).get("bytes"),
                "reported_media_types": result["metadata"].get("reported_media_types", []),
                "protocol_version": result["metadata"].get("protocol_version"),
                "server_info": result["metadata"].get("server_info"),
                "tool_schema_sha256": result["metadata"].get("tool_schema_sha256"),
            }
            return self._fetch_pdf(url, max_length=max_length, mcp_probe=probe)
        return result
