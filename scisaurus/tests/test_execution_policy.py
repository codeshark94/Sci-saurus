import os
import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from scisaurus.cli import main
from scisaurus.runtime.execution_policy import development_execution, execution_policy


class ExecutionPolicyTests(unittest.TestCase):
    def test_development_context_restores_policy_on_failure(self):
        with patch.dict(os.environ, {"SCISAURUS_EXECUTION_POLICY": "operational"}):
            with self.assertRaises(RuntimeError):
                with development_execution(True):
                    self.assertEqual(execution_policy(), "development")
                    raise RuntimeError("interrupted")
            self.assertEqual(execution_policy(), "operational")

    def test_cli_failure_does_not_change_later_operational_run(self):
        with patch.dict(os.environ, {"SCISAURUS_EXECUTION_POLICY": "operational"}), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["run-composer", "--development", "--workflow", "/nonexistent-workflow.json"]), 2)
            self.assertEqual(execution_policy(), "operational")
            self.assertEqual(main(["run-composer", "--workflow", "/nonexistent-workflow.json"]), 2)
            self.assertEqual(execution_policy(), "operational")

    def test_nested_context_retains_explicit_environment_policy(self):
        with patch.dict(os.environ, {"SCISAURUS_EXECUTION_POLICY": "development"}):
            with development_execution(False):
                self.assertEqual(execution_policy(), "development")
            self.assertEqual(execution_policy(), "development")
