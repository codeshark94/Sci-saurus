"""Hash-pinned multimodal review with independent perspectives and verification."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import signal
import struct
import threading

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.runtime.execution import ExecutionRuntime, _invoke_worker
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.time_policy import TimePolicy
from scisaurus.runtime.visual_review_config import validate_visual_review_config


CHECK_OUTCOMES = {"passed", "failed", "insufficient_evidence"}
DECISIONS = {"accept", "revise", "reject", "insufficient_evidence"}
SEVERITIES = {"low", "medium", "high", "critical"}
VERIFY_CHECKS = {
    "asset-grounding",
    "criterion-coverage",
    "issue-action-traceability",
    "surgical-scope",
    "comparative-fidelity",
}


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def image_dimensions(body, media_type):
    """Read dimensions without decoding or transforming the source image."""
    if media_type == "image/png":
        if len(body) < 24 or not body.startswith(b"\x89PNG\r\n\x1a\n") or body[12:16] != b"IHDR":
            raise ValidationError("visual asset is not a valid PNG header")
        width, height = struct.unpack(">II", body[16:24])
    elif media_type == "image/jpeg":
        if len(body) < 4 or not body.startswith(b"\xff\xd8\xff"):
            raise ValidationError("visual asset is not a valid JPEG header")
        offset, width, height = 2, None, None
        while offset + 4 <= len(body):
            if body[offset] != 0xFF:
                offset += 1
                continue
            marker = body[offset + 1]
            offset += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(body):
                break
            length = int.from_bytes(body[offset:offset + 2], "big")
            if length < 2 or offset + length > len(body):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if length < 7:
                    break
                height = int.from_bytes(body[offset + 3:offset + 5], "big")
                width = int.from_bytes(body[offset + 5:offset + 7], "big")
                break
            offset += length
        if width is None or height is None:
            raise ValidationError("visual asset JPEG dimensions are unavailable")
    else:
        raise ValidationError("visual asset media type is unsupported")
    if not width or not height or width * height > 40_000_000:
        raise ValidationError("visual asset dimensions are invalid or exceed forty megapixels")
    return width, height


def _validate_asset_ids(values, asset_ids, name):
    if (not isinstance(values, list) or not values or len(set(values)) != len(values)
            or any(value not in asset_ids for value in values)):
        raise ValidationError(f"{name} must name one or more distinct configured assets")


def _validate_issue(issue, criterion_ids, asset_ids):
    _exact(issue, {"issue_id", "criterion_id", "severity", "asset_ids", "location", "observation",
                   "impact", "recommendation", "change_scope", "preserve"}, "visual issue")
    _text(issue["issue_id"], "visual issue ID")
    if issue["criterion_id"] not in criterion_ids or issue["severity"] not in SEVERITIES:
        raise ValidationError("visual issue criterion or severity is invalid")
    _validate_asset_ids(issue["asset_ids"], asset_ids, "visual issue assets")
    for key in ("location", "observation", "impact", "recommendation", "change_scope"):
        _text(issue[key], f"visual issue {key}")
    if not isinstance(issue["preserve"], list) or any(not isinstance(item, str) or not item.strip()
                                                       for item in issue["preserve"]):
        raise ValidationError("visual issue preserve list is invalid")


def _validate_strength(strength, asset_ids):
    _exact(strength, {"asset_ids", "observation", "evidence"}, "visual strength")
    _validate_asset_ids(strength["asset_ids"], asset_ids, "visual strength assets")
    _text(strength["observation"], "visual strength observation")
    _text(strength["evidence"], "visual strength evidence")


def validate_perspective(value, perspective_id, criterion_ids, asset_ids):
    _exact(value, {"perspective_id", "decision", "summary", "checks", "strengths", "issues",
                   "uncertainties"}, "perspective review")
    if value["perspective_id"] != perspective_id or value["decision"] not in DECISIONS:
        raise ValidationError("perspective review identity or decision is invalid")
    _text(value["summary"], "perspective summary")
    if not isinstance(value["checks"], list):
        raise ValidationError("perspective checks must be a list")
    seen = set()
    for check in value["checks"]:
        _exact(check, {"criterion_id", "outcome", "asset_ids", "location", "evidence", "impact",
                       "recommendation"}, "perspective check")
        criterion_id = check["criterion_id"]
        if criterion_id in seen or criterion_id not in criterion_ids or check["outcome"] not in CHECK_OUTCOMES:
            raise ValidationError("perspective check identity or outcome is invalid")
        seen.add(criterion_id)
        _validate_asset_ids(check["asset_ids"], asset_ids, "perspective check assets")
        for key in ("location", "evidence", "impact", "recommendation"):
            _text(check[key], f"perspective check {key}")
    if seen != criterion_ids:
        raise ValidationError("perspective review must execute every criterion exactly once")
    if value["decision"] == "accept" and any(check["outcome"] != "passed" for check in value["checks"]):
        raise ValidationError("perspective cannot accept with an unresolved criterion")
    for strength in value["strengths"] if isinstance(value["strengths"], list) else ():
        _validate_strength(strength, asset_ids)
    if not isinstance(value["strengths"], list):
        raise ValidationError("perspective strengths must be a list")
    issue_ids = set()
    if not isinstance(value["issues"], list):
        raise ValidationError("perspective issues must be a list")
    for issue in value["issues"]:
        _validate_issue(issue, criterion_ids, asset_ids)
        if issue["issue_id"] in issue_ids:
            raise ValidationError("perspective issue IDs must be unique")
        issue_ids.add(issue["issue_id"])
    failed_criteria = {check["criterion_id"] for check in value["checks"] if check["outcome"] == "failed"}
    if failed_criteria - {issue["criterion_id"] for issue in value["issues"]}:
        raise ValidationError("each failed perspective criterion must have a scoped issue")
    if value["decision"] in {"revise", "reject"} and not value["issues"]:
        raise ValidationError("a revise or reject perspective requires a scoped visual issue")
    if not isinstance(value["uncertainties"], list) or any(
            not isinstance(item, str) or not item.strip() for item in value["uncertainties"]):
        raise ValidationError("perspective uncertainties must be substantive strings")
    return value


def validate_assessment(value, review_id, criterion_ids, asset_ids, asset_refs, review_refs,
                        perspective_reviews):
    _exact(value, {"schema_version", "review_id", "decision", "summary", "asset_refs",
                   "perspective_review_refs", "criteria", "strengths", "issues",
                   "prioritized_actions", "uncertainties"}, "visual assessment")
    if (value["schema_version"] != "visual-assessment-1" or value["review_id"] != review_id
            or value["decision"] not in DECISIONS or value["asset_refs"] != asset_refs
            or value["perspective_review_refs"] != review_refs):
        raise ValidationError("visual assessment identity or dependency references are invalid")
    _text(value["summary"], "visual assessment summary")
    if not isinstance(value["criteria"], list):
        raise ValidationError("visual assessment criteria must be a list")
    seen = set()
    for check in value["criteria"]:
        _exact(check, {"criterion_id", "outcome", "asset_ids", "evidence", "perspective_outcomes"},
               "visual assessment criterion")
        criterion_id = check["criterion_id"]
        if criterion_id in seen or criterion_id not in criterion_ids or check["outcome"] not in CHECK_OUTCOMES:
            raise ValidationError("visual assessment criterion identity or outcome is invalid")
        seen.add(criterion_id)
        _validate_asset_ids(check["asset_ids"], asset_ids, "visual assessment criterion assets")
        _text(check["evidence"], "visual assessment criterion evidence")
        expected = [{"perspective_id": review["perspective_id"], "outcome": next(
            item["outcome"] for item in review["checks"] if item["criterion_id"] == criterion_id)}
                    for review in perspective_reviews]
        if check["perspective_outcomes"] != expected:
            raise ValidationError("visual assessment must copy each perspective outcome exactly")
        if check["outcome"] == "passed" and any(item["outcome"] != "passed" for item in expected):
            raise ValidationError("visual assessment cannot pass an unresolved perspective outcome")
    if seen != criterion_ids:
        raise ValidationError("visual assessment must cover every criterion exactly once")
    if value["decision"] == "accept" and any(check["outcome"] != "passed" for check in value["criteria"]):
        raise ValidationError("visual assessment cannot accept with an unresolved criterion")
    if not isinstance(value["strengths"], list):
        raise ValidationError("visual assessment strengths must be a list")
    for strength in value["strengths"]:
        _validate_strength(strength, asset_ids)
    issue_ids = set()
    if not isinstance(value["issues"], list):
        raise ValidationError("visual assessment issues must be a list")
    for issue in value["issues"]:
        _validate_issue(issue, criterion_ids, asset_ids)
        if issue["issue_id"] in issue_ids:
            raise ValidationError("visual assessment issue IDs must be unique")
        issue_ids.add(issue["issue_id"])
    if not isinstance(value["prioritized_actions"], list):
        raise ValidationError("visual assessment actions must be a list")
    ranks = []
    for action in value["prioritized_actions"]:
        _exact(action, {"rank", "issue_ids", "asset_ids", "objective", "allowed_changes",
                       "protected_elements", "verification"}, "visual action")
        if type(action["rank"]) is not int or action["rank"] < 1:
            raise ValidationError("visual action rank must be positive")
        ranks.append(action["rank"])
        if (not isinstance(action["issue_ids"], list) or not action["issue_ids"]
                or any(issue not in issue_ids for issue in action["issue_ids"])):
            raise ValidationError("visual action must trace to reported issues")
        _validate_asset_ids(action["asset_ids"], asset_ids, "visual action assets")
        for key in ("objective", "verification"):
            _text(action[key], f"visual action {key}")
        if not isinstance(action["allowed_changes"], list) or not action["allowed_changes"] or any(
                not isinstance(item, str) or not item.strip() for item in action["allowed_changes"]):
            raise ValidationError("visual action allowed changes must be substantive strings")
        if not isinstance(action["protected_elements"], list) or any(
                not isinstance(item, str) or not item.strip() for item in action["protected_elements"]):
            raise ValidationError("visual action protected elements are invalid")
    if ranks != list(range(1, len(ranks) + 1)):
        raise ValidationError("visual action ranks must be contiguous and ordered")
    covered_issues = {issue_id for action in value["prioritized_actions"] for issue_id in action["issue_ids"]}
    if covered_issues != issue_ids:
        raise ValidationError("every synthesized visual issue must have a prioritized action")
    if value["decision"] in {"revise", "reject"} and not issue_ids:
        raise ValidationError("a revise or reject decision requires a scoped visual issue")
    if not isinstance(value["uncertainties"], list) or any(
            not isinstance(item, str) or not item.strip() for item in value["uncertainties"]):
        raise ValidationError("visual assessment uncertainties must be substantive strings")
    return value


def validate_verification(value, assessment_ref):
    _exact(value, {"assessment_ref", "checks", "rationale"}, "visual verification")
    if value["assessment_ref"] != assessment_ref or not isinstance(value["checks"], list):
        raise ValidationError("visual verification target or checks are invalid")
    _text(value["rationale"], "visual verification rationale")
    seen = set()
    for check in value["checks"]:
        _exact(check, {"check_id", "outcome", "method", "result"}, "visual verification check")
        if (check["check_id"] in seen or check["check_id"] not in VERIFY_CHECKS
                or check["outcome"] not in CHECK_OUTCOMES):
            raise ValidationError("visual verification check identity or outcome is invalid")
        seen.add(check["check_id"])
        _text(check["method"], "visual verification method")
        _text(check["result"], "visual verification result")
    if seen != VERIFY_CHECKS:
        raise ValidationError("visual verification must execute every required check exactly once")
    return value


class VisualReviewRunner(ExecutionRuntime):
    def __init__(self, project_dir, config, *, on_progress=None):
        config = deepcopy(validate_visual_review_config(config))
        super().__init__(project_dir, config, worker_target=_invoke_worker, on_progress=on_progress)
        self.review = config["visual_review"]
        self.asset_records, self.asset_metadata, self.image_descriptors = [], [], []
        self.review_records = []
        self.serial = 0
        self.time_policy = TimePolicy(stage_seconds=self.review["stage_seconds"],
            unit_count=len(self.review["perspectives"]),
            worker_slots=self.config["limits"]["concurrent_calls"] - 1,
            wall_clock_seconds=self.config["limits"]["wall_clock_seconds"],
            policy=self.config.get("time_policy"))
        self.time_policy.started_at = self.started
        self.deadline = min(self.deadline, self.started + self.time_policy.hard_seconds)

    def _initialize(self):
        self.score = self._publish(f"command/scores/{self.review['id']}", "note", {
            "schema_version": "visual-review-score-1", "visual_review": self.review,
            "time_policy": self.config.get("time_policy")}, "principal")
        self.score_ref = self.score["artifact_ref"]
        self.context = self._publish("inputs/context", "note", {
            "objective": self.config["objective"], "supplied_context": self.config["supplied_context"],
            "target_medium": self.review["target_medium"],
            "intended_audience": self.review["intended_audience"]}, "principal", subjects=[self.score_ref])
        combined = 0
        for asset in self.review["assets"]:
            try:
                path = Path(asset["path"]).resolve(strict=True)
                if not path.is_file():
                    raise OSError("not a regular file")
                body = path.read_bytes()
            except OSError as exc:
                raise ValidationError(f"visual asset is unavailable: {asset['id']}") from exc
            combined += len(body)
            if combined > self.config["model"].get("max_image_bytes", 7_000_000):
                raise ValidationError("combined visual assets exceed the configured model image limit")
            width, height = image_dimensions(body, asset["media_type"])
            record = self.store.publish_artifact(
                logical_id=f"inputs/visual-assets/{asset['id']}", artifact_type="source_capture",
                author="principal", body=body, media_type=asset["media_type"], score_ref=self.score_ref,
                inputs=[{"ref": self.context["artifact_ref"], "purpose": "subject"}])
            metadata = {key: asset[key] for key in ("id", "role", "label", "media_type")}
            metadata.update(asset_ref=record["artifact_ref"], sha256=record["body_hash"],
                            bytes=len(body), width=width, height=height)
            metadata_record = self._publish(f"inputs/visual-asset-metadata/{asset['id']}", "note", metadata,
                                            "principal", subjects=[record["artifact_ref"]])
            self.asset_records.append(record)
            self.asset_metadata.append({**metadata, "metadata_ref": metadata_record["artifact_ref"]})
            self.image_descriptors.append({"path": str(Path(self.store.objects_dir) / record["body_hash"]),
                                           "media_type": asset["media_type"], "sha256": record["body_hash"]})
        self._checkpoint("visual_assets_captured", force=True)

    def _models_checked(self, jobs, *, stage, task_kind):
        """Retry only schema-rejected visual judgments against the same pinned images."""
        pending, accepted, feedback = list(jobs), {}, {}
        for _ in range(self.config["limits"]["max_rounds"]):
            decision = self.time_policy.admit(stage, task_count=len(pending))
            if not decision["allowed"]:
                raise ValidationError(f"time admission deferred visual {stage}: {decision['reason']}")
            specs = []
            for job in pending:
                self.serial += 1
                assignment = deepcopy(job["assignment"])
                if job["name"] in feedback:
                    assignment["validation_feedback"] = feedback[job["name"]]
                specs.append({"task_id": f"visual-{job['name']}-{self.serial}", "kind": "model",
                              "actor": job["actor"], "task_kind": task_kind,
                              "params": {"client": self.config["model"],
                                         "prompt": json.dumps(assignment, ensure_ascii=False),
                                         "images": self.image_descriptors}})
            outcomes = self._call_batch(specs, max_parallel=self.config["limits"]["concurrent_calls"] - 1)
            rejected = []
            for job, spec in zip(pending, specs):
                outcome = outcomes[spec["task_id"]]
                if not outcome["ok"]:
                    raise ValidationError(f"visual model dispatch failed: {job['name']}: {outcome['error']}")
                result = ModelResult(**outcome["result"])
                self.time_policy.observe(stage, result.elapsed_seconds)
                try:
                    value = result.json_object()
                except ValidationError:
                    value = {"raw_text": result.text}
                proposal = self._publish(f"command/model-proposals/{spec['task_id']}", "note", value,
                                         spec["actor"], subjects=[outcome["record_ref"]])
                try:
                    if result.finish_reason != "stop":
                        raise ValidationError("visual model generation did not finish normally")
                    value = result.json_object()
                    job["validator"](value)
                except (ValidationError, TypeError, ValueError, KeyError) as exc:
                    feedback[job["name"]] = {
                        "error": str(exc), "previous_response": value,
                        "scope": ("Repair only this response's contract violations. Preserve every valid judgment and "
                                  "visible observation. Return exactly the requested fields and types, with no extra fields; "
                                  "preserve is always a list of strings."),
                    }
                    self.tasks.transition(spec["task_id"], "blocked", "command.controller", reason=str(exc))
                    self._publish(f"command/validation/{spec['task_id']}", "note", {"error": str(exc)},
                                  "command.controller", subjects=[proposal["artifact_ref"]])
                    rejected.append(job)
                    continue
                self._complete(spec["task_id"])
                accepted[job["name"]] = (value, outcome["record_ref"])
            if not rejected:
                return accepted
            pending = rejected
        raise ValidationError("; ".join(
            f"{job['name']} did not satisfy its visual contract: {feedback[job['name']]['error']}"
            for job in pending))

    def _review_assignment(self, perspective):
        return {
            "phase": "visual_perspective_review", "review_id": self.review["id"],
            "perspective": perspective, "objective": self.config["objective"],
            "supplied_context": self.config["supplied_context"], "mode": self.review["mode"],
            "target_medium": self.review["target_medium"], "intended_audience": self.review["intended_audience"],
            "assets_in_image_order": self.asset_metadata, "criteria": self.review["criteria"],
            "instructions": (
                "Inspect every attached image directly. Return exactly perspective_id, decision, summary, checks, strengths, issues, uncertainties. "
                "Execute every criterion exactly once. Each check is {criterion_id,outcome,asset_ids,location,evidence,impact,recommendation}; outcome is passed, failed, or insufficient_evidence. "
                "Each strength is {asset_ids,observation,evidence}. Each issue is {issue_id,criterion_id,severity,asset_ids,location,observation,impact,recommendation,change_scope,preserve}. "
                "Issue severity is exactly low, medium, high, or critical. In every issue, preserve is a JSON list of substantive strings. Do not add a perspective object or any unrequested field. "
                "Decisions are accept, revise, reject, or insufficient_evidence. Ground observations in visible evidence and use concrete image locations. "
                "Do not infer scientific truth from visual polish. For revisions, state the smallest sufficient change scope and what must remain unchanged. "
                "Evaluate source and rendered images according to their declared roles; do not confuse a reference with the subject."),
        }

    def _perspective_reviews(self):
        criterion_ids = {item["id"] for item in self.review["criteria"]}
        asset_ids = {item["id"] for item in self.review["assets"]}
        jobs = [{"name": f"review-{p['id']}", "actor": f"editorial.visual-reviewer.{p['id']}",
                 "assignment": self._review_assignment(p),
                 "validator": lambda value, pid=p["id"]: validate_perspective(
                     value, pid, criterion_ids, asset_ids)} for p in self.review["perspectives"]]
        outcomes = self._models_checked(jobs, stage="production", task_kind="verification")
        values = []
        for perspective, job in zip(self.review["perspectives"], jobs):
            value, execution = outcomes[job["name"]]
            record = self._publish(f"editorial/visual-reviews/{perspective['id']}", "critique", {
                **value, "execution_ref": execution,
                "asset_refs": [item["artifact_ref"] for item in self.asset_records]},
                job["actor"], subjects=[execution, *[item["artifact_ref"] for item in self.asset_records]])
            self.review_records.append(record)
            values.append(value)
        return values

    def _synthesize(self, reviews):
        asset_refs = [item["artifact_ref"] for item in self.asset_records]
        review_refs = [item["artifact_ref"] for item in self.review_records]
        assignment = {
            "phase": "visual_assessment_synthesis", "review_id": self.review["id"],
            "objective": self.config["objective"], "mode": self.review["mode"],
            "target_medium": self.review["target_medium"], "intended_audience": self.review["intended_audience"],
            "assets_in_image_order": self.asset_metadata, "asset_refs_exact": asset_refs,
            "criteria": self.review["criteria"], "perspective_reviews": reviews,
            "perspective_review_refs_exact": review_refs,
            "instructions": (
                "Inspect the attached images yourself and reconcile the independent reviews. Return a visual-assessment-1 object with exactly schema_version, review_id, decision, summary, asset_refs, perspective_review_refs, criteria, strengths, issues, prioritized_actions, uncertainties. "
                "Copy the exact supplied asset and perspective refs. Each criterion is {criterion_id,outcome,asset_ids,evidence,perspective_outcomes}. perspective_outcomes is the exact ordered list of {perspective_id,outcome} copied from the supplied reviews; do not summarize or alter it. Use the same strength and issue schemas as the reviews. "
                "Each action is {rank,issue_ids,asset_ids,objective,allowed_changes,protected_elements,verification}; allowed_changes and protected_elements are JSON lists of substantive strings, ranks are contiguous from 1, and every action traces to a reported issue. "
                "Resolve disagreements using visible evidence. Preserve scientific meaning, data marks, labels, scale bars, aspect ratio, and source assets unless the review contract explicitly permits a change. "
                "Do not prescribe a broad redesign when a local correction resolves the issue."),
        }
        validator = lambda value: validate_assessment(
            value, self.review["id"], {item["id"] for item in self.review["criteria"]},
            {item["id"] for item in self.review["assets"]}, asset_refs, review_refs, reviews)
        value, execution = self._models_checked([{
            "name": "synthesis", "actor": "editorial.visual-integrator",
            "assignment": assignment, "validator": validator,
        }], stage="unit_review", task_kind="selection")["synthesis"]
        record = self._publish(f"editorial/visual-assessments/{self.review['id']}", "report", {
            **value, "execution_ref": execution}, "editorial.visual-integrator",
            subjects=[execution, *review_refs, *asset_refs])
        return value, record

    def _verify(self, assessment, record):
        assignment = {
            "phase": "visual_assessment_verification", "assessment_ref": record["artifact_ref"],
            "objective": self.config["objective"], "assets_in_image_order": self.asset_metadata,
            "criteria": self.review["criteria"], "perspective_reviews": [
                json.loads(self.store.read_body(item["body_hash"])) for item in self.review_records],
            "assessment": assessment,
            "required_checks": sorted(VERIFY_CHECKS),
            "instructions": (
                "Independently inspect the attached images and verify the exact assessment. Return exactly assessment_ref, checks, rationale. "
                "Execute every required check exactly once as {check_id,outcome,method,result}; outcome is passed, failed, or insufficient_evidence. "
                "Check that observations describe visible content, all criteria are covered, actions trace to issues, change scopes are the smallest sufficient scopes with protected elements, and comparisons respect subject/reference/source/rendered roles. "
                "A well-written assessment fails if its visual evidence is wrong or its proposed change could disturb unrelated content."),
        }
        value, execution = self._models_checked([{
            "name": "verification", "actor": "methods.visual-verifier", "assignment": assignment,
            "validator": lambda value: validate_verification(value, record["artifact_ref"]),
        }], stage="integrated_review", task_kind="verification")["verification"]
        verification = self._publish(f"methods/visual-verifications/{self.review['id']}", "verification", {
            **value, "execution_ref": execution}, "methods.visual-verifier",
            subjects=[record["artifact_ref"], execution, *[item["artifact_ref"] for item in self.asset_records]])
        if any(check["outcome"] != "passed" for check in value["checks"]):
            raise ValidationError("independent visual verification did not pass every required check")
        adopted = self.store.adopt(record["artifact_id"], target_version=record["version"],
                                   expected_accepted_version=None, actor="command.controller")
        self.incumbent = adopted["artifact_ref"]
        self.time_policy.mark_first_verified_result(self.incumbent)
        self.verified_changes.append({"kind": "accepted_visual_assessment", "ref": self.incumbent,
                                      "verification_ref": verification["artifact_ref"]})
        return verification

    def run(self):
        if threading.current_thread() is not threading.main_thread():
            raise ValidationError("visual review requires the main thread")
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt("termination requested")))
        try:
            return self._run()
        finally:
            signal.signal(signal.SIGTERM, previous)

    def _run(self):
        status, error, assessment, assessment_record, verification = "blocked", None, None, None, None
        try:
            self._initialize()
            if not self.time_policy.snapshot()["initial_hard_limit_feasible"]:
                raise ValidationError("configured visual review stages do not fit the hard deadline")
            reviews = self._perspective_reviews()
            assessment, assessment_record = self._synthesize(reviews)
            verification = self._verify(assessment, assessment_record)
            status = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.blockers.append({"reason": error})
        finally:
            for row in self.control._conn.execute("SELECT task_id FROM tasks WHERE state='awaiting_review'").fetchall():
                self.tasks.transition(row[0], "blocked", "command.controller",
                                      reason="Run ended without an accepted scoped output")
            self._checkpoint(status, force=True)
        result = {
            "run_id": self.run_id, "project_id": self.config["project_id"], "status": status,
            "error": error, "review_id": self.review["id"], "incumbent_ref": self.incumbent,
            "assessment_ref": self.incumbent,
            "assessment_current": bool(self.incumbent), "decision": assessment.get("decision") if assessment else None,
            "asset_refs": [item["artifact_ref"] for item in self.asset_records],
            "perspective_review_refs": [item["artifact_ref"] for item in self.review_records],
            "verification_ref": verification["artifact_ref"] if verification else None,
            "usage": self.budget.get_window("run-window"), "unreported_usage": self.usage_gaps,
            "time_plan": self.time_policy.snapshot(), "blockers": self.blockers,
            "event_chain": self.control.verify_chain(), "release_status": "not_released",
        }
        self._publish("command/results/final", "report", result, "command.controller")
        self._export(result, assessment)
        self.control.close()
        return result

    def _export(self, result, assessment):
        output = self.dir / "output"
        output.mkdir(exist_ok=True)
        (output / "run.json").write_bytes(canonical_bytes(result))
        if assessment is not None:
            (output / "visual-assessment.json").write_bytes(canonical_bytes(assessment))
            lines = [f"# {self.review['id']}", "", assessment["summary"], "",
                     f"Decision: **{assessment['decision']}**", "", "## Criteria", ""]
            for check in assessment["criteria"]:
                lines += [f"- **{check['criterion_id']} — {check['outcome']}**: {check['evidence']}"]
            lines += ["", "## Prioritized actions", ""]
            if assessment["prioritized_actions"]:
                for action in assessment["prioritized_actions"]:
                    lines += [f"{action['rank']}. **{action['objective']}** — {'; '.join(action['allowed_changes'])}. "
                              f"Verification: {action['verification']}"]
            else:
                lines += ["No corrective action was accepted by the assessment."]
            (output / "visual-assessment.md").write_text("\n".join(lines) + "\n")
