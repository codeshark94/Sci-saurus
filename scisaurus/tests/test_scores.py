"""Score validation at the public artifact and capability contract boundary."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.project_config import validate_project_config
from scisaurus.runtime.scores import all_units, project_contract, validate_scored_config, validate_text


APPROVED_GUIDE = (
    "Artifact-gateway permits 3 total attempts with exponential backoff from 200 ms to 2000 ms and full jitter. "
    "The rate limit is 120 requests per minute with burst capacity 20. "
    "This configuration candidate has not been deployed and live behavior has not been measured.")


def scored_config(mode="pass", *, checker=None, single=False, rounds=1):
    path = Path(__file__).resolve().parents[2] / "config/operations-run.example.json"
    config = json.loads(path.read_text())
    config["live_dispatch_allowed"] = True
    config["model"].update(base_url="http://127.0.0.1:1", model=mode, timeout_seconds=8)
    config["limits"].update(wall_clock_seconds=60, checkpoint_seconds=0.03, max_rounds=rounds)
    config["score"]["stage_seconds"] = {
        "setup": 2, "supervision": 1, "production": 1,
        "unit_review": 1, "integrated_review": 1, "reassessment": 1}
    config["time_policy"] = {"first_result_seconds": 30, "target_seconds": 40, "hard_seconds": 50}
    if single:
        guide = config["deliverable"]["groups"][1]["units"][0]
        guide.update(text=APPROVED_GUIDE, editable=False, objective=None)
    if checker is None:
        config["score"].update(capabilities=[], workloads=[], candidate_checks=[])
        config["score"]["checks"] = config["score"]["checks"][:1]
    else:
        capability = config["score"]["capabilities"][0]
        capability["client"] = {"command": [sys.executable, str(checker)], "timeout": 8, "max_bytes": 100000}
        capability["environment_files"] = [str(checker)]
        capability["representative"] = {"input": {"text": "{}"}}
        baseline = config["deliverable"]["groups"][0]["units"][0]["text"]
        config["score"]["workloads"] = [{"id": "baseline-check", "capability_id": capability["id"],
                                           "arguments": {"input": {"text": baseline}}}]
        config["score"]["candidate_checks"] = [{"id": "approved-attempts", "capability_id": capability["id"],
            "unit_id": "policy", "input": {}, "text_field": "text", "result_path": ["valid"], "expected": True}]
    return config


class ScoreTests(unittest.TestCase):
    def test_one_editable_unit_requires_no_retrieval_configuration(self):
        config = scored_config(single=True)
        self.assertNotIn("public_queries", config)
        self.assertNotIn("mcp_fetch_command", config)
        self.assertIs(validate_project_config(config), config)
        score, deliverable = project_contract(config)
        self.assertEqual(sum(unit["editable"] for unit in all_units(deliverable)), 1)
        deliverable["title"] = "Independent normalized copy"
        self.assertNotEqual(deliverable["title"], config["deliverable"]["title"])
        self.assertEqual(score["capabilities"], [])

    def test_output_paths_reject_escape_reserved_names_and_file_directory_collisions(self):
        for output in ("../policy.json", "/tmp/policy.json", "run.json", "report.md", "progress.json",
                       "nested/../policy.json", "nested\\policy.json", "policy.json/child"):
            with self.subTest(output=output):
                config = scored_config()
                config["deliverable"]["output_file"] = output
                with self.assertRaises(ValidationError):
                    validate_scored_config(config)
        config = scored_config()
        config["deliverable"]["groups"][1]["units"][0]["output_file"] = "policy.json"
        with self.assertRaisesRegex(ValidationError, "duplicate"):
            validate_scored_config(config)

    def test_undeclared_capability_cannot_authorize_workload_or_candidate_execution(self):
        config = scored_config()
        config["score"]["workloads"] = [{"id": "undeclared", "capability_id": "shell", "arguments": {}}]
        with self.assertRaisesRegex(ValidationError, "undeclared capability"):
            validate_scored_config(config)
        config = scored_config()
        config["score"]["candidate_checks"] = [{"id": "undeclared", "capability_id": "shell", "unit_id": "policy",
            "input": {}, "text_field": "text", "result_path": ["valid"], "expected": True}]
        with self.assertRaisesRegex(ValidationError, "undeclared capability"):
            validate_scored_config(config)

    def test_unit_format_checks_parse_real_content_and_reject_duplicate_json_keys(self):
        for kind, text in (("json", '{"a": 1, "a": 2}'), ("json", '{"a": NaN}'),
                           ("json", '{"a":'), ("code", "def broken(:"), ("paragraph", "one\ntwo"),
                           ("code", "return 3"), ("code", "break"), ("code", "nonlocal value"),
                           ("code", "await f()")):
            with self.subTest(kind=kind, text=text), self.assertRaises(ValidationError):
                validate_text(kind, text)
        for kind, text in (("json", '{"a": [1, true]}'), ("code", "def retry():\n    return 3\n"),
                           ("list_item", "Retry at most three times.")):
            validate_text(kind, text)

    def test_foreign_claim_and_duplicate_unit_assignments_fail_before_run_creation(self):
        config = scored_config()
        config["deliverable"]["groups"][0]["units"][0]["claim_ids"] = ["foreign-claim"]
        with self.assertRaisesRegex(ValidationError, "claim binding"):
            validate_scored_config(config)
        config = scored_config()
        config["deliverable"]["groups"][1]["units"].append(deepcopy(config["deliverable"]["groups"][0]["units"][0]))
        with self.assertRaisesRegex(ValidationError, "duplicate unit"):
            validate_scored_config(config)

    def test_output_paths_reserve_case_and_unicode_equivalent_file_identities(self):
        for guide, unit in (("RUN.JSON", "policy.json"), ("POLICY.JSON", "policy.json"),
                            ("REPORT.MD/content.md", "policy.json"), ("Guide.md", "guide.MD/policy.json"),
                            ("r\u00e9sum\u00e9.md", "re\u0301sume\u0301.md")):
            with self.subTest(guide=guide, unit=unit):
                config = scored_config()
                config["deliverable"]["output_file"] = guide
                config["deliverable"]["groups"][0]["units"][0]["output_file"] = unit
                with self.assertRaises(ValidationError):
                    validate_scored_config(config)

    def test_optional_time_policy_still_respects_authorized_wall_clock_ceiling(self):
        config = scored_config()
        del config["time_policy"]
        validate_scored_config(config)
        config["time_policy"] = {"hard_seconds": config["limits"]["wall_clock_seconds"] + 1}
        with self.assertRaisesRegex(ValidationError, "cannot exceed"):
            validate_scored_config(config)

    def test_malformed_unit_and_capability_shapes_raise_validation_errors(self):
        changes = [
            lambda c: c["deliverable"]["groups"][0]["units"][0].update(kind=[]),
            lambda c: c["score"]["capabilities"][0].update(adapter=[]),
            lambda c: c["score"]["workloads"][0].update(capability_id=[]),
            lambda c: c["score"]["candidate_checks"][0].update(unit_id={}),
            lambda c: c["score"]["candidate_checks"][0].update(result_path=[-1]),
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                config = scored_config(checker=Path(__file__).resolve())
                change(config)
                with self.assertRaises(ValidationError):
                    validate_scored_config(config)


if __name__ == "__main__":
    unittest.main()
