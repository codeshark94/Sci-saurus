"""Admission gates for model-authored experiment programs (foundry P3.3).

Gate order (each independent, and each must pass before the next):

1. static scan          -- ``program_admission.validate_program_candidate`` (P3.1)
2. deterministic replay -- the same test vector must produce byte-identical
                           output on repeated sandboxed executions
3. test-vector digest   -- the replay output must match the declared SHA-256
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
from scisaurus.runtime.experiment import (
    bind_deterministic_validation, validate_deterministic_validation,
    validate_program_output,
)
from scisaurus.runtime.program_admission import validate_program_candidate

ADMISSION_SCHEMA = "method-program-admission-1"


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
        raise ValidationError(f"{name} did not return a JSON document") from exc


def admit_program_candidate(candidate, *, execute, validate, readiness=None, review=None,
                            replay_runs=3):
    """Run every admission gate and return an immutable admission record."""
    validate_program_candidate(candidate)
    if type(replay_runs) is not int or not 3 <= replay_runs <= 8:
        raise ValidationError("admission replay_runs must be an integer between three and eight")
    payload = canonical_bytes(candidate["test_vector"]["input"])
    expected = candidate["test_vector"]["expected_output_sha256"]
    replays = []
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
        replays.append(canonical_bytes(document))
    if any(replay != replays[0] for replay in replays[1:]):
        raise ValidationError("deterministic replay produced non-identical output")
    digest = hashlib.sha256(replays[0]).hexdigest()
    if digest != expected:
        raise ValidationError("replay output does not match the declared test-vector digest")
    document = json.loads(replays[0])
    digests = [digest]
    if (document.get("study_id") != candidate["study_id"]
            or document.get("revision") != candidate["revision"]):
        raise ValidationError("program output does not match the candidate study identity")
    document = validate_program_output(document, candidate["experiment_intent"])
    verdict_result = validate(canonical_bytes({
        "configured_input": {}, "candidate": document, "candidate_sha256": digests[0],
        "primary_outcomes": candidate["experiment_intent"]["primary_outcomes"],
    }))
    if getattr(verdict_result, "mode", None) != "sandbox-exec":
        raise ValidationError(
            "program validator requires the deny-by-default sandbox-exec boundary")
    verdict = _parse_json_output(verdict_result, "program validator")
    verdict = validate_deterministic_validation(
        verdict, candidate["experiment_intent"], digests[0])
    if verdict.get("decision") != "accepted":
        raise ValidationError(
            "independent recalculation did not accept the candidate: "
            + json.dumps(verdict, ensure_ascii=False, sort_keys=True)[:2400])
    checks = verdict.get("checks") or []
    if not checks or any(item.get("outcome") != "passed" for item in checks if isinstance(item, dict)):
        raise ValidationError("independent recalculation reported a failed check")
    recalculations = verdict.get("metric_recalculations") or []
    if not recalculations:
        raise ValidationError("independent recalculation reported no metric comparison")
    bind_deterministic_validation(
        verdict, document, candidate["experiment_intent"])
    readiness_record = None
    if readiness is not None:
        readiness_result = readiness()
        if getattr(readiness_result, "mode", None) != "sandbox-exec":
            raise ValidationError(
                "validator readiness requires the deny-by-default sandbox-exec boundary")
        readiness_record = _parse_json_output(readiness_result, "program validator readiness")
        if readiness_record != {"status": "ready"}:
            raise ValidationError(
                "program validator readiness must return exactly {'status': 'ready'}")
    review_record = None
    if review is not None:
        review_record = review(candidate, document, verdict)
        if not isinstance(review_record, dict) or review_record.get("status") not in {"admitted", "rejected"}:
            raise ValidationError("adversarial review must return a status of admitted or rejected")
        blocking = [item for item in review_record.get("findings", [])
                    if isinstance(item, dict) and item.get("severity") == "blocking"]
        if review_record["status"] != "admitted" or blocking:
            raise ValidationError("adversarial review rejected the candidate program")
    return {
        "schema_version": ADMISSION_SCHEMA,
        "study_id": candidate["study_id"],
        "revision": candidate["revision"],
        "candidate_record_sha256": hashlib.sha256(canonical_bytes(candidate)).hexdigest(),
        "executor_source_sha256": hashlib.sha256(candidate["executor_source"].encode()).hexdigest(),
        "validator_source_sha256": hashlib.sha256(candidate["validator_source"].encode()).hexdigest(),
        "runtime": candidate["runtime"],
        "replay_runs": replay_runs,
        "output_sha256": digests[0],
        "independent_recalculation": {"decision": verdict.get("decision"),
                                       "checks": len(checks),
                                       "metrics": len(recalculations)},
        "validator_readiness": readiness_record,
        "adversarial_review": review_record,
        "gates": ["static_scan", "deterministic_replay", "test_vector_digest",
                  "independent_recalculation"] + (["validator_readiness"] if readiness_record else [])
                  + (["adversarial_review"] if review_record else []),
    }
