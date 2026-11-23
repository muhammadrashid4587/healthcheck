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
        self.assertIsNone(History().uptime_pct_window("api", 3600))

    def test_last_error_reports_most_recent_failed_check(self):
        hist = History()
        self.assertIsNone(hist.last_error("api"))
        hist.record(CheckResult(name="api", ok=False, latency_ms=1.0,
                                checked_at=1_700_000_000.0,
                                error="connection refused"))
        hist.record(CheckResult(name="api", ok=False, latency_ms=1.0,
                                checked_at=1_700_000_001.0,
                                error="HTTP 500"))
        hist.record(CheckResult(name="api", ok=True, latency_ms=1.0,
                                checked_at=1_700_000_002.0))
        # The error stays after the recovery: it answers why the
        # endpoint last failed, not why it is down now.
        self.assertEqual(hist.last_error("api"), "HTTP 500")
        # An endpoint that never failed has no last error.
        hist.record(_result("clean", True))
        self.assertIsNone(hist.last_error("clean"))

    def test_last_error_ignores_skips_and_maintenance(self):
        hist = History()
        hist.record(CheckResult(name="api", ok=False, latency_ms=0.0,
                                checked_at=1_700_000_000.0,
                                skipped=True, depends_on="db",
                                error="skipped: db is down"))
        hist.record(CheckResult(name="api", ok=False, latency_ms=0.0,
                                checked_at=1_700_000_001.0,
                                maintenance=True,
                                error="maintenance window: sat 02:00-04:00"))
        self.assertIsNone(hist.last_error("api"))

    def test_uptime_window_ignores_old_checks(self):
        # now = 100000; 24h window starts at 13600.
        hist = History()
        hist.record(_result("api", True, at=1000.0))    # too old
        hist.record(_result("api", False, at=20000.0))  # in window
        hist.record(_result("api", True, at=90000.0))   # in window
        self.assertEqual(hist.uptime_pct("api"), 66.7)   # all 3
        self.assertEqual(hist.uptime_pct_window("api", 24 * 3600, now=100000.0), 50.0)

    def test_uptime_window_none_when_no_checks_in_window(self):
        hist = History()
        hist.record(_result("api", True, at=1000.0))
        self.assertIsNone(hist.uptime_pct_window("api", 3600, now=100000.0))

    def test_uptime_window_boundary_is_inclusive(self):
        hist = History()
        hist.record(_result("api", True, at=1000.0))
        self.assertEqual(hist.uptime_pct_window("api", 1000, now=2000.0), 100.0)

    def test_uptime_all_up_and_all_down(self):
        hist = History()
        hist.record(_result("a", True))
        hist.record(_result("a", True))
        self.assertEqual(hist.uptime_pct("a"), 100.0)
        hist.record(_result("b", False))
        self.assertEqual(hist.uptime_pct("b"), 0.0)

    def test_latency_percentiles_on_known_history(self):
        hist = History()
        for latency in (10.0, 20.0, 30.0, 40.0, 50.0,
                        60.0, 70.0, 80.0, 90.0, 100.0):
            hist.record(_result("api", True, latency=latency))
        # Nearest-rank: p50 is the 5th of 10, p95 the 10th, p99 the 10th.
        self.assertEqual(hist.latency_percentiles("api", now=1_700_000_000.0),
                         {"p50": 50.0, "p95": 100.0, "p99": 100.0})

    def test_latency_percentiles_single_check(self):
        hist = History()
        hist.record(_result("api", True, latency=42.0))
        self.assertEqual(hist.latency_percentiles("api", now=1_700_000_000.0),
                         {"p50": 42.0, "p95": 42.0, "p99": 42.0})

    def test_latency_percentiles_window_ignores_old_checks(self):
        # now = 100000; 24h window starts at 13600.
        hist = History()
        hist.record(_result("api", True, latency=5.0, at=1000.0))    # too old
        hist.record(_result("api", True, latency=10.0, at=20000.0))  # in window
        hist.record(_result("api", True, latency=20.0, at=90000.0))  # in window
        self.assertEqual(
            hist.latency_percentiles("api", now=100000.0),
            {"p50": 10.0, "p95": 20.0, "p99": 20.0})

    def test_latency_percentiles_none_when_no_checks_in_window(self):
        hist = History()
        hist.record(_result("api", True, latency=10.0, at=1000.0))
        self.assertIsNone(hist.latency_percentiles("api", now=100000.0))

    def test_latency_percentiles_none_without_latencies(self):
        self.assertIsNone(History().latency_percentiles("api"))
        hist = History()
        hist.record(CheckResult(
            name="api", ok=False, latency_ms=0.0, checked_at=1_700_000_000.0,
            skipped=True, depends_on="db"))
        self.assertIsNone(hist.latency_percentiles("api", now=1_700_000_000.0))

    def test_latency_percentiles_exclude_skipped_and_maintenance(self):
        hist = History()
        hist.record(_result("api", True, latency=10.0))
        hist.record(_result("api", True, latency=20.0))
        hist.record(CheckResult(
            name="api", ok=False, latency_ms=999.0, checked_at=1_700_000_000.0,
            skipped=True, depends_on="db"))
        hist.record(CheckResult(
            name="api", ok=False, latency_ms=888.0, checked_at=1_700_000_000.0,
            maintenance=True))
        # Only the two real checks count: p50 is the 1st of 2, p95/p99 the 2nd.
        self.assertEqual(hist.latency_percentiles("api", now=1_700_000_000.0),
                         {"p50": 10.0, "p95": 20.0, "p99": 20.0})

    def test_latency_percentiles_include_failed_checks(self):
        hist = History()
        hist.record(_result("api", True, latency=10.0))
        hist.record(_result("api", False, latency=90.0))
        self.assertEqual(hist.latency_percentiles("api", now=1_700_000_000.0),
                         {"p50": 10.0, "p95": 90.0, "p99": 90.0})

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
