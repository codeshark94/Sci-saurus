import json
import tempfile
import unittest
from pathlib import Path

from scisaurus.runtime.failure_recovery import (
    build_failure_dossier, build_repair_request, classify_failure,
)
from scisaurus.runtime.capability_foundry import SourceDataUnavailable
from scisaurus.core.errors import ModelContractError, ProviderRateLimitError, QuotaExceededError, ValidationError
from scisaurus.runtime.experiment import ExperimentProgramOutputContractError
from scisaurus.runtime.model_work import ModelWorkBlocked
from scisaurus.runtime.models import ModelCallError


class FailureRecoveryTests(unittest.TestCase):
    def test_missing_empirical_input_is_same_topic_source_acquisition(self):
        error = SourceDataUnavailable("controller-supplied raw rows are unavailable")
        self.assertEqual(classify_failure("experiment", error),
                         "evidence_input_unavailable")
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 7, "project_dir": None},
            error=error,
            stage_result={"status": "blocked", "results_status": "not_executed"},
        )
        self.assertEqual(dossier["failure_class"], "evidence_input_unavailable")
        self.assertEqual(
            [item["operation"] for item in dossier["repair_commands"]],
            ["acquire_source_data"],
        )
        request = build_repair_request(
            dossier, stage_id="experiment", target_stage_id="survey",
            target_stage_kind="survey")
        self.assertEqual(request["kind"], "full_text_retrieval")
        self.assertEqual(request["owner"], "research.source-acquirer")
        self.assertEqual(request["target_stage_id"], "survey")
        self.assertEqual(request["target_stage_kind"], "survey")

    def test_survey_evidence_shape_failure_is_model_contract_not_scientific_rejection(self):
        error = ModelWorkBlocked(
            "survey output contract failed: required assessment fields are missing")
        self.assertEqual(classify_failure("survey", error), "model_contract")

    def test_explicit_analysis_output_contract_failure_is_not_scientific_failure(self):
        error = ModelWorkBlocked(
            "capability foundry did not admit a program: analysis comparison has an invalid shape")
        error.failure_class = "model_contract"
        self.assertEqual(classify_failure("experiment", error), "model_contract")

    def test_legacy_stage_seconds_shape_error_is_a_model_contract_failure(self):
        error = ModelWorkBlocked(
            "Unchanged experiment input failed: ModelWorkBlocked: capability foundry did not "
            "admit a program: stage_seconds requires exactly "
            "['setup', 'supervision', 'production', 'unit_review', 'integrated_review', 'reassessment']")
        self.assertEqual(classify_failure("experiment", error), "model_contract")

    def test_executor_output_shape_contract_routes_to_format_recovery(self):
        error = ExperimentProgramOutputContractError(
            required_fields=["observations", "metrics"],
            observed_fields=["metrics", "analysis"],
            missing_fields=["observations"],
            unexpected_fields=[],
        )
        self.assertEqual(classify_failure("experiment", error), "model_contract")
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 3, "project_dir": None},
            error=error,
            stage_result={"status": "blocked", "results_status": "not_executed"},
        )
        self.assertEqual(dossier["failure_class"], "model_contract")
        self.assertEqual(dossier["next_action"], "repair_model_contract_before_stage_retry")
        self.assertFalse(any("additional_experiment" in item.get("operation", "")
                             for item in dossier["repair_commands"]))

    def test_experiment_failure_becomes_code_and_result_repair_plan(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            (root / "output").mkdir()
            result_path = root / "output" / "run.json"
            result_path.write_text(json.dumps({
                "status": "blocked", "execution_refs": ["exec-1"],
                "metrics": [{"id": "onset", "value": 4.2}],
                "blockers": [{"reason": "independent recalculation rejected"}],
            }))
            dossier = build_failure_dossier(
                stage={"id": "experiment", "kind": "experiment"},
                attempt_stage={"attempt_number": 2, "project_dir": str(root)},
                error=ValidationError("independent recalculation rejected the result"),
                stage_result={
                    "status": "blocked", "output_path": str(result_path),
                    "execution_refs": ["exec-1"],
                    "results_package": str(result_path),
                },
                program_snapshot=[{
                    "path": str(root / "program.py"), "sha256": "abc", "source": "print(1)"
                }],
                specialist_reports=[{
                    "assigned_role": "methods.statistical-reviewer",
                    "response": {
                        "findings": ["The estimator is not independent of the executor."],
                        "requested_actions": ["Recompute the estimand with an independent implementation."],
                    },
                }],
            )
            self.assertEqual(dossier["failure_class"], "experiment_failure")
            self.assertEqual(dossier["attempt_number"], 2)
            self.assertTrue(dossier["recoverable"])
            operations = {item["operation"] for item in dossier["repair_commands"]}
            self.assertTrue({"inspect", "recalculate", "edit_program", "execute"}.issubset(operations))
            self.assertIn("results_package_snapshot", dossier["observed_result"])
            self.assertTrue(dossier["review_directives"])
            self.assertIn("independent implementation",
                          dossier["repair_commands"][1]["instruction"])

            request = build_repair_request(dossier, stage_id="experiment")
            self.assertEqual(request["kind"], "additional_experiment")
            self.assertEqual(request["owner"], "methods.validation")
            self.assertEqual(request["recovery_mode"], "repair_then_rerun")
            self.assertEqual(request["target_stage_id"], "experiment")
            self.assertEqual(request["target_stage_kind"], "experiment")
            self.assertEqual(request["repair_priority"], "immediate")
            self.assertTrue(request["repair_commands"])

    def test_provider_failure_does_not_become_scientific_repair(self):
        self.assertEqual(
            classify_failure("experiment", ValidationError("provider cooldown 429")),
            "resource_fence",
        )
        self.assertEqual(
            classify_failure("survey", ValidationError("run requires a new project directory")),
            "operational_recovery",
        )

    def test_provider_rate_limit_remains_a_resource_fence_across_serialization(self):
        details = {"outcome": "rate_limited", "metadata": {"http_status": 429}}
        error = ProviderRateLimitError("source result package unavailable", provider="publisher", details=details)
        details["metadata"]["http_status"] = 403
        self.assertEqual(error.details["metadata"]["http_status"], 429)
        for value, result in (
            (error, None),
            (ValidationError("source result package unavailable"), {"failure": {"kind": "provider_rate_limit"}}),
        ):
            with self.subTest(error_type=type(value).__name__):
                self.assertEqual(classify_failure("experiment", value, result), "resource_fence")
                dossier = build_failure_dossier(
                    stage={"id": "survey", "kind": "survey"},
                    attempt_stage={"attempt_number": 3, "project_dir": None},
                    error=value, stage_result=result,
                )
                self.assertFalse(dossier["recoverable"])
                self.assertEqual([item["operation"] for item in dossier["repair_commands"]], ["reconcile"])

    def test_python_runtime_exception_is_a_nonrecoverable_harness_bug(self):
        error = AttributeError("'NoneType' object has no attribute 'get'")
        error.runtime_frames = [{"file": "composer.py", "line": 9012, "function": "run"}]
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 488, "project_dir": None},
            error=error,
        )

        self.assertEqual(dossier["failure_class"], "harness_bug")
        self.assertFalse(dossier["recoverable"])
        self.assertEqual(dossier["next_action"], "patch_harness_before_resume")
        self.assertEqual(dossier["model_diagnostics"]["runtime_frames"], error.runtime_frames)
        self.assertEqual(
            [item["operation"] for item in dossier["repair_commands"]], ["patch_runtime"])

    def test_resource_dossier_is_resume_only(self):
        dossier = build_failure_dossier(
            stage={"id": "survey", "kind": "survey"},
            attempt_stage={"attempt_number": 3, "project_dir": None},
            error=ValidationError("provider cooldown 429"),
        )
        self.assertEqual(dossier["failure_class"], "resource_fence")
        self.assertFalse(dossier["recoverable"])
        self.assertEqual(dossier["next_action"], "resume_from_checkpoint")
        self.assertEqual(
            [item["operation"] for item in dossier["repair_commands"]], ["reconcile"])

    def test_model_response_contract_does_not_open_scientific_recovery(self):
        error = ValidationError("research argument review did not finish normally: length")
        self.assertEqual(classify_failure("argument", error), "model_contract")
        dossier = build_failure_dossier(
            stage={"id": "argument", "kind": "argument"},
            attempt_stage={"attempt_number": 4, "project_dir": None},
            error=error,
        )
        self.assertEqual(dossier["failure_class"], "model_contract")
        self.assertEqual(dossier["next_action"],
                         "repair_model_contract_before_stage_retry")
        self.assertEqual(
            [item["operation"] for item in dossier["repair_commands"]],
            ["reroute_and_compact"],
        )

    def test_typed_model_contract_precedes_experiment_text_markers(self):
        error = ModelContractError(
            "experiment result package response contract was incomplete")
        self.assertEqual(classify_failure("experiment", error), "model_contract")

    def test_local_topic_intake_exhaustion_is_not_a_provider_quota_fence(self):
        cases = (
            ("scientific_candidate_rejected", "scientific_review"),
            ("intake_contract_failure", "model_contract"),
            ("refinement_contract_failure", "topic_refinement_contract"),
        )
        for retry_reason, expected_class in cases:
            with self.subTest(retry_reason=retry_reason):
                error = QuotaExceededError(
                    "topic discovery bounded intake exhausted after 6 candidate attempts",
                    dimension="topic_attempts", limit=6, observed=6,
                    usage={"model_calls": 7},
                )
                error.topic_budget_scope = "intake"
                error.retryable_topic_intake = True
                error.topic_retry_reason = retry_reason
                self.assertEqual(
                    classify_failure("topic_discovery", error), expected_class)
                dossier = build_failure_dossier(
                    stage={"id": "topic", "kind": "topic_discovery"},
                    attempt_stage={"attempt_number": 3, "project_dir": None},
                    error=error,
                )
                self.assertEqual(dossier["failure_class"], expected_class)
                self.assertTrue(dossier["recoverable"])
                self.assertNotEqual(dossier["next_action"], "resume_from_checkpoint")
                if retry_reason == "refinement_contract_failure":
                    self.assertEqual(
                        [item["operation"] for item in dossier["repair_commands"]],
                        ["refine"],
                    )

    def test_experiment_program_author_truncation_is_model_contract_repair(self):
        error = ValidationError(
            "capability foundry did not admit a program: "
            "program author did not finish normally"
        )
        self.assertEqual(classify_failure("experiment", error), "model_contract")
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 5, "project_dir": None},
            error=error,
        )
        self.assertEqual(dossier["failure_class"], "model_contract")
        self.assertEqual(dossier["next_action"],
                         "repair_model_contract_before_stage_retry")
        self.assertEqual(
            [item["operation"] for item in dossier["repair_commands"]],
            ["reroute_and_compact"],
        )

    def test_zero_observation_failure_overrides_stale_format_label(self):
        error = ModelWorkBlocked(
            "Unchanged experiment input failed 1 time(s): capability foundry did not admit a "
            "program: experiment emitted 0 observation rows, fewer than configured minimum "
            "run_count=1000 rows"
        )
        error.failure_class = "model_contract"

        self.assertEqual(classify_failure("experiment", error), "experiment_failure")
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 464, "project_dir": None},
            error=error,
            stage_result={"status": "blocked", "results_status": "not_executed"},
        )
        self.assertEqual(dossier["failure_class"], "experiment_failure")
        self.assertEqual(
            dossier["next_action"],
            "create_scoped_repair_work_order_and_repair_before_rerun",
        )
        self.assertEqual(
            {item["operation"] for item in dossier["repair_commands"]},
            {"inspect", "recalculate", "edit_program", "execute"},
        )

    def test_provider_429_precedes_experiment_output_failure_marker(self):
        error = ModelCallError(
            "experiment emitted 0 observation rows, fewer than configured minimum run_count=1000 rows",
            status_code=429,
        )
        error.failure_class = "model_contract"
        self.assertEqual(classify_failure("experiment", error), "resource_fence")

    def test_model_diagnostics_survive_failure_dossier_boundary(self):
        error = ValidationError("program author response was incomplete (finish_reason=length)")
        error.model_diagnostics = {
            "author_responses": [{
                "attempt": 2, "finish_reason": "length",
                "outcome": "incomplete_response",
            }],
        }
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 2, "project_dir": None},
            error=error,
        )
        self.assertEqual(
            dossier["model_diagnostics"]["model_diagnostics"]["author_responses"][0]["finish_reason"],
            "length",
        )

    def test_sandbox_executor_failure_is_not_misclassified_by_timeout_false(self):
        error = ValidationError(
            "capability foundry did not admit a program: executor failed in the sandbox "
            "(status=-9, timeout=False, truncated=True):"
        )
        self.assertEqual(classify_failure("experiment", error), "experiment_failure")
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 6, "project_dir": None},
            error=error,
        )
        self.assertEqual(dossier["failure_class"], "experiment_failure")
        self.assertIn(
            "edit_program",
            {item["operation"] for item in dossier["repair_commands"]},
        )

    def test_adversarial_experiment_rejection_with_model_outputs_is_scientific_failure(self):
        error = ValidationError(
            "The observations are model outputs, not measurements; do not claim an empirical result."
        )
        error.failure_class = "experiment_capability_repair"
        error.repair_gate = "adversarial_review"
        error.repair_feedback = {
            "gate": "adversarial_review",
            "feedback": "Model outputs do not support the stated empirical claim.",
        }
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 181, "project_dir": None},
            error=error,
        )
        self.assertEqual(dossier["failure_class"], "experiment_failure")
        self.assertEqual(
            dossier["next_action"],
            "create_scoped_repair_work_order_and_repair_before_rerun",
        )
        self.assertEqual(
            {item["operation"] for item in dossier["repair_commands"]},
            {"inspect", "recalculate", "edit_program", "execute"},
        )
        self.assertEqual(
            dossier["model_diagnostics"]["repair_gate"], "adversarial_review")
        self.assertEqual(
            classify_failure(
                "experiment",
                ValidationError("The observations are model outputs, not measurements.")),
            "experiment_contract",
        )

    def test_argument_review_verdict_survives_into_repair_order(self):
        error = ValidationError("research argument adjudication requires revision")
        error.research_argument = {"primary_argument": {"thesis": "bounded claim"}}
        error.research_review = {
            "decision": "revise",
            "required_repairs": [{
                "id": "mechanism",
                "target": "primary_argument",
                "problem": "causal step is unsupported",
                "repair": "downgrade to an association and add the discriminating test",
                "verification": "claim links to the result and test",
            }],
        }
        dossier = build_failure_dossier(
            stage={"id": "argument", "kind": "argument"},
            attempt_stage={"attempt_number": 1, "project_dir": None},
            error=error,
        )
        request = build_repair_request(dossier, stage_id="argument")
        self.assertTrue(dossier["model_diagnostics"]["research_review"])
        self.assertTrue(any("downgrade to an association" in item["text"]
                            for item in dossier["review_directives"]))
        self.assertIn("downgrade to an association", request["objective"])
        self.assertEqual(request["target_stage_id"], "argument")
        self.assertEqual(request["target_stage_kind"], "argument")

    def test_failed_review_check_is_hypothesis_not_authoritative_repair(self):
        error = ValidationError("independent recalculation found a null mismatch")
        error.research_review = {
            "decision": "revise",
            "checks": [{
                "check_id": "primary-metrics-from-observations",
                "outcome": "failed",
                "evidence": "All primary metrics recalculated from observations agree within tolerance.",
            }],
            "required_repairs": [{
                "target": "control-null",
                "problem": "reported numeric zero but independent result is null",
                "repair": "Recalculate control-null from explicit condition-labelled observations.",
            }],
        }
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 470, "project_dir": None},
            error=error,
        )

        check_directive = next(
            item for item in dossier["review_directives"]
            if item["kind"] == "review_claim_requires_independent_verification"
        )
        repair_proposal = next(
            item for item in dossier["review_directives"]
            if "Recalculate control-null" in item["text"]
        )
        self.assertIn("not an independently confirmed defect", check_directive["text"])
        self.assertIn("agree within tolerance", check_directive["text"])
        self.assertEqual(repair_proposal["kind"], "reviewer_proposal_to_verify")
        review_command = next(
            item for item in dossier["repair_commands"]
            if item["id"] == "execute-review-directed-repairs"
        )
        self.assertIn("hypotheses, not established facts", review_command["instruction"])

    def test_failure_dossier_does_not_promote_stale_attempt_reviews_to_repairs(self):
        reports = [{
            "role_id": "methodologist",
            "failure_lineage": {
                "failure_dossier_ref": (
                    "artifact:command/failure-recovery/experiment/attempt-278@1"),
                "stage_attempt_number": 278,
                "failure_input_sha256": "a" * 64,
            },
            "response": {
                "findings": ["The attempt-278 result is underpowered."],
                "requested_actions": ["Rerun attempt 278."],
            },
        }, {
            "role_id": "reproducibility-reviewer",
            "failure_lineage": {
                "stage_attempt_number": 282,
                "failure_input_sha256": "c" * 64,
            },
            "response": {
                "findings": ["The current executor is deterministic."],
                "requested_actions": ["Recalculate attempt-278 before continuing."],
            },
        }, {
            "role_id": "statistical-reviewer",
            "response": {
                "findings": ["The current estimator needs a sensitivity check."],
                "requested_actions": ["Add the sensitivity analysis to this attempt."],
            },
        }]
        error = ValidationError("independent recalculation failed")
        error.research_review = {
            "failure_lineage": {
                "stage_id": "argument",
                "stage_attempt_number": 282,
            },
            "required_repairs": ["Carry over the stale argument attempt-282 review."],
        }
        dossier = build_failure_dossier(
            stage={"id": "experiment", "kind": "experiment"},
            attempt_stage={"attempt_number": 282, "project_dir": None},
            error=error,
            specialist_reports=reports,
            attempt_number=282,
        )

        directive_text = " ".join(item["text"] for item in dossier["review_directives"])
        self.assertIn("sensitivity check", directive_text)
        self.assertNotIn("attempt-278", directive_text)
        self.assertNotIn("Rerun attempt 278", directive_text)
        self.assertNotIn("stale argument attempt-282", directive_text)
        self.assertEqual(len(dossier["specialist_reports"]), 3)
        provenance = dossier["repair_directive_provenance"]
        self.assertEqual(provenance["excluded_cross_lineage_specialist_report_count"], 2)
        self.assertTrue(provenance["excluded_cross_lineage_diagnostic_review"])


if __name__ == "__main__":
    unittest.main()
