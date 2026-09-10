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

    def _grant(self, grant_id="g-1", cr_id="cr-1", units_map=None):
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
            actor="strategy.writer-1",
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
        self._grant(units_map={"strategy/units/u-1": {"ops": ["move"]}})
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
        self._grant(units_map={"strategy/units/u-1": {"ops": ["move"]}})
        with self.assertRaises(ConflictError):
            self.changes.apply_changeset(
                grant_id="g-1",
                ops=[{"op": "move", "unit": "strategy/units/u-1", "to_parent": None, "index": 2}],
                author="editorial.structural",
                expected_accepted_manifest_version=0,
            )
        self.assertEqual(self.store.versions("strategy/documents/doc-1"), [1])


if __name__ == "__main__":
    unittest.main()