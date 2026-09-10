"""Wall-clock progress checkpoints and anytime results (D29).

Checkpoints are durable status artifacts produced by the native scheduler,
independent of model-call completion: they record the incumbent or its
absence, verified artifact/information changes, blockers, cumulative usage,
and the next allocation decision. Missing judgments stay pending — the
scheduler cannot invent progress.
"""

from __future__ import annotations

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore


class ProgressManager:
    def __init__(self, control: ControlStore, store: ArtifactStore):
        self.control = control
        self.store = store

    def publish_checkpoint(
        self,
        *,
        checkpoint_id: str,
        author: str,
        incumbent_ref: str | None,
        verified_changes: list[dict],
        information_changes: list[dict],
        blockers: list[dict],
        cumulative_usage: dict,
        next_action: dict,
    ) -> dict:
        """Publish a durable ProgressCheckpoint artifact (command/progress/*)."""
        if not cumulative_usage:
            raise ValidationError("checkpoints must record cumulative usage (truthful accounting)")
        manifest = self.store.publish_artifact(
            logical_id=f"command/progress/{checkpoint_id}",
            artifact_type="progress_checkpoint",
            author=author,
            body=canonical_bytes(
                {
                    "incumbent_ref": incumbent_ref,
                    "verified_changes": verified_changes,
                    "information_changes": information_changes,
                    "blockers": blockers,
                    "cumulative_usage": cumulative_usage,
                    "next_action": next_action,
                }
            ),
            media_type="application/json+scisaurus-checkpoint",
        )
        with self.control.tx() as conn:
            self.control.append_event(
                conn,
                actor=author,
                event_type="progress.checkpointed",
                payload={
                    "checkpoint_ref": manifest["artifact_ref"],
                    "incumbent_ref": incumbent_ref,
                    "blockers": len(blockers),
                },
            )
        return manifest


def admission_decision(
    *,
    unverified_backlog: int,
    review_capacity_remaining: int,
    backlog_threshold: int,
) -> dict:
    """Backpressure rule (T45): unverified candidate backlog shifts capacity
    toward review and integration instead of admitting more producers."""
    if unverified_backlog >= backlog_threshold:
        return {
            "admit_new_candidates": False,
            "shift": "review_and_integration",
            "reason": (
                f"unverified backlog {unverified_backlog} >= threshold {backlog_threshold};"
                " independent review capacity is not starved (T45)"
            ),
        }
    if review_capacity_remaining <= 0:
        return {
            "admit_new_candidates": False,
            "shift": "none",
            "reason": "no review capacity remaining; do not admit unreviewable candidates",
        }
    return {"admit_new_candidates": True, "shift": "production", "reason": "backlog within threshold"}


def completion_decision(*, mission_complete: bool, unused_capacity: bool) -> dict:
    """Completion policy (T52): satisfying the stop condition stops
    discretionary work; available GPUs are not a reason to continue."""
    if not mission_complete:
        return {"action": "continue", "reason": "mission stop condition not met"}
    return {
        "action": "propose_release_and_stop",
        "reason": (
            "completion contract satisfied; discretionary work stops even with"
            f" {'unused' if unused_capacity else 'no'} capacity remaining (T52)"
        ),
    }