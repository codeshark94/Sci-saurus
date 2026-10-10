#!/usr/bin/env python3
"""Operational check: frequency-domain acoustic Helmholtz solve in SfePy.

Reads one JSON object on stdin::

    {"c": 343.0, "frequency": 300.0, "length": 1.0, "width": 0.25,
     "nx": 80, "ny": 20, "outputs": {"vtk": "acoustic.vtk"}}

Solves ``laplacian(p) - k^2 p = 0`` for the acoustic pressure with a unit
pressure inlet and a zero-pressure outlet.  The frequency is chosen below the
first transverse-duct cutoff so the one-dimensional plane-wave solution is a
valid cheap reference; the bounded acceptance criterion is the measured
relative error against that reference, not a fit.  Emits exactly one JSON
object on stdout; all solver diagnostics go to stderr.  This is a
solver-readiness check, not a research result.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

# Reserve the real stdout for the single JSON report.
_ORIGINAL_STDOUT = os.dup(1)
os.dup2(2, 1)

import numpy as np  # noqa: E402
from sfepy.base.conf import ProblemConf  # noqa: E402
from sfepy.discrete import Problem  # noqa: E402
from sfepy.mesh.mesh_generators import gen_block_mesh  # noqa: E402

ANALYTIC_RELATIVE_TOLERANCE = 1.0e-2
RESIDUAL_RELATIVE_TOLERANCE = 1.0e-8
RAW_SAMPLE = 64


def _sha(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def _emit(report):
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    os.write(_ORIGINAL_STDOUT, payload)
    os.close(_ORIGINAL_STDOUT)


def main():
    request = json.load(sys.stdin)
    c = float(request.get("c", 343.0))
    frequency = float(request.get("frequency", 300.0))
    length = float(request.get("length", 1.0))
    width = float(request.get("width", 0.25))
    nx = int(request.get("nx", 80))
    ny = int(request.get("ny", 20))
    outputs = request.get("outputs") or {}
    vtk_name = outputs.get("vtk", "acoustic.vtk")
    started = time.perf_counter()
    omega = 2.0 * np.pi * frequency
    wavenumber = omega / c
    cutoff = c / (2.0 * width)
    mesh = gen_block_mesh([length, width], [nx, ny], [length / 2, width / 2], name="duct")
    coors = mesh.coors
    xmin, xmax = float(coors[:, 0].min()), float(coors[:, 0].max())
    conf = {
        "filename_mesh": mesh,
        "integrals": {"i": ("v", 2)},
        "regions": {"Omega": "all",
                    "Left": (f"vertices in (x < {xmin + 1e-9})", "facet"),
                    "Right": (f"vertices in (x > {xmax - 1e-9})", "facet")},
        "fields": {"pressure": ("real", 1, "Omega", 1)},
        "variables": {"p": ("unknown field", "pressure", 0),
                      "q": ("test field", "pressure", "p")},
        "materials": {"air": ({"one": 1.0, "k2": wavenumber * wavenumber},)},
        "ebcs": {"p_in": ("Left", {"p.0": 1.0}), "p_out": ("Right", {"p.0": 0.0})},
        "equations": {"helmholtz": "dw_laplace.i.Omega(air.one, q, p) - dw_dot.i.Omega(air.k2, q, p) = 0"},
        "solvers": {"ls": ("ls.scipy_direct", {}),
                    "nls": ("nls.newton", {"i_max": 1, "eps_a": 1e-10})},
    }
    problem = Problem.from_conf(ProblemConf.from_dict(conf, sys.modules[__name__]))
    state = problem.solve()
    pressure = np.asarray(problem.get_variables()["p"]())
    residual = np.asarray(problem.equations.eval_residuals(state.get_state()))
    residual_abs = float(np.linalg.norm(residual))
    problem.save_state(vtk_name, state)
    analytic = np.sin(wavenumber * (xmax - coors[:, 0])) / np.sin(
        wavenumber * (xmax - xmin))
    relative_error = float(np.abs(pressure - analytic).max() / np.abs(analytic).max())
    with open(vtk_name, "rb") as stream:
        vtk_sha = hashlib.sha256(stream.read()).hexdigest()
    # Independent discretisation scale for the dimensional absolute residual.
    load_scale = (wavenumber ** 2) * float(np.sqrt(mesh.n_nod)) * float(np.abs(pressure).max() + 1.0)
    relative_residual = residual_abs / load_scale
    order = np.argsort(coors[:, 0])
    sample_index = order[np.unique(np.linspace(0, len(order) - 1, min(RAW_SAMPLE, len(order))).astype(int))]
    checks = {
        "below_transverse_cutoff": frequency < cutoff,
        "analytic_relative_error_bounded": relative_error <= ANALYTIC_RELATIVE_TOLERANCE,
        "residual_bounded": relative_residual <= RESIDUAL_RELATIVE_TOLERANCE,
    }
    report = {
        "tool": "sfepy", "physics": "acoustic_helmholtz",
        "nodes": int(mesh.n_nod), "frequency_hz": frequency, "speed_of_sound": c,
        "wavenumber": wavenumber, "transverse_cutoff_hz": cutoff,
        "pressure_max": float(pressure.max()), "analytic_max": float(np.abs(analytic).max()),
        "relative_1d_error": relative_error,
        "residual_abs": residual_abs,
        "load_scale": load_scale,
        "relative_residual": relative_residual,
        "raw_pressure_profile": {
            "x": [float(coors[index, 0]) for index in sample_index],
            "pressure": [float(pressure[index]) for index in sample_index],
            "analytic": [float(analytic[index]) for index in sample_index],
        },
        "checks": checks,
        "criteria": {"analytic_relative_tolerance": ANALYTIC_RELATIVE_TOLERANCE,
                     "residual_relative_tolerance": RESIDUAL_RELATIVE_TOLERANCE},
        "passed": all(checks.values()),
        "outputs": {vtk_name: vtk_sha},
        "elapsed_seconds": time.perf_counter() - started,
        "purpose": "operational_check_not_research_result",
    }
    _emit(report)


if __name__ == "__main__":
    main()
