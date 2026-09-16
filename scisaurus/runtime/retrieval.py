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
from http.client import HTTPException, IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


ADAPTER_VERSION = "3"
MCP_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {MCP_PROTOCOL_VERSION, "2025-06-18", "2025-03-26", "2024-11-05"}
SAFE_PROCESS_ENV = {
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT",
    "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "LANG", "LC_ALL",
}


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
    return url


def _capture(body: bytes, media_type: str) -> dict:
    return {
        "encoding": "base64", "body": base64.b64encode(body).decode("ascii"),
        "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "media_type": media_type,
    }


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
    """Public bibliographic search; one explicitly bounded result page per call."""

    def __init__(
        self, *, mailto: str | None = None, timeout: float = 30,
        max_bytes: int = 1_048_576, endpoint: str = "https://api.crossref.org/works",
    ):
        _limits(timeout, max_bytes)
        self.timeout, self.max_bytes, self.endpoint, self.mailto = timeout, max_bytes, _url(endpoint), mailto

    def search(self, query: str, *, limit: int = 5, cursor: str | None = None) -> dict:
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
        deadline = time.monotonic() + self.timeout
        response = None
        body = bytearray()
        try:
            try:
                response = urlopen(Request(url, headers={
                    "User-Agent": "Sci-saurus/0.8 (research metadata client)",
                    "Accept": "application/json",
                }), timeout=self.timeout)
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
    """Run the configured MCP Fetch server and retain exact tool output provenance.

    Set ``own_process_group=False`` only inside a supervisor that owns and reaps
    the enclosing process group after every invocation, including normal exits.
    """

    def __init__(
        self, command: list[str], *, timeout: float = 30, max_bytes: int = 1_048_576,
        env: dict | None = None, own_process_group: bool = True, cwd: str | None = None,
    ):
        _limits(timeout, max_bytes)
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

    def fetch(self, url: str, *, max_length: int = 20000, start_index: int = 0, raw: bool = False) -> dict:
        _url(url)
        if type(max_length) is not int or not 0 < max_length < 1_000_000 or type(start_index) is not int or start_index < 0 or type(raw) is not bool:
            raise ValueError("fetch requires bounded max_length, nonnegative start_index, and a Boolean raw flag")
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
                result["metadata"]["tool_schema_sha256"] = hashlib.sha256(_json_bytes(schema)).hexdigest()
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
        return result
