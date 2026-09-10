"""T56–T58: scoped surgical changes — scope enforcement, protected spans,
identity/lineage preservation, and citation anchoring."""

import unittest

from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.documents import Documents
from scisaurus.core.changes import ChangeService
from scisaurus.core.errors import ConflictError, ValidationError


class ChangeFixture(unittest.TestCase):
    """Builds the slice document: heading + two paragraphs (one with a
    citation, one with a protected value) in a single manifest."""

    def setUp(self):
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="scisaurus-ch-")
        self.control = ControlStore(self.dir)
        self.store = ArtifactStore(self.control)
        self.store.init_project(principal_note="changes-test")
        self.documents = Documents(self.control, self.store)
        self.changes = ChangeService(self.control, self.store, self.documents)

        self.store.publish_artifact(
            logical_id="kb/references/r1",
            artifact_type="reference_card",
            author="research.cataloger",
            body=b'{"title": "Sleep and immediate recall"}',
            media_type="application/json",
        )
        self.u_sec = self.documents.publish_unit(
            logical_id="strategy/units/u-sec-1", kind="heading",
            text="Results", author="editorial.format",
        )
        self.u1_text = (
            "We observed higher recall in the normal-sleep group."
            " This matches prior findings."
        )
        self.u1 = self.documents.publish_unit(
            logical_id="strategy/units/u-1", kind="paragraph",
            text=self.u1_text, author="strategy.writer-1",
            citations=[{
                "occurrence_id": "c1",
                "span": [52, 76],
                "reference_card_ref": "artifact:kb/references/r1@1",
                "claim_ref": "artifact:strategy/claims/C1@1",
                "relation": "supports",
            }],
        )
        self.u2_text = "Participants recalled 14.1 vs 12.4 items."
        self.u2 = self.documents.publish_unit(
            logical_id="strategy/units/u-2", kind="paragraph",
            text=self.u2_text, author="strategy.writer-1",
        )
        self.doc = self.documents.publish_manifest(
            document_id="strategy/documents/doc-1",
            tree={"units": [
                {"ref": self.u_sec["artifact_ref"], "children": []},
                {"ref": self.u1["artifact_ref"], "children": []},
                {"ref": self.u2["artifact_ref"], "children": []},
            ]},
            author="editorial.chief",
        )
        self.store.adopt(
            "strategy/documents/doc-1",
            target_version=1, expected_accepted_version=None, actor="command",
        )

    def tearDown(self):
        self.control.close()

    def _grant(self, grant_id="g-1", cr_id="cr-1", units_map=None, actor="strategy.writer-1"):
        units_map = units_map or {"strategy/units/u-1": {"ops": ["replace_body"]}}
        cr = self.changes.create_change_request(
            cr_id=cr_id,
            author="command",
            purpose="fix the overclaim",
            baseline_manifest_ref=self.doc["artifact_ref"],
            scope={"units": sorted(units_map)},
            preservation=["required findings and values survive"],
        )
        return self.changes.issue_grant(
            grant_id=grant_id,
            request_ref=cr["artifact_ref"],
            baseline_manifest_ref=self.doc["artifact_ref"],
            actor=actor,
            units=units_map,
        )

    def _u1_hash(self):
        return self.store.get(self.u1["artifact_ref"])["body_hash"]

    def _u2_hash(self):
        return self.store.get(self.u2["artifact_ref"])["body_hash"]


class TestT56ScopeEnforcement(ChangeFixture):
    def test_out_of_scope_unit_rejected_and_manifest_unchanged(self):
        self._grant(units_map={"strategy/units/u-1": {"ops": ["replace_body"]}})
        ops = [
            {"op": "replace_body", "unit": "strategy/units/u-1",
             "expected_unit_version": 1, "expected_body_hash": self._u1_hash(),
             "span": [0, 10], "new_text": "Higher recall was observed"},
            {"op": "replace_body", "unit": "strategy/units/u-2",
             "expected_unit_version": 1, "expected_body_hash": self._u2_hash(),
             "span": [0, 5], "new_text": "XX"},
        ]
        with self.assertRaises(ValidationError):
            self.changes.apply_changeset(
                grant_id="g-1", ops=ops, author="strategy.writer-1",
                expected_accepted_manifest_version=1,
            )
        self.assertEqual(self.store.versions("strategy/documents/doc-1"), [1])

    def test_foreign_document_and_unpermitted_op_rejected(self):
        self._grant(units_map={"strategy/units/u-1": {"ops": ["replace_body"]}})
        with self.assertRaises(ValidationError):
            self.changes.apply_changeset(
                grant_id="g-1",
                ops=[{"op": "replace_body", "unit": "strategy/units/u-1",
                      "expected_unit_version": 1, "expected_body_hash": self._u1_hash(),
                      "span": [0, 10], "new_text": "x",
                      "target_document_ref": "artifact:strategy/documents/other@1"}],
                author="strategy.writer-1",
                expected_accepted_manifest_version=1,
            )
        self._grant(grant_id="g-2", cr_id="cr-2",
                    units_map={"strategy/units/u-1": {"ops": ["move"]}})
        with self.assertRaises(ValidationError):
            self.changes.apply_changeset(
                grant_id="g-2",
                ops=[{"op": "retire", "unit": "strategy/units/u-1"}],
                author="strategy.writer-1",
                expected_accepted_manifest_version=1,
            )
        self.assertEqual(self.store.versions("strategy/documents/doc-1"), [1])


class TestT57ProtectedSpans(ChangeFixture):
    def test_protected_span_change_rejected_but_other_edits_pass(self):
        start = self.u2_text.find("14.1")
        self._grant(units_map={
            "strategy/units/u-2": {
                "ops": ["replace_body"],
                "protected_spans": [[start, start + 4]],
            },
        })
        with self.assertRaises(ValidationError):
            self.changes.apply_changeset(
                grant_id="g-1",
                ops=[{"op": "replace_body", "unit": "strategy/units/u-2",
                      "expected_unit_version": 1, "expected_body_hash": self._u2_hash(),
                      "span": [start, start + 4], "new_text": "99.9"}],
                author="strategy.writer-1",
                expected_accepted_manifest_version=1,
            )
        self.assertEqual(self.store.versions("strategy/documents/doc-1"), [1])
        result = self.changes.apply_changeset(
            grant_id="g-1",
            ops=[{"op": "replace_body", "unit": "strategy/units/u-2",
                  "expected_unit_version": 1, "expected_body_hash": self._u2_hash(),
                  "span": [0, 12], "new_text": "In this experiment, the"}],
            author="strategy.writer-1",
            expected_accepted_manifest_version=1,
        )
        self.assertEqual(result["manifest_version"], 2)
        accepted = self.store.accepted("strategy/documents/doc-1")
        tree = self.documents.get_tree(accepted["artifact_ref"])
        u2_ref = [n["ref"] for n in tree["units"] if "u-2" in n["ref"]][0]
        unit = self.documents.read_unit(u2_ref)
        self.assertIn("14.1", unit["text"])
        self.assertIn("In this experiment", unit["text"])


class TestT58IdentityLineageAnchors(ChangeFixture):
    def test_move_preserves_identity_without_new_unit_version(self):
        self._grant(units_map={"strategy/units/u-1": {"ops": ["move"]}}, actor="editorial.structural")
        result = self.changes.apply_changeset(
            grant_id="g-1",
            ops=[{"op": "move", "unit": "strategy/units/u-1",
                  "to_parent": None, "index": 2}],
            author="editorial.structural",
            expected_accepted_manifest_version=1,
        )
        tree = self.documents.get_tree(result["manifest_ref"])
        refs = [n["ref"] for n in tree["units"]]
        self.assertEqual(refs[-1], self.u1["artifact_ref"])  # moved to index 2
        self.assertEqual(self.store.versions("strategy/units/u-1"), [1])  # no new body version

    def test_split_preserves_lineage_and_forbids_id_reuse(self):
        self._grant(units_map={"strategy/units/u-1": {"ops": ["split"]}})
        split_at = self.u1_text.find(" This")  # 52
        result = self.changes.apply_changeset(
            grant_id="g-1",
            ops=[{"op": "split", "unit": "strategy/units/u-1",
                  "offset": split_at,
                  "left_unit": "strategy/units/u-1a", "right_unit": "strategy/units/u-1b",
                  "left_text": self.u1_text[:split_at],
                  "right_text": self.u1_text[split_at:],
                  "citations_left": [], "citations_right": ["c1"]}],
            author="strategy.writer-1",
            expected_accepted_manifest_version=1,
        )
        tree = self.documents.get_tree(result["manifest_ref"])
        refs = [n["ref"] for n in tree["units"]]
        left_ref = [r for r in refs if "u-1a" in r][0]
        right_ref = [r for r in refs if "u-1b" in r][0]
        left = self.documents.read_unit(left_ref)
        right = self.documents.read_unit(right_ref)
        self.assertEqual(left["lineage"]["derived_from"], [self.u1["artifact_ref"]])
        self.assertEqual(right["lineage"]["derived_from"], [self.u1["artifact_ref"]])
        self.assertEqual(right["citations"][0]["occurrence_id"], "c1")
        # the retired id is never reused
        with self.assertRaises(ValidationError):
            self.documents.publish_unit(
                logical_id="strategy/units/u-1", kind="paragraph",
                text="pretend this is new", author="strategy.writer-1",
            )

    def test_citation_inside_replaced_span_requires_reanchor(self):
        self._grant(units_map={"strategy/units/u-1": {"ops": ["replace_body"]}})
        with self.assertRaises(ValidationError):
            self.changes.apply_changeset(
                grant_id="g-1",
                ops=[{"op": "replace_body", "unit": "strategy/units/u-1",
                      "expected_unit_version": 1, "expected_body_hash": self._u1_hash(),
                      "span": [52, 76], "new_text": "consistent with earlier reports."}],
                author="strategy.writer-1",
                expected_accepted_manifest_version=1,
            )
        result = self.changes.apply_changeset(
            grant_id="g-1",
            ops=[{"op": "replace_body", "unit": "strategy/units/u-1",
                  "expected_unit_version": 1, "expected_body_hash": self._u1_hash(),
                  "span": [52, 76], "new_text": "consistent with earlier reports.",
                  "reanchors": [{"occurrence_id": "c1", "new_span": [52, 76]}]}],
            author="strategy.writer-1",
            expected_accepted_manifest_version=1,
        )
        accepted = self.store.accepted("strategy/documents/doc-1")
        tree = self.documents.get_tree(accepted["artifact_ref"])
        new_ref = [n["ref"] for n in tree["units"] if "u-1" in n["ref"]][0]
        unit = self.documents.read_unit(new_ref)
        self.assertIn("consistent with earlier reports", unit["text"])
        self.assertEqual(unit["citations"][0]["occurrence_id"], "c1")
        self.assertEqual(unit["citations"][0]["span"], [52, 76])

    def test_stale_cas_expectation_rejected(self):
        self._grant(units_map={"strategy/units/u-1": {"ops": ["move"]}}, actor="editorial.structural")
        with self.assertRaises(ConflictError):
            self.changes.apply_changeset(
                grant_id="g-1",
                ops=[{"op": "move", "unit": "strategy/units/u-1", "to_parent": None, "index": 2}],
                author="editorial.structural",
                expected_accepted_manifest_version=0,
            )
        self.assertEqual(self.store.versions("strategy/documents/doc-1"), [1])


class ChangeIntegrityFixture(ChangeFixture):
    document_id = "strategy/documents/doc-1"
    heading_id = "strategy/units/u-sec-1"
    u1_id = "strategy/units/u-1"
    u2_id = "strategy/units/u-2"

    def _replace(self, logical, start, end, text, **extra):
        original = {self.u1_id: self.u1, self.u2_id: self.u2}[logical]
        return {
            "op": "replace_body", "unit": logical,
            "expected_unit_version": original["version"],
            "expected_body_hash": original["body_hash"],
            "span": [start, end], "new_text": text, **extra,
        }

    def _apply(self, ops, *, grant_id="g-1", expected=None):
        return self.changes.apply_changeset(
            grant_id=grant_id, ops=ops, author="strategy.writer-1",
            expected_accepted_manifest_version=self.doc["version"] if expected is None else expected,
        )

    def _unit(self, manifest_ref, logical):
        node, _, _ = self.changes._find(self.documents.get_tree(manifest_ref), logical)
        return self.documents.read_unit(node["ref"])

    def _state(self):
        return {
            table: [tuple(row) for row in self.control._conn.execute(f"SELECT * FROM {table}")]
            for table in ("artifacts", "accepted_heads", "retired_units", "events")
        }

    def _nest(self):
        old_version = self.doc["version"]
        self.doc = self.documents.publish_manifest(
            document_id=self.document_id,
            tree={"units": [{"ref": self.u_sec["artifact_ref"], "children": [
                {"ref": self.u1["artifact_ref"], "children": []},
                {"ref": self.u2["artifact_ref"], "children": []},
            ]}]}, author="command",
        )
        self.store.adopt(self.document_id, target_version=self.doc["version"],
                         expected_accepted_version=old_version, actor="command")

    def _split(self, logical=None, text=None, offset=None, **extra):
        logical = self.u1_id if logical is None else logical
        text = self.u1_text if text is None else text
        offset = text.index(" This") if offset is None else offset
        return {
            "op": "split", "unit": logical, "offset": offset,
            "left_unit": "strategy/units/left", "right_unit": "strategy/units/right",
            "left_text": text[:offset], "right_text": text[offset:],
            "citations_left": [], "citations_right": ["c1"] if logical == self.u1_id else [],
            **extra,
        }



class TestChangeIntegrity(ChangeIntegrityFixture):
    def test_stale_baseline_cannot_overwrite_intervening_acceptance(self):
        self._grant()
        self._grant(grant_id="g-2", cr_id="cr-2",
                    units_map={self.u2_id: {"ops": ["replace_body"]}})
        first = self._apply([self._replace(self.u1_id, 0, 2, "Scientists")])
        before = self._state()
        with self.assertRaises(ConflictError):
            self._apply([self._replace(self.u2_id, 0, 12, "Subjects")], grant_id="g-2", expected=2)
        self.assertEqual(self._state(), before)
        self.assertEqual(self.store.accepted(self.document_id)["artifact_ref"], first["manifest_ref"])
        self.assertTrue(self._unit(first["manifest_ref"], self.u1_id)["text"].startswith("Scientists"))
        self.doc = self.store.accepted(self.document_id)
        self._grant(grant_id="g-3", cr_id="cr-3", units_map={self.u2_id: {"ops": ["replace_body"]}})
        second = self._apply([self._replace(self.u2_id, 0, 12, "Subjects")], grant_id="g-3")
        self.assertTrue(self._unit(second["manifest_ref"], self.u1_id)["text"].startswith("Scientists"))
        self.assertTrue(self._unit(second["manifest_ref"], self.u2_id)["text"].startswith("Subjects"))

    def test_another_actor_cannot_apply_a_valid_edit_grant(self):
        self._grant()
        op = self._replace(self.u1_id, 0, 2, "Scientists")
        before = self._state()
        with self.assertRaisesRegex(ValidationError, "grant actor"):
            self.changes.apply_changeset(
                grant_id="g-1", ops=[op], author="strategy.writer-2",
                expected_accepted_manifest_version=1,
            )
        self.assertEqual(self._state(), before)
        result = self._apply([op])
        self.assertTrue(self._unit(result["manifest_ref"], self.u1_id)["text"].startswith("Scientists"))

    def test_parent_retirement_requires_descendant_authority(self):
        self._nest()
        self._grant(units_map={self.heading_id: {"ops": ["retire"]}})
        before = self._state()
        with self.assertRaises(ValidationError):
            self._apply([{"op": "retire", "unit": self.heading_id}])
        self.assertEqual(self._state(), before)

    def test_authorized_subtree_retirement_records_every_identity(self):
        self._nest()
        unit_ids = [self.heading_id, self.u1_id, self.u2_id]
        self._grant(units_map={logical: {"ops": ["retire"]} for logical in unit_ids})
        result = self._apply([{"op": "retire", "unit": self.heading_id}])
        self.assertEqual(result["retired_units"], unit_ids)
        self.assertEqual(self.documents.get_tree(result["manifest_ref"])["units"], [])
        retired = self.control._conn.execute("SELECT logical_id FROM retired_units").fetchall()
        self.assertEqual({row["logical_id"] for row in retired}, set(unit_ids))
        for logical in unit_ids:
            with self.assertRaises(ValidationError):
                self.documents.publish_unit(logical_id=logical, kind="paragraph", text="reuse", author="command")

    def test_parent_split_cannot_discard_children(self):
        self._nest()
        self._grant(units_map={self.heading_id: {"ops": ["split"]}})
        before = self._state()
        with self.assertRaises(ValidationError):
            self._apply([self._split(self.heading_id, "Results", 3)])
        self.assertEqual(self._state(), before)

    def test_split_cannot_rewrite_protected_content(self):
        start = self.u2_text.index("14.1")
        self._grant(units_map={self.u2_id: {"ops": ["split"], "protected_spans": [[start, start + 4]]}})
        before = self._state()
        with self.assertRaises(ValidationError):
            self._apply([self._split(self.u2_id, self.u2_text, 1,
                                     left_text="Unrelated", right_text="All findings removed")])
        self.assertEqual(self._state(), before)
        result = self._apply([self._split(self.u2_id, self.u2_text, start)])
        left = self._unit(result["manifest_ref"], "strategy/units/left")
        right = self._unit(result["manifest_ref"], "strategy/units/right")
        self.assertEqual(left["text"] + right["text"], self.u2_text)
        self.assertTrue(right["text"].startswith("14.1"))

    def test_split_cannot_bisect_protected_span_or_duplicate_identity(self):
        start = self.u2_text.index("14.1")
        self._grant(units_map={self.u2_id: {"ops": ["split"], "protected_spans": [[start, start + 4]]}})
        before = self._state()
        for op in (
            self._split(self.u2_id, self.u2_text, start + 1),
            self._split(self.u2_id, self.u2_text, 1, right_unit="strategy/units/left"),
        ):
            with self.subTest(op=op), self.assertRaises(ValidationError):
                self._apply([op])
            self.assertEqual(self._state(), before)

    def test_prefix_edit_remaps_downstream_anchor_to_same_text(self):
        self._grant()
        original = self.documents.read_unit(self.u1["artifact_ref"])
        result = self._apply([self._replace(self.u1_id, 0, 2, "Scientists")])
        revised = self._unit(result["manifest_ref"], self.u1_id)
        before_start, before_end = original["citations"][0]["span"]
        after_start, after_end = revised["citations"][0]["span"]
        self.assertEqual([after_start, after_end], [before_start + 8, before_end + 8])
        self.assertEqual(original["text"][before_start:before_end], revised["text"][after_start:after_end])

    def test_split_remaps_right_anchor_to_child_coordinates(self):
        self._grant(units_map={self.u1_id: {"ops": ["split"]}})
        original = self.documents.read_unit(self.u1["artifact_ref"])
        op = self._split()
        result = self._apply([op])
        right = self._unit(result["manifest_ref"], "strategy/units/right")
        start, end = original["citations"][0]["span"]
        self.assertEqual(right["citations"][0]["span"], [start - op["offset"], end - op["offset"]])
        self.assertEqual(right["text"][start - op["offset"]:end - op["offset"]], original["text"][start:end])

    def test_split_requires_non_crossing_unique_correct_citation_homes(self):
        self._grant(units_map={self.u1_id: {"ops": ["split"]}})
        before = self._state()
        for op in (
            self._split(offset=60),
            self._split(citations_left=["c1"], citations_right=["c1"]),
            self._split(citations_left=["c1"], citations_right=[]),
            self._split(citations_right=["c1", "c1"]),
        ):
            with self.subTest(op=op), self.assertRaises(ValidationError):
                self._apply([op])
            self.assertEqual(self._state(), before)

    def test_multiple_replacements_compose_original_coordinates_once(self):
        self._grant(units_map={self.u2_id: {"ops": ["replace_body"]}})
        start = self.u2_text.index("14.1")
        result = self._apply([
            self._replace(self.u2_id, start, start + 4, "15.0"),
            self._replace(self.u2_id, 0, 12, "Subjects"),
        ])
        self.assertEqual(self._unit(result["manifest_ref"], self.u2_id)["text"],
                         "Subjects recalled 15.0 vs 12.4 items.")
        self.assertEqual(self.store.versions(self.u2_id), [1, 2])
        self.assertEqual(result["changed_units"], [self.u2_id])

    def test_overlapping_or_destructive_composites_reject_atomically(self):
        self._grant(units_map={self.u2_id: {"ops": ["replace_body", "split", "retire"]}})
        before = self._state()
        for ops in (
            [self._replace(self.u2_id, 0, 12, "Subjects"), self._replace(self.u2_id, 3, 15, "X")],
            [self._replace(self.u2_id, 0, 0, "A"), self._replace(self.u2_id, 0, 0, "B")],
            [self._replace(self.u2_id, 0, 12, "Subjects"), self._split(self.u2_id, self.u2_text, 13)],
            [self._replace(self.u2_id, 0, 12, "Subjects"), {"op": "retire", "unit": self.u2_id}],
        ):
            with self.subTest(ops=ops), self.assertRaises(ValidationError):
                self._apply(ops)
            self.assertEqual(self._state(), before)

    def test_reanchors_are_validated_in_final_composed_coordinates(self):
        self._grant()
        prefix = self._replace(self.u1_id, 0, 2, "Scientists")
        replacement = self._replace(self.u1_id, 52, 76, "Earlier findings agree.",
                                    reanchors=[{"occurrence_id": "c1", "new_span": [60, 83]}])
        before = self._state()
        invalid = {**replacement, "reanchors": [{"occurrence_id": "c1", "new_span": [60, 1000]}]}
        with self.assertRaises(ValidationError):
            self._apply([prefix, invalid])
        self.assertEqual(self._state(), before)
        result = self._apply([prefix, replacement])
        revised = self._unit(result["manifest_ref"], self.u1_id)
        start, end = revised["citations"][0]["span"]
        self.assertEqual(revised["text"][start:end], "Earlier findings agree.")

    def test_move_and_replace_preserve_both_changes(self):
        self._grant(units_map={self.u2_id: {"ops": ["replace_body", "move"]}})
        result = self._apply([
            self._replace(self.u2_id, 0, 12, "Subjects"),
            {"op": "move", "unit": self.u2_id, "to_parent": None, "index": 0},
        ])
        tree = self.documents.get_tree(result["manifest_ref"])
        self.assertEqual(tree["units"][0]["ref"], f"artifact:{self.u2_id}@2")
        self.assertTrue(self._unit(result["manifest_ref"], self.u2_id)["text"].startswith("Subjects"))

    def test_split_and_another_unit_edit_integrate_together(self):
        self._grant(units_map={self.u1_id: {"ops": ["split"]}, self.u2_id: {"ops": ["replace_body"]}})
        result = self._apply([self._split(), self._replace(self.u2_id, 0, 12, "Subjects")])
        left = self._unit(result["manifest_ref"], "strategy/units/left")
        right = self._unit(result["manifest_ref"], "strategy/units/right")
        self.assertEqual(left["text"] + right["text"], self.u1_text)
        self.assertTrue(self._unit(result["manifest_ref"], self.u2_id)["text"].startswith("Subjects"))

    def test_manifest_publication_failure_rolls_back_unit_versions_and_events(self):
        from unittest.mock import patch

        self._grant()
        before = self._state()
        with patch.object(self.documents, "publish_manifest", side_effect=RuntimeError("assembly failed")):
            with self.assertRaisesRegex(RuntimeError, "assembly failed"):
                self._apply([self._replace(self.u1_id, 0, 2, "Scientists")])
        self.assertEqual(self._state(), before)

    def test_ancestor_retire_cannot_discard_another_planned_edit(self):
        self._nest()
        self._grant(units_map={self.heading_id: {"ops": ["retire"]},
                              self.u1_id: {"ops": ["retire", "replace_body"]},
                              self.u2_id: {"ops": ["retire"]}})
        before = self._state()
        with self.assertRaises(ValidationError):
            self._apply([self._replace(self.u1_id, 0, 2, "Scientists"),
                         {"op": "retire", "unit": self.heading_id}])
        self.assertEqual(self._state(), before)

    def test_assembly_dependencies_survive_manifest_and_local_publication(self):
        reference = "artifact:kb/references/r1@1"
        tree = self.documents.get_tree(self.doc["artifact_ref"])
        tree["assembly_dependencies"] = [reference]
        self.doc = self.documents.publish_manifest(document_id=self.document_id, tree=tree, author="command")
        self.store.adopt(self.document_id, target_version=2, expected_accepted_version=1, actor="command")
        self.assertEqual(self.documents.get_tree(self.doc["artifact_ref"])["assembly_dependencies"], [reference])
        self._grant(units_map={self.u2_id: {"ops": ["replace_body"]}})
        result = self._apply([self._replace(self.u2_id, 0, 12, "Subjects")])
        self.assertEqual(self.documents.get_tree(result["manifest_ref"])["assembly_dependencies"], [reference])
        explicit = self.documents.publish_manifest(document_id=self.document_id, tree=tree,
                                                    assembly_dependencies=[], author="command")
        self.assertEqual(self.documents.get_tree(explicit["artifact_ref"])["assembly_dependencies"], [])

    def test_invalid_citation_coordinates_are_rejected_at_unit_publication(self):
        for span in ([0, 100], [3, 2], [0, 0], [0.5, 1], [False, 1]):
            with self.subTest(span=span), self.assertRaises(ValidationError):
                self.documents.publish_unit(
                    logical_id="strategy/units/invalid", kind="paragraph", text="text", author="command",
                    citations=[{"occurrence_id": "invalid", "span": span,
                                "reference_card_ref": "artifact:kb/references/r1@1"}],
                )
        self.assertEqual(self.store.versions("strategy/units/invalid"), [])


class TestStagedChangeAcceptance(ChangeIntegrityFixture):
    def _stage(self, changeset_id="stage-1", text="Scientists", grant_id="g-1"):
        return self.changes.stage_changeset(
            changeset_id=changeset_id, grant_id=grant_id,
            ops=[self._replace(self.u1_id, 0, 2, text)], author="strategy.writer-1",
            expected_accepted_manifest_version=self.doc["version"],
        )

    def _verify_stage(self, stage, *, outcome="passed"):
        from scisaurus.core.schema import canonical_bytes
        from scisaurus.review.issues import IssueManager

        reviews = IssueManager(self.control, self.store, self.documents)
        criterion = self.store.publish_artifact(
            logical_id="kb/criteria/stage", artifact_type="note", author="command",
            body=b"Clarify the subject while preserving the remaining findings.",
        )
        critique = reviews.publish_critique(
            critique_id="stage", author_role="review.critic", target_ref=self.doc["artifact_ref"],
            target_location="strategy/units/u-1", criterion_ref=criterion["artifact_ref"],
            allegation="The subject is underspecified.",
            basis={"counterexample": "The paragraph begins with an unresolved pronoun."},
            material_impact="Readers cannot identify the investigators.", proposed_severity="major",
            resolution_condition="The subject is explicit and the findings remain intact.",
        )
        reviews.register_issue(issue_id="issue-stage", critique_ref=critique["artifact_ref"])
        reviews.triage("issue-stage", "review.triage", admissible=True, reason="Concrete repair criterion")
        response = reviews.respond("issue-stage", response_id="stage", author="strategy.writer-1",
                                   stance="accept", candidate_ref=stage["manifest_ref"])
        binding = {
            "issue_id": "issue-stage", "critique_ref": critique["artifact_ref"],
            "response_ref": response["artifact_ref"], "candidate_ref": stage["manifest_ref"],
            "baseline_ref": self.doc["artifact_ref"],
            "resolution_condition": "The subject is explicit and the findings remain intact.",
        }
        refs = []
        for kind in ("resolution", "regression"):
            evidence = self.store.publish_artifact(
                logical_id=f"issues/checks/stage-{kind}", artifact_type="evidence_record",
                author="review.verifier",
                body=canonical_bytes({**binding, "check_id": kind, "kind": kind,
                                      "outcome": outcome, "method": "Read exact before and after manifests",
                                      "result": "Compared the subject, untouched paragraph, values, and anchors."}),
                media_type="application/json",
                inputs=[{"ref": ref, "purpose": "subject"}
                        for ref in (self.doc["artifact_ref"], stage["manifest_ref"])],
            )
            refs.append(evidence["artifact_ref"])
        verification = reviews.verify(
            "issue-stage", verification_id="stage", verifier="review.verifier",
            response_ref=response["artifact_ref"], candidate_ref=stage["manifest_ref"],
            baseline_ref=self.doc["artifact_ref"], resolution_condition=binding["resolution_condition"],
            check_refs=refs, rationale="Executed resolution and preservation checks.",
        )
        return verification

    def _accept(self, stage, verification, **extra):
        return self.changes.accept_changeset(
            changeset_ref=stage["changeset_ref"], verification_ref=verification["artifact_ref"],
            author="strategy.writer-1", expected_accepted_manifest_version=self.doc["version"], **extra,
        )

    def test_staging_leaves_accepted_document_untouched_until_verified_acceptance(self):
        self._grant()
        stage = self._stage()
        self.assertEqual(self.store.accepted(self.document_id)["artifact_ref"], self.doc["artifact_ref"])
        self.assertEqual(self.store.versions(self.u1_id), [1, 2])
        self.assertEqual(self.control._conn.execute("SELECT COUNT(*) FROM retired_units").fetchone()[0], 0)
        self.assertEqual(self.store.get(stage["manifest_ref"])["parents"], [self.doc["artifact_ref"]])
        self.assertTrue(self._unit(stage["manifest_ref"], self.u1_id)["text"].startswith("Scientists"))
        verification = self._verify_stage(stage)
        self.assertEqual(self.store.accepted(self.document_id)["artifact_ref"], self.doc["artifact_ref"])
        result = self._accept(stage, verification)
        self.assertEqual(result["manifest_ref"], stage["manifest_ref"])
        self.assertEqual(self.store.accepted(self.document_id)["artifact_ref"], stage["manifest_ref"])
        self.assertEqual(self._unit(stage["manifest_ref"], self.u2_id)["text"], self.u2_text)

    def test_staging_rejects_structural_operations_and_missing_identity_without_effects(self):
        self._grant(units_map={self.u1_id: {"ops": ["replace_body", "split"]}})
        before = self._state()
        with self.assertRaises(ValidationError):
            self.changes.stage_changeset(changeset_id="structural", grant_id="g-1", ops=[self._split()],
                                         author="strategy.writer-1", expected_accepted_manifest_version=1)
        self.assertEqual(self._state(), before)
        for identity in (None, "", " "):
            with self.subTest(identity=identity), self.assertRaises(ValidationError):
                self._stage(changeset_id=identity)
            self.assertEqual(self._state(), before)

    def test_staging_identity_collision_does_not_publish_another_candidate(self):
        self._grant()
        self._stage()
        before = self._state()
        with self.assertRaises(ConflictError):
            self._stage()
        self.assertEqual(self._state(), before)

    def test_failed_verification_does_not_adopt_candidate(self):
        self._grant()
        stage = self._stage()
        verification = self._verify_stage(stage, outcome="failed")
        before = self._state()
        with self.assertRaises(ValidationError):
            self._accept(stage, verification)
        self.assertEqual(self._state(), before)

    def test_copy_of_passing_verification_is_not_a_committed_review_decision(self):
        self._grant()
        stage = self._stage()
        verification = self._verify_stage(stage)
        copy = self.store.publish_artifact(
            logical_id="issues/verifications/copied", artifact_type="verification",
            author="review.verifier", body=self.store.read_body(verification["body_hash"]),
            media_type="application/json",
        )
        before = self._state()
        with self.assertRaisesRegex(ValidationError, "committed passing decision"):
            self._accept(stage, copy)
        self.assertEqual(self._state(), before)

    def test_verification_cannot_transfer_to_a_different_staged_candidate(self):
        self._grant()
        stage = self._stage()
        alternative = self._stage(changeset_id="stage-2", text="Researchers")
        verification = self._verify_stage(stage)
        before = self._state()
        with self.assertRaisesRegex(ValidationError, "exact candidate"):
            self._accept(alternative, verification)
        self.assertEqual(self._state(), before)
        self.assertEqual(self.store.get(alternative["manifest_ref"])["parents"], [self.doc["artifact_ref"]])

    def test_accepted_head_advance_blocks_even_verified_staged_candidate(self):
        self._grant()
        stage = self._stage()
        verification = self._verify_stage(stage)
        self._grant(grant_id="g-2", cr_id="cr-2", units_map={self.u2_id: {"ops": ["replace_body"]}})
        advanced = self._apply([self._replace(self.u2_id, 0, 12, "Subjects")], grant_id="g-2")
        before = self._state()
        with self.assertRaises(ConflictError):
            self.changes.accept_changeset(
                changeset_ref=stage["changeset_ref"], verification_ref=verification["artifact_ref"],
                author="strategy.writer-1", expected_accepted_manifest_version=advanced["manifest_version"],
            )
        self.assertEqual(self._state(), before)

    def test_revoked_grant_blocks_verified_candidate_acceptance(self):
        from scisaurus.core.errors import StateError

        self._grant()
        stage = self._stage()
        verification = self._verify_stage(stage)
        self.changes.revoke_grant("g-1", "command")
        before = self._state()
        with self.assertRaises(StateError):
            self._accept(stage, verification)
        self.assertEqual(self._state(), before)

    def test_expired_grant_blocks_verified_candidate_acceptance(self):
        from unittest.mock import patch
        from scisaurus.core.errors import StateError

        self._grant()
        stage = self._stage()
        verification = self._verify_stage(stage)
        expires_at = self.changes.get_grant("g-1")["expires_at"]
        before = self._state()
        with patch("scisaurus.core.changes.time.time", return_value=expires_at + 1):
            with self.assertRaises(StateError):
                self._accept(stage, verification)
        self.assertEqual(self._state(), before)

    def test_staged_candidate_and_review_survive_store_reopen(self):
        self._grant()
        stage = self._stage()
        verification = self._verify_stage(stage)
        self.control.close()
        self.control = ControlStore(self.dir)
        self.store = ArtifactStore(self.control)
        self.documents = Documents(self.control, self.store)
        self.changes = ChangeService(self.control, self.store, self.documents)
        result = self._accept(stage, verification)
        self.assertEqual(result["manifest_ref"], stage["manifest_ref"])
        self.assertEqual(self.store.accepted(self.document_id)["artifact_ref"], stage["manifest_ref"])

    def test_other_actor_cannot_accept_a_verified_candidate(self):
        self._grant()
        stage = self._stage()
        verification = self._verify_stage(stage)
        before = self._state()
        with self.assertRaisesRegex(ValidationError, "grant actor"):
            self.changes.accept_changeset(
                changeset_ref=stage["changeset_ref"], verification_ref=verification["artifact_ref"],
                author="strategy.writer-2", expected_accepted_manifest_version=1,
            )
        self.assertEqual(self._state(), before)

    def test_acceptance_event_failure_rolls_back_head_transition(self):
        from unittest.mock import patch

        self._grant()
        stage = self._stage()
        verification = self._verify_stage(stage)
        before = self._state()
        append = self.control.append_event

        def fail_integration(*args, **kwargs):
            if kwargs["event_type"] == "changeset.integrated":
                raise RuntimeError("integration event failed")
            return append(*args, **kwargs)

        with patch.object(self.control, "append_event", side_effect=fail_integration):
            with self.assertRaisesRegex(RuntimeError, "integration event failed"):
                self._accept(stage, verification)
        self.assertEqual(self._state(), before)


if __name__ == "__main__":
    unittest.main()
