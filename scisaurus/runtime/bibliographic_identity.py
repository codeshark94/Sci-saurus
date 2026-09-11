"""Independent bibliographic observations and deterministic identity reconciliation."""
from __future__ import annotations

import re
import unicodedata
from html import unescape

from scisaurus.core.errors import ValidationError
from scisaurus.runtime.scores import exact


def normalize_doi(value):
    if not isinstance(value, str):
        raise ValidationError("bibliographic DOI must be a string")
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.IGNORECASE).lower()
    if not re.fullmatch(r"10\.[0-9]{4,9}/\S+", value):
        raise ValidationError("bibliographic DOI is malformed")
    return value


def normalize_title(value):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("bibliographic title must be a nonempty string")
    plain = re.sub(r"<[^>]+>", " ", unescape(value))
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", plain).casefold()))


def crossref_year(source):
    published = source.get("published")
    try:
        year = published["date-parts"][0][0]
    except (KeyError, IndexError, TypeError):
        return None
    return year if type(year) is int and 1 <= year <= 9999 else None


def openalex_observation(work, work_ref):
    return {"schema_version": "bibliographic-observation-1", "provider": "openalex",
            "provider_record_ref": work_ref, "work_id": work["work_id"], "doi": work.get("doi"),
            "title": work["title"], "year": work.get("year")}


def crossref_observation(source, execution_ref, *, work_id):
    return {"schema_version": "bibliographic-observation-1", "provider": "crossref",
            "provider_record_ref": execution_ref, "work_id": work_id,
            "doi": normalize_doi(source["doi"]), "title": source["title"],
            "year": crossref_year(source)}


def reconcile(openalex, crossref=None, *, lookup_execution_ref=None):
    """Classify one DOI identity while preserving field-level disagreement."""
    observations = [(openalex, "openalex")]
    if crossref is not None:
        observations.append((crossref, "crossref"))
    for item, provider in observations:
        exact(item, {"schema_version", "provider", "provider_record_ref", "work_id", "doi", "title", "year"},
              "bibliographic observation")
        if item["schema_version"] != "bibliographic-observation-1" or item["provider"] != provider:
            raise ValidationError("bibliographic observation provider or schema is invalid")
    if crossref is None:
        status, checks = "insufficient_evidence", [{"field": "crossref_record", "outcome": "unavailable"}]
        refs = [openalex["provider_record_ref"]]
    else:
        if openalex["work_id"] != crossref["work_id"]:
            raise ValidationError("bibliographic observations must target the same work")
        checks = [
            {"field": "doi", "outcome": "match" if normalize_doi(openalex["doi"]) == crossref["doi"] else "conflict",
             "openalex": openalex["doi"], "crossref": crossref["doi"]},
            {"field": "title", "outcome": "match" if normalize_title(openalex["title"]) == normalize_title(crossref["title"]) else "conflict",
             "openalex": openalex["title"], "crossref": crossref["title"]},
        ]
        if openalex["year"] is None or crossref["year"] is None:
            checks.append({"field": "year", "outcome": "unavailable",
                           "openalex": openalex["year"], "crossref": crossref["year"]})
        else:
            checks.append({"field": "year", "outcome": "match" if openalex["year"] == crossref["year"] else "conflict",
                           "openalex": openalex["year"], "crossref": crossref["year"]})
        status = "conflicted" if any(check["outcome"] == "conflict" for check in checks) else (
            "verified" if all(check["outcome"] == "match" for check in checks) else "verified_with_gaps")
        refs = [openalex["provider_record_ref"], crossref["provider_record_ref"]]
    return {"schema_version": "bibliographic-identity-1", "work_id": openalex["work_id"],
            "doi": openalex["doi"], "status": status, "checks": checks,
            "observation_refs": refs, "lookup_execution_ref": lookup_execution_ref}


def reconcile_result(work, work_ref, result, execution_ref):
    """Rebuild one identity decision from exact provider execution output."""
    if (not isinstance(result, dict) or result.get("outcome") not in {"ok", "empty"}
            or not isinstance(result.get("sources"), list)):
        raise ValidationError("Crossref identity requires a complete successful lookup result")
    observed = openalex_observation(work, work_ref)
    target = normalize_doi(work.get("doi"))
    matches = [source for source in result["sources"] if normalize_doi(source.get("doi")) == target]
    crossref = crossref_observation(matches[0], execution_ref, work_id=work["work_id"]) if len(matches) == 1 else None
    identity = reconcile(observed, crossref, lookup_execution_ref=execution_ref)
    if len(matches) > 1:
        identity["status"] = "conflicted"
        identity["checks"] = [{"field": "crossref_record", "outcome": "conflict", "matches": len(matches)}]
    return identity
