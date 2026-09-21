"""Stable character-span locators for immutable captured source text."""
from __future__ import annotations

import hashlib
from copy import deepcopy
import re
import unicodedata

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


LEGACY_EVIDENCE_FIELDS = {"work_id", "source_ref", "quote"}
SPAN_EVIDENCE_FIELDS = {*LEGACY_EVIDENCE_FIELDS, "start", "end", "quote_sha256"}


def contains_legacy(value) -> bool:
    if isinstance(value, dict):
        return set(value) == LEGACY_EVIDENCE_FIELDS or any(contains_legacy(child) for child in value.values())
    if isinstance(value, list):
        return any(contains_legacy(child) for child in value)
    return False


def evidence_id(proof):
    return "ev-" + hashlib.sha256(canonical_bytes(proof)).hexdigest()[:24]


def index_evidence(value, sources):
    """Replace repeated exact quotations with stable IDs in a model packet."""
    bound = bind(value, sources)
    catalog = {}

    def visit(item):
        if isinstance(item, dict):
            if set(item) == SPAN_EVIDENCE_FIELDS:
                identifier = evidence_id(item)
                catalog[identifier] = {"evidence_id": identifier, **item}
                return {"evidence_id": identifier}
            return {key: visit(child) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    projected = visit(bound)
    return projected, [catalog[key] for key in sorted(catalog)]


def expand_evidence(value, catalog, sources, *, windows=None):
    """Replay evidence IDs only against exact, visible, content-bound records."""
    if not isinstance(catalog, list):
        raise ValidationError("evidence catalog must be a list")
    lookup = {}
    for item in catalog:
        if not isinstance(item, dict) or set(item) != SPAN_EVIDENCE_FIELDS | {"evidence_id"}:
            raise ValidationError("evidence catalog entries require exact spans and an ID")
        proof = {key: item[key] for key in SPAN_EVIDENCE_FIELDS}
        identifier = item["evidence_id"]
        if identifier != evidence_id(proof) or identifier in lookup:
            raise ValidationError("evidence catalog ID is duplicated or does not match its content")
        source = sources.get(proof["source_ref"])
        if source is None or source.get("work_id") != proof["work_id"]:
            raise ValidationError("evidence catalog identifies an unavailable source or different work")
        validate(proof, source, require_span=True,
                 window=(windows or {}).get(proof["source_ref"]))
        lookup[identifier] = proof

    def visit(item):
        if isinstance(item, dict):
            if "evidence_id" in item:
                if set(item) != {"evidence_id"} or not isinstance(item["evidence_id"], str) or item["evidence_id"] not in lookup:
                    raise ValidationError("evidence selection must identify exactly one supplied evidence ID")
                return deepcopy(lookup[item["evidence_id"]])
            return {key: visit(child) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    return visit(value)


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


def _restore_unique_source_whitespace(source: dict, quote: str, *, window: dict | None = None) -> str:
    """Return the exact source slice for one whitespace-equivalent quotation.

    JSON-producing models commonly collapse a rendered line break into a space.  That
    transport difference is safe to repair only when every non-whitespace character
    is unchanged and the resulting pattern identifies one source span.
    """
    text = source.get("text")
    if not isinstance(text, str) or not isinstance(quote, str) or not quote or not quote.strip():
        raise ValidationError("source text and evidence quote must be nonempty strings")
    start, end = (0, len(text)) if window is None else (window.get("start"), window.get("end"))
    if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(text)):
        raise ValidationError("source window must be a valid character range")
    parts = re.split(r"\s+", quote.strip())

    # Renderers commonly replace ASCII punctuation with typographic
    # equivalents while preserving the scientific wording.  Treat only the
    # small, unambiguous punctuation family below as equivalent; return the
    # original source slice so the stored span remains byte-faithful.
    equivalents = {
        "'": "'’‘ʻʼ",
        '"': '"“”„‟',
        "-": "-‐‑‒–—−",
    }

    def pattern_part(part):
        rendered = []
        for char in part:
            if char in equivalents:
                rendered.append("[" + re.escape(equivalents[char]) + "]")
            else:
                rendered.append(re.escape(char))
        return "".join(rendered)

    pattern = re.compile(r"\s+".join(pattern_part(part) for part in parts))
    matches = list(pattern.finditer(text, start, end))
    if len(matches) == 1:
        return matches[0].group(0)
    if matches:
        raise ValidationError("evidence quote occurs more than once; provide a longer unique quotation")

    # Formula renderers may additionally duplicate a TeX expression in a
    # plain-text form, change Unicode symbols, or escape an underscore.  A
    # token-level fallback handles those transport differences while still
    # requiring one unique contiguous source span.  It deliberately ignores
    # punctuation only after the whitespace-aware match has failed; ordinary
    # prose therefore keeps the stricter character contract above.
    def token_spans(value):
        chars, starts, ends = [], [], []
        for index, char in enumerate(value):
            normalized = unicodedata.normalize("NFKC", char).casefold()
            normalized = normalized.translate(str.maketrans({
                "’": "'", "‘": "'", "ʻ": "'", "ʼ": "'",
                "“": '"', "”": '"', "„": '"', "‟": '"',
                "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
            }))
            for rendered in normalized:
                chars.append(rendered)
                starts.append(index)
                ends.append(index + 1)
        normalized_text = "".join(chars)
        return [(match.group(0), starts[match.start()], ends[match.end() - 1])
                for match in re.finditer(r"\w+", normalized_text, flags=re.UNICODE)]

    expected = [token[0] for token in token_spans(quote)]
    observed = token_spans(text)
    if len(expected) >= 2:
        candidates = []
        for offset in range(len(observed) - len(expected) + 1):
            window_tokens = observed[offset:offset + len(expected)]
            if [token[0] for token in window_tokens] != expected:
                continue
            if window_tokens[0][1] < start or window_tokens[-1][2] > end:
                continue
            candidates.append(window_tokens)
        if len(candidates) == 1:
            return text[candidates[0][0][1]:candidates[0][-1][2]]

    # HTML/MathJax-to-Markdown extractors sometimes retain both the rendered
    # Unicode symbol and its TeX spelling (``ξ\\xi``), or duplicate a
    # one-letter variable at a word boundary (``ff``).  A model that copies
    # the rendered sentence may therefore omit the transport alias while
    # preserving every scientific word.  Match those two representation-only
    # artifacts as alternatives, but still return the exact original slice
    # and require one unique candidate inside the displayed window.
    math_aliases = {
        "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon",
        "θ": "theta", "λ": "lambda", "μ": "mu", "π": "pi", "σ": "sigma",
        "τ": "tau", "φ": "phi", "ω": "omega", "ξ": "xi", "∞": "infty",
        "≤": "leq", "≥": "geq", "≠": "ne", "∈": "in", "∉": "notin",
        "∥": "Vert", "→": "to", "⇒": "Rightarrow",
    }

    def transport_tokens(value, *, source_text=False):
        """Tokenize with representation aliases retained as span metadata.

        The returned token value is deliberately conservative: only a TeX
        alias immediately following its rendered Unicode symbol is collapsible,
        and only a standalone duplicated one-letter variable is collapsible.
        Ordinary prose and repeated letters inside words remain unchanged.
        """
        tokens = token_spans(value)
        if not source_text:
            # A rendered Unicode symbol is equivalent to its named math
            # token for transport matching.  This affects only the explicit
            # symbol characters below; ordinary words such as ``xi`` remain
            # ordinary words unless they occur as a source-side TeX alias.
            return [(f"__math_{math_aliases[token].casefold()}__" if token in math_aliases else token,
                     start, end) for token, start, end in tokens]
        collapsed = []
        index = 0
        while index < len(tokens):
            token, start, end = tokens[index]
            canonical = token
            span_end = end
            if token in math_aliases and index + 1 < len(tokens):
                alias, alias_start, alias_end = tokens[index + 1]
                between = value[end:alias_start]
                if re.fullmatch(r"\s*\\?", between) and alias.casefold() == math_aliases[token].casefold():
                    canonical = f"__math_{math_aliases[token].casefold()}__"
                    span_end = alias_end
                    index += 1
            if (len(token) == 2 and token[0] == token[1]
                    and token.isascii() and token.isalpha()
                    and (start == 0 or not value[start - 1].isalnum())
                    and (end == len(value) or not value[end].isalnum())):
                canonical = token[0]
            collapsed.append((canonical, start, span_end))
            index += 1
        return collapsed

    expected_transport = transport_tokens(quote)
    observed_transport = transport_tokens(text, source_text=True)
    expected_values = [token for token, _, _ in expected_transport]
    candidates = []
    for offset in range(len(observed_transport) - len(expected_values) + 1):
        window_tokens = observed_transport[offset:offset + len(expected_values)]
        if [token for token, _, _ in window_tokens] != expected_values:
            continue
        if window_tokens[0][1] < start or window_tokens[-1][2] > end:
            continue
        candidates.append(window_tokens)
    if len(candidates) == 1:
        return text[candidates[0][0][1]:candidates[0][-1][2]]
    if len(candidates) > 1:
        raise ValidationError("evidence quote occurs more than once; provide a longer unique quotation")
    raise ValidationError("evidence quote is absent from the supplied source window")


def bind(value, sources: dict, *, windows: dict | None = None) -> dict:
    """Bind quotation objects to deterministic spans without altering other fields.

    Model replies sometimes copy a valid quotation together with character offsets
    from a different representation of the same source. The quotation remains the
    authoritative payload: when a span-shaped item does not reproduce it, discard
    only the stale locator and re-locate that exact quotation inside the pinned
    source window. A quotation that cannot be located uniquely still fails closed.
    """
    value = deepcopy(value)
    windows = windows or {}
    errors = []

    def visit(item, path="$"):
        if isinstance(item, dict):
            fields = set(item)
            if fields == LEGACY_EVIDENCE_FIELDS or fields == SPAN_EVIDENCE_FIELDS:
                source = sources.get(item.get("source_ref"))
                if source is None or source.get("work_id") != item.get("work_id"):
                    errors.append(f"{path}: evidence identifies an unavailable source or different work")
                    return
                try:
                    window = windows.get(item["source_ref"])
                    if fields == SPAN_EVIDENCE_FIELDS:
                        start, end = item.get("start"), item.get("end")
                        text = source.get("text")
                        if (isinstance(text, str) and type(start) is int and type(end) is int
                                and 0 <= start < end <= len(text)
                                and text[start:end] == item.get("quote")
                                and item.get("quote_sha256") == quote_sha256(item.get("quote"))):
                            try:
                                validate(item, source, require_span=True, window=window)
                            except ValidationError as exc:
                                errors.append(f"{path}: {exc}")
                            return
                    item.update(locate(source, item.get("quote"), window=window))
                except ValidationError as exc:
                    try:
                        item["quote"] = _restore_unique_source_whitespace(
                            source, item.get("quote"), window=window)
                        item.update(locate(source, item["quote"], window=window))
                    except ValidationError:
                        errors.append(
                            f"{path}: evidence for {item.get('work_id')} must quote exact captured text "
                            f"unambiguously: {exc}")
                        return
            for key, child in item.items():
                visit(child, f"{path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")

    visit(value)
    if errors:
        raise ValidationError("; ".join(errors))
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
