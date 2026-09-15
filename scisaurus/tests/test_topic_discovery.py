import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.errors import QuotaExceededError, ValidationError
from scisaurus.runtime.literature import ProviderCooldownError
from scisaurus.runtime.models import ModelCallError, ModelResult
from scisaurus.runtime.topic_discovery import (
    SCHEMA_VERSION,
    STAGE_CONFIG_SCHEMA_VERSION,
    TopicBudget,
    TopicDiscoveryRunner,
    topic_maturity_admitted,
    topic_signature,
    validate_topic_package,
    validate_topic_maturity_review,
    validate_topic_novelty,
    validate_topic_stage_config,
    validate_frontier_seed_plan,
    validate_source_challenge,
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


def frontier_plan(count=6):
    domains = ["marine ecology", "soft matter", "plant hydraulics", "acoustics",
               "geomorphology", "microbial evolution", "atmospheric chemistry", "neuroscience"]
    return {
        "schema_version": "topic-frontier-seeds-1",
        "seeds": [{
            "id": f"frontier_{index}", "domain": domains[index],
            "phenomenon": f"domain-specific transition {index}",
            "mechanism": f"competing transport mechanism {index}",
            "unit_of_analysis": f"observed unit {index}",
            "search_queries": [
                f"{domains[index]} transition mechanism",
                f"{domains[index]} boundary scaling",
            ],
        } for index in range(count)],
    }


class FakeOpenAlex:
    queries = []

    def __init__(self, **config):
        self.config = config

    def run(self, *, operation, query, limit, cursor):
        self.queries.append(query)
        prefix = int(hashlib.sha256(query.encode()).hexdigest()[:8], 16) * 100
        return {
            "outcome": "ok",
            "works": [{
                "id": f"W{prefix + i + 1}", "title": f"{query} paper {i}",
                "year": 2025 if i % 2 else 2022,
                "abstract": f"A scholarly abstract about {query}.",
                "doi": None, "locations": [],
            } for i in range(10)],
            "metadata": {"provider": "openalex", "http_status": 200},
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
        payload = json.loads(prompt)
        if payload.get("assignment") == "science_first_frontier_seed_generation":
            value = frontier_plan(payload["seed_count"])
        elif payload.get("assignment") == "topic_source_and_template_challenge":
            value = {
                "schema_version": "topic-source-challenge-1",
                "decision": "admit_to_survey",
                "selected_id": payload["selected_topic"]["id"],
                "source_relevance": 4, "template_independence": 4,
                "prior_work_risk": "low",
                "closest_work_ids": [payload["targeted_scholarly_records"][0]["work_id"]],
                "rationale": "The supplied records are relevant and the question tests a distinct mechanism.",
                "required_changes": [],
            }
        else:
            value = package(payload["principal_objective"])
            seeds = payload.get("frontier_seeds") or []
            papers = payload.get("recent_papers") or []
            if seeds and papers:
                papers_by_seed = {}
                for paper in papers:
                    papers_by_seed.setdefault(paper["frontier_seed_id"], paper)
                grounded = [seed for seed in seeds if seed["id"] in papers_by_seed][:3]
                for candidate, seed in zip(value["candidates"], grounded):
                    paper = papers_by_seed[seed["id"]]
                    candidate.update({
                        "title": f"{seed['domain']} {seed['phenomenon']}",
                        "domain": seed["domain"],
                        "research_question": (
                            f"Does {seed['mechanism']} alter {seed['phenomenon']} "
                            f"for {seed['unit_of_analysis']}?"),
                        "scope": f"Bounded observations of {seed['unit_of_analysis']}.",
                        "search_queries": [
                            seed["search_queries"][0], seed["search_queries"][1],
                            f"{seed['domain']} {seed['phenomenon']}",
                        ],
                        "frontier_seed_id": seed["id"],
                        "prior_work_ids": [paper["work_id"]],
                    })
        return ModelResult(text=json.dumps(value), model="fake",
                           usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                           elapsed_seconds=0.01, finish_reason="stop")


class MaturityModel:
    """Return a thin first proposal, then a substantively refined proposal."""

    calls = []
    review_count = 0

    def __init__(self, **config):
        self.config = config

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        self.calls.append(payload.get("assignment"))
        if payload.get("assignment") == "topic_maturity_review":
            type(self).review_count += 1
            decision = "refine" if type(self).review_count == 1 else "admit"
            review = {
                "decision": decision,
                "selected_id": "direction_1",
                "scores": {
                    "question_specificity": 4,
                    "mechanism_depth": 3 if decision == "admit" else 1,
                    "comparison_design": 4,
                    "contribution_potential": 3 if decision == "admit" else 1,
                    "falsifiability": 4,
                },
                "rationale": "The refined direction distinguishes a mechanism under a controlled comparison.",
                "required_changes": [] if decision == "admit" else [
                    "Vary the data regime and make the mechanism discriminating."],
                "changed_dimensions": [] if decision == "admit" else ["data_regime", "mechanism"],
            }
            return ModelResult(text=json.dumps(review), model="fake",
                               usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                               elapsed_seconds=0.01, finish_reason="stop")
        objective = payload["principal_objective"]
        value = package(objective)
        if payload.get("assignment") == "refine_topic_discovery":
            value["candidates"][1]["title"] = "Mechanism-sensitive outcome stability"
            value["candidates"][1]["research_question"] = (
                "Does mechanism 1 change the measured outcome across clean and contaminated regimes, "
                "and which regime separates the competing explanations?")
            value["candidates"][1]["scope"] = (
                "Public data and a reproducible local experiment spanning clean and contaminated regimes.")
        return ModelResult(text=json.dumps(value), model="fake",
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
                "budgets": {"max_model_calls": 4, "max_openalex_requests": 5},
            }
            self.assertEqual(validate_topic_stage_config(config), config)
            value = package("Choose a feasible research direction")
            self.assertEqual(validate_topic_package(value, objective=value["objective"], candidate_count=3), value)

    def test_maturity_review_contract_requires_substantive_admission(self):
        review = {
            "decision": "admit",
            "selected_id": "direction_1",
            "scores": {dimension: 3 for dimension in (
                "question_specificity", "mechanism_depth", "comparison_design",
                "contribution_potential", "falsifiability")},
            "rationale": "The question has a discriminating comparison and a falsifiable outcome.",
            "required_changes": [],
            "changed_dimensions": [],
        }
        self.assertEqual(validate_topic_maturity_review(review, candidate_ids={"direction_1"}), review)
        self.assertTrue(topic_maturity_admitted(review))
        review["scores"]["mechanism_depth"] = 1
        self.assertFalse(topic_maturity_admitted(review))

    def test_runner_refines_topic_after_maturity_review(self):
        objective = "Choose a feasible research direction"
        MaturityModel.calls = []
        MaturityModel.review_count = 0
        with patch("scisaurus.runtime.topic_discovery.ModelClient", MaturityModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run(objective, candidate_count=3, bibliography=False,
                   maturity_review_rounds=1, max_attempts=4)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(MaturityModel.calls,
                         ["free_topic_discovery", "topic_maturity_review",
                          "refine_topic_discovery", "topic_maturity_review"])
        self.assertEqual(len(result["maturity_reviews"]), 2)
        self.assertEqual(len(result["maturity_review_history"]), 2)
        self.assertEqual(result["maturity_review_history"][0]["review"]["decision"], "refine")
        self.assertEqual(result["maturity_score"], 18)
        self.assertIn("across clean and contaminated regimes", result["question"])

    def test_samples_recent_records_with_unicode_objective_and_reproducible_shuffle(self):
        FakeOpenAlex.queries = []
        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex):
            first = TopicDiscoveryRunner._recent_paper_sample(
                "최근 기후 모델 비교", sampling_seed=17, frontier_seed_plan=frontier_plan())
            second = TopicDiscoveryRunner._recent_paper_sample(
                "최근 기후 모델 비교", sampling_seed=17, frontier_seed_plan=frontier_plan())
        self.assertEqual(first, second)
        self.assertTrue(any("marine ecology" in query for query in FakeOpenAlex.queries))
        self.assertEqual(len(first[0]), 12)
        self.assertEqual(first[1], 17)
        self.assertEqual(len(first[2]), 6)
        self.assertTrue(all(item["year"] >= 2022 for item in first[0]))
        self.assertGreaterEqual(len({item["frontier_seed_id"] for item in first[0]}), 4)

    def test_topic_budget_stops_openalex_before_the_next_network_request(self):
        FakeOpenAlex.queries = []
        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex):
            with self.assertRaisesRegex(QuotaExceededError, "openalex_requests"):
                TopicDiscoveryRunner._recent_paper_sample(
                    "bounded science", sampling_seed=17, frontier_seed_plan=frontier_plan(),
                    budget=TopicBudget({"max_openalex_requests": 1}, {}))
        self.assertEqual(len(FakeOpenAlex.queries), 1)

    def test_failed_model_attempts_leave_usage_and_event_diagnostics(self):
        class AlwaysUnavailableModel:
            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                raise ModelCallError(
                    "provider unavailable", outcome_known=False,
                    attempts=2, elapsed_seconds=0.25)

        with patch("scisaurus.runtime.topic_discovery.ModelClient", AlwaysUnavailableModel):
            with self.assertRaises(ValidationError) as caught:
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                    "timeout_seconds": 1, "max_output_tokens": 4096,
                }).run(
                    "Choose a feasible research direction", candidate_count=3,
                    bibliography=False, max_attempts=3,
                    budgets={"max_model_calls": 3},
                )
        snapshot = caught.exception.topic_budget
        self.assertEqual(snapshot["usage"]["model_calls"], 3)
        self.assertEqual(len(snapshot["events"]), 3)
        self.assertTrue(all(event["status"] == "error" for event in snapshot["events"]))
        self.assertTrue(all(event["request_attempts"] == 2 for event in snapshot["events"]))

    def test_topic_sampling_rejects_single_token_provider_false_positives(self):
        class MixedRelevanceOpenAlex:
            def __init__(self, **config):
                pass

            def run(self, *, operation, query, limit, cursor):
                prefix = int(hashlib.sha256(query.encode()).hexdigest()[:8], 16) * 100
                first = query.split()[0]
                return {
                    "outcome": "ok",
                    "works": [
                        {"id": f"W{prefix + 1}", "title": f"{first} unrelated catalog",
                         "year": 2025, "abstract": "A generic bibliographic record.",
                         "doi": None, "locations": []},
                        {"id": f"W{prefix + 2}", "title": query,
                         "year": 2025, "abstract": f"A focused study of {query}.",
                         "doi": None, "locations": []},
                    ],
                    "metadata": {"provider": "openalex", "http_status": 200},
                }

        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", MixedRelevanceOpenAlex):
            papers, _, trace = TopicDiscoveryRunner._recent_paper_sample(
                "feasible science", sampling_seed=3, frontier_seed_plan=frontier_plan())
        self.assertEqual(len(papers), 6)
        self.assertTrue(all(item["work_id"].endswith("2") for item in papers))
        self.assertTrue(all(len(item["irrelevant_work_ids"]) == 1 for item in trace))
        self.assertTrue(all(item["required_query_token_matches"] == 2 for item in trace))

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
        self.assertGreaterEqual(
            len({item["frontier_seed_id"] for item in result["candidates"]}), 3)
        self.assertTrue(all(item["prior_work_ids"] for item in result["candidates"]))

    def test_grounded_portfolio_rejects_invented_sources_and_seed_collapse(self):
        value = package("Choose a feasible research direction")
        seeds = frontier_plan(3)["seeds"]
        papers = []
        for index, seed in enumerate(seeds):
            work_id = f"W{index + 1}"
            papers.append({
                "work_id": work_id, "title": seed["search_queries"][0],
                "abstract": f"Evidence about {seed['mechanism']} and {seed['phenomenon']}.",
                "frontier_seed_id": seed["id"],
            })
            candidate = value["candidates"][index]
            candidate.update({
                "title": f"{seed['domain']} transition",
                "domain": seed["domain"],
                "research_question": f"Does {seed['mechanism']} change {seed['phenomenon']}?",
                "scope": f"Observed {seed['unit_of_analysis']}.",
                "search_queries": [*seed["search_queries"], f"{seed['domain']} transition"],
                "frontier_seed_id": seed["id"], "prior_work_ids": [work_id],
            })
        validate_topic_package(
            value, objective=value["objective"], candidate_count=3,
            frontier_seeds=seeds, recent_papers=papers, require_grounding=True)
        value["candidates"][0]["prior_work_ids"] = ["W999"]
        with self.assertRaisesRegex(ValidationError, "outside the supplied evidence"):
            validate_topic_package(
                value, objective=value["objective"], candidate_count=3,
                frontier_seeds=seeds, recent_papers=papers, require_grounding=True)

    def test_grounded_portfolio_rejects_near_duplicate_questions(self):
        value = package("Choose a feasible research direction")
        seeds = frontier_plan(3)["seeds"]
        papers = []
        for index, seed in enumerate(seeds):
            work_id = f"W{index + 1}"
            papers.append({
                "work_id": work_id, "title": seed["search_queries"][0],
                "abstract": f"Evidence about {seed['mechanism']} and {seed['phenomenon']}.",
                "frontier_seed_id": seed["id"],
            })
            candidate = value["candidates"][index]
            candidate.update({
                "domain": seed["domain"],
                "research_question": "Does the same mechanism alter the same measured outcome?",
                "scope": f"Observed {seed['unit_of_analysis']}.",
                "search_queries": [*seed["search_queries"], f"{seed['domain']} transition"],
                "frontier_seed_id": seed["id"], "prior_work_ids": [work_id],
                "experiment_capability_id": "shared_capability",
            })
        with self.assertRaisesRegex(ValidationError, "near-duplicate"):
            validate_topic_package(
                value, objective=value["objective"], candidate_count=3,
                experiment_capability_ids={"shared_capability"},
                frontier_seeds=seeds, recent_papers=papers, require_grounding=True)

    def test_selected_topic_cannot_paraphrase_a_hidden_fallback_template(self):
        value = package("Choose a feasible research direction")
        selected = value["candidates"][1]
        template = {"id": "fixed_template", "domain": selected["domain"],
                    "research_question": selected["research_question"]}
        with self.assertRaisesRegex(ValidationError, "fallback experiment template"):
            validate_topic_package(
                value, objective=value["objective"], candidate_count=3,
                fallback_templates=[template])

    def test_admitted_source_challenge_requires_a_closest_work(self):
        review = {
            "schema_version": "topic-source-challenge-1", "decision": "admit_to_survey",
            "selected_id": "direction_1", "source_relevance": 4,
            "template_independence": 4, "prior_work_risk": "low",
            "closest_work_ids": [], "rationale": "Relevant bounded evidence.",
            "required_changes": [],
        }
        with self.assertRaisesRegex(ValidationError, "at least one closest"):
            validate_source_challenge(review, selected_id="direction_1", work_ids=["W1"])

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

    def test_design_driven_capability_requires_a_valid_experiment_design(self):
        design = {
            "family": "monte_carlo_estimator_comparison",
            "data_process": {"kind": "student_t", "sample_size": 300, "df": 4.0},
            "estimators": ["mean", "median", "trimmed_mean"],
            "primary": "trimmed_mean", "baseline": "mean",
            "trim_fraction": 0.1, "seed": 11,
        }
        value = package("Choose a feasible research direction")
        for candidate in value["candidates"]:
            candidate["experiment_capability_id"] = "design_driven"
        with self.assertRaisesRegex(ValidationError, "requires an experiment_design"):
            validate_topic_package(value, objective=value["objective"], candidate_count=3,
                                   experiment_capability_ids={"design_driven"},
                                   design_driven_capability_ids={"design_driven"})
        for candidate in value["candidates"]:
            candidate["experiment_design"] = json.loads(json.dumps(design))
        validate_topic_package(value, objective=value["objective"], candidate_count=3,
                               experiment_capability_ids={"design_driven"},
                               design_driven_capability_ids={"design_driven"})
        value["candidates"][0]["experiment_design"]["estimators"] = ["mean", "quantile"]
        with self.assertRaisesRegex(ValidationError, "estimators"):
            validate_topic_package(value, objective=value["objective"], candidate_count=3,
                                   experiment_capability_ids={"design_driven"},
                                   design_driven_capability_ids={"design_driven"})
        value["candidates"][0]["experiment_design"] = json.loads(json.dumps(design))
        for candidate in value["candidates"]:
            candidate["experiment_capability_id"] = "fixed"
        with self.assertRaisesRegex(ValidationError, "does not accept one"):
            validate_topic_package(value, objective=value["objective"], candidate_count=3,
                                   experiment_capability_ids={"fixed"},
                                   design_driven_capability_ids={"design_driven"})


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

        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FailedOpenAlex):
            with self.assertRaisesRegex(ValidationError, "source-diversity floor"):
                TopicDiscoveryRunner._recent_paper_sample(
                    "feasible science", sampling_seed=3, frontier_seed_plan=frontier_plan())

    def test_rate_limited_openalex_fails_closed_without_crossref_topic_admission(self):
        class FailedOpenAlex:
            def __init__(self, **config):
                pass

            def run(self, **kwargs):
                return {"outcome": "rate_limited", "works": [], "metadata": {}}

        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FailedOpenAlex):
            with self.assertRaisesRegex(ValidationError, "source-diversity floor"):
                TopicDiscoveryRunner._recent_paper_sample(
                    "feasible science", sampling_seed=3, frontier_seed_plan=frontier_plan())

    def test_rate_limited_topic_propagates_provider_reset_boundary(self):
        class FailedOpenAlex:
            def __init__(self, **config):
                pass

            def run(self, **kwargs):
                return {
                    "outcome": "rate_limited", "works": [], "error": "budget exhausted",
                    "metadata": {"rate_limit": {
                        "kind": "daily_budget", "retry_after_seconds": 321,
                        "reset": 321, "remaining": 0,
                    }},
                }

        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FailedOpenAlex):
            with self.assertRaises(ProviderCooldownError) as caught:
                TopicDiscoveryRunner._recent_paper_sample(
                    "feasible science", sampling_seed=3, frontier_seed_plan=frontier_plan())
        self.assertEqual(caught.exception.retry_after_seconds, 321)

    def test_frontier_seed_plan_rejects_mission_boilerplate_queries(self):
        value = frontier_plan()
        self.assertEqual(validate_frontier_seed_plan(value, seed_count=6), value)
        value["seeds"][0]["search_queries"][0] = "autonomous research workflow"
        with self.assertRaisesRegex(ValidationError, "mission boilerplate"):
            validate_frontier_seed_plan(value, seed_count=6)

    def test_frontier_query_must_be_anchored_to_its_seed(self):
        value = frontier_plan()
        value["seeds"][0]["search_queries"][0] = "quantum entanglement entropy"
        with self.assertRaisesRegex(ValidationError, "anchored"):
            validate_frontier_seed_plan(value, seed_count=6)


if __name__ == "__main__":
    unittest.main()
