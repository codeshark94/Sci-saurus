"""Fixed-route, file-based DSH engineering jobs over its public stdio protocol."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import uuid

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes, json_object
from scisaurus.runtime.model_work import ModelWorkBlocked
from scisaurus.runtime.model_dispatch import model_dispatch_slot
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.program_sandbox import SANDBOX_EXEC, sandbox_profile
from scisaurus.runtime.run_control import ensure_run_allowed, start_process
from scisaurus.runtime.run_control import RunPausedError


class DshBatchError(ModelWorkBlocked):
    """A batch outcome is retained without starting another authoring loop."""

    def __init__(self, message, *, receipt, usage):
        super().__init__(message, failure_class="operational_recovery")
        self.receipt = str(receipt)
        self.usage = dict(usage)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_batch_config(value):
    required = {"schema_version", "command", "pinned_files", "read_roots", "composition",
                "provider", "model", "auth_env", "timeout_seconds", "max_output_tokens"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValidationError(f"DSH batch configuration requires exactly {sorted(required)}")
    if value["schema_version"] != "dsh-batch-1":
        raise ValidationError("unsupported DSH batch configuration")
    command = value["command"]
    if (not isinstance(command, list) or not command
            or any(not isinstance(arg, str) or not arg for arg in command)
            or not Path(command[0]).is_absolute() or not Path(command[0]).is_file()):
        raise ValidationError("DSH command must start with an existing absolute executable")
    pins = value["pinned_files"]
    if not isinstance(pins, dict) or not pins:
        raise ValidationError("DSH runtime and composition require pinned files")
    for path, digest in pins.items():
        if (not isinstance(path, str) or not Path(path).is_absolute()
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValidationError(f"DSH pin declaration is invalid: {path}")
    if command[0] not in pins:
        raise ValidationError("DSH executable must be pinned")
    for arg in command[1:]:
        if Path(arg).is_absolute() and Path(arg).is_file() and arg not in pins:
            raise ValidationError("DSH file arguments must be pinned")
    if value["composition"] not in pins:
        raise ValidationError("DSH composition must be pinned")
    roots = value["read_roots"]
    if (not isinstance(roots, list) or not roots
            or any(not isinstance(p, str) or not Path(p).is_absolute() or not Path(p).is_dir()
                   or Path(p).resolve() == Path("/") for p in roots)):
        raise ValidationError("DSH read roots must be explicit existing directories")
    for key in ("provider", "model", "auth_env"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValidationError(f"DSH {key} must be nonempty")
    if (type(value["timeout_seconds"]) not in (int, float)
            or not math.isfinite(value["timeout_seconds"]) or value["timeout_seconds"] <= 0):
        raise ValidationError("DSH timeout must be finite and positive")
    if type(value["max_output_tokens"]) is not int or value["max_output_tokens"] < 1:
        raise ValidationError("DSH output limit must be a positive integer")
    return deepcopy(value)


def verify_batch_runtime(config):
    """Verify deployment content at preflight, never during read-model polling."""
    for path, digest in config["pinned_files"].items():
        if not Path(path).is_file() or sha256(path) != digest:
            raise ValidationError(f"DSH pin mismatch: {path}")


def terminate_tree(process):
    """Run DSH's managed-group disposal before forced descendant cleanup."""
    rows = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True,
                          text=True, timeout=5, check=True).stdout.splitlines()
    children = {}
    for row in rows:
        pid, parent = map(int, row.split())
        children.setdefault(parent, []).append(pid)
    owned = []
    def visit(pid):
        for child in children.get(pid, []):
            visit(child)
        owned.append(pid)
    visit(process.pid)
    # EOF and TERM invoke DSH's registry of detached groups, including groups
    # whose launcher has exited and whose descendants were reparented.
    if process.stdin and not process.stdin.closed:
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    for pid in owned:
        if pid == process.pid and process.poll() is not None:
            continue
        try:
            os.kill(pid, signal.SIGSTOP)
        except ProcessLookupError:
            pass
    for pid in owned:
        if pid == process.pid and process.poll() is not None:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


class DshBatchRunner:
    """One fresh session per work order, with controller-owned receipts."""

    def __init__(self, config, *, root, runtime_read_roots=()):
        self.config = validate_batch_config(config)
        self.root = Path(root).resolve()
        self.runtime_read_roots = [str(Path(p).resolve()) for p in runtime_read_roots]

    def run(self, task, *, inputs, seed_files=None, outputs, deadline=None):
        deadline = min(deadline if deadline is not None else math.inf,
                       time.monotonic() + self.config["timeout_seconds"])
        # One DSH session has sequential model steps. Keep its slot until the
        # process tree is reaped, including periods spent editing and executing.
        try:
            with model_dispatch_slot(deadline=deadline) as slot:
                return self._run(task, inputs=inputs, seed_files=seed_files,
                                 outputs=outputs, deadline=deadline, dispatch_slot=slot)
        except DshBatchError:
            raise
        except (ValidationError, TimeoutError, OSError) as exc:
            if isinstance(exc, RunPausedError) and getattr(exc, "receipt", None):
                raise
            job = self.root / uuid.uuid4().hex
            job.mkdir(parents=True)
            receipt = job / "receipt.json"
            usage = {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}
            receipt.write_bytes(canonical_bytes({
                "schema_version": "dsh-batch-receipt-1", "status": "not_dispatched",
                "config_sha256": hashlib.sha256(canonical_bytes(self.config)).hexdigest(),
                "provider": self.config["provider"], "model": self.config["model"],
                "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
                "usage": usage, "error": f"{type(exc).__name__}: {exc}"}))
            if isinstance(exc, RunPausedError):
                exc.receipt, exc.usage, exc.dsh_not_dispatched = str(receipt), usage, True
                raise
            raise DshBatchError(str(exc), receipt=receipt, usage=usage) from exc

    def _run(self, task, *, inputs, seed_files, outputs, deadline, dispatch_slot):
        ensure_run_allowed()
        config = validate_batch_config(self.config)
        verify_batch_runtime(config)
        if (not isinstance(inputs, dict) or not isinstance(seed_files or {}, dict)
                or not isinstance(outputs, list) or not outputs or len(set(outputs)) != len(outputs)):
            raise ValidationError("batch requires file mappings and unique nonempty output names")
        if not SANDBOX_EXEC:
            raise ValidationError("DSH batch requires sandbox-exec; isolation cannot be downgraded")
        for name in [*inputs, *(seed_files or {}), *outputs]:
            if not isinstance(name, str):
                raise ValidationError("batch file names must be strings")
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValidationError("batch file names must be relative and contained")
        if not isinstance(task, str) or not task.strip():
            raise ValidationError("batch task must be nonempty")
        remaining = config["timeout_seconds"]
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
        if remaining <= 0:
            raise ValidationError("DSH batch has no mission time remaining")
        job = self.root / uuid.uuid4().hex
        work, frozen = job / "work", job / "input"
        work.mkdir(parents=True)
        frozen.mkdir()
        def write_files(root, files):
            for name, data in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data if isinstance(data, bytes) else data.encode())
        write_files(frozen, inputs)
        write_files(work, seed_files or {})
        for path in frozen.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        bound = {name: sha256(frozen / name) for name in inputs}
        receipt = job / "receipt.json"
        journal = job / "events.jsonl"
        state = {"schema_version": "dsh-batch-receipt-1", "status": "prepared",
                 "session_id": uuid.uuid4().hex, "config_sha256": hashlib.sha256(canonical_bytes(config)).hexdigest(),
                 "provider": config["provider"], "model": config["model"], "input_sha256": bound,
                 "task_sha256": hashlib.sha256(task.encode()).hexdigest(), "outputs": {},
                 "seed_sha256": {name: sha256(work / name) for name in seed_files or {}},
                 "usage": {"model_calls": 0, "input_tokens": 0, "output_tokens": 0}}
        def save():
            tmp = receipt.with_suffix(".tmp")
            tmp.write_bytes(canonical_bytes(state))
            tmp.replace(receipt)
        save()
        credential = os.environ.get(config["auth_env"])
        if not credential:
            state.update(status="not_dispatched", error="credential environment reference is unset")
            save()
            raise DshBatchError("DSH credential environment reference is unset", receipt=receipt, usage=state["usage"])
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "en_US.UTF-8",
               "HOME": str(work), "TMPDIR": str(work), "DSH_CWD": str(work),
               "DSH_SESSION_ROOT": str(work / ".sessions"),
               "DSH_CORDIS_CONFIG": config["composition"], "DSH_MAX_TOKENS_AS_SUCCESS": "false",
               config["auth_env"]: credential}
        profile = sandbox_profile(work, config["command"], allow_network=True,
            read_only_paths=[str(frozen), *config["read_roots"], *self.runtime_read_roots])
        # DSH supervises detached shell groups. It must be able to inspect
        # processes and signal members of its own inherited Seatbelt domain.
        profile += "\n(allow process-info*)\n(allow signal (target same-sandbox))\n"
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("DSH batch reached its execution deadline before dispatch")
            process = start_process([SANDBOX_EXEC, "-p", profile, *config["command"]],
                cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
                pass_fds=(dispatch_slot.fileno(),))
        except BaseException as exc:
            state.update(status="not_dispatched", error=f"{type(exc).__name__}: {exc}")
            save()
            if isinstance(exc, RunPausedError):
                exc.receipt, exc.usage, exc.dsh_not_dispatched = str(receipt), dict(state["usage"]), True
                raise
            raise DshBatchError(str(exc), receipt=receipt, usage=state["usage"]) from exc
        start = time.monotonic()
        finish = None
        received = False
        message_id = None
        pending_events = []
        selector = None
        buffers = {"stdout": bytearray()}
        calls_seen = set()
        step_usage = {}
        def send(identity, method, params):
            ensure_run_allowed()
            if time.monotonic() >= deadline:
                raise TimeoutError("DSH batch reached its execution deadline")
            process.stdin.write(canonical_bytes({"jsonrpc": "2.0", "id": identity,
                                                "method": method, "params": params}) + b"\n")
            process.stdin.flush()
        def event_usage(payload):
            if payload.get("method") != "session.event":
                return
            params = payload.get("params", {})
            event = params.get("event", {})
            data = event.get("data", {})
            identity = (params.get("sessionId"), data.get("turn"), data.get("step"))
            if event.get("type") == "step/start":
                if identity not in calls_seen:
                    calls_seen.add(identity)
                    state["usage"]["model_calls"] += 1
                return
            if event.get("type") == "assistant/chunk" and data.get("chunk", {}).get("type") == "usage":
                usage = data["chunk"].get("usage", {})
            elif event.get("type") == "assistant/message":
                usage = data.get("usage", {})
            else:
                return
            if identity not in calls_seen:
                calls_seen.add(identity)
                state["usage"]["model_calls"] += 1
            prior = step_usage.setdefault(identity, {})
            for source, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                                   ("cacheReadTokens", "cache_read_tokens"), ("reasoningTokens", "reasoning_tokens")):
                number = usage.get(source)
                if type(number) is int and number >= 0:
                    previous = prior.get(target, 0)
                    state["usage"][target] = state["usage"].get(target, 0) + max(0, number - previous)
                    prior[target] = max(number, previous)
        def owned_event(payload):
            nonlocal received, finish
            params = payload.get("params", {})
            if params.get("sessionId") != state["session_id"]:
                return False
            event = params.get("event", {})
            if event.get("type") == "agent/inbox/spliced":
                inserted = event.get("data", {}).get("inserted", [])
                received |= any(item.get("id") == message_id for item in inserted if isinstance(item, dict))
            if received and event.get("type") == "turn/end":
                finish = event.get("data", {}).get("reason", {}).get("kind")
            return received and payload.get("method") == "session.status" and params.get("status") == "idle"
        try:
            state.update(status="running", pid=process.pid)
            save()
            selector = selectors.DefaultSelector()
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            send(1, "initialize", {"cwd": str(work), "provider": config["provider"],
                                   "model": config["model"], "maxTokens": config["max_output_tokens"]})
            done = False
            with journal.open("ab", buffering=0) as log, (job / "stderr.log").open("ab", buffering=0) as errors:
                while not done:
                    ensure_run_allowed()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("DSH batch reached its execution deadline")
                    if not selector.get_map():
                        raise RuntimeError("DSH transport ended before the owned session became idle")
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        name = key.data
                        if name == "stderr":
                            errors.write(chunk)
                            continue
                        buffers[name].extend(chunk)
                        if len(buffers[name]) > 60_000_000:
                            raise RuntimeError("DSH protocol line exceeds the receipt capacity")
                        while b"\n" in buffers[name]:
                            line, _, tail = buffers[name].partition(b"\n")
                            buffers[name] = bytearray(tail)
                            payload = json.loads(line)
                            log.write(canonical_bytes(payload) + b"\n")
                            if payload.get("error"):
                                raise RuntimeError(f"DSH protocol error: {payload['error']}")
                            if payload.get("id") == 1:
                                send(2, "session/prompt", {"sessionId": state["session_id"],
                                    "contentBlocks": [{"type": "text", "text":
                                        f"Immutable task files: {frozen}\nWritable workspace: {work}\n" + task}]})
                            elif payload.get("id") == 2:
                                message_id = payload["result"]["messageId"]
                                for item in pending_events:
                                    done |= owned_event(item)
                                pending_events.clear()
                            elif payload.get("method"):
                                event_usage(payload)
                                if message_id is None:
                                    pending_events.append(payload)
                                else:
                                    done |= owned_event(payload)
                            save()
            ensure_run_allowed()
            if finish != "completed":
                raise RuntimeError(f"DSH job ended with {finish!r}; files are not accepted")
            if {name: sha256(frozen / name) for name in inputs} != bound:
                raise RuntimeError("DSH job changed frozen task files")
            files = {}
            for name in outputs:
                path = work / name
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(work):
                    raise RuntimeError(f"DSH job did not produce a contained regular file: {name}")
                files[name] = path.read_bytes()
                state["outputs"][name] = hashlib.sha256(files[name]).hexdigest()
            state.update(status="completed", finish_reason=finish)
            return {"files": files, "receipt": str(receipt), "usage": dict(state["usage"]),
                    "elapsed_seconds": time.monotonic() - start}
        except BaseException as exc:
            state.update(status="result_unknown", error=f"{type(exc).__name__}: {exc}", finish_reason=finish)
            raise DshBatchError(str(exc), receipt=receipt, usage=state["usage"]) from exc
        finally:
            # Persist the paid outcome before disposal; disposal failure must
            # retain the same receipt and usage instead of replacing them.
            cleanup_errors = []
            state["elapsed_seconds"] = time.monotonic() - start
            try:
                save()
            except Exception as exc:
                cleanup_errors.append(exc)
            try:
                terminate_tree(process)
            except Exception as exc:
                cleanup_errors.append(exc)
                # TERM gives the DSH managed-group registry a final disposal
                # opportunity even when process enumeration itself failed.
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                except Exception as fallback_error:
                    cleanup_errors.append(fallback_error)
            state["process_reaped"] = process.poll() is not None
            for resource in (selector, process.stdin, process.stdout, process.stderr):
                if resource is None:
                    continue
                try:
                    resource.close()
                except Exception as exc:
                    cleanup_errors.append(exc)
            state["elapsed_seconds"] = time.monotonic() - start
            if cleanup_errors:
                state["cleanup_error"] = "; ".join(
                    f"{type(error).__name__}: {error}" for error in cleanup_errors)
            try:
                save()
            except Exception as exc:
                cleanup_errors.append(exc)
            if cleanup_errors:
                raise DshBatchError("DSH transport disposal failed: " + "; ".join(
                    f"{type(error).__name__}: {error}" for error in cleanup_errors),
                    receipt=receipt, usage=state["usage"]) from cleanup_errors[0]



class DshAuthorClient:
    """Import source files into the existing independent admission pipeline."""

    def __init__(self, config, *, root, runtime_python):
        self.config = validate_batch_config(config)
        self.model = config["model"]
        self.timeout_seconds = config["timeout_seconds"]
        self.max_output_tokens = config["max_output_tokens"]
        self.base_assignment = None
        self.runtime_python = str(Path(runtime_python).absolute())
        self.runner = DshBatchRunner(config, root=root,
            runtime_read_roots=[str(Path(runtime_python).absolute().parent.parent)])

    def complete(self, *, system, prompt):
        assignment = json.loads(prompt)
        seed = {}
        for key in ("candidate", "previous_attempt"):
            candidate = assignment.get(key)
            if isinstance(candidate, dict) and isinstance(candidate.get("executor_source"), str):
                seed["executor.py"] = candidate["executor_source"]
                seed["intent.json"] = json.dumps(candidate["experiment_intent"], ensure_ascii=False)
        repair = assignment.get("repair_request")
        if isinstance(repair, dict) and isinstance(repair.get("previous_attempt"), dict):
            candidate = repair["previous_attempt"]
            if isinstance(candidate.get("executor_source"), str):
                seed["executor.py"] = candidate["executor_source"]
                seed["intent.json"] = json.dumps(candidate["experiment_intent"], ensure_ascii=False)
        current = assignment.get("current_candidate")
        if isinstance(current, dict):
            context = current.get("source_context", {}).get("executor_source", {})
            if isinstance(context.get("source"), str):
                source = context["source"]
                if hashlib.sha256(source.encode()).hexdigest() != context.get("source_sha256"):
                    raise ValidationError("DSH repair source does not match its controller fingerprint")
                seed["executor.py"] = source
                seed["intent.json"] = json.dumps(current["experiment_intent"], ensure_ascii=False)
        # Transform only response-envelope fields, never recursively filter scientific data.
        def project(value):
            projected = deepcopy(value)
            for key in ("response_contract", "instructions", "author_backend"):
                projected.pop(key, None)
            return projected
        projected = project(assignment)
        projected.pop("author_backend", None)
        contract = assignment.get("output_contract", {})
        projected["file_output_contract"] = {
            "executor.py": contract.get("executor_source"),
            "intent.json": contract.get("experiment_intent")}
        projected.pop("output_contract", None)
        base = project(self.base_assignment or assignment)
        base.pop("author_backend", None)
        base.pop("response_contract", None)
        base["runtime_python"] = self.runtime_python
        task = ("Read assignment.json and execution-contract.json and implement the stated scientific task. "
                "The current assignment contains current repair evidence; execution-contract.json contains "
                "the full frozen configured_input, pinned runtime and output schemas. Work by editing files, "
                "running the program, examining actual errors and repairing them. You may inspect documentation "
                "and provision dependencies inside this workspace. Export a self-contained executor.py and "
                "a complete intent.json. The controller commissions a separate blinded validator; do not author one. "
                "Frozen configured_input and required_intent_fields are immutable. Do not change the model "
                "definition, fabricate measurements or force positive findings. Escalate an unimplementable "
                "definition instead of silently replacing it. Preserve raw runs and commands in the workspace. "
                "The executor uses the supplied stdin/stdout and analysis contracts and the declared pinned "
                "runtime. Do not patch output observations to pass checks. Your final chat answer is not the "
                "deliverable; the two files are. Experimental admission is performed independently by the controller.")
        result = self.runner.run(task, inputs={"assignment.json": canonical_bytes(projected),
                                               "execution-contract.json": canonical_bytes(base)},
            seed_files=seed, outputs=["executor.py", "intent.json"],
            deadline=time.monotonic() + self.timeout_seconds)
        try:
            intent = json_object(result["files"]["intent.json"].decode(), "DSH experiment intent")
            source = result["files"]["executor.py"].decode()
            if not source.strip():
                raise ValidationError("DSH executor file is empty")
        except (ValueError, UnicodeError, ValidationError) as exc:
            raise DshBatchError(f"invalid batch deliverable: {exc}", receipt=result["receipt"],
                                usage=result["usage"]) from exc
        return ModelResult(json.dumps({"executor_source": source, "experiment_intent": intent}),
            self.model, result["usage"], result["elapsed_seconds"], "stop",
            response_metadata={"backend": "dsh-batch-1", "receipt": result["receipt"],
                               "configuration_sha256": hashlib.sha256(canonical_bytes(self.config)).hexdigest()})


def read_completed_structured_producer(job, *, config_sha256):
    """Read immutable inputs and output from a settled structured producer."""
    job = Path(job)
    paths = [job, *(job / name for name in (
        "receipt.json", "input", "work", "input/assignment.json",
        "input/system-contract.txt", "work/response.json"))]
    if any(path.is_symlink() for path in paths):
        raise ValidationError("completed producer evidence is symlinked")
    receipt = json.loads((job / "receipt.json").read_bytes())
    assignment = json.loads((job / "input/assignment.json").read_bytes())
    if (receipt.get("schema_version") != "dsh-batch-receipt-1"
            or receipt.get("status") != "completed" or receipt.get("process_reaped") is not True
            or not isinstance(receipt.get("model"), str) or not receipt["model"].strip()
            or not isinstance(config_sha256, str) or len(config_sha256) != 64
            or receipt.get("config_sha256") != config_sha256
            or receipt.get("task_sha256") != hashlib.sha256(
                DshStructuredProducerClient.task(None, assignment).encode()).hexdigest()
            or set(receipt.get("input_sha256", {})) != {"assignment.json", "system-contract.txt"}
            or not isinstance(receipt.get("usage"), dict)
            or any(type(receipt["usage"].get(key)) not in (int, float)
                   or not math.isfinite(receipt["usage"][key]) or receipt["usage"][key] < 0
                   for key in ("model_calls", "input_tokens", "output_tokens"))):
        raise ValidationError("completed producer receipt has no settled owner")
    for name, digest in receipt["input_sha256"].items():
        if hashlib.sha256((job / "input" / name).read_bytes()).hexdigest() != digest:
            raise ValidationError("completed producer input digest differs")
    output = (job / "work/response.json").read_bytes()
    if hashlib.sha256(output).hexdigest() != receipt.get("outputs", {}).get("response.json"):
        raise ValidationError("completed producer output digest differs")
    return receipt, assignment, (job / "input/system-contract.txt").read_text(), json.loads(output)


class DshStructuredProducerClient:
    """Author a file-backed structured deliverable under the controller contract."""

    deliverable_label = "structured producer"

    def __init__(self, config, *, root, runtime_python):
        self.config = validate_batch_config(config)
        self.model = config["model"]
        self.timeout_seconds = config["timeout_seconds"]
        self.max_output_tokens = config["max_output_tokens"]
        self.runtime_python = str(Path(runtime_python).absolute())
        self.runner = DshBatchRunner(config, root=root,
            runtime_read_roots=[str(Path(runtime_python).absolute().parent.parent)])

    def task(self, assignment):
        return (
            "Read the immutable assignment.json and system-contract.txt. Complete the assigned "
            "research or engineering production task in this workspace. Use files, local checks "
            "and revisions to satisfy the exact response contract before finishing. Write the "
            "complete deliverable to response.json as one strict JSON object, with no markdown "
            "or trailing text. output_contract describes the top-level deliverable; nested field "
            "contracts remain nested. Preserve required candidate counts, identities, input and "
            "evidence boundaries. Do not invent observations, citations, successful execution or "
            "scientific validation. The controller validates and admits the deliverable separately. "
            "Your final chat answer is not the deliverable; response.json is.")

    def complete(self, *, system, prompt):
        try:
            assignment = json.loads(prompt)
        except ValueError as exc:
            raise ValidationError(
                "DSH producer assignment must be a strict JSON object") from exc
        if not isinstance(assignment, dict):
            raise ValidationError("DSH producer assignment must be a JSON object")
        # Preserve every scientific field.  Only controller-internal envelope
        # keys are projected away; the response contract itself is delivered as
        # a frozen input file so the producer can satisfy it without guessing.
        projected = deepcopy(assignment)
        for key in ("author_backend",):
            projected.pop(key, None)
        projected["runtime_python"] = self.runtime_python
        result = self.runner.run(
            self.task(projected),
            inputs={"assignment.json": canonical_bytes(projected),
                    "system-contract.txt": (system or "").encode("utf-8")},
            outputs=["response.json"],
            deadline=time.monotonic() + self.timeout_seconds)
        try:
            response = json_object(result["files"]["response.json"].decode("utf-8"),
                                   "DSH " + self.deliverable_label + " response")
        except (ValidationError, ValueError, UnicodeError) as exc:
            raise DshBatchError(f"invalid {self.deliverable_label} deliverable: {exc}",
                                receipt=result["receipt"], usage=result["usage"]) from exc
        if not isinstance(response, dict) or not response:
            raise DshBatchError("DSH " + self.deliverable_label + " response must be a nonempty JSON object",
                                receipt=result["receipt"], usage=result["usage"])
        return ModelResult(
            json.dumps(response, ensure_ascii=False, sort_keys=True),
            self.model, result["usage"], result["elapsed_seconds"], "stop",
            response_metadata={"backend": "dsh-batch-1", "receipt": result["receipt"],
                               "configuration_sha256": hashlib.sha256(
                                   canonical_bytes(self.config)).hexdigest()})


class DshSoftwareProducerClient(DshStructuredProducerClient):
    """Produce controller operations; local diagnostic runs are not receipts."""

    deliverable_label = "software producer"

    def task(self, assignment):
        task = (
            "Read assignment.json and system-contract.txt. You are the scientific software "
            "engineering producer. Develop and debug the scripts required by the declared "
            "scientific operations by editing files and running them inside this workspace; "
            "those local runs are diagnostic development evidence and are NEVER controller "
            "receipts. The controller alone executes every declared operation and returns its "
            "receipt-bound result on the next turn. Export response.json as exactly one strict "
            "JSON object with no prose, markdown, fence, or trailing characters: either "
            "{\"tool_action\": {\"operation\": <name>, \"arguments\": <object>}} to request one "
            "controller operation, or the complete final producer response required by the "
            "assignment and system contract. Do not invent observations, do not present a "
            "shell command or its stdout as a scientific result, and do not name a host path "
            "outside a declared runtime label. Experimental admission and independent "
            "validation are owned by the controller. Your final chat answer is not the "
            "deliverable; response.json is.")
        if isinstance(assignment.get("scientific_software_tools"), dict):
            task += (" The scientific_software_tools object in assignment.json declares the "
                     "exact controller operations and argument schema you may request.")
        return task


class DshValidatorClient:
    """A fresh blinded engineering session for the separately authored validator."""

    def __init__(self, config, *, root, runtime_python):
        self.config = validate_batch_config(config)
        self.model = config["model"]
        self.timeout_seconds = config["timeout_seconds"]
        self.max_output_tokens = config["max_output_tokens"]
        self.deadline = None
        self.runtime_python = str(Path(runtime_python).absolute())
        self.runner = DshBatchRunner(config, root=root,
            runtime_read_roots=[str(Path(runtime_python).absolute().parent.parent)])

    def complete(self, *, system, prompt):
        assignment = json.loads(prompt)
        seed = {}
        repair = assignment.get("validator_repair") or {}
        prior = repair.get("prior_source")
        if prior is not None:
            if (not isinstance(prior, str)
                    or hashlib.sha256(prior.encode()).hexdigest() != repair.get("source_sha256")):
                raise ValidationError("DSH validator repair source differs from its controller fingerprint")
            seed["validator.py"] = prior
        assignment = deepcopy(assignment)
        assignment.pop("response_contract", None)
        assignment.pop("instructions", None)
        if isinstance(assignment.get("validator_repair"), dict):
            assignment["validator_repair"].pop("patch_contract", None)
            assignment["validator_repair"].pop("instructions", None)
        assignment["runtime_python"] = self.runtime_python
        task = ("Read assignment.json and implement validator.py as a self-contained program in the "
                "declared runtime. The assignment is deliberately blinded to executor implementation "
                "and reported results. Independently derive the declared calculations from raw observations "
                "and frozen intent. Preserve acceptance criteria, exact metric identifiers, indexing and "
                "candidate digest echo. Use runtime_request_shape and readiness_handshake for stdin/stdout. "
                "Edit the file, run local interface checks, inspect actual errors and repair. Any "
                "validator_repair contains only your prior validator source and current failure evidence. "
                "Do not search for the producer implementation or fabricate observations. Do not change "
                "scientific inputs, estimands, tolerances or gates to force agreement. Preserve local commands "
                "and check outputs in the workspace. The exported file, rather than final chat text, is the deliverable.")
        result = self.runner.run(task, inputs={"assignment.json": canonical_bytes(assignment)},
            seed_files=seed, outputs=["validator.py"], deadline=self.deadline)
        try:
            source = result["files"]["validator.py"].decode()
            if not source.strip():
                raise ValidationError("DSH validator file is empty")
        except (UnicodeError, ValidationError) as exc:
            raise DshBatchError(f"invalid validator deliverable: {exc}", receipt=result["receipt"],
                                usage=result["usage"]) from exc
        return ModelResult(json.dumps({"validator_source": source}), self.model, result["usage"],
            result["elapsed_seconds"], "stop", response_metadata={"backend": "dsh-batch-1",
                "receipt": result["receipt"],
                "configuration_sha256": hashlib.sha256(canonical_bytes(self.config)).hexdigest()})
