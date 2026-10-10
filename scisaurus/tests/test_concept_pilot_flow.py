"""Concept production and implementation evidence retain different obligations."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from scisaurus.runtime.composer import ComposerRunner
from scisaurus.runtime.survey import SurveyRunner
from scisaurus.runtime.material_development import scope_workflow_to_experiments
from scisaurus.tests.test_material_development import brief
from scisaurus.tests.test_survey import survey_config


class ConceptPilotFlowTests(unittest.TestCase):
    def test_experiment_goal_keeps_all_required_dependencies_and_original_manifest(self):
        workflow = {"completion": {"required_stage_ids": ["paper"], "release_requires_human": True},
                    "stages": [{"id": identity, "kind": kind, "depends_on": dependencies}
                               for identity, kind, dependencies in [
                                   ("topic", "topic_discovery", []), ("survey", "survey", ["topic"]),
                                   ("experiment", "experiment", ["survey"]),
                                   ("interpretation", "interpretation", ["experiment"]),
                                   ("paper", "paper", ["interpretation"])]]}
        original = deepcopy(workflow)
        scoped = scope_workflow_to_experiments(workflow)
        self.assertEqual([stage["id"] for stage in scoped["stages"]], ["topic", "survey", "experiment"])
        self.assertEqual(scoped["completion"]["required_stage_ids"], ["experiment"])
        self.assertTrue(scoped["completion"]["release_requires_human"])
        self.assertEqual(workflow, original)
        workflow["stages"][2]["depends_on"].append("missing")
        with self.assertRaisesRegex(Exception, "prerequisite"):
            scope_workflow_to_experiments(workflow)

    def test_concept_preflight_does_not_repeat_producer_selection(self):
        topic = {"kind": "topic_discovery"}
        select = ComposerRunner._active_stage_role_ids
        self.assertEqual(select(topic, descriptor={"intake_mode": "concept"}), [])
        self.assertIsNone(select(topic, descriptor={"intake_mode": "portfolio"}))
        self.assertIsNone(select(topic))
        self.assertEqual(select({"kind": "survey"}, descriptor={"intake_mode": "concept"}), ["search-strategist"])
        self.assertIsNone(select({"kind": "experiment"}, descriptor={"intake_mode": "concept"}))

    def test_implementation_keeps_authored_hypothesis_without_renomination(self):
        runner = ComposerRunner.__new__(ComposerRunner)
        topic = {"id": "design", "research_question": "Does the designed response exceed its baseline?",
                 "search_queries": ["constitutive model verification"], "design_brief": brief()}
        runner.workflow = {"stages": [{"id": "topic", "kind": "topic_discovery", "depends_on": []}],
                           "experiment_catalog": ["existing"]}
        runner.context = {"topic": {"topic": topic, "intake_mode": "concept"}}
        runner._topic_review_obligation = Mock(return_value=None)
        config = survey_config("http://127.0.0.1:1")
        original = deepcopy(config)
        projected = runner._apply_topic_to_survey_config({"id": "survey", "depends_on": ["topic"],
                                                        "project_dir": "/tmp/concept-pilot-survey"}, config)
        self.assertEqual(projected["survey"]["question"], topic["research_question"])
        self.assertEqual(projected["survey"]["proposed_gap"]["statement"], topic["design_brief"]["differentiation_hypothesis"])
        self.assertEqual(projected["survey"]["design_brief"], topic["design_brief"])
        self.assertEqual(projected["survey"]["search"], original["survey"]["search"])
        survey = SurveyRunner.__new__(SurveyRunner)
        survey.score = projected["survey"]
        survey.gate = Mock()
        survey.survey_ref = "accepted-current-survey"
        survey._publish = Mock(return_value={"artifact_ref": "current-nomination"})
        survey._model_checked = Mock(side_effect=AssertionError("no new gap-author request"))
        survey._nominate()
        self.assertEqual(survey.nomination, projected["survey"]["proposed_gap"])
        survey.gate.require_current.assert_called_once_with(survey.survey_ref)
        survey._model_checked.assert_not_called()

    def test_ordinary_survey_keeps_independent_search_planners(self):
        runner = SurveyRunner.__new__(SurveyRunner)
        runner.score = {}
        self.assertEqual(runner._initial_search_roles(), ("research.search-planner", "methods.blind-search-planner"))
        runner.score = {"design_brief": brief()}
        self.assertEqual(runner._initial_search_roles(), ())
