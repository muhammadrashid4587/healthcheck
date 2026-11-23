"""Tests for the remote status-page source: scraping another
instance's GET /api/status over HTTP."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from healthcheck.cli import main
from healthcheck.remote import ApiStatusHistory, fetch_remote_status
from healthcheck.statuspage import (
    endpoint_status,
    recent_incidents,
    render_status_page,
)
from healthcheck.store import History

BASE = 1_700_000_000.0

SNAPSHOT = {
    "generated_at": BASE + 200,
    "endpoints": [
        {"name": "web", "status": "UP", "depends_on": None,
         "maintenance_windows": [],
         "uptime_pct": 99.5, "uptime_24h_pct": 100.0,
         "uptime_7d_pct": 99.0, "latency_ms": 12.5,
         "latency_percentiles": {"p50": 11.0, "p95": 20.0, "p99": 30.0},
         "last_checked_at": BASE + 180},
        {"name": "db", "status": "DOWN", "depends_on": None,
         "maintenance_windows": [],
         "uptime_pct": 50.0, "uptime_24h_pct": 50.0,
         "uptime_7d_pct": None, "latency_ms": 3.0,
         "latency_percentiles": None, "last_checked_at": BASE + 190},
        {"name": "api", "status": "SKIPPED", "depends_on": "db",
         "maintenance_windows": [],
         "uptime_pct": None, "uptime_24h_pct": None,
         "uptime_7d_pct": None, "latency_ms": None,
         "latency_percentiles": None, "last_checked_at": BASE + 185},
        {"name": "batch", "status": "MAINTENANCE", "depends_on": None,
         "maintenance_windows": [{"start": "02:00", "end": "04:00",
                                  "days": "sat"}],
         "uptime_pct": 100.0, "uptime_24h_pct": 100.0,
         "uptime_7d_pct": 100.0, "latency_ms": None,
         "latency_percentiles": None, "last_checked_at": BASE + 170},
        {"name": "new", "status": "UNKNOWN", "depends_on": None,
         "maintenance_windows": [],
         "uptime_pct": None, "uptime_24h_pct": None,
         "uptime_7d_pct": None, "latency_ms": None,
         "latency_percentiles": None, "last_checked_at": None},
    ],
}


def _snapshot_endpoints():
    return [{"name": ep["name"]} for ep in SNAPSHOT["endpoints"]]


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.history = ApiStatusHistory(SNAPSHOT)

    def test_names_are_the_snapshot_endpoints(self):
        self.assertEqual(self.history.names(),
                         ["api", "batch", "db", "new", "web"])

    def test_uptime_figures_pass_through(self):
        self.assertEqual(self.history.uptime_pct("web"), 99.5)
        self.assertEqual(
            self.history.uptime_pct_window("web", 24 * 3600), 100.0)
        self.assertEqual(
            self.history.uptime_pct_window("web", 7 * 24 * 3600), 99.0)
        self.assertIsNone(self.history.uptime_pct("api"))
        self.assertIsNone(
            self.history.uptime_pct_window("db", 7 * 24 * 3600))

    def test_latency_percentiles_pass_through(self):
        self.assertEqual(self.history.latency_percentiles("web"),
                         {"p50": 11.0, "p95": 20.0, "p99": 30.0})
        self.assertIsNone(self.history.latency_percentiles("db"))

    def test_statuses_match_the_snapshot(self):
        self.assertEqual(endpoint_status(self.history, "web"), "UP")
        self.assertEqual(endpoint_status(self.history, "db"), "DOWN")
        self.assertEqual(endpoint_status(self.history, "api"), "SKIPPED")
        self.assertEqual(endpoint_status(self.history, "batch"),
                         "MAINTENANCE")
        self.assertEqual(endpoint_status(self.history, "new"), "UNKNOWN")

    def test_no_per_check_history_means_no_sparkline_or_incidents(self):
        self.assertEqual(self.history.recent("web"), [])
        self.assertEqual(self.history.recent("db"), [])
        # Even the endpoint that is DOWN right now gets no incident
        # invented from a single snapshot sample.
        self.assertEqual(recent_incidents(self.history, "db"), [])

    def test_not_an_object_is_rejected(self):
        with self.assertRaises(ValueError):
            ApiStatusHistory(["not", "a", "dict"])

    def test_missing_endpoints_list_is_rejected(self):
        with self.assertRaises(ValueError):
            ApiStatusHistory({"generated_at": BASE})

    def test_endpoint_without_a_name_is_rejected(self):
        with self.assertRaises(ValueError):
            ApiStatusHistory({"endpoints": [{"status": "UP"}]})

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            ApiStatusHistory({"endpoints": [{"name": "web",
                                             "status": "WEIRD"}]})

    def test_non_numeric_uptime_is_rejected(self):
        with self.assertRaises(ValueError):
            ApiStatusHistory({"endpoints": [{"name": "web", "status": "UP",
                                             "uptime_pct": "high"}]})


class RenderTests(unittest.TestCase):
    def test_remote_source_renders_as_a_labelled_section(self):
        page = render_status_page(
            # Local source: empty history, no endpoints — the page
            # still gets its "local" heading once a source exists.
            History(), [], now=BASE + 200,
            extra_sources=[("eu-west", ApiStatusHistory(SNAPSHOT),
                            _snapshot_endpoints())])
        self.assertIn("<h2>local</h2>", page)
        self.assertIn("<h2>eu-west</h2>", page)
        self.assertIn("99.5%", page)   # remote web uptime, as reported
        self.assertIn("50.0%", page)   # remote db uptime, as reported
        self.assertIn("1 endpoint down", page)  # remote db is DOWN
        self.assertIn("SKIPPED", page)
        self.assertIn("MAINTENANCE", page)
        self.assertIn("UNKNOWN", page)
        self.assertIn("No incidents recorded.", page)

    def test_remote_down_endpoint_does_not_fabricate_an_incident(self):
        page = render_status_page(
            History(), [], now=BASE + 200,
            extra_sources=[("eu-west", ApiStatusHistory(SNAPSHOT),
                            _snapshot_endpoints())])
        # The remote db is DOWN, but a snapshot has no incident
        # history, so no "failed check" line may appear.
        self.assertNotIn("failed check", page)


def _make_server(payload, status=200):
    """A tiny HTTP server answering GET /api/status with `payload`.

    `payload` is a dict (served as JSON), a str (served verbatim),
    or a callable taking the request path and returning
    (status, body-bytes). Returns (server, thread, base_url).
    """
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if callable(payload):
                code, body = payload(self.path)
            elif isinstance(payload, dict):
                code, body = status, json.dumps(payload).encode("utf-8")
            else:
                code, body = status, payload.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


class FetchTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, "_server"):
            self._server.shutdown()
            self._server.server_close()

    def _serve(self, payload, status=200):
        self._server, thread, base = _make_server(payload, status)
        self._thread = thread
        return base

    def test_fetch_returns_the_snapshot(self):
        base = self._serve(SNAPSHOT)
        history = fetch_remote_status(base)
        self.assertIsNotNone(history)
        self.assertEqual(history.names(),
                         ["api", "batch", "db", "new", "web"])
        self.assertEqual(history.uptime_pct("web"), 99.5)

    def test_base_url_with_trailing_slash(self):
        base = self._serve(SNAPSHOT)
        self.assertIsNotNone(fetch_remote_status(base + "/"))

    def test_api_status_url_given_directly(self):
        base = self._serve(SNAPSHOT)
        self.assertIsNotNone(fetch_remote_status(base + "/api/status"))

    def test_unreachable_instance_returns_none(self):
        # Nothing listens on this port: connection refused.
        self.assertIsNone(fetch_remote_status("http://127.0.0.1:1"))

    def test_non_200_returns_none(self):
        base = self._serve(SNAPSHOT, status=500)
        self.assertIsNone(fetch_remote_status(base))

    def test_bad_json_returns_none(self):
        base = self._serve("this is not json {")
        self.assertIsNone(fetch_remote_status(base))

    def test_wrong_shape_returns_none(self):
        base = self._serve({"hello": "world"})
        self.assertIsNone(fetch_remote_status(base))

    def test_non_http_url_returns_none(self):
        self.assertIsNone(fetch_remote_status("ftp://example.com"))


class CliRemoteSourceTests(unittest.TestCase):
    def setUp(self):
        self._server, self._thread, self._base = _make_server(SNAPSHOT)

    def tearDown(self):
        self._server.shutdown()
        self._server.server_close()

    def _write_config(self, d):
        import os
        cfg_path = os.path.join(d, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"endpoints": [{"name": "web", "type": "http",
                                      "url": "http://example.com"}]}, fh)
        return cfg_path

    def test_status_page_merges_remote_source(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._write_config(d)
            out_path = os.path.join(d, "status.html")
            rc = main(["--config", cfg_path, "--status-page", out_path,
                       "--status-page-source", f"eu-west={self._base}"])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("<h2>local</h2>", page)
            self.assertIn("<h2>eu-west</h2>", page)
            self.assertIn("99.5%", page)
            self.assertIn("1 endpoint down", page)

    def test_unreachable_remote_source_still_renders(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._write_config(d)
            out_path = os.path.join(d, "status.html")
            rc = main(["--config", cfg_path, "--status-page", out_path,
                       "--status-page-source",
                       "eu-west=http://127.0.0.1:1"])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("<h2>eu-west</h2>", page)
            self.assertIn("No endpoints configured.", page)
            self.assertIn("web", page)  # local source still rendered


if __name__ == "__main__":
    unittest.main()
