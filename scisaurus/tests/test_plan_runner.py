"""End-to-end execution of domain-independent plan tasks."""

import tempfile
import unittest

from scisaurus.core.events import ControlStore
from scisaurus.core.plans import PLAN_SCHEMA, PlanService
from scisaurus.core.schema import canonical_bytes
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.plan_runner import PlanRunner


class PlanRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.control = ControlStore(self.temp.name)
        self.store = ArtifactStore(self.control); self.store.init_project(principal_note="generic")
        brief = self.store.publish_artifact(logical_id="inputs/brief", artifact_type="note", author="principal",
            body=canonical_bytes({"request": "produce a checked answer"}), media_type="application/json")
        self.plan = {"schema_version": PLAN_SCHEMA, "id": "generic", "revision": 1,
            "objective": "Execute two dependent tasks.", "tasks": [
                {"id": "collect", "kind": "retrieval", "owner": "research.worker", "objective": "Collect evidence",
                 "depends_on": [], "input_refs": [brief["artifact_ref"]], "capability_requirements": ["search"],
                 "output_logical_ids": ["kb/evidence/generic"], "estimate_seconds": 1},
                {"id": "answer", "kind": "production", "owner": "strategy.writer", "objective": "Write the answer",
                 "depends_on": ["collect"], "input_refs": [brief["artifact_ref"]], "capability_requirements": [],
                 "output_logical_ids": ["strategy/answer/generic"], "estimate_seconds": 1}],
            "completion": {"required_task_ids": ["collect", "answer"],
                           "required_output_logical_ids": ["kb/evidence/generic", "strategy/answer/generic"],
                           "release_requires_human": False}}
        self.plan_ref = PlanService(self.control, self.store).publish(self.plan)["artifact_ref"]

    def tearDown(self):
        self.control.close(); self.temp.cleanup()

    def test_executes_dependencies_capabilities_and_independent_reviews(self):
        calls = []
        def executor(context):
            task = context["task"]
            calls.append((task["id"], context["capability_bindings"], context["dependency_result_refs"]))
            logical = task["output_logical_ids"][0]
            return {"outputs": {logical: {"artifact_type": "report", "body": {"task": task["id"]},
                                                  "media_type": "application/json"}}, "usage": {"calls": 1}}
        runner = PlanRunner(self.control, self.store, executor=executor,
            verifier=lambda context: {"author": "methods.reviewer", "checks": [{"outcome": "passed"}]},
            capability_resolver=lambda requirement: {"capability": requirement})
        result = runner.run(self.plan_ref, deadline_seconds=10)
        self.assertEqual(result["completion_state"], "completed")
        self.assertEqual(calls[0][1], {"search": {"capability": "search"}})
        self.assertTrue(calls[1][2]["collect"].startswith("artifact:command/plan-results/"))
        self.assertTrue(self.control.verify_chain()[0])

    def test_pauses_before_dispatch_when_required_closure_cannot_fit(self):
        runner = PlanRunner(self.control, self.store,
            executor=lambda _: self.fail("executor must not run"),
            verifier=lambda _: self.fail("verifier must not run"))
        result = runner.run(self.plan_ref, deadline_seconds=.5)
        self.assertEqual(result["execution_state"], "paused_deadline")
        self.assertEqual(result["deadline_decision"]["selected_task_ids"], [])


if __name__ == "__main__":
    unittest.main()
