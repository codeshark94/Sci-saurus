import json
import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from scisaurus.cli import _composer_progress_line
from scisaurus.runtime.capability_foundry import SYSTEM, candidate_prompt
from scisaurus.runtime.composer import (
    CAPABILITY_REPAIR_SOURCE_CHARS, LEGACY_EXPERIMENT_SURVEY_ADMISSION_ERROR,
    ComposerRunner,
    read_interim_report, validate_workflow,
)
from scisaurus.runtime.departments import default_organization
from scisaurus.runtime.literature import ProviderCooldownError
from scisaurus.runtime.model_work import ModelWorkBlocked
from scisaurus.runtime.models import ModelResult, estimate_input_tokens
from scisaurus.runtime.specialists import (
    VERIFIER_SYSTEM, build_specialist_prompt, redact_sensitive_text,
)
from scisaurus.core.errors import QuotaExceededError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes, parse_ref, safe_artifact_component
from scisaurus.core.store import ArtifactStore
from scisaurus.tests.test_research_program import topic_package


class _ComposerTestSpecialistClient:
    def __init__(self, **config):
        self.config = config

    def complete(self, *, system, prompt, images=None):
        if system == VERIFIER_SYSTEM:
            body = {
                "decision": "accept", "rationale": "The bounded test evidence is sufficient.",
                "critical_findings": [], "repair_scope": [],
            }
        else:
            body = {
                "decision": "pass", "summary": "The assigned bounded check passed.",
                "findings": [], "evidence_gaps": [], "requested_actions": [],
            }
        return ModelResult(
            text=json.dumps(body), model=self.config.get("model", "test-specialist"),
            usage={"model_calls": 1, "input_tokens": 1, "output_tokens": 1},
            elapsed_seconds=0.001, finish_reason="stop",
        )


class ComposerWorkflowTests(unittest.TestCase):
    def test_provider_cooldown_route_ids_publish_through_safe_artifact_components(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            cooldown_key = "recovery-strategy.argument-reviewer-1-gemma4:31b-cloud"
            component = safe_artifact_component(cooldown_key)

            class Dispatcher:
                model_config = {"model": "test-model"}
                provider_cooldowns = {cooldown_key: time.monotonic() + 60}

                def cooldown_keys(self):
                    return {cooldown_key}

                def dispatch(self, assignments, packet, *, verifier, on_result):
                    report = {"role_id": assignments[0]["role_id"],
                              "status": "succeeded", "usage": {}}
                    on_result(report)
                    return [report]

            try:
                result = runner._dispatch_specialist_work(
                    Dispatcher(), [{
                        "assigned_role": "strategy.argument-reviewer",
                        "role_id": "argument-reviewer",
                        "stage_id": "interpretation",
                        "_prompt": "bounded provider cooldown test",
                    }], {},
                )
                self.assertEqual(result[0]["status"], "succeeded")
                record = runner.store.head(
                    f"command/provider-cooldowns/{component}")
                self.assertIsNotNone(record)
                parse_ref(record["artifact_ref"])
                body = json.loads(runner.store.read_body(record["body_hash"]))
                self.assertEqual(body["cooldown_key"], cooldown_key)
                self.assertEqual(
                    safe_artifact_component("ollama-deepseek"),
                    "ollama-deepseek",
                )
                self.assertNotEqual(
                    component,
                    safe_artifact_component(
                        "recovery-strategy.argument-reviewer-1-gemma4-31b-cloud"),
                )
            finally:
                runner.close()

    def test_interrupted_runner_result_propagates_process_stop_not_stage_retry(self):
        with self.assertRaises(KeyboardInterrupt):
            ComposerRunner._raise_stage_failure({"status": "paused", "error": "termination requested",
                                                 "failure": {"kind": "process_interrupted"}})

    def test_non_admissible_stage_result_is_carried_with_the_failure(self):
        result = {"status": "blocked", "error": "independent review rejected the result",
                  "execution_refs": ["execution-1"], "metrics": [{"id": "x", "value": 1.0}]}
        with self.assertRaises(ValidationError) as raised:
            ComposerRunner._raise_stage_failure(result)
        self.assertEqual(raised.exception.stage_result, result)
        self.assertEqual(raised.exception.usage, {})

    def test_argument_review_diagnostics_are_visible_to_failure_specialists(self):
        error = ValidationError("research argument adjudication requires revision")
        error.research_argument = {
            "schema_version": "research-argument-1",
            "observed_patterns": [{"id": "pattern-1", "evidence_ids": ["e1"]}],
            "hypotheses": [],
            "limitations": ["The mechanism is not calibrated."],
        }
        error.research_review = {
            "schema_version": "research-argument-review-1",
            "decision": "revise",
            "checks": [],
            "required_repairs": [{"id": "calibration", "repair": "Run a sensitivity sweep."}],
            "rationale": "The comparator is uncalibrated.",
        }
        error.research_review_argument_sha256 = "paired-candidate-digest"
        result = ComposerRunner._failure_stage_result(
            {"id": "argument", "kind": "argument"}, error)
        self.assertIn("argument_package", result)
        self.assertEqual(result["argument_package"]["review"]["decision"], "revise")
        self.assertEqual(result["argument"]["observed_patterns"][0]["id"], "pattern-1")
        self.assertEqual(result["research_review_argument_sha256"],
                         "paired-candidate-digest")

    def test_repair_order_prevents_forward_handoff_until_rerun(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                runner.context["experiment"] = {
                    "kind": "experiment",
                    "status": "research_expansion_required",
                    "results_status": "observed",
                    "failure_recovery": {"recovery_mode": "repair_then_rerun"},
                    "research_requests": [{"id": "repair-experiment"}],
                }
                runner.workflow["progression_policy"] = "forward_first"
                self.assertFalse(runner._composer_can_advance_after_admission(
                    runner.workflow["stages"][1], ModelWorkBlocked("scientific review hold")))
            finally:
                runner.close()

    def test_capability_repair_order_blocks_forward_without_observed_projection(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                runner.context["experiment"] = {
                    "kind": "experiment",
                    "status": "research_expansion_required",
                    "review_status": "scientific_assignment_blocked",
                    "error": "capability foundry adversarial review rejected the program",
                    "failure_recovery": {"recovery_mode": "repair_then_rerun"},
                    "research_requests": [{
                        "id": "repair-experiment",
                        "kind": "additional_experiment",
                        "owner": "methods.validation",
                    }],
                }
                runner.workflow["progression_policy"] = "forward_first"
                stage = runner.workflow["stages"][1]
                # The failed authoring/admission path has no observed result
                # to project. It must still execute the explicit Methods order
                # before a downstream interpretation can be admitted.
                self.assertFalse(runner._composer_can_advance_after_admission(
                    stage, ModelWorkBlocked("capability repair exhausted")))
            finally:
                runner.close()

    def test_completed_capability_repair_panel_suppresses_duplicate_failure_review(self):
        completed_panel = {
            "status": "completed",
            "reports": [{"status": "succeeded"}],
            "verifier": {"status": "succeeded"},
        }
        self.assertTrue(ComposerRunner._capability_repair_panel_completed(completed_panel))
        self.assertFalse(ComposerRunner._capability_repair_panel_completed({
            **completed_panel, "verifier": {"status": "failed"},
        }))
        self.assertFalse(ComposerRunner._capability_repair_panel_completed({
            **completed_panel, "reports": [{"status": "failed"}],
        }))

        ordinary_failure = ValidationError("unreviewed producer failure")
        self.assertTrue(ComposerRunner._should_run_failure_specialist_review(ordinary_failure))
        reviewed_failure = ModelWorkBlocked("capability admission failed")
        reviewed_failure.capability_repair_panel_completed = True
        self.assertFalse(ComposerRunner._should_run_failure_specialist_review(reviewed_failure))

    def test_run_stage_preserves_completed_repair_panel_failure_marker(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                failure = ModelWorkBlocked("capability admission failed")
                failure.capability_repair_panel_completed = True

                def fail(*_args, **_kwargs):
                    raise failure

                runner._execute_stage = fail
                with self.assertRaises(ModelWorkBlocked) as raised:
                    runner._run_stage(runner.workflow["stages"][1])
                self.assertTrue(raised.exception.capability_repair_panel_completed)
            finally:
                runner.close()

    def test_forward_progress_does_not_erase_actionable_repair_order(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                stage = runner.workflow["stages"][1]
                context = {
                    "kind": "experiment",
                    "status": "research_expansion_required",
                    "failure_recovery": {"recovery_mode": "repair_then_rerun"},
                    "research_requests": [{
                        "id": "repair-experiment",
                        "kind": "additional_experiment",
                        "owner": "methods.validation",
                    }],
                }
                result = runner._materialize_forward_progress(
                    stage,
                    {"project_dir": stage["project_dir"]},
                    ModelWorkBlocked("capability repair exhausted"),
                    context,
                    {"reports": []},
                    [],
                    force_advance=True,
                )
                self.assertIsNone(result)
            finally:
                runner.close()

    def test_model_contract_failure_cannot_forward_an_unexecuted_experiment(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                stage = runner.workflow["stages"][1]
                runner.workflow["progression_policy"] = "forward_first"
                context = {
                    "kind": "experiment",
                    "status": "candidate_needs_review",
                    "results_status": "not_executed",
                    "review_status": "model_contract_repair",
                    "format_recovery": True,
                    "format_recovery_attempts": 1,
                    "failure_recovery": {
                        "failure_class": "model_contract",
                        "recovery_mode": "format_repair_then_rerun",
                    },
                }
                runner.context["experiment"] = context
                runner.stage_records["experiment"] = {
                    "kind": "experiment", "status": "candidate_needs_review",
                    "attempt_count": 80, "forward_progress": True,
                    "composer_decision": "advance_with_findings",
                }
                error = ModelWorkBlocked("capability foundry did not admit a program: model output must contain valid JSON")
                self.assertFalse(runner._composer_can_advance_after_admission(stage, error))
                self.assertIsNone(runner._materialize_forward_progress(
                    stage, {"project_dir": stage["project_dir"]}, error, context,
                    {"reports": []}, [], force_advance=True))
                held = runner._hold_unexecuted_experiment(
                    stage, context, reason=str(error))
                self.assertEqual(held["status"], "research_expansion_required")
                self.assertTrue(held["release_blocking"])
                self.assertEqual(held["results_status"], "not_executed")
                self.assertEqual(held["research_requests"][0]["kind"], "recovery")
            finally:
                runner.close()

    def test_scoped_experiment_repair_cannot_be_forwarded_or_lost_on_resume(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                stage = runner.workflow["stages"][1]
                runner.workflow["progression_policy"] = "forward_first"
                request = {
                    "id": "repair_rejected_experiment_result",
                    "kind": "additional_experiment",
                    "owner": "methods.validation",
                    "objective": "Repair the current direction or narrow its claim.",
                    "why": "Independent review rejected the package.",
                    "success_condition": "Fresh evidence resolves the blocking finding.",
                    "evidence_needed": "Review findings and retained raw observations.",
                }
                context = {
                    "kind": "experiment", "status": "candidate_needs_review",
                    "results_status": "executed",
                    "research_expansion_requests": [request],
                }
                record = {
                    "kind": "experiment", "status": "candidate_needs_review",
                    "forward_progress": True, "composer_decision": "advance_with_findings",
                    "release_blocking": False, "attempt_count": 1,
                }
                self.assertTrue(ComposerRunner._has_scoped_research_work(context))
                self.assertFalse(runner._should_authorize_forward_hold(
                    "research_expansion_required", context))
                self.assertTrue(runner._should_authorize_forward_hold(
                    "research_expansion_required", {"status": "research_expansion_required"}))
                self.assertFalse(runner._can_migrate_forward_candidate(record, context))

                runner.context["experiment"] = context
                runner.stage_records["experiment"] = record
                by_id = {item["id"]: item for item in runner.workflow["stages"]}
                reconciled = runner._reconcile_stale_forward_handoffs(by_id)

                self.assertEqual([item["stage_id"] for item in reconciled], ["experiment"])
                self.assertEqual(runner.stage_records["experiment"]["status"], "retrying")
                repaired_context = runner.context["experiment"]
                self.assertEqual(repaired_context["status"], "research_expansion_required")
                self.assertEqual(repaired_context["research_requests"], [request])
                self.assertEqual(repaired_context["research_expansion_requests"], [])
                self.assertTrue(repaired_context["release_blocking"])
            finally:
                runner.close()

    def test_forward_progress_uses_durable_recovery_over_stale_runner_packet(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                stage = runner.workflow["stages"][1]
                runner.context["experiment"] = {
                    "kind": "experiment",
                    "status": "format_recovery_required",
                    "results_status": "not_executed",
                    "review_status": "model_contract_repair",
                    "format_recovery": True,
                    "failure_recovery": {
                        "failure_class": "model_contract",
                        "recovery_mode": "format_repair_then_rerun",
                    },
                }
                stale_runner_packet = {
                    "kind": "experiment",
                    "status": "blocked",
                    "results_status": "not_executed",
                }
                result = runner._materialize_forward_progress(
                    stage,
                    {"project_dir": stage["project_dir"]},
                    ModelWorkBlocked("program author did not finish normally"),
                    stale_runner_packet,
                    {"reports": []},
                    [],
                    force_advance=True,
                )
                self.assertIsNone(result)
            finally:
                runner.close()

    def test_resume_reconciles_legacy_forwarded_experiment_before_dependency_admission(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                stage = runner.workflow["stages"][1]
                runner.stage_records["experiment"] = {
                    "kind": "experiment",
                    "status": "candidate_needs_review",
                    "composer_decision": "advance_with_findings",
                    "forward_progress": True,
                    "release_blocking": False,
                    "attempt_count": 4,
                }
                runner.context["experiment"] = {
                    "kind": "experiment",
                    "status": "candidate_needs_review",
                    "composer_decision": "advance_with_findings",
                    "results_status": "not_executed",
                    "failure_recovery": {
                        "failure_class": "experiment_failure",
                        "recovery_mode": "repair_then_rerun",
                        "dossier_ref": "artifact:failure@1",
                        "input_sha256": "a" * 64,
                        "repair_commands": [{"id": "repair", "instruction": "fix the program"}],
                        "acceptance_checks": ["fresh independent check"],
                    },
                    "error": "capability authoring failed after the prior handoff",
                    "failure_dossier_ref": "artifact:failure@1",
                    "repair_commands": [{"id": "repair", "instruction": "fix the program"}],
                    "acceptance_checks": ["fresh independent check"],
                }
                by_id = {item["id"]: item for item in runner.workflow["stages"]}
                reconciled = runner._reconcile_stale_forward_handoffs(by_id)
                self.assertEqual([item["stage_id"] for item in reconciled], ["experiment"])
                self.assertEqual(runner.stage_records["experiment"]["status"], "retrying")
                context = runner.context["experiment"]
                self.assertEqual(context["status"], "research_expansion_required")
                self.assertTrue(context["research_requests"])
                self.assertEqual(context["research_requests"][0]["kind"], "additional_experiment")
                self.assertEqual(context["research_requests"][0]["target_stage_id"], "experiment")
                self.assertIn("experiment", runner.continuation_pending_stage_ids)
            finally:
                runner.close()

    def test_resume_migrates_legacy_program_author_contract_to_format_repair(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                stage = runner.workflow["stages"][1]
                runner.stage_records["experiment"] = {
                    "kind": "experiment",
                    "status": "candidate_needs_review",
                    "composer_decision": "advance_with_findings",
                    "release_blocking": False,
                    "attempt_count": 88,
                }
                runner.context["experiment"] = {
                    "kind": "experiment",
                    "status": "candidate_needs_review",
                    "results_status": "not_executed",
                    "release_blocking": False,
                    "error": (
                        "Unchanged experiment input failed: ModelWorkBlocked: "
                        "capability foundry did not admit a program: "
                        "program author did not finish normally"
                    ),
                    "failure_dossier_ref": "artifact:failure@legacy",
                    "failure_recovery": {
                        "failure_class": "model_contract",
                        "recovery_mode": "format_repair_then_rerun",
                        "dossier_ref": "artifact:failure@legacy",
                        "input_sha256": "b" * 64,
                    },
                    "format_recovery": True,
                    "format_recovery_attempts": 1,
                }
                by_id = {item["id"]: item for item in runner.workflow["stages"]}
                reconciled = runner._reconcile_stale_forward_handoffs(by_id)
                self.assertEqual([item["stage_id"] for item in reconciled], ["experiment"])
                context = runner.context["experiment"]
                self.assertEqual(context["failure_recovery"]["failure_class"],
                                 "model_contract")
                self.assertEqual(context["failure_recovery"]["recovery_mode"],
                                 "format_repair_then_rerun")
                self.assertTrue(context["format_recovery"])
                self.assertEqual(context["research_requests"][0]["kind"], "recovery")
                self.assertEqual(runner.stage_records["experiment"]["status"], "retrying")
            finally:
                runner.close()

    def test_argument_evidence_repairs_route_to_existing_experiment(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            (root / "interpretation").mkdir()
            (root / "argument").mkdir()
            interpretation = {
                "id": "interpretation", "kind": "interpretation", "depends_on": ["experiment"],
                "config_path": str((root / "stage.json").resolve()),
                "project_dir": str((root / "interpretation").resolve()),
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            argument_stage = {
                "id": "argument", "kind": "argument", "depends_on": ["interpretation"],
                "config_path": str((root / "stage.json").resolve()),
                "project_dir": str((root / "argument").resolve()),
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"].extend([interpretation, argument_stage])
            runner = ComposerRunner(workflow)
            try:
                request = runner._argument_experiment_repair_request(
                    argument_stage,
                    {
                        "input_sha256": "a" * 64,
                        "review_directives": [{
                            "text": "Run a threshold sensitivity sweep and independently recalculate the onset.",
                        }],
                        "repair_commands": [], "acceptance_checks": [],
                        "artifact_ref": "artifact:failure@1",
                    },
                )
                self.assertEqual(request["target_stage_id"], "experiment")
                self.assertEqual(request["kind"], "additional_experiment")
                self.assertEqual(request["repair_priority"], "immediate")
            finally:
                runner.close()

    def test_current_argument_failure_overrides_stale_checkpoint_and_routes_new_evidence(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            interpretation_dir = root / "interpretation"
            argument_dir = root / "argument"
            interpretation_dir.mkdir()
            argument_dir.mkdir()
            interpretation = {
                "id": "interpretation", "kind": "interpretation",
                "depends_on": ["experiment"], "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(interpretation_dir.resolve()), "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                "reuse_output_path": None,
            }
            argument_stage = {
                "id": "argument", "kind": "argument",
                "depends_on": ["interpretation"], "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(argument_dir.resolve()), "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                "reuse_output_path": None,
            }
            workflow["stages"].extend([interpretation, argument_stage])
            runner = ComposerRunner(workflow)
            try:
                runner.context["argument"] = {
                    "kind": "argument", "status": "blocked",
                    "error": "stage argument quota exhausted: model_calls=14 > 12",
                    "observed_result": {"project_dir": "cycle-253/attempt-41"},
                    "research_requests": [{
                        "kind": "topic_refinement", "owner": "research.intelligence",
                        "source_stage_id": "argument",
                    }],
                    "format_recovery_attempts": 1,
                }
                error = ValidationError("research argument adjudication requires revision")
                error.research_argument = {"research_question": "current question"}
                error.research_review = {
                    "decision": "revise", "required_repairs": [
                        {"id": "recalculation", "repair": "Independently recalculate the primary result."},
                    ],
                }
                current = {
                    "kind": "argument", "status": "blocked",
                    "error": str(error), "usage": {"model_calls": 8},
                }
                dossier = runner._record_failure_recovery(
                    argument_stage,
                    {"attempt_number": 46, "project_dir": str(argument_dir.resolve())},
                    error, current,
                    {"reports": [{
                        "assigned_role": "strategy.evidence-linker",
                        "response": {"evidence_gaps": [
                            "No independent recalculation of the primary result is supplied.",
                        ]},
                    }]},
                    None, 46,
                )

                self.assertEqual(dossier["failure_class"], "scientific_review")
                self.assertTrue(dossier["recoverable"])
                self.assertEqual(dossier["observed_result"]["error"], str(error))
                self.assertEqual(dossier["project_dir"], str(argument_dir.resolve()))
                recovered = runner.context["argument"]
                self.assertEqual(recovered["format_recovery_attempts"], 1)
                self.assertEqual(recovered["failure_observed_result"]["error"], str(error))
                experiment_orders = [
                    item for item in recovered["research_requests"]
                    if item.get("target_stage_id") == "experiment"
                ]
                self.assertEqual(len(experiment_orders), 1)
                self.assertEqual(experiment_orders[0]["kind"], "additional_experiment")
            finally:
                runner.close()

    def test_resume_reclassifies_saved_argument_review_without_spending_model_calls(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            interpretation_dir = root / "interpretation"
            argument_dir = root / "argument"
            interpretation_dir.mkdir()
            argument_dir.mkdir()
            workflow["stages"].extend([
                {
                    "id": "interpretation", "kind": "interpretation",
                    "depends_on": ["experiment"],
                    "config_path": workflow["stages"][0]["config_path"],
                    "project_dir": str(interpretation_dir.resolve()),
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
                {
                    "id": "argument", "kind": "argument",
                    "depends_on": ["interpretation"],
                    "config_path": workflow["stages"][0]["config_path"],
                    "project_dir": str(argument_dir.resolve()),
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
            ])
            workflow["completion"]["required_stage_ids"].extend(
                ["interpretation", "argument"])
            runner = ComposerRunner(workflow)
            try:
                error_text = "research argument adjudication requires revision"
                dossier = {
                    "schema_version": "composer-failure-recovery-1",
                    "stage_id": "argument", "stage_kind": "argument",
                    "attempt_number": 46, "failure_class": "resource_fence",
                    "recoverable": False, "error": error_text,
                    "observed_result": {"status": "blocked", "error": error_text},
                    "model_diagnostics": {"research_review": {
                        "decision": "revise",
                        "required_repairs": [{
                            "id": "sensitivity",
                            "repair": "Run a threshold sensitivity sweep and independently recalculate the onset.",
                        }],
                    }},
                    "review_directives": [{
                        "text": "Run a threshold sensitivity sweep and independently recalculate the onset.",
                    }],
                    "repair_commands": [{
                        "id": "repair", "operation": "revise_argument",
                        "instruction": "Rebuild the argument from the revised evidence.",
                        "acceptance_check": "All claims map to current evidence.",
                    }],
                    "acceptance_checks": ["All claims map to current evidence."],
                    "input_sha256": "a" * 64,
                }
                manifest = runner._publish(
                    "command/composer/failure-recovery/argument/attempt-46",
                    "report", dossier, "command.composer")
                runner.stage_records["argument"] = {
                    "kind": "argument", "status": "running",
                    "attempt_id": "interrupted-parent-attempt",
                    "task_id": "interrupted-parent-task", "attempt_number": 47,
                    "attempt_count": 47, "active_agents": ["strategy.section-writer"],
                    "attempts": [{
                        "attempt_number": 46, "state": "failed",
                        "error": f"ValidationError: {error_text}",
                        "failure_class": "resource_fence",
                        "failure_dossier_ref": manifest["artifact_ref"],
                    }],
                }
                with patch.object(runner, "_reconcile_interrupted_attempt") as reconcile:
                    self.assertTrue(runner._resume_misclassified_argument_review(
                        set(), {item["id"]: item for item in workflow["stages"]}))
                reconcile.assert_called_once_with("interrupted-parent-attempt")

                recovered = runner.context["argument"]
                self.assertEqual(recovered["failure_recovery"]["failure_class"], "scientific_review")
                self.assertEqual(len(recovered["research_requests"]), 2)
                experiment_order = next(
                    item for item in recovered["research_requests"]
                    if item["kind"] == "additional_experiment")
                self.assertEqual(experiment_order["target_stage_id"], "experiment")
                self.assertEqual(runner.stage_records["argument"]["attempts"][-1]["state"], "unknown")
                self.assertNotIn("attempt_id", runner.stage_records["argument"])
                corrected = runner.store.get(recovered["failure_dossier_ref"])
                corrected_body = json.loads(runner.store.read_body(corrected["body_hash"]))
                self.assertEqual(corrected_body["failure_class"], "scientific_review")
                self.assertEqual(corrected_body["recovery_migration"]["supersedes_artifact_ref"],
                                 manifest["artifact_ref"])

                by_id = {item["id"]: item for item in workflow["stages"]}
                self.assertTrue(runner._begin_continuation(set(), by_id))
                self.assertEqual(runner.active_research_requests[0]["target_stage_id"], "argument")
                self.assertEqual(
                    next(item for item in runner.active_research_requests
                         if item["kind"] == "additional_experiment")["target_stage_id"],
                    "experiment",
                )
                self.assertEqual(runner.reopened_stage_ids,
                                 {"experiment", "interpretation", "argument"})
            finally:
                runner.close()

    def test_resume_retires_argument_review_from_superseded_topic_lineage(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            interpretation_dir = root / "interpretation"
            argument_dir = root / "argument"
            topic_dir.mkdir()
            interpretation_dir.mkdir()
            argument_dir.mkdir()
            config_path = workflow["stages"][0]["config_path"]
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "depends_on": [], "config_path": config_path,
                "project_dir": str(topic_dir.resolve()), "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["stages"].extend([
                {
                    "id": "interpretation", "kind": "interpretation",
                    "depends_on": ["experiment"], "config_path": config_path,
                    "project_dir": str(interpretation_dir.resolve()),
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
                {
                    "id": "argument", "kind": "argument",
                    "depends_on": ["interpretation"], "config_path": config_path,
                    "project_dir": str(argument_dir.resolve()),
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
            ])
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "topic": {"id": "new-topic", "title": "Current direction"},
                    "topic_evolution": {"cycle": 10, "mode": "refinement"},
                }
                error_text = "research argument adjudication requires revision"
                dossier = {
                    "schema_version": "composer-failure-recovery-1",
                    "stage_id": "argument", "stage_kind": "argument",
                    "attempt_number": 46, "failure_class": "resource_fence",
                    "recoverable": False, "error": error_text,
                    "observed_result": {"status": "blocked", "error": error_text},
                    "model_diagnostics": {"research_review": {
                        "decision": "revise",
                        "required_repairs": [{
                            "id": "sensitivity",
                            "repair": "Run an independent sensitivity analysis.",
                        }],
                    }},
                    "review_directives": [{
                        "text": "Run an independent sensitivity analysis.",
                    }],
                    "repair_commands": [{
                        "id": "repair", "operation": "revise_argument",
                        "instruction": "Rebuild the argument from the revised evidence.",
                        "acceptance_check": "All claims map to current evidence.",
                    }],
                    "acceptance_checks": ["All claims map to current evidence."],
                    "input_sha256": "b" * 64,
                }
                manifest = runner._publish(
                    "command/composer/failure-recovery/argument/attempt-46",
                    "report", dossier, "command.composer")
                transition_ref = runner._publish(
                    "command/composer/topic-lineages/cycle-10/retired/argument",
                    "note", {"stage_id": "argument"}, "command.composer")
                runner.stage_records["argument"] = {
                    "kind": "argument", "status": "retrying",
                    "lineage_state": "awaiting_topic_admission",
                    "superseded_topic_id": "old-topic",
                    "lineage_transition_ref": transition_ref["artifact_ref"],
                    "attempts": [{
                        "attempt_number": 46, "cycle": 5, "state": "failed",
                        "failure_class": "resource_fence",
                        "failure_dossier_ref": manifest["artifact_ref"],
                    }],
                }
                runner.context["argument"] = {
                    "kind": "argument", "status": "research_expansion_required",
                    "failure_dossier_ref": manifest["artifact_ref"],
                    "failure_recovery": {
                        "attempt_number": 46, "dossier_ref": manifest["artifact_ref"],
                    },
                    "research_requests": [{
                        "id": "stale-argument-repair", "kind": "additional_experiment",
                        "target_stage_id": "experiment",
                    }],
                }

                by_id = {item["id"]: item for item in workflow["stages"]}
                self.assertFalse(runner._resume_misclassified_argument_review(set(), by_id))
                recovered = runner.context["argument"]
                self.assertEqual(recovered["status"], "topic_pivot_pending")
                self.assertEqual(recovered["lineage_state"], "awaiting_topic_admission")
                self.assertEqual(recovered["lineage_transition_ref"], transition_ref["artifact_ref"])
                self.assertNotIn("research_requests", recovered)
                self.assertEqual(runner._continuation_requests(), [])
                self.assertEqual(
                    runner.store.head("command/composer/failure-recovery/argument/attempt-46")["artifact_ref"],
                    manifest["artifact_ref"],
                )
                retired = [item for item in runner.department_activity
                           if item.get("action") == "retire_superseded_argument_review_recovery"]
                self.assertEqual(len(retired), 1)
                self.assertEqual(retired[0]["source_cycle"], 5)
                self.assertEqual(retired[0]["model_calls"], 0)

                self.assertFalse(runner._resume_misclassified_argument_review(set(), by_id))
                self.assertEqual(len([item for item in runner.department_activity
                                      if item.get("action") == "retire_superseded_argument_review_recovery"]), 1)
            finally:
                runner.close()

    def test_resume_reconciles_review_only_from_feedback_bound_to_exact_argument(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                argument = {"schema_version": "research-argument-1",
                            "research_question": "Current bounded question.",
                            "observed_patterns": [{"id": "current-result", "value": 0.4052}]}
                stale_review = {"decision": "revise", "rationale": "Review of an older result."}
                current_review = {"decision": "revise", "rationale": "Review of the current result."}
                runner.context["argument"] = {
                    "attempt_id": "current-argument-attempt",
                    "research_argument": argument,
                    "research_review": stale_review,
                    "review": stale_review,
                    "research_feedback": {
                        "previous_response": argument,
                        "adjudication": current_review,
                    },
                    "argument_package": {
                        "argument": argument, "review": stale_review,
                    },
                }
                unrelated = {
                    "research_argument": {"research_question": "Another candidate."},
                    "research_review": stale_review,
                    "research_feedback": {
                        "previous_response": {"research_question": "Different candidate."},
                        "adjudication": current_review,
                    },
                }
                runner.context["interpretation"] = unrelated

                self.assertEqual(
                    runner._reconcile_restored_argument_review_pairs(), ["argument"])
                context = runner.context["argument"]
                expected_argument_hash = hashlib.sha256(canonical_bytes(argument)).hexdigest()
                expected_review_hash = hashlib.sha256(canonical_bytes(current_review)).hexdigest()
                self.assertEqual(context["research_review"], current_review)
                self.assertEqual(context["review"], current_review)
                self.assertEqual(context["research_review_argument_sha256"], expected_argument_hash)
                self.assertEqual(context["argument_package"]["review"], current_review)
                self.assertEqual(context["argument_package"]["argument_sha256"], expected_argument_hash)
                self.assertEqual(context["argument_package"]["review_sha256"], expected_review_hash)
                self.assertEqual(runner.context["interpretation"], unrelated)
                self.assertEqual(
                    runner.department_activity[-1]["action"],
                    "reconcile_argument_review_pairing")
            finally:
                runner.close()

    def test_resumable_survey_checkpoint_must_match_current_topic(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "depends_on": [],
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "topic": {"id": "current-rhizosphere"},
                    "topic_evolution": {"cycle": 10, "mode": "refinement"},
                }
                old_project = root / "survey-old-topic"
                (old_project / "state").mkdir(parents=True)
                (old_project / "state" / "control.sqlite").write_bytes(b"")
                (old_project / "output").mkdir()
                run_path = old_project / "output" / "run.json"
                payload = {
                    "survey_current": True,
                    "survey_ref": "artifact:kb/surveys/old@1",
                    "assessment_current": False,
                    "nomination": {"id": "topic-old-mhd-topic"},
                }
                run_path.write_text(json.dumps(payload))
                runner.context["survey"] = {"project_dir": str(old_project)}
                # Even a Composer attempt mislabeled with the new lineage
                # cannot override the topic identity in the survey itself.
                runner.stage_records["survey"] = {"attempts": [{
                    "project_dir": str(old_project),
                    "topic_id": "current-rhizosphere",
                    "topic_cycle": 10,
                }]}
                survey_stage = workflow["stages"][1]
                self.assertIsNone(runner._latest_resumable_survey_project(survey_stage))
                self.assertTrue(any(
                    item.get("action") == "skip_survey_checkpoint_other_topic"
                    and item.get("checkpoint_topic_id") == "old-mhd-topic"
                    for item in runner.department_activity
                ))

                payload["nomination"] = {"id": "topic-current-rhizosphere"}
                run_path.write_text(json.dumps(payload))
                self.assertEqual(
                    runner._latest_resumable_survey_project(survey_stage),
                    old_project.resolve(),
                )
            finally:
                runner.close()

    def test_exhausted_assignment_preserves_runner_handoff(self):
        result = {
            "status": "blocked",
            "error": "gap assessment response contract exhausted",
            "failure": {"kind": "unchanged_assignment_exhausted"},
            "project_dir": "/tmp/survey",
            "survey_ref": "artifact:survey/current@1",
            "assessment_ref": None,
            "survey_current": True,
            "assessment_current": False,
        }
        with self.assertRaises(ModelWorkBlocked) as raised:
            ComposerRunner._raise_stage_failure(result)
        self.assertEqual(raised.exception.stage_result, result)
        self.assertEqual(raised.exception.failure_scope, "stage")

    def test_failure_analysis_persists_dossier_and_scoped_repair_order(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                stage = workflow["stages"][1]
                attempt_stage = {"attempt_number": 1, "project_dir": stage["project_dir"]}
                error = ValidationError("independent recalculation rejected the result")
                error.stage_result = {
                    "status": "blocked", "error": str(error),
                    "execution_refs": ["execution-1"],
                    "metrics": [{"id": "onset", "value": 1.0}],
                }
                dossier = runner._record_failure_recovery(
                    stage, attempt_stage, error, None,
                    {"reports": []}, None, 1)
                self.assertEqual(dossier["failure_class"], "experiment_failure")
                self.assertTrue(dossier["artifact_ref"])
                self.assertEqual(
                    runner.context["experiment"]["failure_recovery"]["recovery_mode"],
                    "repair_then_rerun",
                )
                requests = runner._continuation_requests()
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["kind"], "additional_experiment")
                self.assertTrue(requests[0]["repair_commands"])
            finally:
                runner.close()

    def test_failed_unregistered_foundry_program_is_preserved_in_failure_dossier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"].insert(0, "topic")
            runner = ComposerRunner(workflow)
            question = "Does a revised mechanism change the measured response?"
            domain = "computational physics"
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "completed",
                "topic": {"id": "topic-a", "domain": domain,
                          "research_question": question},
            }
            failed_work = {
                "status": "blocked", "attempts": 3,
                "cache_ref": "artifact:command/foundry-work/cache-key@12",
                "feedback": "Reported and independently recalculated metrics differ.",
                "validation_context": {"reported": 1.4, "recalculated": 1.2},
                "last_attempt": {
                    "executor_source": "def execute(): return {'metric': 1.4}",
                    "validator_source": "def validate(data): return data['metric'] == 1.2",
                    "experiment_intent": {"id": "topic-a", "revision": 1},
                },
            }
            try:
                error = ValidationError(
                    "capability foundry did not admit a program: independent recalculation mismatch")
                with patch.object(runner, "_latest_foundry_failure_projection",
                                  return_value=failed_work) as latest:
                    dossier = runner._record_failure_recovery(
                        workflow["stages"][2],
                        {"attempt_number": 1,
                         "project_dir": workflow["stages"][2]["project_dir"]},
                        error, {}, {"reports": []}, None, 1)
                latest.assert_called_once_with(question, domain)
                snapshot = dossier["foundry_work_snapshot"]
                self.assertEqual(snapshot["cache_ref"], failed_work["cache_ref"])
                self.assertEqual(snapshot["last_attempt"]["executor_source"],
                                 failed_work["last_attempt"]["executor_source"])
                self.assertEqual(snapshot["last_attempt"]["validator_source"],
                                 failed_work["last_attempt"]["validator_source"])
                self.assertEqual(snapshot["validation_context"], failed_work["validation_context"])
                self.assertEqual(snapshot["research_question"], question)
                self.assertTrue(dossier["artifact_ref"])
            finally:
                runner.close()

    def test_foundry_failure_lookup_scans_past_unrelated_history(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                decoys = [{
                    "status": "blocked",
                    "assignment": {"required_intent_fields": {
                        "research_question": f"unrelated question {index}",
                        "domain": "other domain",
                    }},
                } for index in range(256)]
                target = {
                    "status": "blocked", "feedback": "exact failure evidence",
                    "assignment": {"required_intent_fields": {
                        "research_question": "Does mechanism A change outcome B?",
                        "domain": "computational physics",
                    }},
                    "last_attempt": {
                        "experiment_intent": {"id": "topic-a", "revision": 1},
                        "executor_source": "# executor\n" + ("value = 1\n" * 3200),
                        "validator_source": "# validator\n" + ("value = 2\n" * 3200),
                    },
                }
                # A successful record may retain old feedback, but is not the
                # failed authoring state and must not shadow the actual failure.
                stale_success = {
                    "status": "succeeded", "feedback": "stale prior defect",
                    "assignment": target["assignment"],
                }
                stale_manifest = runner.store.publish_artifact(
                    logical_id="command/foundry-work/stale-success",
                    artifact_type="note", author="command.controller",
                    media_type="application/json",
                    body=json.dumps(stale_success).encode("utf-8"))
                stale_success["cache_ref"] = stale_manifest["artifact_ref"]
                target_manifest = runner.store.publish_artifact(
                    logical_id="command/foundry-work/current-failure",
                    artifact_type="note", author="command.controller",
                    media_type="application/json", body=json.dumps(target).encode("utf-8"))
                target["cache_ref"] = target_manifest["artifact_ref"]
                with patch("scisaurus.runtime.composer.ModelWorkCache.entries",
                           return_value=[stale_success, *decoys, target]):
                    result = runner._latest_foundry_failure_projection(
                        "Does mechanism A change outcome B?", "computational physics")
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["feedback"], "exact failure evidence")
                self.assertEqual(result["last_attempt"]["executor_source"],
                                 target["last_attempt"]["executor_source"])
                self.assertEqual(result["last_attempt"]["validator_source"],
                                 target["last_attempt"]["validator_source"])
                self.assertFalse(result["last_attempt"]["source_integrity"]["executor"][
                    "truncated"])
            finally:
                runner.close()

    def test_foundry_failure_projection_marks_oversized_source_as_truncated(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            source = "# executor\n" + ("x" * (CAPABILITY_REPAIR_SOURCE_CHARS + 1))
            target = {
                "status": "blocked",
                "assignment": {"required_intent_fields": {
                    "research_question": "Does mechanism A change outcome B?",
                    "domain": "computational physics",
                }},
                "last_attempt": {
                    "experiment_intent": {"id": "topic-a", "revision": 1},
                    "executor_source": source,
                },
            }
            try:
                target_manifest = runner.store.publish_artifact(
                    logical_id="command/foundry-work/oversized-failure",
                    artifact_type="note", author="command.controller",
                    media_type="application/json", body=json.dumps(target).encode("utf-8"))
                target["cache_ref"] = target_manifest["artifact_ref"]
                with patch("scisaurus.runtime.composer.ModelWorkCache.entries",
                           return_value=[target]):
                    result = runner._latest_foundry_failure_projection(
                        "Does mechanism A change outcome B?", "computational physics")

                projected = result["last_attempt"]["executor_source"]
                integrity = result["last_attempt"]["source_integrity"]["executor"]
                self.assertEqual(projected, source[:CAPABILITY_REPAIR_SOURCE_CHARS])
                self.assertEqual(integrity["characters"], len(source))
                self.assertEqual(integrity["sha256"], hashlib.sha256(
                    source.encode("utf-8")).hexdigest())
                self.assertTrue(integrity["truncated"])
            finally:
                runner.close()

    def test_failure_dossier_evidence_omits_oversized_repair_source(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            executor = "# executor\n" + ("x" * (CAPABILITY_REPAIR_SOURCE_CHARS + 1))
            validator = "def validate(result):\n    return result is not None\n"
            intent = {"id": "topic-a", "revision": 1}
            source_integrity = {
                "executor": {"sha256": hashlib.sha256(
                    executor.encode("utf-8")).hexdigest()},
                "validator": {"sha256": hashlib.sha256(
                    validator.encode("utf-8")).hexdigest()},
            }
            try:
                foundry = runner.store.publish_artifact(
                    logical_id="command/foundry-work/oversized-repair-source",
                    artifact_type="note", author="command.controller",
                    media_type="application/json",
                    body=json.dumps({
                        "status": "blocked",
                        "last_attempt": {
                            "experiment_intent": intent,
                            "source_integrity": source_integrity,
                            "executor_source": executor,
                            "validator_source": validator,
                        },
                    }).encode("utf-8"),
                )
                dossier = runner.store.publish_artifact(
                    logical_id="command/failure-recovery/oversized-source-attempt-1",
                    artifact_type="report", author="command.composer",
                    media_type="application/json",
                    body=json.dumps({
                        "stage_id": "experiment",
                        "attempt_number": 1,
                        "input_sha256": "a" * 64,
                        "foundry_work_snapshot": {
                            "cache_ref": foundry["artifact_ref"],
                            "last_attempt": {
                                "experiment_intent": intent,
                                "source_integrity": source_integrity,
                                "executor_source": executor[:1000],
                                "validator_source": validator,
                            },
                        },
                    }).encode("utf-8"),
                )
                runner.stage_records["experiment"] = {
                    "kind": "experiment",
                    "attempts": [{
                        "attempt_number": 1,
                        "failure_dossier_ref": dossier["artifact_ref"],
                    }],
                }

                evidence = runner._failure_dossier_evidence(
                    dossier["artifact_ref"], expected_stage_id="experiment",
                    expected_attempt_number=1)
                self.assertTrue(evidence["available"])
                executor_record = evidence["source_files"]["executor"]
                self.assertTrue(executor_record["integrity_verified"])
                self.assertTrue(executor_record["source_truncated"])
                self.assertFalse(executor_record["available"])
                self.assertEqual(executor_record["source_chunks"], [])
                self.assertEqual(executor_record["source_characters"], len(executor))
                self.assertTrue(evidence["source_files"]["validator"]["available"])
            finally:
                runner.close()

    def test_capability_repair_packet_preserves_full_failed_program_sources(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            executor = (
                "# executor source\n" + ("value = 1\n" * 3200)
                + "\n# SCISAURUS_TEST_API_KEY=TEST-OPENALEX-KEY-0123456789ABCDEF\n"
            )
            safe_executor = redact_sensitive_text(executor)
            validator = "# validator source\n" + ("value = 2\n" * 3200)
            source_integrity = {
                "executor": {"characters": len(executor), "sha256": hashlib.sha256(
                    executor.encode("utf-8")).hexdigest(),
                             "truncated": False},
                "validator": {"characters": len(validator), "sha256": hashlib.sha256(
                    validator.encode("utf-8")).hexdigest(),
                              "truncated": False},
            }
            topic = {
                "id": "topic-a", "title": "Bounded model comparison",
                "domain": "computational physics",
                "research_question": "Does mechanism A change outcome B?",
            }
            try:
                foundry_work = {
                    "status": "blocked", "attempts": 3,
                    "assignment": {"required_intent_fields": {
                        "research_question": topic["research_question"],
                        "domain": topic["domain"],
                    }},
                    "last_attempt": {
                        "experiment_intent": {"id": "topic-a", "revision": 1},
                        "executor_source": executor,
                        "validator_source": validator,
                        "source_integrity": source_integrity,
                    },
                }
                foundry_manifest = runner.store.publish_artifact(
                    logical_id="command/foundry-work/repair-source-pair",
                    artifact_type="note", author="command.controller",
                    media_type="application/json",
                    body=json.dumps(foundry_work).encode("utf-8"))
                foundry_work["cache_ref"] = foundry_manifest["artifact_ref"]
                dossier_ref = runner.store.publish_artifact(
                    logical_id="command/failure-recovery/experiment-attempt-3",
                    artifact_type="report", author="command.composer",
                    media_type="application/json",
                    body=json.dumps({
                        "stage_id": "experiment", "attempt_number": 3,
                        "failure_class": "experiment_failure",
                        "input_sha256": "c" * 64,
                        "error": "independent recalculation failed",
                        "acceptance_checks": [
                            "independent recalculation for attempt 3",
                            "Do not reuse the stale attempt-2 result",
                        ],
                        "repair_commands": [
                            "repair the failed mechanism in attempt-3",
                            {"instruction": "reopen the stale attempt 2 implementation"},
                        ],
                        "review_directives": [
                            {"text": "Recalculate this attempt 3 independently."},
                            {"text": "Reuse attempt-2 reviewer instructions."},
                        ],
                        "observed_result": {"status": "blocked"},
                        "program_snapshot": [],
                        "foundry_work_snapshot": {
                            "cache_ref": foundry_manifest["artifact_ref"],
                            "feedback": "Independent recalculation failed.",
                            "last_attempt": {
                                "experiment_intent": foundry_work["last_attempt"][
                                    "experiment_intent"],
                                "source_integrity": source_integrity,
                                "executor_source": executor[:18_014],
                                "validator_source": validator[:12_000],
                            },
                        },
                    }).encode("utf-8"),
                )
                dossier = json.loads(runner.store.read_body(dossier_ref["body_hash"]))
                dossier["artifact_ref"] = dossier_ref["artifact_ref"]
                runner.stage_records["experiment"] = {
                    "kind": "experiment",
                    "attempts": [{
                        "attempt_number": 3,
                        "failure_dossier_ref": dossier_ref["artifact_ref"],
                    }],
                }
                prior_context = {
                    "status": "research_expansion_required",
                    "review_status": "scientific_assignment_blocked",
                    "attempt_number": 3,
                    "failure_dossier_ref": dossier_ref["artifact_ref"],
                    "failure_recovery": {
                        "dossier_ref": dossier_ref["artifact_ref"],
                        "attempt_number": 3,
                        "failure_class": "experiment_failure",
                    },
                    "error": "independent recalculation failed",
                    "failure_debt": {"diagnostic": "stale attempt-2 narrative"},
                    "results_package": {"stale_result_ref": "attempt-2-only-result"},
                    "failure_observed_result": {"attempt": "attempt-2-only-result"},
                    "specialist_reports": [{
                        "role_id": "methodologist",
                        "failure_lineage": {
                            "failure_dossier_ref": "artifact:command/failure-recovery/experiment-attempt-2@1",
                            "stage_attempt_number": 2,
                            "failure_input_sha256": "b" * 64,
                        },
                        "response": {
                            "decision": "hold",
                            "summary": "stale attempt-2 report",
                            "findings": ["stale attempt-2 finding"],
                            "requested_actions": ["stale attempt-2 action"],
                        },
                    }, {
                        "role_id": "reproducibility-reviewer",
                        "failure_lineage": {
                            "failure_dossier_ref": dossier_ref["artifact_ref"],
                            "stage_attempt_number": 3,
                            "failure_input_sha256": "c" * 64,
                        },
                        "response": {
                            "decision": "hold",
                            "summary": "This packet asks to reproduce experiment attempt-2.",
                            "findings": [
                                "failure_input_sha256=" + "b" * 64,
                            ],
                            "requested_actions": ["Use the current attempt 3 only."],
                        },
                    }],
                    "specialist_verifier": {
                        "failure_lineage": {
                            "failure_dossier_ref": dossier_ref["artifact_ref"],
                            "stage_attempt_number": 3,
                            "failure_input_sha256": "c" * 64,
                        },
                        "response": {
                            "decision": "hold",
                            "critical_findings": ["Use the stale attempt-2 result."],
                            "repair_scope": ["Recheck attempt 3 only."],
                        },
                    },
                    "experiment_repair_plan": {
                        "failure_lineage": {
                            "failure_dossier_ref": dossier_ref["artifact_ref"],
                            "stage_attempt_number": 3,
                            "failure_input_sha256": "c" * 64,
                        },
                        "root_causes": ["Restart from attempt 2."],
                    },
                    "experiment_repair_history": [{
                        "failure_lineage": {
                            "failure_dossier_ref": "artifact:command/failure-recovery/experiment-attempt-2@1",
                            "stage_attempt_number": 2,
                            "failure_input_sha256": "b" * 64,
                        },
                        "decision": "stale attempt-2 history",
                    }, {
                        "failure_lineage": {
                            "failure_dossier_ref": dossier_ref["artifact_ref"],
                            "stage_attempt_number": 3,
                            "failure_input_sha256": "c" * 64,
                        },
                        "decision": "This history continues from attempt-2.",
                    }],
                }
                dossier_evidence = runner._failure_dossier_evidence(
                    dossier_ref["artifact_ref"], expected_stage_id="experiment",
                    expected_attempt_number=3)
                self.assertEqual(
                    dossier_evidence["review_directives"],
                    ["Recalculate this attempt 3 independently."],
                )
                self.assertEqual(
                    dossier_evidence["repair_commands"],
                    ["repair the failed mechanism in attempt-3"],
                )
                self.assertEqual(
                    dossier_evidence["acceptance_checks"],
                    ["independent recalculation for attempt 3"],
                )
                self.assertEqual(
                    dossier_evidence["provenance_exclusions"],
                    {
                        "review_directive_lineage_conflicts": 1,
                        "repair_command_lineage_conflicts": 1,
                        "acceptance_check_lineage_conflicts": 1,
                    },
                )
                packet = runner._build_capability_repair_packet(
                    runner.workflow["stages"][1], {"topic": topic}, prior_context,
                    ValidationError("independent recalculation failed"))

                self.assertTrue(packet["failure_lineage"]["identity_verified"])
                self.assertEqual(packet["failure_lineage"]["attempt_number"], 3)
                self.assertEqual(packet["failure_lineage"]["failure_input_sha256"], "c" * 64)
                self.assertEqual(packet["prior_specialist_reviews"], [])
                self.assertFalse(packet["prior_verifier"]["available"])
                self.assertIsNone(packet["experiment_repair_plan"])
                self.assertEqual(packet["experiment_repair_history"], [])
                exclusions = packet["provenance_exclusions"]
                self.assertEqual(exclusions["unbound_specialist_report_count"], 1)
                self.assertEqual(
                    exclusions["content_conflicting_specialist_report_count"], 1)
                self.assertFalse(exclusions["unbound_verifier_excluded"])
                self.assertTrue(exclusions["content_conflicting_verifier_excluded"])
                self.assertFalse(exclusions["unbound_repair_plan_excluded"])
                self.assertTrue(exclusions["content_conflicting_repair_plan_excluded"])
                self.assertEqual(exclusions["unbound_repair_history_count"], 1)
                self.assertEqual(exclusions["content_conflicting_repair_history_count"], 1)
                serialized_packet = json.dumps(packet, ensure_ascii=False)
                self.assertNotIn("attempt-2-only", serialized_packet)
                self.assertNotIn("stale attempt-2", serialized_packet)
                self.assertNotIn("experiment attempt-2", serialized_packet)
                self.assertNotIn("Use the stale attempt-2 result", serialized_packet)
                self.assertNotIn("b" * 64, serialized_packet)

                attempt = packet["prior_foundry_work"]["last_attempt"]
                self.assertEqual(attempt["executor_source"], safe_executor)
                self.assertEqual(attempt["validator_source"], validator)
                self.assertTrue(packet["prior_foundry_work"]["cache_body_verified"])
                for name, expected in (("executor_source", safe_executor),
                                       ("validator_source", validator)):
                    source_record = attempt["source_files"][name.removesuffix("_source")]
                    self.assertTrue(source_record["integrity_verified"])
                    self.assertEqual("".join(source_record["source_chunks"]), expected)
                projected = ComposerRunner._capability_authoring_repair_projection(
                    {"packet": packet, "reports": []})
                self.assertEqual(projected["failed_program"]["executor_source"], safe_executor)
                self.assertEqual(projected["failed_program"]["validator_source"], validator)
                self.assertEqual(
                    projected["failed_program"]["source_integrity"]["executor"]["sha256"],
                    hashlib.sha256(executor.encode("utf-8")).hexdigest())
                self.assertFalse(projected["failed_program"]["prompt_source_integrity"]
                                 ["executor"]["matches_expected"])
                self.assertTrue(projected["failed_program"]["prompt_source_integrity"]
                                ["executor"]["redaction_applied"])

                panel_prompt = json.loads(build_specialist_prompt({
                    "assigned_role": "methods.methodologist",
                    "model_role": "methods.methodologist",
                    "stage_id": "experiment",
                    "stage_kind": "experiment",
                    "role_id": "methodologist",
                    "system_contract": "Inspect the exact failed executor and validator.",
                    "input_projection": [],
                    "quota": {"max_input_tokens": 245760},
                }, {
                    "repair_panel": True,
                    "capability_repair_packet": packet,
                }))
                self.assertNotIn("truncated_context", panel_prompt)
                panel_attempt = panel_prompt["projected_input"][
                    "capability_repair_packet"]["prior_foundry_work"]["last_attempt"]
                self.assertEqual("".join(panel_attempt["source_files"]["executor"]
                                         ["source_chunks"]), safe_executor)
                self.assertEqual("".join(panel_attempt["source_files"]["validator"]
                                         ["source_chunks"]), validator)
                self.assertNotIn("TEST-OPENALEX-KEY-0123456789ABCDEF",
                                 json.dumps(panel_prompt, ensure_ascii=False))
            finally:
                runner.close()

    def test_model_contract_failure_does_not_regenerate_experiment_program(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                stage = workflow["stages"][1]
                error = ModelWorkBlocked(
                    "work-review-1 did not satisfy its evidence contract")
                dossier = runner._record_failure_recovery(
                    stage,
                    {"attempt_number": 1, "project_dir": str(root / "experiment")},
                    error,
                    {},
                    {"reports": []},
                    None,
                    1,
                )
                self.assertFalse(
                    runner.context["experiment"]["failure_recovery"]
                    ["requires_capability_repair"])
                self.assertEqual(dossier["failure_class"], "experiment_contract")
            finally:
                runner.close()

    def test_format_contract_failure_stays_in_stage_without_scientific_order(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                stage = workflow["stages"][1]
                error = ValidationError(
                    "research argument review did not finish normally: length")
                dossier = runner._record_failure_recovery(
                    stage,
                    {"attempt_number": 1, "project_dir": str(root / "experiment")},
                    error,
                    {},
                    {"reports": []},
                    None,
                    1,
                )
                context = runner.context["experiment"]
                self.assertEqual(dossier["failure_class"], "model_contract")
                self.assertTrue(context["format_recovery"])
                self.assertEqual(context["format_recovery_attempts"], 1)
                self.assertEqual(len(context["research_requests"]), 1)
                self.assertEqual(context["research_requests"][0]["kind"], "recovery")
                self.assertEqual(context["research_requests"][0]["target_stage_id"], "experiment")
                self.assertEqual(
                    context["failure_recovery"]["recovery_mode"],
                    "format_repair_then_rerun",
                )
            finally:
                runner.close()

    def _workflow(self, root):
        config = root / "stage.json"
        config.write_text("{}")
        survey_dir = root / "survey"
        experiment_dir = root / "experiment"
        survey_dir.mkdir(); experiment_dir.mkdir()
        return {
            "schema_version": "composer-workflow-1",
            "id": "demo-workflow",
            "revision": 1,
            "project_id": str(root / "composer"),
            "objective": "Exercise a dependency-aware composer loop",
            "stages": [
                {"id": "survey", "kind": "survey", "config_path": str(config.resolve()),
                 "project_dir": str(survey_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                 "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                 "reuse_output_path": None},
                {"id": "experiment", "kind": "experiment", "config_path": str(config.resolve()),
                 "project_dir": str(experiment_dir.resolve()), "depends_on": ["survey"], "estimate_seconds": 1,
                 "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                 "reuse_output_path": None},
            ],
            "time_policy": {"first_result_seconds": 1, "target_seconds": 10,
                             "hard_seconds": 30, "checkpoint_seconds": 1},
            "completion": {"required_stage_ids": ["survey", "experiment"],
                            "release_requires_human": True},
        }

    def test_rejects_dependency_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            workflow["stages"][0]["depends_on"] = ["experiment"]
            with self.assertRaises(ValidationError):
                validate_workflow(workflow)

    def test_topic_to_experiment_requires_an_intervening_survey(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][2]["depends_on"] = ["topic"]
            with self.assertRaisesRegex(ValidationError, "requires a survey between"):
                validate_workflow(workflow)

    def test_independent_experiment_does_not_inherit_unrelated_topic_context(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "unrelated-topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "unrelated_topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            runner.context["unrelated_topic"] = {
                "kind": "topic_discovery",
                "admission_state": "provisional_for_survey",
                "topic": {
                    "id": "unrelated", "title": "Unrelated",
                    "research_question": "This must not bind to the experiment.",
                },
            }
            experiment_stage = next(
                item for item in workflow["stages"] if item["id"] == "experiment")
            config = {"sentinel": "unchanged"}
            self.assertIs(
                runner._apply_topic_to_experiment_config(experiment_stage, config), config)
            self.assertEqual(config, {"sentinel": "unchanged"})
            runner.close()

    def test_agenda_policy_is_strictly_validated(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            workflow["agenda_policy"] = {"mode": "adaptive"}
            self.assertEqual(
                validate_workflow(workflow)["agenda_policy"],
                {"mode": "adaptive"})
            workflow["agenda_policy"] = {"mode": "random"}
            with self.assertRaisesRegex(ValidationError, "agenda_policy"):
                validate_workflow(workflow)

    def test_omitted_agenda_policy_preserves_legacy_declaration_order(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            paper_dir = root / "paper"
            paper_dir.mkdir()
            paper = {
                "id": "paper", "kind": "paper",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(paper_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            survey = workflow["stages"][0]
            workflow["stages"] = [paper, survey]
            workflow["completion"]["required_stage_ids"] = ["paper", "survey"]
            runner = ComposerRunner(workflow)
            try:
                ordered = runner._agenda_order(
                    workflow["stages"], completed=set(),
                    by_id={item["id"]: item for item in workflow["stages"]})
                self.assertEqual(runner._agenda_policy(), {"mode": "ordered"})
                self.assertEqual([item["id"] for item in ordered], ["paper", "survey"])
            finally:
                runner.close()

    def test_custom_organization_must_cover_stage_owners(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            organization = default_organization()
            organization["departments"] = [item for item in organization["departments"] if item["id"] != "research"]
            workflow["organization"] = organization
            with self.assertRaisesRegex(ValidationError, "stage-owning departments"):
                validate_workflow(workflow)

    def test_computational_topic_preferences_are_validated_and_projected(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["topic_preferences"] = {
                "mode": "computational_native",
                "must_have": ["a quantitative estimand"],
                "avoid": ["a generic benchmark"],
            }
            validate_workflow(workflow)
            runner = ComposerRunner(workflow)
            try:
                context = runner._runtime_context({"protocol": "openai", "model": "test"})
                self.assertEqual(
                    context["topic_preferences"], workflow["topic_preferences"])
                self.assertEqual(context["research_feasibility"]["max_model_calls"], 0)
                self.assertEqual(context["research_feasibility"]["max_external_requests"], 0)
                self.assertEqual(context["research_feasibility"]["max_experiment_seconds"], 10)
                workflow["topic_preferences"]["mode"] = "unsupported"
                with self.assertRaisesRegex(ValidationError, "mode must be general"):
                    validate_workflow(workflow)
            finally:
                runner.close()

    def test_resume_reopens_legacy_topic_before_downstream_admission(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_model = root / "topic-model.json"
            topic_model.write_text("{}")
            topic_config = root / "topic.json"
            topic_config.write_text(json.dumps({
                "schema_version": "topic-discovery-config-1",
                "model_config_path": str(topic_model.resolve()),
                "output_path": str((topic_dir / "output" / "topic.json").resolve()),
                "candidate_count": 3, "max_attempts": 1,
            }))
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            validate_workflow(workflow)
            runner = ComposerRunner(workflow)
            try:
                candidate = {
                    "id": "direction_0",
                    "capability_requirements": {
                        "executables": [], "python_packages": [], "stage_kinds": [],
                    },
                }
                runner.stage_records = {"topic": {"status": "completed"}}
                runner.context = {
                    "topic": {
                        "kind": "topic_discovery", "status": "completed",
                        "selected_id": "direction_0", "candidates": [candidate],
                        "topic": candidate,
                    },
                }
                failure = runner._restored_topic_feasibility_failure({
                    stage["id"]: stage for stage in workflow["stages"]})
                self.assertIsNotNone(failure)
                self.assertIn("feasibility", failure[1])
                runner._queue_topic_feasibility_revalidation(*failure)
                completed = {"topic"}
                self.assertTrue(runner._begin_continuation(
                    completed, {stage["id"]: stage for stage in workflow["stages"]}))
                self.assertEqual(completed, set())
                self.assertIn("topic", runner.reopened_stage_ids)
                self.assertEqual(
                    runner.active_research_requests[0]["kind"], "topic_refinement")
            finally:
                runner.close()

    def test_runtime_env_files_load_nested_owner_credentials_before_dispatch(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            nested = root / "qwen.env"
            nested.write_text("SCISAURUS_TEST_NESTED=loaded\n")
            env_file = root / "runtime.env"
            env_file.write_text(
                f"SCISAURUS_QWEN_ENV_FILE={nested.name}\n"
                "SCISAURUS_TEST_RUNTIME=loaded\n")
            workflow["runtime_env_files"] = [str(env_file.resolve())]
            keys = ("SCISAURUS_QWEN_ENV_FILE", "SCISAURUS_TEST_RUNTIME",
                    "SCISAURUS_TEST_NESTED")
            previous = {key: os.environ.get(key) for key in keys}
            for key in keys:
                os.environ.pop(key, None)
            runner = ComposerRunner(workflow)
            try:
                self.assertEqual(os.environ.get("SCISAURUS_TEST_RUNTIME"), "loaded")
                self.assertEqual(os.environ.get("SCISAURUS_TEST_NESTED"), "loaded")
            finally:
                runner.close()
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_runs_stages_and_routes_feedback(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def fake_stage(stage, **_kwargs):
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = fake_stage
            with patch("scisaurus.runtime.specialists.ModelClient",
                       _ComposerTestSpecialistClient):
                result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(list(result["stages"]), ["survey", "experiment"])
            self.assertEqual(len(result["feedback"]), 2)
            self.assertEqual(result["release_status"], "needs_human_approval")
            self.assertTrue((Path(workflow["project_id"]) / "output" / "run.json").is_file())
            self.assertEqual(result["feedback"][0]["to"], {"dept": "methods", "agent": "chief"})
            self.assertEqual(result["feedback"][1]["to"], {"dept": "executive-command", "agent": "intent-keeper"})
            with sqlite3.connect(Path(workflow["project_id"]) / "state" / "control.sqlite") as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM messages WHERE state='acknowledged'").fetchone()[0], 2)
            self.assertEqual(result["feedback"][0]["message_disposition"], "scheduled")
            self.assertEqual(result["feedback"][1]["message_disposition"], "scheduled")
            self.assertEqual(result["organization"]["backlog_counts"]["research"]["completed"], 1)
            self.assertEqual(result["organization"]["backlog_counts"]["methods"]["completed"], 1)

    def test_adaptive_agenda_treats_stage_order_as_dependencies_not_itinerary(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            paper_dir = root / "paper"
            paper_dir.mkdir()
            paper = {
                "id": "paper", "kind": "paper",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(paper_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            survey = workflow["stages"][0]
            workflow["stages"] = [paper, survey]
            workflow["completion"]["required_stage_ids"] = ["paper", "survey"]
            workflow["agenda_policy"] = {"mode": "adaptive"}
            workflow["exploration_seed"] = 7
            runner = ComposerRunner(workflow)
            calls = []

            def fake_stage(stage, **_kwargs):
                calls.append(stage["id"])
                output = root / f"{stage['id']}-adaptive.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {
                    "status": "completed", "output_path": str(output),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                }

            runner._run_stage = fake_stage
            with patch("scisaurus.runtime.specialists.ModelClient",
                       _ComposerTestSpecialistClient):
                result = runner.run()
            self.assertEqual(calls, ["survey", "paper"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["agenda_policy"], {"mode": "adaptive"})
            first = result["agenda_decisions"][0]
            self.assertEqual(first["selected_stage_id"], "survey")
            self.assertEqual(
                [item["stage_id"] for item in first["candidate_stages"]],
                ["survey", "paper"])
            self.assertEqual(result["research_state"]["phase"], "release_candidate")

    def test_adaptive_retry_yields_to_other_ready_work_before_replanning(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            paper_dir = root / "paper"
            paper_dir.mkdir()
            paper = {
                "id": "paper", "kind": "paper",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(paper_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            survey = workflow["stages"][0]
            workflow["stages"] = [paper, survey]
            workflow["completion"]["required_stage_ids"] = ["paper", "survey"]
            workflow["agenda_policy"] = {"mode": "adaptive"}
            workflow["retry_policy"] = {"mode": "until_deadline", "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def flaky_stage(stage, **_kwargs):
                calls.append(stage["id"])
                if stage["id"] == "survey" and calls.count("survey") == 1:
                    raise RuntimeError("survey provider failed once")
                output = root / f"{stage['id']}-{len(calls)}.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {
                    "status": "completed", "output_path": str(output),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                }

            runner._run_stage = flaky_stage
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["survey", "paper", "survey"])
            self.assertEqual(
                [item["selected_stage_id"] for item in result["agenda_decisions"][:3]],
                ["survey", "paper", "survey"],
            )
            self.assertTrue(any(
                item.get("action") == "yield_retry_to_agenda"
                for item in result["department_activity"]))
            self.assertEqual(result["retry_schedule"], {})

    def test_resume_restores_agenda_frontier_and_decision_history(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["agenda_policy"] = {"mode": "adaptive"}
            workflow["exploration_seed"] = 19
            runner = ComposerRunner(workflow)
            by_id = {stage["id"]: stage for stage in workflow["stages"]}
            ordered = runner._agenda_order(
                [workflow["stages"][0]], completed=set(), by_id=by_id)
            self.assertEqual(ordered[0]["id"], "survey")
            runner._checkpoint("agenda:test:selected", force=True)
            decision_ref = runner.agenda_decisions[0]["artifact_ref"]
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(len(resumed.agenda_decisions), 1)
                self.assertEqual(
                    resumed.agenda_decisions[0]["artifact_ref"], decision_ref)
                state = resumed._research_state()
                self.assertEqual(state["frontier_stage_ids"], ["survey"])
                self.assertEqual(
                    state["last_agenda_decision"]["selected_stage_id"], "survey")
            finally:
                resumed.close()

    def test_resume_preserves_persisted_adaptive_policy_for_legacy_workflow(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            self.assertNotIn("agenda_policy", workflow)
            runner = ComposerRunner(workflow)
            runner._restored_agenda_policy = {"mode": "adaptive"}
            runner._checkpoint("agenda:migrated", force=True)
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(resumed._agenda_policy(), {"mode": "adaptive"})
            finally:
                resumed.close()

    def test_newer_agenda_checkpoint_supersedes_stale_terminal_report(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["agenda_policy"] = {"mode": "adaptive"}
            runner = ComposerRunner(workflow)
            by_id = {stage["id"]: stage for stage in workflow["stages"]}
            runner._agenda_order([workflow["stages"][0]], completed=set(), by_id=by_id)
            runner._checkpoint("agenda:test:selected", force=True)
            checkpoint_revision = runner.state_revision
            runner._publish("command/composer/run", "report", {
                "schema_version": "composer-run-1",
                "workflow_id": workflow["id"],
                "run_id": runner.run_id,
                "status": "paused",
                "state_revision": checkpoint_revision - 1,
                "stages": {}, "context": {}, "feedback": [], "blockers": [],
                "usage": {}, "agenda_decisions": [],
            }, "command.composer")
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(resumed.state_revision, checkpoint_revision)
                self.assertEqual(len(resumed.agenda_decisions), 1)
                self.assertEqual(
                    resumed.agenda_decisions[0]["selected_stage_id"], "survey")
            finally:
                resumed.close()

    def test_fake_stage_end_to_end_records_specialist_activation_and_verdict(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def fake_stage(stage, **_kwargs):
                output = root / f"{stage['id']}-specialist-result.json"
                output.write_text(json.dumps({"stage": stage["id"], "checked": True}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = fake_stage
            with patch("scisaurus.runtime.specialists.ModelClient",
                       _ComposerTestSpecialistClient):
                result = runner.run()
            self.assertEqual(result["organization"]["schema_version"], "project-organization-2")
            for stage_id in ("survey", "experiment"):
                record = result["stages"][stage_id]
                self.assertEqual(record["active_agents"], [])
                self.assertTrue(record["last_active_agents"])
                self.assertTrue(record["assignment_ids"])
                self.assertNotEqual(record["chief_agent"], record["verifier_agent"])
                self.assertTrue(record["verifier_artifact_ref"].startswith("artifact:"))
                self.assertEqual(record["verifier_outcome"], "accepted")
            self.assertEqual(result["organization"]["active_assignments"], [])
            assignment_count = sum(
                counts["completed"] for counts in result["organization"]["assignment_counts"].values())
            self.assertGreaterEqual(assignment_count, 2)
            with sqlite3.connect(root / "composer" / "state" / "control.sqlite") as conn:
                assignment_tasks = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE payload_json LIKE '%assignment_id%'").fetchone()[0]
            self.assertGreaterEqual(assignment_tasks, 2)

    def test_topic_specialists_review_generated_evidence_not_an_empty_frontier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"] = [{
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            runner = ComposerRunner(workflow)
            specialist_packets = []

            def fake_stage(stage, **_kwargs):
                output = root / "topic-result.json"
                output.write_text(json.dumps({"status": "completed"}))
                return {
                    "status": "completed", "output_path": str(output),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                    "topic": {"id": "direction-1", "research_question": "Does A change B?"},
                    "candidates": [{"id": "direction-1", "research_question": "Does A change B?"}],
                    "frontier_seed_plan": {"seeds": [{"id": "frontier-1", "domain": "A"}]},
                    "recent_papers": [{"work_id": "W1", "title": "A study"}],
                    "candidate_prior_work": [{"work_id": "W1", "title": "A study"}],
                    "feasibility_check": {"status": "feasible"},
                }

            def fake_pool(stage, assignment, descriptor, *, stage_result=None):
                specialist_packets.append(stage_result)
                return {"reports": [], "by_role": {}, "usage": {}, "model_enabled": False}

            runner._run_stage = fake_stage
            runner._run_specialist_pool = fake_pool
            result = runner.run()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(specialist_packets), 1)
            packet = runner._specialist_stage_result_projection(
                workflow["stages"][0], specialist_packets[0])
            self.assertEqual(packet["candidate_topics"][0]["id"], "direction-1")
            self.assertEqual(packet["frontier_seeds"][0]["id"], "frontier-1")
            self.assertEqual(packet["scholarly_records"][0]["work_id"], "W1")
            self.assertEqual(packet["prior_work"][0]["work_id"], "W1")
            runner.close()

    def test_downstream_specialists_receive_materialized_scientific_inputs(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                runner.workflow["stages"].extend([
                    {"id": "interpretation", "kind": "interpretation",
                     "depends_on": ["experiment"]},
                    {"id": "argument", "kind": "argument",
                     "depends_on": ["interpretation"]},
                ])
                runner.context["experiment"] = {
                    "kind": "experiment",
                    "results_package": {
                        "question": "Does the onset survive the control?",
                        "metrics": [{"id": "onset", "value": 4.2, "unit": "nm"}],
                        "findings": [{"id": "finding-1", "statement": "The onset is interior."}],
                        "limitations": ["Reduced model."],
                    },
                }
                interpretation = runner._specialist_stage_result_projection(
                    runner.workflow["stages"][-2], {
                        "status": "completed",
                        "interpretation": {
                            "research_question": "Does the onset survive the control?",
                            "result_patterns": [{"id": "pattern-1", "result_ref": "onset"}],
                            "competing_explanations": [{"id": "aging", "status": "possible"}],
                        },
                    })
                self.assertEqual(interpretation["results"]["metrics"][0]["id"], "onset")
                self.assertEqual(interpretation["alternative_hypotheses"][0]["id"], "aging")

                runner.context["interpretation"] = {
                    "kind": "interpretation",
                    "interpretation": {"interpretation": interpretation["interpretation"]},
                }
                argument = runner._specialist_stage_result_projection(
                    runner.workflow["stages"][-1], {
                        "status": "completed",
                        "argument_package": {
                            "argument": {
                                "research_question": "Does the onset survive the control?",
                                "observed_patterns": [{"id": "pattern-1"}],
                                "hypotheses": [{"id": "aging"}],
                                "primary_argument": {"thesis": "The result is bounded."},
                                "limitations": ["Reduced model."],
                            },
                            "review": {"decision": "accept", "findings": []},
                        },
                    })
                self.assertEqual(argument["claims"][0]["id"], "pattern-1")
                self.assertEqual(argument["argument_plan"]["primary_argument"]["thesis"],
                                 "The result is bounded.")
                self.assertEqual(argument["review_findings"]["decision"], "accept")
                self.assertEqual(argument["evidence_records"][0]["id"], "onset")
            finally:
                runner.close()

    def test_experiment_failure_projection_marks_results_available_only_when_observed(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "topic": {
                        "id": "topic-1",
                        "research_question": "Does substitution change helicity?",
                        "disconfirmation_test": "The response remains invariant.",
                    },
                }
                stage = runner.workflow["stages"][1]
                package_path = Path(stage["project_dir"]) / "output" / "results-package.json"
                package_path.parent.mkdir(parents=True, exist_ok=True)
                failed = runner._specialist_experiment_projection(
                    stage, {"experiment": {}},
                    stage_result={"status": "blocked", "error": "capability not admitted"},
                )
                self.assertEqual(failed["analysis_plan"]["state"], "pre_execution")
                self.assertIsNone(failed["raw_results"])
                self.assertEqual(failed["derived_results"], {})
                self.assertEqual(failed["figures"], [])

                empty = runner._specialist_experiment_projection(
                    stage, {"experiment": {}},
                    stage_result={
                        "status": "blocked",
                        "results_package": {"schema_version": "results-package-2", "id": "stub"},
                        "metrics": [], "findings": [], "analysis": {}, "assets": [],
                    },
                )
                self.assertEqual(empty["analysis_plan"]["state"], "pre_execution")

                metadata_only = runner._specialist_experiment_projection(
                    stage, {"experiment": {}},
                    stage_result={
                        "status": "blocked",
                        "results_package": {"observations": [{"replicate": 1}]},
                        "metrics": [
                            {"id": "", "value": 0.5},
                            {"id": "undefined", "value": None},
                        ],
                    },
                )
                self.assertEqual(
                    metadata_only["analysis_plan"]["state"], "pre_execution")

                nested_metadata = runner._specialist_experiment_projection(
                    stage, {"experiment": {}},
                    stage_result={
                        "status": "blocked",
                        "results_package": {"observations": [{
                            "measurement": {"metadata": {"source": "simulator"}},
                        }]},
                    },
                )
                self.assertEqual(
                    nested_metadata["analysis_plan"]["state"], "pre_execution")

                execution_metadata = runner._specialist_experiment_projection(
                    stage, {"experiment": {}},
                    stage_result={
                        "status": "blocked",
                        "results_package": {"observations": [{
                            "timestamp": "2026-09-25T00:00:00Z",
                            "execution_ref": "run-1",
                            "record_ref": "artifact:experiment/observation@1",
                            "path": "results/observations.json",
                            "ref": "artifact:result@1",
                            "url": "https://example.test/result",
                            "hash": "abc123",
                        }, {"schema_version": "experiment-observation-1"},
                            {"attempt_number": 1}, {"process_returncode": 0}]},
                        "analysis": {"conditions": {"path": "results/conditions.json"}},
                    },
                )
                self.assertEqual(
                    execution_metadata["analysis_plan"]["state"], "pre_execution")

                both_sources = runner._specialist_experiment_projection(
                    stage, {"experiment": {}},
                    stage_result={
                        "status": "blocked",
                        "results_package": {
                            "id": "study-1", "metrics": [],
                            "observations": [{"replicate": 1}],
                        },
                        "raw_results": {"raw_measurement": 0.0},
                    },
                )
                self.assertEqual(
                    both_sources["analysis_plan"]["state"], "stage_result_available")
                self.assertEqual(
                    both_sources["raw_results"]["raw_results"]["raw_measurement"], 0.0)

                oversized_path = package_path.parent / "oversized-results.json"
                oversized_path.write_text(json.dumps({
                    "id": "study-1", "metrics": [{"id": "metric", "value": 1.0}],
                }))
                with patch("scisaurus.runtime.composer.MAX_EXPERIMENT_RESULT_PACKAGE_BYTES", 8):
                    oversized = runner._specialist_experiment_projection(
                        stage, {"experiment": {}},
                        stage_result={"status": "completed",
                                      "results_package": str(oversized_path)})
                self.assertEqual(
                    oversized["analysis_plan"]["state"], "result_payload_unavailable")
                self.assertIsNone(oversized["raw_results"])

                outside_path = root / "outside-results.json"
                outside_path.write_text(json.dumps({
                    "id": "study-1", "metrics": [{"id": "metric", "value": 1.0}],
                }))
                outside = runner._specialist_experiment_projection(
                    stage, {"experiment": {}},
                    stage_result={"status": "completed",
                                  "results_package": str(outside_path)})
                self.assertEqual(
                    outside["analysis_plan"]["state"], "result_payload_unavailable")
                self.assertIsNone(outside["raw_results"])

                observed = runner._specialist_experiment_projection(
                    stage, {"experiment": {}},
                    stage_result={
                        "status": "completed",
                        "results_package": {"observations": [
                            {"replicate": 1, "raw_measurement": 0.0},
                        ]},
                        "metrics": [{"id": "helicity", "value": 0.0}],
                    },
                )
                self.assertEqual(
                    observed["analysis_plan"]["state"], "stage_result_available")

                package_path.write_text(json.dumps({
                    "schema_version": "results-package-2",
                    "id": "study-1",
                    "revision": 1,
                    "metrics": [],
                    "observations": ([{"replicate": index} for index in range(33)] + [{
                        **{f"metadata_{index}": f"entry-{index}" for index in range(36)},
                        "raw_measurement": 0.0,
                    }]),
                }))
                file_backed = runner._specialist_experiment_projection(
                    stage, {"experiment": {}},
                    stage_result={"status": "blocked",
                                  "results_package": str(package_path)})
                self.assertEqual(
                    file_backed["analysis_plan"]["state"], "stage_result_available")
                self.assertEqual(
                    file_backed["raw_results"]["observations"][-1]["raw_measurement"],
                    0.0)

                current_project = Path(stage["project_dir"]) / "attempt-current"
                current_output = current_project / "output"
                current_output.mkdir(parents=True)
                (current_output / "results-package.json").write_text(json.dumps({
                    "schema_version": "results-package-2",
                    "id": "study-current",
                    "metrics": [{"id": "slope", "value": 0.12,
                                 "unit": "log10-rate/log10-gap"}],
                    "findings": [{"id": "finding-current",
                                  "statement": "The fitted slope is positive."}],
                    "observations": [{"replicate": 1, "raw_measurement": 0.12}],
                    "assets": [
                        {"id": "figure-current", "role": "figure",
                         "media_type": "image/png", "path": "figure.png"},
                        {"id": "raw-data", "role": "raw_data",
                         "media_type": "application/json", "path": "raw-data.json"},
                    ],
                }))
                runner.context["experiment"] = {
                    "kind": "experiment", "error": "Earlier attempt timed out.",
                }
                with patch.object(
                        runner, "_failure_program_snapshot",
                        return_value=[{"path": "execution.py", "sha256": "snapshot-hash"}]):
                    current = runner._specialist_experiment_projection(
                        stage, {"experiment": {}}, stage_result={
                            "status": "candidate_needs_review",
                            "attempt_id": "attempt-current",
                            "project_dir": str(current_project.resolve()),
                            "results_package": "output/results-package.json",
                            "execution_refs": ["artifact:execution/current@1"],
                        })
                self.assertEqual(
                    current["analysis_plan"]["state"], "stage_result_available")
                self.assertEqual(current["derived_results"]["metrics"][0]["value"], 0.12)
                self.assertEqual(
                    current["derived_results"]["findings"][0]["id"], "finding-current")
                self.assertEqual([item["id"] for item in current["figures"]],
                                 ["figure-current"])
                self.assertEqual(
                    current["claims"]["status"], "measurements_available")
                self.assertEqual(
                    current["claims"]["hypothesis_status"], "not_adjudicated")
                self.assertEqual(
                    current["execution_manifest"]["state"], "observed_results")
                self.assertEqual(
                    current["execution_manifest"]["execution_refs"],
                    ["artifact:execution/current@1"])
                self.assertEqual(
                    current["analysis_code"]["state"],
                    "execution_source_unverified")
                self.assertEqual(
                    current["failure_evidence"]["temporal_scope"],
                    "historical_prior_attempts_only")
            finally:
                runner.close()

    def test_provisional_topic_verifier_hold_becomes_survey_requirements(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            calls = []

            def fake_stage(stage, **_kwargs):
                calls.append(stage["id"])
                output = root / f"{stage['id']}-provisional.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                result = {
                    "status": "completed", "output_path": str(output),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                }
                if stage["id"] == "topic":
                    result.update({
                        "admission_state": "provisional_for_survey",
                        "next_evidence_action": "literature_survey",
                        "maturity_open_requirements": ["Ground the comparator."],
                        "topic": {
                            "id": "direction-1", "title": "A provisional direction",
                            "research_question": "Does A distinguish B from C?",
                        },
                    })
                return result

            def verifier(stage, *_args, **_kwargs):
                response = ({
                    "decision": "hold",
                    "rationale": "The reference dataset must be located.",
                    "critical_findings": ["The reference dataset is not pinned."],
                    "repair_scope": ["Locate and verify the reference dataset in the survey."],
                } if stage["id"] == "topic" else {
                    "decision": "accept", "rationale": "The bounded result is supported.",
                    "critical_findings": [], "repair_scope": [],
                })
                return {"status": "succeeded", "response": response, "usage": {}}

            runner._run_stage = fake_stage
            runner._run_specialist_pool = lambda *_args, **_kwargs: {
                "reports": [], "by_role": {}, "usage": {}, "model_enabled": False,
            }
            runner._publish_specialist_reports = lambda _stage, _assignment, bundle: bundle
            runner._run_specialist_verifier = verifier
            result = runner.run()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["topic", "survey", "experiment"])
            topic = result["context"]["topic"]
            self.assertIn(
                "Locate and verify the reference dataset in the survey.",
                topic["maturity_open_requirements"],
            )
            self.assertEqual(
                topic["provisional_adversarial_challenge"]["control_disposition"],
                "carried_to_literature_survey",
            )
            self.assertEqual(result["stages"]["topic"]["verifier_outcome"], "hold")
            runner.close()

    def test_candidate_release_is_forwarded_for_principal_review(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def fake_stage(stage, **_kwargs):
                output = root / f"{stage['id']}-result.pdf"
                output.write_bytes(b"candidate")
                return {"status": "candidate_needs_review" if stage["id"] == "experiment" else "completed",
                        "output_path": str(output), "project_dir": stage["project_dir"],
                        "stage_id": stage["id"]}

            runner._run_stage = fake_stage
            with patch("scisaurus.runtime.specialists.ModelClient",
                       _ComposerTestSpecialistClient):
                result = runner.run()
            self.assertEqual(result["status"], "candidate_needs_review")
            self.assertEqual(result["release_status"], "candidate_needs_review")
            self.assertEqual(result["feedback"][-1]["status"], "candidate_needs_review")
            self.assertEqual(result["feedback"][-1]["action"], "advance")
            self.assertEqual(result["feedback"][-1]["to"], {"dept": "executive-command", "agent": "intent-keeper"})
            self.assertIn("principal review", result["feedback"][-1]["next_condition"])

    def test_paper_release_gate_fences_provisional_scientific_ancestors(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            argument_dir = root / "argument"
            paper_dir = root / "paper"
            argument_dir.mkdir()
            paper_dir.mkdir()
            config_path = workflow["stages"][0]["config_path"]
            workflow["stages"].extend([
                {
                    "id": "argument", "kind": "argument", "config_path": config_path,
                    "project_dir": str(argument_dir.resolve()), "depends_on": ["experiment"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
                {
                    "id": "paper", "kind": "paper", "config_path": config_path,
                    "project_dir": str(paper_dir.resolve()), "depends_on": ["argument"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
            ])
            runner = ComposerRunner(workflow)
            try:
                runner.stage_records = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "composer_decision": "advance_with_findings",
                        "release_blocking": False,
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                    "paper": {
                        "kind": "paper", "status": "candidate_needs_review",
                        "forward_progress": True,
                        "composer_decision": "advance_with_findings",
                        "release_blocking": False,
                        "failure_debt": {},
                    },
                }
                runner.context = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "verifier_outcome": "hold", "gap_state": "insufficient_evidence",
                        "topic_admission": "exploratory_pilot", "survey_current": False,
                        "assessment_current": False,
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                paper = by_id["paper"]
                blockers = runner._paper_release_blockers(paper, by_id)
                self.assertEqual([item["stage_id"] for item in blockers], ["survey"])
                self.assertIn("provisional_candidate", blockers[0]["reasons"])
                self.assertIn("verifier_hold", blockers[0]["reasons"])
                self.assertFalse(ComposerRunner._stage_releases_dependencies(
                    {"status": "candidate_needs_review", "composer_decision": "advance_with_findings"},
                    stage_kind="paper"))

                completed = {"survey", "experiment", "argument"}
                release_blocked = set()
                self.assertTrue(runner._refresh_paper_release_gate(
                    completed, by_id, release_blocked))
                self.assertIn("paper", release_blocked)
                self.assertEqual(
                    runner.stage_records["paper"]["release_gate"]["kind"],
                    "upstream_scientific_hold")

                runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
                runner.context["survey"] = {"kind": "survey", "status": "completed"}
                self.assertTrue(runner._refresh_paper_release_gate(
                    completed, by_id, release_blocked))
                self.assertNotIn("paper", release_blocked)
                self.assertEqual(runner.stage_records["paper"]["status"], "pending")
            finally:
                runner.close()

    def test_paper_is_not_dispatched_while_upstream_candidate_is_provisional(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            argument_dir = root / "argument"
            paper_dir = root / "paper"
            argument_dir.mkdir()
            paper_dir.mkdir()
            config_path = workflow["stages"][0]["config_path"]
            workflow["stages"].extend([
                {
                    "id": "argument", "kind": "argument", "config_path": config_path,
                    "project_dir": str(argument_dir.resolve()), "depends_on": ["experiment"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
                {
                    "id": "paper", "kind": "paper", "config_path": config_path,
                    "project_dir": str(paper_dir.resolve()), "depends_on": ["argument"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
            ])
            workflow["completion"]["required_stage_ids"] = ["paper"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 0}
            runner = ComposerRunner(workflow)
            calls = []
            try:
                runner.stage_records = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "composer_decision": "advance_with_findings",
                        "release_blocking": False,
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }
                runner.context = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "verifier_outcome": "hold",
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }

                def must_not_dispatch(stage, **_kwargs):
                    calls.append(stage["id"])
                    raise AssertionError("paper was dispatched behind a scientific hold")

                runner._run_stage = must_not_dispatch
                result = runner.run()
                self.assertEqual(result["status"], "candidate_needs_review")
                self.assertEqual(calls, [])
                self.assertEqual(
                    result["stages"]["paper"]["release_gate"]["kind"],
                    "upstream_scientific_hold")
                self.assertTrue(any(
                    item.get("stop_reason") == "upstream_scientific_hold"
                    for item in result["blockers"]))
            finally:
                runner.close()

    def test_paper_gate_admits_scoped_repair_before_returning_a_candidate(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            argument_dir = root / "argument"
            paper_dir = root / "paper"
            argument_dir.mkdir()
            paper_dir.mkdir()
            config_path = workflow["stages"][0]["config_path"]
            workflow["stages"].extend([
                {
                    "id": "argument", "kind": "argument", "config_path": config_path,
                    "project_dir": str(argument_dir.resolve()), "depends_on": ["experiment"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
                {
                    "id": "paper", "kind": "paper", "config_path": config_path,
                    "project_dir": str(paper_dir.resolve()), "depends_on": ["argument"],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                },
            ])
            workflow["completion"]["required_stage_ids"] = ["paper"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            runner = ComposerRunner(workflow)
            calls = []
            try:
                runner.stage_records = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "composer_decision": "advance_with_findings",
                        "release_blocking": False,
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }
                runner.context = {
                    "survey": {
                        "kind": "survey", "status": "candidate_needs_review",
                        "verifier_outcome": "hold", "gap_state": "insufficient_evidence",
                    },
                    "experiment": {"kind": "experiment", "status": "completed"},
                    "argument": {"kind": "argument", "status": "completed"},
                }

                def recover(stage, **_kwargs):
                    calls.append(stage["id"])
                    output = root / f"{stage['id']}-repaired.json"
                    output.write_text(json.dumps({"stage": stage["id"], "repaired": True}))
                    return {
                        "status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"],
                    }

                runner._run_stage = recover
                result = runner.run()
                self.assertEqual(result["status"], "completed")
                self.assertEqual(calls, ["survey", "experiment", "argument", "paper"])
                self.assertEqual(result["continuation_cycles"], 1)
                self.assertTrue(any(
                    item.get("action") == "paper_release_gate"
                    and item.get("repair_requests")
                    for item in result["department_activity"]))
            finally:
                runner.close()

    def test_scientific_hold_remains_visible_in_stage_task_backlog(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 0}
            runner = ComposerRunner(workflow)

            def held(stage, **kwargs):
                output = root / f"{stage['id']}-hold.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "research_expansion_required", "output_path": str(output),
                        "project_dir": stage["project_dir"], "research_expansion_requests": []}

            runner._run_stage = held
            result = runner.run()
            self.assertEqual(result["status"], "research_expansion_required")
            self.assertEqual(result["organization"]["backlog_counts"]["research"]["awaiting_review"], 1)

    def test_topic_verifier_hold_reopens_topic_before_admitting_survey(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "review_rejected",
                "specialist_verifier": {
                    "response": {
                        "decision": "hold",
                        "repair_scope": ["cite the decisive parameter source"],
                    },
                },
            }
            runner.stage_records["topic"] = {
                "kind": "topic_discovery", "status": "review_rejected",
            }

            requests = runner._continuation_requests()
            self.assertEqual([item["kind"] for item in requests], ["topic_refinement"])
            self.assertEqual(requests[0]["evidence_needed"], "cite the decisive parameter source")
            self.assertNotIn("manuscript_revision", {item["kind"] for item in requests})

            completed = {"topic"}
            by_id = {stage["id"]: stage for stage in workflow["stages"]}
            self.assertTrue(runner._begin_continuation(completed, by_id))
            self.assertNotIn("topic", completed)
            self.assertNotIn("survey", completed)
            self.assertEqual(runner.reopened_stage_ids,
                             {"topic", "survey", "experiment"})
            runner.close()

    def test_topic_pivot_archives_and_clears_downstream_live_lineage(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"] = ["topic", "survey", "experiment"]
            runner = ComposerRunner(workflow)
            try:
                runner.context = {
                    "topic": {
                        "kind": "topic_discovery", "status": "research_expansion_required",
                        "topic": {"id": "old-topic", "research_question": "Old question?"},
                        "research_expansion_requests": [{
                            "id": "pivot-topic", "kind": "topic_refinement",
                            "owner": "research.intelligence", "objective": "Choose a new direction.",
                            "why": "The current direction did not produce a usable result.",
                            "success_condition": "Admit a distinct feasible question.",
                            "evidence_needed": "Prior failure plus a fresh candidate review.",
                        }],
                    },
                    "survey": {"kind": "survey", "status": "completed",
                               "foreign_claim": "stale survey result",
                               "survey_ref": "artifact:kb/surveys/old@1"},
                    "experiment": {"kind": "experiment", "status": "completed",
                                   "foreign_claim": "stale experiment result",
                                   "results_ref": "artifact:methods/results/old@1"},
                }
                old_attempts = [{"attempt_number": 3, "state": "failed",
                                 "project_dir": "/old/topic/attempt-3"}]
                runner.stage_records = {
                    "topic": {"kind": "topic_discovery", "status": "completed"},
                    "survey": {"kind": "survey", "status": "completed",
                                "attempt_count": 3, "attempts": old_attempts},
                    "experiment": {"kind": "experiment", "status": "completed",
                                    "attempt_count": 3, "attempts": old_attempts},
                }
                completed = {"topic", "survey", "experiment"}
                by_id = {item["id"]: item for item in workflow["stages"]}

                self.assertTrue(runner._begin_continuation(completed, by_id))
                for stage_id in ("survey", "experiment"):
                    context = runner.context[stage_id]
                    record = runner.stage_records[stage_id]
                    self.assertNotIn("foreign_claim", context)
                    self.assertEqual(context["research_requests"], [])
                    self.assertEqual(context["status"], "topic_pivot_pending")
                    self.assertEqual(context["superseded_topic_id"], "old-topic")
                    self.assertEqual(record["status"], "retrying")
                    self.assertEqual(record["attempts"], old_attempts)
                    self.assertEqual(record["lineage_state"], "awaiting_topic_admission")
                    transition = runner.store.get(context["lineage_transition_ref"])
                    body = json.loads(runner.store.read_body(transition["body_hash"]))
                    self.assertEqual(body["previous_topic_id"], "old-topic")
                    expected_ref = (
                        "artifact:kb/surveys/old@1" if stage_id == "survey"
                        else "artifact:methods/results/old@1"
                    )
                    self.assertIn(expected_ref, body["prior_artifact_refs"])
                self.assertEqual(runner.context["topic"]["topic"]["id"], "old-topic")
                self.assertNotIn("survey", completed)
                self.assertNotIn("experiment", completed)
            finally:
                runner.close()

    def test_superseded_topic_request_cannot_be_retagged_as_current(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery", "topic": {"id": "new-topic"},
                }
                runner.context["survey"] = {
                    "kind": "survey", "status": "research_expansion_required",
                    "research_requests": [{
                        "id": "old-topic-order", "kind": "literature_expansion",
                        "owner": "research.intelligence", "objective": "Continue old topic.",
                        "why": "Old result needs work.", "success_condition": "Refresh old result.",
                        "evidence_needed": "Old sources.",
                    }],
                }
                runner.stage_records["survey"] = {"kind": "survey", "topic_id": "old-topic"}

                self.assertEqual(runner._continuation_requests(), [])
                self.assertTrue(any(
                    item.get("action") == "drop_superseded_topic_work_orders"
                    and item.get("stage_id") == "survey"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_fresh_current_topic_attempt_releases_its_repair_orders(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 2}
            workflow["progression_policy"] = "forward_first"
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "new-topic"},
                    "topic_evolution": {"mode": "refinement", "cycle": 7},
                }
                runner.continuation_cycles = 2
                runner._continuation_budget_baseline = 0
                topic_identity = runner._current_topic_identity()
                attempt_id = "fresh-argument-attempt"
                request = {
                    "id": "fresh-experiment-repair",
                    "kind": "additional_experiment",
                    "owner": "methods.validation",
                    "objective": "Run a discriminating control for the current topic.",
                    "why": "The current result leaves two explanations unresolved.",
                    "success_condition": "The control separates the competing explanations.",
                    "evidence_needed": "Versioned output and independent recalculation.",
                    "target_stage_id": "experiment",
                    "target_stage_kind": "experiment",
                }
                runner.context["argument"] = {
                    "kind": "argument", "status": "research_expansion_required",
                    "superseded_topic_id": "old-topic",
                    "lineage_state": "awaiting_topic_admission",
                    "lineage_transition_ref": "artifact:old-topic-transition@1",
                    "research_requests": [request],
                }
                runner.stage_records["argument"] = {
                    "kind": "argument", "status": "running",
                    "attempt_id": attempt_id,
                    "topic_id": topic_identity["topic_id"],
                    "topic_cycle": topic_identity["topic_cycle"],
                }

                self.assertFalse(runner._refresh_stage_topic_lineage(
                    "argument", runner.context["argument"],
                    attempt_id="different-attempt", topic_identity=topic_identity))
                self.assertEqual(
                    runner.context["argument"]["superseded_topic_id"], "old-topic")
                self.assertTrue(runner._refresh_stage_topic_lineage(
                    "argument", runner.context["argument"],
                    attempt_id=attempt_id, topic_identity=topic_identity))
                current_context = runner.context["argument"]
                self.assertEqual(current_context["topic_lineage"], topic_identity)
                self.assertEqual(current_context["topic_lineage_attempt_id"], attempt_id)
                self.assertNotIn("superseded_topic_id", current_context)
                self.assertNotIn("lineage_state", current_context)
                self.assertNotIn("lineage_transition_ref", current_context)

                requests = runner._continuation_requests()
                self.assertEqual([item["id"] for item in requests], [request["id"]])
                by_id = {item["id"]: item for item in workflow["stages"]}
                self.assertTrue(runner._begin_continuation(set(), by_id))
                self.assertEqual(runner.active_research_requests[0]["id"], request["id"])
                self.assertEqual(runner.reopened_stage_ids, {"experiment"})
                self.assertEqual(runner.continuation_cycles, 3)
                self.assertTrue(any(
                    item.get("action") == "refresh_stage_topic_lineage"
                    and item.get("attempt_id") == attempt_id
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_resume_restores_current_topic_repair_from_failed_attempt_dossier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 2}
            workflow["progression_policy"] = "forward_first"
            runner = ComposerRunner(workflow)
            try:
                topic_identity = {"topic_id": "new-topic", "topic_cycle": 9}
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": topic_identity["topic_id"]},
                    "topic_evolution": {"mode": "refinement", "cycle": 9},
                }
                attempt_id = "failed-current-topic-attempt"
                dossier_ref = "artifact:command/composer/failure-recovery/argument/attempt-4@1"
                request = {
                    "id": "resume-experiment-repair",
                    "kind": "additional_experiment",
                    "owner": "methods.validation",
                    "objective": "Run the missing current-topic control.",
                    "why": "The current review requested new discriminating evidence.",
                    "success_condition": "The added control separates the alternatives.",
                    "evidence_needed": "Raw output and independent recalculation.",
                    "target_stage_id": "experiment",
                    "target_stage_kind": "experiment",
                }
                runner.context["argument"] = {
                    "stage_id": "argument", "kind": "argument",
                    "status": "research_expansion_required",
                    "superseded_topic_id": "prior-topic",
                    "lineage_state": "awaiting_topic_admission",
                    "lineage_transition_ref": "artifact:prior-topic-transition@1",
                    "failure_dossier_ref": dossier_ref,
                    "failure_recovery": {"attempt_number": 4},
                    "research_requests": [request],
                }
                runner.stage_records["argument"] = {
                    "kind": "argument", "status": "blocked",
                    "topic_id": topic_identity["topic_id"],
                    "topic_cycle": topic_identity["topic_cycle"],
                    "attempts": [{
                        "attempt_number": 4, "attempt_id": attempt_id,
                        "state": "failed", "cycle": 9,
                        "topic_id": topic_identity["topic_id"],
                        "topic_cycle": topic_identity["topic_cycle"],
                        "failure_dossier_ref": dossier_ref,
                        "repair_order_issued": True,
                    }],
                }
                runner.continuation_cycles = 2
                runner._continuation_budget_baseline = 0

                self.assertEqual(
                    runner._reconcile_restored_stage_topic_lineage(), ["argument"])
                self.assertEqual(runner.context["argument"]["topic_lineage"], topic_identity)
                self.assertEqual(
                    [item["id"] for item in runner._continuation_requests()], [request["id"]])
                by_id = {item["id"]: item for item in workflow["stages"]}
                self.assertTrue(runner._begin_continuation(set(), by_id))
                self.assertEqual(runner.active_research_requests[0]["id"], request["id"])
            finally:
                runner.close()

    def test_topic_pivot_never_resumes_an_old_surveys_incomplete_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            try:
                old_survey = root / "survey" / "continuations" / "cycle-2"
                (old_survey / "state").mkdir(parents=True)
                (old_survey / "state" / "control.sqlite").write_bytes(b"checkpoint")
                (old_survey / "output").mkdir()
                (old_survey / "output" / "run.json").write_text(json.dumps({
                    "status": "blocked", "survey_ref": "artifact:kb/surveys/old@1",
                    "survey_current": True, "assessment_current": False,
                }))
                runner.context["topic"] = {
                    "kind": "topic_discovery", "topic": {"id": "new-topic"},
                }
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "retrying",
                    "lineage_state": "awaiting_topic_admission",
                    "superseded_topic_id": "old-topic",
                    "attempts": [{"project_dir": str(old_survey), "state": "blocked"}],
                }
                runner.reopened_stage_ids = {"survey"}

                dispatched = runner._stage_for_cycle(workflow["stages"][1])
                self.assertNotEqual(Path(dispatched["project_dir"]), old_survey.resolve())
                self.assertIn("continuations/cycle-0", dispatched["project_dir"])
            finally:
                runner.close()

    def test_scientific_hold_without_request_gets_a_cycle_specific_recovery_strategy(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.context["experiment"] = {
                "kind": "experiment", "status": "research_expansion_required",
                "research_expansion_requests": [],
                "specialist_verifier": {
                    "response": {
                        "decision": "hold",
                        "critical_findings": ["The current control cannot separate the explanations."],
                    },
                },
            }

            first = runner._continuation_requests()
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["kind"], "additional_experiment")
            self.assertEqual(first[0]["owner"], "methods.validation")
            self.assertIn("discriminating", first[0]["objective"])
            runner._attempted_request_signatures.add(
                runner._research_request_signature(first[0]))

            runner.continuation_cycles = 1
            second = runner._continuation_requests()
            self.assertEqual(len(second), 1)
            self.assertNotEqual(first[0]["id"], second[0]["id"])
            self.assertNotEqual(first[0]["objective"], second[0]["objective"])
            self.assertIn("boundary", second[0]["objective"])
            runner.close()

    def test_blocked_scientific_assignment_admits_a_fresh_recovery_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            runner = ComposerRunner(workflow)
            try:
                runner.context["survey"] = {"kind": "survey", "status": "completed"}
                runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
                runner.stage_records["experiment"] = {"kind": "experiment", "status": "blocked"}
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = {"survey"}
                admitted = runner._admit_scientific_blocker_recovery(
                    workflow["stages"][1],
                    ModelWorkBlocked("independent review rejected the estimator"),
                    completed, by_id)
                # This fixture has no topic ancestor, so there is no changed
                # scientific frontier to reopen. Preserve the blocker instead
                # of manufacturing a same-stage repair loop.
                self.assertFalse(admitted)
                self.assertEqual(runner.continuation_cycles, 0)
                self.assertNotIn("experiment", completed)
                self.assertEqual(runner.active_research_requests, [])
            finally:
                runner.close()

    def test_pre_execution_capability_failure_opens_scoped_repair_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"].insert(0, topic_stage)
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            workflow["progression_policy"] = "forward_first"
            workflow["completion"]["required_stage_ids"] = ["topic", "survey", "experiment"]
            runner = ComposerRunner(workflow)
            try:
                error = "ModelWorkBlocked: capability foundry did not admit a program: metric is nan"
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "old", "title": "Old direction",
                              "research_question": "Does A change B?"},
                }
                runner.context["survey"] = {"kind": "survey", "status": "completed"}
                runner.context["experiment"] = {
                    "kind": "experiment", "status": "candidate_needs_review",
                    "results_status": "not_executed", "failure_debt": {
                    "attempts": 3, "error": error,
                    },
                }
                runner.stage_records["topic"] = {"kind": "topic_discovery", "status": "completed"}
                runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
                runner.stage_records["experiment"] = {
                    "kind": "experiment", "status": "candidate_needs_review",
                    "attempt_count": 3, "error": error,
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = {"topic", "survey"}
                self.assertTrue(runner._admit_scientific_blocker_recovery(
                    workflow["stages"][2], ModelWorkBlocked(error), completed, by_id))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertFalse(any(
                    item.get("action") == "pivot_topic_after_scientific_blocker"
                    for item in runner.department_activity
                ))
                self.assertFalse(runner._composer_can_advance_after_admission(
                    workflow["stages"][2], ModelWorkBlocked(error)))
            finally:
                runner.close()

    def test_repeated_pre_execution_capability_failure_keeps_repairing_same_topic(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            workflow["progression_policy"] = "forward_first"
            runner = ComposerRunner(workflow)
            try:
                topic = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "old", "title": "Old direction",
                              "domain": "computational physics",
                              "research_question": "Does A change B?"},
                }
                recovery = {
                    "failure_class": "experiment_failure",
                    "requires_capability_repair": True,
                    "recovery_mode": "repair_then_rerun",
                    "dossier_ref": "artifact:failure-dossier",
                    "input_sha256": "a" * 64,
                    "repair_commands": [{"id": "edit", "operation": "edit_program",
                                         "instruction": "repair the executor"}],
                    "acceptance_checks": ["independent recalculation"],
                    "review_directives": [{"text": "change the failed mechanism"}],
                }
                runner.context = {
                    "topic": topic,
                    "survey": {"kind": "survey", "status": "completed"},
                    "experiment": {
                        "kind": "experiment", "status": "research_expansion_required",
                        "review_status": "scientific_assignment_blocked",
                        "error": "capability foundry independent recalculation failed",
                        "capability_repair_attempts": 3,
                        "failure_recovery": recovery,
                    },
                }
                runner.stage_records = {
                    "topic": {"kind": "topic_discovery", "status": "completed"},
                    "survey": {"kind": "survey", "status": "completed"},
                    "experiment": {"kind": "experiment", "status": "blocked"},
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = {"topic", "survey"}
                error = ModelWorkBlocked(
                    "capability foundry independent recalculation repair budget exhausted")
                self.assertTrue(runner._admit_scientific_blocker_recovery(
                    by_id["experiment"], error, completed, by_id))
                self.assertEqual(runner.continuation_cycles, 1)
                request = runner.active_research_requests[0]
                self.assertEqual(request["kind"], "additional_experiment")
                self.assertEqual(request["owner"], "methods.validation")
                self.assertEqual(request["target_stage_id"], "experiment")
                self.assertEqual(request["failure_dossier_ref"], "artifact:failure-dossier")
                self.assertEqual(request["experiment_repair_plan"]["lineage"][
                    "prior_capability_repair_attempts"], 3)
                self.assertEqual(runner.context["experiment"]["capability_repair_attempts"], 4)
                self.assertEqual(runner.context["topic"]["topic"]["id"], "old")
                self.assertFalse(any(
                    item.get("action") == "pivot_topic_after_experiment_repair_limit"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_fresh_capability_failure_repairs_same_topic_despite_many_stage_attempts(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            workflow["progression_policy"] = "forward_first"
            runner = ComposerRunner(workflow)
            try:
                topic = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "current", "title": "Current direction",
                              "domain": "computational physics",
                              "research_question": "Does A change B?"},
                }
                error_text = (
                    "ModelWorkBlocked: capability foundry did not admit a program: "
                    "executor failed in the sandbox with IndexError"
                )
                recovery = {
                    "failure_class": "experiment_failure",
                    "requires_capability_repair": True,
                    "recovery_mode": "repair_then_rerun",
                    "dossier_ref": "artifact:failure-dossier",
                    "input_sha256": "b" * 64,
                    "repair_commands": [{"id": "edit", "operation": "edit_program",
                                         "instruction": "repair the failed executor"}],
                    "acceptance_checks": ["independent recalculation"],
                    "review_directives": [{"text": "fix the exact IndexError and rerun"}],
                }
                runner.context = {
                    "topic": topic,
                    "survey": {"kind": "survey", "status": "completed"},
                    "experiment": {
                        "kind": "experiment", "status": "research_expansion_required",
                        "review_status": "scientific_assignment_blocked",
                        "error": error_text, "results_status": "not_executed",
                        "failure_dossier_ref": "artifact:failure-dossier",
                        "failure_recovery": recovery,
                        "failure_debt": {"attempts": 165, "error": error_text},
                    },
                }
                runner.stage_records = {
                    "topic": {"kind": "topic_discovery", "status": "completed"},
                    "survey": {"kind": "survey", "status": "completed"},
                    "experiment": {"kind": "experiment", "status": "blocked",
                                   "attempt_count": 165},
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                self.assertEqual(runner._pre_execution_repair_count(
                    by_id["experiment"], error=error_text), 0)
                foundry_gate_exhaustion = (
                    "ModelWorkBlocked: capability foundry adversarial_review repair budget "
                    "exhausted after 2 failures: model outputs are not measurements"
                )
                runner.context["experiment"]["failure_recovery"]["model_diagnostics"] = {
                    "repair_gate": "adversarial_review",
                    "repair_budget_exhausted": {"gate": "adversarial_review", "failures": 2},
                }
                self.assertEqual(runner._pre_execution_repair_count(
                    by_id["experiment"], error=foundry_gate_exhaustion), 0)
                runner.context["experiment"]["experiment_repair_history"] = [
                    {"repair_attempts": 0},
                ]
                self.assertEqual(runner._pre_execution_repair_count(
                    by_id["experiment"], error=error_text), 1)
                runner.context["experiment"].pop("experiment_repair_history")
                self.assertTrue(runner._admit_scientific_blocker_recovery(
                    by_id["experiment"], ModelWorkBlocked(error_text),
                    {"topic", "survey"}, by_id))
                request = runner.active_research_requests[0]
                self.assertEqual(request["kind"], "additional_experiment")
                self.assertEqual(request["target_stage_id"], "experiment")
                self.assertEqual(request["failure_dossier_ref"], "artifact:failure-dossier")
                self.assertEqual(runner.context["experiment"]["capability_repair_attempts"], 1)
                self.assertEqual(runner.context["topic"]["topic"]["id"], "current")
                self.assertFalse(any(
                    item.get("action") == "pivot_topic_after_experiment_repair_limit"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_unexecuted_capability_can_never_be_forwarded_after_repair_lease(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["progression_policy"] = "forward_first"
            runner = ComposerRunner(workflow)
            try:
                runner.context["experiment"] = {
                    "kind": "experiment", "status": "research_expansion_required",
                    "review_status": "scientific_assignment_blocked",
                    "error": "capability foundry did not admit a program",
                    "capability_repair_attempts": 99,
                    "results_status": "not_executed",
                    "failure_recovery": {"requires_capability_repair": True},
                }
                self.assertFalse(runner._composer_can_advance_after_admission(
                    workflow["stages"][1], ModelWorkBlocked("capability foundry failed")))
            finally:
                runner.close()

    def test_survey_evidence_contract_failure_resumes_only_the_gap_assessment(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"] = ["topic", "survey", "experiment"]
            survey_dir = root / "survey-attempt"
            (survey_dir / "state").mkdir(parents=True)
            (survey_dir / "state" / "control.sqlite").touch()
            (survey_dir / "output").mkdir()
            (survey_dir / "output" / "run.json").write_text(json.dumps({
                "status": "blocked",
                "survey_ref": "artifact:kb/surveys/current@2",
                "survey_current": True,
                "assessment_ref": None,
                "assessment_current": False,
                "nomination": {"id": "topic-old"},
            }))
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "old", "title": "Old direction",
                              "research_question": "Does A change B?"},
                    "topic_evolution": {"mode": "initial", "cycle": 3},
                }
                runner.context["survey"] = {
                    "kind": "survey", "status": "blocked",
                    "error": "gap-assessment did not satisfy its evidence contract",
                }
                runner.stage_records["topic"] = {"kind": "topic_discovery", "status": "completed"}
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "blocked", "attempt_count": 2,
                    "attempts": [{
                        "state": "failed", "project_dir": str(survey_dir),
                        "topic_id": "old", "topic_cycle": 3,
                    }],
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = {"topic"}
                error = ModelWorkBlocked(
                    "survey-gap-assessment did not satisfy its evidence contract: "
                    "unknown or duplicate required check"
                )
                with patch.object(ComposerRunner, "_durable_stage_config", return_value={"model": {}}):
                    self.assertTrue(runner._admit_scientific_blocker_recovery(
                        workflow["stages"][1], error, completed, by_id))
                    resumed = runner._stage_for_cycle(workflow["stages"][1])
                self.assertEqual(Path(resumed["project_dir"]), survey_dir.resolve())
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertEqual(runner.context["topic"]["status"], "completed")
                self.assertEqual(runner.context["survey"]["review_status"], "gap_assessment_resume")
                self.assertEqual(runner.context["survey"]["resume_scope"], "gap_assessment")
                self.assertEqual(
                    runner.context["survey"]["research_requests"][0]["kind"],
                    "literature_expansion",
                )
                self.assertEqual(runner.context["survey"]["research_requests"][0]["target_stage_id"], "survey")
                self.assertEqual(runner.reopened_stage_ids, {"survey", "experiment"})
            finally:
                runner.close()

    def test_failed_survey_acceptance_reopens_only_integrated_review(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"] = ["topic", "survey", "experiment"]
            survey_dir = root / "survey-attempt"
            (survey_dir / "state").mkdir(parents=True)
            (survey_dir / "state" / "control.sqlite").touch()
            (survey_dir / "output").mkdir()
            (survey_dir / "output" / "run.json").write_text(json.dumps({
                "status": "blocked",
                "error": (
                    "ModelWorkBlocked: deterministic aggregate survey review failed: "
                    "W101 is included despite a conflicted bibliographic identity"
                ),
                "survey_ref": "artifact:kb/surveys/current@2",
                "survey_current": False,
                "assessment_ref": None,
                "assessment_current": False,
                "nomination": {"id": "topic-old"},
            }))
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "old", "title": "Old direction",
                              "research_question": "Does A change B?"},
                    "topic_evolution": {"mode": "initial", "cycle": 3},
                }
                runner.context["survey"] = {
                    "kind": "survey", "status": "blocked",
                    "error": (
                        "deterministic aggregate survey review failed: "
                        "W101 is included despite a conflicted bibliographic identity"
                    ),
                }
                runner.stage_records["topic"] = {"kind": "topic_discovery", "status": "completed"}
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "blocked",
                    "attempts": [{
                        "state": "failed", "project_dir": str(survey_dir),
                        "topic_id": "old", "topic_cycle": 3,
                    }],
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                error = ModelWorkBlocked(
                    "deterministic aggregate survey review failed: "
                    "W101 is included despite a conflicted bibliographic identity"
                )
                with patch.object(ComposerRunner, "_durable_stage_config", return_value={"model": {}}):
                    self.assertTrue(runner._admit_scientific_blocker_recovery(
                        workflow["stages"][1], error, {"topic"}, by_id))
                    resumed = runner._stage_for_cycle(workflow["stages"][1])
                self.assertEqual(Path(resumed["project_dir"]), survey_dir.resolve())
                self.assertEqual(runner.context["topic"]["status"], "completed")
                self.assertEqual(runner.context["survey"]["review_status"], "survey_integrity_repair")
                self.assertEqual(runner.context["survey"]["resume_scope"], "integrated_review")
                self.assertEqual(runner.reopened_stage_ids, {"survey", "experiment"})
                self.assertEqual(runner._survey_resume_scope(
                    {"survey_current": False}, stage_context=runner.context["survey"]),
                    "integrated_review")
                self.assertEqual(ComposerRunner._active_stage_role_ids(
                    workflow["stages"][1], stage_context=runner.context["survey"]), [])
                self.assertEqual(runner.context["survey"]["research_requests"][0]["kind"],
                                 "literature_expansion")
            finally:
                runner.close()

    def test_failed_topic_pivot_restores_exact_unassessed_survey_frontier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"] = ["topic", "survey", "experiment"]
            topic_id, topic_cycle = "direction_quantum_heat_test", 12
            topic = {"id": topic_id, "title": "Heat transport test",
                     "research_question": "Does bath memory change heat flux?"}
            topic_evolution = {"mode": "initial", "cycle": topic_cycle}
            admitted_dir = topic_dir / "continuations" / f"cycle-{topic_cycle}" / "attempts" / "attempt-8"
            admitted_dir.mkdir(parents=True)
            (admitted_dir / "topic-discovery.json").write_text(json.dumps({
                "admission_state": "provisional_for_survey",
                "topic": topic, "topic_evolution": topic_evolution,
            }))
            failed_topic_dir = topic_dir / "continuations" / "cycle-13" / "attempts" / "attempt-9"
            failed_topic_dir.mkdir(parents=True)
            survey_project = (Path(workflow["stages"][1]["project_dir"])
                              / "continuations" / f"cycle-{topic_cycle}")
            (survey_project / "state").mkdir(parents=True)
            (survey_project / "state" / "control.sqlite").touch()
            (survey_project / "output").mkdir()
            survey_ref = "artifact:kb/surveys/current@2"
            (survey_project / "output" / "run.json").write_text(json.dumps({
                "status": "blocked", "survey_current": True,
                "survey_ref": survey_ref, "assessment_current": False,
                "assessment_ref": None,
                "nomination": {"id": f"topic-{topic_id}"},
            }))

            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 13
                runner._continuation_budget_baseline = 13
                runner.active_research_requests = [{
                    "id": "auto-topic-pivot-13", "kind": "topic_refinement",
                    "owner": "research.intelligence", "objective": "Try a fresh topic.",
                    "why": "The gap assessment was blocked.",
                    "success_condition": "Admit a new topic.",
                    "evidence_needed": "A changed candidate.",
                    "topic_id": topic_id, "topic_cycle": topic_cycle,
                    "source_stage_id": "topic",
                }]
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "research_expansion_required",
                    "topic": topic, "topic_evolution": topic_evolution,
                    "topic_pivot": {"status": "required", "source_stage_id": "survey"},
                    "research_requests": [{"id": "failed-format-repair", "kind": "recovery"}],
                    "research_expansion_requests": [runner.active_research_requests[0]],
                }
                runner.context["survey"] = {
                    "kind": "survey", "status": "topic_pivot_pending",
                    "error": "gap-assessment did not satisfy its evidence contract",
                    "superseded_topic_id": topic_id,
                }
                runner.context["experiment"] = {
                    "kind": "experiment", "status": "topic_pivot_pending",
                    "superseded_topic_id": topic_id,
                }
                runner.stage_records["topic"] = {
                    "kind": "topic_discovery", "status": "blocked", "attempt_count": 9,
                    "error": "topic discovery repeated the byte-identical rejected response",
                    "attempts": [
                        {"attempt_number": 8, "state": "succeeded",
                         "project_dir": str(admitted_dir)},
                        {"attempt_number": 9, "state": "failed",
                         "failure_class": "model_contract", "project_dir": str(failed_topic_dir)},
                    ],
                }
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "retrying", "attempt_count": 3,
                    "superseded_topic_id": topic_id,
                    "lineage_state": "awaiting_topic_admission",
                    "attempts": [{
                        "attempt_number": 3, "state": "failed",
                        "failure_class": "model_contract",
                        "project_dir": str(survey_project),
                        "topic_id": topic_id, "topic_cycle": topic_cycle,
                        "error": "gap-assessment output was truncated",
                    }],
                }
                runner.stage_records["experiment"] = {
                    "kind": "experiment", "status": "retrying",
                    "superseded_topic_id": topic_id,
                    "lineage_state": "awaiting_topic_admission",
                }
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = set()
                with patch.object(ComposerRunner, "_durable_stage_config",
                                  return_value={"model": {}}):
                    self.assertTrue(runner._restore_retained_survey_frontier_after_failed_topic_pivot(
                        completed, by_id))
                self.assertIn("topic", completed)
                self.assertEqual(runner.context["topic"]["status"], "completed")
                self.assertNotIn("topic_pivot", runner.context["topic"])
                survey_context = runner.context["survey"]
                self.assertEqual(survey_context["project_dir"], str(survey_project.resolve()))
                self.assertEqual(survey_context["survey_ref"], survey_ref)
                self.assertEqual(survey_context["research_requests"][0]["kind"], "literature_expansion")
                self.assertEqual(survey_context["review_status"], "gap_assessment_resume")
                self.assertEqual(survey_context["resume_scope"], "gap_assessment")

                self.assertTrue(runner._begin_continuation(completed, by_id))
                self.assertEqual(runner.active_research_requests[0]["kind"], "literature_expansion")
                self.assertEqual(runner.reopened_stage_ids, {"survey", "experiment"})
                resumed_survey = runner._stage_for_cycle(by_id["survey"])
                self.assertEqual(Path(resumed_survey["project_dir"]), survey_project.resolve())
                self.assertFalse(any(
                    item.get("kind") == "topic_refinement"
                    for item in runner.active_research_requests
                ))
            finally:
                runner.close()

    def test_rejected_off_lineage_checkpoint_restores_current_evidence_and_repairs_experiment(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            for stage_id, kind, dependency in (
                    ("interpretation", "interpretation", "experiment"),
                    ("argument", "argument", "interpretation"),
                    ("paper", "paper", "argument")):
                stage_dir = root / stage_id
                stage_dir.mkdir()
                workflow["stages"].append({
                    "id": stage_id, "kind": kind,
                    "config_path": workflow["stages"][0]["config_path"],
                    "project_dir": str(stage_dir.resolve()), "depends_on": [dependency],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                })
            workflow["completion"]["required_stage_ids"] = ["paper"]
            workflow["progression_policy"] = "forward_first"
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}

            topic_id, topic_cycle = "direction_3", 17
            topic = {
                "id": topic_id,
                "title": "Onset-Detection Operator Sensitivity",
                "phenomenon": "Shear thickening onset in confined suspensions",
                "research_question": "Does the measured onset slope depend on operator?",
                "domain": "soft matter physics",
            }
            evolution = {"mode": "refinement", "cycle": topic_cycle,
                         "parent_topic_id": topic_id}
            accepted_topic_dir = topic_dir / "attempts" / "attempt-17"
            accepted_topic_dir.mkdir(parents=True)
            (accepted_topic_dir / "topic-discovery.json").write_text(json.dumps({
                "schema_version": "topic-discovery-1", "status": "completed",
                "admission_state": "provisional_for_survey", "topic": topic,
                "topic_evolution": evolution,
            }))

            survey_project = root / "survey" / "continuations" / f"cycle-{topic_cycle}"
            survey_project.mkdir(parents=True)
            survey_control = ControlStore(survey_project)
            survey_store = ArtifactStore(survey_control)
            survey_store.init_project(principal_note="test survey")
            try:
                survey_manifest = survey_store.publish_artifact(
                    logical_id="kb/surveys/current", artifact_type="report",
                    author="research.survey", body=canonical_bytes({
                        "schema_version": "literature-survey-1", "status": "completed",
                    }))
                assessment_body = {
                    "state": "insufficient_evidence",
                    "survey_ref": survey_manifest["artifact_ref"],
                    "rationale": "The available literature does not establish this specific gap.",
                    "checks": [{"outcome": "insufficient_evidence"}],
                }
                assessment_manifest = survey_store.publish_artifact(
                    logical_id="kb/gap-assessments/current", artifact_type="report",
                    author="research.survey", body=canonical_bytes(assessment_body))
                (survey_project / "output").mkdir()
                (survey_project / "output" / "run.json").write_text(json.dumps({
                    "status": "completed", "survey_current": True,
                    "assessment_current": True, "gap_state": "insufficient_evidence",
                    "survey_ref": survey_manifest["artifact_ref"],
                    "assessment_ref": assessment_manifest["artifact_ref"],
                    "nomination": {"id": f"topic-{topic_id}",
                                   "statement": "An exploratory operator-sensitivity question."},
                    "coverage": {"unique_works": 42, "verified_full_texts": 3},
                }))
                (survey_project / "output" / "gap-assessment.json").write_text(
                    json.dumps(assessment_body))

                low_coverage_project = survey_project / "attempt-low-coverage"
                low_coverage_project.mkdir()
                low_coverage_control = ControlStore(low_coverage_project)
                try:
                    low_coverage_store = ArtifactStore(low_coverage_control)
                    low_coverage_store.init_project(principal_note="lower coverage survey")
                    low_survey = low_coverage_store.publish_artifact(
                        logical_id="kb/surveys/current", artifact_type="report",
                        author="research.survey", body=canonical_bytes({
                            "schema_version": "literature-survey-1", "status": "completed",
                        }))
                    low_assessment_body = {
                        **assessment_body, "survey_ref": low_survey["artifact_ref"],
                    }
                    low_assessment = low_coverage_store.publish_artifact(
                        logical_id="kb/gap-assessments/current", artifact_type="report",
                        author="research.survey", body=canonical_bytes(low_assessment_body))
                    (low_coverage_project / "output").mkdir()
                    (low_coverage_project / "output" / "run.json").write_text(json.dumps({
                        "status": "completed", "survey_current": True,
                        "assessment_current": True, "gap_state": "insufficient_evidence",
                        "survey_ref": low_survey["artifact_ref"],
                        "assessment_ref": low_assessment["artifact_ref"],
                        "nomination": {"id": f"topic-{topic_id}"},
                        "coverage": {"unique_works": 9, "verified_full_texts": 0},
                    }))
                    (low_coverage_project / "output" / "gap-assessment.json").write_text(
                        json.dumps(low_assessment_body))
                finally:
                    low_coverage_control.close()

                runner = ComposerRunner(workflow)
                try:
                    runner.continuation_cycles = 18
                    runner._continuation_budget_baseline = 18
                    error = "independent recalculation rejected three reported slopes"
                    dossier = {
                        "schema_version": "composer-failure-recovery-1",
                        "stage_id": "experiment", "attempt_number": 21,
                        "failure_class": "experiment_failure", "error": error,
                        "input_sha256": "a" * 64,
                        "repair_commands": [{
                            "id": "repair-source", "operation": "edit_program",
                            "instruction": "Fix the estimator from the raw observations.",
                        }],
                        "acceptance_checks": ["Independently recalculate every primary metric."],
                        "review_directives": [{"text": "Use a fresh execution namespace."}],
                        "model_diagnostics": {"metric_mismatches": [{"id": "slope", "matches": False}]},
                        "observed_result": {"results_status": "not_executed"},
                    }
                    dossier_manifest = runner._publish(
                        "command/composer/failure-recovery/experiment/attempt-21",
                        "report", dossier, "command.composer")
                    older_dossier = {
                        **dossier,
                        "attempt_number": 20,
                        "error": "the prior experiment program review was incomplete",
                        "input_sha256": "b" * 64,
                    }
                    older_dossier_manifest = runner._publish(
                        "command/composer/failure-recovery/experiment/attempt-20",
                        "report", older_dossier, "command.composer")
                    runner.context = {
                        "topic": {
                            "stage_id": "topic", "kind": "topic_discovery",
                            "status": "format_recovery_required", "topic": topic,
                            "topic_evolution": evolution, "attempt_number": 17,
                            "project_dir": str(accepted_topic_dir),
                            "admission_state": "provisional_for_survey",
                            "topic_pivot": {
                                "cycle": 19, "status": "required",
                                "source_stage_id": "experiment",
                                "reason": "experiment capability produced no observation after 3 source-level repairs",
                            },
                            "format_recovery": True,
                            "research_requests": [{"id": "stale-topic-retry"}],
                        },
                        "survey": {"kind": "survey", "status": "topic_pivot_pending",
                                   "superseded_topic_id": topic_id},
                        "experiment": {
                            "kind": "experiment", "status": "topic_pivot_pending",
                            "superseded_scope_failure": {
                                "failure_class": "experiment_failure",
                                "failure_dossier_ref": dossier_manifest["artifact_ref"],
                                "pivot_cycle": 19,
                                "source_stage_id": "experiment",
                            },
                        },
                    }
                    runner.stage_records = {
                        "topic": {
                            "kind": "topic_discovery", "status": "blocked",
                            "attempt_count": 18,
                            "attempts": [
                                {"attempt_number": 17, "cycle": 17, "state": "succeeded",
                                 "project_dir": str(accepted_topic_dir)},
                                {"attempt_number": 18, "cycle": 19, "state": "failed",
                                 "failure_class": "model_contract",
                                 "project_dir": str(topic_dir / "attempts" / "attempt-18")},
                            ],
                        },
                        "survey": {
                            "kind": "survey", "status": "retrying", "attempt_count": 4,
                            "superseded_topic_id": topic_id,
                            "attempts": [{
                                "attempt_number": 4, "cycle": topic_cycle,
                                "state": "succeeded", "topic_id": topic_id,
                                "topic_cycle": topic_cycle,
                                "project_dir": str(survey_project),
                            }, {
                                "attempt_number": 5, "cycle": topic_cycle,
                                "state": "succeeded", "topic_id": topic_id,
                                "topic_cycle": topic_cycle,
                                "project_dir": str(low_coverage_project),
                            }],
                        },
                        "experiment": {
                            "kind": "experiment", "status": "retrying", "attempt_count": 21,
                            "superseded_topic_id": topic_id,
                            "attempts": [{
                                "attempt_number": 20, "cycle": 17,
                                "state": "failed", "topic_id": topic_id,
                                "topic_cycle": topic_cycle,
                                "project_dir": str(root / "experiment" / "attempt-20"),
                                "failure_dossier_ref": older_dossier_manifest["artifact_ref"],
                            }, {
                                "attempt_number": 21, "cycle": 18,
                                "state": "failed", "topic_id": topic_id,
                                "topic_cycle": topic_cycle,
                                "project_dir": str(root / "experiment" / "attempt-21"),
                                "failure_dossier_ref": dossier_manifest["artifact_ref"],
                            }],
                        },
                    }
                    runner.department_activity = [{
                        "action": "schedule_pre_execution_capability_repair",
                        "stage_id": "experiment", "cycle": 17,
                        "repair_attempts": 2,
                        "failure_dossier_ref": older_dossier_manifest["artifact_ref"],
                    }, {
                        "action": "schedule_pre_execution_capability_repair",
                        "stage_id": "experiment", "cycle": 18,
                        "repair_attempts": 3,
                        "failure_dossier_ref": dossier_manifest["artifact_ref"],
                    }]
                    runner._restored_topic_lineage_reconciliation = {
                        "action": "reject_unjustified_topic_refinement_checkpoint",
                        "retained_topic_id": topic_id,
                        "rejected_topic_id": "direction_1",
                    }
                    completed = set()
                    by_id = {stage["id"]: stage for stage in workflow["stages"]}
                    self.assertTrue(runner._restore_unjustified_refinement_frontier(
                        completed, by_id))

                    self.assertEqual(runner.context["topic"]["topic"]["id"], topic_id)
                    self.assertEqual(runner.context["topic"]["status"], "completed")
                    self.assertNotIn("topic_pivot", runner.context["topic"])
                    self.assertIn("topic_pivot", runner.context["topic"]["deferred_topic_state"])
                    self.assertIn("topic", completed)
                    self.assertIn("survey", completed)
                    survey_context = runner.context["survey"]
                    self.assertEqual(survey_context["gap_state"], "insufficient_evidence")
                    self.assertEqual(survey_context["topic_admission"], "exploratory_pilot")
                    self.assertIn(
                        "Literature novelty remains unresolved; exploratory results cannot establish an original contribution.",
                        survey_context["carried_maturity_requirements"],
                    )
                    self.assertFalse(survey_context["scientific_limitations"][
                        "novelty_claim_authorized"])
                    self.assertEqual(survey_context["survey_ref"], survey_manifest["artifact_ref"])
                    self.assertEqual(survey_context["assessment_ref"],
                                     assessment_manifest["artifact_ref"])
                    self.assertEqual(survey_context["project_dir"],
                                     str(survey_project.resolve()))
                    request = runner.active_research_requests[0]
                    self.assertEqual(request["kind"], "additional_experiment")
                    self.assertEqual(request["owner"], "methods.validation")
                    self.assertEqual(request["failure_dossier_ref"],
                                     dossier_manifest["artifact_ref"])
                    self.assertEqual(
                        request["experiment_repair_plan"]["lineage"][
                            "prior_capability_repair_attempts"], 3)
                    self.assertEqual(runner.reopened_stage_ids,
                                     {"experiment", "interpretation", "argument", "paper"})
                    self.assertEqual(runner.context["paper"]["topic_lineage"], {
                        "topic_id": topic_id, "topic_cycle": topic_cycle,
                    })
                    self.assertTrue(any(
                        item.get("action") == "restore_verified_frontier_and_repair_experiment"
                        for item in runner.department_activity
                    ))

                    gate_refs = []
                    for number in (22, 23):
                        gate_dossier = {
                            "schema_version": "composer-failure-recovery-1",
                            "stage_id": "experiment", "attempt_number": number,
                            "failure_class": "experiment_failure",
                            "error": LEGACY_EXPERIMENT_SURVEY_ADMISSION_ERROR,
                            "input_sha256": str(number) * 64,
                            "repair_commands": [{
                                "id": "must-not-be-used-as-methods-repair",
                                "operation": "inspect",
                                "instruction": "The literature admission gate was not met.",
                            }],
                            "acceptance_checks": ["This is not experiment evidence."],
                            "review_directives": [{"text": "Do not alter the research program."}],
                        }
                        gate_manifest = runner._publish(
                            f"command/composer/failure-recovery/experiment/attempt-{number}",
                            "report", gate_dossier, "command.composer")
                        gate_refs.append(gate_manifest["artifact_ref"])
                        runner.stage_records["experiment"]["attempts"].append({
                            "attempt_number": number, "cycle": number,
                            "state": "failed", "topic_id": topic_id,
                            "topic_cycle": topic_cycle,
                            "failure_dossier_ref": gate_manifest["artifact_ref"],
                            "error": f"ValidationError: {LEGACY_EXPERIMENT_SURVEY_ADMISSION_ERROR}",
                            "usage": {"model_calls": 4},
                        })
                        runner.department_activity.append({
                            "action": "schedule_pre_execution_capability_repair",
                            "stage_id": "experiment", "cycle": number,
                            "repair_attempts": number - 18,
                            "failure_dossier_ref": gate_manifest["artifact_ref"],
                        })
                    runner.context["experiment"].update({
                        "error": LEGACY_EXPERIMENT_SURVEY_ADMISSION_ERROR,
                        "failure_dossier_ref": gate_refs[-1],
                        "superseded_scope_failure": {
                            "failure_dossier_ref": older_dossier_manifest["artifact_ref"],
                        },
                    })
                    runner.stage_records["survey"]["attempts"].append({
                        "attempt_number": 6,
                        "cycle": runner.continuation_cycles,
                        "state": "succeeded",
                        "topic_id": topic_id,
                        "topic_cycle": topic_cycle,
                        "project_dir": str(low_coverage_project),
                    })
                    runner.active_research_requests = [{
                        "id": "invalid-admission-repair",
                        "kind": "additional_experiment",
                        "owner": "methods.validation",
                        "target_stage_id": "experiment",
                        "failure_dossier_ref": gate_refs[-1],
                    }, {
                        "id": "survey-expansion-already-attempted",
                        "kind": "literature_expansion",
                        "owner": "research.intelligence",
                        "target_stage_id": None,
                    }]
                    runner.context["survey"].pop("restored_frontier", None)
                    runner.context["survey"]["status"] = "candidate_needs_review"
                    runner._restored_topic_lineage_reconciliation = None
                    self.assertTrue(runner._restore_unjustified_refinement_frontier(
                        completed, by_id))
                    self.assertEqual(
                        runner.context["experiment"]["failure_dossier_ref"],
                        dossier_manifest["artifact_ref"],
                    )
                    self.assertEqual(
                        runner.context["experiment"]["superseded_failure_pointer"]["reference"],
                        older_dossier_manifest["artifact_ref"],
                    )
                    self.assertEqual(runner.context["experiment"][
                        "capability_repair_attempts"], 4)
                    self.assertEqual(runner.context["experiment"][
                        "experiment_repair_plan"]["lineage"][
                            "prior_capability_repair_attempts"], 3)
                    self.assertEqual(
                        runner.context["experiment"]["quarantined_admission_gate_retries"]
                        and [item["attempt_number"] for item in runner.context["experiment"][
                            "quarantined_admission_gate_retries"]],
                        [22, 23],
                    )
                    self.assertEqual(
                        runner.context["experiment"]["quarantined_admission_gate_retries"][-1][
                            "actual_usage"]["model_calls"], 4)
                    self.assertTrue(all(
                        item.get("failure_dossier_ref") not in gate_refs
                        for item in runner.active_research_requests
                        if isinstance(item, dict)
                    ))
                    self.assertFalse(any(
                        item.get("id") == "survey-expansion-already-attempted"
                        for item in runner.active_research_requests
                        if isinstance(item, dict)
                    ))
                    self.assertEqual(
                        runner.context["survey"]["deferred_research_requests"][0]["id"],
                        "survey-expansion-already-attempted",
                    )

                    runner.context["experiment"].update({
                        "error": older_dossier["error"],
                        "failure_dossier_ref": older_dossier_manifest["artifact_ref"],
                    })
                    runner.stage_records["experiment"]["failure_dossier_ref"] = (
                        older_dossier_manifest["artifact_ref"])
                    runner.active_research_requests = [{
                        "id": "stale-methods-pointer",
                        "kind": "additional_experiment",
                        "owner": "methods.validation",
                        "target_stage_id": "experiment",
                        "failure_dossier_ref": older_dossier_manifest["artifact_ref"],
                    }]
                    runner._restored_topic_lineage_reconciliation = None
                    self.assertTrue(runner._restore_unjustified_refinement_frontier(
                        completed, by_id))
                    self.assertEqual(
                        runner.context["experiment"]["failure_dossier_ref"],
                        dossier_manifest["artifact_ref"],
                    )
                finally:
                    runner.close()
            finally:
                survey_control.close()

    def test_resume_reconciles_interrupted_survey_contract_before_dispatch(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"] = ["topic", "survey", "experiment"]
            survey_dir = root / "survey-attempt"
            (survey_dir / "state").mkdir(parents=True)
            (survey_dir / "state" / "control.sqlite").touch()
            (survey_dir / "output").mkdir()
            (survey_dir / "output" / "run.json").write_text(json.dumps({
                "status": "blocked",
                "survey_ref": "artifact:kb/surveys/current@2",
                "survey_current": True,
                "assessment_ref": None,
                "assessment_current": False,
                "nomination": {"id": "topic-old"},
            }))
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 9
                runner.continuation_pending_stage_ids = {"survey", "experiment"}
                runner.context["topic"] = {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": {"id": "old", "title": "Old direction",
                              "research_question": "Does A change B?"},
                    "topic_evolution": {"mode": "initial", "cycle": 3},
                }
                runner.context["survey"] = {
                    "kind": "survey", "status": "research_expansion_required",
                    "project_dir": str(survey_dir),
                    "error": (
                        "Unchanged survey input failed 1 time(s): ModelWorkBlocked: "
                        "gap-assessment did not satisfy its evidence contract: "
                        "model generation did not finish normally: length"
                    ),
                }
                runner.stage_records["topic"] = {"kind": "topic_discovery", "status": "completed"}
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "running",
                    "project_dir": str(survey_dir),
                    "attempts": [{"state": "failed", "project_dir": str(survey_dir),
                                  "topic_id": "old", "topic_cycle": 3}],
                }
                runner.active_research_requests = []
                by_id = {stage["id"]: stage for stage in workflow["stages"]}
                completed = {"topic"}
                with patch.object(ComposerRunner, "_durable_stage_config", return_value={"model": {}}):
                    self.assertTrue(runner._resume_stale_survey_contract_repair(completed, by_id))
                self.assertEqual(runner.continuation_cycles, 10)
                self.assertEqual(runner.context["topic"]["status"], "completed")
                self.assertEqual(
                    runner.context["survey"]["research_requests"][0]["kind"],
                    "literature_expansion",
                )
                self.assertEqual(runner.context["survey"]["resume_scope"], "gap_assessment")
            finally:
                runner.close()

    def test_continuation_fences_stale_stage_aggregate_and_unknown_attempt(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                aggregate = runner._stage_task(workflow["stages"][0])
                aggregate_attempt = f"{aggregate['task_id']}-attempt"
                runner.tasks.start_attempt(
                    aggregate["task_id"], aggregate_attempt, owner="command.composer",
                    lease_ttl_seconds=60, payload={"stage_id": "survey"})
                specialist_id = "composer-demo-workflow-survey-specialist"
                runner.tasks.create(
                    specialist_id, "production",
                    {"stage_id": "survey", "assignment_id": "specialist-1"},
                    "research.search-strategist")
                runner.tasks.admit(specialist_id, "command.composer")
                specialist_attempt = f"{specialist_id}-attempt"
                runner.tasks.start_attempt(
                    specialist_id, specialist_attempt, owner="research.search-strategist",
                    lease_ttl_seconds=60, payload={"assignment_id": "specialist-1"})

                retired = runner._retire_superseded_stage_tasks(
                    {"survey"}, reason="superseded by a newly admitted continuation")

                self.assertEqual(retired, [{
                    "task_id": aggregate["task_id"],
                    "stage_id": "survey",
                    "state": "stale",
                }])
                self.assertEqual(runner.tasks.get(aggregate["task_id"])["state"], "stale")
                self.assertEqual(
                    runner.tasks.get_attempt(aggregate_attempt)["state"], "result_unknown")
                self.assertEqual(runner.tasks.get(specialist_id)["state"], "running")
                self.assertEqual(
                    runner.tasks.get_attempt(specialist_attempt)["state"], "started")
            finally:
                runner.close()

    def test_known_topic_contract_debt_is_eligible_for_one_resume_repair(self):
        stage = {"kind": "topic_discovery"}
        record = {
            "status": "candidate_needs_review",
            "failure_debt": {
                "failure_class": "mechanical_contract",
                "error": "ValidationError: feasibility_plan.project_artifact must declare a project_artifact input",
            },
        }
        self.assertTrue(
            ComposerRunner._release_blocked_topic_contract_retry_allowed(stage, record))
        record["contract_recovery_admitted"] = True
        self.assertFalse(
            ComposerRunner._release_blocked_topic_contract_retry_allowed(stage, record))

    def test_experiment_recovery_specialists_receive_a_bounded_scientific_brief(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "question": "Does the diagnostic survive the finite-size boundary?",
                    "feasibility_check": {"plan": {
                        "execution_mode": "foundry",
                        "required_executables": ["python3"],
                        "required_packages": ["numpy"],
                        "network_access": False,
                        "evidence_inputs": [{"source": "W1"}],
                    }},
                    "topic": {
                        "id": "direction_0",
                        "research_question": "Does the diagnostic survive the finite-size boundary?",
                        "hypothesis": "The contrast weakens below a boundary.",
                        "comparison": "diagnostic A versus diagnostic B",
                        "measurement": "difference in predictive power",
                        "disconfirmation_test": "No size-dependent difference.",
                        "resource_plan": "Deterministic finite-matrix simulation.",
                    },
                    "research_program": {"branches": [{
                        "id": "direction_0",
                        "hypothesis": "The contrast weakens below a boundary.",
                    }]},
                }
                stage = next(item for item in workflow["stages"] if item["id"] == "experiment")
                packet = runner._specialist_stage_packet(
                    stage, {"experiment": {"primary_outcomes": [{"id": "gap"}]}})
                assignment = {
                    "assigned_role": "methods.methodologist",
                    "model_role": "methods.methodologist",
                    "stage_id": "experiment",
                    "stage_kind": "experiment",
                    "role_id": "methodologist",
                    "system_contract": "Design a falsifiable experiment.",
                    "input_projection": ["research_question", "hypotheses",
                                          "method_constraints", "available_assets"],
                    "quota": {"max_input_tokens": 12000},
                }
                prompt = json.loads(build_specialist_prompt(assignment, packet))
                projected = prompt["projected_input"]
                self.assertEqual(projected["research_question"],
                                 "Does the diagnostic survive the finite-size boundary?")
                self.assertEqual(projected["hypotheses"],
                                 "The contrast weakens below a boundary.")
                self.assertEqual(projected["available_assets"]["required_packages"], ["numpy"])
                self.assertEqual(prompt["shared_stage_context"]["stage_kind"], "experiment")
            finally:
                runner.close()

    def test_experiment_recovery_brief_carries_prior_failure_and_review_evidence(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                def source_blob(label, size):
                    prefix = f"# {label} source evidence\n"
                    return prefix + label[0].lower() * (size - len(prefix))

                executor_source = source_blob("EXECUTOR", 32_706)
                validator_source = source_blob("VALIDATOR", 19_885)
                source_integrity = {
                    "executor": {"sha256": hashlib.sha256(
                        executor_source.encode("utf-8")).hexdigest()},
                    "validator": {"sha256": hashlib.sha256(
                        validator_source.encode("utf-8")).hexdigest()},
                }
                foundry_cache = runner.store.publish_artifact(
                    logical_id="command/foundry-work/experiment-attempt-3",
                    artifact_type="note",
                    author="command.controller",
                    media_type="application/json",
                    body=json.dumps({
                        "status": "failed",
                        "last_attempt": {
                            "experiment_intent": {"hypothesis": "negative slope"},
                            "source_integrity": source_integrity,
                            "executor_source": executor_source,
                            "validator_source": validator_source,
                            "runtime": {"command": "python program.py"},
                            "test_input": {"replicates": 8},
                        },
                        "validation_feedback": {
                            "decision": "rejected",
                            "failed_checks": [{"id": "sign", "outcome": "failed"}],
                        },
                        "validation_context": {"observation_count": 24},
                        "last_response": {
                            "model": "test-model", "finish_reason": "stop",
                            "request_attempts": 1, "text": "response-" + "z" * 11_950,
                            "usage": {"input_tokens": 200, "output_tokens": 80},
                        },
                    }).encode("utf-8"),
                )
                failure_dossier = runner.store.publish_artifact(
                    logical_id="command/failure-recovery/experiment-attempt-3",
                    artifact_type="report",
                    author="command.composer",
                    media_type="application/json",
                    body=json.dumps({
                        "stage_id": "experiment",
                        "attempt_number": 3,
                        "failure_class": "experiment_failure",
                        "input_sha256": "a" * 64,
                        "error": "executor failed: mechanism inactive on all cells",
                        "observed_result": {
                            "status": "blocked",
                            "error": "the sign constraint failed",
                        },
                        "acceptance_checks": ["independent recalculation"],
                        "repair_commands": ["Repair executor and validator together."],
                        "review_directives": [{
                            "text": "Correct the sign convention in both executor and validator."
                        }],
                        "program_snapshot": [],
                        "foundry_work_snapshot": {
                            "feedback": "Independent slopes were positive.",
                            "cache_ref": foundry_cache["artifact_ref"],
                            "last_attempt": {
                                "experiment_intent": {"hypothesis": "negative slope"},
                                "source_integrity": source_integrity,
                                "executor_source": executor_source[:18_014],
                                "validator_source": validator_source[:12_000],
                            },
                        },
                    }).encode("utf-8"),
                )
                failure_dossier_ref = failure_dossier["artifact_ref"]
                runner.stage_records["experiment"] = {
                    "kind": "experiment",
                    "attempts": [{
                        "attempt_number": 3,
                        "failure_dossier_ref": failure_dossier_ref,
                    }],
                }
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "topic": {
                        "id": "direction_0",
                        "research_question": "Does the declared mechanism change the onset slope?",
                        "hypothesis": "The declared mechanism produces a negative slope.",
                        "comparison": "Two onset operators",
                        "measurement": "Log-log onset slope",
                        "disconfirmation_test": "The slope is positive under the declared convention.",
                    },
                }
                runner.context["experiment"] = {
                    "kind": "experiment",
                    "status": "research_expansion_required",
                    "error": "executor failed: mechanism inactive on all cells",
                    "failure_dossier_ref": failure_dossier_ref,
                    "failure_recovery": {
                        "failure_class": "experiment_failure",
                        "dossier_ref": failure_dossier_ref,
                        "attempt_number": 3,
                        "review_directives": [{
                            "text": "Correct the sign convention in both executor and validator."
                        }],
                    },
                    "experiment_repair_plan": {
                        "design_axis": "review_directed",
                        "root_causes": ["The executable sign contradicts the declared mechanism."],
                        "required_changes": ["Correct executor and validator together."],
                    },
                    "capability_repair_panel": {
                        "decision": "repair",
                        "root_causes": ["The executable uses the opposite sign from the intent."],
                        "required_changes": ["Change the executor and validator sign convention together."],
                        "verifier": {
                            "decision": "hold",
                            "critical_findings": ["The accepted mechanism is not exercised."],
                            "repair_scope": ["Re-run the exact grid after the source repair."],
                        },
                    },
                }
                stage = next(item for item in runner.workflow["stages"]
                             if item["id"] == "experiment")
                packet = runner._specialist_stage_packet(stage, {"experiment": {}})
                evidence = packet["failure_evidence"]
                self.assertEqual(evidence["failure_dossier_ref"], failure_dossier_ref)
                dossier_evidence = evidence["failure_dossier"]
                self.assertTrue(dossier_evidence["available"])
                self.assertEqual(dossier_evidence["artifact_body_sha256"],
                                 failure_dossier["body_hash"])
                self.assertEqual(dossier_evidence["input_sha256"], "a" * 64)
                self.assertTrue(dossier_evidence["foundry_work_body_verified"])
                executor_record = dossier_evidence["source_files"]["executor"]
                validator_record = dossier_evidence["source_files"]["validator"]
                self.assertEqual("".join(executor_record["source_chunks"]), executor_source)
                self.assertEqual("".join(validator_record["source_chunks"]), validator_source)
                self.assertTrue(executor_record["matches_expected"])
                self.assertTrue(validator_record["matches_expected"])
                self.assertLessEqual(max(map(len, executor_record["source_chunks"])), 7000)
                self.assertEqual(dossier_evidence["last_response"]["text_characters"], 11_959)
                self.assertNotIn("text", dossier_evidence["last_response"])
                self.assertIn("opposite sign", " ".join(
                    evidence["latest_methods_panel"]["root_causes"]))
                self.assertIn("validator sign convention", " ".join(
                    evidence["latest_methods_panel"]["required_changes"]))

                assignment = {
                    "assigned_role": "methods.reproducibility-reviewer",
                    "model_role": "methods.reproducibility-reviewer",
                    "stage_id": "experiment",
                    "stage_kind": "experiment",
                    "role_id": "reproducibility-reviewer",
                    "system_contract": "Use the recorded failure evidence.",
                    "input_projection": ["execution_manifest", "failure_evidence"],
                    "quota": {"max_input_tokens": 245760},
                }
                prompt_body = build_specialist_prompt(assignment, packet)
                prompt = json.loads(prompt_body)
                projected = prompt["projected_input"]["failure_evidence"]
                self.assertEqual(projected["failure_dossier_ref"], failure_dossier_ref)
                self.assertNotIn("truncated_context", prompt)
                self.assertEqual("".join(projected["failure_dossier"]["source_files"]
                                         ["executor"]["source_chunks"]), executor_source)
                self.assertEqual("".join(projected["failure_dossier"]["source_files"]
                                         ["validator"]["source_chunks"]), validator_source)
                self.assertIn("opposite sign", " ".join(
                    projected["latest_methods_panel"]["root_causes"]))

                mismatched_attempt = runner._failure_dossier_evidence(
                    failure_dossier_ref, expected_stage_id="experiment",
                    expected_attempt_number=4)
                self.assertFalse(mismatched_attempt["available"])
                self.assertEqual(mismatched_attempt["observed_attempt_number"], 3)
                with patch.object(runner.store, "read_body", return_value=b"{}"):
                    corrupted_dossier = runner._failure_dossier_evidence(
                        failure_dossier_ref, expected_stage_id="experiment",
                        expected_attempt_number=3)
                self.assertFalse(corrupted_dossier["available"])

                read_body = runner.store.read_body

                def corrupt_foundry_body(body_hash):
                    if body_hash == foundry_cache["body_hash"]:
                        return b"{}"
                    return read_body(body_hash)

                with patch.object(runner.store, "read_body", side_effect=corrupt_foundry_body):
                    unverified_cache = runner._failure_dossier_evidence(
                        failure_dossier_ref, expected_stage_id="experiment",
                        expected_attempt_number=3)
                self.assertTrue(unverified_cache["available"])
                self.assertFalse(unverified_cache["foundry_work_body_verified"])
                self.assertFalse(unverified_cache["source_files"]["executor"]["available"])
            finally:
                runner.close()

    def test_experiment_repair_follows_panel_evidence_instead_of_rotating_axes(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            try:
                stage = next(item for item in runner.workflow["stages"]
                             if item["id"] == "experiment")
                panel = {
                    "root_causes": [
                        "The generated phi_c expression reverses the sign declared by the topic."
                    ],
                    "required_changes": [
                        "Correct the sign in executor and validator, then verify active cells."
                    ],
                    "verifier": {
                        "decision": "hold",
                        "critical_findings": [],
                        "repair_scope": ["Keep the question and repair the source mechanism."],
                    },
                }
                context = {
                    "failure_recovery": {
                        "failure_class": "experiment_failure",
                        "dossier_ref": "artifact:failure/experiment@3",
                        "input_sha256": "a" * 64,
                    },
                    "capability_repair_panel": panel,
                }
                request = runner._autonomous_experiment_repair_request(
                    stage, context, "dilation term inactive in every cell", 61)
                self.assertEqual(request["repair_strategy"], "review_directed")
                self.assertEqual(request["experiment_repair_plan"]["design_axis"],
                                 "review_directed")
                self.assertIn("Correct the sign in executor and validator",
                              request["objective"])

                stale = {
                    "kind": "additional_experiment",
                    "target_stage_kind": "experiment",
                    "objective": "Change only the estimand axis.",
                    "repair_strategy": "estimand",
                    "experiment_repair_plan": {"design_axis": "estimand"},
                }
                projection = ComposerRunner._capability_authoring_follow_up_projection(
                    [stale], repair_context=panel)[0]
                self.assertEqual(projection["repair_strategy"], "review_directed")
                self.assertNotIn("Change only the estimand axis.", projection["objective"])
                self.assertIn("Correct the sign in executor and validator",
                              projection["objective"])
            finally:
                runner.close()

    def test_experiment_specialist_brief_does_not_leak_an_unrelated_template(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                topic_executor = root / "finite_size_executor.py"
                topic_validator = root / "finite_size_validator.py"
                topic_executor.write_text("print('finite-size experiment')\n")
                topic_validator.write_text("print('finite-size validation')\n")
                capability_path = root / "finite-size-capability.json"
                capability_path.write_text(json.dumps({
                    "schema_version": "experiment-capability-1",
                    "capability_id": "finite_size_winding_breakdown",
                    "experiment": {
                        "id": "finite_size_winding_breakdown",
                        "research_question": "Does the diagnostic survive the finite-size boundary?",
                        "study_type": "exploratory",
                        "method": "Seeded finite-matrix simulation.",
                        "parameters": {"sizes": [16, 32]},
                        "primary_outcomes": [{"id": "rho_l16", "unit": "dimensionless"}],
                        "stopping_rule": "Run the declared grid exactly once.",
                        "limitations": ["Only the declared finite-size grid is covered."],
                        "execution": {
                            "environment_files": [str(topic_executor.resolve())],
                            "client": {"command": ["python3", str(topic_executor.resolve())]},
                        },
                        "validation": {
                            "environment_files": [str(topic_validator.resolve())],
                            "client": {"command": ["python3", str(topic_validator.resolve())]},
                        },
                    },
                }))
                unrelated_executor = root / "robust_mean_study.py"
                unrelated_validator = root / "validate_robust_mean.py"
                unrelated_executor.write_text("print('unrelated pilot')\n")
                unrelated_validator.write_text("print('unrelated validator')\n")
                unrelated_capability_path = root / "robust-mean-capability.json"
                unrelated_capability_path.write_text(json.dumps({
                    "schema_version": "experiment-capability-1",
                    "capability_id": "robust_mean_pilot",
                    "experiment": {
                        "id": "robust_mean_pilot",
                        "research_question": "Does median-of-means reduce contaminated tail error?",
                        "execution": {
                            "environment_files": [str(unrelated_executor.resolve())],
                        },
                        "validation": {
                            "environment_files": [str(unrelated_validator.resolve())],
                        },
                    },
                }))
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "question": "Does the diagnostic survive the finite-size boundary?",
                    "topic": {
                        "id": "direction_0",
                        "experiment_capability_id": "finite_size_winding_breakdown",
                        "research_question": "Does the diagnostic survive the finite-size boundary?",
                        "hypothesis": "The contrast weakens below a boundary.",
                        "comparison": "diagnostic A versus diagnostic B",
                        "measurement": "difference in predictive power",
                        "disconfirmation_test": "No size-dependent difference.",
                    },
                    "generated_capability": {
                        "capability_id": "finite_size_winding_breakdown",
                        "descriptor_path": str(capability_path.resolve()),
                    },
                }
                runner.context["experiment"] = {
                    "failure_dossier_ref": "artifact:command/composer/failure-recovery/experiment/attempt-1@1",
                    "failure_recovery": {"failure_class": "experiment_failure"},
                }
                stage = next(item for item in workflow["stages"] if item["id"] == "experiment")
                stale_descriptor = {
                    "experiment": {
                        "id": "robust_mean_pilot",
                        "research_question": "Does median-of-means reduce contaminated tail error?",
                        "primary_outcomes": [{"id": "contamination_p95_reduction_percent"}],
                        "environment_files": [
                            str(unrelated_executor.resolve()),
                            str(unrelated_validator.resolve()),
                        ],
                    },
                }
                packet = runner._specialist_stage_packet(stage, stale_descriptor)
                self.assertEqual(packet["analysis_plan"]["primary_outcomes"][0]["id"], "rho_l16")
                self.assertEqual(packet["analysis_plan"]["capability_source"],
                                 "admitted_topic_capability")
                self.assertEqual(packet["execution_manifest"]["capability_source"],
                                 "admitted_topic_capability")
                self.assertNotIn(str(unrelated_executor.resolve()), packet["input_digests"])
                self.assertNotIn(str(unrelated_validator.resolve()), packet["input_digests"])
                self.assertEqual(packet["input_digests"][str(topic_executor.resolve())],
                                 hashlib.sha256(topic_executor.read_bytes()).hexdigest())
                self.assertEqual(packet["input_digests"][str(topic_validator.resolve())],
                                 hashlib.sha256(topic_validator.read_bytes()).hexdigest())
                snapshot_paths = {item["path"] for item in packet["analysis_code"]["files"]}
                self.assertIn(str(topic_executor.resolve()), snapshot_paths)
                self.assertIn(str(topic_validator.resolve()), snapshot_paths)
                default_snapshot_paths = {
                    item["path"] for item in runner._failure_program_snapshot(stage)
                }
                self.assertIn(str(topic_executor.resolve()), default_snapshot_paths)
                self.assertIn(str(topic_validator.resolve()), default_snapshot_paths)

                generated_capability = runner.context["topic"].pop("generated_capability")
                matching_descriptor = {
                    "experiment": {
                        "id": "finite_size_winding_breakdown",
                        "research_question": "Does the diagnostic survive the finite-size boundary?",
                        "primary_outcomes": [{"id": "rho_l16"}],
                        "execution": {
                            "environment_files": [str(topic_executor.resolve())],
                        },
                        "validation": {
                            "environment_files": [str(topic_validator.resolve())],
                        },
                    },
                }
                matching_packet = runner._specialist_stage_packet(stage, matching_descriptor)
                self.assertEqual(matching_packet["analysis_plan"]["capability_source"],
                                 "matching_stage_descriptor")
                self.assertEqual(matching_packet["input_digests"][str(topic_executor.resolve())],
                                 hashlib.sha256(topic_executor.read_bytes()).hexdigest())
                self.assertEqual(matching_packet["input_digests"][str(topic_validator.resolve())],
                                 hashlib.sha256(topic_validator.read_bytes()).hexdigest())

                runner.context["topic"]["generated_capability"] = {
                    "capability_id": "robust_mean_pilot",
                    "descriptor_path": str(unrelated_capability_path.resolve()),
                }
                runner.context["topic"]["topic"].pop("experiment_capability_id")
                self.assertIsNone(runner._stage_experiment_capability_id(stage))
                stale_same_question_descriptor = {
                    "experiment": {
                        "id": "legacy-template-with-same-question",
                        "research_question": "Does the diagnostic survive the finite-size boundary?",
                        "environment_files": [
                            str(unrelated_executor.resolve()),
                            str(unrelated_validator.resolve()),
                        ],
                    },
                }
                topic_only_packet = runner._specialist_stage_packet(
                    stage, stale_same_question_descriptor)
                self.assertEqual(topic_only_packet["analysis_plan"]["capability_source"],
                                 "topic_only")
                self.assertNotIn(str(unrelated_executor.resolve()),
                                 topic_only_packet["input_digests"])
                self.assertNotIn(str(unrelated_validator.resolve()),
                                 topic_only_packet["input_digests"])
                snapshot_paths = {item["path"] for item in topic_only_packet["analysis_code"]["files"]}
                self.assertNotIn(str(unrelated_executor.resolve()), snapshot_paths)
                self.assertNotIn(str(unrelated_validator.resolve()), snapshot_paths)
                self.assertEqual(topic_only_packet["analysis_code"]["files"], [])
            finally:
                runner.close()

    def test_experiment_specialist_keeps_distinct_capability_and_study_ids_bound(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["stages"][2]["depends_on"] = ["survey"]
            runner = ComposerRunner(workflow)
            try:
                question = "Does the bounded coherence contrast survive detuning?"
                executor = root / "study_executor.py"
                validator = root / "study_validator.py"
                executor.write_text("print('study execution')\n")
                validator.write_text("print('study validation')\n")
                capability_path = root / "capability.json"
                capability_path.write_text(json.dumps({
                    "schema_version": "experiment-capability-1",
                    "experiment": {
                        "id": "study-b",
                        "research_question": question,
                        "study_type": "exploratory",
                        "method": "Matched resonant and detuned comparison.",
                        "parameters": {"mode_frequency": [600, 800]},
                        "primary_outcomes": [{"id": "coherence_integral"}],
                        "stopping_rule": "Run the declared grid once.",
                        "execution": {
                            "environment_files": [str(executor.resolve())],
                            "client": {"command": ["python3", str(executor.resolve())]},
                        },
                        "validation": {
                            "environment_files": [str(validator.resolve())],
                            "client": {"command": ["python3", str(validator.resolve())]},
                        },
                    },
                }))
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "question": question,
                    "topic": {
                        "id": "direction_b",
                        "experiment_capability_id": "cap-b",
                        "research_question": question,
                    },
                    "generated_capability": {
                        "capability_id": "cap-b",
                        "descriptor_path": str(capability_path.resolve()),
                    },
                }
                stage = runner.workflow["stages"][2]
                stale_descriptor = {
                    "experiment": {
                        "id": "robust_mean_pilot",
                        "research_question": "Does median-of-means reduce contaminated tail error?",
                    },
                }
                packet = runner._specialist_stage_packet(stage, stale_descriptor)
                orphan_stage = {**stage, "depends_on": []}
                self.assertEqual(
                    runner._specialist_experiment_projection(orphan_stage, stale_descriptor),
                    {},
                )
                self.assertEqual(runner._stage_experiment_capability_id(stage), "cap-b")
                self.assertEqual(runner._stage_experiment_study_id(stage), "study-b")
                self.assertEqual(packet["analysis_plan"]["capability_source"],
                                 "admitted_topic_capability")
                self.assertEqual(packet["analysis_plan"]["research_question"], question)
                self.assertEqual(packet["input_digests"][str(executor.resolve())],
                                 hashlib.sha256(executor.read_bytes()).hexdigest())
                self.assertEqual(packet["input_digests"][str(validator.resolve())],
                                 hashlib.sha256(validator.read_bytes()).hexdigest())
                snapshot_paths = {
                    item["path"] for item in runner._failure_program_snapshot(stage)
                }
                self.assertIn(str(executor.resolve()), snapshot_paths)
                self.assertIn(str(validator.resolve()), snapshot_paths)
            finally:
                runner.close()

    def test_invalid_continuation_request_is_rejected_without_blocking_composer(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            runner = ComposerRunner(workflow)

            def held_or_completed(stage, **kwargs):
                output = root / f"{stage['id']}-invalid-request.json"
                if stage["id"] == "survey":
                    payload = {"status": "completed"}
                else:
                    payload = {
                        "status": "research_expansion_required",
                        "research_expansion_requests": [{
                            "id": "bad-owner-request", "kind": "additional_experiment",
                            "owner": "unknown.department", "objective": "Do the control.",
                            "why": "The result is ambiguous.",
                            "success_condition": "The explanation is separated.",
                            "evidence_needed": "Validated raw output.",
                        }],
                    }
                output.write_text(json.dumps(payload))
                return {**payload, "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = held_or_completed
            result = runner.run()
            self.assertEqual(result["status"], "research_expansion_required")
            self.assertFalse(result["blockers"])
            self.assertTrue(any(item.get("action") == "reject_work_order"
                                for item in result["department_activity"]))

    def test_retries_failed_survey_in_its_durable_project_directory(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {"max_attempts": 3, "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def flaky_stage(stage, **kwargs):
                calls.append(stage["project_dir"])
                if len(calls) == 1:
                    raise RuntimeError("transient provider failure")
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"], "attempt": len(calls)}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = flaky_stage
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(result["stages"]["survey"]["attempts"]), 2)
            self.assertEqual(result["stages"]["survey"]["attempts"][0]["state"], "failed")
            self.assertEqual(result["stages"]["survey"]["attempts"][1]["state"], "succeeded")
            self.assertEqual(calls[1], calls[0])
            self.assertTrue(any(item["action"] == "retry_stage" for item in result["feedback"]))

    def test_survey_retry_migrates_to_latest_legacy_attempt_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            stage = self._workflow(root)["stages"][0]
            legacy = Path(stage["project_dir"]) / "attempts" / "attempt-7" / "state"
            legacy.mkdir(parents=True)
            (legacy / "control.sqlite").write_bytes(b"checkpoint")
            candidate = ComposerRunner._attempt_stage(stage, 8)
            self.assertEqual(
                Path(candidate["project_dir"]),
                legacy.parent,
            )

            base_state = Path(stage["project_dir"]) / "state"
            base_state.mkdir(parents=True)
            (base_state / "control.sqlite").write_bytes(b"newer-base")
            candidate = ComposerRunner._attempt_stage(stage, 9)
            self.assertEqual(Path(candidate["project_dir"]), Path(stage["project_dir"]).resolve())

    def test_survey_resume_scope_keeps_completed_milestones(self):
        self.assertEqual(ComposerRunner._survey_resume_scope({
            "survey_current": True, "survey_ref": "artifact:kb/surveys/current@4",
            "assessment_current": False, "status": "blocked"}), "gap_assessment")
        for prior in ({}, {"survey_current": True},
                      {"survey_current": False, "survey_ref": "artifact:kb/surveys/current@4"}):
            self.assertEqual(ComposerRunner._survey_resume_scope(prior), "focused_review")

    def test_reopened_survey_reuses_latest_current_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            stage = workflow["stages"][0]
            stale = root / "survey" / "continuations" / "cycle-8"
            current = root / "survey" / "continuations" / "cycle-7"
            for project, survey_ref, current_flag in (
                    (stale, "artifact:kb/surveys/current@1", False),
                    (current, "artifact:kb/surveys/current@2", True)):
                (project / "state").mkdir(parents=True)
                (project / "state" / "control.sqlite").write_bytes(b"checkpoint")
                (project / "output").mkdir()
                (project / "output" / "run.json").write_text(json.dumps({
                    "status": "blocked", "survey_ref": survey_ref,
                    "survey_current": current_flag, "assessment_current": False,
                }))
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 9
                runner.reopened_stage_ids = {"survey"}
                runner.context["survey"] = {
                    "kind": "survey", "project_dir": str(stale),
                    "survey_ref": "artifact:kb/surveys/current@1",
                    "survey_current": False, "assessment_current": False,
                }
                runner.stage_records["survey"] = {
                    "attempts": [{"project_dir": str(current), "state": "failed"}],
                }
                dispatched = runner._stage_for_cycle(stage)
                self.assertEqual(Path(dispatched["project_dir"]), current.resolve())
                self.assertNotIn("continuations/cycle-9", dispatched["project_dir"])
            finally:
                runner.close()

    def test_exploratory_admission_is_bound_before_capability_authoring(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            self.addCleanup(runner.close)
            runner.workflow["progression_policy"] = "full_pass"
            runner.workflow["capability_foundry_config_path"] = "configured-foundry"
            runner.context["topic"] = {"kind": "topic_discovery", "topic": {
                "id": "direction", "domain": "physics", "research_question": "A bounded question?"}}
            runner.context["survey"] = {"kind": "survey", "topic_admission": "exploratory_pilot",
                "gap_state": "insufficient_evidence", "survey_current": True, "assessment_current": True}
            with patch.object(runner, "_materialize_topic_capability", side_effect=RuntimeError("authoring boundary")) as author:
                with self.assertRaisesRegex(RuntimeError, "authoring boundary"):
                    runner._apply_topic_to_experiment_config(runner.workflow["stages"][1], {"experiment": {}})
            self.assertEqual(author.call_args.kwargs["study_type"], "exploratory")

    def test_foundry_usage_is_charged_once_and_restored_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            self.addCleanup(runner.close)
            runner._publish("command/foundry-work/fixture", "note", {
                "status": "repairing", "usage": {"model_calls": 2, "input_tokens": 300, "output_tokens": 70}},
                "command.controller")
            self.assertTrue(runner._sync_foundry_usage())
            self.assertEqual(runner.usage["model_calls"], 2)
            self.assertFalse(runner._sync_foundry_usage())
            runner._checkpoint("experiment:capability_validation_failed", force=True)
            runner._publish("command/foundry-work/fixture", "note", {
                "status": "calling", "usage": {"model_calls": 3, "input_tokens": 300, "output_tokens": 70}},
                "command.controller")
            workflow = runner.workflow
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)
            self.addCleanup(resumed.close)
            self.assertEqual(resumed.usage["model_calls"], 3)
            self.assertEqual(resumed.usage["input_tokens"], 300)
            self.assertFalse(resumed._sync_foundry_usage())

    def test_legacy_survey_resume_reads_immutable_runner_config(self):
        with tempfile.TemporaryDirectory() as path:
            project = Path(path) / "survey"
            control = ControlStore(project)
            store = ArtifactStore(control)
            store.init_project(principal_note="legacy-survey")
            stored = {
                "project_id": str(project.resolve()),
                "limits": {"wall_clock_seconds": 1200},
                "survey": {"question": "the admitted question"},
            }
            store.publish_artifact(
                logical_id="inputs/run-config", artifact_type="note", author="principal",
                body=canonical_bytes(stored), media_type="application/json",
            )
            control.close()

            self.assertEqual(ComposerRunner._durable_stage_config(project), stored)

    def test_retry_policy_exhaustion_keeps_failures_and_blocks(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {"max_attempts": 2, "backoff_seconds": 0}
            runner = ComposerRunner(workflow)

            def failed_stage(stage, **kwargs):
                raise RuntimeError("persistent provider failure")

            runner._run_stage = failed_stage
            result = runner.run()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["stages"]["survey"]["attempt_count"], 2)
            self.assertEqual(len(result["stages"]["survey"]["attempts"]), 2)
            self.assertEqual(result["blockers"][0]["attempts"], 2)
            self.assertEqual(sum(item["action"] == "retry_stage" for item in result["feedback"]), 1)

    def test_bounded_provider_cooldown_retries_after_reset_without_charging_quota(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {"max_attempts": 2, "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def cooldown_then_complete(stage, **kwargs):
                calls.append(stage["id"])
                if len(calls) == 1:
                    raise ProviderCooldownError(
                        "provider reset pending", retry_after_seconds=0.03,
                        rate_limit={"kind": "daily_budget"},
                    )
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = cooldown_then_complete
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["survey", "survey", "experiment"])
            self.assertEqual(result["stages"]["survey"]["attempt_count"], 2)
            self.assertEqual(result["stages"]["survey"]["attempts"][0]["usage"]["model_calls"], 0)
            self.assertTrue(any(
                item.get("action") == "provider_cooldown_auto_retry"
                for item in result["department_activity"]
            ))
            self.assertEqual(result["retry_schedule"], {})

    def test_bounded_unconfirmed_model_429_auto_retries_without_charging_model_quota(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {"max_attempts": 1, "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def cooldown_then_complete(stage, **kwargs):
                calls.append(stage["id"])
                if calls.count("survey") == 1:
                    raise ProviderCooldownError(
                        "model returned 429 without Retry-After",
                        retry_after_seconds=60,
                        rate_limit={"provider": "model", "status_code": 429,
                                    "retry_after_known": False},
                    )
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = cooldown_then_complete
            with patch(
                    "scisaurus.runtime.composer.DEFAULT_MODEL_RATE_LIMIT_COOLDOWN_SECONDS",
                    0.03):
                result = runner.run()
            self.assertEqual(result["status"], "completed", {
                "status": result["status"],
                "calls": calls,
                "survey_attempts": result["stages"]["survey"].get("attempts"),
                "active_blockers": result.get("active_blockers"),
            })
            self.assertEqual(calls, ["survey", "survey", "experiment"])
            first = result["stages"]["survey"]["attempts"][0]
            self.assertEqual(first["usage"]["model_calls"], 0)
            self.assertTrue(any(
                item.get("action") == "provider_cooldown_auto_retry"
                for item in result["department_activity"]
            ))
            self.assertEqual(result["retry_schedule"], {})

    def test_unconfirmed_model_rate_limit_uses_escalating_cooldown(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {
                "mode": "until_deadline", "backoff_seconds": 0,
            }
            runner = ComposerRunner(workflow)
            try:
                error = ProviderCooldownError(
                    "model returned 429 without Retry-After",
                    retry_after_seconds=60,
                    rate_limit={"provider": "model", "status_code": 429,
                                "retry_after_known": False},
                )
                observed = []
                for index in (1, 2, 3, 8, 9, 20):
                    error.cooldown_retry_index = index
                    observed.append(runner._retry_delay_seconds(error, index))
                self.assertEqual(observed, [60, 120, 240, 7680, 15360, 21600])
                legacy_error = ProviderCooldownError(
                    "legacy checkpoint model 429 without a reset header",
                    retry_after_seconds=82379,
                    rate_limit={"provider": "model", "status_code": 429},
                )
                self.assertEqual(runner._retry_delay_seconds(legacy_error, 1), 60)
            finally:
                runner.close()

    def test_deadline_governed_provider_cooldown_auto_retries_after_reset(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {
                "mode": "until_deadline", "backoff_seconds": 0,
            }
            runner = ComposerRunner(workflow)
            calls = []

            def cooldown_then_complete(stage, **kwargs):
                calls.append(stage["id"])
                if calls.count("survey") == 1:
                    raise ProviderCooldownError(
                        "provider reset pending", retry_after_seconds=0.03,
                        rate_limit={"kind": "daily_budget"},
                    )
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = cooldown_then_complete
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["survey", "survey", "experiment"])
            self.assertTrue(any(
                item.get("action") == "provider_cooldown_auto_retry"
                for item in result["department_activity"]
            ))

    def test_openalex_cooldown_with_admitted_crossref_fallback_retries_changed_route(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {"max_attempts": 2, "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def set_fallback(stage, error=None):
                runner.context[stage["id"]] = {
                    "kind": "survey",
                    "provider_fallback": {"mode": "crossref_metadata"},
                }
                return True

            def cooldown_then_complete(stage, **kwargs):
                calls.append(stage["id"])
                if stage["id"] == "survey" and calls.count("survey") == 1:
                    raise ProviderCooldownError(
                        "OpenAlex reset pending", retry_after_seconds=60,
                        rate_limit={"kind": "daily_budget"},
                    )
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._survey_provider_cooldown = lambda *args: None
            runner._admit_survey_provider_fallback = set_fallback
            runner._run_stage = cooldown_then_complete
            result = runner.run()
            self.assertEqual(result["status"], "completed", json.dumps(result["blockers"]))
            self.assertEqual(calls, ["survey", "survey", "experiment"])
            self.assertTrue(any(
                item.get("action") == "survey_provider_fallback_retry"
                for item in result["department_activity"]
            ))
            self.assertFalse(any(
                item.get("reason") == "provider_cooldown"
                for item in result["blockers"]
            ))

    def test_resume_reopens_rejected_topic_intake_from_durable_attempt_reason(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["progression_policy"] = "forward_first"
            runner = ComposerRunner(workflow)
            self.addCleanup(runner.close)
            rejected = [{
                "topic_id": "rejected-direction", "title": "A rejected direction",
                "domain": "test", "research_question": "Does A change B?",
                "rejection_type": "novelty",
            }]
            runner.stage_records["topic"] = {
                "kind": "topic_discovery", "status": "blocked",
                "error": (
                    "QuotaExceededError: topic discovery bounded intake exhausted "
                    "after 5 candidate attempts: selected topic is too similar"),
                "attempts": [{
                    "state": "failed", "retry_reason": "scientific_candidate_rejected",
                    "rejected_topic_history": rejected,
                    "topic_usage": {"model_calls": 8, "input_tokens": 100},
                }],
            }
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "blocked",
                "topic": {"id": "rejected-direction", "title": "A rejected direction"},
            }
            with patch.object(runner, "_begin_continuation", return_value=True):
                self.assertTrue(runner._reopen_blocked_checkpoint(set(), {
                    item["id"]: item for item in workflow["stages"]
                }))
            self.assertEqual(runner.stage_records["topic"]["status"], "retrying")
            self.assertTrue(any(
                entry.get("topic_id") == "rejected-direction"
                and entry.get("history_status") == "rejected"
                for entry in runner.topic_history["entries"]
            ))
            self.assertTrue(any(
                item.get("action") == "pivot_topic_after_intake_candidate_rejection"
                for item in runner.department_activity
            ))

    def test_resume_reconstructs_legacy_topic_pivot_only_from_saved_scientific_evidence(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["topic_history_path"] = str((root / "topic-history.json").resolve())
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            self.addCleanup(runner.close)
            rejected = [{
                "topic_id": "legacy-rejected-direction",
                "title": "A rejected direction", "domain": "test",
                "research_question": "Does A change B?",
                "rejection_type": "novelty",
            }]
            runner.stage_records["topic"] = {
                "kind": "topic_discovery", "status": "blocked",
                "error": (
                    "QuotaExceededError: topic discovery bounded intake exhausted "
                    "after 5 candidate attempts"),
                "rejected_topic_history": rejected,
                "attempts": [{"state": "failed"}],
            }
            with patch.object(runner, "_begin_continuation", return_value=True):
                self.assertTrue(runner._reopen_blocked_checkpoint(set(), {
                    item["id"]: item for item in workflow["stages"]
                }))
            self.assertEqual(runner.stage_records["topic"]["status"], "retrying")
            self.assertTrue(any(
                entry.get("topic_id") == "legacy-rejected-direction"
                and entry.get("history_status") == "rejected"
                for entry in runner.topic_history["entries"]
            ))

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["topic_history_path"] = str((root / "topic-history.json").resolve())
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            self.addCleanup(runner.close)
            runner.stage_records["topic"] = {
                "kind": "topic_discovery", "status": "blocked",
                "error": "QuotaExceededError: provider quota exhausted",
                "rejected_topic_history": rejected,
                "attempts": [{"state": "failed"}],
            }
            with patch.object(runner, "_begin_continuation", return_value=True):
                self.assertFalse(runner._reopen_blocked_checkpoint(set(), {
                    item["id"]: item for item in workflow["stages"]
                }))

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["topic_history_path"] = str((root / "topic-history.json").resolve())
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            self.addCleanup(runner.close)
            runner.stage_records["topic"] = {
                "kind": "topic_discovery", "status": "blocked",
                "error": "QuotaExceededError: topic discovery quota exhausted: model_calls=10, limit=10",
                "rejected_topic_history": rejected,
                "attempts": [{"state": "failed", "usage": {"model_calls": 10}}],
            }
            with patch.object(runner, "_begin_continuation", return_value=True):
                self.assertTrue(runner._reopen_blocked_checkpoint(set(), {
                    item["id"]: item for item in workflow["stages"]
                }))
            self.assertTrue(any(
                entry.get("topic_id") == "legacy-rejected-direction"
                and entry.get("history_status") == "rejected"
                for entry in runner.topic_history["entries"]
            ))

    def test_resume_recovers_legacy_bounded_topic_contract_failure(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            self.addCleanup(runner.close)
            contract_trace = [
                {
                    "attempt": attempt,
                    "status": "rejected",
                    "error": (
                        "topic discovery package requires exactly "
                        "['candidates', 'objective', 'schema_version', "
                        "'selected_id', 'selection_rationale']"
                    ),
                    "outcome_known": True,
                }
                for attempt in (1, 2, 4, 5)
            ]
            contract_trace.extend([
                {
                    "attempt": 3, "status": "rejected",
                    "error": "topic candidate id must be a bounded lowercase identifier",
                    "outcome_known": True,
                },
                {
                    "attempt": 6, "status": "maturity_refine",
                    "error": "topic maturity review requires substantive refinement",
                    "outcome_known": True,
                },
            ])
            contract_trace.sort(key=lambda item: item["attempt"])
            runner.stage_records["topic"] = {
                "kind": "topic_discovery", "status": "blocked",
                "error": (
                    "QuotaExceededError: topic discovery bounded intake exhausted "
                    "after 6 candidate attempt(s) (configured maximum 6): "
                    "topic discovery package requires exactly ['candidates', "
                    "'objective', 'schema_version', 'selected_id', "
                    "'selection_rationale']"),
                "repair_order_issued": False,
                "attempts": [{
                    "state": "failed",
                    "candidate_attempt_trace": contract_trace,
                }],
            }
            with patch.object(runner, "_begin_continuation", return_value=True):
                self.assertTrue(runner._reopen_blocked_checkpoint(set(), {
                    item["id"]: item for item in workflow["stages"]
                }))
            self.assertEqual(runner.stage_records["topic"]["status"], "retrying")
            self.assertTrue(any(
                item.get("action") == "admit_model_contract_recovery"
                for item in runner.department_activity
            ))
            self.assertEqual(
                runner.context["topic"]["research_requests"][0]["recovery_mode"],
                "format_repair_then_rerun",
            )

    def test_process_interrupt_persists_a_resumable_pause(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def interrupted(stage, **kwargs):
                raise KeyboardInterrupt("run cancellation requested")

            runner._run_stage = interrupted
            result = runner.run()
            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["interim_report"]["stop_reason"], "process_interrupted")
            self.assertTrue(any(item.get("stop_reason") == "process_interrupted"
                                for item in result["blockers"]))
            progress = json.loads((root / "composer" / "output" / "progress.json").read_text())
            self.assertEqual(progress["status"], "paused")
            self.assertEqual(progress["phase"], "paused_process_interruption")

    def test_specialist_live_cards_survive_an_ordinary_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            stage = workflow["stages"][0]
            runner.stage_records[stage["id"]] = {
                "kind": stage["kind"], "status": "running",
                "assignment_task_ids": ["specialist-survey-cataloger"],
            }
            runner._checkpoint("survey:admitted", force=True)
            runner._specialist_progress(stage["id"], {
                "event": "dispatched", "role": "research.cataloger",
                "role_id": "cataloger", "task_id": "specialist-survey-cataloger",
                "stage_id": stage["id"], "execution_mode": "model",
                "model": "qwen", "status": "running",
            })
            runner._checkpoint("survey:running", force=True)
            progress = json.loads((root / "composer" / "output" / "progress.json").read_text())
            live = progress["stages"][stage["id"]]["specialist_live"]
            self.assertEqual(live["research.cataloger"]["task_id"],
                             "specialist-survey-cataloger")
            runner.close()

    def test_blocker_projection_separates_recovered_history_from_live_stop(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                runner.stage_records["survey"] = {"kind": "survey", "status": "running"}
                runner.blockers = [
                    {"stage_id": "survey", "reason": "old scientific failure",
                     "recovery": "cycle_admitted"},
                    {"stage_id": "survey", "reason": "forwarded finding",
                     "gating": False, "release_blocking": False},
                ]
                self.assertEqual(runner._active_blockers(), [])

                runner.stage_records["survey"]["status"] = "blocked"
                runner.blockers.append({"stage_id": "survey", "reason": "current stop"})
                active = runner._active_blockers()
                self.assertEqual([item["reason"] for item in active], ["current stop"])
                runner._checkpoint("survey:blocked", force=True)
                progress = json.loads(
                    (root / "composer" / "output" / "progress.json").read_text())
                self.assertEqual(progress["blocker_counts"], {"active": 1, "historical": 3})
                self.assertEqual(progress["active_blockers"][0]["reason"], "current stop")
                progress["usage"] = {"model_calls": 7, "input_tokens": 11,
                                     "output_tokens": 13, "openalex_requests": 17}
                line = json.loads(_composer_progress_line(progress))
                self.assertEqual(line["usage"]["model_calls"], 7)
                self.assertEqual(line["blockers"], 1)
                self.assertEqual(line["historical_blockers"], 3)
            finally:
                runner.close()

    def test_missing_stage_input_blocker_expires_when_current_binding_is_present(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            consumer = workflow["stages"][1]
            consumer["bindings"] = [{
                "target": "packet.results_package",
                "source": "survey.results_package",
            }]
            result_path = root / "results-package.json"
            result_path.write_text("{}")
            runner = ComposerRunner(workflow)
            try:
                runner.status = "blocked"
                runner.stage_records["experiment"] = {
                    "kind": "experiment", "status": "candidate_needs_review",
                }
                runner.context["survey"] = {
                    "kind": "survey", "results_package": str(result_path),
                }
                runner.blockers = [{
                    "stage_id": "experiment",
                    "stop_reason": "missing_stage_input",
                    "reason": "required downstream artifact is unavailable",
                    "dependencies": [{"source": "survey.results_package"}],
                }]

                self.assertEqual(runner._active_blockers(), [])
                report = runner.interim_report(stop_reason=None, persist=False)
                self.assertEqual(report["stop_reason"], "blocked")

                result_path.unlink()
                active = runner._active_blockers()
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["stop_reason"], "missing_stage_input")
            finally:
                runner.close()

    def test_finish_does_not_promote_resolved_historical_input_gap_to_stop_reason(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["stages"][1]["bindings"] = [{
                "target": "packet.results_package",
                "source": "survey.results_package",
            }]
            result_path = root / "results-package.json"
            result_path.write_text("{}")
            runner = ComposerRunner(workflow)
            try:
                runner.status = "blocked"
                runner.stage_records["experiment"] = {
                    "kind": "experiment", "status": "candidate_needs_review",
                }
                runner.context["survey"] = {
                    "kind": "survey", "results_package": str(result_path),
                }
                runner.blockers = [{
                    "stage_id": "experiment", "stop_reason": "missing_stage_input",
                    "reason": "old missing binding",
                }]
                result = runner._finish()
                self.assertEqual(result["interim_report"]["stop_reason"], "blocked")
                self.assertEqual(result["active_blockers"], [])
            finally:
                runner.close()

    def test_resume_uses_the_process_interruption_checkpoint_at_equal_frontier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.stage_records["survey"] = {
                "kind": "survey", "status": "running", "attempt_count": 1,
                "attempt_number": 1, "project_dir": workflow["stages"][0]["project_dir"],
            }
            runner._checkpoint("survey:running", force=True)
            runner.status = "paused"
            runner.blockers.append({
                "stage_id": "workflow", "reason": "KeyboardInterrupt: ",
                "stop_reason": "process_interrupted",
            })
            runner._checkpoint("paused_process_interruption", force=True)
            runner._finish()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(resumed.status, "running")
                self.assertEqual(resumed._progress_snapshot["phase"],
                                 "paused_process_interruption")
                self.assertEqual(resumed.stage_records["survey"]["status"], "running")
            finally:
                resumed.close()

    def test_quota_exhaustion_does_not_recreate_a_stage_budget(self):
        class FastClock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                self.value += 1.0
                return self.value

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 5, "reuse_completed": False,
                "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["retry_policy"] = {"mode": "until_deadline", "backoff_seconds": 0}
            runner = ComposerRunner(workflow, clock=FastClock())
            calls = []

            def quota_exhausted(stage, **kwargs):
                calls.append(stage["id"])
                raise QuotaExceededError(
                    "topic budget exhausted", dimension="model_calls", limit=1, observed=1,
                    usage={"model_calls": 1, "input_tokens": 11,
                           "output_tokens": 7, "openalex_requests": 2},
                    diagnostics=[{"kind": "model", "status": "error"}],
                )

            runner._run_stage = quota_exhausted
            result = runner.run()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(calls, ["topic"])
            self.assertEqual(result["stages"]["topic"]["status"], "blocked")
            self.assertEqual(result["interim_report"]["stop_reason"], "blocked")
            self.assertEqual(result["usage"], {
                "model_calls": 1, "input_tokens": 11,
                "output_tokens": 7, "openalex_requests": 2,
            })
            self.assertEqual(result["stages"]["topic"]["attempts"][0]["usage"]["model_calls"], 1)

    def test_scientific_topic_intake_failure_pivots_until_deadline(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"] = [{
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            workflow["retry_policy"] = {"mode": "until_deadline", "backoff_seconds": 0}
            runner = ComposerRunner(workflow)
            calls = []

            def pivot_then_complete(stage, **kwargs):
                calls.append(stage["project_dir"])
                if len(calls) == 1:
                    error = QuotaExceededError(
                        "topic discovery bounded intake exhausted",
                        dimension="topic_attempts", limit=6, observed=6,
                        usage={"model_calls": 4, "input_tokens": 40,
                               "output_tokens": 20, "openalex_requests": 3},
                        diagnostics=[],
                    )
                    error.topic_budget_scope = "intake"
                    error.retryable_topic_intake = True
                    error.topic_retry_reason = "scientific_candidate_rejected"
                    error.rejected_topic_history = [{
                        "topic_id": "rejected-direction",
                        "title": "A weak direction",
                        "domain": "test",
                        "research_question": "Does A change B?",
                    }]
                    raise error
                output = root / "topic-result.json"
                output.write_text(json.dumps({"status": "completed"}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = pivot_then_complete
            runner._run_specialist_pool = lambda *args, **kwargs: {
                "reports": [], "by_role": {}, "usage": {}, "model_enabled": False,
            }
            runner._publish_specialist_reports = lambda stage, assignment, bundle: bundle
            runner._run_specialist_verifier = lambda *args, **kwargs: None
            result = runner.run()

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[1].endswith("attempts/attempt-2"))
            self.assertIn("continuations/cycle-1/attempts/attempt-2", calls[1])
            self.assertEqual(
                [item["cycle"] for item in result["stages"]["topic"]["attempts"]],
                [0, 1],
            )
            self.assertTrue(any(
                item.get("action") == "pivot_topic_direction"
                and item.get("reason") == "scientific_candidate_rejected"
                for item in result["department_activity"]
            ))

    def test_semantic_topic_rejection_opens_parent_refinement_not_budget_reset(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 30,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"] = [stage]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            runner = ComposerRunner(workflow)
            parent = {
                "id": "direction-mhd", "title": "Hall reconnection scaling",
                "phenomenon": "Collisionless magnetic reconnection",
                "research_question": "Does the reconnection rate scale with d_i/d_e?",
            }
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "research_expansion_required",
                "topic": parent,
                "topic_pivot": {"status": "required", "source_stage_id": "experiment"},
                "topic_evolution": {"salvage": {
                    "mode": "structural_pivot",
                    "attempted_branch_ids": [
                        "mechanism-observable", "comparison-baseline", "evidence-boundary",
                    ],
                }},
            }
            rejected = [{
                "topic_id": "off-parent-direction", "title": "Unrelated direction",
                "domain": "computational science",
                "research_question": "Does an unrelated metric change?",
                "rejection_type": "refinement",
                "rejection_reason": "topic salvage branch structural_pivot must preserve the parent's phenomenon",
            }]
            error = QuotaExceededError(
                "topic discovery bounded intake exhausted after one rejected refinement",
                dimension="topic_attempts", limit=8, observed=8,
                usage={"model_calls": 2, "openalex_requests": 3}, diagnostics=[],
            )
            error.topic_budget_scope = "continuation"
            error.retryable_topic_intake = True
            error.topic_retry_reason = "scientific_candidate_rejected"
            error.rejected_topic_history = rejected
            error.candidate_attempt_trace = [{
                "status": "refinement_rejected",
                "rejection_type": "refinement",
                "selected_topic": {"id": "off-parent-direction", "title": "Unrelated direction"},
                "error": rejected[0]["rejection_reason"],
            }]
            try:
                self.assertFalse(runner._is_local_topic_budget_exhaustion(error, stage))
                self.assertTrue(runner._admit_scientific_blocker_recovery(
                    stage, error, set(), {"topic": stage}))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertEqual(len(runner.active_research_requests), 1)
                request = runner.active_research_requests[0]
                self.assertEqual(request["kind"], "topic_refinement")
                self.assertNotEqual(
                    request.get("recovery_mode"), "continue_same_topic_after_local_budget")
                self.assertEqual(runner.context["topic"]["topic"], parent)
                self.assertEqual(
                    runner.context["topic"]["topic_evolution"]["salvage"]["attempted_branch_ids"],
                    ["mechanism-observable", "comparison-baseline", "evidence-boundary"],
                )
                self.assertTrue(any(
                    item.get("action") == "pivot_topic_after_intake_candidate_rejection"
                    for item in runner.department_activity
                ))
                self.assertTrue(any(
                    entry.get("topic_id") == "off-parent-direction"
                    and entry.get("history_status") == "rejected"
                    for entry in runner.topic_history["entries"]
                ))
            finally:
                runner.close()

    def test_unbudgeted_topic_contract_failure_keeps_typed_retry_metadata(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            model_path = root / "model.json"
            model_path.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            runner = ComposerRunner(workflow)
            error = ValidationError("topic candidate has an invalid shape")
            error.topic_intake_recoverable = True
            error.topic_retry_reason = "intake_contract_failure"
            error.candidate_attempt_trace = [{
                "status": "rejected", "error": str(error),
            }]
            try:
                with patch("scisaurus.runtime.topic_discovery.validate_topic_stage_config") as validate, \
                        patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as topic_runner:
                    validate.return_value = {
                        "model_config_path": str(model_path.resolve()),
                        "output_path": str((root / "topic-output.json").resolve()),
                        "candidate_count": 3, "max_attempts": 1,
                        "schema_version": "topic-discovery-config-1",
                    }
                    topic_runner.return_value.run.side_effect = error
                    with self.assertRaises(ValidationError) as raised:
                        runner._run_stage(stage)
                self.assertIs(raised.exception, error)
                self.assertTrue(raised.exception.retryable_topic_intake)
                self.assertEqual(raised.exception.topic_retry_reason,
                                 "intake_contract_failure")
                self.assertTrue(runner._is_topic_intake_retry(raised.exception, stage))
            finally:
                runner.close()

    def test_bounded_topic_contract_exhaustion_retains_local_recovery_scope(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            model_path = root / "model.json"
            model_path.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            runner = ComposerRunner(workflow)
            error = ValidationError("topic candidate has an invalid shape")
            error.topic_intake_recoverable = True
            error.topic_retry_reason = "intake_contract_failure"
            error.topic_budget = {"usage": {"model_calls": 4}, "events": []}
            error.topic_response_repair = {
                "response_sha256": "response-digest",
                "previous_validation_error": "topic candidate search query is not anchored",
            }
            error.candidate_attempt_trace = [{
                "status": "rejected", "error": str(error),
            }]
            try:
                with patch("scisaurus.runtime.topic_discovery.validate_topic_stage_config") as validate, \
                        patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as topic_runner:
                    validate.return_value = {
                        "model_config_path": str(model_path.resolve()),
                        "output_path": str((root / "topic-output.json").resolve()),
                        "candidate_count": 3, "max_attempts": 6,
                        "repair_mode": "bounded",
                        "budgets": {"max_model_calls": 20},
                        "schema_version": "topic-discovery-config-1",
                    }
                    topic_runner.return_value.run.side_effect = error
                    with self.assertRaises(QuotaExceededError) as raised:
                        runner._run_stage(stage)
                self.assertTrue(raised.exception.retryable_topic_intake)
                self.assertEqual(raised.exception.topic_budget_scope, "intake")
                self.assertEqual(raised.exception.topic_retry_reason,
                                 "intake_contract_failure")
                self.assertEqual(
                    raised.exception.topic_response_repair,
                    error.topic_response_repair,
                )
                self.assertFalse(runner._is_local_topic_budget_exhaustion(
                    raised.exception, stage))
            finally:
                runner.close()

    def test_raw_topic_feasibility_boundary_failure_becomes_scientific_retry(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            runner = ComposerRunner(workflow)
            error = ValidationError(
                "feasibility_plan.estimated_compute_seconds must be an integer between 1 and 604800")
            try:
                runner._execute_stage = lambda *args, **kwargs: (_ for _ in ()).throw(error)
                with self.assertRaises(ValidationError) as raised:
                    runner._run_stage(stage)
                caught = raised.exception
                self.assertTrue(caught.retryable_topic_intake)
                self.assertTrue(caught.topic_intake_recoverable)
                self.assertEqual(caught.topic_retry_reason,
                                 "scientific_candidate_rejected")
                self.assertTrue(runner._is_topic_intake_retry(caught, stage))
            finally:
                runner.close()

    def test_topic_budget_is_cumulative_across_isolated_attempts(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.stage_records["topic"] = {"attempts": [{
                "state": "failed",
                "usage": {"model_calls": 3, "input_tokens": 100,
                           "output_tokens": 25, "openalex_requests": 2},
            }]}
            remaining = runner._topic_budgets_for_attempt("topic", {
                "max_model_calls": 8,
                "max_openalex_requests": 5,
                "max_input_tokens": 200,
                "max_output_tokens": 40,
            })
            self.assertEqual(remaining, {
                "max_model_calls": 5,
                "max_openalex_requests": 3,
                "max_input_tokens": 100,
                "max_output_tokens": 15,
            })
            runner.close()

    def test_topic_continuation_budget_isolated_per_admitted_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 2
            runner.stage_records["topic"] = {"attempts": [
                {"state": "failed", "cycle": 0,
                 "usage": {"model_calls": 8, "input_tokens": 100,
                            "output_tokens": 25, "openalex_requests": 2}},
                {"state": "succeeded", "cycle": 1,
                 "topic_usage": {"model_calls": 3, "input_tokens": 40,
                                  "output_tokens": 10, "openalex_requests": 1},
                 # Specialist usage belongs to the stage ledger but not the
                 # topic runner's continuation envelope.
                 "usage": {"model_calls": 7, "input_tokens": 500,
                            "output_tokens": 50, "openalex_requests": 1}},
                {"state": "failed", "cycle": 2,
                 "topic_usage": {"model_calls": 2, "input_tokens": 20,
                                  "output_tokens": 5, "openalex_requests": 1}},
            ]}
            initial = runner._topic_budgets_for_attempt(
                "topic", {"max_model_calls": 10}, scope="intake")
            continuation = runner._topic_budgets_for_attempt(
                "topic", {"max_model_calls": 6}, scope="continuation")
            self.assertEqual(initial["max_model_calls"], 2)
            self.assertEqual(continuation["max_model_calls"], 4)
            runner.close()

    def test_topic_budget_reserves_parallel_review_work_inside_stage_quota(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
                "quota": {
                    "max_model_calls": 18, "max_input_tokens": 180,
                    "max_output_tokens": 60, "max_openalex_requests": 18,
                },
            })
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"topic"}
            runner.stage_records["topic"] = {"attempts": [{
                "state": "succeeded", "cycle": 1,
                "topic_usage": {
                    "model_calls": 4, "input_tokens": 40,
                    "output_tokens": 10, "openalex_requests": 2,
                },
                # Aggregate use includes five specialist calls beyond the
                # topic runner's own usage.
                "usage": {
                    "model_calls": 9, "input_tokens": 70,
                    "output_tokens": 25, "openalex_requests": 4,
                },
            }]}
            remaining = runner._topic_budgets_for_attempt(
                "topic", {
                    "max_model_calls": 20, "max_input_tokens": 200,
                    "max_output_tokens": 100, "max_openalex_requests": 20,
                }, scope="continuation",
                current_usage={
                    "model_calls": 3, "input_tokens": 30,
                    "output_tokens": 10, "openalex_requests": 1,
                },
                reserved_usage={
                    "model_calls": 2, "input_tokens": 20,
                    "output_tokens": 5,
                },
            )
            self.assertEqual(remaining, {
                "max_model_calls": 4,
                "max_input_tokens": 60,
                "max_output_tokens": 20,
                "max_openalex_requests": 13,
            })
            runner.close()

    def test_first_topic_dispatch_reserves_all_planned_specialist_and_verifier_work(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            model_path = root / "model.json"
            model_path.write_text(json.dumps({"base_url": "http://model.test", "model": "qwen"}))
            topic_stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 10, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
                "quota": {
                    "max_model_calls": 20, "max_input_tokens": 8000,
                    "max_output_tokens": 2000, "max_openalex_requests": 100,
                },
            }
            workflow["stages"].insert(0, topic_stage)
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"].insert(0, "topic")
            runner = ComposerRunner(workflow)
            stage_assignment = {"assignments": [
                {"assignment_phase": "specialist", "execution_kind": "model",
                 "quota": {"max_calls": 2, "max_input_tokens": 1200,
                           "max_output_tokens": 200}},
                {"assignment_phase": "specialist", "execution_kind": "review",
                 "quota": {"max_calls": 1, "max_input_tokens": 800,
                           "max_output_tokens": 100}},
                {"assignment_phase": "specialist", "execution_kind": "deterministic",
                 "quota": {"max_calls": 9, "max_input_tokens": 900,
                           "max_output_tokens": 90}},
                {"assignment_phase": "verifier", "execution_kind": "review",
                 "quota": {"max_calls": 2, "max_input_tokens": 3000,
                           "max_output_tokens": 600}},
            ]}
            descriptor = {
                "schema_version": "topic-discovery-config-1",
                "model_config_path": str(model_path.resolve()),
                "output_path": str((root / "topic-result.json").resolve()),
                "candidate_count": 3, "max_attempts": 1,
                "budgets": {"max_model_calls": 30, "max_input_tokens": 10000,
                            "max_output_tokens": 4000, "max_openalex_requests": 50},
            }
            try:
                with patch("scisaurus.runtime.topic_discovery.validate_topic_stage_config",
                           return_value=descriptor), \
                        patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as topic_runner, \
                        patch.object(runner, "_runtime_context", return_value={}), \
                        patch.object(runner, "_topic_refinement_context", return_value={}):
                    topic_runner.return_value.run.return_value = {"status": "completed"}
                    runner._run_stage(topic_stage, stage_assignment=stage_assignment)

                budgets = topic_runner.return_value.run.call_args.kwargs["budgets"]
                self.assertEqual(budgets, {
                    "max_model_calls": 15,
                    "max_input_tokens": 3000,
                    "max_output_tokens": 1100,
                    "max_openalex_requests": 50,
                })
            finally:
                runner.close()

    def test_reopened_stage_quota_isolated_per_admitted_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"survey"}
            runner.stage_records["survey"] = {
                "kind": "survey",
                # This is the prior cycle snapshot. It must not consume the
                # fresh quota envelope before the reopened cycle dispatches.
                "usage": {"model_calls": 80, "input_tokens": 100,
                           "output_tokens": 20, "openalex_requests": 2},
                "attempts": [
                    {"state": "failed", "cycle": 0,
                     "usage": {"model_calls": 80, "input_tokens": 100,
                                "output_tokens": 20, "openalex_requests": 2}},
                    {"state": "succeeded", "cycle": 1,
                     "usage": {"model_calls": 12, "input_tokens": 40,
                                "output_tokens": 10, "openalex_requests": 1}},
                ],
            }
            survey = next(item for item in workflow["stages"] if item["id"] == "survey")
            survey["quota"] = {
                "max_model_calls": 24,
                "max_input_tokens": 1000,
                "max_output_tokens": 500,
                "max_openalex_requests": 12,
            }
            self.assertEqual(runner._stage_usage("survey"), {
                "model_calls": 12, "input_tokens": 40,
                "output_tokens": 10, "openalex_requests": 1,
            })
            self.assertIsNone(runner._stage_quota_error(survey))
            runner.close()

    def test_stage_quota_exhaustion_admits_narrow_scoped_recovery(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.context["survey"] = {
                    "kind": "survey", "status": "completed",
                    "survey_current": True,
                }
                by_id = {item["id"]: item for item in workflow["stages"]}
                error = QuotaExceededError(
                    "stage survey quota exhausted: model_calls=97 > 96",
                    dimension="max_model_calls", limit=96, observed=97,
                    usage={"model_calls": 97},
                )
                self.assertTrue(runner._admit_stage_quota_recovery(
                    by_id["survey"], error, set(), by_id))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertEqual(
                    runner.context["survey"]["quota_recovery"]["mode"],
                    "narrow_scope",
                )
                request = next(
                    item for item in runner.active_research_requests
                    if item.get("kind") == "literature_expansion")
                self.assertIn("narrower decisive gate", request["objective"])
                self.assertTrue(any(
                    item.get("action") == "stage_quota_recovery_admitted"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_resume_reuses_durable_exploratory_survey_for_exact_topic_lineage(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["stages"][1]["id"] = "literature-review"
            workflow["stages"][2]["depends_on"] = ["literature-review"]
            workflow["completion"]["required_stage_ids"] = [
                "literature-review", "experiment",
            ]
            runner = ComposerRunner(workflow)
            survey_project = root / "survey" / "continuations" / "cycle-581"
            survey_project.mkdir(parents=True)
            control = ControlStore(survey_project)
            store = ArtifactStore(control)
            store.init_project(principal_note="current exploratory survey")
            try:
                survey = store.publish_artifact(
                    logical_id="kb/surveys/current", artifact_type="report",
                    author="research.survey",
                    body=canonical_bytes({"status": "completed"}),
                )
                assessment = store.publish_artifact(
                    logical_id="kb/gap-assessments/current", artifact_type="report",
                    author="research.survey",
                    body=canonical_bytes({
                        "state": "insufficient_evidence",
                        "survey_ref": survey["artifact_ref"],
                    }),
                )
                output = survey_project / "output"
                output.mkdir()
                (output / "run.json").write_text(json.dumps({
                    "status": "completed", "survey_current": True,
                    "assessment_current": True,
                    "gap_state": "insufficient_evidence",
                    "survey_ref": survey["artifact_ref"],
                    "assessment_ref": assessment["artifact_ref"],
                }))
                (output / "composer-gated-run.json").write_text(json.dumps({
                    "status": "completed", "topic_admission": "exploratory_pilot",
                    "survey_ref": survey["artifact_ref"],
                    "assessment_ref": assessment["artifact_ref"],
                }))

                runner.context["topic"] = {
                    "kind": "topic_discovery", "topic": {"id": "direction_3"},
                    "topic_evolution": {"cycle": 581},
                }
                runner.context["literature-review"] = {
                    "kind": "survey", "status": "research_expansion_required",
                    "review_status": "stage_quota_exhausted",
                    "quota_recovery": {
                        "status": "admitted", "admitted_cycle": 670,
                        "recovery_id": "quota-recovery-survey-670",
                    },
                }
                runner.stage_records["literature-review"] = {
                    "kind": "survey", "status": "research_expansion_required",
                    "attempts": [{
                        "attempt_number": 372, "cycle": 669,
                        "state": "succeeded", "topic_id": "direction_3",
                        "topic_cycle": 581,
                        "project_dir": str(survey_project.resolve()),
                    }],
                }
                survey_recovery = {
                    "id": "auto-literature-review-recovery-670",
                    "kind": "literature_expansion",
                    "owner": "research.intelligence",
                    "objective": "Repeat the full literature catalog.",
                }
                methods_repair = {
                    "id": "methods-experiment-repair",
                    "kind": "additional_experiment",
                    "owner": "methods.methodologist",
                }
                runner.active_research_requests = [survey_recovery, methods_repair]
                blocker = {
                    "stage_id": "literature-review", "stop_reason": "stage_quota_exhausted",
                    "quota_recovery_id": "quota-recovery-survey-670",
                }
                runner.blockers.append(blocker)
                completed = set()
                by_id = {stage["id"]: stage for stage in workflow["stages"]}

                self.assertTrue(
                    runner._restore_completed_survey_after_quota_recovery(completed, by_id))
                self.assertEqual(runner.context["literature-review"]["status"], "completed")
                self.assertEqual(runner.context["literature-review"]["topic_lineage"], {
                    "topic_id": "direction_3", "topic_cycle": 581,
                })
                self.assertEqual(runner.context["literature-review"]["topic_admission"], "exploratory_pilot")
                self.assertIn("literature-review", completed)
                self.assertEqual(runner.stage_records["literature-review"]["survey_ref"],
                                 survey["artifact_ref"])
                self.assertEqual(runner.active_research_requests, [methods_repair])
                self.assertEqual(
                    runner.context["literature-review"]["deferred_research_requests"][0]["id"],
                    survey_recovery["id"],
                )
                self.assertEqual(blocker["recovery"], "superseded_by_current_stage_state")
                self.assertTrue(any(
                    event["action"] == "reuse_completed_exploratory_survey_after_quota_recovery"
                    for event in runner.department_activity
                ))
            finally:
                control.close()
                runner.close()

    def test_resume_ignores_historical_quota_blocker_after_stage_result(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            try:
                reason = "stage survey quota exhausted: model_calls=4919 > 96"
                runner.context["survey"] = {
                    "kind": "survey",
                    "status": "completed",
                    "survey_current": True,
                }
                runner.stage_records["survey"] = {
                    "kind": "survey",
                    "status": "succeeded",
                    "attempts": [{"state": "succeeded", "cycle": 4}],
                }
                blocker = {
                    "stage_id": "survey",
                    "reason": reason,
                    "stop_reason": "stage_quota_exhausted",
                    "dimension": "max_model_calls",
                    "limit": 96,
                    "observed": 4919,
                }
                runner.blockers.append(blocker)
                by_id = {item["id"]: item for item in runner.workflow["stages"]}

                self.assertFalse(runner._resume_stage_quota_recovery(set(), by_id))
                self.assertEqual(runner.continuation_cycles, 0)
                self.assertEqual(
                    blocker["recovery"], "superseded_by_current_stage_state")
                self.assertFalse(any(
                    item.get("kind") == "literature_expansion"
                    for item in runner.active_research_requests
                ))
            finally:
                runner.close()

    def test_resume_admits_only_matching_pending_quota_recovery(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            try:
                reason = "stage survey quota exhausted: model_calls=97 > 96"
                recovery_id = "quota-recovery-survey-0-test"
                runner.context["survey"] = {
                    "kind": "survey",
                    "status": "research_expansion_required",
                    "review_status": "stage_quota_exhausted",
                    "error": reason,
                    "quota_recovery": {
                        "status": "required",
                        "recovery_id": recovery_id,
                        "mode": "narrow_scope",
                        "previous_cycle": 0,
                        "recovery_count": 0,
                        "dimension": "max_model_calls",
                        "limit": 96,
                        "observed": 97,
                    },
                }
                blocker = {
                    "stage_id": "survey",
                    "reason": reason,
                    "stop_reason": "stage_quota_exhausted",
                    "dimension": "max_model_calls",
                    "limit": 96,
                    "observed": 97,
                    "quota_recovery_id": recovery_id,
                }
                runner.blockers.append(blocker)
                by_id = {item["id"]: item for item in runner.workflow["stages"]}
                original_checkpoint = runner._checkpoint
                persisted_admission = {}

                def capture_continuation_checkpoint(label, *args, **kwargs):
                    if label == "continuation:1:admitted":
                        persisted_admission["recovery"] = deepcopy(
                            runner.context["survey"]["quota_recovery"])
                        persisted_admission["requests"] = deepcopy(
                            runner.active_research_requests)
                    return original_checkpoint(label, *args, **kwargs)

                runner._checkpoint = capture_continuation_checkpoint

                self.assertTrue(runner._resume_stage_quota_recovery(set(), by_id))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertEqual(blocker["recovery"], "cycle_admitted")
                self.assertEqual(
                    runner.context["survey"]["quota_recovery"]["status"], "admitted")
                self.assertEqual(persisted_admission["recovery"]["status"], "admitted")
                self.assertEqual(persisted_admission["recovery"]["admitted_cycle"], 1)
                self.assertEqual(
                    persisted_admission["recovery"]["request_id"],
                    "auto-survey-recovery-1")
                self.assertIn(
                    "auto-survey-recovery-1",
                    {request["id"] for request in persisted_admission["requests"]})
            finally:
                runner.close()

    def test_new_quota_recovery_persists_admission_before_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            try:
                stage = next(item for item in runner.workflow["stages"]
                             if item.get("kind") == "survey")
                by_id = {item["id"]: item for item in runner.workflow["stages"]}
                quota_error = QuotaExceededError(
                    "stage survey quota exhausted: model_calls=97 > 96",
                    dimension="max_model_calls", limit=96, observed=97,
                )
                original_checkpoint = runner._checkpoint
                persisted_admission = {}

                def capture_continuation_checkpoint(label, *args, **kwargs):
                    if label == "continuation:1:admitted":
                        persisted_admission["recovery"] = deepcopy(
                            runner.context[stage["id"]]["quota_recovery"])
                    return original_checkpoint(label, *args, **kwargs)

                runner._checkpoint = capture_continuation_checkpoint
                self.assertTrue(
                    runner._admit_stage_quota_recovery(stage, quota_error, set(), by_id))
                self.assertEqual(persisted_admission["recovery"]["status"], "admitted")
                self.assertEqual(persisted_admission["recovery"]["admitted_cycle"], 1)
                self.assertEqual(
                    persisted_admission["recovery"]["request_id"],
                    f"auto-{stage['id']}-recovery-1")
            finally:
                runner.close()

    def test_resume_retries_same_cycle_quota_admission_without_new_recovery(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            try:
                reason = "stage survey quota exhausted: model_calls=97 > 96"
                recovery_id = "quota-recovery-survey-0-pending"
                runner.context["survey"] = {
                    "kind": "survey",
                    "status": "research_expansion_required",
                    "review_status": "stage_quota_exhausted",
                    "error": reason,
                    "quota_recovery": {
                        "status": "required",
                        "recovery_id": recovery_id,
                        "mode": "narrow_scope",
                        "previous_cycle": 0,
                        "recovery_count": 1,
                        "dimension": "max_model_calls",
                        "limit": 96,
                        "observed": 97,
                    },
                }
                blocker = {
                    "stage_id": "survey",
                    "reason": reason,
                    "stop_reason": "stage_quota_exhausted",
                    "dimension": "max_model_calls",
                    "limit": 96,
                    "observed": 97,
                    "quota_recovery_id": recovery_id,
                }
                runner.blockers.append(blocker)
                by_id = {item["id"]: item for item in runner.workflow["stages"]}

                self.assertTrue(runner._resume_stage_quota_recovery(set(), by_id))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertEqual(
                    runner.context["survey"]["quota_recovery"]["recovery_count"], 1)
                self.assertEqual(
                    runner.context["survey"]["quota_recovery"]["recovery_id"], recovery_id)
                self.assertEqual(
                    runner.context["survey"]["quota_recovery"]["status"], "admitted")
            finally:
                runner.close()

    def test_specialist_resume_reuses_durable_response_for_identical_interrupted_packet(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                stage = workflow["stages"][0]
                descriptor = {}
                stage_result = {
                    "status": "research_expansion_required",
                    "survey_current": False,
                    "gap_state": "evidence_incomplete",
                }
                prior = runner.departments.begin_stage(
                    "survey", "survey", attempt_number=312,
                    input_ref={"kind": "composer_stage_task", "ref": "survey-312"},
                    deadline_seconds=20, active_role_ids=["search-strategist"],
                )
                packet = runner._specialist_stage_packet(
                    stage, descriptor, stage_result=stage_result)
                packet_digest = hashlib.sha256(canonical_bytes(packet)).hexdigest()
                prior_assignment = next(
                    item for item in prior["assignments"]
                    if item["assignment_phase"] == "specialist")
                specialist_report = {
                    "status": "succeeded", "execution_mode": "model",
                    "assigned_role": prior_assignment["assigned_role"],
                    "role_id": prior_assignment["role_id"],
                    "model": "test-model", "model_role": "research.search-planner",
                    "usage": {"model_calls": 1, "input_tokens": 11, "output_tokens": 7},
                    "response": {"decision": "repair", "summary": "Evidence needs repair.",
                                 "findings": [], "evidence_gaps": [],
                                 "requested_actions": []},
                }
                runner.store.publish_artifact(
                    logical_id=f"{prior_assignment['assignment_logical_id']}/execution",
                    artifact_type="report", author=prior_assignment["assigned_role"],
                    body=canonical_bytes({
                        "schema_version": "specialist-execution-1",
                        "project_id": workflow["project_id"],
                        "stage_id": "survey", "stage_kind": "survey",
                        "attempt_number": 312,
                        "assigned_role": prior_assignment["assigned_role"],
                        "role_id": prior_assignment["role_id"],
                        "model_role": prior_assignment["model_role"],
                        "assignment_id": prior_assignment["assignment_id"],
                        "task_id": prior_assignment["task_id"],
                        "input_digest": packet_digest,
                        "report": specialist_report,
                    }),
                    media_type="application/json",
                )
                runner.departments.reconcile_interrupted_assignments()
                current = runner.departments.begin_stage(
                    "survey", "survey", attempt_number=313,
                    input_ref={"kind": "composer_stage_task", "ref": "survey-313"},
                    deadline_seconds=20, active_role_ids=["search-strategist"],
                )
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "running", "attempt_number": 313,
                    "attempts": [{"attempt_number": 312, "state": "unknown", "cycle": 1}],
                }
                with patch.object(
                        ComposerRunner, "_specialist_model_config",
                        return_value={"base_url": "http://127.0.0.1:1", "model": "test-model"}), \
                        patch.object(runner, "_dispatch_specialist_work",
                                     side_effect=AssertionError("provider call must be reused")):
                    bundle = runner._run_specialist_pool(
                        stage, current, descriptor, stage_result=stage_result)

                self.assertEqual(len(bundle["reports"]), 1)
                self.assertTrue(bundle["reports"][0]["provider_call_reused"])
                self.assertEqual(bundle["reports"][0]["reused_prior_usage"]["model_calls"], 1)
                self.assertEqual(bundle["reports"][0]["usage"]["model_calls"], 0)
                self.assertEqual(bundle["usage"]["model_calls"], 0)
                self.assertEqual(
                    bundle["reports"][0]["reused_from_attempt_id"],
                    prior_assignment["attempt_id"],
                )
                bundle = runner._publish_specialist_reports(stage, current, bundle)
                verifier_result = {
                    "status": "succeeded", "response": {"decision": "accept"},
                    "usage": {},
                }
                result = runner.departments.finish_stage(
                    "survey", "survey", attempt_number=313, outcome="completed",
                    usage=bundle["usage"], specialist_results=bundle["by_role"],
                    verifier_result=verifier_result,
                )
                current_assignment = next(
                    item for item in current["assignments"]
                    if item["assignment_phase"] == "specialist")
                current_attempt = runner.tasks.get_attempt(current_assignment["attempt_id"])
                self.assertEqual(current_attempt["state"], "cancelled")
                self.assertFalse(current_attempt["usage"].get("actual"))
                self.assertEqual(result["verifier_outcome"], "accepted")
            finally:
                runner.close()

    def test_durable_specialist_response_settles_provider_attempt_immediately(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                stage = workflow["stages"][0]
                assignment = runner.departments.begin_stage(
                    "survey", "survey", attempt_number=1,
                    input_ref={"kind": "composer_stage_task", "ref": "survey-1"},
                    deadline_seconds=20, active_role_ids=["search-strategist"],
                )
                role_assignment = next(
                    item for item in assignment["assignments"]
                    if item["assignment_phase"] == "specialist")
                report = {
                    "status": "succeeded", "execution_mode": "model",
                    "assigned_role": role_assignment["assigned_role"],
                    "role_id": role_assignment["role_id"],
                    "usage": {"model_calls": 1, "input_tokens": 123, "output_tokens": 45},
                    "response": {"decision": "repair", "summary": "The evidence needs repair.",
                                 "findings": [], "evidence_gaps": [],
                                 "requested_actions": []},
                }
                bundle = runner._publish_specialist_reports(stage, assignment, {
                    "model_enabled": True,
                    "packet": {"objective": "Verify the bounded evidence."},
                    "reports": [report],
                    "by_role": {"search-strategist": report},
                    "usage": {},
                })

                attempt = runner.tasks.get_attempt(role_assignment["attempt_id"])
                self.assertEqual(attempt["state"], "succeeded")
                self.assertEqual(attempt["usage"]["actual"], report["usage"])
                self.assertTrue(bundle["reports"][0]["artifact_ref"].endswith("@1"))
                self.assertEqual(
                    runner.tasks.get(role_assignment["task_id"])["state"], "running")
            finally:
                runner.close()

    def test_repeated_survey_quota_exhaustion_pivots_to_topic(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 1
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "status": "completed",
                    "topic": {
                        "id": "prior-topic",
                        "title": "Prior direction",
                        "research_question": "Does the mechanism change the observable?",
                    },
                }
                runner.context["survey"] = {
                    "kind": "survey",
                    "status": "research_expansion_required",
                    "quota_recovery": {
                        "status": "required",
                        "mode": "narrow_scope",
                        "previous_cycle": 0,
                        "recovery_count": 1,
                    },
                }
                by_id = {item["id"]: item for item in workflow["stages"]}
                error = QuotaExceededError(
                    "stage survey quota exhausted: model_calls=97 > 96",
                    dimension="max_model_calls", limit=96, observed=97,
                    usage={"model_calls": 97},
                )
                self.assertTrue(runner._admit_stage_quota_recovery(
                    by_id["survey"], error, {"topic"}, by_id))
                self.assertEqual(runner.continuation_cycles, 2)
                self.assertEqual(
                    runner.context["survey"]["quota_recovery"]["mode"],
                    "topic_pivot",
                )
                self.assertEqual(
                    runner.context["topic"]["topic_pivot"]["source_stage_id"],
                    "survey",
                )
                self.assertTrue(any(
                    item.get("action") == "pivot_topic_after_repeated_survey_quota"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_continuation_clears_stale_live_assignment_projection(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.stage_records["survey"] = {
                    "kind": "survey",
                    "status": "running",
                    "active_agents": ["research.search-strategist"],
                    "last_active_agents": [],
                }
                runner.context["survey"] = {
                    "kind": "survey",
                    "status": "research_expansion_required",
                    "research_requests": [{
                        "id": "survey-repair",
                        "kind": "literature_expansion",
                        "owner": "research.intelligence",
                        "source_stage_id": "survey",
                    }],
                }
                by_id = {item["id"]: item for item in workflow["stages"]}
                completed = set()
                self.assertTrue(runner._begin_continuation(completed, by_id))
                self.assertEqual(runner.stage_records["survey"]["status"], "retrying")
                self.assertEqual(runner.stage_records["survey"]["active_agents"], [])
                self.assertEqual(
                    runner.stage_records["survey"]["last_active_agents"],
                    ["research.search-strategist"],
                )
            finally:
                runner.close()

    def test_continuation_archives_inflight_attempt_before_allocating_next_number(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                task = runner._stage_task(workflow["stages"][0])
                attempt_id = f"{task['task_id']}-attempt-313"
                runner.tasks.start_attempt(
                    task["task_id"], attempt_id, owner="command.composer",
                    lease_ttl_seconds=60, payload={"stage_id": "survey"})
                runner.continuation_cycles = 4
                runner.stage_records["survey"] = {
                    "kind": "survey",
                    "status": "running",
                    "task_id": task["task_id"],
                    "attempt_id": attempt_id,
                    "attempt_number": 313,
                    "attempt_count": 313,
                    "project_dir": workflow["stages"][0]["project_dir"],
                    "attempts": [
                        {"attempt_number": number, "attempt_id": f"old-{number}",
                         "state": "failed"}
                        for number in range(1, 313)
                    ],
                }
                runner.context["survey"] = {
                    "kind": "survey",
                    "status": "research_expansion_required",
                    "research_requests": [{
                        "id": "survey-repair",
                        "kind": "literature_expansion",
                        "owner": "research.intelligence",
                        "source_stage_id": "survey",
                        "objective": "Verify the missing direct evidence.",
                        "why": "The previous survey left a decisive evidence gap.",
                        "success_condition": "The cited evidence is verified.",
                        "evidence_needed": "Exact full-text passages and identities.",
                    }],
                }

                by_id = {item["id"]: item for item in workflow["stages"]}
                self.assertTrue(runner._begin_continuation(set(), by_id))

                history = runner.stage_records["survey"]["attempts"]
                self.assertEqual(len(history), 313)
                self.assertEqual(history[-1]["attempt_id"], attempt_id)
                self.assertEqual(history[-1]["attempt_number"], 313)
                self.assertEqual(history[-1]["cycle"], 4)
                self.assertEqual(history[-1]["state"], "unknown")
                self.assertEqual(runner.stage_records["survey"]["attempt_count"], 313)
                self.assertEqual(len(history) + 1, 314)
                self.assertEqual(
                    runner.tasks.get_attempt(attempt_id)["state"], "result_unknown")
            finally:
                runner.close()

    def test_stage_attempt_archive_preserves_terminal_ledger_usage(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                task = runner._stage_task(workflow["stages"][0])
                attempt_id = f"{task['task_id']}-attempt-known-success"
                runner.tasks.start_attempt(
                    task["task_id"], attempt_id, owner="command.composer",
                    lease_ttl_seconds=60, payload={"stage_id": "survey"})
                usage = {"model_calls": 2, "input_tokens": 123, "output_tokens": 45}
                runner.tasks.finish_attempt(attempt_id, "succeeded", usage=usage)
                record = {
                    "status": "running",
                    "attempt_id": attempt_id,
                    "attempt_number": 7,
                    "attempt_count": 7,
                    "attempts": [],
                }

                history = runner._archive_stage_attempt(record, cycle=3)

                self.assertEqual(history[-1]["state"], "succeeded")
                self.assertEqual(history[-1]["usage"], usage)
                self.assertEqual(history[-1]["attempt_number"], 7)
                self.assertEqual(record["attempt_count"], 7)
            finally:
                runner.close()

    def test_resumed_continuation_lease_ignores_historical_cycles(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 231
                runner._continuation_budget_baseline = 231
                request = {
                    "id": "survey-recovery",
                    "kind": "literature_expansion",
                    "owner": "research.intelligence",
                    "objective": "Run a changed boundary-focused search.",
                    "why": "The previous survey allocation was exhausted.",
                    "success_condition": "The gap decision is refreshed.",
                    "evidence_needed": "Identity-reconciled primary records.",
                    "source_stage_id": "survey",
                }
                runner.context["survey"] = {
                    "kind": "survey", "status": "completed",
                    "research_requests": [request],
                }
                runner.active_research_requests = [request]
                completed = {"survey"}
                by_id = {item["id"]: item for item in workflow["stages"]}

                self.assertTrue(runner._begin_continuation(completed, by_id))
                self.assertEqual(runner.continuation_cycles, 232)
                self.assertEqual(runner._continuation_budget_used(), 1)
                self.assertFalse(runner._begin_continuation(completed, by_id))
            finally:
                runner.close()

    def test_failed_materialized_stage_is_reviewed_before_recovery(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            config = root / "argument.json"
            config.write_text("{}")
            argument_dir = root / "argument"
            argument_dir.mkdir()
            workflow = {
                "schema_version": "composer-workflow-1",
                "id": "failed-materialized-review",
                "revision": 1,
                "project_id": str(root / "composer"),
                "objective": "Review a failed scientific artifact.",
                "progression_policy": "forward_first",
                "continuation_policy": {"mode": "bounded", "max_cycles": 0},
                "stages": [{
                    "id": "argument", "kind": "argument",
                    "config_path": str(config.resolve()),
                    "project_dir": str(argument_dir.resolve()), "depends_on": [],
                    "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                    "reuse_completed": False, "reuse_output_path": None,
                }],
                "time_policy": {"first_result_seconds": 1, "target_seconds": 10,
                                 "hard_seconds": 30, "checkpoint_seconds": 1},
                "completion": {"required_stage_ids": ["argument"],
                                "release_requires_human": True},
            }
            runner = ComposerRunner(workflow)
            packets = []
            try:
                def failed(_stage, **_kwargs):
                    error = ValidationError("the argument needs a narrower claim")
                    error.stage_result = {
                        "status": "blocked", "error": str(error),
                        "argument_package": {"claims": [{"id": "claim-1"}]},
                    }
                    raise error

                def review(_stage, _assignment, _descriptor, *, stage_result=None):
                    packets.append(stage_result)
                    return {"reports": [], "by_role": {}, "usage": {},
                            "model_enabled": False, "packet": {}}

                runner._run_stage = failed
                runner._run_specialist_pool = review
                runner._publish_specialist_reports = lambda _s, _a, bundle: bundle
                runner._run_specialist_verifier = lambda *_args, **_kwargs: None
                result = runner.run()

                self.assertEqual(len(packets), 1)
                self.assertEqual(
                    packets[0]["argument_package"]["claims"][0]["id"], "claim-1")
                self.assertTrue(any(
                    item.get("action") == "failure_specialist_review_completed"
                    for item in result["department_activity"]))
            finally:
                runner.close()

    def test_failed_survey_handoff_is_rehydrated_before_dependency_admission(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            survey_dir = root / "survey" / "continuations" / "cycle-3"
            (survey_dir / "output").mkdir(parents=True)
            (survey_dir / "output" / "run.json").write_text(json.dumps({
                "status": "blocked",
                "survey_ref": "artifact:survey/current@1",
                "assessment_ref": None,
                "nomination_ref": "artifact:nomination@1",
                "survey_current": True,
                "assessment_current": False,
                "gap_state": "insufficient_evidence",
            }))
            runner = ComposerRunner(workflow)
            try:
                runner.stage_records["survey"] = {
                    "kind": "survey",
                    "status": "candidate_needs_review",
                    "composer_decision": "advance_with_findings",
                    "attempts": [{"state": "failed", "cycle": 3,
                                  "project_dir": str(survey_dir)}],
                }
                runner.context["survey"] = {
                    "kind": "survey", "status": "candidate_needs_review",
                    "forward_progress": True,
                }
                runner._hydrate_provisional_handoffs()
                self.assertEqual(
                    runner.context["survey"]["survey_ref"],
                    "artifact:survey/current@1",
                )
                self.assertIsNone(runner.context["survey"]["assessment_ref"])
                self.assertEqual(
                    runner.context["survey"]["project_dir"],
                    str(survey_dir.resolve()),
                )
            finally:
                runner.close()

    def test_openalex_cooldown_admits_bounded_crossref_fallback(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            descriptor = root / "survey-descriptor.json"
            descriptor.write_text(json.dumps({
                "survey": {
                    "bibliography_fallback": "disabled",
                    "identity": {"adapter": "crossref"},
                }
            }))
            workflow["stages"][0]["config_path"] = str(descriptor.resolve())
            runner = ComposerRunner(workflow)
            try:
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "paused",
                    "error": "ProviderCooldownError: OpenAlex survey retrieval is paused until the provider quota resets",
                }
                runner.retry_schedule["survey"] = {
                    "error": "ProviderCooldownError: OpenAlex daily quota",
                    "not_before_epoch": time.time() + 86400,
                }
                self.assertTrue(runner._resume_survey_provider_fallback({
                    item["id"]: item for item in workflow["stages"]
                }))
                self.assertNotIn("survey", runner.retry_schedule)
                self.assertEqual(
                    runner.context["survey"]["provider_fallback"]["mode"],
                    "crossref_metadata",
                )
                config = {
                    "project_id": str(root / "survey"),
                    "survey": {
                        "revision": 5,
                        "bibliography_fallback": "disabled",
                        "seed_work_ids": [],
                        "search": {
                            "max_works": 120, "max_analyzed_works": 20,
                            "challenge_reserve": 5, "queries_per_role": 3,
                            "results_per_query": 10, "max_api_calls": 900,
                            "max_full_texts": 40, "expansion_rounds": 3,
                            "expansion_seed_count": 3, "saturation_rounds": 3,
                        },
                    },
                }
                with patch.object(runner, "_augment_full_text_routes"):
                    adapted = runner._adapt_continuation_config(
                        workflow["stages"][0], config)
                self.assertEqual(
                    adapted["survey"]["bibliography_fallback"], "crossref_metadata")
                self.assertEqual(adapted["survey"]["search"]["max_works"], 20)
                self.assertEqual(adapted["survey"]["search"]["expansion_rounds"], 0)
                self.assertEqual(adapted["survey"]["search"]["max_api_calls"], 32)
            finally:
                runner.close()

    def test_resume_invalidates_legacy_route_agnostic_specialist_cooldown(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.stage_records["experiment"] = {
                    "kind": "experiment", "status": "paused",
                    "error": "ProviderCooldownError: specialist provider is cooling down",
                }
                runner.retry_schedule["experiment"] = {
                    "delay_seconds": 86400,
                    "error": "ProviderCooldownError: specialist provider is cooling down",
                    "failed_attempt_number": 64,
                    "next_attempt_number": 65,
                    "not_before_epoch": time.time() + 86400,
                }
                runner.retry_schedule["argument"] = {
                    "delay_seconds": 15,
                    "error": "ProviderCooldownError: specialist provider is cooling down",
                    "route_id": "ollama-glm",
                    "not_before_epoch": time.time() + 15,
                }
                stages = {item["id"]: item for item in workflow["stages"]}
                invalidated = runner._invalidate_legacy_provider_retry_schedules(stages)
                self.assertEqual([item["stage_id"] for item in invalidated], ["experiment"])
                self.assertNotIn("experiment", runner.retry_schedule)
                self.assertIn("argument", runner.retry_schedule)
                self.assertEqual(runner.stage_records["experiment"]["status"], "retrying")
                self.assertTrue(any(
                    item.get("action") == "invalidate_legacy_provider_retry_schedules"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_stage_work_order_projection_does_not_cross_contaminate_reopened_stages(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            runner.active_research_requests = [
                {"id": "survey-repair", "source_stage_id": "survey",
                 "kind": "literature_expansion", "objective": "Refresh the literature map."},
                {"id": "experiment-repair", "source_stage_id": "experiment",
                 "kind": "additional_experiment", "objective": "Run a control."},
            ]
            runner.reopened_stage_ids = {"survey", "experiment"}
            self.assertEqual(
                [item["id"] for item in runner._requests_for_stage("survey")],
                ["survey-repair"],
            )
            self.assertEqual(
                [item["id"] for item in runner._requests_for_stage("experiment")],
                ["experiment-repair"],
            )
            runner.close()

    def test_argument_repair_targets_argument_and_outranks_unrelated_ready_work(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                request = {
                    "id": "repair-argument-review",
                    "kind": "interpretation_expansion",
                    "owner": "strategy.interpretation",
                    "objective": "Repair the reviewed claim-evidence graph.",
                    "why": "The independent argument review found five scoped repairs.",
                    "success_condition": "The revised argument passes independent adjudication.",
                    "evidence_needed": "The prior argument and fresh evidence links.",
                    "source_stage_id": "argument",
                    "target_stage_id": "argument",
                    "target_stage_kind": "argument",
                    "repair_priority": "immediate",
                }
                by_id = {
                    "survey": {"id": "survey", "kind": "survey", "depends_on": []},
                    "argument": {"id": "argument", "kind": "argument", "depends_on": []},
                    "paper": {"id": "paper", "kind": "paper", "depends_on": ["argument"]},
                }
                self.assertEqual(
                    ComposerRunner._continuation_targets([request], by_id),
                    {"argument", "paper"},
                )
                runner.active_research_requests = [request]
                runner.reopened_stage_ids = {"argument", "paper"}
                runner.workflow["agenda_policy"] = {"mode": "adaptive"}
                ordered = runner._agenda_order(
                    [by_id["survey"], by_id["argument"]],
                    completed=set(), by_id=by_id,
                )
                self.assertEqual(ordered[0]["id"], "argument")
                self.assertEqual(
                    [item["id"] for item in runner._requests_for_stage("argument")],
                    ["repair-argument-review"],
                )
                self.assertEqual(runner._requests_for_stage("survey"), [])
            finally:
                runner.close()

    def test_legacy_argument_recovery_gets_an_explicit_execution_address(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            runner = ComposerRunner(self._workflow(root))
            try:
                request = runner._autonomous_recovery_request(
                    "argument",
                    {
                        "kind": "argument",
                        "status": "research_expansion_required",
                        "error": "independent adjudicator requested revision",
                    },
                )
                self.assertEqual(request["target_stage_id"], "argument")
                self.assertEqual(request["target_stage_kind"], "argument")
                self.assertEqual(request["repair_priority"], "immediate")
            finally:
                runner.close()

    def test_topic_refinement_supersedes_downstream_work_orders(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["progression_policy"] = "forward_first"
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            runner.context = {
                    "topic": {
                        "kind": "topic_discovery", "status": "research_expansion_required",
                        "topic_pivot": {"status": "required", "cycle": 3},
                        "topic": {
                            "id": "parent-topic", "title": "Old direction",
                            "research_question": "What explains the old observation?",
                            "domain": "plasma physics",
                            "phenomenon": "Collisionless magnetic reconnection",
                            "frontier_seed_id": "parent-seed",
                            "search_queries": ["collisionless reconnection scaling"],
                        },
                        "frontier_seed_plan": {"seeds": [{
                            "id": "parent-seed", "domain": "plasma physics",
                            "phenomenon": "Collisionless magnetic reconnection",
                            "mechanism": "Hall effect", "unit_of_analysis": "current sheet",
                            "search_queries": ["collisionless reconnection scaling"],
                        }]},
                        "recent_papers": [{
                            "work_id": "W-parent", "title": "Parent phenomenon",
                            "frontier_seed_id": "parent-seed",
                        }],
                        "candidate_prior_work": [{
                            "work_id": "W-prior", "title": "Targeted parent evidence",
                            "frontier_seed_id": "selected_direction",
                        }],
                        "format_recovery": True,
                        "format_recovery_attempts": 1,
                        "failure_recovery": {
                            "failure_class": "model_contract",
                            "recovery_mode": "format_repair_then_rerun",
                        },
                        "error": (
                            "topic discovery repeated the rejected response; previous validation "
                            "failure: topic candidate search query is not anchored to its scientific direction"
                        ),
                        "candidate_attempt_trace": [{
                            "status": "rejected",
                            "error": "topic candidate search query is not anchored to its scientific direction",
                        }],
                        "research_expansion_requests": [{
                        "id": "topic-repair", "kind": "topic_refinement",
                        "owner": "research.intelligence", "objective": "Change direction.",
                        "why": "The old direction was not supported.",
                        "success_condition": "A new admitted topic.",
                        "evidence_needed": "Source-grounded feasibility.",
                    }],
                    "research_requests": [{
                        "id": "topic-format-recovery", "kind": "recovery",
                        "owner": "research.intelligence",
                        "objective": "Repair the topic response contract.",
                        "why": "The prior complete response failed deterministic validation.",
                        "success_condition": "A fresh complete, locally valid topic package.",
                        "evidence_needed": "The validation diagnostic and a new topic package.",
                        "target_stage_id": "topic",
                        "target_stage_kind": "topic_discovery",
                        "repair_priority": "immediate",
                        "recovery_mode": "format_repair_then_rerun",
                    }],
                },
                "experiment": {
                    "kind": "experiment", "status": "research_expansion_required",
                    "research_expansion_requests": [{
                        "id": "stale-experiment-repair", "kind": "additional_experiment",
                        "owner": "methods.validation", "objective": "Repair the old experiment.",
                        "why": "The previous frontier failed.",
                        "success_condition": "A valid result.",
                        "evidence_needed": "Raw output.",
                    }],
                },
            }
            requests = runner._continuation_requests()
            self.assertEqual(
                [item["id"] for item in requests],
                ["topic-format-recovery"],
            )
            runner.active_research_requests = requests
            refinement = runner._topic_refinement_context(workflow["stages"][0])
            self.assertEqual(refinement["mode"], "response_contract_repair")
            self.assertEqual(
                refinement["response_contract_repair"]["validation_error"],
                "topic candidate search query is not anchored to its scientific direction",
            )
            self.assertNotIn("work_orders", refinement)
            repair = requests[0]
            repair_signature = runner._research_request_signature(repair)
            runner._attempted_request_signatures.add(repair_signature)
            repair_only = runner._continuation_requests()
            self.assertEqual([item["id"] for item in repair_only], ["topic-repair"])
            runner.workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 2}
            runner.continuation_cycles = 12
            runner._continuation_budget_baseline = 0
            by_id = {item["id"]: item for item in workflow["stages"]}
            # The check above simulated a completed format-repair request.
            # Remove that completion marker to exercise its first admission;
            # otherwise only the queued scientific refinement remains and
            # this assertion would test a different continuation.
            runner._attempted_request_signatures.discard(repair_signature)
            runner.active_research_requests = []
            self.assertTrue(runner._begin_continuation(set(by_id), by_id))
            self.assertEqual(
                runner.feedback[-1]["cycle_budget_override"],
                "bounded_model_contract_recovery",
            )
            self.assertIn("topic", runner.reopened_stage_ids)
            self.assertTrue(runner.context["topic"]["format_recovery_dispatched"])
            runner._attempted_request_signatures.add(repair_signature)
            repair_only = runner._continuation_requests()
            self.assertEqual([item["id"] for item in repair_only], ["topic-repair"])
            runner.active_research_requests = repair_only
            repair_refinement = runner._topic_refinement_context(workflow["stages"][0])
            self.assertEqual(repair_refinement["objective"], "Change direction.")
            self.assertEqual(
                repair_refinement["parent_evidence"]["frontier_seed_plan"]["seeds"][0]["id"],
                "parent-seed",
            )
            self.assertEqual(
                [item["work_id"] for item in repair_refinement["parent_evidence"]["candidate_prior_work"]],
                ["W-prior"],
            )
            self.assertEqual(
                repair_refinement["work_orders"][0]["objective"],
                "Change direction.",
            )
            self.assertEqual(
                repair_refinement["survey_feedback"]["requests"][0]["id"],
                "topic-repair",
            )
            runner.continuation_cycles = 13
            self.assertTrue(runner._begin_continuation(set(by_id), by_id))
            self.assertEqual(runner.active_research_requests[0]["id"], "topic-repair")
            self.assertEqual(
                runner.feedback[-1]["cycle_budget_override"],
                "adaptive_topic_pivot",
            )
            runner.close()

    def test_restored_requests_are_fenced_to_the_new_topic_frontier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "completed",
                "topic": {"id": "new-topic"},
                "topic_evolution": {"mode": "refinement", "cycle": 3},
            }
            runner.stage_records["topic"] = {"status": "completed"}
            requests = [
                {"id": "legacy", "source_stage_id": "experiment"},
                {"id": "current", "source_stage_id": "experiment",
                 "topic_id": "new-topic", "topic_cycle": 3},
            ]
            self.assertEqual(
                [item["id"] for item in runner._scope_active_research_requests(requests)],
                ["current"],
            )
            runner.close()

    def test_terminal_topic_failure_closes_active_revalidation_order(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            request = {
                "id": "topic-runtime-feasibility-revalidation",
                "kind": "topic_refinement",
                "owner": "research.intelligence",
                "objective": "Replan the selected direction.",
                "why": "The checkpoint predates the feasibility contract.",
                "success_condition": "A valid executable plan is admitted.",
                "evidence_needed": "Runtime inventory and bounded experiment plan.",
            }
            runner.active_research_requests = [request]
            active = runner.departments.activate_work_orders(runner.active_research_requests)
            self.assertEqual(active[0]["task_state"], "running")
            runner._resolve_terminal_stage_work_orders({"id": "topic", "kind": "topic_discovery"})
            task = runner.tasks.get(active[0]["task_id"])
            self.assertEqual(task["state"], "blocked")
            runner.close()

    def test_topic_budget_admission_snapshot_is_not_charged_twice(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            error = QuotaExceededError(
                "topic discovery mission quota exhausted",
                dimension="model_calls", limit=8, observed=8,
                usage={}, diagnostics=[{"kind": "composer_topic_budget"}],
            )
            error.usage_is_snapshot = True
            self.assertEqual(
                runner._record_failed_stage_usage(error),
                {"model_calls": 0, "input_tokens": 0,
                 "output_tokens": 0, "openalex_requests": 0})
            runner.close()

    def test_local_topic_budget_exhaustion_resumes_same_topic_lineage(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            workflow["stages"] = [{
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner._continuation_budget_baseline = 1
            prior_request = {
                "id": "auto-topic-recovery-1",
                "kind": "topic_refinement",
                "owner": "research.intelligence",
                "objective": "Generate a materially different research question.",
                "why": "The prior bounded topic intake failed.",
                "success_condition": "Admit a source-grounded, feasible topic.",
                "evidence_needed": "The prior topic failure and its evidence.",
                "source_stage_id": "topic",
            }
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "status": "format_recovery_required",
                "format_recovery": True,
                "format_recovery_attempts": 2,
                "format_recovery_dispatched": True,
                "failure_recovery": {
                    "failure_class": "model_contract",
                    "recovery_mode": "format_repair_then_rerun",
                },
                "research_requests": [prior_request],
                "topic": {"id": "old", "title": "Old", "research_question": "Old?"},
                "research_expansion_requests": [],
            }
            runner.active_research_requests = [prior_request]
            runner._attempted_request_signatures.add(
                runner._research_request_signature(prior_request))
            error = QuotaExceededError(
                "topic discovery quota exhausted: model_calls=10, limit=10",
                dimension="model_calls", limit=10, observed=10,
                usage={}, diagnostics=[{"kind": "composer_topic_budget"}],
            )
            error.topic_budget_scope = "continuation"
            self.assertTrue(runner._admit_scientific_blocker_recovery(
                workflow["stages"][0], error, set(), {"topic": workflow["stages"][0]}))
            self.assertEqual(runner.continuation_cycles, 2)
            self.assertIn("topic", runner.reopened_stage_ids)
            self.assertTrue(any(
                item.get("action") == "resume_topic_intake_after_budget_exhaustion"
                for item in runner.department_activity
            ))
            self.assertEqual(
                runner.active_research_requests[0]["id"],
                "auto-topic-budget-recovery-2")
            self.assertEqual(
                runner.active_research_requests[0]["recovery_mode"],
                "continue_same_topic_after_local_budget")
            self.assertIn(
                "Keep the selected scientific phenomenon",
                runner.active_research_requests[0]["objective"])
            runner.close()

    def test_topic_contract_failure_runs_before_pending_scientific_refinement(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"] = [topic_stage]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner._continuation_budget_baseline = 1
            topic = {
                "id": "direction-v1", "title": "Current direction",
                "phenomenon": "The retained physical phenomenon",
                "research_question": "Does the retained mechanism change the measured boundary?",
            }
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "format_recovery_required",
                "topic": topic, "format_recovery": True,
                "format_recovery_attempts": 21,
                "failure_recovery": {
                    "failure_class": "model_contract",
                    "recovery_mode": "format_repair_then_rerun",
                    "model_diagnostics": {
                        "topic_response_repair": {
                            "previous_validation_error": (
                                "topic candidate prior_work_ids cite records outside the supplied evidence"
                            ),
                        },
                    },
                },
                "research_requests": [],
                "research_expansion_requests": [{
                    "id": "auto-topic-budget-recovery-2",
                    "kind": "topic_refinement",
                    "owner": "research.intelligence",
                    "objective": "Continue the existing topic after its local intake budget reset.",
                    "why": "A local attempt budget is not scientific evidence against the topic.",
                    "success_condition": "Continue the same topic without changing its identity.",
                    "evidence_needed": "The existing topic and its preserved evidence.",
                    "recovery_mode": "continue_same_topic_after_local_budget",
                }],
            }
            error = QuotaExceededError(
                "topic discovery bounded intake exhausted after 4 candidate attempts",
                dimension="topic_attempts", limit=8, observed=8,
                usage={}, diagnostics=[{"kind": "composer_topic_budget"}],
            )
            error.topic_budget_scope = "intake"
            error.retryable_topic_intake = True
            try:
                self.assertTrue(runner._admit_scientific_blocker_recovery(
                    topic_stage, error, set(), {"topic": topic_stage}))
                self.assertEqual(runner.continuation_cycles, 2)
                active_ids = {item["id"] for item in runner.active_research_requests}
                self.assertNotIn("auto-topic-budget-recovery-2", active_ids)
                repair_requests = [
                    item for item in runner.active_research_requests
                    if item.get("recovery_mode") == "format_repair_then_rerun"
                ]
                self.assertEqual(len(repair_requests), 1)
                self.assertEqual(runner.context["topic"]["topic"], topic)
                self.assertTrue(runner.context["topic"]["format_recovery_dispatched"])
                self.assertTrue(any(
                    item.get("action") == "admit_model_contract_recovery"
                    for item in runner.department_activity
                ))
                refinement = runner._topic_refinement_context(topic_stage)
                self.assertEqual(refinement["mode"], "response_contract_repair")
                self.assertNotIn("work_orders", refinement)
                self.assertEqual(
                    refinement["response_contract_repair"]["validation_error"],
                    "topic candidate prior_work_ids cite records outside the supplied evidence",
                )
            finally:
                runner.close()

    def test_pending_topic_pivot_is_not_lost_when_budget_recovery_is_active(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"] = [topic_stage]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            runner = ComposerRunner(workflow)
            parent = {
                "id": "direction-mhd", "title": "Hall reconnection scaling",
                "phenomenon": "Collisionless magnetic reconnection",
                "research_question": (
                    "Does the normalized reconnection rate scale with d_i/d_e or saturate?"),
                "research_form": "experimental_design",
                "evidence_mode": "analytical_derivation",
                "comparison_type": "model_selection",
            }
            budget_request = {
                "id": "auto-topic-budget-recovery-443",
                "kind": "topic_refinement",
                "owner": "research.intelligence",
                "objective": "Continue the selected direction after its local budget resets.",
                "why": "A local budget is not evidence against the topic.",
                "success_condition": "Retain the existing evidence lineage.",
                "evidence_needed": "The current topic and its source-backed pivot decision.",
                "source_stage_id": "topic",
                "target_stage_id": "topic",
                "recovery_mode": "continue_same_topic_after_local_budget",
            }
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "status": "research_expansion_required",
                "topic": parent,
                "topic_pivot": {
                    "status": "required", "cycle": 417,
                    "source_stage_id": "experiment",
                    "reason": "the experiment response contract failed its bounded repair pass",
                },
                "topic_evolution": {
                    "mode": "refinement",
                    "salvage": {
                        "mode": "structural_pivot",
                        "attempted_branch_ids": [
                            "mechanism-observable", "comparison-baseline", "evidence-boundary",
                        ],
                    },
                },
                "research_requests": [],
                "research_expansion_requests": [budget_request],
            }
            runner.context["experiment"] = {
                "kind": "experiment", "status": "topic_pivot_pending",
                "superseded_scope_failure": {
                    "failure_class": "model_contract",
                    "failure_dossier_ref": "artifact:experiment/failure-dossier@1",
                },
            }
            runner.active_research_requests = [budget_request]
            try:
                refinement = runner._topic_refinement_context(topic_stage)
                self.assertEqual(refinement["mode"], "refinement")
                self.assertEqual(refinement["parent_topic_id"], parent["id"])
                self.assertEqual(refinement["parent_topic"], parent)
                self.assertEqual(
                    refinement["salvage_plan"]["mode"], "structural_pivot")
                objectives = [item["objective"] for item in refinement["work_orders"]]
                self.assertEqual(len(objectives), 1)
                self.assertIn("already-decided topic refinement", objectives[0])
                self.assertFalse(any("Continue the selected direction" in item
                                     for item in objectives))
                self.assertFalse(runner._is_topic_pivot_request(budget_request))
            finally:
                runner.close()

    def test_resume_reclassifies_off_parent_salvage_as_topic_refinement(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_dir = root / "topic"
            topic_dir.mkdir()
            stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"] = [stage]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            runner = ComposerRunner(workflow)
            parent = {
                "id": "direction-mhd", "title": "Hall reconnection scaling",
                "phenomenon": "Collisionless magnetic reconnection",
                "research_question": "Does the reconnection rate scale with d_i/d_e?",
            }
            validation_error = (
                "topic salvage branch structural_pivot must preserve the parent's phenomenon")
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "research_expansion_required",
                "topic": parent,
                "topic_pivot": {
                    "status": "required", "cycle": 417,
                    "source_stage_id": "experiment", "reason": "experiment repair exhausted",
                },
                "topic_evolution": {"salvage": {
                    "mode": "structural_pivot",
                    "attempted_branch_ids": [
                        "mechanism-observable", "comparison-baseline", "evidence-boundary",
                    ],
                }},
                "format_recovery": True,
                "format_recovery_attempts": 27,
                "format_recovery_dispatched": True,
                "failure_dossier_ref": "artifact:failure/topic@223",
                "failure_recovery": {
                    "failure_class": "model_contract",
                    "recovery_mode": "format_repair_then_rerun",
                    "dossier_ref": "artifact:failure/topic@223",
                    "model_diagnostics": {"topic_response_repair": {
                        "previous_validation_error": validation_error,
                    }},
                },
                "rejected_topic_history": [{
                    "topic_id": "off-parent-direction", "title": "Unrelated direction",
                    "domain": "computational science",
                    "research_question": "Does an unrelated metric change?",
                    "rejection_type": "refinement", "rejection_reason": validation_error,
                }],
                "research_requests": [{
                    "id": "stale-format-repair", "kind": "recovery",
                    "owner": "research.intelligence", "objective": "Repair formatting.",
                    "why": "A prior response failed.",
                    "success_condition": "A valid response.",
                    "evidence_needed": "The exact schema failure.",
                    "target_stage_id": "topic", "target_stage_kind": "topic_discovery",
                    "repair_priority": "immediate",
                    "recovery_mode": "format_repair_then_rerun",
                }],
                "research_expansion_requests": [{
                    "id": "stale-budget-reset", "kind": "topic_refinement",
                    "owner": "research.intelligence", "objective": "Reset local budget.",
                    "why": "A previous intake used its allocation.",
                    "success_condition": "Continue the same topic.",
                    "evidence_needed": "The previous budget.",
                    "recovery_mode": "continue_same_topic_after_local_budget",
                }],
            }
            runner.stage_records["topic"] = {
                "kind": "topic_discovery", "status": "running",
                "attempt_number": 224, "task_id": "interrupted-topic-task",
            }
            runner.active_research_requests = [
                runner.context["topic"]["research_requests"][0]]
            try:
                self.assertTrue(runner._reconcile_restored_topic_refinement_failure(
                    set(), {"topic": stage}))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertEqual(runner.active_research_requests[0]["kind"], "topic_refinement")
                self.assertNotEqual(
                    runner.active_research_requests[0].get("recovery_mode"),
                    "continue_same_topic_after_local_budget",
                )
                restored = runner.context["topic"]
                self.assertEqual(restored["topic"], parent)
                self.assertEqual(restored["status"], "research_expansion_required")
                self.assertNotIn("format_recovery", restored)
                self.assertNotIn("failure_recovery", restored)
                self.assertEqual(
                    restored["superseded_scope_failure"]["failure_dossier_ref"],
                    "artifact:failure/topic@223",
                )
                self.assertIn(validation_error, restored["topic_pivot"]["reason"])
                self.assertTrue(any(
                    item.get("action") == "resume_parent_preserving_topic_refinement"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_admitted_topic_budget_exhaustion_resumes_its_survey_not_a_new_topic(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"].insert(0, topic_stage)
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner._continuation_budget_baseline = 1
            topic = {
                "id": "direction-v1", "title": "Current scientific direction",
                "phenomenon": "A supported phenomenon",
                "research_question": "Does the supported mechanism shift the measured boundary?",
            }
            runner.context["topic"] = {
                "kind": "topic_discovery", "status": "completed",
                "admission_state": "provisional_for_survey", "topic": topic,
            }
            runner.context["survey"] = {
                "kind": "survey", "status": "completed",
                "gap_state": "insufficient_evidence",
                "survey_ref": "artifact:survey/current@1",
                "assessment_ref": "artifact:survey/gap-assessment@1",
            }
            by_id = {stage["id"]: stage for stage in workflow["stages"]}
            error = QuotaExceededError(
                "topic discovery quota exhausted: model_calls=10, limit=10",
                dimension="model_calls", limit=10, observed=10, usage={},
                diagnostics=[{"kind": "composer_topic_budget"}],
            )
            error.topic_budget_scope = "continuation"
            try:
                self.assertTrue(runner._admit_scientific_blocker_recovery(
                    topic_stage, error, {"topic", "survey"}, by_id))
                request = runner.active_research_requests[0]
                self.assertEqual(request["kind"], "literature_expansion")
                self.assertEqual(request["target_stage_id"], "survey")
                self.assertEqual(
                    request["recovery_mode"], "continue_same_topic_after_local_budget")
                self.assertEqual(runner.context["topic"]["topic"], topic)
                self.assertNotIn("topic", runner.reopened_stage_ids)
                self.assertEqual(runner.reopened_stage_ids, {"survey", "experiment"})
                self.assertTrue(any(
                    item.get("action") == "continue_same_topic_after_local_budget"
                    for item in runner.department_activity))
            finally:
                runner.close()

    def test_topic_stage_quota_overflow_opens_a_fresh_continuation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_config = root / "topic.json"
            topic_config.write_text("{}")
            topic_stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
                "quota": {"max_model_calls": 8, "max_input_tokens": 1000,
                          "max_output_tokens": 500, "max_openalex_requests": 12},
            }
            workflow["stages"] = [topic_stage]
            workflow["completion"]["required_stage_ids"] = ["topic"]
            runner = ComposerRunner(workflow)
            try:
                error = QuotaExceededError(
                    "stage topic quota exhausted: model_calls=9 > 8",
                    dimension="max_model_calls", limit=8, observed=9,
                    usage={"model_calls": 9},
                )
                by_id = {"topic": topic_stage}
                self.assertTrue(runner._admit_stage_quota_recovery(
                    topic_stage, error, set(), by_id))
                self.assertEqual(runner.continuation_cycles, 1)
                self.assertIn("topic", runner.reopened_stage_ids)
                self.assertTrue(any(
                    item.get("action") == "resume_topic_intake_after_budget_exhaustion"
                    for item in runner.department_activity
                ))
            finally:
                runner.close()

    def test_until_deadline_retry_mode_does_not_stop_at_attempt_counter(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["retry_policy"] = {
                "mode": "until_deadline", "max_attempts": 1, "backoff_seconds": 0,
            }
            runner = ComposerRunner(workflow)
            calls = []

            def recovers_after_four_attempts(stage, **kwargs):
                calls.append(stage["project_dir"])
                if len(calls) < 4:
                    raise RuntimeError("transient validation failure")
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"], "attempt": len(calls)}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = recovers_after_four_attempts
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(calls), 5)  # four survey attempts, then experiment
            self.assertEqual(calls[3], calls[0])
            self.assertEqual(result["retry_policy"]["mode"], "until_deadline")
            self.assertTrue(all(item.get("retry_mode") == "until_deadline"
                                for item in result["feedback"] if item["action"] == "retry_stage"))

    def test_until_deadline_dispatches_a_residual_stage_window(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["stages"][1]["estimate_seconds"] = 20
            workflow["retry_policy"] = {
                "mode": "until_deadline", "backoff_seconds": 0,
            }
            runner = ComposerRunner(workflow)
            calls = []

            def residual_stage(stage, **kwargs):
                calls.append(stage["id"])
                if stage["id"] == "survey":
                    # Leave less time than the experiment forecast while
                    # retaining a safe control-plane dispatch margin.
                    runner.deadline = runner.clock() + 1.5
                    runner.deadline_epoch = time.time() + 1.5
                output = root / f"{stage['id']}-residual.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = residual_stage
            result = runner.run()
            self.assertEqual(calls, ["survey", "experiment"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["deadline_decisions"][0]["admission"], "residual_window")

    def test_stage_return_after_hard_wall_cannot_be_reported_as_completed(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)

            def late_stage(stage, **kwargs):
                output = root / f"{stage['id']}-late.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                if stage["id"] == "experiment":
                    runner.deadline = runner.clock() - 1
                    runner.deadline_epoch = time.time() - 1
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = late_stage
            result = runner.run()
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["interim_report"]["stop_reason"], "hard_deadline")
            self.assertTrue(any("deadline" in item.get("reason", "")
                                for item in result["blockers"]))

    def test_bounded_mode_pauses_when_the_required_stage_window_does_not_fit(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["stages"][1]["estimate_seconds"] = 20
            workflow["retry_policy"] = {
                "mode": "bounded", "max_attempts": 1, "backoff_seconds": 0,
            }
            runner = ComposerRunner(workflow)
            calls = []

            def strict_stage(stage, **kwargs):
                calls.append(stage["id"])
                if stage["id"] == "survey":
                    runner.deadline = runner.clock() + 1.5
                    runner.deadline_epoch = time.time() + 1.5
                output = root / f"{stage['id']}-strict.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = strict_stage
            result = runner.run()
            self.assertEqual(calls, ["survey"])
            self.assertEqual(result["status"], "paused")
            self.assertEqual(result["interim_report"]["stop_reason"],
                             "required_stage_window_does_not_fit_remaining_deadline")
            self.assertEqual(result["deadline_decisions"][0]["admission"], "deferred")

    def test_ten_hour_composer_hard_wall_is_valid(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            workflow["time_policy"] = {"first_result_seconds": 600,
                                        "target_seconds": 18000,
                                        "hard_seconds": 36000,
                                        "checkpoint_seconds": 30}
            self.assertEqual(validate_workflow(workflow)["time_policy"]["hard_seconds"], 36000)

    def test_omitted_retry_policy_is_deadline_governed(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            runner = ComposerRunner(workflow)
            self.assertEqual(runner._retry_policy()["mode"], "until_deadline")
            self.assertIsNone(runner._retry_policy()["max_attempts"])
            runner.close()

    def test_topic_history_path_requires_an_absolute_path(self):
        with tempfile.TemporaryDirectory() as path:
            workflow = self._workflow(Path(path))
            workflow["topic_history_path"] = "relative-topic-history.json"
            with self.assertRaisesRegex(ValidationError, "absolute"):
                validate_workflow(workflow)

    def test_foundry_backed_topic_hides_templates_and_materializes_admitted_program(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            model_path = root / "model.json"
            model_path.write_text(json.dumps({
                "protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
                "model": "stub", "timeout_seconds": 60, "max_output_tokens": 128,
            }))
            requirements = root / "requirements.txt"
            requirements.write_text("numpy==2.5.2\n")
            descriptor_path = root / "generated-capability.json"
            descriptor_path.write_text(json.dumps({
                "schema_version": "experiment-capability-1", "capability_id": "generated_frontier",
                "experiment": {
                    "id": "generated_frontier", "revision": 1, "study_type": "exploratory",
                    "domain": "marine ecology", "research_question": "Does transport alter patch recovery?",
                    "hypothesis": "Transport alters recovery.", "method": "Run a seeded comparison.",
                    "parameters": {}, "seed": 3, "run_count": 5,
                    "stopping_rule": "Run five replicates.", "primary_outcomes": [],
                    "limitations": ["Synthetic boundary."], "literature_gate": None,
                    "execution": {}, "validation": {}, "required_assets": [], "reviewers": [],
                    "stage_seconds": {}, "max_observations": 5, "max_asset_bytes": 100,
                },
            }))
            foundry_config = root / "foundry.json"
            foundry_config.write_text(json.dumps({
                "schema_version": "capability-foundry-config-1",
                "model_config_path": str(model_path.resolve()),
                "runtime_python": str(Path(sys.executable).resolve()),
                "workspace_root": str((root / "foundry-workspace").resolve()),
                "registry_root": str((root / "registry").resolve()),
                "repo_root": str(root.resolve()),
                "requirements_file": str(requirements.resolve()),
                "runtime_packages": [{"name": "numpy", "version": "2.5.2"}],
                "max_attempts": 2, "timeout_seconds": 30,
            }))
            workflow["capability_foundry_config_path"] = str(foundry_config.resolve())
            workflow["experiment_catalog"] = [{
                "id": "fallback", "config_path": str(descriptor_path.resolve())}]
            validate_workflow(workflow)
            runner = ComposerRunner(workflow)
            runtime_context = runner._runtime_context(json.loads(model_path.read_text()))
            self.assertEqual(runtime_context["experiment_catalog"], [])
            self.assertEqual(len(runtime_context["fallback_experiment_catalog"]), 1)
            self.assertTrue(runtime_context["capability_foundry"]["enabled"])
            self.assertTrue(runtime_context["python_packages"]["numpy"])
            self.assertEqual(
                runtime_context["capability_foundry"]["runtime_packages"],
                [{"name": "numpy", "version": "2.5.2"}],
            )
            self.assertEqual(
                runtime_context["capability_foundry"]["allowed_evidence_modes"],
                ["analytical_derivation", "synthetic_simulation"],
            )
            self.assertEqual(runtime_context["capability_foundry"]["timeout_seconds"], 30)
            self.assertEqual(runtime_context["research_feasibility"]["max_model_calls"], 0)
            self.assertEqual(runtime_context["research_feasibility"]["max_external_requests"], 0)
            self.assertEqual(runtime_context["research_feasibility"]["max_experiment_seconds"], 10)
            result = {
                "status": "completed",
                "topic": {"id": "frontier", "title": "Patch recovery", "domain": "marine ecology",
                          "research_question": "Does transport alter patch recovery?", "scope": "Synthetic patches",
                          "disconfirmation_test": "No recovery difference.", "resource_plan": "Seeded simulation."},
                "candidates": [{"id": "frontier"}], "candidate_prior_work": [],
                "source_challenge": {"decision": "admit_to_survey"},
            }
            generated = {
                "status": "registered", "attempts": 1,
                "registration": {"capability_id": "generated_frontier",
                                 "descriptor_path": str(descriptor_path.resolve())},
                "admission": {"gates": ["static_scan", "independent_recalculation"]},
            }
            from scisaurus.runtime.research_quality import default_research_quality_contract
            quality_contract = default_research_quality_contract()
            with patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate",
                       return_value=generated) as call:
                result = runner._materialize_topic_capability(
                    result, quality_contract=quality_contract)
            self.assertEqual(result["topic"]["experiment_capability_id"], "generated_frontier")
            self.assertEqual(call.call_args.kwargs["required_intent"]["research_question"],
                             "Does transport alter patch recovery?")
            self.assertEqual(
                call.call_args.kwargs["required_intent"]["quality_contract"],
                quality_contract)
            runner.context["topic"] = {"kind": "topic_discovery", **result}
            config = {"experiment": {"revision": 1, "literature_gate": {"required_state": "eligible_for_experiment"}},
                      "supplied_context": "base"}
            with patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate", return_value=generated):
                projected = runner._apply_topic_to_experiment_config(workflow["stages"][1], config)
            self.assertEqual(projected["experiment"]["id"], "generated_frontier")
            entry = {"id": "generated_frontier", "revision": 1,
                     "path": str(descriptor_path), "candidate_record_sha256": "a" * 64}
            admission_path = root / "admission.json"
            admission_path.write_text(json.dumps({"adversarial_review": {"status": "admitted", "findings": []}}))
            with patch("scisaurus.runtime.capability_registry.load_registry", return_value={"capabilities": [entry]}), \
                    patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate", return_value=generated) as upgrade:
                runner._materialize_topic_capability(result)
            self.assertEqual(upgrade.call_args.kwargs["required_intent"]["revision"], 2)
            from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests
            valid_review = {**CapabilityFoundryTests._review_payload(), "role": "review.methods",
                "review_method": "independent_model", "candidate_sha256": "a" * 64}
            for verdict in ({**valid_review, "status": "rejected"},
                            {name: value for name, value in valid_review.items() if name != "status"}):
                admission_path.write_text(json.dumps({"adversarial_review": verdict}))
                with patch("scisaurus.runtime.capability_registry.load_registry", return_value={"capabilities": [entry]}), \
                        patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate", return_value=generated) as upgrade:
                    runner._materialize_topic_capability(result)
                upgrade.assert_called_once()
            admission_path.write_text(json.dumps({"adversarial_review": valid_review}))
            with patch("scisaurus.runtime.capability_registry.load_registry", return_value={"capabilities": [entry]}), \
                    patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate") as regenerate_existing:
                checked = runner._materialize_topic_capability(result)
            regenerate_existing.assert_not_called()
            self.assertTrue(checked["generated_capability"]["reused"])
            lazy_result = json.loads(json.dumps(result))
            lazy_result.pop("generated_capability")
            lazy_result["topic"].pop("experiment_capability_id", None)
            runner.context["topic"] = {"kind": "topic_discovery", **lazy_result}
            with patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate",
                       return_value=generated), \
                    patch.object(runner, "_materialize_topic_capability",
                                 wraps=runner._materialize_topic_capability) as materialize:
                runner._apply_topic_to_experiment_config(workflow["stages"][1], {
                    "experiment": {"revision": 1,
                                   "literature_gate": {"required_state": "eligible_for_experiment"}},
                    "supplied_context": "base"})
            materialize.assert_called_once()
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"experiment"}
            runner.active_research_requests = [{
                "id": "run-control", "kind": "additional_experiment", "owner": "methods.validation",
                "objective": "Run a control that separates the mechanisms.",
                "why": "The first result left both explanations viable.",
                "success_condition": "The new result changes the mechanism decision.",
                "evidence_needed": "Raw observations and independent recalculation.",
            }]
            runner.context["topic"] = {"kind": "topic_discovery", "topic": result["topic"],
                                         "generated_capability": generated}
            # A fresh capability is warranted only after the prior capability
            # produced an observed result that the scoped work order is meant
            # to extend. Before first execution, the admitted capability is
            # reused so authoring failures cannot starve the actual experiment.
            runner.context["experiment"] = {
                "kind": "experiment", "status": "research_expansion_required",
                "project_dir": str(root / "attempt-previous"),
                "capability_id": "generated_frontier", "study_id": "generated_frontier",
                "results_package": {
                    "schema_version": "results-package-1",
                    "id": "generated_frontier",
                    "metrics": [{"id": "observed_metric", "value": 1.0}],
                },
            }
            runner.stage_records["experiment"] = {
                "project_dir": str(root / "attempt-current"),
            }
            regenerated = {
                **generated,
                "registration": {
                    "capability_id": "frontier-cycle-1",
                    "descriptor_path": str(descriptor_path.resolve()),
                },
            }
            with patch("scisaurus.runtime.capability_foundry.CapabilityFoundry.generate",
                       return_value=regenerated) as regenerate, \
                    patch.object(runner, "_run_capability_repair_panel", return_value={
                        "schema_version": "capability-repair-panel-1",
                        "decision": "repair", "input_sha256": "b" * 64,
                        "root_causes": ["the observed result did not separate the explanations"],
                        "required_changes": ["add a discriminating control"],
                        "ledger": {"panel_stage_id": "experiment-repair-panel"},
                    }) as repair_panel:
                runner._apply_topic_to_experiment_config(workflow["stages"][1], {
                    "experiment": {"revision": 2,
                                   "literature_gate": {"required_state": "eligible_for_experiment"}},
                    "supplied_context": "base"})
            repair_panel.assert_called_once()
            self.assertEqual(regenerate.call_args.kwargs["required_intent"]["id"], "frontier-cycle-1")
            self.assertEqual(regenerate.call_args.kwargs["required_intent"]["revision"], 2)
            runner.close()

    def test_stale_experiment_result_does_not_count_for_current_capability(self):
        stale = {
            "study_id": "old_capability",
            "results_package": {
                "id": "old_capability",
                "metrics": [{"id": "old_metric", "value": 1.0}],
            },
        }
        self.assertFalse(
            ComposerRunner._has_executed_experiment_result(stale, "current_capability"))
        self.assertTrue(
            ComposerRunner._has_executed_experiment_result(stale, "old_capability"))

        self.assertFalse(ComposerRunner._has_executed_experiment_result({
            "capability_id": "current_capability",
            "study_id": "study-current",
            "results_package": {
                "capability_id": "old_capability",
                "id": "study-current",
                "metrics": [{"id": "metric", "value": 1.0}],
            },
        }, "current_capability"))
        self.assertFalse(ComposerRunner._has_executed_experiment_result({
            "capability_id": "current_capability",
            "study_id": "study-current",
            "results_package": {
                "id": "study-old",
                "metrics": [{"id": "metric", "value": 1.0}],
            },
        }, "current_capability"))
        self.assertTrue(ComposerRunner._has_executed_experiment_result({
            "capability_id": "cap-b",
            "study_id": "capability_b_study",
            "results_package": {
                "id": "capability_b_study",
                "metrics": [{"id": "metric", "value": 1.0}],
            },
        }, "cap-b"))
        self.assertFalse(ComposerRunner._has_executed_experiment_result({
            "capability_id": "cap-current",
            "results_package": {
                "capability_id": "cap-stale",
                "metrics": [{"id": "metric", "value": 1.0}],
            },
        }))
        self.assertFalse(ComposerRunner._has_executed_experiment_result({
            "results_package": {
                "metrics": [{"id": "metric", "value": 1.0}],
            },
        }, "capability-without-result-identity"))
        self.assertFalse(ComposerRunner._has_executed_experiment_result({
            "raw_results": {"raw_measurement": 0.0},
        }, "capability-without-result-identity"))
        self.assertTrue(ComposerRunner._has_executed_experiment_result({
            "raw_results": {"raw_measurement": 0.0},
        }))

    def test_prior_attempt_results_do_not_count_as_current_attempt_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prior_dir = root / "attempt-135"
            current_dir = root / "attempt-146"
            (prior_dir / "output").mkdir(parents=True)
            current_dir.mkdir()
            package_path = prior_dir / "output" / "results-package.json"
            package_path.write_text(json.dumps({
                "id": "capability-a",
                "capability_id": "capability-a",
                "metrics": [{"id": "ordering_score", "value": 0.42}],
            }))
            context = {
                "kind": "experiment", "project_dir": str(prior_dir),
                "capability_id": "capability-a", "study_id": "capability-a",
                "results_package": "output/results-package.json",
                "failure_recovery": {"requires_capability_repair": True},
                "metrics": [{"id": "ordering_score", "value": 0.42}],
            }
            self.assertTrue(ComposerRunner._has_executed_experiment_result(
                context, "capability-a"))
            self.assertFalse(ComposerRunner._has_executed_experiment_result(
                context, "capability-a", project_dir=current_dir))

            runner = object.__new__(ComposerRunner)
            runner.workflow = {"stages": [{
                "id": "experiment", "kind": "experiment",
                "experiment_capability_id": "capability-a",
            }]}
            runner.stage_records = {"experiment": {"project_dir": str(current_dir)}}
            stage = runner.workflow["stages"][0]
            self.assertTrue(runner._is_pre_execution_capability_failure(stage, context))

    def test_reused_experiment_result_is_bound_to_catalog_capability(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["stages"][2]["depends_on"] = ["survey"]
            prior_run = root / "prior-experiment-run.json"
            reused_output = root / "prior-results-package.json"
            prior_run.write_text(json.dumps({
                "status": "completed",
                "output_path": str(reused_output),
                "study_id": "study-b",
                "results_package": {
                    "id": "study-b",
                    "metrics": [{"id": "metric", "value": 1.0}],
                },
            }))
            workflow["stages"][2]["reuse_completed"] = True
            workflow["stages"][2]["reuse_output_path"] = str(prior_run)
            runner = ComposerRunner(workflow)
            try:
                descriptor_path = root / "catalog-capability.json"
                descriptor_path.write_text(json.dumps({
                    "experiment": {"id": "study-b"},
                }))
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "generated_capability": {
                        "capability_id": "cap-b",
                        "descriptor_path": str(descriptor_path),
                    },
                }
                stage = runner.workflow["stages"][2]
                reused = runner._execute_stage(stage)
                self.assertEqual(reused["capability_id"], "cap-b")
                self.assertEqual(reused["study_id"], "study-b")
                runner.context[stage["id"]] = reused
                runner._checkpoint("experiment:reused", force=True)

                stale_result = {
                    "capability_id": "cap-old",
                    "study_id": "study-b",
                    "results_package": {
                        "id": "study-b",
                        "metrics": [{"id": "metric", "value": 1.0}],
                    },
                }
                self.assertFalse(runner._bind_reused_experiment_capability(
                    stage, stale_result))

                stale_study = {
                    "capability_id": "cap-b",
                    "study_id": "study-old",
                    "results_package": {
                        "id": "study-old",
                        "metrics": [{"id": "metric", "value": 1.0}],
                    },
                }
                self.assertFalse(runner._bind_reused_experiment_capability(
                    stage, stale_study))

                missing_study = {
                    "capability_id": "cap-b",
                    "results_package": {
                        "metrics": [{"id": "metric", "value": 1.0}],
                    },
                }
                self.assertFalse(runner._bind_reused_experiment_capability(
                    stage, missing_study))
            finally:
                runner.close()
            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(
                    resumed.context["experiment"]["capability_id"], "cap-b")
                self.assertEqual(resumed.context["experiment"]["study_id"], "study-b")
            finally:
                resumed.close()

    def test_experiment_execution_references_and_empty_envelopes_are_not_measurements(self):
        metadata_only_contexts = [
            {"raw_results": {}},
            {"raw_results": {"observations": [{
                "replicate": 1,
                "process_returncode": 0,
                "timestamp": "2026-09-25T00:00:00Z",
            }]}},
            {"metrics": []},
            {"execution_refs": ["artifact:execution@1"]},
            {
                "execution_refs": ["artifact:execution@1"],
                "deterministic_validation_ref": "artifact:validation@1",
                "assessment_ref": "artifact:assessment@1",
                "model_review_refs": ["artifact:review@1"],
            },
            {"results_package": {
                "schema_version": "results-package-2",
                "id": "capability-a",
                "revision": 1,
                "metrics": [],
                "findings": [{"statement": "No result was measured."}],
                "assets": [{"path": "results/plot.png"}],
            }},
            {"results_package": {
                "id": "capability-a",
                "metrics": [{"id": "", "value": 0.5}],
            }},
            {"results_package": {
                "id": "capability-a",
                "metrics": [{"id": "bad metric", "value": 0.5}],
            }},
        ]
        for context in metadata_only_contexts:
            with self.subTest(context=context):
                self.assertFalse(
                    ComposerRunner._has_executed_experiment_result(
                        context, "capability-a"))

        self.assertTrue(ComposerRunner._has_executed_experiment_result({
            "study_id": "capability-a",
            "execution_refs": ["artifact:execution@1"],
            "raw_results": {"observations": [{"replicate": 1, "helicity": 0.0}]},
        }, "capability-a"))
        self.assertTrue(ComposerRunner._has_executed_experiment_result({
            "results_package": {
                "id": "capability-a",
                "metrics": [{"id": "helicity", "value": 0.0}],
            },
        }, "capability-a"))

    def test_capability_repair_panel_routes_upper_methods_roles_into_authoring_context(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "topic": {
                        "id": "frontier",
                        "title": "A falsifiable direction",
                        "domain": "computational physics",
                        "research_question": "Does mechanism A change response B?",
                        "comparison": "mechanism-off baseline",
                        "measurement": "finite transition radius",
                    },
                }
                stage = workflow["stages"][1]
                descriptor = {"model": {"base_url": "http://example.invalid", "model": "stub"}}
                prior = {
                    "kind": "experiment",
                    "status": "research_expansion_required",
                    "review_status": "scientific_assignment_blocked",
                    "error": "capability foundry did not admit a program: constant observations",
                    "specialist_reports": [{
                        "role_id": "analysis-reviewer",
                        "status": "failed",
                        "response": {"findings": ["the declared intervention cancels from the output"]},
                    }],
                }
                topic = runner.context["topic"]
                assignment = {
                    "active_agents": [
                        "methods.methodologist", "methods.statistical-reviewer",
                        "methods.reproducibility-reviewer", "methods.analysis-reviewer",
                    ],
                    "verifier_agent": "methods.adversarial-reviewer",
                    "plan_ref": "artifact:plan",
                }
                reports = [{
                    "role_id": "methodologist",
                    "assigned_role": "methods.methodologist",
                    "status": "succeeded",
                    "response": {
                        "decision": "repair",
                        "summary": "Replace the cancelling algebra with a state-evolution design.",
                        "findings": ["The intervention is absent from the simulated dynamics."],
                        "requested_actions": ["Integrate the declared state variables across the intervention grid."],
                    },
                    "usage": {"model_calls": 1},
                    "artifact_ref": "artifact:methodologist",
                }]
                bundle = {
                    "reports": reports, "by_role": {"methodologist": reports[0]},
                    "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 10},
                    "model_enabled": True,
                }
                verifier = {
                    "status": "succeeded",
                    "response": {
                        "decision": "hold",
                        "rationale": "The repair is required before authoring.",
                        "critical_findings": ["The old mechanism is not identifiable."],
                        "repair_scope": ["Add an independent recalculation from raw observations."],
                    },
                    "usage": {"model_calls": 1},
                    "artifact_ref": "artifact:adversary",
                }
                with patch.object(runner, "_latest_foundry_failure_projection", return_value={
                        "feedback": "constant observations", "last_attempt": {
                            "experiment_intent": {"hypothesis": "A changes B"},
                            "executor_source": "old executor",
                            "validator_source": "old validator",
                        }}), \
                        patch.object(runner.departments, "begin_stage", return_value=assignment) as begin, \
                        patch.object(runner, "_run_specialist_pool", return_value=bundle), \
                        patch.object(runner, "_publish_specialist_reports", side_effect=lambda _s, _a, b: b), \
                        patch.object(runner, "_run_specialist_verifier", return_value=verifier), \
                        patch.object(runner.departments, "finish_stage", return_value={
                            "chief_synthesis_ref": "artifact:chief",
                            "verifier_artifact_ref": "artifact:verdict",
                        }), \
                        patch.object(runner, "_checkpoint"):
                    panel = runner._run_capability_repair_panel(
                        stage, descriptor, topic, prior, prior["error"])
                self.assertEqual(panel["decision"], "repair")
                self.assertIn("intervention is absent", " ".join(panel["root_causes"]))
                self.assertIn("independent recalculation", " ".join(panel["required_changes"]))
                self.assertEqual(panel["ledger"]["verifier_artifact_ref"], "artifact:verdict")
                begin.assert_called_once()
                self.assertEqual(begin.call_args.args[1], "experiment")
            finally:
                runner.close()

    def test_pre_execution_repair_passes_panel_to_fresh_foundry_generation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.workflow["capability_foundry_config_path"] = "configured-by-test"
            descriptor = root / "generated.json"
            descriptor.write_text(json.dumps({
                "experiment": {
                    "id": "frontier-cycle-1", "revision": 1,
                    "domain": "computational physics",
                    "research_question": "Does mechanism A change response B?",
                    "execution": {}, "validation": {},
                }
            }))
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "topic": {
                    "id": "frontier", "domain": "computational physics",
                    "research_question": "Does mechanism A change response B?",
                },
            }
            runner.context["experiment"] = {
                "kind": "experiment", "status": "research_expansion_required",
                "review_status": "scientific_assignment_blocked",
                "error": "capability foundry did not admit a program: constant observations",
            }
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"experiment"}
            runner.active_research_requests = [{
                "id": "repair", "kind": "additional_experiment",
                "owner": "methods.validation", "objective": "Repair the executable",
                "why": "The first capability was rejected", "success_condition": "Independent recalculation",
                "evidence_needed": "Raw observations",
            }]
            panel = {
                "schema_version": "capability-repair-panel-1",
                "status": "completed",
                "input_sha256": "a" * 64,
                "decision": "repair", "root_causes": ["constant output"],
                "required_changes": ["integrate the state variables"],
                "reports": [{"status": "succeeded"}],
                "verifier": {"status": "succeeded"},
                "ledger": {"panel_stage_id": "experiment-repair-panel-1-1",
                           "verifier_artifact_ref": "artifact:verdict"},
            }
            def materialize(topic_result, **kwargs):
                self.assertEqual(kwargs["repair_context"]["decision"], "repair")
                topic_result["generated_capability"] = {
                    "capability_id": "frontier-cycle-1",
                    "descriptor_path": str(descriptor.resolve()),
                }
                return topic_result
            with patch.object(runner, "_run_capability_repair_panel", return_value=panel) as panel_call, \
                    patch.object(runner, "_materialize_topic_capability", side_effect=materialize) as materialize_call:
                result = runner._apply_topic_to_experiment_config(
                    workflow["stages"][1],
                    {"experiment": {"revision": 1, "literature_gate": {}},
                     "supplied_context": "base"},
                )
            panel_call.assert_called_once()
            materialize_call.assert_called_once()
            self.assertEqual(result["experiment"]["id"], "frontier-cycle-1")

            with patch.object(runner, "_run_capability_repair_panel", return_value=panel), \
                    patch.object(runner, "_materialize_topic_capability",
                                 side_effect=ValidationError("repaired program was rejected")):
                with self.assertRaises(ValidationError) as raised:
                    runner._apply_topic_to_experiment_config(
                        workflow["stages"][1],
                        {"experiment": {"revision": 1, "literature_gate": {}},
                         "supplied_context": "base"},
                    )
            self.assertTrue(raised.exception.capability_repair_panel_completed)
            runner.close()

    def test_capability_authoring_repair_projection_fits_context_without_losing_sources(self):
        source = "# executable repair source\n" + ("value = 1\n" * 1400)
        repair = {
            "schema_version": "capability-repair-panel-1",
            "input_sha256": "a" * 64,
            "packet": {
                "topic": {"id": "frontier", "domain": "computational physics",
                          "research_question": "Does mechanism A change response B?"},
                "failure": {"error": "invalid estimator", "failure_debt": {"finding": "bad"}},
                "failure_recovery": {
                    "failure_class": "scientific_hold", "recovery_mode": "repair_then_rerun",
                    "requires_capability_repair": True,
                    "repair_commands": [{"id": "repair", "instruction": "Change the mechanism."}],
                    "review_directives": [{"kind": "requested_actions", "text": "Run a control."}],
                },
                "program_snapshot": [{"path": "/tmp/executor.py", "sha256": "b" * 64,
                                      "size_bytes": len(source), "source": source,
                                      "source_truncated": False}],
                "prior_foundry_work": {
                    "status": "repairing", "attempts": 1,
                    "feedback": "adversarial review rejected the candidate",
                    "last_attempt": {"executor_source": source, "validator_source": source,
                                      "experiment_intent": {"id": "frontier", "revision": 1}},
                },
                "repair_contract": {"must_change": ["the mechanism"]},
            },
            "root_causes": ["The estimator is not identifiable."],
            "required_changes": ["Add a discriminating control."],
            "acceptance_checks": ["Independent recalculation."],
            "repair_commands": [{"id": "repair", "operation": "edit_program",
                                 "instruction": "Change the source."}],
            "verifier": {"decision": "repair", "critical_findings": ["The mechanism is invalid."]},
            "reports": [{"role_id": "methodologist", "status": "completed",
                         "summary": "Bounded repair required.",
                         "findings": ["The intervention is absent."],
                         "requested_actions": ["Add a control."]}],
        }
        projected = ComposerRunner._capability_authoring_repair_projection(repair)
        self.assertNotIn("packet", projected)
        self.assertEqual(projected["failed_program"]["executor_source"], source)
        brief = {
            "topic": {"id": "frontier", "domain": "computational physics",
                      "research_question": "Does mechanism A change response B?"},
            "capability_repair": projected,
            "required_properties": ["bounded reproducible experiment"],
        }
        prompt = json.dumps(candidate_prompt(
            json.dumps(brief, ensure_ascii=False, sort_keys=True),
            [("numpy", "2.5.2")], {"probe": True},
            required_intent={"domain": "computational physics",
                             "research_question": "Does mechanism A change response B?"},
            runtime_version="3.12"), ensure_ascii=False, sort_keys=True)
        self.assertLessEqual(
            estimate_input_tokens(SYSTEM, prompt), 56000,
            "program-repair authoring packet must fit the configured 64k route")

    def test_observed_experiment_repair_requires_panel_and_reserves_full_envelope(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            stage = workflow["stages"][1]
            stage["quota"] = {
                "max_model_calls": 14, "max_input_tokens": 100000,
                "max_output_tokens": 20000, "max_openalex_requests": 0,
            }
            runner = ComposerRunner(workflow)
            try:
                runner.workflow["capability_foundry_config_path"] = "configured-by-test"
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "topic": {
                        "id": "frontier", "domain": "computational physics",
                        "research_question": "Does mechanism A change response B?",
                    },
                    "generated_capability": {"capability_id": "capability-a"},
                }
                prior_attempt = root / "experiment" / "attempt-previous"
                current_attempt = root / "experiment" / "attempt-current"
                package_path = prior_attempt / "output" / "results-package" / "results-package.json"
                package_path.parent.mkdir(parents=True)
                package_path.write_text(json.dumps({
                    "id": "study-a", "capability_id": "capability-a",
                    "metrics": [{"id": "metric", "value": 1.0}],
                }))
                runner.context["experiment"] = {
                    "kind": "experiment", "status": "research_expansion_required",
                    "project_dir": str(prior_attempt), "capability_id": "capability-a",
                    "study_id": "study-a",
                    "results_package": "output/results-package/results-package.json",
                    "failure_debt": {"failure_class": "scientific_hold"},
                }
                # The next attempt has a fresh, empty workspace. Repair
                # admission must inspect the previous attempt's evidence,
                # not mistake this current namespace for the prior result.
                runner.stage_records["experiment"] = {"project_dir": str(current_attempt)}
                runner.continuation_cycles = 1
                runner.active_research_requests = [{
                    "id": "repair", "kind": "additional_experiment",
                    "owner": "methods.validation", "objective": "Add a control",
                    "why": "The result is not discriminating",
                    "success_condition": "The control separates the explanations",
                    "evidence_needed": "Raw observations and independent recalculation",
                }]
                self.assertTrue(runner._capability_repair_panel_required(stage))
                self.assertEqual(runner._foundry_model_call_budget(stage), 2)
            finally:
                runner.close()

    def test_foundry_budget_does_not_double_charge_carried_failure_reviews(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                stage = next(item for item in workflow["stages"]
                             if item["kind"] == "experiment")
                stage["quota"] = {"max_model_calls": 24}
                runner.workflow["capability_foundry_config_path"] = "configured-by-test"
                runner.continuation_cycles = 1
                runner.reopened_stage_ids.add(stage["id"])
                runner.active_research_requests = [{
                    "id": "repair", "kind": "additional_experiment",
                    "owner": "methods.validation", "objective": "Repair the candidate",
                }]
                runner.context[stage["id"]] = {
                    "status": "research_expansion_required",
                    "review_status": "scientific_assignment_blocked",
                    "error": "capability foundry did not admit the candidate",
                    "specialist_reports": [{"usage": {"model_calls": 6}}],
                }
                runner.stage_records[stage["id"]] = {
                    "attempts": [{
                        "cycle": 0,
                        "usage": {"model_calls": 6},
                    }],
                }

                self.assertEqual(runner._foundry_model_call_budget(stage), 12)
            finally:
                runner.close()

    def test_capability_repair_panel_fake_model_e2e_publishes_real_assignment_ledger(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "topic": {
                        "id": "frontier", "domain": "computational physics",
                        "research_question": "Does mechanism A change response B?",
                    },
                }
                stage = next(item for item in workflow["stages"] if item["id"] == "experiment")
                descriptor = {
                    "model": {
                        "protocol": "openai_compatible", "base_url": "http://fake/v1",
                        "model": "fake", "context_window_tokens": 20000,
                        "max_input_tokens": 16000, "max_output_tokens": 4000,
                        "timeout_seconds": 5,
                    },
                    "limits": {"concurrent_calls": 4},
                }
                prior = {
                    "kind": "experiment", "status": "research_expansion_required",
                    "review_status": "scientific_assignment_blocked",
                    "error": "capability foundry did not admit a program: constant observations",
                }

                class FakeModel:
                    def __init__(self, **_config):
                        pass

                    def complete(self, *, system, prompt):
                        if system.startswith("You are an independent adversarial verifier"):
                            payload = {
                                "decision": "hold",
                                "rationale": "A new executable is required.",
                                "critical_findings": ["The failed mechanism is not identifiable."],
                                "repair_scope": ["Change the state evolution and recalculate from raw observations."],
                            }
                        else:
                            role = json.loads(prompt)["assignment"]["assigned_role"]
                            payload = {
                                "decision": "repair",
                                "summary": f"{role} found a bounded repair.",
                                "findings": ["The intervention must enter the dynamics."],
                                "evidence_gaps": [],
                                "requested_actions": ["Use a materially different state-evolution design."],
                            }
                        return ModelResult(json.dumps(payload), "fake", {"model_calls": 1}, 0.0, "stop")

                with patch("scisaurus.runtime.specialists.ModelClient", FakeModel):
                    panel = runner._run_capability_repair_panel(
                        stage, descriptor, runner.context["topic"], prior, prior["error"])
                self.assertEqual(panel["status"], "completed")
                self.assertEqual(len(panel["reports"]), 4)
                self.assertEqual(panel["verifier"]["decision"], "hold")
                self.assertEqual(len(panel["ledger"]["active_agents"]), 4)
                rows = runner.control._conn.execute(
                    "SELECT state FROM tasks WHERE task_id LIKE ?",
                    (f"%{panel['ledger']['panel_stage_id']}%",),
                ).fetchall()
                self.assertEqual(len(rows), 5)
                self.assertTrue(all(row["state"] == "completed" for row in rows))
            finally:
                runner.close()

    def test_topic_history_is_append_only_and_rotates_recent_capability(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            history_path = root / "shared" / "topic-history.json"
            capability_a = root / "cap-a.json"
            capability_b = root / "cap-b.json"
            for capability in (capability_a, capability_b):
                capability.write_text(json.dumps({"schema_version": "experiment-capability-1",
                                                   "capability_id": capability.stem,
                                                   "experiment": {}}))
            workflow["experiment_catalog"] = [
                {"id": "cap_a", "config_path": str(capability_a.resolve())},
                {"id": "cap_b", "config_path": str(capability_b.resolve())},
            ]
            workflow["topic_history_path"] = str(history_path.resolve())
            runner = ComposerRunner(workflow)
            topic = {"id": "chosen_topic_a", "title": "Direction A", "domain": "science",
                     "research_question": "Does mechanism A change the measured outcome?"}
            runner._record_topic_history({"topic": {**topic, "experiment_capability_id": "cap_a"}})
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            self.assertEqual(resumed.topic_history["entries"][0]["topic_id"], "chosen_topic_a")
            self.assertEqual(resumed._effective_topic_exclusions()["capability_ids"], ["cap_a"])
            self.assertIn("chosen_topic_a", resumed._effective_topic_exclusions()["topic_ids"])
            resumed.close()

    def test_legacy_topic_history_migration_separates_scientific_rejections(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            history_path = root / "shared" / "topic-history.json"
            workflow["topic_history_path"] = str(history_path.resolve())
            history_path.parent.mkdir(parents=True)
            history_path.write_text(json.dumps({
                "schema_version": "topic-history-1",
                "scopes": {"legacy": {"entries": [
                    {"topic_id": "old-novelty", "rejection_type": "intake_validation",
                     "rejection_reason": "selected topic is too similar to a previously attempted direction"},
                    {"topic_id": "old-maturity", "rejection_type": "intake_validation",
                     "rejection_reason": "topic maturity review requires substantive refinement: the comparison is too thin"},
                    {"topic_id": "old-contract", "rejection_type": "intake_validation",
                     "rejection_reason": "topic candidate has an invalid shape (missing=['search_queries'], unexpected=[])"},
                ]}},
            }))

            runner = ComposerRunner(workflow)
            try:
                document = json.loads(history_path.read_text())
                entries = [entry for scope in document["scopes"].values()
                           for entry in scope["entries"]]
                by_id = {entry["topic_id"]: entry for entry in entries}
                self.assertEqual(by_id["old-novelty"]["rejection_type"], "novelty")
                self.assertEqual(by_id["old-maturity"]["rejection_type"], "maturity")
                self.assertNotIn("old-contract", by_id)
                self.assertNotIn(
                    "intake_validation",
                    {entry.get("rejection_type") for entry in entries})
                self.assertEqual(
                    {entry["topic_id"] for entry in runner.topic_history["entries"]},
                    {"old-novelty", "old-maturity"})
            finally:
                runner.close()

    def test_generated_slot_history_id_does_not_become_exact_exclusion(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            history_path = root / "shared" / "topic-history.json"
            workflow["topic_history_path"] = str(history_path.resolve())
            workflow["topic_exclusions"] = {
                "capability_ids": [], "topic_ids": ["explicit_topic_id"]}
            runner = ComposerRunner(workflow)
            runner._record_topic_history({"topic": {
                "id": "direction_qft_topology_scaling",
                "title": "A generated slot label",
                "domain": "quantum science",
                "research_question": "Does a changed observable distinguish two mechanisms?",
            }})
            exclusions = runner._effective_topic_exclusions()
            self.assertNotIn("direction_qft_topology_scaling", exclusions["topic_ids"])
            self.assertIn("explicit_topic_id", exclusions["topic_ids"])
            runner.close()

    def test_rejected_topic_history_persists_across_fresh_missions(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            history_path = root / "shared" / "topic-history.json"
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir(); second_root.mkdir()
            first_workflow = self._workflow(first_root)
            first_workflow["topic_history_path"] = str(history_path.resolve())
            first = ComposerRunner(first_workflow)
            first._record_topic_rejection_history([{
                "topic_id": "rejected_direction",
                "title": "Rejected direction",
                "domain": "ecology",
                "research_question": "Does dispersal change recovery after disturbance?",
                "rejection_type": "maturity",
            }])
            first.close()

            second_workflow = self._workflow(second_root)
            second_workflow["topic_history_path"] = str(history_path.resolve())
            second = ComposerRunner(second_workflow)
            self.assertEqual(
                [item["topic_id"] for item in second.topic_history["entries"]],
                ["rejected_direction"],
            )
            self.assertEqual(second.topic_history["entries"][0]["history_status"], "rejected")
            self.assertIn(
                "rejected_direction", second._effective_topic_exclusions()["topic_ids"])
            second.close()

    def test_explicit_topic_history_survives_objective_wording_changes(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            history_path = root / "shared-topic-history.json"
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir(); second_root.mkdir()
            first_workflow = self._workflow(first_root)
            first_workflow["topic_history_path"] = str(history_path.resolve())
            first = ComposerRunner(first_workflow)
            first._record_topic_history({"topic": {
                "id": "prior_direction", "title": "Prior direction",
                "domain": "ecology",
                "research_question": "Does dispersal change recovery after disturbance?",
            }})
            first.close()

            second_workflow = self._workflow(second_root)
            second_workflow["objective"] = "Explore a newly worded scientific frontier"
            second_workflow["topic_history_path"] = str(history_path.resolve())
            second = ComposerRunner(second_workflow)
            self.assertEqual(
                [item["topic_id"] for item in second.topic_history["entries"]],
                ["prior_direction"],
            )
            self.assertIn("prior_direction", second._effective_topic_exclusions()["topic_ids"])
            second.close()

    def test_default_family_history_survives_objective_wording_changes(self):
        with tempfile.TemporaryDirectory() as path:
            family = Path(path)
            first_root = family / "run-1"
            second_root = family / "run-2"
            first_root.mkdir(); second_root.mkdir()
            first_workflow = self._workflow(first_root)
            first = ComposerRunner(first_workflow)
            first._record_topic_history({"topic": {
                "id": "prior_default_direction", "title": "Prior default direction",
                "domain": "ecology",
                "research_question": "Does dispersal change recovery after disturbance?",
            }})
            first.close()

            second_workflow = self._workflow(second_root)
            second_workflow["objective"] = "Explore a reworded frontier under the same mission"
            second = ComposerRunner(second_workflow)
            self.assertEqual(first.topic_history_path, second.topic_history_path)
            self.assertEqual(
                [item["topic_id"] for item in second.topic_history["entries"]],
                ["prior_default_direction"],
            )
            second.close()

    def test_exploration_seed_is_random_once_and_persisted_for_resume(self):
        with tempfile.TemporaryDirectory() as path, patch(
                "scisaurus.runtime.composer.secrets.randbits", return_value=123456):
            workflow = self._workflow(Path(path))
            runner = ComposerRunner(workflow)
            self.assertEqual(runner.exploration_seed, 123456)
            self.assertEqual(runner._topic_sampling_seed(), runner._topic_sampling_seed())
            self.assertNotEqual(
                runner._topic_sampling_seed(attempt_number=1),
                runner._topic_sampling_seed(attempt_number=2),
            )
            runner._checkpoint("seed-persisted", force=True)
            progress = json.loads((Path(workflow["project_id"]) / "output" / "progress.json").read_text())
            self.assertEqual(progress["status"], "running")
            self.assertEqual(progress["exploration_seed"], 123456)
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)
            self.assertEqual(resumed.exploration_seed, 123456)
            resumed.close()

    def test_free_topic_selection_projects_question_and_queries_without_manual_bindings(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str((root / "topic").resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            (root / "topic").mkdir()
            # The workflow's topic descriptor is not needed for this projection
            # test; the context is the same packet a completed topic stage emits.
            runner = ComposerRunner(workflow)
            # Catalog-backed free-topic runs must not promote the broad
            # discovery sampler's records to evidence seeds for the selected
            # question.
            runner.workflow["experiment_catalog"] = [{"id": "capability"}]
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "topic": {"research_question": "Does mechanism change the measured outcome?",
                          "search_queries": ["mechanism comparison", "controlled experiment", "public data"],
                          "recent_papers": [{"work_id": "W123456789"}]},
            }
            config = {"survey": {"question": "placeholder", "seed_queries": ["old query"],
                                  "seed_work_ids": ["W999"]}}
            projected = runner._apply_topic_to_survey_config(workflow["stages"][1], config)
            self.assertEqual(projected["survey"]["question"], "Does mechanism change the measured outcome?")
            self.assertEqual(projected["survey"]["seed_queries"],
                             ["mechanism comparison", "controlled experiment", "public data"])
            self.assertEqual(projected["survey"]["seed_work_ids"], [])
            self.assertEqual(projected["survey"]["bibliography_fallback"], "disabled")
            runner.close()

    def test_free_topic_enables_auto_pdf_routes_without_overriding_exact_routes(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "topic": {"id": "direction-1",
                          "research_question": "Does this mechanism change the outcome?",
                          "search_queries": ["mechanism and outcome"], "recent_papers": []},
            }
            routes = [
                {"work_id": "W101", "title": "Paper 101", "url": "https://example.org/101",
                 "section_markers": ["Introduction"]},
                {"work_id": "W102", "title": "Paper 102", "url": "https://example.org/102",
                 "section_markers": ["Introduction"], "route_policy": "exact"},
            ]
            config = {"survey": {"question": "placeholder", "seed_queries": ["old query"],
                                  "seed_work_ids": [], "full_text_sources": routes}}
            projected = runner._apply_topic_to_survey_config(workflow["stages"][1], config)
            self.assertEqual(
                [item["route_policy"] for item in projected["survey"]["full_text_sources"]],
                ["auto", "exact"],
            )
            runner.close()

    def test_topic_query_projection_adds_exact_hyphenated_concept(self):
        queries = ComposerRunner._topic_search_queries({
            "title": "Contamination tail benefit of median-of-means",
            "research_question": "How does median-of-means behave under contamination?",
            "search_queries": [
                "median of means estimator contamination",
                "median of means finite sample comparison",
            ],
        })
        self.assertEqual(queries, [
            "median of means estimator contamination",
            "median of means finite sample comparison",
        ])

    def test_survey_activates_only_its_search_preflight_specialist(self):
        self.assertEqual(
            ComposerRunner._active_stage_role_ids({"kind": "survey"}),
            ["search-strategist"],
        )
        self.assertEqual(
            ComposerRunner._active_stage_role_ids(
                {"kind": "survey"},
                stage_context={
                    "review_status": "gap_assessment_resume",
                    "survey_current": True,
                    "assessment_current": False,
                    "resume_scope": "gap_assessment",
                },
            ),
            [],
        )
        self.assertEqual(
            ComposerRunner._active_stage_role_ids(
                {"kind": "survey"},
                stage_context={"review_status": "gap_assessment_resume",
                               "survey_current": False,
                               "assessment_current": False,
                               "resume_scope": "gap_assessment"},
            ),
            ["search-strategist"],
        )
        self.assertIsNone(
            ComposerRunner._active_stage_role_ids({"kind": "experiment"})
        )

    def test_free_topic_literature_hold_requests_question_refinement(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "topic": {"id": "direction_a", "title": "A direction",
                          "domain": "science",
                          "phenomenon": "mechanistic transition in system A",
                          "research_question": "Does mechanism A change the measured outcome?"},
            }
            gated = runner._gate_free_topic_survey({
                "status": "completed", "gap_state": "insufficient_evidence",
                "survey_ref": "survey-ref", "assessment_ref": "assessment-ref",
            })
            self.assertEqual(gated["status"], "research_expansion_required")
            self.assertEqual({item["kind"] for item in gated["research_expansion_requests"]},
                             {"literature_expansion"})
            self.assertEqual(gated["topic_admission"], "expand_literature_before_refine")
            # A second insufficient assessment after the scoped expansion is
            # the point at which the question is sent back for redesign.
            runner.context["survey"] = gated
            refined_hold = runner._gate_free_topic_survey({
                "status": "completed", "gap_state": "insufficient_evidence",
                "survey_ref": "survey-ref-2", "assessment_ref": "assessment-ref-2",
            })
            self.assertEqual({item["kind"] for item in refined_hold["research_expansion_requests"]},
                             {"literature_expansion", "topic_refinement"})
            self.assertEqual(refined_hold["topic_admission"], "refine_before_experiment")
            refinement_request = next(
                item for item in refined_hold["research_expansion_requests"]
                if item["kind"] == "topic_refinement")
            self.assertIn("parent phenomenon", refinement_request["objective"])
            self.assertIn("Do not replace the subject with an unrelated topic",
                          refinement_request["objective"])

            refuted_hold = runner._gate_free_topic_survey({
                "status": "completed", "gap_state": "refuted_by_prior_work",
                "survey_ref": "survey-ref-3", "assessment_ref": "assessment-ref-3",
            })
            refuted_request = next(
                item for item in refuted_hold["research_expansion_requests"]
                if item["kind"] == "topic_refinement")
            self.assertIn("Preserve the parent phenomenon", refuted_request["objective"])
            self.assertIn("does not by itself establish", refuted_request["why"])
            runner.active_research_requests = [
                {**item, "source_stage_id": "survey"}
                for item in refined_hold["research_expansion_requests"]
            ]
            runner.topic_history = {
                "entries": [{
                    "topic_id": "direction_a",
                    "title": "A rejected rate formulation",
                    "domain": "science",
                    "research_question": "Does mechanism A change the measured rate?",
                    "history_status": "rejected",
                    "rejection_type": "novelty",
                    "rejection_reason": "The measured comparison repeats prior attempts.",
                }],
                "capability_counts": {},
            }
            self.assertEqual(
                runner._topic_history_context()["entries"][0]["history_status"],
                "rejected",
            )
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"topic", "survey"}
            context = runner._topic_refinement_context(workflow["stages"][0])
            self.assertEqual(context["parent_topic_id"], "direction_a")
            self.assertEqual(
                context["rejected_directions"][0]["research_question"],
                "Does mechanism A change the measured rate?",
            )
            self.assertEqual(
                context["rejected_directions"][0]["rejection_reason"],
                "The measured comparison repeats prior attempts.",
            )
            self.assertEqual(context["mode"], "refinement")
            self.assertEqual(context["salvage_plan"]["mode"], "salvage")
            self.assertEqual(
                context["salvage_plan"]["active_branch"]["id"],
                "mechanism-observable",
            )
            # A completed salvage branch is carried in the immutable topic
            # lineage so the next continuation selects the next repair axis.
            runner.context["topic"]["topic_evolution"] = {
                "mode": "refinement",
                "salvage": {
                    "mode": "salvage",
                    "attempted_branch_ids": ["mechanism-observable"],
                },
            }
            next_context = runner._topic_refinement_context(workflow["stages"][0])
            self.assertEqual(
                next_context["salvage_plan"]["active_branch"]["id"],
                "comparison-baseline",
            )
            runner.context["topic"]["runtime_feasibility_revalidation"] = {
                "status": "required",
                "reason": "the restored topic exceeds the current execution boundary",
            }
            forced_context = runner._topic_refinement_context(workflow["stages"][0])
            self.assertEqual(forced_context["salvage_plan"]["mode"], "structural_pivot")
            self.assertTrue(forced_context["salvage_plan"]["forced"])
            self.assertIn("topic", runner._continuation_targets(
                runner.active_research_requests, {"topic": workflow["stages"][0],
                                                  "survey": workflow["stages"][1],
                                                  "experiment": workflow["stages"][2]}))
            runner.close()

    def test_eligible_survey_carries_provisional_topic_requirements_forward(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "admission_state": "provisional_for_survey",
                "maturity_open_requirements": ["Ground the mechanism."],
                "topic": {
                    "id": "direction_a", "title": "A direction",
                    "domain": "science",
                    "research_question": "Does mechanism A change the measured outcome?",
                },
            }
            gated = runner._gate_free_topic_survey({
                "status": "completed", "gap_state": "eligible_for_experiment",
                "survey_ref": "survey-ref", "assessment_ref": "assessment-ref",
            }, stage=workflow["stages"][1])
            self.assertEqual(
                gated["topic_admission"], "provisional_supported_for_experiment")
            self.assertEqual(
                gated["carried_maturity_requirements"], ["Ground the mechanism."])
            runner.close()

    def test_repeated_continuation_hold_pivots_until_the_deadline(self):
        """A repeated survey hold keeps changing strategy until the hard wall."""
        class FastClock:
            def __init__(self):
                self.value = 0.0

            def __call__(self):
                self.value += 0.05
                return self.value

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 5,
                "reuse_completed": False, "reuse_output_path": None,
            })
            for stage in workflow["stages"]:
                stage["deadline_seconds"] = 5
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["time_policy"] = {
                "first_result_seconds": 1, "target_seconds": 2,
                "hard_seconds": 5, "checkpoint_seconds": 1,
            }
            workflow["retry_policy"] = {"mode": "until_deadline", "backoff_seconds": 0}
            runner = ComposerRunner(workflow, clock=FastClock())
            calls = []

            def staged(stage, **kwargs):
                calls.append(stage["id"])
                result = {
                    "status": "completed",
                    "output_path": str(Path(stage["project_dir"]) / "result.json"),
                    "project_dir": stage["project_dir"], "stage_id": stage["id"],
                }
                if stage["id"] == "topic":
                    result["topic"] = {
                        "id": "direction_x", "title": "Direction X",
                        "domain": "science", "research_question": "Does X change Y?",
                    }
                elif stage["id"] == "survey":
                    result.update({"gap_state": "insufficient_evidence"})
                    result = runner._gate_free_topic_survey(result, stage=stage)
                return result

            runner._run_stage = staged
            result = runner.run()
            self.assertEqual(result["status"], "paused")
            self.assertGreaterEqual(result["continuation_cycles"], 2)
            self.assertGreaterEqual(len(calls), 5)
            self.assertEqual(calls[:5], ["topic", "survey", "survey", "topic", "survey"])
            self.assertTrue(any(item.get("action") == "continue_research"
                                for item in result["feedback"]))
            self.assertTrue(any(
                request.get("id", "").startswith("auto-")
                for item in result["feedback"]
                if item.get("action") == "continue_research"
                for request in item.get("research_requests", [])
                if isinstance(request, dict)))
            runner.close()

    def test_free_topic_selection_switches_between_pinned_experiment_capabilities(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            catalog = root / "capability.json"
            catalog.write_text(json.dumps({
                "schema_version": "experiment-capability-1",
                "capability_id": "cap_b",
                "experiment": {
                    "id": "capability_b_study",
                    "revision": 1,
                    "study_type": "methods_validation",
                    "domain": "robust statistics",
                    "research_question": "Does the robust estimator reduce tail error under contamination?",
                    "hypothesis": "The robust estimator reduces contaminated tail error.",
                    "method": "Run a frozen paired simulation.",
                    "parameters": {}, "seed": 1, "run_count": 1,
                    "stopping_rule": "Run the declared simulation once.",
                    "primary_outcomes": [], "limitations": [], "literature_gate": None,
                    "execution": {}, "validation": {}, "required_assets": [], "reviewers": [],
                    "stage_seconds": {}, "max_observations": 1, "max_asset_bytes": 1,
                },
            }))
            workflow["experiment_catalog"] = [{"id": "cap_b", "config_path": str(catalog.resolve())}]
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            unrelated_dir = root / "unrelated-topic"
            unrelated_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "unrelated_topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(unrelated_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            next(item for item in workflow["stages"]
                 if item["id"] == "survey")["depends_on"] = ["topic"]
            runner = ComposerRunner(workflow)
            template = json.loads(Path("config/experiment-capabilities/free_quadrature_peak.json").read_text())
            config = {"experiment": template["experiment"], "supplied_context": "base"}
            runner.context["unrelated_topic"] = {
                "kind": "topic_discovery",
                "admission_state": "mature",
                "topic": {
                    "experiment_capability_id": "cap_b",
                    "research_question": "This unrelated branch must never be bound.",
                },
            }
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "admission_state": "provisional_for_survey",
                "maturity_open_requirements": ["Separate the competing mechanism."],
                "topic": {
                    "experiment_capability_id": "cap_b",
                    "research_question": "Does the robust estimator reduce tail error under contamination?",
                },
            }
            experiment_stage = next(
                item for item in workflow["stages"] if item["id"] == "experiment")
            with self.assertRaisesRegex(ValidationError, "provisional topic cannot enter"):
                runner._apply_topic_to_experiment_config(experiment_stage, config)
            runner.context["survey"] = {
                "kind": "survey",
                "status": "completed",
                "gap_state": "eligible_for_experiment",
                "topic_admission": "provisional_supported_for_experiment",
                "carried_maturity_requirements": ["Separate the competing mechanism."],
            }
            selected = runner._apply_topic_to_experiment_config(experiment_stage, config)
            self.assertEqual(selected["experiment"]["id"], "capability_b_study")
            self.assertEqual(selected["experiment"]["research_question"],
                             "Does the robust estimator reduce tail error under contamination?")
            self.assertIn(
                "Separate the competing mechanism.", selected["supplied_context"])
            self.assertIn(
                "literature gap decision does not by itself resolve them",
                selected["supplied_context"])
            self.assertEqual(
                selected["experiment"]["limitations"],
                config["experiment"]["limitations"],
                "deferred maturity requirements must not mutate the generated program contract",
            )
            runner.close()

    def test_design_driven_capability_injects_the_proposed_design(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            design = {
                "family": "monte_carlo_estimator_comparison",
                "data_process": {"kind": "gaussian_contamination", "sample_size": 150,
                                 "contamination_rate": 0.08, "contamination_scale": 12.0},
                "estimators": ["mean", "median", "median_of_means"],
                "primary": "median_of_means", "baseline": "mean", "block_size": 5, "seed": 99,
            }
            catalog = root / "capability.json"
            catalog.write_text(json.dumps({
                "schema_version": "experiment-capability-1", "capability_id": "design_driven",
                "experiment": {
                    "id": "design_driven_study", "revision": 1, "study_type": "methods_validation",
                    "domain": "computational statistics",
                    "research_question": "Default question.", "hypothesis": "Pinned hypothesis.",
                    "method": "Run the pinned design-driven engine.", "parameters": {"design_driven": True},
                    "seed": 1, "run_count": 1, "stopping_rule": "Run the declared design once.",
                    "primary_outcomes": [], "limitations": [], "literature_gate": None,
                    "execution": {"input": {"design": {}}}, "validation": {}, "required_assets": [],
                    "reviewers": [], "stage_seconds": {}, "max_observations": 1, "max_asset_bytes": 1,
                },
            }))
            workflow["experiment_catalog"] = [{"id": "design_driven", "config_path": str(catalog.resolve())}]
            runner = ComposerRunner(workflow)
            config = {"experiment": {"revision": 1, "literature_gate": None}, "supplied_context": "base"}
            runner.context["topic"] = {"kind": "topic_discovery", "topic": {
                "experiment_capability_id": "design_driven",
                "research_question": "Does the pinned engine reproduce the declared contrast?",
                "domain": "robust statistics", "experiment_design": design}}
            selected = runner._apply_topic_to_experiment_config(workflow["stages"][1], config)
            experiment = selected["experiment"]
            self.assertEqual(experiment["execution"]["input"]["design"], design)
            self.assertEqual(experiment["seed"], 99)
            self.assertEqual(experiment["domain"], "robust statistics")
            self.assertEqual(experiment["research_question"],
                             "Does the pinned engine reproduce the declared contrast?")
            self.assertTrue(experiment["parameters"]["design_driven"])
            with self.assertRaisesRegex(ValidationError, "requires a proposed experiment_design"):
                runner.context["topic"]["topic"].pop("experiment_design")
                runner._apply_topic_to_experiment_config(workflow["stages"][1], config)
            runner.close()


    def test_catalog_paper_projection_replaces_stale_scientific_identity(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            capability = Path("config/experiment-capabilities/robust_mean.json").resolve()
            workflow["experiment_catalog"] = [{"id": "robust_mean", "config_path": str(capability)}]
            runner = ComposerRunner(workflow)
            runner.context["topic"] = {
                "kind": "topic_discovery",
                "topic": {"title": "Robust tail behavior under contamination",
                          "research_question": "Does median-of-means reduce contaminated tail error?"},
            }
            packet = {
                "writer_contract": {"section_order": [
                    {"id": "introduction", "unit_ids": ["intro_p1"]},
                    {"id": "question", "unit_ids": ["question_p1"]},
                    {"id": "methods", "unit_ids": ["methods_p1"]},
                    {"id": "results", "unit_ids": ["results_p1"]},
                    {"id": "interpretation", "unit_ids": ["interpretation_p1"]},
                    {"id": "limitations", "unit_ids": ["limitations_p1"]},
                    {"id": "conclusion", "unit_ids": ["conclusion_p1"]},
                ]},
                "results_package": {
                    "id": "robust_mean_pilot",
                    "question": "Does median-of-means reduce contaminated tail error?",
                    "procedures": [{"id": "robust_protocol", "description":
                                    "Run seeded samples comparing the empirical mean and median-of-means under replacement contamination."}],
                    "metrics": [{"id": "tail_reduction", "presentation":
                                  "median-of-means reduced the contaminated 95th percentile error by 48.2 percent"}],
                    "findings": [{"id": "robustness_gain", "statement":
                                  "Under contamination, median-of-means reduced the 95th percentile error by 48.2 percent."}],
                    "limitations": ["The result applies only to the declared univariate simulation."],
                    "assets": [],
                },
            }
            paper_config = {
                "schema_version": "paper-release-score-3", "paper_id": "dynamic_test",
                "title": "old quadrature title", "revision": 1, "document_type": "research_paper",
                "manuscript_project_dir": str(root / "manuscript"),
                "survey_project_dir": str(root / "survey"), "survey_ref": "x", "assessment_ref": "y",
                "results_package": "z", "evidence": [], "claims": [], "references": [],
                "authors": ["Sci-saurus"], "keywords": ["test"],
                "storyline": {"id": "old", "revision": 1, "thesis": "old",
                              "beats": [{"id": "old", "role": "question", "proposition": "old"}]},
                "depth_profile": {"min_words": 1, "min_references": 1, "min_full_text_references": 0,
                                  "min_sections": 1, "required_section_titles": ["Results"],
                                  "max_numeric_repetitions": 4, "max_caveat_repetitions": 3},
                "interpretation_file": str(root / "interpretation.json"), "figure_arguments": [],
                "surface_policy": {"allow_control_patterns": [], "max_numeric_repetitions": 4,
                                   "max_caveat_repetitions": 3},
            }
            (root / "interpretation.json").write_text("{}")
            projected_paper, projected_packet = runner._synchronize_catalog_paper_inputs(
                paper_config, packet, {"argument": {"primary_argument": {
                    "thesis": "Median-of-means trades a small clean-data penalty for lower contaminated tail error."
                }}})
            self.assertEqual(projected_paper["title"], "Robust tail behavior under contamination")
            self.assertNotIn("quadrature", json.dumps(projected_paper).casefold())
            self.assertIn("median-of-means", json.dumps(projected_packet).casefold())
            self.assertEqual(len(projected_paper["storyline"]["beats"]), 7)
            runner.close()

    def test_reopens_only_requested_stage_in_a_bounded_continuation_cycle(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["continuation_policy"] = {"max_cycles": 2}
            runner = ComposerRunner(workflow)
            calls = []

            def staged(stage, **kwargs):
                calls.append((stage["id"], stage["project_dir"]))
                output = Path(stage["project_dir"]) / "result.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                if stage["id"] == "experiment" and len([item for item in calls if item[0] == "experiment"]) == 1:
                    return {
                        "status": "research_expansion_required",
                        "output_path": str(output),
                        "project_dir": stage["project_dir"],
                        "research_expansion_requests": [{
                            "id": "run_control", "kind": "additional_experiment", "owner": "methods.validation",
                            "objective": "Run a discriminating control.",
                            "why": "The current result does not separate the explanations.",
                            "success_condition": "The control separates the explanations.",
                            "evidence_needed": "Validated raw output.",
                        }],
                    }
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = staged
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["continuation_cycles"], 1)
            self.assertEqual([item[0] for item in calls], ["survey", "experiment", "experiment"])
            self.assertIn("continuations/cycle-1", calls[-1][1])
            self.assertEqual(result["stages"]["experiment"]["attempt_count"], 2)
            self.assertTrue(any(item["action"] == "continue_research" for item in result["feedback"]))
            self.assertTrue(any(item["action"] == "activate_work_orders"
                                for item in result["department_activity"]))
            self.assertTrue(any(item["action"] == "resolve_work_orders" and item["outcome"] == "completed"
                                for item in result["department_activity"]))

    def test_research_hold_never_admits_downstream_before_continuation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            argument_dir = root / "argument"
            argument_dir.mkdir()
            argument_config = root / "argument.json"
            argument_config.write_text("{}")
            workflow["stages"].append({
                "id": "argument", "kind": "argument", "config_path": str(argument_config.resolve()),
                "project_dir": str(argument_dir.resolve()), "depends_on": ["experiment"],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            calls = []

            def staged(stage, **kwargs):
                calls.append(stage["id"])
                output = Path(stage["project_dir"]) / "result.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                if stage["id"] == "experiment" and calls.count("experiment") == 1:
                    return {
                        "status": "research_expansion_required", "output_path": str(output),
                        "project_dir": stage["project_dir"],
                        "research_expansion_requests": [{
                            "id": "run_control", "kind": "additional_experiment", "owner": "methods.validation",
                            "objective": "Run a discriminating control.",
                            "why": "The current result does not separate the explanations.",
                            "success_condition": "The control separates the explanations.",
                            "evidence_needed": "Validated raw output.",
                        }],
                    }
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = staged
            result = runner.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["survey", "experiment", "experiment", "argument"])
            self.assertEqual(result["continuation_cycles"], 1)
            self.assertEqual(result["stages"]["experiment"]["status"], "completed")

    def test_resume_hold_reopens_before_dependency_scheduler(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.context["experiment"] = {
                "stage_id": "experiment", "kind": "experiment", "status": "research_expansion_required",
                "research_expansion_requests": [{
                    "id": "run_control", "kind": "additional_experiment", "owner": "methods.validation",
                    "objective": "Run a discriminating control.", "why": "The result is ambiguous.",
                    "success_condition": "The control separates the explanations.",
                    "evidence_needed": "Validated raw output.",
                }],
            }
            runner.context["survey"] = {"stage_id": "survey", "kind": "survey", "status": "completed"}
            runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
            runner.stage_records["experiment"] = {"kind": "experiment", "status": "research_expansion_required"}
            runner._checkpoint("held", force=True)
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)
            self.assertEqual(resumed._continuation_policy()["mode"], "until_deadline")
            calls = []

            def recovered(stage, **kwargs):
                calls.append(stage["id"])
                output = Path(stage["project_dir"]) / "result.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            resumed._run_stage = recovered
            result = resumed.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(calls, ["experiment"])
            self.assertEqual(result["continuation_cycles"], 1)

    def test_continuation_interpretation_output_uses_a_new_cycle_namespace(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            original_output = root / "interpretation.json"
            input_path = root / "packet.json"
            model_path = root / "model.json"
            input_path.write_text(json.dumps({"results_package": {}}))
            model_path.write_text(json.dumps({}))
            original_output.write_text("old")
            stage = {
                "id": "interpretation", "kind": "interpretation",
                "config_path": str((root / "interpretation-config.json").resolve()),
                "project_dir": str((root / "interpretation").resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            stage["project_dir"] = str((root / "interpretation").resolve())
            Path(stage["project_dir"]).mkdir()
            Path(stage["config_path"]).write_text(json.dumps({
                "model_config_path": str(model_path.resolve()),
                "input_path": str(input_path.resolve()),
                "output_path": str(original_output.resolve()),
            }))
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"interpretation"}
            dispatch = runner._stage_for_cycle(stage)

            class FakeInterpretation:
                def __init__(self, *args, **kwargs):
                    pass

                def run(self, packet):
                    return {"research_question": "Does the measured rate change?", "result_patterns": [],
                            "competing_explanations": [], "discriminating_experiments": [],
                            "prioritization": {"primary_pattern_id": "none", "secondary_pattern_ids": [],
                                               "rationale": "No pattern was supplied."},
                            "conclusion": "No conclusion is available."}

            with patch("scisaurus.runtime.scientific_interpretation.ScientificInterpretationRunner",
                       FakeInterpretation):
                result = runner._run_stage(dispatch)
            self.assertIn("continuations/cycle-1", result["output_path"])
            self.assertTrue(Path(result["output_path"]).is_file())
            self.assertEqual(original_output.read_text(), "old")
            runner.close()

    def test_resume_preserves_the_persisted_hard_wall(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.started_epoch = 100.0
            runner.deadline_epoch = 110.0
            runner._checkpoint("paused", force=True)
            runner.close()
            with patch("scisaurus.runtime.composer.time.time", return_value=111.0):
                resumed = ComposerRunner(workflow, resume=True)
                self.assertEqual(resumed.deadline_epoch, 110.0)
                with self.assertRaisesRegex(ValidationError, "deadline"):
                    resumed._remaining()
                resumed.close()

    def test_hard_timeout_persists_a_concise_interim_report(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.deadline = runner.clock() - 1
            runner.deadline_epoch = time.time() - 1
            result = runner.run()
            report_path = root / "composer" / "output" / "interim_report.json"
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(Path(result["interim_report_path"]), report_path.resolve())
            self.assertTrue(report_path.is_file())
            report = json.loads(report_path.read_text())
            self.assertEqual(report["schema_version"], "composer-interim-report-1")
            self.assertEqual(report["stop_reason"], "hard_deadline")
            self.assertIn("resume_hint", report)
            self.assertEqual(report["completed_stage_ids"], [])
            self.assertEqual([row["id"] for row in report["stages"]], ["survey", "experiment"])
            report_path.unlink()
            fallback = read_interim_report(root / "composer")
            self.assertEqual(fallback["stop_reason"], "process_interrupted")
            self.assertEqual(fallback["deadline_seconds"], 30)
            self.assertEqual(fallback["pending_stage_ids"], ["survey", "experiment"])
            self.assertEqual([(row["id"], row["kind"]) for row in fallback["stages"]],
                             [("survey", "survey"), ("experiment", "experiment")])

    def test_resume_can_extend_the_same_deadline_and_persist_the_decision(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.started_epoch = time.time() - 31
            runner.started = runner.clock() - 31
            runner.deadline_epoch = time.time() - 1
            runner.deadline = runner.clock() - 1
            runner._checkpoint("paused", force=True)
            runner.close()

            resumed = ComposerRunner(workflow, resume=True, additional_seconds=60)
            self.assertGreater(resumed.deadline_epoch, time.time() + 50)
            self.assertEqual(len(resumed.deadline_extensions), 1)
            progress = json.loads((root / "composer" / "output" / "progress.json").read_text())
            self.assertEqual(len(progress["deadline_extensions"]), 1)
            self.assertGreater(resumed._remaining(), 50)
            resumed.close()

            resumed_again = ComposerRunner(workflow, resume=True)
            self.assertEqual(len(resumed_again.deadline_extensions), 1)
            self.assertGreater(resumed_again._remaining(), 50)
            resumed_again.close()

    def test_new_composer_run_cannot_apply_a_deadline_extension(self):
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaisesRegex(ValidationError, "only valid when resuming"):
                ComposerRunner(self._workflow(Path(path)), additional_seconds=30)

    def test_finished_live_ticker_cannot_overwrite_a_newer_stage_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            workflow["time_policy"]["checkpoint_seconds"] = 0.5
            runner = ComposerRunner(workflow)
            finish = runner._start_live_progress(workflow["stages"][0])
            stale_epoch = runner._live_progress_epoch
            finish()
            runner._checkpoint("next-stage", force=True)
            before = (root / "composer" / "output" / "progress.json").read_bytes()
            runner._live_progress("survey:running", epoch=stale_epoch)
            self.assertEqual((root / "composer" / "output" / "progress.json").read_bytes(), before)
            runner.close()

    def test_resume_selects_an_older_checkpoint_with_ahead_attempt_frontier(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            terminal = {
                "status": "paused",
                "stages": {
                    "survey": {"status": "retrying", "attempt_count": 51},
                    "experiment": {"status": "pending", "attempt_count": 0},
                },
            }

            def checkpoint(attempt_count):
                runner._publish(
                    f"command/composer/checkpoints/regression-{attempt_count}",
                    "progress_checkpoint",
                    {"workflow_id": workflow["id"], "status": "running", "stages": {
                        "survey": {"status": "retrying", "attempt_count": attempt_count},
                        "experiment": {"status": "pending", "attempt_count": 0},
                    }},
                    "command.composer",
                )

            # The stale checkpoint is newer, so a first-row-only lookup would
            # incorrectly discard the older checkpoint at the true frontier.
            checkpoint(53)
            checkpoint(51)
            selected = runner._latest_inflight_checkpoint(terminal)
            self.assertIsNotNone(selected)
            self.assertEqual(selected["stages"]["survey"]["attempt_count"], 53)
            self.assertTrue(ComposerRunner._checkpoint_advances_terminal_state(
                selected, terminal))
            runner.close()

    def test_routes_journal_editor_to_editor_in_chief(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            event = {
                "event_id": "paper-review-journal-editor",
                "kind": "review",
                "reviewer_id": "journal_editor",
                "stage": 6,
                "status": "needs_revision",
                "decision": "revise",
                "severity_counts": {"blocking": 0, "major": 2, "minor": 0},
                "finding_ids": ["journal_editor_reference_count"],
            }
            runner._record_internal_feedback(workflow["stages"][0], event)
            self.assertEqual(runner.feedback[0]["to"],
                             {"dept": "editorial", "agent": "editor-in-chief"})
            self.assertEqual(runner.feedback[0]["action"], "request_revision")
            runner.close()

    def test_routes_research_expansion_to_owning_departments(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            event = {
                "event_id": "paper-research-expansion",
                "kind": "research_gate",
                "reviewer_id": "journal_editor",
                "stage": 6,
                "status": "research_expansion_required",
                "decision": "research_expansion_required",
                "expansion_requests": [{
                    "id": "run_more", "kind": "additional_experiment", "owner": "methods.validation",
                    "objective": "Run a discriminating control.", "why": "The current data do not separate the hypotheses.",
                    "success_condition": "The control changes the decision.", "evidence_needed": "Validated raw output.",
                    "blocked_checks": ["figure_count"],
                }],
            }
            runner._record_internal_feedback(workflow["stages"][0], event)
            self.assertEqual(runner.feedback[0]["action"], "request_research_expansion")
            self.assertEqual(runner.feedback[0]["to"], {"dept": "methods", "agent": "chief"})
            runner.close()

    def test_custom_department_chief_receives_composer_handoff(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            organization = default_organization()
            for charter in organization["departments"]:
                if charter["id"] == "methods":
                    charter["chief"] = "methods-lead"
            workflow["organization"] = organization
            runner = ComposerRunner(workflow)
            runner._record_feedback(workflow["stages"][0], {
                "status": "completed", "output_path": str(root / "survey.json"),
            })
            self.assertEqual(runner.feedback[0]["to"], {"dept": "methods", "agent": "methods-lead"})
            runner.close()

    def test_live_progress_uses_plain_snapshot_outside_sqlite_thread(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.departments.snapshot = lambda: (_ for _ in ()).throw(
                AssertionError("background progress must not query SQLite"))
            error = []

            def tick():
                try:
                    runner._live_progress("survey:running")
                except Exception as exc:  # pragma: no cover - assertion below reports the failure
                    error.append(exc)

            thread = threading.Thread(target=tick)
            thread.start(); thread.join()
            self.assertEqual(error, [])
            self.assertEqual(json.loads((root / "composer" / "output" / "progress.json").read_text())["phase"],
                             "survey:running")
            runner.close()

    def test_resuming_running_stage_reconciles_task_attempt_before_retry(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            task = runner._stage_task(workflow["stages"][0])
            attempt_id = "interrupted-attempt"
            runner.tasks.start_attempt(task["task_id"], attempt_id, owner="command.composer", lease_ttl_seconds=30)
            runner.stage_records["survey"] = {
                "kind": "survey", "status": "running", "attempt_id": attempt_id,
                "attempt_number": 1, "project_dir": workflow["stages"][0]["project_dir"],
            }
            runner._checkpoint("survey:running", force=True)
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)

            def recovered(stage, **kwargs):
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            resumed._run_stage = recovered
            result = resumed.run()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["stages"]["survey"]["attempts"][0]["state"], "unknown")
            with sqlite3.connect(root / "composer" / "state" / "control.sqlite") as conn:
                self.assertEqual(conn.execute(
                    "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0],
                    "result_unknown")

    def test_resuming_after_terminal_task_crash_uses_recovery_generation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            task = runner._stage_task(workflow["stages"][0])
            attempt_id = "terminal-crash-attempt"
            runner.tasks.start_attempt(task["task_id"], attempt_id, owner="command.composer", lease_ttl_seconds=30)
            runner.tasks.finish_attempt(attempt_id, "succeeded")
            runner.tasks.transition(task["task_id"], "awaiting_review", "command.composer")
            runner.tasks.transition(task["task_id"], "completed", "command.composer")
            runner.stage_records["survey"] = {
                "kind": "survey", "status": "running", "attempt_id": attempt_id,
                "attempt_number": 1, "project_dir": workflow["stages"][0]["project_dir"],
                "task_id": task["task_id"],
            }
            runner._checkpoint("survey:running", force=True)
            runner.close()
            resumed = ComposerRunner(workflow, resume=True)

            def recovered(stage, **kwargs):
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            resumed._run_stage = recovered
            result = resumed.run()
            self.assertEqual(result["status"], "completed")
            recovery_task = result["stages"]["survey"]["task_id"]
            self.assertIn("recovery-", recovery_task)
            with sqlite3.connect(root / "composer" / "state" / "control.sqlite") as conn:
                self.assertEqual(conn.execute(
                    "SELECT state FROM tasks WHERE task_id=?", (recovery_task,)).fetchone()[0],
                    "completed")

    def test_resume_reconciles_expired_duplicate_stage_attempt_with_durable_result(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            stage = runner.workflow["stages"][0]
            task = runner._stage_task(stage)
            payload = {
                "stage_id": "survey", "kind": "survey", "attempt_number": 4,
                "project_dir": stage["project_dir"],
            }
            runner.tasks.start_attempt(
                task["task_id"], "survey-attempt-success", owner="command.composer",
                lease_ttl_seconds=30, payload=payload)
            runner.tasks.finish_attempt("survey-attempt-success", "succeeded")
            runner.tasks.start_attempt(
                task["task_id"], "survey-attempt-orphan", owner="command.composer",
                lease_ttl_seconds=0, payload=payload)
            runner.tasks.transition(task["task_id"], "awaiting_review", "command.composer")
            runner.tasks.transition(task["task_id"], "completed", "command.composer")
            output_dir = Path(stage["project_dir"]) / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "run.json").write_text(json.dumps({"status": "completed"}))
            runner._checkpoint("survey:completed", force=True)
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                attempt = resumed.tasks.get_attempt("survey-attempt-orphan")
                self.assertEqual(attempt["state"], "cancelled")
                self.assertEqual(
                    attempt["usage"]["accounting"],
                    "superseded_by_identical_durable_success",
                )
                self.assertTrue(any(
                    item.get("action") == "reconcile_orphaned_stage_attempts"
                    for item in resumed.department_activity
                ))
            finally:
                resumed.close()

    def test_resuming_after_stale_continuation_task_uses_recovery_generation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 1
                runner.reopened_stage_ids = {"survey"}
                first = runner._stage_task(workflow["stages"][0])
                runner.tasks.transition(
                    first["task_id"], "stale", "command.composer",
                    reason="watchdog fenced the prior continuation",
                )
                runner.stage_records["survey"] = {
                    "kind": "survey", "status": "retrying",
                    "attempt_id": "prior-attempt",
                }
                recovered = runner._stage_task(workflow["stages"][0])
                self.assertNotEqual(recovered["task_id"], first["task_id"])
                self.assertIn("recovery-", recovered["task_id"])
                self.assertEqual(recovered["state"], "queued")
                self.assertEqual(runner.tasks.get(first["task_id"])["state"], "stale")
            finally:
                runner.close()

    def test_resume_reconciles_unknown_aggregate_attempt_and_retries_under_fresh_task(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            stage = workflow["stages"][0]
            task = runner._stage_task(stage)
            attempt_id = "interrupted-aggregate-survey"
            runner.tasks.start_attempt(
                task["task_id"], attempt_id, owner="command.composer",
                lease_ttl_seconds=3600,
                payload={"stage_id": "survey", "kind": "survey", "attempt_number": 1},
            )
            runner.stage_records["survey"] = {
                "kind": "survey", "status": "retrying", "attempt_id": attempt_id,
                "attempt_number": 1, "attempt_count": 1,
                "project_dir": stage["project_dir"], "task_id": task["task_id"],
                "attempts": [],
            }
            runner._checkpoint("survey:interrupted", force=True)
            runner.close()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                def recovered(current_stage, **kwargs):
                    output = root / f"{current_stage['id']}-result.json"
                    output.write_text(json.dumps({"stage": current_stage["id"]}))
                    return {"status": "completed", "output_path": str(output),
                            "project_dir": current_stage["project_dir"],
                            "stage_id": current_stage["id"]}

                resumed._run_stage = recovered
                result = resumed.run()
                record = result["stages"]["survey"]
                self.assertEqual(record["attempts"][0]["state"], "unknown")
                self.assertEqual(record["attempts"][0]["attempt_id"], attempt_id)
                recovery_task = record["task_id"]
                self.assertNotEqual(recovery_task, task["task_id"])
                self.assertIn("recovery-", recovery_task)
                with sqlite3.connect(root / "composer" / "state" / "control.sqlite") as conn:
                    self.assertEqual(conn.execute(
                        "SELECT state FROM attempts WHERE attempt_id=?",
                        (attempt_id,),
                    ).fetchone()[0], "result_unknown")
                self.assertTrue(any(
                    item.get("action") == "reconcile_interrupted_stage_attempts"
                    for item in result["department_activity"]
                ))
                self.assertTrue(any(
                    item.get("action") == "recover_stage_task_with_unresolved_attempt"
                    for item in result["department_activity"]
                ))
            finally:
                resumed.close()

    def test_terminal_run_report_does_not_mask_a_more_advanced_live_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            task = runner._stage_task(workflow["stages"][0])
            attempt_id = "live-checkpoint-attempt"
            runner.tasks.start_attempt(attempt_id=attempt_id, task_id=task["task_id"],
                                       owner="command.composer", lease_ttl_seconds=30)
            runner.stage_records["survey"] = {
                "kind": "survey", "status": "completed", "attempt_id": attempt_id,
                "attempt_number": 1, "project_dir": workflow["stages"][0]["project_dir"],
                "task_id": task["task_id"],
            }
            runner.context["survey"] = {"status": "completed", "kind": "survey"}
            runner._checkpoint("survey:completed", force=True)
            runner.stage_records["survey"]["status"] = "blocked"
            runner.context = {}
            runner.status = "blocked"
            runner._finish()

            resumed = ComposerRunner(workflow, resume=True)
            self.assertEqual(resumed.stage_records["survey"]["status"], "completed")
            self.assertEqual(resumed.context["survey"]["status"], "completed")
            resumed.close()

    def test_deadline_prevents_continuation_activation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.deadline = runner.clock() - 1
            runner.active_research_requests = [{
                "id": "run-control", "kind": "additional_experiment", "owner": "methods.validation",
                "objective": "Run a discriminating control.", "why": "The result is ambiguous.",
                "success_condition": "The control separates the explanations.",
                "evidence_needed": "Validated raw output.", "source_stage_id": "experiment",
            }]
            with self.assertRaisesRegex(ValidationError, "deadline"):
                runner._begin_continuation(set(), {stage["id"]: stage for stage in workflow["stages"]})
            self.assertEqual(runner.continuation_cycles, 0)
            runner.close()

    def test_follow_up_requests_are_projected_into_fresh_interpretation_packet(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"interpretation"}
            runner.active_research_requests = [{
                "id": "interpret-more", "kind": "interpretation_expansion", "owner": "strategy.interpretation",
                "objective": "Compare the two plausible mechanisms.", "why": "The observed metrics diverge.",
                "success_condition": "The revised interpretation identifies a discriminating prediction.",
                "evidence_needed": "Metric-level comparison and sensitivity analysis.",
                "source_stage_id": "paper",
            }]
            packet = runner._project_continuation_requests({}, {"id": "interpretation"})
            self.assertEqual(packet["scientific_follow_up"][0]["objective"],
                             "Compare the two plausible mechanisms.")
            self.assertIn("follow_up_instruction", packet)
            runner.close()

    def test_continuation_materializes_interpretation_and_paper_packets(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 1
            runner.reopened_stage_ids = {"interpretation", "paper"}
            runner.active_research_requests = [{
                "id": "revise-meaning", "kind": "interpretation_expansion", "owner": "strategy.interpretation",
                "objective": "Compare the two plausible mechanisms.", "why": "The observed metrics diverge.",
                "success_condition": "The revised interpretation identifies a discriminating prediction.",
                "evidence_needed": "Metric-level comparison and sensitivity analysis.",
                "source_stage_id": "paper",
            }]
            source = root / "packet.json"
            source.write_text(json.dumps({"results_package": {}}))
            interpretation_stage = {"id": "interpretation", "kind": "interpretation",
                                    "project_dir": str((root / "interpretation").resolve())}
            interpretation_config = {"input_path": str(source.resolve())}
            runner._adapt_continuation_config(interpretation_stage, interpretation_config)
            self.assertNotEqual(interpretation_config["input_path"], str(source.resolve()))
            self.assertEqual(json.loads(Path(interpretation_config["input_path"]).read_text())[
                "scientific_follow_up"][0]["kind"], "interpretation_expansion")
            paper_stage = {"id": "paper", "kind": "paper",
                            "project_dir": str((root / "paper").resolve())}
            paper_config = {"packet_path": str(source.resolve())}
            runner._adapt_continuation_config(paper_stage, paper_config)
            self.assertNotEqual(paper_config["packet_path"], str(source.resolve()))
            self.assertTrue(Path(paper_config["packet_path"]).is_file())
            runner.close()

    def test_reopened_internal_events_get_cycle_specific_ids(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            runner.continuation_cycles = 2
            runner.reopened_stage_ids = {"survey"}
            event = {"event_id": "paper-research-admission", "kind": "research_gate",
                     "reviewer_id": "journal_editor", "status": "accepted", "decision": "proceed"}
            runner._record_internal_feedback(workflow["stages"][0], event)
            runner._record_internal_feedback(workflow["stages"][0], event)
            self.assertEqual(len(runner.feedback), 1)
            self.assertEqual(runner.feedback[0]["event_id"], "cycle-2-paper-research-admission")
            runner.close()

    def test_reuses_only_an_explicit_accepted_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            checkpoint = root / "accepted.json"
            checkpoint.write_text(json.dumps({"status": "accepted", "value": 7}))
            workflow["stages"][0]["reuse_completed"] = True
            workflow["stages"][0]["reuse_output_path"] = str(checkpoint.resolve())
            runner = ComposerRunner(workflow)
            reused = runner._run_stage(workflow["stages"][0])
            self.assertEqual(reused["value"], 7)
            runner.context["survey"] = reused
            runner.stage_records["survey"] = {"kind": "survey", "status": "completed"}
            # The second stage keeps its conventional output/run.json path;
            # it has no such file and is dispatched normally.
            calls = []

            def fake_stage(stage, **_kwargs):
                calls.append(stage["id"])
                output = root / f"{stage['id']}-result.json"
                output.write_text(json.dumps({"stage": stage["id"]}))
                return {"status": "completed", "output_path": str(output),
                        "project_dir": stage["project_dir"], "stage_id": stage["id"]}

            runner._run_stage = fake_stage
            with patch("scisaurus.runtime.specialists.ModelClient",
                       _ComposerTestSpecialistClient):
                result = runner.run()
            self.assertEqual(calls, ["experiment"])

    def test_reopened_topic_cycle_does_not_reuse_blocked_stage_cache(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            topic_stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str((root / "stage.json").resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"].insert(0, topic_stage)
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 1
                runner.reopened_stage_ids = {"topic"}
                runner.context["topic"] = {
                    "kind": "topic_discovery",
                    "review_status": "topic_budget_exhausted",
                }
                calls = []

                def blocked(stage, **kwargs):
                    calls.append("blocked")
                    raise ModelWorkBlocked("the previous topic envelope was exhausted")

                with patch.object(runner, "_execute_stage", side_effect=blocked):
                    with self.assertRaises(ModelWorkBlocked):
                        runner._run_stage(topic_stage)

                output = topic_dir / "output" / "topic.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"status": "completed"}))

                def recovered(stage, **kwargs):
                    calls.append("recovered")
                    return {
                        "status": "completed", "output_path": str(output.resolve()),
                    }

                with patch.object(runner, "_execute_stage", side_effect=recovered):
                    result = runner._run_stage(topic_stage)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(calls, ["blocked", "recovered"])
            finally:
                runner.close()

    def test_reopened_survey_cycle_does_not_reuse_blocked_stage_cache(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                runner.continuation_cycles = 1
                runner.reopened_stage_ids = {"survey"}
                runner.context["survey"] = {
                    "kind": "survey", "status": "research_expansion_required",
                    "review_status": "survey_handoff_incomplete",
                    "handoff_repair_required": True,
                }
                calls = []

                def blocked(stage, **kwargs):
                    calls.append("blocked")
                    raise ModelWorkBlocked("gap-assessment evidence contract failed")

                with patch.object(runner, "_execute_stage", side_effect=blocked):
                    with self.assertRaises(ModelWorkBlocked):
                        runner._run_stage(workflow["stages"][0])

                output = root / "survey" / "output" / "run.json"
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({"status": "completed"}))

                def recovered(stage, **kwargs):
                    calls.append("recovered")
                    return {
                        "status": "completed", "output_path": str(output.resolve()),
                    }

                with patch.object(runner, "_execute_stage", side_effect=recovered):
                    result = runner._run_stage(workflow["stages"][0])
                self.assertEqual(result["status"], "completed")
                self.assertEqual(calls, ["blocked", "recovered"])
            finally:
                runner.close()

    def test_free_topic_checkpoint_reuse_is_opt_in(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text(json.dumps({"schema_version": "topic-discovery-config-1"}))
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": True,
                "reuse_output_path": str((root / "accepted-topic.json").resolve()),
            })
            checkpoint = root / "accepted-topic.json"
            checkpoint.write_text(json.dumps({"status": "accepted", "topic": {"research_question": "old"}}))
            runner = ComposerRunner(workflow)
            runner._remaining = lambda: 10.0
            with patch("scisaurus.runtime.topic_discovery.validate_topic_stage_config") as validate, \
                    patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as topic_runner:
                validate.return_value = {
                    "model_config_path": str((root / "model.json").resolve()),
                    "output_path": str((root / "topic-output.json").resolve()),
                    "candidate_count": 3, "max_attempts": 1, "schema_version": "topic-discovery-config-1",
                }
                (root / "model.json").write_text("{}")
                topic_runner.return_value.run.return_value = {
                    "status": "completed", "topic": {"research_question": "new"}}
                result = runner._run_stage(workflow["stages"][0])
            self.assertEqual(result["topic"]["research_question"], "new")
            runner.close()

    def test_topic_stage_materializes_research_program_artifact(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text(json.dumps({"schema_version": "topic-discovery-config-1"}))
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery", "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [], "estimate_seconds": 1,
                "bindings": [], "deadline_seconds": 10, "reuse_completed": False,
                "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            runner._remaining = lambda: 10.0
            output_path = root / "topic-output.json"
            model_path = root / "model.json"
            model_path.write_text("{}")
            result = {**topic_package(), "status": "completed",
                      "topic": next(item for item in topic_package()["candidates"]
                                     if item["id"] == "branch_1")}
            with patch("scisaurus.runtime.topic_discovery.validate_topic_stage_config") as validate, \
                    patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as topic_runner:
                validate.return_value = {
                    "model_config_path": str(model_path.resolve()),
                    "output_path": str(output_path.resolve()),
                    "candidate_count": 3, "max_attempts": 1,
                    "schema_version": "topic-discovery-config-1",
                }
                topic_runner.return_value.run.return_value = result
                context = runner._run_stage(workflow["stages"][0])
            program_path = Path(context["research_program_path"])
            self.assertTrue(program_path.is_file())
            program = json.loads(program_path.read_text())
            self.assertEqual(program["schema_version"], "research-program-1")
            self.assertEqual(program["selected_id"], "branch_1")
            self.assertEqual(len(program["branches"]), 3)
            self.assertEqual(json.loads(output_path.read_text())["research_program"], program)
            runner.context["topic"] = {**context, "kind": "topic_discovery"}
            packet = {}
            runner._attach_topic_program(packet)
            self.assertEqual(packet["research_program"], program)
            runner.close()

    def test_topic_response_contract_repair_retains_the_admitted_incumbent(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_config = root / "topic.json"
            topic_config.write_text(json.dumps({"schema_version": "topic-discovery-config-1"}))
            topic_dir = root / "topic"
            topic_dir.mkdir()
            stage = {
                "id": "topic", "kind": "topic_discovery",
                "config_path": str(topic_config.resolve()),
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            }
            workflow["stages"].insert(0, stage)
            runner = ComposerRunner(workflow)
            runner._remaining = lambda: 10.0
            model_path = root / "model.json"
            model_path.write_text("{}")
            output_path = root / "topic-output.json"
            package = topic_package()
            selected = next(item for item in package["candidates"]
                            if item["id"] == package["selected_id"])
            incumbent = {
                **package, "status": "completed", "topic": selected,
                "usage": {"model_calls": 11, "input_tokens": 1200,
                          "output_tokens": 250, "openalex_requests": 4},
            }
            runner.context["topic"] = incumbent
            refinement = {
                "mode": "response_contract_repair",
                "parent_topic_id": selected["id"],
                "parent_topic": selected,
                "response_contract_repair": {
                    "validation_error": "The returned package contained unknown keys.",
                },
            }
            with patch("scisaurus.runtime.topic_discovery.validate_topic_stage_config") as validate, \
                    patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as topic_runner, \
                    patch.object(runner, "_topic_refinement_context", return_value=refinement):
                validate.return_value = {
                    "model_config_path": str(model_path.resolve()),
                    "output_path": str(output_path.resolve()),
                    "candidate_count": 3, "max_attempts": 1,
                    "schema_version": "topic-discovery-config-1",
                }
                recovered = runner._execute_stage(stage, specialist_reports=[])

            topic_runner.return_value.run.assert_not_called()
            self.assertEqual(recovered["topic"]["research_question"],
                             selected["research_question"])
            self.assertEqual(recovered["selected_id"], selected["id"])
            self.assertEqual(recovered["usage"]["model_calls"], 0)
            self.assertEqual(
                recovered["response_contract_recovery"]["incumbent_usage"]["model_calls"],
                11)
            self.assertEqual(
                recovered["response_contract_recovery"]["mode"],
                "retained_admitted_incumbent")
            self.assertTrue(Path(recovered["research_program_path"]).is_file())
            runner.close()

    def test_topic_refinement_cannot_change_the_admitted_phenomenon(self):
        parent = {
            "id": "direction_3",
            "phenomenon": "Shear thickening onset in confined cornstarch suspensions",
            "research_question": (
                "In confined cornstarch suspensions, does the onset-detection operator "
                "change the measured thickening threshold?"
            ),
        }
        candidate = {
            "id": "direction_4",
            "phenomenon": (
                "Hyper-entanglement scaling in multi-photon states generated by SPDC"
            ),
            "research_question": (
                "In hyper-entanglement scaling generated by SPDC, does the visibility "
                "curvature change with gain?"
            ),
        }
        result = {
            "topic": candidate,
            "topic_evolution": {
                "mode": "refinement", "parent_topic_id": parent["id"],
            },
            "candidate_attempt_trace": [{"attempt": 1, "selected_topic": candidate}],
        }
        refinement = {
            "mode": "refinement", "parent_topic_id": parent["id"],
            "parent_topic": parent, "salvage_plan": {"mode": "salvage"},
        }

        with self.assertRaises(ValidationError) as raised:
            ComposerRunner._validate_refined_topic_result(result, refinement)

        self.assertIn("must preserve the parent's phenomenon", str(raised.exception))
        self.assertTrue(raised.exception.topic_intake_recoverable)
        self.assertEqual(raised.exception.topic_retry_reason, "parent_identity_violation")
        self.assertEqual(
            raised.exception.rejected_topic_history[0]["topic_id"], "direction_4")

    def test_resume_restores_last_valid_topic_and_preserves_attempt_accounting(self):
        with tempfile.TemporaryDirectory() as path:
            runner = ComposerRunner(self._workflow(Path(path)))
            parent = {
                "id": "direction_3",
                "phenomenon": "Shear thickening onset in confined cornstarch suspensions",
                "research_question": (
                    "In confined cornstarch suspensions, does the onset-detection operator "
                    "change the measured thickening threshold?"
                ),
            }
            off_lineage = {
                "id": "direction_4",
                "phenomenon": "Hyper-entanglement scaling in multi-photon SPDC states",
                "research_question": (
                    "In hyper-entanglement scaling generated by SPDC, does visibility "
                    "curvature change with gain?"
                ),
            }
            older = {
                "workflow_id": "test-workflow", "status": "blocked",
                "state_revision": 10, "remaining_seconds": 100.0,
                "continuation_cycles": 12,
                "usage": {"model_calls": 40, "input_tokens": 4000},
                "context": {"topic": {
                    "kind": "topic_discovery", "topic": parent,
                    "topic_evolution": {"cycle": 12, "mode": "refinement"},
                }},
                "stages": {
                    "topic": {
                        "status": "blocked", "attempt_count": 1,
                        "attempts": [{
                            "attempt_id": "topic-attempt-1", "attempt_number": 1,
                            "state": "succeeded", "project_dir": "/safe/topic-1",
                        }],
                    },
                    "experiment": {"status": "retrying", "attempt_count": 2,
                                    "attempts": [], "usage": {"model_calls": 8}},
                },
            }
            newer = {
                **older, "status": "running", "state_revision": 14,
                "remaining_seconds": 80.0, "continuation_cycles": 16,
                "usage": {"model_calls": 75, "input_tokens": 9000},
                "context": {"topic": {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": off_lineage,
                    "topic_evolution": {
                        "cycle": 16, "mode": "refinement",
                        "parent_topic_id": "direction_3",
                    },
                }},
                "stages": {
                    "topic": {
                        "status": "completed", "attempt_count": 2,
                        "attempts": [
                            older["stages"]["topic"]["attempts"][0],
                            {"attempt_id": "topic-attempt-2", "attempt_number": 2,
                             "state": "succeeded", "project_dir": "/bad/topic-2"},
                        ],
                    },
                    "experiment": {
                        "status": "running", "attempt_count": 3,
                        "attempt_id": "active-experiment-attempt", "task_id": "active-task",
                        "attempt_number": 3, "project_dir": "/bad/experiment-3",
                        "attempts": [], "usage": {"model_calls": 20},
                    },
                },
            }

            try:
                reason = runner._unjustified_topic_refinement_error(
                    newer, older, "topic")
                self.assertIn("must preserve the parent's phenomenon", reason)
                restored, audit = runner._restore_topic_lineage_checkpoint(
                    newer, older, "topic", reason)

                self.assertEqual(
                    restored["context"]["topic"]["topic"]["id"], "direction_3")
                self.assertEqual(restored["usage"]["model_calls"], 75)
                self.assertEqual(restored["continuation_cycles"], 16)
                self.assertEqual(restored["remaining_seconds"], 80.0)
                self.assertEqual(restored["stages"]["topic"]["attempt_count"], 2)
                self.assertEqual(
                    restored["stages"]["topic"]["attempts"][-1]["state"],
                    "lineage_quarantined",
                )
                self.assertEqual(
                    restored["stages"]["experiment"]["attempt_id"],
                    "active-experiment-attempt",
                )
                self.assertEqual(
                    audit["action"], "reject_unjustified_topic_refinement_checkpoint")
                self.assertEqual(audit["rejected_topic_id"], "direction_4")
            finally:
                runner.close()

    def test_resume_selects_valid_run_head_over_off_lineage_live_checkpoint(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            topic_dir = root / "topic"
            topic_dir.mkdir()
            workflow["stages"].insert(0, {
                "id": "topic", "kind": "topic_discovery",
                "config_path": workflow["stages"][0]["config_path"],
                "project_dir": str(topic_dir.resolve()), "depends_on": [],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            workflow["stages"][1]["depends_on"] = ["topic"]
            workflow["completion"]["required_stage_ids"] = [
                "topic", "survey", "experiment",
            ]
            parent = {
                "id": "direction_3",
                "phenomenon": "Shear thickening onset in confined cornstarch suspensions",
                "research_question": (
                    "In confined cornstarch suspensions, does the onset-detection operator "
                    "change the measured thickening threshold?"
                ),
            }
            replacement = {
                "id": "direction_4",
                "phenomenon": "Hyper-entanglement scaling in multi-photon SPDC states",
                "research_question": (
                    "In hyper-entanglement scaling generated by SPDC, does visibility "
                    "curvature change with gain?"
                ),
            }
            older = {
                "schema_version": "composer-checkpoint-1",
                "workflow_id": workflow["id"], "run_id": "run-1",
                "status": "blocked", "state_revision": 10,
                "remaining_seconds": 100.0, "continuation_cycles": 12,
                "context": {"topic": {
                    "kind": "topic_discovery", "status": "blocked",
                    "topic": parent,
                    "topic_evolution": {"cycle": 12, "mode": "refinement"},
                }},
                "stages": {
                    "topic": {"status": "blocked", "attempt_count": 1,
                              "attempts": []},
                    "experiment": {"status": "retrying", "attempt_count": 2,
                                   "attempts": []},
                },
                "usage": {"model_calls": 40, "input_tokens": 4000},
                "foundry_usage": {}, "feedback": [], "blockers": [],
            }
            newer = deepcopy(older)
            newer.update({
                "status": "running", "state_revision": 14,
                "remaining_seconds": 80.0, "continuation_cycles": 16,
                "usage": {"model_calls": 75, "input_tokens": 9000},
                "context": {"topic": {
                    "kind": "topic_discovery", "status": "completed",
                    "topic": replacement,
                    "topic_evolution": {
                        "cycle": 16, "mode": "refinement",
                        "parent_topic_id": "direction_3",
                    },
                }},
                "stages": {
                    "topic": {"status": "completed", "attempt_count": 2,
                              "attempts": [{
                                  "attempt_id": "off-lineage-topic-attempt",
                                  "attempt_number": 2, "state": "succeeded",
                                  "project_dir": "/preserved/topic-attempt-2",
                              }]},
                    "experiment": {
                        "status": "running", "attempt_count": 3,
                        "attempt_id": "active-experiment-attempt",
                        "attempt_number": 3, "task_id": "active-experiment-task",
                        "project_dir": "/preserved/experiment-attempt-3",
                        "attempts": [], "usage": {"model_calls": 20},
                    },
                },
            })

            seed = ComposerRunner(workflow)
            seed._publish("command/composer/run", "report", older, "command.composer")
            seed._publish(
                "command/composer/checkpoints/1", "progress_checkpoint", newer,
                "command.composer")
            paused = deepcopy(newer)
            paused.update({"status": "paused", "phase": "paused"})
            progress_path = seed.root / "output" / "progress.json"
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            progress_path.write_bytes(canonical_bytes(paused))
            seed.close()

            resumed = ComposerRunner(workflow, resume=True)
            try:
                self.assertEqual(
                    resumed.context["topic"]["topic"]["id"], "direction_3")
                self.assertEqual(resumed.usage["model_calls"], 75)
                self.assertEqual(resumed.stage_records["experiment"]["attempt_id"],
                                 "active-experiment-attempt")
                self.assertEqual(
                    resumed.stage_records["topic"]["attempts"][-1]["state"],
                    "lineage_quarantined",
                )
                self.assertEqual(
                    resumed._restored_topic_lineage_reconciliation["action"],
                    "reject_unjustified_topic_refinement_checkpoint",
                )
            finally:
                resumed.close()

    def test_interrupted_topic_review_reuses_verified_producer_artifact(self):
        from scisaurus.runtime.research_program import build_research_program

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            try:
                selected = next(item for item in topic_package()["candidates"]
                                if item["id"] == "branch_1")
                result = {
                    **topic_package(),
                    "status": "completed",
                    "admission_state": "provisional_for_survey",
                    "topic": selected,
                    "topic_evolution": {"cycle": 4},
                    "usage": {"model_calls": 5, "input_tokens": 500,
                              "output_tokens": 80, "openalex_requests": 3},
                }
                result["research_program"] = build_research_program(result)
                output = root / "prior-attempt" / "topic-discovery.json"
                output.parent.mkdir(parents=True)
                output.write_text(json.dumps(result))
                (output.parent / "research-program.json").write_text(
                    json.dumps(result["research_program"]))
                stage = {"id": "topic", "kind": "topic_discovery",
                         "project_dir": str((root / "next-attempt").resolve()),
                         "deadline_seconds": 20}
                record = {"status": "retrying", "project_dir": str(output.parent.resolve()),
                          "attempt_number": 9}

                retained = ComposerRunner._recover_interrupted_topic_result(stage, record)
                self.assertIsNotNone(retained)
                stage["_resume_topic_result"] = retained
                runner._remaining = lambda: 10.0
                with patch("scisaurus.runtime.topic_discovery.TopicDiscoveryRunner") as producer:
                    recovered = runner._execute_stage(stage)
                producer.assert_not_called()
                self.assertEqual(recovered["selected_id"], "branch_1")
                self.assertEqual(recovered["producer_recovery"]["producer_calls_replayed"], 0)
                self.assertEqual(recovered["producer_recovery"]["source_usage"], result["usage"])
                self.assertEqual(recovered["usage"], result["usage"])
                self.assertEqual(recovered["output_path"], str(output.resolve()))
                self.assertEqual(
                    recovered["research_program_path"],
                    str((output.parent / "research-program.json").resolve()),
                )
                original_context = {
                    **result,
                    "stage_id": "topic", "kind": "topic_discovery",
                    "project_dir": str(output.parent.resolve()),
                    "output_path": str(output.resolve()),
                    "research_program_path": str(
                        (output.parent / "research-program.json").resolve()),
                }
                with patch.object(runner, "_specialist_model_config",
                                  return_value={"model": "fixture"}), \
                        patch.object(runner, "_runtime_context",
                                     return_value={"stable": True}):
                    self.assertEqual(
                        runner._specialist_stage_packet(stage, {}, stage_result=recovered),
                        runner._specialist_stage_packet(
                            stage, {}, stage_result=original_context),
                    )
                recovered["usage"]["model_calls"] += 1
                current_usage = runner._current_attempt_usage(recovered)
                self.assertEqual(current_usage["model_calls"], 1)
                self.assertEqual(current_usage["input_tokens"], 0)
                self.assertEqual(current_usage["openalex_requests"], 0)
                self.assertEqual(recovered["usage"]["model_calls"], 6)
            finally:
                runner.close()

    def test_journal_consumer_attaches_default_experiment_quality_contract(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            paper_config = root / "paper-config.json"
            paper_config.write_text(json.dumps({
                "schema_version": "paper-release-score-3", "document_type": "research_paper",
                "depth_profile": {"min_figures": 3},
            }))
            paper_dir = root / "paper"
            paper_dir.mkdir()
            workflow["stages"].append({
                "id": "paper", "kind": "paper", "config_path": str(paper_config.resolve()),
                "project_dir": str(paper_dir.resolve()), "depends_on": ["experiment"],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            config = {"experiment": {"id": "study"}}
            result = runner._ensure_journal_quality_contract(workflow["stages"][1], config)
            self.assertEqual(result["experiment"]["quality_contract"]["minimum_figures"], 3)
            runner.close()

    def test_journal_profile_resolves_nested_paper_descriptor(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            nested = root / "paper-config.json"
            nested.write_text(json.dumps({
                "schema_version": "paper-release-score-3",
                "document_type": "research_paper",
                "depth_profile": {"min_figures": 4},
            }))
            descriptor = root / "paper.json"
            descriptor.write_text(json.dumps({"paper_config_path": str(nested)}))
            paper_dir = root / "paper"
            paper_dir.mkdir()
            workflow["stages"].append({
                "id": "paper", "kind": "paper", "config_path": str(descriptor.resolve()),
                "project_dir": str(paper_dir.resolve()), "depends_on": ["experiment"],
                "estimate_seconds": 1, "bindings": [], "deadline_seconds": 10,
                "reuse_completed": False, "reuse_output_path": None,
            })
            runner = ComposerRunner(workflow)
            profile = runner._paper_depth_profile("experiment")
            self.assertIsNotNone(profile)
            self.assertEqual(profile[0]["min_figures"], 4)
            runner.close()

    def test_paper_figure_reconciliation_drops_stale_asset_bindings(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            paper_config = {"schema_version": "paper-release-score-3",
                            "figure_arguments": [{"asset_id": "old_figure", "unit_id": "results_p1",
                                                   "why": "old", "observation": "old"}]}
            packet = {"results_package": {"assets": [
                {"id": "new_figure", "role": "figure", "caption": "A new observed pattern."},
            ]}, "writer_contract": {"section_order": [{"id": "results", "unit_ids": ["results_p1"]}],
                                        "figure_readings": [
                {"asset_id": "old_figure", "unit_id": "results_p1", "why": "old", "observation": "old"},
            ]}}
            argument = {"figure_plan": [{"kind": "figure", "asset_id": "new_figure",
                                          "purpose": "A new display.", "readout": "A new observed pattern."}]}
            result = runner._synchronize_paper_figure_arguments(paper_config, packet, argument)
            self.assertEqual([item["asset_id"] for item in result["figure_arguments"]], ["new_figure"])
            self.assertEqual(packet["writer_contract"]["figure_readings"][0]["asset_id"], "new_figure")
            runner.close()

    def test_routes_internal_review_feedback_and_deduplicates_resume_replay(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            stage = workflow["stages"][0]
            event = {"event_id": "paper-review-r1-science", "kind": "review", "reviewer_id": "science",
                     "stage": 1, "status": "needs_revision", "decision": "revise",
                     "severity_counts": {"blocking": 0, "major": 1, "minor": 0},
                     "finding_ids": ["science_f1"]}
            runner._record_internal_feedback(stage, event)
            runner._record_internal_feedback(stage, event)
            self.assertEqual(len(runner.feedback), 1)
            self.assertEqual(runner.feedback[0]["to"], {"dept": "strategy", "agent": "chief"})
            self.assertEqual(runner.feedback[0]["action"], "request_revision")
            with sqlite3.connect(Path(workflow["project_id"]) / "state" / "control.sqlite") as conn:
                row = conn.execute("SELECT state, disposition FROM messages").fetchone()
                self.assertEqual(row, ("acknowledged", "scheduled"))

            failure = {"event_id": "paper-review-failure-r1-methods", "kind": "review_failure",
                       "reviewer_id": "methods", "status": "blocked", "error": "provider deadline"}
            runner._record_internal_feedback(stage, failure)
            self.assertEqual(len(runner.feedback), 2)
            self.assertEqual(runner.feedback[1]["to"], {"dept": "executive-command", "agent": "arbiter"})
            self.assertEqual(runner.feedback[1]["message_disposition"], "escalated")

    def test_projects_interpretation_package_binding_to_direct_artifact(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            workflow = self._workflow(root)
            runner = ComposerRunner(workflow)
            interpretation = {
                "schema_version": "scientific-interpretation-1",
                "research_question": "Does placement change the observed rate?",
                "result_patterns": [{
                    "id": "rate_shift", "result_ref": "rate", "pattern": "The observed rate changes.",
                    "so_what": "The practical ranking can change.", "supporting_evidence": ["rate"],
                    "contradicting_evidence": [],
                }],
                "competing_explanations": [{
                    "id": "geometry", "mechanism": "Geometry changes the error pattern.", "status": "possible",
                    "supporting_evidence": ["rate"], "counterevidence": [],
                    "discriminating_test": "Repeat the sweep across placements.",
                }],
                "discriminating_experiments": [{
                    "id": "placement_sweep", "question": "Does placement matter?",
                    "design": "Sweep the placement parameter.",
                    "predictions": ["The rate varies."], "required_measurements": ["Observed rate"],
                }],
                "prioritization": {
                    "primary_pattern_id": "rate_shift", "secondary_pattern_ids": [],
                    "rationale": "The rate shift is the central observation.",
                },
                "conclusion": "Placement may change the observed rate.",
            }
            package_path = root / "interpretation-package.json"
            package_path.write_text(json.dumps({
                "schema_version": "scientific-interpretation-package-1",
                "status": "accepted", "interpretation": interpretation,
            }))
            bound = runner._interpretation_binding_path(str(package_path.resolve()))
            self.assertTrue(Path(bound).is_file())
            self.assertEqual(json.loads(Path(bound).read_text()), interpretation)
            runner.close()


if __name__ == "__main__":
    unittest.main()
