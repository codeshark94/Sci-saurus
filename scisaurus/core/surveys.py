"""Transactional survey acceptance and exact-source assessment prerequisites."""

from __future__ import annotations

import base64
import json
import math
import re
import unicodedata

from scisaurus.core.errors import ConflictError, ValidationError
from scisaurus.core.schema import canonical_bytes, json_object, parse_ref, sha256_hex
from scisaurus.core.source_spans import (bind as bind_source_spans, expand_evidence,
                                        validate as validate_source_span)
from scisaurus.runtime.bibliographic_identity import normalize_doi, reconcile_result
from scisaurus.runtime.operation_adapters import get_adapter


SURVEY_CHECKS = frozenset({"coverage-accounting", "source-fidelity", "map-support"})
WORK_CHECKS = ("inclusion", "reason", "problem", "approach", "finding", "limitations")
ABSTENTION_REASONS = {
    "source_unavailable": "The catalog record is relevant by metadata, but no abstract or verified full text was available, so substantive content could not be assessed.",
    "deep_analysis_budget": "This catalog record is deferred by the declared deep-analysis budget. No substantive claim is admitted; targeted follow-up may expand this scope.",
    "contract_exhausted": "The bounded extraction did not produce a valid evidence contract; substantive claims remain unknown.",
    "screening_unresolved": "The captured evidence does not resolve the screening rationale.",
    "review_exhausted": "Focused review remains unresolved after bounded revision. This work is not admitted as support for scientific assertions; its prior analysis and review remain retained.",
}


def is_explicit_abstention(entry, record):
    """A mechanical review is valid only when no scientific assertion remains."""
    return (record.get("scope") in ABSTENTION_REASONS
            and entry.get("inclusion") == "uncertain"
            and entry.get("reason") in ABSTENTION_REASONS.values()
            and record.get("work_id") == entry.get("work_id")
            and record.get("entry_sha256") == sha256_hex(canonical_bytes(entry))
            and all(entry.get(field) == {"text": None, "evidence": []} for field in WORK_CHECKS[2:]))


RELATIONSHIP_SEMANTICS = {
    "extends": "The source work builds on or extends the target work; the direction is source to target, never the inverse.",
    "contradicts": "The source work reports findings incompatible with the target work under comparable scope.",
    "compares": "An analyst comparison supported by evidence from both works; it does not assert that the source explicitly cites the target.",
    "related": "A supported topical connection between the works without a claim of inheritance.",
}
ASSESSMENT_CHECKS = frozenset({
    "closest-prior-work", "scope-comparability", "counterevidence", "full-text-support",
})
ASSESSMENT_STATES = frozenset({
    "refuted_by_prior_work", "insufficient_evidence", "eligible_for_experiment",
})


def work_review_checks(relationship_refs):
    """Name every entry field and exact outgoing relationship requiring review."""
    return (*WORK_CHECKS, *(f"relationship:{ref}" for ref in relationship_refs))


class SurveyGate:
    """Accept independently checked surveys and fence their downstream decisions.

    All reads use the writer's connection. Acceptance pins both immutable bytes
    and the latest published version of every governing artifact; publishing
    new evidence revokes the prerequisite even before that evidence is adopted.
    """

    def __init__(self, control, store):
        if store.control is not control:
            raise ValidationError("survey gate and artifact store must share the control connection")
        self.control, self.store = control, store

    @staticmethod
    def _guard(guard):
        if guard is not None and guard() is False:
            raise ValidationError("survey commit guard rejected the operation")

    @staticmethod
    def _text(value, name):
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{name} must be a nonempty string")
        return value

    @staticmethod
    def _json(raw, name):
        return json_object(raw, name)

    def _artifact(self, ref, *, current=True):
        if not isinstance(ref, str):
            raise ValidationError("survey dependency must be an exact artifact reference")
        parse_ref(ref)
        manifest = self.store.get(ref)
        row = self.control._conn.execute(
            "SELECT manifest_hash FROM artifacts WHERE artifact_ref=?", (ref,),
        ).fetchone()
        if (row is None or manifest["artifact_ref"] != ref
                or row["manifest_hash"] != sha256_hex(canonical_bytes(manifest))):
            raise ValidationError(f"artifact manifest integrity failed: {ref}")
        if current and self.store.head(manifest["artifact_id"])["artifact_ref"] != ref:
            raise ConflictError(f"survey prerequisite has a newer published version: {ref}")
        if not manifest.get("body_hash"):
            raise ValidationError(f"survey prerequisite has no body: {ref}")
        raw = self.store.read_body(manifest["body_hash"])
        if (sha256_hex(raw) != manifest["body_hash"]
                or len(raw) != manifest["body_size_bytes"]):
            raise ValidationError(f"artifact body integrity failed: {ref}")
        return manifest, raw

    def _note(self, ref):
        manifest, raw = self._artifact(ref)
        if manifest["artifact_type"] != "note":
            raise ValidationError(f"survey control artifact must be a note: {ref}")
        return manifest, self._json(raw, ref)

    def _refs(self, value, name):
        if not isinstance(value, list):
            raise ValidationError(f"{name} must be an explicit list of exact artifact references")
        logical_ids = set()
        for ref in value:
            if not isinstance(ref, str):
                raise ValidationError(f"{name} must contain exact artifact references")
            namespace, logical, _ = parse_ref(ref)
            identity = (namespace, logical)
            if identity in logical_ids:
                raise ValidationError(f"{name} contains duplicate artifact identities")
            logical_ids.add(identity)
        return value

    def _survey(self, survey_ref):
        manifest, body = self._note(survey_ref)
        if body.get("schema_version") not in {"literature-survey-2", "literature-survey-3"}:
            raise ValidationError("unsupported literature survey schema")
        scalar = [body.get(key) for key in ("score_ref", "protocol_ref", "map_ref", "coverage_ref")]
        list_fields = ["source_refs", "work_refs", "query_refs", "work_review_refs"]
        if body["schema_version"] == "literature-survey-3":
            list_fields.append("identity_refs")
        listed = [ref for key in list_fields
                  for ref in self._refs(body.get(key), key)]
        required = self._refs([*scalar, *listed], "survey governing references")
        dependencies = self._refs(body.get("dependency_refs"), "dependency_refs")
        if not set(required).issubset(dependencies) or survey_ref in dependencies:
            raise ValidationError("survey dependencies must pin every governing artifact")
        pins = []
        for ref in dependencies:
            item, _ = self._artifact(ref)
            pins.append({"ref": ref, "body_hash": item["body_hash"]})
        if body["schema_version"] == "literature-survey-3":
            self._bibliographic_identities(body)
        return manifest, body, pins

    def _bibliographic_identities(self, survey):
        identities, works = {}, {}
        dependencies = set(survey["dependency_refs"])
        for ref in survey["work_refs"]:
            _, raw = self._artifact(ref)
            work = self._json(raw, "survey work")
            work_id = self._text(work.get("work_id"), "survey work_id")
            if work_id in works:
                raise ValidationError("survey work identities must be unique")
            works[work_id] = (ref, work)
        for ref in survey["identity_refs"]:
            manifest, raw = self._artifact(ref)
            body = self._json(raw, "bibliographic identity")
            if (manifest["artifact_type"] != "reference_card"
                    or body.get("schema_version") != "bibliographic-identity-1"
                    or body.get("status") not in {"verified", "verified_with_gaps", "conflicted", "insufficient_evidence"}):
                raise ValidationError("survey bibliographic identity record is invalid")
            work_id = self._text(body.get("work_id"), "bibliographic identity work_id")
            if work_id in identities:
                raise ValidationError("survey has duplicate bibliographic identity records")
            observation_refs = self._refs(body.get("observation_refs"), "bibliographic observation_refs")
            lookup = body.get("lookup_execution_ref")
            if (not set(observation_refs).issubset(dependencies) or not isinstance(lookup, str)
                    or lookup not in dependencies or work_id not in works):
                raise ValidationError("bibliographic identity inputs must be pinned by survey dependencies")
            execution, _ = self._artifact(lookup)
            _, _, result, params = self._recorded_execution(
                lookup, execution["author"], operation="crossref", task_kinds={"retrieval"})
            operational_checks, _ = get_adapter("crossref").inspect_result(
                {"adapter": "crossref"}, result, params, representative=False)
            work_ref, work = works[work_id]
            if (normalize_doi(params.get("query")) != normalize_doi(work.get("doi"))
                    or result.get("metadata", {}).get("match_mode") != "exact_doi"
                    or not all(check["outcome"] == "passed" for check in operational_checks)
                    or canonical_bytes(body) != canonical_bytes(
                        reconcile_result(work, work_ref, result, lookup))):
                raise ValidationError("bibliographic identity does not match its exact provider executions")
            identities[work_id] = ref

    def _passed_checks(self, checks, required, name):
        if not isinstance(checks, list) or len(checks) != len(required):
            raise ValidationError(f"{name} must report every required check exactly once")
        identifiers = []
        for check in checks:
            if not isinstance(check, dict):
                raise ValidationError(f"{name} checks must be objects")
            for key in ("check_id", "outcome", "method", "result"):
                self._text(check.get(key), f"review check {key}")
            if check["outcome"] != "passed":
                raise ValidationError(f"{name} did not pass every required check")
            identifiers.append(check["check_id"])
        if set(identifiers) != set(required):
            raise ValidationError(f"{name} check IDs are missing, duplicated, or unknown")

    def _work_reviews(self, survey):
        _, mapped = self._note(survey["map_ref"])
        entry_refs = self._refs(mapped.get("entry_refs"), "map entry_refs")
        relationship_refs = self._refs(mapped.get("relationship_refs"), "map relationship_refs")
        dependencies = set(survey["dependency_refs"])
        if not set([*entry_refs, *relationship_refs]).issubset(dependencies):
            raise ValidationError("survey dependencies must pin every map entry and relationship")
        entries, work_ids = {}, set()
        for ref in entry_refs:
            entry, body = self._note(ref)
            work_id = self._text(body.get("work_id"), "map entry work_id")
            if work_id in work_ids:
                raise ValidationError("survey map must contain exactly one entry per work")
            work_ids.add(work_id)
            entries[ref] = (entry, body)
        registered = set()
        for ref in survey["work_refs"]:
            _, raw = self._artifact(ref)
            work = self._json(raw, "survey work")
            work_id = self._text(work.get("work_id"), "survey work_id")
            if work_id in registered:
                raise ValidationError("survey work identities must be unique")
            registered.add(work_id)
        if registered != work_ids:
            raise ValidationError("survey map must contain exactly one entry for every registered work")
        outgoing = {work_id: set() for work_id in work_ids}
        relationship_bodies = {}
        for ref in relationship_refs:
            _, body = self._note(ref)
            source = self._text(body.get("source"), "map relationship source")
            if source not in outgoing:
                raise ValidationError("every map relationship requires a valid source entry")
            target = self._text(body.get("target"), "map relationship target")
            if target not in work_ids:
                raise ValidationError("every map relationship requires a valid target entry")
            outgoing[source].add(ref)
            relationship_bodies[ref] = body
        reviewed, evidence = set(), []
        for ref in survey["work_review_refs"]:
            review, body = self._note(ref)
            entry_ref = body.get("entry_ref")
            if not isinstance(entry_ref, str) or entry_ref not in entries or entry_ref in reviewed:
                raise ValidationError("focused work review must bind exactly one current map entry")
            entry, entry_body = entries[entry_ref]
            work_id = entry_body["work_id"]
            if review["author"] == entry["author"]:
                raise ValidationError("focused work review requires an independent reviewer")
            relationships = self._refs(body.get("relationship_refs"), "work review relationship_refs")
            if set(relationships) != outgoing[work_id]:
                raise ValidationError("focused work review must cover every exact outgoing relationship")
            checks = body.get("checks")
            self._passed_checks(checks, work_review_checks(relationships), "focused work review")
            self._text(body.get("rationale"), "work review rationale")
            if body.get("verification_kind") == "deterministic_abstention":
                execution, abstention = self._note(body.get("execution_ref"))
                if (relationships or execution["author"] != "command.controller"
                        or execution["artifact_id"] != f"command/survey-abstentions/{work_id}"
                        or execution["artifact_ref"] not in dependencies
                        or not is_explicit_abstention(entry_body, abstention)):
                    raise ValidationError("deterministic abstention review cannot admit substantive or unbound claims")
                reviewed.add(entry_ref)
                evidence.extend((review, execution))
                continue
            execution, context, prompt, reply = self._model_review_execution(body.get("execution_ref"), review["author"])
            if execution["artifact_ref"] not in dependencies:
                raise ValidationError("survey dependencies must pin every focused review execution")
            if prompt.get("entry_ref") != entry_ref or prompt.get("relationship_refs") != relationships:
                raise ValidationError("focused review dispatch must inspect the exact entry and relationships")
            self._work_review_context(survey, prompt, entry_body,
                [{**relationship_bodies[ref], "artifact_ref": ref} for ref in relationships])
            if reply.get("checks") != checks or reply.get("rationale") != body["rationale"]:
                raise ValidationError("focused work review does not match the completed model reply")
            reviewed.add(entry_ref)
            evidence.extend((review, execution, context))
        if reviewed != set(entry_refs):
            raise ValidationError("survey acceptance requires one focused work review for every map entry")
        return evidence

    def _work_review_context(self, survey, prompt, entry, relationships):
        if canonical_bytes(prompt.get("relationship_semantics")) != canonical_bytes(RELATIONSHIP_SEMANTICS):
            raise ValidationError("focused review prompt must contain the shared relationship semantics")
        if (canonical_bytes(prompt.get("entry")) != canonical_bytes(entry)
                or canonical_bytes(prompt.get("relationships")) != canonical_bytes(relationships)):
            raise ValidationError("focused review prompt must contain the exact entry and relationship bodies")
        sources = prompt.get("sources")
        if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
            raise ValidationError("focused review sources must be explicit source context objects")
        refs = self._refs([source.get("source_ref") for source in sources], "focused review source_refs")
        if not set(refs).issubset(survey["source_refs"]):
            raise ValidationError("focused review sources must be pinned by the survey source_refs")
        visible = {}
        for source in sources:
            manifest, raw = self._artifact(source["source_ref"])
            captured = self._json(raw, "focused review source capture")
            text = captured.get("text")
            work_id = self._text(captured.get("work_id"), "source capture work_id")
            representation = self._text(captured.get("representation"), "source capture representation")
            if (manifest["artifact_type"] != "source_capture" or not isinstance(text, str)
                    or source.get("work_id") != work_id or source.get("representation") != representation):
                raise ValidationError("focused review source context must match its captured work and representation")
            window = source.get("window")
            if (type(source.get("available_chars")) is not int or source["available_chars"] != len(text)
                    or not isinstance(window, dict) or set(window) != {"start", "end"}
                    or any(type(window[key]) is not int for key in ("start", "end"))
                    or not 0 <= window["start"] <= window["end"] <= len(text)
                    or source.get("text") != text[window["start"]:window["end"]]):
                raise ValidationError("focused review source window must contain the exact captured text slice")
            visible[source["source_ref"]] = {**source, "text": text}
        for field in WORK_CHECKS[2:]:
            self._visible_evidence(entry.get(field), visible, work_id=entry["work_id"],
                                   require_spans=survey["schema_version"] == "literature-survey-3")
        for relationship in relationships:
            self._visible_evidence(relationship.get("claim"), visible,
                                   require_spans=survey["schema_version"] == "literature-survey-3")

    def _visible_evidence(self, statement, sources, *, work_id=None, require_spans=False):
        if not isinstance(statement, dict) or set(statement) != {"text", "evidence"}:
            raise ValidationError("focused review statements must contain explicit text and evidence")
        evidence = statement["evidence"]
        if not isinstance(evidence, list):
            raise ValidationError("focused review evidence must be an explicit list")
        if statement["text"] is None:
            if evidence:
                raise ValidationError("unknown focused review statements cannot claim evidence")
            return
        self._text(statement["text"], "focused review statement")
        if not evidence:
            raise ValidationError("asserted focused review statements require visible source evidence")
        for proof in evidence:
            if not isinstance(proof, dict):
                raise ValidationError("focused review evidence must contain source quotation objects")
            for key in ("source_ref", "work_id", "quote"):
                self._text(proof.get(key), f"focused review evidence {key}")
            source = sources.get(proof["source_ref"])
            if (source is None or source["work_id"] != proof["work_id"]
                    or work_id is not None and proof["work_id"] != work_id):
                raise ValidationError("focused review evidence must identify the correct visible source and work")
            validate_source_span(proof, source, require_span=require_spans, window=source["window"])

    def _review(self, survey, review_ref):
        review, body = self._note(review_ref)
        if review["author"] == survey["author"]:
            raise ValidationError("survey acceptance requires an independent reviewer")
        if body.get("survey_ref") != survey["artifact_ref"]:
            raise ValidationError("survey review must bind the exact survey version")
        checks = body.get("checks")
        self._passed_checks(checks, SURVEY_CHECKS, "survey review")
        self._text(body.get("rationale"), "review rationale")
        execution, context, reply = self._execution(body.get("execution_ref"), review["author"], survey["artifact_ref"])
        if reply.get("checks") != checks or reply.get("rationale") != body["rationale"]:
            raise ValidationError("survey review does not match the completed model reply")
        return review, execution, context

    def _execution(self, execution_ref, author, survey_ref):
        execution, context, prompt, reply = self._model_review_execution(execution_ref, author)
        if prompt.get("survey_ref") != survey_ref:
            raise ValidationError("review dispatch did not inspect the exact survey version")
        return execution, context, reply

    def _model_review_execution(self, execution_ref, author):
        execution, context, result, params = self._recorded_execution(
            execution_ref, author, operation="model", task_kinds={"review", "verification"},
        )
        prompt = self._json(params.get("prompt"), "review prompt")
        reply = json_object(result.get("text"), "review model reply", model_envelope=True)
        if result.get("finish_reason") != "stop":
            raise ValidationError("review model reply did not finish normally")
        return execution, context, prompt, reply

    def _recorded_execution(self, execution_ref, author, *, operation, task_kinds):
        execution, raw = self._artifact(execution_ref)
        prefix = "command/executions/"
        if (execution["artifact_type"] != "report" or not execution["artifact_id"].startswith(prefix)
                or execution["author"] != author):
            raise ValidationError("review requires its reviewer's recorded model execution")
        task_id = execution["artifact_id"][len(prefix):]
        task = self.control._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        attempt = self.control._conn.execute(
            "SELECT * FROM attempts WHERE task_id=? ORDER BY lease_fence DESC LIMIT 1", (task_id,),
        ).fetchone()
        if (task is None or task["kind"] not in task_kinds
                or task["state"] not in {"awaiting_review", "completed"}
                or self._json(task["payload_json"], "review task").get("operation") != operation
                or attempt is None or attempt["state"] != "succeeded"
                or attempt["lease_owner"] != author):
            raise ValidationError("review execution has no successful independent model attempt")
        usage = self._json(attempt["usage_json"], "execution usage").get("actual", {})
        quantity = usage.get("model_calls" if operation == "model" else "retrieval_calls") if isinstance(usage, dict) else None
        if type(quantity) not in (int, float) or not math.isfinite(quantity) or quantity < 1:
            raise ValidationError("execution must retain actual attributed call usage")
        subjects = [item["ref"] for item in execution["inputs"] if item.get("purpose") == "subject"]
        if len(subjects) != 1:
            raise ValidationError("review execution must pin its exact dispatch context")
        context, context_raw = self._artifact(subjects[0])
        if (context["artifact_id"] != f"command/contexts/{task_id}"
                or context["author"] != author or context["artifact_type"] != "note"):
            raise ValidationError("review execution context does not belong to the reviewer task")
        started, finished, published = None, None, {}
        rows = self.control._conn.execute(
            "SELECT seq,event_type,payload_json FROM events WHERE event_type IN "
            "('attempt.started','attempt.finished','artifact.published') ORDER BY seq",
        )
        for row in rows:
            event = self._json(row["payload_json"], "execution provenance event")
            if event.get("attempt_id") == attempt["attempt_id"]:
                if row["event_type"] == "attempt.started":
                    started = row["seq"]
                elif event.get("outcome") == "succeeded":
                    finished = row["seq"]
            ref = event.get("artifact_ref")
            if ref in {execution_ref, context["artifact_ref"]}:
                published[ref] = row["seq"]
        context_seq, execution_seq = published.get(context["artifact_ref"]), published.get(execution_ref)
        if (any(value is None for value in (started, finished, context_seq, execution_seq))
                or not started < context_seq < execution_seq < finished):
            raise ValidationError("execution record was not published during its successful attempt")
        return execution, context, self._json(raw, "execution result"), self._json(context_raw, "execution context")

    def _acceptance(self, survey_ref, review_ref):
        survey, body, dependencies = self._survey(survey_ref)
        work_evidence = self._work_reviews(body)
        review, execution, context = self._review(survey, review_ref)
        return survey, {
            "survey_ref": survey_ref, "review_ref": review_ref,
            "execution_ref": execution["artifact_ref"], "dependency_pins": dependencies,
            "evidence_pins": [{"ref": item["artifact_ref"], "body_hash": item["body_hash"]}
                              for item in (survey, review, execution, context, *work_evidence)],
        }

    def accept(self, survey_ref, review_ref, *, author, expected_version=None, guard=None):
        self._text(author, "acceptance author")
        with self.control.tx() as conn:
            self._guard(guard)
            survey, payload = self._acceptance(survey_ref, review_ref)
            self._guard(guard)
            accepted = self.store.adopt(
                survey["artifact_id"], target_version=survey["version"],
                expected_accepted_version=expected_version, actor=author, conn=conn,
            )
            self._guard(guard)
            self.control.append_event(conn, actor=author, event_type="survey.accepted", payload=payload)
            self.require_current(survey_ref)
            self._guard(guard)
            return accepted

    def require_current(self, survey_ref):
        """Return the accepted manifest only while the recorded acceptance holds."""
        survey, _ = self._note(survey_ref)
        accepted = self.store.accepted(survey["artifact_id"])
        if accepted is None or accepted["artifact_ref"] != survey_ref:
            raise ValidationError("gap assessment requires an accepted current survey")
        rows = self.control._conn.execute(
            "SELECT payload_json FROM events WHERE event_type='survey.accepted' ORDER BY seq DESC",
        ).fetchall()
        for row in rows:
            payload = self._json(row["payload_json"], "survey acceptance event")
            if payload.get("survey_ref") == survey_ref:
                current, expected = self._acceptance(survey_ref, payload.get("review_ref"))
                if expected != payload:
                    raise ValidationError("survey acceptance evidence no longer matches its recorded pins")
                return current
        raise ValidationError("survey has no authoritative independent acceptance record")

    def require_current_nomination(self, nomination_ref):
        """Validate a current candidate and its historical survey prerequisite.

        Counter-search can supersede the originating survey without changing
        the question being assessed. The candidate itself remains versioned.
        """
        self._text(nomination_ref, "assessment nomination_ref")
        _, body = self._note(nomination_ref)
        for key in ("id", "statement", "survey_ref"):
            self._text(body.get(key), f"nomination {key}")
        origin, raw = self._artifact(body["survey_ref"], current=False)
        if (origin["artifact_type"] != "note"
                or self._json(raw, "nomination survey").get("schema_version") not in
                {"literature-survey-2", "literature-survey-3"}):
            raise ValidationError("nomination must originate from an accepted literature survey")
        acceptance = self._accepted_event("survey.accepted", "survey_ref", body["survey_ref"])
        pins = acceptance.get("evidence_pins", [])
        if not isinstance(pins, list) or not any(
                pin == {"ref": body["survey_ref"], "body_hash": origin["body_hash"]} for pin in pins):
            raise ValidationError("nomination survey does not match its historical acceptance")
        return body

    def _accepted_event(self, event_type, field, ref):
        rows = self.control._conn.execute(
            "SELECT payload_json FROM events WHERE event_type=? ORDER BY seq DESC", (event_type,),
        )
        for row in rows:
            payload = self._json(row["payload_json"], event_type)
            if payload.get(field) == ref:
                return payload
        raise ValidationError(f"{ref} has no authoritative {event_type} record")

    def _assessment(self, assessment_ref, survey_ref):
        assessment, body = self._note(assessment_ref)
        if body.get("survey_ref") != survey_ref or body.get("state") not in ASSESSMENT_STATES:
            raise ValidationError("assessment must bind its accepted survey and a supported state")
        survey_manifest, survey, _ = self._survey(survey_ref)
        if assessment["author"] == survey_manifest["author"]:
            raise ValidationError("gap assessment requires an independent reviewer")
        self._text(body.get("rationale"), "assessment rationale")
        nomination = self.require_current_nomination(body.get("nomination_ref"))
        if self.store.get(body["nomination_ref"])["author"] == assessment["author"]:
            raise ValidationError("gap assessment requires a reviewer independent of the nomination author")
        if parse_ref(nomination["survey_ref"])[:2] != parse_ref(survey_ref)[:2]:
            raise ValidationError("assessment and nomination must share the same logical survey")
        _, context, reply = self._execution(body.get("execution_ref"), assessment["author"], survey_ref)
        context_body = self._json(self.store.read_body(context["body_hash"]), "assessment context")
        prompt = self._json(context_body.get("prompt"), "assessment prompt")
        if (prompt.get("nomination_ref") != body["nomination_ref"]
                or prompt.get("gap") != {key: nomination[key] for key in ("id", "statement")}):
            raise ValidationError("assessment dispatch must bind the exact nomination and gap statement")
        if survey["schema_version"] == "literature-survey-3":
            source_values, windows = {}, {}
            supplied = prompt.get("sources")
            if not isinstance(supplied, list):
                raise ValidationError("assessment dispatch must contain explicit source windows")
            for context in supplied:
                if not isinstance(context, dict) or context.get("source_ref") not in survey["source_refs"]:
                    raise ValidationError("assessment dispatch source is not pinned by the survey")
                _, raw = self._artifact(context["source_ref"])
                source = self._json(raw, "assessment source capture")
                window = context.get("window")
                if (not isinstance(window, dict) or set(window) != {"start", "end"}
                        or any(type(window.get(key)) is not int for key in ("start", "end"))
                        or not 0 <= window["start"] <= window["end"] <= len(source.get("text", ""))
                        or context.get("text") != source["text"][window["start"]:window["end"]]):
                    raise ValidationError("assessment dispatch source window is not an exact captured slice")
                source_values[context["source_ref"]] = source
                windows[context["source_ref"]] = window
            reply = expand_evidence(reply, prompt.get("evidence_catalog", []), source_values, windows=windows)
            # A model may copy a sentence from the abstract while attaching
            # the same work's full-text reference (or the inverse).  The
            # runner repairs this unambiguous representation mismatch before
            # publishing the candidate, so the acceptance gate must apply the
            # identical repair to the immutable raw reply.  Otherwise a
            # scientifically valid candidate can be published and then be
            # rejected solely because its transport-level source label was
            # broader than the quotation's actual representation.
            reply = self._rebind_assessment_sources(reply, source_values, windows)
            reply = bind_source_spans(reply, source_values, windows=windows)
        fields = ("state", "rationale", "comparisons", "checks", "evidence")
        if any(key not in body or reply.get(key) != body[key] for key in fields):
            raise ValidationError("assessment does not match its recorded model reply")
        if not isinstance(body["comparisons"], list):
            raise ValidationError("assessment comparisons must be an explicit list")
        evidence = body.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValidationError("assessment evidence must be a list")
        decisive = body["state"] in {"eligible_for_experiment", "refuted_by_prior_work"}
        checks = body["checks"]
        if not isinstance(checks, list) or len(checks) != len(ASSESSMENT_CHECKS):
            raise ValidationError("assessment must report every required check exactly once")
        identifiers = []
        for check in checks:
            if not isinstance(check, dict):
                raise ValidationError("assessment checks must be objects")
            for key in ("check_id", "outcome", "method", "result"):
                self._text(check.get(key), f"assessment check {key}")
            if check["outcome"] not in {"passed", "failed", "insufficient_evidence", "check_failed"}:
                raise ValidationError("assessment check outcome is unsupported")
            if decisive and check["outcome"] != "passed":
                raise ValidationError("decisive gap assessment requires every check to pass")
            identifiers.append(check["check_id"])
        if set(identifiers) != ASSESSMENT_CHECKS:
            raise ValidationError("assessment check IDs are missing, duplicated, or unknown")
        if decisive and not evidence:
            raise ValidationError("decisive gap assessment requires full-text evidence")
        works = {}
        for ref in survey["work_refs"]:
            _, raw = self._artifact(ref)
            work = self._json(raw, "survey work")
            work_id = self._text(work.get("work_id"), "survey work_id")
            if work_id in works:
                raise ValidationError("survey work identities must be unique")
            works[work_id] = work
        self._evidence(evidence, works, survey, full_text=decisive)
        comparisons, seen = body["comparisons"], set()
        for comparison in comparisons:
            if not isinstance(comparison, dict):
                raise ValidationError("assessment comparisons must contain objects")
            for key in ("work_id", "relationship", "statement"):
                self._text(comparison.get(key), f"comparison {key}")
            work_id, relationship = comparison["work_id"], comparison["relationship"]
            if work_id not in works or work_id in seen:
                raise ValidationError("comparison work identities must be registered and unique")
            seen.add(work_id)
            if relationship not in {"solves", "partial", "different", "uncertain"}:
                raise ValidationError("assessment comparison relationship is unsupported")
            full_text = body["state"] == "eligible_for_experiment" or (
                body["state"] == "refuted_by_prior_work" and relationship == "solves")
            self._evidence(comparison.get("evidence"), works, survey,
                           required=relationship != "uncertain" or full_text,
                           full_text=full_text, work_id=work_id)
        if body["state"] == "refuted_by_prior_work" and not any(
                item["relationship"] == "solves" for item in comparisons):
            raise ValidationError("refutation requires a full-text-supported prior solution")
        if body["state"] == "eligible_for_experiment" and (not comparisons or any(
                item["relationship"] in {"solves", "uncertain"} for item in comparisons)):
            raise ValidationError("eligibility requires resolved comparisons without a known solution")
        return assessment, body

    @staticmethod
    def _rebind_assessment_sources(value, sources, windows):
        """Repair only a unique quote/source mismatch within one work.

        The quotation remains authoritative.  Rebinding is permitted only
        when the exact quote occurs once in exactly one other displayed source
        for the same work and within that source's recorded window.  Ambiguous
        or unsupported text is left untouched so the normal span validator
        still fails closed.
        """
        value = json.loads(json.dumps(value, ensure_ascii=False))
        by_work = {}
        for ref, source in sources.items():
            by_work.setdefault(source.get("work_id"), []).append((ref, source))

        def repair(items):
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                quote, ref, work_id = item.get("quote"), item.get("source_ref"), item.get("work_id")
                if not all(isinstance(part, str) for part in (quote, ref, work_id)):
                    continue
                source = sources.get(ref)
                window = windows.get(ref, {})
                if source is not None and isinstance(source.get("text"), str):
                    start, end = window.get("start"), window.get("end")
                    visible = source["text"][start:end] if type(start) is int and type(end) is int else ""
                    if visible.count(quote) == 1:
                        continue
                    # Preserve the supplied representation when the only
                    # difference is renderer whitespace or typography.  The
                    # shared binder returns the exact source slice and keeps
                    # decisive full-text evidence attached to full text.
                    try:
                        bound = bind_source_spans({"evidence": [item]}, sources, windows=windows)
                        item.clear()
                        item.update(bound["evidence"][0])
                        continue
                    except ValidationError:
                        pass
                candidates = []
                for candidate_ref, candidate in by_work.get(work_id, []):
                    candidate_window = windows.get(candidate_ref, {})
                    start, end = candidate_window.get("start"), candidate_window.get("end")
                    if type(start) is not int or type(end) is not int:
                        continue
                    visible = candidate.get("text", "")[start:end]
                    if visible.count(quote) == 1:
                        candidates.append(candidate_ref)
                if len(candidates) == 1:
                    item["source_ref"] = candidates[0]

        repair(value.get("evidence"))
        for comparison in value.get("comparisons", []):
            if isinstance(comparison, dict):
                repair(comparison.get("evidence"))
        return value

    def _evidence(self, evidence, works, survey, *, required=False, full_text=False, work_id=None):
        if not isinstance(evidence, list) or required and not evidence:
            raise ValidationError("asserted comparisons require explicit evidence from their own work")
        verified_full_text = False
        for item in evidence:
            if not isinstance(item, dict):
                raise ValidationError("assessment evidence must contain objects")
            for key in ("work_id", "source_ref", "quote"):
                self._text(item.get(key), f"assessment evidence {key}")
            if item["work_id"] not in works or item["source_ref"] not in survey["source_refs"]:
                raise ValidationError("assessment evidence must be pinned by the accepted survey")
            if work_id is not None and item["work_id"] != work_id:
                raise ValidationError("comparison evidence must come from that comparison's own work")
            _, raw = self._artifact(item["source_ref"])
            source = self._json(raw, "assessment source")
            if source.get("work_id") != item["work_id"] or not isinstance(source.get("text"), str):
                raise ValidationError("assessment evidence identity or exact quotation is unsupported")
            validate_source_span(item, source,
                                 require_span=survey["schema_version"] == "literature-survey-3")
            if full_text and source.get("representation") == "full_text":
                self._full_text(source, works[item["work_id"]], survey)
                verified_full_text = True
        if full_text and not verified_full_text:
            raise ValidationError("decisive evidence requires identity-verified full text from the compared work")

    def _full_text(self, source, work, survey):
        if source.get("representation") != "full_text" or source.get("identity_verified") is not True:
            raise ValidationError("decisive gap assessment requires identity-verified full text")
        identity = source.get("identity_checks")
        if (not isinstance(identity, dict) or identity.get("title_match") is not True
                or not isinstance(identity.get("section_markers"), list) or not identity["section_markers"]):
            raise ValidationError("full text requires recorded title and section identity checks")
        def normalize(value):
            return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))
        text = normalize(source["text"])
        title = normalize(self._text(work.get("title"), "registered work title"))
        if not title or title not in text:
            raise ValidationError("full text does not contain the registered work title")
        for marker in identity["section_markers"]:
            marker = normalize(self._text(marker, "full-text section marker"))
            if not marker or marker not in text:
                raise ValidationError("full text is missing a verified section marker")
        if source.get("execution_ref") not in survey["dependency_refs"]:
            raise ValidationError("full-text execution must be pinned by the accepted survey")
        execution, _ = self._artifact(source["execution_ref"])
        _, _, result, params = self._recorded_execution(
            source["execution_ref"], execution["author"], operation="fetch", task_kinds={"retrieval"},
        )
        metadata = result.get("metadata", {})
        if (result.get("outcome") != "ok" or result.get("text") != source["text"]
                or params.get("url") != self._text(source.get("url"), "full-text URL")
                or not isinstance(metadata, dict)
                or metadata.get("representation") != "extracted_text"
                or metadata.get("provider") != "mcp-fetch" or metadata.get("transport") != "mcp_stdio"
                or metadata.get("capture_truncated") or metadata.get("capture_incomplete")):
            raise ValidationError("full text must match a complete successful MCP Fetch execution")
        capture = result.get("capture")
        try:
            raw = base64.b64decode(capture["body"], validate=True)
            valid = (capture["encoding"] == "base64" and capture["bytes"] == len(raw)
                     and raw == source["text"].encode("utf-8")
                     and sha256_hex(raw) == capture["sha256"] == result.get("capture_sha256"))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValidationError("full-text capture bytes do not match the execution integrity record")

    def commit_assessment(self, assessment_ref, *, survey_ref, author, guard=None):
        self._text(author, "assessment acceptance author")
        with self.control.tx() as conn:
            self._guard(guard)
            self.require_current(survey_ref)
            assessment, body = self._assessment(assessment_ref, survey_ref)
            self._guard(guard)
            self.require_current(survey_ref)
            self.require_current_nomination(body["nomination_ref"])
            previous = self.store.accepted(assessment["artifact_id"])
            accepted = self.store.adopt(
                assessment["artifact_id"], target_version=assessment["version"],
                expected_accepted_version=previous["version"] if previous else None,
                actor=author, conn=conn,
            )
            self._guard(guard)
            self.control.append_event(conn, actor=author, event_type="assessment.accepted",
                                      payload=self._assessment_acceptance(assessment, body))
            self.require_current_assessment(assessment_ref)
            self._guard(guard)
            return accepted

    @staticmethod
    def _assessment_acceptance(assessment, body):
        return {"assessment_ref": assessment["artifact_ref"], "survey_ref": body["survey_ref"],
                "nomination_ref": body["nomination_ref"], "state": body["state"],
                "body_hash": assessment["body_hash"]}

    def require_current_assessment(self, assessment_ref):
        """Return a decision only while its survey and nominated question hold."""
        assessment, body = self._note(assessment_ref)
        accepted = self.store.accepted(assessment["artifact_id"])
        if accepted is None or accepted["artifact_ref"] != assessment_ref:
            raise ValidationError("assessment requires an accepted current decision")
        self.require_current(body.get("survey_ref"))
        current, body = self._assessment(assessment_ref, body["survey_ref"])
        acceptance = self._accepted_event("assessment.accepted", "assessment_ref", assessment_ref)
        if acceptance != self._assessment_acceptance(current, body):
            raise ValidationError("assessment no longer matches its authoritative acceptance")
        return current
