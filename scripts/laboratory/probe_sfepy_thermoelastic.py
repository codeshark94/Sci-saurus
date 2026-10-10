#!/usr/bin/env python3
"""Operational check: SfePy thermoelasticity with a declared stress-free reference.

Reads one JSON object on stdin::

    {"mesh": "inputs/part_volume.vtk",
     "lam": 10.0, "mu": 5.0, "alpha": 1.25e-5,
     "T_hot": 100.0, "T_cold": 20.0, "T_ref": 20.0,
     "outputs": {"vtk": "thermoelastic.vtk", "hdf5": "thermoelastic.h5"}}

Physics and sign conventions
-----------------------------
* ``T`` is an absolute temperature in kelvin and ``T_ref`` is the declared
  stress-free reference temperature.  The mechanical problem is driven by the
  temperature *change* ``dT = T - T_ref``; the weak form uses the documented
  SfePy ``dw_biot`` term with ``B = (3*lambda + 2*mu) * alpha * I``.
* The perforated specimen is clamped on the left face and traction-free
  elsewhere, so it has no closed-form tip solution.  It is checked with two
  matched controls (``alpha = 0`` and ``dT = 0``) instead of a false slender-bar
  claim.
* The analytical limit is a *separate* straight, laterally constrained bar
  under uniform ``dT``.  There the exact one-way solution is
  ``u_x = alpha * dT * (x - x_min)``; a tight tolerance is justified because
  linear tetrahedra reproduce the linear field exactly.
* The residual is reported as a dimensional absolute norm and normalised by an
  independent physical load scale ``|3*lambda + 2*mu| * |alpha| * |dT_span| *
  volume**(2/3)`` rather than the old self-normalisation ``norm/(norm+1)``.

Emits exactly one JSON object on stdout; every solver, library and native
diagnostic goes to stderr.
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
from sfepy.base.conf import ProblemConf  # noqa: E402
from sfepy.discrete import Problem  # noqa: E402
from sfepy.discrete.fem import Mesh  # noqa: E402
from sfepy.mechanics.matcoefs import stiffness_from_lame  # noqa: E402
from sfepy.mesh.mesh_generators import gen_block_mesh  # noqa: E402

BAR_ANALYTIC_RELATIVE_TOLERANCE = 1.0e-6
CONTROL_RELATIVE_TOLERANCE = 1.0e-6
RAW_SAMPLE = 32


def _sha(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def _emit(report):
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    os.write(_ORIGINAL_STDOUT, payload)
    os.close(_ORIGINAL_STDOUT)


def _sample_indices(count, limit=RAW_SAMPLE):
    if count <= limit:
        return np.arange(count)
    return np.unique(np.linspace(0, count - 1, limit).astype(int))


def _solve(mesh, lam, mu, alpha_value, dT_left, dT_right, *, clamp_left=True,
           lateral_rollers=False):
    """Solve one coupled heat-conduction/elasticity problem and return fields."""
    coors = np.asarray(mesh.coors)
    xmin, xmax = float(coors[:, 0].min()), float(coors[:, 0].max())
    eye_sym = np.array([[1.0], [1.0], [1.0], [0.0], [0.0], [0.0]])
    biot = (3.0 * lam + 2.0 * mu) * alpha_value * eye_sym
    regions = {
        "Omega": "all",
        "Left": (f"vertices in (x < {xmin + 1e-6})", "facet"),
        "Right": (f"vertices in (x > {xmax - 1e-6})", "facet"),
    }
    ebcs = {
        "T_left": ("Left", {"T.0": dT_left}),
        "T_right": ("Right", {"T.0": dT_right}),
    }
    if clamp_left:
        ebcs["u_left"] = ("Left", {"u.all": 0.0})
    if lateral_rollers:
        ymin, ymax = float(coors[:, 1].min()), float(coors[:, 1].max())
        zmin, zmax = float(coors[:, 2].min()), float(coors[:, 2].max())
        regions["Ymin"] = (f"vertices in (y < {ymin + 1e-6})", "facet")
        regions["Ymax"] = (f"vertices in (y > {ymax - 1e-6})", "facet")
        regions["Zmin"] = (f"vertices in (z < {zmin + 1e-6})", "facet")
        regions["Zmax"] = (f"vertices in (z > {zmax - 1e-6})", "facet")
        ebcs["u_y_min"] = ("Ymin", {"u.1": 0.0})
        ebcs["u_y_max"] = ("Ymax", {"u.1": 0.0})
        ebcs["u_z_min"] = ("Zmin", {"u.2": 0.0})
        ebcs["u_z_max"] = ("Zmax", {"u.2": 0.0})
    conf = {
        "filename_mesh": mesh,
        "integrals": {"i": ("v", 2), "2": ("v", 2)},
        "regions": regions,
        "fields": {"displacement": ("real", 3, "Omega", 1),
                   "temperature": ("real", 1, "Omega", 1)},
        "variables": {
            "u": ("unknown field", "displacement", 0),
            "v": ("test field", "displacement", "u"),
            "T": ("unknown field", "temperature", 1),
            "s": ("test field", "temperature", "T"),
        },
        "materials": {
            "cond": ({"K": 1.0},),
            "solid": ({"D": stiffness_from_lame(3, lam=lam, mu=mu),
                       "alpha": biot},),
        },
        "ebcs": ebcs,
        "equations": {
            "thermal": "dw_laplace.i.Omega(cond.K, s, T) = 0",
            "elastic": "dw_lin_elastic.2.Omega(solid.D, v, u) - dw_biot.2.Omega(solid.alpha, v, T) = 0",
        },
        "solvers": {"ls": ("ls.scipy_direct", {}),
                    "nls": ("nls.newton", {"i_max": 1, "eps_a": 1e-10})},
    }
    problem = Problem.from_conf(ProblemConf.from_dict(conf, sys.modules[__name__]))
    state = problem.solve()
    dT = np.asarray(problem.get_variables()["T"]())
    displacement = np.asarray(problem.get_variables()["u"]()).reshape(-1, 3)
    residual = np.asarray(problem.equations.eval_residuals(state.get_state()))
    return {"problem": problem, "state": state, "coors": coors, "dT": dT,
            "displacement": displacement, "residual": residual,
            "residual_abs": float(np.linalg.norm(residual)),
            "node_count": int(mesh.n_nod), "element_count": int(mesh.n_el)}


def _tip_ux(fields):
    coors = fields["coors"]
    xmax = float(coors[:, 0].max())
    right = coors[:, 0] > xmax - 1e-6
    tip_face = float(fields["displacement"][right, 0].mean())
    global_max = float(fields["displacement"][:, 0].max())
    return tip_face, global_max, int(right.sum())


def _sample(fields, temperature):
    indices = _sample_indices(fields["node_count"])
    return {
        "nodes": [int(index) for index in indices],
        "x": [float(fields["coors"][index, 0]) for index in indices],
        "temperature": [float(temperature[index]) for index in indices],
        "dT": [float(fields["dT"][index]) for index in indices],
        "ux": [float(fields["displacement"][index, 0]) for index in indices],
    }


def main():
    request = json.load(sys.stdin)
    mesh_path = request.get("mesh") or "inputs/part_volume.vtk"
    lam = float(request.get("lam", 10.0))
    mu = float(request.get("mu", 5.0))
    alpha = float(request.get("alpha", 1.25e-5))
    T_hot = float(request.get("T_hot", 100.0))
    T_cold = float(request.get("T_cold", 20.0))
    T_ref = float(request.get("T_ref", T_cold))
    outputs = request.get("outputs") or {}
    vtk_name = outputs.get("vtk", "thermoelastic.vtk")
    hdf5_name = outputs.get("hdf5", "thermoelastic.h5")
    started = time.perf_counter()

    mesh = Mesh.from_file(mesh_path)
    dT_hot, dT_cold = T_hot - T_ref, T_cold - T_ref

    main_fields = _solve(mesh, lam, mu, alpha, dT_hot, dT_cold)
    temperature = main_fields["dT"] + T_ref
    tip_face, global_max, right_nodes = _tip_ux(main_fields)

    alpha_zero = _solve(mesh, lam, mu, 0.0, dT_hot, dT_cold)
    tip_alpha0, _, _ = _tip_ux(alpha_zero)

    delta_t_zero = _solve(mesh, lam, mu, alpha, 0.0, 0.0)
    tip_deltaT0, _, _ = _tip_ux(delta_t_zero)

    # Independent analytical limit on a separate straight, laterally constrained
    # bar under uniform dT.  Roller faces enforce eps_yy = eps_zz = 0, so the
    # exact uniaxial-strain solution is eps_xx = alpha*dT*(3*lambda+2*mu)/(lambda+2*mu)
    # and u_x = eps_xx * (x - x_min); linear tetrahedra reproduce it exactly.
    bar_length = 10.0
    bar = gen_block_mesh([bar_length, 2.0, 2.0], [20, 4, 4],
                         [bar_length / 2, 1.0, 1.0], name="bar")
    bar_fields = _solve(bar, lam, mu, alpha, dT_hot, dT_hot, lateral_rollers=True)
    bar_tip, _, _ = _tip_ux(bar_fields)
    bar_strain_factor = (3.0 * lam + 2.0 * mu) / (lam + 2.0 * mu)
    bar_analytic = alpha * dT_hot * bar_strain_factor * bar_length
    bar_relative_error = abs(bar_tip - bar_analytic) / abs(bar_analytic)

    # Independent physical load scale for the residual; never norm/(norm+1).
    volume = float(getattr(mesh, "volume", 0.0)) if getattr(mesh, "volume", None) else None
    if not volume:
        # Fall back to a bounding-box volume when the mesh has no volume attribute.
        coors = main_fields["coors"]
        spans = np.ptp(coors, axis=0)
        volume = float(np.prod(np.maximum(spans, 1e-12)))
    stress_scale = abs(3.0 * lam + 2.0 * mu) * abs(alpha) * abs(T_hot - T_cold)
    load_scale = stress_scale * volume ** (2.0 / 3.0)
    relative_residual = (main_fields["residual_abs"] / load_scale
                         if load_scale > 0.0 else None)

    tip_scale = abs(alpha * (T_hot - T_cold) * (float(main_fields["coors"][:, 0].max())
                                                 - float(main_fields["coors"][:, 0].min())))
    control_tolerance = CONTROL_RELATIVE_TOLERANCE * tip_scale
    checks = {
        "bar_analytic_matches": bar_relative_error <= BAR_ANALYTIC_RELATIVE_TOLERANCE,
        "alpha_zero_control_bounded": abs(tip_alpha0) <= control_tolerance,
        "delta_t_zero_control_bounded": abs(tip_deltaT0) <= control_tolerance,
        "thermal_increment_nonzero": abs(tip_face - tip_alpha0) > 0.0,
        "residual_bounded": relative_residual is not None and relative_residual <= 1e-8,
    }

    problem = main_fields["problem"]
    state = main_fields["state"]
    problem.save_state(vtk_name, state)
    with h5py.File(hdf5_name, "w") as handle:
        handle.create_dataset("temperature", data=temperature)
        handle.create_dataset("dT", data=main_fields["dT"])
        handle.create_dataset("displacement", data=main_fields["displacement"])
        handle.create_dataset("residual", data=main_fields["residual"])
        handle.create_dataset("bar_displacement", data=bar_fields["displacement"])
        handle.attrs["T_ref"] = T_ref
        handle.attrs["alpha"] = alpha
    vtk_sha = _sha(vtk_name)
    hdf5_sha = _sha(hdf5_name)

    report = {
        "tool": "sfepy",
        "coupling": "one_way_temperature_to_thermal_strain",
        "units": "SI (m, K, Pa); alpha in 1/K",
        "reference_temperature_K": T_ref,
        "temperature_min": float(temperature.min()),
        "temperature_max": float(temperature.max()),
        "nodes": int(mesh.n_nod), "volume_elements": int(mesh.n_el),
        "specimen": {
            "tip_face_ux": tip_face, "tip_face_node_count": right_nodes,
            "tip_global_max_ux": global_max,
            "tip_ux_alpha0_control": tip_alpha0,
            "tip_ux_deltaT0_control": tip_deltaT0,
            "thermal_tip_increment": tip_face - tip_alpha0,
            "tip_scale": tip_scale, "control_tolerance": control_tolerance,
        },
        "bar_analytic": {
            "geometry": "straight laterally constrained (roller) bar, uniform dT, uniaxial strain",
            "length": bar_length, "dT": dT_hot, "strain_factor": bar_strain_factor,
            "tip_ux": bar_tip, "analytic_tip_ux": bar_analytic,
            "relative_error": bar_relative_error,
            "tolerance": BAR_ANALYTIC_RELATIVE_TOLERANCE,
        },
        "residual_abs": main_fields["residual_abs"],
        "load_scale": load_scale,
        "relative_residual": relative_residual,
        "raw_field_sample": _sample(main_fields, temperature),
        "checks": checks,
        "passed": all(checks.values()),
        "outputs": {vtk_name: vtk_sha, hdf5_name: hdf5_sha},
        "elapsed_seconds": time.perf_counter() - started,
        "purpose": "operational_check_not_research_result",
    }
    _emit(report)


if __name__ == "__main__":
    main()
