"""Project-scoped executive composer for end-to-end research workflows.

The composer is the control loop around specialist runners.  It owns the
workflow graph, dependency admission, elapsed-time accounting, checkpoints,
feedback routing, and release proposal.  It does not make scientific
acceptance decisions or edit departmental artifacts directly; those remain the
responsibility of the stage runner and its independent checks.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.messages import MessageBus
from scisaurus.core.schema import canonical_bytes, now_iso
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager


SCHEMA_VERSION = "composer-workflow-1"
RUN_SCHEMA_VERSION = "composer-run-1"
STAGE_KINDS = frozenset({"survey", "experiment", "interpretation", "argument", "paper"})
STAGE_ROLES = {
    "survey": "research.intelligence",
    "experiment": "methods.validation",
    "interpretation": "strategy.interpretation",
    "argument": "strategy.argument",
    "paper": "editorial.composer",
}
DEPARTMENT_ADDRESSES = {
    "research.intelligence": {"dept": "research", "agent": "chief"},
    "methods.validation": {"dept": "methods", "agent": "chief"},
    "strategy.interpretation": {"dept": "strategy", "agent": "chief"},
    "strategy.argument": {"dept": "strategy", "agent": "chief"},
    "editorial.composer": {"dept": "editorial", "agent": "editor-in-chief"},
}
COMMAND_ADDRESSES = {
    "arbiter": {"dept": "executive-command", "agent": "arbiter"},
    "progress": {"dept": "executive-command", "agent": "progress-controller"},
    "intent": {"dept": "executive-command", "agent": "intent-keeper"},
}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _positive_number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{name} must be finite and positive")


def validate_workflow(value):
    """Validate the immutable composer workflow contract."""
    fields = {"schema_version", "id", "revision", "project_id", "objective", "stages", "time_policy", "completion"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError(f"composer workflow requires exactly {sorted(fields)}")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError(f"composer workflow schema must be {SCHEMA_VERSION}")
    _identifier(value["id"], "workflow id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("workflow revision must be a positive integer")
    _text(value["project_id"], "workflow project_id")
    _text(value["objective"], "workflow objective")
    stages = value["stages"]
    if not isinstance(stages, list) or not stages:
        raise ValidationError("composer workflow requires at least one stage")
    stage_ids, outputs = set(), set()
    stage_fields = {"id", "kind", "config_path", "project_dir", "depends_on", "estimate_seconds", "bindings",
                    "deadline_seconds", "reuse_completed", "reuse_output_path"}
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != stage_fields:
            raise ValidationError(f"composer stage requires exactly {sorted(stage_fields)}")
        stage_id = _identifier(stage["id"], "stage id")
        if stage_id in stage_ids:
            raise ValidationError("composer stage IDs must be unique")
        stage_ids.add(stage_id)
        if stage["kind"] not in STAGE_KINDS:
            raise ValidationError(f"unsupported composer stage kind: {stage['kind']}")
        for key in ("config_path", "project_dir"):
            path = Path(stage[key])
            if not path.is_absolute() or not path.exists():
                raise ValidationError(f"stage {stage_id} {key} must be an existing absolute path")
        _positive_number(stage["estimate_seconds"], f"stage {stage_id} estimate_seconds")
        _positive_number(stage["deadline_seconds"], f"stage {stage_id} deadline_seconds")
        deps = stage["depends_on"]
        if not isinstance(deps, list) or len(deps) != len(set(deps)) or any(not isinstance(dep, str) for dep in deps):
            raise ValidationError(f"stage {stage_id} depends_on must be a unique string list")
        if stage_id in deps:
            raise ValidationError(f"stage {stage_id} cannot depend on itself")
        bindings = stage["bindings"]
        if not isinstance(bindings, list):
            raise ValidationError(f"stage {stage_id} bindings must be a list")
        for binding in bindings:
            if not isinstance(binding, dict) or set(binding) != {"target", "source"}:
                raise ValidationError(f"stage {stage_id} binding must have target and source")
            _text(binding["target"], f"stage {stage_id} binding target")
            _text(binding["source"], f"stage {stage_id} binding source")
        if type(stage["reuse_completed"]) is not bool:
            raise ValidationError(f"stage {stage_id} reuse_completed must be Boolean")
        checkpoint = stage["reuse_output_path"]
        if checkpoint is not None:
            if (not isinstance(checkpoint, str) or not Path(checkpoint).is_absolute()
                    or not Path(checkpoint).is_file()):
                raise ValidationError(f"stage {stage_id} reuse_output_path must be an existing absolute file or null")
        if stage["reuse_completed"] and checkpoint is None:
            # A stage may use its conventional output/run.json when no file is
            # supplied.  The explicit Boolean is what makes that reuse a
            # deliberate workflow decision rather than an implicit fallback.
            pass
        outputs.add(stage_id)
    for stage in stages:
        if set(stage["depends_on"]) - stage_ids:
            raise ValidationError(f"stage {stage['id']} depends on an unknown stage")
    # Dependency graph must be acyclic; a topological order is also used for
    # deterministic dispatch and restart accounting.
    visiting, visited = set(), set()
    by_id = {stage["id"]: stage for stage in stages}
    def visit(stage_id):
        if stage_id in visiting:
            raise ValidationError("composer stage graph must be acyclic")
        if stage_id in visited:
            return
        visiting.add(stage_id)
        for dep in by_id[stage_id]["depends_on"]:
            visit(dep)
        visiting.remove(stage_id)
        visited.add(stage_id)
    for stage_id in stage_ids:
        visit(stage_id)
    policy = value["time_policy"]
    policy_fields = {"first_result_seconds", "target_seconds", "hard_seconds", "checkpoint_seconds"}
    if not isinstance(policy, dict) or set(policy) != policy_fields:
        raise ValidationError(f"time_policy requires exactly {sorted(policy_fields)}")
    for key in policy_fields:
        _positive_number(policy[key], f"time_policy.{key}")
    if not (policy["first_result_seconds"] <= policy["target_seconds"] <= policy["hard_seconds"]):
        raise ValidationError("composer time policy requires first_result <= target <= hard")
    for stage in stages:
        if stage["deadline_seconds"] > policy["hard_seconds"]:
            raise ValidationError(f"stage {stage['id']} deadline_seconds cannot exceed composer hard_seconds")
    completion = value["completion"]
    completion_fields = {"required_stage_ids", "release_requires_human"}
    if not isinstance(completion, dict) or set(completion) != completion_fields:
        raise ValidationError(f"completion requires exactly {sorted(completion_fields)}")
    required = completion["required_stage_ids"]
    if not isinstance(required, list) or not required or len(required) != len(set(required)) or set(required) - stage_ids:
        raise ValidationError("completion.required_stage_ids must name unique known stages")
    if type(completion["release_requires_human"]) is not bool:
        raise ValidationError("completion.release_requires_human must be Boolean")
    canonical_bytes(value)
    return value


def _set_path(container, path, value):
    parts = path.split(".")
    if not parts or any(not part for part in parts):
        raise ValidationError("binding target path is invalid")
    current = container
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            raise ValidationError(f"binding target does not exist: {path}")
        current = current[part]
    if not isinstance(current, dict) or parts[-1] not in current:
        raise ValidationError(f"binding target does not exist: {path}")
    current[parts[-1]] = deepcopy(value)


def _get_path(container, path):
    current = container
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValidationError(f"binding source does not exist: {path}")
        current = current[part]
    return deepcopy(current)


class ComposerRunner:
    """Execute a complete stage graph with a durable composer feedback loop.

    The stage runners are deliberately called through a small allowlist.  A
    workflow cannot smuggle an arbitrary shell command into the control plane;
    operations that need a program or MCP service remain inside their own
    project-scoped runner.
    """

    def __init__(self, workflow, *, resume=False, clock=time.monotonic, on_progress=None):
        self.workflow = deepcopy(validate_workflow(workflow))
        self.root = Path(self.workflow["project_id"]).resolve()
        # project_id is the stable identity; the workflow's project directory
        # is derived from it so a config cannot redirect the control ledger.
        self.root.mkdir(parents=True, exist_ok=True)
        existing = (self.root / "state" / "control.sqlite").exists()
        if existing and not resume:
            raise ValidationError("composer project already exists; pass resume=True to continue it")
        if resume and not existing:
            raise ValidationError("composer resume requires an existing project")
        self.control = ControlStore(self.root)
        self.store = ArtifactStore(self.control)
        self.messages = MessageBus(self.control)
        self.store.init_project(principal_note=f"composer:{self.workflow['id']}")
        self.tasks = TaskManager(self.control)
        self.clock = clock
        self.on_progress = on_progress or (lambda state: None)
        self.started = self.clock()
        policy = self.workflow["time_policy"]
        self.deadline = self.started + float(policy["hard_seconds"])
        self.next_checkpoint = self.started
        self.run_id = uuid.uuid4().hex
        self.context = {}
        self.stage_records = {}
        self.feedback = []
        self.blockers = []
        self.usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.status = "running"
        self._workflow_record = None
        if not existing:
            self._workflow_record = self._publish("command/composer/workflow", "note", self.workflow, "command.composer")
            self._publish("inputs/composer-run", "note", {
                "schema_version": "composer-run-input-1", "workflow_ref": self._workflow_record["artifact_ref"],
                "run_id": self.run_id, "resume": False,
            }, "command.composer")
        else:
            head = self.store.head("command/composer/workflow")
            if head is None or json.loads(self.store.read_body(head["body_hash"])) != self.workflow:
                raise ValidationError("composer resume workflow does not match the original immutable workflow")
            self._restore()

    def close(self):
        if self.control is not None:
            self.control.close()
            self.control = None

    def _publish(self, logical_id, artifact_type, body, author):
        return self.store.publish_artifact(logical_id=logical_id, artifact_type=artifact_type,
                                           author=author, body=canonical_bytes(body),
                                           media_type="application/json")

    def _remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise ValidationError("composer hard deadline exceeded")
        return remaining

    def _checkpoint(self, phase, *, force=False):
        now = self.clock()
        if not force and now < self.next_checkpoint:
            return
        state = {
            "schema_version": "composer-checkpoint-1", "run_id": self.run_id,
            "phase": phase, "elapsed_seconds": max(0.0, now - self.started),
            "remaining_seconds": max(0.0, self.deadline - now),
            "stages": deepcopy(self.stage_records), "feedback": deepcopy(self.feedback),
            "blockers": deepcopy(self.blockers), "usage": deepcopy(self.usage),
        }
        self._publish(f"command/composer/checkpoints/{len(self.feedback) + len(self.stage_records) + 1}",
                      "progress_checkpoint", state, "command.composer")
        output = self.root / "output"
        output.mkdir(parents=True, exist_ok=True)
        (output / "progress.json").write_bytes(canonical_bytes(state))
        self.next_checkpoint = now + float(self.workflow["time_policy"]["checkpoint_seconds"])
        self.on_progress({"phase": phase, "elapsed_seconds": round(state["elapsed_seconds"], 2),
                          "remaining_seconds": round(state["remaining_seconds"], 2),
                          "stages": deepcopy(self.stage_records), "blockers": deepcopy(self.blockers)})

    def _live_progress(self, phase):
        """Publish a non-ledger progress tick while a provider call is active.

        A long model request may not emit a semantic event until it returns.
        The tick deliberately writes only the live checkpoint projection; it
        does not create a decision or alter task state. Durable control events
        continue to be written by ``_checkpoint`` at stage boundaries.
        """
        now = self.clock()
        state = {
            "schema_version": "composer-checkpoint-1", "run_id": self.run_id,
            "phase": phase, "elapsed_seconds": max(0.0, now - self.started),
            "remaining_seconds": max(0.0, self.deadline - now),
            "stages": deepcopy(self.stage_records), "feedback": deepcopy(self.feedback),
            "blockers": deepcopy(self.blockers), "usage": deepcopy(self.usage),
        }
        output = self.root / "output"
        output.mkdir(parents=True, exist_ok=True)
        temporary = output / f"progress-live-{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(canonical_bytes(state))
        temporary.replace(output / "progress.json")
        self.on_progress({"phase": phase, "elapsed_seconds": round(state["elapsed_seconds"], 2),
                          "remaining_seconds": round(state["remaining_seconds"], 2),
                          "stages": deepcopy(self.stage_records), "blockers": deepcopy(self.blockers)})

    def _start_live_progress(self, stage):
        """Start a bounded ticker for one admitted stage."""
        interval = max(0.5, float(self.workflow["time_policy"]["checkpoint_seconds"]))
        stop = threading.Event()

        def tick():
            while not stop.wait(interval):
                try:
                    self._live_progress(f"{stage['id']}:running")
                except Exception:
                    # A live status tick must never hide the stage's real
                    # result or turn a completed provider call into a failure.
                    return

        thread = threading.Thread(target=tick, name=f"composer-progress-{stage['id']}", daemon=True)
        thread.start()

        def finish():
            stop.set()
            thread.join(timeout=min(interval, 2.0))

        return finish

    def _restore(self):
        head = self.store.head("command/composer/run")
        if head is not None:
            body = json.loads(self.store.read_body(head["body_hash"]))
            self.status = body.get("status", "running")
            self.stage_records = body.get("stages", {})
            self.context = body.get("context", {})
            self.feedback = body.get("feedback", [])
            self.blockers = body.get("blockers", [])
            self.usage = body.get("usage", self.usage)
        if self.status in {"completed", "candidate_needs_review", "blocked", "paused"}:
            # A resumed workflow must explicitly continue from a non-terminal
            # checkpoint; completed output remains inspectable and immutable.
            self.status = "running"

    def _source_value(self, expression):
        parts = expression.split(".")
        if len(parts) < 2 or parts[0] not in self.context:
            raise ValidationError(f"binding source must name a completed stage: {expression}")
        return _get_path(self.context[parts[0]], ".".join(parts[1:]))

    def _interpretation_binding_path(self, value):
        """Return a direct interpretation artifact for a bound package path.

        Stage outputs are often persisted as envelopes (status, usage, and the
        actual scientific interpretation).  The release validator consumes
        the inner interpretation object, so a path binding must project that
        shape explicitly instead of handing the envelope to the next stage.
        The projection is content-addressed and immutable within this
        Composer run.
        """
        if not isinstance(value, str):
            return value
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            return value
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"bound interpretation artifact is unreadable: {path}") from exc
        if isinstance(payload, dict) and isinstance(payload.get("interpretation"), dict):
            interpretation = payload["interpretation"]
            from scisaurus.runtime.scientific_interpretation import validate_interpretation
            validate_interpretation(interpretation)
            digest = hashlib.sha256(canonical_bytes(interpretation)).hexdigest()
            target = self.root / "output" / "bindings" / f"scientific-interpretation-{digest}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() != canonical_bytes(interpretation):
                raise ValidationError("content-addressed interpretation binding was modified")
            if not target.exists():
                target.write_bytes(canonical_bytes(interpretation))
            return str(target.resolve())
        from scisaurus.runtime.scientific_interpretation import validate_interpretation
        validate_interpretation(payload)
        return value

    def _apply_bindings(self, payload, bindings):
        for binding in bindings:
            value = self._source_value(binding["source"])
            # A path reference to a JSON package is resolved to the package
            # body when it is bound into another stage's packet.
            if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                try:
                    value = json.loads(Path(value).read_text())
                except (OSError, ValueError) as exc:
                    raise ValidationError(f"bound JSON package is unreadable: {value}") from exc
            _set_path(payload, binding["target"], value)
        return payload

    def _stage_task(self, stage):
        task_id = f"composer-{self.workflow['id']}-{stage['id']}"
        try:
            existing = self.tasks.get(task_id)
            if existing["state"] in {"blocked", "paused"}:
                self.tasks.transition(task_id, "queued", "command.composer", reason="scoped stage recovery")
            return self.tasks.get(task_id)
        except Exception:
            self.tasks.create(task_id, "production", {
                "stage_id": stage["id"], "kind": stage["kind"], "objective": self.workflow["objective"],
                "depends_on": stage["depends_on"], "config_path": stage["config_path"],
            }, "command.composer")
            return self.tasks.admit(task_id, "command.composer")

    def _run_stage(self, stage):
        """Dispatch one allowlisted specialist runner and return its context."""
        project_dir = Path(stage["project_dir"])
        stage_deadline = min(float(stage["deadline_seconds"]), self._remaining())
        prior_run = (Path(stage["reuse_output_path"])
                     if stage["reuse_output_path"] is not None
                     else project_dir / "output" / "run.json")
        if stage["reuse_completed"] and prior_run.is_file():
            try:
                prior = json.loads(prior_run.read_text())
            except (OSError, ValueError) as exc:
                raise ValidationError(f"reusable stage output is unreadable: {prior_run}") from exc
            if prior.get("status") in {"completed", "accepted"}:
                output_value = (prior.get("output_path") or prior.get("results_package")
                                or prior.get("pdf") or str(prior_run.resolve()))
                context = {**prior, "stage_id": stage["id"], "kind": stage["kind"],
                           "project_dir": str(project_dir.resolve()), "output_path": str(Path(output_value).resolve())}
                if stage["kind"] == "experiment":
                    context["results_package"] = prior.get("results_package")
                if stage["kind"] == "argument":
                    context["argument_package_path"] = context["output_path"]
                self._publish(f"command/composer/reuse/{stage['id']}-{len(self.feedback) + 1}",
                              "decision_note", {
                                  "stage_id": stage["id"], "action": "reuse_completed_stage",
                                  "source_run": str(prior_run.resolve()), "status": prior.get("status"),
                              }, "command.composer")
                return context
        config = json.loads(Path(stage["config_path"]).read_text())
        kind = stage["kind"]
        if kind in {"survey", "experiment"}:
            config = self._apply_bindings(config, stage["bindings"])
            config.setdefault("limits", {})["wall_clock_seconds"] = min(
                float(config["limits"]["wall_clock_seconds"]), stage_deadline)
            if "time_policy" in config and isinstance(config["time_policy"], dict):
                config["time_policy"]["hard_seconds"] = min(
                    float(config["time_policy"].get("hard_seconds", stage_deadline)), stage_deadline)
                config["time_policy"]["target_seconds"] = min(
                    float(config["time_policy"].get("target_seconds", stage_deadline)),
                    config["time_policy"]["hard_seconds"])
                config["time_policy"]["first_result_seconds"] = min(
                    float(config["time_policy"].get("first_result_seconds", config["time_policy"]["target_seconds"])),
                    config["time_policy"]["target_seconds"])
            if kind == "survey":
                from scisaurus.runtime.survey import SurveyRunner
                project_dir = Path(stage["project_dir"])
                prior_run = project_dir / "output" / "run.json"
                resume_policy = None
                if prior_run.is_file():
                    try:
                        prior = json.loads(prior_run.read_text())
                    except (OSError, ValueError):
                        prior = {}
                    if prior.get("status") in {"blocked", "paused"}:
                        # A known literature review blocker is a recoverable
                        # stage outcome.  Reopen only the affected scope and
                        # charge a bounded continuation window; never restart
                        # the whole survey or loop indefinitely.
                        resume_policy = {
                            "additional_seconds": min(1200.0, float(config["limits"]["wall_clock_seconds"])),
                            "unknown_outcomes": {"mode": "block", "usage_per_attempt": {}},
                            "source_changes": {"mode": "reopen", "reopen_scopes": ["focused_review"]},
                        }
                        self._publish(f"command/composer/recovery/{stage['id']}-{len(self.feedback) + 1}",
                                      "decision_note", {
                                          "stage_id": stage["id"], "action": "resume_scoped_stage",
                                          "scope": "focused_review", "reason": prior.get("error", "recoverable stage blocker"),
                                          "additional_seconds": resume_policy["additional_seconds"],
                                      }, "command.composer")
                result = SurveyRunner(stage["project_dir"], config, on_progress=self._stage_progress(stage),
                                      resume_policy=resume_policy).run()
            else:
                from scisaurus.runtime.experiment import ExperimentRunner
                result = ExperimentRunner(stage["project_dir"], config, on_progress=self._stage_progress(stage)).run()
            output_path = Path(stage["project_dir"]) / "output" / "run.json"
        elif kind == "interpretation":
            from scisaurus.runtime.scientific_interpretation import ScientificInterpretationRunner
            descriptor_bindings = [item for item in stage["bindings"]
                                   if not item["target"].startswith("packet.")]
            config = self._apply_bindings(config, descriptor_bindings)
            model_path = Path(config.pop("model_config_path"))
            input_path = Path(config.pop("input_path"))
            output_path = Path(config.pop("output_path"))
            if config:
                raise ValidationError(f"interpretation descriptor has unknown fields: {sorted(config)}")
            packet = json.loads(input_path.read_text())
            for binding in stage["bindings"]:
                if binding["target"].startswith("packet."):
                    value = self._source_value(binding["source"])
                    if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                        value = json.loads(Path(value).read_text())
                    _set_path(packet, binding["target"][len("packet."):], value)
            result = ScientificInterpretationRunner(
                json.loads(model_path.read_text()), deadline_seconds=stage_deadline).run(packet)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(canonical_bytes(result))
            result = {"status": "completed", "output_path": str(output_path.resolve()),
                      "interpretation": result}
        elif kind == "argument":
            from scisaurus.runtime.research_argument import ResearchArgumentRunner, argument_evidence_packet
            descriptor_bindings = [item for item in stage["bindings"]
                                   if not item["target"].startswith("packet.")]
            config = self._apply_bindings(config, descriptor_bindings)
            model_path = Path(config.pop("model_config_path"))
            input_path = Path(config.pop("input_path"))
            output_path = Path(config.pop("output_path"))
            if config:
                raise ValidationError(f"argument descriptor has unknown fields: {sorted(config)}")
            packet = json.loads(input_path.read_text())
            for binding in stage["bindings"]:
                if binding["target"].startswith("packet."):
                    value = self._source_value(binding["source"])
                    if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                        value = json.loads(Path(value).read_text())
                    _set_path(packet, binding["target"][len("packet."):], value)
            model = json.loads(model_path.read_text())
            result = ResearchArgumentRunner(model, deadline_seconds=stage_deadline).run(
                argument_evidence_packet(packet), min_figures=2, min_tables=1, min_experiments=2)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(canonical_bytes(result))
            result = {"status": "completed", "output_path": str(output_path.resolve()),
                      "argument_package": result, "argument": result.get("argument")}
        else:  # paper
            from scisaurus.runtime.paper_pipeline import PaperPipelineRunner
            # The paper descriptor names three independently versioned inputs.
            # Bindings addressed as ``packet.*`` or ``paper_config.*`` are
            # applied to those loaded objects; descriptor bindings (for
            # example ``argument_package_path``) are applied before loading.
            descriptor_bindings = [item for item in stage["bindings"]
                                   if not item["target"].startswith(("packet.", "paper_config."))]
            config = self._apply_bindings(config, descriptor_bindings)
            required = {"packet_path", "model_config_path", "paper_config_path", "output_dir", "argument_package_path",
                        "images", "max_review_rounds", "review_deadline_seconds", "pipeline_deadline_seconds",
                        "argument_deadline_seconds", "min_argument_figures", "min_argument_tables", "min_argument_experiments"}
            optional = {"draft_path", "initial_review_package_path", "release_on_review_limit",
                        "review_max_output_tokens", "review_reasoning_effort",
                        "review_call_timeout_seconds", "review_inter_request_interval_seconds",
                        "repair_max_output_tokens", "model_call_timeout_seconds",
                        "review_arbiter_enabled"}
            if set(config) - required - optional or not required.issubset(config):
                raise ValidationError(
                    f"paper descriptor requires {sorted(required)} and permits {sorted(optional)}")
            if "release_on_review_limit" in config and type(config["release_on_review_limit"]) is not bool:
                raise ValidationError("paper descriptor release_on_review_limit must be Boolean")
            if "review_arbiter_enabled" in config and type(config["review_arbiter_enabled"]) is not bool:
                raise ValidationError("paper descriptor review_arbiter_enabled must be Boolean")
            packet = json.loads(Path(config["packet_path"]).read_text())
            model = json.loads(Path(config["model_config_path"]).read_text())
            paper_config = json.loads(Path(config["paper_config_path"]).read_text())
            draft = (json.loads(Path(config["draft_path"]).read_text())
                     if config.get("draft_path") else None)
            initial_review_package = (json.loads(Path(config["initial_review_package_path"]).read_text())
                                     if config.get("initial_review_package_path") else None)
            for binding in stage["bindings"]:
                if binding["target"].startswith("packet."):
                    value = self._source_value(binding["source"])
                    if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                        value = json.loads(Path(value).read_text())
                    _set_path(packet, binding["target"][len("packet."):], value)
                elif binding["target"].startswith("paper_config."):
                    value = self._source_value(binding["source"])
                    if binding["target"] == "paper_config.interpretation_file":
                        value = self._interpretation_binding_path(value)
                    _set_path(paper_config, binding["target"][len("paper_config."):], value)
            argument_package_path = config["argument_package_path"]
            argument_package = json.loads(Path(argument_package_path).read_text()) if argument_package_path else None
            runner = PaperPipelineRunner(packet=packet, model_config=model, paper_config=paper_config,
                output_dir=config["output_dir"], image_paths=config["images"],
                max_review_rounds=config["max_review_rounds"],
                review_deadline_seconds=min(config["review_deadline_seconds"], stage_deadline),
                pipeline_deadline_seconds=min(config["pipeline_deadline_seconds"], stage_deadline),
                argument_deadline_seconds=min(config["argument_deadline_seconds"], stage_deadline),
                review_max_output_tokens=config.get("review_max_output_tokens", 4096),
                review_reasoning_effort=config.get("review_reasoning_effort", "medium"),
                review_call_timeout_seconds=min(config.get("review_call_timeout_seconds", 300.0), stage_deadline),
                review_inter_request_interval_seconds=config.get("review_inter_request_interval_seconds", 0.5),
                repair_max_output_tokens=config.get("repair_max_output_tokens", 6000),
                model_call_timeout_seconds=min(config.get("model_call_timeout_seconds", 300.0), stage_deadline),
                review_arbiter_enabled=bool(config.get("review_arbiter_enabled", True)),
                draft=draft, initial_review_package=initial_review_package,
                release_on_review_limit=bool(config.get("release_on_review_limit", False)),
                initial_argument_package=argument_package,
                min_argument_figures=config["min_argument_figures"], min_argument_tables=config["min_argument_tables"],
                min_argument_experiments=config["min_argument_experiments"],
                feedback_callback=lambda event: self._record_internal_feedback(stage, event))
            result = runner.run()
            output_path = Path(result["pdf"])
        if not isinstance(result, dict):
            raise ValidationError(f"{kind} stage returned a non-object result")
        context = {**result, "stage_id": stage["id"], "kind": kind,
                   "project_dir": str(Path(stage["project_dir"]).resolve()),
                   "output_path": str(output_path.resolve())}
        if kind == "experiment":
            context["results_package"] = result.get("results_package")
        if kind == "argument":
            context["argument_package_path"] = context["output_path"]
        return context

    def _stage_progress(self, stage):
        def callback(state):
            self._checkpoint(f"{stage['id']}:{state.get('phase', 'progress')}")
        return callback

    def _record_feedback(self, stage, context):
        role = STAGE_ROLES[stage["kind"]]
        action = ("advance" if context.get("status") in {"completed", "accepted", "candidate_needs_review"}
                  else "reconcile_blocker")
        if action == "advance":
            consumers = [candidate for candidate in self.workflow["stages"]
                         if stage["id"] in candidate["depends_on"]]
            recipient = (DEPARTMENT_ADDRESSES[STAGE_ROLES[consumers[0]["kind"]]]
                         if consumers else COMMAND_ADDRESSES["intent"])
        else:
            recipient = COMMAND_ADDRESSES["arbiter"]
        feedback = {
            "stage_id": stage["id"], "role": STAGE_ROLES[stage["kind"]],
            "from": COMMAND_ADDRESSES["progress"], "to": recipient,
            "status": context.get("status"), "output_path": context.get("output_path"),
            "scientific_state": context.get("gap_state") or context.get("review_status") or context.get("argument_status"),
            "action": action,
            "stage_deadline_seconds": stage["deadline_seconds"],
            "elapsed_seconds": context.get("elapsed_seconds"),
            "dependencies": list(stage["depends_on"]),
            "next_condition": "all declared dependencies current and stage output independently checked",
        }
        if context.get("status") not in {"completed", "accepted", "candidate_needs_review"}:
            feedback["next_condition"] = "reconcile the stage blocker before downstream admission"
        elif context.get("status") == "candidate_needs_review":
            feedback["next_condition"] = "principal review is required before external submission"
        feedback["message_id"] = f"composer-feedback-{uuid.uuid4().hex}"
        feedback["message_disposition"] = "scheduled" if action == "advance" else "escalated"
        self.feedback.append(feedback)
        note = self._publish(f"command/composer/feedback/{stage['id']}-{len(self.feedback)}", "decision_note", feedback,
                             "command.composer")
        self._route_feedback(feedback, note)

    @staticmethod
    def _feedback_address(event):
        """Choose the receiving organ for an internal specialist exchange."""
        kind = event.get("kind")
        reviewer = event.get("reviewer_id")
        if kind in {"review_failure", "synthesis_failure", "arbitration_failure", "repair_failure"}:
            return COMMAND_ADDRESSES["arbiter"]
        if kind == "review":
            # A blocking critique is a dispute for the Arbiter; ordinary
            # findings remain with the department that owns the surface.
            if (event.get("severity_counts") or {}).get("blocking", 0):
                return COMMAND_ADDRESSES["arbiter"]
            if reviewer == "science":
                return DEPARTMENT_ADDRESSES["strategy.interpretation"]
            if reviewer == "methods":
                return DEPARTMENT_ADDRESSES["methods.validation"]
            if reviewer == "journal_editor":
                # Publication-depth findings are editorial admission
                # decisions.  They must reach the editor-in-chief rather than
                # being treated as an ordinary copy edit.
                return DEPARTMENT_ADDRESSES["editorial.composer"]
            return DEPARTMENT_ADDRESSES["editorial.composer"]
        if kind in {"argument", "draft", "repair"}:
            return DEPARTMENT_ADDRESSES["editorial.composer"]
        if kind == "release":
            return COMMAND_ADDRESSES["intent"]
        if kind == "synthesis":
            return (COMMAND_ADDRESSES["arbiter"]
                    if event.get("decision") == "insufficient_evidence"
                    else DEPARTMENT_ADDRESSES["editorial.composer"])
        return COMMAND_ADDRESSES["arbiter"]

    def _record_internal_feedback(self, stage, event):
        """Route reviewer/editor events through the same Composer desk.

        Stage runners retain write authority over their own artifacts, while
        the Composer owns the organizational exchange. Event IDs are stable
        across checkpoint resume, so replaying a cached review cannot create
        duplicate messages or acknowledgements.
        """
        if not isinstance(event, dict) or not event.get("kind"):
            raise ValidationError("internal composer feedback requires a kind")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            stable = canonical_bytes({key: event[key] for key in sorted(event) if key != "artifact_path"})
            event_id = f"{stage['id']}:{hashlib.sha256(stable).hexdigest()[:24]}"
        if any(item.get("event_id") == event_id for item in self.feedback):
            return
        status = event.get("status", "needs_revision")
        failure = event.get("kind", "").endswith("failure") or status == "blocked"
        recipient = self._feedback_address(event)
        action = ("reconcile_blocker" if failure else
                  "request_revision" if status in {"needs_revision", "revise"} else
                  "verify_repair" if event.get("kind") == "repair" else
                  "propose_release" if event.get("kind") == "release" else
                  "record_observation")
        feedback = {
            "event_id": event_id,
            "stage_id": stage["id"],
            "role": STAGE_ROLES[stage["kind"]],
            "from": {"dept": "specialist-review", "agent": event["kind"]},
            "to": recipient,
            "status": status,
            "action": action,
            "event": deepcopy(event),
            "output_path": event.get("artifact_path") or event.get("pdf_path"),
            "stage_deadline_seconds": stage["deadline_seconds"],
            "dependencies": list(stage["depends_on"]),
            "next_condition": (
                "Arbiter validates the failure and Progress Controller reopens only the affected scope"
                if failure else
                "the receiving department records a scoped response before the next Composer admission"
            ),
            "message_id": "composer-feedback-" + hashlib.sha256(
                f"{self.workflow['id']}:{event_id}".encode("utf-8")).hexdigest()[:32],
            "message_disposition": "escalated" if failure else "scheduled",
        }
        self.feedback.append(feedback)
        note = self._publish(
            f"command/composer/internal/{stage['id']}/{event_id}",
            "decision_note", feedback, "command.composer")
        self._route_feedback(feedback, note)
        self._checkpoint(f"{stage['id']}:feedback:{event['kind']}")

    def _route_feedback(self, feedback, note):
        """Route one durable command decision through the project message bus.

        The note is the immutable decision record; the envelope is the
        asynchronous handoff a department would receive in an ordinary
        organization.  Both carry the same body and reference, so a resumed
        Composer can reconstruct who acted, why, and what must happen next.
        """
        message_type = "decision" if feedback.get("action") == "advance" else "critique"
        envelope = {
            "message_id": feedback["message_id"],
            "project_id": self.workflow["project_id"],
            "type": message_type,
            "from": feedback.get("from", COMMAND_ADDRESSES["progress"]),
            "to": feedback.get("to", COMMAND_ADDRESSES["arbiter"]),
            "subject": f"{feedback.get('action', 'review')}:{feedback.get('stage_id', 'workflow')}",
            "body": json.dumps(feedback, ensure_ascii=False, sort_keys=True),
            "refs": [note["artifact_ref"]],
            "created_at": now_iso(),
            "idempotency_key": f"feedback:{self.workflow['id']}:{feedback.get('event_id') or feedback.get('message_id')}",
        }
        message_id = self.messages.publish(envelope)
        # The recipient's department chief owns receipt.  Acknowledging the
        # envelope here is the bounded command-desk handoff: the specialist
        # stage itself still has to satisfy its own acceptance contract before
        # the next dependency is admitted.  If receipt cannot be committed,
        # surface that control-plane failure instead of presenting a false
        # downstream decision.
        owner = f"{feedback['to']['dept']}.{feedback['to']['agent']}"
        # Receipt is a local control-plane operation and must remain durable
        # even when the stage has just crossed its execution deadline.  The
        # deadline controls new scientific work; it does not erase the audit
        # trail for a failed handoff.
        fence = self.messages.lease(message_id, owner=owner, ttl_seconds=30.0)
        self.messages.acknowledge(message_id, fence, feedback["message_disposition"], actor=owner)
        if message_id != feedback["message_id"]:
            raise ValidationError("message bus returned a different feedback message ID")

    def _record_blocker_feedback(self, stage, error):
        feedback = {
            "stage_id": stage["id"], "role": STAGE_ROLES[stage["kind"]],
            "from": COMMAND_ADDRESSES["progress"], "to": COMMAND_ADDRESSES["arbiter"],
            "status": "blocked", "action": "reconcile_blocker",
            "error": str(error), "stage_deadline_seconds": stage["deadline_seconds"],
            "dependencies": list(stage["depends_on"]),
            "next_condition": "Arbiter validates the blocker and Progress Controller reopens only its affected scope",
        }
        feedback["message_id"] = f"composer-feedback-{uuid.uuid4().hex}"
        feedback["message_disposition"] = "escalated"
        self.feedback.append(feedback)
        note = self._publish(f"command/composer/feedback/{stage['id']}-{len(self.feedback)}", "decision_note", feedback,
                             "command.composer")
        self._route_feedback(feedback, note)

    def run(self):
        try:
            by_id = {stage["id"]: stage for stage in self.workflow["stages"]}
            completed = {stage_id for stage_id, row in self.stage_records.items()
                         if row.get("status") in {"completed", "accepted", "candidate_needs_review"}}
            while len(completed) < len(self.workflow["completion"]["required_stage_ids"]):
                self._remaining()
                progress = False
                for stage in self.workflow["stages"]:
                    stage_id = stage["id"]
                    if stage_id in completed:
                        continue
                    if not set(stage["depends_on"]).issubset(completed):
                        continue
                    # The estimate is a planning reservation, not a promise;
                    # never admit discretionary work that cannot fit the hard
                    # window alongside the required downstream closure.
                    remaining = self._remaining()
                    downstream_ids = set()
                    frontier = [stage_id]
                    while frontier:
                        current = frontier.pop()
                        if current in downstream_ids:
                            continue
                        downstream_ids.add(current)
                        frontier.extend(item for item, candidate in by_id.items()
                                        if current in candidate["depends_on"])
                    downstream = sum(float(by_id[item]["estimate_seconds"]) for item in downstream_ids)
                    if remaining < min(float(stage["estimate_seconds"]), downstream):
                        self.status = "paused"
                        self.blockers.append({"stage_id": stage_id, "reason": "stage estimate does not fit remaining hard window"})
                        self._checkpoint("paused_deadline", force=True)
                        return self._finish()
                    task = self._stage_task(stage)
                    task_id = task["task_id"]
                    attempt_id = f"{task_id}-{uuid.uuid4().hex}"
                    self.tasks.start_attempt(task_id, attempt_id, owner="command.composer",
                                             lease_ttl_seconds=max(1.0, remaining),
                                             payload={"stage_id": stage_id, "kind": stage["kind"]})
                    self.stage_records[stage_id] = {"kind": stage["kind"], "status": "running",
                                                    "attempt_id": attempt_id, "started_elapsed": self.clock() - self.started}
                    self._checkpoint(f"{stage_id}:admitted", force=True)
                    stop_live_progress = self._start_live_progress(stage)
                    try:
                        context = self._run_stage(stage)
                        outcome = context.get("status")
                        if outcome not in {"completed", "accepted", "candidate_needs_review"}:
                            raise ValidationError(f"stage {stage_id} did not complete: {outcome}")
                        for key in self.usage:
                            value = context.get("usage", {}).get(key, 0)
                            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                                self.usage[key] += value
                        self.tasks.finish_attempt(attempt_id, "succeeded", usage=context.get("usage", {}))
                        self.tasks.transition(task_id, "awaiting_review", "command.composer", reason="stage output returned")
                        self.tasks.transition(task_id, "completed", "command.composer", reason="stage-specific checks passed")
                        context["stage_id"] = stage_id
                        self.context[stage_id] = context
                        self.stage_records[stage_id] = {"kind": stage["kind"],
                                                        "status": ("candidate_needs_review"
                                                                   if outcome == "candidate_needs_review"
                                                                   else "completed"),
                                                        "attempt_id": attempt_id, "output_path": context.get("output_path"),
                                                        "elapsed_seconds": self.clock() - self.started}
                        completed.add(stage_id)
                        self._record_feedback(stage, context)
                        self._checkpoint(f"{stage_id}:completed", force=True)
                        progress = True
                        break
                    except Exception as exc:
                        try:
                            self.tasks.finish_attempt(attempt_id, "failed", usage={})
                            self.tasks.transition(task_id, "blocked", "command.composer", reason=str(exc))
                        except Exception:
                            pass
                        self.stage_records[stage_id] = {"kind": stage["kind"], "status": "blocked",
                                                        "attempt_id": attempt_id, "error": f"{type(exc).__name__}: {exc}"}
                        self.blockers.append({"stage_id": stage_id, "reason": str(exc)})
                        self._record_blocker_feedback(stage, exc)
                        self.status = "blocked"
                        self._checkpoint(f"{stage_id}:blocked", force=True)
                        return self._finish()
                    finally:
                        stop_live_progress()
                if not progress:
                    self.status = "blocked"
                    self.blockers.append({"reason": "composer made no dependency-respecting progress"})
                    self._checkpoint("blocked_no_progress", force=True)
                    return self._finish()
            # A stage can finish its bounded execution while its artifact is
            # still a candidate.  Preserve that distinction at the workflow
            # level so the CLI and any scheduler cannot mistake a candidate
            # for a completed release.
            candidate_stage = any(
                row.get("status") == "candidate_needs_review"
                or self.context.get(stage_id, {}).get("status") == "candidate_needs_review"
                for stage_id, row in self.stage_records.items())
            self.status = "candidate_needs_review" if candidate_stage else "completed"
            self._checkpoint("complete_proposed", force=True)
            return self._finish()
        except Exception as exc:
            self.status = "blocked"
            self.blockers.append({"reason": f"{type(exc).__name__}: {exc}"})
            self._checkpoint("blocked", force=True)
            return self._finish()
        finally:
            self.close()

    def _finish(self):
        elapsed = max(0.0, self.clock() - self.started)
        result = {
            "schema_version": RUN_SCHEMA_VERSION, "run_id": self.run_id, "workflow_id": self.workflow["id"],
            "status": self.status, "stages": deepcopy(self.stage_records), "context": deepcopy(self.context),
            "feedback": deepcopy(self.feedback), "blockers": deepcopy(self.blockers), "usage": deepcopy(self.usage),
            "elapsed_seconds": elapsed, "deadline_seconds": self.workflow["time_policy"]["hard_seconds"],
            "release_status": (
                "candidate_needs_review" if self.status == "candidate_needs_review"
                else "needs_human_approval" if self.status == "completed"
                and self.workflow["completion"]["release_requires_human"]
                else "not_released"
            ),
            "event_chain": self.control.verify_chain() if self.control is not None else None,
        }
        if self.control is not None:
            self._publish("command/composer/run", "report", result, "command.composer")
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "run.json").write_bytes(canonical_bytes(result))
            self.on_progress({"phase": self.status, "elapsed_seconds": round(elapsed, 2),
                              "stages": deepcopy(self.stage_records), "blockers": deepcopy(self.blockers)})
        return result


__all__ = ["SCHEMA_VERSION", "RUN_SCHEMA_VERSION", "validate_workflow", "ComposerRunner"]
