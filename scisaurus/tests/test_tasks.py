"""T05: restart during an external call of unknown outcome records uncertainty
with conservative budget accounting — never a false zero-cost success."""

import unittest

from scisaurus.core.events import ControlStore
from scisaurus.core.tasks import TaskManager
from scisaurus.core.errors import StateError


class TestTaskLifecycle(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="scisaurus-tk-")
        self.control = ControlStore(self.dir)
        self.tm = TaskManager(self.control)

    def tearDown(self):
        self.control.close()

    def test_lifecycle_happy_path(self):
        self.tm.create("t-1", "production", {"objective": "draft section"}, "composer")
        self.tm.admit("t-1", "composer")
        fence = self.tm.start_attempt(
            "t-1", "a-1", owner="worker-1", lease_ttl_seconds=30,
            external_ref="req-1", reserved={"tokens": 100},
        )
        self.tm.finish_attempt("a-1", "succeeded", usage={"tokens": 82})
        attempt = self.tm.get_attempt("a-1")
        self.assertEqual(attempt["state"], "succeeded")
        self.assertEqual(attempt["usage"]["actual"], {"tokens": 82})
        task = self.tm.get("t-1")
        self.assertEqual(task["state"], "running")

    def test_t05_unknown_outcome_is_not_zero_cost(self):
        self.tm.create("t-2", "retrieval", {"objective": "fetch source"}, "composer")
        self.tm.admit("t-2", "composer")
        self.tm.start_attempt(
            "t-2", "a-2", owner="worker-9", lease_ttl_seconds=30,
            external_ref="provider-call-42", reserved={"usd": 0.42, "tokens": 9000},
        )
        # the runtime restarts without ever seeing the provider's outcome
        attempt = self.tm.reconcile_unknown("a-2", "scheduler")
        self.assertEqual(attempt["state"], "result_unknown")
        self.assertEqual(attempt["usage"]["reserved"], {"usd": 0.42, "tokens": 9000})
        self.assertEqual(
            attempt["usage"]["accounting"], "conservative_pending_reconciliation"
        )
        # the task cannot silently complete
        task = self.tm.get("t-2")
        self.assertEqual(task["state"], "blocked")

    def test_illegal_transition_rejected(self):
        self.tm.create("t-3", "review", {"objective": "x"}, "composer")
        with self.assertRaises(Exception):
            self.tm.transition("t-3", "completed", "composer")  # proposed -> completed illegal
        self.tm.admit("t-3", "composer")
        self.tm.transition("t-3", "blocked", "scheduler", reason="missing evidence")
        self.tm.transition("t-3", "cancelled", "composer")  # cancel from blocked allowed
        with self.assertRaises(Exception):
            self.tm.transition("t-3", "queued", "composer")  # terminal

    def test_unknown_outcome_preserves_terminal_and_paused_tasks(self):
        for target in ("cancelled", "failed", "stale", "completed", "paused"):
            with self.subTest(target=target):
                task_id, attempt_id = "t-" + target, "a-" + target
                self.tm.create(task_id, "production", {"objective": "draft"}, "composer")
                self.tm.admit(task_id, "composer")
                self.tm.start_attempt(task_id, attempt_id, owner="writer", lease_ttl_seconds=60,
                                      reserved={"tokens": 100})
                if target == "completed":
                    self.tm.transition(task_id, "awaiting_review", "scheduler")
                self.tm.transition(task_id, target, "principal")
                result = self.tm.reconcile_unknown(attempt_id, "scheduler")
                self.assertEqual(self.tm.get(task_id)["state"], target)
                self.assertEqual(result["state"], "result_unknown")
                self.assertEqual(result["usage"]["reserved"], {"tokens": 100})
                self.tm.finish_attempt(attempt_id, "succeeded", usage={"tokens": 90})
                self.assertEqual(self.tm.get(task_id)["state"], target)


if __name__ == "__main__":
    unittest.main()