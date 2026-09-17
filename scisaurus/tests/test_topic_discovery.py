import json
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.errors import QuotaExceededError, ValidationError
from scisaurus.runtime.literature import ProviderCooldownError
from scisaurus.runtime.models import ModelCallError, ModelResult, estimate_input_tokens
from scisaurus.runtime.topic_discovery import (
    MAX_BOUNDED_TOPIC_ATTEMPTS,
    SYSTEM,
    PORTFOLIO_DIMENSIONS,
    SCHEMA_VERSION,
    STAGE_CONFIG_SCHEMA_VERSION,
    TopicBudget,
    TopicDiscoveryRunner,
    topic_maturity_admitted,
    topic_maturity_survey_eligible,
    topic_portfolio_profile,
    topic_refinement_dimensions,
    topic_signature,
    validate_topic_package,
    validate_topic_maturity_review,
    validate_topic_novelty,
    validate_topic_portfolio,
    validate_topic_refinement,
    validate_topic_stage_config,
    validate_frontier_seed_plan,
    validate_source_challenge,
    topic_prompt,
    _anchor_frontier_seed_queries,
    _anchor_topic_candidate_queries,
    _grounding_eligible_frontier_seeds,
    _merge_candidate_source_records,
    _materialize_foundry_capability_requirements,
    _materialize_foundry_feasibility,
    _materialize_seed_bindings,
    _materialize_seed_domains,
    _materialize_topic_objective,
    _portfolio_shape_plan,
    _repair_foundry_selection,
    _repair_topic_novelty_selection,
    _repair_topic_refinement_selection,
    _source_challenge_requires_frontier_seed_pivot,
    _topic_retry_reason,
)


def package(objective):
    candidates = []
    research_forms = ("theory_simulation", "observational_reanalysis", "methodological_benchmark")
    evidence_modes = ("synthetic_simulation", "published_observations", "public_dataset")
    comparison_types = ("mechanism_ablation", "cross_method", "model_selection")
    for index in range(3):
        candidates.append({
            "id": f"direction_{index}",
            "title": f"Direction {index}",
            "domain": "computational science",
            "research_question": f"Does mechanism {index} change the measured outcome under a controlled comparison?",
            "research_form": research_forms[index],
            "evidence_mode": evidence_modes[index],
            "comparison_type": comparison_types[index],
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


class InvalidCandidateShapeModel(FakeModel):
    """Return a scientifically usable package with one contract-only key."""

    def complete(self, *, system, prompt, images=None):
        result = super().complete(system=system, prompt=prompt, images=images)
        value = json.loads(result.text)
        if isinstance(value.get("candidates"), list):
            value["candidates"][0]["mechanism_boundary"] = (
                "This must be represented by a declared boundary field.")
        return ModelResult(
            text=json.dumps(value), model="fake",
            usage=result.usage, elapsed_seconds=result.elapsed_seconds,
            finish_reason=result.finish_reason)


class MissingResearchQuestionModel(FakeModel):
    """Omit one required field, then return only its targeted patch."""

    assignments = []

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        type(self).assignments.append(payload.get("assignment"))
        if payload.get("assignment") == "repair_missing_topic_fields":
            patches = [{
                "id": item["id"],
                "fields": {
                    "research_question": (
                        f"Does {item['candidate_fields'].get('mechanism', 'the mechanism')} change "
                        f"{item['candidate_fields'].get('measurement', 'the measured outcome')} under the declared "
                        f"{item['candidate_fields'].get('comparison_type', 'controlled')} comparison?"
                    ),
                },
            } for item in payload["candidate_context"]]
            return ModelResult(
                text=json.dumps({"candidate_patches": patches}), model="fake",
                usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                elapsed_seconds=0.01, finish_reason="stop")
        result = super().complete(system=system, prompt=prompt, images=images)
        value = json.loads(result.text)
        value["candidates"][0].pop("research_question")
        return ModelResult(
            text=json.dumps(value), model="fake",
            usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
            elapsed_seconds=0.01, finish_reason="stop")


class MissingTitleModel(FakeModel):
    """Omit a candidate title, then return only the targeted title patch."""

    assignments = []

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        type(self).assignments.append(payload.get("assignment"))
        if payload.get("assignment") == "repair_missing_topic_fields":
            patches = [{
                "id": item["id"],
                "fields": {"title": f"Working title for {item['candidate_fields']['domain']}"},
            } for item in payload["candidate_context"]]
            return ModelResult(
                text=json.dumps({"candidate_patches": patches}), model="fake",
                usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                elapsed_seconds=0.01, finish_reason="stop")
        result = super().complete(system=system, prompt=prompt, images=images)
        value = json.loads(result.text)
        value["candidates"][0].pop("title")
        return ModelResult(
            text=json.dumps(value), model="fake",
            usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
            elapsed_seconds=0.01, finish_reason="stop")


class PortfolioRepairModel(FakeModel):
    """Return one structurally collapsed package, then a valid repair."""

    topic_calls = 0
    prompts = []

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        type(self).prompts.append(payload)
        result = super().complete(system=system, prompt=prompt, images=images)
        if payload.get("assignment") in {"free_topic_discovery", "repair_invalid_topic_discovery"}:
            type(self).topic_calls += 1
            if type(self).topic_calls == 1:
                value = json.loads(result.text)
                for candidate in value["candidates"]:
                    candidate.update({
                        "research_form": "theory_simulation",
                        "evidence_mode": "synthetic_simulation",
                        "comparison_type": "mechanism_ablation",
                    })
                result = ModelResult(
                    text=json.dumps(value), model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")
        return result


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
        if payload.get("assignment") == "repair_selected_topic_candidate":
            candidate = dict(payload["parent_candidate"])
            candidate.update(payload["required_shape"])
            candidate["research_question"] = (
                "Does mechanism 1 change the measured outcome across clean and contaminated regimes, "
                "and which regime separates the competing explanations?"
            )
            candidate["scope"] = (
                "Public data and a reproducible local experiment spanning clean and contaminated regimes."
            )
            return ModelResult(
                text=json.dumps({"candidate": candidate}), model="fake",
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


class MaturityMalformedRepairModel(MaturityModel):
    """Return an incomplete review once, then a complete bounded repair."""

    review_calls = 0
    assignments = []

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        type(self).assignments.append(payload.get("assignment"))
        if payload.get("assignment") in {
                "topic_maturity_review", "repair_topic_maturity_review"}:
            type(self).review_calls += 1
            if type(self).review_calls == 1:
                return ModelResult(text='{"decision":', model="fake",
                                   usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                                   elapsed_seconds=0.01, finish_reason="stop")
            review = {
                "decision": "admit", "selected_id": payload["selected_id_to_copy_exactly"],
                "scores": {
                    "question_specificity": 4, "mechanism_depth": 3,
                    "comparison_design": 4, "contribution_potential": 3,
                    "falsifiability": 4,
                },
                "rationale": "The bounded direction has a concrete mechanism and disconfirmation route.",
                "required_changes": [], "changed_dimensions": [],
            }
            return ModelResult(text=json.dumps(review), model="fake",
                               usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                               elapsed_seconds=0.01, finish_reason="stop")
        return super().complete(system=system, prompt=prompt, images=images)


class SurveyEligibleMaturityModel(MaturityModel):
    """Keep a candidate below the journal floor but above the survey floor."""

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        if payload.get("assignment") == "topic_maturity_review":
            type(self).calls.append(payload.get("assignment"))
            review = {
                "decision": "refine",
                "selected_id": payload["selected_id_to_copy_exactly"],
                "scores": {
                    "question_specificity": 3,
                    "mechanism_depth": 2,
                    "comparison_design": 2,
                    "contribution_potential": 2,
                    "falsifiability": 3,
                },
                "rationale": (
                    "The direction is concrete enough to investigate, but literature must sharpen "
                    "the mechanism and contribution before experiment admission."
                ),
                "required_changes": [
                    "Ground the mechanism in prior work.",
                    "Identify the strongest competing explanation.",
                ],
                "changed_dimensions": ["mechanism", "research_form"],
            }
            return ModelResult(
                text=json.dumps(review), model="fake",
                usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                elapsed_seconds=0.01, finish_reason="stop")
        return super().complete(system=system, prompt=prompt, images=images)


class SourceChallengeRefinementModel(FakeModel):
    """Reject the first source challenge, then admit its bounded repair."""

    calls = []
    payloads = []
    challenge_count = 0

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        self.calls.append(payload.get("assignment"))
        self.payloads.append(payload)
        if payload.get("assignment") == "topic_source_and_template_challenge":
            type(self).challenge_count += 1
            admitted = type(self).challenge_count > 1
            review = {
                "schema_version": "topic-source-challenge-1",
                "decision": "admit_to_survey" if admitted else "refine",
                "selected_id": payload["selected_topic"]["id"],
                "source_relevance": 4,
                "template_independence": 4 if admitted else 2,
                "prior_work_risk": "low" if admitted else "high",
                "closest_work_ids": [payload["targeted_scholarly_records"][0]["work_id"]],
                "rationale": "The first direction needs a changed boundary; the repaired direction is distinct.",
                "required_changes": [] if admitted else ["Change the mechanism and comparison boundary."],
            }
            return ModelResult(text=json.dumps(review), model="fake",
                               usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                               elapsed_seconds=0.01, finish_reason="stop")
        if payload.get("assignment") == "repair_selected_topic_candidate":
            candidate = dict(payload["parent_candidate"])
            candidate.update(payload["required_shape"])
            target_seed = next(
                seed for seed in payload["frontier_seeds"]
                if seed["id"] == payload["target_frontier_seed_id"])
            candidate.update({
                "domain": target_seed["domain"],
                "frontier_seed_id": target_seed["id"],
                "prior_work_ids": [payload["target_seed_records"][0]["work_id"]],
                "title": f"{target_seed['domain']} changed boundary",
                "research_question": (
                    f"Does {target_seed['mechanism']} alter the bounded observable "
                    f"under a changed comparison for {target_seed['unit_of_analysis']}?"
                ),
                "mechanism": target_seed["mechanism"],
                "comparison": "Compare the supplied mechanism against its stated boundary.",
                "measurement": target_seed["unit_of_analysis"],
            })
            return ModelResult(
                text=json.dumps({"candidate": candidate}), model="fake",
                usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                elapsed_seconds=0.01, finish_reason="stop")
        result = super().complete(system=system, prompt=prompt, images=images)
        if payload.get("assignment") == "refine_topic_discovery":
            value = json.loads(result.text)
            value["candidates"][1].update({
                "research_form": "scaling_boundary",
                "evidence_mode": "cross_source_synthesis",
                "comparison_type": "causal_contrast",
            })
            result = ModelResult(
                text=json.dumps(value), model="fake",
                usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                elapsed_seconds=0.01, finish_reason="stop")
        return result


class SourceChallengeInvalidIdRepairModel(FakeModel):
    """Repair a challenger response that cites an ID outside its evidence packet."""

    assignments = []
    challenge_count = 0

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        type(self).assignments.append(payload.get("assignment"))
        if payload.get("assignment") in {
                "topic_source_and_template_challenge", "repair_topic_source_challenge"}:
            type(self).challenge_count += 1
            allowed = payload["allowed_work_ids"]
            work_ids = ["W-not-supplied"] if type(self).challenge_count == 1 else [allowed[0]]
            review = {
                "schema_version": "topic-source-challenge-1",
                "decision": "admit_to_survey", "selected_id": payload["selected_topic"]["id"],
                "source_relevance": 4, "template_independence": 4, "prior_work_risk": "low",
                "closest_work_ids": work_ids,
                "rationale": "The supplied record supports a distinct bounded comparison.",
                "required_changes": [],
            }
            return ModelResult(text=json.dumps(review), model="fake",
                               usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                               elapsed_seconds=0.01, finish_reason="stop")
        return super().complete(system=system, prompt=prompt, images=images)


class RefinementValidationRepairModel(FakeModel):
    """Repair a refinement using the rejected package and validation error."""

    challenge_calls = 0
    refinement_calls = 0
    payloads = []

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        type(self).payloads.append(payload)
        if payload.get("assignment") == "topic_source_and_template_challenge":
            type(self).challenge_calls += 1
            admitted = type(self).challenge_calls > 1
            review = {
                "schema_version": "topic-source-challenge-1",
                "decision": "admit_to_survey" if admitted else "refine",
                "selected_id": payload["selected_topic"]["id"],
                "source_relevance": 4,
                "template_independence": 4 if admitted else 2,
                "prior_work_risk": "low" if admitted else "high",
                "closest_work_ids": [payload["targeted_scholarly_records"][0]["work_id"]],
                "rationale": "The first direction needs a substantive pivot; the repaired direction is distinct.",
                "required_changes": [] if admitted else [
                    "Change the research form and comparison boundary."
                ],
            }
            return ModelResult(text=json.dumps(review), model="fake",
                               usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                               elapsed_seconds=0.01, finish_reason="stop")
        if payload.get("assignment") == "repair_selected_topic_candidate":
            type(self).refinement_calls += 1
            candidate = dict(payload["parent_candidate"])
            candidate["research_question"] = (
                "Does the changed mechanism alter the observed transition under a bounded comparison?"
            )
            if type(self).refinement_calls > 1:
                candidate.update(payload["required_shape"])
                target_seed = next(
                    seed for seed in payload["frontier_seeds"]
                    if seed["id"] == payload["target_frontier_seed_id"])
                candidate.update({
                    "domain": target_seed["domain"],
                    "frontier_seed_id": target_seed["id"],
                    "prior_work_ids": [payload["target_seed_records"][0]["work_id"]],
                    "scope": "A bounded transition comparison across supplied records.",
                    "mechanism": target_seed["mechanism"],
                    "measurement": target_seed["unit_of_analysis"],
                })
            return ModelResult(
                text=json.dumps({"candidate": candidate}), model="fake",
                usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                elapsed_seconds=0.01, finish_reason="stop")
        result = super().complete(system=system, prompt=prompt, images=images)
        if payload.get("assignment") == "refine_topic_discovery":
            type(self).refinement_calls += 1
            value = json.loads(result.text)
            candidate = value["candidates"][1]
            candidate["research_question"] = (
                "Does the changed mechanism alter the observed transition under a bounded comparison?"
            )
            if type(self).refinement_calls > 1:
                candidate.update({
                    "research_form": "scaling_boundary",
                    "evidence_mode": "cross_source_synthesis",
                    "comparison_type": "causal_contrast",
                    "scope": "A bounded transition comparison across the supplied scholarly records.",
                })
            result = ModelResult(
                text=json.dumps(value), model="fake",
                usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                elapsed_seconds=0.01, finish_reason="stop")
        return result


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
            config["max_attempts"] = MAX_BOUNDED_TOPIC_ATTEMPTS
            self.assertEqual(validate_topic_stage_config(config), config)
            value = package("Choose a feasible research direction")
            self.assertEqual(validate_topic_package(value, objective=value["objective"], candidate_count=3), value)

    def test_frontier_seed_scaffolding_is_not_checked_as_reader_prose(self):
        plan = frontier_plan()
        plan["seeds"][0]["mechanism"] = "independent validator of competing transport"
        self.assertEqual(validate_frontier_seed_plan(plan), plan)

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

    def test_maturity_survey_floor_requires_substance_in_every_dimension(self):
        review = {
            "decision": "refine",
            "selected_id": "direction_1",
            "scores": {dimension: 2 for dimension in (
                "question_specificity", "mechanism_depth", "comparison_design",
                "contribution_potential", "falsifiability")},
            "rationale": "The question merits evidence gathering but is not experiment-ready.",
            "required_changes": ["Ground the mechanism."],
            "changed_dimensions": ["mechanism"],
        }
        self.assertTrue(topic_maturity_survey_eligible(review))
        review["scores"]["mechanism_depth"] = 1
        self.assertFalse(topic_maturity_survey_eligible(review))
        contradictory_admit = {
            **review,
            "decision": "admit",
            "scores": {dimension: 2 for dimension in review["scores"]},
            "required_changes": [],
        }
        self.assertFalse(topic_maturity_survey_eligible(contradictory_admit))

    def test_generated_direction_slot_id_does_not_block_a_different_question(self):
        prior = {
            "topic_id": "direction_2",
            "title": "Charging protection in Majorana islands",
            "domain": "topological superconductivity",
            "research_question": "Where does charging energy protect a zero-bias peak?",
            "research_form": "scaling_boundary",
            "evidence_mode": "analytical_derivation",
            "comparison_type": "cross_method",
        }
        candidate = {
            "id": "direction_2",
            "title": "Chemotactic colony branching threshold",
            "domain": "microbial ecology",
            "research_question": "Does membrane-potential coupling change branching in chemotactic bacterial colonies?",
            "research_form": "observational_reanalysis",
            "evidence_mode": "published_observations",
            "comparison_type": "causal_contrast",
        }
        self.assertTrue(validate_topic_novelty(candidate, {"entries": [prior]}))

    def test_subject_shaped_generated_id_is_not_a_scientific_identity(self):
        prior = {
            "topic_id": "direction_qft_topology_scaling",
            "title": "Topological susceptibility scaling",
            "domain": "quantum field theory",
            "research_question": "Does susceptibility follow a universal temperature scaling form?",
            "research_form": "theory_simulation",
            "evidence_mode": "published_observations",
            "comparison_type": "replication",
        }
        candidate = {
            "id": "direction_qft_topology_scaling",
            "title": "Finite-size estimator bias near deconfinement",
            "domain": "quantum field theory",
            "research_question": "Does an instanton-size cutoff alter estimator bias across lattice extent?",
            "research_form": "methodological_benchmark",
            "evidence_mode": "synthetic_simulation",
            "comparison_type": "mechanism_ablation",
        }
        self.assertTrue(validate_topic_novelty(candidate, {"entries": [prior]}))

    def test_topic_portfolio_profile_tracks_research_shape(self):
        value = package("Choose a feasible research direction")
        profile = topic_portfolio_profile(value["candidates"])
        self.assertEqual(profile["candidate_count"], 3)
        self.assertEqual(profile["distinct"], {
            "research_form": 3, "evidence_mode": 3, "comparison_type": 3,
        })
        signature = topic_signature(value["candidates"][0])
        self.assertEqual(signature["structure"]["research_form"], "theory_simulation")
        self.assertTrue(signature["structure_fingerprint"])

    def test_topic_portfolio_gate_rejects_structural_collapse(self):
        value = package("Choose a feasible research direction")
        for candidate in value["candidates"]:
            candidate.update({
                "research_form": "theory_simulation",
                "evidence_mode": "synthetic_simulation",
                "comparison_type": "mechanism_ablation",
            })
        with self.assertRaisesRegex(ValidationError, "distinct research_form"):
            validate_topic_package(
                value, objective=value["objective"], candidate_count=3,
                enforce_portfolio_diversity=True)

    def test_topic_portfolio_gate_rejects_repeated_archetype(self):
        value = package("Choose a feasible research direction")
        value["candidates"][1].update({
            "research_form": value["candidates"][0]["research_form"],
            "evidence_mode": value["candidates"][0]["evidence_mode"],
            "comparison_type": value["candidates"][0]["comparison_type"],
        })
        with self.assertRaisesRegex(ValidationError, "same research archetype"):
            validate_topic_portfolio(
                value["candidates"], minimum_research_forms=1,
                minimum_evidence_modes=1, minimum_comparison_types=1)

    def test_topic_history_rejects_same_research_archetype_with_new_prose(self):
        value = package("Choose a feasible research direction")
        prior = value["candidates"][0]
        candidate = {
            **value["candidates"][1],
            "id": "new_direction",
            "title": "A different title",
            "domain": "a different domain",
            "research_question": "Which unrelated wording tests a new phenomenon?",
            "research_form": prior["research_form"],
            "evidence_mode": prior["evidence_mode"],
            "comparison_type": prior["comparison_type"],
        }
        with self.assertRaisesRegex(ValidationError, "too similar"):
            validate_topic_novelty(candidate, {"entries": [{
                "topic_id": "old_direction", "signature": topic_signature(prior),
            }]})

    def test_maturity_refinement_requires_structural_pivot_when_enabled(self):
        review = {
            "decision": "refine", "selected_id": "direction_1",
            "scores": {dimension: 1 for dimension in (
                "question_specificity", "mechanism_depth", "comparison_design",
                "contribution_potential", "falsifiability")},
            "rationale": "The direction remains too close to the prior form.",
            "required_changes": ["Change the study shape and mechanism."],
            "changed_dimensions": ["mechanism"],
        }
        with self.assertRaisesRegex(ValidationError, "at least two"):
            validate_topic_maturity_review(
                review, candidate_ids={"direction_1"}, require_structural_pivot=True)
        review["changed_dimensions"] = ["mechanism", "research_form"]
        self.assertEqual(
            validate_topic_maturity_review(
                review, candidate_ids={"direction_1"}, require_structural_pivot=True),
            review)

    def test_topic_refinement_gate_compares_actual_parent_and_child(self):
        value = package("Choose a feasible research direction")
        parent = value["candidates"][0]
        child = {**parent, "research_question": "A genuinely changed question."}
        self.assertEqual(topic_refinement_dimensions(parent, child), ["research_question"])
        with self.assertRaisesRegex(ValidationError, "at least two"):
            validate_topic_refinement(parent, child, require_structural_pivot=True)
        child.update({
            "research_form": "scaling_boundary",
            "evidence_mode": "cross_source_synthesis",
        })
        changed = validate_topic_refinement(parent, child, require_structural_pivot=True)
        self.assertEqual(changed, ["research_question", "research_form", "evidence_mode"])

    def test_refinement_shape_plan_moves_the_parent_slot(self):
        initial = _portfolio_shape_plan(4, seed=123456)
        parent_index = 1
        parent_shape = initial[parent_index]
        payload = json.loads(topic_prompt(
            "Choose a feasible research direction", 4,
            refinement_context={
                "parent_topic": {"id": "direction_1", **parent_shape},
                "parent_candidate_index": parent_index,
                "refinement_feedback": {"required_changes": ["Change the comparison boundary."]},
            },
            portfolio_seed=123456,
        ))
        repaired = payload["portfolio_shape_plan"]
        self.assertNotEqual(
            tuple(repaired[parent_index][field] for field in PORTFOLIO_DIMENSIONS),
            tuple(parent_shape[field] for field in PORTFOLIO_DIMENSIONS),
        )
        self.assertEqual(
            len({item["research_form"] for item in repaired}), 4)
        self.assertEqual(
            len({item["evidence_mode"] for item in repaired}), 3)
        self.assertEqual(
            len({item["comparison_type"] for item in repaired}), 3)

    def test_foundry_shape_plan_and_selection_repair_keep_one_executable_member(self):
        context = {
            "capability_foundry": {
                "enabled": True,
                "allowed_evidence_modes": ["analytical_derivation", "synthetic_simulation"],
            },
            "executables": {"python3": True},
            "python_packages": {},
            "configured_stage_kinds": ["experiment"],
        }
        payload = json.loads(topic_prompt(
            "Choose a feasible research direction", 4,
            runtime_context=context, portfolio_seed=9876,
        ))
        self.assertTrue(any(
            item["evidence_mode"] in context["capability_foundry"]["allowed_evidence_modes"]
            for item in payload["portfolio_shape_plan"]))
        value = package("Choose a feasible research direction")
        value["candidates"][0]["evidence_mode"] = "synthetic_simulation"
        value["candidates"][0].pop("capability_requirements")
        value["candidates"][1]["evidence_mode"] = "published_observations"
        value["selected_id"] = "direction_1"
        repair = _repair_foundry_selection(value, context)
        self.assertEqual(repair["from_selected_id"], "direction_1")
        self.assertEqual(repair["to_selected_id"], "direction_0")
        self.assertEqual(value["selected_id"], "direction_0")
        self.assertEqual(
            value["candidates"][0]["capability_requirements"],
            {"executables": ["python3"], "python_packages": [], "stage_kinds": ["experiment"]},
        )

    def test_refinement_selection_repair_uses_actual_candidate_shapes(self):
        value = package("Choose a feasible research direction")
        parent = value["candidates"][1]
        value["selected_id"] = parent["id"]
        repair = _repair_topic_refinement_selection(value, parent)
        self.assertIsNotNone(repair)
        self.assertNotEqual(value["selected_id"], parent["id"])
        replacement = next(
            candidate for candidate in value["candidates"]
            if candidate["id"] == value["selected_id"])
        self.assertNotEqual(
            tuple(replacement[field] for field in PORTFOLIO_DIMENSIONS),
            tuple(parent[field] for field in PORTFOLIO_DIMENSIONS),
        )
        self.assertGreaterEqual(len(repair["changed_dimensions"]), 2)

    def test_refinement_selection_repair_skips_rejected_seeds_when_pivot_is_required(self):
        value = package("Choose a feasible research direction")
        for index, candidate in enumerate(value["candidates"]):
            candidate["frontier_seed_id"] = f"frontier_{index}"
        parent = value["candidates"][1]
        value["selected_id"] = parent["id"]
        repair = _repair_topic_refinement_selection(
            value, parent, require_frontier_seed_pivot=True,
            rejected_frontier_seed_ids=["frontier_0", "frontier_1"])
        self.assertIsNotNone(repair)
        replacement = next(
            candidate for candidate in value["candidates"]
            if candidate["id"] == value["selected_id"])
        self.assertEqual(replacement["frontier_seed_id"], "frontier_2")
        self.assertNotIn(replacement["frontier_seed_id"], {"frontier_0", "frontier_1"})

    def test_weak_high_risk_source_refinement_requires_a_new_frontier_seed(self):
        value = package("Choose a feasible research direction")
        parent = value["candidates"][1]
        parent["frontier_seed_id"] = "frontier_1"
        child = {
            **parent,
            "research_question": "A genuinely changed question.",
            "research_form": "scaling_boundary",
            "evidence_mode": "cross_source_synthesis",
            "frontier_seed_id": "frontier_1",
        }
        with self.assertRaisesRegex(ValidationError, "different frontier seed"):
            validate_topic_refinement(
                parent, child, require_structural_pivot=True,
                require_frontier_seed_pivot=True)
        child["frontier_seed_id"] = "frontier_2"
        changed = validate_topic_refinement(
            parent, child, require_structural_pivot=True,
            require_frontier_seed_pivot=True)
        self.assertIn("frontier_seed_id", changed)

    def test_only_weak_or_template_near_high_risk_source_forces_seed_pivot(self):
        self.assertFalse(_source_challenge_requires_frontier_seed_pivot({
            "prior_work_risk": "high", "source_relevance": 4,
            "template_independence": 2,
        }))
        self.assertTrue(_source_challenge_requires_frontier_seed_pivot({
            "prior_work_risk": "high", "source_relevance": 4,
            "template_independence": 1,
        }))
        self.assertTrue(_source_challenge_requires_frontier_seed_pivot({
            "prior_work_risk": "high", "source_relevance": 2,
            "template_independence": 4,
        }))

    def test_topic_prompt_exposes_portfolio_contract_and_attempt_history(self):
        from scisaurus.runtime.topic_discovery import topic_prompt
        payload = json.loads(topic_prompt(
            "Choose a feasible research direction", 6,
            candidate_history=[{"status": "rejected", "candidate_signatures": []}]))
        self.assertEqual(payload["portfolio_requirements"]["minimum_distinct_research_forms"], 4)
        self.assertIn("research_form", payload["output_contract"]["candidate"])
        self.assertEqual(payload["previous_candidate_directions"][0]["status"], "rejected")

    def test_topic_prompt_applies_computational_native_preferences(self):
        payload = json.loads(topic_prompt(
            "Choose a feasible research direction", 4,
            runtime_context={
                "topic_preferences": {
                    "mode": "computational_native",
                    "must_have": ["a quantitative estimand", "a known-limit check"],
                    "avoid": ["a generic benchmark"],
                },
            }))
        self.assertEqual(
            payload["runtime_context"]["topic_preferences"]["mode"],
            "computational_native")
        self.assertTrue(any("computational-native" in item for item in payload["constraints"]))
        self.assertTrue(any("known-limit check" in item for item in payload["constraints"]))
        self.assertTrue(any("generic benchmark" in item for item in payload["constraints"]))

    def test_candidate_prompt_excludes_frontier_seeds_without_source_records(self):
        seeds = frontier_plan(4)["seeds"]
        papers = [{"work_id": "W1", "frontier_seed_id": "frontier_0"},
                  {"work_id": "W2", "frontier_seed_id": "frontier_1"},
                  {"work_id": "W3", "frontier_seed_id": "frontier_2"}]
        eligible = _grounding_eligible_frontier_seeds(seeds, papers, 4)
        self.assertEqual([item["id"] for item in eligible],
                         ["frontier_0", "frontier_1", "frontier_2"])

    def test_topic_prompt_exposes_source_seed_pivot_requirement(self):
        from scisaurus.runtime.topic_discovery import topic_prompt
        payload = json.loads(topic_prompt(
            "Choose a feasible research direction", 3,
            frontier_seeds=frontier_plan(3)["seeds"],
            recent_papers=[{
                "work_id": "W1", "frontier_seed_id": "frontier_0",
                "title": "Marine ecology transition", "abstract": "A bounded study.",
            }],
            refinement_context={
                "parent_topic": {"frontier_seed_id": "frontier_0"},
                "require_frontier_seed_pivot": True,
            }))
        self.assertTrue(payload["refinement_shape"]["frontier_seed_pivot_required"])
        self.assertTrue(any("different frontier_seed_id" in item for item in payload["constraints"]))

    def test_topic_objective_is_restored_from_the_declared_mission(self):
        value = {"objective": "model paraphrase"}
        self.assertIs(_materialize_topic_objective(value, "declared mission"), value)
        self.assertEqual(value["objective"], "declared mission")

    def test_missing_domain_is_derived_only_from_the_declared_frontier_seed(self):
        value = {"candidates": [
            {"id": "direction_1", "frontier_seed_id": "frontier_1"},
            {"id": "direction_2", "frontier_seed_id": "missing", "domain": "explicit"},
        ]}
        repairs = _materialize_seed_domains(value, [{
            "id": "frontier_1", "domain": "marine ecology",
        }])
        self.assertEqual(value["candidates"][0]["domain"], "marine ecology")
        self.assertEqual(value["candidates"][1]["domain"], "explicit")
        self.assertEqual(repairs[0]["source"], "frontier_seed_id")

    def test_missing_grounding_is_repaired_only_from_an_exact_seed_and_matching_record(self):
        value = {"candidates": [{
            "id": "direction_1",
            "domain": "marine ecology",
            "title": "Diel oxygen transition",
            "research_question": "Does oxygen control a plankton transition?",
            "scope": "Estuarine plankton under diel oxygen forcing.",
        }]}
        repairs = _materialize_seed_bindings(
            value,
            [{
                "id": "frontier_1", "domain": "marine ecology",
                "phenomenon": "oxygen transition", "mechanism": "diel forcing",
                "unit_of_analysis": "plankton community",
            }],
            [{
                "work_id": "W1", "frontier_seed_id": "frontier_1",
                "title": "Oxygen transitions in estuarine plankton",
                "abstract": "Diel oxygen forcing changes plankton community turnover.",
            }],
        )
        self.assertEqual(value["candidates"][0]["frontier_seed_id"], "frontier_1")
        self.assertEqual(value["candidates"][0]["prior_work_ids"], ["W1"])
        self.assertEqual(
            {(item["field"], item["source"]) for item in repairs},
            {
                ("frontier_seed_id", "exact_frontier_domain"),
                ("prior_work_ids", "seed_record_token_overlap"),
            },
        )

    def test_runner_persists_candidate_attempt_trace_and_admission(self):
        with patch("scisaurus.runtime.topic_discovery.ModelClient", FakeModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run("Choose a feasible research direction", candidate_count=3,
                   bibliography=False, max_attempts=1)
        self.assertEqual(result["candidate_attempt_trace"][0]["status"], "admitted")
        self.assertEqual(len(result["candidate_attempt_trace"][0]["candidate_signatures"]), 3)
        self.assertEqual(result["candidate_attempt_trace"][0]["portfolio_profile"]["distinct"]["research_form"], 3)

    def test_schema_failure_is_retryable_but_does_not_poison_rejection_history(self):
        with patch("scisaurus.runtime.topic_discovery.ModelClient", InvalidCandidateShapeModel):
            with self.assertRaises(ValidationError) as raised:
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                    "timeout_seconds": 1, "max_output_tokens": 4096,
                }).run("Choose a feasible research direction", candidate_count=3,
                       bibliography=False, max_attempts=1)
        error = raised.exception
        self.assertTrue(error.topic_intake_recoverable)
        self.assertEqual(error.topic_retry_reason, "intake_contract_failure")
        self.assertEqual(error.rejected_topic_history, [])
        self.assertIn("mechanism_boundary", error.candidate_attempt_trace[0]["error"])
        self.assertEqual(
            _topic_retry_reason(
                ValidationError("topic candidate has an invalid shape"),
                [{"status": "rejected"}],
                [{"topic_id": "earlier-scientific-rejection"}],
            ),
            "intake_contract_failure",
        )

    def test_runner_passes_rejected_candidate_history_to_repair(self):
        PortfolioRepairModel.topic_calls = 0
        PortfolioRepairModel.prompts = []
        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex), \
                patch("scisaurus.runtime.topic_discovery.ModelClient", PortfolioRepairModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run("Choose a feasible research direction", candidate_count=3,
                   max_attempts=2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(PortfolioRepairModel.topic_calls, 2)
        self.assertEqual([item["status"] for item in result["candidate_attempt_trace"]],
                         ["rejected", "admitted"])
        repair_prompts = [item for item in PortfolioRepairModel.prompts
                          if item.get("assignment") == "repair_invalid_topic_discovery"]
        self.assertEqual(len(repair_prompts), 1)
        self.assertIn("portfolio_repair", repair_prompts[0])
        self.assertEqual(repair_prompts[0]["previous_candidate_directions"][0]["status"], "rejected")

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

    def test_runner_preserves_survey_eligible_candidate_as_provisional(self):
        SurveyEligibleMaturityModel.calls = []
        SurveyEligibleMaturityModel.review_count = 0
        with patch("scisaurus.runtime.topic_discovery.ModelClient",
                   SurveyEligibleMaturityModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run("Choose a feasible research direction", candidate_count=3,
                   bibliography=False, maturity_review_rounds=1, max_attempts=2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["admission_state"], "provisional_for_survey")
        self.assertEqual(result["next_evidence_action"], "literature_survey")
        self.assertEqual(len(result["maturity_open_requirements"]), 2)
        self.assertEqual(
            result["candidate_attempt_trace"][-1]["status"],
            "provisional_for_survey")

    def test_runner_repairs_incomplete_maturity_review_without_discarding_package(self):
        MaturityMalformedRepairModel.review_calls = 0
        MaturityMalformedRepairModel.assignments = []
        with patch("scisaurus.runtime.topic_discovery.ModelClient", MaturityMalformedRepairModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run("Choose a feasible research direction", candidate_count=3,
                   bibliography=False, maturity_review_rounds=1, max_attempts=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(MaturityMalformedRepairModel.review_calls, 2)
        self.assertEqual(MaturityMalformedRepairModel.assignments[-2:], [
            "topic_maturity_review", "repair_topic_maturity_review",
        ])

    def test_runner_carries_source_challenge_feedback_into_repair(self):
        SourceChallengeRefinementModel.calls = []
        SourceChallengeRefinementModel.payloads = []
        SourceChallengeRefinementModel.challenge_count = 0
        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex), \
                patch("scisaurus.runtime.topic_discovery.ModelClient", SourceChallengeRefinementModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run("Choose a feasible research direction", candidate_count=3,
                   max_attempts=2, maturity_review_rounds=0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(SourceChallengeRefinementModel.challenge_count, 2)
        self.assertEqual(
            SourceChallengeRefinementModel.calls.count("repair_selected_topic_candidate"), 1)
        refinement = next(item for item in SourceChallengeRefinementModel.payloads
                          if item.get("assignment") == "repair_selected_topic_candidate")
        self.assertEqual(
            refinement["targeted_feedback"]["required_changes"],
            ["Change the mechanism and comparison boundary."],
        )
        self.assertNotEqual(
            refinement["target_frontier_seed_id"],
            refinement["parent_candidate"]["frontier_seed_id"],
        )
        challenge_payload = next(item for item in SourceChallengeRefinementModel.payloads
                                if item.get("assignment") == "topic_source_and_template_challenge")
        self.assertEqual(
            challenge_payload["allowed_work_ids"],
            [record["work_id"] for record in challenge_payload["targeted_scholarly_records"]],
        )
        self.assertTrue(set(refinement["output_contract"]["candidate"]).issuperset({
            "research_question", "mechanism", "measurement", "frontier_seed_id",
        }))
        self.assertEqual(result["source_challenge"]["decision"], "admit_to_survey")

    def test_runner_repairs_refinement_against_the_rejected_package(self):
        RefinementValidationRepairModel.challenge_calls = 0
        RefinementValidationRepairModel.refinement_calls = 0
        RefinementValidationRepairModel.payloads = []
        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex), \
                patch("scisaurus.runtime.topic_discovery.ModelClient", RefinementValidationRepairModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
        }).run("Choose a feasible research direction", candidate_count=3,
                   max_attempts=3, maturity_review_rounds=0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected_id"], result["topic"]["id"])
        self.assertEqual(result["selected_id"], "direction_0")
        self.assertEqual(RefinementValidationRepairModel.refinement_calls, 1)
        repair = [
            item for item in RefinementValidationRepairModel.payloads
            if item.get("assignment") == "refine_topic_discovery"
            and "previous_response" in item
        ]
        self.assertEqual(repair, [])

    def test_source_challenge_repairs_out_of_packet_work_id(self):
        SourceChallengeInvalidIdRepairModel.assignments = []
        SourceChallengeInvalidIdRepairModel.challenge_count = 0
        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex), \
                patch("scisaurus.runtime.topic_discovery.ModelClient", SourceChallengeInvalidIdRepairModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run("Choose a feasible research direction", candidate_count=3,
                   max_attempts=1, maturity_review_rounds=0)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(SourceChallengeInvalidIdRepairModel.challenge_count, 2)
        self.assertEqual(SourceChallengeInvalidIdRepairModel.assignments[-2:], [
            "topic_source_and_template_challenge", "repair_topic_source_challenge",
        ])

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
        self.assertEqual(
            [item["status"] for item in caught.exception.candidate_attempt_trace],
            ["result_unknown", "result_unknown", "result_unknown"],
        )
        self.assertTrue(all(
            item["outcome_known"] is False
            for item in caught.exception.candidate_attempt_trace
        ))

    def test_validation_after_external_response_is_kept_in_diagnostics(self):
        budget = TopicBudget({"max_model_calls": 2}, {})
        budget.before_model_call("topic_discovery", "fake")
        budget.record_model_result(ModelResult(
            text="{}", model="fake",
            usage={"model_calls": 1, "input_tokens": 1, "output_tokens": 1},
            elapsed_seconds=0.01, finish_reason="stop"))
        budget.record_validation_error(ValidationError("source challenge requires refinement"))
        events = budget.snapshot()["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["kind"], "validation")
        self.assertIn("source challenge", events[-1]["error"])

    def test_topic_budget_preflights_input_before_provider_dispatch(self):
        budget = TopicBudget({"max_model_calls": 2, "max_input_tokens": 1}, {})
        with self.assertRaisesRegex(QuotaExceededError, "input_tokens"):
            budget.before_model_call(
                "topic_discovery", "fake", system="Return JSON.", prompt="A bounded request.")
        snapshot = budget.snapshot()
        self.assertEqual(snapshot["usage"]["model_calls"], 0)
        self.assertEqual(snapshot["usage"]["input_tokens"], 0)
        self.assertEqual(len(snapshot["events"]), 1)
        self.assertEqual(snapshot["events"][0]["kind"], "quota")
        self.assertEqual(snapshot["events"][0]["status"], "blocked")
        self.assertEqual(snapshot["events"][0]["dimension"], "input_tokens")

    def test_topic_budget_records_conservative_input_estimate_on_dispatch(self):
        budget = TopicBudget({"max_model_calls": 1, "max_input_tokens": 1000}, {})
        budget.before_model_call(
            "topic_discovery", "fake", system="Return JSON.", prompt="A bounded request.")
        event = budget.snapshot()["events"][0]
        self.assertEqual(event["kind"], "model")
        self.assertGreater(event["estimated_input_tokens"], 0)

    def test_topic_refinement_prompt_stays_inside_qwen_context_after_compaction(self):
        large = "evidence span " * 5000
        papers = [{
            "work_id": f"W{index}", "title": f"Paper {index}",
            "abstract": large, "authors": large, "doi": f"10.1000/{index}",
            "frontier_seed_id": "seed-1", "frontier_domain": "soft matter",
            "matched_query": large, "source_url": "https://example.invalid/work",
            "year": 2025,
        } for index in range(24)]
        candidate = {
            "id": "direction_1", "title": large, "domain": "soft matter",
            "research_question": large, "research_form": "theory_simulation",
            "evidence_mode": "synthetic_simulation", "comparison_type": "cross_method",
        }
        history = [{
            "attempt": index, "status": "rejected", "selected_id": "direction_1",
            "selected_topic": candidate, "error": large,
            "candidate_signatures": [{"question_tokens": list(range(2000)), "title_tokens": list(range(2000))}],
            "source_challenge": {"rationale": large, "required_changes": [large] * 12},
        } for index in range(24)]
        runtime = {
            "topic_history": {"schema_version": "topic-history-1", "entries": history},
            "project_files": [large] * 80,
            "independent_specialist_reports": [{
                "assigned_role": "research.adversary", "role_id": "adversary",
                "decision": "hold", "summary": large,
                "findings": [large] * 10, "evidence_gaps": [large] * 10,
                "requested_actions": [large] * 10,
            } for _ in range(12)],
            "experiment_catalog": [{"id": "cap-1", "design_template": {"detail": large}}],
        }
        refinement = {
            "mode": "refinement", "cycle": 3, "parent_topic_id": "direction_1",
            "parent_candidate_index": 1, "changed_dimensions": ["mechanism"],
            "require_frontier_seed_pivot": True, "rejected_frontier_seed_ids": ["seed-1"],
            "parent_topic": {**candidate, "mechanism": large, "resource_plan": large},
            "refinement_feedback": {
                "review_type": "specialist_verifier", "decision": "hold",
                "rationale": large, "required_changes": [large] * 12,
                "critical_findings": [large] * 12,
            },
            "survey_feedback": {"gap_state": "insufficient_evidence", "evidence": {"gap": large}},
            "specialist_feedback": runtime["independent_specialist_reports"],
        }
        prompt = topic_prompt(
            "Find a computationally executable scientific question", 4,
            recent_papers=papers, frontier_seeds=frontier_plan(),
            runtime_context=runtime, refinement_context=refinement,
            candidate_history=history, rejected_candidate_directions=history,
            portfolio_seed=7,
        )
        self.assertLess(estimate_input_tokens(SYSTEM, prompt), 56000)

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

    def test_runner_repairs_only_a_missing_research_question(self):
        objective = "Choose a feasible research direction"
        MissingResearchQuestionModel.assignments = []
        with patch("scisaurus.runtime.topic_discovery.ModelClient", MissingResearchQuestionModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run(objective, candidate_count=3, bibliography=False, max_attempts=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            MissingResearchQuestionModel.assignments,
            ["free_topic_discovery", "repair_missing_topic_fields"],
        )
        self.assertTrue(all(candidate["research_question"] for candidate in result["candidates"]))
        repairs = result["candidate_attempt_trace"][0]["derived_field_repairs"]
        self.assertEqual(repairs[0]["field"], "research_question")
        self.assertEqual(repairs[0]["source"], "targeted_model_field_repair")

    def test_runner_repairs_only_a_missing_title(self):
        objective = "Choose a feasible research direction"
        MissingTitleModel.assignments = []
        with patch("scisaurus.runtime.topic_discovery.ModelClient", MissingTitleModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run(objective, candidate_count=3, bibliography=False, max_attempts=1)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            MissingTitleModel.assignments,
            ["free_topic_discovery", "repair_missing_topic_fields"],
        )
        self.assertTrue(all(candidate["title"] for candidate in result["candidates"]))
        repairs = result["candidate_attempt_trace"][0]["derived_field_repairs"]
        self.assertEqual(repairs[0]["field"], "title")
        self.assertEqual(repairs[0]["source"], "targeted_model_field_repair")

    def test_topic_client_clamps_provider_timeout_to_stage_deadline(self):
        runner = TopicDiscoveryRunner({
            "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
            "timeout_seconds": 120, "max_output_tokens": 4096,
        })
        client = runner._client("topic_discovery", deadline=time.monotonic() + 5)
        self.assertGreater(client.timeout_seconds, 0)
        self.assertLessEqual(client.timeout_seconds, 5)

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

    def test_foundry_selected_baseline_is_materialized_when_model_omits_it(self):
        value = package("Choose a feasible research direction")
        value["candidates"][1].pop("capability_requirements")
        context = {
            "capability_foundry": {"enabled": True},
            "executables": {"python3": True},
            "python_packages": {"numpy": True},
            "configured_stage_kinds": ["topic_discovery", "experiment", "paper"],
        }
        _materialize_foundry_capability_requirements(value, context)
        self.assertEqual(
            value["candidates"][1]["capability_requirements"],
            {"executables": ["python3"], "python_packages": [], "stage_kinds": ["experiment"]},
        )
        from scisaurus.runtime.topic_discovery import validate_topic_feasibility
        self.assertEqual(validate_topic_feasibility(value, context)["status"], "feasible")

    def test_foundry_feasibility_note_is_materialized_without_inventing_evidence(self):
        value = package("Choose a feasible research direction")
        value["candidates"][0].pop("feasibility")
        context = {
            "capability_foundry": {"enabled": True},
            "executables": {}, "python_packages": {}, "configured_stage_kinds": [],
        }
        repairs = _materialize_foundry_feasibility(value, context)
        self.assertEqual(repairs[0]["field"], "feasibility")
        self.assertIn("bounded experiment baseline", value["candidates"][0]["feasibility"])

    def test_foundry_topic_rejects_external_evidence_mode(self):
        value = package("Choose a feasible research direction")
        context = {
            "capability_foundry": {
                "enabled": True,
                "allowed_evidence_modes": ["analytical_derivation", "synthetic_simulation"],
            },
            "executables": {}, "python_packages": {},
            "configured_stage_kinds": [],
        }
        with self.assertRaisesRegex(ValidationError, "analytical or synthetic"):
            from scisaurus.runtime.topic_discovery import validate_topic_feasibility
            validate_topic_feasibility(value, context)
        value["candidates"][1]["evidence_mode"] = "synthetic_simulation"
        self.assertEqual(validate_topic_feasibility(value, context)["status"], "feasible")

    def test_frontier_query_anchor_repair_preserves_model_terms(self):
        value = frontier_plan(4)
        original = "unrelated spectroscopy transition"
        value["seeds"][0]["search_queries"][0] = original
        repaired = _anchor_frontier_seed_queries(value)
        self.assertTrue(repaired["seeds"][0]["search_queries"][0].startswith(original))
        validate_frontier_seed_plan(repaired, seed_count=4)

    def test_topic_query_anchor_repair_preserves_model_terms(self):
        value = package("Choose a feasible research direction")
        original = "unrelated spectroscopy transition"
        value["candidates"][0]["domain"] = "marine ecology"
        value["candidates"][0]["title"] = "Diel oxygen boundary in estuarine plankton"
        value["candidates"][0]["research_question"] = (
            "Does oxygen depletion change plankton turnover across salinity gradients?")
        value["candidates"][0]["search_queries"] = [
            original, "oxygen plankton salinity", "estuarine oxygen turnover",
        ]
        self.assertEqual(_anchor_topic_candidate_queries(value), 1)
        self.assertTrue(value["candidates"][0]["search_queries"][0].startswith(original))
        validate_topic_package(value, objective=value["objective"], candidate_count=3)

    def test_topic_query_anchor_repair_uses_the_matching_seed_for_sparse_queries(self):
        value = {"candidates": [{
            "id": "direction_1", "frontier_seed_id": "frontier_1",
            "domain": "x", "title": "x", "research_question": "x",
            "scope": "x", "mechanism": "x", "measurement": "x",
            "search_queries": ["x"],
        }]}
        repairs = _anchor_topic_candidate_queries(
            value, frontier_seeds=[{
                "id": "frontier_1", "domain": "remote physics",
                "phenomenon": "phase transition", "mechanism": "coupled transport",
                "unit_of_analysis": "mode amplitude",
            }])
        self.assertEqual(repairs, 1)
        query = value["candidates"][0]["search_queries"][0]
        self.assertGreaterEqual(len(set(query.split())), 2)
        self.assertTrue({"remote", "physics", "phase", "transition"}.intersection(query.split()))

    def test_source_challenge_receives_cited_records_before_fresh_hits(self):
        selected = {"prior_work_ids": ["W2", "W-missing"]}
        cited = [{"work_id": "W1", "title": "Other"},
                 {"work_id": "W2", "title": "Cited record"}]
        targeted = [{"work_id": "W3", "title": "Fresh hit"},
                    {"work_id": "W2", "title": "Duplicate fresh hit"}]
        records = _merge_candidate_source_records(selected, cited, targeted, maximum=3)
        self.assertEqual([item["work_id"] for item in records], ["W2", "W3"])

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

    def test_repeated_selection_switches_to_an_unseen_portfolio_member(self):
        value = package("Choose a feasible research direction")
        prior = {
            "topic_id": "old_direction",
            "title": value["candidates"][0]["title"],
            "domain": value["candidates"][0]["domain"],
            "research_question": value["candidates"][0]["research_question"],
            "signature": topic_signature(value["candidates"][0]),
        }
        value["candidates"][1].update({
            "title": "Dispersal-driven recovery after disturbance",
            "domain": "landscape ecology",
            "research_question": "How does habitat dispersal alter recovery time after a disturbance pulse?",
        })
        value["selected_id"] = value["candidates"][0]["id"]
        repair = _repair_topic_novelty_selection(value, {"entries": [prior]})
        self.assertEqual(repair["from_selected_id"], "direction_0")
        self.assertEqual(value["selected_id"], "direction_1")

    def test_excluded_selection_switches_to_an_unseen_portfolio_member(self):
        value = package("Choose a feasible research direction")
        value["selected_id"] = value["candidates"][0]["id"]
        repair = _repair_topic_novelty_selection(
            value, {"entries": []}, excluded_topic_ids={"direction_0"})
        self.assertEqual(repair["from_selected_id"], "direction_0")
        self.assertEqual(value["selected_id"], "direction_1")

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
