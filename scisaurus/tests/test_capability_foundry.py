import json
import sys
import tempfile
import unittest
import time
from copy import deepcopy
from importlib.metadata import version
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_foundry import (
    CapabilityFoundry, CapabilityDeadlineError, CapabilityModelBudgetExceeded,
    apply_authoring_patch, normalize_capability_candidate, program_failure_context,
    PROGRAM_REVIEW_CHECKS,
)
from unittest.mock import patch
from scisaurus.runtime.capability_registry import load_registry
from scisaurus.runtime.experiment import ExperimentRunner
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.model_work import ModelWorkBlocked, ModelWorkCache
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
    def _review_payload():
        return {"status": "admitted", "findings": [], "checks": [
            {"id": key, "outcome": "passed", "evidence": "Bound synthetic fixture verified."}
            for key in sorted(PROGRAM_REVIEW_CHECKS)]}

    def _cache(self, root):
        control = ControlStore(root / "ledger")
        self.addCleanup(control.close)
        store = ArtifactStore(control)
        store.init_project(principal_note="foundry fixture")
        def publish(logical_id, artifact_type, body, author, **kwargs):
            return store.publish_artifact(logical_id=logical_id, artifact_type=artifact_type,
                author=author, body=canonical_bytes(body), media_type="application/json")
        return ModelWorkCache(store, publish, namespace="command/foundry-work")

    @staticmethod
    def _payload():
        return {
            "executor_source": MINI_EXECUTOR,
            "validator_source": MINI_VALIDATOR,
            "runtime": {"python": "3.14", "packages": [{"name": "numpy", "version": "2.5.2"}]},
            "test_input": {"probe": True},
            "experiment_intent": deepcopy(INTENT),
        }

    @staticmethod
    def _foundry(root):
        return CapabilityFoundry(
            {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
             "model": "stub", "timeout_seconds": 60, "max_output_tokens": 128},
            runtime_python=sys.executable, workspace_root=root / "workspace",
            registry_root=root / "registry", repo_root=ROOT,
            requirements_file=ROOT / "requirements-experiment.txt",
            runtime_packages=[("pip", version("pip"))], max_attempts=2,
            reviewer_client=StubClient(CapabilityFoundryTests._review_payload()))

    def test_runtime_pin_mismatch_stops_before_authoring(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.runtime_packages = [("scisaurus-missing-fixture-package", "1.0")]
            client = StubClient(self._payload())
            with self.assertRaisesRegex(ValidationError, "runtime packages do not match"):
                foundry.generate("bounded comparison", client=client)
            self.assertEqual(client.calls, 0)

    def test_failure_projection_retains_degeneracy_without_nonfinite_json(self):
        projected = program_failure_context({"metrics": [{"id": "correlation", "value": float("nan")}],
            "observations": [{"predictor": 0.0, "response": 1.0},
                             {"predictor": 0.0, "response": 2.0}]})
        self.assertEqual(projected["numeric_observation_fields"]["predictor"]["unique_finite_count"], 1)
        self.assertEqual(projected["numeric_observation_fields"]["response"]["unique_finite_count"], 2)
        self.assertIn("nan", projected["metrics"][0]["value_repr"])
        canonical_bytes(projected)

    def test_identical_failed_source_is_not_executed_again_and_repair_receives_evidence(self):
        payload = self._payload()
        payload["executor_source"] = payload["executor_source"].replace('"value": p95', '"value": float("nan")')
        client = StubClient(payload)
        original = client.complete
        def complete(*, system, prompt):
            if client.calls:
                evidence = json.loads(prompt)["repair_request"]["observed_failure_context"]
                self.assertEqual(evidence["observation_count"], INTENT["run_count"])
                self.assertIn("nan", evidence["metrics"][0]["value_repr"])
            return original(system=system, prompt=prompt)
        client.complete = complete
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            with patch.object(foundry, "_execute", wraps=foundry._execute) as execute:
                with self.assertRaisesRegex(ModelWorkBlocked, "finite JSON scalar"):
                    foundry.generate("bounded comparison", client=client)
                self.assertEqual(client.calls, 2)
                self.assertEqual(execute.call_count, 2)

    def test_validator_launch_failure_is_detected_before_any_experiment_execution(self):
        payload = self._payload()
        payload["validator_source"] = 'import sys\nsys.stdout.write("not json")\n'
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 1
            with patch.object(foundry, "_execute", wraps=foundry._execute) as execute:
                with self.assertRaisesRegex(ModelWorkBlocked, "validator readiness did not return a JSON"):
                    foundry.generate("bounded comparison", client=StubClient(payload))
                self.assertEqual(execute.call_count, 1)
                self.assertEqual(execute.call_args.args[0], payload["validator_source"])

    def test_foundry_and_live_executor_receive_the_same_quality_contract(self):
        intent = deepcopy(INTENT)
        intent["quality_contract"] = {"schema_version": "fixture", "required_axes": ["baseline"]}
        runner = object.__new__(ExperimentRunner)
        runner.experiment = {**intent, "execution": {"input": {"probe": True}}}
        compiled = CapabilityFoundry._payload(intent, {"probe": True})
        self.assertEqual(compiled, runner._program_input())
        self.assertEqual(compiled["experiment"]["quality_contract"], intent["quality_contract"])
        compiled["experiment"]["quality_contract"]["required_axes"].append("modified")
        self.assertEqual(intent["quality_contract"]["required_axes"], ["baseline"])

    def test_model_proposed_program_is_admitted_and_registered(self):
        payload = self._payload()
        del payload["runtime"]
        del payload["test_input"]
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
            self.assertEqual(outcome["admission"]["adversarial_review"]["review_method"], "independent_model")
            self.assertEqual(foundry.reviewer_client.calls, 1)
            descriptor = json.loads(Path(outcome["registration"]["descriptor_path"]).read_text())
            self.assertEqual(descriptor["capability_id"], "generated_study")
            registry = load_registry(ROOT if False else root / "registry")
            self.assertEqual(len(registry["capabilities"]), 1)
            self.assertTrue(Path(descriptor["experiment"]["execution"]["client"]["command"][1]).is_file())
            self.assertEqual(descriptor["experiment"]["execution"]["input"], {"probe": True})
            self.assertTrue(outcome["candidate"]["runtime"]["python"].startswith(
                f"{sys.version_info.major}.{sys.version_info.minor}."))

    def test_model_call_budget_stops_before_independent_review_can_repeat(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            author = StubClient(self._payload())
            with self.assertRaises(CapabilityModelBudgetExceeded):
                foundry.generate("bounded comparison", client=author, model_call_budget=1)
            self.assertEqual(author.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 0)

    def test_author_cannot_replace_the_configured_test_data(self):
        payload = self._payload()
        payload["test_input"] = {"invented_data": [1, 2, 3]}
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 1
            with self.assertRaisesRegex(ValidationError, "controller-owned configured_input"):
                foundry.generate("bounded comparison", client=StubClient(payload))

    def test_identifier_drift_is_normalized_across_intent_and_program_sources(self):
        payload = self._payload()
        drifted = "Max_DvPdP_Window"
        payload["experiment_intent"] = deepcopy(payload["experiment_intent"])
        payload["experiment_intent"]["primary_outcomes"][0]["id"] = drifted
        payload["executor_source"] = payload["executor_source"].replace("tail_error", drifted)
        payload["validator_source"] = payload["validator_source"].replace("tail_error", drifted)
        normalized, repairs = normalize_capability_candidate(payload)
        self.assertEqual(
            normalized["experiment_intent"]["primary_outcomes"][0]["id"],
            "max_dvpdp_window",
        )
        self.assertNotIn(drifted, normalized["executor_source"])
        self.assertNotIn(drifted, normalized["validator_source"])
        self.assertEqual(len(repairs), 1)
        with tempfile.TemporaryDirectory() as path:
            outcome = self._foundry(Path(path)).generate(
                "bounded comparison", client=StubClient(payload))
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(
                outcome["candidate"]["experiment_intent"]["primary_outcomes"][0]["id"],
                "max_dvpdp_window",
            )

    def test_metadata_repair_retains_sources_and_revalidates_the_assembled_program(self):
        payload = self._payload()
        payload["experiment_intent"] = {**payload["experiment_intent"], "study_type": "theory_simulation"}
        client = StubClient(payload)
        original = client.complete
        def complete(*, system, prompt):
            if client.calls == 0:
                return original(system=system, prompt=prompt)
            assignment = json.loads(prompt)
            self.assertIn("updates", assignment["output_contract"])
            self.assertIn("exploratory", assignment["repair_request"]["previous_error"])
            client.calls += 1
            return ModelResult(json.dumps({"updates": {"experiment_intent": {"study_type": "exploratory"}}}),
                               "stub", {"model_calls": 1}, 0, "stop")
        client.complete = complete
        with tempfile.TemporaryDirectory() as path:
            result = self._foundry(Path(path)).generate("bounded comparison", client=client)
            self.assertEqual(result["candidate"]["executor_source"], MINI_EXECUTOR)
            self.assertEqual(result["candidate"]["validator_source"], MINI_VALIDATOR)
            self.assertEqual(result["candidate"]["experiment_intent"]["study_type"], "exploratory")
            self.assertEqual(client.calls, 2)
        self.assertEqual(payload["experiment_intent"]["study_type"], "theory_simulation")

    def test_authoring_patch_cannot_change_host_owned_fields(self):
        with self.assertRaisesRegex(ValidationError, "may change only"):
            apply_authoring_patch(self._payload(), {"updates": {"runtime": {"python": "invented"}}})

    def test_exact_source_edits_preserve_unchanged_code_and_input(self):
        previous = self._payload()
        revised = apply_authoring_patch(previous, {"updates": {"executor_source": {"edits": [
            {"old": 'if __name__ == "__main__":', "new": 'if "__main__" == __name__:'},
        ]}}})
        self.assertEqual(revised["executor_source"],
                         MINI_EXECUTOR.replace('if __name__ == "__main__":', 'if "__main__" == __name__:'))
        self.assertEqual(revised["validator_source"], MINI_VALIDATOR)
        self.assertEqual(previous["executor_source"], MINI_EXECUTOR)

    def test_exact_source_edits_reject_missing_or_ambiguous_matches_atomically(self):
        previous = self._payload()
        for old in ("missing source text", "\n"):
            with self.subTest(old=old), self.assertRaisesRegex(ValidationError, "match exactly once"):
                apply_authoring_patch(previous, {"updates": {"executor_source": {"edits": [
                    {"old": 'if __name__ == "__main__":', "new": 'if "__main__" == __name__:'},
                    {"old": old, "new": "replacement"},
                ]}}})
            self.assertEqual(previous["executor_source"], MINI_EXECUTOR)

    def test_exact_source_edits_are_ordered_and_may_delete_text(self):
        previous = self._payload()
        revised = apply_authoring_patch(previous, {"updates": {"executor_source": {"edits": [
            {"old": "import json", "new": "import json\n# transient marker"},
            {"old": "\n# transient marker", "new": ""},
        ]}}})
        self.assertEqual(revised["executor_source"], MINI_EXECUTOR)

    def test_overlapping_source_matches_are_ambiguous(self):
        previous = self._payload()
        previous["executor_source"] = "aaa"
        with self.assertRaisesRegex(ValidationError, "match exactly once"):
            apply_authoring_patch(previous, {"updates": {"executor_source": {"edits": [
                {"old": "aa", "new": "replacement"},
            ]}}})
        self.assertEqual(previous["executor_source"], "aaa")

    def test_source_edit_requires_exact_fields_and_nonempty_old_text(self):
        for value in ({"edits": []}, {"edits": [{"old": "", "new": "x"}]},
                      {"edits": [{"old": "import json", "new": None}]},
                      {"edits": [{"old": "import json", "new": "x", "regex": True}]}, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                apply_authoring_patch(self._payload(), {"updates": {"executor_source": value}})

    def test_exact_source_repair_passes_the_full_program_gates(self):
        payload = self._payload()
        payload["validator_source"] = MINI_VALIDATOR.replace('"__main__"', '"main__"')
        client = StubClient(payload)
        original = client.complete
        def complete(*, system, prompt):
            if client.calls == 0:
                return original(system=system, prompt=prompt)
            client.calls += 1
            return ModelResult(json.dumps({"updates": {"validator_source": {"edits": [
                {"old": 'if __name__ == "main__":', "new": 'if __name__ == "__main__":'},
            ]}}}), "stub", {"model_calls": 1}, 0, "stop")
        client.complete = complete
        with tempfile.TemporaryDirectory() as path:
            outcome = self._foundry(Path(path)).generate("bounded comparison", client=client)
            self.assertEqual(outcome["candidate"]["validator_source"], MINI_VALIDATOR)
            self.assertEqual(outcome["candidate"]["executor_source"], MINI_EXECUTOR)
            self.assertEqual(client.calls, 2)

    def test_repair_can_remove_unwanted_intent_fields_without_rewriting_sources(self):
        previous = self._payload()
        previous["experiment_intent"] = {**previous["experiment_intent"], "unexpected": {"key": True}}
        revised = apply_authoring_patch(previous, {"updates": {"experiment_intent": {"unexpected": None}}})
        self.assertNotIn("unexpected", revised["experiment_intent"])
        self.assertEqual(revised["executor_source"], previous["executor_source"])
        self.assertIn("unexpected", previous["experiment_intent"])
        with self.assertRaises(ValidationError):
            apply_authoring_patch(previous, {"updates": {"runtime": None}})
        self.assertIsNone(revised["experiment_intent"]["primary_outcomes"][0]["threshold"])

    def test_merge_patch_removes_null_members_in_new_and_replaced_objects(self):
        previous = self._payload()
        previous["experiment_intent"]["parameters"] = {"replace": 7}
        revised = apply_authoring_patch(previous, {"updates": {"experiment_intent": {
            "parameters": {"new": {"omitted": None, "kept": 3},
                           "replace": {"omitted": None, "kept": 4}}
        }}})
        self.assertEqual(revised["experiment_intent"]["parameters"],
                         {"new": {"kept": 3}, "replace": {"kept": 4}})

    def test_validation_deadline_retains_response_even_on_final_authoring_attempt(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(self._payload())
            foundry = self._foundry(root)
            foundry.max_attempts = 1
            with patch.object(foundry, "_execute", side_effect=CapabilityDeadlineError("deadline")):
                with self.assertRaises(CapabilityDeadlineError):
                    foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(cache.entries()[0]["status"], "response_received")
            result = foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(client.calls, 1)

    def test_cached_capability_rechecks_registered_program_integrity(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(self._payload())
            foundry = self._foundry(root)
            outcome = foundry.generate("bounded comparison", client=client, work_cache=cache)
            descriptor = json.loads(Path(outcome["registration"]["descriptor_path"]).read_text())
            executor = Path(descriptor["experiment"]["execution"]["client"]["command"][1])
            executor.unlink()
            with self.assertRaises(ValidationError):
                foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(client.calls, 1)

    def test_changed_contract_uses_failed_source_as_input_not_as_accepted_work(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            previous = self._payload()
            previous["experiment_intent"] = {**previous["experiment_intent"], "unexpected": True}
            cache.put("previous-contract", {"status": "blocked", "last_attempt": previous,
                "feedback": "remove unexpected field", "usage": {"model_calls": 2},
                "requests": [{"prompt": json.dumps({"capability_brief": "bounded comparison",
                    "configured_input": {"probe": True}})}]})
            client = StubClient({"updates": {"experiment_intent": {"unexpected": None}}})
            foundry = self._foundry(root)
            outcome = foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(outcome["candidate"]["executor_source"], previous["executor_source"])
            self.assertEqual(client.calls, 1)
            self.assertIn("candidate_seed_ref", cache.entries()[0])

    def test_changed_patch_contract_replays_recorded_repair_before_spending_a_call(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            previous = self._payload()
            previous["experiment_intent"] = {**previous["experiment_intent"], "unexpected": None}
            reply = {"updates": {"experiment_intent": {"unexpected": None}}}
            cache.put("previous-contract", {"status": "blocked", "last_attempt": previous,
                "feedback": "remove unexpected field", "usage": {"model_calls": 2},
                "last_response": {"text": json.dumps(reply), "model": "stub",
                    "usage": {"model_calls": 1}, "elapsed_seconds": 0, "finish_reason": "stop"},
                "requests": [{"prompt": json.dumps({"capability_brief": "bounded comparison",
                    "configured_input": {"probe": True}})}]})
            client = StubClient(reply)
            outcome = self._foundry(root).generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(client.calls, 0)
            self.assertEqual(cache.entries()[0]["usage"], {"model_calls": 1})

    def test_changed_contract_replays_exact_edit_against_its_original_base(self):
        for recorded_base in (False, True):
            with self.subTest(recorded_base=recorded_base), tempfile.TemporaryDirectory() as path:
                root = Path(path)
                cache = self._cache(root)
                assembled = self._payload()
                previous = self._payload()
                previous["validator_source"] = MINI_VALIDATOR.replace('"__main__"', '"main__"')
                reply = {"updates": {"validator_source": {"edits": [
                    {"old": 'if __name__ == "main__":', "new": 'if __name__ == "__main__":'},
                ]}}}
                prior = {"status": "blocked", "last_attempt": assembled,
                    "feedback": "old validation contract", "usage": {"model_calls": 2},
                    "last_response": {"text": json.dumps(reply), "model": "stub",
                        "usage": {"model_calls": 1}, "elapsed_seconds": 0, "finish_reason": "stop"},
                    "requests": [{"role": "research.experiment-author", "status": "succeeded", "prompt": json.dumps({
                        "capability_brief": "bounded comparison", "configured_input": {"probe": True},
                        "repair_request": {"previous_attempt": previous}})}]}
                prior["requests"].append({"role": "research.experiment-author", "status": "result_unknown",
                    "prompt": json.dumps({"capability_brief": "bounded comparison",
                        "configured_input": {"probe": True}, "repair_request": {"previous_attempt": assembled}})})
                if recorded_base:
                    prior["response_base"] = previous
                cache.put("previous-contract", prior)
                author = StubClient(reply)
                outcome = self._foundry(root).generate("bounded comparison", client=author, work_cache=cache)
                self.assertEqual(outcome["status"], "registered")
                self.assertEqual(outcome["candidate"]["validator_source"], MINI_VALIDATOR)
                self.assertEqual(author.calls, 0)
                self.assertEqual(cache.entries()[0]["usage"]["model_calls"], 1)

    def test_check_only_review_rejection_preserves_the_failed_evidence(self):
        review = self._review_payload()
        review["status"] = "rejected"
        review["checks"][0].update(outcome="failed", evidence="The reported estimator ignores its declared input")
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.reviewer_client = StubClient(review)
            author = StubClient(self._payload())
            complete = author.complete
            def inspect_repair(**kwargs):
                if author.calls:
                    feedback = json.loads(kwargs["prompt"])["repair_request"]["validation_feedback"]
                    self.assertEqual(feedback["failed_checks"], [review["checks"][0]])
                    self.assertEqual(feedback["findings"], [])
                return complete(**kwargs)
            author.complete = inspect_repair
            with self.assertRaisesRegex(ModelWorkBlocked, "ignores its declared input"):
                foundry.generate("bounded comparison", client=author)
            self.assertEqual(author.calls, 2)
            self.assertEqual(foundry.reviewer_client.calls, 1)

    def test_recalculation_feedback_prioritizes_failures_over_successful_checks(self):
        from scisaurus.runtime.program_gates import ProgramGateRejected
        passed = {"id": "recalculation", "outcome": "passed", "evidence": "matching values" * 100}
        failed = {"id": "sensitivity", "outcome": "failed", "evidence": "declared input not measured"}
        exc = ProgramGateRejected("independent recalculation did not accept the candidate",
                                  {"decision": "rejected", "checks": [passed] * 100 + [failed]},
                                  gate="independent_recalculation")
        self.assertEqual(exc.feedback["failed_checks"], [failed])
        self.assertIn("declared input not measured", str(exc))
        self.assertNotIn("matching values", str(exc))

    def test_independent_rejection_blocks_registration_and_reuses_the_exact_verdict(self):
        review = self._review_payload()
        review["status"] = "rejected"
        review["checks"][0]["outcome"] = "failed"
        review["findings"] = [{"severity": "blocking", "finding": "Undefined estimator hidden as zero",
                               "evidence": "empty aggregate substituted with zero", "required_change": "repair the estimand"}]
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.reviewer_client = StubClient(review)
            author = StubClient(self._payload())
            complete = author.complete
            def inspect_repair(**kwargs):
                if author.calls:
                    corrections = json.loads(kwargs["prompt"])["repair_request"]["validation_feedback"]["findings"]
                    self.assertEqual(corrections[0]["required_change"], "repair the estimand")
                return complete(**kwargs)
            author.complete = inspect_repair
            cache = self._cache(root)
            for _ in range(2):
                with self.assertRaisesRegex(ModelWorkBlocked, "Undefined estimator hidden as zero"):
                    foundry.generate("bounded comparison", client=author, work_cache=cache)
            self.assertEqual(author.calls, 2)
            self.assertEqual(foundry.reviewer_client.calls, 1)
            self.assertEqual(cache.entries()[0]["usage"]["model_calls"], 3)
            self.assertFalse((root / "registry/capabilities/index.json").exists())

    def test_review_retains_raw_response_and_usage_across_interruption(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            author = StubClient(self._payload())
            cache = self._cache(root)
            def interrupt(phase, state):
                if phase == "scientific_review_response":
                    raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                foundry.generate("bounded comparison", client=author, work_cache=cache, on_progress=interrupt)
            result = foundry.generate("bounded comparison", client=author, work_cache=cache)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(author.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 1)
            self.assertEqual(cache.entries()[0]["usage"]["model_calls"], 2)

    def test_late_scientific_verdict_is_retained_but_not_registered(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            author = StubClient(self._payload())
            monotonic = time.monotonic
            deadline = monotonic() + 60
            completed = False
            original = foundry.reviewer_client.complete
            def late_review(**kwargs):
                nonlocal completed
                response = original(**kwargs)
                completed = True
                return response
            foundry.reviewer_client.complete = late_review
            with patch("scisaurus.runtime.capability_foundry.time.monotonic",
                       side_effect=lambda: deadline + 1 if completed else monotonic()):
                with self.assertRaises(CapabilityDeadlineError):
                    foundry.generate("bounded comparison", client=author, work_cache=cache, deadline=deadline)
            self.assertFalse((root / "registry/capabilities/index.json").exists())
            self.assertEqual(cache.entries()[0]["status"], "response_received")
            result = foundry.generate("bounded comparison", client=author, work_cache=cache,
                                     deadline=monotonic() + 60)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(author.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 1)

    def test_truncated_review_uses_only_one_configured_format_fallback_and_survives_resume(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.reviewer_client = None
            foundry.model_config["role_models"] = {"review.methods": {"model": "first-reviewer"}}
            foundry.model_config["role_model_fallbacks"] = {"review.methods": [{"model": "second-reviewer"}]}
            author = StubClient(self._payload())
            cache = self._cache(root)
            first = StubClient({})
            def truncated(**kwargs):
                first.calls += 1
                return ModelResult("unfinished reasoning that must not be echoed", "first-reviewer",
                                   {"model_calls": 1}, 0, "length")
            first.complete = truncated
            second = StubClient(self._review_payload())
            original = second.complete
            def finish(**kwargs):
                self.assertNotIn("unfinished reasoning that must not be echoed", kwargs["prompt"])
                self.assertIn("format_repair", json.loads(kwargs["prompt"]))
                return original(**kwargs)
            second.complete = finish
            def interrupt(phase, state):
                if phase == "scientific_review_response":
                    raise KeyboardInterrupt()
            with patch("scisaurus.runtime.capability_foundry.ModelClient", return_value=first):
                with self.assertRaises(KeyboardInterrupt):
                    foundry.generate("bounded comparison", client=author, work_cache=cache, on_progress=interrupt)
            with patch("scisaurus.runtime.capability_foundry.ModelClient", return_value=second) as factory:
                outcome = foundry.generate("bounded comparison", client=author, work_cache=cache)
                self.assertEqual(factory.call_args.kwargs["model"], "second-reviewer")
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual((author.calls, first.calls, second.calls), (1, 1, 1))
            self.assertEqual(cache.entries()[0]["usage"]["model_calls"], 3)

    def test_review_format_repair_budget_is_durable_and_never_relaxes_the_gate(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.reviewer_client = StubClient({"invalid": True})
            author = StubClient(self._payload())
            cache = self._cache(root)
            for _ in range(2):
                with self.assertRaisesRegex(ModelWorkBlocked, "review response is invalid"):
                    foundry.generate("bounded comparison", client=author, work_cache=cache)
            self.assertEqual(author.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 2)
            self.assertFalse((root / "registry/capabilities/index.json").exists())

    def test_unhashable_review_fields_raise_validation_errors(self):
        from scisaurus.runtime.capability_foundry import validate_program_review
        for field in ("id", "outcome", "severity"):
            value = self._review_payload()
            if field == "severity":
                value["findings"] = [{"severity": {}, "finding": "invalid", "evidence": "invalid",
                                      "required_change": "invalid"}]
            else:
                value["checks"][0][field] = []
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_program_review(value)

    def test_malformed_review_field_repairs_format_without_reauthoring(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            invalid = self._review_payload()
            invalid["checks"][0]["id"] = []
            reviewer = StubClient(invalid)
            complete = reviewer.complete
            def repair(**kwargs):
                if reviewer.calls:
                    repair_contract = json.loads(kwargs["prompt"])["format_repair"]
                    self.assertEqual(set(repair_contract["required_check_ids"]), PROGRAM_REVIEW_CHECKS)
                    self.assertIn("independent_validation", repair_contract["instructions"])
                    reviewer.payload = self._review_payload()
                return complete(**kwargs)
            reviewer.complete = repair
            foundry.reviewer_client = reviewer
            author = StubClient(self._payload())
            outcome = foundry.generate("bounded comparison", client=author)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual((author.calls, reviewer.calls), (1, 2))

    def test_review_limitations_do_not_invalidate_a_complete_rejection(self):
        from scisaurus.runtime.capability_foundry import validate_program_review
        value = self._review_payload()
        value.update(status="rejected", limitations=["No external simulation was run."])
        value["checks"][0]["outcome"] = "failed"
        self.assertEqual(validate_program_review(value)["status"], "rejected")
        with self.assertRaises(ValidationError):
            validate_program_review({**value, "status": "admitted"})

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
                runtime_packages=[("pip", version("pip"))], max_attempts=1)
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
                runtime_packages=[("pip", version("pip"))], max_attempts=2)
            with self.assertRaisesRegex(Exception, "did not admit"):
                foundry.generate("compare a declared estimator", client=StubClient(payload))
            self.assertEqual(load_registry(root / "registry")["capabilities"], [])

    def test_malformed_asset_contract_returns_actionable_repair_feedback(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace(
            '"id": asset_id, "path": name, "sha256":',
            '"id": asset_id, "sha256":',
        )
        self.assertNotEqual(payload["executor_source"], MINI_EXECUTOR)
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.max_attempts = 1
            with self.assertRaisesRegex(
                    ValidationError, "experiment asset requires exactly"):
                foundry.generate("compare a declared estimator", client=StubClient(payload))
            self.assertEqual(load_registry(root / "registry")["capabilities"], [])

    def test_duplicate_metric_feedback_names_the_id_and_both_locations(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace('"metrics": metrics,', '"metrics": metrics + metrics,')
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.max_attempts = 1
            with self.assertRaisesRegex(ValidationError, "'tail_error' at metrics\\[1\\] duplicates metrics\\[0\\]"):
                foundry.generate("bounded comparison", client=StubClient(payload))

    def test_repeated_failure_is_durable_and_does_not_burn_remaining_attempts(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace('"metrics": metrics,', '"metrics": metrics + metrics,')
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(payload)
            foundry = self._foundry(root)
            foundry.max_attempts = 8
            states = []
            for _ in range(2):
                with self.assertRaises(ModelWorkBlocked):
                    foundry.generate("bounded comparison", client=client, work_cache=cache,
                                     on_progress=lambda phase, state: states.append(state))
            self.assertEqual(client.calls, 2)
            self.assertEqual(states[-1]["attempts"], 2)
            self.assertEqual(states[-1]["usage"]["model_calls"], 2)
            self.assertEqual(states[-1]["status"], "blocked")
            self.assertIn("metrics[1]", states[-1]["feedback"])
            self.assertEqual(states[-1]["last_attempt"]["executor_source"], payload["executor_source"])
            self.assertEqual(len(states[-1]["requests"]), 2)

    def test_repeated_admission_gate_stops_before_authoring_budget_is_spent(self):
        payload = self._payload()

        class VaryingAuthor:
            def __init__(self, value):
                self.value = value
                self.calls = 0

            def complete(self, *, system, prompt):
                self.calls += 1
                candidate = deepcopy(self.value)
                candidate["executor_source"] = (
                    f"# repair-{self.calls}\n" + candidate["executor_source"])
                return ModelResult(json.dumps(candidate), "author", {"model_calls": 1}, 0.0, "stop")

        rejecting_review = StubClient({
            "status": "rejected",
            "checks": [
                {"id": key, "outcome": "failed", "evidence": "The fixture is not scientifically adequate."}
                for key in sorted(PROGRAM_REVIEW_CHECKS)
            ],
            "findings": [{"severity": "blocking", "finding": "same gate defect",
                           "evidence": "same evidence", "required_change": "change the design"}],
            "limitations": ["The fixture remains bounded."],
        })
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.reviewer_client = rejecting_review
            author = VaryingAuthor(payload)
            with self.assertRaisesRegex(ModelWorkBlocked, "adversarial_review repair budget exhausted"):
                foundry.generate("bounded comparison", client=author)
            self.assertEqual(author.calls, 2)
            self.assertEqual(rejecting_review.calls, 2)

    def test_alternating_validation_errors_cannot_consume_the_full_allowance(self):
        client = StubClient({})
        def alternate(*, system, prompt):
            client.calls += 1
            value = {"extra_a" if client.calls % 2 else "extra_b": True}
            return ModelResult(json.dumps(value), "stub", {"model_calls": 1}, 0, "stop")
        client.complete = alternate
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 8
            with self.assertRaises(ModelWorkBlocked):
                foundry.generate("bounded comparison", client=client)
            self.assertEqual(client.calls, 3)

    def test_recorded_response_resumes_validation_without_another_model_call(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(self._payload())
            foundry = self._foundry(root)
            def stop_after_response(phase, state):
                if phase == "response_received":
                    raise KeyboardInterrupt("fixture interruption")
            with self.assertRaises(KeyboardInterrupt):
                foundry.generate("bounded comparison", client=client, work_cache=cache,
                                 on_progress=stop_after_response)
            outcome = foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(client.calls, 1)
            self.assertEqual(foundry.generate("bounded comparison", client=client, work_cache=cache), outcome)
            self.assertEqual(client.calls, 1)

    def test_author_outer_json_closer_is_repaired_without_a_second_model_call(self):
        payload = self._payload()

        class TruncatedEnvelopeClient(StubClient):
            def complete(self, *, system, prompt):
                self.calls += 1
                return ModelResult(json.dumps(self.payload)[:-1], "stub",
                                   {"model_calls": 1}, 0.0, "stop")

        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            client = TruncatedEnvelopeClient(payload)
            outcome = foundry.generate("bounded comparison", client=client)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(client.calls, 1)

    def test_deadline_blocks_dispatch_before_a_model_call(self):
        with tempfile.TemporaryDirectory() as path:
            client = StubClient(self._payload())
            with self.assertRaisesRegex(ValidationError, "mission deadline"):
                self._foundry(Path(path)).generate("bounded comparison", client=client, deadline=0)
            self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
