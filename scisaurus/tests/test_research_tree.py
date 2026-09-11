"""Bounded branch search keeps alternatives and gates promotion on evidence."""

import copy
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.research_tree import (
    branch_path,
    fork,
    new_tree,
    promote,
    record_result,
    render_tree_html,
    validate_research_tree,
)


class ResearchTreeTests(unittest.TestCase):
    def setUp(self):
        self.tree = new_tree(
            "calibration-study",
            "Choose a pre-registered calibration analysis under a fixed evidence budget.",
            root_hypothesis="Calibration should be evaluated on data excluded from fitting.",
            root_plan="Enumerate bounded method branches before inspecting held-out outcomes.",
            max_depth=2,
            max_children=3,
        )

    def test_fork_is_immutable_and_paths_are_explicit(self):
        baseline = copy.deepcopy(self.tree)
        child = fork(self.tree, "root", "temperature", stage="method", 
                     hypothesis="A single temperature fitted on calibration data may reduce log loss.",
                     plan="Fit temperature on calibration data and evaluate once on test data.", seed=11,
                     input_refs=["artifact:survey/current@1"])
        self.assertEqual(self.tree, baseline)
        self.assertEqual(branch_path(child, "temperature"), ["root", "temperature"])
        self.assertEqual(child["nodes"][-1]["status"], "proposed")

    def test_verified_branch_can_be_promoted_only_with_recorded_evidence(self):
        child = fork(self.tree, "root", "temperature", stage="method",
                     hypothesis="Temperature scaling improves held-out log loss.",
                     plan="Run the frozen split plan.", seed=11)
        running = record_result(child, "temperature", status="running")
        with self.assertRaisesRegex(ValidationError, "only a verified"):
            promote(running, "temperature", evidence_refs=["artifact:result@1"])
        verified = record_result(running, "temperature", status="verified",
                                 metrics={"log_loss_delta": -0.04},
                                 evidence_refs=["artifact:result@1", "artifact:validation@1"])
        selected = promote(verified, "temperature", evidence_refs=["artifact:validation@1"])
        self.assertEqual(selected["promoted_id"], "temperature")
        self.assertEqual(selected["nodes"][1]["status"], "promoted")

    def test_alternatives_are_retained_and_invalid_structure_is_rejected(self):
        branches = self.tree
        for node_id in ("temperature", "platt", "isotonic"):
            branches = fork(branches, "root", node_id, stage="method",
                            hypothesis=f"{node_id} is a bounded candidate.",
                            plan="Run only after the branch plan is frozen.", seed=len(node_id))
        verified = record_result(branches, "platt", status="verified",
                                 metrics={"score": 0.2}, evidence_refs=["artifact:validation@1"])
        selected = promote(verified, "platt", evidence_refs=["artifact:validation@1"])
        self.assertEqual(selected["retained_alternatives"], ["isotonic", "temperature"])
        broken = copy.deepcopy(selected)
        broken["nodes"][1]["parent_id"] = "missing"
        with self.assertRaisesRegex(ValidationError, "unknown parent"):
            validate_research_tree(broken)

    def test_html_is_a_deterministic_audit_view(self):
        child = fork(self.tree, "root", "temperature", stage="method",
                     hypothesis="The calibration branch.", plan="Frozen plan.", seed=1)
        html = render_tree_html(child)
        self.assertIn("temperature", html)
        self.assertIn("Calibration", html)
        self.assertIn("<table>", html)


if __name__ == "__main__":
    unittest.main()
