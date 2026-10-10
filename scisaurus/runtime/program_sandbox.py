"""Sandboxed execution of model-authored experiment programs (foundry P3.2).

The sandbox is the real security boundary for a generative capability.  It
combines two independent controls:

* a macOS ``sandbox-exec`` profile that denies everything by default and then
  allows only process execution, reads, writes inside the throwaway workspace
  and its private temporary namespace, local Unix sockets in that namespace,
  and (optionally) network access;
* POSIX resource limits applied in the child (CPU seconds, address space, file
  size, open files) plus an absolute wall deadline enforced by the parent.

When ``sandbox-exec`` is unavailable the runner still applies the resource
limits but reports ``mode = "rlimits-only"`` so the caller can refuse to treat a
candidate as fully admitted.  Isolation strength is never silently downgraded.
"""
from __future__ import annotations

import os
import json
import resource
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from scisaurus.core.errors import ValidationError

SANDBOX_EXEC = shutil.which("sandbox-exec")
# Sandboxed children inherit no caller-selected execution environment.
SAFE_ENV_KEYS = ()

# Environment variables an operator-declared runtime may set for its own
# process.  They are consumed only from a validated laboratory profile and are
# kept separate from SAFE_ENV_KEYS, so caller inheritance can never smuggle a
# module search path or a dynamic-loader path into a run.
RUNTIME_ENV_KEYS = frozenset({
    "PYTHONHOME", "PYTHONPATH", "DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH",
    "CFFIXED_USER_HOME", "FONTCONFIG_PATH", "FONTCONFIG_FILE",
    "SSL_CERT_FILE", "GIT_SSL_CAINFO",
    "SCI_SOLVER_COMMANDS", "SCI_LABORATORY_RUNTIMES", "ELMER_HOME", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
})

# Cache/config roots are forced under the throwaway workspace. run_sandboxed
# replaces TMPDIR with its own short, private temporary namespace; a declared
# runtime cannot redirect either directory set into the operator's home.
WORKSPACE_ENV_KEYS = ("HOME", "CFFIXED_USER_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                      "XDG_DATA_HOME", "TMPDIR", "MPLCONFIGDIR")

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

    @property
    def completed(self):
        cleanup = getattr(self, "cleanup", None)
        return (self.returncode == 0 and not self.timed_out and not self.truncated
                and (cleanup is None or cleanup.get("completed") is True))


def sandbox_status():
    """Report the isolation strength available on this host."""
    return {"sandbox_exec": SANDBOX_EXEC, "mode": "sandbox-exec" if SANDBOX_EXEC else "rlimits-only"}


def probe_sandbox(*, timeout_seconds=30.0):
    """Test whether the deny-by-default profile can actually be applied.

    ``sandbox_status`` reports the presence of the launcher.  A nested sandbox
    (for example Seatbelt inside another Seatbelt) can expose the binary while
    ``sandbox_apply`` is denied, so callers that require a real security
    boundary must probe rather than trust the presence check.
    """
    import tempfile
    if not SANDBOX_EXEC:
        return {"available": False, "mode": "rlimits-only",
                "reason": "sandbox-exec is not installed"}
    with tempfile.TemporaryDirectory(prefix="sandbox-probe-") as workspace:
        result = run_sandboxed(["/bin/sh", "-c", "printf probe-ok"], workspace=workspace,
                               timeout_seconds=timeout_seconds)
    available = (result.returncode == 0 and result.mode == "sandbox-exec"
                 and b"probe-ok" in result.stdout)
    return {"available": available, "mode": result.mode if available else "rlimits-only",
            "returncode": result.returncode,
            "stderr": result.stderr.decode("utf-8", errors="replace")[:2000]}


def workspace_environment(workspace):
    """Return the cache/config/temp variables forced under *workspace*."""
    workspace = Path(workspace)
    return {
        "HOME": str(workspace),
        "CFFIXED_USER_HOME": str(workspace),
        "XDG_CONFIG_HOME": str(workspace / ".config"),
        "XDG_CACHE_HOME": str(workspace / ".cache"),
        "XDG_DATA_HOME": str(workspace / ".local" / "share"),
        "TMPDIR": str(workspace),
        "MPLCONFIGDIR": str(workspace / ".matplotlib"),
    }


def sandbox_environment(workspace, *, env=None):
    """Build the exact child environment for one sandboxed or direct run.

    Composition order is deliberate: fixed system PATH and locale first, then
    the operator-declared runtime whitelist, then the forced workspace-private
    directories.  A declared runtime can never override HOME or the XDG/temp
    roots, and a caller can never inject PATH/PYTHONHOME/PYTHONPATH/DYLD_*/HOME.
    """
    workspace = Path(workspace)
    process_env = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
    process_env.update(PATH="/usr/bin:/bin", LANG="C", LC_ALL="C")
    process_env["PYTHONIOENCODING"] = "utf-8"
    process_env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env:
        process_env.update({key: value for key, value in env.items() if key in RUNTIME_ENV_KEYS})
        # Package installers may resolve dependencies from an explicitly
        # provisioned private R library, never from the caller's environment.
        if "R_LIBS_USER" in env:
            library = Path(env["R_LIBS_USER"]).resolve()
            if not library.is_relative_to(workspace.resolve()):
                raise ValidationError("R dependency library must belong to the sandbox workspace")
            process_env["R_LIBS_USER"] = str(library)
    process_env.update(workspace_environment(workspace))
    return process_env


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
        Path("/bin"), Path("/usr/bin"), Path("/usr/lib"),
        Path("/Library/Developer/CommandLineTools"),
    }
    # A virtual environment keeps its interpreter and site-packages under one
    # prefix. Never infer a broader user-home root from an arbitrary command.
    if configured.parent.name == "bin" and (configured.parent.parent / "pyvenv.cfg").is_file():
        roots.add(configured.parent.parent.resolve())
    files = {configured.resolve(), executable}
    for selected in ("/private/var/select/sh", "/var/select/sh", "/private/var/select/developer_dir", "/var/select/developer_dir"):
        selector = Path(selected)
        if selector.exists():
            files.update({selector, selector.resolve()})
            if selector.resolve().is_dir():
                roots.add(selector.resolve())
    files.update(_macho_dependency_paths(executable))
    for argument in command[1:]:
        candidate = Path(argument)
        if candidate.is_absolute() and candidate.exists():
            files.add(candidate.resolve())
    return sorted(str(path) for path in roots if path.exists()), sorted(str(path) for path in files)


def sandbox_profile(workspace, command, *, allow_network=False, read_only_paths=(), temporary_workspace=None,
                    directory_sync_paths=()):
    """Return a deny-by-default Seatbelt profile for one throwaway workspace."""
    workspace = str(Path(workspace).resolve())
    read_roots, read_files = _sandbox_read_paths(command, workspace)
    if temporary_workspace is not None:
        temporary_workspace = Path(temporary_workspace)
        if not temporary_workspace.is_absolute() or not temporary_workspace.is_dir():
            raise ValidationError("sandbox temporary workspace must be an existing absolute directory")
        temporary_workspace = str(temporary_workspace.resolve())
        read_roots.append(temporary_workspace)
    for value in read_only_paths:
        path = Path(value)
        if not path.is_absolute() or not path.is_dir():
            raise ValidationError("sandbox dependency read roots must be existing absolute directories")
        read_roots.append(str(path.resolve()))
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
    if temporary_workspace is not None:
        lines.extend([
            f'(allow file-write* (subpath "{temporary_workspace}"))',
            f'(allow network-bind network-inbound network-outbound (subpath "{temporary_workspace}"))',
        ])
    if allow_network:
        lines.append("(allow network*)")
    for value in directory_sync_paths:
        path = Path(value)
        if not path.is_absolute() or not path.is_dir():
            raise ValidationError("directory synchronization paths must be existing absolute directories")
        lines.append('(allow file-read-data (require-all (literal ' +
                     json.dumps(str(path.resolve())) + ') (vnode-type DIRECTORY)))')
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
                  file_size_bytes=DEFAULT_FILE_SIZE, allow_network=False, env=None, read_only_paths=()):
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
    # AF_UNIX addresses have a small fixed byte limit. Controller-owned short
    # temporary paths let spawned managers communicate even for deeply nested
    # project workspaces, without allowing sockets in another run's namespace.
    with tempfile.TemporaryDirectory(prefix="sci-run-", dir="/tmp") as temporary:
        temporary_workspace = Path(temporary).resolve()
        process_env = sandbox_environment(workspace, env=env)
        process_env["TMPDIR"] = str(temporary_workspace)
        mode = "sandbox-exec"
        if SANDBOX_EXEC:
            command = [SANDBOX_EXEC, "-p", sandbox_profile(
                workspace, command, allow_network=allow_network, read_only_paths=read_only_paths,
                temporary_workspace=temporary_workspace), *command]
        else:
            mode = "rlimits-only"
        from scisaurus.runtime.run_control import start_process
        process = start_process(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=str(workspace), env=process_env, shell=False, bufsize=0,
                                   preexec_fn=_limits(cpu_seconds, address_space_bytes, file_size_bytes))
        return capture_process(process, input_bytes=input_bytes, timeout_seconds=timeout_seconds,
                               max_bytes=max_bytes, mode=mode)


def capture_process(process, *, input_bytes, timeout_seconds, max_bytes, mode, check_permission=None):
    """Bound transport output and lifetime for an already isolated process group."""
    stdout, stderr = bytearray(), bytearray()
    truncated = timed_out = False
    deadline = time.monotonic() + float(timeout_seconds)
    selector = selectors.DefaultSelector()
    group_stopped = False

    def stop_group():
        nonlocal group_stopped
        if group_stopped:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        group_stopped = True

    try:
        for stream, buffer in ((process.stdout, stdout), (process.stderr, stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, buffer)
        os.set_blocking(process.stdin.fileno(), False)
        written = 0
        stdin_open = True
        while selector.get_map() or stdin_open or process.poll() is None:
            if check_permission is not None:
                check_permission()
            if process.poll() is not None:
                # Managers and forked workers can retain inherited pipe handles
                # after the program exits. Their lifetime belongs to this run,
                # so end the group and drain buffered output instead of waiting
                # for an unrelated descendant to close every duplicate handle.
                stop_group()
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
    except BaseException as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait()
        error.process_result = SandboxResult(returncode, bytes(stdout), bytes(stderr), timed_out, truncated, mode)
        for stream in (process.stdout, process.stderr):
            stream.close()
        raise
    finally:
        selector.close()
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        returncode = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        stop_group()
        process.kill()
        returncode = process.wait()
        timed_out = True
    stop_group()
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    return SandboxResult(returncode, bytes(stdout), bytes(stderr), timed_out, truncated, mode)
