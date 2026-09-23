"""A bounded research program built from an admitted topic portfolio.

Topic discovery already produces several candidate directions.  This module
turns that portfolio into an explicit, human-readable decision surface: every
candidate has a plan, conditional paper outcomes, a kill condition, and a
status.  The selected direction is only a routing decision; survey and
experiment evidence still decide whether it is promoted, revised, or stopped.
"""
from __future__ import annotations

from copy import deepcopy
import re

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes


SCHEMA_VERSION = "research-program-1"
SELECTION_MODES = {"provisional"}
BRANCH_STATUSES = {"selected", "retained", "unexplored"}
PAPER_OUTCOME_IDS = {"supportive", "null_boundary", "ambiguous"}
PROGRAM_FIELDS = {
    "schema_version", "theme", "objective", "branches", "selected_id",
    "selection_mode", "selection_rationale", "selection_criteria", "decision_log",
}
BRANCH_FIELDS = {
    "id", "title", "question", "hypothesis", "mechanism", "plan",
    "research_form", "evidence_mode", "comparison_type", "paper_if",
    "kill_if", "evidence_obligations", "source_refs", "status",
}
OUTCOME_FIELDS = {"id", "condition", "contribution", "required_evidence"}
DECISION_FIELDS = {"id", "action", "branch_ids", "rationale"}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    if len(value) > 4096:
        raise ValidationError(f"{name} is too long")
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _strings(value, name, *, minimum=0, maximum=32):
    if (not isinstance(value, list) or not minimum <= len(value) <= maximum
            or len(value) != len(set(value))):
        raise ValidationError(f"{name} must contain {minimum} to {maximum} unique strings")
    for item in value:
        _text(item, name)
    return value


def _outcome(value, branch_id):
    if not isinstance(value, dict) or set(value) != OUTCOME_FIELDS:
        raise ValidationError(f"research branch {branch_id} paper_if item has an invalid shape")
    _identifier(value["id"], "paper outcome id")
    if value["id"] not in PAPER_OUTCOME_IDS:
        raise ValidationError("paper outcome id is unsupported")
    _text(value["condition"], "paper outcome condition")
    _text(value["contribution"], "paper outcome contribution")
    _strings(value["required_evidence"], "paper outcome required_evidence", minimum=1, maximum=16)
    return value


def validate_research_program(value):
    """Validate a complete program without assigning scientific truth."""
    if not isinstance(value, dict) or set(value) != PROGRAM_FIELDS:
        raise ValidationError(f"research program requires exactly {sorted(PROGRAM_FIELDS)}")
    if value["schema_version"] != SCHEMA_VERSION:
        raise ValidationError("research program schema version is unsupported")
    _text(value["theme"], "research program theme")
    _text(value["objective"], "research program objective")
    if value["selection_mode"] not in SELECTION_MODES:
        raise ValidationError("research program selection_mode is unsupported")
    _text(value["selection_rationale"], "research program selection_rationale")
    _strings(value["selection_criteria"], "research program selection_criteria", minimum=2, maximum=8)

    branches = value["branches"]
    if not isinstance(branches, list) or not 3 <= len(branches) <= 8:
        raise ValidationError("research program requires three to eight branches")
    branch_ids = set()
    selected = []
    for branch in branches:
        if not isinstance(branch, dict) or set(branch) != BRANCH_FIELDS:
            raise ValidationError("research branch has an invalid shape")
        _identifier(branch["id"], "research branch id")
        if branch["id"] in branch_ids:
            raise ValidationError("research branch IDs must be unique")
        branch_ids.add(branch["id"])
        for key in ("title", "question", "hypothesis", "mechanism", "plan",
                    "research_form", "evidence_mode", "comparison_type", "kill_if"):
            _text(branch[key], f"research branch {key}")
        if branch["status"] not in BRANCH_STATUSES:
            raise ValidationError("research branch status is unsupported")
        if branch["status"] == "selected":
            selected.append(branch["id"])
        outcomes = branch["paper_if"]
        if (not isinstance(outcomes, list)
                or {item.get("id") for item in outcomes if isinstance(item, dict)} != PAPER_OUTCOME_IDS
                or len(outcomes) != len(PAPER_OUTCOME_IDS)):
            raise ValidationError(
                f"research branch {branch['id']} must define supportive, null_boundary, and ambiguous outcomes")
        outcome_ids = set()
        for outcome in outcomes:
            _outcome(outcome, branch["id"])
            if outcome["id"] in outcome_ids:
                raise ValidationError("paper outcome IDs must be unique within a branch")
            outcome_ids.add(outcome["id"])
        _strings(branch["evidence_obligations"], "research branch evidence_obligations",
                 minimum=2, maximum=12)
        _strings(branch["source_refs"], "research branch source_refs", maximum=16)
    if len(selected) != 1:
        raise ValidationError("research program must have exactly one selected branch")
    if value["selected_id"] not in branch_ids or value["selected_id"] != selected[0]:
        raise ValidationError("research program selected_id does not match its selected branch")

    decisions = value["decision_log"]
    if not isinstance(decisions, list) or not decisions:
        raise ValidationError("research program decision_log must be nonempty")
    decision_ids = set()
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != DECISION_FIELDS:
            raise ValidationError("research program decision_log item has an invalid shape")
        _identifier(decision["id"], "research decision id")
        if decision["id"] in decision_ids:
            raise ValidationError("research decision IDs must be unique")
        decision_ids.add(decision["id"])
        _text(decision["action"], "research decision action")
        _strings(decision["branch_ids"], "research decision branch_ids", minimum=1, maximum=8)
        if set(decision["branch_ids"]) - branch_ids:
            raise ValidationError("research decision references an unknown branch")
        _text(decision["rationale"], "research decision rationale")
    canonical_bytes(value)
    return value


def _branch_outcomes(candidate):
    """Return conditional outcomes shared by every candidate branch.

    These are decision rules, not claims about the eventual data.  Keeping
    them identical in shape makes each branch comparable without pretending
    that a model score can rank scientific importance before evidence exists.
    """
    return [
        {
            "id": "supportive",
            "condition": (
                f"For the question '{candidate['research_question']}', the planned contrast is stable within uncertainty, "
                "survives the declared controls and sensitivity checks, and separates the proposed mechanism from a live alternative."
            ),
            "contribution": (
                f"A bounded result about {candidate['title']} may support a paper argument inside the tested conditions."
            ),
            "required_evidence": [
                "condition-level observations with uncertainty",
                "control and sensitivity results",
                "an explicit comparison against the strongest alternative explanation",
            ],
        },
        {
            "id": "null_boundary",
            "condition": (
                f"For the question '{candidate['research_question']}', the contrast is reliably null or reaches a reproducible "
                "boundary, with enough control and sensitivity evidence to exclude a trivial measurement or power failure."
            ),
            "contribution": (
                f"A robust null or boundary can define where the proposed mechanism for {candidate['title']} does not operate "
                "and can support a paper if that boundary is theoretically or practically informative."
            ),
            "required_evidence": [
                "predeclared null or boundary criterion",
                "power, convergence, or sensitivity evidence",
                "a check that the result is not caused by a failed measurement",
            ],
        },
        {
            "id": "ambiguous",
            "condition": (
                f"For the question '{candidate['research_question']}', the result is mixed, underpowered, or not identifiable "
                "after the declared checks, so competing explanations cannot be separated."
            ),
            "contribution": (
                "Keep the branch unresolved and issue a targeted follow-up; ambiguity is not promoted into a mechanism claim."
            ),
            "required_evidence": [
                "the exact source of the ambiguity",
                "the missing measurement or control",
                "a discriminating follow-up with a falsifiable success condition",
            ],
        },
    ]


def build_research_program(topic_package):
    """Materialize a program from an already validated topic package.

    No new provider call is made here.  The candidate portfolio is copied
    into isolated branches and receives only generic, explicit decision rules;
    scientific details remain those supplied by the topic stage.
    """
    if not isinstance(topic_package, dict):
        raise ValidationError("topic package must be an object")
    package = {key: deepcopy(topic_package.get(key))
               for key in ("schema_version", "objective", "candidates", "selected_id", "selection_rationale")}
    if package.get("schema_version") != "topic-discovery-1":
        raise ValidationError("research program requires a topic-discovery-1 package")
    # Reuse the topic contract as the input gate, while allowing the caller to
    # pass the richer Composer result around it.  Runtime topic admission
    # validates the selected candidate's executable plan; retained branches
    # are conditional alternatives and may carry a stale or incomplete plan
    # from the model's portfolio response.  They are not dispatched by this
    # program, so re-validating their execution inventory here would turn an
    # admitted selected direction into a pre-specialist stage failure.
    from scisaurus.runtime.topic_discovery import validate_topic_package
    validation_package = deepcopy(package)
    for candidate in validation_package.get("candidates", []):
        if candidate.get("id") != validation_package.get("selected_id"):
            candidate.pop("feasibility_plan", None)
    validate_topic_package(validation_package, objective=validation_package.get("objective"))

    candidates = package["candidates"]
    selected_id = package["selected_id"]
    selected = next(item for item in candidates if item["id"] == selected_id)
    branches = []
    for candidate in candidates:
        mechanism = candidate.get("mechanism")
        if not isinstance(mechanism, str) or not mechanism.strip():
            mechanism = (
                "The mechanism remains an open part of this candidate; the survey must compare the stated explanation "
                "with plausible alternatives before it is interpreted."
            )
        hypothesis = (
            f"If the candidate mechanism is relevant, the declared comparison should produce a reproducible contrast within "
            f"the stated scope; the question is: {candidate['research_question']}"
        )
        plan = (
            f"Begin with the candidate's literature and feasibility checks, then run the bounded comparison described by "
            f"the scope and resource plan: {candidate['scope']} {candidate['resource_plan']}"
        )
        branches.append({
            "id": candidate["id"],
            "title": candidate["title"],
            "question": candidate["research_question"],
            "hypothesis": hypothesis,
            "mechanism": mechanism,
            "plan": plan,
            "research_form": candidate.get("research_form", "unspecified research form"),
            "evidence_mode": candidate.get("evidence_mode", "unspecified evidence mode"),
            "comparison_type": candidate.get("comparison_type", "unspecified comparison"),
            "paper_if": deepcopy(_branch_outcomes(candidate)),
            "kill_if": candidate["disconfirmation_test"],
            "evidence_obligations": [
                "A current literature survey must test the branch against its closest prior work.",
                "The experiment must retain observations, uncertainty, controls, and sensitivity evidence.",
                "The interpretation must state which alternatives remain unresolved.",
            ],
            "source_refs": list(candidate.get("prior_work_ids", [])),
            "status": "selected" if candidate["id"] == selected_id else "retained",
        })

    program = {
        "schema_version": SCHEMA_VERSION,
        "theme": selected["domain"],
        "objective": package["objective"],
        "branches": branches,
        "selected_id": selected_id,
        "selection_mode": "provisional",
        "selection_rationale": package["selection_rationale"],
        "selection_criteria": [
            "Use the candidate's bounded question, disconfirmation test, feasibility, and resource plan as the intake criteria.",
            "Prefer a branch whose proposed comparison can produce interpretable positive, null, or boundary outcomes.",
            "Treat the selected branch as provisional until the survey, experiment, interpretation, and adversarial review agree.",
        ],
        "decision_log": [
            {
                "id": "portfolio-retained",
                "action": "retain_alternatives",
                "branch_ids": [item["id"] for item in branches if item["id"] != selected_id],
                "rationale": "Every admitted candidate remains available for a scoped follow-up; unselected branches are not treated as disproven.",
            },
            {
                "id": "selection-provisional",
                "action": "route_selected_branch",
                "branch_ids": [selected_id],
                "rationale": "The topic stage's recorded selection rationale chooses the first survey route, but does not establish truth, novelty, or publication eligibility.",
            },
        ],
    }
    validate_research_program(program)
    return program


def project_research_program(program, *, max_alternatives=8):
    """Return a bounded downstream projection for specialist prompts."""
    validate_research_program(program)
    if type(max_alternatives) is not int or not 0 <= max_alternatives <= 8:
        raise ValidationError("max_alternatives must be between zero and eight")
    selected = next(item for item in program["branches"] if item["id"] == program["selected_id"])
    selected_projection = {key: deepcopy(selected[key]) for key in (
        "id", "title", "question", "hypothesis", "mechanism", "plan",
        "research_form", "evidence_mode", "comparison_type", "paper_if",
        "kill_if", "evidence_obligations", "status",
    )}
    alternatives = [
        {key: deepcopy(branch[key]) for key in ("id", "title", "question", "research_form",
                                                "evidence_mode", "comparison_type", "paper_if", "kill_if", "status")}
        for branch in program["branches"]
        if branch["id"] != program["selected_id"]
    ][:max_alternatives]
    return {
        "schema_version": SCHEMA_VERSION,
        "theme": program["theme"],
        "objective": program["objective"],
        "selection_mode": program["selection_mode"],
        "selected_branch": selected_projection,
        "retained_branches": alternatives,
        "selection_criteria": deepcopy(program["selection_criteria"]),
        "selection_rationale": program["selection_rationale"],
    }


__all__ = [
    "SCHEMA_VERSION", "PROGRAM_FIELDS", "BRANCH_FIELDS", "PAPER_OUTCOME_IDS",
    "build_research_program", "project_research_program", "validate_research_program",
]
