"""Project-scoped department state and autonomous work-order intake.

Departments are durable responsibility boundaries, not long-lived model
processes.  The module gives the Composer a real project organization: each
department has a charter, inbox, typed work orders, and a queryable backlog.
Templates seed safe defaults; the current mission and the department's
validated proposal contract decide what work is actually admitted.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
import time

from scisaurus.core.errors import NotFoundError, QuotaExceededError, StateError, ValidationError
from scisaurus.core.schema import canonical_bytes, now_iso


LEGACY_SCHEMA_VERSION = "project-organization-1"
SCHEMA_VERSION = "project-organization-2"
LEGACY_CHARTER_SCHEMA_VERSION = "department-charter-1"
CHARTER_SCHEMA_VERSION = "department-charter-2"
WORK_ORDER_SCHEMA_VERSION = "department-work-order-1"
_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")

ROLE_APPOINTMENTS = frozenset({"specialist", "reviewer", "verifier"})
ROLE_EXECUTION_KINDS = frozenset({"model", "review", "deterministic", "service"})
ROLE_ACTIVATIONS = frozenset({"on_demand"})
ROLE_QUOTA_FIELDS = frozenset({
    "max_calls", "max_input_tokens", "max_output_tokens", "max_seconds",
})
ROLE_FIELDS = frozenset({
    "id", "label", "appointment", "execution_kind", "model_role", "system_contract",
    "input_projection", "stage_kinds", "proposal_kinds", "capability_scope",
    "reviewer_role_id", "independent_review", "activation", "quota",
    "internal_role_aliases",
})

PROPOSAL_KINDS = frozenset({
    "topic_refinement", "literature_expansion", "full_text_retrieval", "additional_experiment",
    "analysis_display", "analysis_repair", "interpretation_expansion",
    "manuscript_revision", "capability_acquisition", "recovery",
})
STAGE_KINDS = frozenset({
    "topic_discovery", "survey", "experiment", "interpretation", "argument", "paper",
})
TASK_KIND_BY_PROPOSAL = {
    "topic_refinement": "production",
    "literature_expansion": "retrieval",
    "full_text_retrieval": "retrieval",
    "additional_experiment": "production",
    "analysis_display": "production",
    "analysis_repair": "production",
    "interpretation_expansion": "production",
    "manuscript_revision": "production",
    "capability_acquisition": "service",
    "recovery": "response",
}
REQUEST_STAGE_KINDS = {
    "topic_refinement": "topic_discovery",
    "literature_expansion": "survey",
    "full_text_retrieval": "survey",
    "additional_experiment": "experiment",
    "analysis_display": "experiment",
    "analysis_repair": "experiment",
    "interpretation_expansion": "interpretation",
    "manuscript_revision": "paper",
}
# A stage's functional role is not a model process and is not the concrete
# chief appointment in a project.  Keeping this routing table beside the
# department charters prevents the Composer and the organization runtime from
# silently developing different ownership maps.
DEFAULT_STAGE_ROUTES = (
    {
        "stage_kind": "topic_discovery", "department": "research", "functional_role": "intelligence",
        "required_role_ids": ["frontier-scout", "search-strategist", "academic-scout", "topic-maturity-reviewer"],
        "verifier_role_id": "adversary", "max_active_agents": 4,
    },
    {
        "stage_kind": "survey", "department": "research", "functional_role": "intelligence",
        "required_role_ids": ["search-strategist", "academic-scout", "source-acquirer", "citation-mapper", "cataloger", "fact-verifier"],
        "verifier_role_id": "adversary", "max_active_agents": 6,
    },
    {
        "stage_kind": "experiment", "department": "methods", "functional_role": "validation",
        "required_role_ids": ["methodologist", "statistical-reviewer", "reproducibility-reviewer", "analysis-reviewer"],
        "verifier_role_id": "adversary", "max_active_agents": 4,
    },
    {
        "stage_kind": "interpretation", "department": "strategy", "functional_role": "interpretation",
        "required_role_ids": ["mechanism-interpreter", "alternative-hypothesis-challenger", "evidence-linker"],
        "verifier_role_id": "adversary", "max_active_agents": 3,
    },
    {
        "stage_kind": "argument", "department": "strategy", "functional_role": "argument",
        "required_role_ids": ["planner", "narrative-architect", "evidence-linker", "section-writer"],
        "verifier_role_id": "adversary", "max_active_agents": 4,
    },
    {
        "stage_kind": "paper", "department": "editorial", "functional_role": "composer",
        "required_role_ids": ["writer", "structural-editor", "format-editor", "consistency-qa", "journal-editor", "ai-surface-reviewer"],
        "verifier_role_id": "adversary", "max_active_agents": 6,
    },
)
COMMAND_ADDRESSES = {
    "arbiter": {"dept": "executive-command", "agent": "arbiter"},
    "progress": {"dept": "executive-command", "agent": "progress-controller"},
    "intent": {"dept": "executive-command", "agent": "intent-keeper"},
}
VOLATILE_FIELDS = frozenset({
    "created_at", "received_at", "updated_at", "source_event_id", "source_note_ref",
    "source_stage_id",
})
ASSIGNMENT_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "stale", "rejected"})
ASSIGNMENT_ACTIVE_STATES = frozenset({"queued", "running", "awaiting_review"})
TASK_KIND_BY_EXECUTION = {
    "model": "production", "review": "review", "deterministic": "verification", "service": "service",
}


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a nonempty string")
    return value.strip()


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValidationError(f"{name} must be a bounded lowercase identifier")
    return value


def _strings(value, name, *, empty=False):
    if not isinstance(value, list) or (not empty and not value):
        raise ValidationError(f"{name} must be an explicit string list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValidationError(f"{name} must contain nonempty strings")
    if len(value) != len(set(value)):
        raise ValidationError(f"{name} cannot contain duplicates")


def _exact(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValidationError(f"{name} requires exactly {sorted(fields)}")


def _positive_number(value, name):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or value <= 0):
        raise ValidationError(f"{name} must be finite and positive")


def _validate_role_quota(value, name="role quota"):
    _exact(value, ROLE_QUOTA_FIELDS, name)
    if type(value["max_calls"]) is not int or not 1 <= value["max_calls"] <= 16:
        raise ValidationError(f"{name} max_calls must be between one and sixteen")
    for field in ("max_input_tokens", "max_output_tokens"):
        if type(value[field]) is not int or value[field] < 1:
            raise ValidationError(f"{name} {field} must be a positive integer")
    _positive_number(value["max_seconds"], f"{name} max_seconds")
    return deepcopy(value)


def _role(
    role_id, label, *, model_role, execution_kind, stage_kinds, proposal_kinds,
    capability_scope, contract, projection, aliases=(), reviewer_role_id="adversary",
    quota=None,
):
    return {
        "id": role_id,
        "label": label,
        "appointment": "specialist",
        "execution_kind": execution_kind,
        "model_role": model_role,
        "system_contract": contract,
        "input_projection": list(projection),
        "stage_kinds": list(stage_kinds),
        "proposal_kinds": list(proposal_kinds),
        "capability_scope": list(capability_scope),
        "reviewer_role_id": reviewer_role_id,
        "independent_review": True,
        "activation": "on_demand",
        "quota": quota or {
            "max_calls": 1, "max_input_tokens": 12000,
            "max_output_tokens": 4000, "max_seconds": 900,
        },
        "internal_role_aliases": list(aliases),
    }


_RESEARCH_STAGES = ["topic_discovery", "survey"]
_RESEARCH_PROPOSALS = [
    "topic_refinement", "literature_expansion", "full_text_retrieval",
    "capability_acquisition", "recovery",
]
_METHODS_STAGES = ["experiment"]
_METHODS_PROPOSALS = [
    "additional_experiment", "analysis_display", "analysis_repair",
    "capability_acquisition", "recovery",
]
_STRATEGY_STAGES = ["interpretation", "argument"]
_STRATEGY_PROPOSALS = ["interpretation_expansion", "analysis_display", "recovery"]
_EDITORIAL_STAGES = ["paper"]
_EDITORIAL_PROPOSALS = [
    "manuscript_revision", "literature_expansion", "analysis_display", "recovery",
]
_OPERATIONS_PROPOSALS = ["capability_acquisition", "recovery"]


DEFAULT_AGENT_ROLES = {
    "research": [
        _role("frontier-scout", "Frontier scout", model_role="research.frontier-seed-planner", execution_kind="model",
              stage_kinds=["topic_discovery"], proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "scholarly_search"], contract="Generate diverse frontier directions anchored to supplied scholarly seeds; do not choose a topic from a template.",
              projection=["objective", "frontier_seeds", "scholarly_records", "topic_history"], aliases=["research.frontier-seed-planner"]),
        _role("search-strategist", "Search strategist", model_role="research.search-planner", execution_kind="model",
              stage_kinds=_RESEARCH_STAGES, proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "scholarly_search"], contract="Design independent terminology families and bounded searches that test the declared question.",
              projection=["objective", "topic", "known_gaps", "source_classes"], aliases=["research.search-planner"]),
        _role("academic-scout", "Academic scout", model_role="research.seed-reader", execution_kind="model",
              stage_kinds=_RESEARCH_STAGES, proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "scholarly_search"], contract="Find and compare scholarly anchors without promoting metadata into verified evidence.",
              projection=["topic", "frontier_seeds", "search_results"], aliases=["research.seed-reader"]),
        _role("open-web-scout", "Open-web scout", model_role="research.open-web-scout", execution_kind="model",
              stage_kinds=_RESEARCH_STAGES, proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "web"], contract="Use authorized open-web leads only as discoverable candidates and preserve source identity.",
              projection=["topic", "search_terms", "source_policy"], aliases=["research.open-web-scout"]),
        _role("technical-ecosystem-scout", "Technical ecosystem scout", model_role="research.technical-ecosystem-scout", execution_kind="model",
              stage_kinds=["topic_discovery", "survey"], proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "web", "mcp"], contract="Map relevant code, datasets, standards, and operational capabilities to the scientific question.",
              projection=["topic", "candidate_methods", "capability_inventory"], aliases=["research.technical-ecosystem-scout"]),
        _role("standards-patent-scout", "Standards and patent scout", model_role="research.standards-patent-scout", execution_kind="model",
              stage_kinds=["topic_discovery", "survey"], proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "web"], contract="Check standards, patents, and non-journal prior art for novelty constraints and terminology.",
              projection=["topic", "claims", "source_classes"], aliases=["research.standards-patent-scout"]),
        _role("genealogy-trend-analyst", "Genealogy and trend analyst", model_role="research.genealogy-trend-analyst", execution_kind="model",
              stage_kinds=["topic_discovery", "survey"], proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "scholarly_search"], contract="Trace how a claim, method, or term evolved and identify neglected branches rather than popularity alone.",
              projection=["topic", "citation_graph", "search_results"], aliases=["research.genealogy-trend-analyst"]),
        _role("source-acquirer", "Source acquirer", model_role="research.source-acquirer", execution_kind="service",
              stage_kinds=["survey"], proposal_kinds=["full_text_retrieval", "literature_expansion", "recovery"],
              capability_scope=["source_fetch", "api", "mcp"], contract="Acquire point-in-time source material with identity, transport, and incomplete-capture state recorded.",
              projection=["topic", "source_candidates", "retrieval_policy"], aliases=["research.source-acquirer"]),
        _role("citation-mapper", "Citation mapper", model_role="research.citation-tracer", execution_kind="model",
              stage_kinds=["survey"], proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "citation_graph"], contract="Map claims to cited works and expose missing, indirect, or contradictory support.",
              projection=["source_records", "claims", "citation_graph"], aliases=["research.citation-tracer"]),
        _role("cataloger", "Evidence cataloger", model_role="research.literature-mapper", execution_kind="model",
              stage_kinds=["survey"], proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "scholarly_search"], contract="Normalize source identity, evidence representation, and coverage without flattening uncertainty.",
              projection=["source_records", "source_identity", "coverage_requirements"], aliases=["research.literature-mapper"]),
        _role("fact-verifier", "Fact verifier", model_role="research.literature-reviewer", execution_kind="review",
              stage_kinds=["survey"], proposal_kinds=_RESEARCH_PROPOSALS,
              capability_scope=["model", "source_fetch"], contract="Verify decisive factual statements against inspectable source locations and label unknowns explicitly.",
              projection=["claims", "source_records", "evidence_records"], aliases=["research.literature-reviewer"]),
        _role("topic-maturity-reviewer", "Topic maturity reviewer", model_role="research.topic-maturity-reviewer", execution_kind="review",
              stage_kinds=["topic_discovery"], proposal_kinds=["topic_refinement", "recovery"],
              capability_scope=["model", "scholarly_search"], contract="Reject generic or template-derived topics and demand a falsifiable, source-grounded research gap.",
              projection=["candidate_topics", "frontier_seeds", "prior_work", "experiment_feasibility"], aliases=["research.topic-maturity-reviewer"]),
    ],
    "methods": [
        _role("methodologist", "Methodologist", model_role="methods.methodologist", execution_kind="model",
              stage_kinds=_METHODS_STAGES, proposal_kinds=_METHODS_PROPOSALS,
              capability_scope=["model", "python", "statistics"], contract="Translate the question into a falsifiable design with controls, estimands, and stopping rules.",
              projection=["research_question", "hypotheses", "method_constraints", "available_assets"], aliases=["methods.methodologist", "research.experiment-author"]),
        _role("statistical-reviewer", "Statistical reviewer", model_role="review.methods", execution_kind="review",
              stage_kinds=_METHODS_STAGES, proposal_kinds=_METHODS_PROPOSALS,
              capability_scope=["model", "statistics"], contract="Stress-test estimands, uncertainty, multiplicity, sensitivity, and interpretation of computed results.",
              projection=["design", "raw_results", "analysis_plan", "claims"], aliases=["review.methods"]),
        _role("reproducibility-reviewer", "Reproducibility reviewer", model_role="methods.reproducibility-reviewer", execution_kind="review",
              stage_kinds=_METHODS_STAGES, proposal_kinds=_METHODS_PROPOSALS,
              capability_scope=["model", "python", "local_program"], contract="Re-run or independently recalculate the declared result from the recorded inputs and code path.",
              projection=["execution_manifest", "input_digests", "raw_results", "analysis_code"], aliases=["methods.reproducibility-reviewer"]),
        _role("analysis-reviewer", "Analysis reviewer", model_role="methods.analysis-reviewer", execution_kind="review",
              stage_kinds=_METHODS_STAGES, proposal_kinds=_METHODS_PROPOSALS,
              capability_scope=["model", "statistics", "python"], contract="Check that displays and derived quantities answer the question without leakage, unsupported transformations, or hidden exclusions.",
              projection=["analysis_plan", "derived_results", "figures", "claims"], aliases=["methods.analysis-reviewer"]),
        _role("control-designer", "Control designer", model_role="methods.control-designer", execution_kind="model",
              stage_kinds=_METHODS_STAGES, proposal_kinds=_METHODS_PROPOSALS,
              capability_scope=["model", "python"], contract="Design discriminating controls and alternative explanations before accepting a positive result.",
              projection=["hypotheses", "candidate_mechanisms", "design", "known_failure_modes"], aliases=["methods.control-designer"]),
    ],
    "strategy": [
        _role("mechanism-interpreter", "Mechanism interpreter", model_role="strategy.interpretation", execution_kind="model",
              stage_kinds=["interpretation"], proposal_kinds=_STRATEGY_PROPOSALS,
              capability_scope=["model", "analysis"], contract="Interpret results through explicit mechanisms while separating observation, inference, and speculation.",
              projection=["research_question", "results", "evidence", "alternative_hypotheses"], aliases=["strategy.interpretation"]),
        _role("planner", "Argument planner", model_role="strategy.argument", execution_kind="model",
              stage_kinds=["argument"], proposal_kinds=_STRATEGY_PROPOSALS,
              capability_scope=["model", "analysis"], contract="Plan a claim-evidence chain whose strongest conclusion is no stronger than its weakest support.",
              projection=["claims", "results", "interpretation", "review_findings"], aliases=["strategy.argument"]),
        _role("narrative-architect", "Narrative architect", model_role="strategy.argument", execution_kind="model",
              stage_kinds=["argument"], proposal_kinds=_STRATEGY_PROPOSALS,
              capability_scope=["model"], contract="Construct a coherent scientific narrative that preserves competing explanations and boundary conditions.",
              projection=["argument_plan", "mechanism", "limitations", "target_audience"], aliases=["strategy.narrative-architect"]),
        _role("evidence-linker", "Evidence linker", model_role="strategy.argument-reviewer", execution_kind="review",
              stage_kinds=_STRATEGY_STAGES, proposal_kinds=_STRATEGY_PROPOSALS,
              capability_scope=["model", "analysis"], contract="Audit every material claim for a direct, current, and correctly scoped evidence link.",
              projection=["claims", "evidence_records", "interpretation", "argument_plan"], aliases=["strategy.argument-reviewer"]),
        _role("section-writer", "Section writer", model_role="strategy.section-writer", execution_kind="model",
              stage_kinds=["argument"], proposal_kinds=_STRATEGY_PROPOSALS,
              capability_scope=["model"], contract="Draft bounded sections from the accepted claim-evidence graph without inventing citations or results.",
              projection=["section_contract", "claims", "evidence_records", "style_constraints"], aliases=["strategy.section-writer"]),
        _role("alternative-hypothesis-challenger", "Alternative-hypothesis challenger", model_role="review.human_scientist", execution_kind="review",
              stage_kinds=["interpretation"], proposal_kinds=_STRATEGY_PROPOSALS,
              capability_scope=["model", "analysis"], contract="Construct plausible alternatives and identify observations or controls that would distinguish them.",
              projection=["results", "mechanism", "alternative_hypotheses", "limitations"], aliases=["review.human_scientist"]),
    ],
    "editorial": [
        _role("writer", "Scientific writer", model_role="editorial.writer", execution_kind="model",
              stage_kinds=_EDITORIAL_STAGES, proposal_kinds=_EDITORIAL_PROPOSALS,
              capability_scope=["model", "document"], contract="Compose only from accepted evidence and preserve uncertainty, provenance, and declared scope.",
              projection=["argument", "evidence", "paper_contract", "style_constraints"], aliases=["editorial.writer", "scientific-author"]),
        _role("structural-editor", "Structural editor", model_role="review.science", execution_kind="review",
              stage_kinds=_EDITORIAL_STAGES, proposal_kinds=_EDITORIAL_PROPOSALS,
              capability_scope=["model", "document"], contract="Audit logical order, claim support, section transitions, and whether the manuscript answers its question.",
              projection=["draft", "argument", "review_package", "evidence_map"], aliases=["review.science"]),
        _role("format-editor", "Format editor", model_role=None, execution_kind="deterministic",
              stage_kinds=_EDITORIAL_STAGES, proposal_kinds=_EDITORIAL_PROPOSALS,
              capability_scope=["latex", "pdf", "document"], contract="Check deterministic format, references, figures, tables, and renderability against the declared journal contract.",
              projection=["manuscript_source", "bibliography", "figure_manifest", "journal_contract"], aliases=["editorial.format-editor"]),
        _role("consistency-qa", "Consistency QA", model_role=None, execution_kind="deterministic",
              stage_kinds=_EDITORIAL_STAGES, proposal_kinds=_EDITORIAL_PROPOSALS,
              capability_scope=["document", "pdf"], contract="Compare terminology, numbers, citations, captions, and cross-references across all assembled artifacts.",
              projection=["manuscript", "results", "figures", "references"], aliases=["editorial.consistency-qa"]),
        _role("journal-editor", "Journal editor", model_role="review.journal_editor", execution_kind="review",
              stage_kinds=_EDITORIAL_STAGES, proposal_kinds=_EDITORIAL_PROPOSALS,
              capability_scope=["model", "document", "pdf"], contract="Apply an independent publication-level standard for novelty, evidence depth, presentation, and likely rejection reasons.",
              projection=["draft", "review_package", "journal_contract", "source_coverage"], aliases=["review.journal_editor"]),
        _role("ai-surface-reviewer", "AI-surface reviewer", model_role="review.ai_smell", execution_kind="review",
              stage_kinds=_EDITORIAL_STAGES, proposal_kinds=_EDITORIAL_PROPOSALS,
              capability_scope=["model", "document"], contract="Find unsupported generic prose, citation-shaped assertions, and machine-like repetition that weaken scientific trust.",
              projection=["draft", "claims", "citations", "style_constraints"], aliases=["review.ai_smell"]),
        _role("citation-style-editor", "Citation style editor", model_role="editorial.surgical-editor", execution_kind="review",
              stage_kinds=_EDITORIAL_STAGES, proposal_kinds=_EDITORIAL_PROPOSALS,
              capability_scope=["model", "document"], contract="Check citation placement and style without changing scientific meaning or source identity.",
              projection=["draft", "references", "journal_contract"], aliases=["editorial.surgical-editor"]),
    ],
    "operations": [
        _role("tool-environment-engineer", "Tool and environment engineer", model_role="operations.tool-engineer", execution_kind="service",
              stage_kinds=[], proposal_kinds=_OPERATIONS_PROPOSALS,
              capability_scope=["local_program", "mcp", "api", "environment"], contract="Prepare or repair the smallest project-scoped capability and record versions, permissions, and probes.",
              projection=["capability_request", "environment", "tool_contract", "failure"], aliases=["operations.engineer", "operations.tool-engineer"]),
        _role("execution-operator", "Execution operator", model_role=None, execution_kind="deterministic",
              stage_kinds=[], proposal_kinds=_OPERATIONS_PROPOSALS,
              capability_scope=["local_program", "mcp", "api", "environment"], contract="Run an authorized capability on declared project inputs and preserve exact execution evidence.",
              projection=["capability", "arguments", "input_digests", "execution_policy"], aliases=["operations.operator"]),
        _role("operational-verifier", "Operational verifier", model_role=None, execution_kind="deterministic",
              stage_kinds=[], proposal_kinds=_OPERATIONS_PROPOSALS,
              capability_scope=["local_program", "mcp", "api", "environment"], contract="Independently probe readiness, failure modes, and reproducibility before a consumer relies on the capability.",
              projection=["capability_profile", "probe_plan", "probe_result", "environment"], aliases=["operations.verifier", "operations.operational-verifier"]),
        _role("runtime-auditor", "Runtime auditor", model_role=None, execution_kind="deterministic",
              stage_kinds=[], proposal_kinds=_OPERATIONS_PROPOSALS,
              capability_scope=["environment", "local_program"], contract="Audit process identity, working directory, leases, checkpoints, and artifact completeness for a running mission.",
              projection=["process", "checkpoint", "task_ledger", "artifact_manifest"], aliases=["operations.runtime-auditor"]),
    ],
}


def default_stage_routes():
    """Return the immutable functional stage map as fresh plain data."""
    return deepcopy(list(DEFAULT_STAGE_ROUTES))


def stage_role(stage_kind):
    """Return the stable functional role used in task and feedback records."""
    _identifier(stage_kind, "stage kind")
    for route in DEFAULT_STAGE_ROUTES:
        if route["stage_kind"] == stage_kind:
            return f"{route['department']}.{route['functional_role']}"
    raise ValidationError(f"unsupported stage kind: {stage_kind}")


def stage_route(stage_kind, charters=None):
    """Resolve one functional route plus its project-specific appointments."""
    _identifier(stage_kind, "stage kind")
    route = next((item for item in DEFAULT_STAGE_ROUTES
                  if item["stage_kind"] == stage_kind), None)
    if route is None:
        raise ValidationError(f"unsupported stage kind: {stage_kind}")
    charter = (charters or {}).get(route["department"])
    if charter is None:
        raise ValidationError(
            f"stage {stage_kind} has no owning department: {route['department']}")
    role_by_id = {item["id"]: item for item in charter.get("agent_roles", [])}
    required_roles = []
    for requested_id in route["required_role_ids"]:
        role = role_by_id.get(requested_id)
        if role is None:
            # v1 custom charters are migrated before reaching the runtime.  A
            # collision with a preserved chief/adversary name can still cause
            # the migration helper to suffix one specialist; resolve that
            # deterministic suffix without weakening v2 validation.
            role = role_by_id.get(f"{requested_id}-specialist")
        if role is None:
            raise ValidationError(
                f"department {route['department']} is missing specialist role: {requested_id}")
        required_roles.append(role)
    required_agents = [f"{route['department']}.{item['id']}" for item in required_roles]
    role_quotas = {
        agent: deepcopy(item["quota"])
        for agent, item in zip(required_agents, required_roles)
    }
    verifier_agent = f"{route['department']}.{charter['adversary']}"
    return {
        **deepcopy(route),
        "role": f"{route['department']}.{route['functional_role']}",
        "chief": charter["chief"],
        "adversary": charter["adversary"],
        "owner_address": {"dept": route["department"], "agent": charter["chief"]},
        "review_address": {"dept": route["department"], "agent": charter["adversary"]},
        "required_role_ids": [item["id"] for item in required_roles],
        "required_agents": required_agents,
        "required_roles": deepcopy(required_roles),
        "verifier_agent": verifier_agent,
        "role_quotas": role_quotas,
    }


def agent_roster(charters):
    """Project the eligible role pool from validated project charters.

    The roster is an eligibility manifest, not a claim that every role is a
    resident model process.  Actual invocation evidence is emitted by
    :meth:`DepartmentRuntime.begin_stage` and lives in the assignment tasks.
    """
    if not isinstance(charters, dict):
        raise ValidationError("agent roster requires department charters")
    roster = []
    for department in sorted(charters):
        charter = charters[department]
        reviewer_agent = f"{department}.{charter['adversary']}"
        roster.append({
            "id": f"{department}.{charter['chief']}",
            "department": department,
            "agent": charter["chief"],
            "role_id": charter["chief"],
            "label": f"{charter['label']} chief",
            "appointment": "chief",
            "execution_kind": "model",
            "model_role": "department.chief",
            "system_contract": "Synthesize the department's scoped work and admit only evidence that passed its assigned checks.",
            "input_projection": ["objective", "stage_packet", "assignment_results", "review_verdict"],
            "independent_review": False,
            "reviewer_role_id": "adversary",
            "reviewer_agent": reviewer_agent,
            "activation": "on_demand",
            "quota": {"max_calls": 1, "max_input_tokens": 16000,
                      "max_output_tokens": 6000, "max_seconds": 900},
            "internal_role_aliases": [],
            "stage_kinds": list(charter["stage_kinds"]),
            "proposal_kinds": list(charter["proposal_kinds"]),
            "capability_scope": list(charter["capability_scope"]),
        })
        roster.append({
            "id": reviewer_agent,
            "department": department,
            "agent": charter["adversary"],
            "role_id": charter["adversary"],
            "label": f"{charter['label']} adversary",
            "appointment": "adversary",
            "execution_kind": "review",
            "model_role": "review.arbiter",
            "system_contract": "Independently challenge the department result and report evidence, uncertainty, and repair scope.",
            "input_projection": ["objective", "stage_packet", "assignment_results", "producer_claims"],
            "independent_review": True,
            "reviewer_role_id": None,
            "reviewer_agent": None,
            "activation": "on_demand",
            # Reserve one bounded repair/fallback call for malformed model
            # output; a verifier still has only one accepted verdict.
            "quota": {"max_calls": 2, "max_input_tokens": 16000,
                      "max_output_tokens": 6000, "max_seconds": 900},
            "internal_role_aliases": [],
            "stage_kinds": list(charter["stage_kinds"]),
            "proposal_kinds": list(charter["proposal_kinds"]),
            "capability_scope": list(charter["capability_scope"]),
        })
        for role in charter.get("agent_roles", []):
            reviewer_id = role.get("reviewer_role_id")
            reviewer_agent = None
            if reviewer_id == "adversary":
                reviewer_agent = f"{department}.{charter['adversary']}"
            elif isinstance(reviewer_id, str):
                reviewer_agent = f"{department}.{reviewer_id}"
            roster.append({
                **deepcopy(role),
                "id": f"{department}.{role['id']}",
                "department": department,
                "agent": role["id"],
                "role_id": role["id"],
                "reviewer_agent": reviewer_agent,
            })
    return roster


DEFAULT_DEPARTMENTS = [
    {
        "id": "research",
        "label": "Research and evidence",
        "chief": "chief",
        "adversary": "adversarial-reviewer",
        "subscriptions": ["research_expansion_required", "source_update", "contradiction", "provider_gap"],
        "proposal_kinds": ["topic_refinement", "literature_expansion", "full_text_retrieval", "capability_acquisition", "recovery"],
        "stage_kinds": ["topic_discovery", "survey"],
        "capability_scope": ["scholarly_search", "source_fetch", "citation_graph", "mcp"],
    },
    {
        "id": "methods",
        "label": "Methods and validation",
        "chief": "chief",
        "adversary": "adversarial-reviewer",
        "subscriptions": ["research_expansion_required", "method_objection", "analysis_failure", "provider_gap"],
        "proposal_kinds": [
            "additional_experiment", "analysis_display", "analysis_repair",
            "capability_acquisition", "recovery",
        ],
        "stage_kinds": ["experiment"],
        "capability_scope": ["local_program", "python", "statistics", "mcp"],
    },
    {
        "id": "strategy",
        "label": "Interpretation and argument",
        "chief": "chief",
        "adversary": "adversarial-reviewer",
        "subscriptions": ["interpretation_request", "argument_objection", "contradiction", "provider_gap"],
        "proposal_kinds": ["interpretation_expansion", "analysis_display", "recovery"],
        "stage_kinds": ["interpretation", "argument"],
        "capability_scope": ["model", "analysis", "mcp"],
    },
    {
        "id": "editorial",
        "label": "Composition and publication",
        "chief": "editor-in-chief",
        "adversary": "human-scientist-reviewer",
        "subscriptions": ["review_objection", "editorial_rejection", "rendering_failure", "provider_gap"],
        "proposal_kinds": ["manuscript_revision", "literature_expansion", "analysis_display", "recovery"],
        "stage_kinds": ["paper"],
        "capability_scope": ["latex", "pdf", "document", "mcp"],
    },
    {
        "id": "operations",
        "label": "Tools and execution environments",
        "chief": "coordinator",
        "adversary": "operational-adversary",
        "subscriptions": ["capability_gap", "tool_failure", "schema_drift", "provider_gap"],
        "proposal_kinds": ["capability_acquisition", "recovery"],
        "stage_kinds": [],
        "capability_scope": ["local_program", "mcp", "api", "environment"],
    },
]


def _default_roles_for_department(department, *, chief=None, adversary=None):
    """Expand a v1 charter without changing its named appointments."""
    roles = deepcopy(DEFAULT_AGENT_ROLES.get(department, []))
    reserved = {item for item in (chief, adversary) if isinstance(item, str)}
    for role in roles:
        if role["id"] in reserved:
            role["id"] = f"{role['id']}-specialist"
    return roles


for _charter in DEFAULT_DEPARTMENTS:
    _charter["agent_roles"] = _default_roles_for_department(
        _charter["id"], chief=_charter["chief"], adversary=_charter["adversary"])


def default_organization():
    return {
        "schema_version": SCHEMA_VERSION,
        "template": "research-project-v2",
        "departments": deepcopy(DEFAULT_DEPARTMENTS),
        "allow_dynamic_proposals": True,
        "max_open_work_orders": 128,
    }


def validate_charter(value):
    base_fields = {
        "id", "label", "chief", "adversary", "subscriptions", "proposal_kinds",
        "stage_kinds", "capability_scope",
    }
    if not isinstance(value, dict) or set(value) not in (base_fields, base_fields | {"agent_roles"}):
        raise ValidationError(
            f"department charter requires exactly {sorted(base_fields)} and optional agent_roles")
    _identifier(value["id"], "department id")
    _text(value["label"], "department label")
    _identifier(value["chief"], "department chief")
    _identifier(value["adversary"], "department adversary")
    if value["chief"] == value["adversary"]:
        raise ValidationError("department chief and adversary must be distinct appointments")
    _strings(value["subscriptions"], "department subscriptions")
    _strings(value["proposal_kinds"], "department proposal_kinds")
    if set(value["proposal_kinds"]) - PROPOSAL_KINDS:
        raise ValidationError("department proposal_kinds contains an unsupported work-order kind")
    _strings(value["stage_kinds"], "department stage_kinds", empty=True)
    if set(value["stage_kinds"]) - STAGE_KINDS:
        raise ValidationError("department stage_kinds contains an unsupported stage")
    _strings(value["capability_scope"], "department capability_scope", empty=True)
    raw_roles = value.get("agent_roles")
    migrated = raw_roles is None
    roles = (_default_roles_for_department(value["id"], chief=value["chief"], adversary=value["adversary"])
             if migrated else raw_roles)
    if not isinstance(roles, list) or not roles:
        raise ValidationError("department agent_roles must be a nonempty list")
    role_ids = set()
    aliases = set()
    validated_roles = []
    for role in roles:
        _exact(role, ROLE_FIELDS, "department agent role")
        role = deepcopy(role)
        _identifier(role["id"], "agent role id")
        if role["id"] in {value["chief"], value["adversary"]}:
            raise ValidationError("department specialist role cannot reuse chief or adversary appointment")
        if role["id"] in role_ids:
            raise ValidationError("department agent role IDs must be unique")
        role_ids.add(role["id"])
        _text(role["label"], "agent role label")
        if role["appointment"] not in ROLE_APPOINTMENTS:
            raise ValidationError("agent role appointment is unsupported")
        if role["execution_kind"] not in ROLE_EXECUTION_KINDS:
            raise ValidationError("agent role execution_kind is unsupported")
        if role["model_role"] is not None:
            _text(role["model_role"], "agent role model_role")
        _text(role["system_contract"], "agent role system_contract")
        _strings(role["input_projection"], "agent role input_projection", empty=True)
        _strings(role["stage_kinds"], "agent role stage_kinds", empty=True)
        if set(role["stage_kinds"]) - STAGE_KINDS:
            raise ValidationError("agent role stage_kinds contains an unsupported stage")
        _strings(role["proposal_kinds"], "agent role proposal_kinds", empty=True)
        if set(role["proposal_kinds"]) - PROPOSAL_KINDS:
            raise ValidationError("agent role proposal_kinds contains an unsupported work-order kind")
        _strings(role["capability_scope"], "agent role capability_scope", empty=True)
        reviewer = role["reviewer_role_id"]
        if reviewer != "adversary" and reviewer not in role_ids:
            # A forward reference to another specialist is valid, so defer the
            # full reference check until all IDs have been collected below.
            if not isinstance(reviewer, str) or not reviewer:
                raise ValidationError("agent role reviewer_role_id is invalid")
        if reviewer == role["id"]:
            raise ValidationError("agent role cannot review itself")
        if type(role["independent_review"]) is not bool:
            raise ValidationError("agent role independent_review must be Boolean")
        if role["independent_review"] is False and role["appointment"] != "specialist":
            raise ValidationError("reviewer and verifier roles require independent_review")
        if role["activation"] not in ROLE_ACTIVATIONS:
            raise ValidationError("agent role activation is unsupported")
        quota = role["quota"]
        _exact(quota, ROLE_QUOTA_FIELDS, "agent role quota")
        if type(quota["max_calls"]) is not int or not 1 <= quota["max_calls"] <= 16:
            raise ValidationError("agent role quota max_calls must be between one and sixteen")
        for field in ("max_input_tokens", "max_output_tokens"):
            if type(quota[field]) is not int or quota[field] < 1:
                raise ValidationError(f"agent role quota {field} must be a positive integer")
        _positive_number(quota["max_seconds"], f"agent role quota max_seconds")
        _strings(role["internal_role_aliases"], "agent role internal_role_aliases", empty=True)
        for alias in role["internal_role_aliases"]:
            if alias in aliases:
                raise ValidationError("agent role internal aliases must be unique")
            aliases.add(alias)
        validated_roles.append(role)
    for role in validated_roles:
        reviewer = role["reviewer_role_id"]
        if reviewer != "adversary" and reviewer not in role_ids:
            raise ValidationError(f"agent role reviewer_role_id is unknown: {reviewer}")
    return {**deepcopy(value), "agent_roles": validated_roles}


def validate_organization(value):
    fields = {"schema_version", "template", "departments", "allow_dynamic_proposals", "max_open_work_orders"}
    _exact(value, fields, "project organization")
    if value["schema_version"] not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
        raise ValidationError(
            f"project organization schema must be {LEGACY_SCHEMA_VERSION} or {SCHEMA_VERSION}")
    _text(value["template"], "organization template")
    departments = value["departments"]
    if not isinstance(departments, list) or not departments:
        raise ValidationError("project organization requires at least one department")
    validated = [validate_charter(item) for item in departments]
    ids = [item["id"] for item in validated]
    if len(ids) != len(set(ids)):
        raise ValidationError("project department IDs must be unique")
    if type(value["allow_dynamic_proposals"]) is not bool:
        raise ValidationError("allow_dynamic_proposals must be Boolean")
    if type(value["max_open_work_orders"]) is not int or not 1 <= value["max_open_work_orders"] <= 10000:
        raise ValidationError("max_open_work_orders must be between one and 10000")
    by_id = {item["id"]: item for item in validated}
    for route in DEFAULT_STAGE_ROUTES:
        charter = by_id.get(route["department"])
        if charter is None:
            continue
        by_role_id = {item["id"]: item for item in charter["agent_roles"]}
        resolved_roles = []
        missing = []
        for role_id in route["required_role_ids"]:
            role = by_role_id.get(role_id) or by_role_id.get(f"{role_id}-specialist")
            if role is None:
                missing.append(role_id)
            else:
                resolved_roles.append((role_id, role))
        if missing:
            raise ValidationError(
                f"department {route['department']} is missing required specialist roles: {', '.join(missing)}")
        for requested_id, role in resolved_roles:
            if role["appointment"] != "specialist":
                raise ValidationError(
                    f"stage {route['stage_kind']} role {requested_id} must be a specialist appointment")
            if route["stage_kind"] not in role["stage_kinds"]:
                raise ValidationError(
                    f"stage {route['stage_kind']} role {requested_id} is not scoped to that stage")
    return {
        **deepcopy(value),
        "schema_version": SCHEMA_VERSION,
        "template": value["template"] if value["schema_version"] == SCHEMA_VERSION else "research-project-v2",
        "departments": validated,
    }


def validate_work_order(value):
    fields = {"schema_version", "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed"}
    _exact(value, fields, "department work order")
    if value["schema_version"] != WORK_ORDER_SCHEMA_VERSION:
        raise ValidationError(f"work-order schema must be {WORK_ORDER_SCHEMA_VERSION}")
    _identifier(value["id"], "work-order id")
    _identifier(value["kind"], "work-order kind")
    if value["kind"] not in PROPOSAL_KINDS:
        raise ValidationError("unsupported work-order kind")
    _text(value["owner"], "work-order owner")
    for key in ("objective", "why", "success_condition", "evidence_needed"):
        _text(value[key], f"work-order {key}")
    return deepcopy(value)


def _department_for_owner(owner):
    owner = _text(owner, "work-order owner")
    if "." in owner:
        owner = owner.split(".", 1)[0]
    return owner


def _owner_role(owner):
    owner = _text(owner, "work-order owner")
    return owner.split(".", 1)[1] if "." in owner else "chief"


class DepartmentRuntime:
    """Durable project organization used by the Composer command desk."""

    def __init__(self, control, store, messages, tasks, *, project_id, organization=None):
        if store.control is not control or messages.control is not control or tasks.control is not control:
            raise ValidationError("department runtime services must share one project control store")
        self.control, self.store, self.messages, self.tasks = control, store, messages, tasks
        self.project_id = _text(project_id, "project_id")
        self.organization = validate_organization(organization or default_organization())
        self.charters = {item["id"]: item for item in self.organization["departments"]}
        self.manifest_refs = {}
        self._ensure_manifests()

    def _publish_idempotent(self, logical_id, artifact_type, body, *, author="command.composer", subjects=()):
        head = self.store.head(logical_id)
        if head is not None:
            current = json.loads(self.store.read_body(head["body_hash"]))
            comparable_current = {key: value for key, value in current.items()
                                  if key not in VOLATILE_FIELDS}
            comparable_body = {key: value for key, value in body.items()
                               if key not in VOLATILE_FIELDS}
            if comparable_current == comparable_body:
                return head
            parents = [head["artifact_ref"]]
        else:
            parents = []
        return self.store.publish_artifact(
            logical_id=logical_id, artifact_type=artifact_type, author=author,
            body=canonical_bytes(body), media_type="application/json", parents=parents,
            inputs=[{"ref": ref, "purpose": "subject"} for ref in dict.fromkeys(subjects) if ref],
        )

    def _ensure_manifests(self):
        org_record = self._publish_idempotent(
            "command/organization", "note",
            {"project_id": self.project_id, **self.organization,
             "agents": self.agents(), "stage_routes": self.stage_routes(),
             "role_pool": self.agents(), "active_assignments": [],
             "assignment_counts": {department: {} for department in self.charters}
             if hasattr(self, "charters") else {},
             "command_agents": deepcopy(COMMAND_ADDRESSES)},
        )
        self.manifest_refs["organization"] = org_record["artifact_ref"]
        for charter in self.organization["departments"]:
            record = self._publish_idempotent(
                f"command/departments/{charter['id']}/charter", "note",
                {"schema_version": CHARTER_SCHEMA_VERSION, "project_id": self.project_id, **charter},
                subjects=[org_record["artifact_ref"]],
            )
            self.manifest_refs[charter["id"]] = record["artifact_ref"]

    def address(self, department, role="chief"):
        department = _identifier(department, "department")
        if department in self.charters:
            assignment = self._resolve_assignment(department, role)
            return {"dept": department, "agent": assignment["agent"]}
        _identifier(role, "department role")
        return {"dept": department, "agent": role}

    def stage_route(self, stage_kind):
        """Return the functional owner and concrete appointments for a stage."""
        return stage_route(stage_kind, self.charters)

    def agents(self):
        """Return the concrete project appointments derived from the charters."""
        return agent_roster(self.charters)

    def stage_routes(self):
        """Return all configured default routes that have a live owner."""
        return [
            self.stage_route(route["stage_kind"])
            for route in DEFAULT_STAGE_ROUTES
            if route["department"] in self.charters
        ]

    def _resolve_assignment(self, department, role="chief"):
        """Resolve a public owner or internal role alias to one appointment."""
        department = _identifier(department, "department")
        charter = self.charters.get(department)
        if charter is None:
            raise ValidationError(f"unknown project department: {department}")
        role = _text(role, "department role")
        requested = role.split(".", 1)[1] if role.startswith(f"{department}.") else role
        if requested in {"chief", charter["chief"]}:
            return {
                "department": department, "agent": charter["chief"],
                "role_id": charter["chief"], "role": f"{department}.{charter['chief']}",
                "appointment": "chief", "execution_kind": "model",
                "model_role": "department.chief",
                "system_contract": "Synthesize the department's scoped work and admit only checked evidence.",
                "input_projection": ["objective", "stage_packet", "assignment_results", "review_verdict"],
                "independent_review": False, "reviewer_role_id": "adversary",
                "reviewer_agent": f"{department}.{charter['adversary']}",
                "quota": {"max_calls": 1, "max_input_tokens": 16000,
                          "max_output_tokens": 6000, "max_seconds": 900},
                "activation": "on_demand", "internal_role_aliases": [],
            }
        if requested in {"adversary", charter["adversary"]}:
            return {
                "department": department, "agent": charter["adversary"],
                "role_id": charter["adversary"], "role": f"{department}.{charter['adversary']}",
                "appointment": "adversary", "execution_kind": "review",
                "model_role": "review.arbiter",
                "system_contract": "Independently challenge the department result and report repair scope.",
                "input_projection": ["objective", "stage_packet", "assignment_results", "producer_claims"],
                "independent_review": True, "reviewer_role_id": None,
                "reviewer_agent": None,
                # Reserve one bounded repair/fallback call for malformed model
                # output; a verifier still has only one accepted verdict.
                "quota": {"max_calls": 2, "max_input_tokens": 16000,
                          "max_output_tokens": 6000, "max_seconds": 900},
                "activation": "on_demand", "internal_role_aliases": [],
            }
        # A department-only request is chief-owned.  Functional stage roles
        # are also chief aliases for compatibility with existing work orders.
        if requested in {route["functional_role"] for route in DEFAULT_STAGE_ROUTES
                         if route["department"] == department}:
            return self._resolve_assignment(department, "chief")
        for role_spec in charter.get("agent_roles", []):
            if requested == role_spec["id"] or requested in role_spec["internal_role_aliases"]:
                reviewer_id = role_spec.get("reviewer_role_id")
                reviewer_agent = None
                if reviewer_id == "adversary":
                    reviewer_agent = f"{department}.{charter['adversary']}"
                elif isinstance(reviewer_id, str):
                    reviewer_agent = f"{department}.{reviewer_id}"
                return {
                    **deepcopy(role_spec), "department": department,
                    "agent": role_spec["id"], "role": f"{department}.{role_spec['id']}",
                    "reviewer_agent": reviewer_agent,
                }
        raise ValidationError(f"unknown role for department {department}: {requested}")

    def resolve_address(self, owner):
        """Resolve a full owner address while preserving command addresses."""
        owner = _text(owner, "owner address")
        department = _department_for_owner(owner)
        return self._resolve_assignment(department, _owner_role(owner))

    def role_for_internal_role(self, internal_role):
        """Return the concrete roster appointment for an existing runner role."""
        internal_role = _text(internal_role, "internal role")
        for department, charter in self.charters.items():
            for role in charter.get("agent_roles", []):
                if internal_role in role.get("internal_role_aliases", []):
                    return self._resolve_assignment(department, role["id"])
        for route in DEFAULT_STAGE_ROUTES:
            if internal_role == f"{route['department']}.{route['functional_role']}":
                return self._resolve_assignment(route["department"], "chief")
        return None

    def _charter_for_owner(self, owner, kind):
        department = _department_for_owner(owner)
        charter = self.charters.get(department)
        if charter is None:
            raise ValidationError(f"work-order owner is not a project department: {owner}")
        assignment = self._resolve_assignment(department, _owner_role(owner))
        if (assignment.get("appointment") == "specialist"
                and kind not in assignment.get("proposal_kinds", [])):
            raise ValidationError(
                f"specialist {assignment['role']} does not admit {kind} work orders")
        if kind not in charter["proposal_kinds"] and not self.organization["allow_dynamic_proposals"]:
            raise ValidationError(f"{department} charter does not admit {kind} work orders")
        return department, charter

    def validate_request(self, proposal):
        """Validate a request against both the public schema and live charter."""
        value = validate_work_order(proposal)
        self._charter_for_owner(value["owner"], value["kind"])
        return value

    def _record_rejection(self, *, department, message_id, request_id, request,
                          reason, source_note_ref):
        """Persist a malformed or unroutable inbox item for later inspection."""
        if not isinstance(request_id, str) or not request_id.strip():
            request_id = hashlib.sha256(canonical_bytes(request)).hexdigest()[:20]
        logical_request_id = request_id if _ID.fullmatch(request_id) else hashlib.sha256(
            request_id.encode("utf-8")).hexdigest()[:20]
        rejection = {
            "schema_version": "department-request-rejection-1", "project_id": self.project_id,
            "message_id": message_id, "department": department, "request_id": request_id,
            "request": deepcopy(request), "reason": reason,
            "source_note_ref": source_note_ref, "state": "rejected", "created_at": now_iso(),
        }
        rejection_ref = self._publish_idempotent(
            f"command/departments/{department}/rejections/{logical_request_id}",
            "decision_note", rejection, subjects=[source_note_ref])
        return {"request_id": request_id, "rejection_ref": rejection_ref["artifact_ref"],
                "reason": reason}

    def reject_request(self, request, *, source_stage_id=None, reason):
        """Persist an invalid continuation request without aborting the run."""
        if isinstance(request, dict):
            raw_id = request.get("id")
            raw_owner = request.get("owner")
        else:
            raw_id = None
            raw_owner = None
        request_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else hashlib.sha256(
            canonical_bytes(request)).hexdigest()[:20]
        try:
            department = _department_for_owner(raw_owner)
        except ValidationError:
            department = "unrouted"
        if department not in self.charters:
            department = "unrouted"
        message_id = f"composer-{source_stage_id or 'workflow'}-{request_id}"
        return self._record_rejection(
            department=department, message_id=message_id, request_id=request_id,
            request=request, reason=reason, source_note_ref=None,
        )

    def _task_id(self, department, order_id, body):
        # Source routing metadata is provenance, not work identity.  A replay
        # from another message or stage must therefore find the same task;
        # substantive changes to the validated order receive a new task
        # generation while the logical artifact retains its version history.
        stable = {key: body[key] for key in (
            "schema_version", "id", "kind", "owner", "objective", "why",
            "success_condition", "evidence_needed") if key in body}
        digest = hashlib.sha256(canonical_bytes(stable)).hexdigest()[:20]
        return f"department-{department}-{order_id}-{digest}"

    def propose(self, proposal, *, source_stage_id=None, source_event_id=None, note_ref=None):
        """Publish and queue one validated autonomous departmental work order."""
        value = validate_work_order(proposal)
        department, charter = self._charter_for_owner(value["owner"], value["kind"])
        assignment = self._resolve_assignment(department, _owner_role(value["owner"]))
        reviewer = assignment.get("reviewer_agent") or f"{department}.{charter['adversary']}"
        previous = self.store.head(f"command/departments/{department}/work-orders/{value['id']}")
        body = {
            **value,
            "project_id": self.project_id,
            "department": department,
            "assigned_agent": assignment["agent"],
            "assigned_role": assignment["role"],
            "assignment_kind": assignment["appointment"],
            "assigned_model_role": assignment.get("model_role"),
            "assigned_execution_kind": assignment.get("execution_kind"),
            "review_agent": reviewer,
            "review_role": reviewer,
            "input_projection": list(assignment.get("input_projection", [])),
            "quota": deepcopy(assignment.get("quota", {})),
            "source_stage_id": source_stage_id,
            "source_event_id": source_event_id,
            "source_note_ref": note_ref,
            "state": "proposed",
            "created_at": now_iso(),
        }
        previous_body = {}
        same_generation = False
        if previous is not None:
            try:
                previous_body = json.loads(self.store.read_body(previous["body_hash"]))
            except (OSError, ValueError, TypeError):
                previous_body = {}
            substantive = (
                "schema_version", "id", "kind", "owner", "objective", "why",
                "success_condition", "evidence_needed",
            )
            same_generation = all(previous_body.get(key) == body.get(key) for key in substantive)
            if same_generation:
                prior_state = previous_body.get("state")
                if isinstance(prior_state, str):
                    body["state"] = prior_state
        logical = f"command/departments/{department}/work-orders/{value['id']}"
        task_id = self._task_id(department, value["id"], body)
        if previous is not None and not same_generation:
            # A changed objective is a new work-order generation.  Fence the
            # old generation before admitting the replacement so two chiefs
            # cannot execute contradictory objectives from one logical ID.
            rows = self.control._conn.execute(
                "SELECT task_id, state, payload_json FROM tasks ORDER BY task_id"
            ).fetchall()
            for row in rows:
                if row["task_id"] == task_id or row["state"] in {"completed", "failed", "cancelled", "stale", "rejected"}:
                    continue
                try:
                    prior_payload = json.loads(row["payload_json"])
                except (ValueError, TypeError):
                    continue
                prior_department = prior_payload.get("department")
                if isinstance(prior_department, str) and "." in prior_department:
                    prior_department = prior_department.split(".", 1)[0]
                if (prior_department == department
                        and prior_payload.get("id") == value["id"]):
                    active_attempts = self.control._conn.execute(
                        "SELECT attempt_id FROM attempts WHERE task_id = ? AND state = 'started'",
                        (row["task_id"],),
                    ).fetchall()
                    for attempt in active_attempts:
                        try:
                            self.tasks.reconcile_unknown(attempt["attempt_id"], "command.composer")
                        except (NotFoundError, StateError):
                            # A concurrent recovery may have settled the attempt
                            # between the task scan and this fence.
                            pass
                    self.tasks.transition(
                        row["task_id"], "stale", "command.composer",
                        reason="superseded by a changed work-order generation",
                    )
            # Artifact bodies are immutable.  Record the supersession as its
            # own version before publishing the replacement so the history
            # contains an explicit stale state for the old generation.
            stale_body = deepcopy(previous_body)
            stale_body["state"] = "stale"
            stale_body["superseded"] = True
            stale_body["updated_at"] = now_iso()
            self._publish_idempotent(
                logical, "decision_note", stale_body, author="command.composer",
                subjects=[previous["artifact_ref"]],
            )
        record = self._publish_idempotent(logical, "decision_note", body,
                                           subjects=[note_ref] if note_ref else [])
        payload = {"department": department, "work_order_ref": record["artifact_ref"], **body}
        try:
            task = self.tasks.get(task_id)
        except NotFoundError:
            task = self.tasks.create(task_id, TASK_KIND_BY_PROPOSAL[value["kind"]], payload,
                                     assignment["role"])
            task = self.tasks.admit(task_id, "command.composer")
        return {"department": department, "charter_ref": self.manifest_refs[department],
                "work_order_ref": record["artifact_ref"], "task_id": task_id,
                "task_state": task["state"], "kind": value["kind"], "id": value["id"],
                "assigned_agent": assignment["agent"], "assigned_role": assignment["role"],
                "review_agent": reviewer}

    def _set_work_order_state(self, result, state, *, actor="command.composer"):
        """Project the task lifecycle state into the immutable work-order ledger."""
        if state not in {"proposed", "queued", "running", "awaiting_review", "completed",
                         "blocked", "paused", "failed", "cancelled", "stale", "rejected"}:
            raise ValidationError(f"unsupported work-order state: {state}")
        logical = f"command/departments/{result['department']}/work-orders/{result['id']}"
        head = self.store.head(logical)
        if head is None:
            raise NotFoundError(f"missing work-order artifact: {logical}")
        body = json.loads(self.store.read_body(head["body_hash"]))
        body["state"] = state
        body["updated_at"] = now_iso()
        record = self._publish_idempotent(logical, "decision_note", body, author=actor,
                                           subjects=[head["artifact_ref"]])
        return record

    def receive_message(self, message_id, feedback, note_ref):
        """Persist an inbox item before the command desk acknowledges delivery."""
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValidationError("department inbox requires a message ID")
        if not isinstance(feedback, dict):
            raise ValidationError("department inbox feedback must be an object")
        target = feedback.get("to") or {}
        department = target.get("dept") if isinstance(target, dict) else None
        if not isinstance(department, str) or not department.strip():
            raise ValidationError("department inbox target is missing")
        department = _identifier(department, "department inbox target")
        body = {
            "schema_version": "department-inbox-1", "project_id": self.project_id,
            "message_id": message_id, "department": department,
            "feedback": deepcopy(feedback), "note_ref": note_ref,
            "state": "received", "received_at": now_iso(),
        }
        logical = f"command/departments/{department}/inbox/{message_id}"
        record = self._publish_idempotent(logical, "decision_note", body,
                                           subjects=[note_ref] if note_ref else [])
        proposals = []
        rejected = []
        # The command desk has one explicit system address in addition to the
        # project departments.  Every other target is retained as an unrouted
        # rejection instead of disappearing into an inbox that no chief owns.
        if department not in self.charters and department != "executive-command":
            rejected.append(self._record_rejection(
                department=department, message_id=message_id,
                request_id="unrouted-target", request={"target_department": department},
                reason="target department is not present in the project organization",
                source_note_ref=record["artifact_ref"],
            ))
            return {"inbox_ref": record["artifact_ref"], "department": department,
                    "proposals": proposals, "rejected": rejected}

        requests = []
        invalid_containers = []

        def collect(label, value):
            if value is None:
                return
            if not isinstance(value, list):
                invalid_containers.append((label, value))
                return
            requests.extend((label, item) for item in value)

        collect("research_expansion_requests", feedback.get("research_expansion_requests"))
        collect("research_requests", feedback.get("research_requests"))
        event = feedback.get("event")
        if isinstance(event, dict):
            for key in ("expansion_requests", "research_requests"):
                collect(f"event.{key}", event.get(key))
        elif event is not None:
            invalid_containers.append(("event", event))
        for label, value in invalid_containers:
            rejected.append(self._record_rejection(
                department=department, message_id=message_id,
                request_id=f"{label.replace('.', '-')}-container",
                request={"container": label, "value": deepcopy(value)},
                reason=f"{label} must be a list when supplied",
                source_note_ref=record["artifact_ref"],
            ))
        seen = set()
        for source_label, request in requests:
            if not isinstance(request, dict):
                rejected.append(self._record_rejection(
                    department=department, message_id=message_id,
                    request_id=f"{source_label.replace('.', '-')}-item-{len(rejected) + 1}",
                    request=request,
                    reason="work-order request must be an object",
                    source_note_ref=record["artifact_ref"],
                ))
                continue
            raw_request_id = request.get("id")
            request_key = raw_request_id if isinstance(raw_request_id, str) else hashlib.sha256(
                canonical_bytes(request)).hexdigest()
            if request_key in seen:
                continue
            seen.add(request_key)
            proposal = {key: request.get(key) for key in (
                "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed")}
            proposal["schema_version"] = WORK_ORDER_SCHEMA_VERSION
            try:
                if not all(isinstance(proposal.get(key), str) and proposal[key].strip() for key in proposal):
                    raise ValidationError("work-order request is missing a required string field")
                proposals.append(self.propose(
                    proposal, source_stage_id=feedback.get("stage_id"),
                    source_event_id=feedback.get("event_id"), note_ref=record["artifact_ref"],
                ))
            except (ValidationError, StateError) as exc:
                request_id = request.get("id") if isinstance(request.get("id"), str) else hashlib.sha256(
                    canonical_bytes(request)).hexdigest()[:20]
                rejected.append(self._record_rejection(
                    department=department, message_id=message_id, request_id=request_id,
                    request=request, reason=f"{type(exc).__name__}: {exc}",
                    source_note_ref=record["artifact_ref"],
                ))
        return {"inbox_ref": record["artifact_ref"], "department": department,
                "proposals": proposals, "rejected": rejected}

    def activate_work_orders(self, requests, *, actor="command.composer"):
        """Admit and mark the scoped work orders as running for a continuation.

        The Composer remains the only stage dispatcher, but this transition
        makes the department backlog reflect actual execution rather than a
        permanently queued notification.
        """
        active = []
        for request in requests or []:
            if not isinstance(request, dict):
                continue
            proposal = {key: request.get(key) for key in (
                "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed")}
            proposal["schema_version"] = WORK_ORDER_SCHEMA_VERSION
            result = self.propose(proposal, source_stage_id=request.get("source_stage_id"))
            task = self.tasks.get(result["task_id"])
            if task["state"] in {"blocked", "paused"}:
                task = self.tasks.transition(
                    result["task_id"], "queued", actor,
                    reason="scoped continuation reopens work order",
                )
            if task["state"] == "queued":
                task = self.tasks.transition(
                    result["task_id"], "running", actor,
                    reason="scoped continuation admitted",
                )
            record = self._set_work_order_state(result, task["state"], actor=actor)
            active.append({**result, "work_order_ref": record["artifact_ref"],
                           "task_state": task["state"]})
        return active

    def resolve_work_orders(self, requests, *, stage_kind, outcome, actor="command.composer"):
        """Close work orders whose owning stage produced an accepted result.

        A hold keeps its work order visible as running so the next autonomous
        continuation can continue the same objective.  A ready result closes
        only the matching request; unrelated departmental work remains intact.
        """
        resolved = []
        for request in requests or []:
            if not isinstance(request, dict) or REQUEST_STAGE_KINDS.get(request.get("kind")) != stage_kind:
                continue
            proposal = {key: request.get(key) for key in (
                "id", "kind", "owner", "objective", "why", "success_condition", "evidence_needed")}
            proposal["schema_version"] = WORK_ORDER_SCHEMA_VERSION
            result = self.propose(proposal, source_stage_id=request.get("source_stage_id"))
            task_id = result["task_id"]
            task = self.tasks.get(task_id)
            if outcome in {"completed", "accepted", "candidate_needs_review"}:
                if task["state"] in {"blocked", "paused"}:
                    task = self.tasks.transition(task_id, "queued", actor, reason="owning stage recovered work order")
                if task["state"] == "queued":
                    task = self.tasks.transition(task_id, "running", actor, reason="work order execution observed")
                if task["state"] == "running":
                    task = self.tasks.transition(task_id, "awaiting_review", actor, reason="owning stage returned")
                if task["state"] == "awaiting_review":
                    task = self.tasks.transition(task_id, "completed", actor, reason="owning stage accepted result")
            elif outcome == "blocked" and task["state"] in {"queued", "running"}:
                task = self.tasks.transition(task_id, "blocked", actor, reason="owning stage blocked")
            self._set_work_order_state(result, task["state"], actor=actor)
            resolved.append({"task_id": task_id, "state": task["state"], "request_id": request.get("id")})
        return resolved

    @staticmethod
    def _safe_assignment_component(value):
        return re.sub(r"[^a-z0-9_.-]+", "-", str(value).lower()).strip("-") or "item"

    def _assignment_task_id(self, department, stage_id, attempt_number, role_id):
        readable = "-".join(self._safe_assignment_component(item)
                             for item in (department, stage_id, attempt_number, role_id))
        digest = hashlib.sha256(
            f"{self.project_id}:{stage_id}:{attempt_number}:{role_id}".encode("utf-8")
        ).hexdigest()[:16]
        return f"specialist-{readable}-{digest}"

    def _assignment_artifact_logical(self, department, stage_id, attempt_number, role_id):
        return (
            f"command/departments/{department}/assignments/{self._safe_assignment_component(stage_id)}"
            f"/attempt-{attempt_number}/{self._safe_assignment_component(role_id)}"
        )

    def _assignment_attempt_id(self, assignment_id):
        return "specialist-attempt-" + hashlib.sha256(assignment_id.encode("utf-8")).hexdigest()[:28]

    def _bounded_input_ref(self, input_ref, stage_id):
        """Keep assignment inputs referential and bounded; never copy a packet."""
        if input_ref is None:
            return {"kind": "stage_context", "stage_id": stage_id}
        if isinstance(input_ref, str):
            return {"ref": input_ref[:512]}
        if isinstance(input_ref, dict):
            projected = {
                key: deepcopy(input_ref[key])
                for key in ("ref", "digest", "kind", "stage_id")
                if key in input_ref
            }
            if projected:
                return projected
            return {"kind": "opaque_input", "digest": hashlib.sha256(
                canonical_bytes(input_ref)).hexdigest()}
        return {"kind": "opaque_input", "digest": hashlib.sha256(
            canonical_bytes({"value": str(input_ref)})).hexdigest()}

    def _assignment_task_rows(self, *, stage_id=None, attempt_number=None):
        rows = self.control._conn.execute(
            "SELECT task_id, state, payload_json FROM tasks ORDER BY task_id"
        ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("assignment_id"), str):
                continue
            if stage_id is not None and payload.get("stage_id") != stage_id:
                continue
            if attempt_number is not None and payload.get("attempt_number") != attempt_number:
                continue
            item = {"task_id": row["task_id"], "task_state": row["state"], **payload}
            attempt_id = payload.get("attempt_id")
            if isinstance(attempt_id, str):
                attempt_row = self.control._conn.execute(
                    "SELECT state, usage_json FROM attempts WHERE attempt_id = ?",
                    (attempt_id,),
                ).fetchone()
                if attempt_row is not None:
                    item["attempt_state"] = attempt_row["state"]
                    try:
                        item["attempt_usage"] = json.loads(attempt_row["usage_json"])
                    except (TypeError, ValueError):
                        item["attempt_usage"] = {}
            result.append(item)
        return result

    def _assignment_projection(self):
        rows = self._assignment_task_rows()
        counts = {department: {state: 0 for state in (
            "proposed", "queued", "running", "awaiting_review", "completed",
            "blocked", "paused", "failed", "other")}
                  for department in self.charters}
        projected = []
        for item in rows:
            department = item.get("department")
            if department not in counts:
                continue
            state = item.get("task_state")
            counts[department][state if state in counts[department] else "other"] += 1
            item = deepcopy(item)
            if not item.get("artifact_ref") and isinstance(item.get("assignment_logical_id"), str):
                head = self.store.head(item["assignment_logical_id"])
                if head is not None:
                    item["artifact_ref"] = head["artifact_ref"]
            projected.append({key: deepcopy(item[key]) for key in (
                "assignment_id", "task_id", "stage_id", "stage_kind", "attempt_number",
                "department", "role_id", "agent", "assigned_role", "appointment",
                "execution_kind", "model_role", "task_state", "attempt_state",
                "assignment_logical_id", "artifact_ref", "verifier_agent",
                "reviewer_agent", "deadline_seconds", "quota", "internal_role_aliases", "internal_roles",
            ) if key in item})
        return projected, counts

    def _refresh_organization_manifest(self):
        assignments, assignment_counts = self._assignment_projection()
        record = self._publish_idempotent(
            "command/organization", "note",
            {"project_id": self.project_id, **self.organization,
             "agents": self.agents(), "stage_routes": self.stage_routes(),
             "command_agents": deepcopy(COMMAND_ADDRESSES),
             "active_assignments": [item for item in assignments
                                     if item.get("task_state") in ASSIGNMENT_ACTIVE_STATES],
             "assignment_counts": assignment_counts},
        )
        self.manifest_refs["organization"] = record["artifact_ref"]
        return record

    def reconcile_interrupted_assignments(self):
        """Mark specialist calls left in-flight by a stopped Composer unknown."""
        rows = self.control._conn.execute(
            "SELECT attempt_id, task_id, payload_json FROM attempts WHERE state = 'started'"
        ).fetchall()
        reconciled = []
        manifest_changed = False
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict) or not payload.get("assignment_id"):
                continue
            try:
                result = self.tasks.reconcile_unknown(row["attempt_id"], "command.composer")
            except (NotFoundError, StateError):
                continue
            manifest_changed = True
            reconciled.append({
                "assignment_id": payload["assignment_id"],
                "task_id": row["task_id"], "attempt_id": row["attempt_id"],
                "outcome": result.get("state", "result_unknown"),
            })
            logical = payload.get("assignment_logical_id")
            if isinstance(logical, str):
                head = self.store.head(logical)
                if head is not None:
                    body = json.loads(self.store.read_body(head["body_hash"]))
                    body.update({"task_state": "blocked", "attempt_state": "result_unknown",
                                 "outcome": "result_unknown",
                                 "accounting": "conservative_pending_reconciliation",
                                 "updated_at": now_iso()})
                    self._publish_idempotent(logical, "decision_note", body,
                                             subjects=[head["artifact_ref"]])
        # A verifier is admitted before the stage result is known but is only
        # started after chief synthesis.  If the process stopped during the
        # specialist phase, that queued verifier was never dispatched; fence
        # it with the interrupted attempt so it cannot remain an apparent
        # active worker when the Composer creates the next attempt.
        for item in self._assignment_task_rows():
            if (item.get("assignment_phase") != "verifier"
                    or item.get("task_state") != "queued"):
                continue
            sibling_unknown = any(
                sibling.get("stage_id") == item.get("stage_id")
                and sibling.get("attempt_number") == item.get("attempt_number")
                and sibling.get("assignment_phase") == "specialist"
                and sibling.get("attempt_state") == "result_unknown"
                for sibling in self._assignment_task_rows()
            )
            if not sibling_unknown:
                continue
            try:
                self.tasks.transition(
                    item["task_id"], "blocked", "command.composer",
                    reason="stage interrupted before verifier dispatch",
                )
            except (NotFoundError, StateError):
                continue
            manifest_changed = True
            logical = item.get("assignment_logical_id")
            if isinstance(logical, str):
                head = self.store.head(logical)
                if head is not None:
                    body = json.loads(self.store.read_body(head["body_hash"]))
                    body.update({"task_state": "blocked", "attempt_state": "not_started",
                                 "outcome": "result_unknown",
                                 "accounting": "not_dispatched_after_interruption",
                                 "updated_at": now_iso()})
                    self._publish_idempotent(logical, "decision_note", body,
                                             subjects=[head["artifact_ref"]])
        if manifest_changed:
            self._refresh_organization_manifest()
        return reconciled

    def begin_stage(self, stage_id, stage_kind, *, attempt_number=1, input_ref=None,
                    deadline_seconds, active_role_ids=None, quotas=None, actor="command.composer"):
        """Admit a bounded specialist pool for one Composer stage attempt."""
        _identifier(stage_id, "stage id")
        _identifier(stage_kind, "stage kind")
        if type(attempt_number) is not int or attempt_number < 1:
            raise ValidationError("specialist attempt_number must be a positive integer")
        _positive_number(deadline_seconds, "specialist stage deadline_seconds")
        route = self.stage_route(stage_kind)
        required_ids = list(route["required_role_ids"])
        selected_ids = list(active_role_ids) if active_role_ids is not None else required_ids
        if not selected_ids:
            raise ValidationError("specialist active_role_ids must be a unique nonempty list")
        if any(not isinstance(role_id, str) or not _ID.fullmatch(role_id)
               for role_id in selected_ids):
            raise ValidationError("specialist active_role_ids must contain bounded role IDs")
        if len(selected_ids) != len(set(selected_ids)):
            raise ValidationError("specialist active_role_ids must be a unique nonempty list")
        if set(selected_ids) - set(required_ids):
            raise ValidationError("specialist active_role_ids must be a subset of required stage roles")
        if len(selected_ids) > route["max_active_agents"]:
            raise QuotaExceededError(
                f"specialist stage pool exceeds max_active_agents={route['max_active_agents']}",
                dimension="max_active_agents", limit=route["max_active_agents"],
                observed=len(selected_ids),
            )
        if quotas is not None and not isinstance(quotas, dict):
            raise ValidationError("specialist quotas must be an object when supplied")
        quota_overrides = quotas or {}
        allowed_quota_keys = set(required_ids) | {
            f"{route['department']}.{role_id}" for role_id in required_ids
        }
        unknown_quota_keys = set(quota_overrides) - allowed_quota_keys
        if unknown_quota_keys:
            raise ValidationError(
                "specialist quotas contains unknown role IDs: "
                + ", ".join(sorted(str(key) for key in unknown_quota_keys))
            )
        input_projection_ref = self._bounded_input_ref(input_ref, stage_id)
        deadline_at_epoch = time.time() + float(deadline_seconds)
        assignment_rows = []
        specialist_roles = {role["id"]: role for role in route["required_roles"]}
        resolved_quotas = {}
        for role_id in selected_ids:
            role = specialist_roles[role_id]
            quota = quota_overrides.get(
                role_id, quota_overrides.get(f"{route['department']}.{role_id}", role["quota"])
            )
            resolved_quotas[role_id] = _validate_role_quota(
                quota, f"{stage_kind}.{role_id} quota")
        for role_id in selected_ids:
            role = specialist_roles[role_id]
            quota = resolved_quotas[role_id]
            assignment_id = f"{self.project_id}:{stage_id}:attempt-{attempt_number}:{role_id}"
            task_id = self._assignment_task_id(route["department"], stage_id, attempt_number, role_id)
            logical = self._assignment_artifact_logical(
                route["department"], stage_id, attempt_number, role_id)
            attempt_id = self._assignment_attempt_id(assignment_id)
            task_payload = {
                "assignment_id": assignment_id, "assignment_logical_id": logical,
                "stage_id": stage_id, "stage_kind": stage_kind,
                "attempt_number": attempt_number, "department": route["department"],
                "role_id": role_id, "agent": role_id,
                "assigned_role": f"{route['department']}.{role_id}",
                "appointment": role["appointment"], "execution_kind": role["execution_kind"],
                "model_role": role["model_role"],
                "system_contract": role["system_contract"],
                "input_projection": list(role["input_projection"]),
                "input_ref": input_projection_ref, "quota": quota,
                "reserved_seconds": min(float(quota["max_seconds"]), float(deadline_seconds)),
                "deadline_seconds": float(deadline_seconds),
                "deadline_at_epoch": deadline_at_epoch,
                "verifier_agent": route["verifier_agent"],
                "reviewer_agent": f"{route['department']}.{route['adversary']}",
                "internal_role_aliases": list(role["internal_role_aliases"]),
                "internal_roles": list(role["internal_role_aliases"]),
                "assignment_phase": "specialist",
                "attempt_id": attempt_id,
            }
            try:
                task = self.tasks.get(task_id)
            except NotFoundError:
                task = self.tasks.create(
                    task_id, TASK_KIND_BY_EXECUTION[role["execution_kind"]], task_payload,
                    f"{route['department']}.{role_id}",
                )
                task = self.tasks.admit(task_id, actor)
            try:
                prior_attempt = self.tasks.get_attempt(attempt_id)
            except NotFoundError:
                prior_attempt = None
            if prior_attempt is None and task["state"] == "queued":
                self.tasks.start_attempt(
                    task_id, attempt_id, owner=f"{route['department']}.{role_id}",
                    lease_ttl_seconds=max(1.0, min(float(deadline_seconds), float(quota["max_seconds"]))),
                    reserved=quota, payload=task_payload,
                )
                task = self.tasks.get(task_id)
            elif prior_attempt is not None and prior_attempt["state"] == "started":
                task = self.tasks.get(task_id)
            artifact_body = {
                "schema_version": "department-assignment-1", "project_id": self.project_id,
                **task_payload, "task_id": task_id, "task_state": task["state"],
                "attempt_state": (prior_attempt or {}).get("state", "started"),
                "outcome": None, "created_at": now_iso(),
            }
            artifact = self._publish_idempotent(logical, "decision_note", artifact_body,
                                                author=f"{route['department']}.{role_id}")
            task_payload["artifact_ref"] = artifact["artifact_ref"]
            assignment_rows.append({**task_payload, "task_id": task_id,
                                    "task_state": task["state"],
                                    "attempt_state": (prior_attempt or {}).get("state", "started"),
                                    "artifact_ref": artifact["artifact_ref"]})

        verifier_id = f"{stage_id}:attempt-{attempt_number}:verifier"
        verifier_role = route["adversary"]
        verifier_task_id = self._assignment_task_id(
            route["department"], stage_id, attempt_number, f"verifier-{verifier_role}")
        verifier_logical = self._assignment_artifact_logical(
            route["department"], stage_id, attempt_number, f"verifier-{verifier_role}")
        verifier_attempt_id = self._assignment_attempt_id(verifier_id)
        verifier_payload = {
            "assignment_id": verifier_id, "assignment_logical_id": verifier_logical,
            "stage_id": stage_id, "stage_kind": stage_kind,
            "attempt_number": attempt_number, "department": route["department"],
            "role_id": verifier_role, "agent": verifier_role,
            "assigned_role": route["verifier_agent"], "appointment": "adversary",
            "execution_kind": "review", "model_role": "review.arbiter",
            "system_contract": "Independently challenge the chief synthesis and specialist result.",
            "input_projection": ["stage_packet", "assignment_results", "chief_synthesis"],
            "input_ref": input_projection_ref,
            # Reserve one bounded repair/fallback call for malformed model
            # output; a verifier still has only one accepted verdict.
            "quota": {"max_calls": 2, "max_input_tokens": 16000,
                      "max_output_tokens": 6000, "max_seconds": min(900, float(deadline_seconds))},
            "reserved_seconds": min(900.0, float(deadline_seconds)),
            "deadline_seconds": float(deadline_seconds), "deadline_at_epoch": deadline_at_epoch,
            "verifier_agent": route["verifier_agent"], "reviewer_agent": None,
            "internal_role_aliases": [], "internal_roles": [], "assignment_phase": "verifier",
            "attempt_id": verifier_attempt_id,
        }
        try:
            verifier_task = self.tasks.get(verifier_task_id)
        except NotFoundError:
            verifier_task = self.tasks.create(verifier_task_id, "verification", verifier_payload,
                                              route["verifier_agent"])
            verifier_task = self.tasks.admit(verifier_task_id, actor)
        verifier_artifact = self._publish_idempotent(
            verifier_logical, "decision_note",
            {"schema_version": "department-assignment-1", "project_id": self.project_id,
             **verifier_payload, "task_id": verifier_task_id,
             "task_state": verifier_task["state"], "attempt_state": None,
             "outcome": None, "created_at": now_iso()},
            author=route["verifier_agent"],
        )
        verifier_payload["artifact_ref"] = verifier_artifact["artifact_ref"]
        assignment_rows.append({**verifier_payload, "task_id": verifier_task_id,
                                "task_state": verifier_task["state"], "attempt_state": None,
                                "artifact_ref": verifier_artifact["artifact_ref"]})
        plan = {
            "schema_version": "department-stage-assignment-1", "project_id": self.project_id,
            "stage_id": stage_id, "stage_kind": stage_kind, "attempt_number": attempt_number,
            "department": route["department"], "required_agents": route["required_agents"],
            "active_agents": [item["assigned_role"] for item in assignment_rows
                              if item.get("assignment_phase") == "specialist"],
            "verifier_agent": route["verifier_agent"], "chief_agent": route["role"].split(".", 1)[0]
            + "." + route["chief"],
            "input_ref": input_projection_ref, "deadline_seconds": float(deadline_seconds),
            "deadline_at_epoch": deadline_at_epoch, "max_active_agents": route["max_active_agents"],
            "role_quotas": {
                item["assigned_role"]: deepcopy(item["quota"])
                for item in assignment_rows
            },
            "assignments": [{key: item[key] for key in (
                "assignment_id", "task_id", "assigned_role", "role_id", "appointment",
                "execution_kind", "model_role", "quota", "internal_roles", "artifact_ref") if key in item}
                            for item in assignment_rows],
            "independence_check": route["chief"] != route["adversary"],
            "created_at": now_iso(),
        }
        plan_logical = (
            f"command/departments/{route['department']}/assignments/"
            f"{self._safe_assignment_component(stage_id)}/attempt-{attempt_number}/plan"
        )
        plan_artifact = self._publish_idempotent(
            plan_logical, "decision_note", plan, author=actor,
            subjects=[item["artifact_ref"] for item in assignment_rows],
        )
        self._refresh_organization_manifest()
        return {
            "stage_id": stage_id, "stage_kind": stage_kind, "attempt_number": attempt_number,
            "department": route["department"], "required_agents": route["required_agents"],
            "active_agents": plan["active_agents"], "verifier_agent": route["verifier_agent"],
            "chief_agent": plan["chief_agent"], "assignments": assignment_rows,
            "assignment_ids": [item["assignment_id"] for item in assignment_rows],
            "task_ids": [item["task_id"] for item in assignment_rows],
            "role_quotas": deepcopy(plan["role_quotas"]),
            "plan_ref": plan_artifact["artifact_ref"], "deadline_seconds": float(deadline_seconds),
        }

    def finish_stage(self, stage_id, stage_kind, *, attempt_number=1, outcome,
                     output_ref=None, usage=None, error=None, actor="command.composer",
                     specialist_results=None, verifier_result=None):
        """Close specialist assignments, then publish chief synthesis and an independent verdict."""
        route = self.stage_route(stage_kind)
        rows = self._assignment_task_rows(stage_id=stage_id, attempt_number=attempt_number)
        if not rows:
            raise NotFoundError(f"no specialist assignments for stage attempt: {stage_id}/{attempt_number}")
        role_order = {
            role_id: index for index, role_id in enumerate(route["required_role_ids"])
        }
        rows.sort(key=lambda item: (
            item.get("assignment_phase") == "verifier",
            role_order.get(item.get("role_id"), len(role_order)),
        ))
        known_success = outcome in {"completed", "accepted", "candidate_needs_review",
                                    "research_expansion_required", "review_rejected"}
        unknown = outcome == "result_unknown"
        stage_usage = deepcopy(usage) if isinstance(usage, dict) else {}
        by_role_usage = stage_usage.get("by_role") if isinstance(stage_usage.get("by_role"), dict) else {}
        result_by_role = specialist_results if isinstance(specialist_results, dict) else {}
        assignment_results = []
        for item in rows:
            if item.get("assignment_phase") == "verifier":
                continue
            task_id = item["task_id"]
            task_state = item["task_state"]
            attempt_id = item.get("attempt_id")
            specialist = result_by_role.get(item.get("role_id"))
            reported = specialist.get("status") if isinstance(specialist, dict) else None
            if unknown:
                assignment_outcome = "result_unknown"
            elif reported in {"succeeded", "failed", "result_unknown"}:
                # The stage result and the child execution result are separate
                # scopes. A chief/stage failure must not rewrite a specialist
                # that already returned a known result as failed.
                assignment_outcome = reported
            else:
                assignment_outcome = "succeeded" if known_success else "failed"
            if attempt_id and item.get("attempt_state") == "started":
                if assignment_outcome == "result_unknown":
                    self.tasks.reconcile_unknown(attempt_id, actor)
                else:
                    self.tasks.finish_attempt(attempt_id, assignment_outcome,
                                              usage=(specialist or {}).get("usage", by_role_usage.get(item.get("role_id"), {})))
            if assignment_outcome == "result_unknown":
                task_state = self.tasks.get(task_id)["state"]
            elif assignment_outcome == "succeeded":
                if task_state == "running":
                    task_state = self.tasks.transition(task_id, "awaiting_review", actor,
                                                       reason="stage result returned")["state"]
            elif task_state == "running":
                task_state = self.tasks.transition(task_id, "failed", actor,
                                                   reason=str((specialist or {}).get("error") or error or outcome))["state"]
            role_usage = deepcopy((specialist or {}).get("usage", by_role_usage.get(item.get("role_id"), {})))
            result_body = {
                "schema_version": "department-assignment-1", "project_id": self.project_id,
                **{key: deepcopy(item[key]) for key in item if key not in {"task_state", "attempt_state", "attempt_usage"}},
                "task_state": task_state, "attempt_state": assignment_outcome,
                "outcome": assignment_outcome, "output_ref": str(output_ref) if output_ref else None,
                "usage": role_usage,
                "usage_scope": "role" if item.get("role_id") in by_role_usage else "stage_unattributed",
                "stage_usage": deepcopy(stage_usage), "error": str(error) if error else None,
                "execution_artifact_ref": (specialist or {}).get("artifact_ref"),
                "verifier_agent": route["verifier_agent"], "updated_at": now_iso(),
            }
            logical = item["assignment_logical_id"]
            artifact = self._publish_idempotent(logical, "decision_note", result_body,
                                                author=item["assigned_role"],
                                                subjects=[item.get("artifact_ref")])
            assignment_results.append({"assignment_id": item["assignment_id"], "task_id": task_id,
                                       "role_id": item["role_id"], "agent": item["assigned_role"],
                                       "task_state": task_state, "outcome": assignment_outcome,
                                       "artifact_ref": artifact["artifact_ref"],
                                       "usage": role_usage,
                                       "execution_artifact_ref": (specialist or {}).get("artifact_ref")})

        chief_agent = route["owner_address"]["dept"] + "." + route["owner_address"]["agent"]
        synthesis_logical = (
            f"command/departments/{route['department']}/assignments/"
            f"{self._safe_assignment_component(stage_id)}/attempt-{attempt_number}/chief-synthesis"
        )
        synthesis = {
            "schema_version": "department-chief-synthesis-1", "project_id": self.project_id,
            "stage_id": stage_id, "stage_kind": stage_kind, "attempt_number": attempt_number,
            "department": route["department"], "chief_agent": chief_agent,
            "producer_agents": [item["agent"] for item in assignment_results],
            "assignment_refs": [item["artifact_ref"] for item in assignment_results],
            "output_ref": str(output_ref) if output_ref else None, "outcome": outcome,
            "usage": deepcopy(stage_usage), "error": str(error) if error else None,
            "specialist_outcomes": {
                item["role_id"]: item["outcome"] for item in assignment_results
            },
            "verifier_agent": route["verifier_agent"],
            "independence_check": chief_agent != route["verifier_agent"],
            "created_at": now_iso(),
        }
        synthesis_artifact = self._publish_idempotent(
            synthesis_logical, "decision_note", synthesis, author=chief_agent,
            subjects=[item["artifact_ref"] for item in assignment_results],
        )

        verifier = next(item for item in rows if item.get("assignment_phase") == "verifier")
        verifier_task_id = verifier["task_id"]
        verifier_attempt_id = verifier.get("attempt_id") or self._assignment_attempt_id(verifier["assignment_id"])
        verifier_task = self.tasks.get(verifier_task_id)
        verifier_payload = deepcopy(verifier)
        verifier_payload["attempt_id"] = verifier_attempt_id
        verifier_payload["chief_synthesis_ref"] = synthesis_artifact["artifact_ref"]
        if isinstance(verifier_result, dict):
            verifier_status = verifier_result.get("status")
            verifier_response = verifier_result.get("response")
            if verifier_status == "result_unknown":
                verifier_outcome, verifier_execution = "result_unknown", "result_unknown"
            elif verifier_status != "succeeded":
                verifier_outcome, verifier_execution = "failed", "failed"
            else:
                decision = verifier_response.get("decision") if isinstance(verifier_response, dict) else None
                verifier_outcome = "accepted" if known_success and decision == "accept" else "hold" if known_success else "failed"
                verifier_execution = "succeeded"
        else:
            verifier_outcome = "result_unknown" if unknown else "accepted" if known_success and outcome in {
                "completed", "accepted", "candidate_needs_review"} else "hold" if known_success else "failed"
            verifier_execution = "succeeded" if verifier_outcome != "result_unknown" else "result_unknown"
        if verifier_task["state"] == "queued":
            self.tasks.start_attempt(
                verifier_task_id, verifier_attempt_id, owner=route["verifier_agent"],
                lease_ttl_seconds=max(1.0, min(float(verifier["deadline_seconds"]),
                                               float(verifier["quota"]["max_seconds"]))),
                reserved=verifier["quota"], payload=verifier_payload,
            )
            verifier_task = self.tasks.get(verifier_task_id)
        try:
            verifier_attempt = self.tasks.get_attempt(verifier_attempt_id)
        except NotFoundError:
            verifier_attempt = None
        if verifier_attempt is not None and verifier_attempt["state"] == "started":
            if verifier_execution == "result_unknown":
                self.tasks.reconcile_unknown(verifier_attempt_id, actor)
            else:
                self.tasks.finish_attempt(verifier_attempt_id, verifier_execution,
                                          usage=(verifier_result or {}).get("usage", {}))
        verifier_task = self.tasks.get(verifier_task_id)
        if verifier_outcome == "result_unknown":
            verifier_state = verifier_task["state"]
        else:
            if verifier_task["state"] == "running":
                if verifier_execution == "failed":
                    verifier_task = self.tasks.transition(verifier_task_id, "failed", actor,
                                                          reason="independent verifier call failed")
                else:
                    verifier_task = self.tasks.transition(verifier_task_id, "awaiting_review", actor,
                                                          reason="independent verdict returned")
            if verifier_task["state"] == "awaiting_review":
                verifier_task = self.tasks.transition(verifier_task_id, "completed", route["verifier_agent"],
                                                      reason=f"verdict:{verifier_outcome}")
            verifier_state = verifier_task["state"]
        verifier_logical = verifier["assignment_logical_id"]
        verdict_body = {
            "schema_version": "department-adversarial-verdict-1", "project_id": self.project_id,
            "stage_id": stage_id, "stage_kind": stage_kind, "attempt_number": attempt_number,
            "department": route["department"], "verifier_agent": route["verifier_agent"],
            "verifier_role": route["verifier_agent"], "chief_agent": chief_agent,
            "producer_agents": [item["agent"] for item in assignment_results],
            "chief_synthesis_ref": synthesis_artifact["artifact_ref"],
            "assignment_refs": [item["artifact_ref"] for item in assignment_results],
            "verdict": verifier_outcome, "task_state": verifier_state,
            "attempt_state": verifier_execution, "output_ref": str(output_ref) if output_ref else None,
            "error": str(error) if error else None,
            "verifier_execution_artifact_ref": (verifier_result or {}).get("artifact_ref"),
            "verifier_report": deepcopy(verifier_result.get("response")) if isinstance(verifier_result, dict) else None,
            "independence_check": (
                chief_agent != route["verifier_agent"]
                and route["verifier_agent"] != route["owner_address"]["agent"]
            ),
            "created_at": now_iso(),
        }
        verdict_artifact = self._publish_idempotent(
            verifier_logical, "decision_note", verdict_body,
            author=route["verifier_agent"], subjects=[synthesis_artifact["artifact_ref"]],
        )
        for item in assignment_results:
            logical = next(row["assignment_logical_id"] for row in rows
                           if row["assignment_id"] == item["assignment_id"])
            head = self.store.head(logical)
            if head is None:
                continue
            body = json.loads(self.store.read_body(head["body_hash"]))
            body.update({"verifier_artifact_ref": verdict_artifact["artifact_ref"],
                         "review_status": verifier_outcome, "updated_at": now_iso()})
            updated = self._publish_idempotent(logical, "decision_note", body,
                                                author=item["agent"], subjects=[head["artifact_ref"], verdict_artifact["artifact_ref"]])
            item["artifact_ref"] = updated["artifact_ref"]
            item["verifier_artifact_ref"] = verdict_artifact["artifact_ref"]
            task = self.tasks.get(item["task_id"])
            if task["state"] == "awaiting_review":
                item["task_state"] = self.tasks.transition(
                    item["task_id"], "completed", actor,
                    reason=f"independent verdict:{verifier_outcome}")["state"]
        self._refresh_organization_manifest()
        return {
            "stage_id": stage_id, "stage_kind": stage_kind, "attempt_number": attempt_number,
            "required_agents": route["required_agents"],
            "active_agents": [item["agent"] for item in assignment_results],
            "verifier_agent": route["verifier_agent"], "chief_agent": chief_agent,
            "assignments": assignment_results, "chief_synthesis_ref": synthesis_artifact["artifact_ref"],
            "verifier_artifact_ref": verdict_artifact["artifact_ref"],
            "verifier_outcome": verifier_outcome, "verifier_task_state": verifier_state,
        }

    def snapshot(self):
        """Return a compact backlog projection for checkpoints and UI status."""
        counts = {department: {"proposed": 0, "queued": 0, "running": 0, "awaiting_review": 0,
                               "completed": 0, "blocked": 0, "paused": 0, "other": 0}
                  for department in self.charters}
        open_orders = []
        rows = self.control._conn.execute("SELECT task_id, state, payload_json FROM tasks ORDER BY task_id").fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (ValueError, TypeError):
                continue
            department = payload.get("department")
            if isinstance(department, str) and department not in counts and "." in department:
                # Older Composer tasks stored the full role address.  Keep
                # their backlog visible while new tasks use the department
                # ID plus a separate role field.
                department = department.split(".", 1)[0]
            if department not in counts:
                continue
            # Specialist assignments have their own activity ledger.  Keep
            # backlog_counts compatible with the original department work
            # order projection and expose assignment counts separately.
            if payload.get("assignment_id"):
                continue
            state = row["state"]
            bucket = state if state in counts[department] else "other"
            counts[department][bucket] += 1
            if payload.get("work_order_ref") and state not in {"completed", "failed", "cancelled", "stale"}:
                work_order_ref = payload.get("work_order_ref")
                order_id = payload.get("id")
                if isinstance(order_id, str):
                    head = self.store.head(
                        f"command/departments/{department}/work-orders/{order_id}")
                    if head is not None:
                        work_order_ref = head["artifact_ref"]
                open_orders.append({"task_id": row["task_id"], "department": department,
                                    "state": state, "work_order_ref": work_order_ref,
                                    "kind": payload.get("kind"), "objective": payload.get("objective")})
        assignments, assignment_counts = self._assignment_projection()
        active_assignments = [item for item in assignments
                              if item.get("task_state") in ASSIGNMENT_ACTIVE_STATES]
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": self.project_id,
            "template": self.organization["template"],
            "departments": deepcopy(self.organization["departments"]),
            "agents": self.agents(),
            "role_pool": self.agents(),
            "stage_routes": self.stage_routes(),
            "command_agents": deepcopy(COMMAND_ADDRESSES),
            "manifest_refs": deepcopy(self.manifest_refs),
            "allow_dynamic_proposals": self.organization["allow_dynamic_proposals"],
            "backlog_counts": counts,
            "open_work_orders": open_orders[:self.organization["max_open_work_orders"]],
            "active_assignments": active_assignments,
            "assignment_counts": assignment_counts,
            "agent_activity": assignments[-256:],
        }


__all__ = [
    "LEGACY_SCHEMA_VERSION", "SCHEMA_VERSION", "LEGACY_CHARTER_SCHEMA_VERSION",
    "CHARTER_SCHEMA_VERSION", "WORK_ORDER_SCHEMA_VERSION", "PROPOSAL_KINDS",
    "DEFAULT_DEPARTMENTS", "DEFAULT_AGENT_ROLES", "DEFAULT_STAGE_ROUTES", "COMMAND_ADDRESSES",
    "default_organization", "default_stage_routes", "stage_role", "stage_route", "agent_roster",
    "validate_charter", "validate_organization", "validate_work_order", "DepartmentRuntime",
]
