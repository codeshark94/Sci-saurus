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

ISSUE_TRANSITIONS = {
    "registered": {"triaged", "rejected_invalid"},
    "triaged": {"awaiting_response"},
    "awaiting_response": {"repair_submitted", "adjudication_pending"},
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
        )
        with self.control.tx() as conn:
            self.control.append_event(
                conn,
                actor=author_role,
                event_type="critique.registered",
                payload={"critique_ref": manifest["artifact_ref"], "severity": proposed_severity},
            )
        return manifest

    # -- issue identity ---------------------------------------------------
    def register_issue(self, *, issue_id: str, critique_ref: str) -> dict:
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
        open_row = self.control._conn.execute(
            "SELECT issue_id FROM issues"
            " WHERE causal_key = ? AND state NOT IN"
            " ('resolved_verified','rejected_invalid','rebutted','needs_evidence')",
            (causal_key,),
        ).fetchone()
        with self.control.tx() as conn:
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
        if admissible:
            self._transition(issue_id, "triaged", actor, reason)
            return self._transition(issue_id, "awaiting_response", actor, reason)
        return self._transition(issue_id, "rejected_invalid", actor, reason)

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
        manifest = self.store.publish_artifact(
            logical_id=f"issues/responses/{response_id}",
            artifact_type="response",
            author=author,
            body=canonical_bytes(
                {
                    "issue_id": issue_id,
                    "stance": stance,
                    "supporting_refs": supporting_refs or [],
                    "candidate_ref": candidate_ref,
                }
            ),
            media_type="application/json+scisaurus-response",
        )
        if stance == "accept":
            if candidate_ref is None:
                raise ValidationError("an accepted repair requires a candidate_ref")
            self._transition(issue_id, "repair_submitted", author, f"response {response_id}")
        elif stance == "rebut":
            self._transition(issue_id, "adjudication_pending", author, f"rebuttal {response_id}")
        else:
            self._transition(issue_id, "needs_evidence", author, f"evidence request {response_id}")
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
        issue = self.get(issue_id)
        if issue["state"] != "adjudication_pending":
            raise StateError(f"issue not adjudicable: {issue['state']}")
        critique_body = self._record_body(issue["critique_ref"])
        # independence: the adjudicator cannot have authored the response it judges
        rows = self.control._conn.execute(
            "SELECT manifest_json FROM artifacts WHERE logical_id LIKE 'issues/responses/%'"
        ).fetchall()
        for row in rows:
            manifest = json.loads(row["manifest_json"])
            body = json.loads(self.store.read_body(manifest["body_hash"]))
            if body.get("issue_id") == issue_id and manifest["author"] == adjudicator:
                raise ValidationError(
                    "adjudicator authored the contested response — recusal required"
                )
        if validity not in {"upheld", "rejected_invalid", "rebutted", "needs_evidence"}:
            raise ValidationError(f"unknown adjudication validity: {validity!r}")
        state = {
            "upheld": "upheld_open",
            "rejected_invalid": "rejected_invalid",
            "rebutted": "rebutted",
            "needs_evidence": "needs_evidence",
        }[validity]
        manifest = self.store.publish_artifact(
            logical_id=f"issues/adjudications/{adjudication_id}",
            artifact_type="adjudication",
            author=adjudicator,
            body=canonical_bytes(
                {
                    "issue_id": issue_id,
                    "validity": validity,
                    "rationale": rationale,
                    "conflict_checks": conflict_checks,
                }
            ),
            media_type="application/json+scisaurus-adjudication",
        )
        self._transition(issue_id, state, adjudicator, f"adjudication {adjudication_id}: {rationale}")
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
        resolved: bool,
        checks: list[dict],
        regressions: list[str] | None = None,
        rationale: str = "",
    ) -> dict:
        issue = self.get(issue_id)
        if issue["state"] == "repair_submitted":
            self._transition(issue_id, "verification_pending", verifier, "verification started")
        elif issue["state"] != "verification_pending":
            raise StateError(f"issue not verifiable: {issue['state']}")
        manifest = self.store.publish_artifact(
            logical_id=f"issues/verifications/{verification_id}",
            artifact_type="verification",
            author=verifier,
            body=canonical_bytes(
                {
                    "issue_id": issue_id,
                    "resolved": resolved,
                    "checks": checks,
                    "regressions": regressions or [],
                    "rationale": rationale,
                }
            ),
            media_type="application/json+scisaurus-verification",
        )
        if resolved:
            self._transition(issue_id, "resolved_verified", verifier, rationale)
        else:
            # a failed verification returns the issue to upheld_open (T08)
            self._transition(issue_id, "upheld_open", verifier, f"verification failed: {rationale}")
        return manifest

    # -- coverage / regression checks -------------------------------------
    def check_required_coverage(self, *, candidate_manifest_ref: str, required_units: list[str]) -> None:
        """A repair that deletes a required finding fails coverage (T09)."""
        if self.documents is None:
            raise ValidationError("coverage check requires the Documents service")
        tree = self.documents.get_tree(candidate_manifest_ref)
        present = set()
        for node in tree.get("units", []):
            ns, name, _ = parse_ref(node["ref"])
            present.add(f"{ns}/{name}")
            for child in node.get("children", []):
                ns2, name2, _ = parse_ref(child["ref"])
                present.add(f"{ns2}/{name2}")
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

    def _transition(self, issue_id: str, new_state: str, actor: str, reason: str | None = None) -> dict:
        row = self.control._conn.execute(
            "SELECT state FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown issue: {issue_id}")
        current = row["state"]
        allowed = ISSUE_TRANSITIONS.get(current, set())
        if new_state != current and new_state not in allowed:
            raise StateError(f"illegal issue transition {current} -> {new_state}")
        with self.control.tx() as conn:
            conn.execute(
                "UPDATE issues SET state = ?, updated_at = ? WHERE issue_id = ?",
                (new_state, now_iso(), issue_id),
            )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="issue.transitioned",
                payload={"issue_id": issue_id, "from": current, "to": new_state, "reason": reason},
            )
        return self.get(issue_id)

    def _record_body(self, ref: str) -> dict:
        manifest = self.store.get(ref)
        return json.loads(self.store.read_body(manifest["body_hash"]))