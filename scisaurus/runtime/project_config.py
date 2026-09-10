"""Project document, claim, and operational capability boundaries."""
from __future__ import annotations

import json
import re
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.config import _text, validate_common
from scisaurus.runtime.contracts import preserves_literals

_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _object(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError("identifiers must be lowercase letters, digits, underscores, or hyphens")


def paragraphs(config):
    return [unit for section in config["document"]["sections"] for unit in section["paragraphs"]]


def validate_project_config(value):
    if isinstance(value, dict) and "score" in value:
        from scisaurus.runtime.scores import validate_scored_config
        return validate_scored_config(value)
    validate_common(value, {"document", "claims", "operations"})
    if value["limits"]["concurrent_calls"] < 3:
        raise ValidationError("project capacity must cover two parallel workers and reserved independent verification")
    _object(value.get("document"), {"title", "sections"}, "document")
    _text(value["document"]["title"], "document.title")
    sections = value["document"]["sections"]
    if not isinstance(sections, list) or not sections:
        raise ValidationError("document.sections must be a nonempty list")
    claims = value.get("claims")
    if not isinstance(claims, list):
        raise ValidationError("claims must be an explicit list")
    claim_ids = set()
    for claim in claims:
        _object(claim, {"id", "statement"}, "claim")
        _identifier(claim["id"])
        _text(claim["statement"], "claim.statement")
        if claim["id"] in claim_ids:
            raise ValidationError("duplicate claim identifier")
        claim_ids.add(claim["id"])
    unit_ids = set()
    for section in sections:
        _object(section, {"id", "title", "paragraphs"}, "section")
        _identifier(section["id"])
        _text(section["title"], "section.title")
        if section["id"] in unit_ids:
            raise ValidationError("duplicate structural unit identifier")
        unit_ids.add(section["id"])
        if not isinstance(section["paragraphs"], list) or not section["paragraphs"]:
            raise ValidationError("each section requires paragraphs")
        for unit in section["paragraphs"]:
            _object(unit, {"id", "text", "editable", "objective", "required_literals", "claim_ids"}, "paragraph")
            _identifier(unit["id"])
            if unit["id"] in unit_ids:
                raise ValidationError("duplicate paragraph assignment or structural identifier")
            unit_ids.add(unit["id"])
            _text(unit["text"], "paragraph.text")
            if any(c in unit["text"] for c in "\r\n\u2028\u2029"):
                raise ValidationError("a paragraph must be one single-line structural unit")
            if type(unit["editable"]) is not bool:
                raise ValidationError("editable must be an explicit Boolean")
            if unit["editable"]:
                _text(unit["objective"], "paragraph.objective")
            elif unit["objective"] is not None:
                raise ValidationError("an immutable paragraph cannot have a revision objective")
            if not isinstance(unit["required_literals"], list):
                raise ValidationError("paragraph required_literals must be a list")
            for literal in unit["required_literals"]:
                _text(literal, "required_literal")
            if not preserves_literals(unit["text"], unit["required_literals"]):
                raise ValidationError("required literals must match complete baseline values in their own paragraph")
            refs = unit["claim_ids"]
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                raise ValidationError("claim_ids must be an explicit list of local claim identifiers")
            if len(set(refs)) != len(refs) or set(refs) - claim_ids:
                raise ValidationError("unknown, foreign, or duplicated claim binding")
    if sum(unit["editable"] for unit in paragraphs(value)) < 2:
        raise ValidationError("a parallel project requires at least two editable paragraph assignments")
    _object(value.get("operations"), {"environment_files"}, "operations")
    files = value["operations"]["environment_files"]
    if not isinstance(files, list) or any(not isinstance(path, str) or not Path(path).is_absolute() for path in files):
        raise ValidationError("operational environment files must be explicit absolute paths")
    return value


def load_project_config(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("project configuration must be a readable JSON file") from exc
    return validate_project_config(value)
