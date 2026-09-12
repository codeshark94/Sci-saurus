"""Explicit operational adapter contracts used by the project Operations cell."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.runtime import literature, programs, retrieval


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _http_url(value):
    _text(value, "URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError("URL must be HTTP(S) without embedded credentials")
    if len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValidationError("URL is invalid or exceeds the length limit")


def _limits(client):
    for key in ("timeout", "max_bytes"):
        value = client.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
            raise ValidationError(f"An explicit positive finite {key} is required")
    if type(client["max_bytes"]) is not int:
        raise ValidationError("max_bytes must be an integer")


def _crossref_client(client, project_path, environment_files):
    _limits(client)
    if set(client) - {"timeout", "max_bytes", "endpoint", "mailto"}:
        raise ValidationError("Unsupported Crossref client options")
    client.setdefault("endpoint", "https://api.crossref.org/works")
    _http_url(client["endpoint"])
    return client


def _openalex_client(client, project_path, environment_files):
    _limits(client)
    if set(client) - {"timeout", "max_bytes", "endpoint", "auth_env", "max_retries", "retry_backoff_seconds"}:
        raise ValidationError("Unsupported OpenAlex client options")
    client.setdefault("endpoint", literature.DEFAULT_ENDPOINT)
    try:
        literature.OpenAlexClient(**client)
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc
    return client


def _process_client(client, project_path, environment_files, label):
    _limits(client)
    if set(client) - {"timeout", "max_bytes", "command", "env", "own_process_group", "cwd"}:
        raise ValidationError(f"Unsupported {label} client options")
    cwd = Path(_text(client.get("cwd"), f"{label} cwd"))
    if not cwd.is_absolute() or not cwd.is_dir() or not cwd.resolve().is_relative_to(Path(project_path)):
        raise ValidationError(f"{label} cwd must be an existing project-owned directory")
    client["cwd"] = str(cwd.resolve())
    environment = client.get("env", {})
    if not isinstance(environment, dict) or any(
        key not in retrieval.SAFE_PROCESS_ENV or not isinstance(value, str) or "\0" in value
        for key, value in environment.items()
    ):
        raise ValidationError(f"{label} profile environment is limited to nonsecret process settings")
    client["env"] = {key: os.environ.get(key, "") for key in sorted(retrieval.SAFE_PROCESS_ENV)}
    client["env"].update(environment)
    if "own_process_group" in client and type(client["own_process_group"]) is not bool:
        raise ValidationError("own_process_group must be a Boolean")
    if not environment_files:
        raise ValidationError(f"{label} requires explicit pinned environment/package identity files")
    return client


def _mcp_client(client, project_path, environment_files):
    command = client.get("command")
    if (not isinstance(command, list) or len(command) != 3 or command[1:] != ["-m", "mcp_server_fetch"]
            or not isinstance(command[0], str) or not Path(command[0]).is_absolute()
            or not Path(command[0]).is_file() or not os.access(command[0], os.X_OK)):
        raise ValidationError("Official MCP Fetch requires an absolute configured Python executable and -m mcp_server_fetch")
    return _process_client(client, project_path, environment_files, "MCP Fetch")


def _program_client(client, project_path, environment_files):
    client = _process_client(client, project_path, environment_files, "Local program")
    client.setdefault("own_process_group", False)
    if client["own_process_group"]:
        raise ValidationError("Local program processes must remain in the ExecutionRuntime worker group")
    try:
        programs.LocalProgramClient(**client)
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc
    return client


def _retrieval_arguments(arguments, required, size_name, maximum):
    if not isinstance(arguments, dict) or set(arguments) != set(required):
        raise ValidationError(f"Capability arguments require exactly {sorted(required)}")
    size = arguments[size_name]
    if type(size) is not int or not 1 <= size <= maximum:
        raise ValidationError("Capability output limit is invalid")
    return dict(arguments)


def _crossref_arguments(arguments):
    arguments = _retrieval_arguments(arguments, {"query", "limit"}, "limit", 1000)
    if len(_text(arguments["query"], "query")) > 2048:
        raise ValidationError("query exceeds the length limit")
    return arguments


def _openalex_arguments(arguments):
    try:
        return literature.validate_arguments(arguments)
    except (TypeError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc


def _mcp_arguments(arguments):
    arguments = _retrieval_arguments(arguments, {"url", "max_length"}, "max_length", 999999)
    _http_url(arguments["url"])
    return arguments


def _program_arguments(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"input"}:
        raise ValidationError("Capability arguments require exactly ['input']")
    try:
        return {"input": programs.json_object(arguments["input"])}
    except (ValueError, RecursionError) as exc:
        raise ValidationError(str(exc)) from exc


def _http_files(profile):
    return []


def _openalex_files(profile):
    return [str(Path(retrieval.__file__).absolute())]


def _process_files(profile):
    cwd = Path(profile["client"]["cwd"])
    if not cwd.is_dir() or str(cwd.resolve()) != profile["client"]["cwd"]:
        raise ValidationError("Pinned process working directory is unavailable or changed")
    return [profile["client"]["command"][0], str(Path(retrieval.__file__).absolute())]


def _inspect_retrieval(profile, result, params, *, representative=True):
    """Inspect response integrity and suitability for the operation's role.

    Readiness needs representative usable output. A routine bibliographic
    query may complete normally without matching any records.
    """
    checks = []
    def check(name, condition, detail):
        checks.append({"check_id": name, "outcome": "passed" if condition else "failed", "result": detail})
    metadata = result.get("metadata", {})
    catalog = CATALOG[profile["adapter"]]
    empty_search = (not representative and profile["adapter"] == "crossref"
                    and result.get("outcome") == "empty")
    check("outcome", result.get("outcome") == "ok" or empty_search,
          f"Observed outcome: {result.get('outcome')}")
    check("representation", all(metadata.get(k) == v for k, v in catalog.items()),
          "Provider, transport and output representation match the configured adapter")
    check("completeness", not any(metadata.get(k) for k in ("capture_truncated", "capture_incomplete")),
          "Captured response is complete")
    capture = result.get("capture") or {}
    try:
        raw = base64.b64decode(capture["body"], validate=True)
        integrity = (capture["encoding"] == "base64" and capture["bytes"] == len(raw)
                     and sha256_hex(raw) == capture["sha256"] == result.get("capture_sha256"))
    except (KeyError, TypeError, ValueError):
        raw, integrity = b"", False
    check("capture-integrity", integrity and bool(raw), "Captured bytes match declared length and SHA-256")
    sources = result.get("sources")
    check("usable-output", (result.get("text") == "" and sources == []) if empty_search else (
        isinstance(result.get("text"), str) and bool(result["text"].strip())
        and isinstance(sources, list) and bool(sources)),
        "A no-match query has empty text and source locators" if empty_search else "Output contains text and source locators")
    if profile["adapter"] == "crossref":
        schema_identity = {"schema_version": metadata.get("schema_version")}
        try:
            payload = json.loads(raw)
            items = payload["message"]["items"]
            valid = (payload == result["raw_response"] and payload["status"] == "ok"
                     and isinstance(items, list) and (items == [] if empty_search else bool(items))
                     and metadata["http_status"] == 200 and metadata["query"] == params["query"]
                     and metadata["rows"] == params["limit"] and isinstance(schema_identity["schema_version"], str)
                     and bool(schema_identity["schema_version"])
                     and schema_identity["schema_version"] == payload.get("message-version"))
            doi_set = {item["DOI"] for item in items}
            valid = valid and len(sources) == len(items) and all(
                source["doi"] in doi_set and source["source_url"] == "https://doi.org/" + source["doi"]
                and source["representation"] == "metadata" for source in sources)
        except (KeyError, TypeError, ValueError):
            valid = False
        check("crossref-response", valid, "Captured Crossref envelope and source identities match the requested query")
    else:
        schema_identity = {key: metadata.get(key) for key in ("protocol_version", "server_info", "tool_schema_sha256")}
        transcript = metadata.get("transcript", [])
        sent = [item.get("message", {}) for item in transcript if isinstance(item, dict) and item.get("direction") == "sent"]
        received = [item.get("message", {}) for item in transcript if isinstance(item, dict) and item.get("direction") == "received"]
        request_map = {item["id"]: item for item in sent if "id" in item and "method" in item}
        response_map = {item["id"]: item for item in received if "id" in item and "result" in item}
        initialization = [item for item in request_map.values() if item["method"] == "initialize"]
        initialized = response_map.get(initialization[0]["id"], {}).get("result", {}) if len(initialization) == 1 else {}
        listed = [item for item in request_map.values() if item["method"] == "tools/list"]
        tool = next((tool for request in listed for tool in response_map.get(request["id"], {}).get("result", {}).get("tools", [])
                     if isinstance(tool, dict) and tool.get("name") == "fetch"), {})
        schema = tool.get("inputSchema")
        # Artifact bodies canonically order JSON keys. Bind schema semantics;
        # the adapter's wire-order digest remains in the raw execution record.
        digest = sha256_hex(canonical_bytes(schema)) if schema else None
        schema_identity["tool_schema_sha256"] = digest
        calls = [item for item in request_map.values() if item["method"] == "tools/call"]
        expected = {"name": "fetch", "arguments": {"url": params["url"], "max_length": params["max_length"], "start_index": 0, "raw": False}}
        reply = result.get("raw_response", {})
        blocks = reply.get("content", []) if isinstance(reply, dict) else []
        joined = "\n".join(block["text"] for block in blocks if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str))
        check("mcp-protocol", metadata.get("protocol_version") in retrieval.SUPPORTED_PROTOCOL_VERSIONS
              and isinstance(metadata.get("server_info"), dict) and metadata["server_info"].get("name") == "mcp-fetch"
              and bool(metadata["server_info"].get("version")) and digest is not None
              and bool(re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("tool_schema_sha256", ""))))
              and isinstance(schema, dict) and isinstance(schema.get("properties"), dict)
              and {"url", "max_length", "start_index", "raw"}.issubset(schema["properties"])
              and initialized.get("protocolVersion") == metadata["protocol_version"]
              and initialized.get("serverInfo") == metadata["server_info"]
              and "tools" in initialized.get("capabilities", {}) and bool(listed)
              and any(item.get("method") == "notifications/initialized" for item in sent),
              "Negotiated official Fetch server and advertised schema are recorded")
        check("mcp-execution", len(calls) == 1 and calls[0].get("params") == expected
              and response_map.get(calls[0]["id"], {}).get("result") == reply and metadata.get("command") == profile["client"]["command"],
              "Recorded tools/call matches the exact configured command and request")
        check("mcp-output", bool(blocks) and len(blocks) == sum(isinstance(b, dict) and b.get("type") == "text" for b in blocks)
              and not reply.get("isError") and joined == result.get("text") and raw == joined.encode()
              and not metadata.get("reported_media_types") and isinstance(sources, list)
              and sources == [{"source_url": params["url"], "representation": "extracted_text"}],
              "Complete extracted text and source locator match the captured tool response")
    return checks, schema_identity


def _inspect_openalex(profile, result, params, *, representative=True):
    """Reconstruct scholarly records from the byte capture independently of the client."""
    checks = []
    def check(name, condition, detail):
        checks.append({"check_id": name, "outcome": "passed" if condition else "failed", "result": detail})
    metadata = result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    schema_identity = {"schema_version": metadata.get("schema_version")}
    check("representation", all(metadata.get(k) == v for k, v in CATALOG["openalex"].items())
          and metadata.get("adapter_version") == literature.ADAPTER_VERSION
          and schema_identity["schema_version"] == literature.SCHEMA_VERSION,
          "OpenAlex metadata representation and adapter schema are recorded")
    capture = result.get("capture") or {}
    try:
        raw = base64.b64decode(capture["body"], validate=True)
        integrity = (capture["encoding"] == "base64" and type(capture["bytes"]) is int
                     and capture["bytes"] == len(raw) and bool(raw)
                     and sha256_hex(raw) == capture["sha256"] == result.get("capture_sha256")
                     and capture.get("media_type") == metadata["headers"]["content-type"])
    except (KeyError, TypeError, ValueError):
        raw, integrity = b"", False
    check("capture-integrity", integrity, "Captured HTTP body matches its declared length, media type and hashes")
    check("completeness", metadata.get("capture_truncated") is False
          and metadata.get("capture_incomplete") is False and len(raw) <= profile["client"]["max_bytes"],
          "The entire response fits the configured byte limit")

    def require(condition):
        if not condition:
            raise ValueError("Invalid captured OpenAlex response")

    def identifier(value):
        require(isinstance(value, str))
        match = re.fullmatch(r"(?:https://openalex\.org/(?:works/)?|works/)?(W[1-9][0-9]*)", value)
        require(match is not None)
        return match[1]

    def object_pairs(pairs):
        require(len({key for key, _ in pairs}) == len(pairs))
        return dict(pairs)

    def reject_constant(value):
        raise ValueError("Nonfinite JSON value")

    def finite_float(value):
        number = float(value)
        require(math.isfinite(number))
        return number

    try:
        require(isinstance(params, dict))
        if "client" in params:
            require(canonical_bytes(params["client"]) == canonical_bytes(profile["client"]))
        expected = _openalex_arguments({key: value for key, value in params.items() if key != "client"})
        require(canonical_bytes(metadata.get("request")) == canonical_bytes(expected))
        require(type(metadata.get("http_status")) is int and metadata["http_status"] == 200)
        endpoint = profile["client"]["endpoint"]
        if expected["operation"] == "work":
            url = endpoint + "/" + expected["work_id"]
        else:
            wire = {"per_page": expected["limit"], "cursor": expected["cursor"] or "*"}
            wire["search" if expected["operation"] == "search" else "filter"] = (
                expected["query"] if expected["operation"] == "search" else "cites:" + expected["work_id"])
            url = endpoint + "?" + urlencode(wire)
        require(result.get("source_url") == metadata.get("final_url") == url)
        request_valid = True
    except (KeyError, TypeError, ValueError, ValidationError):
        request_valid = False
    check("openalex-request", request_valid, "The exact operation arguments and HTTP URL match the execution context")

    expected_works, expected_sources, expected_text, expected_abstract_gaps = [], [], "", []
    try:
        require(request_valid and integrity)
        payload = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=reject_constant, parse_float=finite_float)
        require(isinstance(payload, dict) and canonical_bytes(payload) == canonical_bytes(result.get("raw_response")))
        if expected["operation"] == "work":
            items = [payload]
            count, cursor = 1, None
        else:
            items, page = payload["results"], payload["meta"]
            require(isinstance(items, list) and isinstance(page, dict))
            require(type(page["count"]) is int and page["count"] >= len(items))
            require(type(page["per_page"]) is int and page["per_page"] == expected["limit"]
                    and len(items) <= expected["limit"])
            count, cursor = page["count"], page["next_cursor"]
            require(cursor is None or (isinstance(cursor, str) and 0 < len(cursor) <= 8192
                                      and not any(ord(c) < 32 for c in cursor)))
            require(cursor is None or (bool(items) and cursor != (expected["cursor"] or "*")))
            require(items or expected["cursor"] not in (None, "*") or count == 0)
        for item in items:
            require(isinstance(item, dict))
            identity, title, year = identifier(item["id"]), item["title"], item["publication_year"]
            require(isinstance(title, str) and bool(title.strip()))
            require(year is None or (type(year) is int and 1 <= year <= 9999))
            doi = item.get("doi")
            if doi is not None:
                require(isinstance(doi, str))
                doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
                require(re.fullmatch(r"10\.[0-9]+/\S+", doi) is not None)
                doi = doi.lower()
            relationships = {}
            for field in ("referenced_works", "related_works"):
                require(isinstance(item[field], list))
                relationships[field] = [identifier(value) for value in item[field]]
            require(expected["operation"] != "work" or identity == expected["work_id"])
            require(expected["operation"] != "citing" or expected["work_id"] in relationships["referenced_works"])
            index, abstract = item.get("abstract_inverted_index"), None
            if index is not None:
                try:
                    require(isinstance(index, dict) and bool(index))
                    tokens = []
                    for word, offsets in index.items():
                        require(isinstance(word, str) and bool(word.strip()) and isinstance(offsets, list) and bool(offsets))
                        require(all(type(offset) is int and offset >= 0 for offset in offsets))
                        tokens.extend((offset, word) for offset in offsets)
                    tokens.sort()
                    require([offset for offset, _ in tokens] == list(range(len(tokens))))
                    abstract = " ".join(word for _, word in tokens)
                except ValueError:
                    expected_abstract_gaps.append({"work_id": identity, "reason": "provider_abstract_index_invalid"})
            locations = []
            require(isinstance(item["locations"], list))
            for location in item["locations"]:
                require(isinstance(location, dict) and type(location.get("is_oa")) is bool)
                require(location.get("version") in (None, "publishedVersion", "acceptedVersion", "submittedVersion"))
                mapped = {"is_oa": location["is_oa"], "version": location.get("version")}
                for field in ("landing_page_url", "pdf_url"):
                    value = location.get(field)
                    if value is not None:
                        _http_url(value)
                    mapped[field] = value
                locations.append(mapped)
            work = {"id": identity, "doi": doi, "title": title, "year": year, "abstract": abstract,
                    **relationships, "locations": locations}
            expected_works.append(work)
            expected_sources.append({"work_id": identity, "doi": doi, "title": title, "year": year, "abstract": abstract,
                                     "source_url": "https://openalex.org/" + identity, "representation": "scholarly_metadata"})
        require(len({work["id"] for work in expected_works}) == len(expected_works))
        require(type(metadata.get("count")) is int and metadata["count"] == count
                and metadata.get("next_cursor") == cursor and type(metadata.get("has_more")) is bool
                and metadata["has_more"] == (cursor is not None)
                and metadata.get("abstract_gaps", []) == expected_abstract_gaps)
        require(canonical_bytes(result.get("works")) == canonical_bytes(expected_works))
        require(canonical_bytes(result.get("sources")) == canonical_bytes(expected_sources))
        expected_text = "\n\n".join(work["title"] + " — https://openalex.org/" + work["id"]
                                    + ("\n" + work["abstract"] if work["abstract"] is not None else "")
                                    for work in expected_works)
        require(result.get("text") == expected_text)
        response_valid = True
    except (KeyError, TypeError, ValueError, ValidationError, RecursionError):
        response_valid = False
    check("openalex-response", response_valid,
          "Normalized works, citation links, abstracts, locations and pagination match independently reconstructed captured JSON")
    empty = response_valid and not expected_works and not representative and params["operation"] in {"search", "citing"}
    check("outcome", response_valid and result.get("outcome") == ("empty" if empty else "ok"),
          f"Observed outcome: {result.get('outcome')}")
    check("usable-output", response_valid and (empty or bool(expected_works and expected_text)),
          "Readiness requires usable scholarly records; a routine no-match page is a valid empty result")
    return checks, schema_identity


def _inspect_program(profile, result, params, *, representative=True):
    checks = []
    def check(name, condition, detail):
        checks.append({"check_id": name, "outcome": "passed" if condition else "failed", "result": detail})
    metadata = result.get("metadata", {})
    check("outcome", result.get("outcome") == "ok", f"Observed outcome: {result.get('outcome')}")
    check("representation", all(metadata.get(key) == value for key, value in CATALOG["local_program"].items()),
          "Provider, transport and output representation match the configured adapter")
    def decode_capture(name, digest_name):
        capture = result.get(name) or {}
        try:
            raw = base64.b64decode(capture["body"], validate=True)
            valid = (capture["encoding"] == "base64" and type(capture["bytes"]) is int
                     and capture["bytes"] == len(raw)
                     and sha256_hex(raw) == capture["sha256"] == result.get(digest_name))
        except (KeyError, TypeError, ValueError):
            raw, valid = b"", False
        return raw, valid
    stdout, stdout_valid = decode_capture("capture", "capture_sha256")
    stderr, stderr_valid = decode_capture("stderr_capture", "stderr_sha256")
    stdin, stdin_valid = decode_capture("input_capture", "input_sha256")
    check("capture-integrity", stdout_valid and stderr_valid and bool(stdout),
          "Exact stdout and stderr bytes match their recorded lengths and hashes")
    expected_input = canonical_bytes(params["input"])
    check("input-binding", stdin_valid and stdin == expected_input and result.get("input") == params["input"]
          and type(metadata.get("input_bytes_written")) is int and metadata["input_bytes_written"] == len(stdin),
          "The entire canonical input object matches the execution context and stdin capture")
    check("completeness", metadata.get("capture_truncated") is False
          and metadata.get("capture_incomplete") is False and len(stdout) + len(stderr) <= profile["client"]["max_bytes"],
          "Stdout and stderr are complete and within the configured shared byte limit")
    try:
        parsed = programs._parse_object(stdout)
        valid_output = (canonical_bytes(parsed) == canonical_bytes(result.get("document"))
                        and canonical_bytes(parsed).decode("utf-8") == result.get("text"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        valid_output = False
    check("usable-output", valid_output, "Stdout contains one JSON object and its exact canonical citation text")
    client = profile["client"]
    identity = programs.command_identity(client["command"], client["cwd"], client["env"])
    check("program-execution", type(metadata.get("process_returncode")) is int
          and metadata["process_returncode"] == 0 and metadata.get("command") == client["command"]
          and metadata.get("cwd") == client["cwd"] and metadata.get("command_identity") == identity
          and metadata.get("own_process_group") == client["own_process_group"],
          "A zero exit status is bound to the configured command, executable, environment and working directory")
    schema_identity = {"protocol_version": metadata.get("protocol_version")}
    check("program-protocol", schema_identity["protocol_version"] == programs.PROTOCOL_VERSION
          and metadata.get("adapter_version") == programs.ADAPTER_VERSION,
          "The configured JSON stdin/stdout protocol and adapter version match")
    return checks, schema_identity


@dataclass(frozen=True)
class OperationAdapter:
    catalog: dict
    dispatch_kind: str
    task_kind: str
    usage_dimension: str
    module_path: str
    version: str
    validate_client: object
    validate_arguments: object
    process_files: object
    inspect_result: object

    def identity_files(self, profile):
        return [str(Path(__file__).absolute()), self.module_path,
                *profile["environment_files"], *self.process_files(profile)]


ADAPTERS = {
    "openalex": OperationAdapter(
        {"provider": "openalex", "transport": "http_api", "representation": "scholarly_metadata"},
        "openalex", "retrieval", "retrieval_calls", str(Path(literature.__file__).absolute()), literature.ADAPTER_VERSION,
        _openalex_client, _openalex_arguments, _openalex_files, _inspect_openalex),
    "crossref": OperationAdapter(
        {"provider": "crossref", "transport": "http_api", "representation": "metadata"},
        "crossref", "retrieval", "retrieval_calls", str(Path(retrieval.__file__).absolute()), retrieval.ADAPTER_VERSION,
        _crossref_client, _crossref_arguments, _http_files, _inspect_retrieval),
    "mcp_fetch": OperationAdapter(
        {"provider": "mcp-fetch", "transport": "mcp_stdio", "representation": "extracted_text"},
        "fetch", "retrieval", "retrieval_calls", str(Path(retrieval.__file__).absolute()), retrieval.ADAPTER_VERSION,
        _mcp_client, _mcp_arguments, _process_files, _inspect_retrieval),
    "local_program": OperationAdapter(
        {"provider": "local-program", "transport": "subprocess_stdio", "representation": "json_object"},
        "program", "service", "program_calls", str(Path(programs.__file__).absolute()), programs.ADAPTER_VERSION,
        _program_client, _program_arguments, _process_files, _inspect_program),
}
CATALOG = {name: adapter.catalog for name, adapter in ADAPTERS.items()}


def get_adapter(name):
    if not isinstance(name, str) or name not in ADAPTERS:
        raise ValidationError("Only explicitly configured registered operational adapters are supported")
    return ADAPTERS[name]
