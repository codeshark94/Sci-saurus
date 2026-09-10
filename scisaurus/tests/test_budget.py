"""T41–T45, T52: renewable windows, causal stagnation, backpressure, completion."""

import unittest

from scisaurus.core.events import ControlStore
from scisaurus.core.budget import BudgetManager
from scisaurus.core.progress import ProgressManager, admission_decision, completion_decision
from scisaurus.core.store import ArtifactStore
from scisaurus.core.errors import ConflictError, ValidationError


class BudgetFixture(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="scisaurus-bud-")
        self.control = ControlStore(self.dir)
        self.store = ArtifactStore(self.control)
        self.store.init_project(principal_note="budget-test")
        self.budget = BudgetManager(self.control)
        self.progress = ProgressManager(self.control, self.store)
        self.window = self.budget.open_window(
            window_id="w-1",
            policy_id="pol-1",
            delegation_ref="intent:1/delegation:2",
            capacity={"tokens": 10000, "usd": 5.0},
        )

    def tearDown(self):
        self.control.close()


class TestT41Renewal(BudgetFixture):
    def test_window_renews_under_existing_delegation_with_usage_carried(self):
        self.budget.reserve(window_id="w-1", reservation_id="r-1", task_id="t-1", amount={"tokens": 6000, "usd": 1.0})
        self.budget.settle(window_id="w-1", reservation_id="r-1", actual={"tokens": 420, "usd": 0.9})
        # window ends while justified work remains — renewal needs a recorded
        # rationale, not a new human confirmation
        new_window = self.budget.renew(
            prior_window_id="w-1",
            new_window_id="w-2",
            rationale="coverage target partially met; two source classes remain",
        )
        self.assertEqual(new_window["prior_window"], "w-1")
        self.assertEqual(new_window["state"], "open")
        # cumulative usage carries over (truthful accounting across renewals)
        self.assertEqual(new_window["cumulative_usage"], {"tokens": 420, "usd": 0.9})
        # renewal cannot raise the hard limit
        with self.assertRaises(ValidationError):
            self.budget.renew(
                prior_window_id="w-2", new_window_id="w-3", rationale="more",
                capacity_delta={"tokens": 999999},
            )
        # no recorded rationale → rejected
        with self.assertRaises(ValidationError):
            self.budget.renew(prior_window_id="w-2", new_window_id="w-3", rationale="  ")


class TestT42Stagnation(BudgetFixture):
    def test_stagnation_persists_across_window_renewals(self):
        self.budget.mark_unproductive(cause_key="cause:repetitive-rewrites", window_id="w-1")
        w2 = self.budget.renew(prior_window_id="w-1", new_window_id="w-2", rationale="continue verified work")
        state = self.budget.mark_unproductive(cause_key="cause:repetitive-rewrites", window_id="w-2")
        self.assertEqual(state["observed_count"], 2)
        self.assertEqual(state["windows_spanned"], ["w-1", "w-2"])
        # a renamed task with the same causal identity is the same stagnation;
        # a genuinely different cause is tracked separately
        self.budget.mark_unproductive(cause_key="cause:repetitive-rewrites", window_id="w-2")
        self.assertEqual(self.budget.stagnation_state("cause:repetitive-rewrites")["observed_count"], 3)
        other = self.budget.mark_unproductive(cause_key="cause:missing-evidence", window_id="w-2")
        self.assertEqual(other["observed_count"], 1)


class TestT45Backpressure(BudgetFixture):
    def test_unverified_backlog_shifts_capacity_to_review(self):
        self.assertEqual(
            admission_decision(unverified_backlog=2, review_capacity_remaining=1, backlog_threshold=4),
            {"admit_new_candidates": True, "shift": "production", "reason": "backlog within threshold"},
        )
        shifted = admission_decision(unverified_backlog=5, review_capacity_remaining=1, backlog_threshold=4)
        self.assertEqual(shifted["admit_new_candidates"], False)
        self.assertEqual(shifted["shift"], "review_and_integration")
        # reservations are refused when the pool is exhausted
        self.budget.reserve(window_id="w-1", reservation_id="r-1", task_id="t-1", amount={"tokens": 6000})
        with self.assertRaises(ConflictError):
            self.budget.reserve(window_id="w-1", reservation_id="r-2", task_id="t-2", amount={"tokens": 6000})
        self.assertEqual(
            self.budget.get_window("w-1")["reserved"],
            {"tokens": 6000},
        )


class TestT52Completion(BudgetFixture):
    def test_completion_stops_discretionary_work_despite_unused_capacity(self):
        decision = completion_decision(mission_complete=True, unused_capacity=True)
        self.assertEqual(decision["action"], "propose_release_and_stop")
        # release proposed through a durable checkpoint
        checkpoint = self.progress.publish_checkpoint(
            checkpoint_id="cp-1",
            author="progress-controller",
            incumbent_ref="artifact:strategy/documents/doc-1@1",
            verified_changes=[{"kind": "artifact", "ref": "artifact:strategy/sections/s-1@2"}],
            information_changes=[],
            blockers=[],
            cumulative_usage={"tokens": 12345, "usd": 2.5},
            next_action={"decision": "complete_proposed"},
        )
        self.assertEqual(checkpoint["artifact_type"], "progress_checkpoint")
        # an incomplete mission continues
        self.assertEqual(
            completion_decision(mission_complete=False, unused_capacity=False)["action"],
            "continue",
        )

    def test_checkpoint_requires_truthful_accounting(self):
        with self.assertRaises(ValidationError):
            self.progress.publish_checkpoint(
                checkpoint_id="cp-bad",
                author="progress-controller",
                incumbent_ref=None,
                verified_changes=[],
                information_changes=[],
                blockers=[],
                cumulative_usage={},
                next_action={"decision": "continue"},
            )


if __name__ == "__main__":
    unittest.main()