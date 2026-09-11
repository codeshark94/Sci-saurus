"""Project-scoped discovery and audited provisioning of operational capabilities."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.runtime.operation_adapters import get_adapter


REGISTRY_ENDPOINT = "https://registry.modelcontextprotocol.io/v0.1/servers"
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _strings(value, name, *, empty=False):
    if not isinstance(value, list) or (not empty and not value) or any(
            not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{name} must be an explicit list of nonempty strings")
    if len(value) != len(set(value)):
        raise ValidationError(f"{name} must not contain duplicates")


def validate_requirement(value):
    _exact(value, {"id", "purpose", "adapter", "tags", "data_classification",
                   "representative", "allowed_recipe_ids"}, "capability requirement")
    _identifier(value["id"], "requirement id")
    if not isinstance(value["purpose"], str) or not value["purpose"].strip():
        raise ValidationError("capability purpose must be explicit")
    get_adapter(value["adapter"])
    _strings(value["tags"], "requirement tags")
    if value["data_classification"] not in {"public", "internal", "confidential"}:
        raise ValidationError("unsupported data classification")
    get_adapter(value["adapter"]).validate_arguments(value["representative"])
    _strings(value["allowed_recipe_ids"], "allowed recipe ids")
    return value


def validate_recipe(value):
    _exact(value, {"id", "adapter", "tags", "data_classifications", "source", "client",
                   "representative", "environment_files"}, "capability recipe")
    _identifier(value["id"], "recipe id")
    get_adapter(value["adapter"])
    _strings(value["tags"], "recipe tags")
    _strings(value["data_classifications"], "recipe data classifications")
    if set(value["data_classifications"]) - {"public", "internal", "confidential"}:
        raise ValidationError("recipe contains an unsupported data classification")
    if not isinstance(value["client"], dict):
        raise ValidationError("recipe client must be an object")
    get_adapter(value["adapter"]).validate_arguments(value["representative"])
    _strings(value["environment_files"], "recipe environment files", empty=True)
    source = value["source"]
    if not isinstance(source, dict) or source.get("kind") not in {"local", "registry", "python_wheels"}:
        raise ValidationError("recipe source must be local, registry, or python_wheels")
    if source["kind"] == "local":
        _exact(source, {"kind"}, "local source")
    elif source["kind"] == "registry":
        _exact(source, {"kind", "server_name", "version"}, "registry source")
        if not all(isinstance(source[key], str) and source[key].strip() for key in ("server_name", "version")):
            raise ValidationError("registry source requires exact server name and version")
    else:
        _exact(source, {"kind", "python", "wheels"}, "python wheel source")
        if not Path(source["python"]).is_absolute() or not Path(source["python"]).is_file():
            raise ValidationError("wheel provisioning requires an absolute Python executable")
        if not isinstance(source["wheels"], list) or not source["wheels"]:
            raise ValidationError("wheel provisioning requires pinned local wheel files")
        for wheel in source["wheels"]:
            _exact(wheel, {"path", "sha256"}, "wheel")
            if (not Path(wheel["path"]).is_absolute() or not Path(wheel["path"]).is_file()
                    or not re.fullmatch(r"[0-9a-f]{64}", wheel["sha256"])):
                raise ValidationError("wheel path and SHA-256 must be exact")
    canonical_bytes(value)
    return value


class MCPRegistryClient:
    """Bounded read-only client for the official preview MCP Registry API."""

    def __init__(self, *, endpoint=REGISTRY_ENDPOINT, timeout=15, max_bytes=2_000_000):
        if endpoint != REGISTRY_ENDPOINT:
            raise ValidationError("MCP discovery endpoint must be the official registry API")
        if (type(max_bytes) is not int or max_bytes < 1 or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValidationError("registry discovery requires positive timeout and byte limits")
        self.endpoint, self.timeout, self.max_bytes = endpoint, timeout, max_bytes

    @staticmethod
    def _json(body):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result
        return json.loads(body, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))

    def search(self, query, *, limit=10):
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValidationError("registry search query is invalid")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("registry search limit must be between 1 and 100")
        url = self.endpoint + "?" + urlencode({"search": query, "version": "latest", "limit": limit})
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "Sci-saurus/0.1"})
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read(self.max_bytes + 1)
            media_type = response.headers.get_content_type()
            status = response.status
        if len(body) > self.max_bytes or status != 200 or media_type != "application/json":
            raise ValidationError("registry response failed its transport bounds")
        payload = self._json(body)
        rows = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValidationError("registry response has no server list")
        candidates = []
        for row in rows:
            server = row.get("server", row) if isinstance(row, dict) else {}
            name, version = server.get("name"), server.get("version")
            if isinstance(name, str) and name and isinstance(version, str) and version:
                candidates.append({"name": name, "version": version,
                                   "description": server.get("description") if isinstance(server.get("description"), str) else None})
        return {"query": query, "url": url, "status": status, "media_type": media_type,
                "capture_sha256": sha256_hex(body), "capture_bytes": len(body),
                "candidates": candidates}


class CapabilityAcquirer:
    """Select an approved recipe, provision it, then require Operations readiness."""

    def __init__(self, operations, *, registry=None):
        self.operations = operations
        self.store = operations.store
        self.registry = registry or MCPRegistryClient()

    def _publish(self, logical_id, body, author, subjects=()):
        return self.store.publish_artifact(
            logical_id=logical_id, artifact_type="discovery_record", author=author,
            body=canonical_bytes(body), media_type="application/json",
            inputs=[{"ref": ref, "purpose": "subject"} for ref in subjects],
        )

    def _provision(self, requirement, recipe):
        workspace = self.operations.workspace_dir(requirement["id"])
        source = recipe["source"]
        python = None
        if source["kind"] == "python_wheels":
            for wheel in source["wheels"]:
                digest = hashlib.sha256(Path(wheel["path"]).read_bytes()).hexdigest()
                if digest != wheel["sha256"]:
                    raise ValidationError("pinned wheel hash changed before provisioning")
            environment = workspace / "venv"
            subprocess.run([source["python"], "-m", "venv", str(environment)], check=True,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
            python = environment / "bin" / "python"
            subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "--disable-pip-version-check",
                            *[wheel["path"] for wheel in source["wheels"]]], check=True,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        replacements = {"{workspace}": str(workspace), "{python}": str(python) if python else ""}
        def resolve(value):
            if isinstance(value, str):
                for marker, replacement in replacements.items():
                    value = value.replace(marker, replacement)
                return value
            if isinstance(value, list):
                return [resolve(item) for item in value]
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            return value
        client = resolve(recipe["client"])
        files = [resolve(path) for path in recipe["environment_files"]]
        if source["kind"] == "python_wheels":
            files.extend(wheel["path"] for wheel in source["wheels"])
        return client, files

    def acquire(self, requirement, recipes, executor, *, engineer="operations.engineer",
                operator="operations.operator", verifier="operations.verifier"):
        requirement = validate_requirement(requirement)
        recipes = [validate_recipe(recipe) for recipe in recipes]
        allowed = set(requirement["allowed_recipe_ids"])
        matches = [recipe for recipe in recipes if recipe["id"] in allowed
                   and recipe["adapter"] == requirement["adapter"]
                   and set(requirement["tags"]).issubset(recipe["tags"])
                   and requirement["data_classification"] in recipe["data_classifications"]]
        if len(matches) != 1:
            raise ValidationError("capability acquisition requires one exact approved recipe match")
        recipe = matches[0]
        subjects = []
        if recipe["source"]["kind"] == "registry":
            discovery = self.registry.search(recipe["source"]["server_name"])
            expected = {"name": recipe["source"]["server_name"], "version": recipe["source"]["version"]}
            if not any(all(candidate.get(key) == value for key, value in expected.items())
                       for candidate in discovery["candidates"]):
                raise ValidationError("approved registry server/version is absent from current discovery")
            subjects.append(self._publish(
                f"command/capability-discovery/{requirement['id']}", discovery,
                "operations.discovery")["artifact_ref"])
        client, environment_files = self._provision(requirement, recipe)
        state = self.operations.register(
            requirement["id"], adapter=recipe["adapter"], client=client,
            representative=requirement["representative"], engineer=engineer,
            environment_files=environment_files)
        state = self.operations.ensure_ready(
            requirement["id"], executor, operator=operator, verifier=verifier,
            purpose=requirement["purpose"])
        if state["state"] != "ready":
            raise ValidationError("acquired capability failed independent operational verification")
        state = self.operations.idle(requirement["id"])
        record = self._publish(f"command/capability-acquisitions/{requirement['id']}", {
            "schema_version": "capability-acquisition-1", "requirement": requirement,
            "recipe_id": recipe["id"], "profile_ref": state["profile_ref"],
            "verification_ref": state["verification_ref"], "binding": state["binding"],
            "domain_acceptance": "not_assessed",
        }, "operations.controller", subjects=[*subjects, state["profile_ref"], state["verification_ref"]])
        return {"state": state, "acquisition_ref": record["artifact_ref"]}
