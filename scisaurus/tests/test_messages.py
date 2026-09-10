"""T04: duplicate delivery and lease expiry — one effect, stale worker fenced."""

import unittest

from scisaurus.core.events import ControlStore
from scisaurus.core.messages import MessageBus
from scisaurus.core.errors import StaleFenceError, StateError


def envelope(mid: str, key: str) -> dict:
    return {
        "message_id": mid,
        "project_id": "p-001",
        "type": "request",
        "from": {"dept": "strategy", "agent": "chief"},
        "to": {"dept": "research", "agent": "chief"},
        "subject": "re-survey claim A-3",
        "body": "claim A-3 lacks evidence; request counterexample search",
        "refs": ["artifact:strategy/claims/A-3@1"],
        "created_at": "2026-09-10T00:00:00+00:00",
        "idempotency_key": key,
    }


class TestMessageBus(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="scisaurus-msg-")
        self.control = ControlStore(self.dir)
        self.bus = MessageBus(self.control)

    def tearDown(self):
        self.control.close()

    def test_ack_requires_valid_lease(self):
        self.bus.publish(envelope("m-1", "effect-1"))
        # acknowledging without a lease is a state error, not a silent success
        with self.assertRaises(StateError):
            self.bus.acknowledge("m-1", 1, "scheduled", "worker-0")
        lease = self.bus.lease("m-1", owner="worker-1", ttl_seconds=60)
        key, created = self.bus.acknowledge("m-1", lease, "scheduled", "worker-1")
        self.assertTrue(created)
        self.assertEqual(self.bus.state_of("m-1")["state"], "acknowledged")

    def test_t04_stale_worker_fenced_and_single_effect(self):
        env = envelope("m-2", "effect-2")
        self.bus.publish(env)
        # worker-1 leases, its lease then expires
        fence1 = self.bus.lease("m-2", owner="worker-1", ttl_seconds=-1)
        self.bus.expire_stale_leases()
        # worker-2 takes over with a higher fencing token
        fence2 = self.bus.lease("m-2", owner="worker-2", ttl_seconds=60)
        self.assertGreater(fence2, fence1)
        # stale worker cannot commit effects
        with self.assertRaises(StaleFenceError):
            self.bus.acknowledge("m-2", fence1, "scheduled", "worker-1")
        # current worker commits
        key, created = self.bus.acknowledge("m-2", fence2, "scheduled", "worker-2")
        self.assertTrue(created)
        # duplicate delivery of the same idempotent effect
        self.bus.publish(envelope("m-2b", "effect-2"))
        fence3 = self.bus.lease("m-2b", owner="worker-3", ttl_seconds=30)
        key2, created2 = self.bus.acknowledge("m-2b", fence3, "scheduled", "worker-3")
        self.assertEqual(key2, key)
        self.assertFalse(created2)  # effect not committed twice
        rows = self.control._conn.execute(
            "SELECT COUNT(*) c FROM effects WHERE effect_key = 'effect-2'"
        ).fetchone()
        self.assertEqual(rows["c"], 1)

    def test_t03_outbox_recovery(self):
        from scisaurus.core.store import ArtifactStore
        from scisaurus.core.schema import canonical_bytes

        store = ArtifactStore(self.control)
        store.init_project(principal_note="test")
        env = envelope("m-3", "effect-3")
        store.publish_artifact(
            logical_id="kb/notes/o1",
            artifact_type="note",
            author="research.chief",
            body=b"body",
            messages=[env],
        )
        # crash before dispatch: message not visible yet
        self.assertEqual(self.bus.pending(), [])
        n = self.bus.recover_outbox()
        self.assertEqual(n, 1)
        self.assertIn("m-3", self.bus.pending())
        # recovery is idempotent
        self.assertEqual(self.bus.recover_outbox(), 0)

    def test_expired_message_is_discoverable_after_restart(self):
        self.bus.publish(envelope("m-retry", "retry-effect"))
        old = self.bus.lease("m-retry", "first-worker", ttl_seconds=-1)
        self.assertEqual(self.bus.expire_stale_leases(), 1)
        self.control.close()
        self.control = ControlStore(self.dir)
        self.bus = MessageBus(self.control)
        self.assertEqual(self.bus.pending(), ["m-retry"])
        current = self.bus.lease(self.bus.pending()[0], "replacement")
        with self.assertRaises(StaleFenceError):
            self.bus.acknowledge("m-retry", old, "scheduled", "first-worker")
        self.bus.acknowledge("m-retry", current, "scheduled", "replacement")
        self.assertEqual(self.bus.pending(), [])

    def test_legacy_malformed_outbox_is_quarantined_without_blocking_delivery(self):
        from scisaurus.core.schema import canonical_bytes, now_iso
        from scisaurus.core.store import ArtifactStore

        malformed = envelope("m-bad", "bad-effect")
        malformed["type"] = "invalid"
        with self.control.tx() as conn:
            conn.execute(
                "INSERT INTO outbox(effect_key, message_id, envelope_json, created_at) VALUES (?, ?, ?, ?)",
                ("bad-effect", "m-bad", canonical_bytes(malformed).decode(), now_iso()),
            )
        self.control.close()
        self.control = ControlStore(self.dir)
        self.bus = MessageBus(self.control)
        ArtifactStore(self.control).publish_artifact(
            logical_id="kb/notes/valid", artifact_type="note", author="research.chief",
            body=b"body", messages=[envelope("m-valid", "valid-effect")],
        )
        self.assertEqual(self.bus.recover_outbox(), 1)
        self.assertEqual(self.bus.pending(), ["m-valid"])
        rejected = self.bus.quarantined()
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["message_id"], "m-bad")
        self.assertIn("unknown message type", rejected[0]["reason"])
        self.assertEqual(self.control._conn.execute(
            "SELECT dispatched FROM outbox WHERE message_id='m-bad'"
        ).fetchone()[0], 0)
        self.assertEqual(self.bus.recover_outbox(), 0)
        events = [e for e in self.control.replay() if e["event_type"] == "outbox.quarantined"]
        self.assertEqual(len(events), 1)
        self.assertEqual(self.control.verify_chain(), (True, "ok"))

    def test_message_schema_rejects_malformed_field_types(self):
        from scisaurus.core.errors import ValidationError
        from scisaurus.core.schema import validate_message

        for malformed in (None, [], {**envelope("m", "e"), "type": []},
                          {**envelope("m", "e"), "from": "worker"},
                          {**envelope("m", "e"), "refs": [None]}):
            with self.subTest(envelope=malformed), self.assertRaises(ValidationError):
                validate_message(malformed)


if __name__ == "__main__":
    unittest.main()