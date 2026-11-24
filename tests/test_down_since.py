"""Tests for "down since": when the current outage started, and how
long it has lasted, on the dashboard, in GET /api/status, and in
GET /metrics."""

import unittest

from healthcheck.checker import CheckResult
from healthcheck.server import (format_duration, render_dashboard,
                                render_metrics, status_snapshot)
from healthcheck.store import History


def _result(name, ok, at, unconfirmed=False, skipped=False,
            maintenance=False):
    return CheckResult(name=name, ok=ok, latency_ms=1.0, checked_at=at,
                       unconfirmed=unconfirmed, skipped=skipped,
                       maintenance=maintenance,
                       error=None if ok else "connection refused")


class DownSinceTests(unittest.TestCase):
    def test_none_when_never_checked(self):
        self.assertIsNone(History().down_since("api"))

    def test_none_when_up(self):
        hist = History()
        hist.record(_result("api", False, at=1000.0))
        hist.record(_result("api", True, at=2000.0))
        self.assertIsNone(hist.down_since("api"))

    def test_first_failure_in_the_current_run(self):
        hist = History()
        hist.record(_result("api", True, at=1000.0))
        hist.record(_result("api", False, at=2000.0))
        hist.record(_result("api", False, at=3000.0))
        hist.record(_result("api", False, at=4000.0))
        self.assertEqual(hist.down_since("api"), 2000.0)

    def test_earlier_outage_ignored_after_recovery(self):
        hist = History()
        hist.record(_result("api", False, at=1000.0))
        hist.record(_result("api", False, at=2000.0))
        hist.record(_result("api", True, at=3000.0))
        hist.record(_result("api", False, at=5000.0))
        self.assertEqual(hist.down_since("api"), 5000.0)

    def test_unconfirmed_failures_start_the_clock(self):
        # The outage started when checks first failed, not when the
        # flap threshold confirmed it — so an unconfirmed failure
        # counts as the run's start.
        hist = History()
        hist.record(_result("api", True, at=1000.0))
        hist.record(_result("api", False, at=2000.0, unconfirmed=True))
        hist.record(_result("api", False, at=3000.0, unconfirmed=True))
        hist.record(_result("api", False, at=4000.0))
        self.assertEqual(hist.down_since("api"), 2000.0)

    def test_skip_ends_the_run(self):
        # A skipped round never ran, so it breaks the failure run:
        # an endpoint currently SKIPPED has no outage start.
        hist = History()
        hist.record(_result("api", False, at=1000.0))
        hist.record(_result("api", False, at=2000.0))
        hist.record(_result("api", False, at=3000.0, skipped=True,
                            ))
        self.assertIsNone(hist.down_since("api"))
        # And a failure after the skip starts a fresh run.
        hist.record(_result("api", False, at=4000.0))
        self.assertEqual(hist.down_since("api"), 4000.0)

    def test_maintenance_ends_the_run(self):
        hist = History()
        hist.record(_result("api", False, at=1000.0))
        hist.record(_result("api", False, at=2000.0, maintenance=True))
        self.assertIsNone(hist.down_since("api"))


class FormatDurationTests(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(45), "45s")

    def test_minutes(self):
        self.assertEqual(format_duration(90), "1m 30s")
        self.assertEqual(format_duration(754), "12m 34s")

    def test_hours_and_days(self):
        self.assertEqual(format_duration(2 * 3600 + 5 * 60), "2h 5m")
        self.assertEqual(format_duration(3 * 86400 + 4 * 3600), "3d 4h")

    def test_negative_is_zero(self):
        self.assertEqual(format_duration(-5), "0s")


class DownSinceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.history.record(_result("web", True, at=1_700_000_000.0))
        self.history.record(_result("db", False, at=1_700_000_100.0))
        self.history.record(_result("db", False, at=1_700_000_200.0))
        self.endpoints = [
            {"name": "web", "type": "http", "url": "http://example.com"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1",
             "port": 1},
        ]

    def _by_name(self, snapshot):
        return {ep["name"]: ep for ep in snapshot["endpoints"]}

    def test_snapshot_carries_down_since(self):
        by_name = self._by_name(status_snapshot(self.history,
                                                self.endpoints))
        self.assertEqual(by_name["db"]["down_since"], 1_700_000_100.0)
        self.assertIsNone(by_name["web"]["down_since"])
        self.assertIsNone(by_name["fresh"]["down_since"])

    def test_snapshot_down_since_clears_on_recovery(self):
        self.history.record(_result("db", True, at=1_700_000_300.0))
        by_name = self._by_name(status_snapshot(self.history,
                                                self.endpoints))
        self.assertIsNone(by_name["db"]["down_since"])

    def test_dashboard_shows_outage_duration(self):
        # now = down_since (1_700_000_100) + 754 s: db has been down
        # 12m 34s.
        page = render_dashboard(self.history, self.endpoints,
                                now=1_700_000_100.0 + 754)
        self.assertIn("<th>Down since</th>", page)
        self.assertIn("12m 34s", page)
        # web is up: its row shows "-" in the Down since column.
        web_row = page.split("<tr><td>web</td>")[1].split("</tr>")[0]
        self.assertIn("<td>-</td>", web_row)

    def test_metrics_down_seconds_gauge(self):
        text = render_metrics(self.history, self.endpoints,
                              now=1_700_000_100.0 + 754)
        self.assertIn('healthcheck_down_seconds{name="db"} 754',
                      text)
        self.assertIn('healthcheck_down_seconds{name="web"} 0', text)
        # Never checked: no reading, like healthcheck_up.
        self.assertNotIn('healthcheck_down_seconds{name="fresh"}',
                         text)
