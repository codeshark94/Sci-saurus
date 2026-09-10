#!/usr/bin/env python3
"""Prepare an inert survey configuration using the installed runtime inventory.

Reuses project configuration preparation to verify package and extractor pins.
Does not call a model, fetch a source, install packages, or enable dispatch.
"""
from __future__ import annotations

import argparse
from importlib import metadata, util
import json
from pathlib import Path
import subprocess
import sys
import tempfile


SPEC = util.spec_from_file_location("prepare_project_config", Path(__file__).with_name("prepare-project-config.py"))
project_helper = util.module_from_spec(SPEC)
SPEC.loader.exec_module(project_helper)


def prepare_config(output, *, repo_root=None):
    repo = Path(repo_root or Path(__file__).resolve().parent.parent).resolve()
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Output already exists; refusing to overwrite: {output}")
    template = json.loads((repo / "config/survey-run.example.json").read_text())
    if (template["live_dispatch_allowed"] is not False
            or template["model"]["base_url"] != "runtime_required"
            or template["model"]["model"] != "runtime_required"
            or template["model"]["auth_env"] is not None):
        raise ValueError("Survey example must keep dispatch disabled and model connection unset")
    capability = template["survey"]["full_text"]
    if capability["adapter"] != "mcp_fetch":
        raise ValueError("Survey example must configure the official MCP Fetch adapter")
    with tempfile.TemporaryDirectory(prefix="scisaurus-survey-runtime-") as directory:
        project_path = Path(directory) / "project.json"
        inspected = project_helper.prepare_config(project_path, repo_root=repo)
        project = json.loads(project_path.read_text())
    capability["client"]["command"] = project["mcp_fetch_command"]
    capability["environment_files"] = project["operations"]["environment_files"]
    serialized = json.dumps(template, ensure_ascii=False, indent=2) + "\n"
    with output.open("x", encoding="utf-8") as stream:
        stream.write(serialized)
    return {"output": str(output), "interpreter": inspected["interpreter"],
            "versions": inspected["versions"], "environment_files": capability["environment_files"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True,
                        help="New JSON configuration file; existing files are never overwritten")
    args = parser.parse_args()
    try:
        result = prepare_config(args.output)
        print(f"Prepared {result['output']}")
        print(f"Interpreter: {result['interpreter']}")
        for name, version in result["versions"].items():
            print(f"Installed: {name}=={version}")
        print(f"Recorded {len(result['environment_files'])} scoped runtime files.")
        print("Set the model connection and live_dispatch_allowed explicitly before running.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, metadata.PackageNotFoundError, subprocess.SubprocessError) as exc:
        print(f"Configuration preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
