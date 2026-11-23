"""Tests for the static status page generator."""

import json
import os
import tempfile
import unittest

from healthcheck.checker import CheckResult
from healthcheck.cli import main
from healthcheck.statuspage import (
    endpoint_status,
    recent_incidents,
    render_status_page,
    write_status_page,
)
from healthcheck.store import History

BASE = 1_700_000_000.0


def _result(name, ok, latency=12.5, at=BASE, **kw):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at, **kw)


class IncidentTests(unittest.TestCase):
    def test_no_history_no_incidents(self):
        self.assertEqual(recent_incidents(History(), "web"), [])

    def test_all_up_no_incidents(self):
        hist = History()
        for i in range(3):
            hist.record(_result("web", True, at=BASE + i * 60))
        self.assertEqual(recent_incidents(hist, "web"), [])

    def test_consecutive_downs_are_one_incident(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60))
        hist.record(_result("web", False, at=BASE + 120))
        hist.record(_result("web", True, at=BASE + 180))
        incidents = recent_incidents(hist, "web")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["start"], BASE + 60)
        self.assertEqual(incidents[0]["end"], BASE + 180)
        self.assertEqual(incidents[0]["duration_s"], 120.0)
        self.assertEqual(incidents[0]["checks"], 2)
        self.assertFalse(incidents[0]["ongoing"])

    def test_ongoing_incident_has_no_end(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60))
        incidents = recent_incidents(hist, "web")
        self.assertEqual(len(incidents), 1)
        self.assertIsNone(incidents[0]["end"])
        self.assertTrue(incidents[0]["ongoing"])
        self.assertGreaterEqual(incidents[0]["duration_s"], 60.0)

    def test_separate_outages_are_separate_incidents_newest_first(self):
        hist = History()
        hist.record(_result("web", False, at=BASE))
        hist.record(_result("web", True, at=BASE + 60))
        hist.record(_result("web", False, at=BASE + 120))
        hist.record(_result("web", True, at=BASE + 180))
        incidents = recent_incidents(hist, "web")
        self.assertEqual(len(incidents), 2)
        self.assertEqual(incidents[0]["start"], BASE + 120)  # newest first
        self.assertEqual(incidents[1]["start"], BASE)

    def test_unconfirmed_skipped_and_maintenance_are_not_incidents(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60, unconfirmed=True))
        hist.record(_result("web", False, at=BASE + 120, skipped=True,
                            depends_on="db"))
        hist.record(_result("web", False, at=BASE + 180, maintenance=True))
        hist.record(_result("web", True, at=BASE + 240))
        self.assertEqual(recent_incidents(hist, "web"), [])

    def test_skip_does_not_split_an_incident(self):
        hist = History()
        hist.record(_result("web", False, at=BASE))
        hist.record(_result("web", False, at=BASE + 60, skipped=True,
                            depends_on="db"))
        hist.record(_result("web", False, at=BASE + 120))
        hist.record(_result("web", True, at=BASE + 180))
        incidents = recent_incidents(hist, "web")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["checks"], 2)

    def test_limit_is_respected(self):
        hist = History()
        for i in range(8):
            hist.record(_result("web", False, at=BASE + i * 120))
            hist.record(_result("web", True, at=BASE + i * 120 + 60))
        self.assertEqual(len(recent_incidents(hist, "web")), 5)
        self.assertEqual(len(recent_incidents(hist, "web", limit=2)), 2)


class StatusTests(unittest.TestCase):
    def test_unknown_without_history(self):
        self.assertEqual(endpoint_status(History(), "web"), "UNKNOWN")

    def test_unconfirmed_failure_still_shows_up(self):
        hist = History()
        hist.record(_result("web", False, unconfirmed=True))
        self.assertEqual(endpoint_status(hist, "web"), "UP")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.history.record(_result("web", True, at=BASE))
        self.history.record(_result("web", True, latency=20.0, at=BASE + 60))
        self.history.record(_result("web", False, at=BASE + 120))
        self.history.record(_result("web", True, at=BASE + 180))
        self.history.record(_result("db", False, at=BASE))
        self.endpoints = [
            {"name": "web", "type": "http", "url": "http://example.com"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
        ]

    def test_page_contains_statuses_uptime_and_sparkline(self):
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        self.assertIn("web", page)
        self.assertIn("db", page)
        self.assertIn("UP", page)
        self.assertIn("DOWN", page)
        self.assertIn("75.0%", page)   # web: 3/4 up overall
        self.assertIn("Uptime 24h", page)
        self.assertIn("Uptime 7d", page)
        self.assertIn("<svg", page)    # latency sparkline, inline
        self.assertIn("<polyline", page)

    def test_banner_reports_down_endpoints(self):
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        self.assertIn("1 endpoint down", page)

    def test_all_up_banner_when_nothing_down(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        page = render_status_page(hist, self.endpoints[:1], now=BASE + 60)
        self.assertIn("All systems operational", page)

    def test_incidents_section_lists_the_outage(self):
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        self.assertIn("Recent incidents", page)
        self.assertIn("1 failed check", page)

    def test_no_incidents_message_when_none(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        page = render_status_page(hist, self.endpoints[:1], now=BASE + 60)
        self.assertIn("No incidents recorded.", page)

    def test_page_is_self_contained(self):
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        self.assertNotIn("<script", page)
        self.assertNotIn("src=", page)
        self.assertNotIn("href=", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_html_escapes_endpoint_names(self):
        self.history.record(_result("<b>x</b>", True, at=BASE))
        page = render_status_page(
            self.history, [{"name": "<b>x</b>", "type": "http",
                            "url": "http://x"}], now=BASE + 60)
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)

    def test_empty_endpoints(self):
        page = render_status_page(History(), [], now=BASE)
        self.assertIn("No endpoints configured.", page)
        self.assertIn("All systems operational", page)


class WriteTests(unittest.TestCase):
    def test_write_creates_file_matching_render(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        endpoints = [{"name": "web", "type": "http", "url": "http://example.com"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "status.html")
            self.assertEqual(write_status_page(hist, endpoints, path, now=BASE + 60), path)
            with open(path, encoding="utf-8") as fh:
                written = fh.read()
            self.assertEqual(written, render_status_page(hist, endpoints, now=BASE + 60))
            self.assertFalse(os.path.exists(path + ".tmp"))  # no temp left behind


class CliTests(unittest.TestCase):
    def test_status_page_flag_writes_file_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            hist_path = os.path.join(d, "history.json")
            out_path = os.path.join(d, "status.html")
            hist = History()
            hist.record(_result("web", True, at=BASE))
            hist.record(_result("web", False, at=BASE + 60))
            hist.save(hist_path)
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"history_file": hist_path,
                           "endpoints": [{"name": "web", "type": "http",
                                          "url": "http://example.com"}]}, fh)
            rc = main(["--config", cfg_path, "--status-page", out_path])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("web", page)
            self.assertIn("DOWN", page)
            self.assertIn("1 failed check", page)

    def test_status_page_with_no_history_file_still_writes(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            out_path = os.path.join(d, "status.html")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"endpoints": [{"name": "web", "type": "http",
                                          "url": "http://example.com"}]}, fh)
            rc = main(["--config", cfg_path, "--status-page", out_path])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                self.assertIn("UNKNOWN", fh.read())

    def test_once_then_status_page_uses_the_fresh_check(self):
        # A TCP endpoint on a closed localhost port fails fast and
        # deterministically (connection refused), so --once records a
        # real down check the page then shows.
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            out_path = os.path.join(d, "status.html")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                # flap_threshold 1 so the single failed check is a
                # confirmed outage (the default 3 would leave it
                # unconfirmed and the page would still show UP).
                json.dump({"flap_threshold": 1,
                           "endpoints": [{"name": "db", "type": "tcp",
                                          "host": "127.0.0.1", "port": 1}]}, fh)
            rc = main(["--config", cfg_path, "--once",
                       "--status-page", out_path])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("DOWN", page)
            self.assertIn("1 failed check", page)


if __name__ == "__main__":
    unittest.main()
