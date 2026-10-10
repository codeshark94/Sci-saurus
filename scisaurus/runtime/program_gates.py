"""Admission gates for model-authored experiment programs (foundry P3.3).

Gate order (each independent, and each must pass before the next):

1. static scan          -- ``program_admission.validate_program_candidate`` (P3.1)
2. deterministic replay -- the same test vector must produce byte-identical
                           output on repeated sandboxed executions
3. test-vector digest   -- the admitted output projection must match the declared SHA-256
4. independent recalc   -- a *separately authored* validator must recompute the
                           declared outcomes from the recorded observations and
                           accept the exact candidate bytes
5. adversarial review   -- an optional review callback must return no blocking
                           finding

The gates never mutate the candidate and never treat a failed gate as success.
"""
from __future__ import annotations

import hashlib
import json

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes
from scisaurus.runtime.capability_registry import (
    experiment_validation_payload, program_validator_configured_input,
)
from scisaurus.runtime.experiment import (
    bind_deterministic_validation, validate_deterministic_validation,
    validate_program_output,
)
from scisaurus.runtime.program_admission import validate_program_candidate

ADMISSION_SCHEMA = "method-program-admission-1"


class ProgramGateRejected(ValidationError):
    """Keep failed evidence independent of bounded human-readable errors."""

    def __init__(self, message, record, *, gate, details=None):
        self.feedback = {
            "gate": gate,
            "decision": record.get("decision", record.get("status")),
            "failed_checks": [item for item in record.get("checks", [])
                                  if isinstance(item, dict) and item.get("outcome") != "passed"],
            "findings": record.get("findings", []),
            "metric_mismatches": [item for item in record.get("metric_recalculations", [])
                                  if isinstance(item, dict) and item.get("matches") is not True],
        }
        if isinstance(details, dict):
            self.feedback.update(details)
        super().__init__(message + ": " + json.dumps(self.feedback, ensure_ascii=False)[:2400])


class ProgramReviewRejected(ProgramGateRejected):
    """Preserve a methods verdict and its actionable repair evidence."""

    def __init__(self, review):
        self.review = review
        super().__init__("adversarial review rejected the candidate program", review,
                         gate="adversarial_review")


def _parse_json_output(result, name):
    if result.timed_out:
        raise ValidationError(f"{name} exceeded its sandbox deadline")
    if result.truncated:
        raise ValidationError(f"{name} output exceeded the sandbox byte limit")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace")[-1200:] if result.stderr else ""
        raise ValidationError(f"{name} exited with status {result.returncode}: {detail}")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"{name} did not return a JSON document; "
                              f"stdout={result.stdout[:500]!r}, stderr={result.stderr[-500:]!r}; "
                              "verify the entry point and stdin handling") from exc


def _first_replay_difference(left, right, path=""):
    """Locate an exact JSON difference without changing numeric precision."""
    def entry(value):
        if isinstance(value, (dict, list)):
            encoded = canonical_bytes(value)
            return {"present": True, "type": type(value).__name__,
                    "sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}
        return {"present": True, "type": type(value).__name__, "value": value}

    if type(left) is type(right) and isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in left or key not in right:
                return {"path": child,
                        "baseline": entry(left[key]) if key in left else {"present": False},
                        "replay": entry(right[key]) if key in right else {"present": False}}
            difference = _first_replay_difference(left[key], right[key], child)
            if difference is not None:
                return difference
        return None
    if type(left) is type(right) and isinstance(left, list):
        for index in range(max(len(left), len(right))):
            child = path + "/" + str(index)
            if index >= len(left) or index >= len(right):
                return {"path": child,
                        "baseline": entry(left[index]) if index < len(left) else {"present": False},
                        "replay": entry(right[index]) if index < len(right) else {"present": False}}
            difference = _first_replay_difference(left[index], right[index], child)
            if difference is not None:
                return difference
        return None
    if type(left) is not type(right) or canonical_bytes(left) != canonical_bytes(right):
        return {"path": path, "baseline": entry(left), "replay": entry(right)}
    return None


def _replay_failure_details(raw_replays, payload, candidate):
    baseline = json.loads(raw_replays[0])
    comparisons = []
    for index, raw in enumerate(raw_replays[1:], 2):
        if raw == raw_replays[0]:
            continue
        document = json.loads(raw)
        if isinstance(baseline, dict) and isinstance(document, dict):
            differences = []
            for key in sorted(set(baseline) | set(document)):
                difference = _first_replay_difference(
                    {key: baseline[key]} if key in baseline else {},
                    {key: document[key]} if key in document else {})
                if difference is not None:
                    differences.append(difference)
        else:
            differences = [_first_replay_difference(baseline, document)]
        comparisons.append({"baseline_run": 1, "replay_run": index,
                            "first_difference_by_output_field": differences})
    return {"executor_source_sha256": hashlib.sha256(candidate["executor_source"].encode()).hexdigest(),
            "stdin_sha256": hashlib.sha256(payload).hexdigest(),
            "canonical_replay_outputs": [
                {"run": index, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
                for index, raw in enumerate(raw_replays, 1)],
            "replay_comparisons": comparisons}


def validator_readiness_contract():
    return {
        "stdin": {"readiness_probe": True},
        "stdout": {"status": "ready"},
        "transport": "The probe is the entire stdin JSON object, not a configured_input field.",
    }


def validate_validator_readiness(result):
    if getattr(result, "mode", None) != "sandbox-exec":
        raise ValidationError("validator readiness requires the deny-by-default sandbox-exec boundary")
    record = _parse_json_output(result, "program validator readiness")
    contract = validator_readiness_contract()
    if record != contract["stdout"]:
        raise ValidationError(
            "program validator readiness must return exactly {'status': 'ready'}; "
            f"stdin is the entire JSON object {contract['stdin']!r}, not configured_input; "
            f"observed stdout: {json.dumps(record, ensure_ascii=False)[:800]}")
    return record


def admit_program_candidate(candidate, *, execute, validate, readiness=None, review=None,
                            replay_runs=3, laboratory_execution=None):
    """Run every admission gate and return an immutable admission record."""
    validate_program_candidate(candidate, laboratory_execution=laboratory_execution)
    if type(replay_runs) is not int or not 3 <= replay_runs <= 8:
        raise ValidationError("admission replay_runs must be an integer between three and eight")
    payload = canonical_bytes(candidate["test_vector"]["input"])
    expected = candidate["test_vector"]["expected_output_sha256"]
    raw_replays = []
    configured_input = program_validator_configured_input(candidate)
    for index in range(replay_runs):
        result = execute(payload)
        if getattr(result, "mode", None) != "sandbox-exec":
            raise ValidationError(
                "deterministic replay requires the deny-by-default sandbox-exec boundary")
        if result.timed_out or result.truncated or result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace")[-1200:] if result.stderr else ""
            raise ValidationError(
                f"deterministic replay {index + 1} failed (status={result.returncode}, "
                f"timeout={result.timed_out}, truncated={result.truncated}): {detail}")
        try:
            document = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"deterministic replay {index + 1} did not return JSON") from exc
        raw_replays.append(canonical_bytes(document))
    if any(replay != raw_replays[0] for replay in raw_replays[1:]):
        raise ProgramGateRejected(
            "deterministic replay produced non-identical output",
            {"decision": "rejected", "checks": [
                {"id": "replay_identity", "outcome": "failed",
                 "evidence": "Canonical JSON differs between executions of the same source and input."}]},
            gate="deterministic_replay",
            details={"gate_evidence": _replay_failure_details(raw_replays, payload, candidate)})
    document = validate_program_output(
        json.loads(raw_replays[0]), candidate["experiment_intent"],
        configured_input.get("work_orders", []), configured_input=configured_input)
    admitted_output = canonical_bytes(document)
    digest = hashlib.sha256(admitted_output).hexdigest()
    if digest != expected:
        raise ValidationError("replay output does not match the declared test-vector digest")
    digests = [digest]
    if (document.get("study_id") != candidate["study_id"]
            or document.get("revision") != candidate["revision"]):
        raise ValidationError("program output does not match the candidate study identity")
    verdict_result = validate(canonical_bytes(experiment_validation_payload(
        candidate["experiment_intent"], configured_input, document, digests[0])))
    if getattr(verdict_result, "mode", None) != "sandbox-exec":
        raise ValidationError(
            "program validator requires the deny-by-default sandbox-exec boundary")
    verdict = _parse_json_output(verdict_result, "program validator")
    try:
        verdict = validate_deterministic_validation(
            verdict, candidate["experiment_intent"], digests[0])
    except ValidationError as exc:
        if "deterministic validation decision contradicts its checks" not in str(exc):
            raise
        expected_decision = (
            "accepted"
            if all(item.get("outcome") == "passed" for item in verdict.get("checks", []))
            and all(item.get("matches") is True
                    for item in verdict.get("metric_recalculations", []))
            else "rejected"
        )
        raise ProgramGateRejected(
            "independent recalculation decision contradicts its evidence",
            verdict,
            gate="independent_recalculation",
            details={
                "expected_decision": expected_decision,
                "validation_error": str(exc),
            },
        ) from exc
    if verdict.get("decision") != "accepted":
        raise ProgramGateRejected("independent recalculation did not accept the candidate", verdict,
                                  gate="independent_recalculation")
    checks = verdict.get("checks") or []
    if not checks or any(item.get("outcome") != "passed" for item in checks if isinstance(item, dict)):
        raise ProgramGateRejected("independent recalculation reported a failed check", verdict,
                                  gate="independent_recalculation")
    recalculations = verdict.get("metric_recalculations") or []
    if not recalculations:
        raise ValidationError("independent recalculation reported no metric comparison")
    bind_deterministic_validation(
        verdict, document, candidate["experiment_intent"])
    from scisaurus.runtime.measurement_contract import verified_decisions
    decision_assessments = verified_decisions(candidate["experiment_intent"], verdict)
    readiness_record = None
    if readiness is not None:
        readiness_record = validate_validator_readiness(readiness())
    review_record = None
    if review is not None:
        review_record = review(candidate, document, verdict)
        if not isinstance(review_record, dict) or review_record.get("status") not in {"admitted", "rejected"}:
            raise ValidationError("adversarial review must return a status of admitted or rejected")
        blocking = [item for item in review_record.get("findings", [])
                    if isinstance(item, dict) and item.get("severity") == "blocking"]
        if review_record["status"] != "admitted" or blocking:
            raise ProgramReviewRejected(review_record)
    return {
        "schema_version": ADMISSION_SCHEMA,
        "study_id": candidate["study_id"],
        "revision": candidate["revision"],
        "candidate_record_sha256": hashlib.sha256(canonical_bytes(candidate)).hexdigest(),
        "executor_source_sha256": hashlib.sha256(candidate["executor_source"].encode()).hexdigest(),
        "validator_source_sha256": hashlib.sha256(candidate["validator_source"].encode()).hexdigest(),
        "runtime": candidate["runtime"],
        **({"laboratory_execution_sha256": hashlib.sha256(canonical_bytes(laboratory_execution)).hexdigest()}
           if laboratory_execution is not None else {}),
        "replay_runs": replay_runs,
        "output_sha256": digests[0],
        "independent_recalculation": {"decision": verdict.get("decision"),
                                       "checks": len(checks),
                                       "metrics": len(recalculations)},
        "decision_assessments": decision_assessments,
        "validator_readiness": readiness_record,
        "adversarial_review": review_record,
        "gates": ["static_scan", "deterministic_replay", "test_vector_digest",
                  "independent_recalculation"] + (["validator_readiness"] if readiness_record else [])
                  + (["adversarial_review"] if review_record else []),
    }
