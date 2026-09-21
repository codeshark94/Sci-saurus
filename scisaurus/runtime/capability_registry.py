"""Crash-recoverable registry for admitted model-authored programs.

Every published revision binds the candidate, its admission record, both
program sources, and the executable experiment descriptor. Registration is
serialized, staged in a sibling directory, journaled, and atomically exposed.
Registry reads re-verify every digest before returning an entry.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import uuid

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.experiment_config import validate_experiment_config

REGISTRY_SCHEMA = "experiment-capability-registry-1"
DESCRIPTOR_SCHEMA = "experiment-capability-1"
TRANSACTION_SCHEMA = "experiment-capability-registry-transaction-1"
PROGRAM_EXPERIMENT_FIELDS = (
    "id", "revision", "study_type", "domain", "research_question", "hypothesis",
    "method", "parameters", "seed", "run_count", "stopping_rule",
    "primary_outcomes", "limitations",
)
ENTRY_FIELDS = {
    "id", "revision", "path", "descriptor_sha256", "executor_sha256",
    "validator_sha256", "candidate_record_sha256", "admission_sha256",
}
_REGISTRY_THREAD_LOCK = threading.RLock()


def _digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path, body):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _registry_lock(capabilities_root):
    capabilities_root.mkdir(parents=True, exist_ok=True)
    lock_path = capabilities_root / "index.lock"
    with _REGISTRY_THREAD_LOCK:
        lock = lock_path.open("a+")
        try:
            try:
                import fcntl
            except ImportError as exc:
                raise ValidationError(
                    "capability registry requires an interprocess file lock") from exc
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def _empty_index():
    return {"schema_version": REGISTRY_SCHEMA, "capabilities": []}


def _read_index(index_path):
    if not index_path.is_file():
        return _empty_index()
    try:
        index = json.loads(index_path.read_text())
    except (OSError, ValueError, TypeError) as exc:
        raise ValidationError("capability registry index is unreadable") from exc
    if (not isinstance(index, dict) or set(index) != {"schema_version", "capabilities"}
            or index.get("schema_version") != REGISTRY_SCHEMA
            or not isinstance(index.get("capabilities"), list)):
        raise ValidationError("capability registry index has an unsupported schema")
    identities = set()
    for entry in index["capabilities"]:
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            raise ValidationError("capability registry entry has an invalid shape")
        identity = (entry.get("id"), entry.get("revision"))
        if (not isinstance(identity[0], str) or type(identity[1]) is not int
                or identity[1] < 1 or identity in identities):
            raise ValidationError("capability registry entry identity is invalid or duplicated")
        identities.add(identity)
        for key in ENTRY_FIELDS - {"id", "revision", "path"}:
            value = entry.get(key)
            if (not isinstance(value, str) or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)):
                raise ValidationError(f"capability registry {key} is invalid")
        if not isinstance(entry.get("path"), str) or not Path(entry["path"]).is_absolute():
            raise ValidationError("capability registry descriptor path must be absolute")
    return index


def experiment_program_payload(intent, configured_input):
    """Build the exact executor envelope used by ExperimentRunner."""
    if not isinstance(configured_input, dict):
        raise ValidationError("generated experiment configured_input must be an object")
    try:
        experiment = {key: intent[key] for key in PROGRAM_EXPERIMENT_FIELDS}
    except (KeyError, TypeError) as exc:
        raise ValidationError("generated experiment intent cannot form a runtime payload") from exc
    if intent.get("quality_contract") is not None:
        experiment["quality_contract"] = intent["quality_contract"]
    return json.loads(canonical_bytes({
        "configured_input": configured_input,
        "experiment": experiment,
    }).decode())


def _registered_configured_input(candidate):
    supplied = candidate["test_vector"]["input"]
    if not isinstance(supplied, dict) or set(supplied) != {"configured_input", "experiment"}:
        raise ValidationError(
            "registrable program test input must use the ExperimentRunner execution envelope")
    expected = experiment_program_payload(
        candidate["experiment_intent"], supplied["configured_input"])
    if canonical_bytes(supplied) != canonical_bytes(expected):
        raise ValidationError(
            "registrable program test input does not match its experiment intent")
    return expected["configured_input"]


def _trial_config(experiment):
    return {
        "live_dispatch_allowed": True, "data_classification": "public",
        "allocation_mode": "capacity_pool", "project_id": "registry-check",
        "objective": "registry check", "supplied_context": "registry check",
        "model": {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
                  "model": "placeholder", "timeout_seconds": 60, "max_output_tokens": 32,
                  "reasoning_effort": "none", "output_format": "json_object"},
        "limits": {"max_rounds": 2, "wall_clock_seconds": 3600, "checkpoint_seconds": 10,
                   "max_result_bytes": 10000000, "concurrent_calls": 3,
                   "worker_concurrency": 1},
        "time_policy": {"first_result_seconds": 60, "target_seconds": 120,
                        "hard_seconds": 3600},
        "experiment": experiment,
    }


def _program(identifier, runtime_python, source_path, repo_root, requirements, timeout,
             max_bytes, *, configured_input=None, representative_input=None):
    representative_input = ({"readiness_probe": True}
                            if representative_input is None else representative_input)
    return {
        "id": identifier, "adapter": "local_program",
        "client": {"command": [str(runtime_python), str(source_path)], "timeout": timeout,
                   "max_bytes": max_bytes, "cwd": str(repo_root), "env": {},
                   "own_process_group": False, "sandbox_required": True},
        "representative": {"input": deepcopy_json(representative_input)},
        "environment_files": [str(requirements), str(source_path)],
        "input": deepcopy_json(configured_input) if configured_input is not None else {},
    }


def _experiment(candidate, runtime_python, repo_root, requirements, source_root,
                configured_input, literature_gate):
    intent = deepcopy_json(candidate["experiment_intent"])
    return {
        **intent,
        "literature_gate": literature_gate,
        "execution": _program(
            "generated_executor", runtime_python, source_root / "executor.py", repo_root,
            requirements, 900, 60_000_000, configured_input=configured_input,
            representative_input=candidate["test_vector"]["input"],
        ),
        "validation": _program(
            "generated_validator", runtime_python, source_root / "validator.py", repo_root,
            requirements, 300, 5_000_000,
            representative_input={"readiness_probe": True},
        ),
    }


def _load_json(path, name):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError) as exc:
        raise ValidationError(f"registered capability {name} is unreadable") from exc


def _verify_entry(root, entry):
    """Fail closed if any immutable registry component changed."""
    from scisaurus.runtime.program_gates import ADMISSION_SCHEMA
    from scisaurus.runtime.program_admission import validate_program_candidate

    root = Path(root).resolve()
    revision_dir = root / "capabilities" / entry["id"] / f"r{entry['revision']}"
    descriptor_path = revision_dir / "capability.json"
    supplied_path = Path(entry["path"])
    if (supplied_path.is_symlink() or not supplied_path.is_file()
            or supplied_path.resolve() != descriptor_path
            or not descriptor_path.is_relative_to(root / "capabilities")):
        raise ValidationError("capability registry descriptor path escaped its revision")
    descriptor = _load_json(descriptor_path, "descriptor")
    if (_digest(descriptor) != entry["descriptor_sha256"]
            or set(descriptor) != {"schema_version", "capability_id", "experiment"}
            or descriptor.get("schema_version") != DESCRIPTOR_SCHEMA
            or descriptor.get("capability_id") != entry["id"]):
        raise ValidationError("registered capability descriptor integrity check failed")
    experiment = descriptor.get("experiment")
    if not isinstance(experiment, dict) or experiment.get("revision") != entry["revision"]:
        raise ValidationError("registered capability descriptor identity is inconsistent")

    candidate_path = revision_dir / "candidate.json"
    admission_path = revision_dir / "admission.json"
    candidate = _load_json(candidate_path, "candidate")
    admission = _load_json(admission_path, "admission")
    validate_program_candidate(candidate)
    if (_digest(candidate) != entry["candidate_record_sha256"]
            or admission.get("schema_version") != ADMISSION_SCHEMA
            or _digest(admission) != entry["admission_sha256"]
            or admission.get("candidate_record_sha256") != entry["candidate_record_sha256"]):
        raise ValidationError("registered capability admission integrity check failed")
    if admission.get("validator_readiness") != {"status": "ready"}:
        raise ValidationError("registered capability lacks a validated readiness handshake")

    for name, key in (("executor", "executor_sha256"), ("validator", "validator_sha256")):
        source_path = revision_dir / f"{name}.py"
        if (source_path.is_symlink() or not source_path.is_file()
                or _file_digest(source_path) != entry[key]):
            raise ValidationError(f"registered capability {name} source integrity check failed")
        capability = experiment["execution" if name == "executor" else "validation"]
        if (capability["client"].get("command", [None, None])[1] != str(source_path)
                or capability["client"].get("sandbox_required") is not True
                or str(source_path) not in capability.get("environment_files", [])):
            raise ValidationError(f"registered capability {name} descriptor binding is invalid")
    if (canonical_bytes(experiment["execution"]["representative"]["input"])
            != canonical_bytes(candidate["test_vector"]["input"])):
        raise ValidationError("registered executor readiness input differs from its admitted test vector")
    if experiment["validation"]["representative"] != {"input": {"readiness_probe": True}}:
        raise ValidationError("registered validator readiness handshake is invalid")
    validate_experiment_config(_trial_config(deepcopy_json(experiment)), require_literature_gate=False)
    return descriptor


def _recover_transaction(root, index):
    capabilities_root = root / "capabilities"
    journal_path = capabilities_root / ".registry-transaction.json"
    if not journal_path.is_file():
        return index
    journal = _load_json(journal_path, "transaction journal")
    if (not isinstance(journal, dict)
            or set(journal) != {"schema_version", "entry", "revision_path", "temporary_path"}
            or journal.get("schema_version") != TRANSACTION_SCHEMA):
        raise ValidationError("capability registry transaction journal is invalid")
    entry = journal["entry"]
    if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
        raise ValidationError("capability registry transaction entry is invalid")
    final = Path(journal["revision_path"])
    temporary = Path(journal["temporary_path"])
    expected_final = capabilities_root / entry["id"] / f"r{entry['revision']}"
    if (final != expected_final or temporary.parent != expected_final.parent
            or not temporary.name.startswith(f".{expected_final.name}.")):
        raise ValidationError("capability registry transaction paths are invalid")
    identity = (entry["id"], entry["revision"])
    matches = [item for item in index["capabilities"]
               if (item["id"], item["revision"]) == identity]
    if final.is_dir():
        _verify_entry(root, entry)
        if matches and matches[0] != entry:
            raise ValidationError("capability registry transaction conflicts with its index")
        if not matches:
            index["capabilities"].append(entry)
            _atomic_write(capabilities_root / "index.json", canonical_bytes(index))
    elif matches:
        raise ValidationError("capability registry index references an absent transaction revision")
    if temporary.exists():
        shutil.rmtree(temporary)
    journal_path.unlink()
    _fsync_directory(capabilities_root)
    return index


def register_capability(root, candidate, admission, *, runtime_python, repo_root,
                        literature_gate=None, requirements_file=None):
    """Pin an admitted candidate into the registry without partial visibility."""
    from scisaurus.runtime.program_gates import ADMISSION_SCHEMA
    from scisaurus.runtime.program_admission import validate_program_candidate

    validate_program_candidate(candidate)
    if not isinstance(admission, dict) or admission.get("schema_version") != ADMISSION_SCHEMA:
        raise ValidationError("capability registration requires an admission record")
    if (admission.get("study_id") != candidate["study_id"]
            or admission.get("revision") != candidate["revision"]):
        raise ValidationError("admission record does not match the candidate identity")
    if (admission.get("validator_readiness") != {"status": "ready"}
            or "validator_readiness" not in admission.get("gates", [])):
        raise ValidationError("capability registration requires a validated readiness handshake")
    for key, source in (("executor_source_sha256", candidate["executor_source"]),
                        ("validator_source_sha256", candidate["validator_source"])):
        if admission.get(key) != hashlib.sha256(source.encode()).hexdigest():
            raise ValidationError(f"admission {key} does not match the supplied source")
    if admission.get("candidate_record_sha256") != _digest(candidate):
        raise ValidationError("admission record does not bind the complete program candidate")
    runtime_python = Path(runtime_python)
    if not runtime_python.is_absolute() or not runtime_python.is_file():
        raise ValidationError("capability runtime_python must be an existing absolute file")
    repo_root = Path(repo_root).resolve()
    requirements = (Path(requirements_file) if requirements_file
                    else repo_root / "requirements-experiment.txt")
    if not requirements.is_file():
        raise ValidationError("capability requirements file must exist")

    root = Path(root).resolve()
    capabilities_root = root / "capabilities"
    index_path = capabilities_root / "index.json"
    journal_path = capabilities_root / ".registry-transaction.json"
    final_dir = capabilities_root / candidate["study_id"] / f"r{candidate['revision']}"
    temporary_dir = final_dir.with_name(f".{final_dir.name}.{uuid.uuid4().hex}.tmp")
    with _registry_lock(capabilities_root):
        index = _recover_transaction(root, _read_index(index_path))
        if final_dir.exists() or any(
                item["id"] == candidate["study_id"] and item["revision"] == candidate["revision"]
                for item in index["capabilities"]):
            raise ValidationError("capability revision already exists; revisions are never overwritten")
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary_dir.mkdir()
        published = False
        try:
            (temporary_dir / "executor.py").write_text(candidate["executor_source"])
            (temporary_dir / "validator.py").write_text(candidate["validator_source"])
            (temporary_dir / "candidate.json").write_bytes(canonical_bytes(candidate))
            (temporary_dir / "admission.json").write_bytes(canonical_bytes(admission))
            configured_input = _registered_configured_input(candidate)
            trial_experiment = _experiment(
                candidate, runtime_python, repo_root, requirements, temporary_dir,
                configured_input, literature_gate,
            )
            validate_experiment_config(_trial_config(trial_experiment), require_literature_gate=False)
            experiment = _experiment(
                candidate, runtime_python, repo_root, requirements, final_dir,
                configured_input, literature_gate,
            )
            descriptor = {
                "schema_version": DESCRIPTOR_SCHEMA,
                "capability_id": candidate["study_id"],
                "experiment": experiment,
            }
            (temporary_dir / "capability.json").write_bytes(canonical_bytes(descriptor))
            entry = {
                "id": candidate["study_id"], "revision": candidate["revision"],
                "path": str(final_dir / "capability.json"),
                "descriptor_sha256": _digest(descriptor),
                "executor_sha256": admission["executor_source_sha256"],
                "validator_sha256": admission["validator_source_sha256"],
                "candidate_record_sha256": admission["candidate_record_sha256"],
                "admission_sha256": _digest(admission),
            }
            journal = {
                "schema_version": TRANSACTION_SCHEMA, "entry": entry,
                "revision_path": str(final_dir), "temporary_path": str(temporary_dir),
            }
            _atomic_write(journal_path, canonical_bytes(journal))
            os.replace(temporary_dir, final_dir)
            _fsync_directory(final_dir.parent)
            published = True
            _verify_entry(root, entry)
            index["capabilities"].append(entry)
            _atomic_write(index_path, canonical_bytes(index))
            journal_path.unlink()
            _fsync_directory(capabilities_root)
            return {"descriptor_path": entry["path"], "index_path": str(index_path),
                    "capability_id": candidate["study_id"],
                    "revision": candidate["revision"]}
        except Exception:
            indexed = False
            try:
                current = _read_index(index_path)
                indexed = any(item.get("id") == candidate["study_id"]
                              and item.get("revision") == candidate["revision"]
                              for item in current["capabilities"])
            except Exception:
                pass
            if not indexed:
                if published and final_dir.exists():
                    shutil.rmtree(final_dir)
                if temporary_dir.exists():
                    shutil.rmtree(temporary_dir)
                try:
                    journal_path.unlink()
                except FileNotFoundError:
                    pass
            raise


def deepcopy_json(value):
    return json.loads(canonical_bytes(value).decode())


def load_registry(root):
    """Load only entries whose complete immutable graph still verifies."""
    root = Path(root).resolve()
    capabilities_root = root / "capabilities"
    if not capabilities_root.exists():
        return _empty_index()
    with _registry_lock(capabilities_root):
        index = _recover_transaction(root, _read_index(capabilities_root / "index.json"))
        for entry in index["capabilities"]:
            _verify_entry(root, entry)
        return deepcopy_json(index)
