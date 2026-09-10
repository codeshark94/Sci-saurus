#!/usr/bin/env python3
"""Prepare an inert local-program configuration from the installed runtime.

Pins cover the checker, virtual-environment configuration, and runtime files
from jsonschema and its four installed dependencies. They are scoped identity
inputs, not a complete attestation of Python or the operating system. Package
metadata is inspected without running the checker or any network operation.
"""
from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys


PACKAGES = {
    "jsonschema": ("jsonschema",),
    "referencing": ("referencing",),
    "attrs": ("attr", "attrs"),
    "rpds-py": ("rpds",),
    "jsonschema-specifications": ("jsonschema_specifications",),
}
JSONSCHEMA_VERSION = "4.26.0"


def required_file(path):
    path = Path(path).absolute()
    if not path.is_file():
        raise ValueError(f"Required runtime file is missing: {path}")
    return str(path)


def inspect_runtime():
    files, versions = [], {}
    for name, modules in PACKAGES.items():
        distribution = metadata.distribution(name)
        versions[name] = distribution.version
        installed = list(distribution.files or [])
        runtime = [entry for entry in installed
                   if entry.parts[0] in modules and not {"tests", "__pycache__"}.intersection(entry.parts)]
        if not runtime:
            raise ValueError(f"Installed runtime inventory is unavailable: {name}")
        files.extend(required_file(distribution.locate_file(entry)) for entry in runtime)
        for suffix in (".dist-info/METADATA", ".dist-info/RECORD"):
            matches = [entry for entry in installed if str(entry).endswith(suffix)]
            if len(matches) != 1:
                raise ValueError(f"Installed distribution lacks an unambiguous {suffix}: {name}")
            files.append(required_file(distribution.locate_file(matches[0])))
    if versions["jsonschema"] != JSONSCHEMA_VERSION:
        raise ValueError(f"JSON checker requires jsonschema=={JSONSCHEMA_VERSION}")
    return {"prefix": sys.prefix, "versions": versions, "environment_files": sorted(set(files))}


def prepare_config(output, *, repo_root=None):
    repo = Path(repo_root or Path(__file__).resolve().parent.parent).resolve()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists; refusing to overwrite: {output}")
    template = json.loads((repo / "config/operations-run.example.json").read_text())
    capabilities = template["score"]["capabilities"]
    if (len(capabilities) != 1 or capabilities[0]["adapter"] != "local_program"
            or capabilities[0]["id"] != "json-schema-check"):
        raise ValueError("Operations example must declare the local JSON schema checker")
    # Keep the .venv symlink path so Python selects the virtual environment.
    interpreter = repo / ".venv/bin/python"
    if not os.access(interpreter, os.X_OK):
        raise ValueError("Repository .venv interpreter is unavailable; run scripts/setup-runtime.sh")
    checker = required_file(repo / "scripts/validate-json-artifact.py")
    pinned_inputs = [checker, required_file(repo / ".venv/pyvenv.cfg")]
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL") if key in os.environ}
    completed = subprocess.run([str(interpreter), "-I", str(Path(__file__).resolve()), "--inspect-runtime"],
                               cwd=repo, env=environment, check=False, capture_output=True, text=True, timeout=30)
    if completed.returncode:
        raise ValueError("Installed JSON checker runtime inspection failed: " + completed.stderr.strip()[:2000])
    inspected = json.loads(completed.stdout)
    if Path(inspected["prefix"]).resolve() != repo / ".venv":
        raise ValueError("Configured interpreter did not use the repository .venv")
    pinned_inputs.extend(required_file(path) for path in inspected["environment_files"])
    capabilities[0]["client"]["command"] = [str(interpreter), "-I", checker]
    capabilities[0]["environment_files"] = sorted(set(pinned_inputs))
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(template, ensure_ascii=False, indent=2) + "\n")
    return {"output": str(output), "interpreter": str(interpreter), "versions": inspected["versions"],
            "environment_files": capabilities[0]["environment_files"]}


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
