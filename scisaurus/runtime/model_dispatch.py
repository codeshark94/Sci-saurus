"""Host-wide admission for model requests and single-session engineering jobs."""
from contextlib import contextmanager
import fcntl
from pathlib import Path
import time

from scisaurus.runtime.run_control import ensure_run_allowed


MODEL_CONCURRENCY = 3


class ModelSlotTimeout(TimeoutError):
    """A request expired in the queue before provider dispatch."""


def _slot_root():
    return Path.home() / ".local" / "state" / "sci-whale" / "model-slots"


@contextmanager
def model_dispatch_slot(*, deadline):
    """Reserve one of three kernel-owned slots across threads and processes.

    Slot files persist; the locks disappear when their owning process exits.
    The directory must not be replaced or its files unlinked while workers run.
    """
    ensure_run_allowed()
    root = _slot_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    handles = []
    held = None
    try:
        for number in range(MODEL_CONCURRENCY):
            handles.append((root / f"{number}.lock").open("a+b"))
        while held is None:
            ensure_run_allowed()
            if time.monotonic() >= deadline:
                raise ModelSlotTimeout("model request expired while waiting for a dispatch slot")
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                held = handle
                break
            if held is None:
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        ensure_run_allowed()
        yield held
    finally:
        # Closing releases ordinary reservations. An engineering child inherits
        # the held file description and retains its lock if its controller dies.
        for handle in handles:
            handle.close()
