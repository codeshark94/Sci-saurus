"""Frozen study obligations and current raw/verdict binding."""
from copy import deepcopy
import json
import unittest
import tempfile
from pathlib import Path
from contextlib import nullcontext, redirect_stdout
from io import StringIO
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.capability_foundry import candidate_prompt
from scisaurus.runtime.experiment import bind_deterministic_validation, validate_deterministic_validation
from scisaurus.runtime.measurement_contract import recalculation_outcomes
from scisaurus.runtime.program_admission import validate_program_candidate
from scisaurus.runtime.research_quality import build_research_design
from scisaurus.runtime.study_evidence import (
    EVIDENCE_KINDS, evidence_source_refs, study_evidence_contract, validate_evidence_plan,
)
from scisaurus.tests.test_program_foundry import candidate, output_document


def planned_intent():
    intent = candidate()["experiment_intent"]
    intent["evidence_plan"] = [{
        "id": kind, "kind": kind, "status": "planned",
        "metric_ids": ["tail_error"], "condition_ids": [kind],
        "source_refs": ["artifact:kb/reference@1"], "validator_check_id": "check_" + kind,
        "method": "Evaluate the declared diagnostic from the labelled observation rows.",
        "acceptance_rule": "The recorded error is finite and equals its independent recalculation.",
        "claim_limit": "This fixture establishes contract behavior only.",
    } for kind in sorted(EVIDENCE_KINDS)]
    return intent


def verdict(intent, failed=None):
    return {"schema_version": "experiment-validation-1", "study_id": intent["id"],
            "candidate_sha256": "a" * 64, "decision": "rejected" if failed else "accepted",
            "checks": [{"id": entry["validator_check_id"],
                        "outcome": "failed" if entry["kind"] == failed else "passed",
                        "evidence": "Current fixture rows independently checked."}
                       for entry in intent["evidence_plan"] if entry["status"] == "planned"],
            "metric_recalculations": [{"metric_id": "tail_error", "reported_value": 1.0,
                                        "recalculated_value": 1.0, "tolerance": 0, "matches": True}],
            "limitations": []}


def document():
    result = output_document()
    result["observations"] = [{"replicate": 1, "condition": kind, "abs_error": 1.0}
                              for kind in sorted(EVIDENCE_KINDS)]
    result["limitations"].append(planned_intent()["evidence_plan"][0]["claim_limit"])
    return result


class StudyEvidenceTests(unittest.TestCase):
    def test_legacy_intent_remains_byte_equivalent(self):
        intent = candidate()["experiment_intent"]
        before = json.dumps(intent, sort_keys=True)
        self.assertEqual(validate_evidence_plan(intent), [])
        recalculation_outcomes(intent)
        self.assertEqual(json.dumps(intent, sort_keys=True), before)
        with self.assertRaisesRegex(ValidationError, "requires"):
            validate_evidence_plan(intent, required=True)

    def test_intent_and_frozen_design_preserve_complete_plan(self):
        intent = planned_intent()
        value = candidate();value["experiment_intent"] = intent
        value["test_vector"]["input"]["configured_input"] = {
            "scientific_software": {"selection": {"scientific_source_refs": ["artifact:kb/reference@1"]}}}
        validate_program_candidate(value)
        design = build_research_design(intent)
        self.assertEqual(design["evidence_plan"], intent["evidence_plan"])
        design["evidence_plan"][0]["method"] = "Changed copy"
        self.assertNotEqual(design["evidence_plan"], intent["evidence_plan"])

    def test_plan_schema_rejects_missing_duplicate_and_unknown_bindings(self):
        mutations = {
            "missing_kind": lambda p: p.pop(),
            "duplicate_id": lambda p: p[1].update(id=p[0]["id"]),
            "duplicate_check": lambda p: p[1].update(validator_check_id=p[0]["validator_check_id"]),
            "unknown_metric": lambda p: p[0].update(metric_ids=["invented"]),
            "empty_metric": lambda p: p[0].update(metric_ids=[]),
            "duplicate_condition": lambda p: p[0].update(condition_ids=["a", "a"]),
            "missing_source": lambda p: p[0].update(source_refs=[]),
            "empty_limit": lambda p: p[0].update(claim_limit=" "),
            "invalid_status": lambda p: p[0].update(status="completed"),
            "extra_result": lambda p: p[0].update(result=1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                intent = planned_intent();mutate(intent["evidence_plan"])
                with self.assertRaises(ValidationError):
                    recalculation_outcomes(intent)

    def test_current_raw_and_current_verdict_bind_all_planned_obligations(self):
        intent = planned_intent();v = verdict(intent)
        validate_deterministic_validation(v, intent, "a" * 64)
        self.assertEqual(bind_deterministic_validation(v, document(), intent), v)

    def test_metric_agreement_cannot_replace_missing_independent_evidence_check(self):
        intent = planned_intent();v = verdict(intent);v["checks"].pop()
        with self.assertRaisesRegex(ValidationError, "omitted planned evidence"):
            validate_deterministic_validation(v, intent, "a" * 64)

    def test_current_failed_check_remains_rejected(self):
        intent = planned_intent();v = verdict(intent, failed="uncertainty")
        self.assertEqual(validate_deterministic_validation(v, intent, "a" * 64)["decision"], "rejected")
        v["decision"] = "accepted"
        with self.assertRaisesRegex(ValidationError, "contradicts"):
            validate_deterministic_validation(v, intent, "a" * 64)

    def test_analysis_labels_do_not_substitute_for_comparison_rows(self):
        intent = planned_intent();d = document()
        d["observations"] = [row for row in d["observations"] if row["condition"] != "control"]
        d["analysis"] = {"controls": ["control was completed"]}
        with self.assertRaisesRegex(ValidationError, "no observation rows.*control"):
            bind_deterministic_validation(verdict(intent), d, intent)

    def test_unavailable_external_validation_remains_a_limitation(self):
        intent = planned_intent()
        entry = next(row for row in intent["evidence_plan"] if row["kind"] == "external_validation")
        entry.update(status="not_applicable", metric_ids=[], condition_ids=[], validator_check_id=None,
                     method="No compatible acquired observations.",
                     claim_limit="Synthetic prediction only; no empirical validation.")
        validate_evidence_plan(intent)
        d = document();v = verdict(intent)
        with self.assertRaisesRegex(ValidationError, "lost its claim limitation"):
            bind_deterministic_validation(v, d, intent)
        d["limitations"].append(entry["claim_limit"])
        with self.assertRaisesRegex(ValidationError, "non-applicability reason"):
            bind_deterministic_validation(v, d, intent)
        d["limitations"].append(entry["method"])
        bind_deterministic_validation(v, d, intent)
        entry["validator_check_id"] = "pretended_validation"
        with self.assertRaisesRegex(ValidationError, "must not claim"):
            validate_evidence_plan(intent)

    def test_new_author_requires_plan_but_complete_frozen_intent_is_not_migrated(self):
        brief = {"study_evidence_contract": study_evidence_contract()}
        prompt = candidate_prompt(json.dumps(brief), [], {}, required_intent={"research_question": "Question"})
        self.assertTrue(prompt["evidence_plan_required"])
        self.assertIn("evidence_plan", prompt["optional_intent_fields"])
        frozen = candidate()["experiment_intent"]
        prior = deepcopy(frozen)
        prompt = candidate_prompt(brief, [], {}, required_intent=frozen)
        self.assertFalse(prompt["evidence_plan_required"])
        self.assertEqual(frozen, prior)
        self.assertNotIn("evidence_plan", prompt["required_intent_fields"])
        partial = {key: frozen[key] for key in ("id", "revision", "method", "primary_outcomes")}
        self.assertTrue(candidate_prompt(brief, [], {}, required_intent=partial)["evidence_plan_required"])

    def test_claim_limits_are_retained_for_executed_obligations(self):
        intent = planned_intent();d = document();d["limitations"] = []
        with self.assertRaisesRegex(ValidationError, "lost its claim limitation"):
            bind_deterministic_validation(verdict(intent), d, intent)

    def test_acquired_source_catalog_binds_plan_and_excludes_failed_receipts(self):
        configured = {"scientific_software": {
            "selection": {"scientific_source_refs": ["artifact:kb/reference@1"]},
            "operations": [{"receipt_ref": "artifact:receipt@1", "outcome": "ok"}],
            "host_environment_checks": [{"receipt_ref": "artifact:failed@1", "outcome": "failed"}]},
            "source_data_manifest": {"datasets": [{"artifact_ref": "artifact:data@1"}]}}
        refs = evidence_source_refs(configured)
        self.assertEqual(refs, ["artifact:data@1", "artifact:kb/reference@1", "artifact:receipt@1"])
        validate_evidence_plan(planned_intent(), source_refs=refs)
        value = candidate();value["experiment_intent"] = planned_intent()
        value["test_vector"]["input"]["configured_input"] = configured
        validate_program_candidate(value)
        value["experiment_intent"]["evidence_plan"][0]["source_refs"] = ["artifact:invented@1"]
        with self.assertRaisesRegex(ValidationError, "acquired source catalog"):
            validate_program_candidate(value)
        for malformed in (None, [], {"scientific_software": []},
                {"scientific_software": {"selection": {"scientific_source_refs": [None]}}},
                {"scientific_software": {"operations": [False]}},
                {"source_data_manifest": {"datasets": [{}]}}):
            with self.subTest(malformed=malformed), self.assertRaises(ValidationError):
                evidence_source_refs(malformed)

    def test_software_projection_exposes_exact_acquired_baseline_receipts(self):
        from scisaurus.runtime.composer import ComposerRunner
        selected = [{"receipt_ref": "artifact:example@1", "outcome": "ok",
                     "action": {"operation": "example"}, "result": {}}]
        assessment = {"artifact_ref": "artifact:assessment@1", "review": {}, "evidence": {
            "selection": {"strategy": "reuse", "scientific_source_refs": ["artifact:paper@1"]},
            "selected_operations": selected,
            "discovery_and_diagnostics": [{"receipt_ref": "artifact:host@1", "outcome": "ok",
                "action": {"operation": "check_environment"}, "result": {}}]}}
        configured = {"scientific_software": ComposerRunner._scientific_software_projection(assessment)}
        self.assertEqual(evidence_source_refs(configured),
                         ["artifact:example@1", "artifact:host@1", "artifact:paper@1"])
        intent = planned_intent()
        for entry in intent["evidence_plan"]:
            entry["source_refs"] = ["artifact:example@1"]
        validate_evidence_plan(intent, source_refs=evidence_source_refs(configured))

    def test_empirical_condition_labels_preserve_source_values_and_row_identity(self):
        from scisaurus.runtime.capability_foundry import _validate_source_observation_binding
        manifest = {"schema_version": "source-data-manifest-1", "datasets": [{
            "artifact_ref": "artifact:data@1", "source_sha256": "a" * 64,
            "source_url": "https://example.org/data", "source_location": "table 1",
            "extraction_method": "explicit numeric table", "rows": [{"row_id": "row1", "values": {"x": 2}}]}]}
        configured = {"source_data_manifest": manifest}
        d = {"observations": [{"source_record_id": "row1", "source_values": {"x": 2},
                               "replicate": 1, "condition": "baseline"}]}
        _validate_source_observation_binding(d, configured)
        for field, value in (("condition", " "), ("source_values", {"x": 3}),
                              ("source_record_id", "foreign"), ("invented", 1)):
            changed = deepcopy(d);changed["observations"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                _validate_source_observation_binding(changed, configured)

    def test_methods_budget_preserves_whole_plan_or_refuses_packet(self):
        from scisaurus.runtime.specialists import build_repair_adjudication_prompt
        intent = planned_intent()
        intent["evidence_plan"] = [{**entry, "id": entry["id"] + "_" + str(index),
            "validator_check_id": entry["validator_check_id"] + "_" + str(index)}
            for index in range(5) for entry in intent["evidence_plan"]]
        packet = {"prior_foundry_work": {"last_attempt": {"experiment_intent": intent}}}
        for budget in (24000, 8000, 4000, 2000):
            with self.subTest(budget=budget):
                try:
                    prompt = build_repair_adjudication_prompt({"quota": {"max_input_tokens": budget}}, packet, [])
                except ValidationError:
                    continue
                self.assertEqual(json.loads(prompt)["repair_adjudication_packet"]["candidate_program"]
                                 ["experiment_intent"]["evidence_plan"], intent["evidence_plan"])

    def test_paused_terminal_result_is_serialized_without_invented_fields(self):
        from scisaurus.cli import _composer_result_summary
        result = {"status": "paused", "stop_reason": "operator_paused"}
        self.assertEqual(_composer_result_summary(result, "/tmp/report.json"),
                         {**result, "report": "/tmp/report.json"})
        full = {**result, "elapsed_seconds": 12, "stages": [], "interim_report_path": "/tmp/interim.json"}
        summary = _composer_result_summary(full, "/tmp/report.json")
        self.assertEqual(summary["elapsed_seconds"], 12)
        self.assertEqual(summary["interim_report"], "/tmp/interim.json")

    def test_cli_watch_pause_exits_without_terminal_result_key_error(self):
        from scisaurus.cli import main
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflow = ComposerWorkflowTests()._workflow(root)
            path = root / "workflow.json";path.write_text(json.dumps(workflow))
            stdout = StringIO()
            with patch("scisaurus.runtime.run_control.workflow_permission", return_value=nullcontext()), \
                    patch("scisaurus.runtime.composer.load_runtime_environment_files"), \
                    patch("scisaurus.runtime.composer_supervisor.supervise_composer",
                          return_value={"status": "paused", "stop_reason": "operator_paused"}), \
                    redirect_stdout(stdout):
                code = main(["run-composer", "--workflow", str(path), "--watch", "--resume"])
            self.assertEqual(code, 3)
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["stop_reason"], "operator_paused")
            self.assertNotIn("elapsed_seconds", result)

    def test_cache_migration_keeps_new_design_obligations_without_retrofitting_legacy(self):
        from scisaurus.runtime.model_work import ModelWorkBlocked
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests, StubClient
        fixtures = CapabilityFoundryTests()
        for required in (True, False):
            with self.subTest(required=required), tempfile.TemporaryDirectory() as directory:
                root = Path(directory);cache = fixtures._cache(root)
                try:
                    brief = json.dumps({"study_evidence_contract": study_evidence_contract()})
                    previous = fixtures._payload()
                    cache.put("previous-contract", {"status": "repairing", "attempts": 1,
                        "assignment": {"capability_brief": brief, "configured_input": {"probe": True},
                                       "evidence_plan_required": required},
                        "study_evidence_plan_required": required, "last_attempt": previous,
                        "feedback": "Retain the current program and repair the intent contract.", "requests": []})
                    author = StubClient({"updates": {"experiment_intent": {"method": previous["experiment_intent"]["method"]}}})
                    foundry = fixtures._foundry(root);foundry.max_attempts = 1
                    states = []
                    if required:
                        with self.assertRaises(ModelWorkBlocked):
                            foundry.generate(brief, client=author, work_cache=cache,
                                on_progress=lambda phase, state: states.append(deepcopy(state)))
                        self.assertTrue(states[-1]["study_evidence_plan_required"])
                        self.assertIn("requires experiment_intent.evidence_plan", states[-1]["error"])
                    else:
                        result = foundry.generate(brief, client=author, work_cache=cache,
                            on_progress=lambda phase, state: states.append(deepcopy(state)))
                        self.assertEqual(result["status"], "registered")
                        self.assertNotIn("evidence_plan", result["candidate"]["experiment_intent"])
                    self.assertEqual(author.calls, 1)
                finally:
                    fixtures.doCleanups()


if __name__ == "__main__":
    unittest.main()
