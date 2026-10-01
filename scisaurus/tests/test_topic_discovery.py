import json
import hashlib
import os
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scisaurus.core.errors import QuotaExceededError, ValidationError
from scisaurus.runtime.literature import ProviderCooldownError
from scisaurus.runtime.models import ModelCallError, ModelResult, estimate_input_tokens
from scisaurus.runtime.topic_discovery import (
    MAX_BOUNDED_TOPIC_ATTEMPTS,
    MAX_CONSECUTIVE_REFINEMENT_CONTRACT_REJECTIONS,
    MAX_CONSECUTIVE_TOPIC_NOVELTY_REJECTIONS,
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
    topic_salvage_plan,
    topic_signature,
    validate_topic_package,
    validate_feasibility_plan,
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
    _repair_feasibility_input_contract,
    _repair_feasibility_input_duplicates,
    _repair_feasibility_input_kinds,
    _repair_feasibility_input_statuses,
    _strip_noncontract_topic_fields,
    _repair_case_only_topic_candidate_ids,
    _resource_plan_from_feasibility,
    _materialize_seed_bindings,
    _materialize_seed_domains,
    _materialize_topic_objective,
    _normalise_topic_model_response,
    _single_topic_candidate_response,
    _portfolio_shape_plan,
    _record_attempt_selection,
    _repair_executable_selection,
    _repair_foundry_selection,
    _repair_topic_novelty_selection,
    _repair_topic_refinement_selection,
    _source_challenge_requires_frontier_seed_pivot,
    _topic_retry_reason,
    _topic_validation_rejection_type,
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


def foundry_feasibility_plan(**overrides):
    value = {
        "execution_mode": "foundry",
        "experiment_input": "self_contained",
        "evidence_inputs": [{
            "kind": "synthetic", "status": "available",
            "source": "seeded synthetic input generated by the pinned experiment",
        }],
        "data_access": "closed_world",
        "required_packages": ["numpy"],
        "required_executables": ["python3"],
        "estimated_compute_seconds": 120,
        "estimated_api_requests": 0,
        "estimated_model_calls": 0,
        "network_access": False,
    }
    value.update(overrides)
    return value


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


class SingleCandidateThenPortfolioModel(FakeModel):
    calls = []

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        type(self).calls.append(payload.get("assignment"))
        result = super().complete(system=system, prompt=prompt, images=images)
        package_value = json.loads(result.text)
        if payload.get("assignment") == "free_topic_discovery":
            return ModelResult(
                text=json.dumps(package_value["candidates"][0]), model="fake",
                usage=result.usage, elapsed_seconds=result.elapsed_seconds,
                finish_reason="stop")
        if payload.get("assignment") == "complete_topic_portfolio":
            package_value["candidates"][0] = deepcopy(
                payload["portfolio_completion"]["candidate_to_preserve"])
            return ModelResult(
                text=json.dumps(package_value), model="fake",
                usage=result.usage, elapsed_seconds=result.elapsed_seconds,
                finish_reason="stop")
        return result


class RepeatedInvalidTopicResponseModel(FakeModel):
    calls = 0

    def complete(self, *, system, prompt, images=None):
        type(self).calls += 1
        return ModelResult(
            text='{"not": "a topic package"}', model="fake",
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
        refinement = payload.get("refinement_context") or {}
        salvage_plan = (payload.get("salvage_plan")
                        or refinement.get("salvage_plan"))
        if isinstance(salvage_plan, dict):
            active_branch = salvage_plan.get("active_branch")
            if (salvage_plan.get("mode") == "salvage"
                    and isinstance(active_branch, dict)
                    and active_branch.get("id") == "mechanism-observable"):
                value["candidates"][1].update({
                    "mechanism": "mechanism-specific response under controlled regimes",
                    "measurement": "replicate-level response contrast across regimes",
                })
        if payload.get("assignment") == "refine_topic_discovery":
            candidate = value["candidates"][1]
            candidate["title"] = "Mechanism-sensitive outcome stability"
            candidate["research_question"] = (
                "Does mechanism 1 change the measured outcome across clean and contaminated regimes, "
                "and which regime separates the competing explanations?")
            refinement = payload.get("refinement_context") or {}
            salvage_plan = refinement.get("salvage_plan")
            candidate.update({
                "mechanism": "mechanism-specific response under controlled regimes",
                "measurement": "replicate-level response contrast across regimes",
            })
            if not isinstance(salvage_plan, dict):
                candidate["scope"] = (
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


class MaturityLengthModel(MaturityModel):
    """Return a bounded but incomplete maturity response on both review lanes."""

    review_calls = 0

    def complete(self, *, system, prompt, images=None):
        payload = json.loads(prompt)
        if payload.get("assignment") in {
                "topic_maturity_review", "repair_topic_maturity_review"}:
            type(self).review_calls += 1
            return ModelResult(
                text='{"decision": "admit"}', model="fake",
                usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 900},
                elapsed_seconds=0.01, finish_reason="length")
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
                    f"for {target_seed['unit_of_analysis']}?"
                ),
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
    def test_salvage_scope_validation_errors_are_contract_not_scientific_rejections(self):
        self.assertEqual(
            _topic_validation_rejection_type(
                "topic salvage branch evidence-boundary must materially change at least one "
                "of its assigned dimensions: evidence_mode, research_form, scope, comparison_type"),
            "refinement_contract",
        )
        self.assertEqual(
            _topic_validation_rejection_type(
                "topic salvage branch evidence-boundary changed dimensions outside its assigned "
                "repair scope: mechanism"),
            "refinement_contract",
        )
        self.assertEqual(
            _topic_validation_rejection_type(
                "topic salvage branch mechanism-observable must preserve the parent's phenomenon"),
            "refinement",
        )

    def test_topic_response_adapter_recovers_wrappers_and_non_stop_content(self):
        value = package("objective")
        raw = "preface\n```json\n" + json.dumps(
            {"result": value}, ensure_ascii=False) + "\n```\ntrailing note"
        result = ModelResult(
            text=raw, model="fake", usage={}, elapsed_seconds=0.01,
            finish_reason="length")
        parsed, repairs = _normalise_topic_model_response(result)
        self.assertEqual(parsed, value)
        self.assertIn({"kind": "wrapper_unwrap", "wrapper": "result"}, repairs)
        self.assertIn({
            "kind": "non_stop_finish_with_parseable_content",
            "finish_reason": "length",
        }, repairs)

    def test_topic_response_adapter_recovers_unclosed_outer_object(self):
        value = package("objective")
        result = ModelResult(
            text=json.dumps(value, ensure_ascii=False)[:-1], model="fake",
            usage={}, elapsed_seconds=0.01, finish_reason="stop")
        parsed, repairs = _normalise_topic_model_response(result)
        self.assertEqual(parsed, value)
        self.assertEqual(repairs, [])

    def test_topic_response_adapter_does_not_invent_package_from_prose(self):
        result = ModelResult(
            text="The candidate should study a measurable transition.", model="fake",
            usage={}, elapsed_seconds=0.01, finish_reason="stop")
        with self.assertRaisesRegex(ValidationError, "valid JSON"):
            _normalise_topic_model_response(result)

    def test_single_candidate_response_is_only_a_completion_seed(self):
        candidate = package("objective")["candidates"][0]
        self.assertEqual(_single_topic_candidate_response(candidate), candidate)
        self.assertEqual(
            _single_topic_candidate_response({"candidate": candidate}), candidate)
        self.assertIsNone(_single_topic_candidate_response(package("objective")))
        self.assertIsNone(_single_topic_candidate_response({
            **candidate, "unrecognized_metadata": "not a candidate field",
        }))

    def test_runner_completes_candidate_only_response_without_inventing_portfolio_members(self):
        SingleCandidateThenPortfolioModel.calls = []
        with patch("scisaurus.runtime.topic_discovery.ModelClient",
                   SingleCandidateThenPortfolioModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run("Choose a feasible research direction", candidate_count=3,
                   bibliography=False, maturity_review_rounds=0, max_attempts=4)
        self.assertEqual(SingleCandidateThenPortfolioModel.calls, [
            "free_topic_discovery", "complete_topic_portfolio",
        ])
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["candidates"][0]["id"], "direction_0")
        self.assertEqual(result["candidate_attempt_trace"][0]["response_shape"],
                         "single_candidate")
        self.assertEqual(result["candidate_attempt_trace"][0]["status"], "rejected")

    def test_runner_stops_after_byte_identical_invalid_topic_response(self):
        RepeatedInvalidTopicResponseModel.calls = 0
        with patch("scisaurus.runtime.topic_discovery.ModelClient",
                   RepeatedInvalidTopicResponseModel):
            with self.assertRaisesRegex(
                    ValidationError, "byte-identical rejected response") as raised:
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                    "timeout_seconds": 1, "max_output_tokens": 4096,
                }).run("Choose a feasible research direction", candidate_count=3,
                       bibliography=False, maturity_review_rounds=0, max_attempts=8)
        self.assertEqual(RepeatedInvalidTopicResponseModel.calls, 2)
        self.assertEqual(raised.exception.candidate_attempt_trace[-1]["status"],
                         "repeated_response")
        previous_error = raised.exception.candidate_attempt_trace[-1][
            "previous_validation_error"]
        self.assertIn("topic discovery package keys do not match", previous_error)
        self.assertIn(f"previous validation failure: {previous_error}",
                      str(raised.exception))
        self.assertEqual(
            raised.exception.topic_response_repair["previous_validation_error"],
            previous_error)

    def test_repeated_response_keeps_its_validation_error_across_provider_error(self):
        class InterruptedRepeatedResponseModel(FakeModel):
            calls = 0

            def complete(self, *, system, prompt, images=None):
                type(self).calls += 1
                if type(self).calls == 2:
                    raise ModelCallError(
                        "temporary provider interruption", outcome_known=False,
                        attempts=1, elapsed_seconds=0.01)
                return ModelResult(
                    text='{"not": "a topic package"}', model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")

        with patch("scisaurus.runtime.topic_discovery.ModelClient",
                   InterruptedRepeatedResponseModel):
            with self.assertRaisesRegex(
                    ValidationError, "byte-identical rejected response") as raised:
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                    "timeout_seconds": 1, "max_output_tokens": 4096,
                }).run("Choose a feasible research direction", candidate_count=3,
                       bibliography=False, maturity_review_rounds=0, max_attempts=4)
        previous_error = raised.exception.topic_response_repair["previous_validation_error"]
        self.assertIn("topic discovery package keys do not match", previous_error)
        self.assertNotIn("temporary provider interruption", previous_error)

    def test_topic_package_shape_error_names_missing_and_unexpected_fields(self):
        missing = package("objective")
        missing.pop("selected_id")
        with self.assertRaisesRegex(ValidationError, "missing=.*selected_id"):
            validate_topic_package(missing)

        unexpected = package("objective")
        unexpected["commentary"] = "not part of the scientific package"
        with self.assertRaisesRegex(ValidationError, "unexpected=.*commentary"):
            validate_topic_package(unexpected)

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

    def test_topic_package_accepts_immutable_operational_principal_objective(self):
        objective = (
            "Run an autonomous research workflow under explicit evidence, capability, and "
            "human-release gates; select a testable question, validate it, and produce a paper.")
        value = package(objective)
        self.assertEqual(
            validate_topic_package(value, objective=objective, candidate_count=3), value)

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

    def test_parent_refinement_checks_sibling_questions_without_rejecting_shared_shape(self):
        topic_id = "direction_mhd_hall_scaling"
        parent = {
            "id": topic_id, "title": "Hall reconnection rate scaling",
            "domain": "Magnetohydrodynamics",
            "research_question": "Does the normalized reconnection rate plateau across ion and electron inertial scales?",
            "research_form": "theory_simulation",
            "evidence_mode": "analytical_derivation",
            "comparison_type": "mechanism_ablation",
        }
        rejected_sibling = {
            **parent,
            "title": "Whistler-term ablation and reconnection rate scaling",
            "research_question": (
                "Does removing the whistler dispersive term change the normalized Hall-MHD "
                "reconnection rate across the ion-to-electron scale sweep?"),
            "research_form": "experimental_design",
            "comparison_type": "scaling_transition",
        }
        adjacent_refinement = {
            **parent,
            "title": "Guide-field control of electron heating partition",
            "research_question": (
                "In collisionless magnetotail reconnection, how does guide-field strength "
                "shift the partition of released magnetic energy between ion and electron heating?"),
            "research_form": "experimental_design",
            "comparison_type": "model_selection",
        }
        history = {"entries": [
            {"topic_id": topic_id, **parent, "signature": topic_signature(parent)},
            {"topic_id": topic_id, **rejected_sibling,
             "signature": topic_signature(rejected_sibling)},
        ]}
        self.assertTrue(validate_topic_novelty(
            adjacent_refinement, history, lineage_topic_id=topic_id))
        with self.assertRaisesRegex(ValidationError, "too similar"):
            validate_topic_novelty(
                rejected_sibling, history, lineage_topic_id=topic_id)
        with self.assertRaisesRegex(ValidationError, "too similar"):
            validate_topic_novelty(adjacent_refinement, history)

    def test_parent_refinement_ignores_unrelated_projects_portfolio_shape(self):
        topic_id = "direction_mhd_hall_scaling"
        candidate = {
            "id": topic_id,
            "title": "Guide-field control of electron heating partition",
            "domain": "Magnetohydrodynamics",
            "research_question": (
                "In collisionless magnetotail reconnection, how does guide-field strength "
                "shift the partition of released magnetic energy between ion and electron heating?"),
            "research_form": "experimental_design",
            "evidence_mode": "analytical_derivation",
            "comparison_type": "model_selection",
        }
        unrelated_project = {
            "id": "direction_other_mhd",
            "title": "A distinct MHD question",
            "domain": "Magnetohydrodynamics",
            "research_question": (
                "How does boundary curvature alter instability onset in a driven plasma sheet?"),
            "research_form": "experimental_design",
            "evidence_mode": "analytical_derivation",
            "comparison_type": "model_selection",
        }
        history = {"entries": [{
            "topic_id": unrelated_project["id"],
            **unrelated_project,
            "signature": topic_signature(unrelated_project),
        }]}

        with self.assertRaisesRegex(ValidationError, "too similar"):
            validate_topic_novelty(candidate, history)
        self.assertTrue(validate_topic_novelty(
            candidate, history, lineage_topic_id=topic_id))

    def test_parent_phenomenon_identity_normalizes_plural_and_requires_setting(self):
        parent = {
            "id": "direction_mhd_hall_scaling",
            "phenomenon": "Fast reconnection rates in planetary magnetotails",
            "research_question": "Does the reconnection rate saturate in planetary magnetotails?",
        }
        candidate = {
            **parent,
            "research_question": (
                "In a Harris current sheet, does the normalized reconnection rate change "
                "across the ion-to-electron inertial scale boundary?"),
        }
        with self.assertRaisesRegex(ValidationError, "no longer addresses the parent's phenomenon"):
            validate_topic_refinement(
                parent, candidate,
                salvage_plan=topic_salvage_plan(force_structural_pivot=True),
                salvage_anchor=parent,
            )
        candidate["research_question"] = (
            "In planetary magnetotail reconnection, does the fast reconnection rate change "
            "across the ion-to-electron inertial scale boundary?")
        validate_topic_refinement(
            parent, candidate,
            salvage_plan=topic_salvage_plan(force_structural_pivot=True),
            salvage_anchor=parent,
        )

    def test_saturated_history_allows_a_cold_question_to_reuse_an_archetype(self):
        forms = ("theory_simulation", "observational_reanalysis", "experimental_design",
                 "methodological_benchmark")
        modes = ("analytical_derivation", "synthetic_simulation", "published_observations")
        comparisons = ("mechanism_ablation", "model_selection", "cross_method")
        entries = []
        for index in range(60):
            prior = {
                "topic_id": f"old_{index}",
                "title": f"Prior direction {index}",
                "domain": f"prior domain {index}",
                "research_question": f"How does prior phenomenon {index} change under its test condition?",
                "research_form": forms[index % len(forms)],
                "evidence_mode": modes[(index // len(forms)) % len(modes)],
                "comparison_type": comparisons[(index // (len(forms) * len(modes))) % len(comparisons)],
            }
            entries.append(prior)
        candidate = {
            "id": "new_resonator_direction",
            "title": "Boundary resonance in cryogenic phonon transport",
            "domain": "quantum acoustics",
            "research_question": "Which localization threshold emerges for phonon packets in a disordered resonator?",
            "research_form": entries[0]["research_form"],
            "evidence_mode": entries[0]["evidence_mode"],
            "comparison_type": entries[0]["comparison_type"],
        }
        history = {
            "entries": entries,
            "history_summary": {
                "total_entries": len(entries),
                "distinct_structure_fingerprints": 12,
            },
        }
        self.assertTrue(validate_topic_novelty(candidate, history))

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

    def test_parent_identity_violation_is_a_rejected_refinement_not_a_format_error(self):
        error = ValidationError(
            "topic salvage branch structural_pivot must preserve the parent's phenomenon")
        trace = [{
            "status": "refinement_rejected",
            "rejection_type": "refinement",
            "error": str(error),
        }]

        self.assertEqual(_topic_validation_rejection_type(error), "refinement")
        self.assertEqual(
            _topic_retry_reason(error, trace, []),
            "scientific_candidate_rejected",
        )

    def test_salvage_scope_rejection_remains_a_refinement_contract_failure(self):
        error = ValidationError(
            "topic salvage branch evidence-boundary must materially change at least one "
            "of its assigned dimensions: evidence_mode, research_form, scope, comparison_type")
        trace = [{
            "status": "refinement_rejected",
            "rejection_type": "refinement_contract",
            "error": str(error),
        }]
        self.assertEqual(
            _topic_retry_reason(error, trace, []), "refinement_contract_failure")

    def test_salvage_anchor_survives_local_reparenting_during_repair(self):
        original = package("Choose a feasible research direction")["candidates"][1]
        original["phenomenon"] = "Ionotropic receptor desensitization"
        local_parent = {
            **original,
            "phenomenon": "Exciton transport coherence",
            "research_question": "A locally revised but unrelated question.",
        }
        child = {
            **local_parent,
            "research_question": "A further polished unrelated question.",
            "research_form": "scaling_boundary",
            "evidence_mode": "analytical_derivation",
        }
        with self.assertRaisesRegex(ValidationError, "must preserve the parent's phenomenon"):
            validate_topic_refinement(
                local_parent, child,
                require_structural_pivot=True,
                salvage_plan=topic_salvage_plan(force_structural_pivot=True),
                salvage_anchor=original,
            )

    def test_salvage_refinement_preserves_phenomenon_while_strengthening_design(self):
        value = package("Choose a feasible research direction")
        parent = value["candidates"][0]
        parent["phenomenon"] = "nitrogen methane frost sublimation gradients"
        parent["mechanism"] = "coupled frost and clathrate thermodynamics"
        parent["measurement"] = "surface temperature"
        child = {
            **parent,
            "research_question": "For nitrogen-methane frost, does a bounded surface-temperature window separate frost and clathrate predictions?",
            "mechanism": "temperature-dependent phase-transition competition",
            "measurement": "N2/CH4 ratio over the supported temperature interval",
        }
        salvage = topic_salvage_plan()
        validate_topic_refinement(
            parent, child, salvage_plan=salvage)

        unrelated_question = {
            **child,
            "research_question": "Does glutamate receptor cavity selectivity change across membrane conditions?",
        }
        with self.assertRaisesRegex(
                ValidationError, "research question no longer addresses the parent's phenomenon"):
            validate_topic_refinement(parent, unrelated_question, salvage_plan=salvage)

        unrelated_scope_change = {**child, "domain": "plant receptor pharmacology"}
        with self.assertRaisesRegex(ValidationError, "outside its assigned repair scope"):
            validate_topic_refinement(
                parent, unrelated_scope_change, salvage_plan=salvage)

        baseline_plan = topic_salvage_plan(["mechanism-observable"])
        baseline_refinement = {
            **parent,
            "research_question": "Does the nitrogen-methane frost-versus-clathrate ordering reverse across the supported temperature range?",
            "comparison": "phase-transition ordering across bounded temperature bins",
            "data_regime": "published N2/CH4 measurements within the documented temperature window",
            "disconfirmation_test": "the ordering remains unchanged across all supported bins",
        }
        validate_topic_refinement(
            parent, baseline_refinement, salvage_plan=baseline_plan)

        mechanism_plan = topic_salvage_plan()
        mechanism_parent = {
            **parent,
            "phenomenon": "nitrogen-methane frost sublimation gradients",
            "research_question": (
                "In nitrogen-methane frost sublimation, does coupled frost/clathrate "
                "thermodynamics create a surface temperature gradient?"),
            "mechanism": "coupled frost and clathrate thermodynamics",
            "measurement": "surface temperature",
        }
        mechanism_refinement = {
            **mechanism_parent,
            "research_question": (
                "In nitrogen-methane frost sublimation, does phase-transition competition "
                "change the replicate-level N2/CH4 contrast across bounded temperature bins?"),
            "mechanism": "temperature-dependent phase-transition competition",
            "measurement": "replicate-level N2/CH4 ratio across temperature bins",
            "theory_target": "the boundary where frost and clathrate predictions diverge",
            "comparison_type": "scaling_transition",
            "comparison": "phase-transition ordering across bounded temperature bins",
            "data_regime": "published N2/CH4 measurements within the documented temperature window",
            "scope": "the supported nitrogen-methane frost regime only",
            "disconfirmation_test": "the ordering remains unchanged across all supported bins",
        }
        validate_topic_refinement(
            mechanism_parent, mechanism_refinement, salvage_plan=mechanism_plan)

        dependent_only = {
            **mechanism_parent,
            "research_question": (
                "In nitrogen-methane frost sublimation, does the temperature window "
                "separate frost and clathrate predictions?"),
            "comparison": "phase-transition ordering across bounded bins",
            "data_regime": "published N2/CH4 measurements",
            "scope": "the supported nitrogen-methane frost regime only",
            "disconfirmation_test": "the ordering remains unchanged across all supported bins",
        }
        with self.assertRaisesRegex(ValidationError, "must materially change at least one"):
            validate_topic_refinement(
                mechanism_parent, dependent_only, salvage_plan=mechanism_plan)

        evidence_parent = {
            **mechanism_parent,
            "phenomenon": "confined cornstarch thickening onset",
            "research_question": (
                "Does the onset-gap slope remain negative under a fixed analytical operator?"),
            "evidence_mode": "analytical_derivation",
            "research_form": "scaling_boundary",
            "scope": "the supported confined-cornstarch model regime",
            "comparison_type": "operator_sensitivity",
        }
        evidence_refinement = {
            **evidence_parent,
            "research_question": (
                "Does the confined-cornstarch onset-gap slope remain negative under a "
                "synthetic operator calibration?"),
            "evidence_mode": "synthetic_simulation",
            "theory_target": "operator sensitivity of the onset-gap slope",
            "experiment_capability_id": "synthetic_onset_operator_calibration",
        }
        evidence_plan = topic_salvage_plan([
            "mechanism-observable", "comparison-baseline",
        ])
        changed = validate_topic_refinement(
            evidence_parent, evidence_refinement, salvage_plan=evidence_plan)
        self.assertEqual(changed, [
            "research_question", "theory_target", "evidence_mode", "experiment_capability_id",
        ])

        child["phenomenon"] = "plant glutamate receptor cavity selectivity"
        for plan in (
                salvage,
                topic_salvage_plan([
                    "mechanism-observable", "comparison-baseline", "evidence-boundary",
                ]),
                topic_salvage_plan(force_structural_pivot=True)):
            with self.assertRaisesRegex(ValidationError, "must preserve the parent's phenomenon"):
                validate_topic_refinement(
                    parent, child, salvage_plan=plan)

        evidence_authorized = {
            **salvage,
            "phenomenon_change_authorized": True,
            "phenomenon_change_evidence_ref": "artifact:survey/refuting-evidence@1",
            "phenomenon_change_rationale": "The source set directly refutes the parent phenomenon.",
        }
        with self.assertRaisesRegex(ValidationError, "must preserve the parent's phenomenon"):
            validate_topic_refinement(
                parent, child, salvage_plan=evidence_authorized)
        with self.assertRaisesRegex(ValidationError, "must preserve the parent's phenomenon"):
            validate_topic_refinement(parent, child, salvage_anchor=parent)

    def test_salvage_plan_advances_bounded_repairs_before_structural_pivot(self):
        first = topic_salvage_plan()
        self.assertEqual(first["mode"], "salvage")
        self.assertEqual(first["active_branch"]["id"], "mechanism-observable")
        self.assertEqual(first["remaining_branch_ids"], [
            "comparison-baseline", "evidence-boundary",
        ])
        second = topic_salvage_plan(["mechanism-observable"])
        self.assertEqual(second["active_branch"]["id"], "comparison-baseline")
        third = topic_salvage_plan([
            "mechanism-observable", "comparison-baseline",
        ])
        self.assertEqual(third["active_branch"]["id"], "evidence-boundary")
        exhausted = topic_salvage_plan([
            "mechanism-observable", "comparison-baseline", "evidence-boundary",
        ])
        self.assertEqual(exhausted["mode"], "structural_pivot")
        self.assertTrue(exhausted["exhausted"])
        forced = topic_salvage_plan(["mechanism-observable"], force_structural_pivot=True)
        self.assertEqual(forced["mode"], "structural_pivot")
        self.assertTrue(forced["forced"])

    def test_topic_prompt_exposes_active_salvage_branch_and_rejects_cosmetic_repair(self):
        value = package("Choose a feasible research direction")
        plan = topic_salvage_plan()
        payload = json.loads(topic_prompt(
            "Choose a feasible research direction", 3,
            refinement_context={
                "mode": "refinement", "cycle": 2,
                "parent_topic_id": value["candidates"][0]["id"],
                "parent_topic": value["candidates"][0],
                "salvage_plan": plan,
            },
        ))
        self.assertEqual(
            payload["refinement_context"]["salvage_plan"]["active_branch"]["id"],
            "mechanism-observable",
        )
        self.assertIn(
            "scope",
            payload["refinement_context"]["salvage_plan"]["active_branch"][
                "dependent_dimensions"],
        )
        self.assertTrue(any(
            "bounded salvage branch" in item for item in payload["constraints"]
        ))
        self.assertTrue(any(
            "dependent fields when needed" in item for item in payload["constraints"]
        ))
        self.assertTrue(any(
            "Preserve the parent's phenomenon field" in item
            for item in payload["constraints"]
        ))

    def test_salvage_plan_without_parent_fails_before_model_dispatch(self):
        MaturityModel.calls = []
        with patch("scisaurus.runtime.topic_discovery.ModelClient", MaturityModel):
            with self.assertRaisesRegex(
                    ValidationError, "requires its parent_topic anchor"):
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake",
                    "protocol": "ollama", "timeout_seconds": 1,
                    "max_output_tokens": 4096,
                }).run(
                    "Choose a feasible research direction", candidate_count=3,
                    bibliography=False,
                    refinement_context={"salvage_plan": topic_salvage_plan()},
                    maturity_review_rounds=0, max_attempts=3,
                )
        self.assertEqual(MaturityModel.calls, [])

    def test_identical_branch_response_is_stopped_after_one_repair_attempt(self):
        class IdenticalSalvageResponseModel:
            calls = 0

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                type(self).calls += 1
                payload = json.loads(prompt)
                response = package(payload["principal_objective"])
                for candidate in response["candidates"]:
                    candidate["domain"] = "Unrelated domain"
                return ModelResult(
                    text=json.dumps(response), model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")

        parent = package("Choose a feasible research direction")["candidates"][1]
        context = {
            "mode": "refinement", "cycle": 2,
            "parent_topic_id": parent["id"], "parent_topic": parent,
            "salvage_plan": topic_salvage_plan(),
        }
        with patch("scisaurus.runtime.topic_discovery.ModelClient",
                   IdenticalSalvageResponseModel):
            with self.assertRaises(ValidationError) as caught:
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake",
                    "protocol": "ollama", "timeout_seconds": 1,
                    "max_output_tokens": 4096,
                }).run(
                    "Choose a feasible research direction", candidate_count=3,
                    bibliography=False, refinement_context=context,
                    maturity_review_rounds=0, max_attempts=5,
                )
        self.assertEqual(IdenticalSalvageResponseModel.calls, 2)
        self.assertEqual(
            caught.exception.candidate_attempt_trace[-1]["status"],
            "repeated_response")
        self.assertIn(
            "previous_validation_error",
            caught.exception.candidate_attempt_trace[-1])

    def test_runner_records_salvage_branch_in_topic_evolution(self):
        MaturityModel.calls = []
        MaturityModel.review_count = 0
        parent = package("Choose a feasible research direction")["candidates"][1]
        refinement_context = {
            "mode": "refinement",
            "cycle": 4,
            "parent_topic_id": parent["id"],
            "parent_topic": parent,
            "reason": "survey repair",
            "salvage_plan": topic_salvage_plan(),
        }
        with patch("scisaurus.runtime.topic_discovery.validate_topic_refinement",
                   wraps=validate_topic_refinement) as refinement_validator:
            with patch("scisaurus.runtime.topic_discovery.ModelClient", MaturityModel):
                result = TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                    "timeout_seconds": 1, "max_output_tokens": 4096,
                }).run(
                    "Choose a feasible research direction", candidate_count=3,
                    bibliography=False, refinement_context=refinement_context,
                    maturity_review_rounds=0, max_attempts=2,
                )
        self.assertEqual(
            refinement_validator.call_args.kwargs["salvage_anchor"], parent)
        salvage = result["topic_evolution"]["salvage"]
        self.assertEqual(salvage["branch_id"], "mechanism-observable")
        self.assertEqual(salvage["attempted_branch_ids"], ["mechanism-observable"])
        self.assertEqual(
            salvage["remaining_branch_ids"],
            ["comparison-baseline", "evidence-boundary"],
        )

    def test_response_contract_only_is_not_recorded_as_scientific_refinement(self):
        MaturityModel.calls = []
        MaturityModel.review_count = 0
        parent_package = package("Choose a feasible research direction")
        parent = next(item for item in parent_package["candidates"]
                      if item["id"] == parent_package["selected_id"])
        context = {
            "mode": "response_contract_repair",
            "cycle": 9,
            "parent_topic_id": parent["id"],
            "parent_topic": parent,
            "response_contract_repair": {
                "validation_error": "The previous response omitted the required package keys.",
            },
        }

        with patch("scisaurus.runtime.topic_discovery.ModelClient", MaturityModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run(
                "Choose a feasible research direction", candidate_count=3,
                bibliography=False, refinement_context=context,
                maturity_review_rounds=0, max_attempts=1,
            )

        self.assertEqual(result["topic"]["id"], parent["id"])
        self.assertEqual(result["topic_evolution"]["mode"], "response_contract_repair")
        self.assertEqual(result["topic_evolution"]["changed_dimensions"], [])

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

    def test_topic_pivot_prompt_carries_prior_response_contract_failure(self):
        parent = package("Choose a feasible research direction")["candidates"][1]
        diagnostic = "topic candidate search query is not anchored to its scientific direction"
        payload = json.loads(topic_prompt(
            "Choose a feasible research direction", 3,
            refinement_context={
                "mode": "refinement", "cycle": 3,
                "parent_topic_id": parent["id"], "parent_topic": parent,
                "work_orders": [{
                    "id": "pivot-1", "kind": "topic_refinement",
                    "objective": "Generate a materially different computational research question.",
                    "success_condition": "The new question is source-grounded and executable.",
                }],
                "response_contract_repair": {"validation_error": diagnostic},
            },
        ))
        self.assertEqual(
            payload["refinement_context"]["work_orders"][0]["objective"],
            "Generate a materially different computational research question.",
        )
        self.assertEqual(
            payload["refinement_context"]["response_contract_repair"]["validation_error"],
            diagnostic,
        )
        self.assertTrue(any(diagnostic in item for item in payload["constraints"]))
        self.assertTrue(any(
            "response-contract recovery" in item for item in payload["constraints"]
        ))
        self.assertTrue(any(
            "directly satisfy the assigned topic work order" in item
            for item in payload["constraints"]
        ))

    def test_parent_refinement_reuses_parent_seed_and_evidence(self):
        class CaptureModel:
            prompt = None

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                type(self).prompt = prompt
                return ModelResult(
                    text="{}", model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 5},
                    elapsed_seconds=0.01, finish_reason="stop")

        parent = package("Choose a feasible research direction")["candidates"][1]
        parent.update({
            "id": "direction_mhd_hall_scaling",
            "title": "Hall-Induced Reconnection Rate Scaling Boundary",
            "domain": "Magnetohydrodynamics",
            "phenomenon": "Fast reconnection rates in planetary magnetotails",
            "mechanism": "Hall effect from decoupled ion/electron motions",
            "frontier_seed_id": "frontier_1",
            "search_queries": [
                "collisionless reconnection rate d_i/d_e scaling",
                "Hall effect reconnection saturation boundary",
            ],
        })
        parent_work = {
            "work_id": "W_PARENT", "title": "Collisionless magnetic reconnection",
            "abstract": "Observations of Hall signatures in magnetotail reconnection.",
            "frontier_seed_id": "selected_direction", "year": 2022,
        }
        other_frontier_work = {
            "work_id": "W_OTHER", "title": "An unrelated optogenetics study",
            "abstract": "Synaptic photostimulation timing.",
            "frontier_seed_id": "frontier_3", "year": 2023,
        }
        refinement = {
            "mode": "refinement", "cycle": 448,
            "parent_topic_id": parent["id"], "parent_topic": parent,
            "objective": "Refine the reconnection question within its retained phenomenon.",
            "parent_evidence": {
                "frontier_seed_plan": {
                    "schema_version": "topic-frontier-seeds-1",
                    "seeds": [
                        {"id": "frontier_1", "domain": parent["domain"],
                         "phenomenon": parent["phenomenon"],
                         "mechanism": parent["mechanism"],
                         "unit_of_analysis": parent["scope"],
                         "search_queries": parent["search_queries"]},
                        {"id": "frontier_3", "domain": "Optogenetics",
                         "phenomenon": "Temporal precision of synaptic potentials",
                         "mechanism": "Opsin kinetics", "unit_of_analysis": "waveform",
                         "search_queries": ["opsin kinetics synaptic timing"]},
                    ],
                },
                "recent_papers": [parent_work, other_frontier_work],
                "candidate_prior_work": [parent_work],
            },
            "work_orders": [{
                "id": "refine-parent", "kind": "topic_refinement",
                "objective": "Refine the reconnection question within its retained phenomenon.",
            }],
            "salvage_plan": topic_salvage_plan(force_structural_pivot=True),
        }
        runner = TopicDiscoveryRunner({
            "base_url": "http://example.invalid", "model": "fake",
            "protocol": "ollama", "timeout_seconds": 1,
            "max_output_tokens": 4096,
        })
        with patch("scisaurus.runtime.topic_discovery.ModelClient", CaptureModel), \
                patch.object(TopicDiscoveryRunner, "_generate_frontier_seed_plan",
                             side_effect=AssertionError("unrelated seed portfolio regenerated")), \
                patch.object(TopicDiscoveryRunner, "_recent_paper_sample",
                             side_effect=AssertionError("cached parent evidence ignored")):
            with self.assertRaises(ValidationError):
                runner.run(
                    "Refine the reconnection question within its retained phenomenon.",
                    candidate_count=4, max_attempts=1, bibliography=False,
                    sampling_seed=123, refinement_context=refinement,
                )
        payload = json.loads(CaptureModel.prompt)
        self.assertEqual(payload["assignment"], "repair_selected_topic_candidate")
        self.assertEqual([item["id"] for item in payload["frontier_seeds"]], ["frontier_1"])
        self.assertEqual(
            {item["work_id"] for item in payload["target_seed_records"]}, {"W_PARENT"})
        self.assertTrue(all(
            item["frontier_seed_id"] == "frontier_1"
            for item in payload["target_seed_records"]))
        self.assertEqual(
            payload["parent_candidate"]["phenomenon"], parent["phenomenon"])
        self.assertEqual(
            payload["composer_repair_context"]["work_orders"][0]["objective"],
            refinement["work_orders"][0]["objective"],
        )
        self.assertTrue(any(
            "phenomenon field identical" in item for item in payload["constraints"]))

    def test_parent_refinement_admits_one_grounded_candidate_without_portfolio_restart(self):
        class DirectCandidateModel:
            calls = 0
            prompt = None

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                type(self).calls += 1
                type(self).prompt = prompt
                payload = json.loads(prompt)
                self.asserted_assignment = payload.get("assignment")
                candidate = deepcopy(payload["parent_candidate"])
                candidate.update(payload["required_shape"])
                candidate.update({
                    "title": "Hall-mediated magnetotail reconnection boundary",
                    "research_question": (
                        "In planetary magnetotail reconnection, does Hall-mediated flux transfer "
                        "change across the electron-to-ion inertial scale boundary?"),
                    "phenomenon": payload["parent_candidate"]["phenomenon"],
                    "mechanism": "Hall-mediated electron-ion decoupling at the reconnection layer",
                    "measurement": "Measure normalized reconnection rate at the retained inertial-scale boundary.",
                    "frontier_seed_id": payload["target_frontier_seed_id"],
                })
                return ModelResult(
                    text=json.dumps(candidate), model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")

        parent = package("Choose a feasible research direction")["candidates"][1]
        parent.update({
            "id": "direction_mhd_hall_scaling",
            "title": "Hall-Induced Reconnection Rate Scaling Boundary",
            "domain": "Magnetohydrodynamics",
            "phenomenon": "Fast reconnection rates in planetary magnetotails",
            "mechanism": "Hall effect from decoupled ion/electron motions",
            "frontier_seed_id": "frontier_1",
            "prior_work_ids": ["W_PARENT"],
            "search_queries": [
                "collisionless reconnection rate d_i/d_e scaling",
                "Hall effect reconnection saturation boundary",
                "planetary magnetotail reconnection rate",
            ],
        })
        parent_work = {
            "work_id": "W_PARENT", "title": "Collisionless magnetic reconnection",
            "abstract": "Hall signatures in planetary magnetotail reconnection rates.",
            "frontier_seed_id": "selected_direction", "year": 2022,
        }
        refinement = {
            "mode": "refinement", "cycle": 449,
            "parent_topic_id": parent["id"], "parent_topic": parent,
            "reason": "The literature gap needs a sharper measurable boundary.",
            "parent_evidence": {
                "frontier_seed_plan": {
                    "schema_version": "topic-frontier-seeds-1",
                    "seeds": [{
                        "id": "frontier_1", "domain": parent["domain"],
                        "phenomenon": parent["phenomenon"],
                        "mechanism": parent["mechanism"],
                        "unit_of_analysis": parent["scope"],
                        "search_queries": parent["search_queries"],
                    }],
                },
                "recent_papers": [parent_work],
                "candidate_prior_work": [parent_work],
            },
            "work_orders": [{
                "id": "refine-parent", "kind": "topic_refinement",
                "objective": "Quantify the reconnection boundary without changing magnetotail phenomena.",
                "success_condition": "An explicit boundary and rate observable enter survey.",
                "evidence_needed": "Use the retained Hall-reconnection paper W_PARENT.",
            }],
            "rejected_directions": [{
                "title": "Previously rejected rate ablation",
                "research_question": "Does ablating the Hall term change the reconnection rate?",
                "rejection_reason": "This estimand repeats the prior rate comparison.",
            }],
            "salvage_plan": topic_salvage_plan(),
        }
        DirectCandidateModel.calls = 0
        with patch("scisaurus.runtime.topic_discovery.ModelClient", DirectCandidateModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake",
                "protocol": "ollama", "timeout_seconds": 1,
                "max_output_tokens": 4096,
            }).run(
                "Quantify the reconnection boundary without changing magnetotail phenomena.",
                candidate_count=4, max_attempts=1, bibliography=False,
                sampling_seed=123, refinement_context=refinement,
            )
        self.assertEqual(DirectCandidateModel.calls, 1)
        prompt = json.loads(DirectCandidateModel.prompt)
        self.assertEqual(prompt["assignment"], "repair_selected_topic_candidate")
        self.assertEqual(
            prompt["composer_repair_context"]["rejected_directions"],
            refinement["rejected_directions"],
        )
        self.assertTrue(any(
            "hard negative examples" in item for item in prompt["constraints"]))
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["topic"]["id"], parent["id"])
        self.assertEqual(result["topic"]["phenomenon"], parent["phenomenon"])
        self.assertEqual(result["topic"]["research_form"], parent["research_form"])
        self.assertEqual(result["topic"]["evidence_mode"], parent["evidence_mode"])
        self.assertEqual(result["topic"]["comparison_type"], prompt["required_shape"]["comparison_type"])
        self.assertEqual(result["topic_evolution"]["mode"], "refinement")
        self.assertTrue(result["topic_evolution"]["changed_dimensions"])

        class NoCallModel:
            def __init__(self, **config):
                raise AssertionError("unbounded portfolio fallback must not call a model")

        with patch("scisaurus.runtime.topic_discovery.ModelClient", NoCallModel), \
                patch("scisaurus.runtime.topic_discovery._refinement_target_seed",
                      return_value=None), \
                patch.object(TopicDiscoveryRunner, "_recent_paper_sample",
                             return_value=([parent_work], 123, [])):
            with self.assertRaisesRegex(
                    ValidationError, "bounded single-candidate topic refinement cannot proceed"):
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake",
                    "protocol": "ollama", "timeout_seconds": 1,
                    "max_output_tokens": 4096,
                }).run(
                    "Quantify the reconnection boundary without changing magnetotail phenomena.",
                    candidate_count=4, max_attempts=1, bibliography=False,
                    sampling_seed=123, refinement_context=refinement,
                )

    def test_parent_refinement_retries_recorded_novelty_rejection_with_local_negative_memory(self):
        class NoveltyThenDistinctCandidateModel:
            calls = []
            prompts = []

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                payload = json.loads(prompt)
                type(self).calls.append(payload.get("assignment"))
                type(self).prompts.append(payload)
                candidate = deepcopy(payload["parent_candidate"])
                candidate.update(payload["required_shape"])
                is_repair = len(type(self).calls) > 1
                candidate.update({
                    "title": (
                        "Guide-field control of magnetotail reconnection-rate plateau"
                        if is_repair else "Hall-mediated magnetotail reconnection flux transfer"),
                    "research_question": (
                        "In planetary magnetotail reconnection, how does guide-field strength "
                        "shift the fast reconnection-rate plateau between low and high guide-field regimes?"
                        if is_repair else
                        "In planetary magnetotail reconnection, does Hall-mediated flux transfer "
                        "change across the electron-to-ion inertial scale boundary?"),
                    "phenomenon": payload["parent_candidate"]["phenomenon"],
                    "mechanism": (
                        "Guide-field strength shifts the fast reconnection-rate plateau"
                        if is_repair else "Hall-mediated electron-ion decoupling"),
                    "measurement": "Measure normalized fast reconnection rate across the stated boundary.",
                    "frontier_seed_id": payload["target_frontier_seed_id"],
                    "prior_work_ids": ["W_PARENT"],
                    "search_queries": [
                        "planetary magnetotail reconnection rate boundary",
                        "guide-field reconnection rate plateau",
                        "magnetotail fast reconnection observations",
                    ],
                })
                return ModelResult(
                    text=json.dumps({"candidate": candidate}), model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")

        parent = package("Choose a feasible research direction")["candidates"][1]
        parent.update({
            "id": "direction_mhd_hall_scaling",
            "title": "Hall-Induced Reconnection Rate Scaling Boundary",
            "domain": "Magnetohydrodynamics",
            "phenomenon": "Fast reconnection rates in planetary magnetotails",
            "mechanism": "Hall effect from decoupled ion/electron motions",
            "frontier_seed_id": "frontier_1",
            "prior_work_ids": ["W_PARENT"],
            "search_queries": [
                "collisionless reconnection rate d_i/d_e scaling",
                "Hall effect reconnection saturation boundary",
            ],
        })
        parent_work = {
            "work_id": "W_PARENT", "title": "Collisionless magnetic reconnection",
            "abstract": "Hall signatures in planetary magnetotail reconnection rates.",
            "frontier_seed_id": "selected_direction", "year": 2022,
        }
        rejected = deepcopy(parent)
        rejected.update({
            "id": "direction_rejected_hall_flux",
            "title": "Hall-mediated magnetotail reconnection flux transfer",
            "research_question": (
                "In planetary magnetotail reconnection, does Hall-mediated flux transfer "
                "change across the electron-to-ion inertial scale boundary?"),
        })
        refinement = {
            "mode": "refinement", "cycle": 550,
            "parent_topic_id": parent["id"], "parent_topic": parent,
            "reason": "Continue the retained reconnection phenomenon after a novelty rejection.",
            "parent_evidence": {
                "frontier_seed_plan": {
                    "schema_version": "topic-frontier-seeds-1",
                    "seeds": [{
                        "id": "frontier_1", "domain": parent["domain"],
                        "phenomenon": parent["phenomenon"],
                        "mechanism": parent["mechanism"],
                        "unit_of_analysis": parent["scope"],
                        "search_queries": parent["search_queries"],
                    }],
                },
                "recent_papers": [parent_work],
                "candidate_prior_work": [parent_work],
            },
            "work_orders": [{
                "id": "refine-parent", "kind": "topic_refinement",
                "objective": "Refine the reconnection boundary without abandoning the parent phenomenon.",
                "success_condition": "Produce a distinct, measurable reconnection-rate comparison.",
            }],
        }
        runtime_context = {"topic_history": {"entries": [{
            "topic_id": rejected["id"], **rejected,
            "signature": topic_signature(rejected),
        }]}}
        NoveltyThenDistinctCandidateModel.calls = []
        NoveltyThenDistinctCandidateModel.prompts = []

        with patch("scisaurus.runtime.topic_discovery.ModelClient",
                   NoveltyThenDistinctCandidateModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake",
                "protocol": "ollama", "timeout_seconds": 1,
                "max_output_tokens": 4096,
            }).run(
                "Refine the reconnection question within its retained phenomenon.",
                candidate_count=4, max_attempts=3, bibliography=False,
                sampling_seed=123, refinement_context=refinement,
                runtime_context=runtime_context, maturity_review_rounds=0,
            )

        self.assertEqual(NoveltyThenDistinctCandidateModel.calls, [
            "repair_selected_topic_candidate", "repair_selected_topic_candidate",
        ])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["topic"]["id"], parent["id"])
        self.assertEqual(result["topic"]["phenomenon"], parent["phenomenon"])
        self.assertEqual(result["candidate_attempt_trace"][0]["rejection_type"], "novelty")
        self.assertEqual(result["candidate_attempt_trace"][0]["status"], "rejected")
        self.assertEqual(result["candidate_attempt_trace"][1]["status"], "admitted")
        self.assertEqual(result["usage"]["model_calls"], 2)
        rejected_directions = NoveltyThenDistinctCandidateModel.prompts[1][
            "composer_repair_context"]["rejected_directions"]
        self.assertTrue(any(
            item.get("research_question") == rejected["research_question"]
            and "too similar" in item.get("rejection_reason", "")
            for item in rejected_directions
        ))
        self.assertTrue(any(
            "hard negative examples" in item
            for item in NoveltyThenDistinctCandidateModel.prompts[1]["constraints"]
        ))

    def test_distinct_novelty_rejections_return_control_after_one_local_repair(self):
        class RepeatedNoveltyModel:
            calls = 0

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                payload = json.loads(prompt)
                type(self).calls += 1
                value = package(payload["principal_objective"])
                value["selected_id"] = (
                    "direction_0" if type(self).calls == 1 else "direction_2")
                value["selection_rationale"] += f" proposal {type(self).calls}"
                return ModelResult(
                    text=json.dumps(value), model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")

        initial = package("Choose a feasible research direction")
        history = {"entries": [{
            "topic_id": item["id"], "title": item["title"],
            "domain": item["domain"], "research_question": item["research_question"],
            "signature": topic_signature(item),
        } for item in initial["candidates"]]}
        RepeatedNoveltyModel.calls = 0

        with patch("scisaurus.runtime.topic_discovery.ModelClient", RepeatedNoveltyModel):
            with self.assertRaises(ValidationError) as caught:
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake",
                    "protocol": "ollama", "timeout_seconds": 1,
                    "max_output_tokens": 4096,
                }).run(
                    "Choose a feasible research direction", candidate_count=3,
                    max_attempts=8, bibliography=False,
                    runtime_context={"topic_history": history},
                    maturity_review_rounds=0,
                )

        self.assertEqual(
            RepeatedNoveltyModel.calls, MAX_CONSECUTIVE_TOPIC_NOVELTY_REJECTIONS)
        trace = caught.exception.candidate_attempt_trace
        self.assertEqual(len(trace), MAX_CONSECUTIVE_TOPIC_NOVELTY_REJECTIONS)
        self.assertTrue(all(item.get("rejection_type") == "novelty" for item in trace))
        selected_ids = [item["selected_topic"]["id"] for item in trace]
        self.assertEqual(selected_ids, ["direction_0", "direction_2"])
        self.assertEqual(
            {item["topic_id"] for item in caught.exception.rejected_topic_history},
            set(selected_ids),
        )
        self.assertEqual(caught.exception.topic_budget["usage"]["model_calls"], 2)

    def test_non_novelty_failure_resets_the_novelty_rejection_streak(self):
        class InterleavedRejectionModel:
            calls = 0

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                payload = json.loads(prompt)
                type(self).calls += 1
                if type(self).calls == 2:
                    response = "not valid JSON"
                else:
                    value = package(payload["principal_objective"])
                    value["selected_id"] = (
                        "direction_0" if type(self).calls == 1 else "direction_2")
                    value["selection_rationale"] += f" proposal {type(self).calls}"
                    response = json.dumps(value)
                return ModelResult(
                    text=response, model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")

        initial = package("Choose a feasible research direction")
        history = {"entries": [{
            "topic_id": item["id"], "title": item["title"],
            "domain": item["domain"], "research_question": item["research_question"],
            "signature": topic_signature(item),
        } for item in initial["candidates"]]}
        InterleavedRejectionModel.calls = 0

        with patch("scisaurus.runtime.topic_discovery.ModelClient", InterleavedRejectionModel):
            with self.assertRaises(ValidationError) as caught:
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake",
                    "protocol": "ollama", "timeout_seconds": 1,
                    "max_output_tokens": 4096,
                }).run(
                    "Choose a feasible research direction", candidate_count=3,
                    max_attempts=3, bibliography=False,
                    runtime_context={"topic_history": history},
                    maturity_review_rounds=0,
                )

        self.assertEqual(InterleavedRejectionModel.calls, 3)
        trace = caught.exception.candidate_attempt_trace
        self.assertEqual([item.get("rejection_type") for item in trace],
                         ["novelty", None, "novelty"])
        self.assertEqual(caught.exception.topic_budget["usage"]["model_calls"], 3)

    def test_salvage_scope_violation_is_repaired_in_place_without_poisoning_topic_history(self):
        class ScopeRepairModel:
            calls = 0
            prompts = []
            always_invalid = False

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                payload = json.loads(prompt)
                type(self).calls += 1
                type(self).prompts.append(payload)
                candidate = deepcopy(payload["parent_candidate"])
                candidate.update(payload["required_shape"])
                candidate.update({
                    "title": "Boundary-resolved magnetotail reconnection evidence",
                    "research_question": (
                        "Within fast reconnection rates in planetary magnetotails, does the "
                        "documented observation boundary separate normalized-rate predictions?"),
                    "phenomenon": payload["parent_candidate"]["phenomenon"],
                    "scope": "Only the documented planetary magnetotail fast-rate interval.",
                    "frontier_seed_id": payload["target_frontier_seed_id"],
                    "prior_work_ids": ["W_PARENT"],
                    "search_queries": [
                        "planetary magnetotail fast reconnection rates",
                        "magnetotail rate observation boundary",
                        "collisionless reconnection rate measurements",
                    ],
                })
                if type(self).always_invalid or type(self).calls == 1:
                    candidate["mechanism"] = (
                        f"an unrequested kinetic instability mechanism {type(self).calls}")
                    candidate["theory_target"] = (
                        f"an unrelated instability threshold {type(self).calls}")
                else:
                    candidate["mechanism"] = payload["parent_candidate"]["mechanism"]
                    candidate["theory_target"] = payload["parent_candidate"]["theory_target"]
                return ModelResult(
                    text=json.dumps({"candidate": candidate}), model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")

        parent = package("Choose a feasible research direction")["candidates"][1]
        parent.update({
            "id": "direction_mhd_hall_scaling",
            "title": "Hall-Induced Reconnection Rate Scaling Boundary",
            "domain": "Magnetohydrodynamics",
            "research_question": (
                "In fast reconnection rates in planetary magnetotails, how does the Hall effect "
                "shape the normalized reconnection rate across the inertial-scale boundary?"),
            "phenomenon": "Fast reconnection rates in planetary magnetotails",
            "mechanism": "Hall effect from decoupled ion/electron motions",
            "theory_target": "the normalized rate boundary across ion inertial scales",
            "scope": "Planetary magnetotail reconnection across ion inertial scales.",
            "frontier_seed_id": "frontier_1",
            "prior_work_ids": ["W_PARENT"],
            "search_queries": [
                "collisionless reconnection rate inertial scales",
                "Hall effect planetary magnetotail reconnection",
                "fast reconnection rate measurements",
            ],
        })
        parent_work = {
            "work_id": "W_PARENT", "title": "Collisionless magnetic reconnection",
            "abstract": "Hall signatures in planetary magnetotail reconnection rates.",
            "frontier_seed_id": "frontier_1", "year": 2022,
        }
        refinement = {
            "mode": "refinement", "cycle": 576,
            "parent_topic_id": parent["id"], "parent_topic": parent,
            "reason": "Repair the supported evidence boundary without abandoning the phenomenon.",
            "parent_evidence": {
                "frontier_seed_plan": {
                    "schema_version": "topic-frontier-seeds-1",
                    "seeds": [{
                        "id": "frontier_1", "domain": parent["domain"],
                        "phenomenon": parent["phenomenon"],
                        "mechanism": parent["mechanism"],
                        "unit_of_analysis": parent["scope"],
                        "search_queries": parent["search_queries"],
                    }],
                },
                "recent_papers": [parent_work],
                "candidate_prior_work": [parent_work],
            },
            "work_orders": [{
                "id": "refine-parent", "kind": "topic_refinement",
                "objective": "Make the evidence boundary independently testable.",
                "success_condition": "Retain the phenomenon and isolate the observation boundary.",
            }],
            "salvage_plan": topic_salvage_plan([
                "mechanism-observable", "comparison-baseline",
            ]),
        }
        ScopeRepairModel.calls = 0
        ScopeRepairModel.prompts = []

        with patch("scisaurus.runtime.topic_discovery.ModelClient", ScopeRepairModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake",
                "protocol": "ollama", "timeout_seconds": 1,
                "max_output_tokens": 4096,
            }).run(
                "Refine the planetary magnetotail reconnection evidence boundary.",
                candidate_count=4, max_attempts=3, bibliography=False,
                sampling_seed=123, refinement_context=refinement,
                maturity_review_rounds=0,
            )

        self.assertEqual(ScopeRepairModel.calls, 2)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["topic"]["phenomenon"], parent["phenomenon"])
        self.assertEqual(result["candidate_attempt_trace"][0]["rejection_type"],
                         "refinement_contract")
        self.assertNotIn(parent["id"], {
            item.get("topic_id") for item in result["rejected_topic_history"]
        })
        repair_prompt = ScopeRepairModel.prompts[1]
        self.assertIn("outside its assigned repair scope", repair_prompt["validation_error"])
        self.assertIn("not a scientific rejection", repair_prompt["repair_instruction"])

        ScopeRepairModel.calls = 0
        ScopeRepairModel.prompts = []
        ScopeRepairModel.always_invalid = True
        with patch("scisaurus.runtime.topic_discovery.ModelClient", ScopeRepairModel):
            with self.assertRaises(ValidationError) as exhausted:
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake",
                    "protocol": "ollama", "timeout_seconds": 1,
                    "max_output_tokens": 4096,
                }).run(
                    "Refine the planetary magnetotail reconnection evidence boundary.",
                    candidate_count=4, max_attempts=8, bibliography=False,
                    sampling_seed=123, refinement_context=refinement,
                    maturity_review_rounds=0,
                )
        self.assertEqual(ScopeRepairModel.calls,
                         MAX_CONSECUTIVE_REFINEMENT_CONTRACT_REJECTIONS)
        self.assertTrue(all(
            item.get("rejection_type") == "refinement_contract"
            for item in exhausted.exception.candidate_attempt_trace))
        self.assertEqual(exhausted.exception.rejected_topic_history, [])

    def test_response_contract_only_prompt_preserves_the_scientific_direction(self):
        parent = package("Choose a feasible research direction")["candidates"][1]
        diagnostic = "topic candidate prior_work_ids cite records outside supplied evidence"
        payload = json.loads(topic_prompt(
            "Choose a feasible research direction", 3,
            refinement_context={
                "mode": "response_contract_repair",
                "parent_topic_id": parent["id"],
                "parent_topic": parent,
                "response_contract_repair": {"validation_error": diagnostic},
            },
        ))
        self.assertEqual(payload["refinement_shape"], {})
        self.assertTrue(any(
            "not a scientific topic selection or pivot" in item
            for item in payload["constraints"]
        ))
        self.assertTrue(any(
            diagnostic in item for item in payload["constraints"]
        ))
        self.assertFalse(any(
            "selected candidate must change at least one" in item
            for item in payload["constraints"]
        ))

    def test_response_contract_recovery_starts_on_configured_fallback_route(self):
        class RouteCaptureModel:
            models = []

            def __init__(self, **config):
                self.config = config
                type(self).models.append(config["model"])

            def complete(self, *, system, prompt, images=None):
                return ModelResult(
                    text='{"not": "a topic package"}',
                    model=self.config["model"],
                    usage={"model_calls": 1, "input_tokens": 1, "output_tokens": 1},
                    elapsed_seconds=0.01, finish_reason="stop")

        RouteCaptureModel.models = []
        config = {
            "base_url": "http://primary.invalid", "model": "primary", "protocol": "ollama",
            "role_models": {
                "topic_discovery": {
                    "base_url": "http://primary.invalid", "model": "primary", "protocol": "ollama",
                },
            },
            "role_model_fallbacks": {
                "topic_discovery": [
                    {
                        "base_url": "http://fallback.invalid", "model": "fallback", "protocol": "ollama",
                    },
                    {
                        "base_url": "http://last-fallback.invalid", "model": "last-fallback", "protocol": "ollama",
                    },
                ],
            },
        }
        with patch("scisaurus.runtime.topic_discovery.ModelClient", RouteCaptureModel), \
                patch("scisaurus.runtime.topic_discovery.model_call_budget_available",
                      side_effect=lambda route: route["model"] != "last-fallback"):
            with self.assertRaises(ValidationError):
                TopicDiscoveryRunner(config).run(
                    "Choose a feasible research direction", candidate_count=3,
                    bibliography=False, maturity_review_rounds=0, max_attempts=1,
                    refinement_context={
                        "response_contract_repair": {
                            "validation_error": "topic candidate package is invalid",
                        },
                    },
                )
        self.assertEqual(RouteCaptureModel.models, ["fallback"])

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

    def test_executable_selection_repair_reuses_valid_portfolio_member(self):
        value = package("Choose a feasible research direction")
        value["candidates"][0]["evidence_mode"] = "synthetic_simulation"
        value["candidates"][0]["feasibility_plan"] = foundry_feasibility_plan(
            evidence_inputs=[{
                "kind": "analytical_parameters", "status": "available",
                "source": "bounded analytical parameters",
            }])
        value["candidates"][1]["evidence_mode"] = "synthetic_simulation"
        value["candidates"][1]["feasibility_plan"] = foundry_feasibility_plan()
        value["selected_id"] = "direction_0"
        context = {
            "capability_foundry": {
                "enabled": True,
                "allowed_evidence_modes": ["analytical_derivation", "synthetic_simulation"],
            },
            "research_feasibility": {
                "execution_modes": ["foundry"],
                "allowed_input_kinds": ["analytical_parameters", "synthetic"],
                "allowed_data_access": ["closed_world"],
                "network_access": False, "undeclared_data": False,
                "max_external_requests": 0, "max_model_calls": 0,
                "max_experiment_seconds": 900,
                "available_executables": ["python3"],
                "available_packages": ["numpy"],
            },
            "executables": {"python3": True},
            "python_packages": {"numpy": True},
            "configured_stage_kinds": ["experiment"],
        }
        repair = _repair_executable_selection(value, context)
        self.assertEqual(repair["from_selected_id"], "direction_0")
        self.assertEqual(repair["to_selected_id"], "direction_1")
        self.assertEqual(value["selected_id"], "direction_1")
        self.assertEqual(repair["rejected_candidates"][0]["candidate_id"], "direction_0")

    def test_executable_selection_repair_does_not_invent_a_feasible_candidate(self):
        value = package("Choose a feasible research direction")
        for candidate in value["candidates"]:
            candidate["evidence_mode"] = "synthetic_simulation"
            candidate["feasibility_plan"] = foundry_feasibility_plan(
                evidence_inputs=[{
                    "kind": "analytical_parameters", "status": "available",
                    "source": "bounded analytical parameters",
                }])
        context = {
            "capability_foundry": {"enabled": True},
            "research_feasibility": {
                "execution_modes": ["foundry"],
                "allowed_input_kinds": ["analytical_parameters", "synthetic"],
                "allowed_data_access": ["closed_world"],
                "network_access": False, "undeclared_data": False,
                "max_external_requests": 0, "max_model_calls": 0,
                "max_experiment_seconds": 900,
                "available_executables": ["python3"],
                "available_packages": ["numpy"],
            },
            "executables": {"python3": True},
            "python_packages": {"numpy": True},
            "configured_stage_kinds": ["experiment"],
        }
        self.assertIsNone(_repair_executable_selection(value, context))
        self.assertEqual(value["selected_id"], "direction_1")

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
        self.assertEqual(result["question"], result["topic"]["research_question"])
        self.assertNotEqual(result["question"], result["topic"]["why_promising"])
        self.assertNotIn("proposed_gap", result)

    def test_runner_ignores_malformed_unselected_plans_and_repairs_selection(self):
        class InconsistentFeasibilityPortfolioModel(FakeModel):
            def complete(self, *, system, prompt, images=None):
                result = super().complete(system=system, prompt=prompt, images=images)
                payload = json.loads(prompt)
                if payload.get("assignment") != "free_topic_discovery":
                    return result
                value = json.loads(result.text)
                value["candidates"][0]["feasibility_plan"] = foundry_feasibility_plan()
                value["candidates"][1]["feasibility_plan"] = foundry_feasibility_plan(
                    experiment_input="project_artifact",
                    data_access="project_local",
                    evidence_inputs=[{
                        "kind": "public_dataset", "status": "available",
                        "source": "a dataset that is not injected into the foundry",
                    }],
                )
                value["candidates"][2]["feasibility_plan"] = foundry_feasibility_plan(
                    experiment_input="project_artifact",
                    data_access="project_local",
                    evidence_inputs=[{
                        "kind": "public_dataset", "status": "available",
                        "source": "another dataset that is not injected into the foundry",
                    }],
                )
                return ModelResult(
                    text=json.dumps(value), model="fake", usage=result.usage,
                    elapsed_seconds=result.elapsed_seconds, finish_reason=result.finish_reason)

        runtime_context = {
            "capability_foundry": {
                "enabled": True,
                "allowed_evidence_modes": ["synthetic_simulation"],
            },
            "research_feasibility": {
                "execution_modes": ["foundry"],
                "allowed_input_kinds": ["synthetic", "analytical_parameters"],
                "allowed_data_access": ["closed_world"],
                "network_access": False,
                "undeclared_data": False,
                "max_external_requests": 0,
                "max_model_calls": 0,
                "max_experiment_seconds": 900,
                "available_executables": ["python3"],
                "available_packages": ["numpy"],
            },
            "executables": {"python3": True},
            "python_packages": {"numpy": True},
            "configured_stage_kinds": ["experiment"],
        }
        with patch("scisaurus.runtime.topic_discovery.ModelClient",
                   InconsistentFeasibilityPortfolioModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run(
                "Choose a feasible research direction", candidate_count=3,
                bibliography=False, max_attempts=1, runtime_context=runtime_context,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected_id"], "direction_0")
        self.assertEqual(
            result["candidate_attempt_trace"][0]["selection_repair"]["from_selected_id"],
            "direction_1",
        )

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

    def test_repeated_semantically_rejected_candidate_is_a_contract_failure_not_a_new_pivot(self):
        error = ValidationError(
            "topic discovery repeated the byte-identical rejected response after a repair request")
        trace = [
            {"status": "rejected", "rejection_type": "novelty"},
            {"status": "repeated_response"},
        ]
        rejected_history = [{"topic_id": "direction_3", "rejection_type": "novelty"}]

        self.assertEqual(
            _topic_retry_reason(error, trace, rejected_history),
            "intake_contract_failure",
        )

    def test_feasibility_contract_failure_before_attempt_record_stops_local_intake(self):
        runner = TopicDiscoveryRunner({
            "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
            "timeout_seconds": 1, "max_output_tokens": 4096,
        })
        with patch("scisaurus.runtime.topic_discovery.ModelClient", FakeModel), \
                patch.object(
                    runner, "_repair_missing_topic_fields",
                    side_effect=ValidationError(
                        "feasibility_plan.project_artifact must declare a project_artifact input"),
                ):
            with self.assertRaises(ValidationError) as raised:
                runner.run(
                    "Choose a feasible research direction", candidate_count=3,
                    bibliography=False, max_attempts=6,
                    runtime_context={"capability_foundry": {"enabled": True}},
                )
        error = raised.exception
        self.assertEqual(len(error.candidate_attempt_trace), 1)
        self.assertEqual(error.candidate_attempt_trace[0]["status"], "rejected")
        self.assertEqual(error.topic_retry_reason, "intake_contract_failure")
        self.assertEqual(error.topic_budget["usage"]["model_calls"], 1)

    def test_novelty_failure_before_attempt_record_stops_local_intake(self):
        runner = TopicDiscoveryRunner({
            "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
            "timeout_seconds": 1, "max_output_tokens": 4096,
        })
        with patch("scisaurus.runtime.topic_discovery.ModelClient", FakeModel), \
                patch.object(
                    runner, "_repair_missing_topic_fields",
                    side_effect=ValidationError(
                        "selected topic is too similar to a previously attempted direction"),
                ):
            with self.assertRaises(ValidationError) as raised:
                runner.run(
                    "Choose a feasible research direction", candidate_count=3,
                    bibliography=False, max_attempts=6,
                )
        error = raised.exception
        self.assertEqual(len(error.candidate_attempt_trace), 1)
        self.assertEqual(error.topic_retry_reason, "scientific_candidate_rejected")
        self.assertEqual(error.topic_budget["usage"]["model_calls"], 1)

    def test_outer_runner_boundary_preserves_unexpected_validation_usage(self):
        runner = TopicDiscoveryRunner({
            "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
            "timeout_seconds": 1, "max_output_tokens": 4096,
        })
        runner._active_topic_budget = TopicBudget(
            usage={"model_calls": 2, "input_tokens": 120, "output_tokens": 40,
                   "openalex_requests": 3})
        runner._active_topic_trace = [{"status": "rejected"}]
        error = ValidationError(
            "feasibility_plan.project_artifact must declare a project_artifact input")
        with patch.object(runner, "_run_impl", side_effect=error):
            with self.assertRaises(ValidationError) as raised:
                runner.run("Choose a feasible research direction")
        caught = raised.exception
        self.assertTrue(caught.topic_intake_recoverable)
        self.assertEqual(caught.topic_retry_reason, "intake_contract_failure")
        self.assertEqual(caught.topic_budget["usage"]["model_calls"], 2)
        self.assertEqual(caught.usage["openalex_requests"], 3)
        self.assertEqual(caught.candidate_attempt_trace, [{"status": "rejected"}])

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
        self.assertEqual(result["question"], result["topic"]["research_question"])
        self.assertNotIn("proposed_gap", result)
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

    def test_runner_forwards_incomplete_maturity_review_to_survey(self):
        MaturityLengthModel.review_calls = 0
        with patch("scisaurus.runtime.topic_discovery.ModelClient", MaturityLengthModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run("Choose a feasible research direction", candidate_count=3,
                   bibliography=False, maturity_review_rounds=1, max_attempts=6)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["admission_state"], "provisional_for_survey")
        self.assertEqual(MaturityLengthModel.review_calls, 2)
        self.assertIn("topic maturity review did not finish normally: length",
                      result["maturity_review_error"])
        self.assertTrue(result["maturity_open_requirements"])
        self.assertEqual(result["candidate_attempt_trace"][-1]["status"],
                         "provisional_for_survey")

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

    def test_source_challenge_never_falls_back_to_rewriting_the_whole_portfolio(self):
        SourceChallengeRefinementModel.calls = []
        SourceChallengeRefinementModel.payloads = []
        SourceChallengeRefinementModel.challenge_count = 0
        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex), \
                patch("scisaurus.runtime.topic_discovery.ModelClient",
                      SourceChallengeRefinementModel), \
                patch("scisaurus.runtime.topic_discovery._refinement_target_seed",
                      return_value=None):
            with self.assertRaisesRegex(
                    ValidationError,
                    "bounded single-candidate topic refinement cannot proceed"):
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake",
                    "protocol": "ollama", "timeout_seconds": 1,
                    "max_output_tokens": 4096,
                }).run(
                    "Choose a feasible research direction", candidate_count=3,
                    max_attempts=2, maturity_review_rounds=0,
                )
        self.assertEqual(SourceChallengeRefinementModel.challenge_count, 1)
        self.assertNotIn(
            "repair_selected_topic_candidate", SourceChallengeRefinementModel.calls)

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
        self.assertEqual(RefinementValidationRepairModel.refinement_calls, 1)
        candidate_repair = next(
            item for item in RefinementValidationRepairModel.payloads
            if item.get("assignment") == "repair_selected_topic_candidate")
        self.assertEqual(
            result["selected_id"], candidate_repair["candidate_id_to_copy_exactly"])
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

    def test_shared_topic_cache_reuses_results_across_stage_cache_paths(self):
        with tempfile.TemporaryDirectory(prefix="scisaurus-topic-cache-") as directory:
            root = Path(directory)
            shared = root / "shared" / "topic-openalex.json"
            local_a = root / "mission-a" / "topic.json"
            local_b = root / "mission-b" / "topic.json"
            FakeOpenAlex.queries = []
            with patch.dict(os.environ, {
                "SCISAURUS_OPENALEX_SHARED_TOPIC_CACHE_PATH": str(shared),
            }), patch("scisaurus.runtime.topic_discovery.OpenAlexClient", FakeOpenAlex):
                first = TopicDiscoveryRunner._recent_paper_sample(
                    "cache-backed science", sampling_seed=17,
                    frontier_seed_plan=frontier_plan(),
                    bibliography={"cache_path": str(local_a.resolve())},
                )
                first_call_count = len(FakeOpenAlex.queries)
                second = TopicDiscoveryRunner._recent_paper_sample(
                    "cache-backed science", sampling_seed=17,
                    frontier_seed_plan=frontier_plan(),
                    bibliography={"cache_path": str(local_b.resolve())},
                )
            self.assertEqual(len(FakeOpenAlex.queries), first_call_count)
            self.assertEqual(first[0], second[0])
            self.assertEqual(first[1], second[1])
            self.assertEqual(len(first[2]), len(second[2]))
            self.assertTrue(all(item["cache_hit"] for item in second[2]))
            self.assertTrue(shared.is_file())

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

    def test_missing_feasibility_plan_reuses_input_normalization_before_validation(self):
        class MissingPlanModel:
            calls = 0

            def __init__(self, **config):
                self.config = config

            def complete(self, *, system, prompt, images=None):
                type(self).calls += 1
                payload = json.loads(prompt)
                patches = [{
                    "id": item["id"],
                    "fields": {
                        "feasibility_plan": foundry_feasibility_plan(
                            experiment_input="project_artifact",
                            data_access="project_local",
                            evidence_inputs=[{
                                "kind": "analytical_parameters",
                                "status": "available",
                                "source": "bounded analytic parameters supplied by the study",
                            }],
                        ),
                    },
                } for item in payload["candidate_context"]]
                return ModelResult(
                    text=json.dumps({"candidate_patches": patches}), model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")

        value = package("Choose a feasible research direction")
        for candidate in value["candidates"]:
            candidate["feasibility_plan"] = foundry_feasibility_plan()
        value["candidates"][1].pop("feasibility_plan")
        runner = TopicDiscoveryRunner({
            "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
            "timeout_seconds": 1, "max_output_tokens": 4096,
        })
        with patch("scisaurus.runtime.topic_discovery.ModelClient", MissingPlanModel):
            repairs = runner._repair_missing_topic_fields(
                value, deadline=None, budget=TopicBudget(None, {}),
                require_feasibility_plan=True,
                runtime_context={"capability_foundry": {"enabled": True}},
            )
        plan = value["candidates"][1]["feasibility_plan"]
        self.assertEqual(MissingPlanModel.calls, 1)
        self.assertEqual(plan["experiment_input"], "self_contained")
        self.assertEqual(plan["data_access"], "closed_world")
        self.assertEqual(validate_feasibility_plan(plan), plan)
        self.assertTrue(any(
            item.get("source") == "targeted_model_field_repair_contract_normalization"
            for item in repairs
        ))

    def test_empty_resource_plan_is_derived_from_declared_feasibility_without_model_call(self):
        value = package("Choose a feasible research direction")
        candidate = value["candidates"][1]
        candidate["resource_plan"] = "  "
        candidate["feasibility_plan"] = foundry_feasibility_plan(
            evidence_inputs=[{
                "kind": "analytical_parameters",
                "status": "available",
                "source": "bounded analytic parameters supplied by the study",
            }],
            required_executables=["python3"],
            required_packages=["numpy"],
            estimated_compute_seconds=300,
            estimated_api_requests=0,
            network_access=False,
        )
        runner = TopicDiscoveryRunner({
            "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
            "timeout_seconds": 1, "max_output_tokens": 4096,
        })
        with patch.object(runner, "_client", side_effect=AssertionError(
                "a declared resource plan should not need a model repair")):
            repairs = runner._repair_missing_topic_fields(
                value, deadline=None, budget=TopicBudget(None, {}),
                require_feasibility_plan=True,
            )

        self.assertIn("bounded analytic parameters supplied by the study", candidate["resource_plan"])
        self.assertIn("python3, numpy", candidate["resource_plan"])
        self.assertIn("300 seconds", candidate["resource_plan"])
        self.assertIn("Network access is not required", candidate["resource_plan"])
        self.assertTrue(any(
            item.get("field") == "resource_plan"
            and item.get("source") == "feasibility_plan_projection"
            for item in repairs
        ))

    def test_runner_does_not_regenerate_portfolio_for_empty_resource_plan(self):
        class EmptyResourcePlanModel(FakeModel):
            assignments = []

            def complete(self, *, system, prompt, images=None):
                payload = json.loads(prompt)
                type(self).assignments.append(payload.get("assignment"))
                result = super().complete(system=system, prompt=prompt, images=images)
                if payload.get("assignment") != "free_topic_discovery":
                    return result
                value = json.loads(result.text)
                for candidate in value["candidates"]:
                    candidate["resource_plan"] = ""
                    candidate["evidence_mode"] = "synthetic_simulation"
                    candidate["feasibility_plan"] = foundry_feasibility_plan()
                return ModelResult(
                    text=json.dumps(value), model="fake", usage=result.usage,
                    elapsed_seconds=result.elapsed_seconds,
                    finish_reason=result.finish_reason,
                )

        with patch("scisaurus.runtime.topic_discovery.ModelClient", EmptyResourcePlanModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                "timeout_seconds": 1, "max_output_tokens": 4096,
            }).run("Choose a feasible research direction", candidate_count=3,
                   bibliography=False, max_attempts=1)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(EmptyResourcePlanModel.assignments, ["free_topic_discovery"])
        self.assertTrue(all(
            "seeded synthetic input generated by the pinned experiment" in item["resource_plan"]
            for item in result["candidates"]
        ))

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

    def test_topic_client_respects_route_timeout_within_stage_deadline(self):
        runner = TopicDiscoveryRunner({
            "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
            "timeout_seconds": 1800, "max_output_tokens": 4096,
        })
        client = runner._client("topic_discovery", deadline=time.monotonic() + 1200)
        self.assertGreater(client.timeout_seconds, 300)
        self.assertLessEqual(client.timeout_seconds, 1200)

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

    def test_feasibility_plan_is_strictly_typed(self):
        plan = foundry_feasibility_plan()
        self.assertEqual(validate_feasibility_plan(plan), plan)
        invalid = dict(plan)
        invalid["network_access"] = "false"
        with self.assertRaisesRegex(ValidationError, "network_access"):
            validate_feasibility_plan(invalid)

    def test_model_status_aliases_are_normalized_without_relaxing_gate(self):
        value = package("Choose a feasible research direction")
        value["candidates"][0]["feasibility_plan"] = foundry_feasibility_plan(
            evidence_inputs=[{
                "kind": "synthetic", "status": "ready", "source": "generated inputs",
            }])
        repairs = _repair_feasibility_input_statuses(value)
        self.assertEqual(value["candidates"][0]["feasibility_plan"]["evidence_inputs"][0]["status"], "available")
        self.assertEqual(repairs[0]["source"], "lossless_status_alias")
        self.assertEqual(validate_feasibility_plan(value["candidates"][0]["feasibility_plan"])["network_access"], False)

    def test_model_kind_aliases_are_normalized_without_relaxing_gate(self):
        value = package("Choose a feasible research direction")
        value["candidates"][0]["feasibility_plan"] = foundry_feasibility_plan(
            evidence_inputs=[{
                "kind": "synthetic simulation", "status": "available", "source": "generated inputs",
            }])
        repairs = _repair_feasibility_input_kinds(value)
        self.assertEqual(value["candidates"][0]["feasibility_plan"]["evidence_inputs"][0]["kind"], "synthetic")
        self.assertEqual(repairs[0]["source"], "lossless_kind_alias")
        self.assertEqual(validate_feasibility_plan(value["candidates"][0]["feasibility_plan"])["data_access"], "closed_world")

    def test_duplicate_feasibility_kinds_are_merged_without_losing_sources(self):
        value = package("Choose a feasible research direction")
        plan = foundry_feasibility_plan(evidence_inputs=[
            {"kind": "synthetic", "status": "available", "source": "generated inputs"},
            {"kind": "synthetic", "status": "available", "source": "seeded perturbations"},
        ])
        value["candidates"][0]["feasibility_plan"] = plan
        repairs = _repair_feasibility_input_duplicates(value)
        inputs = value["candidates"][0]["feasibility_plan"]["evidence_inputs"]
        self.assertEqual(len(inputs), 1)
        self.assertIn("generated inputs", inputs[0]["source"])
        self.assertIn("seeded perturbations", inputs[0]["source"])
        self.assertEqual(repairs[0]["source"], "lossless_duplicate_kind_merge")
        validate_feasibility_plan(value["candidates"][0]["feasibility_plan"])

    def test_current_foundry_gate_rejects_hidden_external_inputs_and_network(self):
        value = package("Choose a feasible research direction")
        selected = value["candidates"][1]
        selected["evidence_mode"] = "synthetic_simulation"
        selected["feasibility_plan"] = foundry_feasibility_plan(
            evidence_inputs=[{
                "kind": "survey_full_text", "status": "available",
                "source": "digitized published curve",
            }],
            data_access="external_provider", network_access=True,
            estimated_api_requests=1,
        )
        context = {
            "capability_foundry": {
                "enabled": True,
                "allowed_evidence_modes": ["analytical_derivation", "synthetic_simulation"],
            },
            "research_feasibility": {
                "execution_modes": ["foundry"],
                "allowed_input_kinds": ["analytical_parameters", "synthetic"],
                "allowed_data_access": ["closed_world"],
                "network_access": False, "undeclared_data": False,
                "max_external_requests": 0, "max_model_calls": 0,
                "max_experiment_seconds": 900,
                "available_executables": ["python3"],
                "available_packages": ["numpy"],
            },
            "executables": {"python3": True},
            "python_packages": {"numpy": True},
            "configured_stage_kinds": ["experiment"],
        }
        from scisaurus.runtime.topic_discovery import validate_topic_feasibility
        with self.assertRaisesRegex(ValidationError, "evidence_inputs"):
            validate_topic_feasibility(value, context)

    def test_current_foundry_gate_accepts_only_self_contained_bounded_plan(self):
        value = package("Choose a feasible research direction")
        value["candidates"][1]["evidence_mode"] = "synthetic_simulation"
        value["candidates"][1]["feasibility_plan"] = foundry_feasibility_plan()
        context = {
            "capability_foundry": {
                "enabled": True,
                "allowed_evidence_modes": ["analytical_derivation", "synthetic_simulation"],
            },
            "research_feasibility": {
                "execution_modes": ["foundry"],
                "allowed_input_kinds": ["analytical_parameters", "synthetic"],
                "allowed_data_access": ["closed_world"],
                "network_access": False, "undeclared_data": False,
                "max_external_requests": 0, "max_model_calls": 0,
                "max_experiment_seconds": 900,
                "available_executables": ["python3"],
                "available_packages": ["numpy"],
            },
            "executables": {"python3": True},
            "python_packages": {"numpy": True},
            "configured_stage_kinds": ["experiment"],
        }
        from scisaurus.runtime.topic_discovery import validate_topic_feasibility
        result = validate_topic_feasibility(value, context)
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["checks"]["execution_boundary"], "passed")
        value["candidates"][1]["feasibility_plan"]["estimated_model_calls"] = 1
        with self.assertRaisesRegex(ValidationError, "model_budget"):
            validate_topic_feasibility(value, context)

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

    def test_noncontract_topic_fields_are_discarded_without_relaxing_candidate_validation(self):
        value = package("Choose a feasible research direction")
        value.update({
            "status": "completed",
            "topic": deepcopy(value["candidates"][1]),
            "question": value["candidates"][1]["research_question"],
            "budget": {"model_calls": 3},
            "commentary": "unrequested envelope text",
            "diagnostics": {"source": "model-wrapper"},
        })
        repairs = _strip_noncontract_topic_fields(value)
        self.assertEqual(set(value), {
            "schema_version", "objective", "candidates", "selected_id",
            "selection_rationale",
        })
        self.assertEqual(
            {item["field"] for item in repairs},
            {"status", "topic", "question", "budget", "commentary", "diagnostics"},
        )
        self.assertIn(
            {"field": "commentary", "source": "discarded_noncontract_top_level_field"},
            repairs,
        )
        validate_topic_package(value, objective=value["objective"], candidate_count=3)

    def test_case_only_candidate_id_mismatch_is_normalized_losslessly(self):
        value = package("Choose a feasible research direction")
        value["candidates"][1]["id"] = "Direction_Boron_pH_Benchmark"
        value["selected_id"] = "Direction_Boron_pH_Benchmark"

        repairs = _repair_case_only_topic_candidate_ids(value)

        self.assertEqual(value["candidates"][1]["id"], "direction_boron_ph_benchmark")
        self.assertEqual(value["selected_id"], "direction_boron_ph_benchmark")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["source"], "case_only_identifier_normalization")

    def test_case_only_candidate_id_normalization_does_not_merge_candidates(self):
        value = package("Choose a feasible research direction")
        value["candidates"][0]["id"] = "Direction_1"

        repairs = _repair_case_only_topic_candidate_ids(value)

        self.assertEqual(repairs, [])
        self.assertEqual(value["candidates"][0]["id"], "Direction_1")

    def test_non_ascii_candidate_id_is_not_casefolded_into_a_different_id(self):
        value = package("Choose a feasible research direction")
        value["candidates"][0]["id"] = "direction_K"

        repairs = _repair_case_only_topic_candidate_ids(value)

        self.assertEqual(repairs, [])
        self.assertEqual(value["candidates"][0]["id"], "direction_K")

    def test_feasibility_input_label_is_repaired_from_declared_self_contained_inputs(self):
        value = package("Choose a feasible research direction")
        selected = value["candidates"][1]
        selected["evidence_mode"] = "analytical_derivation"
        selected["feasibility_plan"] = foundry_feasibility_plan(
            experiment_input="project_artifact",
            data_access="project_local",
            estimated_compute_seconds=120.0,
            evidence_inputs=[{
                "kind": "analytical_parameters", "status": "available",
                "source": "bounded analytic parameters supplied by the study",
            }],
        )
        context = {
            "capability_foundry": {
                "enabled": True,
                "allowed_evidence_modes": ["analytical_derivation", "synthetic_simulation"],
            },
            "research_feasibility": {
                "execution_modes": ["foundry"],
                "allowed_input_kinds": ["analytical_parameters", "synthetic"],
                "allowed_data_access": ["closed_world"],
                "network_access": False, "undeclared_data": False,
                "max_external_requests": 0, "max_model_calls": 0,
                "max_experiment_seconds": 900,
                "available_executables": ["python3"],
                "available_packages": ["numpy"],
            },
            "executables": {"python3": True},
            "python_packages": {"numpy": True},
            "configured_stage_kinds": ["experiment"],
        }
        repairs = _repair_feasibility_input_contract(value, context)
        self.assertEqual(selected["feasibility_plan"]["experiment_input"], "self_contained")
        self.assertEqual(selected["feasibility_plan"]["data_access"], "closed_world")
        self.assertEqual(
            {item["field"] for item in repairs},
            {"experiment_input", "data_access", "estimated_compute_seconds"},
        )
        from scisaurus.runtime.topic_discovery import validate_topic_feasibility
        self.assertEqual(validate_topic_feasibility(value, context)["status"], "feasible")

    def test_topic_package_normalizes_input_alias_before_strict_feasibility_gate(self):
        value = package("Choose a feasible research direction")
        selected = value["candidates"][1]
        selected["evidence_mode"] = "analytical_derivation"
        selected["feasibility_plan"] = foundry_feasibility_plan(
            experiment_input="project_artifact",
            data_access="project_local",
            evidence_inputs=[{
                "kind": "analytical input", "status": "ready",
                "source": "bounded analytic parameters supplied by the study",
            }],
        )

        validate_topic_package(
            value, objective=value["objective"], candidate_count=3,
        )
        self.assertEqual(
            selected["feasibility_plan"]["experiment_input"], "self_contained")
        self.assertEqual(
            selected["feasibility_plan"]["evidence_inputs"][0]["kind"],
            "analytical_parameters",
        )
        self.assertEqual(
            selected["feasibility_plan"]["evidence_inputs"][0]["status"],
            "available",
        )
        self.assertEqual(selected["feasibility_plan"]["data_access"], "closed_world")

    def test_incompatible_feasibility_plan_is_a_repairable_contract_failure(self):
        error = ValidationError(
            "feasibility_plan.project_artifact must declare a project_artifact input")
        self.assertEqual(
            _topic_validation_rejection_type(error), "feasibility_contract")
        self.assertEqual(
            _topic_retry_reason(error, [{"status": "rejected"}], []),
            "intake_contract_failure",
        )

    def test_feasibility_input_repair_does_not_invent_an_unsupported_input(self):
        value = package("Choose a feasible research direction")
        selected = value["candidates"][1]
        selected["feasibility_plan"] = foundry_feasibility_plan(
            experiment_input="survey_artifact",
            evidence_inputs=[{
                "kind": "public_dataset", "status": "available",
                "source": "named public dataset to be acquired by the survey stage",
            }],
        )
        original = json.loads(json.dumps(selected["feasibility_plan"]))
        context = {
            "capability_foundry": {"enabled": True},
            "research_feasibility": {
                "allowed_input_kinds": ["analytical_parameters", "synthetic"],
            },
        }
        self.assertEqual(_repair_feasibility_input_contract(value, context), [])
        self.assertEqual(selected["feasibility_plan"], original)

    def test_topic_admission_rejects_empirical_data_hidden_in_self_contained_plan(self):
        value = package("Choose an executable research direction")
        selected = value["candidates"][1]
        selected["feasibility_plan"] = foundry_feasibility_plan()
        with self.assertRaisesRegex(ValidationError, "controller-verified source rows"):
            validate_topic_package(value, objective=value["objective"])

        selected["evidence_mode"] = "analytical_derivation"
        selected["resource_plan"] = "Digitize the published trend from Figure 3 and fit its points."
        with self.assertRaisesRegex(ValidationError, "digitized or measured source data"):
            validate_topic_package(value, objective=value["objective"])

        selected["resource_plan"] = "Use the analytic model equations and fit the declared grid."
        selected["data_regime"] = (
            "Analytical evaluation over confinement ratios 2-100; reference trend from "
            "the published onset shear-rate-versus-gap data is digitized from W2162644906.")
        with self.assertRaisesRegex(ValidationError, "controller-verified source-data input"):
            validate_topic_package(value, objective=value["objective"])

    def test_literature_equations_do_not_require_empirical_source_rows(self):
        from scisaurus.runtime.topic_discovery import (
            scope_maturity_requirements_to_topic_evidence,
            topic_requires_source_data,
        )

        topic = {
            "evidence_mode": "analytical_derivation",
            "research_question": "Which term changes the analytic onset scaling?",
            "scope": "A self-contained parameter sweep of the derived equations.",
            "data_regime": "Synthetic/analytic grid; no fitted observations.",
            "resource_plan": "Use equations and parameter ranges reported in published studies.",
            "feasibility_plan": foundry_feasibility_plan(evidence_inputs=[
                {"kind": "analytical_parameters", "status": "available",
                 "source": "Published reentrant-jamming flow curves and onset-gap description."},
                {"kind": "synthetic", "status": "available",
                 "source": "Derived onset grid; no measured observations."},
            ]),
        }
        self.assertFalse(topic_requires_source_data(topic))

        requirements = [
            "Supply digitized onset-versus-gap table with uncertainties",
            "Locate and verify the reference dataset in the literature survey",
            "Provide W4379162019 extract supporting rough-contact threshold",
            "State functional forms and matched parameter counts",
            "Run synthetic-recovery power check on declared grid",
        ]
        scoped = scope_maturity_requirements_to_topic_evidence(
            topic, requirements, evidence_boundary_changed=True)
        self.assertEqual(scoped["out_of_scope"], [requirements[0]])
        self.assertEqual(scoped["active"], requirements[1:])

        unchanged = scope_maturity_requirements_to_topic_evidence(
            topic, requirements, evidence_boundary_changed=False)
        self.assertEqual(unchanged["active"], requirements)
        self.assertEqual(unchanged["out_of_scope"], [])

        empirical = dict(topic)
        empirical["evidence_mode"] = "published_observations"
        still_required = scope_maturity_requirements_to_topic_evidence(
            empirical, requirements, evidence_boundary_changed=True)
        self.assertEqual(still_required["active"], requirements)
        self.assertEqual(still_required["out_of_scope"], [])

        empirical_input = dict(topic)
        empirical_input["feasibility_plan"] = foundry_feasibility_plan(evidence_inputs=[
            {"kind": "public_dataset", "status": "available",
             "source": "Published flow-curve observations."},
        ])
        self.assertTrue(topic_requires_source_data(empirical_input))

    def test_feasibility_numeric_repair_runs_before_optional_input_inventory(self):
        value = package("Choose a feasible research direction")
        selected = value["candidates"][1]
        selected["feasibility_plan"] = foundry_feasibility_plan(
            estimated_compute_seconds=600.0,
        )
        selected["feasibility_plan"].pop("evidence_inputs")
        repairs = _repair_feasibility_input_contract(
            value, {"capability_foundry": {"enabled": True}}
        )
        self.assertEqual(selected["feasibility_plan"]["estimated_compute_seconds"], 600)
        self.assertEqual(repairs[0]["field"], "estimated_compute_seconds")

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

    def test_post_foundry_reselection_cannot_restore_a_rejected_topic(self):
        value = package("Choose a feasible research direction")
        value["candidates"][0].update({
            "title": "Published Albedo Change in Polar Terrain",
            "domain": "planetary science",
            "research_question": "How does seasonal frost loss alter measured albedo across polar terrain?",
            "evidence_mode": "published_observations",
        })
        value["candidates"][1]["evidence_mode"] = "synthetic_simulation"
        value["candidates"][2].update({
            "title": "Dispersal-driven recovery after disturbance",
            "domain": "landscape ecology",
            "research_question": "How does habitat dispersal alter recovery time after a disturbance pulse?",
            "evidence_mode": "synthetic_simulation",
        })
        value["candidates"][2].pop("capability_requirements")
        value["selected_id"] = "direction_0"
        prior_candidate = value["candidates"][1]
        history = {"entries": [{
            "topic_id": prior_candidate["id"],
            "title": prior_candidate["title"],
            "domain": prior_candidate["domain"],
            "research_question": prior_candidate["research_question"],
            "signature": topic_signature(prior_candidate),
        }]}
        context = {
            "capability_foundry": {
                "enabled": True,
                "allowed_evidence_modes": ["synthetic_simulation"],
            },
            "executables": {"python3": True},
            "configured_stage_kinds": ["experiment"],
        }

        self.assertIsNone(_repair_topic_novelty_selection(
            value, history, runtime_context=context))
        foundry_repair = _repair_foundry_selection(value, context)
        self.assertEqual(foundry_repair["to_selected_id"], "direction_1")

        novelty_repair = _repair_topic_novelty_selection(
            value, history, runtime_context=context)
        self.assertEqual(novelty_repair["from_selected_id"], "direction_1")
        self.assertEqual(novelty_repair["to_selected_id"], "direction_2")
        self.assertEqual(
            value["candidates"][2]["capability_requirements"],
            {"executables": ["python3"], "python_packages": [], "stage_kinds": ["experiment"]},
        )
        trace = {"selected_id": "direction_0", "selected_topic": {"id": "direction_0"}}
        _record_attempt_selection(trace, value)
        self.assertEqual(trace["selected_id"], "direction_2")
        self.assertEqual(trace["selected_topic"]["id"], "direction_2")
        self.assertTrue(validate_topic_package(value, topic_history=history))

    def test_runner_revalidates_novelty_after_foundry_reselection(self):
        class FoundryReselectionModel(FakeModel):
            def complete(self, *, system, prompt, images=None):
                payload = json.loads(prompt)
                if payload.get("assignment") != "free_topic_discovery":
                    return super().complete(system=system, prompt=prompt, images=images)
                value = package(payload["principal_objective"])
                for candidate in value["candidates"]:
                    candidate["feasibility_plan"] = foundry_feasibility_plan()
                    candidate.pop("capability_requirements")
                value["selected_id"] = "direction_0"
                value["candidates"][0].update({
                    "title": "Published Albedo Change in Polar Terrain",
                    "domain": "planetary science",
                    "research_question": (
                        "How does seasonal frost loss alter measured albedo across polar terrain?"),
                    "evidence_mode": "published_observations",
                })
                value["candidates"][1]["evidence_mode"] = "synthetic_simulation"
                value["candidates"][2].update({
                    "title": "Dispersal-driven recovery after disturbance",
                    "domain": "landscape ecology",
                    "research_question": (
                        "How does habitat dispersal alter recovery time after a disturbance pulse?"),
                    "evidence_mode": "synthetic_simulation",
                })
                return ModelResult(
                    text=json.dumps(value), model="fake",
                    usage={"model_calls": 1, "input_tokens": 10, "output_tokens": 20},
                    elapsed_seconds=0.01, finish_reason="stop")

        repeated = package("Choose a feasible research direction")["candidates"][1]
        history = {"entries": [{
            "topic_id": repeated["id"], "title": repeated["title"],
            "domain": repeated["domain"],
            "research_question": repeated["research_question"],
            "signature": topic_signature(repeated),
        }]}
        runtime_context = {
            "capability_foundry": {
                "enabled": True,
                "allowed_evidence_modes": ["synthetic_simulation"],
            },
            "research_feasibility": {
                "execution_modes": ["foundry"],
                "allowed_input_kinds": ["synthetic", "analytical_parameters"],
                "allowed_data_access": ["closed_world"],
                "network_access": False,
                "undeclared_data": False,
                "max_external_requests": 0,
                "max_model_calls": 0,
                "max_experiment_seconds": 900,
                "available_executables": ["python3"],
                "available_packages": ["numpy"],
            },
            "executables": {"python3": True},
            "python_packages": {"numpy": True},
            "configured_stage_kinds": ["experiment"],
            "topic_history": history,
        }

        with patch("scisaurus.runtime.topic_discovery.ModelClient", FoundryReselectionModel):
            result = TopicDiscoveryRunner({
                "base_url": "http://example.invalid", "model": "fake",
                "protocol": "ollama", "timeout_seconds": 1,
                "max_output_tokens": 4096,
            }).run(
                "Choose a feasible research direction", candidate_count=3,
                bibliography=False, max_attempts=1, runtime_context=runtime_context)

        attempt = result["candidate_attempt_trace"][0]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selected_id"], "direction_2")
        self.assertEqual(attempt["post_repair_novelty_selection_repair"]["from_selected_id"],
                         "direction_1")
        self.assertEqual(attempt["post_repair_novelty_selection_repair"]["to_selected_id"],
                         "direction_2")
        self.assertEqual(attempt["selected_topic"]["id"], "direction_2")
        self.assertEqual(attempt["status"], "admitted")

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

    def test_provider_cooldown_preflight_spends_no_topic_model_call(self):
        class BlockedOpenAlex:
            def __init__(self, **config):
                self.config = config

            def preflight(self, **kwargs):
                return {
                    "retry_after_seconds": 123,
                    "rate_limit": {"kind": "daily_budget", "remaining": 2},
                }

        class ExplodingModel:
            calls = 0

            def __init__(self, **config):
                pass

            def complete(self, **kwargs):
                type(self).calls += 1
                raise AssertionError("topic model must not run behind a provider fence")

        with patch("scisaurus.runtime.topic_discovery.OpenAlexClient", BlockedOpenAlex), \
                patch("scisaurus.runtime.topic_discovery.ModelClient", ExplodingModel):
            with self.assertRaises(ProviderCooldownError) as caught:
                TopicDiscoveryRunner({
                    "base_url": "http://example.invalid", "model": "fake", "protocol": "ollama",
                    "timeout_seconds": 1, "max_output_tokens": 4096,
                }).run(
                    "feasible science", sampling_seed=3, bibliography={}, max_attempts=1,
                )
        self.assertEqual(caught.exception.retry_after_seconds, 123)
        self.assertEqual(ExplodingModel.calls, 0)

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
