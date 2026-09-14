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
import re
import sys
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.experiment_config import validate_experiment_config

SCHEMA_VERSION = "method-program-candidate-1"

# The sandbox (P3.2) is the real boundary; this allowlist keeps the admission
# surface small and reviewable and is the first of the five gates.
ALLOWED_IMPORTS = frozenset({
    "abc", "array", "base64", "binascii", "bisect", "cmath", "collections", "contextlib",
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
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def scan_program_source(source, name):
    """Static admission gate for one program source.  Returns the module roots."""
    _text(source, f"{name} source")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ValidationError(f"{name} source is not valid Python: {exc.msg}") from exc
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
    forbidden = sorted(roots & FORBIDDEN_IMPORT_ROOTS)
    if forbidden:
        raise ValidationError(f"{name} source imports forbidden modules: {forbidden}")
    unknown = sorted(roots - ALLOWED_IMPORTS)
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


def _validate_intent(intent):
    if not isinstance(intent, dict) or set(intent) != INTENT_FIELDS:
        observed = sorted(intent) if isinstance(intent, dict) else type(intent).__name__
        raise ValidationError(
            f"experiment_intent requires exactly {sorted(INTENT_FIELDS)}; observed keys: {observed}")
    _identifier(intent["id"], "experiment_intent id")
    if type(intent["revision"]) is not int or intent["revision"] < 1:
        raise ValidationError("experiment_intent revision must be a positive integer")
    if intent["study_type"] not in STUDY_TYPES:
        raise ValidationError("experiment_intent study_type is unsupported")
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
            raise ValidationError("experiment_intent primary outcome direction is unsupported")
        if outcome["threshold"] is not None and not isinstance(outcome["threshold"], (int, float)):
            raise ValidationError("experiment_intent primary outcome threshold must be numeric or null")
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
    if not isinstance(stage_seconds, dict) or not stage_seconds:
        raise ValidationError("experiment_intent stage_seconds must be a nonempty object")
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
    validate_experiment_config(trial)
    return intent


def validate_program_candidate(value):
    """Validate one model-proposed program candidate before any execution."""
    if not isinstance(value, dict) or set(value) != CANDIDATE_FIELDS:
        raise ValidationError(f"program candidate requires exactly {sorted(CANDIDATE_FIELDS)}")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError("program candidate schema version is unsupported")
    _identifier(value["study_id"], "program candidate study_id")
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValidationError("program candidate revision must be a positive integer")
    _validate_intent(value["experiment_intent"])
    if value["study_id"] != value["experiment_intent"]["id"]:
        raise ValidationError("program candidate study_id must match its experiment_intent id")
    if value["revision"] != value["experiment_intent"]["revision"]:
        raise ValidationError("program candidate revision must match its experiment_intent revision")
    scan_program_source(value["executor_source"], "program executor")
    scan_program_source(value["validator_source"], "program validator")
    if value["executor_source"].strip() == value["validator_source"].strip():
        raise ValidationError("program executor and validator sources must be independently authored")
    _validate_runtime(value["runtime"])
    _validate_test_vector(value["test_vector"])
    canonical_bytes(value)
    return value
