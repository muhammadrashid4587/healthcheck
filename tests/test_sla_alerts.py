"""Tests for webhook alerting on SLA breach."""

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import healthcheck.alerts as alerts
from healthcheck.alerts import SlaBreachAlerter, sla_breach_payload
from healthcheck.checker import CheckResult
from healthcheck.cli import main, make_sla_alerter
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History


def _at(year, month, day, hour=12, minute=0):
    """Epoch seconds for a local-time date, so the tests work in any
    timezone: month bounds are computed in local time too."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def _result(name, ok, at, latency=10.0, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at, **kwargs)


def _breached_history():
    """History where api breaches 99.9% in Nov 2026 (90.0% over 10
    checks) and web meets it (100% over 10 checks)."""
    history = History()
    for minute in range(8):
        history.record(_result("api", True, _at(2026, 11, 5, 12, minute)))
    history.record(_result("api", False, _at(2026, 11, 5, 12, 8)))
    history.record(_result("api", True, _at(2026, 11, 5, 12, 9)))
    for minute in range(10):
        history.record(_result("web", True, _at(2026, 11, 5, 12, minute)))
    return history


_ENDPOINTS = [
    {"name": "api", "type": "http", "url": "http://example.com",
     "sla_target": 99.9},
    {"name": "web", "type": "http", "url": "http://example.com",
     "sla_target": 99.9},
    {"name": "quiet", "type": "tcp", "host": "127.0.0.1", "port": 5432},
]


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records POST bodies; status code comes from the server attribute."""
    received = []
    status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append(
            (self.path, self.headers.get("Content-Type"), body))
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _run_server(status=200):
    _CaptureHandler.received = []
    _CaptureHandler.status = status
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class PayloadTests(unittest.TestCase):
    def test_breach_payload(self):
        row = {"name": "api", "target": 99.9, "uptime_pct": 90.0,
               "checks": 10, "met": False, "shortfall_pct": 9.9,
               "incidents": 1, "failed_checks": 1,
               "downtime_seconds": 60.0,
               "latency": {"p50": 10.0, "p95": 10.0, "p99": 10.0}}
        self.assertEqual(sla_breach_payload(row, 2026, 11), {
            "type": "sla_breach",
            "endpoint": "api",
            "month": "2026-11",
            "uptime_pct": 90.0,
            "checks": 10,
            "incidents": 1,
            "downtime_seconds": 60.0,
            "target": 99.9,
            "shortfall_pct": 9.9,
            "latency": {"p50": 10.0, "p95": 10.0, "p99": 10.0},
        })


class SlaBreachAlerterTests(unittest.TestCase):
    def setUp(self):
        self.history = _breached_history()
        self._real_post = alerts.post_alert
        self.sent = []
        alerts.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts.post_alert = self._real_post

    def test_fires_once_for_the_breached_endpoint_only(self):
        alerter = SlaBreachAlerter("http://hooks.example/sla")
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 11)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["endpoint"], "api")
        self.assertEqual(sent[0]["type"], "sla_breach")
        self.assertEqual(sent[0]["shortfall_pct"], 9.9)
        self.assertEqual(self.sent[0][0], "http://hooks.example/sla")
        # The meeting endpoint and the one with no target send nothing.
        self.assertEqual(
            [p["endpoint"] for _, p in self.sent], ["api"])

    def test_does_not_refire_for_the_same_month(self):
        alerter = SlaBreachAlerter("http://hooks.example/sla")
        alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 11)
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 11)
        self.assertEqual(sent, [])
        self.assertEqual(len(self.sent), 1)

    def test_fires_again_for_a_new_month(self):
        alerter = SlaBreachAlerter("http://hooks.example/sla")
        alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 11)
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 12)
        self.assertEqual(sent, [])
        # December has no checks at all, so nothing breaches there;
        # a fresh breach in a new month fires again.
        history = History()
        for minute in range(9):
            history.record(
                _result("api", True, _at(2026, 12, 5, 12, minute)))
        history.record(_result("api", False, _at(2026, 12, 5, 12, 9)))
        sent = alerter.maybe_alert(history, _ENDPOINTS, 2026, 12)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["month"], "2026-12")

    def test_no_breach_sends_nothing(self):
        history = History()
        for minute in range(10):
            history.record(_result("api", True, _at(2026, 11, 5, 12, minute)))
        alerter = SlaBreachAlerter("http://hooks.example/sla")
        self.assertEqual(
            alerter.maybe_alert(history, _ENDPOINTS, 2026, 11), [])
        self.assertEqual(self.sent, [])

    def test_no_data_is_not_a_breach(self):
        alerter = SlaBreachAlerter("http://hooks.example/sla")
        sent = alerter.maybe_alert(History(), _ENDPOINTS, 2026, 11)
        self.assertEqual(sent, [])

    def test_per_endpoint_webhook_wins(self):
        alerter = SlaBreachAlerter(
            "http://hooks.example/global",
            routes={"api": "http://hooks.example/api"})
        alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 11)
        self.assertEqual(self.sent[0][0], "http://hooks.example/api")

    def test_no_webhook_anywhere_sends_nothing(self):
        alerter = SlaBreachAlerter(None)
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 11)
        self.assertEqual(sent, [])
        self.assertEqual(self.sent, [])

    def test_records_the_alert_and_survives_recreation(self):
        records = {}
        alerter = SlaBreachAlerter("http://hooks.example/sla",
                                   records=records)
        alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 11,
                            now=1_700_000_000.0)
        self.assertEqual(records["api"]["month"], "2026-11")
        self.assertEqual(records["api"]["shortfall_pct"], 9.9)
        self.assertEqual(records["api"]["alerted_at"], 1_700_000_000.0)
        self.assertEqual(alerter.alerted_for("api"), records["api"])
        self.assertIsNone(alerter.alerted_for("web"))
        # A fresh alerter sharing the records does not re-fire.
        fresh = SlaBreachAlerter("http://hooks.example/sla",
                                 records=records)
        self.assertEqual(
            fresh.maybe_alert(self.history, _ENDPOINTS, 2026, 11), [])
        self.assertEqual(len(self.sent), 1)

    def test_failed_delivery_is_still_recorded_not_resent(self):
        alerts.post_alert = lambda url, payload, timeout=5.0: False
        alerter = SlaBreachAlerter("http://hooks.example/sla")
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 11)
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            alerter.maybe_alert(self.history, _ENDPOINTS, 2026, 11), [])


class MakeSlaAlerterTests(unittest.TestCase):
    def test_none_when_no_webhook_configured(self):
        cfg = {"endpoints": [{"name": "api", "type": "http",
                              "url": "http://example.com"}]}
        self.assertIsNone(make_sla_alerter(cfg, None))

    def test_exists_when_an_endpoint_has_its_own_webhook(self):
        cfg = {"endpoints": [
            {"name": "api", "type": "http", "url": "http://example.com",
             "webhook": "http://hooks.example/api"}]}
        alerter = make_sla_alerter(cfg, None)
        self.assertIsNotNone(alerter)
        self.assertIsNone(alerter.url)
        self.assertEqual(alerter.url_for("api"),
                         "http://hooks.example/api")


class StatusSnapshotTests(unittest.TestCase):
    def test_breach_fields_in_status_snapshot(self):
        # The breach fields describe the current month, so build the
        # history in this month rather than a fixed past one.
        now = time.localtime()
        year, month = now.tm_year, now.tm_mon
        history = History()
        for minute in range(8):
            history.record(_result("api", True, _at(year, month, 5, 12, minute)))
        history.record(_result("api", False, _at(year, month, 5, 12, 8)))
        history.record(_result("api", True, _at(year, month, 5, 12, 9)))
        for minute in range(10):
            history.record(_result("web", True, _at(year, month, 5, 12, minute)))
        snap = status_snapshot(history, _ENDPOINTS)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        api = by_name["api"]
        self.assertEqual(api["sla_target"], 99.9)
        self.assertIs(api["sla_breach"], True)
        self.assertEqual(api["sla_shortfall_pct"], 9.9)
        self.assertIsNone(api["sla_breach_alerted_at"])
        web = by_name["web"]
        self.assertEqual(web["sla_target"], 99.9)
        self.assertIs(web["sla_breach"], False)
        self.assertIsNone(web["sla_shortfall_pct"])
        quiet = by_name["quiet"]
        self.assertIsNone(quiet["sla_target"])
        self.assertIs(quiet["sla_breach"], False)

    def test_alerted_at_in_status_snapshot(self):
        now = time.localtime()
        year, month = now.tm_year, now.tm_mon
        history = History()
        history.record(_result("api", False, _at(year, month, 5, 12, 0)))
        records = {"api": {"month": f"{year:04d}-{month:02d}",
                           "shortfall_pct": 100.0,
                           "alerted_at": 1_700_000_000.0}}
        snap = status_snapshot(history, _ENDPOINTS, sla_alerts=records)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["api"]["sla_breach_alerted_at"],
                         1_700_000_000.0)
        # A record from another month does not count for this one.
        stale = {"api": {"month": "2020-01", "shortfall_pct": 100.0,
                         "alerted_at": 1_600_000_000.0}}
        snap = status_snapshot(history, _ENDPOINTS, sla_alerts=stale)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertIsNone(by_name["api"]["sla_breach_alerted_at"])


class DashboardTests(unittest.TestCase):
    def test_dashboard_shows_the_breach_and_alert_time(self):
        now = time.localtime()
        year, month = now.tm_year, now.tm_mon
        history = History()
        for minute in range(8):
            history.record(_result("api", True, _at(year, month, 5, 12, minute)))
        history.record(_result("api", False, _at(year, month, 5, 12, 8)))
        history.record(_result("api", True, _at(year, month, 5, 12, 9)))
        page = render_dashboard(history, _ENDPOINTS)
        self.assertIn("SLA breach", page)
        self.assertIn("BREACH -9.9pp", page)
        # With an alert record, the time it fired shows too.
        alerted = _at(year, month, 5, 12, 30)
        records = {"api": {"month": f"{year:04d}-{month:02d}",
                           "shortfall_pct": 9.9,
                           "alerted_at": alerted}}
        page = render_dashboard(history, _ENDPOINTS, sla_alerts=records)
        self.assertIn("BREACH -9.9pp alerted 12:30", page)

    def test_dashboard_shows_dash_without_a_breach(self):
        history = History()
        for minute in range(10):
            history.record(_result("web", True, _at(2026, 11, 5, 12, minute)))
        page = render_dashboard(history, _ENDPOINTS)
        self.assertNotIn("BREACH", page)


class SlaAlertCliTests(unittest.TestCase):
    def test_sla_alert_posts_the_breach_and_exits_zero(self):
        server = _run_server(status=200)
        try:
            url = f"http://127.0.0.1:{server.server_port}/hook"
            with tempfile.TemporaryDirectory() as d:
                hist_path = os.path.join(d, "history.json")
                _breached_history().save(hist_path)
                cfg = {"history_file": hist_path,
                       "webhook_url": url,
                       "endpoints": _ENDPOINTS}
                cfg_path = os.path.join(d, "config.json")
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh)
                code = main(["--config", cfg_path,
                             "--sla-alert", "2026-11"])
            self.assertEqual(code, 0)
            self.assertEqual(len(_CaptureHandler.received), 1)
            path, content_type, body = _CaptureHandler.received[0]
            self.assertEqual(path, "/hook")
            self.assertEqual(content_type, "application/json")
            payload = json.loads(body)
            self.assertEqual(payload["type"], "sla_breach")
            self.assertEqual(payload["endpoint"], "api")
            self.assertEqual(payload["month"], "2026-11")
            self.assertEqual(payload["shortfall_pct"], 9.9)
        finally:
            server.shutdown()
            server.server_close()

    def test_sla_alert_with_no_breach_sends_nothing(self):
        server = _run_server(status=200)
        try:
            url = f"http://127.0.0.1:{server.server_port}/hook"
            with tempfile.TemporaryDirectory() as d:
                history = History()
                for minute in range(10):
                    history.record(
                        _result("api", True, _at(2026, 11, 5, 12, minute)))
                hist_path = os.path.join(d, "history.json")
                history.save(hist_path)
                cfg = {"history_file": hist_path,
                       "webhook_url": url,
                       "endpoints": _ENDPOINTS}
                cfg_path = os.path.join(d, "config.json")
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump(cfg, fh)
                self.assertEqual(
                    main(["--config", cfg_path, "--sla-alert", "2026-11"]),
                    0)
            self.assertEqual(_CaptureHandler.received, [])
        finally:
            server.shutdown()
            server.server_close()

    def test_sla_alert_with_no_webhook_sends_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            hist_path = os.path.join(d, "history.json")
            _breached_history().save(hist_path)
            cfg = {"history_file": hist_path, "endpoints": _ENDPOINTS}
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            self.assertEqual(
                main(["--config", cfg_path, "--sla-alert", "2026-11"]), 0)

    def test_sla_alert_rejects_a_bad_month(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"endpoints": _ENDPOINTS}
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            with self.assertRaises(SystemExit) as ctx:
                main(["--config", cfg_path, "--sla-alert", "2026-13"])
            self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
