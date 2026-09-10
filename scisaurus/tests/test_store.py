"""T01 (three immutable versions) and T02 (branching + adoption CAS)."""

import unittest

from scisaurus.core.events import ControlStore
from scisaurus.core.store import ArtifactStore
from scisaurus.core.errors import ConflictError
from scisaurus.core.schema import format_ref, parse_ref


class TestArtifactStore(unittest.TestCase):
    def setUp(self):
        import tempfile, os

        self.dir = tempfile.mkdtemp(prefix="scisaurus-t-")
        self.control = ControlStore(self.dir)
        self.store = ArtifactStore(self.control)
        self.store.init_project(principal_note="test")

    def tearDown(self):
        self.control.close()

    # T01
    def test_t01_three_immutable_versions(self):
        logical = "strategy/notes/n1"
        bodies = [b"alpha", b"beta", b"gamma"]
        manifests = []
        for body in bodies:
            manifests.append(
                self.store.publish_artifact(
                    logical_id=logical,
                    artifact_type="note",
                    author="research.chief",
                    body=body,
                    media_type="text/markdown",
                )
            )
        self.assertEqual(self.store.versions(logical), [1, 2, 3])
        # exact body hashes and bytes preserved
        for manifest, body in zip(manifests, bodies):
            import hashlib

            self.assertEqual(manifest["body_hash"], hashlib.sha256(body).hexdigest())
            self.assertEqual(
                self.store.read_body(manifest["body_hash"]), body
            )
        # parent links chain exactly
        self.assertEqual(manifests[0]["parents"], [])
        self.assertEqual(
            manifests[1]["parents"], [manifests[0]["artifact_ref"]]
        )
        self.assertEqual(
            manifests[2]["parents"], [manifests[1]["artifact_ref"]]
        )
        # immutable bodies: earlier versions still retrievable
        for manifest in manifests:
            got = self.store.get(manifest["artifact_ref"])
            self.assertEqual(got["body_hash"], manifest["body_hash"])
            self.assertEqual(got["version"], manifest["version"])

    # T02
    def test_t02_branch_candidates_and_cas(self):
        logical = "strategy/storyline/s1"
        base = self.store.publish_artifact(
            logical_id=logical, artifact_type="report", author="strategy.chief",
            body=b"base",
        )
        ref1 = base["artifact_ref"]
        cand_a = self.store.publish_artifact(
            logical_id=logical, artifact_type="note", author="writer-a",
            body=b"candidate-a", parents=[ref1],
        )
        cand_b = self.store.publish_artifact(
            logical_id=logical, artifact_type="note", author="writer-b",
            body=b"candidate-b", parents=[ref1],
        )
        # distinct versions, same parent, no lost update
        self.assertEqual((cand_a["version"], cand_b["version"]), (2, 3))
        self.assertEqual(cand_a["parents"], [ref1])
        self.assertEqual(cand_b["parents"], [ref1])
        self.assertEqual(self.store.read_body(cand_a["body_hash"]), b"candidate-a")
        self.assertEqual(self.store.read_body(cand_b["body_hash"]), b"candidate-b")
        # adopt A with correct expectation
        self.store.adopt(logical, target_version=2, expected_accepted_version=None, actor="arbiter")
        self.assertEqual(self.store.accepted(logical)["version"], 2)
        # stale CAS: expecting the old (empty) head is rejected — no lost update
        from scisaurus.core.errors import ConflictError

        with self.assertRaises(ConflictError):
            self.store.adopt(logical, target_version=3, expected_accepted_version=None, actor="arbiter")
        # correct CAS adopts B; A remains preserved for audit
        self.store.adopt(logical, target_version=3, expected_accepted_version=2, actor="arbiter")
        self.assertEqual(self.store.accepted(logical)["version"], 3)
        self.assertEqual(self.store.get(cand_a["artifact_ref"])["version"], 2)

    # T03 (part 1: crash before commit leaves only an orphan blob)
    def test_t03_orphan_blob_harmless(self):
        from scisaurus.core.errors import NotFoundError

        logical = "kb/notes/k1"
        h = self.store.publish_object(b"orphan-body", "text/plain")
        with self.assertRaises(NotFoundError):
            self.store.get(format_ref(logical, 1))
        # a later real publish reuses the same object bytes without error
        manifest = self.store.publish_artifact(
            logical_id=logical, artifact_type="note", author="research.chief",
            body=b"orphan-body",
        )
        self.assertEqual(manifest["body_hash"], h)

    # T03 (part 2): rollback on validation failure leaves no artifact
    def test_t03_invalid_publish_rolls_back(self):
        with self.assertRaises(Exception):
            self.store.publish_artifact(
                logical_id="strategy/notes/r1",
                artifact_type="not_a_registered_type",
                author="strategy.chief",
                body=b"x",
            )
        self.assertEqual(self.store.versions("strategy/notes/r1"), [])
        last_events = [e["event_type"] for e in self.control.replay()]
        self.assertNotIn("artifact.published", last_events)

    def test_malformed_outgoing_batch_rolls_back_publication(self):
        from scisaurus.core.errors import ValidationError
        from scisaurus.core.messages import MessageBus
        from scisaurus.tests.test_messages import envelope

        valid = envelope("m-good", "effect-good")
        malformed = envelope("m-bad", "effect-bad")
        malformed["type"] = "not_registered"
        before = list(self.control.replay())
        with self.assertRaises(ValidationError):
            self.store.publish_artifact(
                logical_id="kb/notes/outbox", artifact_type="note", author="research.chief",
                body=b"candidate", messages=[valid, malformed],
            )
        self.assertEqual(self.store.versions("kb/notes/outbox"), [])
        self.assertEqual(list(self.control.replay()), before)
        self.assertEqual(self.control._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.store.publish_artifact(
            logical_id="kb/notes/outbox", artifact_type="note", author="research.chief",
            body=b"candidate", messages=[valid],
        )
        bus = MessageBus(self.control)
        self.assertEqual(bus.recover_outbox(), 1)
        self.assertEqual(bus.pending(), ["m-good"])


if __name__ == "__main__":
    unittest.main()