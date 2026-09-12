"""Reader-facing scientific language checks and editorial compression helpers.

The control plane keeps exact states, hashes, reservations, and acceptance
records in its ledger.  A manuscript is a different projection: it should
describe the scientific design and result without exposing control-plane
vocabulary.  This module provides deterministic checks for that boundary.  It
does not rewrite prose silently; callers receive precise findings and may ask
for a scoped editorial repair.
"""
from __future__ import annotations

from collections import defaultdict
import re
import unicodedata

from scisaurus.core.errors import ValidationError


CONTROL_PATTERNS = (
    ("finite_by_specification", re.compile(r"\bfinite\s+by\s+specification\b", re.I)),
    ("protocol_bound", re.compile(r"\bprotocol[- ]bound\b", re.I)),
    ("frozen", re.compile(r"\bfrozen\b", re.I)),
    ("insufficient_evidence_enum", re.compile(r"\binsufficient_evidence\b", re.I)),
    ("retained_alternatives", re.compile(r"\bretained\s+alternatives?\b", re.I)),
    ("accepted_artifact", re.compile(r"\baccepted\s+artifact\b", re.I)),
    ("validator", re.compile(r"\b(?:independent\s+)?validator\b", re.I)),
    ("artifact_reference", re.compile(r"\bartifact:[A-Za-z0-9_./-]+@[0-9]+\b", re.I)),
    ("model_call_counter", re.compile(r"\bmodel[_ -]?calls?\b", re.I)),
    ("release_candidate", re.compile(r"\brelease\s+candidate\b", re.I)),
    ("acceptance_check", re.compile(r"\bacceptance\s+(?:check|gate|contract)\b", re.I)),
    ("sha256_qa", re.compile(r"\bSHA[- ]?256\b", re.I)),
    ("qa_tolerance", re.compile(r"\baccepted\s+all\b.*\b(?:within|tolerance)\b", re.I)),
)


PUBLIC_TRANSLATIONS = {
    "finite by specification": "under the prespecified design",
    "protocol-bound": "specific to this dataset and model design",
    "protocol bound": "specific to this dataset and model design",
    "frozen": "prespecified",
    "insufficient_evidence": "available literature was insufficient to determine whether",
    "retained alternatives": "comparators reserved for a separate analysis",
    "retained alternative": "a comparator reserved for a separate analysis",
    "accepted artifact": "the resulting analysis",
    "independent validator": "independent recalculation",
    "validator": "independent recalculation",
}


def project_internal_language(text: str) -> str:
    """Translate common internal labels for a model's reader-facing draft.

    This is deliberately an explicit projection helper rather than a hidden
    post-processing rewrite.  The caller can inspect the proposed projection
    and bind it to the source units before adoption.
    """
    if not isinstance(text, str):
        raise ValidationError("scientific surface text must be a string")
    projected = text
    for source, target in sorted(PUBLIC_TRANSLATIONS.items(), key=lambda item: -len(item[0])):
        projected = re.sub(re.escape(source), target, projected, flags=re.I)
    projected = re.sub(
        r"accepted\s+all\s+(\w+)\s+aggregates\s+within\s+1e-\d+",
        r"independent recalculation reproduced the \1 aggregates within numerical tolerance",
        projected,
        flags=re.I,
    )
    return projected


def find_control_leaks(text: str, *, allowed_patterns=()):
    """Return control-plane terms visible in reader-facing prose.

    ``allowed_patterns`` contains pattern names, not regular expressions, so
    the allowlist remains reviewable and cannot accidentally broaden the gate.
    """
    if not isinstance(text, str):
        raise ValidationError("scientific surface text must be a string")
    allowed = set(allowed_patterns)
    findings = []
    for name, pattern in CONTROL_PATTERNS:
        if name in allowed:
            continue
        for match in pattern.finditer(text):
            findings.append({"kind": name, "text": match.group(0), "start": match.start(), "end": match.end()})
    return findings


def classify_surface_text(text: str, *, allowed_patterns=()):
    """Classify a sentence or unit as scientific, operational, or empty."""
    if not isinstance(text, str) or not text.strip():
        return {"class": "empty", "leaks": []}
    leaks = find_control_leaks(text, allowed_patterns=allowed_patterns)
    return {"class": "operational" if leaks else "scientific", "leaks": leaks}


def split_sentences(text: str):
    if not isinstance(text, str):
        raise ValidationError("scientific surface text must be a string")
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]


def _normal(text: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def _numeric_signature(sentence: str):
    numbers = tuple(re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?%?", sentence.casefold()))
    if not numbers:
        return None
    terms = tuple(sorted(set(re.findall(
        r"\b(?:log\s+loss|brier|ece|accuracy|auc|precision|recall|temperature|p\s*value|confidence\s+interval)\b",
        sentence.casefold()))))
    return numbers, terms


def repeated_numeric_facts(text: str, *, max_repetitions=2):
    """Find repeated numeric facts that make a manuscript feel assembled."""
    if type(max_repetitions) is not int or max_repetitions < 1:
        raise ValidationError("max_repetitions must be a positive integer")
    groups = defaultdict(list)
    for index, sentence in enumerate(split_sentences(text)):
        signature = _numeric_signature(sentence)
        if signature:
            groups[signature].append({"sentence_index": index, "text": sentence})
    return [{"signature": list(signature), "occurrences": occurrences}
            for signature, occurrences in groups.items() if len(occurrences) > max_repetitions]


def repeated_caveats(text: str, *, max_repetitions=2):
    if type(max_repetitions) is not int or max_repetitions < 1:
        raise ValidationError("max_repetitions must be a positive integer")
    caveat_patterns = (
        r"not an inferential confidence interval",
        r"no broader claim",
        r"not a clinical (?:deployment|decision) study",
        r"single (?:public )?dataset",
        r"does not establish (?:a )?universal",
    )
    findings = []
    for pattern in caveat_patterns:
        matches = list(re.finditer(pattern, text, re.I))
        if len(matches) > max_repetitions:
            findings.append({"pattern": pattern, "count": len(matches),
                             "occurrences": [match.group(0) for match in matches]})
    return findings


def editorial_audit(text: str, *, allowed_patterns=(), max_numeric_repetitions=2, max_caveat_repetitions=2):
    """Return deterministic surface and compression findings for a draft."""
    return {
        "surface": classify_surface_text(text, allowed_patterns=allowed_patterns),
        "numeric_repetitions": repeated_numeric_facts(text, max_repetitions=max_numeric_repetitions),
        "caveat_repetitions": repeated_caveats(text, max_repetitions=max_caveat_repetitions),
    }


def validate_scientific_surface(text: str, *, allowed_patterns=(), max_numeric_repetitions=2,
                                max_caveat_repetitions=2):
    """Raise a scoped error when a reader-facing document leaks operations or repeats facts."""
    audit = editorial_audit(text, allowed_patterns=allowed_patterns,
                            max_numeric_repetitions=max_numeric_repetitions,
                            max_caveat_repetitions=max_caveat_repetitions)
    leaks = audit["surface"]["leaks"]
    if leaks:
        names = ", ".join(sorted({item["kind"] for item in leaks}))
        raise ValidationError(f"manuscript exposes control-plane vocabulary: {names}")
    if audit["numeric_repetitions"]:
        raise ValidationError("manuscript repeats a numeric fact beyond the editorial limit")
    if audit["caveat_repetitions"]:
        raise ValidationError("manuscript repeats a caveat beyond the editorial limit")
    return audit

