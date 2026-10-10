"""Offline regressions for sealed-laboratory topic feasibility contracts.

The laboratory adds a native execution boundary that must stay machine-checkable
and separate from both the host package inventory and the deterministic foundry
boundary.  These tests exercise the Composer's runtime context and the topic
feasibility gate through real runner instances and validated attestations; they
never call a model, network, solver or native runtime.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.composer import ComposerRunner, validate_workflow
from scisaurus.runtime.laboratory import (
    laboratory_identity, seal_attestation, validate_attestation,
)
from scisaurus.runtime.scientific_inputs import scientific_topic_sha256
from scisaurus.runtime.topic_discovery import validate_topic_feasibility
from scisaurus.tests.test_laboratory import _profile


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _sealed_attestation(laboratory, *, verified=True):
    """Build a content-addressed attestation without executing any runtime."""
    identity = laboratory_identity(laboratory)
    runtimes = []
    for runtime in laboratory["runtimes"]:
        roots = list(runtime["read_only_roots"])
        runtimes.append({
            "label": runtime["label"], "kind": runtime["kind"],
            "executable": runtime["executable"],
            "environment": deepcopy(runtime["environment"]),
            "environment_sha256": _sha(canonical_bytes(runtime["environment"])),
            "read_only_roots": list(runtime["read_only_roots"]),
            "read_roots_sha256": _sha(canonical_bytes(roots)),
            "executable_sha256": _sha(b"executable:" + runtime["label"].encode()),
            "inventory": {"kind": "none", "package_count": None, "sha256": None},
            "content_manifest": {"schema_version": "installed-runtime-content-2",
                                 "sha256": _sha(b"manifest:" + runtime["label"].encode()),
                                 "files": 1, "bytes": 1, "complete": True, "errors": []},
            "probe": {"status": "verified" if verified else "failed", "mode": "sandbox-exec",
                      "returncode": 0 if verified else 1, "stdout": "{}", "stderr": "",
                      "timed_out": False, "truncated": False, "elapsed_seconds": 0.01},
            "verified": verified,
        })
    body = {
        "schema_version": "metamaterial-laboratory-attestation-1",
        "laboratory_id": laboratory["id"],
        "config_sha256": identity,
        "created_epoch": 0.0,
        "host": {"platform": "test", "architecture": "test", "python": "3"},
        "isolation": {"runner": "run_sandboxed", "sandbox_exec_available": "/usr/bin/sandbox-exec",
                      "note": "offline regression fixture"},
        "runtimes": runtimes,
        "verified_labels": [row["label"] for row in runtimes if row["verified"]],
        "unverified_labels": [row["label"] for row in runtimes if not row["verified"]],
        "scientific_bound": False,
    }
    sealed = seal_attestation(body)
    validate_attestation(sealed)
    return sealed


def _plan(**overrides):
    value = {
        "execution_mode": "foundry", "experiment_input": "self_contained",
        "evidence_inputs": [{"kind": "synthetic", "status": "available",
                             "source": "seeded synthetic input"}],
        "data_access": "closed_world", "required_packages": [],
        "required_executables": [], "estimated_compute_seconds": 10,
        "estimated_api_requests": 0, "estimated_model_calls": 0,
        "network_access": False,
    }
    value.update(overrides)
    return value


def _candidate(plan, *, runtime_labels=("runtime",), package_labels=None):
    requirements = {"executables": [], "python_packages": [], "stage_kinds": []}
    if runtime_labels:
        requirements["runtime_labels"] = list(runtime_labels)
    return {
        "id": "direction_0", "evidence_mode": "synthetic_simulation",
        "capability_requirements": requirements,
        "feasibility_plan": plan,
    }


class _ComposerFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _stage_workflow(self, *, laboratory=None, attestation=None, foundry=True):
        stage = self.root / "stage.json"
        stage.write_text("{}")
        stage_dir = self.root / "stage-dir"
        stage_dir.mkdir()
        workflow = {
            "schema_version": "composer-workflow-1", "id": "lab-feasibility",
            "revision": 1, "project_id": str(self.root / "composer"),
            "objective": "exercise the sealed laboratory feasibility contract",
            "stages": [{"id": "survey", "kind": "survey",
                        "config_path": str(stage.resolve()),
                        "project_dir": str(stage_dir.resolve()), "depends_on": [],
                        "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                        "reuse_completed": False, "reuse_output_path": None}],
            "time_policy": {"first_result_seconds": 1, "target_seconds": 10,
                            "hard_seconds": 30, "checkpoint_seconds": 1},
            "completion": {"required_stage_ids": ["survey"], "release_requires_human": True},
        }
        if laboratory is not None:
            laboratory_path = self.root / "laboratory.json"
            laboratory_path.write_text(json.dumps(laboratory))
            workflow["laboratory_config_path"] = str(laboratory_path.resolve())
            workflow["laboratory_config_sha256"] = laboratory_identity(laboratory)
            if attestation is not None:
                attestation_path = self.root / "attestation.json"
                attestation_path.write_text(json.dumps(attestation))
                workflow["laboratory_attestation_path"] = str(attestation_path.resolve())
                workflow["laboratory_attestation_sha256"] = attestation["attestation_sha256"]
        if foundry:
            model = self.root / "model.json"
            model.write_text(json.dumps({"model": "fake", "protocol": "ollama",
                                         "base_url": "http://example.invalid",
                                         "timeout_seconds": 1, "max_output_tokens": 4096}))
            requirements = self.root / "requirements.txt"
            requirements.write_text("numpy==2.5.2\n")
            foundry_path = self.root / "foundry.json"
            foundry_path.write_text(json.dumps({
                "schema_version": "capability-foundry-config-1",
                "model_config_path": str(model.resolve()),
                "runtime_python": str(Path(sys.executable).resolve()),
                "workspace_root": str(self.root / "foundry-workspace"),
                "registry_root": str(self.root / "registry"),
                "repo_root": str(self.root),
                "requirements_file": str(requirements.resolve()),
                "max_attempts": 2, "timeout_seconds": 30,
                "runtime_packages": [{"name": "numpy", "version": "2.5.2"}],
            }))
            workflow["capability_foundry_config_path"] = str(foundry_path.resolve())
        validate_workflow(workflow)
        return workflow

    def _lab_context(self, laboratory, *, verified=True, extra=None):
        attestation = _sealed_attestation(laboratory, verified=verified)
        workflow = self._stage_workflow(laboratory=laboratory, attestation=attestation)
        runner = ComposerRunner(workflow)
        self.addCleanup(runner.close)
        context = runner._runtime_context({"protocol": "openai", "model": "fake"})
        if extra:
            context.update(extra)
        return runner, context


class SealedLaboratoryContextTests(_ComposerFixture):
    def test_sealed_lab_publishes_native_runtime_and_project_inputs(self):
        laboratory = _profile()
        _, context = self._lab_context(laboratory)
        feasibility = context["research_feasibility"]
        self.assertIn("native_runtime", feasibility["execution_modes"])
        self.assertIn("project_artifact", feasibility["allowed_input_kinds"])
        self.assertIn("project_local", feasibility["allowed_data_access"])
        self.assertEqual(feasibility["runtime_labels"], ["runtime"])
        self.assertEqual(feasibility["attested_runtime_labels"], ["runtime"])
        self.assertEqual(feasibility["laboratory_config_sha256"],
                         laboratory_identity(laboratory))
        self.assertTrue(context["laboratory_feasibility"]["sealed"])
        self.assertEqual(
            context["laboratory_feasibility"]["attestation_sha256"],
            context["research_feasibility"]["laboratory_attestation_sha256"])

    def test_lab_runtime_packages_are_not_flattened_into_foundry_inventory(self):
        laboratory = _profile()
        laboratory["runtimes"][0]["capabilities"] = ["lab-private-package"]
        laboratory["runtimes"][0]["probe_modules"] = ["lab_private_module"]
        _, context = self._lab_context(laboratory)
        feasibility = context["research_feasibility"]
        self.assertIn("lab-private-package",
                      feasibility["runtime_capabilities"]["runtime"])
        self.assertNotIn("lab-private-package", context["python_packages"])
        self.assertNotIn("lab-private-package",
                         [row["name"] for row in
                          context["capability_foundry"].get("runtime_packages", [])])
        self.assertNotIn("lab-private-package", feasibility["available_packages"])
        self.assertEqual(feasibility["foundry_runtime_packages"], ["numpy"])

    def test_native_plan_with_attested_runtime_is_admitted(self):
        laboratory = _profile()
        laboratory["runtimes"][0]["capabilities"] = ["lab-private-package"]
        _, context = self._lab_context(laboratory)
        candidate = _candidate(_plan(
            execution_mode="native_runtime", experiment_input="project_artifact",
            data_access="project_local",
            evidence_inputs=[{"kind": "project_artifact",
                              "status": "acquirable_before_experiment",
                              "source": "generated by the attested runtime"},
                             {"kind": "synthetic", "status": "available", "source": "simulation parameters"}],
            required_packages=["numpy"],
            runtime_labels=["runtime"]))
        result = validate_topic_feasibility(
            {"candidates": [candidate], "selected_id": "direction_0"}, context)
        self.assertEqual(result["status"], "provisional_for_survey")
        self.assertFalse(result["execution_ready"])
        self.assertEqual(result["input_readiness"][0]["status"], "unverified")

    def test_project_artifact_without_generator_or_materialization_is_rejected(self):
        laboratory = _profile()
        _, context = self._lab_context(laboratory)
        candidate = _candidate(
            _plan(execution_mode="native_runtime", experiment_input="project_artifact",
                  data_access="project_local",
                  evidence_inputs=[{"kind": "project_artifact", "status": "available",
                                    "source": "an unnamed local artifact"}]),
            runtime_labels=())
        with self.assertRaisesRegex(ValidationError, "project_artifact"):
            validate_topic_feasibility(
                {"candidates": [candidate], "selected_id": "direction_0"}, context)

    def test_undeclared_runtime_label_is_rejected(self):
        laboratory = _profile()
        _, context = self._lab_context(laboratory)
        candidate = _candidate(_plan(
            execution_mode="native_runtime", experiment_input="project_artifact",
            data_access="project_local",
            evidence_inputs=[{"kind": "project_artifact",
                              "status": "acquirable_before_experiment",
                              "source": "generated"},
                             {"kind": "synthetic", "status": "available", "source": "simulation parameters"}],
            runtime_labels=["ghost-runtime"]), runtime_labels=())
        with self.assertRaisesRegex(ValidationError, "undeclared"):
            validate_topic_feasibility(
                {"candidates": [candidate], "selected_id": "direction_0"}, context)

    def test_unattested_runtime_label_is_rejected(self):
        laboratory = _profile()
        _, context = self._lab_context(laboratory, verified=False)
        self.assertEqual(context["research_feasibility"]["attested_runtime_labels"], [])
        candidate = _candidate(_plan(
            execution_mode="native_runtime", experiment_input="project_artifact",
            data_access="project_local",
            evidence_inputs=[{"kind": "project_artifact",
                              "status": "acquirable_before_experiment",
                              "source": "generated"},
                             {"kind": "synthetic", "status": "available", "source": "simulation parameters"}],
            runtime_labels=["runtime"]), runtime_labels=())
        with self.assertRaisesRegex(ValidationError, "unattested"):
            validate_topic_feasibility(
                {"candidates": [candidate], "selected_id": "direction_0"}, context)

    def test_required_package_must_match_runtime_capabilities(self):
        laboratory = _profile()
        _, context = self._lab_context(laboratory)
        candidate = _candidate(_plan(
            execution_mode="native_runtime", experiment_input="project_artifact",
            data_access="project_local",
            evidence_inputs=[{"kind": "project_artifact",
                              "status": "acquirable_before_experiment",
                              "source": "generated"},
                             {"kind": "synthetic", "status": "available", "source": "simulation parameters"}],
            required_packages=["package-that-no-runtime-declares"],
            runtime_labels=["runtime"]))
        with self.assertRaisesRegex(ValidationError, "required_packages"):
            validate_topic_feasibility(
                {"candidates": [candidate], "selected_id": "direction_0"}, context)

    def test_materialized_project_artifact_satisfies_readiness(self):
        laboratory = _profile()
        _, context = self._lab_context(laboratory)
        candidate = _candidate(_plan(
            execution_mode="native_runtime", experiment_input="project_artifact",
            data_access="project_local",
            evidence_inputs=[{
                "kind": "project_artifact", "status": "available",
                "source": "retained native solver artifact",
                "artifact_refs": ["artifact:kb/scientific-inputs/native@1"]},
                {"kind": "synthetic", "status": "available", "source": "simulation parameters"}],
            runtime_labels=["runtime"]))
        inventory = [{
            "artifact_ref": "artifact:kb/scientific-inputs/native@1",
            "body_sha256": "a" * 64, "topic_id": candidate["id"],
            "topic_sha256": scientific_topic_sha256(candidate),
            "kind": "project_artifact", "payload": {"field": [1.0]},
            "source_refs": ["evidence:1"],
        }]
        context = {**context, "scientific_input_artifacts": inventory}
        result = validate_topic_feasibility(
            {"candidates": [candidate], "selected_id": "direction_0"}, context)
        self.assertEqual(result["status"], "feasible")
        self.assertFalse(result["execution_ready"])
        self.assertEqual(result["input_readiness"][0]["status"], "verified")

    def test_native_synthetic_intermediates_require_generation(self):
        _, context = self._lab_context(_profile())
        candidate = _candidate(_plan(execution_mode="native_runtime",
            runtime_labels=["runtime"], required_packages=["numpy"]))
        result = validate_topic_feasibility(
            {"candidates": [candidate], "selected_id": "direction_0"}, context)
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["input_readiness"][0]["status"], "generation_required")
        self.assertFalse(result["execution_ready"])

    def test_host_package_does_not_authorize_foundry_import(self):
        _, context = self._lab_context(_profile())
        context["python_packages"]["host_only"] = True
        context["research_feasibility"]["available_packages"].append("host_only")
        for in_requirements in (True, False):
            candidate = _candidate(_plan(required_packages=[] if in_requirements else ["host_only"]))
            if in_requirements:
                candidate["capability_requirements"]["python_packages"] = ["host_only"]
            with self.subTest(in_requirements=in_requirements), self.assertRaises(ValidationError):
                validate_topic_feasibility(
                    {"candidates": [candidate], "selected_id": "direction_0"}, context)

    def test_native_capability_cannot_be_imported_by_analysis(self):
        laboratory = _profile()
        laboratory["runtimes"][0]["capabilities"] = ["meep"]
        _, context = self._lab_context(laboratory)
        candidate = _candidate(_plan(execution_mode="native_runtime",
            runtime_labels=["runtime"], required_packages=["meep"]))
        with self.assertRaisesRegex(ValidationError, "required_packages"):
            validate_topic_feasibility(
                {"candidates": [candidate], "selected_id": "direction_0"}, context)

    def test_mixed_inputs_do_not_become_self_contained(self):
        _, context = self._lab_context(_profile())
        candidate = _candidate(_plan(experiment_input="project_artifact", data_access="project_local",
            evidence_inputs=[{"kind": "analytical_parameters", "status": "available", "source": "design"},
                             {"kind": "project_artifact", "status": "available", "source": "required input"}]))
        candidate["evidence_mode"] = "analytical_derivation"
        package = {"candidates": [candidate], "selected_id": "direction_0"}
        result = validate_topic_feasibility(package, context)
        self.assertEqual(package["candidates"][0]["feasibility_plan"]["experiment_input"], "project_artifact")
        self.assertEqual(result["status"], "provisional_for_survey")

    def test_diagnostic_attestation_does_not_authorize_native_execution(self):
        laboratory = _profile()
        attestation = _sealed_attestation(laboratory)
        attestation["isolation"]["runner"] = "direct_runner"
        attestation = seal_attestation(attestation)
        workflow = self._stage_workflow(laboratory=laboratory, attestation=attestation)
        runner = ComposerRunner(workflow)
        self.addCleanup(runner.close)
        context = runner._runtime_context({"protocol": "openai", "model": "fake"})
        self.assertNotIn("native_runtime", context["research_feasibility"]["execution_modes"])
        self.assertFalse(context["laboratory_feasibility"]["sealed"])


class UnboundLaboratoryLegacyTests(_ComposerFixture):
    def test_unbound_workflow_keeps_the_legacy_foundry_boundary(self):
        workflow = self._stage_workflow(foundry=True)
        runner = ComposerRunner(workflow)
        self.addCleanup(runner.close)
        context = runner._runtime_context({"protocol": "openai", "model": "fake"})
        feasibility = context["research_feasibility"]
        self.assertEqual(feasibility["execution_modes"], ["foundry"])
        self.assertEqual(feasibility["allowed_input_kinds"],
                         ["analytical_parameters", "synthetic"])
        self.assertNotIn("native_runtime", feasibility["execution_modes"])
        self.assertNotIn("project_artifact", feasibility["allowed_input_kinds"])
        self.assertEqual(feasibility["runtime_labels"], [])
        self.assertEqual(feasibility["attested_runtime_labels"], [])
        self.assertIsNone(context["laboratory_feasibility"])
        # A foundry-only mission cannot name a laboratory runtime label.
        candidate = _candidate(_plan(runtime_labels=["runtime"]),
                               runtime_labels=())
        with self.assertRaisesRegex(ValidationError, "undeclared"):
            validate_topic_feasibility(
                {"candidates": [candidate], "selected_id": "direction_0"}, context)
        # The plain self-contained foundry plan still passes.
        plain = _candidate(_plan(), runtime_labels=())
        result = validate_topic_feasibility(
            {"candidates": [plain], "selected_id": "direction_0"}, context)
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["checks"]["scientific_inputs"], "generation_required")


if __name__ == "__main__":
    unittest.main()
