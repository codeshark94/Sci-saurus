"""Project-owned, serialized DSH development history and workspace."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
from pathlib import Path
import time
import uuid

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.run_control import ensure_run_allowed


def development_session_binding(project):
    owner = Path(project).resolve()
    return {"project_id": str(owner), "root": str(owner / "development-session")}


class DevelopmentSession:
    def __init__(self, binding):
        if (not isinstance(binding, dict) or set(binding) != {"root", "project_id"}
                or any(not isinstance(v, str) or not Path(v).is_absolute() for v in binding.values())):
            raise ValidationError("development session requires an absolute project owner and root")
        self.binding = deepcopy(binding)
        self.root = Path(binding["root"]).resolve()
        project = Path(binding["project_id"]).resolve()
        if self.root == project or not self.root.is_relative_to(project):
            raise ValidationError("development workspace must belong to its project")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "session.json"
        self.state = None

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(canonical_bytes(self.state))
        temporary.replace(self.path)

    @contextmanager
    def lease(self, config, deadline):
        with (self.root / "owner.lock").open("a+b") as lock:
            while True:
                ensure_run_allowed()
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("development session is owned by another dispatch")
                    time.sleep(.05)
            try:
                digest = hashlib.sha256(canonical_bytes(config)).hexdigest()
                if self.path.exists():
                    self.state = json.loads(self.path.read_bytes())
                    if (self.state.get("schema_version") != "dsh-development-session-1"
                            or self.state.get("binding") != self.binding
                            or self.state.get("config_sha256") != digest):
                        raise ValidationError("development session owner or backend changed")
                    if self.state.get("active_job"):
                        raise ValidationError("development session has an unsettled dispatch")
                    for index, call in enumerate(self.state["calls"]):
                        receipt_path = Path(call["receipt"]).resolve()
                        body = receipt_path.read_bytes()
                        if hashlib.sha256(body).hexdigest() != call["sha256"]:
                            raise ValidationError("development session receipt changed")
                        receipt = json.loads(body)
                        owner = receipt.get("development_session", {})
                        if (receipt.get("job_directory") != str(receipt_path.parent)
                                or owner.get("binding") != self.binding
                                or owner.get("prior_receipts") != self.state["calls"][:index]
                                or receipt.get("session_id") != self.state["session_id"]
                                or receipt.get("config_sha256") != digest
                                or not receipt.get("process_reaped")
                                or not (receipt.get("status") == "completed"
                                    or receipt.get("status") == "failed" and receipt.get("outcome_known") is True)):
                            raise ValidationError("development session requires a settled, reaped outcome")
                        boundaries = [("input", receipt.get("input_sha256", {}))]
                        boundaries.extend((turn["input_directory"], turn["input_sha256"])
                                          for turn in receipt.get("turns", []))
                        boundaries.extend((f"turns/{index}", turn["outputs"])
                                          for index, turn in enumerate(receipt.get("turns", [])))
                        for directory, hashes in boundaries:
                            for name, expected in hashes.items():
                                path = receipt_path.parent / directory / name
                                if (not path.resolve().is_relative_to(receipt_path.parent)
                                        or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                                    raise ValidationError("development history evidence changed")
                        output_directory = receipt.get("output_directory")
                        if receipt.get("status") == "completed":
                            if output_directory != "outputs":
                                raise ValidationError("development history has no immutable output archive")
                            for name, expected in receipt["outputs"].items():
                                path = receipt_path.parent / output_directory / name
                                if (not path.resolve().is_relative_to(receipt_path.parent)
                                        or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                                    raise ValidationError("development history output changed")
                else:
                    self.state = {"schema_version": "dsh-development-session-1", "binding": self.binding,
                                  "config_sha256": digest, "session_id": uuid.uuid4().hex,
                                  "calls": [], "active_job": None}
                    self.save()
                yield self
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @property
    def resume(self):
        return bool(self.state["calls"])

    @property
    def read_roots(self):
        return [str(Path(row["receipt"]).parent) for row in self.state["calls"]]

    @property
    def transport_history(self):
        return [json.loads(Path(row["receipt"]).read_bytes()) for row in self.state["calls"]]

    def bind_job(self, receipt):
        self.state["active_job"] = str(receipt)
        self.save()

    def settle(self, receipt):
        if str(receipt) != self.state.get("active_job"):
            raise ValidationError("development session settlement lost its dispatch owner")
        body = Path(receipt).read_bytes()
        value = json.loads(body)
        owner = value.get("development_session", {})
        if (value.get("job_directory") != str(Path(receipt).resolve().parent)
                or owner.get("binding") != self.binding
                or owner.get("prior_receipts") != self.state["calls"]):
            raise ValidationError("development session settlement has a foreign receipt owner")
        if value.get("status") == "not_dispatched":
            self.state["active_job"] = None
        elif (value.get("session_id") == self.state["session_id"] and value.get("process_reaped")
                and (value.get("status") == "completed"
                     or value.get("status") == "failed" and value.get("outcome_known") is True)):
            self.state["calls"].append({"receipt": str(receipt),
                "sha256": hashlib.sha256(body).hexdigest(), "usage": value["usage"]})
            self.state["active_job"] = None
        self.save()
