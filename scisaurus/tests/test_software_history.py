from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.software_history import engineering_history
from scisaurus.runtime.software_workbench import SoftwareWorkbench, software_prompt_results


class SoftwareHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE artifacts (artifact_ref TEXT, logical_id TEXT, version INTEGER)")
        self.store = SimpleNamespace(control=SimpleNamespace(_conn=self.db))
        self.workbench = SoftwareWorkbench(self.root, deadline=time.monotonic() + 30)
        self.topic = {"id": "design", "domain": "waves", "research_question": "Does the mechanism work?"}
        self.identity = {"topic": self.topic, "computation_scope": {"work_orders": []}}
        self.rows = {}
        self.operation = {"action": {"operation": "run", "arguments": {"runtime": "waves",
            "purpose": "scientific_computation", "source": "print('full source')", "input": {"fraction": .25}}},
            "outcome": "ok", "result": {"output": {"raw": list(range(1000))}}}
        self.ref = self.seal(self.operation)
        self.producer_ref = "producer"
        self.publish()

    def seal(self, row):
        data = canonical_bytes(row)
        sha = hashlib.sha256(data).hexdigest()
        (self.root / "receipts").mkdir(exist_ok=True)
        (self.root / "receipts" / (sha + ".json")).write_bytes(data)
        return "software:sha256:" + sha

    def publish(self, *, status="result_unknown"):
        digest = hashlib.sha256(canonical_bytes(self.identity)).hexdigest()
        logical = "command/scientific-software-assessments/" + digest + "/failure"
        ref = "artifact:" + logical + "@1"
        self.db.execute("DELETE FROM artifacts")
        self.db.execute("INSERT INTO artifacts VALUES (?, ?, 1)", (ref, logical))
        self.assessment_ref = ref
        self.rows = {ref: ({"author": "command.composer"}, "body", {
            "identity": deepcopy(self.identity), "status": "blocked", "producer_execution_ref": self.producer_ref}),
            self.producer_ref: ({}, "producer_body", {"project_id": "project", "input_ref": {
                "kind": "scientific_software_assessment", "stage_id": "experiment", "digest": digest},
                "report": {"status": status, "software_tool_results": [
                    {**deepcopy(self.operation), "receipt_ref": self.ref, "reused": True}]}})}

    def history(self):
        return engineering_history(self.store, lambda ref: deepcopy(self.rows[ref]), self.workbench,
            project_id="project", stage_id="experiment", topic=self.topic)

    def test_unknown_producer_preserves_known_operation_without_settlement(self):
        before = deepcopy(self.rows)
        with patch.object(self.workbench, "_verify_run_state", side_effect=AssertionError("no mutable admission")):
            result = self.history()
        self.assertEqual(result["assessments"][0]["producer_status"], "result_unknown")
        self.assertEqual(result["operations"][0]["outcome"], "ok")
        self.assertEqual(self.rows, before)
        self.assertNotIn("raw", json.dumps(result["operations"]))
        self.assertEqual(result["operations"][0]["receipt_ref"], self.ref)

    def test_failed_operation_remains_failed(self):
        self.operation["outcome"] = "failed"
        self.operation["error"] = "invalid geometry"
        self.ref = self.seal(self.operation)
        self.publish(status="failed")
        self.assertEqual(self.history()["operations"][0]["outcome"], "failed")

    def test_foreign_topic_question_domain_is_not_inherited(self):
        for key in self.topic:
            with self.subTest(key=key):
                self.rows[self.assessment_ref][2]["identity"]["topic"][key] = "foreign"
                self.assertEqual(self.history()["operations"], [])
                self.publish()

    def test_changed_producer_ownership_fails_closed(self):
        original = deepcopy(self.rows)
        for keys, value in [(('project_id',), 'foreign'), (('input_ref', 'stage_id'), 'foreign'),
                            (('input_ref', 'digest'), '0' * 64), (('input_ref', 'kind'), 'foreign'),
                            (('report', 'status'), 'running')]:
            with self.subTest(keys=keys):
                self.rows = deepcopy(original)
                current = self.rows[self.producer_ref][2]
                for key in keys[:-1]:
                    current = current[key]
                current[keys[-1]] = value
                with self.assertRaises(ValidationError):
                    self.history()

    def test_altered_assessment_identity_or_author_fails_closed(self):
        original = deepcopy(self.rows)
        self.rows[self.assessment_ref][2]["identity"]["changed"] = True
        with self.assertRaises(ValidationError):
            self.history()
        self.rows = deepcopy(original)
        self.rows[self.assessment_ref][0]["author"] = "foreign"
        with self.assertRaises(ValidationError):
            self.history()

    def test_altered_report_receipt_or_original_bytes_fails_closed(self):
        self.rows[self.producer_ref][2]["report"]["software_tool_results"][0]["result"] = {}
        with self.assertRaises(ValidationError):
            self.history()
        self.publish()
        (self.root / "receipts" / (self.ref.split(":")[-1] + ".json")).write_bytes(b"{}")
        with self.assertRaises(ValidationError):
            self.history()

    def test_paged_read_is_complete_and_hash_bound_without_execution(self):
        self.workbench.history_refs = frozenset({self.ref})
        text, start = "", 0
        while True:
            row = self.workbench.execute({"operation": "read_receipt", "arguments": {
                "receipt_ref": self.ref, "start": start, "max_chars": 500}})
            self.assertEqual(row["outcome"], "ok")
            page = row["result"]
            self.assertEqual(page["body_sha256"], self.ref.split(":")[-1])
            text += page["text"]
            if page["next_start"] is None:
                break
            start = page["next_start"]
        self.assertEqual(json.loads(text), self.operation)
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), self.ref.split(":")[-1])

    def test_history_files_preserve_complete_original_bytes_and_owned_scope(self):
        self.operation["result"]["output"]["raw"] = list(range(20000))
        self.operation["outcome"] = "failed"
        self.operation["error"] = "unresolved geometry"
        self.ref = self.seal(self.operation)
        foreign = self.seal({"outcome": "ok", "result": "foreign"})
        self.workbench.history_refs = frozenset({self.ref})
        with patch.object(self.workbench, "execute", side_effect=AssertionError("no dispatch")):
            files = self.workbench.history_input_files()
        index = json.loads(files["engineering-receipts.json"])
        entry, = index["entries"]
        body = files[entry["path"]]
        self.assertGreater(len(body), 32000)
        self.assertEqual(body, (self.root / "receipts" / (self.ref.split(":")[-1] + ".json")).read_bytes())
        self.assertEqual(hashlib.sha256(body).hexdigest(), entry["body_sha256"])
        self.assertEqual(len(body), entry["size_bytes"])
        self.assertEqual(json.loads(body), self.operation)
        self.assertNotIn(foreign, json.dumps(index))
        self.assertEqual(set(files), {"engineering-receipts.json", entry["path"]})

    def test_history_files_fail_on_changed_original_and_empty_scope_stays_empty(self):
        self.assertEqual(self.workbench.history_input_files(), {})
        self.workbench.history_refs = frozenset({self.ref})
        (self.root / "receipts" / (self.ref.split(":")[-1] + ".json")).write_bytes(b"{}")
        with self.assertRaisesRegex(ValidationError, "hash changed"):
            self.workbench.history_input_files()

    def test_foreign_receipt_read_and_cached_tamper_are_rejected(self):
        action = {"operation": "read_receipt", "arguments": {"receipt_ref": self.ref}}
        with self.assertRaises(ValidationError):
            self.workbench.execute(action)
        self.workbench.history_refs = frozenset({self.ref})
        self.assertEqual(self.workbench.execute(action)["outcome"], "ok")
        (self.root / "receipts" / (self.ref.split(":")[-1] + ".json")).write_bytes(b"{}")
        with self.assertRaises(ValidationError):
            self.workbench.execute(action)

    def test_history_prompt_is_index_only_and_new_receipt_remains_full(self):
        row = {**deepcopy(self.operation), "receipt_ref": self.ref}
        new = {**deepcopy(row), "receipt_ref": "new"}
        prompt = software_prompt_results([row, new], [self.ref])
        self.assertNotIn("source", prompt[0]["arguments"])
        self.assertEqual(prompt[0]["source_sha256"], hashlib.sha256(
            self.operation["action"]["arguments"]["source"].encode()).hexdigest())
        self.assertEqual(prompt[1], new)

    def test_valid_foreign_cache_receipt_cannot_replace_owned_read(self):
        other = {**deepcopy(self.operation), "action": {"operation": "read", "arguments": {"path": "private"}}}
        other_ref = self.seal(other)
        self.workbench.history_refs = frozenset({self.ref, other_ref})
        action_a = {"operation": "read_receipt", "arguments": {"receipt_ref": self.ref}}
        action_b = {"operation": "read_receipt", "arguments": {"receipt_ref": other_ref}}
        first = self.workbench.execute(action_a)
        foreign = self.workbench.execute(action_b)
        for path in (self.root / "actions").glob("*.json"):
            index = json.loads(path.read_text())
            if index.get("receipt_ref") == first["receipt_ref"]:
                index["receipt_ref"] = foreign["receipt_ref"]
                path.write_bytes(canonical_bytes(index))
        self.workbench.history_refs = frozenset({self.ref})
        with self.assertRaisesRegex(ValidationError, "another action"):
            self.workbench.execute(action_a)


class SoftwareHistorySnapshotTests(unittest.TestCase):
    def test_assignment_snapshot_is_frozen_across_resume_and_source_refs_are_owned(self):
        from scisaurus.runtime.composer import ComposerRunner
        from scisaurus.tests.test_software_discovery import CarriedAssessmentTests
        fixture = CarriedAssessmentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        runner = fixture.runner
        runner._software_author_backend_config = lambda: {"author_backend": {"model": "fixed"}, "runtime_python": "python"}
        runner.laboratory_binding = SimpleNamespace(context=lambda: {})
        runner.laboratory = {}
        saved = {}
        runner.store.head = lambda logical: saved.get(logical)

        def publish(logical, kind, body, author):
            version = saved.get(logical, {}).get("version", 0) + 1
            ref = "artifact:" + logical + "@" + str(version)
            saved[logical] = {"artifact_ref": ref, "version": version}
            fixture.rows[ref] = deepcopy(body)
            return saved[logical]

        runner._publish = publish
        authors = {}
        runner._read_verified_artifact_json = lambda ref: ({"artifact_ref": ref,
            "author": authors.get(ref, "command.composer")}, "hash", deepcopy(fixture.rows[ref]))
        inventory = {"revision": "software-engineering-history-1", "assessments": [],
                     "operations": [{"receipt_ref": "software:sha256:" + "a" * 64}]}
        with patch("scisaurus.runtime.software_history.engineering_history", return_value=inventory) as history, \
                patch("scisaurus.runtime.laboratory.laboratory_engineering_contract", return_value={}):
            for _ in range(2):
                with self.assertRaises(fixture.FreshAssessment):
                    ComposerRunner._assess_scientific_software(runner, fixture.stage, {}, fixture.topic,
                                                               computation_scope=fixture.scope)
            self.assertEqual(history.call_count, 1)
        inputs = [assignment["assignments"][0] for assignment in fixture.assignments]
        self.assertEqual(inputs[0]["_software_history_refs"], inputs[1]["_software_history_refs"])
        self.assertEqual(inputs[0]["_prompt"], inputs[1]["_prompt"])
        self.assertTrue(all(row["version"] == 1 for key, row in saved.items() if key.endswith("/request")))
        prompt = json.loads(inputs[1]["_prompt"])
        self.assertEqual(prompt["software_assessment_request"]["engineering_history"], inventory)
        self.assertNotIn("_software_history_refs", prompt)
        request_ref = next(row["artifact_ref"] for key, row in saved.items() if key.endswith("/request"))
        authors[request_ref] = "foreign"
        with patch("scisaurus.runtime.laboratory.laboratory_engineering_contract", return_value={}):
            with self.assertRaisesRegex(ValidationError, "controller ownership"):
                ComposerRunner._assess_scientific_software(runner, fixture.stage, {}, fixture.topic,
                                                           computation_scope=fixture.scope)


class SoftwareHistoryDispatcherTests(unittest.TestCase):
    def test_custom_model_retains_discovery_with_historical_selected_source(self):
        from scisaurus.tests.test_dsh_software_producer import DshSoftwareProducerTests
        from scisaurus.runtime.software_workbench import validate_selection
        fixture = DshSoftwareProducerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        directory = fixture.root / "software" / "receipts"
        directory.mkdir(parents=True)
        refs = []
        for op, result in [("inspect", {"files": []}), ("read", {"content": "source content"})]:
            row = {"action": {"operation": op, "arguments": {}}, "outcome": "ok", "result": result}
            body = canonical_bytes(row)
            sha = hashlib.sha256(body).hexdigest()
            (directory / (sha + ".json")).write_bytes(body)
            refs.append("software:sha256:" + sha)
        fixture.assignment["_software_history_refs"] = refs
        response = fixture.final_response()
        response["software_selection"].update(strategy="custom_model", scientific_source_refs=[refs[1]], model_definition=None)
        with patch("scisaurus.runtime.dsh_batch.DshBatchRunner.run", return_value=fixture.batch_result(response)):
            report = fixture.dispatcher().dispatch([fixture.assignment], {})[0]
        self.assertEqual(report["status"], "succeeded", report)
        workbench = SoftwareWorkbench(fixture.root / "software", deadline=time.monotonic() + 30)
        validate_selection(response, workbench, report["software_tool_results"])
        self.assertEqual(report["usage"]["model_calls"], 3)
        self.assertTrue(set(refs).issubset({row["receipt_ref"] for row in report["software_tool_results"]}))


if __name__ == "__main__":
    unittest.main()
