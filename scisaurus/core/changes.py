"""Purpose-directed surgical change control.

Implements docs/45-artifact-change-control.md §4–§7 at slice scale: a
ChangeRequest states purpose/baseline/scope/preservation; an EditGrant binds
it to an authenticated actor with permitted operations per unit; a ChangeSet
is applied by the control plane only after it validates the *complete*
mutation set (scope, expected preimages, protected spans, citation anchors),
then publishes new unit versions plus the successor manifest and advances the
accepted head with compare-and-swap — atomically, or not at all.

Slice scope (P1 acceptance subset T56–T58): replace_body / move / split /
retire with identity and lineage preservation. Coupled multi-request groups
(T61), rebasing (T60), and shared-asset scoping (T59) are later milestones.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from scisaurus.core.errors import (
    ConflictError,
    NotFoundError,
    StateError,
    ValidationError,
)
from scisaurus.core.schema import canonical_bytes, now_iso, parse_ref
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.documents import Documents

GRANT_OPS = frozenset({"replace_body", "move", "split", "retire"})
UNIT_CONTENT_KEYS = ("kind", "text", "purpose", "claim_refs", "citations", "lineage")


def _logical(ref: str) -> str:
    ns, name, _ = parse_ref(ref)
    return f"{ns}/{name}"


def _id_used(control: ControlStore, store: ArtifactStore, logical: str) -> bool:
    """A unit id is 'used' if any version exists or it was retired — ids are
    never reused to hide a replacement (45 §2)."""
    if store.versions(logical):
        return True
    row = control._conn.execute(
        "SELECT 1 FROM retired_units WHERE logical_id = ?", (logical,)
    ).fetchone()
    return row is not None


class ChangeService:
    def __init__(self, control: ControlStore, store: ArtifactStore, documents: Documents):
        self.control = control
        self.store = store
        self.documents = documents

    # -- change requests and grants --------------------------------------
    def create_change_request(
        self,
        *,
        cr_id: str,
        author: str,
        purpose: str,
        baseline_manifest_ref: str,
        scope: dict,
        preservation: list[str],
        authority: str = "delegated",
        task_id: str | None = None,
    ) -> dict:
        manifest = self.store.publish_artifact(
            logical_id=f"strategy/changes/{cr_id}",
            artifact_type="change_request",
            author=author,
            body=canonical_bytes(
                {
                    "purpose": purpose,
                    "baseline": baseline_manifest_ref,
                    "scope": scope,
                    "preservation": preservation,
                    "authority": authority,
                }
            ),
            media_type="application/json+scisaurus-change",
            task_id=task_id,
        )
        with self.control.tx() as conn:
            self.control.append_event(
                conn,
                actor=author,
                event_type="change.requested",
                payload={"change_request_ref": manifest["artifact_ref"], "purpose": purpose},
            )
        return manifest

    def issue_grant(
        self,
        *,
        grant_id: str,
        request_ref: str,
        baseline_manifest_ref: str,
        actor: str,
        units: dict[str, dict],
        expires_in_seconds: float = 3600.0,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict:
        """Bind the request and baseline to an authenticated actor with exact
        permitted operations. Read access and edit rights are separate (45 §4)."""
        for logical, spec in units.items():
            for op in spec.get("ops", []):
                if op not in GRANT_OPS:
                    raise ValidationError(f"unknown grant op: {op!r}")
        issued_at = now_iso()
        with self.control.tx() as conn:
            conn.execute(
                "INSERT INTO edit_grants(grant_id, request_ref, baseline_manifest_ref,"
                " actor, units_json, task_id, attempt_id, issued_at, expires_at, state)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'issued')",
                (
                    grant_id,
                    request_ref,
                    baseline_manifest_ref,
                    actor,
                    canonical_bytes(units).decode("utf-8"),
                    task_id,
                    attempt_id,
                    issued_at,
                    time.time() + expires_in_seconds,
                ),
            )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="edit_grant.issued",
                payload={
                    "grant_id": grant_id,
                    "baseline": baseline_manifest_ref,
                    "units": sorted(units),
                },
            )
        return self.get_grant(grant_id)

    def revoke_grant(self, grant_id: str, actor: str) -> None:
        with self.control.tx() as conn:
            row = conn.execute(
                "SELECT state FROM edit_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"unknown grant: {grant_id}")
            if row["state"] != "issued":
                raise StateError(f"grant not revocable: {row['state']}")
            conn.execute(
                "UPDATE edit_grants SET state='revoked' WHERE grant_id = ?", (grant_id,)
            )
            self.control.append_event(
                conn,
                actor=actor,
                event_type="edit_grant.revoked",
                payload={"grant_id": grant_id},
            )

    def get_grant(self, grant_id: str) -> dict:
        row = self.control._conn.execute(
            "SELECT * FROM edit_grants WHERE grant_id = ?", (grant_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown grant: {grant_id}")
        grant = dict(row)
        grant["units"] = json.loads(grant.pop("units_json"))
        return grant

    # -- application -----------------------------------------------------
    @staticmethod
    def _check_scope(grant: dict, unit_logical: str, op_type: str) -> None:
        spec = grant["units"].get(unit_logical)
        if spec is None:
            raise ValidationError(
                f"op outside grant scope: {unit_logical!r}"
                " (complete mutation set exceeds the grant, T56)"
            )
        if op_type not in spec.get("ops", []):
            raise ValidationError(f"op {op_type!r} not permitted for {unit_logical!r}")

    @staticmethod
    def _find(tree: dict, logical: str):
        for node, children, idx in iter_all(tree.get("units", [])):
            ref = node["ref"]
            node_logical = ref.removeprefix("PENDING:") if ref.startswith("PENDING:") else _logical(ref)
            if node_logical == logical:
                return node, children, idx
        return None, None, None

    @staticmethod
    def _intersects(left: list[int], right: list[int]) -> bool:
        return left[0] < right[1] and right[0] < left[1]

    @staticmethod
    def _validate_span(span, text: str, *, allow_empty: bool = True) -> None:
        if not (
            isinstance(span, list) and len(span) == 2
            and all(type(value) is int for value in span)
            and 0 <= span[0] <= span[1] <= len(text)
            and (allow_empty or span[0] < span[1])
        ):
            raise ValidationError(f"invalid span {span!r}")

    def _replace_content(self, content: dict, node_ref: str, ops: list[dict], spec: dict) -> dict:
        """Compile disjoint replacements in baseline coordinates. Explicit
        reanchors use coordinates in the final composed unit, never an
        intermediate version; a unit is published once per ChangeSet."""
        text = content["text"]
        original = self.store.get(node_ref)
        protected_spans = spec.get("protected_spans", [])
        for protected in protected_spans:
            self._validate_span(protected, text, allow_empty=False)
        replacements = []
        reanchors = {}
        known_citations = {c["occurrence_id"]: c for c in content["citations"]}
        for op in ops:
            if op.get("expected_unit_version") != original["version"]:
                raise ValidationError("preimage mismatch: expected_unit_version")
            if op.get("expected_body_hash") != original["body_hash"]:
                raise ValidationError("preimage mismatch: expected_body_hash")
            span = op.get("span")
            self._validate_span(span, text)
            if not isinstance(op.get("new_text"), str):
                raise ValidationError("replacement new_text must be a string")
            for protected in protected_spans:
                if self._intersects(span, protected):
                    raise ValidationError(f"op span {span} intersects protected span {protected} (T57)")
            for reanchor in op.get("reanchors", []):
                occurrence = reanchor.get("occurrence_id")
                citation = known_citations.get(occurrence)
                if citation is None or not self._intersects(span, citation["span"]):
                    raise ValidationError(f"reanchor must identify an affected citation: {occurrence!r}")
                if occurrence in reanchors:
                    raise ValidationError(f"duplicate citation reanchor: {occurrence!r}")
                reanchors[occurrence] = reanchor.get("new_span")
            replacements.append(op)
        replacements.sort(key=lambda op: tuple(op["span"]))
        for before, after in zip(replacements, replacements[1:]):
            # Coincident insertions or an insertion at a replacement boundary
            # have no unambiguous order in baseline coordinates.
            if (before["span"][1] > after["span"][0]
                or (before["span"][1] == after["span"][0]
                    and (before["span"][0] == before["span"][1]
                         or after["span"][0] == after["span"][1]))):
                raise ValidationError("overlapping or ambiguous replacement spans")
        fragments = []
        cursor = 0
        for op in replacements:
            start, end = op["span"]
            fragments.extend((text[cursor:start], op["new_text"]))
            cursor = end
        fragments.append(text[cursor:])
        new_text = "".join(fragments)
        citations = []
        for citation in content["citations"]:
            span = citation["span"]
            affected = any(self._intersects(op["span"], span) for op in replacements)
            if affected:
                if citation["occurrence_id"] not in reanchors:
                    raise ValidationError(
                        f"citation anchor inside replaced span not reanchored: {citation['occurrence_id']}"
                    )
                new_span = reanchors[citation["occurrence_id"]]
            else:
                shift = sum(
                    len(op["new_text"]) - (op["span"][1] - op["span"][0])
                    for op in replacements if op["span"][1] <= span[0]
                )
                new_span = [span[0] + shift, span[1] + shift]
            citations.append({**citation, "span": new_span})
        revised = {**content, "text": new_text, "citations": citations}
        self.documents.validate_unit_content(revised)
        return revised

    def apply_changeset(
        self,
        *,
        grant_id: str,
        ops: list[dict],
        author: str,
        expected_accepted_manifest_version: int | None = None,
        reason: str | None = None,
    ) -> dict:
        """Plan and integrate against one locked accepted baseline. Structural
        operations run in order; body replacements use baseline coordinates.
        Split preserves exact text and only accepts leaves. Retire requires
        explicit retirement rights for every unit it removes."""
        with self.control.tx() as conn:
            return self._apply_changeset_in(
                conn, grant_id=grant_id, ops=ops, author=author,
                expected_accepted_manifest_version=expected_accepted_manifest_version,
                reason=reason,
            )

    def stage_changeset(
        self, *, changeset_id: str, grant_id: str, ops: list[dict], author: str,
        expected_accepted_manifest_version: int | None = None,
        reason: str | None = None,
    ) -> dict:
        """Publish an immutable body-edit candidate for external verification.

        The accepted head and retirement ledger are untouched. Structural
        staging is unsupported until its pending membership effects have a
        durable acceptance contract.
        """
        if not isinstance(changeset_id, str) or not changeset_id.strip():
            raise ValidationError("staging requires a nonempty changeset_id")
        with self.control.tx() as conn:
            return self._apply_changeset_in(
                conn, grant_id=grant_id, ops=ops, author=author,
                expected_accepted_manifest_version=expected_accepted_manifest_version,
                reason=reason, changeset_id=changeset_id,
            )

    def accept_changeset(
        self, *, changeset_ref: str, verification_ref: str, author: str,
        expected_accepted_manifest_version: int | None = None,
        expected_head_refs: list[str] | None = None,
        acceptance_guard: Callable[[], None] | None = None,
    ) -> dict:
        """Adopt the exact staged document after a committed passing review.

        The producer may submit the acceptance request but cannot supply a
        self-issued verification. Acceptance rechecks the baseline and live
        grant and any governing input heads in the same transaction as the
        accepted-head transition. A runtime guard may abort admission after
        evidence validation or roll back an adoption before its transaction commits.
        """
        from scisaurus.review.issues import IssueManager

        reviews = IssueManager(self.control, self.store, self.documents)
        if acceptance_guard is not None and not callable(acceptance_guard):
            raise ValidationError("acceptance_guard must be callable")
        with self.control.tx() as conn:
            if expected_head_refs is not None and not isinstance(expected_head_refs, list):
                raise ValidationError("expected_head_refs must be an explicit list of pinned artifact refs")
            expected_heads = {}
            for ref in expected_head_refs or []:
                if not isinstance(ref, str):
                    raise ValidationError("governing head preconditions must contain artifact ref strings")
                logical = _logical(ref)
                if logical in expected_heads:
                    raise ValidationError("governing head preconditions must identify each artifact once")
                expected_heads[logical] = ref
            for logical, ref in expected_heads.items():
                head = self.store.head(logical)
                if head is None or head["artifact_ref"] != ref:
                    raise ConflictError(f"governing artifact head changed: {logical}")
            record = self.store.get(changeset_ref)
            if record["artifact_type"] != "change_set":
                raise ValidationError("acceptance requires a staged change_set artifact")
            stage = reviews._record_body(changeset_ref)
            events = self._event_payloads("changeset.staged")
            if not any(
                event.get("changeset_ref") == changeset_ref
                and event.get("candidate_ref") == stage.get("candidate_ref")
                and event.get("baseline_ref") == stage.get("baseline_ref")
                and event.get("grant_id") == stage.get("grant_id")
                for event in events
            ):
                raise ValidationError("change_set has no committed staging record")
            if stage.get("pending_retirements") != [] or not stage.get("ops") or any(
                op.get("op") != "replace_body" for op in stage["ops"]
            ):
                raise ValidationError("staged structural changes are unsupported")
            grant = self.get_grant(stage["grant_id"])
            if author != grant["actor"] or record["author"] != grant["actor"]:
                raise ValidationError("changeset author does not match the edit grant actor")
            if grant["state"] != "issued":
                raise StateError(f"grant {grant['grant_id']} is {grant['state']}")
            def check_admission():
                if acceptance_guard is not None:
                    acceptance_guard()
                if grant["expires_at"] is not None and grant["expires_at"] <= time.time():
                    raise StateError(f"grant {grant['grant_id']} expired")
            check_admission()
            if (stage["baseline_ref"] != grant["baseline_manifest_ref"]
                or stage["request_ref"] != grant["request_ref"]):
                raise ValidationError("staged candidate no longer matches its grant")
            candidate_ref, baseline_ref = stage["candidate_ref"], stage["baseline_ref"]
            candidate = self.store.get(candidate_ref)
            baseline = self.store.get(baseline_ref)
            accepted = self.store.accepted(baseline["artifact_id"])
            if accepted is None or accepted["artifact_ref"] != baseline_ref:
                raise ConflictError("staged candidate baseline is no longer the accepted document")
            if accepted["version"] != expected_accepted_manifest_version:
                raise ConflictError("accepted document version does not match expected version")
            if candidate["artifact_type"] != "document_manifest" or candidate["author"] != author:
                raise ValidationError("staged candidate must be the producer's document manifest")
            for op in stage["ops"]:
                self._check_scope(grant, op["unit"], op["op"])
            self.documents.validate_tree(self.documents.get_tree(candidate_ref))
            self._require_passing_verification(
                reviews, verification_ref=verification_ref,
                candidate_ref=candidate_ref, baseline_ref=baseline_ref,
            )
            check_admission()
            self.store._adopt_in(
                conn, baseline["artifact_id"], candidate["version"],
                expected_accepted_manifest_version, author,
            )
            self.control.append_event(
                conn, actor=author, event_type="changeset.integrated",
                payload={
                    "grant_id": grant["grant_id"], "changeset_ref": changeset_ref,
                    "manifest_ref": candidate_ref, "verification_ref": verification_ref,
                    "changed_units": stage["changed_units"], "retired_units": [],
                    "expected_head_refs": list(expected_heads.values()),
                },
            )
            check_admission()
            return {
                "changeset_ref": changeset_ref, "manifest_ref": candidate_ref,
                "manifest_version": candidate["version"], "verification_ref": verification_ref,
                "changed_units": stage["changed_units"], "retired_units": [],
            }

    def _event_payloads(self, event_type: str) -> list[dict]:
        return [json.loads(row["payload_json"]) for row in self.control._conn.execute(
            "SELECT payload_json FROM events WHERE event_type = ? ORDER BY seq", (event_type,)
        )]

    def _require_passing_verification(
        self, reviews, *, verification_ref: str, candidate_ref: str, baseline_ref: str,
    ) -> None:
        """Validate the committed IssueManager decision and its exact evidence.
        A standalone verification artifact is not an authoritative decision."""
        from scisaurus.review.issues import CHECK_KINDS

        manifest = self.store.get(verification_ref)
        if manifest["artifact_type"] != "verification":
            raise ValidationError("acceptance requires a verification artifact")
        verdict = reviews._record_body(verification_ref)
        if (
            verdict.get("resolved") is not True
            or any(verdict.get(key) != [] for key in ("regressions", "uncertainties", "missing_check_kinds"))
            or verdict.get("candidate_ref") != candidate_ref
            or verdict.get("baseline_ref") != baseline_ref
        ):
            raise ValidationError("verification must pass for the exact candidate and baseline")
        issue = reviews.get(verdict["issue_id"])
        transitions = [event for event in self._event_payloads("issue.transitioned")
                       if event.get("issue_id") == issue["issue_id"] and event.get("to")]
        if (issue["state"] != "resolved_verified" or not transitions
            or transitions[-1].get("to") != "resolved_verified"
            or transitions[-1].get("record_ref") != verification_ref):
            raise ValidationError("verification is not the issue's committed passing decision")
        critique = reviews._record_body(issue["critique_ref"])
        responses = reviews._responses(issue["issue_id"])
        repairs = [(record, body) for record, body in responses if body["stance"] == "accept"]
        if not repairs:
            raise ValidationError("passing verification has no committed repair response")
        response, repair = repairs[-1]
        binding = {
            "issue_id": issue["issue_id"], "critique_ref": issue["critique_ref"],
            "response_ref": response["artifact_ref"], "candidate_ref": candidate_ref,
            "baseline_ref": baseline_ref, "resolution_condition": critique["resolution_condition"],
        }
        if (critique["target_ref"] != baseline_ref
            or any(verdict.get(key) != value for key, value in binding.items())
            or any(repair.get(key) != value for key, value in binding.items() if key != "response_ref")):
            raise ValidationError("verification bindings do not match the exact repair contract")
        reviews._validate_candidate(candidate_ref, baseline_ref)
        reviews._require_independent(manifest["author"], issue, responses)
        checks = verdict.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValidationError("passing verification requires check evidence")
        kinds, check_ids, evidence_refs = set(), set(), set()
        for check in checks:
            evidence_ref = check.get("evidence_ref")
            if not evidence_ref or evidence_ref in evidence_refs:
                raise ValidationError("verification requires distinct check evidence")
            evidence_refs.add(evidence_ref)
            evidence = self.store.get(evidence_ref)
            body = reviews._record_body(evidence_ref)
            if evidence["artifact_type"] != "evidence_record" or body != {
                key: value for key, value in check.items() if key != "evidence_ref"
            }:
                raise ValidationError("verification check does not match its evidence artifact")
            if (any(body.get(key) != value for key, value in binding.items())
                or body.get("outcome") != "passed"
                or body.get("regressions") or body.get("uncertainties")
                or body.get("kind") not in CHECK_KINDS
                or not all(reviews._nonempty(body.get(key)) for key in ("check_id", "method", "result"))):
                raise ValidationError("verification evidence must pass the exact repair contract")
            if body["check_id"] in check_ids:
                raise ValidationError("verification repeats a check_id")
            check_ids.add(body["check_id"])
            kinds.add(body["kind"])
            subjects = {item["ref"] for item in evidence["inputs"] if item["purpose"] == "subject"}
            if not {candidate_ref, baseline_ref}.issubset(subjects):
                raise ValidationError("verification evidence lacks exact candidate and baseline inputs")
            reviews._require_independent(evidence["author"], issue, responses)
        if kinds != CHECK_KINDS:
            raise ValidationError("verification requires passing resolution and regression checks")

    def _apply_changeset_in(
        self, conn, *, grant_id, ops, author,
        expected_accepted_manifest_version, reason, changeset_id=None,
    ) -> dict:
        if changeset_id is not None:
            logical_id = f"strategy/change_sets/{changeset_id}"
            parse_ref(f"artifact:{logical_id}@1")
            if self.store.versions(logical_id):
                raise ConflictError(f"changeset id already exists: {changeset_id}")
            if not ops or any(op.get("op") != "replace_body" for op in ops):
                raise ValidationError("staging supports replace_body operations only")
        grant = self.get_grant(grant_id)
        if author != grant["actor"]:
            raise ValidationError("changeset author does not match the edit grant actor")
        if grant["state"] != "issued":
            raise StateError(f"grant {grant_id} is {grant['state']}")
        if grant["expires_at"] is not None and grant["expires_at"] <= time.time():
            raise StateError(f"grant {grant_id} expired")
        baseline_ref = grant["baseline_manifest_ref"]
        baseline_manifest = self.store.get(baseline_ref)
        if baseline_manifest["artifact_type"] != "document_manifest":
            raise ValidationError("grant baseline must be a document manifest")
        document_id = baseline_manifest["artifact_id"]
        accepted = self.store.accepted(document_id)
        if accepted is None or accepted["artifact_ref"] != baseline_ref:
            raise ConflictError("grant baseline is not the accepted document; a fresh grant is required")
        if accepted["version"] != expected_accepted_manifest_version:
            raise ConflictError("accepted document version does not match expected version")
        tree = self.documents.get_tree(baseline_ref)
        working = json.loads(json.dumps(tree))
        if not isinstance(ops, list) or not ops:
            raise ValidationError("changeset requires at least one operation")

        by_unit: dict[str, list[dict]] = {}
        for op in ops:
            op_type = op.get("op")
            if op_type not in GRANT_OPS:
                raise ValidationError(f"unknown op: {op_type!r}")
            foreign = op.get("target_document_ref")
            if foreign is not None and foreign != baseline_ref:
                raise ValidationError(f"op targets another document: {foreign!r}")
            logical = op.get("unit")
            if not logical:
                raise ValidationError("op missing 'unit'")
            self._check_scope(grant, logical, op_type)
            if self._find(tree, logical)[0] is None:
                raise ValidationError(f"unit not in baseline tree: {logical!r}")
            by_unit.setdefault(logical, []).append(op)
        for unit_ops in by_unit.values():
            if len(unit_ops) > 1 and any(op["op"] in {"split", "retire"} for op in unit_ops):
                raise ValidationError("split or retire cannot combine with another operation on the same unit")

        planned: dict[str, dict] = {}
        retirements: list[str] = []
        for op in ops:
            op_type, logical = op["op"], op["unit"]
            node, children, idx = self._find(working, logical)
            if node is None:
                raise ValidationError(f"operation targets a unit removed earlier in the changeset: {logical}")
            node_ref = node["ref"]
            content = self.documents.read_unit(node_ref)
            self.documents.validate_unit_content(content)
            if op_type == "replace_body":
                if logical not in planned:
                    revised = self._replace_content(
                        content, node_ref,
                        [item for item in by_unit[logical] if item["op"] == "replace_body"],
                        grant["units"][logical],
                    )
                    planned[logical] = {
                        "content": revised, "parents": [node_ref], "node": node,
                    }
            elif op_type == "move":
                to_parent = op.get("to_parent")
                del children[idx]
                if to_parent is None:
                    target_children = working["units"]
                else:
                    target_node, _, _ = self._find(working, to_parent)
                    if target_node is None:
                        raise ValidationError(f"move target parent missing: {to_parent!r}")
                    target_children = target_node.setdefault("children", [])
                index = op.get("index", len(target_children))
                if type(index) is not int or not 0 <= index <= len(target_children):
                    raise ValidationError(f"move index outside target children: {index!r}")
                target_children.insert(index, node)
            elif op_type == "split":
                if node.get("children"):
                    raise ValidationError("split requires a leaf; move children to their intended parents explicitly")
                offset, text = op.get("offset"), content["text"]
                if not (type(offset) is int and 0 < offset < len(text)):
                    raise ValidationError(f"split offset invalid: {offset!r}")
                if op.get("left_text") != text[:offset] or op.get("right_text") != text[offset:]:
                    raise ValidationError("split must preserve exact source slices; use replace_body for textual edits")
                for protected in grant["units"][logical].get("protected_spans", []):
                    self._validate_span(protected, text, allow_empty=False)
                    if protected[0] < offset < protected[1]:
                        raise ValidationError("split crosses a protected span")
                new_ids = [op.get("left_unit"), op.get("right_unit")]
                if new_ids[0] == new_ids[1]:
                    raise ValidationError("split requires two distinct new unit ids")
                for new_id in new_ids:
                    if not isinstance(new_id, str):
                        raise ValidationError("split requires new unit ids")
                    parse_ref(f"artifact:{new_id}@1")
                    if new_id in planned or _id_used(self.control, self.store, new_id):
                        raise ValidationError(f"unit id reuse forbidden: {new_id}")
                citations = [[], []]
                for citation in content["citations"]:
                    start, end = citation["span"]
                    if end <= offset:
                        citations[0].append(dict(citation))
                    elif start >= offset:
                        citations[1].append({**citation, "span": [start - offset, end - offset]})
                    else:
                        raise ValidationError("split crosses a citation anchor; choose a boundary outside its span")
                for side, key in enumerate(("citations_left", "citations_right")):
                    requested = op.get(key, [])
                    actual = {citation["occurrence_id"] for citation in citations[side]}
                    if (not isinstance(requested, list) or len(requested) != len(actual)
                        or set(requested) != actual):
                        raise ValidationError("split must re-home each citation exactly once to its text slice")
                new_nodes = []
                for side, new_id in enumerate(new_ids):
                    revised = {
                        **content,
                        "text": text[:offset] if side == 0 else text[offset:],
                        "citations": citations[side],
                        "lineage": {"derived_from": [node_ref], "operation": "split"},
                    }
                    self.documents.validate_unit_content(revised)
                    new_node = {"ref": f"PENDING:{new_id}", "kind": content["kind"], "children": []}
                    new_nodes.append(new_node)
                    planned[new_id] = {"content": revised, "parents": [], "node": new_node}
                children[idx:idx + 1] = new_nodes
                retirements.append(logical)
            elif op_type == "retire":
                removed = [node, *(item for item, _, _ in iter_all(node.get("children", [])))]
                for removed_node in removed:
                    removed_id = _logical(removed_node["ref"])
                    self._check_scope(grant, removed_id, "retire")
                    if grant["units"][removed_id].get("protected_spans"):
                        raise ValidationError(f"retire would delete protected content: {removed_id}")
                    retirements.append(removed_id)
                del children[idx]

        for logical, item in planned.items():
            if self._find(working, logical)[0] is not item["node"]:
                raise ValidationError(f"changeset removes another operation's output: {logical}")
            item["body"] = canonical_bytes({key: item["content"][key] for key in UNIT_CONTENT_KEYS})
        for logical, item in planned.items():
            manifest = self.store._publish_artifact_in(
                conn, logical_id=logical, artifact_type="content_unit", author=author,
                body=item["body"], media_type="application/json+scisaurus-unit",
                parents=item["parents"], task_id=grant.get("task_id"),
            )
            item["node"]["ref"] = manifest["artifact_ref"]
        for logical in retirements:
            conn.execute(
                "INSERT INTO retired_units(logical_id, retired_at) VALUES (?, ?)",
                (logical, now_iso()),
            )
            self.control.append_event(
                conn, actor=author, event_type="artifact.invalidated",
                payload={"unit": logical, "reason": "retired"},
            )
        new_manifest = self.documents.publish_manifest(
            document_id=document_id, tree=working, author=author,
            task_id=grant.get("task_id"), parents=[baseline_ref], conn=conn,
        )
        result = {
            "manifest_ref": new_manifest["artifact_ref"],
            "manifest_version": new_manifest["version"],
            "changed_units": list(planned), "retired_units": retirements,
        }
        if changeset_id is not None:
            staged = self.store._publish_artifact_in(
                conn, logical_id=f"strategy/change_sets/{changeset_id}",
                artifact_type="change_set", author=author,
                body=canonical_bytes({
                    "grant_id": grant_id, "request_ref": grant["request_ref"],
                    "baseline_ref": baseline_ref, "candidate_ref": result["manifest_ref"],
                    "ops": ops, "changed_units": result["changed_units"],
                    "pending_retirements": [], "reason": reason,
                }),
                media_type="application/json+scisaurus-change-set",
                inputs=[{"ref": ref, "purpose": "subject"}
                        for ref in (grant["request_ref"], baseline_ref, result["manifest_ref"])],
                task_id=grant.get("task_id"),
            )
            result["changeset_ref"] = staged["artifact_ref"]
        self.control.append_event(
            conn, actor=author, event_type="changeset.staged",
            payload={"grant_id": grant_id, "ops": len(ops), "reason": reason,
                     "changeset_ref": result.get("changeset_ref"),
                     "baseline_ref": baseline_ref, "candidate_ref": result["manifest_ref"]},
        )
        if changeset_id is not None:
            return result
        self.store._adopt_in(
            conn, document_id, new_manifest["version"],
            expected_accepted_manifest_version, author,
        )
        self.control.append_event(
            conn, actor=author, event_type="changeset.integrated",
            payload={
                "grant_id": grant_id, "manifest_ref": new_manifest["artifact_ref"],
                "changed_units": list(planned), "retired_units": retirements,
            },
        )
        return result


def iter_all(children):
    for i, node in enumerate(children):
        yield node, children, i
        yield from iter_all(node.get("children", []))
