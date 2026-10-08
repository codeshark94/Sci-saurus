"""Evidence required to release conservative, unobserved dispatch charges."""
import math
from scisaurus.core.errors import ValidationError


def dispatch_release_count(before, after):
    """Recognize only a frozen request's proven transition to zero dispatches."""
    old, new = before.get("requests", []), after.get("requests", [])
    if len(old) != len(new) or before.get("assignment") != after.get("assignment"):
        raise ValidationError("dispatch release changes its request owner")
    released = 0
    for body in (before, after):
        usage = body.get("usage", {})
        if (not isinstance(usage, dict)
                or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in usage.values())
                or type(usage.get("model_calls")) is not int):
            raise ValidationError("dispatch release has invalid usage")
    mutable = {"status", "usage", "error", "context_budget", "request_attempts",
               "status_code", "retry_after_seconds", "provider_error_kind"}
    for prior, current in zip(old, new):
        if prior == current:
            continue
        if (prior.get("status") != "started" or prior.get("usage") != {"model_calls": 1}
                or type(prior.get("usage", {}).get("model_calls")) is not int
                or current.get("usage") != {"model_calls": 0}
                or type(current.get("usage", {}).get("model_calls")) is not int
                or not isinstance(current.get("error"), str) or not current["error"].strip()
                or {k: v for k, v in prior.items() if k not in mutable}
                   != {k: v for k, v in current.items() if k not in mutable}):
            raise ValidationError("dispatch release changes an observed or unknown request")
        status = current.get("status")
        if status == "context_not_dispatched":
            budget = current.get("context_budget", {})
            estimate, allowed = budget.get("estimated_input_tokens"), budget.get("allowed_input_tokens")
            attempts = current.get("request_attempts", 0)
            proven = (type(estimate) is int and type(allowed) is int and estimate > allowed >= 0
                      and type(attempts) is int and attempts == 0)
        elif status in {"cooldown_not_dispatched", "operator_paused_not_dispatched"}:
            proven = type(current.get("request_attempts")) is int and current["request_attempts"] == 0
        else:
            proven = False
        if not proven:
            raise ValidationError("dispatch release has no zero-dispatch receipt")
        released += 1
    keys = set(before.get("usage", {})) | set(after.get("usage", {}))
    for key in keys:
        difference = before.get("usage", {}).get(key, 0) - after.get("usage", {}).get(key, 0)
        if difference != (released if key == "model_calls" else 0):
            raise ValidationError("dispatch release changes actual provider usage")
    if not released:
        raise ValidationError("dispatch release has no canceled reservation")
    return released
