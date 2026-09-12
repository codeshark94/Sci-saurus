"""Deterministic desk review for the scholarly depth of a manuscript.

The ordinary manuscript reviewers inspect argument, methods, scientific
interpretation, and prose.  This module supplies the missing handling-editor
decision: whether the declared publication tier has enough literature and
visual evidence to be considered a conventional scholarly article.

The thresholds are named profiles rather than hidden heuristics.  A project
chooses its profile in the paper configuration; the gate records every count
and emits scoped findings when the candidate falls short.
"""

from __future__ import annotations

from copy import deepcopy
import re

from scisaurus.core.errors import ValidationError


SCHEMA_VERSION = "scholarly-depth-review-1"
CHECK_IDS = (
    "reference_count", "full_text_support", "citation_density",
    "cited_section_coverage", "figure_count", "table_count",
)
OBSERVED_FIELDS = {
    "references", "full_text_references", "intext_citations", "unique_citations",
    "cited_sections", "figures", "tables",
}


# These are desk-review floors for the two supported release modes.  They are
# deliberately explicit and inspectable: projects with a different venue can
# add a profile rather than silently weakening a journal release.
PROFILES = {
    "validation_report": {
        "label": "bounded validation report",
        "min_references": 5,
        "min_full_text_references": 1,
        "min_intext_citations": 3,
        "min_figures": 2,
        "min_tables": 1,
        "min_cited_sections": 2,
    },
    "empirical_journal": {
        "label": "empirical journal article desk floor",
        "min_references": 20,
        "min_full_text_references": 5,
        "min_intext_citations": 12,
        "min_figures": 3,
        "min_tables": 1,
        "min_cited_sections": 3,
    },
}


def profile_ids():
    return tuple(PROFILES)


def profile_for_paper(paper_config):
    """Resolve the publication floor without silently downgrading a paper.

    A validation report is intentionally allowed to be short.  A declared
    research paper, however, must enter the journal desk with the empirical
    floor even when an older descriptor omitted the optional profile field.
    Explicit profiles remain authoritative and are validated here once.
    """
    explicit = paper_config.get("scholarly_profile") if isinstance(paper_config, dict) else None
    default = ("empirical_journal" if isinstance(paper_config, dict)
               and paper_config.get("document_type") == "research_paper"
               else "validation_report")
    resolved = validate_profile_id(explicit or default)
    if default == "empirical_journal" and resolved != "empirical_journal":
        raise ValidationError("research_paper cannot use the validation_report scholarly profile")
    return resolved


def validate_profile_id(value):
    if not isinstance(value, str) or value not in PROFILES:
        raise ValidationError(f"scholarly_profile must be one of {sorted(PROFILES)}")
    return value


def _citation_markers(text):
    return re.findall(r"\[\[cite:([a-z][a-z0-9_-]{0,63})\]\]", text)


def _full_text_reference_count(references, survey):
    sources = survey.get("sources", {}) if isinstance(survey, dict) else {}
    return sum(sources.get(item.get("source_ref"), {}).get("representation") == "full_text"
               for item in references if isinstance(item, dict))


def _check(check_id, outcome, observed, required, evidence):
    return {"id": check_id, "outcome": outcome, "observed": observed,
            "required": required, "evidence": evidence}


def evaluate_scholarly_depth(manuscript, paper_config, results, survey, *, profile_id=None):
    """Return a handling-editor review without changing the manuscript."""
    profile_id = validate_profile_id(profile_id) if profile_id else profile_for_paper(paper_config)
    target = PROFILES[profile_id]
    references = [item for item in paper_config.get("references", []) if isinstance(item, dict)]
    sections = manuscript.get("sections")
    if sections is None:
        # PaperReleaseBuilder exposes the accepted DocumentManifest as
        # ``groups`` plus a unit index, while the pipeline review projection
        # uses ``sections``.  Normalize both read-only representations before
        # counting scholarly displays and citation distribution.
        sections = []
        for index, group in enumerate(manuscript.get("groups", [])):
            section_units = []
            for unit in group.get("units", []):
                if isinstance(unit, dict):
                    section_units.append(unit)
            sections.append({"id": f"group_{index}", "units": section_units})
    units = [unit for section in sections for unit in section.get("units", [])]
    text_by_section = {
        section.get("id"): " ".join(unit.get("text", "") for unit in section.get("units", []))
        for section in sections
    }
    markers_by_section = {
        section_id: _citation_markers(text)
        for section_id, text in text_by_section.items()
    }
    markers = [marker for values in markers_by_section.values() for marker in values]
    figures = [asset for asset in results.get("assets", []) if asset.get("role") == "figure"]
    tables = [unit for unit in units if unit.get("kind") == "table"]
    full_text = _full_text_reference_count(references, survey)
    cited_sections = sum(bool(values) for values in markers_by_section.values())
    observed = {
        "references": len(references),
        "full_text_references": full_text,
        "intext_citations": len(markers),
        "unique_citations": len(set(markers)),
        "cited_sections": cited_sections,
        "figures": len(figures),
        "tables": len(tables),
    }
    checks = [
        _check("reference_count", "passed" if observed["references"] >= target["min_references"] else "failed",
               observed["references"], target["min_references"],
               f"{observed['references']} configured references; desk floor is {target['min_references']}"),
        _check("full_text_support", "passed" if observed["full_text_references"] >= target["min_full_text_references"] else "failed",
               observed["full_text_references"], target["min_full_text_references"],
               f"{observed['full_text_references']} references have full-text evidence; desk floor is {target['min_full_text_references']}"),
        _check("citation_density", "passed" if observed["intext_citations"] >= target["min_intext_citations"] else "failed",
               observed["intext_citations"], target["min_intext_citations"],
               f"{observed['intext_citations']} in-text citation bindings; desk floor is {target['min_intext_citations']}"),
        _check("cited_section_coverage", "passed" if observed["cited_sections"] >= target["min_cited_sections"] else "failed",
               observed["cited_sections"], target["min_cited_sections"],
               f"{observed['cited_sections']} sections contain citations; desk floor is {target['min_cited_sections']}"),
        _check("figure_count", "passed" if observed["figures"] >= target["min_figures"] else "failed",
               observed["figures"], target["min_figures"],
               f"{observed['figures']} argument-linked figures; desk floor is {target['min_figures']}"),
        _check("table_count", "passed" if observed["tables"] >= target["min_tables"] else "failed",
               observed["tables"], target["min_tables"],
               f"{observed['tables']} tables; desk floor is {target['min_tables']}"),
    ]
    findings = []
    for check in checks:
        if check["outcome"] == "passed":
            continue
        check_id = check["id"]
        findings.append({
            "id": f"journal_editor_{check_id}",
            "severity": "major",
            "location": "Bibliography, literature synthesis, and Results displays",
            "problem": (f"The candidate does not meet the {target['label']} profile requirement for {check_id}: "
                        f"observed {check['observed']}, required {check['required']}."),
            "surgical_fix": ("Extend the accepted literature or experiment package and add only "
                             "claim-relevant citations or displays; do not pad the bibliography or duplicate figures."),
            "protected": ["all accepted result values", "the current research question and bounded conclusion"],
            "verification": f"Re-run the journal_editor desk check with {check_id} at or above its declared floor.",
        })
    decision = "accept" if not findings else "revise"
    return {
        "schema_version": SCHEMA_VERSION,
        "reviewer_id": "journal_editor",
        "profile_id": profile_id,
        "decision": decision,
        "checks": checks,
        "observed": observed,
        "findings": findings,
        "rationale": (f"Applied the {target['label']} profile. "
                      + ("All declared depth checks passed." if not findings
                         else f"{len(findings)} depth checks require a scholarly expansion before release.")),
    }


def validate_scholarly_depth_review(value):
    """Validate the deterministic desk-review record before persistence."""
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "reviewer_id", "profile_id", "decision", "checks",
            "observed", "findings", "rationale"}:
        raise ValidationError("scholarly depth review has an invalid shape")
    if value["schema_version"] != SCHEMA_VERSION or value["reviewer_id"] != "journal_editor":
        raise ValidationError("scholarly depth review identity is invalid")
    profile_id = validate_profile_id(value["profile_id"])
    if value["decision"] not in {"accept", "revise"}:
        raise ValidationError("scholarly depth review decision is invalid")
    if (not isinstance(value["observed"], dict)
            or set(value["observed"]) != OBSERVED_FIELDS
            or any(type(value["observed"][field]) is not int or value["observed"][field] < 0
                   for field in OBSERVED_FIELDS)):
        raise ValidationError("scholarly depth observed counts are invalid")
    if not isinstance(value["checks"], list) or len(value["checks"]) != len(CHECK_IDS):
        raise ValidationError("scholarly depth review lists are invalid")
    if [item.get("id") for item in value["checks"]] != list(CHECK_IDS):
        raise ValidationError("scholarly depth checks are incomplete or out of order")
    required_by_check = {
        "reference_count": PROFILES[profile_id]["min_references"],
        "full_text_support": PROFILES[profile_id]["min_full_text_references"],
        "citation_density": PROFILES[profile_id]["min_intext_citations"],
        "cited_section_coverage": PROFILES[profile_id]["min_cited_sections"],
        "figure_count": PROFILES[profile_id]["min_figures"],
        "table_count": PROFILES[profile_id]["min_tables"],
    }
    observed_by_check = {
        "reference_count": "references", "full_text_support": "full_text_references",
        "citation_density": "intext_citations", "cited_section_coverage": "cited_sections",
        "figure_count": "figures", "table_count": "tables",
    }
    for check in value["checks"]:
        if (not isinstance(check, dict)
                or set(check) != {"id", "outcome", "observed", "required", "evidence"}
                or check["outcome"] not in {"passed", "failed"}
                or type(check["observed"]) is not int or check["observed"] < 0
                or type(check["required"]) is not int or check["required"] < 0
                or not isinstance(check["evidence"], str) or not check["evidence"].strip()):
            raise ValidationError("scholarly depth check has an invalid shape")
        if (check["required"] != required_by_check[check["id"]]
                or check["observed"] != value["observed"][observed_by_check[check["id"]]]
                or (check["outcome"] == "passed") != (check["observed"] >= check["required"])):
            raise ValidationError("scholarly depth check is inconsistent with its profile counts")
    failed = [check["id"] for check in value["checks"] if check["outcome"] == "failed"]
    if value["decision"] == "accept" and (failed or value["findings"]):
        raise ValidationError("accepted scholarly depth review cannot retain findings")
    if value["decision"] == "revise" and not value["findings"]:
        raise ValidationError("revising scholarly depth review requires findings")
    if value["decision"] == "revise" and len(value["findings"]) != len(failed):
        raise ValidationError("scholarly depth findings must cover every failed check")
    finding_ids = []
    for finding in value["findings"]:
        if (not isinstance(finding, dict)
                or set(finding) != {"id", "severity", "location", "problem", "surgical_fix", "protected", "verification"}
                or finding["severity"] != "major"
                or not isinstance(finding["id"], str)
                or not isinstance(finding["location"], str) or not finding["location"].strip()
                or not isinstance(finding["problem"], str) or not finding["problem"].strip()
                or not isinstance(finding["surgical_fix"], str) or not finding["surgical_fix"].strip()
                or not isinstance(finding["protected"], list)
                or any(not isinstance(item, str) or not item.strip() for item in finding["protected"])
                or not isinstance(finding["verification"], str) or not finding["verification"].strip()):
            raise ValidationError("scholarly depth finding has an invalid shape")
        finding_ids.append(finding["id"])
    expected_ids = [f"journal_editor_{check_id}" for check_id in failed]
    if finding_ids != expected_ids:
        raise ValidationError("scholarly depth findings do not match failed checks")
    if not isinstance(value["rationale"], str) or not value["rationale"].strip():
        raise ValidationError("scholarly depth rationale is invalid")
    return deepcopy(value)
