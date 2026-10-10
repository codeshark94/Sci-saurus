import unittest
from copy import deepcopy
from scisaurus.core.errors import ValidationError
from scisaurus.runtime.material_development import (
    DESIGN_BRIEF_REVISION, validate_design_brief, implementation_evidence_scope,
    validate_concept_candidates, concept_intake_contract,
)


def brief():
    return {
        "schema_version": DESIGN_BRIEF_REVISION,
        "use_case": "Vibration isolation", "differentiation_hypothesis": "Graded compliant connections may broaden attenuation",
        "design_family_id": "lattice", "physics_family_ids": ["elasticity"],
        "parameters": [{"name": "strut radius", "unit": "mm", "lower": 0.2, "upper": 0.8}],
        "target_response": {"quantity": "transmission", "unit": "dimensionless", "direction": "minimize",
                            "rationale": "Reduce transmitted vibration"},
        "baseline": "Uniform lattice with equal volume", "constraints": ["Equal specimen volume"],
        "first_pilot": {"calculation": "Small linear elastic frequency sweep", "observable": "Displacement and transmission",
                        "verification": "Uniform specimen analytic limit", "editable_design": "Parametric CAD geometry"},
    }


def concept_candidates():
    records = []
    for index, (function, mechanism, physics) in enumerate([
            ("Thermal routing", "Anisotropic conduction", "thermal"),
            ("Optical filtering", "Interference of dielectric modes", "electromagnetic"),
            ("Flow mixing", "Advection across microchannel interfaces", "fluid")]):
        design = brief()
        design.update(use_case=function, physics_family_ids=[physics], design_family_id="pattern")
        records.append({"id": f"concept_{index}", "mechanism": mechanism, "design_brief": design})
    return records


class MaterialDevelopmentTests(unittest.TestCase):
    def test_reference_handoff_carries_requirements_without_scientific_fulfillment(self):
        from scisaurus.runtime.material_development import implementation_reference_handoff
        design = brief(); orders = [{"id": "measurement", "objective": "Check the baseline"}]
        packet = implementation_reference_handoff(question="Compare responses", design_brief=design,
            survey_ref="artifact:kb/surveys/current@1", assessment_ref="artifact:kb/gap-assessments/current@1",
            assessment_state="insufficient_evidence", work_orders=orders, project_dir="/tmp/references")
        self.assertEqual(packet["assessment_state"], "insufficient_evidence")
        self.assertEqual(packet["open_work_orders"], orders)
        self.assertIn("not fulfilled", packet["completion_boundary"])
        orders[0]["objective"] = "changed"; design["baseline"] = "changed"
        self.assertEqual(packet["open_work_orders"][0]["objective"], "Check the baseline")
        self.assertNotEqual(packet["design_brief"], design)
        with self.assertRaises(ValidationError):
            implementation_reference_handoff(question="Compare responses", design_brief=brief(),
                survey_ref=packet["survey_ref"], assessment_ref=packet["assessment_ref"],
                assessment_state="refuted_by_prior_work", work_orders=[], project_dir="/tmp/references")
    def test_concept_comparison_spans_supported_nonstructural_physics(self):
        candidates = concept_candidates()
        laboratory = {"design_families": [{"id": "pattern"}],
                      "physics_families": [{"id": name} for name in ("thermal", "electromagnetic", "fluid")]}
        self.assertIs(validate_concept_candidates(candidates, "concept_1", laboratory), candidates)
        invalid = deepcopy(candidates)
        invalid[0]["design_brief"]["physics_family_ids"] = ["unavailable"]
        with self.assertRaisesRegex(ValidationError, "undeclared"):
            validate_concept_candidates(invalid, "concept_1", laboratory)

    def test_comparison_rejects_cosmetic_mechanism_duplicates(self):
        candidates = concept_candidates()
        for index, candidate in enumerate(candidates):
            candidate["mechanism"] = "  ANISOTROPIC   conduction " if index else "Anisotropic conduction"
            candidate["design_brief"]["parameters"][0]["upper"] += index
        with self.assertRaisesRegex(ValidationError, "different physical mechanisms"):
            validate_concept_candidates(candidates, "concept_1")

    def test_comparison_requires_all_briefs_and_unique_owned_selection(self):
        for transform in (lambda c: c[0].pop("design_brief"),
                          lambda c: c[0].update(id=c[1]["id"]),
                          lambda c: c[0].pop("mechanism")):
            candidates = concept_candidates()
            transform(candidates)
            with self.assertRaises(ValidationError):
                validate_concept_candidates(candidates, "concept_1")
        with self.assertRaisesRegex(ValidationError, "belong"):
            validate_concept_candidates(concept_candidates(), "unknown")
        validate_concept_candidates([{"id": "legacy", "design_brief": brief()}], "legacy")

    def test_intake_contract_prioritizes_function_inside_attested_tools(self):
        contract = concept_intake_contract()
        rules = " ".join(contract["exploration_rules"])
        self.assertIn("Unconventional use cases", rules)
        self.assertIn("currently attested tools", rules)
        self.assertIn("electromagnetic", rules)
        self.assertIn("Do not assume additional installations", rules)
        self.assertIn("same bounded response", rules)
        self.assertIn("cosmetic diversity", contract["review_rule"])

    def test_native_verifier_retains_whole_concept_comparison_at_every_detail(self):
        from scisaurus.runtime.specialists import _verifier_chief_result, build_verifier_prompt
        import json
        candidates = [deepcopy(concept_candidates()[index % 3]) for index in range(8)]
        for index, candidate in enumerate(candidates):
            candidate["id"] = f"concept_{index}"
            candidate["feasibility_plan"] = {"runtime_labels": ["attested"], "evidence_inputs": []}
        result = {"intake_mode": "concept", "novelty_status": "unverified", "candidates": candidates,
                  "selected_id": "concept_7", "topic": candidates[-1]}
        for detail in ("full", "compact", "minimal", "focused"):
            projected = _verifier_chief_result(result, detail=detail)
            with self.subTest(detail=detail):
                self.assertEqual(projected["candidates"], candidates)
        packet = {"stage_acceptance_contract": {"current_stage_id": "topic", "downstream_stage_ids": ["survey"],
                                                "concept_intake": concept_intake_contract()}}
        payload = json.loads(build_verifier_prompt({"id": "topic", "kind": "topic_discovery"}, packet, [], result))
        self.assertEqual(payload["chief_result"]["candidates"], candidates)
        self.assertIn("cosmetic diversity", payload["verifier_contract"]["concept_intake"]["review_rule"])

    def test_native_specialist_and_verifier_receive_attested_laboratory_limits(self):
        from scisaurus.runtime.specialists import build_specialist_prompt, build_verifier_prompt
        import json
        laboratory = {"schema_version": "laboratory-context-1", "config_sha256": "a" * 64,
                      "physics_families": [{"id": "thermal", "runtime": "heat", "limitations": ["No phase change"],
                                            "coupling_role": "independent", "coupling_note": "No automatic feedback"}],
                      "runtimes": [{"label": "heat", "controller_attested": True}],
                      "assessment_requirements": {"verification": "analytic limit"}}
        packet = {"objective": "Develop a useful material", "laboratory": laboratory}
        assignment = {"role_id": "topic-maturity-reviewer", "assigned_role": "research.topic-maturity-reviewer",
                      "stage_id": "topic", "stage_kind": "topic_discovery", "input_projection": ["objective"]}
        specialist = json.loads(build_specialist_prompt(assignment, packet))
        verifier = json.loads(build_verifier_prompt({"id": "topic", "kind": "topic_discovery"}, packet, [], {}))
        self.assertEqual(specialist["shared_stage_context"]["laboratory"], laboratory)
        self.assertEqual(verifier["laboratory"], laboratory)

    def test_brief_is_design_not_result(self):
        value = brief()
        self.assertIs(validate_design_brief(value), value)
        scope = implementation_evidence_scope(value)
        value["parameters"][0]["upper"] = 3
        self.assertEqual(scope["design_brief"]["parameters"][0]["upper"], 0.8)
        self.assertIn("competing designs", " ".join(scope["evidence_obligations"]))

    def test_bad_brief_is_rejected(self):
        for key in brief():
            value = brief(); value.pop(key)
            with self.subTest(key=key), self.assertRaises(ValidationError):
                validate_design_brief(value)
        for bad in [True, None, float("nan"), float("inf"), 10 ** 400, "1"]:
            value = brief(); value["parameters"][0]["upper"] = bad
            with self.subTest(bound=bad), self.assertRaises(ValidationError):
                validate_design_brief(value)
        for bad in [[], [None], [{}], ["elasticity", "elasticity"]]:
            value = brief(); value["physics_family_ids"] = bad
            with self.subTest(physics=bad), self.assertRaises(ValidationError):
                validate_design_brief(value)
        value = brief(); value["parameters"][0]["upper"] = 0.1
        with self.assertRaises(ValidationError):
            validate_design_brief(value)
        value = brief(); value["achieved_performance"] = 42
        with self.assertRaises(ValidationError):
            validate_design_brief(value)

    def test_declared_lab_boundary(self):
        validate_design_brief(brief(), {"design_families": [{"id": "lattice"}], "physics_families": [{"id": "elasticity"}]})
        with self.assertRaises(ValidationError):
            validate_design_brief(brief(), {"design_families": [], "physics_families": []})

    def test_review_keeps_brief_and_phase_boundary(self):
        import json
        from scisaurus.runtime.material_development import concept_intake_contract
        from scisaurus.runtime.specialists import build_verifier_prompt, _compact_topic_maturity_projection
        chief = {"intake_mode": "concept", "novelty_status": "unverified", "topic": {"id": "design", "design_brief": brief()}}
        packet = {"stage_acceptance_contract": {"current_stage_id": "topic", "downstream_stage_ids": ["survey", "experiment"], "concept_intake": concept_intake_contract()}}
        response = json.loads(build_verifier_prompt({"id": "topic", "kind": "topic_discovery"}, packet, [], chief))
        self.assertEqual(response["chief_result"]["topic"]["design_brief"], brief())
        self.assertEqual(response["chief_result"]["novelty_status"], "unverified")
        self.assertIn("follow concept selection", response["verifier_contract"]["provisional_rule"])
        projection = _compact_topic_maturity_projection({"topic": chief["topic"]})
        self.assertEqual(projection["topic"]["design_brief"], brief())

    def test_survey_assignments_keep_implementation_scope(self):
        from scisaurus.runtime.survey import SurveyRunner
        runner = object.__new__(SurveyRunner)
        runner.score = {"design_brief": brief()}
        runner.work_orders = []
        result = runner._follow_up_assignment({"phase": "exploration_plan"})
        self.assertEqual(result["implementation_evidence_scope"]["design_brief"], brief())
        self.assertIn("source integrity", result["implementation_evidence_scope"]["completion_boundary"])
