#!/usr/bin/env python3
"""Prepare an inert experiment configuration from the pinned local runtime."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


def prepare_config(output, *, repo_root=None):
    repo = Path(repo_root or Path(__file__).resolve().parent.parent).resolve()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists; refusing to overwrite: {output}")
    template = json.loads((repo / "config" / "experiment-run.example.json").read_text())
    if (template["live_dispatch_allowed"] is not False
            or template["model"]["base_url"] != "runtime_required"
            or template["model"]["model"] != "runtime_required"
            or template["model"]["auth_env"] is not None):
        raise ValueError("Experiment example must keep dispatch disabled and model connection unset")
    runtime = repo / ".runs" / "experiment-runtime" / "bin" / "python"
    if not runtime.is_file():
        raise FileNotFoundError("Run scripts/setup-experiment-runtime.sh first")
    probe = subprocess.run([str(runtime), "-c",
        "import json,numpy,matplotlib; print(json.dumps({'numpy':numpy.__version__,'matplotlib':matplotlib.__version__}))"],
        capture_output=True, text=True, check=True, timeout=30)
    versions = json.loads(probe.stdout)
    if versions != {"numpy": "2.5.2", "matplotlib": "3.11.1"}:
        raise ValueError("Experiment runtime versions do not match requirements-experiment.txt")
    requirements = repo / "requirements-experiment.txt"
    programs = {"execution": repo / "scripts" / "experiments" / "robust_mean_study.py",
                "validation": repo / "scripts" / "experiments" / "validate_robust_mean.py"}
    for name, program in programs.items():
        capability = template["experiment"][name]
        # Keep the virtual-environment launcher path. Resolving its symlink selects
        # the base interpreter and silently drops the environment's site-packages.
        capability["client"]["command"] = [str(runtime.absolute()), str(program.resolve())]
        capability["client"]["cwd"] = str(repo)
        capability["environment_files"] = [str(requirements), str(program)]
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(template, ensure_ascii=False, indent=2) + "\n")
    return {"output": str(output), "runtime": str(runtime), "versions": versions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare_config(args.output)
        print(f"Prepared {result['output']}")
        print(f"Interpreter: {result['runtime']}")
        for name, version in result["versions"].items():
            print(f"Installed: {name}=={version}")
        print("Set the model connection, optional literature gate, and live_dispatch_allowed before running.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"Configuration preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
