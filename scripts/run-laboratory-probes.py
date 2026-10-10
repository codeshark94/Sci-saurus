#!/usr/bin/env python3
"""Run the metamaterial-laboratory operational probes end to end.

The probes are implementation checks of the operator-provisioned CAD, meshing,
continuum and electromagnetic solvers.  They are not research results, they do
not create a mission, and they do not call a model provider.

Isolation contract
------------------
Every probe runs through :class:`SoftwareWorkbench`, which requires the native
deny-by-default sandbox.  When the nested Seatbelt denies ``sandbox_apply`` the
runner does **not** silently downgrade: it stops unless the operator passes the
explicit ``--allow-unsandboxed`` diagnostics flag.  Unsandboxed output is
labelled ``diagnostic_only`` and is ineligible for production, scientific
readiness or admission.

Artifact flow
-------------
Every probe declares its inputs and outputs with the shared public contract
(``artifact_ref``/``name``, optional ``media_type``).  Earlier outputs are
retained content-addressed and staged into a later probe under a safe relative
name after their hash is re-verified.

Usage::

    python scripts/run-laboratory-probes.py \
        --laboratory config/laboratory-metamaterial.json \
        --output /path/to/probe-run [--reprovision] [--skip cad]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scisaurus.runtime.laboratory import (  # noqa: E402
    LaboratoryBinding, direct_runner, load_attestation, provision_laboratory,
    resolve_runtime_environment, runtime_read_roots,
)
from scisaurus.runtime.program_sandbox import probe_sandbox  # noqa: E402
from scisaurus.runtime.programs import _parse_object  # noqa: E402
from scisaurus.runtime.software_workbench import SoftwareWorkbench  # noqa: E402

STEPS = [
    {
        "id": "cad", "runtime": "freecad",
        "script": "scripts/laboratory/probe_freecad_step.py",
        "params": {"params": {"Lx": 20.0, "Ly": 10.0, "t": 2.0, "r": 1.2,
                              "pitch": 4.0, "nx": 3, "ny": 2}},
        "inputs": [],
        "outputs": [{"name": "part.step", "media_type": "application/step"},
                    {"name": "part.FCStd", "media_type": "application/octet-stream"}],
    },
    {
        "id": "mesh", "runtime": "meshing",
        "script": "scripts/laboratory/probe_gmsh_volume_mesh.py",
        "params": {"step": "inputs/part.step", "mesh_size_m": 0.0015},
        "inputs": [{"name": "part.step"}],
        "outputs": [{"name": "part.msh", "media_type": "application/octet-stream"},
                    {"name": "part_volume.vtk", "media_type": "application/octet-stream"}],
    },
    {
        "id": "thermoelastic", "runtime": "continuum",
        "script": "scripts/laboratory/probe_sfepy_thermoelastic.py",
        "params": {"mesh": "inputs/part_volume.vtk", "lam": 10.0, "mu": 5.0,
                   "alpha": 1.25e-5, "T_hot": 100.0, "T_cold": 20.0},
        "inputs": [{"name": "part_volume.vtk"}],
        "outputs": [{"name": "thermoelastic.vtk", "media_type": "application/octet-stream"},
                    {"name": "thermoelastic.h5", "media_type": "application/x-hdf5"}],
    },
    {
        "id": "acoustic", "runtime": "continuum",
        "script": "scripts/laboratory/probe_sfepy_acoustic.py",
        "params": {"c": 343.0, "frequency": 300.0, "length": 1.0, "width": 0.25},
        "inputs": [],
        "outputs": [{"name": "acoustic.vtk", "media_type": "application/octet-stream"}],
    },
    {
        "id": "meep", "runtime": "waves",
        "script": "scripts/laboratory/probe_meep_time_evolution.py",
        "params": {"resolution": 16, "cell": 4.0, "until": 12.0, "frequency": 0.6},
        "inputs": [],
        "outputs": [{"name": "meep_fields.h5", "media_type": "application/x-hdf5"}],
    },
    {
        "id": "mpb", "runtime": "waves",
        "script": "scripts/laboratory/probe_mpb_eigenmode.py",
        "params": {"resolution": 16, "num_bands": 4, "epsilon": 11.56, "rod": 0.3},
        "inputs": [],
        "outputs": [{"name": "mpb_bands.h5", "media_type": "application/x-hdf5"}],
    },
]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _run_direct(runtime, script_path, step_dir, request):
    command = [runtime["executable"], str(script_path)]
    environment = resolve_runtime_environment(runtime, step_dir)
    started = time.monotonic()
    result = direct_runner(
        command, workspace=str(step_dir),
        input_bytes=json.dumps(request, sort_keys=True).encode("utf-8"),
        timeout_seconds=float(runtime.get("resource_limits", {}).get("cpu_seconds", 600)),
        max_bytes=5_000_000, env=environment,
        read_only_paths=runtime_read_roots(runtime))
    return {
        "command": command, "mode": result.mode, "returncode": result.returncode,
        "stdout": result.stdout.decode("utf-8", errors="replace")[:20000],
        "stderr": result.stderr.decode("utf-8", errors="replace")[:20000],
        "timed_out": result.timed_out, "truncated": result.truncated,
        "elapsed_seconds": time.monotonic() - started,
    }


def _run_sandboxed(workbench, runtime_label, script_path, declared_inputs, declared_outputs, request):
    source = script_path.read_text()
    action = {"operation": "run", "arguments": {
        "runtime": runtime_label, "source": source, "input": request,
        "purpose": "scientific_computation", "inputs": declared_inputs,
        "outputs": declared_outputs, "documentation_refs": [], "expected": None}}
    return workbench.execute(action)


def _evaluate(result, record):
    """Gate a step on the probe's own measured checks, not just its exit code."""
    report = result.get("output")
    record["probe_report"] = report
    if not isinstance(report, dict):
        record.update(outcome="failed", error="probe did not emit one JSON object")
        return False
    record["probe_passed"] = report.get("passed") is True
    if report.get("passed") is not True:
        record.update(outcome="failed", error="probe measured checks did not pass")
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run-laboratory-probes")
    parser.add_argument("--laboratory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--reprovision", action="store_true")
    parser.add_argument("--allow-unsandboxed", action="store_true",
                        help="explicitly run diagnostics without the native sandbox; results are "
                             "ineligible for production, scientific readiness or admission")
    parser.add_argument("--skip", action="append", default=[],
                        help="probe id to skip; may be repeated")
    args = parser.parse_args(argv)

    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    sandbox = probe_sandbox()
    isolated = bool(sandbox["available"])
    if not isolated and not args.allow_unsandboxed:
        print(json.dumps({
            "schema_version": "metamaterial-laboratory-probe-run-1",
            "sandbox_probe": sandbox,
            "status": "sandbox_unavailable",
            "hint": "the native deny-by-default sandbox could not be applied; pass "
                    "--allow-unsandboxed for diagnostic-only execution (never production)"},
            sort_keys=True))
        return 2
    from scisaurus.runtime.laboratory import load_laboratory
    laboratory = load_laboratory(args.laboratory)
    attestation_path = output / "laboratory-attestation.json"
    if args.reprovision or not attestation_path.exists():
        runner = None if isolated else direct_runner
        attestation = provision_laboratory(laboratory, output, deadline=time.monotonic() + 3600,
                                           runner=runner)
    else:
        attestation = load_attestation(attestation_path)
    binding = LaboratoryBinding(laboratory, attestation)
    workbench = SoftwareWorkbench(str(output / "workbench"), deadline=time.monotonic() + 3600,
                                  laboratory=binding)
    results, artifacts = [], {}
    for step in STEPS:
        if step["id"] in args.skip:
            continue
        script_path = (REPO_ROOT / step["script"]).resolve()
        runtime = binding.runtime(step["runtime"])
        step_dir = output / f"step-{step['id']}"
        step_dir.mkdir(parents=True, exist_ok=True)
        record = {"id": step["id"], "runtime": step["runtime"], "isolated": isolated,
                  "diagnostic_only": not isolated, "sandbox_probe": sandbox,
                  "script": step["script"]}
        missing = [row["name"] for row in step["inputs"] if row["name"] not in artifacts]
        if missing:
            record.update(outcome="skipped", error=f"missing upstream artifacts {missing}",
                          artifacts={})
            results.append(record)
            continue
        # Shared exact public run-input contract: artifact_ref + name only.
        declared_inputs = [{"artifact_ref": artifacts[row["name"]], "name": row["name"]}
                           for row in step["inputs"]]
        request = dict(step["params"])
        try:
            if isolated:
                execution = _run_sandboxed(workbench, step["runtime"], script_path,
                                           declared_inputs, step["outputs"], request)
                if execution["outcome"] == "ok":
                    result = execution["result"]
                    outputs = result["outputs"]
                    record.update(outcome="ok", execution=result["execution"], outputs=outputs,
                                  stdout_sha256=result["stdout_sha256"])
                    _evaluate({"output": result["output"]}, record)
                else:
                    outputs = []
                    record.update(outcome="failed", error=execution.get("error"),
                                  execution=execution.get("execution"), outputs=[])
            else:
                # Diagnostic-only path: the declared input artifacts are still
                # validated and staged through the shared workbench contract, but
                # the result is never production or scientific readiness evidence.
                normalized = workbench._declared_inputs(
                    declared_inputs, binding.laboratory["limits"])
                staged = workbench._stage_inputs(normalized, step_dir) if normalized else []
                execution = _run_direct(runtime, script_path, step_dir, request)
                if execution["returncode"] != 0 or execution["timed_out"]:
                    record.update(outcome="failed", execution=execution, error="probe exited nonzero",
                                  inputs=staged, outputs=[])
                    results.append(record)
                    continue
                try:
                    report = _parse_object(execution["stdout"].encode("utf-8"))
                except ValueError as exc:
                    record.update(outcome="failed", execution=execution,
                                  error=f"probe stdout was not exactly one JSON object: {exc}",
                                  inputs=staged, outputs=[])
                    results.append(record)
                    continue
                outputs = workbench._collect_outputs(step["outputs"], step_dir,
                                                      binding.laboratory["limits"])
                record.update(outcome="ok", execution=execution, inputs=staged,
                              outputs=outputs)
                _evaluate({"output": report}, record)
            for row in outputs:
                artifacts[row["name"]] = row["artifact_ref"]
            record["artifacts"] = {row["name"]: row["artifact_ref"] for row in outputs}
        except Exception as exc:  # noqa: BLE001 - preserve exact probe failure
            record.update(outcome="failed", error=f"{type(exc).__name__}: {exc}")
        results.append(record)
    required_ok = (len(results) == len(STEPS) and
                   all(row.get("outcome") == "ok" and row.get("probe_passed") is True
                       for row in results))
    summary = {
        "schema_version": "metamaterial-laboratory-probe-run-1",
        "laboratory_id": laboratory["id"],
        "laboratory_config_sha256": binding.identity,
        "sandbox_probe": sandbox,
        "isolated": isolated,
        "diagnostic_only": not isolated,
        "production_eligible": bool(isolated and required_ok),
        "attestation": {row["label"]: row["verified"] for row in attestation["runtimes"]},
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "steps": results,
        "purpose": "operational_check_not_research_result",
        "limitations": [
            "These probes validate the installed toolchain and the artifact handoff; they are not admitted mission output.",
            "Unsandboxed diagnostics are ineligible for production, scientific readiness or admission.",
        ] + ([] if isolated else [
            "The nested Seatbelt denied sandbox_apply; every step is diagnostic_only and must be "
            "re-run under the production sandbox before any readiness claim."]),
    }
    (output / "probe-results.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps({key: summary[key] for key in (
        "schema_version", "laboratory_id", "sandbox_probe", "isolated", "diagnostic_only",
        "production_eligible", "artifact_count", "artifacts")}, sort_keys=True))
    print(json.dumps({"steps": [{key: row.get(key) for key in (
        "id", "runtime", "outcome", "isolated", "probe_passed", "error")} for row in results]},
        sort_keys=True))
    return 0 if required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
