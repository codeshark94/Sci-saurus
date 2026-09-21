"""Canonical serialization, hashing, and record validation.

Implements the common conventions of docs/40-execution-contract.md §1:
canonical JSON (UTF-8, sorted keys, no insignificant whitespace, no non-finite
numbers), sha256 content addressing, and the ArtifactRef syntax
``artifact:<namespace>/<logical_name>@<positive_integer_version>``.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
import re

from scisaurus.core.errors import ValidationError

SCHEMA_VERSION = "0.8"

# Declared genesis hash for the event chain (40 §11).
GENESIS_HASH = hashlib.sha256(b"scisaurus-genesis-v0.8").hexdigest()

_REF_RE = re.compile(r"^artifact:([A-Za-z0-9_.\-]+)/([A-Za-z0-9_./\-]+)@([1-9][0-9]*)$")

MESSAGE_TYPES = frozenset({"request", "review", "data", "critique", "decision"})

DISPOSITIONS = frozenset(
    {"scheduled", "linked_existing", "deferred", "rejected", "escalated"}
)

TASK_KINDS = frozenset(
    {
        "production",
        "retrieval",
        "review",
        "response",
        "adjudication",
        "verification",
        "selection",
        "human",
        "service",
    }
)

INPUT_PURPOSES = frozenset({"premise", "subject"})
PREMISE_REQUIRED_STATES = frozenset({"accepted", "provisional_allowed"})

# Registered artifact types (40 §3.1: no unknown critical type accepted by
# default). Extensible through register_artifact_type().
ARTIFACT_TYPES: set[str] = {
    "note",
    "results_package",
    "argument",
    "search_campaign",
    "query_record",
    "discovery_record",
    "reference_card",
    "source_capture",
    "evidence_record",
    "coverage_report",
    "claim",
    "critique",
    "response",
    "adjudication",
    "verification",
    "review_coverage",
    "progress_record",
    "supervision_decision",
    "progress_checkpoint",
    "content_unit",
    "document_manifest",
    "change_request",
    "change_set",
    "edit_grant",
    "draft",
    "report",
    "decision_note",
    "release_note",
    "test_input",
}


def register_artifact_type(artifact_type: str) -> None:
    ARTIFACT_TYPES.add(artifact_type)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical_bytes(obj) -> bytes:
    """Canonical serialization: sorted keys, compact, UTF-8, no non-finite numbers."""
    text = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )
    return text.encode("utf-8")


def json_object(raw, name="JSON", *, model_envelope=False) -> dict:
    """Parse one unambiguous object, with optional provider transport wrappers.

    Immutable control records remain plain JSON. Model replies may additionally
    use a single JSON fence or an explicit reasoning terminator; neither form
    permits duplicate keys, non-finite values, comments, or trailing prose.
    """
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def finite(_):
        raise ValueError("nonfinite JSON")

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("nonfinite JSON")
        return value

    candidates = [raw]
    if model_envelope and isinstance(raw, str):
        text = raw.strip()
        candidates = [text]
        if "</think>" in text:
            candidates.append(text.split("</think>", 1)[1].strip())
        for candidate in tuple(candidates):
            lines = candidate.splitlines()
            if (len(lines) >= 3 and lines[0].strip().casefold() in {"```json", "```jsonc"}
                    and lines[-1].strip() == "```"
                    and all(not line.strip().startswith("```") for line in lines[1:-1])):
                candidates.append("\n".join(lines[1:-1]).strip())
    error = None
    for candidate in candidates:
        try:
            value = json.loads(candidate, object_pairs_hook=unique, parse_constant=finite,
                               parse_float=finite_float)
        except (ValueError, TypeError, UnicodeError) as exc:
            error = exc
            continue
        if not isinstance(value, dict):
            raise ValidationError(f"{name} must contain a JSON object")
        return value
    raise ValidationError(f"{name} must contain valid JSON") from error


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_ref(ref: str) -> tuple[str, str, int]:
    """Parse ``artifact:<namespace>/<logical_name>@<version>``."""
    m = _REF_RE.match(ref)
    if not m:
        raise ValidationError(f"malformed artifact ref: {ref!r}")
    return m.group(1), m.group(2), int(m.group(3))


def format_ref(logical_id: str, version: int) -> str:
    return f"artifact:{logical_id}@{version}"


def validate_artifact_manifest(m: dict) -> None:
    """Validate an ArtifactVersion manifest against 40 §3.1."""
    required = (
        "schema_version",
        "artifact_ref",
        "artifact_id",
        "version",
        "artifact_type",
        "owner",
        "author",
        "body_hash",
        "body_media_type",
        "body_size_bytes",
        "parents",
        "inputs",
        "intent_ref",
        "mission_ref",
        "score_ref",
        "task_id",
        "attempt_id",
        "context_ref",
        "created_at",
    )
    missing = [k for k in required if k not in m]
    if missing:
        raise ValidationError(f"artifact manifest missing fields: {missing}")
    if m["schema_version"] != SCHEMA_VERSION:
        raise ValidationError(f"unsupported schema_version: {m['schema_version']!r}")
    if not isinstance(m["version"], int) or m["version"] < 1:
        raise ValidationError("version must be a positive integer")
    ns, name, version = parse_ref(m["artifact_ref"])
    logical = f"{ns}/{m['artifact_id'].split('/', 1)[1]}"
    if m["artifact_id"] != m["artifact_id"].strip() or not m["artifact_id"]:
        raise ValidationError("artifact_id must be a non-empty logical name")
    if m["artifact_ref"] != format_ref(m["artifact_id"], m["version"]):
        raise ValidationError("artifact_ref inconsistent with artifact_id@version")
    if m["artifact_type"] not in ARTIFACT_TYPES:
        raise ValidationError(f"unregistered artifact type: {m['artifact_type']!r}")
    for parent in m["parents"]:
        pns, pid, pversion = parse_ref(parent)
        if f"{pns}/{pid}" != m["artifact_id"]:
            raise ValidationError(f"parent {parent!r} belongs to another logical id")
        if pversion >= m["version"]:
            raise ValidationError(f"parent version must be smaller than {m['version']}")
    for entry in m["inputs"]:
        if not isinstance(entry, dict) or "ref" not in entry or "purpose" not in entry:
            raise ValidationError("inputs entries need 'ref' and 'purpose'")
        if entry["purpose"] not in INPUT_PURPOSES:
            raise ValidationError(f"input purpose must be premise|subject: {entry!r}")
        if entry["purpose"] == "premise":
            state = entry.get("required_state")
            if state not in PREMISE_REQUIRED_STATES:
                raise ValidationError(
                    f"premise input needs required_state accepted|provisional_allowed: {entry!r}"
                )


def validate_message(envelope: dict) -> None:
    """Validate the message envelope against 40 §5.1."""
    if not isinstance(envelope, dict):
        raise ValidationError("message envelope must be an object")
    for key in ("message_id", "project_id", "type", "from", "to", "subject", "body", "refs", "created_at"):
        if key not in envelope or envelope[key] is None:
            raise ValidationError(f"message envelope missing field: {key}")
    for key in ("message_id", "project_id", "type", "subject", "created_at"):
        if not isinstance(envelope[key], str) or not envelope[key].strip():
            raise ValidationError(f"message {key} must be a nonempty string")
    if not isinstance(envelope["body"], str):
        raise ValidationError("message body must be a string")
    for key in ("from", "to"):
        address = envelope[key]
        if not isinstance(address, dict) or any(
            not isinstance(address.get(part), str) or not address[part].strip()
            for part in ("dept", "agent")
        ):
            raise ValidationError(f"message {key} requires a department and agent")
    if envelope["type"] not in MESSAGE_TYPES:
        raise ValidationError(f"unknown message type: {envelope['type']!r}")
    if not isinstance(envelope["refs"], list):
        raise ValidationError("message refs must be a list")
    for ref in envelope["refs"]:
        if not isinstance(ref, str):
            raise ValidationError("message refs must be artifact reference strings")
        parse_ref(ref)
    if envelope.get("idempotency_key") is not None and not isinstance(
        envelope["idempotency_key"], str
    ):
        raise ValidationError("idempotency_key must be a string")


def validate_task_record(task: dict) -> None:
    for key in ("task_id", "kind", "payload", "created_at"):
        if key not in task or task[key] is None:
            raise ValidationError(f"task record missing field: {key}")
    if task["kind"] not in TASK_KINDS:
        raise ValidationError(f"unknown task kind: {task['kind']!r}")
