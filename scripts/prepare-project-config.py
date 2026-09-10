#!/usr/bin/env python3
"""Prepare an inert project configuration from the installed repository runtime.

Pins cover the three named Python packages and selected extractor installation
inputs. They are not a complete attestation of all transitive dependencies.
The inspected packages are not imported; extraction, network requests and
installation commands are not executed.
"""
from __future__ import annotations

import argparse
from importlib import metadata, util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


PACKAGES = {"mcp-server-fetch": "mcp_server_fetch", "mcp": "mcp", "readabilipy": "readabilipy"}


def required_file(path):
    path = Path(path).absolute()
    if not path.is_file():
        raise ValueError(f"Required runtime file is missing: {path}")
    return str(path)


def inspect_runtime():
    """Use only distribution metadata and source paths in this interpreter."""
    files, versions = [], {}
    for name, module in PACKAGES.items():
        distribution = metadata.distribution(name)
        versions[name] = distribution.version
        installed = list(distribution.files or [])
        spec = util.find_spec(module)
        if spec is None or not spec.origin:
            raise ValueError(f"Installed module is unavailable: {module}")
        files.append(required_file(spec.origin))
        source_files = [entry for entry in installed if str(entry).startswith(module + "/") and str(entry).endswith(".py")]
        if not source_files:
            raise ValueError(f"Installed Python source inventory is unavailable: {name}")
        for suffix in (".dist-info/METADATA", ".dist-info/RECORD"):
            matches = [entry for entry in installed if str(entry).endswith(suffix)]
            if len(matches) != 1:
                raise ValueError(f"Installed distribution lacks an unambiguous {suffix}: {name}")
            files.append(required_file(distribution.locate_file(matches[0])))
        files.extend(required_file(distribution.locate_file(entry)) for entry in source_files)
    javascript = Path(metadata.distribution("readabilipy").locate_file("readabilipy/javascript"))
    for name in ("ExtractArticle.js", "package.json", "package-lock.json"):
        files.append(required_file(javascript / name))
    package = json.loads((javascript / "package.json").read_text())
    lock = json.loads((javascript / "package-lock.json").read_text())
    dependencies = package.get("dependencies", {})
    if not dependencies or not isinstance(lock.get("packages"), dict):
        raise ValueError("Extractor dependency inventory or npm lock is unavailable")
    for name in dependencies:
        directory = javascript / "node_modules" / name
        manifest_path = required_file(directory / "package.json")
        manifest = json.loads(Path(manifest_path).read_text())
        if manifest.get("version") != lock["packages"].get("node_modules/" + name, {}).get("version"):
            raise ValueError(f"Installed extractor dependency differs from its lock: {name}")
        entrypoint = manifest.get("main")
        if not isinstance(entrypoint, str) or not entrypoint:
            raise ValueError(f"Extractor dependency has no explicit main entrypoint: {name}")
        files.extend([manifest_path, required_file(directory / entrypoint)])
    node = shutil.which("node")
    if node is None:
        raise ValueError("The installed source extractor requires Node.js on PATH")
    files.append(required_file(node))
    return {"prefix": sys.prefix, "versions": versions, "environment_files": sorted(set(files)),
            "extractor_lock": str(javascript / "package-lock.json")}


def prepare_config(output, *, repo_root=None):
    repo = Path(repo_root or Path(__file__).resolve().parent.parent).resolve()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists; refusing to overwrite: {output}")
    template = json.loads((repo / "config/project-run.example.json").read_text())
    command = template.get("mcp_fetch_command")
    if not isinstance(command, list) or len(command) != 3 or command[1:] != ["-m", "mcp_server_fetch"]:
        raise ValueError("Project example must configure the official MCP Fetch module")
    # Resolving this final symlink would select the base interpreter, losing .venv.
    interpreter = Path(command[0])
    interpreter = interpreter.absolute() if interpreter.is_absolute() else repo / interpreter
    if interpreter.parent.parent != repo / ".venv" or not os.access(interpreter, os.X_OK):
        raise ValueError("The configured repository .venv interpreter is missing; run scripts/setup-runtime.sh")
    pinned_inputs = [required_file(repo / ".venv/pyvenv.cfg"), required_file(repo / "requirements-runtime.txt"),
                     required_file(repo / "scripts/readabilipy-package-lock.json")]
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if key in os.environ}
    completed = subprocess.run([str(interpreter), "-I", str(Path(__file__).resolve()), "--inspect-runtime"],
                               cwd=repo, env=environment, check=False, capture_output=True, text=True, timeout=30)
    if completed.returncode:
        raise ValueError("Installed runtime inspection failed; run scripts/setup-runtime.sh. " + completed.stderr.strip()[:2000])
    inspected = json.loads(completed.stdout)
    if Path(inspected["prefix"]).resolve() != repo / ".venv":
        raise ValueError("Configured interpreter did not use the repository .venv")
    expected = {}
    for line in (repo / "requirements-runtime.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if match is None:
            raise ValueError("Runtime requirements must contain explicit package==version pins")
        expected[match[1].lower().replace("_", "-")] = match[2]
    for name in PACKAGES:
        if not expected.get(name) or inspected["versions"].get(name) != expected[name]:
            raise ValueError(f"Installed {name} differs from requirements-runtime.txt; run scripts/setup-runtime.sh")
    if Path(inspected["extractor_lock"]).read_bytes() != (repo / "scripts/readabilipy-package-lock.json").read_bytes():
        raise ValueError("Installed extractor lock differs from the repository pin; run scripts/setup-runtime.sh")
    pinned_inputs.extend(required_file(path) for path in inspected["environment_files"])
    template["mcp_fetch_command"] = [str(interpreter), *command[1:]]
    template["operations"]["environment_files"] = sorted(set(pinned_inputs))
    serialized = json.dumps(template, ensure_ascii=False, indent=2) + "\n"
    with output.open("x", encoding="utf-8") as stream:
        stream.write(serialized)
    return {"output": str(output), "interpreter": str(interpreter), "versions": inspected["versions"],
            "environment_files": template["operations"]["environment_files"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New JSON configuration file; existing files are never overwritten")
    parser.add_argument("--inspect-runtime", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.inspect_runtime and args.output is None:
        parser.error("--output is required")
    try:
        if args.inspect_runtime:
            print(json.dumps(inspect_runtime()))
        else:
            result = prepare_config(args.output)
            print(f"Prepared {result['output']}")
            print(f"Interpreter: {result['interpreter']}")
            for name, version in result["versions"].items():
                print(f"Installed: {name}=={version}")
            print(f"Recorded {len(result['environment_files'])} scoped runtime files.")
            print("Set the model connection and live_dispatch_allowed explicitly before running.")
        return 0
    except (OSError, ValueError, KeyError, metadata.PackageNotFoundError, subprocess.SubprocessError) as exc:
        print(f"Configuration preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
