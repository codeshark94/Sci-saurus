"""Execute versioned general-purpose plans through explicit project handlers."""

from __future__ import annotations

import time
import uuid
import math

from scisaurus.core.errors import ValidationError
from scisaurus.core.plans import PlanService
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.resume import deadline_replan


class PlanRunner:
    """A small control-plane runner; domain work stays in injected handlers.

    The executor receives one immutable task contract, exact input references,
    dependency result references, and capability bindings. It returns bounded
    output bodies. A separate verifier callback must inspect those published
    outputs and return an explicit report.
    """

    def __init__(self, control, store, *, executor, verifier, capability_resolver=None, clock=time.monotonic):
        self.control, self.store = control, store
        self.service = PlanService(control, store)
        self.executor, self.verifier = executor, verifier
        self.capability_resolver = capability_resolver or (lambda requirement: None)
        self.clock = clock

    def run(self, plan_ref, *, deadline_seconds, worker_slots=1):
        if (type(deadline_seconds) not in (int, float)
                or not math.isfinite(deadline_seconds) or deadline_seconds <= 0):
            raise ValidationError("plan execution deadline must be positive")
        _, plan = self.service._body(plan_ref)
        started = self.clock()
        while True:
            status = self.service.activate(plan_ref)
            if status["required_complete"]:
                return {**self.service.completion(plan_ref), "elapsed_seconds": self.clock() - started}
            completed = [item["task_id"] for item in status["tasks"] if item["result_ref"]]
            available = max(0.0, deadline_seconds - (self.clock() - started))
            decision = deadline_replan(plan, completed, available_seconds=available, worker_slots=worker_slots)
            if decision["action"] == "retain_and_pause":
                return {**status, "execution_state": "paused_deadline", "deadline_decision": decision,
                        "elapsed_seconds": self.clock() - started}
            selected = set(decision["selected_task_ids"])
            ready = [item for item in status["tasks"] if item["ready"] and item["task_id"] in selected]
            if not ready:
                raise ValidationError("plan made no executable progress toward its required closure")
            for item in ready:
                task = next(candidate for candidate in plan["tasks"] if candidate["id"] == item["task_id"])
                physical = item["physical_task_id"]
                attempt = physical + "-" + uuid.uuid4().hex
                self.service.tasks.start_attempt(physical, attempt, owner=task["owner"], lease_ttl_seconds=deadline_seconds)
                try:
                    dependency_results = {dependency: next(
                        candidate["result_ref"] for candidate in status["tasks"]
                        if candidate["task_id"] == dependency) for dependency in task["depends_on"]}
                    bindings = {requirement: self.capability_resolver(requirement)
                                for requirement in task["capability_requirements"]}
                    result = self.executor({"task": task, "plan_ref": plan_ref,
                                            "input_refs": list(task["input_refs"]),
                                            "dependency_result_refs": dependency_results,
                                            "capability_bindings": bindings})
                    if not isinstance(result, dict) or set(result) != {"outputs", "usage"}:
                        raise ValidationError("plan executor must return exactly outputs and usage")
                    if not isinstance(result["outputs"], dict) or set(result["outputs"]) != set(task["output_logical_ids"]):
                        raise ValidationError("plan executor outputs must exactly match the task write scope")
                    output_refs = []
                    for logical_id, output in result["outputs"].items():
                        if not isinstance(output, dict) or set(output) != {"artifact_type", "body", "media_type"}:
                            raise ValidationError("planned output requires artifact_type, body and media_type")
                        body = output["body"] if isinstance(output["body"], bytes) else canonical_bytes(output["body"])
                        record = self.store.publish_artifact(logical_id=logical_id,
                            artifact_type=output["artifact_type"], author=task["owner"], body=body,
                            media_type=output["media_type"], task_id=physical, attempt_id=attempt,
                            inputs=[{"ref": ref, "purpose": "subject"} for ref in [
                                plan_ref, *task["input_refs"], *dependency_results.values()]])
                        output_refs.append(record["artifact_ref"])
                    review = self.verifier({"task": task, "plan_ref": plan_ref, "output_refs": output_refs,
                                            "dependency_result_refs": dependency_results})
                    if (not isinstance(review, dict) or set(review) != {"author", "checks"}
                            or review["author"] == task["owner"] or not isinstance(review["checks"], list)
                            or not review["checks"] or not all(check.get("outcome") == "passed" for check in review["checks"])):
                        raise ValidationError("plan verifier must be independent and all checks must pass")
                    verification = self.store.publish_artifact(
                        logical_id=f"methods/plan-verifications/{plan['id']}/{task['id']}",
                        artifact_type="verification", author=review["author"], body=canonical_bytes(review),
                        media_type="application/json", inputs=[{"ref": ref, "purpose": "subject"} for ref in output_refs],
                        task_id=physical, attempt_id=attempt)
                    self.service.tasks.finish_attempt(attempt, "succeeded", usage=result["usage"])
                    self.service.tasks.transition(physical, "awaiting_review", task["owner"])
                    self.service.record_result(plan_ref, task["id"], output_refs=output_refs,
                                               verification_refs=[verification["artifact_ref"]])
                except Exception:
                    attempt_row = self.service.tasks.get_attempt(attempt)
                    if attempt_row["state"] == "running":
                        self.service.tasks.finish_attempt(attempt, "failed", usage={})
                    current = self.service.tasks.get(physical)
                    if current["state"] in {"queued", "running", "awaiting_review"}:
                        self.service.tasks.transition(physical, "blocked", "command.controller",
                                                      reason="planned execution or verification failed")
                    raise
