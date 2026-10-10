"""Opt-in laboratory configuration, provisioning, artifact handoff and drift."""
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from copy import deepcopy
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.laboratory import (
    COUPLING_ROLES, LaboratoryBinding, direct_runner, laboratory_context,
    laboratory_engineering_contract, laboratory_identity, load_attestation,
    load_laboratory, prepare_laboratory_workflow, provision_laboratory,
    validate_laboratory, validate_laboratory_engineering, verify_laboratory,
)
from scisaurus.runtime.program_sandbox import SandboxResult
from scisaurus.runtime.software_workbench import (
    SoftwareWorkbench, project_receipt, selection_contract, validate_selection,
)


def _profile(**overrides):
    laboratory = {
        "schema_version": "metamaterial-laboratory-1",
        "id": "test-lab",
        "enabled": True,
        "scope": {"domain": "architected metamaterials",
                  "objective": "broad permitted scope; the topic stays agent-authored",
                  "non_goals": ["selecting a fixed experiment"], "notes": None},
        "design_families": [{"id": "solid", "kind": "cad_solid",
                             "description": "parametric solid", "tool": "freecad",
                             "limitations": []}],
        "physics_families": [
            {"id": "thermal", "domain": "thermal", "description": "heat conduction",
             "runtime": "runtime", "capabilities": ["dw_laplace"],
             "coupling_role": "independent", "coupling_note": "standalone",
             "limitations": []},
            {"id": "thermoelastic", "domain": "multiphysics", "description": "thermal strain",
             "runtime": "runtime", "capabilities": ["dw_biot"],
             "coupling_role": "one_way", "coupling_note": "temperature to strain",
             "limitations": []},
        ],
        "workflows": [{"id": "wf", "description": "one stage",
                       "stages": [{"id": "s", "operation": "op", "runtime": "runtime",
                                   "inputs": [], "outputs": ["vtk"]}],
                       "coupling_plan": None, "limitations": []}],
        "runtimes": [{"label": "runtime", "kind": "venv",
                      "executable": sys.executable, "description": "test runtime",
                      "capabilities": ["numpy"], "probe_modules": [],
                      "environment": {}, "lock_provenance": {"kind": "none", "path": None,
                                                             "note": "test"},
                      "read_only_roots": [str(Path(sys.executable).resolve().parent)],
                      "limitations": []}],
        "assessment": {"contribution_review": True, "analytic_probes_first": True,
                       "manufacturability": True, "mesh_solver_convergence": True,
                       "conservation_residual_checks": True, "robustness_study": True,
                       "equal_constraint_comparison": True,
                       "required_coupling_disclosure": sorted(COUPLING_ROLES)},
        "limits": {"max_runtime_seconds": 60, "max_wall_seconds": 120,
                   "max_input_bytes": 1 << 20, "max_output_bytes": 1 << 20,
                   "max_runtime_files": 16},
    }
    laboratory.update(overrides)
    return laboratory


def _engineering(laboratory):
    return {
        "contribution_assessment": {
            "closest_prior_work": [{"source_ref": "evidence:1", "difference": "unit cell size"}],
            "planned_contribution": "prospective bounded increment to be tested",
            "novelty_claim": "bounded_increment"},
        "analytic_plan": [{"name": "closed form", "purpose": "bound",
                           "predicted_limit": "0.02", "acceptance_tolerance": "2%"}],
        "manufacturability_plan": {"assessment": "planned machinability", "limitations": []},
        "convergence_plan": {"mesh_or_discretization": "element size",
                             "refinement_plan": "two halvings",
                             "acceptance_criterion": "below 2%"},
        "conservation_plan": [{"quantity": "energy",
                               "method": "reaction sum",
                               "acceptance_criterion": "relative residual below 1e-8"}],
        "robustness_plan": {"perturbations": ["pitch +10%"],
                            "acceptance_criterion": "survives",
                            "limitations": []},
        "equal_constraint_comparison_plan": {"baseline_ref": "evidence:baseline",
                                             "constraints": ["equal mass"],
                                             "acceptance_criterion": "stiffer within 5%"},
        "coupling_disclosure": {"kind": "one_way",
                                "planned_exchange": "temperature artifact into strain",
                                "limitations": []},
        "scale_scope": {"homogenization_plan": "unit cell",
                        "finite_structure_plan": "plate"},
        "readiness_claim": "not_verified", "limitations": [],
    }


class StubBinding:
    """Duck-typed laboratory binding with a controllable attestation."""

    def __init__(self, laboratory, *, verified=True):
        self.laboratory = validate_laboratory(deepcopy(laboratory))
        self.identity = laboratory_identity(self.laboratory)
        self.verified = verified

    def runtime(self, label):
        for row in self.laboratory["runtimes"]:
            if row["label"] == label:
                return row
        raise ValidationError(f"laboratory runtime {label!r} is not declared")

    def runtime_fingerprint(self, label):
        self.runtime(label)
        return {"label": label, "config_sha256": self.identity,
                "executable_sha256": "a" * 64, "environment_sha256": "c" * 64,
                "read_roots_sha256": "d" * 64, "inventory_sha256": "b" * 64,
                "content_manifest_sha256": "e" * 64,
                "matches_attestation": self.verified}

    def attested(self, label):
        return {"label": label, "verified": self.verified, "executable_sha256": "a" * 64,
                "config_sha256": self.identity, "environment_sha256": "c" * 64,
                "read_roots_sha256": "d" * 64,
                "inventory": {"sha256": "b" * 64, "package_count": 1},
                "content_manifest": {"sha256": "e" * 64},
                "probe": {"status": "verified", "mode": "sandbox-exec", "elapsed_seconds": 0.1}}

    def context(self):
        return laboratory_context(self.laboratory)


class LaboratoryProfileTests(unittest.TestCase):
    def test_opt_in_identity_and_strict_fields(self):
        laboratory = validate_laboratory(_profile())
        identity = laboratory_identity(laboratory)
        changed = _profile(scope={"domain": "other", "objective": "different",
                                  "non_goals": [], "notes": None})
        self.assertNotEqual(identity, laboratory_identity(changed))
        self.assertEqual(identity, laboratory_identity(deepcopy(laboratory)))
        for mutate, pattern in (
                (lambda value: value.update({"enabled": False}), "enabled must be true"),
                (lambda value: value.update({"unexpected": 1}), "unexpected fields"),
                (lambda value: value["physics_families"][0].update({"runtime": "absent"}),
                 "undeclared runtime"),
                (lambda value: value["runtimes"][0]["environment"].update({"AWS_SECRET": "x"}),
                 "may only set declared runtime keys"),
                (lambda value: value["runtimes"][0].update({"executable": "relative/python"}),
                 "absolute declared host path"),
                (lambda value: value["assessment"].update({"robustness_study": False}),
                 "must be true"),
                (lambda value: value["assessment"].update({"required_coupling_disclosure": ["independent"]}),
                 "name every coupling role"),
        ):
            candidate = _profile()
            mutate(candidate)
            with self.subTest(pattern=pattern), self.assertRaisesRegex(ValidationError, pattern):
                validate_laboratory(candidate)

    def test_context_is_path_free_and_does_not_select_a_topic(self):
        laboratory = _profile()
        laboratory["runtimes"][0]["environment"] = {"PYTHONHOME": "/private/runtime"}
        context = laboratory_context(laboratory)
        serialized = canonical_bytes(context).decode()
        self.assertNotIn(sys.executable, serialized)
        self.assertNotIn("/private/runtime", serialized)
        self.assertIn("does not select the research topic", context["topic_authority"])
        self.assertFalse(context["runtimes"][0]["controller_attested"])
        self.assertIn("presence or a bare import is never readiness", context["readiness_rule"])

    def test_design_iteration_defers_final_claims_without_removing_assessment(self):
        context = laboratory_context(_profile())
        iteration = context["design_iteration"]
        self.assertEqual(iteration["sequence"][1], "baseline_and_small_pilot")
        self.assertEqual(iteration["sequence"][-1], "validate_final_design")
        self.assertIn("Re-execute changed sources", iteration["iteration"])
        self.assertIn("not prerequisites", iteration["selection_boundary"])
        self.assertIn("independent recalculation", iteration["final_claim"])
        self.assertTrue(context["assessment_requirements"]["mesh_solver_convergence"])

    def test_engineering_contract_rejects_claimed_readiness(self):
        laboratory = validate_laboratory(_profile())
        validate_laboratory_engineering(_engineering(laboratory), laboratory)
        claimed = _engineering(laboratory)
        claimed["readiness_claim"] = "verified"
        with self.assertRaisesRegex(ValidationError, "must be not_verified"):
            validate_laboratory_engineering(claimed, laboratory)
        incomplete = _engineering(laboratory)
        incomplete.pop("convergence_plan")
        with self.assertRaisesRegex(ValidationError, "missing fields"):
            validate_laboratory_engineering(incomplete, laboratory)
        free_text = _engineering(laboratory)
        free_text["equal_constraint_comparison_plan"]["baseline_ref"] = "a plain plate"
        with self.assertRaisesRegex(ValidationError, "typed stage/receipt"):
            validate_laboratory_engineering(free_text, laboratory)


class ProvisioningTests(unittest.TestCase):
    def _wrapper(self, root):
        root = root / "installation"
        root.mkdir(exist_ok=True)
        executable = root / "runtime.sh"
        executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
        lock = root / "conda-meta"
        lock.mkdir(exist_ok=True)
        (lock / "pkg-1.0-0.json").write_text(json.dumps(
            {"name": "pkg", "version": "1.0", "build": "0", "channel": "test", "sha256": "c" * 64}))
        return executable, lock

    def _laboratory(self, root, executable, lock):
        return validate_laboratory(_profile(
            runtimes=[{"label": "runtime", "kind": "conda", "executable": str(executable),
                       "description": "test runtime", "capabilities": ["numpy"],
                       "probe_modules": [], "environment": {},
                       "lock_provenance": {"kind": "conda-meta", "path": str(lock), "note": "test"},
                       "read_only_roots": [str(executable.parent)], "limitations": [],
                       "resource_limits": {"address_space_bytes": 1 << 31}}]))

    def test_provisioning_executes_and_records_content_hashes(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            executable, lock = self._wrapper(root)
            laboratory = self._laboratory(root, executable, lock)
            attestation = provision_laboratory(laboratory, root / "prep",
                                               deadline=time.monotonic() + 60,
                                               runner=direct_runner)
            self.assertEqual(attestation["verified_labels"], ["runtime"])
            row = attestation["runtimes"][0]
            self.assertEqual(row["executable_sha256"],
                             hashlib.sha256(executable.read_bytes()).hexdigest())
            self.assertEqual(row["inventory"]["package_count"], 1)
            self.assertEqual(row["probe"]["mode"], "unsandboxed")
            self.assertEqual(row["probe"]["report"]["modules"], {})
            binding = LaboratoryBinding(laboratory, attestation)
            self.assertTrue(binding.runtime_fingerprint("runtime")["matches_attestation"])
            self.assertEqual(binding.verify()["status"], "ok")

    def test_executable_and_inventory_drift_are_reported(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            executable, lock = self._wrapper(root)
            laboratory = self._laboratory(root, executable, lock)
            attestation = provision_laboratory(laboratory, root / "prep",
                                               deadline=time.monotonic() + 60,
                                               runner=direct_runner)
            executable.write_text("#!/bin/sh\nexit 0\n")
            executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
            drift = verify_laboratory(laboratory, attestation)
            self.assertEqual(drift["status"], "drift")
            self.assertIn("executable_changed", {row["kind"] for row in drift["drift"]})
            executable, lock = self._wrapper(root)
            (lock / "second-2.0-0.json").write_text(json.dumps(
                {"name": "second", "version": "2.0", "build": "0", "channel": "test",
                 "sha256": "d" * 64}))
            drift = verify_laboratory(laboratory, attestation)
            self.assertIn("inventory_changed", {row["kind"] for row in drift["drift"]})
            binding = LaboratoryBinding(laboratory, attestation)
            self.assertFalse(binding.runtime_fingerprint("runtime")["matches_attestation"])

    def test_configuration_identity_change_is_drift(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            executable, lock = self._wrapper(root)
            laboratory = self._laboratory(root, executable, lock)
            attestation = provision_laboratory(laboratory, root / "prep",
                                               deadline=time.monotonic() + 60,
                                               runner=direct_runner)
            changed = deepcopy(laboratory)
            changed["scope"]["objective"] = "changed objective"
            drift = verify_laboratory(changed, attestation)
            self.assertIn("configuration_changed", {row["kind"] for row in drift["drift"]})
            self.assertFalse(LaboratoryBinding(changed, attestation)
                             .runtime_fingerprint("runtime")["matches_attestation"])

    def test_native_library_and_external_data_bytes_invalidate_attestation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            executable, lock = self._wrapper(root)
            library = executable.parent / "libsolver.dylib"
            library.write_bytes(b"native bytes")
            data_root = root / "material-data"
            data_root.mkdir()
            data = data_root / "material.dat"
            data.write_bytes(b"material bytes")
            laboratory = self._laboratory(root, executable, lock)
            laboratory["runtimes"][0]["read_only_roots"].append(str(data_root))
            attestation = provision_laboratory(laboratory, root / "prep",
                                               deadline=time.monotonic() + 60,
                                               runner=direct_runner)
            for target in (library, data):
                original = target.read_bytes()
                target.write_bytes(original + b"changed")
                self.assertFalse(LaboratoryBinding(laboratory, attestation)
                                 .runtime_fingerprint("runtime")["matches_attestation"])
                target.write_bytes(original)
            tampered = deepcopy(attestation)
            tampered["runtimes"][0]["verified"] = False
            with self.assertRaises(ValidationError):
                LaboratoryBinding(laboratory, tampered)

    def test_native_dependencies_distinguish_install_name_and_weak_load(self):
        from scisaurus.runtime.laboratory import _native_load_paths
        from types import SimpleNamespace
        text = """Load command 0
          cmd LC_ID_DYLIB
         name /build-machine/libsolver.dylib (offset 24)
        Load command 1
          cmd LC_LOAD_DYLIB
         name /opt/runtime/libsolver.dylib (offset 24)
        Load command 2
          cmd LC_LOAD_WEAK_DYLIB
         name /Library/Frameworks/Optional.framework/Optional (offset 24)
        Load command 3
          cmd LC_LOAD_DYLIB
         name /usr/lib/libSystem.B.dylib (offset 24)
        """
        with patch("scisaurus.runtime.laboratory.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout=text, stderr="")):
            result = _native_load_paths(Path("/test/native.dylib"), "test-load-commands")
        self.assertEqual(result, [(Path("/opt/runtime/libsolver.dylib"), False),
                                  (Path("/Library/Frameworks/Optional.framework/Optional"), True)])

    def test_venv_base_interpreter_content_is_bound(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            executable, lock = self._wrapper(root)
            prefix = executable.parent
            (prefix / "bin").mkdir()
            executable.rename(prefix / "bin" / "python")
            executable = prefix / "bin" / "python"
            base = root / "base"
            (base / "bin").mkdir(parents=True)
            library = base / "stdlib.dat"
            library.write_bytes(b"stdlib")
            (prefix / "pyvenv.cfg").write_text(f"home = {base / 'bin'}\n")
            laboratory = self._laboratory(root, executable, lock)
            laboratory["runtimes"][0]["read_only_roots"] = [str(prefix)]
            attestation = provision_laboratory(laboratory, root / "prep",
                                               deadline=time.monotonic() + 60, runner=direct_runner)
            self.assertTrue(LaboratoryBinding(laboratory, attestation)
                            .runtime_fingerprint("runtime")["matches_attestation"])
            library.write_bytes(b"changed stdlib")
            self.assertFalse(LaboratoryBinding(laboratory, attestation)
                             .runtime_fingerprint("runtime")["matches_attestation"])


class ArtifactHandoffTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.laboratory = _profile()
        self.binding = StubBinding(self.laboratory)
        self.runs = []

    def tearDown(self):
        self.directory.cleanup()

    def _runner(self, command, **kwargs):
        self.runs.append((command, kwargs))
        workspace = Path(kwargs["workspace"])
        program = Path(command[-1])
        source = program.read_text()
        if "fail" in source:
            return SandboxResult(1, b"partial output", b"missing dependency", False, False,
                                 "sandbox-exec")
        if "write_output" in source:
            (workspace / "part.step").write_text("STEP-CONTENT")
        if "check_input" in source:
            if not (workspace / "inputs" / "part.step").is_file():
                return SandboxResult(1, b"", b"input missing", False, False, "sandbox-exec")
            (workspace / "result.json").write_text("{}")
        return SandboxResult(0, b'{"answer": 1}', b"", False, False, "sandbox-exec")

    def _workbench(self, *, verified=True):
        binding = StubBinding(self.laboratory, verified=verified)
        return SoftwareWorkbench(self.root / "wb", deadline=time.monotonic() + 120,
                                 laboratory=binding, runner=self._runner), binding

    def test_list_and_inspect_runtimes_are_uncached_and_report_drift(self):
        workbench, binding = self._workbench()
        with patch("scisaurus.runtime.software_workbench.sandbox_status",
                   return_value={"mode": "sandbox-exec"}):
            listed = workbench.execute({"operation": "list_runtimes", "arguments": {}})
            self.assertEqual(listed["outcome"], "ok")
            self.assertTrue(listed["result"]["runtimes"][0]["controller_attested"])
            self.assertTrue(listed["result"]["runtimes"][0]["identity_current"])
            first = listed["receipt_ref"]
            listed = workbench.execute({"operation": "list_runtimes", "arguments": {}})
            self.assertFalse(listed["reused"])
            inspected = workbench.execute({"operation": "inspect_runtime",
                                           "arguments": {"runtime": "runtime"}})
            self.assertTrue(inspected["result"]["execution_authorized"])
            self.assertIn("never readiness", inspected["result"]["readiness_semantics"])
        self.assertNotEqual(first, listed["receipt_ref"])

    def test_artifact_outputs_are_retained_and_consumed_as_exact_inputs(self):
        workbench, _ = self._workbench()
        source = "write_output\nprint('{\"answer\": 1}')"
        with patch("scisaurus.runtime.software_workbench.sandbox_status",
                   return_value={"mode": "sandbox-exec"}):
            produced = workbench.execute({"operation": "run", "arguments": {
                "runtime": "runtime", "source": source, "input": {},
                "purpose": "scientific_computation", "outputs": [{"name": "part.step"}],
                "documentation_refs": [], "expected": None}})
            self.assertEqual(produced["outcome"], "ok", produced)
            self.assertEqual(produced["result"]["runtime"], "runtime")
            self.assertEqual(len(produced["result"]["outputs"]), 1)
            artifact = produced["result"]["outputs"][0]
            self.assertEqual(artifact["sha256"], hashlib.sha256(b"STEP-CONTENT").hexdigest())
            consumed = workbench.execute({"operation": "run", "arguments": {
                "runtime": "runtime", "source": "check_input\nprint('{\"answer\": 1}')",
                "input": {"n": 2}, "purpose": "scientific_computation",
                "inputs": [{"artifact_ref": artifact["artifact_ref"], "name": "part.step"}],
                "outputs": [{"name": "result.json"}], "documentation_refs": [], "expected": None}})
            self.assertEqual(consumed["outcome"], "ok", consumed)
            self.assertEqual(consumed["result"]["inputs"][0]["staged_path"], "inputs/part.step")
            self.assertEqual(consumed["result"]["outputs"][0]["name"], "result.json")

    def test_traversal_symlink_missing_and_unknown_receipts_are_rejected(self):
        workbench, _ = self._workbench()
        with patch("scisaurus.runtime.software_workbench.sandbox_status",
                   return_value={"mode": "sandbox-exec"}):
            base = {"runtime": "runtime", "source": "print('{\"answer\": 1}')",
                    "input": {}, "purpose": "scientific_computation",
                    "documentation_refs": [], "expected": None}
            for name in ("../escape.step", "/absolute.step", "a/../../escape.step"):
                with self.subTest(name=name), self.assertRaisesRegex(ValidationError, "leaves the run workspace"):
                    workbench.execute({"operation": "run", "arguments": {
                        **base, "inputs": [{"artifact_ref": "software-artifact:sha256:" + "0" * 64,
                                            "name": name}]}})
            unknown = {"operation": "run", "arguments": {
                **base, "inputs": [{"artifact_ref": "software-artifact:sha256:" + "0" * 64,
                                    "name": "in.step"}]}}
            with self.assertRaisesRegex(ValidationError, "missing or not a regular file"):
                workbench.execute(unknown)
            symlink = self.root / "wb" / "artifacts" / ("1" * 64)
            symlink.parent.mkdir(parents=True, exist_ok=True)
            symlink.symlink_to(self.root / "outside")
            with self.assertRaisesRegex(ValidationError, "missing or not a regular file"):
                workbench.execute({"operation": "run", "arguments": {
                    **base, "inputs": [{"artifact_ref": "software-artifact:sha256:" + "1" * 64,
                                        "name": "in.step"}]}})

    def test_changed_artifact_cannot_hide_behind_a_receipt(self):
        workbench, _ = self._workbench()
        with patch("scisaurus.runtime.software_workbench.sandbox_status",
                   return_value={"mode": "sandbox-exec"}):
            produced = workbench.execute({"operation": "run", "arguments": {
                "runtime": "runtime", "source": "write_output\nprint('{\"answer\": 1}')",
                "input": {}, "purpose": "scientific_computation",
                "outputs": [{"name": "part.step"}], "documentation_refs": [], "expected": None}})
        artifact = produced["result"]["outputs"][0]
        digest = artifact["sha256"]
        (self.root / "wb" / "artifacts" / digest).write_text("TAMPERED")
        with self.assertRaisesRegex(ValidationError, "content changed"):
            workbench.execute({"operation": "run", "arguments": {
                "runtime": "runtime", "source": "check_input\nprint('{\"answer\": 1}')",
                "input": {}, "purpose": "scientific_computation",
                "inputs": [{"artifact_ref": artifact["artifact_ref"], "name": "part.step"}],
                "documentation_refs": [], "expected": None}})

    def test_runtime_drift_and_failed_stdout_are_preserved(self):
        workbench, _ = self._workbench(verified=False)
        with patch("scisaurus.runtime.software_workbench.sandbox_status",
                   return_value={"mode": "sandbox-exec"}):
            with self.assertRaisesRegex(ValidationError, "provisioning attestation"):
                workbench.execute({"operation": "run", "arguments": {
                    "runtime": "runtime", "source": "print('{\"answer\": 1}')", "input": {},
                    "purpose": "scientific_computation", "documentation_refs": [], "expected": None}})
        workbench, _ = self._workbench()
        with patch("scisaurus.runtime.software_workbench.sandbox_status",
                   return_value={"mode": "sandbox-exec"}):
            failed = workbench.execute({"operation": "run", "arguments": {
                "runtime": "runtime", "source": "fail\nprint('{}')", "input": {"parameter": 2},
                "purpose": "scientific_computation",
                "outputs": [{"name": "part.step"}],
                "documentation_refs": [], "expected": None}})
        self.assertEqual(failed["outcome"], "failed")
        self.assertEqual(failed["execution"]["stdout"], "partial output")
        self.assertEqual(failed["execution"]["stderr"], "missing dependency")
        self.assertEqual(failed["execution"]["returncode"], 1)
        self.assertEqual(failed["action"]["arguments"]["input"], {"parameter": 2})

    def test_lab_bound_selection_requires_engineering_obligations(self):
        workbench, binding = self._workbench()
        response = selection_contract(binding.laboratory)
        response.update(decision="hold", summary="held")
        response["software_selection"].update(strategy="unavailable", rationale="not ready")
        response["software_selection"]["laboratory_engineering"] = _engineering(binding.laboratory)
        validate_selection(response, workbench, [])
        missing = deepcopy(response)
        missing["software_selection"].pop("laboratory_engineering")
        validate_selection(missing, workbench, [])
        passing = deepcopy(missing)
        passing.update(decision="pass", summary="ready")
        with self.assertRaisesRegex(ValidationError, "laboratory_engineering is required"):
            validate_selection(passing, workbench, [{"outcome": "ok",
                                                     "action": {"operation": "check_environment"}}])
        unbound = SoftwareWorkbench(self.root / "wb2", deadline=time.monotonic() + 60)
        base = selection_contract()
        base.update(decision="hold", summary="held")
        base["software_selection"].update(strategy="unavailable", rationale="not ready")
        validate_selection(base, unbound, [])
        base["software_selection"]["laboratory_engineering"] = _engineering(binding.laboratory)
        with self.assertRaisesRegex(ValidationError, "requires a bound laboratory"):
            validate_selection(base, unbound, [])


class WorkflowBindingTests(unittest.TestCase):
    def _workflow(self, root):
        config = root / "stage.json"
        config.write_text("{}")
        stage_dir = root / "survey"
        stage_dir.mkdir()
        return {
            "schema_version": "composer-workflow-1", "id": "lab-workflow", "revision": 1,
            "project_id": str(root / "composer"),
            "objective": "exercise an opt-in laboratory",
            "stages": [{"id": "survey", "kind": "survey", "config_path": str(config.resolve()),
                        "project_dir": str(stage_dir.resolve()), "depends_on": [],
                        "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                        "reuse_completed": False, "reuse_output_path": None}],
            "time_policy": {"first_result_seconds": 1, "target_seconds": 10,
                            "hard_seconds": 30, "checkpoint_seconds": 1},
            "completion": {"required_stage_ids": ["survey"], "release_requires_human": True},
        }

    def test_preparation_is_inert_and_rejects_silent_rebinding(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow_path = root / "workflow.json"
            workflow = self._workflow(root)
            workflow_path.write_text(json.dumps(workflow))
            laboratory_path = root / "laboratory-1.json"
            laboratory_path.write_text(json.dumps(_profile(id="lab-one")))
            other_path = root / "laboratory-2.json"
            other_path.write_text(json.dumps(_profile(id="lab-two")))
            output = root / "workflow-lab.json"
            result = prepare_laboratory_workflow(workflow_path, laboratory_path, output)
            self.assertEqual(result["model_calls"], 0)
            self.assertFalse(result["mission_started"])
            self.assertEqual(json.loads(workflow_path.read_text()), workflow)
            bound = json.loads(output.read_text())
            self.assertEqual(bound["revision"], 2)
            self.assertEqual(bound["laboratory_config_path"], str(laboratory_path.resolve()))
            with self.assertRaisesRegex(ValidationError, "different laboratory"):
                prepare_laboratory_workflow(output, other_path, root / "workflow-other.json")
            with self.assertRaisesRegex(ValidationError, "already binds"):
                prepare_laboratory_workflow(output, laboratory_path, root / "workflow-same.json")

    def test_invalid_laboratory_path_is_rejected_by_workflow_validation(self):
        from scisaurus.runtime.composer import validate_workflow
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["laboratory_config_path"] = str((root / "missing.json"))
            with self.assertRaisesRegex(ValidationError, "existing absolute file"):
                validate_workflow(workflow)
            bad = root / "bad.json"
            bad.write_text(json.dumps({"schema_version": "nope"}))
            workflow["laboratory_config_path"] = str(bad)
            with self.assertRaisesRegex(ValidationError, "laboratory config is invalid"):
                validate_workflow(workflow)


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_workspace_environment_overrides_caller_and_declared_values(self):
        from scisaurus.runtime.program_sandbox import sandbox_environment
        with tempfile.TemporaryDirectory() as path:
            workspace = Path(path)
            with patch.dict("os.environ", {"PYTHONHOME": "/caller/home",
                                           "PYTHONPATH": "/caller/modules",
                                           "DYLD_LIBRARY_PATH": "/caller/lib"}, clear=False):
                env = sandbox_environment(workspace, env={
                    "PYTHONPATH": "/declared/modules",
                    "CFFIXED_USER_HOME": "/declared/home",
                    "HOME": "/declared/home",
                    "KMP_DUPLICATE_LIB_OK": "TRUE",
                })
        self.assertEqual(env["HOME"], str(workspace))
        self.assertEqual(env["CFFIXED_USER_HOME"], str(workspace))
        for key, relative in (("XDG_CONFIG_HOME", ".config"), ("XDG_CACHE_HOME", ".cache"),
                               ("XDG_DATA_HOME", ".local/share"), ("MPLCONFIGDIR", ".matplotlib"),
                               ("TMPDIR", "")):
            self.assertTrue(Path(env[key]).is_relative_to(workspace), (key, env[key]))
            self.assertEqual(Path(env[key]), (workspace / relative) if relative else workspace)
        self.assertEqual(env["PYTHONPATH"], "/declared/modules")
        self.assertNotIn("PYTHONHOME", env)
        self.assertNotIn("DYLD_LIBRARY_PATH", env)
        self.assertNotIn("KMP_DUPLICATE_LIB_OK", env)


class AttestationIdentityTests(unittest.TestCase):
    def _wrapper(self, root):
        root = root / "installation"
        root.mkdir(exist_ok=True)
        executable = root / "runtime.sh"
        executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
        lock = root / "conda-meta"
        lock.mkdir(exist_ok=True)
        (lock / "pkg-1.0-0.json").write_text(json.dumps(
            {"name": "pkg", "version": "1.0", "build": "0", "channel": "test", "sha256": "c" * 64}))
        return executable, lock

    def _laboratory(self, root, executable, lock, *, probe_modules=None):
        return validate_laboratory(_profile(
            runtimes=[{"label": "runtime", "kind": "conda", "executable": str(executable),
                       "description": "test runtime", "capabilities": ["numpy"],
                       "probe_modules": list(probe_modules or []), "environment": {},
                       "lock_provenance": {"kind": "conda-meta", "path": str(lock), "note": "test"},
                       "read_only_roots": [str(executable.parent)], "limitations": [],
                       "resource_limits": {"address_space_bytes": 1 << 31}}]))

    def test_changed_environment_or_read_roots_with_unchanged_executable(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            executable, lock = self._wrapper(root)
            laboratory = self._laboratory(root, executable, lock)
            attestation = provision_laboratory(laboratory, root / "prep",
                                               deadline=time.monotonic() + 60, runner=direct_runner)
            from copy import deepcopy as _deepcopy
            changed = _deepcopy(laboratory)
            changed["runtimes"][0]["environment"] = {"PYTHONPATH": "/somewhere/else"}
            drift = verify_laboratory(changed, attestation)
            self.assertIn("environment_changed", {row["kind"] for row in drift["drift"]})
            changed = _deepcopy(laboratory)
            changed["runtimes"][0]["read_only_roots"] = [str(root / "other")]
            drift = verify_laboratory(changed, attestation)
            self.assertIn("read_roots_changed", {row["kind"] for row in drift["drift"]})
            binding = LaboratoryBinding(changed, attestation)
            self.assertFalse(binding.runtime_fingerprint("runtime")["matches_attestation"])

    def test_runtime_module_modification_is_content_manifest_drift(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            prefix = root / "rt"
            site = prefix / "lib" / "python3.12" / "site-packages"
            module = site / "mymod"
            module.mkdir(parents=True)
            init = module / "__init__.py"
            init.write_text("VALUE = 1\n")
            executable = prefix / "bin" / "wrapper.sh"
            executable.parent.mkdir(parents=True)
            executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
            executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
            laboratory = validate_laboratory(_profile(
                runtimes=[{"label": "runtime", "kind": "venv", "executable": str(executable),
                           "description": "module runtime", "capabilities": ["mymod"],
                           "probe_modules": ["mymod"],
                           "environment": {"PYTHONPATH": str(site)},
                           "lock_provenance": {"kind": "none", "path": None, "note": "test"},
                           "read_only_roots": [str(prefix)], "limitations": []}]))
            attestation = provision_laboratory(laboratory, root / "prep",
                                               deadline=time.monotonic() + 60, runner=direct_runner)
            self.assertEqual(attestation["verified_labels"], ["runtime"])
            manifest = attestation["runtimes"][0]["content_manifest"]
            self.assertIsNotNone(manifest["sha256"])
            init.write_text("VALUE = 2\n")
            drift = verify_laboratory(laboratory, attestation)
            self.assertIn("content_manifest_changed", {row["kind"] for row in drift["drift"]})

    def test_attestation_is_content_addressed_and_scientific_bound_is_immutable(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            executable, lock = self._wrapper(root)
            laboratory = self._laboratory(root, executable, lock)
            attestation = provision_laboratory(laboratory, root / "prep",
                                               deadline=time.monotonic() + 60, runner=direct_runner,
                                               scientific_bound=True)
            self.assertTrue(attestation["scientific_bound"])
            loaded = load_attestation(root / "prep" / "laboratory-attestation.json")
            self.assertEqual(loaded["attestation_sha256"], attestation["attestation_sha256"])
            tampered = deepcopy(loaded)
            tampered["laboratory_id"] = "other-lab"
            (root / "prep" / "laboratory-attestation.json").write_text(json.dumps(tampered))
            with self.assertRaisesRegex(ValidationError, "content address"):
                load_attestation(root / "prep" / "laboratory-attestation.json")
            (root / "prep" / "laboratory-attestation.json").write_bytes(canonical_bytes(loaded))
            changed = deepcopy(laboratory)
            changed["scope"]["objective"] = "a different objective"
            with self.assertRaisesRegex(ValidationError, "scientific-bound"):
                provision_laboratory(changed, root / "prep", deadline=time.monotonic() + 60,
                                     runner=direct_runner, scientific_bound=True)

    def test_rejects_empty_bundle_inventory(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            empty_lock = root / "empty-conda-meta"
            empty_lock.mkdir()
            installation = root / "installation"
            installation.mkdir()
            executable = installation / "runtime.sh"
            executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
            executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
            laboratory = validate_laboratory(_profile(
                runtimes=[{"label": "runtime", "kind": "app_bundle", "executable": str(executable),
                           "description": "bundle runtime", "capabilities": ["numpy"],
                           "probe_modules": [], "environment": {},
                           "lock_provenance": {"kind": "app-bundle", "path": str(empty_lock),
                                               "note": "empty"},
                           "read_only_roots": [str(root)], "limitations": []}]))
            attestation = provision_laboratory(laboratory, root / "prep",
                                               deadline=time.monotonic() + 60, runner=direct_runner)
            drift = verify_laboratory(laboratory, attestation)
            self.assertIn("inventory_empty", {row["kind"] for row in drift["drift"]})


class WorkflowIdentityTests(unittest.TestCase):
    def _workflow(self, root):
        config = root / "stage.json"
        config.write_text("{}")
        stage_dir = root / "survey"
        stage_dir.mkdir(exist_ok=True)
        return {
            "schema_version": "composer-workflow-1", "id": "lab-workflow", "revision": 1,
            "project_id": str(root / "composer"),
            "objective": "exercise an opt-in laboratory",
            "stages": [{"id": "survey", "kind": "survey", "config_path": str(config.resolve()),
                        "project_dir": str(stage_dir.resolve()), "depends_on": [],
                        "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                        "reuse_completed": False, "reuse_output_path": None}],
            "time_policy": {"first_result_seconds": 1, "target_seconds": 10,
                            "hard_seconds": 30, "checkpoint_seconds": 1},
            "completion": {"required_stage_ids": ["survey"], "release_requires_human": True},
        }

    def test_workflow_pins_config_identity_and_attestation(self):
        from scisaurus.runtime.composer import validate_workflow
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            laboratory_path = root / "laboratory.json"
            laboratory_path.write_text(json.dumps(_profile()))
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps(self._workflow(root)))
            output = root / "workflow-lab.json"
            prepare_laboratory_workflow(workflow_path, laboratory_path, output)
            prepared = json.loads(output.read_text())
            self.assertEqual(prepared["laboratory_config_sha256"],
                             laboratory_identity(load_laboratory(laboratory_path)))
            validate_workflow(prepared)
            # Same mutable path, changed content: the pinned identity must fail closed.
            laboratory_path.write_text(json.dumps(_profile(scope={
                "domain": "changed", "objective": "changed objective",
                "non_goals": [], "notes": None})))
            with self.assertRaisesRegex(ValidationError, "identity is missing or has changed"):
                validate_workflow(prepared)
            # A workflow that records a path without a pinned identity is rejected.
            lab_back = json.loads((root / "workflow-lab.json").read_text())
            lab_back.pop("laboratory_config_sha256")
            with self.assertRaisesRegex(ValidationError, "identity is missing or has changed"):
                validate_workflow(lab_back)

    def test_resume_rejects_changed_config_at_same_path(self):
        from scisaurus.runtime.composer import ComposerRunner
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            laboratory_path = root / "laboratory.json"
            laboratory_path.write_text(json.dumps(_profile()))
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps(self._workflow(root)))
            output = root / "workflow-lab.json"
            prepare_laboratory_workflow(workflow_path, laboratory_path, output)
            prepared = json.loads(output.read_text())
            laboratory_path.write_text(json.dumps(_profile(scope={
                "domain": "changed", "objective": "changed objective",
                "non_goals": [], "notes": None})))
            for resume in (False, True):
                with self.subTest(resume=resume), self.assertRaisesRegex(
                        ValidationError, "identity is missing or has changed"):
                    ComposerRunner(prepared, resume=resume)

    def test_workflow_rejects_wrong_or_missing_attestation(self):
        from scisaurus.runtime.composer import validate_workflow
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            laboratory_path = root / "laboratory.json"
            laboratory_path.write_text(json.dumps(_profile()))
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps(self._workflow(root)))
            workflow = self._workflow(root)
            workflow["laboratory_config_path"] = str(laboratory_path.resolve())
            workflow["laboratory_config_sha256"] = laboratory_identity(load_laboratory(laboratory_path))
            workflow["laboratory_attestation_path"] = str(root / "missing-attestation.json")
            with self.assertRaisesRegex(ValidationError, "existing absolute file"):
                validate_workflow(workflow)
            # A sealed attestation for a different configuration is not silently accepted.
            other = validate_laboratory(_profile(id="other-lab"))
            attestation = provision_laboratory(other, root / "other-prep",
                                               deadline=time.monotonic() + 60, runner=direct_runner)
            (root / "other-attestation.json").write_bytes(canonical_bytes(attestation))
            workflow["laboratory_attestation_path"] = str(root / "other-attestation.json")
            with self.assertRaisesRegex(ValidationError, "different configuration"):
                validate_workflow(workflow)


class CachedRunIntegrityTests(unittest.TestCase):
    def test_cached_success_rehashes_retained_outputs(self):
        from scisaurus.runtime.software_workbench import SoftwareWorkbench
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            binding = StubBinding(_profile())

            def runner(command, **kwargs):
                workspace = Path(kwargs["workspace"])
                (workspace / "result.json").write_text("{}")
                return SandboxResult(0, b'{"answer": 1}', b"", False, False, "sandbox-exec")

            workbench = SoftwareWorkbench(root / "wb", deadline=time.monotonic() + 120,
                                          laboratory=binding, runner=runner)
            arguments = {"runtime": "runtime", "source": "write_output\nprint('{}')",
                         "input": {}, "purpose": "scientific_computation",
                         "outputs": [{"name": "result.json"}], "documentation_refs": [],
                         "expected": None}
            with patch("scisaurus.runtime.software_workbench.sandbox_status",
                       return_value={"mode": "sandbox-exec"}):
                first = workbench.execute({"operation": "run", "arguments": arguments})
                self.assertEqual(first["outcome"], "ok", first)
                artifact = first["result"]["outputs"][0]
                reused = workbench.execute({"operation": "run", "arguments": arguments})
                self.assertTrue(reused["reused"])
                (root / "wb" / "artifacts" / artifact["sha256"]).write_text("TAMPERED")
                with self.assertRaisesRegex(ValidationError, "content changed"):
                    workbench.execute({"operation": "run", "arguments": arguments})


class DispatcherIntegrationTests(unittest.TestCase):
    def test_real_dispatcher_runs_laboratory_tool_and_reuses_cache(self):
        from scisaurus.runtime.models import ModelResult
        from scisaurus.runtime.specialists import SpecialistDispatcher
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            binding = StubBinding(_profile())
            tool_action = {"tool_action": {"operation": "run", "arguments": {
                "runtime": "runtime", "source": "write_output\nprint('{}')", "input": {"n": 1},
                "purpose": "scientific_computation", "outputs": [{"name": "result.json"}],
                "documentation_refs": [], "expected": None}}}
            final = {"decision": "pass", "summary": "tool loop complete", "findings": [],
                     "evidence_gaps": [], "requested_actions": []}

            def runner(command, **kwargs):
                workspace = Path(kwargs["workspace"])
                (workspace / "result.json").write_text("{}")
                return SandboxResult(0, b'{"answer": 1}', b"", False, False, "sandbox-exec")

            def dispatch_once():
                assignment = {"assigned_role": "methods.methodologist",
                              "model_role": "methods.methodologist", "role_id": "methodologist",
                              "task_id": "software-task", "stage_id": "software",
                              "_response_contract": "scientific_software_selection",
                              "_software_tools": True,
                              "_prompt": json.dumps({"software_assessment_request": {"evidence_catalog": []}}),
                              "quota": {"max_input_tokens": 24000, "max_output_tokens": 2000,
                                        "max_calls": 4, "max_seconds": 30}}
                model = {"protocol": "openai_compatible", "base_url": "http://fake/v1",
                         "model": "fake", "max_input_tokens": 24000, "max_output_tokens": 512,
                         "timeout_seconds": 5}
                reports = SpecialistDispatcher(
                    model, max_parallel=1, deadline=time.monotonic() + 30,
                    software_workspace=str(root / "software"),
                    software_laboratory=binding).dispatch([assignment], {})
                return reports[0]

            with patch("scisaurus.runtime.software_workbench.sandbox_status",
                       return_value={"mode": "sandbox-exec"}), \
                    patch("scisaurus.runtime.software_workbench.run_sandboxed", side_effect=runner):
                with patch("scisaurus.runtime.specialists.ModelClient") as client:
                    client.return_value.complete.side_effect = [
                        ModelResult(json.dumps(tool_action), "fake", {"model_calls": 1}, 0, "stop"),
                        ModelResult(json.dumps(final), "fake", {"model_calls": 1}, 0, "stop"),
                    ]
                    first = dispatch_once()
                with patch("scisaurus.runtime.specialists.ModelClient") as client:
                    client.return_value.complete.side_effect = [
                        ModelResult(json.dumps(tool_action), "fake", {"model_calls": 1}, 0, "stop"),
                        ModelResult(json.dumps(final), "fake", {"model_calls": 1}, 0, "stop"),
                    ]
                    second = dispatch_once()
            self.assertEqual(first["status"], "succeeded", first)
            receipts = first["software_tool_results"]
            self.assertEqual(receipts[0]["action"]["operation"], "run")
            self.assertFalse(receipts[0]["reused"])
            second_receipts = second["software_tool_results"]
            self.assertTrue(second_receipts[0]["reused"])


if __name__ == "__main__":
    unittest.main()


class AdditiveLaboratoryTests(unittest.TestCase):
    def test_append_retains_prior_runtime_and_contract(self):
        from scisaurus.runtime.laboratory import is_additive_laboratory_extension
        old=_profile(); new=deepcopy(old)
        extra=deepcopy(old["runtimes"][0]);extra["label"]="extra";new["runtimes"].append(extra)
        self.assertTrue(is_additive_laboratory_extension(old,new))
        for field,value in (("scope",{**new["scope"],"objective":"changed"}), ("limits",{**new["limits"],"max_output_bytes":1})):
            bad=deepcopy(new);bad[field]=value
            self.assertFalse(is_additive_laboratory_extension(old,bad))
        bad=deepcopy(new);bad["runtimes"][0]["description"]="changed"
        self.assertFalse(is_additive_laboratory_extension(old,bad))
        self.assertFalse(is_additive_laboratory_extension(old,old))

    def test_workflow_amendment_preserves_scientific_and_time_contract(self):
        from scisaurus.runtime.composer import ComposerRunner
        from types import SimpleNamespace
        from scisaurus.runtime.laboratory import laboratory_identity
        old=_profile();new=deepcopy(old);extra=deepcopy(old["runtimes"][0]);extra["label"]="extra";new["runtimes"].append(extra)
        def binding(path,attestation_path=None):
            profile=old if path=="old" else new
            return SimpleNamespace(laboratory=profile,identity=laboratory_identity(profile),attestation={"attestation_sha256":path})
        a={"revision":1,"project_id":"unchanged","stages":[{"id":"experiment"}],"time_policy":{"hard_seconds":100},"laboratory_config_path":"old","laboratory_config_sha256":laboratory_identity(old),"laboratory_attestation_path":"old","laboratory_attestation_sha256":"old"}
        b={**a,"revision":2,"laboratory_config_path":"new","laboratory_config_sha256":laboratory_identity(new),"laboratory_attestation_path":"new","laboratory_attestation_sha256":"new"}
        with patch("scisaurus.runtime.laboratory.LaboratoryBinding.load",side_effect=binding):
            self.assertTrue(ComposerRunner._is_laboratory_extension(a,b))
            for key,value in (("project_id","changed"),("time_policy",{"hard_seconds":101}),("stages",[]),("laboratory_config_sha256","0"*64)):
                self.assertFalse(ComposerRunner._is_laboratory_extension(a,{**b,key:value}))
