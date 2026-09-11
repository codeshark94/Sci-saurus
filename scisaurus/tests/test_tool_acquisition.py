"""Case-by-case capability acquisition through the Operations gate."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.runtime.operations import OperationsCell
from scisaurus.runtime.tool_acquisition import CapabilityAcquirer
from scisaurus.tests.test_operations import RecordedExecutor


class CapabilityAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.control = ControlStore(self.root / "project")
        self.store = ArtifactStore(self.control)
        self.store.init_project(principal_note="tools")
        self.operations = OperationsCell(self.control, self.store, project_id="tools")
        self.executor = RecordedExecutor(self.control, self.store)
        self.program = self.root / "summarize.py"
        self.program.write_text(
            "import json,sys\nvalue=json.load(sys.stdin)\n"
            "print(json.dumps({'word_count':len(value['text'].split())}))\n")

    def tearDown(self):
        self.control.close()
        self.temp.cleanup()

    def requirement(self):
        return {"id": "word_counter", "purpose": "Count words in project text",
                "adapter": "local_program", "tags": ["text", "count"],
                "data_classification": "internal", "representative": {"input": {"text": "one two"}},
                "allowed_recipe_ids": ["local_word_counter"]}

    def recipe(self, recipe_id="local_word_counter"):
        return {"id": recipe_id, "adapter": "local_program", "tags": ["text", "count", "deterministic"],
                "data_classifications": ["public", "internal"], "source": {"kind": "local"},
                "client": {"command": [sys.executable, str(self.program)], "timeout": 5,
                           "max_bytes": 10000, "cwd": "{workspace}"},
                "representative": {"input": {"text": "unused recipe sample"}},
                "environment_files": [str(self.program)]}

    def test_acquires_runs_and_independently_verifies_an_approved_project_tool(self):
        acquired = CapabilityAcquirer(self.operations).acquire(
            self.requirement(), [self.recipe()], self.executor)
        self.assertEqual(acquired["state"]["state"], "idle")
        self.assertTrue(acquired["state"]["binding"])
        self.assertEqual(len(self.executor.calls), 1)
        result, _ = self.operations.run(
            acquired["state"]["binding"], {"input": {"text": "one two three"}},
            self.executor, operator="research.worker")
        self.assertEqual(result["document"], {"word_count": 3})
        record = self.store.get(acquired["acquisition_ref"])
        body = json.loads(self.store.read_body(record["body_hash"]))
        self.assertEqual(body["domain_acceptance"], "not_assessed")
        self.assertTrue(self.control.verify_chain()[0])

    def test_rejects_ambiguous_or_unapproved_recipe_selection(self):
        with self.assertRaisesRegex(ValidationError, "one exact approved"):
            CapabilityAcquirer(self.operations).acquire(
                self.requirement(), [self.recipe("different")], self.executor)
        requirement = self.requirement()
        requirement["allowed_recipe_ids"] = ["local_word_counter", "second"]
        with self.assertRaisesRegex(ValidationError, "one exact approved"):
            CapabilityAcquirer(self.operations).acquire(
                requirement, [self.recipe(), self.recipe("second")], self.executor)

    def test_data_classification_is_an_enforced_selection_boundary(self):
        requirement = self.requirement()
        requirement["data_classification"] = "confidential"
        with self.assertRaisesRegex(ValidationError, "one exact approved"):
            CapabilityAcquirer(self.operations).acquire(
                requirement, [self.recipe()], self.executor)


if __name__ == "__main__":
    unittest.main()
