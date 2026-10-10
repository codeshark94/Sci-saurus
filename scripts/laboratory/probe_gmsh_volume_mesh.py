#!/usr/bin/env python3
"""Operational check: Gmsh imports a retained STEP artifact and builds a 3D volume mesh.

Reads one JSON object on stdin::

    {"step": "inputs/part.step", "mesh_size_m": 0.0015,
     "outputs": {"msh": "part.msh", "vtk": "part_volume.vtk"}}

The VTK export keeps only tetrahedral volume cells so a continuum solver can
load a clean volume mesh.  Signed tetrahedron volumes are recomputed
independently with numpy; a non-positive minimum invalidates the mesh.  Emits
exactly one JSON object on stdout; all Gmsh/OpenMP diagnostics go to stderr.
This is a meshing toolchain check, not a convergence study.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

# Reserve the real stdout for the single JSON report.
_ORIGINAL_STDOUT = os.dup(1)
os.dup2(2, 1)

import gmsh  # noqa: E402
import meshio  # noqa: E402
import numpy as np  # noqa: E402

MIN_SIGNED_VOLUME = 0.0
RAW_SAMPLE = 16


def _sha(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def _emit(report):
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    os.write(_ORIGINAL_STDOUT, payload)
    os.close(_ORIGINAL_STDOUT)


def _tetra_volumes(points, tets):
    corners = points[tets]
    a, b, c, d = (corners[:, index, :] for index in range(4))
    return np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) / 6.0


def main():
    request = json.load(sys.stdin)
    step_path = request.get("step") or "inputs/part.step"
    mesh_size = float(request["mesh_size_m"])
    if not np.isfinite(mesh_size) or mesh_size <= 0:
        raise ValueError("mesh_size_m must be positive and finite")
    outputs = request.get("outputs") or {}
    msh_name = outputs.get("msh", "part.msh")
    vtk_name = outputs.get("vtk", "part_volume.vtk")
    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.option.setString("Geometry.OCCTargetUnit", "M")
    gmsh.open(step_path)
    gmsh.model.occ.synchronize()
    gmsh.model.mesh.setSize(gmsh.model.getEntities(0), mesh_size)
    gmsh.model.mesh.generate(3)
    node_tags = gmsh.model.mesh.getNodes()[0]
    _, element_tags, _ = gmsh.model.mesh.getElements(3)
    volume_elements = sum(len(tags) for tags in element_tags)
    gmsh.write(msh_name)
    gmsh.finalize()
    mesh = meshio.read(msh_name)
    tets = mesh.cells_dict.get("tetra")
    if tets is None:
        raise SystemExit("Gmsh produced no tetrahedral volume elements")
    meshio.write(vtk_name, meshio.Mesh(mesh.points, [("tetra", tets)]))
    signed_volumes = _tetra_volumes(np.asarray(mesh.points, dtype=float), np.asarray(tets, dtype=int))
    min_volume = float(signed_volumes.min()) if signed_volumes.size else 0.0
    checks = {
        "nodes_positive": int(len(node_tags)) > 0,
        "volume_elements_positive": int(volume_elements) > 0,
        "vtk_tetrahedra_match": int(len(tets)) == int(volume_elements),
        "all_signed_volumes_positive": bool(signed_volumes.size) and min_volume > MIN_SIGNED_VOLUME,
    }
    sample = [round(float(value), 12) for value in signed_volumes[:RAW_SAMPLE]]
    report = {
        "tool": "gmsh", "gmsh_version": gmsh.__version__,
        "nodes": int(len(node_tags)), "volume_elements": int(volume_elements),
        "vtk_tetrahedra": int(len(tets)), "vtk_points": int(len(mesh.points)),
        "min_signed_tetra_volume": min_volume,
        "mean_signed_tetra_volume": float(signed_volumes.mean()) if signed_volumes.size else 0.0,
        "raw_tetra_volume_sample": sample,
        "checks": checks,
        "criteria": {"min_signed_volume": MIN_SIGNED_VOLUME, "raw_sample_size": RAW_SAMPLE},
        "passed": all(checks.values()),
        "coordinate_unit": "m",
        "coordinate_bounds": [mesh.points.min(axis=0).tolist(), mesh.points.max(axis=0).tolist()],
        "outputs": {msh_name: _sha(msh_name), vtk_name: _sha(vtk_name)},
        "purpose": "operational_check_not_research_result",
    }
    _emit(report)


if __name__ == "__main__":
    main()
