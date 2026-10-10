#!/usr/bin/env python3
"""Operational check: parametric FreeCAD solid exported to STEP and FCStd.

Reads one JSON object on stdin::

    {"params": {"Lx": 20.0, "Ly": 10.0, "t": 2.0, "r": 1.2, "pitch": 4.0,
                "nx": 3, "ny": 2},
     "outputs": {"step": "part.step", "fcstd": "part.FCStd"}}

Writes the declared files into the current working directory and emits exactly
one JSON object on stdout describing the CAD kernel result, bounded numerical
checks and a closed-form volume reference.  All CAD kernel and library
diagnostics, including C-level and atexit output, go to stderr.  This is an
implementation check of the CAD toolchain, not a scientific result.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys

# Reserve the real stdout for the single JSON report; diagnostics from the CAD
# kernel and its C++ dependencies go to stderr, including at interpreter exit.
_ORIGINAL_STDOUT = os.dup(1)
os.dup2(2, 1)

import FreeCAD  # noqa: E402
import Import  # noqa: E402
import Part  # noqa: E402

# Bounded acceptance criteria for this operational benchmark.
VOLUME_TOLERANCE = 1.0e-6
MIN_SOLID_VOLUME = 1.0e-9


def _sha(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def _emit(report):
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    os.write(_ORIGINAL_STDOUT, payload)
    os.close(_ORIGINAL_STDOUT)


def main():
    request = json.load(sys.stdin)
    params = request.get("params") or {}
    outputs = request.get("outputs") or {}
    step_name = outputs.get("step", "part.step")
    fcstd_name = outputs.get("fcstd", "part.FCStd")
    Lx = float(params.get("Lx", 20.0))
    Ly = float(params.get("Ly", 10.0))
    t = float(params.get("t", 2.0))
    r = float(params.get("r", 1.2))
    pitch = float(params.get("pitch", 4.0))
    nx = int(params.get("nx", 3))
    ny = int(params.get("ny", 2))
    doc = FreeCAD.newDocument("metamaterial")
    shape = Part.makeBox(Lx, Ly, t, FreeCAD.Vector(-Lx / 2, -Ly / 2, 0.0))
    for i in range(nx):
        for j in range(ny):
            cx = -Lx / 2 + (Lx - pitch * (nx - 1)) / 2 + pitch * i
            cy = -Ly / 2 + (Ly - pitch * (ny - 1)) / 2 + pitch * j
            shape = shape.cut(Part.makeCylinder(r, t * 4, FreeCAD.Vector(cx, cy, -t)))
    obj = doc.addObject("Part::Feature", "Plate")
    obj.Shape = shape
    doc.recompute()
    Import.export([obj], step_name)
    doc.saveAs(fcstd_name)
    analytic = Lx * Ly * t - nx * ny * math.pi * r * r * t
    box = shape.BoundBox
    relative_volume_error = abs(shape.Volume - analytic) / analytic
    checks = {
        "positive_volume": shape.Volume > MIN_SOLID_VOLUME,
        "single_solid": len(shape.Solids) == 1,
        "outline_matches": (abs(box.XMin + Lx / 2) < 1e-6 and abs(box.XMax - Lx / 2) < 1e-6
                            and abs(box.YMin + Ly / 2) < 1e-6 and abs(box.YMax - Ly / 2) < 1e-6
                            and abs(box.ZMin) < 1e-6 and abs(box.ZMax - t) < 1e-6),
        "volume_within_tolerance": relative_volume_error <= VOLUME_TOLERANCE,
    }
    report = {
        "tool": "freecad", "freecad_version": FreeCAD.Version()[0:3],
        "volume": shape.Volume, "analytic_volume": analytic,
        "relative_volume_error": relative_volume_error,
        "solids": len(shape.Solids), "holes": nx * ny,
        "expected_holes": nx * ny,
        "bounding_box": [box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax],
        "checks": checks,
        "criteria": {"volume_tolerance": VOLUME_TOLERANCE, "min_solid_volume": MIN_SOLID_VOLUME},
        "passed": all(checks.values()),
        "outputs": {step_name: _sha(step_name), fcstd_name: _sha(fcstd_name)},
        "purpose": "operational_check_not_research_result",
    }
    _emit(report)


if __name__ == "__main__":
    main()
