#!/usr/bin/env python3
"""Operational check: tiny Meep time-domain electromagnetic evolution.

Reads one JSON object on stdin::

    {"resolution": 16, "cell": 4.0, "until": 12.0, "frequency": 0.6,
     "outputs": {"hdf5": "meep_fields.h5"}}

Runs a real FDTD evolution with a dielectric block and a continuous source,
records the Ez array and box energy history, and writes HDF5.  Bounded checks
require an actual evolution (timesteps advanced, non-zero finite fields and a
changed box energy), not merely a successful import.  Emits exactly one JSON
object on stdout; all native solver and atexit diagnostics go to stderr.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

# Reserve the real stdout for the single JSON report; Meep/MPB and their C++
# dependencies write progress to file descriptor 1, including at exit.
_ORIGINAL_STDOUT = os.dup(1)
os.dup2(2, 1)

import h5py  # noqa: E402
import numpy as np  # noqa: E402
import meep as mp  # noqa: E402

RAW_SAMPLE = 16


def _sha(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def _emit(report):
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    os.write(_ORIGINAL_STDOUT, payload)
    os.close(_ORIGINAL_STDOUT)


def main():
    request = json.load(sys.stdin)
    resolution = int(request.get("resolution", 16))
    cell_size = float(request.get("cell", 4.0))
    until = float(request.get("until", 12.0))
    frequency = float(request.get("frequency", 0.6))
    outputs = request.get("outputs") or {}
    hdf5_name = outputs.get("hdf5", "meep_fields.h5")
    started = time.perf_counter()
    cell = mp.Vector3(cell_size, cell_size, 0)
    geometry = [mp.Block(mp.Vector3(1.2, 1.2, mp.inf), center=mp.Vector3(),
                         material=mp.Medium(epsilon=9.0))]
    sources = [mp.Source(mp.ContinuousSource(frequency=frequency), component=mp.Ez,
                         center=mp.Vector3(-1.2, -1.2))]
    simulation = mp.Simulation(cell_size=cell, resolution=resolution, geometry=geometry,
                                sources=sources, boundary_layers=[mp.PML(0.5)])
    energies = []

    def record(sim):
        energies.append(float(sim.field_energy_in_box(size=cell, center=mp.Vector3())))

    simulation.run(mp.at_every(2.0, record), until=until)
    field = np.asarray(simulation.get_array(component=mp.Ez, center=mp.Vector3(), size=cell))
    with h5py.File(hdf5_name, "w") as handle:
        handle.create_dataset("Ez", data=field)
        handle.create_dataset("energy_history", data=np.asarray(energies))
        handle.attrs["resolution"] = resolution
        handle.attrs["cell_size"] = cell_size
    hdf5_sha = _sha(hdf5_name)
    energy_initial = energies[0] if energies else None
    energy_final = energies[-1] if energies else None
    field_max_abs = float(np.abs(field).max())
    sample_rows = min(RAW_SAMPLE, field.shape[0])
    sample_cols = min(RAW_SAMPLE, field.shape[1])
    checks = {
        "timesteps_positive": int(simulation.fields.t) > 0,
        "field_nonzero": field_max_abs > 0.0,
        "field_finite": bool(np.isfinite(field).all()),
        "energy_evolved": (energy_initial is not None and energy_final is not None
                           and energy_initial != energy_final),
    }
    report = {
        "tool": "meep", "meep_version": mp.__version__,
        "timesteps": int(simulation.fields.t), "field_shape": list(field.shape),
        "field_max_abs": field_max_abs,
        "energy_initial": energy_initial,
        "energy_final": energy_final,
        "raw_field_sample": [list(row) for row in field[:sample_rows, :sample_cols].tolist()],
        "raw_energy_sample": energies[:RAW_SAMPLE],
        "checks": checks,
        "passed": all(checks.values()),
        "outputs": {hdf5_name: hdf5_sha},
        "elapsed_seconds": time.perf_counter() - started,
        "purpose": "operational_check_not_research_result",
    }
    _emit(report)


if __name__ == "__main__":
    main()
