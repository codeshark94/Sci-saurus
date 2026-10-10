"""Immutable Methods invoices and their containing Composer attempt."""
from datetime import datetime
import hashlib
import json
import re

from scisaurus.core.errors import ValidationError
from scisaurus.core.tasks import TaskManager


def repair_panel_invoice_owner(store, *, project_id, workflow_id, stage_records, stage_id, invoice, usage_keys):
    control = store.control
    tasks = TaskManager(control)
    bodies, records = {}, {}
    for name, schema in (("assignment_plan_ref", "department-stage-assignment-1"),
                         ("chief_synthesis_ref", "department-chief-synthesis-1")):
        record = store.get(invoice[name])
        raw = store.read_body(record["body_hash"])
        body = json.loads(raw)
        if (hashlib.sha256(raw).hexdigest() != record["body_hash"]
                or body.get("schema_version") != schema
                or body.get("project_id") != project_id
                or body.get("stage_kind") != "experiment"):
            raise ValidationError("Methods budget invoice has invalid immutable provenance")
        bodies[name], records[name] = body, record
    plan, chief = bodies["assignment_plan_ref"], bodies["chief_synthesis_ref"]
    panel_id = plan.get("stage_id")
    base = re.sub(r"[^a-z0-9-]+", "-", str(stage_id).casefold()).strip("-") or "experiment"
    suffix = ("-repair-panel-" + panel_id.rsplit("-repair-panel-", 1)[1]
              if isinstance(panel_id, str) and "-repair-panel-" in panel_id else None)
    if (plan.get("stage_id") != chief.get("stage_id")
            or suffix is None or panel_id != base[:64 - len(suffix)] + suffix
            or plan.get("attempt_number") != chief.get("attempt_number")
            or any(chief.get("usage", {}).get(key, 0) != invoice["usage"].get(key, 0)
                   for key in usage_keys)):
        raise ValidationError("Methods budget invoice differs from its immutable synthesis")
    start = datetime.fromisoformat(records["assignment_plan_ref"]["created_at"]).timestamp()
    finish = datetime.fromisoformat(records["chief_synthesis_ref"]["created_at"]).timestamp()
    if finish < start:
        raise ValidationError("Methods budget invoice has reversed provenance interval")
    record = stage_records.get(stage_id, {})
    candidates = {item.get("attempt_id"): item.get("cycle")
                  for item in record.get("attempts", []) if isinstance(item, dict)}
    if isinstance(record.get("attempt_id"), str):
        candidates.setdefault(record["attempt_id"], None)
    owners = []
    for attempt_id, cycle in candidates.items():
        if not isinstance(attempt_id, str):
            continue
        attempt = tasks.get_attempt(attempt_id)
        task = tasks.get(attempt["task_id"])
        opened = datetime.fromisoformat(attempt["created_at"]).timestamp()
        closed = (datetime.fromisoformat(attempt["finished_at"]).timestamp()
                  if attempt.get("finished_at") else None)
        if (attempt.get("lease_owner") == "command.composer"
                and task.get("payload", {}).get("stage_id") == stage_id
                and opened <= start):
            owners.append((opened, closed, attempt_id, cycle))
    latest = max((item[0] for item in owners), default=None)
    owners = [item for item in owners if item[0] == latest
              and (item[1] is None or finish <= item[1])]
    if len(owners) != 1:
        raise ValidationError("Methods budget invoice has ambiguous parent attempt/cycle ownership")
    attempt_id, cycle = owners[0][2:]
    attempt = tasks.get_attempt(attempt_id)
    if "model_budget_cycle" in attempt.get("payload", {}):
        cycle = attempt["payload"]["model_budget_cycle"]
    elif invoice.get("model_budget_owner") is None:
        if cycle is not None and (type(cycle) is not int or cycle < 0):
            raise ValidationError("Methods budget invoice has invalid admitted cycle ownership")
        checkpoint_row = control._conn.execute(
            "SELECT body_hash FROM artifacts WHERE artifact_type='progress_checkpoint' "
            "AND logical_id LIKE 'command/composer/checkpoints/%' AND created_at>=? AND created_at<=? "
            "ORDER BY created_at ASC LIMIT 1", (attempt["created_at"], records["assignment_plan_ref"]["created_at"])).fetchone()
        checkpoint_raw = store.read_body(checkpoint_row["body_hash"]) if checkpoint_row else None
        if checkpoint_raw is not None and hashlib.sha256(checkpoint_raw).hexdigest() != checkpoint_row["body_hash"]:
            raise ValidationError("Methods budget admission checkpoint digest differs from its artifact")
        checkpoint = json.loads(checkpoint_raw) if checkpoint_raw is not None else None
        if (not isinstance(checkpoint, dict)
                or checkpoint.get("schema_version") != "composer-checkpoint-1"
                or checkpoint.get("workflow_id") != workflow_id
                or checkpoint.get("stages", {}).get(stage_id, {}).get("attempt_id") != attempt_id):
            raise ValidationError("Methods budget invoice has ambiguous admitted cycle ownership")
        admitted_global_cycle = checkpoint.get("continuation_cycles")
        reopened = checkpoint.get("reopened_stage_ids")
        if (type(admitted_global_cycle) is not int or admitted_global_cycle < 0
                or not isinstance(reopened, list)
                or any(not isinstance(item, str) for item in reopened)):
            raise ValidationError("Methods budget invoice has invalid immutable admission scope")
        admitted_model_cycle = (admitted_global_cycle
                                if stage_id in reopened else 0)
        if cycle is not None and cycle not in (admitted_global_cycle, admitted_model_cycle):
            raise ValidationError("Methods budget invoice has ambiguous admitted cycle ownership")
        cycle = admitted_model_cycle
    elif isinstance(invoice.get("model_budget_owner"), dict):
        cycle = invoice["model_budget_owner"].get("cycle")
    if type(cycle) is not int or cycle < 0:
        raise ValidationError("Methods budget invoice has invalid admitted cycle ownership")
    return {"attempt_id": attempt_id, "cycle": cycle,
            "cycle_pinned": "model_budget_cycle" in attempt.get("payload", {})}
