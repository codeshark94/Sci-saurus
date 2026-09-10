"""Review coverage records and the review gate (40-execution-contract §6.1).

A review invocation returns an immutable ReviewCoverage record with one of
four outcomes. ``review_failed`` (tool/runtime failure) and
``insufficient_evidence`` can never be converted into a pass (T10).
"""

from __future__ import annotations

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore

OUTCOMES = frozenset(
    {"objections_found", "no_valid_objection_found", "insufficient_evidence", "review_failed"}
)


class ReviewProtocol:
    def __init__(self, control: ControlStore, store: ArtifactStore):
        self.control = control
        self.store = store

    def publish_review_coverage(
        self,
        *,
        review_id: str,
        reviewer: str,
        target_refs: list[str],
        checks_attempted: list[str],
        checks_completed: list[str],
        exclusions: list[str],
        outcome: str,
        critique_refs: list[str] | None = None,
        usage: dict | None = None,
    ) -> dict:
        if outcome not in OUTCOMES:
            raise ValidationError(f"unknown review outcome: {outcome!r}")
        if outcome == "no_valid_objection_found" and not checks_completed:
            # a failed tool call cannot stand in for a completed review (T10)
            raise ValidationError(
                "no_valid_objection_found requires completed checks;"
                " failed reviews must use review_failed"
            )
        manifest = self.store.publish_artifact(
            logical_id=f"issues/reviews/{review_id}",
            artifact_type="review_coverage",
            author=reviewer,
            body=canonical_bytes(
                {
                    "reviewer": reviewer,
                    "target_refs": target_refs,
                    "checks_attempted": checks_attempted,
                    "checks_completed": checks_completed,
                    "exclusions": exclusions,
                    "outcome": outcome,
                    "critique_refs": critique_refs or [],
                    "usage": usage or {},
                }
            ),
            media_type="application/json+scisaurus-review",
        )
        with self.control.tx() as conn:
            self.control.append_event(
                conn,
                actor=reviewer,
                event_type="review.completed",
                payload={"review_ref": manifest["artifact_ref"], "outcome": outcome},
            )
        return manifest

    @staticmethod
    def review_passes(outcome: str) -> tuple[bool, str]:
        """Gate helper: a failed/insufficient review is never an implicit pass (T10)."""
        if outcome == "no_valid_objection_found":
            return True, "review completed; no justified objection found within coverage"
        if outcome == "objections_found":
            return (
                False,
                "objections registered; resolution and verification required before the gate passes",
            )
        if outcome == "insufficient_evidence":
            return (
                False,
                "review could not assess the material point with current inputs (T10)",
            )
        if outcome == "review_failed":
            return False, "review tool/runtime failure — never converted into a pass (T10)"
        raise ValidationError(f"unknown review outcome: {outcome!r}")