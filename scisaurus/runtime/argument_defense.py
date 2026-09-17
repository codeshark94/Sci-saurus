"""Transparent argumentation for weak points in a scientific paper.

This is a posture ledger, not a rhetoric generator.  It tells the writer
which statements are observations, supported inferences, bounded inferences,
provisional explanations, or future tests.  A missing result can therefore be
explained and scoped, but never laundered into evidence by confident prose.
"""
from __future__ import annotations

from copy import deepcopy
import re

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


SCHEMA_VERSION = "argument-defense-1"
POSTURES = {"observed", "supported_inference", "bounded_inference", "provisional", "future_test"}
STRATEGIES = {"mechanistic_interpretation", "scope_boundary", "alternative_explanation",
              "future_test", "downgrade_claim"}
SECTIONS = {"results", "discussion", "limitations", "future_work"}
TOP_FIELDS = {"schema_version", "research_question", "claim_postures", "weak_points", "policy"}
CLAIM_FIELDS = {"id", "claim", "posture", "evidence_ids", "defense", "caveat", "allowed_sections"}
WEAK_POINT_FIELDS = {"id", "weak_point", "defense_strategy", "argument", "remaining_uncertainty", "reviewer_test"}
POLICY_FIELDS = {"results_rule", "discussion_rule", "limitation_rule", "missing_evidence_action"}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    if len(value) > 4096:
        raise ValidationError(f"{name} is too long")
    return value


def _id(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _unique_id(base, used):
    """Derive a valid bounded ID without letting input IDs collide."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", str(base).casefold()).strip("-")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = f"claim-{cleaned}" if cleaned else "claim"
    cleaned = cleaned[:64].rstrip("-")
    candidate = cleaned
    suffix = 2
    while candidate in used:
        suffix_text = f"-{suffix}"
        candidate = f"{cleaned[:64 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used.add(candidate)
    return candidate


def _refs(value, name, *, evidence_ids=None, required=False):
    if (not isinstance(value, list) or len(value) != len(set(value))
            or required and not value):
        raise ValidationError(f"{name} must be a unique string list")
    for item in value:
        _text(item, name)
    if evidence_ids is not None and set(value) - set(evidence_ids):
        raise ValidationError(f"{name} references unknown evidence")
    return value


def validate_argument_defense(value, *, evidence_ids=None):
    """Validate posture/evidence separation before it reaches composition."""
    if not isinstance(value, dict) or set(value) != TOP_FIELDS:
        raise ValidationError(f"argument defense requires exactly {sorted(TOP_FIELDS)}")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError("argument defense schema version is unsupported")
    _text(value["research_question"], "argument defense research_question")
    evidence_ids = None if evidence_ids is None else set(evidence_ids)
    claim_ids = set()
    claims = value["claim_postures"]
    if not isinstance(claims, list) or not claims:
        raise ValidationError("argument defense claim_postures must be nonempty")
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != CLAIM_FIELDS:
            raise ValidationError("argument defense claim has an invalid shape")
        _id(claim["id"], "argument defense claim id")
        if claim["id"] in claim_ids:
            raise ValidationError("argument defense claim IDs must be unique")
        claim_ids.add(claim["id"])
        _text(claim["claim"], "argument defense claim")
        posture = claim["posture"]
        if posture not in POSTURES:
            raise ValidationError("argument defense posture is unsupported")
        required_evidence = posture in {"observed", "supported_inference", "bounded_inference"}
        _refs(claim["evidence_ids"], "argument defense claim evidence_ids",
              evidence_ids=evidence_ids, required=required_evidence)
        _text(claim["defense"], "argument defense defense")
        _text(claim["caveat"], "argument defense caveat")
        sections = claim["allowed_sections"]
        if (not isinstance(sections, list) or not sections or len(sections) != len(set(sections))
                or any(item not in SECTIONS for item in sections)):
            raise ValidationError("argument defense allowed_sections is invalid")
        if posture == "observed" and "results" not in sections:
            raise ValidationError("observed claims must be allowed in Results")
        if posture != "observed" and "results" in sections:
            raise ValidationError("interpretive defense cannot be allowed in Results")
        if posture == "future_test" and "future_work" not in sections:
            raise ValidationError("future_test claims require Future Work")

    weak_ids = set()
    weak_points = value["weak_points"]
    if not isinstance(weak_points, list):
        raise ValidationError("argument defense weak_points must be a list")
    for item in weak_points:
        if not isinstance(item, dict) or set(item) != WEAK_POINT_FIELDS:
            raise ValidationError("argument defense weak point has an invalid shape")
        _id(item["id"], "argument defense weak point id")
        if item["id"] in weak_ids:
            raise ValidationError("argument defense weak point IDs must be unique")
        weak_ids.add(item["id"])
        for key in ("weak_point", "argument", "remaining_uncertainty", "reviewer_test"):
            _text(item[key], f"argument defense {key}")
        if item["defense_strategy"] not in STRATEGIES:
            raise ValidationError("argument defense strategy is unsupported")

    policy = value["policy"]
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS:
        raise ValidationError("argument defense policy has an invalid shape")
    for key in POLICY_FIELDS:
        _text(policy[key], f"argument defense policy {key}")
    canonical_bytes(value)
    return value


def _pattern_evidence(argument, pattern_id):
    for pattern in argument.get("observed_patterns", []):
        if isinstance(pattern, dict) and pattern.get("id") == pattern_id:
            return list(pattern.get("evidence_ids", []))
    return []


def build_argument_defense(argument, evidence_packet, *, research_program=None):
    """Build a deterministic defense ledger from accepted argument content."""
    if not isinstance(argument, dict) or not isinstance(evidence_packet, dict):
        raise ValidationError("argument defense requires argument and evidence packet objects")
    question = argument.get("research_question")
    _text(question, "argument defense research_question")
    evidence_ids = set(evidence_packet.get("evidence_ids", []))
    claims = []
    claim_ids = set()
    for pattern in argument.get("observed_patterns", []):
        if not isinstance(pattern, dict):
            continue
        claims.append({
            "id": _unique_id(f"observed-{pattern['id']}", claim_ids),
            "claim": pattern["observation"],
            "posture": "observed",
            "evidence_ids": list(pattern["evidence_ids"]),
            "defense": "Report this pattern as observed and do not attach a mechanism to it in the Results section.",
            "caveat": "The observation alone does not identify a causal or mechanistic explanation.",
            "allowed_sections": ["results", "discussion"],
        })

    hypotheses = {item.get("id"): item for item in argument.get("hypotheses", [])
                  if isinstance(item, dict) and isinstance(item.get("id"), str)}
    primary = argument.get("primary_argument") or {}
    primary_id = primary.get("primary_hypothesis_id")
    primary_hypothesis = hypotheses.get(primary_id, {})
    primary_refs = list(primary_hypothesis.get("evidence_ids", []))
    if not primary_refs:
        for pattern_id in primary_hypothesis.get("explains_pattern_ids", []):
            primary_refs.extend(_pattern_evidence(argument, pattern_id))
    primary_refs = list(dict.fromkeys(primary_refs))
    claims.append({
        "id": _unique_id("primary-thesis", claim_ids),
        "claim": primary["thesis"],
        "posture": "bounded_inference",
        "evidence_ids": primary_refs,
        "defense": (
            f"Use the stated rationale to connect the supplied observations to this interpretation, while keeping the scope "
            f"boundary explicit: {primary['rationale']}"
        ),
        "caveat": primary["scope_boundary"],
        "allowed_sections": ["discussion", "limitations"],
    })

    weak_points = []
    weak_ids = set()
    for hypothesis in argument.get("hypotheses", []):
        if not isinstance(hypothesis, dict) or hypothesis.get("id") == primary_id:
            continue
        status = hypothesis.get("status")
        refs = list(hypothesis.get("evidence_ids", []))
        if status in {"supported", "disfavored"} and refs:
            posture = "supported_inference"
            sections = ["discussion", "limitations"]
        else:
            posture = "provisional"
            sections = ["discussion", "future_work"]
        claims.append({
            "id": _unique_id(f"hypothesis-{hypothesis['id']}", claim_ids),
            "claim": hypothesis["statement"],
            "posture": posture,
            "evidence_ids": refs,
            "defense": (
                "Keep this explanation at the stated posture and use its predictions to show what would change the conclusion."
            ),
            "caveat": f"The discriminating test remains necessary: {hypothesis['discriminating_test']}",
            "allowed_sections": sections,
        })
        if posture == "provisional":
            weak_points.append({
                "id": _unique_id(f"unresolved-{hypothesis['id']}", weak_ids),
                "weak_point": f"The mechanism '{hypothesis['id']}' is not identified by the current evidence.",
                "defense_strategy": "future_test",
                "argument": (
                    "Present this as a live alternative, not as a result; explain why its discriminating test is the next "
                    "informative action."
                ),
                "remaining_uncertainty": hypothesis["discriminating_test"],
                "reviewer_test": hypothesis["discriminating_test"],
            })

    for index, limitation in enumerate(argument.get("limitations", [])):
        weak_points.append({
            "id": _unique_id(f"limitation-{index}", weak_ids),
            "weak_point": limitation,
            "defense_strategy": "scope_boundary",
            "argument": "State the limitation and its consequence for interpretation instead of hiding it behind stronger wording.",
            "remaining_uncertainty": limitation,
            "reviewer_test": "Ask whether the conclusion changes outside the declared data, model, or design boundary.",
        })

    if isinstance(research_program, dict):
        selected = research_program.get("selected_branch")
        if not isinstance(selected, dict):
            selected = next((item for item in research_program.get("branches", [])
                             if isinstance(item, dict) and item.get("id") == research_program.get("selected_id")), None)
        if isinstance(selected, dict) and isinstance(selected.get("kill_if"), str) and selected["kill_if"].strip():
            weak_points.append({
                "id": _unique_id("branch-kill-condition", weak_ids),
                "weak_point": "The selected direction has a declared disconfirmation condition that must remain live.",
                "defense_strategy": "downgrade_claim",
                "argument": "Treat the declared kill condition as a decision boundary and do not defend the branch if it is met.",
                "remaining_uncertainty": selected["kill_if"],
                "reviewer_test": selected["kill_if"],
            })

    ledger = {
        "schema_version": SCHEMA_VERSION,
        "research_question": question,
        "claim_postures": claims,
        "weak_points": weak_points,
        "policy": {
            "results_rule": "Only observed patterns and directly bound measurements may be written as Results.",
            "discussion_rule": "Mechanistic explanations must be labelled supported, bounded, or provisional and tied to their evidence.",
            "limitation_rule": "A limitation is stated with its consequence for interpretation, not hidden by confident prose.",
            "missing_evidence_action": "A missing result creates a scoped research request or future test; it cannot be repaired with rhetoric.",
        },
    }
    validate_argument_defense(ledger, evidence_ids=evidence_ids)
    return ledger


__all__ = [
    "SCHEMA_VERSION", "POSTURES", "STRATEGIES", "validate_argument_defense",
    "build_argument_defense",
]
