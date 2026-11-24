"""Tests for GET /api/sla: the monthly SLA report over HTTP."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from healthcheck.checker import CheckResult
from healthcheck.server import make_handler, sla_snapshot
from healthcheck.sla import render_sla_report_json, sla_report
from healthcheck.store import History


def _at(year, month, day, hour=12, minute=0):
    """Epoch seconds for a local-time date, so the tests work in any
    timezone: month bounds are computed in local time too."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def _result(name, ok, at, latency=10.0, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at, **kwargs)


class SlaSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        # api: 9 up, 1 down, then a recovery up — 90.0% in Nov 2026,
        # against a 99.9 target, so it breaches by 9.9 points.
        for minute in range(8):
            self.history.record(
                _result("api", True, _at(2026, 11, 5, 12, minute)))
        self.history.record(_result("api", False, _at(2026, 11, 5, 12, 8)))
        self.history.record(_result("api", True, _at(2026, 11, 5, 12, 9)))
        self.endpoints = [
            {"name": "api", "type": "http", "url": "http://example.com",
             "sla_target": 99.9},
            {"name": "quiet", "type": "tcp", "host": "127.0.0.1",
             "port": 5432},
        ]

    def test_snapshot_matches_the_json_export(self):
        snap = sla_snapshot(self.history, self.endpoints, 2026, 11)
        exported = json.loads(
            render_sla_report_json(
                sla_report(self.history, self.endpoints, 2026, 11),
                2026, 11))
        self.assertEqual(snap, exported)

    def test_snapshot_reports_the_month_detail(self):
        snap = sla_snapshot(self.history, self.endpoints, 2026, 11)
        self.assertEqual(snap["month"], "2026-11")
        self.assertEqual(snap["breaches"], ["api"])
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        api = by_name["api"]
        self.assertEqual(api["uptime_pct"], 90.0)
        self.assertEqual(api["checks"], 10)
        self.assertEqual(api["target"], 99.9)
        self.assertIs(api["met"], False)
        self.assertEqual(api["shortfall_pct"], 9.9)
        self.assertEqual(api["incidents"], 1)
        self.assertEqual(api["failed_checks"], 1)
        # One minute from the down check to the recovery check.
        self.assertEqual(api["downtime_seconds"], 60.0)
        self.assertEqual(api["latency"],
                         {"p50": 10.0, "p95": 10.0, "p99": 10.0})
        quiet = by_name["quiet"]
        self.assertIsNone(quiet["uptime_pct"])
        self.assertEqual(quiet["checks"], 0)
        self.assertIsNone(quiet["met"])
        self.assertIsNone(quiet["latency"])

    def test_snapshot_defaults_to_the_current_month(self):
        now = time.localtime()
        snap = sla_snapshot(self.history, self.endpoints)
        self.assertEqual(
            snap["month"], f"{now.tm_year:04d}-{now.tm_mon:02d}")


class SlaApiTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        for minute in range(8):
            self.history.record(
                _result("api", True, _at(2026, 11, 5, 12, minute)))
        self.history.record(_result("api", False, _at(2026, 11, 5, 12, 8)))
        self.history.record(_result("api", True, _at(2026, 11, 5, 12, 9)))
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

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}") as resp:
            return (resp.status,
                    resp.headers.get("Content-Type"),
                    resp.read().decode("utf-8"))

    def test_sla_served_over_http_matches_the_snapshot(self):
        status, content_type, body = self._get("/api/sla?month=2026-11")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertEqual(
            payload,
            sla_snapshot(self.history, self.endpoints, 2026, 11))
        self.assertEqual(payload["month"], "2026-11")
        self.assertEqual(payload["breaches"], ["api"])
        self.assertEqual(payload["endpoints"][0]["uptime_pct"], 90.0)

    def test_sla_without_month_serves_the_current_month(self):
        status, _, body = self._get("/api/sla")
        now = time.localtime()
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            payload["month"], f"{now.tm_year:04d}-{now.tm_mon:02d}")

    def test_bad_month_is_a_400_with_a_json_error(self):
        for bad in ("not-a-month", "2026-13", "2026-00", "2026"):
            with self.subTest(month=bad):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/api/sla"
                        f"?month={bad}")
                self.assertEqual(ctx.exception.code, 400)
                self.assertIn("application/json",
                              ctx.exception.headers.get("Content-Type"))
                error = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertIn("error", error)
                self.assertTrue(error["error"])


if __name__ == "__main__":
    unittest.main()
