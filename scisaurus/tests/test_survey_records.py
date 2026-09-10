"""Evidence and assignment boundaries for literature maps and gap decisions."""
from copy import deepcopy
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.survey_records import (
    GAP_CHECKS,
    MAP_FIELDS,
    SURVEY_CHECKS,
    evidence,
    statement,
    validate_assessment,
    validate_map,
    validate_survey_review,
)


SOURCES = {
    "artifact:kb/source-one@1": {"work_id": "W1", "representation": "full_text", "identity_verified": True,
                                "text": "Treatment A improves the measured outcome. Generalization was not tested."},
    "artifact:kb/source-two@1": {"work_id": "W2", "representation": "full_text", "identity_verified": True,
                                "text": "Treatment B improves the same outcome in a different population."},
    "artifact:kb/source-three@1": {"work_id": "W3", "representation": "full_text", "identity_verified": True,
                                  "text": "Treatment C has an uncertain effect."},
}
WORKS = {"W1", "W2", "W3"}


def proof(work_id="W1"):
    ref, source = next((ref, source) for ref, source in SOURCES.items() if source["work_id"] == work_id)
    return {"work_id": work_id, "source_ref": ref, "quote": source["text"]}


def entry(work_id="W1"):
    return {"work_id": work_id, "inclusion": "included", "reason": "The study examines the scoped outcome.",
            **{field: {"text": None, "evidence": []} for field in MAP_FIELDS},
            "finding": {"text": SOURCES[proof(work_id)["source_ref"]]["text"], "evidence": [proof(work_id)]}}


def relationship():
    return {"source": "W1", "target": "W2", "kind": "related",
            "claim": {"text": "Both studies examine improvements in an outcome under different treatments.",
                      "evidence": [proof("W1"), proof("W2")]}}


def required_checks(names):
    return [{"check_id": name, "outcome": "passed", "method": "Inspect exact source captures and declared scope.",
             "result": "The scoped evidence requirement is satisfied."} for name in names]


def assessment(state="refuted_by_prior_work"):
    return {"state": state, "rationale": "The prior result addresses the bounded proposed claim.",
            "comparisons": [{"work_id": "W1", "relationship": "solves", "statement": "The study reports the proposed effect.",
                             "evidence": [proof()]}],
            "checks": required_checks(GAP_CHECKS), "evidence": [proof()]}


class TestSurveyEvidence(unittest.TestCase):
    def test_map_reports_all_invalid_fields_with_work_and_evidence_locations(self):
        first, second = entry("W1"), entry("W2")
        first["reason"] = {"text": "Incorrect type", "evidence": []}
        first["finding"]["evidence"][0]["quote"] = "Invented result."
        second["problem"] = {"text": "Unsupported problem", "evidence": []}
        before = deepcopy([first, second])
        with self.assertRaises(ValidationError) as error:
            validate_map({"entries": [first, second], "relationships": []}, ["W1", "W2"], WORKS, SOURCES)
        message = str(error.exception)
        self.assertIn("entries[0] (W1).reason", message)
        self.assertIn("entries[0] (W1).finding: evidence[0]", message)
        self.assertIn("artifact:kb/source-one@1", message)
        self.assertIn("entries[1] (W2).problem", message)
        self.assertEqual([first, second], before)

    def test_valid_exact_quote_remains_bound_to_source_and_work(self):
        original = [proof()]
        before = deepcopy(original)
        evidence(original, SOURCES, required=True)
        self.assertEqual(original, before)

    def test_missing_capture_wrong_work_and_invented_quote_are_rejected(self):
        for field, value in (("source_ref", "artifact:kb/missing@1"), ("work_id", "W2"),
                             ("quote", "The intervention has no limitations.")):
            with self.subTest(field=field):
                item = proof()
                item[field] = value
                with self.assertRaisesRegex(ValidationError, "exact captured text"):
                    evidence([item], SOURCES, required=True)

    def test_evidence_cannot_replace_an_exact_quote_with_a_paraphrase(self):
        item = proof()
        item["quote"] = "Treatment A improves outcomes."
        with self.assertRaisesRegex(ValidationError, "exact captured text"):
            evidence([item], SOURCES)

    def test_malformed_evidence_identity_is_a_controlled_validation_failure(self):
        for field in ("source_ref", "work_id"):
            for value in ([], {}, None, 12):
                with self.subTest(field=field, value=value):
                    item = proof()
                    item[field] = value
                    with self.assertRaises(ValidationError):
                        evidence([item], SOURCES, required=True)

    def test_assertion_requires_nonempty_text_and_nonempty_evidence(self):
        for value in ({"text": "An asserted finding", "evidence": []}, {"text": " ", "evidence": [proof()]},
                      {"text": "An asserted finding", "evidence": None}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                statement(value, SOURCES)

    def test_unknown_statement_requires_an_explicit_empty_evidence_list(self):
        statement({"text": None, "evidence": []}, SOURCES)
        for proofs in ([proof()], None, {}, ""):
            with self.subTest(proofs=proofs), self.assertRaisesRegex(ValidationError, "unknown statement"):
                statement({"text": None, "evidence": proofs}, SOURCES)

    def test_work_statement_cannot_borrow_real_evidence_from_another_work(self):
        value = {"text": "The treatment improves the measured outcome.", "evidence": [proof("W2")]}
        with self.assertRaisesRegex(ValidationError, "from that work"):
            statement(value, SOURCES, work_id="W1")


class TestSurveyMap(unittest.TestCase):
    def validate(self, value, requested=("W1",)):
        validate_map(value, requested, WORKS, SOURCES)

    def test_scoped_map_accepts_evidence_bound_links_to_other_known_works(self):
        value = {"entries": [entry()], "relationships": [relationship()]}
        original = deepcopy(value)
        self.validate(value)
        self.assertEqual(value, original)

    def test_scoped_update_cannot_change_an_unassigned_work(self):
        value = {"entries": [entry("W1"), entry("W2")], "relationships": []}
        with self.assertRaisesRegex(ValidationError, "unassigned"):
            self.validate(value)

    def test_each_assigned_work_must_appear_exactly_once(self):
        for entries in ([entry("W1")], [entry("W1"), entry("W1"), entry("W2")]):
            with self.subTest(entries=entries), self.assertRaises(ValidationError):
                self.validate({"entries": entries, "relationships": []}, requested=("W1", "W2"))

    def test_each_work_field_cannot_import_another_work_source(self):
        for field in MAP_FIELDS:
            with self.subTest(field=field):
                item = entry()
                item[field] = {"text": "A claim copied from another work.", "evidence": [proof("W2")]}
                with self.assertRaisesRegex(ValidationError, "from that work"):
                    self.validate({"entries": [item], "relationships": []})

    def test_unknown_screening_state_and_unstructured_entries_are_rejected(self):
        value = {"entries": [entry()], "relationships": []}
        value["entries"][0]["inclusion"] = "accepted-as-ground-truth"
        with self.assertRaisesRegex(ValidationError, "screening decision"):
            self.validate(value)
        value["entries"] = {"W1": entry()}
        with self.assertRaisesRegex(ValidationError, "must be lists"):
            self.validate(value)

    def test_relationship_requires_known_distinct_works_and_assignment_overlap(self):
        for source, target in (("W1", "W404"), ("W1", "W1"), ("W2", "W3")):
            with self.subTest(source=source, target=target):
                relation = relationship()
                relation.update(source=source, target=target)
                with self.assertRaisesRegex(ValidationError, "known works.*assigned work"):
                    self.validate({"entries": [entry()], "relationships": [relation]})

    def test_unknown_and_duplicate_conceptual_relationships_are_rejected(self):
        relation = relationship()
        relation["kind"] = "cites"
        with self.assertRaisesRegex(ValidationError, "unsupported conceptual relationship"):
            self.validate({"entries": [entry()], "relationships": [relation]})
        with self.assertRaisesRegex(ValidationError, "duplicate conceptual relationship"):
            self.validate({"entries": [entry()], "relationships": [relationship(), relationship()]})

    def test_related_claim_requires_evidence_from_both_works(self):
        for proofs in ([proof("W1")], [proof("W2")], [proof("W1"), proof("W3")]):
            with self.subTest(proofs=proofs):
                relation = relationship()
                relation["claim"]["evidence"] = proofs
                with self.assertRaisesRegex(ValidationError, "both works"):
                    self.validate({"entries": [entry()], "relationships": [relation]})

    def test_null_relationship_cannot_manufacture_a_supported_edge(self):
        relation = relationship()
        relation["claim"] = {"text": None, "evidence": []}
        with self.assertRaisesRegex(ValidationError, "both works"):
            self.validate({"entries": [entry()], "relationships": [relation]})


class TestGapAssessment(unittest.TestCase):
    def validate(self, value):
        validate_assessment(value, SOURCES, WORKS)

    def test_refutation_binds_a_prior_solution_and_exact_evidence(self):
        value = assessment()
        original = deepcopy(value)
        self.validate(value)
        self.assertEqual(value, original)

    def test_refutation_cannot_be_based_only_on_partial_different_or_uncertain_work(self):
        for relation in ("partial", "different", "uncertain"):
            with self.subTest(relation=relation):
                value = assessment()
                value["comparisons"][0]["relationship"] = relation
                with self.assertRaisesRegex(ValidationError, "identify a prior solution"):
                    self.validate(value)
        value = assessment()
        value["comparisons"] = []
        with self.assertRaisesRegex(ValidationError, "identify a prior solution"):
            self.validate(value)

    def test_eligibility_requires_resolved_unsolved_comparisons(self):
        for relation in ("solves", "uncertain"):
            with self.subTest(relation=relation):
                value = assessment("eligible_for_experiment")
                value["comparisons"][0]["relationship"] = relation
                with self.assertRaisesRegex(ValidationError, "cannot authorize experiment eligibility"):
                    self.validate(value)
        value = assessment("eligible_for_experiment")
        value["comparisons"] = []
        with self.assertRaisesRegex(ValidationError, "cannot authorize experiment eligibility"):
            self.validate(value)
        for relation in ("partial", "different"):
            with self.subTest(relation=relation):
                value = assessment("eligible_for_experiment")
                value["comparisons"][0]["relationship"] = relation
                self.validate(value)

    def test_both_decisive_states_require_explicit_top_level_evidence(self):
        for state in ("refuted_by_prior_work", "eligible_for_experiment"):
            with self.subTest(state=state):
                value = assessment(state)
                value["evidence"] = []
                with self.assertRaisesRegex(ValidationError, "explicit source evidence"):
                    self.validate(value)

    def test_unrelated_full_text_cannot_validate_an_abstract_only_decisive_comparison(self):
        for state, relation in (("refuted_by_prior_work", "solves"), ("eligible_for_experiment", "partial")):
            for source_changes in ({"representation": "abstract"}, {"identity_verified": False},
                                   {"identity_verified": 1}, {"representation": None, "identity_verified": None}):
                with self.subTest(state=state, source_changes=source_changes):
                    value = assessment(state)
                    value["comparisons"] = [{"work_id": "W2", "relationship": relation,
                                             "statement": "The second study addresses the proposed task.",
                                             "evidence": [proof("W2")]}]
                    sources = deepcopy(SOURCES)
                    sources[proof("W2")["source_ref"]].update(source_changes)
                    self.assertEqual(value["evidence"], [proof("W1")])
                    self.assertEqual(sources[proof("W1")["source_ref"]]["representation"], "full_text")
                    self.assertIs(sources[proof("W1")["source_ref"]]["identity_verified"], True)
                    with self.assertRaisesRegex(ValidationError, "decisive comparison requires verified full text"):
                        validate_assessment(value, sources, WORKS)

    def test_available_full_text_must_be_cited_by_the_decisive_comparison(self):
        value = assessment()
        sources = deepcopy(SOURCES)
        abstract_ref = "artifact:kb/source-one-abstract@1"
        sources[abstract_ref] = {"work_id": "W1", "representation": "abstract", "identity_verified": True,
                                 "text": SOURCES[proof()["source_ref"]]["text"]}
        value["comparisons"][0]["evidence"][0]["source_ref"] = abstract_ref
        with self.assertRaisesRegex(ValidationError, "decisive comparison requires verified full text"):
            validate_assessment(value, sources, WORKS)

    def test_abstention_can_retain_uncertain_comparison_without_invented_evidence(self):
        value = assessment("insufficient_evidence")
        value["evidence"] = []
        value["comparisons"][0].update(relationship="uncertain", evidence=[])
        value["checks"][0]["outcome"] = "insufficient_evidence"
        self.validate(value)

    def test_asserted_comparison_requires_evidence_from_its_own_work(self):
        for proofs in ([], [proof("W2")]):
            with self.subTest(proofs=proofs):
                value = assessment()
                value["comparisons"][0]["evidence"] = proofs
                with self.assertRaises(ValidationError):
                    self.validate(value)

    def test_comparison_cannot_name_unknown_work_or_repeat_a_work(self):
        value = assessment()
        value["comparisons"][0]["work_id"] = "W404"
        with self.assertRaisesRegex(ValidationError, "unknown or duplicate work"):
            self.validate(value)
        value = assessment()
        value["comparisons"].append(deepcopy(value["comparisons"][0]))
        with self.assertRaisesRegex(ValidationError, "unknown or duplicate work"):
            self.validate(value)

    def test_unknown_comparison_relationship_cannot_supply_a_prior_solution(self):
        value = assessment()
        value["comparisons"][0]["relationship"] = "probably-solves"
        with self.assertRaisesRegex(ValidationError, "unknown comparison relationship"):
            self.validate(value)


class TestSurveyChecks(unittest.TestCase):
    def test_every_check_is_required_once_in_each_review_contract(self):
        for kind in ("survey", "gap"):
            for mutation in ("omitted", "duplicate", "unknown", "unknown-outcome"):
                with self.subTest(kind=kind, mutation=mutation):
                    value = ({"checks": required_checks(SURVEY_CHECKS), "rationale": "The search and map are supported."}
                             if kind == "survey" else assessment())
                    if mutation == "omitted":
                        value["checks"].pop()
                    elif mutation == "duplicate":
                        value["checks"].append(deepcopy(value["checks"][0]))
                    elif mutation == "unknown":
                        value["checks"][0]["check_id"] = "unregistered-check"
                    else:
                        value["checks"][0]["outcome"] = "maybe-passed"
                    with self.assertRaises(ValidationError):
                        if kind == "survey":
                            validate_survey_review(value)
                        else:
                            validate_assessment(value, SOURCES, WORKS)

    def test_review_reports_can_preserve_failed_checks_for_downstream_gate(self):
        value = {"checks": required_checks(SURVEY_CHECKS), "rationale": "A source fidelity defect remains unresolved."}
        value["checks"][1]["outcome"] = "failed"
        validate_survey_review(value)


if __name__ == "__main__":
    unittest.main()
