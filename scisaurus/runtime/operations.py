"""Project-scoped operational readiness for explicitly configured capabilities.

Execution remains with the injected project runner, including capacity accounting,
process cleanup and network access. Operational checks establish usable transport
output; they never establish scientific support or adopt a document artifact.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import uuid

from scisaurus.core.errors import StateError, ValidationError
from scisaurus.core.schema import canonical_bytes, now_iso, sha256_hex
from scisaurus.core.tasks import TaskManager
from scisaurus.runtime.operation_adapters import CATALOG, get_adapter


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _hash_file(path):
    path = Path(path).absolute()
    if not path.is_file():
        raise ValidationError(f"identity file is unavailable: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return {"path": str(path), "resolved_path": str(path.resolve()), "sha256": digest.hexdigest()}


class OperationsCell:
    """A bounded service; configured callers supply authority, not model output.

    ``executor(task_id, kind, params, *, actor, task_kind)`` must return
    ``(result, execution_ref)`` after recording the task, successful attempt,
    actual usage, exact context and immutable execution report in this project.
    Actor separation is a control-plane invariant, not identity authentication.
    """

    def __init__(self, control, store, *, project_id):
        if store.control is not control:
            raise ValidationError("Operations store and control must be the same project connection")
        self.control, self.store, self.tasks = control, store, TaskManager(control)
        self.project_id = _text(project_id, "project_id")
        self.project_path = str(Path(control.dir).resolve())
        self.session_id = uuid.uuid4().hex
        with control.tx() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='operations_project'").fetchone()
            if row is None:
                identity = {"project_id": project_id, "project_path": self.project_path,
                            "project_instance": uuid.uuid4().hex}
                conn.execute("INSERT INTO meta(key,value) VALUES ('operations_project',?)",
                             (canonical_bytes(identity).decode(),))
                control.append_event(conn, actor="operations.controller", event_type="operations.project_bound", payload=identity)
            else:
                identity = json.loads(row["value"])
                if identity["project_id"] != project_id or identity["project_path"] != self.project_path:
                    raise ValidationError("Operations project identity/path does not match the stored binding")
        self.project_instance = identity["project_instance"]

    def _scope(self):
        return {"project_id": self.project_id, "project_path": self.project_path,
                "project_instance": self.project_instance}

    def _check_scope(self, record):
        if any(record.get(key) != value for key, value in self._scope().items()):
            raise ValidationError("Capability binding belongs to a different project")

    def _logical(self, capability_id, suffix):
        if not isinstance(capability_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", capability_id):
            raise ValidationError("capability_id must be a bounded lowercase identifier")
        return f"command/operations/{capability_id}/{suffix}"

    def _body(self, ref):
        record = self.store.get(ref)
        return record, json.loads(self.store.read_body(record["body_hash"]))

    def _publish(self, capability_id, suffix, body, actor, *, kind="report", subjects=(), conn=None,
                 task_id=None, attempt_id=None):
        return self.store.publish_artifact(
            logical_id=self._logical(capability_id, suffix), artifact_type=kind, author=actor,
            body=canonical_bytes({**body, **self._scope()}), media_type="application/json",
            inputs=[{"ref": ref, "purpose": "subject"} for ref in dict.fromkeys(subjects) if ref],
            task_id=task_id, attempt_id=attempt_id, conn=conn,
        )

    def _state(self, capability_id):
        manifest = self.store.head(self._logical(capability_id, "state"))
        if manifest is None:
            raise StateError(f"capability is not registered: {capability_id}")
        _, state = self._body(manifest["artifact_ref"])
        self._check_scope(state)
        return {**state, "state_ref": manifest["artifact_ref"]}

    def _transition(self, state, phase, actor, reason, *, conn=None, **changes):
        if conn is None:
            with self.control.tx() as transaction:
                return self._transition(state, phase, actor, reason, conn=transaction, **changes)
        current = self.store.head(self._logical(state["capability_id"], "state"))
        if current and current["artifact_ref"] != state.get("state_ref"):
            raise StateError("Capability state changed concurrently")
        body = {key: value for key, value in state.items() if key != "state_ref"}
        body.update(changes, state=phase, reason=reason, updated_at=now_iso())
        manifest = self._publish(state["capability_id"], "state", body, actor, kind="note", conn=conn,
                                 subjects=[body.get("profile_ref"), body.get("probe_ref"), body.get("verification_ref")])
        self.control.append_event(conn, actor=actor, event_type="operations.state_changed", payload={
            "capability_id": state["capability_id"], "from": state.get("state"), "to": phase,
            "reason": reason, "state_ref": manifest["artifact_ref"], **self._scope(),
        })
        return {**body, "state_ref": manifest["artifact_ref"]}

    def workspace_dir(self, capability_id):
        """Return owned scratch space; immutable evidence stays in the artifact store."""
        self._logical(capability_id, "state")
        path = Path(self.project_path) / "workspace" / "operations" / self.project_instance / capability_id
        expected = {**self._scope(), "capability_id": capability_id}
        if path.resolve() != path or any(parent.is_symlink() for parent in path.parents):
            raise ValidationError("Operations workspace must not traverse symlinks")
        marker = path / ".ownership.json"
        if path.exists():
            if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != expected:
                raise ValidationError("Operations workspace ownership cannot be established")
        else:
            path.mkdir(parents=True)
            marker.write_bytes(canonical_bytes(expected))
        return path

    def _identity(self, profile):
        adapter = get_adapter(profile["adapter"])
        identity = {
            "adapter_version": adapter.version,
            "client": profile["client"],
            "files": [_hash_file(path) for path in sorted(set(adapter.identity_files(profile)))],
        }
        return {"sha256": sha256_hex(canonical_bytes(identity)), "details": identity}

    @staticmethod
    def _arguments(adapter, arguments):
        return get_adapter(adapter).validate_arguments(arguments)

    def register(self, capability_id, *, adapter, client, representative, engineer, environment_files=()):
        """Bind explicit approved configuration; no discovery/install commands run."""
        self._logical(capability_id, "profile")
        _text(engineer, "engineer")
        contract = get_adapter(adapter)
        if not isinstance(client, dict):
            raise ValidationError("Capability client must be an object")
        if not isinstance(environment_files, (list, tuple)) or any(not isinstance(p, (str, Path)) for p in environment_files):
            raise ValidationError("environment_files must list explicit public software identity files")
        client = contract.validate_client(json.loads(canonical_bytes(client)), self.project_path, environment_files)
        profile = {"capability_id": capability_id, "adapter": adapter, "client": client,
                   "representative": self._arguments(adapter, representative), "engineer": engineer,
                   "environment_files": [str(Path(path).absolute()) for path in environment_files],
                   "catalog": CATALOG[adapter], "authority": "configured_project_runner_only"}
        profile["identity"] = self._identity(profile)
        existing = self.store.head(self._logical(capability_id, "state"))
        previous = self._state(capability_id) if existing else {"capability_id": capability_id, **self._scope()}
        if previous.get("state") in {"probing", "awaiting_verification"}:
            raise StateError("Cannot rebind a capability with unfinished operational work")
        with self.control.tx() as conn:
            manifest = self._publish(capability_id, "profile", profile, engineer, kind="note", conn=conn)
            return self._transition(previous, "inactive", engineer, "Explicit capability profile registered", conn=conn,
                                    profile_ref=manifest["artifact_ref"], profile_sha256=manifest["body_hash"],
                                    probe_ref=None, verification_ref=None, binding=None, schema_identity=None, failure_ref=None)

    def activate(self, capability_id, *, requester, purpose):
        _text(requester, "requester")
        _text(purpose, "purpose")
        state = self._state(capability_id)
        if state["state"] in {"probing", "awaiting_verification"}:
            raise StateError("Operational work is already active")
        return self._transition(state, "active", requester, purpose, binding=None,
                                activation_session=self.session_id, failure_ref=None)

    def status(self, capability_id):
        state = self._state(capability_id)
        if state["state"] in {"ready", "idle"}:
            _, profile = self._body(state["profile_ref"])
            reason = None
            if not state["binding"] or state["binding"].get("session_id") != self.session_id:
                reason = "Runtime session changed; a fresh operational probe is required"
            else:
                try:
                    if self._identity(profile) != profile["identity"]:
                        reason = "Pinned executable, environment or adapter identity changed"
                except (OSError, ValidationError) as exc:
                    reason = str(exc)
            if reason:
                state = self._transition(state, "degraded", "operations.controller", reason, binding=None)
        return state

    def _execution(self, task_id, params, result, execution_ref, actor, adapter):
        manifest, stored = self._body(execution_ref)
        if manifest["artifact_type"] != "report" or manifest["author"] != actor or stored != result:
            raise ValidationError("Executor did not publish its exact result under the operator identity")
        task = self.tasks.get(task_id)
        if task["state"] not in {"awaiting_review", "completed"}:
            raise ValidationError("Executor task is not awaiting review or completed")
        attempts = self.control._conn.execute("SELECT attempt_id FROM attempts WHERE task_id=?", (task_id,)).fetchall()
        succeeded = [self.tasks.get_attempt(row["attempt_id"]) for row in attempts]
        succeeded = [attempt for attempt in succeeded if attempt["state"] == "succeeded" and attempt["lease_owner"] == actor]
        usage = succeeded[0]["usage"].get("actual", {}) if len(succeeded) == 1 else {}
        dimension = get_adapter(adapter).usage_dimension
        if (not isinstance(usage, dict) or isinstance(usage.get(dimension), bool)
                or not isinstance(usage.get(dimension), (int, float))
                or not math.isfinite(usage[dimension]) or usage[dimension] < 1):
            raise ValidationError("Executor must retain one successful attributed attempt with actual usage")
        context_ref = None
        for item in manifest["inputs"]:
            if item["purpose"] == "subject":
                context, context_body = self._body(item["ref"])
                if (context["author"] == actor and context["artifact_type"] == "note" and context_body == params
                        and (context.get("task_id") == task_id or context["artifact_id"] == f"command/contexts/{task_id}")):
                    context_ref = item["ref"]
        if context_ref is None or (manifest.get("task_id") != task_id and manifest["artifact_id"] != f"command/executions/{task_id}"):
            raise ValidationError("Execution report is not bound to the exact local task/context")
        return {"task_id": task_id, "attempt_id": succeeded[0]["attempt_id"],
                "execution_ref": execution_ref, "context_ref": context_ref, "operator": actor}

    def probe(self, capability_id, executor, *, operator):
        _text(operator, "operator")
        state = self._state(capability_id)
        if state["state"] != "active":
            raise StateError("Activate the capability before probing")
        _, profile = self._body(state["profile_ref"])
        task_id = f"ops-{capability_id}-{uuid.uuid4().hex}"
        params = {"client": profile["client"], **profile["representative"]}
        state = self._transition(state, "probing", operator, "Representative workload dispatched", binding=None)
        execution = {}
        try:
            identity = self._identity(profile)
            if identity != profile["identity"]:
                raise ValidationError("Pinned executable, environment or adapter identity changed; explicit rebinding is required")
            result, execution_ref = executor(task_id, get_adapter(profile["adapter"]).dispatch_kind, params, actor=operator, task_kind="service")
            execution = self._execution(task_id, params, result, execution_ref, operator, profile["adapter"])
            probe = {"profile_ref": state["profile_ref"], "profile_sha256": state["profile_sha256"],
                     "identity": identity, "params": params, **execution}
            with self.control.tx() as conn:
                manifest = self._publish(capability_id, "probes", probe, operator, conn=conn,
                                         subjects=[state["profile_ref"], execution_ref], task_id=task_id,
                                         attempt_id=execution["attempt_id"])
                return self._transition(state, "awaiting_verification", operator, "Representative output requires independent operational checks",
                                        conn=conn, probe_ref=manifest["artifact_ref"], verification_ref=None)
        except Exception as exc:
            with self.control.tx() as conn:
                failure = self._publish(capability_id, "failures", {"task_id": task_id, "error": f"{type(exc).__name__}: {exc}",
                                        "profile_ref": state["profile_ref"], **execution}, operator, conn=conn,
                                        subjects=[state["profile_ref"], execution.get("execution_ref")])
                return self._transition(state, "degraded", operator, "Operational probe failed", conn=conn,
                                        failure_ref=failure["artifact_ref"], binding=None)

    @staticmethod
    def _inspect(profile, result, params, *, representative=True):
        return get_adapter(profile["adapter"]).inspect_result(
            profile, result, params, representative=representative)

    def verify(self, capability_id, *, verifier):
        _text(verifier, "verifier")
        state = self._state(capability_id)
        if state["state"] != "awaiting_verification":
            raise StateError("A completed probe is required before verification")
        profile_manifest, profile = self._body(state["profile_ref"])
        probe_manifest, probe = self._body(state["probe_ref"])
        if verifier in {profile["engineer"], profile_manifest["author"], probe["operator"], probe_manifest["author"]}:
            raise ValidationError("Operational verifier must be independent of profile engineer and execution operator")
        task_id = f"ops-verify-{capability_id}-{uuid.uuid4().hex}"
        self.tasks.create(task_id, "verification", {"profile_ref": state["profile_ref"], "probe_ref": state["probe_ref"]}, verifier)
        self.tasks.admit(task_id, "operations.controller")
        attempt_id = task_id + "-attempt"
        self.tasks.start_attempt(task_id, attempt_id, owner=verifier, lease_ttl_seconds=60)
        try:
            self._check_scope(profile)
            self._check_scope(probe)
            _, result = self._body(probe["execution_ref"])
            self._execution(probe["task_id"], probe["params"], result, probe["execution_ref"], probe["operator"], profile["adapter"])
            checks, schema_identity = self._inspect(profile, result, probe["params"])
            checks.append({"check_id": "profile-identity", "outcome": "passed" if (
                profile_manifest["body_hash"] == state["profile_sha256"] == probe["profile_sha256"]
                and probe["profile_ref"] == state["profile_ref"] and profile["identity"] == probe["identity"] == self._identity(profile)
                and probe["params"] == {"client": profile["client"], **profile["representative"]}
            ) else "failed", "result": "Exact project profile, context and current software identity match the probe"})
            checks.append({"check_id": "schema-stability", "outcome": "passed" if state["schema_identity"] in (None, schema_identity) else "failed",
                           "result": "Previously verified provider schema and server identity remain unchanged"})
        except Exception as exc:
            checks = [{"check_id": "evidence-integrity", "outcome": "failed", "result": f"{type(exc).__name__}: {exc}"}]
            schema_identity = None
        passed = bool(checks) and all(check["outcome"] == "passed" for check in checks)
        report = {"profile_ref": state["profile_ref"], "probe_ref": state["probe_ref"],
                  "execution_ref": probe["execution_ref"], "checks": checks, "schema_identity": schema_identity,
                  "outcome": "passed" if passed else "failed", "domain_acceptance": "not_assessed"}
        with self.control.tx() as conn:
            manifest = self._publish(capability_id, "verifications", report, verifier, kind="verification", conn=conn,
                                     subjects=[state["profile_ref"], state["probe_ref"], probe["execution_ref"]], task_id=task_id, attempt_id=attempt_id)
            binding = {**self._scope(), "capability_id": capability_id, "session_id": self.session_id,
                       "profile_ref": state["profile_ref"], "profile_sha256": state["profile_sha256"],
                       "verification_ref": manifest["artifact_ref"], "identity_sha256": profile["identity"]["sha256"],
                       "schema_identity": schema_identity} if passed else None
            state = self._transition(state, "ready" if passed else "degraded", verifier,
                                     "Independent operational checks passed" if passed else "Independent operational checks failed",
                                     conn=conn, verification_ref=manifest["artifact_ref"], binding=binding,
                                     schema_identity=schema_identity if passed else state["schema_identity"])
        self.tasks.finish_attempt(attempt_id, "succeeded", usage={"operational_checks": len(checks)})
        self.tasks.transition(task_id, "awaiting_review", verifier)
        self.tasks.transition(task_id, "completed", verifier, reason="Operational verification report recorded")
        if self.tasks.get(probe["task_id"])["state"] == "awaiting_review":
            self.tasks.transition(probe["task_id"], "completed" if passed else "blocked", verifier,
                                  reason="Operational output checked")
        return state

    def ensure_ready(self, capability_id, executor, *, operator, verifier, requester="command.controller", purpose="Enable configured operational capability"):
        _, profile = self._body(self._state(capability_id)["profile_ref"])
        _text(verifier, "verifier")
        if verifier in {profile["engineer"], operator}:
            raise ValidationError("Operational verifier must be independent of profile engineer and execution operator")
        self.activate(capability_id, requester=requester, purpose=purpose)
        state = self.probe(capability_id, executor, operator=operator)
        return self.verify(capability_id, verifier=verifier) if state["state"] == "awaiting_verification" else state

    def authorize(self, binding):
        """Validate an exact ready binding before a department's routine use."""
        if not isinstance(binding, dict):
            raise ValidationError("A project capability binding is required")
        self._check_scope(binding)
        state = self.status(binding.get("capability_id"))
        if state["state"] not in {"ready", "idle"} or state["binding"] != binding:
            raise StateError("Capability binding is stale or not ready")
        _, profile = self._body(state["profile_ref"])
        return profile

    def run(self, binding, arguments, executor, *, operator):
        profile = self.authorize(binding)
        capability_id = profile["capability_id"]
        params = {"client": profile["client"], **self._arguments(profile["adapter"], arguments)}
        task_id = f"ops-work-{capability_id}-{uuid.uuid4().hex}"
        execution, checks = {}, []
        try:
            result, ref = executor(task_id, get_adapter(profile["adapter"]).dispatch_kind, params, actor=operator, task_kind=get_adapter(profile["adapter"]).task_kind)
            execution = self._execution(task_id, params, result, ref, operator, profile["adapter"])
            checks, schema = self._inspect(profile, result, params, representative=False)
            self.authorize(binding)
            if not all(check["outcome"] == "passed" for check in checks) or schema != binding["schema_identity"]:
                raise ValidationError("Workload output or provider schema failed operational validation")
            self._publish(capability_id, "workloads", {**execution, "checks": checks, "binding": binding,
                          "domain_acceptance": "not_assessed"}, "operations.controller", subjects=[ref, binding["verification_ref"]])
            if self.tasks.get(task_id)["state"] == "awaiting_review":
                self.tasks.transition(task_id, "completed", "operations.controller", reason="Operational output checked")
            return result, ref
        except Exception as exc:
            state = self._state(capability_id)
            with self.control.tx() as conn:
                failure = self._publish(capability_id, "failures", {
                    "task_id": task_id, "binding": binding, "checks": checks, **execution,
                    "error": f"{type(exc).__name__}: {exc}",
                }, "operations.controller", subjects=[binding["verification_ref"], execution.get("execution_ref")], conn=conn)
                self._transition(state, "degraded", "operations.controller", "Workload failed operational checks", conn=conn,
                                 failure_ref=failure["artifact_ref"], binding=None)
            if execution and self.tasks.get(task_id)["state"] == "awaiting_review":
                self.tasks.transition(task_id, "blocked", "operations.controller", reason="Operational output failed validation")
            raise

    def idle(self, capability_id, *, actor="operations.controller"):
        state = self.status(capability_id)
        if state["state"] != "ready":
            raise StateError("Only a ready capability can become idle")
        return self._transition(state, "idle", actor, "Operations cell idle; verified capability remains available")

    def cleanup(self, capability_id, *, actor="operations.controller"):
        state = self._state(capability_id)
        if state["state"] in {"probing", "awaiting_verification"}:
            raise StateError("Cannot clean a capability while operational work is unfinished")
        path = self.workspace_dir(capability_id)
        # The injected runner owns processes. Only this nonce-bound scratch tree
        # is removed; installed software, runner outputs and evidence stay intact.
        shutil.rmtree(path)
        return self._transition(state, "inactive", actor, "Owned scratch removed and capability binding revoked", binding=None)
