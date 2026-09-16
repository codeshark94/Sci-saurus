"""Survey configuration boundaries and installed-runtime preparation."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.config import configured_worker_slots
from scisaurus.runtime.survey_config import load_survey_config, validate_survey_config


REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "config/survey-run.example.json"
SCRIPT = REPO / "scripts/prepare-survey-config.py"
SPEC = importlib.util.spec_from_file_location("prepare_survey_config", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def survey_config():
    value = json.loads(EXAMPLE.read_text())
    value["live_dispatch_allowed"] = True
    value["model"].update(base_url="http://127.0.0.1:1/v1", model="configuration-test-model")
    return value


class TestSurveyConfig(unittest.TestCase):
    def test_example_is_inert_and_activated_config_is_copied(self):
        with self.assertRaisesRegex(ValidationError, "explicitly true"):
            load_survey_config(EXAMPLE)
        value = survey_config()
        validated = validate_survey_config(value)
        self.assertEqual(validated, value)
        validated["survey"]["seed_queries"].append("Independent modification")
        self.assertEqual(value["survey"]["seed_queries"], ["Attention Is All You Need"])

    def test_legacy_revision_remains_valid_without_mutating_in_a_challenge_reserve(self):
        value = survey_config()
        value["survey"]["revision"] = 4
        del value["survey"]["search"]["challenge_reserve"]
        self.assertEqual(validate_survey_config(value), value)
        current = survey_config()
        del current["survey"]["search"]["challenge_reserve"]
        with self.assertRaisesRegex(ValidationError, "challenge_reserve"):
            validate_survey_config(current)

    def test_public_input_and_capacity_authorization_are_explicit(self):
        for field, invalid in (("live_dispatch_allowed", 1), ("data_classification", "private"),
                               ("allocation_mode", "unbounded")):
            with self.subTest(field=field):
                value = survey_config()
                value[field] = invalid
                with self.assertRaises(ValidationError):
                    validate_survey_config(value)

    def test_serial_provider_can_limit_worker_dispatch_without_dropping_verification_capacity(self):
        value = survey_config()
        value["limits"]["worker_concurrency"] = 1
        self.assertEqual(configured_worker_slots(validate_survey_config(value)["limits"]), 1)
        value["limits"]["worker_concurrency"] = value["limits"]["concurrent_calls"]
        with self.assertRaisesRegex(ValidationError, "worker_concurrency"):
            validate_survey_config(value)

    def test_provider_pools_register_every_explicit_role_route(self):
        value = survey_config()
        route_url = value["model"]["base_url"]
        value["model"]["role_routes"] = {
            "research.literature-mapper": [{
                "id": "local-route", "pool": "ollama", "protocol": "openai_compatible",
                "base_url": route_url, "model": "route-model", "auth_env": None,
            }]
        }
        value["limits"]["provider_pools"] = {
            "ollama": {"max_concurrent": 3, "base_urls": [route_url]},
        }
        self.assertEqual(validate_survey_config(value)["limits"]["provider_pools"]["ollama"]["max_concurrent"], 3)
        value["model"]["role_routes"]["research.literature-mapper"][0]["pool"] = "missing"
        with self.assertRaisesRegex(ValidationError, "unknown provider pool"):
            validate_survey_config(value)

    def test_unknown_and_missing_survey_fields_are_rejected(self):
        for field in ("survey", "proposed_gap", "search", "full_text_sources"):
            with self.subTest(field=field):
                value = survey_config()
                target = (value["survey"] if field == "survey" else value["survey"][field][0]
                          if field == "full_text_sources" else value["survey"][field])
                target["unrecognized"] = "unused"
                with self.assertRaises(ValidationError):
                    validate_survey_config(value)
        value = survey_config()
        del value["survey"]["question"]
        with self.assertRaises(ValidationError):
            validate_survey_config(value)

    def test_search_limits_reject_invalid_counts_and_provider_overflow(self):
        for field, amount in (("max_works", 0), ("challenge_reserve", -1),
                              ("queries_per_role", True), ("max_api_calls", 1.5),
                              ("expansion_rounds", -1), ("min_new_works", -1),
                              ("results_per_query", 101), ("max_text_chars", 1000000),
                              ("context_chars", 300001)):
            with self.subTest(field=field, amount=amount):
                value = survey_config()
                value["survey"]["search"][field] = amount
                with self.assertRaises(ValidationError):
                    validate_survey_config(value)
        value = survey_config()
        value["survey"]["seed_work_ids"].append("W123")
        value["survey"]["search"].update(max_works=2, challenge_reserve=1)
        with self.assertRaisesRegex(ValidationError, "seed works exceed the discovery"):
            validate_survey_config(value)
        value = survey_config()
        value["survey"]["search"].update(max_works=2, challenge_reserve=2)
        with self.assertRaisesRegex(ValidationError, "leave at least one"):
            validate_survey_config(value)

    def test_provider_intervals_are_explicit_and_finite(self):
        value = survey_config()
        value["survey"]["provider_intervals"] = {
            "bibliography": 1.5, "identity": 0.25, "full_text": 0,
        }
        self.assertEqual(validate_survey_config(value), value)
        for invalid in ({"bibliography": -1, "identity": 0, "full_text": 0},
                        {"bibliography": float("inf"), "identity": 0, "full_text": 0},
                        {"bibliography": 1, "identity": 0, "extra": 0}):
            with self.subTest(invalid=invalid):
                candidate = survey_config()
                candidate["survey"]["provider_intervals"] = invalid
                with self.assertRaises(ValidationError):
                    validate_survey_config(candidate)

    def test_search_seed_identity_and_unique_queries_are_required(self):
        for field, invalid in (("seed_queries", []), ("seed_queries", ["same", "same"]),
                               ("seed_queries", ["x" * 2049]), ("seed_queries", ["a\nb"]),
                               ("seed_work_ids", ["W0"]), ("seed_work_ids", ["W01"]),
                               ("seed_work_ids", ["https://openalex.org/W2626778328"]),
                               ("seed_work_ids", ["W2626778328", "W2626778328"])):
            with self.subTest(field=field, invalid=invalid):
                value = survey_config()
                value["survey"][field] = invalid
                with self.assertRaises(ValidationError):
                    validate_survey_config(value)

    def test_capabilities_cannot_share_identity_or_own_workspaces(self):
        for mutation in ("duplicate", "adapter", "cwd", "arguments", "environment"):
            with self.subTest(mutation=mutation):
                value = survey_config()
                capability = value["survey"]["full_text"]
                if mutation == "duplicate":
                    capability["id"] = value["survey"]["bibliography"]["id"]
                elif mutation == "adapter":
                    capability["adapter"] = "local_program"
                elif mutation == "cwd":
                    capability["client"]["cwd"] = "/tmp"
                elif mutation == "arguments":
                    capability["representative"]["extra"] = True
                else:
                    capability["environment_files"] = "runtime_required"
                with self.assertRaises(ValidationError):
                    validate_survey_config(value)

    def test_full_text_mapping_requires_safe_url_markers_and_capability(self):
        for mutation in ("no-capability", "duplicate", "url", "markers", "identity"):
            with self.subTest(mutation=mutation):
                value = survey_config()
                source = value["survey"]["full_text_sources"][0]
                if mutation == "no-capability":
                    value["survey"]["full_text"] = None
                elif mutation == "duplicate":
                    value["survey"]["full_text_sources"].append(deepcopy(source))
                elif mutation == "url":
                    source["url"] = "https://name:password@example.org/paper"
                elif mutation == "markers":
                    source["section_markers"] = []
                else:
                    source["work_id"] = "not-an-openalex-work"
                with self.assertRaises(ValidationError):
                    validate_survey_config(value)

    def test_metadata_only_survey_has_no_implicit_full_text_capability(self):
        value = survey_config()
        value["survey"]["full_text"] = None
        value["survey"]["full_text_sources"] = []
        self.assertEqual(validate_survey_config(value), value)

    def test_gap_nomination_can_be_deferred_without_an_implicit_claim(self):
        value = survey_config()
        value["survey"]["proposed_gap"] = None
        self.assertIsNone(validate_survey_config(value)["survey"]["proposed_gap"])

    def test_time_policy_cannot_exceed_hard_wall_limit_or_invert_targets(self):
        for field, amount in (("hard_seconds", 901), ("target_seconds", 1000), ("first_result_seconds", 721)):
            with self.subTest(field=field):
                value = survey_config()
                value["time_policy"][field] = amount
                with self.assertRaises(ValidationError):
                    validate_survey_config(value)
        value = survey_config()
        value["survey"]["stage_seconds"]["unit_review"] = 0
        with self.assertRaises(ValidationError):
            validate_survey_config(value)


class TestPrepareSurveyConfig(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-survey-preparation-test-")
        self.directory = Path(self.temp.name)
        self.output = self.directory / "prepared.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_output_or_dangling_symlink_is_never_overwritten_or_inspected(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                if symlink:
                    self.output.symlink_to(self.directory / "missing.json")
                else:
                    self.output.write_text("existing configuration")
                with patch.object(helper.project_helper, "prepare_config") as inspect, self.assertRaises(FileExistsError):
                    helper.prepare_config(self.output)
                inspect.assert_not_called()
                if not symlink:
                    self.assertEqual(self.output.read_text(), "existing configuration")
                self.output.unlink()

    def test_runtime_inspection_failure_leaves_no_partial_configuration(self):
        with patch.object(helper.project_helper, "prepare_config", side_effect=ValueError("installed runtime unavailable")), \
                self.assertRaisesRegex(ValueError, "installed runtime unavailable"):
            helper.prepare_config(self.output)
        self.assertFalse(self.output.exists())

    def test_modified_example_cannot_enable_dispatch_or_inherit_model_connection(self):
        config_directory = self.directory / "config"
        config_directory.mkdir()
        for field, value in (("live_dispatch_allowed", True), ("base_url", "http://127.0.0.1:1/v1"),
                             ("model", "configured-model"), ("auth_env", "EXAMPLE_AUTH")):
            with self.subTest(field=field):
                template = json.loads(EXAMPLE.read_text())
                target = template if field == "live_dispatch_allowed" else template["model"]
                target[field] = value
                (config_directory / "survey-run.example.json").write_text(json.dumps(template))
                with patch.object(helper.project_helper, "prepare_config") as inspect, \
                        self.assertRaisesRegex(ValueError, "dispatch disabled and model connection unset"):
                    helper.prepare_config(self.output, repo_root=self.directory)
                inspect.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_preparation_reuses_project_runtime_inventory_without_changing_survey(self):
        inventory = [str(REPO / "requirements-runtime.txt")]
        command = [str(REPO / ".venv/bin/python"), "-m", "mcp_server_fetch"]

        def prepare_project(path, *, repo_root):
            self.assertEqual(repo_root, REPO)
            Path(path).write_text(json.dumps({"mcp_fetch_command": command,
                                             "operations": {"environment_files": inventory}}))
            return {"interpreter": command[0], "versions": {"mcp": "installed-version"}}

        with patch.object(helper.project_helper, "prepare_config", side_effect=prepare_project) as inspect:
            result = helper.prepare_config(self.output)
        inspect.assert_called_once()
        prepared = json.loads(self.output.read_text())
        expected = json.loads(EXAMPLE.read_text())
        expected["survey"]["full_text"]["client"]["command"] = command
        expected["survey"]["full_text"]["environment_files"] = inventory
        self.assertEqual(prepared, expected)
        self.assertEqual(result["environment_files"], inventory)
        self.assertFalse(Path(inspect.call_args.args[0]).exists())

    @unittest.skipUnless((REPO / ".venv/bin/python").is_file(), "Installed project runtime is unavailable")
    def test_actual_runtime_pins_are_recorded_without_enabling_dispatch(self):
        completed = subprocess.run([sys.executable, str(SCRIPT), "--output", str(self.output)],
                                   text=True, capture_output=True, timeout=40)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        prepared = json.loads(self.output.read_text())
        self.assertFalse(prepared["live_dispatch_allowed"])
        self.assertEqual(prepared["model"], json.loads(EXAMPLE.read_text())["model"])
        capability = prepared["survey"]["full_text"]
        self.assertEqual(capability["client"]["command"], [str(REPO / ".venv/bin/python"), "-m", "mcp_server_fetch"])
        files = capability["environment_files"]
        for ending in ("pyvenv.cfg", "requirements-runtime.txt", "readabilipy-package-lock.json", "package-lock.json",
                       "mcp_server_fetch/server.py", "mcp/types.py", "readabilipy/simple_json.py", "ExtractArticle.js"):
            self.assertTrue(any(path.endswith(ending) for path in files), ending)
        self.assertEqual(sum(path.endswith(".dist-info/METADATA") for path in files), 3)
        self.assertTrue(all(Path(path).is_absolute() and Path(path).is_file() for path in files))
        self.assertFalse(any("__pycache__" in path for path in files))
        with self.assertRaisesRegex(ValidationError, "explicitly true"):
            load_survey_config(self.output)


if __name__ == "__main__":
    unittest.main()
