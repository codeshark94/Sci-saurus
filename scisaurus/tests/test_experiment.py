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
from scisaurus.runtime.execution import _invoke_worker
from scisaurus.runtime.experiment import (ExperimentRunner, validate_assessment,
                                          validate_deterministic_validation, validate_program_output)
from scisaurus.runtime.experiment_config import validate_experiment_config
from scisaurus.runtime.results import validate_results_package


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
            "c=x['candidate']; m=c['metrics'][0]\n"
            "v={'schema_version':'experiment-validation-1','study_id':c['study_id'],"
            "'candidate_sha256':x['candidate_sha256'],'decision':'accepted',"
            "'checks':[{'id':'row_count','outcome':'passed','evidence':'One configured row is present.'}],"
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

    def test_quality_contract_rejects_program_without_analysis_summary(self):
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
        with self.assertRaisesRegex(ValidationError, "analysis summary"):
            validate_program_output(candidate, config)

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
