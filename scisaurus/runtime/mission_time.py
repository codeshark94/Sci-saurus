"""Operator-owned mission horizons and cumulative active-stage time allocation."""
from copy import deepcopy
import math
import threading

from scisaurus.core.errors import ValidationError


def validate_mission_time_policy(value):
    fields = {"mode", "support_seconds", "design_fraction_target"}
    if not isinstance(value, dict) or set(value) != fields or value["mode"] != "unbounded":
        raise ValidationError("mission time policy requires mode=unbounded, support_seconds and design_fraction_target")
    for key in ("support_seconds", "design_fraction_target"):
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]):
            raise ValidationError("mission time policy requires finite nonboolean values")
    if value["support_seconds"] <= 0 or not 0 < value["design_fraction_target"] <= 1:
        raise ValidationError("mission time policy has invalid allocation bounds")
    return deepcopy(value)


class MissionTimeLedger:
    """Accumulate execution intervals; stopped-process time is never charged."""

    def __init__(self, policy, *, clock, restored=None):
        self.policy = validate_mission_time_policy(policy)
        self.clock = clock
        self.used = {"design": 0.0, "support": 0.0}
        if restored is not None:
            if not isinstance(restored, dict) or set(restored) != set(self.used):
                raise ValidationError("invalid mission time accounting")
            for key, value in restored.items():
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValidationError("invalid active-stage seconds")
                self.used[key] = float(value)
        self.active = None
        self.active_kind = None
        self.observed = clock()
        self.lock = threading.RLock()

    def switch(self, kind):
        with self.lock:
            now = self.clock()
            if self.active is not None:
                self.used[self.active] += max(0.0, now - self.observed)
            self.observed = now
            self.active_kind = kind
            self.active = None if kind is None else ("support" if kind in {"survey", "paper", "argument"} else "design")

    def remaining_support(self):
        with self.lock:
            self.switch(self.active_kind)
            return max(0.0, self.policy["support_seconds"] - self.used["support"])

    def snapshot(self):
        with self.lock:
            self.remaining_support()
            total = sum(self.used.values())
            return {"active_kind": self.active_kind, "used_seconds": dict(self.used), "support_remaining_seconds": max(
                0.0, self.policy["support_seconds"] - self.used["support"]),
                "design_fraction_observed": self.used["design"] / total if total else None,
                "design_fraction_target": self.policy["design_fraction_target"],
                "scope": "active topic/design and experiment stages; support is survey, argument and paper stages"}
