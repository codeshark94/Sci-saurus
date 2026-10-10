"""Controller-owned, immutable Linux runtimes with no host daemon access in jobs."""

import hashlib
import json
import math
from dataclasses import dataclass, field
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid

from scisaurus.runtime.program_sandbox import SandboxResult, capture_process
from scisaurus.core.errors import ValidationError


def validate_container(value):
    fields = {"image_id", "platform", "interpreter", "cpu_count", "memory_bytes", "daemon_socket"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("container runtime requires image_id, platform, interpreter, cpu_count, memory_bytes, daemon_socket")
    if not isinstance(value["image_id"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["image_id"]):
        raise ValidationError("container image must be a locally installed immutable image id")
    if value["platform"] not in {"linux/arm64", "linux/amd64"}:
        raise ValidationError("container platform must be linux/arm64 or linux/amd64")
    path = value["interpreter"]
    if not isinstance(path, str) or not PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
        raise ValidationError("container interpreter must be an absolute image path")
    for key in ("cpu_count", "memory_bytes"):
        if type(value[key]) is not int or value[key] <= 0:
            raise ValidationError(f"container {key} must be a positive integer")
    socket = value["daemon_socket"]
    if not isinstance(socket, str) or not Path(socket).is_absolute() or ".." in Path(socket).parts:
        raise ValidationError("container daemon must be an explicit local Unix socket")
    return value


def docker_command(runtime):
    config = validate_container(runtime["container"])
    return [runtime["executable"], "--host", "unix://" + config["daemon_socket"]]


CONTROLLER_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}


def image_manifest(runtime):
    config = validate_container(runtime["container"])
    result = subprocess.run([*docker_command(runtime), "image", "inspect", config["image_id"]],
                            capture_output=True, timeout=30, check=False, env=CONTROLLER_ENV)
    if result.returncode:
        raise ValidationError("declared container image is unavailable: " + result.stderr.decode(errors="replace"))
    try:
        rows = json.loads(result.stdout)
        row = rows[0]
    except (ValueError, IndexError, TypeError) as error:
        raise ValidationError("invalid container image identity response") from error
    platform = f"{row.get('Os')}/{row.get('Architecture')}"
    if len(rows) != 1 or row.get("Id") != config["image_id"] or platform != config["platform"]:
        raise ValidationError("container image identity or architecture differs from declared runtime")
    body = {"image_id": row["Id"], "platform": platform,
            "rootfs": row["RootFS"], "config": row["Config"], "size_bytes": row["Size"]}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": "installed-container-content-1", "sha256": digest,
            "complete": True, "errors": [], "image": body}


@dataclass
class ContainerResult(SandboxResult):
    cleanup: dict = field(default_factory=dict)


def container_command(runtime, program, workspace, arguments=(), *, env=None, file_size_bytes=268435456,
                      timeout_seconds=300):
    config = validate_container(runtime["container"])
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValidationError("container timeout must be finite and positive")
    if type(file_size_bytes) is not int or file_size_bytes <= 0:
        raise ValidationError("container file limit must be a positive integer")
    workspace, program = Path(workspace).resolve(strict=True), Path(program).resolve(strict=True)
    if not workspace.is_dir() or not program.is_file() or not program.is_relative_to(workspace):
        raise ValidationError("container program must be a regular file inside its run workspace")
    name = "sci-whale-" + uuid.uuid4().hex
    command = [*docker_command(runtime), "run", "--rm", "--pull=never", "--name", name,
               "--platform", config["platform"], "--network=none", "--read-only",
               "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=256",
               "--cpus", str(config["cpu_count"]), "--memory", str(config["memory_bytes"]),
               "--memory-swap", str(config["memory_bytes"]),
               "--ulimit", f"fsize={file_size_bytes}:{file_size_bytes}",
               "--user", f"{os.getuid()}:{os.getgid()}",
               "--tmpfs", "/tmp:rw,nosuid,nodev,size=536870912,mode=1777",
               "--mount", f"type=bind,source={workspace},target=/work",
               "--workdir", "/work", "--entrypoint", "/usr/bin/timeout", "--interactive"]
    # The host controller alone can set image paths. No caller environment or
    # daemon socket is mounted or inherited by the program inside the image.
    environment = {**(env or {}), "HOME": "/work", "TMPDIR": "/tmp",
                   "XDG_CACHE_HOME": "/work/.cache", "MPLCONFIGDIR": "/work/.matplotlib",
                   "OMP_NUM_THREADS": str(config["cpu_count"]),
                   "OPENBLAS_NUM_THREADS": str(config["cpu_count"]),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    for key, value in sorted(environment.items()):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key) or not isinstance(value, str):
            raise ValidationError("invalid controller container environment")
        command.extend(["--env", f"{key}={value}"])
    command.extend([config["image_id"], "--kill-after=2s", str(timeout_seconds), config["interpreter"],
                    "/work/" + program.relative_to(workspace).as_posix(), *arguments])
    return name, command


def run_container(runtime, program, *, workspace, arguments=(), input_bytes=b"", timeout_seconds=300,
                  env=None, max_bytes=2097152, file_size_bytes=268435456, check_workspace=None):
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValidationError("container output limit must be a positive integer")
    image_manifest(runtime)
    name, command = container_command(runtime, program, workspace, arguments,
                                      env=env, file_size_bytes=file_size_bytes, timeout_seconds=timeout_seconds)
    result, primary_error = None, None
    try:
        from scisaurus.runtime.run_control import ensure_run_allowed, start_process
        def permission():
            ensure_run_allowed()
            if check_workspace is not None:
                check_workspace()
        process = start_process(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=CONTROLLER_ENV, start_new_session=True, bufsize=0)
        result = capture_process(process, input_bytes=input_bytes, timeout_seconds=timeout_seconds,
                                 max_bytes=max_bytes, mode="container", check_permission=permission)
    except BaseException as error:
        primary_error = error
        result = getattr(error, "process_result", None)
    cleanup = {"container_name": name, "completed": False}
    try:
        removed = subprocess.run([*docker_command(runtime), "rm", "--force", name], capture_output=True,
                                 timeout=30, check=False, env=CONTROLLER_ENV)
        cleanup.update(returncode=removed.returncode, stderr=removed.stderr.decode(errors="replace"))
        if removed.returncode == 0:
            cleanup["completed"] = True
        else:
            # --rm normally removes an exited container before this call.
            inspected = subprocess.run([*docker_command(runtime), "container", "ls", "--all",
                                        "--filter", "name=^/" + name + "$", "--format", "{{.Names}}"],
                                       capture_output=True, timeout=30, check=False, env=CONTROLLER_ENV)
            cleanup["completed"] = inspected.returncode == 0 and not inspected.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        cleanup["error"] = str(error)
    if result is not None:
        result = ContainerResult(result.returncode, result.stdout, result.stderr, result.timed_out,
                                 result.truncated, result.mode, cleanup)
    if primary_error is not None:
        primary_error.process_result = result
        primary_error.container_cleanup = cleanup
        raise primary_error
    return result
