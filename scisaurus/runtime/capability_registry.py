"""Pinned capability registry for admitted model-authored programs (foundry P3.4).

Only a candidate that passed every admission gate may be registered.  The
registration writes the two pinned program sources into a content-addressed,
never-overwritten directory, builds the frozen ``experiment-capability-1``
descriptor around them, validates it with the repository's own experiment
contract, and appends the entry to a registry index.

The registry is the bridge back into the Composer: a workflow's
``experiment_catalog`` can point at any registered descriptor exactly like the
shipped pinned capabilities.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.experiment_config import validate_experiment_config

REGISTRY_SCHEMA = "experiment-capability-registry-1"
DESCRIPTOR_SCHEMA = "experiment-capability-1"


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _trial_config(experiment, runtime_python, repo_root):
    return {
        "live_dispatch_allowed": True, "data_classification": "public",
        "allocation_mode": "capacity_pool", "project_id": "registry-check",
        "objective": "registry check", "supplied_context": "registry check",
        "model": {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
                  "model": "placeholder", "timeout_seconds": 60, "max_output_tokens": 32,
                  "reasoning_effort": "none", "output_format": "json_object"},
        "limits": {"max_rounds": 2, "wall_clock_seconds": 3600, "checkpoint_seconds": 10,
                   "max_result_bytes": 10000000, "concurrent_calls": 3, "worker_concurrency": 1},
        "time_policy": {"first_result_seconds": 60, "target_seconds": 120, "hard_seconds": 3600},
        "experiment": experiment,
    }


def register_capability(root, candidate, admission, *, runtime_python, repo_root,
                        literature_gate=None, requirements_file=None):
    """Pin an admitted candidate into the registry.  Never overwrites."""
    from scisaurus.runtime.program_gates import ADMISSION_SCHEMA
    from scisaurus.runtime.program_admission import validate_program_candidate

    validate_program_candidate(candidate)
    if not isinstance(admission, dict) or admission.get("schema_version") != ADMISSION_SCHEMA:
        raise ValidationError("capability registration requires an admission record")
    if admission.get("study_id") != candidate["study_id"] or admission.get("revision") != candidate["revision"]:
        raise ValidationError("admission record does not match the candidate identity")
    for key, source in (("executor_source_sha256", candidate["executor_source"]),
                        ("validator_source_sha256", candidate["validator_source"])):
        if admission.get(key) != hashlib.sha256(source.encode()).hexdigest():
            raise ValidationError(f"admission {key} does not match the supplied source")
    runtime_python = Path(runtime_python)
    if not runtime_python.is_absolute() or not runtime_python.is_file():
        raise ValidationError("capability runtime_python must be an existing absolute file")
    repo_root = Path(repo_root).resolve()
    requirements = Path(requirements_file) if requirements_file else repo_root / "requirements-experiment.txt"
    if not requirements.is_file():
        raise ValidationError("capability requirements file must exist")

    root = Path(root)
    revision_dir = root / "capabilities" / candidate["study_id"] / f"r{candidate['revision']}"
    if revision_dir.exists():
        raise ValidationError("capability revision already exists; revisions are never overwritten")
    revision_dir.mkdir(parents=True)
    executor_path = revision_dir / "executor.py"
    validator_path = revision_dir / "validator.py"
    executor_path.write_text(candidate["executor_source"])
    validator_path.write_text(candidate["validator_source"])

    intent = json.loads(canonical_bytes(candidate["experiment_intent"]).decode())
    experiment = {
        **intent,
        "literature_gate": literature_gate,
        "execution": _program("generated_executor", runtime_python, executor_path, repo_root,
                              requirements, 900, 60000000),
        "validation": _program("generated_validator", runtime_python, validator_path, repo_root,
                               requirements, 300, 5000000),
    }
    descriptor = {"schema_version": DESCRIPTOR_SCHEMA, "capability_id": candidate["study_id"],
                  "experiment": experiment}
    validate_experiment_config(_trial_config(json.loads(canonical_bytes(experiment).decode()),
                                             runtime_python, repo_root))
    descriptor_path = revision_dir / "capability.json"
    descriptor_path.write_text(json.dumps(descriptor, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    index_path = root / "capabilities" / "index.json"
    if index_path.is_file():
        try:
            index = json.loads(index_path.read_text())
        except ValueError as exc:
            raise ValidationError("capability registry index is unreadable") from exc
        if not isinstance(index, dict) or index.get("schema_version") != REGISTRY_SCHEMA:
            raise ValidationError("capability registry index has an unsupported schema")
    else:
        index = {"schema_version": REGISTRY_SCHEMA, "capabilities": []}
    index["capabilities"].append({
        "id": candidate["study_id"], "revision": candidate["revision"],
        "path": str(descriptor_path), "descriptor_sha256": _digest(descriptor),
        "executor_sha256": admission["executor_source_sha256"],
        "validator_sha256": admission["validator_source_sha256"],
    })
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"descriptor_path": str(descriptor_path), "index_path": str(index_path),
            "capability_id": candidate["study_id"], "revision": candidate["revision"]}


def _program(identifier, runtime_python, source_path, repo_root, requirements, timeout, max_bytes):
    return {
        "id": identifier, "adapter": "local_program",
        "client": {"command": [str(runtime_python), str(source_path)], "timeout": timeout,
                   "max_bytes": max_bytes, "cwd": str(repo_root), "env": {}, "own_process_group": False},
        "representative": {"input": {"probe": True}},
        "environment_files": [str(requirements), str(source_path)],
        "input": {},
    }


def load_registry(root):
    index_path = Path(root) / "capabilities" / "index.json"
    if not index_path.is_file():
        return {"schema_version": REGISTRY_SCHEMA, "capabilities": []}
    index = json.loads(index_path.read_text())
    if not isinstance(index, dict) or index.get("schema_version") != REGISTRY_SCHEMA:
        raise ValidationError("capability registry index has an unsupported schema")
    return index
