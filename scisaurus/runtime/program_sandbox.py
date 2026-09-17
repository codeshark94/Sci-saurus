"""Sandboxed execution of model-authored experiment programs (foundry P3.2).

The sandbox is the real security boundary for a generative capability.  It
combines two independent controls:

* a macOS ``sandbox-exec`` profile that denies everything by default and then
  allows only process execution, reads, writes inside the throwaway workspace,
  and (optionally) network access;
* POSIX resource limits applied in the child (CPU seconds, address space, file
  size, open files) plus an absolute wall deadline enforced by the parent.

When ``sandbox-exec`` is unavailable the runner still applies the resource
limits but reports ``mode = "rlimits-only"`` so the caller can refuse to treat a
candidate as fully admitted.  Isolation strength is never silently downgraded.
"""
from __future__ import annotations

import os
import resource
import selectors
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from scisaurus.core.errors import ValidationError

SANDBOX_EXEC = shutil.which("sandbox-exec")
SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "PYTHONIOENCODING", "TMPDIR", "MPLCONFIGDIR")
DEFAULT_CPU_SECONDS = 600
DEFAULT_ADDRESS_SPACE = 4 * 1024 * 1024 * 1024
DEFAULT_FILE_SIZE = 256 * 1024 * 1024


@dataclass
class SandboxResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    mode: str


def sandbox_status():
    """Report the isolation strength available on this host."""
    return {"sandbox_exec": SANDBOX_EXEC, "mode": "sandbox-exec" if SANDBOX_EXEC else "rlimits-only"}


def _macho_dependency_paths(executable):
    """Return existing absolute Mach-O libraries needed to start *executable*.

    macOS Python distributions do not all place their framework library next
    to the virtual environment.  Inspecting the pinned executable keeps the
    Seatbelt read allowlist narrow while supporting Homebrew, hosted-toolcache,
    and framework-based Python installations alike.
    """
    otool = shutil.which("otool")
    if not otool:
        return set()
    pending = [Path(executable)]
    seen = set()
    dependencies = set()
    while pending:
        current = pending.pop()
        try:
            current = current.resolve()
        except OSError:
            continue
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        try:
            completed = subprocess.run(
                [otool, "-L", str(current)], capture_output=True, text=True,
                check=False,
            )
        except (OSError, ValueError):
            continue
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines()[1:]:
            raw_path = line.strip().split(" (", 1)[0]
            if not raw_path.startswith("/"):
                continue
            dependency = Path(raw_path)
            if not dependency.is_file():
                continue
            dependencies.add(dependency)
            try:
                resolved = dependency.resolve()
            except OSError:
                resolved = dependency
            dependencies.add(resolved)
            if resolved not in seen:
                pending.append(resolved)
    return dependencies


def _sandbox_read_paths(command, workspace):
    """Return the minimal immutable runtime roots needed to start Python."""
    executable = Path(command[0]).resolve()
    configured = Path(command[0])
    roots = {
        Path(workspace).resolve(),
        Path("/opt/homebrew"),
    }
    # A virtual environment keeps its interpreter and site-packages under one
    # prefix. Never infer a broader user-home root from an arbitrary command.
    if configured.parent.name == "bin" and configured.parent.parent.name in {".venv", "venv"}:
        roots.add(configured.parent.parent.resolve())
    files = {configured.resolve(), executable}
    files.update(_macho_dependency_paths(executable))
    for argument in command[1:]:
        candidate = Path(argument)
        if candidate.is_absolute() and candidate.exists():
            files.add(candidate.resolve())
    return sorted(str(path) for path in roots if path.exists()), sorted(str(path) for path in files)


def sandbox_profile(workspace, command, *, allow_network=False):
    """Return a deny-by-default Seatbelt profile for one throwaway workspace."""
    workspace = str(Path(workspace).resolve())
    read_roots, read_files = _sandbox_read_paths(command, workspace)
    metadata_paths = set()
    for value in [*read_roots, *read_files]:
        path = Path(value)
        metadata_paths.add(str(path))
        metadata_paths.update(str(parent) for parent in path.parents if str(parent) != "/")
    lines = [
        "(version 1)",
        "(deny default)",
        '(import "system.sb")',
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow file-read-metadata",
        *[f'  (literal "{path}")' for path in sorted(metadata_paths)],
        ")",
        "(allow file-read*",
        *[f'  (subpath "{path}")' for path in read_roots],
        *[f'  (literal "{path}")' for path in read_files],
        '  (literal "/dev/null")',
        '  (literal "/dev/urandom"))',
        "(allow file-write*",
        f'  (subpath "{workspace}")',
        '  (literal "/dev/null")',
        '  (literal "/dev/stdout")',
        '  (literal "/dev/stderr"))',
    ]
    if allow_network:
        lines.append("(allow network*)")
    return "\n".join(lines)


def _limits(cpu_seconds, address_space_bytes, file_size_bytes):
    def apply():
        try:
            os.setsid()
        except OSError:
            pass
        for which, soft, hard in (
                (resource.RLIMIT_CPU, int(cpu_seconds), int(cpu_seconds)),
                (resource.RLIMIT_AS, int(address_space_bytes), int(address_space_bytes)),
                (resource.RLIMIT_FSIZE, int(file_size_bytes), int(file_size_bytes)),
                (resource.RLIMIT_NOFILE, 256, 256)):
            try:
                current_soft, current_hard = resource.getrlimit(which)
                hard = current_hard if current_hard != resource.RLIM_INFINITY else hard
                soft = min(soft, hard) if hard != resource.RLIM_INFINITY else soft
                resource.setrlimit(which, (int(soft), int(hard)))
            except (OSError, ValueError, TypeError):
                continue
    return apply


def run_sandboxed(command, *, workspace, input_bytes=b"", timeout_seconds=300.0,
                  max_bytes=5_000_000, cpu_seconds=DEFAULT_CPU_SECONDS,
                  address_space_bytes=DEFAULT_ADDRESS_SPACE,
                  file_size_bytes=DEFAULT_FILE_SIZE, allow_network=False, env=None):
    """Run one pinned program under the sandbox and return bounded output."""
    if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item for item in command):
        raise ValidationError("sandbox command must be a nonempty argument list")
    workspace = Path(workspace)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValidationError("sandbox workspace must be an existing absolute directory")
    if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
        raise ValidationError("sandbox timeout must be positive")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValidationError("sandbox output limit must be a positive integer")
    process_env = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
    process_env.setdefault("PATH", "/usr/bin:/bin")
    process_env["PYTHONIOENCODING"] = "utf-8"
    process_env["TMPDIR"] = str(workspace)
    if env:
        process_env.update({key: value for key, value in env.items() if key in SAFE_ENV_KEYS})
    mode = "sandbox-exec"
    if SANDBOX_EXEC:
        command = [SANDBOX_EXEC, "-p", sandbox_profile(
            workspace, command, allow_network=allow_network), *command]
    else:
        mode = "rlimits-only"
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=str(workspace), env=process_env, shell=False, bufsize=0,
                               preexec_fn=_limits(cpu_seconds, address_space_bytes, file_size_bytes))
    stdout, stderr = bytearray(), bytearray()
    truncated = timed_out = False
    deadline = time.monotonic() + float(timeout_seconds)
    selector = selectors.DefaultSelector()
    try:
        for stream, buffer in ((process.stdout, stdout), (process.stderr, stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, buffer)
        os.set_blocking(process.stdin.fileno(), False)
        written = 0
        stdin_open = True
        while selector.get_map() or stdin_open:
            if stdin_open and written >= len(input_bytes):
                try:
                    process.stdin.close()
                except OSError:
                    pass
                stdin_open = False
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(min(0.25, remaining))
            for key, _ in events:
                try:
                    chunk = key.fileobj.read(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = key.data
                remaining_bytes = max_bytes - len(stdout) - len(stderr)
                if remaining_bytes > 0:
                    buffer.extend(chunk[:remaining_bytes])
                if len(chunk) > remaining_bytes:
                    truncated = True
                    break
            if truncated:
                break
            if stdin_open and written < len(input_bytes):
                try:
                    written += os.write(process.stdin.fileno(), input_bytes[written:written + 65536])
                except (BlockingIOError, InterruptedError):
                    pass
                except (BrokenPipeError, OSError):
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                    stdin_open = False
        if timed_out or truncated:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
    finally:
        selector.close()
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        returncode = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait()
        timed_out = True
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    return SandboxResult(returncode, bytes(stdout), bytes(stderr), timed_out, truncated, mode)
