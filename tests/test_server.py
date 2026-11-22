"""Tests for the status dashboard."""

import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from healthcheck.checker import CheckResult
from healthcheck.server import latency_sparkline, make_handler, render_dashboard
from healthcheck.store import History


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

    def test_dashboard_contains_latency_sparkline(self):
        for latency in (10.0, 20.0, 15.0):
            self.history.record(_result("web", True, latency=latency))
        page = render_dashboard(self.history, self.endpoints)
        self.assertIn("Latency history", page)
        self.assertIn("<svg", page)
        self.assertIn("<polyline", page)

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
