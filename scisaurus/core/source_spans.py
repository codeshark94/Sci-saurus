"""Stable character-span locators for immutable captured source text."""
from __future__ import annotations

import hashlib
from copy import deepcopy

from scisaurus.core.errors import ValidationError


LEGACY_EVIDENCE_FIELDS = {"work_id", "source_ref", "quote"}
SPAN_EVIDENCE_FIELDS = {*LEGACY_EVIDENCE_FIELDS, "start", "end", "quote_sha256"}


def contains_legacy(value) -> bool:
    if isinstance(value, dict):
        return set(value) == LEGACY_EVIDENCE_FIELDS or any(contains_legacy(child) for child in value.values())
    if isinstance(value, list):
        return any(contains_legacy(child) for child in value)
    return False


def quote_sha256(quote: str) -> str:
    if not isinstance(quote, str):
        raise ValidationError("evidence quote must be a string")
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()


def locate(source: dict, quote: str, *, window: dict | None = None) -> dict:
    """Return an unambiguous exact occurrence inside an explicitly supplied window."""
    text = source.get("text")
    if not isinstance(text, str) or not isinstance(quote, str) or not quote:
        raise ValidationError("source text and evidence quote must be nonempty strings")
    start, end = (0, len(text)) if window is None else (window.get("start"), window.get("end"))
    if (type(start) is not int or type(end) is not int
            or not 0 <= start <= end <= len(text)):
        raise ValidationError("source window must be a valid character range")
    position = text.find(quote, start, end)
    if position < 0 or position + len(quote) > end:
        raise ValidationError("evidence quote is absent from the supplied source window")
    if text.find(quote, position + 1, end) >= 0:
        raise ValidationError("evidence quote occurs more than once; provide a longer unique quotation")
    return {"start": position, "end": position + len(quote), "quote_sha256": quote_sha256(quote)}


def bind(value, sources: dict, *, windows: dict | None = None) -> dict:
    """Bind legacy quotation objects to deterministic spans without altering other fields."""
    value = deepcopy(value)
    windows = windows or {}

    def visit(item):
        if isinstance(item, dict):
            if set(item) == LEGACY_EVIDENCE_FIELDS:
                source = sources.get(item.get("source_ref"))
                if source is None or source.get("work_id") != item.get("work_id"):
                    raise ValidationError("evidence identifies an unavailable source or different work")
                try:
                    item.update(locate(source, item.get("quote"), window=windows.get(item["source_ref"])))
                except ValidationError as exc:
                    raise ValidationError(
                        f"evidence for {item.get('work_id')} must quote exact captured text unambiguously: {exc}") from exc
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return value


def validate(proof: dict, source: dict, *, require_span: bool, window: dict | None = None) -> None:
    if not isinstance(proof, dict):
        raise ValidationError("evidence must be an object")
    fields = set(proof)
    if fields == LEGACY_EVIDENCE_FIELDS and not require_span:
        text = source.get("text", "")
        if window is not None:
            start, end = window.get("start"), window.get("end")
            visible = text[start:end] if type(start) is int and type(end) is int else ""
        else:
            visible = text
        if proof.get("quote") not in visible:
            if window is not None:
                raise ValidationError("focused review quotation must be visible in its recorded source window")
            raise ValidationError(
                f"evidence must quote exact captured text ({proof.get('source_ref')})")
        return
    if fields != SPAN_EVIDENCE_FIELDS:
        requirement = sorted(SPAN_EVIDENCE_FIELDS if require_span else LEGACY_EVIDENCE_FIELDS)
        raise ValidationError(f"evidence fields must match a supported contract; stable evidence requires {requirement}")
    start, end = proof.get("start"), proof.get("end")
    text = source.get("text")
    if (not isinstance(text, str) or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(text)):
        raise ValidationError("evidence span must be a valid nonempty character range")
    if text[start:end] != proof.get("quote"):
        raise ValidationError("evidence span does not reproduce the captured source quote")
    if proof.get("quote_sha256") != quote_sha256(proof["quote"]):
        raise ValidationError("evidence quote SHA-256 does not match the exact span text")
    if window is not None:
        window_start, window_end = window.get("start"), window.get("end")
        if (type(window_start) is not int or type(window_end) is not int
                or start < window_start or end > window_end):
            raise ValidationError("evidence span lies outside the displayed source window")
