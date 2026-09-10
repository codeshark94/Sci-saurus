"""Stable content units and ordered document manifests.

Implements docs/45-artifact-change-control.md §2: ContentUnits carry stable
identity (the ArtifactVersion logical id), a kind, purpose, claim refs,
citation occurrences, and lineage; DocumentManifests pin the exact ordered
containment tree of unit versions. Moving a unit changes the manifest
topology without inventing a new body version; split/merge preserve lineage;
retired unit ids are never reused.
"""

from __future__ import annotations

import json

from scisaurus.core.errors import ValidationError
from scisaurus.core.schema import canonical_bytes, parse_ref
from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore

UNIT_KINDS = frozenset(
    {"paragraph", "heading", "list_item", "equation", "table", "figure", "caption", "container"}
)

UNIT_MEDIA_TYPE = "application/json+scisaurus-unit"


class Documents:
    def __init__(self, control: ControlStore, store: ArtifactStore):
        self.control = control
        self.store = store

    # -- units -----------------------------------------------------------
    def publish_unit(
        self,
        *,
        logical_id: str,
        kind: str,
        text: str,
        author: str,
        purpose: str | None = None,
        claim_refs: list[dict] | None = None,
        citations: list[dict] | None = None,
        lineage: dict | None = None,
        task_id: str | None = None,
        conn=None,
    ) -> dict:
        """Publish one ContentUnit version.

        ``citations`` entries: {occurrence_id, span:[start,end],
        reference_card_ref, claim_ref, relation}. ``lineage`` records
        split/merge/retire relationships, e.g. {"derived_from": [ref]}.
        """
        if kind not in UNIT_KINDS:
            raise ValidationError(f"unknown unit kind: {kind!r}")
        retired = self.control._conn.execute(
            "SELECT 1 FROM retired_units WHERE logical_id = ?", (logical_id,)
        ).fetchone()
        if retired is not None:
            # retired ids are never reused to hide a replacement (45 §2)
            raise ValidationError(f"unit id reuse forbidden: {logical_id} was retired")
        for citation in citations or []:
            for key in ("occurrence_id", "span", "reference_card_ref"):
                if key not in citation:
                    raise ValidationError(f"citation occurrence missing {key}: {citation!r}")
        content = {
            "kind": kind,
            "text": text,
            "purpose": purpose,
            "claim_refs": claim_refs or [],
            "citations": citations or [],
            "lineage": lineage or {},
        }
        return self.store.publish_artifact(
            logical_id=logical_id,
            artifact_type="content_unit",
            author=author,
            body=canonical_bytes(content),
            media_type=UNIT_MEDIA_TYPE,
            task_id=task_id,
            conn=conn,
        )

    def read_unit(self, ref: str) -> dict:
        manifest = self.store.get(ref)
        content = json.loads(self.store.read_body(manifest["body_hash"]))
        content["manifest"] = manifest
        return content

    # -- manifests -------------------------------------------------------
    @staticmethod
    def _walk_nodes(node):
        for child in node.get("units", node.get("children", [])):
            yield child
            yield from Documents._walk_nodes(child)

    def validate_tree(self, tree: dict) -> None:
        seen: set[str] = set()
        for node in self._walk_nodes({"units": tree.get("units", [])}):
            ref = node.get("ref")
            ns, name, version = parse_ref(ref) if ref else (None, None, None)
            logical = f"{ns}/{name}"
            if logical in seen:
                raise ValidationError(f"live unit appears twice in tree: {logical}")
            seen.add(logical)
            self.store.get(ref)  # must resolve to a published unit version

    def publish_manifest(
        self,
        *,
        document_id: str,
        tree: dict,
        author: str,
        task_id: str | None = None,
        assembly_dependencies: list[str] | None = None,
        conn=None,
    ) -> dict:
        """Publish one DocumentManifest version pinning the unit tree."""
        ns, _, _ = (document_id.split("/", 1) + [""])[:3] if "/" in document_id else ("", None, None)
        if not document_id.startswith("strategy/documents/"):
            raise ValidationError("document_id must live under 'strategy/documents/'")
        pinned = {
            "document_id": document_id,
            "units": tree.get("units", []),
            "assembly_dependencies": assembly_dependencies or [],
        }
        self.validate_tree(pinned)
        kwargs = dict(
            logical_id=document_id,
            artifact_type="document_manifest",
            author=author,
            body=canonical_bytes(pinned),
            media_type="application/json+scisaurus-document",
            task_id=task_id,
        )
        if conn is not None:
            return self.store._publish_artifact_in(conn, **kwargs)
        return self.store.publish_artifact(**kwargs)

    def get_tree(self, manifest_ref: str) -> dict:
        manifest = self.store.get(manifest_ref)
        return json.loads(self.store.read_body(manifest["body_hash"]))