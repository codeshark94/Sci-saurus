"""Process execution policy inherited by supervised and worker processes."""
from contextlib import contextmanager
import os

from scisaurus.core.errors import ValidationError


MODEL_COST_LIMITS = frozenset({"max_model_calls", "max_input_tokens", "max_output_tokens"})


def execution_policy():
    mode = os.environ.get("SCISAURUS_EXECUTION_POLICY", "operational")
    if mode not in {"operational", "development"}:
        raise ValidationError("SCISAURUS_EXECUTION_POLICY must be operational or development")
    return mode


def enforce_model_cost_limits():
    return execution_policy() == "operational"


@contextmanager
def development_execution(enabled):
    """Apply an explicit CLI policy for this invocation and its child processes."""
    previous = os.environ.get("SCISAURUS_EXECUTION_POLICY")
    if enabled:
        os.environ["SCISAURUS_EXECUTION_POLICY"] = "development"
    try:
        yield
    finally:
        if enabled:
            if previous is None:
                os.environ.pop("SCISAURUS_EXECUTION_POLICY", None)
            else:
                os.environ["SCISAURUS_EXECUTION_POLICY"] = previous
