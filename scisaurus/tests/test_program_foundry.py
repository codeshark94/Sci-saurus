import base64
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_registry import (
    TRANSACTION_SCHEMA, experiment_program_payload, load_registry, register_capability,
)
from scisaurus.runtime.program_admission import SCHEMA_VERSION, validate_program_candidate
from scisaurus.runtime.program_gates import admit_program_candidate
from scisaurus.runtime.program_sandbox import (
    _macho_dependency_paths, run_sandboxed, sandbox_profile, sandbox_status,
)
from scisaurus.runtime.programs import LocalProgramClient
from scisaurus.tests.test_program_admission import EXECUTOR, INTENT, VALIDATOR

REPO_ROOT = Path(__file__).resolve().parents[2]


class Result:
    def __init__(self, stdout=b"", returncode=0, timed_out=False, truncated=False, mode="sandbox-exec"):
        self.stdout, self.returncode = stdout, returncode
        self.timed_out, self.truncated, self.mode = timed_out, truncated, mode


def candidate():
    return {
        "schema_version": SCHEMA_VERSION, "study_id": "generated_study", "revision": 1,
        "executor_source": EXECUTOR, "validator_source": VALIDATOR,
        "runtime": {"python": "3.14", "packages": [{"name": "numpy", "version": "2.5.2"}]},
        "test_vector": {"input": {"probe": True}, "expected_output_sha256": "0" * 64},
        "experiment_intent": INTENT,
    }


def expected_digest(document):
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def output_document():
    return {
        "schema_version": "experiment-program-output-1", "study_id": "generated_study",
        "revision": 1,
        "procedures": [{"id": "protocol", "description": INTENT["method"],
                        "source": "generated executor"}],
        "observations": [{"replicate": index + 1, "abs_error": 1.0}
                         for index in range(INTENT["run_count"])],
        "metrics": [{"id": "tail_error", "value": 1.0, "unit": "error",
                     "conditions": "declared fixture", "source": "observations",
                     "presentation": "Tail error is 1.0."}],
        "findings": [{"id": "tail_summary", "statement": "Tail error is 1.0.",
                      "metric_ids": ["tail_error"]}],
        "limitations": list(INTENT["limitations"]),
        "assets": [{"id": "figure_a", "path": "figure_a.png", "sha256": "a" * 64,
                    "role": "figure", "media_type": "image/png", "caption": "Fixture."}],
    }


class SandboxTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("otool"),
                         "Mach-O dependency inspection is only available on macOS")
    def test_profile_includes_actual_runtime_dependencies(self):
        with tempfile.TemporaryDirectory() as path:
            workspace = Path(path)
            executable = Path(sys.executable).resolve()
            dependencies = _macho_dependency_paths(executable)
            self.assertTrue(dependencies)
            profile = sandbox_profile(workspace, [sys.executable, "-c", "print('ok')"])
            for dependency in dependencies:
                self.assertIn(f'(literal "{dependency}")', profile)

    def test_deny_by_default_profile_blocks_network_and_outside_writes(self):
        self.assertEqual(sandbox_status()["mode"], "sandbox-exec")
        with tempfile.TemporaryDirectory() as path:
            workspace = Path(path)
            benign = run_sandboxed([sys.executable, "-c", "print('ok')"], workspace=workspace,
                                   timeout_seconds=30)
            self.assertEqual(benign.returncode, 0)
            self.assertIn(b"ok", benign.stdout)
            network = run_sandboxed(
                [sys.executable, "-c", "import socket; socket.create_connection(('1.1.1.1', 80), 2)"],
                workspace=workspace, timeout_seconds=30)
            self.assertNotEqual(network.returncode, 0)
            outside = run_sandboxed(
                [sys.executable, "-c", "open('/etc/scisaurus_probe', 'w').write('x')"],
                workspace=workspace, timeout_seconds=30)
            self.assertNotEqual(outside.returncode, 0)
            self.assertFalse(Path("/etc/scisaurus_probe").exists())

    def test_wall_deadline_is_enforced(self):
        with tempfile.TemporaryDirectory() as path:
            result = run_sandboxed([sys.executable, "-c", "import time; time.sleep(60)"],
                                   workspace=Path(path), timeout_seconds=3, cpu_seconds=60)
            self.assertTrue(result.timed_out)
            self.assertNotEqual(result.returncode, 0)

    def test_required_runtime_client_keeps_generated_code_inside_sandbox(self):
        with tempfile.TemporaryDirectory() as path:
            workspace = Path(path)
            outside = workspace.parent / (workspace.name + "-outside")
            program = workspace / "program.py"
            program.write_text(
                "import json,sys\n"
                f"open({str(outside)!r}, 'w').write('escaped')\n"
                "print(json.dumps({'ok': True}))\n")
            client = LocalProgramClient(
                [sys.executable, str(program)], timeout=10, max_bytes=100000,
                cwd=str(workspace.resolve()), env={}, sandbox_required=True,
            )
            result = client.run({"probe": True})
            self.assertNotEqual(result["outcome"], "ok")
            self.assertEqual(result["metadata"]["sandbox_mode"], "sandbox-exec")
            self.assertFalse(outside.exists())

    def test_required_runtime_client_cannot_read_a_sibling_secret(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workspace = root / "workspace"
            workspace.mkdir()
            secret = root / "secret.txt"
            secret.write_text("private-fixture-value")
            program = workspace / "program.py"
            program.write_text(
                "import json,sys\nfrom pathlib import Path\n"
                f"value=Path({str(secret)!r}).read_text()\n"
                "print(json.dumps({'value': value}))\n")
            result = LocalProgramClient(
                [sys.executable, str(program)], timeout=10, max_bytes=100000,
                cwd=str(workspace.resolve()), env={}, sandbox_required=True,
            ).run({"probe": True})
            self.assertNotEqual(result["outcome"], "ok")
            self.assertNotIn("private-fixture-value", result["text"])
            self.assertNotIn(
                b"private-fixture-value", base64.b64decode(result["capture"]["body"]),
            )
            self.assertNotIn(
                b"private-fixture-value", base64.b64decode(result["stderr_capture"]["body"]),
            )


class GateTests(unittest.TestCase):
    def _execute(self, document):
        payload = json.dumps(document).encode()
        return lambda _: Result(payload)

    def _validate(self, decision=None, matches=True):
        def validate(data):
            request = json.loads(data)
            reported = request["candidate"]["metrics"][0]["value"]
            observed_decision = decision or ("accepted" if matches else "rejected")
            check_outcome = "passed" if observed_decision == "accepted" else "failed"
            verdict = {
                "schema_version": "experiment-validation-1", "study_id": "generated_study",
                "candidate_sha256": request["candidate_sha256"], "decision": observed_decision,
                "checks": [{"id": "row_arithmetic", "outcome": check_outcome,
                            "evidence": "recalculated from observations"}],
                "metric_recalculations": [{"metric_id": "tail_error",
                    "reported_value": reported,
                    "recalculated_value": reported if matches else reported + 1.0,
                    "tolerance": 1e-9, "matches": matches}],
                "limitations": ["bounded"],
            }
            return Result(json.dumps(verdict).encode())
        return validate

    def test_admitted_candidate_passes_every_gate(self):
        document = output_document()
        payload = json.dumps(document).encode()
        value = candidate()
        value["test_vector"]["expected_output_sha256"] = expected_digest(document)
        record = admit_program_candidate(value, execute=lambda _: Result(payload),
                                         validate=self._validate(),
                                         review=lambda *a: {"status": "admitted", "findings": []})
        self.assertEqual(record["gates"], ["static_scan", "deterministic_replay",
                                           "test_vector_digest", "independent_recalculation",
                                           "adversarial_review"])
        self.assertEqual(record["independent_recalculation"]["metrics"], 1)

    def test_non_deterministic_replay_is_rejected(self):
        state = {"index": 0}

        def execute(_):
            state["index"] += 1
            return Result(json.dumps({"n": state["index"]}).encode())

        with self.assertRaisesRegex(ValidationError, "non-identical"):
            admit_program_candidate(candidate(), execute=execute, validate=self._validate())

    def test_digest_mismatch_is_rejected(self):
        value = candidate()
        document = output_document()
        value["test_vector"]["expected_output_sha256"] = hashlib.sha256(b"other").hexdigest()
        with self.assertRaisesRegex(ValidationError, "test-vector digest"):
            admit_program_candidate(value, execute=self._execute(document), validate=self._validate())

    def test_validator_rejection_is_rejected(self):
        document = output_document()
        payload = json.dumps(document).encode()
        value = candidate()
        value["test_vector"]["expected_output_sha256"] = expected_digest(document)
        with self.assertRaisesRegex(ValidationError, "did not accept"):
            admit_program_candidate(value, execute=lambda _: Result(payload),
                                    validate=self._validate(decision="rejected"))

    def test_failed_check_is_rejected(self):
        document = output_document()
        payload = json.dumps(document).encode()
        value = candidate()
        value["test_vector"]["expected_output_sha256"] = expected_digest(document)
        with self.assertRaisesRegex(ValidationError, "contradicts"):
            admit_program_candidate(value, execute=lambda _: Result(payload),
                                    validate=self._validate(decision="accepted", matches=False))

    def test_blocking_review_finding_is_rejected(self):
        document = output_document()
        payload = json.dumps(document).encode()
        value = candidate()
        value["test_vector"]["expected_output_sha256"] = expected_digest(document)
        with self.assertRaisesRegex(ValidationError, "review rejected"):
            admit_program_candidate(value, execute=lambda _: Result(payload), validate=self._validate(),
                                    review=lambda *a: {"status": "admitted",
                                                       "findings": [{"severity": "blocking"}]})


class RegistryTests(unittest.TestCase):
    @staticmethod
    def _candidate():
        value = candidate()
        value["test_vector"]["input"] = experiment_program_payload(
            value["experiment_intent"], {"probe": True})
        return value

    def _admission(self, value):
        document = output_document()
        payload = json.dumps(document).encode()
        value["test_vector"]["expected_output_sha256"] = expected_digest(document)
        return admit_program_candidate(
            value, execute=lambda _: Result(payload),
            validate=GateTests()._validate(),
            readiness=lambda: Result(b'{"status":"ready"}'))

    def test_admitted_candidate_is_pinned_and_indexed(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            value = self._candidate()
            admission = self._admission(value)
            result = register_capability(root, value, admission,
                                         runtime_python=sys.executable,
                                         repo_root=REPO_ROOT)
            descriptor = json.loads(Path(result["descriptor_path"]).read_text())
            self.assertEqual(descriptor["capability_id"], "generated_study")
            self.assertEqual(descriptor["schema_version"], "experiment-capability-1")
            command = descriptor["experiment"]["execution"]["client"]["command"]
            self.assertTrue(Path(command[1]).is_file())
            self.assertEqual(descriptor["experiment"]["execution"]["input"], {"probe": True})
            self.assertEqual(descriptor["experiment"]["execution"]["representative"]["input"],
                             value["test_vector"]["input"])
            self.assertTrue(descriptor["experiment"]["execution"]["client"]["sandbox_required"])
            self.assertTrue(Path(descriptor["experiment"]["validation"]["client"]["command"][1]).is_file())
            index = load_registry(root)
            self.assertEqual(len(index["capabilities"]), 1)
            from scisaurus.core.schema import canonical_bytes
            self.assertEqual(index["capabilities"][0]["descriptor_sha256"],
                             hashlib.sha256(canonical_bytes(descriptor)).hexdigest())
            self.assertEqual(index["capabilities"][0]["candidate_record_sha256"],
                             admission["candidate_record_sha256"])

    def test_registry_load_rejects_source_tampering(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            value = self._candidate()
            admission = self._admission(value)
            result = register_capability(root, value, admission, runtime_python=sys.executable,
                                         repo_root=REPO_ROOT)
            descriptor = json.loads(Path(result["descriptor_path"]).read_text())
            Path(descriptor["experiment"]["execution"]["client"]["command"][1]).write_text(
                value["executor_source"] + "\n# tampered\n")
            with self.assertRaisesRegex(ValidationError, "source integrity"):
                load_registry(root)

    def test_registry_journal_recovers_revision_published_before_index(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            value = self._candidate()
            admission = self._admission(value)
            register_capability(root, value, admission, runtime_python=sys.executable,
                                repo_root=REPO_ROOT)
            index_path = root / "capabilities" / "index.json"
            entry = json.loads(index_path.read_text())["capabilities"][0]
            index_path.write_bytes(canonical_bytes({
                "schema_version": "experiment-capability-registry-1", "capabilities": []}))
            revision = Path(entry["path"]).parent
            journal = {
                "schema_version": TRANSACTION_SCHEMA, "entry": entry,
                "revision_path": str(revision),
                "temporary_path": str(revision.with_name(".r1.crash.tmp")),
            }
            journal_path = root / "capabilities" / ".registry-transaction.json"
            journal_path.write_bytes(canonical_bytes(journal))
            recovered = load_registry(root)
            self.assertEqual(recovered["capabilities"], [entry])
            self.assertFalse(journal_path.exists())

    def test_revisions_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            value = self._candidate()
            admission = self._admission(value)
            register_capability(root, value, admission, runtime_python=sys.executable,
                                repo_root=REPO_ROOT)
            with self.assertRaisesRegex(ValidationError, "never overwritten"):
                register_capability(root, value, admission, runtime_python=sys.executable,
                                    repo_root=REPO_ROOT)

    def test_admission_must_match_supplied_source(self):
        with tempfile.TemporaryDirectory() as path:
            value = self._candidate()
            admission = self._admission(value)
            tampered = dict(value)
            tampered["executor_source"] = value["executor_source"] + "\n# changed\n"
            with self.assertRaisesRegex(ValidationError, "does not match the supplied source"):
                register_capability(Path(path), tampered, admission, runtime_python=sys.executable,
                                    repo_root=REPO_ROOT)

    def test_admission_binds_test_input_and_complete_intent(self):
        with tempfile.TemporaryDirectory() as path:
            value = self._candidate()
            admission = self._admission(value)
            tampered = json.loads(json.dumps(value))
            tampered["test_vector"]["input"]["configured_input"]["probe"] = False
            with self.assertRaisesRegex(ValidationError, "complete program candidate"):
                register_capability(Path(path), tampered, admission,
                                    runtime_python=sys.executable, repo_root=REPO_ROOT)

    def test_registry_rejects_non_runtime_test_envelope(self):
        with tempfile.TemporaryDirectory() as path:
            value = candidate()
            admission = self._admission(value)
            with self.assertRaisesRegex(ValidationError, "ExperimentRunner execution envelope"):
                register_capability(Path(path), value, admission,
                                    runtime_python=sys.executable, repo_root=REPO_ROOT)
            self.assertEqual(load_registry(Path(path))["capabilities"], [])

    def test_failed_registration_removes_its_unindexed_revision(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            value = self._candidate()
            admission = self._admission(value)
            with patch("scisaurus.runtime.capability_registry.validate_experiment_config",
                       side_effect=ValidationError("invalid generated descriptor")):
                with self.assertRaisesRegex(ValidationError, "invalid generated descriptor"):
                    register_capability(root, value, admission, runtime_python=sys.executable,
                                        repo_root=REPO_ROOT)
            self.assertFalse(root.joinpath("capabilities", "generated_study", "r1").exists())
            self.assertEqual(load_registry(root)["capabilities"], [])


if __name__ == "__main__":
    unittest.main()
