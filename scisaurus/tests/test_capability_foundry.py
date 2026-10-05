import hashlib
import json
import signal
import sys
import tempfile
import unittest
import time
from copy import deepcopy
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from scisaurus.core.errors import ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_foundry import (
    CapabilityFoundry, CapabilityDeadlineError, CapabilityModelBudgetExceeded,
    AUTHOR_CONTINUATION_MAX_OUTPUT_TOKENS,
    AUTHOR_PATCH_MAX_SOURCE_CHARS, AUTHOR_PATCH_MAX_STRUCTURAL_REMOVALS,
    SourceDataUnavailable, _is_repeated_repair_failure, _sandbox_failure_signature,
    _program_gate_failure_signature,
    _authored_candidate_sha256, _candidate_bound_value, _source_patch_context,
    _author_requested_candidate,
    _retained_candidate_failure,
    _repair_scientific_input,
    _author_response_format_failure_signature,
    _sandbox_status_text,
    _author_request_signature, _author_request_was_attempted,
    _author_format_repair_instructions,
    _author_json_prefix_state,
    _compact_prior_blocking_issues, _retain_prior_blocking_issues,
    _reconcile_prior_blocking_issues, _record_program_gate_feedback,
    _validate_source_data_manifest, _validate_source_observation_binding,
    _model_route_identity,
    apply_authoring_patch, authoring_patch_prompt, normalize_capability_candidate,
    program_failure_context, program_review_evidence, candidate_prompt, PROGRAM_REVIEW_CHECKS, validate_program_review,
)
from unittest.mock import patch
from scisaurus.runtime.capability_registry import load_registry
from scisaurus.runtime.experiment import ExperimentRunner, validate_program_output
from scisaurus.runtime.experiment_config import ExperimentWorkOrderContractError
from scisaurus.runtime.models import ModelCallError, ModelContextBudgetError, ModelResult
from scisaurus.runtime.model_work import ModelWorkBlocked, ModelWorkCache
from scisaurus.runtime.research_quality import (
    ANALYSIS_FIELDS, default_research_quality_contract,
)
from scisaurus.tests.test_experiment import fixture_worker
from scisaurus.tests.test_program_admission import INTENT

ROOT = Path(__file__).resolve().parents[2]

MINI_EXECUTOR = '''
import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path


def png(width, height, rgb):
    raw = b"".join(b"\\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def main():
    request = json.load(sys.stdin)
    experiment = request["experiment"]
    run_count = int(experiment["run_count"])
    seed = int(experiment["seed"])
    state = seed
    errors, observations = [], []
    for index in range(run_count):
        state = (1103515245 * state + 12345) % (2 ** 31)
        value = abs(state / (2 ** 31) - 0.5)
        errors.append(value)
        observations.append({"replicate": index + 1, "estimate": value, "true_value": 0.0, "abs_error": value})
    ordered = sorted(errors)
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    p95 = ordered[lower] * (1 - fraction) + ordered[upper] * fraction
    metrics = [{"id": "tail_error", "value": p95, "unit": "error", "conditions": "declared seed",
                "source": "engine", "presentation": "95th-percentile absolute error is %.6g." % p95}]
    findings = [{"id": "tail_summary", "metric_ids": ["tail_error"],
                 "statement": "The declared estimator has 95th-percentile absolute error %.6g." % p95}]
    assets = []
    for asset_id, colour in (("figure_a", (200, 30, 30)), ("figure_b", (30, 200, 30)),
                             ("figure_c", (30, 30, 200))):
        body = png(2, 2, colour)
        name = asset_id + ".png"
        Path(name).write_bytes(body)
        assets.append({"id": asset_id, "path": name, "sha256": hashlib.sha256(body).hexdigest(),
                       "role": "figure", "media_type": "image/png", "caption": "Declared figure %s." % asset_id})
    result = {"schema_version": "experiment-program-output-1", "study_id": experiment["id"],
              "revision": experiment["revision"],
              "procedures": [{"id": "protocol", "description": experiment["method"], "source": "generated program"}],
              "observations": observations, "metrics": metrics, "findings": findings,
              "limitations": list(experiment["limitations"]), "assets": assets}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
'''

MINI_VALIDATOR = '''
import json
import sys


def main():
    request = json.load(sys.stdin)
    candidate = request.get("candidate")
    if candidate is None:
        sys.stdout.write(json.dumps({"status": "ready"}))
        return
    experiment = request["experiment"]
    ordered = sorted(float(row["abs_error"]) for row in candidate["observations"])
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    p95 = ordered[lower] * (1 - fraction) + ordered[upper] * fraction
    reported = {metric["id"]: metric["value"] for metric in candidate["metrics"]}
    matches = abs(float(reported["tail_error"]) - p95) <= 1e-12
    row_count = len(candidate["observations"])
    enough_rows = row_count >= int(experiment["run_count"])
    result = {"schema_version": "experiment-validation-1", "study_id": candidate["study_id"],
              "candidate_sha256": request["candidate_sha256"],
              "decision": "accepted" if matches and enough_rows else "rejected",
              "checks": [{"id": "row_arithmetic", "outcome": "passed", "evidence": "observations parsed"},
                         {"id": "design_row_count", "outcome": "passed" if enough_rows else "failed",
                          "evidence": "%d rows against %d planned" % (row_count, experiment["run_count"])},
                         {"id": "finite_values", "outcome": "passed", "evidence": "all recorded errors finite"}],
              "metric_recalculations": [{"metric_id": "tail_error",
                                          "reported_value": float(reported["tail_error"]),
                                          "recalculated_value": p95, "tolerance": 1e-12, "matches": matches}],
              "limitations": ["Recalculates summaries from recorded observations only."]}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
'''


def _add_prior_review_checks(payload, prompt):
    try:
        request = json.loads(prompt)
    except (TypeError, ValueError):
        return payload
    if (not isinstance(request, dict)
            or request.get("assignment") != "independent_scientific_program_review"
            or not isinstance(payload, dict)
            or payload.get("status") not in {"admitted", "rejected"}
            or not isinstance(payload.get("checks"), list)):
        return payload
    result = deepcopy(payload)
    from scisaurus.runtime.evidence import scientific_input_recovery_contract
    if request.get("scientific_input_recovery") != scientific_input_recovery_contract():
        raise AssertionError("program reviewers require the scientific input recovery contract")
    check_ids = {
        item["id"] for item in result["checks"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for issue in request.get("prior_blocking_issues", []):
        if not isinstance(issue, dict):
            continue
        check_id = issue.get("review_check_id")
        if not isinstance(check_id, str) or check_id in check_ids:
            continue
        result["checks"].append({
            "id": check_id,
            "outcome": "passed" if result["status"] == "admitted" else "failed",
            "evidence": "The revised candidate was reassessed against the recorded prior issue.",
        })
        check_ids.add(check_id)
    return result


class StubClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete(self, *, system, prompt):
        self.calls += 1
        payload = _add_prior_review_checks(self.payload, prompt)
        return ModelResult(json.dumps(payload), "stub", {"model_calls": 1}, 0.0, "stop")


class CapabilityFoundryTests(unittest.TestCase):
    def test_cloud_model_identity_matches_provider_response_alias(self):
        self.assertEqual(
            _model_route_identity("glm-5.3-flash:cloud"),
            _model_route_identity("glm-5.3-flash"),
        )

    def test_empirical_foundry_gate_stops_before_any_model_call_without_source_rows(self):
        brief = {
            "evidence_policy": {"requires_source_data_manifest": True},
            "topic": {"evidence_mode": "published_observations"},
        }
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            author = StubClient(self._payload())
            with self.assertRaises(SourceDataUnavailable):
                foundry.generate(json.dumps(brief), client=author)
        self.assertEqual(author.calls, 0)
        self.assertEqual(foundry.reviewer_client.calls, 0)

    def test_control_plane_recovery_order_is_rejected_before_any_model_call(self):
        recovery = {
            "id": "repair-format", "kind": "recovery",
            "owner": "methods.validation", "objective": "Repair the response contract.",
            "why": "The author response was not valid.",
            "success_condition": "A fresh response satisfies the role schema.",
            "evidence_needed": "The failure record and local schema validation.",
            "repair_policy_revision": "format-policy-5",
            "recovery_mode": "format_repair_then_rerun",
        }
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            author = StubClient(self._payload())
            with self.assertRaises(ExperimentWorkOrderContractError) as raised:
                foundry.generate(
                    "bounded comparison", test_input={"work_orders": [recovery]},
                    client=author)

        self.assertEqual(raised.exception.failure_class, "harness_bug")
        self.assertEqual(author.calls, 0)
        self.assertEqual(foundry.reviewer_client.calls, 0)

    def test_empirical_observations_are_bound_one_to_one_to_manifest_rows(self):
        manifest = {
            "schema_version": "source-data-manifest-1",
            "datasets": [{
                "artifact_ref": "artifact:survey/source@1",
                "source_sha256": "a" * 64,
                "source_url": "https://doi.org/10.1234/source",
                "source_location": "Figure 3, panel b, rows 1-2",
                "extraction_method": "deterministic table extraction",
                "rows": [
                    {"row_id": "point-1", "values": {"gap_mm": 0.25, "rate_s-1": 5.1}},
                    {"row_id": "point-2", "values": {"gap_mm": 1.0, "rate_s-1": 5.4}},
                ],
            }],
        }
        _validate_source_data_manifest(manifest)
        _validate_source_observation_binding({"observations": [
            {"source_record_id": "point-1",
             "source_values": {"gap_mm": 0.25, "rate_s-1": 5.1}, "replicate": 1},
            {"source_record_id": "point-2",
             "source_values": {"gap_mm": 1.0, "rate_s-1": 5.4}, "replicate": 1},
        ]}, {"source_data_manifest": manifest})
        forged = {"observations": [
            {"source_record_id": "point-1",
             "source_values": {"gap_mm": 0.25, "rate_s-1": 5.1}, "replicate": 1},
            {"source_record_id": "point-2",
             "source_values": {"gap_mm": 3.0, "rate_s-1": 2.0}, "replicate": 1},
        ]}
        with self.assertRaisesRegex(ValidationError, "exact controller-supplied source row"):
            _validate_source_observation_binding(forged, {"source_data_manifest": manifest})

    def test_sandbox_signal_exit_is_reported_by_name(self):
        signal_number = getattr(signal, "SIGXCPU", None)
        if signal_number is None:
            self.skipTest("SIGXCPU is not available on this platform")
        self.assertEqual(
            _sandbox_status_text(-int(signal_number)),
            f"-{int(signal_number)} (SIGXCPU)",
        )
        self.assertEqual(_sandbox_status_text(1), "1")

    @staticmethod
    def _review_payload():
        return {"status": "admitted", "findings": [], "checks": [
            {"id": key, "outcome": "passed", "evidence": "Bound synthetic fixture verified."}
            for key in sorted(PROGRAM_REVIEW_CHECKS)]}

    def test_authoring_patch_preserves_complete_admitted_plan(self):
        plan = {"root_cause": {"statement": "Diagnosis " * 300},
                "required_changes": [{"target": "executor", "instruction": "Exact operator " * 300}],
                "acceptance_checks": [{"phase": "plan" if i == 0 else "execution",
                                       "check": f"Check {i}: " + "Independent recalculation " * 100}
                                      for i in range(16)]}
        digest = hashlib.sha256(canonical_bytes(plan)).hexdigest()
        prompt = authoring_patch_prompt(
            brief=json.dumps({"capability_repair": {"repair_plan": plan}}), required_intent={},
            configured_input={"work_orders": [{"objective": "Use methods_adjudication",
                                                "methods_adjudication": plan}]},
            candidate={"executor_source": "def run(): pass", "validator_source": "def check(): pass",
                       "experiment_intent": {}},
            feedback="Repair the source", validation_context={}, validation_feedback={}, format_repair={})
        request = prompt["repair_request"]
        self.assertEqual(request["repair_plan"], plan)
        self.assertEqual(request["repair_plan_sha256"], digest)
        self.assertEqual(request["work_orders"][0]["methods_adjudication"], plan)
        self.assertEqual(request["work_orders"][0]["methods_adjudication_sha256"], digest)
        from scisaurus.runtime.specialists import REPAIR_CHECK_PHASE_RULE
        self.assertIn(REPAIR_CHECK_PHASE_RULE, prompt["instructions"])
        initial = candidate_prompt(json.dumps({"capability_repair": {"repair_plan": plan}}), [], {})
        self.assertEqual(initial["repair_check_phase_rule"], REPAIR_CHECK_PHASE_RULE)
        self.assertEqual(json.loads(initial["capability_brief"])["capability_repair"]["repair_plan"], plan)
        prompt = authoring_patch_prompt(
            brief=json.dumps({"capability_repair": {"repair_plan": plan}}), required_intent={},
            configured_input={"work_orders": []},
            candidate={"executor_source": "def run(): pass", "validator_source": "def check(): pass",
                       "experiment_intent": {}},
            feedback="Repair the source", validation_context={}, validation_feedback={}, format_repair={})
        self.assertEqual(prompt["repair_request"]["repair_plan"], plan)
        self.assertEqual(prompt["repair_request"]["repair_plan_sha256"], digest)

    def test_authoring_repair_prompt_targets_one_blocker_and_defers_the_rest(self):
        primary = {
            "severity": "blocking", "finding": "The contrast is algebraic by construction.",
            "evidence": "The down branch is the up branch times one parameter factor.",
            "required_change": "Reframe the claim or define an independent mechanism.",
        }
        secondary = {
            "severity": "blocking", "finding": "The interval is not independently checked.",
            "evidence": "The validator reuses the executor bootstrap code.",
            "required_change": "Recompute it independently or remove the interval claim.",
        }
        warning = {
            "severity": "warning", "finding": "The null control is tautological.",
            "evidence": "The branch term is zero in that control.",
            "required_change": "Describe it as an implementation check.",
        }
        feedback = {
            "decision": "rejected", "gate": "independent_recalculation",
            "failed_checks": [
                {"id": "claim_support", "outcome": "failed", "evidence": "Primary contrast is imposed."},
                {"id": "independent_validation", "outcome": "failed", "evidence": "Interval not recomputed."},
            ],
            "findings": [primary, secondary, warning],
            "metric_mismatches": [{"metric_id": "slope_diff", "reported_value": -0.05,
                                   "recalculated_value": 0.0, "tolerance": 1e-12,
                                   "matches": False}],
        }
        prompt = authoring_patch_prompt(
            brief={"topic": {"research_question": "Compare two onset slopes."}},
            required_intent={"primary_outcome": "slope difference"},
            configured_input={},
            candidate={"executor_source": "def run(): pass", "validator_source": "def check(): pass",
                       "experiment_intent": {}},
            feedback=("DEFERRED-FEEDBACK-LEAK-MARKER scientific candidate rejected: "
                      + json.dumps(feedback)),
            validation_context={}, validation_feedback=feedback,
            format_repair={"previous_error": "Malformed response: " + json.dumps(feedback)},
        )

        repair = prompt["repair_request"]["validation_feedback"]
        self.assertEqual(repair["findings"], [primary])
        self.assertEqual(repair["deferred_failed_check_ids"],
                         ["claim_support", "independent_validation"])
        self.assertEqual(repair["repair_scope"], {
            "policy": "one_issue_per_candidate_revision",
            "active_issue": "blocking_finding",
            "deferred_blocking_findings": 1,
            "deferred_warning_findings": 1,
            "deferred_failed_checks": 2,
            "deferred_metric_mismatches": 1,
        })
        self.assertEqual(repair["deferred_failed_check_ids"],
                         ["claim_support", "independent_validation"])
        self.assertEqual(repair["deferred_metric_ids"], ["slope_diff"])
        mismatch_prompt = authoring_patch_prompt(
            brief={}, required_intent={}, configured_input={},
            candidate={"executor_source": "x", "validator_source": "y", "experiment_intent": {}},
            feedback="independent recalculation failed", validation_context={},
            validation_feedback={"decision": "rejected", "gate": "independent_recalculation",
                                 "metric_mismatches": feedback["metric_mismatches"]},
            format_repair={},
        )["repair_request"]["validation_feedback"]
        self.assertEqual(mismatch_prompt["metric_mismatches"],
                         [feedback["metric_mismatches"][0]])
        serialized = json.dumps(prompt, ensure_ascii=False)
        self.assertNotIn(secondary["finding"], serialized)
        self.assertNotIn(warning["finding"], serialized)
        self.assertNotIn("DEFERRED-FEEDBACK-LEAK-MARKER", serialized)
        self.assertNotIn("Malformed response: " + json.dumps(feedback), serialized)
        self.assertIn("deferred scientific findings remain recorded",
                      prompt["format_repair"]["previous_error"])
        self.assertIn("resolve only the single active issue", prompt["instructions"])
        prior_issues = _compact_prior_blocking_issues(feedback)
        self.assertEqual(len(prior_issues), 5)
        self.assertIn("The interval is not independently checked.",
                      json.dumps(prior_issues, ensure_ascii=False))
        self.assertIn("prior-check-claim_support",
                      json.dumps(prior_issues, ensure_ascii=False))

        detailed_check = {
            **feedback["failed_checks"][0], "severity": "blocking",
            "finding": "The check's measured input is absent.",
            "required_change": "Measure the declared input.",
        }
        check_only = authoring_patch_prompt(
            brief={}, required_intent={}, configured_input={},
            candidate={"executor_source": "x", "validator_source": "y", "experiment_intent": {}},
            feedback="failed check", validation_context={},
            validation_feedback={"decision": "rejected",
                                 "failed_checks": [detailed_check, feedback["failed_checks"][1]]},
            format_repair={},
        )["repair_request"]["validation_feedback"]
        self.assertEqual(check_only["failed_checks"], [detailed_check])
        self.assertEqual(check_only["deferred_failed_check_ids"], ["independent_validation"])
        self.assertEqual(check_only["repair_scope"]["active_issue"], "failed_check")












    def test_repair_source_context_preserves_complete_bytes_and_physical_indices(self):
        for newline in ["\n", "\r\n", "\r"]:
            for separator in ["\u2028", "\u2029", "\x85", "\v", "\f"]:
                with self.subTest(newline=repr(newline), separator=repr(separator)):
                    source = newline.join([
                        "label = 'before" + separator + "after'", "",
                        "def measure():", "    return 1", "",
                        "def measure():", "    return 2", "",
                    ])
                    context = _source_patch_context(source)
                    self.assertEqual(context["source"], source)
                    self.assertTrue(context["source_complete"])
                    self.assertEqual(context["source_sha256"], hashlib.sha256(source.encode()).hexdigest())
                    self.assertEqual(context["duplicate_definitions"],
                                     [{"name": "measure", "line_starts": [3, 6]}])

    def test_repair_prompt_includes_large_complete_source_without_feedback_selection(self):
        source = ("def collect():\n    padding = '" + "x" * 50000 + "'\n"
                  "    output = {'unrequested': True}\n    return output\n")
        candidate = {"executor_source": source, "validator_source": source,
                     "experiment_intent": {"id": "study"}}
        prompt = authoring_patch_prompt(
            brief={}, required_intent={}, configured_input={}, candidate=candidate,
            feedback="unknown failure", validation_context={}, validation_feedback={},
            format_repair={})
        for context in prompt["current_candidate"]["source_context"].values():
            self.assertEqual(context["source"], source)
            self.assertEqual(context["characters"], len(source))
            self.assertTrue(context["source_complete"])
        self.assertEqual(json.loads(json.dumps(prompt))["current_candidate"]["source_context"]
                         ["executor_source"]["source"], source)

    def test_repair_source_context_preserves_invalid_source_for_syntax_repair(self):
        source = "def incomplete(:\n    return 1\n"
        context = _source_patch_context(source)
        self.assertEqual(context["source"], source)
        self.assertEqual(context["syntax_error"]["line"], 1)

    def test_candidate_failure_is_not_attached_to_a_different_revision(self):
        candidate = {"executor_source": "def run(): return 1", "validator_source": "def check(): pass",
                     "experiment_intent": {"id": "study"}}
        state = {"candidate_failure": {"error": "old candidate failure"},
                 "candidate_failure_sha256": _authored_candidate_sha256(candidate)}
        self.assertEqual(_retained_candidate_failure(state, candidate)["error"], "old candidate failure")
        revised = {**candidate, "executor_source": "def run(): return 2"}
        self.assertEqual(_retained_candidate_failure(state, revised), {})

    def test_candidate_failure_restores_only_exact_recorded_legacy_output_failure(self):
        candidate = {"executor_source": "def run(): return 1", "validator_source": "def check(): pass",
                     "experiment_intent": {"id": "study"}, "runtime": {"python": "3"},
                     "test_input": {"conditions": [1]}}
        details = {"repair_kind": "executor_output_contract", "previous_error": "undocumented field",
                   "required_fields": ["observations"], "observed_fields": ["observations", "extra"],
                   "missing_fields": [], "unexpected_fields": ["extra"]}
        fingerprint = _authored_candidate_sha256(candidate)
        current = {"experiment_intent": candidate["experiment_intent"],
                   "source_context": {key: _source_patch_context(candidate[key])
                                      for key in ("executor_source", "validator_source")}}
        state = {"last_attempt": candidate,
                 "repair_ledger": [{"candidate_sha256": hashlib.sha256(canonical_bytes(candidate)).hexdigest(),
                                    "gate": "program_output_contract", "error": "undocumented field"}],
                 "requests": [{"prompt": json.dumps({"candidate_sha256": fingerprint,
                                                       "current_candidate": current,
                                                       "format_repair": details})}]}
        self.assertEqual(_retained_candidate_failure(state, candidate)["unexpected_fields"], ["extra"])
        for newline in ["\n", "\r\n", "\r"]:
            with self.subTest(legacy_newline=repr(newline)):
                legacy_candidate = {**candidate, "executor_source": "def run():" + newline + "    return 1" + newline}
                legacy = deepcopy(state)
                legacy["last_attempt"] = legacy_candidate
                legacy["repair_ledger"][0]["candidate_sha256"] = hashlib.sha256(canonical_bytes(legacy_candidate)).hexdigest()
                context = {"experiment_intent": legacy_candidate["experiment_intent"], "source_context": {
                    key: {"source_sha256": hashlib.sha256(legacy_candidate[key].encode()).hexdigest(),
                          "sections": [{"line_start": 1, "line_end": len(legacy_candidate[key].splitlines()),
                                        "source": "\n".join(legacy_candidate[key].splitlines())}]}
                    for key in ("executor_source", "validator_source")}}
                request = {"candidate_sha256": _authored_candidate_sha256(legacy_candidate),
                           "current_candidate": context, "format_repair": details}
                legacy["requests"][0]["prompt"] = json.dumps(request)
                self.assertEqual(_retained_candidate_failure(legacy, legacy_candidate)["unexpected_fields"], ["extra"])
                request["current_candidate"]["source_context"]["executor_source"]["sections"][0]["source"] += "forged"
                legacy["requests"][0]["prompt"] = json.dumps(request)
                self.assertEqual(_retained_candidate_failure(legacy, legacy_candidate), {})
        mismatched = deepcopy(state)
        mismatched["requests"][0]["prompt"] = json.dumps({"candidate_sha256": "0" * 64,
                                                          "format_repair": details})
        self.assertEqual(_retained_candidate_failure(mismatched, candidate), {})
        conflicting = deepcopy(state)
        request = json.loads(conflicting["requests"][0]["prompt"])
        request["current_candidate"]["source_context"]["executor_source"]["source_sha256"] = "0" * 64
        conflicting["requests"][0]["prompt"] = json.dumps(request)
        self.assertEqual(_retained_candidate_failure(conflicting, candidate), {})
        stale = deepcopy(state)
        stale["repair_ledger"].append({**state["repair_ledger"][0], "gate": "scientific_review"})
        self.assertEqual(_retained_candidate_failure(stale, candidate), {})
        malformed = deepcopy(state)
        malformed["requests"] = [{"prompt": "[]"}, {"prompt": "invalid"}]
        self.assertEqual(_retained_candidate_failure(malformed, candidate), {})

    def test_patch_prompt_preserves_exact_scoped_evidence_frontier(self):
        frontier = {"receipt_ref": "artifact:methods-evidence/receipt@1",
                    "origin_science_sha256": "a" * 64, "current_science_sha256": "b" * 64,
                    "request": {"requested_actions": ["derive " + "x" * 2500]},
                    "notes": [{"action_disposition": "superseded", "content": "analysis " + "y" * 5000}]}
        digest = hashlib.sha256(canonical_bytes(frontier)).hexdigest()
        brief = {"repair_evidence_frontier": frontier, "repair_evidence_frontier_sha256": digest}
        candidate = {"executor_source": "def run(): return 1", "validator_source": "def check(): pass",
                     "experiment_intent": {"id": "study"}}
        def prompt(value):
            return authoring_patch_prompt(
                brief=value, required_intent={}, configured_input={}, candidate=candidate,
                feedback="repair", validation_context={}, validation_feedback={}, format_repair={})
        result = json.loads(json.dumps(prompt(brief)))["repair_request"]
        self.assertEqual(result["repair_evidence_frontier"], frontier)
        self.assertEqual(result["repair_evidence_frontier_sha256"], digest)
        with self.assertRaisesRegex(ValidationError, "fingerprint does not match"):
            prompt({**brief, "repair_evidence_frontier_sha256": "0" * 64})
        with self.assertRaisesRegex(ValidationError, "frontier must be an object"):
            prompt({"repair_evidence_frontier": []})

    def test_patch_prompt_preserves_exact_topic_review_closure(self):
        obligations = [{"obligation": {"attempt_lineage": {
            "verifier_execution_ref": "artifact:topic/verifier@2",
            "verifier_execution_sha256": "a" * 64,
            "research_question": "question " + "\u03b1" * 2500,
            "blocking_findings": ["evidence " + "x" * 5000]}},
            "reviewed_closure": {"receipt_ref": "artifact:survey/fulfillment@1",
                                 "body_sha256": "b" * 64, "status": "resolved"}}]
        digest = hashlib.sha256(canonical_bytes(obligations)).hexdigest()
        brief = {"topic_review_obligations": obligations,
                 "topic_review_obligations_sha256": digest}
        candidate = {"executor_source": "def run(): return 1", "validator_source": "def check(): pass",
                     "experiment_intent": {"id": "study"}}
        def prompt(value):
            return authoring_patch_prompt(
                brief=value, required_intent={}, configured_input={}, candidate=candidate,
                feedback="repair", validation_context={}, validation_feedback={}, format_repair={})
        result = json.loads(json.dumps(prompt(brief)))["repair_request"]
        self.assertEqual(result["topic_review_obligations"], obligations)
        self.assertEqual(result["topic_review_obligations_sha256"], digest)
        self.assertEqual(brief["topic_review_obligations"], obligations)
        with self.assertRaisesRegex(ValidationError, "fingerprint does not match"):
            prompt({**brief, "topic_review_obligations_sha256": "0" * 64})
        for invalid in [{}, ["unreviewed"]]:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValidationError, "list of objects"):
                prompt({"topic_review_obligations": invalid})

    def test_duplicate_structure_patch_preserves_unicode_strings_and_physical_lines(self):
        for newline in ["\n", "\r\n", "\r"]:
            with self.subTest(newline=repr(newline)):
                source = newline.join([
                    "label = 'before\u2028after'", "",
                    "def measure():", "    return 1", "",
                    "def measure():", "    return 2", "",
                ])
                context = _source_patch_context(source)
                previous = self._payload()
                previous["executor_source"] = source
                revised = apply_authoring_patch(previous, {"updates": {"executor_source": {
                    "source_sha256": context["source_sha256"],
                    "remove_duplicate_definitions": [{"name": "measure", "keep_line_start": 6}],
                }}})
                result = revised["executor_source"]
                self.assertIn("label = 'before\u2028after'" + newline, result)
                self.assertIn("def measure():" + newline + "    return 2", result)
                self.assertNotIn("return 1", result)
                self.assertEqual(result.count("def measure():"), 1)


    def test_duplicate_structure_patch_removes_only_model_selected_declarations(self):
        source = (
            "def main():\n    return None\n\n"
            "def measure():\n    return 1\n\n"
            "def unrelated():\n    return 'retain me'\n\n"
            "def measure():\n    return 2\n\n"
            "if __name__ == '__main__':\n    main()\n\n"
            "if __name__ == '__main__':\n    main()\n"
        )
        context = _source_patch_context(source)
        duplicate = next(item for item in context["duplicate_definitions"]
                         if item["name"] == "measure")
        self.assertEqual(len(duplicate["line_starts"]), 2)
        self.assertEqual(len(context["entry_guard_line_starts"]), 2)
        self.assertEqual(context["source"], source)
        self.assertTrue(context["source_complete"])
        prompt = authoring_patch_prompt(
            brief={}, required_intent={}, configured_input={},
            candidate={"executor_source": source,
                       "validator_source": "def validate():\n    return True\n",
                       "experiment_intent": {}},
            feedback="remove duplicate definitions", validation_context={},
            validation_feedback={}, format_repair={},
        )
        self.assertIn("remove_duplicate_definitions",
                      prompt["output_contract"]["updates"]["executor_source"])
        self.assertEqual(
            prompt["current_candidate"]["source_context"]["executor_source"]
            ["duplicate_definitions"], context["duplicate_definitions"])

        patch = {
            "source_sha256": context["source_sha256"],
            "remove_duplicate_definitions": [{
                "name": "measure", "keep_line_start": duplicate["line_starts"][1],
            }],
            "keep_entry_guard_line_start": context["entry_guard_line_starts"][0],
        }
        repaired = apply_authoring_patch(
            {"executor_source": source, "validator_source": "def validate():\n    return True\n",
             "experiment_intent": {}},
            {"updates": {"executor_source": patch}},
        )["executor_source"]

        self.assertIn("def measure():\n    return 2", repaired)
        self.assertNotIn("def measure():\n    return 1", repaired)
        self.assertIn("def unrelated():\n    return 'retain me'", repaired)
        self.assertEqual(repaired.count("if __name__ == '__main__':"), 1)
        self.assertEqual(_source_patch_context(repaired)["duplicate_definitions"], [])
        self.assertEqual(len(_source_patch_context(repaired)["entry_guard_line_starts"]), 1)
        from scisaurus.runtime.program_admission import scan_program_source
        scan_program_source(repaired, "program executor")

    def test_duplicate_entry_guard_patch_compares_complete_bodies_in_either_order(self):
        source = (
            "def first():\n    return 1\n\n"
            "def second():\n    return 2\n\n"
            "if '__main__' == __name__:\n    first()\n\n"
            "if __name__ == '__main__':\n    second()\n"
        )
        context = _source_patch_context(source)
        self.assertEqual(len(context["entry_guard_line_starts"]), 2)
        self.assertEqual(context["source"], source)
        self.assertTrue(context["source_complete"])
        prompt = authoring_patch_prompt(
            brief={}, required_intent={}, configured_input={},
            candidate={"executor_source": source, "validator_source": "def check(): pass",
                       "experiment_intent": {}},
            feedback="duplicate main guards", validation_context={},
            validation_feedback={}, format_repair={},
        )
        serialized = json.dumps(prompt)
        self.assertIn("entry_guard_line_starts", serialized)
        self.assertIn("first()", serialized)
        self.assertIn("second()", serialized)
        patch = {
            "source_sha256": context["source_sha256"],
            "remove_duplicate_definitions": [],
            "keep_entry_guard_line_start": context["entry_guard_line_starts"][0],
        }
        repaired = apply_authoring_patch(
            {"executor_source": source, "validator_source": "def check(): pass",
             "experiment_intent": {}},
            {"updates": {"executor_source": patch}},
        )["executor_source"]
        self.assertIn("first()", repaired)
        self.assertNotIn("    second()\n", repaired)
        from scisaurus.runtime.program_admission import scan_program_source
        scan_program_source(repaired, "program executor")

    def test_duplicate_entry_guard_patch_compares_large_complete_source(self):
        long_body = "    value = " + repr("x" * 24500) + "\n    consume(value)\n"
        source = (
            "if '__main__' == __name__:\n" + long_body + "\n"
            "if __name__ == '__main__':\n" + long_body
        )
        context = _source_patch_context(source)
        self.assertEqual(context["source"], source)
        patch = {
            "source_sha256": context["source_sha256"],
            "remove_duplicate_definitions": [],
            "keep_entry_guard_line_start": context["entry_guard_line_starts"][0],
        }
        revised = apply_authoring_patch(
            {"executor_source": source, "validator_source": "def check(): pass",
             "experiment_intent": {}}, {"updates": {"executor_source": patch}})
        self.assertEqual(revised["executor_source"].count("consume(value)"), 1)

    def test_duplicate_structure_patch_rejects_stale_hash_and_nonduplicate_targets(self):
        source = "def keep():\n    return 1\n\ndef keep():\n    return 2\n"
        context = _source_patch_context(source)
        starts = context["duplicate_definitions"][0]["line_starts"]
        base = {"executor_source": source, "validator_source": "def validate(): pass",
                "experiment_intent": {}}
        stale_patch = {
            "source_sha256": "0" * 64,
            "remove_duplicate_definitions": [{"name": "keep", "keep_line_start": starts[0]}],
        }
        with self.assertRaisesRegex(ValidationError, "fingerprint does not match"):
            apply_authoring_patch(base, {"updates": {"executor_source": stale_patch}})

        invalid_target = {
            "source_sha256": context["source_sha256"],
            "remove_duplicate_definitions": [{"name": "missing", "keep_line_start": starts[0]}],
        }
        with self.assertRaisesRegex(ValidationError, "existing duplicated top-level definition"):
            apply_authoring_patch(base, {"updates": {"executor_source": invalid_target}})

        arbitrary_range = {
            "source_sha256": context["source_sha256"],
            "remove_duplicate_definitions": [{"name": "keep", "keep_line_start": starts[0]}],
            "delete_lines": [1, 2],
        }
        with self.assertRaisesRegex(ValidationError, "only its source fingerprint"):
            apply_authoring_patch(base, {"updates": {"executor_source": arbitrary_range}})

    def test_duplicate_structure_patch_is_bounded(self):
        self.assertEqual(AUTHOR_PATCH_MAX_STRUCTURAL_REMOVALS, 8)
        names = [f"def f{index}():\n    return 1\n" for index in range(9)]
        source = "\n".join(names + names)
        context = _source_patch_context(source)
        patch = {
            "source_sha256": context["source_sha256"],
            "remove_duplicate_definitions": [
                {"name": item["name"], "keep_line_start": item["line_starts"][0]}
                for item in context["duplicate_definitions"]
            ],
        }
        with self.assertRaisesRegex(ValidationError, "bounded removal limit"):
            apply_authoring_patch(
                {"executor_source": source, "validator_source": "def validate(): pass",
                 "experiment_intent": {}},
                {"updates": {"executor_source": patch}},
            )

    def test_duplicate_structure_patch_compares_large_complete_source(self):
        body = "    payload = " + repr("x" * 24500) + "\n    return payload\n"
        source = "def calculate():\n" + body + "\ndef calculate():\n" + body
        context = _source_patch_context(source)
        self.assertEqual(context["source"], source)
        starts = context["duplicate_definitions"][0]["line_starts"]
        patch = {
            "source_sha256": context["source_sha256"],
            "remove_duplicate_definitions": [{
                "name": "calculate", "keep_line_start": starts[0],
            }],
        }
        revised = apply_authoring_patch(
            {"executor_source": source, "validator_source": "def validate(): pass",
             "experiment_intent": {}}, {"updates": {"executor_source": patch}})
        self.assertEqual(revised["executor_source"].count("def calculate():"), 1)

    def test_validation_feedback_is_bound_to_the_candidate_that_was_reviewed(self):
        candidate = {"executor_source": "def run(): return 1", "validator_source": "def check(): return 1",
                     "experiment_intent": {"id": "study"}}
        fingerprint = _authored_candidate_sha256(candidate)
        feedback = {"decision": "rejected", "findings": [{"finding": "specific defect"}]}
        state = {"validation_feedback": feedback,
                 "validation_feedback_candidate_sha256": fingerprint}
        self.assertEqual(_candidate_bound_value(
            state, candidate, "validation_feedback",
            "validation_feedback_candidate_sha256"), feedback)
        revised = {**candidate, "executor_source": "def run(): return 2"}
        self.assertEqual(_candidate_bound_value(
            state, revised, "validation_feedback",
            "validation_feedback_candidate_sha256"), {})
        legacy = {"validation_feedback": feedback}
        self.assertEqual(_candidate_bound_value(
            legacy, candidate, "validation_feedback",
            "validation_feedback_candidate_sha256"), {})

    def test_prior_repair_feedback_is_not_itself_a_repeated_result(self):
        error = ValidationError("program author response was incomplete")
        self.assertFalse(_is_repeated_repair_failure(error, [], []))
        self.assertTrue(_is_repeated_repair_failure(error, [str(error)], []))
        self.assertFalse(_is_repeated_repair_failure(
            error, [str(error)], [], seed_replay=True))

    def test_malformed_response_signature_is_stable_across_finish_paths(self):
        length_signature = _author_response_format_failure_signature(None, "length")
        stopped_signature = _author_response_format_failure_signature(None, "stop")
        self.assertEqual(length_signature, stopped_signature)
        self.assertTrue(_is_repeated_repair_failure(
            ValidationError("malformed JSON"), [], [length_signature], stopped_signature))
        self.assertNotEqual(
            _author_response_format_failure_signature({"partial": True}, "length"),
            _author_response_format_failure_signature({"partial": True}, "stop"),
        )
        self.assertNotEqual(
            _author_response_format_failure_signature(None, "length", route_index=0),
            _author_response_format_failure_signature(None, "length", route_index=1),
        )

    def test_author_retry_diagnostic_matches_the_actual_source_patch_failure(self):
        ambiguous = _author_format_repair_instructions(
            "executor_source edit old text must match exactly once; observed 2 occurrences",
            has_candidate=True)
        self.assertIn("valid JSON", ambiguous)
        self.assertIn("recorded match locations", ambiguous)
        self.assertIn("unique", ambiguous)
        truncated = _author_format_repair_instructions(
            "program author response was incomplete (finish_reason=length)",
            has_candidate=True)
        self.assertIn("truncated", truncated)
        self.assertIn("bounded patch contract", truncated)

    def test_author_request_signature_prevents_an_unchanged_route_replay(self):
        prompt = '{"assignment":"repair_existing_experiment_candidate"}'
        signature = _author_request_signature("glm-5.3-flash:cloud", 4096, prompt)
        state = {"requests": [{"request_signature": signature}]}
        self.assertTrue(_author_request_was_attempted(state, signature))
        self.assertFalse(_author_request_was_attempted(
            state, _author_request_signature("glm-5.3:cloud", 4096, prompt)))
        self.assertFalse(_author_request_was_attempted(
            state, _author_request_signature("glm-5.3-flash:cloud", 4096, prompt + " ")))

    def test_author_json_prefix_classifier_rejects_narrative_and_accepts_real_prefixes(self):
        self.assertEqual(_author_json_prefix_state("Let me carefully parse this task."), "not_json")
        self.assertEqual(_author_json_prefix_state('{"experiment_intent":'), "incomplete")
        self.assertEqual(_author_json_prefix_state('{"source":"unfinished'), "incomplete")
        self.assertEqual(_author_json_prefix_state('{"ok":true}'), "complete")
        self.assertEqual(_author_json_prefix_state('{"ok": nope'), "invalid")

    def test_format_repair_without_candidate_requests_full_artifact_not_source_edits(self):
        full_generation = _author_format_repair_instructions(
            "length-limited experiment-author response was not a valid JSON object prefix")
        self.assertIn("complete authoring assignment", full_generation)
        self.assertIn("only experiment_intent and executor_source", full_generation)
        self.assertIn("Do not return edits", full_generation)
        self.assertIn("bounded patch contract", _author_format_repair_instructions(
            "program author response was incomplete (finish_reason=length)",
            has_candidate=True))

    def test_prose_at_output_limit_skips_suffix_calls_and_reauthors_full_program(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            payload = self._payload()

            class NarrativeThenProgramAuthor:
                def __init__(inner_self):
                    inner_self.calls = 0
                    inner_self.prompts = []

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    request = json.loads(prompt)
                    inner_self.prompts.append(request)
                    if inner_self.calls == 1:
                        return ModelResult(
                            "Let me carefully parse this task. " * 2000,
                            "author", {"model_calls": 1}, 1.0, "length")
                    return ModelResult(
                        json.dumps(payload), "author", {"model_calls": 1}, 1.0, "stop")

            author = NarrativeThenProgramAuthor()
            progress = []
            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=self._cache(Path(path)),
                on_progress=lambda phase, state: progress.append((phase, state)))

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 2)
        self.assertTrue(all(
            request.get("operation") != "continue_truncated_response"
            for request in progress[-1][1]["requests"]))
        repair_prompt = author.prompts[1]
        self.assertEqual(repair_prompt["assignment"], "author_experiment_program")
        self.assertIn("complete authoring assignment",
                      repair_prompt["format_repair"]["instructions"])
        self.assertEqual(
            repair_prompt["author_response_contract_version"], "experiment-author-json-v3")

    def test_unknown_author_continuation_is_not_replayed_and_full_generation_recovers(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            payload = self._payload()
            complete_response = json.dumps(payload, separators=(",", ":"))

            class InterruptedContinuationAuthor:
                def __init__(inner_self):
                    inner_self.calls = 0
                    inner_self.prompts = []

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    request = json.loads(prompt)
                    inner_self.prompts.append(request)
                    if inner_self.calls == 1:
                        return ModelResult(
                            complete_response[:120], "author",
                            {"model_calls": 1}, 0.1, "length")
                    if request.get("assignment") == "continue_truncated_experiment_author_json":
                        raise RuntimeError("simulated unknown continuation outcome")
                    return ModelResult(
                        json.dumps(payload), "author", {"model_calls": 1}, 0.1, "stop")

            author = InterruptedContinuationAuthor()
            with self.assertRaisesRegex(RuntimeError, "unknown continuation outcome"):
                foundry.generate("bounded comparison", client=author, work_cache=cache)

            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=cache)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 3)
        self.assertEqual(sum(
            prompt.get("assignment") == "continue_truncated_experiment_author_json"
            for prompt in author.prompts), 1)
        self.assertEqual(author.prompts[2]["assignment"], "author_experiment_program")
        self.assertIn("complete authoring assignment",
                      author.prompts[2]["format_repair"]["instructions"])

    def test_unknown_initial_author_request_recovers_with_distinct_full_generation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            payload = self._payload()

            class UnknownThenProgramAuthor:
                def __init__(inner_self):
                    inner_self.calls = 0
                    inner_self.prompts = []

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    request = json.loads(prompt)
                    inner_self.prompts.append(request)
                    if inner_self.calls == 1:
                        raise RuntimeError("simulated unknown initial author outcome")
                    return ModelResult(
                        json.dumps(payload), "author", {"model_calls": 1}, 0.1, "stop")

            author = UnknownThenProgramAuthor()
            with self.assertRaisesRegex(RuntimeError, "unknown initial author outcome"):
                foundry.generate("bounded comparison", client=author, work_cache=cache)

            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=cache)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 2)
        self.assertEqual(author.prompts[1]["assignment"], "author_experiment_program")
        self.assertIn("complete authoring assignment",
                      author.prompts[1]["format_repair"]["instructions"])

    def test_author_request_history_migrates_legacy_completed_requests(self):
        prompt = '{"assignment":"repair_existing_experiment_candidate"}'
        state = {"requests": [{
            "role": "research.experiment-author", "model": "glm-5.3-flash:cloud",
            "max_output_tokens": 8192, "prompt": prompt,
        }]}
        signature = _author_request_signature("glm-5.3-flash:cloud", 8192, prompt)
        self.assertTrue(_author_request_was_attempted(state, signature))

    def test_prior_blockers_are_required_review_checks_and_failed_rechecks_stay_blocking(self):
        from scisaurus.runtime.capability_foundry import validate_program_review
        issue = _compact_prior_blocking_issues({"findings": [{
            "severity": "blocking", "finding": "The measured response is imposed by the equation.",
            "evidence": "The executor directly assigns the outcome from the control parameter.",
            "required_change": "Use an independently measured response or narrow the claim.",
        }]})[0]
        review = self._review_payload()
        with self.assertRaisesRegex(ValidationError, issue["review_check_id"]):
            validate_program_review(review, prior_blocking_issues=[issue])

        review["checks"].append({
            "id": issue["review_check_id"], "outcome": "failed",
            "evidence": "The revised executor still assigns the outcome from the parameter.",
        })
        review["status"] = "rejected"
        validated = validate_program_review(review, prior_blocking_issues=[issue])
        self.assertTrue(any(
            item["severity"] == "blocking"
            and item["finding"] == issue["finding"]
            and item["required_change"] == issue["required_change"]
            for item in validated["findings"]))

    def test_blocking_issue_ledger_survives_intervening_gate_failures_without_truncation(self):
        findings = [{
            "severity": "blocking",
            "finding": f"Independent defect {index} remains in the candidate.",
            "evidence": f"Candidate evidence {index}.",
            "required_change": f"Repair defect {index} and verify it independently.",
        } for index in range(15)]
        state = {}
        initial = _retain_prior_blocking_issues(state, {"findings": findings})
        self.assertEqual(len(initial), 15)

        recalculation_failure = {
            "gate": "independent_recalculation", "decision": "rejected",
            "failed_checks": [{
                "id": "positive_observation", "outcome": "failed",
                "evidence": "The revised candidate still has no positive observation.",
            }],
        }
        retained = _retain_prior_blocking_issues(state, recalculation_failure)
        self.assertEqual(len(retained), 16)
        self.assertEqual(
            {item["finding"] for item in retained},
            {item["finding"] for item in initial}
            | {"Prior check positive_observation failed"},
        )
        self.assertEqual(
            len(_retain_prior_blocking_issues(state, {"findings": findings})), 16)

        single_state = {}
        single = _retain_prior_blocking_issues(
            single_state, {"findings": [findings[0]]})
        repeated_feedback = {
            "findings": [findings[0]],
            "failed_checks": [{
                "id": single[0]["review_check_id"], "outcome": "failed",
                "evidence": "The reviewer rechecked this same issue and it remains unresolved.",
            }],
        }
        merged = _retain_prior_blocking_issues(single_state, repeated_feedback)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["review_check_id"], single[0]["review_check_id"])
        self.assertIn("remains unresolved", merged[0]["evidence"])

    def test_replacing_gate_feedback_does_not_drop_the_previous_gate_blocker(self):
        state = {"validation_feedback": {"findings": [{
            "severity": "blocking", "finding": "The outcome is imposed by the model equation.",
            "evidence": "The executor assigns the outcome from the input parameter.",
            "required_change": "Measure an outcome that is independent of the imposed equation.",
        }]}}
        later_feedback = {
            "gate": "independent_recalculation", "decision": "rejected",
            "failed_checks": [{
                "id": "positive_observation", "outcome": "failed",
                "evidence": "No observation is positive.",
            }],
        }

        ledger = _record_program_gate_feedback(state, later_feedback)

        self.assertEqual(state["validation_feedback"], later_feedback)
        self.assertEqual(len(ledger), 2)
        self.assertEqual(
            {item["finding"] for item in ledger},
            {"The outcome is imposed by the model equation.",
             "Prior check positive_observation failed"},
        )

    def test_blocking_issue_reconciliation_clears_only_verified_issues(self):
        from scisaurus.runtime.capability_foundry import validate_program_review

        prior = _compact_prior_blocking_issues({"findings": [{
            "severity": "blocking", "finding": f"Prior defect {index}.",
            "evidence": f"Observed evidence {index}.",
            "required_change": f"Resolve defect {index} with independent evidence.",
        } for index in range(15)]})
        review = self._review_payload()
        review["checks"].extend({
            "id": issue["review_check_id"],
            "outcome": "failed" if index == 4 else "passed",
            "evidence": f"Rechecked prior issue {index}.",
        } for index, issue in enumerate(prior))
        review["checks"][0].update(
            outcome="failed", evidence="The primary mechanism remains unmeasured.")
        review["findings"].append({
            "severity": "blocking", "finding": "A new independent defect remains.",
            "evidence": "The measured outcome is still imposed by the executor.",
            "required_change": "Measure the outcome independently from the executor formula.",
        })
        review["status"] = "rejected"
        review = validate_program_review(review, prior_blocking_issues=prior)

        remaining = _reconcile_prior_blocking_issues(prior, review)
        identifiers = {item["review_check_id"] for item in remaining}
        self.assertIn(prior[4]["review_check_id"], identifiers)
        self.assertEqual(len(identifiers), 3)
        self.assertNotIn(prior[3]["review_check_id"], identifiers)
        self.assertIn("A new independent defect remains.",
                      {item["finding"] for item in remaining})

    def test_seeded_candidate_preserves_format_progress_and_author_budget_across_provenance(self):
        payload = self._payload()
        rejected = self._review_payload()
        rejected["status"] = "rejected"
        rejected["checks"][0].update(outcome="failed", evidence="The primary claim is imposed.")
        rejected["findings"] = [{
            "severity": "blocking", "finding": "The contrast is algebraic by construction.",
            "evidence": "The down branch is the up branch times a parameter factor.",
            "required_change": "Reframe the claim or define an independent mechanism.",
        }]

        class SequencedReviewer:
            calls = 0
            prior_issues_seen = None
            prior_prompt = None

            def complete(inner_self, *, system, prompt):
                inner_self.calls += 1
                request = json.loads(prompt)
                if inner_self.calls > 1:
                    inner_self.prior_issues_seen = request.get("prior_blocking_issues")
                    inner_self.prior_prompt = request
                value = rejected if inner_self.calls == 1 else self._review_payload()
                value = _add_prior_review_checks(value, prompt)
                return ModelResult(json.dumps(value), "reviewer", {"model_calls": 1}, 0.0, "stop")

        class SequencedAuthor:
            def __init__(inner_self):
                inner_self.responses = [
                    ModelResult(json.dumps(payload), "author", {"model_calls": 1}, 0.0, "stop"),
                    ModelResult('{"updates":', "author", {"model_calls": 1}, 0.0, "length"),
                    ModelResult('{"updates":', "author", {"model_calls": 1}, 0.0, "length"),
                ]
                inner_self.calls = 0
                inner_self.output_limits = []
                inner_self.prompts = []
                inner_self.max_output_tokens = 24000

            def complete(inner_self, *, system, prompt):
                inner_self.calls += 1
                inner_self.output_limits.append(inner_self.max_output_tokens)
                inner_self.prompts.append(prompt)
                request = json.loads(prompt)
                if request.get("assignment") == "continue_truncated_experiment_author_json":
                    return ModelResult(json.dumps({
                        "marker": "wrong-continuation-marker",
                        "continuation": "}",
                    }), "author", {"model_calls": 1}, 0.0, "stop")
                return inner_self.responses.pop(0)

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            foundry = self._foundry(root)
            reviewer = SequencedReviewer()
            foundry.reviewer_client = reviewer
            author = SequencedAuthor()
            with self.assertRaises(ModelWorkBlocked):
                foundry.generate("bounded comparison", client=author, work_cache=cache,
                                 repair_provenance={"review_revision": 1})

            foundry.max_attempts = 3
            with self.assertRaises(ModelWorkBlocked):
                foundry.generate("bounded comparison", client=author, work_cache=cache,
                                 repair_provenance={"review_revision": 2})

            progress = []
            with self.assertRaises(ModelWorkBlocked) as duplicate:
                foundry.generate("bounded comparison", client=author, work_cache=cache,
                                 repair_provenance={"review_revision": 3},
                                 on_progress=lambda phase, state: progress.append((phase, state)))

        self.assertIn("in 3 attempts", str(duplicate.exception))
        self.assertEqual(author.calls, 4)
        self.assertEqual(len(author.output_limits), author.calls)
        self.assertEqual(len(author.prompts), len(set(author.prompts)))
        self.assertEqual(reviewer.calls, 1)
        migrated_state = progress[-1][1]
        self.assertEqual(migrated_state["attempts"], 3)
        self.assertIn("truncated", migrated_state["format_repair"]["instructions"])
        self.assertGreaterEqual(len(migrated_state["model_diagnostics"]), 2)

    def _cache(self, root):
        control = ControlStore(root / "ledger")
        self.addCleanup(control.close)
        store = ArtifactStore(control)
        store.init_project(principal_note="foundry fixture")
        def publish(logical_id, artifact_type, body, author, **kwargs):
            return store.publish_artifact(logical_id=logical_id, artifact_type=artifact_type,
                author=author, body=canonical_bytes(body), media_type="application/json")
        return ModelWorkCache(store, publish, namespace="command/foundry-work")

    @staticmethod
    def _payload():
        return {
            "executor_source": MINI_EXECUTOR,
            "validator_source": MINI_VALIDATOR,
            "runtime": {"python": "3.14", "packages": [{"name": "numpy", "version": "2.5.2"}]},
            "test_input": {"probe": True},
            "experiment_intent": deepcopy(INTENT),
        }

    @staticmethod
    def _foundry(root):
        return CapabilityFoundry(
            {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
             "model": "stub", "timeout_seconds": 60, "max_output_tokens": 128},
            runtime_python=sys.executable, workspace_root=root / "workspace",
            registry_root=root / "registry", repo_root=ROOT,
            requirements_file=ROOT / "requirements-experiment.txt",
            runtime_packages=[("pip", version("pip"))], max_attempts=2,
            reviewer_client=StubClient(CapabilityFoundryTests._review_payload()),
            validator_client=StubClient({"validator_source": MINI_VALIDATOR}))

    def test_runtime_pin_mismatch_stops_before_authoring(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.runtime_packages = [("scisaurus-missing-fixture-package", "1.0")]
            client = StubClient(self._payload())
            with self.assertRaisesRegex(ValidationError, "runtime packages do not match"):
                foundry.generate("bounded comparison", client=client)
            self.assertEqual(client.calls, 0)

    def test_author_output_budget_rebalances_unused_input_reservation(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.model_config.update({
                "context_window_tokens": 262144,
                "max_input_tokens": 245760,
                "max_output_tokens": 8192,
            })
            route = foundry._model_config_for_role(
                "research.experiment-author", foundry.author_max_output_tokens)

        self.assertEqual(route["max_output_tokens"], 24000)
        self.assertEqual(route["max_input_tokens"], 238144)
        self.assertLessEqual(
            route["max_input_tokens"] + route["max_output_tokens"],
            route["context_window_tokens"],
        )

    def test_unset_foundry_model_timeout_inherits_configured_route_timeout(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.model_config["timeout_seconds"] = 1800
            route = foundry._model_config_for_role(
                "research.experiment-author", foundry.author_max_output_tokens)

        self.assertIsNone(foundry.model_timeout_seconds)
        self.assertEqual(route["timeout_seconds"], 1800.0)

    def test_author_format_failure_uses_peer_route_before_consuming_author_lease(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.model_config["role_models"] = {
                "research.experiment-author": {
                    "protocol": "openai_compatible", "base_url": "https://primary.invalid/v1",
                    "model": "primary-author", "context_window_tokens": 131072,
                    "max_input_tokens": 112000, "output_format": "json_object",
                },
            }
            foundry.model_config["role_model_fallbacks"] = {
                "research.experiment-author": [{
                    "protocol": "openai_compatible", "base_url": "https://fallback.invalid/v1",
                    "model": "fallback-author", "context_window_tokens": 131072,
                    "max_input_tokens": 112000, "output_format": "json_object",
                }, {
                    "protocol": "openai_compatible", "base_url": "https://premium.invalid/v1",
                    "model": "premium-author", "context_window_tokens": 131072,
                    "max_input_tokens": 112000, "output_format": "json_object",
                    "model_call_budget_path": str(root / "model-budgets.sqlite"),
                    "model_call_budget_key": "high-impact-pool",
                    "model_call_budget_limit": 20,
                }],
            }
            malformed = StubClient({"not": "an author envelope"})
            valid = StubClient(self._payload())
            with patch(
                    "scisaurus.runtime.capability_foundry.ModelClient",
                    side_effect=[malformed, valid]) as factory:
                outcome = foundry.generate(
                    "bounded comparison", client=None, work_cache=self._cache(root))
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(malformed.calls, 1)
            self.assertEqual(valid.calls, 1)
            self.assertEqual(
                [call.kwargs["model"] for call in factory.call_args_list],
                ["primary-author", "fallback-author"],
            )
            self.assertTrue(all(call.kwargs["max_output_tokens"] == 24000
                                for call in factory.call_args_list))

    def test_author_format_failure_uses_bulk_route_before_premium_fallback(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.model_config["role_models"] = {
                "research.experiment-author": {
                    "protocol": "openai_compatible", "base_url": "https://primary.invalid/v1",
                    "model": "primary-author", "context_window_tokens": 131072,
                    "max_input_tokens": 112000, "output_format": "json_object",
                },
            }
            foundry.model_config["role_model_fallbacks"] = {
                "research.experiment-author": [{
                    "protocol": "openai_compatible", "base_url": "https://flash.invalid/v1",
                    "model": "glm-flash-author", "context_window_tokens": 131072,
                    "max_input_tokens": 112000, "output_format": "json_object",
                }, {
                    "protocol": "openai_compatible", "base_url": "https://bulk.invalid/v1",
                    "model": "gemma-bulk-author", "context_window_tokens": 131072,
                    "max_input_tokens": 112000, "output_format": "json_object",
                }, {
                    "protocol": "openai_compatible", "base_url": "https://premium.invalid/v1",
                    "model": "premium-author", "context_window_tokens": 131072,
                    "max_input_tokens": 112000, "output_format": "json_object",
                    "model_call_budget_path": str(root / "model-budgets.sqlite"),
                    "model_call_budget_key": "high-impact-pool",
                    "model_call_budget_limit": 20,
                }],
            }
            primary = StubClient({"not": "an author envelope"})
            flash = StubClient({"still_not": "an author envelope"})
            bulk = StubClient(self._payload())
            with patch(
                    "scisaurus.runtime.capability_foundry.ModelClient",
                    side_effect=[primary, flash, bulk]) as factory:
                outcome = foundry.generate(
                    "bounded comparison", client=None, work_cache=self._cache(root))

            self.assertEqual(outcome["status"], "registered")
            self.assertEqual([primary.calls, flash.calls, bulk.calls], [1, 1, 1])
            self.assertEqual(
                [call.kwargs["model"] for call in factory.call_args_list],
                ["primary-author", "glm-flash-author", "gemma-bulk-author"],
            )

    def test_length_limited_author_response_continues_after_resume(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            complete_response = json.dumps(self._payload(), separators=(",", ":"))

            class ContinuationClient:
                def __init__(inner_self):
                    inner_self.calls = 0
                    inner_self.max_output_tokens = 24000
                    inner_self.output_format = "json_object"
                    inner_self.output_formats_seen = []

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    inner_self.output_formats_seen.append(inner_self.output_format)
                    if inner_self.calls == 1:
                        return ModelResult(
                            complete_response[:120], "author",
                            {"model_calls": 1, "completion_tokens": 40}, 0.1, "length")
                    request = json.loads(prompt)
                    prefix = request["partial_response"]
                    suffix_start = len(prefix)
                    suffix_end = suffix_start + (100 if inner_self.calls == 2 else 10_000_000)
                    suffix = complete_response[suffix_start:suffix_end]
                    return ModelResult(
                        suffix, "author", {"model_calls": 1, "completion_tokens": 80},
                        0.1, "stop")

            class SimulatedInterruption(RuntimeError):
                pass

            author = ContinuationClient()
            interrupted = False

            def interrupt_after_persisting_suffix(phase, state):
                nonlocal interrupted
                if phase == "author_response_continuation_received" and not interrupted:
                    interrupted = True
                    raise SimulatedInterruption("restart after durable continuation response")

            with self.assertRaises(SimulatedInterruption):
                foundry.generate(
                    "bounded comparison", client=author, work_cache=cache,
                    on_progress=interrupt_after_persisting_suffix)

            resumed_states = []
            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=cache,
                on_progress=lambda phase, state: resumed_states.append((phase, state)))

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 3)
        self.assertEqual(author.output_formats_seen, ["json_object", None, None])
        self.assertEqual(author.output_format, "json_object")
        ready_state = next(
            state for phase, state in reversed(resumed_states)
            if phase == "author_response_ready_for_validation")
        continuation_requests = [
            request for request in ready_state["requests"]
            if request.get("operation") == "continue_truncated_response"]
        self.assertEqual(len(continuation_requests), 2)
        self.assertTrue(all(request["status"] == "succeeded"
                            for request in continuation_requests))
        self.assertEqual(len({request["prefix_sha256"]
                              for request in continuation_requests}), 2)
        self.assertEqual(
            ready_state["author_response_continuation"]["status"], "completed")
        self.assertEqual(ready_state["author_response_continuation"]["continuations"], 2)

    def test_length_limited_narrative_prefix_repairs_without_continuing_invalid_text(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.max_attempts = 3
            cache = self._cache(root)

            class NarrativeThenJSONAuthor:
                def __init__(inner_self):
                    inner_self.prompts = []

                def complete(inner_self, *, system, prompt):
                    inner_self.prompts.append(json.loads(prompt))
                    if len(inner_self.prompts) == 1:
                        return ModelResult(
                            "Let me carefully parse this task.\\n```python\\n",
                            "author", {"model_calls": 1}, 0.1, "length")
                    return ModelResult(
                        json.dumps(self._payload(), separators=(",", ":")),
                        "author", {"model_calls": 1}, 0.1, "stop")

            author = NarrativeThenJSONAuthor()
            progress = []
            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=cache,
                model_call_budget=6,
                on_progress=lambda phase, state: progress.append((phase, state)))

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(len(author.prompts), 2)
        self.assertIn("format_repair", author.prompts[1])
        ready_state = next(
            state for phase, state in reversed(progress)
            if phase == "author_response_ready_for_validation")
        self.assertFalse(any(
            request.get("operation") == "continue_truncated_response"
            for request in ready_state["requests"]))
        rejected_prefix = next(
            state for phase, state in progress if phase == "author_response_not_json_prefix")
        self.assertEqual(
            rejected_prefix["author_response_continuation"]["status"],
            "format_repair_required")
        self.assertEqual(
            rejected_prefix["author_response_continuation"]["continuations"], 0)

    def test_missing_author_intent_reuses_sources_for_compact_metadata_repair(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            complete = self._payload()

            class PartialAuthor:
                def __init__(inner_self):
                    inner_self.prompts = []

                def complete(inner_self, *, system, prompt):
                    request = json.loads(prompt)
                    inner_self.prompts.append(request)
                    if len(inner_self.prompts) == 1:
                        response = {
                            "executor_source": complete["executor_source"],
                            "validator_source": complete["validator_source"],
                        }
                    else:
                        self.assertEqual(
                            request["assignment"], "repair_existing_experiment_candidate")
                        self.assertEqual(
                            request["current_candidate"]["experiment_intent"], {})
                        response = {"updates": {
                            "experiment_intent": complete["experiment_intent"],
                        }}
                    return ModelResult(
                        json.dumps(response), "author", {"model_calls": 1}, 0.0, "stop")

            author = PartialAuthor()
            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=cache)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(len(author.prompts), 2)
        self.assertEqual(
            outcome["candidate"]["executor_source"], complete["executor_source"])
        self.assertEqual(
            outcome["candidate"]["validator_source"], complete["validator_source"])
        self.assertEqual(
            outcome["candidate"]["experiment_intent"], complete["experiment_intent"])
        self.assertIn("experiment_intent", author.prompts[0]["authoring_output_order"])

    def test_legacy_truncated_response_survives_validation_contract_change(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            author = type("LegacyAuthor", (), {"model": "author"})()
            complete_response = json.dumps(self._payload(), separators=(",", ":"))
            runtime = foundry._runtime()
            assignment = candidate_prompt(
                "bounded comparison", foundry.runtime_packages, {"probe": True},
                runtime_version=runtime["python"])
            author_prompt = json.dumps(assignment, ensure_ascii=False, sort_keys=True)
            signature = _author_request_signature(
                author.model, foundry.author_max_output_tokens, author_prompt)
            cached_result = ModelResult(
                complete_response[:120], author.model,
                {"model_calls": 1}, 0.1, "length")
            cache.put("previous-validation-contract", {
                "status": "blocked", "attempts": 1, "assignment": assignment,
                "author_route_index": 0,
                "author_request_signatures": [signature],
                "requests": [{
                    "attempt": 1, "role": "research.experiment-author",
                    "status": "succeeded", "model": author.model,
                    "request_signature": signature,
                    "max_output_tokens": foundry.author_max_output_tokens,
                    "prompt": author_prompt,
                    "usage": {"model_calls": 1},
                }],
                "last_response": asdict(cached_result),
                "usage": {"model_calls": 1},
                "format_repair": {"previous_error": "response truncated"},
            })

            class ContinuationOnlyAuthor:
                model = "author"

                def __init__(inner_self):
                    inner_self.calls = 0

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    request = json.loads(prompt)
                    suffix = complete_response[len(request["partial_response"]):]
                    return ModelResult(json.dumps({
                        "marker": request["output_contract"]["legacy_json_envelope"]["marker"],
                        "continuation": suffix,
                    }), "author", {"model_calls": 1}, 0.1, "stop")

            continuation_author = ContinuationOnlyAuthor()
            outcome = foundry.generate(
                "bounded comparison", client=continuation_author,
                work_cache=cache)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(continuation_author.calls, 1)

    def test_length_limited_author_does_not_dispatch_after_deadline(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            complete_response = json.dumps(self._payload(), separators=(",", ":"))

            class ExpiringClient:
                def __init__(inner_self):
                    inner_self.calls = 0
                    inner_self.max_output_tokens = 24000
                    inner_self.timeout_seconds = 1800

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    foundry.deadline = time.monotonic() - 1
                    return ModelResult(
                        complete_response[:120], "author",
                        {"model_calls": 1}, 0.1, "length")

            author = ExpiringClient()
            progress = []
            with self.assertRaisesRegex(CapabilityDeadlineError, "mission deadline"):
                foundry.generate(
                    "bounded comparison", client=author, work_cache=cache,
                    deadline=time.monotonic() + 60,
                    on_progress=lambda phase, state: progress.append((phase, state)))

        self.assertEqual(author.calls, 1)
        final_state = progress[-1][1]
        self.assertEqual(final_state["usage"]["model_calls"], 1)
        self.assertFalse(any(
            item.get("operation") == "continue_truncated_response"
            for item in final_state["requests"]))

    def test_local_author_context_rejection_preserves_retry_without_charging(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry, cache = self._foundry(root), self._cache(root)
            class ContextRejectedAuthor:
                calls = 0
                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        raise ModelContextBudgetError("input cannot fit", model="author",
                            estimated_input_tokens=200, allowed_input_tokens=100,
                            context_window_tokens=120, max_input_tokens=100, max_output_tokens=20)
                    return ModelResult(json.dumps(self._payload()), "author", {"model_calls": 1}, .1, "stop")
            author, progress = ContextRejectedAuthor(), []
            with self.assertRaises(ModelContextBudgetError):
                foundry.generate("bounded comparison", client=author, work_cache=cache,
                    on_progress=lambda phase, state: progress.append(state))
            stopped = progress[-1]
            self.assertEqual(stopped["attempts"], 0)
            self.assertEqual(stopped["usage"]["model_calls"], 0)
            self.assertEqual(stopped["requests"][-1]["status"], "context_not_dispatched")
            self.assertEqual(stopped["requests"][-1]["context_budget"]["estimated_input_tokens"], 200)
            self.assertFalse(_author_request_was_attempted(stopped, stopped["requests"][-1]["request_signature"]))
            self.assertEqual(foundry.generate("bounded comparison", client=author, work_cache=cache)["status"], "registered")
            self.assertEqual(author.calls, 2)

    def test_local_review_context_rejection_keeps_candidate_and_response_lease(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry, cache = self._foundry(root), self._cache(root)
            class ContextRejectedReviewer:
                calls = 0
                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        raise ModelContextBudgetError("review cannot fit", model="reviewer",
                            estimated_input_tokens=200, allowed_input_tokens=100,
                            context_window_tokens=120, max_input_tokens=100, max_output_tokens=20)
                    return ModelResult(json.dumps(self._review_payload()), "reviewer", {"model_calls": 1}, .1, "stop")
            author, reviewer, progress = StubClient(self._payload()), ContextRejectedReviewer(), []
            foundry.reviewer_client = reviewer
            with self.assertRaises(ModelContextBudgetError):
                foundry.generate("bounded comparison", client=author, work_cache=cache,
                    on_progress=lambda phase, state: progress.append(state))
            stopped = progress[-1]
            self.assertEqual(stopped["requests"][-1]["status"], "context_not_dispatched")
            self.assertEqual(stopped["requests"][-1]["usage"]["model_calls"], 0)
            self.assertEqual(next(iter(stopped["scientific_reviews"].values()))["status"], "pending")
            self.assertEqual(foundry.generate("bounded comparison", client=author, work_cache=cache)["status"], "registered")
            self.assertEqual(author.calls, 1)
            self.assertEqual(reviewer.calls, 2)

    def test_local_review_continuation_context_rejection_can_resume_exact_suffix(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry, cache = self._foundry(root), self._cache(root)
            complete_response = json.dumps(self._review_payload())
            prefix = complete_response[:150]
            class ContextRejectedContinuation:
                calls = 0
                model = "reviewer"
                max_output_tokens = 4000
                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        return ModelResult(prefix, "reviewer", {"model_calls": 1}, .1, "length")
                    if inner_self.calls == 2:
                        raise ModelContextBudgetError("continuation cannot fit", model="reviewer",
                            estimated_input_tokens=200, allowed_input_tokens=100,
                            context_window_tokens=120, max_input_tokens=100, max_output_tokens=20)
                    return ModelResult(complete_response[len(prefix):], "reviewer", {"model_calls": 1}, .1, "stop")
            author, reviewer, progress = StubClient(self._payload()), ContextRejectedContinuation(), []
            foundry.reviewer_client = reviewer
            with self.assertRaises(ModelContextBudgetError):
                foundry.generate("bounded comparison", client=author, work_cache=cache,
                    on_progress=lambda phase, state: progress.append(state))
            retained = next(iter(progress[-1]["scientific_reviews"].values()))
            self.assertEqual(retained["response_continuation"]["partial_response"], prefix)
            self.assertEqual(retained["response_continuation"]["request_signatures"], [])
            self.assertEqual(progress[-1]["requests"][-1]["usage"]["model_calls"], 0)
            self.assertEqual(foundry.generate("bounded comparison", client=author, work_cache=cache)["status"], "registered")
            self.assertEqual(author.calls, 1)
            self.assertEqual(reviewer.calls, 3)

    def test_known_author_rate_limit_is_resumable_without_charging_an_attempt(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)

            class RateLimitedAuthor:
                def __init__(inner_self):
                    inner_self.calls = 0

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        raise ModelCallError(
                            "provider rate limited request", outcome_known=True,
                            attempts=1, status_code=429, retry_after_seconds=30,
                            provider_error_kind="rate_limited")
                    return ModelResult(
                        json.dumps(self._payload()), "author", {"model_calls": 1},
                        0.1, "stop")

            author = RateLimitedAuthor()
            first_progress = []
            with self.assertRaises(ModelCallError) as limited:
                foundry.generate(
                    "bounded comparison", client=author, work_cache=cache,
                    on_progress=lambda phase, state: first_progress.append((phase, state)))

            self.assertEqual(limited.exception.status_code, 429)
            stopped = first_progress[-1][1]
            self.assertEqual(stopped["status"], "repairing")
            self.assertEqual(stopped["attempts"], 0)
            self.assertEqual(stopped["requests"][-1]["status"], "provider_rate_limited")
            self.assertEqual(stopped["usage"]["model_calls"], 1)
            self.assertEqual(stopped["author_request_signatures"], [])

            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=cache)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 2)

    def test_local_provider_cooldown_is_not_recorded_as_a_model_call(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)

            class CooldownAuthor:
                def __init__(inner_self):
                    inner_self.calls = 0

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        raise ModelCallError(
                            "route is still in provider cooldown", outcome_known=True,
                            attempts=0, status_code=429, retry_after_seconds=5,
                            provider_error_kind="rate_limited")
                    return ModelResult(
                        json.dumps(self._payload()), "author", {"model_calls": 1},
                        0.1, "stop")

            author = CooldownAuthor()
            first_progress = []
            with self.assertRaises(ModelCallError):
                foundry.generate(
                    "bounded comparison", client=author, work_cache=cache,
                    on_progress=lambda phase, state: first_progress.append((phase, state)))

            stopped = first_progress[-1][1]
            self.assertEqual(stopped["status"], "repairing")
            self.assertEqual(stopped["attempts"], 0)
            self.assertEqual(stopped["requests"][-1]["status"], "cooldown_not_dispatched")
            self.assertEqual(stopped["requests"][-1]["usage"]["model_calls"], 0)
            self.assertEqual(stopped["usage"].get("model_calls", 0), 0)

            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=cache)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 2)

    def test_known_continuation_rate_limit_retries_only_after_resume(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            complete_response = json.dumps(self._payload(), separators=(",", ":"))

            class RateLimitedContinuationClient:
                def __init__(inner_self):
                    inner_self.calls = 0
                    inner_self.max_output_tokens = 24000

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        return ModelResult(
                            complete_response[:120], "author",
                            {"model_calls": 1}, 0.1, "length")
                    if inner_self.calls == 2:
                        raise ModelCallError(
                            "provider rate limited continuation", outcome_known=True,
                            attempts=1, status_code=429, retry_after_seconds=30,
                            provider_error_kind="rate_limited")
                    request = json.loads(prompt)
                    suffix = complete_response[len(request["partial_response"]):]
                    return ModelResult(json.dumps({
                        "marker": request["output_contract"]["legacy_json_envelope"]["marker"],
                        "continuation": suffix,
                    }), "author", {"model_calls": 1}, 0.1, "stop")

            author = RateLimitedContinuationClient()
            first_progress = []
            with self.assertRaises(ModelCallError) as limited:
                foundry.generate(
                    "bounded comparison", client=author, work_cache=cache,
                    on_progress=lambda phase, state: first_progress.append((phase, state)))

            self.assertEqual(limited.exception.status_code, 429)
            stopped = first_progress[-1][1]
            self.assertEqual(stopped["status"], "response_received")
            self.assertEqual(
                stopped["author_response_continuation"]["status"], "pending")
            self.assertEqual(stopped["requests"][-1]["status"], "provider_rate_limited")
            self.assertEqual(stopped["usage"]["model_calls"], 2)
            self.assertEqual(len(stopped["author_request_signatures"]), 1)

            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=cache)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 3)

    def test_known_independent_review_rate_limit_is_resumable(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            author = StubClient(self._payload())

            class RateLimitedReviewer:
                def __init__(inner_self):
                    inner_self.calls = 0

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        raise ModelCallError(
                            "review provider rate limited request", outcome_known=True,
                            attempts=1, status_code=429, retry_after_seconds=30,
                            provider_error_kind="rate_limited")
                    return ModelResult(
                        json.dumps(self._review_payload()), "reviewer",
                        {"model_calls": 1}, 0.1, "stop")

            reviewer = RateLimitedReviewer()
            foundry.reviewer_client = reviewer
            first_progress = []
            with self.assertRaises(ModelCallError) as limited:
                foundry.generate(
                    "bounded comparison", client=author, work_cache=cache,
                    on_progress=lambda phase, state: first_progress.append((phase, state)))

            self.assertEqual(limited.exception.status_code, 429)
            stopped = first_progress[-1][1]
            retained = next(iter(stopped["scientific_reviews"].values()))
            self.assertEqual(retained["status"], "pending")
            self.assertEqual(retained["responses"], [])
            self.assertEqual(stopped["requests"][-1]["status"], "provider_rate_limited")

            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=cache)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 1)
        self.assertEqual(reviewer.calls, 2)

    def test_exact_budget_reserves_one_continuation_and_the_independent_review(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            complete_response = json.dumps(self._payload(), separators=(",", ":"))

            class OneContinuationAuthor:
                def __init__(inner_self):
                    inner_self.calls = 0

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        return ModelResult(
                            complete_response[:120], "author",
                            {"model_calls": 1}, 0.1, "length")
                    request = json.loads(prompt)
                    suffix = complete_response[len(request["partial_response"]):]
                    return ModelResult(json.dumps({
                        "marker": request["output_contract"]["legacy_json_envelope"]["marker"],
                        "continuation": suffix,
                    }), "author", {"model_calls": 1}, 0.1, "stop")

            author = OneContinuationAuthor()
            progress = []
            outcome = foundry.generate(
                "bounded comparison", client=author,
                model_call_budget=4,
                on_progress=lambda phase, state: progress.append((phase, state)))

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 2)
        self.assertEqual(foundry.reviewer_client.calls, 1)
        ready_state = next(
            state for phase, state in reversed(progress)
            if phase == "author_response_ready_for_validation")
        continuation_request = next(
            request for request in ready_state["requests"]
            if request.get("operation") == "continue_truncated_response")
        self.assertEqual(
            continuation_request["max_output_tokens"],
            AUTHOR_CONTINUATION_MAX_OUTPUT_TOKENS)

    def test_long_author_response_continues_until_complete_within_stage_budget(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            complete_response = json.dumps(self._payload(), separators=(",", ":"))

            class MultiContinuationAuthor:
                def __init__(inner_self):
                    inner_self.calls = 0

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        return ModelResult(
                            complete_response[:120], "author",
                            {"model_calls": 1}, 0.1, "length")
                    request = json.loads(prompt)
                    prefix = request["partial_response"]
                    suffix = complete_response[len(prefix):len(prefix) + 700]
                    return ModelResult(json.dumps({
                        "marker": request["output_contract"]["legacy_json_envelope"]["marker"],
                        "continuation": suffix,
                    }), "author", {"model_calls": 1}, 0.1, "stop")

            author = MultiContinuationAuthor()
            progress = []
            outcome = foundry.generate(
                "bounded comparison", client=author, model_call_budget=12,
                on_progress=lambda phase, state: progress.append((phase, state)))

        self.assertEqual(outcome["status"], "registered")
        self.assertGreater(author.calls - 1, 4)
        ready_state = next(
            state for phase, state in reversed(progress)
            if phase == "author_response_ready_for_validation")
        continuation_requests = [
            request for request in ready_state["requests"]
            if request.get("operation") == "continue_truncated_response"]
        self.assertEqual(len(continuation_requests), author.calls - 1)
        self.assertEqual(
            ready_state["author_response_continuation"]["status"], "completed")
        self.assertEqual(foundry.reviewer_client.calls, 1)

    def test_author_format_fallback_preserves_scientific_repair_as_compact_update(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.max_attempts = 4
            foundry.model_config["role_models"] = {
                "research.experiment-author": {
                    "protocol": "openai_compatible", "base_url": "https://primary.invalid/v1",
                    "model": "primary-author", "context_window_tokens": 131072,
                    "max_input_tokens": 112000, "output_format": "json_object",
                },
            }
            foundry.model_config["role_model_fallbacks"] = {
                "research.experiment-author": [{
                    "protocol": "openai_compatible", "base_url": "https://fallback.invalid/v1",
                    "model": "fallback-author", "context_window_tokens": 131072,
                    "max_input_tokens": 112000, "output_format": "json_object",
                }],
            }

            rejected = self._review_payload()
            rejected["status"] = "rejected"
            rejected["checks"][0].update(
                outcome="failed", evidence="The declared mechanism is not measured.")
            rejected["findings"] = [{
                "severity": "blocking",
                "finding": "The experiment does not test its declared mechanism.",
                "evidence": "The observed metric is imposed by the executor formula.",
                "required_change": "Replace the imposed response with a measured quantity.",
            }]

            class SequencedReviewer:
                calls = 0
                requests = []

                def complete(self, *, system, prompt):
                    self.calls += 1
                    request = json.loads(prompt)
                    self.requests.append(request)
                    value = rejected if self.calls == 1 else self._review_payload()
                    value = _add_prior_review_checks(value, prompt)
                    return ModelResult(json.dumps(value), "reviewer",
                                       {"model_calls": 1}, 0.0, "stop")

                @staticmethod
                def _review_payload():
                    return CapabilityFoundryTests._review_payload()

            reviewer = SequencedReviewer()
            foundry.reviewer_client = reviewer
            payload = self._payload()

            class PrimaryAuthor:
                calls = 0
                max_output_tokens = 24000

                def complete(self, *, system, prompt):
                    self.calls += 1
                    if self.calls == 1:
                        return ModelResult(json.dumps(payload), "primary-author",
                                           {"model_calls": 1}, 0.0, "stop")
                    if json.loads(prompt).get("assignment") == (
                            "continue_truncated_experiment_author_json"):
                        return ModelResult(json.dumps({
                            "marker": "wrong-continuation-marker",
                            "continuation": "}",
                        }), "primary-author", {"model_calls": 1}, 0.0, "stop")
                    self_test.assertEqual(self.max_output_tokens, 24000)
                    return ModelResult('{"updates":', "primary-author",
                                       {"model_calls": 1, "output_tokens": 16384},
                                       0.0, "length")

            class FallbackAuthor:
                calls = 0
                max_output_tokens = 24000
                prompt_value = None

                def complete(self, *, system, prompt):
                    self_test.assertEqual(self.max_output_tokens, 24000)
                    self.calls += 1
                    request = json.loads(prompt)
                    self.prompt_value = request
                    update = {"updates": {"executor_source": {"edits": [{
                        "old": '    run_count = int(experiment["run_count"])',
                        "new": ('    # Execute the declared observation count exactly.\n'
                                '    run_count = int(experiment["run_count"])'),
                    }]}}}
                    return ModelResult(json.dumps(update), "fallback-author",
                                       {"model_calls": 1}, 0.0, "stop")

            self_test = self
            primary = PrimaryAuthor()
            fallback = FallbackAuthor()
            with patch("scisaurus.runtime.capability_foundry.ModelClient",
                       side_effect=[primary, fallback]) as factory:
                outcome = foundry.generate("bounded comparison", client=None)

            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(outcome["candidate"]["executor_source"].count(
                "Execute the declared observation count exactly."), 1)
            self.assertEqual((primary.calls, fallback.calls, reviewer.calls), (3, 1, 2))
            self.assertEqual(reviewer.requests[0]["prior_blocking_issues"], [])
            second_review = reviewer.requests[1]
            prior_issues = second_review["prior_blocking_issues"]
            mechanism_issues = [
                item for item in prior_issues
                if item["finding"] == "The experiment does not test its declared mechanism."
            ]
            self.assertEqual(len(mechanism_issues), 1)
            self.assertTrue(all(item["review_check_id"] for item in prior_issues))
            required_check_ids = {
                item["id"] for item in second_review["output_contract"]["checks"]
            }
            self.assertTrue({item["review_check_id"] for item in prior_issues}
                            <= required_check_ids)
            self.assertIn("Reassess every prior_blocking_issues entry",
                          second_review["review_instructions"])
            feedback = fallback.prompt_value["repair_request"]["validation_feedback"]
            self.assertEqual(feedback["findings"][0]["finding"],
                             "The experiment does not test its declared mechanism.")
            self.assertEqual(fallback.prompt_value["output_contract"].keys(), {"updates"})
            format_error = fallback.prompt_value["format_repair"]["previous_error"]
            self.assertNotIn("finish_reason=length", format_error)
            self.assertNotIn("does not test its declared mechanism", format_error)
            self.assertIn("did not satisfy the requested JSON format", format_error)
            self.assertEqual(
                [call.kwargs["model"] for call in factory.call_args_list],
                ["primary-author", "fallback-author"],
            )

    def test_failure_projection_retains_degeneracy_without_nonfinite_json(self):
        projected = program_failure_context({"metrics": [{"id": "correlation", "value": float("nan")}],
            "observations": [{"predictor": 0.0, "response": 1.0},
                             {"predictor": 0.0, "response": 2.0}]})
        self.assertEqual(projected["numeric_observation_fields"]["predictor"]["unique_finite_count"], 1)
        self.assertEqual(projected["numeric_observation_fields"]["response"]["unique_finite_count"], 2)
        self.assertIn("nan", projected["metrics"][0]["value_repr"])
        canonical_bytes(projected)

    def test_identical_failed_source_is_not_executed_again_and_repair_receives_evidence(self):
        payload = self._payload()
        payload["executor_source"] = payload["executor_source"].replace('"value": p95', '"value": float("nan")')
        client = StubClient(payload)
        original = client.complete
        def complete(*, system, prompt):
            if client.calls:
                evidence = json.loads(prompt)["repair_request"]["observed_failure_context"]
                self.assertEqual(evidence["observation_count"], INTENT["run_count"])
                self.assertIn("nan", evidence["metrics"][0]["value_repr"])
            return original(system=system, prompt=prompt)
        client.complete = complete
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            with patch.object(foundry, "_execute", wraps=foundry._execute) as execute:
                with self.assertRaisesRegex(ModelWorkBlocked, "finite JSON scalar"):
                    foundry.generate("bounded comparison", client=client)
                self.assertEqual(client.calls, 2)
                self.assertEqual(execute.call_count, 1)

    def test_independent_validator_launch_failure_blocks_registration(self):
        payload = self._payload()
        payload["validator_source"] = 'import sys\nsys.stdout.write("not json")\n'
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 1
            foundry.validator_client = StubClient({"validator_source": payload["validator_source"]})
            with patch.object(foundry, "_execute", wraps=foundry._execute) as execute:
                with self.assertRaisesRegex(ModelWorkBlocked, "validator readiness did not return a JSON"):
                    foundry.generate("bounded comparison", client=StubClient(payload))
                self.assertEqual(execute.call_count, 2)
                self.assertEqual(execute.call_args.args[0], payload["validator_source"])

    def test_foundry_and_live_executor_receive_the_same_quality_contract(self):
        intent = deepcopy(INTENT)
        intent["quality_contract"] = {"schema_version": "fixture", "required_axes": ["baseline"]}
        runner = object.__new__(ExperimentRunner)
        runner.experiment = {**intent, "execution": {"input": {"probe": True}}}
        runner.work_orders = []
        compiled = CapabilityFoundry._payload(intent, {"probe": True})
        self.assertEqual(compiled, runner._program_input())
        self.assertEqual(compiled["experiment"]["quality_contract"], intent["quality_contract"])
        compiled["experiment"]["quality_contract"]["required_axes"].append("modified")
        self.assertEqual(intent["quality_contract"]["required_axes"], ["baseline"])

    def test_validator_receives_frozen_experiment_design_at_admission(self):
        payload = self._payload()
        prompt_capture = StubClient(payload)
        with tempfile.TemporaryDirectory() as path:
            outcome = self._foundry(Path(path)).generate(
                "bounded comparison", client=prompt_capture)
        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(prompt_capture.calls, 1)

    def test_model_proposed_program_is_admitted_and_registered(self):
        payload = self._payload()
        del payload["runtime"]
        del payload["test_input"]
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            client = StubClient(payload)
            outcome = foundry.generate(
                "compare a declared estimator against a baseline", client=client,
                repair_provenance={
                    "kind": "independent_repair",
                    "origin": "composer_model_panel",
                    "panel_stage_id": "experiment-repair-panel-1-1",
                })
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(client.calls, 1)
            self.assertEqual(outcome["admission"]["gates"][:4], [
                "static_scan", "deterministic_replay", "test_vector_digest", "independent_recalculation"])
            self.assertEqual(outcome["admission"]["adversarial_review"]["status"], "admitted")
            self.assertEqual(outcome["admission"]["adversarial_review"]["review_method"], "independent_model")
            self.assertEqual(
                outcome["admission"]["repair_provenance"]["origin"],
                "composer_model_panel")
            self.assertEqual(foundry.reviewer_client.calls, 1)
            descriptor = json.loads(Path(outcome["registration"]["descriptor_path"]).read_text())
            self.assertEqual(descriptor["capability_id"], "generated_study")
            registry = load_registry(ROOT if False else root / "registry")
            self.assertEqual(len(registry["capabilities"]), 1)
            self.assertTrue(Path(descriptor["experiment"]["execution"]["client"]["command"][1]).is_file())
            self.assertEqual(descriptor["experiment"]["execution"]["input"], {"probe": True})
            self.assertTrue(outcome["candidate"]["runtime"]["python"].startswith(
                f"{sys.version_info.major}.{sys.version_info.minor}."))

    def test_model_call_budget_stops_before_independent_review_can_repeat(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            author = StubClient(self._payload())
            with self.assertRaises(CapabilityModelBudgetExceeded):
                foundry.generate("bounded comparison", client=author, model_call_budget=1)
            self.assertEqual(author.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 0)

    def test_author_cannot_replace_the_configured_test_data(self):
        payload = self._payload()
        payload["test_input"] = {"invented_data": [1, 2, 3]}
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 1
            with self.assertRaisesRegex(ValidationError, "controller-owned configured_input"):
                foundry.generate("bounded comparison", client=StubClient(payload))

    def test_identifier_drift_is_normalized_across_intent_and_program_sources(self):
        payload = self._payload()
        drifted = "Max_DvPdP_Window"
        payload["experiment_intent"] = deepcopy(payload["experiment_intent"])
        payload["experiment_intent"]["primary_outcomes"][0]["id"] = drifted
        payload["executor_source"] = payload["executor_source"].replace("tail_error", drifted)
        payload["validator_source"] = payload["validator_source"].replace("tail_error", drifted)
        normalized, repairs = normalize_capability_candidate(payload)
        self.assertEqual(
            normalized["experiment_intent"]["primary_outcomes"][0]["id"],
            "max_dvpdp_window",
        )
        self.assertNotIn(drifted, normalized["executor_source"])
        self.assertNotIn(drifted, normalized["validator_source"])
        self.assertEqual(len(repairs), 1)
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.validator_client = StubClient({"validator_source": MINI_VALIDATOR.replace("tail_error", "max_dvpdp_window")})
            outcome = foundry.generate(
                "bounded comparison", client=StubClient(payload))
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(
                outcome["candidate"]["experiment_intent"]["primary_outcomes"][0]["id"],
                "max_dvpdp_window",
            )

    def test_metadata_repair_retains_sources_and_revalidates_the_assembled_program(self):
        payload = self._payload()
        payload["experiment_intent"] = {**payload["experiment_intent"], "study_type": "theory_simulation"}
        client = StubClient(payload)
        original = client.complete
        def complete(*, system, prompt):
            if client.calls == 0:
                return original(system=system, prompt=prompt)
            assignment = json.loads(prompt)
            self.assertIn("updates", assignment["output_contract"])
            self.assertIn("exploratory", assignment["repair_request"]["previous_error"])
            client.calls += 1
            return ModelResult(json.dumps({"updates": {"experiment_intent": {"study_type": "exploratory"}}}),
                               "stub", {"model_calls": 1}, 0, "stop")
        client.complete = complete
        with tempfile.TemporaryDirectory() as path:
            result = self._foundry(Path(path)).generate("bounded comparison", client=client)
            self.assertEqual(result["candidate"]["executor_source"], MINI_EXECUTOR)
            self.assertEqual(result["candidate"]["validator_source"], MINI_VALIDATOR)
            self.assertEqual(result["candidate"]["experiment_intent"]["study_type"], "exploratory")
            self.assertEqual(client.calls, 2)
        self.assertEqual(payload["experiment_intent"]["study_type"], "theory_simulation")

    def test_authoring_patch_cannot_change_host_owned_fields(self):
        with self.assertRaisesRegex(ValidationError, "may change only"):
            apply_authoring_patch(self._payload(), {"updates": {"runtime": {"python": "invented"}}})

    def test_candidate_contract_keeps_independent_validation_on_the_same_estimand(self):
        prompt = candidate_prompt(
            "bounded comparison", [("numpy", "2.0")], {"probe": True})
        constraints = " ".join(prompt["constraints"])
        self.assertIn("same declared formula", constraints)
        self.assertIn("nearest-rank cannot validate a linear-interpolation percentile", constraints)
        self.assertIn("separately labelled secondary diagnostic", constraints)
        self.assertIn("do not manufacture an onset", constraints)
        self.assertIn("run_count is only a minimum total-row check", constraints)
        self.assertIn("Derive max_observations from the complete planned row count", constraints)
        self.assertIn("every check outcome is", prompt["independent_validation_contract"])
        self.assertIn("Metric agreement alone does not override a failed check",
                      prompt["independent_validation_contract"])
        self.assertIn("'configured_input','experiment','candidate'",
                      prompt["independent_validation_contract"])
        self.assertIn("request['experiment']", " ".join(prompt["constraints"]))

    def test_candidate_contract_separates_input_recovery_from_paper_access(self):
        from scisaurus.runtime.evidence import scientific_input_recovery_contract
        prompt = candidate_prompt("bounded comparison", [], {"probe": True})
        self.assertEqual(prompt["scientific_input_recovery"], scientific_input_recovery_contract())
        routes = prompt["scientific_input_recovery"]["routes"]
        self.assertEqual(set(routes), {"captured_source", "independent_calibration",
                                      "bounded_design", "unresolved"})
        self.assertIn("held-out validation", routes["independent_calibration"])
        self.assertIn("mathematical assumption", routes["bounded_design"])
        self.assertIn("not an executed measurement", routes["unresolved"])

    def test_candidate_contract_delivers_scoped_work_orders_to_generated_program(self):
        order = {
            "id": "repair-grid", "kind": "additional_experiment",
            "owner": "methods.validation", "objective": "Refine the measurement grid.",
            "why": "The prior result was boundary-sensitive.",
            "success_condition": "The interior estimate is reproduced.",
            "evidence_needed": "Raw observations and an independently checked estimate.",
        }
        prompt = candidate_prompt("bounded comparison", [], {"work_orders": [order]})
        self.assertNotIn("work_order_assessments", prompt["output_contract"]["executor_source"])
        self.assertNotIn("work_order_assessments", prompt["executor_output_exact_shapes"])
        self.assertTrue(any("configured_input.work_orders" in item
                            for item in prompt["constraints"]))
        self.assertTrue(any("independent reviewers adjudicate" in item
                            for item in prompt["constraints"]))

    def test_journal_quality_contract_is_required_during_capability_authoring(self):
        quality_contract = default_research_quality_contract()
        prompt = candidate_prompt(
            "bounded comparison", [("numpy", "2.0")], {"probe": True},
            required_intent={"quality_contract": quality_contract})

        self.assertEqual(
            prompt["output_contract"]["experiment_intent"]["quality_contract"],
            quality_contract)
        self.assertEqual(
            set(prompt["executor_output_exact_shapes"]["analysis"]),
            ANALYSIS_FIELDS)
        self.assertIn("required analysis object matching",
                      prompt["output_contract"]["executor_source"])
        self.assertTrue(any("analysis.conditions" in item
                            for item in prompt["constraints"]))
        self.assertTrue(any("minimum_independent_seeds" in item
                            for item in prompt["constraints"]))
        analysis_contract = prompt["executor_output_exact_shapes"]["analysis"]
        self.assertIn("id,description", analysis_contract["uncertainty"])
        self.assertIn("strings are normalized to records", analysis_contract["comparisons"])
        self.assertIn("additional JSON evidence fields are preserved",
                      analysis_contract["comparisons"])
        self.assertTrue(any(
            "closed top-level object" in item
            and "analysis.uncertainty" in item
            and "bootstrap_slope_difference" in item
            for item in prompt["constraints"]))
        self.assertTrue(any(
            "machine-readable estimate, lower, and upper" in item
            for item in prompt["constraints"]))

    def test_authoring_contract_exposes_non_estimable_analysis_without_fake_numbers(self):
        prompt = candidate_prompt("bounded comparison", [], {"probe": True},
                                  required_intent={"quality_contract": default_research_quality_contract()})
        constraints = " ".join(prompt["constraints"])
        self.assertIn("status=not_estimable", constraints)
        self.assertIn("metric_ids naming the emitted metrics", constraints)
        self.assertIn("does not establish censoring", constraints)
        for field in ("uncertainty", "effect_sizes", "sensitivity", "ablation"):
            self.assertIn("not_estimable", prompt["executor_output_exact_shapes"]["analysis"][field])

    def test_exact_source_edits_preserve_unchanged_code_and_input(self):
        previous = self._payload()
        revised = apply_authoring_patch(previous, {"updates": {"executor_source": {"edits": [
            {"old": 'if __name__ == "__main__":', "new": 'if "__main__" == __name__:'},
        ]}}})
        self.assertEqual(revised["executor_source"],
                         MINI_EXECUTOR.replace('if __name__ == "__main__":', 'if "__main__" == __name__:'))
        self.assertEqual(revised["validator_source"], MINI_VALIDATOR)
        self.assertEqual(previous["executor_source"], MINI_EXECUTOR)

    def test_authoring_repair_rejects_complete_source_replacement(self):
        with self.assertRaisesRegex(ValidationError, "complete source replacement is not accepted"):
            apply_authoring_patch(self._payload(), {"updates": {
                "executor_source": MINI_EXECUTOR,
            }})

    def test_source_repair_uses_configured_output_ceiling_and_exact_edits(self):
        class RepairClient:
            def __init__(self):
                self.max_output_tokens = 24000
                self.output_limits = []
                self.calls = 0

            def complete(inner_self, *, system, prompt):
                inner_self.output_limits.append(inner_self.max_output_tokens)
                inner_self.calls += 1
                if inner_self.calls == 1:
                    payload = CapabilityFoundryTests._payload()
                    payload["executor_source"] = MINI_EXECUTOR.replace("def main():", "def main(")
                    response = payload
                else:
                    response = {"updates": {"executor_source": {"edits": [
                        {"old": "def main(", "new": "def main():"},
                    ]}}}
                return ModelResult(json.dumps(response), "stub", {"model_calls": 1}, 0, "stop")

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            client = RepairClient()
            with patch("scisaurus.runtime.capability_foundry.ModelClient", return_value=client):
                outcome = foundry.generate("bounded comparison")

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(outcome["candidate"]["executor_source"], MINI_EXECUTOR)
        self.assertEqual(client.output_limits, [24000, 24000])

    def test_exact_source_edits_reject_missing_or_ambiguous_matches_atomically(self):
        previous = self._payload()
        for old in ("missing source text", "\n"):
            with self.subTest(old=old), self.assertRaisesRegex(ValidationError, "match exactly once"):
                apply_authoring_patch(previous, {"updates": {"executor_source": {"edits": [
                    {"old": 'if __name__ == "__main__":', "new": 'if "__main__" == __name__:'},
                    {"old": old, "new": "replacement"},
                ]}}})
            self.assertEqual(previous["executor_source"], MINI_EXECUTOR)

    def test_exact_source_edits_are_ordered_and_may_delete_text(self):
        previous = self._payload()
        revised = apply_authoring_patch(previous, {"updates": {"executor_source": {"edits": [
            {"old": "import json", "new": "import json\n# transient marker"},
            {"old": "\n# transient marker", "new": ""},
        ]}}})
        self.assertEqual(revised["executor_source"], MINI_EXECUTOR)

    def test_overlapping_source_matches_are_ambiguous(self):
        previous = self._payload()
        previous["executor_source"] = "aaa"
        with self.assertRaisesRegex(ValidationError, "match exactly once"):
            apply_authoring_patch(previous, {"updates": {"executor_source": {"edits": [
                {"old": "aa", "new": "replacement"},
            ]}}})
        self.assertEqual(previous["executor_source"], "aaa")

    def test_source_edit_mismatch_reports_actionable_current_source_context(self):
        previous = self._payload()
        previous["validator_source"] = "first\nmarker\nmiddle\nmarker\nlast"
        with self.assertRaises(ValidationError) as missing:
            apply_authoring_patch(previous, {"updates": {"validator_source": {"edits": [
                {"old": "not present", "new": "replacement"},
            ]}}})
        self.assertIn("validator_source edit old text must match exactly once", str(missing.exception))
        self.assertIn("observed 0 occurrences", str(missing.exception))
        self.assertIn("requested_old_prefix='not present'", str(missing.exception))

        with self.assertRaises(ValidationError) as ambiguous:
            apply_authoring_patch(previous, {"updates": {"validator_source": {"edits": [
                {"old": "marker", "new": "replacement"},
            ]}}})
        message = str(ambiguous.exception)
        self.assertIn("observed 2 occurrences", message)
        self.assertIn("line 2", message)
        self.assertIn("line 4", message)
        self.assertIn("requested_old_prefix='marker'", message)
        self.assertEqual(previous["validator_source"], "first\nmarker\nmiddle\nmarker\nlast")

    def test_source_edit_requires_exact_fields_and_nonempty_old_text(self):
        for value in ({"edits": []}, {"edits": [{"old": "", "new": "x"}]},
                      {"edits": [{"old": "import json", "new": None}]},
                      {"edits": [{"old": "import json", "new": "x", "regex": True}]}, None):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                apply_authoring_patch(self._payload(), {"updates": {"executor_source": value}})

    def test_source_patch_limits_reject_excessive_edit_count_and_size_atomically(self):
        candidate = self._payload()
        candidate["executor_source"] = "a b c d e"
        too_many_edits = {"updates": {"executor_source": {"edits": [
            {"old": old, "new": old.upper()} for old in "abcde"
        ]}}}
        with self.assertRaisesRegex(ValidationError, "bounded source-edit limit of 4 edits"):
            apply_authoring_patch(candidate, too_many_edits)
        self.assertEqual(candidate["executor_source"], "a b c d e")

        oversized = {"updates": {"executor_source": {"edits": [
            {"old": "a", "new": "z" * AUTHOR_PATCH_MAX_SOURCE_CHARS}
        ]}}}
        with self.assertRaisesRegex(
                ValidationError,
                f"bounded source-edit limit of {AUTHOR_PATCH_MAX_SOURCE_CHARS} characters"):
            apply_authoring_patch(candidate, oversized)
        self.assertEqual(candidate["executor_source"], "a b c d e")

    def test_exact_source_repair_passes_the_full_program_gates(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace('"__main__"', '"main__"')
        client = StubClient(payload)
        original = client.complete
        def complete(*, system, prompt):
            if client.calls == 0:
                return original(system=system, prompt=prompt)
            client.calls += 1
            return ModelResult(json.dumps({"updates": {"executor_source": {"edits": [
                {"old": 'if __name__ == "main__":', "new": 'if __name__ == "__main__":'},
            ]}}}), "stub", {"model_calls": 1}, 0, "stop")
        client.complete = complete
        with tempfile.TemporaryDirectory() as path:
            outcome = self._foundry(Path(path)).generate("bounded comparison", client=client)
            self.assertEqual(outcome["candidate"]["validator_source"], MINI_VALIDATOR)
            self.assertEqual(outcome["candidate"]["executor_source"], MINI_EXECUTOR)
            self.assertEqual(client.calls, 2)

    def test_rejected_source_edit_feedback_reaches_next_repair_and_recovers(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace('"__main__"', '"main__"')
        prompts = []

        class RepairSequenceClient:
            calls = 0

            def complete(inner_self, *, system, prompt):
                inner_self.calls += 1
                prompts.append(prompt)
                if inner_self.calls == 1:
                    return ModelResult(json.dumps(payload), "stub",
                                       {"model_calls": 1}, 0.0, "stop")
                if inner_self.calls == 2:
                    return ModelResult(json.dumps({"updates": {"executor_source": {"edits": [
                        {"old": "not present in current source",
                         "new": 'if __name__ == "__main__":'},
                    ]}}}), "stub", {"model_calls": 1}, 0.0, "stop")
                self.assertIn("observed 0 occurrences", prompt)
                self.assertIn("requested_old_prefix='not present in current source'", prompt)
                return ModelResult(json.dumps({"updates": {"executor_source": {"edits": [
                    {"old": 'if __name__ == "main__":',
                     "new": 'if __name__ == "__main__":'},
                ]}}}), "stub", {"model_calls": 1}, 0.0, "stop")

        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 3
            client = RepairSequenceClient()
            outcome = foundry.generate("bounded comparison", client=client)
            self.assertEqual(client.calls, 3)
            self.assertEqual(outcome["candidate"]["validator_source"], MINI_VALIDATOR)
            self.assertEqual(outcome["candidate"]["executor_source"], MINI_EXECUTOR)
            self.assertEqual(len(prompts), 3)

    def test_repair_can_remove_unwanted_intent_fields_without_rewriting_sources(self):
        previous = self._payload()
        previous["experiment_intent"] = {**previous["experiment_intent"], "unexpected": {"key": True}}
        revised = apply_authoring_patch(previous, {"updates": {"experiment_intent": {"unexpected": None}}})
        self.assertNotIn("unexpected", revised["experiment_intent"])
        self.assertEqual(revised["executor_source"], previous["executor_source"])
        self.assertIn("unexpected", previous["experiment_intent"])
        with self.assertRaises(ValidationError):
            apply_authoring_patch(previous, {"updates": {"runtime": None}})
        self.assertIsNone(revised["experiment_intent"]["primary_outcomes"][0]["threshold"])

    def test_merge_patch_removes_null_members_in_new_and_replaced_objects(self):
        previous = self._payload()
        previous["experiment_intent"]["parameters"] = {"replace": 7}
        revised = apply_authoring_patch(previous, {"updates": {"experiment_intent": {
            "parameters": {"new": {"omitted": None, "kept": 3},
                           "replace": {"omitted": None, "kept": 4}}
        }}})
        self.assertEqual(revised["experiment_intent"]["parameters"],
                         {"new": {"kept": 3}, "replace": {"kept": 4}})

    def test_validation_deadline_retains_response_even_on_final_authoring_attempt(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(self._payload())
            foundry = self._foundry(root)
            foundry.max_attempts = 1
            with patch.object(foundry, "_execute", side_effect=CapabilityDeadlineError("deadline")):
                with self.assertRaises(CapabilityDeadlineError):
                    foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(cache.entries()[0]["status"], "response_received")
            result = foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(client.calls, 1)

    def test_cached_capability_rechecks_registered_program_integrity(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(self._payload())
            foundry = self._foundry(root)
            outcome = foundry.generate("bounded comparison", client=client, work_cache=cache)
            descriptor = json.loads(Path(outcome["registration"]["descriptor_path"]).read_text())
            executor = Path(descriptor["experiment"]["execution"]["client"]["command"][1])
            executor.unlink()
            with self.assertRaises(ValidationError):
                foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(client.calls, 1)

    def test_changed_contract_uses_failed_source_as_input_not_as_accepted_work(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            previous = self._payload()
            previous["experiment_intent"] = {**previous["experiment_intent"], "unexpected": True}
            cache.put("previous-contract", {"status": "blocked", "last_attempt": previous,
                "feedback": "remove unexpected field", "usage": {"model_calls": 2},
                "requests": [{"prompt": json.dumps({"capability_brief": "bounded comparison",
                    "configured_input": {"probe": True}})}]})
            client = StubClient({"updates": {"experiment_intent": {"unexpected": None}}})
            foundry = self._foundry(root)
            outcome = foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(outcome["candidate"]["executor_source"], previous["executor_source"])
            self.assertEqual(client.calls, 1)
            self.assertIn("candidate_seed_ref", cache.entries()[0])

    def test_repair_seed_identity_preserves_scientific_fields_and_unrelated_lineages(self):
        order = {
            "kind": "analysis_repair", "objective": "Recalculate the primary contrast",
            "attempt_lineage": {"failure_input_sha256": "a" * 64,
                                "prior_attempt_reconciliation": {"attempt_count": 2}},
            "experiment_repair_plan": {"schema_version": "experiment-repair-plan-1",
                "lineage": {"failure_input_sha256": "a" * 64,
                            "prior_attempt_reconciliation": {"attempt_count": 2}}},
        }
        revised = deepcopy(order)
        revised["attempt_lineage"]["prior_attempt_reconciliation"]["attempt_count"] = 3
        revised["experiment_repair_plan"]["lineage"]["prior_attempt_reconciliation"]["attempt_count"] = 3
        self.assertEqual(_repair_scientific_input(order), _repair_scientific_input(revised))
        self.assertEqual(order["attempt_lineage"]["prior_attempt_reconciliation"]["attempt_count"], 2)
        for key in ("objective", "failure_input_sha256"):
            changed = deepcopy(revised)
            if key == "objective":
                changed[key] = "Measure a different contrast"
            else:
                changed["attempt_lineage"][key] = "b" * 64
            self.assertNotEqual(_repair_scientific_input(order), _repair_scientific_input(changed))
        parameter = {"parameters": {"lineage": {"prior_attempt_reconciliation": {"value": 2}}}}
        self.assertEqual(_repair_scientific_input(parameter), parameter)
        for kind in ({}, [], None):
            parameter = {"parameters": {"kind": kind,
                         "attempt_lineage": {"prior_attempt_reconciliation": 7}}}
            self.assertEqual(_repair_scientific_input(parameter), parameter)

    def test_reconciled_repair_seed_retains_candidate_counters_and_current_validation_input(self):
        order = {"kind": "analysis_repair", "objective": "Recalculate the primary contrast",
                 "attempt_lineage": {"failure_input_sha256": "a" * 64,
                                     "prior_attempt_reconciliation": {"attempt_count": 2}}}
        updated_order = deepcopy(order)
        updated_order["attempt_lineage"]["prior_attempt_reconciliation"]["attempt_count"] = 3
        old_input = {"probe": True, "repair_order": order}
        new_input = {"probe": True, "repair_order": updated_order}
        old_brief = json.dumps({"continuation": {"requests": [order]}})
        new_brief = json.dumps({"continuation": {"requests": [updated_order]}})
        previous = self._payload()
        previous["experiment_intent"]["unexpected"] = True
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            cache.put("previous-contract", {"status": "repairing", "attempts": 2,
                "assignment": {"capability_brief": old_brief, "configured_input": old_input},
                "last_attempt": previous, "feedback": "remove unexpected field",
                "usage": {"model_calls": 9, "input_tokens": 900},
                "repair_gate_counts": {"author_response_contract": 1},
                "repair_ledger": [{"attempt": 2, "gate": "author_response_contract"}],
                "requests": []})
            class PatchAuthor(StubClient):
                def complete(inner_self, *, system, prompt):
                    parsed = json.loads(prompt)
                    self.assertEqual(parsed["assignment"], "repair_existing_experiment_candidate")
                    return super().complete(system=system, prompt=prompt)
            author = PatchAuthor({"updates": {"experiment_intent": {"unexpected": None}}})
            foundry = self._foundry(root)
            foundry.max_attempts = 3
            progress = []
            result = foundry.generate(new_brief, client=author, work_cache=cache,
                test_input=new_input,
                on_progress=lambda phase, state: progress.append((phase, state)))
            final = progress[-1][1]
            self.assertEqual(author.calls, 1)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(final["attempts"], 3)
            self.assertEqual(final["repair_gate_counts"]["author_response_contract"], 1)
            self.assertEqual(final["repair_ledger"][0]["attempt"], 2)
            self.assertEqual(result["candidate"]["test_vector"]["input"]["configured_input"], new_input)
            self.assertEqual(final["usage"]["model_calls"], 3)
            self.assertNotEqual(final["usage"].get("input_tokens"), 900)
            self.assertIn("candidate_seed_ref", final)

    def test_new_unrequested_repair_subject_retains_history_but_receives_author_attempt(self):
        previous = self._payload()
        previous["experiment_intent"]["unexpected"] = True
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            cache.put("exhausted-prior-subject", {"status": "blocked", "attempts": 10,
                "assignment": {"capability_brief": "bounded comparison", "configured_input": {"probe": True}},
                "last_attempt": previous, "feedback": "remove unexpected field",
                "repair_subject_attempt_offset": 0, "usage": {"model_calls": 9},
                "repair_ledger": [{"attempt": 10, "gate": "author_response_contract"}],
                "requests": [{"role": "research.experiment-author", "status": "succeeded",
                              "prompt": json.dumps({"candidate_sha256": _authored_candidate_sha256(self._payload())})}]})
            author = StubClient({"updates": {"experiment_intent": {"unexpected": None}}})
            foundry = self._foundry(root)
            foundry.max_attempts = 2
            states = []
            result = foundry.generate("bounded comparison", client=author, work_cache=cache,
                on_progress=lambda phase, state: states.append(state))
            self.assertEqual(result["status"], "registered")
            self.assertEqual(author.calls, 1)
            self.assertEqual(states[-1]["attempts"], 11)
            self.assertEqual(states[-1]["repair_subject_attempt_offset"], 10)
            self.assertEqual(states[-1]["repair_subject_attempt_limit"], 12)
            self.assertEqual(states[-1]["repair_ledger"][0]["attempt"], 10)
            self.assertEqual(states[-1]["candidate_seed_ref"].split("@")[0],
                             "artifact:command/foundry-work/exhausted-prior-subject")

    def test_existing_repair_subject_retains_absolute_limit_after_policy_change(self):
        previous = self._payload()
        previous["experiment_intent"]["unexpected"] = True
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            cache.put("exhausted-owned-subject", {"status": "blocked", "attempts": 10,
                "assignment": {"capability_brief": "bounded comparison", "configured_input": {"probe": True}},
                "last_attempt": previous, "feedback": "remove unexpected field",
                "repair_subject_sha256": _authored_candidate_sha256(previous),
                "repair_subject_attempt_offset": 8, "repair_subject_attempt_limit": 10})
            author = StubClient({"updates": {"experiment_intent": {"unexpected": None}}})
            foundry = self._foundry(root)
            foundry.max_attempts = 4
            states = []
            with self.assertRaises(ModelWorkBlocked):
                foundry.generate("bounded comparison", client=author, work_cache=cache,
                    on_progress=lambda phase, state: states.append(state))
            self.assertEqual(author.calls, 0)
            self.assertEqual(states[-1]["attempts"], 10)
            self.assertEqual(states[-1]["repair_subject_attempt_limit"], 10)

    def test_repair_subject_attribution_uses_dispatched_request_identity(self):
        candidate = self._payload()
        request = {"role": "research.experiment-author", "request_signature": "recorded",
                   "prompt": json.dumps({"repair_request": {"previous_attempt": candidate}})}
        for status in ("succeeded", "failed", "result_unknown"):
            with self.subTest(status=status):
                self.assertTrue(_author_requested_candidate([{**request, "status": status}], candidate))
        for status in ("provider_rate_limited", "cooldown_not_dispatched"):
            with self.subTest(status=status):
                self.assertFalse(_author_requested_candidate([{**request, "status": status}], candidate))
        self.assertFalse(_author_requested_candidate([request], {}))
        invalid = {**request, "prompt": json.dumps({"repair_request": {"previous_attempt": {}}})}
        self.assertFalse(_author_requested_candidate([invalid], {}))

    def test_changed_scientific_repair_input_does_not_seed_prior_candidate(self):
        order = {"kind": "analysis_repair", "objective": "Old contrast",
                 "attempt_lineage": {"prior_attempt_reconciliation": {"attempt_count": 2}}}
        changed = deepcopy(order)
        changed["objective"] = "Different contrast"
        old_input = {"probe": True, "repair_order": order}
        new_input = {"probe": True, "repair_order": changed}
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            cache.put("previous-contract", {"status": "repairing", "attempts": 2,
                "assignment": {"capability_brief": "comparison", "configured_input": old_input},
                "last_attempt": self._payload(), "feedback": "old failure", "requests": []})
            payload = self._payload()
            payload["test_input"] = new_input
            author = StubClient(payload)
            progress = []
            result = self._foundry(root).generate("comparison", client=author, work_cache=cache,
                test_input=new_input,
                on_progress=lambda phase, state: progress.append((phase, state)))
            self.assertEqual(result["status"], "registered")
            self.assertEqual(author.calls, 1)
            self.assertNotIn("candidate_seed_ref", progress[-1][1])
            self.assertEqual(progress[-1][1]["attempts"], 1)

    def test_changed_patch_contract_replays_recorded_repair_before_spending_a_call(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            previous = self._payload()
            previous["experiment_intent"] = {**previous["experiment_intent"], "unexpected": None}
            reply = {"updates": {"experiment_intent": {"unexpected": None}}}
            cache.put("previous-contract", {"status": "blocked", "last_attempt": previous,
                "feedback": "remove unexpected field", "usage": {"model_calls": 2},
                "last_response": {"text": json.dumps(reply), "model": "stub",
                    "usage": {"model_calls": 1}, "elapsed_seconds": 0, "finish_reason": "stop"},
                "requests": [{"prompt": json.dumps({"capability_brief": "bounded comparison",
                    "configured_input": {"probe": True}})}]})
            client = StubClient(reply)
            outcome = self._foundry(root).generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(client.calls, 0)
            self.assertEqual(cache.entries()[0]["usage"], {"model_calls": 2})

    def test_changed_contract_replays_exact_edit_against_its_original_base(self):
        for recorded_base in (False, True):
            with self.subTest(recorded_base=recorded_base), tempfile.TemporaryDirectory() as path:
                root = Path(path)
                cache = self._cache(root)
                assembled = self._payload()
                previous = self._payload()
                previous["validator_source"] = MINI_VALIDATOR.replace('"__main__"', '"main__"')
                reply = {"updates": {"validator_source": {"edits": [
                    {"old": 'if __name__ == "main__":', "new": 'if __name__ == "__main__":'},
                ]}}}
                prior = {"status": "blocked", "last_attempt": assembled,
                    "feedback": "old validation contract", "usage": {"model_calls": 2},
                    "last_response": {"text": json.dumps(reply), "model": "stub",
                        "usage": {"model_calls": 1}, "elapsed_seconds": 0, "finish_reason": "stop"},
                    "requests": [{"role": "research.experiment-author", "status": "succeeded", "prompt": json.dumps({
                        "capability_brief": "bounded comparison", "configured_input": {"probe": True},
                        "repair_request": {"previous_attempt": previous}})}]}
                prior["requests"].append({"role": "research.experiment-author", "status": "result_unknown",
                    "prompt": json.dumps({"capability_brief": "bounded comparison",
                        "configured_input": {"probe": True}, "repair_request": {"previous_attempt": assembled}})})
                if recorded_base:
                    prior["response_base"] = previous
                cache.put("previous-contract", prior)
                author = StubClient(reply)
                outcome = self._foundry(root).generate("bounded comparison", client=author, work_cache=cache)
                self.assertEqual(outcome["status"], "registered")
                self.assertEqual(outcome["candidate"]["validator_source"], MINI_VALIDATOR)
                self.assertEqual(author.calls, 0)
                self.assertEqual(cache.entries()[0]["usage"]["model_calls"], 2)

    def test_check_only_review_rejection_preserves_the_failed_evidence(self):
        review = self._review_payload()
        review["status"] = "rejected"
        review["checks"][0].update(outcome="failed", evidence="The reported estimator ignores its declared input")
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.reviewer_client = StubClient(review)
            author = StubClient(self._payload())
            complete = author.complete
            def inspect_repair(**kwargs):
                if author.calls:
                    feedback = json.loads(kwargs["prompt"])["repair_request"]["validation_feedback"]
                    self.assertEqual(feedback["failed_checks"], [review["checks"][0]])
                    self.assertEqual(feedback["findings"], [])
                return complete(**kwargs)
            author.complete = inspect_repair
            with self.assertRaisesRegex(ModelWorkBlocked, "ignores its declared input"):
                foundry.generate("bounded comparison", client=author)
            self.assertEqual(author.calls, 2)
            self.assertEqual(foundry.reviewer_client.calls, 1)

    def test_recalculation_feedback_prioritizes_failures_over_successful_checks(self):
        from scisaurus.runtime.program_gates import ProgramGateRejected
        passed = {"id": "recalculation", "outcome": "passed", "evidence": "matching values" * 100}
        failed = {"id": "sensitivity", "outcome": "failed", "evidence": "declared input not measured"}
        exc = ProgramGateRejected("independent recalculation did not accept the candidate",
                                  {"decision": "rejected", "checks": [passed] * 100 + [failed]},
                                  gate="independent_recalculation")
        self.assertEqual(exc.feedback["failed_checks"], [failed])
        self.assertIn("declared input not measured", str(exc))
        self.assertNotIn("matching values", str(exc))

    def test_independent_rejection_blocks_registration_and_reuses_the_exact_verdict(self):
        review = self._review_payload()
        review["status"] = "rejected"
        review["checks"][0]["outcome"] = "failed"
        review["findings"] = [{"severity": "blocking", "finding": "Undefined estimator hidden as zero",
                               "evidence": "empty aggregate substituted with zero", "required_change": "repair the estimand"}]
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.reviewer_client = StubClient(review)
            author = StubClient(self._payload())
            complete = author.complete
            def inspect_repair(**kwargs):
                if author.calls:
                    corrections = json.loads(kwargs["prompt"])["repair_request"]["validation_feedback"]["findings"]
                    self.assertEqual(corrections[0]["required_change"], "repair the estimand")
                return complete(**kwargs)
            author.complete = inspect_repair
            cache = self._cache(root)
            for _ in range(2):
                with self.assertRaisesRegex(ModelWorkBlocked, "Undefined estimator hidden as zero"):
                    foundry.generate("bounded comparison", client=author, work_cache=cache)
            self.assertEqual(author.calls, 2)
            self.assertEqual(foundry.reviewer_client.calls, 1)
            self.assertEqual(cache.entries()[0]["usage"]["model_calls"], 4)
            self.assertFalse((root / "registry/capabilities/index.json").exists())

    def test_review_retains_raw_response_and_usage_across_interruption(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            author = StubClient(self._payload())
            cache = self._cache(root)
            def interrupt(phase, state):
                if phase == "scientific_review_response":
                    raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                foundry.generate("bounded comparison", client=author, work_cache=cache, on_progress=interrupt)
            result = foundry.generate("bounded comparison", client=author, work_cache=cache)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(author.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 1)
            self.assertEqual(cache.entries()[0]["usage"]["model_calls"], 3)

    def test_review_receives_complete_candidate_bound_recalculation_and_observations(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            author = StubClient(self._payload())
            reviewer = foundry.reviewer_client
            complete = reviewer.complete
            packets = []
            def inspect(**kwargs):
                packets.append(json.loads(kwargs["prompt"]))
                return complete(**kwargs)
            reviewer.complete = inspect
            cache = self._cache(root)
            result = foundry.generate("bounded comparison", client=author,
                                     test_input={"probe": True}, work_cache=cache)
            packet = packets[0]
            evidence = packet["execution_evidence"]
            execution = next(item for item in cache.entries()[0]["sandbox_executions"]
                             if item["operation"] == "executor_replay")
            document = validate_program_output(json.loads(
                cache.store.read_body(execution["stdout_sha256"])),
                result["candidate"]["experiment_intent"])
            self.assertEqual(evidence["raw_observations"], document["observations"])
            self.assertTrue(evidence["raw_observations_complete"])
            self.assertEqual(evidence["reported_metrics"], document["metrics"])
            self.assertEqual(evidence["configured_input"], {"probe": True})
            digest = hashlib.sha256(canonical_bytes(document)).hexdigest()
            self.assertEqual(evidence["candidate_sha256"], digest)
            self.assertEqual(evidence["independent_validation"]["candidate_sha256"], digest)
            self.assertEqual(evidence["independent_validation"]["decision"], "accepted")
            self.assertEqual(evidence["executor_source_sha256"],
                hashlib.sha256(packet["executor_source"].encode()).hexdigest())
            self.assertEqual(evidence["validator_source_sha256"],
                hashlib.sha256(packet["validator_source"].encode()).hexdigest())
            changed = deepcopy(evidence["independent_validation"])
            changed["candidate_sha256"] = "0" * 64
            with self.assertRaises(ValidationError):
                program_review_evidence(result["candidate"], document, changed)

    def test_review_cache_without_execution_evidence_is_not_reused_on_resume(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            author = StubClient(self._payload())
            cache = self._cache(root)
            def interrupt(phase, state):
                if phase == "scientific_review_response":
                    raise KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                foundry.generate("bounded comparison", client=author,
                                 work_cache=cache, on_progress=interrupt)
            entry = cache.entries()[0]
            key, retained = next(iter(entry["scientific_reviews"].items()))
            old_scope = hashlib.sha256(canonical_bytes(sorted(PROGRAM_REVIEW_CHECKS))).hexdigest()
            entry["scientific_reviews"] = {key.split(":")[0] + ":" + old_scope: retained}
            cache.put(entry["cache_ref"].rsplit("/", 1)[-1].split("@")[0], entry)
            result = foundry.generate("bounded comparison", client=author, work_cache=cache)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(author.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 2)

    def test_review_schema_error_identifies_echoed_input_fields(self):
        review = self._review_payload() | {"analysis": {}, "assignment": "review"}
        with self.assertRaisesRegex(ValidationError,
                r"unexpected=\['analysis', 'assignment'\]"):
            validate_program_review(review)

    def test_late_scientific_verdict_is_retained_but_not_registered(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            author = StubClient(self._payload())
            monotonic = time.monotonic
            deadline = monotonic() + 60
            completed = False
            original = foundry.reviewer_client.complete
            def late_review(**kwargs):
                nonlocal completed
                response = original(**kwargs)
                completed = True
                return response
            foundry.reviewer_client.complete = late_review
            with patch("scisaurus.runtime.capability_foundry.time.monotonic",
                       side_effect=lambda: deadline + 1 if completed else monotonic()):
                with self.assertRaises(CapabilityDeadlineError):
                    foundry.generate("bounded comparison", client=author, work_cache=cache, deadline=deadline)
            self.assertFalse((root / "registry/capabilities/index.json").exists())
            self.assertEqual(cache.entries()[0]["status"], "response_received")
            result = foundry.generate("bounded comparison", client=author, work_cache=cache,
                                     deadline=monotonic() + 60)
            self.assertEqual(result["status"], "registered")
            self.assertEqual(author.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 1)

    def test_truncated_review_uses_only_one_configured_format_fallback_and_survives_resume(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.reviewer_client = None
            foundry.model_config["role_models"] = {"review.methods": {"model": "first-reviewer"}}
            foundry.model_config["role_model_fallbacks"] = {"review.methods": [{"model": "second-reviewer"}]}
            author = StubClient(self._payload())
            cache = self._cache(root)
            first = StubClient({})
            def truncated(**kwargs):
                first.calls += 1
                return ModelResult("unfinished reasoning that must not be echoed", "first-reviewer",
                                   {"model_calls": 1}, 0, "length")
            first.complete = truncated
            annotated_review = self._review_payload()
            for check in annotated_review["checks"]:
                check.update({
                    "severity": "warning",
                    "finding": f"The {check['id']} check needs attention.",
                    "required_change": f"Rerun the {check['id']} check with evidence.",
                })
            second = StubClient(annotated_review)
            original = second.complete
            def finish(**kwargs):
                self.assertNotIn("unfinished reasoning that must not be echoed", kwargs["prompt"])
                self.assertIn("format_repair", json.loads(kwargs["prompt"]))
                return original(**kwargs)
            second.complete = finish
            def interrupt(phase, state):
                if phase == "scientific_review_response":
                    raise KeyboardInterrupt()
            with patch("scisaurus.runtime.capability_foundry.ModelClient", return_value=first):
                with self.assertRaises(KeyboardInterrupt):
                    foundry.generate("bounded comparison", client=author, work_cache=cache, on_progress=interrupt)
            with patch("scisaurus.runtime.capability_foundry.ModelClient", return_value=second) as factory:
                outcome = foundry.generate("bounded comparison", client=author, work_cache=cache)
                self.assertEqual(factory.call_args.kwargs["model"], "second-reviewer")
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual((author.calls, first.calls, second.calls), (1, 1, 1))
            self.assertEqual(cache.entries()[0]["usage"]["model_calls"], 4)

    def test_complete_review_json_is_accepted_when_finish_reason_is_length(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            author = StubClient(self._payload())

            class CompleteLengthReviewer:
                calls = 0

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    return ModelResult(
                        json.dumps(self._review_payload()), "reviewer",
                        {"model_calls": 1}, 0.0, "length")

            reviewer = CompleteLengthReviewer()
            foundry.reviewer_client = reviewer
            outcome = foundry.generate("bounded comparison", client=author)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual((author.calls, reviewer.calls), (1, 1))

    def test_truncated_review_continues_from_saved_prefix_without_repeating_prompt(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            author = StubClient(self._payload())

            class LengthReviewer:
                calls = 0
                max_output_tokens = 1024
                timeout_seconds = 30
                output_format = "json"

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        return ModelResult(
                            json.dumps(self._review_payload())[:-1], "reviewer",
                            {"model_calls": 1}, 0.0, "length")
                    request = json.loads(prompt)
                    self.assertEqual(
                        request["assignment"], "continue_truncated_independent_review_json")
                    self.assertIsNone(inner_self.output_format)
                    self.assertEqual(
                        request["partial_response"][-1],
                        json.dumps(self._review_payload())[-2],
                    )
                    return ModelResult("}", "reviewer", {"model_calls": 1}, 0.0, "stop")

            reviewer = LengthReviewer()
            foundry.reviewer_client = reviewer
            outcome = foundry.generate(
                "bounded comparison", client=author, work_cache=self._cache(root))

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual((author.calls, reviewer.calls), (1, 2))
        self.assertEqual(reviewer.output_format, "json")

    def test_truncated_evidence_echo_repairs_verdict_without_suffix_continuation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            author = StubClient(self._payload())
            outer = self
            class Reviewer:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    packet = json.loads(prompt)
                    outer.assertEqual(packet['assignment'], 'independent_scientific_program_review')
                    if inner.calls == 1:
                        return ModelResult('{"sta\\q":',
                                           'reviewer', {'model_calls': 1}, 0, 'length')
                    outer.assertIn('format_repair', packet)
                    return ModelResult(json.dumps(outer._review_payload()),
                                       'reviewer', {'model_calls': 1}, 0, 'stop')
            foundry.reviewer_client = reviewer = Reviewer()
            outcome = foundry.generate('bounded comparison', client=author,
                                       work_cache=self._cache(root))
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual((author.calls, reviewer.calls), (1, 2))

    def test_format_recovery_revalidates_frozen_executor_without_author_call(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            author = StubClient(self._payload())
            foundry.reviewer_client = StubClient({'invalid': True})
            with self.assertRaises(ModelWorkBlocked) as failure:
                foundry.generate('bounded comparison', client=author, work_cache=cache)
            ref = failure.exception.repair_feedback['foundry_work_ref']
            record = cache.store.get(ref)
            frozen = json.loads(cache.store.read_body(record['body_hash']))
            competing = deepcopy(frozen)
            competing['last_attempt']['executor_source'] += '\n# distinct program\n'
            competing['last_response']['text'] = json.dumps({
                **self._payload(), 'executor_source': competing['last_attempt']['executor_source']})
            competing.pop('response_base', None)
            cache.put('distinct-work', competing)
            foundry.reviewer_client = StubClient(self._review_payload())
            next_author = StubClient({'invalid': True})
            result = foundry.generate(frozen['assignment']['capability_brief'],
                test_input=frozen['assignment']['configured_input'],
                required_intent=frozen['last_attempt']['experiment_intent'],
                client=next_author, work_cache=cache, resume_work_ref=ref)
            self.assertEqual(result['status'], 'registered')
            self.assertEqual(next_author.calls, 0)
            self.assertEqual(result['candidate']['executor_source'],
                             frozen['last_attempt']['executor_source'])
            self.assertEqual(result['candidate']['experiment_intent'],
                             frozen['last_attempt']['experiment_intent'])

    def test_unknown_review_continuation_is_not_replayed_after_resume(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            author = StubClient(self._payload())

            class InterruptibleReviewer:
                calls = 0

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        return ModelResult(
                            json.dumps(self._review_payload())[:-1], "reviewer",
                            {"model_calls": 1}, 0.0, "length")
                    return ModelResult("}", "reviewer", {"model_calls": 1}, 0.0, "stop")

            reviewer = InterruptibleReviewer()
            foundry.reviewer_client = reviewer
            cache = self._cache(root)

            def interrupt_before_dispatch(phase, state):
                if phase == "scientific_review_continuation_calling":
                    raise KeyboardInterrupt()

            with self.assertRaises(KeyboardInterrupt):
                foundry.generate(
                    "bounded comparison", client=author, work_cache=cache,
                    on_progress=interrupt_before_dispatch)
            with self.assertRaisesRegex(ModelWorkBlocked, "unobserved provider outcome"):
                foundry.generate("bounded comparison", client=author, work_cache=cache)

        self.assertEqual((author.calls, reviewer.calls), (1, 1))

    def test_review_format_repair_budget_is_durable_and_never_relaxes_the_gate(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.reviewer_client = StubClient({"invalid": True})
            author = StubClient(self._payload())
            cache = self._cache(root)
            for _ in range(2):
                with self.assertRaisesRegex(ModelWorkBlocked, "review response is invalid") as blocked:
                    foundry.generate("bounded comparison", client=author, work_cache=cache)
                self.assertEqual(blocked.exception.failure_class, "model_contract")
                self.assertEqual(blocked.exception.repair_gate, "review_response_format")
                self.assertEqual(blocked.exception.recovery_mode, "format_repair_then_rerun")
            self.assertEqual(author.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 2)
            self.assertFalse((root / "registry/capabilities/index.json").exists())
            self.assertFalse(cache.entries()[0]["failed_candidates"])

    def test_failed_review_retains_complete_sandbox_execution_objects(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.reviewer_client = StubClient({'invalid': True})
            cache = self._cache(root)
            with self.assertRaises(ModelWorkBlocked):
                foundry.generate('bounded comparison', client=StubClient(self._payload()),
                                 work_cache=cache)
            runs = cache.entries()[0]['sandbox_executions']
            self.assertEqual([r['operation'] for r in runs], [
                'executor_preview', 'validator_readiness', 'validator_preview',
                'executor_replay', 'executor_replay', 'executor_replay',
                'validator_recalculation'])
            store = cache.store
            for run in runs:
                for key in ('program_sha256', 'stdin_sha256', 'stdout_sha256', 'stderr_sha256'):
                    body = store.read_body(run[key])
                    self.assertEqual(hashlib.sha256(body).hexdigest(), run[key])
                self.assertEqual(run['returncode'], 0)
                self.assertEqual(run['mode'], 'sandbox-exec')
                self.assertFalse(run['truncated'])
                self.assertFalse(run['timed_out'])
            raw = json.loads(store.read_body(runs[0]['stdout_sha256']))
            validator_input = json.loads(store.read_body(runs[-1]['stdin_sha256']))
            self.assertEqual(validator_input['candidate'], raw)
            self.assertEqual(json.loads(store.read_body(runs[-1]['stdout_sha256']))['decision'],
                             'accepted')
            self.assertEqual({r['stdout_sha256'] for r in runs if r['operation']=='executor_replay'},
                             {runs[0]['stdout_sha256']})

    def test_unhashable_review_fields_raise_validation_errors(self):
        from scisaurus.runtime.capability_foundry import validate_program_review
        for field in ("id", "outcome", "severity"):
            value = self._review_payload()
            if field == "severity":
                value["findings"] = [{"severity": {}, "finding": "invalid", "evidence": "invalid",
                                      "required_change": "invalid"}]
            else:
                value["checks"][0][field] = []
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_program_review(value)

    def test_complete_review_preserves_inline_check_diagnostics(self):
        from scisaurus.runtime.capability_foundry import validate_program_review
        value = self._review_payload()
        value["status"] = "rejected"
        for item in value["checks"]:
            item.update({
                "outcome": "failed",
                "severity": "blocking",
                "finding": f"The {item['id']} check failed on the supplied evidence.",
                "required_change": f"Repair the {item['id']} check and rerun it.",
            })

        validated = validate_program_review(value)

        self.assertEqual(validated["checks"], value["checks"])
        self.assertEqual(len(validated["checks"]), len(PROGRAM_REVIEW_CHECKS))

    def test_inline_blocking_check_diagnostic_cannot_admit_review(self):
        from scisaurus.runtime.capability_foundry import validate_program_review
        value = self._review_payload()
        value["checks"][0].update({
            "severity": "blocking",
            "finding": "A blocking defect contradicts the passed check.",
            "required_change": "Resolve the defect and rerun the check.",
        })

        with self.assertRaisesRegex(ValidationError, "status contradicts its checks"):
            validate_program_review(value)

        value["status"] = "rejected"
        self.assertEqual(validate_program_review(value)["status"], "rejected")

    def test_review_check_diagnostics_reject_unknown_or_malformed_fields(self):
        from scisaurus.runtime.capability_foundry import validate_program_review
        value = self._review_payload()
        value["checks"][0].update({
            "severity": "blocking",
            "finding": "The check failed.",
            "required_change": "Repair and rerun the check.",
        })
        value["checks"][0]["unexpected"] = "must not be silently discarded"
        with self.assertRaises(ValidationError):
            validate_program_review(value)

        value["checks"][0].pop("unexpected")
        value["checks"][0]["severity"] = {}
        with self.assertRaises(ValidationError):
            validate_program_review(value)

    def test_malformed_review_field_repairs_format_without_reauthoring(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            invalid = self._review_payload()
            invalid["checks"][0]["id"] = []
            reviewer = StubClient(invalid)
            complete = reviewer.complete
            def repair(**kwargs):
                if reviewer.calls:
                    repair_contract = json.loads(kwargs["prompt"])["format_repair"]
                    self.assertEqual(set(repair_contract["required_check_ids"]), PROGRAM_REVIEW_CHECKS)
                    self.assertIn("independent_validation", repair_contract["instructions"])
                    reviewer.payload = self._review_payload()
                return complete(**kwargs)
            reviewer.complete = repair
            foundry.reviewer_client = reviewer
            author = StubClient(self._payload())
            outcome = foundry.generate("bounded comparison", client=author)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual((author.calls, reviewer.calls), (1, 2))

    def test_review_limitations_do_not_invalidate_a_complete_rejection(self):
        from scisaurus.runtime.capability_foundry import validate_program_review
        value = self._review_payload()
        value.update(status="rejected", limitations=["No external simulation was run."])
        value["checks"][0]["outcome"] = "failed"
        self.assertEqual(validate_program_review(value)["status"], "rejected")
        with self.assertRaises(ValidationError):
            validate_program_review({**value, "status": "admitted"})

    def test_registered_generated_program_runs_end_to_end_under_required_sandbox(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            outcome = self._foundry(root).generate(
                "compare a declared estimator against a baseline",
                client=StubClient(self._payload()),
            )
            descriptor = json.loads(Path(
                outcome["registration"]["descriptor_path"]).read_text())
            config = {
                "live_dispatch_allowed": True, "data_classification": "public",
                "allocation_mode": "capacity_pool", "project_id": "generated-e2e",
                "objective": "Exercise an admitted generated experiment end to end.",
                "supplied_context": "Synthetic deterministic integration fixture.",
                "model": {"protocol": "openai_compatible",
                          "base_url": "http://example.invalid/v1", "model": "fixture-model",
                          "timeout_seconds": 5, "max_output_tokens": 2000,
                          "auth_env": None},
                "limits": {"max_rounds": 2, "wall_clock_seconds": 120,
                           "checkpoint_seconds": 1, "max_result_bytes": 10_000_000,
                           "concurrent_calls": 3, "worker_concurrency": 1},
                "time_policy": {"first_result_seconds": 60, "target_seconds": 90,
                                "hard_seconds": 120},
                "experiment": descriptor["experiment"],
            }
            runner = ExperimentRunner(root / "experiment-run", config)
            runner.worker_target = fixture_worker
            result = runner.run()
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(len(result["execution_refs"]), 2)
            self.assertTrue(result["event_chain"][0])

    def test_required_scientific_intent_cannot_be_paraphrased(self):
        payload = {
            "executor_source": MINI_EXECUTOR, "validator_source": MINI_VALIDATOR,
            "runtime": {"python": "3.14", "packages": [{"name": "numpy", "version": "2.5.2"}]},
            "test_input": {"probe": True}, "experiment_intent": dict(INTENT),
        }
        payload["experiment_intent"] = dict(payload["experiment_intent"])
        payload["experiment_intent"]["research_question"] = "A convenient replacement question"
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundry(
                {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
                 "model": "stub", "timeout_seconds": 60, "max_output_tokens": 128},
                runtime_python=sys.executable, workspace_root=root / "workspace",
                registry_root=root / "registry", repo_root=ROOT,
                requirements_file=ROOT / "requirements-experiment.txt",
                runtime_packages=[("pip", version("pip"))], max_attempts=1)
            with self.assertRaisesRegex(ValidationError, "required scientific intent"):
                foundry.generate(
                    "bounded question", required_intent={
                        "domain": INTENT["domain"],
                        "research_question": INTENT["research_question"]},
                    client=StubClient(payload))

    def test_required_revision_is_controller_owned_and_normalized(self):
        payload = self._payload()
        payload["experiment_intent"]["revision"] = INTENT["revision"] + 1
        payload["experiment_intent"].pop("stage_seconds")
        required_intent = {
            "revision": INTENT["revision"],
            "domain": INTENT["domain"],
            "research_question": INTENT["research_question"],
            "stage_seconds": dict(INTENT["stage_seconds"]),
        }
        prompt = candidate_prompt(
            "bounded question", [], {"probe": True}, required_intent=required_intent)
        self.assertEqual(
            prompt["output_contract"]["experiment_intent"]["revision"],
            INTENT["revision"])
        self.assertEqual(
            prompt["output_contract"]["experiment_intent"]["stage_seconds"],
            INTENT["stage_seconds"])
        self.assertTrue(any("never increment it" in item for item in prompt["constraints"]))
        self.assertTrue(any(
            "experiment_intent.stage_seconds must contain exactly these keys" in item
            and "reassessment" in item
            for item in prompt["constraints"]
        ))

        with tempfile.TemporaryDirectory() as path:
            progress = []
            outcome = self._foundry(Path(path)).generate(
                "bounded question", required_intent=required_intent,
                client=StubClient(payload),
                on_progress=lambda phase, state: progress.append(state))

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(outcome["candidate"]["revision"], INTENT["revision"])
        self.assertEqual(outcome["candidate"]["experiment_intent"]["revision"],
                         INTENT["revision"])
        self.assertTrue(any(
            row.get("kind") == "controller_owned_intent_revision"
            and row.get("required") == INTENT["revision"]
            and row.get("received") == INTENT["revision"] + 1
            for state in progress for row in state.get("normalizations", [])))
        self.assertTrue(any(
            row.get("kind") == "controller_owned_stage_seconds"
            and row.get("required") == INTENT["stage_seconds"]
            and row.get("received") is None
            for state in progress for row in state.get("normalizations", [])))

    def test_broken_program_is_never_registered(self):
        payload = {
            "executor_source": "import json\nimport sys\nsys.stdout.write('not json')\n",
            "validator_source": MINI_VALIDATOR,
            "runtime": {"python": "3.14", "packages": [{"name": "numpy", "version": "2.5.2"}]},
            "test_input": {"probe": True},
            "experiment_intent": INTENT,
        }
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundry(
                {"protocol": "openai_compatible", "base_url": "https://example.invalid/v1",
                 "model": "stub", "timeout_seconds": 60, "max_output_tokens": 128},
                runtime_python=sys.executable, workspace_root=root / "workspace",
                registry_root=root / "registry", repo_root=ROOT,
                requirements_file=ROOT / "requirements-experiment.txt",
                runtime_packages=[("pip", version("pip"))], max_attempts=2)
            with self.assertRaisesRegex(Exception, "did not admit"):
                foundry.generate("compare a declared estimator", client=StubClient(payload))
            self.assertEqual(load_registry(root / "registry")["capabilities"], [])

    def test_malformed_asset_contract_returns_actionable_repair_feedback(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace(
            '"id": asset_id, "path": name, "sha256":',
            '"id": asset_id, "sha256":',
        )
        self.assertNotEqual(payload["executor_source"], MINI_EXECUTOR)
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.max_attempts = 1
            with self.assertRaisesRegex(
                    ValidationError, "experiment asset requires exactly"):
                foundry.generate("compare a declared estimator", client=StubClient(payload))
            self.assertEqual(load_registry(root / "registry")["capabilities"], [])

    def test_duplicate_metric_feedback_names_the_id_and_both_locations(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace('"metrics": metrics,', '"metrics": metrics + metrics,')
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.max_attempts = 1
            with self.assertRaisesRegex(ValidationError, "'tail_error' at metrics\\[1\\] duplicates metrics\\[0\\]"):
                foundry.generate("bounded comparison", client=StubClient(payload))

    def test_repeated_failure_is_durable_and_does_not_burn_remaining_attempts(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace('"metrics": metrics,', '"metrics": metrics + metrics,')
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(payload)
            foundry = self._foundry(root)
            foundry.max_attempts = 8
            states = []
            for _ in range(2):
                with self.assertRaises(ModelWorkBlocked):
                    foundry.generate("bounded comparison", client=client, work_cache=cache,
                                     on_progress=lambda phase, state: states.append(state))
            self.assertEqual(client.calls, 2)
            self.assertEqual(states[-1]["attempts"], 2)
            self.assertEqual(states[-1]["usage"]["model_calls"], 2)
            self.assertEqual(states[-1]["status"], "blocked")
            self.assertIn("metrics[1]", states[-1]["feedback"])
            self.assertEqual(states[-1]["last_attempt"]["executor_source"], payload["executor_source"])
            self.assertEqual(len(states[-1]["requests"]), 2)

    def test_analysis_output_contract_failure_uses_bounded_format_repair_classification(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace(
            "    sys.stdout.write(json.dumps(result))",
            '    result["analysis"] = {"controls": [{"description": "observed control"}]}\n'
            "    sys.stdout.write(json.dumps(result))",
        )
        self.assertNotEqual(payload["executor_source"], MINI_EXECUTOR)
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(payload)
            foundry = self._foundry(root)
            states = []
            with self.assertRaises(ModelWorkBlocked) as blocked:
                foundry.generate("bounded comparison", client=client, work_cache=cache,
                                 on_progress=lambda phase, state: states.append(state))

            self.assertEqual(client.calls, 2)
            self.assertEqual(blocked.exception.failure_class, "model_contract")
            self.assertEqual(blocked.exception.recovery_mode, "format_repair_then_rerun")
            self.assertEqual(blocked.exception.repair_gate, "analysis_output_contract")
            self.assertEqual(states[-1]["last_failure_class"], "model_contract")
            self.assertEqual(states[-1]["last_failure_gate"], "analysis_output_contract")

    def test_current_analysis_error_drives_successive_contract_repairs(self):
        payload = self._payload()
        marker = '    sys.stdout.write(json.dumps(result))'
        initial = '    result["analysis"] = {"effect_sizes": [{"id": "effect", "description": "Point estimate with unavailable interval", "estimate": 1.0, "lower": None, "upper": None}]}\n'
        unavailable = initial.replace('"estimate": 1.0', '"status": "not_estimable", "reason": "Interval unavailable", "metric_ids": ["tail_error"], "estimate": 1.0')
        repaired = unavailable.replace('"estimate": 1.0', '"estimate": None')
        payload["executor_source"] = MINI_EXECUTOR.replace(marker, initial + marker)

        class PatchingAuthor:
            calls = 0
            max_output_tokens = 24000

            def __init__(inner_self):
                inner_self.prompts = []

            def complete(inner_self, *, system, prompt):
                inner_self.calls += 1
                inner_self.prompts.append(json.loads(prompt))
                if inner_self.calls == 1:
                    value = payload
                else:
                    old, new = (initial, unavailable) if inner_self.calls == 2 else (unavailable, repaired)
                    value = {"updates": {"executor_source": {"edits": [{"old": old, "new": new}]}}}
                return ModelResult(json.dumps(value), "stub", {"model_calls": 1, "input_tokens": 5, "output_tokens": 10}, 0.0, "stop")

        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 3
            author = PatchingAuthor()
            outcome = foundry.generate("bounded comparison", client=author)
        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 3)
        first, second = author.prompts[1:]
        for prompt in (first, second):
            self.assertEqual(prompt["format_repair"]["repair_kind"], "analysis_output_contract")
            self.assertIn("not_estimable", prompt["format_repair"]["analysis_contract"]["effect_sizes"])
            self.assertIsNone(prompt["repair_request"]["author_response_error"])
            self.assertEqual(prompt["repair_request"]["candidate_failure"]["gate"], "analysis_output_contract")
        self.assertIn("must be a finite number", first["repair_request"]["previous_error"])
        self.assertIn("not_estimable numeric fields must be null", second["repair_request"]["previous_error"])
        self.assertEqual(first["format_repair"]["observed_analysis"]["effect_sizes"][0]["estimate"], 1.0)
        self.assertEqual(second["format_repair"]["observed_analysis"]["effect_sizes"][0]["status"], "not_estimable")
        self.assertEqual(outcome["candidate"]["executor_source"], MINI_EXECUTOR.replace(marker, repaired + marker))

    def test_stage_seconds_intent_failure_uses_format_repair_without_review_panel(self):
        payload = self._payload()
        payload["experiment_intent"]["stage_seconds"].pop("reassessment")
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            author = StubClient(payload)
            foundry = self._foundry(root)
            states = []
            with self.assertRaises(ModelWorkBlocked) as blocked:
                foundry.generate(
                    "bounded comparison", client=author, work_cache=cache,
                    on_progress=lambda phase, state: states.append(state),
                )

        self.assertEqual(author.calls, 2)
        self.assertEqual(foundry.reviewer_client.calls, 0)
        self.assertEqual(blocked.exception.failure_class, "model_contract")
        self.assertEqual(blocked.exception.recovery_mode, "format_repair_then_rerun")
        self.assertEqual(blocked.exception.repair_gate, "author_response_contract")
        self.assertEqual(states[-1]["last_failure_class"], "model_contract")
        self.assertEqual(states[-1]["last_failure_gate"], "author_response_contract")
        self.assertTrue(any(
            item.get("failure_signature") == "author_response_contract:experiment_intent"
            for item in states[-1]["repair_ledger"]
        ))

    def test_executor_output_shape_failure_gets_targeted_repair_not_scientific_review(self):
        payload = self._payload()
        payload["executor_source"] = MINI_EXECUTOR.replace(
            "    sys.stdout.write(json.dumps(result))",
            '    result.pop("observations")\n'
            '    result["unrequested"] = True\n'
            "    sys.stdout.write(json.dumps(result))",
        )
        self.assertNotEqual(payload["executor_source"], MINI_EXECUTOR)
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(payload)
            complete = client.complete

            def inspect_repair(**kwargs):
                if client.calls:
                    prompt = json.loads(kwargs["prompt"])
                    repair = prompt["format_repair"]
                    self.assertEqual(repair["repair_kind"], "executor_output_contract")
                    self.assertEqual(repair["missing_fields"], ["observations"])
                    self.assertEqual(repair["unexpected_fields"], ["unrequested"])
                    self.assertIn("Preserve the frozen experiment intent", repair["instructions"])
                return complete(**kwargs)

            client.complete = inspect_repair
            states = []
            foundry = self._foundry(root)
            with self.assertRaises(ModelWorkBlocked) as blocked:
                foundry.generate("bounded comparison", client=client, work_cache=cache,
                                 on_progress=lambda phase, state: states.append(state))

            self.assertEqual(client.calls, 2)
            self.assertEqual(foundry.reviewer_client.calls, 0)
            self.assertEqual(blocked.exception.failure_class, "model_contract")
            self.assertEqual(blocked.exception.recovery_mode, "format_repair_then_rerun")
            self.assertEqual(blocked.exception.repair_gate, "program_output_contract")
            self.assertEqual(states[-1]["last_failure_class"], "model_contract")
            self.assertEqual(states[-1]["last_failure_gate"], "program_output_contract")

    def test_repeated_sandbox_exception_stops_even_when_source_edits_shift_lines(self):
        payload = self._payload()

        class RepeatingSandboxAuthor:
            def __init__(self):
                self.calls = 0

            def complete(inner_self, *, system, prompt):
                inner_self.calls += 1
                candidate = deepcopy(payload)
                line = '    run_count = int(experiment["run_count"])'
                replacement = (
                    f"    # distinct-source-revision-{inner_self.calls}\n"
                    '    result = 1 / 0\n'
                    + line
                )
                candidate["executor_source"] = MINI_EXECUTOR.replace(
                    line, replacement, 1)
                return ModelResult(
                    json.dumps(candidate), "stub",
                    {"model_calls": 1, "input_tokens": 4, "output_tokens": 8},
                    0.0, "stop",
                )

        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 6
            author = RepeatingSandboxAuthor()
            with self.assertRaises(ModelWorkBlocked) as blocked:
                foundry.generate("bounded comparison", client=author)

        self.assertEqual(author.calls, 2)
        signature = blocked.exception.repair_ledger[-1]["failure_signature"]
        self.assertTrue(signature.startswith("sandbox:ZeroDivisionError:division by zero"))
        self.assertEqual(blocked.exception.failure_class, "experiment_capability_repair")

    def test_sandbox_failure_signature_ignores_paths_and_line_numbers(self):
        first = ValidationError(
            'executor failed in the sandbox: Traceback (most recent call last):\n'
            '  File "/tmp/first/program.py", line 40, in main\n'
            '    value = 1 / denominator\n'
            'ZeroDivisionError: division by zero')
        shifted = ValidationError(
            'executor failed in the sandbox: Traceback (most recent call last):\n'
            '  File "/tmp/second/program.py", line 88, in main\n'
            '    value = 1 / denominator\n'
            'ZeroDivisionError: division by zero')
        self.assertEqual(_sandbox_failure_signature(first),
                         _sandbox_failure_signature(shifted))

    def test_program_gate_failure_signature_tracks_scientific_result_not_candidate_hash(self):
        from scisaurus.runtime.program_gates import ProgramGateRejected
        first = ProgramGateRejected(
            "independent recalculation did not accept the candidate",
            {"decision": "rejected", "checks": [{
                "id": "metric_agreement", "outcome": "failed",
                "evidence": "Metrics do not match.",
            }], "metric_recalculations": [{
                "metric_id": "control_slope", "reported_value": 0.0,
                "recalculated_value": None, "tolerance": 1e-6, "matches": False,
            }]}, gate="independent_recalculation")
        revised_source_same_result = ProgramGateRejected(
            "independent recalculation did not accept the candidate",
            {"decision": "rejected", "checks": [{
                "id": "metric_agreement", "outcome": "failed",
                "evidence": "Still mismatched after a source edit.",
            }], "metric_recalculations": [{
                "metric_id": "control_slope", "reported_value": 0.0,
                "recalculated_value": None, "tolerance": 1e-6, "matches": False,
            }]}, gate="independent_recalculation")
        materially_different_result = ProgramGateRejected(
            "independent recalculation did not accept the candidate",
            {"decision": "rejected", "checks": [{
                "id": "metric_agreement", "outcome": "failed",
                "evidence": "Metrics do not match.",
            }], "metric_recalculations": [{
                "metric_id": "control_slope", "reported_value": 0.2,
                "recalculated_value": 0.1, "tolerance": 1e-6, "matches": False,
            }]}, gate="independent_recalculation")

        self.assertEqual(_program_gate_failure_signature(first),
                         _program_gate_failure_signature(revised_source_same_result))
        self.assertNotEqual(_program_gate_failure_signature(first),
                            _program_gate_failure_signature(materially_different_result))

    def test_independent_gate_disagreement_requires_adjudication_before_source_patch(self):
        from scisaurus.runtime.program_gates import ProgramGateRejected

        rejected = ProgramGateRejected(
            "independent recalculation did not accept the candidate",
            {"decision": "rejected", "checks": [{
                "id": "metric_agreement", "outcome": "failed",
                "evidence": "Primary outcomes do not agree.",
            }], "metric_recalculations": [{
                "metric_id": "tail_error", "reported_value": 0.5,
                "recalculated_value": 0.4, "tolerance": 1e-12, "matches": False,
            }]}, gate="independent_recalculation")

        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 8
            author = StubClient(self._payload())
            with patch("scisaurus.runtime.capability_foundry.admit_program_candidate",
                       side_effect=rejected):
                with self.assertRaises(ModelWorkBlocked) as blocked:
                    foundry.generate("bounded comparison", client=author)

        ledger = blocked.exception.repair_ledger
        self.assertEqual(author.calls, 1)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["next_action"], "methods_adjudication_before_source_repair")
        self.assertTrue(ledger[0]["failure_signature"].startswith(
            "program_gate:independent_recalculation:"))
        self.assertEqual(blocked.exception.failure_class, "experiment_capability_repair")

    def test_current_scientific_gate_owns_exception_over_historical_response_diagnostic(self):
        from scisaurus.runtime.program_gates import ProgramGateRejected
        rejected = ProgramGateRejected("Current recalculation rejected", {
            "decision": "rejected", "checks": [{"id": "selection", "outcome": "failed", "evidence": "Current rows disagree."}],
        }, gate="independent_recalculation")
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            cache = self._cache(root)
            author = StubClient(self._payload())
            with patch("scisaurus.runtime.capability_foundry.admit_program_candidate", side_effect=rejected):
                with self.assertRaises(ModelWorkBlocked):
                    foundry.generate("bounded comparison", client=author, work_cache=cache)
            state = cache.entries()[0]
            key = state.pop("cache_ref").split("/")[-1].split("@")[0]
            state["model_diagnostics"] = [{"outcome": "incomplete_response", "error": "Old length failure"}]
            cache.put(key, state)
            with self.assertRaises(ModelWorkBlocked) as blocked:
                foundry.generate("bounded comparison", client=author, work_cache=cache)
            self.assertEqual(author.calls, 1)
            self.assertEqual(blocked.exception.failure_class, "experiment_capability_repair")
            self.assertEqual(blocked.exception.repair_gate, "independent_recalculation")
            self.assertEqual(blocked.exception.recovery_mode, "repair_then_rerun")
            self.assertNotIn("incomplete", blocked.exception.repair_feedback["feedback"])
            self.assertEqual(blocked.exception.repair_feedback["foundry_work_ref"], cache.entries()[0]["cache_ref"])

    def test_truncated_patch_preserves_scientific_review_without_reclassifying_it_as_a_review_failure(self):
        payload = self._payload()
        rejected_review = {
            "status": "rejected",
            "checks": [
                {"id": name, "outcome": "failed", "evidence": "The retained program contradicts the stated mechanism."}
                for name in sorted(PROGRAM_REVIEW_CHECKS)
            ],
            "findings": [{
                "severity": "blocking",
                "finding": "The primary slope is fixed by the equation rather than identified by the experiment.",
                "evidence": "The executor substitutes gap directly into a_over_d, making the log slope an identity.",
                "required_change": "Change the constitutive expression so the gap exponent is derived rather than imposed, then update the independent validator.",
            }, {
                "severity": "warning",
                "finding": "The validator repeats the executor's estimator.",
                "evidence": "Both implementations call the same slope helper.",
                "required_change": "Calculate the slope independently and recompute a bootstrap interval.",
            }],
            "limitations": ["The synthetic fixture does not establish an empirical material result."],
        }

        class CandidateThenTruncatedPatch:
            def __init__(self):
                self.calls = 0
                self.max_output_tokens = 24000
                self.output_budgets = []
                self.prompts = []
                self.authoring_prompts = []

            def complete(self, *, system, prompt):
                self.calls += 1
                self.output_budgets.append(self.max_output_tokens)
                self.prompts.append(prompt)
                request = json.loads(prompt)
                if request.get("assignment") == "continue_truncated_experiment_author_json":
                    return ModelResult(json.dumps({
                        "marker": "wrong-continuation-marker",
                        "continuation": "}",
                    }), "stub", {"model_calls": 1}, 0.0, "stop")
                self.authoring_prompts.append(prompt)
                if self.calls == 1:
                    return ModelResult(
                        json.dumps(payload), "stub",
                        {"model_calls": 1, "input_tokens": 5, "output_tokens": 10},
                        0.0, "stop",
                    )
                return ModelResult(
                    '{"updates":{"executor_source":{"edits":[', "stub",
                    {"model_calls": 1, "input_tokens": 7, "output_tokens": 4096},
                    0.0, "length",
                )

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.max_attempts = 3
            author = CandidateThenTruncatedPatch()
            foundry.reviewer_client = StubClient(rejected_review)
            with self.assertRaises(ModelWorkBlocked) as blocked:
                foundry.generate("bounded comparison", client=author)

        self.assertEqual(author.calls, 4)
        self.assertEqual(len(author.output_budgets), author.calls)
        first_repair_prompt = json.loads(author.authoring_prompts[1])
        self.assertEqual(first_repair_prompt["assignment"], "repair_existing_experiment_candidate")
        self.assertNotIn("capability_brief", first_repair_prompt)
        patch_prompt = json.loads(author.authoring_prompts[-1])
        self.assertEqual(patch_prompt["assignment"], "repair_existing_experiment_candidate")
        self.assertNotIn("capability_brief", patch_prompt)
        self.assertEqual(set(patch_prompt["output_contract"]), {"updates"})
        self.assertNotIn("executor_source", patch_prompt["current_candidate"])
        self.assertEqual(
            patch_prompt["current_candidate"]["source_context"]["executor_source"]["source_sha256"],
            hashlib.sha256(payload["executor_source"].encode()).hexdigest())
        self.assertEqual(
            patch_prompt["current_candidate"]["source_context"]["executor_source"]["source"],
            payload["executor_source"])
        self.assertIn(
            "The primary slope is fixed by the equation",
            json.dumps(patch_prompt["repair_request"]["validation_feedback"]))
        self.assertEqual(blocked.exception.failure_class, "model_contract")
        self.assertEqual(blocked.exception.recovery_mode, "format_repair_then_rerun")
        self.assertEqual(blocked.exception.repair_gate, "author_response_format")
        feedback = blocked.exception.repair_feedback
        self.assertEqual(feedback["validation_feedback"]["decision"], "rejected")
        self.assertEqual(feedback["validation_feedback"]["findings"][0]["severity"], "blocking")
        self.assertIn("equation", blocked.exception.research_review["required_repairs"][0]["repair"])
        self.assertIn("bootstrap interval", blocked.exception.research_review["required_repairs"][1]["repair"])
        self.assertEqual(blocked.exception.research_review["checks"][0]["outcome"], "failed")

    def test_compact_output_contract_repair_is_applied_and_replayed_through_all_gates(self):
        payload = self._payload()
        invalid_lines = (
            '    result.pop("observations")\n'
            '    result["unrequested"] = True\n'
        )
        marker = '    sys.stdout.write(json.dumps(result))'
        payload["executor_source"] = MINI_EXECUTOR.replace(
            marker, invalid_lines + marker)
        self.assertNotEqual(payload["executor_source"], MINI_EXECUTOR)

        class PatchingAuthor:
            def __init__(self):
                self.calls = 0
                self.max_output_tokens = 24000
                self.prompts = []
                self.output_budgets = []

            def complete(inner_self, *, system, prompt):
                inner_self.calls += 1
                inner_self.prompts.append(prompt)
                inner_self.output_budgets.append(inner_self.max_output_tokens)
                if inner_self.calls == 1:
                    value = payload
                elif inner_self.calls == 2:
                    value = {"invalid_author_envelope": True}
                else:
                    value = {"updates": {"executor_source": {"edits": [
                        {"old": invalid_lines, "new": ""},
                    ]}}}
                return ModelResult(
                    json.dumps(value), "stub",
                    {"model_calls": 1, "input_tokens": 5, "output_tokens": 10},
                    0.0, "stop",
                )

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.max_attempts = 3
            author = PatchingAuthor()
            outcome = foundry.generate("bounded comparison", client=author)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 3)
        self.assertEqual(author.output_budgets, [24000, 24000, 24000])
        repair_prompt = json.loads(author.prompts[1])
        self.assertEqual(repair_prompt["format_repair"]["repair_kind"], "executor_output_contract")
        self.assertEqual(repair_prompt["format_repair"]["missing_fields"], ["observations"])
        self.assertEqual(repair_prompt["format_repair"]["unexpected_fields"], ["unrequested"])

        retry_prompt = json.loads(author.prompts[2])
        failure = retry_prompt["repair_request"]["candidate_failure"]
        self.assertEqual(failure["gate"], "program_output_contract")
        self.assertEqual(retry_prompt["repair_request"]["repair_scope"]["active_issue"], "candidate_failure")
        self.assertIsNone(repair_prompt["repair_request"]["author_response_error"])
        self.assertEqual(failure["missing_fields"], ["observations"])
        self.assertEqual(failure["unexpected_fields"], ["unrequested"])
        self.assertIn("unexpected=['unrequested']", retry_prompt["repair_request"]["previous_error"])
        self.assertIn("observed keys", retry_prompt["repair_request"]["author_response_error"])
        for prompt in (repair_prompt, retry_prompt):
            context = prompt["current_candidate"]["source_context"]["executor_source"]
            self.assertEqual(context["source"], payload["executor_source"])
            self.assertIn(invalid_lines, context["source"])
            self.assertEqual(context["source_sha256"], hashlib.sha256(context["source"].encode()).hexdigest())

    def test_repeated_admission_gate_stops_before_authoring_budget_is_spent(self):
        payload = self._payload()

        class VaryingAuthor:
            def __init__(self, value):
                self.value = value
                self.calls = 0

            def complete(self, *, system, prompt):
                self.calls += 1
                candidate = deepcopy(self.value)
                candidate["executor_source"] = (
                    f"# repair-{self.calls}\n" + candidate["executor_source"])
                return ModelResult(json.dumps(candidate), "author", {"model_calls": 1}, 0.0, "stop")

        rejecting_review = StubClient({
            "status": "rejected",
            "checks": [
                {"id": key, "outcome": "failed", "evidence": "The fixture is not scientifically adequate."}
                for key in sorted(PROGRAM_REVIEW_CHECKS)
            ],
            "findings": [{"severity": "blocking", "finding": "same gate defect",
                           "evidence": "same evidence", "required_change": "change the design"}],
            "limitations": ["The fixture remains bounded."],
        })
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.reviewer_client = rejecting_review
            author = VaryingAuthor(payload)
            with self.assertRaisesRegex(ModelWorkBlocked, "same gate defect"):
                foundry.generate("bounded comparison", client=author)
            self.assertEqual(author.calls, 2)
            self.assertEqual(rejecting_review.calls, 2)

    def test_distinct_recalculation_findings_can_use_remaining_source_repairs(self):
        payload = self._payload()

        class VaryingAuthor:
            def __init__(self, value):
                self.value = value
                self.calls = 0

            def complete(inner_self, *, system, prompt):
                inner_self.calls += 1
                candidate = deepcopy(inner_self.value)
                candidate["executor_source"] = (
                    f"# source-repair-{inner_self.calls}\\n" + candidate["executor_source"])
                return ModelResult(json.dumps(candidate), "author",
                                   {"model_calls": 1}, 0.0, "stop")

        class ChangingReviewer:
            def __init__(self):
                self.calls = 0

            def complete(inner_self, *, system, prompt):
                inner_self.calls += 1
                verdict = deepcopy(CapabilityFoundryTests._review_payload())
                if inner_self.calls < 3:
                    verdict["status"] = "rejected"
                    verdict["checks"] = [
                        {"id": check, "outcome": "failed",
                         "evidence": f"recalculation mismatch revision {inner_self.calls}"}
                        for check in sorted(PROGRAM_REVIEW_CHECKS)
                    ]
                    verdict["findings"] = [{
                        "severity": "blocking",
                        "finding": "Primary metric does not match raw observations",
                        "evidence": f"independent recalculation {inner_self.calls}",
                        "required_change": "Repair the computation, then replay it.",
                    }]
                verdict = _add_prior_review_checks(verdict, prompt)
                return ModelResult(json.dumps(verdict), "reviewer",
                                   {"model_calls": 1}, 0.0, "stop")

        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self._foundry(root)
            foundry.max_attempts = 5
            author = VaryingAuthor(payload)
            reviewer = ChangingReviewer()
            foundry.reviewer_client = reviewer
            outcome = foundry.generate("bounded comparison", client=author)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(author.calls, 3)
        self.assertEqual(reviewer.calls, 3)

    def test_alternating_validation_errors_cannot_consume_the_full_allowance(self):
        client = StubClient({})
        def alternate(*, system, prompt):
            client.calls += 1
            value = {"extra_a" if client.calls % 2 else "extra_b": True}
            return ModelResult(json.dumps(value), "stub", {"model_calls": 1}, 0, "stop")
        client.complete = alternate
        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            foundry.max_attempts = 8
            with self.assertRaises(ModelWorkBlocked):
                foundry.generate("bounded comparison", client=client)
            self.assertEqual(client.calls, 3)

    def test_recorded_response_resumes_validation_without_another_model_call(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            client = StubClient(self._payload())
            foundry = self._foundry(root)
            def stop_after_response(phase, state):
                if phase == "response_received":
                    raise KeyboardInterrupt("fixture interruption")
            with self.assertRaises(KeyboardInterrupt):
                foundry.generate("bounded comparison", client=client, work_cache=cache,
                                 on_progress=stop_after_response)
            outcome = foundry.generate("bounded comparison", client=client, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(client.calls, 1)
            self.assertEqual(foundry.generate("bounded comparison", client=client, work_cache=cache), outcome)
            self.assertEqual(client.calls, 1)

    def test_changed_format_recovery_assignment_does_not_reuse_blocked_cache(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            cache = self._cache(root)
            foundry = self._foundry(root)
            foundry.max_attempts = 1

            class InvalidAuthor:
                model = "stub"

                def __init__(inner_self):
                    inner_self.calls = 0

                def complete(inner_self, *, system, prompt):
                    inner_self.calls += 1
                    return ModelResult("{}", "stub", {"model_calls": 1}, 0.0, "stop")

            invalid = InvalidAuthor()
            with self.assertRaises(ModelWorkBlocked):
                foundry.generate(
                    "bounded comparison", client=invalid, work_cache=cache,
                    required_intent=INTENT)
            self.assertEqual(invalid.calls, 1)

            recovery = StubClient(self._payload())
            outcome = foundry.generate(
                "bounded comparison\nFormat-recovery policy revision: analysis-output-contract-2",
                client=recovery, work_cache=cache, required_intent=INTENT)

        self.assertEqual(outcome["status"], "registered")
        self.assertEqual(recovery.calls, 1)

    def test_author_outer_json_closer_is_repaired_without_a_second_model_call(self):
        payload = self._payload()

        class TruncatedEnvelopeClient(StubClient):
            def complete(self, *, system, prompt):
                self.calls += 1
                return ModelResult(json.dumps(self.payload)[:-1], "stub",
                                   {"model_calls": 1}, 0.0, "stop")

        with tempfile.TemporaryDirectory() as path:
            foundry = self._foundry(Path(path))
            client = TruncatedEnvelopeClient(payload)
            outcome = foundry.generate("bounded comparison", client=client)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(client.calls, 1)

    def test_complete_program_envelope_is_still_gated_when_provider_reports_length(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)

            class LengthClient(StubClient):
                def complete(self, *, system, prompt):
                    self.calls += 1
                    return ModelResult(
                        json.dumps(self.payload), "stub",
                        {"model_calls": 1, "input_tokens": 3, "output_tokens": 4},
                        0.0, "length",
                    )

            author = LengthClient(self._payload())
            outcome = self._foundry(root).generate(
                "bounded comparison", client=author)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(author.calls, 1)

    def test_complete_program_envelope_is_rejected_for_non_length_abnormal_finish(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)

            class FilteredClient(StubClient):
                def complete(self, *, system, prompt):
                    self.calls += 1
                    return ModelResult(
                        json.dumps(self.payload), "stub",
                        {"model_calls": 1, "input_tokens": 3, "output_tokens": 4},
                        0.0, "content_filter",
                    )

            author = FilteredClient(self._payload())
            foundry = self._foundry(root)
            foundry.max_attempts = 1
            with self.assertRaises(ModelWorkBlocked) as raised:
                foundry.generate("bounded comparison", client=author)
            self.assertEqual(author.calls, 1)
            self.assertEqual(load_registry(root / "registry")["capabilities"], [])
            responses = raised.exception.model_diagnostics["author_responses"]
            self.assertEqual(responses[-1]["finish_reason"], "content_filter")
            self.assertEqual(responses[-1]["outcome"], "inadmissible_finish_reason")
            self.assertIn("not admissible", str(raised.exception))

    def test_length_response_with_missing_json_closer_is_not_salvaged(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)

            class TruncatedEnvelopeClient(StubClient):
                def complete(self, *, system, prompt):
                    self.calls += 1
                    if self.calls > 1:
                        return ModelResult(json.dumps({
                            "marker": "wrong-continuation-marker",
                            "continuation": "}",
                        }), "stub", {"model_calls": 1}, 0.0, "stop")
                    return ModelResult(
                        json.dumps(self.payload)[:-1], "stub",
                        {"model_calls": 1, "input_tokens": 3, "output_tokens": 4},
                        0.0, "length",
                    )

            author = TruncatedEnvelopeClient(self._payload())
            foundry = self._foundry(root)
            foundry.max_attempts = 1
            with self.assertRaises(ModelWorkBlocked) as raised:
                foundry.generate("bounded comparison", client=author)
            self.assertEqual(author.calls, 2)
            self.assertEqual(load_registry(root / "registry")["capabilities"], [])
            responses = raised.exception.model_diagnostics["author_responses"]
            self.assertEqual(responses[-1]["finish_reason"], "length")
            self.assertEqual(responses[-1]["outcome"], "incomplete_response")

    def test_incomplete_author_response_records_finish_reason_and_partial_usage(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)

            class TruncatedClient:
                calls = 0

                def complete(self, *, system, prompt):
                    self.calls += 1
                    return ModelResult(
                        '{"executor_source":' if self.calls == 1 else '{"validator_source":', "stub",
                        {"model_calls": 1, "input_tokens": 11, "output_tokens": 7},
                        0.0, "length",
                    )

            author = TruncatedClient()
            with self.assertRaises(ModelWorkBlocked) as raised:
                self._foundry(root).generate("bounded comparison", client=author)
            self.assertIn("finish_reason=length", str(raised.exception))
            self.assertGreater(author.calls, 4)
            self.assertEqual(raised.exception.usage["model_calls"], author.calls)
            self.assertEqual(raised.exception.usage["input_tokens"], author.calls * 11)
            self.assertEqual(raised.exception.usage["output_tokens"], author.calls * 7)
            self.assertEqual(raised.exception.failure_class, "model_contract")
            self.assertEqual(raised.exception.recovery_mode, "format_repair_then_rerun")
            self.assertEqual(raised.exception.repair_gate, "author_response_format")
            responses = raised.exception.model_diagnostics["author_responses"]
            author_failures = [item for item in responses
                               if item.get("outcome") == "incomplete_response"]
            self.assertEqual(len(author_failures), 2)
            self.assertEqual(author_failures[-1]["finish_reason"], "length")

    def test_deadline_blocks_dispatch_before_a_model_call(self):
        with tempfile.TemporaryDirectory() as path:
            client = StubClient(self._payload())
            with self.assertRaisesRegex(ValidationError, "mission deadline"):
                self._foundry(Path(path)).generate("bounded comparison", client=client, deadline=0)
            self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()


class IndependentValidatorAuthorshipTests(unittest.TestCase):
    def test_captured_validator_response_requires_latest_successful_dispatch_owner(self):
        from scisaurus.runtime.capability_foundry import _captured_validator_request
        response = {'model': 'peer', 'finish_reason': 'stop', 'elapsed_seconds': 1.5,
                    'usage': {'model_calls': 1}, 'text': '{"validator_source":"source"}'}
        request = {key: value for key, value in response.items() if key != 'text'}
        assignment = {'design': 'frozen'}
        identity = hashlib.sha256(canonical_bytes(assignment)).hexdigest()
        prompt = json.dumps(assignment)
        request.update(role='methods.validator-author', assignment_sha256=identity, status='succeeded',
                       prompt=prompt, prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                       response_sha256=hashlib.sha256(response['text'].encode()).hexdigest())
        self.assertEqual(_captured_validator_request({'requests': [request]}, identity, response), request)
        for update in ({'status': 'result_unknown'}, {'status': 'started'}, {'model': 'other'},
                       {'elapsed_seconds': 2.0}, {'usage': {'model_calls': 2}},
                       {'response_sha256': 'wrong'}, {'finish_reason': 'length'}):
            newer = {**request, **update}
            self.assertIsNone(_captured_validator_request({'requests': [request, newer]},
                                                         identity, response))

    def test_validator_model_transport_preserves_source_and_rejects_ambiguous_payloads(self):
        from scisaurus.runtime.capability_foundry import _independent_validator_source
        body = json.dumps({"validator_source": MINI_VALIDATOR})
        for text in (body, '```json\n' + body + '\n```', 'reasoning</think>' + body):
            self.assertEqual(_independent_validator_source({"text": text, "finish_reason": "stop"}),
                             MINI_VALIDATOR)
        for text in (body + ' trailing', body + body, body[:-1],
                     '{"validator_source":"a","validator_source":"b"}',
                     '{"validator_source":"a","extra":NaN}',
                     json.dumps({"validator_source": MINI_VALIDATOR, "extra": True})):
            with self.subTest(text=text[:60]), self.assertRaises(ValidationError):
                _independent_validator_source({"text": text, "finish_reason": "stop"})
        with self.assertRaises(ValidationError):
            _independent_validator_source({"text": body, "finish_reason": "tool_calls"})

    def test_fenced_validator_runs_all_admission_gates_without_another_author_call(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = CapabilityFoundryTests._foundry(Path(path))
            producer = StubClient(CapabilityFoundryTests._payload())
            class FencedValidator:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    return ModelResult('```json\n' + json.dumps({'validator_source': MINI_VALIDATOR}) + '\n```',
                                       'independent', {'model_calls': 1}, 0, 'stop')
            foundry.validator_client = FencedValidator()
            outcome = foundry.generate('bounded comparison', client=producer)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 1))

    def test_captured_validator_transport_is_revalidated_before_another_dispatch(self):
        from scisaurus.core.schema import json_object
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            cache = CapabilityFoundryTests._cache(self, root)
            producer = StubClient(CapabilityFoundryTests._payload())
            class FencedValidator:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    return ModelResult('```json\n' + json.dumps({'validator_source': MINI_VALIDATOR}) + '\n```',
                                       'independent', {'model_calls': 1}, 0, 'stop')
            foundry.validator_client = FencedValidator()
            def prior_decoder(raw, name='JSON', **kwargs):
                return json_object(raw, name, model_envelope=False)
            def pause_at_failure(phase, state):
                if phase == 'independent_validator_contract_failed':
                    raise CapabilityDeadlineError('captured transport failure')
            with patch('scisaurus.runtime.capability_foundry.parse_complete_json_object',
                       side_effect=prior_decoder), self.assertRaises(CapabilityDeadlineError):
                foundry.generate('bounded comparison', client=producer, work_cache=cache,
                                 on_progress=pause_at_failure)
            phases = []
            outcome = foundry.generate('bounded comparison', client=producer, work_cache=cache,
                                       on_progress=lambda phase, state: phases.append(phase))
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 1))
            self.assertIn('independent_validator_transport_revalidated', phases)

    def routed_foundry(self, root, models=("primary", "peer")):
        foundry = CapabilityFoundryTests._foundry(root)
        foundry.validator_client = None
        foundry.max_attempts = 4
        foundry.model_config["role_models"] = {"methods.validator-author": {"model": models[0]}}
        foundry.model_config["role_model_fallbacks"] = {
            "methods.validator-author": [{"model": model} for model in models[1:]]}
        return foundry

    def test_validator_routes_use_shared_identity_and_parent_resolution(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self.routed_foundry(Path(path))
            foundry.model_config["role_models"] = {"methods": {"model": "parent"},
                "review.methods": {"model": "reviewer"}}
            foundry.model_config["role_model_fallbacks"] = {
                "methods": [{"model": "parent", "reasoning_effort": "high"},
                            {"model": "peer"}, {"model": "peer", "timeout_seconds": 90}],
                "review.methods": [{"model": "review-peer"}]}
            routes = foundry._format_model_routes("methods.validator-author", 24000,
                                                 inherited_role="review.methods")
            self.assertEqual([route["model"] for route in routes], ["parent", "peer"])

    def test_validator_owned_role_never_borrows_reviewer_fallback(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self.routed_foundry(Path(path))
            foundry.model_config["role_model_fallbacks"] = {"review.methods": [{"model": "review-peer"}]}
            routes = foundry._format_model_routes("methods.validator-author", 24000,
                                                 inherited_role="review.methods")
            self.assertEqual([route["model"] for route in routes], ["primary"])

    def test_legacy_validator_role_inherits_review_primary_and_peer(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self.routed_foundry(Path(path))
            foundry.model_config["role_models"] = {"review.methods": {"model": "reviewer"}}
            foundry.model_config["role_model_fallbacks"] = {"review.methods": [{"model": "review-peer"}]}
            routes = foundry._format_model_routes("methods.validator-author", 24000,
                                                 inherited_role="review.methods")
            self.assertEqual([route["model"] for route in routes], ["reviewer", "review-peer"])

    def test_empty_validator_responses_advance_distinct_routes_once(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = self.routed_foundry(Path(path), ("primary", "peer", "last"))
            producer = StubClient(CapabilityFoundryTests._payload())
            seen = []
            class Validator:
                def __init__(inner, **config):
                    inner.model = config["model"]
                def complete(inner, *, system, prompt):
                    seen.append(inner.model)
                    packet = json.loads(prompt)
                    self.assertNotIn("executor_source", packet)
                    self.assertNotIn("metrics", packet)
                    text = json.dumps({"validator_source": MINI_VALIDATOR}) if inner.model == "last" else ""
                    return ModelResult(text, inner.model, {"model_calls": 1}, 0, "length")
            with patch("scisaurus.runtime.capability_foundry.ModelClient", Validator):
                outcome = foundry.generate("bounded comparison", client=producer)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(seen, ["primary", "peer", "last"])
            self.assertEqual(producer.calls, 1)

    def test_exhausted_validator_response_routes_do_not_dispatch_again(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self.routed_foundry(root)
            cache = CapabilityFoundryTests._cache(self, root)
            producer = StubClient(CapabilityFoundryTests._payload())
            empty = StubClient({})
            with patch("scisaurus.runtime.capability_foundry.ModelClient", return_value=empty):
                for _ in range(2):
                    with self.assertRaises(ModelWorkBlocked) as blocked:
                        foundry.generate("bounded comparison", client=producer, work_cache=cache)
                    self.assertEqual(blocked.exception.repair_gate, "independent_validator_contract")
            self.assertEqual((producer.calls, empty.calls), (1, 2))

    def test_unknown_validator_fallback_resumes_next_route_after_reordering(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = self.routed_foundry(root, ("primary", "peer", "last"))
            cache = CapabilityFoundryTests._cache(self, root)
            producer = StubClient(CapabilityFoundryTests._payload())
            producer.model = "stub"
            seen = []
            class Validator:
                def __init__(inner, **config):
                    inner.model = config["model"]
                def complete(inner, *, system, prompt):
                    seen.append(inner.model)
                    if inner.model == "peer":
                        raise ModelCallError("provider outcome unknown")
                    text = json.dumps({"validator_source": MINI_VALIDATOR}) if inner.model == "last" else ""
                    return ModelResult(text, inner.model, {"model_calls": 1}, 0, "length")
            with patch("scisaurus.runtime.capability_foundry.ModelClient", Validator):
                with self.assertRaises(ModelCallError):
                    foundry.generate("bounded comparison", client=producer, work_cache=cache)
                foundry.model_config["role_model_fallbacks"]["methods.validator-author"].reverse()
                outcome = foundry.generate("bounded comparison", client=producer, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(seen, ["primary", "peer", "last"])
            self.assertEqual(producer.calls, 1)

    def test_reviewer_route_recovery_preserves_validator_across_config_rekey(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            foundry.reviewer_client = None
            foundry.model_config["role_models"] = {"review.methods": {"model": "first"}}
            foundry.model_config["role_model_fallbacks"] = {"review.methods": [{"model": "second"}]}
            producer = StubClient(CapabilityFoundryTests._payload())
            producer.model = "stub"
            cache = CapabilityFoundryTests._cache(self, root)
            seen = []
            class Reviewer:
                def __init__(inner, **config):
                    inner.model = config["model"]
                def complete(inner, *, system, prompt):
                    seen.append(inner.model)
                    value = CapabilityFoundryTests._review_payload() if inner.model == "last" else {}
                    return ModelResult(json.dumps(value), inner.model, {"model_calls": 1}, 0, "stop")
            with patch("scisaurus.runtime.capability_foundry.ModelClient", Reviewer):
                with self.assertRaises(ModelWorkBlocked) as blocked:
                    foundry.generate("bounded comparison", client=producer, work_cache=cache)
                self.assertEqual(blocked.exception.repair_gate, "review_response_format")
                foundry.model_config["role_model_fallbacks"]["review.methods"].append({"model": "last"})
                outcome = foundry.generate("bounded comparison", client=producer, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(seen, ["first", "second", "last"])
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 1))

    def test_unknown_reviewer_fallback_recovers_only_to_next_distinct_route(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            foundry.reviewer_client = None
            foundry.model_config["role_models"] = {"review.methods": {"model": "first"}}
            foundry.model_config["role_model_fallbacks"] = {
                "review.methods": [{"model": "unknown"}, {"model": "last"}]}
            producer = StubClient(CapabilityFoundryTests._payload())
            producer.model = "stub"
            cache = CapabilityFoundryTests._cache(self, root)
            seen = []
            class Reviewer:
                def __init__(inner, **config):
                    inner.model = config["model"]
                def complete(inner, *, system, prompt):
                    seen.append(inner.model)
                    if inner.model == "unknown":
                        raise ModelCallError("unknown dispatched review")
                    value = CapabilityFoundryTests._review_payload() if inner.model == "last" else {}
                    return ModelResult(json.dumps(value), inner.model, {"model_calls": 1}, 0, "stop")
            with patch("scisaurus.runtime.capability_foundry.ModelClient", Reviewer):
                with self.assertRaises(ModelCallError):
                    foundry.generate("bounded comparison", client=producer, work_cache=cache)
                outcome = foundry.generate("bounded comparison", client=producer, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(seen, ["first", "unknown", "last"])
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 1))

    def test_unknown_reviewer_suffix_is_preserved_without_replay(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            foundry.reviewer_client = None
            foundry.model_config["role_models"] = {"review.methods": {"model": "first"}}
            foundry.model_config["role_model_fallbacks"] = {"review.methods": [{"model": "peer"}]}
            producer = StubClient(CapabilityFoundryTests._payload())
            producer.model = "stub"
            cache = CapabilityFoundryTests._cache(self, root)
            seen = []
            class Reviewer:
                def __init__(inner, **config):
                    inner.model = config["model"]
                def complete(inner, *, system, prompt):
                    seen.append(inner.model)
                    if inner.model == "peer":
                        return ModelResult(json.dumps(CapabilityFoundryTests._review_payload()),
                                           inner.model, {"model_calls": 1}, 0, "stop")
                    if len(seen) > 1:
                        raise ModelCallError("unknown suffix outcome")
                    return ModelResult('{"status":"admitted","checks":[', inner.model,
                                       {"model_calls": 1}, 0, "length")
            with patch("scisaurus.runtime.capability_foundry.ModelClient", Reviewer):
                with self.assertRaises(ModelCallError):
                    foundry.generate("bounded comparison", client=producer, work_cache=cache)
                outcome = foundry.generate("bounded comparison", client=producer, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(seen, ["first", "first", "peer"])
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 1))
            reviews = cache.entries()[0]["scientific_reviews"].values()
            self.assertTrue(any(value.get("retired_response_continuations") for value in reviews))

    def test_retired_suffix_does_not_block_new_peer_json_continuation(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            foundry.reviewer_client = None
            foundry.model_config["role_models"] = {"review.methods": {"model": "first"}}
            foundry.model_config["role_model_fallbacks"] = {"review.methods": [{"model": "peer"}]}
            producer = StubClient(CapabilityFoundryTests._payload())
            producer.model = "stub"
            cache = CapabilityFoundryTests._cache(self, root)
            review_json = json.dumps(CapabilityFoundryTests._review_payload())
            prefix = review_json[:-2]
            seen = []
            class Reviewer:
                def __init__(inner, **config):
                    inner.model = config["model"]
                def complete(inner, *, system, prompt):
                    seen.append(inner.model)
                    count = seen.count(inner.model)
                    if inner.model == "first" and count == 2:
                        raise ModelCallError("unknown suffix outcome")
                    text = prefix if count == 1 else review_json[len(prefix):]
                    return ModelResult(text, inner.model, {"model_calls": 1}, 0,
                                       "length" if count == 1 else "stop")
            with patch("scisaurus.runtime.capability_foundry.ModelClient", Reviewer):
                with self.assertRaises(ModelCallError):
                    foundry.generate("bounded comparison", client=producer, work_cache=cache)
                outcome = foundry.generate("bounded comparison", client=producer, work_cache=cache)
            self.assertEqual(outcome["status"], "registered")
            self.assertEqual(seen, ["first", "first", "peer", "peer"])
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 1))

    def test_reviewer_response_failure_does_not_request_producer_source_patch(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            foundry.reviewer_client = StubClient({})
            producer = StubClient(CapabilityFoundryTests._payload())
            cache = CapabilityFoundryTests._cache(self, root)
            states = []
            for _ in range(2):
                with self.assertRaises(ModelWorkBlocked) as blocked:
                    foundry.generate("bounded comparison", client=producer, work_cache=cache,
                        on_progress=lambda phase, state: states.append(state))
                self.assertEqual(blocked.exception.failure_class, "model_contract")
                self.assertEqual(blocked.exception.repair_gate, "review_response_format")
            self.assertEqual((producer.calls, foundry.reviewer_client.calls), (1, 2))
            self.assertEqual(states[-1]["repair_owner"], "review.methods")
            self.assertEqual(states[-1]["last_attempt"]["executor_source"], MINI_EXECUTOR)
            self.assertEqual(states[-1]["repair_ledger"][-1]["next_action"], "format_repair_then_rerun")
            self.assertFalse(states[-1]["failed_candidates"])

    def test_validator_protocol_deadline_retains_captured_response(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            cache = CapabilityFoundryTests._cache(self, root)
            producer = StubClient(CapabilityFoundryTests._payload())
            execute = foundry._execute
            def deadline_at_validation(source, payload):
                if source == MINI_VALIDATOR and not json.loads(payload).get('readiness_probe'):
                    raise CapabilityDeadlineError('validator protocol deadline')
                return execute(source, payload)
            with patch.object(foundry, '_execute', side_effect=deadline_at_validation):
                with self.assertRaises(CapabilityDeadlineError):
                    foundry.generate('bounded comparison', client=producer, work_cache=cache)
            outcome = foundry.generate('bounded comparison', client=producer, work_cache=cache)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 1))

    def test_validator_protocol_format_repair_stays_with_its_independent_author(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = CapabilityFoundryTests._foundry(Path(path))
            producer = StubClient(CapabilityFoundryTests._payload())
            class Independent:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    packet = json.loads(prompt)
                    self.assertEqual(packet['readiness_handshake']['stdin'],
                                     {'readiness_probe': True})
                    self.assertEqual(packet['readiness_handshake']['stdout'],
                                     {'status': 'ready'})
                    self.assertEqual(set(packet['runtime_request_shape']),
                                     {'configured_input', 'experiment', 'candidate',
                                      'candidate_sha256', 'primary_outcomes'})
                    self.assertNotIn('experiment_intent', packet['runtime_request_shape'])
                    self.assertEqual(packet['runtime_request_shape']['candidate'],
                                     "the exact JSON object the executor printed")
                    self.assertIn('canonical candidate JSON',
                                  packet['runtime_request_shape']['candidate_sha256'])
                    self.assertEqual(set(packet['validator_output_exact_shapes']['checks'][0]),
                                     {'id', 'outcome', 'evidence'})
                    if inner.calls > 1:
                        self.assertIn('validator_repair', packet)
                    source = (MINI_VALIDATOR.replace('"evidence":', '"message":')
                              if inner.calls == 1 else MINI_VALIDATOR)
                    return ModelResult(json.dumps({'validator_source': source}),
                                       'independent', {'model_calls': 1}, 0, 'stop')
            foundry.validator_client = Independent()
            outcome = foundry.generate('bounded comparison', client=producer)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 2))

    def test_exhausted_validator_contract_preserves_owner_and_executor(self):
        from scisaurus.runtime.failure_recovery import classify_failure
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            producer = StubClient(CapabilityFoundryTests._payload())
            foundry.validator_client = StubClient({'validator_source':
                MINI_VALIDATOR.replace('"evidence":', '"detail":')})
            cache = CapabilityFoundryTests._cache(self, root)
            states = []
            for _ in range(2):
                with self.assertRaises(ModelWorkBlocked) as blocked:
                    foundry.generate('bounded comparison', client=producer,
                        work_cache=cache, on_progress=lambda phase, state: states.append(state))
                error = blocked.exception
                self.assertEqual(error.failure_class, 'model_contract')
                self.assertEqual(error.recovery_mode, 'format_repair_then_rerun')
                self.assertEqual(error.repair_gate, 'independent_validator_contract')
                self.assertEqual(classify_failure('experiment', error), 'model_contract')
                self.assertEqual(error.repair_ledger[-1]['gate'], 'independent_validator_contract')
                self.assertEqual(error.repair_ledger[-1]['next_action'], 'patch_current_validator')
                self.assertEqual((producer.calls, foundry.validator_client.calls), (1, 2))
            state = states[-1]
            self.assertEqual(state['last_attempt']['executor_source'], MINI_EXECUTOR)
            self.assertFalse(state['failed_candidates'])
            self.assertEqual(state['last_failure_class'], 'model_contract')
            self.assertEqual(state['last_failure_gate'], 'independent_validator_contract')
            failed_source = MINI_VALIDATOR.replace('"evidence":', '"detail":')
            self.assertEqual(state['last_attempt']['validator_source'], failed_source)
            self.assertEqual(state['validator_failure']['status'], 'rejected')
            self.assertEqual(state['validator_failure']['provenance']['source_sha256'],
                             hashlib.sha256(failed_source.encode()).hexdigest())
            self.assertEqual(state['validator_failure']['provenance']['role'], 'methods.validator-author')
            self.assertFalse(list((root / 'registry').glob('**/*.json')))

    def test_malformed_current_validator_response_does_not_expose_stale_source(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            producer = StubClient(CapabilityFoundryTests._payload())
            bad_source = MINI_VALIDATOR.replace('"evidence":', '"detail":')
            class Independent:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    value = {'validator_source': bad_source} if inner.calls == 1 else {'invalid': True}
                    return ModelResult(json.dumps(value), 'independent', {'model_calls': 1}, 0, 'stop')
            foundry.validator_client = Independent()
            states = []
            with self.assertRaises(ModelWorkBlocked):
                foundry.generate('bounded comparison', client=producer,
                    on_progress=lambda phase, state: states.append(state))
            self.assertNotIn('validator_source', states[-1]['last_attempt'])
            self.assertNotIn('validator_failure', states[-1])
            self.assertFalse(list((root / 'registry').glob('**/*.json')))

    def test_cached_recalculation_rejection_preserves_scientific_gate(self):
        from scisaurus.runtime.failure_recovery import classify_failure
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            producer = StubClient(CapabilityFoundryTests._payload())
            source = MINI_VALIDATOR.replace('    reported =', '    p95 += 1\n    reported =')
            foundry.validator_client = StubClient({'validator_source': source})
            cache = CapabilityFoundryTests._cache(self, root)
            states = []
            for _ in range(2):
                with self.assertRaises(ModelWorkBlocked) as blocked:
                    foundry.generate('bounded comparison', client=producer, work_cache=cache,
                        on_progress=lambda phase, state: states.append(state))
                self.assertEqual(blocked.exception.failure_class, 'experiment_capability_repair')
                self.assertEqual(blocked.exception.repair_gate, 'independent_recalculation')
                self.assertEqual(classify_failure('experiment', blocked.exception), 'experiment_failure')
            self.assertTrue(states[-1]['failed_candidates'])
            self.assertFalse(list((root / 'registry').glob('**/*.json')))

    def test_repaired_approval_retains_prior_issue_contract_on_cached_reuse(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            cache = CapabilityFoundryTests._cache(self, root)
            payload = CapabilityFoundryTests._payload()
            class Author:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    value = payload if inner.calls == 1 else {'updates': {'executor_source': {
                        'edits': [{'old': 'import json', 'new': 'import json\n'}]}}}
                    return ModelResult(json.dumps(value), 'producer', {'model_calls': 1}, 0, 'stop')
            class Reviewer:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    value = CapabilityFoundryTests._review_payload()
                    if inner.calls == 1:
                        value['status'] = 'rejected'
                        value['checks'][0]['outcome'] = 'failed'
                        value['findings'] = [{'severity': 'blocking', 'finding': 'source formatting',
                            'evidence': 'import layout', 'required_change': 'separate import section'}]
                    value = _add_prior_review_checks(value, prompt)
                    return ModelResult(json.dumps(value), 'reviewer', {'model_calls': 1}, 0, 'stop')
            author = Author()
            reviewer = Reviewer()
            foundry.reviewer_client = reviewer
            first = foundry.generate('bounded comparison', client=author, work_cache=cache)
            second = foundry.generate('bounded comparison', client=author, work_cache=cache)
            self.assertEqual(first, second)
            self.assertTrue(first['admission']['adversarial_review']['prior_blocking_issues'])
            self.assertEqual((author.calls, reviewer.calls), (2, 2))

    def test_contract_change_preserves_complete_producer_and_blind_responses(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            author = StubClient(CapabilityFoundryTests._payload())
            author.model = 'stub'
            cache = CapabilityFoundryTests._cache(self, root)
            def captured_response(phase, state):
                if phase == 'independent_validator_response':
                    raise CapabilityDeadlineError('captured response boundary')
            with self.assertRaises(CapabilityDeadlineError):
                foundry.generate('bounded comparison', client=author, work_cache=cache,
                                 on_progress=captured_response)
            original_key = cache.key
            def new_contract_key(**kwargs):
                return hashlib.sha256((original_key(**kwargs) + ':new-contract').encode()).hexdigest()
            with patch.object(cache, 'key', side_effect=new_contract_key):
                outcome = foundry.generate('bounded comparison', client=author, work_cache=cache)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual((author.calls, foundry.validator_client.calls), (1, 1))

    def test_old_arithmetic_review_cannot_omit_substantive_science_checks(self):
        value = {'status': 'admitted', 'findings': [], 'checks': [
            {'id': name, 'outcome': 'passed', 'evidence': 'recalculated'} for name in
            ['method_implementation', 'estimator_definedness', 'independent_validation', 'claim_support']]}
        with self.assertRaises(ValidationError):
            validate_program_review(value)

    def test_missing_intent_repair_retains_two_field_producer_executor(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = CapabilityFoundryTests._foundry(Path(path))
            payload = CapabilityFoundryTests._payload()
            class Author:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    if inner.calls == 1:
                        return ModelResult(json.dumps({'executor_source': payload['executor_source']}),
                            'producer', {'model_calls': 1}, 0, 'stop')
                    packet = json.loads(prompt)
                    self.assertNotIn('validator_source', packet['output_contract']['updates'])
                    return ModelResult(json.dumps({'updates': {
                        'experiment_intent': payload['experiment_intent']}}),
                        'producer', {'model_calls': 1}, 0, 'stop')
            author = Author()
            outcome = foundry.generate('bounded comparison', client=author)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual(outcome['candidate']['executor_source'], payload['executor_source'])
            self.assertEqual((author.calls, foundry.validator_client.calls), (2, 1))

    def test_blind_author_receives_contract_without_producer_source_or_results(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = CapabilityFoundryTests._foundry(Path(path))
            payload = CapabilityFoundryTests._payload()
            payload.pop('validator_source')
            author = StubClient(payload)
            independent = foundry.validator_client
            original = independent.complete
            def complete(*, system, prompt):
                packet = json.loads(prompt)
                self.assertEqual(packet['assignment'], 'author_independent_validator')
                self.assertNotIn('executor_source', packet)
                self.assertNotIn('validator_source', packet)
                self.assertNotIn('metrics', packet)
                self.assertNotIn('findings', packet)
                self.assertEqual(packet['observation_schema'][0]['replicate'], 'int')
                expected = candidate_prompt("schema", [], {})['executor_output_exact_shapes']
                self.assertEqual(packet['candidate_output_exact_shapes'], expected)
                metrics = packet['candidate_output_exact_shapes']['metrics']
                self.assertIsInstance(metrics, list)
                self.assertEqual(set(metrics[0]), {'id', 'value', 'unit', 'conditions', 'source', 'presentation'})
                self.assertIn("only for reported_value", packet['instructions'])
                self.assertNotIn('recalculated_value', json.dumps(packet['candidate_output_exact_shapes']))
                return original(system=system, prompt=prompt)
            independent.complete = complete
            reviewer = foundry.reviewer_client
            original_review = reviewer.complete
            def review_complete(*, system, prompt):
                packet = json.loads(prompt)
                self.assertIn('analysis', packet)
                self.assertTrue(packet['raw_observation_sample'])
                self.assertIn('model_applicability', {
                    check['id'] for check in packet['output_contract']['checks']})
                return original_review(system=system, prompt=prompt)
            reviewer.complete = review_complete
            cache = CapabilityFoundryTests._cache(self, Path(path))
            first = foundry.generate('bounded comparison', client=author, work_cache=cache)
            second = foundry.generate('bounded comparison', client=author, work_cache=cache)
            self.assertEqual(first, second)
            self.assertEqual((author.calls, independent.calls, foundry.reviewer_client.calls), (1, 1, 1))
            self.assertEqual(first['candidate']['validator_source'], MINI_VALIDATOR)
            proof = first['admission']['validator_authorship']
            self.assertEqual(proof['source_sha256'], hashlib.sha256(MINI_VALIDATOR.encode()).hexdigest())
            self.assertEqual(cache.entries()[0]['usage']['model_calls'], 3)

    def test_429_preserves_validator_repair_allowance_and_producer_response(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            author = StubClient(CapabilityFoundryTests._payload())
            cache = CapabilityFoundryTests._cache(self, root)
            class Independent:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    if inner.calls == 1:
                        raise ModelCallError('cooldown', status_code=429, outcome_known=True, attempts=1, retry_after_seconds=0)
                    if inner.calls == 2:
                        return ModelResult('{', 'independent', {'model_calls': 1}, 0, 'stop')
                    self.assertIn('validator_repair', json.loads(prompt))
                    return ModelResult(json.dumps({'validator_source': MINI_VALIDATOR}), 'independent', {'model_calls': 1}, 0, 'stop')
            independent = Independent()
            foundry.validator_client = independent
            with self.assertRaises(ModelCallError):
                foundry.generate('bounded comparison', client=author, work_cache=cache)
            outcome = foundry.generate('bounded comparison', client=author, work_cache=cache)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual((author.calls, independent.calls), (1, 3))
            self.assertEqual(cache.entries()[0]['usage']['model_calls'], 5)

    def test_unknown_validator_dispatch_never_reauthors_the_producer(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            author = StubClient(CapabilityFoundryTests._payload())
            cache = CapabilityFoundryTests._cache(self, root)
            class Unknown:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    raise ModelCallError('unknown validator outcome', outcome_known=False, attempts=1)
            independent = Unknown()
            foundry.validator_client = independent
            with self.assertRaises(ModelCallError):
                foundry.generate('bounded comparison', client=author, work_cache=cache)
            with self.assertRaisesRegex(ModelWorkBlocked, 'unresolved dispatched request'):
                foundry.generate('bounded comparison', client=author, work_cache=cache)
            self.assertEqual((author.calls, independent.calls), (1, 1))
            self.assertEqual(cache.entries()[0]['requests'][-1]['status'], 'result_unknown')

    def test_two_field_producer_response_repairs_its_executor_without_a_validator_draft(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = CapabilityFoundryTests._foundry(Path(path))
            payload = CapabilityFoundryTests._payload()
            payload.pop('validator_source')
            payload['executor_source'] = MINI_EXECUTOR.replace('"__main__"', '"main__"')
            class Author:
                calls = 0
                def complete(inner, *, system, prompt):
                    inner.calls += 1
                    if inner.calls == 1:
                        return ModelResult(json.dumps(payload), 'producer', {'model_calls': 1}, 0, 'stop')
                    packet = json.loads(prompt)
                    self.assertNotIn('validator_source', packet['output_contract']['updates'])
                    return ModelResult(json.dumps({'updates': {'executor_source': {'edits': [{
                        'old': 'if __name__ == "main__":', 'new': 'if __name__ == "__main__":'}]}}}),
                        'producer', {'model_calls': 1}, 0, 'stop')
            author = Author()
            outcome = foundry.generate('bounded comparison', client=author)
            self.assertEqual(outcome['status'], 'registered')
            self.assertEqual((author.calls, foundry.validator_client.calls), (2, 1))


class RecalculationOwnershipTests(unittest.TestCase):
    def test_valid_rejection_hands_ownership_to_methods_before_producer_retry(self):
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            producer = StubClient(CapabilityFoundryTests._payload())
            bad_validator = MINI_VALIDATOR.replace('    reported =', '    p95 += 1\n    reported =')
            foundry.validator_client = StubClient({'validator_source': bad_validator})
            cache = CapabilityFoundryTests._cache(self, root)
            states = []
            for _ in range(2):
                with self.assertRaises(ModelWorkBlocked) as raised:
                    foundry.generate('bounded comparison', client=producer, work_cache=cache,
                                     on_progress=lambda phase, state: states.append(state))
                self.assertEqual(raised.exception.failure_class, 'experiment_capability_repair')
                self.assertEqual(raised.exception.repair_gate, 'independent_recalculation')
            self.assertEqual(producer.calls, 1)
            self.assertEqual(foundry.validator_client.calls, 1)
            self.assertEqual(foundry.reviewer_client.calls, 0)
            self.assertEqual(states[-1]['repair_owner'], 'methods_adjudication')
            self.assertEqual(states[-1]['repair_ledger'][-1]['next_action'],
                             'methods_adjudication_before_source_repair')
            self.assertEqual(states[-1]['last_attempt']['executor_source'], MINI_EXECUTOR)
            self.assertEqual(states[-1]['last_attempt']['validator_source'], bad_validator)
            self.assertFalse(list((root / 'registry').glob('**/*.json')))

    @staticmethod
    def _methods_review():
        plan = {'disposition': 'repair', 'root_cause': {'statement': 'Clarify the frozen estimand.',
                'evidence': ['The independent source uses a different convention.']},
                'required_changes': [{'target': 'validator', 'instruction': 'Use the declared convention.',
                                     'scientific_basis': 'Frozen primary outcome definition.', 'source_refs': []}]}
        brief = {'capability_repair': {'repair_plan': plan}}
        provenance = {'kind': 'independent_repair', 'origin': 'composer_model_panel',
                      'repair_plan_sha256': hashlib.sha256(canonical_bytes(plan)).hexdigest(),
                      'panel_input_sha256': 'a' * 64,
                      'panel_verdict_artifact_ref': 'artifact:methods/verifier@1'}
        return brief, provenance

    def test_validator_author_receives_bound_methods_plan_and_raw_conventions(self):
        brief, provenance = self._methods_review()
        brief['capability_repair']['repair_plan']['root_cause']['evidence'].append(MINI_EXECUTOR)
        brief['capability_repair']['repair_plan']['required_changes'][0]['instruction'] += ' reported_metric=987654.321'
        provenance['repair_plan_sha256'] = hashlib.sha256(canonical_bytes(brief['capability_repair']['repair_plan'])).hexdigest()
        with tempfile.TemporaryDirectory() as path:
            root = Path(path)
            foundry = CapabilityFoundryTests._foundry(root)
            validator = foundry.validator_client
            complete = validator.complete
            prompts = []
            def inspect(**kwargs):
                prompts.append(json.loads(kwargs['prompt']))
                return complete(**kwargs)
            validator.complete = inspect
            outcome = foundry.generate(json.dumps(brief), client=StubClient(CapabilityFoundryTests._payload()),
                                       repair_provenance=provenance)
            self.assertEqual(outcome['status'], 'registered')
            assignment = prompts[0]
            self.assertNotIn('repair_plan', assignment['methods_repair_review'])
            self.assertNotIn('reported_metric=987654.321', json.dumps(assignment))
            self.assertEqual(assignment['methods_repair_review']['repair_plan_sha256'], provenance['repair_plan_sha256'])
            self.assertTrue(assignment['raw_observation_sample'])
            self.assertNotIn('executor_source', assignment)
            self.assertNotIn(MINI_EXECUTOR, json.dumps(assignment))
            self.assertNotIn('metrics', assignment)
            changed = deepcopy(brief)
            changed['capability_repair']['repair_plan']['required_changes'][0]['instruction'] = 'Clarify indexing.'
            from scisaurus.runtime.capability_foundry import validator_methods_repair
            updated_provenance = {**provenance, 'repair_plan_sha256': hashlib.sha256(canonical_bytes(changed['capability_repair']['repair_plan'])).hexdigest()}
            revised = deepcopy(assignment)
            revised['methods_repair_review'] = validator_methods_repair(changed, updated_provenance)
            self.assertNotEqual(hashlib.sha256(canonical_bytes(assignment)).hexdigest(),
                                hashlib.sha256(canonical_bytes(revised)).hexdigest())

    def test_unadmitted_or_tampered_methods_plan_is_rejected_before_dispatch(self):
        brief, provenance = self._methods_review()
        for mutation in ['digest', 'input', 'origin']:
            damaged = deepcopy(provenance)
            damaged[{'digest': 'repair_plan_sha256', 'input': 'panel_input_sha256', 'origin': 'origin'}[mutation]] = None
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as path:
                producer = StubClient(CapabilityFoundryTests._payload())
                foundry = CapabilityFoundryTests._foundry(Path(path))
                with self.assertRaisesRegex(ValidationError, 'fingerprint-bound Methods plan'):
                    foundry.generate(brief, client=producer, repair_provenance=damaged)
                self.assertEqual(producer.calls, 0)
                self.assertEqual(foundry.validator_client.calls, 0)

    def test_validator_budget_boundary_preserves_current_executor_without_old_verdict(self):
        with tempfile.TemporaryDirectory() as path:
            foundry = CapabilityFoundryTests._foundry(Path(path))
            states = []
            with self.assertRaises(CapabilityModelBudgetExceeded):
                foundry.generate('bounded comparison', client=StubClient(CapabilityFoundryTests._payload()),
                                 model_call_budget=1,
                                 on_progress=lambda phase, state: states.append((phase, state)))
            latest = states[-1][1]
            self.assertEqual(latest['last_attempt']['executor_source'], MINI_EXECUTOR)
            self.assertEqual(latest['last_attempt']['experiment_intent'], INTENT)
            self.assertEqual(latest['last_attempt']['test_input'], {'probe': True})
            self.assertNotIn('validator_source', latest['last_attempt'])
            self.assertEqual(foundry.validator_client.calls, 0)
            self.assertIn('executor_observations_recorded', [phase for phase, _ in states])


    def test_legacy_admitted_plan_retains_missing_verdict_reference_without_invention(self):
        from scisaurus.runtime.capability_foundry import validator_methods_repair
        from scisaurus.runtime.composer import ComposerRunner
        brief, _ = self._methods_review()
        context = {**brief['capability_repair'], 'input_sha256': 'a' * 64, 'ledger': None}
        provenance = ComposerRunner._capability_repair_provenance(context)
        binding = validator_methods_repair(brief, provenance)
        self.assertIsNone(binding['panel_verdict_artifact_ref'])
        self.assertEqual(binding['repair_plan_sha256'], provenance['repair_plan_sha256'])
