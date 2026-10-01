"""Scientific execution contracts and an isolated end-to-end fixture."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ModelContractError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.runtime.execution import _invoke_worker
from scisaurus.runtime.experiment import (ExperimentProgramOutputContractError, ExperimentRunner,
                                          reconcile_model_review_disposition,
                                          _review_repair_directives,
                                          _scoped_review_assessment,
                                          validate_assessment, validate_deterministic_validation,
                                          validate_model_review, validate_program_output)
from scisaurus.runtime.experiment_config import validate_experiment_config
from scisaurus.runtime.models import ModelCallError
from scisaurus.runtime.research_quality import (
    AnalysisContractError, evaluate_result_package_quality,
)
from scisaurus.runtime.results import validate_results_package
from scisaurus.core.store import ArtifactStore


def fixture_worker(kind, params, channel):
    if kind != "model":
        return _invoke_worker(kind, params, channel)
    assignment = json.loads(params["prompt"])
    if assignment["phase"] == "experiment_result_review":
        from scisaurus.runtime.evidence import scientific_input_recovery_contract
        if assignment.get("scientific_input_recovery") != scientific_input_recovery_contract():
            raise AssertionError("experiment reviewers require the scientific input recovery contract")
        findings = assignment["program_output_summary"]["findings"]
        if assignment["required_finding_ids"] != sorted(item["id"] for item in findings):
            raise AssertionError("review prompt did not enumerate exact required finding IDs")
        value = {"reviewer_id": assignment["reviewer"]["id"], "decision": "accepted",
                 "checks": [{"check_id": check, "outcome": "passed", "evidence": "Bound fixture evidence passed."}
                            for check in assignment["required_checks"]],
                 "finding_assessments": [{"finding_id": item["id"], "outcome": "supported",
                                           "rationale": "The recalculated metric supports the bounded statement."}
                                          for item in findings],
                 "limitations": ["This fixture establishes workflow behavior only."]}
        if assignment.get("work_orders"):
            if "work_order_assessments field as a defect" not in assignment["instructions"]:
                raise AssertionError("review prompt does not separate producer output from peer verdicts")
            value["work_order_assessments"] = [{
                "work_order_id": order["id"],
                "outcome": "resolved",
                "evidence_paths": ["/findings/0", "/deterministic_validation/checks"],
                "limitation_path": None,
                "rationale": "The cited finding is supported by the fresh observations and recalculation.",
            } for order in assignment["work_orders"]]
    else:
        value = {"schema_version": "experiment-assessment-1", "study_id": assignment["study_id"],
                 "decision": "accepted_with_limitations", "summary": "The bounded fixture result passed.",
                 "evidence_refs": assignment["evidence_refs_exact"],
                 "reviewer_outcomes": assignment["reviewer_outcomes_exact"],
                 "accepted_findings": [item["id"] for item in assignment["findings"]],
                 "limitations": assignment["program_limitations"]}
    channel.put({"ok": True, "result": {"text": json.dumps(value), "model": "fixture-model",
                 "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 10},
                 "elapsed_seconds": 0.01, "finish_reason": "stop"}})


def rejecting_claims_fixture_worker(kind, params, channel):
    if kind == "model":
        assignment = json.loads(params["prompt"])
        if (assignment.get("phase") == "experiment_result_review"
                and assignment["reviewer"]["id"] == "claims"):
            findings = assignment["program_output_summary"]["findings"]
            value = {
                "reviewer_id": "claims", "decision": "rejected",
                "checks": [{"check_id": check,
                            "outcome": "failed" if check == "inference_scope" else "passed",
                            "evidence": "The observed effect does not support the declared inference."
                            if check == "inference_scope" else "Bound fixture evidence passed."}
                           for check in assignment["required_checks"]],
                "finding_assessments": [{"finding_id": item["id"], "outcome": "supported",
                                          "rationale": "The measured value supports this bounded observation."}
                                         for item in findings],
                "limitations": ["This fixture establishes workflow behavior only."],
            }
            channel.put({"ok": True, "result": {"text": json.dumps(value), "model": "fixture-model",
                         "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 10},
                         "elapsed_seconds": 0.01, "finish_reason": "stop"}})
            return
    return fixture_worker(kind, params, channel)


def overstated_claim_fixture_worker(kind, params, channel):
    if kind == "model":
        assignment = json.loads(params["prompt"])
        if assignment.get("phase") == "experiment_result_review":
            findings = assignment["program_output_summary"]["findings"]
            assessments = [
                {"finding_id": item["id"],
                 "outcome": "overstated" if index == 0 else "supported",
                 "rationale": "The first finding exceeds what the measured result establishes."
                 if index == 0 else "The measured value supports this bounded observation."}
                for index, item in enumerate(findings)]
            value = {
                "reviewer_id": assignment["reviewer"]["id"],
                "decision": "accepted_with_limitations",
                "checks": [{"check_id": check, "outcome": "passed",
                            "evidence": "The required review check was completed."}
                           for check in assignment["required_checks"]],
                "finding_assessments": assessments,
                "limitations": ["This fixture establishes workflow behavior only."],
            }
            channel.put({"ok": True, "result": {"text": json.dumps(value), "model": "fixture-model",
                         "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 10},
                         "elapsed_seconds": 0.01, "finish_reason": "stop"}})
            return
    return fixture_worker(kind, params, channel)


def disputed_claim_fixture_worker(kind, params, channel):
    if kind == "model":
        assignment = json.loads(params["prompt"])
        if (assignment.get("phase") == "experiment_result_review"
                and assignment["reviewer"]["id"] == "claims"):
            findings = assignment["program_output_summary"]["findings"]
            assessments = [
                {"finding_id": item["id"],
                 "outcome": "overstated" if index == 0 else "supported",
                 "rationale": "The first finding exceeds the tested inference scope."
                 if index == 0 else "The measured value supports this bounded observation."}
                for index, item in enumerate(findings)]
            value = {
                "reviewer_id": "claims", "decision": "accepted_with_limitations",
                "checks": [{"check_id": check, "outcome": "passed",
                            "evidence": "The required check was completed."}
                           for check in assignment["required_checks"]],
                "finding_assessments": assessments,
                "limitations": ["This fixture establishes workflow behavior only."],
            }
            channel.put({"ok": True, "result": {"text": json.dumps(value), "model": "fixture-model",
                         "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 10},
                         "elapsed_seconds": 0.01, "finish_reason": "stop"}})
            return
    return fixture_worker(kind, params, channel)


class ExperimentTests(unittest.TestCase):
    def test_runner_budget_failure_preserves_typed_fence_into_composer(self):
        from scisaurus.runtime.models import ModelBudgetExceededError
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.core.errors import QuotaExceededError
        runner = ExperimentRunner(self.root / "budget-failure", self.config())
        admission = {"path": str(self.root / "owner.sqlite"), "key": "stage",
                     "dimension": "input_tokens", "limit": 100,
                     "observed": 70, "reserved": 20, "requested": 11}
        error = ModelBudgetExceededError("admission rejected", budget_admission=admission,
                                        outcome_known=True, attempts=0)
        error.usage = {"input_tokens": 7}
        with patch.object(runner, "_setup", side_effect=error):
            result = runner.run()
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(result["failure"], error.failure_details())
        with self.assertRaises(ModelBudgetExceededError) as raised:
            ComposerRunner._raise_stage_failure(result)
        self.assertIsInstance(raised.exception, QuotaExceededError)
        self.assertEqual(raised.exception.budget_admission, admission)
        self.assertEqual(raised.exception.attempts, 0)
        self.assertEqual(raised.exception.usage, result["usage"])
        self.assertEqual(raised.exception.stage_result, result)

    def test_integrated_model_reviews_use_json_mode_and_configured_output_budget(self):
        config = self.config()
        config["model"]["max_output_tokens"] = 8192
        runner = ExperimentRunner(self.root / "review-contract", config)
        captured = {}

        def call_batch(specs, *, max_parallel):
            spec = specs[0]
            captured.update(spec["params"]["client"])
            assignment = json.loads(spec["params"]["prompt"])
            reviewer_id = assignment["reviewer"]["id"]
            review = {
                "reviewer_id": reviewer_id,
                "decision": "accepted",
                "checks": [{"check_id": item, "outcome": "passed", "evidence": "Checked."}
                           for item in assignment["required_checks"]],
                "finding_assessments": [
                    {"finding_id": item, "outcome": "supported", "rationale": "Supported."}
                    for item in assignment["required_finding_ids"]],
                "limitations": [],
            }
            return {spec["task_id"]: {"ok": True, "result": {
                "text": json.dumps(review), "model": "fixture-model",
                "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 10},
                "elapsed_seconds": 0.01, "finish_reason": "stop",
            }, "record_ref": "artifact:test/execution@1"}}

        try:
            with patch.object(runner, "_call_batch", side_effect=call_batch), \
                    patch.object(runner.tasks, "transition"), \
                    patch.object(runner, "_complete"):
                result = runner._model_checked([{
                    "name": "claims", "actor": "methods.claims-reviewer",
                    "assignment": {
                        "reviewer": {"id": "claims"},
                        "required_checks": ["calculation_trace"],
                        "required_finding_ids": ["finding-1"],
                    },
                    "validator": lambda _value: None,
                }], images=[], stage="integrated_review")
            self.assertIn("claims", result)
            self.assertEqual(captured["output_format"], "json_object")
            self.assertEqual(captured["max_output_tokens"], 8192)
            self.assertNotIn("output_format", config["model"])
        finally:
            runner.control.close()

    def test_truncated_experiment_review_continues_same_assignment(self):
        config = self.config()
        runner = ExperimentRunner(self.root / "review-continuation", config)
        self.addCleanup(runner.control.close)
        calls = []
        prefix = '{"reviewer_id":"claims",'
        complete = '{"reviewer_id":"claims","decision":"accepted"}'

        def call_batch(specs, **_kwargs):
            spec = specs[0]
            calls.append(spec)
            result = {
                "text": prefix if len(calls) == 1 else complete,
                "model": "fixture-model",
                "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 10},
                "elapsed_seconds": 0.01,
                "finish_reason": "length" if len(calls) == 1 else "stop",
            }
            return {spec["task_id"]: {
                "ok": True, "result": result,
                "record_ref": f"artifact:test/execution@{len(calls)}",
            }}

        runner._call_batch = call_batch
        with patch.object(runner.tasks, "transition"), patch.object(runner, "_complete"):
            accepted = runner._model_checked([{
                "name": "claims", "actor": "methods.claims-reviewer",
                "assignment": {
                    "reviewer": {"id": "claims"},
                    "required_checks": [], "required_finding_ids": [],
                },
                "validator": lambda _value: None,
            }], images=[], stage="integrated_review")

        self.assertIn("claims", accepted)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["params"]["prompt"], calls[1]["params"]["prompt"])
        self.assertEqual(calls[1]["params"]["continuation_text"], prefix)

    def test_experiment_review_preserves_typed_model_429(self):
        runner = ExperimentRunner(self.root / "review-rate-limit", self.config())
        self.addCleanup(runner.control.close)
        runner._call_batch = lambda specs, **_kwargs: {
            specs[0]["task_id"]: {
                "ok": False,
                "error": "model HTTP request failed with status 429",
                "error_type": "ModelCallError",
                "outcome_known": True,
                "attempts": 1,
                "status_code": 429,
                "retry_after_seconds": 3600,
                "provider_error_kind": "quota_exhausted",
            }
        }
        with self.assertRaises(ModelCallError) as caught:
            runner._model_checked([{
                "name": "claims", "actor": "methods.claims-reviewer",
                "assignment": {"reviewer": {"id": "claims"},
                               "required_checks": [], "required_finding_ids": []},
                "validator": lambda _value: None,
            }], images=[], stage="integrated_review")
        self.assertEqual(caught.exception.status_code, 429)
        self.assertEqual(caught.exception.retry_after_seconds, 3600)
        self.assertEqual(caught.exception.provider_error_kind, "quota_exhausted")
        self.assertTrue(caught.exception.outcome_known)

    def test_exhausted_experiment_reviewer_schema_repair_is_not_scientific_failure(self):
        from scisaurus.runtime.failure_recovery import classify_failure

        config = self.config()
        config["limits"]["max_rounds"] = 1
        runner = ExperimentRunner(self.root / "review-contract-failure", config)
        self.addCleanup(runner.control.close)
        runner._call_batch = lambda specs, **_kwargs: {
            specs[0]["task_id"]: {
                "ok": True,
                "result": {
                    "text": json.dumps({"reviewer_id": "claims", "decision": "rejected"}),
                    "model": "fixture-model",
                    "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 10},
                    "elapsed_seconds": 0.01,
                    "finish_reason": "stop",
                },
                "record_ref": "artifact:test/review-contract@1",
            }
        }

        def reject_duplicate_finding_ids(_value):
            raise ValidationError(
                "experiment review finding IDs must match the required IDs exactly once; "
                "missing=[]; unknown=[]; duplicates=['finding_ablation']")

        job = {
            "name": "claims", "actor": "methods.claims-reviewer",
            "assignment": {"reviewer": {"id": "claims"},
                           "required_checks": ["calculation_trace"],
                           "required_finding_ids": ["finding-ablation"]},
            "validator": reject_duplicate_finding_ids,
        }
        with patch.object(runner.tasks, "transition"), self.assertRaises(
                ModelContractError) as caught:
            runner._model_checked([job], images=[], stage="integrated_review")
        self.assertEqual(caught.exception.failure_class, "model_contract")
        self.assertEqual(
            classify_failure("experiment", caught.exception), "model_contract")

    def test_process_stop_exports_a_resumable_interruption(self):
        runner = ExperimentRunner(self.root / "interrupted-run", self.config())
        with patch.object(runner, "_setup", side_effect=KeyboardInterrupt("termination requested")):
            result = runner.run()
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["failure"], {"kind": "process_interrupted"})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-experiment-test-")
        self.root = Path(self.temp.name)
        self.executor = self.root / "execute.py"
        self.validator = self.root / "validate.py"
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
        self.executor.write_text(
            "import base64,hashlib,json,pathlib,sys\n"
            "x=json.load(sys.stdin)\n"
            "if 'experiment' not in x: print(json.dumps({'probe':True})); raise SystemExit\n"
            f"b=base64.b64decode({base64.b64encode(png).decode()!r})\n"
            "pathlib.Path('figure.png').write_bytes(b)\n"
            "e=x['experiment']\n"
            "v={'schema_version':'experiment-program-output-1','study_id':e['id'],'revision':e['revision'],"
            "'procedures':[{'id':'method','description':e['method'],'source':'execute.py'}],"
            "'observations':[{'run':1,'accuracy':0.75}],"
            "'metrics':[{'id':'accuracy','value':0.75,'unit':'proportion','conditions':'fixture',"
            "'source':'raw-data.json','presentation':'0.75 accuracy'}],"
            "'findings':[{'id':'observed','statement':'The fixture produced 0.75 accuracy.',"
            "'metric_ids':['accuracy']}],'limitations':e['limitations'],"
            "'assets':[{'id':'main_figure','path':'figure.png','sha256':hashlib.sha256(b).hexdigest(),"
            "'role':'figure','media_type':'image/png','caption':'Fixture result.'}]}\n"
            "print(json.dumps(v,sort_keys=True,separators=(',',':')))\n")
        self.validator.write_text(
            "import json,sys\n"
            "x=json.load(sys.stdin)\n"
            "if 'candidate' not in x: print(json.dumps({'probe':True})); raise SystemExit\n"
            "c=x['candidate']; e=x['experiment']; m=c['metrics'][0]\n"
            "enough=len(c['observations']) >= int(e['run_count'])\n"
            "v={'schema_version':'experiment-validation-1','study_id':c['study_id'],"
            "'candidate_sha256':x['candidate_sha256'],'decision':'accepted' if enough else 'rejected',"
            "'checks':[{'id':'row_count','outcome':'passed' if enough else 'failed',"
            "'evidence':'Observed %d rows against %d planned.' % (len(c['observations']),e['run_count'])}],"
            "'metric_recalculations':[{'metric_id':'accuracy','reported_value':m['value'],"
            "'recalculated_value':c['observations'][0]['accuracy'],'tolerance':0,'matches':True}],"
            "'limitations':['Workflow fixture only.']}\nprint(json.dumps(v))\n")

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        capability = lambda cid, script: {"id": cid, "adapter": "local_program",
            "client": {"command": [sys.executable, str(script)], "timeout": 5, "max_bytes": 1000000,
                       "cwd": str(self.root), "env": {}, "own_process_group": False},
            "representative": {"input": {"probe": True}}, "environment_files": [str(script)], "input": {}}
        return {"live_dispatch_allowed": True, "data_classification": "public",
            "allocation_mode": "capacity_pool", "project_id": "experiment-fixture",
            "objective": "Exercise a real program, replay, independent calculation, and result review.",
            "supplied_context": "Synthetic workflow fixture with no scientific interpretation.",
            "model": {"protocol": "openai_compatible", "base_url": "http://example.invalid/v1",
                      "model": "fixture-model", "timeout_seconds": 5, "max_output_tokens": 2000,
                      "auth_env": None},
            "limits": {"max_rounds": 2, "wall_clock_seconds": 30, "checkpoint_seconds": 1,
                       "max_result_bytes": 2000000, "concurrent_calls": 3},
            "time_policy": {"first_result_seconds": 20, "target_seconds": 25, "hard_seconds": 30},
            "experiment": {"id": "fixture_study", "revision": 1, "study_type": "methods_validation",
                "domain": "software validation", "research_question": "Does the fixture preserve evidence?",
                "hypothesis": "The accepted package will bind a replay and an independent calculation.",
                "method": "Execute the fixed fixture once and replay it exactly.", "parameters": {"n": 1},
                "seed": 7, "run_count": 1, "stopping_rule": "Stop after one configured run.",
                "primary_outcomes": [{"id": "accuracy", "definition": "Fixture accuracy",
                                      "unit": "proportion", "direction": "descriptive", "threshold": None}],
                "limitations": ["This fixture establishes workflow behavior only."], "literature_gate": None,
                "execution": capability("study_executor", self.executor),
                "validation": capability("study_validator", self.validator),
                "required_assets": [{"role": "figure", "media_types": ["image/png"], "min_count": 1}],
                "reviewers": [{"id": "methods", "focus": "method binding"},
                              {"id": "claims", "focus": "claim scope"}],
                "stage_seconds": {key: 0.1 for key in (
                    "setup", "supervision", "production", "unit_review", "integrated_review", "reassessment")},
                "max_observations": 10, "max_asset_bytes": 100000}}

    def test_program_and_deterministic_contracts_reject_missing_evidence(self):
        config = validate_experiment_config(self.config())["experiment"]
        candidate = {"schema_version": "experiment-program-output-1", "study_id": "fixture_study", "revision": 1,
            "procedures": [{"id": "method", "description": config["method"], "source": "execute.py"}],
            "observations": [{"run": 1}],
            "metrics": [{"id": "accuracy", "value": .75, "unit": "proportion", "conditions": "fixture",
                         "source": "raw-data.json", "presentation": "0.75 accuracy"}],
            "findings": [{"id": "observed", "statement": "Observed.", "metric_ids": ["accuracy"]}],
            "limitations": [], "assets": []}
        with self.assertRaisesRegex(ValidationError, "limitation"):
            validate_program_output(candidate, config)
        rejected = {"schema_version": "experiment-validation-1", "study_id": "fixture_study",
            "candidate_sha256": "0" * 64, "decision": "accepted",
            "checks": [{"id": "rows", "outcome": "failed", "evidence": "missing"}],
            "metric_recalculations": [{"metric_id": "accuracy", "reported_value": .75,
                "recalculated_value": .5, "tolerance": 0, "matches": False}], "limitations": []}
        with self.assertRaisesRegex(ValidationError, "contradicts"):
            validate_deterministic_validation(rejected, config, "0" * 64)

    def test_scoped_work_orders_reach_executor_and_require_result_linked_evidence(self):
        order = {
            "id": "repair-estimator", "kind": "additional_experiment",
            "owner": "methods.validation", "objective": "Re-estimate the onset on a refined grid.",
            "why": "The prior estimate was grid-boundary sensitive.",
            "success_condition": "A fresh interior estimate is reproduced and independently reviewed.",
            "evidence_needed": "Raw observations, recalculated metric, and a sensitivity result.",
        }
        config = self.config()
        config["work_orders"] = [order]
        validated = validate_experiment_config(config)
        runner = object.__new__(ExperimentRunner)
        runner.experiment = validated["experiment"]
        runner.work_orders = validated["work_orders"]
        payload = runner._program_input()
        self.assertEqual(payload["configured_input"]["work_orders"], [order])

        experiment = validated["experiment"]
        candidate = {
            "schema_version": "experiment-program-output-1", "study_id": experiment["id"],
            "revision": experiment["revision"],
            "procedures": [{"id": "refined_grid", "description": experiment["method"],
                            "source": "reproducible.py"}],
            "observations": [{"replicate": 1, "accuracy": 0.75}],
            "metrics": [{"id": "accuracy", "value": 0.75, "unit": "proportion",
                         "conditions": "refined grid", "source": "observations",
                         "presentation": "0.75"}],
            "findings": [{"id": "refined_estimate", "statement": "The refined estimate is interior.",
                          "metric_ids": ["accuracy"]}],
            "limitations": experiment["limitations"],
            "assets": [{"id": "figure_1", "path": "figure_1.png", "sha256": "0" * 64,
                        "role": "figure", "media_type": "image/png", "caption": "Refined estimate."}],
        }
        self.assertIs(validate_program_output(candidate, experiment, [order]), candidate)
        self_assessment = {**candidate, "work_order_assessments": [{
            "work_order_id": order["id"], "disposition": "completed",
            "summary": "Self-reported completion.", "evidence_paths": ["/metrics/99"],
            "limitation_path": None,
        }]}
        self.assertIs(validate_program_output(self_assessment, experiment, [order]), self_assessment)

    def test_independent_review_must_adjudicate_each_scoped_work_order(self):
        order = {
            "id": "repair-estimator", "kind": "additional_experiment",
            "owner": "methods.validation", "objective": "Repair the estimator.",
            "why": "The prior estimate was unstable.",
            "success_condition": "A fresh estimate passes independent review.",
            "evidence_needed": "Raw rows and an independently checked metric.",
        }
        program_output = {
            "procedures": [{"description": "The estimator was recomputed."}],
            "observations": [{"estimate": 0.5}], "metrics": [{"value": 0.5}],
            "findings": [], "assets": [], "analysis": {}, "limitations": [],
        }
        deterministic_validation = {
            "checks": [{"id": "independent_recalculation", "outcome": "passed",
                        "evidence": "The metric was independently recalculated."}],
            "metric_recalculations": [],
        }
        required_checks = sorted({"method_alignment", "calculation_trace", "inference_scope",
                                  "limitation_coverage", "work_order_resolution"})
        review = {
            "reviewer_id": "methods", "decision": "accepted",
            "checks": [{"check_id": check, "outcome": "passed", "evidence": "Checked."}
                       for check in required_checks],
            "finding_assessments": [], "limitations": [],
            "work_order_assessments": [{
                "work_order_id": order["id"], "outcome": "resolved",
                "evidence_paths": ["/metrics/0", "/deterministic_validation/checks"],
                "limitation_path": None,
                "rationale": "The cited recomputation satisfies the repair condition.",
            }],
        }
        self.assertEqual(validate_model_review(
            review, "methods", set(), [order], program_output,
            deterministic_validation=deterministic_validation)["reviewer_id"], "methods")
        unsupported_path = json.loads(json.dumps(review))
        unsupported_path["work_order_assessments"][0]["evidence_paths"] = ["/metrics/99"]
        with self.assertRaisesRegex(ValidationError, "does not resolve"):
            validate_model_review(unsupported_path, "methods", set(), [order], program_output,
                                  deterministic_validation=deterministic_validation)
        unavailable_validation = json.loads(json.dumps(review))
        with self.assertRaisesRegex(ValidationError, "unavailable independent validation"):
            validate_model_review(unavailable_validation, "methods", set(), [order], program_output)
        self_assertion_path = json.loads(json.dumps(review))
        self_assertion_path["work_order_assessments"][0]["evidence_paths"] = [
            "/work_order_assessments/0"]
        with self.assertRaisesRegex(ValidationError, "program output or independent validation"):
            validate_model_review(self_assertion_path, "methods", set(), [order], program_output,
                                  deterministic_validation=deterministic_validation)
        unresolved = json.loads(json.dumps(review))
        unresolved["work_order_assessments"][0]["outcome"] = "not_resolved"
        unresolved["checks"][required_checks.index("work_order_resolution")]["outcome"] = "insufficient_evidence"
        unresolved["decision"] = "rejected"
        self.assertEqual(validate_model_review(
            unresolved, "methods", set(), [order], program_output,
            deterministic_validation=deterministic_validation)["reviewer_id"], "methods")
        unresolved["checks"][required_checks.index("work_order_resolution")]["outcome"] = "passed"
        with self.assertRaisesRegex(ValidationError, "cannot pass"):
            validate_model_review(unresolved, "methods", set(), [order], program_output,
                                  deterministic_validation=deterministic_validation)

    def test_review_evidence_paths_resolve_stable_ids_and_reject_ambiguity(self):
        order = {
            "id": "repair-kill-condition", "kind": "result_repair",
            "owner": "methods.validation", "objective": "Correct the disjunctive finding.",
            "why": "The prior wording claims a sign-change condition that did not occur.",
            "success_condition": "The revised result distinguishes the triggered spread clause.",
            "evidence_needed": "A corrected finding and fresh validation.",
        }
        program_output = {
            "procedures": [], "observations": [], "metrics": [{"id": "spread", "value": 0.086141}],
            "findings": [{"id": "finding_kill_condition", "statement": "Only the spread clause fired."}],
            "assets": [], "analysis": {}, "limitations": [],
        }
        deterministic_validation = {
            "checks": [{"id": "independent_recalculation", "outcome": "passed",
                        "evidence": "The reported spread was independently recalculated."}],
            "metric_recalculations": [],
        }
        required_checks = sorted({"method_alignment", "calculation_trace", "inference_scope",
                                  "limitation_coverage", "work_order_resolution"})
        review = {
            "reviewer_id": "adversarial_claims", "decision": "rejected",
            "checks": [{"check_id": check,
                        "outcome": "insufficient_evidence" if check == "work_order_resolution" else "passed",
                        "evidence": "The result is scoped to the analytic model."}
                       for check in required_checks],
            "finding_assessments": [{"finding_id": "finding_kill_condition", "outcome": "overstated",
                                     "rationale": "The sign-change disjunct did not fire."}],
            "limitations": [],
            "work_order_assessments": [{
                "work_order_id": order["id"], "outcome": "not_resolved",
                "evidence_paths": ["/metrics/0", "/findings/finding_kill_condition",
                                   "/deterministic_validation/checks/independent_recalculation"],
                "limitation_path": None,
                "rationale": "The finding still needs a fresh repaired execution.",
            }],
        }
        self.assertEqual(validate_model_review(
            review, "adversarial_claims", {"finding_kill_condition"}, [order], program_output,
            deterministic_validation=deterministic_validation)["reviewer_id"], "adversarial_claims")

        ambiguous = json.loads(json.dumps(program_output))
        ambiguous["findings"].append({"finding_id": "finding_kill_condition", "statement": "Duplicate ID."})
        with self.assertRaisesRegex(ValidationError, "ambiguous.*findings/finding_kill_condition"):
            validate_model_review(
                review, "adversarial_claims", {"finding_kill_condition"}, [order], ambiguous,
                deterministic_validation=deterministic_validation)

        unknown = json.loads(json.dumps(review))
        unknown["work_order_assessments"][0]["evidence_paths"] = ["/findings/finding_missing"]
        with self.assertRaisesRegex(ValidationError, "does not resolve.*findings/finding_missing"):
            validate_model_review(
                unknown, "adversarial_claims", {"finding_kill_condition"}, [order], program_output,
                deterministic_validation=deterministic_validation)

    def test_primary_outcome_id_mismatch_reports_missing_and_unexpected_ids(self):
        config = validate_experiment_config(self.config())["experiment"]
        validation = {
            "schema_version": "experiment-validation-1",
            "study_id": config["id"],
            "candidate_sha256": "0" * 64,
            "decision": "accepted",
            "checks": [{"id": "rows", "outcome": "passed", "evidence": "One row was checked."}],
            "metric_recalculations": [{
                "metric_id": "wrong_outcome",
                "reported_value": 0.75,
                "recalculated_value": 0.75,
                "tolerance": 0,
                "matches": True,
            }],
            "limitations": [],
        }

        with self.assertRaisesRegex(
                ValidationError,
                r"missing metric_ids=\['accuracy'\].*unexpected metric_ids=\['wrong_outcome'\]"):
            validate_deterministic_validation(validation, config, "0" * 64)

    def test_multicondition_observation_overflow_explains_total_row_bound(self):
        config = self.config()["experiment"]
        config["run_count"] = 40
        config["max_observations"] = 4000
        candidate = {
            "schema_version": "experiment-program-output-1",
            "study_id": config["id"],
            "revision": config["revision"],
            "procedures": [],
            "observations": [{} for _ in range(40 * 121)],
            "metrics": [],
            "findings": [],
            "limitations": [],
            "assets": [],
        }
        with self.assertRaisesRegex(
                ValidationError,
                r"4840 observation rows but max_observations=4000.*total row ceiling.*"
                r"complete planned row count"):
            validate_program_output(candidate, config)

    def test_censored_metric_is_replayed_as_matching_null_not_zero(self):
        package = {
            "schema_version": "results-package-1", "id": "censored-study", "revision": 1,
            "procedures": [{"id": "method", "description": "Bounded crossing scan.",
                             "source": "observations"}],
            "metrics": [{"id": "onset", "value": None, "unit": "nm",
                          "conditions": "no interior crossing", "source": "observations",
                          "presentation": "censored when no interior crossing exists"}],
            "findings": [{"id": "censored", "statement": "The onset is censored.",
                           "metric_ids": ["onset"]}],
            "limitations": ["Only the declared grid is covered."], "assets": [],
        }
        validate_results_package(package)
        experiment = self.config()["experiment"]
        experiment["primary_outcomes"] = [{"id": "onset", "definition": "Crossing onset",
                                            "unit": "nm", "direction": "descriptive", "threshold": None}]
        validation = {
            "schema_version": "experiment-validation-1", "study_id": "fixture_study",
            "candidate_sha256": "0" * 64, "decision": "accepted",
            "checks": [{"id": "crossing", "outcome": "passed", "evidence": "Both paths found no crossing."}],
            "metric_recalculations": [{"metric_id": "onset", "reported_value": None,
                                        "recalculated_value": None, "tolerance": 0,
                                        "matches": True}],
            "limitations": ["Censoring is explicit."]}
        self.assertEqual(validate_deterministic_validation(validation, experiment, "0" * 64)["decision"],
                         "accepted")

    def test_censored_outcome_is_allowed_but_all_null_primary_results_are_not(self):
        experiment = validate_experiment_config(self.config())["experiment"]
        experiment["primary_outcomes"] = [
            {"id": "accuracy", "definition": "Fixture accuracy", "unit": "proportion",
             "direction": "descriptive", "threshold": None},
            {"id": "onset", "definition": "Crossing onset", "unit": "nm",
             "direction": "descriptive", "threshold": None},
        ]

        def output(accuracy, onset_statement="The onset is censored.",
                   onset_presentation="Censored because no interior crossing was observed.",
                   onset_conditions="no interior crossing", extra_metrics=(), extra_findings=()):
            return {
                "schema_version": "experiment-program-output-1",
                "study_id": experiment["id"], "revision": experiment["revision"],
                "procedures": [{"id": "method", "description": experiment["method"],
                                "source": "observations"}],
                "observations": [{"replicate": 1, "accuracy": accuracy, "onset_nm": None,
                                  "onset_censored": True}],
                "metrics": [
                    {"id": "accuracy", "value": accuracy, "unit": "proportion",
                     "conditions": "fixture", "source": "observations",
                     "presentation": "Fixture accuracy."},
                    {"id": "onset", "value": None, "unit": "nm",
                     "conditions": onset_conditions, "source": "observations",
                     "presentation": onset_presentation},
                ] + list(extra_metrics),
                "findings": [
                    {"id": "accuracy_observed", "statement": "Fixture accuracy was recorded.",
                     "metric_ids": ["accuracy"]},
                    {"id": "onset_censored", "statement": onset_statement,
                     "metric_ids": ["onset"]},
                ] + list(extra_findings),
                "limitations": experiment["limitations"],
                "assets": [{"id": "figure_1", "path": "figure_1.png", "sha256": "0" * 64,
                            "role": "figure", "media_type": "image/png",
                            "caption": "Fixture outcomes."}],
            }

        self.assertEqual(validate_program_output(output(0.75), experiment)["study_id"],
                         "fixture_study")
        censored_with_boundary_as_event = output(0.75)
        censored_with_boundary_as_event["observations"][0]["onset_status"] = "non_crossing"
        censored_with_boundary_as_event["observations"][0]["onset_shear_rate"] = 50.0
        with self.assertRaisesRegex(ValidationError, "numeric event value"):
            validate_program_output(censored_with_boundary_as_event, experiment)
        censored_with_bound = output(0.75)
        censored_with_bound["observations"][0].update({
            "onset_status": "non_crossing", "onset_gamma_upper_bound": 50.0,
        })
        self.assertEqual(validate_program_output(censored_with_bound, experiment)["study_id"],
                         "fixture_study")
        with self.assertRaisesRegex(ValidationError, "all declared primary outcomes are null") as failure:
            validate_program_output(output(None), experiment)
        self.assertIn("Diagnose the undefinedness", str(failure.exception))
        self.assertIn("Preserve the admitted research question and primary outcome definitions",
                      str(failure.exception))
        self.assertIn("do not replace an outcome or change a parameter range merely",
                      str(failure.exception))
        self.assertNotIn("add a scientifically meaningful", str(failure.exception))
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(output(0.75, "The onset shifted by nan atm."), experiment)
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, onset_presentation="Undefined onset: NaN nm."), experiment)
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, onset_conditions="value=+Inf nm"), experiment)
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, onset_presentation="The onset is Infinity nm."), experiment)
        self.assertEqual(validate_program_output(output(
            0.75, onset_presentation="The onset is not Infinity nm."),
            experiment)["study_id"], "fixture_study")
        self.assertEqual(validate_program_output(output(
            0.75, onset_presentation=("Asymptotic limit: +Infinity describes the limiting case.")),
            experiment)["study_id"], "fixture_study")
        self.assertEqual(validate_program_output(output(
            0.75, onset_presentation=("Asymptotic limit: +Infinity nm describes the limiting case.")),
            experiment)["study_id"], "fixture_study")
        diagnostic = {"id": "diagnostic", "value": None, "unit": "atm",
                      "conditions": "finite sweep", "source": "raw observations",
                      "presentation": "Theoretical diagnostic."}
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, extra_metrics=[{**diagnostic,
                    "conditions": "NaN (undefined)"}]), experiment)
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, extra_metrics=[diagnostic], extra_findings=[
                    {"id": "diagnostic_result", "statement": "The diagnostic was Infinity.",
                     "metric_ids": ["diagnostic"]}]), experiment)
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, extra_metrics=[{**diagnostic,
                    "presentation": "Undefined diagnostic: Infinity."}]), experiment)
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, extra_metrics=[diagnostic], extra_findings=[
                    {"id": "diagnostic_nan", "statement": "The diagnostic was NaN.",
                     "metric_ids": ["diagnostic"]}]), experiment)
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, extra_metrics=[diagnostic], extra_findings=[
                    {"id": "diagnostic_infinity", "statement": "The diagnostic returned Infinity.",
                     "metric_ids": ["diagnostic"]}]), experiment)
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, extra_metrics=[diagnostic], extra_findings=[
                    {"id": "diagnostic_produced", "statement": "The diagnostic produced Infinity.",
                     "metric_ids": ["diagnostic"]}]), experiment)
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, extra_metrics=[diagnostic], extra_findings=[
                    {"id": "diagnostic_reports", "statement": "The diagnostic reports Infinity.",
                     "metric_ids": ["diagnostic"]}]), experiment)
        self.assertEqual(validate_program_output(output(
            0.75, extra_metrics=[diagnostic], extra_findings=[
                {"id": "diagnostic_no_infinity", "statement": "The diagnostic never returned Infinity.",
                 "metric_ids": ["diagnostic"]}]), experiment)["study_id"], "fixture_study")
        with self.assertRaisesRegex(ValidationError, "non-finite numeric marker"):
            validate_program_output(
                output(0.75, extra_metrics=[diagnostic], extra_findings=[
                    {"id": "diagnostic_after_negative_clause",
                     "statement": "The diagnostic had no finite observations but returned Infinity.",
                     "metric_ids": ["diagnostic"]}]), experiment)
        self.assertEqual(validate_program_output(output(
            0.75, onset_presentation=("The theoretical limit approaches infinity; "
                                     "no finite crossing was observed.")), experiment)["study_id"],
                         "fixture_study")
        self.assertEqual(validate_program_output(output(
            0.75, onset_presentation=("Asymptotic limit: infinity describes the limiting case.")),
            experiment)["study_id"], "fixture_study")
        self.assertEqual(validate_program_output(output(
            0.75, onset_presentation=("No NaN values were detected; the onset remains censored.")),
            experiment)["study_id"], "fixture_study")

    def test_program_asset_shape_failure_is_actionable_validation_error(self):
        config = validate_experiment_config(self.config())["experiment"]
        candidate = {
            "schema_version": "experiment-program-output-1",
            "study_id": "fixture_study", "revision": 1,
            "procedures": [{"id": "method", "description": config["method"],
                            "source": "execute.py"}],
            "observations": [{"run": 1, "accuracy": .75}],
            "metrics": [{"id": "accuracy", "value": .75, "unit": "proportion",
                         "conditions": "fixture", "source": "raw-data.json",
                         "presentation": "0.75 accuracy"}],
            "findings": [{"id": "observed", "statement": "Observed.",
                          "metric_ids": ["accuracy"]}],
            "limitations": config["limitations"],
            "assets": [{"id": "main_figure", "sha256": "0" * 64,
                        "role": "figure", "media_type": "image/png",
                        "caption": "Fixture result."}],
        }
        with self.assertRaisesRegex(ValidationError, "experiment asset requires exactly"):
            validate_program_output(candidate, config)

    def test_novel_research_requires_a_substantive_quality_contract(self):
        value = self.config()
        value["experiment"]["study_type"] = "novel_research"
        value["experiment"]["literature_gate"] = None
        with self.assertRaisesRegex(ValidationError, "quality_contract"):
            validate_experiment_config(value)
        value["experiment"]["quality_contract"] = {
            "minimum_conditions": 2, "minimum_independent_seeds": 1,
            "minimum_controls": 1, "minimum_comparisons": 2,
            "required_analyses": ["uncertainty", "effect_size", "sensitivity", "raw_data"],
            "minimum_figures": 3,
        }
        with self.assertRaisesRegex(ValidationError, "literature gate"):
            validate_experiment_config(value)
        self.assertEqual(validate_experiment_config(value, require_literature_gate=False)["experiment"]["study_type"],
                         "novel_research")
        with self.assertRaisesRegex(ValidationError, "literature gate"):
            validate_experiment_config(value)

    def test_quality_contract_defers_missing_analysis_to_research_admission(self):
        config = validate_experiment_config(self.config())["experiment"]
        config["quality_contract"] = {
            "minimum_conditions": 2, "minimum_independent_seeds": 1,
            "minimum_controls": 1, "minimum_comparisons": 1,
            "required_analyses": ["uncertainty", "effect_size", "sensitivity", "raw_data"],
            "minimum_figures": 1,
        }
        candidate = {"schema_version": "experiment-program-output-1", "study_id": "fixture_study", "revision": 1,
            "procedures": [{"id": "method", "description": config["method"], "source": "execute.py"}],
            "observations": [{"run": 1}],
            "metrics": [{"id": "accuracy", "value": .75, "unit": "proportion", "conditions": "fixture",
                         "source": "raw-data.json", "presentation": "0.75 accuracy"}],
            "findings": [{"id": "observed", "statement": "Observed.", "metric_ids": ["accuracy"]}],
            "limitations": config["limitations"], "assets": [{"id": "main_figure", "path": "figure.png",
                         "sha256": "0" * 64, "role": "figure", "media_type": "image/png", "caption": "Figure."}]}
        self.assertEqual(validate_program_output(candidate, config)["study_id"], "fixture_study")
        admission = evaluate_result_package_quality({
            "quality_contract": config["quality_contract"],
            "assets": candidate["assets"],
        })
        self.assertEqual(admission["decision"], "research_expansion_required")
        self.assertTrue(any(item["field"] == "analysis" for item in admission["deficits"]))

    def test_generated_program_output_shape_error_reports_contract_delta(self):
        experiment = validate_experiment_config(self.config())["experiment"]
        output = {
            "schema_version": "experiment-program-output-1",
            "study_id": experiment["id"], "revision": experiment["revision"],
            "procedures": [], "metrics": [], "findings": [], "limitations": [],
            "assets": [], "analysis": {}, "unrequested": True,
        }

        with self.assertRaises(ExperimentProgramOutputContractError) as raised:
            validate_program_output(output, experiment)

        self.assertEqual(raised.exception.failure_class, "model_contract")
        self.assertEqual(raised.exception.recovery_mode, "format_repair_then_rerun")
        self.assertEqual(raised.exception.repair_gate, "program_output_contract")
        self.assertEqual(raised.exception.missing_fields, ("observations",))
        self.assertEqual(raised.exception.unexpected_fields, ("unrequested",))
        self.assertIn("missing=['observations']", str(raised.exception))
        self.assertIn("unexpected=['unrequested']", str(raised.exception))

    def test_partial_analysis_is_normalized_to_explicit_quality_debt(self):
        config = validate_experiment_config(self.config())["experiment"]
        config["quality_contract"] = {
            "minimum_conditions": 2, "minimum_independent_seeds": 1,
            "minimum_controls": 1, "minimum_comparisons": 1,
            "required_analyses": ["uncertainty", "effect_size", "sensitivity", "raw_data"],
            "minimum_figures": 1,
        }
        candidate = {
            "schema_version": "experiment-program-output-1", "study_id": "fixture_study", "revision": 1,
            "procedures": [{"id": "method", "description": config["method"], "source": "execute.py"}],
            "observations": [{"run": 1}],
            "metrics": [{"id": "accuracy", "value": .75, "unit": "proportion", "conditions": "fixture",
                         "source": "raw-data.json", "presentation": "0.75 accuracy"}],
            "findings": [{"id": "observed", "statement": "Observed.", "metric_ids": ["accuracy"]}],
            "limitations": config["limitations"],
            "assets": [{"id": "main_figure", "path": "figure.png", "sha256": "0" * 64,
                        "role": "figure", "media_type": "image/png", "caption": "Figure."}],
            "analysis": {"conditions": [], "independent_seeds": []},
        }

        admitted_output = validate_program_output(candidate, config)
        self.assertEqual(set(admitted_output["analysis"]), {
            "conditions", "independent_seeds", "controls", "comparisons", "uncertainty",
            "effect_sizes", "sensitivity", "ablation", "raw_data",
        })
        admission = evaluate_result_package_quality({
            "quality_contract": config["quality_contract"],
            "analysis": admitted_output["analysis"],
            "assets": admitted_output["assets"],
        })
        self.assertEqual(admission["decision"], "research_expansion_required")
        deficits = {item["field"] for item in admission["deficits"]}
        self.assertTrue({"conditions", "independent_seeds", "controls", "comparisons",
                         "uncertainty", "effect_size", "sensitivity", "raw_data"}.issubset(deficits))

        incomplete = dict(admitted_output)
        incomplete["analysis"] = {**admitted_output["analysis"], "controls": ["", "  "]}
        normalized = validate_program_output(incomplete, config)
        self.assertEqual(normalized["analysis"]["controls"], [])
        incomplete_admission = evaluate_result_package_quality({
            "quality_contract": config["quality_contract"],
            "analysis": normalized["analysis"],
            "assets": normalized["assets"],
        })
        self.assertIn("controls", {item["field"] for item in incomplete_admission["deficits"]})

        duplicate_summary = dict(admitted_output)
        duplicate_summary["analysis"] = {
            **admitted_output["analysis"], "controls": [" control ", "control"]}
        self.assertEqual(validate_program_output(duplicate_summary, config)["analysis"]["controls"],
                         ["control"])

        malformed = dict(admitted_output)
        malformed["analysis"] = {**admitted_output["analysis"], "controls": [None]}
        with self.assertRaisesRegex(AnalysisContractError,
                                    "analysis.controls must contain only"):
            validate_program_output(malformed, config)

        structured = dict(admitted_output)
        structured["analysis"] = {
            **admitted_output["analysis"],
            "controls": [{"id": "baseline", "description": "Matched baseline."}],
            "comparisons": [{"id": "model_gap", "description": "Paired fit contrast.",
                             "delta_rms": 0.0414}],
            "uncertainty": [{"id": "digitization_bootstrap",
                             "description": "Bootstrap over digitized gap points.",
                             "replicates": 2000}],
        }
        normalized_structured = validate_program_output(structured, config)
        self.assertEqual(normalized_structured["analysis"]["comparisons"][0]["delta_rms"],
                         0.0414)
        self.assertEqual(normalized_structured["analysis"]["uncertainty"][0]["replicates"],
                         2000)
        structured_admission = evaluate_result_package_quality({
            "quality_contract": config["quality_contract"],
            "analysis": normalized_structured["analysis"],
            "assets": normalized_structured["assets"],
        })
        self.assertEqual(structured_admission["observed"]["comparisons"], 1)

    def test_assessment_cannot_rephrase_or_duplicate_program_limitations(self):
        value = {"schema_version": "experiment-assessment-1", "study_id": "fixture_study",
            "decision": "accepted_with_limitations", "summary": "Bounded fixture result.",
            "evidence_refs": ["artifact:evidence@1"],
            "reviewer_outcomes": [{"reviewer_id": "methods", "decision": "accepted"}],
            "accepted_findings": ["observed"], "limitations": ["A paraphrased limitation."]}
        with self.assertRaisesRegex(ValidationError, "exact program limitations"):
            validate_assessment(value, "fixture_study", ["artifact:evidence@1"],
                [{"reviewer_id": "methods", "decision": "accepted"}], {"observed"},
                ["This fixture establishes workflow behavior only."])

    def test_end_to_end_run_publishes_replay_validated_results_package(self):
        runner = ExperimentRunner(self.root / "run", self.config())
        runner.worker_target = fixture_worker
        result = runner.run()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(result["execution_refs"]), 2)
        package_path = Path(result["results_package"])
        package = json.loads(package_path.read_text())
        validate_results_package(package, base_dir=package_path.parent)
        self.assertEqual(package["schema_version"], "results-package-2")
        self.assertEqual(package["validation"]["decision"], "accepted_with_limitations")
        self.assertRegex(package["provenance"]["replay_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(package["provenance"]["replay_sha256"], "0" * 64)
        self.assertTrue((package_path.parent / "figure.png").is_file())
        self.assertTrue(result["event_chain"][0])

    def test_review_transport_failure_retains_executed_results_and_typed_failure(self):
        runner = ExperimentRunner(self.root / "review-failure", self.config())
        runner.worker_target = fixture_worker
        with patch.object(runner, "_model_reviews", side_effect=ModelCallError(
                "model call budget exhausted", outcome_known=True, attempts=2)):
            result = runner.run()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure"]["kind"], "model_call")
        self.assertEqual(result["failure"]["attempts"], 2)
        raw = json.loads(Path(result["raw_results"]).read_text())
        self.assertEqual(result["raw_results_sha256"],
                         hashlib.sha256(Path(result["raw_results"]).read_bytes()).hexdigest())
        self.assertEqual(raw["study_id"], "fixture_study")
        self.assertEqual(raw["observations"], [{"run": 1, "accuracy": .75}])
        self.assertEqual(len(result["execution_refs"]), 2)
        self.assertIsNotNone(result["deterministic_validation_ref"])
        self.assertIsNone(result["results_package"])

    def test_end_to_end_work_order_is_executed_reviewed_and_preserved_in_package(self):
        config = self.config()
        config["work_orders"] = [{
            "id": "refine-estimator", "kind": "additional_experiment",
            "owner": "methods.validation", "objective": "Recompute the estimator on the declared grid.",
            "why": "The previous result needs a targeted sensitivity repair.",
            "success_condition": "The recomputed metric is linked to emitted observations.",
            "evidence_needed": "A metric row and raw observation row cited by output paths.",
        }]
        runner = ExperimentRunner(self.root / "work-order-run", config)
        runner.worker_target = fixture_worker
        result = runner.run()
        self.assertEqual(result["status"], "completed", result)
        package_path = Path(result["results_package"])
        package = json.loads(package_path.read_text())
        validate_results_package(package, base_dir=package_path.parent)
        work_order = package["work_order_assessments"][0]
        self.assertEqual(work_order["id"], "refine-estimator")
        self.assertEqual(work_order["disposition"], "completed")
        self.assertEqual(work_order["evidence_paths"], [
            "/findings/0", "/deterministic_validation/checks"])
        self.assertEqual(len(work_order["reviewer_assessments"]), 2)
        self.assertEqual({item["outcome"] for item in work_order["reviewer_assessments"]},
                         {"resolved"})

    def test_scope_only_review_concern_does_not_rerun_a_reproducible_experiment(self):
        config = self.config()
        runner = ExperimentRunner(self.root / "scoped-review-run", config)
        runner.worker_target = rejecting_claims_fixture_worker
        original = runner._model_checked
        assessment_jobs = []

        def record_jobs(jobs, **kwargs):
            assessment_jobs.extend(job["name"] for job in jobs
                                   if kwargs.get("stage") == "integrated_review")
            return original(jobs, **kwargs)

        with patch.object(runner, "_model_checked", side_effect=record_jobs):
            result = runner.run()

        self.assertEqual(result["status"], "completed", result)
        self.assertNotIn("repair_rejected_experiment_result",
                         {item["id"] for item in result["research_expansion_requests"]})
        self.assertEqual(result["release_status"], "not_released")
        self.assertIsNotNone(result["incumbent_ref"])
        self.assertNotIn("final-assessment", assessment_jobs)
        package_path = Path(result["results_package"])
        package = json.loads(package_path.read_text())
        validate_results_package(package, base_dir=package_path.parent)
        self.assertEqual(package["schema_version"], "results-package-3")
        self.assertEqual(package["validation"]["decision"], "accepted_with_limitations")
        self.assertEqual(package["withheld_findings"], [])
        self.assertTrue(any("The inference scope remains bounded" in item
                            for item in package["limitations"]))
        control = ControlStore(runner.dir)
        try:
            store = ArtifactStore(control)
            assessment_record = store.get(result["assessment_ref"])
            assessment = json.loads(store.read_body(assessment_record["body_hash"]))
        finally:
            control.close()
        self.assertEqual(assessment["schema_version"], "experiment-assessment-2")
        self.assertEqual(assessment["accepted_findings"],
                         [item["id"] for item in package["findings"]])
        self.assertIn("inference_scope=failed", assessment["summary"])
        self.assertTrue(package["findings"])
        self.assertTrue((package_path.parent / "raw-data.json").is_file())
        report = package_path.parent.parent / "experiment.md"
        self.assertIn("Independent assessment: **accepted_with_limitations**", report.read_text())

    def test_disputed_finding_is_withheld_while_consensus_results_remain_usable(self):
        source = self.executor.read_text()
        original = (
            "'findings':[{'id':'observed','statement':'The fixture produced 0.75 accuracy.',"
            "'metric_ids':['accuracy']}],'limitations':e['limitations'],"
        )
        expanded = (
            "'findings':[{'id':'observed','statement':'The fixture produced 0.75 accuracy.',"
            "'metric_ids':['accuracy']},{'id':'bounded','statement':'The fixture output contains "
            "one measured accuracy value.','metric_ids':['accuracy']}],"
            "'limitations':e['limitations'],"
        )
        self.assertIn(original, source)
        self.executor.write_text(source.replace(original, expanded, 1))
        runner = ExperimentRunner(self.root / "disputed-finding-run", self.config())
        runner.worker_target = disputed_claim_fixture_worker
        result = runner.run()

        self.assertEqual(result["status"], "completed", result)
        self.assertNotIn("repair_rejected_experiment_result",
                         {item["id"] for item in result["research_expansion_requests"]})
        package_path = Path(result["results_package"])
        package = json.loads(package_path.read_text())
        validate_results_package(package, base_dir=package_path.parent)
        self.assertEqual(package["schema_version"], "results-package-3")
        self.assertEqual(package["validation"]["decision"], "accepted_with_limitations")
        self.assertEqual(len(package["withheld_findings"]), 1)
        withheld = package["withheld_findings"][0]
        self.assertEqual(withheld["id"], "observed")
        self.assertEqual({item["outcome"] for item in withheld["reviewer_assessments"]},
                         {"supported", "overstated"})
        self.assertTrue(any("withheld from the admitted results" in item
                            for item in package["limitations"]))
        self.assertNotIn(withheld["id"], {item["id"] for item in package["findings"]})

    def test_scoped_acceptance_never_overrides_a_methods_or_calculation_failure(self):
        finding_ids = {"observed"}
        reviews = [
            {
                "reviewer_id": "methods", "decision": "rejected",
                "checks": [{"check_id": "method_alignment", "outcome": "failed",
                            "evidence": "The operation does not implement the frozen method."}],
                "finding_assessments": [{"finding_id": "observed", "outcome": "supported",
                                         "rationale": "The displayed number is arithmetically correct."}],
            },
            {
                "reviewer_id": "independent", "decision": "accepted",
                "checks": [{"check_id": "method_alignment", "outcome": "passed",
                            "evidence": "The method is aligned."}],
                "finding_assessments": [{"finding_id": "observed", "outcome": "supported",
                                         "rationale": "The finding is supported."}],
            },
        ]
        self.assertIsNone(_scoped_review_assessment(reviews, finding_ids))

    def test_repair_directives_preserve_review_claims_as_unverified_hypotheses(self):
        directives = _review_repair_directives([{
            "reviewer_id": "independent",
            "decision": "rejected",
            "checks": [{"check_id": "inference_scope", "outcome": "failed",
                        "evidence": "The comparison supports the causal wording."}],
            "finding_assessments": [{"finding_id": "primary_claim", "outcome": "overstated",
                                     "rationale": "The measured contrast is descriptive only."}],
        }])
        self.assertEqual([item["kind"] for item in directives],
                         ["review_check_to_verify", "review_finding_to_verify"])
        self.assertIn("causal wording", directives[0]["text"])
        self.assertIn("not an independently confirmed defect", directives[0]["text"])
        self.assertIn("conflicts with its evidence", directives[0]["text"])
        self.assertIn("descriptive only", directives[1]["text"])

    def test_adverse_review_evidence_reconciles_acceptance_to_scoped_rejection(self):
        finding_ids = {"observed"}
        raw_review = {
            "reviewer_id": "claims", "decision": "accepted_with_limitations",
            "checks": [{"check_id": check, "outcome": "passed", "evidence": "Checked."}
                       for check in sorted({"method_alignment", "calculation_trace",
                                            "inference_scope", "limitation_coverage"})],
            "finding_assessments": [{"finding_id": "observed", "outcome": "overstated",
                                     "rationale": "The result does not support the claim as phrased."}],
            "limitations": ["Fixture only."],
            "finding_assessments_summary": ["Supplemental redundant model summary."],
        }
        validated_review = validate_model_review(raw_review, "claims", finding_ids)
        self.assertNotIn("finding_assessments_summary", validated_review)
        self.assertIn("finding_assessments_summary", raw_review)
        wrong_id = dict(validated_review)
        wrong_id["finding_assessments"] = [
            dict(item) for item in validated_review["finding_assessments"]
        ]
        wrong_id["finding_assessments"][0]["finding_id"] = "observed_placeholder"
        with self.assertRaisesRegex(
                ValidationError,
                r"missing=\['observed'\]; unknown=\['observed_placeholder'\].*required_ids"):
            validate_model_review(wrong_id, "claims", finding_ids)
        reconciled = reconcile_model_review_disposition(validated_review)
        self.assertEqual(reconciled["decision"], "rejected")
        self.assertEqual(reconciled["reported_decision"], "accepted_with_limitations")
        self.assertEqual(reconciled["finding_assessments"], raw_review["finding_assessments"])
        self.assertEqual(reconciled["decision_reconciliation"]["adverse_findings"],
                         [{"finding_id": "observed", "outcome": "overstated"}])

        runner = ExperimentRunner(self.root / "adverse-review-run", self.config())
        runner.worker_target = overstated_claim_fixture_worker
        result = runner.run()
        self.assertEqual(result["status"], "research_expansion_required", result)
        package_path = Path(result["results_package"])
        package = json.loads(package_path.read_text())
        validate_results_package(package, base_dir=package_path.parent)
        self.assertEqual(package["validation"]["decision"], "rejected")
        self.assertEqual(len(package["validation"]["model_review_refs"]), 2)

        control = ControlStore(runner.dir)
        try:
            store = ArtifactStore(control)
            review_record = store.get(package["validation"]["model_review_refs"][0])
            review = json.loads(store.read_body(review_record["body_hash"]))
        finally:
            control.close()
        self.assertEqual(review["decision"], "rejected")
        self.assertEqual(review["reported_decision"], "accepted_with_limitations")
        self.assertEqual(review["decision_reconciliation"]["rule"],
                         "adverse_review_evidence_requires_rejection")
        self.assertEqual(review["finding_assessments"][0]["outcome"], "overstated")

    def test_missing_analysis_is_quality_debt_after_valid_execution(self):
        config = self.config()
        config["experiment"]["quality_contract"] = {
            "minimum_conditions": 1, "minimum_independent_seeds": 1,
            "minimum_controls": 0, "minimum_comparisons": 0,
            "required_analyses": ["raw_data"], "minimum_figures": 1,
        }
        runner = ExperimentRunner(self.root / "quality-debt-run", config)
        runner.worker_target = fixture_worker
        result = runner.run()
        self.assertEqual(result["status"], "research_expansion_required", result)
        package_path = Path(result["results_package"])
        package = json.loads(package_path.read_text())
        self.assertEqual(package["quality_admission"]["decision"], "research_expansion_required")
        self.assertTrue(any(item["field"] == "analysis" for item in package["quality_admission"]["deficits"]))
        validate_results_package(package, base_dir=package_path.parent)

    def test_generated_package_detects_changed_assets(self):
        runner = ExperimentRunner(self.root / "run", self.config())
        runner.worker_target = fixture_worker
        result = runner.run()
        package_path = Path(result["results_package"])
        figure = package_path.parent / "figure.png"
        figure.write_bytes(figure.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValidationError, "unavailable or changed"):
            validate_results_package(json.loads(package_path.read_text()), base_dir=package_path.parent)


if __name__ == "__main__":
    unittest.main()
