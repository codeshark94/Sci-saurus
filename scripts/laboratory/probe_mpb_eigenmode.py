#!/usr/bin/env python3
"""Operational check: MPB periodic eigenmode / band-structure solve.

Reads one JSON object on stdin::

    {"resolution": 16, "num_bands": 4, "epsilon": 11.56, "rod": 0.3,
     "k_intervals": 4, "outputs": {"hdf5": "mpb_bands.h5"}}

Runs a real MPB eigenmode solve for a square lattice of dielectric rods and
writes the eigenfrequencies and k-points to HDF5.  A separate empty-lattice
control (epsilon = 1) at k = (0.5, 0, 0) is compared with the exact free-photon
frequency 0.5 in units of c/a.  The band result describes the infinite lattice;
finite-size validation is a separate step.  Emits exactly one JSON object on
stdout; all native solver and atexit diagnostics go to stderr.
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

import h5py  # noqa: E402
import numpy as np  # noqa: E402
from meep import mpb  # noqa: E402
import meep as mp  # noqa: E402

EMPTY_LATTICE_RELATIVE_TOLERANCE = 1.0e-2


def _sha(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def _emit(report):
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    os.write(_ORIGINAL_STDOUT, payload)
    os.close(_ORIGINAL_STDOUT)


def _solver(num_bands, resolution, epsilon, rod):
    geometry = [] if epsilon == 1.0 else [
        mp.Block(mp.Vector3(rod, rod, mp.inf), material=mp.Medium(epsilon=epsilon))]
    return mpb.ModeSolver(num_bands=num_bands,
                          geometry_lattice=mp.Lattice(size=mp.Vector3(1, 1, 0)),
                          geometry=geometry, resolution=resolution,
                          default_material=mp.Medium(epsilon=1.0))


def main():
    request = json.load(sys.stdin)
    resolution = int(request.get("resolution", 16))
    num_bands = int(request.get("num_bands", 4))
    epsilon = float(request.get("epsilon", 11.56))
    rod = float(request.get("rod", 0.3))
    k_intervals = int(request.get("k_intervals", 4))
    outputs = request.get("outputs") or {}
    hdf5_name = outputs.get("hdf5", "mpb_bands.h5")
    started = time.perf_counter()
    solver = _solver(num_bands, resolution, epsilon, rod)
    k_points = mp.interpolate(k_intervals, [mp.Vector3(0, 0, 0), mp.Vector3(0.5, 0, 0)])
    solver.k_points = k_points
    solver.tolerance = 1.0e-6
    solver.run_te()
    frequencies = np.asarray(solver.all_freqs)

    # Independent analytic control: the empty lattice at k=(0.5,0,0) has exact
    # free-photon frequency |k| = 0.5 in units of c/a.
    control = _solver(num_bands, resolution, 1.0, rod)
    control.k_points = [mp.Vector3(0.5, 0, 0)]
    control.tolerance = 1.0e-6
    control.run_te()
    control_frequencies = np.asarray(control.all_freqs)[0]
    positive = control_frequencies[control_frequencies > 1e-9]
    control_lowest = float(positive.min()) if positive.size else None
    control_relative_error = (abs(control_lowest - 0.5) / 0.5
                              if control_lowest is not None else None)

    with h5py.File(hdf5_name, "w") as handle:
        handle.create_dataset("frequencies", data=frequencies)
        handle.create_dataset("k_points", data=np.asarray([[k.x, k.y, k.z] for k in k_points]))
        handle.create_dataset("empty_lattice_frequencies", data=control_frequencies)
        handle.attrs["resolution"] = resolution
        handle.attrs["epsilon"] = epsilon
    hdf5_sha = _sha(hdf5_name)

    nonzero = frequencies[frequencies > 1e-9]
    checks = {
        "bands_match_requested": int(frequencies.shape[1]) == num_bands,
        "k_points_present": int(frequencies.shape[0]) > 0,
        "frequencies_finite_nonnegative": bool(np.isfinite(frequencies).all()
                                               and (frequencies >= -1e-9).all()),
        "nonzero_band_exists": nonzero.size > 0,
        "empty_lattice_analytic_matches": (control_relative_error is not None
                                            and control_relative_error
                                            <= EMPTY_LATTICE_RELATIVE_TOLERANCE),
    }
    report = {
        "tool": "mpb", "mpb_version": list(mpb.__version__),
        "bands": int(frequencies.shape[1]), "k_points": int(frequencies.shape[0]),
        "gamma_frequencies": [float(value) for value in frequencies[0]],
        "x_frequencies": [float(value) for value in frequencies[-1]],
        "empty_lattice_control": {
            "k_point": [0.5, 0.0, 0.0],
            "frequencies": [float(value) for value in control_frequencies],
            "lowest_positive_frequency": control_lowest,
            "analytic_frequency": 0.5,
            "relative_error": control_relative_error,
            "tolerance": EMPTY_LATTICE_RELATIVE_TOLERANCE,
        },
        "raw_frequencies": [[float(value) for value in row] for row in frequencies.tolist()],
        "raw_k_points": [[float(k.x), float(k.y), float(k.z)] for k in k_points],
        "checks": checks,
        "passed": all(checks.values()),
        "outputs": {hdf5_name: hdf5_sha},
        "elapsed_seconds": time.perf_counter() - started,
        "purpose": "operational_check_not_research_result",
    }
    _emit(report)


if __name__ == "__main__":
    main()
