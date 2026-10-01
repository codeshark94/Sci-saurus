"""Execute a frozen study twice, recalculate it independently, and review its claims."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import threading
import time
import uuid

from scisaurus.core.errors import ModelContractError, ValidationError
from scisaurus.core.events import ControlStore
from scisaurus.core.schema import canonical_bytes, sha256_hex
from scisaurus.core.store import ArtifactStore
from scisaurus.core.surveys import SurveyGate
from scisaurus.runtime.execution import ExecutionRuntime, _invoke_worker
from scisaurus.runtime.evidence import scientific_input_recovery_contract
from scisaurus.runtime.capability_registry import (
    experiment_program_payload, experiment_validation_payload,
)
from scisaurus.runtime.config import configured_worker_slots
from scisaurus.runtime.experiment_config import (
    ASSET_MEDIA_TYPES, validate_experiment_config, validate_work_orders,
)
from scisaurus.runtime.models import ModelCallError, ModelResult
from scisaurus.runtime.operations import OperationsCell
from scisaurus.runtime.results import validate_results_package
from scisaurus.runtime.research_quality import (
    build_research_design,
    evaluate_result_package_quality,
    validate_analysis,
)
from scisaurus.runtime.scores import exact, identifier, output_path
from scisaurus.runtime.time_policy import TimePolicy


PROGRAM_OUTPUT_FIELDS = (
    "schema_version", "study_id", "revision", "procedures", "observations",
    "metrics", "findings", "limitations", "assets",
)
PROGRAM_OUTPUT_OPTIONAL_FIELDS = ("analysis",)
PROGRAM_OUTPUT_LEGACY_FIELDS = ("work_order_assessments",)


class ExperimentProgramOutputContractError(ModelContractError):
    """A generated executor emitted valid JSON with an invalid output envelope."""

    failure_class = "model_contract"
    recovery_mode = "format_repair_then_rerun"
    repair_gate = "program_output_contract"

    def __init__(self, *, required_fields, observed_fields, missing_fields,
                 unexpected_fields, observed_type=None):
        self.required_fields = tuple(required_fields)
        self.observed_fields = tuple(observed_fields)
        self.missing_fields = tuple(missing_fields)
        self.unexpected_fields = tuple(unexpected_fields)
        self.observed_type = observed_type
        details = [
            f"required={list(self.required_fields)}",
            f"observed={list(self.observed_fields)}",
            f"missing={list(self.missing_fields)}",
            f"unexpected={list(self.unexpected_fields)}",
        ]
        if observed_type is not None:
            details.append(f"observed_type={observed_type}")
        super().__init__(
            "experiment program output requires the declared top-level contract "
            "and permits only documented optional fields (" + "; ".join(details) + ")")


REVIEW_CHECKS = {"method_alignment", "calculation_trace", "inference_scope", "limitation_coverage"}
REVIEW_RESULT_ROOTS = frozenset({"procedures", "observations", "metrics", "findings", "assets", "analysis"})
REVIEW_DECISIONS = {"accepted", "accepted_with_limitations", "rejected"}
_NONFINITE_NUMERIC_TOKEN = re.compile(
    r"(?<![\w.])(?P<sign>[+-]?)(?P<value>nan|inf(?:inity)?)(?!\w)",
    re.IGNORECASE)
_NONFINITE_VALUE_CONTEXT = re.compile(
    r"(?:[=<>]\s*|\b(?:value|result|measurement|estimate|outcome|metric)"
    r"(?:\s*[:=]|\s+(?:is|was|equals?|reported\s+as|returned\s+as))\s*)$",
    re.IGNORECASE)
_NONFINITE_UNDEFINED_SUFFIX = re.compile(
    r"\s*[\(\[]\s*(?:undefined|censored|unavailable|not\s+(?:finite|defined))\b",
    re.IGNORECASE)
_NONFINITE_NEGATION = re.compile(
    r"\b(?:not|never|no|without|cannot|can['’]t|didn['’]?t|doesn['’]?t|"
    r"isn['’]?t|wasn['’]?t|failed\s+to)\b", re.IGNORECASE)
_NONFINITE_NEGATED_PREFIX = re.compile(
    r"\b(?:not|never|no|without|cannot|can['’]t|didn['’]?t|doesn['’]?t|"
    r"isn['’]?t|wasn['’]?t|aren['’]?t|weren['’]?t)\s*$", re.IGNORECASE)


def _contains_nonfinite_numeric_marker(text, *, metric_id, unit=None):
    """Identify non-finite placeholders without matching prose about infinity."""
    metric_words = [word for word in re.split(r"[_-]+", metric_id) if word]
    metric_label = r"[\s_-]+".join(re.escape(word) for word in metric_words)
    metric_context = re.compile(
        r"\b" + metric_label + r"(?P<qualifiers>(?:\s+[\w'’-]+){0,8})\s+"
        r"(?:is|was|equals?|reported(?:\s+as)?|reports?|returned(?:\s+as)?|returns?|"
        r"produced|produces|yielded|yields|generated|generates|emitted|emits)\s*$",
        re.IGNORECASE)
    metric_header_context = re.compile(
        r"\b" + metric_label + r"(?:\s+[\w-]+){0,3}\s*:\s*$", re.IGNORECASE)
    metric_change_context = re.compile(
        r"\b" + metric_label + r"(?:\s+[\w-]+){0,3}\s+"
        r"(?:shift|change|increase|decrease|move)\w*\s+by\s*$", re.IGNORECASE)
    for match in _NONFINITE_NUMERIC_TOKEN.finditer(text):
        before, after = text[:match.start()], text[match.end():]
        if _NONFINITE_NEGATED_PREFIX.search(before):
            continue
        if (not before.strip()
                and not after.strip(" \t\r\n.,;:!?)]}")):
            return True
        value_context = _NONFINITE_VALUE_CONTEXT.search(before)
        metric_value_context = metric_context.search(before)
        if metric_value_context:
            recent_qualifiers = " ".join(
                metric_value_context.group("qualifiers").split()[-3:])
            if _NONFINITE_NEGATION.search(recent_qualifiers):
                continue
        metric_header = metric_header_context.search(before)
        metric_change = metric_change_context.search(before)
        unit_match = (re.match(r"\s*" + re.escape(unit) + r"(?!\w)", after, re.IGNORECASE)
                      if unit else None)
        unit_follows = unit_match is not None
        unit_tail = after[unit_match.end():] if unit_match else ""
        unit_ends_value = unit_follows and not unit_tail.strip(
            " \t\r\n.,;:!?)]}")
        if (_NONFINITE_UNDEFINED_SUFFIX.match(after)
                and (not before.strip() or value_context or metric_value_context or metric_header)):
            return True
        if value_context or metric_value_context or metric_change:
            return True
        if unit_ends_value or (metric_header and not after.strip(" \t\r\n.,;:!?)]}")):
            return True
    return False


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value


def _finite_scalar(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValidationError(f"{name} must be an exact JSON scalar")
    canonical_bytes(value)
    return value


def _json_pointer_value(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValidationError("work-order evidence paths must be absolute JSON Pointers")
    value = document
    for raw_token in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw_token):
            raise ValidationError(
                f"work-order evidence path has invalid JSON Pointer escaping: {pointer!r}")
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdigit() and (token == "0" or not token.startswith("0")):
            index = int(token)
            if index >= len(value):
                raise ValidationError(
                    f"work-order evidence path does not resolve in the result: {pointer!r}")
            value = value[index]
        elif isinstance(value, list):
            # Reviewer evidence commonly names a stable finding, metric, or check ID.
            # Accept that selector alongside ordinary JSON Pointer array indices.
            matches = [item for item in value if isinstance(item, dict)
                       and any((key == "id" or key.endswith("_id")) and candidate == token
                               for key, candidate in item.items())]
            if len(matches) == 1:
                value = matches[0]
            elif len(matches) > 1:
                raise ValidationError(
                    f"work-order evidence path is ambiguous for stable ID {token!r}: {pointer!r}")
            else:
                raise ValidationError(
                    f"work-order evidence path does not resolve in the result: {pointer!r}")
        else:
            raise ValidationError(
                f"work-order evidence path does not resolve in the result: {pointer!r}")
    return value


def _validate_censored_event_observations(observations):
    """Do not admit a censoring boundary as though an event was observed."""
    censored_terms = (
        "censor", "non_cross", "not_observed", "no_cross", "undefined",
    )
    bound_terms = ("bound", "limit", "lower", "upper", "threshold", "window")
    for row_index, observation in enumerate(observations, start=1):
        for status_key, status_value in observation.items():
            if status_key.endswith("_status") and isinstance(status_value, str):
                status = status_value.casefold().replace("-", "_").replace(" ", "_")
                if not any(term in status for term in censored_terms):
                    continue
                event_prefix = status_key[:-len("_status")]
                status_text = status_value
            elif status_key.endswith("_censored") and status_value is True:
                event_prefix = status_key[:-len("_censored")]
                status_text = "censored"
            else:
                continue

            point_values = [
                key for key, value in observation.items()
                if (key == event_prefix or key.startswith(event_prefix + "_"))
                and not any(term in key.casefold() for term in bound_terms)
                and type(value) in (int, float)
                and math.isfinite(value)
            ]
            if point_values:
                raise ValidationError(
                    f"observation row {row_index} marks {event_prefix!r} as "
                    f"{status_text!r} but supplies numeric event value(s) "
                    f"{point_values[:4]}; retain the censoring bound separately "
                    "and do not use it as an observed event")


def validate_program_output(value, experiment, work_orders=None):
    work_orders = validate_work_orders(work_orders)
    output_fields = set(PROGRAM_OUTPUT_FIELDS)
    allowed_output_fields = output_fields | set(PROGRAM_OUTPUT_OPTIONAL_FIELDS)
    if work_orders:
        # Backward compatibility only: a program's self-assessment is ignored;
        # independent reviewers determine whether the work order is evidenced.
        allowed_output_fields.update(PROGRAM_OUTPUT_LEGACY_FIELDS)
    if not isinstance(value, dict):
        raise ExperimentProgramOutputContractError(
            required_fields=sorted(output_fields), observed_fields=[],
            missing_fields=sorted(output_fields), unexpected_fields=[],
            observed_type=type(value).__name__,
        )
    observed_fields = {str(key) for key in value}
    missing_fields = output_fields - set(value)
    unexpected_fields = set(value) - allowed_output_fields
    if missing_fields or unexpected_fields:
        raise ExperimentProgramOutputContractError(
            required_fields=sorted(output_fields),
            observed_fields=sorted(observed_fields),
            missing_fields=sorted(str(key) for key in missing_fields),
            unexpected_fields=sorted(str(key) for key in unexpected_fields),
        )
    if value["schema_version"] != "experiment-program-output-1":
        raise ValidationError("unsupported experiment program output schema")
    if value["study_id"] != experiment["id"] or value["revision"] != experiment["revision"]:
        raise ValidationError("experiment program output does not match the frozen study identity")
    observations = value["observations"]
    if not isinstance(observations, list):
        raise ValidationError("experiment observations must be a list of rows")
    observation_count = len(observations)
    if observation_count < experiment["run_count"]:
        raise ValidationError(
            f"experiment emitted {observation_count} observation rows, fewer than "
            f"configured minimum run_count={experiment['run_count']} rows")
    if observation_count > experiment["max_observations"]:
        raise ValidationError(
            f"experiment emitted {observation_count} observation rows but "
            f"max_observations={experiment['max_observations']}; max_observations is "
            "the total row ceiling across all conditions and replicates, so it must cover "
            "the complete planned row count, or the design must be reduced without "
            "changing its scientific question")
    for observation in observations:
        if not isinstance(observation, dict):
            raise ValidationError("every experiment observation must be an object")
        canonical_bytes(observation)
    _validate_censored_event_observations(observations)

    assets = value["assets"]
    if not isinstance(assets, list):
        raise ValidationError("experiment assets must be an explicit list")
    roles = {}
    for asset in assets:
        exact(asset, {"id", "path", "sha256", "role", "media_type", "caption"},
              "experiment asset")
        identifier(asset["id"])
        output_path(asset["path"])
        identifier(asset["role"])
        if asset["media_type"] not in ASSET_MEDIA_TYPES:
            raise ValidationError("experiment asset media type is unsupported")
        if asset["caption"] is not None:
            _text(asset["caption"], "experiment asset caption")
        if asset["role"] == "figure" and (
                asset["media_type"] not in {"image/png", "image/jpeg", "application/pdf"}
                or asset["caption"] is None):
            raise ValidationError("experiment figures require a renderable type and caption")
        roles.setdefault(asset["role"], []).append(asset)

    core = {"schema_version": "results-package-1", "id": value["study_id"], "revision": value["revision"],
            "procedures": value["procedures"], "metrics": value["metrics"], "findings": value["findings"],
            "limitations": value["limitations"], "assets": [
                {key: asset[key] for key in ("path", "sha256", "role")} for asset in assets]}
    validate_results_package(core)
    configured = {item["id"]: item for item in experiment["primary_outcomes"]}
    observed = {item["id"]: item for item in value["metrics"]}
    if set(configured) - set(observed):
        raise ValidationError("experiment output omits a configured primary outcome")
    if any(observed[key]["unit"] != contract["unit"] for key, contract in configured.items()):
        raise ValidationError("experiment output changes a configured primary outcome unit")
    null_primary = {
        metric_id for metric_id in configured
        if observed[metric_id]["value"] is None
    }
    if len(null_primary) == len(configured):
        raise ValidationError(
            "all declared primary outcomes are null; the experiment has no estimable "
            "primary result. Diagnose the undefinedness against the executable model, "
            "measurement procedure, and raw observations before proposing a repair. "
            "Preserve the admitted research question and primary outcome definitions; "
            "do not replace an outcome or change a parameter range merely to obtain a "
            "non-null value. Preserve censored values as null and never substitute zero "
            "or a grid boundary")
    null_metrics = {metric["id"]: metric for metric in value["metrics"]
                    if metric["value"] is None}
    for metric_id, metric in null_metrics.items():
        if any(_contains_nonfinite_numeric_marker(
                metric[field], metric_id=metric_id, unit=metric["unit"])
               for field in ("conditions", "presentation")):
            raise ValidationError(
                f"metric {metric_id!r} uses a non-finite numeric marker for an explicitly undefined "
                "outcome; keep the value null and state only its supported censoring or "
                "undefinedness reason")
    for finding in value["findings"]:
        linked_null_metrics = [null_metrics[metric_id] for metric_id in finding["metric_ids"]
                               if metric_id in null_metrics]
        if any(_contains_nonfinite_numeric_marker(
                finding["statement"], metric_id=metric["id"], unit=metric["unit"])
               for metric in linked_null_metrics):
            raise ValidationError(
                f"finding {finding['id']!r} uses a non-finite numeric marker for an undefined "
                "outcome; keep the metric null and state only its supported censoring or "
                "undefinedness reason")
    if not set(experiment["limitations"]).issubset(value["limitations"]):
        raise ValidationError("experiment output omits a frozen design limitation")

    for requirement in experiment["required_assets"]:
        matches = [asset for asset in roles.get(requirement["role"], [])
                   if asset["media_type"] in requirement["media_types"]]
        if len(matches) < requirement["min_count"]:
            raise ValidationError("experiment output omits a required asset")
    if "analysis" in value:
        value["analysis"] = validate_analysis(value["analysis"])
    # The executable result is admitted on reproducibility and independent
    # recalculation first.  A quality contract is a substantive publication
    # floor, not a pre-execution response-format gate: an author may omit the
    # reader-facing analysis ledger even when it has emitted valid raw data.
    # ``_package`` records the resulting quality admission and the paper
    # pipeline turns any deficit into a scoped Methods work order.  This keeps
    # a real scientific failure blocking while preventing a missing summary
    # field from burning the capability repair budget.
    canonical_bytes(value)
    return value


def validate_deterministic_validation(value, experiment, candidate_sha256):
    exact(value, {"schema_version", "study_id", "candidate_sha256", "decision", "checks",
                  "metric_recalculations", "limitations"}, "deterministic experiment validation")
    if value["schema_version"] != "experiment-validation-1" or value["study_id"] != experiment["id"]:
        raise ValidationError("deterministic validation does not match the experiment")
    if value["candidate_sha256"] != candidate_sha256:
        raise ValidationError("deterministic validation does not bind the exact program output")
    if value["decision"] not in {"accepted", "rejected"}:
        raise ValidationError("deterministic validation decision is invalid")
    if not isinstance(value["checks"], list) or not value["checks"]:
        raise ValidationError("deterministic validation requires checks")
    check_ids = set()
    for check in value["checks"]:
        exact(check, {"id", "outcome", "evidence"}, "deterministic validation check")
        identifier(check["id"])
        if check["id"] in check_ids or check["outcome"] not in {"passed", "failed"}:
            raise ValidationError("deterministic validation check is duplicated or invalid")
        check_ids.add(check["id"])
        _text(check["evidence"], "deterministic validation evidence")
    recalculations = value["metric_recalculations"]
    if not isinstance(recalculations, list) or not recalculations:
        raise ValidationError("deterministic validation requires metric recalculations; "
            "validator checks=" + json.dumps(value["checks"][:8], ensure_ascii=False)[:2400] +
            "; each primary_outcomes entry already has its final exact metric ID; "
            "do not append a condition suffix again")
    metric_ids = set()
    for metric in recalculations:
        exact(metric, {"metric_id", "reported_value", "recalculated_value", "tolerance", "matches"},
              "metric recalculation")
        identifier(metric["metric_id"])
        if metric["metric_id"] in metric_ids or type(metric["matches"]) is not bool:
            raise ValidationError("metric recalculation is duplicated or invalid")
        metric_ids.add(metric["metric_id"])
        reported = metric["reported_value"]
        recalculated = metric["recalculated_value"]
        reported_is_number = type(reported) in (int, float) and math.isfinite(reported)
        recalculated_is_number = (
            type(recalculated) in (int, float) and math.isfinite(recalculated))
        if (reported is not None and not reported_is_number) or (
                recalculated is not None and not recalculated_is_number):
            raise ValidationError(
                "metric recalculation values must be finite numbers or matching explicit nulls")
        if (type(metric["tolerance"]) not in (int, float)
                or not math.isfinite(metric["tolerance"]) or metric["tolerance"] < 0):
            raise ValidationError("metric recalculation tolerance is invalid")
        if reported is None or recalculated is None:
            # A censored/undefined estimand is reproducible only when the
            # independent validator reaches the same undefined status. Never
            # coerce it to a boundary value or zero for arithmetic comparison.
            observed_match = reported is None and recalculated is None
        else:
            observed_match = abs(float(reported) - float(recalculated)) <= float(metric["tolerance"])
        if metric["matches"] is not observed_match:
            raise ValidationError("metric recalculation match flag contradicts its values")
    configured_metric_ids = {item["id"] for item in experiment["primary_outcomes"]}
    if metric_ids != configured_metric_ids:
        missing = sorted(configured_metric_ids - metric_ids)
        unexpected = sorted(metric_ids - configured_metric_ids)
        raise ValidationError(
            "deterministic validation must recalculate exactly the primary outcomes; "
            f"missing metric_ids={missing}; unexpected metric_ids={unexpected}; "
            f"expected metric_ids={sorted(configured_metric_ids)}; "
            f"observed metric_ids={sorted(metric_ids)}")
    if not isinstance(value["limitations"], list):
        raise ValidationError("deterministic validation limitations must be a list")
    for limitation in value["limitations"]:
        _text(limitation, "deterministic validation limitation")
    passed = (all(check["outcome"] == "passed" for check in value["checks"])
              and all(item["matches"] for item in recalculations))
    expected_decision = "accepted" if passed else "rejected"
    if value["decision"] != expected_decision:
        failed_checks = [item["id"] for item in value["checks"]
                         if item["outcome"] != "passed"]
        mismatched_metrics = [item["metric_id"] for item in recalculations
                              if item["matches"] is not True]
        raise ValidationError(
            "deterministic validation decision contradicts its checks: derived "
            f"decision={expected_decision}, observed={value['decision']}; "
            f"failed_checks={failed_checks[:8]}, "
            f"mismatched_metrics={mismatched_metrics[:8]}")
    return value


def bind_deterministic_validation(value, candidate, experiment):
    """Bind validator metric claims to the exact candidate metric values."""
    declared = {item["id"] for item in experiment["primary_outcomes"]}
    reported = {item["id"]: item["value"] for item in candidate["metrics"]}
    recalculations = value["metric_recalculations"]
    if {item["metric_id"] for item in recalculations} != declared:
        raise ValidationError(
            "deterministic validation must bind exactly the declared primary outcomes")
    for item in recalculations:
        metric_id = item["metric_id"]
        if metric_id not in reported \
                or canonical_bytes(item["reported_value"]) != canonical_bytes(reported[metric_id]):
            raise ValidationError(
                "deterministic validation changed a reported primary metric")
    return value


def validate_model_review(value, reviewer_id, finding_ids, work_orders=None,
                          program_output=None, deterministic_validation=None):
    work_orders = validate_work_orders(work_orders)
    required_fields = [
        "reviewer_id", "decision", "checks", "finding_assessments", "limitations",
    ]
    if work_orders:
        required_fields.append("work_order_assessments")
    if not isinstance(value, dict) or set(required_fields) - set(value):
        raise ValidationError(
            "experiment model review omits required contract fields")
    # Model-call artifacts preserve the complete provider response. The
    # scientific review contract consumes only its required fields so harmless
    # supplemental summaries do not discard otherwise valid assessments.
    value = {key: value[key] for key in required_fields}
    if value["reviewer_id"] != reviewer_id or value["decision"] not in REVIEW_DECISIONS:
        raise ValidationError("experiment review identity or decision is invalid")
    required_checks = REVIEW_CHECKS | ({"work_order_resolution"} if work_orders else set())
    if not isinstance(value["checks"], list) or len(value["checks"]) != len(required_checks):
        raise ValidationError("experiment review must execute every required check")
    seen = set()
    for check in value["checks"]:
        exact(check, {"check_id", "outcome", "evidence"}, "experiment review check")
        if check["check_id"] in seen or check["check_id"] not in required_checks:
            raise ValidationError("experiment review check is unknown or duplicated")
        seen.add(check["check_id"])
        if check["outcome"] not in {"passed", "failed", "insufficient_evidence"}:
            raise ValidationError("experiment review check outcome is invalid")
        _text(check["evidence"], "experiment review check evidence")
    assessments = value["finding_assessments"]
    if not isinstance(assessments, list):
        raise ValidationError("experiment finding assessments must be a list")
    observed_ids = []
    malformed_assessments = []
    for item in assessments:
        finding_id = item.get("finding_id") if isinstance(item, dict) else None
        if isinstance(finding_id, str):
            observed_ids.append(finding_id)
        else:
            malformed_assessments.append(str(item)[:160])
    counts = {finding_id: observed_ids.count(finding_id) for finding_id in set(observed_ids)}
    missing_ids = sorted(finding_ids - set(observed_ids))
    unknown_ids = sorted(set(observed_ids) - finding_ids, key=str)
    duplicate_ids = sorted((finding_id for finding_id, count in counts.items()
                            if count > 1), key=str)
    if (malformed_assessments or missing_ids or unknown_ids
            or duplicate_ids or len(assessments) != len(finding_ids)):
        raise ValidationError(
            "experiment review finding IDs must match the required IDs exactly once; "
            f"missing={missing_ids}; unknown={unknown_ids}; duplicates={duplicate_ids}; "
            f"malformed={malformed_assessments[:4]}; required_ids={sorted(finding_ids)}")
    for item in assessments:
        exact(item, {"finding_id", "outcome", "rationale"}, "finding assessment")
        if item["outcome"] not in {"supported", "overstated", "insufficient_evidence"}:
            raise ValidationError("finding assessment outcome is invalid")
        _text(item["rationale"], "finding assessment rationale")
    if work_orders:
        expected = {item["id"] for item in work_orders}
        order_assessments = value["work_order_assessments"]
        ids = [item.get("work_order_id") for item in order_assessments
               if isinstance(item, dict)] if isinstance(order_assessments, list) else []
        if (not isinstance(order_assessments, list) or len(ids) != len(order_assessments)
                or any(not isinstance(item, str) for item in ids)
                or set(ids) != expected or len(ids) != len(set(ids))
                or not isinstance(program_output, dict)):
            raise ValidationError("review work-order assessments must match active orders exactly once")
        for item in order_assessments:
            exact(item, {"work_order_id", "outcome", "evidence_paths", "limitation_path", "rationale"},
                  "review work-order assessment")
            if item["outcome"] not in {"resolved", "bounded", "not_resolved"}:
                raise ValidationError("review work-order outcome is invalid")
            _text(item["rationale"], "review work-order rationale")
            evidence_paths = item["evidence_paths"]
            if (not isinstance(evidence_paths, list) or not evidence_paths
                    or any(not isinstance(path, str) for path in evidence_paths)
                    or len(evidence_paths) != len(set(evidence_paths))):
                raise ValidationError("review work-order evidence must cite unique result paths")
            for pointer in evidence_paths:
                parts = pointer[1:].split("/", 1) if pointer.startswith("/") else []
                root = (parts[0].replace("~1", "/").replace("~0", "~")
                        if parts else None)
                if root == "deterministic_validation":
                    if not isinstance(deterministic_validation, dict):
                        raise ValidationError(
                            "review work-order evidence cites unavailable independent validation")
                    cited = _json_pointer_value(
                        {"deterministic_validation": deterministic_validation}, pointer)
                elif root in REVIEW_RESULT_ROOTS:
                    cited = _json_pointer_value(program_output, pointer)
                else:
                    raise ValidationError(
                        "review work-order evidence must point to program output or independent validation")
                if cited is None or cited == "" or cited == [] or cited == {}:
                    raise ValidationError("review work-order evidence path points to empty result data")
            limitation_path = item["limitation_path"]
            if item["outcome"] == "bounded" or limitation_path is not None:
                if (not isinstance(limitation_path, str)
                        or not limitation_path.startswith("/limitations/")):
                    raise ValidationError(
                        "bounded work orders require a result limitation path; any supplied limitation path must resolve")
                limitation = _json_pointer_value(program_output, limitation_path)
                if not isinstance(limitation, str) or not limitation.strip():
                    raise ValidationError("work-order limitation path must resolve to nonempty text")
            if item["outcome"] == "resolved" and limitation_path is not None:
                raise ValidationError("resolved work orders cannot cite a limitation path")
        order_check = next(item for item in value["checks"]
                           if item["check_id"] == "work_order_resolution")
        unresolved = any(item["outcome"] == "not_resolved" for item in order_assessments)
        if unresolved and order_check["outcome"] == "passed":
            raise ValidationError("work-order check cannot pass while a scoped order is unresolved")
        if not unresolved and order_check["outcome"] != "passed":
            raise ValidationError("work-order check must pass only after every order is evidenced or bounded")
    if not isinstance(value["limitations"], list):
        raise ValidationError("experiment review limitations must be a list")
    for limitation in value["limitations"]:
        _text(limitation, "experiment review limitation")
    return value


def _review_repair_directives(reviews):
    """Carry adverse reviewer claims forward without treating them as verified defects."""
    directives = []
    for review in reviews if isinstance(reviews, list) else []:
        if not isinstance(review, dict):
            continue
        reviewer_id = review.get("reviewer_id")
        if not isinstance(reviewer_id, str) or not reviewer_id.strip():
            continue
        for check in review.get("checks", []):
            if not isinstance(check, dict) or check.get("outcome") == "passed":
                continue
            check_id = check.get("check_id")
            evidence = check.get("evidence")
            if not isinstance(check_id, str) or not isinstance(evidence, str):
                continue
            directives.append({
                "id": f"{reviewer_id}-{check_id}",
                "kind": "review_check_to_verify",
                "source": reviewer_id,
                "text": (
                    f"{reviewer_id} reported check {check_id} as {check.get('outcome')!r}. "
                    "This is a reviewer verdict, not an independently confirmed defect. "
                    "Reproduce the check from the retained observations and deterministic "
                    "validation before changing code, data, or claims; if the reported outcome "
                    "conflicts with its evidence, record and resolve that contradiction rather "
                    f"than assuming a repair is needed. Reported evidence: {evidence.strip()}"
                )[:2200],
            })
        for assessment in review.get("finding_assessments", []):
            if not isinstance(assessment, dict) or assessment.get("outcome") == "supported":
                continue
            finding_id = assessment.get("finding_id")
            rationale = assessment.get("rationale")
            if not isinstance(finding_id, str) or not isinstance(rationale, str):
                continue
            directives.append({
                "id": f"{reviewer_id}-{finding_id}",
                "kind": "review_finding_to_verify",
                "source": reviewer_id,
                "text": (
                    f"{reviewer_id} assessed finding {finding_id} as "
                    f"{assessment.get('outcome')!r}. Verify that scope judgment against the "
                    "linked observations and the stated claim before changing the analysis; "
                    f"reviewer rationale: {rationale.strip()}"
                )[:2200],
            })
    if not directives:
        rejected = [item.get("reviewer_id") for item in reviews
                    if isinstance(item, dict) and item.get("decision") == "rejected"]
        if rejected:
            directives.append({
                "id": "review-rejected-disposition",
                "kind": "rejected_disposition",
                "source": ", ".join(str(item) for item in rejected),
                "text": (
                    "An independent reviewer rejected the result without a machine-readable failed "
                    "check or unsupported finding. Inspect the retained review artifact and establish "
                    "the concrete scientific reason from the observations before changing the analysis."
                ),
            })
    return directives[:16]


def reconcile_model_review_disposition(value):
    """Make the review disposition consistent with its recorded evidence."""
    adverse_checks = [
        item["check_id"] for item in value["checks"] if item["outcome"] != "passed"]
    adverse_findings = [
        {"finding_id": item["finding_id"], "outcome": item["outcome"]}
        for item in value["finding_assessments"] if item["outcome"] != "supported"]
    if value["decision"] == "rejected" or not (adverse_checks or adverse_findings):
        return value
    return {
        **value,
        "decision": "rejected",
        "reported_decision": value["decision"],
        "decision_reconciliation": {
            "rule": "adverse_review_evidence_requires_rejection",
            "adverse_checks": adverse_checks,
            "adverse_findings": adverse_findings,
        },
    }


def _scoped_review_assessment(reviews, finding_ids):
    """Admit only findings supported by every reviewer when only scope is disputed."""
    if not isinstance(reviews, list) or not reviews:
        return None
    if not any(item.get("decision") == "rejected" for item in reviews
               if isinstance(item, dict)):
        return None

    scope_checks = {"inference_scope", "limitation_coverage"}
    adverse_checks = []
    for review in reviews:
        if not isinstance(review, dict):
            return None
        for check in review.get("checks", []):
            if not isinstance(check, dict) or check.get("outcome") == "passed":
                continue
            if check.get("check_id") not in scope_checks:
                return None
            adverse_checks.append({
                "reviewer_id": review.get("reviewer_id"),
                "check_id": check.get("check_id"),
                "outcome": check.get("outcome"),
                "evidence": check.get("evidence"),
            })

    assessments = {}
    for review in reviews:
        reviewer_id = review.get("reviewer_id")
        by_id = {item.get("finding_id"): item
                 for item in review.get("finding_assessments", [])
                 if isinstance(item, dict)}
        if set(by_id) != finding_ids:
            return None
        for finding_id in finding_ids:
            assessments.setdefault(finding_id, []).append({
                "reviewer_id": reviewer_id,
                "outcome": by_id[finding_id].get("outcome"),
                "rationale": by_id[finding_id].get("rationale"),
            })
    accepted = sorted(
        finding_id for finding_id, outcomes in assessments.items()
        if all(item["outcome"] == "supported" for item in outcomes)
    )
    if not accepted:
        return None
    withheld = sorted(finding_ids - set(accepted))
    if not withheld and not adverse_checks:
        return None
    disagreement_notes = [
        f"{item['reviewer_id']}:{item['check_id']}={item['outcome']}"
        for item in adverse_checks
    ]
    disagreement_notes.extend(
        f"{reviewer_id}:{finding_id}={item['outcome']}"
        for finding_id, outcomes in assessments.items()
        for item in outcomes if item["outcome"] != "supported"
        for reviewer_id in (item["reviewer_id"],)
    )
    summary = (
        "Independent deterministic checks and numerical recalculation passed. The assessment admits only "
        "findings supported by every assigned reviewer; disputed inference-scope findings are withheld. "
        "Unresolved scope evidence: " + "; ".join(disagreement_notes)
    )
    return {
        "accepted_findings": accepted,
        "withheld_findings": [
            {"finding_id": finding_id,
             "reviewer_assessments": assessments[finding_id]}
            for finding_id in withheld
        ],
        "scope_review_evidence": {"adverse_checks": adverse_checks},
        "summary": summary[:4000],
    }


def validate_assessment(value, study_id, evidence_refs, review_outcomes, finding_ids,
                        expected_limitations, *, reviews=None):
    base_fields = {"schema_version", "study_id", "decision", "summary", "evidence_refs",
                   "reviewer_outcomes", "accepted_findings", "limitations"}
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    if schema_version == "experiment-assessment-2":
        exact(value, base_fields | {"withheld_findings", "scope_review_evidence"},
              "scoped experiment assessment")
    else:
        exact(value, base_fields, "experiment assessment")
    if schema_version not in {"experiment-assessment-1", "experiment-assessment-2"} \
            or value["study_id"] != study_id:
        raise ValidationError("experiment assessment does not match the study")
    if value["decision"] not in REVIEW_DECISIONS:
        raise ValidationError("experiment assessment decision is invalid")
    _text(value["summary"], "experiment assessment summary")
    if value["evidence_refs"] != evidence_refs or value["reviewer_outcomes"] != review_outcomes:
        raise ValidationError("experiment assessment must copy exact evidence and reviewer outcomes")
    if (not isinstance(value["accepted_findings"], list)
            or len(value["accepted_findings"]) != len(set(value["accepted_findings"]))
            or set(value["accepted_findings"]) - finding_ids):
        raise ValidationError("experiment assessment accepted_findings are invalid")
    if value["limitations"] != expected_limitations:
        raise ValidationError("experiment assessment must preserve the exact program limitations")
    for limitation in value["limitations"]:
        _text(limitation, "experiment assessment limitation")
    accepted = set(value["accepted_findings"])
    if value["decision"] == "rejected":
        if accepted:
            raise ValidationError("rejected assessment cannot admit findings")
    elif value["decision"] == "accepted":
        if (accepted != finding_ids
                or any(item["decision"] == "rejected" for item in review_outcomes)):
            raise ValidationError("accepted assessment must retain every finding without a rejected review")
    elif reviews is None:
        if (accepted != finding_ids
                or any(item["decision"] == "rejected" for item in review_outcomes)):
            raise ValidationError(
                "legacy accepted_with_limitations assessment must retain every independently reviewed finding")
    elif schema_version == "experiment-assessment-2":
        if value["decision"] != "accepted_with_limitations" or reviews is None:
            raise ValidationError("scoped acceptance requires the validated independent reviews")
        expected = _scoped_review_assessment(reviews, finding_ids)
        observed = {
            "accepted_findings": value["accepted_findings"],
            "withheld_findings": value["withheld_findings"],
            "scope_review_evidence": value["scope_review_evidence"],
            "summary": value["summary"],
        }
        if expected is None or canonical_bytes(observed) != canonical_bytes(expected):
            raise ValidationError(
                "scoped assessment must preserve the exact unanimous findings and review evidence")
    else:
        scope_checks = {"inference_scope", "limitation_coverage"}
        evidence_by_finding = {finding_id: [] for finding_id in finding_ids}
        adverse_checks = []
        for review in reviews:
            for check in review.get("checks", []):
                if check.get("outcome") != "passed":
                    if check.get("check_id") not in scope_checks:
                        raise ValidationError(
                            "limited acceptance cannot override a methods or calculation failure")
                    adverse_checks.append(check)
            for item in review.get("finding_assessments", []):
                evidence_by_finding[item["finding_id"]].append(item["outcome"])
        if not accepted or any(
                evidence_by_finding[finding_id]
                and any(outcome != "supported" for outcome in evidence_by_finding[finding_id])
                for finding_id in accepted):
            raise ValidationError(
                "limited acceptance may include only findings supported by every reviewer")
        withheld = finding_ids - accepted
        if any(all(outcome == "supported" for outcome in evidence_by_finding[finding_id])
               for finding_id in withheld):
            raise ValidationError(
                "withheld findings must have a recorded reviewer disagreement")
        if (any(item["decision"] == "rejected" for item in review_outcomes)
                and not adverse_checks
                and not withheld):
            raise ValidationError(
                "limited acceptance must preserve the rejected review's scoped concern")
    return value


class ExperimentRunner(ExecutionRuntime):
    def __init__(self, project_dir, config, *, on_progress=None):
        config = validate_experiment_config(config)
        super().__init__(project_dir, config, worker_target=_invoke_worker, on_progress=on_progress)
        self.experiment = config["experiment"]
        self.work_orders = config.get("work_orders", [])
        self.operations = OperationsCell(self.control, self.store, project_id=config["project_id"])
        self.bindings = {}
        self.profile_refs = {}
        self.design_ref = None
        self.execution_refs = []
        self.asset_records = []
        self.asset_files = []
        self.review_records = []
        self.raw_results_path = None
        self.raw_results_sha256 = None
        self.serial = 0
        self.literature = {"survey_ref": None, "assessment_ref": None, "state": None}
        self.literature_gate_mismatch = None
        self.research_expansion_requests = []
        self.worker_slots = configured_worker_slots(self.config["limits"])
        self.time_policy = TimePolicy(stage_seconds=self.experiment["stage_seconds"],
            unit_count=len(self.experiment["reviewers"]),
            worker_slots=self.worker_slots,
            wall_clock_seconds=self.config["limits"]["wall_clock_seconds"],
            policy=self.config.get("time_policy"))
        self.time_policy.started_at = self.started
        self.deadline = min(self.deadline, self.started + self.time_policy.hard_seconds)

    def _initialize(self):
        record = self._publish(f"command/scores/{self.experiment['id']}", "note", {
            "schema_version": "experiment-score-1", "experiment": self.experiment,
            "time_policy": self.config.get("time_policy")}, "principal")
        self.score_ref = record["artifact_ref"]
        design = build_research_design(self.experiment)
        design_record = self._publish(
            "inputs/research-design", "note", design, "principal", subjects=[self.score_ref])
        self.design_ref = design_record["artifact_ref"]
        gate = self.experiment["literature_gate"]
        if gate is not None:
            control = ControlStore(gate["project_dir"])
            try:
                store = ArtifactStore(control)
                survey_gate = SurveyGate(control, store)
                survey_gate.require_current(gate["survey_ref"])
                assessment = survey_gate.require_current_assessment(gate["assessment_ref"])
                body = json.loads(store.read_body(assessment["body_hash"]))
                self.literature = {"survey_ref": gate["survey_ref"],
                                   "assessment_ref": gate["assessment_ref"], "state": body["state"]}
                if body["state"] != gate["required_state"]:
                    self.literature_gate_mismatch = {
                        "observed_state": body["state"],
                        "required_state": gate["required_state"],
                        "survey_ref": gate["survey_ref"],
                        "assessment_ref": gate["assessment_ref"],
                    }
                    self.research_expansion_requests = [{
                        "id": f"literature-gate-{body['state']}",
                        "kind": "literature_expansion",
                        "owner": "research.intelligence",
                        "objective": "Expand the literature search and secure enough verified full-text evidence to resolve the experiment admission state.",
                        "why": f"The current literature assessment is {body['state']}, while this experiment requires {gate['required_state']}.",
                        "success_condition": f"A current literature assessment reports {gate['required_state']} with its source and identity checks satisfied.",
                        "evidence_needed": "Additional scoped searches, verified source identities, and decisive full-text quotations bound to the accepted survey.",
                    }]
            finally:
                control.close()
        self.context = self._publish("inputs/experiment-context", "note", {
            "objective": self.config["objective"], "supplied_context": self.config["supplied_context"],
            "literature": self.literature}, "principal", subjects=[self.score_ref])
        self._checkpoint("experiment_frozen", force=True)

    def _setup(self):
        for name in ("execution", "validation"):
            capability = self.experiment[name]
            client = deepcopy(capability["client"])
            command = list(client["command"])
            command[0] = str(Path(command[0]).absolute()) if "/" in command[0] else (shutil.which(command[0]) or command[0])
            workspace = self.operations.workspace_dir(capability["id"])
            environment_files = list(capability["environment_files"])
            if client.get("sandbox_required"):
                if len(command) != 2:
                    raise ValidationError(
                        "generated experiment programs require one pinned source argument")
                source = Path(command[1])
                if (not source.is_absolute() or not source.is_file()
                        or str(source) not in environment_files):
                    raise ValidationError(
                        "generated experiment source is not pinned by its capability descriptor")
                local_source = workspace / f"{name}.py"
                temporary = workspace / f".{name}.{uuid.uuid4().hex}.tmp"
                try:
                    temporary.write_bytes(source.read_bytes())
                    os.replace(temporary, local_source)
                finally:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                command[1] = str(local_source)
                environment_files.append(str(local_source))
            client.update(command=command, cwd=str(workspace),
                          own_process_group=False)
            state = self.operations.register(capability["id"], adapter="local_program", client=client,
                representative=capability["representative"], engineer=f"operations.engineer.{name}",
                environment_files=environment_files)
            state = self.operations.ensure_ready(capability["id"], self._call,
                operator=f"operations.operator.{name}", verifier=f"operations.verifier.{name}",
                purpose=f"Prepare the frozen experiment {name} program")
            if state["state"] != "ready":
                raise ValidationError(f"experiment {name} capability is unavailable: {state['reason']}")
            self.bindings[name] = state["binding"]
            self.profile_refs[name] = state["profile_ref"]
            self.operations.idle(capability["id"])
            self.information_changes.append({"kind": f"verified_{name}_capability",
                                             "ref": state["verification_ref"]})
        if self.bindings["execution"]["identity_sha256"] == self.bindings["validation"]["identity_sha256"]:
            raise ValidationError("experiment execution and validation must use distinct pinned program identities")
        self._checkpoint("experiment_capabilities_ready", force=True)

    def _program_input(self):
        configured_input = deepcopy(self.experiment["execution"]["input"])
        if self.work_orders:
            configured_input["work_orders"] = deepcopy(self.work_orders)
        return experiment_program_payload(self.experiment, configured_input)

    def _execute_once(self):
        decision = self.time_policy.admit("production", task_count=1)
        if not decision["allowed"]:
            raise ValidationError(f"time admission deferred experiment execution: {decision['reason']}")
        started = time.monotonic()
        result, ref = self.operations.run(self.bindings["execution"], {"input": self._program_input()}, self._call,
            operator="methods.experiment-operator")
        self.time_policy.observe("production", time.monotonic() - started)
        candidate = validate_program_output(result["document"], self.experiment, self.work_orders)
        self.execution_refs.append(ref)
        return candidate

    def _workspace_assets(self, candidate):
        workspace = self.operations.workspace_dir(self.experiment["execution"]["id"]).resolve()
        total, captured = 0, []
        for asset in candidate["assets"]:
            path = workspace / asset["path"]
            try:
                resolved = path.resolve(strict=True)
                if not resolved.is_file() or not resolved.is_relative_to(workspace) or path.is_symlink():
                    raise OSError("asset is outside the experiment workspace")
                body = resolved.read_bytes()
            except OSError as exc:
                raise ValidationError(f"experiment asset is unavailable: {asset['id']}") from exc
            total += len(body)
            if total > self.experiment["max_asset_bytes"]:
                raise ValidationError("experiment assets exceed the configured byte limit")
            if sha256_hex(body) != asset["sha256"]:
                raise ValidationError("experiment asset does not match its declared SHA-256")
            if asset["media_type"] == "image/png" and not body.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValidationError("experiment asset media type does not match PNG bytes")
            if asset["media_type"] == "image/jpeg" and not body.startswith(b"\xff\xd8\xff"):
                raise ValidationError("experiment asset media type does not match JPEG bytes")
            captured.append((asset, body))
        return captured

    def _capture_assets(self, candidate):
        for asset, body in self._workspace_assets(candidate):
            record = self.store.publish_artifact(logical_id=f"methods/experiment-assets/{asset['id']}",
                artifact_type="source_capture", author="methods.experiment-operator", body=body,
                media_type=asset["media_type"], inputs=[{"ref": self.execution_refs[0], "purpose": "subject"}],
                score_ref=self.score_ref)
            self.asset_records.append(record)
            self.asset_files.append({"descriptor": asset, "body": body, "record": record})

    def _deterministic_validate(self, candidate, candidate_sha256):
        decision = self.time_policy.admit("unit_review", task_count=1)
        if not decision["allowed"]:
            raise ValidationError(f"time admission deferred deterministic validation: {decision['reason']}")
        payload = experiment_validation_payload(
            self.experiment, self.experiment["validation"]["input"],
            candidate, candidate_sha256)
        started = time.monotonic()
        result, execution_ref = self.operations.run(self.bindings["validation"], {"input": payload}, self._call,
            operator="methods.independent-calculator")
        self.time_policy.observe("unit_review", time.monotonic() - started)
        value = validate_deterministic_validation(result["document"], self.experiment, candidate_sha256)
        bind_deterministic_validation(value, candidate, self.experiment)
        record = self._publish("methods/experiment-deterministic-validation", "verification", {
            **value, "execution_ref": execution_ref}, "methods.independent-calculator",
            subjects=[*self.execution_refs, execution_ref, *[item["artifact_ref"] for item in self.asset_records]])
        if value["decision"] != "accepted":
            raise ValidationError("independent metric recalculation rejected the experiment output")
        return value, record, execution_ref

    def _review_assignment(self, reviewer, candidate, deterministic, evidence_refs):
        summary = {key: candidate[key] for key in (
            "schema_version", "study_id", "revision", "procedures", "observations",
            "metrics", "findings", "limitations", "assets")}
        if "analysis" in candidate:
            summary["analysis"] = candidate["analysis"]
        required_checks = REVIEW_CHECKS | ({"work_order_resolution"} if self.work_orders else set())
        instructions = (
            "Inspect the summarized output and every attached figure. Return only the complete JSON object with reviewer_id, decision, checks, finding_assessments, limitations; no preamble, markdown, or chain-of-thought. "
            "Copy reviewer.id as reviewer_id. Execute each required check exactly once as {check_id,outcome,evidence}; outcomes are passed, failed, or insufficient_evidence. "
            "Assess every ID in required_finding_ids exactly once as {finding_id,outcome,rationale}; outcomes are supported, overstated, or insufficient_evidence. Do not invent suffixes, placeholders, or summary IDs. "
            "Decision is accepted, accepted_with_limitations, or rejected. Both accepted decisions require every check passed and every finding supported. Use accepted_with_limitations only for limitations that do not contradict a check or finding outcome. If any check is failed or insufficient_evidence, or any finding is overstated or insufficient_evidence, choose rejected and preserve those outcomes; never soften adverse evidence to make the decision acceptable. "
            "Treat program numbers as observations only after the independent recalculation passes. Check method-contract alignment, calculation trace, inference scope, and limitation coverage. "
            "Do not infer general scientific truth, novelty, or external validity from one finite computational study. Preserve negative and mixed results. Keep each evidence or rationale field concise (at most 400 characters) and include no more than six limitations."
        )
        if self.work_orders:
            instructions += (
                " For every supplied work order, independently assess its exact result evidence and return one "
                "work_order_assessments row with work_order_id, outcome, evidence_paths, limitation_path, rationale. "
                "The experiment program must not self-certify work orders; do not treat an absent "
                "producer-authored work_order_assessments field as a defect. These assessments belong in your "
                "review response only. "
                "Use outcome=resolved only when its success_condition is demonstrated, outcome=bounded only when "
                "the remaining scope is explicitly supported by a result limitation, and outcome=not_resolved "
                "when the objective or success condition is not demonstrated. Cite evidence paths rooted at "
                "program output or the supplied independent recalculation. Array segments may be numeric indices "
                "or a unique stable object identifier (an `id` or `*_id` field), for example /metrics/0, "
                "/findings/finding_kill_condition, or /deterministic_validation/checks/independent_recalculation; "
                "producer-authored assessments are not evidence. A bounded "
                "outcome must cite its exact limitation_path. Use null when no result limitation is cited. "
                "evidence_paths must use the result_reference_contract roots directly, without a "
                "program_output_summary prefix. Artifact references, source hashes, work-order paths, "
                "and prose belong in rationale and are not result pointers. "
                "The work_order_resolution check must pass iff all orders are resolved or explicitly bounded; otherwise "
                "mark it insufficient_evidence or failed and reject the review. Do not treat a narrative assertion "
                "as evidence when the cited observations, metrics, findings, procedures, or analysis do not support it."
            )
        assignment = {"phase": "experiment_result_review", "reviewer": reviewer,
            "scientific_input_recovery": scientific_input_recovery_contract(),
            "study": {key: self.experiment[key] for key in (
                "id", "study_type", "domain", "research_question", "hypothesis", "method", "parameters",
                "seed", "run_count", "stopping_rule", "primary_outcomes", "limitations")},
            "program_output_summary": summary, "deterministic_validation": deterministic,
            "evidence_refs": evidence_refs, "required_checks": sorted(required_checks),
            "required_finding_ids": sorted(item["id"] for item in candidate["findings"]),
            "instructions": instructions}
        if self.work_orders:
            assignment["work_orders"] = deepcopy(self.work_orders)
            assignment["required_work_order_ids"] = [item["id"] for item in self.work_orders]
            assignment["result_reference_contract"] = {
                "evidence_path_roots": [f"/{root}" for root in sorted(REVIEW_RESULT_ROOTS)]
                    + ["/deterministic_validation"],
                "limitation_path": {
                    "required_for": ["bounded"],
                    "available_paths": [f"/limitations/{index}" for index in range(len(candidate["limitations"]))],
                    "by_outcome": {"resolved": "null", "bounded": "an available limitation path",
                                   "not_resolved": "null or an available limitation path"},
                },
            }
        return assignment

    def _model_checked(self, jobs, *, images, stage):
        pending, accepted, feedback = list(jobs), {}, {}
        repair_mode = self.config["limits"].get("repair_mode", "bounded")
        rounds = (itertools.count() if repair_mode == "until_deadline"
                  else range(self.config["limits"]["max_rounds"]))
        continuation_prefixes = {}
        continuation_rounds = {}
        continuation_no_progress = []
        for _ in rounds:
            self._ensure_active()
            admission = self.time_policy.admit(stage, task_count=len(pending))
            if not admission["allowed"]:
                raise ValidationError(f"time admission deferred experiment review: {admission['reason']}")
            review_model = deepcopy(self.config["model"])
            if review_model.get("protocol") == "openai_compatible":
                review_model["output_format"] = "json_object"
            specs = []
            for job in pending:
                self.serial += 1
                assignment = deepcopy(job["assignment"])
                if job["name"] in feedback and job["name"] not in continuation_prefixes:
                    assignment["validation_feedback"] = feedback[job["name"]]
                params = {"client": review_model,
                          "prompt": json.dumps(assignment, ensure_ascii=False)}
                if job["name"] in continuation_prefixes:
                    params["continuation_text"] = continuation_prefixes[job["name"]]
                specs.append({"task_id": f"experiment-{job['name']}-{self.serial}", "kind": "model",
                              "actor": job["actor"], "task_kind": "verification",
                              "params": {**params, **({"images": images} if images else {})}})
            outcomes = self._call_batch(specs, max_parallel=self.worker_slots)
            rejected = []
            for job, spec in zip(pending, specs):
                outcome = outcomes[spec["task_id"]]
                if not outcome["ok"]:
                    if outcome.get("error_type") in {"ModelCallError", "ModelBudgetExceededError"}:
                        self._raise_model_failure(outcome)
                    raise ValidationError(f"experiment model dispatch failed: {job['name']}: {outcome['error']}")
                result = ModelResult(**outcome["result"])
                self.time_policy.observe(stage, result.elapsed_seconds)
                prior_prefix = continuation_prefixes.get(job["name"])
                try:
                    if result.finish_reason != "stop":
                        raise ValidationError(
                            f"review response remains incomplete after continuation: {result.finish_reason}")
                    value = result.json_object()
                    job["validator"](value)
                except (ValidationError, TypeError, ValueError, KeyError) as exc:
                    made_progress = (
                        isinstance(result.text, str) and bool(result.text)
                        and (prior_prefix is None or len(result.text) > len(prior_prefix))
                    )
                    if (result.finish_reason == "length" and made_progress
                            and continuation_rounds.get(job["name"], 0) < 2):
                        continuation_prefixes[job["name"]] = result.text
                        continuation_rounds[job["name"]] = (
                            continuation_rounds.get(job["name"], 0) + 1)
                        feedback.pop(job["name"], None)
                        self.tasks.transition(
                            spec["task_id"], "blocked", "command.controller",
                            reason="review response is still truncated; continue the same provider output")
                        rejected.append(job)
                        continue
                    if result.finish_reason == "length":
                        continuation_no_progress.append({
                            "reviewer": job["name"],
                            "response_ref": outcome.get("record_ref"),
                            "prior_prefix_chars": len(prior_prefix or ""),
                            "response_chars": len(result.text or ""),
                        })
                        continuation_prefixes.pop(job["name"], None)
                        feedback[job["name"]] = {
                            "error": "continuation returned no additional text or exhausted its bounded continuation rounds",
                            "response_ref": outcome.get("record_ref"),
                            "finish_reason": result.finish_reason,
                            "scope": "Do not replay unchanged content; preserve this response as an incomplete reviewer artifact.",
                        }
                        self.tasks.transition(
                            spec["task_id"], "blocked", "command.controller",
                            reason="review continuation made no progress")
                        rejected.append(job)
                        continue
                    continuation_prefixes.pop(job["name"], None)
                    feedback[job["name"]] = {"error": str(exc),
                        "response_ref": outcome.get("record_ref"),
                        "finish_reason": result.finish_reason,
                        "scope": (
                            "Complete the previously truncated reviewer response without changing the review assignment."
                            if result.finish_reason == "length" else
                            "Repair only the response schema or unsupported acceptance. Return the complete requested JSON object."
                        )}
                    self.tasks.transition(spec["task_id"], "blocked", "command.controller", reason=str(exc))
                    rejected.append(job)
                    continue
                self._complete(spec["task_id"])
                continuation_prefixes.pop(job["name"], None)
                continuation_rounds.pop(job["name"], None)
                accepted[job["name"]] = (value, outcome["record_ref"])
            if not rejected:
                return accepted
            if continuation_no_progress:
                detail = json.dumps(continuation_no_progress, ensure_ascii=False, sort_keys=True)[:6000]
                raise ModelContractError(
                    "experiment model review continuation made no progress; "
                    f"incomplete outputs={detail}")
            pending = rejected
        failures = [
            {"reviewer": job["name"], **feedback[job["name"]]}
            for job in pending if job["name"] in feedback
        ]
        detail = json.dumps(failures, ensure_ascii=False, sort_keys=True)[:6000]
        raise ModelContractError(
            "experiment model review did not satisfy its contract; "
            f"rejected responses={detail}")

    def _model_reviews(self, candidate, deterministic, deterministic_record):
        finding_ids = {item["id"] for item in candidate["findings"]}
        image_records = [item for item in self.asset_files if item["descriptor"]["media_type"] in {"image/png", "image/jpeg"}]
        images = [{"path": str(Path(self.store.objects_dir) / item["record"]["body_hash"]),
                   "media_type": item["descriptor"]["media_type"], "sha256": item["record"]["body_hash"]}
                  for item in image_records[:16]]
        evidence_refs = [*self.execution_refs, deterministic_record["artifact_ref"],
                         *[item["artifact_ref"] for item in self.asset_records]]
        jobs = [{"name": reviewer["id"], "actor": f"methods.experiment-reviewer.{reviewer['id']}",
                 "assignment": self._review_assignment(reviewer, candidate, deterministic, evidence_refs),
                 "validator": lambda value, rid=reviewer["id"]: validate_model_review(
                     value, rid, finding_ids, self.work_orders, candidate,
                     deterministic_validation=deterministic)}
                for reviewer in self.experiment["reviewers"]]
        values = self._model_checked(jobs, images=images, stage="integrated_review")
        reviews = []
        for reviewer, job in zip(self.experiment["reviewers"], jobs):
            value, execution_ref = values[job["name"]]
            value = reconcile_model_review_disposition(value)
            record = self._publish(f"methods/experiment-reviews/{reviewer['id']}", "verification", {
                **value, "execution_ref": execution_ref}, job["actor"],
                subjects=[execution_ref, *evidence_refs])
            self.review_records.append(record)
            reviews.append(value)
        return reviews, images

    def _work_order_package_assessments(self, candidate, reviews):
        if not self.work_orders:
            return []
        reviewer_by_id = {review["reviewer_id"]: review for review in reviews}
        packages = []
        for order in self.work_orders:
            reviewers = [next(item for item in review["work_order_assessments"]
                              if item["work_order_id"] == order["id"])
                         for review in reviewer_by_id.values()]
            outcomes = {item["outcome"] for item in reviewers}
            disposition = (
                "unresolved" if "not_resolved" in outcomes else
                "bounded" if "bounded" in outcomes else "completed"
            )
            evidence_paths = list(dict.fromkeys(
                path for item in reviewers for path in item["evidence_paths"]))
            rationales = [f"{reviewer['reviewer_id']}: {item['rationale']}"
                          for reviewer, item in zip(reviewer_by_id.values(), reviewers)]
            limitation_values = list(dict.fromkeys(
                _json_pointer_value(candidate, item["limitation_path"])
                for item in reviewers if item["limitation_path"] is not None))
            limitation = "; ".join(limitation_values) if disposition == "bounded" else None
            packages.append({
                "id": order["id"], "kind": order["kind"], "owner": order["owner"],
                "objective": order["objective"], "success_condition": order["success_condition"],
                "disposition": disposition, "summary": "; ".join(rationales),
                "evidence_paths": evidence_paths, "limitation": limitation,
                "reviewer_assessments": [
                    {"reviewer_id": reviewer["reviewer_id"], "outcome": item["outcome"],
                     "evidence_paths": list(item["evidence_paths"]), "rationale": item["rationale"]}
                    for reviewer, item in zip(reviewer_by_id.values(), reviewers)
                ],
            })
        return packages

    def _assess(self, candidate, deterministic_record, reviews, images):
        review_refs = [record["artifact_ref"] for record in self.review_records]
        evidence_refs = [*self.execution_refs, deterministic_record["artifact_ref"], *review_refs,
                         *[item["artifact_ref"] for item in self.asset_records]]
        outcomes = [{"reviewer_id": value["reviewer_id"], "decision": value["decision"]} for value in reviews]
        finding_ids = {item["id"] for item in candidate["findings"]}
        rejected_reviews = [item for item in reviews if item["decision"] == "rejected"]
        if rejected_reviews:
            scoped = _scoped_review_assessment(reviews, finding_ids)
            if scoped is not None:
                value = {
                    "schema_version": "experiment-assessment-2",
                    "study_id": self.experiment["id"],
                    "decision": "accepted_with_limitations",
                    "summary": scoped["summary"],
                    "evidence_refs": evidence_refs,
                    "reviewer_outcomes": outcomes,
                    "accepted_findings": scoped["accepted_findings"],
                    "limitations": list(candidate["limitations"]),
                    "withheld_findings": scoped["withheld_findings"],
                    "scope_review_evidence": scoped["scope_review_evidence"],
                }
                value = validate_assessment(
                    value, self.experiment["id"], evidence_refs, outcomes,
                    finding_ids, candidate["limitations"], reviews=reviews)
                record = self._publish(
                    "methods/experiment-assessment", "verification",
                    {**value, "execution_ref": None,
                     "decision_basis": "unanimous_finding_intersection"},
                    "methods.experiment-arbiter", subjects=evidence_refs)
                return value, record
            # A model arbiter can repeatedly soften an independent rejection,
            # then consume its bounded retries failing the same deterministic
            # gate. Reconcile the disposition from the validated reviews.
            # Package-level rejection means no finding is admitted as a
            # verified claim; raw observations remain available separately.
            rejection_reasons = []
            for review in rejected_reviews:
                reasons = [f"{item['check_id']}={item['outcome']}"
                           for item in review["checks"] if item["outcome"] != "passed"]
                reasons.extend(
                    f"finding {item['finding_id']}={item['outcome']}"
                    for item in review["finding_assessments"] if item["outcome"] != "supported")
                rejection_reasons.append(
                    f"{review['reviewer_id']}: {', '.join(reasons) or 'review disposition rejected'}")
            value = {
                "schema_version": "experiment-assessment-1",
                "study_id": self.experiment["id"],
                "decision": "rejected",
                "summary": (
                    "The experiment completed its declared replay and deterministic checks, but independent "
                    "scientific review rejected the result package: " + "; ".join(rejection_reasons) + ". "
                    "No findings are admitted as verified claims. Raw observations and review evidence are "
                    "retained for interpretation and targeted repair."
                ),
                "evidence_refs": evidence_refs,
                "reviewer_outcomes": outcomes,
                "accepted_findings": [],
                "limitations": list(candidate["limitations"]),
            }
            value = validate_assessment(value, self.experiment["id"], evidence_refs, outcomes,
                                        finding_ids, candidate["limitations"])
            execution_ref = None
            decision_basis = "deterministic_review_reconciliation"
        else:
            assignment = {"phase": "experiment_result_assessment", "study_id": self.experiment["id"],
                "question": self.experiment["research_question"], "hypothesis": self.experiment["hypothesis"],
                "metrics": candidate["metrics"], "findings": candidate["findings"],
                "design_limitations": self.experiment["limitations"], "program_limitations": candidate["limitations"],
                "independent_reviews": reviews, "evidence_refs_exact": evidence_refs,
                "reviewer_outcomes_exact": outcomes,
                "instructions": (
                    "Reconcile the exact reviews without changing measured values. Return an experiment-assessment-1 object with exactly schema_version, study_id, decision, summary, evidence_refs, reviewer_outcomes, accepted_findings, limitations. "
                    "Copy the exact evidence_refs and reviewer_outcomes in order. Decision is accepted, accepted_with_limitations, or rejected. "
                    "Accept a finding only when every supplied review marks it supported. An accepted result must list every finding ID. "
                    "Copy program_limitations exactly as limitations, in the same order and wording; reviewers may assess them but the arbiter cannot rewrite the result package. "
                    "State what the finite study shows without claiming novelty or external validity." )}
            job = {"name": "final-assessment", "actor": "methods.experiment-arbiter", "assignment": assignment,
                   "validator": lambda value: validate_assessment(
                       value, self.experiment["id"], evidence_refs, outcomes, finding_ids, candidate["limitations"])}
            value, execution_ref = self._model_checked([job], images=images, stage="integrated_review")["final-assessment"]
            decision_basis = "model_arbiter"
        record = self._publish("methods/experiment-assessment", "verification", {
            **value, "execution_ref": execution_ref, "decision_basis": decision_basis},
            "methods.experiment-arbiter",
            subjects=[*([execution_ref] if isinstance(execution_ref, str) else []), *evidence_refs])
        return value, record

    def _package(self, candidate, candidate_sha256, validation_record, validator_execution_ref,
                 assessment, assessment_record, reviews):
        package_dir = self.dir / "output" / "results-package"
        package_dir.mkdir(parents=True, exist_ok=True)
        raw = canonical_bytes({"schema_version": "experiment-observations-1", "study_id": self.experiment["id"],
                               "observations": candidate["observations"]})
        raw_path = package_dir / "raw-data.json"
        raw_path.write_bytes(raw)
        raw_record = self.store.publish_artifact(logical_id="methods/experiment-assets/raw_data",
            artifact_type="source_capture", author="methods.experiment-operator", body=raw,
            media_type="application/json", inputs=[{"ref": self.execution_refs[0], "purpose": "subject"}],
            score_ref=self.score_ref)
        assets = [{"id": "raw_data", "path": "raw-data.json", "sha256": sha256_hex(raw),
                   "role": "raw_data", "media_type": "application/json", "caption": None}]
        for item in self.asset_files:
            asset = item["descriptor"]
            destination = package_dir / asset["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item["body"])
            assets.append(dict(asset))
        scoped_assessment = assessment.get("schema_version") == "experiment-assessment-2"
        accepted_findings = set(assessment["accepted_findings"])
        candidate_findings = {item["id"]: item for item in candidate["findings"]}
        withheld_findings = [
            {
                **deepcopy(candidate_findings[item["finding_id"]]),
                "reviewer_assessments": deepcopy(item["reviewer_assessments"]),
            }
            for item in assessment.get("withheld_findings", [])
        ]
        limitations = list(candidate["limitations"])
        for item in withheld_findings:
            rationales = "; ".join(
                f"{review['reviewer_id']} ({review['outcome']}): {review['rationale']}"
                for review in item["reviewer_assessments"]
                if review["outcome"] != "supported")
            limitations.append(
                f"A candidate finding was withheld from the admitted results: {item['statement']} "
                f"Independent review: {rationales}"
            )
        scope_evidence = assessment.get("scope_review_evidence", {})
        for check in scope_evidence.get("adverse_checks", []):
            limitations.append(
                f"The inference scope remains bounded: {check['evidence']}"
            )
        limitations = list(dict.fromkeys(limitations))
        work_order_assessments = self._work_order_package_assessments(candidate, reviews)
        for item in work_order_assessments:
            if (item["disposition"] == "bounded" and item["limitation"]
                    and item["limitation"] not in limitations):
                limitations.append(item["limitation"])
        package = {"schema_version": "results-package-3" if scoped_assessment
                   else "results-package-2", "id": self.experiment["id"],
            "revision": self.experiment["revision"], "study_type": self.experiment["study_type"],
            "question": self.experiment["research_question"], "hypothesis": self.experiment["hypothesis"],
            "procedures": candidate["procedures"], "metrics": candidate["metrics"],
            "findings": ([deepcopy(item) for item in candidate["findings"]
                          if item["id"] in accepted_findings]
                         if scoped_assessment else deepcopy(candidate["findings"])),
            "limitations": limitations,
            "assets": assets,
            "provenance": {"score_ref": self.score_ref,
                "literature_survey_ref": self.literature["survey_ref"],
                "literature_assessment_ref": self.literature["assessment_ref"],
                "execution_refs": self.execution_refs, "validator_execution_ref": validator_execution_ref,
                "execution_profile_ref": self.profile_refs["execution"],
                "validation_profile_ref": self.profile_refs["validation"],
                "design_ref": self.design_ref,
                "replay_sha256": candidate_sha256},
            "validation": {"decision": assessment["decision"],
                "deterministic_validation_ref": validation_record["artifact_ref"],
                "model_review_refs": [record["artifact_ref"] for record in self.review_records],
                "assessment_ref": assessment_record["artifact_ref"]}}
        if scoped_assessment:
            package["withheld_findings"] = withheld_findings
        if work_order_assessments:
            package["work_order_assessments"] = work_order_assessments
        if self.experiment.get("quality_contract") is not None:
            package["quality_contract"] = deepcopy(self.experiment["quality_contract"])
            if "analysis" in candidate:
                package["analysis"] = deepcopy(candidate["analysis"])
            package["quality_admission"] = evaluate_result_package_quality(package)
        validate_results_package(package, base_dir=package_dir)
        path = package_dir / "results-package.json"
        path.write_bytes(canonical_bytes(package))
        record = self._publish("methods/experiment-results/package", "results_package", package,
            "methods.result-integrator", subjects=[self.score_ref, *self.execution_refs,
                raw_record["artifact_ref"], validation_record["artifact_ref"], assessment_record["artifact_ref"],
                *[item["artifact_ref"] for item in self.asset_records]])
        if assessment["decision"] != "rejected":
            adopted = self.store.adopt(record["artifact_id"], target_version=record["version"],
                                       expected_accepted_version=None, actor="command.controller")
            self.incumbent = adopted["artifact_ref"]
            self.time_policy.mark_first_verified_result(self.incumbent)
            self.verified_changes.append({"kind": "accepted_experiment_results", "ref": self.incumbent,
                                          "assessment_ref": assessment_record["artifact_ref"]})
        return package, path

    def run(self):
        if threading.current_thread() is not threading.main_thread():
            raise ValidationError("experiment run requires the main thread")
        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt("termination requested")))
        try:
            return self._run()
        finally:
            signal.signal(signal.SIGTERM, previous)

    def _run(self):
        status, error, package, package_path = "blocked", None, None, None
        failure = None
        validation_record = assessment_record = None
        try:
            self._initialize()
            if not self.time_policy.snapshot()["initial_hard_limit_feasible"]:
                raise ValidationError("configured experiment stages do not fit the hard deadline")
            if self.literature_gate_mismatch is not None:
                # A missing prerequisite is a research work order, not a
                # failed experiment attempt.  Return the bounded request so
                # the Composer can reopen the survey closure automatically.
                status = "research_expansion_required"
                error = "literature admission requires additional evidence"
            else:
                self._setup()
                candidate = self._execute_once()
                self._capture_assets(candidate)
                replay = self._execute_once()
                candidate_sha256 = sha256_hex(canonical_bytes(candidate))
                if canonical_bytes(replay) != canonical_bytes(candidate):
                    raise ValidationError("frozen replay did not reproduce the exact experiment output")
                replay_assets = self._workspace_assets(replay)
                if any(body != self.asset_files[index]["body"] for index, (_, body) in enumerate(replay_assets)):
                    raise ValidationError("frozen replay did not reproduce the exact experiment assets")
                self.raw_results_path = self.dir / "output" / "raw-results.json"
                self.raw_results_path.parent.mkdir(parents=True, exist_ok=True)
                self.raw_results_path.write_bytes(canonical_bytes(candidate))
                self.raw_results_sha256 = candidate_sha256
                deterministic, validation_record, validator_execution_ref = self._deterministic_validate(
                    candidate, candidate_sha256)
                reviews, images = self._model_reviews(candidate, deterministic, validation_record)
                assessment, assessment_record = self._assess(candidate, validation_record, reviews, images)
                package, package_path = self._package(candidate, candidate_sha256, validation_record,
                                                      validator_execution_ref, assessment, assessment_record,
                                                      reviews)
                quality = package.get("quality_admission")
                requests = [deepcopy(item) for item in self.research_expansion_requests
                            if isinstance(item, dict)]
                if isinstance(quality, dict):
                    requests.extend(deepcopy(item) for item in quality.get("expansion_requests", [])
                                    if isinstance(item, dict))
                if assessment.get("decision") == "rejected":
                    repair_id = "repair_rejected_experiment_result"
                    repair = next((item for item in requests if item.get("id") == repair_id), None)
                    if repair is None:
                        repair = {"id": repair_id, "kind": "additional_experiment",
                                  "owner": "methods.validation"}
                        requests.append(repair)
                    review_directives = _review_repair_directives(reviews)
                    directive_ids = [item["id"] for item in review_directives]
                    directive_evidence = " | ".join(
                        item["text"] for item in review_directives)
                    repair.update({
                        "objective": (
                            "Diagnose the rejected experiment on its current research direction. Treat each "
                            "review directive below as a hypothesis to verify, not as an established defect. "
                            "Reproduce adverse checks against retained observations and deterministic validation; "
                            "change source or analysis only for a confirmed issue, otherwise reconcile the "
                            "contradiction and state a narrowly bounded limitation. Do not alter measured data "
                            "or substitute an unrelated topic. Reviewer reports: "
                            + directive_evidence[:4200]),
                        "why": (
                            "The independent assessment rejected this package, so none of its findings may be used "
                            "as verified paper evidence. The raw observations, executable sources, and review "
                            "findings are retained for a targeted Methods repair."),
                        "success_condition": (
                            "Every review directive is evidenced by a changed executable analysis, a corrected "
                            "claim supported by unchanged observations, or an explicit bounded limitation. A fresh "
                            "replay, independent recalculation, and assessment verify the repair."),
                        "evidence_needed": (
                            "Independent reviewer findings, current executor and validator sources, raw observations, "
                            "targeted repaired or narrowed analysis, deterministic replay, and a fresh result package. "
                            f"Assessment={assessment_record.get('artifact_ref')}; package={package_path}."),
                        "review_directives": review_directives,
                        "acceptance_checks": [
                            f"Disposition {directive_id} with an executable change, a result-backed claim correction, "
                            "or an explicit bounded limitation; prose comments alone do not satisfy this check."
                            for directive_id in directive_ids
                        ] + [
                            "Preserve measured observations; correct interpretation or analysis without editing a result package.",
                            "Replay the fresh program and independently recalculate the primary outcomes.",
                        ],
                        "experiment_repair_plan": {
                            "schema_version": "experiment-repair-plan-1",
                            "mode": "diagnose_patch_execute_recalculate",
                            "design_axis": "review_directed",
                            "root_causes": [],
                            "review_hypotheses": [item["text"] for item in review_directives[:8]],
                            "required_changes": [
                                f"Verify review hypothesis {item['id']} against retained evidence; "
                                "make a source or claim change only if the defect is confirmed, otherwise "
                                "record the reconciled contradiction or a bounded limitation."
                                for item in review_directives[:8]
                            ],
                            "must_change": directive_ids[:8],
                            "must_preserve": [
                                "the admitted scientific phenomenon and the measured observations",
                                "independent replay and recalculation",
                            ],
                            "prohibited": [
                                "comment-only repairs with unchanged scientific behavior",
                                "editing result JSON instead of source or analysis",
                                "claiming a failed or unresolved check passed",
                            ],
                        },
                        "repair_strategy": "review_directed",
                    })
                unique_requests = {}
                for item in requests:
                    request_id = item.get("id")
                    if isinstance(request_id, str):
                        unique_requests[request_id] = item
                self.research_expansion_requests = list(unique_requests.values())
                quality_repair_required = (
                    isinstance(quality, dict)
                    and quality.get("decision") == "research_expansion_required"
                )
                if assessment.get("decision") == "rejected" or quality_repair_required:
                    status = "research_expansion_required"
                    error = (
                        "experiment assessment rejected the result; targeted Methods repair required"
                        if assessment.get("decision") == "rejected"
                        else "experiment result has scoped research-quality debt requiring Methods work"
                    )
                else:
                    status = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.blockers.append({"reason": error})
            if isinstance(exc, KeyboardInterrupt):
                status = "paused"
                failure = {"kind": "process_interrupted"}
            elif isinstance(exc, ModelCallError):
                failure = exc.failure_details()
        finally:
            for row in self.control._conn.execute("SELECT task_id FROM tasks WHERE state='awaiting_review'").fetchall():
                self.tasks.transition(row[0], "blocked", "command.controller",
                                      reason="Run ended without an accepted experiment result")
            self._checkpoint(status, force=True)
        result = {"run_id": self.run_id, "project_id": self.config["project_id"], "status": status,
            "error": error, "failure": failure, "study_id": self.experiment["id"], "incumbent_ref": self.incumbent,
            "score_ref": getattr(self, "score_ref", None), "literature": self.literature,
            "execution_refs": self.execution_refs,
            "research_question": self.experiment["research_question"],
            "raw_results": str(self.raw_results_path.resolve()) if self.raw_results_path else None,
            "raw_results_sha256": self.raw_results_sha256,
            "deterministic_validation_ref": validation_record["artifact_ref"] if validation_record else None,
            "model_review_refs": [record["artifact_ref"] for record in self.review_records],
            "assessment_ref": assessment_record["artifact_ref"] if assessment_record else None,
            "results_package": str(package_path) if package_path else None,
            "research_expansion_requests": deepcopy(self.research_expansion_requests),
            "usage": self.budget.get_window("run-window"), "unreported_usage": self.usage_gaps,
            "time_plan": self.time_policy.snapshot(), "blockers": self.blockers,
            "event_chain": self.control.verify_chain(), "release_status": "not_released"}
        self._publish("command/results/final", "report", result, "command.controller")
        self._export(result, package)
        self.control.close()
        return result

    def _export(self, result, package):
        output = self.dir / "output"
        output.mkdir(exist_ok=True)
        (output / "run.json").write_bytes(canonical_bytes(result))
        lines = [f"# {self.experiment['research_question']}", "",
                 f"Run status: **{result['status']}**.", ""]
        if package is not None:
            if package["validation"]["decision"] == "rejected":
                lines += ["## Rejected result package", "",
                          "Independent assessment: **rejected**.", "",
                          "No findings from this package are admitted as verified claims. Raw observations and review evidence are retained for interpretation and targeted repair.", "",
                          "### Candidate findings (not verified)", ""]
            else:
                lines += ["## Result", "",
                          f"Independent assessment: **{package['validation']['decision']}**.", ""]
            for finding in package["findings"]:
                lines.append(f"- {finding['statement']}")
            lines += ["", "## Metrics", ""]
            for metric in package["metrics"]:
                lines.append(f"- **{metric['id']}**: {metric['presentation']}")
            lines += ["", "## Limitations", ""]
            for limitation in package["limitations"]:
                lines.append(f"- {limitation}")
            if package.get("withheld_findings"):
                lines += ["", "## Withheld candidate findings", ""]
                for finding in package["withheld_findings"]:
                    lines.append(f"- **Not admitted:** {finding['statement']}")
                    for review in finding["reviewer_assessments"]:
                        if review["outcome"] != "supported":
                            lines.append(
                                f"  - {review['reviewer_id']}: {review['rationale']}"
                            )
        elif result["error"]:
            lines += ["## Blocker", "", result["error"]]
        (output / "experiment.md").write_text("\n".join(lines) + "\n")
