"""Durable execution permission shared by launchers and worker dispatch."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import uuid

from scisaurus.core.errors import ValidationError


class RunPausedError(ValidationError):
    failure_class = "operational_recovery"
    diagnostic_kind = "operator_paused"
    attempts = 0
    outcome_known = True
    usage = {}


@contextmanager
def control_lock(output, *, exclusive=True):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "run-control.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield output
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def read_control(output):
    path = Path(output) / "run-control.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RunPausedError("execution control is unreadable") from exc
    if not isinstance(value, dict):
        raise RunPausedError("execution control must be an object")
    return value


def save_control(output, value):
    path = Path(output) / "run-control.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def authorized_control(workflow_path, *, previous=None, settings=None):
    return {**(previous or {}), "workflow_path": str(Path(workflow_path).resolve()),
            "generation": uuid.uuid4().hex, "stop_requested": False,
            **({"settings": settings} if settings is not None else {})}


def check_project_stop(project_id):
    control = read_control(Path(project_id) / "output")
    if control and control.get("stop_requested") is True:
        raise RunPausedError("execution is paused; an explicit start or resume is required")


@contextmanager
def workflow_permission(workflow_path, *, initialize=False):
    """Bind a CLI invocation and all of its spawned workers to one grant."""
    workflow_path = Path(workflow_path).resolve()
    workflow = json.loads(workflow_path.read_text())
    output = Path(workflow["project_id"]).resolve() / "output"
    with control_lock(output):
        control = read_control(output)
        if control is None:
            if not initialize:
                raise RunPausedError("resume has no execution grant; use the project start control")
            control = authorized_control(workflow_path)
            save_control(output, control)
        if control.get("workflow_path") != str(workflow_path):
            raise RunPausedError("execution grant belongs to another workflow")
        if control.get("stop_requested") is not False:
            raise RunPausedError("execution is paused; use the project resume control")
        if not isinstance(control.get("generation"), str) or not control["generation"]:
            raise RunPausedError("legacy execution control has no current grant; use the project resume control")
    previous = {name: os.environ.get(name) for name in (
        "SCISAURUS_RUN_CONTROL", "SCISAURUS_RUN_GENERATION")}
    os.environ["SCISAURUS_RUN_CONTROL"] = str(output / "run-control.json")
    os.environ["SCISAURUS_RUN_GENERATION"] = control["generation"]
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def dispatch_permission():
    """Linearize a request submission against stop without locking its response wait."""
    path = os.environ.get("SCISAURUS_RUN_CONTROL")
    generation = os.environ.get("SCISAURUS_RUN_GENERATION")
    if path is None and generation is None:
        yield
        return
    if not path or not generation:
        raise RunPausedError("execution permission is incomplete")
    with control_lock(Path(path).parent, exclusive=False):
        control = read_control(Path(path).parent)
        if (not control or control.get("stop_requested") is not False
                or control.get("generation") != generation):
            raise RunPausedError("execution permission was stopped or replaced")
        yield


def ensure_run_allowed():
    with dispatch_permission():
        pass


def start_process(*args, **kwargs):
    import subprocess
    with dispatch_permission():
        return subprocess.Popen(*args, **kwargs)


def run_process(*args, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """Guard process creation, then wait without holding the launch permission lock."""
    import subprocess
    if input is not None:
        if "stdin" in kwargs:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if "stdout" in kwargs or "stderr" in kwargs:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with start_process(*args, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException as exc:
            process.kill()
            stdout, stderr = process.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                exc.stdout, exc.stderr = stdout, stderr
            raise
        result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


@contextmanager
def project_permission(project_id):
    """Bind embedded runners to an existing managed grant as well as CLI runners."""
    output = Path(project_id).resolve() / "output"
    with control_lock(output, exclusive=False):
        control = read_control(output)
        if control is not None and (control.get("stop_requested") is not False or not isinstance(control.get("generation"), str) or not control.get("generation")):
            raise RunPausedError("managed execution has no active grant")
        values = ({"SCISAURUS_RUN_CONTROL": str(output / "run-control.json"),
                  "SCISAURUS_RUN_GENERATION": control["generation"]} if control is not None else {})
        if values and os.environ.get("SCISAURUS_RUN_CONTROL") and any(os.environ.get(key) != value for key,value in values.items()):
            raise RunPausedError("embedded runner belongs to another execution grant")
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        ensure_run_allowed()
        yield
    finally:
        for key,value in previous.items():
            if value is None:
                os.environ.pop(key,None)
            else:
                os.environ[key] = value
