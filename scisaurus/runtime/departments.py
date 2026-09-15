"""Project-scoped department state and autonomous work-order intake.

Departments are durable responsibility boundaries, not long-lived model
processes.  The module gives the Composer a real project organization: each
department has a charter, inbox, typed work orders, and a queryable backlog.
Templates seed safe defaults; the current mission and the department's
validated proposal contract decide what work is actually admitted.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from scisaurus.core.errors import NotFoundError, StateError, ValidationError
from scisaurus.core.schema import canonical_bytes, now_iso


SCHEMA_VERSION = "project-organization-1"
CHARTER_SCHEMA_VERSION = "department-charter-1"
WORK_ORDER_SCHEMA_VERSION = "department-work-order-1"
_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")

PROPOSAL_KINDS = frozenset({
    "topic_refinement", "literature_expansion", "full_text_retrieval", "additional_experiment",
    "analysis_display", "analysis_repair", "interpretation_expansion",
    "manuscript_revision", "capability_acquisition", "recovery",
})
STAGE_KINDS = frozenset({
    "topic_discovery", "survey", "experiment", "interpretation", "argument", "paper",
})
TASK_KIND_BY_PROPOSAL = {
    "topic_refinement": "production",
    "literature_expansion": "retrieval",
    "full_text_retrieval": "retrieval",
    "additional_experiment": "production",
    "analysis_display": "production",
    "analysis_repair": "production",
    "interpretation_expansion": "production",
    "manuscript_revision": "production",
    "capability_acquisition": "service",
    "recovery": "response",
}
REQUEST_STAGE_KINDS = {
    "topic_refinement": "topic_discovery",
    "literature_expansion": "survey",
    "full_text_retrieval": "survey",
    "additional_experiment": "experiment",
    "analysis_display": "experiment",
    "analysis_repair": "experiment",
    "interpretation_expansion": "interpretation",
    "manuscript_revision": "paper",
}
# A stage's functional role is not a model process and is not the concrete
# chief appointment in a project.  Keeping this routing table beside the
# department charters prevents the Composer and the organization runtime from
# silently developing different ownership maps.
DEFAULT_STAGE_ROUTES = (
    {"stage_kind": "topic_discovery", "department": "research", "functional_role": "intelligence"},
    {"stage_kind": "survey", "department": "research", "functional_role": "intelligence"},
    {"stage_kind": "experiment", "department": "methods", "functional_role": "validation"},
    {"stage_kind": "interpretation", "department": "strategy", "functional_role": "interpretation"},
    {"stage_kind": "argument", "department": "strategy", "functional_role": "argument"},
    {"stage_kind": "paper", "department": "editorial", "functional_role": "composer"},
)
COMMAND_ADDRESSES = {
    "arbiter": {"dept": "executive-command", "agent": "arbiter"},
    "progress": {"dept": "executive-command", "agent": "progress-controller"},
    "intent": {"dept": "executive-command", "agent": "intent-keeper"},
}
VOLATILE_FIELDS = frozenset({
    "created_at", "received_at", "updated_at", "source_event_id", "source_note_ref",
    "source_stage_id",
})


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value.strip()


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _strings(value, name, *, empty=False):
    if not isinstance(value, list) or (not empty and not value):
        raise ValidationError(f"{name} must be an explicit string list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{name} must contain nonempty strings")
    if len(value) != len(set(value)):
        raise ValidationError(f"{name} cannot contain duplicates")


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def default_stage_routes():
    """Return the immutable functional stage map as fresh plain data."""
    return deepcopy(list(DEFAULT_STAGE_ROUTES))


def stage_role(stage_kind):
    """Return the stable functional role used in task and feedback records."""
    _identifier(stage_kind, "stage kind")
    for route in DEFAULT_STAGE_ROUTES:
        if route["stage_kind"] == stage_kind:
            return f"{route['department']}.{route['functional_role']}"
    raise ValidationError(f"unsupported stage kind: {stage_kind}")


def stage_route(stage_kind, charters=None):
    """Resolve one functional route plus its project-specific appointments."""
    _identifier(stage_kind, "stage kind")
    route = next((item for item in DEFAULT_STAGE_ROUTES
                  if item["stage_kind"] == stage_kind), None)
    if route is None:
        raise ValidationError(f"unsupported stage kind: {stage_kind}")
    charter = (charters or {}).get(route["department"])
    if charter is None:
        raise ValidationError(
            f"stage {stage_kind} has no owning department: {route['department']}")
    return {
        **deepcopy(route),
        "role": f"{route['department']}.{route['functional_role']}",
        "chief": charter["chief"],
        "adversary": charter["adversary"],
        "owner_address": {"dept": route["department"], "agent": charter["chief"]},
        "review_address": {"dept": route["department"], "agent": charter["adversary"]},
    }


def agent_roster(charters):
    """Project concrete chief/adversary appointments from validated charters."""
    if not isinstance(charters, dict):
        raise ValidationError("agent roster requires department charters")
    roster = []
    for department in sorted(charters):
        charter = charters[department]
        roster.extend((
            {
                "id": f"{department}.{charter['chief']}",
                "department": department,
                "agent": charter["chief"],
                "appointment": "chief",
                "independent_review": False,
                "stage_kinds": list(charter["stage_kinds"]),
                "proposal_kinds": list(charter["proposal_kinds"]),
            },
            {
                "id": f"{department}.{charter['adversary']}",
                "department": department,
                "agent": charter["adversary"],
                "appointment": "adversary",
                "independent_review": True,
                "stage_kinds": list(charter["stage_kinds"]),
                "proposal_kinds": list(charter["proposal_kinds"]),
            },
        ))
    return roster


DEFAULT_DEPARTMENTS = [
    {
        "id": "research",
        "label": "Research and evidence",
        "chief": "chief",
        "adversary": "adversarial-reviewer",
        "subscriptions": ["research_expansion_required", "source_update", "contradiction", "provider_gap"],
        "proposal_kinds": ["topic_refinement", "literature_expansion", "full_text_retrieval", "capability_acquisition", "recovery"],
        "stage_kinds": ["topic_discovery", "survey"],
        "capability_scope": ["scholarly_search", "source_fetch", "citation_graph", "mcp"],
    },
    {
        "id": "methods",
        "label": "Methods and validation",
        "chief": "chief",
        "adversary": "adversarial-reviewer",
        "subscriptions": ["research_expansion_required", "method_objection", "analysis_failure", "provider_gap"],
        "proposal_kinds": [
            "additional_experiment", "analysis_display", "analysis_repair",
            "capability_acquisition", "recovery",
        ],
        "stage_kinds": ["experiment"],
        "capability_scope": ["local_program", "python", "statistics", "mcp"],
    },
    {
        "id": "strategy",
        "label": "Interpretation and argument",
        "chief": "chief",
        "adversary": "adversarial-reviewer",
        "subscriptions": ["interpretation_request", "argument_objection", "contradiction", "provider_gap"],
        "proposal_kinds": ["interpretation_expansion", "analysis_display", "recovery"],
        "stage_kinds": ["interpretation", "argument"],
        "capability_scope": ["model", "analysis", "mcp"],
    },
    {
        "id": "editorial",
        "label": "Composition and publication",
        "chief": "editor-in-chief",
        "adversary": "human-scientist-reviewer",
        "subscriptions": ["review_objection", "editorial_rejection", "rendering_failure", "provider_gap"],
        "proposal_kinds": ["manuscript_revision", "literature_expansion", "analysis_display", "recovery"],
        "stage_kinds": ["paper"],
        "capability_scope": ["latex", "pdf", "document", "mcp"],
    },
    {
        "id": "operations",
        "label": "Tools and execution environments",
        "chief": "coordinator",
        "adversary": "operational-verifier",
        "subscriptions": ["capability_gap", "tool_failure", "schema_drift", "provider_gap"],
        "proposal_kinds": ["capability_acquisition", "recovery"],
        "stage_kinds": [],
        "capability_scope": ["local_program", "mcp", "api", "environment"],
    },
]


def default_organization():
    return {
        "schema_version": SCHEMA_VERSION,
        "template": "research-project-v1",
        "departments": deepcopy(DEFAULT_DEPARTMENTS),
        "allow_dynamic_proposals": True,
        "max_open_work_orders": 128,
    }


def validate_charter(value):
    fields = {
        "id", "label", "chief", "adversary", "subscriptions", "proposal_kinds",
        "stage_kinds", "capability_scope",
    }
    _exact(value, fields, "department charter")
    _identifier(value["id"], "department id")
    _text(value["label"], "department label")
    _identifier(value["chief"], "department chief")
    _identifier(value["adversary"], "department adversary")
    if value["chief"] == value["adversary"]:
        raise ValidationError("department chief and adversary must be distinct appointments")
    _strings(value["subscriptions"], "department subscriptions")
    _strings(value["proposal_kinds"], "department proposal_kinds")
    if set(value["proposal_kinds"]) - PROPOSAL_KINDS:
        raise ValidationError("department proposal_kinds contains an unsupported work-order kind")
    _strings(value["stage_kinds"], "department stage_kinds", empty=True)
    if set(value["stage_kinds"]) - STAGE_KINDS:
        raise ValidationError("department stage_kinds contains an unsupported stage")
    _strings(value["capability_scope"], "department capability_scope", empty=True)
    return deepcopy(value)


def validate_organization(value):
    fields = {"schema_version", "template", "departments", "allow_dynamic_proposals", "max_open_work_orders"}
    _exact(value, fields, "project organization")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError(f"project organization schema must be {SCHEMA_VERSION}")
    _text(value["template"], "organization template")
    departments = value["departments"]
    if not isinstance(departments, list) or not departments:
        raise ValidationError("project organization requires at least one department")
    validated = [validate_charter(item) for item in departments]
    ids = [item["id"] for item in validated]
    if len(ids) != len(set(ids)):
        raise ValidationError("project department IDs must be unique")
    if type(value["allow_dynamic_proposals"]) is not bool:
        raise ValidationError("allow_dynamic_proposals must be Boolean")
    if type(value["max_open_work_orders"]) is not int or not 1 <= value["max_open_work_orders"] <= 10000:
        raise ValidationError("max_open_work_orders must be between one and 10000")
    return {**value, "departments": validated}


def validate_work_order(value):
    fields = {"schema_version", "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed"}
    _exact(value, fields, "department work order")
    if value["schema_version"] != WORK_ORDER_SCHEMA_VERSION:
        raise ValidationError(f"work-order schema must be {WORK_ORDER_SCHEMA_VERSION}")
    _identifier(value["id"], "work-order id")
    _identifier(value["kind"], "work-order kind")
    if value["kind"] not in PROPOSAL_KINDS:
        raise ValidationError("unsupported work-order kind")
    _text(value["owner"], "work-order owner")
    for key in ("objective", "why", "success_condition", "evidence_needed"):
        _text(value[key], f"work-order {key}")
    return deepcopy(value)


def _department_for_owner(owner):
    owner = _text(owner, "work-order owner")
    if "." in owner:
        owner = owner.split(".", 1)[0]
    return owner


class DepartmentRuntime:
    """Durable project organization used by the Composer command desk."""

    def __init__(self, control, store, messages, tasks, *, project_id, organization=None):
        if store.control is not control or messages.control is not control or tasks.control is not control:
            raise ValidationError("department runtime services must share one project control store")
        self.control, self.store, self.messages, self.tasks = control, store, messages, tasks
        self.project_id = _text(project_id, "project_id")
        self.organization = validate_organization(organization or default_organization())
        self.charters = {item["id"]: item for item in self.organization["departments"]}
        self.manifest_refs = {}
        self._ensure_manifests()

    def _publish_idempotent(self, logical_id, artifact_type, body, *, author="command.composer", subjects=()):
        head = self.store.head(logical_id)
        if head is not None:
            current = json.loads(self.store.read_body(head["body_hash"]))
            comparable_current = {key: value for key, value in current.items()
                                  if key not in VOLATILE_FIELDS}
            comparable_body = {key: value for key, value in body.items()
                               if key not in VOLATILE_FIELDS}
            if comparable_current == comparable_body:
                return head
            parents = [head["artifact_ref"]]
        else:
            parents = []
        return self.store.publish_artifact(
            logical_id=logical_id, artifact_type=artifact_type, author=author,
            body=canonical_bytes(body), media_type="application/json", parents=parents,
            inputs=[{"ref": ref, "purpose": "subject"} for ref in dict.fromkeys(subjects) if ref],
        )

    def _ensure_manifests(self):
        org_record = self._publish_idempotent(
            "command/organization", "note",
            {"project_id": self.project_id, **self.organization,
             "agents": self.agents(), "stage_routes": self.stage_routes(),
             "command_agents": deepcopy(COMMAND_ADDRESSES)},
        )
        self.manifest_refs["organization"] = org_record["artifact_ref"]
        for charter in self.organization["departments"]:
            record = self._publish_idempotent(
                f"command/departments/{charter['id']}/charter", "note",
                {"schema_version": CHARTER_SCHEMA_VERSION, "project_id": self.project_id, **charter},
                subjects=[org_record["artifact_ref"]],
            )
            self.manifest_refs[charter["id"]] = record["artifact_ref"]

    def address(self, department, role="chief"):
        department = _identifier(department, "department")
        if department in self.charters:
            if role == "chief":
                role = self.charters[department]["chief"]
            else:
                _identifier(role, "department role")
        return {"dept": department, "agent": role}

    def stage_route(self, stage_kind):
        """Return the functional owner and concrete appointments for a stage."""
        return stage_route(stage_kind, self.charters)

    def agents(self):
        """Return the concrete project appointments derived from the charters."""
        return agent_roster(self.charters)

    def stage_routes(self):
        """Return all configured default routes that have a live owner."""
        return [
            self.stage_route(route["stage_kind"])
            for route in DEFAULT_STAGE_ROUTES
            if route["department"] in self.charters
        ]

    def _charter_for_owner(self, owner, kind):
        department = _department_for_owner(owner)
        charter = self.charters.get(department)
        if charter is None:
            raise ValidationError(f"work-order owner is not a project department: {owner}")
        if kind not in charter["proposal_kinds"] and not self.organization["allow_dynamic_proposals"]:
            raise ValidationError(f"{department} charter does not admit {kind} work orders")
        return department, charter

    def validate_request(self, proposal):
        """Validate a request against both the public schema and live charter."""
        value = validate_work_order(proposal)
        self._charter_for_owner(value["owner"], value["kind"])
        return value

    def _record_rejection(self, *, department, message_id, request_id, request,
                          reason, source_note_ref):
        """Persist a malformed or unroutable inbox item for later inspection."""
        if not isinstance(request_id, str) or not request_id.strip():
            request_id = hashlib.sha256(canonical_bytes(request)).hexdigest()[:20]
        logical_request_id = request_id if _ID.fullmatch(request_id) else hashlib.sha256(
            request_id.encode("utf-8")).hexdigest()[:20]
        rejection = {
            "schema_version": "department-request-rejection-1", "project_id": self.project_id,
            "message_id": message_id, "department": department, "request_id": request_id,
            "request": deepcopy(request), "reason": reason,
            "source_note_ref": source_note_ref, "state": "rejected", "created_at": now_iso(),
        }
        rejection_ref = self._publish_idempotent(
            f"command/departments/{department}/rejections/{logical_request_id}",
            "decision_note", rejection, subjects=[source_note_ref])
        return {"request_id": request_id, "rejection_ref": rejection_ref["artifact_ref"],
                "reason": reason}

    def reject_request(self, request, *, source_stage_id=None, reason):
        """Persist an invalid continuation request without aborting the run."""
        if isinstance(request, dict):
            raw_id = request.get("id")
            raw_owner = request.get("owner")
        else:
            raw_id = None
            raw_owner = None
        request_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else hashlib.sha256(
            canonical_bytes(request)).hexdigest()[:20]
        try:
            department = _department_for_owner(raw_owner)
        except ValidationError:
            department = "unrouted"
        if department not in self.charters:
            department = "unrouted"
        message_id = f"composer-{source_stage_id or 'workflow'}-{request_id}"
        return self._record_rejection(
            department=department, message_id=message_id, request_id=request_id,
            request=request, reason=reason, source_note_ref=None,
        )

    def _task_id(self, department, order_id, body):
        # Source routing metadata is provenance, not work identity.  A replay
        # from another message or stage must therefore find the same task;
        # substantive changes to the validated order receive a new task
        # generation while the logical artifact retains its version history.
        stable = {key: body[key] for key in (
            "schema_version", "id", "kind", "owner", "objective", "why",
            "success_condition", "evidence_needed") if key in body}
        digest = hashlib.sha256(canonical_bytes(stable)).hexdigest()[:20]
        return f"department-{department}-{order_id}-{digest}"

    def propose(self, proposal, *, source_stage_id=None, source_event_id=None, note_ref=None):
        """Publish and queue one validated autonomous departmental work order."""
        value = validate_work_order(proposal)
        department, charter = self._charter_for_owner(value["owner"], value["kind"])
        previous = self.store.head(f"command/departments/{department}/work-orders/{value['id']}")
        body = {
            **value,
            "project_id": self.project_id,
            "department": department,
            "assigned_agent": charter["chief"],
            "source_stage_id": source_stage_id,
            "source_event_id": source_event_id,
            "source_note_ref": note_ref,
            "state": "proposed",
            "created_at": now_iso(),
        }
        previous_body = {}
        same_generation = False
        if previous is not None:
            try:
                previous_body = json.loads(self.store.read_body(previous["body_hash"]))
            except (OSError, ValueError, TypeError):
                previous_body = {}
            substantive = (
                "schema_version", "id", "kind", "owner", "objective", "why",
                "success_condition", "evidence_needed",
            )
            same_generation = all(previous_body.get(key) == body.get(key) for key in substantive)
            if same_generation:
                prior_state = previous_body.get("state")
                if isinstance(prior_state, str):
                    body["state"] = prior_state
        logical = f"command/departments/{department}/work-orders/{value['id']}"
        task_id = self._task_id(department, value["id"], body)
        if previous is not None and not same_generation:
            # A changed objective is a new work-order generation.  Fence the
            # old generation before admitting the replacement so two chiefs
            # cannot execute contradictory objectives from one logical ID.
            rows = self.control._conn.execute(
                "SELECT task_id, state, payload_json FROM tasks ORDER BY task_id"
            ).fetchall()
            for row in rows:
                if row["task_id"] == task_id or row["state"] in {"completed", "failed", "cancelled", "stale", "rejected"}:
                    continue
                try:
                    prior_payload = json.loads(row["payload_json"])
                except (ValueError, TypeError):
                    continue
                prior_department = prior_payload.get("department")
                if isinstance(prior_department, str) and "." in prior_department:
                    prior_department = prior_department.split(".", 1)[0]
                if (prior_department == department
                        and prior_payload.get("id") == value["id"]):
                    active_attempts = self.control._conn.execute(
                        "SELECT attempt_id FROM attempts WHERE task_id = ? AND state = 'started'",
                        (row["task_id"],),
                    ).fetchall()
                    for attempt in active_attempts:
                        try:
                            self.tasks.reconcile_unknown(attempt["attempt_id"], "command.composer")
                        except (NotFoundError, StateError):
                            # A concurrent recovery may have settled the attempt
                            # between the task scan and this fence.
                            pass
                    self.tasks.transition(
                        row["task_id"], "stale", "command.composer",
                        reason="superseded by a changed work-order generation",
                    )
            # Artifact bodies are immutable.  Record the supersession as its
            # own version before publishing the replacement so the history
            # contains an explicit stale state for the old generation.
            stale_body = deepcopy(previous_body)
            stale_body["state"] = "stale"
            stale_body["superseded"] = True
            stale_body["updated_at"] = now_iso()
            self._publish_idempotent(
                logical, "decision_note", stale_body, author="command.composer",
                subjects=[previous["artifact_ref"]],
            )
        record = self._publish_idempotent(logical, "decision_note", body,
                                           subjects=[note_ref] if note_ref else [])
        payload = {"department": department, "work_order_ref": record["artifact_ref"], **body}
        try:
            task = self.tasks.get(task_id)
        except NotFoundError:
            task = self.tasks.create(task_id, TASK_KIND_BY_PROPOSAL[value["kind"]], payload,
                                     f"{department}.{charter['chief']}")
            task = self.tasks.admit(task_id, "command.composer")
        return {"department": department, "charter_ref": self.manifest_refs[department],
                "work_order_ref": record["artifact_ref"], "task_id": task_id,
                "task_state": task["state"], "kind": value["kind"], "id": value["id"]}

    def _set_work_order_state(self, result, state, *, actor="command.composer"):
        """Project the task lifecycle state into the immutable work-order ledger."""
        if state not in {"proposed", "queued", "running", "awaiting_review", "completed",
                         "blocked", "paused", "failed", "cancelled", "stale", "rejected"}:
            raise ValidationError(f"unsupported work-order state: {state}")
        logical = f"command/departments/{result['department']}/work-orders/{result['id']}"
        head = self.store.head(logical)
        if head is None:
            raise NotFoundError(f"missing work-order artifact: {logical}")
        body = json.loads(self.store.read_body(head["body_hash"]))
        body["state"] = state
        body["updated_at"] = now_iso()
        record = self._publish_idempotent(logical, "decision_note", body, author=actor,
                                           subjects=[head["artifact_ref"]])
        return record

    def receive_message(self, message_id, feedback, note_ref):
        """Persist an inbox item before the command desk acknowledges delivery."""
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValidationError("department inbox requires a message ID")
        if not isinstance(feedback, dict):
            raise ValidationError("department inbox feedback must be an object")
        target = feedback.get("to") or {}
        department = target.get("dept") if isinstance(target, dict) else None
        if not isinstance(department, str) or not department.strip():
            raise ValidationError("department inbox target is missing")
        department = _identifier(department, "department inbox target")
        body = {
            "schema_version": "department-inbox-1", "project_id": self.project_id,
            "message_id": message_id, "department": department,
            "feedback": deepcopy(feedback), "note_ref": note_ref,
            "state": "received", "received_at": now_iso(),
        }
        logical = f"command/departments/{department}/inbox/{message_id}"
        record = self._publish_idempotent(logical, "decision_note", body,
                                           subjects=[note_ref] if note_ref else [])
        proposals = []
        rejected = []
        # The command desk has one explicit system address in addition to the
        # project departments.  Every other target is retained as an unrouted
        # rejection instead of disappearing into an inbox that no chief owns.
        if department not in self.charters and department != "executive-command":
            rejected.append(self._record_rejection(
                department=department, message_id=message_id,
                request_id="unrouted-target", request={"target_department": department},
                reason="target department is not present in the project organization",
                source_note_ref=record["artifact_ref"],
            ))
            return {"inbox_ref": record["artifact_ref"], "department": department,
                    "proposals": proposals, "rejected": rejected}

        requests = []
        invalid_containers = []

        def collect(label, value):
            if value is None:
                return
            if not isinstance(value, list):
                invalid_containers.append((label, value))
                return
            requests.extend((label, item) for item in value)

        collect("research_expansion_requests", feedback.get("research_expansion_requests"))
        collect("research_requests", feedback.get("research_requests"))
        event = feedback.get("event")
        if isinstance(event, dict):
            for key in ("expansion_requests", "research_requests"):
                collect(f"event.{key}", event.get(key))
        elif event is not None:
            invalid_containers.append(("event", event))
        for label, value in invalid_containers:
            rejected.append(self._record_rejection(
                department=department, message_id=message_id,
                request_id=f"{label.replace('.', '-')}-container",
                request={"container": label, "value": deepcopy(value)},
                reason=f"{label} must be a list when supplied",
                source_note_ref=record["artifact_ref"],
            ))
        seen = set()
        for source_label, request in requests:
            if not isinstance(request, dict):
                rejected.append(self._record_rejection(
                    department=department, message_id=message_id,
                    request_id=f"{source_label.replace('.', '-')}-item-{len(rejected) + 1}",
                    request=request,
                    reason="work-order request must be an object",
                    source_note_ref=record["artifact_ref"],
                ))
                continue
            raw_request_id = request.get("id")
            request_key = raw_request_id if isinstance(raw_request_id, str) else hashlib.sha256(
                canonical_bytes(request)).hexdigest()
            if request_key in seen:
                continue
            seen.add(request_key)
            proposal = {key: request.get(key) for key in (
                "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed")}
            proposal["schema_version"] = WORK_ORDER_SCHEMA_VERSION
            try:
                if not all(isinstance(proposal.get(key), str) and proposal[key].strip() for key in proposal):
                    raise ValidationError("work-order request is missing a required string field")
                proposals.append(self.propose(
                    proposal, source_stage_id=feedback.get("stage_id"),
                    source_event_id=feedback.get("event_id"), note_ref=record["artifact_ref"],
                ))
            except (ValidationError, StateError) as exc:
                request_id = request.get("id") if isinstance(request.get("id"), str) else hashlib.sha256(
                    canonical_bytes(request)).hexdigest()[:20]
                rejected.append(self._record_rejection(
                    department=department, message_id=message_id, request_id=request_id,
                    request=request, reason=f"{type(exc).__name__}: {exc}",
                    source_note_ref=record["artifact_ref"],
                ))
        return {"inbox_ref": record["artifact_ref"], "department": department,
                "proposals": proposals, "rejected": rejected}

    def activate_work_orders(self, requests, *, actor="command.composer"):
        """Admit and mark the scoped work orders as running for a continuation.

        The Composer remains the only stage dispatcher, but this transition
        makes the department backlog reflect actual execution rather than a
        permanently queued notification.
        """
        active = []
        for request in requests or []:
            if not isinstance(request, dict):
                continue
            proposal = {key: request.get(key) for key in (
                "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed")}
            proposal["schema_version"] = WORK_ORDER_SCHEMA_VERSION
            result = self.propose(proposal, source_stage_id=request.get("source_stage_id"))
            task = self.tasks.get(result["task_id"])
            if task["state"] in {"blocked", "paused"}:
                task = self.tasks.transition(
                    result["task_id"], "queued", actor,
                    reason="scoped continuation reopens work order",
                )
            if task["state"] == "queued":
                task = self.tasks.transition(
                    result["task_id"], "running", actor,
                    reason="scoped continuation admitted",
                )
            record = self._set_work_order_state(result, task["state"], actor=actor)
            active.append({**result, "work_order_ref": record["artifact_ref"],
                           "task_state": task["state"]})
        return active

    def resolve_work_orders(self, requests, *, stage_kind, outcome, actor="command.composer"):
        """Close work orders whose owning stage produced an accepted result.

        A hold keeps its work order visible as running so the next autonomous
        continuation can continue the same objective.  A ready result closes
        only the matching request; unrelated departmental work remains intact.
        """
        resolved = []
        for request in requests or []:
            if not isinstance(request, dict) or REQUEST_STAGE_KINDS.get(request.get("kind")) != stage_kind:
                continue
            proposal = {key: request.get(key) for key in (
                "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed")}
            proposal["schema_version"] = WORK_ORDER_SCHEMA_VERSION
            result = self.propose(proposal, source_stage_id=request.get("source_stage_id"))
            task_id = result["task_id"]
            task = self.tasks.get(task_id)
            if outcome in {"completed", "accepted", "candidate_needs_review"}:
                if task["state"] in {"blocked", "paused"}:
                    task = self.tasks.transition(task_id, "queued", actor, reason="owning stage recovered work order")
                if task["state"] == "queued":
                    task = self.tasks.transition(task_id, "running", actor, reason="work order execution observed")
                if task["state"] == "running":
                    task = self.tasks.transition(task_id, "awaiting_review", actor, reason="owning stage returned")
                if task["state"] == "awaiting_review":
                    task = self.tasks.transition(task_id, "completed", actor, reason="owning stage accepted result")
            elif outcome == "blocked" and task["state"] in {"queued", "running"}:
                task = self.tasks.transition(task_id, "blocked", actor, reason="owning stage blocked")
            self._set_work_order_state(result, task["state"], actor=actor)
            resolved.append({"task_id": task_id, "state": task["state"], "request_id": request.get("id")})
        return resolved

    def snapshot(self):
        """Return a compact backlog projection for checkpoints and UI status."""
        counts = {department: {"proposed": 0, "queued": 0, "running": 0, "awaiting_review": 0,
                               "completed": 0, "blocked": 0, "paused": 0, "other": 0}
                  for department in self.charters}
        open_orders = []
        rows = self.control._conn.execute("SELECT task_id, state, payload_json FROM tasks ORDER BY task_id").fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (ValueError, TypeError):
                continue
            department = payload.get("department")
            if isinstance(department, str) and department not in counts and "." in department:
                # Older Composer tasks stored the full role address.  Keep
                # their backlog visible while new tasks use the department
                # ID plus a separate role field.
                department = department.split(".", 1)[0]
            if department not in counts:
                continue
            state = row["state"]
            bucket = state if state in counts[department] else "other"
            counts[department][bucket] += 1
            if payload.get("work_order_ref") and state not in {"completed", "failed", "cancelled", "stale"}:
                work_order_ref = payload.get("work_order_ref")
                order_id = payload.get("id")
                if isinstance(order_id, str):
                    head = self.store.head(
                        f"command/departments/{department}/work-orders/{order_id}")
                    if head is not None:
                        work_order_ref = head["artifact_ref"]
                open_orders.append({"task_id": row["task_id"], "department": department,
                                    "state": state, "work_order_ref": work_order_ref,
                                    "kind": payload.get("kind"), "objective": payload.get("objective")})
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": self.project_id,
            "template": self.organization["template"],
            "departments": deepcopy(self.organization["departments"]),
            "agents": self.agents(),
            "stage_routes": self.stage_routes(),
            "command_agents": deepcopy(COMMAND_ADDRESSES),
            "manifest_refs": deepcopy(self.manifest_refs),
            "allow_dynamic_proposals": self.organization["allow_dynamic_proposals"],
            "backlog_counts": counts,
            "open_work_orders": open_orders[:self.organization["max_open_work_orders"]],
        }


__all__ = [
    "SCHEMA_VERSION", "CHARTER_SCHEMA_VERSION", "WORK_ORDER_SCHEMA_VERSION", "PROPOSAL_KINDS",
    "DEFAULT_DEPARTMENTS", "DEFAULT_STAGE_ROUTES", "COMMAND_ADDRESSES",
    "default_organization", "default_stage_routes", "stage_role", "stage_route", "agent_roster",
    "validate_charter", "validate_organization", "validate_work_order", "DepartmentRuntime",
]
