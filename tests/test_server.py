"""Tests for the status dashboard."""

import html
import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from healthcheck.checker import CheckResult
from healthcheck.server import (latency_sparkline, make_handler,
                                render_dashboard,
                                render_recent_incidents, status_snapshot)
from healthcheck.store import History


class StatusApiTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.history.record(_result("web", True))
        self.history.record(_result("db", False))
        self.endpoints = [
            {"name": "web", "type": "http", "url": "http://example.com"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
        ]

    def _by_name(self, snapshot):
        return {ep["name"]: ep for ep in snapshot["endpoints"]}

    def test_snapshot_reports_status_uptime_and_latency(self):
        snap = status_snapshot(self.history, self.endpoints)
        self.assertIn("generated_at", snap)
        by_name = self._by_name(snap)
        self.assertEqual(by_name["web"]["status"], "UP")
        self.assertEqual(by_name["web"]["uptime_pct"], 100.0)
        self.assertEqual(by_name["web"]["latency_ms"], 12.5)
        self.assertEqual(by_name["web"]["last_checked_at"], 1_700_000_000.0)
        self.assertEqual(by_name["db"]["status"], "DOWN")
        self.assertEqual(by_name["db"]["uptime_pct"], 0.0)
        self.assertIsNone(by_name["web"]["depends_on"])
        self.assertEqual(by_name["web"]["maintenance_windows"], [])

    def test_snapshot_unknown_endpoint_has_null_fields(self):
        snap = status_snapshot(self.history, self.endpoints + [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1", "port": 1}])
        fresh = self._by_name(snap)["fresh"]
        self.assertEqual(fresh["status"], "UNKNOWN")
        self.assertIsNone(fresh["uptime_pct"])
        self.assertIsNone(fresh["uptime_24h_pct"])
        self.assertIsNone(fresh["uptime_7d_pct"])
        self.assertIsNone(fresh["latency_ms"])
        self.assertIsNone(fresh["last_checked_at"])

    def test_snapshot_skipped_and_maintenance_statuses(self):
        hist = History()
        hist.record(CheckResult(
            name="app", ok=False, latency_ms=0.0, checked_at=1_700_000_000.0,
            skipped=True, depends_on="db"))
        hist.record(CheckResult(
            name="svc", ok=False, latency_ms=0.0, checked_at=1_700_000_000.0,
            maintenance=True))
        snap = status_snapshot(hist, [
            {"name": "app", "type": "http", "url": "http://x",
             "depends_on": "db"},
            {"name": "svc", "type": "tcp", "host": "127.0.0.1", "port": 1,
             "maintenance_windows": [{"days": "sat", "start": "02:00",
                                      "end": "04:00"}]},
        ])
        by_name = self._by_name(snap)
        self.assertEqual(by_name["app"]["status"], "SKIPPED")
        self.assertEqual(by_name["app"]["depends_on"], "db")
        self.assertIsNone(by_name["app"]["latency_ms"])
        self.assertIsNone(by_name["app"]["uptime_pct"])
        self.assertEqual(by_name["svc"]["status"], "MAINTENANCE")
        self.assertEqual(by_name["svc"]["maintenance_windows"],
                         [{"start": "02:00", "end": "04:00", "days": "sat"}])

    def test_snapshot_reports_last_error(self):
        hist = History()
        hist.record(CheckResult(
            name="db", ok=False, latency_ms=1.0, checked_at=1_700_000_000.0,
            error="connection refused"))
        hist.record(_result("web", True))
        snap = status_snapshot(hist, self.endpoints)
        by_name = self._by_name(snap)
        self.assertEqual(by_name["db"]["last_error"], "connection refused")
        self.assertIsNone(by_name["web"]["last_error"])
        # The error stays after the endpoint recovers.
        hist.record(_result("db", True, at=1_700_000_001.0))
        snap = status_snapshot(hist, self.endpoints)
        self.assertEqual(self._by_name(snap)["db"]["last_error"],
                         "connection refused")

    def test_snapshot_last_error_ignores_skips_and_maintenance(self):
        hist = History()
        hist.record(CheckResult(
            name="app", ok=False, latency_ms=0.0, checked_at=1_700_000_000.0,
            skipped=True, depends_on="db", error="skipped: db is down"))
        hist.record(CheckResult(
            name="svc", ok=False, latency_ms=0.0, checked_at=1_700_000_000.0,
            maintenance=True,
            error="maintenance window: sat 02:00-04:00"))
        snap = status_snapshot(hist, [
            {"name": "app", "type": "http", "url": "http://x",
             "depends_on": "db"},
            {"name": "svc", "type": "tcp", "host": "127.0.0.1", "port": 1},
        ])
        by_name = self._by_name(snap)
        self.assertIsNone(by_name["app"]["last_error"])
        self.assertIsNone(by_name["svc"]["last_error"])

    def test_dashboard_shows_last_error_escaped(self):
        hist = History()
        hist.record(CheckResult(
            name="db", ok=False, latency_ms=1.0, checked_at=1_700_000_000.0,
            error="<script>alert(1)</script> refused"))
        hist.record(_result("web", True))
        page = render_dashboard(hist, self.endpoints)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; refused", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("<th>Last error</th>", page)
        # An endpoint that never failed shows "-" in the column.
        self.assertIn("<td>-</td>", page)

    def test_snapshot_reports_latency_percentiles(self):
        hist = History()
        for latency in (10.0, 20.0, 30.0, 40.0, 50.0,
                        60.0, 70.0, 80.0, 90.0, 100.0):
            hist.record(_result("web", True, latency=latency))
        snap = status_snapshot(hist, self.endpoints, now=1_700_000_000.0)
        by_name = self._by_name(snap)
        self.assertEqual(by_name["web"]["latency_percentiles"],
                         {"p50": 50.0, "p95": 100.0, "p99": 100.0})
        self.assertIsNone(by_name["db"]["latency_percentiles"])

    def test_snapshot_unconfirmed_failure_still_up(self):
        hist = History()
        hist.record(CheckResult(
            name="web", ok=False, latency_ms=3.0, checked_at=1_700_000_000.0,
            unconfirmed=True))
        snap = status_snapshot(hist, self.endpoints[:1])
        self.assertEqual(snap["endpoints"][0]["status"], "UP")

    def test_snapshot_reports_recent_incidents(self):
        hist = History()
        hist.record(_result("db", True, at=1_700_000_000.0))
        hist.record(_result("db", False, at=1_700_000_060.0))
        hist.record(_result("db", False, at=1_700_000_120.0))
        hist.record(_result("db", True, at=1_700_000_180.0))
        snap = status_snapshot(hist, self.endpoints)
        by_name = self._by_name(snap)
        self.assertEqual(by_name["db"]["recent_incidents"], [{
            "start": 1_700_000_060.0, "end": 1_700_000_180.0,
            "checks": 2, "ongoing": False, "duration_s": 120.0}])
        # An endpoint that never failed has no incidents.
        self.assertEqual(by_name["web"]["recent_incidents"], [])

    def test_snapshot_ongoing_incident_duration_uses_now(self):
        hist = History()
        hist.record(_result("db", False, at=1_700_000_000.0))
        snap = status_snapshot(hist, self.endpoints, now=1_700_000_300.0)
        incident = self._by_name(snap)["db"]["recent_incidents"][0]
        self.assertEqual(incident["start"], 1_700_000_000.0)
        self.assertIsNone(incident["end"])
        self.assertTrue(incident["ongoing"])
        self.assertEqual(incident["duration_s"], 300.0)

    def test_snapshot_incidents_ignore_skips_and_unconfirmed(self):
        hist = History()
        hist.record(CheckResult(
            name="db", ok=False, latency_ms=1.0, checked_at=1_700_000_000.0,
            unconfirmed=True))
        hist.record(CheckResult(
            name="db", ok=False, latency_ms=0.0, checked_at=1_700_000_060.0,
            skipped=True, depends_on="web"))
        hist.record(_result("db", True, at=1_700_000_120.0))
        snap = status_snapshot(hist, self.endpoints)
        self.assertEqual(self._by_name(snap)["db"]["recent_incidents"], [])

    def test_snapshot_unknown_endpoint_has_no_incidents(self):
        snap = status_snapshot(self.history, self.endpoints + [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1", "port": 1}])
        self.assertEqual(self._by_name(snap)["fresh"]["recent_incidents"],
                         [])

    def test_snapshot_is_json_serializable(self):
        snap = status_snapshot(self.history, self.endpoints)
        parsed = json.loads(json.dumps(snap))
        self.assertEqual(parsed, snap)

    def test_api_status_served_over_http(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.history, self.endpoints))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status") as resp:
                body = resp.read().decode("utf-8")
                content_type = resp.headers.get("Content-Type")
                status = resp.status
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        snap = json.loads(body)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["web"]["status"], "UP")
        self.assertEqual(by_name["db"]["status"], "DOWN")


def _result(name, ok, latency=12.5, at=1_700_000_000.0):
    return CheckResult(name=name, ok=ok, latency_ms=latency, checked_at=at)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.history.record(_result("web", True))
        self.history.record(_result("db", False))
        self.endpoints = [
            {"name": "web", "type": "http", "url": "http://example.com"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
        ]

    def test_html_contains_each_endpoint_name_and_status(self):
        page = render_dashboard(self.history, self.endpoints)
        self.assertIn("web", page)
        self.assertIn("db", page)
        self.assertIn("UP", page)
        self.assertIn("DOWN", page)
        self.assertIn("100.0%", page)  # web: 1/1 up
        self.assertIn("0.0%", page)    # db: 0/1 up

    def test_dashboard_shows_windowed_uptime_columns(self):
        # Checks recorded just now: both in the 24h and 7d windows.
        import time
        now = time.time()
        hist = History()
        hist.record(_result("web", True, at=now - 60))
        hist.record(_result("web", False, at=now - 30))
        page = render_dashboard(hist, [{"name": "web", "type": "http", "url": "http://example.com"}])
        self.assertIn("Uptime 24h", page)
        self.assertIn("Uptime 7d", page)
        self.assertIn("50.0%", page)  # 1/2 up in both windows and overall

    def test_dashboard_windowed_uptime_dash_when_no_recent_checks(self):
        # Checks are from 2023, long outside both windows: dashes.
        page = render_dashboard(self.history, self.endpoints)
        self.assertIn("Uptime 24h", page)
        # web row: all-history 100.0% plus two "-" window cells
        self.assertIn("<td>100.0%</td><td>-</td><td>-</td>", page)

    def test_html_escapes_endpoint_names(self):
        self.history.record(_result("<b>x</b>", True))
        page = render_dashboard(self.history, [{"name": "<b>x</b>", "type": "http", "url": "http://x"}])
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)

    def test_served_dashboard_over_http(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.history, self.endpoints))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                body = resp.read().decode("utf-8")
                status = resp.status
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status, 200)
        self.assertIn("web", body)
        self.assertIn("db", body)
        self.assertIn("UP", body)
        self.assertIn("DOWN", body)

    def test_dashboard_shows_latency_percentiles_column(self):
        for latency in (10.0, 20.0, 30.0, 40.0, 50.0,
                        60.0, 70.0, 80.0, 90.0, 100.0):
            self.history.record(_result("web", True, latency=latency))
        page = render_dashboard(self.history, self.endpoints, now=1_700_000_000.0)
        self.assertIn("Latency p50 / p95 / p99", page)
        self.assertIn("50.0 / 100.0 / 100.0 ms", page)

    def test_dashboard_percentiles_dash_without_data(self):
        page = render_dashboard(self.history, self.endpoints, now=1_700_000_000.0)
        # web has one check, so it shows percentiles; a never-checked
        # endpoint shows a dash instead.
        fresh = render_dashboard(self.history, self.endpoints + [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1", "port": 1}],
            now=1_700_000_000.0)
        self.assertIn("<td>-</td>", fresh)

    def test_dashboard_contains_latency_sparkline(self):
        for latency in (10.0, 20.0, 15.0):
            self.history.record(_result("web", True, latency=latency))
        page = render_dashboard(self.history, self.endpoints)
        self.assertIn("Latency history", page)
        self.assertIn("<svg", page)
        self.assertIn("<polyline", page)

    def test_dashboard_shows_recent_incidents(self):
        hist = History()
        hist.record(_result("db", True, at=1_700_000_000.0))
        hist.record(_result("db", False, at=1_700_000_060.0))
        hist.record(_result("db", False, at=1_700_000_120.0))
        hist.record(_result("db", True, at=1_700_000_180.0))
        hist.record(_result("web", True))
        page = render_dashboard(hist, self.endpoints)
        self.assertIn("<h2>Recent incidents</h2>", page)
        self.assertIn("<h3>db</h3>", page)
        # Only the endpoint with an outage gets a subsection.
        self.assertNotIn("<h3>web</h3>", page)
        start = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(1_700_000_060.0))
        end = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(1_700_000_180.0))
        self.assertIn(start, page)  # incident start
        self.assertIn(end, page)    # recovery
        self.assertIn("(2m 0s, 2 failed checks)", page)

    def test_dashboard_shows_duration_histogram_per_endpoint(self):
        # db: a 30 s incident and a 2 min incident; web: no
        # incidents, so no subsection and no histogram for it.
        hist = History()
        hist.record(_result("db", True, at=1_700_000_000.0))
        hist.record(_result("db", False, at=1_700_000_060.0))
        hist.record(_result("db", True, at=1_700_000_090.0))
        hist.record(_result("db", False, at=1_700_000_200.0))
        hist.record(_result("db", True, at=1_700_000_320.0))
        hist.record(_result("web", True))
        page = render_dashboard(hist, self.endpoints)
        # The histogram on the page is the helper's output for the
        # same data, so the dashboard and the text report agree
        # (labels are HTML-escaped on the page, as in the markup).
        buckets = ", ".join(
            f"{html.escape(b['label'])}: {b['count']}"
            for b in hist.incident_duration_histogram("db", limit=5))
        self.assertIn(f"Duration histogram: {buckets}", page)
        self.assertIn("&lt; 1m: 1", page)
        self.assertIn("1m – 5m: 1", page)
        # One histogram only: the endpoint with no incidents gets
        # neither a subsection nor a histogram.
        self.assertEqual(page.count("Duration histogram:"), 1)

    def test_dashboard_histogram_covers_only_listed_incidents(self):
        # Six 2-minute incidents, but the section lists at most 5 —
        # the histogram must count the same 5, not all 6.
        hist = History()
        base = 1_700_000_000.0
        for i in range(6):
            hist.record(_result("db", False, at=base + i * 1000))
            hist.record(_result("db", True, at=base + i * 1000 + 120))
        hist.record(_result("web", True))
        page = render_dashboard(hist, self.endpoints)
        self.assertEqual(page.count("<li>"), 5)
        self.assertIn("1m – 5m: 5", page)
        histogram = hist.incident_duration_histogram("db", limit=5)
        self.assertEqual(sum(b["count"] for b in histogram), 5)

    def test_dashboard_no_histogram_when_quiet(self):
        hist = History()
        hist.record(_result("web", True))
        hist.record(_result("db", True))
        page = render_dashboard(hist, self.endpoints)
        self.assertNotIn("Duration histogram:", page)

    def test_dashboard_ongoing_incident_shows_no_end(self):
        hist = History()
        hist.record(_result("db", False, at=1_700_000_000.0))
        hist.record(_result("web", True))
        page = render_dashboard(hist, self.endpoints, now=1_700_000_300.0)
        self.assertIn("<h2>Recent incidents</h2>", page)
        start = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(1_700_000_000.0))
        self.assertIn(f"{start} – ongoing", page)
        self.assertIn("(5m 0s, 1 failed check)", page)

    def test_dashboard_no_incidents_section_when_quiet(self):
        # A monitor with no outages renders no incidents section at
        # all — the dashboard is unchanged for a quiet monitor.
        hist = History()
        hist.record(_result("web", True))
        hist.record(_result("db", True))
        page = render_dashboard(hist, self.endpoints)
        self.assertNotIn("Recent incidents", page)
        self.assertEqual(render_recent_incidents(hist, self.endpoints), "")

    def test_dashboard_incidents_newest_first_and_capped_at_five(self):
        hist = History()
        base = 1_700_000_000.0
        for i in range(6):
            hist.record(_result("db", False, at=base + i * 1000))
            hist.record(_result("db", False, at=base + i * 1000 + 60))
            hist.record(_result("db", True, at=base + i * 1000 + 120))
        hist.record(_result("web", True))
        page = render_dashboard(hist, self.endpoints)
        # At most 5 incidents, newest first: the newest start is in
        # the page, the oldest incident is not.
        self.assertEqual(page.count("<li>"), 5)
        newest = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(base + 5000))
        oldest = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(base))
        self.assertIn(newest, page)
        self.assertNotIn(oldest, page)

    def test_served_dashboard_shows_recent_incidents_over_http(self):
        hist = History()
        hist.record(_result("db", False, at=1_700_000_000.0))
        hist.record(_result("db", False, at=1_700_000_060.0))
        hist.record(_result("db", True, at=1_700_000_120.0))
        hist.record(_result("web", True))
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(hist, self.endpoints))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                body = resp.read().decode("utf-8")
                status = resp.status
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status, 200)
        self.assertIn("<h2>Recent incidents</h2>", body)
        self.assertIn("<h3>db</h3>", body)
        self.assertIn("2 failed checks", body)

    def test_unknown_path_404(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.history, self.endpoints))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/nope")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(ctx.exception.code, 404)


class SparklineTests(unittest.TestCase):
    def test_too_little_data_is_a_dash(self):
        history = History()
        self.assertEqual(latency_sparkline(history, "web"), "-")
        history.record(_result("web", True, latency=12.5))
        self.assertEqual(latency_sparkline(history, "web"), "-")

    def test_flat_history_is_a_flat_line(self):
        history = History()
        for _ in range(3):
            history.record(_result("web", True, latency=10.0))
        svg = latency_sparkline(history, "web")
        self.assertIn("<polyline", svg)
        ys = [float(p.split(",")[1]) for p in
              svg.split('points="')[1].split('"')[0].split()]
        self.assertEqual(len(set(ys)), 1)  # all points at the same height

    def test_points_stay_inside_the_canvas(self):
        history = History()
        for latency in (1.0, 50.0, 5.0, 100.0, 2.0):
            history.record(_result("web", True, latency=latency))
        svg = latency_sparkline(history, "web", width=100, height=24)
        points = svg.split('points="')[1].split('"')[0].split()
        self.assertEqual(len(points), 5)
        for point in points:
            x, y = (float(v) for v in point.split(","))
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x, 100)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(y, 24)

    def test_uses_only_the_most_recent_20(self):
        history = History()
        for i in range(25):
            history.record(_result("web", True, latency=float(i)))
        svg = latency_sparkline(history, "web")
        points = svg.split('points="')[1].split('"')[0].split()
        self.assertEqual(len(points), 20)


if __name__ == "__main__":
    unittest.main()
