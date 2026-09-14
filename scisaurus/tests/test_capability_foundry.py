import json
import sys
import tempfile
import unittest
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.capability_foundry import CapabilityFoundry
from scisaurus.runtime.capability_registry import load_registry
from scisaurus.runtime.experiment import ExperimentRunner
from scisaurus.runtime.models import ModelResult
from scisaurus.tests.test_experiment import fixture_worker
from scisaurus.tests.test_program_admission import INTENT

ROOT = Path(__file__).resolve().parents[2]

MINI_EXECUTOR = '''
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path


def png(width, height, rgb):
    raw = b"".join(b"\\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    request = json.load(sys.stdin)
    experiment = request["experiment"]
    run_count = int(experiment["run_count"])
    seed = int(experiment["seed"])
    state = seed
    errors, observations = [], []
    for index in range(run_count):
        state = (1103515245 * state + 12345) % (2 ** 31)
        value = abs(state / (2 ** 31) - 0.5)
        errors.append(value)
        observations.append({"replicate": index + 1, "estimate": value, "true_value": 0.0, "abs_error": value})
    ordered = sorted(errors)
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    p95 = ordered[lower] * (1 - fraction) + ordered[upper] * fraction
    metrics = [{"id": "tail_error", "value": p95, "unit": "error", "conditions": "declared seed",
                "source": "engine", "presentation": "95th-percentile absolute error is %.6g." % p95}]
    findings = [{"id": "tail_summary", "metric_ids": ["tail_error"],
                 "statement": "The declared estimator has 95th-percentile absolute error %.6g." % p95}]
    assets = []
    for asset_id, colour in (("figure_a", (200, 30, 30)), ("figure_b", (30, 200, 30)),
                             ("figure_c", (30, 30, 200))):
        body = png(2, 2, colour)
        name = asset_id + ".png"
        Path(name).write_bytes(body)
        assets.append({"id": asset_id, "path": name, "sha256": hashlib.sha256(body).hexdigest(),
                       "role": "figure", "media_type": "image/png", "caption": "Declared figure %s." % asset_id})
    result = {"schema_version": "experiment-program-output-1", "study_id": experiment["id"],
              "revision": experiment["revision"],
              "procedures": [{"id": "protocol", "description": experiment["method"], "source": "generated program"}],
              "observations": observations, "metrics": metrics, "findings": findings,
              "limitations": list(experiment["limitations"]), "assets": assets}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
'''

MINI_VALIDATOR = '''
import json
import sys


def main():
    request = json.load(sys.stdin)
    candidate = request.get("candidate")
    if candidate is None:
        sys.stdout.write(json.dumps({"status": "ready"}))
        return
    ordered = sorted(float(row["abs_error"]) for row in candidate["observations"])
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    p95 = ordered[lower] * (1 - fraction) + ordered[upper] * fraction
    reported = {metric["id"]: metric["value"] for metric in candidate["metrics"]}
    matches = abs(float(reported["tail_error"]) - p95) <= 1e-12
    result = {"schema_version": "experiment-validation-1", "study_id": candidate["study_id"],
              "candidate_sha256": request["candidate_sha256"],
              "decision": "accepted" if matches else "rejected",
              "checks": [{"id": "row_arithmetic", "outcome": "passed", "evidence": "observations parsed"},
                         {"id": "finite_values", "outcome": "passed", "evidence": "all recorded errors finite"}],
              "metric_recalculations": [{"metric_id": "tail_error",
                                          "reported_value": float(reported["tail_error"]),
                                          "recalculated_value": p95, "tolerance": 1e-12, "matches": matches}],
              "limitations": ["Recalculates summaries from recorded observations only."]}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
'''


class StubClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete(self, *, system, prompt):
        self.calls += 1
        return ModelResult(json.dumps(self.payload), "stub", {"model_calls": 1}, 0.0, "stop")


class CapabilityFoundryTests(unittest.TestCase):
    @staticmethod
    def _payload():
        return {
            "executor_source": MINI_EXECUTOR,
            "validator_source": MINI_VALIDATOR,
            "runtime": {"python": "3.14", "packages": [{"name": "numpy", "version": "2.5.2"}]},
            "test_input": {"probe": True},
            "experiment_intent": INTENT,
        }

    @staticmethod
    def _foundry(root):
        return CapabilityFoundry(
            {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
             "model": "stub", "timeout_seconds": 60, "max_output_tokens": 128},
            runtime_python=sys.executable, workspace_root=root / "workspace",
            registry_root=root / "registry", repo_root=ROOT,
            requirements_file=ROOT / "requirements-experiment.txt",
            runtime_packages=[("numpy", "2.5.2")], max_attempts=2)

    def test_model_proposed_program_is_admitted_and_registered(self):
        payload = self._payload()
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            client = StubClient(payload)
            outcome = foundry.generate("compare a declared estimator against a baseline", client=client)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(client.calls, 1)
            self.assertEqual(outcome["admission"]["gates"][:4], [
                "static_scan", "deterministic_replay", "test_vector_digest", "independent_recalculation"])
            self.assertEqual(outcome["admission"]["adversarial_review"]["status"], "admitted")
            descriptor = json.loads(Path(outcome["registration"]["descriptor_path"]).read_text())
            self.assertEqual(descriptor["capability_id"], "generated_study")
            registry = load_registry(ROOT if False else root / "registry")
            self.assertEqual(len(registry["capabilities"]), 1)
            self.assertTrue(Path(descriptor["experiment"]["execution"]["client"]["command"][1]).is_file())
            self.assertEqual(descriptor["experiment"]["execution"]["input"], {"probe": True})

    def test_registered_generated_program_runs_end_to_end_under_required_sandbox(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            outcome = self._foundry(root).generate(
                "compare a declared estimator against a baseline",
                client=StubClient(self._payload()),
            )
            descriptor = json.loads(Path(
                outcome["registration"]["descriptor_path"]).read_text())
            config = {
                "live_dispatch_allowed": True, "data_classification": "public",
                "allocation_mode": "capacity_pool", "project_id": "generated-e2e",
                "objective": "Exercise an admitted generated experiment end to end.",
                "supplied_context": "Synthetic deterministic integration fixture.",
                "model": {"protocol": "openai_compatible",
                          "base_url": "http://example.invalid/v1", "model": "fixture-model",
                          "timeout_seconds": 5, "max_output_tokens": 2000,
                          "auth_env": None},
                "limits": {"max_rounds": 2, "wall_clock_seconds": 120,
                           "checkpoint_seconds": 1, "max_result_bytes": 10_000_000,
                           "concurrent_calls": 3, "worker_concurrency": 1},
                "time_policy": {"first_result_seconds": 60, "target_seconds": 90,
                                "hard_seconds": 120},
                "experiment": descriptor["experiment"],
            }
            runner = ExperimentRunner(root / "experiment-run", config)
            runner.worker_target = fixture_worker
            result = runner.run()
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(len(result["execution_refs"]), 2)
            self.assertTrue(result["event_chain"][0])

    def test_required_scientific_intent_cannot_be_paraphrased(self):
        payload = {
            "executor_source": MINI_EXECUTOR, "validator_source": MINI_VALIDATOR,
            "runtime": {"python": "3.14", "packages": [{"name": "numpy", "version": "2.5.2"}]},
            "test_input": {"probe": True}, "experiment_intent": dict(INTENT),
        }
        payload["experiment_intent"] = dict(payload["experiment_intent"])
        payload["experiment_intent"]["research_question"] = "A convenient replacement question"
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundry(
                {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
                 "model": "stub", "timeout_seconds": 60, "max_output_tokens": 128},
                runtime_python=sys.executable, workspace_root=root / "workspace",
                registry_root=root / "registry", repo_root=ROOT,
                requirements_file=ROOT / "requirements-experiment.txt",
                runtime_packages=[("numpy", "2.5.2")], max_attempts=1)
            with self.assertRaisesRegex(ValidationError, "required scientific intent"):
                foundry.generate(
                    "bounded question", required_intent={
                        "domain": INTENT["domain"],
                        "research_question": INTENT["research_question"]},
                    client=StubClient(payload))

    def test_broken_program_is_never_registered(self):
        payload = {
            "executor_source": "import json\nimport sys\nsys.stdout.write('not json')\n",
            "validator_source": MINI_VALIDATOR,
            "runtime": {"python": "3.14", "packages": [{"name": "numpy", "version": "2.5.2"}]},
            "test_input": {"probe": True},
            "experiment_intent": INTENT,
        }
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundry(
                {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
                 "model": "stub", "timeout_seconds": 60, "max_output_tokens": 128},
                runtime_python=sys.executable, workspace_root=root / "workspace",
                registry_root=root / "registry", repo_root=ROOT,
                requirements_file=ROOT / "requirements-experiment.txt",
                runtime_packages=[("numpy", "2.5.2")], max_attempts=2)
            with self.assertRaisesRegex(Exception, "did not admit"):
                foundry.generate("compare a declared estimator", client=StubClient(payload))
            self.assertEqual(load_registry(root / "registry")["capabilities"], [])


if __name__ == "__main__":
    unittest.main()
