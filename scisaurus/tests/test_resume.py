"""Durable resume accounting and deadline-replanning tests."""

import json
from pathlib import Path
import tempfile
import unittest

from scisaurus.core.budget import BudgetManager
from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager
from scisaurus.runtime.resume import ResumeController, deadline_replan, source_manifest


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "scisaurus").mkdir()
        (self.root / "scisaurus" / "worker.py").write_text("VERSION = 1\n")
        self.run = self.root / "run"
        self.control = ControlStore(self.run)
        self.store = ArtifactStore(self.control); self.store.init_project(principal_note="resume")
        self.config = {"project_id": "resume", "limits": {"wall_clock_seconds": 100, "concurrent_calls": 2}}
        self.store.publish_artifact(logical_id="inputs/run-config", artifact_type="note", author="principal",
            body=canonical_bytes(self.config), media_type="application/json")
        manifest = source_manifest(self.root)
        self.store.publish_artifact(logical_id="inputs/source-manifest", artifact_type="note", author="command.controller",
            body=canonical_bytes(manifest), media_type="application/json")
        self.tasks = TaskManager(self.control); self.budget = BudgetManager(self.control)
        self.budget.open_window(window_id="run-window", policy_id="run-capacity", delegation_ref="inputs/run-config",
                                capacity={"concurrent_calls": 2})
        self.controller = ResumeController(self.control, self.store, repository_root=self.root)

    def tearDown(self):
        self.control.close(); self.temp.cleanup()

    def policy(self, **updates):
        value = {"additional_seconds": 60,
                 "unknown_outcomes": {"mode": "charge_and_retry", "usage_per_attempt": {"unknown_calls": 1}},
                 "source_changes": {"mode": "reject", "reopen_scopes": []}}
        value.update(updates); return value

    def unknown(self):
        self.tasks.create("remote", "service", {"operation": "model"}, "worker")
        self.tasks.admit("remote", "scheduler")
        self.budget.reserve(window_id="run-window", reservation_id="remote", task_id="remote",
                            amount={"concurrent_calls": 1})
        self.tasks.start_attempt("remote", "remote-attempt", owner="worker", lease_ttl_seconds=1)
        self.tasks.reconcile_unknown("remote-attempt", "command.controller")

    def test_unknown_call_is_charged_and_reservation_released_before_retry(self):
        self.unknown()
        result = self.controller.prepare(self.config, self.policy())
        self.assertEqual(len(result["unknown_reconciliations"]), 1)
        self.assertEqual(self.tasks.get_attempt("remote-attempt")["state"], "failed")
        window = self.budget.get_window("run-window")
        self.assertEqual(window["reserved"], {})
        self.assertEqual(window["cumulative_usage"], {"unknown_calls": 1})

    def test_unknown_call_blocks_without_explicit_reconciliation(self):
        self.unknown()
        policy = self.policy(); policy["unknown_outcomes"] = {"mode": "block", "usage_per_attempt": {}}
        with self.assertRaisesRegex(ValidationError, "unknown external outcomes"):
            self.controller.prepare(self.config, policy)
        self.assertEqual(self.budget.get_window("run-window")["reserved"], {"concurrent_calls": 1})

    def test_source_change_requires_named_reopened_scope(self):
        (self.root / "scisaurus" / "worker.py").write_text("VERSION = 2\n")
        with self.assertRaisesRegex(ValidationError, "source changed"):
            self.controller.prepare(self.config, self.policy())
        policy = self.policy(source_changes={"mode": "reopen", "reopen_scopes": ["production"]})
        result = self.controller.prepare(self.config, policy)
        self.assertEqual(result["source_changed_paths"], ["scisaurus/worker.py"])
        self.assertEqual(result["reopened_scopes"], ["production"])

    def test_explicit_retry_scope_is_retained_without_source_change(self):
        policy = self.policy(source_changes={"mode": "reopen", "reopen_scopes": ["operations"]})
        result = self.controller.prepare(self.config, policy)
        self.assertEqual(result["source_changed_paths"], [])
        self.assertEqual(result["reopened_scopes"], ["operations"])

    def test_new_runtime_source_file_is_also_detected(self):
        (self.root / "scisaurus" / "new_module.py").write_text("ENABLED = True\n")
        with self.assertRaisesRegex(ValidationError, "source changed"):
            self.controller.prepare(self.config, self.policy())

    def test_configuration_drift_never_silently_resumes(self):
        changed = json.loads(json.dumps(self.config)); changed["project_id"] = "other"
        with self.assertRaisesRegex(ValidationError, "exactly match"):
            self.controller.prepare(changed, self.policy())

    def test_deadline_replan_keeps_required_closure_and_defers_optional_work(self):
        plan = {"tasks": [
            {"id": "capture", "depends_on": [], "estimate_seconds": 10},
            {"id": "draft", "depends_on": ["capture"], "estimate_seconds": 20},
            {"id": "review", "depends_on": ["draft"], "estimate_seconds": 15},
            {"id": "alternative", "depends_on": ["capture"], "estimate_seconds": 40}],
            "completion": {"required_task_ids": ["review"]}}
        decision = deadline_replan(plan, ["capture"], available_seconds=40, worker_slots=2)
        self.assertEqual(decision["action"], "continue_required_scope")
        self.assertEqual(decision["selected_task_ids"], ["draft", "review"])
        self.assertEqual(decision["deferred_optional_task_ids"], ["alternative"])
        blocked = deadline_replan(plan, ["capture"], available_seconds=34, worker_slots=2)
        self.assertEqual(blocked["action"], "retain_and_pause")

    def test_deadline_replan_handles_optional_dependency_closures(self):
        plan = {"tasks": [
            {"id": "required", "depends_on": [], "estimate_seconds": 5},
            {"id": "optional_a", "depends_on": [], "estimate_seconds": 3},
            {"id": "optional_b", "depends_on": ["optional_a"], "estimate_seconds": 4}],
            "completion": {"required_task_ids": ["required"]}}
        decision = deadline_replan(plan, [], available_seconds=12, worker_slots=1)
        self.assertEqual(decision["action"], "continue")
        self.assertEqual(decision["selected_task_ids"], ["optional_a", "optional_b", "required"])


if __name__ == "__main__":
    unittest.main()
