#!/usr/bin/env python3
"""Outer-controller verification of the metamaterial-laboratory contracts.

This script reproduces, offline, the exact production contract failures and
then verifies the repaired sources:

1. MIME type/subtype validation accepts the controller's real
   ``application/step``, ``application/octet-stream`` and
   ``application/x-hdf5`` declarations and rejects bare tokens.
2. ``run.inputs`` uses the shared exact public contract field set.
3. Every solver program emits exactly one JSON object on stdout, with all
   solver/native diagnostics on stderr.  Strict parsing (never a last-JSON
   scan) is what the workbench uses.
4. The declared checks of each probe pass.

The native deny-by-default sandbox is *never* faked.  When ``sandbox-exec``
cannot be applied (for example a nested Seatbelt that denies ``sandbox_apply``)
this script refuses to run unless the operator passes ``--allow-unsandboxed``;
unsandboxed output is labelled diagnostic-only and is ineligible for production,
scientific readiness or admission.  A controller-run verification under the real
production sandbox is required before any production success claim.

Usage::

    python scripts/verify-laboratory-contracts.py \
        --profile deployment-profile.json \
        --output /path/to/verification [--allow-unsandboxed]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scisaurus.runtime.software_workbench import (  # noqa: E402
    DECLARED_INPUT_FIELDS, DECLARED_OUTPUT_FIELDS, _MEDIA_TYPE,
)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def mime_contract():
    accepted = ["application/step", "application/octet-stream", "application/x-hdf5"]
    rejected = ["step", "application", "application/", "/step", "application step"]
    good = all(_MEDIA_TYPE.fullmatch(value) for value in accepted)
    bad = all(not _MEDIA_TYPE.fullmatch(value) for value in rejected)
    return {"accepted": accepted, "rejected": rejected,
            "accepted_ok": good, "rejected_ok": bad, "passed": good and bad}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="verify-laboratory-contracts")
    parser.add_argument("--profile", default="deployment-profile.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-unsandboxed", action="store_true",
                        help="explicitly allow diagnostic-only offline execution; this can never "
                             "establish production, scientific readiness or admission")
    args = parser.parse_args(argv)

    from scisaurus.runtime.program_sandbox import probe_sandbox
    sandbox = probe_sandbox()
    isolated = bool(sandbox["available"])
    if not isolated and not args.allow_unsandboxed:
        print(json.dumps({
            "status": "sandbox_unavailable", "sandbox_probe": sandbox,
            "hint": "re-run under the production sandbox, or pass --allow-unsandboxed for "
                    "diagnostic-only offline verification"},
            sort_keys=True))
        return 2

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)

    command = [sys.executable, str(REPO_ROOT / "scripts" / "run-laboratory-probes.py"),
               "--laboratory", str(Path(args.profile).resolve()),
               "--output", str(output / "probes")]
    if not isolated:
        command.append("--allow-unsandboxed")
    started = time.monotonic()
    completed = subprocess.run(command, capture_output=True, text=True, cwd=str(REPO_ROOT))
    rc = completed.returncode
    report_path = output / "probes" / "probe-results.json"
    report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    steps = report.get("steps", [])
    all_passed = bool(steps) and all(
        row.get("outcome") == "ok" and row.get("probe_passed") is True for row in steps)
    verification = {
        "schema_version": "metamaterial-laboratory-contract-verification-1",
        "profile": str(Path(args.profile).resolve()),
        "profile_sha256": _sha(args.profile),
        "sandbox_probe": sandbox,
        "isolated": isolated,
        "diagnostic_only": not isolated,
        "production_eligible": False,
        "run_command": command,
        "run_returncode": rc,
        "run_stdout": completed.stdout[-20000:],
        "run_stderr": completed.stderr[-20000:],
        "mime_contract": mime_contract(),
        "declared_input_contract": list(DECLARED_INPUT_FIELDS),
        "declared_output_contract": list(DECLARED_OUTPUT_FIELDS),
        "probe_steps": [{key: row.get(key) for key in
                         ("id", "runtime", "outcome", "probe_passed", "error")}
                        for row in steps],
        "probe_artifact_count": report.get("artifact_count"),
        "elapsed_seconds": time.monotonic() - started,
        "result": "passed" if (all_passed and rc == 0 and mime_contract()["passed"]) else "failed",
    }
    (output / "verification.json").write_text(json.dumps(verification, indent=2, sort_keys=True))
    print(json.dumps({key: verification[key] for key in (
        "schema_version", "isolated", "diagnostic_only", "production_eligible",
        "run_returncode", "result", "probe_steps")}, sort_keys=True))
    return 0 if verification["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
