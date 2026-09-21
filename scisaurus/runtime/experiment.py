"""Execute a frozen study twice, recalculate it independently, and review its claims."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import threading
import time
import uuid

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.core.store import ArtifactStore
from scisaurus.core.surveys import SurveyGate
from scisaurus.runtime.execution import ExecutionRuntime, _invoke_worker
from scisaurus.runtime.capability_registry import experiment_program_payload
from scisaurus.runtime.config import configured_worker_slots
from scisaurus.runtime.experiment_config import ASSET_MEDIA_TYPES, validate_experiment_config
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.operations import OperationsCell
from scisaurus.runtime.results import validate_results_package
from scisaurus.runtime.research_quality import (
    build_research_design,
    check_analysis_contract,
    validate_analysis,
)
from scisaurus.runtime.scores import exact, identifier, output_path
from scisaurus.runtime.time_policy import TimePolicy


REVIEW_CHECKS = {"method_alignment", "calculation_trace", "inference_scope", "limitation_coverage"}
REVIEW_DECISIONS = {"accepted", "accepted_with_limitations", "rejected"}


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _finite_scalar(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValidationError(f"{name} must be an exact JSON scalar")
    canonical_bytes(value)
    return value


def validate_program_output(value, experiment):
    output_fields = {"schema_version", "study_id", "revision", "procedures", "observations",
                     "metrics", "findings", "limitations", "assets"}
    if (not isinstance(value, dict) or set(value) - (output_fields | {"analysis"})
            or not output_fields.issubset(value)):
        raise ValidationError(
            f"experiment program output requires {sorted(output_fields)} and permits analysis")
    if value["schema_version"] != "experiment-program-output-1":
        raise ValidationError("unsupported experiment program output schema")
    if value["study_id"] != experiment["id"] or value["revision"] != experiment["revision"]:
        raise ValidationError("experiment program output does not match the frozen study identity")
    observations = value["observations"]
    if (not isinstance(observations, list) or len(observations) < experiment["run_count"]
            or len(observations) > experiment["max_observations"]):
        raise ValidationError("experiment observations violate the configured count bounds")
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValidationError("every experiment observation must be an object")
        canonical_bytes(observation)

    assets = value["assets"]
    if not isinstance(assets, list):
        raise ValidationError("experiment assets must be an explicit list")
    roles = {}
    for asset in assets:
        exact(asset, {"id", "path", "sha256", "role", "media_type", "caption"},
              "experiment asset")
        identifier(asset["id"])
        output_path(asset["path"])
        identifier(asset["role"])
        if asset["media_type"] not in ASSET_MEDIA_TYPES:
            raise ValidationError("experiment asset media type is unsupported")
        if asset["caption"] is not None:
            _text(asset["caption"], "experiment asset caption")
        if asset["role"] == "figure" and (
                asset["media_type"] not in {"image/png", "image/jpeg", "application/pdf"}
                or asset["caption"] is None):
            raise ValidationError("experiment figures require a renderable type and caption")
        roles.setdefault(asset["role"], []).append(asset)

    core = {"schema_version": "results-package-1", "id": value["study_id"], "revision": value["revision"],
            "procedures": value["procedures"], "metrics": value["metrics"], "findings": value["findings"],
            "limitations": value["limitations"], "assets": [
                {key: asset[key] for key in ("path", "sha256", "role")} for asset in assets]}
    validate_results_package(core)
    configured = {item["id"]: item for item in experiment["primary_outcomes"]}
    observed = {item["id"]: item for item in value["metrics"]}
    if set(configured) - set(observed):
        raise ValidationError("experiment output omits a configured primary outcome")
    if any(observed[key]["unit"] != contract["unit"] for key, contract in configured.items()):
        raise ValidationError("experiment output changes a configured primary outcome unit")
    if not set(experiment["limitations"]).issubset(value["limitations"]):
        raise ValidationError("experiment output omits a frozen design limitation")

    for requirement in experiment["required_assets"]:
        matches = [asset for asset in roles.get(requirement["role"], [])
                   if asset["media_type"] in requirement["media_types"]]
        if len(matches) < requirement["min_count"]:
            raise ValidationError("experiment output omits a required asset")
    if "analysis" in value:
        validate_analysis(value["analysis"])
    quality_contract = experiment.get("quality_contract")
    if quality_contract is not None:
        analysis = value.get("analysis")
        if analysis is None:
            raise ValidationError("experiment quality contract requires an analysis summary")
        deficits = check_analysis_contract(
            analysis, quality_contract,
            figure_count=sum(1 for asset in value["assets"] if asset.get("role") == "figure"))
        if deficits:
            fields = ", ".join(item["field"] for item in deficits)
            raise ValidationError(f"experiment quality contract is not satisfied: {fields}")
    canonical_bytes(value)
    return value


def validate_deterministic_validation(value, experiment, candidate_sha256):
    exact(value, {"schema_version", "study_id", "candidate_sha256", "decision", "checks",
                  "metric_recalculations", "limitations"}, "deterministic experiment validation")
    if value["schema_version"] != "experiment-validation-1" or value["study_id"] != experiment["id"]:
        raise ValidationError("deterministic validation does not match the experiment")
    if value["candidate_sha256"] != candidate_sha256:
        raise ValidationError("deterministic validation does not bind the exact program output")
    if value["decision"] not in {"accepted", "rejected"}:
        raise ValidationError("deterministic validation decision is invalid")
    if not isinstance(value["checks"], list) or not value["checks"]:
        raise ValidationError("deterministic validation requires checks")
    check_ids = set()
    for check in value["checks"]:
        exact(check, {"id", "outcome", "evidence"}, "deterministic validation check")
        identifier(check["id"])
        if check["id"] in check_ids or check["outcome"] not in {"passed", "failed"}:
            raise ValidationError("deterministic validation check is duplicated or invalid")
        check_ids.add(check["id"])
        _text(check["evidence"], "deterministic validation evidence")
    recalculations = value["metric_recalculations"]
    if not isinstance(recalculations, list) or not recalculations:
        raise ValidationError("deterministic validation requires metric recalculations; "
            "validator checks=" + json.dumps(value["checks"][:8], ensure_ascii=False)[:2400] +
            "; each primary_outcomes entry already has its final exact metric ID; "
            "do not append a condition suffix again")
    metric_ids = set()
    for metric in recalculations:
        exact(metric, {"metric_id", "reported_value", "recalculated_value", "tolerance", "matches"},
              "metric recalculation")
        identifier(metric["metric_id"])
        if metric["metric_id"] in metric_ids or type(metric["matches"]) is not bool:
            raise ValidationError("metric recalculation is duplicated or invalid")
        metric_ids.add(metric["metric_id"])
        reported = metric["reported_value"]
        recalculated = metric["recalculated_value"]
        if (type(reported) not in (int, float) or not math.isfinite(reported)
                or type(recalculated) not in (int, float) or not math.isfinite(recalculated)):
            raise ValidationError("metric recalculation values must be finite numbers")
        if (type(metric["tolerance"]) not in (int, float)
                or not math.isfinite(metric["tolerance"]) or metric["tolerance"] < 0):
            raise ValidationError("metric recalculation tolerance is invalid")
        observed_match = abs(float(reported) - float(recalculated)) <= float(metric["tolerance"])
        if metric["matches"] is not observed_match:
            raise ValidationError("metric recalculation match flag contradicts its values")
    configured_metric_ids = {item["id"] for item in experiment["primary_outcomes"]}
    if metric_ids != configured_metric_ids:
        raise ValidationError(
            "deterministic validation must recalculate exactly the primary outcomes")
    if not isinstance(value["limitations"], list):
        raise ValidationError("deterministic validation limitations must be a list")
    for limitation in value["limitations"]:
        _text(limitation, "deterministic validation limitation")
    passed = (all(check["outcome"] == "passed" for check in value["checks"])
              and all(item["matches"] for item in recalculations))
    if value["decision"] != ("accepted" if passed else "rejected"):
        raise ValidationError("deterministic validation decision contradicts its checks")
    return value


def bind_deterministic_validation(value, candidate, experiment):
    """Bind validator metric claims to the exact candidate metric values."""
    declared = {item["id"] for item in experiment["primary_outcomes"]}
    reported = {item["id"]: item["value"] for item in candidate["metrics"]}
    recalculations = value["metric_recalculations"]
    if {item["metric_id"] for item in recalculations} != declared:
        raise ValidationError(
            "deterministic validation must bind exactly the declared primary outcomes")
    for item in recalculations:
        metric_id = item["metric_id"]
        if metric_id not in reported \
                or canonical_bytes(item["reported_value"]) != canonical_bytes(reported[metric_id]):
            raise ValidationError(
                "deterministic validation changed a reported primary metric")
    return value


def validate_model_review(value, reviewer_id, finding_ids):
    exact(value, {"reviewer_id", "decision", "checks", "finding_assessments", "limitations"},
          "experiment model review")
    if value["reviewer_id"] != reviewer_id or value["decision"] not in REVIEW_DECISIONS:
        raise ValidationError("experiment review identity or decision is invalid")
    if not isinstance(value["checks"], list) or len(value["checks"]) != len(REVIEW_CHECKS):
        raise ValidationError("experiment review must execute every required check")
    seen = set()
    for check in value["checks"]:
        exact(check, {"check_id", "outcome", "evidence"}, "experiment review check")
        if check["check_id"] in seen or check["check_id"] not in REVIEW_CHECKS:
            raise ValidationError("experiment review check is unknown or duplicated")
        seen.add(check["check_id"])
        if check["outcome"] not in {"passed", "failed", "insufficient_evidence"}:
            raise ValidationError("experiment review check outcome is invalid")
        _text(check["evidence"], "experiment review check evidence")
    assessments = value["finding_assessments"]
    if not isinstance(assessments, list) or {item.get("finding_id") for item in assessments} != finding_ids:
        raise ValidationError("experiment review must assess every finding exactly once")
    if len(assessments) != len(finding_ids):
        raise ValidationError("experiment finding assessments cannot contain duplicates")
    for item in assessments:
        exact(item, {"finding_id", "outcome", "rationale"}, "finding assessment")
        if item["outcome"] not in {"supported", "overstated", "insufficient_evidence"}:
            raise ValidationError("finding assessment outcome is invalid")
        _text(item["rationale"], "finding assessment rationale")
    if not isinstance(value["limitations"], list):
        raise ValidationError("experiment review limitations must be a list")
    for limitation in value["limitations"]:
        _text(limitation, "experiment review limitation")
    if value["decision"] != "rejected" and (
            any(check["outcome"] != "passed" for check in value["checks"])
            or any(item["outcome"] != "supported" for item in assessments)):
        raise ValidationError("an accepted experiment review cannot retain a failed check or unsupported finding")
    return value


def validate_assessment(value, study_id, evidence_refs, review_outcomes, finding_ids, expected_limitations):
    exact(value, {"schema_version", "study_id", "decision", "summary", "evidence_refs",
                  "reviewer_outcomes", "accepted_findings", "limitations"}, "experiment assessment")
    if value["schema_version"] != "experiment-assessment-1" or value["study_id"] != study_id:
        raise ValidationError("experiment assessment does not match the study")
    if value["decision"] not in REVIEW_DECISIONS:
        raise ValidationError("experiment assessment decision is invalid")
    _text(value["summary"], "experiment assessment summary")
    if value["evidence_refs"] != evidence_refs or value["reviewer_outcomes"] != review_outcomes:
        raise ValidationError("experiment assessment must copy exact evidence and reviewer outcomes")
    if (not isinstance(value["accepted_findings"], list)
            or len(value["accepted_findings"]) != len(set(value["accepted_findings"]))
            or set(value["accepted_findings"]) - finding_ids):
        raise ValidationError("experiment assessment accepted_findings are invalid")
    if value["limitations"] != expected_limitations:
        raise ValidationError("experiment assessment must preserve the exact program limitations")
    for limitation in value["limitations"]:
        _text(limitation, "experiment assessment limitation")
    if value["decision"] != "rejected" and (
            set(value["accepted_findings"]) != finding_ids
            or any(item["decision"] == "rejected" for item in review_outcomes)):
        raise ValidationError("accepted assessment must retain every independently supported finding")
    return value


class ExperimentRunner(ExecutionRuntime):
    def __init__(self, project_dir, config, *, on_progress=None):
        config = validate_experiment_config(config)
        super().__init__(project_dir, config, worker_target=_invoke_worker, on_progress=on_progress)
        self.experiment = config["experiment"]
        self.operations = OperationsCell(self.control, self.store, project_id=config["project_id"])
        self.bindings = {}
        self.profile_refs = {}
        self.design_ref = None
        self.execution_refs = []
        self.asset_records = []
        self.asset_files = []
        self.review_records = []
        self.serial = 0
        self.literature = {"survey_ref": None, "assessment_ref": None, "state": None}
        self.literature_gate_mismatch = None
        self.research_expansion_requests = []
        self.worker_slots = configured_worker_slots(self.config["limits"])
        self.time_policy = TimePolicy(stage_seconds=self.experiment["stage_seconds"],
            unit_count=len(self.experiment["reviewers"]),
            worker_slots=self.worker_slots,
            wall_clock_seconds=self.config["limits"]["wall_clock_seconds"],
            policy=self.config.get("time_policy"))
        self.time_policy.started_at = self.started
        self.deadline = min(self.deadline, self.started + self.time_policy.hard_seconds)

    def _initialize(self):
        record = self._publish(f"command/scores/{self.experiment['id']}", "note", {
            "schema_version": "experiment-score-1", "experiment": self.experiment,
            "time_policy": self.config.get("time_policy")}, "principal")
        self.score_ref = record["artifact_ref"]
        design = build_research_design(self.experiment)
        design_record = self._publish(
            "inputs/research-design", "note", design, "principal", subjects=[self.score_ref])
        self.design_ref = design_record["artifact_ref"]
        gate = self.experiment["literature_gate"]
        if gate is not None:
            control = ControlStore(gate["project_dir"])
            try:
                store = ArtifactStore(control)
                survey_gate = SurveyGate(control, store)
                survey_gate.require_current(gate["survey_ref"])
                assessment = survey_gate.require_current_assessment(gate["assessment_ref"])
                body = json.loads(store.read_body(assessment["body_hash"]))
                self.literature = {"survey_ref": gate["survey_ref"],
                                   "assessment_ref": gate["assessment_ref"], "state": body["state"]}
                if body["state"] != gate["required_state"]:
                    self.literature_gate_mismatch = {
                        "observed_state": body["state"],
                        "required_state": gate["required_state"],
                        "survey_ref": gate["survey_ref"],
                        "assessment_ref": gate["assessment_ref"],
                    }
                    self.research_expansion_requests = [{
                        "id": f"literature-gate-{body['state']}",
                        "kind": "literature_expansion",
                        "owner": "research.intelligence",
                        "objective": "Expand the literature search and secure enough verified full-text evidence to resolve the experiment admission state.",
                        "why": f"The current literature assessment is {body['state']}, while this experiment requires {gate['required_state']}.",
                        "success_condition": f"A current literature assessment reports {gate['required_state']} with its source and identity checks satisfied.",
                        "evidence_needed": "Additional scoped searches, verified source identities, and decisive full-text quotations bound to the accepted survey.",
                    }]
            finally:
                control.close()
        self.context = self._publish("inputs/experiment-context", "note", {
            "objective": self.config["objective"], "supplied_context": self.config["supplied_context"],
            "literature": self.literature}, "principal", subjects=[self.score_ref])
        self._checkpoint("experiment_frozen", force=True)

    def _setup(self):
        for name in ("execution", "validation"):
            capability = self.experiment[name]
            client = deepcopy(capability["client"])
            command = list(client["command"])
            command[0] = str(Path(command[0]).absolute()) if "/" in command[0] else (shutil.which(command[0]) or command[0])
            workspace = self.operations.workspace_dir(capability["id"])
            environment_files = list(capability["environment_files"])
            if client.get("sandbox_required"):
                if len(command) != 2:
                    raise ValidationError(
                        "generated experiment programs require one pinned source argument")
                source = Path(command[1])
                if (not source.is_absolute() or not source.is_file()
                        or str(source) not in environment_files):
                    raise ValidationError(
                        "generated experiment source is not pinned by its capability descriptor")
                local_source = workspace / f"{name}.py"
                temporary = workspace / f".{name}.{uuid.uuid4().hex}.tmp"
                try:
                    temporary.write_bytes(source.read_bytes())
                    os.replace(temporary, local_source)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                command[1] = str(local_source)
                environment_files.append(str(local_source))
            client.update(command=command, cwd=str(workspace),
                          own_process_group=False)
            state = self.operations.register(capability["id"], adapter="local_program", client=client,
                representative=capability["representative"], engineer=f"operations.engineer.{name}",
                environment_files=environment_files)
            state = self.operations.ensure_ready(capability["id"], self._call,
                operator=f"operations.operator.{name}", verifier=f"operations.verifier.{name}",
                purpose=f"Prepare the frozen experiment {name} program")
            if state["state"] != "ready":
                raise ValidationError(f"experiment {name} capability is unavailable: {state['reason']}")
            self.bindings[name] = state["binding"]
            self.profile_refs[name] = state["profile_ref"]
            self.operations.idle(capability["id"])
            self.information_changes.append({"kind": f"verified_{name}_capability",
                                             "ref": state["verification_ref"]})
        if self.bindings["execution"]["identity_sha256"] == self.bindings["validation"]["identity_sha256"]:
            raise ValidationError("experiment execution and validation must use distinct pinned program identities")
        self._checkpoint("experiment_capabilities_ready", force=True)

    def _program_input(self):
        return experiment_program_payload(self.experiment, self.experiment["execution"]["input"])

    def _execute_once(self):
        decision = self.time_policy.admit("production", task_count=1)
        if not decision["allowed"]:
            raise ValidationError(f"time admission deferred experiment execution: {decision['reason']}")
        started = time.monotonic()
        result, ref = self.operations.run(self.bindings["execution"], {"input": self._program_input()}, self._call,
            operator="methods.experiment-operator")
        self.time_policy.observe("production", time.monotonic() - started)
        candidate = validate_program_output(result["document"], self.experiment)
        self.execution_refs.append(ref)
        return candidate

    def _workspace_assets(self, candidate):
        workspace = self.operations.workspace_dir(self.experiment["execution"]["id"]).resolve()
        total, captured = 0, []
        for asset in candidate["assets"]:
            path = workspace / asset["path"]
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_file() or not resolved.is_relative_to(workspace) or path.is_symlink():
                    raise OSError("asset is outside the experiment workspace")
                body = resolved.read_bytes()
            except OSError as exc:
                raise ValidationError(f"experiment asset is unavailable: {asset['id']}") from exc
            total += len(body)
            if total > self.experiment["max_asset_bytes"]:
                raise ValidationError("experiment assets exceed the configured byte limit")
            if sha256_hex(body) != asset["sha256"]:
                raise ValidationError("experiment asset does not match its declared SHA-256")
            if asset["media_type"] == "image/png" and not body.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValidationError("experiment asset media type does not match PNG bytes")
            if asset["media_type"] == "image/jpeg" and not body.startswith(b"\xff\xd8\xff"):
                raise ValidationError("experiment asset media type does not match JPEG bytes")
            captured.append((asset, body))
        return captured

    def _capture_assets(self, candidate):
        for asset, body in self._workspace_assets(candidate):
            record = self.store.publish_artifact(logical_id=f"methods/experiment-assets/{asset['id']}",
                artifact_type="source_capture", author="methods.experiment-operator", body=body,
                media_type=asset["media_type"], inputs=[{"ref": self.execution_refs[0], "purpose": "subject"}],
                score_ref=self.score_ref)
            self.asset_records.append(record)
            self.asset_files.append({"descriptor": asset, "body": body, "record": record})

    def _deterministic_validate(self, candidate, candidate_sha256):
        decision = self.time_policy.admit("unit_review", task_count=1)
        if not decision["allowed"]:
            raise ValidationError(f"time admission deferred deterministic validation: {decision['reason']}")
        payload = {"configured_input": self.experiment["validation"]["input"], "candidate": candidate,
                   "candidate_sha256": candidate_sha256,
                   "primary_outcomes": self.experiment["primary_outcomes"]}
        started = time.monotonic()
        result, execution_ref = self.operations.run(self.bindings["validation"], {"input": payload}, self._call,
            operator="methods.independent-calculator")
        self.time_policy.observe("unit_review", time.monotonic() - started)
        value = validate_deterministic_validation(result["document"], self.experiment, candidate_sha256)
        bind_deterministic_validation(value, candidate, self.experiment)
        record = self._publish("methods/experiment-deterministic-validation", "verification", {
            **value, "execution_ref": execution_ref}, "methods.independent-calculator",
            subjects=[*self.execution_refs, execution_ref, *[item["artifact_ref"] for item in self.asset_records]])
        if value["decision"] != "accepted":
            raise ValidationError("independent metric recalculation rejected the experiment output")
        return value, record, execution_ref

    def _review_assignment(self, reviewer, candidate, deterministic, evidence_refs):
        summary = {key: candidate[key] for key in (
            "schema_version", "study_id", "revision", "procedures", "metrics", "findings", "limitations", "assets")}
        if "analysis" in candidate:
            summary["analysis"] = candidate["analysis"]
        return {"phase": "experiment_result_review", "reviewer": reviewer,
            "study": {key: self.experiment[key] for key in (
                "id", "study_type", "domain", "research_question", "hypothesis", "method", "parameters",
                "seed", "run_count", "stopping_rule", "primary_outcomes", "limitations")},
            "program_output_summary": summary, "deterministic_validation": deterministic,
            "evidence_refs": evidence_refs, "required_checks": sorted(REVIEW_CHECKS),
            "instructions": (
                "Inspect the summarized output and every attached figure. Return exactly reviewer_id, decision, checks, finding_assessments, limitations. "
                "Copy reviewer.id as reviewer_id. Execute each required check exactly once as {check_id,outcome,evidence}; outcomes are passed, failed, or insufficient_evidence. "
                "Assess each finding exactly once as {finding_id,outcome,rationale}; outcomes are supported, overstated, or insufficient_evidence. "
                "Decision is accepted, accepted_with_limitations, or rejected. An accepted decision requires all checks passed and all findings supported. "
                "Treat program numbers as observations only after the independent recalculation passes. Check method-contract alignment, calculation trace, inference scope, and limitation coverage. "
                "Do not infer general scientific truth, novelty, or external validity from one finite computational study. Preserve negative and mixed results." )}

    def _model_checked(self, jobs, *, images, stage):
        pending, accepted, feedback = list(jobs), {}, {}
        repair_mode = self.config["limits"].get("repair_mode", "bounded")
        rounds = (itertools.count() if repair_mode == "until_deadline"
                  else range(self.config["limits"]["max_rounds"]))
        for _ in rounds:
            self._ensure_active()
            admission = self.time_policy.admit(stage, task_count=len(pending))
            if not admission["allowed"]:
                raise ValidationError(f"time admission deferred experiment review: {admission['reason']}")
            specs = []
            for job in pending:
                self.serial += 1
                assignment = deepcopy(job["assignment"])
                if job["name"] in feedback:
                    assignment["validation_feedback"] = feedback[job["name"]]
                specs.append({"task_id": f"experiment-{job['name']}-{self.serial}", "kind": "model",
                              "actor": job["actor"], "task_kind": "verification",
                              "params": {"client": self.config["model"],
                                         "prompt": json.dumps(assignment, ensure_ascii=False),
                                         **({"images": images} if images else {})}})
            outcomes = self._call_batch(specs, max_parallel=self.worker_slots)
            rejected = []
            for job, spec in zip(pending, specs):
                outcome = outcomes[spec["task_id"]]
                if not outcome["ok"]:
                    raise ValidationError(f"experiment model dispatch failed: {job['name']}: {outcome['error']}")
                result = ModelResult(**outcome["result"])
                self.time_policy.observe(stage, result.elapsed_seconds)
                try:
                    if result.finish_reason != "stop":
                        raise ValidationError("experiment model generation did not finish normally")
                    value = result.json_object()
                    job["validator"](value)
                except (ValidationError, TypeError, ValueError, KeyError) as exc:
                    feedback[job["name"]] = {"error": str(exc),
                        "scope": "Repair only the response schema or unsupported acceptance. Return the complete requested JSON object."}
                    self.tasks.transition(spec["task_id"], "blocked", "command.controller", reason=str(exc))
                    rejected.append(job)
                    continue
                self._complete(spec["task_id"])
                accepted[job["name"]] = (value, outcome["record_ref"])
            if not rejected:
                return accepted
            pending = rejected
        raise ValidationError("experiment model review did not satisfy its contract")

    def _model_reviews(self, candidate, deterministic, deterministic_record):
        finding_ids = {item["id"] for item in candidate["findings"]}
        image_records = [item for item in self.asset_files if item["descriptor"]["media_type"] in {"image/png", "image/jpeg"}]
        images = [{"path": str(Path(self.store.objects_dir) / item["record"]["body_hash"]),
                   "media_type": item["descriptor"]["media_type"], "sha256": item["record"]["body_hash"]}
                  for item in image_records[:16]]
        evidence_refs = [*self.execution_refs, deterministic_record["artifact_ref"],
                         *[item["artifact_ref"] for item in self.asset_records]]
        jobs = [{"name": reviewer["id"], "actor": f"methods.experiment-reviewer.{reviewer['id']}",
                 "assignment": self._review_assignment(reviewer, candidate, deterministic, evidence_refs),
                 "validator": lambda value, rid=reviewer["id"]: validate_model_review(value, rid, finding_ids)}
                for reviewer in self.experiment["reviewers"]]
        values = self._model_checked(jobs, images=images, stage="integrated_review")
        reviews = []
        for reviewer, job in zip(self.experiment["reviewers"], jobs):
            value, execution_ref = values[job["name"]]
            record = self._publish(f"methods/experiment-reviews/{reviewer['id']}", "verification", {
                **value, "execution_ref": execution_ref}, job["actor"],
                subjects=[execution_ref, *evidence_refs])
            self.review_records.append(record)
            reviews.append(value)
        return reviews, images

    def _assess(self, candidate, deterministic_record, reviews, images):
        review_refs = [record["artifact_ref"] for record in self.review_records]
        evidence_refs = [*self.execution_refs, deterministic_record["artifact_ref"], *review_refs,
                         *[item["artifact_ref"] for item in self.asset_records]]
        outcomes = [{"reviewer_id": value["reviewer_id"], "decision": value["decision"]} for value in reviews]
        finding_ids = {item["id"] for item in candidate["findings"]}
        assignment = {"phase": "experiment_result_assessment", "study_id": self.experiment["id"],
            "question": self.experiment["research_question"], "hypothesis": self.experiment["hypothesis"],
            "metrics": candidate["metrics"], "findings": candidate["findings"],
            "design_limitations": self.experiment["limitations"], "program_limitations": candidate["limitations"],
            "independent_reviews": reviews, "evidence_refs_exact": evidence_refs,
            "reviewer_outcomes_exact": outcomes,
            "instructions": (
                "Reconcile the exact reviews without changing measured values. Return an experiment-assessment-1 object with exactly schema_version, study_id, decision, summary, evidence_refs, reviewer_outcomes, accepted_findings, limitations. "
                "Copy the exact evidence_refs and reviewer_outcomes in order. Decision is accepted, accepted_with_limitations, or rejected. "
                "Accept a finding only when every supplied review marks it supported. An accepted result must list every finding ID. "
                "Copy program_limitations exactly as limitations, in the same order and wording; reviewers may assess them but the arbiter cannot rewrite the result package. "
                "State what the finite study shows without claiming novelty or external validity." )}
        job = {"name": "final-assessment", "actor": "methods.experiment-arbiter", "assignment": assignment,
               "validator": lambda value: validate_assessment(
                   value, self.experiment["id"], evidence_refs, outcomes, finding_ids, candidate["limitations"])}
        value, execution_ref = self._model_checked([job], images=images, stage="integrated_review")["final-assessment"]
        record = self._publish("methods/experiment-assessment", "verification", {
            **value, "execution_ref": execution_ref}, job["actor"], subjects=[execution_ref, *evidence_refs])
        if value["decision"] == "rejected":
            raise ValidationError("independent experiment assessment rejected the result")
        return value, record

    def _package(self, candidate, candidate_sha256, validation_record, validator_execution_ref,
                 assessment, assessment_record):
        package_dir = self.dir / "output" / "results-package"
        package_dir.mkdir(parents=True, exist_ok=True)
        raw = canonical_bytes({"schema_version": "experiment-observations-1", "study_id": self.experiment["id"],
                               "observations": candidate["observations"]})
        raw_path = package_dir / "raw-data.json"
        raw_path.write_bytes(raw)
        raw_record = self.store.publish_artifact(logical_id="methods/experiment-assets/raw_data",
            artifact_type="source_capture", author="methods.experiment-operator", body=raw,
            media_type="application/json", inputs=[{"ref": self.execution_refs[0], "purpose": "subject"}],
            score_ref=self.score_ref)
        assets = [{"id": "raw_data", "path": "raw-data.json", "sha256": sha256_hex(raw),
                   "role": "raw_data", "media_type": "application/json", "caption": None}]
        for item in self.asset_files:
            asset = item["descriptor"]
            destination = package_dir / asset["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item["body"])
            assets.append(dict(asset))
        package = {"schema_version": "results-package-2", "id": self.experiment["id"],
            "revision": self.experiment["revision"], "study_type": self.experiment["study_type"],
            "question": self.experiment["research_question"], "hypothesis": self.experiment["hypothesis"],
            "procedures": candidate["procedures"], "metrics": candidate["metrics"],
            "findings": candidate["findings"],
            "limitations": list(candidate["limitations"]),
            "assets": assets,
            "provenance": {"score_ref": self.score_ref,
                "literature_survey_ref": self.literature["survey_ref"],
                "literature_assessment_ref": self.literature["assessment_ref"],
                "execution_refs": self.execution_refs, "validator_execution_ref": validator_execution_ref,
                "execution_profile_ref": self.profile_refs["execution"],
                "validation_profile_ref": self.profile_refs["validation"],
                "design_ref": self.design_ref,
                "replay_sha256": candidate_sha256},
            "validation": {"decision": assessment["decision"],
                "deterministic_validation_ref": validation_record["artifact_ref"],
                "model_review_refs": [record["artifact_ref"] for record in self.review_records],
                "assessment_ref": assessment_record["artifact_ref"]}}
        if self.experiment.get("quality_contract") is not None:
            package["quality_contract"] = deepcopy(self.experiment["quality_contract"])
            if "analysis" in candidate:
                package["analysis"] = deepcopy(candidate["analysis"])
        validate_results_package(package, base_dir=package_dir)
        path = package_dir / "results-package.json"
        path.write_bytes(canonical_bytes(package))
        record = self._publish("methods/experiment-results/package", "results_package", package,
            "methods.result-integrator", subjects=[self.score_ref, *self.execution_refs,
                raw_record["artifact_ref"], validation_record["artifact_ref"], assessment_record["artifact_ref"],
                *[item["artifact_ref"] for item in self.asset_records]])
        adopted = self.store.adopt(record["artifact_id"], target_version=record["version"],
                                   expected_accepted_version=None, actor="command.controller")
        self.incumbent = adopted["artifact_ref"]
        self.time_policy.mark_first_verified_result(self.incumbent)
        self.verified_changes.append({"kind": "accepted_experiment_results", "ref": self.incumbent,
                                      "assessment_ref": assessment_record["artifact_ref"]})
        return package, path

    def run(self):
        if threading.current_thread() is not threading.main_thread():
            raise ValidationError("experiment run requires the main thread")
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt("termination requested")))
        try:
            return self._run()
        finally:
            signal.signal(signal.SIGTERM, previous)

    def _run(self):
        status, error, package, package_path = "blocked", None, None, None
        failure = None
        validation_record = assessment_record = None
        try:
            self._initialize()
            if not self.time_policy.snapshot()["initial_hard_limit_feasible"]:
                raise ValidationError("configured experiment stages do not fit the hard deadline")
            if self.literature_gate_mismatch is not None:
                # A missing prerequisite is a research work order, not a
                # failed experiment attempt.  Return the bounded request so
                # the Composer can reopen the survey closure automatically.
                status = "research_expansion_required"
                error = "literature admission requires additional evidence"
            else:
                self._setup()
                candidate = self._execute_once()
                self._capture_assets(candidate)
                replay = self._execute_once()
                candidate_sha256 = sha256_hex(canonical_bytes(candidate))
                if canonical_bytes(replay) != canonical_bytes(candidate):
                    raise ValidationError("frozen replay did not reproduce the exact experiment output")
                replay_assets = self._workspace_assets(replay)
                if any(body != self.asset_files[index]["body"] for index, (_, body) in enumerate(replay_assets)):
                    raise ValidationError("frozen replay did not reproduce the exact experiment assets")
                deterministic, validation_record, validator_execution_ref = self._deterministic_validate(
                    candidate, candidate_sha256)
                reviews, images = self._model_reviews(candidate, deterministic, validation_record)
                assessment, assessment_record = self._assess(candidate, validation_record, reviews, images)
                package, package_path = self._package(candidate, candidate_sha256, validation_record,
                                                      validator_execution_ref, assessment, assessment_record)
                status = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.blockers.append({"reason": error})
            if isinstance(exc, KeyboardInterrupt):
                status = "paused"
                failure = {"kind": "process_interrupted"}
        finally:
            for row in self.control._conn.execute("SELECT task_id FROM tasks WHERE state='awaiting_review'").fetchall():
                self.tasks.transition(row[0], "blocked", "command.controller",
                                      reason="Run ended without an accepted experiment result")
            self._checkpoint(status, force=True)
        result = {"run_id": self.run_id, "project_id": self.config["project_id"], "status": status,
            "error": error, "failure": failure, "study_id": self.experiment["id"], "incumbent_ref": self.incumbent,
            "score_ref": getattr(self, "score_ref", None), "literature": self.literature,
            "execution_refs": self.execution_refs,
            "deterministic_validation_ref": validation_record["artifact_ref"] if validation_record else None,
            "model_review_refs": [record["artifact_ref"] for record in self.review_records],
            "assessment_ref": assessment_record["artifact_ref"] if assessment_record else None,
            "results_package": str(package_path) if package_path else None,
            "research_expansion_requests": deepcopy(self.research_expansion_requests),
            "usage": self.budget.get_window("run-window"), "unreported_usage": self.usage_gaps,
            "time_plan": self.time_policy.snapshot(), "blockers": self.blockers,
            "event_chain": self.control.verify_chain(), "release_status": "not_released"}
        self._publish("command/results/final", "report", result, "command.controller")
        self._export(result, package)
        self.control.close()
        return result

    def _export(self, result, package):
        output = self.dir / "output"
        output.mkdir(exist_ok=True)
        (output / "run.json").write_bytes(canonical_bytes(result))
        lines = [f"# {self.experiment['research_question']}", "",
                 f"Run status: **{result['status']}**.", ""]
        if package is not None:
            lines += ["## Result", ""]
            for finding in package["findings"]:
                lines.append(f"- {finding['statement']}")
            lines += ["", "## Metrics", ""]
            for metric in package["metrics"]:
                lines.append(f"- **{metric['id']}**: {metric['presentation']}")
            lines += ["", "## Limitations", ""]
            for limitation in package["limitations"]:
                lines.append(f"- {limitation}")
        elif result["error"]:
            lines += ["## Blocker", "", result["error"]]
        (output / "experiment.md").write_text("\n".join(lines) + "\n")
