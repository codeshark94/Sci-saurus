"""Project-scoped parallel proposals and exact composed-document verification."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from pathlib import Path
import shutil
import signal
import threading
import time

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes, parse_ref
from scisaurus.runtime.contracts import required_checks, required_strings, preserves_literals, validate_verdict
from scisaurus.runtime.execution import ExecutionRuntime, _invoke_worker
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.operations import OperationsCell
from scisaurus.runtime.project_config import validate_project_config
from scisaurus.runtime.scores import all_units, project_contract, validate_text
from scisaurus.runtime.time_policy import TimePolicy


_REVIEW = (
    "Verify the assigned scope independently against the governing supplied facts and objective. "
    "Challenge a supervisor condition that contradicts those facts; preserve collection, measurement, processing, "
    "verification, and reporting states exactly. Return exactly checks, regressions, uncertainties, observations, rationale. "
    "checks is a list of {check_id,kind,outcome,method,result}; kind is resolution or regression and outcome is "
    "passed, failed, insufficient_evidence, or check_failed. Execute every required_checks entry, copying its check_id "
    "and kind exactly. Include at least one resolution check and concrete comparisons. Do not use the reserved "
    "check_id mechanical-preservation. regressions and uncertainties are lists of substantive strings; material "
    "unresolved acceptance questions belong in uncertainties. An uncertainty is blocking: use it only when a required "
    "check cannot be passed or a concrete acceptance criterion remains unresolved. If every required check passes and "
    "there are no regressions, uncertainties MUST be an empty list. Informational notes, intentionally bounded states "
    "already required by the supplied objective (including an insufficient_evidence literature assessment when the "
    "document makes no novelty claim), and wording alternatives within the resolution condition belong in observations. "
    "observations is a list of {observation,reason_nonblocking}; both strings must explain why the note cannot affect "
    "acceptance. Do not downgrade failed checks or insufficient evidence to observations. Use empty lists when there "
    "are no items. rationale is a substantive string. "
    "Do not propose replacement deliverable content."
)


class ProjectRunner(ExecutionRuntime):
    def __init__(self, project_dir, config, *, on_progress=None):
        super().__init__(project_dir, deepcopy(validate_project_config(config)), worker_target=_invoke_worker,
                         on_progress=on_progress)
        self.score, self.layout = project_contract(self.config)
        self.units = {unit["id"]: unit for unit in all_units(self.layout)}
        self.editable = {key: unit for key, unit in self.units.items() if unit["editable"]}
        self.unit_records, self.claims, self.proposals = {}, {}, {}
        self.capabilities, self.rounds, self.worker_errors = {}, [], []
        self.operations = OperationsCell(self.control, self.store, project_id=self.config["project_id"])
        self.registered_capabilities = []
        self.time_policy = (TimePolicy(stage_seconds=self.score["stage_seconds"], unit_count=len(self.editable),
            worker_slots=self.config["limits"]["concurrent_calls"] - 1,
            wall_clock_seconds=self.config["limits"]["wall_clock_seconds"], policy=self.config.get("time_policy"))
            if self.score["stage_seconds"] else None)
        if self.time_policy:
            self.time_policy.started_at = self.started
            self.deadline = min(self.deadline, self.started + self.time_policy.hard_seconds)
        self.time_decisions = []

    def run(self):
        if threading.current_thread() is not threading.main_thread():
            raise ValidationError("project run requires the main thread for cancellation reconciliation")
        previous = signal.getsignal(signal.SIGTERM)
        def terminate(signum, frame):
            raise KeyboardInterrupt("termination requested")
        signal.signal(signal.SIGTERM, terminate)
        try:
            return self._run()
        finally:
            signal.signal(signal.SIGTERM, previous)

    def _required_checks(self, extra=None):
        return required_checks({**{f"score-{c['check_id']}": c["requirement"] for c in self.score["checks"]}, **(extra or {})})

    def _admit_time(self, stage, *, task_count=1, pending_review_count=0):
        self._ensure_active()
        if not self.time_policy:
            return
        decision = self.time_policy.admit(stage, task_count=task_count, pending_review_count=pending_review_count)
        record = self._publish(f"command/time-decisions/{len(self.time_decisions) + 1}", "note", decision,
                               "command.controller", subjects=[self.score_ref])
        self.time_decisions.append({**decision, "ref": record["artifact_ref"]})
        if not decision["allowed"]:
            raise ValidationError(f"time admission deferred {stage}: {decision['reason']}")

    def _timed_model(self, stage, *args, **kwargs):
        self._admit_time(stage)
        start = time.monotonic()
        result = self._model(*args, **kwargs)
        if self.time_policy:
            self.time_policy.observe(stage, time.monotonic() - start)
        return result

    def _initialize(self):
        self.score_record = self._publish(f"command/scores/{self.score['id']}", "note", {
            "schema_version": "bounded-artifact-score-1", "score": self.score, "deliverable": self.layout,
            "time_policy": self.config.get("time_policy")}, "principal")
        self.score_ref = self.score_record["artifact_ref"]
        self.context = self._publish("inputs/context", "note",
                                     {"text": self.config["supplied_context"]}, "principal")
        for claim in self.config["claims"]:
            self.claims[claim["id"]] = self._publish(f"strategy/claims/{claim['id']}", "claim",
                {**claim, "kind": "required_meaning", "context_ref": self.context["artifact_ref"]}, "principal",
                subjects=[self.context["artifact_ref"]])
        self.criterion = self._publish("command/criteria/project", "note", {
            "objective": self.config["objective"], "unit_contracts": list(self.units.values()), "score_ref": self.score_ref,
            "claim_refs": {key: claim["artifact_ref"] for key, claim in self.claims.items()},
            "required_checks": self._required_checks(), "structure": "preserve exact group and unit membership/order",
            "adoption": "all changed units and composed candidate independently verified before atomic adoption",
        }, "principal", subjects=[self.score_ref, self.context["artifact_ref"], *[c["artifact_ref"] for c in self.claims.values()]])
        tree = {"units": [], "assembly_dependencies": [self.context["artifact_ref"], self.criterion["artifact_ref"],
                                                       self.score_ref, *[c["artifact_ref"] for c in self.claims.values()]]}
        for section in self.layout["groups"]:
            heading = self.documents.publish_unit(logical_id=f"strategy/units/{section['id']}", kind="heading",
                                                   text=section["title"], author="principal")
            children = []
            for unit in section["units"]:
                record = self.documents.publish_unit(logical_id=f"strategy/units/{unit['id']}", kind=unit["kind"],
                    text=unit["text"], author="principal", purpose=unit["objective"],
                    claim_refs=[self.claims[c]["artifact_ref"] for c in unit["claim_ids"]])
                self.unit_records[unit["id"]] = record
                children.append({"ref": record["artifact_ref"], "children": []})
            tree["units"].append({"ref": heading["artifact_ref"], "children": children})
        self.baseline = self.documents.publish_manifest(document_id=f"strategy/documents/{self.layout['id']}", tree=tree, author="principal")
        self.store.adopt(self.baseline["artifact_id"], target_version=self.baseline["version"],
                         expected_accepted_version=None, actor="principal")
        self.incumbent = self.baseline["artifact_ref"]
        self._checkpoint("baseline_recorded", force=True)

    def _setup_operations(self):
        needed = {item["capability_id"] for item in self.score["workloads"] + self.score["candidate_checks"]}
        definitions = {item["id"]: item for item in self.score["capabilities"] if item["id"] in needed}
        for capability, definition in definitions.items():
            client = deepcopy(definition["client"])
            if definition["adapter"] in {"mcp_fetch", "local_program"}:
                command = list(client["command"])
                command[0] = str(Path(command[0]).absolute()) if "/" in command[0] else (shutil.which(command[0]) or command[0])
                client.update(command=command, cwd=str(self.operations.workspace_dir(capability)), own_process_group=False)
            self.operations.register(capability, adapter=definition["adapter"], client=client,
                representative=definition["representative"], engineer="operations.engineer",
                environment_files=definition["environment_files"])
            self.registered_capabilities.append(capability)
        for workload in self.score["workloads"]:
            definition = definitions[workload["capability_id"]]
            self.operations._arguments(definition["adapter"], workload["arguments"])
        for check in self.score["candidate_checks"]:
            self.operations._arguments("local_program", {"input": {**check["input"],
                check["text_field"]: self.units[check["unit_id"]]["text"]}})
        for capability in definitions:
            state = self.operations.ensure_ready(capability, self._call, operator="operations.operator",
                verifier="operations.verifier", purpose=self.config["objective"])
            if state["state"] != "ready":
                raise ValidationError(f"required project capability unavailable: {capability}: {state['reason']}")
            self.capabilities[capability] = state["binding"]
            self.operations.idle(capability)
            self.information_changes.append({"kind": "verified_capability", "ref": state["verification_ref"]})
            self._checkpoint("capability_ready", force=True)

    def _retrieve(self, role, label):
        campaign = self._publish(f"kb/campaigns/{label}", "search_campaign", {
            "owner": role, "objective": self.config["objective"], "workloads": self.score["workloads"],
            "scope": "explicitly declared capabilities and inputs; not exhaustive coverage",
        }, role, subjects=[self.score_ref])
        captures = []
        definitions = {item["id"]: item for item in self.score["capabilities"]}
        for workload in self.score["workloads"]:
            capability = workload["capability_id"]
            result, execution = self.operations.run(self.capabilities[capability], workload["arguments"], self._call, operator=role)
            adapter = definitions[capability]["adapter"]
            if adapter == "crossref":
                record = self._publish(f"kb/queries/{label}-{workload['id']}", "query_record", result, role,
                                      subjects=[campaign["artifact_ref"], execution])
                for index, source in enumerate(result["sources"]):
                    discovery = self._publish(f"kb/discoveries/{label}-{workload['id']}-{index}", "discovery_record", source,
                                              role, subjects=[record["artifact_ref"]])
                    self._publish(f"kb/references/{label}-{workload['id']}-{index}", "reference_card", source,
                                  role, subjects=[discovery["artifact_ref"]])
                self.information_changes.append({"kind": "metadata_discovery", "ref": record["artifact_ref"]})
                continue
            record = self._publish(f"kb/captures/{label}-{workload['id']}", "source_capture", result, role,
                                  subjects=[campaign["artifact_ref"], execution])
            captures.append({"ref": record["artifact_ref"], "url": workload["arguments"].get("url"),
                "text": result["text"], "capture_sha256": result["capture_sha256"],
                "representation": "program_output" if adapter == "local_program" else "extracted_text",
                "execution_ref": execution, "capability_id": capability})
            self.information_changes.append({"kind": "captured_program_output" if adapter == "local_program" else "source_capture",
                                             "ref": record["artifact_ref"]})
        self._publish(f"kb/coverage/{label}", "coverage_report", {
            "campaign_ref": campaign["artifact_ref"], "capture_refs": [c["ref"] for c in captures],
            "scope": "declared_workloads", "termination_reason": "configured_routes_completed",
            "independent_context": role == "methods.researcher",
        }, role, subjects=[campaign["artifact_ref"], *[c["ref"] for c in captures]])
        self.sources.extend(captures)
        return captures

    def _document(self, ref):
        sections = []
        for section in self.documents.get_tree(ref)["units"]:
            heading = self.documents.read_unit(section["ref"])
            sections.append({"title": heading["text"], "ref": section["ref"], "units": [
                {"unit_id": parse_ref(node["ref"])[1].removeprefix("units/"), "ref": node["ref"],
                 "kind": self.documents.read_unit(node["ref"])["kind"],
                 "text": self.documents.read_unit(node["ref"])["text"]} for node in section["children"]]})
        return {"title": self.layout["title"], "groups": sections}

    def _supervise(self):
        decision, execution = self._timed_model("supervision", "supervise-project", "command.supervisor", {
            "assignment": "Identify defects in the editable output units and define a purpose-directed repair. "
                "Return exactly allegation, material_impact, resolution_condition, rationale (substantive strings) and "
                "unit_focus (list of {unit_id,change_focus}). Include each editable unit exactly once. Respect supplied "
                "facts and required claims; do not add unrelated requirements or propose changing immutable units.",
            "domain": self.score["domain"], "score_ref": self.score_ref, "required_checks": self._required_checks(),
            "objective": self.config["objective"], "document": self._document(self.baseline["artifact_ref"]),
            "editable_units": list(self.editable.values()), "claims": self.config["claims"],
            "supplied_context": self.config["supplied_context"],
        }, task_kind="service")
        required_strings(decision, ("allegation", "material_impact", "resolution_condition", "rationale"))
        focus = self._targets(decision.get("unit_focus"), required=set(self.editable))
        self._complete("supervise-project")
        self._publish("command/supervision/initial", "supervision_decision", decision, "command.supervisor",
                      subjects=[execution, self.criterion["artifact_ref"], self.baseline["artifact_ref"]])
        critique = self.issues.publish_critique(critique_id="project", author_role="command.supervisor",
            target_ref=self.baseline["artifact_ref"], target_location=",".join(self.editable),
            criterion_ref=self.criterion["artifact_ref"], allegation=decision["allegation"],
            basis={"evidence_refs": [self.context["artifact_ref"]]}, material_impact=decision["material_impact"],
            proposed_severity="blocking", resolution_condition=decision["resolution_condition"])
        self.issues.register_issue(issue_id="project", critique_ref=critique["artifact_ref"])
        self.issues.triage("project", "command.controller", admissible=True, reason="Specific editable units and governing contract bound")
        return decision, focus

    def _targets(self, values, *, required=()):
        if not isinstance(values, list) or not values:
            raise ValidationError("supervision must specify targeted unit changes")
        targets = {}
        for value in values:
            if not isinstance(value, dict) or set(value) != {"unit_id", "change_focus"}:
                raise ValidationError("target requires exactly unit_id and change_focus")
            required_strings(value, ("unit_id", "change_focus"))
            if value["unit_id"] not in self.editable or value["unit_id"] in targets:
                raise ValidationError("unknown, immutable, or duplicated supervised unit target")
            targets[value["unit_id"]] = value["change_focus"]
        if not set(required).issubset(targets):
            raise ValidationError("supervision omitted units with unresolved work")
        return targets

    def _model_jobs(self, jobs):
        specs = [{"task_id": job["task_id"], "kind": "model", "params": {"client": self.config["model"],
                  "prompt": json.dumps(job["assignment"], ensure_ascii=False)}, "actor": job["actor"],
                  "task_kind": job["task_kind"], **({"reservation_id": job["reservation_id"]} if job.get("reservation_id") else {})}
                 for job in jobs]
        results = self._call_batch(specs)
        if self.time_policy:
            for job in jobs:
                outcome = results[job["task_id"]]
                if outcome["ok"]:
                    self.time_policy.observe(job["time_stage"], outcome["result"]["elapsed_seconds"])
        for task_id, outcome in results.items():
            if outcome["ok"]:
                try:
                    model = ModelResult(**outcome["result"])
                    if model.finish_reason != "stop":
                        raise ValidationError(f"model generation did not finish normally: {model.finish_reason}")
                    outcome["value"] = model.json_object()
                except Exception as exc:
                    outcome.update(ok=False, error=f"{type(exc).__name__}: {exc}", outcome_known=True)
                    self._block_task(task_id, outcome["error"])
        return results

    def _block_task(self, task_id, reason):
        if self.tasks.get(task_id)["state"] == "awaiting_review":
            self.tasks.transition(task_id, "blocked", "command.controller", reason=reason)

    def _propose(self, number, focus, supervisor, sources, feedback):
        jobs = []
        for unit_id, change_focus in focus.items():
            record, unit = self.unit_records[unit_id], self.editable[unit_id]
            jobs.append({"task_id": f"write-{number}-{unit_id}", "actor": f"strategy.writer-{unit_id}", "task_kind": "production",
                "time_stage": "production",
                "assignment": {"assignment": "Revise only your assigned output unit. Return exactly unit_id, baseline_unit_ref, "
                    "text, support. Copy unit_id and baseline_unit_ref exactly. text must satisfy output_kind: "
                    "paragraph/list_item is single-line prose; json is a complete JSON value without fences; code is valid Python source. "
                    "Present content without control IDs or references to the user or assignment. "
                    "support is a list of {source_ref,quote,supports}; quotes must exactly match a supplied source capture "
                    "and contain at most 20 words. An explicit empty list is valid when all assertions use supplied facts "
                    "or permitted interpretation. Do not add external background merely to fill the support list. "
                    "The other output units are read-only context and must not be returned or changed.",
                    "output_kind": unit["kind"], "domain": self.score["domain"], "score_ref": self.score_ref,
                    "program_requirements": [c for c in self.score["candidate_checks"] if c["unit_id"] == unit_id],
                    "unit_id": unit_id, "baseline_unit_ref": record["artifact_ref"], "baseline": unit["text"],
                    "objective": unit["objective"], "project_objective": self.config["objective"],
                    "resolution_condition": supervisor["resolution_condition"], "change_focus": change_focus,
                    "required_literals": unit["required_literals"], "claims": [c for c in self.config["claims"] if c["id"] in unit["claim_ids"]],
                    "read_only_document": self._document(self.baseline["artifact_ref"]),
                    "supplied_context": self.config["supplied_context"], "sources": sources,
                    "previous_proposal": self.proposals.get(unit_id, {}).get("text"), "verification_feedback": feedback}})
        results = self._model_jobs(jobs)
        failed, unknown = set(), False
        for job in jobs:
            task_id = job["task_id"]
            unit_id = job["assignment"]["unit_id"]
            outcome = results[task_id]
            if outcome["ok"]:
                try:
                    proposal = outcome["value"]
                    if set(proposal) != {"unit_id", "baseline_unit_ref", "text", "support"}:
                        raise ValidationError("producer must return exactly the assigned proposal fields")
                    required_strings(proposal, ("unit_id", "baseline_unit_ref", "text"))
                    if proposal["unit_id"] != unit_id or proposal["baseline_unit_ref"] != self.unit_records[unit_id]["artifact_ref"]:
                        raise ValidationError("producer returned a foreign unit or stale baseline")
                    validate_text(self.editable[unit_id]["kind"], proposal["text"])
                    if not preserves_literals(proposal["text"], self.editable[unit_id]["required_literals"]):
                        raise ValidationError("producer changed a preserved value in the assigned unit")
                    if not isinstance(proposal["support"], list):
                        raise ValidationError("producer must explicitly report source support")
                    by_ref = {source["ref"]: source for source in sources}
                    for support in proposal["support"]:
                        if not isinstance(support, dict) or set(support) != {"source_ref", "quote", "supports"}:
                            raise ValidationError("source support requires exact structured fields")
                        required_strings(support, ("source_ref", "quote", "supports"))
                        if (support["source_ref"] not in by_ref or len(support["quote"].split()) > 20
                            or support["quote"] not in by_ref[support["source_ref"]]["text"]):
                            raise ValidationError("source support does not match a captured excerpt")
                    old = self.proposals.get(unit_id)
                    if old:
                        self.tasks.transition(old["task_id"], "stale", "command.controller", reason="Superseded by targeted revision")
                    proposal = {**proposal, "author": job["actor"], "task_id": task_id,
                                "score_ref": self.score_ref,
                                "criteria_ref": self.criterion["artifact_ref"], "context_ref": self.context["artifact_ref"],
                                "claim_refs": [self.claims[key]["artifact_ref"] for key in self.editable[unit_id]["claim_ids"]],
                                "source_refs": list(by_ref), "execution_ref": outcome["record_ref"]}
                    record = self._publish(f"strategy/proposals/{unit_id}", "draft", proposal, job["actor"],
                        subjects=[outcome["record_ref"], proposal["baseline_unit_ref"], self.context["artifact_ref"],
                                  self.criterion["artifact_ref"], self.score_ref, *proposal["claim_refs"], *by_ref])
                    self.proposals[unit_id] = {**proposal, "proposal_ref": record["artifact_ref"], "proposal_sha256": record["body_hash"]}
                    self.information_changes.append({"kind": "unit_proposal", "ref": record["artifact_ref"]})
                except Exception as exc:
                    outcome.update(ok=False, error=f"{type(exc).__name__}: {exc}", outcome_known=True)
                    self._block_task(task_id, outcome["error"])
            if not outcome["ok"]:
                failed.add(unit_id)
                unknown |= not outcome.get("outcome_known", False)
                self.worker_errors.append({"task_id": task_id, "unit_id": unit_id, "error": outcome["error"],
                                           "outcome_known": outcome.get("outcome_known", False)})
        self._checkpoint("proposals_recorded", force=True)
        return failed, unknown

    def _governing_refs(self):
        return [self.score_ref, self.context["artifact_ref"], self.criterion["artifact_ref"],
                *[claim["artifact_ref"] for claim in self.claims.values()],
                *[source["ref"] for source in self.sources]]

    def _assert_governing_inputs(self):
        for ref in self._governing_refs():
            namespace, name, _ = parse_ref(ref)
            if self.store.head(f"{namespace}/{name}")["artifact_ref"] != ref:
                raise ValidationError("governing context, criteria, claims or source captures changed; fresh proposals and review required")
        for binding in self.capabilities.values():
            self.operations.authorize(binding)

    def _program_checks(self, number, candidate_ref):
        checks = []
        nodes = {parse_ref(node["ref"])[1].removeprefix("units/"): node["ref"]
                 for node in self.documents._walk_nodes(self.documents.get_tree(candidate_ref))}
        for specification in self.score["candidate_checks"]:
            unit_ref = nodes[specification["unit_id"]]
            text = self.documents.read_unit(unit_ref)["text"]
            arguments = {"input": {**deepcopy(specification["input"]), specification["text_field"]: text}}
            result, execution = self.operations.run(self.capabilities[specification["capability_id"]], arguments,
                self._call, operator="methods.program-verifier")
            value = result["document"]
            try:
                for key in specification["result_path"]:
                    value = value[key]
                passed = canonical_bytes(value) == canonical_bytes(specification["expected"])
                observed = value
            except (KeyError, IndexError, TypeError):
                passed, observed = False, {"missing_result_path": specification["result_path"]}
            check = {"check_id": f"program/{specification['id']}", "kind": "regression",
                "outcome": "passed" if passed else "failed", "unit_id": specification["unit_id"],
                "method": "Execute the declared program on the exact candidate unit and compare the required result",
                "result": json.dumps({"observed": observed, "expected": specification["expected"],
                                      "program_result": result["document"]}, ensure_ascii=False),
                "candidate_ref": candidate_ref, "unit_ref": unit_ref, "execution_ref": execution}
            record = self._publish(f"methods/program-checks/{number}/{specification['id']}", "evidence_record", check,
                                  "methods.program-verifier", subjects=[candidate_ref, unit_ref, execution, self.score_ref])
            checks.append({**check, "evidence_refs": [record["artifact_ref"], execution]})
        return checks

    def _stage(self, number):
        self._ensure_active()
        self._assert_governing_inputs()
        if set(self.proposals) != set(self.editable):
            raise ValidationError("cannot compose a document with missing unit proposals")
        if self.store.accepted(self.baseline["artifact_id"])["artifact_ref"] != self.baseline["artifact_ref"]:
            raise ValidationError("accepted baseline changed; proposals require fresh authority")
        for unit_id, proposal in self.proposals.items():
            if (proposal["baseline_unit_ref"] != self.unit_records[unit_id]["artifact_ref"]
                or proposal["criteria_ref"] != self.criterion["artifact_ref"] or proposal["context_ref"] != self.context["artifact_ref"]
                or proposal["score_ref"] != self.score_ref
                or proposal["claim_refs"] != [self.claims[key]["artifact_ref"] for key in self.editable[unit_id]["claim_ids"]]):
                raise ValidationError("proposal governing inputs or baseline changed")
        author = "strategy.integrator"
        request = self.changes.create_change_request(cr_id=f"project-{number}", author="command.controller",
            purpose=self.config["objective"], baseline_manifest_ref=self.baseline["artifact_ref"],
            scope={"units": [r["artifact_id"] for key, r in self.unit_records.items() if key in self.editable],
                   "source_refs": [source["ref"] for source in self.sources]},
            preservation=["exact structure, immutable units, per-unit literals, claim bindings and assembly dependencies"])
        grant_id = f"project-{number}"
        self.changes.issue_grant(grant_id=grant_id, request_ref=request["artifact_ref"],
            baseline_manifest_ref=self.baseline["artifact_ref"], actor=author,
            units={self.unit_records[key]["artifact_id"]: {"ops": ["replace_body"]} for key in self.editable},
            expires_in_seconds=max(0.001, self.deadline - time.monotonic()))
        staged = self.changes.stage_changeset(changeset_id=f"project-{number}", grant_id=grant_id, author=author,
            expected_accepted_manifest_version=self.baseline["version"], ops=[{
                "op": "replace_body", "unit": self.unit_records[key]["artifact_id"],
                "span": [0, len(self.editable[key]["text"])], "new_text": self.proposals[key]["text"],
                "expected_unit_version": self.unit_records[key]["version"],
                "expected_body_hash": self.unit_records[key]["body_hash"],
            } for key in self.editable])
        candidate = {"round": number, "candidate_ref": staged["manifest_ref"], "changeset_ref": staged["changeset_ref"],
                     "proposal_refs": {key: self.proposals[key]["proposal_ref"] for key in self.editable}, "adopted": False}
        self.candidates.append(candidate)
        response = self.issues.respond("project", response_id=f"project-{number}", author=author, stance="accept",
            candidate_ref=staged["manifest_ref"], supporting_refs=list(candidate["proposal_refs"].values()))
        self._checkpoint("candidate_staged", force=True)
        return candidate, response

    def _preservation(self, candidate_ref):
        baseline = self.documents.get_tree(self.baseline["artifact_ref"])
        candidate = self.documents.get_tree(candidate_ref)
        normalized = deepcopy(candidate)
        checks = []
        changed = {record["artifact_id"]: key for key, record in self.unit_records.items() if key in self.editable}
        seen = set()
        for node in self.documents._walk_nodes(normalized):
            ns, name, _ = parse_ref(node["ref"])
            logical = f"{ns}/{name}"
            if logical in changed:
                unit_id = changed[logical]
                content = self.documents.read_unit(node["ref"])
                prior = self.documents.read_unit(self.unit_records[unit_id]["artifact_ref"])
                valid = (content["text"] == self.proposals[unit_id]["text"]
                         and content["claim_refs"] == prior["claim_refs"] and content["citations"] == prior["citations"]
                         and preserves_literals(content["text"], self.editable[unit_id]["required_literals"]))
                checks.append({"check_id": f"preservation-{unit_id}", "kind": "regression",
                    "outcome": "passed" if valid else "failed", "method": "Compare exact proposal, per-unit literals, claims and citations",
                    "result": f"unit={unit_id}; preserved={valid}"})
                seen.add(unit_id)
                node["ref"] = self.unit_records[unit_id]["artifact_ref"]
        # Candidate identity is allowed to differ; assembly inputs and tree topology are not.
        for key in ("document_id",):
            normalized[key] = baseline[key]
        structure_ok = normalized == baseline and seen == set(self.editable)
        checks.append({"check_id": "mechanical-preservation", "kind": "regression",
            "outcome": "passed" if structure_ok else "failed", "method": "Compare normalized tree, immutable refs and assembly dependencies",
            "result": f"structure_and_immutable_units_preserved={structure_ok}"})
        return checks

    def _review(self, number, candidate, response, supervisor, producer_sources, verifier_sources, reservation):
        ref = candidate["candidate_ref"]
        self._admit_time("unit_review", task_count=len(self.editable))
        program_checks = self._program_checks(number, ref)
        document = self._document(ref)
        common = {"assignment": _REVIEW, "objective": self.config["objective"],
            "resolution_condition": supervisor["resolution_condition"], "supplied_context": self.config["supplied_context"],
            "candidate_ref": ref, "baseline_ref": self.baseline["artifact_ref"], "candidate_document": document,
            "baseline_document": self._document(self.baseline["artifact_ref"]), "claims": self.config["claims"],
            "producer_captures": producer_sources, "independently_fetched_sources": verifier_sources,
            "program_checks": program_checks, "domain": self.score["domain"], "score_ref": self.score_ref}
        jobs = [{"task_id": f"verify-{number}-{key}", "actor": f"methods.verifier-{key}", "task_kind": "verification",
                 "time_stage": "unit_review",
                 "assignment": {**common, "scope": "assigned unit in exact composed candidate", "unit_id": key,
                    "unit_contract": unit, "required_checks": self._required_checks(), "claimed_support": self.proposals[key]["support"]}}
                for key, unit in self.editable.items()]
        results = self._model_jobs(jobs)
        extra = {"document-consistency": "Inspect all output units together, including immutable context, for contradictory facts, "
                 "configuration values, terminology, conditions, evidence status or incompatible interpretations. Check executable outputs against their guide.",
                 **{f"unit-{key}": f"Confirm unit {key} preserves its full contract and claims in this exact deliverable, independently of prior verdicts."
                    for key in self.editable}}
        whole = {"task_id": f"integrated-review-{number}", "actor": "methods.document-verifier", "task_kind": "verification",
                 "time_stage": "integrated_review",
                 "reservation_id": reservation, "assignment": {**common, "scope": "whole composed document",
                    "required_checks": self._required_checks(extra), "unit_contracts": list(self.editable.values()),
                    "claimed_support": {key: proposal["support"] for key, proposal in self.proposals.items()}}}
        jobs.append(whole)
        self._admit_time("integrated_review")
        results.update(self._model_jobs([whole]))
        binding = {"issue_id": "project", "critique_ref": self.issues.get("project")["critique_ref"],
            "response_ref": response["artifact_ref"], "candidate_ref": ref, "baseline_ref": self.baseline["artifact_ref"],
            "resolution_condition": supervisor["resolution_condition"]}
        check_refs, regressions, uncertainties, observations, failures, feedback = [], [], [], [], set(), []
        global_failed, unknown = False, False
        for job in jobs:
            outcome = results[job["task_id"]]
            unit_id = job["assignment"].get("unit_id")
            required_kinds = {c["check_id"]: c["kind"] for c in job["assignment"]["required_checks"]}
            if outcome["ok"]:
                try:
                    validate_verdict(outcome["value"], required_check_kinds=required_kinds)
                except Exception as exc:
                    outcome.update(ok=False, error=f"{type(exc).__name__}: {exc}", outcome_known=True)
                    self._block_task(job["task_id"], outcome["error"])
            if not outcome["ok"]:
                verdict = {"checks": [{"check_id": "review-unavailable", "kind": "regression", "outcome": "check_failed",
                    "method": "Require completed structured independent review", "result": outcome["error"]}],
                    "regressions": [], "uncertainties": [outcome["error"]], "observations": [], "rationale": outcome["error"]}
                unknown |= not outcome.get("outcome_known", False)
            else:
                verdict = outcome["value"]
                self._complete(job["task_id"])
            passed = (outcome["ok"] and all(c["outcome"] == "passed" for c in verdict["checks"])
                      and not verdict["regressions"] and not verdict["uncertainties"])
            if not passed:
                if unit_id:
                    failures.add(unit_id)
                else:
                    global_failed = True
            prefix = f"unit/{unit_id}" if unit_id is not None else "document"
            for index, check in enumerate(verdict["checks"]):
                record = self._publish(f"methods/checks/{number}/{prefix}/{index}", "evidence_record",
                    {**check, **binding, "check_id": f"{prefix}:{check['check_id']}"}, job["actor"],
                    subjects=[ref, self.baseline["artifact_ref"], *([outcome["record_ref"]] if outcome.get("record_ref") else []),
                              *[source["ref"] for source in verifier_sources]])
                check_refs.append(record["artifact_ref"])
            regressions.extend(f"{prefix}: {x}" for x in verdict["regressions"])
            uncertainties.extend(f"{prefix}: {x}" for x in verdict["uncertainties"])
            observations.extend({"scope": prefix, **x} for x in verdict["observations"])
            feedback.append({"scope": prefix, "passed": passed, "verdict": verdict})
        for index, check in enumerate([*self._preservation(ref), *program_checks]):
            record = self._publish(f"methods/checks/{number}/deterministic/{index}", "evidence_record", {**check, **binding},
                                  "methods.mechanical-verifier", subjects=[ref, self.baseline["artifact_ref"], *check.get("evidence_refs", [])])
            check_refs.append(record["artifact_ref"])
            if check["outcome"] != "passed":
                if check.get("unit_id"):
                    failures.add(check["unit_id"])
                else:
                    global_failed = True
                feedback.append({"scope": check.get("unit_id", "document"), "passed": False, "check": check})
        verification = self.issues.verify("project", verification_id=f"project-{number}", verifier="methods.acceptance-verifier",
            response_ref=response["artifact_ref"], candidate_ref=ref, baseline_ref=self.baseline["artifact_ref"],
            resolution_condition=supervisor["resolution_condition"], check_refs=check_refs, regressions=regressions,
            uncertainties=uncertainties, rationale="Every assigned unit, configured program check and exact composed deliverable require passing checks")
        verified = json.loads(self.store.read_body(verification["body_hash"]))
        candidate.update(verification_ref=verification["artifact_ref"], resolved=verified["resolved"],
                         observations=observations, regressions=regressions, uncertainties=uncertainties)
        return verified["resolved"], failures, global_failed, unknown, feedback

    def _reassess(self, number, failures, global_failed, feedback):
        task_id = f"reassess-{number}"
        decision, record = self._timed_model("reassessment", task_id, "command.supervisor", {
            "assignment": "Decide whether a targeted revision can resolve the recorded failure. Return decision (revise or pause), "
                "rationale, targets. For pause use targets: []. For revise targets is a nonempty list of {unit_id,change_focus}; "
                "include every failed unit, select no immutable or unrelated unit, and identify a concrete change. "
                "Successful proposals should be reused unless the whole-deliverable failure gives a concrete reason to revise them. "
                "Do not repeat an unknown external operation or treat a contradictory governing condition as permission to rewrite facts.",
            "objective": self.config["objective"], "allowed_unit_ids": list(self.editable), "failed_units": sorted(failures),
            "whole_document_failed": global_failed, "feedback": feedback,
            "remaining_seconds": max(0, self.deadline - time.monotonic()),
        }, task_kind="service")
        if set(decision) != {"decision", "rationale", "targets"}:
            raise ValidationError("supervisor reassessment requires exactly decision, rationale and targets")
        required_strings(decision, ("decision", "rationale"))
        if decision["decision"] not in {"revise", "pause"}:
            raise ValidationError("unsupported supervision decision")
        if decision["decision"] == "pause":
            if decision["targets"] != []:
                raise ValidationError("paused supervision cannot dispatch revision targets")
            focus = {}
        else:
            focus = self._targets(decision["targets"], required=failures)
            if not global_failed and set(focus) - failures:
                raise ValidationError("supervisor tried to revise a successful unrelated unit")
        self._publish(f"command/supervision/reassess-{number}", "supervision_decision", decision,
                      "command.supervisor", subjects=[record])
        self._complete(task_id)
        return focus, decision["rationale"]

    def _release_unstarted_reservations(self):
        for row in self.control._conn.execute("SELECT r.reservation_id FROM reservations r WHERE r.state='reserved' "
            "AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.task_id=r.task_id)").fetchall():
            self.budget.settle(window_id="run-window", reservation_id=row[0], actual={})

    def _run(self):
        status, error = "blocked", None
        try:
            self._initialize()
            if self.time_policy and not self.time_policy.snapshot()["initial_hard_limit_feasible"]:
                raise ValidationError("configured stage estimates do not fit the hard deadline; no external work dispatched")
            self._admit_time("setup")
            setup_started = time.monotonic()
            self._setup_operations()
            producer_sources = self._retrieve("research.researcher", "producer")
            verifier_sources = self._retrieve("methods.researcher", "independent")
            if self.time_policy:
                self.time_policy.observe("setup", time.monotonic() - setup_started)
            supervisor, focus = self._supervise()
            feedback = []
            for number in range(1, self.config["limits"]["max_rounds"] + 1):
                self._admit_time("production", task_count=len(focus), pending_review_count=len(self.editable) - len(focus))
                reservation = f"reserved-review-{number}"
                self.budget.reserve(window_id="run-window", reservation_id=reservation,
                                    task_id=f"integrated-review-{number}", amount={"concurrent_calls": 1})
                failed, unknown = self._propose(number, focus, supervisor, producer_sources, feedback)
                self._ensure_active()
                global_failed, accepted = False, False
                if not failed:
                    candidate, response = self._stage(number)
                    accepted, failed, global_failed, review_unknown, feedback = self._review(
                        number, candidate, response, supervisor, producer_sources, verifier_sources, reservation)
                    unknown |= review_unknown
                    self._ensure_active()
                    if accepted:
                        self._assert_governing_inputs()
                        adopted = self.changes.accept_changeset(changeset_ref=candidate["changeset_ref"],
                            verification_ref=candidate["verification_ref"], author="strategy.integrator",
                            expected_accepted_manifest_version=self.baseline["version"],
                            expected_head_refs=self._governing_refs(), acceptance_guard=self._ensure_active)
                        self.incumbent = adopted["manifest_ref"]
                        if self.time_policy:
                            self.time_policy.mark_first_verified_result(self.incumbent)
                        candidate["adopted"] = True
                        for proposal in self.proposals.values():
                            self._complete(proposal["task_id"])
                        self.verified_changes.append({"kind": "composed_document_revision", "ref": self.incumbent,
                                                      "verification_ref": candidate["verification_ref"]})
                else:
                    feedback = [item for item in self.worker_errors if item["task_id"].startswith(f"write-{number}-")]
                self._release_unstarted_reservations()
                self.rounds.append({"round": number, "requested_units": list(focus), "failed_units": sorted(failed),
                                    "global_failed": global_failed, "adopted": accepted})
                if accepted:
                    status = "accepted"
                    break
                if unknown:
                    raise ValidationError("external outcome unknown; retained reservations require explicit reconciliation")
                self.budget.mark_unproductive(cause_key="project-objective", window_id="run-window")
                self._checkpoint("targeted_revision_required", force=True)
                if number == self.config["limits"]["max_rounds"]:
                    status = "unresolved"
                    self.blockers.append({"reason": "revision round limit reached", "failed_units": sorted(failed)})
                    break
                focus, reason = self._reassess(number, failed, global_failed, feedback)
                if not focus:
                    status = "paused"
                    self.blockers.append({"reason": reason})
                    break
        except (Exception, KeyboardInterrupt) as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.blockers.append({"reason": error})
        finally:
            self._release_unstarted_reservations()
            if hasattr(self, "baseline"):
                head = self.store.accepted(self.baseline["artifact_id"])
                self.incumbent = head["artifact_ref"] if head else None
            for row in self.control._conn.execute("SELECT task_id FROM tasks WHERE state='awaiting_review'").fetchall():
                self.tasks.transition(row[0], "blocked", "command.controller", reason="Run ended without accepted output for this task")
            for candidate in self.candidates:
                candidate["adopted"] = candidate["candidate_ref"] == self.incumbent
            self._checkpoint(status, force=True)
        result = {"run_id": self.run_id, "project_id": self.config["project_id"], "status": status, "error": error,
            "baseline_ref": getattr(self, "baseline", {}).get("artifact_ref"), "incumbent_ref": self.incumbent,
            "candidates": self.candidates, "proposals": self.proposals, "rounds": self.rounds, "worker_errors": self.worker_errors,
            "source_captures": [{key: value for key, value in source.items() if key != "text"} for source in self.sources],
            "score_ref": self.score_ref,
            "capabilities": {key: self.operations.status(key) for key in self.registered_capabilities},
            "time_plan": self.time_policy.snapshot() if self.time_policy else None, "time_decisions": self.time_decisions,
            "usage": self.budget.get_window("run-window"), "unreported_usage": self.usage_gaps,
            "blockers": self.blockers, "event_chain": self.control.verify_chain(), "release_status": "not_released"}
        self._publish("command/results/final", "report", result, "command.controller")
        self._export(result)
        self.control.close()
        return result

    def _export(self, result):
        output = self.dir / "output"
        output.mkdir(exist_ok=True)
        (output / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        lines = ["# Project integration run", "", f"Status: **{result['status']}**", "",
                 "Release status: not_released.", "", f"Current manifest: `{self.incumbent}`", "",
                 f"Score: `{self.score_ref}`", "", "## Resource accounting", "",
                 json.dumps(result["usage"]["cumulative_usage"], sort_keys=True), "", "## Unit proposals", ""]
        for key, proposal in self.proposals.items():
            lines.extend([f"### {key}", "", proposal["text"], "",
                          f"Author: `{proposal['author']}`. Proposal: `{proposal['proposal_ref']}`.", ""])
        for candidate in self.candidates:
            lines.extend([f"## Candidate {candidate['round']}", "",
                          f"Manifest: `{candidate['candidate_ref']}`. Adopted: {candidate['adopted']}.", ""])
            for note in candidate.get("observations", []):
                lines.extend([f"- {note['scope']}: {note['observation']} Basis: {note['reason_nonblocking']}", ""])
        lines.extend(["## Operations", ""])
        for key, state in result["capabilities"].items():
            lines.append(f"- {key}: {state['state']}; verification `{state['verification_ref']}`")
        lines.extend(["", "## Captured evidence", ""])
        for source in self.sources:
            label = f"[{source['url']}]({source['url']})" if source.get("url") else source.get("capability_id", "Captured input")
            lines.append(f"- {label}: `{source['ref']}`")
        if result.get("time_plan"):
            lines.extend(["", "## Time contract", "", "```json", json.dumps(result["time_plan"], indent=2), "```", ""])
        if self.blockers:
            lines.extend(["", "## Unresolved items", ""])
            lines.extend(f"- {item['reason']}" for item in self.blockers)
        (output / "report.md").write_text("\n".join(lines) + "\n")
        if self.incumbent:
            document = self._document(self.incumbent)
            assembled = [f"# {document['title']}", ""]
            for group in document["groups"]:
                assembled.extend([f"## {group['title']}", ""])
                for unit in group["units"]:
                    kind, text = unit["kind"], unit["text"]
                    if kind in {"json", "code"}:
                        fence = "`" * max(3, 1 + max((len(part) for part in re.findall(r"`+", text)), default=0))
                        assembled.extend([fence + ("json" if kind == "json" else "python"), text, fence, ""])
                    else:
                        assembled.extend([("- " if kind == "list_item" else "") + text, ""])
                    path = self.units[unit["unit_id"]]["output_file"]
                    if path:
                        target = output / path
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(text, encoding="utf-8", newline="")
            target = output / self.layout["output_file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(assembled) + "\n")
