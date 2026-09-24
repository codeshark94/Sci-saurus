"""Durable failure analysis and executable repair plans for Composer stages.

An unsuccessful stage is not itself a retry instruction.  This module turns a
bounded failure record into an inspectable dossier and a scoped repair plan.
The plan is deliberately declarative: the Composer and the allowlisted stage
runner decide which commands can be executed, while the dossier preserves the
evidence that justified that decision.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from scisaurus.core.schema import canonical_bytes


SCHEMA_VERSION = "composer-failure-recovery-1"

_RESOURCE_MARKERS = (
    "cooling down", "rate limit", "429", "quota", "deadline",
    "timed out", "timeout", "context budget", "process_interrupted",
    "termination requested", "missing credential", "invalid endpoint",
    "provider configuration", "provider unavailable",
)
_OPERATIONAL_MARKERS = (
    "new project directory", "overwrite", "checkpoint", "resume controller",
    "project directory", "workspace is already",
)
_MODEL_CONTRACT_MARKERS = (
    "invalid json", "malformed json",
    "response contract", "output contract", "model output",
    "did not finish normally", "finish_reason", "research argument review",
    "research argument was not accepted",
)
_EXPERIMENT_PROGRAM_MARKERS = (
    # Keep this list specific to a generated program or its observed result.
    # The surrounding ``capability foundry did not admit a program`` envelope
    # is also used for provider/model-contract failures, so it cannot classify
    # the failure by itself.  Likewise, a program-author truncation is a model
    # response failure until an executable program actually exists.
    "executor failed in the sandbox", "program validator",
    "program admission", "independent recalculation", "results-package",
    "result package",
)


def _bounded(value, *, depth=0, max_depth=6, max_items=32, max_keys=48, max_text=4000):
    if depth >= max_depth and isinstance(value, (dict, list)):
        return "[truncated]"
    if isinstance(value, dict):
        output = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_keys:
                output["[truncated_keys]"] = True
                break
            output[str(key)] = _bounded(
                item, depth=depth + 1, max_depth=max_depth,
                max_items=max_items, max_keys=max_keys, max_text=max_text,
            )
        return output
    if isinstance(value, list):
        output = [_bounded(
            item, depth=depth + 1, max_depth=max_depth,
            max_items=max_items, max_keys=max_keys, max_text=max_text,
        ) for item in value[:max_items]]
        if len(value) > max_items:
            output.append("[truncated_items]")
        return output
    if isinstance(value, str):
        return value if len(value) <= max_text else value[:max_text] + "...[truncated]"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:max_text]


def classify_failure(stage_kind, error, stage_result=None):
    """Classify a failure without mistaking a resource fence for science."""
    error_type = getattr(error, "__class__", type(error)).__name__
    status_code = getattr(error, "status_code", None)
    failure_kind = ((stage_result or {}).get("failure", {}).get("kind")
                    if isinstance(stage_result, dict)
                    and isinstance(stage_result.get("failure"), dict) else None)
    text = " ".join(
        str(item or "") for item in (
            getattr(error, "failure_class", None),
            getattr(error, "stop_reason", None),
            error,
            (stage_result or {}).get("error") if isinstance(stage_result, dict) else None,
        )
    ).casefold()
    # Sandbox executor/validator failures are failures of the generated
    # experiment program, even when the envelope includes resource-looking
    # fields such as ``timeout=False`` or ``truncated=True``.  The old order
    # matched the substring ``timeout`` first and incorrectly replayed the
    # same candidate as a provider/resource fence.
    if stage_kind == "experiment" and any(
            marker in text for marker in _EXPERIMENT_PROGRAM_MARKERS):
        return "experiment_failure"
    if (error_type in {
            "ProviderCooldownError", "ProviderConfigurationError", "QuotaExceededError",
            "ComposerHardDeadlineExceeded", "ComposerLateStageResult",
            "CapabilityDeadlineError", "CapabilityModelBudgetExceeded",
            "ModelCallError",
        }
            or failure_kind in {"provider_cooldown", "provider_configuration",
                                "process_interrupted"}
            or status_code in {408, 425, 429, 500, 502, 503, 504}):
        return "resource_fence"
    text = " ".join(
        str(item or "") for item in (
            getattr(error, "failure_class", None),
            getattr(error, "stop_reason", None),
            error,
            (stage_result or {}).get("error") if isinstance(stage_result, dict) else None,
        )
    ).casefold()
    if any(marker in text for marker in _RESOURCE_MARKERS):
        return "resource_fence"
    if any(marker in text for marker in _OPERATIONAL_MARKERS):
        return "operational_recovery"
    # A malformed/truncated provider response must be repaired at the model
    # interface before any scientific continuation is opened.  It is not a
    # rejected hypothesis and it should not replay survey/interpretation work.
    if any(marker in text for marker in _MODEL_CONTRACT_MARKERS):
        return "model_contract"
    if stage_kind == "experiment":
        if any(marker in text for marker in (
                "executor", "validator", "replay", "result package", "results-package",
                "assessment", "estimator", "metric", "observation", "experiment",
                "research-quality", "independent recalculation", "program",
        )):
            return "experiment_failure"
        return "experiment_contract"
    if stage_kind in {"survey", "interpretation", "argument", "paper", "topic_discovery"}:
        return "scientific_review"
    return "stage_contract"


def _file_record(path, *, root=None, include_text=False, max_bytes=24000):
    path = Path(path)
    try:
        body = path.read_bytes()
    except (OSError, ValueError):
        return None
    record = {
        "path": str(path.resolve()),
        "relative_path": str(path.resolve().relative_to(root.resolve()))
        if root is not None and path.resolve().is_relative_to(root.resolve()) else str(path),
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    if include_text:
        text = body[:max_bytes].decode("utf-8", "replace")
        record["text"] = text
        record["text_truncated"] = len(body) > max_bytes
    return record


def inventory_project(project_dir, *, max_files=72, max_file_bytes=2_000_000):
    """Capture a bounded hash inventory; never copy an entire run directory."""
    root = Path(project_dir).resolve() if project_dir else None
    if root is None or not root.exists():
        return []
    records = []
    for path in sorted(root.rglob("*")):
        if len(records) >= max_files:
            break
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        record = _file_record(path, root=root, max_bytes=0)
        if record is not None:
            records.append(record)
    return records


def _result_projection(stage_result):
    if not isinstance(stage_result, dict):
        return {}
    keys = (
        "status", "error", "failure", "study_id", "run_id", "project_id", "incumbent_ref",
        "project_dir", "survey_ref", "nomination_ref", "survey_current",
        "assessment_current", "gap_state", "topic_admission",
        "results_package", "output_path", "execution_refs", "deterministic_validation_ref",
        "model_review_refs", "assessment_ref", "metrics", "findings", "limitations",
        "analysis", "blockers", "research_expansion_requests", "usage", "time_plan",
    )
    projected = {key: stage_result.get(key) for key in keys if key in stage_result}
    # A runner may only return a path after a late validation failure. Include
    # the bounded JSON body as evidence so the next repair does not have to
    # guess which result existed before the failure.
    for key in ("results_package", "output_path"):
        value = stage_result.get(key)
        if not isinstance(value, str):
            continue
        path = Path(value)
        if not path.is_file() or path.suffix.lower() != ".json":
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError, TypeError):
            continue
        projected[f"{key}_snapshot"] = _bounded(payload, max_depth=7, max_items=24, max_text=5000)
    return _bounded(projected, max_depth=7)


def _error_diagnostics(error):
    """Retain model-level verdicts that would otherwise be lost at the runner boundary."""
    diagnostics = {}
    for attribute in (
            "research_argument", "research_review", "research_feedback",
            "research_response", "review", "required_repairs"):
        value = getattr(error, attribute, None)
        if value is not None:
            diagnostics[attribute] = _bounded(value, max_depth=7, max_items=24,
                                              max_text=6000)
    for attribute in ("repair_gate", "repair_attempts", "repair_ledger", "repair_feedback"):
        value = getattr(error, attribute, None)
        if value is not None:
            diagnostics[attribute] = _bounded(value, max_depth=7, max_items=24,
                                              max_text=6000)
    return diagnostics


def _review_directives(specialist_reports, diagnostics):
    """Flatten bounded reviewer findings into executable, evidence-linked directions."""
    directives = []
    seen = set()

    def add(source, kind, value):
        if isinstance(value, dict):
            value = value.get("repair") or value.get("required_change") or value.get("finding") \
                or value.get("problem") or value.get("text")
        if not isinstance(value, str) or not value.strip():
            return
        text = value.strip()[:1800]
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        directives.append({"source": str(source)[:160], "kind": str(kind)[:80], "text": text})

    for report in specialist_reports or []:
        if not isinstance(report, dict):
            continue
        source = report.get("assigned_role") or report.get("role_id") or "specialist"
        response = report.get("response") if isinstance(report.get("response"), dict) else report
        for key in ("critical_findings", "required_repairs", "requested_actions",
                    "repair_scope", "evidence_gaps", "findings"):
            values = response.get(key) if isinstance(response, dict) else None
            if isinstance(values, list):
                for value in values[:8]:
                    add(source, key, value)

    review = diagnostics.get("research_review") if isinstance(diagnostics, dict) else None
    if isinstance(review, dict):
        for key in ("required_repairs", "checks"):
            values = review.get(key)
            if isinstance(values, list):
                for value in values[:12]:
                    if key == "checks" and isinstance(value, dict):
                        if value.get("outcome") in {"failed", "insufficient_evidence"}:
                            add("argument-adjudicator", key, value.get("evidence") or value)
                    else:
                        add("argument-adjudicator", key, value)
    return directives[:24]


def _review_command(review_directives, *, stage_kind):
    if not review_directives:
        return None
    lines = [
        f"[{item['source']}::{item['kind']}] {item['text']}"
        for item in review_directives[:8]
    ]
    return {
        "id": "execute-review-directed-repairs",
        "operation": "execute_review_directives",
        "target": f"{stage_kind or 'stage'} evidence, claims, and unresolved reviewer findings",
        "instruction": (
            "Apply these evidence-backed directives as the repair brief, preserving supported findings "
            "and recording any directive that remains unresolved; do not issue a generic retry: "
            + " | ".join(lines)
        )[:7000],
        "acceptance_check": (
            "Each directive is linked to a changed artifact, a new check/result, or an explicit bounded "
            "limitation; the next review can verify the change independently."
        ),
    }


def build_repair_commands(stage_kind, failure_class, *, stage_result=None,
                          program_snapshot=None, error_text="", review_directives=None):
    """Return concrete, bounded commands for the next scoped work order."""
    if failure_class == "resource_fence":
        return [{
            "id": "reconcile-resource-fence",
            "operation": "reconcile",
            "target": "provider, quota, deadline, or interrupted attempt",
            "instruction": "Reconcile the durable attempt and resume from the latest checkpoint; do not replay a completed call.",
            "acceptance_check": "The next dispatch has a fresh route/deadline admission and the prior call is charged exactly once.",
        }]
    if failure_class == "operational_recovery":
        return [{
            "id": "repair-attempt-namespace",
            "operation": "reopen",
            "target": "the failed stage project namespace",
            "instruction": "Create a fresh attempt namespace, retain the old checkpoint read-only, and bind the same immutable input without overwriting it.",
            "acceptance_check": "The runner starts in a unique attempt directory and the old run remains inspectable.",
        }]
    if failure_class == "model_contract":
        return [{
            "id": "repair-model-response-contract",
            "operation": "reroute_and_compact",
            "target": "the failed model response and its role contract",
            "instruction": "Preserve the scientific input and evidence; send a schema-only repair prompt to the configured fallback role with a bounded output budget, then validate the repaired object locally before reopening any scientific stage.",
            "acceptance_check": "The fallback response parses, satisfies the exact role schema, and no new scientific continuation is admitted for a formatting-only failure.",
        }]

    if stage_kind == "experiment":
        observed = isinstance(stage_result, dict) and any(
            stage_result.get(key) for key in (
                "execution_refs", "results_package", "deterministic_validation_ref",
                "metrics", "findings",
            )
        )
        commands = [
            {
                "id": "inspect-experiment-failure",
                "operation": "inspect",
                "target": "experiment result, raw observations, execution trace, executor source, and validator source",
                "instruction": "Reconstruct the failure from the exact attempt evidence and identify the first invalid assumption; do not infer a cause from the final error alone.",
                "acceptance_check": "A root cause is bound to an observed field, source line, validation check, or reproducible runtime trace.",
            },
            {
                "id": "independent-recalculate-estimand",
                "operation": "recalculate",
                "target": "raw observations and every declared primary outcome",
                "instruction": "Independently recompute the estimand, check finite/non-degenerate variation, and compare the result with the reported metric without using the executor implementation.",
                "acceptance_check": "Every primary outcome either matches an independent recalculation or is explicitly marked unresolved with a scoped limitation.",
            },
            {
                "id": "repair-experiment-program",
                "operation": "edit_program",
                "target": "executor_source and validator_source",
                "instruction": "Use the failure dossier and methods repair panel to make an exact source-level repair to the failed mechanism or estimator. Preserve the admitted question, change the invalid mechanism, and never patch the result JSON or merely rename a threshold.",
                "acceptance_check": "A new capability revision passes static scan, validator readiness, sandbox execution, replay, digest, independent recalculation, and methods review.",
            },
            {
                "id": "fresh-replay-and-review",
                "operation": "execute",
                "target": "a fresh continuation capability and experiment namespace",
                "instruction": "Run the repaired program from a new attempt directory, retain raw output and figures, then rerun deterministic and independent review gates before downstream interpretation.",
                "acceptance_check": "The fresh result is independently checked and its limitations/claim scope are visible to interpretation.",
            },
        ]
        review_command = _review_command(review_directives, stage_kind=stage_kind)
        if review_command is not None:
            commands.insert(1, review_command)
        if observed:
            edit_command = next((item for item in commands
                                 if item.get("id") == "repair-experiment-program"), None)
            if edit_command is not None:
                edit_command["instruction"] += (
                    " Reuse the observed result as diagnostic evidence; do not discard it "
                    "without recording what it falsified."
                )
        return commands

    if stage_kind == "survey":
        return [
            {
                "id": "audit-literature-evidence",
                "operation": "inspect",
                "target": "source identities, full-text spans, contradictory findings, and gap assessment",
                "instruction": "Trace every decisive claim to a reconciled primary source and separate retrieval/format failures from a genuinely closed gap.",
                "acceptance_check": "The gap decision contains source-level evidence, counterevidence, and an explicit currentness assessment.",
            },
            {
                "id": "repair-survey-route",
                "operation": "revise_search",
                "target": "search strategy and evidence contract",
                "instruction": "Change the missing evidence operation (terminology, citation chain, full text, or contradiction search) instead of replaying the same catalog.",
                "acceptance_check": "The fresh survey introduces new evidence or records a justified topic pivot.",
            },
        ]
    if stage_kind in {"interpretation", "argument"}:
        commands = [
            {
                "id": "rebuild-claim-evidence-graph",
                "operation": "reconcile",
                "target": "observations, competing explanations, and material claims",
                "instruction": "Separate observation from mechanism, link each claim to exact evidence, and identify the smallest discriminating analysis still missing.",
                "acceptance_check": "No material claim lacks a source/result link or is presented as causal when the evidence only supports association.",
            },
            {
                "id": "issue-scoped-repair-order",
                "operation": "plan",
                "target": "the unresolved scientific debt",
                "instruction": "Issue one bounded experiment, literature, or analysis command with a falsifiable acceptance check; do not emit a generic retry.",
                "acceptance_check": "The next work order names its target artifact, evidence needed, and stop/advance condition.",
            },
        ]
        review_command = _review_command(review_directives, stage_kind=stage_kind)
        if review_command is not None:
            commands.insert(1, review_command)
        return commands
    if stage_kind == "paper":
        return [
            {
                "id": "repair-manuscript-evidence-chain",
                "operation": "revise",
                "target": "manuscript claims, figures, tables, and reviewer findings",
                "instruction": "Resolve reviewer findings against accepted evidence, preserve negative/uncertain results, and render a new reviewable manuscript.",
                "acceptance_check": "The fresh rendered manuscript has no unresolved blocking finding and every major claim is evidence-linked.",
            },
        ]
    return [{
        "id": "diagnose-stage-contract",
        "operation": "inspect",
        "target": stage_kind or "stage",
        "instruction": "Inspect the exact input, output, and acceptance contract, then issue a bounded repair order tied to the first failed check.",
        "acceptance_check": "The next attempt changes the failed contract or evidence operation and records an independent check.",
    }]


def build_failure_dossier(*, stage, attempt_stage, error, stage_result=None,
                          specialist_reports=None, verifier=None,
                          program_snapshot=None, attempt_number=None):
    """Build an immutable, bounded failure dossier for the Composer ledger."""
    stage_kind = stage.get("kind") if isinstance(stage, dict) else None
    failure_class = classify_failure(stage_kind, error, stage_result)
    project_dir = (attempt_stage or {}).get("project_dir") if isinstance(attempt_stage, dict) else None
    specialist_reports = specialist_reports or []
    diagnostics = _error_diagnostics(error)
    directives = _review_directives(specialist_reports, diagnostics)
    commands = build_repair_commands(
        stage_kind, failure_class, stage_result=stage_result,
        program_snapshot=program_snapshot, error_text=str(error),
        review_directives=directives,
    )
    observed = _result_projection(stage_result)
    dossier = {
        "schema_version": SCHEMA_VERSION,
        "stage_id": stage.get("id") if isinstance(stage, dict) else None,
        "stage_kind": stage_kind,
        "attempt_number": (
            attempt_number if attempt_number is not None else
            ((attempt_stage or {}).get("attempt_number")
             if isinstance(attempt_stage, dict) else None)
        ),
        "project_dir": str(Path(project_dir).resolve()) if project_dir else None,
        "failure_class": failure_class,
        "recoverable": failure_class not in {"resource_fence"},
        "error": str(error)[:6000],
        "observed_result": observed,
        "project_inventory": inventory_project(project_dir),
        "program_snapshot": _bounded(program_snapshot or [], max_depth=5, max_items=8, max_text=18000),
        "specialist_reports": _bounded(specialist_reports, max_depth=6, max_items=8, max_text=2200),
        "verifier": _bounded(verifier or {}, max_depth=6, max_items=8, max_text=2200),
        "model_diagnostics": diagnostics,
        "review_directives": directives,
        "repair_commands": commands,
        "acceptance_checks": [item["acceptance_check"] for item in commands],
        "next_action": (
            "resume_from_checkpoint" if failure_class == "resource_fence" else
            "repair_model_contract_before_stage_retry" if failure_class == "model_contract" else
            "create_scoped_repair_work_order_and_repair_before_rerun"
        ),
    }
    dossier["input_sha256"] = hashlib.sha256(canonical_bytes(dossier)).hexdigest()
    return dossier


def build_repair_request(dossier, *, stage_id):
    """Project one valid department work order from a failure dossier."""
    stage_kind = dossier.get("stage_kind")
    mapping = {
        "topic_discovery": ("topic_refinement", "research.intelligence"),
        "survey": ("literature_expansion", "research.intelligence"),
        "experiment": ("additional_experiment", "methods.validation"),
        "interpretation": ("interpretation_expansion", "strategy.interpretation"),
        "argument": ("interpretation_expansion", "strategy.interpretation"),
        "paper": ("manuscript_revision", "editorial.composer"),
    }
    kind, owner = mapping.get(stage_kind, ("recovery", "executive-command"))
    digest = dossier.get("input_sha256", "")[:16]
    commands = dossier.get("repair_commands", [])
    objective = (commands[0].get("instruction") if commands and isinstance(commands[0], dict)
                 else "Inspect the failure evidence and issue a bounded repair.")
    directives = dossier.get("review_directives", [])
    if isinstance(directives, list) and directives:
        directive_text = " ".join(
            str(item.get("text", "")) for item in directives[:6]
            if isinstance(item, dict) and item.get("text")
        )
        if directive_text:
            objective = (objective + " Apply these recorded reviewer directives: "
                         + directive_text)[:1800]
    checks = dossier.get("acceptance_checks", [])
    return {
        "id": f"repair-{stage_id}-{digest}",
        "kind": kind,
        "owner": owner,
        # ``kind`` identifies the department's work-order contract.  It is
        # not always the stage that must execute the repair: an argument is
        # owned by Strategy's interpretation function, but the repaired
        # claim-evidence packet must be rerun by the argument stage itself.
        # Keep that execution address explicit instead of making the Composer
        # infer it from the owner and accidentally reopening an unrelated
        # stage.
        "target_stage_id": stage_id,
        "target_stage_kind": stage_kind,
        "repair_priority": "immediate",
        "objective": objective[:1800],
        "why": f"{stage_id} produced {dossier.get('failure_class')} evidence: {dossier.get('error', '')[:1800]}",
        "success_condition": (checks[0] if checks else
                              "The repaired scope passes its independent acceptance checks."),
        "evidence_needed": "Failure dossier, exact prior inputs, source/result artifacts, root-cause analysis, reviewer directives, and a fresh independently checked output.",
        "source_stage_id": stage_id,
        "failure_dossier_ref": None,
        "failure_input_sha256": dossier.get("input_sha256"),
        "repair_commands": deepcopy(commands),
        "acceptance_checks": deepcopy(checks),
        "review_directives": deepcopy(directives),
        "recovery_mode": "repair_then_rerun",
    }


__all__ = [
    "SCHEMA_VERSION", "build_failure_dossier", "build_repair_commands",
    "build_repair_request", "classify_failure", "inventory_project",
]
