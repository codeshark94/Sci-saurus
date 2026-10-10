#!/usr/bin/env python3
"""Resolve a portable laboratory template into a validated host profile.

This is a trusted operator step.  It substitutes explicit command-line
placeholders, validates the resolved laboratory with the strict laboratory
schema, and writes an immutable host profile.  It never reads a runtime path
from the caller environment, never launches a mission and never calls a model
provider.

Usage::

    python scripts/prepare-laboratory-profile.py \
        --template config/laboratory-metamaterial.example.json \
        --runtime-root /path/to/laboratory \
        --freecad-app /Applications/FreeCAD.app \
        --output deployment-profile.json
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scisaurus.core.schema import canonical_bytes  # noqa: E402
from scisaurus.runtime.laboratory import (  # noqa: E402
    laboratory_identity, load_laboratory, validate_laboratory,
)

TEMPLATE_SCHEMA = "metamaterial-laboratory-template-1"
PLACEHOLDERS = ("${LABORATORY_ROOT}", "${FREECAD_APP}")


def _substitute(value, mapping):
    if isinstance(value, str):
        for key, replacement in mapping.items():
            value = value.replace(key, replacement)
        return value
    if isinstance(value, list):
        return [_substitute(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, mapping) for key, item in value.items()}
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(prog="prepare-laboratory-profile")
    parser.add_argument("--template", required=True)
    parser.add_argument("--runtime-root", required=True,
                        help="operator directory containing the continuum and waves runtimes")
    parser.add_argument("--freecad-app", required=True,
                        help="FreeCAD application bundle for this host")
    parser.add_argument("--output", required=True)
    parser.add_argument("--binding", action="append", default=[], help="explicit additional NAME=VALUE template binding")
    args = parser.parse_args(argv)

    template_path = Path(args.template)
    try:
        template = json.loads(template_path.read_text())
    except (OSError, ValueError) as exc:
        print(f"laboratory template is unreadable: {exc}", file=sys.stderr)
        return 2
    if not isinstance(template, dict) or template.get("schema_version") != TEMPLATE_SCHEMA:
        print("not a portable laboratory template", file=sys.stderr)
        return 2
    runtime_root = Path(args.runtime_root).resolve()
    freecad_app = Path(args.freecad_app).resolve()
    if not runtime_root.is_dir():
        print(f"runtime root is not a directory: {runtime_root}", file=sys.stderr)
        return 2
    if not (freecad_app / "Contents" / "Resources" / "bin" / "python").is_file():
        print(f"FreeCAD application interpreter is absent under: {freecad_app}", file=sys.stderr)
        return 2
    mapping = {"${LABORATORY_ROOT}": str(runtime_root), "${FREECAD_APP}": str(freecad_app)}
    available = set(re.findall(r"\$\{([A-Z_]+)\}", json.dumps(template)))
    for entry in args.binding:
        name, separator, value = entry.partition("=")
        token = "${" + name + "}"
        if not separator or not value or name not in available or token in mapping:
            print("invalid, duplicate or unused explicit template binding", file=sys.stderr)
            return 2
        mapping[token] = value
    laboratory = _substitute(template["laboratory"], mapping)
    if re.search(r"\$\{[A-Z_]+\}", json.dumps(laboratory)):
        print("unresolved laboratory template binding", file=sys.stderr)
        return 2
    try:
        validate_laboratory(laboratory)
    except Exception as exc:  # noqa: BLE001 - report the exact validation failure
        print(f"resolved laboratory is invalid: {exc}", file=sys.stderr)
        return 2
    missing = [row["executable"] for row in laboratory["runtimes"]
               if not Path(row["executable"]).is_file()]
    if missing:
        print(f"resolved laboratory declares absent runtimes: {missing}", file=sys.stderr)
        return 2
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        print("profile output already exists; use a new immutable profile path", file=sys.stderr)
        return 2
    output.write_bytes(canonical_bytes(laboratory) + b"\n")
    print(json.dumps({
        "status": "prepared", "output": str(output),
        "laboratory_id": laboratory["id"],
        "laboratory_config_sha256": laboratory_identity(laboratory),
        "runtimes": [row["label"] for row in laboratory["runtimes"]],
        "model_calls": 0, "mission_started": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
