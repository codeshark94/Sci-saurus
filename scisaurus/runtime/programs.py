"""Bounded execution of explicitly configured local JSON programs.

Arguments and process settings are supplied by the project control plane. Workload
JSON travels only over stdin. The enclosing ExecutionRuntime owns the process
group and reaps descendants after every call, including normal completion.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

from scisaurus.core.schema import canonical_bytes, now_iso, sha256_hex
from scisaurus.runtime.retrieval import SAFE_PROCESS_ENV


ADAPTER_VERSION = "1"
PROTOCOL_VERSION = "json-stdin-object-v1"


def _capture(body, media_type):
    return {"encoding": "base64", "body": base64.b64encode(body).decode("ascii"),
            "sha256": sha256_hex(body), "bytes": len(body), "media_type": media_type}


def json_object(value):
    """Reject Python values that cannot be represented faithfully in JSON."""
    def validate(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                validate(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                validate(child)
            return
        raise ValueError("Program input must contain only finite JSON values and string object keys")
    if type(value) is not dict:
        raise ValueError("Program input must be a JSON object")
    validate(value)
    return json.loads(canonical_bytes(value))


def _parse_object(body):
    def pairs(entries):
        value = {}
        for key, item in entries:
            if key in value:
                raise ValueError("Program output contains duplicate object keys")
            value[key] = item
        return value
    def invalid_constant(value):
        raise ValueError(f"Program output contains a nonfinite number: {value}")
    value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    return json_object(value)


def command_identity(command, cwd, env):
    executable = Path(command[0])
    digest = hashlib.sha256()
    with executable.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    details = {"command": command, "cwd": cwd, "env": env,
               "executable": {"path": str(executable), "resolved_path": str(executable.resolve()),
                              "sha256": digest.hexdigest()}}
    return {"sha256": sha256_hex(canonical_bytes(details)), "details": details}


class LocalProgramClient:
    """A fixed executable and argv accepting one JSON object on stdin.

    ``max_bytes`` bounds stdout and stderr together. The default leaves the
    process in the worker's group; standalone callers may explicitly own a group.
    """
    def __init__(self, command, *, timeout, max_bytes, cwd, env, own_process_group=False):
        if (not isinstance(command, list) or not command
                or any(not isinstance(arg, str) or "\0" in arg for arg in command)
                or not Path(command[0]).is_absolute() or not Path(command[0]).is_file()
                or not os.access(command[0], os.X_OK)):
            raise ValueError("Program command requires an absolute executable and fixed string arguments")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be a positive finite number")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if (not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir()
                or str(Path(cwd).resolve()) != cwd):
            raise ValueError("Program cwd must be an existing resolved absolute directory")
        if (not isinstance(env, dict) or any(key not in SAFE_PROCESS_ENV or not isinstance(value, str)
                                           or "\0" in value for key, value in env.items())):
            raise ValueError("Program environment is limited to explicit nonsecret process settings")
        if type(own_process_group) is not bool:
            raise ValueError("own_process_group must be a Boolean")
        self.command, self.timeout, self.max_bytes = list(command), timeout, max_bytes
        self.cwd, self.env, self.own_process_group = cwd, dict(env), own_process_group

    def run(self, input):
        document = json_object(input)
        stdin = canonical_bytes(document)
        identity = command_identity(self.command, self.cwd, self.env)
        result = {"outcome": "program_error", "document": None, "text": "", "sources": [], "gaps": [],
                  "input": document, "input_capture": _capture(stdin, "application/json"),
                  "input_sha256": sha256_hex(stdin),
                  "metadata": {"provider": "local-program", "transport": "subprocess_stdio",
                               "representation": "json_object", "adapter_version": ADAPTER_VERSION,
                               "protocol_version": PROTOCOL_VERSION, "started_at": now_iso(),
                               "command": self.command, "cwd": self.cwd, "command_identity": identity,
                               "own_process_group": self.own_process_group, "process_returncode": None,
                               "input_bytes_written": 0, "capture_truncated": False,
                               "capture_incomplete": False}}
        stdout, stderr = bytearray(), bytearray()
        process = None
        deadline = time.monotonic() + self.timeout
        selector = selectors.DefaultSelector()
        try:
            process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, cwd=self.cwd, env=self.env, shell=False,
                                       bufsize=0, start_new_session=self.own_process_group)
            for stream, name, event in ((process.stdin, "stdin", selectors.EVENT_WRITE),
                                        (process.stdout, "stdout", selectors.EVENT_READ),
                                        (process.stderr, "stderr", selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event, name)
            offset = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Program exceeded its execution deadline")
                for key, _ in selector.select(remaining):
                    stream, name = key.fileobj, key.data
                    if name == "stdin":
                        try:
                            written = os.write(stream.fileno(), stdin[offset:offset + 65536])
                        except BrokenPipeError:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        offset += written
                        result["metadata"]["input_bytes_written"] = offset
                        if offset == len(stdin):
                            selector.unregister(stream)
                            stream.close()
                    else:
                        # Read one byte beyond the shared limit to detect truncation.
                        remaining_bytes = self.max_bytes - len(stdout) - len(stderr)
                        chunk = os.read(stream.fileno(), min(65536, remaining_bytes + 1))
                        if not chunk:
                            selector.unregister(stream)
                            stream.close()
                            continue
                        target = stdout if name == "stdout" else stderr
                        target.extend(chunk[:remaining_bytes])
                        if len(chunk) > remaining_bytes:
                            result["outcome"] = "partial"
                            result["metadata"].update(capture_truncated=True, capture_incomplete=True)
                            result["gaps"].append("Program stdout and stderr exceeded the shared capture byte limit")
                            raise _OutputLimit()
            process.wait(timeout=max(0, deadline - time.monotonic()))
            if process.returncode != 0:
                result["gaps"].append(f"Program exited with status {process.returncode}")
            elif offset != len(stdin):
                result["gaps"].append("Program stdin closed before the entire input could be written")
            else:
                try:
                    result["document"] = _parse_object(bytes(stdout))
                    result["text"] = canonical_bytes(result["document"]).decode("utf-8")
                    result["outcome"] = "ok"
                except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                    result["outcome"] = "parse_error"
                    result["gaps"].append(f"Program stdout is not one complete JSON object: {exc}")
        except (TimeoutError, subprocess.TimeoutExpired):
            result["outcome"] = "timeout"
            result["metadata"]["capture_incomplete"] = True
            result["gaps"].append("Program exceeded its execution deadline")
        except _OutputLimit:
            pass
        except OSError as exc:
            result["metadata"]["capture_incomplete"] = True
            result["gaps"].append(f"Program execution failed: {type(exc).__name__}: {exc}")
        finally:
            selector.close()
            if process is not None:
                if self.own_process_group and os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.kill()
                process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if not stream.closed:
                        stream.close()
                result["metadata"]["process_returncode"] = process.returncode
            result.update(capture=_capture(bytes(stdout), "application/json"),
                          stderr_capture=_capture(bytes(stderr), "text/plain"),
                          capture_sha256=sha256_hex(bytes(stdout)), stderr_sha256=sha256_hex(bytes(stderr)))
            result["metadata"]["finished_at"] = now_iso()
        return result


class _OutputLimit(Exception):
    pass
