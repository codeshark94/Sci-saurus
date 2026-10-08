"""Dispatch reservation settlement and lossless review transport."""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_foundry import review_observation_table
from scisaurus.runtime.foundry_usage import dispatch_release_count
from scisaurus.tests.test_foundry_request_history import FoundryRequestHistoryTests


class DispatchReleaseTests(unittest.TestCase):
    def states(self, status="context_not_dispatched"):
        before = {"assignment": {"question": "fixed"}, "requests": [
            {"role": "review.methods", "prompt": "frozen", "status": "started", "usage": {"model_calls": 1}}],
            "usage": {"model_calls": 3, "input_tokens": 12}}
        after = deepcopy(before)
        after["requests"][0].update(status=status, usage={"model_calls": 0}, error="not dispatched")
        if status == "context_not_dispatched":
            after["requests"][0]["context_budget"] = {"estimated_input_tokens": 200, "allowed_input_tokens": 100}
        else:
            after["requests"][0]["request_attempts"] = 0
        after["usage"]["model_calls"] = 2
        return before, after

    def test_proven_zero_dispatches_only(self):
        for status in ("context_not_dispatched", "cooldown_not_dispatched", "operator_paused_not_dispatched"):
            before, after = self.states(status)
            self.assertEqual(dispatch_release_count(before, after), 1)

    def test_unknown_real_usage_and_identity_changes_rejected(self):
        mutations = [lambda b: b["requests"][0].update(status="result_unknown"),
                     lambda b: b["requests"][0].update(prompt="different"),
                     lambda b: b["usage"].update(input_tokens=0),
                     lambda b: b["usage"].update(model_calls=0),
                     lambda b: b["usage"].update(model_calls=True),
                     lambda b: b["requests"][0]["usage"].update(model_calls=False),
                     lambda b: b["requests"][0].update(request_attempts=2),
                     lambda b: b["requests"][0].update(context_budget={}),
                     lambda b: b["requests"][0]["context_budget"].update(estimated_input_tokens=True)]
        for mutate in mutations:
            before, after = self.states()
            mutate(after)
            with self.assertRaises(ValidationError):
                dispatch_release_count(before, after)
        before, after = self.states("operator_paused_not_dispatched")
        after["requests"][0]["request_attempts"] = 1
        with self.assertRaises(ValidationError):
            dispatch_release_count(before, after)

    def test_composer_releases_only_paid_reservation_and_resume_is_idempotent(self):
        fixture = FoundryRequestHistoryTests()
        self.addCleanup(fixture.doCleanups)
        runner, _, _, _ = fixture.fixture()
        before, after = self.states()
        runner._publish("command/foundry-work/review", "note", before, "command.controller")
        runner._sync_foundry_usage()
        runner._checkpoint("experiment:capability_scientific_review", force=True)
        charged = runner.usage["model_calls"]
        runner._publish("command/foundry-work/review", "note", after, "command.controller")
        self.assertTrue(runner._sync_foundry_usage())
        self.assertEqual(runner.usage["model_calls"], charged - 1)
        self.assertFalse(runner._sync_foundry_usage())
        runner._checkpoint("released", force=True)
        from scisaurus.runtime.composer import ComposerRunner
        resumed = ComposerRunner(runner.workflow, resume=True)
        self.addCleanup(resumed.close)
        self.assertEqual(resumed.usage["model_calls"], charged - 1)
        self.assertFalse(resumed._sync_foundry_usage())

    def test_composer_rejects_unproven_decrease(self):
        fixture = FoundryRequestHistoryTests()
        self.addCleanup(fixture.doCleanups)
        runner, _, _, _ = fixture.fixture()
        before, after = self.states()
        runner._publish("command/foundry-work/review", "note", before, "command.controller")
        runner._sync_foundry_usage()
        runner._checkpoint("before", force=True)
        after["requests"][0]["status"] = "result_unknown"
        runner._publish("command/foundry-work/review", "note", after, "command.controller")
        with self.assertRaises(ValidationError):
            runner._sync_foundry_usage()

    def test_composer_rejects_unowned_release(self):
        fixture = FoundryRequestHistoryTests()
        self.addCleanup(fixture.doCleanups)
        runner, _, _, _ = fixture.fixture()
        before, after = self.states()
        runner._publish("command/foundry-work/review", "note", before, "command.controller")
        runner._sync_foundry_usage()
        runner._checkpoint("before", force=True)
        runner._publish("command/foundry-work/review", "note", after, "research.untrusted")
        with self.assertRaises(ValidationError):
            runner._sync_foundry_usage()

    def test_composer_rejects_erased_intermediate_dispatch_outcome(self):
        fixture = FoundryRequestHistoryTests()
        self.addCleanup(fixture.doCleanups)
        runner, _, _, _ = fixture.fixture()
        before, after = self.states()
        runner._publish("command/foundry-work/review", "note", before, "command.controller")
        runner._sync_foundry_usage()
        runner._checkpoint("before", force=True)
        intermediate = deepcopy(before)
        intermediate["requests"][0]["status"] = "succeeded"
        runner._publish("command/foundry-work/review", "note", intermediate, "command.controller")
        runner._publish("command/foundry-work/review", "note", after, "command.controller")
        with self.assertRaises(ValidationError):
            runner._sync_foundry_usage()

    def test_proven_recovery_resumes_same_attempt_without_scientific_order(self):
        fixture = FoundryRequestHistoryTests()
        self.addCleanup(fixture.doCleanups)
        runner, _, _, _ = fixture.fixture()
        before, after = self.states()
        stage = {"id": "experiment", "kind": "experiment"}
        record = {"status": "running", "attempt_number": 1, "attempt_id": "one",
                  "project_dir": "exact-input-owner"}
        runner.stage_records["experiment"] = record
        runner._publish("command/foundry-work/review", "note", before, "command.controller")
        runner._sync_foundry_usage()
        runner._checkpoint("paid", force=True)
        runner._publish("command/foundry-work/review", "note", after, "command.controller")
        runner._sync_foundry_usage()
        record.update(status="blocked", failure_class="operational_recovery",
                      attempts=[{"attempt_number": 1, "attempt_id": "one", "project_dir": "exact-input-owner",
                                 "failure_dossier_ref": "fixture"}])
        evidence = {"available": True, "failure_class": "operational_recovery",
                    "error": "foundry usage ledger regressed below its recorded checkpoint"}
        with patch.object(runner, "_failure_dossier_evidence", return_value=evidence):
            self.assertTrue(runner._resume_proven_dispatch_release(stage, record, set()))
            record["status"] = "blocked"
            for key in ("attempt_id", "project_dir", "attempt_number"):
                record.pop(key)
            self.assertTrue(runner._resume_proven_dispatch_release(stage, record, set()))
            record["status"] = "blocked"
            record["attempt_id"] = "unrelated"
            self.assertFalse(runner._resume_proven_dispatch_release(stage, record, set()))


class ObservationTableTests(unittest.TestCase):
    def test_lossless_missing_null_types_and_order(self):
        observations = [{"a": None, "b": True}, {"b": False}, {"a": 9007199254740993, "b": "x"}, {}]
        table = review_observation_table(observations)
        decoded = [dict(zip(table["schemas"][table["schema_ids"][i]], row)) for i, row in enumerate(table["rows"])]
        self.assertEqual(decoded, observations)
        self.assertEqual(table["observations_sha256"], hashlib.sha256(canonical_bytes(decoded)).hexdigest())
        self.assertEqual(review_observation_table([])["row_count"], 0)


class PhysicalAttemptUsageTests(unittest.TestCase):
    def test_pause_and_rate_limit_preserve_prior_physical_attempts(self):
        from scisaurus.tests.test_capability_foundry import CapabilityFoundryTests
        from scisaurus.runtime.models import ModelCallError
        from scisaurus.runtime.run_control import RunPausedError
        for kind in ("pause", "rate_limit"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fixture = CapabilityFoundryTests()
                root = Path(directory)
                foundry, cache = fixture._foundry(root), fixture._cache(root)
                if kind == "pause":
                    error = RunPausedError("paused after retries")
                    error.attempts = 2
                else:
                    error = ModelCallError("limited after retries", outcome_known=True, attempts=2,
                                           status_code=429, retry_after_seconds=30)
                error.usage = {"model_calls": 2, "input_tokens": 40, "output_tokens": 5}
                class Author:
                    def complete(self, **kwargs):
                        raise error
                with self.assertRaises(type(error)):
                    foundry.generate("bounded comparison", client=Author(), work_cache=cache)
                state = cache.entries()[0]
                self.assertEqual(state["usage"], {"model_calls": 2, "input_tokens": 40, "output_tokens": 5})
                self.assertEqual(state["requests"][-1]["status"],
                                 "result_unknown" if kind == "pause" else "provider_rate_limited")
