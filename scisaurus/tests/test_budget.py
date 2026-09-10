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

class TestPoolAccounting(BudgetFixture):
    def test_outstanding_reservations_and_late_settlements_survive_renewals(self):
        self.budget.reserve(window_id="w-1", reservation_id="r-1", task_id="t-1",
                            amount={"tokens": 10000})
        self.budget.renew(prior_window_id="w-1", new_window_id="w-2", rationale="continue")
        self.budget.renew(prior_window_id="w-2", new_window_id="w-3", rationale="continue")
        with self.assertRaises(ConflictError):
            self.budget.reserve(window_id="w-3", reservation_id="r-2", task_id="t-2",
                                amount={"tokens": 1})
        self.assertEqual(self.budget.get_window("w-3")["reserved"], {"tokens": 10000})
        self.budget.settle(window_id="w-1", reservation_id="r-1", actual={"tokens": 12500})
        self.control.close()
        self.control = ControlStore(self.dir)
        self.budget = BudgetManager(self.control)
        current = self.budget.get_window("w-3")
        self.assertEqual(current["cumulative_usage"], {"tokens": 12500})
        self.assertEqual(current["reserved"], {})
        self.budget.reserve(window_id="w-3", reservation_id="r-2", task_id="t-2",
                            amount={"tokens": 10000})
        from scisaurus.core.errors import StateError
        with self.assertRaises(StateError):
            self.budget.settle(window_id="w-1", reservation_id="r-1", actual={"tokens": 12500})
        self.assertEqual(self.budget.get_window("w-3")["cumulative_usage"], {"tokens": 12500})

    def test_independent_windows_share_policy_capacity(self):
        self.budget.open_window(window_id="parallel", policy_id="pol-1", delegation_ref="another",
                                capacity={"tokens": 10000, "usd": 5})
        self.budget.reserve(window_id="w-1", reservation_id="r-1", task_id="t-1", amount={"tokens": 6000})
        with self.assertRaises(ConflictError):
            self.budget.reserve(window_id="parallel", reservation_id="r-2", task_id="t-2", amount={"tokens": 6000})
        self.budget.open_window(window_id="separate", policy_id="pol-2", delegation_ref="another",
                                capacity={"tokens": 10000})
        self.budget.reserve(window_id="separate", reservation_id="r-2", task_id="t-2", amount={"tokens": 10000})
        with self.assertRaises(ValidationError):
            self.budget.open_window(window_id="escalated", policy_id="pol-1", delegation_ref="another",
                                    capacity={"tokens": 20000})

    def test_invalid_quantities_never_mutate_accounting(self):
        invalid = [-1, float("nan"), float("inf"), float("-inf"), True, "1", None]
        for value in invalid:
            with self.subTest(value=value):
                before = list(self.control.replay())
                with self.assertRaises(ValidationError):
                    self.budget.reserve(window_id="w-1", reservation_id="bad", task_id="t",
                                        amount={"tokens": value})
                with self.assertRaises(ValidationError):
                    self.budget.open_window(window_id="bad", policy_id="p-bad", delegation_ref="d",
                                            capacity={"tokens": value})
                self.assertEqual(list(self.control.replay()), before)
                self.assertEqual(self.budget.get_window("w-1")["reserved"], {})
        self.budget.reserve(window_id="w-1", reservation_id="valid", task_id="t", amount={"tokens": 10})
        for value in invalid:
            with self.assertRaises(ValidationError):
                self.budget.settle(window_id="w-1", reservation_id="valid", actual={"tokens": value})
        self.assertEqual(self.budget.get_reservation("valid")["state"], "reserved")

    def test_failed_renewal_keeps_predecessor_open_and_events_unchanged(self):
        import sqlite3
        for capacity in ({"tokens": -1}, {"tokens": float("nan")}, {"new-resource": 0}):
            before = list(self.control.replay())
            with self.assertRaises(ValidationError):
                self.budget.renew(prior_window_id="w-1", new_window_id="bad", rationale="continue",
                                  capacity_delta=capacity)
            self.assertEqual(self.budget.get_window("w-1")["state"], "open")
            self.assertEqual(list(self.control.replay()), before)
        self.budget.open_window(window_id="occupied", policy_id="pol-1", delegation_ref="d",
                                capacity={"tokens": 10})
        before = list(self.control.replay())
        with self.assertRaises(sqlite3.IntegrityError):
            self.budget.renew(prior_window_id="w-1", new_window_id="occupied", rationale="continue")
        self.assertEqual(self.budget.get_window("w-1")["state"], "open")
        self.assertEqual(list(self.control.replay()), before)
        self.budget.renew(prior_window_id="w-1", new_window_id="w-2", rationale="continue")
        self.assertEqual(self.budget.get_window("w-1")["state"], "closed")
        with self.assertRaises(ValidationError):
            self.budget.open_window(window_id="bypass", policy_id="pol-1", delegation_ref="d",
                                    capacity={"tokens": 1}, prior_window="w-2")

    def test_legacy_snapshots_recover_late_actuals_once(self):
        from scisaurus.core.schema import canonical_bytes
        self.budget.reserve(window_id="w-1", reservation_id="r-1", task_id="t", amount={"tokens": 100})
        self.budget.renew(prior_window_id="w-1", new_window_id="w-2", rationale="continue")
        self.budget.settle(window_id="w-1", reservation_id="r-1", actual={"tokens": 80})
        self.budget.reserve(window_id="w-2", reservation_id="r-2", task_id="t", amount={"tokens": 100})
        self.budget.settle(window_id="w-2", reservation_id="r-2", actual={"tokens": 90})
        with self.control.tx() as conn:
            conn.execute("DELETE FROM resource_pools")
            conn.execute("UPDATE allocation_windows SET cumulative_usage_json=? WHERE window_id='w-1'",
                         (canonical_bytes({"tokens": 80}).decode(),))
            conn.execute("UPDATE allocation_windows SET cumulative_usage_json=? WHERE window_id='w-2'",
                         (canonical_bytes({"tokens": 90}).decode(),))
        self.budget = BudgetManager(self.control)
        self.assertEqual(self.budget.get_window("w-2")["cumulative_usage"], {"tokens": 170})
        self.budget = BudgetManager(self.control)
        self.assertEqual(self.budget.get_window("w-2")["cumulative_usage"], {"tokens": 170})

    def test_concurrent_connections_cannot_overbook_shared_pool(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        self.budget.open_window(window_id="parallel", policy_id="pol-1", delegation_ref="d",
                                capacity={"tokens": 10000})
        ready = Barrier(2)
        def reserve(window_id):
            control = ControlStore(self.dir)
            try:
                budget = BudgetManager(control)
                ready.wait(timeout=5)
                try:
                    budget.reserve(window_id=window_id, reservation_id="r-" + window_id,
                                   task_id="t", amount={"tokens": 6000})
                    return "reserved"
                except ConflictError:
                    return "capacity_exhausted"
            finally:
                control.close()
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(reserve, ["w-1", "parallel"]))
        self.assertCountEqual(outcomes, ["reserved", "capacity_exhausted"])
        self.assertEqual(self.budget.get_window("w-1")["reserved"], {"tokens": 6000})

    def test_late_renewal_event_failure_rolls_back_successor_and_closure(self):
        from unittest.mock import patch

        append_event = self.control.append_event
        def fail_closure(conn, **kwargs):
            if kwargs["event_type"] == "allocation.closed":
                raise RuntimeError("event write failed")
            return append_event(conn, **kwargs)
        before = list(self.control.replay())
        with patch.object(self.control, "append_event", side_effect=fail_closure):
            with self.assertRaises(RuntimeError):
                self.budget.renew(prior_window_id="w-1", new_window_id="w-2", rationale="continue")
        self.assertEqual(self.budget.get_window("w-1")["state"], "open")
        self.assertIsNone(self.control._conn.execute(
            "SELECT 1 FROM allocation_windows WHERE window_id='w-2'"
        ).fetchone())
        self.assertEqual(list(self.control.replay()), before)


if __name__ == "__main__":
    unittest.main()