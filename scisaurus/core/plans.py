"""Versioned project plans with dependency-aware, selectively reusable results."""

from __future__ import annotations

import hashlib
import json
import math
import re

from scisaurus.core.errors import StateError, ValidationError
from scisaurus.core.schema import TASK_KINDS, canonical_bytes, parse_ref
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager


_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
PLAN_SCHEMA = "project-plan-1"


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError(f"{name} must be a lowercase identifier")
    return value


def validate_plan(value):
    """Validate the complete immutable plan, including its acyclic task graph."""
    _exact(value, {"schema_version", "id", "revision", "objective", "tasks", "completion"}, "project plan")
    if value["schema_version"] != PLAN_SCHEMA:
        raise ValidationError(f"project plan schema must be {PLAN_SCHEMA}")
    _identifier(value["id"], "plan id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("plan revision must be a positive integer")
    _text(value["objective"], "plan objective")
    if not isinstance(value["tasks"], list) or not value["tasks"]:
        raise ValidationError("project plan requires a nonempty task list")
    tasks, outputs = {}, set()
    fields = {"id", "kind", "owner", "objective", "depends_on", "input_refs",
              "capability_requirements", "output_logical_ids", "estimate_seconds"}
    for task in value["tasks"]:
        _exact(task, fields, "planned task")
        task_id = _identifier(task["id"], "task id")
        if task_id in tasks:
            raise ValidationError("project plan task IDs must be unique")
        if task["kind"] not in TASK_KINDS:
            raise ValidationError("planned task kind is unsupported")
        _text(task["owner"], "task owner")
        _text(task["objective"], "task objective")
        for name in ("depends_on", "input_refs", "capability_requirements", "output_logical_ids"):
            items = task[name]
            if not isinstance(items, list) or any(not isinstance(item, str) or not item for item in items):
                raise ValidationError(f"planned task {name} must be a string list")
            if len(items) != len(set(items)):
                raise ValidationError(f"planned task {name} cannot contain duplicates")
        for dependency in task["depends_on"]:
            _identifier(dependency, "task dependency")
            if dependency == task_id:
                raise ValidationError("a planned task cannot depend on itself")
        for capability in task["capability_requirements"]:
            _identifier(capability, "capability requirement")
        for logical_id in task["output_logical_ids"]:
            if logical_id.count("/") < 1 or logical_id.startswith("/") or logical_id.endswith("/"):
                raise ValidationError("planned output must be a namespace-qualified logical ID")
            if logical_id in outputs:
                raise ValidationError("planned outputs must have one task owner")
            outputs.add(logical_id)
        if (type(task["estimate_seconds"]) not in (int, float)
                or not math.isfinite(task["estimate_seconds"]) or task["estimate_seconds"] <= 0):
            raise ValidationError("task estimate_seconds must be finite and positive")
        tasks[task_id] = task
    for task in tasks.values():
        if set(task["depends_on"]) - set(tasks):
            raise ValidationError("planned task depends on an unknown task")
    visiting, visited = set(), set()
    def visit(task_id):
        if task_id in visiting:
            raise ValidationError("project plan task graph must be acyclic")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]["depends_on"]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)
    for task_id in tasks:
        visit(task_id)
    completion = value["completion"]
    _exact(completion, {"required_task_ids", "required_output_logical_ids", "release_requires_human"},
           "plan completion")
    for name in ("required_task_ids", "required_output_logical_ids"):
        if not isinstance(completion[name], list) or len(completion[name]) != len(set(completion[name])):
            raise ValidationError(f"completion {name} must be a unique list")
    if not completion["required_task_ids"] or set(completion["required_task_ids"]) - set(tasks):
        raise ValidationError("completion must name known required tasks")
    if set(completion["required_output_logical_ids"]) - outputs:
        raise ValidationError("completion requires an output not owned by the plan")
    if type(completion["release_requires_human"]) is not bool:
        raise ValidationError("release_requires_human must be Boolean")
    canonical_bytes(value)
    return value


def task_contract(task):
    return {key: task[key] for key in (
        "id", "kind", "owner", "objective", "depends_on", "input_refs",
        "capability_requirements", "output_logical_ids", "estimate_seconds")}


def task_contract_hash(task):
    return hashlib.sha256(canonical_bytes(task_contract(task))).hexdigest()


class PlanService:
    """Publish, activate, replan, and close work without copying stale approval."""

    def __init__(self, control, store: ArtifactStore):
        if store.control is not control:
            raise ValidationError("plan service and artifact store must share a control connection")
        self.control, self.store = control, store
        self.tasks = TaskManager(control)

    def _body(self, ref):
        manifest = self.store.get(ref)
        if manifest["artifact_type"] != "note":
            raise ValidationError("project plan must be a note artifact")
        value = json.loads(self.store.read_body(manifest["body_hash"]))
        return manifest, validate_plan(value)

    def publish(self, plan, *, author="command.composer"):
        validate_plan(plan)
        for task in plan["tasks"]:
            for ref in task["input_refs"]:
                self.store.get(ref)
        logical_id = f"command/plans/{plan['id']}"
        head = self.store.head(logical_id)
        if head:
            prior = json.loads(self.store.read_body(head["body_hash"]))
            if plan["revision"] != prior["revision"] + 1:
                raise ValidationError("plan revision must increment the current logical plan by one")
        elif plan["revision"] != 1:
            raise ValidationError("a new logical plan starts at revision 1")
        record = self.store.publish_artifact(logical_id=logical_id, artifact_type="note", author=author,
            body=canonical_bytes(plan), media_type="application/json",
            inputs=[{"ref": ref, "purpose": "subject"} for task in plan["tasks"] for ref in task["input_refs"]])
        with self.control.tx() as conn:
            self.control.append_event(conn, actor=author, event_type="plan.published", payload={
                "plan_ref": record["artifact_ref"], "plan_id": plan["id"], "revision": plan["revision"]})
        return record

    @staticmethod
    def _physical_id(plan, task):
        return f"plan-{plan['id']}-{task['id']}-{task_contract_hash(task)[:12]}"

    def _result(self, plan, task, cache):
        task_id = task["id"]
        if task_id in cache:
            return cache[task_id]
        dependencies = {}
        for dependency_id in task["depends_on"]:
            dependency = next(item for item in plan["tasks"] if item["id"] == dependency_id)
            result = self._result(plan, dependency, cache)
            if result is None:
                cache[task_id] = None
                return None
            dependencies[dependency_id] = result["artifact_ref"]
        head = self.store.head(f"command/plan-results/{plan['id']}/{task_id}")
        if head is None:
            cache[task_id] = None
            return None
        body = json.loads(self.store.read_body(head["body_hash"]))
        if (body.get("task_contract_hash") != task_contract_hash(task)
                or body.get("dependency_results") != dependencies):
            cache[task_id] = None
            return None
        if body.get("output_refs") is None or body.get("verification_refs") is None:
            cache[task_id] = None
            return None
        for ref in [*body["output_refs"], *body["verification_refs"]]:
            self.store.get(ref)
        cache[task_id] = {**body, "artifact_ref": head["artifact_ref"]}
        return cache[task_id]

    def state(self, plan_ref):
        _, plan = self._body(plan_ref)
        cache, rows = {}, []
        for task in plan["tasks"]:
            result = self._result(plan, task, cache)
            physical_id = self._physical_id(plan, task)
            row = self.control._conn.execute("SELECT state FROM tasks WHERE task_id=?", (physical_id,)).fetchone()
            ready = result is None and all(self._result(plan, next(
                candidate for candidate in plan["tasks"] if candidate["id"] == dependency), cache)
                for dependency in task["depends_on"])
            rows.append({"task_id": task["id"], "physical_task_id": physical_id,
                         "state": "completed_reused" if result else (row["state"] if row else "uncreated"),
                         "ready": ready, "result_ref": result["artifact_ref"] if result else None})
        required = set(plan["completion"]["required_task_ids"])
        complete = all(cache.get(task_id) is not None for task_id in required)
        outputs = {ref for task_id in required for ref in (cache.get(task_id) or {}).get("output_refs", [])}
        required_outputs = set(plan["completion"]["required_output_logical_ids"])
        output_ids = {self.store.get(ref)["artifact_id"] for ref in outputs}
        complete &= required_outputs.issubset(output_ids)
        return {"plan_ref": plan_ref, "tasks": rows, "required_complete": complete,
                "completion_state": ("needs_human" if complete and plan["completion"]["release_requires_human"]
                                     else "completed" if complete else "in_progress")}

    def activate(self, plan_ref, *, actor="command.scheduler"):
        _, plan = self._body(plan_ref)
        status = self.state(plan_ref)
        for item, task in zip(status["tasks"], plan["tasks"]):
            if item["result_ref"] is not None:
                continue
            if item["state"] == "uncreated":
                self.tasks.create(item["physical_task_id"], task["kind"], {
                    "plan_ref": plan_ref, "task_id": task["id"], "objective": task["objective"],
                    "read_refs": task["input_refs"], "write_logical_ids": task["output_logical_ids"],
                    "capability_requirements": task["capability_requirements"]}, task["owner"])
            current = self.tasks.get(item["physical_task_id"])
            if item["ready"] and current["state"] in {"proposed", "blocked", "paused"}:
                self.tasks.admit(item["physical_task_id"], actor)
        return self.state(plan_ref)

    def record_result(self, plan_ref, task_id, *, output_refs, verification_refs,
                      author="command.integrator"):
        _, plan = self._body(plan_ref)
        matches = [task for task in plan["tasks"] if task["id"] == task_id]
        if len(matches) != 1:
            raise ValidationError("plan result names an unknown task")
        task = matches[0]
        if not isinstance(output_refs, list) or not isinstance(verification_refs, list) or not verification_refs:
            raise ValidationError("plan result requires explicit outputs and independent verification")
        if len(output_refs) != len(set(output_refs)) or len(verification_refs) != len(set(verification_refs)):
            raise ValidationError("plan result references cannot contain duplicates")
        physical_id = self._physical_id(plan, task)
        row = self.tasks.get(physical_id)
        if row["state"] != "awaiting_review":
            raise StateError("plan task must finish execution before integration")
        manifests = [self.store.get(ref) for ref in output_refs]
        if {manifest["artifact_id"] for manifest in manifests} != set(task["output_logical_ids"]):
            raise ValidationError("plan result outputs must exactly match the task write scope")
        attempt_ids = {manifest.get("attempt_id") for manifest in manifests}
        if (len(attempt_ids) != 1 or None in attempt_ids
                or any(manifest.get("task_id") != physical_id or manifest["author"] != task["owner"]
                       for manifest in manifests)):
            raise ValidationError("plan outputs must come from one successful owned task attempt")
        attempt_id = next(iter(attempt_ids))
        attempt = self.tasks.get_attempt(attempt_id)
        if attempt["task_id"] != physical_id or attempt["state"] != "succeeded":
            raise ValidationError("plan outputs must come from one successful owned task attempt")
        checks = [self.store.get(ref) for ref in verification_refs]
        for check in checks:
            if (check["artifact_type"] != "verification" or check["author"] == task["owner"]
                    or check.get("task_id") != physical_id or check.get("attempt_id") != attempt_id
                    or {item.get("ref") for item in check["inputs"]} != set(output_refs)):
                raise ValidationError("plan result verification must independently bind the exact task outputs")
            try:
                body = json.loads(self.store.read_body(check["body_hash"]))
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ValidationError("plan result verification must contain exact JSON") from exc
            if (not isinstance(body, dict) or set(body) != {"author", "checks"}
                    or body["author"] != check["author"] or not isinstance(body["checks"], list)
                    or not body["checks"] or any(not isinstance(item, dict) or item.get("outcome") != "passed"
                                                  for item in body["checks"])):
                raise ValidationError("plan result verification must record independent passed checks")
        cache, dependencies = {}, {}
        for dependency_id in task["depends_on"]:
            dependency = next(item for item in plan["tasks"] if item["id"] == dependency_id)
            result = self._result(plan, dependency, cache)
            if result is None:
                raise ValidationError("plan result cannot precede a required dependency")
            dependencies[dependency_id] = result["artifact_ref"]
        body = {"plan_ref": plan_ref, "task_id": task_id, "physical_task_id": physical_id,
                "task_contract_hash": task_contract_hash(task), "dependency_results": dependencies,
                "output_refs": output_refs, "verification_refs": verification_refs}
        result = self.store.publish_artifact(logical_id=f"command/plan-results/{plan['id']}/{task_id}",
            artifact_type="report", author=author, body=canonical_bytes(body), media_type="application/json",
            inputs=[{"ref": ref, "purpose": "subject"} for ref in [plan_ref, *output_refs, *verification_refs,
                                                                     *dependencies.values()]])
        self.tasks.transition(physical_id, "completed", author, reason="exact outputs independently verified")
        self.activate(plan_ref, actor=author)
        return result

    def completion(self, plan_ref, *, approval_ref=None):
        _, plan = self._body(plan_ref)
        status = self.state(plan_ref)
        if not status["required_complete"]:
            return {**status, "release_authorized": False}
        if plan["completion"]["release_requires_human"]:
            if approval_ref is None:
                return {**status, "release_authorized": False}
            approval = self.store.get(approval_ref)
            if approval["author"] != "principal" or approval["artifact_type"] != "decision_note":
                raise ValidationError("final release approval must be a principal decision note")
            try:
                approval_body = json.loads(self.store.read_body(approval["body_hash"]))
            except (TypeError, ValueError, UnicodeError) as exc:
                raise ValidationError("final release approval must contain exact JSON") from exc
            required = set(plan["completion"]["required_task_ids"])
            result_refs = {item["task_id"]: item["result_ref"] for item in status["tasks"]
                           if item["task_id"] in required}
            output_refs = sorted(ref for task_id in required for ref in json.loads(self.store.read_body(
                self.store.get(result_refs[task_id])["body_hash"]))["output_refs"])
            expected = {"schema_version": "plan-release-approval-1", "plan_ref": plan_ref,
                        "result_refs": result_refs, "output_refs": output_refs}
            if canonical_bytes(approval_body) != canonical_bytes(expected):
                raise ValidationError("final release approval does not bind the exact plan results and outputs")
        return {**status, "completion_state": "completed", "release_authorized": True,
                "approval_ref": approval_ref}
