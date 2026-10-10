"""Admission contract for model-authored experiment programs (foundry P3.1).

A generative capability must never execute model-authored code directly.  This
module defines the immutable candidate record the model may propose and the
static admission checks that run before any sandboxed execution (P3.2), the
independent recalculation and adversarial review (P3.3), and pinning into the
capability registry (P3.4).

The record carries:

* the study intent (reusing the frozen experiment-score contract),
* the executor program source and the *independently authored* validator source,
* a pinned runtime (python version + exact package==version pins),
* a deterministic test vector with the expected output digest.

Only after the static scan, deterministic replay, independent recalculation and
review gates pass may a candidate become an ``experiment-capability-1`` entry.
"""
from __future__ import annotations

import ast
import json
import math
import re
import sys
from pathlib import Path

from scisaurus.runtime.measurement_contract import INTENT_EXTENSIONS, ModelDefinitionError, recalculation_outcomes, validate_model_definition
from scisaurus.core.errors import ModelContractError, ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.experiment_config import validate_experiment_config
from scisaurus.runtime.time_policy import STAGES

SCHEMA_VERSION = "method-program-candidate-1"


class ExperimentIntentContractError(ModelContractError):
    """A model-authored experiment intent violated its frozen response contract."""

# The sandbox (P3.2) is the real boundary; this allowlist keeps the admission
# surface small and reviewable and is the first of the five gates.
ALLOWED_IMPORTS = frozenset({
    "__future__", "abc", "array", "base64", "binascii", "bisect", "cmath", "collections", "contextlib",
    "copy", "dataclasses", "decimal", "enum", "fractions", "functools", "hashlib", "heapq",
    "io", "itertools", "json", "math", "matplotlib", "numbers", "numpy", "operator", "os",
    "pathlib", "random", "re", "statistics", "string", "struct", "sys", "textwrap", "time",
    "types", "typing", "warnings", "zlib",
})
FORBIDDEN_IMPORT_ROOTS = frozenset({
    "asyncio", "concurrent", "ctypes", "ftplib", "http", "multiprocessing", "pickle",
    "requests", "shutil", "smtplib", "socket", "socketserver", "ssl", "subprocess",
    "telnetlib", "threading", "urllib", "webbrowser", "xmlrpc",
})
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__", "input", "breakpoint", "open"})
FORBIDDEN_ATTRIBUTES = frozenset({
    ("os", "system"), ("os", "popen"), ("os", "fork"), ("os", "execv"), ("os", "execve"),
    ("os", "spawnv"), ("os", "spawnl"), ("os", "remove"), ("os", "rmdir"), ("os", "unlink"),
    ("shutil", "rmtree"), ("sys", "settrace"), ("sys", "setprofile"), ("builtins", "open"),
})
HEX64 = re.compile(r"[0-9a-f]{64}")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}")
STUDY_TYPES = frozenset({"novel_research", "replication", "methods_validation", "exploratory"})
DIRECTIONS = frozenset({"higher", "lower", "descriptive"})

INTENT_FIELDS = frozenset({
    "id", "revision", "study_type", "domain", "research_question", "hypothesis", "method",
    "parameters", "seed", "run_count", "stopping_rule", "primary_outcomes", "limitations",
    "required_assets", "reviewers", "stage_seconds", "max_observations", "max_asset_bytes",
})
CANDIDATE_FIELDS = frozenset({
    "schema_version", "study_id", "revision", "executor_source", "validator_source",
    "runtime", "test_vector", "experiment_intent",
})


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValidationError(f"{name} {value!r} must match {IDENTIFIER.pattern!r}; "
                              "keep the declared ID, executor metric ID and validator references identical")
    return value


def is_main_entry_guard(node):
    """Recognize either operand order of Python's main-module equality guard."""
    if not (isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and len(node.test.ops) == 1
            and isinstance(node.test.ops[0], ast.Eq)
            and len(node.test.comparators) == 1):
        return False
    left, right = node.test.left, node.test.comparators[0]
    return (
        isinstance(left, ast.Name) and left.id == "__name__"
        and isinstance(right, ast.Constant) and right.value == "__main__"
    ) or (
        isinstance(right, ast.Name) and right.id == "__name__"
        and isinstance(left, ast.Constant) and left.value == "__main__"
    )


def scan_program_source(source, name, *, laboratory_execution=None):
    """Static admission gate for one program source.  Returns the module roots."""
    _text(source, f"{name} source")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValidationError(f"{name} source is not valid Python: {exc.msg}") from exc
    top_level_names = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_level_names.setdefault(node.name, []).append(node.lineno)
    duplicates = {key: lines for key, lines in top_level_names.items() if len(lines) > 1}
    if duplicates:
        details = ", ".join(
            f"{key} at lines {','.join(map(str, lines))}"
            for key, lines in sorted(duplicates.items()))
        raise ValidationError(f"{name} source has duplicate top-level definitions: {details}")
    entry_guards = [node for node in tree.body if is_main_entry_guard(node)]
    if len(entry_guards) > 1:
        lines = ",".join(str(node.lineno) for node in entry_guards)
        raise ValidationError(
            f"{name} source has multiple __main__ entry guards at lines {lines}")
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise ValidationError(f"{name} source must not use relative imports")
            if node.module:
                roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                raise ValidationError(f"{name} source calls the forbidden function {func.id}")
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                if pair in FORBIDDEN_ATTRIBUTES:
                    raise ValidationError(f"{name} source calls the forbidden attribute {'.'.join(pair)}")
    permitted = set(ALLOWED_IMPORTS)
    forbidden_roots = set(FORBIDDEN_IMPORT_ROOTS)
    if laboratory_execution is not None:
        from scisaurus.runtime.laboratory import LaboratoryBinding
        LaboratoryBinding.from_execution_binding(laboratory_execution)
        permitted.add("subprocess")
        forbidden_roots.remove("subprocess")
    forbidden = sorted(roots & forbidden_roots)
    if forbidden:
        raise ValidationError(f"{name} source imports forbidden modules: {forbidden}")
    unknown = sorted(roots - permitted)
    if unknown:
        raise ValidationError(f"{name} source imports modules outside the pinned allowlist: {unknown}")
    return roots


def _validate_runtime(value):
    if not isinstance(value, dict) or set(value) != {"python", "packages"}:
        raise ValidationError("program runtime requires exactly python and packages")
    _text(value["python"], "program runtime python")
    packages = value["packages"]
    if not isinstance(packages, list) or not packages:
        raise ValidationError("program runtime requires pinned package==version entries")
    names = set()
    for entry in packages:
        if not isinstance(entry, dict) or set(entry) != {"name", "version"}:
            raise ValidationError("program runtime package entries require exactly name and version")
        _text(entry["name"], "program runtime package name")
        _text(entry["version"], "program runtime package version")
        if entry["name"] in names:
            raise ValidationError("program runtime package names must be unique")
        names.add(entry["name"])
    return value


def _validate_test_vector(value):
    if not isinstance(value, dict) or set(value) != {"input", "expected_output_sha256"}:
        raise ValidationError("program test_vector requires exactly input and expected_output_sha256")
    try:
        canonical_bytes(value["input"])
    except (ValueError, RecursionError) as exc:
        raise ValidationError("program test_vector input must be a finite JSON object") from exc
    if not isinstance(value["expected_output_sha256"], str) or not HEX64.fullmatch(value["expected_output_sha256"]):
        raise ValidationError("program test_vector requires a lowercase 64-hex output digest")
    return value


def _validate_experiment_intent(intent):
    if (not isinstance(intent, dict) or not INTENT_FIELDS.issubset(intent)
            or set(intent) - (INTENT_FIELDS | {"quality_contract"} | INTENT_EXTENSIONS)):
        observed = sorted(intent) if isinstance(intent, dict) else type(intent).__name__
        raise ValidationError(
            f"experiment_intent requires {sorted(INTENT_FIELDS)} and permits quality_contract; observed keys: {observed}")
    _identifier(intent["id"], "experiment_intent id")
    if type(intent["revision"]) is not int or intent["revision"] < 1:
        raise ValidationError("experiment_intent revision must be a positive integer")
    if intent["study_type"] not in STUDY_TYPES:
        raise ValidationError(
            f"experiment_intent study_type {intent['study_type']!r} must be one of {sorted(STUDY_TYPES)}")
    for key in ("domain", "research_question", "hypothesis", "method", "stopping_rule"):
        _text(intent[key], f"experiment_intent.{key}")
    try:
        canonical_bytes(intent["parameters"])
    except (ValueError, RecursionError) as exc:
        raise ValidationError("experiment_intent parameters must be finite JSON") from exc
    if type(intent["seed"]) is not int or intent["seed"] < 0:
        raise ValidationError("experiment_intent seed must be a non-negative integer")
    if type(intent["run_count"]) is not int or intent["run_count"] < 1:
        raise ValidationError("experiment_intent run_count must be a positive integer")
    for key in ("max_observations", "max_asset_bytes"):
        if type(intent[key]) is not int or intent[key] < 1:
            raise ValidationError(f"experiment_intent.{key} must be a positive integer")
    outcomes = intent["primary_outcomes"]
    if not isinstance(outcomes, list) or not outcomes:
        raise ValidationError("experiment_intent primary_outcomes must be nonempty")
    seen = set()
    for outcome in outcomes:
        if not isinstance(outcome, dict) or set(outcome) != {"id", "definition", "unit", "direction", "threshold"}:
            raise ValidationError("experiment_intent primary outcome has an invalid shape")
        _identifier(outcome["id"], "primary outcome id")
        if outcome["id"] in seen:
            raise ValidationError("experiment_intent primary outcome ids must be unique")
        seen.add(outcome["id"])
        _text(outcome["definition"], "primary outcome definition")
        _text(outcome["unit"], "primary outcome unit")
        if outcome["direction"] not in DIRECTIONS:
            raise ValidationError(
                f"experiment_intent primary outcome direction {outcome['direction']!r} must be one of {sorted(DIRECTIONS)}")
        if outcome["threshold"] is not None and (type(outcome["threshold"]) not in (int, float) or (type(outcome["threshold"]) is float and not math.isfinite(outcome["threshold"]))):
            raise ValidationError("experiment_intent primary outcome threshold must be numeric or null")
    recalculation_outcomes(intent)
    validate_model_definition(intent)
    if not isinstance(intent["limitations"], list) or not intent["limitations"]:
        raise ValidationError("experiment_intent limitations must be nonempty")
    for limitation in intent["limitations"]:
        _text(limitation, "experiment_intent limitation")
    assets = intent["required_assets"]
    if not isinstance(assets, list):
        raise ValidationError("experiment_intent required_assets must be a list")
    roles = set()
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != {"role", "media_types", "min_count"}:
            raise ValidationError("experiment_intent required asset has an invalid shape")
        _identifier(asset["role"], "required asset role")
        if asset["role"] in roles:
            raise ValidationError("experiment_intent required asset roles must be unique")
        roles.add(asset["role"])
        if (not isinstance(asset["media_types"], list) or not asset["media_types"]
                or any(not isinstance(item, str) or not item for item in asset["media_types"])):
            raise ValidationError("experiment_intent required asset media_types must be a nonempty list")
        if type(asset["min_count"]) is not int or asset["min_count"] < 1:
            raise ValidationError("experiment_intent required asset min_count must be positive")
    reviewers = intent["reviewers"]
    if not isinstance(reviewers, list) or not 2 <= len(reviewers) <= 6:
        raise ValidationError("experiment_intent requires two to six reviewers")
    ids = set()
    for reviewer in reviewers:
        if not isinstance(reviewer, dict) or set(reviewer) != {"id", "focus"}:
            raise ValidationError("experiment_intent reviewer has an invalid shape")
        _identifier(reviewer["id"], "reviewer id")
        if reviewer["id"] in ids:
            raise ValidationError("experiment_intent reviewer ids must be unique")
        ids.add(reviewer["id"])
        _text(reviewer["focus"], "reviewer focus")
    stage_seconds = intent["stage_seconds"]
    if not isinstance(stage_seconds, dict) or set(stage_seconds) != set(STAGES):
        raise ValidationError(
            f"experiment_intent stage_seconds requires exactly {list(STAGES)}")
    for value in stage_seconds.values():
        if type(value) not in (int, float) or value <= 0:
            raise ValidationError("experiment_intent stage_seconds values must be positive")
    # Reuse the frozen experiment-score contract by supplying placeholder local
    # programs.  The real program paths are produced later by P3.4 pinning.
    placeholder = str(Path(__file__).resolve())
    trial = {
        "live_dispatch_allowed": True, "data_classification": "public",
        "allocation_mode": "capacity_pool", "project_id": "program-candidate",
        "objective": "program candidate", "supplied_context": "program candidate",
        "model": {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
                  "model": "placeholder", "timeout_seconds": 60, "max_output_tokens": 32,
                  "reasoning_effort": "none", "output_format": "json_object"},
        "limits": {"max_rounds": 2, "wall_clock_seconds": 3600, "checkpoint_seconds": 10,
                   "max_result_bytes": 10000000, "concurrent_calls": 3, "worker_concurrency": 1},
        "time_policy": {"first_result_seconds": 60, "target_seconds": 120, "hard_seconds": 3600},
        "experiment": {
            **json.loads(canonical_bytes(intent).decode()),
            "literature_gate": None,
            "execution": {"id": "candidate_executor", "adapter": "local_program",
                          "client": {"command": [sys.executable, placeholder], "timeout": 60,
                                     "max_bytes": 1000000, "cwd": str(Path(__file__).resolve().parent),
                                     "env": {}, "own_process_group": False},
                          "representative": {"input": {"probe": True}},
                          "environment_files": [placeholder], "input": {}},
            "validation": {"id": "candidate_validator", "adapter": "local_program",
                           "client": {"command": [sys.executable, placeholder], "timeout": 60,
                                      "max_bytes": 1000000, "cwd": str(Path(__file__).resolve().parent),
                                      "env": {}, "own_process_group": False},
                           "representative": {"input": {"probe": True}},
                           "environment_files": [placeholder], "input": {}},
        },
    }
    validate_experiment_config(trial, require_literature_gate=False)
    return intent


def validate_experiment_intent(intent):
    """Validate model-authored intent and preserve its response-contract type."""
    try:
        return _validate_experiment_intent(intent)
    except (ModelContractError, ModelDefinitionError):
        raise
    except ValidationError as exc:
        raise ExperimentIntentContractError(str(exc)) from exc


def format_recovery_intent_constraints(intent, required, *, configured_input, evidence_required=False):
    """Freeze validated declarations without promoting malformed response fields."""
    from copy import deepcopy
    from scisaurus.runtime.study_evidence import evidence_source_refs, validate_evidence_plan

    if not isinstance(intent, dict) or not isinstance(required, dict):
        raise ExperimentIntentContractError("format recovery intent and controller constraints must be objects")
    source_refs = evidence_source_refs(configured_input)
    base = {key: deepcopy(value) for key, value in intent.items()
            if key in INTENT_FIELDS or key == "quality_contract"}
    # Base declarations must validate before any authored science becomes a constraint.
    # A malformed base stays a response failure rather than an executable frozen plan.
    validate_experiment_intent(base)
    constraints = deepcopy(base)
    diagnostics = {key: "experiment_intent field is not declared by the response contract"
                   for key in set(intent) - (INTENT_FIELDS | {"quality_contract"} | INTENT_EXTENSIONS)}
    groups = (("model_definition",), ("decision_outcomes", "decision_rules"), ("evidence_plan",))
    for group in groups:
        fields = {key: deepcopy(intent[key]) for key in group if key in intent}
        if not fields:
            continue
        trial = {**constraints, **fields}
        try:
            validate_experiment_intent(trial)
            if "evidence_plan" in fields:
                validate_evidence_plan(trial, required=evidence_required, source_refs=source_refs)
        except ModelDefinitionError:
            raise
        except ExperimentIntentContractError as exc:
            diagnostics.update({key: str(exc) for key in fields})
        except ValidationError as exc:
            diagnostics.update({key: str(exc) for key in fields})
        else:
            constraints.update(fields)
    if evidence_required and "evidence_plan" not in intent:
        diagnostics["evidence_plan"] = "new computational study requires experiment_intent.evidence_plan"
    # Controller constraints remain authoritative, including when the response disagrees.
    # Invalid controller declarations cannot be repaired by releasing their constraints.
    constraints.update(deepcopy(required))
    validate_experiment_intent(constraints)
    validate_evidence_plan(constraints,
                          required=evidence_required and "evidence_plan" in required,
                          source_refs=source_refs)
    return constraints, diagnostics


def validate_program_candidate(value, *, laboratory_execution=None):
    """Validate one model-proposed program candidate before any execution."""
    if not isinstance(value, dict) or set(value) != CANDIDATE_FIELDS:
        raise ValidationError(f"program candidate requires exactly {sorted(CANDIDATE_FIELDS)}")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError("program candidate schema version is unsupported")
    _identifier(value["study_id"], "program candidate study_id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("program candidate revision must be a positive integer")
    validate_experiment_intent(value["experiment_intent"])
    if value["study_id"] != value["experiment_intent"]["id"]:
        raise ValidationError("program candidate study_id must match its experiment_intent id")
    if value["revision"] != value["experiment_intent"]["revision"]:
        raise ValidationError("program candidate revision must match its experiment_intent revision")
    scan_program_source(value["executor_source"], "program executor", laboratory_execution=laboratory_execution)
    scan_program_source(value["validator_source"], "program validator", laboratory_execution=laboratory_execution)
    if value["executor_source"].strip() == value["validator_source"].strip():
        raise ValidationError("program executor and validator sources must be independently authored")
    _validate_runtime(value["runtime"])
    _validate_test_vector(value["test_vector"])
    if "evidence_plan" in value["experiment_intent"]:
        from scisaurus.runtime.study_evidence import evidence_source_refs, validate_evidence_plan
        runtime_input = value["test_vector"]["input"]
        if not isinstance(runtime_input, dict):
            raise ValidationError("study evidence requires an object runtime input")
        configured = runtime_input.get("configured_input", {})
        validate_evidence_plan(value["experiment_intent"], source_refs=evidence_source_refs(configured))
    canonical_bytes(value)
    return value
