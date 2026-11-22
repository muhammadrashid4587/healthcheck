"""Tests for history storage and uptime math."""

import os
import tempfile
import unittest

from healthcheck.checker import CheckResult
from healthcheck.store import History


def _result(name, ok, latency=10.0, at=1_700_000_000.0):
    return CheckResult(name=name, ok=ok, latency_ms=latency, checked_at=at)


class StoreTests(unittest.TestCase):
    def test_uptime_math_on_known_history(self):
        hist = History()
        for ok in (True, True, True, False):
            hist.record(_result("api", ok))
        self.assertEqual(hist.uptime_pct("api"), 75.0)

    def test_uptime_none_without_checks(self):
        self.assertIsNone(History().uptime_pct("api"))

    def test_uptime_all_up_and_all_down(self):
        hist = History()
        hist.record(_result("a", True))
        hist.record(_result("a", True))
        self.assertEqual(hist.uptime_pct("a"), 100.0)
        hist.record(_result("b", False))
        self.assertEqual(hist.uptime_pct("b"), 0.0)

    def test_current_and_recent(self):
        hist = History()
        hist.record(_result("api", True, at=100.0))
        hist.record(_result("api", False, at=200.0))
        self.assertFalse(hist.current("api")["ok"])
        self.assertEqual(hist.current("api")["checked_at"], 200.0)
        self.assertEqual(len(hist.recent("api", n=1)), 1)
        self.assertIsNone(hist.current("missing"))

    def test_maxlen_bounds_history(self):
        hist = History(maxlen=3)
        for _ in range(5):
            hist.record(_result("api", True))
        self.assertEqual(len(hist.recent("api", n=10)), 3)

    def test_save_load_round_trip(self):
        hist = History()
        hist.record(_result("api", True, at=100.0))
        hist.record(_result("api", False, at=200.0))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.json")
            hist.save(path)
            loaded = History.load(path)
        self.assertEqual(loaded.uptime_pct("api"), 50.0)
        self.assertEqual(loaded.current("api")["checked_at"], 200.0)


if __name__ == "__main__":
    unittest.main()
