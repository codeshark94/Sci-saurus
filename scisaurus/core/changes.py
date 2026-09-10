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
        def walk(children):
            for idx, node in enumerate(children):
                ns, name, _ = parse_ref(node["ref"])
                if f"{ns}/{name}" == logical:
                    return node, children, idx
                found = walk(node.get("children", []))
                if found is not None:
                    return found
            return None

        found = walk(tree.get("units", []))
        return found if found is not None else (None, None, None)

    def apply_changeset(
        self,
        *,
        grant_id: str,
        ops: list[dict],
        author: str,
        expected_accepted_manifest_version: int | None = None,
        reason: str | None = None,
    ) -> dict:
        grant = self.get_grant(grant_id)
        if grant["state"] != "issued":
            raise StateError(f"grant {grant_id} is {grant['state']}")
        if grant["expires_at"] is not None and grant["expires_at"] <= time.time():
            raise StateError(f"grant {grant_id} expired")
        baseline_ref = grant["baseline_manifest_ref"]
        baseline_manifest = self.store.get(baseline_ref)
        document_id = baseline_manifest["artifact_id"]
        tree = self.documents.get_tree(baseline_ref)
        working = json.loads(json.dumps(tree))

        planned: list[dict] = []  # unit publications, rewritten into the tree after publish
        retirements: list[str] = []

        for op in ops:
            op_type = op.get("op")
            if op_type not in GRANT_OPS:
                raise ValidationError(f"unknown op: {op_type!r}")
            foreign = op.get("target_document_ref")
            if foreign is not None and foreign != baseline_ref:
                raise ValidationError(
                    f"op targets another document: {foreign!r} (grant baseline {baseline_ref!r})"
                )
            unit_logical = op.get("unit")
            if not unit_logical:
                raise ValidationError("op missing 'unit'")
            self._check_scope(grant, unit_logical, op_type)
            node, children, idx = self._find(working, unit_logical)
            if node is None:
                raise ValidationError(f"unit not in baseline tree: {unit_logical!r}")
            node_ref = node["ref"]
            content = self.documents.read_unit(node_ref)

            if op_type == "replace_body":
                _, _, current_v = parse_ref(node_ref)
                if op.get("expected_unit_version") != current_v:
                    raise ValidationError(
                        f"preimage mismatch: expected unit version {op.get('expected_unit_version')!r},"
                        f" tree has {current_v}"
                    )
                if op.get("expected_body_hash") != self.store.get(node_ref)["body_hash"]:
                    raise ValidationError("preimage mismatch: expected_body_hash")
                text = content["text"]
                span = op.get("span")
                if not (
                    isinstance(span, list)
                    and len(span) == 2
                    and 0 <= span[0] <= span[1] <= len(text)
                ):
                    raise ValidationError(f"invalid span {span!r}")
                for protected in grant["units"][unit_logical].get("protected_spans", []):
                    if not (span[1] <= protected[0] or span[0] >= protected[1]):
                        raise ValidationError(
                            f"op span {span} intersects protected span {protected}"
                            f" of {unit_logical} (T57)"
                        )
                reanchors = {r["occurrence_id"]: r for r in op.get("reanchors", [])}
                new_citations = []
                for citation in content["citations"]:
                    c_span = citation["span"]
                    if span[0] < c_span[1] and c_span[0] < span[1]:
                        if citation["occurrence_id"] not in reanchors:
                            raise ValidationError(
                                "citation anchor inside replaced span not reanchored:"
                                f" {citation['occurrence_id']}"
                            )
                        citation = {
                            **citation,
                            "span": reanchors[citation["occurrence_id"]]["new_span"],
                        }
                    new_citations.append(citation)
                new_text = text[: span[0]] + op["new_text"] + text[span[1] :]
                planned.append(
                    {
                        "type": "replace",
                        "logical": unit_logical,
                        "content": {**content, "text": new_text, "citations": new_citations},
                        "parents": [node_ref],
                        "node": node,
                    }
                )

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
                target_children.insert(
                    min(int(op.get("index", len(target_children))), len(target_children)), node
                )

            elif op_type == "split":
                offset = op.get("offset")
                text = content["text"]
                if not (isinstance(offset, int) and 0 < offset < len(text)):
                    raise ValidationError(f"split offset invalid: {offset!r}")
                left_id, right_id = op.get("left_unit"), op.get("right_unit")
                for new_id in (left_id, right_id):
                    if _id_used(self.control, self.store, new_id):
                        raise ValidationError(f"unit id reuse forbidden: {new_id}")
                c_left = op.get("citations_left", [])
                c_right = op.get("citations_right", [])
                known = {c["occurrence_id"] for c in content["citations"]}
                if set(c_left) | set(c_right) != known:
                    raise ValidationError("split must re-home every citation occurrence explicitly")
                for occ in c_left + c_right:
                    if occ not in known:
                        raise ValidationError(f"unknown citation occurrence: {occ!r}")
                base = {
                    "kind": content["kind"],
                    "purpose": content.get("purpose"),
                    "claim_refs": content.get("claim_refs", []),
                }
                planned.append(
                    {
                        "type": "split_left",
                        "logical": left_id,
                        "content": {
                            **content,
                            "text": op["left_text"],
                            "citations": [
                                c for c in content["citations"] if c["occurrence_id"] in c_left
                            ],
                            "lineage": {"derived_from": [node_ref], "operation": "split"},
                        },
                        "parents": [],
                        "placeholder": f"PENDING:{left_id}",
                    }
                )
                planned.append(
                    {
                        "type": "split_right",
                        "logical": right_id,
                        "content": {
                            **content,
                            "text": op["right_text"],
                            "citations": [
                                c for c in content["citations"] if c["occurrence_id"] in c_right
                            ],
                            "lineage": {"derived_from": [node_ref], "operation": "split"},
                        },
                        "parents": [],
                        "placeholder": f"PENDING:{right_id}",
                    }
                )
                children[idx] = {"ref": f"PENDING:{left_id}", "kind": content["kind"], "children": []}
                children.insert(
                    idx + 1, {"ref": f"PENDING:{right_id}", "kind": content["kind"], "children": []}
                )
                retirements.append(unit_logical)

            elif op_type == "retire":
                del children[idx]
                retirements.append(unit_logical)

        # -- publish: objects first (crash → harmless orphan), then one tx --
        for item in planned:
            item["body"] = canonical_bytes(
                {k: item["content"][k] for k in UNIT_CONTENT_KEYS}
            )
            item["body_hash"] = self.store.publish_object(
                item["body"], "application/json+scisaurus-unit"
            )

        with self.control.tx() as conn:
            for item in planned:
                manifest = self.store._publish_artifact_in(
                    conn,
                    logical_id=item["logical"],
                    artifact_type="content_unit",
                    author=author,
                    body=item["body"],
                    media_type="application/json+scisaurus-unit",
                    parents=item["parents"],
                    task_id=grant.get("task_id"),
                )
                if item["type"] == "replace":
                    item["node"]["ref"] = manifest["artifact_ref"]
                else:  # split placeholders carry their pending marker
                    self._rewrite_placeholders(working, item["placeholder"], manifest["artifact_ref"])
            for logical in retirements:
                conn.execute(
                    "INSERT OR IGNORE INTO retired_units(logical_id, retired_at) VALUES (?, ?)",
                    (logical, now_iso()),
                )
                self.control.append_event(
                    conn,
                    actor=author,
                    event_type="artifact.invalidated",
                    payload={"unit": logical, "reason": "retired"},
                )
            self.control.append_event(
                conn,
                actor=author,
                event_type="changeset.staged",
                payload={"grant_id": grant_id, "ops": len(ops), "reason": reason},
            )
            new_manifest = self.documents.publish_manifest(
                document_id=document_id,
                tree=working,
                author=author,
                task_id=grant.get("task_id"),
                conn=conn,
            )
            self.store._adopt_in(
                conn,
                document_id,
                new_manifest["version"],
                expected_accepted_manifest_version,
                author,
            )
            self.control.append_event(
                conn,
                actor=author,
                event_type="changeset.integrated",
                payload={
                    "grant_id": grant_id,
                    "manifest_ref": new_manifest["artifact_ref"],
                    "changed_units": [i["logical"] for i in planned],
                    "retired_units": retirements,
                },
            )
        return {
            "manifest_ref": new_manifest["artifact_ref"],
            "manifest_version": new_manifest["version"],
            "changed_units": [i["logical"] for i in planned],
            "retired_units": retirements,
        }

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _rewrite_placeholders(tree: dict, placeholder: str, new_ref: str) -> None:
        for node, children, idx in iter_all(tree.get("units", [])):
            if node.get("ref") == placeholder:
                node["ref"] = new_ref
                return


def iter_all(children):
    for i, node in enumerate(children):
        yield node, children, i
        yield from iter_all(node.get("children", []))