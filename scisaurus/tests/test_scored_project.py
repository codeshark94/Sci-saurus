"""Score execution with simulated models and an actual local program fixture."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.documents import Documents
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.execution import _invoke_worker as original_worker
from scisaurus.runtime.project import ProjectRunner
from scisaurus.tests.test_scores import APPROVED_GUIDE, scored_config


def score_worker(kind, params, channel):
    if kind == "program":
        original_worker(kind, params, channel)
        return
    if kind != "model":
        raise AssertionError(f"Undeclared adapter was dispatched: {kind}")
    assignment = json.loads(params["prompt"])
    mode = params["client"]["model"]
    if assignment["assignment"].startswith("Identify"):
        output = {"allegation": "The policy and guide disagree with approved operating values.",
            "material_impact": "The service could use an unintended retry policy.",
            "resolution_condition": "Use the supplied approved operating values and preserve deployment uncertainty.",
            "rationale": "Configuration and guide need consistent supplied values.",
            "unit_focus": [{"unit_id": unit["id"], "change_focus": "Apply the supplied approved policy values."}
                           for unit in assignment["editable_units"]]}
    elif assignment["assignment"].startswith("Revise"):
        if assignment["output_kind"] == "json":
            policy = json.loads(assignment["baseline"])
            policy["retry"].update(max_attempts=3, strategy="exponential", base_delay_ms=200,
                                   max_delay_ms=2000, jitter="full")
            policy["rate_limit"].update(requests_per_minute=120, burst=20)
            if mode == "program-fail":
                policy["retry"]["max_attempts"] = 2
            text = json.dumps(policy, indent=2)
            if mode == "invalid-json":
                text = '{"service":'
            elif mode == "duplicate-json":
                text = '{"service":"artifact-gateway","service":"artifact-gateway"}'
        else:
            text = APPROVED_GUIDE
            if mode == "wrong-format":
                text += "\nThis line violates the declared paragraph boundary."
        output = {"unit_id": assignment["unit_id"], "baseline_unit_ref": assignment["baseline_unit_ref"],
                  "text": text, "support": []}
        if mode == "foreign-unit" and assignment["unit_id"] == "policy":
            output["unit_id"] = "unassigned-unit"
    elif assignment["assignment"].startswith("Decide"):
        output = {"decision": "revise", "rationale": "Apply a targeted correction to the failed output units.",
                  "targets": [{"unit_id": unit, "change_focus": "Resolve the recorded failed requirement."}
                              for unit in assignment["failed_units"]]}
    else:
        checks = [{"check_id": check["check_id"], "kind": check["kind"], "outcome": "passed",
                   "method": "Explicitly simulated comparison against supplied values and scope.",
                   "result": "The simulated reviewer reports the assigned requirement satisfied."}
                  for check in assignment["required_checks"]]
        if mode == "review-fail":
            checks[0].update(outcome="failed", result="Explicitly simulated unresolved objective.")
        output = {"checks": checks, "regressions": [], "uncertainties": [], "observations": [],
                  "rationale": "This deterministic model fixture reports the declared comparison outcomes."}
    channel.put({"ok": True, "result": {"text": json.dumps(output), "model": "explicit-score-simulation",
        "usage": {"model_calls": 1, "input_tokens": 10, "output_tokens": 5},
        "elapsed_seconds": 0.01, "finish_reason": "stop"}})


CHECKER = '''import json
from pathlib import Path
import sys

value = json.load(sys.stdin)
policy = json.loads(value["text"])
valid = policy.get("retry", {}).get("max_attempts") == 3
record = {"valid": valid, "text": value["text"]}
with Path(__file__).with_suffix(".calls.jsonl").open("a") as log:
    log.write(json.dumps(record) + "\\n")
print(json.dumps({"valid": valid}))
'''


class ScoredProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-score-test-")
        self.root = Path(self.temp.name)
        self.checker = self.root / "check_policy.py"
        self.checker.write_text(CHECKER)
        self.path = self.root / "project"

    def tearDown(self):
        self.temp.cleanup()

    def run_config(self, config=None, *, prepare=None, on_progress=None, name="project"):
        self.path = self.root / name
        updates = []
        def progress(value):
            updates.append(deepcopy(value))
            if on_progress:
                on_progress(runner, value)
        with patch("scisaurus.runtime.project._invoke_worker", score_worker):
            runner = ProjectRunner(self.path, config or scored_config(), on_progress=progress)
            if prepare:
                prepare(runner)
            result = runner.run()
        self.assertTrue(result["event_chain"][0], result)
        return result, updates

    def open_run(self):
        control = ControlStore(self.path)
        self.addCleanup(control.close)
        store = ArtifactStore(control)
        return control, store, Documents(control, store)

    @staticmethod
    def body(store, ref):
        return json.loads(store.read_body(store.get(ref)["body_hash"]))

    def test_generic_outputs_keep_kinds_names_and_immutable_units_without_capability_calls(self):
        result, updates = self.run_config()
        self.assertEqual(result["status"], "accepted", result["error"])
        self.assertEqual(result["capabilities"], {})
        self.assertEqual(result["source_captures"], [])
        self.assertNotIn("program_calls", result["usage"]["cumulative_usage"])
        self.assertNotIn("retrieval_calls", result["usage"]["cumulative_usage"])
        self.assertEqual(json.loads((self.path / "output/policy.json").read_text())["retry"]["max_attempts"], 3)
        guide = (self.path / "output/operations-guide.md").read_text()
        self.assertIn("```json", guide)
        self.assertIn("120 requests per minute", guide)
        self.assertFalse((self.path / "output/manuscript.md").exists())
        _, store, documents = self.open_run()
        self.assertEqual(store.versions("strategy/units/deployment-boundary"), [1])
        tree = documents.get_tree(result["incumbent_ref"])
        baseline = documents.get_tree(result["baseline_ref"])
        self.assertEqual(tree["units"][1]["children"][1], baseline["units"][1]["children"][1])
        kinds = [documents.read_unit(node["ref"])["kind"] for group in tree["units"] for node in group["children"]]
        self.assertEqual(kinds, ["json", "paragraph", "paragraph"])
        for update in updates:
            if update["phase"] != "accepted":
                self.assertIn(update["time_plan"]["first_result_status"], {"pending", "missed"})
        self.assertEqual(result["time_plan"]["first_verified_result"]["artifact_ref"], result["incumbent_ref"])
        self.assertEqual(result["time_plan"]["first_result_status"], "available_on_time")

    def test_single_editable_unit_completes_with_independent_unit_and_integrated_review(self):
        result, _ = self.run_config(scored_config(single=True))
        self.assertEqual(result["status"], "accepted", result["error"])
        control, store, _ = self.open_run()
        attempts = {row[0] for row in control._conn.execute("SELECT task_id FROM attempts")}
        self.assertEqual(attempts, {"supervise-project", "write-1-policy", "verify-1-policy", "integrated-review-1"})
        self.assertEqual(store.versions("strategy/units/operating-guide"), [1])

    def test_declared_program_really_executes_on_bad_baseline_and_exact_good_candidate(self):
        result, _ = self.run_config(scored_config(checker=self.checker))
        self.assertEqual(result["status"], "accepted", result["error"])
        calls = [json.loads(line) for line in self.checker.with_suffix(".calls.jsonl").read_text().splitlines()]
        self.assertEqual(result["usage"]["cumulative_usage"]["program_calls"], len(calls))
        self.assertEqual(len(calls), 4)
        self.assertTrue(any(not row["valid"] and "artifact-gateway" in row["text"] for row in calls))
        self.assertEqual(calls[-1], {"valid": True, "text": result["proposals"]["policy"]["text"]})
        _, store, _ = self.open_run()
        check = self.body(store, store.head("methods/program-checks/1/approved-attempts")["artifact_ref"])
        self.assertEqual(check["candidate_ref"], result["candidates"][0]["candidate_ref"])
        self.assertEqual(check["outcome"], "passed")
        self.assertTrue(check["execution_ref"])
        self.assertTrue(all(source["representation"] == "program_output" for source in result["source_captures"]))

    def test_export_preserves_verified_unit_bytes_and_passes_the_same_program_check(self):
        self.checker.write_text(CHECKER.replace(
            'valid = policy.get("retry", {}).get("max_attempts") == 3',
            'valid = policy.get("retry", {}).get("max_attempts") == 3 and not value["text"].endswith("\\n")'))
        result, _ = self.run_config(scored_config(checker=self.checker))
        self.assertEqual(result["status"], "accepted", result["error"])
        _, store, documents = self.open_run()
        check = self.body(store, store.head("methods/program-checks/1/approved-attempts")["artifact_ref"])
        self.assertEqual(check["outcome"], "passed")
        verified_text = documents.read_unit(check["unit_ref"])["text"]
        self.assertFalse(verified_text.endswith("\n"))
        exported = (self.path / "output/policy.json").read_bytes()
        execution = subprocess.run([sys.executable, str(self.checker)],
            input=json.dumps({"text": exported.decode("utf-8")}), text=True,
            capture_output=True, check=True, timeout=5)
        self.assertEqual({"exact_bytes": exported == verified_text.encode("utf-8"),
                          "checker_accepted_export": json.loads(execution.stdout)["valid"]},
                         {"exact_bytes": True, "checker_accepted_export": True})

    def test_false_program_result_blocks_atomic_adoption_despite_passing_model_reviews(self):
        config = scored_config("program-fail", checker=self.checker)
        result, _ = self.run_config(config)
        self.assertEqual(result["status"], "unresolved", result["error"])
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])
        self.assertFalse(result["candidates"][0]["adopted"])
        _, store, _ = self.open_run()
        verification = self.body(store, result["candidates"][0]["verification_ref"])
        failed = [check for check in verification["checks"] if check["outcome"] != "passed"]
        self.assertEqual([check["check_id"] for check in failed], ["program/approved-attempts"])
        for task_id in ("verify-1-policy", "verify-1-operating-guide", "integrated-review-1"):
            execution = self.body(store, store.head(f"command/executions/{task_id}")["artifact_ref"])
            self.assertTrue(all(check["outcome"] == "passed" for check in json.loads(execution["text"])["checks"]))

    def test_foreign_unit_wrong_format_and_invalid_json_proposals_never_stage(self):
        for mode, reason in (("foreign-unit", "foreign unit"), ("wrong-format", "invalid paragraph"),
                             ("invalid-json", "invalid json"), ("duplicate-json", "duplicate JSON key")):
            with self.subTest(mode=mode):
                result, _ = self.run_config(scored_config(mode), name=mode)
                self.assertEqual(result["status"], "unresolved", result["error"])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
                self.assertTrue(any(reason in error["error"] for error in result["worker_errors"]))

    def test_score_head_change_during_review_invalidates_known_passing_verdicts(self):
        changed = False
        def update_score(runner, update):
            nonlocal changed
            if not changed and "integrated-review-1" in update["active_tasks"]:
                body = self.body(runner.store, runner.score_ref)
                body["score"]["revision"] += 1
                runner.store.publish_artifact(logical_id=f"command/scores/{runner.score['id']}", artifact_type="note",
                    author="principal", body=canonical_bytes(body), media_type="application/json")
                changed = True
        result, _ = self.run_config(on_progress=update_score)
        self.assertTrue(changed)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("governing", result["error"])
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertFalse(result["candidates"][0]["adopted"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])

    def test_impossible_initial_budget_dispatches_nothing_and_clamps_real_hard_deadline(self):
        config = scored_config(checker=self.checker)
        config["time_policy"] = {"hard_seconds": 2}
        def check_deadline(runner):
            self.assertAlmostEqual(runner.deadline - runner.started, 2)
        result, _ = self.run_config(config, prepare=check_deadline)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("no external work dispatched", result["error"])
        self.assertFalse(result["time_plan"]["initial_hard_limit_feasible"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])
        self.assertFalse(self.checker.with_suffix(".calls.jsonl").exists())
        control, _, _ = self.open_run()
        self.assertEqual(control._conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 0)
        self.assertEqual(result["usage"]["cumulative_usage"], {})

    def test_remaining_review_reserve_prevents_a_new_revision_round(self):
        def consume_time(runner):
            review = runner._review
            def wrapped(*args, **kwargs):
                outcome = review(*args, **kwargs)
                runner.time_policy.clock = lambda: runner.time_policy.started_at + runner.time_policy.target_seconds - 2.5
                return outcome
            runner._review = wrapped
        result, _ = self.run_config(scored_config("review-fail", rounds=2), prepare=consume_time)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("time admission deferred reassessment", result["error"])
        decision = result["time_decisions"][-1]
        self.assertEqual(decision["stage"], "reassessment")
        self.assertGreater(decision["estimated_seconds"] + decision["reserved_review_seconds"],
                           decision["target_remaining_seconds"])
        control, _, _ = self.open_run()
        attempts = {row[0] for row in control._conn.execute("SELECT task_id FROM attempts")}
        self.assertNotIn("reassess-1", attempts)
        self.assertFalse(any(task.startswith("write-2-") for task in attempts))
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])

    def test_deadline_during_atomic_adoption_rolls_back_head_and_events(self):
        for boundary in ("_require_passing_verification", "_adopt_in"):
            with self.subTest(boundary=boundary):
                expired = []
                def expire_during_adoption(runner):
                    target = runner.changes if boundary == "_require_passing_verification" else runner.store
                    original = getattr(target, boundary)
                    def expire(*args, **kwargs):
                        result = original(*args, **kwargs)
                        if boundary != "_adopt_in" or runner.candidates:
                            expired.append(boundary)
                            runner.deadline = time.monotonic() - 1
                        return result
                    setattr(target, boundary, expire)
                result, _ = self.run_config(prepare=expire_during_adoption, name=boundary)
                self.assertEqual(expired, [boundary])
                self.assertEqual(result["status"], "blocked", result["error"])
                self.assertIn("deadline reached", result["error"])
                self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
                self.assertIsNone(result["time_plan"]["first_verified_result"])
                control, _, _ = self.open_run()
                self.assertEqual(control._conn.execute("SELECT COUNT(*) FROM events WHERE event_type='changeset.integrated'").fetchone()[0], 0)

    def test_grant_expiry_during_evidence_validation_blocks_adoption(self):
        def expire_grant(runner):
            original = runner.changes._require_passing_verification
            def expire(*args, **kwargs):
                result = original(*args, **kwargs)
                clock = patch("scisaurus.core.changes.time.time", return_value=time.time() + 1000)
                clock.start()
                self.addCleanup(clock.stop)
                return result
            runner.changes._require_passing_verification = expire
        result, _ = self.run_config(prepare=expire_grant)
        self.assertEqual(result["status"], "blocked", result["error"])
        self.assertIn("expired", result["error"])
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])

    def test_expiry_after_known_final_result_cannot_adopt_or_claim_first_verified_result(self):
        observed = []
        def expire_at_final_result(runner):
            model_jobs = runner._model_jobs
            def wrapped(jobs):
                outcomes = model_jobs(jobs)
                if jobs[0]["time_stage"] == "integrated_review":
                    observed.append(all(item["ok"] for item in outcomes.values()))
                    runner.deadline = time.monotonic() - 0.01
                    runner.time_policy.clock = lambda: runner.time_policy.started_at + runner.time_policy.hard_seconds
                return outcomes
            runner._model_jobs = wrapped
        result, _ = self.run_config(prepare=expire_at_final_result)
        self.assertEqual(observed, [True])
        self.assertEqual(result["status"], "blocked")
        self.assertIn("deadline reached", result["error"])
        self.assertEqual(result["incumbent_ref"], result["baseline_ref"])
        self.assertFalse(result["candidates"][0]["adopted"])
        self.assertIsNone(result["time_plan"]["first_verified_result"])
        self.assertEqual(result["time_plan"]["first_result_status"], "missed")
        self.assertEqual(result["usage"]["reserved"], {})


if __name__ == "__main__":
    unittest.main()
