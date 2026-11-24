"""Tests for dependent checks: skip when the dependency is not UP."""

import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from healthcheck.checker import CheckResult, skipped_result
from healthcheck.cli import check_all, load_config
from healthcheck.flap import FlapDetector
from healthcheck.server import render_dashboard
from healthcheck.store import History


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class _RecordingAlerter:
    """Stands in for WebhookAlerter; records what it was told about."""

    def __init__(self):
        self.notified = []

    def notify(self, result):
        self.notified.append(result.name)
        return None


def _result(name, ok, latency=10.0, at=1_700_000_000.0, skipped=False,
            depends_on=None):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at, skipped=skipped,
                       depends_on=depends_on)


def _write_config(directory, endpoints):
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"endpoints": endpoints}, fh)
    return path


class SkipTests(unittest.TestCase):
    def test_dependent_skipped_while_dependency_down(self):
        # Dependent listed FIRST in the config: the round must still
        # check the dependency first, then skip the dependent. The
        # dependency only counts as down after the flap threshold
        # (3 consecutive failures), so three rounds run here with one
        # shared detector; in the first two the dependent is still
        # checked, because a single blip is not an outage.
        db_port = _free_port()
        api_port = _free_port()
        endpoints = [
            {"name": "api", "type": "tcp", "host": "127.0.0.1",
             "port": api_port, "depends_on": "db"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": db_port},
        ]
        history = History()
        alerter = _RecordingAlerter()
        detector = FlapDetector()
        for _ in range(3):
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
        db_entry = history.current("db")
        api_entry = history.current("api")
        self.assertFalse(db_entry["ok"])            # dependency is DOWN
        self.assertFalse(db_entry["unconfirmed"])   # confirmed, not a blip
        self.assertTrue(api_entry["skipped"])       # dependent is SKIPPED
        self.assertEqual(api_entry["depends_on"], "db")
        self.assertIn("db", api_entry["error"])
        self.assertIn("DOWN", api_entry["error"])
        # A skip is not a state change: the dependent is only
        # reported while it is really checked (rounds 1-2), never
        # for the round it was skipped.
        self.assertEqual(alerter.notified,
                         ["db", "api", "db", "api", "db"])
        # A history of only skips is no data, not 0% uptime.
        self.assertIsNone(history.uptime_pct("api"))

    def test_dependent_resumes_when_dependency_recovers(self):
        port = _free_port()
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": port},
            {"name": "api", "type": "tcp", "host": "127.0.0.1",
             "port": port, "depends_on": "db"},
        ]
        history = History()
        detector = FlapDetector()
        # Three rounds: nothing listening — db confirmed DOWN on the
        # third, api SKIPPED from then on.
        for _ in range(3):
            check_all(endpoints, history, flap_detector=detector)
        self.assertTrue(history.current("api")["skipped"])
        # Next round: the dependency is up now — api is really checked.
        server = HTTPServer(("127.0.0.1", port), _OkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            check_all(endpoints, history, flap_detector=detector)
        finally:
            server.shutdown()
            server.server_close()
        api_entry = history.current("api")
        self.assertFalse(api_entry["skipped"])
        self.assertTrue(api_entry["ok"])
        self.assertEqual(api_entry["depends_on"], "db")
        # Uptime counts only the real check: 1/1, not 1/2.
        self.assertEqual(history.uptime_pct("api"), 100.0)

    def test_skip_propagates_down_a_chain(self):
        db_port, mid_port, api_port = _free_port(), _free_port(), _free_port()
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": db_port},
            {"name": "mid", "type": "tcp", "host": "127.0.0.1",
             "port": mid_port, "depends_on": "db"},
            {"name": "api", "type": "tcp", "host": "127.0.0.1",
             "port": api_port, "depends_on": "mid"},
        ]
        history = History()
        detector = FlapDetector()
        # Three rounds so db is confirmed down; only then does the
        # skip propagate down the chain.
        for _ in range(3):
            check_all(endpoints, history, flap_detector=detector)
        self.assertFalse(history.current("db")["ok"])
        self.assertTrue(history.current("mid")["skipped"])
        self.assertTrue(history.current("api")["skipped"])
        self.assertIn("SKIPPED", history.current("api")["error"])


class UptimeExcludesSkipsTests(unittest.TestCase):
    def test_uptime_counts_only_real_checks(self):
        history = History()
        history.record(_result("api", True, at=100.0))
        history.record(_result("api", False, at=200.0, skipped=True,
                               depends_on="db"))
        history.record(_result("api", False, at=300.0))
        self.assertEqual(history.uptime_pct("api"), 50.0)  # 1/2, not 1/3
        self.assertEqual(
            history.uptime_pct_window("api", 1000, now=400.0), 50.0)

    def test_all_skips_is_no_data(self):
        history = History()
        history.record(_result("api", False, at=100.0, skipped=True,
                               depends_on="db"))
        self.assertIsNone(history.uptime_pct("api"))
        self.assertIsNone(history.uptime_pct_window("api", 1000, now=200.0))

    def test_skipped_result_helper(self):
        result = skipped_result("api", "db", "dependency 'db' is DOWN")
        self.assertTrue(result.skipped)
        self.assertFalse(result.ok)
        self.assertEqual(result.depends_on, "db")
        self.assertIn("db", result.error)


class ConfigValidationTests(unittest.TestCase):
    def test_cycle_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, [
                {"name": "a", "type": "tcp", "host": "127.0.0.1",
                 "port": 1, "depends_on": "b"},
                {"name": "b", "type": "tcp", "host": "127.0.0.1",
                 "port": 2, "depends_on": "a"},
            ])
            with self.assertRaises(ValueError) as ctx:
                load_config(path)
        self.assertIn("cycle", str(ctx.exception))

    def test_self_dependency_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, [
                {"name": "a", "type": "tcp", "host": "127.0.0.1",
                 "port": 1, "depends_on": "a"},
            ])
            with self.assertRaises(ValueError):
                load_config(path)

    def test_missing_dependency_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, [
                {"name": "a", "type": "tcp", "host": "127.0.0.1",
                 "port": 1, "depends_on": "ghost"},
            ])
            with self.assertRaises(ValueError) as ctx:
                load_config(path)
        self.assertIn("ghost", str(ctx.exception))

    def test_valid_dependency_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, [
                {"name": "db", "type": "tcp", "host": "127.0.0.1",
                 "port": 5432},
                {"name": "api", "type": "http",
                 "url": "http://127.0.0.1:8000", "depends_on": "db"},
            ])
            cfg = load_config(path)
        self.assertEqual(cfg["endpoints"][1]["depends_on"], "db")


class DashboardSkipTests(unittest.TestCase):
    def test_dashboard_shows_skipped_and_dependency(self):
        history = History()
        history.record(_result("db", False, at=1_700_000_000.0))
        history.record(_result("api", False, at=1_700_000_000.0,
                               skipped=True, depends_on="db"))
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": 5432},
            {"name": "api", "type": "http", "url": "http://127.0.0.1:8000",
             "depends_on": "db"},
        ]
        page = render_dashboard(history, endpoints)
        self.assertIn("SKIPPED", page)
        self.assertIn("Depends on", page)
        # The api row names its dependency; the db row has none.
        self.assertIn("<td>db</td>", page)
        # Skipped checks show "-" uptime, not 0%. The Down since
        # column sits between Status and Method, the Method column
        # between that and Headers, the Headers column between that
        # and HTTP status, the HTTP status column between that and
        # Cert expires, and Cert expires between that and Depends
        # on: a skipped check never ran, so it has no method to
        # show, sends no headers, never returned a status, never
        # read a certificate, and is not down, so all five are "-".
        self.assertIn("<td class=\"skipped\">SKIPPED</td><td>-</td>"
                      "<td>-</td><td>-</td><td>-</td><td>-</td><td>db</td>"
                      "<td>-</td>",
                      page)


if __name__ == "__main__":
    unittest.main()
