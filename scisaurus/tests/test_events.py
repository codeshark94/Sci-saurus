"""T13: event alteration/truncation detected against a trusted saved head."""

import unittest

from scisaurus.core.events import ControlStore


class TestEventChain(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.dir = tempfile.mkdtemp(prefix="scisaurus-ev-")
        self.control = ControlStore(self.dir)

    def tearDown(self):
        self.control.close()

    def _seed(self):
        for i in range(3):
            self.control.append(
                actor="test",
                event_type="task.proposed",
                payload={"n": i},
            )
        return self.control.trusted_head()

    def test_chain_valid_when_untouched(self):
        head = self._seed()
        ok, reason = self.control.verify_chain(expected_head=head)
        self.assertTrue(ok, reason)

    def test_t13_alteration_detected(self):
        head = self._seed()
        with self.control.tx() as conn:
            conn.execute(
                "UPDATE events SET payload_json = '{\"tampered\": true}' WHERE seq = 2"
            )
        ok, reason = self.control.verify_chain(expected_head=head)
        self.assertFalse(ok)
        self.assertIn("hash mismatch", reason)

    def test_t13_truncation_detected_against_trusted_head(self):
        head = self._seed()
        with self.control.tx() as conn:
            conn.execute("DELETE FROM events WHERE seq = 3")
        ok, reason = self.control.verify_chain(expected_head=head)
        self.assertFalse(ok)
        self.assertIn("trusted head mismatch", reason)


if __name__ == "__main__":
    unittest.main()