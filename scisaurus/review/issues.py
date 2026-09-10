"""Stable issue identity and the challenge→respond→adjudicate→verify cycle.

Implements docs/40-execution-contract.md §6 and SSOT D13/D20: critiques must
be admissible (exact target, criterion, basis, material impact, resolution
condition); responses may accept, rebut, or request evidence; adjudication is
non-conflicted; closure requires a passing verification artifact. Stable issue
identity deduplicates across revisions via a causal key — artifact versions
alone never mint new issues.
"""

from __future__ import annotations

import json

from scisaurus.core.errors import NotFoundError, StateError, ValidationError
from scisaurus.core.schema import canonical_bytes, now_iso, sha256_hex, parse_ref
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore

CRITIQUE_SEVERITIES = frozenset({"blocking", "major", "minor"})
STANCES = frozenset({"accept", "rebut", "request_evidence"})
CHECK_KINDS = frozenset({"resolution", "regression"})
CHECK_OUTCOMES = frozenset({"passed", "failed", "insufficient_evidence", "check_failed"})

ISSUE_TRANSITIONS = {
    "registered": {"triaged", "rejected_invalid"},
    "triaged": {"awaiting_response"},
    "awaiting_response": {"repair_submitted", "adjudication_pending", "needs_evidence"},
    "needs_evidence": {"repair_submitted", "adjudication_pending"},
    "adjudication_pending": {"upheld_open", "rejected_invalid", "rebutted", "needs_evidence"},
    "upheld_open": {"repair_submitted"},
    "repair_submitted": {"verification_pending"},
    "verification_pending": {"resolved_verified", "upheld_open"},
}


class IssueManager:
    def __init__(self, control: ControlStore, store: ArtifactStore, documents=None):
        self.control = control
        self.store = store
        self.documents = documents

    # -- critique publication + admissibility -----------------------------
    def publish_critique(
        self,
        *,
        critique_id: str,
        author_role: str,
        target_ref: str,
        target_location: str,
        criterion_ref: str,
        allegation: str,
        basis: dict,
        material_impact: str,
        proposed_severity: str,
        resolution_condition: str,
        verification_method: str | None = None,
        uncertainty: str | None = None,
    ) -> dict:
        """Publish a Critique; generic or evidence-free criticism is rejected
        at the boundary (T06), never forwarded to the producer."""
        if proposed_severity not in CRITIQUE_SEVERITIES:
            raise ValidationError(f"severity must be blocking|major|minor: {proposed_severity!r}")
        if not (allegation and material_impact and resolution_condition):
            raise ValidationError(
                "critique is not admissible: specific allegation, material impact,"
                " and a resolution condition are required (T06)"
            )
        if not (basis.get("evidence_refs") or basis.get("counterexample")):
            raise ValidationError(
                "critique needs evidence refs or an explicit counterexample/reproducible"
                " check (T06: invented requirements and vague criticism are invalid)"
            )
        refs = [target_ref, criterion_ref, *basis.get("evidence_refs", [])]
        for ref in refs:
            self.store.get(ref)
        with self.control.tx() as conn:
            manifest = self.store.publish_artifact(
                logical_id=f"issues/critiques/{critique_id}",
                artifact_type="critique",
                author=author_role,
                body=canonical_bytes(
                    {
                        "target_ref": target_ref,
                        "target_location": target_location,
                        "criterion_ref": criterion_ref,
                        "allegation": allegation,
                        "basis": basis,
                        "material_impact": material_impact,
                        "proposed_severity": proposed_severity,
                        "resolution_condition": resolution_condition,
                        "verification_method": verification_method,
                        "uncertainty": uncertainty,
                    }
                ),
                media_type="application/json+scisaurus-critique",
                conn=conn,
            )
            self.control.append_event(
                conn,
                actor=author_role,
                event_type="critique.registered",
                payload={"critique_ref": manifest["artifact_ref"], "severity": proposed_severity},
            )
            return manifest

    # -- issue identity ---------------------------------------------------
    def register_issue(self, *, issue_id: str, critique_ref: str) -> dict:
        if self.store.get(critique_ref)["artifact_type"] != "critique":
            raise ValidationError("an issue must reference a critique artifact")
        critique_body = self._record_body(critique_ref)
        causal_key = sha256_hex(
            canonical_bytes(
                {
                    "criterion_ref": critique_body["criterion_ref"],
                    "target": critique_body["target_ref"].split("@")[0],
                    "allegation": critique_body["allegation"].strip().lower(),
                }
            )
        )
        with self.control.tx() as conn:
            open_row = conn.execute(
                "SELECT issue_id FROM issues"
                " WHERE causal_key = ? AND state NOT IN"
                " ('resolved_verified','rejected_invalid','rebutted')",
                (causal_key,),
            ).fetchone()
            if open_row is not None:
                self.control.append_event(
                    conn,
                    actor="triage",
                    event_type="issue.transitioned",
                    payload={
                        "issue_id": open_row["issue_id"],
                        "deduplicated": True,
                        "incoming_critique": critique_ref,
                    },
                )
                return {"issue": self.get(open_row["issue_id"]), "deduplicated": True}
            conn.execute(
                "INSERT INTO issues(issue_id, causal_key, state, generation, critique_ref, updated_at)"
                " VALUES (?, ?, 'registered', 1, ?, ?)",
                (issue_id, causal_key, critique_ref, now_iso()),
            )
        return {"issue": self.get(issue_id), "deduplicated": False}

    # -- lifecycle --------------------------------------------------------
    def triage(self, issue_id: str, actor: str, *, admissible: bool, reason: str) -> dict:
        """Triage checks admissibility; it is not proof of truth (40 §6.3)."""
        with self.control.tx() as conn:
            if admissible:
                self._transition(issue_id, "triaged", actor, reason, conn=conn)
                return self._transition(issue_id, "awaiting_response", actor, reason, conn=conn)
            return self._transition(issue_id, "rejected_invalid", actor, reason, conn=conn)

    def respond(
        self,
        issue_id: str,
        *,
        response_id: str,
        author: str,
        stance: str,
        supporting_refs: list[str] | None = None,
        candidate_ref: str | None = None,
    ) -> dict:
        if stance not in STANCES:
            raise ValidationError(f"stance must be accept|rebut|request_evidence: {stance!r}")
        state = {
            "accept": "repair_submitted",
            "rebut": "adjudication_pending",
            "request_evidence": "needs_evidence",
        }[stance]
        with self.control.tx() as conn:
            issue = self.get(issue_id)
            if issue["state"] not in {"awaiting_response", "upheld_open", "needs_evidence"}:
                raise StateError(f"issue not awaiting a response: {issue['state']}")
            critique = self._record_body(issue["critique_ref"])
            self._require_transition(issue["state"], state)
            for ref in supporting_refs or []:
                self.store.get(ref)
            if stance == "accept":
                if candidate_ref is None:
                    raise ValidationError("an accepted repair requires a candidate_ref")
                self._validate_candidate(candidate_ref, critique["target_ref"])
            elif candidate_ref is not None:
                raise ValidationError("only an accepted repair may submit a candidate_ref")
            body = {
                "issue_id": issue_id,
                "critique_ref": issue["critique_ref"],
                "baseline_ref": critique["target_ref"],
                "resolution_condition": critique["resolution_condition"],
                "stance": stance,
                "supporting_refs": supporting_refs or [],
                "candidate_ref": candidate_ref,
            }
            manifest = self.store.publish_artifact(
                logical_id=f"issues/responses/{response_id}",
                artifact_type="response",
                author=author,
                body=canonical_bytes(body),
                media_type="application/json+scisaurus-response",
                inputs=self._subjects([
                    issue["critique_ref"], critique["target_ref"],
                    *([candidate_ref] if candidate_ref else []), *(supporting_refs or []),
                ]),
                conn=conn,
            )
            self._transition(
                issue_id, state, author, f"{stance} response {response_id}",
                conn=conn, record_ref=manifest["artifact_ref"],
            )
            return manifest

    def adjudicate(
        self,
        issue_id: str,
        *,
        adjudication_id: str,
        adjudicator: str,
        validity: str,
        rationale: str,
        conflict_checks: str,
    ) -> dict:
        if validity not in {"upheld", "rejected_invalid", "rebutted", "needs_evidence"}:
            raise ValidationError(f"unknown adjudication validity: {validity!r}")
        if not self._nonempty(rationale) or not self._nonempty(conflict_checks):
            raise ValidationError("adjudication requires rationale and conflict checks")
        state = {
            "upheld": "upheld_open",
            "rejected_invalid": "rejected_invalid",
            "rebutted": "rebutted",
            "needs_evidence": "needs_evidence",
        }[validity]
        with self.control.tx() as conn:
            issue = self.get(issue_id)
            if issue["state"] != "adjudication_pending":
                raise StateError(f"issue not adjudicable: {issue['state']}")
            critique = self._record_body(issue["critique_ref"])
            responses = self._responses(issue_id)
            if not responses or responses[-1][1]["stance"] != "rebut":
                raise ValidationError("adjudication requires a committed rebuttal response")
            self._require_independent(adjudicator, issue, responses)
            manifest = self.store.publish_artifact(
                logical_id=f"issues/adjudications/{adjudication_id}",
                artifact_type="adjudication",
                author=adjudicator,
                body=canonical_bytes({
                    "issue_id": issue_id,
                    "critique_ref": issue["critique_ref"],
                    "critique_refs": self._critique_refs(issue),
                    "response_refs": [m["artifact_ref"] for m, _ in responses],
                    "criterion_ref": critique["criterion_ref"],
                    "validity": validity,
                    "rationale": rationale,
                    "conflict_checks": conflict_checks,
                }),
                media_type="application/json+scisaurus-adjudication",
                inputs=self._subjects([
                    issue["critique_ref"], critique["criterion_ref"],
                    *[m["artifact_ref"] for m, _ in responses],
                ]),
                conn=conn,
            )
            self._transition(
                issue_id, state, adjudicator, f"adjudication {adjudication_id}: {rationale}",
                conn=conn, record_ref=manifest["artifact_ref"],
            )
            return manifest

    def submit_repair(self, issue_id: str, candidate_ref: str, author: str) -> dict:
        return self.respond(
            issue_id,
            response_id=f"resp-{issue_id}",
            author=author,
            stance="accept",
            candidate_ref=candidate_ref,
        )

    def verify(
        self,
        issue_id: str,
        *,
        verification_id: str,
        verifier: str,
        response_ref: str,
        candidate_ref: str,
        baseline_ref: str,
        resolution_condition: str,
        check_refs: list[str],
        regressions: list[str] | None = None,
        uncertainties: list[str] | None = None,
        rationale: str,
    ) -> dict:
        """Record an independently supported decision about an exact repair.

        Each check ref identifies an ``evidence_record`` JSON artifact with
        issue_id, critique_ref, response_ref, candidate_ref, baseline_ref, resolution_condition,
        check_id, kind (resolution|regression), outcome
        (passed|failed|insufficient_evidence|check_failed), method, and result.
        Its subject inputs must pin the inspected baseline and candidate.
        Resolution and regression checks must both pass for closure.

        This validates recorded evidence and participant separation. It does not
        execute arbitrary checks, authenticate caller identities, or establish
        scientific truth; those are responsibilities of the future runner and
        qualified verifier.
        """
        if (
            not isinstance(check_refs, list) or not check_refs
            or any(not self._nonempty(ref) for ref in check_refs)
            or len(set(check_refs)) != len(check_refs)
        ):
            raise ValidationError("verification requires distinct check evidence refs")
        if not self._nonempty(rationale):
            raise ValidationError("verification requires a rationale")
        for observations in (regressions, uncertainties):
            if observations is not None and (
                not isinstance(observations, list)
                or any(not self._nonempty(item) for item in observations)
            ):
                raise ValidationError("regressions and uncertainties must be lists of observations")
        with self.control.tx() as conn:
            issue = self.get(issue_id)
            if issue["state"] not in {"repair_submitted", "verification_pending"}:
                raise StateError(f"issue not verifiable: {issue['state']}")
            critique = self._record_body(issue["critique_ref"])
            responses = self._responses(issue_id)
            repairs = [(m, b) for m, b in responses if b["stance"] == "accept"]
            if not repairs:
                raise ValidationError("verification requires a published repair response")
            response, repair = repairs[-1]
            if response_ref != response["artifact_ref"]:
                raise ValidationError("verification does not match the current repair response")
            binding = {
                "issue_id": issue_id,
                "critique_ref": issue["critique_ref"],
                "candidate_ref": candidate_ref,
                "baseline_ref": baseline_ref,
                "resolution_condition": resolution_condition,
            }
            if any(repair.get(key) != value for key, value in binding.items()):
                raise ValidationError("verification does not match the exact submitted repair")
            if baseline_ref != critique["target_ref"] or resolution_condition != critique["resolution_condition"]:
                raise ValidationError("verification changed the critique baseline or resolution condition")
            binding["response_ref"] = response_ref
            self._validate_candidate(candidate_ref, baseline_ref)
            self._require_independent(verifier, issue, responses)
            checks = []
            check_ids = set()
            for ref in check_refs:
                manifest = self.store.get(ref)
                if manifest["artifact_type"] != "evidence_record":
                    raise ValidationError("check evidence must be an evidence_record artifact")
                self._require_independent(manifest["author"], issue, responses)
                check = self._record_body(ref)
                if any(check.get(key) != value for key, value in binding.items()):
                    raise ValidationError("check evidence does not match the exact verification subjects")
                subjects = {
                    item["ref"] for item in manifest["inputs"] if item["purpose"] == "subject"
                }
                if not {candidate_ref, baseline_ref}.issubset(subjects):
                    raise ValidationError("check evidence must pin its inspected candidate and baseline inputs")
                if not all(self._nonempty(check.get(key)) for key in ("check_id", "method", "result")):
                    raise ValidationError("check evidence requires a check_id, executed method, and result")
                if check["check_id"] in check_ids:
                    raise ValidationError("verification repeats a check_id")
                check_ids.add(check["check_id"])
                if check.get("kind") not in CHECK_KINDS or check.get("outcome") not in CHECK_OUTCOMES:
                    raise ValidationError("check evidence has an invalid kind or outcome")
                for key in ("regressions", "uncertainties"):
                    observations = check.get(key, [])
                    if not isinstance(observations, list) or any(
                        not self._nonempty(item) for item in observations
                    ):
                        raise ValidationError(f"check evidence {key} must be a list of observations")
                checks.append({**check, "evidence_ref": ref})
            missing_kinds = sorted(CHECK_KINDS - {check["kind"] for check in checks})
            resolved = (
                not missing_kinds
                and all(
                    check["outcome"] == "passed"
                    and not check.get("regressions") and not check.get("uncertainties")
                    for check in checks
                )
                and not regressions
                and not uncertainties
            )
            if issue["state"] == "repair_submitted":
                self._transition(issue_id, "verification_pending", verifier, "verification started", conn=conn)
            manifest = self.store.publish_artifact(
                logical_id=f"issues/verifications/{verification_id}",
                artifact_type="verification",
                author=verifier,
                body=canonical_bytes({
                    **binding,
                    "response_ref": response["artifact_ref"],
                    "resolved": resolved,
                    "checks": checks,
                    "missing_check_kinds": missing_kinds,
                    "regressions": regressions or [],
                    "uncertainties": uncertainties or [],
                    "rationale": rationale,
                }),
                media_type="application/json+scisaurus-verification",
                inputs=self._subjects([
                    issue["critique_ref"], response["artifact_ref"], baseline_ref,
                    candidate_ref, *check_refs,
                ]),
                conn=conn,
            )
            self.control.append_event(
                conn, actor=verifier, event_type="verification.completed",
                payload={"verification_ref": manifest["artifact_ref"], "resolved": resolved},
            )
            self._transition(
                issue_id, "resolved_verified" if resolved else "upheld_open", verifier,
                rationale if resolved else f"verification failed: {rationale}",
                conn=conn, record_ref=manifest["artifact_ref"],
            )
            return manifest

    # -- coverage / regression checks -------------------------------------
    def check_required_coverage(self, *, candidate_manifest_ref: str, required_units: list[str]) -> None:
        """A repair that deletes a required finding fails coverage (T09)."""
        if self.documents is None:
            raise ValidationError("coverage check requires the Documents service")
        tree = self.documents.get_tree(candidate_manifest_ref)
        present = set()
        for node in self.documents._walk_nodes(tree):
            ns, name, _ = parse_ref(node["ref"])
            present.add(f"{ns}/{name}")
        missing = [u for u in required_units if u not in present]
        if missing:
            raise ValidationError(
                f"required finding removed by the repair: {missing} (T09)"
            )

    # -- reads ------------------------------------------------------------
    def get(self, issue_id: str) -> dict:
        row = self.control._conn.execute(
            "SELECT * FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown issue: {issue_id}")
        return dict(row)

    @staticmethod
    def _nonempty(value) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @staticmethod
    def _subjects(refs: list[str]) -> list[dict]:
        return [{"ref": ref, "purpose": "subject"} for ref in dict.fromkeys(refs)]

    @staticmethod
    def _require_transition(current: str, new_state: str) -> None:
        if new_state not in ISSUE_TRANSITIONS.get(current, set()):
            raise StateError(f"illegal issue transition {current} -> {new_state}")

    def _transition(
        self, issue_id: str, new_state: str, actor: str, reason: str | None = None,
        *, conn, record_ref: str | None = None,
    ) -> dict:
        current = self.get(issue_id)["state"]
        self._require_transition(current, new_state)
        conn.execute(
            "UPDATE issues SET state = ?, updated_at = ? WHERE issue_id = ?",
            (new_state, now_iso(), issue_id),
        )
        payload = {"issue_id": issue_id, "from": current, "to": new_state, "reason": reason}
        if record_ref is not None:
            payload["record_ref"] = record_ref
        self.control.append_event(
            conn, actor=actor, event_type="issue.transitioned", payload=payload,
        )
        return self.get(issue_id)

    def _responses(self, issue_id: str) -> list[tuple[dict, dict]]:
        # Only responses committed with a lifecycle transition participate.
        responses = []
        rows = self.control._conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'issue.transitioned' ORDER BY seq"
        ).fetchall()
        for row in rows:
            event = json.loads(row["payload_json"])
            if event.get("issue_id") != issue_id or not event.get("record_ref"):
                continue
            manifest = self.store.get(event["record_ref"])
            if manifest["artifact_type"] == "response":
                body = self._record_body(manifest["artifact_ref"])
                if body.get("issue_id") != issue_id:
                    raise ValidationError("response is bound to another issue")
                responses.append((manifest, body))
        return responses

    def _require_independent(self, actor: str, issue: dict, responses: list[tuple[dict, dict]]) -> None:
        target_ref = self._record_body(issue["critique_ref"])["target_ref"]
        conflicted = {
            *[self.store.get(ref)["author"] for ref in self._critique_refs(issue)],
            *self._artifact_authors(target_ref),
        }
        for manifest, body in responses:
            conflicted.add(manifest["author"])
            if body.get("candidate_ref"):
                conflicted.update(self._artifact_authors(body["candidate_ref"]))
        if not self._nonempty(actor) or actor in conflicted:
            raise ValidationError(
                "review decision or check author participated in the critique, target,"
                " response, or repair — independent reviewer and recusal required"
            )

    def _critique_refs(self, issue: dict) -> list[str]:
        refs = [issue["critique_ref"]]
        rows = self.control._conn.execute(
            "SELECT payload_json FROM events WHERE event_type = 'issue.transitioned' ORDER BY seq"
        ).fetchall()
        for row in rows:
            event = json.loads(row["payload_json"])
            if event.get("issue_id") == issue["issue_id"] and event.get("incoming_critique"):
                refs.append(event["incoming_critique"])
        return list(dict.fromkeys(refs))

    def _artifact_authors(self, ref: str) -> set[str]:
        manifest = self.store.get(ref)
        authors = {manifest["author"]}
        if manifest["artifact_type"] == "document_manifest":
            nodes = list(self._record_body(ref)["units"])
            while nodes:
                node = nodes.pop()
                authors.add(self.store.get(node["ref"])["author"])
                nodes.extend(node.get("children", []))
        return authors

    def _validate_candidate(self, candidate_ref: str, baseline_ref: str) -> None:
        candidate = self.store.get(candidate_ref)
        baseline = self.store.get(baseline_ref)
        if (
            candidate["artifact_id"] != baseline["artifact_id"]
            or candidate["artifact_type"] != baseline["artifact_type"]
            or candidate["version"] <= baseline["version"]
        ):
            raise ValidationError("repair candidate must be a later version of the contested artifact")
        if baseline["body_hash"] is None:
            raise ValidationError("repair baseline must have an inspectable body")
        if candidate["body_hash"] is None or candidate["body_hash"] == baseline["body_hash"]:
            raise ValidationError("repair candidate must contain a changed artifact body")
        self.store.read_body(candidate["body_hash"])
        self.store.read_body(baseline["body_hash"])
        ancestors = list(candidate["parents"])
        seen = set()
        while ancestors:
            ref = ancestors.pop()
            if ref == baseline_ref:
                return
            if ref not in seen:
                seen.add(ref)
                ancestors.extend(self.store.get(ref)["parents"])
        raise ValidationError("repair candidate does not descend from the contested baseline")

    def _record_body(self, ref: str) -> dict:
        manifest = self.store.get(ref)
        if manifest["body_hash"] is None:
            raise ValidationError(f"record has no body: {ref}")
        try:
            body = json.loads(self.store.read_body(manifest["body_hash"]))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError(f"record must contain JSON: {ref}") from exc
        if not isinstance(body, dict):
            raise ValidationError(f"record body must be a JSON object: {ref}")
        return body
