import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_registry import load_registry, register_capability
from scisaurus.runtime.program_admission import SCHEMA_VERSION, validate_program_candidate
from scisaurus.runtime.program_gates import admit_program_candidate
from scisaurus.runtime.program_sandbox import run_sandboxed, sandbox_status
from scisaurus.tests.test_program_admission import EXECUTOR, INTENT, VALIDATOR


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
    return {"schema_version": "experiment-program-output-1", "study_id": "generated_study", "revision": 1}
    # noqa: E501 - kept small; gates only read the identity fields


class SandboxTests(unittest.TestCase):
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


class GateTests(unittest.TestCase):
    def _execute(self, document):
        payload = json.dumps(document).encode()
        return lambda _: Result(payload)

    def _validate(self, decision="accepted", matches=True):
        verdict = json.dumps({
            "schema_version": "experiment-validation-1", "study_id": "generated_study",
            "candidate_sha256": "0" * 64, "decision": decision,
            "checks": [{"id": "row_arithmetic", "outcome": "passed", "evidence": "ok"}],
            "metric_recalculations": [{"metric_id": "tail_error", "reported_value": 1.0,
                                       "recalculated_value": 1.0, "tolerance": 1e-9, "matches": matches}],
            "limitations": ["bounded"]}).encode()
        return lambda _: Result(verdict)

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
        document = {"schema_version": "experiment-program-output-1", "study_id": "generated_study", "revision": 1}
        value["test_vector"]["expected_output_sha256"] = hashlib.sha256(b"other").hexdigest()
        with self.assertRaisesRegex(ValidationError, "test-vector digest"):
            admit_program_candidate(value, execute=self._execute(document), validate=self._validate())

    def test_validator_rejection_is_rejected(self):
        document = {"schema_version": "experiment-program-output-1", "study_id": "generated_study", "revision": 1}
        payload = json.dumps(document).encode()
        value = candidate()
        value["test_vector"]["expected_output_sha256"] = expected_digest(document)
        with self.assertRaisesRegex(ValidationError, "did not accept"):
            admit_program_candidate(value, execute=lambda _: Result(payload),
                                    validate=self._validate(decision="rejected"))

    def test_failed_check_is_rejected(self):
        document = {"schema_version": "experiment-program-output-1", "study_id": "generated_study", "revision": 1}
        payload = json.dumps(document).encode()
        value = candidate()
        value["test_vector"]["expected_output_sha256"] = expected_digest(document)
        with self.assertRaisesRegex(ValidationError, "did not match every"):
            admit_program_candidate(value, execute=lambda _: Result(payload),
                                    validate=self._validate(matches=False))

    def test_blocking_review_finding_is_rejected(self):
        document = {"schema_version": "experiment-program-output-1", "study_id": "generated_study", "revision": 1}
        payload = json.dumps(document).encode()
        value = candidate()
        value["test_vector"]["expected_output_sha256"] = expected_digest(document)
        with self.assertRaisesRegex(ValidationError, "review rejected"):
            admit_program_candidate(value, execute=lambda _: Result(payload), validate=self._validate(),
                                    review=lambda *a: {"status": "admitted",
                                                       "findings": [{"severity": "blocking"}]})


class RegistryTests(unittest.TestCase):
    def _admission(self, value):
        document = {"schema_version": "experiment-program-output-1",
                    "study_id": "generated_study", "revision": 1}
        payload = json.dumps(document).encode()
        value["test_vector"]["expected_output_sha256"] = expected_digest(document)
        return admit_program_candidate(value, execute=lambda _: Result(payload), validate=lambda _: Result(
            json.dumps({"schema_version": "experiment-validation-1", "study_id": "generated_study",
                        "candidate_sha256": "0" * 64, "decision": "accepted",
                        "checks": [{"id": "row_arithmetic", "outcome": "passed", "evidence": "ok"}],
                        "metric_recalculations": [{"metric_id": "tail_error", "reported_value": 1.0,
                                                   "recalculated_value": 1.0, "tolerance": 1e-9,
                                                   "matches": True}],
                        "limitations": ["bounded"]}).encode()))

    def test_admitted_candidate_is_pinned_and_indexed(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            value = candidate()
            admission = self._admission(value)
            result = register_capability(root, value, admission,
                                         runtime_python=sys.executable,
                                         repo_root=Path("/Users/seungyeop/Sci-saurus"))
            descriptor = json.loads(Path(result["descriptor_path"]).read_text())
            self.assertEqual(descriptor["capability_id"], "generated_study")
            self.assertEqual(descriptor["schema_version"], "experiment-capability-1")
            command = descriptor["experiment"]["execution"]["client"]["command"]
            self.assertTrue(Path(command[1]).is_file())
            self.assertTrue(Path(descriptor["experiment"]["validation"]["client"]["command"][1]).is_file())
            index = load_registry(root)
            self.assertEqual(len(index["capabilities"]), 1)
            from scisaurus.core.schema import canonical_bytes
            self.assertEqual(index["capabilities"][0]["descriptor_sha256"],
                             hashlib.sha256(canonical_bytes(descriptor)).hexdigest())

    def test_revisions_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            value = candidate()
            admission = self._admission(value)
            register_capability(root, value, admission, runtime_python=sys.executable,
                                repo_root=Path("/Users/seungyeop/Sci-saurus"))
            with self.assertRaisesRegex(ValidationError, "never overwritten"):
                register_capability(root, value, admission, runtime_python=sys.executable,
                                    repo_root=Path("/Users/seungyeop/Sci-saurus"))

    def test_admission_must_match_supplied_source(self):
        with tempfile.TemporaryDirectory() as path:
            value = candidate()
            admission = self._admission(value)
            tampered = dict(value)
            tampered["executor_source"] = value["executor_source"] + "\n# changed\n"
            with self.assertRaisesRegex(ValidationError, "does not match the supplied source"):
                register_capability(Path(path), tampered, admission, runtime_python=sys.executable,
                                    repo_root=Path("/Users/seungyeop/Sci-saurus"))


if __name__ == "__main__":
    unittest.main()
