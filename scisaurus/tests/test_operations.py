"""Operational lifecycle with recorded retrieval and actual local program fixtures."""
from copy import deepcopy
import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scisaurus.core.errors import StateError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.core.store import ArtifactStore
from scisaurus.core.tasks import TaskManager
from scisaurus.runtime.operations import OperationsCell
from scisaurus.runtime.programs import LocalProgramClient


def capture(data):
    return {"body": base64.b64encode(data).decode(), "encoding": "base64",
            "bytes": len(data), "sha256": sha256_hex(data), "media_type": "application/json"}


def crossref_result(params):
    payload = {"status": "ok", "message-version": "1.0.0", "message": {
        "items": [{"DOI": "10.1234/example", "title": ["Example paper"]}]}}
    captured = capture(canonical_bytes(payload))
    return {"outcome": "ok", "text": "Example paper — https://doi.org/10.1234/example",
            "sources": [{"doi": "10.1234/example", "source_url": "https://doi.org/10.1234/example", "representation": "metadata"}],
            "capture": captured, "capture_sha256": captured["sha256"], "raw_response": payload,
            "metadata": {"provider": "crossref", "transport": "http_api", "representation": "metadata", "http_status": 200,
                         "schema_version": "1.0.0", "query": params["query"], "rows": params["limit"]}}


def empty_crossref_result(result):
    result.update(outcome="empty", text="", sources=[])
    result["raw_response"]["message"]["items"] = []
    result["capture"] = capture(canonical_bytes(result["raw_response"]))
    result["capture_sha256"] = result["capture"]["sha256"]


def mcp_result(params):
    schema = {"type": "object", "properties": {key: {"type": kind} for key, kind in (
        ("url", "string"), ("max_length", "integer"), ("start_index", "integer"), ("raw", "boolean"))},
        "required": ["url"]}
    reply = {"content": [{"type": "text", "text": "Contents of the paper: a representative extracted paragraph."}]}
    content = reply["content"][0]["text"]
    server = {"name": "mcp-fetch", "version": "1.30.0"}
    transcript = [
        {"direction": "sent", "message": {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}},
        {"direction": "received", "message": {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-11-25", "serverInfo": server, "capabilities": {"tools": {}}}}},
        {"direction": "sent", "message": {"jsonrpc": "2.0", "method": "notifications/initialized"}},
        {"direction": "sent", "message": {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}},
        {"direction": "received", "message": {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "fetch", "inputSchema": schema}]}}},
        {"direction": "sent", "message": {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "fetch", "arguments": {"url": params["url"], "max_length": params["max_length"], "start_index": 0, "raw": False}}}},
        {"direction": "received", "message": {"jsonrpc": "2.0", "id": 3, "result": reply}},
    ]
    captured = capture(content.encode())
    return {"outcome": "ok", "text": content, "raw_response": reply, "capture": captured, "capture_sha256": captured["sha256"],
            "source_url": params["url"], "sources": [{"source_url": params["url"], "representation": "extracted_text"}],
            "metadata": {"provider": "mcp-fetch", "transport": "mcp_stdio", "representation": "extracted_text", "reported_media_types": [],
                         "command": params["client"]["command"], "protocol_version": "2025-11-25", "server_info": server,
                         "tool_schema_sha256": sha256_hex(json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()),
                         "tool_schema_wire_json": json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
                         "transcript": transcript, "process_returncode": 0}}


class RecordedExecutor:
    """Use real project task/evidence stores without launching a remote provider."""
    def __init__(self, control, store, *, mutate=None):
        self.tasks, self.store, self.mutate = TaskManager(control), store, mutate
        self.calls = []

    def __call__(self, task_id, kind, params, *, actor, task_kind):
        self.calls.append((task_id, kind, params, actor, task_kind))
        self.tasks.create(task_id, task_kind, {"operation": kind}, actor)
        self.tasks.admit(task_id, "command.controller")
        attempt = task_id + "-attempt"
        self.tasks.start_attempt(task_id, attempt, owner=actor, lease_ttl_seconds=60)
        context = self.store.publish_artifact(logical_id=f"command/contexts/{task_id}", artifact_type="note",
                    author=actor, body=canonical_bytes(params), media_type="application/json")
        if kind == "program":
            result = LocalProgramClient(**params["client"]).run(params["input"])
        else:
            result = crossref_result(params) if kind == "crossref" else mcp_result(params)
        if self.mutate:
            self.mutate(result)
        report = self.store.publish_artifact(logical_id=f"command/executions/{task_id}", artifact_type="report", author=actor,
                    body=canonical_bytes(result), media_type="application/json", inputs=[{"ref": context["artifact_ref"], "purpose": "subject"}])
        self.tasks.finish_attempt(attempt, "succeeded", usage={"program_calls" if kind == "program" else "retrieval_calls": 1})
        self.tasks.transition(task_id, "awaiting_review", actor)
        return result, report["artifact_ref"]


class TestOperationsCell(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scisaurus-operations-test-")
        self.root = Path(self.temp.name)
        self.control, self.store, self.cell = self.project("one")
        self.executor = RecordedExecutor(self.control, self.store)
        self.environment = self.root / "package-metadata"
        self.environment.write_text("mcp-server-fetch==2026.8.18\n")
        self.python = self.root / "python"
        self.python.write_text("fixture interpreter identity; this file is never executed")
        self.python.chmod(0o755)
        self.controls = [self.control]

    def tearDown(self):
        for control in self.controls:
            control.close()
        self.temp.cleanup()

    def project(self, name):
        control = ControlStore(self.root / name)
        store = ArtifactStore(control)
        store.init_project()
        return control, store, OperationsCell(control, store, project_id=name)

    def register(self, adapter="crossref"):
        if adapter == "crossref":
            client = {"timeout": 2, "max_bytes": 100000}
            representative = {"query": "causal inference", "limit": 2}
            environment_files = []
        elif adapter == "mcp_fetch":
            client = {"command": [str(self.python), "-m", "mcp_server_fetch"], "timeout": 2, "max_bytes": 100000,
                      "cwd": str(self.cell.workspace_dir("papers")), "own_process_group": False}
            representative = {"url": "https://example.org/paper", "max_length": 2000}
            environment_files = [self.environment]
        else:
            program = self.root / "program.py"
            program.write_text("import json, sys\nprint(json.dumps({'valid': False, 'input': json.load(sys.stdin)}))\n")
            client = {"command": [sys.executable, str(program)], "timeout": 2, "max_bytes": 100000,
                      "cwd": str(self.cell.workspace_dir("papers"))}
            representative = {"input": {"sample": "representative"}}
            environment_files = [program]
        return self.cell.register("papers", adapter=adapter, client=client, representative=representative,
                                  engineer="operations.engineer", environment_files=environment_files)

    def ready(self, adapter="crossref", executor=None):
        self.register(adapter)
        return self.cell.ensure_ready("papers", executor or self.executor, operator="operations.operator", verifier="operations.verifier")

    def verification(self, state):
        manifest = self.store.get(state["verification_ref"])
        return json.loads(self.store.read_body(manifest["body_hash"]))

    def test_activation_probe_independent_verification_idle_and_real_workload_records(self):
        registered = self.register()
        self.assertEqual(registered["state"], "inactive")
        with self.assertRaises(StateError):
            self.cell.probe("papers", self.executor, operator="operations.operator")
        self.cell.activate("papers", requester="research.searcher", purpose="Acquire project bibliography")
        pending = self.cell.probe("papers", self.executor, operator="operations.operator")
        self.assertEqual(pending["state"], "awaiting_verification")
        self.assertIsNone(pending["binding"])
        for actor in ("operations.engineer", "operations.operator"):
            with self.assertRaisesRegex(ValidationError, "independent"):
                self.cell.verify("papers", verifier=actor)
        state = self.cell.verify("papers", verifier="operations.verifier")
        self.assertEqual(state["state"], "ready")
        self.assertEqual(self.verification(state)["domain_acceptance"], "not_assessed")
        idle = self.cell.idle("papers")
        self.assertEqual(idle["binding"], state["binding"])
        result, ref = self.cell.run(idle["binding"], {"query": "replication studies", "limit": 1}, self.executor, operator="research.searcher")
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(len(self.executor.calls), 2)
        self.assertTrue(self.store.get(ref))
        self.assertEqual(self.control._conn.execute("SELECT COUNT(*) FROM accepted_heads").fetchone()[0], 0)
        self.assertEqual(self.control._conn.execute("SELECT COUNT(*) FROM tasks WHERE state='completed'").fetchone()[0], 3)
        self.assertTrue(self.control.verify_chain()[0])

    def test_interrupted_probe_records_degradation_and_propagates_stop(self):
        self.register()
        self.cell.activate("papers", requester="research.searcher", purpose="Acquire bibliography")
        with self.assertRaises(KeyboardInterrupt):
            self.cell.probe("papers", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt("stop")), operator="operations.operator")
        self.assertEqual(self.cell.status("papers")["state"], "degraded")

    def test_resume_reconciles_stale_phase_only_after_calls_are_settled(self):
        self.register()
        state = self.cell.activate("papers", requester="research.searcher", purpose="Acquire bibliography")
        self.cell._transition(state, "probing", "operations.operator", "Interrupted legacy probe")
        tasks = TaskManager(self.control)
        tasks.create("old-probe", "service", {}, "operations.operator")
        tasks.admit("old-probe", "command.controller")
        tasks.start_attempt("old-probe", "old-probe-attempt", owner="operations.operator", lease_ttl_seconds=60)
        resume = self.store.publish_artifact(logical_id="command/resume-sessions/1", artifact_type="report",
            author="command.recovery", body=canonical_bytes({"schema_version": "resume-session-1"}), media_type="application/json")
        with self.assertRaisesRegex(StateError, "new resume session"):
            self.cell.reconcile_interrupted("papers", resume_ref=resume["artifact_ref"])
        restored = OperationsCell(self.control, self.store, project_id="one")
        with self.assertRaisesRegex(StateError, "reconciled"):
            restored.reconcile_interrupted("papers", resume_ref=resume["artifact_ref"])
        tasks.reconcile_unknown("old-probe-attempt", "command.recovery")
        with self.assertRaisesRegex(StateError, "reconciled"):
            restored.reconcile_interrupted("papers", resume_ref=resume["artifact_ref"])
        tasks.finish_attempt("old-probe-attempt", "failed", usage={"retrieval_calls": 1})
        recovered = restored.reconcile_interrupted("papers", resume_ref=resume["artifact_ref"])
        self.assertEqual(recovered["state"], "degraded")
        self.assertIsNone(recovered["binding"])
        self.assertEqual(recovered["recovery_ref"], resume["artifact_ref"])
        self.cell = restored
        self.assertEqual(self.ready()["state"], "ready")
        self.assertTrue(self.control.verify_chain()[0])

    def test_same_local_artifact_names_do_not_enable_cross_project_binding(self):
        state = self.ready()
        other_control, other_store, other = self.project("two")
        self.controls.append(other_control)
        with self.assertRaisesRegex(ValidationError, "different project"):
            other.authorize(state["binding"])
        with self.assertRaisesRegex(ValidationError, "same project"):
            OperationsCell(other_control, self.store, project_id="two")
        with self.assertRaisesRegex(ValidationError, "identity/path"):
            OperationsCell(self.control, self.store, project_id="two")
        self.assertIsNone(other_store.head("command/operations/papers/profile"))

    def test_zero_exit_ok_flag_and_empty_output_do_not_establish_readiness(self):
        def invalid(result):
            result["text"] = ""
            result["sources"] = []
            result["metadata"]["process_returncode"] = 0
        state = self.ready("mcp_fetch", RecordedExecutor(self.control, self.store, mutate=invalid))
        self.assertEqual(state["state"], "degraded")
        self.assertIsNone(state["binding"])
        self.assertEqual(self.verification(state)["outcome"], "failed")

    def test_failing_provider_result_preserves_execution_and_blocks_binding(self):
        state = self.ready(executor=RecordedExecutor(self.control, self.store, mutate=lambda result: result.update(outcome="timeout")))
        self.assertEqual(state["state"], "degraded")
        report = self.verification(state)
        self.assertTrue(self.store.get(report["execution_ref"]))
        self.assertEqual(self.control._conn.execute("SELECT COUNT(*) FROM tasks WHERE state='blocked'").fetchone()[0], 1)

    def test_dispatch_failure_is_explicit_and_does_not_publish_ready(self):
        self.register()
        def failing(*args, **kwargs):
            raise TimeoutError("fixture deadline")
        state = self.cell.ensure_ready("papers", failing, operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(state["state"], "degraded")
        self.assertTrue(self.store.get(state["failure_ref"]))
        self.assertIsNone(state["binding"])

    def test_capture_hash_tampering_blocks_even_when_adapter_claims_success(self):
        state = self.ready(executor=RecordedExecutor(self.control, self.store, mutate=lambda result: result["capture"].update(body=base64.b64encode(b"replacement").decode())))
        checks = {check["check_id"]: check for check in self.verification(state)["checks"]}
        self.assertEqual(checks["capture-integrity"]["outcome"], "failed")
        self.assertEqual(state["state"], "degraded")

    def test_official_mcp_protocol_schema_and_call_are_independently_checked(self):
        state = self.ready("mcp_fetch")
        self.assertEqual(state["state"], "ready", self.verification(state))
        self.assertEqual(self.executor.calls[0][1], "fetch")
        self.assertTrue(state["binding"]["schema_identity"]["tool_schema_sha256"])
        def wrong_call(result):
            result["metadata"]["transcript"][-2]["message"]["params"]["arguments"]["url"] = "https://example.org/other"
        refreshed = self.cell.ensure_ready("papers", RecordedExecutor(self.control, self.store, mutate=wrong_call),
                                          operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(refreshed["state"], "degraded")

    def test_reprobe_detects_schema_drift_until_explicit_rebinding(self):
        state = self.ready()
        def drift(result):
            result["metadata"]["schema_version"] = "2.0.0"
            result["raw_response"]["message-version"] = "2.0.0"
            result["capture"] = capture(canonical_bytes(result["raw_response"]))
            result["capture_sha256"] = result["capture"]["sha256"]
        changed = RecordedExecutor(self.control, self.store, mutate=drift)
        refreshed = self.cell.ensure_ready("papers", changed, operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(refreshed["state"], "degraded")
        self.assertEqual(refreshed["schema_identity"], state["schema_identity"])
        with self.assertRaises(StateError):
            self.cell.authorize(state["binding"])
        self.register()
        rebound = self.cell.ensure_ready("papers", changed, operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(rebound["state"], "ready")
        self.assertNotEqual(rebound["profile_ref"], state["profile_ref"])

    def test_executable_and_environment_drift_block_launch_and_old_binding(self):
        state = self.ready("mcp_fetch")
        self.python.write_text("different interpreter")
        self.assertEqual(self.cell.status("papers")["state"], "degraded")
        with self.assertRaises(StateError):
            self.cell.authorize(state["binding"])
        refreshed = self.cell.ensure_ready("papers", self.executor, operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(refreshed["state"], "degraded")
        self.assertEqual(len(self.executor.calls), 1)
        self.register("mcp_fetch")
        rebound = self.cell.ensure_ready("papers", self.executor, operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(rebound["state"], "ready")
        self.environment.write_text("mcp-server-fetch==changed\n")
        self.assertEqual(self.cell.status("papers")["state"], "degraded")

    def test_mcp_schema_drift_requires_rebinding_and_missing_handshake_fails(self):
        first = self.ready("mcp_fetch")
        def changed_schema(result):
            schema = result["metadata"]["transcript"][4]["message"]["result"]["tools"][0]["inputSchema"]
            schema["properties"]["max_length"]["maximum"] = 50000
            wire_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            result["metadata"]["tool_schema_wire_json"] = wire_json
            result["metadata"]["tool_schema_sha256"] = sha256_hex(wire_json.encode())
        refreshed = self.cell.ensure_ready("papers", RecordedExecutor(self.control, self.store, mutate=changed_schema),
                                          operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(refreshed["state"], "degraded")
        checks = {item["check_id"]: item["outcome"] for item in self.verification(refreshed)["checks"]}
        self.assertEqual(checks["mcp-protocol"], "passed")
        self.assertEqual(checks["schema-stability"], "failed")
        self.assertEqual(refreshed["schema_identity"], first["schema_identity"])
        def missing_handshake(result):
            result["metadata"]["transcript"] = result["metadata"]["transcript"][3:]
        invalid = self.cell.ensure_ready("papers", RecordedExecutor(self.control, self.store, mutate=missing_handshake),
                                        operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(invalid["state"], "degraded")

    def test_invalid_routine_workload_degrades_with_exact_failure_evidence(self):
        state = self.ready()
        def incomplete(result):
            result["metadata"]["capture_incomplete"] = True
        with self.assertRaises(ValidationError):
            self.cell.run(state["binding"], {"query": "second query", "limit": 2},
                          RecordedExecutor(self.control, self.store, mutate=incomplete), operator="research.searcher")
        failed = self.cell.status("papers")
        self.assertEqual(failed["state"], "degraded")
        manifest = self.store.get(failed["failure_ref"])
        body = json.loads(self.store.read_body(manifest["body_hash"]))
        self.assertTrue(self.store.get(body["execution_ref"]))
        self.assertEqual(TaskManager(self.control).get(body["task_id"])["state"], "blocked")
        self.assertTrue(any(check["outcome"] == "failed" for check in body["checks"]))

    def test_complete_no_match_query_preserves_binding_and_exact_empty_result(self):
        state = self.ready()
        self.cell.idle("papers")
        result, ref = self.cell.run(state["binding"], {"query": "no matching papers", "limit": 2},
                                   RecordedExecutor(self.control, self.store, mutate=empty_crossref_result),
                                   operator="research.searcher")
        self.assertEqual(result["outcome"], "empty")
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["raw_response"]["message"]["items"], [])
        manifest = self.store.get(ref)
        self.assertEqual(json.loads(self.store.read_body(manifest["body_hash"])), result)
        available = self.cell.status("papers")
        self.assertEqual(available["state"], "idle")
        self.assertEqual(available["binding"], state["binding"])
        self.cell.authorize(state["binding"])
        task_id = manifest["artifact_id"].rsplit("/", 1)[1]
        self.assertEqual(TaskManager(self.control).get(task_id)["state"], "completed")

    def test_no_match_query_cannot_establish_representative_readiness(self):
        state = self.ready(executor=RecordedExecutor(self.control, self.store, mutate=empty_crossref_result))
        self.assertEqual(state["state"], "degraded")
        self.assertIsNone(state["binding"])

    def test_partial_or_inconsistent_empty_result_still_degrades(self):
        def concealed_items(result):
            result["raw_response"]["message"]["items"] = [{"DOI": "10.1234/hidden"}]
            result["capture"] = capture(canonical_bytes(result["raw_response"]))
            result["capture_sha256"] = result["capture"]["sha256"]
        for change in (lambda result: result["metadata"].update(capture_incomplete=True),
                       lambda result: result.update(outcome="partial"),
                       lambda result: result["capture"].update(sha256="0" * 64),
                       concealed_items):
            with self.subTest(change=change):
                state = self.ready()
                def malformed(result):
                    empty_crossref_result(result)
                    change(result)
                with self.assertRaises(ValidationError):
                    self.cell.run(state["binding"], {"query": "no matching papers", "limit": 2},
                                  RecordedExecutor(self.control, self.store, mutate=malformed), operator="research.searcher")
                self.assertEqual(self.cell.status("papers")["state"], "degraded")

    def test_empty_fetch_remains_unusable_for_routine_work(self):
        state = self.ready("mcp_fetch")
        def empty_fetch(result):
            result.update(outcome="empty", text="", sources=[])
        with self.assertRaises(ValidationError):
            self.cell.run(state["binding"], {"url": "https://example.org/paper", "max_length": 2000},
                          RecordedExecutor(self.control, self.store, mutate=empty_fetch), operator="research.searcher")
        self.assertEqual(self.cell.status("papers")["state"], "degraded")

    def test_zero_recorded_usage_is_not_a_completed_representative_execution(self):
        self.register()
        def unaccounted(task_id, kind, params, **kwargs):
            result, ref = self.executor(task_id, kind, params, **kwargs)
            with self.control.tx() as conn:
                conn.execute("UPDATE attempts SET usage_json=? WHERE task_id=?",
                             (json.dumps({"actual": {"retrieval_calls": 0}}), task_id))
            return result, ref
        state = self.cell.ensure_ready("papers", unaccounted, operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(state["state"], "degraded")
        self.assertIsNone(state["binding"])

    def test_persisted_ready_never_establishes_readiness_after_runtime_restart(self):
        state = self.ready()
        restarted = OperationsCell(self.control, self.store, project_id="one")
        self.assertEqual(restarted.status("papers")["state"], "degraded")
        with self.assertRaises(StateError):
            restarted.authorize(state["binding"])
        refreshed = restarted.ensure_ready("papers", self.executor, operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(refreshed["state"], "ready")
        self.assertNotEqual(refreshed["binding"]["session_id"], state["binding"]["session_id"])

    def test_external_executor_without_local_task_evidence_cannot_enable_capability(self):
        self.register()
        other_control, other_store, other = self.project("two")
        self.controls.append(other_control)
        state = self.cell.ensure_ready("papers", RecordedExecutor(other_control, other_store),
                                      operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(state["state"], "degraded")
        self.assertIsNone(state["binding"])

    def test_cleanup_only_owns_nonce_bound_scratch_and_preserves_software_evidence(self):
        state = self.ready("mcp_fetch")
        scratch = self.cell.workspace_dir("papers")
        (scratch / "temporary.txt").write_text("temporary output")
        other = self.root / "shared-file"
        other.write_text("retained")
        (scratch / "foreign-link").symlink_to(other)
        cleaned = self.cell.cleanup("papers")
        self.assertEqual(cleaned["state"], "inactive")
        self.assertFalse(scratch.exists())
        self.assertEqual(other.read_text(), "retained")
        self.assertTrue(self.environment.exists())
        self.assertTrue(self.store.get(state["verification_ref"]))
        with self.assertRaises(StateError):
            self.cell.authorize(state["binding"])

    def test_cleanup_rejects_replaced_workspace_symlink(self):
        self.ready("mcp_fetch")
        scratch = self.cell.workspace_dir("papers")
        (scratch / ".ownership.json").unlink()
        scratch.rmdir()
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "kept").write_text("not owned")
        scratch.symlink_to(foreign)
        with self.assertRaises(ValidationError):
            self.cell.cleanup("papers")
        self.assertEqual((foreign / "kept").read_text(), "not owned")

    def test_profile_rejects_unapproved_command_private_env_and_foreign_cwd(self):
        self.register("mcp_fetch")
        _, profile = self.cell._body(self.cell.status("papers")["profile_ref"])
        for key, value in (("command", ["sh", "-c", "echo installed"]), ("env", {"API_KEY": "never-publish"}), ("cwd", str(self.root))):
            client = deepcopy(profile["client"])
            client[key] = value
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.cell.register("papers", adapter="mcp_fetch", client=client, representative=profile["representative"],
                                   engineer="operations.engineer", environment_files=[self.environment])
        with patch.dict("os.environ", {"PRIVATE_MODEL_API_KEY": "never-inherit"}):
            self.register("mcp_fetch")
            _, updated = self.cell._body(self.cell.status("papers")["profile_ref"])
            self.assertNotIn("PRIVATE_MODEL_API_KEY", updated["client"]["env"])

    def test_local_program_readiness_preserves_negative_domain_output_and_routine_input(self):
        state = self.ready("local_program")
        self.assertEqual(state["state"], "ready", self.verification(state))
        self.assertEqual(self.executor.calls[0][1], "program")
        self.assertEqual(self.verification(state)["domain_acceptance"], "not_assessed")
        self.cell.idle("papers")
        arguments = {"input": {"items": [1, 2], "note": "A changed workload"}}
        result, ref = self.cell.run(state["binding"], arguments, self.executor, operator="research.checker")
        self.assertEqual(result["outcome"], "ok")
        self.assertFalse(result["document"]["valid"])
        self.assertEqual(result["document"]["input"], arguments["input"])
        self.assertEqual(self.executor.calls[-1][-1], "service")
        self.assertEqual(self.cell.status("papers")["state"], "idle")
        self.assertEqual(self.cell.status("papers")["binding"], state["binding"])
        self.assertTrue(self.store.get(ref))
        self.assertEqual(self.control._conn.execute("SELECT COUNT(*) FROM accepted_heads").fetchone()[0], 0)
        self.assertTrue(self.control.verify_chain()[0])

    def test_local_program_input_output_and_command_evidence_are_independently_checked(self):
        mutations = [
            ("input-binding", lambda result: result.update(input={"sample": "different"})),
            ("input-binding", lambda result: result["input_capture"].update(body=base64.b64encode(b"{}").decode())),
            ("capture-integrity", lambda result: result["stderr_capture"].update(sha256="0" * 64)),
            ("capture-integrity", lambda result: result["capture"].update(bytes=1)),
            ("usable-output", lambda result: result.update(document={"valid": True})),
            ("usable-output", lambda result: result["document"].update(valid=0)),
            ("usable-output", lambda result: result.update(text='{"unrelated":true}')),
            ("program-execution", lambda result: result["metadata"].update(command=["/different/program"])),
            ("program-execution", lambda result: result["metadata"].update(process_returncode=1)),
            ("program-execution", lambda result: result["metadata"]["command_identity"].update(sha256="0" * 64)),
            ("completeness", lambda result: result["metadata"].update(capture_incomplete=True)),
        ]
        for check_id, mutate in mutations:
            with self.subTest(check_id=check_id):
                state = self.ready("local_program", RecordedExecutor(self.control, self.store, mutate=mutate))
                self.assertEqual(state["state"], "degraded")
                report = self.verification(state)
                checks = {check["check_id"]: check["outcome"] for check in report["checks"]}
                self.assertEqual(checks[check_id], "failed", report)
                self.assertIsNone(state["binding"])

    def test_local_program_arguments_cannot_change_process_configuration(self):
        state = self.ready("local_program")
        for arguments in ({"input": {}, "command": ["/tmp/other"]}, {"input": []},
                          {"command": "arbitrary"}, {"input": {"invalid": float("nan")}}):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                self.cell.run(state["binding"], arguments, self.executor, operator="research.checker")
        self.assertEqual(len(self.executor.calls), 1)
        self.assertEqual(self.cell.status("papers")["state"], "ready")

    def test_local_program_identity_drift_and_runtime_restart_revoke_bindings(self):
        state = self.ready("local_program")
        program = self.root / "program.py"
        program.write_text("print('{}')\n")
        self.assertEqual(self.cell.status("papers")["state"], "degraded")
        with self.assertRaises(StateError):
            self.cell.authorize(state["binding"])
        before = len(self.executor.calls)
        reprobed = self.cell.ensure_ready("papers", self.executor, operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(reprobed["state"], "degraded")
        self.assertEqual(len(self.executor.calls), before)
        rebound = self.ready("local_program")
        restarted = OperationsCell(self.control, self.store, project_id="one")
        self.assertEqual(restarted.status("papers")["state"], "degraded")
        with self.assertRaises(StateError):
            restarted.authorize(rebound["binding"])

    def test_local_program_profiles_require_public_identity_and_project_owned_process_settings(self):
        self.register("local_program")
        _, profile = self.cell._body(self.cell.status("papers")["profile_ref"])
        for patch_value in ({"cwd": str(self.root)}, {"env": {"API_KEY": "never-publish"}},
                            {"command": ["python3", "program.py"]}, {"own_process_group": True}):
            with self.subTest(patch_value=patch_value), self.assertRaises(ValidationError):
                self.cell.register("papers", adapter="local_program", client={**profile["client"], **patch_value},
                                   representative=profile["representative"], engineer="operations.engineer",
                                   environment_files=profile["environment_files"])
        with self.assertRaisesRegex(ValidationError, "identity files"):
            self.cell.register("papers", adapter="local_program", client=profile["client"],
                               representative=profile["representative"], engineer="operations.engineer")

    def test_local_program_usage_must_use_its_own_dimension(self):
        self.register("local_program")
        def retrieval_accounted(task_id, kind, params, **kwargs):
            result, ref = self.executor(task_id, kind, params, **kwargs)
            with self.control.tx() as conn:
                conn.execute("UPDATE attempts SET usage_json=? WHERE task_id=?",
                             (json.dumps({"actual": {"retrieval_calls": 1, "program_calls": 0}}), task_id))
            return result, ref
        state = self.cell.ensure_ready("papers", retrieval_accounted,
                                       operator="operations.operator", verifier="operations.verifier")
        self.assertEqual(state["state"], "degraded")
        self.assertIsNone(state["binding"])


if __name__ == "__main__":
    unittest.main()
