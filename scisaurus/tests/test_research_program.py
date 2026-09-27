"""Contracts for provisional multi-branch research programs."""

import copy
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.research_program import (
    build_research_program,
    project_research_program,
    validate_research_program,
)


def topic_package():
    forms = ("theory_simulation", "observational_reanalysis", "methodological_benchmark")
    evidence_modes = ("synthetic_simulation", "published_observations", "public_dataset")
    comparisons = ("mechanism_ablation", "cross_method", "model_selection")
    candidates = []
    for index in range(3):
        candidates.append({
            "id": f"branch_{index}",
            "title": f"Candidate branch {index}",
            "domain": f"domain {index}",
            "research_question": f"Does mechanism {index} change the outcome under a controlled comparison?",
            "scope": "A bounded public-data and local-computation study.",
            "search_queries": [f"mechanism {index} comparison", "controlled outcome study", "reproducible analysis"],
            "why_promising": "The comparison is testable and the closest prior work leaves its boundary open.",
            "disconfirmation_test": "Discard this direction if the contrast cannot be measured reproducibly.",
            "feasibility": "The declared runtime can execute the bounded comparison.",
            "resource_plan": "Use the current literature survey and a deterministic local experiment.",
            "research_form": forms[index],
            "evidence_mode": evidence_modes[index],
            "comparison_type": comparisons[index],
        })
    return {
        "schema_version": "topic-discovery-1",
        "objective": "Find a new testable question.",
        "candidates": candidates,
        "selected_id": "branch_1",
        "selection_rationale": "Branch 1 has the clearest measurement and the smallest dependency surface.",
    }


class ResearchProgramTests(unittest.TestCase):
    def test_scoped_single_candidate_refinement_survives_program_validation(self):
        package = topic_package()
        selected = next(item for item in package["candidates"]
                        if item["id"] == package["selected_id"])
        package["candidates"] = [selected]
        package["topic_evolution"] = {
            "mode": "refinement",
            "parent_topic_id": selected["id"],
            "package_contract": "single_candidate_parent_refinement",
        }

        program = build_research_program(package)

        self.assertEqual(program["selected_id"], selected["id"])
        self.assertEqual(len(program["branches"]), 1)

    def test_single_candidate_without_scoped_refinement_contract_is_rejected(self):
        package = topic_package()
        package["candidates"] = [
            item for item in package["candidates"]
            if item["id"] == package["selected_id"]
        ]

        with self.assertRaisesRegex(ValidationError, "configured candidate portfolio"):
            build_research_program(package)

    def test_scoped_single_candidate_refinement_must_match_parent_id(self):
        package = topic_package()
        selected = next(item for item in package["candidates"]
                        if item["id"] == package["selected_id"])
        package["candidates"] = [selected]
        package["topic_evolution"] = {
            "mode": "refinement",
            "parent_topic_id": "different_parent",
            "package_contract": "single_candidate_parent_refinement",
        }

        with self.assertRaisesRegex(ValidationError, "preserve exactly its parent topic"):
            build_research_program(package)

    def test_unselected_malformed_feasibility_plan_does_not_block_program_materialization(self):
        package = topic_package()
        package["candidates"][0]["feasibility_plan"] = {
            "experiment_input": "self_contained",
            "evidence_inputs": [{
                "kind": "project_artifact", "status": "available", "source": "stale output",
            }],
            "execution_mode": "foundry", "data_access": "closed_world",
            "required_packages": [], "required_executables": [],
            "estimated_compute_seconds": 600.0,
            "estimated_api_requests": 0, "estimated_model_calls": 0,
            "network_access": False,
        }
        program = build_research_program(package)
        self.assertEqual(program["selected_id"], "branch_1")
        self.assertEqual(len(program["branches"]), 3)

    def test_builds_selected_and_retained_conditional_branches(self):
        program = build_research_program(topic_package())
        validate_research_program(program)
        self.assertEqual(program["selected_id"], "branch_1")
        self.assertEqual(len(program["branches"]), 3)
        self.assertEqual(
            [branch["status"] for branch in program["branches"]],
            ["retained", "selected", "retained"],
        )
        for branch in program["branches"]:
            self.assertEqual(
                {item["id"] for item in branch["paper_if"]},
                {"supportive", "null_boundary", "ambiguous"},
            )
        projection = project_research_program(program, max_alternatives=1)
        self.assertEqual(projection["selected_branch"]["id"], "branch_1")
        self.assertEqual(len(projection["retained_branches"]), 1)

    def test_selection_is_provisional_and_structure_is_strict(self):
        program = build_research_program(topic_package())
        self.assertEqual(program["selection_mode"], "provisional")
        broken = copy.deepcopy(program)
        broken["selected_id"] = "branch_0"
        with self.assertRaisesRegex(ValidationError, "selected_id"):
            validate_research_program(broken)

        broken = copy.deepcopy(program)
        broken["branches"][0]["paper_if"][0]["id"] = "unsupported"
        with self.assertRaisesRegex(ValidationError, "supportive"):
            validate_research_program(broken)


if __name__ == "__main__":
    unittest.main()
