"""Operator-provisioned metamaterial CAD and multiphysics laboratory.

The laboratory is a strict, opt-in description of a *broad* engineering scope,
the design and physics families that may be explored, the engineering
workflows an admitted topic may use, and the installed runtimes the operator
has provisioned on this host.  It deliberately does not select a research
topic: topic selection, hypotheses and scientific design stay agent-authored.
The laboratory only bounds which mechanisms, tools and evidence obligations
are legitimate for the project that opted in.

Trusted preparation executes the declared runtimes, records their exact
identity (executable content hash, package/conda lock inventory) and writes an
immutable attestation.  Later agent work may only *use* a declared runtime by
label; it can never supply a host path. Execution authorization requires a
matching installation attestation. Solver operation and scientific validity
require separate checks.

The module is intentionally independent of the Composer so it can be validated,
provisioned and tested without starting a mission or a model provider.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.program_sandbox import (
    RUNTIME_ENV_KEYS, SandboxResult, run_sandboxed, sandbox_environment, sandbox_status,
)

LABORATORY_SCHEMA_VERSION = "metamaterial-laboratory-1"
LABORATORY_ATTESTATION_SCHEMA_VERSION = "metamaterial-laboratory-attestation-1"
LABORATORY_CONTEXT_REVISION = "metamaterial-laboratory-context-1"
LABORATORY_ENGINEERING_REVISION = "metamaterial-engineering-assessment-1"
LABORATORY_FEASIBILITY_REVISION = "metamaterial-laboratory-feasibility-1"

RUNTIME_KINDS = frozenset({"conda", "venv", "pip", "app_bundle", "binary", "container"})
LOCK_KINDS = frozenset({"conda-meta", "pip-freeze", "requirements-txt", "app-bundle", "none"})
COUPLING_ROLES = frozenset({"independent", "one_way", "two_way", "homogenization",
                            "finite_structure"})
DESIGN_KINDS = frozenset({"cad_solid", "periodic_cell", "lattice", "porous", "laminate",
                          "architected_2d", "architected_3d", "granular"})
PHYSICS_DOMAINS = frozenset({"thermal", "structural", "acoustic", "electromagnetic",
                             "optical", "fluid", "multiphysics", "homogenization", "finite_structure"})
WORKSPACE_TOKEN = "$WORKSPACE"

# Environment keys a declared runtime may set for its own process.  They are
# consumed only from the operator-authored laboratory profile and only when a
# runtime is invoked through the sandbox; a model response can never supply
# them.  The set is exactly the sandbox module's operator-runtime whitelist, so
# a declared variable can never be silently dropped or widened.
LAB_RUNTIME_ENV_KEYS = frozenset(RUNTIME_ENV_KEYS)

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_MEDIA = re.compile(r"[a-z0-9][a-z0-9.+-]{0,63}\Z")

PROBE_PROGRAM = r'''
import hashlib, importlib, json, os, platform, sys

# Native libraries (for example a CAD kernel) can write to file descriptor 1
# during import.  Keep the original stdout for the single JSON report and send
# every diagnostic, including C-level and atexit output, to stderr.
_ORIGINAL_STDOUT = os.dup(1)
os.dup2(2, 1)


def main():
    report = {"executable": sys.executable, "version": sys.version, "prefix": sys.prefix,
              "base_prefix": sys.base_prefix, "architecture": platform.machine(),
              "platform": platform.system()}
    modules = {}
    for name in sys.argv[1:]:
        try:
            module = importlib.import_module(name)
            state = {"imported": True}
            path = getattr(module, "__file__", None)
            if isinstance(path, str) and os.path.isfile(path):
                real = os.path.realpath(path)
                with open(real, "rb") as stream:
                    state["file"] = real
                    state["sha256"] = hashlib.sha256(stream.read()).hexdigest()
            modules[name] = state
        except BaseException as error:  # noqa: BLE001 - report the import failure
            modules[name] = {"imported": False,
                             "error": type(error).__name__ + ": " + str(error)[:200]}
    report["modules"] = modules
    report["commands"] = {name: {"executable": os.path.isfile(path) and os.access(path, os.X_OK)}
                          for name, path in json.loads(os.environ.get("SCI_SOLVER_COMMANDS", "{}")).items()}
    os.write(_ORIGINAL_STDOUT, (json.dumps(report, sort_keys=True) + "\n").encode())
    os.close(_ORIGINAL_STDOUT)


main()
'''


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _object(value, fields, optional, name):
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object; observed {type(value).__name__}")
    missing = sorted(set(fields) - set(value))
    unexpected = sorted(set(value) - set(fields) - set(optional))
    if missing or unexpected:
        raise ValidationError(
            f"{name}: missing fields {missing}; unexpected fields {unexpected}; "
            f"required fields {sorted(fields)}; optional fields {sorted(optional)}")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} requires an explicit nonempty string")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError(f"{name} must be a lowercase identifier")
    return value


def _string_list(value, name, *, nonempty=False):
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(set(value)) != len(value)):
        raise ValidationError(f"{name} must be a unique list of nonempty strings")
    return value


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValidationError(f"{name} must be a positive integer")
    return value


def _enum(value, allowed, name):
    if value not in allowed:
        raise ValidationError(f"{name} must be one of {sorted(allowed)}")
    return value


# ---------------------------------------------------------------------------
# Strict profile validation
# ---------------------------------------------------------------------------

def _validate_scope(value):
    _object(value, {"domain", "objective", "non_goals"}, {"notes"}, "laboratory.scope")
    _text(value["domain"], "laboratory.scope.domain")
    _text(value["objective"], "laboratory.scope.objective")
    _string_list(value["non_goals"], "laboratory.scope.non_goals")
    if "notes" in value and value["notes"] is not None:
        _text(value["notes"], "laboratory.scope.notes")
    return value


def _validate_design_family(value, index):
    name = f"laboratory.design_families[{index}]"
    _object(value, {"id", "kind", "description", "tool", "limitations"}, set(), name)
    _identifier(value["id"], name + ".id")
    _enum(value["kind"], DESIGN_KINDS, name + ".kind")
    _text(value["description"], name + ".description")
    _text(value["tool"], name + ".tool")
    _string_list(value["limitations"], name + ".limitations")
    return value


def _validate_physics_family(value, index):
    name = f"laboratory.physics_families[{index}]"
    _object(value, {"id", "domain", "description", "runtime", "capabilities",
                    "coupling_role", "coupling_note", "limitations"}, set(), name)
    _identifier(value["id"], name + ".id")
    _enum(value["domain"], PHYSICS_DOMAINS, name + ".domain")
    _text(value["description"], name + ".description")
    _identifier(value["runtime"], name + ".runtime")
    _string_list(value["capabilities"], name + ".capabilities", nonempty=True)
    _enum(value["coupling_role"], COUPLING_ROLES, name + ".coupling_role")
    _text(value["coupling_note"], name + ".coupling_note")
    _string_list(value["limitations"], name + ".limitations")
    return value


def _validate_workflow_stage(value, index, name):
    _object(value, {"id", "operation", "runtime", "inputs", "outputs"}, set(),
            f"{name}.stages[{index}]")
    _identifier(value["id"], f"{name}.stages[{index}].id")
    _identifier(value["operation"], f"{name}.stages[{index}].operation")
    _identifier(value["runtime"], f"{name}.stages[{index}].runtime")
    for key in ("inputs", "outputs"):
        media = _string_list(value[key], f"{name}.stages[{index}].{key}")
        for item in media:
            if not _MEDIA.fullmatch(item):
                raise ValidationError(f"{name}.stages[{index}].{key} contains an invalid media token")
    return value


def _validate_workflow(value, index):
    name = f"laboratory.workflows[{index}]"
    _object(value, {"id", "description", "stages", "coupling_plan", "limitations"},
            set(), name)
    _identifier(value["id"], name + ".id")
    _text(value["description"], name + ".description")
    if not isinstance(value["stages"], list) or not value["stages"]:
        raise ValidationError(f"{name}.stages must be a nonempty list")
    for position, stage in enumerate(value["stages"]):
        _validate_workflow_stage(stage, position, name)
    plan = value["coupling_plan"]
    if plan is not None:
        _object(plan, {"kind", "direction", "note"}, set(), name + ".coupling_plan")
        _enum(plan["kind"], COUPLING_ROLES, name + ".coupling_plan.kind")
        _text(plan["direction"], name + ".coupling_plan.direction")
        _text(plan["note"], name + ".coupling_plan.note")
    _string_list(value["limitations"], name + ".limitations")
    return value


def _validate_runtime_environment(value, name):
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    for key, item in value.items():
        if key not in LAB_RUNTIME_ENV_KEYS:
            raise ValidationError(
                f"{name} may only set declared runtime keys {sorted(LAB_RUNTIME_ENV_KEYS)}")
        _text(item, f"{name}.{key}")
    return value


def _validate_lock_provenance(value, name):
    _object(value, {"kind", "path", "note"}, set(), name)
    _enum(value["kind"], LOCK_KINDS, name + ".kind")
    if value["kind"] == "none":
        if value["path"] is not None:
            raise ValidationError(f"{name}.path must be null for kind none")
    else:
        _text(value["path"], name + ".path")
        path = Path(value["path"])
        if not path.is_absolute():
            raise ValidationError(f"{name}.path must be an absolute path")
        if not path.exists():
            raise ValidationError(f"{name}.path does not exist")
    _text(value["note"], name + ".note")
    return value


def _validate_runtime_resource_limits(value, name):
    _object(value, set(), {"address_space_bytes", "cpu_seconds", "file_size_bytes"}, name)
    for key, item in value.items():
        _positive_int(item, f"{name}.{key}")
    return value


def _validate_runtime(value, index):
    name = f"laboratory.runtimes[{index}]"
    _object(value, {"label", "kind", "executable", "description", "capabilities",
                    "probe_modules", "environment", "lock_provenance", "read_only_roots",
                    "limitations"}, {"resource_limits", "container", "commands"}, name)
    _identifier(value["label"], name + ".label")
    _enum(value["kind"], RUNTIME_KINDS, name + ".kind")
    if value["kind"] == "container":
        from scisaurus.runtime.container_runtime import validate_container
        validate_container(value.get("container"))
    elif "container" in value:
        raise ValidationError(f"{name}.container requires kind container")
    _text(value["executable"], name + ".executable")
    executable = Path(value["executable"])
    if not executable.is_absolute():
        raise ValidationError(f"{name}.executable must be an absolute declared host path")
    if "\\" in value["executable"]:
        raise ValidationError(f"{name}.executable must be a POSIX path")
    _text(value["description"], name + ".description")
    _string_list(value["capabilities"], name + ".capabilities", nonempty=True)
    _string_list(value["probe_modules"], name + ".probe_modules")
    for module in value["probe_modules"]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", module):
            raise ValidationError(f"{name}.probe_modules contains an invalid module name")
    _validate_runtime_environment(value["environment"], name + ".environment")
    _validate_lock_provenance(value["lock_provenance"], name + ".lock_provenance")
    roots = value["read_only_roots"]
    if not isinstance(roots, list) or not roots:
        raise ValidationError(f"{name}.read_only_roots must be a nonempty list")
    for root in roots:
        if not isinstance(root, str) or not Path(root).is_absolute():
            raise ValidationError(f"{name}.read_only_roots entries must be absolute paths")
    commands = value.get("commands", {})
    if not isinstance(commands, dict):
        raise ValidationError(f"{name}.commands must map tool names to absolute runtime paths")
    for label, path in commands.items():
        _identifier(label, name + ".commands label")
        if not isinstance(path, str) or not PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ValidationError(f"{name}.commands requires absolute runtime paths")
        if value["kind"] != "container" and not any(Path(path).resolve().is_relative_to(Path(root).resolve()) for root in roots):
            raise ValidationError(f"{name}.commands must belong to declared read-only installations")
    _string_list(value["limitations"], name + ".limitations")
    if "resource_limits" in value:
        _validate_runtime_resource_limits(value["resource_limits"], name + ".resource_limits")
    return value


def _validate_assessment(value):
    fields = {"contribution_review", "analytic_probes_first", "manufacturability",
              "mesh_solver_convergence", "conservation_residual_checks",
              "robustness_study", "equal_constraint_comparison",
              "required_coupling_disclosure"}
    _object(value, fields, set(), "laboratory.assessment")
    for key in fields - {"required_coupling_disclosure"}:
        if value[key] is not True:
            raise ValidationError(
                f"laboratory.assessment.{key} must be true; the laboratory does not "
                "admit an engineering workflow that skips this obligation")
    roles = value["required_coupling_disclosure"]
    _string_list(roles, "laboratory.assessment.required_coupling_disclosure", nonempty=True)
    if set(roles) != set(COUPLING_ROLES):
        raise ValidationError(
            "laboratory.assessment.required_coupling_disclosure must name every coupling "
            f"role exactly once: {sorted(COUPLING_ROLES)}")
    return value


def _validate_limits(value):
    fields = {"max_runtime_seconds", "max_wall_seconds", "max_input_bytes",
              "max_output_bytes", "max_runtime_files"}
    _object(value, fields, set(), "laboratory.limits")
    for key in fields:
        _positive_int(value[key], f"laboratory.limits.{key}")
    return value


def validate_laboratory(value):
    """Validate one opt-in laboratory profile and return it unchanged."""
    fields = {"schema_version", "id", "enabled", "scope", "design_families",
              "physics_families", "workflows", "runtimes", "assessment", "limits"}
    _object(value, fields, set(), "laboratory")
    if value["schema_version"] != LABORATORY_SCHEMA_VERSION:
        raise ValidationError(f"laboratory schema must be {LABORATORY_SCHEMA_VERSION}")
    _identifier(value["id"], "laboratory.id")
    if value["enabled"] is not True:
        raise ValidationError("laboratory.enabled must be true; omit the laboratory to run without it")
    _validate_scope(value["scope"])
    for key, validator in (("design_families", _validate_design_family),
                           ("physics_families", _validate_physics_family),
                           ("workflows", _validate_workflow),
                           ("runtimes", _validate_runtime)):
        rows = value[key]
        if not isinstance(rows, list) or not rows:
            raise ValidationError(f"laboratory.{key} must be a nonempty list")
        seen = set()
        for index, row in enumerate(rows):
            validator(row, index)
            identity = row["id"] if "id" in row else row["label"]
            if identity in seen:
                raise ValidationError(f"laboratory.{key} identities must be unique")
            seen.add(identity)
    _validate_assessment(value["assessment"])
    _validate_limits(value["limits"])
    runtime_labels = {row["label"] for row in value["runtimes"]}
    for family in value["physics_families"]:
        if family["runtime"] not in runtime_labels:
            raise ValidationError(
                f"physics family {family['id']} names an undeclared runtime "
                f"{family['runtime']!r}; declared runtimes are {sorted(runtime_labels)}")
    for workflow in value["workflows"]:
        for stage in workflow["stages"]:
            if stage["runtime"] not in runtime_labels:
                raise ValidationError(
                    f"workflow {workflow['id']} names an undeclared runtime "
                    f"{stage['runtime']!r}; declared runtimes are {sorted(runtime_labels)}")
    return value


def load_laboratory(path):
    """Read, parse and validate an operator-authored laboratory profile."""
    path = Path(path)
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError(f"laboratory configuration is unreadable: {path}") from exc
    return validate_laboratory(value)


def laboratory_identity(laboratory):
    """Return the immutable identity of a validated laboratory profile."""
    validate_laboratory(laboratory)
    return _sha(canonical_bytes(laboratory))


# ---------------------------------------------------------------------------
# Model-facing context and engineering assessment contract
# ---------------------------------------------------------------------------

def laboratory_context(laboratory, *, attestation=None):
    """Project the laboratory into a bounded, path-free model context.

    Host paths, lock paths and environment values never enter the context.
    The context states which families are permitted, not which topic is chosen.
    """
    validate_laboratory(laboratory)
    attested = {row["label"]: row for row in (attestation or {}).get("runtimes", [])
                if isinstance(row, dict) and isinstance(row.get("label"), str)}
    runtimes = []
    for runtime in laboratory["runtimes"]:
        record = attested.get(runtime["label"], {})
        runtimes.append({
            "label": runtime["label"], "kind": runtime["kind"],
            "description": runtime["description"],
            "capabilities": list(runtime["capabilities"]),
            "probe_modules": list(runtime["probe_modules"]),
            "limitations": list(runtime["limitations"]),
            "commands": sorted(runtime.get("commands", {})),
            "command_access": "Read the controller-injected JSON mapping SCI_SOLVER_COMMANDS and use subprocess argument lists; never install or guess host paths.",
            "execution_platform": (runtime.get("container") or {}).get("platform", platform.system()),
            "controller_attested": (record.get("verified") is True
                                    and record.get("probe", {}).get("mode") == ("container" if runtime["kind"] == "container" else "sandbox-exec")),
        })
    return {
        "schema_version": LABORATORY_CONTEXT_REVISION,
        "laboratory_id": laboratory["id"],
        "config_sha256": laboratory_identity(laboratory),
        "scope": deepcopy(laboratory["scope"]),
        "design_families": [
            {key: deepcopy(row[key]) for key in ("id", "kind", "description", "tool", "limitations")}
            for row in laboratory["design_families"]],
        "physics_families": [
            {key: deepcopy(row[key]) for key in (
                "id", "domain", "description", "runtime", "capabilities",
                "coupling_role", "coupling_note", "limitations")}
            for row in laboratory["physics_families"]],
        "workflows": [
            {key: deepcopy(row[key]) for key in ("id", "description", "stages", "coupling_plan", "limitations")}
            for row in laboratory["workflows"]],
        "runtimes": runtimes,
        "assessment_requirements": deepcopy(laboratory["assessment"]),
        "design_iteration": {
            "sequence": ["concept", "baseline_and_small_pilot", "diagnose", "revise_design",
                         "compare", "validate_final_design"],
            "first_run": "Use the smallest informative geometry and established-solver calculation; retain an editable design and raw fields.",
            "iteration": "Read current fields, metrics, errors and resource receipts before changing geometry, mesh or solver settings. Re-execute changed sources and compare against the same baseline under equal constraints.",
            "continuation": "Keep a promising concept across design revisions. Revise or abandon it when actual evidence invalidates its mechanism, utility or feasibility; do not regenerate a topic solely because a pilot lacks final validation.",
            "selection_boundary": "Software selection requires a sourced model, feasible runtime and validation plans. Final convergence, robustness, performance and novelty evidence are produced by execution, not prerequisites for selecting the first pilot.",
            "final_claim": "A completed design is not automatically a publishable result. Require retained simulation evidence, independent recalculation, convergence, robustness, fair comparisons and a contribution assessment before final claims.",
        },
        "topic_authority": (
            "The laboratory defines a broad permitted scope, design families, physics "
            "families and workflows. It does not select the research topic, hypothesis, "
            "candidate geometry or expected outcome; those remain agent-authored and "
            "must be justified against the captured literature."),
        "coupling_semantics": (
            "independent: one physics calculation does not consume another's field. "
            "one_way: a declared field is passed as a receipt-bound input to a later "
            "calculation, e.g. temperature into thermal strain. two_way: each physics "
            "consumes the other's converged field and the exchange is explicitly "
            "declared and validated. homogenization: an effective medium property is "
            "computed on a unit cell. finite_structure: the structure itself is "
            "discretized and solved at the specimen scale. Sequential receipt-bound "
            "runs implement the declared coupling; no automatic coupling engine is "
            "assumed."),
        "readiness_rule": (
            "Runtimes marked controller_attested have executed a provisioning probe "
            "and match a recorded inventory; presence or a bare import is never "
            "readiness. Any claim of verified readiness by a specialist is rejected."),
    }


def laboratory_runtime_readiness(laboratory, *, attestation=None):
    """Project runtime labels into the machine-checkable feasibility contract.

    The result is deliberately path-free and package-inventory scoped by
    runtime label: a consumer can require an exact runtime label and read its
    declared capabilities, but it can never infer a host path or merge those
    packages into another execution boundary.  ``sealed`` is true only for an
    attestation whose content address was produced for this exact laboratory
    configuration, so an unsealed profile cannot authorize native execution.
    """
    validate_laboratory(laboratory)
    attestation = attestation or {}
    if attestation:
        validate_attestation(attestation)
    config_sha256 = laboratory_identity(laboratory)
    sealed = (bool(attestation) and attestation.get("config_sha256") == config_sha256
              and attestation.get("isolation", {}).get("runner") == "run_sandboxed")
    attested = {row["label"]: row for row in attestation.get("runtimes", [])
                if isinstance(row, dict) and isinstance(row.get("label"), str)}
    runtimes = []
    for runtime in laboratory["runtimes"]:
        record = attested.get(runtime["label"], {}) if sealed else {}
        runtimes.append({
            "label": runtime["label"],
            "kind": runtime["kind"],
            "capabilities": list(runtime["capabilities"]),
            "probe_modules": list(runtime["probe_modules"]),
            "controller_attested": (record.get("verified") is True
                                    and record.get("probe", {}).get("mode") == ("container" if runtime["kind"] == "container" else "sandbox-exec")),
        })
    return {
        "schema_version": LABORATORY_FEASIBILITY_REVISION,
        "laboratory_id": laboratory["id"],
        "config_sha256": config_sha256,
        "attestation_sha256": attestation.get("attestation_sha256") if sealed else None,
        "sealed": sealed,
        "runtimes": runtimes,
        "workflows": [
            {"id": row["id"],
             "stages": [
                 {"id": stage["id"], "operation": stage["operation"],
                  "runtime": stage["runtime"], "inputs": list(stage["inputs"]),
                  "outputs": list(stage["outputs"])}
                 for stage in row["stages"]]}
            for row in laboratory["workflows"]],
        "readiness_rule": (
            "A runtime label is execution_authorized only when the sealed "
            "attestation marks that exact label controller_attested. Presence, "
            "an import, installed package metadata, or a model claim is never "
            "readiness; native artifacts still require independent validation."),
    }


_ENGINEERING_FIELDS = {
    "contribution_assessment", "analytic_plan", "manufacturability_plan",
    "convergence_plan", "conservation_plan", "robustness_plan",
    "equal_constraint_comparison_plan", "coupling_disclosure", "scale_scope",
    "readiness_claim", "limitations",
}

# A reference that can back an engineering obligation.  Free text can never
# satisfy completion: the reference must name a typed stage, receipt, source or
# captured evidence identity that the controller can resolve independently.
_TYPED_REF = re.compile(
    r"(stage|receipt|source|evidence|literature|artifact|software|software-artifact|"
    r"software-evidence):[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}\Z")


def _typed_ref(value, name):
    if not isinstance(value, str) or not _TYPED_REF.fullmatch(value):
        raise ValidationError(
            f"{name} must be a typed stage/receipt/source/evidence reference, not free text")
    return value


def laboratory_engineering_contract(laboratory):
    """Return the prospective engineering plan contract for laboratory work.

    These are *selection-stage* plans and acceptance criteria, not measured
    outcomes.  A selection that opts into the laboratory must say what it will
    measure, against which independently resolvable reference and with which
    bounded acceptance rule.  Measured convergence, residuals, robustness
    outcomes and comparisons belong to completed experiment receipts and are
    never fabricated to satisfy this contract.
    """
    validate_laboratory(laboratory)
    return {
        "contribution_assessment": {
            "closest_prior_work": [{"source_ref": "typed source/evidence reference",
                                    "difference": "planned difference this work tests"}],
            "planned_contribution": "bounded prospective statement tied to a planned test",
            "novelty_claim": "not_claimed | bounded_increment",
        },
        "analytic_plan": [
            {"name": "cheap closed-form or reduced-order probe",
             "purpose": "what hypothesis it discriminates before expensive optimization",
             "predicted_limit": "the relation or limit it predicts",
             "acceptance_tolerance": "declared numeric tolerance or explicit decision rule"}
        ],
        "manufacturability_plan": {"assessment": "planned process and tolerance reasoning",
                                   "limitations": ["..."]},
        "convergence_plan": {"mesh_or_discretization": "planned refinement variable",
                             "refinement_plan": "planned refinement sequence",
                             "acceptance_criterion": "declared bounded acceptance rule"},
        "conservation_plan": [{"quantity": "energy | mass | flux",
                               "method": "planned independent recalculation",
                               "acceptance_criterion": "declared bounded residual rule"}],
        "robustness_plan": {"perturbations": ["planned variation"],
                            "acceptance_criterion": "declared survival/failure rule",
                            "limitations": ["..."]},
        "equal_constraint_comparison_plan": {"baseline_ref": "typed reference design or prior result",
                                             "constraints": ["equal budget/geometry constraints"],
                                             "acceptance_criterion": "declared comparison rule"},
        "coupling_disclosure": {"kind": " | ".join(sorted(COUPLING_ROLES)),
                                "planned_exchange": "which receipt-bound fields will be exchanged",
                                "limitations": ["..."]},
        "scale_scope": {"homogenization_plan": "planned unit-cell property derivation, if any",
                        "finite_structure_plan": "planned specimen-scale validation, if any"},
        "readiness_claim": "not_verified",
        "limitations": ["..."],
    }


def _engineering_object(value, fields, name, optional=()):
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    missing = sorted(set(fields) - set(value))
    unexpected = sorted(set(value) - set(fields) - set(optional))
    if missing or unexpected:
        raise ValidationError(
            f"{name}: missing fields {missing}; unexpected fields {unexpected}")


def validate_laboratory_engineering(value, laboratory):
    """Validate the prospective laboratory engineering plan carried by a selection."""
    _engineering_object(value, _ENGINEERING_FIELDS, "/laboratory_engineering")
    contribution = value["contribution_assessment"]
    _engineering_object(contribution, {"closest_prior_work", "planned_contribution",
                                       "novelty_claim"}, "/laboratory_engineering.contribution_assessment")
    if (not isinstance(contribution["closest_prior_work"], list)
            or not contribution["closest_prior_work"]):
        raise ValidationError(
            "/laboratory_engineering.contribution_assessment.closest_prior_work must name "
            "the closest captured prior work")
    for index, row in enumerate(contribution["closest_prior_work"]):
        _engineering_object(row, {"source_ref", "difference"},
                            f"/laboratory_engineering.contribution_assessment.closest_prior_work[{index}]")
        _typed_ref(row["source_ref"], "closest prior work source_ref")
        _text(row["difference"], "closest prior work difference")
    _text(contribution["planned_contribution"], "planned_contribution")
    if contribution["novelty_claim"] not in {"not_claimed", "bounded_increment"}:
        raise ValidationError(
            "/laboratory_engineering.contribution_assessment.novelty_claim must be "
            "not_claimed or bounded_increment")
    if (not isinstance(value["analytic_plan"], list) or not value["analytic_plan"]):
        raise ValidationError(
            "/laboratory_engineering.analytic_plan must declare at least one cheap prospective probe")
    for index, row in enumerate(value["analytic_plan"]):
        _engineering_object(row, {"name", "purpose", "predicted_limit", "acceptance_tolerance"},
                            f"/laboratory_engineering.analytic_plan[{index}]")
        for key in ("name", "purpose", "predicted_limit", "acceptance_tolerance"):
            _text(row[key], f"analytic plan {key}")
    _engineering_object(value["manufacturability_plan"], {"assessment", "limitations"},
                        "/laboratory_engineering.manufacturability_plan")
    _text(value["manufacturability_plan"]["assessment"], "manufacturability_plan.assessment")
    _string_list(value["manufacturability_plan"]["limitations"], "manufacturability_plan.limitations")
    _engineering_object(value["convergence_plan"],
                        {"mesh_or_discretization", "refinement_plan", "acceptance_criterion"},
                        "/laboratory_engineering.convergence_plan")
    for key in ("mesh_or_discretization", "refinement_plan", "acceptance_criterion"):
        _text(value["convergence_plan"][key], f"convergence_plan.{key}")
    if (not isinstance(value["conservation_plan"], list) or not value["conservation_plan"]):
        raise ValidationError(
            "/laboratory_engineering.conservation_plan must declare at least one residual check plan")
    for index, row in enumerate(value["conservation_plan"]):
        _engineering_object(row, {"quantity", "method", "acceptance_criterion"},
                            f"/laboratory_engineering.conservation_plan[{index}]")
        for key in ("quantity", "method", "acceptance_criterion"):
            _text(row[key], f"conservation plan {key}")
    _engineering_object(value["robustness_plan"], {"perturbations", "acceptance_criterion", "limitations"},
                        "/laboratory_engineering.robustness_plan")
    _string_list(value["robustness_plan"]["perturbations"], "robustness_plan.perturbations", nonempty=True)
    _text(value["robustness_plan"]["acceptance_criterion"], "robustness_plan.acceptance_criterion")
    _string_list(value["robustness_plan"]["limitations"], "robustness_plan.limitations")
    _engineering_object(value["equal_constraint_comparison_plan"],
                        {"baseline_ref", "constraints", "acceptance_criterion"},
                        "/laboratory_engineering.equal_constraint_comparison_plan")
    _typed_ref(value["equal_constraint_comparison_plan"]["baseline_ref"],
               "equal_constraint_comparison_plan.baseline_ref")
    _string_list(value["equal_constraint_comparison_plan"]["constraints"],
                 "equal_constraint_comparison_plan.constraints", nonempty=True)
    _text(value["equal_constraint_comparison_plan"]["acceptance_criterion"],
          "equal_constraint_comparison_plan.acceptance_criterion")
    _engineering_object(value["coupling_disclosure"], {"kind", "planned_exchange", "limitations"},
                        "/laboratory_engineering.coupling_disclosure")
    _enum(value["coupling_disclosure"]["kind"], COUPLING_ROLES,
          "/laboratory_engineering.coupling_disclosure.kind")
    declared = {row["coupling_role"] for row in laboratory["physics_families"]}
    if value["coupling_disclosure"]["kind"] not in declared:
        raise ValidationError(
            "/laboratory_engineering.coupling_disclosure.kind is not among the laboratory's "
            f"declared physics coupling roles {sorted(declared)}")
    _text(value["coupling_disclosure"]["planned_exchange"], "coupling_disclosure.planned_exchange")
    _string_list(value["coupling_disclosure"]["limitations"], "coupling_disclosure.limitations")
    _engineering_object(value["scale_scope"], {"homogenization_plan", "finite_structure_plan"},
                        "/laboratory_engineering.scale_scope")
    for key in ("homogenization_plan", "finite_structure_plan"):
        _text(value["scale_scope"][key], f"scale_scope.{key}")
    if value["readiness_claim"] != "not_verified":
        raise ValidationError(
            "/laboratory_engineering.readiness_claim must be not_verified; verified readiness "
            "is a controller provisioning fact, never a specialist claim")
    _string_list(value["limitations"], "limitations")
    return value


# ---------------------------------------------------------------------------
# Trusted preparation: probes, inventory and immutable attestation
# ---------------------------------------------------------------------------

def direct_runner(command, *, workspace, input_bytes=b"", timeout_seconds=300.0,
                  max_bytes=5_000_000, env=None, read_only_paths=(), allow_network=False):
    """Unsandboxed preparation runner for an operator-provisioned runtime.

    Used only by trusted provisioning when a native deny-by-default sandbox is
    not applicable.  The resulting attestation records ``unsandboxed`` so no
    downstream consumer can mistake it for an isolated execution boundary.
    """
    started = time.monotonic()
    process_env = sandbox_environment(workspace, env=env)
    try:
        completed = subprocess.run(command, cwd=str(workspace), env=process_env,
                                   input=input_bytes, capture_output=True,
                                   timeout=max(1.0, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        return SandboxResult(None, exc.stdout or b"", exc.stderr or b"", True, False, "unsandboxed")
    except OSError as exc:
        return SandboxResult(None, b"", str(exc).encode(), False, False, "unsandboxed")
    truncated = len(completed.stdout) + len(completed.stderr) > max_bytes
    return SandboxResult(completed.returncode, completed.stdout[:max_bytes],
                         completed.stderr[:max_bytes], False, truncated, "unsandboxed")


def resolve_runtime_environment(runtime, workspace):
    """Resolve a declared runtime environment, substituting the run workspace."""
    resolved = {}
    for key, value in runtime["environment"].items():
        resolved[key] = str(workspace) if value == WORKSPACE_TOKEN else value
    if runtime.get("commands"):
        resolved["SCI_SOLVER_COMMANDS"] = json.dumps(runtime["commands"], sort_keys=True)
    return resolved


def runtime_read_roots(runtime):
    roots = list(runtime["read_only_roots"])
    configured = Path(runtime["executable"])
    if configured.parent.name == "bin":
        venv_config = configured.parent.parent / "pyvenv.cfg"
        if venv_config.is_file():
            values = {key.strip(): value.strip() for line in venv_config.read_text().splitlines()
                      if "=" in line for key, value in [line.split("=", 1)]}
            home = Path(values.get("home", ""))
            if not home.is_absolute() or not home.is_dir():
                raise ValidationError("virtual environment has no valid base interpreter home")
            roots.append(str(home.parent.resolve()))
    executable = Path(runtime["executable"]).resolve()
    if executable.parent.name == "bin":
        prefix = executable.parent.parent
        if prefix.is_dir():
            roots.append(str(prefix))
    return tuple(dict.fromkeys(str(Path(root).resolve()) for root in roots))


def _conda_inventory(lock_path):
    """Return a deterministic package lock from a conda-meta directory."""
    packages = []
    for path in sorted(Path(lock_path).glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or "name" not in record:
            continue
        packages.append({
            "name": record.get("name"), "version": record.get("version"),
            "build": record.get("build"),
            "channel": record.get("channel"),
            "sha256": record.get("sha256"),
        })
    packages.sort(key=lambda row: (str(row["name"]), str(row["version"]), str(row["build"])))
    return {"kind": "conda-meta", "package_count": len(packages),
            "sha256": _sha(canonical_bytes(packages))}


def _pip_inventory(prefix):
    """Hash installed Python distribution RECORD/PKG-INFO files.

    A conda prefix can carry pip-installed distributions (for example Gmsh)
    that never appear in ``conda-meta``.  Hashing the installed distribution
    metadata makes those packages part of the attested inventory instead of
    trusting a package-lock file that does not mention them.
    """
    records = []
    for pattern in ("lib/python*/site-packages", "Lib/site-packages", "lib/site-packages"):
        for site in sorted(Path(prefix).glob(pattern)):
            if not site.is_dir():
                continue
            for path in sorted(site.glob("*.dist-info/RECORD")) + sorted(site.glob("*.egg-info/PKG-INFO")):
                if path.is_file():
                    records.append({"name": path.parent.name, "sha256": _sha(path.read_bytes())})
    records.sort(key=lambda row: row["name"])
    return {"count": len(records), "sha256": _sha(canonical_bytes(records)) if records else None}


def _runtime_inventory(runtime):
    lock = runtime["lock_provenance"]
    if lock["kind"] in {"conda-meta", "app-bundle"}:
        path = Path(lock["path"])
        if path.is_dir():
            conda = _conda_inventory(path)
            pip = _pip_inventory(path.parent)
            conda["pip"] = pip
            conda["installed_distributions"] = pip["count"]
            return conda
        if path.is_file():
            return {"kind": lock["kind"], "package_count": None,
                    "sha256": _sha(path.read_bytes())}
        return {"kind": lock["kind"], "package_count": None, "sha256": None}
    if lock["kind"] in {"pip-freeze", "requirements-txt"}:
        path = Path(lock["path"])
        if path.is_file():
            return {"kind": lock["kind"], "package_count": None,
                    "sha256": _sha(path.read_bytes())}
        return {"kind": lock["kind"], "package_count": None, "sha256": None}
    return {"kind": "none", "package_count": None, "sha256": None}


_NATIVE_LOAD_COMMANDS = {}
_MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def _native_load_paths(path, digest):
    """Cache immutable load commands, resolving their symlinks at each scan."""
    key = (str(path), digest)
    if key not in _NATIVE_LOAD_COMMANDS:
        result = subprocess.run(["/usr/bin/otool", "-l", str(path)],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise OSError(f"cannot inspect native dependencies: {path}: {result.stderr.strip()}")
        dependencies, command = [], None
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("cmd "):
                command = line.split()[1]
            elif line.startswith("name ") and command in {
                    "LC_LOAD_DYLIB", "LC_REEXPORT_DYLIB", "LC_LOAD_WEAK_DYLIB",
                    "LC_LAZY_LOAD_DYLIB", "LC_LOAD_UPWARD_DYLIB"}:
                value = line[5:].split(" (offset ", 1)[0]
                if value.startswith("/") and not value.startswith(("/usr/lib/", "/System/Library/")):
                    dependencies.append((value, command == "LC_LOAD_WEAK_DYLIB"))
        _NATIVE_LOAD_COMMANDS[key] = tuple(dependencies)
    return [(Path(value), weak) for value, weak in _NATIVE_LOAD_COMMANDS[key]]


def runtime_content_manifest(runtime):
    """Hash complete installed trees, including native libraries and data.

    Read roots must contain immutable installations, separate from preparation
    and run workspaces. Symlink destinations are included once; unreadable or
    missing content makes the manifest incomplete rather than silently partial.
    """
    if runtime["kind"] == "container":
        from scisaurus.runtime.container_runtime import image_manifest
        return image_manifest(runtime)
    pending = [Path(root) for root in runtime_read_roots(runtime)]
    executable = Path(runtime["executable"])
    pending.append(executable.parent.parent if executable.parent.name == "bin"
                   else executable.parent)
    seen, rows, errors = set(), [], []
    total = 0
    while pending:
        root = pending.pop()
        try:
            root = root.resolve(strict=True)
            if root in seen or any(root.is_relative_to(parent) for parent in seen):
                continue
            seen.add(root)
            paths = [root] if root.is_file() else []
            if root.is_dir():
                for base, directories, names in os.walk(
                        root, onerror=lambda error: errors.append(str(error))):
                    directories.sort()
                    for name in directories + sorted(names):
                        path = Path(base) / name
                        if path.is_symlink():
                            target = path.resolve(strict=True)
                            rows.append({"path": str(path), "symlink": os.readlink(path)})
                            pending.append(target)
                        elif path.is_file():
                            paths.append(path)
            for path in sorted(paths):
                digest = hashlib.sha256()
                size = 0
                magic = b""
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        if size == 0:
                            magic = chunk[:4]
                        digest.update(chunk)
                        size += len(chunk)
                rows.append({"path": str(path), "sha256": digest.hexdigest(), "size": size})
                if sys.platform == "darwin" and magic in _MACHO_MAGIC:
                    for dependency, weak in _native_load_paths(path, digest.hexdigest()):
                        if weak and not dependency.exists():
                            rows.append({"path": str(dependency), "weak_dependency_absent": True})
                        else:
                            pending.append(dependency)
                total += size
        except OSError as error:
            errors.append(str(error))
    rows.sort(key=lambda row: row["path"])
    return {"schema_version": "installed-runtime-content-2",
            "sha256": _sha(canonical_bytes(rows)), "files": len(rows), "bytes": total,
            "complete": bool(rows) and not errors, "errors": sorted(errors)}


def _inventory_sha256(inventory):
    return _sha(canonical_bytes({
        "kind": inventory.get("kind"),
        "package_count": inventory.get("package_count"),
        "sha256": inventory.get("sha256"),
        "pip": inventory.get("pip"),
    }))


def _probe_runtime(runtime, root, *, runner, deadline):
    workspace = Path(root) / f"probe-{runtime['label']}"
    workspace.mkdir(parents=True, exist_ok=True)
    program = workspace / "laboratory_probe.py"
    program.write_text(PROBE_PROGRAM)
    command = [runtime["executable"], str(program), *runtime["probe_modules"]]
    environment = resolve_runtime_environment(runtime, workspace)
    started = time.monotonic()
    try:
        timeout = max(1.0, min(deadline - time.monotonic(), 900.0))
        if runtime["kind"] == "container":
            from scisaurus.runtime.container_runtime import run_container
            result = run_container(runtime, program, workspace=workspace, arguments=runtime["probe_modules"],
                                   timeout_seconds=timeout, env=environment)
        else:
            result = runner(command, workspace=str(workspace), input_bytes=b"",
                            timeout_seconds=timeout, allow_network=False, env=environment,
                            read_only_paths=runtime_read_roots(runtime))
    except OSError as exc:
        return {"status": "failed", "mode": None, "returncode": None, "stdout": "",
                "stderr": "", "elapsed_seconds": time.monotonic() - started,
                "startup_error": {"type": type(exc).__name__, "errno": exc.errno,
                                  "message": str(exc)}}
    record = {"status": "verified" if result.completed else "failed",
              "mode": result.mode, "returncode": result.returncode,
              "stdout": result.stdout.decode("utf-8", errors="replace")[:20000],
              "stderr": result.stderr.decode("utf-8", errors="replace")[:20000],
              "timed_out": result.timed_out, "truncated": result.truncated,
              "elapsed_seconds": time.monotonic() - started,
              **({"cleanup": result.cleanup} if hasattr(result, "cleanup") else {})}
    if record["status"] == "verified":
        report = None
        try:
            candidate = json.loads(record["stdout"])
        except ValueError:
            candidate = None
        if isinstance(candidate, dict) and isinstance(candidate.get("modules"), dict):
            report = candidate
        if report is None:
            # Strict: a provisioning probe must emit exactly one JSON object on
            # stdout.  Diagnostics, including native import output, belong on
            # stderr and never justify scanning the tail of stdout.
            record["status"] = "failed"
            record["probe_error"] = ("probe stdout was not exactly one JSON object; "
                                     "diagnostics must be written to stderr")
        else:
            for module, state in report["modules"].items():
                if not (isinstance(state, dict) and state.get("imported") is True):
                    record["status"] = "failed"
            if set(report.get("commands", {})) != set(runtime.get("commands", {})) or any(
                    state.get("executable") is not True for state in report.get("commands", {}).values()):
                record["status"] = "failed"
            record["report"] = report
    return record


ATTESTATION_FIELDS = frozenset({
    "schema_version", "laboratory_id", "config_sha256", "attestation_sha256", "created_epoch",
    "host", "isolation", "runtimes", "verified_labels", "unverified_labels", "scientific_bound",
})
ATTESTATION_RUNTIME_FIELDS = frozenset({
    "label", "kind", "executable", "environment", "environment_sha256", "read_only_roots",
    "read_roots_sha256", "executable_sha256", "inventory", "content_manifest", "probe", "verified",
})


def seal_attestation(body):
    """Return *body* with its content address set over every other field."""
    sealed = {key: value for key, value in body.items() if key != "attestation_sha256"}
    sealed["attestation_sha256"] = _sha(canonical_bytes(sealed))
    return sealed


def validate_attestation(value):
    """Strictly validate a sealed attestation and return it unchanged."""
    _object(value, ATTESTATION_FIELDS, set(), "laboratory.attestation")
    if value["schema_version"] != LABORATORY_ATTESTATION_SCHEMA_VERSION:
        raise ValidationError("laboratory attestation has an unsupported schema")
    _identifier(value["laboratory_id"], "laboratory.attestation.laboratory_id")
    for key in ("config_sha256", "attestation_sha256"):
        if not isinstance(value[key], str) or not _SHA.fullmatch(value[key]):
            raise ValidationError(f"laboratory.attestation.{key} must be a sha256 digest")
    if value["attestation_sha256"] != seal_attestation(value)["attestation_sha256"]:
        raise ValidationError("laboratory attestation content address does not match its body")
    if not isinstance(value["scientific_bound"], bool):
        raise ValidationError("laboratory.attestation.scientific_bound must be a boolean")
    if not isinstance(value["runtimes"], list) or not value["runtimes"]:
        raise ValidationError("laboratory.attestation.runtimes must be a nonempty list")
    for index, row in enumerate(value["runtimes"]):
        _object(row, ATTESTATION_RUNTIME_FIELDS, set(), f"laboratory.attestation.runtimes[{index}]")
        if row["kind"] not in RUNTIME_KINDS:
            raise ValidationError("laboratory attestation runtime has an unsupported kind")
        if (row["verified"] is True) != ((row["probe"] or {}).get("status") == "verified"):
            raise ValidationError("laboratory attestation runtime verified flag is inconsistent")
    if set(value["verified_labels"]) | set(value["unverified_labels"]) != {
            row["label"] for row in value["runtimes"]}:
        raise ValidationError("laboratory attestation label projections are inconsistent")
    return value


def provision_laboratory(laboratory, root, *, deadline, runner=None, note=None,
                         scientific_bound=False):
    """Execute and record every declared runtime in a trusted preparation step.

    ``runner`` must match :func:`run_sandboxed`.  When it is omitted the native
    sandbox is used; callers without a sandbox may pass :func:`direct_runner`
    explicitly, and the resulting attestation records ``unsandboxed`` mode so
    downstream code cannot represent it as isolated.

    The attestation is content-addressed.  An existing scientific-bound
    attestation is never overwritten under the same identity; a changed
    preparation must use a new identity or an explicit re-provision.
    """
    validate_laboratory(laboratory)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if runner is None:
        runner = run_sandboxed
    used_direct = runner is direct_runner
    runtimes = []
    for runtime in laboratory["runtimes"]:
        executable = Path(runtime["executable"])
        roots = runtime_read_roots(runtime)
        row = {"label": runtime["label"], "kind": runtime["kind"],
               "executable": str(executable),
               "environment": deepcopy(runtime["environment"]),
               "environment_sha256": _sha(canonical_bytes(runtime["environment"])),
               "read_only_roots": list(roots),
               "read_roots_sha256": _sha(canonical_bytes(list(roots))),
               "executable_sha256": None, "inventory": _runtime_inventory(runtime),
               "content_manifest": runtime_content_manifest(runtime),
               "probe": None, "verified": False}
        if executable.is_file():
            row["executable_sha256"] = _sha(executable.resolve().read_bytes())
        else:
            row["probe"] = {"status": "failed", "mode": None, "returncode": None,
                            "stdout": "", "stderr": "declared executable is absent",
                            "elapsed_seconds": 0.0}
        if row["executable_sha256"] is not None:
            row["probe"] = _probe_runtime(runtime, root, runner=runner, deadline=deadline)
            row["verified"] = row["probe"].get("status") == "verified"
        runtimes.append(row)
    body = {
        "schema_version": LABORATORY_ATTESTATION_SCHEMA_VERSION,
        "laboratory_id": laboratory["id"],
        "config_sha256": laboratory_identity(laboratory),
        "created_epoch": time.time(),
        "host": {"platform": platform.system(), "architecture": platform.machine(),
                 "python": sys.version},
        "isolation": {
            "runner": "direct_runner" if used_direct else "run_sandboxed",
            "sandbox_exec_available": sandbox_status().get("sandbox_exec"),
            "note": note or ("Unsandboxed trusted preparation; execution isolation is asserted "
                             "separately by the production controller."
                             if used_direct else
                             "Preparation executed through the deny-by-default sandbox."),
        },
        "runtimes": runtimes,
        "verified_labels": [row["label"] for row in runtimes if row["verified"]],
        "unverified_labels": [row["label"] for row in runtimes if not row["verified"]],
        "scientific_bound": bool(scientific_bound),
    }
    attestation = seal_attestation(body)
    target = root / "laboratory-attestation.json"
    if target.is_file():
        try:
            existing = validate_attestation(json.loads(target.read_text()))
        except (OSError, ValueError, ValidationError) as exc:
            raise ValidationError(
                "refusing to replace an unreadable laboratory attestation; "
                "re-provision into a new directory") from exc
        if existing.get("scientific_bound") is True and existing != attestation:
            raise ValidationError(
                "refusing to overwrite a scientific-bound laboratory attestation under the "
                "same identity; prepare a new laboratory identity or directory")
        if existing == attestation:
            return existing
    temporary = target.with_suffix(".json.tmp")
    temporary.write_bytes(canonical_bytes(attestation))
    temporary.replace(target)
    return attestation


def load_attestation(path):
    path = Path(path)
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError(f"laboratory attestation is unreadable: {path}") from exc
    return validate_attestation(value)


def _inventory_acceptable(inventory):
    """Reject an inventory that cannot attest any installed content.

    A bundle/conda lock that yields no packages, and a metadata-only lock with
    no pip distribution records, are empty attestations: presence is not
    integrity and package-lock metadata alone is not runtime byte integrity.
    """
    kind = inventory.get("kind")
    if kind in {"conda-meta", "app-bundle"}:
        return bool(inventory.get("package_count")) or bool((inventory.get("pip") or {}).get("count"))
    if kind in {"pip-freeze", "requirements-txt"}:
        return bool(inventory.get("sha256"))
    return True


def verify_laboratory(laboratory, attestation, *, runner=None, root=None, deadline=None):
    """Recompute declared runtime identity and report drift without silent repair."""
    validate_laboratory(laboratory)
    validate_attestation(attestation)
    drift = []
    identity = laboratory_identity(laboratory)
    if attestation.get("config_sha256") != identity:
        drift.append({"kind": "configuration_changed",
                      "expected": attestation.get("config_sha256"), "observed": identity})
    declared = {row["label"]: row for row in laboratory["runtimes"]}
    attested = {row.get("label"): row for row in attestation.get("runtimes", [])
                if isinstance(row, dict)}
    for label, runtime in declared.items():
        record = attested.get(label)
        if record is None:
            drift.append({"kind": "runtime_missing", "runtime": label})
            continue
        executable = Path(runtime["executable"])
        if not executable.is_file():
            drift.append({"kind": "executable_missing", "runtime": label,
                          "path": str(executable)})
            continue
        observed_executable = _sha(executable.resolve().read_bytes())
        if observed_executable != record.get("executable_sha256"):
            drift.append({"kind": "executable_changed", "runtime": label,
                          "expected": record.get("executable_sha256"),
                          "observed": observed_executable})
        roots = list(runtime_read_roots(runtime))
        if _sha(canonical_bytes(runtime["environment"])) != record.get("environment_sha256"):
            drift.append({"kind": "environment_changed", "runtime": label,
                          "expected": record.get("environment_sha256"),
                          "observed": _sha(canonical_bytes(runtime["environment"]))})
        if _sha(canonical_bytes(roots)) != record.get("read_roots_sha256"):
            drift.append({"kind": "read_roots_changed", "runtime": label,
                          "expected": record.get("read_roots_sha256"),
                          "observed": _sha(canonical_bytes(roots))})
        observed_inventory = _runtime_inventory(runtime)
        recorded_inventory = record.get("inventory") or {}
        if not _inventory_acceptable(observed_inventory):
            drift.append({"kind": "inventory_empty", "runtime": label,
                          "observed": observed_inventory})
        if _inventory_sha256(observed_inventory) != _inventory_sha256(recorded_inventory):
            drift.append({"kind": "inventory_changed", "runtime": label,
                          "expected": recorded_inventory.get("sha256"),
                          "observed": observed_inventory.get("sha256")})
        observed_manifest = runtime_content_manifest(runtime)
        if observed_manifest.get("complete") is not True:
            drift.append({"kind": "content_manifest_incomplete", "runtime": label,
                          "errors": observed_manifest.get("errors")})
        if observed_manifest.get("sha256") != (record.get("content_manifest") or {}).get("sha256"):
            drift.append({"kind": "content_manifest_changed", "runtime": label,
                          "expected": (record.get("content_manifest") or {}).get("sha256"),
                          "observed": observed_manifest.get("sha256")})
    return {"status": "drift" if drift else "ok", "config_sha256": identity,
            "drift": drift,
            "verified_labels": [label for label in declared if label in attestation.get("verified_labels", [])]}


class LaboratoryBinding:
    """A validated laboratory plus its operator attestation for one project."""

    def __init__(self, laboratory, attestation=None):
        self.laboratory = validate_laboratory(deepcopy(laboratory))
        self.attestation = validate_attestation(deepcopy(attestation)) if attestation else {}
        self.identity = laboratory_identity(self.laboratory)

    @classmethod
    def load(cls, path, *, attestation_path=None):
        laboratory = load_laboratory(path)
        attestation = load_attestation(attestation_path) if attestation_path else None
        return cls(laboratory, attestation)

    def runtime(self, label):
        for row in self.laboratory["runtimes"]:
            if row["label"] == label:
                return row
        raise ValidationError(f"laboratory runtime {label!r} is not declared")

    def attested(self, label):
        for row in self.attestation.get("runtimes", []):
            if isinstance(row, dict) and row.get("label") == label:
                return row
        return None

    def context(self):
        return laboratory_context(self.laboratory, attestation=self.attestation)

    def feasibility(self):
        """Return the sealed runtime-label contract used for topic admission."""
        return laboratory_runtime_readiness(self.laboratory, attestation=self.attestation)

    def runtime_fingerprint(self, label):
        """Recompute the exact identity used for admission and cache keys.

        The fingerprint binds the laboratory config identity, the declared
        runtime environment and read roots, the executable bytes, the package
        inventory and the installed module content.  Any difference invalidates
        the attestation instead of silently reusing it.
        """
        runtime = self.runtime(label)
        executable = Path(runtime["executable"])
        record = self.attested(label) or {}
        roots = list(runtime_read_roots(runtime))
        inventory = _runtime_inventory(runtime)
        manifest = runtime_content_manifest(runtime)
        fingerprint = {
            "label": label,
            "config_sha256": self.identity,
            "executable_sha256": None,
            "environment_sha256": _sha(canonical_bytes(runtime["environment"])),
            "read_roots_sha256": _sha(canonical_bytes(roots)),
            "inventory_sha256": _inventory_sha256(inventory),
            "content_manifest_sha256": manifest.get("sha256"),
        }
        if executable.is_file():
            fingerprint["executable_sha256"] = _sha(executable.resolve().read_bytes())
        fingerprint["matches_attestation"] = bool(
            record.get("verified") is True
            and self.attestation.get("config_sha256") == self.identity
            and manifest.get("complete") is True
            and record.get("executable_sha256") == fingerprint["executable_sha256"]
            and record.get("environment_sha256") == fingerprint["environment_sha256"]
            and record.get("read_roots_sha256") == fingerprint["read_roots_sha256"]
            and _inventory_sha256(record.get("inventory") or {}) == fingerprint["inventory_sha256"]
            and (record.get("content_manifest") or {}).get("sha256")
            == fingerprint["content_manifest_sha256"])
        return fingerprint

    def verify(self, *, runner=None, root=None, deadline=None):
        return verify_laboratory(self.laboratory, self.attestation, runner=runner,
                                 root=root, deadline=deadline)


# ---------------------------------------------------------------------------
# Inert workflow preparation
# ---------------------------------------------------------------------------

def resolve_laboratory_workflow_config(workflow):
    """Return the bound laboratory identity inputs for a validated workflow.

    The returned identity is recomputed from the current content at the recorded
    path.  Callers that need to reject a changed profile compare it with the
    identity pinned in the workflow.
    """
    path = workflow.get("laboratory_config_path")
    if path is None:
        return None
    laboratory = load_laboratory(path)
    return {"laboratory": laboratory, "identity": laboratory_identity(laboratory)}


def is_additive_laboratory_extension(previous, requested):
    """Retain existing definitions exactly while appending installed capabilities."""
    validate_laboratory(previous)
    validate_laboratory(requested)
    collections = {"runtimes": "label", "design_families": "id", "physics_families": "id", "workflows": "id"}
    if {k: v for k, v in previous.items() if k not in collections} != {k: v for k, v in requested.items() if k not in collections}:
        return False
    changed = False
    for key, identity in collections.items():
        old, new = previous[key], requested[key]
        if len(new) < len(old) or new[:len(old)] != old:
            return False
        changed |= len(new) > len(old)
    return changed


def prepare_laboratory_workflow(workflow_path, laboratory_path, output_path, *, project_id=None,
                                attestation_path=None, allow_additive_upgrade=False):
    """Copy a workflow and bind one validated laboratory without running it.

    The historical workflow artifact is never modified.  A workflow that
    already carries a laboratory is only re-bound to the identical profile;
    silently swapping the laboratory is rejected.  The prepared workflow pins
    the canonical configuration identity (and, when supplied, the attestation
    content address) so a later resume that sees changed content fails closed
    instead of silently rebinding a mutable path.
    """
    workflow_path, laboratory_path = Path(workflow_path), Path(laboratory_path)
    try:
        workflow = json.loads(workflow_path.read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("workflow to prepare is unreadable") from exc
    laboratory = load_laboratory(laboratory_path)
    identity = laboratory_identity(laboratory)
    existing = workflow.get("laboratory_config_path")
    if existing is not None:
        if Path(existing).resolve() == laboratory_path.resolve():
            raise ValidationError(
                "workflow already binds this laboratory; no new copy is required")
        if laboratory_identity(load_laboratory(existing)) != identity and not (
                allow_additive_upgrade and is_additive_laboratory_extension(load_laboratory(existing), laboratory)):
            raise ValidationError(
                "workflow is already bound to a different laboratory; silent re-binding is rejected")
    prepared = deepcopy(workflow)
    prepared["laboratory_config_path"] = str(laboratory_path.resolve())
    prepared["laboratory_config_sha256"] = identity
    if attestation_path is not None:
        attestation = load_attestation(attestation_path)
        if attestation["config_sha256"] != identity:
            raise ValidationError(
                "laboratory attestation was produced for a different laboratory configuration")
        prepared["laboratory_attestation_path"] = str(Path(attestation_path).resolve())
        prepared["laboratory_attestation_sha256"] = attestation["attestation_sha256"]
    prepared["revision"] = int(workflow.get("revision", 1)) + 1
    if project_id is not None:
        _text(project_id, "laboratory workflow project_id")
        prepared["project_id"] = project_id
    from scisaurus.runtime.composer import validate_workflow
    validate_workflow(prepared)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_bytes(canonical_bytes(prepared))
    temporary.replace(output_path)
    return {"status": "prepared", "workflow_path": str(output_path),
            "project_id": prepared["project_id"],
            "laboratory_id": laboratory["id"], "laboratory_config_sha256": identity,
            "laboratory_attestation_sha256": prepared.get("laboratory_attestation_sha256"),
            "model_calls": 0, "mission_started": False}
