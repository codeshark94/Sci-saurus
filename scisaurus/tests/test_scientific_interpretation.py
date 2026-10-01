import json
import threading
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import (
    ModelCallError, ModelResult, admit_model_provider_call,
    clear_model_provider_cooldown,
    model_provider_cooldown_remaining,
    record_model_provider_cooldown,
)
from scisaurus.runtime.scientific_interpretation import (
    ScientificInterpretationRunner, validate_interpretation,
)


def interpretation():
    return {
        "schema_version": "scientific-interpretation-1",
        "research_question": "Does recalibration improve all probability scores on a small tabular sample?",
        "result_patterns": [{
            "id": "mixed_scores", "result_ref": "result-1",
            "pattern": "A proper score worsened while two calibration summaries improved.",
            "so_what": "Calibration quality cannot be represented by one summary alone.",
            "supporting_evidence": ["finding-1"], "contradicting_evidence": [],
        }],
        "competing_explanations": [{
            "id": "tail_reweighting", "mechanism": "A scalar map may improve central bins while amplifying errors in a small number of extreme cases.",
            "status": "possible", "supporting_evidence": ["finding-1"], "counterevidence": [],
            "discriminating_test": "Compare per-observation loss changes and calibration strata across more datasets.",
        }],
        "discriminating_experiments": [{
            "id": "stratified_replication", "question": "Does the same tradeoff recur across datasets and model families?",
            "design": "Repeat the comparison across prespecified datasets, models, and resampling schemes.",
            "predictions": ["A tail-driven mechanism predicts concentrated log-loss increases."],
            "required_measurements": ["Per-observation log-loss changes", "Calibration curves by score range"],
        }],
        "prioritization": {"primary_pattern_id": "mixed_scores", "secondary_pattern_ids": [],
                            "rationale": "The disagreement between proper and calibration summaries determines the practical interpretation."},
        "conclusion": "The observed tradeoff motivates metric-specific evaluation and does not establish universal benefit.",
    }


class ScientificInterpretationTests(unittest.TestCase):
    def setUp(self):
        clear_model_provider_cooldown({
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "auth_env": None,
        })

    def test_validates_explicit_pattern_mechanism_and_test(self):
        value = interpretation()
        validate_interpretation(value, evidence_ids={"finding-1"})

    def test_inflight_success_cannot_clear_a_newer_provider_circuit(self):
        scope = {
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "auth_env": None,
        }
        observed_generation, wait_seconds = admit_model_provider_call(scope)
        self.assertEqual(wait_seconds, 0.0)
        self.assertIsNotNone(observed_generation)
        record_model_provider_cooldown(scope, retry_after_seconds=120)

        cleared = clear_model_provider_cooldown(
            scope, expected_generation=observed_generation)

        self.assertFalse(cleared)
        self.assertGreater(model_provider_cooldown_remaining(scope), 0)

    def test_provider_call_admission_is_linearizable_with_cooldown_open(self):
        scope = {
            "protocol": "openai_compatible",
            "base_url": "http://admission-race.test/v1",
            "auth_env": None,
        }
        barrier = threading.Barrier(3)
        outcomes = {}
        errors = []

        def admit():
            try:
                barrier.wait()
                outcomes["admission"] = admit_model_provider_call(scope)
            except Exception as exc:
                errors.append(exc)

        def open_circuit():
            try:
                barrier.wait()
                outcomes["cooldown"] = record_model_provider_cooldown(
                    scope, retry_after_seconds=120)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=admit),
                   threading.Thread(target=open_circuit)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        generation, wait_seconds = outcomes["admission"]
        if generation is None:
            self.assertGreater(wait_seconds, 0)
        else:
            self.assertEqual(wait_seconds, 0)
            self.assertFalse(clear_model_provider_cooldown(
                scope, expected_generation=generation))
        self.assertGreater(model_provider_cooldown_remaining(scope), 0)

    def test_rejects_internal_vocabulary(self):
        value = interpretation()
        value["conclusion"] = "The frozen artifact was accepted."
        with self.assertRaisesRegex(ValidationError, "control-plane"):
            validate_interpretation(value)

    def test_rejects_unknown_evidence(self):
        value = interpretation()
        value["result_patterns"][0]["supporting_evidence"] = ["unknown"]
        with self.assertRaisesRegex(ValidationError, "unknown evidence"):
            validate_interpretation(value, evidence_ids={"finding-1"})

    def test_maturity_closure_requires_complete_scoped_evidence_assessments(self):
        value = interpretation()
        requirements = [{"id": "requirement-1", "requirement": "Check the independent comparator."}]
        with self.assertRaises(ValidationError):
            validate_interpretation(value, evidence_ids={"finding-1"}, requirements=requirements)
        value["requirement_assessments"] = [{
            "requirement_id": "requirement-1", "disposition": "resolved",
            "evidence_ids": ["finding-1"], "rationale": "The independent comparator is present in the supplied finding."}]
        validate_interpretation(value, evidence_ids={"finding-1"}, requirements=requirements)
        value["requirement_assessments"][0]["evidence_ids"] = []
        with self.assertRaisesRegex(ValidationError, "needs supplied evidence"):
            validate_interpretation(value, evidence_ids={"finding-1"}, requirements=requirements)

    def test_known_429_stops_without_dispatching_model_fallback(self):
        primary = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1",
            "model": "deepseek-v4.1-flash:cloud", "auth_env": None,
            "context_window_tokens": 262144, "max_input_tokens": 245760,
            "max_output_tokens": 8192,
        }
        fallback = {**primary, "model": "glm-5.3-flash:cloud"}
        config = {
            **primary,
            "role_models": {"strategy.interpretation": primary},
            "role_model_fallbacks": {"strategy.interpretation": [fallback]},
        }
        routed_models = []

        class StubClient:
            def __init__(self, **route):
                self.route = route
                routed_models.append(route["model"])

            def complete(self, *, system, prompt, images=None):
                if self.route["model"] == primary["model"]:
                    raise ModelCallError(
                        "model HTTP request failed with status 429",
                        outcome_known=True, attempts=1, status_code=429,
                    )
                return ModelResult(
                    text=json.dumps(interpretation()), model=self.route["model"],
                    usage={"input_tokens": 23, "output_tokens": 17},
                    elapsed_seconds=0.1, finish_reason="stop", request_attempts=1,
                )

        with patch("scisaurus.runtime.scientific_interpretation.ModelClient", StubClient):
            with self.assertRaises(ModelCallError) as caught:
                ScientificInterpretationRunner(config).run(
                    {"evidence_ids": ["finding-1"]}, evidence_ids={"finding-1"},
                )

        self.assertEqual(routed_models, [primary["model"]])
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.route_history, [{
            "route": "primary", "model": primary["model"],
            "status_code": 429, "provider_error_kind": None,
            "request_attempts": 1,
        }])

    def test_unknown_model_outcome_does_not_replay_through_fallback(self):
        primary = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1",
            "model": "deepseek-v4.1-flash:cloud", "auth_env": None,
        }
        config = {
            **primary,
            "role_models": {"strategy.interpretation": primary},
            "role_model_fallbacks": {"strategy.interpretation": [
                {**primary, "model": "glm-5.3-flash:cloud"},
            ]},
        }
        calls = []

        class StubClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                raise ModelCallError(
                    "model request outcome is unknown", outcome_known=False,
                    attempts=1, status_code=429,
                )

        with patch("scisaurus.runtime.scientific_interpretation.ModelClient", StubClient):
            with self.assertRaises(ModelCallError):
                ScientificInterpretationRunner(config).run(
                    {"evidence_ids": ["finding-1"]}, evidence_ids={"finding-1"},
                )
        self.assertEqual(calls, [primary["model"]])

    def test_all_configured_routes_stop_at_first_429(self):
        primary = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1",
            "model": "deepseek-v4.1-flash:cloud", "auth_env": None,
        }
        fallback = {**primary, "model": "glm-5.3-flash:cloud"}
        last_resort = {**primary, "model": "gemma4:31b-cloud"}
        config = {
            **primary,
            "role_models": {"strategy.interpretation": primary},
            "role_model_fallbacks": {"strategy.interpretation": [fallback, last_resort]},
        }
        calls = []

        class StubClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                raise ModelCallError(
                    "model HTTP request failed with status 429",
                    outcome_known=True, attempts=1, status_code=429,
                )

        with patch("scisaurus.runtime.scientific_interpretation.ModelClient", StubClient):
            with self.assertRaises(ModelCallError) as caught:
                ScientificInterpretationRunner(config).run(
                    {"evidence_ids": ["finding-1"]}, evidence_ids={"finding-1"},
                )
        self.assertEqual(calls, [primary["model"]])
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(caught.exception.route_history, [{
            "route": "primary", "model": primary["model"],
            "status_code": 429, "provider_error_kind": None,
            "request_attempts": 1,
        }])

    def test_account_quota_429_does_not_fan_out_to_other_models(self):
        primary = {
            "protocol": "openai_compatible", "base_url": "http://127.0.0.1:11434/v1",
            "model": "deepseek-v4.1-flash:cloud", "auth_env": None,
        }
        fallback = {**primary, "model": "glm-5.3-flash:cloud"}
        config = {
            **primary,
            "role_models": {"strategy.interpretation": primary},
            "role_model_fallbacks": {"strategy.interpretation": [fallback]},
        }
        calls = []

        class StubClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                raise ModelCallError(
                    "model HTTP request failed with status 429",
                    outcome_known=True, attempts=1, status_code=429,
                    provider_error_kind="quota_exhausted",
                )

        with patch("scisaurus.runtime.scientific_interpretation.ModelClient", StubClient):
            with self.assertRaises(ModelCallError) as caught:
                ScientificInterpretationRunner(config).run(
                    {"evidence_ids": ["finding-1"]}, evidence_ids={"finding-1"},
                )
        self.assertEqual(calls, [primary["model"]])
        self.assertEqual(caught.exception.provider_error_kind, "quota_exhausted")

    def test_provider_quota_never_uses_local_cooldown_fallback(self):
        cloud = {
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "deepseek-v4.1-flash:cloud", "auth_env": None,
            "context_window_tokens": 262144, "max_input_tokens": 245760,
            "max_output_tokens": 8192, "timeout_seconds": 60,
        }
        local = {
            **cloud, "model": "gemma-local",
            "provider_quota_scope": "ollama-local",
        }
        config = {
            **cloud,
            "role_models": {"strategy.interpretation": cloud},
            "role_model_fallbacks": {"strategy.interpretation": [
                {**cloud, "model": "glm-5.3-flash:cloud"},
                {**cloud, "model": "gemma4:31b-cloud"},
            ]},
            "provider_cooldown_fallback": {
                "id": "ollama-local-cooldown-recovery",
                "pool": "ollama", **local,
            },
        }
        calls = []

        class StubClient:
            def __init__(self, **route):
                self.route = route

            def complete(self, *, system, prompt, images=None):
                calls.append(self.route["model"])
                if self.route["model"] != local["model"]:
                    raise ModelCallError(
                        "cloud account quota exhausted", outcome_known=True,
                        attempts=1, status_code=429,
                        provider_error_kind="quota_exhausted",
                    )
                return ModelResult(
                    text=json.dumps(interpretation()), model=self.route["model"],
                    usage={"input_tokens": 31, "output_tokens": 19},
                    elapsed_seconds=0.1, finish_reason="stop", request_attempts=1,
                )

        with patch("scisaurus.runtime.scientific_interpretation.ModelClient", StubClient):
            with self.assertRaises(ModelCallError) as caught:
                ScientificInterpretationRunner(config).run(
                    {"evidence_ids": ["finding-1"]}, evidence_ids={"finding-1"},
                )

        self.assertEqual(calls, [cloud["model"]])
        self.assertEqual(caught.exception.provider_error_kind, "quota_exhausted")

    def test_cooldown_fallback_stays_last_when_a_peer_is_preferred(self):
        from scisaurus.runtime.models import model_route_candidates

        cloud = {
            "protocol": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "deepseek-v4.1-flash:cloud", "auth_env": None,
        }
        config = {
            **cloud,
            "role_models": {"strategy.argument": cloud},
            "role_model_fallbacks": {"strategy.argument": [
                {**cloud, "model": "glm-5.3-flash:cloud"},
            ]},
            "provider_cooldown_fallback": {
                "id": "ollama-local-cooldown-recovery", "pool": "ollama",
                **cloud, "model": "gemma-local",
                "provider_quota_scope": "ollama-local",
            },
        }
        routes = model_route_candidates(
            config, role="strategy.argument", prefer_fallback=True,
            include_cooldown_fallback=True)
        self.assertEqual([route["model"] for route in routes], [
            "glm-5.3-flash:cloud", "deepseek-v4.1-flash:cloud", "gemma-local",
        ])


if __name__ == "__main__":
    unittest.main()
