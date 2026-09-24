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
import itertools
import json
import math
import os
import platform
from pathlib import Path
import re
import secrets
import shutil
import sys
import threading
import time
import uuid

from scisaurus.core.errors import (
    NotFoundError, ProviderConfigurationError, QuotaExceededError, StateError,
    ValidationError,
)
from scisaurus.core.events import ControlStore
from scisaurus.core.messages import MessageBus
from scisaurus.core.schema import canonical_bytes, now_iso
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager
from scisaurus.runtime.literature import (
    OpenAlexClient, ProviderCooldownError, preferred_full_text_url,
)
from scisaurus.runtime.departments import (
    COMMAND_ADDRESSES,
    DEFAULT_STAGE_ROUTES,
    stage_role,
)
from scisaurus.runtime.specialists import (
    SPECIALIST_SYSTEM, VERIFIER_SYSTEM, SpecialistDispatcher,
    build_specialist_prompt, build_verifier_prompt,
)
from scisaurus.runtime.model_work import ModelWorkBlocked, ModelWorkCache
from scisaurus.runtime.models import ModelCallError, ModelContextBudgetError
from scisaurus.runtime.failure_recovery import (
    build_failure_dossier, build_repair_commands, build_repair_request,
    classify_failure,
)
from scisaurus.runtime.topic_discovery import (
    DEFAULT_TOPIC_BUDGETS,
    DEFAULT_TOPIC_CONTINUATION_BUDGETS,
    EVIDENCE_MODE_VALUES,
    topic_salvage_plan,
    _topic_validation_rejection_type,
)


SCHEMA_VERSION = "composer-workflow-1"
RUN_SCHEMA_VERSION = "composer-run-1"
STAGE_KINDS = frozenset({"topic_discovery", "survey", "experiment", "interpretation", "argument", "paper"})
# These are the only stage outcomes that satisfy a downstream dependency in
# strict policies. ``research_expansion_required`` and ``review_rejected`` are
# terminal for the current attempt, but ``forward_first`` projects an admitted
# hold to ``candidate_needs_review`` with explicit unresolved findings.
STAGE_READY_STATUSES = frozenset({"completed", "accepted", "candidate_needs_review"})
STAGE_HOLD_STATUSES = frozenset({"research_expansion_required", "review_rejected"})
STAGE_QUOTA_FIELDS = frozenset({
    "max_model_calls", "max_input_tokens", "max_output_tokens", "max_openalex_requests",
})
# Compatibility projection for callers that imported the old Composer map.
# Ownership is defined in runtime.departments.DEFAULT_STAGE_ROUTES.
STAGE_ROLES = {route["stage_kind"]: stage_role(route["stage_kind"])
               for route in DEFAULT_STAGE_ROUTES}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
# Legacy execution remains deadline-governed for compatibility: a transient
# model, provider, or validation failure is not stopped by an arbitrary
# counter. New autonomous-lab workflows should opt into ``forward_first``;
# that policy supplies bounded repair and backfill paths while keeping
# unexecuted capabilities out of downstream evidence.
DEFAULT_RETRY_POLICY = {"mode": "until_deadline", "max_attempts": None, "backoff_seconds": 2.0}
# Research re-entry is also deadline-governed by default.  A deterministic
# workflow may opt into a finite cycle budget explicitly; the manuscript's
# independent three-round peer review remains a separate contract.
DEFAULT_CONTINUATION_POLICY = {"mode": "until_deadline", "max_cycles": None}
# ``forward_first`` is the autonomous-lab policy. It keeps the older strict
# policies available for exact/release-oriented workflows, but gives a new
# mission bounded repair and durable provisional paths instead of allowing a
# deterministic harness check to veto an already admitted, evidence-bearing
# stage. An unexecuted capability still requires source-level repair first.
# A provisional handoff is dependency-releasable; it is never a claim that
# the scientific result exists or passed review.
FORWARD_FIRST_POLICY = "forward_first"
FORWARD_FIRST_RETRY_POLICY = {
    "mode": "bounded", "max_attempts": 2, "backoff_seconds": 2.0,
}
FORWARD_FIRST_CONTINUATION_POLICY = {
    "mode": "bounded", "max_cycles": 2,
}
# A capability-authoring rejection is not yet a reason to discard a
# literature-backed question.  This is the maximum number of source-level
# edits attempted for one capability lineage before Composer changes the
# experimental design axis.  The mission can continue after that pivot while
# its stage quota and hard deadline remain the resource fences.
PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT = 3
EXPERIMENT_REPAIR_AXES = (
    {
        "id": "mechanism",
        "instruction": (
            "change the executable mechanism or state evolution that produced the failure; "
            "a renamed threshold or relabelled output is not a repair"
        ),
        "evidence": "the intervention changes the simulated dynamics and leaves a trace in raw observations",
    },
    {
        "id": "estimand",
        "instruction": (
            "restate and implement the primary estimand so the executor and independent validator "
            "use the same declared convention without silently substituting a boundary or zero"
        ),
        "evidence": "every primary outcome is independently recalculated from raw observations under the declared convention",
    },
    {
        "id": "design",
        "instruction": (
            "change the control, baseline, or parameter grid so the competing explanations make "
            "different predictions in an observable regime"
        ),
        "evidence": "the new design contains an informative contrast rather than an all-censored or degenerate grid",
    },
    {
        "id": "measurement",
        "instruction": (
            "replace the failed observable with a measurable proxy that remains faithful to the "
            "research question and specify its limitations"
        ),
        "evidence": "the proxy is finite, non-degenerate, intervention-sensitive, and independently validated",
    },
)
# Argument adjudication can identify an evidence-producing repair that the
# argument writer cannot perform by itself.  These markers are intentionally
# narrow: a prose-only scope downgrade stays in Strategy, while a request for
# a new observation, recalculation, calibration, or sensitivity result reopens
# the existing experiment stage first.
ARGUMENT_EXPERIMENT_REPAIR_MARKERS = (
    "additional experiment", "run a", "raw data", "recalculat", "sensitivity",
    "sweep", "calibrat", "per-velocity", "parameter isolation", "threshold",
    "dispersion", "critical length", "independent validation", "new result",
    "control", "measurement", "observed value",
)
# Capability authoring has its own bounded lease inside an experiment stage.
# One candidate can consume one author call plus two independent review calls;
# twelve calls therefore allow several materially different candidates while
# leaving the stage's verifier and downstream accounting room.
AUTONOMOUS_FOUNDRY_MODEL_CALL_LIMIT = 12
FOUNDRY_VERIFIER_MODEL_CALL_RESERVE = 2
# A pre-execution capability repair panel consists of four methods roles and
# one independent adversary. Each assignment has one bounded JSON repair slot,
# so reserve the worst-case envelope before the foundry author is admitted;
# otherwise the panel can consume the stage quota and the controller will
# discover the overrun only after dispatching it.
CAPABILITY_REPAIR_PANEL_MODEL_CALL_RESERVE = 10
# Workflows created before agenda selection existed retain declaration-order
# semantics. New autonomous-lab workflows opt into adaptive selection
# explicitly, so replaying an immutable legacy graph never changes its meaning.
DEFAULT_AGENDA_POLICY = {"mode": "ordered"}


class ComposerHardDeadlineExceeded(ValidationError):
    """The immutable mission wall was reached during control-plane work."""


class ComposerLateStageResult(ValidationError):
    """A stage returned an artifact only after the immutable mission wall."""


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def load_runtime_environment_files(configured):
    """Load owner-local env files into this process without persisting secrets."""
    configured = list(configured or [])
    if not configured:
        return
    loaded = {}
    visited = set()

    def parse(path):
        values = {}
        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeError) as exc:
            raise ValidationError(f"runtime environment file is unreadable: {path}") from exc
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
        return values

    def resolve_reference(path, reference):
        candidate = Path(reference)
        if candidate.is_absolute():
            candidates = [candidate]
        else:
            repo_root = Path(__file__).resolve().parents[2]
            candidates = [
                path.parent / candidate,
                path.parent.parent / candidate,
                Path.cwd() / candidate,
                repo_root / candidate,
            ]
        for item in candidates:
            try:
                resolved = item.resolve()
            except OSError:
                continue
            if resolved.is_file():
                return resolved
        raise ValidationError(
            f"runtime environment reference is unavailable: {reference}")

    def visit(path):
        path = path.resolve()
        if path in visited:
            return
        visited.add(path)
        values = parse(path)
        loaded.update(values)
        nested = values.get("SCISAURUS_QWEN_ENV_FILE")
        if nested:
            visit(resolve_reference(path, nested))

    for configured_path in configured:
        path = Path(configured_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise ValidationError(f"runtime environment file is unavailable: {path}")
        visit(path)
    for key, value in loaded.items():
        os.environ.setdefault(key, value)


def default_runtime_environment_files(repo_root=None):
    """Return the owner-local env files used by a checked-out Sci-saurus run.

    Older workflow descriptors predate ``runtime_env_files`` and therefore
    cannot name the credential file without being regenerated.  The launcher
    still has a stable, repository-owned location for those files.  Only
    existing files are returned; no environment variable is guessed and no
    secret is copied into a workflow artifact.
    """
    root = (Path(repo_root).resolve() if repo_root is not None
            else Path(__file__).resolve().parents[2])
    candidates = (
        root / "local-private" / "openalex.env",
        root / "local-private" / "ollama-cloud.env",
    )
    return [str(path) for path in candidates if path.is_file()]


def default_openalex_shared_state_path(repo_root=None):
    """Return the repository-owned OpenAlex account ledger path."""
    root = (Path(repo_root).resolve() if repo_root is not None
            else Path(__file__).resolve().parents[2])
    return root / "local-private" / "provider-state" / "openalex-rate-state.json"


def default_openalex_shared_topic_cache_path(repo_root=None):
    """Return the repository-owned cross-mission topic cache path."""
    root = (Path(repo_root).resolve() if repo_root is not None
            else Path(__file__).resolve().parents[2])
    return root / "local-private" / "provider-cache" / "topic-openalex.json"


def _identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _positive_number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{name} must be finite and positive")


def _validate_topic_preferences(value):
    """Validate optional scientific topic-selection preferences.

    Preferences narrow the search objective without replacing the evidence,
    maturity, or feasibility gates applied to the selected topic.
    """
    allowed = {"mode", "must_have", "avoid"}
    if (not isinstance(value, dict) or "mode" not in value
            or set(value) - allowed):
        raise ValidationError(
            "workflow topic_preferences requires mode and permits must_have and avoid")
    if value["mode"] not in {"general", "computational_native"}:
        raise ValidationError(
            "workflow topic_preferences.mode must be general or computational_native")
    for field in ("must_have", "avoid"):
        items = value.get(field, [])
        if (not isinstance(items, list) or len(items) > 12
                or any(not isinstance(item, str) or not item.strip() or len(item) > 240
                       for item in items)):
            raise ValidationError(
                f"workflow topic_preferences.{field} must be a list of at most twelve nonempty strings of 240 characters or fewer")


def validate_workflow(value):
    """Validate the immutable composer workflow contract."""
    fields = {"schema_version", "id", "revision", "project_id", "objective", "stages", "time_policy", "completion"}
    allowed_fields = fields | {"retry_policy", "continuation_policy", "agenda_policy",
                               "organization", "exploration_seed",
                               "topic_reuse_allowed", "experiment_catalog", "topic_exclusions",
                               "topic_history_path", "capability_foundry_config_path",
                               "topic_preferences", "runtime_env_files", "progression_policy"}
    if not isinstance(value, dict) or set(value) - allowed_fields or not fields.issubset(value):
        raise ValidationError(
            f"composer workflow requires {sorted(fields)} and permits ['agenda_policy', 'capability_foundry_config_path', 'continuation_policy', 'exploration_seed', 'experiment_catalog', 'organization', 'progression_policy', 'retry_policy', 'runtime_env_files', 'topic_exclusions', 'topic_history_path', 'topic_preferences', 'topic_reuse_allowed']")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError(f"composer workflow schema must be {SCHEMA_VERSION}")
    _identifier(value["id"], "workflow id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("workflow revision must be a positive integer")
    _text(value["project_id"], "workflow project_id")
    _text(value["objective"], "workflow objective")
    if "exploration_seed" in value and (
            type(value["exploration_seed"]) is not int or value["exploration_seed"] < 0):
        raise ValidationError("workflow exploration_seed must be a non-negative integer when configured")
    if "topic_reuse_allowed" in value and type(value["topic_reuse_allowed"]) is not bool:
        raise ValidationError("workflow topic_reuse_allowed must be Boolean when configured")
    if "topic_preferences" in value:
        _validate_topic_preferences(value["topic_preferences"])
    agenda_policy = value.get("agenda_policy")
    if "progression_policy" in value and (not isinstance(value["progression_policy"], str)
            or value["progression_policy"] not in {
                "evidence_first", "full_pass", FORWARD_FIRST_POLICY,
            }):
        raise ValidationError(
            "progression_policy must be evidence_first, full_pass, or forward_first")
    if agenda_policy is not None:
        if (not isinstance(agenda_policy, dict)
                or set(agenda_policy) != {"mode"}
                or agenda_policy.get("mode") not in {"adaptive", "ordered"}):
            raise ValidationError(
                "workflow agenda_policy requires exactly mode=adaptive or mode=ordered")
    if "topic_history_path" in value:
        history_path = value["topic_history_path"]
        if not isinstance(history_path, str) or not history_path.strip():
            raise ValidationError("workflow topic_history_path must be a nonempty absolute path")
        if not Path(history_path).is_absolute():
            raise ValidationError("workflow topic_history_path must be an absolute path")
        if Path(history_path).exists() and not Path(history_path).is_file():
            raise ValidationError("workflow topic_history_path must name a file")
    if "runtime_env_files" in value:
        env_files = value["runtime_env_files"]
        if (not isinstance(env_files, list) or not env_files
                or len(env_files) > 8
                or any(not isinstance(item, str) or not Path(item).is_absolute()
                       or not Path(item).is_file() for item in env_files)):
            raise ValidationError(
                "workflow runtime_env_files must contain one to eight existing absolute files")
    if "capability_foundry_config_path" in value:
        foundry_path = value["capability_foundry_config_path"]
        if (not isinstance(foundry_path, str) or not Path(foundry_path).is_absolute()
                or not Path(foundry_path).is_file()):
            raise ValidationError(
                "workflow capability_foundry_config_path must be an existing absolute file")
        from scisaurus.runtime.capability_foundry import validate_foundry_config
        try:
            validate_foundry_config(json.loads(Path(foundry_path).read_text()))
        except (OSError, ValueError, TypeError) as exc:
            raise ValidationError("workflow capability foundry config is unreadable") from exc
    if "topic_exclusions" in value:
        exclusions = value["topic_exclusions"]
        if (not isinstance(exclusions, dict)
                or set(exclusions) != {"capability_ids", "topic_ids"}
                or not isinstance(exclusions["capability_ids"], list)
                or not isinstance(exclusions["topic_ids"], list)):
            raise ValidationError("workflow topic_exclusions requires capability_ids and topic_ids lists")
        for name in ("capability_ids", "topic_ids"):
            if len(exclusions[name]) != len(set(exclusions[name])):
                raise ValidationError(f"workflow topic_exclusions.{name} must be unique")
            for item in exclusions[name]:
                _identifier(item, f"workflow topic exclusion {name} entry")
    if "experiment_catalog" in value:
        catalog = value["experiment_catalog"]
        if (not isinstance(catalog, list) or not catalog
                or len(catalog) > 16):
            raise ValidationError("workflow experiment_catalog must contain one to sixteen entries")
        catalog_ids = set()
        for entry in catalog:
            if not isinstance(entry, dict) or set(entry) != {"id", "config_path"}:
                raise ValidationError("workflow experiment_catalog entries require exactly id and config_path")
            entry_id = _identifier(entry["id"], "experiment catalog id")
            if entry_id in catalog_ids:
                raise ValidationError("workflow experiment_catalog IDs must be unique")
            catalog_ids.add(entry_id)
            path = Path(entry["config_path"])
            if not path.is_absolute() or not path.is_file():
                raise ValidationError("experiment catalog config_path must be an existing absolute file")
    stages = value["stages"]
    if not isinstance(stages, list) or not stages:
        raise ValidationError("composer workflow requires at least one stage")
    stage_ids, outputs = set(), set()
    stage_fields = {"id", "kind", "config_path", "project_dir", "depends_on", "estimate_seconds", "bindings",
                    "deadline_seconds", "reuse_completed", "reuse_output_path"}
    allowed_stage_fields = stage_fields | {"quota"}
    for stage in stages:
        if not isinstance(stage, dict) or not stage_fields.issubset(stage) or set(stage) - allowed_stage_fields:
            raise ValidationError(
                f"composer stage requires {sorted(stage_fields)} and permits quota")
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
        if "quota" in stage:
            quota = stage["quota"]
            if not isinstance(quota, dict) or set(quota) != STAGE_QUOTA_FIELDS:
                raise ValidationError(
                    f"stage {stage_id} quota requires exactly {sorted(STAGE_QUOTA_FIELDS)}")
            if (type(quota["max_model_calls"]) is not int
                    or quota["max_model_calls"] < 1):
                raise ValidationError(f"stage {stage_id} quota max_model_calls must be a positive integer")
            for field in ("max_input_tokens", "max_output_tokens"):
                if type(quota[field]) is not int or quota[field] < 1:
                    raise ValidationError(f"stage {stage_id} quota {field} must be a positive integer")
            if (type(quota["max_openalex_requests"]) is not int
                    or quota["max_openalex_requests"] < 0):
                raise ValidationError(
                    f"stage {stage_id} quota max_openalex_requests must be a nonnegative integer")
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

    ancestor_cache = {}

    def ancestors(stage_id):
        if stage_id not in ancestor_cache:
            found = set()
            pending = list(by_id[stage_id]["depends_on"])
            while pending:
                current = pending.pop()
                if current in found:
                    continue
                found.add(current)
                pending.extend(by_id[current]["depends_on"])
            ancestor_cache[stage_id] = found
        return ancestor_cache[stage_id]

    # A topic admitted only for evidence gathering must never flow directly
    # into execution. Any experiment downstream of topic discovery therefore
    # needs a survey ancestor that is itself downstream of that topic. Runtime
    # admission additionally checks the survey's actual gap verdict.
    for stage in stages:
        if stage["kind"] != "experiment":
            continue
        experiment_ancestors = ancestors(stage["id"])
        topic_ancestors = {
            stage_id for stage_id in experiment_ancestors
            if by_id[stage_id]["kind"] == "topic_discovery"
        }
        for topic_id in topic_ancestors:
            has_literature_gate = any(
                by_id[stage_id]["kind"] == "survey"
                and topic_id in ancestors(stage_id)
                for stage_id in experiment_ancestors
            )
            if not has_literature_gate:
                raise ValidationError(
                    f"experiment stage {stage['id']} requires a survey between "
                    f"topic stage {topic_id} and experiment admission")
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
    retry_policy = value.get("retry_policy")
    if retry_policy is not None:
        retry_fields = {"mode", "max_attempts", "backoff_seconds"}
        if not isinstance(retry_policy, dict) or set(retry_policy) - retry_fields \
                or "backoff_seconds" not in retry_policy:
            raise ValidationError(f"retry_policy requires backoff_seconds and permits {sorted(retry_fields - {'backoff_seconds'})}")
        mode = retry_policy.get("mode", "bounded")
        if mode not in {"bounded", "until_deadline"}:
            raise ValidationError("retry_policy.mode must be bounded or until_deadline")
        attempts = retry_policy.get("max_attempts")
        if mode == "bounded":
            if type(attempts) is not int or not 1 <= attempts <= 8:
                raise ValidationError("retry_policy.max_attempts must be an integer between one and eight for bounded mode")
        elif attempts is not None and (type(attempts) is not int or attempts < 1):
            raise ValidationError("retry_policy.max_attempts may be omitted or a positive integer in until_deadline mode")
        if (type(retry_policy["backoff_seconds"]) not in (int, float)
                or not math.isfinite(retry_policy["backoff_seconds"])
                or retry_policy["backoff_seconds"] < 0):
                raise ValidationError("retry_policy.backoff_seconds must be finite and non-negative")
    continuation_policy = value.get("continuation_policy")
    if continuation_policy is not None:
        continuation_fields = {"mode", "max_cycles"}
        if (not isinstance(continuation_policy, dict)
                or set(continuation_policy) - continuation_fields
                or not continuation_policy):
            raise ValidationError("continuation_policy permits mode and max_cycles")
        mode = continuation_policy.get("mode", "bounded")
        if mode not in {"bounded", "until_deadline"}:
            raise ValidationError("continuation_policy.mode must be bounded or until_deadline")
        cycles = continuation_policy.get("max_cycles")
        if mode == "bounded":
            if type(cycles) is not int or not 0 <= cycles <= 8:
                raise ValidationError("continuation_policy.max_cycles must be an integer between zero and eight for bounded mode")
        elif cycles is not None and (type(cycles) is not int or cycles < 0):
            raise ValidationError("continuation_policy.max_cycles may be omitted or a non-negative integer in until_deadline mode")
    organization = value.get("organization")
    if organization is not None:
        from scisaurus.runtime.departments import validate_organization
        validated_organization = validate_organization(organization)
        configured_departments = {item["id"] for item in validated_organization["departments"]}
        required_departments = {stage_role(stage["kind"]).split(".", 1)[0] for stage in stages}
        if required_departments - configured_departments:
            raise ValidationError(
                "project organization is missing stage-owning departments: "
                + ", ".join(sorted(required_departments - configured_departments)))
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

    def __init__(self, workflow, *, resume=False, clock=time.monotonic, on_progress=None,
                 additional_seconds=None):
        self.workflow = deepcopy(validate_workflow(workflow))
        self._runtime_environment_before = {
            key: value for key, value in os.environ.items()
            if key.startswith("SCISAURUS_")
        }
        self._runtime_environment_after = None
        self._load_runtime_environment()
        self._runtime_environment_after = {
            key: value for key, value in os.environ.items()
            if key.startswith("SCISAURUS_")
        }
        if additional_seconds is not None and not resume:
            raise ValidationError("additional_seconds is only valid when resuming a Composer project")
        self.root = Path(self.workflow["project_id"]).resolve()
        # project_id is the stable identity; the workflow's project directory
        # is derived from it so a config cannot redirect the control ledger.
        self.root.mkdir(parents=True, exist_ok=True)
        self.topic_history_path = self._resolve_topic_history_path()
        self.topic_history_scope = self._topic_history_scope_key()
        self._migrate_legacy_topic_history()
        self.topic_history = self._load_topic_history()
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
        from scisaurus.runtime.departments import DepartmentRuntime
        self.departments = DepartmentRuntime(
            self.control, self.store, self.messages, self.tasks,
            project_id=self.workflow["project_id"], organization=self.workflow.get("organization"),
        )
        if resume:
            # Specialist attempts have their own leases in addition to the
            # Composer stage attempt.  Reconcile those durable child leases
            # before restoring the checkpoint so a restarted run cannot
            # silently reuse an in-flight external call.
            self.departments.reconcile_interrupted_assignments()
        # The live progress ticker runs in a background thread while the
        # project ledger is deliberately bound to the Composer thread.  Keep a
        # plain-data snapshot for that ticker; SQLite connections are not
        # shared across threads.
        self.organization_snapshot = deepcopy(self.departments.snapshot())
        self.clock = clock
        self.on_progress = on_progress or (lambda state: None)
        self._progress_lock = threading.Lock()
        self._live_progress_epoch = 0
        self.started = self.clock()
        # ``monotonic`` is the right clock while this process is alive, but it
        # cannot survive a pause/restart.  Keep a wall-clock fence alongside
        # it so ``--resume`` cannot silently grant the mission another hard
        # window.  The monotonic deadline remains the fast local check.
        self.started_epoch = time.time()
        self.deadline_epoch = self.started_epoch + float(self.workflow["time_policy"]["hard_seconds"])
        policy = self.workflow["time_policy"]
        self.deadline = self.started + float(policy["hard_seconds"])
        self.next_checkpoint = self.started
        self.run_id = uuid.uuid4().hex
        # A fresh mission receives one entropy-backed exploration seed.  It is
        # persisted in the run input/checkpoints so retries and resumes repeat
        # the same proposal, while two independently created missions explore
        # different literature samples and topic generations.  A workflow can
        # pin the seed when exact replay is desired.
        configured_seed = self.workflow.get("exploration_seed")
        self.exploration_seed = (configured_seed if configured_seed is not None
                                 else secrets.randbits(63))
        self.context = {}
        self.stage_records = {}
        self.feedback = []
        self.blockers = []
        self.usage = {
            "model_calls": 0, "input_tokens": 0, "output_tokens": 0,
            "openalex_requests": 0,
        }
        self.foundry_usage = {}
        self.continuation_cycles = 0
        # A bounded continuation policy is a lease for one Composer
        # invocation.  A resumed legacy checkpoint may already contain many
        # historical cycles; charging those cycles against the new lease
        # would make the first recoverable stage quota stop before any fresh
        # work is dispatched.  The baseline is reset only when a process
        # explicitly resumes an existing checkpoint, while the durable
        # historical counter remains unchanged for auditability.
        self._continuation_budget_baseline = 0
        self.reopened_stage_ids = set()
        self.continuation_pending_stage_ids = set()
        self.active_research_requests = []
        self.department_activity = []
        self.deadline_extensions = []
        self.deadline_decisions = []
        self.agenda_decisions = []
        self.retry_schedule = {}
        self.state_revision = 0
        self._restored_agenda_policy = None
        # A hold can echo the same work order in every returned stage packet.
        # Suppress that exact request for the current Composer invocation after
        # its owning stage has been attempted.  The set is intentionally
        # process-local: an explicit resume creates a fresh attempt and may
        # retry the unresolved request after the operator grants more time.
        self._attempted_request_signatures = set()
        self.status = "running"
        self._progress_snapshot = {
            "schema_version": "composer-checkpoint-1", "workflow_id": self.workflow["id"],
            "run_id": self.run_id,
            "status": self.status, "phase": "initialized", "elapsed_seconds": 0.0,
            "remaining_seconds": max(0.0, float(self.workflow["time_policy"]["hard_seconds"])),
            "started_at_epoch": self.started_epoch, "deadline_at_epoch": self.deadline_epoch,
            "exploration_seed": self.exploration_seed,
            "retry_policy": self._retry_policy(),
            "continuation_policy": self._continuation_policy(),
            "agenda_policy": self._agenda_policy(),
            "agenda_decisions": [],
            "retry_schedule": {},
            "state_revision": self.state_revision,
            "continuation_cycles": 0, "reopened_stage_ids": [],
            "continuation_budget_baseline": 0,
            "continuation_budget_used": 0,
            "continuation_pending_stage_ids": [], "active_research_requests": [],
            "department_activity": [], "stages": {}, "context": {}, "feedback": [],
            "blockers": [], "active_blockers": [],
            "blocker_counts": {"active": 0, "historical": 0},
            "usage": deepcopy(self.usage), "foundry_usage": {}, "deadline_decisions": [],
            "deadline_extensions": [],
            "organization": deepcopy(self.organization_snapshot),
            "topic_history_path": str(self.topic_history_path),
            "topic_history_scope": self.topic_history_scope,
            "topic_history_entries": len(self.topic_history.get("entries", [])),
        }
        self._workflow_record = None
        if not existing:
            self._workflow_record = self._publish("command/composer/workflow", "note", self.workflow, "command.composer")
            self._publish("inputs/composer-run", "note", {
                "schema_version": "composer-run-input-1", "workflow_ref": self._workflow_record["artifact_ref"],
                "run_id": self.run_id, "resume": False,
                "exploration_seed": self.exploration_seed,
                "agenda_policy": self._agenda_policy(),
            }, "command.composer")
        else:
            head = self.store.head("command/composer/workflow")
            if head is None or json.loads(self.store.read_body(head["body_hash"])) != self.workflow:
                raise ValidationError("composer resume workflow does not match the original immutable workflow")
            self._restore()
            self._continuation_budget_baseline = max(
                0, int(self.continuation_cycles or 0))
            self.active_research_requests = self._scope_active_research_requests(
                self.active_research_requests)
            # A stopped process can leave older generations of department
            # work orders in ``running`` even after the Composer checkpoint
            # has advanced to a newer continuation.  Fence those rows during
            # resume, before the dashboard or scheduler treats them as live.
            # Keep every request in the restored continuation: requests may
            # belong to a later stage and remain intentionally open until
            # that stage gets its turn.
            active_request_ids = {
                item.get("id") for item in self.active_research_requests
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            retired_work_orders = self.departments.retire_superseded_work_orders(
                active_request_ids,
                reason="superseded work order fenced during Composer resume",
            )
            if retired_work_orders:
                self.organization_snapshot = deepcopy(self.departments.snapshot())
                self.department_activity.append({
                    "cycle": self.continuation_cycles,
                    "action": "retire_superseded_work_orders_on_resume",
                    "work_orders": retired_work_orders,
                })
            if self._sync_foundry_usage():
                self._checkpoint("recovered_usage", force=True)
        if additional_seconds is not None:
            self._extend_deadline(additional_seconds)

    def close(self):
        if self.control is not None:
            self.control.close()
            self.control = None
        # Loading owner-local env files is process-local setup, not a durable
        # mutation of the caller's shell. Restore only values that are still
        # exactly the values this runner installed; a child test or caller
        # that deliberately changed a variable while the runner was alive is
        # left untouched.
        before = self._runtime_environment_before or {}
        after = self._runtime_environment_after or {}
        for key, loaded_value in after.items():
            if os.environ.get(key) != loaded_value or before.get(key) == loaded_value:
                continue
            if key in before:
                os.environ[key] = before[key]
            else:
                os.environ.pop(key, None)
        self._runtime_environment_after = None

    def _publish(self, logical_id, artifact_type, body, author, *, subjects=()):
        return self.store.publish_artifact(logical_id=logical_id, artifact_type=artifact_type,
                                           author=author, body=canonical_bytes(body),
                                           inputs=[{"ref": ref, "purpose": "subject"} for ref in dict.fromkeys(subjects)],
                                           media_type="application/json")

    def _record_failed_stage_usage(self, error):
        """Merge bounded work consumed by a failed stage into run accounting."""
        # A Composer-level budget admission failure reports the observed
        # ledger snapshot in its diagnostics.  That snapshot is not work
        # performed by this failed attempt and must not be charged a second
        # time as if it were a fresh provider call.
        if bool(getattr(error, "usage_is_snapshot", False)):
            return {key: 0 for key in self.usage}
        usage = getattr(error, "usage", None)
        if not isinstance(usage, dict) or not usage:
            snapshot = getattr(error, "topic_budget", None)
            usage = snapshot.get("usage", {}) if isinstance(snapshot, dict) else {}
        if not isinstance(usage, dict):
            usage = {}
        actual = {}
        for key in self.usage:
            value = usage.get(key, 0)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                self.usage[key] += value
                actual[key] = value
        return actual

    def _sync_foundry_usage(self):
        """Reconcile durable per-request charges with the last usage checkpoint."""
        totals = {}
        rows = self.control._conn.execute(
            "SELECT a.body_hash FROM artifacts a JOIN "
            "(SELECT logical_id,MAX(version) version FROM artifacts "
            "WHERE logical_id LIKE 'command/foundry-work/%' GROUP BY logical_id) h "
            "ON a.logical_id=h.logical_id AND a.version=h.version")
        for row in rows:
            body = json.loads(self.store.read_body(row["body_hash"]))
            for key, amount in body.get("usage", {}).items():
                if key in self.usage and type(amount) in (int, float) and math.isfinite(amount) and amount >= 0:
                    totals[key] = totals.get(key, 0) + amount
        changed = False
        for key, total in totals.items():
            delta = total - self.foundry_usage.get(key, 0)
            if delta < 0:
                raise ValidationError("foundry usage ledger regressed below its recorded checkpoint")
            if delta:
                self.usage[key] += delta
                changed = True
        self.foundry_usage = totals
        return changed

    @staticmethod
    def _usage_delta(current, baseline):
        current = current if isinstance(current, dict) else {}
        baseline = baseline if isinstance(baseline, dict) else {}
        return {
            key: max(0, current.get(key, 0) - baseline.get(key, 0))
            for key in ("model_calls", "input_tokens", "output_tokens")
            if type(current.get(key, 0)) in (int, float)
            and type(baseline.get(key, 0)) in (int, float)
        }

    def _foundry_model_call_budget(self, stage, specialist_reports):
        """Reserve foundry calls inside the experiment stage envelope."""
        if not self.workflow.get("capability_foundry_config_path"):
            return None
        quota = stage.get("quota") if isinstance(stage, dict) else None
        if not isinstance(quota, dict) or type(quota.get("max_model_calls")) is not int:
            return AUTONOMOUS_FOUNDRY_MODEL_CALL_LIMIT
        prior = self._stage_usage(stage["id"]).get("model_calls", 0)
        specialist_usage = self._specialist_usage(specialist_reports)
        active_specialist_calls = specialist_usage.get("model_calls", 0)
        repair_panel_reserve = (
            CAPABILITY_REPAIR_PANEL_MODEL_CALL_RESERVE
            if self._capability_repair_panel_required(stage) else 0
        )
        available = quota["max_model_calls"] - prior - active_specialist_calls \
            - FOUNDRY_VERIFIER_MODEL_CALL_RESERVE - repair_panel_reserve
        return max(0, min(AUTONOMOUS_FOUNDRY_MODEL_CALL_LIMIT, available))

    def _capability_repair_panel_required(self, stage):
        """Return whether a fresh capability needs an upper-model design panel."""
        if not isinstance(stage, dict) or stage.get("kind") != "experiment":
            return False
        context = self.context.get(stage.get("id"), {})
        context = context if isinstance(context, dict) else {}
        error = context.get("error")
        if self._is_pre_execution_capability_failure(stage, context, error):
            return True
        if not self._has_executed_experiment_result(
                context, self._stage_experiment_capability_id(stage)):
            return False
        requests = self._requests_for_stage(stage)
        substantive_repair = any(
            isinstance(item, dict)
            and item.get("kind") in {"additional_experiment", "analysis_display", "analysis_repair"}
            for item in requests
        )
        if not substantive_repair:
            return False
        return (
            context.get("status") in STAGE_HOLD_STATUSES
            or context.get("review_status") == "scientific_assignment_blocked"
            or isinstance(context.get("failure_debt"), dict)
            or context.get("backfill_required") is True
        )

    def _foundry_progress(self, stage_id, phase, state):
        self._sync_foundry_usage()
        self._checkpoint(f"{stage_id}:capability_{phase}", force=True)

    def _incremental_stage_usage(self, stage, usage):
        """Charge only newly consumed work from a resumed runner window."""
        if not isinstance(usage, dict) or not isinstance(usage.get("cumulative_usage"), dict):
            return usage
        totals = usage["cumulative_usage"]
        namespace = hashlib.sha256(str(Path(stage["project_dir"]).resolve()).encode()).hexdigest()
        logical_id = f"command/stage-usage/{namespace}"
        record = self.store.head(logical_id)
        prior = json.loads(self.store.read_body(record["body_hash"])) if record else {}
        delta = {key: max(0, value - prior.get(key, 0)) for key, value in totals.items()
                 if type(value) in (int, float) and math.isfinite(value) and value >= 0}
        self._publish(logical_id, "note", totals, "command.controller")
        return delta

    @staticmethod
    def _topic_attempt_usage(attempt):
        """Return the topic runner usage for one attempt, excluding reviewers.

        A successful Composer attempt stores both the core topic intake and
        the independently admitted specialist pool in ``usage``.  The topic
        descriptor's intake quota governs the runner itself, so prefer its
        explicit budget snapshot and fall back to the durable output for
        checkpoints written before that field existed.
        """
        if not isinstance(attempt, dict):
            return {}
        direct = attempt.get("topic_usage")
        if isinstance(direct, dict):
            return direct
        project_dir = attempt.get("project_dir")
        if isinstance(project_dir, str):
            output = Path(project_dir) / "topic-discovery.json"
            if output.is_file():
                try:
                    body = json.loads(output.read_text())
                except (OSError, TypeError, ValueError):
                    body = None
                if isinstance(body, dict) and isinstance(body.get("budget"), dict):
                    usage = body["budget"].get("usage")
                    if isinstance(usage, dict):
                        return usage
        usage = attempt.get("usage")
        return usage if isinstance(usage, dict) else {}

    @classmethod
    def _sum_topic_attempt_usage(cls, attempts, *, scope="all", cycle=None):
        """Sum observed topic work for one bounded admission scope.

        Initial intake retries share one envelope.  Each continuation is a
        separate, deliberately admitted scientific pivot and gets its own
        envelope; otherwise a legacy run with many historical pivots could
        make every newly admitted direction fail before it is evaluated. The
        immutable workflow deadline and provider/model ledgers still bound the
        mission as a whole. Missing usage is treated as zero here; an
        interrupted provider call is reconciled separately by the
        stage/task lease machinery.
        """
        totals = {
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "openalex_requests": 0,
        }
        if not isinstance(attempts, list):
            return totals
        selected_cycle = cycle
        if scope == "continuation" and selected_cycle is None:
            positive_cycles = [
                item.get("cycle") for item in attempts
                if isinstance(item, dict) and isinstance(item.get("cycle"), int)
                and item.get("cycle") > 0
            ]
            selected_cycle = max(positive_cycles) if positive_cycles else None
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            attempt_cycle = attempt.get("cycle", 0)
            if scope == "intake" and attempt_cycle not in (None, 0):
                continue
            if scope == "continuation" and attempt_cycle != selected_cycle:
                continue
            usage = cls._topic_attempt_usage(attempt)
            for key in totals:
                value = usage.get(key, 0)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    totals[key] += value
        return totals

    def _topic_budgets_for_attempt(self, stage_id, configured_budgets, *, scope="intake"):
        """Return the remaining scoped budget for one fresh topic intake.

        The topic runner's bounded repair budget is intentionally local so a
        rejected direction can be abandoned cleanly. The Composer carries a
        separate workflow-level ceiling for initial intake and each deliberate
        continuation pivot. The immutable deadline and provider/model ledgers
        remain the mission-wide guardrails while each pivot retains a real
        bounded repair path after a reviewer hold.
        """
        if not isinstance(configured_budgets, dict) or not configured_budgets:
            return configured_budgets
        record = self.stage_records.get(stage_id, {})
        budget_cycle = self.continuation_cycles if scope == "continuation" else None
        observed = self._sum_topic_attempt_usage(
            record.get("attempts", []), scope=scope, cycle=budget_cycle)
        usage_by_budget = {
            "max_model_calls": "model_calls",
            "max_openalex_requests": "openalex_requests",
            "max_input_tokens": "input_tokens",
            "max_output_tokens": "output_tokens",
        }
        remaining = deepcopy(configured_budgets)
        for budget_key, usage_key in usage_by_budget.items():
            if budget_key not in configured_budgets:
                continue
            limit = configured_budgets[budget_key]
            used = observed[usage_key]
            available = limit - used
            if available < 1:
                diagnostics = [{
                    "kind": "composer_topic_budget",
                    "stage_id": stage_id,
                    "dimension": usage_key,
                    "limit": limit,
                    "observed": used,
                    "scope": (
                        f"continuation_cycle:{budget_cycle}"
                        if scope == "continuation" else "intake"
                    ),
                }]
                quota_error = QuotaExceededError(
                    "topic discovery mission quota exhausted: "
                    f"{usage_key}={used}, limit={limit}",
                    dimension=usage_key,
                    limit=limit,
                    observed=used,
                    # ``observed`` is a diagnostic snapshot, not consumption
                    # caused by the attempted admission itself.
                    usage={},
                    diagnostics=diagnostics,
                )
                quota_error.usage_is_snapshot = True
                quota_error.topic_budget_scope = scope
                raise quota_error
            remaining[budget_key] = int(available)
        return remaining

    @staticmethod
    def _is_topic_intake_retry(error, stage):
        """Return whether a topic intake has an autonomous retry path."""
        return (
            stage.get("kind") == "topic_discovery"
            and bool(getattr(error, "retryable_topic_intake", False))
        )

    @staticmethod
    def _normalize_topic_intake_failure(error, stage):
        """Preserve a scientific topic rejection across every runner boundary.

        A topic validation can be raised after the runner has assembled its
        result (for example while materializing the conditional research
        program) or before the runner's local exception boundary.  Both are
        candidate-level scientific rejections and must enter the same fresh
        exploration path instead of becoming a generic transient retry.
        """
        if stage.get("kind") != "topic_discovery":
            return error
        if getattr(error, "retryable_topic_intake", False):
            return error
        rejection_type = _topic_validation_rejection_type(error)
        if rejection_type not in {"feasibility", "novelty"}:
            return error
        setattr(error, "retryable_topic_intake", True)
        setattr(error, "topic_intake_recoverable", True)
        setattr(error, "topic_retry_reason", "scientific_candidate_rejected")
        return error

    @staticmethod
    def _is_local_topic_budget_exhaustion(error, stage):
        """Distinguish a Composer topic envelope from a provider quota.

        A topic envelope is intentionally finite for one exploration cycle. It
        must trigger a fresh research direction, not terminate the mission and
        not be retried with the same exhausted prompt. Provider/model quotas
        remain hard resource fences and do not match this predicate.
        """
        if stage.get("kind") != "topic_discovery":
            return False
        if isinstance(error, QuotaExceededError):
            if getattr(error, "topic_budget_scope", None) in {"intake", "continuation"}:
                return True
            diagnostics = getattr(error, "diagnostics", None)
            if isinstance(diagnostics, list) and any(
                    isinstance(item, dict) and item.get("kind") == "composer_topic_budget"
                    for item in diagnostics):
                return True
        return "topic discovery quota exhausted" in str(error).casefold()

    @staticmethod
    def _forward_failure_class(error):
        """Classify a failed attempt without turning every defect into a stop."""
        if getattr(error, "failure_class", None) == "context_budget":
            return "resource_fence"
        if isinstance(error, (ProviderConfigurationError, ProviderCooldownError, QuotaExceededError,
                              ComposerHardDeadlineExceeded, ComposerLateStageResult)):
            return "resource_fence"
        if isinstance(error, ModelCallError) and not getattr(error, "outcome_known", True):
            return "unknown_external_outcome"
        if isinstance(error, KeyboardInterrupt):
            return "process_interruption"
        text = str(error or "").casefold()
        if any(token in text for token in (
                "deadline", "quota", "cooldown", "result_unknown",
                "unknown external outcome", "provider is unavailable",
                "provider error", "authentication", "auth_required",
                "credential is unavailable", "credential is invalid",
                "access_denied", "provider configuration")):
            return "resource_fence"
        if isinstance(error, ModelWorkBlocked) or any(token in text for token in (
                "adversarial", "claim_support", "estimator_definedness",
                "independent_validation", "scientific", "hypothesis",
                "evidence contract", "review rejected", "insufficient evidence")):
            return "scientific_hold"
        return "mechanical_contract"

    @staticmethod
    def _topic_candidate_from_failure(error):
        """Recover a model-selected topic for a truthful provisional handoff."""
        traces = getattr(error, "candidate_attempt_trace", [])
        if not isinstance(traces, list):
            return None
        for trace in reversed(traces):
            if not isinstance(trace, dict):
                continue
            selected = trace.get("selected_topic")
            if isinstance(selected, dict) and isinstance(selected.get("id"), str):
                return deepcopy(selected)
        return None

    def _stage_experiment_capability_id(self, stage):
        """Return the capability identity expected by an experiment stage."""
        if not isinstance(stage, dict) or stage.get("kind") != "experiment":
            return None
        by_id = {
            item.get("id"): item for item in self.workflow.get("stages", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        pending = list(stage.get("depends_on", []))
        seen = set()
        while pending:
            stage_id = pending.pop()
            if stage_id in seen or stage_id not in by_id:
                continue
            seen.add(stage_id)
            ancestor = by_id[stage_id]
            if ancestor.get("kind") == "topic_discovery":
                context = self.context.get(stage_id, {})
                if isinstance(context, dict):
                    generated = context.get("generated_capability")
                    if isinstance(generated, dict) and isinstance(
                            generated.get("capability_id"), str):
                        return generated["capability_id"]
                    topic = context.get("topic")
                    if isinstance(topic, dict) and isinstance(
                            topic.get("experiment_capability_id"), str):
                        return topic["experiment_capability_id"]
            pending.extend(ancestor.get("depends_on", []))
        return None

    @staticmethod
    def _has_executed_experiment_result(context, expected_capability_id=None):
        """Recognize only executed evidence for the current capability.

        A resumed stage can retain a results-package path from an older
        capability or project attempt.  Presence of that path alone is not
        proof that the current experiment ran.  Require result content and,
        when the current capability is known, an identity match.
        """
        context = context if isinstance(context, dict) else {}
        observed = False
        identities = set()

        def collect_identity(value):
            if not isinstance(value, dict):
                return
            for key in ("id", "study_id", "capability_id", "experiment_id"):
                item = value.get(key)
                if isinstance(item, str) and item:
                    identities.add(item)

        collect_identity(context)
        raw_results = context.get("raw_results")
        if raw_results is not None:
            observed = True
            collect_identity(raw_results)
        metrics = context.get("metrics")
        if isinstance(metrics, list):
            observed = True
        if isinstance(context.get("execution_refs"), list) and context.get("execution_refs"):
            # A run can fail during deterministic validation or independent
            # review after execution has already produced an observation. It
            # is still observed evidence even when no results-package path
            # was written.
            observed = True
        if any(context.get(key) for key in (
                "deterministic_validation_ref", "assessment_ref", "model_review_refs")):
            observed = True

        package = context.get("results_package")
        if isinstance(package, dict):
            collect_identity(package)
            observed = any(isinstance(package.get(key), list)
                           for key in ("metrics", "findings", "assets"))
        elif isinstance(package, str) and package:
            path = Path(package)
            if path.is_file():
                try:
                    payload = json.loads(path.read_text())
                except (OSError, ValueError, TypeError):
                    payload = None
                if isinstance(payload, dict):
                    collect_identity(payload)
                    observed = any(isinstance(payload.get(key), list)
                                   for key in ("metrics", "findings", "assets"))

        if not observed:
            return False
        if expected_capability_id is not None:
            # Legacy result packets may not carry a capability identity. They
            # remain usable as observed evidence when there is no contradictory
            # identity; an explicit mismatch is what marks a stale packet.
            return not identities or expected_capability_id in identities
        return True

    def _is_pre_execution_capability_failure(self, stage, context=None, error=None):
        """Detect generated-program failure before any current result exists."""
        if not isinstance(stage, dict) or stage.get("kind") != "experiment":
            return False
        context = context if isinstance(context, dict) else {}
        expected_capability_id = self._stage_experiment_capability_id(stage)
        if self._has_executed_experiment_result(context, expected_capability_id):
            return False
        failure_recovery = context.get("failure_recovery")
        if not isinstance(failure_recovery, dict):
            failure_recovery = {}
        # A malformed provider envelope is a transport/model-contract
        # recovery, not a failed scientific capability.  Counting it against
        # the pre-execution design lease used to authorize an empty
        # experiment handoff after many retries.
        if (failure_recovery.get("failure_class") == "model_contract"
                or failure_recovery.get("recovery_mode") == "format_repair_then_rerun"
                or context.get("review_status") == "model_contract_repair"):
            return False
        if failure_recovery.get("requires_capability_repair") is True:
            # A failed executable is a repairable pre-execution capability
            # failure even when the runner never produced a JSON result.
            return True
        messages = [str(error or ""), str(context.get("error") or "")]
        debt = context.get("failure_debt")
        if isinstance(debt, dict):
            messages.append(str(debt.get("error") or ""))
        lowered = " ".join(messages).casefold()
        return (
            "capability foundry" in lowered
            and any(token in lowered for token in (
                "did not admit", "did not finish normally", "author failed",
                "authoring failed", "no executable program", "program author",
            ))
        )

    def _experiment_handoff_requires_recovery(self, stage, context):
        """Keep an unexecuted experiment behind a model-contract repair.

        ``forward_first`` may carry a scientific result with unresolved debt,
        but it must never turn a provider-format failure into an input for
        interpretation or argument.  This predicate is deliberately scoped
        to experiments with no current observation so result-bearing review
        failures retain the ordinary Composer handoff policy.
        """
        if not isinstance(stage, dict) or stage.get("kind") != "experiment":
            return False
        if not isinstance(context, dict):
            return False
        if self._has_executed_experiment_result(
                context, self._stage_experiment_capability_id(stage)):
            return False
        recovery = context.get("failure_recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        return (
            context.get("results_status") == "not_executed"
            and (
                recovery.get("recovery_mode") in {
                    "repair_then_rerun", "format_repair_then_rerun",
                    "experiment_diagnose_patch_execute_recalculate",
                }
                or recovery.get("failure_class") == "model_contract"
                or context.get("review_status") == "model_contract_repair"
                or context.get("format_recovery") is True
            )
        )

    def _hold_unexecuted_experiment(self, stage, context, *, reason):
        """Convert a false provisional experiment into an executable hold.

        The specialist verifier can return ``candidate_needs_review`` after a
        transport/contract failure even though the experiment emitted no
        observation.  That status is valid for an evidence-bearing candidate,
        not for an empty experiment input.  Keep the failure scoped, recreate a
        same-stage format work order when necessary, and make the result
        release-blocking so Interpretation cannot manufacture claims from it.
        """
        held = deepcopy(context) if isinstance(context, dict) else {}
        for key in ("composer_decision", "progression_state", "backfill_required"):
            held.pop(key, None)
        held.update({
            "status": "research_expansion_required",
            "results_status": "not_executed",
            "release_blocking": True,
            "review_status": held.get("review_status") or "scientific_assignment_blocked",
            "error": held.get("error") or reason,
        })
        debt = held.get("failure_debt")
        debt = deepcopy(debt) if isinstance(debt, dict) else {}
        debt.update({
            "stage_id": stage.get("id"),
            "kind": "experiment",
            "failure_class": "unexecuted_experiment",
            "error": held["error"],
            "next_action": "repair the experiment contract or capability before downstream interpretation",
            "release_blocking": True,
        })
        held["failure_debt"] = debt
        requests = held.get("research_requests")
        requests = deepcopy(requests) if isinstance(requests, list) else []
        if not requests:
            request = self._format_contract_recovery_request(stage, held)
            if isinstance(request, dict):
                requests = [request]
        held["research_requests"] = requests
        return held

    def _pre_execution_repair_count(self, stage, error=None):
        """Return the durable repair count for an unexecuted capability.

        A forward-first mission gets a bounded chance to repair an executable
        program. The count belongs to the current capability-repair lineage,
        not to every historical attempt of the stage. Using the stage's total
        attempt count here made a later, materially changed capability look
        exhausted before it received its first authoring call.
        """
        if not isinstance(stage, dict):
            return 0
        context = self.context.get(stage.get("id"), {})
        context = context if isinstance(context, dict) else {}
        count = context.get("capability_repair_attempts")
        counts = []
        if type(count) is int and count >= 0:
            counts.append(count)
        history = context.get("experiment_repair_history")
        if isinstance(history, list):
            # History is capped for packet size. Use its lineage values rather
            # than its length so a projected stage result cannot erase the
            # durable repair count.
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                value = entry.get("repair_attempts")
                if type(value) is int and value >= 0:
                    counts.append(value)
        if count is None:
            recovery = context.get("failure_recovery")
            recovery = recovery if isinstance(recovery, dict) else {}
            diagnostics = recovery.get("model_diagnostics")
            diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
            error_text = " ".join(
                str(value or "") for value in (context.get("error"), error))
            if (recovery.get("requires_capability_repair") is True
                    and ("repair budget exhausted" in error_text.casefold()
                         or "model-call budget exhausted" in error_text.casefold()
                         or "model_call_budget_exhausted" in error_text.casefold()
                         or diagnostics.get("repair_gate") is not None
                         or diagnostics.get("repair_budget_exhausted") is not None
                         or diagnostics.get("budget_exhausted") is not None)):
                # Legacy checkpoints did not persist the per-capability
                # counter. Their foundry error still records the exhausted
                # gate, so migrate them to the design-pivot boundary instead
                # of forwarding the unexecuted experiment.
                counts.append(PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT)
            # Older checkpoints retained only the failed-attempt ledger. If
            # the capability never produced an observation, that ledger is a
            # conservative lower bound for the repair lineage.
            if recovery.get("requires_capability_repair") is True:
                failure_debt = context.get("failure_debt")
                failure_debt = failure_debt if isinstance(failure_debt, dict) else {}
                stage_record = self.stage_records.get(stage.get("id"), {})
                stage_attempts = (
                    stage_record.get("attempt_count")
                    if isinstance(stage_record, dict) else None
                )
                for value in (
                        failure_debt.get("attempts"),
                        recovery.get("attempt_number"),
                        stage_attempts,
                ):
                    if type(value) is int and value > 0:
                        counts.append(min(value, PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT))
        if not counts:
            return 0
        return max(0, min(max(counts), PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT))

    @staticmethod
    def _is_experiment_repair_request(request):
        """Return whether a work order is an executable Methods repair.

        A scientific experiment repair may outlive a small continuation cycle
        lease in a forward-first mission. It remains fenced by the mission
        wall, stage quota, provider quota, and the experiment admission gates.
        """
        if not isinstance(request, dict):
            return False
        if request.get("target_stage_kind") == "experiment":
            return request.get("kind") in {
                "additional_experiment", "analysis_display", "analysis_repair",
            }
        return (
            request.get("kind") == "additional_experiment"
            and request.get("owner") == "methods.validation"
        )

    def _autonomous_experiment_repair_request(self, stage, context, error,
                                               repair_attempts):
        """Create a concrete code-and-evidence repair order after a lease.

        A repeated pre-execution foundry failure must not become an empty
        provisional result for Interpretation. This order records a changed
        design axis and makes the next experiment responsible for diagnosis,
        source edits, replay, independent recalculation, and review.
        """
        if not isinstance(stage, dict) or stage.get("kind") != "experiment":
            return None
        context = context if isinstance(context, dict) else {}
        recovery = context.get("failure_recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        history = context.get("experiment_repair_history")
        history = history if isinstance(history, list) else []
        axis = EXPERIMENT_REPAIR_AXES[
            max(0, int(repair_attempts)) % len(EXPERIMENT_REPAIR_AXES)
        ]
        cycle = self.continuation_cycles + 1
        digest = str(
            recovery.get("input_sha256")
            or context.get("failure_input_sha256")
            or hashlib.sha256(str(error).encode()).hexdigest()
        )[:12]
        safe_stage = re.sub(r"[^a-z0-9-]+", "-", str(stage.get("id", "experiment")).casefold()).strip("-")
        safe_stage = safe_stage[:20] or "experiment"
        request_id = f"auto-{safe_stage}-repair-{cycle}-{axis['id']}-{digest[:6]}"[:64]
        prior_gate = recovery.get("gate") or recovery.get("failure_class") or "scientific_admission"
        commands = recovery.get("repair_commands")
        commands = deepcopy(commands) if isinstance(commands, list) else []
        commands.extend([
            {
                "id": "methods-diagnose-source-and-trace",
                "operation": "inspect",
                "target": "the exact executor, validator, raw trace, and failed gate",
                "instruction": (
                    "Reconstruct the failure from the immutable dossier before editing code; "
                    "identify the first invalid scientific assumption and bind it to a source line, "
                    "observed field, or deterministic check."
                ),
                "acceptance_check": "The repair plan names the failed assumption and the evidence that falsifies it.",
            },
            {
                "id": "methods-edit-executor-and-validator",
                "operation": "edit_program",
                "target": "executor_source and validator_source",
                "instruction": axis["instruction"],
                "acceptance_check": axis["evidence"],
            },
            {
                "id": "methods-rerun-independent-review",
                "operation": "execute",
                "target": "a fresh capability revision and experiment attempt namespace",
                "instruction": (
                    "Run the repaired program, retain raw observations and trace, recompute every "
                    "primary outcome independently, then rerun deterministic and adversarial review."
                ),
                "acceptance_check": (
                    "The new result is executable, independently recalculated, and either admitted "
                    "or retained as an explicit bounded negative result."
                ),
            },
        ])
        checks = recovery.get("acceptance_checks")
        checks = deepcopy(checks) if isinstance(checks, list) else []
        checks.extend([axis["evidence"], "The repaired code is executed in a new immutable attempt namespace."])
        plan = {
            "schema_version": "experiment-repair-plan-1",
            "mode": "diagnose_patch_execute_recalculate",
            "lineage": {
                "source_stage_id": stage.get("id"),
                "prior_capability_repair_attempts": repair_attempts,
                "continuation_cycle": cycle,
                "failure_input_sha256": recovery.get("input_sha256") or context.get("failure_input_sha256"),
                "prior_gate": prior_gate,
            },
            "design_axis": axis["id"],
            "instruction": axis["instruction"],
            "required_evidence": axis["evidence"],
            "must_preserve": [
                "the admitted topic and exact research question",
                "the prior failure dossier and raw diagnostic evidence",
                "independent validation rather than executor self-confirmation",
            ],
            "must_change": [
                "the failed mechanism, estimand, design, or measurement axis",
                "the executor and validator together when their conventions disagree",
            ],
            "prohibited": [
                "forwarding an unexecuted result to interpretation",
                "patching result JSON instead of the executable source",
                "hiding an undefined or censored estimand with zero, NaN, or endpoint fallback",
            ],
        }
        history_entry = {
            "cycle": cycle,
            "repair_attempts": repair_attempts,
            "plan": deepcopy(plan),
            "error": str(error)[:2400],
            "dossier_ref": recovery.get("dossier_ref") or context.get("failure_dossier_ref"),
        }
        updated_history = (history + [history_entry])[-12:]
        request = {
            "id": request_id,
            "kind": "additional_experiment",
            "owner": "methods.validation",
            "target_stage_id": stage.get("id"),
            "target_stage_kind": "experiment",
            "repair_priority": "immediate",
            "objective": (
                "Diagnose, patch, and rerun the failed computational experiment on the "
                f"{axis['id']} axis: {axis['instruction']}."
            )[:1800],
            "why": (
                "The generated capability exhausted its source-level repair lease before producing "
                f"an observation. Gate={prior_gate}; this is a new design axis, not a repeated validator-only retry."
            )[:1800],
            "success_condition": (
                "The repaired executor and validator run in a fresh namespace, every primary outcome "
                "is independently recalculated, and the result is admitted or recorded as a bounded negative result."
            ),
            "evidence_needed": (
                "Immutable failure dossier, exact executor/validator sources, raw observations, "
                "execution trace, deterministic replay, independent recalculation, and adversarial review."
            ),
            "source_stage_id": stage.get("id"),
            "failure_dossier_ref": recovery.get("dossier_ref") or context.get("failure_dossier_ref"),
            "failure_input_sha256": recovery.get("input_sha256") or context.get("failure_input_sha256"),
            "repair_commands": commands[-12:],
            "acceptance_checks": checks[-12:],
            "review_directives": deepcopy(recovery.get("review_directives", []))
                if isinstance(recovery.get("review_directives"), list) else [],
            "model_diagnostics": deepcopy(recovery.get("model_diagnostics", {}))
                if isinstance(recovery.get("model_diagnostics"), dict) else {},
            "recovery_mode": "experiment_diagnose_patch_execute_recalculate",
            "experiment_repair_plan": plan,
            "repair_strategy": axis["id"],
            "attempt_lineage": deepcopy(plan["lineage"]),
        }
        context.update({
            "stage_id": stage.get("id"),
            "kind": "experiment",
            "status": "research_expansion_required",
            "review_status": "scientific_assignment_blocked",
            "release_blocking": True,
            # Preserve the lineage counter across the new work order. Resetting
            # this field made every continuation look like the first repair and
            # allowed an unchanged capability to consume the whole mission.
            "capability_repair_attempts": max(
                int(repair_attempts) + 1, len(updated_history)),
            "experiment_repair_plan": deepcopy(plan),
            "experiment_repair_history": updated_history,
            "research_requests": [request],
            "research_expansion_requests": [],
            "failure_debt": {
                "stage_id": stage.get("id"),
                "kind": "experiment",
                "failure_class": "experiment_capability_repair",
                "error": str(error)[:4096],
                "attempts": repair_attempts,
                "next_action": "execute the diagnosis-patch-replay-recalculation work order",
                "release_blocking": True,
                "failure_dossier_ref": request.get("failure_dossier_ref"),
            },
        })
        if recovery:
            recovery = deepcopy(recovery)
            recovery["recovery_mode"] = "experiment_diagnose_patch_execute_recalculate"
            recovery["requires_capability_repair"] = True
            recovery["experiment_repair_plan"] = deepcopy(plan)
            context["failure_recovery"] = recovery
        self.context[stage["id"]] = context
        return request

    def _composer_can_advance_after_admission(self, stage, error):
        """Return whether Composer authority may hand off a failed stage.

        The stage assignment is the Composer's admission decision.  Once that
        decision exists, deterministic validation, model output shape, and
        scientific review findings are evidence to carry forward rather than
        vetoes.  A process interruption is different: its external outcome
        is genuinely unknown and must be reconciled before another attempt.
        Provider cooldowns and hard deadline exceptions are also kept as
        resource fences because no downstream model call can be made safely
        while the resource is unavailable.
        """
        if not self._forward_first() or not isinstance(stage, dict):
            return False
        # An executable scientific repair order has priority over the
        # provisional handoff.  Forwarding here used to mark the failed scope
        # as a candidate immediately after issuing a dossier, so the next
        # adaptive cycle spent its budget on the same upstream work instead of
        # executing the repair that the reviewer requested.
        context = self.context.get(stage.get("id"), {})
        recovery = context.get("failure_recovery") if isinstance(context, dict) else None
        if self._experiment_handoff_requires_recovery(stage, context):
            return False
        observed_experiment = (
            isinstance(context, dict)
            and (
                context.get("results_status") not in {None, "not_executed"}
                or any(context.get(key) for key in (
                    "results_package", "raw_results", "metrics", "findings",
                    "execution_refs", "deterministic_validation_ref",
                ))
            )
        )
        repair_first = (
            stage.get("kind") in {"interpretation", "argument"}
            or stage.get("kind") == "experiment" and observed_experiment
        )
        # The recovery ledger is authoritative even when the failed runner
        # could not project its observed result into the top-level context.
        # In particular, capability authoring/admission failures can carry a
        # valid experiment repair order while ``results_package`` is absent.
        # Gating this check on ``observed_experiment`` allowed those failures
        # to become an empty forward-progress packet, after which a downstream
        # interpretation was admitted with no experiment input.  Any explicit
        # scientific repair order must be executed before a provisional handoff
        # for every research stage.
        if (isinstance(recovery, dict)
                and recovery.get("recovery_mode") == "repair_then_rerun"
                and isinstance(context.get("research_requests"), list)
                and context.get("research_requests")):
            return False
        # A malformed survey/assignment envelope has no current scientific
        # handoff. Forward-first is allowed to carry unresolved evidence, not
        # an absent assessment that the downstream stage cannot consume.
        if (isinstance(recovery, dict)
                and recovery.get("failure_class") == "model_contract"
                and (context.get("format_recovery") is True
                     or context.get("review_status") == "model_contract_repair")):
            return False
        # A generated capability that never executed is never a valid
        # downstream input. Once its source-level repair lease is exhausted,
        # _admit_scientific_blocker_recovery creates a changed experiment work
        # order; it must not become a provisional empty result.
        if self._is_pre_execution_capability_failure(
                stage, self.context.get(stage.get("id")), error):
            return False
        failure_class = self._forward_failure_class(error)
        return failure_class not in {"resource_fence", "unknown_external_outcome",
                                     "process_interruption"}

    def _authorize_forward_context(self, stage, context, *, reason):
        """Make Composer's handoff explicit without manufacturing evidence."""
        candidate = deepcopy(context) if isinstance(context, dict) else {}
        stage_id = stage["id"]
        prior_status = candidate.get("status")
        debt = candidate.get("failure_debt")
        if not isinstance(debt, dict):
            debt = {
                "stage_id": stage_id,
                "kind": stage.get("kind"),
                "failure_class": "scientific_hold" if prior_status in STAGE_HOLD_STATUSES
                else "composer_handoff",
                "error": candidate.get("error") or (
                    f"stage returned {prior_status}" if prior_status else reason),
                "attempts": candidate.get("attempt_number", 0),
                "next_action": "backfill or re-review this scope after downstream work",
            }
        debt["release_blocking"] = False
        debt["authority"] = "command.composer"
        candidate.update({
            "status": "candidate_needs_review",
            "composer_decision": "advance_with_findings",
            "progression_state": "advanced_with_findings",
            "evidence_state": candidate.get("evidence_state") or (
                "unverified" if candidate.get("results_status") == "not_executed"
                else "needs_review"),
            "release_blocking": False,
            "failure_debt": debt,
            "backfill_required": True,
            "preserve_work_orders": True,
        })
        if prior_status in STAGE_HOLD_STATUSES:
            candidate.setdefault("deferred_review_findings", {
                "decision": prior_status,
                "reason": candidate.get("error") or reason,
            })
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "composer_authorized_advance",
            "stage_id": stage_id,
            "from_status": prior_status,
            "reason": reason,
            "authority": "command.composer",
            "evidence_state": candidate["evidence_state"],
            "release_blocking": False,
        })
        return candidate

    def _mark_forward_blockers_advisory(self, stage_id):
        """Keep blocker evidence while removing its authority to stop flow."""
        for blocker in self.blockers:
            if not isinstance(blocker, dict) or blocker.get("stage_id") != stage_id:
                continue
            blocker.update({
                "gating": False,
                "release_blocking": False,
                "disposition": "forwarded_with_findings",
                "authority": "command.composer",
            })

    @staticmethod
    def _prior_stage_artifact(stage, attempt_history):
        """Find the newest successful scientific artifact for a stage.

        A failed continuation must not erase the last usable packet.  The
        attempt ledger already records immutable per-attempt directories, so
        reusing the latest successful file is deterministic and does not
        require copying a mutable incumbent into the new continuation.
        """
        if not isinstance(stage, dict) or not isinstance(attempt_history, list):
            return None
        filenames = {
            "interpretation": ("interpretation.json", "output/interpretation.json"),
            "argument": ("argument.json", "output/argument.json"),
            "paper": ("output/run.json", "output/manuscript.json"),
        }.get(stage.get("kind"), ())
        for attempt in reversed(attempt_history):
            if not isinstance(attempt, dict) or attempt.get("state") != "succeeded":
                continue
            root = attempt.get("project_dir")
            if not isinstance(root, str):
                continue
            root = Path(root)
            for relative in filenames:
                path = root / relative
                if not path.is_file():
                    continue
                try:
                    payload = json.loads(path.read_text())
                except (OSError, ValueError, TypeError):
                    continue
                if isinstance(payload, dict):
                    return {"path": str(path.resolve()), "payload": payload}
        return None

    @staticmethod
    def _interpretation_package_from_artifact(artifact):
        """Project a successful interpretation output into its package shape."""
        if not isinstance(artifact, dict):
            return None
        payload = artifact.get("payload")
        if not isinstance(payload, dict):
            return None
        if (payload.get("schema_version") == "scientific-interpretation-package-1"
                and isinstance(payload.get("interpretation"), dict)):
            return deepcopy(payload)
        if (payload.get("schema_version") == "scientific-interpretation-1"
                and set(payload) >= {"schema_version", "research_question"}):
            return {
                "schema_version": "scientific-interpretation-package-1",
                "interpretation": deepcopy(payload),
                "status": "accepted",
            }
        return None

    def _materialize_forward_progress(self, stage, attempt_stage, error, context,
                                      specialist_bundle, attempt_history,
                                      *, force_advance=False):
        """Create an honest provisional node so the agenda can keep moving."""
        if not self._forward_first() and not force_advance:
            return None
        stage_id = stage.get("id") if isinstance(stage, dict) else None
        # The durable context is authoritative when failure analysis has
        # already issued a repair order. A caller may still hold the pre-error
        # runner envelope, especially after a model-contract failure. Check
        # both packets so a stale local projection cannot turn an unexecuted
        # experiment into a downstream input.
        authoritative_context = (
            self.context.get(stage_id) if isinstance(stage_id, str) else None
        )
        if (isinstance(stage, dict)
                and stage.get("kind") == "experiment"
                and isinstance(authoritative_context, dict)
                and self._experiment_handoff_requires_recovery(
                    stage, authoritative_context)):
            return None
        # Do not erase a scoped scientific repair order by materializing a
        # provisional packet.  This is a defensive second line behind
        # ``_composer_can_advance_after_admission`` for restored/legacy
        # checkpoints whose context was reconstructed between those calls.
        existing_recovery = (
            context.get("failure_recovery") if isinstance(context, dict) else None
        )
        existing_requests = context.get("research_requests") if isinstance(context, dict) else None
        if self._experiment_handoff_requires_recovery(stage, context):
            return None
        if (isinstance(existing_recovery, dict)
                and existing_recovery.get("recovery_mode") == "repair_then_rerun"
                and isinstance(existing_requests, list)
                and existing_requests):
            return None
        if (self._is_pre_execution_capability_failure(stage, context, error)
                and not force_advance):
            # An unexecuted generated program is not a useful provisional
            # result. Keep the failure scoped so the Composer can pivot the
            # executable direction instead of parking behind a release gate.
            return None
        failure_class = self._forward_failure_class(error)
        if failure_class in {"resource_fence", "unknown_external_outcome",
                             "process_interruption"} and not force_advance:
            return None
        stage_id = stage["id"]
        candidate = deepcopy(context) if isinstance(context, dict) else {}
        incumbent = self.context.get(stage_id)
        if not candidate and isinstance(incumbent, dict):
            candidate = deepcopy(incumbent)
        prior_artifact = self._prior_stage_artifact(stage, attempt_history)
        if stage.get("kind") == "interpretation":
            prior_package = self._interpretation_package_from_artifact(prior_artifact)
            if prior_package is not None and not isinstance(candidate.get("interpretation"), dict):
                # The scientific packet is explicitly marked as incumbent
                # evidence.  It is not re-admitted as a fresh interpretation;
                # the current failure debt remains attached to the handoff.
                candidate["interpretation"] = prior_package
                candidate["binding_output_path"] = prior_artifact["path"]
                candidate["incumbent_interpretation_path"] = prior_artifact["path"]
        elif stage.get("kind") == "argument" and prior_artifact is not None:
            # Paper composition may still inspect a prior argument while the
            # current repair is carried as debt.  Keep the visible forward
            # progress path separate from the immutable binding artifact.
            candidate["binding_output_path"] = prior_artifact["path"]
            candidate["incumbent_argument_path"] = prior_artifact["path"]
        if stage["kind"] == "topic_discovery" and not isinstance(candidate.get("topic"), dict):
            selected = self._topic_candidate_from_failure(error)
            if selected is None:
                if not force_advance:
                    # A topic-less packet cannot feed a survey honestly. It gets a
                    # bounded retry/pivot, not a fabricated question.
                    return None
                candidate["topic_resolution"] = "unresolved_after_composer_admission"
            else:
                candidate.update({
                    "topic": selected,
                    "selected_id": selected["id"],
                    "admission_state": "provisional_for_survey",
                })

        # Runner envelopes do not always repeat the workspace identity on a
        # late validation failure.  The attempt directory is the immutable
        # source of the survey/experiment handoff and must remain addressable
        # even when the public result is provisional.
        candidate.setdefault(
            "project_dir", str(Path(attempt_stage["project_dir"]).resolve()))

        output_root = Path(attempt_stage["project_dir"]).resolve() / "output"
        output_root.mkdir(parents=True, exist_ok=True)
        output_path = output_root / "forward-progress.json"
        available = {
            key: deepcopy(candidate.get(key))
            for key in (
                "output_path", "project_dir", "survey_ref", "assessment_ref",
                "nomination_ref", "incumbent_ref", "survey_current", "assessment_current",
                "gap_state", "topic_admission", "results_package", "argument_package_path",
                "topic", "selected_id", "results_status", "evidence_state",
                "binding_output_path", "incumbent_interpretation_path",
                "incumbent_argument_path",
            )
            if key in candidate
        }
        specialist_findings = []
        if isinstance(specialist_bundle, dict):
            for report in specialist_bundle.get("reports", []):
                if not isinstance(report, dict):
                    continue
                specialist_findings.append({
                    key: deepcopy(report[key])
                    for key in ("role_id", "status", "status_code", "error",
                                "artifact_ref", "verifier_artifact_ref")
                    if key in report
                })
        debt = {
            "stage_id": stage_id,
            "kind": stage["kind"],
            "failure_class": failure_class,
            "error": str(error)[:4096],
            "attempts": len(attempt_history),
            "next_action": "backfill this scope after the agenda completes higher-value work",
            "release_blocking": False if force_advance else True,
            "authority": "command.composer" if force_advance else "stage_runner",
        }
        body = {
            "schema_version": "composer-forward-progress-1",
            "status": "candidate_needs_review",
            "provisional": True,
            "stage_id": stage_id,
            "kind": stage["kind"],
            "composer_decision": "advance_with_findings" if force_advance else "defer",
            "evidence_state": "unverified",
            "failure_debt": debt,
            "available_context": available,
            "specialist_findings": specialist_findings,
            "attempt_history": deepcopy(attempt_history[-8:]),
            "usage": deepcopy(candidate.get("usage", {}))
                if isinstance(candidate.get("usage"), dict) else {},
            "specialist_usage": deepcopy(specialist_bundle.get("usage", {}))
                if isinstance(specialist_bundle, dict) else {},
        }
        if isinstance(prior_artifact, dict):
            package = self._interpretation_package_from_artifact(prior_artifact)
            if package is not None:
                # Keep the handoff self-contained for a legacy resume that
                # reconstructs context from only forward-progress.json.
                body["provisional_interpretation"] = package
                body["provisional_interpretation_path"] = prior_artifact["path"]
        artifact = self._publish(
            f"command/composer/forward-progress/{stage_id}/{self.continuation_cycles}-"
            f"{len(attempt_history)}",
            "note", body, "command.composer",
        )
        body["artifact_ref"] = artifact["artifact_ref"]
        output_path.write_bytes(canonical_bytes(body))

        candidate.update({
            "stage_id": stage_id,
            "kind": stage["kind"],
            "status": "candidate_needs_review",
            "provisional": True,
            "forward_progress": True,
            "composer_decision": "advance_with_findings" if force_advance else "defer",
            "progression_state": "advanced_with_findings" if force_advance else "deferred",
            "evidence_state": "unverified",
            "release_blocking": False if force_advance else True,
            "forward_progress_artifact": artifact["artifact_ref"],
            "forward_progress_path": str(output_path),
            "failure_debt": debt,
            "output_path": str(output_path),
            # Do not let a technical failure immediately regenerate the same
            # continuation. The debt is visible and can be backfilled later.
            "research_expansion_requests": [],
            "research_requests": [],
            "deferred_research_requests": [debt],
            "backfill_required": True,
            "preserve_work_orders": True,
        })
        if stage["kind"] == "survey":
            candidate.setdefault("gap_state", "insufficient_evidence")
            candidate.setdefault("topic_admission", "exploratory_pilot")
            candidate.setdefault("survey_current", False)
            candidate.setdefault("assessment_current", False)
        if stage["kind"] == "experiment":
            candidate.setdefault("results_status", "not_executed")
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "forward_provisional_stage",
            "stage_id": stage_id,
            "failure_class": failure_class,
            "artifact_ref": artifact["artifact_ref"],
            "composer_decision": "advance_with_findings" if force_advance else "defer",
            "release_blocking": False if force_advance else True,
            "next_action": debt["next_action"],
        })
        if force_advance:
            self._mark_forward_blockers_advisory(stage_id)
        return candidate

    def _remaining(self):
        remaining = self.deadline - self.clock()
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining = min(remaining, self.deadline_epoch - time.time())
        if remaining <= 0:
            raise ComposerHardDeadlineExceeded("composer hard deadline exceeded")
        return remaining

    def _deadline_dispatch_floor(self):
        """Return the small control-plane window needed for safe dispatch.

        Stage estimates are forecasts, not execution fences.  In the
        deadline-governed mode, a residual window may still produce a useful
        accepted stage result, so the Composer keeps admitting work until only
        this checkpoint-and-ledger margin remains.  The margin follows the
        configured checkpoint cadence and is capped to keep it small relative
        to a scientific stage.
        """
        checkpoint = float(self.workflow["time_policy"]["checkpoint_seconds"])
        return max(0.5, min(5.0, checkpoint / 10.0))

    def _deadline_exhausted(self):
        """Return whether the immutable mission wall has been reached."""
        remaining = self.deadline - self.clock()
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining = min(remaining, self.deadline_epoch - time.time())
        return remaining <= 0

    def _extend_deadline(self, additional_seconds):
        """Apply an explicit human extension without mutating the workflow graph."""
        if not isinstance(additional_seconds, (int, float)) or isinstance(additional_seconds, bool) \
                or not math.isfinite(additional_seconds) or additional_seconds <= 0:
            raise ValidationError("additional_seconds must be finite and positive")
        extension = float(additional_seconds)
        if self._workflow_record is None and not (self.root / "state" / "control.sqlite").exists():
            raise ValidationError("a deadline extension requires an existing Composer project")
        old_deadline_epoch = self.deadline_epoch
        old_deadline = self.deadline
        new_deadline_epoch = old_deadline_epoch + extension
        new_deadline = old_deadline + extension
        body = {
            "schema_version": "composer-deadline-extension-1",
            "workflow_id": self.workflow["id"], "run_id": self.run_id,
            "additional_seconds": extension,
            "previous_deadline_at_epoch": old_deadline_epoch,
            "deadline_at_epoch": new_deadline_epoch,
            "created_at": now_iso(),
        }
        record = self._publish(
            f"command/composer/deadline-extensions/{uuid.uuid4().hex}",
            "decision_note", body, "principal")
        self.deadline_epoch = new_deadline_epoch
        self.deadline = new_deadline
        self.deadline_extensions.append({
            "additional_seconds": extension,
            "previous_deadline_at_epoch": old_deadline_epoch,
            "deadline_at_epoch": new_deadline_epoch,
            "artifact_ref": record["artifact_ref"],
        })
        # Persist the extension immediately.  If the process is interrupted
        # before the next stage boundary, resume still sees the new mission
        # wall rather than silently discarding the human decision.
        self._checkpoint("deadline_extended", force=True)

    def interim_report(self, *, stop_reason=None, persist=True):
        """Return and optionally persist a concise human-readable run snapshot.

        The Composer's full checkpoint and ledger remain the source of truth.
        This projection is intentionally small: it tells an operator what was
        completed, what is active or held, why execution stopped, and exactly
        how to resume with an explicit deadline extension.
        """
        now = self.clock()
        remaining = self.deadline - now
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining = min(remaining, self.deadline_epoch - time.time())
        rows = []
        completed, active, pending, held, blocked = [], [], [], [], []
        for stage in self.workflow["stages"]:
            stage_id = stage["id"]
            record = self.stage_records.get(stage_id, {})
            status = record.get("status", "pending")
            attempts = record.get("attempts", [])
            if not isinstance(attempts, list):
                attempts = []
            attempt_count = record.get("attempt_count", len(attempts))
            if type(attempt_count) is not int or attempt_count < 0:
                attempt_count = len(attempts)
            row = {
                "id": stage_id,
                "kind": stage["kind"],
                "status": status,
                "attempts": attempt_count,
            }
            output_path = record.get("output_path")
            if isinstance(output_path, str) and output_path:
                row["output_path"] = output_path
            error = record.get("error")
            if isinstance(error, str) and error:
                row["error"] = error[:240]
            rows.append(row)
            if status in STAGE_READY_STATUSES:
                completed.append(stage_id)
            elif status in {"running", "retrying"}:
                active.append(stage_id)
            elif status in STAGE_HOLD_STATUSES:
                held.append(stage_id)
            elif status == "blocked":
                blocked.append(stage_id)
            else:
                pending.append(stage_id)
        if stop_reason is None:
            if self._deadline_exhausted():
                stop_reason = "hard_deadline"
            elif self.status == "paused" and any(
                    isinstance(item, dict)
                    and item.get("reason") == "required_stage_window_does_not_fit_remaining_deadline"
                    for item in self.blockers):
                stop_reason = "required_stage_window_does_not_fit_remaining_deadline"
            elif self.status == "paused" and any(
                    isinstance(item, dict)
                    and item.get("reason") == "provider_cooldown"
                    for item in self.blockers):
                stop_reason = "provider_cooldown"
            else:
                stop_reason = self.status
        organization = self.organization_snapshot if isinstance(self.organization_snapshot, dict) else {}
        open_orders = organization.get("open_work_orders", [])
        if not isinstance(open_orders, list):
            open_orders = []
        orders = []
        for item in open_orders[:8]:
            if not isinstance(item, dict):
                continue
            orders.append({key: item[key] for key in (
                "task_id", "department", "state", "kind", "objective") if key in item})
        blockers = []
        for item in self.blockers[-3:]:
            if isinstance(item, dict):
                blockers.append({key: item[key] for key in (
                    "stage_id", "reason", "provider_error", "retry_after_seconds",
                    "retry_after_epoch", "rate_limit", "attempts", "usage",
                    "diagnostics", "candidate_attempt_trace", "maturity_review_history",
                    "rejected_topic_history") if key in item})
            else:
                blockers.append({"reason": str(item)[:240]})
        active_blockers = self._active_blockers()
        report = {
            "schema_version": "composer-interim-report-1",
            "workflow_id": self.workflow["id"],
            "run_id": self.run_id,
            "status": self.status,
            "stop_reason": stop_reason,
            "last_phase": self._progress_snapshot.get("phase", self.status),
            "elapsed_seconds": max(0.0, now - self.started),
            "remaining_seconds": max(0.0, remaining),
            "deadline_seconds": self.workflow["time_policy"]["hard_seconds"],
            "started_at_epoch": self.started_epoch,
            "deadline_at_epoch": self.deadline_epoch,
            "completed_stage_ids": completed,
            "active_stage_ids": active,
            "pending_stage_ids": pending,
            "held_stage_ids": held,
            "blocked_stage_ids": blocked,
            "stages": rows,
            "pending_work_orders": orders,
            "blockers": blockers,
            "active_blockers": deepcopy(active_blockers),
            "blocker_counts": {
                "active": len(active_blockers),
                "historical": len(self.blockers),
            },
            "deadline_decisions": deepcopy(self.deadline_decisions),
            "deadline_extensions": deepcopy(self.deadline_extensions),
            "next_actions": [
                "Extend the deadline and resume the same workflow."
                if stop_reason in {"hard_deadline", "required_stage_window_does_not_fit_remaining_deadline"}
                else "Inspect the blocker and resume the same workflow after the cause is recoverable.",
                "Resume reconciles interrupted attempts before dispatching a fresh isolated attempt.",
            ],
            "resume_hint": (
                "python -m scisaurus.cli run-composer --workflow "
                "<path-to-the-same-workflow.json> --resume --extend-deadline-seconds <N>"
            ),
        }
        if persist:
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            artifact_ref = None
            artifact_error = None
            if self.control is not None:
                try:
                    artifact = self._publish(
                        "command/composer/interim-report", "report", report, "command.composer")
                    artifact_ref = artifact["artifact_ref"]
                except Exception as exc:
                    artifact_error = f"{type(exc).__name__}: {exc}"
            persisted = {**report, "artifact_ref": artifact_ref,
                         "path": str((output / "interim_report.json").resolve())}
            if artifact_error:
                persisted["artifact_error"] = artifact_error
            temporary = output / f"interim-report-{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(canonical_bytes(persisted))
            temporary.replace(output / "interim_report.json")
            report = persisted
        return report

    def _active_blockers(self):
        """Return blockers that still constrain the current Composer frontier.

        ``blockers`` is an append-only audit trail.  Treating its length as a
        live health signal made every recovered scientific failure look like
        a current stop, even after the Composer admitted a new continuation.
        A blocker is live only while its owning stage remains held/blocked or
        the mission itself is paused/blocked. Forwarded findings and scoped
        recoveries stay in the audit trail but do not gate the next action.
        """
        held_statuses = STAGE_HOLD_STATUSES | {
            "blocked", "paused", "candidate_needs_review",
        }
        stage_status = {
            stage_id: record.get("status")
            for stage_id, record in self.stage_records.items()
            if isinstance(record, dict)
        }
        mission_held = self.status in held_statuses
        active = []
        for item in self.blockers:
            if not isinstance(item, dict):
                if mission_held:
                    active.append({"reason": str(item)[:240]})
                continue
            if (item.get("gating") is False
                    or item.get("release_blocking") is False
                    or item.get("recovery") == "cycle_admitted"
                    or item.get("disposition") == "forwarded_with_findings"):
                continue
            stage_id = item.get("stage_id")
            if stage_id is None:
                if mission_held:
                    active.append(deepcopy(item))
                continue
            if stage_status.get(stage_id) in held_statuses:
                active.append(deepcopy(item))
        return active

    def _retry_policy(self):
        """Return the Composer retry policy for this immutable run.

        ``until_deadline`` deliberately has no attempt-count stop condition.
        The hard wall and dependency admission remain the termination
        conditions; a residual stage may be admitted until the small
        control-plane margin, but it can never extend the mission wall.
        """
        configured = self.workflow.get("retry_policy") or {}
        policy = {**DEFAULT_RETRY_POLICY, **configured}
        # Before ``mode`` was introduced, the presence of ``max_attempts``
        # meant a bounded policy.  Preserve that unambiguous legacy contract
        # while an omitted policy gets the autonomous deadline-governed mode.
        if "mode" not in configured and "max_attempts" in configured:
            policy["mode"] = "bounded"
        if self._forward_first():
            # A forward-first mission never inherits an unbounded retry loop
            # from a legacy/template workflow. Even an explicit larger bounded
            # value is clamped: one blocker may not monopolize the provider
            # budget before the rest of the graph gets a turn.
            bounded = dict(FORWARD_FIRST_RETRY_POLICY)
            if isinstance(configured, dict) and type(configured.get("max_attempts")) is int:
                bounded["max_attempts"] = min(
                    FORWARD_FIRST_RETRY_POLICY["max_attempts"],
                    max(1, configured["max_attempts"]),
                )
            if isinstance(configured, dict) and type(configured.get("backoff_seconds")) in (int, float):
                bounded["backoff_seconds"] = max(0.0, float(configured["backoff_seconds"]))
            policy = bounded
        return policy

    def _continuation_policy(self):
        """Return the research-continuation policy for this immutable run."""
        configured = self.workflow.get("continuation_policy") or {}
        policy = {**DEFAULT_CONTINUATION_POLICY, **configured}
        # Preserve the pre-mode meaning of an explicitly supplied cycle count.
        if "mode" not in configured and "max_cycles" in configured:
            policy["mode"] = "bounded"
        if self._forward_first():
            bounded = dict(FORWARD_FIRST_CONTINUATION_POLICY)
            if isinstance(configured, dict) and type(configured.get("max_cycles")) is int:
                bounded["max_cycles"] = min(
                    FORWARD_FIRST_CONTINUATION_POLICY["max_cycles"],
                    max(0, configured["max_cycles"]),
                )
            policy = bounded
        return policy

    def _forward_first(self):
        """Whether this mission prioritizes a truthful next move over a hard stop.

        This is deliberately a Composer policy, not a relaxation of a stage's
        scientific claims. Release gates remain strict; the policy only
        decides what to do when the current attempt cannot produce an accepted
        artifact.
        """
        return self.workflow.get("progression_policy") == FORWARD_FIRST_POLICY

    def _allows_provisional_progress(self):
        """Whether the mission may carry an honest provisional draft."""
        return self.workflow.get("progression_policy") in {
            "full_pass", FORWARD_FIRST_POLICY,
        }

    def _agenda_policy(self):
        """Return how Composer chooses among dependency-ready research work.

        A workflow declaration remains a DAG of hard artifact dependencies.
        It is not interpreted as a preferred linear itinerary in adaptive
        mode. Workflows without an agenda declaration retain legacy ordered
        semantics; newly generated autonomous missions opt into adaptive mode.
        """
        configured = self.workflow.get("agenda_policy")
        if configured is not None:
            return {**DEFAULT_AGENDA_POLICY, **configured}
        if isinstance(self._restored_agenda_policy, dict):
            return deepcopy(self._restored_agenda_policy)
        if self._forward_first():
            return {"mode": "adaptive"}
        return deepcopy(DEFAULT_AGENDA_POLICY)

    def _autonomous_recovery_request(self, stage_id, context):
        """Synthesize a new scoped move when a scientific hold gives no order.

        A stage-level ``hold`` is a result of the current attempt, not an
        instruction for a person to decide what happens next.  Reviewers and
        legacy runners do not always emit a typed research request, so the
        control plane derives one from the stage kind and the recorded
        blocker.  The cycle-specific strategy changes the work itself and
        gives the next attempt a reason to diverge instead of replaying the
        same prompt.  Environmental failures are deliberately excluded: they
        are handled by the provider cooldown, quota, and deadline fences.
        """
        if not isinstance(context, dict):
            return None
        stage_record = self.stage_records.get(stage_id, {})
        stage_kind = context.get("kind") or stage_record.get("kind")
        if stage_kind not in {"topic_discovery", "survey", "experiment",
                              "interpretation", "argument", "paper"}:
            return None
        if context.get("status") not in STAGE_HOLD_STATUSES:
            return None

        cycle = self.continuation_cycles + 1
        strategies = {
            "topic_discovery": (
                "abandon the rejected framing or frontier seed when necessary and pivot to an orthogonal mechanism, boundary, or observable",
                "change the evidence mode and comparison while preserving only the supported scientific core",
                "reframe the question around a discriminating prediction and a declared analytic or computational limit",
                "search a neighboring phenomenon with a different measurement and an explicit falsification test",
            ),
            "survey": (
                "expand exact terminology and citation chaining from primary studies",
                "search contradictory findings and neighboring terminology, then capture full-text evidence",
                "trace methods and results spans from the most relevant papers instead of adding abstract-only records",
                "run a boundary-focused counter-search across an adjacent field and reconcile identities before admission",
            ),
            "experiment": (
                "run a discriminating control or null-model comparison",
                "sweep the declared boundary and test whether the effect survives the relevant regimes",
                "perform an analytic-limit and sensitivity check with an independent recalculation",
                "change the measurement or baseline so the competing explanations make different predictions",
            ),
            "interpretation": (
                "construct the strongest alternative mechanism and identify its discriminating observable",
                "separate observation from mechanism across the declared parameter regimes",
                "recalculate the interpretation at the analytic limit and state the boundary of inference",
                "relink every material claim to evidence and downgrade any explanation the data cannot distinguish",
            ),
            "argument": (
                "rebuild the claim-evidence graph around the strongest supported result",
                "reorder the narrative around the decisive comparison and expose the main limitation",
                "remove unsupported causal steps and add the missing mechanism-to-observation bridge",
                "write a competing explanation and show exactly which evidence favors or fails to favor it",
            ),
            "paper": (
                "recompose the manuscript around the accepted evidence and the unresolved reviewer findings",
                "repair the weakest claim-evidence links and make all scope boundaries explicit",
                "rebuild the results and discussion transitions so observations are not presented as mechanisms",
                "perform a publication-level rejection pass, then revise every retained blocking finding",
            ),
        }
        strategy = strategies[stage_kind][(cycle - 1) % len(strategies[stage_kind])]

        evidence = []
        verifier = context.get("specialist_verifier")
        response = verifier.get("response") if isinstance(verifier, dict) else None
        if not isinstance(response, dict):
            response = {}
        for key in ("repair_scope", "critical_findings", "required_changes"):
            value = response.get(key)
            if isinstance(value, list):
                evidence.extend(str(item).strip() for item in value if str(item).strip())
        for key in ("rationale", "error", "gap_state", "topic_admission", "review_status"):
            value = response.get(key) if key in response else context.get(key)
            if isinstance(value, str) and value.strip():
                evidence.append(value.strip())
        evidence_text = "; ".join(dict.fromkeys(evidence))
        if not evidence_text:
            evidence_text = "the current stage returned a scientific hold without a typed repair request"
        evidence_text = evidence_text[:1800]

        base = re.sub(r"[^a-z0-9_.-]+", "-", str(stage_id).casefold()).strip("-")
        base = base[:24] or "stage"
        request_id = f"auto-{base}-recovery-{cycle}"
        if stage_kind == "topic_discovery":
            return {
                "id": request_id,
                "kind": "topic_refinement",
                "owner": "research.intelligence",
                "objective": (
                    f"Generate and independently review a materially different research question: {strategy}."
                ),
                "why": (
                    "The topic stage produced a scientific hold without a complete typed repair order. "
                    f"Recovery cycle {cycle} must change the research direction, not its wording."
                ),
                "success_condition": (
                    "A source-grounded topic package addresses the recorded blocker, survives maturity and adversarial review, and is admitted before survey."
                ),
                "evidence_needed": evidence_text,
                "salvage_policy": "bounded_salvage_before_structural_pivot",
                "salvage_branch_limit": 3,
                "source_stage_id": stage_id,
            }
        if stage_kind == "survey":
            quota_recovery = isinstance(context.get("quota_recovery"), dict)
            return {
                "id": request_id,
                "kind": "literature_expansion",
                "owner": "research.intelligence",
                "objective": (
                    (
                        f"Resume the literature stage from accepted checkpoints with a narrower decisive gate: {strategy}. "
                        "Do not replay the exhausted catalog or repeat already accepted source work. "
                        "Spend the fresh allocation only on the missing gap decision."
                        if quota_recovery else
                        f"Re-run the literature stage with a changed retrieval strategy: {strategy}. "
                        "Update the gap assessment from identity-reconciled primary and full-text evidence."
                    )
                ),
                "why": (
                    f"The survey exhausted its local stage allocation before the decisive gate. Recorded blocker: {evidence_text}"
                    if quota_recovery else
                    f"The survey hold was not accompanied by an executable repair order. Recorded blocker: {evidence_text}"
                ),
                "success_condition": (
                    "The refreshed survey either establishes an experiment-worthy gap or emits evidence for a substantive topic redesign."
                ),
                "evidence_needed": "Primary-study records, citation-chain results, full-text spans, contradiction checks, and a refreshed gap assessment.",
                "source_stage_id": stage_id,
            }
        if stage_kind == "experiment":
            return {
                "id": request_id,
                "kind": "additional_experiment",
                "owner": "methods.validation",
                "objective": (
                    f"Run a fresh discriminating analysis or experiment: {strategy}. Preserve raw observations and independently recalculate the primary estimand."
                ),
                "why": f"The methods stage returned a scientific hold without a complete typed repair order. Recorded blocker: {evidence_text}",
                "success_condition": "The new declared control, boundary, or sensitivity result separates the retained explanations or records why it cannot.",
                "evidence_needed": "Versioned raw output, prespecified control or baseline, primary estimand, independent recalculation, and limitations.",
                "source_stage_id": stage_id,
            }
        if stage_kind in {"interpretation", "argument"}:
            request = {
                "id": request_id,
                "kind": "interpretation_expansion",
                "owner": "strategy.interpretation",
                "objective": (
                    f"Rebuild the scientific interpretation and downstream argument: {strategy}. Keep unsupported mechanisms provisional."
                ),
                "why": f"The strategy stage returned a scientific hold without a complete typed repair order. Recorded blocker: {evidence_text}",
                "success_condition": "Every material claim is linked to an observation or source, alternatives are separated, and the independent reviewer accepts the revised argument.",
                "evidence_needed": "Claim-evidence graph, competing explanations, boundary conditions, recalculated results, and explicit uncertainty language.",
                "source_stage_id": stage_id,
            }
            # Strategy owns the repair brief, but the stage that produced the
            # rejected packet owns execution.  In particular, an argument
            # review must reopen argument before an unrelated survey order;
            # the department kind alone cannot express that distinction.
            request.update({
                "target_stage_id": stage_id,
                "target_stage_kind": stage_kind,
                "repair_priority": "immediate",
            })
            return request
        return {
            "id": request_id,
            "kind": "manuscript_revision",
            "owner": "editorial.composer",
            "objective": (
                f"Recompose and re-review the manuscript after the scientific hold: {strategy}. Preserve accepted results and repair the evidence chain."
            ),
            "why": f"The paper stage returned a hold without a complete typed repair order. Recorded blocker: {evidence_text}",
            "success_condition": "The independent reviewer and editor accept the revised manuscript with no unresolved blocking finding or research request.",
            "evidence_needed": "Current manuscript, reviewer findings, accepted evidence map, revision ledger, and a fresh rendered PDF check.",
            "source_stage_id": stage_id,
        }

    def _continuation_requests(self):
        """Collect validated research requests that can reopen a scoped stage.

        A request is a scientific work order, not a prose edit.  The specialist
        runners validate its shape before it reaches this method; the Composer
        copies only the stable public fields into its control plan.  A malformed
        request is a durable rejection, not a reason to abort an otherwise
        recoverable run.
        """
        requests = []
        seen = set()
        for stage_id, context in self.context.items():
            if not isinstance(context, dict):
                continue
            containers = []
            for label in (
                    "research_expansion_requests", "research_requests",
                    "deferred_research_requests"):
                if label not in context or context[label] is None:
                    continue
                value = context[label]
                containers.append((label, value))
            for label, candidates in containers:
                if not isinstance(candidates, list):
                    rejection = self.departments.reject_request(
                        candidates, source_stage_id=stage_id,
                        reason=f"{label} must be a list when supplied",
                    )
                    self.department_activity.append({
                        "action": "reject_work_order", "stage_id": stage_id,
                        "request_id": rejection["request_id"], "rejection": rejection,
                    })
                    continue
                for request in candidates:
                    raw_request = request
                    if label == "deferred_research_requests":
                        request = self._forward_debt_work_order(stage_id, request)
                        if request is None:
                            rejection = self.departments.reject_request(
                                raw_request, source_stage_id=stage_id,
                                reason="deferred failure debt could not be mapped to a stage work order",
                            )
                            self.department_activity.append({
                                "action": "reject_work_order", "stage_id": stage_id,
                                "request_id": rejection["request_id"], "rejection": rejection,
                            })
                            continue
                    if not isinstance(request, dict):
                        rejection = self.departments.reject_request(
                            request, source_stage_id=stage_id,
                            reason="work-order request must be an object",
                        )
                        self.department_activity.append({
                            "action": "reject_work_order", "stage_id": stage_id,
                            "request_id": rejection["request_id"], "rejection": rejection,
                        })
                        continue
                    item = {key: deepcopy(request.get(key)) for key in (
                        "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed")}
                    item["schema_version"] = "department-work-order-1"
                    try:
                        self.departments.validate_request(item)
                    except ValidationError as exc:
                        rejection = self.departments.reject_request(
                            request, source_stage_id=stage_id,
                            reason=f"{type(exc).__name__}: {exc}",
                        )
                        self.department_activity.append({
                            "action": "reject_work_order", "stage_id": stage_id,
                            "request_id": rejection["request_id"], "rejection": rejection,
                        })
                        continue
                    request_id = item["id"]
                    if request_id in seen:
                        continue
                    item.pop("schema_version", None)
                    # These fields are controller metadata rather than part
                    # of the public department proposal schema. Keep them on
                    # the Composer work order so the next specialist sees the
                    # exact dossier and executable repair commands, while the
                    # department still validates the stable v1 proposal.
                    for key in (
                            "failure_dossier_ref", "failure_input_sha256",
                            "repair_commands", "acceptance_checks", "review_directives",
                            "model_diagnostics", "recovery_mode", "target_stage_id",
                            "target_stage_kind", "repair_priority", "experiment_repair_plan",
                            "repair_strategy", "attempt_lineage"):
                        if key in request:
                            item[key] = deepcopy(request[key])
                    item["source_stage_id"] = stage_id
                    topic_identity = self._current_topic_identity()
                    if topic_identity is not None:
                        item["topic_id"] = topic_identity["topic_id"]
                        item["topic_cycle"] = topic_identity["topic_cycle"]
                    if self._research_request_signature(item) in self._attempted_request_signatures:
                        continue
                    requests.append(item)
                    seen.add(request_id)
            if context.get("status") in STAGE_HOLD_STATUSES:
                # A hold with a valid request is routed as emitted.  If the
                # request was malformed, empty, or already attempted, create
                # a cycle-specific strategy so the lab still has a concrete
                # next move instead of escalating an ordinary scientific
                # judgment to a person.
                stage_requests = [
                    item for item in requests
                    if isinstance(item, dict) and item.get("source_stage_id") == stage_id
                ]
                if not stage_requests:
                    item = self._autonomous_recovery_request(stage_id, context)
                    if (isinstance(item, dict)
                            and item.get("id") not in seen
                            and self._research_request_signature(item)
                            not in self._attempted_request_signatures):
                        requests.append(item)
                        seen.add(item["id"])
        # A topic refinement replaces the downstream closure. Do not carry a
        # survey/experiment repair order from the superseded frontier into the
        # new topic; the next survey and experiment must be derived from the
        # newly admitted question.
        topic_requests = [
            item for item in requests
            if isinstance(item, dict) and item.get("kind") == "topic_refinement"
        ]
        return topic_requests if topic_requests else requests

    @staticmethod
    def _continuation_targets(requests, by_id):
        """Map research work orders to the smallest stage closure that can answer them."""
        target_kinds = set()
        target_ids = set()
        for request in requests:
            explicit_stage_id = request.get("target_stage_id") if isinstance(request, dict) else None
            if isinstance(explicit_stage_id, str) and explicit_stage_id in by_id:
                target_ids.add(explicit_stage_id)
                continue
            kind = request.get("kind")
            owner = request.get("owner")
            if kind == "topic_refinement":
                target_kinds.add("topic_discovery")
            elif kind in {"literature_expansion", "full_text_retrieval"} or owner == "research.intelligence":
                target_kinds.add("survey")
            elif kind in {"additional_experiment", "analysis_display", "analysis_repair"} \
                    or owner == "methods.validation":
                target_kinds.add("experiment")
            elif kind == "interpretation_expansion" or owner == "strategy.interpretation":
                target_kinds.add("interpretation")
            elif kind == "manuscript_revision" or owner == "editorial.composer":
                target_kinds.add("paper")
        targets = set(target_ids)
        targets.update(
            stage_id for stage_id, stage in by_id.items()
            if stage["kind"] in target_kinds and stage_id not in targets
        )
        if not targets:
            return set()
        # Rerun every downstream consumer because its bound packet may have
        # changed.  The closure is deterministic and keeps unrelated branches
        # intact.
        changed = True
        while changed:
            changed = False
            for stage_id, stage in by_id.items():
                if stage_id in targets:
                    continue
                if set(stage["depends_on"]) & targets:
                    targets.add(stage_id)
                    changed = True
        return targets

    def _record_continuation(self, requests, reopened_stage_ids, *,
                             budget_override=False, budget_override_kind=None):
        """Persist one continuation decision and route its work orders."""
        cycle = self.continuation_cycles
        owners = {item.get("owner") for item in requests}
        if len(owners) == 1 and next(iter(owners)):
            recipient = self._address_for_role(next(iter(owners)))
        else:
            recipient = COMMAND_ADDRESSES["arbiter"]
        feedback = {
            "event_id": f"composer-continuation-{cycle}-{uuid.uuid4().hex}",
            "stage_id": "workflow",
            "role": "executive-command",
            "from": COMMAND_ADDRESSES["progress"],
            "to": recipient,
            "status": "continuing",
            "action": "continue_research",
            "cycle": cycle,
            "max_cycles": self._continuation_policy()["max_cycles"],
            "budget_baseline": self._continuation_budget_baseline,
            "budget_used": self._continuation_budget_used(),
            "cycle_budget_override": (
                budget_override_kind or "autonomous_experiment_repair"
                if budget_override else None
            ),
            "research_requests": deepcopy(requests),
            "reopened_stage_ids": sorted(reopened_stage_ids),
            "next_condition": "complete each scoped work order, rerun downstream interpretation and review, and re-evaluate the release gate",
            "message_id": f"composer-continuation-{cycle}-{uuid.uuid4().hex}",
            "message_disposition": "scheduled",
        }
        self.feedback.append(feedback)
        note = self._publish(
            f"command/composer/continuation/{cycle}", "decision_note", feedback, "command.composer")
        self._route_feedback(feedback, note)

    def _continuation_budget_used(self):
        """Return continuation cycles consumed by the current process lease."""
        return max(0, int(self.continuation_cycles or 0)
                   - int(self._continuation_budget_baseline or 0))

    def _begin_continuation(self, completed, by_id):
        """Reopen the affected closure after a research or review request."""
        # A continuation is new scientific work.  Check the immutable mission
        # wall before publishing its decision or activating any work order.
        self._remaining()
        requests = self._continuation_requests()
        experiment_repair_override = (
            self._forward_first()
            and any(self._is_experiment_repair_request(item) for item in requests)
        )
        topic_pivot_override = (
            self._forward_first()
            and any(
                isinstance(item, dict) and item.get("kind") == "topic_refinement"
                for item in requests
            )
        )
        continuation_budget_override = experiment_repair_override or topic_pivot_override
        policy = self._continuation_policy()
        if (policy.get("mode", "bounded") == "bounded"
                and self._continuation_budget_used() >= policy["max_cycles"]
                and not continuation_budget_override):
            return False
        targets = self._continuation_targets(requests, by_id)
        if not requests or not targets:
            return False
        retired_work_orders = self.departments.retire_superseded_work_orders(
            {item.get("id") for item in requests if isinstance(item, dict)},
            reason="superseded by the current scoped continuation",
        )
        self.continuation_cycles += 1
        self.active_research_requests = requests
        self.reopened_stage_ids = set(targets)
        self.continuation_pending_stage_ids = set(targets)
        # A continuation is a new dispatch boundary.  A prior child may have
        # been interrupted after its last stage checkpoint, leaving the old
        # aggregate record marked ``running`` even though no provider call is
        # alive.  Clear that live projection before the new agenda is
        # published; the next actual admission installs a fresh assignment.
        for target_id in targets:
            record = self.stage_records.get(target_id)
            if not isinstance(record, dict):
                continue
            if record.get("status") == "running":
                record["status"] = "retrying"
            retired = self._retire_stage_assignment(record)
            record.update(retired)
            self.stage_records[target_id] = record
        completed.difference_update(targets)
        retired_stage_tasks = self._retire_superseded_stage_tasks(
            targets,
            reason="superseded by a newly admitted continuation",
        )
        self._record_continuation(
            requests, targets, budget_override=continuation_budget_override,
            budget_override_kind=(
                "autonomous_experiment_repair" if experiment_repair_override
                else "adaptive_topic_pivot" if topic_pivot_override
                else None
            ))
        if experiment_repair_override:
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "extend_experiment_repair_lease",
                "reason": "a generated capability has not produced an observation; continue only within mission and stage fences",
                "work_order_ids": [item.get("id") for item in requests
                                   if self._is_experiment_repair_request(item)],
                "continuation_policy": deepcopy(policy),
            })
        if topic_pivot_override:
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "extend_topic_pivot_lease",
                "reason": "a new research direction changes the frontier; keep searching within mission and stage fences",
                "work_order_ids": [item.get("id") for item in requests
                                   if isinstance(item, dict)
                                   and item.get("kind") == "topic_refinement"],
                "continuation_policy": deepcopy(policy),
            })
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "activate_work_orders",
            "work_orders": self.departments.activate_work_orders(requests),
        })
        if retired_work_orders:
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "retire_superseded_work_orders",
                "work_orders": retired_work_orders,
            })
        if retired_stage_tasks:
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "retire_superseded_stage_tasks",
                "stage_tasks": retired_stage_tasks,
            })
        self._checkpoint(f"continuation:{self.continuation_cycles}:admitted", force=True)
        return True

    @staticmethod
    def _survey_checkpoint(project_dir):
        """Return the durable survey milestone recorded in one workspace."""
        if not isinstance(project_dir, (str, Path)):
            return None
        root = Path(project_dir).resolve()
        if not (root / "state" / "control.sqlite").is_file():
            return None
        output = root / "output" / "run.json"
        if not output.is_file():
            return None
        try:
            payload = json.loads(output.read_text())
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("survey_current") is not True or not payload.get("survey_ref"):
            return None
        # A current survey without a current assessment is an honest
        # resumable frontier.  A fully assessed survey is a released upstream
        # dependency and should be left alone when another continuation is
        # admitted for a downstream scope.
        if payload.get("assessment_current") is True and payload.get("assessment_ref"):
            return None
        return payload

    def _latest_resumable_survey_project(self, stage):
        """Find the newest accepted survey checkpoint before making a cycle.

        Reopened survey stages are not ordinary stateless workers.  The
        survey runner owns a source catalogue, identity ledger, and accepted
        map; creating a fresh cycle directory after a gap-assessment failure
        discards all of that work and silently turns a scoped repair into a
        full literature restart.  Prefer the checkpoint named by the live
        context, then walk the immutable Composer attempt ledger newest-first.
        """
        if not isinstance(stage, dict) or stage.get("kind") != "survey":
            return None
        candidates = []
        context = self.context.get(stage.get("id"), {})
        if isinstance(context, dict):
            candidates.append(context.get("project_dir"))
        record = self.stage_records.get(stage.get("id"), {})
        attempts = record.get("attempts", []) if isinstance(record, dict) else []
        if isinstance(attempts, list):
            candidates.extend(
                attempt.get("project_dir")
                for attempt in reversed(attempts)
                if isinstance(attempt, dict)
            )
        candidates.append(stage.get("project_dir"))
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            resolved = str(Path(candidate).resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            if self._survey_checkpoint(resolved) is not None:
                return Path(resolved)
        return None

    def _stage_for_cycle(self, stage):
        """Route reopened work without discarding a resumable survey ledger."""
        if stage["id"] not in self.reopened_stage_ids:
            return stage
        candidate = deepcopy(stage)
        if stage.get("kind") == "survey":
            resumable = self._latest_resumable_survey_project(stage)
            if resumable is not None:
                candidate["project_dir"] = str(resumable)
                candidate["reuse_completed"] = False
                candidate["reuse_output_path"] = None
                return candidate
        base = Path(stage["project_dir"]).resolve()
        candidate["project_dir"] = str(
            base / "continuations" / f"cycle-{self.continuation_cycles}")
        candidate["reuse_completed"] = False
        candidate["reuse_output_path"] = None
        return candidate

    def _runtime_context(self, model):
        """Expose safe, actionable execution capabilities to topic selection.

        This is intentionally a capability inventory rather than a dump of the
        process environment: credentials, tokens, and unrelated host variables
        never enter a model prompt.  The selected topic is therefore judged
        against the tools that the actual Composer can call.
        """
        executable_names = ("python3", "git", "tectonic", "pdflatex", "latexmk",
                            "node", "npm", "Rscript", "julia")
        package_names = ("numpy", "scipy", "pandas", "sklearn", "matplotlib",
                         "statsmodels", "sympy", "torch", "tensorflow")
        try:
            from importlib.util import find_spec
            packages = {name: find_spec(name) is not None for name in package_names}
        except (ImportError, ModuleNotFoundError, ValueError):
            packages = {name: False for name in package_names}
        project_files = []
        ignored_roots = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules"}
        inventory_roots = [(self.root, "composer")]
        for stage in self.workflow["stages"]:
            inventory_roots.append((Path(stage["project_dir"]).resolve(), f"stage:{stage['id']}"))
        seen_files = set()
        for stage in self.workflow["stages"]:
            descriptor = Path(stage["config_path"]).resolve()
            if descriptor.is_file() and str(descriptor) not in seen_files:
                seen_files.add(str(descriptor))
                project_files.append(f"config:{stage['id']}/{descriptor.name}")
                if len(project_files) >= 100:
                    break
        try:
            for base, label in inventory_roots:
                if not base.exists():
                    continue
                for path in sorted(base.rglob("*")):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(base)
                    if relative.parts and (relative.parts[0] in {"state", "objects"}
                                           or relative.parts[0] in ignored_roots
                                           or any(part in ignored_roots for part in relative.parts)):
                        continue
                    identity = str(path.resolve())
                    if identity in seen_files:
                        continue
                    seen_files.add(identity)
                    project_files.append(f"{label}/{relative}")
                    if len(project_files) >= 100:
                        break
                if len(project_files) >= 100:
                    break
        except OSError:
            project_files = []
        experiment_contract = None
        experiment_catalog = []
        catalog = self.workflow.get("experiment_catalog") or []
        for entry in catalog:
            try:
                configured = json.loads(Path(entry["config_path"]).read_text())
            except (OSError, ValueError) as exc:
                raise ValidationError(
                    f"experiment capability template is unreadable: {entry['config_path']}") from exc
            experiment = configured.get("experiment") if isinstance(configured, dict) else None
            if not isinstance(experiment, dict):
                raise ValidationError("experiment capability template must contain an experiment object")
            experiment_catalog.append({
                "id": entry["id"],
                "study_type": experiment.get("study_type"),
                "domain": experiment.get("domain"),
                "research_question": experiment.get("research_question"),
                "method": experiment.get("method"),
                "stopping_rule": experiment.get("stopping_rule"),
                "primary_outcomes": [
                    {key: outcome.get(key) for key in ("id", "definition", "unit")}
                    for outcome in experiment.get("primary_outcomes", [])
                    if isinstance(outcome, dict)
                ],
                "required_asset_roles": [
                    {key: asset.get(key) for key in ("role", "media_types", "min_count")}
                    for asset in experiment.get("required_assets", [])
                    if isinstance(asset, dict)
                ],
                "execution_adapter": (experiment.get("execution") or {}).get("adapter"),
                "design_driven": bool((experiment.get("parameters") or {}).get("design_driven")),
                "design_template": (
                    (experiment.get("execution") or {}).get("input") or {}).get("design"),
            })
        experiment_stages = [stage for stage in self.workflow["stages"] if stage["kind"] == "experiment"]
        if experiment_stages and not catalog:
            try:
                configured = json.loads(Path(experiment_stages[0]["config_path"]).read_text())
                experiment = configured.get("experiment") if isinstance(configured, dict) else None
                if isinstance(experiment, dict):
                    experiment_contract = {
                        "study_type": experiment.get("study_type"),
                        "research_question": experiment.get("research_question"),
                        "method": experiment.get("method"),
                        "stopping_rule": experiment.get("stopping_rule"),
                        "primary_outcomes": [
                            {key: outcome.get(key) for key in ("id", "definition", "unit")}
                            for outcome in experiment.get("primary_outcomes", [])
                            if isinstance(outcome, dict)
                        ],
                        "required_asset_roles": [
                            {key: asset.get(key) for key in ("role", "media_types", "min_count")}
                            for asset in experiment.get("required_assets", [])
                            if isinstance(asset, dict)
                        ],
                        "execution_adapter": (experiment.get("execution") or {}).get("adapter"),
                    }
            except (OSError, ValueError, TypeError):
                experiment_contract = None
        foundry_enabled = bool(self.workflow.get("capability_foundry_config_path"))
        foundry_runtime_packages = []
        foundry_max_attempts = None
        foundry_timeout_seconds = None
        if foundry_enabled:
            try:
                foundry_config = json.loads(
                    Path(self.workflow["capability_foundry_config_path"]).read_text())
                foundry_max_attempts = foundry_config.get("max_attempts")
                foundry_timeout_seconds = foundry_config.get("timeout_seconds")
                foundry_runtime_packages = [
                    {"name": item["name"], "version": item["version"]}
                    for item in foundry_config.get("runtime_packages", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("name"), str)
                    and isinstance(item.get("version"), str)
                ]
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise ValidationError(
                    "capability foundry runtime package inventory is unreadable") from exc
            # These packages are installed in the isolated foundry runtime,
            # which is the execution boundary for the generated program. The
            # host interpreter may intentionally not have the same packages.
            for item in foundry_runtime_packages:
                packages[item["name"]] = True
        stage_deadlines_seconds = {
            stage["id"]: float(stage["deadline_seconds"])
            for stage in self.workflow["stages"]
        }
        experiment_deadlines = [
            stage["deadline_seconds"] for stage in self.workflow["stages"]
            if stage["kind"] == "experiment"
        ]
        survey_sources = {
            "metadata": False, "full_text": False, "max_api_requests": None,
        }
        for stage in self.workflow["stages"]:
            if stage["kind"] != "survey":
                continue
            try:
                descriptor = json.loads(Path(stage["config_path"]).read_text())
                survey = descriptor.get("survey", {}) if isinstance(descriptor, dict) else {}
                if not isinstance(survey, dict):
                    survey = {}
                search = survey.get("search", {}) if isinstance(survey, dict) else {}
                survey_sources.update({
                    "metadata": isinstance(survey.get("bibliography"), dict),
                    "full_text": isinstance(survey.get("full_text"), dict),
                    "max_api_requests": search.get("max_api_calls")
                    if type(search.get("max_api_calls")) is int else None,
                })
            except (OSError, ValueError, TypeError):
                # The stage validator remains authoritative for unreadable
                # descriptors; topic context should not expose a partial path
                # dump or turn an inventory failure into a scientific claim.
                pass
            break
        if foundry_enabled:
            feasibility_boundary = {
                "execution_modes": ["foundry"],
                "allowed_input_kinds": ["analytical_parameters", "synthetic"],
                "allowed_data_access": ["closed_world"],
                "network_access": False,
                "undeclared_data": False,
                "max_external_requests": 0,
                "max_model_calls": 0,
            }
        else:
            feasibility_boundary = {
                "execution_modes": ["configured_program", "project_runner"],
                "allowed_input_kinds": ["project_artifact", "synthetic"],
                "allowed_data_access": ["closed_world", "project_local"],
                "network_access": False,
                "undeclared_data": False,
                # Literature-provider capacity is not experiment capacity. The
                # current project-runner boundary is closed-world and cannot
                # smuggle a network/API dependency through the topic plan.
                "max_external_requests": 0,
                "max_model_calls": 0,
            }
        feasibility_boundary.update({
            "schema_version": "research-feasibility-1",
            "allowed_evidence_modes": (
                ["analytical_derivation", "synthetic_simulation"]
                if foundry_enabled else list(EVIDENCE_MODE_VALUES)
            ),
            "available_executables": sorted(
                name for name in executable_names if shutil.which(name) is not None
            ),
            "available_packages": sorted(
                name for name, present in packages.items() if present
            ),
            "stage_deadlines_seconds": stage_deadlines_seconds,
            "max_experiment_seconds": min(
                [*experiment_deadlines, *([foundry_timeout_seconds]
                 if foundry_enabled and isinstance(foundry_timeout_seconds, (int, float))
                 else [])]
            ) if experiment_deadlines or foundry_timeout_seconds is not None else None,
            "survey_sources": survey_sources,
        })
        return {
            "operating_system": platform.system(),
            "platform": platform.machine(),
            "python_version": sys.version.split()[0],
            "current_date": time.strftime("%Y-%m-%d", time.gmtime()),
            "executables": {name: shutil.which(name) is not None for name in executable_names},
            "python_packages": packages,
            "model_protocol": model.get("protocol") if isinstance(model, dict) else None,
            "model_name": model.get("model") if isinstance(model, dict) else None,
            "configured_stage_kinds": [stage["kind"] for stage in self.workflow["stages"]],
            "experiment_contract": experiment_contract,
            # A foundry-backed mission defines the scientific question before
            # execution design. Frozen templates remain an auditable fallback
            # inventory but cannot seed or constrain topic generation.
            "experiment_catalog": [] if foundry_enabled else experiment_catalog,
            "fallback_experiment_catalog": experiment_catalog if foundry_enabled else [],
            "capability_foundry": ({
                "enabled": True,
                "execution_boundary": "deterministic seeded Python with no network or subprocess access",
                "runtime_packages": foundry_runtime_packages,
                "max_attempts": foundry_max_attempts,
                "timeout_seconds": foundry_timeout_seconds,
                "allowed_evidence_modes": ["analytical_derivation", "synthetic_simulation"],
                "admission_gates": [
                    "static scan", "sandbox execution", "deterministic replay",
                    "test-vector digest", "independent recalculation", "adversarial review",
                ],
            } if foundry_enabled else {"enabled": False}),
            "topic_exclusions": self._effective_topic_exclusions(),
            "topic_history": self._topic_history_context(),
            "topic_preferences": deepcopy(self.workflow.get("topic_preferences") or {}),
            "project_files": project_files,
            "project_scoped_execution": True,
            # A survey-only/free-topic workflow has no experiment boundary to
            # validate yet.  Leave its topic package on the legacy contract;
            # experiment-backed missions receive the strict input/runtime
            # feasibility gate above.
            "research_feasibility": feasibility_boundary if experiment_stages else None,
        }

    def _topic_sampling_seed(self, *, attempt_number=0):
        """Derive a stable but attempt-diverse topic sampling seed.

        Resume keeps the mission seed, while each isolated topic intake gets
        its own deterministic branch. Without the attempt component, a
        provider that honors the seed can replay the same rejected direction
        indefinitely even after the Composer has persisted its reason.
        """
        material = (
            f"{self.exploration_seed}:topic:{self.continuation_cycles}:"
            f"attempt:{attempt_number}"
        ).encode("utf-8")
        # The digest is intentionally wide for entropy, then reduced to the
        # signed-int64 range accepted by the configured model endpoint.
        from scisaurus.runtime.models import MAX_PROVIDER_SEED
        return int(hashlib.sha256(material).hexdigest()[:16], 16) % MAX_PROVIDER_SEED

    def _resolve_topic_history_path(self):
        """Resolve the durable history shared by a family of Composer runs.

        A Composer project is intentionally immutable and each fresh mission
        gets its own project directory.  Topic memory therefore lives beside
        that directory, rather than inside a mutable stage checkpoint.  A
        workflow may pin an explicit path; otherwise the parent of the run
        family receives a hidden, project-local history file.
        """
        configured = self.workflow.get("topic_history_path")
        if isinstance(configured, str) and configured.strip():
            return Path(configured).expanduser().resolve()
        family_root = self.root.parent.parent
        # Avoid turning a shallow project path such as ``/tmp/composer`` into
        # a write to the filesystem root.  Normal run families (for example
        # ``project/.runs/run/composer``) still share the stable ``.runs``
        # parent; a shallow project keeps history beside its composer root.
        if family_root == Path(self.root.anchor or "/"):
            family_root = self.root.parent
        if family_root == Path(self.root.anchor or "/"):
            family_root = Path.cwd()
        return (family_root / ".scisaurus-topic-history.json").resolve()

    def _topic_history_scope_key(self):
        """Keep topic memory stable across harmless objective/catalog edits."""
        material = {
            "workflow_id": self.workflow["id"],
            "stage_graph": [{"id": item["id"], "kind": item["kind"],
                             "depends_on": sorted(item.get("depends_on", []))}
                            for item in self.workflow["stages"]],
        }
        return hashlib.sha256(canonical_bytes(material)).hexdigest()

    @staticmethod
    def _validate_topic_history_document(document):
        """Validate the small append-only topic-history envelope."""
        if not isinstance(document, dict) or document.get("schema_version") != "topic-history-1":
            raise ValidationError("topic history has an unsupported schema")
        scopes = document.get("scopes")
        if not isinstance(scopes, dict):
            raise ValidationError("topic history scopes must be an object")
        for scope_key, scope in scopes.items():
            if not isinstance(scope_key, str) or not isinstance(scope, dict):
                raise ValidationError("topic history contains an invalid scope")
            entries = scope.get("entries", [])
            if not isinstance(entries, list):
                raise ValidationError("topic history entries must be a list")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValidationError("topic history entry must be an object")
                if not isinstance(entry.get("topic_id"), str) or not entry["topic_id"].strip():
                    raise ValidationError("topic history entry requires topic_id")
                for key in ("title", "research_question", "domain"):
                    if key in entry and entry[key] is not None and not isinstance(entry[key], str):
                        raise ValidationError(f"topic history entry {key} must be a string")
        return document

    def _load_topic_history(self):
        """Load the current objective's topic memory, or an empty scope."""
        empty = {"schema_version": "topic-history-1", "scope_key": self.topic_history_scope,
                 "entries": [], "capability_counts": {}}
        path = self.topic_history_path
        if not path.is_file():
            return empty
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"topic history is unreadable: {path}") from exc
        self._validate_topic_history_document(document)
        if self.workflow.get("topic_history_path"):
            # An explicit history path is itself the mission-family boundary.
            # Preserve prior directions across harmless objective wording or
            # capability-inventory revisions instead of silently reopening
            # already explored topics under a new hash scope.
            entries = [deepcopy(entry)
                       for scope in document["scopes"].values()
                       for entry in scope.get("entries", [])]
        else:
            scope = document["scopes"].get(self.topic_history_scope, {})
            if not isinstance(scope, dict):
                raise ValidationError("topic history scope is invalid")
            entries = deepcopy(scope.get("entries", []))
            if not isinstance(entries, list):
                raise ValidationError("topic history scope entries must be a list")
        # Keep the durable file complete.  Prompt projections are bounded in
        # `_topic_history_context`, but an old attempt remains available for
        # deterministic repeat checks and audit.
        counts = {}
        for entry in entries:
            capability = entry.get("experiment_capability_id")
            if isinstance(capability, str) and capability:
                counts[capability] = counts.get(capability, 0) + 1
        return {"schema_version": "topic-history-1", "scope_key": self.topic_history_scope,
                "entries": entries, "capability_counts": counts}

    def _load_runtime_environment(self):
        """Load the workflow's owner-local environment before any model call."""
        configured = list(self.workflow.get("runtime_env_files", []))
        repository = Path(__file__).resolve().parents[2]
        project_root = Path(self.workflow["project_id"]).resolve()
        local_private = repository / "local-private"
        owner_local_project = project_root == local_private or local_private in project_root.parents
        # ``runtime_env_files`` was added after several long-lived missions
        # had already been generated.  Load the repository-owned credentials
        # as a compatibility path so a resume cannot silently downgrade an
        # authenticated OpenAlex run to ``auth_required`` merely because its
        # descriptor is old.
        if owner_local_project:
            for path in default_runtime_environment_files(repository):
                if path not in configured:
                    configured.append(path)
        load_runtime_environment_files(configured)
        # Topic and survey descriptors intentionally retain their stable,
        # stage-local paths for auditability. The actual provider budget is
        # account-wide, so a Composer run also exposes one shared ledger and
        # one reusable topic cache to all of its child runners. Test and
        # projectless temporary workflows stay isolated unless they live under
        # this checkout's owner-local mission area.
        if owner_local_project:
            os.environ.setdefault(
                "SCISAURUS_OPENALEX_SHARED_RATE_STATE_PATH",
                str(default_openalex_shared_state_path(repository)),
            )
            os.environ.setdefault(
                "SCISAURUS_OPENALEX_SHARED_TOPIC_CACHE_PATH",
                str(default_openalex_shared_topic_cache_path(repository)),
            )

    def _migrate_legacy_topic_history(self):
        """Remove contract failures from the pre-typed rejection memory.

        Older Composer runs recorded every intake ``ValidationError`` as a
        rejection.  That mixed provider/contract failures with scientific
        novelty and maturity decisions, so a malformed response could become
        a durable exclusion and distort later topic selection.  Semantic
        entries are retained under their typed rejection kind; non-semantic
        legacy entries are removed because they are not evidence against a
        research direction.
        """
        path = self.topic_history_path
        if not path.is_file():
            return
        lock_path = Path(str(path) + ".lock")
        lock = lock_path.open("a+")
        flock = None
        temporary = None
        try:
            try:
                import fcntl
                flock = fcntl
                flock.flock(lock.fileno(), flock.LOCK_EX)
            except ImportError as exc:
                raise ValidationError(
                    "topic history requires an interprocess file lock") from exc
            try:
                document = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise ValidationError(f"topic history is unreadable: {path}") from exc
            self._validate_topic_history_document(document)
            from scisaurus.runtime.topic_discovery import (
                _TOPIC_SEMANTIC_REJECTION_TYPES,
                _topic_validation_rejection_type,
            )
            changed = False
            for scope in document["scopes"].values():
                migrated_entries = []
                for entry in scope.get("entries", []):
                    if entry.get("rejection_type") != "intake_validation":
                        migrated_entries.append(entry)
                        continue
                    rejection_type = _topic_validation_rejection_type(
                        entry.get("rejection_reason"))
                    if rejection_type not in _TOPIC_SEMANTIC_REJECTION_TYPES:
                        changed = True
                        continue
                    if rejection_type != entry.get("rejection_type"):
                        entry = deepcopy(entry)
                        entry["rejection_type"] = rejection_type
                        changed = True
                    migrated_entries.append(entry)
                scope["entries"] = migrated_entries
            if not changed:
                return
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(canonical_bytes(document))
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            if flock is not None:
                try:
                    flock.flock(lock.fileno(), flock.LOCK_UN)
                except OSError:
                    pass
            lock.close()

    def _effective_topic_exclusions(self):
        """Merge configured exclusions with automatic recent-direction memory."""
        configured = self.workflow.get("topic_exclusions") or {}
        capability_ids = list(configured.get("capability_ids", [])) if isinstance(configured, dict) else []
        topic_ids = list(configured.get("topic_ids", [])) if isinstance(configured, dict) else []
        entries = self.topic_history.get("entries", [])
        # A model-generated portfolio slot is not a durable scientific
        # identity.  Keep it in the bounded history projection for semantic
        # repeat checks, but do not turn its subject-shaped label into an
        # exact-match exclusion.  Explicit workflow exclusions remain exact.
        from scisaurus.runtime.topic_discovery import _is_generated_slot_topic_id
        for entry in entries:
            topic_id = entry.get("topic_id") if isinstance(entry, dict) else None
            if (isinstance(topic_id, str) and topic_id
                    and not _is_generated_slot_topic_id(topic_id)
                    and topic_id not in topic_ids):
                topic_ids.append(topic_id)
        catalog_ids = [
            item.get("id") for item in self.workflow.get("experiment_catalog", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        # Rotate the most recently attempted capability while another
        # capability remains eligible.  Once the portfolio has been covered,
        # the next direction may return to the least-recent capability.
        if len(catalog_ids) > 1:
            explicit = set(capability_ids)
            available = [item for item in catalog_ids if item not in explicit]
            recent_capability = next(
                (entry.get("experiment_capability_id") for entry in reversed(entries)
                 if isinstance(entry, dict) and isinstance(entry.get("experiment_capability_id"), str)),
                None,
            )
            if (recent_capability in available
                    and any(item != recent_capability for item in available)
                    and recent_capability not in capability_ids):
                capability_ids.append(recent_capability)
        return {"capability_ids": list(dict.fromkeys(capability_ids)),
                "topic_ids": list(dict.fromkeys(topic_ids))}

    def _topic_history_context(self):
        """Return bounded history summaries suitable for the intake prompt."""
        all_entries = [entry for entry in self.topic_history.get("entries", [])
                       if isinstance(entry, dict)]
        entries = []
        for entry in all_entries[-24:]:
            if not isinstance(entry, dict):
                continue
            entries.append({key: entry.get(key) for key in (
                "topic_id", "title", "domain", "research_question", "experiment_capability_id",
                "research_form", "evidence_mode", "comparison_type",
                "parent_topic_id", "refinement_cycle", "changed_dimensions")})
        structure_fingerprints = set()
        from scisaurus.runtime.topic_discovery import topic_signature
        for entry in all_entries:
            signature = entry.get("signature")
            if not isinstance(signature, dict):
                signature = topic_signature(entry)
            fingerprint = signature.get("structure_fingerprint")
            if isinstance(fingerprint, str) and fingerprint:
                structure_fingerprints.add(fingerprint)
        return {
            "schema_version": "topic-history-1",
            "scope_key": self.topic_history_scope,
            "entries": entries,
            "history_summary": {
                "total_entries": len(all_entries),
                "distinct_structure_fingerprints": len(structure_fingerprints),
            },
            "capability_counts": deepcopy(self.topic_history.get("capability_counts", {})),
        }

    def _append_topic_history_entries(self, entries):
        """Append topic outcomes to the project-family memory atomically."""
        entries = [deepcopy(item) for item in (entries or [])
                   if isinstance(item, dict)
                   and isinstance(item.get("topic_id"), str)
                   and item["topic_id"].strip()]
        if not entries:
            return
        path = self.topic_history_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(path) + ".lock")
        lock = lock_path.open("a+")
        flock = None
        try:
            try:
                import fcntl
                flock = fcntl
                flock.flock(lock.fileno(), flock.LOCK_EX)
            except ImportError as exc:
                raise ValidationError(
                    "topic history requires an interprocess file lock") from exc
            if path.is_file():
                try:
                    document = json.loads(path.read_text())
                except (OSError, ValueError) as exc:
                    raise ValidationError(f"topic history is unreadable: {path}") from exc
                self._validate_topic_history_document(document)
            else:
                document = {"schema_version": "topic-history-1", "scopes": {}}
            scope = document["scopes"].setdefault(self.topic_history_scope, {"entries": []})
            stored_entries = scope.setdefault("entries", [])
            comparison_entries = (
                [item for stored_scope in document["scopes"].values()
                 for item in stored_scope.get("entries", [])]
                if self.workflow.get("topic_history_path") else list(stored_entries)
            )
            new_entries = []
            for entry in entries:
                fingerprint = ((entry.get("signature") or {}).get("fingerprint")
                               if isinstance(entry.get("signature"), dict) else None)
                duplicate = any(
                    isinstance(item, dict)
                    and (
                        fingerprint
                        and ((item.get("signature") or {}).get("fingerprint") == fingerprint)
                        or not fingerprint
                        and item.get("topic_id") == entry.get("topic_id")
                        and item.get("research_question") == entry.get("research_question")
                    )
                    for item in comparison_entries
                )
                if duplicate:
                    continue
                stored_entries.append(entry)
                comparison_entries.append(entry)
                new_entries.append(entry)
            scope["entries"] = stored_entries
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(canonical_bytes(document))
            os.replace(temporary, path)
            self.topic_history = self._load_topic_history()
            if self.control is not None:
                for entry in new_entries:
                    self._publish(
                        f"command/composer/topic-history/{entry['topic_id']}",
                        "note",
                        {"schema_version": "topic-history-entry-1",
                         "scope_key": self.topic_history_scope, "entry": entry},
                        "command.composer",
                    )
        finally:
            if flock is not None:
                try:
                    flock.flock(lock.fileno(), flock.LOCK_UN)
                except OSError:
                    pass
            lock.close()

    def _record_topic_history(self, context):
        """Append an accepted topic selection to the project-family memory."""
        topic = context.get("topic") if isinstance(context, dict) else None
        if not isinstance(topic, dict) or not isinstance(topic.get("id"), str):
            return
        from scisaurus.runtime.topic_discovery import topic_signature
        signature = topic_signature(topic)
        entry = {
            "topic_id": topic["id"],
            "title": topic.get("title"),
            "domain": topic.get("domain"),
            "research_question": topic.get("research_question"),
            "experiment_capability_id": topic.get("experiment_capability_id"),
            "research_form": topic.get("research_form"),
            "evidence_mode": topic.get("evidence_mode"),
            "comparison_type": topic.get("comparison_type"),
            "signature": signature,
            "run_id": self.run_id,
            "recorded_at": now_iso(),
        }
        evolution = context.get("topic_evolution") if isinstance(context, dict) else None
        if isinstance(evolution, dict) and evolution.get("mode") == "refinement":
            entry["parent_topic_id"] = evolution.get("parent_topic_id")
            entry["refinement_cycle"] = evolution.get("cycle")
            entry["refinement_reason"] = evolution.get("reason")
            entry["changed_dimensions"] = list(evolution.get("changed_dimensions", []))
        self._append_topic_history_entries([entry])

    def _record_topic_rejection_history(self, rejected_entries):
        """Persist bounded topic directions rejected before survey admission."""
        from scisaurus.runtime.topic_discovery import topic_signature

        entries = []
        for rejected in rejected_entries or []:
            if not isinstance(rejected, dict):
                continue
            topic_id = rejected.get("topic_id")
            if not isinstance(topic_id, str) or not topic_id.strip():
                continue
            entry = deepcopy(rejected)
            topic = {
                key: entry.get(key) for key in (
                    "id", "title", "domain", "research_question", "research_form",
                    "evidence_mode", "comparison_type")
            }
            topic["id"] = topic_id
            entry.setdefault("signature", topic_signature(topic))
            entry["history_status"] = "rejected"
            entry["run_id"] = self.run_id
            entry["recorded_at"] = now_iso()
            entries.append(entry)
        self._append_topic_history_entries(entries)

    def _current_topic_identity(self):
        """Return the admitted topic frontier identity for work-order fencing."""
        topic_stage = next(
            (item for item in self.workflow.get("stages", [])
             if isinstance(item, dict) and item.get("kind") == "topic_discovery"),
            None,
        )
        if not isinstance(topic_stage, dict):
            return None
        context = self.context.get(topic_stage.get("id"))
        if not isinstance(context, dict) or not isinstance(context.get("topic"), dict):
            return None
        topic_id = context["topic"].get("id")
        if not isinstance(topic_id, str) or not topic_id:
            return None
        evolution = context.get("topic_evolution")
        topic_cycle = evolution.get("cycle") if isinstance(evolution, dict) else None
        if type(topic_cycle) is not int or topic_cycle < 0:
            topic_cycle = self.continuation_cycles
        return {"topic_id": topic_id, "topic_cycle": topic_cycle}

    def _scope_active_research_requests(self, requests):
        """Drop legacy work orders that predate the currently selected topic."""
        if not isinstance(requests, list):
            return []
        identity = self._current_topic_identity()
        if identity is None:
            return [deepcopy(item) for item in requests if isinstance(item, dict)]
        topic_stage_id = next(
            (item.get("id") for item in self.workflow.get("stages", [])
             if isinstance(item, dict) and item.get("kind") == "topic_discovery"),
            None,
        )
        topic_record = self.stage_records.get(topic_stage_id, {})
        topic_context = self.context.get(topic_stage_id, {})
        if not isinstance(topic_record, dict):
            topic_record = {}
        if not isinstance(topic_context, dict):
            topic_context = {}
        refinement_completed = (
            isinstance(topic_context.get("topic_evolution"), dict)
            and topic_context["topic_evolution"].get("mode") == "refinement"
            and topic_record.get("status") in STAGE_READY_STATUSES
        )
        scoped = []
        for item in requests:
            if not isinstance(item, dict):
                continue
            if (item.get("topic_id") == identity["topic_id"]
                    and item.get("topic_cycle") == identity["topic_cycle"]):
                scoped.append(deepcopy(item))
            elif not refinement_completed:
                scoped.append(deepcopy(item))
        return scoped

    def _retire_requests_after_topic_change(self, previous_context, current_context):
        """Fence all prior-frontier work orders after a new topic is admitted."""
        previous_topic = (previous_context.get("topic", {})
                          if isinstance(previous_context, dict) else {})
        current_topic = (current_context.get("topic", {})
                         if isinstance(current_context, dict) else {})
        previous_id = previous_topic.get("id") if isinstance(previous_topic, dict) else None
        current_id = current_topic.get("id") if isinstance(current_topic, dict) else None
        if not isinstance(current_id, str) or current_id == previous_id:
            return
        stale = [deepcopy(item) for item in self.active_research_requests
                 if isinstance(item, dict)]
        retired = self.departments.retire_superseded_work_orders(
            set(), reason="superseded by a newly admitted topic frontier")
        if stale or retired:
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "retire_work_orders_after_topic_change",
                "previous_topic_id": previous_id,
                "topic_id": current_id,
                "work_orders": retired or [
                    {"request_id": item.get("id"), "state": "stale"}
                    for item in stale
                ],
            })
        self.active_research_requests = []
        for stage_id, context in self.context.items():
            if stage_id == next(
                    (item.get("id") for item in self.workflow.get("stages", [])
                     if isinstance(item, dict) and item.get("kind") == "topic_discovery"),
                    None):
                continue
            if not isinstance(context, dict):
                continue
            carried = []
            for key in ("research_expansion_requests", "research_requests",
                        "deferred_research_requests"):
                value = context.get(key)
                if isinstance(value, list):
                    carried.extend(deepcopy(value))
                    context[key] = []
            if carried:
                context.setdefault("superseded_research_requests", []).extend(carried[-24:])

    def _retire_superseded_stage_tasks(self, stage_ids, *, reason, keep_task_ids=()):
        """Fence aggregate Composer tasks from an older reopened stage.

        A stopped process can leave a stage aggregate task in ``running`` even
        after its durable project checkpoint is paused.  Work-order retirement
        does not cover those aggregate rows because they are not department
        work orders.  Reopening the closure must reconcile any started call and
        stale the old aggregate so the ledger cannot advertise a second live
        execution of the same stage.
        """
        stage_ids = {item for item in (stage_ids or []) if isinstance(item, str) and item}
        if not stage_ids:
            return []
        keep_task_ids = {item for item in (keep_task_ids or [])
                         if isinstance(item, str) and item}
        prefix = f"composer-{self.workflow['id']}-"
        terminal = {"completed", "failed", "cancelled", "stale", "rejected"}
        rows = self.tasks.control._conn.execute(
            "SELECT task_id, state, payload_json FROM tasks ORDER BY task_id"
        ).fetchall()
        retired = []
        for row in rows:
            task_id = row["task_id"]
            if (row["state"] in terminal or task_id in keep_task_ids
                    or not task_id.startswith(prefix)):
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if payload.get("stage_id") not in stage_ids:
                continue
            # Specialist assignments have their own lifecycle and are fenced
            # by the department ledger. This helper owns only the aggregate
            # Composer stage task created by _stage_task().
            if payload.get("assignment_id") or payload.get("assignment_logical_id"):
                continue
            active_attempts = self.tasks.control._conn.execute(
                "SELECT attempt_id FROM attempts WHERE task_id = ? AND state = 'started'",
                (task_id,),
            ).fetchall()
            for attempt in active_attempts:
                try:
                    self.tasks.reconcile_unknown(attempt["attempt_id"], "command.composer")
                except (NotFoundError, StateError):
                    continue
            try:
                task = self.tasks.get(task_id)
                if task["state"] not in terminal:
                    task = self.tasks.transition(task_id, "stale", "command.composer", reason=reason)
            except (NotFoundError, StateError):
                continue
            retired.append({
                "task_id": task_id,
                "stage_id": payload.get("stage_id"),
                "state": task["state"],
            })
        return retired

    @staticmethod
    def _request_target_kind(request):
        """Return the stage kind that owns a research work order.

        ``source_stage_id`` records where a request was emitted, not where it
        is executed.  Routing by that provenance field made a survey hold
        disappear from topic refinement and made legacy unscoped requests
        unusable.  The request kind/owner is the stable execution contract.
        """
        if not isinstance(request, dict):
            return None
        explicit_kind = request.get("target_stage_kind")
        if explicit_kind in STAGE_KINDS:
            return explicit_kind
        kind = request.get("kind")
        owner = request.get("owner")
        if kind == "topic_refinement":
            return "topic_discovery"
        if kind in {"literature_expansion", "full_text_retrieval"} or owner == "research.intelligence":
            return "survey"
        if kind in {"additional_experiment", "analysis_display", "analysis_repair"} \
                or owner == "methods.validation":
            return "experiment"
        if kind == "interpretation_expansion" or owner == "strategy.interpretation":
            return "interpretation"
        if kind == "manuscript_revision" or owner == "editorial.composer":
            return "paper"
        return None

    def _requests_for_stage(self, stage_or_id):
        # A continuation may reopen a downstream closure because its input
        # changed, but that does not make every active work order an input to
        # every reopened stage.  Route by the request's execution contract,
        # not its emission provenance.  Interpretation work is also projected
        # to the paper consumer because the manuscript must receive the
        # revised scientific meaning; topic, survey, and experiment repairs
        # are consumed only by their owning stage.
        stage_id = (stage_or_id.get("id") if isinstance(stage_or_id, dict)
                    else stage_or_id)
        stage = stage_or_id if isinstance(stage_or_id, dict) else next(
            (item for item in self.workflow.get("stages", [])
             if isinstance(item, dict) and item.get("id") == stage_id), None)
        stage_kind = stage.get("kind") if isinstance(stage, dict) else None
        requests = []
        for item in self.active_research_requests:
            if not isinstance(item, dict):
                continue
            target_stage_id = item.get("target_stage_id")
            if isinstance(target_stage_id, str):
                if target_stage_id == stage_id:
                    requests.append(deepcopy(item))
                # An explicitly addressed order must never be projected into
                # a sibling stage merely because its department owner is
                # shared.  Legacy orders without this address retain the
                # routing below.
                continue
            target_kind = self._request_target_kind(item)
            if target_kind == stage_kind or (
                    target_kind == "interpretation" and stage_kind == "paper") or (
                    stage_kind is None and target_kind == stage_id):
                requests.append(deepcopy(item))
        return requests

    @staticmethod
    def _research_request_signature(request):
        """Return a stable identity for one substantive work-order attempt."""
        stable = {key: request.get(key) for key in (
            "id", "kind", "owner", "objective", "why", "success_condition",
            "evidence_needed", "source_stage_id", "target_stage_id",
            "target_stage_kind", "repair_priority")}
        return hashlib.sha256(canonical_bytes(stable)).hexdigest()

    def _mark_research_requests_attempted(self, stage):
        """Fence echoed work orders after their owning stage has returned.

        A scientific hold remains visible as an open departmental order, but
        the same request must not reopen the identical stage closure in a hot
        loop.  A changed request body receives a different signature and can
        trigger a new continuation in this invocation.
        """
        from scisaurus.runtime.departments import REQUEST_STAGE_KINDS

        for request in self.active_research_requests:
            if not isinstance(request, dict):
                continue
            target_stage_id = request.get("target_stage_id")
            if isinstance(target_stage_id, str):
                if target_stage_id == stage.get("id"):
                    self._attempted_request_signatures.add(
                        self._research_request_signature(request))
                continue
            target_kind = REQUEST_STAGE_KINDS.get(request.get("kind"))
            # Legacy argument repair was represented as an
            # ``interpretation_expansion`` order because the claim-evidence
            # graph is owned by Strategy.  Keep that fallback for old
            # checkpoints; new orders carry an explicit target stage above.
            argument_scoped = (
                stage.get("kind") == "argument"
                and request.get("source_stage_id") == stage.get("id")
                and target_kind == "interpretation"
            )
            if target_kind != stage.get("kind") and not argument_scoped:
                continue
            self._attempted_request_signatures.add(self._research_request_signature(request))

    @staticmethod
    def _forward_debt_work_order(stage_id, debt):
        """Turn deferred failure debt into one deterministic backfill order."""
        if not isinstance(debt, dict):
            return None
        stage_kind = debt.get("kind")
        mapping = {
            "topic_discovery": (
                "topic_refinement", "research.intelligence",
                "Return to the failed topic envelope with a materially different, source-grounded question.",
            ),
            "survey": (
                "literature_expansion", "research.intelligence",
                "Backfill the survey evidence debt with identity-reconciled primary and full-text support.",
            ),
            "experiment": (
                "additional_experiment", "methods.validation",
                "Backfill the methods debt with a discriminating control, sensitivity check, or independent recalculation.",
            ),
            "interpretation": (
                "interpretation_expansion", "strategy.interpretation",
                "Backfill the interpretation debt by separating competing mechanisms and relinking claims to observations.",
            ),
            "argument": (
                "interpretation_expansion", "strategy.interpretation",
                "Backfill the argument debt by repairing the claim-evidence graph and unsupported inferential steps.",
            ),
            "paper": (
                "manuscript_revision", "editorial.composer",
                "Backfill the manuscript debt with a fresh evidence-linked revision and rendered review pass.",
            ),
        }
        mapped = mapping.get(stage_kind)
        if mapped is None:
            return None
        kind, owner, default_objective = mapped
        stable = {
            "stage_id": stage_id,
            "kind": kind,
            "failure_class": debt.get("failure_class"),
            "error": debt.get("error"),
            "attempts": debt.get("attempts"),
        }
        request_id = "forward-" + hashlib.sha256(canonical_bytes(stable)).hexdigest()[:20]
        error = str(debt.get("error") or "the previous assignment did not produce an accepted artifact")[:1800]
        return {
            "id": request_id,
            "kind": kind,
            "owner": owner,
            "objective": default_objective,
            "why": f"The Composer carried an unresolved provisional result for {stage_id}. Failure debt: {error}",
            "success_condition": "Produce the missing evidence or analysis, rerun the affected acceptance checks, and preserve the prior candidate if the debt remains unresolved.",
            "evidence_needed": "The prior candidate artifact, failure debt, exact input identity, and a fresh independently checked result.",
            "source_stage_id": stage_id,
            "target_stage_id": stage_id,
            "target_stage_kind": stage_kind,
            "repair_priority": "immediate",
        }

    def _free_topic_stage(self):
        """Return a topic stage that feeds a scholarly survey.

        A free-topic mission is defined by its dependency graph, not by the
        presence of a particular executable capability catalog.  Catalogs are
        useful for selecting pinned experiment adapters, while a topic→survey
        edge is the actual boundary at which literature evidence must be able
        to send the question back for redesign.
        """
        stages = self.workflow.get("stages", [])
        for topic in stages:
            if topic.get("kind") != "topic_discovery":
                continue
            pending = [topic.get("id")]
            seen = set()
            while pending:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                for stage in stages:
                    if current not in stage.get("depends_on", []):
                        continue
                    if stage.get("kind") == "survey":
                        return topic
                    pending.append(stage.get("id"))
        return None

    def _topic_stage_for_survey(self, survey_stage=None):
        """Find the topic ancestor for one survey stage, if any."""
        if survey_stage is None:
            return self._free_topic_stage()
        by_id = {item.get("id"): item for item in self.workflow.get("stages", [])
                 if isinstance(item, dict)}
        pending = list(survey_stage.get("depends_on", []))
        seen = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            candidate = by_id.get(current)
            if not isinstance(candidate, dict):
                continue
            if candidate.get("kind") == "topic_discovery":
                return candidate
            pending.extend(candidate.get("depends_on", []))
        return None

    def _survey_context_for_topic(self, topic_stage):
        """Return the latest survey context downstream of a topic stage."""
        stages = self.workflow.get("stages", [])
        by_id = {item.get("id"): item for item in stages if isinstance(item, dict)}
        pending = [topic_stage.get("id")]
        seen = set()
        survey_ids = []
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for stage in stages:
                if current not in stage.get("depends_on", []):
                    continue
                if stage.get("kind") == "survey":
                    survey_ids.append(stage.get("id"))
                pending.append(stage.get("id"))
        for survey_id in survey_ids:
            context = self.context.get(survey_id)
            if isinstance(context, dict):
                return context
        return next((value for value in self.context.values()
                     if isinstance(value, dict) and value.get("kind") == "survey"), {})

    def _topic_refinement_context(self, stage):
        """Build the evidence handoff for a substantive topic revision."""
        if stage.get("kind") != "topic_discovery":
            return None
        requests = [item for item in self._requests_for_stage(stage)
                    if item.get("kind") == "topic_refinement"]
        if not requests:
            return None
        parent_context = self.context.get(stage["id"], {})
        parent = parent_context.get("topic") if isinstance(parent_context, dict) else None
        if not isinstance(parent, dict):
            return None
        survey = self._survey_context_for_topic(stage)
        survey_evidence = {}
        survey_dir = survey.get("project_dir") if isinstance(survey, dict) else None
        if isinstance(survey_dir, str):
            survey_root = Path(survey_dir).resolve() / "output"
            for filename in ("gap-assessment.json", "coverage.json"):
                path = survey_root / filename
                if not path.is_file():
                    continue
                try:
                    value = json.loads(path.read_text())
                except (OSError, ValueError, TypeError):
                    continue
                # Keep the handoff bounded; source spans and work IDs are
                # enough to direct the next search without copying a whole
                # literature database into the topic model prompt.
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
                if len(encoded) > 20000:
                    encoded = encoded[:20000]
                survey_evidence[filename] = encoded
        feedback = {
            "gap_state": survey.get("gap_state") if isinstance(survey, dict) else None,
            "nomination": deepcopy(survey.get("nomination")) if isinstance(survey, dict) else None,
            "assessment_ref": survey.get("assessment_ref") if isinstance(survey, dict) else None,
            "evidence": survey_evidence,
            "requests": [{key: item.get(key) for key in (
                "id", "kind", "objective", "why", "success_condition", "evidence_needed",
                "salvage_policy", "salvage_branch_limit")}
                         for item in requests],
        }
        specialist_feedback = []
        prior_reports = parent_context.get("specialist_reports") if isinstance(parent_context, dict) else None
        if isinstance(prior_reports, list):
            for report in prior_reports[:16]:
                if not isinstance(report, dict):
                    continue
                response = report.get("response") if isinstance(report.get("response"), dict) else {}
                specialist_feedback.append({
                    "assigned_role": report.get("assigned_role"),
                    "role_id": report.get("role_id"),
                    "decision": response.get("decision", report.get("decision")),
                    "summary": str(response.get("summary", report.get("summary", "")))[:2000],
                    "findings": [str(item)[:1200] for item in (response.get("findings", []) or [])[:8]],
                    "evidence_gaps": [str(item)[:1200] for item in (response.get("evidence_gaps", []) or [])[:8]],
                    "requested_actions": [str(item)[:1200] for item in (response.get("requested_actions", []) or [])[:8]],
                })
        verifier = parent_context.get("specialist_verifier") if isinstance(parent_context, dict) else None
        verifier_response = verifier.get("response") if isinstance(verifier, dict) else None
        if not isinstance(verifier_response, dict):
            verifier_response = {}
        verifier_repair = [str(item)[:1600] for item in (
            verifier_response.get("repair_scope") or verifier_response.get("critical_findings") or []
        )[:12] if str(item).strip()]
        refinement_feedback = None
        if verifier_repair or verifier_response.get("decision") == "hold":
            refinement_feedback = {
                "review_type": "specialist_verifier",
                "decision": verifier_response.get("decision"),
                "rationale": str(verifier_response.get("rationale", ""))[:4000],
                "required_changes": verifier_repair,
                "critical_findings": [str(item)[:1600] for item in (
                    verifier_response.get("critical_findings", []) or [])[:12]],
            }
        parent_evolution = (
            parent_context.get("topic_evolution")
            if isinstance(parent_context, dict) else None
        )
        parent_salvage = (
            parent_evolution.get("salvage")
            if isinstance(parent_evolution, dict) else None
        )
        # A structural pivot starts a new lineage. A successful salvage branch
        # carries its attempted IDs forward so the next continuation selects a
        # different repair axis instead of replaying the same one.
        attempted_salvage = []
        if isinstance(parent_salvage, dict) and parent_salvage.get("mode") == "salvage":
            attempted_salvage = list(parent_salvage.get("attempted_branch_ids", []))
        source_challenge = (
            parent_context.get("source_challenge")
            if isinstance(parent_context, dict) else None
        )
        feasibility_revalidation = (
            parent_context.get("runtime_feasibility_revalidation")
            if isinstance(parent_context, dict) else None
        )
        force_structural_pivot = (
            any(
                isinstance(item, dict)
                and item.get("require_frontier_seed_pivot") is True
                for item in requests
            )
            or verifier_response.get("require_frontier_seed_pivot") is True
            or (
                isinstance(source_challenge, dict)
                and source_challenge.get("prior_work_risk") == "high"
            )
            or (
                isinstance(feasibility_revalidation, dict)
                and feasibility_revalidation.get("status") == "required"
            )
        )
        salvage_plan = topic_salvage_plan(
            attempted_salvage,
            force_structural_pivot=force_structural_pivot,
        )
        return {
            "mode": "refinement",
            "cycle": self.continuation_cycles,
            "parent_topic_id": parent.get("id"),
            "parent_topic": deepcopy(parent),
            "reason": "The literature and admission review did not support the current question as a sufficient journal study.",
            "changed_dimensions": list((
                "mechanism", "data_regime", "comparison", "measurement", "theory",
                "research_form", "evidence_mode", "comparison_type",
            )),
            "survey_feedback": feedback,
            "specialist_feedback": specialist_feedback,
            "refinement_feedback": refinement_feedback,
            "salvage_plan": salvage_plan,
        }

    @staticmethod
    def _carry_provisional_verifier_challenge(context, verifier_result):
        """Route a survey-stage challenge forward without declaring it solved.

        A provisional topic has already passed deterministic schema, source,
        feasibility, and minimum-substance checks. The next action exists to
        investigate unresolved novelty, provenance, comparator, threshold,
        and mechanism questions. An adversary's hold on those questions is
        therefore evidence for the survey brief, not a reason to demand the
        survey's result before the survey can start. Experiment admission
        remains independently blocked by the later literature verdict.
        """
        if (not isinstance(context, dict)
                or context.get("admission_state") != "provisional_for_survey"
                or not isinstance(verifier_result, dict)):
            return False
        response = verifier_result.get("response")
        if not isinstance(response, dict) or response.get("decision") != "hold":
            return False
        existing = [str(item).strip() for item in context.get(
            "maturity_open_requirements", []) if str(item).strip()]
        requested = response.get("repair_scope")
        if not isinstance(requested, list) or not requested:
            requested = response.get("critical_findings", [])
        merged = []
        seen = set()
        for item in [*existing, *(requested if isinstance(requested, list) else [])]:
            text = str(item).strip()
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            merged.append(text[:1600])
            if len(merged) >= 16:
                break
        context["maturity_open_requirements"] = merged
        response["control_disposition"] = "carried_to_literature_survey"
        verifier_result["response"] = response
        verifier_result["gate_disposition"] = "carried_to_literature_survey"
        context["provisional_adversarial_challenge"] = {
            "decision": "hold",
            "control_disposition": "carried_to_literature_survey",
            "rationale": str(response.get("rationale", ""))[:4000],
            "critical_findings": [str(item)[:1600] for item in (
                response.get("critical_findings", []) or [])[:12]],
            "repair_scope": [str(item)[:1600] for item in (
                response.get("repair_scope", []) or [])[:12]],
        }
        context["specialist_verifier"] = deepcopy(verifier_result)
        return True

    def _gate_free_topic_survey(self, result, *, stage=None):
        """Hold a free-topic mission until its question survives literature review.

        A bounded survey can be faithful while still failing to establish an
        experiment-worthy distinction.  Treat that outcome as a scientific
        redesign request instead of allowing the Composer to pass a thin
        question directly to the executable experiment.
        """
        topic_stage = self._topic_stage_for_survey(stage)
        if topic_stage is None or not isinstance(result, dict):
            return result
        if result.get("status") not in {"completed", "accepted"}:
            return result
        state = result.get("gap_state")
        if state == "eligible_for_experiment":
            topic_context = self.context.get(topic_stage["id"], {})
            if (isinstance(topic_context, dict)
                    and topic_context.get("admission_state") == "provisional_for_survey"):
                supported = deepcopy(result)
                supported["topic_admission"] = "provisional_supported_for_experiment"
                supported["carried_maturity_requirements"] = deepcopy(
                    topic_context.get("maturity_open_requirements", []))
                return supported
            return result
        if state not in {"refuted_by_prior_work", "insufficient_evidence"}:
            return result
        if (self._allows_provisional_progress()
                and state == "insufficient_evidence"
                and result.get("survey_current") is True
                and result.get("assessment_current") is True):
            pilot = deepcopy(result)
            pilot["topic_admission"] = "exploratory_pilot"
            pilot["carried_maturity_requirements"] = deepcopy(
                self.context.get(topic_stage["id"], {}).get("maturity_open_requirements", []))
            pilot["carried_maturity_requirements"].append(
                "Literature novelty remains unresolved; exploratory results cannot establish an original contribution.")
            return pilot
        gated = deepcopy(result)
        requests = [deepcopy(item) for item in result.get("research_expansion_requests", [])
                    if isinstance(item, dict)]
        known = {item.get("id") for item in requests if isinstance(item.get("id"), str)}

        def add(item):
            if item["id"] not in known:
                requests.append(item)
                known.add(item["id"])

        if state == "insufficient_evidence":
            # The first insufficient survey is an evidence problem, not a
            # scientific-direction problem.  Give the survey one scoped
            # expansion pass before asking the topic stage to pivot.  The
            # previous Composer result is durable context, so a resumed or
            # continued run can distinguish that first pass from a repeated
            # failure without relying on an in-memory counter.
            prior_survey = self._survey_context_for_topic(
                topic_stage)
            expanded_before = (
                isinstance(prior_survey, dict)
                and prior_survey.get("gap_state") == "insufficient_evidence"
                and prior_survey.get("topic_admission") in {
                    "expand_literature_before_refine", "refine_before_experiment"
                })
            add({
                "id": "topic-literature-expansion",
                "kind": "literature_expansion",
                "owner": "research.intelligence",
                "objective": "Expand the scholarly search with exact terminology, citation chaining, and verified full text for the selected question before making an admission decision.",
                "why": "The recorded survey does not yet provide enough comparable evidence to judge whether the question distinguishes unresolved work.",
                "success_condition": "A current survey and gap assessment either establishes experiment eligibility or supplies evidence for a substantive question redesign.",
                "evidence_needed": "Relevant primary studies, identity reconciliation, full-text source spans, and a current gap assessment.",
            })
            if expanded_before:
                add({
                    "id": f"topic-refinement-{state}",
                    "kind": "topic_refinement",
                    "owner": "research.intelligence",
                    "objective": "Redesign the selected question around the expanded survey evidence so that it tests a meaningful mechanism, comparison, boundary, or measurement rather than repeating the current narrow direction.",
                    "why": "The expanded literature search still cannot establish an experiment-worthy distinction; the question must evolve before execution.",
                    "success_condition": "A new question materially changes at least one substantive dimension, survives the topic maturity review, and is re-surveyed under its own search terms.",
                    "evidence_needed": "The parent question, expanded survey map, gap assessment, counter-search findings, and an explicit account of the changed scientific dimension.",
                })
        elif state == "refuted_by_prior_work":
            add({
                "id": f"topic-refinement-{state}",
                "kind": "topic_refinement",
                "owner": "research.intelligence",
                "objective": "Redesign the selected question around the refuting literature so that it tests a meaningful mechanism, comparison, boundary, or measurement rather than repeating an established result.",
                "why": "The current literature assessment identifies prior work that already answers the selected question; the question must pivot before execution.",
                "success_condition": "A new question materially changes at least one substantive dimension, survives the topic maturity review, and is re-surveyed under its own search terms.",
                "evidence_needed": "The parent question, refuting source spans, survey map, counter-search findings, and an explicit account of the changed scientific dimension.",
            })
        gated["status"] = "research_expansion_required"
        gated["topic_admission"] = (
            "refine_before_experiment"
            if any(item.get("kind") == "topic_refinement" for item in requests)
            else "expand_literature_before_refine"
        )
        gated["research_expansion_requests"] = requests
        return gated

    def _address_for_role(self, role):
        """Resolve a role through the live project charter.

        The functional role identifies the owning department; the live charter
        supplies the concrete chief appointment.  Command roles are the only
        addresses outside a project department.
        """
        if isinstance(role, str):
            department = role.split(".", 1)[0]
            if department in self.departments.charters:
                try:
                    assignment = self.departments.resolve_address(role)
                    return {"dept": assignment["department"], "agent": assignment["agent"]}
                except ValidationError:
                    return deepcopy(COMMAND_ADDRESSES["arbiter"])
            assignment = self.departments.role_for_internal_role(role)
            if assignment is not None:
                return {"dept": assignment["department"], "agent": assignment["agent"]}
        return deepcopy(COMMAND_ADDRESSES.get(role, COMMAND_ADDRESSES["arbiter"]))

    @staticmethod
    def _topic_program(topic_context):
        """Return the validated program attached to a topic context, if any."""
        if not isinstance(topic_context, dict):
            return None
        program = topic_context.get("research_program")
        if not isinstance(program, dict):
            return None
        from scisaurus.runtime.research_program import validate_research_program
        validate_research_program(program)
        return program

    @classmethod
    def _topic_program_projection(cls, topic_context, *, max_alternatives=8):
        program = cls._topic_program(topic_context)
        if program is None:
            return None
        from scisaurus.runtime.research_program import project_research_program
        return project_research_program(program, max_alternatives=max_alternatives)

    def _attach_topic_program(self, packet):
        """Attach the full, validated program to a scientific packet in memory."""
        if not isinstance(packet, dict):
            return packet
        topic_context = next((value for value in self.context.values()
                              if isinstance(value, dict)
                              and value.get("kind") == "topic_discovery"
                              and isinstance(value.get("topic"), dict)), None)
        program = self._topic_program(topic_context)
        if program is not None:
            packet["research_program"] = deepcopy(program)
        return packet

    @staticmethod
    def _prior_stage_project(stage_id, context):
        value = context.get(stage_id, {}) if isinstance(context, dict) else {}
        path = value.get("project_dir") if isinstance(value, dict) else None
        return Path(path).resolve() if isinstance(path, str) and Path(path).is_absolute() else None

    def _augment_full_text_routes(self, config, source_project):
        """Add OA locations from the incumbent survey for a full-text continuation."""
        if not source_project:
            return
        works_path = source_project / "output" / "works.json"
        try:
            works = json.loads(works_path.read_text())
        except (OSError, ValueError):
            return
        failed_ids = set()
        coverage_path = source_project / "output" / "coverage.json"
        try:
            coverage = json.loads(coverage_path.read_text())
            failed_ids = {
                item.get("work_id") for item in coverage.get("access_and_limit_gaps", [])
                if isinstance(item, dict) and item.get("kind") == "full_text_failure"
            }
        except (OSError, ValueError, AttributeError):
            pass
        survey = config.get("survey") if isinstance(config, dict) else None
        if not isinstance(survey, dict) or not isinstance(survey.get("full_text_sources"), list):
            return
        existing = {item.get("work_id") for item in survey["full_text_sources"] if isinstance(item, dict)}
        for work in works:
            if (not isinstance(work, dict) or work.get("work_id") in existing
                    or work.get("work_id") in failed_ids):
                continue
            url = preferred_full_text_url(work.get("locations"))
            if not url:
                url = work.get("source_url")
            if not url:
                doi = work.get("doi")
                url = "https://doi.org/" + doi if isinstance(doi, str) and doi.strip() else None
            if not url:
                continue
            survey["full_text_sources"].append({
                "work_id": work["work_id"], "title": work["title"], "url": url,
                "section_markers": ["Introduction"],
            })
            existing.add(work["work_id"])

    def _apply_topic_to_survey_config(self, stage, config):
        """Project the selected free-topic question into its survey intake.

        A free-topic workflow should be runnable without hand-written binding
        entries for the common question/query fields.  Explicit bindings still
        run afterward and therefore remain authoritative for deliberate
        overrides.
        """
        topic_stage_ids = {item["id"] for item in self.workflow["stages"]
                           if item["kind"] == "topic_discovery"}
        pending = list(stage.get("depends_on", []))
        upstream = set()
        by_id = {item["id"]: item for item in self.workflow["stages"]}
        while pending:
            dependency = pending.pop()
            if dependency in upstream:
                continue
            upstream.add(dependency)
            pending.extend(by_id.get(dependency, {}).get("depends_on", []))
        if not (upstream & topic_stage_ids):
            return config
        topic_context = next((value for value in self.context.values()
                              if isinstance(value, dict)
                              and value.get("kind") == "topic_discovery"
                              and isinstance(value.get("topic"), dict)), None)
        if topic_context is None:
            return config
        survey = config.get("survey") if isinstance(config, dict) else None
        topic = topic_context["topic"]
        if not isinstance(survey, dict):
            return config
        # A free-topic novelty decision requires citation-graph evidence.  A
        # Composer-admitted provider recovery is the one explicit exception:
        # it keeps the degraded Crossref route visible in the survey result
        # instead of letting the topic binding silently switch it back off.
        provider_fallback = self.context.get(stage["id"], {}).get("provider_fallback")
        if (isinstance(provider_fallback, dict)
                and provider_fallback.get("mode") == "crossref_metadata"):
            survey["bibliography_fallback"] = "crossref_metadata"
        else:
            survey["bibliography_fallback"] = "disabled"
        # A catalog-backed stage is a new project identity.  Reusing the
        # template's project or capability IDs would leak the previous
        # experiment into the survey ledger and can collide with its reserved
        # verification capacity.  Preserve the descriptor's worker setting;
        # the configured capacity must still leave one independent verification
        # slot required by the survey contract.
        config["project_id"] = str(Path(stage["project_dir"]).resolve())
        limits = config.setdefault("limits", {})
        if type(limits.get("concurrent_calls")) is int and limits["concurrent_calls"] < 2:
            limits["concurrent_calls"] = 2
        if "worker_concurrency" not in limits and type(limits.get("concurrent_calls")) is int:
            limits["worker_concurrency"] = limits["concurrent_calls"] - 1
        if isinstance(topic.get("id"), str):
            topic_key = re.sub(r"[^a-z0-9_-]+", "-", topic["id"].casefold()).strip("-")[:48]
            if topic_key:
                survey["id"] = f"topic-{topic_key}-literature"[:64]
                for capability_key in ("bibliography", "identity", "full_text"):
                    capability = survey.get(capability_key)
                    if isinstance(capability, dict) and isinstance(capability.get("id"), str):
                        capability["id"] = f"topic-{topic_key}-{capability_key}"[:64]
        survey["question"] = topic["research_question"]
        if isinstance(topic.get("id"), str):
            survey["proposed_gap"] = {
                "id": f"topic-{topic['id']}"[:64],
                "statement": topic.get("why_promising") or topic["research_question"],
            }
        # The discovery sampler is intentionally broad: it gives the topic
        # selector a current landscape, but those records are not evidence
        # for the selected question.  Carrying their IDs into the survey
        # would silently mix unrelated papers into the new study.  A
        # catalog-backed mission therefore starts from the selected queries;
        # the survey provider, planner, and expansion rounds build the seed
        # set from question-relevant records.  Legacy non-catalog workflows
        # retain their explicit seed IDs.
        if self.workflow.get("experiment_catalog"):
            survey["seed_work_ids"] = []
        else:
            recent = topic_context.get("recent_papers", [])
            seed_work_ids = [item.get("work_id") for item in recent
                             if isinstance(item, dict) and re.fullmatch(r"W\d+", str(item.get("work_id", "")))]
            survey["seed_work_ids"] = list(dict.fromkeys(seed_work_ids))[:max(0, survey.get("search", {}).get("max_works", 0) - survey.get("search", {}).get("challenge_reserve", 0))]
        if self.workflow.get("experiment_catalog"):
            # Explicit full-text routes are part of a fixed study template.
            # Let the current survey discover open locations for the selected
            # question instead of fetching the previous topic's URLs.
            survey["full_text_sources"] = []
        queries = topic.get("search_queries")
        if isinstance(queries, list) and queries:
            unique_queries = self._topic_search_queries(topic)
            # The survey's configured planner width is the explicit intake
            # budget.  A topic may propose more discovery terms, but silently
            # dispatching all of them would defeat provider-aware quotas.
            width = (survey.get("search", {}) or {}).get("queries_per_role")
            if type(width) is int and width > 0:
                unique_queries = unique_queries[:width]
            survey["seed_queries"] = unique_queries
        # Provider readiness is an operational check, but its representative
        # workload is still part of the live survey input.  A free-topic
        # stage must not probe a descriptor's historical paper (the template
        # used to probe an Attention paper even when the selected question
        # concerned another field).  Use the selected question's first search
        # family so a successful probe also proves that the actual route can
        # serve this mission.
        probe_query = next(
            (item for item in survey.get("seed_queries", [])
             if isinstance(item, str) and item.strip()),
            topic.get("research_question", "scholarly research"),
        )
        bibliography = survey.get("bibliography")
        if isinstance(bibliography, dict) and bibliography.get("adapter") == "openalex":
            bibliography["representative"] = {
                "operation": "search", "query": str(probe_query)[:2048],
                "work_id": None, "limit": 1, "cursor": None,
            }
        identity = survey.get("identity")
        if isinstance(identity, dict) and identity.get("adapter") == "crossref":
            identity["representative"] = {"query": str(probe_query)[:2048], "limit": 1}
        if isinstance(config.get("objective"), str):
            config["objective"] = (
                f"Investigate the selected question with a bounded scholarly survey: "
                f"{topic['research_question']}")
        if isinstance(config.get("supplied_context"), str):
            program_projection = self._topic_program_projection(topic_context, max_alternatives=8)
            program_context = (
                "\nResearch program (provisional; conditional outcomes are decision rules, not results):\n"
                + json.dumps(program_projection, ensure_ascii=False, sort_keys=True)
                if program_projection is not None else ""
            )
            config["supplied_context"] = (
                "A catalog-backed free-topic intake selected this direction. "
                "Rebuild the literature map around the current question and preserve only evidence "
                "that is relevant to the selected study.\n" + topic["research_question"]
                + program_context)
        return config

    @staticmethod
    def _topic_search_queries(topic):
        """Return the selected topic's bounded search families unchanged.

        Search planning already supplies independent terminology families.
        Earlier code manufactured an exact query by splitting a hyphenated
        word from the research question (for example ``friction-triggered``
        became ``"friction triggered"``).  That synthetic query was not a
        declared search family and regularly admitted unrelated records before
        the substantive queries ran.  Exact phrases remain available when a
        planner explicitly declares them.
        """
        raw = [item.strip() for item in topic.get("search_queries", [])
               if isinstance(item, str) and item.strip()]
        return list(dict.fromkeys(raw))

    @staticmethod
    def _continuation_capability_id(topic, cycle):
        """Derive a new registry identity for a substantive experiment pass."""
        base = topic.get("id") if isinstance(topic, dict) else None
        base = re.sub(r"[^a-z0-9_-]+", "-", str(base or "topic").casefold()).strip("-")
        if not base or not re.match(r"[a-z]", base):
            base = f"topic-{base}" if base else "topic"
        suffix = f"-cycle-{cycle}"
        return f"{base[:64 - len(suffix)]}{suffix}"

    @staticmethod
    def _capability_repair_projection(value, *, depth=0, max_depth=6,
                                      max_keys=48, max_items=16, max_text=2600):
        """Bound repair evidence before it enters a specialist or author prompt."""
        if depth >= max_depth and isinstance(value, (dict, list)):
            return "[truncated]"
        if isinstance(value, dict):
            output = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= max_keys:
                    output["[truncated_keys]"] = True
                    break
                lowered = str(key).casefold()
                if any(part in lowered for part in (
                        "api_key", "apikey", "authorization", "password", "secret", "token")):
                    output[str(key)] = "[redacted]"
                    continue
                output[str(key)] = ComposerRunner._capability_repair_projection(
                    item, depth=depth + 1, max_depth=max_depth,
                    max_keys=max_keys, max_items=max_items, max_text=max_text)
            return output
        if isinstance(value, list):
            output = [ComposerRunner._capability_repair_projection(
                item, depth=depth + 1, max_depth=max_depth,
                max_keys=max_keys, max_items=max_items, max_text=max_text)
                      for item in value[:max_items]]
            if len(value) > max_items:
                output.append("[truncated_items]")
            return output
        if isinstance(value, str):
            return value if len(value) <= max_text else value[:max_text] + "...[truncated]"
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:max_text]

    @staticmethod
    def _capability_authoring_repair_projection(value):
        """Keep the source-level repair evidence without replaying the whole panel.

        The methods panel stores a durable packet for auditability.  Passing that
        packet wholesale to the program author duplicated the failure dossier,
        program snapshot, prior foundry response, and every specialist report in
        one prompt.  This projection retains one exact source pair and the
        decision-bearing findings while dropping duplicated transport envelopes.
        """
        context = value if isinstance(value, dict) else {}
        packet = context.get("packet") if isinstance(context.get("packet"), dict) else {}
        prior_work = packet.get("prior_foundry_work")
        prior_work = prior_work if isinstance(prior_work, dict) else {}
        last_attempt = prior_work.get("last_attempt")
        last_attempt = last_attempt if isinstance(last_attempt, dict) else {}
        snapshot = packet.get("program_snapshot")
        snapshot = snapshot if isinstance(snapshot, list) else []

        def clip(item, limit):
            if not isinstance(item, str):
                return item
            return item if len(item) <= limit else item[:limit] + "...[truncated]"

        def compact_list(items, limit, text_limit=1200):
            if not isinstance(items, list):
                return []
            output = []
            for item in items[:limit]:
                if isinstance(item, dict):
                    output.append({str(key): clip(item.get(key), text_limit)
                                   for key in item if key in {
                                       "id", "kind", "source", "role", "role_id",
                                       "assigned_role", "operation", "target",
                                       "instruction", "acceptance_check", "text",
                                       "failure_dossier_ref", "failure_input_sha256",
                                       "recovery_mode", "repair_priority",
                                   }})
                else:
                    output.append(clip(item, text_limit))
            if len(items) > limit:
                output.append("[truncated_items]")
            return output

        def source_from_snapshot(name):
            for item in snapshot:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path", "")).casefold()
                if path.endswith(f"/{name}.py") or path.endswith(f"\\{name}.py"):
                    return item.get("source")
            return None

        def source(name):
            candidate = last_attempt.get(f"{name}_source")
            if not isinstance(candidate, str) or not candidate:
                candidate = source_from_snapshot(name)
            return clip(candidate, 24000) if isinstance(candidate, str) else None

        recovery = packet.get("failure_recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        recovery_projection = {
            "failure_class": recovery.get("failure_class"),
            "recovery_mode": recovery.get("recovery_mode"),
            "requires_capability_repair": recovery.get("requires_capability_repair"),
            "dossier_ref": recovery.get("dossier_ref"),
            "input_sha256": recovery.get("input_sha256"),
            "acceptance_checks": compact_list(recovery.get("acceptance_checks"), 8, 900),
            "repair_commands": compact_list(recovery.get("repair_commands"), 8, 1100),
            "review_directives": compact_list(recovery.get("review_directives"), 12, 1100),
        }
        panel_reports = context.get("reports")
        if not isinstance(panel_reports, list):
            panel_reports = packet.get("prior_specialist_reviews")
        panel_reports = panel_reports if isinstance(panel_reports, list) else []
        reports = []
        for report in panel_reports[:8]:
            if not isinstance(report, dict):
                continue
            reports.append({
                key: clip(report.get(key), 1400) for key in (
                    "role_id", "assigned_role", "status", "decision", "summary",
                    "artifact_ref") if key in report
            } | {
                "findings": compact_list(report.get("findings"), 4, 900),
                "requested_actions": compact_list(report.get("requested_actions"), 4, 900),
            })

        return {
            "schema_version": context.get("schema_version") or "capability-repair-panel-1",
            "input_sha256": context.get("input_sha256") or packet.get("input_sha256"),
            "topic": ComposerRunner._capability_repair_projection(
                packet.get("topic"), max_depth=4, max_keys=24, max_items=8, max_text=1600),
            "failure": ComposerRunner._capability_repair_projection(
                packet.get("failure"), max_depth=4, max_keys=20, max_items=8, max_text=1600),
            "failure_recovery": recovery_projection,
            "failed_program": {
                "source_manifest": [{key: item.get(key) for key in (
                    "path", "sha256", "size_bytes", "source_truncated") if key in item}
                                    for item in snapshot[:8] if isinstance(item, dict)],
                "executor_source": source("executor"),
                "validator_source": source("validator"),
            },
            "prior_foundry_work": {
                "status": prior_work.get("status"),
                "attempts": prior_work.get("attempts"),
                "cache_ref": prior_work.get("cache_ref"),
                "repair_gate_counts": prior_work.get("repair_gate_counts"),
                "feedback": clip(prior_work.get("feedback"), 7000),
                "validation_context": ComposerRunner._capability_repair_projection(
                    prior_work.get("validation_context"), max_depth=4, max_keys=20,
                    max_items=8, max_text=1400),
                "validation_feedback": ComposerRunner._capability_repair_projection(
                    prior_work.get("validation_feedback"), max_depth=4, max_keys=20,
                    max_items=8, max_text=1600),
                "experiment_intent": ComposerRunner._capability_repair_projection(
                    last_attempt.get("experiment_intent"), max_depth=4, max_keys=24,
                    max_items=8, max_text=1600),
            },
            "observed_result": ComposerRunner._capability_repair_projection(
                packet.get("observed_result"), max_depth=4, max_keys=24, max_items=8, max_text=1600),
            "failure_observed_result": ComposerRunner._capability_repair_projection(
                packet.get("failure_observed_result"), max_depth=4, max_keys=24, max_items=8, max_text=1600),
            "prior_specialist_reviews": reports,
            "prior_verifier": ComposerRunner._capability_repair_projection(
                packet.get("prior_verifier"), max_depth=4, max_keys=20, max_items=8, max_text=1400),
            "root_causes": compact_list(context.get("root_causes"), 12, 1400),
            "required_changes": compact_list(context.get("required_changes"), 12, 1400),
            "acceptance_checks": compact_list(context.get("acceptance_checks"), 8, 900),
            "repair_commands": compact_list(context.get("repair_commands"), 8, 1100),
            "verifier": ComposerRunner._capability_repair_projection(
                context.get("verifier"), max_depth=4, max_keys=20, max_items=8, max_text=1400),
            "repair_contract": ComposerRunner._capability_repair_projection(
                packet.get("repair_contract"), max_depth=4, max_keys=20, max_items=8, max_text=1400),
        }

    @staticmethod
    def _capability_authoring_follow_up_projection(requests):
        """Project only actionable experiment directives into the author prompt."""
        output = []
        for item in requests if isinstance(requests, list) else []:
            if not isinstance(item, dict):
                continue
            entry = {
                key: (item.get(key)[:2400] if isinstance(item.get(key), str)
                      else item.get(key)) for key in (
                    "kind", "objective", "why", "success_condition", "evidence_needed",
                    "failure_dossier_ref", "failure_input_sha256", "recovery_mode",
                    "target_stage_id", "target_stage_kind", "repair_priority",
                    "experiment_repair_plan", "repair_strategy", "attempt_lineage",
                ) if key in item
            }
            for key, limit, count in (
                    ("repair_commands", 1100, 8),
                    ("acceptance_checks", 900, 8),
                    ("review_directives", 1100, 12)):
                if key in item:
                    value = item.get(key)
                    if isinstance(value, list):
                        compacted = []
                        for child in value[:count]:
                            if isinstance(child, dict):
                                compacted.append({str(name): (
                                    child.get(name) if not isinstance(child.get(name), str)
                                    else child.get(name)[:limit]) for name in child
                                    if name in {"id", "kind", "source", "operation", "target",
                                                "instruction", "acceptance_check", "text"}})
                            elif isinstance(child, str):
                                compacted.append(child[:limit])
                        entry[key] = compacted
            if isinstance(item.get("model_diagnostics"), dict):
                entry["model_diagnostics"] = ComposerRunner._capability_repair_projection(
                    item["model_diagnostics"], max_depth=3, max_keys=12, max_items=4, max_text=800)
            output.append(entry)
        return output

    def _latest_foundry_failure_projection(self, question, domain):
        """Find the latest failed authoring state for this exact scientific intent."""
        cache = ModelWorkCache(self.store, self._publish, namespace="command/foundry-work")
        try:
            entries = cache.entries()
        except (OSError, ValueError, TypeError):
            return {}
        for entry in entries[:256]:
            assignment = entry.get("assignment") if isinstance(entry, dict) else None
            required = (assignment.get("required_intent_fields")
                        if isinstance(assignment, dict) else None)
            if (not isinstance(required, dict)
                    or required.get("research_question") != question
                    or required.get("domain") != domain):
                continue
            if entry.get("status") == "succeeded" and not entry.get("feedback"):
                continue
            last_attempt = entry.get("last_attempt")
            if isinstance(last_attempt, dict):
                last_attempt = {
                    "experiment_intent": deepcopy(last_attempt.get("experiment_intent")),
                    "executor_source": str(last_attempt.get("executor_source", ""))[:7000],
                    "validator_source": str(last_attempt.get("validator_source", ""))[:7000],
                }
            else:
                last_attempt = None
            return self._capability_repair_projection({
                "status": entry.get("status"),
                "attempts": entry.get("attempts"),
                "feedback": entry.get("feedback") or entry.get("error"),
                "validation_context": entry.get("validation_context"),
                "validation_feedback": entry.get("validation_feedback"),
                "repair_gate_counts": entry.get("repair_gate_counts"),
                "last_attempt": last_attempt,
                "cache_ref": entry.get("cache_ref"),
            }, max_text=7000)
        return {}

    def _build_capability_repair_packet(self, stage, topic_result, prior_context, error):
        """Assemble bounded evidence for the model-led methods repair panel."""
        selected = topic_result.get("topic") if isinstance(topic_result, dict) else {}
        selected = selected if isinstance(selected, dict) else {}
        prior_context = prior_context if isinstance(prior_context, dict) else {}
        failure_history = prior_context.get("specialist_reports")
        if not isinstance(failure_history, list):
            failure_history = []
        prior_reports = []
        for report in failure_history[-8:]:
            if not isinstance(report, dict):
                continue
            response = report.get("response") if isinstance(report.get("response"), dict) else {}
            prior_reports.append({
                "role_id": report.get("role_id"),
                "assigned_role": report.get("assigned_role"),
                "status": report.get("status"),
                "decision": response.get("decision", report.get("decision")),
                "summary": str(response.get("summary", report.get("summary", "")))[:1800],
                "findings": [str(item)[:1200] for item in response.get("findings", [])[:8]]
                if isinstance(response.get("findings"), list) else [],
                "requested_actions": [str(item)[:1200] for item in response.get("requested_actions", [])[:8]]
                if isinstance(response.get("requested_actions"), list) else [],
            })
        verifier = prior_context.get("specialist_verifier")
        verifier_response = verifier.get("response") if isinstance(verifier, dict) else {}
        if not isinstance(verifier_response, dict):
            verifier_response = {}
        foundry_failure = self._latest_foundry_failure_projection(
            selected.get("research_question"), selected.get("domain"))
        packet = {
            "schema_version": "capability-repair-packet-1",
            "stage_id": stage.get("id"),
            "continuation_cycle": self.continuation_cycles,
            "topic": {key: selected.get(key) for key in (
                "id", "title", "domain", "research_question", "scope",
                "comparison", "measurement", "disconfirmation_test", "resource_plan",
            )},
            "failure": {
                "error": str(error)[:4096],
                "prior_status": prior_context.get("status"),
                "review_status": prior_context.get("review_status"),
                "failure_debt": self._capability_repair_projection(
                    prior_context.get("failure_debt"), max_text=1800),
            },
            "failure_recovery": self._capability_repair_projection(
                prior_context.get("failure_recovery"), max_text=3200),
            "program_snapshot": self._capability_repair_projection(
                self._failure_program_snapshot(stage), max_text=18000),
            "observed_result": self._capability_repair_projection({
                key: prior_context.get(key) for key in (
                    "results_package", "raw_results", "metrics", "findings", "assets",
                    "execution_refs", "deterministic_validation_ref", "model_review_refs",
                    "assessment_ref", "analysis", "limitations")
                if key in prior_context
            }, max_text=7000),
            "failure_observed_result": self._capability_repair_projection(
                prior_context.get("failure_observed_result"), max_text=9000),
            "prior_foundry_work": foundry_failure,
            "experiment_repair_plan": self._capability_repair_projection(
                prior_context.get("experiment_repair_plan"), max_text=4200),
            "experiment_repair_history": self._capability_repair_projection(
                prior_context.get("experiment_repair_history"), max_depth=4,
                max_items=4, max_text=1800),
            "prior_specialist_reviews": prior_reports,
            "prior_verifier": {
                "status": verifier.get("status") if isinstance(verifier, dict) else None,
                "decision": verifier_response.get("decision"),
                "critical_findings": [str(item)[:1400]
                                       for item in verifier_response.get("critical_findings", [])[:8]]
                if isinstance(verifier_response.get("critical_findings"), list) else [],
                "repair_scope": [str(item)[:1400]
                                 for item in verifier_response.get("repair_scope", [])[:8]]
                if isinstance(verifier_response.get("repair_scope"), list) else [],
            },
            "repair_contract": {
                "must_preserve": ["the admitted topic domain", "the exact research question"],
                "must_change": [
                    "the mechanism or estimand responsible for the recorded failure",
                    "the validator and acceptance checks when the prior validator accepted invalid evidence",
                    "the declared design axis in experiment_repair_plan when the prior capability lineage is exhausted",
                ],
                "must_prove": [
                    "finite non-degenerate observations",
                    "an independent validator that recalculates every primary outcome",
                    "a result that is sensitive to the declared intervention",
                ],
                "prohibited": [
                    "repeating the same executor with a renamed threshold",
                    "endpoint or NaN fallback for undefined crossings",
                    "claiming an unobserved mechanism from an analytic comparator",
                ],
            },
        }
        packet = self._capability_repair_projection(packet, max_text=7000)
        packet["input_sha256"] = hashlib.sha256(canonical_bytes(packet)).hexdigest()
        return packet

    def _run_capability_repair_panel(self, stage, descriptor, topic_result,
                                     prior_context, error):
        """Run upper-model methods roles before a capability redesign."""
        packet = self._build_capability_repair_packet(
            stage, topic_result, prior_context, error)
        prior_context = prior_context if isinstance(prior_context, dict) else {}
        retained = prior_context.get("capability_repair_panel")
        if (isinstance(retained, dict)
                and retained.get("input_sha256") == packet.get("input_sha256")):
            return deepcopy(retained)

        base = re.sub(r"[^a-z0-9-]+", "-", str(stage.get("id", "experiment")).casefold()).strip("-")
        attempt = prior_context.get("capability_repair_attempts", 1)
        if type(attempt) is not int or attempt < 1:
            attempt = 1
        suffix = f"-repair-panel-{self.continuation_cycles}-{attempt}"
        panel_id = f"{(base or 'experiment')[:64 - len(suffix)]}{suffix}"
        panel_stage = deepcopy(stage)
        panel_stage.update({
            "id": panel_id,
            "kind": "experiment",
            "deadline_seconds": min(float(stage.get("deadline_seconds", 900)),
                                     max(1.0, self._remaining())),
        })
        panel_result = {
            "_repair_panel": True,
            "capability_repair_packet": deepcopy(packet),
            "research_question": packet["topic"].get("research_question"),
            "hypotheses": ((packet.get("prior_foundry_work") or {}).get("last_attempt") or {}
                           ).get("experiment_intent", {}).get("hypothesis"),
            "design": ((packet.get("prior_foundry_work") or {}).get("last_attempt") or {}
                       ).get("experiment_intent", {}),
            "analysis_plan": {
                "state": "pre_execution_repair_panel",
                "failure": packet["failure"],
                "repair_contract": packet["repair_contract"],
            },
            "claims": {"status": "failed_capability_requires_model_repair"
                       if not prior_context.get("raw_results")
                       else "observed_result_requires_model_repair"},
            "raw_results": prior_context.get("raw_results"),
            "derived_results": prior_context.get("derived_results", {}),
            "figures": prior_context.get("figures", []),
        }
        input_ref = {
            "kind": "capability_repair_panel",
            "stage_id": stage.get("id"),
            "panel_id": panel_id,
            "digest": packet["input_sha256"],
        }
        assignment = self.departments.begin_stage(
            panel_id, "experiment", attempt_number=1, input_ref=input_ref,
            deadline_seconds=panel_stage["deadline_seconds"], active_role_ids=None,
        )
        bundle = self._run_specialist_pool(
            panel_stage, assignment, descriptor, stage_result=panel_result)
        bundle = self._publish_specialist_reports(panel_stage, assignment, bundle)
        verifier = self._run_specialist_verifier(
            panel_stage, assignment, descriptor, bundle, panel_result,
            stage_result=panel_result)
        panel_usage = self._specialist_usage(bundle.get("reports", []))
        if isinstance(verifier, dict):
            verifier_usage = self._specialist_usage([verifier])
            for key in ("model_calls", "input_tokens", "output_tokens"):
                panel_usage[key] = panel_usage.get(key, 0) + verifier_usage.get(key, 0)
        model_enabled = bundle.get("model_enabled") is True
        panel_error = None if model_enabled else "capability repair panel has no configured model route"
        panel_outcome = "completed" if model_enabled else "blocked"
        assignment_result = self.departments.finish_stage(
            panel_id, "experiment", attempt_number=1, outcome=panel_outcome,
            output_ref=None, usage=panel_usage, error=panel_error,
            actor="command.composer", specialist_results=bundle.get("by_role", {}),
            verifier_result=verifier, failure_scope=None if model_enabled else "stage",
        )
        reports = []
        root_causes = []
        required_changes = []
        for report in bundle.get("reports", []):
            response = report.get("response") if isinstance(report.get("response"), dict) else {}
            reports.append({
                "role_id": report.get("role_id"),
                "assigned_role": report.get("assigned_role"),
                "status": report.get("status"),
                "decision": response.get("decision", report.get("decision")),
                "summary": str(response.get("summary", report.get("summary", "")))[:2400],
                "findings": [str(item)[:1600] for item in response.get("findings", [])[:8]]
                if isinstance(response.get("findings"), list) else [],
                "requested_actions": [str(item)[:1600] for item in response.get("requested_actions", [])[:8]]
                if isinstance(response.get("requested_actions"), list) else [],
                "artifact_ref": report.get("artifact_ref"),
            })
            root_causes.extend(reports[-1]["findings"])
            required_changes.extend(reports[-1]["requested_actions"])
        verifier_response = verifier.get("response") if isinstance(verifier, dict) else {}
        verifier_response = verifier_response if isinstance(verifier_response, dict) else {}
        root_causes.extend(str(item)[:1600] for item in verifier_response.get("critical_findings", [])[:8]
                           if isinstance(verifier_response.get("critical_findings"), list))
        required_changes.extend(str(item)[:1600] for item in verifier_response.get("repair_scope", [])[:8]
                                if isinstance(verifier_response.get("repair_scope"), list))
        panel = {
            "schema_version": "capability-repair-panel-1",
            "status": "completed" if model_enabled else "unavailable",
            "decision": "repair" if model_enabled else "defer",
            "input_sha256": packet["input_sha256"],
            "packet": packet,
            "root_causes": list(dict.fromkeys(root_causes))[:24],
            "required_changes": list(dict.fromkeys(required_changes))[:24],
            "acceptance_checks": deepcopy(packet["repair_contract"]["must_prove"]),
            "repair_commands": deepcopy(
                prior_context.get("repair_commands", [])
                if isinstance(prior_context.get("repair_commands"), list) else []
            ),
            "reports": reports,
            "verifier": {
                "status": verifier.get("status") if isinstance(verifier, dict) else None,
                "decision": verifier_response.get("decision"),
                "rationale": str(verifier_response.get("rationale", ""))[:2400],
                "critical_findings": [str(item)[:1600] for item in verifier_response.get("critical_findings", [])[:8]]
                if isinstance(verifier_response.get("critical_findings"), list) else [],
                "repair_scope": [str(item)[:1600] for item in verifier_response.get("repair_scope", [])[:8]]
                if isinstance(verifier_response.get("repair_scope"), list) else [],
                "artifact_ref": verifier.get("artifact_ref") if isinstance(verifier, dict) else None,
            },
            "ledger": {
                "panel_stage_id": panel_id,
                "assignment_plan_ref": assignment.get("plan_ref"),
                "chief_synthesis_ref": assignment_result.get("chief_synthesis_ref"),
                "verifier_artifact_ref": assignment_result.get("verifier_artifact_ref"),
                "active_agents": deepcopy(assignment.get("active_agents", [])),
                "verifier_agent": assignment.get("verifier_agent"),
            },
            "usage": panel_usage,
            "error": panel_error,
        }
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "capability_repair_panel",
            "stage_id": stage.get("id"),
            "panel_stage_id": panel_id,
            "decision": panel["decision"],
            "active_agents": deepcopy(assignment.get("active_agents", [])),
            "verifier_agent": assignment.get("verifier_agent"),
            "chief_synthesis_ref": assignment_result.get("chief_synthesis_ref"),
            "verifier_artifact_ref": assignment_result.get("verifier_artifact_ref"),
            "input_sha256": packet["input_sha256"],
        })
        self._checkpoint(f"{stage.get('id')}:capability_repair_panel", force=True)
        return panel

    def _materialize_topic_capability(self, result, *, force_regenerate=False,
                                      continuation_requests=(), continuation_revision=None,
                                      stage_id=None, study_type=None,
                                      model_call_budget=None, repair_context=None):
        """Generate and admit a pinned program for one science-first topic.

        A substantive methods continuation must not silently execute the same
        generated capability again.  Forced generations receive a new
        deterministic study identity and the red-team work orders in their
        authoring brief, while the original question and domain stay pinned.
        An independently registered repair artifact is the exception: it is
        already a new immutable capability and can be selected before another
        authoring call burns the same scope.
        """
        configured_path = self.workflow.get("capability_foundry_config_path")
        if not configured_path:
            return result
        from scisaurus.runtime.capability_foundry import (
            CapabilityFoundry, CapabilityModelBudgetExceeded,
            validate_foundry_config, validate_program_review,
        )
        from scisaurus.runtime.capability_registry import load_registry

        configured = validate_foundry_config(json.loads(Path(configured_path).read_text()))
        selected = result.get("topic") if isinstance(result, dict) else None
        if not isinstance(selected, dict):
            raise ValidationError("capability foundry requires a selected topic")
        question = selected.get("research_question")
        domain = selected.get("domain")
        if not isinstance(question, str) or not isinstance(domain, str):
            raise ValidationError("capability foundry requires topic domain and research question")

        registry_root = Path(configured["registry_root"])
        existing = None
        superseded = None
        prior_capability = deepcopy(result.get("generated_capability"))
        for entry in reversed(load_registry(registry_root).get("capabilities", [])):
            try:
                path = Path(entry["path"])
                descriptor = json.loads(path.read_text())
                experiment = descriptor.get("experiment", {})
            except (KeyError, OSError, ValueError, TypeError):
                continue
            if (experiment.get("research_question") == question
                    and experiment.get("domain") == domain):
                admission = json.loads((path.parent / "admission.json").read_text())
                review = admission.get("adversarial_review") or {}
                repair = admission.get("repair_provenance") or {}
                if force_regenerate:
                    if (not isinstance(repair, dict)
                            or repair.get("kind") != "independent_repair"
                            or repair.get("origin") != "composer_model_panel"):
                        continue
                    # A pre-execution repair is already a materially different,
                    # independently admitted program. Reuse it even when the
                    # checkpoint points at the same capability; otherwise a
                    # recovery resume needlessly re-enters model authoring and
                    # can starve the experiment. Once observations exist, the
                    # same identity is no longer a fresh continuation.
                    has_observed_capability = self._has_executed_experiment_result(
                        self.context.get(stage_id, {}), experiment.get("id"))
                    if (has_observed_capability and isinstance(prior_capability, dict)
                            and experiment.get("id") == prior_capability.get("capability_id")):
                        continue
                try:
                    valid_verdict = validate_program_review({
                        name: review.get(name) for name in ("status", "checks", "findings")})["status"] == "admitted"
                except ValidationError:
                    valid_verdict = False
                if (not valid_verdict or review.get("role") != "review.methods"
                        or review.get("review_method") != "independent_model"
                        or review.get("candidate_sha256") != entry["candidate_record_sha256"]
                        or (study_type is not None and experiment.get("study_type") != study_type)):
                    if superseded is None or experiment["revision"] > superseded["revision"]:
                        superseded = {"id": experiment["id"], "revision": experiment["revision"]}
                    continue
                existing = {
                    "capability_id": descriptor.get("capability_id"),
                    "descriptor_path": str(path.resolve()), "reused": True,
                    "registry_entry": deepcopy(entry),
                }
                if isinstance(repair, dict) and repair:
                    existing["repair_provenance"] = deepcopy(repair)
                break

        if existing is None:
            model = json.loads(Path(configured["model_config_path"]).read_text())
            # The foundry's sandbox timeout is not a model-request timeout.
            # Keep the two fences explicit and cap the authoring call to the
            # smaller of the configured foundry budget and this mission's
            # remaining wall, so a stalled provider cannot consume hours of
            # the experiment stage before its own gates even start.
            remaining = self._remaining()
            if remaining <= 0.2:
                raise ValidationError("capability foundry model has no safe request window remaining")
            model_timeout = min(
                float(configured.get("model_timeout_seconds", 300.0)), remaining)
            if (type(model.get("timeout_seconds")) not in (int, float)
                    or not math.isfinite(model["timeout_seconds"])
                    or model["timeout_seconds"] <= 0):
                raise ValidationError("capability foundry model timeout_seconds must be finite and positive")
            model["timeout_seconds"] = max(0.2, min(float(model["timeout_seconds"]), model_timeout))
            if model_call_budget is not None and model_call_budget < 1:
                raise CapabilityModelBudgetExceeded(
                    "capability foundry has no model-call budget left in the experiment stage",
                    limit=0, observed=0, usage={})
            foundry_usage_before = deepcopy(self.foundry_usage)
            foundry = CapabilityFoundry(
                model,
                runtime_python=configured["runtime_python"],
                workspace_root=configured["workspace_root"],
                registry_root=configured["registry_root"],
                repo_root=configured["repo_root"],
                requirements_file=configured["requirements_file"],
                runtime_packages=[(item["name"], item["version"])
                                  for item in configured["runtime_packages"]],
                max_attempts=configured["max_attempts"],
                timeout_seconds=configured["timeout_seconds"],
                model_timeout_seconds=configured.get("model_timeout_seconds", 300.0),
            )
            brief = {
                "topic": {key: selected.get(key) for key in (
                    "id", "title", "domain", "research_question", "scope",
                    "disconfirmation_test", "resource_plan")},
                "source_challenge": result.get("source_challenge"),
                "continuation": {
                    "cycle": self.continuation_cycles if continuation_requests else None,
                    "prior_capability_id": (
                        prior_capability.get("capability_id")
                        if isinstance(prior_capability, dict) else None
                    ),
                    "requests": self._capability_authoring_follow_up_projection(continuation_requests)
                    if continuation_requests else [],
                },
                "closest_prior_work": [{key: item.get(key) for key in (
                    "work_id", "title", "year", "abstract", "source_url")}
                    for item in result.get("candidate_prior_work", [])[:8]
                    if isinstance(item, dict)],
                "required_properties": [
                    "bounded reproducible experiment",
                    "raw observations sufficient for independent recalculation",
                    "at least three scientifically informative figures",
                    "no network access or undeclared data",
                ],
            }
            program_projection = self._topic_program_projection(result, max_alternatives=8)
            if program_projection is not None:
                brief["research_program"] = program_projection
            if isinstance(repair_context, dict):
                brief["capability_repair"] = self._capability_authoring_repair_projection(
                    repair_context)
                brief["repair_execution_contract"] = {
                    "sequence": [
                        "inspect the exact failure dossier, observed result, executor source, and validator source",
                        "identify the first invalid mechanism or acceptance assumption",
                        "make a source-level repair that changes that mechanism rather than relabelling a threshold",
                        "run an independent sensitivity/recalculation check before registration",
                    ],
                    "required": [
                        "preserve the admitted research question and domain",
                        "retain the failed result as diagnostic evidence",
                        "return a fresh capability revision with exact provenance",
                        "pass replay, digest, independent recalculation, and methods review gates",
                    ],
                    "prohibited": [
                        "editing result JSON instead of the program",
                        "using NaN, endpoint, or constant fallback to hide an undefined estimand",
                        "claiming a resolved mechanism when the new run remains diagnostic",
                    ],
                }
            required_intent = {"domain": domain, "research_question": question}
            if study_type is not None:
                required_intent["study_type"] = study_type
            if superseded is not None:
                required_intent.update(id=superseded["id"], revision=superseded["revision"] + 1)
            if force_regenerate:
                revision = continuation_revision
                if type(revision) is not int or revision < 1:
                    revision = max(1, self.continuation_cycles + 1)
                required_intent.update({
                    "id": self._continuation_capability_id(selected, self.continuation_cycles),
                    "revision": revision,
                })
            try:
                outcome = foundry.generate(
                    json.dumps(brief, ensure_ascii=False, sort_keys=True),
                    required_intent=required_intent,
                    work_cache=ModelWorkCache(self.store, self._publish, namespace="command/foundry-work"),
                    on_progress=lambda phase, state: self._foundry_progress(stage_id or selected["id"], phase, state),
                    deadline=time.monotonic() + self._remaining(),
                    model_call_budget=model_call_budget,
                    repair_provenance=(
                        {
                            "kind": "independent_repair",
                            "origin": "composer_model_panel",
                            "panel_stage_id": repair_context.get("ledger", {}).get("panel_stage_id"),
                            "panel_input_sha256": repair_context.get("input_sha256"),
                            "panel_verdict_artifact_ref": repair_context.get("ledger", {}).get(
                                "verifier_artifact_ref"),
                            "root_causes": deepcopy(repair_context.get("root_causes", []))[:12],
                            "required_changes": deepcopy(repair_context.get("required_changes", []))[:12],
                        }
                        if isinstance(repair_context, dict) else None
                    ),
                )
            except Exception as exc:
                self._sync_foundry_usage()
                setattr(exc, "foundry_usage", self._usage_delta(
                    self.foundry_usage, foundry_usage_before))
                raise
            self._sync_foundry_usage()
            existing = {
                "capability_id": outcome["registration"]["capability_id"],
                "descriptor_path": outcome["registration"]["descriptor_path"],
                "reused": False, "attempts": outcome["attempts"],
                "admission": outcome["admission"],
                "foundry_usage": self._usage_delta(self.foundry_usage, foundry_usage_before),
            }
            if isinstance(outcome.get("admission", {}).get("repair_provenance"), dict):
                existing["repair_provenance"] = deepcopy(
                    outcome["admission"]["repair_provenance"])
            if force_regenerate:
                existing["continuation_cycle"] = self.continuation_cycles
        if not isinstance(existing.get("capability_id"), str):
            raise ValidationError("capability foundry did not return a capability identity")
        result["generated_capability"] = existing
        selected["experiment_capability_id"] = existing["capability_id"]
        for candidate in result.get("candidates", []):
            if isinstance(candidate, dict) and candidate.get("id") == selected.get("id"):
                candidate["experiment_capability_id"] = existing["capability_id"]
        return result

    def _apply_topic_to_experiment_config(self, stage, config, *, model_call_budget=None):
        """Select the admitted generated program or a pinned catalog capability.

        A foundry-backed topic becomes actionable only after its generated
        program and independent validator pass the complete admission gate.
        Legacy workflows may still select an explicitly configured template.
        In both cases, the current run supplies the accepted literature gate
        and project-local paths; model output never becomes a command directly.
        """
        by_id = {item["id"]: item for item in self.workflow["stages"]}
        pending = list(stage.get("depends_on", []))
        ancestor_ids = set()
        while pending:
            current = pending.pop()
            if current in ancestor_ids or current not in by_id:
                continue
            ancestor_ids.add(current)
            pending.extend(by_id[current].get("depends_on", []))
        ancestor_topic_ids = [
            item["id"] for item in self.workflow["stages"]
            if item["id"] in ancestor_ids and item["kind"] == "topic_discovery"
        ]
        if len(ancestor_topic_ids) > 1:
            raise ValidationError(
                f"experiment stage {stage['id']} has multiple topic ancestors; "
                "each research branch requires its own experiment stage")
        if ancestor_topic_ids:
            topic_stage_id = ancestor_topic_ids[0]
            value = self.context.get(topic_stage_id)
            topic_match = (
                (topic_stage_id, value)
                if isinstance(value, dict)
                and value.get("kind") == "topic_discovery"
                and isinstance(value.get("topic"), dict)
                else None
            )
            if topic_match is None:
                raise ValidationError(
                    f"experiment stage {stage['id']} is missing its topic ancestor context")
        else:
            # Compatibility for workflows that predate an explicit topic
            # stage but inject a single topic packet into a standalone
            # experiment helper. A mixed DAG that contains topic stages but
            # has none in this experiment's ancestor closure is an independent
            # branch and must not inherit a global topic packet.
            if any(item["kind"] == "topic_discovery" for item in self.workflow["stages"]):
                return config
            topic_match = next((
                (stage_id, value) for stage_id, value in self.context.items()
                if isinstance(value, dict)
                and value.get("kind") == "topic_discovery"
                and isinstance(value.get("topic"), dict)
            ), None)
        if topic_match is None:
            return config
        topic_stage_id, topic_context = topic_match
        pilot_survey = next((self.context[ancestor_id] for ancestor_id in ancestor_ids
            if self._allows_provisional_progress()
            and by_id[ancestor_id]["kind"] == "survey"
            and self.context.get(ancestor_id, {}).get("topic_admission") == "exploratory_pilot"
            and self.context[ancestor_id].get("gap_state") == "insufficient_evidence"
            and self.context[ancestor_id].get("survey_current") is True
            and self.context[ancestor_id].get("assessment_current") is True), None)
        if topic_context.get("admission_state") == "provisional_for_survey":
            eligible_surveys = []
            for ancestor_id in ancestor_ids:
                ancestor = by_id[ancestor_id]
                if ancestor.get("kind") != "survey":
                    continue
                survey_topic = self._topic_stage_for_survey(ancestor)
                if not isinstance(survey_topic, dict) or survey_topic.get("id") != topic_stage_id:
                    continue
                survey_context = self.context.get(ancestor_id)
                if (isinstance(survey_context, dict) and (
                        (survey_context.get("gap_state") == "eligible_for_experiment"
                         and survey_context.get("topic_admission") == "provisional_supported_for_experiment")
                        or (self._allows_provisional_progress()
                            and survey_context.get("topic_admission") == "exploratory_pilot"
                            and survey_context.get("gap_state") == "insufficient_evidence"
                            and survey_context.get("survey_current") is True
                            and survey_context.get("assessment_current") is True))):
                    eligible_surveys.append(ancestor_id)
                    if survey_context.get("topic_admission") == "exploratory_pilot":
                        pilot_survey = survey_context
            if not eligible_surveys:
                raise ValidationError(
                    "provisional topic cannot enter an experiment until its dependent "
                    "survey records eligible_for_experiment with carried maturity requirements")
        selected = topic_context["topic"]
        generated = topic_context.get("generated_capability")
        continuation_requests = self._requests_for_stage(stage)
        prior_experiment = self.context.get(stage["id"])
        expected_capability_id = (
            generated.get("capability_id") if isinstance(generated, dict)
            else selected.get("experiment_capability_id")
        )
        has_observed_experiment = self._has_executed_experiment_result(
            prior_experiment, expected_capability_id)
        pre_execution_capability_blocked = (
            isinstance(prior_experiment, dict)
            and prior_experiment.get("review_status") == "scientific_assignment_blocked"
            and (
                "capability foundry" in str(prior_experiment.get("error", "")).casefold()
                or isinstance(prior_experiment.get("failure_recovery"), dict)
                and prior_experiment["failure_recovery"].get("requires_capability_repair") is True
            )
            and not has_observed_experiment
        )
        capability_repair_attempts = self._pre_execution_repair_count(stage)
        # A pre-execution program rejection is materially different from an
        # additional experiment after observed data. Give the foundry a
        # bounded set of fresh, cycle-specific chances, including one final
        # simplification pass; after that the Composer pivots the research
        # direction instead of spending the deadline on an unchanged program.
        fresh_pre_execution_repair = (
            pre_execution_capability_blocked
            and capability_repair_attempts < PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT
        )
        observed_experiment_repair = (
            has_observed_experiment
            and bool(self.continuation_cycles)
            and any(item.get("kind") in {"additional_experiment", "analysis_display", "analysis_repair"}
                    for item in continuation_requests)
            and (
                (isinstance(prior_experiment, dict)
                 and prior_experiment.get("status") in STAGE_HOLD_STATUSES)
                or (isinstance(prior_experiment, dict)
                    and prior_experiment.get("review_status") == "scientific_assignment_blocked")
                or (isinstance(prior_experiment, dict)
                    and isinstance(prior_experiment.get("failure_debt"), dict))
                or (isinstance(prior_experiment, dict)
                    and prior_experiment.get("backfill_required") is True)
            )
        )
        needs_fresh_capability = (
            bool(self.continuation_cycles and continuation_requests)
            and any(item.get("kind") in {"additional_experiment", "analysis_display", "analysis_repair"}
                    for item in continuation_requests)
                    and has_observed_experiment
        ) or fresh_pre_execution_repair
        if needs_fresh_capability and not self.workflow.get("capability_foundry_config_path"):
            raise ValidationError(
                "a substantive experiment continuation requires capability_foundry_config_path; "
                "a pinned catalog cannot silently repeat the prior experiment")
        repair_context = None
        repair_panel_required = fresh_pre_execution_repair or observed_experiment_repair
        if repair_panel_required and isinstance(prior_experiment, dict):
            # A failed capability or an observed scientific hold is a design
            # failure, not merely a malformed transport response. Ask the
            # methods pool to inspect the evidence before the foundry receives
            # another authoring call.
            repair_context = self._run_capability_repair_panel(
                stage, config, topic_context, prior_experiment,
                prior_experiment.get("error") or (
                    "observed experiment result requires a model-led methods repair"
                    if observed_experiment_repair
                    else "pre-execution capability authoring failed"
                ),
            )
            prior_experiment["capability_repair_panel"] = deepcopy(repair_context)
            # Keep the canonical blocker status while a fresh capability is
            # being authored. The recovery scheduler uses this status to
            # distinguish a real repair lineage from an ordinary observed
            # experiment continuation.
            if fresh_pre_execution_repair:
                prior_experiment["review_status"] = "scientific_assignment_blocked"
            self.context[stage["id"]] = prior_experiment
        if needs_fresh_capability and self.workflow.get("capability_foundry_config_path"):
            if repair_panel_required and isinstance(prior_experiment, dict):
                prior_experiment["capability_repair_attempts"] = capability_repair_attempts + 1
                self.context[stage["id"]] = prior_experiment
            self._materialize_topic_capability(
                topic_context, force_regenerate=True,
                continuation_requests=continuation_requests,
                continuation_revision=int((config.get("experiment") or {}).get("revision", 1)),
                stage_id=stage["id"], study_type="exploratory" if pilot_survey else None,
                model_call_budget=model_call_budget,
                repair_context=repair_context)
            generated = topic_context.get("generated_capability")
        elif self.workflow.get("capability_foundry_config_path"):
            # Capability authoring is downstream of the accepted literature
            # gate.  Do not spend the topic-stage wall generating an executable
            # program before the survey has tested whether the question is
            # actually worth executing.
            self._materialize_topic_capability(topic_context, stage_id=stage["id"],
                                               study_type="exploratory" if pilot_survey else None,
                                               model_call_budget=model_call_budget)
            generated = topic_context.get("generated_capability")
        if isinstance(generated, dict):
            capability_id = generated.get("capability_id")
            entry = {"id": capability_id, "config_path": generated.get("descriptor_path")}
        else:
            catalog = self.workflow.get("experiment_catalog") or []
            capability_id = selected.get("experiment_capability_id")
            entry = next((item for item in catalog if item["id"] == capability_id), None)
        if entry is None:
            if self.workflow.get("capability_foundry_config_path"):
                raise ValidationError("experiment stage could not materialize its generated experiment capability")
            if not self.workflow.get("experiment_catalog"):
                if pilot_survey is None:
                    return config
                template = deepcopy(config)
            else:
                raise ValidationError(
                    "topic selection must name one configured experiment capability")
        else:
            try:
                template = json.loads(Path(entry["config_path"]).read_text())
            except (OSError, ValueError) as exc:
                raise ValidationError(
                    f"experiment capability template is unreadable: {entry['config_path']}") from exc
        experiment = template.get("experiment") if isinstance(template, dict) else None
        if not isinstance(experiment, dict):
            raise ValidationError("experiment capability template must contain an experiment object")
        current = config.get("experiment")
        if not isinstance(current, dict):
            raise ValidationError("experiment stage config must contain an experiment object")
        selected_experiment = deepcopy(experiment)
        # The template is a pinned capability, while the live literature gate
        # belongs to this mission and is supplied by Composer bindings below.
        selected_experiment["literature_gate"] = deepcopy(current.get("literature_gate"))
        if pilot_survey is not None:
            selected_experiment["study_type"] = "exploratory"
            selected_experiment["literature_gate"]["required_state"] = "insufficient_evidence"
            # Maturity requirements are deferred scientific obligations, not
            # part of the generated program's frozen execution contract.  The
            # foundry program was admitted against its own experiment_intent
            # limitations; appending survey requirements here changes that
            # intent after admission and makes an otherwise identical replay
            # fail with "output omits a frozen design limitation".  Keep the
            # requirements in supplied_context (and the downstream review
            # ledger) where they can constrain interpretation without
            # mutating the executable capability.
        selected_experiment["revision"] = int(current.get("revision", selected_experiment.get("revision", 1)))
        # A design-driven capability executes a bounded declarative design that
        # the topic stage proposed.  The program, estimators and data-process
        # families stay pinned; only the declared design and the scientific
        # identity of this mission are projected in.
        design_driven = bool((selected_experiment.get("parameters") or {}).get("design_driven"))
        design = selected.get("experiment_design")
        if design_driven:
            from scisaurus.runtime.topic_discovery import validate_experiment_design
            if not isinstance(design, dict):
                raise ValidationError(
                    "design-driven experiment capability requires a proposed experiment_design")
            design = validate_experiment_design(design)
            selected_experiment.setdefault("execution", {}).setdefault("input", {})["design"] = design
            parameters = dict(selected_experiment.get("parameters") or {})
            parameters["default_design"] = deepcopy(design)
            selected_experiment["parameters"] = parameters
            selected_experiment["seed"] = int(design["seed"])
            for key in ("domain", "research_question"):
                value = selected.get(key)
                if isinstance(value, str) and value.strip():
                    selected_experiment[key] = value.strip()
        elif isinstance(design, dict):
            raise ValidationError("fixed experiment capability cannot accept a proposed experiment_design")
        config["experiment"] = selected_experiment
        config["project_id"] = str(Path(stage["project_dir"]).resolve())
        for branch in ("execution", "validation"):
            client = config["experiment"][branch].get("client")
            if isinstance(client, dict):
                client["cwd"] = str(Path(stage["project_dir"]).resolve())
        context = config.get("supplied_context", "")
        config["supplied_context"] = (
            f"{context}\nSelected executable capability: {capability_id}.\n"
            f"Selected research question: {selected.get('research_question', '')}"
        )
        maturity_requirements = topic_context.get("maturity_open_requirements", [])
        if (topic_context.get("admission_state") == "provisional_for_survey"
                and isinstance(maturity_requirements, list) and maturity_requirements):
            config["supplied_context"] += (
                "\nThe topic entered evidence gathering provisionally. The experiment design, "
                "analysis, and interpretation must address these still-open intake requirements; "
                "the literature gap decision does not by itself resolve them:\n- "
                + "\n- ".join(str(item) for item in maturity_requirements[:8])
            )
        program_projection = self._topic_program_projection(topic_context, max_alternatives=8)
        if program_projection is not None:
            config["supplied_context"] += (
                "\nResearch program (provisional; use the selected branch's kill condition and conditional outcomes):\n"
                + json.dumps(program_projection, ensure_ascii=False, sort_keys=True)
            )
        return config

    @staticmethod
    def _synchronize_research_packet_identity(packet):
        """Replace stale template identity after a current result is bound."""
        if not isinstance(packet, dict):
            return packet
        results = packet.get("results_package")
        if not isinstance(results, dict):
            return packet
        question = results.get("question")
        if isinstance(question, str) and question.strip():
            packet["study_question"] = question
        procedure = next((item.get("description") for item in results.get("procedures", [])
                          if isinstance(item, dict) and isinstance(item.get("description"), str)
                          and item.get("description").strip()), None)
        if isinstance(procedure, str) and procedure.strip():
            packet["scope_statement"] = procedure
        # A packet prepared for another experiment must not retain its old
        # evidence IDs or literature prose.  The current survey projection is
        # added by the paper synchronizer; result IDs remain authoritative for
        # the interpretation and argument stages.
        result_ids = []
        for key in ("procedures", "metrics", "findings"):
            result_ids.extend(item.get("id") for item in results.get(key, [])
                              if isinstance(item, dict) and isinstance(item.get("id"), str))
        result_ids.extend(f"limitation-{index}" for index, _ in enumerate(results.get("limitations", [])))
        packet["evidence_ids"] = list(dict.fromkeys(result_ids))
        packet["literature_evidence"] = []
        packet["reference_cards"] = []
        return packet

    def _downstream_paper_stage(self, stage_id):
        """Find the paper consumer whose publication contract governs a stage."""
        by_id = {item["id"]: item for item in self.workflow["stages"]}
        pending = [item["id"] for item in self.workflow["stages"]
                   if item["kind"] == "paper" and stage_id in item.get("depends_on", [])]
        seen = set()
        while pending:
            candidate = pending.pop(0)
            if candidate in seen:
                continue
            seen.add(candidate)
            stage = by_id.get(candidate)
            if stage is None:
                continue
            if stage["kind"] == "paper":
                return stage
            pending.extend(next_stage["id"] for next_stage in by_id.values()
                           if candidate in next_stage.get("depends_on", []))
        # The direct-dependency walk above is sufficient for the ordinary
        # Composer graph.  Keep a transitive fallback for a branched workflow.
        for stage in by_id.values():
            if stage["kind"] != "paper":
                continue
            pending = list(stage.get("depends_on", []))
            visited = set()
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                if current == stage_id:
                    return stage
                pending.extend(by_id.get(current, {}).get("depends_on", []))
        return None

    def _paper_depth_profile(self, stage_id):
        paper_stage = self._downstream_paper_stage(stage_id)
        if paper_stage is None:
            return None
        try:
            descriptor_path = Path(paper_stage["config_path"])
            descriptor = json.loads(descriptor_path.read_text())
            # Composer paper stages normally point at a small descriptor whose
            # ``paper_config_path`` names the independently versioned release
            # contract.  Read that nested contract before deciding whether an
            # upstream experiment/survey must satisfy journal-grade floors.
            nested_path = descriptor.get("paper_config_path")
            if isinstance(nested_path, str):
                candidate = Path(nested_path)
                if not candidate.is_absolute():
                    candidate = descriptor_path.parent / candidate
                if candidate.is_file():
                    descriptor = json.loads(candidate.read_text())
        except (OSError, ValueError, TypeError):
            return None
        if (descriptor.get("schema_version") == "paper-release-score-3"
                or descriptor.get("document_type") == "research_paper" and "depth_profile" in descriptor):
            try:
                from scisaurus.runtime.scholarly_depth import PROFILES, profile_for_paper
                return descriptor.get("depth_profile"), PROFILES[profile_for_paper(descriptor)]
            except (KeyError, ValidationError):
                return None
        return None

    def _ensure_journal_quality_contract(self, stage, config):
        """Bind a substantive analysis floor to experiments feeding a paper."""
        profile = self._paper_depth_profile(stage["id"])
        if self._allows_provisional_progress() and not self._requests_for_stage(stage):
            return config
        if profile is None or stage["kind"] != "experiment":
            return config
        experiment = config.get("experiment")
        if not isinstance(experiment, dict):
            return config
        from scisaurus.runtime.research_quality import ensure_minimum_quality_contract
        # The downstream journal floor is monotone: retain stricter local
        # requirements, but never let an older weak contract turn a research
        # paper into a thin validation note.
        experiment["quality_contract"] = ensure_minimum_quality_contract(
            experiment.get("quality_contract"), study_type=experiment.get("study_type"))
        return config

    def _argument_minimums(self, stage_id, config):
        """Raise argument display floors to the publication profile."""
        if self._allows_provisional_progress():
            return {"figures": 0, "tables": 0, "experiments": 1}
        minimums = {"figures": 2, "tables": 1, "experiments": 2}
        profile = self._paper_depth_profile(stage_id)
        if profile is not None:
            _, floor = profile
            minimums["figures"] = max(minimums["figures"], int(floor.get("min_figures", 0)))
            minimums["tables"] = max(minimums["tables"], int(floor.get("min_tables", 0)))
        return minimums

    @staticmethod
    def _packet_unit_ids(packet):
        contract = packet.get("writer_contract", {}) if isinstance(packet, dict) else {}
        order = contract.get("section_order", []) if isinstance(contract, dict) else []
        ids = []
        for section in order:
            if not isinstance(section, dict):
                continue
            for unit_id in section.get("unit_ids", []):
                if isinstance(unit_id, str) and unit_id not in ids:
                    ids.append(unit_id)
        return ids

    def _synchronize_paper_figure_arguments(self, paper_config, packet, argument_package):
        """Carry every accepted result figure into the release bindings."""
        if paper_config.get("schema_version") != "paper-release-score-3":
            return paper_config
        results = packet.get("results_package") if isinstance(packet, dict) else None
        assets = [asset for asset in (results or {}).get("assets", [])
                  if isinstance(asset, dict) and asset.get("role") == "figure"]
        if not assets:
            return paper_config
        asset_ids = {asset.get("id") for asset in assets if isinstance(asset.get("id"), str)}
        # A paper descriptor can have been prepared against an earlier result
        # package.  Stale figure bindings are removed as one explicit
        # reconciliation step; carrying them forward would make the release
        # claim an asset that is no longer part of the accepted evidence.
        arguments = [item for item in paper_config.get("figure_arguments", [])
                     if isinstance(item, dict) and item.get("asset_id") in asset_ids]
        paper_config["figure_arguments"] = arguments
        known = {item.get("asset_id") for item in arguments}
        plan = {}
        argument = argument_package.get("argument") if isinstance(argument_package, dict) else None
        for item in (argument or {}).get("figure_plan", []):
            if isinstance(item, dict) and item.get("kind") == "figure" and isinstance(item.get("asset_id"), str):
                plan[item["asset_id"]] = item
        unit_ids = self._packet_unit_ids(packet)
        used_units = {item.get("unit_id") for item in arguments if isinstance(item, dict)}
        preferred = [unit_id for unit_id in unit_ids
                     if unit_id.startswith(("results_", "interpretation_"))]
        for asset in assets:
            asset_id = asset.get("id")
            if not isinstance(asset_id, str) or asset_id in known:
                continue
            planned = plan.get(asset_id, {})
            unit_id = next((item for item in preferred if item not in used_units), None)
            if unit_id is None:
                unit_id = next((item for item in preferred), None)
            if unit_id is None:
                # Let the normal release validator report an unbindable asset;
                # inventing a manuscript address would violate surgical edits.
                continue
            observation = planned.get("readout") or asset.get("caption")
            why = planned.get("purpose") or "This display exposes a result pattern needed by the argument."
            if not isinstance(observation, str) or not observation.strip():
                continue
            arguments.append({"asset_id": asset_id, "observation": observation,
                              "unit_id": unit_id, "why": why})
            known.add(asset_id)
            used_units.add(unit_id)
            contract = packet.get("writer_contract")
            if isinstance(contract, dict):
                readings = contract.setdefault("figure_readings", [])
                if not isinstance(readings, list):
                    readings = []
                    contract["figure_readings"] = readings
                readings[:] = [item for item in readings
                               if isinstance(item, dict) and item.get("asset_id") in asset_ids]
                if not any(isinstance(item, dict) and item.get("asset_id") == asset_id for item in readings):
                    readings.append({"asset_id": asset_id, "observation": observation,
                                     "unit_id": unit_id, "why": why})
        return paper_config

    def _synchronize_catalog_paper_inputs(self, paper_config, packet, argument_package):
        """Rebuild topic-dependent paper contracts from the accepted run.

        Free-topic stages begin with reusable descriptors so the Composer can
        validate a workflow before any provider call.  Those descriptors are
        structural templates, not scientific content.  Once a capability has
        produced a result, carrying the old claims, storyline, or figure jobs
        forward would make a new experiment wear the previous paper's skin.
        This projection keeps the section tree stable while replacing every
        content-bearing edge with the current result and accepted argument.
        """
        if not self.workflow.get("experiment_catalog"):
            return paper_config, packet
        if not isinstance(packet, dict):
            return paper_config, packet
        results = packet.get("results_package")
        if not isinstance(results, dict):
            return paper_config, packet

        argument = argument_package.get("argument") if isinstance(argument_package, dict) else None
        if isinstance(argument, dict):
            packet["research_argument"] = deepcopy(argument)
            if isinstance(argument_package.get("review"), dict):
                packet["research_argument_review"] = deepcopy(argument_package["review"])
            if isinstance(argument_package.get("argument_defense"), dict):
                packet["argument_defense"] = deepcopy(argument_package["argument_defense"])

        self._attach_topic_program(packet)

        question = results.get("question") or packet.get("study_question")
        if not isinstance(question, str) or not question.strip():
            question = "What mechanism explains the observed difference under the declared study conditions?"
        procedure = next((item.get("description") for item in results.get("procedures", [])
                           if isinstance(item, dict) and isinstance(item.get("description"), str)
                           and item["description"].strip()), None)
        scope = procedure or packet.get("scope_statement") or question
        if not isinstance(scope, str) or not scope.strip():
            scope = question
        packet["study_question"] = question
        packet["scope_statement"] = scope
        packet["composition_goal"] = (
            "Write a complete scientific paper for the selected study. Preserve the observed results, "
            "explain competing mechanisms, connect each figure to an argument, and keep the conclusion "
            "inside the declared data and design boundary."
        )

        # The packet is the writer's literature-facing context.  It is rebuilt
        # from the current accepted survey so background prose cannot inherit a
        # prior experiment's source cards.
        survey = None
        if "survey" in self.context:
            from scisaurus.runtime.paper import load_paper_survey
            # An accepted survey is a hard dependency of a research-paper
            # stage.  Let a broken or stale survey identity reach the normal
            # Composer retry path instead of silently falling back to an old
            # packet with no literature basis.
            survey = load_paper_survey(paper_config,
                require_eligible=not self._allows_provisional_progress())
        if isinstance(survey, dict):
            cards, literature = [], []
            for source_ref, source in list(survey.get("sources", {}).items())[:50]:
                if not isinstance(source, dict):
                    continue
                work_id = source.get("work_id")
                if not isinstance(work_id, str) or not work_id:
                    continue
                title = source.get("title") or work_id
                abstract = source.get("abstract") or source.get("text") or ""
                if not isinstance(abstract, str):
                    abstract = str(abstract)
                authors = source.get("authors") or "Authors not supplied by the survey."
                if isinstance(authors, list):
                    authors = ", ".join(
                        item if isinstance(item, str) else str(item)
                        for item in authors)
                cards.append({
                    "source_ref": source_ref, "work_id": work_id,
                    "title": str(title), "authors": str(authors),
                    "year": source.get("year"),
                    "abstract": abstract[:1800],
                    "representation": source.get("representation"),
                    "reader_use": "Use this record only for the background and related-work context supported by the survey.",
                })
                literature.append({
                    "id": f"literature-{len(literature)}", "work_id": work_id,
                    "source_ref": source_ref, "quote": str(title),
                    "relation": "context",
                })
            packet["reference_cards"] = cards
            packet["literature_evidence"] = literature

        unit_ids = self._packet_unit_ids(packet)
        if not unit_ids:
            return paper_config, packet

        def unit_for(*prefixes):
            return next((item for item in unit_ids
                         if item.startswith(prefixes)), unit_ids[0])

        def first_text(items, key):
            return next((item.get(key) for item in items
                         if isinstance(item, dict) and isinstance(item.get(key), str)
                         and item[key].strip()), None)

        procedures = [item for item in results.get("procedures", []) if isinstance(item, dict)]
        metrics = [item for item in results.get("metrics", []) if isinstance(item, dict)]
        findings = [item for item in results.get("findings", []) if isinstance(item, dict)]
        raw_limitations = [item for item in results.get("limitations", []) if isinstance(item, str) and item.strip()]
        evidence, evidence_by_kind = [], {key: [] for key in ("procedure", "result", "finding", "limitation")}
        evidence_ids = set()

        def add_evidence(kind, locator, quote, relation="support"):
            if not isinstance(locator, str) or not locator.strip() or not isinstance(quote, str) or not quote.strip():
                return None
            prefix = {"procedure": "procedure", "result": "result", "finding": "finding",
                      "limitation": "limitation"}[kind]
            base = f"{prefix}-{locator.replace('/', '-') }".replace(" ", "-")
            base = re.sub(r"[^a-zA-Z0-9_-]", "-", base).strip("-").casefold()
            if not base or not base[0].isalpha():
                base = f"evidence-{prefix}"
            evidence_id = base[:64]
            suffix = 2
            while evidence_id in evidence_ids:
                stem = base[: max(1, 63 - len(str(suffix)))]
                evidence_id = f"{stem}-{suffix}"
                suffix += 1
            item = {"id": evidence_id, "kind": kind, "locator": locator,
                    "quote": quote, "relation": relation}
            evidence.append(item)
            evidence_ids.add(evidence_id)
            evidence_by_kind[kind].append(evidence_id)
            return evidence_id

        for item in procedures:
            add_evidence("procedure", item.get("id"), item.get("description"))
        for item in metrics:
            add_evidence("result", item.get("id"), item.get("presentation"))
        for item in findings:
            add_evidence("finding", item.get("id"), item.get("statement"))
        for index, limitation in enumerate(raw_limitations):
            add_evidence("limitation", f"limitation/{index}", limitation, "qualify")

        fallback_evidence = next((items for items in evidence_by_kind.values() if items), [])
        if not fallback_evidence:
            return paper_config, packet

        thesis = None
        if isinstance(argument, dict):
            primary = argument.get("primary_argument")
            if isinstance(primary, dict) and isinstance(primary.get("thesis"), str) and primary["thesis"].strip():
                thesis = primary["thesis"].strip()
        thesis = thesis or (findings[0].get("statement") if findings else question)
        result_proposition = first_text(findings, "statement") or first_text(metrics, "presentation") or question
        limitation_proposition = raw_limitations[0] if raw_limitations else (
            "The interpretation is limited to the declared data, model, and parameter range.")
        method_proposition = procedure or "The study follows the declared reproducible comparison protocol."
        beats = [
            {"id": "motivation", "role": "motivation",
             "proposition": f"The study examines {question.rstrip('?')} under a defined empirical boundary."},
            {"id": "question", "role": "question", "proposition": question},
            {"id": "method", "role": "method", "proposition": method_proposition},
            {"id": "result", "role": "result", "proposition": result_proposition},
            {"id": "interpretation", "role": "interpretation", "proposition": thesis},
            {"id": "limitation", "role": "limitation", "proposition": limitation_proposition},
            {"id": "conclusion", "role": "conclusion", "proposition": thesis},
        ]
        beat_evidence = {
            "motivation": evidence_by_kind["procedure"] or evidence_by_kind["result"] or fallback_evidence,
            "question": evidence_by_kind["procedure"] or evidence_by_kind["result"] or fallback_evidence,
            "method": evidence_by_kind["procedure"] or fallback_evidence,
            "result": evidence_by_kind["finding"] or evidence_by_kind["result"] or fallback_evidence,
            "interpretation": evidence_by_kind["finding"] or evidence_by_kind["result"] or fallback_evidence,
            "limitation": evidence_by_kind["limitation"] or fallback_evidence,
            "conclusion": evidence_by_kind["finding"] or evidence_by_kind["result"] or fallback_evidence,
        }
        beat_units = {
            "motivation": [unit_for("intro_")], "question": [unit_for("question_")],
            "method": [unit_for("methods_")], "result": [unit_for("results_")],
            "interpretation": [unit_for("interpretation_", "discussion_")],
            "limitation": [unit_for("limitations_")], "conclusion": [unit_for("conclusion_")],
        }

        if paper_config.get("schema_version") in {"paper-release-score-2", "paper-release-score-3"}:
            paper_config["storyline"] = {
                "id": str(paper_config.get("storyline", {}).get("id", "study-storyline")),
                "revision": int(paper_config.get("storyline", {}).get("revision", 1)) + 1,
                "thesis": thesis, "beats": beats,
            }
            paper_config["evidence"] = evidence
            paper_config["claims"] = [{
                "id": f"claim-{beat['id']}", "statement": beat["proposition"],
                "unit_ids": beat_units[beat["id"]],
                "evidence_ids": list(dict.fromkeys(beat_evidence[beat["id"]])),
                "storyline_id": beat["id"],
            } for beat in beats]
            topic = next((value.get("topic") for value in self.context.values()
                          if isinstance(value, dict) and value.get("kind") == "topic_discovery"
                          and isinstance(value.get("topic"), dict)), None)
            if isinstance(topic, dict) and isinstance(topic.get("title"), str) and topic["title"].strip():
                paper_config["title"] = topic["title"].strip()
            elif isinstance(results.get("id"), str):
                paper_config["title"] = results["id"].replace("_", " ").replace("-", " ").title()

        contract = packet.get("writer_contract") if isinstance(packet.get("writer_contract"), dict) else {}
        exact = []
        if procedure:
            exact.append({"text": procedure, "unit_id": unit_for("methods_")})
        for item in findings[:4]:
            exact.append({"text": item["statement"], "unit_id": unit_for("results_")})
        for item in raw_limitations[:2]:
            exact.append({"text": item, "unit_id": unit_for("limitations_")})
        exact.extend([
            {"text": question, "unit_id": unit_for("question_")},
            {"text": thesis, "unit_id": unit_for("interpretation_", "discussion_")},
        ])
        contract["required_exact_content"] = exact
        contract["required_citation_markers"] = [
            f"[[cite:{reference['key']}]]" for reference in paper_config.get("references", [])
            if isinstance(reference, dict) and isinstance(reference.get("key"), str)
        ]
        contract["figure_readings"] = [
            {"asset_id": item["asset_id"], "observation": item["observation"],
             "unit_id": item["unit_id"], "why": item["why"]}
            for item in paper_config.get("figure_arguments", [])
            if isinstance(item, dict)
        ]
        contract["surface_rules"] = [
            "Write for a scientific reader and keep execution bookkeeping out of the manuscript.",
            "Results report observations; interpretation and Discussion explain mechanisms and distinguish possibilities from established findings.",
            "Use each numeric result where it advances the argument and avoid repeating the same fact across sections.",
            "Limitations must state how the declared design boundary changes the interpretation.",
        ]
        packet["writer_contract"] = contract
        return paper_config, packet

    @staticmethod
    def _extend_paper_images(config, packet, paper_config):
        images = list(config.get("images", []))
        result_path = paper_config.get("results_package")
        base = Path(result_path).resolve().parent if isinstance(result_path, str) else None
        for asset in (packet.get("results_package", {}) or {}).get("assets", []):
            if not isinstance(asset, dict) or asset.get("role") != "figure" or base is None:
                continue
            candidate = (base / asset.get("path", "")).resolve()
            if candidate.is_file() and str(candidate) not in images:
                images.append(str(candidate))
        config["images"] = images
        return config

    def _adapt_continuation_config(self, stage, config):
        """Derive a fresh stage configuration from explicit research work orders."""
        kind = stage["kind"]
        stage_context = self.context.get(stage["id"], {})
        provider_fallback = stage_context.get("provider_fallback")
        provider_fallback = (
            provider_fallback if isinstance(provider_fallback, dict)
            and provider_fallback.get("mode") == "crossref_metadata" else None
        )
        requests = self._requests_for_stage(stage)
        if kind == "survey" and provider_fallback is not None:
            config["project_id"] = str(Path(stage["project_dir"]).resolve())
            survey = config.get("survey", {})
            search = survey.get("search", {})
            # Crossref supplies bounded bibliographic metadata, not an
            # OpenAlex citation graph. Keep this recovery deliberately small
            # and preserve the degraded provenance in the survey output;
            # OpenAlex can backfill the missing graph after its reset.
            survey["bibliography_fallback"] = "crossref_metadata"
            seed_count = len(survey.get("seed_work_ids", []))
            reserve = min(2, max(0, int(search.get("challenge_reserve", 0))))
            current_works = max(1, int(search.get("max_works", 20)))
            search["max_works"] = max(seed_count + reserve + 1, min(current_works, 20))
            search["challenge_reserve"] = min(reserve, search["max_works"] - 1)
            search["queries_per_role"] = max(1, min(int(search.get("queries_per_role", 2)), 2))
            search["results_per_query"] = max(1, min(int(search.get("results_per_query", 10)), 10))
            search["max_api_calls"] = max(1, min(int(search.get("max_api_calls", 32)), 32))
            search["max_full_texts"] = max(1, min(int(search.get("max_full_texts", 6)), 6))
            search["expansion_rounds"] = 0
            search["expansion_seed_count"] = 1
            search["saturation_rounds"] = 1
            if "max_analyzed_works" in search:
                search["max_analyzed_works"] = max(
                    reserve + 1,
                    min(int(search["max_analyzed_works"]), search["max_works"], 12),
                )
            survey["revision"] = int(survey.get("revision", 1)) + self.continuation_cycles
            self._augment_full_text_routes(config, self._prior_stage_project("survey", self.context))
        if not requests:
            return config
        quota_recovery = stage_context.get("quota_recovery")
        quota_recovery = quota_recovery if isinstance(quota_recovery, dict) else None
        if kind == "survey":
            config["project_id"] = str(Path(stage["project_dir"]).resolve())
            survey = config.get("survey", {})
            search = survey.get("search", {})
            if quota_recovery is not None:
                # A local allocation failure is not repaired by widening the
                # catalog. Reopen the durable checkpoint with one focused gate
                # pass and a smaller retrieval envelope.
                limits = config.setdefault("limits", {})
                current_rounds = limits.get("max_rounds")
                limits["max_rounds"] = (
                    1 if type(current_rounds) is not int else max(1, min(current_rounds, 1))
                )
                reserve = min(3, max(0, int(search.get("challenge_reserve", 0))))
                seed_count = len(survey.get("seed_work_ids", []))
                current_works = max(1, int(search.get("max_works", 40)))
                search["max_works"] = max(seed_count + reserve + 1, min(current_works, 40))
                reserve = min(reserve, search["max_works"] - 1)
                search["challenge_reserve"] = reserve
                search["max_full_texts"] = max(1, min(int(search.get("max_full_texts", 8)), 8))
                search["max_api_calls"] = max(1, min(int(search.get("max_api_calls", 48)), 48))
                search["expansion_rounds"] = 0
                search["saturation_rounds"] = 1
                search["expansion_seed_count"] = 1
                if "max_analyzed_works" in search:
                    search["max_analyzed_works"] = max(
                        reserve + 1,
                        min(int(search["max_analyzed_works"]), search["max_works"], 24),
                    )
            else:
                # Expansion is deliberate and bounded. It increases
                # discovery, full-text, and citation capacity together so the
                # next gate does not simply see more abstracts while
                # remaining evidence-poor.
                journal_profile = self._paper_depth_profile(stage["id"])
                if journal_profile is not None:
                    # A journal continuation must widen the search graph and
                    # the full-text frontier together. Increasing only the
                    # abstract limit creates a larger bibliography without
                    # improving the evidence needed for methods and
                    # related-work claims.
                    search["queries_per_role"] = min(8, max(search.get("queries_per_role", 1) + 1, 4))
                    search["results_per_query"] = min(100, max(search.get("results_per_query", 1) + 10, 50))
                    search["max_works"] = min(1000, max(search.get("max_works", 1) + 50, 120))
                    search["max_full_texts"] = min(100, max(search.get("max_full_texts", 1) + 10, 20))
                    search["max_api_calls"] = min(1000, max(search.get("max_api_calls", 1) + 60, 100))
                    search["challenge_reserve"] = min(20, max(search.get("challenge_reserve", 0), 5))
                    search["saturation_rounds"] = min(8, max(search.get("saturation_rounds", 1), 3))
                    search["expansion_seed_count"] = min(20, max(search.get("expansion_seed_count", 1), 4))
                else:
                    search["max_works"] = min(1000, max(search.get("max_works", 1) + 10, 20))
                    search["max_full_texts"] = min(100, max(search.get("max_full_texts", 1) + 5, 5))
                    search["max_api_calls"] = min(1000, max(search.get("max_api_calls", 1) + 20, 40))
            search["expansion_rounds"] = min(8, search.get("expansion_rounds", 0) + 1)
            if "max_analyzed_works" in search:
                # A continuation may widen discovery, but it must not turn a
                # repair request into a wholesale remap of the catalog.  Keep
                # the substantive-analysis increment small and independent of
                # the provider page size; citation and full-text work are
                # selected separately by the survey funnel.
                increment = max(1, min(5, search.get("results_per_query", 1) // 10 or 1))
                search["max_analyzed_works"] = min(
                    search["max_works"], search["max_analyzed_works"] + increment)
            survey["revision"] = int(survey.get("revision", 1)) + self.continuation_cycles
            self._augment_full_text_routes(config, self._prior_stage_project("survey", self.context))
        elif kind == "experiment":
            config["project_id"] = str(Path(stage["project_dir"]).resolve())
            experiment = config.get("experiment", {})
            experiment["revision"] = int(experiment.get("revision", 1)) + self.continuation_cycles
            context = config.get("supplied_context", "")
            config["supplied_context"] = (
                f"{context}\nContinuation work orders:\n"
                + json.dumps(requests, ensure_ascii=False, sort_keys=True))
            if quota_recovery is not None:
                limits = config.setdefault("limits", {})
                current_rounds = limits.get("max_rounds")
                limits["max_rounds"] = (
                    1 if type(current_rounds) is not int else max(1, min(current_rounds, 1))
                )
                config["supplied_context"] += (
                    "\nBudget-recovery instruction: change the executable or measurement path, "
                    "preserve every prior failure as evidence, and stop after the first decisive "
                    "admission result. Do not regenerate the same program unchanged."
                )
        elif kind in {"interpretation", "paper"}:
            if kind == "paper" and config.get("schema_version") == "review-article-config-1":
                config["work_orders"] = self._follow_up_projection(requests)
                return config
            # These descriptors are intentionally strict.  Materialize an
            # immutable, cycle-specific packet instead of smuggling control
            # fields into the descriptor or rerunning with unchanged input.
            packet_key = "input_path" if kind == "interpretation" else "packet_path"
            self._materialize_follow_up_packet(stage, config, requests, packet_key)
        return config

    @staticmethod
    def _follow_up_projection(requests):
        return [{
            "kind": item["kind"],
            "objective": item["objective"],
            "why": item["why"],
            "success_condition": item["success_condition"],
            "evidence_needed": item["evidence_needed"],
            **({key: deepcopy(item[key]) for key in (
                "failure_dossier_ref", "failure_input_sha256", "repair_commands",
                "acceptance_checks", "review_directives", "model_diagnostics",
                "recovery_mode", "target_stage_id", "target_stage_kind",
                "repair_priority", "experiment_repair_plan", "repair_strategy",
                "attempt_lineage") if key in item}),
        } for item in requests]

    def _materialize_follow_up_packet(self, stage, config, requests, packet_key):
        source_value = config.get(packet_key)
        if not isinstance(source_value, str) or not Path(source_value).is_file():
            raise ValidationError(f"continuation {stage['kind']} packet is unreadable: {source_value}")
        try:
            packet = json.loads(Path(source_value).read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"continuation {stage['kind']} packet is not valid JSON: {source_value}") from exc
        if not isinstance(packet, dict):
            raise ValidationError(f"continuation {stage['kind']} packet must be a JSON object")
        packet = deepcopy(packet)
        packet["scientific_follow_up"] = self._follow_up_projection(requests)
        packet["follow_up_instruction"] = (
            "Address every supplied scientific follow-up by incorporating its evidence or analysis into this fresh pass. "
            "Keep unresolved explanations provisional and do not claim completion when the requested evidence is absent."
        )
        target = (Path(stage["project_dir"]).resolve() / "inputs"
                  / f"{stage['kind']}-follow-up-cycle-{self.continuation_cycles}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        body = canonical_bytes(packet)
        if target.exists() and target.read_bytes() != body:
            raise ValidationError(f"continuation packet was modified: {target}")
        if not target.exists():
            target.write_bytes(body)
        config[packet_key] = str(target)

    def _project_continuation_requests(self, packet, stage):
        """Give reopened interpretation and composition stages an explicit task.

        Their descriptors intentionally remain strict and immutable.  The
        scoped work order therefore travels with the fresh evidence packet,
        where the specialist can act on its objective and success condition
        without allowing Composer control vocabulary into the manuscript.
        """
        requests = self._requests_for_stage(stage)
        if not requests:
            return packet
        packet["scientific_follow_up"] = self._follow_up_projection(requests)
        packet["follow_up_instruction"] = (
            "Address every supplied scientific follow-up by incorporating its evidence or analysis into this fresh pass. "
            "Keep unresolved explanations provisional and do not claim completion when the requested evidence is absent."
        )
        return packet

    @staticmethod
    def _attempt_stage(stage, attempt_number):
        """Choose a retry workspace without discarding resumable stage state.

        SurveyRunner owns a durable work register and source checkpoint.  A
        provider failure should therefore reopen that same survey namespace;
        putting every retry in a new directory turns a recoverable role failure
        into a full literature restart.  Older runs created isolated survey
        attempts, so select the newest such directory when the base namespace
        has not yet been migrated.  Other stage kinds retain isolated retries.
        """
        candidate = deepcopy(stage)
        base = Path(stage["project_dir"]).resolve()
        if stage.get("kind") == "survey":
            durable_project = base
            if not (base / "state" / "control.sqlite").is_file():
                prior_attempts = []
                attempts_root = base / "attempts"
                if attempts_root.is_dir():
                    for path in attempts_root.iterdir():
                        if not path.is_dir() or not (path / "state" / "control.sqlite").is_file():
                            continue
                        try:
                            number = int(path.name.removeprefix("attempt-"))
                        except ValueError:
                            continue
                        if path.name == f"attempt-{number}":
                            prior_attempts.append((number, path))
                if prior_attempts:
                    durable_project = max(prior_attempts, key=lambda item: item[0])[1]
            candidate["project_dir"] = str(durable_project)
            candidate["reuse_completed"] = False
            candidate["reuse_output_path"] = None
            return candidate
        if attempt_number <= 1:
            return candidate
        candidate["project_dir"] = str(base / "attempts" / f"attempt-{attempt_number}")
        # A failed attempt is never a reusable checkpoint.  Successful stages
        # are skipped by the Composer before this helper is reached.
        candidate["reuse_completed"] = False
        candidate["reuse_output_path"] = None
        return candidate

    @staticmethod
    def _survey_resume_scope(prior):
        """Resume from the last accepted milestone, subject to fresh gate checks."""
        if prior.get("survey_current") is True and prior.get("survey_ref"):
            return "gap_assessment"
        return "focused_review"

    @staticmethod
    def _durable_stage_config(project_dir):
        """Read the immutable runner input from an existing stage workspace.

        Composer descriptors are templates.  Once a stage has been admitted,
        topic bindings, project identity, provider routes, and deadline
        projections are captured in the runner's ``inputs/run-config``
        artifact.  A legacy survey retry must resume that exact input rather
        than rebuilding it from today's template and accidentally turning a
        checkpoint migration into a different study.
        """
        project_dir = Path(project_dir)
        if not (project_dir / "state" / "control.sqlite").is_file():
            return None
        control = ControlStore(project_dir)
        try:
            store = ArtifactStore(control)
            head = store.head("inputs/run-config")
            if head is None:
                return None
            body = json.loads(store.read_body(head["body_hash"]))
            return body if isinstance(body, dict) else None
        except (OSError, TypeError, ValueError, KeyError):
            return None
        finally:
            control.close()

    def _record_retry_feedback(self, stage, *, attempt_number, retry_index, error, delay_seconds):
        """Record a retry decision before dispatching the next isolated attempt."""
        policy = self._retry_policy()
        event_id = f"composer-retry-{stage['id']}-{attempt_number}"
        topic_retry_reason = getattr(error, "topic_retry_reason", None)
        topic_retry = bool(getattr(error, "retryable_topic_intake", False))
        topic_pivot = (
            topic_retry_reason == "scientific_candidate_rejected"
            or bool(getattr(error, "rejected_topic_history", []))
        )
        feedback = {
            "event_id": event_id,
            "stage_id": stage["id"],
            "role": STAGE_ROLES[stage["kind"]],
            "from": COMMAND_ADDRESSES["progress"],
            "to": COMMAND_ADDRESSES["progress"],
            "status": "retrying",
            "action": "retry_stage",
            "attempt_number": attempt_number,
            "retry_index": retry_index,
            "retry_mode": policy.get("mode", "bounded"),
            "max_attempts": policy.get("max_attempts") if policy.get("mode", "bounded") == "bounded" else None,
            "delay_seconds": delay_seconds,
            "error": f"{type(error).__name__}: {error}",
            "retry_reason": (topic_retry_reason if topic_retry
                              else "transient_stage_failure"),
            "next_condition": (
                "abandon the rejected direction and sample a fresh topic portfolio within the remaining mission budget"
                if topic_pivot else
                "repair the topic intake contract and dispatch a fresh isolated proposal within the remaining mission budget"
                if topic_retry else
                "dispatch a fresh isolated stage attempt within the Composer hard deadline"
            ),
            "stage_deadline_seconds": stage["deadline_seconds"],
            "dependencies": list(stage["depends_on"]),
            "message_id": event_id,
            "message_disposition": "scheduled",
        }
        self.feedback.append(feedback)
        note = self._publish(
            f"command/composer/retry/{stage['id']}-{attempt_number}",
            "decision_note", feedback, "command.composer")
        self._route_feedback(feedback, note)

    def _retry_delay_seconds(self, error, retry_index):
        """Return the bounded backoff for one failed attempt."""
        policy = self._retry_policy()
        base_delay = float(policy["backoff_seconds"])
        if policy.get("mode", "bounded") == "until_deadline":
            base_delay = max(0.25, base_delay)
        delay = min(60.0, base_delay * (2 ** min(max(0, retry_index - 1), 6)))
        provider_delay = getattr(error, "retry_after_seconds", None)
        if (type(provider_delay) in (int, float) and math.isfinite(provider_delay)
                and provider_delay > 0):
            delay = max(delay, float(provider_delay))
        return delay

    def _retry_fits(self, stage, *, delay, downstream_seconds):
        policy = self._retry_policy()
        remaining = self._remaining()
        if policy.get("mode", "bounded") == "until_deadline":
            required_window = self._deadline_dispatch_floor()
        else:
            required_window = min(float(stage["estimate_seconds"]), downstream_seconds)
        return remaining > delay + max(0.2, required_window)

    def _schedule_adaptive_retry(self, stage, *, attempt_number, error,
                                 downstream_seconds):
        """Defer a failed stage and return control to the global agenda.

        The retry remains deadline-bounded, but it no longer monopolizes the
        stage loop. Other dependency-ready work can run during its backoff and
        the Composer re-evaluates the complete frontier before the next call.
        """
        retry_index = max(1, attempt_number)
        delay = self._retry_delay_seconds(error, retry_index)
        if not self._retry_fits(stage, delay=delay, downstream_seconds=downstream_seconds):
            return False
        next_attempt = attempt_number + 1
        self._record_retry_feedback(
            stage, attempt_number=next_attempt, retry_index=retry_index,
            error=error, delay_seconds=delay)
        self.retry_schedule[stage["id"]] = {
            "not_before_epoch": time.time() + delay,
            "delay_seconds": delay,
            "failed_attempt_number": attempt_number,
            "next_attempt_number": next_attempt,
            "error": f"{type(error).__name__}: {error}",
        }
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "yield_retry_to_agenda",
            "stage_id": stage["id"],
            "failed_attempt_number": attempt_number,
            "next_attempt_number": next_attempt,
            "not_before_epoch": self.retry_schedule[stage["id"]]["not_before_epoch"],
        })
        return True

    def _wait_before_retry(self, stage, *, attempt_number, retry_index, error, downstream_seconds):
        """Pace a retry without sleeping past the hard deadline."""
        delay = self._retry_delay_seconds(error, retry_index)
        if not self._retry_fits(stage, delay=delay, downstream_seconds=downstream_seconds):
            return False
        self._record_retry_feedback(stage, attempt_number=attempt_number, retry_index=retry_index,
                                    error=error,
                                    delay_seconds=delay)
        wait_until = self.clock() + delay
        while True:
            left = wait_until - self.clock()
            if left <= 0:
                return True
            self._remaining()
            self._checkpoint(f"{stage['id']}:retry_wait")
            time.sleep(min(5.0, left))

    def _research_state(self):
        """Project the current research frontier for checkpoints and dashboards."""
        completed = {
            stage_id for stage_id, record in self.stage_records.items()
            if isinstance(record, dict) and record.get("status") in STAGE_READY_STATUSES
        }
        if self.continuation_pending_stage_ids:
            completed.difference_update(self.continuation_pending_stage_ids)
        ready = [
            stage["id"] for stage in self.workflow["stages"]
            if stage["id"] not in completed
            and set(stage["depends_on"]).issubset(completed)
        ]
        active = [
            stage_id for stage_id, record in self.stage_records.items()
            if isinstance(record, dict)
            and record.get("status") in {"running", "retrying", "paused"}
        ]
        topic_context = next((
            value for value in self.context.values()
            if isinstance(value, dict) and value.get("kind") == "topic_discovery"
            and isinstance(value.get("topic"), dict)
        ), None)
        topic = topic_context.get("topic", {}) if topic_context else {}
        program = topic_context.get("research_program", {}) if topic_context else {}
        if self.active_research_requests or self.reopened_stage_ids:
            phase = "repair_and_revalidation"
        elif any(self.stage_records.get(stage["id"], {}).get("status") in STAGE_READY_STATUSES
                 for stage in self.workflow["stages"] if stage["kind"] == "paper"):
            phase = "release_candidate"
        elif ready:
            kinds = {stage["id"]: stage["kind"] for stage in self.workflow["stages"]}
            phase = {
                "topic_discovery": "topic_exploration",
                "survey": "evidence_mapping",
                "experiment": "empirical_probe",
                "interpretation": "mechanism_interpretation",
                "argument": "claim_construction",
                "paper": "manuscript_and_review",
            }.get(kinds.get(ready[0]), "research")
        else:
            phase = "waiting_for_frontier"
        return {
            "mode": self._agenda_policy()["mode"],
            "phase": phase,
            "frontier_stage_ids": ready,
            "active_stage_ids": active,
            "completed_stage_ids": sorted(completed),
            "reopened_stage_ids": sorted(self.reopened_stage_ids),
            "active_work_order_ids": [
                item.get("id") for item in self.active_research_requests
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ],
            "deferred_retry_stage_ids": sorted(self.retry_schedule),
            "topic": ({
                "id": topic.get("id"),
                "title": topic.get("title"),
                "research_question": topic.get("research_question"),
                "admission_state": topic_context.get("admission_state"),
            } if topic else None),
            "retained_branch_count": (
                len(program.get("branches", []))
                if isinstance(program, dict) and isinstance(program.get("branches"), list)
                else 0
            ),
            "last_agenda_decision": (
                deepcopy(self.agenda_decisions[-1]) if self.agenda_decisions else None),
        }

    def _agenda_order(self, ready_stages, *, completed, by_id):
        """Rank dependency-ready stages by current expected information value."""
        ready_stages = list(ready_stages)
        if not ready_stages:
            return []
        policy = self._agenda_policy()
        if policy["mode"] == "ordered":
            ordered = ready_stages
            candidates = [{
                "stage_id": stage["id"], "kind": stage["kind"],
                "score": float(len(ready_stages) - index),
                "factors": ["workflow_declaration_order"],
            } for index, stage in enumerate(ready_stages)]
        else:
            from scisaurus.runtime.departments import REQUEST_STAGE_KINDS

            information_value = {
                "topic_discovery": 5.0,
                "survey": 6.0,
                "experiment": 6.0,
                "interpretation": 4.0,
                "argument": 2.5,
                "paper": 1.0,
            }
            request_kinds = {
                REQUEST_STAGE_KINDS.get(item.get("kind"))
                for item in self.active_research_requests if isinstance(item, dict)
            }
            decision_index = len(self.agenda_decisions) + 1
            candidates = []
            for declaration_index, stage in enumerate(ready_stages):
                stage_id = stage["id"]
                score = information_value.get(stage["kind"], 0.0)
                factors = [f"information_value={score:.2f}"]
                if stage["kind"] in request_kinds:
                    score += 8.0
                    factors.append("active_work_order=+8.00")
                if stage_id in self.reopened_stage_ids:
                    score += 4.0
                    factors.append("reopened_scope=+4.00")
                immediate_repairs = [
                    item for item in self.active_research_requests
                    if isinstance(item, dict)
                    and item.get("repair_priority") == "immediate"
                    and item.get("target_stage_id") == stage_id
                ]
                if immediate_repairs:
                    score += 16.0
                    factors.append("immediate_repair_order=+16.00")
                if stage_id in self.workflow["completion"]["required_stage_ids"]:
                    score += 0.5
                    factors.append("required_output=+0.50")
                unlocks = sum(
                    1 for candidate in by_id.values()
                    if stage_id in candidate["depends_on"]
                    and candidate["id"] not in completed
                )
                if unlocks:
                    score += min(1.5, unlocks * 0.4)
                    factors.append(f"unlocks={unlocks}")
                attempts = self.stage_records.get(stage_id, {}).get("attempt_count", 0)
                if type(attempts) is int and attempts > 0:
                    penalty = min(0.75, attempts * 0.05)
                    score -= penalty
                    factors.append(f"retry_cost=-{penalty:.2f}")
                # Seeded jitter changes only close decisions.  It provides
                # controlled exploration without allowing a writing stage to
                # outrank a ready evidence-producing stage by chance alone.
                material = (
                    f"{self.exploration_seed}:agenda:{self.continuation_cycles}:"
                    f"{decision_index}:{stage_id}"
                ).encode("utf-8")
                jitter = int(hashlib.sha256(material).hexdigest()[:8], 16) / 0xFFFFFFFF * 0.45
                score += jitter
                factors.append(f"seeded_exploration=+{jitter:.2f}")
                candidates.append({
                    "stage_id": stage_id,
                    "kind": stage["kind"],
                    "score": round(score, 6),
                    "factors": factors,
                    "declaration_index": declaration_index,
                })
            ranked = sorted(
                candidates,
                key=lambda item: (-item["score"], item["declaration_index"], item["stage_id"]),
            )
            rank = {item["stage_id"]: index for index, item in enumerate(ranked)}
            ordered = sorted(ready_stages, key=lambda stage: rank[stage["id"]])
            candidates = ranked
        decision = {
            "schema_version": "composer-agenda-decision-1",
            "decision_id": f"agenda-{len(self.agenda_decisions) + 1}",
            "created_at": now_iso(),
            "cycle": self.continuation_cycles,
            "mode": policy["mode"],
            "completed_stage_ids": sorted(completed),
            "candidate_stages": [{key: deepcopy(item[key]) for key in (
                "stage_id", "kind", "score", "factors")}
                for item in candidates],
            "selected_stage_id": ordered[0]["id"],
            "reason": (
                "selected the dependency-ready work with the highest expected information value, "
                "scoped work-order urgency, and downstream unlock value"
                if policy["mode"] == "adaptive"
                else "selected the first dependency-ready stage in workflow declaration order"
            ),
        }
        artifact = self._publish(
            f"command/composer/agenda/{len(self.agenda_decisions) + 1}",
            "decision_note", decision, "command.composer")
        decision["artifact_ref"] = artifact["artifact_ref"]
        self.agenda_decisions.append(decision)
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "select_agenda_stage",
            "selected_stage_id": decision["selected_stage_id"],
            "candidate_stage_ids": [item["stage_id"] for item in decision["candidate_stages"]],
            "agenda_decision_ref": artifact["artifact_ref"],
        })
        return ordered

    def _checkpoint(self, phase, *, force=False):
        now = self.clock()
        if not force and now < self.next_checkpoint:
            return
        # Refresh the organization only on the Composer thread.  The live
        # ticker consumes the cached plain-data projection below.
        self.organization_snapshot = deepcopy(self.departments.snapshot())
        remaining_snapshot = self.deadline - now
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining_snapshot = min(remaining_snapshot, self.deadline_epoch - time.time())
        self.state_revision += 1
        # Specialist callbacks publish a live card between stage-boundary
        # checkpoints.  Rebuilding ``stages`` from the durable stage records
        # used to erase those cards on the next ordinary checkpoint, leaving
        # the dashboard unable to distinguish an admitted role from an actual
        # provider call.  Carry only cards belonging to the current stage
        # assignment; completed/retired attempts must not look live.
        with self._progress_lock:
            previous_stages = deepcopy(self._progress_snapshot.get("stages", {}))
        checkpoint_stages = deepcopy(self.stage_records)
        for stage_id, record in checkpoint_stages.items():
            if not isinstance(record, dict) or record.get("status") != "running":
                continue
            previous = previous_stages.get(stage_id, {})
            live = previous.get("specialist_live") if isinstance(previous, dict) else None
            if not isinstance(live, dict):
                continue
            allowed_tasks = {
                item for item in record.get("assignment_task_ids", [])
                if isinstance(item, str)
            }
            if allowed_tasks:
                live = {
                    role: event for role, event in live.items()
                    if isinstance(event, dict) and event.get("task_id") in allowed_tasks
                }
            if live:
                record["specialist_live"] = live
        active_blockers = self._active_blockers()
        blocker_projection = {
            "active_blockers": active_blockers,
            "blocker_counts": {
                "active": len(active_blockers),
                "historical": len(self.blockers),
            },
        }
        state = {
            "schema_version": "composer-checkpoint-1", "workflow_id": self.workflow["id"],
            "run_id": self.run_id,
            "status": self.status, "phase": phase,
            "elapsed_seconds": max(0.0, now - self.started),
            "remaining_seconds": max(0.0, remaining_snapshot),
            "started_at_epoch": self.started_epoch, "deadline_at_epoch": self.deadline_epoch,
            "retry_policy": self._retry_policy(),
            "continuation_policy": self._continuation_policy(),
            "agenda_policy": self._agenda_policy(),
            "agenda_decisions": deepcopy(self.agenda_decisions),
            "retry_schedule": deepcopy(self.retry_schedule),
            "state_revision": self.state_revision,
            "research_state": self._research_state(),
            "exploration_seed": self.exploration_seed,
            "continuation_cycles": self.continuation_cycles,
            "continuation_budget_baseline": self._continuation_budget_baseline,
            "continuation_budget_used": self._continuation_budget_used(),
            "reopened_stage_ids": sorted(self.reopened_stage_ids),
            "continuation_pending_stage_ids": sorted(self.continuation_pending_stage_ids),
            "active_research_requests": deepcopy(self.active_research_requests),
            "department_activity": deepcopy(self.department_activity),
            "deadline_extensions": deepcopy(self.deadline_extensions),
            "stages": checkpoint_stages, "context": deepcopy(self.context),
            "feedback": deepcopy(self.feedback),
            "blockers": deepcopy(self.blockers), **blocker_projection,
            "usage": deepcopy(self.usage),
            "foundry_usage": deepcopy(self.foundry_usage),
            "deadline_decisions": deepcopy(self.deadline_decisions),
            "organization": deepcopy(self.organization_snapshot),
            "topic_history_path": str(self.topic_history_path),
            "topic_history_scope": self.topic_history_scope,
            "topic_history_entries": len(self.topic_history.get("entries", [])),
        }
        with self._progress_lock:
            self._progress_snapshot = deepcopy(state)
        self._publish(f"command/composer/checkpoints/{len(self.feedback) + len(self.stage_records) + 1}",
                      "progress_checkpoint", state, "command.composer")
        with self._progress_lock:
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "progress.json").write_bytes(canonical_bytes(state))
        self.next_checkpoint = now + float(self.workflow["time_policy"]["checkpoint_seconds"])
        self.on_progress({"phase": phase, "elapsed_seconds": round(state["elapsed_seconds"], 2),
                          "remaining_seconds": round(state["remaining_seconds"], 2),
                          "stages": deepcopy(state.get("stages", {})),
                          "blockers": deepcopy(state.get("active_blockers", [])),
                          "active_blockers": deepcopy(state.get("active_blockers", [])),
                          "blocker_counts": deepcopy(state.get("blocker_counts", {})),
                          "usage": deepcopy(state.get("usage", {})),
                          "foundry_usage": deepcopy(state.get("foundry_usage", {})),
                          "organization": deepcopy(state["organization"])})

    def _live_progress(self, phase, *, epoch=None):
        """Publish a non-ledger progress tick while a provider call is active.

        A long model request may not emit a semantic event until it returns.
        The tick deliberately writes only the live checkpoint projection; it
        does not create a decision or alter task state. Durable control events
        continue to be written by ``_checkpoint`` at stage boundaries.
        """
        now = self.clock()
        remaining_snapshot = self.deadline - now
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining_snapshot = min(remaining_snapshot, self.deadline_epoch - time.time())
        # The ticker must not read mutable Composer state while a specialist
        # callback is updating it.  It consumes the last complete checkpoint
        # and changes only the heartbeat fields for this atomic write.
        with self._progress_lock:
            if epoch is not None and epoch != self._live_progress_epoch:
                return
            state = deepcopy(self._progress_snapshot)
            state.setdefault("organization", deepcopy(self.organization_snapshot))
            state.update({
                "schema_version": "composer-checkpoint-1", "workflow_id": self.workflow["id"],
                "run_id": self.run_id,
                "phase": phase, "elapsed_seconds": max(0.0, now - self.started),
                "remaining_seconds": max(0.0, remaining_snapshot),
                "started_at_epoch": self.started_epoch, "deadline_at_epoch": self.deadline_epoch,
            })
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            temporary = output / f"progress-live-{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(canonical_bytes(state))
            temporary.replace(output / "progress.json")
            # Keep the heartbeat fields and specialist cards in the in-memory
            # base as well. The next specialist event must build on the latest
            # live state instead of restoring the last stage-boundary snapshot.
            self._progress_snapshot = deepcopy(state)
        self.on_progress({"phase": phase, "elapsed_seconds": round(state["elapsed_seconds"], 2),
                          "remaining_seconds": round(state["remaining_seconds"], 2),
                          "stages": deepcopy(state.get("stages", {})),
                          "blockers": deepcopy(state.get("active_blockers", [])),
                          "active_blockers": deepcopy(state.get("active_blockers", [])),
                          "blocker_counts": deepcopy(state.get("blocker_counts", {})),
                          "usage": deepcopy(state.get("usage", {})),
                          "foundry_usage": deepcopy(state.get("foundry_usage", {})),
                          "organization": deepcopy(state["organization"])})

    def _start_live_progress(self, stage):
        """Start a bounded ticker for one admitted stage."""
        # Durable checkpoint cadence may be deliberately coarse for large
        # artifacts, but the supervisor and dashboard need a much tighter
        # liveness signal while a provider/worker is active.
        interval = max(0.5, min(
            15.0, float(self.workflow["time_policy"]["checkpoint_seconds"])))
        stop = threading.Event()
        with self._progress_lock:
            self._live_progress_epoch += 1
            epoch = self._live_progress_epoch

        def tick():
            while not stop.wait(interval):
                try:
                    self._live_progress(f"{stage['id']}:running", epoch=epoch)
                except Exception:
                    # A live status tick must never hide the stage's real
                    # result or turn a completed provider call into a failure.
                    return

        thread = threading.Thread(target=tick, name=f"composer-progress-{stage['id']}", daemon=True)
        thread.start()

        def finish():
            with self._progress_lock:
                self._live_progress_epoch += 1
            stop.set()
            thread.join(timeout=min(interval, 2.0))

        return finish

    @staticmethod
    def _ready_stage_ids(state):
        stages = state.get("stages", {}) if isinstance(state, dict) else {}
        if not isinstance(stages, dict):
            return set()
        return {
            stage_id for stage_id, record in stages.items()
            if isinstance(record, dict) and record.get("status") in STAGE_READY_STATUSES
        }

    @classmethod
    def _checkpoint_advances_terminal_state(cls, checkpoint, terminal):
        """Recognize an in-flight checkpoint that supersedes a stale final report."""
        if not isinstance(checkpoint, dict) or checkpoint.get("status") != "running":
            return False
        if not isinstance(terminal, dict):
            return True
        checkpoint_revision = checkpoint.get("state_revision", 0)
        terminal_revision = terminal.get("state_revision", 0)
        if (type(checkpoint_revision) is int and checkpoint_revision >= 0
                and type(terminal_revision) is int and terminal_revision >= 0):
            if checkpoint_revision > terminal_revision:
                return True
            if checkpoint_revision < terminal_revision:
                return False
        checkpoint_ready = cls._ready_stage_ids(checkpoint)
        terminal_ready = cls._ready_stage_ids(terminal)
        if checkpoint_ready > terminal_ready:
            return True
        if checkpoint_ready != terminal_ready:
            return False

        # A retrying stage can make durable progress without changing the set
        # of ready dependencies.  Compare its attempt frontier as well, or a
        # stale terminal report can hide the only checkpoint that tells resume
        # which isolated attempt directory is safe to create next.
        def attempt_counts(state):
            stages = state.get("stages", {})
            if not isinstance(stages, dict):
                return {}
            counts = {}
            for stage_id, record in stages.items():
                if not isinstance(record, dict):
                    continue
                value = record.get("attempt_count", record.get("attempt_number", 0))
                if type(value) is int and value >= 0:
                    counts[stage_id] = value
            return counts

        checkpoint_counts = attempt_counts(checkpoint)
        terminal_counts = attempt_counts(terminal)
        if any(
            checkpoint_counts.get(stage_id, 0) > terminal_counts.get(stage_id, 0)
            for stage_id in checkpoint_counts
        ):
            return True
        checkpoint_agenda = checkpoint.get("agenda_decisions", [])
        terminal_agenda = terminal.get("agenda_decisions", [])
        return (isinstance(checkpoint_agenda, list)
                and isinstance(terminal_agenda, list)
                and len(checkpoint_agenda) > len(terminal_agenda))

    @staticmethod
    def _is_process_interruption_state(state):
        """Identify a checkpoint or report produced by a process-level stop."""
        if not isinstance(state, dict):
            return False
        if state.get("phase") == "paused_process_interruption":
            return True
        if state.get("stop_reason") == "process_interrupted":
            return True
        interim = state.get("interim_report")
        return (isinstance(interim, dict)
                and interim.get("stop_reason") == "process_interrupted")

    def _latest_inflight_checkpoint(self, terminal=None):
        """Read the newest durable running checkpoint when output was finalized stale."""
        # One Composer checkpoint contains the full research context and can
        # be tens of megabytes.  The previous unbounded scan reparsed every
        # historical checkpoint during resume, making a normal restart look
        # hung after a long run.  Check only a small newest window: checkpoints
        # are append-only and the newest valid body is the only candidate that
        # can advance the terminal report.
        rows = self.control._conn.execute(
            "SELECT manifest_json FROM artifacts "
            "WHERE logical_id LIKE 'command/composer/checkpoints/%' "
            "ORDER BY created_at DESC LIMIT 8"
        ).fetchall()
        for row in rows:
            try:
                manifest = json.loads(row["manifest_json"])
                if manifest.get("artifact_type") != "progress_checkpoint":
                    continue
                checkpoint = json.loads(self.store.read_body(manifest["body_hash"]))
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if (isinstance(checkpoint, dict)
                    and checkpoint.get("workflow_id") == self.workflow["id"]
                    and checkpoint.get("status") == "running"):
                if terminal is not None and not self._checkpoint_advances_terminal_state(checkpoint, terminal):
                    continue
                return checkpoint
        return None

    def _restore(self):
        timing_state = None
        # The run input is the earliest durable record and carries the seed
        # even when a process exits before its first stage checkpoint.
        run_input = self.store.head("inputs/composer-run")
        if run_input is not None:
            try:
                input_body = json.loads(self.store.read_body(run_input["body_hash"]))
            except (OSError, TypeError, ValueError):
                input_body = None
            if (isinstance(input_body, dict) and type(input_body.get("exploration_seed")) is int
                    and input_body["exploration_seed"] >= 0):
                self.exploration_seed = input_body["exploration_seed"]
            input_agenda = input_body.get("agenda_policy") if isinstance(input_body, dict) else None
            if ("agenda_policy" not in self.workflow
                    and isinstance(input_agenda, dict)
                    and input_agenda.get("mode") in {"adaptive", "ordered"}):
                self._restored_agenda_policy = {"mode": input_agenda["mode"]}
        head = self.store.head("command/composer/run")
        head_status = None
        head_body = None
        if head is not None:
            body = json.loads(self.store.read_body(head["body_hash"]))
            head_body = body
            self.status = body.get("status", "running")
            head_status = self.status
            self.stage_records = body.get("stages", {})
            self.context = body.get("context", {})
            self.feedback = body.get("feedback", [])
            self.blockers = body.get("blockers", [])
            self.usage = body.get("usage", self.usage)
            self.foundry_usage = body.get("foundry_usage", {})
            self.deadline_decisions = body.get("deadline_decisions", [])
            self.agenda_decisions = body.get("agenda_decisions", [])
            self.retry_schedule = body.get("retry_schedule", {})
            if not isinstance(self.retry_schedule, dict):
                self.retry_schedule = {}
            revision = body.get("state_revision", 0)
            if type(revision) is int and revision >= 0:
                self.state_revision = revision
            restored_agenda = body.get("agenda_policy")
            if ("agenda_policy" not in self.workflow
                    and isinstance(restored_agenda, dict)
                    and restored_agenda.get("mode") in {"adaptive", "ordered"}):
                self._restored_agenda_policy = {"mode": restored_agenda["mode"]}
            self.continuation_cycles = body.get("continuation_cycles", 0)
            self.reopened_stage_ids = set(body.get("reopened_stage_ids", []))
            self.continuation_pending_stage_ids = set(body.get("continuation_pending_stage_ids", []))
            self.active_research_requests = body.get("active_research_requests", [])
            self.department_activity = body.get("department_activity", [])
            self.deadline_extensions = body.get("deadline_extensions", [])
            if (type(body.get("exploration_seed")) is int
                    and body["exploration_seed"] >= 0):
                self.exploration_seed = body["exploration_seed"]
            if (isinstance(body.get("organization"), dict)
                    and body["organization"].get("schema_version") == self.departments.organization["schema_version"]):
                self.organization_snapshot = deepcopy(body["organization"])
            self._progress_snapshot = deepcopy(body)
            timing_state = body
        progress_path = self.root / "output" / "progress.json"
        # A process can die after a checkpoint but before publishing the final
        # run report.  Restore that checkpoint as the authoritative in-flight
        # state so the next Composer invocation can mark an interrupted
        # attempt unknown and dispatch a fresh one.  If cleanup did publish a
        # stale terminal report afterward, recover the newest durable running
        # checkpoint only when it contains strictly more admitted stages.
        checkpoint = None
        if progress_path.is_file():
            try:
                checkpoint = json.loads(progress_path.read_text())
            except (OSError, ValueError):
                checkpoint = None
        interrupted_pause = self._is_process_interruption_state(head_body)
        if head is None or head_status == "running":
            live_checkpoint = checkpoint if isinstance(checkpoint, dict) else self._latest_inflight_checkpoint()
        elif interrupted_pause and isinstance(checkpoint, dict):
            # SIGINT/KeyboardInterrupt publishes a paused final report after
            # the same attempt-boundary checkpoint.  That checkpoint is the
            # authoritative frontier even when its revision and attempt
            # count are equal to the final report; requiring a strictly
            # larger frontier would discard the only record that marks the
            # in-flight attempt for result-unknown reconciliation.
            live_checkpoint = checkpoint
        elif self._checkpoint_advances_terminal_state(checkpoint, head_body):
            live_checkpoint = checkpoint
        else:
            # The newest running checkpoint may belong to a failed resume and
            # may have fewer attempts than an older checkpoint from the
            # interrupted process.  Scan until an actually advancing one is
            # found instead of returning the first stale candidate.
            live_checkpoint = self._latest_inflight_checkpoint(head_body)
        if isinstance(live_checkpoint, dict):
            timing_state = live_checkpoint
            self.stage_records = live_checkpoint.get("stages", self.stage_records)
            if "context" in live_checkpoint:
                self.context = live_checkpoint.get("context", self.context)
            self.feedback = live_checkpoint.get("feedback", self.feedback)
            self.blockers = live_checkpoint.get("blockers", self.blockers)
            self.usage = live_checkpoint.get("usage", self.usage)
            self.foundry_usage = live_checkpoint.get("foundry_usage", {})
            self.deadline_decisions = live_checkpoint.get("deadline_decisions", self.deadline_decisions)
            self.agenda_decisions = live_checkpoint.get(
                "agenda_decisions", self.agenda_decisions)
            retry_schedule = live_checkpoint.get("retry_schedule", self.retry_schedule)
            if isinstance(retry_schedule, dict):
                self.retry_schedule = retry_schedule
            revision = live_checkpoint.get("state_revision", self.state_revision)
            if type(revision) is int and revision >= 0:
                self.state_revision = revision
            restored_agenda = live_checkpoint.get("agenda_policy")
            if ("agenda_policy" not in self.workflow
                    and isinstance(restored_agenda, dict)
                    and restored_agenda.get("mode") in {"adaptive", "ordered"}):
                self._restored_agenda_policy = {"mode": restored_agenda["mode"]}
            self.continuation_cycles = live_checkpoint.get("continuation_cycles", self.continuation_cycles)
            self.reopened_stage_ids = set(live_checkpoint.get("reopened_stage_ids", self.reopened_stage_ids))
            self.continuation_pending_stage_ids = set(live_checkpoint.get(
                "continuation_pending_stage_ids", self.continuation_pending_stage_ids))
            self.active_research_requests = live_checkpoint.get(
                "active_research_requests", self.active_research_requests)
            self.department_activity = live_checkpoint.get("department_activity", self.department_activity)
            self.deadline_extensions = live_checkpoint.get("deadline_extensions", self.deadline_extensions)
            if (type(live_checkpoint.get("exploration_seed")) is int
                    and live_checkpoint["exploration_seed"] >= 0):
                self.exploration_seed = live_checkpoint["exploration_seed"]
            if (isinstance(live_checkpoint.get("organization"), dict)
                    and live_checkpoint["organization"].get("schema_version") == self.departments.organization["schema_version"]):
                self.organization_snapshot = deepcopy(live_checkpoint["organization"])
            self._progress_snapshot = deepcopy(live_checkpoint)
        self._restore_context_from_stage_records()
        self._hydrate_provisional_handoffs()
        # Older Composer reports did not carry an epoch fence.  They retain
        # their historical restart behavior; every new run persists the
        # fields above so future resumes remain inside one mission wall.
        if isinstance(timing_state, dict):
            started_epoch = timing_state.get("started_at_epoch")
            deadline_epoch = timing_state.get("deadline_at_epoch")
            if (type(started_epoch) in (int, float) and math.isfinite(started_epoch)
                    and type(deadline_epoch) in (int, float) and math.isfinite(deadline_epoch)
                    and deadline_epoch >= started_epoch):
                self.started_epoch = float(started_epoch)
                self.deadline_epoch = float(deadline_epoch)
                elapsed_hint = timing_state.get("elapsed_seconds", 0.0)
                if type(elapsed_hint) not in (int, float) or not math.isfinite(elapsed_hint) or elapsed_hint < 0:
                    elapsed_hint = 0.0
                wall_elapsed = max(0.0, time.time() - self.started_epoch)
                elapsed = max(float(elapsed_hint), wall_elapsed)
                self.started = self.clock() - elapsed
                # Rebuild the live monotonic fence from the persisted wall
                # fence, including any explicit extensions.  Reapplying only
                # the original workflow duration would silently discard a
                # previously recorded extension on the next resume.
                wall_remaining = self.deadline_epoch - time.time()
                self.deadline = self.clock() + wall_remaining
        if self.status in {"completed", "candidate_needs_review", "research_expansion_required", "review_rejected",
                           "blocked", "paused"}:
            # A resumed workflow must explicitly continue from a non-terminal
            # checkpoint; completed output remains inspectable and immutable.
            self.status = "running"

    def _restore_context_from_stage_records(self):
        """Rehydrate completed stage packets when resuming an older checkpoint.

        Early Composer checkpoints persisted stage records but not the in-memory
        context.  A resume must still be able to bind accepted survey,
        experiment, interpretation, and argument outputs without replaying
        expensive upstream work or admitting a consumer against no packet.
        New checkpoints carry ``context`` directly; this reconstruction is a
        compatibility path for those already on disk.
        """
        by_id = {stage["id"]: stage for stage in self.workflow["stages"]}
        for stage_id, record in self.stage_records.items():
            if stage_id in self.context or not isinstance(record, dict):
                continue
            stage = by_id.get(stage_id)
            if stage is None:
                continue
            candidates = [record.get("output_path")]
            project_dir = record.get("project_dir") or stage.get("project_dir")
            if isinstance(project_dir, str):
                candidates.append(str(Path(project_dir) / "output" / "run.json"))
            output_path = next((Path(item) for item in candidates
                                if isinstance(item, str) and Path(item).is_file()), None)
            if output_path is None:
                continue
            try:
                payload = json.loads(output_path.read_text())
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            context = {**payload, "stage_id": stage_id, "kind": stage["kind"],
                       "project_dir": str(Path(stage.get("project_dir", project_dir)).resolve()),
                       "output_path": str(output_path.resolve())}
            if stage["kind"] == "experiment":
                context["results_package"] = payload.get("results_package")
            if stage["kind"] == "interpretation" and "interpretation" not in context:
                provisional = payload.get("provisional_interpretation")
                if isinstance(provisional, dict):
                    context["interpretation"] = provisional
                    context["binding_output_path"] = payload.get(
                        "provisional_interpretation_path")
                else:
                    context["interpretation"] = payload
            if stage["kind"] == "argument":
                context["argument_package_path"] = str(output_path.resolve())
            if (stage["kind"] == "topic_discovery" and "research_program" not in context
                    and isinstance(context.get("candidates"), list)
                    and isinstance(context.get("selected_id"), str)):
                from scisaurus.runtime.research_program import build_research_program
                context["research_program"] = build_research_program(context)
            self.context[stage_id] = context

    def _hydrate_provisional_handoffs(self):
        """Restore typed incumbent inputs for forward-first checkpoints.

        Older provisional checkpoints persisted the Composer decision and the
        forward-progress path, but not the incumbent package needed by a
        downstream binding.  Hydrate that adapter from the immutable attempt
        ledger before dependency admission; never turn the provisional status
        into an accepted scientific result.
        """
        by_id = {stage["id"]: stage for stage in self.workflow.get("stages", [])
                 if isinstance(stage, dict) and isinstance(stage.get("id"), str)}
        for stage_id, record in self.stage_records.items():
            stage = by_id.get(stage_id)
            context = self.context.get(stage_id)
            if (stage is None or not isinstance(record, dict)
                    or not isinstance(context, dict)
                    or record.get("composer_decision") != "advance_with_findings"):
                continue
            prior_artifact = self._prior_stage_artifact(
                stage, record.get("attempts", []))
            if stage.get("kind") == "interpretation" and prior_artifact is not None:
                package = self._interpretation_package_from_artifact(prior_artifact)
                if package is not None and not isinstance(context.get("interpretation"), dict):
                    context["interpretation"] = package
                    context["binding_output_path"] = prior_artifact["path"]
                    context["incumbent_interpretation_path"] = prior_artifact["path"]
            elif stage.get("kind") == "argument" and prior_artifact is not None:
                context.setdefault("binding_output_path", prior_artifact["path"])
                context.setdefault("incumbent_argument_path", prior_artifact["path"])
            self.context[stage_id] = context

            # SurveyRunner records the useful survey packet before a late
            # gap-assessment failure.  Older Composer checkpoints retained
            # only the forward-progress projection, which dropped the
            # survey/assessment refs and made the next experiment look like a
            # missing-input failure.  Rehydrate only the typed handoff fields
            # from the newest immutable attempt result; this does not turn the
            # failed survey into an accepted stage.
            if stage.get("kind") != "survey":
                continue
            needs_handoff = any(
                context.get(key) in (None, "")
                for key in ("project_dir", "survey_ref", "assessment_ref", "nomination_ref")
            )
            if not needs_handoff:
                continue
            payload = None
            payload_project = None
            attempts = record.get("attempts", [])
            for attempt in reversed(attempts if isinstance(attempts, list) else []):
                if not isinstance(attempt, dict):
                    continue
                project_dir = attempt.get("project_dir")
                if not isinstance(project_dir, str):
                    continue
                root = Path(project_dir)
                candidates = [root / "output" / "run.json"]
                if root.name.startswith("attempt-"):
                    candidates.append(root.parent.parent / "output" / "run.json")
                for candidate_path in candidates:
                    if not candidate_path.is_file():
                        continue
                    try:
                        candidate_payload = json.loads(candidate_path.read_text())
                    except (OSError, TypeError, ValueError):
                        continue
                    if isinstance(candidate_payload, dict):
                        payload = candidate_payload
                        payload_project = root
                        break
                if payload is not None:
                    break
            if not isinstance(payload, dict):
                continue
            for key in (
                    "survey_ref", "assessment_ref", "nomination_ref", "survey_current",
                    "assessment_current", "gap_state", "topic_admission"):
                if key in payload and context.get(key) in (None, ""):
                    context[key] = deepcopy(payload[key])
            if context.get("project_dir") in (None, "") and payload_project is not None:
                context["project_dir"] = str(payload_project.resolve())
            context["handoff_rehydrated_from_failed_attempt"] = True
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "hydrate_failed_survey_handoff",
                "stage_id": stage_id,
                "project_dir": context.get("project_dir"),
                "survey_ref": context.get("survey_ref"),
                "assessment_ref": context.get("assessment_ref"),
                "assessment_current": context.get("assessment_current"),
            })
            self.context[stage_id] = context

    def _reconcile_stale_forward_handoffs(self, by_id):
        """Reopen legacy provisional nodes that were released without input.

        Older forward-first checkpoints could mark a stage as dependency-ready
        immediately after issuing a scientific repair order. On resume that
        record looked like an ordinary ``advance_with_findings`` candidate, so
        the scheduler admitted a consumer even when the experiment had never
        produced a result. Reconstruct the typed repair order from the durable
        recovery ledger before dependency admission and put the node back
        behind its owning stage.
        """
        reconciled = []
        for stage_id, record in self.stage_records.items():
            stage = by_id.get(stage_id)
            context = self.context.get(stage_id)
            if (not isinstance(stage, dict) or not isinstance(record, dict)
                    or not isinstance(context, dict)
                    or record.get("composer_decision") != "advance_with_findings"):
                continue
            recovery = context.get("failure_recovery")
            recovery = recovery if isinstance(recovery, dict) else {}
            error_text = str(context.get("error") or "")
            legacy_author_contract = (
                stage.get("kind") == "experiment"
                and context.get("results_status") == "not_executed"
                and "program author did not finish normally" in error_text.casefold()
            )
            legacy_program_failure = (
                stage.get("kind") == "experiment"
                and context.get("results_status") == "not_executed"
                and not legacy_author_contract
                and recovery.get("failure_class") == "model_contract"
                and classify_failure(
                    stage.get("kind"), context.get("error"), context
                ) == "experiment_failure"
            )
            actionable_recovery = (
                recovery.get("recovery_mode") in {
                    "repair_then_rerun", "format_repair_then_rerun",
                }
                and (
                    isinstance(context.get("failure_dossier_ref"), str)
                    or isinstance(recovery.get("dossier_ref"), str)
                    or isinstance(context.get("repair_commands"), list)
                )
            )
            unexecuted_experiment = (
                stage.get("kind") == "experiment"
                and context.get("results_status") == "not_executed"
            )
            if not actionable_recovery and not unexecuted_experiment:
                continue

            requests = [item for item in context.get("research_requests", [])
                        if isinstance(item, dict)]
            if legacy_author_contract:
                # Older checkpoints recorded the model author's truncated
                # response as a scientific program failure.  Repair only the
                # response contract on resume; do not open a Methods code
                # repair order for a program that never existed.
                format_commands = build_repair_commands(
                    "experiment", "model_contract", error_text=error_text)
                recovery = deepcopy(recovery)
                recovery.update({
                    "failure_class": "model_contract",
                    "recovery_mode": "format_repair_then_rerun",
                    "requires_capability_repair": False,
                    "repair_commands": deepcopy(format_commands),
                    "acceptance_checks": [
                        item.get("acceptance_check") for item in format_commands
                        if isinstance(item, dict) and item.get("acceptance_check")
                    ],
                })
                context["failure_recovery"] = recovery
                context["format_recovery"] = True
                context["review_status"] = "model_contract_repair"
                context["repair_commands"] = deepcopy(format_commands)
                context["acceptance_checks"] = deepcopy(recovery["acceptance_checks"])
                request = self._format_contract_recovery_request(stage, context)
                requests = [request] if isinstance(request, dict) else []
            elif legacy_program_failure:
                # Migrate checkpoints written before program-author failures
                # were distinguished from malformed provider envelopes.  The
                # old packet may contain a schema-only order, but an
                # unexecuted generated program needs a Methods repair order so
                # the next attempt edits the executable source and replays it.
                error_text = context.get("error") or "legacy experiment program failure"
                commands = build_repair_commands(
                    "experiment", "experiment_failure",
                    stage_result=context,
                    error_text=error_text,
                    review_directives=(context.get("review_directives")
                                       if isinstance(context.get("review_directives"), list)
                                       else recovery.get("review_directives", [])),
                )
                dossier = {
                    "stage_kind": "experiment",
                    "failure_class": "experiment_failure",
                    "error": error_text,
                    "input_sha256": recovery.get("input_sha256")
                    or context.get("failure_input_sha256") or "",
                    "repair_commands": commands,
                    "acceptance_checks": [
                        item.get("acceptance_check") for item in commands
                        if isinstance(item, dict) and item.get("acceptance_check")
                    ],
                    "review_directives": deepcopy(
                        context.get("review_directives")
                        if isinstance(context.get("review_directives"), list)
                        else recovery.get("review_directives", [])
                    ),
                }
                request = build_repair_request(dossier, stage_id=stage_id)
                request["failure_dossier_ref"] = (
                    context.get("failure_dossier_ref")
                    or recovery.get("dossier_ref"))
                requests = [request]
                recovery = deepcopy(recovery)
                recovery.update({
                    "failure_class": "experiment_failure",
                    "recovery_mode": "repair_then_rerun",
                    "requires_capability_repair": True,
                    "repair_commands": deepcopy(commands),
                    "acceptance_checks": deepcopy(dossier["acceptance_checks"]),
                })
                context["failure_recovery"] = recovery
                context["format_recovery"] = False
                context["review_status"] = "scientific_assignment_blocked"
            if not requests:
                if recovery.get("recovery_mode") == "format_repair_then_rerun":
                    request = self._format_contract_recovery_request(stage, context)
                    if isinstance(request, dict):
                        requests = [request]
                elif actionable_recovery:
                    dossier = {
                        "stage_kind": stage.get("kind"),
                        "failure_class": recovery.get("failure_class") or "scientific_hold",
                        "error": context.get("error") or "stale provisional handoff",
                        "input_sha256": recovery.get("input_sha256")
                        or context.get("failure_input_sha256", ""),
                        "repair_commands": context.get("repair_commands")
                        or recovery.get("repair_commands", []),
                        "acceptance_checks": context.get("acceptance_checks")
                        or recovery.get("acceptance_checks", []),
                        "review_directives": context.get("review_directives")
                        or recovery.get("review_directives", []),
                    }
                    request = build_repair_request(dossier, stage_id=stage_id)
                    request["failure_dossier_ref"] = (
                        context.get("failure_dossier_ref")
                        or recovery.get("dossier_ref"))
                    requests = [request]
                else:
                    debt = context.get("failure_debt")
                    if not isinstance(debt, dict):
                        debt = {
                            "stage_id": stage_id,
                            "kind": stage.get("kind"),
                            "failure_class": "scientific_hold",
                            "error": context.get("error") or "experiment produced no result",
                            "attempts": record.get("attempt_count", 0),
                        }
                    request = self._forward_debt_work_order(stage_id, debt)
                    if isinstance(request, dict):
                        requests = [request]
            if not requests:
                # A malformed legacy packet must not be released merely because
                # it carries the old Composer decision. Leave it visible for
                # the normal terminal recovery path instead.
                continue

            context.update({
                "status": "research_expansion_required",
                "review_status": "scientific_assignment_blocked",
                "composer_decision": "repair_required",
                "progression_state": "repair_required",
                "release_blocking": True,
                "research_requests": deepcopy(requests),
                "deferred_research_requests": [],
                "preserve_work_orders": True,
                "stale_forward_handoff_reconciled": True,
            })
            record.update({
                "status": "retrying",
                "composer_decision": "repair_required",
                "progression_state": "repair_required",
                "release_blocking": True,
                "results_status": context.get("results_status"),
                "recovery_admitted": True,
            })
            self.context[stage_id] = context
            self.stage_records[stage_id] = record
            self.continuation_pending_stage_ids.add(stage_id)
            reconciled.append({
                "stage_id": stage_id,
                "kind": stage.get("kind"),
                "request_ids": [item.get("id") for item in requests
                                 if isinstance(item.get("id"), str)],
                "reason": "legacy forward-first handoff had an actionable repair order or no experiment result",
            })
        if reconciled:
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "reconcile_stale_forward_handoffs",
                "stages": reconciled,
                "next_action": "execute the owning repair order before admitting downstream inputs",
            })
        return reconciled

    def _restored_topic_feasibility_failure(self, by_id):
        """Recheck a completed topic against the current execution boundary.

        A checkpoint can outlive the code that produced it.  If a newer
        Composer adds a required feasibility contract, letting an older topic
        flow directly into survey or experiment would bypass the new gate.
        Return the affected topic stage and a durable reason so ``run`` can
        reopen its dependency closure through the ordinary continuation path.
        """
        if not any(stage.get("kind") == "experiment"
                   for stage in self.workflow.get("stages", [])):
            return None
        topic_stage = next(
            (stage for stage in self.workflow.get("stages", [])
             if stage.get("kind") == "topic_discovery"), None)
        if not isinstance(topic_stage, dict):
            return None
        record = self.stage_records.get(topic_stage.get("id"), {})
        if not isinstance(record, dict) or record.get("status") not in STAGE_READY_STATUSES:
            return None
        topic_context = self.context.get(topic_stage.get("id"))
        if not isinstance(topic_context, dict):
            return (topic_stage["id"],
                    "completed topic output is unavailable for current feasibility revalidation")
        try:
            from scisaurus.runtime.topic_discovery import (
                _materialize_foundry_capability_requirements,
                validate_topic_feasibility, validate_topic_stage_config,
            )
            descriptor = validate_topic_stage_config(
                json.loads(Path(topic_stage["config_path"]).read_text()))
            model = json.loads(Path(descriptor["model_config_path"]).read_text())
            runtime_context = self._runtime_context(model)
            package = _materialize_foundry_capability_requirements(
                deepcopy(topic_context), runtime_context)
            feasibility = validate_topic_feasibility(package, runtime_context)
            if feasibility.get("status") == "legacy_unchecked":
                return (topic_stage["id"],
                        "completed topic output has no current capability admission record")
        except (KeyError, OSError, StopIteration, TypeError, ValueError, ValidationError) as exc:
            return topic_stage["id"], str(exc)
        return None

    def _queue_topic_feasibility_revalidation(self, stage_id, reason):
        """Attach a typed topic repair order to an incompatible checkpoint."""
        context = self.context.get(stage_id)
        if not isinstance(context, dict):
            context = {"kind": "topic_discovery", "status": "invalidated"}
        requests = context.get("research_requests")
        if not isinstance(requests, list):
            requests = []
        request_id = "topic-runtime-feasibility-revalidation"
        if not any(isinstance(item, dict) and item.get("id") == request_id
                   for item in requests):
            requests.append({
                "id": request_id,
                "kind": "topic_refinement",
                "owner": "research.intelligence",
                "objective": "Replan the selected direction against the current executable and data boundary before literature admission.",
                "why": "The checkpoint predates the machine-readable feasibility admission contract.",
                "success_condition": "A candidate carries a valid feasibility plan that passes the current runtime boundary and can be admitted to the survey.",
                "evidence_needed": "Current runtime inventory, experiment boundary, declared inputs, dependency availability, provider work, model work, and compute limit.",
            })
        context["research_requests"] = requests
        context["runtime_feasibility_revalidation"] = {
            "status": "required", "reason": str(reason)[:2048],
        }
        self.context[stage_id] = context

    def _reconcile_interrupted_attempt(self, attempt_id):
        """Close an attempt left running before a process interruption.

        The stage record is only a summary.  The task ledger is authoritative
        about whether an external call is still started, already unknown, or
        has a settled outcome, so resumption reconciles that ledger before a
        fresh attempt is admitted.
        """
        try:
            attempt = self.tasks.get_attempt(attempt_id)
        except NotFoundError:
            # Older checkpoints may predate task-attempt persistence.
            return
        state = attempt.get("state")
        if state == "started":
            self.tasks.reconcile_unknown(attempt_id, "command.composer")
            return
        if state in {"result_unknown", "failed", "succeeded", "cancelled"}:
            return
        raise StateError(f"interrupted Composer attempt has unsupported state: {state}")

    def _source_value(self, expression):
        parts = expression.split(".")
        if len(parts) < 2 or parts[0] not in self.context:
            raise ValidationError(f"binding source must name a completed stage: {expression}")
        context = self.context[parts[0]]
        path = ".".join(parts[1:])
        # A provisional stage keeps its forward-progress report as the public
        # output while exposing the last successful immutable artifact for a
        # downstream packet that needs a typed file path.  The debt and status
        # remain in the context, so this is a handoff adapter, not an approval.
        if path == "output_path" and isinstance(context, dict):
            binding_path = context.get("binding_output_path")
            if isinstance(binding_path, str) and binding_path:
                return binding_path
        return _get_path(context, path)

    @staticmethod
    def _walk_provider_descriptors(value):
        """Yield provider configuration fragments without exposing secrets."""
        if isinstance(value, dict):
            if isinstance(value.get("auth_env"), str) and value["auth_env"].strip():
                yield value
            for item in value.values():
                yield from ComposerRunner._walk_provider_descriptors(item)
        elif isinstance(value, list):
            for item in value:
                yield from ComposerRunner._walk_provider_descriptors(item)

    def _stage_provider_configuration_error(self, stage):
        """Preflight required credentials before specialists or models start."""
        try:
            descriptor = json.loads(Path(stage["config_path"]).read_text())
        except (OSError, ValueError, TypeError) as exc:
            return ProviderConfigurationError(
                f"stage {stage.get('id')} provider descriptor is unreadable: {exc}")
        for fragment in self._walk_provider_descriptors(descriptor):
            env_name = fragment["auth_env"]
            value = os.environ.get(env_name)
            if isinstance(value, str) and value and all(33 <= ord(char) <= 126 for char in value):
                continue
            endpoint = str(fragment.get("endpoint") or fragment.get("base_url") or "")
            provider = "openalex" if "openalex" in endpoint.casefold() else "model"
            return ProviderConfigurationError(
                f"stage {stage.get('id')} requires configured {provider} credential "
                f"{env_name}; load the owner-local runtime environment before dispatch",
                provider=provider, credential_env=env_name,
            )
        return None

    def _survey_provider_cooldown(self, stage, descriptor):
        """Read the shared OpenAlex fence before activating survey specialists.

        Survey specialists are useful only when the survey can admit a
        bibliographic route. Previously they were dispatched first, and the
        SurveyRunner discovered the same persistent OpenAlex fence afterward;
        every retry therefore spent specialist calls without adding evidence.
        This check is local and does not make an HTTP request.
        """
        if not isinstance(stage, dict) or stage.get("kind") != "survey":
            return None
        stage_context = self.context.get(stage.get("id"), {})
        if (isinstance(stage_context, dict)
                and isinstance(stage_context.get("provider_fallback"), dict)
                and stage_context["provider_fallback"].get("mode") == "crossref_metadata"):
            return None
        survey = descriptor.get("survey") if isinstance(descriptor, dict) else None
        bibliography = survey.get("bibliography") if isinstance(survey, dict) else None
        if (not isinstance(bibliography, dict)
                or bibliography.get("adapter") != "openalex"):
            return None
        client = bibliography.get("client")
        if not isinstance(client, dict):
            return None
        fields = {
            "timeout", "max_bytes", "endpoint", "auth_env", "max_retries",
            "retry_backoff_seconds", "min_interval_seconds", "rate_state_path",
            "allow_anonymous_fallback",
        }
        client_config = {key: deepcopy(value) for key, value in client.items()
                         if key in fields}
        preflight_client = OpenAlexClient(**client_config)
        representative = bibliography.get("representative")
        representative = representative if isinstance(representative, dict) else {}
        cooldown = preflight_client.preflight(
            operation=representative.get("operation", "search"),
            query=representative.get("query"),
            work_id=representative.get("work_id"),
            limit=representative.get("limit", 1),
            cursor=representative.get("cursor"),
        )
        if not isinstance(cooldown, dict):
            return None
        rate_limit = deepcopy(cooldown.get("rate_limit") or {})
        delay = cooldown.get("retry_after_seconds")
        if type(delay) not in (int, float) or not math.isfinite(delay) or delay <= 0:
            return None
        return ProviderCooldownError(
            "OpenAlex survey retrieval is paused before specialist admission "
            "because the shared provider budget is exhausted",
            retry_after_seconds=delay,
            rate_limit=rate_limit,
        )

    def _stage_dependency_gaps(self, stage):
        """Return missing bound values before a downstream worker is admitted."""
        gaps = []
        for binding in stage.get("bindings", []):
            source = binding.get("source")
            target = binding.get("target")
            try:
                value = self._source_value(source)
            except ValidationError as exc:
                gaps.append({"source": source, "target": target, "reason": str(exc)})
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                gaps.append({
                    "source": source, "target": target,
                    "reason": "bound value is null or empty",
                })
                continue
            if (isinstance(value, str) and target.endswith("results_package")
                    and not Path(value).is_file()):
                gaps.append({
                    "source": source, "target": target,
                    "reason": f"bound artifact is unavailable: {value}",
                })
        return gaps

    def _admit_upstream_dependency_recovery(self, stage, gaps, completed, by_id):
        """Route an unresolved provisional handoff back to its owning stage.

        A forward-first candidate may be visible to the agenda while still
        lacking the artifact needed by its consumer.  That is a scientific
        repair condition, not a generic wiring error: the Composer must reopen
        the upstream scope (with a changed work order) before it considers the
        consumer.  Returning the upstream stage id lets the caller distinguish
        an explicit scientific hold from an actually malformed workflow.
        """
        if not self._forward_first() or not isinstance(stage, dict):
            return None
        for gap in gaps if isinstance(gaps, list) else []:
            source = gap.get("source") if isinstance(gap, dict) else None
            if not isinstance(source, str) or "." not in source:
                continue
            upstream_id = source.split(".", 1)[0]
            upstream = by_id.get(upstream_id)
            context = self.context.get(upstream_id)
            if (not isinstance(upstream, dict)
                    or upstream.get("kind") != "survey"
                    or not isinstance(context, dict)):
                continue
            if context.get("assessment_current") is True and context.get("assessment_ref"):
                continue
            context = deepcopy(context)
            context["status"] = "research_expansion_required"
            context["review_status"] = "survey_handoff_incomplete"
            context["error"] = (
                "survey produced a provisional packet without a current gap assessment; "
                "the downstream experiment must wait for the scoped literature repair"
            )
            context["handoff_repair_required"] = True
            requests = context.get("research_requests")
            if not isinstance(requests, list):
                requests = []
            if not any(isinstance(item, dict) for item in requests):
                deferred = context.get("deferred_research_requests")
                if isinstance(deferred, list):
                    requests.extend(deepcopy(item) for item in deferred
                                    if isinstance(item, dict))
            if not any(isinstance(item, dict) for item in requests):
                request = self._autonomous_recovery_request(upstream_id, context)
                if isinstance(request, dict):
                    requests.append(request)
            context["research_requests"] = requests
            self.context[upstream_id] = context
            admitted = self._begin_continuation(completed, by_id)
            if admitted:
                self.department_activity.append({
                    "cycle": self.continuation_cycles,
                    "action": "recover_upstream_dependency",
                    "upstream_stage_id": upstream_id,
                    "consumer_stage_id": stage.get("id"),
                    "reason": "provisional survey lacks a current assessment_ref",
                })
            return upstream_id, admitted
        return None

    def _stage_usage(self, stage_id):
        """Aggregate the active quota scope for one stage.

        Ordinary stages have one aggregate envelope. A continuation is an
        explicitly admitted new scientific work order with its own bounded
        stage budget. Counting every historical attempt against the newest
        repair cycle makes a valid recovery stop before it can execute the
        scoped work. Keep the mission-wide usage ledger unchanged while
        scoping every reopened stage fence to the current continuation cycle.
        """
        record = self.stage_records.get(stage_id, {})
        attempts = record.get("attempts", []) if isinstance(record, dict) else []
        stage_kind = next(
            (item.get("kind") for item in self.workflow.get("stages", [])
             if isinstance(item, dict) and item.get("id") == stage_id),
            None,
        )
        active_cycle = self.continuation_cycles
        if stage_id in self.reopened_stage_ids and active_cycle:
            attempts = [
                item for item in attempts
                if isinstance(item, dict) and item.get("cycle") == active_cycle
            ]
        elif stage_kind == "topic_discovery":
            # Initial topic intake has no continuation cycle.  Preserve the
            # legacy checkpoint convention where the cycle field may be
            # omitted, while excluding any later pivot history.
            attempts = [
                item for item in attempts
                if isinstance(item, dict) and item.get("cycle", 0) in (None, 0)
            ]
        totals = {"model_calls": 0, "input_tokens": 0,
                  "output_tokens": 0, "openalex_requests": 0}
        observed = False
        for attempt in attempts if isinstance(attempts, list) else []:
            usage = attempt.get("usage") if isinstance(attempt, dict) else None
            if not isinstance(usage, dict):
                continue
            observed = True
            for key in totals:
                value = usage.get(key, 0)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    totals[key] += value
        # A reopened stage has an isolated quota envelope for the admitted
        # continuation cycle.  Its top-level ``usage`` is the previous
        # attempt's snapshot; using it as a fallback before the new cycle has
        # an attempt recreates the exhausted quota and can spin continuations
        # without ever dispatching work.
        isolated_reopened_cycle = stage_id in self.reopened_stage_ids and active_cycle
        if (not observed and not isolated_reopened_cycle
                and isinstance(record, dict) and isinstance(record.get("usage"), dict)):
            for key in totals:
                value = record["usage"].get(key, 0)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    totals[key] += value
        return totals

    def _stage_quota_error(self, stage):
        """Check an aggregate stage quota before admitting another attempt."""
        quota = stage.get("quota") if isinstance(stage, dict) else None
        if not isinstance(quota, dict):
            return None
        usage = self._stage_usage(stage["id"])
        dimensions = {
            "max_model_calls": ("model_calls", quota["max_model_calls"]),
            "max_input_tokens": ("input_tokens", quota["max_input_tokens"]),
            "max_output_tokens": ("output_tokens", quota["max_output_tokens"]),
            "max_openalex_requests": ("openalex_requests", quota["max_openalex_requests"]),
        }
        for dimension, (usage_key, limit) in dimensions.items():
            if usage[usage_key] > limit:
                error = QuotaExceededError(
                    f"stage {stage['id']} quota exhausted: {usage_key}={usage[usage_key]} > {limit}",
                    dimension=dimension, limit=limit, observed=usage[usage_key], usage=usage,
                )
                error.usage_is_snapshot = True
                return error
        return None

    def _admit_stage_quota_recovery(self, stage, quota_error, completed, by_id):
        """Turn a local stage-budget exhaustion into changed scoped work.

        A stage quota is an allocation boundary for one stage attempt, not a
        scientific conclusion and not a reason to park an otherwise live
        mission.  The old path converted this condition directly to
        ``paused``; the outer supervisor then deliberately refused to resume
        it, leaving a scheduled retry with no process to consume it.  Reopen
        the affected closure with a typed recovery marker so the next
        attempt can narrow its work rather than replaying the same packet.

        Provider/global quota errors do not enter this helper.  They remain
        resource fences because dispatching another call would be unsafe.
        """
        if not isinstance(stage, dict) or stage.get("kind") == "topic_discovery":
            # Topic intake has its own candidate-envelope budget and pivot
            # semantics.  Do not confuse that with a reusable downstream
            # stage allocation.
            return False
        if not isinstance(quota_error, QuotaExceededError):
            return False
        if self._remaining() <= self._deadline_dispatch_floor():
            return False
        stage_id = stage["id"]
        prior = self.context.get(stage_id)
        context = deepcopy(prior) if isinstance(prior, dict) else {
            "stage_id": stage_id, "kind": stage.get("kind"),
        }
        previous_cycle = self.continuation_cycles
        existing = context.get("quota_recovery")
        if (isinstance(existing, dict)
                and existing.get("previous_cycle") == previous_cycle):
            return False
        prior_recoveries = 0
        if isinstance(existing, dict) and type(existing.get("recovery_count")) is int:
            prior_recoveries = max(0, existing["recovery_count"])
        recovery_count = prior_recoveries + 1
        if (stage.get("kind") == "survey" and prior_recoveries >= 1):
            # One narrowed allocation is a useful repair. A second exhausted
            # allocation without a current gap assessment is evidence that
            # the direction, not the worker, is the bottleneck. Return to the
            # topic frontier instead of charging another identical survey
            # cycle against the mission.
            context.update({
                "stage_id": stage_id,
                "kind": stage.get("kind"),
                "status": "research_expansion_required",
                "error": str(quota_error)[:4096],
                "review_status": "survey_quota_recovery_exhausted",
                "quota_recovery": {
                    "status": "pivot_required",
                    "mode": "topic_pivot",
                    "previous_cycle": previous_cycle,
                    "recovery_count": recovery_count,
                    "dimension": getattr(quota_error, "dimension", None),
                    "limit": getattr(quota_error, "limit", None),
                    "observed": getattr(quota_error, "observed", None),
                    "reason": (
                        "two bounded survey allocations were consumed without a current gap decision"
                    ),
                },
                "research_expansion_requests": [],
                "research_requests": [],
            })
            self.context[stage_id] = context
            pivoted = self._pivot_topic_after_scientific_blocker(
                stage, context, completed, by_id,
                reason=(
                    "the survey exhausted a full allocation and one narrowed recovery allocation "
                    "without producing a current gap decision"
                ),
                objective=(
                    "Generate a materially different, source-grounded computational question whose "
                    "literature gate has a bounded decisive evidence target; do not reuse the exhausted "
                    "survey packet or its unresolved terminology."
                ),
                why=(
                    "Repeating the same literature map consumed another bounded allocation without "
                    "adding a current gap assessment, so the research direction must change."
                ),
            )
            if pivoted:
                self.department_activity.append({
                    "cycle": self.continuation_cycles,
                    "action": "pivot_topic_after_repeated_survey_quota",
                    "stage_id": stage_id,
                    "recovery_count": recovery_count,
                    "reason": "same survey exhausted two bounded allocations",
                })
            return pivoted
        context.update({
            "stage_id": stage_id,
            "kind": stage.get("kind"),
            "status": "research_expansion_required",
            "error": str(quota_error)[:4096],
            "review_status": "stage_quota_exhausted",
            "quota_recovery": {
                "status": "required",
                "mode": "narrow_scope",
                "previous_cycle": previous_cycle,
                "recovery_count": recovery_count,
                "dimension": getattr(quota_error, "dimension", None),
                "limit": getattr(quota_error, "limit", None),
                "observed": getattr(quota_error, "observed", None),
                "reason": "the local stage allocation was consumed before the decisive gate completed",
            },
            "research_expansion_requests": [],
            "research_requests": [],
        })
        self.context[stage_id] = context
        if not self._begin_continuation(completed, by_id):
            return False
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "stage_quota_recovery_admitted",
            "stage_id": stage_id,
            "previous_cycle": previous_cycle,
            "dimension": getattr(quota_error, "dimension", None),
            "limit": getattr(quota_error, "limit", None),
            "observed": getattr(quota_error, "observed", None),
            "repair_mode": "narrow_scope",
            "next_condition": (
                "resume from accepted checkpoints with a smaller decisive workload; "
                "do not replay the exhausted packet"
            ),
        })
        return True

    def _resume_stage_quota_recovery(self, completed, by_id):
        """Consume a persisted local-quota stop before scheduling stages."""
        for blocker in reversed(self.blockers):
            if not isinstance(blocker, dict):
                continue
            if blocker.get("stop_reason") != "stage_quota_exhausted":
                continue
            if blocker.get("recovery") == "cycle_admitted":
                continue
            stage_id = blocker.get("stage_id")
            stage = by_id.get(stage_id)
            if not isinstance(stage, dict):
                continue
            error = QuotaExceededError(
                blocker.get("reason") or f"stage {stage_id} quota exhausted",
                dimension=blocker.get("dimension"),
                limit=blocker.get("limit"),
                observed=blocker.get("observed"),
                usage=blocker.get("usage", {}),
            )
            error.usage_is_snapshot = True
            if self._admit_stage_quota_recovery(stage, error, completed, by_id):
                blocker["recovery"] = "cycle_admitted"
                record = self.stage_records.get(stage_id)
                if isinstance(record, dict):
                    record["status"] = "retrying"
                    record["recovery_admitted"] = True
                return True
        return False

    @staticmethod
    def _is_openalex_provider_cooldown(value):
        """Recognize an OpenAlex quota fence without matching model failures."""
        if isinstance(value, dict):
            try:
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                value = str(value)
        text = str(value or "").casefold()
        return (
            "openalex" in text
            and any(token in text for token in ("quota", "rate", "cooldown", "paused", "reset"))
        )

    @staticmethod
    def _survey_has_crossref_identity(stage):
        """Check the immutable survey descriptor for a safe metadata fallback."""
        if not isinstance(stage, dict) or stage.get("kind") != "survey":
            return False
        try:
            descriptor = json.loads(Path(stage["config_path"]).read_text())
        except (OSError, TypeError, ValueError, KeyError):
            return False
        survey = descriptor.get("survey") if isinstance(descriptor, dict) else None
        identity = survey.get("identity") if isinstance(survey, dict) else None
        return (
            isinstance(survey, dict)
            and survey.get("bibliography_fallback", "crossref_metadata") == "disabled"
            and isinstance(identity, dict)
            and identity.get("adapter") == "crossref"
        )

    def _admit_survey_provider_fallback(self, stage, error=None):
        """Open a bounded Crossref continuation for an exhausted OpenAlex quota."""
        if not self._survey_has_crossref_identity(stage):
            return False
        stage_id = stage["id"]
        record = self.stage_records.get(stage_id, {})
        schedule = self.retry_schedule.get(stage_id, {})
        candidates = [error, record.get("error"), schedule.get("error")]
        candidates.extend(
            item for item in reversed(self.blockers)
            if isinstance(item, dict) and item.get("stage_id") == stage_id
        )
        if not any(self._is_openalex_provider_cooldown(item) for item in candidates):
            return False
        context = deepcopy(self.context.get(stage_id, {}))
        previous = context.get("provider_fallback")
        if (isinstance(previous, dict)
                and previous.get("mode") == "crossref_metadata"):
            # The next attempt must prove that the admitted route actually
            # changed the runner input.  Re-admitting the same fallback on a
            # second OpenAlex error would turn a miswired recovery into a hot
            # retry loop instead of exposing the failed route transition.
            return False
        context["provider_fallback"] = {
            "status": "admitted",
            "mode": "crossref_metadata",
            "source_provider": "openalex",
            "reason": "OpenAlex daily quota/cooldown would otherwise park the survey until reset",
            "cycle": self.continuation_cycles,
            "preserve_openalex_evidence": True,
        }
        self.context[stage_id] = context
        self.retry_schedule.pop(stage_id, None)
        record = deepcopy(record) if isinstance(record, dict) else {"kind": "survey"}
        record["status"] = "retrying"
        record["provider_fallback"] = "crossref_metadata"
        self.stage_records[stage_id] = record
        if not (isinstance(previous, dict) and previous.get("mode") == "crossref_metadata"):
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "provider_fallback_admitted",
                "stage_id": stage_id,
                "from": "openalex",
                "to": "crossref_metadata",
                "scope": "bounded survey recovery; OpenAlex captures remain authoritative when available",
                "next_condition": "complete the narrow metadata survey and retain degraded evidence gaps",
            })
        return True

    def _resume_survey_provider_fallback(self, by_id):
        """Reopen a paused survey immediately when its cooldown has a safe fallback."""
        for stage in self.workflow.get("stages", []):
            if stage.get("kind") != "survey":
                continue
            if self._admit_survey_provider_fallback(stage):
                return True
        return False

    def _invalidate_legacy_provider_retry_schedules(self, by_id):
        """Re-evaluate route-agnostic specialist cooldowns after a resume.

        Older Composer checkpoints encoded a generic ``specialist provider is
        cooling down`` error as a stage retry with a potentially day-long
        ``not_before_epoch``.  That representation predates route-scoped
        cooldowns and can preserve a stale pool-wide fence even when another
        model route is available.  Remove only that unscoped specialist
        schedule; the next attempt will load the current route cooldown
        records and either select a healthy route or create a fresh, bounded
        schedule from the current provider state.
        """
        invalidated = []
        for stage_id, schedule in list(self.retry_schedule.items()):
            if not isinstance(schedule, dict):
                continue
            error_text = str(schedule.get("error") or "").casefold()
            if "specialist provider is cooling down" not in error_text:
                continue
            if any(isinstance(schedule.get(key), str) and schedule[key].strip()
                   for key in ("route_id", "cooldown_key")):
                continue
            stage = by_id.get(stage_id)
            if not isinstance(stage, dict) or stage.get("kind") == "survey":
                # Survey/OpenAlex cooldowns have a separate fallback path and
                # must not be confused with specialist model routing.
                continue
            self.retry_schedule.pop(stage_id, None)
            record = self.stage_records.get(stage_id)
            if isinstance(record, dict) and record.get("status") == "paused":
                record["status"] = "retrying"
                record["recovery_admitted"] = True
                record["legacy_retry_schedule_invalidated"] = True
            invalidated.append({
                "stage_id": stage_id,
                "failed_attempt_number": schedule.get("failed_attempt_number"),
                "next_attempt_number": schedule.get("next_attempt_number"),
                "stale_delay_seconds": schedule.get("delay_seconds"),
                "reason": "legacy route-agnostic specialist cooldown schedule",
            })
        if invalidated:
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "invalidate_legacy_provider_retry_schedules",
                "schedules": invalidated,
                "next_condition": (
                    "re-dispatch once through the current route-scoped cooldown map; "
                    "do not sleep on a legacy pool-wide fence"
                ),
            })
        return invalidated

    @staticmethod
    def _apply_stage_quota(config, stage):
        """Pass the declared model-call ceiling into ExecutionRuntime."""
        quota = stage.get("quota") if isinstance(stage, dict) else None
        if not isinstance(quota, dict) or not isinstance(config, dict) or "limits" not in config:
            return config
        limits = config.setdefault("limits", {})
        current = limits.get("max_model_calls")
        limits["max_model_calls"] = (
            min(current, quota["max_model_calls"])
            if type(current) is int else quota["max_model_calls"]
        )
        return config

    @staticmethod
    def _stage_releases_dependencies(record, *, stage_kind=None):
        """Return whether a stage is safe to expose to dependent stages."""
        if not isinstance(record, dict):
            return False
        status = record.get("status")
        if status in {"completed", "accepted"}:
            return True
        if status != "candidate_needs_review":
            return False
        # A paper candidate is never a dependency release.  Exploratory
        # argument work may be useful before all scientific debt is cleared,
        # but publication must remain behind an explicit evidence closure.
        # This is deliberately independent of ``composer_decision`` so a
        # forward-first handoff cannot turn a draft into a release input.
        if stage_kind == "paper":
            return False
        # In the autonomous-lab policy, this is an explicit Composer handoff,
        # not a release approval by the deterministic harness.  The candidate
        # remains visibly provisional and repairable, but it is dependency-
        # releasable once the Composer has recorded this decision.
        if record.get("composer_decision") == "advance_with_findings":
            return True
        if record.get("release_blocking") is True:
            return False
        debt = record.get("failure_debt")
        return not (isinstance(debt, dict) and debt.get("release_blocking") is True)

    def _paper_release_blockers(self, paper_stage, by_id):
        """Return unresolved scientific ancestors that fence paper release.

        The scheduler may let an adaptive Composer inspect provisional work in
        ``argument``.  A paper is a different boundary: every ancestor must
        have an accepted/completed scientific packet with no active verifier,
        evidence, or backfill debt.  Keep this check on the Composer side so
        it applies equally to a fresh run and a restored checkpoint.
        """
        if not isinstance(paper_stage, dict) or paper_stage.get("kind") != "paper":
            return []
        pending = list(paper_stage.get("depends_on", []))
        ancestor_ids = set()
        while pending:
            stage_id = pending.pop()
            if stage_id in ancestor_ids or stage_id not in by_id:
                continue
            ancestor_ids.add(stage_id)
            pending.extend(by_id[stage_id].get("depends_on", []))

        blockers = []
        workflow_order = [
            stage.get("id") for stage in self.workflow.get("stages", [])
            if isinstance(stage, dict) and stage.get("id") in ancestor_ids
        ]
        survey_ancestors = [
            stage_id for stage_id in workflow_order
            if by_id[stage_id].get("kind") == "survey"
        ]
        for stage_id in workflow_order:
            stage = by_id[stage_id]
            record = self.stage_records.get(stage_id, {})
            context = self.context.get(stage_id, {})
            if not isinstance(record, dict):
                record = {}
            if not isinstance(context, dict):
                context = {}
            record_status = record.get("status")
            context_status = context.get("status")
            specialist_verifier = context.get("specialist_verifier")
            if not isinstance(specialist_verifier, dict):
                specialist_verifier = {}
            reasons = []

            if record_status in STAGE_HOLD_STATUSES or context_status in STAGE_HOLD_STATUSES:
                reasons.append("scientific_hold")
            if (record_status == "candidate_needs_review"
                    or context_status == "candidate_needs_review"):
                reasons.append("provisional_candidate")
            if (record.get("verifier_outcome") == "hold"
                    or context.get("verifier_outcome") == "hold"
                    or specialist_verifier.get("verdict") == "hold"
                    or specialist_verifier.get("decision") == "hold"):
                reasons.append("verifier_hold")
            if (record.get("release_blocking") is True
                    or context.get("release_blocking") is True):
                reasons.append("release_blocking")
            debt = record.get("failure_debt")
            context_debt = context.get("failure_debt")
            if (isinstance(debt, dict) and debt.get("release_blocking") is True
                    or isinstance(context_debt, dict)
                    and context_debt.get("release_blocking") is True):
                reasons.append("failure_debt")
            if (record.get("backfill_required") is True
                    or context.get("backfill_required") is True
                    or record.get("progression_state") in {"deferred", "advanced_with_findings"}
                    or context.get("progression_state") in {"deferred", "advanced_with_findings"}):
                reasons.append("backfill_required")

            kind = stage.get("kind")
            if kind == "topic_discovery" and (
                    context.get("admission_state") == "provisional_for_survey"
                    or record.get("admission_state") == "provisional_for_survey"):
                # A topic is intentionally provisional at the topic -> survey
                # boundary.  Once an ancestor survey has produced a current,
                # experiment-eligible assessment, that provisional label is
                # resolved for the paper branch; otherwise the survey (or the
                # missing survey) remains the repair target.
                # If a survey ancestor exists but has not resolved the topic,
                # its own evidence gate is the repair target.  Do not pivot
                # the topic merely because the first survey pass is thin.
                if not survey_ancestors:
                    reasons.append("provisional_topic")
            if kind == "survey":
                effective_status = context_status or record_status
                if context.get("topic_admission") in {
                        "exploratory_pilot", "expand_literature_before_refine",
                        "refine_before_experiment"}:
                    reasons.append("survey_not_experiment_eligible")
                if context.get("gap_state") in {"insufficient_evidence", "refuted_by_prior_work"}:
                    reasons.append("survey_gap_unresolved")
                if context.get("survey_current") is False or context.get("assessment_current") is False:
                    reasons.append("survey_assessment_not_current")
                if (effective_status in {"completed", "accepted"}
                        and "gap_state" in context
                        and context.get("gap_state") != "eligible_for_experiment"):
                    reasons.append("survey_not_experiment_eligible")
                if (effective_status in {"completed", "accepted"}
                        and ("survey_current" in context or "assessment_current" in context)
                        and (context.get("survey_current") is not True
                             or context.get("assessment_current") is not True)):
                    reasons.append("survey_assessment_not_current")
            if kind == "experiment" and context.get("results_status") == "not_executed":
                reasons.append("experiment_not_executed")

            if reasons:
                blockers.append({
                    "stage_id": stage_id,
                    "kind": kind,
                    "status": context_status or record_status or "pending",
                    "reasons": list(dict.fromkeys(reasons)),
                })
        return blockers

    def _ensure_paper_gate_repair_requests(self, blockers):
        """Create one scoped repair order without discarding the current branch."""
        repairs = []
        for blocker in blockers[:1]:
            stage_id = blocker.get("stage_id") if isinstance(blocker, dict) else None
            if not isinstance(stage_id, str):
                continue
            context = self.context.get(stage_id, {})
            if not isinstance(context, dict):
                context = {}
            existing = []
            for key in ("research_expansion_requests", "research_requests",
                        "deferred_research_requests"):
                value = context.get(key)
                if isinstance(value, list):
                    existing.extend(item for item in value if isinstance(item, dict))
            unattempted = [
                item for item in existing
                if self._research_request_signature(item)
                not in self._attempted_request_signatures
            ]
            if unattempted:
                repairs.extend(deepcopy(unattempted))
                continue

            # Candidate packets do not always emit a typed work order.  Reuse
            # the stage's deterministic recovery strategy, but present the
            # candidate as a hold to that helper so a repair is generated.
            repair_context = deepcopy(context)
            repair_context["status"] = "research_expansion_required"
            request = self._autonomous_recovery_request(stage_id, repair_context)
            if not isinstance(request, dict):
                continue
            request["repair_policy"] = "preserve_branch_before_rejection"
            request["preserve_artifacts"] = True
            request["source_blocker"] = {
                "stage_id": stage_id,
                "reasons": deepcopy(blocker.get("reasons", [])),
            }
            context.setdefault("research_requests", []).append(request)
            context["repair_policy"] = "preserve_branch_before_rejection"
            context["repair_target_stage_id"] = stage_id
            self.context[stage_id] = context
            repairs.append(deepcopy(request))
        return repairs

    def _refresh_paper_release_gate(self, completed, by_id, release_blocked_stage_ids):
        """Fence paper dispatch until every scientific ancestor is releasable."""
        changed = False
        self._paper_gate_repair_requests = []
        for paper_stage in self.workflow.get("stages", []):
            if paper_stage.get("kind") != "paper":
                continue
            paper_id = paper_stage["id"]
            # A paper only becomes schedulable after its direct dependencies
            # are complete.  Once that frontier is reached, evaluate the full
            # transitive scientific closure rather than only the last stage.
            if not set(paper_stage.get("depends_on", [])).issubset(completed):
                continue
            blockers = self._paper_release_blockers(paper_stage, by_id)
            record = deepcopy(self.stage_records.get(paper_id, {}))
            gate = record.get("release_gate") if isinstance(record, dict) else None
            gate_is_ours = isinstance(gate, dict) and gate.get("kind") == "upstream_scientific_hold"
            if blockers:
                repairs = self._ensure_paper_gate_repair_requests(blockers)
                self._paper_gate_repair_requests.extend(repairs)
                completed.discard(paper_id)
                release_blocked_stage_ids.add(paper_id)
                next_gate = {
                    "kind": "upstream_scientific_hold",
                    "stage_id": paper_id,
                    "release_blocking": True,
                    "blockers": blockers,
                    "repair_requests": repairs,
                }
                if (record.get("status") != "candidate_needs_review"
                        or record.get("release_blocking") is not True
                        or gate != next_gate):
                    record.update({
                        "kind": "paper",
                        "status": "candidate_needs_review",
                        "release_blocking": True,
                        "release_gate": next_gate,
                        "error": "paper release blocked by unresolved upstream scientific evidence",
                    })
                    self.stage_records[paper_id] = record
                    self.blockers.append({
                        "stage_id": paper_id,
                        "reason": "paper release blocked by unresolved upstream scientific evidence",
                        "stop_reason": "upstream_scientific_hold",
                        "gating": True,
                        "release_blocking": True,
                        "dependencies": blockers,
                        "repair_requests": repairs,
                    })
                    self.department_activity.append({
                        "cycle": self.continuation_cycles,
                        "action": "paper_release_gate",
                        "stage_id": paper_id,
                        "blockers": blockers,
                        "repair_requests": repairs,
                        "next_action": "resolve upstream scientific debt before dispatching paper",
                    })
                    changed = True
                continue

            # Remove only a gate created by this method.  A real paper attempt
            # or a separately produced editorial candidate must remain intact.
            if (gate_is_ours and record.get("attempt_count", 0) == 0
                    and not record.get("attempts") and not record.get("output_path")):
                for key in ("release_gate", "error", "release_blocking"):
                    record.pop(key, None)
                record["status"] = "pending"
                self.stage_records[paper_id] = record
                release_blocked_stage_ids.discard(paper_id)
                changed = True
        return changed

    def _can_migrate_forward_candidate(self, record):
        """Identify an old forward-first candidate eligible for handoff."""
        if not isinstance(record, dict):
            return False
        if not self._forward_first():
            return False
        if record.get("status") != "candidate_needs_review":
            return False
        if record.get("forward_progress") is not True:
            return False
        failure_class = None
        debt = record.get("failure_debt")
        if isinstance(debt, dict):
            failure_class = debt.get("failure_class")
        if failure_class in {"resource_fence", "unknown_external_outcome",
                             "process_interruption"}:
            return False
        text = " ".join(str(record.get(key) or "") for key in ("error", "reason"))
        lowered = text.casefold()
        return not any(token in lowered for token in (
            "provider cooldown", "quota", "deadline", "result_unknown",
            "unknown external outcome", "authentication",
        ))

    def _release_blocked_format_retry_allowed(self, stage, record):
        """Allow one resume to consume a cached provider-format repair.

        A legacy or strict-policy candidate checkpoint may be release-blocking.  A bounded
        serialization failure is different: the scientific work may already
        be durable and the stage runner can resume its exact input-addressed
        cache without rediscovering the literature.  Permit only the known
        format/length classes and only while the workflow retry envelope has
        one attempt left; all scientific holds and environmental failures
        remain release-blocking.
        """
        if not isinstance(stage, dict) or not isinstance(record, dict):
            return False
        if record.get("status") != "candidate_needs_review" or record.get("release_blocking") is not True:
            return False
        attempts = record.get("attempt_count")
        max_attempts = self._retry_policy().get("max_attempts")
        if type(attempts) is not int or type(max_attempts) is not int:
            return False
        debt = record.get("failure_debt") if isinstance(record.get("failure_debt"), dict) else {}
        error = " ".join(str(value) for value in (record.get("error"), debt.get("error")) if value)
        lowered = error.casefold()
        aggregate_fallback = (
            stage.get("kind") == "survey"
            and (
                ("survey-review" in lowered and any(marker in lowered for marker in (
                    "valid json", "finish normally: length",
                )))
                or "review requires its reviewer's recorded model execution" in lowered
                or "deterministic survey review" in lowered
                or "attributeerror: 'bytes' object has no attribute 'get'" in lowered
            )
        )
        if attempts >= max_attempts and not aggregate_fallback:
            return False
        return any(marker in lowered for marker in (
            "model output must contain valid json",
            "model generation did not finish normally: length",
            "model response did not contain valid json",
            "review requires its reviewer's recorded model execution",
            "deterministic survey review",
            "attributeerror: 'bytes' object has no attribute 'get'",
        ))

    @staticmethod
    def _release_blocked_topic_contract_retry_allowed(stage, record):
        """Allow one resumed topic intake after a deterministic contract fix.

        A topic package that never crossed intake has no scientific artifact to
        release or reject.  When the durable debt identifies the known
        feasibility-input contract and the current runtime repairs that
        redundant label losslessly, the next resume may spend one fresh topic
        attempt.  This is intentionally narrower than a generic validation
        retry: external inputs are still rejected by the normal gate.
        """
        if not isinstance(stage, dict) or stage.get("kind") != "topic_discovery":
            return False
        if not isinstance(record, dict) or record.get("status") != "candidate_needs_review":
            return False
        if record.get("contract_recovery_admitted") is True:
            return False
        debt = record.get("failure_debt")
        if not isinstance(debt, dict) or debt.get("failure_class") != "mechanical_contract":
            return False
        error = " ".join(str(value) for value in (
            record.get("error"), debt.get("error"),
        ) if value).casefold()
        return "feasibility_plan.project_artifact must declare a project_artifact input" in error

    def _synchronize_paper_references(self, paper_config):
        """Project newly accepted survey sources into a continuation paper descriptor.

        A paper descriptor may have been prepared before the survey expanded.
        Keeping its original references is correct for an immutable first
        attempt, but it would make a continuation gate inspect stale coverage.
        This helper appends one reader-facing reference per newly captured
        source, preferring full text, while retaining every original key and
        evidence locator.  It never invents a DOI or authorship record.
        """
        try:
            from scisaurus.runtime.paper import load_paper_survey
            survey = load_paper_survey(paper_config,
                require_eligible=not self._allows_provisional_progress())
        except Exception:
            # The normal paper validator will report the precise dependency
            # problem.  Do not hide it behind a best-effort synchronization.
            raise
        control = ControlStore(paper_config["survey_project_dir"])
        try:
            store = ArtifactStore(control)
            works = {}
            rows = control._conn.execute(
                "SELECT logical_id, MAX(version) AS version FROM artifacts "
                "WHERE logical_id LIKE 'kb/works/%' GROUP BY logical_id"
            ).fetchall()
            for row in rows:
                record = store.get(f"artifact:{row['logical_id']}@{row['version']}")
                body = json.loads(store.read_body(record["body_hash"]))
                works[body.get("work_id")] = body
            identities = {}
            for ref, identity in survey.get("identities", {}).items():
                if isinstance(identity, dict) and identity.get("work_id"):
                    identities[identity["work_id"]] = identity
        finally:
            control.close()

        # Catalog-backed missions are a new scientific identity.  Their
        # references must be projected from the current survey rather than
        # appended to a descriptor inherited from a previous topic.
        if self.workflow.get("experiment_catalog"):
            paper_config["references"] = []
        existing_refs = paper_config.get("references", [])
        existing_source_refs = {item.get("source_ref") for item in existing_refs if isinstance(item, dict)}
        existing_keys = {item.get("key") for item in existing_refs if isinstance(item, dict)}
        # Keep the configured order stable while prioritizing full text for new
        # entries.  A bounded cap prevents a broad search from turning the
        # writer prompt into an accidental bibliography dump.
        sources = sorted(
            survey.get("sources", {}).items(),
            key=lambda pair: (pair[1].get("representation") != "full_text", pair[1].get("work_id", "")),
        )
        profile_floor = 0
        depth = paper_config.get("depth_profile")
        if isinstance(depth, dict):
            profile_floor = int(depth.get("min_references", 0))
        target = max(profile_floor, len(existing_refs))
        target = min(max(target, profile_floor + 10), 50) if profile_floor else min(max(target, 20), 50)
        for source_ref, source in sources:
            if len(paper_config["references"]) >= target and source.get("representation") != "full_text":
                break
            if source_ref in existing_source_refs:
                continue
            work_id = source.get("work_id")
            work = works.get(work_id, {})
            key = re.sub(r"[^a-z0-9_-]+", "-", f"oa_{str(work_id).casefold()}").strip("-")[:64]
            if not key or not key[0].isalpha():
                key = f"ref_{len(existing_keys)}"
            if key in existing_keys:
                key = f"oa_{str(work_id).casefold()}_{len(existing_keys)}"
            identity = identities.get(work_id, {})
            # DOI fields are only emitted when an independently reconciled
            # identity exists.  A catalog record alone remains a URL citation.
            doi = identity.get("doi") if identity.get("status") == "verified" else None
            authors = work.get("authors") or source.get("authors") or "Authors not supplied by OpenAlex"
            if isinstance(authors, list):
                authors = ", ".join(
                    item if isinstance(item, str) else str(item) for item in authors)
            paper_config["references"].append({
                "key": key,
                "title": work.get("title") or source.get("title") or f"OpenAlex work {work_id}",
                "authors": str(authors),
                "year": str(work.get("year") or "unknown"),
                "doi": doi,
                "url": source.get("url") or f"https://openalex.org/{work_id}",
                "source_ref": source_ref,
                **({"identity_ref": next((ref for ref, item in survey.get("identities", {}).items()
                                          if isinstance(item, dict) and item.get("work_id") == work_id), None)}
                   if doi else {}),
            })
            existing_source_refs.add(source_ref)
            existing_keys.add(key)
            if len(paper_config["references"]) >= target:
                # Continue only when the full-text floor still needs entries.
                full_text_count = sum(
                    survey.get("sources", {}).get(item.get("source_ref"), {}).get("representation") == "full_text"
                    for item in paper_config["references"]
                )
                full_text_floor = int(depth.get("min_full_text_references", 0)) if isinstance(depth, dict) else 0
                if full_text_count >= full_text_floor:
                    break
        return paper_config

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
        prior_record = self.stage_records.get(stage["id"], {})
        continuation_task = stage["id"] in self.reopened_stage_ids and self.continuation_cycles
        task_id = None if continuation_task else (
            prior_record.get("task_id") if isinstance(prior_record, dict) else None)
        if not isinstance(task_id, str) or not task_id:
            task_id = f"composer-{self.workflow['id']}-{stage['id']}"
            if continuation_task:
                task_id += f"-continuation-{self.continuation_cycles}"
        try:
            existing = self.tasks.get(task_id)
        except NotFoundError:
            existing = None
        if existing is not None:
            state = existing["state"]
            if state in {"blocked", "paused"}:
                try:
                    self.tasks.transition(
                        task_id, "queued", "command.composer", reason="scoped stage recovery"
                    )
                    return self.tasks.get(task_id)
                except StateError:
                    # A concurrent cleanup may have terminalized the task
                    # between the read and the transition. Fall through to a
                    # fresh generation rather than handing a stale row to
                    # start_attempt().
                    existing = self.tasks.get(task_id)
                    state = existing["state"]
            elif state in {"queued", "running"}:
                return self.tasks.get(task_id)
            else:
                # A continuation task is a new bounded work order. Its old
                # aggregate can legitimately be stale/failed/completed after
                # a watchdog kill or a crash at the stage boundary. Reusing
                # that row makes the next start_attempt fail with
                # "not dispatchable (state=stale)" and turns a recoverable
                # run into a terminal Composer blocker.
                seed = canonical_bytes({
                    "stage_id": stage["id"],
                    "cycle": self.continuation_cycles,
                    "prior_task_id": task_id,
                    "prior_state": state,
                    "attempt_id": prior_record.get("attempt_id")
                    if isinstance(prior_record, dict) else None,
                })
                recovery = hashlib.sha256(seed).hexdigest()[:12]
                base_task_id = task_id
                generation = 0
                while True:
                    suffix = f"-recovery-{recovery}"
                    if generation:
                        suffix += f"-{generation}"
                    candidate_id = f"{base_task_id}{suffix}"
                    try:
                        candidate = self.tasks.get(candidate_id)
                    except NotFoundError:
                        task_id, existing = candidate_id, None
                        break
                    if candidate["state"] in {"queued", "running"}:
                        task_id, existing = candidate_id, candidate
                        break
                    generation += 1
        if existing is not None:
            return self.tasks.get(task_id)
        route = self.departments.stage_route(stage["kind"])
        self.tasks.create(task_id, "production", {
            "stage_id": stage["id"], "kind": stage["kind"], "objective": self.workflow["objective"],
            "depends_on": stage["depends_on"], "config_path": stage["config_path"],
            "department": route["department"], "role": route["role"],
            "owner_agent": route["chief"], "adversary_agent": route["adversary"],
            "required_role_ids": route["required_role_ids"],
            "required_agents": route["required_agents"],
            "verifier_agent": route["verifier_agent"],
            "role_quotas": route["role_quotas"],
            "assignment_policy": "bounded_on_demand_role_isolated",
        }, "command.composer")
        return self.tasks.admit(task_id, "command.composer")

    @staticmethod
    def _stage_assignment_fields(plan, result=None):
        """Project role dispatch evidence into a compact stage checkpoint."""
        if not isinstance(plan, dict):
            return {}
        fields = {
            "required_agents": deepcopy(plan.get("required_agents", [])),
            "active_agents": deepcopy(plan.get("active_agents", [])),
            "verifier_agent": plan.get("verifier_agent"),
            "chief_agent": plan.get("chief_agent"),
            "role_quotas": deepcopy(plan.get("role_quotas", {})),
            "assignment_plan_ref": plan.get("plan_ref"),
            "assignment_ids": deepcopy(plan.get("assignment_ids", [])),
            "assignment_task_ids": deepcopy(plan.get("task_ids", [])),
            "assignment_deadline_seconds": plan.get("deadline_seconds"),
        }
        if isinstance(result, dict):
            fields.update({
                "chief_synthesis_ref": result.get("chief_synthesis_ref"),
                "verifier_artifact_ref": result.get("verifier_artifact_ref"),
                "verifier_outcome": result.get("verifier_outcome"),
                "specialist_failure_scope": result.get("failure_scope"),
                "specialist_assignments": deepcopy(result.get("assignments", [])),
            })
        return fields

    @staticmethod
    def _retire_stage_assignment(fields):
        """Separate the last dispatch from roles that are still executing.

        Assignment plans are durable evidence, but their ``active_agents``
        projection is live state.  Keeping the dispatch list after a stage
        returns makes a completed or retrying stage look like it still owns
        worker slots.  Preserve the list for auditability under
        ``last_active_agents`` and expose an empty live list until the next
        admission installs a fresh plan.
        """
        retired = deepcopy(fields) if isinstance(fields, dict) else {}
        active = retired.get("active_agents")
        if isinstance(active, list) and active:
            retired["last_active_agents"] = deepcopy(active)
        retired["active_agents"] = []
        return retired

    @staticmethod
    def _active_stage_role_ids(stage):
        """Select the smallest useful preflight pool for the stage.

        The organization manifest describes the complete bounded capability
        pool, but every role is not a reason to make a model call.  Survey's
        production runner already owns search, cataloging, source capture,
        identity checks, and fact verification.  Activating all six of those
        roles before production duplicated the same work without giving them
        a concrete result to review.  Keep one search-planning specialist for
        admission and reserve the independent adversary for the completed
        stage result.
        """
        if not isinstance(stage, dict):
            return None
        if stage.get("kind") == "survey":
            return ["search-strategist"]
        return None

    @staticmethod
    def _specialist_model_config(stage, descriptor):
        """Load the model config used by a stage, when one is declared."""
        if not isinstance(descriptor, dict):
            return None
        model = descriptor.get("model")
        if isinstance(model, dict):
            return deepcopy(model) if model.get("base_url") and model.get("model") else None
        model_path = descriptor.get("model_config_path")
        if not isinstance(model_path, str) or not model_path:
            return None
        path = Path(model_path)
        if not path.is_file():
            return None
        try:
            model = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            return None
        return deepcopy(model) if isinstance(model, dict) and model.get("base_url") and model.get("model") else None

    @staticmethod
    def _specialist_provider_pools(descriptor):
        limits = descriptor.get("limits") if isinstance(descriptor, dict) else None
        pools = limits.get("provider_pools") if isinstance(limits, dict) else None
        return deepcopy(pools) if isinstance(pools, dict) else None

    def _specialist_file_ref(self, artifact):
        """Return the dashboard-safe object path for a Composer artifact."""
        body_hash = artifact.get("body_hash") if isinstance(artifact, dict) else None
        if not isinstance(body_hash, str) or len(body_hash) != 64:
            return None
        return f"composer::objects/sha256/{body_hash}"

    def _specialist_ancestor_contexts(self, stage):
        """Return the current dependency closure in nearest-first order.

        Specialist prompts must be built from the current stage result and its
        actual scientific ancestors.  Looking only at ``stage_result`` leaves
        interpretation/argument roles without the experiment package, while
        forwarding the whole Composer context breaks role isolation.  This
        helper keeps the lookup deterministic and lets the named projections
        below select only the fields a role is allowed to see.
        """
        by_id = {item.get("id"): item for item in self.workflow.get("stages", [])
                 if isinstance(item, dict) and isinstance(item.get("id"), str)}
        pending = list(stage.get("depends_on", [])) if isinstance(stage, dict) else []
        seen = set()
        contexts = []
        while pending:
            stage_id = pending.pop(0)
            if stage_id in seen or stage_id not in by_id:
                continue
            seen.add(stage_id)
            context = self.context.get(stage_id)
            if isinstance(context, dict):
                contexts.append(context)
            pending.extend(by_id[stage_id].get("depends_on", []))
        return contexts

    @staticmethod
    def _specialist_json_value(value):
        """Load a local JSON artifact when a stage context stores a path."""
        if isinstance(value, dict):
            return deepcopy(value)
        if not isinstance(value, str):
            return value
        path = Path(value)
        if not path.is_file():
            return value
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            return value
        return deepcopy(loaded)

    @staticmethod
    def _specialist_result_spine(results):
        """Expose the reproducible result spine without raw execution bulk."""
        results = ComposerRunner._specialist_json_value(results)
        if not isinstance(results, dict):
            return results
        selected = {}
        for key in (
                "schema_version", "id", "revision", "study_type", "question",
                "hypothesis", "procedures", "metrics", "findings", "limitations",
                "assets", "validation"):
            if key in results:
                selected[key] = deepcopy(results[key])
        return selected or deepcopy(results)

    @staticmethod
    def _specialist_interpretation_value(value):
        value = ComposerRunner._specialist_json_value(value)
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("interpretation"), dict):
            value = value["interpretation"]
        return {
            key: deepcopy(value[key]) for key in (
                "schema_version", "research_question", "result_patterns",
                "competing_explanations", "prioritization", "conclusion",
                "discriminating_experiments", "limitations", "evidence_gaps",
                "requested_actions") if key in value
        }

    @staticmethod
    def _specialist_argument_value(value):
        value = ComposerRunner._specialist_json_value(value)
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("argument_package"), dict):
            value = value["argument_package"]
        if isinstance(value.get("argument"), dict):
            value = value["argument"]
        return {
            key: deepcopy(value[key]) for key in (
                "schema_version", "research_question", "observed_patterns",
                "hypotheses", "primary_argument", "limitations",
                "discriminating_experiments", "figure_plan", "claims")
            if key in value
        }

    def _specialist_stage_result_projection(self, stage, stage_result):
        """Expose named scientific inputs from a completed stage result.

        Topic discovery is the graph frontier: it has no upstream context from
        which its seed plan, candidate portfolio, or source records could be
        projected.  Its specialists therefore review the generated result,
        while later stages retain the normal pre-execution planning packet.
        The aliases below keep role contracts stable without forwarding the
        whole result into any one prompt.
        """
        if not isinstance(stage_result, dict):
            return {}
        projected = deepcopy(stage_result)
        product = stage_result.get("review_product")
        if isinstance(product, dict):
            plan, draft = product["plan"], product["draft"]
            sources = product["sources"]
            journal = next(v for v in plan["venues"] if v["journal_id"] == plan["journal_id"])
            projected.update({
                "argument": {key: plan[key] for key in ("thesis", "insights", "outline", "benchmarks")},
                "evidence": sources, "evidence_map": product["unit_sources"],
                "paper_contract": {"article_type": "critical_review", "coverage_limits": plan["coverage_limits"], "venue": journal},
                "style_constraints": "Critical synthesis; distinguish reported findings, hypotheses, and uncertainty. No original experiments or systematic-coverage claim.",
                "draft": draft, "manuscript": draft, "manuscript_source": draft,
                "review_package": product["peer_reviews"], "journal_contract": journal,
                "source_coverage": product["coverage"], "claims": plan["insights"],
                "references": [s["work"] for s in sources if s["kind"] == "article"],
                "bibliography": [s["work"] for s in sources if s["kind"] == "article"],
                "citations": product["unit_sources"], "figure_manifest": product["render"],
            })
            return projected
        if stage.get("kind") == "interpretation":
            ancestors = self._specialist_ancestor_contexts(stage)
            experiment = next((item for item in ancestors
                               if item.get("kind") == "experiment"), {})
            results = self._specialist_result_spine(
                experiment.get("results_package") or stage_result.get("results_package"))
            interpretation = self._specialist_interpretation_value(
                stage_result.get("interpretation"))
            question = (interpretation.get("research_question")
                        if isinstance(interpretation, dict) else None)
            if not question and isinstance(results, dict):
                question = results.get("question")
            alternatives = (interpretation.get("competing_explanations", [])
                            if isinstance(interpretation, dict) else [])
            projected.update({
                "research_question": question,
                "results": results,
                "evidence": results,
                "interpretation": interpretation,
                "mechanism": deepcopy(alternatives),
                "alternative_hypotheses": deepcopy(alternatives),
                "limitations": deepcopy(results.get("limitations", [])
                                         if isinstance(results, dict) else []),
            })
            return projected

        if stage.get("kind") == "argument":
            ancestors = self._specialist_ancestor_contexts(stage)
            experiment = next((item for item in ancestors
                               if item.get("kind") == "experiment"), {})
            interpretation_context = next((item for item in ancestors
                                           if item.get("kind") == "interpretation"), {})
            results = self._specialist_result_spine(experiment.get("results_package"))
            interpretation = self._specialist_interpretation_value(
                interpretation_context.get("interpretation"))
            argument_package = stage_result.get("argument_package")
            if not isinstance(argument_package, dict):
                # A rejected adjudication is carried on the runner exception
                # rather than in a normal stage result.  The failure review
                # envelope preserves these fields; project them through the
                # same bounded path as a successful argument package.
                if isinstance(stage_result.get("research_argument"), dict):
                    argument_package = {
                        "argument": deepcopy(stage_result["research_argument"]),
                    }
                    if isinstance(stage_result.get("research_review"), dict):
                        argument_package["review"] = deepcopy(stage_result["research_review"])
            argument = self._specialist_argument_value(
                argument_package or stage_result.get("argument"))
            review = argument_package.get("review") if isinstance(argument_package, dict) else None
            if not isinstance(review, dict):
                review = stage_result.get("review")
            review = {
                key: deepcopy(review[key]) for key in (
                    "schema_version", "decision", "checks", "findings",
                    "required_repairs", "rationale") if isinstance(review, dict) and key in review
            }
            claims = []
            if isinstance(argument, dict):
                claims.extend(deepcopy(argument.get("observed_patterns", [])))
                primary = argument.get("primary_argument")
                if isinstance(primary, dict):
                    claims.append(deepcopy(primary))
            evidence_records = []
            if isinstance(results, dict):
                for key in ("procedures", "metrics", "findings", "limitations", "assets"):
                    value = results.get(key)
                    if isinstance(value, list):
                        evidence_records.extend(deepcopy(value))
            if isinstance(interpretation, dict):
                evidence_records.extend(deepcopy(interpretation.get("result_patterns", [])))
            research_question = (argument.get("research_question")
                                 if isinstance(argument, dict) else None)
            if not research_question and isinstance(results, dict):
                research_question = results.get("question")
            projected.update({
                "research_question": research_question,
                "claims": claims,
                "results": results,
                "interpretation": interpretation,
                "review_findings": review,
                "argument_plan": argument,
                "mechanism": deepcopy(argument.get("hypotheses", [])
                                      if isinstance(argument, dict) else []),
                "limitations": deepcopy(argument.get("limitations", [])
                                         if isinstance(argument, dict) else []),
                "evidence_records": evidence_records,
                "section_contract": {
                    "research_question": research_question,
                    "figure_plan": deepcopy(argument.get("figure_plan", [])
                                             if isinstance(argument, dict) else []),
                },
                "style_constraints": (
                    "Report observations separately from mechanisms; keep unsupported explanations "
                    "provisional and bind every material claim to an evidence record."),
                "target_audience": "readers evaluating a bounded computational result",
            })
            return projected

        if stage.get("kind") == "paper":
            ancestors = self._specialist_ancestor_contexts(stage)
            argument_context = next((item for item in ancestors
                                     if item.get("kind") == "argument"), {})
            experiment = next((item for item in ancestors
                               if item.get("kind") == "experiment"), {})
            argument = self._specialist_argument_value(
                argument_context.get("argument_package") or argument_context.get("argument"))
            results = self._specialist_result_spine(experiment.get("results_package"))
            projected.update({
                "argument": argument,
                "claims": deepcopy(argument.get("observed_patterns", [])
                                     if isinstance(argument, dict) else []),
                "evidence": results,
                "paper_contract": {
                    "stage_id": stage.get("id"),
                    "release_status": stage_result.get("release_status"),
                    "output_path": stage_result.get("output_path"),
                },
                "style_constraints": (
                    "Preserve the accepted evidence boundary, distinguish results from discussion, "
                    "and expose unresolved reviewer findings."),
                "review_package": deepcopy(stage_result.get("review_package", {})),
                "draft": deepcopy(stage_result.get("draft")) if "draft" in stage_result else None,
                "manuscript": deepcopy(stage_result.get("manuscript"))
                    if "manuscript" in stage_result else None,
            })
            return projected

        if stage.get("kind") != "topic_discovery":
            return projected

        frontier_plan = stage_result.get("frontier_seed_plan")
        seeds = (frontier_plan.get("seeds", [])
                 if isinstance(frontier_plan, dict) else [])
        recent_papers = stage_result.get("recent_papers")
        if not isinstance(recent_papers, list):
            recent_papers = []
        candidate_prior_work = stage_result.get("candidate_prior_work")
        if not isinstance(candidate_prior_work, list):
            candidate_prior_work = []
        topic = stage_result.get("topic")
        if not isinstance(topic, dict):
            topic = {}
        selected_seed_id = topic.get("frontier_seed_id")
        selected_domain = topic.get("domain")
        selected_seed_records = [
            deepcopy(item) for item in (candidate_prior_work or recent_papers)
            if isinstance(item, dict)
            and (
                selected_seed_id is None
                or item.get("frontier_seed_id") == selected_seed_id
                or (
                    item.get("frontier_seed_id") == "selected_direction"
                    and selected_domain
                    and item.get("frontier_domain") == selected_domain
                )
            )
        ]

        projected.update({
            "candidate_topics": deepcopy(stage_result.get("candidates", [])),
            "frontier_seeds": deepcopy(seeds),
            "scholarly_records": deepcopy(recent_papers),
            # The broad sample is useful for frontier scouting but is not
            # evidence for the selected direction. Keep both scopes named so
            # role projections and the verifier cannot mistake inspiration
            # records from other seeds for selected-topic support.
            "frontier_inspiration_records": deepcopy(recent_papers),
            "selected_seed_id": selected_seed_id,
            "selected_seed_records": selected_seed_records,
            "topic_history": self._topic_history_context(),
            "known_gaps": deepcopy(stage_result.get("proposed_gap", "")),
            "source_classes": sorted({
                item.get("source_class") for item in recent_papers
                if isinstance(item, dict) and isinstance(item.get("source_class"), str)
            }),
            "search_results": deepcopy(candidate_prior_work or selected_seed_records or recent_papers),
            "prior_work": deepcopy(candidate_prior_work or selected_seed_records or recent_papers),
            "experiment_feasibility": deepcopy(stage_result.get("feasibility_check", {})),
            "research_question": stage_result.get("question") or topic.get("research_question"),
            "search_terms": deepcopy(stage_result.get("search_queries", topic.get("search_queries", []))),
            "candidate_methods": deepcopy(topic.get("resource_plan", "")),
            "capability_inventory": deepcopy(stage_result.get("feasibility_check", {})),
        })
        return projected

    def _specialist_experiment_projection(self, stage, descriptor, stage_result=None):
        """Project an actionable brief before an experiment has observations.

        Methods specialists are admitted before the experiment runner. A
        continuation therefore cannot rely on ``stage_result`` containing
        raw observations yet, but it must still receive the question, declared
        design, feasibility boundary, and prior failure history. Keep this
        projection bounded instead of forwarding the full dependency graph.
        """
        if stage.get("kind") != "experiment":
            return {}

        by_id = {item["id"]: item for item in self.workflow.get("stages", [])}
        pending = list(stage.get("depends_on", []))
        ancestor_ids = set()
        while pending:
            current = pending.pop()
            if current in ancestor_ids or current not in by_id:
                continue
            ancestor_ids.add(current)
            pending.extend(by_id[current].get("depends_on", []))
        topic_context = next(
            (self.context.get(item_id) for item_id in ancestor_ids
             if isinstance(self.context.get(item_id), dict)
             and self.context[item_id].get("kind") == "topic_discovery"
             and isinstance(self.context[item_id].get("topic"), dict)),
            None,
        )
        if topic_context is None:
            topic_context = next(
                (value for value in self.context.values()
                 if isinstance(value, dict)
                 and value.get("kind") == "topic_discovery"
                 and isinstance(value.get("topic"), dict)),
                None,
            )
        if not isinstance(topic_context, dict):
            return {}

        selected = topic_context["topic"]
        program = topic_context.get("research_program")
        selected_branch = None
        if isinstance(program, dict) and isinstance(program.get("branches"), list):
            selected_branch = next(
                (branch for branch in program["branches"]
                 if isinstance(branch, dict) and branch.get("id") == selected.get("id")),
                None,
            )
        selected_branch = selected_branch if isinstance(selected_branch, dict) else {}
        feasibility = topic_context.get("feasibility_check")
        feasibility = feasibility if isinstance(feasibility, dict) else {}
        feasibility_plan = feasibility.get("plan")
        feasibility_plan = feasibility_plan if isinstance(feasibility_plan, dict) else {}
        configured_experiment = descriptor.get("experiment") if isinstance(descriptor, dict) else None
        configured_experiment = configured_experiment if isinstance(configured_experiment, dict) else {}
        capability_source = "topic_only"
        generated = topic_context.get("generated_capability")
        generated_experiment = None
        if isinstance(generated, dict) and isinstance(generated.get("descriptor_path"), str):
            try:
                generated_descriptor = json.loads(Path(generated["descriptor_path"]).read_text())
                candidate_experiment = generated_descriptor.get("experiment")
                if isinstance(candidate_experiment, dict):
                    generated_experiment = candidate_experiment
            except (OSError, TypeError, ValueError):
                generated_experiment = None

        # Specialist admission happens before _run_stage materializes the
        # selected capability.  The immutable stage descriptor can therefore
        # still contain a template from an older mission (the live workflow
        # used robust_mean while the selected topic is spectral winding).
        # Never expose that unrelated design as if it were the current
        # experiment. Prefer the already admitted topic capability; otherwise
        # accept the stage descriptor only when its scientific identity matches
        # the selected topic.
        if isinstance(generated_experiment, dict):
            generated_question = generated_experiment.get("research_question")
            generated_id = generated_experiment.get("id")
            selected_capability_id = (
                selected.get("experiment_capability_id")
                or generated.get("capability_id")
            )
            if (generated_question == selected.get("research_question")
                    or generated_id == selected_capability_id):
                configured_experiment = generated_experiment
                capability_source = "admitted_topic_capability"
        if capability_source == "topic_only":
            descriptor_question = configured_experiment.get("research_question")
            descriptor_id = configured_experiment.get("id")
            selected_capability_id = selected.get("experiment_capability_id")
            if (descriptor_question == selected.get("research_question")
                    or (isinstance(selected_capability_id, str)
                        and descriptor_id == selected_capability_id)):
                capability_source = "matching_stage_descriptor"
            else:
                configured_experiment = {}
        result = stage_result if isinstance(stage_result, dict) else {}

        question = topic_context.get("question") or selected.get("research_question")
        hypothesis = selected_branch.get("hypothesis") or selected.get("disconfirmation_test")
        method_constraints = {
            key: deepcopy(selected.get(key))
            for key in ("scope", "data_regime", "feasibility", "resource_plan",
                        "capability_requirements", "comparison", "measurement",
                        "disconfirmation_test")
            if selected.get(key) is not None
        }
        method_constraints["feasibility_plan"] = deepcopy(feasibility_plan)
        available_assets = {
            "prior_work_ids": deepcopy(selected.get("prior_work_ids", [])),
            "evidence_inputs": deepcopy(feasibility_plan.get("evidence_inputs", [])),
            "required_executables": deepcopy(feasibility_plan.get("required_executables", [])),
            "required_packages": deepcopy(feasibility_plan.get("required_packages", [])),
            "network_access": feasibility_plan.get("network_access"),
        }
        design = {
            key: deepcopy(configured_experiment[key])
            for key in ("study_type", "method", "parameters", "primary_outcomes",
                        "stopping_rule", "limitations")
            if key in configured_experiment
        }
        topic_design = selected.get("experiment_design")
        if isinstance(topic_design, dict):
            design["proposed_design"] = deepcopy(topic_design)
        analysis_plan = {
            "research_question": question,
            "hypothesis": hypothesis,
            "comparison": selected.get("comparison"),
            "measurement": selected.get("measurement"),
            "disconfirmation_test": selected.get("disconfirmation_test"),
            "primary_outcomes": deepcopy(configured_experiment.get("primary_outcomes", [])),
            "stopping_rule": deepcopy(configured_experiment.get("stopping_rule")),
            "capability_source": capability_source,
            "state": "pre_execution" if not result else "stage_result_available",
        }
        raw_results = result.get("results_package") or result.get("raw_results")
        derived_results = {
            key: deepcopy(result[key])
            for key in ("metrics", "findings", "analysis")
            if key in result
        }
        figures = deepcopy(result.get("assets", [])) if isinstance(result.get("assets"), list) else []
        prior_stage_context = self.context.get(stage.get("id"), {})
        prior_stage_context = prior_stage_context if isinstance(prior_stage_context, dict) else {}
        failure_recovery = prior_stage_context.get("failure_recovery")
        failure_recovery = failure_recovery if isinstance(failure_recovery, dict) else None
        program_snapshot = self._failure_program_snapshot(stage)
        blockers = [
            {key: deepcopy(item.get(key)) for key in ("stage_id", "reason", "diagnostics") if key in item}
            for item in self.blockers
            if isinstance(item, dict) and item.get("stage_id") == stage.get("id")
        ][-4:]
        try:
            input_digests = self._stage_input_files(stage, descriptor)
        except (OSError, TypeError, ValueError):
            input_digests = {}
        return {
            "research_question": question,
            "hypotheses": hypothesis,
            "method_constraints": method_constraints,
            "available_assets": available_assets,
            "design": design,
            "analysis_plan": analysis_plan,
            "claims": {
                "declared_hypothesis": hypothesis,
                "disconfirmation_test": selected.get("disconfirmation_test"),
                "status": "declared_not_observed",
            },
            "execution_manifest": {
                "state": "planned",
                "capability_id": selected.get("experiment_capability_id"),
                "capability_source": capability_source,
                "execution_mode": feasibility_plan.get("execution_mode"),
                "required_executables": deepcopy(feasibility_plan.get("required_executables", [])),
                "required_packages": deepcopy(feasibility_plan.get("required_packages", [])),
                "network_access": feasibility_plan.get("network_access"),
            },
            "input_digests": input_digests,
            "raw_results": deepcopy(raw_results) if raw_results is not None else None,
            "analysis_code": (
                {"state": "failure_snapshot", "files": program_snapshot,
                 "failure_dossier_ref": prior_stage_context.get("failure_dossier_ref")}
                if program_snapshot or failure_recovery else
                {"state": "not_available_before_execution",
                 "reason": "The executable is generated and admitted after the methods brief."}
            ),
            "derived_results": derived_results,
            "figures": figures,
            "failure_history": blockers,
            "failure_recovery": deepcopy(failure_recovery) if failure_recovery else None,
        }

    def _specialist_stage_packet(self, stage, descriptor, *, stage_result=None):
        """Build a bounded, non-secret packet for specialist input projection."""
        packet = {
            "objective": self.workflow["objective"],
            "stage_id": stage["id"],
            "stage_kind": stage["kind"],
            "work_orders": self._follow_up_projection(self._requests_for_stage(stage)),
            "dependencies": deepcopy(self.context),
            "stage_result": self._specialist_stage_result_projection(
                stage, stage_result or {}),
            "configured_stage": {
                key: deepcopy(value) for key, value in (descriptor or {}).items()
                if key not in {"model", "model_config_path", "bibliography"}
            },
        }
        packet.update(self._specialist_experiment_projection(
            stage, descriptor, stage_result=stage_result))
        if (isinstance(stage_result, dict)
                and stage_result.get("_repair_panel") is True
                and isinstance(stage_result.get("capability_repair_packet"), dict)):
            packet["repair_panel"] = True
            packet["capability_repair_packet"] = deepcopy(
                stage_result["capability_repair_packet"])
        if stage["kind"] == "topic_discovery":
            model = self._specialist_model_config(stage, descriptor)
            if model is not None:
                packet["runtime_context"] = self._runtime_context(model)
        return packet

    def _specialist_progress(self, stage_id, event):
        """Publish a thread-safe live projection without touching SQLite."""
        if not isinstance(event, dict):
            return
        role = event.get("role")
        if not isinstance(role, str):
            return
        live = {key: deepcopy(event.get(key)) for key in (
            "event", "role", "role_id", "task_id", "stage_id", "model_role", "route_id",
            "provider_pool", "model", "base_url", "cache_prompt", "execution_mode", "status",
            "decision", "elapsed_seconds", "error", "attempts", "usage", "artifact_ref",
            "response_ref",
        ) if key in event}
        live["observed_at"] = now_iso()
        now = self.clock()
        remaining_snapshot = self.deadline - now
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining_snapshot = min(remaining_snapshot, self.deadline_epoch - time.time())
        with self._progress_lock:
            state = deepcopy(self._progress_snapshot)
            state.update({
                "phase": f"{stage_id}:specialists",
                "elapsed_seconds": max(0.0, now - self.started),
                "remaining_seconds": max(0.0, remaining_snapshot),
                "started_at_epoch": self.started_epoch,
                "deadline_at_epoch": self.deadline_epoch,
            })
            stage_record = state.setdefault("stages", {}).setdefault(stage_id, {})
            previous = stage_record.setdefault("specialist_live", {}).get(role, {})
            if isinstance(previous, dict) and event.get("event") == "completed":
                live = {**previous, **live}
                live.setdefault("started_at", previous.get("observed_at"))
            if event.get("event") == "dispatched":
                live["started_at"] = live["observed_at"]
            live["updated_at"] = live["observed_at"]
            stage_record["specialist_live"][role] = live
            self._progress_snapshot = deepcopy(state)
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            temporary = output / f"progress-specialist-{uuid.uuid4().hex}.tmp"
            temporary.write_bytes(canonical_bytes(state))
            temporary.replace(output / "progress.json")

    def _dispatch_specialist_work(self, dispatcher, assignments, packet, *, verifier=False):
        """Persist each completed role before another role can fail or time out."""
        cache = ModelWorkCache(self.store, self._publish)
        pending, reports, keys = [], [], {}

        def reusable_cached_report(value):
            """Keep transport failures out of the scientific result cache."""
            if not isinstance(value, dict):
                return False
            if value.get("status") == "result_unknown":
                return False
            if value.get("status_code") in {408, 425, 429, 500, 502, 503, 504}:
                return False
            error = str(value.get("error") or "").casefold()
            return not any(token in error for token in (
                "provider", "cooling down", "transport", "http request",
                "connection reset", "connection refused", "timeout",
            ))

        for assignment in assignments:
            prompt = assignment.get("_prompt") or build_specialist_prompt(assignment, packet)
            key = cache.key(scope=f"specialist:{assignment['stage_id']}:{verifier}",
                role=assignment.get("assigned_role"), system=VERIFIER_SYSTEM if verifier else SPECIALIST_SYSTEM,
                prompt=prompt, model=dispatcher.model_config)
            keys[assignment["role_id"]] = key
            retained = cache.get(key)
            retained_report = retained.get("report") if isinstance(retained, dict) else None
            if (retained and retained.get("status") in {"succeeded", "blocked"}
                    and reusable_cached_report(retained_report)):
                report = deepcopy(retained["report"])
                report.update(usage={}, reused_from=retained["cache_ref"],
                              elapsed_seconds=0, request_attempts=0,
                              execution_mode="retained_model_result" if retained["status"] == "succeeded" else "retained_failure")
                reports.append(report)
            else:
                pending.append(assignment)
        # Cooldowns are route-scoped.  The capacity pool remains shared, but
        # a transient failure from one model must not fence every model served
        # by the same endpoint.  Legacy pool-scoped records are intentionally
        # not loaded here: they have no route provenance and were the source
        # of cross-model 24-hour stalls in resumed runs.
        cooldown_keys = (dispatcher.cooldown_keys()
                         if hasattr(dispatcher, "cooldown_keys") else [])
        for cooldown_key in cooldown_keys:
            record = self.store.head(f"command/provider-cooldowns/{cooldown_key}")
            if record:
                body = json.loads(self.store.read_body(record["body_hash"]))
                remaining = body["not_before_epoch"] - time.time()
                if remaining > 0:
                    dispatcher.provider_cooldowns[cooldown_key] = time.monotonic() + remaining

        def retain(report):
            if report.get("status") == "succeeded":
                cache.put(keys[report["role_id"]], {"status": "succeeded", "report": report})
            elif (report.get("status_code") not in {408, 425, 429, 500, 502, 503, 504}
                  and report.get("status") != "result_unknown"):
                # Failed/unknown reports remain failures. Retaining the exact
                # assignment prevents a new outer stage attempt from silently
                # replenishing its specialist call allowance.
                cache.put(keys[report["role_id"]], {"status": "blocked", "report": report})
            for cooldown_key, until in list(dispatcher.provider_cooldowns.items()):
                self._publish(f"command/provider-cooldowns/{cooldown_key}", "note", {
                    "cooldown_key": cooldown_key,
                    "not_before_epoch": time.time() + max(0, until - time.monotonic()),
                }, "command.controller")

        reports.extend(dispatcher.dispatch(pending, packet, verifier=verifier, on_result=retain))
        return reports

    def _run_specialist_pool(self, stage, stage_assignment, descriptor, *, stage_result=None):
        """Run the admitted specialist assignments in a bounded provider pool."""
        model = self._specialist_model_config(stage, descriptor)
        if model is None or not isinstance(stage_assignment, dict):
            return {"reports": [], "by_role": {}, "usage": {}, "model_enabled": False}
        assignments = [item for item in stage_assignment.get("assignments", [])
                       if isinstance(item, dict) and item.get("assignment_phase") == "specialist"]
        if not assignments:
            return {"reports": [], "by_role": {}, "usage": {}, "model_enabled": False}
        limits = descriptor.get("limits") if isinstance(descriptor, dict) else {}
        max_parallel = limits.get("concurrent_calls") if isinstance(limits, dict) else None
        if type(max_parallel) is not int or max_parallel < 1:
            max_parallel = min(4, len(assignments))
        deadline = time.monotonic() + min(float(stage["deadline_seconds"]), self._remaining())
        packet = self._specialist_stage_packet(
            stage, descriptor, stage_result=stage_result)
        dispatcher = SpecialistDispatcher(
            model, provider_pools=self._specialist_provider_pools(descriptor),
            max_parallel=min(max_parallel, len(assignments)), deadline=deadline,
            on_progress=lambda event: self._specialist_progress(stage["id"], event),
        )
        reports = self._dispatch_specialist_work(dispatcher, assignments, packet)
        by_role = {}
        for report in reports:
            role_id = report.get("role_id")
            if isinstance(role_id, str):
                by_role[role_id] = report
        return {
            "reports": reports,
            "by_role": by_role,
            "packet": packet,
            "usage": self._specialist_usage(reports),
            "model_enabled": True,
        }

    @staticmethod
    def _failure_stage_result(stage, error):
        """Build the bounded packet given to reviewers after producer failure.

        Stage runners can return a durable observation package and then fail
        admission.  Older control flow discarded that package before the
        specialist pool ran, so a failure became a bare string and the
        assigned reviewers were cancelled without doing any work.  Preserve
        the runner's exact package when available and otherwise expose only a
        bounded failure envelope; this never invents scientific observations.
        """
        retained = getattr(error, "stage_result", None)
        result = deepcopy(retained) if isinstance(retained, dict) else {
            "status": "blocked",
            "error": str(error),
            "failure_scope": "stage",
        }
        result.setdefault("status", "blocked")
        result.setdefault("error", str(error))
        result.setdefault("failure_scope", "stage")
        result.setdefault("stage_id", stage.get("id"))
        result.setdefault("kind", stage.get("kind"))
        usage = getattr(error, "usage", None)
        if isinstance(usage, dict) and not isinstance(result.get("usage"), dict):
            result["usage"] = deepcopy(usage)
        failure_class = getattr(error, "failure_class", None)
        if isinstance(failure_class, str) and failure_class:
            result.setdefault("failure_class", failure_class)
        # Runner exceptions are the only durable carrier for a rejected
        # argument when adjudication fails after producing a valid candidate.
        # Preserve that candidate and verdict for the failure-specialist panel;
        # reducing it to ``{status, error}`` made every reviewer report that the
        # argument was empty and prevented a meaningful repair order.
        for attribute in ("research_argument", "research_review", "research_feedback",
                          "research_response"):
            value = getattr(error, attribute, None)
            if value is not None:
                result[attribute] = deepcopy(value)
        if isinstance(result.get("research_argument"), dict):
            result.setdefault("argument", deepcopy(result["research_argument"]))
            package = result.get("argument_package")
            if not isinstance(package, dict):
                package = {}
            package.setdefault("argument", deepcopy(result["research_argument"]))
            if isinstance(result.get("research_review"), dict):
                package.setdefault("review", deepcopy(result["research_review"]))
            result["argument_package"] = package
        if isinstance(result.get("research_review"), dict):
            result.setdefault("review", deepcopy(result["research_review"]))
        return result

    def _run_failure_specialist_review(self, stage, stage_assignment, descriptor, error):
        """Give materialized failure evidence to specialists before recovery.

        This path is intentionally separate from ordinary producer success:
        it is only entered for a scientific/model-contract failure, never for
        provider cooldown, credential, deadline, or quota fences.  Reviewer
        calls therefore analyze the failed artifact or exact error once and
        can issue a scoped repair order without turning an operational outage
        into another expensive retry.
        """
        stage_result = self._failure_stage_result(stage, error)
        empty = {"reports": [], "by_role": {}, "usage": {},
                 "model_enabled": False, "packet": {}}
        try:
            bundle = self._run_specialist_pool(
                stage, stage_assignment, descriptor, stage_result=stage_result)
            bundle = self._publish_specialist_reports(
                stage, stage_assignment, bundle)
        except Exception as review_error:
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "failure_specialist_review_error",
                "stage_id": stage["id"],
                "error": f"{type(review_error).__name__}: {review_error}",
                "producer_failure": str(error)[:2048],
            })
            return empty, None, stage_result

        reports = bundle.get("reports", []) if isinstance(bundle, dict) else []
        verifier = None
        # A verifier is useful only when at least one producer specialist
        # returned an actual response.  Calling it against an all-failed pool
        # would consume quota without adding an independent judgment.
        if any(isinstance(report, dict)
               and report.get("status") == "succeeded"
               for report in reports):
            try:
                verifier = self._run_specialist_verifier(
                    stage, stage_assignment, descriptor, bundle, stage_result,
                    stage_result=stage_result)
            except Exception as verifier_error:
                self.department_activity.append({
                    "cycle": self.continuation_cycles,
                    "action": "failure_specialist_verifier_error",
                    "stage_id": stage["id"],
                    "error": f"{type(verifier_error).__name__}: {verifier_error}",
                    "producer_failure": str(error)[:2048],
                })
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "failure_specialist_review_completed",
            "stage_id": stage["id"],
            "producer_failure": str(error)[:2048],
            "specialist_count": len(reports),
            "successful_specialists": sum(
                1 for report in reports
                if isinstance(report, dict) and report.get("status") == "succeeded"),
            "verifier_dispatched": verifier is not None,
        })
        return bundle, verifier, stage_result

    @staticmethod
    def _specialist_usage(reports):
        totals = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
        by_role = {}
        for report in reports if isinstance(reports, list) else []:
            if not isinstance(report, dict):
                continue
            role_id = report.get("role_id") or report.get("assigned_role")
            usage = report.get("usage") if isinstance(report.get("usage"), dict) else {}
            if isinstance(role_id, str):
                by_role[role_id] = deepcopy(usage)
            for key in totals:
                value = usage.get(key, 0)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    totals[key] += value
        totals["by_role"] = by_role
        return totals

    def _publish_specialist_reports(self, stage, stage_assignment, bundle):
        """Persist each specialist's independent response in its own namespace."""
        if not bundle.get("model_enabled"):
            return bundle
        packet = bundle.get("packet") if isinstance(bundle.get("packet"), dict) else {}
        packet_digest = hashlib.sha256(canonical_bytes(packet)).hexdigest()
        updated = {}
        rows = [item for item in stage_assignment.get("assignments", [])
                if isinstance(item, dict) and item.get("assignment_phase") == "specialist"]
        reports = bundle.get("by_role", {})
        for row in rows:
            report = deepcopy(reports.get(row.get("role_id")))
            if not isinstance(report, dict):
                report = {
                    "status": "failed", "execution_mode": "model",
                    "assigned_role": row.get("assigned_role"), "role_id": row.get("role_id"),
                    "error": "specialist dispatcher returned no report", "usage": {},
                }
            body = {
                "schema_version": "specialist-execution-1",
                "project_id": self.workflow["project_id"],
                "stage_id": stage["id"], "stage_kind": stage["kind"],
                "attempt_number": stage_assignment.get("attempt_number"),
                "assigned_role": row.get("assigned_role"), "role_id": row.get("role_id"),
                "model_role": row.get("model_role"),
                "assignment_id": row.get("assignment_id"),
                "task_id": row.get("task_id"),
                "input_ref": row.get("input_ref"), "input_digest": packet_digest,
                "report": report,
                "created_at": now_iso(),
            }
            artifact = self._publish(
                f"{row['assignment_logical_id']}/execution", "report", body,
                row["assigned_role"],
            )
            report["artifact_ref"] = artifact["artifact_ref"]
            self._specialist_progress(stage["id"], {
                "event": "completed", "role": row.get("assigned_role"),
                "role_id": row.get("role_id"), "task_id": row.get("task_id"),
                "stage_id": stage["id"], "execution_mode": report.get("execution_mode"),
                "status": report.get("status"), "decision": report.get("decision"),
                "usage": report.get("usage", {}), "elapsed_seconds": report.get("elapsed_seconds"),
                "artifact_ref": report.get("artifact_ref"),
                "response_ref": self._specialist_file_ref(artifact),
                "error": report.get("error"),
            })
            updated[row.get("role_id")] = report
        bundle["by_role"] = updated
        bundle["reports"] = list(updated.values())
        bundle["usage"] = self._specialist_usage(bundle["reports"])
        return bundle

    def _run_specialist_verifier(self, stage, stage_assignment, descriptor, bundle, chief_result,
                                 *, stage_result=None):
        """Run the queued adversary after producer calls and chief output exist."""
        model = self._specialist_model_config(stage, descriptor)
        if model is None or not isinstance(stage_assignment, dict):
            return None
        verifier = next((item for item in stage_assignment.get("assignments", [])
                         if isinstance(item, dict) and item.get("assignment_phase") == "verifier"), None)
        if verifier is None:
            return None
        # The verifier must see the same concrete stage result that the
        # producer specialists reviewed. ``chief_result`` is the stage
        # outcome/ledger projection and may omit or rewrite scientific fields;
        # using it as the sole packet source made a valid frontier seed plan
        # appear empty to the adversary.
        packet_source = stage_result if isinstance(stage_result, dict) else chief_result
        packet = self._specialist_stage_packet(stage, descriptor, stage_result=packet_source)
        packet["chief_result"] = deepcopy(chief_result)
        packet["specialist_reports"] = deepcopy(bundle.get("reports", []))
        verifier = deepcopy(verifier)
        verifier["_prompt"] = build_verifier_prompt(
            stage, packet, bundle.get("reports", []), chief_result,
            max_input_tokens=(verifier.get("quota", {}) or {}).get("max_input_tokens"))
        limits = descriptor.get("limits") if isinstance(descriptor, dict) else {}
        max_parallel = limits.get("concurrent_calls") if isinstance(limits, dict) else 1
        if type(max_parallel) is not int or max_parallel < 1:
            max_parallel = 1
        deadline = time.monotonic() + min(float(stage["deadline_seconds"]), self._remaining())
        dispatcher = SpecialistDispatcher(
            model, provider_pools=self._specialist_provider_pools(descriptor),
            max_parallel=1, deadline=deadline,
            on_progress=lambda event: self._specialist_progress(stage["id"], event),
        )
        result = self._dispatch_specialist_work(dispatcher, [verifier], packet, verifier=True)
        report = result[0] if result else {
            "status": "failed", "error": "verifier dispatcher returned no report", "usage": {},
        }
        body = {
            "schema_version": "specialist-verifier-execution-1",
            "project_id": self.workflow["project_id"],
            "stage_id": stage["id"], "stage_kind": stage["kind"],
            "attempt_number": stage_assignment.get("attempt_number"),
            "assigned_role": verifier.get("assigned_role"), "role_id": verifier.get("role_id"),
            "model_role": verifier.get("model_role"),
            "assignment_id": verifier.get("assignment_id"), "task_id": verifier.get("task_id"),
            "chief_result": chief_result, "specialist_reports": bundle.get("reports", []),
            "report": report, "created_at": now_iso(),
        }
        artifact = self._publish(
            f"{verifier['assignment_logical_id']}/execution", "report", body,
            verifier["assigned_role"],
        )
        report["artifact_ref"] = artifact["artifact_ref"]
        self._specialist_progress(stage["id"], {
            "event": "completed", "role": verifier.get("assigned_role"),
            "role_id": verifier.get("role_id"), "task_id": verifier.get("task_id"),
            "stage_id": stage["id"], "model_role": verifier.get("model_role"),
            "execution_mode": report.get("execution_mode"), "status": report.get("status"),
            "decision": (report.get("response") or {}).get("decision") if isinstance(report.get("response"), dict) else None,
            "usage": report.get("usage", {}), "elapsed_seconds": report.get("elapsed_seconds"),
            "artifact_ref": report.get("artifact_ref"),
            "response_ref": self._specialist_file_ref(artifact),
            "error": report.get("error"),
        })
        return report

    def _stage_input_files(self, stage, descriptor):
        """Fingerprint declared input files, including images and bound packages."""
        files = {}
        def visit(value, key="", *, destinations=False, base=None):
            if isinstance(value, dict):
                for name, item in value.items():
                    visit(item, name, destinations=destinations, base=base)
            elif isinstance(value, list):
                for item in value:
                    visit(item, key, destinations=destinations, base=base)
            elif (isinstance(value, str)
                  and (key.endswith(("_path", "_file", "_files"))
                       or key in {"path", "images", "results_package"})
                  and not key.endswith("budget_path")
                  and not (destinations and key in {"output_path", "preflight_path"})):
                path = Path(value)
                if not path.is_absolute():
                    if base is None:
                        return
                    path = base / path
                path = path.resolve()
                if not path.is_file() or str(path) in files:
                    return
                body = path.read_bytes()
                files[str(path)] = hashlib.sha256(body).hexdigest()
                if path.suffix == ".json":
                    try:
                        nested = json.loads(body)
                    except (ValueError, UnicodeDecodeError):
                        return
                    visit(nested, destinations=True, base=path.parent)
        visit(descriptor, destinations=True)
        for dependency in stage["depends_on"]:
            visit(self.context.get(dependency))
        for binding in stage.get("bindings", []):
            visit(self._source_value(binding["source"]), binding["target"].rsplit(".", 1)[-1])
        return files

    @staticmethod
    def _stage_runtime_revision():
        """Bind stage admission to the implementation of its execution contract.

        Model-work caches retain their evidence/input identities separately;
        changing runtime validation must not discard successful source work.
        """
        root = Path(__file__).resolve().parent.parent
        digest = hashlib.sha256()
        for directory in ("core", "runtime"):
            for path in sorted((root / directory).rglob("*.py")):
                digest.update(str(path.relative_to(root)).encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    def _run_stage(self, stage, *, attempt_number=1, specialist_reports=None):
        """Reuse completed production when only an outer reviewer failed."""
        descriptor = json.loads(Path(stage["config_path"]).read_text())
        files = self._stage_input_files(stage, descriptor)
        packet = {
            "descriptor": descriptor, "files": files,
            "dependencies": {key: self.context.get(key) for key in stage["depends_on"]},
            "work_orders": self._requests_for_stage(stage),
            "specialists": [{key: report.get(key) for key in ("assigned_role", "status", "response")}
                            for report in specialist_reports or []],
            "progression_policy": self.workflow.get("progression_policy", "evidence_first"),
            "runtime_revision": self._stage_runtime_revision(),
        }
        if stage["kind"] == "topic_discovery":
            packet["topic_history"] = self._topic_history_context()
            # Topic intake is a sampling boundary, not a deterministic
            # transformation.  A bounded attempt can reject one portfolio
            # while the next attempt deliberately changes its seed and
            # candidate history.  If those attempts share the ordinary stage
            # cache key, a prior ``blocked`` projection can cancel the new
            # specialist review before the topic runner makes a call.
            packet["topic_exploration_boundary"] = {
                "continuation_cycle": self.continuation_cycles,
                "attempt_number": attempt_number,
            }
        elif stage["id"] in self.reopened_stage_ids and self.continuation_cycles:
            # A reopened assignment is a new Composer work boundary even when
            # it deliberately reuses an upstream durable store (notably the
            # survey ledger).  Include that boundary in the stage cache key so
            # a prior blocked envelope cannot short-circuit the scoped repair
            # before the runner gets a chance to use its retained evidence.
            packet["continuation_boundary"] = {
                "continuation_cycle": self.continuation_cycles,
                "attempt_number": attempt_number,
            }
        cache = ModelWorkCache(self.store, self._publish)
        key = cache.key(scope=f"stage:{stage['id']}", role=stage["kind"],
                        system="checked-stage-output-1", prompt=packet, model={})
        retained = cache.get(key)
        if retained and retained.get("status") == "blocked":
            # A scientific recovery cycle is a new execution boundary.  The
            # old stage cache records why the previous program was rejected,
            # but must not prevent `_execute_stage` from materializing the
            # cycle-specific repair requested by the Composer.  Without this
            # distinction the specialist pool can repeat forever while the
            # actual experiment runner is never reached.
            recovery_context = self.context.get(stage["id"])
            scientific_recovery = (
                stage["kind"] in {"survey", "experiment", "topic_discovery"}
                and stage["id"] in self.reopened_stage_ids
                and bool(self.continuation_cycles)
                and isinstance(recovery_context, dict)
                and (
                    recovery_context.get("review_status")
                    in {"scientific_assignment_blocked", "topic_budget_exhausted",
                        "survey_handoff_incomplete"}
                    or (stage["kind"] == "survey"
                        and recovery_context.get("status") in STAGE_HOLD_STATUSES
                        and (isinstance(recovery_context.get("failure_recovery"), dict)
                             or recovery_context.get("handoff_repair_required") is True
                             or recovery_context.get("research_requests")))
                    or stage["kind"] == "topic_discovery"
                )
            )
            format_recovery = (
                isinstance(recovery_context, dict)
                and recovery_context.get("format_recovery") is True
                and type(recovery_context.get("format_recovery_attempts")) is int
                and recovery_context.get("format_recovery_attempts") <= 1
            )
            if not scientific_recovery and not format_recovery:
                blocked = ModelWorkBlocked(retained["error"])
                if retained.get("failure_class") == "context_budget":
                    blocked.failure_class = "context_budget"
                    blocked.context_budget = deepcopy(retained.get("context_budget", {}))
                raise blocked
        if (retained and retained.get("status") == "succeeded"
                and Path(retained["result"]["output_path"]).is_file()
                and hashlib.sha256(Path(retained["result"]["output_path"]).read_bytes()).hexdigest()
                    == retained.get("output_sha256")):
            result = deepcopy(retained["result"])
            result.update(usage={}, reused_from=retained["cache_ref"])
            return result
        try:
            result = self._execute_stage(stage, attempt_number=attempt_number, specialist_reports=specialist_reports)
            self._raise_stage_failure(result)
            if result.get("status") not in STAGE_READY_STATUSES | STAGE_HOLD_STATUSES:
                error = ValidationError(result.get("error") or f"stage {stage['id']} did not complete: {result.get('status')}")
                error.usage = result.get("usage", {})
                raise error
        except Exception as exc:
            # A provider reset is a time boundary, not a failed scientific
            # revision. Scoped research holds likewise return work orders.
            from scisaurus.runtime.capability_foundry import CapabilityDeadlineError
            if isinstance(exc, (ProviderConfigurationError, ProviderCooldownError, CapabilityDeadlineError)) or (
                    isinstance(exc, ModelCallError) and exc.status_code == 429):
                raise
            if stage["kind"] == "topic_discovery":
                self._normalize_topic_intake_failure(exc, stage)
                # Topic intake is an exploration boundary. Its runner already
                # records candidate/rejection traces and the Composer owns
                # the typed pivot to a fresh direction. Treating a rejected
                # topic as unchanged model work creates a misleading
                # ``Unchanged topic input failed`` cache entry, strips the
                # retry metadata from the original error after one repair,
                # and can cancel a valid next intake before it starts.
                # Never cache a topic-stage failure as generic model work.
                raise
            if isinstance(exc, ModelContextBudgetError):
                failures = (retained or {}).get("failed_attempts", 0) + 1
                budget = {
                    key: deepcopy(getattr(exc, key, None)) for key in (
                        "model", "estimated_input_tokens", "allowed_input_tokens",
                        "context_window_tokens", "max_input_tokens",
                        "max_output_tokens", "image_count",
                    )
                }
                error = (
                    f"stage {stage['id']} input projection cannot fit the selected model: "
                    f"{exc}")
                cache.put(key, {
                    "status": "blocked", "failed_attempts": failures,
                    "failure_class": "context_budget", "context_budget": budget,
                    "error": error,
                })
                blocked = ModelWorkBlocked(error)
                blocked.failure_class = "context_budget"
                blocked.context_budget = budget
                blocked.usage = getattr(exc, "usage", {})
                raise blocked from exc
            failures = (retained or {}).get("failed_attempts", 0) + 1
            limit = ((descriptor.get("limits") or {}).get("max_rounds")
                     or self._retry_policy().get("max_attempts") or 3)
            exhausted = isinstance(exc, ModelWorkBlocked) or failures >= limit
            error = f"Unchanged {stage['id']} input failed {failures} time(s): {type(exc).__name__}: {exc}"
            cache.put(key, {"status": "blocked" if exhausted else "repairing",
                            "failed_attempts": failures, "error": error})
            if exhausted:
                blocked = ModelWorkBlocked(error)
                for attribute in ("research_argument", "research_review", "research_feedback",
                                  "research_response"):
                    value = getattr(exc, attribute, None)
                    if value is not None:
                        setattr(blocked, attribute, deepcopy(value))
                blocked.usage = getattr(exc, "usage", {})
                blocked.foundry_usage = deepcopy(getattr(exc, "foundry_usage", {}))
                stage_result = getattr(exc, "stage_result", None)
                if isinstance(stage_result, dict):
                    blocked.stage_result = deepcopy(stage_result)
                raise blocked from exc
            raise
        if result.get("status") in STAGE_READY_STATUSES:
            cache.put(key, {"status": "succeeded", "result": result,
                           "output_sha256": hashlib.sha256(Path(result["output_path"]).read_bytes()).hexdigest()})
        return result

    @staticmethod
    def _raise_stage_failure(result):
        """Preserve recoverability and resource accounting across runner boundaries."""
        failure = result.get("failure") or {}
        if failure.get("kind") == "provider_cooldown":
            error = ProviderCooldownError(result.get("error") or "stage provider is cooling down",
                retry_after_seconds=failure["retry_after_seconds"], rate_limit=failure.get("rate_limit"))
        elif failure.get("kind") == "provider_configuration":
            error = ProviderConfigurationError(
                result.get("error") or "stage provider configuration is unavailable",
                provider=failure.get("provider"),
                credential_env=failure.get("credential_env"),
            )
        elif failure.get("kind") == "unchanged_assignment_exhausted":
            error = ModelWorkBlocked(result.get("error") or "unchanged stage assignment exhausted")
            # The runner still returned its complete scientific envelope.  Do
            # not reduce an exhausted model-contract retry to a bare string;
            # the Composer's failure panel needs the observed refs and gate
            # state to issue a useful repair order.
            error.stage_result = deepcopy(result)
            error.failure_scope = "stage"
        elif failure.get("kind") == "process_interrupted":
            raise KeyboardInterrupt(result.get("error") or "stage process interrupted")
        else:
            status = result.get("status") if isinstance(result, dict) else None
            if status in STAGE_READY_STATUSES | STAGE_HOLD_STATUSES:
                return
            # Stage runners return their complete durable run record even when
            # the scientific output is unusable.  Raising a plain string here
            # discarded that record and made the Composer retry the same input
            # without first inspecting the observations, source, and checks.
            error = ValidationError(
                result.get("error") or f"stage returned non-admissible status: {status}")
            error.stage_result = deepcopy(result)
            error.failure_scope = "stage"
        error.usage = result.get("usage", {})
        raise error

    def _failure_program_snapshot(self, stage):
        """Read the exact generated program used by a failed experiment.

        The snapshot is evidence for the repair panel, not an executable
        instruction.  Capability descriptors pin both source files through
        ``environment_files``; only those pinned files are copied into the
        bounded dossier.
        """
        if not isinstance(stage, dict) or stage.get("kind") != "experiment":
            return []
        descriptors = []
        for context in self.context.values():
            if not isinstance(context, dict):
                continue
            generated = context.get("generated_capability")
            path = generated.get("descriptor_path") if isinstance(generated, dict) else None
            if isinstance(path, str):
                descriptors.append(Path(path))
        paths = []
        for descriptor_path in descriptors:
            try:
                descriptor = json.loads(descriptor_path.read_text())
            except (OSError, ValueError, TypeError):
                continue
            experiment = descriptor.get("experiment") if isinstance(descriptor, dict) else None
            if not isinstance(experiment, dict):
                continue
            for capability_name in ("execution", "validation"):
                capability = experiment.get(capability_name)
                if not isinstance(capability, dict):
                    continue
                for value in capability.get("environment_files", []):
                    if isinstance(value, str):
                        paths.append(Path(value))
                command = capability.get("client", {}).get("command", [])
                if isinstance(command, list):
                    paths.extend(Path(value) for value in command[1:]
                                  if isinstance(value, str) and value.endswith(".py"))
        snapshots = []
        seen = set()
        for path in paths:
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if str(resolved) in seen or not resolved.is_file():
                continue
            seen.add(str(resolved))
            try:
                body = resolved.read_bytes()
            except OSError:
                continue
            limit = 30000
            snapshots.append({
                "path": str(resolved),
                "sha256": hashlib.sha256(body).hexdigest(),
                "size_bytes": len(body),
                "source": body[:limit].decode("utf-8", "replace"),
                "source_truncated": len(body) > limit,
            })
            if len(snapshots) >= 4:
                break
        return snapshots

    def _experiment_ancestor_stage_id(self, stage):
        """Find the experiment scope that can produce evidence for a later repair."""
        if not isinstance(stage, dict):
            return None
        by_id = {
            item.get("id"): item for item in self.workflow.get("stages", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        pending = list(stage.get("depends_on", []))
        seen = set()
        while pending:
            stage_id = pending.pop()
            if stage_id in seen or stage_id not in by_id:
                continue
            seen.add(stage_id)
            candidate = by_id[stage_id]
            if candidate.get("kind") == "experiment":
                return stage_id
            pending.extend(candidate.get("depends_on", []))
        return None

    def _argument_experiment_repair_request(self, stage, dossier):
        """Route evidence-producing argument repairs back to Methods first.

        An argument adjudicator can identify a missing sensitivity sweep or
        independent recalculation, but Strategy cannot manufacture that
        observation from prose.  Keep the argument repair order and add one
        explicitly addressed Methods order so the existing experiment is
        repaired before the argument is adjudicated again.
        """
        if not isinstance(stage, dict) or stage.get("kind") != "argument":
            return None
        directives = dossier.get("review_directives", []) if isinstance(dossier, dict) else []
        texts = [
            item.get("text", "") for item in directives
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        directive_text = " ".join(texts)
        lowered = directive_text.casefold()
        if not directive_text or not any(marker in lowered for marker in ARGUMENT_EXPERIMENT_REPAIR_MARKERS):
            return None
        experiment_stage_id = self._experiment_ancestor_stage_id(stage)
        if experiment_stage_id is None:
            return None
        digest = str(dossier.get("input_sha256") or "")[:16]
        commands = deepcopy(dossier.get("repair_commands", []))
        checks = deepcopy(dossier.get("acceptance_checks", []))
        return {
            "id": f"repair-{stage.get('id')}-experiment-{digest}",
            "kind": "additional_experiment",
            "owner": "methods.validation",
            "target_stage_id": experiment_stage_id,
            "target_stage_kind": "experiment",
            "repair_priority": "immediate",
            "objective": (
                "Execute the evidence-producing repairs required by the argument review before "
                "rebuilding the claim-evidence graph: " + directive_text
            )[:1800],
            "why": (
                "The argument reviewer identified observations or recalculations that cannot be "
                "resolved by wording alone; Methods must produce or explicitly bound them first."
            ),
            "success_condition": (
                "A fresh raw result, deterministic validation, and independent recalculation address "
                "each evidence-producing directive, or record an explicit bounded limitation."
            ),
            "evidence_needed": (
                "The prior experiment result, exact review directives, executable source and inputs, "
                "fresh raw output, deterministic validation, and independent recalculation."
            ),
            "source_stage_id": stage.get("id"),
            "failure_dossier_ref": dossier.get("artifact_ref"),
            "failure_input_sha256": dossier.get("input_sha256"),
            "repair_commands": commands,
            "acceptance_checks": checks,
            "review_directives": deepcopy(directives),
            "recovery_mode": "repair_then_rerun",
        }

    def _format_contract_recovery_request(self, stage, context, dossier=None):
        """Build a same-stage order for a malformed model response.

        This is intentionally a ``recovery`` order rather than an
        ``additional_experiment`` order.  It changes routing, output framing,
        or compaction only; it must not be interpreted as a new scientific
        result or trigger a capability-design panel.
        """
        if not isinstance(stage, dict) or stage.get("kind") not in STAGE_KINDS:
            return None
        context = context if isinstance(context, dict) else {}
        recovery = context.get("failure_recovery")
        recovery = recovery if isinstance(recovery, dict) else {}
        digest = (
            (dossier or {}).get("input_sha256")
            if isinstance(dossier, dict) else None
        ) or recovery.get("input_sha256") or context.get("failure_input_sha256") or ""
        digest = re.sub(r"[^a-z0-9]", "", str(digest).casefold())[:16] or "undigested"
        safe_stage = re.sub(r"[^a-z0-9_.-]+", "-", str(stage["id"]).casefold()).strip("-")
        safe_stage = safe_stage[:24] or "stage"
        attempt = context.get("format_recovery_attempts", 1)
        if type(attempt) is not int or attempt < 1:
            attempt = 1
        try:
            owner = self.departments.stage_route(stage["kind"])["role"]
        except (AttributeError, KeyError, ValidationError):
            owner = STAGE_ROLES.get(stage["kind"], "executive-command.arbiter")
        return {
            "id": f"recovery-{safe_stage}-format-{digest}-{attempt}"[:64],
            "kind": "recovery",
            "owner": owner,
            "objective": (
                "Repair the model response contract for this exact stage: reroute the next call or "
                "use a compact schema-only prompt, then validate the complete JSON object locally. "
                "Preserve the scientific assignment and do not invent or release a result."
            ),
            "why": (
                "The previous provider response was unusable as JSON before the experiment produced "
                "an observation; forwarding it would create a false downstream input."
            ),
            "success_condition": (
                "The same stage returns a complete schema-valid object, or the Composer records an "
                "explicit model-contract blocker without admitting any downstream consumer."
            ),
            "evidence_needed": (
                "The failed response contract, exact stage input digest, bounded error, and a fresh "
                "locally validated response."
            ),
            "target_stage_id": stage["id"],
            "target_stage_kind": stage["kind"],
            "repair_priority": "immediate",
            "recovery_mode": "format_repair_then_rerun",
            "failure_dossier_ref": context.get("failure_dossier_ref") or recovery.get("dossier_ref"),
            "failure_input_sha256": digest,
            "repair_commands": deepcopy(
                context.get("repair_commands") or recovery.get("repair_commands") or []),
            "acceptance_checks": deepcopy(
                context.get("acceptance_checks") or recovery.get("acceptance_checks") or []),
        }

    def _record_failure_recovery(self, stage, attempt_stage, error, context,
                                 specialist_bundle, specialist_verifier,
                                 attempt_number):
        """Persist a failure dossier and replace blind retry with a repair order."""
        # The main scheduler clears its local ``context`` before dispatching a
        # stage. When that stage raises, the durable stage packet is therefore
        # the only place that contains prior format/capability repair counts.
        # Prefer it as the recovery base so a new continuation cannot reset a
        # bounded lease to zero on every failure.
        durable_context = self.context.get(stage.get("id")) if isinstance(stage, dict) else None
        if isinstance(durable_context, dict):
            recovered_base = deepcopy(durable_context)
            if isinstance(context, dict):
                # Keep fresh runner fields that are not already represented in
                # the durable packet; failure-specific result data is carried
                # separately through ``error.stage_result`` below.
                for key, value in context.items():
                    recovered_base.setdefault(key, deepcopy(value))
            context = recovered_base
        stage_result = getattr(error, "stage_result", None)
        if not isinstance(stage_result, dict) and isinstance(context, dict):
            stage_result = context
        dossier = build_failure_dossier(
            stage=stage, attempt_stage=attempt_stage, error=error,
            stage_result=stage_result,
            specialist_reports=(specialist_bundle or {}).get("reports", [])
            if isinstance(specialist_bundle, dict) else [],
            verifier=specialist_verifier,
            program_snapshot=self._failure_program_snapshot(stage),
            attempt_number=attempt_number,
        )
        # Resource fences still get a dossier for the ledger, but they do not
        # create a scientific work order or consume a repair call.
        dossier_record = self._publish(
            f"command/composer/failure-recovery/{stage['id']}/attempt-{attempt_number}",
            "report", dossier, "command.composer",
        )
        dossier["artifact_ref"] = dossier_record["artifact_ref"]
        if not dossier.get("recoverable") or dossier.get("failure_class") == "operational_recovery":
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "failure_dossier_recorded",
                "stage_id": stage["id"],
                "attempt_number": attempt_number,
                "failure_class": dossier.get("failure_class"),
                "dossier_ref": dossier["artifact_ref"],
                "next_action": dossier.get("next_action"),
            })
            return dossier

        if dossier.get("failure_class") == "model_contract":
            prior_attempts = 0
            if isinstance(context, dict) and type(context.get("format_recovery_attempts")) is int:
                prior_attempts = context["format_recovery_attempts"]
            recovery = {
                "schema_version": "failure-recovery-ledger-1",
                "failure_class": "model_contract",
                "dossier_ref": dossier["artifact_ref"],
                "input_sha256": dossier.get("input_sha256"),
                "repair_commands": deepcopy(dossier.get("repair_commands", [])),
                "acceptance_checks": deepcopy(dossier.get("acceptance_checks", [])),
                "review_directives": deepcopy(dossier.get("review_directives", [])),
                "model_diagnostics": deepcopy(dossier.get("model_diagnostics", {})),
                "recovery_mode": "format_repair_then_rerun",
                "requires_capability_repair": False,
                "attempt_number": attempt_number,
            }
            recovery_context = deepcopy(context) if isinstance(context, dict) else {}
            if isinstance(attempt_stage, dict) and isinstance(
                    attempt_stage.get("project_dir"), str):
                recovery_context.setdefault(
                    "project_dir", str(Path(attempt_stage["project_dir"]).resolve()))
            recovery_context.update({
                "stage_id": stage["id"],
                "kind": stage["kind"],
                "status": "format_recovery_required",
                "error": str(error)[:4096],
                "review_status": "model_contract_repair",
                "failure_recovery": recovery,
                "failure_dossier_ref": dossier["artifact_ref"],
                "repair_commands": deepcopy(dossier.get("repair_commands", [])),
                "acceptance_checks": deepcopy(dossier.get("acceptance_checks", [])),
                "review_directives": deepcopy(dossier.get("review_directives", [])),
                "model_diagnostics": deepcopy(dossier.get("model_diagnostics", {})),
                "research_requests": [],
                "format_recovery": True,
                "format_recovery_attempts": prior_attempts + 1,
            })
            format_request = self._format_contract_recovery_request(
                stage, recovery_context, dossier)
            if isinstance(format_request, dict):
                recovery_context["research_requests"] = [format_request]
            self.context[stage["id"]] = recovery_context
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "model_contract_repair_order_issued",
                "stage_id": stage["id"],
                "attempt_number": attempt_number,
                "failure_class": "model_contract",
                "dossier_ref": dossier["artifact_ref"],
                "repair_commands": deepcopy(dossier.get("repair_commands", [])),
                "acceptance_checks": deepcopy(dossier.get("acceptance_checks", [])),
                "next_action": "reroute_and_compact_before_scientific_recovery",
            })
            return dossier

        request = build_repair_request(dossier, stage_id=stage["id"])
        request["failure_dossier_ref"] = dossier["artifact_ref"]
        # A failed experiment is not automatically a failed program.  A
        # malformed specialist verdict should be repaired at the model/task
        # contract; a result-bearing failure, executor/validator failure, or
        # explicit foundry rejection deserves the expensive source-level
        # methods panel. This distinction keeps the controller from regenerating
        # code for every ordinary response-contract defect.
        requires_capability_repair = self._experiment_program_repair_required(
            stage, dossier, stage_result)
        recovery = {
            "schema_version": "failure-recovery-ledger-1",
            "failure_class": dossier.get("failure_class"),
            "dossier_ref": dossier["artifact_ref"],
            "input_sha256": dossier.get("input_sha256"),
            "repair_commands": deepcopy(dossier.get("repair_commands", [])),
            "acceptance_checks": deepcopy(dossier.get("acceptance_checks", [])),
            "review_directives": deepcopy(dossier.get("review_directives", [])),
            "model_diagnostics": deepcopy(dossier.get("model_diagnostics", {})),
            "recovery_mode": "repair_then_rerun",
            "requires_capability_repair": requires_capability_repair,
            "attempt_number": attempt_number,
        }
        recovery_context = deepcopy(context) if isinstance(context, dict) else {}
        if isinstance(attempt_stage, dict) and isinstance(
                attempt_stage.get("project_dir"), str):
            recovery_context.setdefault(
                "project_dir", str(Path(attempt_stage["project_dir"]).resolve()))
        requests = [request]
        experiment_request = self._argument_experiment_repair_request(stage, dossier)
        if experiment_request is not None:
            requests.append(experiment_request)
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "route_argument_evidence_repair_to_experiment",
                "stage_id": stage["id"],
                "target_stage_id": experiment_request["target_stage_id"],
                "directive_count": len(experiment_request.get("review_directives", [])),
                "next_action": "repair_experiment_before_argument_adjudication",
            })
        recovery_context.update({
            "stage_id": stage["id"],
            "kind": stage["kind"],
            "status": "research_expansion_required",
            "error": str(error)[:4096],
            "review_status": "scientific_assignment_blocked",
            "failure_recovery": recovery,
            "failure_dossier_ref": dossier["artifact_ref"],
                "failure_observed_result": deepcopy(dossier.get("observed_result", {})),
                "repair_commands": deepcopy(dossier.get("repair_commands", [])),
                "acceptance_checks": deepcopy(dossier.get("acceptance_checks", [])),
                "review_directives": deepcopy(dossier.get("review_directives", [])),
                "model_diagnostics": deepcopy(dossier.get("model_diagnostics", {})),
                "research_requests": requests,
        })
        if isinstance(stage_result, dict):
            # Preserve result-bearing fields so the next capability repair can
            # distinguish a bad program from a bad claim about a real result.
            for key in (
                    "results_package", "raw_results", "metrics", "findings", "assets",
                    "execution_refs", "deterministic_validation_ref", "model_review_refs",
                    "assessment_ref", "survey_ref", "nomination_ref", "survey_current",
                    "assessment_current", "gap_state", "topic_admission", "incumbent_ref",
                    "limitations", "analysis", "study_id", "run_id"):
                if key in stage_result:
                    recovery_context[key] = deepcopy(stage_result[key])
        recovery_context["failure_debt"] = {
            "stage_id": stage["id"], "kind": stage["kind"],
            "failure_class": dossier.get("failure_class"),
            "error": str(error)[:4096], "attempts": attempt_number,
            "next_action": "execute the recorded repair commands before rerunning this scope",
            "release_blocking": True,
            "failure_dossier_ref": dossier["artifact_ref"],
        }
        self.context[stage["id"]] = recovery_context
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "failure_analyzed_repair_order_issued",
            "stage_id": stage["id"],
            "attempt_number": attempt_number,
            "failure_class": dossier.get("failure_class"),
            "dossier_ref": dossier["artifact_ref"],
            "repair_order": deepcopy(request),
            "repair_commands": deepcopy(dossier.get("repair_commands", [])),
            "acceptance_checks": deepcopy(dossier.get("acceptance_checks", [])),
            "next_action": "repair_before_rerun",
        })
        return dossier

    @staticmethod
    def _experiment_program_repair_required(stage, dossier, stage_result=None):
        """Decide whether an experiment failure warrants source-level repair."""
        if not isinstance(stage, dict) or stage.get("kind") != "experiment":
            return False
        if not isinstance(dossier, dict) or dossier.get("failure_class") in {
                "resource_fence", "operational_recovery", "model_contract"}:
            return False
        if dossier.get("failure_class") == "experiment_failure":
            return True
        observed = stage_result if isinstance(stage_result, dict) else {}
        if any(observed.get(key) for key in (
                "execution_refs", "results_package", "raw_results",
                "deterministic_validation_ref", "assessment_ref", "metrics", "findings")):
            return True
        text = str(dossier.get("error", "")).casefold()
        return any(marker in text for marker in (
            "capability foundry", "executor", "validator", "estimand",
            "independent recalculation", "results-package", "program admission",
        ))

    def _execute_stage(self, stage, *, attempt_number=1, specialist_reports=None):
        """Dispatch one allowlisted specialist runner and return its context."""
        kind = stage["kind"]
        foundry_usage_before = deepcopy(self.foundry_usage) if kind == "experiment" else {}
        foundry_usage_delta = {}
        project_dir = Path(stage["project_dir"])
        output_path = None
        stage_deadline = min(float(stage["deadline_seconds"]), self._remaining())
        prior_run = (Path(stage["reuse_output_path"])
                     if stage["reuse_output_path"] is not None
                     else project_dir / "output" / "run.json")
        # A fresh free-topic mission must sample a new scholarly pool.  Topic
        # reuse is an explicit replay mode and is opt-in at workflow level;
        # other stages retain their existing checkpoint-reuse contract.
        topic_reuse_allowed = (kind != "topic_discovery"
                               or self.workflow.get("topic_reuse_allowed", False))
        if stage["reuse_completed"] and topic_reuse_allowed and prior_run.is_file():
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
                if (stage["kind"] == "topic_discovery" and "research_program" not in context
                        and isinstance(context.get("candidates"), list)
                        and isinstance(context.get("selected_id"), str)):
                    from scisaurus.runtime.research_program import build_research_program
                    context["research_program"] = build_research_program(context)
                self._publish(f"command/composer/reuse/{stage['id']}-{len(self.feedback) + 1}",
                              "decision_note", {
                                  "stage_id": stage["id"], "action": "reuse_completed_stage",
                                  "source_run": str(prior_run.resolve()), "status": prior.get("status"),
                              }, "command.composer")
                return context
        config = json.loads(Path(stage["config_path"]).read_text())
        config = self._adapt_continuation_config(stage, config)
        config = self._apply_stage_quota(config, stage)
        # The mission deadline governs total autonomy, not the number of
        # attempts spent repairing one unchanged model assignment.
        if kind in {"survey", "experiment"} and self._allows_provisional_progress():
            config.setdefault("limits", {})["repair_mode"] = "bounded"
        if (kind == "topic_discovery"
                and self._retry_policy().get("mode", "bounded") == "until_deadline"):
            # A descriptor may deliberately impose a tighter intake repair
            # budget.  Only inherit the workflow mode when the descriptor did
            # not state one; otherwise the generic Composer policy silently
            # defeats the topic quota.
            config.setdefault("repair_mode", "until_deadline")
        if stage["id"] in self.reopened_stage_ids and self.continuation_cycles:
            # A continuation must never overwrite its incumbent.  Specialist
            # runners create new output roots, so remap their configured
            # result path into the cycle namespace before dispatch.
            cycle_root = Path(stage["project_dir"]).resolve()
            if kind in {"interpretation", "argument"}:
                configured_output = Path(config["output_path"])
                config["output_path"] = str(cycle_root / configured_output.name)
            elif kind == "paper":
                configured_output = Path(config["output_dir"])
                config["output_dir"] = str(cycle_root / configured_output.name)
        if attempt_number > 1:
            attempt_root = Path(stage["project_dir"]).resolve()
            if kind in {"interpretation", "argument"}:
                output_path = Path(config["output_path"])
                config["output_path"] = str(attempt_root / output_path.name)
            elif kind == "paper":
                output_dir = Path(config["output_dir"])
                config["output_dir"] = str(attempt_root / output_dir.name)
        if kind == "topic_discovery":
            from scisaurus.runtime.topic_discovery import (
                TopicDiscoveryRunner, validate_topic_stage_config,
            )
            # Another fresh Composer mission may have recorded a direction
            # after this process was initialized.  Refresh the family memory
            # at the admission boundary so parallel missions see the latest
            # completed selection before they ask the model for a topic.
            self.topic_history = self._load_topic_history()
            descriptor = validate_topic_stage_config(config)
            model = json.loads(Path(descriptor["model_config_path"]).read_text())
            runner = TopicDiscoveryRunner(model, deadline_seconds=stage_deadline)
            maturity_rounds = descriptor.get("maturity_review_rounds", 0)
            # Journal-oriented free-topic missions target a research paper,
            # not an executable demo.  Give the intake an independent maturity
            # screen by default; ordinary legacy topic stages keep their
            # original one-pass contract unless they opt in explicitly.
            if maturity_rounds == 0 and (
                    self.workflow.get("experiment_catalog")
                    or any(item.get("kind") == "paper" for item in self.workflow["stages"])):
                maturity_rounds = 2
            try:
                continuation_scope = bool(
                    stage["id"] in self.reopened_stage_ids and self.continuation_cycles)
                configured_topic_budgets = (
                    descriptor.get("continuation_budgets")
                    if continuation_scope and descriptor.get("continuation_budgets")
                    else descriptor.get("budgets")
                )
                if not configured_topic_budgets:
                    configured_topic_budgets = (
                        DEFAULT_TOPIC_CONTINUATION_BUDGETS
                        if continuation_scope else DEFAULT_TOPIC_BUDGETS
                    )
                topic_budgets = self._topic_budgets_for_attempt(
                    stage["id"], configured_topic_budgets,
                    scope="continuation" if continuation_scope else "intake")
                topic_kwargs = dict(
                    objective=self.workflow["objective"],
                    candidate_count=descriptor["candidate_count"],
                    max_attempts=descriptor["max_attempts"],
                    repair_mode=descriptor.get("repair_mode", "bounded"),
                    runtime_context=self._runtime_context(model),
                    bibliography=descriptor.get("bibliography"),
                    budgets=topic_budgets,
                    sampling_seed=self._topic_sampling_seed(attempt_number=attempt_number),
                    maturity_review_rounds=maturity_rounds,
                    refinement_context=self._topic_refinement_context(stage),
                )
                if specialist_reports:
                    topic_kwargs["specialist_reports"] = deepcopy(specialist_reports)
                result = runner.run(
                    **topic_kwargs,
                )
            except ProviderCooldownError:
                raise
            except ValidationError as exc:
                # The bounded descriptor owns one isolated intake. A
                # scientifically rejected direction is still recoverable at
                # the Composer level, where the next attempt receives a fresh
                # sampling boundary and the remaining mission budget. Only
                # the topic runner's explicit evidence trace can authorize
                # that pivot; authentication/provider failures stay on the
                # ordinary provider/error policy instead.
                self._normalize_topic_intake_failure(exc, stage)
                topic_recoverable = (
                    stage.get("kind") == "topic_discovery"
                    and bool(getattr(exc, "topic_intake_recoverable", False))
                )
                topic_retry_reason = getattr(exc, "topic_retry_reason", None)
                if topic_recoverable and not isinstance(topic_retry_reason, str):
                    topic_retry_reason = (
                        "scientific_candidate_rejected"
                        if getattr(exc, "rejected_topic_history", [])
                        else "intake_contract_failure"
                    )
                if (descriptor.get("budgets")
                        and descriptor.get("repair_mode", "bounded") == "bounded"):
                    snapshot = getattr(exc, "topic_budget", {})
                    snapshot = snapshot if isinstance(snapshot, dict) else {}
                    quota_error = QuotaExceededError(
                        "topic discovery bounded intake exhausted after "
                        f"{max(1, len(getattr(exc, 'candidate_attempt_trace', [])))} "
                        "candidate attempt(s) "
                        f"(configured maximum {descriptor['max_attempts']}): {exc}",
                        dimension="topic_attempts", limit=descriptor["max_attempts"],
                        observed=descriptor["max_attempts"],
                        usage=snapshot.get("usage", {}),
                        diagnostics=snapshot.get("events", []),
                    )
                    setattr(quota_error, "retryable_topic_intake", topic_recoverable)
                    setattr(quota_error, "topic_retry_reason",
                            topic_retry_reason if topic_recoverable else "intake_failure")
                    setattr(quota_error, "topic_budget", snapshot)
                    # Preserve the bounded runner's scientific trace when the
                    # Composer wraps its validation error as a quota error.
                    # Without this handoff, the run kept provider events but
                    # lost which candidate and review caused each retry.
                    for attribute in ("candidate_attempt_trace", "maturity_review_history",
                                      "rejected_topic_history"):
                        value = getattr(exc, attribute, None)
                        if isinstance(value, list):
                            setattr(quota_error, attribute, deepcopy(value))
                    raise quota_error from exc
                if topic_recoverable:
                    # Autonomous topic contracts without a local attempt quota
                    # must still reach the Composer's typed retry path. A raw
                    # ValidationError otherwise looks like an ordinary stage
                    # failure and loses the runner's rejection class.
                    setattr(exc, "retryable_topic_intake", True)
                    setattr(exc, "topic_retry_reason", topic_retry_reason)
                raise
            output_path = Path(descriptor["output_path"])
            if attempt_number > 1 or stage["id"] in self.reopened_stage_ids:
                output_path = Path(stage["project_dir"]) / output_path.name
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if (result.get("schema_version") == "topic-discovery-1"
                    and isinstance(result.get("candidates"), list)
                    and isinstance(result.get("selected_id"), str)):
                from scisaurus.runtime.research_program import build_research_program
                try:
                    research_program = build_research_program(result)
                except ValidationError as exc:
                    # Keep the runner's bounded accounting when a downstream
                    # materializer catches a candidate-level contract issue.
                    # Without this handoff the Composer sees zero usage and
                    # loses the scientific retry class.
                    for attribute in (
                            "usage", "topic_budget", "candidate_attempt_trace",
                            "maturity_review_history", "rejected_topic_history"):
                        value = result.get(attribute)
                        if value is not None and not hasattr(exc, attribute):
                            setattr(exc, attribute, deepcopy(value))
                    self._normalize_topic_intake_failure(exc, stage)
                    raise
                research_program_path = output_path.parent / "research-program.json"
                research_program_path.write_bytes(canonical_bytes(research_program))
                result["research_program"] = research_program
                result["research_program_path"] = str(research_program_path.resolve())
            output_path.write_bytes(canonical_bytes(result))
        elif kind in {"survey", "experiment"}:
            if kind == "survey":
                config = self._apply_topic_to_survey_config(stage, config)
            elif kind == "experiment":
                config = self._apply_topic_to_experiment_config(
                    stage, config,
                    model_call_budget=self._foundry_model_call_budget(stage, specialist_reports))
                self._sync_foundry_usage()
                foundry_usage_delta = self._usage_delta(
                    self.foundry_usage, foundry_usage_before)
            config = self._apply_bindings(config, stage["bindings"])
            if kind == "experiment":
                config = self._ensure_journal_quality_contract(stage, config)
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
                project_has_checkpoint = (project_dir / "state" / "control.sqlite").is_file()
                prior = {}
                if prior_run.is_file():
                    try:
                        prior = json.loads(prior_run.read_text())
                    except (OSError, ValueError):
                        prior = {}
                durable_config = (
                    self._durable_stage_config(project_dir)
                    if project_has_checkpoint else None
                )
                # A continuation can be interrupted after the durable survey
                # store is created but before SurveyRunner writes run.json.
                # That is still a resumable run, not a new-directory request.
                # An empty crash-created scaffold is different: it has no
                # immutable runner input and must be initialized as a fresh
                # survey, not sent through ResumeController.
                resumable_checkpoint = (
                    project_has_checkpoint
                    and durable_config is not None
                    and (not prior_run.is_file()
                         or prior.get("status") in {"blocked", "paused", "running"})
                )
                provider_fallback = None
                if resumable_checkpoint:
                    # The durable runner input is authoritative for a
                    # resumed workspace.  Reapplying the current descriptor
                    # would replace the selected topic, project identity, or
                    # provider route and makes ResumeController reject the
                    # checkpoint.
                    config = durable_config
                    fallback = self.context.get(stage["id"], {}).get("provider_fallback")
                    if (isinstance(fallback, dict)
                            and fallback.get("mode") == "crossref_metadata"):
                        provider_fallback = "crossref_metadata"
                    scope = self._survey_resume_scope(prior)
                    resume_policy = {
                        # Composer owns autonomous recovery.  A process
                        # interruption cannot observe an in-flight model
                        # response, so reconcile it as one conservative
                        # model call before retrying the scoped survey.
                        "additional_seconds": min(
                            float(config["limits"]["wall_clock_seconds"]),
                            max(1.0, self._remaining()),
                        ),
                        "unknown_outcomes": {
                            "mode": "charge_and_retry",
                            "usage_per_attempt": {"model_calls": 1},
                        },
                        "source_changes": {"mode": "reopen", "reopen_scopes": [scope]},
                    }
                    self._publish(f"command/composer/recovery/{stage['id']}-{len(self.feedback) + 1}",
                                  "decision_note", {
                                      "stage_id": stage["id"], "action": "resume_scoped_stage",
                                      "scope": scope, "reason": prior.get("error", "recoverable stage blocker"),
                                      "additional_seconds": resume_policy["additional_seconds"],
                                  }, "command.composer")
                result = SurveyRunner(stage["project_dir"], config, on_progress=self._stage_progress(stage),
                                      resume_policy=resume_policy,
                                      provider_fallback=provider_fallback).run()
                result["usage"] = self._incremental_stage_usage(stage, result.get("usage", {}))
                self._raise_stage_failure(result)
            else:
                from scisaurus.runtime.experiment import ExperimentRunner
                try:
                    result = ExperimentRunner(stage["project_dir"], config,
                                              on_progress=self._stage_progress(stage)).run()
                except Exception as exc:
                    self._sync_foundry_usage()
                    setattr(exc, "foundry_usage", self._usage_delta(
                        self.foundry_usage, foundry_usage_before))
                    raise
                result["usage"] = self._incremental_stage_usage(stage, result.get("usage", {}))
            if kind == "experiment":
                result["foundry_usage"] = deepcopy(foundry_usage_delta)
            if kind == "survey":
                gated = self._gate_free_topic_survey(result, stage=stage)
                if gated is not result:
                    # Preserve the SurveyRunner's own output and write the
                    # Composer admission decision beside it so a process
                    # interruption can restore the hold and its work orders
                    # without relying on in-memory state.
                    gated_path = Path(stage["project_dir"]) / "output" / "composer-gated-run.json"
                    gated_path.parent.mkdir(parents=True, exist_ok=True)
                    gated_path.write_bytes(canonical_bytes(gated))
                    result = gated
                    output_path = gated_path
                else:
                    result = gated
            if output_path is None:
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
            packet = self._project_continuation_requests(packet, stage)
            packet = self._attach_topic_program(packet)
            for binding in stage["bindings"]:
                if binding["target"].startswith("packet."):
                    value = self._source_value(binding["source"])
                    if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                        value = json.loads(Path(value).read_text())
                    _set_path(packet, binding["target"][len("packet."):], value)
            self._synchronize_research_packet_identity(packet)
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
            # Argument repair orders are scientific work orders, not merely
            # Composer ledger entries.  Put them into the fresh evidence
            # packet before the bound experiment and interpretation artifacts
            # are applied so the adjudicator can act on the exact requested
            # repair instead of replaying the old argument.
            packet = self._project_continuation_requests(packet, stage)
            packet = self._attach_topic_program(packet)
            for binding in stage["bindings"]:
                if binding["target"].startswith("packet."):
                    value = self._source_value(binding["source"])
                    if isinstance(value, str) and Path(value).is_file() and binding["target"].endswith("results_package"):
                        value = json.loads(Path(value).read_text())
                    _set_path(packet, binding["target"][len("packet."):], value)
            self._synchronize_research_packet_identity(packet)
            model = json.loads(model_path.read_text())
            minimums = self._argument_minimums(stage["id"], config)
            result = ResearchArgumentRunner(model, deadline_seconds=stage_deadline).run(
                argument_evidence_packet(packet), min_figures=minimums["figures"],
                min_tables=minimums["tables"], min_experiments=minimums["experiments"])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(canonical_bytes(result))
            result = {"status": "completed", "output_path": str(output_path.resolve()),
                      "argument_package": result, "argument": result.get("argument")}
        elif kind == "paper" and config.get("schema_version") == "review-article-config-1":
            from scisaurus.runtime.review_article import ReviewArticleRunner
            config = self._apply_bindings(config, stage["bindings"])
            config["work_orders"] = self._follow_up_projection(self._requests_for_stage(stage))
            result = ReviewArticleRunner(
                config, retained_work_dir=self.root / "retained-review-work" / stage["id"],
                deadline_seconds=stage_deadline,
                on_progress=lambda event: self._record_internal_feedback(stage, event),
            ).run()
            output_path = Path(result["output_path"])
        else:  # empirical paper
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
                        "review_arbiter_enabled", "model_concurrency",
                        "research_redteam_deadline_seconds", "research_redteam_max_attempts"}
            if set(config) - required - optional or not required.issubset(config):
                raise ValidationError(
                    f"paper descriptor requires {sorted(required)} and permits {sorted(optional)}")
            if "release_on_review_limit" in config and type(config["release_on_review_limit"]) is not bool:
                raise ValidationError("paper descriptor release_on_review_limit must be Boolean")
            if "review_arbiter_enabled" in config and type(config["review_arbiter_enabled"]) is not bool:
                raise ValidationError("paper descriptor review_arbiter_enabled must be Boolean")
            if "research_redteam_deadline_seconds" in config:
                value = config["research_redteam_deadline_seconds"]
                if (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                    raise ValidationError(
                        "paper descriptor research_redteam_deadline_seconds must be finite and positive")
            if "research_redteam_max_attempts" in config:
                value = config["research_redteam_max_attempts"]
                if type(value) is not int or not 1 <= value <= 8:
                    raise ValidationError(
                        "paper descriptor research_redteam_max_attempts must be an integer from 1 to 8")
            packet = json.loads(Path(config["packet_path"]).read_text())
            packet = self._project_continuation_requests(packet, stage)
            packet = self._attach_topic_program(packet)
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
            self._synchronize_research_packet_identity(packet)
            # The paper descriptor is often prepared before discovery.  Once
            # the current survey has been accepted, project its actual source
            # set into the fresh paper input on the first pass as well as on a
            # continuation; otherwise the depth gate would inspect stale
            # references from an earlier mission.
            if "survey" in self.context:
                paper_config = self._synchronize_paper_references(paper_config)
            argument_package_path = config["argument_package_path"]
            argument_package = json.loads(Path(argument_package_path).read_text()) if argument_package_path else None
            paper_config = self._synchronize_paper_figure_arguments(
                paper_config, packet, argument_package)
            paper_config, packet = self._synchronize_catalog_paper_inputs(
                paper_config, packet, argument_package)
            config = self._extend_paper_images(config, packet, paper_config)
            runner = PaperPipelineRunner(packet=packet, model_config=model, paper_config=paper_config,
                output_dir=config["output_dir"], image_paths=config["images"],
                max_review_rounds=config["max_review_rounds"],
                review_deadline_seconds=min(config["review_deadline_seconds"], stage_deadline),
                pipeline_deadline_seconds=min(config["pipeline_deadline_seconds"], stage_deadline),
                argument_deadline_seconds=min(config["argument_deadline_seconds"], stage_deadline),
                review_max_output_tokens=config.get("review_max_output_tokens"),
                review_reasoning_effort=config.get("review_reasoning_effort", "xhigh"),
                review_call_timeout_seconds=min(config.get("review_call_timeout_seconds", 300.0), stage_deadline),
                review_inter_request_interval_seconds=config.get("review_inter_request_interval_seconds", 0.5),
                repair_max_output_tokens=config.get("repair_max_output_tokens"),
                model_call_timeout_seconds=min(config.get("model_call_timeout_seconds", 300.0), stage_deadline),
                model_concurrency=config.get("model_concurrency", 1),
                review_arbiter_enabled=bool(config.get("review_arbiter_enabled", True)),
                research_redteam_deadline_seconds=min(
                    config.get("research_redteam_deadline_seconds", 900.0), stage_deadline),
                research_redteam_max_attempts=config.get("research_redteam_max_attempts", 3),
                draft=draft, initial_review_package=initial_review_package,
                release_on_review_limit=bool(config.get("release_on_review_limit", False)),
                draft_before_research_review=self._allows_provisional_progress(),
                retained_work_dir=self.root / "retained-paper-work" / stage["id"],
                deferred_requirements=[{
                    "stage_id": key, "findings": value.get("deferred_review_findings"),
                    "requirements": value.get("carried_maturity_requirements", []),
                } for key, value in self.context.items() if isinstance(value, dict)
                   and (value.get("deferred_review_findings") or value.get("carried_maturity_requirements"))],
                initial_argument_package=argument_package,
                min_argument_figures=(0 if self._allows_provisional_progress() else config["min_argument_figures"]),
                min_argument_tables=(0 if self._allows_provisional_progress() else config["min_argument_tables"]),
                min_argument_experiments=(1 if self._allows_provisional_progress() else config["min_argument_experiments"]),
                feedback_callback=lambda event: self._record_internal_feedback(stage, event))
            result = runner.run()
            output_path = Path(result["pdf"] or result.get("preflight_path")
                               or result.get("pipeline_result_path") or config["output_dir"])
        if not isinstance(result, dict):
            raise ValidationError(f"{kind} stage returned a non-object result")
        context = {**result, "stage_id": stage["id"], "kind": kind,
                   "project_dir": str(Path(stage["project_dir"]).resolve()),
                   "output_path": str(output_path.resolve())}
        if kind == "experiment":
            context["results_package"] = result.get("results_package")
            # ``_apply_topic_to_experiment_config`` increments the repair
            # lineage before the foundry call. Stage runners return their own
            # result envelope, so explicitly carry the lineage fields into
            # that envelope; otherwise the next failed attempt is mistaken
            # for the first repair and regenerates the same capability.
            incumbent = self.context.get(stage["id"])
            if isinstance(incumbent, dict):
                for key in (
                        "capability_repair_attempts",
                        "experiment_repair_history",
                        "experiment_repair_plan",
                        "capability_repair_panel",
                ):
                    if key in incumbent:
                        context[key] = deepcopy(incumbent[key])
        if kind == "argument":
            context["argument_package_path"] = context["output_path"]
        return context

    def _stage_progress(self, stage):
        def callback(state):
            self._checkpoint(f"{stage['id']}:{state.get('phase', 'progress')}")
        return callback

    def _record_feedback(self, stage, context):
        route = self.departments.stage_route(stage["kind"])
        assignment = self.stage_records.get(stage["id"], {})
        action = ("advance" if context.get("status") in {"completed", "accepted", "candidate_needs_review"}
                  else "reconcile_blocker")
        if context.get("status") == "research_expansion_required":
            action = "request_research_expansion"
        elif context.get("status") == "review_rejected":
            action = "editor_rejected"
        if action == "advance":
            consumers = [candidate for candidate in self.workflow["stages"]
                         if stage["id"] in candidate["depends_on"]]
            recipient = (self._address_for_role(STAGE_ROLES[consumers[0]["kind"]])
                         if consumers else COMMAND_ADDRESSES["intent"])
        else:
            recipient = COMMAND_ADDRESSES["arbiter"]
            if action == "request_research_expansion":
                requests = context.get("research_expansion_requests", [])
                owners = {item.get("owner") for item in requests if isinstance(item, dict)}
                if len(owners) == 1 and next(iter(owners)):
                    recipient = self._address_for_role(next(iter(owners)))
            elif action == "editor_rejected":
                recipient = self._address_for_role("editorial.composer")
        feedback = {
            "stage_id": stage["id"], "role": route["role"],
            "department": route["department"],
            "owner_agent": route["chief"],
            "adversary_agent": route["adversary"],
            "required_agents": deepcopy(assignment.get("required_agents", route["required_agents"])),
            "active_agents": deepcopy(assignment.get(
                "active_agents", assignment.get("last_active_agents", []))),
            "verifier_agent": assignment.get("verifier_agent", route["verifier_agent"]),
            "chief_synthesis_ref": assignment.get("chief_synthesis_ref"),
            "verifier_artifact_ref": assignment.get("verifier_artifact_ref"),
            "from": COMMAND_ADDRESSES["progress"], "to": recipient,
            "status": context.get("status"), "output_path": context.get("output_path"),
            "scientific_state": context.get("gap_state") or context.get("review_status") or context.get("argument_status"),
            "research_expansion_requests": deepcopy(context.get("research_expansion_requests", [])),
            "action": action,
            "stage_deadline_seconds": stage["deadline_seconds"],
            "elapsed_seconds": context.get("elapsed_seconds"),
            "dependencies": list(stage["depends_on"]),
            "next_condition": "all declared dependencies current and stage output independently checked",
        }
        if context.get("status") == "research_expansion_required":
            feedback["next_condition"] = "complete every scoped literature, experiment, or analysis request and re-run the admission gate"
        elif context.get("status") == "review_rejected":
            feedback["next_condition"] = "reopen the rejected research or manuscript scope and obtain a new three-stage editorial decision"
        elif context.get("status") not in {"completed", "accepted", "candidate_needs_review"}:
            feedback["next_condition"] = "reconcile the stage blocker before downstream admission"
        elif context.get("status") == "candidate_needs_review":
            feedback["next_condition"] = "principal review is required before external submission"
        feedback["message_id"] = f"composer-feedback-{uuid.uuid4().hex}"
        feedback["message_disposition"] = "scheduled" if action == "advance" else "escalated"
        self.feedback.append(feedback)
        note = self._publish(f"command/composer/feedback/{stage['id']}-{len(self.feedback)}", "decision_note", feedback,
                             "command.composer")
        self._route_feedback(feedback, note)

    def _feedback_address(self, event):
        """Choose the receiving organ for an internal specialist exchange."""
        kind = event.get("kind")
        reviewer = event.get("reviewer_id")
        if kind in {"review_failure", "synthesis_failure", "arbitration_failure", "repair_failure"}:
            return COMMAND_ADDRESSES["arbiter"]
        if kind == "research_gate":
            owners = {item.get("owner") for item in event.get("expansion_requests", [])
                      if isinstance(item, dict)}
            if len(owners) == 1 and next(iter(owners)):
                return self._address_for_role(next(iter(owners)))
            if event.get("decision") in {"proceed", "accept"}:
                return self._address_for_role("editorial.composer")
            return COMMAND_ADDRESSES["arbiter"]
        if kind == "editor_decision":
            return self._address_for_role("editorial.composer")
        if kind == "review":
            # A blocking critique is a dispute for the Arbiter; ordinary
            # findings remain with the department that owns the surface.
            if (event.get("severity_counts") or {}).get("blocking", 0):
                return COMMAND_ADDRESSES["arbiter"]
            if reviewer == "science":
                return self._address_for_role("strategy.interpretation")
            if reviewer == "methods":
                return self._address_for_role("methods.validation")
            if reviewer == "journal_editor":
                # Publication-depth findings are editorial admission
                # decisions.  They must reach the editor-in-chief rather than
                # being treated as an ordinary copy edit.
                return self._address_for_role("editorial.composer")
            return self._address_for_role("editorial.composer")
        if kind in {"argument", "draft", "repair"}:
            return self._address_for_role("editorial.composer")
        if kind == "release":
            return COMMAND_ADDRESSES["intent"]
        if kind == "synthesis":
            return (COMMAND_ADDRESSES["arbiter"]
                    if event.get("decision") == "insufficient_evidence"
                    else self._address_for_role("editorial.composer"))
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
        # PaperPipelineRunner deliberately uses semantic event IDs so a
        # replay inside one pass is idempotent.  A reopened paper is a new
        # scientific pass, so namespace those IDs by Composer cycle while
        # preserving same-cycle deduplication and resume stability.
        # ArtifactRef logical names cannot contain a colon.  Normalize model
        # supplied IDs once at the control boundary and retain a short digest
        # when normalization changed their identity.
        safe_event_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", event_id).strip("-")
        if safe_event_id != event_id:
            safe_event_id = f"{safe_event_id[:72]}-{hashlib.sha256(event_id.encode('utf-8')).hexdigest()[:8]}"
        event_id = safe_event_id or f"{stage['id']}-{hashlib.sha256(canonical_bytes(event)).hexdigest()[:24]}"
        if (self.continuation_cycles and stage["id"] in self.reopened_stage_ids
                and not event_id.startswith(f"cycle-{self.continuation_cycles}-")):
            event_id = f"cycle-{self.continuation_cycles}-{event_id}"
        if any(item.get("event_id") == event_id for item in self.feedback):
            return
        status = event.get("status", "needs_revision")
        failure = event.get("kind", "").endswith("failure") or status == "blocked"
        recipient = self._feedback_address(event)
        route = self.departments.stage_route(stage["kind"])
        internal_role = None
        reviewer_id = event.get("reviewer_id")
        if reviewer_id == "science":
            internal_role = "review.science"
        elif reviewer_id == "methods":
            internal_role = "review.methods"
        elif reviewer_id == "journal_editor":
            internal_role = "review.journal_editor"
        elif event.get("kind") in {"argument", "draft"}:
            internal_role = "strategy.argument"
        elif event.get("kind") == "release":
            internal_role = "editorial.writer"
        internal_assignment = (self.departments.role_for_internal_role(internal_role)
                               if internal_role else None)
        stage_assignment = self.stage_records.get(stage["id"], {})
        action = ("reconcile_blocker" if failure else
                  "request_research_expansion" if status == "research_expansion_required" else
                  "editor_rejected" if status == "rejected" else
                  "request_revision" if status in {"needs_revision", "revise"} else
                  "verify_repair" if event.get("kind") == "repair" else
                  "propose_release" if event.get("kind") == "release" else
                  "record_observation")
        feedback = {
            "event_id": event_id,
            "stage_id": stage["id"],
            "role": STAGE_ROLES[stage["kind"]],
            "department": route["department"],
            "owner_agent": route["chief"],
            "adversary_agent": route["adversary"],
            "required_agents": deepcopy(stage_assignment.get("required_agents", route["required_agents"])),
            "active_agents": deepcopy(stage_assignment.get("active_agents", [])),
            "verifier_agent": stage_assignment.get("verifier_agent", route["verifier_agent"]),
            "internal_role": internal_role,
            "internal_agent": ({"dept": internal_assignment["department"], "agent": internal_assignment["agent"]}
                                if internal_assignment else None),
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
        if status == "research_expansion_required":
            feedback["next_condition"] = (
                "Complete every requested literature, experiment, or interpretation workstream, then re-admit the paper stage."
            )
        elif status == "rejected":
            feedback["next_condition"] = (
                "Reopen the rejected research scope and obtain a new three-stage reviewer and editor decision."
            )
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
        # Persist the receiving department's inbox and any typed work orders
        # before acknowledging the envelope.  This makes a handoff an actual
        # organizational operation rather than metadata attached only to the
        # Composer's own ledger.
        self.departments.receive_message(message_id, feedback, note["artifact_ref"])
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
        route = self.departments.stage_route(stage["kind"])
        assignment = self.stage_records.get(stage["id"], {})
        feedback = {
            "stage_id": stage["id"], "role": route["role"],
            "department": route["department"],
            "owner_agent": route["chief"],
            "adversary_agent": route["adversary"],
            "required_agents": deepcopy(assignment.get("required_agents", route["required_agents"])),
            "active_agents": deepcopy(assignment.get(
                "active_agents", assignment.get("last_active_agents", []))),
            "verifier_agent": assignment.get("verifier_agent", route["verifier_agent"]),
            "chief_synthesis_ref": assignment.get("chief_synthesis_ref"),
            "verifier_artifact_ref": assignment.get("verifier_artifact_ref"),
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

    def _resolve_terminal_stage_work_orders(self, stage):
        """Close scoped work orders when their owning stage is terminally blocked.

        A stage failure can happen before its normal success path reaches the
        work-order resolver. Leaving the request running then makes the
        dashboard claim that a repair is active after the Composer has already
        stopped. Holds intentionally bypass this helper and remain running for
        the next continuation cycle.
        """
        if not self.active_research_requests:
            return []
        resolved = self.departments.resolve_work_orders(
            self.active_research_requests, stage_kind=stage["kind"], outcome="blocked")
        if resolved:
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "resolve_work_orders",
                "stage_id": stage["id"],
                "outcome": "blocked",
                "work_orders": resolved,
            })
        return resolved

    def _pivot_topic_after_scientific_blocker(self, stage, context, completed, by_id,
                                              *, reason, objective, why):
        """Open one fresh topic direction for a non-productive stage blocker."""
        pending = list(stage.get("depends_on", []))
        seen = set()
        topic_stage_id = None
        while pending:
            candidate_id = pending.pop()
            if candidate_id in seen or candidate_id not in by_id:
                continue
            seen.add(candidate_id)
            candidate_stage = by_id[candidate_id]
            if candidate_stage.get("kind") == "topic_discovery":
                topic_stage_id = candidate_id
                break
            pending.extend(candidate_stage.get("depends_on", []))
        if topic_stage_id is None:
            return False
        topic_context = self.context.get(topic_stage_id)
        topic_context = deepcopy(topic_context) if isinstance(topic_context, dict) else {
            "kind": "topic_discovery"
        }
        pivot_cycle = self.continuation_cycles + 1
        topic_context["status"] = "research_expansion_required"
        topic_context["topic_pivot"] = {
            "status": "required", "reason": reason,
            "source_stage_id": stage["id"], "cycle": pivot_cycle,
        }
        topic_context.pop("generated_capability", None)
        topic_context["research_expansion_requests"] = [{
            "id": f"auto-topic-pivot-{pivot_cycle}",
            "kind": "topic_refinement",
            "owner": "research.intelligence",
            "objective": objective,
            "why": why,
            "success_condition": (
                "A new topic survives maturity and adversarial review, passes the bounded literature "
                "gate, and has an executable primary estimand."
            ),
            "evidence_needed": (
                "The failed stage's concrete checks, retained evidence boundary, a materially changed "
                "research question, and a bounded execution plan."
            ),
        }]
        self.context[topic_stage_id] = topic_context
        context["research_expansion_requests"] = []
        context["research_requests"] = []
        context["pivoted_to_topic"] = topic_stage_id
        if stage.get("kind") in {"survey", "experiment"}:
            # A topic pivot starts a new scientific lineage. Keep the prior
            # failure reference for auditability, but do not let its repair
            # counter, model-contract gate, or failure debt become the input
            # contract for the newly sampled topic.
            superseded = {}
            for key in (
                    "failure_recovery", "failure_debt", "failure_dossier_ref",
                    "repair_commands", "acceptance_checks", "review_directives",
                    "model_diagnostics", "format_recovery", "format_recovery_attempts",
                    "capability_repair_attempts", "experiment_repair_plan",
                    "experiment_repair_history", "capability_repair_panel",
            ):
                if key in context:
                    superseded[key] = deepcopy(context.pop(key))
            if superseded:
                context["superseded_scope_failure"] = {
                    "source_stage_id": stage.get("id"),
                    "pivot_cycle": pivot_cycle,
                    "reason": reason,
                    "failure_dossier_ref": superseded.get("failure_dossier_ref")
                    or (superseded.get("failure_recovery") or {}).get("dossier_ref"),
                    "failure_class": (superseded.get("failure_recovery") or {}).get(
                        "failure_class") if isinstance(superseded.get("failure_recovery"), dict) else None,
                }
            context["status"] = "research_expansion_required"
            context["review_status"] = "topic_pivot_pending"
            context["error"] = reason
        self.context[stage["id"]] = context
        if not self._begin_continuation(completed, by_id):
            return False
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "pivot_topic_after_scientific_blocker",
            "stage_id": stage["id"], "topic_stage_id": topic_stage_id,
            "reason": reason,
        })
        return True

    def _reopen_blocked_checkpoint(self, completed, by_id):
        """Turn a recoverable stop checkpoint into one fresh work cycle.

        The Composer normally admits recovery in the same process that sees a
        stage failure. A crash, manual restart, or an older runtime can leave
        only the durable ``blocked`` projection behind. Without this bridge,
        the next ``--resume`` merely redispatches or reports the same stale
        blocker. This method is deliberately conservative: environmental
        quota/deadline/cooldown and unknown outcomes are left for their own
        policies, while scientific assignment failures get a new scoped order.
        """
        for stage in self.workflow.get("stages", []):
            stage_id = stage.get("id")
            record = self.stage_records.get(stage_id, {})
            if not isinstance(record, dict) or record.get("status") != "blocked":
                continue
            error_text = str(record.get("error") or "")
            lowered = error_text.casefold()
            if any(token in lowered for token in (
                    "hard deadline", "provider cooldown", "result_unknown",
                    "unknown_external_outcome", "process_interrupted")):
                continue
            if "quotaexceedederror" in lowered and not (
                    stage.get("kind") == "topic_discovery"
                    and "topic discovery quota exhausted" in lowered):
                continue
            if stage.get("kind") == "topic_discovery" and (
                    "topic discovery quota exhausted" in lowered):
                error = QuotaExceededError(
                    error_text or "topic discovery quota exhausted",
                    dimension="model_calls", limit=0, observed=0,
                    usage={}, diagnostics=[{"kind": "composer_topic_budget"}],
                )
                error.topic_budget_scope = "restored_checkpoint"
                error.usage_is_snapshot = True
            else:
                error = ModelWorkBlocked(error_text or "recoverable stage assignment blocker")
            if self._admit_scientific_blocker_recovery(
                    stage, error, completed, by_id):
                record["status"] = "retrying"
                record["recovery_admitted"] = True
                return True
        return False

    @staticmethod
    def _is_survey_evidence_contract_blocker(stage, error):
        """Identify a persisted survey assignment that cannot be repaired in place."""
        return (
            isinstance(stage, dict)
            and stage.get("kind") == "survey"
            and isinstance(error, (ModelWorkBlocked, str))
            and any(marker in str(error).casefold() for marker in (
                "did not satisfy its evidence contract",
                "gap-assessment",
                "unknown or duplicate required check",
            ))
        )

    def _resume_stale_survey_contract_pivot(self, completed, by_id):
        """Convert an interrupted pre-pivot survey hold into its topic pivot.

        A process can stop after the Composer has admitted a continuation but
        before that continuation reaches the stage failure handler.  In that
        window the stage record is ``running`` even though its durable context
        already contains the prior evidence-contract blocker.  Replaying the
        continuation would bypass the new blocker classification and spend the
        same survey budget again, so reconcile that durable context before the
        dependency scheduler admits the stage.
        """
        if any(isinstance(item, dict) and item.get("kind") == "topic_refinement"
               for item in self.active_research_requests):
            return False
        for stage in self.workflow.get("stages", []):
            stage_id = stage.get("id")
            context = self.context.get(stage_id)
            record = self.stage_records.get(stage_id, {})
            if not isinstance(context, dict) or not isinstance(record, dict):
                continue
            if context.get("status") != "research_expansion_required":
                continue
            if stage_id not in self.continuation_pending_stage_ids:
                continue
            if not self._is_survey_evidence_contract_blocker(stage, context.get("error")):
                continue
            record["status"] = "retrying"
            record["error"] = str(context.get("error"))[:4096]
            self.stage_records[stage_id] = record
            return self._pivot_topic_after_scientific_blocker(
                stage, deepcopy(context), completed, by_id,
                reason="the literature evidence contract was exhausted without a valid gap assessment",
                objective=(
                    "Generate a materially different, source-grounded computational question whose "
                    "literature evidence contract is bounded enough to complete without repeating "
                    "an invalid gap assessment."
                ),
                why=(
                    "The prior survey exhausted its bounded evidence-contract repairs before the "
                    "process stopped; replaying that same assessment packet would not add evidence."
                ),
            )
        return False

    def _resume_exhausted_pre_execution_experiment(self, completed, by_id):
        """Pivot a legacy unexecuted experiment before redispatching it.

        Older checkpoints did not persist the capability-repair counter and
        could therefore resume directly into the same generated program. The
        durable failure ledger is enough to identify that state; reconcile it
        at startup so a process restart cannot spend one more specialist and
        foundry pass before the normal failure handler notices the loop.
        """
        if any(
                isinstance(item, dict)
                and item.get("kind") == "topic_refinement"
                for item in self.active_research_requests
        ):
            return False
        for stage in self.workflow.get("stages", []):
            if stage.get("kind") != "experiment":
                continue
            stage_id = stage.get("id")
            context = self.context.get(stage_id)
            record = self.stage_records.get(stage_id, {})
            if not isinstance(context, dict) or not isinstance(record, dict):
                continue
            if context.get("pivoted_to_topic"):
                # Migrate checkpoints written before topic pivots retired the
                # old lineage. The pivot marker is durable, but older packets
                # may still carry the failed capability gate alongside it.
                stale_keys = (
                    "failure_recovery", "failure_debt", "failure_dossier_ref",
                    "repair_commands", "acceptance_checks", "review_directives",
                    "model_diagnostics", "format_recovery", "format_recovery_attempts",
                    "capability_repair_attempts", "experiment_repair_plan",
                    "experiment_repair_history", "capability_repair_panel",
                )
                if any(key in context for key in stale_keys):
                    for key in stale_keys:
                        context.pop(key, None)
                    context["status"] = "research_expansion_required"
                    context["review_status"] = "topic_pivot_pending"
                    context["error"] = "legacy topic pivot lineage retired before new topic execution"
                    self.context[stage_id] = context
                    self.department_activity.append({
                        "cycle": self.continuation_cycles,
                        "action": "migrate_topic_pivot_lineage",
                        "stage_id": stage_id,
                        "next_action": "execute the new topic lineage without the superseded repair gate",
                    })
                continue
            if not self._is_pre_execution_capability_failure(
                    stage, context, record.get("error")):
                continue
            repair_attempts = self._pre_execution_repair_count(
                stage, error=record.get("error"))
            if repair_attempts < PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT:
                continue
            if not self._pivot_topic_after_scientific_blocker(
                    stage, deepcopy(context), completed, by_id,
                    reason=(
                        "a legacy unexecuted experiment already exhausted "
                        f"{PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT} capability repairs"
                    ),
                    objective=(
                        "Choose a materially different computational research direction or executable "
                        "mechanism, retaining the prior failure dossier as negative evidence."
                    ),
                    why=(
                        "The restored checkpoint has no observed experiment result and its prior "
                        "capability lineage is exhausted; redispatching it would repeat the same failure."
                    )):
                return False
            record["status"] = "retrying"
            record["recovery_admitted"] = True
            self.stage_records[stage_id] = record
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "resume_pivot_legacy_experiment_repair_loop",
                "stage_id": stage_id,
                "repair_attempts": repair_attempts,
                "repair_limit": PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT,
                "next_action": "sample a fresh topic/design branch",
            })
            return True
        return False

    def _admit_scientific_blocker_recovery(self, stage, error, completed, by_id,
                                           specialist_verifier=None):
        """Turn a bounded scientific failure into a fresh scoped work order.

        ``ModelWorkBlocked`` means the current assignment exhausted its own
        validation/repair contract. A Composer-owned topic envelope is the
        same kind of bounded scientific failure: preserve that cycle, change
        the research direction, and reopen only the affected dependency
        closure. Provider/model quotas, deadlines, and process interruptions
        remain environmental fences and never enter this path.
        """
        if getattr(error, "failure_class", None) == "context_budget":
            # The smallest role-specific packet already failed local
            # admission. Reopening the same scientific scope cannot add
            # evidence and would only replay the deterministic failure.
            return False
        prior_context = self.context.get(stage["id"])
        prior_context = deepcopy(prior_context) if isinstance(prior_context, dict) else {}
        failure_recovery = prior_context.get("failure_recovery")
        failure_recovery = failure_recovery if isinstance(failure_recovery, dict) else {}
        model_contract_failure = (
            getattr(error, "failure_class", None) == "model_contract"
            or failure_recovery.get("failure_class") == "model_contract"
        )
        if model_contract_failure:
            # A bounded format repair is a same-stage route/prompt change. It
            # is still autonomous work: admit one explicit recovery order so
            # the next Composer pass actually reruns this stage. It must not
            # be mislabeled as a scientific pivot or released downstream.
            attempts = prior_context.get("format_recovery_attempts", 0)
            if (failure_recovery.get("recovery_mode") == "format_repair_then_rerun"
                    and type(attempts) is int and attempts <= 1):
                requests = [item for item in prior_context.get("research_requests", [])
                            if isinstance(item, dict)]
                if not requests:
                    request = self._format_contract_recovery_request(stage, prior_context)
                    if isinstance(request, dict):
                        requests = [request]
                if requests:
                    prior_context.update({
                        "status": "research_expansion_required",
                        "review_status": "model_contract_repair",
                        "release_blocking": True,
                        "research_requests": deepcopy(requests),
                        "preserve_work_orders": True,
                    })
                    self.context[stage["id"]] = prior_context
                    if self._begin_continuation(completed, by_id):
                        self.department_activity.append({
                            "cycle": self.continuation_cycles,
                            "action": "admit_model_contract_recovery",
                            "stage_id": stage["id"],
                            "request_ids": [item.get("id") for item in requests],
                            "next_action": "reroute_and_compact_before_scientific_recovery",
                        })
                        return True
            # If the bounded route/format recovery also failed, keep the
            # current scope from replaying indefinitely. For a full research
            # mission, pivot to a fresh topic so the Composer keeps searching
            # for a useful direction; for a standalone stage, preserve the
            # explicit blocker for the caller.
            if stage.get("kind") in {"survey", "experiment"}:
                return self._pivot_topic_after_scientific_blocker(
                    stage, prior_context, completed, by_id,
                    reason="the stage response contract failed its bounded repair pass",
                    objective=(
                        "Generate a materially different, source-grounded computational question "
                        "whose evidence packet can be returned in the declared schema without "
                        "replaying the failed response contract."
                    ),
                    why=(
                        "The current stage produced no valid handoff after its compact format repair; "
                        "repeating the same assignment would burn quota without adding evidence."
                    ),
                )
            # In particular, do not let a model-contract failure become a
            # downstream missing-input handoff.
            return False
        pre_execution_capability_failure = self._is_pre_execution_capability_failure(
            stage, prior_context, error)
        repair_order_available = (
            failure_recovery.get("recovery_mode") == "repair_then_rerun"
            and isinstance(prior_context.get("failure_dossier_ref"), str)
        )
        if (self._forward_first()
                and not pre_execution_capability_failure
                and not repair_order_available
                and stage.get("kind") != "survey"):
            # ``run`` converts actionable blockers into non-gating
            # provisional nodes. A restored old ``blocked`` projection must
            # not reopen the same stage and consume another mission cycle.
            return False
        topic_budget_exhausted = self._is_local_topic_budget_exhaustion(error, stage)
        # A full survey pass can be scientifically non-productive even when
        # the runner returns a normal ValidationError rather than the generic
        # ModelWorkBlocked wrapper.  It must enter the same scoped topic-pivot
        # path; otherwise until-deadline retry policy replays an unchanged
        # literature map indefinitely.
        survey_review_blocker = (
            stage.get("kind") == "survey"
            and isinstance(error, ValidationError)
            and "survey review did not pass every required check" in str(error).casefold()
        )
        # A survey's bounded model contract is an evidence gate, not a
        # recoverable formatting retry.  The survey runner already spends its
        # local repair allowance and persists the failed assignment.  Reopening
        # the same survey here would recreate the same 70k-token assessment
        # packet on every Composer cycle, even though no new source evidence
        # has entered the graph.  Treat these failures as a scientific
        # direction blocker and move the frontier back to topic discovery.
        survey_assignment_blocker = self._is_survey_evidence_contract_blocker(stage, error)
        if ((not isinstance(error, ModelWorkBlocked)
             and not topic_budget_exhausted
             and not survey_review_blocker
             and not survey_assignment_blocker
             and not repair_order_available)
                or stage.get("kind") not in STAGE_KINDS):
            return False
        self._remaining()
        if isinstance(error, ModelWorkBlocked) and stage.get("kind") != "topic_discovery":
            # A survey/experiment-only workflow has no upstream frontier to
            # replan. Do not manufacture repeated same-stage repair cycles;
            # preserve the blocker for an explicit caller. Full research
            # missions always have a topic ancestor and continue through that
            # pivot path instead.
            pending = list(stage.get("depends_on", []))
            seen = set()
            has_topic_ancestor = False
            while pending:
                candidate_id = pending.pop()
                if candidate_id in seen or candidate_id not in by_id:
                    continue
                seen.add(candidate_id)
                candidate_stage = by_id[candidate_id]
                if candidate_stage.get("kind") == "topic_discovery":
                    has_topic_ancestor = True
                    break
                pending.extend(candidate_stage.get("depends_on", []))
            if not has_topic_ancestor:
                return False
        context = deepcopy(prior_context)
        observed_experiment = self._has_executed_experiment_result(
            prior_context, self._stage_experiment_capability_id(stage))
        repair_attempts = self._pre_execution_repair_count(stage, error=error)
        failure_debt = prior_context.get("failure_debt")
        pre_execution_loop = (
            stage.get("kind") == "experiment"
            and (prior_context.get("review_status") == "scientific_assignment_blocked"
                 or pre_execution_capability_failure)
            and "capability foundry" in " ".join((
                str(prior_context.get("error") or ""),
                str((failure_debt or {}).get("error")
                    if isinstance(failure_debt, dict) else ""),
                str(error),
            )).casefold()
            and not observed_experiment
            and type(repair_attempts) is int
            and repair_attempts >= PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT
        )
        if topic_budget_exhausted:
            # The previous cycle has no remaining model/retrieval capacity.
            # Clear only its emitted work orders so _continuation_requests()
            # creates a cycle-specific strategy instead of replaying the same
            # exhausted request. The selected topic is retained as negative
            # evidence for the next prompt, not as an executable incumbent.
            context.update({
                "stage_id": stage["id"],
                "kind": stage["kind"],
                "status": "research_expansion_required",
                "error": str(error)[:4096],
                "review_status": "topic_budget_exhausted",
                "topic_budget_recovery": {
                    "status": "required",
                    "scope": getattr(error, "topic_budget_scope", "unknown"),
                    "reason": "the current topic exploration envelope was exhausted",
                    "previous_cycle": self.continuation_cycles,
                },
                "research_expansion_requests": [],
                "research_requests": [],
            })
            self.context[stage["id"]] = context
            if not self._begin_continuation(completed, by_id):
                return False
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "pivot_topic_after_budget_exhaustion",
                "stage_id": stage["id"],
                "previous_cycle": self.continuation_cycles - 1,
                "reason": "topic envelope exhausted; opened a fresh bounded exploration cycle",
            })
            return True
        if pre_execution_loop:
            # A capability that never executed cannot be carried to
            # Interpretation as a provisional result. Once the bounded
            # source-level repair lineage is exhausted, return to topic
            # discovery and ask for a materially different executable branch;
            # reopening the same experiment would only burn another full
            # specialist/foundry pass.
            pivoted = self._pivot_topic_after_scientific_blocker(
                stage, context, completed, by_id,
                reason=(
                    "the experiment capability produced no observation after "
                    f"{PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT} bounded repairs"
                ),
                objective=(
                    "Choose a materially different computational research direction or executable "
                    "mechanism; preserve the failed dossier as negative evidence and avoid replaying "
                    "the same non-executable capability."
                ),
                why=(
                    "The current research question did not yield an executable observation within "
                    "the bounded capability-repair envelope; a new topic/design branch is more "
                    "informative than another unchanged experiment call."
                ),
            )
            if pivoted:
                self.department_activity.append({
                    "cycle": self.continuation_cycles,
                    "action": "pivot_topic_after_experiment_repair_limit",
                    "stage_id": stage["id"],
                    "repair_attempts": repair_attempts,
                    "repair_limit": PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT,
                    "next_action": "sample a materially different topic and rerun the bounded literature gate",
                })
                return True
            self.department_activity.append({
                "cycle": self.continuation_cycles,
                "action": "experiment_repair_waiting_for_budget",
                "stage_id": stage["id"],
                "repair_attempts": repair_attempts,
                "repair_limit": PRE_EXECUTION_CAPABILITY_REPAIR_LIMIT,
                "reason": "the exhausted experiment lineage could not open a fresh topic continuation",
            })
            return False
        if survey_review_blocker:
            # A complete literature pass has already paid for retrieval,
            # claim extraction, and independent review. Replaying the same
            # map until the mission wall is exhausted only burns model quota;
            # route the scientific direction back to topic discovery.
            return self._pivot_topic_after_scientific_blocker(
                stage, context, completed, by_id,
                reason="the literature acceptance gate rejected the current direction after a full pass",
                objective=(
                    "Generate a materially different, source-grounded computational question whose "
                    "literature map can pass independent evidence-fidelity checks before execution."
                ),
                why=(
                    "The current literature survey failed its independent required checks after a full "
                    "bounded pass; repeating the same map is not progress."
                ),
            )
        if survey_assignment_blocker:
            return self._pivot_topic_after_scientific_blocker(
                stage, context, completed, by_id,
                reason="the literature evidence contract was exhausted without a valid gap assessment",
                objective=(
                    "Generate a materially different, source-grounded computational question whose "
                    "literature evidence contract is bounded enough to complete without repeating "
                    "an invalid gap assessment."
                ),
                why=(
                    "The current survey exhausted its bounded evidence-contract repairs; no new "
                    "evidence would be introduced by replaying the same assessment packet."
                ),
            )
        context.update({
            "stage_id": stage["id"],
            "kind": stage["kind"],
            "status": "research_expansion_required",
            "error": str(error)[:4096],
            "review_status": "scientific_assignment_blocked",
        })
        if isinstance(specialist_verifier, dict):
            context["specialist_verifier"] = deepcopy(specialist_verifier)
        self.context[stage["id"]] = context
        if not self._begin_continuation(completed, by_id):
            return False
        self.department_activity.append({
            "cycle": self.continuation_cycles,
            "action": "auto_recover_scientific_blocker",
            "stage_id": stage["id"],
            "error": str(error)[:2048],
            "reopened_stage_ids": sorted(self.reopened_stage_ids),
            "next_condition": "complete the cycle-specific work order and re-run every affected admission gate",
        })
        return True

    def run(self):
        try:
            by_id = {stage["id"]: stage for stage in self.workflow["stages"]}
            stale_forward_handoffs = self._reconcile_stale_forward_handoffs(by_id)
            if stale_forward_handoffs:
                self._checkpoint("resume:reconcile_stale_forward_handoffs", force=True)
            legacy_retry_schedules = self._invalidate_legacy_provider_retry_schedules(by_id)
            if legacy_retry_schedules:
                self._checkpoint("resume:legacy_provider_retry_schedule_invalidated", force=True)
            completed = {stage_id for stage_id, row in self.stage_records.items()
                         if self._stage_releases_dependencies(
                             row, stage_kind=by_id.get(stage_id, {}).get("kind"))}
            resume_keep_task_ids = {
                row.get("task_id") for row in self.stage_records.values()
                if isinstance(row, dict)
                and row.get("status") in {"running", "retrying"}
                and isinstance(row.get("task_id"), str)
            }
            retired_stage_tasks = self._retire_superseded_stage_tasks(
                by_id,
                reason="superseded by the restored Composer frontier",
                keep_task_ids=resume_keep_task_ids,
            )
            if retired_stage_tasks:
                self.department_activity.append({
                    "cycle": self.continuation_cycles,
                    "action": "retire_stale_stage_tasks_on_resume",
                    "stage_tasks": retired_stage_tasks,
                })
                self._checkpoint("resume:retire_stale_stage_tasks", force=True)
            release_blocked_stage_ids = {
                stage_id for stage_id, row in self.stage_records.items()
                if row.get("status") == "candidate_needs_review"
                and not self._stage_releases_dependencies(
                    row, stage_kind=by_id.get(stage_id, {}).get("kind"))
            }
            # A prior attempt may have produced a release-blocking candidate
            # solely because a provider wrapped an otherwise complete JSON
            # response incorrectly.  Let the exact stage checkpoint consume
            # its one bounded format recovery; do not replay a full mission or
            # silently release a scientific candidate downstream.
            for stage_id in tuple(release_blocked_stage_ids):
                stage = by_id.get(stage_id)
                if self._release_blocked_format_retry_allowed(
                        stage, self.stage_records.get(stage_id, {})):
                    release_blocked_stage_ids.remove(stage_id)
                    self.stage_records[stage_id]["format_recovery_admitted"] = True
                    self._checkpoint(f"resume:{stage_id}:format_recovery_admitted", force=True)
            # Older forward-first checkpoints could park a capability-authoring
            # failure as a release-blocking candidate even though the
            # experiment never produced data. Reopen that scoped failure into
            # the Composer's scientific pivot path instead of replaying the
            # same program or waiting for human review.
            for stage_id in tuple(release_blocked_stage_ids):
                stage = by_id.get(stage_id)
                record = self.stage_records.get(stage_id, {})
                if self._release_blocked_topic_contract_retry_allowed(stage, record):
                    # The failed topic never produced an admitted artifact.
                    # Reuse the already-open continuation, if any, and let
                    # the fixed intake contract consume exactly one fresh
                    # topic attempt instead of reopening another cycle.
                    release_blocked_stage_ids.remove(stage_id)
                    record["contract_recovery_admitted"] = True
                    record["status"] = "retrying"
                    self._checkpoint(
                        f"resume:{stage_id}:contract_recovery_admitted", force=True)
                    continue
                if (stage_id in self.continuation_pending_stage_ids
                        and self._is_pre_execution_capability_failure(
                            stage, self.context.get(stage_id), record.get("error"))):
                    # This blocker already belongs to the active continuation
                    # opened by the prior Composer. Do not create another
                    # pivot or spend a second recovery cycle; simply release
                    # the old provisional node so the new topic can flow back
                    # through survey and experiment in order.
                    release_blocked_stage_ids.remove(stage_id)
                    record["status"] = "retrying"
                    record["recovery_admitted"] = True
                    self._checkpoint(
                        f"resume:{stage_id}:existing_continuation_admitted", force=True)
                    continue
                context = self.context.get(stage_id)
                if not self._is_pre_execution_capability_failure(
                        stage, context, record.get("error")):
                    continue
                error_text = record.get("error") or (
                    context.get("error") if isinstance(context, dict) else None
                ) or "pre-execution capability authoring failed"
                if self._admit_scientific_blocker_recovery(
                    stage, ModelWorkBlocked(str(error_text)), completed, by_id):
                    release_blocked_stage_ids.remove(stage_id)
                    record["status"] = "retrying"
                    record["recovery_admitted"] = True
                    self._checkpoint(f"resume:{stage_id}:scientific_recovery_admitted", force=True)
            # Migrate the old forward-first meaning on resume.  Earlier
            # checkpoints used ``release_blocking`` for every provisional
            # artifact, so the Composer could be left behind its own
            # deterministic repair gate.  The current authority model keeps
            # the same artifact and debt but records an explicit Composer
            # handoff and exposes it to the next dependency.
            for stage_id in tuple(release_blocked_stage_ids):
                stage = by_id.get(stage_id)
                record = self.stage_records.get(stage_id, {})
                if (not self._can_migrate_forward_candidate(record)
                        or stage is None or stage.get("kind") == "paper"):
                    continue
                context = self.context.get(stage_id, {})
                context = self._authorize_forward_context(
                    stage, context, reason="resume_migrated_forward_first_candidate")
                self.context[stage_id] = deepcopy(context)
                record.update({
                    "status": "candidate_needs_review",
                    "composer_decision": "advance_with_findings",
                    "progression_state": "advanced_with_findings",
                    "release_blocking": False,
                    "failure_debt": deepcopy(context.get("failure_debt", {})),
                })
                self.stage_records[stage_id] = record
                release_blocked_stage_ids.remove(stage_id)
                completed.add(stage_id)
                self.continuation_pending_stage_ids.discard(stage_id)
                self._mark_forward_blockers_advisory(stage_id)
                self.department_activity.append({
                    "cycle": self.continuation_cycles,
                    "action": "migrate_forward_first_candidate",
                    "stage_id": stage_id,
                    "authority": "command.composer",
                    "release_blocking": False,
                })
                self._checkpoint(
                    f"resume:{stage_id}:composer_advance_with_findings", force=True)
            for stage_id, record in self.stage_records.items():
                if (isinstance(record, dict)
                        and record.get("composer_decision") == "advance_with_findings"):
                    self._mark_forward_blockers_advisory(stage_id)
                    self.continuation_pending_stage_ids.discard(stage_id)
            required_ids = set(self.workflow["completion"]["required_stage_ids"])
            if self.continuation_pending_stage_ids:
                completed.difference_update(self.continuation_pending_stage_ids)
            migration_reopened = False
            restored_topic_failure = self._restored_topic_feasibility_failure(by_id)
            if restored_topic_failure is not None:
                topic_stage_id, reason = restored_topic_failure
                self._queue_topic_feasibility_revalidation(topic_stage_id, reason)
                if not self._begin_continuation(completed, by_id):
                    self.status = "blocked"
                    self.blockers.append({
                        "stage_id": topic_stage_id,
                        "reason": "completed topic failed current feasibility revalidation and no continuation window is available",
                        "detail": str(reason)[:2048],
                    })
                    self._checkpoint("blocked:topic_feasibility_revalidation", force=True)
                    return self._finish()
                migration_reopened = True
                self._checkpoint("resume:topic_feasibility_revalidation", force=True)
            # A checkpoint may predate the immediate-hold admission rule.  On
            # resume, repair/review holds are reconciled before the scheduler
            # can admit any downstream consumer; this also prevents an
            # interrupted run from silently treating an old proposal as data.
            held = {stage_id for stage_id, row in self.stage_records.items()
                    if row.get("status") in STAGE_HOLD_STATUSES}
            if held and not migration_reopened and self._begin_continuation(completed, by_id):
                self._checkpoint("continuation:resume_admitted", force=True)
            elif held and not migration_reopened:
                hold_status = ("research_expansion_required"
                               if any(self.stage_records[item].get("status") == "research_expansion_required"
                                      for item in held)
                               else "review_rejected")
                self.status = hold_status
                self._checkpoint(f"resume:{hold_status}", force=True)
                return self._finish()
            # A supervisor may restart after the previous process wrote a
            # terminal-looking blocker. Reopen recoverable scientific stages
            # before dependency scheduling so a restart is a continuation, not
            # another pass through the same dead end. Hard deadlines, provider
            # cooldowns, global quotas, and unknown external outcomes remain
            # explicit stop conditions.
            if self._reopen_blocked_checkpoint(completed, by_id):
                self._checkpoint("resume:blocked_stage_recovery_admitted", force=True)
            if self._resume_stage_quota_recovery(completed, by_id):
                self._checkpoint("resume:stage_quota_recovery_admitted", force=True)
            if self._resume_survey_provider_fallback(by_id):
                self._checkpoint("resume:survey_provider_fallback_admitted", force=True)
            if self._resume_stale_survey_contract_pivot(completed, by_id):
                self._checkpoint("resume:survey_contract_pivot_admitted", force=True)
            if self._resume_exhausted_pre_execution_experiment(completed, by_id):
                self._checkpoint("resume:experiment_repair_loop_pivot_admitted", force=True)
            while True:
                paper_gate_changed = self._refresh_paper_release_gate(
                    completed, by_id, release_blocked_stage_ids)
                if paper_gate_changed:
                    self._checkpoint("paper:upstream_scientific_hold", force=True)
                if self._paper_gate_repair_requests:
                    if self._begin_continuation(completed, by_id):
                        # The repair closure includes the paper consumer.  It
                        # must be retried after the repaired upstream packet,
                        # rather than remaining in the old release-blocked set.
                        release_blocked_stage_ids.difference_update(
                            self.reopened_stage_ids)
                        self._checkpoint(
                            f"paper:scoped_repair_admitted:{self.continuation_cycles}",
                            force=True,
                        )
                        continue
                if required_ids.issubset(completed) and self.continuation_pending_stage_ids.issubset(completed):
                    # A completed graph may still contain a first-class
                    # research request or an editorial rejection.  Reopen only
                    # the affected closure, then let the normal dependency
                    # scheduler execute it under the same hard wall.
                    if self._begin_continuation(completed, by_id):
                        continue
                    break
                self._remaining()
                progress = False
                dependency_ready_stages = [
                    stage for stage in self.workflow["stages"]
                    if stage["id"] not in completed
                    and stage["id"] not in release_blocked_stage_ids
                    and set(stage["depends_on"]).issubset(completed)
                ]
                ready_stages = []
                deferred_stages = []
                now_epoch = time.time()
                for stage in dependency_ready_stages:
                    schedule = self.retry_schedule.get(stage["id"])
                    not_before = (schedule.get("not_before_epoch")
                                  if isinstance(schedule, dict) else None)
                    if (type(not_before) in (int, float) and math.isfinite(not_before)
                            and not_before > now_epoch):
                        deferred_stages.append(stage)
                        continue
                    self.retry_schedule.pop(stage["id"], None)
                    ready_stages.append(stage)
                if not ready_stages and deferred_stages:
                    next_epoch = min(
                        self.retry_schedule[stage["id"]]["not_before_epoch"]
                        for stage in deferred_stages)
                    self._remaining()
                    self._checkpoint("agenda:retry_deferred")
                    time.sleep(min(5.0, max(0.0, next_epoch - time.time())))
                    continue
                ordered_ready_stages = self._agenda_order(
                    ready_stages, completed=completed, by_id=by_id)
                if not ordered_ready_stages and release_blocked_stage_ids:
                    self.status = "candidate_needs_review"
                    self._checkpoint("candidate_needs_review:release_blocked", force=True)
                    return self._finish()
                if ordered_ready_stages:
                    self._checkpoint(
                        f"agenda:{self.agenda_decisions[-1]['decision_id']}:selected",
                        force=True)
                for stage in ordered_ready_stages:
                    provider_configuration_error = self._stage_provider_configuration_error(stage)
                    if provider_configuration_error is not None:
                        self.status = "paused"
                        self.blockers.append({
                            "stage_id": stage["id"],
                            "reason": str(provider_configuration_error),
                            "stop_reason": "provider_configuration",
                            "provider": provider_configuration_error.provider,
                            "credential_env": provider_configuration_error.credential_env,
                            "model_calls_dispatched": 0,
                        })
                        self._checkpoint(f"{stage['id']}:provider_configuration", force=True)
                        return self._finish()
                    dependency_gaps = self._stage_dependency_gaps(stage)
                    if dependency_gaps:
                        # A provisional survey can be visible to the agenda
                        # while its gap assessment is still missing.  Route
                        # that scientific debt back to survey before the
                        # consumer is admitted; reporting it as a generic
                        # wiring failure used to strand the whole mission.
                        upstream_recovery = self._admit_upstream_dependency_recovery(
                            stage, dependency_gaps, completed, by_id)
                        if upstream_recovery is not None:
                            upstream_id, recovery_admitted = upstream_recovery
                            if recovery_admitted:
                                self._checkpoint(
                                    f"{stage['id']}:upstream_dependency_recovery", force=True)
                                progress = True
                                break
                            stop_reason = "upstream_scientific_hold"
                            reason = (
                                f"upstream stage {upstream_id} has no current typed handoff "
                                "and no continuation lease is available"
                            )
                        else:
                            stop_reason = "missing_stage_input"
                            reason = "required downstream artifact is unavailable"
                        self.status = "blocked"
                        self.blockers.append({
                            "stage_id": stage["id"],
                            "reason": reason,
                            "stop_reason": stop_reason,
                            "dependencies": dependency_gaps,
                            "model_calls_dispatched": 0,
                        })
                        self._checkpoint(f"{stage['id']}:{stop_reason}", force=True)
                        return self._finish()
                    stage_quota_error = self._stage_quota_error(stage)
                    if stage_quota_error is not None:
                        if self._admit_stage_quota_recovery(
                                stage, stage_quota_error, completed, by_id):
                            blocker = {
                                "stage_id": stage["id"],
                                "reason": str(stage_quota_error),
                                "stop_reason": "stage_quota_exhausted",
                                "dimension": stage_quota_error.dimension,
                                "limit": stage_quota_error.limit,
                                "observed": stage_quota_error.observed,
                                "usage": deepcopy(stage_quota_error.usage),
                                "model_calls_dispatched": 0,
                                "recovery": "cycle_admitted",
                                "repair_mode": "narrow_scope",
                            }
                            self.blockers.append(blocker)
                            stage_record = self.stage_records.setdefault(
                                stage["id"], {"kind": stage.get("kind")})
                            stage_record["status"] = "retrying"
                            stage_record["recovery_admitted"] = True
                            self._checkpoint(
                                f"{stage['id']}:stage_quota_recovery_admitted", force=True)
                            progress = True
                            break
                        self.status = "paused"
                        self.blockers.append({
                            "stage_id": stage["id"],
                            "reason": str(stage_quota_error),
                            "stop_reason": "stage_quota_exhausted",
                            "dimension": stage_quota_error.dimension,
                            "limit": stage_quota_error.limit,
                            "observed": stage_quota_error.observed,
                            "usage": deepcopy(stage_quota_error.usage),
                            "model_calls_dispatched": 0,
                        })
                        self._checkpoint(f"{stage['id']}:stage_quota_exhausted", force=True)
                        return self._finish()
                    stage_id = stage["id"]
                    # The estimate is a planning reservation, not a promise.
                    # Bounded jobs reserve the complete downstream closure;
                    # an autonomous deadline-governed job may use a residual
                    # window for the next specialist and let that specialist's
                    # own contract decide whether a useful result fits.
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
                    required_window = min(float(stage["estimate_seconds"]), downstream)
                    retry_policy = self._retry_policy()
                    if remaining < required_window:
                        if (retry_policy.get("mode", "bounded") == "until_deadline"
                                and remaining > self._deadline_dispatch_floor()):
                            self.deadline_decisions.append({
                                "stage_id": stage_id,
                                "admission": "residual_window",
                                "remaining_seconds": remaining,
                                "estimated_stage_seconds": float(stage["estimate_seconds"]),
                                "estimated_downstream_seconds": downstream,
                                "safety_margin_seconds": self._deadline_dispatch_floor(),
                                "reason": "full_stage_estimate_does_not_fit; autonomous mode keeps the residual window",
                            })
                        else:
                            self.status = "paused"
                            self.blockers.append({
                                "stage_id": stage_id,
                                "reason": "required_stage_window_does_not_fit_remaining_deadline",
                                "remaining_seconds": remaining,
                                "required_stage_seconds": required_window,
                                "mode": retry_policy.get("mode", "bounded"),
                            })
                            self.deadline_decisions.append({
                                "stage_id": stage_id,
                                "admission": "deferred",
                                "remaining_seconds": remaining,
                                "required_stage_seconds": required_window,
                                "reason": "required_stage_window_does_not_fit_remaining_deadline",
                            })
                            self._checkpoint("paused_deadline", force=True)
                            return self._finish()
                    # Continuation stages receive a cycle-specific project
                    # namespace.  The original stage descriptor remains the
                    # immutable graph identity; only the dispatch copy changes.
                    stage = self._stage_for_cycle(stage)
                    prior_record = self.stage_records.get(stage_id, {})
                    attempt_history = deepcopy(prior_record.get("attempts", []))
                    if not isinstance(attempt_history, list):
                        attempt_history = []
                    if prior_record.get("status") == "running" and prior_record.get("attempt_id"):
                        # A process interruption can leave a provider call
                        # without an observable outcome.  Preserve that
                        # uncertainty and start the next attempt in a fresh
                        # directory instead of overwriting its workspace.
                        self._reconcile_interrupted_attempt(prior_record["attempt_id"])
                        if not any(item.get("attempt_id") == prior_record["attempt_id"]
                                   and item.get("state") == "unknown"
                                   for item in attempt_history if isinstance(item, dict)):
                            attempt_history.append({
                                "attempt_number": prior_record.get("attempt_number", len(attempt_history) + 1),
                                "attempt_id": prior_record["attempt_id"],
                                "cycle": self.continuation_cycles,
                                "state": "unknown",
                                "project_dir": prior_record.get("project_dir", stage["project_dir"]),
                                "error": "Composer resumed after an incomplete stage attempt; outcome was not observed.",
                            })
                    last_error = None
                    stage_succeeded = False
                    context = None
                    adaptive_turn = self._agenda_policy()["mode"] == "adaptive"
                    adaptive_retry_scheduled = False
                    retry_indices = (range(1) if adaptive_turn else (
                        itertools.count()
                        if retry_policy.get("mode", "bounded") == "until_deadline"
                        else range(retry_policy["max_attempts"])
                    ))
                    for retry_index in retry_indices:
                        attempt_number = len(attempt_history) + 1
                        if retry_index:
                            try:
                                if not self._wait_before_retry(
                                        stage, attempt_number=attempt_number,
                                        retry_index=retry_index, error=last_error,
                                        downstream_seconds=downstream):
                                    last_error = ValidationError(
                                        "retry budget no longer fits the Composer hard deadline")
                                    break
                            except ComposerHardDeadlineExceeded:
                                raise
                            except Exception as exc:
                                last_error = exc
                                break
                        attempt_stage = self._attempt_stage(stage, attempt_number)
                        task = self._stage_task(stage)
                        task_id = task["task_id"]
                        attempt_id = f"{task_id}-{uuid.uuid4().hex}"
                        attempt_started = self.clock()
                        self.tasks.start_attempt(
                            task_id, attempt_id, owner="command.composer",
                            lease_ttl_seconds=max(1.0, self._remaining()),
                            payload={"stage_id": stage_id, "kind": stage["kind"],
                                     "attempt_number": attempt_number,
                                     "project_dir": attempt_stage["project_dir"]})
                        self.stage_records[stage_id] = {
                            "kind": stage["kind"], "status": "running",
                            "task_id": task_id, "attempt_id": attempt_id, "attempt_number": attempt_number,
                            "attempt_count": attempt_number,
                            "started_elapsed": attempt_started - self.started,
                            "project_dir": attempt_stage["project_dir"],
                            "attempts": deepcopy(attempt_history),
                        }
                        self._checkpoint(f"{stage_id}:admitted", force=True)
                        stop_live_progress = self._start_live_progress(attempt_stage)
                        # This belongs to the current attempt only.  A prior
                        # attempt may have produced a candidate before failing
                        # review; retaining it here would misclassify a later
                        # control-plane deadline as a late returned result.
                        context = None
                        stage_assignment = None
                        stage_admitted = False
                        specialist_bundle = {"reports": [], "by_role": {}, "usage": {}, "model_enabled": False}
                        specialist_verifier = None
                        failure_dossier = None
                        usage_recorded = False
                        try:
                            stage_assignment = self.departments.begin_stage(
                                stage_id, stage["kind"], attempt_number=attempt_number,
                                input_ref={
                                    "kind": "composer_stage_task", "ref": task_id,
                                    "digest": hashlib.sha256(canonical_bytes({
                                        "stage_id": stage_id, "attempt_number": attempt_number,
                                        "project_dir": attempt_stage["project_dir"],
                                    })).hexdigest(),
                                },
                                deadline_seconds=min(float(stage["deadline_seconds"]), self._remaining()),
                                active_role_ids=self._active_stage_role_ids(stage),
                            )
                            stage_admitted = True
                            self.stage_records[stage_id].update(
                                self._stage_assignment_fields(stage_assignment))
                            self.department_activity.append({
                                "cycle": self.continuation_cycles,
                                "action": "activate_specialist_pool",
                                "stage_id": stage_id,
                                "attempt_number": attempt_number,
                                "required_agents": deepcopy(stage_assignment["required_agents"]),
                                "active_agents": deepcopy(stage_assignment["active_agents"]),
                                "verifier_agent": stage_assignment["verifier_agent"],
                                "assignment_plan_ref": stage_assignment["plan_ref"],
                                "assignment_ids": deepcopy(stage_assignment["assignment_ids"]),
                            })
                            self._checkpoint(f"{stage_id}:specialists_admitted", force=True)
                            try:
                                descriptor = json.loads(Path(attempt_stage["config_path"]).read_text())
                            except (OSError, ValueError, TypeError):
                                descriptor = {}
                            # Self-contained producers have no upstream
                            # research package. Their specialists inspect the
                            # generated artifact instead of an empty packet.
                            review_generated_result = (stage["kind"] == "topic_discovery"
                                or descriptor.get("schema_version") == "review-article-config-1")
                            # Methods, interpretation, argument, and editorial
                            # specialists review materialized scientific
                            # artifacts. Running the methods pool before the
                            # capability exists gives reproducibility and
                            # analysis reviewers empty projections; those
                            # deterministic "not run yet" holds do not change
                            # the capability brief and are repeated after a
                            # producer failure. Review the experiment result
                            # or its bounded failure envelope instead.
                            materialized_specialist_review = (
                                review_generated_result
                                or stage["kind"] in {
                                    "experiment", "interpretation", "argument", "paper"
                                }
                            )
                            if not review_generated_result and stage["kind"] == "survey":
                                preflight_error = self._survey_provider_cooldown(
                                    attempt_stage, descriptor)
                                if preflight_error is not None:
                                    # A configured Crossref identity is a
                                    # bounded route change, not permission to
                                    # burn specialist calls against a fenced
                                    # OpenAlex account.
                                    if not self._admit_survey_provider_fallback(
                                            attempt_stage, preflight_error):
                                        raise preflight_error
                            if materialized_specialist_review:
                                prior_topic_reports = None
                                if stage_id in self.reopened_stage_ids:
                                    prior_topic_context = self.context.get(stage_id, {})
                                    if isinstance(prior_topic_context, dict):
                                        prior_topic_reports = prior_topic_context.get(
                                            "specialist_reports")
                                try:
                                    if attempt_number == 1:
                                        context = self._run_stage(
                                            attempt_stage, specialist_reports=prior_topic_reports)
                                    else:
                                        context = self._run_stage(
                                            attempt_stage, attempt_number=attempt_number,
                                            specialist_reports=prior_topic_reports)
                                except Exception as producer_error:
                                    # A failed producer still has a scientific
                                    # object to inspect.  Give the admitted
                                    # reviewers the exact returned package (or
                                    # a bounded failure envelope) before the
                                    # outer recovery path records the dossier.
                                    reviewable_failure = not isinstance(
                                        producer_error, (
                                            ProviderCooldownError,
                                            ProviderConfigurationError,
                                            QuotaExceededError,
                                            ComposerLateStageResult,
                                            ComposerHardDeadlineExceeded,
                                            KeyboardInterrupt,
                                        ))
                                    if reviewable_failure:
                                        specialist_bundle, specialist_verifier, context = (
                                            self._run_failure_specialist_review(
                                                attempt_stage, stage_assignment, descriptor,
                                                producer_error))
                                        if isinstance(context, dict):
                                            context["specialist_reports"] = deepcopy(
                                                specialist_bundle.get("reports", []))
                                            context["specialist_usage"] = deepcopy(
                                                specialist_bundle.get("usage", {}))
                                            if specialist_verifier is not None:
                                                context["specialist_verifier"] = deepcopy(
                                                    specialist_verifier)
                                    raise
                                specialist_bundle = self._run_specialist_pool(
                                    attempt_stage, stage_assignment, descriptor,
                                    stage_result=context)
                            else:
                                specialist_bundle = self._run_specialist_pool(
                                    attempt_stage, stage_assignment, descriptor)
                            specialist_bundle = self._publish_specialist_reports(
                                attempt_stage, stage_assignment, specialist_bundle)
                            specialist_reports = specialist_bundle.get("reports", [])
                            limited = [report for report in specialist_reports
                                       if report.get("status") != "succeeded" and report.get("status_code") == 429]
                            if limited:
                                raise ProviderCooldownError("specialist provider is cooling down",
                                    retry_after_seconds=max(float(report.get("retry_after_seconds") or
                                        self._remaining()) for report in limited),
                                    rate_limit={"provider": "model", "status_code": 429})
                            if not materialized_specialist_review:
                                if attempt_number == 1:
                                    context = (self._run_stage(
                                        attempt_stage, specialist_reports=specialist_reports)
                                        if specialist_reports else self._run_stage(attempt_stage))
                                else:
                                    context = (self._run_stage(
                                        attempt_stage, attempt_number=attempt_number,
                                        specialist_reports=specialist_reports)
                                        if specialist_reports else self._run_stage(
                                            attempt_stage, attempt_number=attempt_number))
                            if self._deadline_exhausted():
                                raise ComposerLateStageResult(
                                    "stage result exceeded the Composer hard deadline")
                            outcome = context.get("status")
                            if outcome not in {"completed", "accepted", "candidate_needs_review",
                                               "research_expansion_required", "review_rejected"}:
                                raise ValidationError(f"stage {stage_id} did not complete: {outcome}")
                            context["specialist_reports"] = deepcopy(specialist_reports)
                            context["specialist_usage"] = deepcopy(specialist_bundle.get("usage", {}))
                            merged_usage = deepcopy(context.get("usage", {}))
                            if not isinstance(merged_usage, dict):
                                merged_usage = {}
                            for key in ("model_calls", "input_tokens", "output_tokens"):
                                value = specialist_bundle.get("usage", {}).get(key, 0)
                                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                                    merged_usage[key] = merged_usage.get(key, 0) + value
                            foundry_usage = context.get("foundry_usage", {})
                            if not isinstance(foundry_usage, dict):
                                foundry_usage = {}
                            for key in ("model_calls", "input_tokens", "output_tokens"):
                                value = foundry_usage.get(key, 0)
                                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                                    merged_usage[key] = merged_usage.get(key, 0) + value
                            merged_usage["by_role"] = deepcopy(
                                specialist_bundle.get("usage", {}).get("by_role", {}))
                            context["usage"] = merged_usage
                            specialist_verifier = self._run_specialist_verifier(
                                attempt_stage, stage_assignment, descriptor,
                                specialist_bundle, context, stage_result=context)
                            if specialist_verifier is not None:
                                context["specialist_verifier"] = deepcopy(specialist_verifier)
                                verifier_usage = self._specialist_usage([specialist_verifier])
                                for key in ("model_calls", "input_tokens", "output_tokens"):
                                    value = verifier_usage.get(key, 0)
                                    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                                        context["usage"][key] = context["usage"].get(key, 0) + value
                                context["specialist_verifier_usage"] = deepcopy(verifier_usage)
                                verdict = (specialist_verifier.get("response", {})
                                           if isinstance(specialist_verifier, dict) else {})
                                if (specialist_verifier.get("status") != "succeeded"
                                        or not isinstance(verdict, dict)
                                        or verdict.get("decision") != "accept"):
                                    carried = (
                                        specialist_verifier.get("status") == "succeeded"
                                        and stage["kind"] == "topic_discovery"
                                        and self._carry_provisional_verifier_challenge(
                                            context, specialist_verifier)
                                    )
                                    if not carried:
                                        defer = (self.workflow.get("progression_policy") in {
                                                     "full_pass", FORWARD_FIRST_POLICY,
                                                 }
                                                 and stage["kind"] != "paper"
                                                 and specialist_verifier.get("status") == "succeeded"
                                                 and outcome in STAGE_READY_STATUSES)
                                        outcome = "candidate_needs_review" if defer else "review_rejected"
                                        if defer:
                                            context["deferred_review_findings"] = deepcopy(verdict)
                                        context["status"] = outcome
                            # ``candidate_needs_review`` is only a valid
                            # forward-first outcome when the experiment has
                            # produced inspectable evidence.  A model-contract
                            # recovery can otherwise look like a successful
                            # specialist pass and release an empty packet to
                            # Interpretation.  Convert every terminal-looking
                            # unexecuted experiment into a blocking, typed hold
                            # before the normal completion bookkeeping.
                            recovery = context.get("failure_recovery")
                            recovery = recovery if isinstance(recovery, dict) else {}
                            explicitly_unexecuted = (
                                context.get("results_status") == "not_executed"
                                or context.get("review_status") == "model_contract_repair"
                                or context.get("format_recovery") is True
                                or recovery.get("recovery_mode") in {
                                    "repair_then_rerun", "format_repair_then_rerun",
                                    "experiment_diagnose_patch_execute_recalculate",
                                }
                            )
                            if (
                                stage["kind"] == "experiment"
                                and explicitly_unexecuted
                                and outcome in {"completed", "accepted", "candidate_needs_review"}
                                and not self._has_executed_experiment_result(
                                    context, self._stage_experiment_capability_id(stage))
                            ):
                                context = self._hold_unexecuted_experiment(
                                    stage, context,
                                    reason=(context.get("error")
                                            or "experiment returned no executable result"))
                                outcome = context["status"]
                                self.department_activity.append({
                                    "cycle": self.continuation_cycles,
                                    "action": "hold_unexecuted_experiment",
                                    "stage_id": stage_id,
                                    "reason": "no observed experiment result is available for downstream evidence",
                                    "work_order_ids": [item.get("id") for item in context.get("research_requests", [])
                                                       if isinstance(item, dict)],
                                })
                            # A failed independent review is a finding on an
                            # admitted stage, not a deterministic veto.  Keep
                            # the verdict attached to the candidate and let
                            # the Composer expose the dependency with an
                            # explicit backfill debt.
                            if (self._forward_first()
                                    and outcome in STAGE_HOLD_STATUSES):
                                context = self._authorize_forward_context(
                                    stage, context, reason="admitted_stage_review_finding")
                                outcome = context["status"]
                            if stage["kind"] == "topic_discovery":
                                # Record the direction at admission time, even
                                # when a later survey or experiment hold stops
                                # the mission.  A failed attempt is still an
                                # attempted direction and must not be selected
                                # again by the next free-topic mission.
                                self._record_topic_history(context)
                            foundry_charged = context.get("foundry_usage", {})
                            if not isinstance(foundry_charged, dict):
                                foundry_charged = {}
                            for key in self.usage:
                                value = context.get("usage", {}).get(key, 0)
                                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                                    already_charged = foundry_charged.get(key, 0)
                                    if type(already_charged) not in (int, float) or already_charged < 0:
                                        already_charged = 0
                                    self.usage[key] += max(0, value - already_charged)
                            usage_recorded = True
                            assignment_result = self.departments.finish_stage(
                                stage_id, stage["kind"], attempt_number=attempt_number,
                                outcome=outcome, output_ref=context.get("output_path"),
                                usage=context.get("usage", {}), actor="command.composer",
                                specialist_results=specialist_bundle.get("by_role", {}),
                                verifier_result=specialist_verifier)
                            self.department_activity.append({
                                "cycle": self.continuation_cycles,
                                "action": "complete_specialist_pool",
                                "stage_id": stage_id,
                                "attempt_number": attempt_number,
                                "verifier_agent": assignment_result["verifier_agent"],
                                "verifier_outcome": assignment_result["verifier_outcome"],
                                "chief_synthesis_ref": assignment_result["chief_synthesis_ref"],
                                "verifier_artifact_ref": assignment_result["verifier_artifact_ref"],
                            })
                            self.tasks.finish_attempt(attempt_id, "succeeded", usage=context.get("usage", {}))
                            self.tasks.transition(task_id, "awaiting_review", "command.composer", reason="stage output returned")
                            if outcome not in STAGE_HOLD_STATUSES:
                                self.tasks.transition(task_id, "completed", "command.composer", reason="stage-specific checks passed")
                            context["stage_id"] = stage_id
                            context["attempt_number"] = attempt_number
                            previous_stage_context = deepcopy(self.context.get(stage_id))
                            self.context[stage_id] = context
                            if stage["kind"] == "topic_discovery":
                                self._retire_requests_after_topic_change(
                                    previous_stage_context, context)
                            self._mark_research_requests_attempted(stage)
                            if self.active_research_requests and not context.get(
                                    "preserve_work_orders"):
                                self.department_activity.append({
                                    "cycle": self.continuation_cycles,
                                    "action": "resolve_work_orders",
                                    "stage_id": stage_id,
                                    "outcome": outcome,
                                    "work_orders": self.departments.resolve_work_orders(
                                        self.active_research_requests,
                                        stage_kind=stage["kind"], outcome=outcome),
                                })
                            elif self.active_research_requests:
                                self.department_activity.append({
                                    "cycle": self.continuation_cycles,
                                    "action": "retain_work_orders_for_backfill",
                                    "stage_id": stage_id,
                                    "outcome": outcome,
                                    "work_orders": deepcopy(self.active_research_requests),
                                })
                            self.continuation_pending_stage_ids.discard(stage_id)
                            topic_usage = {}
                            if isinstance(context.get("budget"), dict):
                                topic_usage = context["budget"].get("usage", {})
                            if not isinstance(topic_usage, dict):
                                topic_usage = {}
                            attempt_history.append({
                                "attempt_number": attempt_number,
                                "attempt_id": attempt_id,
                                "cycle": self.continuation_cycles,
                                "state": "succeeded",
                                "project_dir": attempt_stage["project_dir"],
                                "usage": deepcopy(context.get("usage", {})),
                                "topic_usage": deepcopy(topic_usage),
                                "elapsed_seconds": self.clock() - attempt_started,
                            })
                            self.stage_records[stage_id] = {
                                "kind": stage["kind"],
                                "status": (outcome if outcome in {
                                    "candidate_needs_review", "research_expansion_required", "review_rejected"}
                                           else "completed"),
                                "task_id": task_id, "attempt_id": attempt_id,
                                "attempt_number": attempt_number,
                                "attempt_count": attempt_number,
                                "attempts": deepcopy(attempt_history),
                                "output_path": context.get("output_path"),
                                "elapsed_seconds": self.clock() - self.started,
                                **self._stage_assignment_fields(stage_assignment, assignment_result),
                            }
                            stage_succeeded = True
                            break
                        except Exception as exc:
                            original_usage = getattr(exc, "usage", {})
                            if isinstance(exc, ModelCallError) and exc.status_code == 429:
                                exc = ProviderCooldownError(str(exc),
                                    retry_after_seconds=exc.retry_after_seconds or max(0.1, self._remaining()),
                                    rate_limit={"provider": "model", "status_code": 429})
                                exc.usage = original_usage
                            self._normalize_topic_intake_failure(exc, stage)
                            last_error = exc
                            failure_usage = (deepcopy(context.get("usage", {})) if usage_recorded
                                             else self._record_failed_stage_usage(exc))
                            foundry_failure_usage = getattr(exc, "foundry_usage", {})
                            if isinstance(foundry_failure_usage, dict):
                                for key in ("model_calls", "input_tokens", "output_tokens"):
                                    amount = foundry_failure_usage.get(key, 0)
                                    if type(amount) in (int, float) and math.isfinite(amount) and amount >= 0:
                                        failure_usage[key] = failure_usage.get(key, 0) + amount
                            specialist_usage = self._specialist_usage([
                                *specialist_bundle.get("reports", []),
                                *([specialist_verifier] if isinstance(specialist_verifier, dict) else []),
                            ])
                            for key in (() if usage_recorded else self.usage):
                                amount = specialist_usage.get(key, 0)
                                if type(amount) in (int, float) and amount >= 0:
                                    self.usage[key] += amount
                                    failure_usage[key] = failure_usage.get(key, 0) + amount
                            provider_paused = isinstance(exc, ProviderCooldownError)
                            provider_configuration = isinstance(exc, ProviderConfigurationError)
                            provider_fallback_admitted = (
                                provider_paused
                                and stage["kind"] == "survey"
                                and self._admit_survey_provider_fallback(stage, exc)
                            )
                            repair_order_issued = False
                            model_contract_repair = False
                            stage_contract_failure = (
                                isinstance(exc, (ValidationError, ModelWorkBlocked))
                                or isinstance(getattr(exc, "stage_result", None), dict)
                            )
                            resource_or_control_failure = (
                                provider_paused or provider_configuration
                                or isinstance(exc, QuotaExceededError)
                                or isinstance(exc, (ComposerLateStageResult,
                                                    ComposerHardDeadlineExceeded,
                                                    KeyboardInterrupt))
                                or (isinstance(exc, ModelCallError)
                                    and exc.status_code in {408, 425, 429, 500, 502, 503, 504})
                            )
                            # Every known terminal condition gets an immutable
                            # dossier. Resource/control failures are recorded
                            # as fences without creating scientific work orders;
                            # only the scientific classes below can open repair
                            # work. This keeps the audit trail complete while
                            # preventing a quota or provider outage from being
                            # misread as a bad hypothesis.
                            record_failure_dossier = (
                                (stage_contract_failure or resource_or_control_failure)
                                and not isinstance(exc, KeyboardInterrupt)
                            )
                            if record_failure_dossier:
                                try:
                                    failure_dossier = self._record_failure_recovery(
                                        stage, attempt_stage, exc, context,
                                        specialist_bundle, specialist_verifier,
                                        attempt_number)
                                    repair_order_issued = (
                                        isinstance(failure_dossier, dict)
                                        and failure_dossier.get("recoverable") is True
                                        and failure_dossier.get("failure_class") not in {
                                            "operational_recovery", "model_contract"
                                        }
                                    )
                                    model_contract_repair = (
                                        isinstance(failure_dossier, dict)
                                        and failure_dossier.get("failure_class") == "model_contract"
                                        and isinstance(self.context.get(stage_id), dict)
                                        and self.context[stage_id].get("format_recovery") is True
                                        and self.context[stage_id].get("format_recovery_attempts", 0) <= 1
                                    )
                                    # Failure recovery replaces the stage packet
                                    # in the durable context. The local
                                    # ``context`` still points at the failed
                                    # runner envelope, so using it below could
                                    # erase the freshly issued repair order and
                                    # authorize a provisional handoff. Carry
                                    # the authoritative recovery packet through
                                    # the remainder of this attempt.
                                    recovered_context = self.context.get(stage_id)
                                    if (isinstance(recovered_context, dict)
                                            and isinstance(
                                                recovered_context.get("failure_recovery"), dict)):
                                        context = deepcopy(recovered_context)
                                except Exception as recovery_error:
                                    # Failure analysis is additive. It must
                                    # never erase the original stage error or
                                    # turn an otherwise recoverable run into a
                                    # control-plane crash.
                                    self.department_activity.append({
                                        "cycle": self.continuation_cycles,
                                        "action": "failure_analysis_error",
                                        "stage_id": stage_id,
                                        "attempt_number": attempt_number,
                                        "error": f"{type(recovery_error).__name__}: {recovery_error}",
                                    })
                            topic_intake_retry = self._is_topic_intake_retry(exc, stage)
                            quota_exhausted = (
                                isinstance(exc, QuotaExceededError)
                                and not topic_intake_retry
                            )
                            if topic_intake_retry:
                                topic_retry_reason = getattr(
                                    exc, "topic_retry_reason", "intake_contract_failure")
                                rejected = list(getattr(exc, "rejected_topic_history", []) or [])
                                if (topic_retry_reason == "scientific_candidate_rejected"
                                        and not rejected):
                                    for trace in reversed(
                                            getattr(exc, "candidate_attempt_trace", [])):
                                        if not isinstance(trace, dict):
                                            continue
                                        rejection_type = trace.get("rejection_type")
                                        if rejection_type not in {
                                                "novelty", "source_challenge", "maturity",
                                                "feasibility"}:
                                            continue
                                        selected = (
                                            trace.get("selected_topic")
                                            if isinstance(trace, dict) else None
                                        )
                                        if (not isinstance(selected, dict)
                                                or not isinstance(selected.get("id"), str)):
                                            continue
                                        rejected = [{
                                            "topic_id": selected["id"],
                                            "title": selected.get("title"),
                                            "domain": selected.get("domain"),
                                            "research_question": selected.get("research_question"),
                                            "research_form": selected.get("research_form"),
                                            "evidence_mode": selected.get("evidence_mode"),
                                            "comparison_type": selected.get("comparison_type"),
                                            "rejection_type": rejection_type,
                                            "rejection_reason": str(exc)[:2048],
                                        }]
                                        break
                                if rejected:
                                    try:
                                        self._record_topic_rejection_history(rejected)
                                        self.department_activity.append({
                                            "cycle": self.continuation_cycles,
                                            "action": "pivot_topic_direction",
                                            "stage_id": stage_id,
                                            "attempt_number": attempt_number,
                                            "rejected_topic_ids": [
                                                item.get("topic_id") for item in rejected
                                                if isinstance(item, dict)
                                                and isinstance(item.get("topic_id"), str)
                                            ],
                                            "reason": topic_retry_reason,
                                        })
                                    except Exception as history_error:
                                        # A pivot without durable rejection
                                        # memory could immediately rediscover
                                        # the same weak direction. Preserve the
                                        # failure rather than weakening the
                                        # exclusion contract silently.
                                        topic_intake_retry = False
                                        quota_exhausted = isinstance(exc, QuotaExceededError)
                                        last_error = ValidationError(
                                            "topic pivot history could not be persisted: "
                                            f"{type(history_error).__name__}: {history_error}"
                                        )
                                else:
                                    self.department_activity.append({
                                        "cycle": self.continuation_cycles,
                                        "action": "retry_topic_intake",
                                        "stage_id": stage_id,
                                        "attempt_number": attempt_number,
                                        "rejected_topic_ids": [],
                                        "reason": topic_retry_reason,
                                    })
                            if stage_assignment is not None:
                                try:
                                    assignment_result = self.departments.finish_stage(
                                        stage_id, stage["kind"], attempt_number=attempt_number,
                                        outcome="blocked", output_ref=None,
                                        usage=failure_usage, error=exc,
                                        actor="command.composer",
                                        specialist_results=specialist_bundle.get("by_role", {}),
                                        verifier_result=specialist_verifier,
                                        failure_scope="stage")
                                    self.department_activity.append({
                                        "cycle": self.continuation_cycles,
                                        "action": "record_stage_failure",
                                        "stage_id": stage_id,
                                        "attempt_number": attempt_number,
                                        "failure_scope": "stage",
                                        "verifier_agent": assignment_result["verifier_agent"],
                                        "verifier_outcome": assignment_result["verifier_outcome"],
                                        "chief_synthesis_ref": assignment_result["chief_synthesis_ref"],
                                        "verifier_artifact_ref": assignment_result["verifier_artifact_ref"],
                                    })
                                    self.stage_records[stage_id].update(
                                        self._stage_assignment_fields(stage_assignment, assignment_result))
                                except (NotFoundError, StateError, ValidationError):
                                    # Preserve the stage failure as the primary
                                    # signal; the assignment ledger remains
                                    # inspectable and resume can reconcile any
                                    # child attempt still marked started.
                                    assignment_result = None
                            try:
                                self.tasks.finish_attempt(
                                    attempt_id, "failed", usage=failure_usage)
                                self.tasks.transition(
                                    task_id,
                                    "paused" if (provider_paused or provider_configuration) else "blocked",
                                    "command.composer",
                                    reason=str(exc),
                                )
                            except Exception:
                                pass
                            failed_attempt = {
                                "attempt_number": attempt_number,
                                "attempt_id": attempt_id,
                                "cycle": self.continuation_cycles,
                                "state": "failed",
                                "project_dir": attempt_stage["project_dir"],
                                "error": f"{type(exc).__name__}: {exc}",
                                "usage": deepcopy(failure_usage),
                                "retry_reason": (
                                    getattr(exc, "topic_retry_reason", None)
                                    if topic_intake_retry else None
                                ),
                                "elapsed_seconds": self.clock() - attempt_started,
                            }
                            if isinstance(failure_dossier, dict):
                                failed_attempt["failure_dossier_ref"] = failure_dossier.get("artifact_ref")
                                failed_attempt["failure_class"] = failure_dossier.get("failure_class")
                                failed_attempt["repair_order_issued"] = repair_order_issued
                            if stage["kind"] == "topic_discovery":
                                failed_attempt["topic_usage"] = deepcopy(failure_usage)
                                for attribute in (
                                        "candidate_attempt_trace",
                                        "maturity_review_history",
                                        "rejected_topic_history"):
                                    value = getattr(exc, attribute, None)
                                    if isinstance(value, list) and value:
                                        # Keep the retry ledger useful to the
                                        # dashboard without copying an
                                        # unbounded provider response.
                                        failed_attempt[attribute] = deepcopy(value[-24:])
                            attempt_history.append(failed_attempt)
                            # A known provider reset is a transient resource
                            # condition in the autonomous deadline-governed
                            # policy. The retry wait below honors its exact
                            # reset boundary; bounded workflows retain the
                            # explicit pause contract.
                            retry_open = (
                                not provider_configuration
                                and not quota_exhausted
                                and not isinstance(
                                    exc, (ComposerLateStageResult,
                                          ComposerHardDeadlineExceeded))
                                and (
                                    model_contract_repair
                                    or not isinstance(exc, ModelWorkBlocked)
                                )
                                and (
                                    retry_policy.get("mode", "bounded") == "until_deadline"
                                    or len(attempt_history) < retry_policy.get("max_attempts", 0)
                                )
                            )
                            # A scientific failure has already been converted
                            # into an evidence dossier and a scoped repair
                            # order.  Retrying the unchanged stage here would
                            # bypass that order and burn the same model/data
                            # budget, so let the Composer continuation handle
                            # the repair first.
                            if repair_order_issued:
                                retry_open = False
                            if (adaptive_turn and retry_open
                                    and (not provider_paused
                                         or retry_policy.get("mode", "bounded")
                                         == "until_deadline")):
                                retry_open = self._schedule_adaptive_retry(
                                    stage, attempt_number=attempt_number, error=last_error,
                                    downstream_seconds=downstream)
                                adaptive_retry_scheduled = retry_open
                            assignment_fields = {
                                key: deepcopy(self.stage_records.get(stage_id, {}).get(key))
                                for key in (
                                    "required_agents", "active_agents", "verifier_agent", "chief_agent",
                                    "assignment_plan_ref", "assignment_ids", "assignment_task_ids",
                                    "assignment_deadline_seconds", "chief_synthesis_ref",
                                    "verifier_artifact_ref", "verifier_outcome", "specialist_assignments",
                                )
                                if key in self.stage_records.get(stage_id, {})
                            }
                            assignment_fields = self._retire_stage_assignment(assignment_fields)
                            self.stage_records[stage_id] = {
                                "kind": stage["kind"],
                                "status": ("paused" if (provider_paused or provider_configuration)
                                           else "retrying" if retry_open else "blocked"),
                                "task_id": task_id, "attempt_id": attempt_id, "attempt_number": attempt_number,
                                "attempt_count": attempt_number, "attempts": deepcopy(attempt_history),
                                "error": f"{type(exc).__name__}: {exc}",
                                "usage": deepcopy(failure_usage),
                                **assignment_fields,
                            }
                            if provider_paused:
                                retry_after = float(exc.retry_after_seconds)
                                cooldown_blocker = {
                                    "stage_id": stage_id,
                                    "reason": "provider_cooldown",
                                    "provider_error": str(exc),
                                    "retry_after_seconds": retry_after,
                                    "retry_after_epoch": time.time() + retry_after,
                                    "rate_limit": deepcopy(exc.rate_limit),
                                    "attempts": len(attempt_history),
                                }
                                cooldown_snapshot = getattr(exc, "topic_budget", {})
                                cooldown_snapshot = (
                                    cooldown_snapshot if isinstance(cooldown_snapshot, dict) else {})
                                cooldown_usage = getattr(exc, "usage", None)
                                if not isinstance(cooldown_usage, dict) or not cooldown_usage:
                                    cooldown_usage = cooldown_snapshot.get("usage", {})
                                cooldown_diagnostics = getattr(exc, "diagnostics", None)
                                if not isinstance(cooldown_diagnostics, list) or not cooldown_diagnostics:
                                    cooldown_diagnostics = cooldown_snapshot.get("events", [])
                                if isinstance(cooldown_usage, dict) and cooldown_usage:
                                    cooldown_blocker["usage"] = deepcopy(cooldown_usage)
                                if isinstance(cooldown_diagnostics, list) and cooldown_diagnostics:
                                    cooldown_blocker["diagnostics"] = deepcopy(cooldown_diagnostics)
                                if (retry_open
                                        and retry_policy.get("mode", "bounded") == "until_deadline"):
                                    # Keep the cooldown in the durable activity
                                    # ledger without misclassifying it as a
                                    # terminal blocker. The next retry waits
                                    # until the provider reset boundary and
                                    # then starts a fresh isolated attempt.
                                    self.department_activity.append({
                                        "cycle": self.continuation_cycles,
                                        "action": "provider_cooldown_auto_retry",
                                        "stage_id": stage_id,
                                        "attempt_number": attempt_number,
                                        "retry_after_seconds": retry_after,
                                        "retry_after_epoch": cooldown_blocker["retry_after_epoch"],
                                        "rate_limit": deepcopy(exc.rate_limit),
                                        "next_condition": "wait for provider reset, then dispatch a fresh isolated attempt",
                                    })
                                    self._checkpoint(
                                        f"{stage_id}:provider_cooldown_retrying", force=True)
                                else:
                                    self.blockers.append(cooldown_blocker)
                                    self.status = "paused"
                                    self._checkpoint(f"{stage_id}:provider_cooldown", force=True)
                                    return self._finish()
                            if provider_fallback_admitted:
                                # The fallback changes the next input packet;
                                # do not sleep until the OpenAlex reset or
                                # leave the stale cooldown in the agenda.
                                self.retry_schedule.pop(stage_id, None)
                                self.stage_records.setdefault(stage_id, {})["status"] = "retrying"
                                adaptive_retry_scheduled = True
                            if provider_configuration:
                                self.blockers.append({
                                    "stage_id": stage_id,
                                    "reason": str(exc),
                                    "stop_reason": "provider_configuration",
                                    "provider": exc.provider,
                                    "credential_env": exc.credential_env,
                                    "model_calls_dispatched": failure_usage.get("model_calls", 0),
                                })
                                self.status = "paused"
                                self._checkpoint(f"{stage_id}:provider_configuration", force=True)
                                return self._finish()
                            self._checkpoint(f"{stage_id}:retrying" if retry_open else f"{stage_id}:failed", force=True)
                            if isinstance(exc, ComposerLateStageResult):
                                raise
                            if isinstance(exc, ComposerHardDeadlineExceeded):
                                if isinstance(context, dict):
                                    raise ComposerLateStageResult(
                                        "stage result exceeded the Composer hard deadline"
                                    ) from exc
                                raise
                            if adaptive_retry_scheduled:
                                break
                            if quota_exhausted or not retry_open:
                                # Provider/mission quota exhaustion is a hard
                                # stop. A local scientific intake exhaustion
                                # deliberately does not enter this branch: it
                                # authorizes a fresh direction instead.
                                break
                        finally:
                            stop_live_progress()
                    if adaptive_retry_scheduled:
                        # One failed call is one adaptive agenda turn. The
                        # scheduler can now choose another ready action, or
                        # wait for this retry's not-before boundary and
                        # reassess the frontier before dispatching it again.
                        progress = True
                        break
                    if (not stage_succeeded and stage_admitted
                            and self._composer_can_advance_after_admission(
                                stage, last_error)
                            and last_error is not None):
                        provisional = self._materialize_forward_progress(
                            stage, attempt_stage, last_error, context,
                            specialist_bundle, attempt_history,
                            force_advance=True)
                        if provisional is not None:
                            context = provisional
                            self.context[stage_id] = deepcopy(provisional)
                            current_record = deepcopy(self.stage_records.get(stage_id, {}))
                            current_record.update({
                                "status": "candidate_needs_review",
                                "output_path": provisional.get("output_path"),
                                "forward_progress": True,
                                "composer_decision": "advance_with_findings",
                                "progression_state": "advanced_with_findings",
                                "evidence_state": provisional.get("evidence_state", "unverified"),
                                "results_status": provisional.get("results_status"),
                                "review_status": provisional.get("review_status"),
                                "release_blocking": False,
                                "failure_debt": deepcopy(provisional.get("failure_debt", {})),
                            })
                            self.stage_records[stage_id] = current_record
                            self.continuation_pending_stage_ids.discard(stage_id)
                            stage_succeeded = True
                    if stage_succeeded:
                        release_blocking = (
                            context.get("release_blocking") is True
                            or isinstance(context.get("failure_debt"), dict)
                            and context["failure_debt"].get("release_blocking") is True
                        )
                        # A paper result remains a review candidate until its
                        # own publication contract is accepted.  Never let a
                        # provisional editorial packet satisfy a dependency.
                        if (stage["kind"] == "paper"
                                and context.get("status") == "candidate_needs_review"):
                            release_blocking = True
                        if release_blocking:
                            release_blocked_stage_ids.add(stage_id)
                        else:
                            completed.add(stage_id)
                        self._record_feedback(stage, context)
                        self.stage_records[stage_id] = self._retire_stage_assignment(
                            self.stage_records.get(stage_id, {}))
                        if context.get("status") in STAGE_HOLD_STATUSES:
                            # A hold is a control decision, not a successful
                            # dependency.  Start the owning continuation now;
                            # otherwise return the hold without admitting a
                            # consumer that would write against insufficient
                            # or rejected evidence.
                            if self._begin_continuation(completed, by_id):
                                self._checkpoint(f"{stage_id}:continuation_admitted", force=True)
                                progress = True
                                break
                            self.status = context["status"]
                            self._checkpoint(f"{stage_id}:{context['status']}", force=True)
                            return self._finish()
                        self._checkpoint(
                            f"{stage_id}:release_blocked" if release_blocking
                            else f"{stage_id}:completed",
                            force=True,
                        )
                        progress = True
                        break
                    error = last_error or ValidationError(
                        f"stage {stage_id} exhausted its retry policy")
                    assignment_fields = {
                        key: deepcopy(self.stage_records.get(stage_id, {}).get(key))
                        for key in (
                            "required_agents", "active_agents", "verifier_agent", "chief_agent",
                            "assignment_plan_ref", "assignment_ids", "assignment_task_ids",
                            "assignment_deadline_seconds", "chief_synthesis_ref",
                            "verifier_artifact_ref", "verifier_outcome", "specialist_assignments",
                        )
                        if key in self.stage_records.get(stage_id, {})
                    }
                    assignment_fields = self._retire_stage_assignment(assignment_fields)
                    self.stage_records[stage_id] = {
                        "kind": stage["kind"], "status": "blocked",
                        "task_id": task_id,
                        "attempt_count": len(attempt_history), "attempts": deepcopy(attempt_history),
                        "error": f"{type(error).__name__}: {error}",
                        **assignment_fields,
                    }
                    if isinstance(failure_dossier, dict):
                        self.stage_records[stage_id].update({
                            "failure_dossier_ref": failure_dossier.get("artifact_ref"),
                            "failure_class": failure_dossier.get("failure_class"),
                            "repair_order_issued": failure_dossier.get("recoverable") is True
                                and failure_dossier.get("failure_class") not in {
                                    "operational_recovery", "model_contract"
                                },
                            "repair_commands": deepcopy(failure_dossier.get("repair_commands", [])),
                            "acceptance_checks": deepcopy(failure_dossier.get("acceptance_checks", [])),
                        })
                    blocker = {"stage_id": stage_id, "reason": str(error),
                               "attempts": len(attempt_history)}
                    if isinstance(failure_dossier, dict):
                        blocker.update({
                            "failure_dossier_ref": failure_dossier.get("artifact_ref"),
                            "failure_class": failure_dossier.get("failure_class"),
                            "repair_order_issued": failure_dossier.get("recoverable") is True
                                and failure_dossier.get("failure_class") not in {
                                    "operational_recovery", "model_contract"
                                },
                        })
                    if getattr(error, "failure_class", None) == "context_budget":
                        blocker["failure_class"] = "context_budget"
                        blocker["context_budget"] = deepcopy(
                            getattr(error, "context_budget", {}))
                    if stage["kind"] == "topic_discovery":
                        rejected_history = getattr(error, "rejected_topic_history", None)
                        if isinstance(rejected_history, list) and rejected_history:
                            try:
                                self._record_topic_rejection_history(rejected_history)
                            except Exception as history_error:
                                blocker["topic_history_error"] = (
                                    f"{type(history_error).__name__}: {history_error}")
                    if isinstance(getattr(error, "usage", None), dict) and error.usage:
                        blocker["usage"] = deepcopy(error.usage)
                    if isinstance(getattr(error, "diagnostics", None), list) and error.diagnostics:
                        blocker["diagnostics"] = deepcopy(error.diagnostics)
                    for attribute in ("candidate_attempt_trace", "maturity_review_history",
                                      "rejected_topic_history"):
                        value = getattr(error, attribute, None)
                        if isinstance(value, list) and value:
                            blocker[attribute] = deepcopy(value)
                    self.blockers.append(blocker)
                    self._record_blocker_feedback(stage, error)
                    # A failed continuation has consumed the current scoped
                    # work order even when no stage result exists to reach the
                    # normal success bookkeeping.  Fence that exact request
                    # before synthesizing a new repair action; otherwise the
                    # same dossier can reopen the same closure indefinitely.
                    self._mark_research_requests_attempted(stage)
                    recovery_admitted = False
                    try:
                        recovery_admitted = self._admit_scientific_blocker_recovery(
                            stage, error, completed, by_id,
                            specialist_verifier=specialist_verifier)
                    except (ComposerHardDeadlineExceeded, ProviderCooldownError,
                            QuotaExceededError, ValidationError):
                        recovery_admitted = False
                    if recovery_admitted:
                        blocker["recovery"] = "cycle_admitted"
                        self.stage_records[stage_id]["status"] = "retrying"
                        self._checkpoint(f"{stage_id}:scientific_recovery_admitted", force=True)
                        progress = True
                        break
                    try:
                        self._resolve_terminal_stage_work_orders(stage)
                    except (NotFoundError, StateError, ValidationError) as work_order_error:
                        blocker["work_order_resolution_error"] = (
                            f"{type(work_order_error).__name__}: {work_order_error}")
                    self.status = "blocked"
                    self._checkpoint(f"{stage_id}:blocked", force=True)
                    return self._finish()
                if not progress:
                    self.status = "blocked"
                    self.blockers.append({"reason": "composer made no dependency-respecting progress"})
                    self._checkpoint("blocked_no_progress", force=True)
                    return self._finish()
            # A stage can finish its bounded execution while its artifact is
            # still a candidate.  Preserve that distinction at the workflow
            # level so the CLI and any scheduler cannot mistake a candidate
            # for a completed release.
            research_stage = any(
                row.get("status") == "research_expansion_required"
                or self.context.get(stage_id, {}).get("status") == "research_expansion_required"
                for stage_id, row in self.stage_records.items())
            rejected_stage = any(
                row.get("status") == "review_rejected"
                or self.context.get(stage_id, {}).get("status") == "review_rejected"
                for stage_id, row in self.stage_records.items())
            candidate_stage = any(
                row.get("status") == "candidate_needs_review"
                or self.context.get(stage_id, {}).get("status") == "candidate_needs_review"
                for stage_id, row in self.stage_records.items())
            self.status = ("research_expansion_required" if research_stage
                           else "review_rejected" if rejected_stage
                           else "candidate_needs_review" if candidate_stage else "completed")
            self._checkpoint("complete_proposed", force=True)
            return self._finish()
        except ComposerHardDeadlineExceeded as exc:
            # Reaching the wall after useful research is a resumable mission
            # pause, not a scientific blocker.  A workflow that was already
            # expired before doing any work remains blocked because there is
            # no execution state to resume without an explicit extension.
            had_research_activity = bool(
                self.stage_records or self.context or self.agenda_decisions)
            self.status = "paused" if had_research_activity else "blocked"
            self.blockers.append({
                "stage_id": "workflow",
                "reason": str(exc),
                "stop_reason": "hard_deadline",
            })
            self._checkpoint(
                "paused_deadline" if had_research_activity else "blocked_deadline",
                force=True)
            return self._finish()
        except KeyboardInterrupt as exc:
            # A process-level stop is a resumable pause, not a failed
            # workflow.  Persist it here so the dashboard and the next
            # resume observe the same durable state.
            self.status = "paused"
            self.blockers.append({
                "stage_id": "workflow",
                "reason": f"{type(exc).__name__}: {exc}",
                "stop_reason": "process_interrupted",
            })
            self._checkpoint("paused_process_interruption", force=True)
            return self._finish()
        except Exception as exc:
            self.status = "blocked"
            self.blockers.append({"reason": f"{type(exc).__name__}: {exc}"})
            self._checkpoint("blocked", force=True)
            return self._finish()
        finally:
            self.close()

    def _finish(self):
        deadline_exhausted = self._deadline_exhausted()
        # A provider may return just after the fence even when admission was
        # valid.  Such an artifact remains inspectable, but the mission must
        # not report graph completion after its hard wall has passed.
        if deadline_exhausted and self.status not in {"blocked", "paused"}:
            self.status = "blocked"
            self.blockers.append({
                "stage_id": "workflow",
                "reason": "hard_deadline_after_stage_completion",
            })
        elapsed = max(0.0, self.clock() - self.started)
        if self.control is not None and self.active_research_requests:
            paused_orders = self.departments.pause_work_orders(
                self.active_research_requests,
                reason="Composer process ended; continuation order is dormant until resume",
            )
            if paused_orders:
                self.department_activity.append({
                    "cycle": self.continuation_cycles,
                    "action": "pause_work_orders_on_process_exit",
                    "work_orders": paused_orders,
                })
        if self.control is not None:
            # Final publication is on the Composer thread, so it is safe to
            # refresh the durable organization projection here.
            self.organization_snapshot = deepcopy(self.departments.snapshot())
        interim = None
        interim_error = None
        if deadline_exhausted or self.status in {"blocked", "paused"}:
            try:
                stop_reason = "hard_deadline" if deadline_exhausted else None
                if stop_reason is None and self.status == "paused" and any(
                        isinstance(item, dict)
                        and item.get("reason") == "required_stage_window_does_not_fit_remaining_deadline"
                        for item in self.blockers):
                    stop_reason = "required_stage_window_does_not_fit_remaining_deadline"
                if stop_reason is None and self.status in {"paused", "blocked"} and any(
                        isinstance(item, dict)
                        and item.get("reason") == "provider_cooldown"
                        for item in self.blockers):
                    stop_reason = "provider_cooldown"
                if stop_reason is None and self.status in {"paused", "blocked"} and any(
                        isinstance(item, dict)
                        and item.get("stop_reason") == "provider_configuration"
                        for item in self.blockers):
                    stop_reason = "provider_configuration"
                if stop_reason is None and self.status in {"paused", "blocked"} and any(
                        isinstance(item, dict)
                        and item.get("stop_reason") == "missing_stage_input"
                        for item in self.blockers):
                    stop_reason = "missing_stage_input"
                if stop_reason is None and self.status in {"paused", "blocked"} and any(
                        isinstance(item, dict)
                        and item.get("stop_reason") == "stage_quota_exhausted"
                        for item in self.blockers):
                    stop_reason = "stage_quota_exhausted"
                if stop_reason is None and self.status == "paused" and any(
                        isinstance(item, dict)
                        and item.get("stop_reason") == "process_interrupted"
                        for item in self.blockers):
                    stop_reason = "process_interrupted"
                interim = self.interim_report(
                    stop_reason=stop_reason or self.status)
            except Exception as exc:
                # The run report remains publishable even if a secondary
                # progress projection encounters an I/O or ledger failure.
                interim_error = f"{type(exc).__name__}: {exc}"
        remaining_seconds = max(0.0, self.deadline - self.clock())
        if isinstance(self.deadline_epoch, (int, float)) and math.isfinite(self.deadline_epoch):
            remaining_seconds = max(0.0, min(remaining_seconds, self.deadline_epoch - time.time()))
        active_blockers = self._active_blockers()
        result = {
            "schema_version": RUN_SCHEMA_VERSION, "run_id": self.run_id, "workflow_id": self.workflow["id"],
            "status": self.status, "stages": deepcopy(self.stage_records), "context": deepcopy(self.context),
            "feedback": deepcopy(self.feedback), "blockers": deepcopy(self.blockers),
            "active_blockers": deepcopy(active_blockers),
            "blocker_counts": {
                "active": len(active_blockers),
                "historical": len(self.blockers),
            },
            "usage": deepcopy(self.usage),
            "foundry_usage": deepcopy(self.foundry_usage),
            "organization": deepcopy(self.organization_snapshot),
            "retry_policy": self._retry_policy(),
            "continuation_policy": self._continuation_policy(),
            "agenda_policy": self._agenda_policy(),
            "agenda_decisions": deepcopy(self.agenda_decisions),
            "retry_schedule": deepcopy(self.retry_schedule),
            "state_revision": self.state_revision,
            "research_state": self._research_state(),
            "continuation_cycles": self.continuation_cycles,
            "continuation_budget_baseline": self._continuation_budget_baseline,
            "continuation_budget_used": self._continuation_budget_used(),
            "reopened_stage_ids": sorted(self.reopened_stage_ids),
            "continuation_pending_stage_ids": sorted(self.continuation_pending_stage_ids),
            "active_research_requests": deepcopy(self.active_research_requests),
            "department_activity": deepcopy(self.department_activity),
            "deadline_decisions": deepcopy(self.deadline_decisions),
            "deadline_extensions": deepcopy(self.deadline_extensions),
            "topic_history_path": str(self.topic_history_path),
            "topic_history_scope": self.topic_history_scope,
            "topic_history_entries": len(self.topic_history.get("entries", [])),
            "started_at_epoch": self.started_epoch, "deadline_at_epoch": self.deadline_epoch,
            "elapsed_seconds": elapsed, "remaining_seconds": remaining_seconds,
            "deadline_seconds": self.workflow["time_policy"]["hard_seconds"],
            "release_status": (
                "research_expansion_required" if self.status == "research_expansion_required"
                else "review_rejected" if self.status == "review_rejected"
                else "candidate_needs_review" if self.status == "candidate_needs_review"
                else "needs_human_approval" if self.status == "completed"
                and self.workflow["completion"]["release_requires_human"]
                else "not_released"
            ),
            "event_chain": self.control.verify_chain() if self.control is not None else None,
            "interim_report_path": interim.get("path") if isinstance(interim, dict) else None,
        }
        if isinstance(interim, dict):
            result["interim_report"] = deepcopy(interim)
        if interim_error:
            result["interim_report_error"] = interim_error
        if self.control is not None:
            self._publish("command/composer/run", "report", result, "command.composer")
            output = self.root / "output"
            output.mkdir(parents=True, exist_ok=True)
            (output / "run.json").write_bytes(canonical_bytes(result))
            self.on_progress({"phase": self.status, "elapsed_seconds": round(elapsed, 2),
                              "stages": deepcopy(self.stage_records),
                              "blockers": deepcopy(result["active_blockers"]),
                              "active_blockers": deepcopy(result["active_blockers"]),
                              "blocker_counts": deepcopy(result["blocker_counts"]),
                              "usage": deepcopy(result["usage"]),
                              "foundry_usage": deepcopy(result["foundry_usage"]),
                              "organization": deepcopy(result["organization"])})
        return result


def read_interim_report(project_dir):
    """Load the latest concise Composer interim report from a project."""
    path = Path(project_dir).resolve() / "output" / "interim_report.json"
    if path.is_file():
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValidationError(f"Composer interim report is not valid JSON: {path}") from exc
        if not isinstance(report, dict) or report.get("schema_version") != "composer-interim-report-1":
            raise ValidationError(f"Composer interim report has an unsupported schema: {path}")
        return report

    # A hard process interruption can occur before ``_finish`` gets to write
    # the projection.  The live checkpoint is still an inspectable, atomic
    # source for a short recovery report in that case.
    checkpoint_path = path.parent / "progress.json"
    if not checkpoint_path.is_file():
        raise ValidationError(f"Composer interim report does not exist: {path}")
    try:
        checkpoint = json.loads(checkpoint_path.read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError(f"Composer progress checkpoint is not valid JSON: {checkpoint_path}") from exc
    if not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != "composer-checkpoint-1":
        raise ValidationError(f"Composer progress checkpoint has an unsupported schema: {checkpoint_path}")
    stages = checkpoint.get("stages", {})
    if not isinstance(stages, dict):
        stages = {}
    workflow = None
    control = None
    workflow_error = None
    try:
        control = ControlStore(project_dir)
        artifacts = ArtifactStore(control)
        workflow_head = artifacts.head("command/composer/workflow")
        if workflow_head is not None:
            workflow = json.loads(artifacts.read_body(workflow_head["body_hash"]))
    except Exception as exc:
        workflow = None
        workflow_error = f"{type(exc).__name__}: {exc}"
    finally:
        if control is not None:
            control.close()
    workflow_stages = workflow.get("stages", []) if isinstance(workflow, dict) else []
    ordered_ids = [stage["id"] for stage in workflow_stages
                   if isinstance(stage, dict) and isinstance(stage.get("id"), str)]
    stage_kind_by_id = {stage["id"]: stage.get("kind") for stage in workflow_stages
                        if isinstance(stage, dict) and isinstance(stage.get("id"), str)}
    if not ordered_ids:
        ordered_ids = sorted(stages)
    rows = []
    completed, active, pending, held, blocked = [], [], [], [], []
    for stage_id in ordered_ids:
        record = stages.get(stage_id, {}) if isinstance(stages.get(stage_id, {}), dict) else {}
        status = record.get("status", "pending")
        attempts = record.get("attempt_count", len(record.get("attempts", []))
                              if isinstance(record.get("attempts", []), list) else 0)
        row = {"id": stage_id, "kind": record.get("kind") or stage_kind_by_id.get(stage_id), "status": status,
               "attempts": attempts}
        if isinstance(record.get("output_path"), str):
            row["output_path"] = record["output_path"]
        if isinstance(record.get("error"), str):
            row["error"] = record["error"][:240]
        if record.get("forward_progress") is True:
            row["forward_progress"] = True
            row["release_blocking"] = record.get("release_blocking", True)
            debt = record.get("failure_debt")
            if isinstance(debt, dict):
                row["failure_debt"] = {
                    key: deepcopy(debt.get(key))
                    for key in ("failure_class", "error", "attempts", "next_action", "release_blocking")
                    if key in debt
                }
        rows.append(row)
        if status in STAGE_READY_STATUSES:
            completed.append(stage_id)
        elif status in {"running", "retrying"}:
            active.append(stage_id)
        elif status in STAGE_HOLD_STATUSES:
            held.append(stage_id)
        elif status == "blocked":
            blocked.append(stage_id)
        else:
            pending.append(stage_id)
    started_at_epoch = checkpoint.get("started_at_epoch")
    deadline_at_epoch = checkpoint.get("deadline_at_epoch")
    duration = None
    if (type(started_at_epoch) in (int, float) and type(deadline_at_epoch) in (int, float)
            and math.isfinite(started_at_epoch) and math.isfinite(deadline_at_epoch)
            and deadline_at_epoch >= started_at_epoch):
        duration = deadline_at_epoch - started_at_epoch
    report = {
        "schema_version": "composer-interim-report-1",
        "workflow_id": checkpoint.get("workflow_id"),
        "run_id": checkpoint.get("run_id"),
        "status": "interrupted",
        "stop_reason": "process_interrupted",
        "last_phase": checkpoint.get("phase"),
        "elapsed_seconds": checkpoint.get("elapsed_seconds", 0.0),
        "remaining_seconds": checkpoint.get("remaining_seconds", 0.0),
        "deadline_seconds": workflow.get("time_policy", {}).get("hard_seconds", duration)
        if isinstance(workflow, dict) else duration,
        "started_at_epoch": started_at_epoch,
        "deadline_at_epoch": deadline_at_epoch,
        "completed_stage_ids": completed,
        "active_stage_ids": active,
        "pending_stage_ids": pending,
        "held_stage_ids": held,
        "blocked_stage_ids": blocked,
        "stages": rows,
        "pending_work_orders": (checkpoint.get("organization", {}) or {}).get("open_work_orders", [])[:8]
        if isinstance(checkpoint.get("organization"), dict) else [],
        "blockers": checkpoint.get("blockers", [])[-3:]
        if isinstance(checkpoint.get("blockers"), list) else [],
        "active_blockers": checkpoint.get("active_blockers", [])
        if isinstance(checkpoint.get("active_blockers"), list) else [],
        "blocker_counts": checkpoint.get("blocker_counts", {
            "active": len(checkpoint.get("active_blockers", []))
            if isinstance(checkpoint.get("active_blockers"), list) else 0,
            "historical": len(checkpoint.get("blockers", []))
            if isinstance(checkpoint.get("blockers"), list) else 0,
        }),
        "deadline_extensions": checkpoint.get("deadline_extensions", []),
        "next_actions": ["Inspect retained stage outputs and resume the same workflow."],
        "resume_hint": (
            "python -m scisaurus.cli run-composer --workflow "
            "<path-to-the-same-workflow.json> --resume --extend-deadline-seconds <N>"
        ),
        "source": str(checkpoint_path.resolve()),
    }
    if workflow_error:
        report["workflow_metadata_error"] = workflow_error
    return report


__all__ = ["SCHEMA_VERSION", "RUN_SCHEMA_VERSION", "validate_workflow", "ComposerRunner",
           "read_interim_report"]
