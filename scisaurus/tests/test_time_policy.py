"""Time contracts protect verification while preserving honest estimate state."""
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.time_policy import STAGES, TimePolicy, validate_time_policy


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TimePolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.seeds = {"setup": 10, "supervision": 5, "production": 20,
                      "unit_review": 8, "integrated_review": 12, "reassessment": 5}

    def plan(self, **kwargs):
        options = {"stage_seconds": self.seeds, "unit_count": 4, "worker_slots": 2,
                   "wall_clock_seconds": 300, "clock": self.clock}
        options.update(kwargs)
        return TimePolicy(**options)

    def test_deadline_is_optional_and_stage_batches_derive_target(self):
        plan = self.plan()
        snapshot = plan.snapshot()
        self.assertEqual(snapshot["initial_schedule"]["stages"], {
            "setup": 10, "supervision": 5, "production": 40,
            "unit_review": 16, "integrated_review": 12})
        self.assertEqual(snapshot["target_seconds"], 83)
        self.assertEqual(snapshot["first_result_seconds"], 83)
        self.assertEqual(snapshot["hard_seconds"], 300)
        self.assertEqual(snapshot["deadline_provenance"]["target_seconds"],
                         "stage_schedule_capped_by_hard_limit")
        self.assertEqual(snapshot["stage_estimates"]["production"]["provenance"], "configured_seed")
        self.assertIsNone(snapshot["first_verified_result"])
        self.assertNotIn("percent_complete", snapshot)

    def test_incomplete_batch_uses_a_full_worker_wave(self):
        plan = self.plan(unit_count=5)
        self.assertEqual(plan.estimate("production", 5), 60)
        self.assertEqual(plan.snapshot()["target_seconds"], 111)

    def test_impossibly_short_hard_limit_does_not_falsify_forecast(self):
        plan = self.plan(wall_clock_seconds=25)
        snapshot = plan.snapshot()
        self.assertEqual(snapshot["target_seconds"], 25)
        self.assertEqual(snapshot["initial_schedule"]["total_seconds"], 83)
        self.assertFalse(snapshot["initial_target_feasible"])
        self.assertFalse(snapshot["initial_hard_limit_feasible"])
        decision = plan.admit("production")
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["estimated_seconds"], 20)
        self.assertEqual(decision["reserved_review_seconds"], 20)
        self.assertEqual(decision["reason"], "insufficient_time_for_stage_and_review")

    def test_unit_and_integrated_review_must_fit_before_production(self):
        plan = self.plan(policy={"target_seconds": 50})
        self.clock.advance(11)
        decision = plan.admit("production")
        self.assertEqual(decision["target_remaining_seconds"], 39)
        self.assertEqual(decision["reserved_review_seconds"], 8 + 12)
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reason"], "insufficient_target_time_for_production_and_review")

    def test_pending_review_backlog_is_reserved_with_new_work(self):
        plan = self.plan()
        empty = plan.admit("production", task_count=2)
        backlog = plan.admit("production", task_count=2, pending_review_count=3)
        self.assertEqual(empty["reserved_review_seconds"], 20)
        self.assertEqual(backlog["reserved_review_seconds"], 36)

    def test_target_stops_production_revision_and_reassessment_but_not_review(self):
        plan = self.plan(policy={"target_seconds": 50, "hard_seconds": 100})
        self.clock.advance(50)
        for stage in ("production", "revision", "reassessment"):
            with self.subTest(stage=stage):
                decision = plan.admit(stage)
                self.assertFalse(decision["allowed"])
                self.assertEqual(decision["reason"], "target_reached_finish_verification")
        self.assertTrue(plan.admit("unit_review")["allowed"])
        self.assertTrue(plan.admit("integrated_review")["allowed"])

    def test_unit_review_still_protects_whole_review_near_hard_limit(self):
        plan = self.plan(policy={"hard_seconds": 100})
        self.clock.advance(85)
        decision = plan.admit("unit_review")
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["reserved_review_seconds"], 12)
        self.assertTrue(plan.admit("integrated_review")["allowed"])

    def test_hard_limit_stops_every_stage(self):
        plan = self.plan(policy={"hard_seconds": 100})
        self.clock.advance(100)
        for stage in STAGES:
            with self.subTest(stage=stage):
                decision = plan.admit(stage)
                self.assertFalse(decision["allowed"])
                self.assertEqual(decision["reason"], "hard_deadline_reached")
                self.assertEqual(decision["remaining_seconds"], 0)

    def test_observations_change_forecasts_without_moving_deadlines(self):
        plan = self.plan(policy={"target_seconds": 100})
        self.assertTrue(plan.admit("production", task_count=4)["allowed"])
        plan.observe("production", 40)
        plan.observe("production", 10)
        snapshot = plan.snapshot()
        self.assertEqual(snapshot["target_seconds"], 100)
        self.assertEqual(snapshot["initial_schedule"]["total_seconds"], 83)
        self.assertEqual(snapshot["current_schedule_estimate"]["total_seconds"], 123)
        estimate = snapshot["stage_estimates"]["production"]
        self.assertEqual(estimate["seconds"], 40)
        self.assertEqual(estimate["observations"], {"count": 2, "total_seconds": 50, "max_seconds": 40})
        self.assertEqual(estimate["provenance"], "configured_seed_and_observed_max")
        self.assertFalse(plan.admit("production", task_count=4)["allowed"])

    def test_fast_observation_does_not_erase_conservative_seed(self):
        plan = self.plan()
        plan.observe("revision", 1)
        self.assertEqual(plan.estimate("production"), 20)
        self.assertEqual(plan.snapshot()["stage_estimates"]["production"]["observations"]["count"], 1)

    def test_absent_first_result_is_distinct_from_missed_target(self):
        plan = self.plan(policy={"first_result_seconds": 30, "target_seconds": 90})
        self.assertEqual(plan.snapshot()["first_result_status"], "pending")
        self.assertFalse(plan.snapshot()["first_result_target_missed"])
        self.clock.advance(31)
        snapshot = plan.snapshot()
        self.assertIsNone(snapshot["first_verified_result"])
        self.assertEqual(snapshot["first_result_status"], "missed")
        self.assertTrue(snapshot["first_result_target_missed"])
        plan.mark_first_verified_result("artifact:document@2")
        snapshot = plan.snapshot()
        self.assertEqual(snapshot["first_result_status"], "available_late")
        self.assertEqual(snapshot["first_verified_result"],
                         {"elapsed_seconds": 31, "artifact_ref": "artifact:document@2"})
        self.assertTrue(snapshot["first_result_target_missed"])

    def test_verified_result_timestamp_is_recorded_only_once(self):
        plan = self.plan(policy={"first_result_seconds": 30, "target_seconds": 90})
        self.clock.advance(20)
        plan.mark_first_verified_result("artifact:document@2")
        self.clock.advance(50)
        plan.mark_first_verified_result("artifact:document@3")
        self.assertEqual(plan.snapshot()["first_result_status"], "available_on_time")
        self.assertEqual(plan.snapshot()["first_verified_result"]["elapsed_seconds"], 20)

    def test_retained_result_does_not_count_as_new_progress_in_a_resume_window(self):
        plan = self.plan(policy={"first_result_seconds": 30, "target_seconds": 90})
        plan.mark_retained_result("artifact:document@1")
        self.clock.advance(80)
        snapshot = plan.snapshot()
        self.assertIsNone(snapshot["first_verified_result"])
        self.assertEqual(snapshot["retained_result"], {
            "artifact_ref": "artifact:document@1", "origin": "prior_run"})
        self.assertEqual(snapshot["first_result_status"], "retained_from_prior_run")
        self.assertFalse(snapshot["first_result_target_missed"])

    def test_policy_validation_rejects_escalation_and_inconsistent_deadlines(self):
        for policy in ({"hard_seconds": 301}, {"first_result_seconds": 80, "target_seconds": 70},
                       {"target_seconds": 90, "hard_seconds": 80}, {"extra": 1}):
            with self.subTest(policy=policy), self.assertRaises(ValidationError):
                self.plan(policy=policy)

    def test_time_values_must_be_explicit_finite_numbers(self):
        for invalid in (True, "20", 0, -1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    self.plan(policy={"target_seconds": invalid})
                with self.assertRaises(ValidationError):
                    self.plan(stage_seconds={**self.seeds, "production": invalid})
        plan = self.plan()
        for invalid in (True, "20", -1, float("nan"), float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                plan.observe("production", invalid)
        self.assertEqual(plan.snapshot()["stage_estimates"]["production"]["observations"]["count"], 0)

    def test_counts_and_stage_names_are_validated(self):
        for invalid in (True, 0, -1, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                self.plan(worker_slots=invalid)
        plan = self.plan()
        for kwargs in ({"task_count": 0}, {"pending_review_count": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                plan.admit("production", **kwargs)
        with self.assertRaises(ValidationError):
            plan.admit("untracked_stage")

    def test_validation_helper_accepts_omitted_policy(self):
        self.assertIsNone(validate_time_policy(None, stage_seconds=self.seeds, unit_count=4,
                                              worker_slots=2, wall_clock_seconds=300))

    def test_snapshot_cannot_mutate_internal_estimates(self):
        plan = self.plan()
        snapshot = plan.snapshot()
        snapshot["initial_schedule"]["stages"]["production"] = -1
        snapshot["stage_estimates"]["production"]["observations"]["count"] = 50
        self.assertEqual(plan.snapshot()["initial_schedule"]["stages"]["production"], 40)
        self.assertEqual(plan.snapshot()["stage_estimates"]["production"]["observations"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
