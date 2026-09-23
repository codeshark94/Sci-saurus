"""Persistent input identity, bounded retries, and independent result retention."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.composer import ComposerRunner, validate_workflow
from scisaurus.runtime.model_work import ModelWorkBlocked
from scisaurus.runtime.literature import ProviderCooldownError
from scisaurus.runtime.models import ModelCallError, ModelResult
from scisaurus.runtime.manuscript_review import DEFAULT_REVIEWERS, ManuscriptReviewRunner
from scisaurus.runtime.paper import load_paper_survey
from scisaurus.runtime.paper_pipeline import PaperPipelineRunner
from scisaurus.tests import test_composer
from scisaurus.tests import test_surveys
from scisaurus.tests.test_manuscript_review import review_for


class ModelWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workflow = test_composer.ComposerWorkflowTests()._workflow(self.root)
        self.runner = ComposerRunner(self.workflow)
        self.addCleanup(self.runner.close)
        self.stage = self.workflow["stages"][0]

    def reopen(self):
        self.runner.close()
        self.runner = ComposerRunner(self.workflow, resume=True)
        self.addCleanup(self.runner.close)

    def test_completed_stage_survives_resume_but_changed_output_is_not_reused(self):
        output = self.root / "checked.json"
        Path(self.stage["config_path"]).write_text(json.dumps({"output_path": str(output)}))
        result = {"status": "completed", "output_path": str(output), "usage": {"model_calls": 3}}
        def produce(*args, **kwargs):
            output.write_text('{"checked":true}')
            return result
        with patch.object(self.runner, "_execute_stage", side_effect=produce) as execute:
            self.assertEqual(self.runner._run_stage(self.stage)["usage"]["model_calls"], 3)
            self.assertEqual(self.runner._run_stage(self.stage, attempt_number=2)["usage"], {})
            self.assertEqual(execute.call_count, 1)
        self.reopen()
        with patch.object(self.runner, "_execute_stage", return_value=result) as execute:
            self.assertIn("reused_from", self.runner._run_stage(self.stage))
            self.assertEqual(execute.call_count, 0)
            output.write_text('{"checked":false}')
            self.runner._run_stage(self.stage)
            self.assertEqual(execute.call_count, 1)

    def test_unchanged_stage_failure_allowance_is_not_reset_on_resume(self):
        Path(self.stage["config_path"]).write_text('{"limits":{"max_rounds":2}}')
        with patch.object(self.runner, "_execute_stage", side_effect=ValidationError("invalid output")) as execute:
            with self.assertRaises(ValidationError):
                self.runner._run_stage(self.stage)
            with self.assertRaises(ModelWorkBlocked):
                self.runner._run_stage(self.stage)
            self.assertEqual(execute.call_count, 2)
        self.reopen()
        with patch.object(self.runner, "_execute_stage") as execute:
            with self.assertRaises(ModelWorkBlocked):
                self.runner._run_stage(self.stage)
            execute.assert_not_called()
            Path(self.stage["config_path"]).write_text('{"limits":{"max_rounds":2},"input":"new evidence"}')
            execute.side_effect = ValidationError("a changed assignment")
            with self.assertRaisesRegex(ValidationError, "changed assignment"):
                self.runner._run_stage(self.stage)
            self.assertEqual(execute.call_count, 1)

    def test_provider_cooldown_does_not_consume_content_repair_allowance(self):
        with patch.object(self.runner, "_execute_stage", side_effect=ProviderCooldownError("reset", retry_after_seconds=60)):
            for _ in range(5):
                with self.assertRaises(ProviderCooldownError):
                    self.runner._run_stage(self.stage)

    def test_repaired_runtime_reopens_stage_failure_without_resetting_model_work(self):
        from scisaurus.runtime.model_work import ModelWorkCache
        cache = ModelWorkCache(self.runner.store, self.runner._publish)
        source_key = cache.key(scope="survey:map-W1", role="research.literature-mapper", system="contract", prompt={"source": "exact"}, model={})
        cache.put(source_key, {"status": "succeeded", "value": {"retained": True}})
        with patch.object(self.runner, "_stage_runtime_revision", return_value="before"), \
             patch.object(self.runner, "_execute_stage", side_effect=ModelWorkBlocked("invalid runner")) as execute:
            for _ in range(2):
                with self.assertRaises(ModelWorkBlocked):
                    self.runner._run_stage(self.stage)
            self.assertEqual(execute.call_count, 1)
        with patch.object(self.runner, "_stage_runtime_revision", return_value="after"), \
             patch.object(self.runner, "_execute_stage", side_effect=ValidationError("fresh runner path")) as execute:
            with self.assertRaisesRegex(ValidationError, "fresh runner path"):
                self.runner._run_stage(self.stage)
            execute.assert_called_once()
        self.assertEqual(cache.get(source_key)["value"], {"retained": True})

    def test_serialized_provider_pause_does_not_consume_content_repair_allowance(self):
        paused = {"status": "paused", "error": "provider reset", "usage": {"model_calls": 1},
                  "failure": {"kind": "provider_cooldown", "retry_after_seconds": 2}}
        with patch.object(self.runner, "_execute_stage", return_value=paused):
            for _ in range(5):
                with self.assertRaises(ProviderCooldownError) as raised:
                    self.runner._run_stage(self.stage)
                self.assertEqual(raised.exception.retry_after_seconds, 2)
                self.assertEqual(raised.exception.usage, {"model_calls": 1})

    def test_resumed_runner_usage_is_charged_incrementally(self):
        self.assertEqual(self.runner._incremental_stage_usage(self.stage, {"cumulative_usage": {"model_calls": 3}}), {"model_calls": 3})
        self.reopen()
        self.assertEqual(self.runner._incremental_stage_usage(self.stage, {"cumulative_usage": {"model_calls": 5}}), {"model_calls": 2})
        self.assertEqual(self.runner._incremental_stage_usage(self.stage, {"cumulative_usage": {"model_calls": 5}}), {"model_calls": 0})

    def test_image_and_bound_dependency_contents_invalidate_stage_reuse(self):
        image = self.root / "plot.png"
        image.write_bytes(b"first plot")
        results = self.root / "results.json"
        results.write_text(json.dumps({"assets": [{"path": image.name}]}))
        interpretation = self.root / "interpretation.json"
        interpretation.write_text('{"explanation":"first"}')
        paper = self.root / "paper.json"
        paper.write_text(json.dumps({"interpretation_file": str(interpretation)}))
        Path(self.stage["config_path"]).write_text(json.dumps({"images": [str(image)], "paper_config_path": str(paper)}))
        self.stage["depends_on"] = ["upstream"]
        self.runner.context["upstream"] = {"results_package": str(results)}
        output = self.root / "checked.json"
        output.write_text("{}")
        with patch.object(self.runner, "_execute_stage", return_value={"status": "completed", "output_path": str(output)}) as execute:
            self.runner._run_stage(self.stage)
            self.runner._run_stage(self.stage)
            self.assertEqual(execute.call_count, 1)
            image.write_bytes(b"changed plot")
            self.runner._run_stage(self.stage)
            self.assertEqual(execute.call_count, 2)
            results.write_text('{"new_evidence":true}')
            self.runner._run_stage(self.stage)
            self.assertEqual(execute.call_count, 3)
            interpretation.write_text('{"explanation":"revised"}')
            self.runner._run_stage(self.stage)
            self.assertEqual(execute.call_count, 4)

    def test_current_exploratory_survey_can_feed_composition_but_not_release(self):
        fixture = test_surveys.TestSurveyGate()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.accept()
        assessment = fixture.assessment(state="insufficient_evidence")
        fixture.commit(assessment)
        config = {"survey_project_dir": fixture.directory.name, "survey_ref": fixture.survey,
                  "assessment_ref": assessment, "document_type": "research_paper", "references": []}
        self.assertEqual(load_paper_survey(config, require_eligible=False)["state"], "insufficient_evidence")
        with self.assertRaisesRegex(ValidationError, "experiment-eligible"):
            load_paper_survey(config)
        self.runner.workflow["progression_policy"] = "full_pass"
        self.assertTrue(self.runner._synchronize_paper_references(config)["references"])
        results = self.root / "pilot-results.json"
        results.write_text("{}")
        pipeline = PaperPipelineRunner(packet={}, model_config={}, paper_config={
            "schema_version": "paper-release-score-3", "document_type": "research_paper"},
            output_dir=self.root / "pilot-paper", draft_before_research_review=True)
        with patch("scisaurus.runtime.paper_pipeline.validate_paper_config", return_value={
                    **config, "results_package": str(results)}), \
                patch("scisaurus.runtime.paper_pipeline.validate_results_package", return_value={}), \
                patch("scisaurus.runtime.paper_pipeline.evaluate_scholarly_preflight", return_value={
                    "decision": "proceed", "profile_id": "empirical_journal", "expansion_requests": []}), \
                patch("scisaurus.runtime.paper_pipeline.validate_scholarly_preflight", side_effect=lambda value: value), \
                patch("scisaurus.runtime.paper_pipeline.evaluate_result_package_quality", return_value={
                    "decision": "proceed", "expansion_requests": []}):
            result = pipeline._research_admission({}, {"decision": "accept"})
        self.assertEqual(result["status"], "research_expansion_required")
        self.assertEqual(result["research_expansion_requests"][0]["kind"], "literature_expansion")
        self.assertEqual(result["research_expansion_requests"][0]["id"], "establish_literature_distinction")

    def test_specialist_response_survives_later_dispatch_failure(self):
        class Dispatcher:
            model_config = {"model": "fake"}
            provider_pools = {}
            provider_cooldowns = {}
            fail = True
            seen = []

            def dispatch(inner, assignments, packet, *, on_result, **kwargs):
                reports = []
                for assignment in assignments:
                    inner.seen.append(assignment["role_id"])
                    if assignment["role_id"] == "second" and inner.fail:
                        raise RuntimeError("sibling failed")
                    report = {"role_id": assignment["role_id"], "assigned_role": assignment["assigned_role"],
                              "status": "succeeded", "response": {"decision": "pass"}, "usage": {"model_calls": 1}}
                    on_result(report)
                    reports.append(report)
                return reports

        assignments = [{"role_id": role, "assigned_role": "research." + role,
                        "stage_id": "survey", "input_projection": ["evidence"]} for role in ("first", "second")]
        dispatcher = Dispatcher()
        with self.assertRaises(RuntimeError):
            self.runner._dispatch_specialist_work(dispatcher, assignments, {"evidence": "source one"})
        self.reopen()
        dispatcher.fail = False
        reports = self.runner._dispatch_specialist_work(dispatcher, assignments, {"evidence": "source one"})
        self.assertEqual(dispatcher.seen, ["first", "second", "second"])
        self.assertEqual(sum(report["usage"].get("model_calls", 0) for report in reports), 1)
        self.runner._dispatch_specialist_work(dispatcher, assignments, {"evidence": "source two"})
        self.assertEqual(dispatcher.seen[-2:], ["first", "second"])
        count = len(dispatcher.seen)
        self.runner._dispatch_specialist_work(dispatcher, assignments, {
            "evidence": "source two", "work_orders": [{"objective": "Test the competing explanation."}]})
        self.assertEqual(len(dispatcher.seen), count + 2)

    def test_exhausted_input_does_not_loop_under_until_deadline_policy(self):
        self.runner._run_stage = lambda *args, **kwargs: (_ for _ in ()).throw(ModelWorkBlocked("input needs a scoped repair"))
        result = self.runner.run()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["stages"]["survey"]["attempt_count"], 1)

    def test_progression_policy_rejects_invalid_types(self):
        for invalid in ({}, [], None, "always_accept"):
            with self.assertRaises(ValidationError):
                validate_workflow({**self.workflow, "progression_policy": invalid})

    def test_full_pass_reaches_paper_before_scoped_research_reentry(self):
        self.runner.close()
        workflow = deepcopy(self.workflow)
        workflow["project_id"] = str(self.root / "full-pass")
        workflow["progression_policy"] = "full_pass"
        workflow["continuation_policy"] = {"mode": "bounded", "max_cycles": 1}
        template = deepcopy(self.stage)
        names = [("topic", "topic_discovery"), ("survey", "survey"), ("experiment", "experiment"),
                 ("interpretation", "interpretation"), ("argument", "argument"), ("paper", "paper")]
        for name, _ in names:
            (self.root / name).mkdir(exist_ok=True)
        workflow["stages"] = [{**template, "id": name, "kind": kind,
            "project_dir": str(self.root / name), "depends_on": [names[index-1][0]] if index else []}
            for index, (name, kind) in enumerate(names)]
        workflow["completion"]["required_stage_ids"] = [name for name, _ in names]
        runner = ComposerRunner(workflow)
        self.addCleanup(runner.close)
        calls = []
        def execute(stage, **kwargs):
            calls.append(stage["id"])
            result = {"status": "completed", "kind": stage["kind"], "usage": {"model_calls": 1}}
            if stage["kind"] == "topic_discovery":
                result.update(topic={"id": "direction", "research_question": "A testable mechanism?"},
                              admission_state="provisional_for_survey")
            if stage["kind"] == "survey":
                result.update(gap_state="eligible_for_experiment", survey_current=True,
                              assessment_current=True, topic_admission="eligible_for_experiment")
                result = runner._gate_free_topic_survey(result, stage=stage)
            if stage["id"] == "paper" and calls.count("paper") == 1:
                result.update(status="research_expansion_required", research_expansion_requests=[{
                    "id": "counter-search", "kind": "literature_expansion", "owner": "research",
                    "objective": "Compare the closest competing method.", "why": "Originality is unresolved.",
                    "success_condition": "A captured comparison supports or refutes the distinction.",
                    "evidence_needed": "Captured closest prior work."}])
            output = self.root / f"result-{len(calls)}.json"
            output.write_text(json.dumps(result))
            return {**result, "output_path": str(output)}
        runner._execute_stage = execute
        result = runner.run()
        self.assertEqual(result["status"], "completed", result.get("blockers"))
        self.assertEqual(calls[:6], [name for name, _ in names])
        self.assertEqual(calls[6:], ["survey", "experiment", "interpretation", "argument", "paper"])
        self.assertEqual(calls.count("topic"), 1)

    def test_exploratory_pilot_does_not_turn_unresolved_novelty_into_eligible_gap(self):
        self.runner.workflow["progression_policy"] = "full_pass"
        topic_stage = {"id": "topic", "kind": "topic_discovery"}
        self.runner.context["topic"] = {"maturity_open_requirements": ["test mechanism"]}
        result = {"status": "completed", "gap_state": "insufficient_evidence",
                  "survey_current": True, "assessment_current": True}
        with patch.object(self.runner, "_topic_stage_for_survey", return_value=topic_stage):
            admitted = self.runner._gate_free_topic_survey(result, stage=self.stage)
        self.assertEqual(admitted["topic_admission"], "exploratory_pilot")
        self.assertEqual(admitted["gap_state"], "insufficient_evidence")
        self.assertIn("test mechanism", admitted["carried_maturity_requirements"])


class ReviewRetentionTests(unittest.TestCase):
    def test_text_only_cached_acceptance_is_not_layout_acceptance(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "page.png"
            image.write_bytes(b"rendered page")
            images = [{"path": str(image)}]
            package = {"status": "accepted", "reviews": [review_for("editorial_compression")]}
            self.assertFalse(PaperPipelineRunner._cached_review_matches_layout(package, images))
            package["layout_images_sha256"] = [hashlib.sha256(image.read_bytes()).hexdigest()]
            self.assertTrue(PaperPipelineRunner._cached_review_matches_layout(package, images))
            image.write_bytes(b"changed rendered page")
            self.assertFalse(PaperPipelineRunner._cached_review_matches_layout(package, images))

    def test_missing_render_tools_fail_before_any_argument_or_writer_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "compiler.py"
            script.write_text("raise AssertionError('must not execute')")
            runner = PaperPipelineRunner(packet={}, model_config={}, paper_config={
                "schema_version": "paper-release-score-3", "document_type": "research_paper"},
                output_dir=root / "pipeline", compile_script=script)
            with patch("scisaurus.runtime.paper.shutil.which", return_value=None), \
                    patch.object(runner, "_prepare_argument") as argument_call:
                with self.assertRaisesRegex(ValidationError, "pdfinfo.*pdftoppm"):
                    runner.run()
                argument_call.assert_not_called()

    def test_review_rate_limit_blocks_queued_calls_in_the_same_provider(self):
        runner = ManuscriptReviewRunner({"model": "fake", "base_url": "http://fixture.invalid/v1",
            "protocol": "openai_compatible", "timeout_seconds": 1, "max_output_tokens": 256},
            max_workers=1, inter_request_interval_seconds=0)
        with patch("scisaurus.runtime.manuscript_review.ModelClient") as client:
            client.return_value.complete.side_effect = ModelCallError("quota", outcome_known=True,
                                                                     status_code=429, retry_after_seconds=60)
            with self.assertRaises(ModelCallError):
                runner.run({"title": "draft", "sections": []})
            self.assertEqual(client.return_value.complete.call_count, 1)

    def test_completed_review_is_reused_across_isolated_pipeline_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = {"model": "fake"}
            reviewer = DEFAULT_REVIEWERS[0]
            first = ManuscriptReviewRunner(model, retained_work_dir=root / "retained")
            checked = (review_for("science"), ModelResult(text="", model="fake", usage={"model_calls": 1},
                                                        elapsed_seconds=0.01, finish_reason="stop"))
            with patch.object(first, "_call_review", return_value=checked):
                first._retained_review({}, reviewer, None, None, None, argument=None, evidence={},
                                       artifact_dir=root / "attempt-one")
            second = ManuscriptReviewRunner(model, retained_work_dir=root / "retained")
            with patch.object(second, "_call_review") as call:
                _, result = second._retained_review({}, reviewer, None, None, None, argument=None,
                                                   evidence={}, artifact_dir=root / "attempt-two")
                call.assert_not_called()
                self.assertEqual(result.usage, {})

    def test_layout_pages_are_all_reviewed_in_bounded_batches_and_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index in range(17):
                path = root / f"page-{index}.png"
                path.write_bytes(str(index).encode())
                paths.append({"path": str(path), "media_type": "image/png"})
            runner = ManuscriptReviewRunner({"model": "fake"})
            reviewer = next(item for item in DEFAULT_REVIEWERS if item["id"] == "editorial_compression")
            def checked(*args, **kwargs):
                return review_for(reviewer["id"]), ModelResult(text="", model="fake", usage={"model_calls": 1}, elapsed_seconds=0.01, finish_reason="stop")
            with patch.object(runner, "_call_review", side_effect=checked) as call:
                value, result = runner._retained_review({"title": "draft"}, reviewer, None, None, None,
                    argument=None, evidence={}, artifact_dir=root / "review", layout_images=paths)
                self.assertEqual([len(item.args[2]) for item in call.call_args_list], [16, 1])
                self.assertEqual(result.usage["model_calls"], 2)
                self.assertEqual(value["decision"], "accept")
                _, reused = runner._retained_review({"title": "draft"}, reviewer, None, None, None,
                    argument=None, evidence={}, artifact_dir=root / "review", layout_images=paths)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(reused.usage, {})

    def test_nonvisual_reviewer_does_not_receive_layout_images(self):
        runner = ManuscriptReviewRunner({"model": "fake"})
        reviewer = DEFAULT_REVIEWERS[0]
        with patch.object(runner, "_call_review", return_value=(review_for("science"), ModelResult(
                text="", model="fake", usage={}, elapsed_seconds=0, finish_reason="stop"))) as call:
            runner._retained_review({}, reviewer, None, None, None, argument=None, evidence={},
                                    artifact_dir=None, layout_images=[{"path": "/not/opened.png"}])
            self.assertIsNone(call.call_args.args[2])

    def test_layout_batches_fit_the_visual_model_input_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "page.png"
            path.write_bytes(b"fixture")
            runner = ManuscriptReviewRunner({"model": "fake", "max_output_tokens": 1024,
                                            "context_window_tokens": 16000, "max_input_tokens": 14000})
            reviewer = next(item for item in DEFAULT_REVIEWERS if item["id"] == "editorial_compression")
            checked = (review_for(reviewer["id"]), ModelResult(text="", model="fake", usage={"model_calls": 1},
                                                             elapsed_seconds=0.01, finish_reason="stop"))
            with patch.object(runner, "_call_review", return_value=checked) as call:
                runner._retained_review({}, reviewer, None, None, None, argument=None, evidence={},
                    artifact_dir=None, layout_images=[{"path": str(path), "media_type": "image/png"}] * 5)
                sizes = [len(item.args[2]) for item in call.call_args_list]
                self.assertEqual(sum(sizes), 5)
                self.assertLessEqual(max(sizes), 3)
