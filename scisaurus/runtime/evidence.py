"""Authoritative source identifiers shared by scientific consumers."""

from scisaurus.core.errors import ValidationError
import hashlib
from scisaurus.core.source_spans import index_evidence


def maturity_requirement_id(requirement):
    return hashlib.sha256(requirement.encode("utf-8")).hexdigest()


def survey_evidence_projection(survey):
    """Project captured survey text into bounded, source-addressed context."""
    cards, literature = [], []
    assessment, proofs = index_evidence(survey.get("assessment", {}), survey.get("sources", {}))
    decisive_sources = {proof["source_ref"] for proof in proofs}
    sources = list(survey.get("sources", {}).items())
    sources.sort(key=lambda item: (item[0] not in decisive_sources, "full-text" not in item[0]))
    for source_ref, source in sources[:50]:
        if not isinstance(source, dict) or not isinstance(source.get("work_id"), str):
            continue
        work_id = source["work_id"]
        title = source.get("title") or work_id
        text = source.get("abstract") or source.get("text") or ""
        authors = source.get("authors") or "Authors not supplied by the survey."
        if isinstance(authors, list):
            authors = ", ".join(str(item) for item in authors)
        cards.append({"source_ref": source_ref, "work_id": work_id, "title": str(title),
                      "authors": str(authors), "year": source.get("year"), "abstract": str(text)[:1800],
                      "representation": source.get("representation"),
                      "reader_use": "Use this record only for the background and related-work context supported by the survey."})
        literature.append({"id": f"literature-{len(literature)}", "work_id": work_id,
                           "source_ref": source_ref, "quote": str(title), "relation": "context"})
    literature.extend({"id": proof["evidence_id"], **proof, "relation": "context"} for proof in proofs)
    return {"reference_cards": cards, "literature_evidence": literature,
            "survey_assessment": assessment,
            "survey_lineage": {key: survey.get(key) for key in ("survey_ref", "assessment_ref", "state")}}


def evidence_ids_from_packet(packet):
    """Return stable evidence IDs without inventing a claim source."""
    if not isinstance(packet, dict):
        raise ValidationError("argument evidence packet must be an object")
    ids = set(packet.get("evidence_ids", [])) if isinstance(packet.get("evidence_ids", []), list) else set()
    results = packet.get("results_package") or packet.get("results") or {}
    if isinstance(results, dict):
        for key in ("procedures", "metrics", "findings"):
            for item in results.get(key, []):
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    ids.add(item["id"])
        for index, _ in enumerate(results.get("limitations", [])):
            ids.add(f"limitation-{index}")
    for key in ("literature_evidence", "evidence", "reference_cards"):
        values = packet.get(key, [])
        if isinstance(values, dict):
            values = list(values.values())
        for item in values:
            if isinstance(item, dict):
                for candidate in (item.get("id"), item.get("evidence_id"), item.get("work_id")):
                    if isinstance(candidate, str) and candidate:
                        ids.add(candidate)
    return sorted(ids)
