"""Evidence and assignment boundaries for literature maps and gap decisions."""
from copy import deepcopy
import unittest

from scisaurus.core.errors import ModelContractError, ValidationError
from scisaurus.runtime.survey_records import (
    GAP_CHECKS,
    MAP_FIELDS,
    SURVEY_CHECKS,
    evidence,
    statement,
    validate_assessment,
    validate_map,
    validate_survey_review,
    validate_follow_up_result,
    follow_up_response_contract,
    normalize_survey_review_envelope, survey_review_response_contract,
    survey_review_assignment_identity, SURVEY_QUOTE_LOCATION_INSTRUCTION,
)


SOURCES = {
    "artifact:kb/source-one@1": {"work_id": "W1", "representation": "full_text", "identity_verified": True,
                                "identity_checks": {"title_match": True, "section_markers": ["Results"]},
                                "text": "Treatment A improves the measured outcome. Generalization was not tested."},
    "artifact:kb/source-two@1": {"work_id": "W2", "representation": "full_text", "identity_verified": True,
                                "identity_checks": {"title_match": True, "section_markers": ["Results"]},
                                "text": "Treatment B improves the same outcome in a different population."},
    "artifact:kb/source-three@1": {"work_id": "W3", "representation": "full_text", "identity_verified": True,
                                  "identity_checks": {"title_match": True, "section_markers": ["Results"]},
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
    def test_follow_up_dispatch_and_validator_share_status_and_field_contract(self):
        contract = follow_up_response_contract()
        row = {"id": "one", "status": "unresolved", "rationale": "Capture unavailable.", "evidence": [],
               "query_refs": [], "limitation": "Access denied.", "next_action": "Retain the missing input.",
               "completion": {"outcome": "unmet", "rationale": "Required capture is absent."}}
        self.assertEqual(set(row), set(contract["required_order_fields"]))
        self.assertEqual(contract["status"], ["resolved", "limited", "unresolved"])
        for status in contract["status"]:
            candidate = {**row, "status": status}
            if status == "resolved":
                from scisaurus.core.source_spans import bind
                candidate["evidence"] = bind([proof()], SOURCES)
            if status == "limited":
                candidate["query_refs"] = ["artifact:query@1"]
            windows = {ref: {"start": 0, "end": len(source["text"])} for ref, source in SOURCES.items()}
            validate_follow_up_result({"orders": [candidate]}, [{"id": "one"}], SOURCES,
                                      ["artifact:query@1"], windows=windows, require_completion=True)
        for status in ("unavailable", "unknown", "partial"):
            with self.subTest(status=status), self.assertRaises(ModelContractError):
                validate_follow_up_result({"orders": [{**row, "status": status}]}, [{"id": "one"}],
                                          {}, [], windows={}, require_completion=True)
        contract["status"].append("unavailable")
        self.assertNotIn("unavailable", follow_up_response_contract()["status"])

    def test_follow_up_repair_preserves_dispatched_catalog_and_exposes_enums(self):
        from unittest.mock import Mock
        from scisaurus.core.source_spans import index_evidence
        from scisaurus.runtime.survey import SurveyRunner
        runner = object.__new__(SurveyRunner)
        runner.score, runner.work_orders = {}, []
        runner._map_input_limit = lambda actor: None
        runner._follow_up_repair_catalog = Mock(side_effect=AssertionError("catalog changed"))
        _, catalog = index_evidence({"evidence": [proof()]}, SOURCES)
        assignment = {"phase": "survey_follow_up", "instructions": "Preserve the exact operation.",
                      "response_contract": follow_up_response_contract(), "evidence_catalog": catalog,
                      "sources": [{"source_ref": ref, "text": source["text"],
                                   "window": {"start": 0, "end": len(source["text"])}}
                                  for ref, source in SOURCES.items()]}
        original = deepcopy(assignment)
        repaired = runner._repair_assignment(
            {"actor": "methods.evidence-verifier", "assignment": assignment},
            {"error": "invalid status", "finish_reason": "stop",
             "previous_response": {"orders": [{"id": "one", "status": "unavailable"}]}})
        self.assertEqual(assignment, original)
        self.assertEqual(repaired["evidence_catalog"], catalog)
        self.assertEqual(repaired["response_contract"], follow_up_response_contract())
        self.assertEqual(repaired["validation_feedback"]["response_contract"], repaired["response_contract"])
        self.assertEqual(repaired["validation_feedback"]["previous_dispositions"][0]["status"], "unavailable")
        self.assertTrue(all("text" not in source for source in repaired["sources"]))

    def test_review_check_rejection_identifies_unexpected_nested_findings(self):
        value = {"checks": required_checks(SURVEY_CHECKS), "rationale": "Inspect the exact claims."}
        value["checks"][1]["findings"] = []
        with self.assertRaisesRegex(ValidationError, "unexpected fields \\['findings'\\]"):
            validate_survey_review(value)

    def test_follow_up_source_and_search_binding_errors_are_model_contract_failures(self):
        from scisaurus.core.source_spans import bind
        row = {"id": "one", "status": "limited", "rationale": "The capture remains bounded.",
               "evidence": bind([proof()], SOURCES), "query_refs": ["artifact:query@1"],
               "limitation": "No independent measurement.", "next_action": "Acquire a measurement."}
        windows = {ref: {"start": 0, "end": len(source["text"])} for ref, source in SOURCES.items()}
        for field, replacement in (("query_refs", ["artifact:invented@1"]), ("limitation", "")):
            with self.subTest(field=field), self.assertRaises(ModelContractError):
                validate_follow_up_result({"orders": [{**row, field: replacement}]}, [{"id": "one"}],
                                          SOURCES, row["query_refs"], windows=windows)
        bad = deepcopy(row)
        bad["evidence"][0]["work_id"] = "W2"
        with self.assertRaises(ModelContractError):
            validate_follow_up_result({"orders": [bad]}, [{"id": "one"}], SOURCES, row["query_refs"], windows=windows)

    def test_follow_up_evidence_ids_require_the_exact_dispatched_catalog(self):
        from scisaurus.core.source_spans import index_evidence
        from scisaurus.runtime.survey import SurveyRunner
        runner = object.__new__(SurveyRunner)
        runner.source_docs = SOURCES
        indexed, catalog = index_evidence({"orders": [{"evidence": [proof()]}]}, SOURCES)
        assignment = {"sources": [{"source_ref": ref, "window": {"start": 0, "end": len(source["text"])}}
                                  for ref, source in SOURCES.items()], "evidence_catalog": catalog}
        bound = runner._normalize_follow_up_result(indexed, assignment)
        self.assertEqual(bound["orders"][0]["evidence"][0]["quote"], proof()["quote"])
        with self.assertRaises(ModelContractError):
            runner._normalize_follow_up_result(indexed, {**assignment, "evidence_catalog": []})
        forged = deepcopy(assignment)
        forged["evidence_catalog"][0]["source_ref"] = "artifact:kb/source-two@1"
        with self.assertRaises(ModelContractError):
            runner._normalize_follow_up_result(indexed, forged)

    def test_record_provenance_requires_pinned_metadata_and_never_closes_science(self):
        inventory = {"works": [{"work_id": "W1", "work_ref": "artifact:kb/works/W1@2",
                                "map_entry_ref": "artifact:kb/work-analyses/W1@3"}]}
        row = {"id": "one", "status": "unresolved", "rationale": "Current record retained; earlier projection absent.",
               "evidence": [], "record_evidence": [inventory["works"][0]["work_ref"]], "query_refs": [],
               "limitation": "Earlier projection provenance is not captured.", "next_action": "Inspect upstream projection."}
        validate_follow_up_result({"orders": [row]}, [{"id": "one"}], {}, [], windows={}, record_inventory=inventory)
        for field, value in [("record_evidence", ["artifact:kb/works/W1@1"]),
                             ("record_evidence", ["artifact:kb/works/W2@2"]), ("record_evidence", row["record_evidence"]*2),
                             ("record_evidence", "artifact:kb/works/W1@2"), ("status", "limited"), ("status", "resolved")]:
            with self.subTest(field=field, value=value), self.assertRaises(ModelContractError):
                validate_follow_up_result({"orders": [{**row, field: value}]}, [{"id": "one"}], {}, [],
                                          windows={}, record_inventory=inventory)
        with self.assertRaises(ModelContractError):
            validate_follow_up_result({"orders": [row]}, [{"id": "one"}], {}, [], windows={})

    def test_follow_up_schema_errors_retain_model_contract_type(self):
        order = {"id": "one"}
        row = {"id": "one", "status": "unresolved", "rationale": "Absent evidence.", "evidence": [],
               "query_refs": [], "limitation": "Source absent.", "next_action": "Acquire exact sources."}
        values = [{"orders": []}, {"orders": [{"id": "one", "status": "limited", "rationale": "Bounded."}]}]
        values.extend({"orders": [{**row, field: replacement}]} for field, replacement in (
            ("rationale", 42), ("limitation", 42), ("query_refs", None), ("evidence", None)))
        for value in values:
            with self.subTest(value=value), self.assertRaises(ModelContractError):
                validate_follow_up_result(value, [order], SOURCES, [], windows={})

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

    def test_unverified_and_untyped_captures_cannot_support_map_fields_or_links(self):
        for changes in ({"representation": "unverified_text"}, {"representation": "other"},
                        {"identity_verified": False}, {"identity_verified": 1},
                        {"identity_checks": {"title_match": True, "section_markers": []}},
                        {"identity_checks": {"title_match": True, "section_markers": ["Abstract"]}},
                        {"identity_checks": {"title_match": True, "section_markers": ["**Abstract**"]}},
                        {"identity_checks": {"title_match": True, "section_markers": ["Summary."]}}):
            sources = deepcopy(SOURCES)
            sources[proof()["source_ref"]].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValidationError, "identity-verified full text"):
                validate_map({"entries": [entry()], "relationships": [relationship()]}, ["W1"], WORKS, sources)
        sources = deepcopy(SOURCES)
        sources[proof()["source_ref"]]["representation"] = "abstract"
        validate_map({"entries": [entry()], "relationships": []}, ["W1"], WORKS, sources)

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

    def test_nondecisive_comparison_cannot_quote_unverified_work(self):
        value = assessment("insufficient_evidence")
        value["comparisons"][0]["relationship"] = "different"
        sources = deepcopy(SOURCES)
        sources[proof()["source_ref"]].update(representation="unverified_text", identity_verified=False)
        with self.assertRaisesRegex(ValidationError, "identity-verified full text"):
            validate_assessment(value, sources, WORKS)

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
                    with self.assertRaisesRegex(ValidationError, "decisive comparison requires verified full text|identity-verified full text"):
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
    def test_indexed_assertion_catalog_keeps_exact_binding_without_repeating_text(self):
        current, raw = self.quote_location_fixture()
        before = deepcopy(current)
        full = survey_review_response_contract(current)
        indexed = survey_review_response_contract(current, indexed=True)
        self.assertEqual([row["assertion_id"] for row in indexed["assertion_catalog"]],
                         [row["assertion_id"] for row in full["assertion_catalog"]])
        self.assertTrue(all("quote" not in row for row in indexed["assertion_catalog"]))
        selected = next(row for row in indexed["assertion_catalog"] if row["field"] == "inclusion")
        raw["findings"] = [{"check_id": "map-support", "assertion_id": selected["assertion_id"],
                            "rationale": "Read the complete current screening rationale."}]
        raw["checks"][2]["outcome"] = "failed"
        bound = normalize_survey_review_envelope(raw, current_map=current)
        validate_survey_review(bound, current_map=current)
        self.assertEqual(bound["findings"][0]["quote"], current["entries"][0]["reason"])
        self.assertEqual(current, before)

    def test_selection_and_legacy_response_schemas_have_distinct_fields(self):
        current, _ = self.quote_location_fixture()
        contract = survey_review_response_contract(current)
        self.assertEqual(set(contract["findings"]["required_fields"]), {"check_id", "assertion_id", "rationale"})
        self.assertFalse(contract["findings"]["additional_fields"])
        self.assertNotIn("optional_fields", contract["findings"])
        self.assertNotIn("quote_field", contract["findings"])
        legacy = survey_review_response_contract(current, legacy=True)
        self.assertNotIn("assertion_catalog", legacy)
        self.assertIn("quote", legacy["findings"]["required_fields"])

    def test_assertion_selection_preserves_verdict_rationale_and_edit_authority(self):
        current, raw = self.quote_location_fixture()
        selected = next(row for row in survey_review_response_contract(current)["assertion_catalog"]
                        if row["field"] == "inclusion")
        finding = raw["findings"][0]
        raw["findings"] = [{"check_id": finding["check_id"], "assertion_id": selected["assertion_id"],
                            "rationale": finding["rationale"]}]
        before = deepcopy(raw)
        bound = normalize_survey_review_envelope(raw, current_map=current)
        validate_survey_review(bound, current_map=current)
        self.assertEqual(bound["checks"], raw["checks"])
        self.assertEqual(bound["rationale"], raw["rationale"])
        self.assertEqual(bound["findings"][0], {**finding, "quote_field": "reason"})
        self.assertEqual(raw, before)
        self.assertEqual(normalize_survey_review_envelope(bound, current_map=current), bound)

    def test_assertion_selection_rejects_stale_changed_unknown_and_conflicting_bindings(self):
        current, raw = self.quote_location_fixture()
        selected = next(row for row in survey_review_response_contract(current)["assertion_catalog"]
                        if row["field"] == "inclusion")
        raw["findings"] = [{"check_id": "map-support", "assertion_id": selected["assertion_id"],
                            "rationale": "Check the screening scope."}]
        for mutation in ("version", "text", "unknown", "conflict", "invalid_type"):
            with self.subTest(mutation=mutation):
                target, proposal = deepcopy(current), deepcopy(raw)
                if mutation == "version": target["entry_refs"]["W1"] = "artifact:kb/work-analyses/W1@3"
                elif mutation == "text": target["entries"][0]["reason"] += " A changed qualifier."
                elif mutation == "unknown": proposal["findings"][0]["assertion_id"] = "assertion-missing"
                elif mutation == "invalid_type": proposal["findings"][0]["assertion_id"] = []
                else: proposal["findings"][0]["field"] = "finding"
                with self.assertRaises(ValidationError):
                    normalize_survey_review_envelope(proposal, current_map=target)

    def test_assertion_catalog_excludes_absent_claims_and_binds_relationship_text(self):
        current, _ = self.quote_location_fixture()
        current["entries"][0]["finding"]["text"] = None
        relation = {**relationship(), "artifact_ref": "artifact:kb/relationships/one@3"}
        current["relationships"] = [relation]; current["relationship_refs"] = [relation["artifact_ref"]]
        catalog = survey_review_response_contract(current)["assertion_catalog"]
        self.assertFalse(any(row["field"] == "finding" for row in catalog))
        selected = next(row for row in catalog if row["field"] == "claim")
        raw = {"checks": required_checks(SURVEY_CHECKS), "rationale": "Inspect the relation.", "findings": [
            {"check_id": "source-fidelity", "assertion_id": selected["assertion_id"], "rationale": "Adjudicate comparability."}]}
        raw["checks"][1]["outcome"] = "failed"
        bound = normalize_survey_review_envelope(raw, current_map=current)
        validate_survey_review(bound, current_map=current)
        self.assertEqual(bound["findings"][0]["quote"], relation["claim"]["text"])
        self.assertEqual(bound["findings"][0]["target_ref"], relation["artifact_ref"])

    def quote_location_fixture(self):
        row = entry()
        ref = "artifact:kb/work-analyses/W1@2"
        current = {"entries": [row], "entry_refs": {"W1": ref}, "relationships": [], "relationship_refs": []}
        value = {"checks": required_checks(SURVEY_CHECKS), "rationale": "Check the screening decision.",
                 "findings": [{"check_id": "map-support", "target_ref": ref, "field": "inclusion",
                               "quote": row["reason"], "rationale": "The inclusion rationale needs adjudication."}]}
        next(check for check in value["checks"] if check["check_id"] == "map-support")["outcome"] = "failed"
        return current, value

    def test_inclusion_criticism_binds_reason_without_changing_science_or_authority(self):
        current, raw = self.quote_location_fixture()
        original = deepcopy(raw)
        with self.assertRaises(ModelContractError):
            validate_survey_review(raw, current_map=current)
        value = normalize_survey_review_envelope(raw, current_map=current)
        validate_survey_review(value, current_map=current)
        self.assertEqual(value["findings"][0]["quote_field"], "reason")
        self.assertEqual(value["findings"][0]["field"], "inclusion")
        self.assertEqual(value["checks"], raw["checks"])
        self.assertEqual({key: item for key, item in value["findings"][0].items() if key != "quote_field"},
                         raw["findings"][0])
        self.assertEqual(raw, original)
        self.assertEqual(normalize_survey_review_envelope(value, current_map=current), value)

    def test_quote_location_rejects_ambiguous_stale_absent_null_and_explicit_wrong_bindings(self):
        for mutation in ("ambiguous", "stale", "source_only", "null", "wrong_explicit", "unknown_explicit"):
            with self.subTest(mutation=mutation):
                current, raw = self.quote_location_fixture()
                finding = raw["findings"][0]
                if mutation == "ambiguous":
                    current["entries"][0]["problem"]["text"] = finding["quote"]
                elif mutation == "stale":
                    finding["target_ref"] = finding["target_ref"].replace("@2", "@1")
                elif mutation == "source_only":
                    finding["quote"] = "Source-only assertion absent from the current map."
                elif mutation == "null":
                    current["entries"][0]["reason"] = None
                else:
                    finding["quote_field"] = "inclusion" if mutation == "wrong_explicit" else "unknown"
                value = normalize_survey_review_envelope(raw, current_map=current)
                with self.assertRaises(ModelContractError):
                    validate_survey_review(value, current_map=current)

    def test_explicit_cross_field_and_legacy_same_field_quotes_validate(self):
        current, raw = self.quote_location_fixture()
        raw["findings"][0]["quote_field"] = "reason"
        validate_survey_review(raw, current_map=current)
        raw["findings"][0].pop("quote_field")
        raw["findings"][0]["quote"] = "included"
        value = normalize_survey_review_envelope(raw, current_map=current)
        self.assertEqual(value["findings"][0]["quote_field"], "inclusion")
        validate_survey_review(value, current_map=current)

    def test_nested_findings_lift_without_changing_checks_quotes_or_repair_authority(self):
        current, raw = self.quote_location_fixture()
        finding = raw.pop("findings")[0]
        target = next(row for row in raw["checks"] if row["check_id"] == finding["check_id"])
        target["findings"] = [{key: item for key, item in finding.items() if key != "check_id"}]
        original = deepcopy(raw)
        value = normalize_survey_review_envelope(raw, current_map=current)
        validate_survey_review(value, current_map=current)
        self.assertEqual(value["findings"], [{**finding, "quote_field": "reason"}])
        self.assertEqual(value["checks"], [{key: item for key, item in row.items() if key != "findings"}
                                            for row in raw["checks"]])
        self.assertEqual(raw, original)
        self.assertEqual(normalize_survey_review_envelope(value, current_map=current), value)
        target["findings"][0]["check_id"] = "source-fidelity"
        with self.assertRaisesRegex(ModelContractError, "conflicts with its containing check"):
            normalize_survey_review_envelope(raw, current_map=current)

    def test_nested_findings_cannot_hide_passed_unknown_or_unbound_assertions(self):
        for mutation in ("passed", "extra_field", "stale", "invalid_list"):
            with self.subTest(mutation=mutation):
                current, raw = self.quote_location_fixture()
                finding = raw.pop("findings")[0]
                target = next(row for row in raw["checks"] if row["check_id"] == finding["check_id"])
                target["findings"] = [finding]
                if mutation == "passed": target["outcome"] = "passed"
                elif mutation == "extra_field": finding["unsupported"] = True
                elif mutation == "stale": finding["target_ref"] = finding["target_ref"].replace("@2", "@1")
                else: target["findings"] = None
                with self.assertRaises(ModelContractError):
                    validate_survey_review(normalize_survey_review_envelope(raw, current_map=current), current_map=current)

    def test_assignment_replay_migration_preserves_every_scientific_input_and_instruction(self):
        current, _ = self.quote_location_fixture()
        prior = {"phase": "survey_review", "map": current, "question": "A scoped question?",
                 "sources": [{"text": "Captured source."}], "instructions": (
                     "Return findings:[{check_id,target_ref,field,quote,rationale}]. "
                     "Identify its field and an exact substring quote from that field. Inspect every current assertion."),
                 "response_contract": survey_review_response_contract(current, legacy=True)}
        upgraded = {**prior, "response_contract": survey_review_response_contract(current),
                    "instructions": prior["instructions"].replace(
                        "findings:[{check_id,target_ref,field,quote,rationale}]",
                        "findings:[{check_id,target_ref,field,quote_field,quote,rationale}]").replace(
                        "its field and an exact substring quote from that field.",
                        "its affected field, quote_field, and an exact substring quote from quote_field on that same target.")
                        + " " + SURVEY_QUOTE_LOCATION_INSTRUCTION}
        self.assertEqual(survey_review_assignment_identity(prior), survey_review_assignment_identity(upgraded))
        for key, changed in (("question", "A different question?"), ("sources", []),
                             ("instructions", "Pass without examining sources.")):
            self.assertNotEqual(survey_review_assignment_identity(prior),
                                survey_review_assignment_identity({**upgraded, key: changed}))

    def test_invalid_survey_findings_report_every_current_target_without_changing_verdicts(self):
        row = entry()
        ref = "artifact:kb/work-analyses/W1@2"
        current = {"entries": [row], "entry_refs": {"W1": ref}, "relationships": []}
        value = {"checks": required_checks(SURVEY_CHECKS), "rationale": "Inspect captured evidence.",
                 "findings": []}
        for check in value["checks"]:
            if check["check_id"] != "coverage-accounting":
                check["outcome"] = "failed"
        for check_id, field, quote in (("source-fidelity", "finding", "A historical statement."),
                                       ("map-support", "reason", "A historical rationale."),
                                       ("map-support", "problem", "An absent assertion.")):
            value["findings"].append({"check_id": check_id, "target_ref": ref, "field": field,
                                      "quote": quote, "rationale": "Reassess source support."})
        original = deepcopy(value)
        with self.assertRaises(ModelContractError) as caught:
            validate_survey_review(value, current_map=current)
        message = str(caught.exception)
        for index, field in enumerate(("finding", "reason", "problem")):
            self.assertIn(f"findings[{index}].quote", message)
            self.assertIn(f"target_ref={ref!r}, field={field!r}", message)
            text = row[field]["text"] if isinstance(row[field], dict) else row[field]
            self.assertIn(f"current_field={text!r}", message)
        self.assertIn("map-support, source-fidelity", message)
        self.assertIn("do not replace a historical quotation with unrelated current text", message)
        self.assertEqual(value, original)

    def test_survey_finding_diagnostics_preserve_valid_negative_findings(self):
        row = entry()
        ref = "artifact:kb/work-analyses/W1@2"
        current = {"entries": [row], "entry_refs": {"W1": ref}, "relationships": []}
        value = {"checks": required_checks(SURVEY_CHECKS), "rationale": "Inspect captured evidence.",
                 "findings": []}
        value["checks"][1]["outcome"] = "failed"
        valid = {"check_id": "source-fidelity", "target_ref": ref, "field": "finding",
                 "quote": row["finding"]["text"], "rationale": "This assertion requires a narrower scope."}
        value["findings"] = [valid, {**valid, "target_ref": ref.replace("@2", "@1")},
                             {**valid, "field": "claim"}]
        with self.assertRaises(ModelContractError) as caught:
            validate_survey_review(value, current_map=current)
        message = str(caught.exception)
        self.assertIn("findings[1].target_ref", message)
        self.assertIn("findings[2].field", message)
        self.assertNotIn("negative scientific survey checks require", message)
        value["findings"] = [valid]
        validate_survey_review(value, current_map=current)
        self.assertEqual(value["checks"][1]["outcome"], "failed")

    def test_negative_survey_checks_bind_exact_current_assertions(self):
        row = entry()
        ref = "artifact:kb/work-analyses/W1@2"
        rel = {**relationship(), "artifact_ref": "artifact:kb/relationships/one@3"}
        current = {"entries": [row], "entry_refs": {"W1": ref}, "relationships": [rel]}
        value = {"checks": required_checks(SURVEY_CHECKS), "rationale": "Inspect captured evidence."}
        value["checks"][1]["outcome"] = "failed"
        with self.assertRaises(ModelContractError):
            validate_survey_review(value, current_map=current)
        finding = {"check_id": "source-fidelity", "target_ref": ref, "field": "reason",
                   "quote": row["reason"], "rationale": "The asserted scope is unsupported."}
        value["findings"] = [finding]
        validate_survey_review(value, current_map=current)
        for changes in ({"target_ref": ref.replace("@2", "@1")},
                        {"quote": "An absent hydration ablation claim."},
                        {"field": "inclusion", "quote": "excluded"},
                        {"target_ref": []}, {"field": "finding", "quote": row["reason"]},
                        {"check_id": "map-support"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                validate_survey_review({**value, "findings": [{**finding, **changes}]}, current_map=current)
        value["findings"] = [{**finding, "target_ref": rel["artifact_ref"], "field": "claim",
                              "quote": rel["claim"]["text"]}]
        validate_survey_review(value, current_map=current)

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
