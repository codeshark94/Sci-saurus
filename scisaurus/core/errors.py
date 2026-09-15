"""Contract-level error types (docs/40-execution-contract.md §1)."""

from copy import deepcopy


class ContractError(Exception):
    """Base class for control-plane contract violations."""


class ValidationError(ContractError):
    """A record failed schema/field validation."""


class QuotaExceededError(ContractError):
    """A declared execution quota was exhausted before more work could run."""

    def __init__(self, message, *, dimension=None, limit=None, observed=None,
                 usage=None, diagnostics=None):
        super().__init__(message)
        self.dimension = dimension
        self.limit = limit
        self.observed = observed
        # Failed bounded stages must be able to carry the work they already
        # consumed to their owner.  Keeping this on the exception avoids
        # losing accounting when no successful stage context is returned.
        self.usage = deepcopy(usage) if isinstance(usage, dict) else {}
        self.diagnostics = deepcopy(diagnostics) if isinstance(diagnostics, list) else []


class ConflictError(ContractError):
    """A compare-and-swap (CAS) precondition failed."""


class StaleFenceError(ContractError):
    """A worker tried to commit effects with an expired or superseded lease."""


class StateError(ContractError):
    """A lifecycle transition was not allowed."""


class NotFoundError(ContractError):
    """A referenced object/version does not exist."""
