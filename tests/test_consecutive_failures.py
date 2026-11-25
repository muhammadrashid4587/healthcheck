"""Tests for the per-endpoint consecutive-failures counter.

`History.consecutive_failures` is the current run of failed checks
that actually ran, exposed in `GET /api/status` as
`consecutive_failures` and as the dashboard's (and static status
page's) Fail streak column, so an operator can see how close a
still-UP endpoint is to the flap threshold confirming it down.
"""

import unittest

from healthcheck.checker import CheckResult
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.statuspage import render_status_page
from healthcheck.store import History

T0 = 1_700_000_000.0


def _result(name, ok, at, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=12.5, checked_at=at,
                       **kwargs)


class ConsecutiveFailuresTests(unittest.TestCase):
    def test_never_checked_is_zero(self):
        self.assertEqual(History().consecutive_failures("web"), 0)

    def test_counts_current_run_of_failures(self):
        hist = History()
        hist.record(_result("web", True, T0))
        hist.record(_result("web", False, T0 + 30, error="boom"))
        hist.record(_result("web", False, T0 + 60, error="boom"))
        self.assertEqual(hist.consecutive_failures("web"), 2)

    def test_success_resets_the_run(self):
        hist = History()
        hist.record(_result("web", False, T0, error="boom"))
        hist.record(_result("web", False, T0 + 30, error="boom"))
        hist.record(_result("web", True, T0 + 60))
        self.assertEqual(hist.consecutive_failures("web"), 0)

    def test_unconfirmed_failures_count(self):
        # A failure below the flap threshold is a real failure, so
        # it counts toward the streak — the run started when checks
        # first failed, not when the threshold confirmed it.
        hist = History()
        hist.record(_result("web", False, T0, error="boom",
                            unconfirmed=True))
        hist.record(_result("web", False, T0 + 30, error="boom",
                            unconfirmed=True))
        self.assertEqual(hist.consecutive_failures("web"), 2)

    def test_skipped_and_maintenance_end_the_run(self):
        hist = History()
        hist.record(_result("web", False, T0, error="boom"))
        hist.record(_result("web", False, T0 + 30, skipped=True,
                            depends_on="db"))
        self.assertEqual(hist.consecutive_failures("web"), 0)
        hist.record(_result("web", False, T0 + 60, error="boom"))
        hist.record(_result("web", False, T0 + 90, maintenance=True))
        self.assertEqual(hist.consecutive_failures("web"), 0)

    def test_agrees_with_down_since(self):
        # The streak and the outage clock are the same run: a run
        # of N failures means down_since is the Nth-from-last
        # entry's timestamp.
        hist = History()
        hist.record(_result("web", True, T0))
        hist.record(_result("web", False, T0 + 30, error="boom"))
        hist.record(_result("web", False, T0 + 60, error="boom"))
        self.assertEqual(hist.consecutive_failures("web"), 2)
        self.assertEqual(hist.down_since("web"), T0 + 30)


class ConsecutiveFailuresApiTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.history.record(_result("web", True, T0))
        self.history.record(_result("web", False, T0 + 30, error="boom"))
        self.history.record(_result("web", False, T0 + 60, error="boom"))
        self.history.record(_result("db", True, T0))
        self.endpoints = [
            {"name": "web", "type": "http", "url": "http://example.com"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1",
             "port": 1},
        ]

    def _by_name(self, snapshot):
        return {ep["name"]: ep for ep in snapshot["endpoints"]}

    def test_snapshot_carries_consecutive_failures(self):
        snap = status_snapshot(self.history, self.endpoints)
        by_name = self._by_name(snap)
        self.assertEqual(by_name["web"]["consecutive_failures"], 2)
        self.assertEqual(by_name["db"]["consecutive_failures"], 0)
        self.assertEqual(by_name["fresh"]["consecutive_failures"], 0)

    def test_dashboard_shows_fail_streak_column(self):
        page = render_dashboard(self.history, self.endpoints, now=T0 + 60)
        self.assertIn("<th>Fail streak</th>", page)
        # web's row: DOWN with a streak of 2; db's row: UP with 0.
        self.assertIn("<td>2</td>", page)
        self.assertIn("<td>0</td>", page)

    def test_status_page_shows_fail_streak_column(self):
        page = render_status_page(self.history, self.endpoints,
                                  now=T0 + 60)
        self.assertIn("<th>Fail streak</th>", page)
        self.assertIn("<td>2</td>", page)
        self.assertIn("<td>0</td>", page)


if __name__ == "__main__":
    unittest.main()
