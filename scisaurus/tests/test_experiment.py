"""Scientific execution contracts and an isolated end-to-end fixture."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.runtime.execution import _invoke_worker
from scisaurus.runtime.experiment import (ExperimentRunner, reconcile_model_review_disposition,
                                          validate_assessment, validate_deterministic_validation,
                                          validate_model_review, validate_program_output)
from scisaurus.runtime.experiment_config import validate_experiment_config
from scisaurus.runtime.research_quality import (
    default_research_quality_contract,
    evaluate_result_package_quality,
)
from scisaurus.runtime.results import validate_results_package
from scisaurus.core.store import ArtifactStore


def fixture_worker(kind, params, channel):
    if kind != "model":
        return _invoke_worker(kind, params, channel)
    assignment = json.loads(params["prompt"])
    if assignment["phase"] == "experiment_result_review":
        findings = assignment["program_output_summary"]["findings"]
        value = {"reviewer_id": assignment["reviewer"]["id"], "decision": "accepted",
                 "checks": [{"check_id": check, "outcome": "passed", "evidence": "Bound fixture evidence passed."}
                            for check in assignment["required_checks"]],
                 "finding_assessments": [{"finding_id": item["id"], "outcome": "supported",
                                           "rationale": "The recalculated metric supports the bounded statement."}
                                          for item in findings],
                 "limitations": ["This fixture establishes workflow behavior only."]}
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


class ExperimentTests(unittest.TestCase):
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
        with self.assertRaisesRegex(ValidationError, "all declared primary outcomes are null"):
            validate_program_output(output(None), experiment)
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

    def test_rejected_integrated_review_retains_reproducible_evidence_without_arbiter_retry(self):
        config = self.config()
        config["experiment"]["quality_contract"] = default_research_quality_contract()
        runner = ExperimentRunner(self.root / "rejected-review-run", config)
        runner.worker_target = rejecting_claims_fixture_worker
        original = runner._model_checked
        assessment_jobs = []

        def record_jobs(jobs, **kwargs):
            assessment_jobs.extend(job["name"] for job in jobs
                                   if kwargs.get("stage") == "integrated_review")
            return original(jobs, **kwargs)

        with patch.object(runner, "_model_checked", side_effect=record_jobs):
            result = runner.run()

        self.assertEqual(result["status"], "research_expansion_required", result)
        self.assertIn("repair_rejected_experiment_result",
                      {item["id"] for item in result["research_expansion_requests"]})
        self.assertEqual(result["release_status"], "not_released")
        self.assertIsNone(result["incumbent_ref"])
        self.assertNotIn("final-assessment", assessment_jobs)
        package_path = Path(result["results_package"])
        package = json.loads(package_path.read_text())
        validate_results_package(package, base_dir=package_path.parent)
        self.assertEqual(package["validation"]["decision"], "rejected")
        control = ControlStore(runner.dir)
        try:
            store = ArtifactStore(control)
            assessment_record = store.get(result["assessment_ref"])
            assessment = json.loads(store.read_body(assessment_record["body_hash"]))
        finally:
            control.close()
        self.assertEqual(assessment["accepted_findings"], [])
        self.assertIn("inference_scope=failed", assessment["summary"])
        self.assertTrue(package["findings"])
        self.assertTrue((package_path.parent / "raw-data.json").is_file())
        self.assertEqual(package["quality_admission"]["decision"], "research_expansion_required")
        repair = next(item for item in result["research_expansion_requests"]
                      if item["id"] == "repair_rejected_experiment_result")
        self.assertIn("current research direction", repair["objective"])
        self.assertIn("narrow the claim", repair["objective"])
        self.assertTrue(result["error"].startswith("experiment assessment rejected"))
        self.assertIn("repair_rejected_experiment_result",
                      {item["id"] for item in package["quality_admission"]["expansion_requests"]})
        report = package_path.parent.parent / "experiment.md"
        self.assertIn("Independent assessment: **rejected**", report.read_text())
        self.assertIn("No findings from this package are admitted as verified claims", report.read_text())
        self.assertIn("Candidate findings (not verified)", report.read_text())

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
        }
        validate_model_review(raw_review, "claims", finding_ids)
        reconciled = reconcile_model_review_disposition(raw_review)
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
