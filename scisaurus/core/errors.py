"""Contract-level error types (docs/40-execution-contract.md §1)."""


class ContractError(Exception):
    """Base class for control-plane contract violations."""


class ValidationError(ContractError):
    """A record failed schema/field validation."""


class ConflictError(ContractError):
    """A compare-and-swap (CAS) precondition failed."""


class StaleFenceError(ContractError):
    """A worker tried to commit effects with an expired or superseded lease."""


class StateError(ContractError):
    """A lifecycle transition was not allowed."""


class NotFoundError(ContractError):
    """A referenced object/version does not exist."""