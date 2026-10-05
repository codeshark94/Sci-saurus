import base64
from copy import deepcopy
import io
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.models import ModelResult
from scisaurus.runtime.program_sandbox import SandboxResult, sandbox_status
from scisaurus.runtime.software_workbench import SoftwareWorkbench, _matches_expected, project_receipt, selection_contract, validate_selection
from scisaurus.runtime.specialists import SpecialistDispatcher, VERIFIER_SYSTEM, REPAIR_EVIDENCE_SYSTEM, build_verifier_prompt


COMMIT = "a" * 40


def archive(files, *, unsafe=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as stream:
        for name, data in files.items():
            item = tarfile.TarInfo("upstream/" + name)
            body = data.encode()
            item.size = len(body)
            item.mode = 0o755 if name == "configure" else 0o644
            stream.addfile(item, io.BytesIO(body))
        if unsafe:
            item = tarfile.TarInfo("upstream/../../outside")
            stream.addfile(item, io.BytesIO())
    return output.getvalue()


class SoftwareWorkbenchTests(unittest.TestCase):
    def test_selection_shape_diagnostic_identifies_both_wrong_levels(self):
        response = selection_contract()
        response.update(decision="hold", summary="Prerequisites unresolved")
        response["software_selection"].update(strategy="unavailable", rationale="Prerequisites unresolved")
        valid = deepcopy(response)
        for key in ("strategy", "scientific_source_refs"):
            response[key] = response["software_selection"].pop(key)
        before = deepcopy(response)
        with self.assertRaises(ValidationError) as caught:
            validate_selection(response, self.workbench, [])
        diagnostic = str(caught.exception)
        self.assertIn("at /: missing fields []; unexpected fields ['scientific_source_refs', 'strategy']", diagnostic)
        self.assertIn("at /software_selection: missing fields ['scientific_source_refs', 'strategy']; unexpected fields []", diagnostic)
        self.assertEqual(response, before)
        validate_selection(valid, self.workbench, [])

    def test_selection_shape_diagnostic_rejects_nonobjects_and_missing_selection(self):
        for value, diagnostic in ((None, "at / must be an object"),
                                  ([], "at / must be an object"),
                                  ({}, "missing fields"),
                                  ({**selection_contract(), "software_selection": []}, "at /software_selection must be an object")):
            with self.subTest(value=value), self.assertRaisesRegex(ValidationError, diagnostic):
                validate_selection(value, self.workbench, [])

    def test_custom_selection_accepts_source_led_discovery_and_rejects_failed_search(self):
        response = selection_contract()
        response.update(decision="pass", summary="Declared mathematical model")
        from scisaurus.tests.test_harness_recovery import model_definition
        self.workbench.evidence_refs = frozenset({'captured-source'})
        response["software_selection"].update(strategy="custom_model", rationale="Captured mechanism and inspected alternatives", scientific_source_refs=["captured-source"], model_definition=model_definition())
        host = {"outcome": "ok", "action": {"operation": "check_environment"}}
        for operation in ("search", "search_evidence"):
            discovery = {"outcome": "ok", "action": {"operation": operation}}
            with self.subTest(operation=operation):
                validate_selection(response, self.workbench, [host, discovery])
                with self.assertRaisesRegex(ValidationError, "actual software discovery"):
                    validate_selection(response, self.workbench, [host, {**discovery, "outcome": "failed"}])
        with self.assertRaisesRegex(ValidationError, "actual software discovery"):
            validate_selection(response, self.workbench, [host])

    def test_experiment_projection_preserves_host_measurements_and_limits(self):
        from scisaurus.runtime.composer import ComposerRunner
        check = {"receipt_ref":"software:sha256:host", "outcome":"ok",
                 "action":{"operation":"check_environment"},
                 "result":{"resources":{"cpu":{"logical_count":12}},
                           "storage":{"free_bytes":12345},
                           "sandbox_limits":{"address_space_bytes":4294967296}}}
        discovery = {"outcome":"ok", "action":{"operation":"search"}, "result":{"items":[]}}
        failed = {"outcome":"failed", "action":{"operation":"check_environment"}, "error":"probe failed"}
        assessment = {"artifact_ref":"artifact:software@1", "review":{"decision":"accept"},
            "evidence":{"selection":{"strategy":"custom_model"}, "selected_operations":[],
                        "discovery_and_diagnostics":[check, discovery, failed]}}
        projected = ComposerRunner._scientific_software_projection(assessment)
        self.assertEqual(projected["host_environment_checks"], [check])
        self.assertEqual(projected["operations"], [])
        self.assertEqual(len(assessment["evidence"]["discovery_and_diagnostics"]),3)
        self.assertIn("custom model", projected["execution_contract"])
        self.assertIn("not a prerequisite", projected["execution_contract"])
        self.assertNotIn("Reuse the exact acquired", projected["execution_contract"])
        self.assertIn("independent recalculation", projected["execution_contract"])
        assessment["evidence"]["selection"]["strategy"] = "reuse"
        reuse = ComposerRunner._scientific_software_projection(assessment)
        self.assertIn("Reuse the exact acquired", reuse["execution_contract"])
        self.assertIn("New scientific parameters require", reuse["execution_contract"])
        assessment["evidence"]["selection"]["strategy"] = "unknown"
        with self.assertRaisesRegex(ValidationError, "admitted selection strategy"):
            ComposerRunner._scientific_software_projection(assessment)

    def test_inspection_uses_observed_default_branch_without_guessing(self):
        original=self.fetch
        def fetch(url,*,archive=False):
            if url.endswith('/repos/upstream/tinyprobe'):
                return json.dumps({'default_branch':'master','license':{'spdx_id':'MIT'}}).encode()
            return original(url,archive=archive)
        self.workbench.fetch=fetch
        result=self.workbench.execute({'operation':'inspect','arguments':{'repository':'upstream/tinyprobe'}})
        self.assertEqual(result['outcome'],'ok')
        self.assertEqual(result['result']['resolved_revision'],'master')
        self.assertTrue(any('/commits/master' in url for url in self.fetches))

    def test_wrong_explicit_revision_is_not_silently_changed(self):
        from urllib.error import HTTPError
        original=self.fetch
        def fetch(url,*,archive=False):
            if url.endswith('/repos/upstream/tinyprobe'):
                return json.dumps({'default_branch':'master'}).encode()
            if '/commits/main' in url:raise HTTPError(url,422,'missing revision',{},None)
            return original(url,archive=archive)
        self.workbench.fetch=fetch
        result=self.workbench.execute({'operation':'inspect','arguments':{'repository':'upstream/tinyprobe','revision':'main'}})
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('upstream default branch: master',result['diagnostic_notes'][0])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        identity = patch("scisaurus.runtime.software_workbench._runtime_identity", return_value={"fixture":"stable-runtime"})
        identity.start()
        self.addCleanup(identity.stop)
        self.files = {"LICENSE": "MIT License\n", "README.md": "Official example: {\"answer\":42}\n",
                      "DESCRIPTION": "Package: tinyprobe\nVersion: 1.0\nLicense: MIT\n",
                      "R/example.R": "answer <- function() 42\n"}
        self.fetches = []
        self.runs = []
        self.workbench = SoftwareWorkbench(self.directory.name, deadline=time.monotonic()+60,
                                           fetch=self.fetch, runner=self.runner)

    def fetch(self, url, *, archive=False):
        self.fetches.append(url)
        if archive:
            return globals()["archive"](self.files)
        if "search/repositories" in url:
            return json.dumps({"total_count": 1, "items": [{"full_name": "upstream/tinyprobe", "description": "A scientific example"}]}).encode()
        if "/commits/" in url:
            return json.dumps({"sha": COMMIT}).encode()
        if "/git/trees/" in url:
            return json.dumps({"tree": [{"path": name, "type": "blob"} for name in self.files], "truncated": False}).encode()
        if "/contents/" in url:
            name = url.split("/contents/")[1].split("?")[0]
            return json.dumps({"type": "file", "encoding": "base64", "content": base64.b64encode(self.files[name].encode()).decode()}).encode()
        return json.dumps({"license": {"spdx_id": "MIT"}, "html_url": "https://github.com/upstream/tinyprobe"}).encode()

    def runner(self, command, **kwargs):
        self.runs.append((command, kwargs))
        if "CMD" in command:
            library = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--library=")))
            (library / "tinyprobe").mkdir()
            (library / "tinyprobe" / "package").write_text("installed")
            stdout = b"installed"
        elif command[-1].endswith("inventory.R"):
            stdout = b"R 4.6.1\ntinyprobe 1.0\n"
        elif command[-1].endswith("program.R"):
            stdout = b'{"answer":42}'
        elif command[-1].endswith("check.py"):
            stdout = b'{"workspace_writable":true}'
        else:
            stdout = b"R 4.6.1"
        return SandboxResult(0, stdout, b"", False, False, "sandbox-exec")

    def action(self, operation, **arguments):
        return self.workbench.execute({"operation": operation, "arguments": arguments})

    def provision(self):
        inspected = self.action("inspect", repository="upstream/tinyprobe", revision="main")
        license = self.action("read", inspection_ref=inspected["receipt_ref"], path="LICENSE")
        docs = self.action("read", inspection_ref=inspected["receipt_ref"], path="README.md")
        with patch("scisaurus.runtime.software_workbench.shutil.which", return_value=sys.executable), \
                patch("scisaurus.runtime.software_workbench.sandbox_status", return_value={"mode":"sandbox-exec"}):
            installed = self.action("acquire", inspection_ref=inspected["receipt_ref"], license_ref=license["receipt_ref"],
                                    runtime="r", requirements=[], dependencies=[], package_path=".")
        self.assertEqual(installed["outcome"], "ok", installed)
        return inspected, license, docs, installed

    def test_discovery_install_example_and_computation_are_actual_receipts(self):
        searched = self.action("search", query="scientific tinyprobe")
        self.assertEqual(searched["result"]["repositories"][0]["full_name"], "upstream/tinyprobe")
        self.assertEqual(searched["result"]["search_contract"]["documentation_qualifier"],"in:readme")
        self.assertEqual(searched["result"]["query"],"scientific tinyprobe")
        inspected, license, docs, installed = self.provision()
        self.assertEqual(installed["result"]["commit"], COMMIT)
        with patch("scisaurus.runtime.software_workbench.sandbox_status", return_value={"mode":"sandbox-exec"}):
            result = self.action("run", environment_ref=installed["receipt_ref"], source='cat("{\\\"answer\\\":42}")',
                                 input={"parameter": 1}, purpose="upstream_example", documentation_refs=[docs["receipt_ref"]],
                                 expected={"value":{"answer":42},"absolute_tolerance":0,"relative_tolerance":0})
        self.assertEqual(result["outcome"], "ok", result)
        self.assertTrue(result["result"]["expected_matches"])
        self.assertEqual(result["result"]["scientific_admission"], "not_assessed")
        self.assertFalse(self.runs[-1][1]["allow_network"])
        self.assertEqual(self.runs[-1][1]["input_bytes"], b'{"parameter":1}')

    def test_pinned_action_reuses_without_installing_again(self):
        _, _, _, installed = self.provision()
        before = len(self.runs)
        repeated = self.workbench.execute(installed["action"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["receipt_ref"], installed["receipt_ref"])

    def test_example_without_comparison_is_rejected_before_sandbox(self):
        _,_,docs,installed=self.provision()
        before=len(self.runs)
        result=self.action('run',environment_ref=installed['receipt_ref'],source='cat("{}")',input={},
            purpose='upstream_example',documentation_refs=[docs['receipt_ref']],expected=None)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('requires a documented expected output',result['error'])
        self.assertEqual(len(self.runs),before)

    def test_example_mismatch_is_failed_with_complete_observed_result(self):
        _,_,docs,installed=self.provision()
        result=self.action('run',environment_ref=installed['receipt_ref'],source='cat("{}")',input={'parameter':1},
            purpose='upstream_example',documentation_refs=[docs['receipt_ref']],
            expected={'value':{'answer':43},'absolute_tolerance':0,'relative_tolerance':0})
        self.assertEqual(result['outcome'],'failed')
        self.assertIn('does not match',result['error'])
        observed=result['execution']
        self.assertEqual(observed['output'],{'answer':42})
        self.assertEqual(observed['expected']['value'],{'answer':43})
        self.assertFalse(observed['expected_matches'])
        self.assertEqual(observed['execution']['returncode'],0)
        self.assertEqual(observed['input'],{'parameter':1})
        self.assertEqual(observed['scientific_admission'],'not_assessed')
        again=self.workbench.execute(result['action'])
        self.assertTrue(again['reused'])
        self.assertEqual(again['receipt_ref'],result['receipt_ref'])

    def test_previous_reproduction_semantics_do_not_reuse_successful_mismatch(self):
        _,_,docs,installed=self.provision()
        action={'operation':'run','arguments':{
            'environment_ref':installed['receipt_ref'],'source':'cat("{}")','input':{},
            'purpose':'upstream_example','documentation_refs':[docs['receipt_ref']],
            'expected':{'value':{'answer':43},'absolute_tolerance':0,'relative_tolerance':0}}}
        old={'revision':'scientific-software-tools-4','action':action,'outcome':'ok',
             'result':{'expected_matches':False}}
        raw=canonical_bytes(old)
        sha=hashlib.sha256(raw).hexdigest()
        (self.workbench.root/'receipts'/(sha+'.json')).write_bytes(raw)
        old_key=hashlib.sha256(canonical_bytes({'revision':old['revision'],'action':action})).hexdigest()
        (self.workbench.root/'actions'/(old_key+'.json')).write_bytes(canonical_bytes(
            {'status':'finished','receipt_ref':'software:sha256:'+sha}))
        before=len(self.runs)
        current=self.workbench.execute(action)
        self.assertEqual(current['outcome'],'failed')
        self.assertFalse(current['reused'])
        self.assertFalse(current['execution']['expected_matches'])
        self.assertEqual(len(self.runs),before+1)
        self.assertEqual((self.workbench.root/'receipts'/(sha+'.json')).read_bytes(),raw)

    def test_wrong_documentation_receipt_identifies_exact_reference_and_operation(self):
        _,_,_,installed=self.provision()
        before=len(self.runs)
        result=self.action('run',environment_ref=installed['receipt_ref'],source='cat("{}")',input={},
            purpose='scientific_computation',documentation_refs=[installed['receipt_ref']],expected=None)
        self.assertEqual(result['outcome'],'failed')
        self.assertIn(installed['receipt_ref'],result['error'])
        self.assertIn('records operation acquire; operation read is required',result['error'])
        self.assertEqual(len(self.runs),before)

    def test_environment_resource_inventory_keeps_optional_runtime_failure(self):
        old=self.workbench.runner
        def runner(command,**kwargs):
            if command[-1].endswith("check.R"):
                return SandboxResult(1,b"",b"R runtime dependency missing",False,False,"sandbox-exec")
            return old(command,**kwargs)
        self.workbench.runner=runner
        with patch("scisaurus.runtime.software_workbench.shutil.which",return_value=sys.executable), \
                patch("scisaurus.runtime.software_workbench.sandbox_status",return_value={"mode":"sandbox-exec"}):
            row=self.action("check_environment")
        self.assertEqual(row["outcome"],"ok",row)
        self.assertGreater(row["result"]["resources"]["cpu"]["logical_count"],0)
        self.assertGreater(row["result"]["storage"]["total_bytes"],0)
        self.assertEqual(row["result"]["r"]["status"],"unavailable")
        self.assertIn("dependency missing",row["result"]["r"]["execution"]["stderr"])
        self.assertEqual(row["result"]["sandbox_limits"]["requested_posix"]["address_space_bytes"],4*1024**3)
        self.assertIsNone(row["result"]["sandbox_limits"]["observed_python_posix"])

    def test_optional_runtime_startup_error_keeps_successful_python_probe(self):
        old=self.workbench.runner
        def runner(command,**kwargs):
            if command[-1].endswith("check.R"):
                raise OSError(8,"Exec format error")
            return old(command,**kwargs)
        self.workbench.runner=runner
        with patch("scisaurus.runtime.software_workbench.shutil.which",return_value=sys.executable), \
                patch("scisaurus.runtime.software_workbench.sandbox_status",return_value={"mode":"sandbox-exec"}):
            row=self.action("check_environment")
        self.assertEqual(row["outcome"],"ok",row)
        self.assertEqual(row["result"]["python"]["returncode"],0)
        error=row["result"]["r"]["execution"]["startup_error"]
        self.assertEqual(error["errno"],8)
        self.assertIn("Exec format error",error["message"])

    @unittest.skipUnless(sandbox_status()["mode"] == "sandbox-exec", "requires the native sandbox")
    def test_environment_records_actual_child_resource_limits(self):
        from scisaurus.runtime.program_sandbox import run_sandboxed
        self.workbench.runner=run_sandboxed
        with patch("scisaurus.runtime.software_workbench.shutil.which",return_value=None):
            row=self.action("check_environment")
        self.assertEqual(row["outcome"],"ok",row)
        result=row["result"]
        measured=json.loads(result["python"]["stdout"])
        self.assertEqual(result["sandbox_limits"]["observed_python_posix"],measured["posix_limits"])
        self.assertEqual(measured["posix_limits"]["cpu_seconds"]["soft"],600)
        self.assertEqual(measured["baseline"]["file_bytes"],8*1024**2)
        self.assertGreater(result["python"]["elapsed_seconds"],0)

    def test_environment_tampering_is_rejected(self):
        _, _, _, installed = self.provision()
        (Path(installed["result"]["environment_path"]) / "library/tinyprobe/package").write_text("tampered")
        with self.assertRaisesRegex(ValidationError, "environment changed"):
            self.workbench.execute(installed["action"])

    def test_cached_run_rechecks_environment_and_failed_projection_preserves_error(self):
        _, _, docs, installed = self.provision()
        action = {"operation":"run", "arguments":{
            "environment_ref":installed["receipt_ref"], "source":'cat("{\\\"answer\\\":42}")',
            "input":{}, "purpose":"scientific_computation", "documentation_refs":[docs["receipt_ref"]], "expected":None}}
        with patch("scisaurus.runtime.software_workbench.sandbox_status", return_value={"mode":"sandbox-exec"}):
            result = self.workbench.execute(action)
        self.assertEqual(result["outcome"], "ok")
        self.assertTrue(self.workbench.execute(action)["reused"])
        (Path(installed["result"]["environment_path"]) / "library/tinyprobe/package").write_text("tampered")
        with self.assertRaisesRegex(ValidationError, "environment changed"):
            self.workbench.execute(action)
        for operation in ("acquire", "inspect"):
            failure = {"action":{"operation":operation,"arguments":{}},"outcome":"failed","result":{},"error":"missing prerequisite"}
            self.assertEqual(project_receipt(failure),failure)

    def test_dependency_license_and_path_ownership_are_required(self):
        inspected = self.action("inspect", repository="upstream/tinyprobe", revision="main")
        invalid = self.action("read", inspection_ref=inspected["receipt_ref"], path="../../private")
        self.assertEqual(invalid["outcome"], "failed")
        invalid = self.action("acquire", inspection_ref=inspected["receipt_ref"], runtime="r", requirements=[], dependencies=[], package_path=".")
        self.assertEqual(invalid["outcome"], "failed")

    def test_failed_execution_keeps_stdout_stderr_and_input(self):
        _, _, docs, installed = self.provision()
        self.workbench.runner = lambda *args, **kwargs: SandboxResult(1, b"partial output", b"missing dependency", False, False, "sandbox-exec")
        with patch("scisaurus.runtime.software_workbench.sandbox_status", return_value={"mode":"sandbox-exec"}):
            result = self.action("run", environment_ref=installed["receipt_ref"], source="bad call", input={"parameter":2},
                                 purpose="scientific_computation", documentation_refs=[docs["receipt_ref"]], expected=None)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["execution"]["stdout"], "partial output")
        self.assertEqual(result["execution"]["stderr"], "missing dependency")
        self.assertEqual(result["action"]["arguments"]["input"], {"parameter":2})

    def test_parse_failure_keeps_successful_process_diagnostics(self):
        _, _, docs, installed = self.provision()
        self.workbench.runner = lambda *args, **kwargs: SandboxResult(0, b"solver progress\nnot JSON", b"units warning", False, False, "sandbox-exec")
        with patch("scisaurus.runtime.software_workbench.sandbox_status", return_value={"mode":"sandbox-exec"}):
            result = self.action("run", environment_ref=installed["receipt_ref"], source="bad adapter", input={},
                                 purpose="scientific_computation", documentation_refs=[docs["receipt_ref"]], expected=None)
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["execution"]["returncode"], 0)
        self.assertEqual(result["execution"]["stdout"], "solver progress\nnot JSON")
        self.assertEqual(result["execution"]["stderr"], "units warning")

    def test_failed_search_cannot_admit_custom_model(self):
        response = selection_contract()
        response.update(decision="pass", summary="ready")
        response["software_selection"].update(strategy="custom_model", rationale="fit", environment_ref=None, example_ref=None,
                                              computation_refs=[],scientific_source_refs=["source"])
        results = [{"outcome":"ok","action":{"operation":"check_environment"}}, {"outcome":"failed","action":{"operation":"search"}}]
        with self.assertRaisesRegex(ValidationError, "actual software discovery"):
            validate_selection(response, self.workbench, results)

    def test_review_preserves_current_question_selected_source_and_raw_outputs(self):
        evidence = {"request":{"topic":{"research_question":"declared question"}},"selection":{"strategy":"reuse"},
                    "selected_operations":[{"source":"actual adapter source","output":{"result":42},"input":{"n":2},"license":"actual license"}]}
        for limit in (None, 12000):
            prompt = json.loads(build_verifier_prompt({"id":"software","kind":"experiment"},
                {"repair_verification_scope":"scientific_software_fitness"},
                [{"response":{"software_selection":{"strategy":"reuse"}}}], {"software_assessment":evidence}, max_input_tokens=limit))
            self.assertEqual(prompt["chief_result"]["software_assessment"], evidence)
            self.assertEqual(prompt["specialist_reports"][0]["response"]["software_selection"], {"strategy":"reuse"})
            self.assertEqual(prompt["verifier_contract"]["review_subject"]["path"], "chief_result.software_assessment")

    def test_controller_assessment_runs_multi_step_tools_review_and_reuses_owned_receipt(self):
        self._controller_assessment()

    def test_software_selection_review_has_phase_owned_contract_and_preserves_final_gates(self):
        from scisaurus.runtime.specialists import SOFTWARE_SELECTION_SYSTEM
        self.assertIn("For a reuse strategy", SOFTWARE_SELECTION_SYSTEM)
        self.assertIn("custom_model requires the source-bound mathematical specification", SOFTWARE_SELECTION_SYSTEM)
        self.assertIn("mandatory result-admission gates after selection", SOFTWARE_SELECTION_SYSTEM)
        stage = {"id": "experiment", "kind": "experiment"}
        final = {"current_stage_id": "experiment", "downstream_stage_ids": [],
                 "acceptance_target": "independent acceptance of the declared experiment output",
                 "current_requirements": ["actual observations and independent recalculation"]}
        packet = {"repair_verification_scope": "scientific_software_fitness", "stage_acceptance_contract": final,
                  "work_orders": [{"objective": "repair executor and run independent validator"}]}
        chief = {"software_assessment": {"selection": {"strategy": "custom_model"}, "selected_operations": []}}
        before = deepcopy(packet)
        prompt = json.loads(build_verifier_prompt(stage, packet, [], chief, max_input_tokens=32000))
        contract = prompt["verifier_contract"]
        self.assertEqual(contract["result_admission_contract"], final)
        self.assertEqual(contract["result_admission_contract_sha256"], hashlib.sha256(canonical_bytes(final)).hexdigest())
        self.assertEqual(contract["acceptance_target"], contract["stage_acceptance_contract"]["acceptance_target"])
        self.assertEqual(contract["stage_acceptance_contract"]["review_phase"], "scientific_software_fitness")
        self.assertEqual(contract["stage_acceptance_contract"]["downstream_stage_ids"], [])
        self.assertIn("For reuse", contract["stage_acceptance_contract"]["current_requirements"][1])
        self.assertIn("For custom_model", contract["stage_acceptance_contract"]["current_requirements"][1])
        self.assertEqual(prompt["work_orders"], packet["work_orders"])
        self.assertEqual(packet, before)
        ordinary = json.loads(build_verifier_prompt(stage, {"stage_acceptance_contract": final}, [], {}, max_input_tokens=32000))
        self.assertEqual(ordinary["verifier_contract"]["stage_acceptance_contract"], final)
        self.assertNotIn("result_admission_contract", ordinary["verifier_contract"])

    def test_controller_assessment_without_stage_quota_uses_deadline(self):
        self._controller_assessment(with_quota=False)

    def test_controller_producer_contract_failure_keeps_response_ownership(self):
        self._controller_assessment(failure_mode="producer_contract")

    def test_controller_reviewer_contract_failure_keeps_response_ownership(self):
        self._controller_assessment(failure_mode="reviewer_contract")

    def test_controller_scientific_hold_is_not_a_response_contract_failure(self):
        self._controller_assessment(failure_mode="scientific_hold")

    def _controller_assessment(self, *, with_quota=True, failure_mode=None):
        from scisaurus.tests.test_composer import ComposerWorkflowTests
        from scisaurus.runtime.composer import ComposerRunner
        root = Path(self.directory.name)
        workflow = ComposerWorkflowTests()._workflow(root)
        if with_quota:
            workflow["stages"][1]["quota"] = {"max_model_calls":24,"max_input_tokens":1000000,"max_output_tokens":200000,"max_openalex_requests":100}
        runner = ComposerRunner(workflow)
        runner.workflow["capability_foundry_config_path"] = "configured-by-test"
        self.addCleanup(runner.close)
        stage = workflow["stages"][1]
        task = runner._stage_task(stage)
        runner.tasks.start_attempt(task["task_id"], "software-assessment-owner", owner="command.composer", lease_ttl_seconds=30, payload={"stage_id":stage["id"]})
        runner.stage_records[stage["id"]] = {"status":"running","kind":stage["kind"],"task_id":task["task_id"],"attempt_id":"software-assessment-owner","attempts":[]}
        runner._checkpoint("experiment:admitted",force=True)
        topic = {"topic":{"id":"test-question","domain":"test science","research_question":"Does the scientific engine compute the declared value?"}}
        reviewer_inputs = []
        model = {"protocol":"openai_compatible","base_url":"http://127.0.0.1:1/v1","model":"fixture","timeout_seconds":10,"max_output_tokens":4096}
        def complete(**kwargs):
            prompt = json.loads(kwargs["prompt"])
            if "response_format_repair" in prompt:
                prompt = prompt["evidence_packet"]
            if kwargs["system"] == VERIFIER_SYSTEM:
                reviewer_inputs.append(prompt)
                evidence = prompt["chief_result"]["software_assessment"]
                self.assertEqual(evidence["request"]["topic"], topic["topic"])
                self.assertEqual(evidence["selection"]["strategy"], "reuse")
                response = {"decision":"accept","rationale":"fixture evidence is consistent","critical_findings":[],"repair_scope":[]}
                if failure_mode == "reviewer_contract":
                    response["decision"] = "unsupported_verdict"
            else:
                tools = prompt.get("software_tool_results", [])
                by_operation = {}
                for row in tools:
                    by_operation.setdefault(row["action"]["operation"], []).append(row)
                self.assertIn("check_environment",by_operation)
                def action(operation, **arguments):
                    return {"tool_action":{"operation":operation,"arguments":arguments}}
                if "search" not in by_operation:
                    response = action("search", query=topic["topic"]["research_question"])
                elif "inspect" not in by_operation:
                    response = action("inspect", repository=by_operation["search"][0]["result"]["repositories"][0]["full_name"], revision="main")
                elif len(by_operation.get("read", [])) < 2:
                    response = action("read", inspection_ref=by_operation["inspect"][0]["receipt_ref"], path="LICENSE" if not by_operation.get("read") else "README.md")
                elif "acquire" not in by_operation:
                    response = action("acquire", inspection_ref=by_operation["inspect"][0]["receipt_ref"],license_ref=by_operation["read"][0]["receipt_ref"],
                                      runtime="r",requirements=[],dependencies=[],package_path=".")
                elif len(by_operation.get("run", [])) < 2:
                    example = not by_operation.get("run")
                    response = action("run", environment_ref=by_operation["acquire"][0]["receipt_ref"],source='cat("{\\\"answer\\\":42}")',input={"n":2},
                                      purpose="upstream_example" if example else "scientific_computation",documentation_refs=[by_operation["read"][1]["receipt_ref"]],
                                      expected={"value":{"answer":42},"absolute_tolerance":0,"relative_tolerance":0} if example else None)
                else:
                    response = selection_contract()
                    response.update(decision="pass",summary="software fits the fixture question")
                    response["software_selection"].update(strategy="reuse",rationale="declared function matches fixture",environment_ref=by_operation["acquire"][0]["receipt_ref"],
                        example_ref=by_operation["run"][0]["receipt_ref"],computation_refs=[by_operation["run"][1]["receipt_ref"]],scientific_source_refs=[by_operation["read"][1]["receipt_ref"]],limitations=["fixture only"])
                    if failure_mode == "producer_contract":
                        for key in ("strategy", "scientific_source_refs"):
                            response[key] = response["software_selection"].pop(key)
                        response["independent recalculation"] = "wrong response field"
                    elif failure_mode == "scientific_hold":
                        response["decision"] = "hold"
            return ModelResult(json.dumps(response),"fixture",{"model_calls":1,"input_tokens":10,"output_tokens":10},0.001,"stop",1)
        with patch.object(runner,"_specialist_model_config",return_value=model), \
                patch("scisaurus.runtime.composer.enforce_model_cost_limits",return_value=True), \
                patch("scisaurus.runtime.specialists.ModelClient") as client, \
                patch("scisaurus.runtime.software_workbench.SoftwareWorkbench._fetch",side_effect=self.fetch), \
                patch("scisaurus.runtime.software_workbench.run_sandboxed",side_effect=self.runner), \
                patch("scisaurus.runtime.software_workbench.shutil.which",return_value=sys.executable), \
                patch("scisaurus.runtime.software_workbench.sandbox_status",return_value={"mode":"sandbox-exec"}):
            client.return_value.complete.side_effect=complete
            if failure_mode:
                from scisaurus.runtime.model_work import ModelWorkBlocked
                from scisaurus.runtime.failure_recovery import classify_failure
                with self.assertRaises(ModelWorkBlocked) as caught:
                    runner._assess_scientific_software(stage, {}, topic)
                error = caught.exception
                retained = error.stage_result["scientific_software_assessment"]
                self.assertEqual(retained["status"], "blocked")
                self.assertGreater(error.usage["model_calls"], 0)
                self.assertEqual(error.usage, error.repair_panel_usage)
                self.assertTrue(retained["evidence"]["discovery_and_diagnostics"])
                if failure_mode.endswith("contract"):
                    self.assertEqual(classify_failure("experiment", error, error.stage_result), "model_contract")
                    self.assertEqual(error.stage_result["failure"], {"kind": "output_contract", "outcome_known": True, "failure_class": "model_contract"})
                    self.assertFalse(runner._is_pre_execution_capability_failure(stage, error=error))
                    self.assertFalse(runner._is_pre_execution_capability_failure(stage, error.stage_result))
                    if failure_mode.startswith("producer"):
                        prior = {"kind": "experiment", "status": "blocked", "failure_class": "model_contract",
                                 "review_status": "model_contract_repair",
                                 "failure_recovery": {"failure_class": "model_contract", "recovery_mode": "format_repair_then_rerun"}}
                        runner.context[stage["id"]] = prior
                        with patch.object(runner, "_format_contract_recovery_request", return_value={"id": "format-owned", "kind": "recovery", "target_stage_id": stage["id"]}), \
                                patch.object(runner, "_begin_continuation", return_value=True):
                            self.assertTrue(runner._admit_scientific_blocker_recovery(stage, error, set(), {stage["id"]: stage}))
                        self.assertEqual(runner.context[stage["id"]]["review_status"], "model_contract_repair")
                        self.assertEqual(runner.department_activity[-1]["action"], "admit_model_contract_recovery")
                    self.assertEqual(error.repair_gate, "scientific_software_assessment_response")
                    self.assertEqual(error.model_diagnostics["software_assessment_ref"], retained["artifact_ref"])
                    owner_key = "producer_execution_ref" if failure_mode.startswith("producer") else "verifier_execution_ref"
                    self.assertEqual(error.model_diagnostics["response_execution_ref"], retained[owner_key])
                    self.assertEqual(error.model_diagnostics["response_error"], str(error))
                    if failure_mode.startswith("producer"):
                        self.assertIn("at /software_selection", str(error))
                        self.assertEqual(reviewer_inputs, [])
                else:
                    self.assertEqual(classify_failure("experiment", error, error.stage_result), "experiment_failure")
                    self.assertFalse(hasattr(error, "repair_gate"))
                    self.assertEqual(reviewer_inputs, [])
                return
            receipt=runner._assess_scientific_software(stage, {}, topic)
            self.assertEqual(receipt["status"], "accepted")
            self.assertEqual(len(reviewer_inputs),1)
            self.assertGreater(receipt["dispatch_usage"]["model_calls"],4)
            before=client.return_value.complete.call_count
            retained=runner._assess_scientific_software(stage, {}, topic)
            self.assertEqual(client.return_value.complete.call_count,before)
            self.assertEqual(retained["artifact_ref"],receipt["artifact_ref"])
            projection=runner._scientific_software_projection(receipt)
            self.assertEqual(projection["operations"][-1]["result"]["output"],{"answer":42})
            self.assertEqual(projection["host_environment_checks"][0]["action"]["operation"],"check_environment")
            plan=runner._read_verified_artifact_json(receipt["ledger"]["assignment_plan_ref"])[2]
            if with_quota:
                self.assertGreater(plan["role_quotas"]["methods.methodologist"]["max_calls"],4)
                scope = runner._stage_model_config(stage,{})["model_call_budget_scopes"][0]
                self.assertEqual(runner._legacy_stage_model_invoices(stage["id"],scope),{})
            else:
                self.assertIsNone(plan["role_quotas"]["methods.methodologist"]["max_calls"])
            changed_scope = {"source_data_manifest":{"rows_sha256":"a"*64},"work_orders":[{"objective":"Run a new sensitivity contrast"}]}
            revised = runner._assess_scientific_software(stage,{},topic,computation_scope=changed_scope)
            self.assertNotEqual(revised["artifact_ref"],receipt["artifact_ref"])
            self.assertEqual(revised["evidence"]["request"]["computation_scope"],changed_scope)
            self.assertEqual(len(reviewer_inputs),2)
            from scisaurus.runtime.model_work import ModelWorkBlocked
            with patch.object(runner,"_run_specialist_verifier",side_effect=ValidationError("review packet exceeds current input capacity")):
                with self.assertRaises(ModelWorkBlocked) as caught:
                    runner._assess_scientific_software(stage,{},topic,computation_scope={"work_orders":[{"objective":"Third contrast"}]})
            self.assertGreater(caught.exception.usage["model_calls"],0)
            retained_failure=caught.exception.stage_result["scientific_software_assessment"]
            self.assertEqual(retained_failure["status"],"blocked")
            self.assertEqual(retained_failure["evidence"]["selection"]["strategy"],"reuse")
            if with_quota:
                self.assertEqual(runner._legacy_stage_model_invoices(stage["id"],scope),{})

    def test_software_panel_id_preserves_suffix_within_identifier_limit(self):
        from scisaurus.runtime.composer import ComposerRunner
        identifier=ComposerRunner._capability_repair_panel_stage_id("experiment-"+"a"*45,"b"*64,0,1,purpose="software")
        self.assertLessEqual(len(identifier),64)
        self.assertTrue(identifier.endswith("bbbbbbbbbbbb-software"))

    def test_interrupted_operation_is_not_repeated(self):
        action = {"operation":"search", "arguments":{"query":"science"}}
        import hashlib
        from scisaurus.core.schema import canonical_bytes
        from scisaurus.runtime.software_workbench import REVISION
        key = hashlib.sha256(canonical_bytes({"revision":REVISION,"action":action})).hexdigest()
        directory = Path(self.directory.name) / "actions"
        directory.mkdir()
        (directory / (key+".json")).write_text('{"status":"started"}')
        result = self.workbench.execute(action)
        self.assertEqual(result["outcome"], "result_unknown")
        self.assertEqual(self.fetches, [])

    def test_no_silent_sandbox_downgrade(self):
        with patch("scisaurus.runtime.software_workbench.sandbox_status", return_value={"mode":"rlimits-only"}):
            result = self.action("check_environment")
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(self.runs, [])

    def test_unsafe_archive_members_are_rejected_before_installation(self):
        inspected = self.action("inspect", repository="upstream/tinyprobe", revision="main")
        license = self.action("read", inspection_ref=inspected["receipt_ref"], path="LICENSE")
        old = self.workbench.fetch
        self.workbench.fetch = lambda url, **kw: archive(self.files, unsafe=True) if kw.get("archive") else old(url, **kw)
        result = self.action("acquire", inspection_ref=inspected["receipt_ref"], license_ref=license["receipt_ref"], runtime="r", requirements=[], dependencies=[], package_path=".")
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(self.runs, [])

    def test_expected_output_requires_finite_nonboolean_tolerances(self):
        self.assertTrue(_matches_expected({"value":1.234567}, {"value":{"value":1.23457},"absolute_tolerance":0.000005,"relative_tolerance":0}))
        self.assertFalse(_matches_expected({"value":True}, {"value":{"value":1},"absolute_tolerance":0,"relative_tolerance":0}))
        for bad in (True, float("nan"), float("inf"), -1):
            with self.assertRaises(ValidationError):
                _matches_expected({}, {"value":{},"absolute_tolerance":bad,"relative_tolerance":0})

    def test_selection_cannot_claim_installation_without_tools(self):
        response = selection_contract()
        response.update(decision="pass", summary="ready")
        response["software_selection"].update(strategy="reuse", rationale="fit", environment_ref="madeup", example_ref="madeup", computation_refs=["madeup"])
        with self.assertRaises(ValidationError):
            validate_selection(response, self.workbench, [])

    def test_specialist_uses_failure_then_corrected_action_and_final_report(self):
        responses = [
            {"tool_action":{"operation":"read","arguments":{"inspection_ref":"invalid","path":"README"}}},
            {"tool_action":{"operation":"search","arguments":{"query":"another scientific library"}}},
            {"decision":"hold","summary":"Candidate discovered; runtime requires assessment","findings":[],"evidence_gaps":[],"requested_actions":[]},
        ]
        prompts = []
        def complete(**kwargs):
            prompts.append(json.loads(kwargs["prompt"]))
            return ModelResult(json.dumps(responses.pop(0)), "fixture", {"input_tokens":10,"output_tokens":5}, 0.01,"stop",1)
        dispatcher = SpecialistDispatcher({"protocol":"openai_compatible","base_url":"http://127.0.0.1:1/v1","model":"fixture","timeout_seconds":10,"max_output_tokens":1000},
                                         deadline=time.monotonic()+60, software_workspace=self.directory.name)
        with patch("scisaurus.runtime.specialists.ModelClient") as client, \
                patch("scisaurus.runtime.software_workbench.SoftwareWorkbench._fetch", side_effect=self.fetch):
            client.return_value.complete.side_effect = complete
            report = dispatcher._execute({"assigned_role":"methods.methodologist","role_id":"methodologist","_software_tools":True,
                "_prompt":json.dumps({"question":"a scientific question"}), "quota":{"max_calls":5,"max_output_tokens":1000}}, {})
        self.assertEqual(report["status"], "succeeded", report)
        self.assertEqual(prompts[1]["software_tool_results"][0]["outcome"], "failed")
        self.assertEqual(prompts[2]["software_tool_results"][1]["result"]["repositories"][0]["full_name"], "upstream/tinyprobe")
        self.assertEqual(report["usage"]["input_tokens"], 30)
        self.assertEqual(len(report["software_tool_results"]), 2)

    def test_null_call_quota_repairs_malformed_response_and_preserves_provider_failure(self):
        from scisaurus.runtime.models import ModelCallError
        valid=ModelResult(json.dumps({"decision":"hold","summary":"Prerequisites unresolved","findings":[],"evidence_gaps":[],"requested_actions":[]}),"fixture",{"model_calls":1,"input_tokens":10,"output_tokens":5},0.01,"stop",1)
        for initial in (ModelResult("not JSON","fixture",{"model_calls":1,"input_tokens":10,"output_tokens":5},0.01,"stop",1),
                        ModelCallError("provider failed",outcome_known=True,attempts=1,status_code=503)):
            dispatcher=SpecialistDispatcher({"protocol":"openai_compatible","base_url":"http://127.0.0.1:1/v1","model":"fixture","timeout_seconds":10,"max_output_tokens":1000},
                                           deadline=time.monotonic()+60,software_workspace=self.directory.name)
            with patch("scisaurus.runtime.specialists.ModelClient") as client:
                client.return_value.complete.side_effect=[initial,valid,valid]
                report=dispatcher._execute({"assigned_role":"methods.methodologist","role_id":"methodologist","_software_tools":True,
                    "_prompt":json.dumps({"question":"a scientific question"}),"quota":{"max_calls":None,"max_output_tokens":1000}}, {})
            self.assertIn(report["status"],{"succeeded","failed"})
            self.assertTrue(report["retry_history"] or report.get("failure"))
            if isinstance(initial,ModelResult):
                self.assertEqual(report["status"],"succeeded")
                self.assertEqual(report["usage"]["model_calls"],2)

    def test_selection_format_repair_keeps_software_contract_and_evidence_tools_authorized(self):
        response=selection_contract()
        response.update(decision="hold",summary="Required solver runtime unavailable")
        response["software_selection"].update(strategy="unavailable",rationale="Prerequisite missing")
        responses=[ModelResult("invalid JSON","fixture",{"model_calls":1},0.01,"stop",1),
                   ModelResult(json.dumps(response),"fixture",{"model_calls":1},0.01,"stop",1)]
        prompts=[]
        def complete(**kwargs):
            prompts.append(kwargs["prompt"])
            return responses.pop(0)
        dispatcher=SpecialistDispatcher({"protocol":"openai_compatible","base_url":"http://127.0.0.1:1/v1","model":"fixture","timeout_seconds":10,"max_output_tokens":1000},
                                       deadline=time.monotonic()+60,software_workspace=self.directory.name)
        with patch("scisaurus.runtime.specialists.ModelClient") as client:
            client.return_value.complete.side_effect=complete
            report=dispatcher._execute({"assigned_role":"methods.methodologist","role_id":"methodologist","_software_tools":True,"_response_contract":"software_selection",
                "_prompt":json.dumps({"software_assessment_request":{"source_ref_catalog":[]}}),"quota":{"max_calls":None,"max_output_tokens":1000}}, {})
        self.assertEqual(report["status"],"succeeded",report)
        self.assertIn("including software_selection",prompts[1])
        self.assertEqual(report["response"]["software_selection"]["strategy"],"unavailable")
        self.assertIn("intermediate response contains only tool_action",REPAIR_EVIDENCE_SYSTEM)

    @unittest.skipUnless(sandbox_status()["mode"] == "sandbox-exec", "requires macOS sandbox")
    def test_real_native_build_run_and_outside_write_denial(self):
        self.files = {"LICENSE":"MIT License", "README.md":'Expected JSON: {"answer":42}',
                      "Makefile":"all:\n\tcc engine.c -o engine\n", "engine.c":'#include <stdio.h>\nint main(void){puts("{\\\"answer\\\":42}");return 0;}\n'}
        self.workbench.runner = __import__("scisaurus.runtime.program_sandbox", fromlist=["run_sandboxed"]).run_sandboxed
        self.workbench.deadline = time.monotonic()+120
        inspected = self.action("inspect", repository="upstream/tinyprobe", revision="main")
        license = self.action("read", inspection_ref=inspected["receipt_ref"], path="LICENSE")
        docs = self.action("read", inspection_ref=inspected["receipt_ref"], path="README.md")
        installed = self.action("acquire", inspection_ref=inspected["receipt_ref"], license_ref=license["receipt_ref"], runtime="native",
                                requirements=[], dependencies=[], package_path=".", build={"system":"make","options":[],"executable":"source/engine"})
        self.assertEqual(installed["outcome"], "ok", installed)
        engine = installed["result"]["packages"]["engine_path"]
        result = self.action("run", environment_ref=installed["receipt_ref"], source=f'import subprocess; print(subprocess.check_output([{engine!r}]).decode(), end="")',
                             input={}, purpose="upstream_example", documentation_refs=[docs["receipt_ref"]],
                             expected={"value":{"answer":42},"absolute_tolerance":0,"relative_tolerance":0})
        self.assertEqual(result["outcome"], "ok", result)
        self.assertTrue(result["result"]["expected_matches"])
        outside = Path(self.directory.name).parent / (Path(self.directory.name).name+"-outside")
        denied = self.action("run", environment_ref=installed["receipt_ref"], source=f'from pathlib import Path; Path({str(outside)!r}).write_text("bad")',
                             input={}, purpose="scientific_computation", documentation_refs=[docs["receipt_ref"]], expected=None)
        self.assertEqual(denied["outcome"], "failed")
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
