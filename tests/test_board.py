import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aghub.board import event_payload, event_tip, order_events, verify_event_chain  # noqa: E402


def make_event(event_id, prev=None, **extra):
    event = {"id": event_id, "at": "2026-01-01T00:00:00Z", "type": "test", "prev": prev}
    event.update(extra)
    event["hash"] = hashlib.sha256(event_payload(event)).hexdigest()
    return event


class EventChainTests(unittest.TestCase):
    def test_order_follows_prev_links_not_lexical_ulids(self):
        first = make_event("01M65X92CKK", agent="a")
        second = make_event("01M65X92CK1", prev=first["hash"], agent="a")
        third = make_event("01M65X92CVH", prev=second["hash"], agent="a")

        ordered = order_events([third, first, second])
        self.assertEqual([item["id"] for item in ordered], [first["id"], second["id"], third["id"]])
        self.assertEqual(event_tip([third, first, second]), third["hash"])

        report = verify_event_chain([third, first, second], {})
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["head"], third["hash"])

    def test_tampered_event_is_reported(self):
        first = make_event("01", agent="a")
        second = make_event("02", prev=first["hash"], agent="a")
        second["type"] = "tampered"
        report = verify_event_chain([first, second], {})
        self.assertFalse(report["ok"])
        self.assertIn("hash mismatch", report["errors"][0])

    def test_forked_chain_is_reported(self):
        root = make_event("01", agent="a")
        left = make_event("02", prev=root["hash"], agent="a")
        right = make_event("03", prev=root["hash"], agent="a")
        report = verify_event_chain([root, left, right], {})
        self.assertFalse(report["ok"])
        self.assertTrue(any("broken chain link" in item for item in report["errors"]))


if __name__ == "__main__":
    unittest.main()
