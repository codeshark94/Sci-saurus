"""Receipt-bound source and realization evidence for scientific reviewers."""
import base64
from copy import deepcopy
import hashlib
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.review_evidence import review_execution_evidence, REALIZATION_REVIEW_RULE


def capture(body):
    return {"encoding": "base64", "body": base64.b64encode(body).decode(),
            "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}


class ExecutionReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source = "width = 1 - (1 - requested_fraction)**0.5\nphase = four_border_strips(width)\n"
        self.payload = {"experiment": {"id": "unit-cell"}, "configured_input": {
            "requested_fraction": .25,
            "scientific_software": {"operations": [{"source": "solid = distance_to_boundary <= width / 2",
                "output": {"phase_labels": [0, 1], "cell_weights": [3, 1]}}]}}}
        self.candidate = {"observations": [{"reported_fraction": .25,
            "phase_labels": [1, 0, 1], "cell_weights": [1, 2, 1]}]}
        stdin, stdout = canonical_bytes(self.payload), canonical_bytes(self.candidate)
        details = {"command": ["python", "/snapshot/executor.py"],
                   "env": {"PUBLIC_RUNTIME_SETTING": "value"},
                   "source_files": [{"path": "/snapshot/executor.py", "capture": capture(self.source.encode())}]}
        self.result = {"outcome": "ok", "input": deepcopy(self.payload),
                       "input_capture": capture(stdin), "input_sha256": hashlib.sha256(stdin).hexdigest(),
                       "document": deepcopy(self.candidate), "capture": capture(stdout),
                       "capture_sha256": hashlib.sha256(stdout).hexdigest(),
                       "metadata": {"process_returncode": 0, "capture_truncated": False,
                           "capture_incomplete": False, "sandbox_required": True,
                           "source_dispatch_mode": "private_read_only_snapshot", "sandbox_mode": "sandbox-exec",
                           "command_identity": {"details": details,
                               "sha256": hashlib.sha256(canonical_bytes(details)).hexdigest()}}}

    def evidence(self, result=None, payload=None, candidate=None):
        return review_execution_evidence([("artifact:command/executions/solve@1", result or self.result)],
            payload or self.payload, candidate or self.candidate, normalize_output=lambda value: value)

    def test_sources_and_upstream_masks_are_lossless_without_environment_values(self):
        evidence = self.evidence()
        self.assertEqual(evidence["executions"][0]["source_files"][0]["source"], self.source)
        self.assertEqual(evidence["configured_input"], self.payload["configured_input"])
        self.assertNotIn("env", evidence["executions"][0])
        self.assertEqual(evidence["realization_review_rule"], REALIZATION_REVIEW_RULE)
        observation = self.candidate["observations"][0]
        realized = sum(x*w for x, w in zip(observation["phase_labels"], observation["cell_weights"])) / sum(observation["cell_weights"])
        self.assertNotEqual(realized, observation["reported_fraction"])
        self.assertIn("unequal cell", REALIZATION_REVIEW_RULE)
        self.assertIn("across solvers", REALIZATION_REVIEW_RULE)

    def test_changed_source_capture_fails(self):
        value = deepcopy(self.result)
        value["metadata"]["command_identity"]["details"]["source_files"][0]["capture"]["body"] = base64.b64encode(b"fixed_source").decode()
        with self.assertRaises(ValidationError):
            self.evidence(value)

    def test_self_consistent_foreign_input_and_output_fail(self):
        payload = deepcopy(self.payload)
        payload["configured_input"]["requested_fraction"] = .5
        with self.assertRaises(ValidationError):
            self.evidence(payload=payload)
        candidate = deepcopy(self.candidate)
        candidate["observations"][0]["reported_fraction"] = .5
        with self.assertRaises(ValidationError):
            self.evidence(candidate=candidate)

    def test_changed_raw_or_parsed_output_fails(self):
        for field in ("capture", "input_capture"):
            value = deepcopy(self.result)
            value[field]["sha256"] = "0" * 64
            with self.assertRaises(ValidationError):
                self.evidence(value)
        value = deepcopy(self.result)
        value["document"] = {"observations": []}
        with self.assertRaises(ValidationError):
            self.evidence(value)

    def test_incomplete_failed_and_missing_source_receipts_fail(self):
        for field, value in (("capture_truncated", True), ("capture_incomplete", True),
                             ("process_returncode", 1), ("source_dispatch_mode", "workspace")):
            result = deepcopy(self.result)
            result["metadata"][field] = value
            with self.assertRaises(ValidationError):
                self.evidence(result)
        with self.assertRaises(ValidationError):
            review_execution_evidence([], self.payload, self.candidate, normalize_output=lambda v: v)

    def test_replay_receipts_preserve_all_source_bindings(self):
        rows = [("artifact:command/executions/first@1", self.result),
                ("artifact:command/executions/replay@1", self.result)]
        evidence = review_execution_evidence(rows, self.payload, self.candidate, normalize_output=lambda v: v)
        self.assertEqual(len(evidence["executions"]), 2)
        self.assertEqual(evidence["executions"][0]["stdout_sha256"], evidence["executions"][1]["stdout_sha256"])

