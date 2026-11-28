"""Tests for the SLA breach section in the periodic digest.

The digest carries an SLA breach section built from the same
sla_snapshot the --sla-report prints — the month's uptime per
endpoint against its target, breached first (worst shortfall
first), with the summary counts — so the digest, the report, and
the dashboard can never disagree. The section rides along on a
digest that goes out for another reason; a breach on its own never
triggers a send.
"""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import healthcheck.digest as digest_mod
from healthcheck.checker import CheckResult
from healthcheck.cli import monitor_loop
from healthcheck.digest import (DigestAlerter, build_digest,
                                render_digest_email,
                                sla_breach_summary)
from healthcheck.server import sla_snapshot
from healthcheck.store import History


def _at(year, month, day, hour=12, minute=0):
    """Epoch seconds for a local-time date, so the tests work in any
    timezone: month bounds are computed in local time too."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def _result(name, ok, at, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=10.0,
                       checked_at=at, **kwargs)


def _history():
    """Seed for November 2026: api up 10/10 (met 99.9); db up 9/10
    (90.0%, breached 99.9 by 9.9pp); web up 5/5 with no target;
    idle has a target but no checks in the month."""
    hist = History()
    for day in range(1, 11):
        hist.record(_result("api", True, _at(2026, 11, day)))
    for day in range(1, 10):
        hist.record(_result("db", True, _at(2026, 11, day)))
    hist.record(_result("db", False, _at(2026, 11, 10)))
    for day in range(1, 6):
        hist.record(_result("web", True, _at(2026, 11, day)))
    hist.record(_result("idle", True, _at(2026, 10, 15)))
    return hist


ENDPOINTS = [
    {"name": "api", "type": "http", "url": "https://example.com",
     "sla_target": 99.9},
    {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432,
     "sla_target": 99.9},
    {"name": "web", "type": "http", "url": "https://example.com"},
    {"name": "idle", "type": "http", "url": "https://example.com",
     "sla_target": 99.9},
]


def _snapshot():
    return sla_snapshot(_history(), ENDPOINTS, 2026, 11)


def _change(name, ok, at, status=None, error=None):
    return {
        "endpoint": name, "event": "recovered" if ok else "down",
        "ok": ok, "previous_ok": not ok, "latency_ms": 12.3,
        "status_code": status, "error": error, "checked_at": at,
    }


class SlaBreachSummaryTests(unittest.TestCase):
    def test_counts_match_the_report(self):
        self.assertEqual(sla_breach_summary(_snapshot()), {
            "breached": 1, "met": 1, "no_target": 1, "no_data": 1,
        })

    def test_empty_snapshot_counts_zero(self):
        snapshot = sla_snapshot(History(), [], 2026, 11)
        self.assertEqual(sla_breach_summary(snapshot), {
            "breached": 0, "met": 0, "no_target": 0, "no_data": 0,
        })

    def test_breaches_list_is_worst_shortfall_first(self):
        self.assertEqual(_snapshot()["breaches"], ["db"])


class BuildDigestSlaBreachTests(unittest.TestCase):
    def test_payload_carries_snapshot_rows_and_breach_list(self):
        digest = build_digest(
            [_change("db", False, at=1000.0)], ["db"],
            1000.0, 1900.0, sla_breach=_snapshot())
        sla = digest["sla_breach"]
        self.assertEqual(sla["month"], "2026-11")
        self.assertEqual(sla["breaches"], ["db"])
        by_name = {row["name"]: row for row in sla["endpoints"]}
        self.assertEqual(by_name["api"]["uptime_pct"], 100.0)
        self.assertTrue(by_name["api"]["met"])
        self.assertEqual(by_name["db"]["uptime_pct"], 90.0)
        self.assertFalse(by_name["db"]["met"])
        self.assertEqual(by_name["db"]["shortfall_pct"], 9.9)
        self.assertIsNone(by_name["web"]["target"])
        self.assertIsNone(by_name["idle"]["uptime_pct"])
        self.assertEqual(sla["summary"], {
            "breached": 1, "met": 1, "no_target": 1, "no_data": 1,
        })

    def test_no_snapshot_means_no_section(self):
        digest = build_digest([], [], 0.0, 60.0)
        self.assertNotIn("sla_breach", digest)
        # And the existing summary shape is untouched.
        self.assertEqual(digest["summary"], {
            "changes": 0, "down": 0, "recovered": 0,
            "currently_down": 0, "flapping": 0,
        })

    def test_both_sections_coexist(self):
        from healthcheck.server import cert_expiry_snapshot
        cert = cert_expiry_snapshot(History(), [], now=1000.0)
        digest = build_digest(
            [_change("db", False, at=1000.0)], ["db"],
            1000.0, 1900.0, cert_expiry=cert, sla_breach=_snapshot())
        self.assertIn("cert_expiry", digest)
        self.assertIn("sla_breach", digest)


class RenderEmailSlaBreachTests(unittest.TestCase):
    def test_email_lists_breaches_first_with_summary(self):
        digest = build_digest(
            [_change("db", False, at=1000.0)], ["db"],
            1000.0, 1900.0, sla_breach=_snapshot())
        text = render_digest_email(digest, "monitor@example.com",
                                   ["ops@example.com"])
        self.assertIn("SLA breaches (2026-11, 4 endpoint(s)):", text)
        body = text[text.index("SLA breaches (2026-11"):]
        # Breached first, worst shortfall first; the rest follow in
        # config order.
        self.assertLess(body.index("db: 90.0% (10 checks) - BREACH"),
                        body.index("api: 100.0%"))
        self.assertIn("db: 90.0% (10 checks) - BREACH target 99.9% "
                      "(short 9.9pp)", text)
        self.assertIn("api: 100.0% (10 checks) - MET target 99.9%",
                      text)
        self.assertIn("web: 100.0% (5 checks) - no target", text)
        self.assertIn("idle: no data - no data", text)
        self.assertIn("Summary: 1 breached, 1 met, 1 no target, "
                      "1 no data", text)

    def test_email_without_snapshot_has_no_section(self):
        digest = build_digest(
            [_change("db", False, at=10.0)], ["db"], 0.0, 60.0)
        text = render_digest_email(digest, "monitor@example.com",
                                   ["ops@example.com"])
        self.assertNotIn("SLA breaches", text)


class DigestAlerterSlaBreachTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = digest_mod.post_alert
        digest_mod.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        digest_mod.post_alert = self._real_post

    def _alerter(self, routes=None, **overrides):
        cfg = {"interval_seconds": 900,
               "webhook_url": "https://hooks.example/digest"}
        cfg.update(overrides)
        return DigestAlerter(cfg, now_fn=lambda: 1000.0, routes=routes)

    def test_webhook_payload_and_email_carry_the_section(self):
        alerter = self._alerter(email_to=["ops@example.com"])
        alerter.notify(_result("db", True, at=1000.0))
        alerter.notify(_result("db", False, at=1100.0))
        result = alerter.maybe_send(now=2000.0, current_down=["db"],
                                    sla_breach=_snapshot())
        self.assertIsNotNone(result)
        self.assertEqual(len(self.sent), 1)
        _, payload = self.sent[0]
        self.assertEqual(payload["sla_breach"]["month"], "2026-11")
        self.assertEqual(payload["sla_breach"]["breaches"], ["db"])
        self.assertEqual(payload["sla_breach"]["summary"], {
            "breached": 1, "met": 1, "no_target": 1, "no_data": 1,
        })
        self.assertIn("SLA breaches (2026-11, 4 endpoint(s)):",
                      result["email"])
        self.assertIn("db: 90.0% (10 checks) - BREACH target 99.9% "
                      "(short 9.9pp)", result["email"])
        self.assertIn("Summary: 1 breached, 1 met, 1 no target, "
                      "1 no data", result["email"])

    def test_breach_alone_does_not_trigger_a_send(self):
        # The section rides along on a digest that goes out for
        # another reason; a quiet window still sends nothing.
        alerter = self._alerter()
        self.assertIsNone(alerter.maybe_send(
            now=2000.0, current_down=[], sla_breach=_snapshot()))
        self.assertEqual(self.sent, [])

    def test_routed_digests_each_carry_the_full_sla_section(self):
        # The changes and currently-down lists split by routing,
        # but the SLA section is the whole estate in every POST —
        # a routed digest must not disagree with the report's
        # summary counts.
        alerter = self._alerter(
            routes={"db": "https://hooks.example/db"})
        alerter.notify(_result("db", True, at=1000.0))
        alerter.notify(_result("db", False, at=1100.0, error="boom"))
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1150.0, error="boom"))
        result = alerter.maybe_send(now=2000.0,
                                    current_down=["db", "api"],
                                    sla_breach=_snapshot())
        self.assertIsNotNone(result)
        self.assertEqual(len(self.sent), 2)
        by_url = {url: payload for url, payload in self.sent}
        for url, payload in by_url.items():
            self.assertEqual(payload["sla_breach"]["breaches"], ["db"])
            self.assertEqual(payload["sla_breach"]["summary"], {
                "breached": 1, "met": 1, "no_target": 1, "no_data": 1,
            })
        # The split still applies to the changes themselves.
        self.assertEqual(
            [c["endpoint"] for c in
             by_url["https://hooks.example/db"]["changes"]],
            ["db"])
        self.assertEqual(
            [c["endpoint"] for c in
             by_url["https://hooks.example/digest"]["changes"]],
            ["api"])
        self.assertEqual(
            len(result["digest"]["sla_breach"]["endpoints"]), 4)


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records POST bodies as parsed JSON."""
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append(json.loads(body))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _run_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class MonitorLoopDigestSlaBreachSmokeTests(unittest.TestCase):
    """Live smoke: the monitor loop's periodic digest POSTs an SLA
    breach section built from the checks it just ran — the closed
    TCP endpoint breaches its 99.9 target in the current month,
    the healthy HTTP endpoint meets it."""

    def test_loop_digest_posts_sla_breach_section(self):
        _CaptureHandler.received = []
        capture = _run_server(_CaptureHandler)
        ok_server = _run_server(_OkHandler)
        try:
            hook_url = (f"http://127.0.0.1:{capture.server_port}/hook")
            api_url = f"http://127.0.0.1:{ok_server.server_port}/"
            cfg = {
                "interval_seconds": 0.05,
                "webhook_url": hook_url,
                "alert_digest": {
                    "interval_seconds": 1,
                    "webhook_url": hook_url,
                },
                "endpoints": [
                    {"name": "api", "type": "http", "url": api_url,
                     "sla_target": 99.9},
                    {"name": "db", "type": "tcp",
                     "host": "127.0.0.1", "port": 1, "timeout": 1,
                     "sla_target": 99.9},
                ],
            }
            monitor_loop(cfg, History(), None, None,
                         threading.Event(), max_rounds=60)
        finally:
            capture.shutdown()
            capture.server_close()
            ok_server.shutdown()
            ok_server.server_close()
        digests = [p for p in _CaptureHandler.received
                   if p.get("type") == "digest"]
        self.assertTrue(digests, "no digest was POSTed")
        sla = digests[-1]["sla_breach"]
        now = time.localtime()
        self.assertEqual(sla["month"],
                         f"{now.tm_year:04d}-{now.tm_mon:02d}")
        self.assertEqual(sla["breaches"], ["db"])
        by_name = {row["name"]: row for row in sla["endpoints"]}
        self.assertEqual(sorted(by_name), ["api", "db"])
        self.assertTrue(by_name["api"]["met"])
        self.assertEqual(by_name["api"]["uptime_pct"], 100.0)
        self.assertFalse(by_name["db"]["met"])
        self.assertEqual(by_name["db"]["uptime_pct"], 0.0)
        self.assertEqual(by_name["db"]["shortfall_pct"], 99.9)
        self.assertEqual(sla["summary"], {
            "breached": 1, "met": 1, "no_target": 0, "no_data": 0,
        })
        # The db endpoint is down, so the digest went out for a
        # real reason — the SLA section rode along on it.
        self.assertEqual(digests[-1]["currently_down"], ["db"])


if __name__ == "__main__":
    unittest.main()
