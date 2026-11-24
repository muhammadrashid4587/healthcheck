"""Tests for the uptime summary: windowed downtime, the
`GET /api/uptime` endpoint, and the 30-day / downtime fields in
`GET /api/status`."""

import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from healthcheck.checker import CheckResult
from healthcheck.server import (make_handler, status_snapshot,
                                uptime_snapshot)
from healthcheck.store import History

NOW = 1_700_000_000.0
DAY = 24 * 3600


def _result(name, ok, at, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=10.0,
                       checked_at=at, **kwargs)


def _history():
    """A history with one incident in each window for "api", an
    ongoing incident for "flaky", a clipped incident for
    "clipped", and an outage that started before the 24h window
    and never recovered for "stale"."""
    hist = History()
    # api: too old for every window (40 days ago).
    hist.record(_result("api", True, NOW - 40 * DAY))
    # api: one 60-second outage 20 days ago (30d window only).
    hist.record(_result("api", True, NOW - 20 * DAY))
    hist.record(_result("api", False, NOW - 20 * DAY + 60))
    hist.record(_result("api", True, NOW - 20 * DAY + 120))
    # api: one 60-second outage 3 days ago (7d and 30d windows).
    hist.record(_result("api", True, NOW - 3 * DAY))
    hist.record(_result("api", False, NOW - 3 * DAY + 60))
    hist.record(_result("api", True, NOW - 3 * DAY + 120))
    # api: one 10-minute outage an hour ago (every window).
    hist.record(_result("api", True, NOW - 3600))
    hist.record(_result("api", False, NOW - 1800))
    hist.record(_result("api", True, NOW - 1200))
    # flaky: up 2 hours ago, down 1 hour ago, still down.
    hist.record(_result("flaky", True, NOW - 7200))
    hist.record(_result("flaky", False, NOW - 3600))
    # clipped: down 25 hours ago, recovered 23 hours ago — the
    # outage straddles the start of the 24h window.
    hist.record(_result("clipped", False, NOW - 25 * 3600))
    hist.record(_result("clipped", True, NOW - 23 * 3600))
    # stale: down 2 days ago, never recovered — no counted check
    # falls inside the 24h window, but the endpoint has been down
    # throughout it.
    hist.record(_result("stale", False, NOW - 2 * DAY))
    return hist


class DowntimeWindowTests(unittest.TestCase):
    def test_closed_incident_downtime(self):
        hist = _history()
        # api's last-hour outage ran 10 minutes.
        self.assertEqual(
            hist.downtime_seconds_window("api", DAY, now=NOW), 600.0)
        # Over 7 days the 3-day outage adds another minute.
        self.assertEqual(
            hist.downtime_seconds_window("api", 7 * DAY, now=NOW), 660.0)
        # Over 30 days all three outages add up.
        self.assertEqual(
            hist.downtime_seconds_window("api", 30 * DAY, now=NOW),
            720.0)

    def test_incident_straddling_the_window_start_is_clipped(self):
        hist = _history()
        # Only the hour inside the 24h window counts, not the
        # hour before it.
        self.assertEqual(
            hist.downtime_seconds_window("clipped", DAY, now=NOW),
            3600.0)

    def test_ongoing_incident_accrues_to_now(self):
        hist = _history()
        # flaky went down an hour ago and is still down.
        self.assertEqual(
            hist.downtime_seconds_window("flaky", DAY, now=NOW), 3600.0)

    def test_outage_predating_the_window_accrues_the_whole_window(self):
        hist = _history()
        # stale has been down for two days: the whole 24h window.
        self.assertEqual(
            hist.downtime_seconds_window("stale", DAY, now=NOW),
            float(DAY))

    def test_no_history_or_no_outage_is_zero(self):
        hist = _history()
        self.assertEqual(
            hist.downtime_seconds_window("quiet", DAY, now=NOW), 0.0)
        self.assertEqual(
            History().downtime_seconds_window("api", DAY, now=NOW), 0.0)

    def test_skips_unconfirmed_and_maintenance_are_not_downtime(self):
        hist = History()
        hist.record(_result("api", False, NOW - 3600, skipped=True,
                            depends_on="db"))
        hist.record(_result("api", False, NOW - 1800, unconfirmed=True))
        hist.record(_result("api", False, NOW - 900, maintenance=True))
        hist.record(_result("api", True, NOW - 100))
        self.assertEqual(
            hist.downtime_seconds_window("api", DAY, now=NOW), 0.0)


class WindowSummaryTests(unittest.TestCase):
    def test_summary_reports_uptime_downtime_and_checks(self):
        hist = _history()
        summary = hist.window_summary("api", DAY, now=NOW)
        self.assertEqual(summary, {"uptime_pct": 66.7,
                                   "downtime_seconds": 600.0,
                                   "checks": 3})
        summary = hist.window_summary("api", 30 * DAY, now=NOW)
        self.assertEqual(summary, {"uptime_pct": 66.7,
                                   "downtime_seconds": 720.0,
                                   "checks": 9})

    def test_summary_with_no_data_in_the_window(self):
        hist = _history()
        summary = hist.window_summary("quiet", DAY, now=NOW)
        self.assertEqual(summary, {"uptime_pct": None,
                                   "downtime_seconds": 0.0, "checks": 0})

    def test_summary_reports_downtime_when_no_check_falls_in_window(self):
        hist = _history()
        # stale has no counted check in the 24h window, but it has
        # been down throughout it: uptime is None, downtime is the
        # whole window, not 0.
        summary = hist.window_summary("stale", DAY, now=NOW)
        self.assertEqual(summary, {"uptime_pct": None,
                                   "downtime_seconds": float(DAY),
                                   "checks": 0})


class UptimeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.history = _history()
        self.endpoints = [
            {"name": "api", "type": "http", "url": "http://example.com",
             "sla_target": 99.9},
            {"name": "flaky", "type": "http",
             "url": "http://example.com/flaky", "sla_target": 99.0},
            {"name": "quiet", "type": "tcp", "host": "127.0.0.1",
             "port": 5432},
        ]

    def test_snapshot_windows_and_sla_comparison(self):
        snap = uptime_snapshot(self.history, self.endpoints, now=NOW)
        self.assertEqual(snap["windows"], ["24h", "7d", "30d"])
        self.assertEqual(snap["generated_at"], NOW)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        api = by_name["api"]
        self.assertEqual(api["sla_target"], 99.9)
        self.assertEqual(api["windows"]["24h"],
                         {"uptime_pct": 66.7, "downtime_seconds": 600.0,
                          "checks": 3})
        self.assertEqual(api["windows"]["7d"],
                         {"uptime_pct": 66.7, "downtime_seconds": 660.0,
                          "checks": 6})
        self.assertEqual(api["windows"]["30d"],
                         {"uptime_pct": 66.7, "downtime_seconds": 720.0,
                          "checks": 9})
        # 66.7% over 30 days against a 99.9 target: breach by 33.2.
        self.assertIs(api["sla_met_30d"], False)
        self.assertEqual(api["sla_shortfall_30d_pct"], 33.2)
        flaky = by_name["flaky"]
        self.assertEqual(flaky["windows"]["24h"],
                         {"uptime_pct": 50.0, "downtime_seconds": 3600.0,
                          "checks": 2})
        # 50.0% over 30 days against a 99.0 target: breach by 49.0.
        self.assertIs(flaky["sla_met_30d"], False)
        self.assertEqual(flaky["sla_shortfall_30d_pct"], 49.0)

    def test_snapshot_with_no_data_and_no_target(self):
        snap = uptime_snapshot(self.history, self.endpoints, now=NOW)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        quiet = by_name["quiet"]
        self.assertIsNone(quiet["sla_target"])
        for window in quiet["windows"].values():
            self.assertEqual(window, {"uptime_pct": None,
                                      "downtime_seconds": 0.0,
                                      "checks": 0})
        self.assertIsNone(quiet["sla_met_30d"])
        self.assertIsNone(quiet["sla_shortfall_30d_pct"])

    def test_snapshot_met_target(self):
        hist = History()
        for minute in range(10):
            hist.record(_result("api", True, NOW - 60 * minute))
        snap = uptime_snapshot(
            hist, [{"name": "api", "type": "http",
                    "url": "http://example.com", "sla_target": 99.0}],
            now=NOW)
        api = snap["endpoints"][0]
        self.assertEqual(api["windows"]["30d"]["uptime_pct"], 100.0)
        self.assertIs(api["sla_met_30d"], True)
        self.assertIsNone(api["sla_shortfall_30d_pct"])


class StatusSnapshotUptimeFieldsTests(unittest.TestCase):
    def test_status_carries_30d_uptime_and_downtime(self):
        hist = _history()
        snap = status_snapshot(
            hist, [{"name": "api", "type": "http",
                    "url": "http://example.com"}],
            now=NOW)
        api = snap["endpoints"][0]
        self.assertEqual(api["uptime_30d_pct"], 66.7)
        self.assertEqual(api["downtime_24h_seconds"], 600.0)
        self.assertEqual(api["downtime_7d_seconds"], 660.0)
        self.assertEqual(api["downtime_30d_seconds"], 720.0)


class UptimeApiTests(unittest.TestCase):
    def setUp(self):
        # History relative to the real now, so the live windows
        # have data when the handler computes them.
        now = time.time()
        self.history = History()
        for minute in range(8):
            self.history.record(_result("api", True, now - 60 * minute))
        self.history.record(_result("api", False, now - 30))
        self.history.record(_result("api", True, now - 10))
        self.endpoints = [
            {"name": "api", "type": "http", "url": "http://example.com",
             "sla_target": 99.9},
        ]
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.history, self.endpoints))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_uptime_served_over_http_matches_the_snapshot(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/uptime") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json",
                          resp.headers.get("Content-Type"))
            payload = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(payload["windows"], ["24h", "7d", "30d"])
        self.assertAlmostEqual(payload["generated_at"], time.time(),
                               delta=5)
        api = payload["endpoints"][0]
        self.assertEqual(api["name"], "api")
        self.assertEqual(api["sla_target"], 99.9)
        # 9 up, 1 down in the last hour: 90.0% in every window,
        # 20 seconds of downtime (down at -30, up at -10).
        for window in api["windows"].values():
            self.assertEqual(window["uptime_pct"], 90.0)
            self.assertEqual(window["checks"], 10)
        self.assertEqual(api["windows"]["24h"]["downtime_seconds"], 20.0)
        self.assertEqual(api["windows"]["7d"]["downtime_seconds"], 20.0)
        self.assertEqual(api["windows"]["30d"]["downtime_seconds"], 20.0)
        # 90.0% over 30 days against 99.9: breach by 9.9.
        self.assertIs(api["sla_met_30d"], False)
        self.assertEqual(api["sla_shortfall_30d_pct"], 9.9)


if __name__ == "__main__":
    unittest.main()
