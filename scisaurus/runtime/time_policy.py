"""Finite stage planning with explicit estimate provenance and review reserves.

Configured durations are planning assumptions, not measured predictions. This
policy admits batches; the caller owns task execution, cancellation, and proof
that a result has passed verification.
"""
from __future__ import annotations

import math
import time
from copy import deepcopy

from scisaurus.core.errors import ValidationError


STAGES = ("setup", "supervision", "production", "unit_review", "integrated_review", "reassessment")


def _seconds(value, name, *, allow_zero=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValidationError(f"{name} must be finite and {'non-negative' if allow_zero else 'positive'}")
    return float(value)


def _count(value, name, *, allow_zero=False):
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise ValidationError(f"{name} must be a {'non-negative' if allow_zero else 'positive'} integer")
    return value


class TimePolicy:
    def __init__(self, *, stage_seconds, unit_count, worker_slots, wall_clock_seconds,
                 policy=None, clock=time.monotonic):
        if not isinstance(stage_seconds, dict) or set(stage_seconds) != set(STAGES):
            raise ValidationError(f"stage_seconds requires exactly {list(STAGES)}")
        self.seeds = {name: _seconds(stage_seconds[name], name) for name in STAGES}
        self.estimates = dict(self.seeds)
        self.observations = {name: {"count": 0, "total_seconds": 0.0, "max_seconds": None} for name in STAGES}
        self.unit_count = _count(unit_count, "unit_count")
        self.worker_slots = _count(worker_slots, "worker_slots")
        ceiling = _seconds(wall_clock_seconds, "wall_clock_seconds")
        if policy is None:
            policy = {}
        if not isinstance(policy, dict) or set(policy) - {"first_result_seconds", "target_seconds", "hard_seconds"}:
            raise ValidationError("time policy contains unsupported fields")
        self.hard_seconds = _seconds(policy.get("hard_seconds", ceiling), "hard_seconds")
        if self.hard_seconds > ceiling:
            raise ValidationError("hard_seconds cannot exceed wall_clock_seconds")
        self.initial_schedule = self.schedule()
        self.target_seconds = _seconds(policy.get("target_seconds", min(
            self.initial_schedule["total_seconds"], self.hard_seconds)), "target_seconds")
        self.first_result_seconds = _seconds(policy.get("first_result_seconds", min(
            self.initial_schedule["first_verified_result_seconds"], self.target_seconds)), "first_result_seconds")
        if not self.first_result_seconds <= self.target_seconds <= self.hard_seconds:
            raise ValidationError("time policy requires first_result_seconds <= target_seconds <= hard_seconds")
        self.provenance = {
            "hard_seconds": "configured_policy" if "hard_seconds" in policy else "wall_clock_limit",
            "target_seconds": "configured_policy" if "target_seconds" in policy else "stage_schedule_capped_by_hard_limit",
            "first_result_seconds": "configured_policy" if "first_result_seconds" in policy else "stage_schedule_capped_by_target",
        }
        self.clock, self.started_at = clock, clock()
        self.first_verified_result = None
        self.retained_result = None

    @property
    def elapsed_seconds(self):
        return max(0.0, self.clock() - self.started_at)

    def _stage(self, stage):
        stage = "production" if stage == "revision" else stage
        if stage not in STAGES:
            raise ValidationError(f"unknown time stage: {stage}")
        return stage

    def estimate(self, stage, task_count=1):
        stage = self._stage(stage)
        count = _count(task_count, "task_count", allow_zero=True)
        batches = (count + self.worker_slots - 1) // self.worker_slots
        return _seconds(self.estimates[stage] * batches, "batch estimate", allow_zero=True)

    def schedule(self):
        """One production pass followed by unit review and whole-result review.

        Reassessment is conditional on review findings, so it has a duration
        estimate but is not represented as an inevitable initial stage.
        """
        stages = {stage: self.estimate(stage, self.unit_count if stage in {
            "production", "unit_review"} else 1) for stage in STAGES if stage != "reassessment"}
        total = _seconds(sum(stages.values()), "schedule estimate")
        return {"stages": stages, "total_seconds": total, "first_verified_result_seconds": total}

    def admit(self, stage, *, task_count=1, pending_review_count=0):
        """Check the complete proposed batch, including its required review.

        pending_review_count is the caller's unverified backlog, excluding new
        production tasks or the unit-review tasks requested by this call.
        Reservations are forecasts, not a concurrent capacity ledger.
        """
        normalized = self._stage(stage)
        count = _count(task_count, "task_count")
        pending = _count(pending_review_count, "pending_review_count", allow_zero=True)
        estimate = self.estimate(normalized, count)
        new_work = normalized in {"production", "reassessment"}
        review_count = pending + (count if new_work else 0)
        reserve = self.estimate("unit_review", review_count)
        if normalized != "integrated_review":
            reserve += self.estimate("integrated_review")
        elapsed = self.elapsed_seconds
        remaining = max(0.0, self.hard_seconds - elapsed)
        target_remaining = max(0.0, self.target_seconds - elapsed)
        reason = "fits"
        if elapsed >= self.hard_seconds:
            reason = "hard_deadline_reached"
        elif new_work and elapsed >= self.target_seconds:
            reason = "target_reached_finish_verification"
        elif estimate + reserve > remaining:
            reason = "insufficient_time_for_stage_and_review"
        elif new_work and estimate + reserve > target_remaining:
            reason = "insufficient_target_time_for_production_and_review"
        return {"allowed": reason == "fits", "action": "allow" if reason == "fits" else "defer",
                "reason": reason, "stage": stage, "estimated_seconds": estimate,
                "remaining_seconds": remaining, "target_remaining_seconds": target_remaining,
                "reserved_review_seconds": reserve}

    def observe(self, stage, duration_seconds):
        """Record one completed task's duration, not aggregate parallel time."""
        stage = self._stage(stage)
        duration = _seconds(duration_seconds, "duration_seconds", allow_zero=True)
        observation = self.observations[stage]
        observation["count"] += 1
        observation["total_seconds"] += duration
        observation["max_seconds"] = max(duration, observation["max_seconds"] or 0.0)
        self.estimates[stage] = max(self.seeds[stage], observation["max_seconds"])

    def mark_first_verified_result(self, artifact_ref=None):
        if artifact_ref is not None and (not isinstance(artifact_ref, str) or not artifact_ref.strip()):
            raise ValidationError("artifact_ref must be a nonempty string when supplied")
        if self.first_verified_result is None:
            self.first_verified_result = {"elapsed_seconds": self.elapsed_seconds, "artifact_ref": artifact_ref}

    def mark_retained_result(self, artifact_ref):
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            raise ValidationError("retained artifact_ref must be a nonempty string")
        if self.first_verified_result is None:
            self.retained_result = {"artifact_ref": artifact_ref, "origin": "prior_run"}

    def snapshot(self):
        elapsed = self.elapsed_seconds
        result = self.first_verified_result
        missed = (result["elapsed_seconds"] > self.first_result_seconds if result is not None
                  else False if self.retained_result else elapsed >= self.first_result_seconds)
        status = (("available_late" if missed else "available_on_time") if result
                  else "retained_from_prior_run" if self.retained_result
                  else "missed" if missed else "pending")
        return {
            "first_result_seconds": self.first_result_seconds, "target_seconds": self.target_seconds,
            "hard_seconds": self.hard_seconds, "elapsed_seconds": elapsed,
            "remaining_seconds": max(0.0, self.hard_seconds - elapsed),
            "target_reached": elapsed >= self.target_seconds, "hard_deadline_reached": elapsed >= self.hard_seconds,
            "first_verified_result": dict(result) if result else None,
            "retained_result": dict(self.retained_result) if self.retained_result else None,
            "first_result_status": status, "first_result_target_missed": missed,
            "deadline_provenance": dict(self.provenance), "initial_schedule": deepcopy(self.initial_schedule),
            "initial_target_feasible": self.initial_schedule["total_seconds"] <= self.target_seconds,
            "initial_hard_limit_feasible": self.initial_schedule["total_seconds"] <= self.hard_seconds,
            "initial_first_result_feasible": self.initial_schedule["first_verified_result_seconds"] <= self.first_result_seconds,
            "current_schedule_estimate": self.schedule(),
            "stage_estimates": {stage: {"seconds": self.estimates[stage], "configured_seconds": self.seeds[stage],
                "provenance": "configured_seed_and_observed_max" if self.observations[stage]["count"] else "configured_seed",
                "observations": dict(self.observations[stage])} for stage in STAGES},
            "estimate_uncertainty": "Planning estimates are not guarantees; observed maxima are not upper bounds.",
        }


def validate_time_policy(policy, *, stage_seconds, unit_count, worker_slots, wall_clock_seconds):
    """Validate policy without dispatching work or requiring a user deadline."""
    TimePolicy(stage_seconds=stage_seconds, unit_count=unit_count, worker_slots=worker_slots,
               wall_clock_seconds=wall_clock_seconds, policy=policy)
    return policy
