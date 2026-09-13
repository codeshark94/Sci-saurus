import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.topic_discovery import (
    SCHEMA_VERSION,
    STAGE_CONFIG_SCHEMA_VERSION,
    TopicDiscoveryRunner,
    topic_signature,
    validate_topic_package,
    validate_topic_novelty,
    validate_topic_stage_config,
)


def package(objective):
    candidates = []
    for index in range(3):
        candidates.append({
            "id": f"direction_{index}",
            "title": f"Direction {index}",
            "domain": "computational science",
            "research_question": f"Does mechanism {index} change the measured outcome under a controlled comparison?",
            "scope": "Public data and a reproducible local experiment.",
            "search_queries": [f"mechanism {index} comparison", "controlled computational experiment", "reproducible public data"],
            "why_promising": "The sampled recent records suggest a testable comparison without establishing novelty.",
            "disconfirmation_test": "Discard this direction if the comparison cannot be measured reproducibly.",
            "feasibility": "The declared Python runtime and project tools can execute the bounded comparison.",
            "resource_plan": "Use public scholarly records, Python numerical libraries, and project-local generated results.",
            "capability_requirements": {"executables": [], "python_packages": [], "stage_kinds": []},
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "objective": objective,
        "candidates": candidates,
        "selected_id": "direction_1",
        "selection_rationale": "Direction 1 has the clearest measurement and the lowest dependence on unavailable resources.",
    }


class FakeOpenAlex:
    queries = []

    def __init__(self, **config):
        self.config = config

    def run(self, *, operation, query, limit, cursor):
        self.queries.append(query)
        return {
            "outcome": "ok",
            "works": [{
                "id": f"W{1000 + i}", "title": f"Recent paper {i}",
                "year": 2025 if i % 2 else 2022, "abstract": "A scholarly abstract.",
                "doi": None, "locations": [],
            } for i in range(10)],
        }


class FakeCrossref:
    def __init__(self, **config):
        self.config = config

    def search(self, query, *, limit=5, cursor=None):
        return {
            "outcome": "ok",
            "source_url": "https://api.crossref.org/works",
            "capture_sha256": "a" * 64,
            "metadata": {"provider": "crossref", "http_status": 200},
            "sources": [{
                "doi": "10.1234/topic-fallback",
                "title": "A feasible topic record",
                "published": {"date-parts": [[2025]]},
                "source_url": "https://doi.org/10.1234/topic-fallback",
                "abstract": "A scholarly abstract.",
            }],
        }


class FailedCrossref(FakeCrossref):
    def search(self, query, *, limit=5, cursor=None):
        return {"outcome": "rate_limited", "sources": [], "metadata": {}}


class FakeModel:
    def __init__(self, **config):
        self.config = config

    def complete(self, *, system, prompt, images=None):
        objective = json.loads(prompt)["principal_objective"]
        return ModelResult(text=json.dumps(package(objective)), model="fake",
                           usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                           elapsed_seconds=0.01, finish_reason="stop")


class TopicDiscoveryTests(unittest.TestCase):
    def test_config_and_package_contracts(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            model = root / "model.json"
            model.write_text("{}")
            config = {
                "schema_version": STAGE_CONFIG_SCHEMA_VERSION,
                "model_config_path": str(model.resolve()),
                "output_path": str((root / "topic.json").resolve()),
                "candidate_count": 3,
                "max_attempts": 2,
            }
            self.assertEqual(validate_topic_stage_config(config), config)
            value = package("Choose a feasible research direction")
            self.assertEqual(validate_topic_package(value, objective=value["objective"], candidate_count=3), value)

    def test_samples_recent_records_with_unicode_objective_and_reproducible_shuffle(self):
        FakeOpenAlex.queries = []
        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex):
            first = TopicDiscoveryRunner._recent_paper_sample("최근 기후 모델 비교", sampling_seed=17)
            second = TopicDiscoveryRunner._recent_paper_sample("최근 기후 모델 비교", sampling_seed=17)
        self.assertEqual(first, second)
        self.assertTrue(any("최근" in query for query in FakeOpenAlex.queries))
        self.assertEqual(len(first[0]), 10)
        self.assertEqual(first[1], 17)
        self.assertEqual(len(first[2]), 4)
        self.assertTrue(all(item["year"] >= 2022 for item in first[0]))

    def test_runner_includes_runtime_context_and_sample_provenance(self):
        objective = "Choose a feasible research direction"
        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex), \
                patch("scisaurus.runtime.topic_discovery.ModelClient", FakeModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run(objective, candidate_count=3, runtime_context={"python_packages": {"numpy": True}},
                  sampling_seed=9)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected_id"], "direction_1")
        self.assertEqual(result["sampling_seed"], 9)
        self.assertTrue(result["recent_papers"])
        self.assertEqual(result["feasibility_check"]["status"], "feasible")

    def test_unavailable_selected_capability_is_rejected(self):
        value = package("Choose a feasible research direction")
        value["candidates"][1]["capability_requirements"] = {
            "executables": ["definitely_missing_binary"], "python_packages": [], "stage_kinds": []}
        with self.assertRaisesRegex(ValidationError, "unavailable capabilities"):
            from scisaurus.runtime.topic_discovery import validate_topic_feasibility
            validate_topic_feasibility(value, {"executables": {}, "python_packages": {}, "configured_stage_kinds": []})

    def test_catalog_bound_candidates_must_name_an_available_experiment(self):
        value = package("Choose a feasible research direction")
        for candidate in value["candidates"]:
            candidate["experiment_capability_id"] = "cap_a"
        self.assertEqual(
            validate_topic_package(value, objective=value["objective"], candidate_count=3,
                                   experiment_capability_ids={"cap_a"}), value)
        value["candidates"][0]["experiment_capability_id"] = "cap_missing"
        with self.assertRaisesRegex(ValidationError, "configured experiment capability"):
            validate_topic_package(value, objective=value["objective"], candidate_count=3,
                                   experiment_capability_ids={"cap_a"})

    def test_catalog_bound_candidates_must_cover_the_available_portfolio(self):
        value = package("Choose a feasible research direction")
        for candidate in value["candidates"]:
            candidate["experiment_capability_id"] = "cap_a"
        with self.assertRaisesRegex(ValidationError, "distinct experiment capabilities"):
            validate_topic_package(value, objective=value["objective"], candidate_count=3,
                                   experiment_capability_ids={"cap_a", "cap_b", "cap_c"},
                                   require_capability_coverage=True)
        value["candidates"][1]["experiment_capability_id"] = "cap_b"
        value["candidates"][2]["experiment_capability_id"] = "cap_c"
        validate_topic_package(value, objective=value["objective"], candidate_count=3,
                               experiment_capability_ids={"cap_a", "cap_b", "cap_c"},
                               require_capability_coverage=True)

    def test_exploration_history_cannot_select_an_excluded_direction(self):
        value = package("Choose a feasible research direction")
        for candidate in value["candidates"]:
            candidate["experiment_capability_id"] = "cap_a"
        with self.assertRaisesRegex(ValidationError, "excluded"):
            validate_topic_package(value, objective=value["objective"], candidate_count=3,
                                   experiment_capability_ids={"cap_a"},
                                   excluded_capability_ids={"cap_a"})

    def test_topic_history_rejects_same_question_with_new_identifier(self):
        value = package("Choose a feasible research direction")
        selected = value["candidates"][1]
        prior = {
            "topic_id": "old_direction",
            "title": selected["title"],
            "domain": selected["domain"],
            "research_question": selected["research_question"],
            "signature": topic_signature(selected),
        }
        selected["id"] = "new_direction"
        with self.assertRaisesRegex(ValidationError, "too similar|repeats"):
            validate_topic_novelty(selected, {"entries": [prior]})

    def test_topic_history_keeps_different_questions_in_same_portfolio(self):
        value = package("Choose a feasible research direction")
        prior = {
            "topic_id": "old_direction",
            "title": "Robust estimator under contamination",
            "domain": "robust statistics",
            "research_question": "Does a median-of-means estimator reduce contaminated tail error?",
            "experiment_capability_id": "robust_mean",
        }
        candidate = {
            **value["candidates"][0],
            "id": "different_direction",
            "title": "Clean-data efficiency penalty",
            "domain": "robust statistics",
            "research_question": "What is the finite-sample variance cost of median-of-means on clean Gaussian data?",
            "experiment_capability_id": "robust_mean",
        }
        self.assertTrue(validate_topic_novelty(candidate, {"entries": [prior]}))

    def test_runner_retries_instead_of_accepting_a_historical_repeat(self):
        objective = "Choose a feasible research direction"
        selected = package(objective)["candidates"][1]
        history = {"entries": [{
            "topic_id": "old_direction", "title": selected["title"],
            "domain": selected["domain"], "research_question": selected["research_question"],
            "signature": topic_signature(selected),
        }]}
        with patch("scisaurus.runtime.topic_discovery.ModelClient", FakeModel):
            with self.assertRaisesRegex(ValidationError, "too similar|repeats"):
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                    "timeout_seconds": 1, "max_output_tokens": 4096,
                }).run(objective, candidate_count=3, bibliography=False,
                       runtime_context={"topic_history": history}, max_attempts=1)

    def test_provider_failure_does_not_turn_into_a_fabricated_topic(self):
        class FailedOpenAlex(FakeOpenAlex):
            def run(self, **kwargs):
                return {"outcome": "rate_limited", "error": "429", "works": []}

        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FailedOpenAlex), \
                patch("scisaurus.runtime.topic_discovery.CrossrefClient", FailedCrossref):
            with self.assertRaisesRegex(ValidationError, "sampling failed"):
                TopicDiscoveryRunner._recent_paper_sample("feasible science")

    def test_rate_limited_openalex_can_seed_topic_intake_from_crossref(self):
        class FailedOpenAlex:
            def __init__(self, **config):
                pass

            def run(self, **kwargs):
                return {"outcome": "rate_limited", "works": [], "metadata": {}}

        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FailedOpenAlex), \
                patch("scisaurus.runtime.topic_discovery.CrossrefClient", FakeCrossref):
            records, seed, trace = TopicDiscoveryRunner._recent_paper_sample("feasible science", sampling_seed=3)
        self.assertEqual(seed, 3)
        self.assertTrue(records)
        self.assertTrue(any(item.get("provider") == "crossref" for item in trace))


if __name__ == "__main__":
    unittest.main()
