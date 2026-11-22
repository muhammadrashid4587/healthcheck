"""Tests for the status dashboard."""

import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from healthcheck.checker import CheckResult
from healthcheck.server import make_handler, render_dashboard
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


if __name__ == "__main__":
    unittest.main()
