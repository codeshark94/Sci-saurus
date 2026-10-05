"""Source-following discovery with immutable captures and public-only retrieval."""
from __future__ import annotations

from contextlib import closing
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import os
import json
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlencode
from urllib.request import Request

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes, parse_ref
from scisaurus.runtime import retrieval, pdf_text


def digest(body):
    return hashlib.sha256(body).hexdigest()


def source_links(text, base_url=""):
    links = []
    for value in re.findall(r'https?://[^\s<>"\[\]\x00-\x1f]+', text):
        value = value.rstrip(".,;:}")
        while value.endswith(")") and value.count(")") > value.count("("):
            value = value[:-1]
        links.append(urljoin(base_url,value))
    return list(dict.fromkeys(links))


def accepted_survey_sources(project_dir):
    """Read only the exact work/source versions bound to an accepted survey."""
    root = Path(project_dir).resolve()
    database = root / "state/control.sqlite"
    if not database.is_file():
        return {"status":"unavailable", "sources":[], "reason":"survey artifact store is absent"}
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True)) as db:
        def read(ref):
            namespace, name, version = parse_ref(ref)
            row = db.execute("SELECT manifest_json FROM artifacts WHERE logical_id=? AND version=?",
                             (namespace+"/"+name, version)).fetchone()
            if row is None:
                raise ValidationError("accepted survey source reference is missing")
            manifest = json.loads(row[0])
            if not isinstance(manifest.get("body_hash"),str) or not re.fullmatch(r"[0-9a-f]{64}",manifest["body_hash"]):
                raise ValidationError("accepted survey artifact has an invalid body identity")
            body = (root / "objects/sha256" / manifest["body_hash"]).read_bytes()
            if digest(body) != manifest["body_hash"] or manifest["artifact_ref"] != ref:
                raise ValidationError("accepted survey source lost its immutable identity")
            return manifest, json.loads(body)

        row = db.execute("SELECT accepted_version FROM accepted_heads WHERE logical_id='kb/surveys/current'").fetchone()
        if row is None:
            return {"status":"unavailable", "sources":[], "reason":"survey has no accepted source bundle"}
        bundle_ref = f"artifact:kb/surveys/current@{row[0]}"
        bundle_manifest, bundle = read(bundle_ref)
        if bundle.get("schema_version") not in {"literature-survey-2", "literature-survey-3"}:
            raise ValidationError("software discovery requires a versioned accepted survey bundle")
        works = {}
        for ref in bundle.get("work_refs", []):
            _, body = read(ref)
            works[body["work_id"]] = body
        sources = []
        for ref in bundle.get("source_refs", []):
            manifest, body = read(ref)
            if not isinstance(body.get("text"), str):
                raise ValidationError("accepted survey capture has no source text")
            work = works.get(body.get("work_id"), {})
            sources.append({"origin_ref":ref, "origin_sha256":manifest["body_hash"],
                "bundle_ref":bundle_ref, "bundle_sha256":bundle_manifest["body_hash"],
                "work_id":body.get("work_id"), "title":work.get("title"), "doi":work.get("doi"),
                "url":body.get("url"), "representation":body.get("representation"),
                "identity_verified":body.get("identity_verified"), "text":body["text"]})
        return {"status":"available", "bundle_ref":bundle_ref,
                "bundle_sha256":bundle_manifest["body_hash"], "sources":sources}


def retain_sources(root, sources):
    directory = Path(root) / "evidence"
    directory.mkdir(parents=True, exist_ok=True)
    catalog = []
    for source in sources:
        body = canonical_bytes(source)
        sha = digest(body)
        path = directory / (sha+".json")
        if path.exists() and path.read_bytes() != body:
            raise ValidationError("software evidence snapshot changed")
        path.write_bytes(body)
        catalog.append({"source_ref":"software-evidence:sha256:"+sha,
            **{key:source.get(key) for key in ("origin_ref","origin_sha256","bundle_ref","bundle_sha256",
                "work_id","title","doi","url","representation","identity_verified")},
            "text_chars":len(source["text"])})
    return catalog


class DiscoveryFailure(ValidationError):
    def __init__(self, record):
        self.record = record
        super().__init__(record.get("error") or record.get("outcome") or "source discovery failed")


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, hostname, address, timeout):
        super().__init__(hostname, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def public_url(url):
    try:
        retrieval._url(url)
    except (ValueError,TypeError) as exc:
        raise ValidationError("source URL must be HTTPS without embedded credentials") from exc
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.port not in (None, 443):
        raise ValidationError("source retrieval requires public HTTPS on the standard port")
    if parsed.hostname.endswith("."):
        raise ValidationError("source hostname must use its canonical public form")
    return parsed


def public_addresses(hostname):
    addresses = list(dict.fromkeys(row[4][0] for row in socket.getaddrinfo(hostname,443,type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise ValidationError("source retrieval rejects private, loopback and non-public destinations")
    return addresses


class _HTTPResponse:
    def __init__(self, connection, response):
        self.connection, self.response = connection, response
        self.status, self.headers = response.status, response.headers
        self.fp = response.fp

    def read(self, size):
        return self.response.read(size)

    def read1(self, size):
        return self.response.read1(size)

    def close(self):
        self.response.close()
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _HTMLSource(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self.hidden = [], [], 0
        self.search_hits, self.anchor = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script","style","noscript"}:
            self.hidden += 1
        if tag in {"p","div","br","li","h1","h2","h3","pre","tr"}:
            self.text.append("\n")
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
            if "result__a" in attrs.get("class","").split():
                self.anchor = {"url":attrs["href"],"title":""}

    def handle_endtag(self, tag):
        if tag in {"script","style","noscript"} and self.hidden:
            self.hidden -= 1
        if tag == "a" and self.anchor is not None:
            self.search_hits.append(self.anchor)
            self.anchor = None

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)
            if self.anchor is not None:
                self.anchor["title"] += data


class PublicSourceClient:
    def __init__(self, root, *, deadline, opener=None):
        self.root, self.deadline = Path(root), deadline
        self.opener = opener or self._open
        self.robots = {}

    def _capture(self, body):
        sha = digest(body)
        path = self.root/"captures"/sha
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and path.read_bytes() != body:
            raise ValidationError("source capture lost its content identity")
        path.write_bytes(body)
        return sha

    def _transport_failure(self, record, exc):
        partial = getattr(exc,"partial",b"")
        retained = {"partial_segment_sha256":self._capture(partial),"partial_segment_bytes":len(partial)} if isinstance(partial,bytes) and partial else {}
        return DiscoveryFailure({**record,**retained,"outcome":"transport_error",
            "error_type":type(exc).__name__,"error":str(exc),"complete_source_available":False})

    def _open(self, request, *, timeout):
        parsed = public_url(request.full_url)
        addresses = public_addresses(parsed.hostname)
        connection = _PinnedHTTPS(parsed.hostname, addresses[0], timeout)
        path = parsed.path or "/"
        if parsed.query:
            path += "?"+parsed.query
        headers = {"User-Agent":retrieval.PDF_USER_AGENT,"Accept":"text/html,application/xml,text/plain,application/pdf"}
        # Only an explicitly selected search service receives its own credential.
        for name,value in request.header_items():
            if name.lower() == "x-subscription-token" and parsed.hostname == "api.search.brave.com":
                headers[name] = value
        try:
            from scisaurus.runtime.run_control import dispatch_permission
            with dispatch_permission():
                connection.request("GET",path,headers=headers)
            return _HTTPResponse(connection,connection.getresponse())
        except BaseException:
            connection.close()
            raise

    def fetch(self, url, *, check_robots=True):
        public_url(url)
        record = {"request_url":url,"redirects":[],"robots_checks":[],"outcome":"failed"}
        try:
            return self._fetch(url,record,check_robots=check_robots)
        except (OSError,http.client.HTTPException) as exc:
            raise self._transport_failure(record,exc) from exc

    def _fetch(self, url, record, *, check_robots):
        current = url
        for hop in range(retrieval.MAX_HTTP_REDIRECTS+1):
            public_url(current)
            if check_robots:
                policy = retrieval._robots_policy(current,deadline=self.deadline,cache=self.robots,http_open=self.opener)
                record["robots_checks"].append(policy)
                if policy["outcome"] != "allowed":
                    raise DiscoveryFailure({**record,"outcome":policy["outcome"],"error":policy.get("reason"),
                        "retry_after":policy.get("retry_after")})
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                raise DiscoveryFailure({**record,"outcome":"timeout","error":"source request exceeded its deadline"})
            with self.opener(Request(current),timeout=remaining) as response:
                status = response.status
                record.update(final_url=current,http_status=status)
                if status in retrieval.REDIRECT_STATUSES:
                    target = urljoin(current,response.headers.get("Location", ""))
                    if target == current or hop == retrieval.MAX_HTTP_REDIRECTS:
                        raise DiscoveryFailure({**record,"error":"source redirect is invalid or exceeds the supported hops"})
                    public_url(target)
                    record["redirects"].append({"from":current,"to":target,"status":status})
                    current = target
                    continue
                body,truncated = retrieval._bounded_http_body(response,byte_limit=8*1024*1024,deadline=self.deadline)
                sha = self._capture(body)
                record.update(capture_sha256=sha,capture_bytes=len(body),capture_truncated=truncated,
                    content_type=response.headers.get("Content-Type",""),
                    retry_after=response.headers.get("Retry-After"))
                if status != 200 or truncated:
                    outcome = "partial" if truncated else {401:"auth_required",403:"access_denied",404:"not_found",429:"rate_limited",202:"challenge"}.get(status,"provider_error")
                    raise DiscoveryFailure({**record,"outcome":outcome,"error":f"source response HTTP {status}; complete source unavailable"})
                return record,body
        raise DiscoveryFailure({**record,"error":"source redirect did not resolve"})

    def read(self, url, *, start=0, max_chars=32000, capture=None):
        if capture is None:
            record,body = self.fetch(url)
        else:
            record = {key:capture[key] for key in ("request_url","redirects","robots_checks","final_url",
                "http_status","capture_sha256","capture_bytes","capture_truncated","content_type","retry_after")}
            if record["request_url"] != url:
                raise ValidationError("source pagination changed its captured URL")
            body = (self.root/"captures"/record["capture_sha256"]).read_bytes()
            if digest(body) != record["capture_sha256"]:
                raise ValidationError("source pagination capture changed")
        media = record["content_type"].split(";",1)[0].casefold()
        if media in {"application/pdf","application/x-pdf"}:
            extraction = pdf_text.extract_pdf_text(body,max_chars=999999,timeout=max(.01,self.deadline-time.monotonic()))
            if extraction["outcome"] != "ok":
                raise DiscoveryFailure({**record,"outcome":extraction["outcome"],"error":extraction.get("error")})
            text,links,representation = extraction["text"],[],"pdf_extracted_text"
        elif media.startswith("text/") or media in {"application/xml","application/xhtml+xml","application/json"}:
            charset = re.search(r"charset=([\w-]+)",record["content_type"],re.I)
            try:
                raw = body.decode(charset[1] if charset else "utf-8",errors="replace")
            except LookupError as exc:
                raise DiscoveryFailure({**record,"outcome":"parse_error","error":"unsupported source character encoding"}) from exc
            if "html" in media:
                parser = _HTMLSource(); parser.feed(raw)
                text = re.sub(r"\n[ \t\n]+","\n", "".join(parser.text))
                links = list(dict.fromkeys(urljoin(record["final_url"],value) for value in parser.links))
                representation = "html_extracted_text"
            else:
                text,links,representation = raw,[],"source_text"
        else:
            raise DiscoveryFailure({**record,"outcome":"unsupported_capability","error":"source is not readable text or PDF"})
        return {**record,"outcome":"ok","representation":representation,"text":text[start:start+max_chars],
                "text_sha256":digest(text.encode()),"total_chars":len(text),"start":start,
                "next_start":start+max_chars if start+max_chars<len(text) else None,
                "complete":start==0 and len(text)<=max_chars,
                "links":list(dict.fromkeys([*links,*source_links(text,record["final_url"])])),
                "evidence_status":"unreviewed source capture; not a verified scientific claim"}

    def search(self, query):
        try:
            return self._search(query)
        except (OSError,http.client.HTTPException) as exc:
            raise self._transport_failure({"query":query,"operation":"search_web"},exc) from exc

    def _search(self, query):
        token = os.environ.get("BRAVE_SEARCH_API_KEY")
        if token:
            return self._brave_search(query, token)
        url = "https://html.duckduckgo.com/html/?"+urlencode({"q":query})
        record,body = self.fetch(url)
        parser = _HTMLSource(); parser.feed(body.decode("utf-8",errors="replace"))
        text = "".join(parser.text)
        if not parser.search_hits and "No results" not in text:
            raise DiscoveryFailure({**record,"outcome":"parse_error","error":"search page contained neither results nor a verified empty-result marker"})
        from urllib.parse import parse_qs
        discoveries = []
        for hit in parser.search_hits:
            target = urljoin(url,hit["url"])
            redirect = parse_qs(urlsplit(target).query).get("uddg")
            target = redirect[0] if redirect else target
            discoveries.append({"url":target,"title":hit["title"].strip()})
        return {**record,"outcome":"ok","provider":"DuckDuckGo public HTML","query":query,
                "discoveries":discoveries,"coverage":"one returned result page; non-exhaustive, snippets are not source evidence",
                "complete_index_coverage":False}

    def _brave_search(self, query, token):
        url = "https://api.search.brave.com/res/v1/web/search?"+urlencode({"q":query})
        record = {"request_url":url,"provider":"Brave Search API","query":query}
        try:
            return self._brave_response(url,token,record)
        except (OSError,http.client.HTTPException) as exc:
            raise self._transport_failure(record,exc) from exc

    def _brave_response(self, url, token, record):
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise DiscoveryFailure({"outcome":"timeout", "error":"search deadline expired"})
        with self.opener(Request(url, headers={"X-Subscription-Token":token}),timeout=remaining) as response:
            record.update(http_status=response.status,retry_after=response.headers.get("Retry-After"))
            body,truncated = retrieval._bounded_http_body(response,byte_limit=8*1024*1024,deadline=self.deadline)
            sha = self._capture(body)
            record.update(capture_sha256=sha,capture_bytes=len(body))
            if response.status != 200 or truncated:
                raise DiscoveryFailure({**record,"outcome":"partial" if truncated else "provider_error",
                                        "error":f"search response HTTP {response.status}"})
            try:
                payload = json.loads(body)
                if not isinstance(payload,dict) or not isinstance(payload.get("web"),dict) or "results" not in payload["web"]:
                    raise ValueError("missing web results envelope")
                hits = payload["web"]["results"]
                if not isinstance(hits,list) or any(not isinstance(hit,dict) or not isinstance(hit.get("url"),str) for hit in hits):
                    raise ValueError("invalid search results")
            except (ValueError,AttributeError) as exc:
                raise DiscoveryFailure({**record,"outcome":"parse_error","error":"search API returned an invalid result envelope"}) from exc
            return {**record,"outcome":"ok",
                    "discoveries":[{"url":hit["url"],"title":hit.get("title"),"description":hit.get("description")} for hit in hits],
                    "coverage":"returned search results; non-exhaustive, snippets are not source evidence",
                    "complete_index_coverage":False}
