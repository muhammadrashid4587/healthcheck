"""Tests for the latency-p95 alert section in the periodic digest.

The digest carries a latency-p95 section built from the same
latency_p95_snapshot the dashboard's Latency p95 alert column
reads — the windowed p95 over the last 24 hours per endpoint
against its threshold, alerting first (worst overage first), with
the summary counts — so the digest and the dashboard can never
disagree. The section rides along on a digest that goes out for
another reason; a p95 over its threshold on its own never triggers
a send.
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
                                latency_p95_summary,
                                render_digest_email)
from healthcheck.server import latency_p95_snapshot
from healthcheck.store import History


def _at(year, month, day, hour=12, minute=0):
    """Epoch seconds for a local-time date, so the tests work in any
    timezone: window bounds are computed in local time too."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


NOW = _at(2026, 11, 27, 12, 0)


def _result(name, ok, at, latency_ms=10.0, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=latency_ms,
                       checked_at=at, **kwargs)


def _history():
    """Seed, all checks one hour before NOW (inside the 24-hour
    window): slower p95 900 vs threshold 100 (alerting, overage
    800); slow p95 800 vs threshold 400 (alerting, overage 400);
    ok p95 200 vs threshold 400 (within); idle has a threshold but
    no checks in the window; plain has no threshold."""
    hist = History()
    for i in range(10):
        hist.record(_result("slower", True, NOW - 3600 + i,
                            latency_ms=900.0))
    for i in range(10):
        hist.record(_result("slow", True, NOW - 3600 + i,
                            latency_ms=800.0))
    for i in range(5):
        hist.record(_result("ok", True, NOW - 3600 + i,
                            latency_ms=200.0))
    hist.record(_result("idle", True, NOW - 48 * 3600,
                        latency_ms=100.0))
    hist.record(_result("plain", True, NOW - 3600, latency_ms=50.0))
    return hist


ENDPOINTS = [
    {"name": "slower", "type": "http", "url": "https://example.com",
     "latency_p95_threshold_ms": 100},
    {"name": "slow", "type": "http", "url": "https://example.com",
     "latency_p95_threshold_ms": 400},
    {"name": "ok", "type": "http", "url": "https://example.com",
     "latency_p95_threshold_ms": 400},
    {"name": "idle", "type": "http", "url": "https://example.com",
     "latency_p95_threshold_ms": 400},
    {"name": "plain", "type": "http", "url": "https://example.com"},
]


def _snapshot():
    return latency_p95_snapshot(_history(), ENDPOINTS, now=NOW)


def _change(name, ok, at, status=None, error=None):
    return {
        "endpoint": name, "event": "recovered" if ok else "down",
        "ok": ok, "previous_ok": not ok, "latency_ms": 12.3,
        "status_code": status, "error": error, "checked_at": at,
    }


class LatencyP95SummaryTests(unittest.TestCase):
    def test_counts_match_the_dashboard(self):
        self.assertEqual(latency_p95_summary(_snapshot()), {
            "alerting": 2, "within": 1, "no_data": 1, "no_threshold": 1,
        })

    def test_empty_snapshot_counts_zero(self):
        snapshot = latency_p95_snapshot(History(), [], now=NOW)
        self.assertEqual(latency_p95_summary(snapshot), {
            "alerting": 0, "within": 0, "no_data": 0, "no_threshold": 0,
        })


class LatencyP95SnapshotTests(unittest.TestCase):
    def test_rows_sorted_alerting_first_worst_overage_first(self):
        self.assertEqual([row["name"] for row in _snapshot()["endpoints"]],
                         ["slower", "slow", "ok", "idle", "plain"])

    def test_rows_carry_the_dashboard_text(self):
        by_name = {row["name"]: row
                   for row in _snapshot()["endpoints"]}
        self.assertEqual(by_name["slower"]["p95_ms"], 900.0)
        self.assertEqual(by_name["slower"]["threshold_ms"], 100)
        self.assertTrue(by_name["slower"]["alert"])
        self.assertEqual(by_name["slower"]["text"],
                         "ALERT 900.0 > 100 ms")
        self.assertEqual(by_name["slow"]["text"], "ALERT 800.0 > 400 ms")
        self.assertFalse(by_name["ok"]["alert"])
        self.assertEqual(by_name["ok"]["text"], "p95 200.0 / 400 ms")
        self.assertIsNone(by_name["idle"]["p95_ms"])
        self.assertEqual(by_name["idle"]["text"], "400 ms (no data)")
        self.assertIsNone(by_name["plain"]["threshold_ms"])
        self.assertEqual(by_name["plain"]["text"], "-")

    def test_generated_at_is_pinned(self):
        self.assertEqual(_snapshot()["generated_at"], NOW)


class BuildDigestLatencyP95Tests(unittest.TestCase):
    def test_payload_carries_snapshot_rows_and_summary(self):
        digest = build_digest(
            [_change("slow", False, at=1000.0)], ["slow"],
            1000.0, 1900.0, latency_p95=_snapshot())
        section = digest["latency_p95"]
        self.assertEqual(section["generated_at"], NOW)
        self.assertEqual([row["name"] for row in section["endpoints"]],
                         ["slower", "slow", "ok", "idle", "plain"])
        by_name = {row["name"]: row for row in section["endpoints"]}
        self.assertEqual(by_name["slower"]["p95_ms"], 900.0)
        self.assertTrue(by_name["slower"]["alert"])
        self.assertFalse(by_name["ok"]["alert"])
        self.assertEqual(section["summary"], {
            "alerting": 2, "within": 1, "no_data": 1, "no_threshold": 1,
        })

    def test_no_snapshot_means_no_section(self):
        digest = build_digest([], [], 0.0, 60.0)
        self.assertNotIn("latency_p95", digest)
        # And the existing summary shape is untouched.
        self.assertEqual(digest["summary"], {
            "changes": 0, "down": 0, "recovered": 0,
            "currently_down": 0, "flapping": 0,
        })

    def test_all_three_sections_coexist(self):
        from healthcheck.server import (cert_expiry_snapshot,
                                        sla_snapshot)
        cert = cert_expiry_snapshot(History(), [], now=1000.0)
        sla = sla_snapshot(History(), [], 2026, 11)
        digest = build_digest(
            [_change("slow", False, at=1000.0)], ["slow"],
            1000.0, 1900.0, cert_expiry=cert, sla_breach=sla,
            latency_p95=_snapshot())
        self.assertIn("cert_expiry", digest)
        self.assertIn("sla_breach", digest)
        self.assertIn("latency_p95", digest)


class RenderEmailLatencyP95Tests(unittest.TestCase):
    def test_email_lists_alerting_first_with_summary(self):
        digest = build_digest(
            [_change("slow", False, at=1000.0)], ["slow"],
            1000.0, 1900.0, latency_p95=_snapshot())
        text = render_digest_email(digest, "monitor@example.com",
                                   ["ops@example.com"])
        self.assertIn("Latency p95 alerts (24h window, "
                      "5 endpoint(s)):", text)
        body = text[text.index("Latency p95 alerts (24h window"):]
        # Alerting first, worst overage first; the rest follow in
        # config order.
        self.assertLess(body.index("slower: ALERT 900.0 > 100 ms"),
                        body.index("slow: ALERT 800.0 > 400 ms"))
        self.assertLess(body.index("slow: ALERT 800.0 > 400 ms"),
                        body.index("ok: p95 200.0 / 400 ms"))
        self.assertIn("ok: p95 200.0 / 400 ms", text)
        self.assertIn("idle: 400 ms (no data)", text)
        self.assertIn("plain: -", text)
        self.assertIn("Summary: 2 alerting, 1 within threshold, "
                      "1 no data, 1 no threshold", text)

    def test_email_without_snapshot_has_no_section(self):
        digest = build_digest(
            [_change("slow", False, at=10.0)], ["slow"], 0.0, 60.0)
        text = render_digest_email(digest, "monitor@example.com",
                                   ["ops@example.com"])
        self.assertNotIn("Latency p95 alerts", text)


class DigestAlerterLatencyP95Tests(unittest.TestCase):
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
        alerter.notify(_result("slow", True, at=1000.0))
        alerter.notify(_result("slow", False, at=1100.0))
        result = alerter.maybe_send(now=2000.0, current_down=["slow"],
                                    latency_p95=_snapshot())
        self.assertIsNotNone(result)
        self.assertEqual(len(self.sent), 1)
        _, payload = self.sent[0]
        section = payload["latency_p95"]
        self.assertEqual(section["generated_at"], NOW)
        self.assertEqual([row["name"] for row in section["endpoints"]],
                         ["slower", "slow", "ok", "idle", "plain"])
        self.assertEqual(section["summary"], {
            "alerting": 2, "within": 1, "no_data": 1, "no_threshold": 1,
        })
        self.assertIn("Latency p95 alerts (24h window, "
                      "5 endpoint(s)):", result["email"])
        self.assertIn("slower: ALERT 900.0 > 100 ms", result["email"])
        self.assertIn("Summary: 2 alerting, 1 within threshold, "
                      "1 no data, 1 no threshold", result["email"])

    def test_alert_alone_does_not_trigger_a_send(self):
        # The section rides along on a digest that goes out for
        # another reason; a quiet window still sends nothing.
        alerter = self._alerter()
        self.assertIsNone(alerter.maybe_send(
            now=2000.0, current_down=[], latency_p95=_snapshot()))
        self.assertEqual(self.sent, [])

    def test_routed_digests_each_carry_the_full_p95_section(self):
        # The changes and currently-down lists split by routing,
        # but the latency-p95 section is the whole estate in every
        # POST — a routed digest must not disagree with the
        # dashboard's alert column.
        alerter = self._alerter(
            routes={"slow": "https://hooks.example/slow"})
        alerter.notify(_result("slow", True, at=1000.0))
        alerter.notify(_result("slow", False, at=1100.0, error="boom"))
        alerter.notify(_result("ok", True, at=1000.0))
        alerter.notify(_result("ok", False, at=1150.0, error="boom"))
        result = alerter.maybe_send(now=2000.0,
                                    current_down=["slow", "ok"],
                                    latency_p95=_snapshot())
        self.assertIsNotNone(result)
        self.assertEqual(len(self.sent), 2)
        by_url = {url: payload for url, payload in self.sent}
        for url, payload in by_url.items():
            section = payload["latency_p95"]
            self.assertEqual(
                [row["name"] for row in section["endpoints"]],
                ["slower", "slow", "ok", "idle", "plain"])
            self.assertEqual(section["summary"], {
                "alerting": 2, "within": 1, "no_data": 1,
                "no_threshold": 1,
            })
        # The split still applies to the changes themselves.
        self.assertEqual(
            [c["endpoint"] for c in
             by_url["https://hooks.example/slow"]["changes"]],
            ["slow"])
        self.assertEqual(
            [c["endpoint"] for c in
             by_url["https://hooks.example/digest"]["changes"]],
            ["ok"])
        self.assertEqual(
            len(result["digest"]["latency_p95"]["endpoints"]), 5)


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


class _SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(0.25)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

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


class MonitorLoopDigestLatencyP95SmokeTests(unittest.TestCase):
    """Live smoke: the monitor loop's periodic digest POSTs a
    latency-p95 section built from the checks it just ran — the
    slow HTTP endpoint's p95 (~250 ms) crosses its 100 ms
    threshold, the fast one stays inside its 400 ms threshold.
    The closed TCP endpoint is down, so the digest goes out for a
    real reason — the p95 section rides along on it."""

    def test_loop_digest_posts_latency_p95_section(self):
        _CaptureHandler.received = []
        capture = _run_server(_CaptureHandler)
        slow_server = _run_server(_SlowHandler)
        ok_server = _run_server(_OkHandler)
        try:
            hook_url = (f"http://127.0.0.1:{capture.server_port}/hook")
            slow_url = f"http://127.0.0.1:{slow_server.server_port}/"
            fast_url = f"http://127.0.0.1:{ok_server.server_port}/"
            cfg = {
                "interval_seconds": 0.05,
                "webhook_url": hook_url,
                "alert_digest": {
                    "interval_seconds": 1,
                    "webhook_url": hook_url,
                },
                "endpoints": [
                    {"name": "slow", "type": "http", "url": slow_url,
                     "latency_p95_threshold_ms": 100},
                    {"name": "fast", "type": "http", "url": fast_url,
                     "latency_p95_threshold_ms": 400},
                    {"name": "db", "type": "tcp",
                     "host": "127.0.0.1", "port": 1, "timeout": 1},
                ],
            }
            monitor_loop(cfg, History(), None, None,
                         threading.Event(), max_rounds=60)
        finally:
            capture.shutdown()
            capture.server_close()
            slow_server.shutdown()
            slow_server.server_close()
            ok_server.shutdown()
            ok_server.server_close()
        digests = [p for p in _CaptureHandler.received
                   if p.get("type") == "digest"]
        self.assertTrue(digests, "no digest was POSTed")
        section = digests[-1]["latency_p95"]
        by_name = {row["name"]: row for row in section["endpoints"]}
        self.assertEqual(sorted(by_name), ["db", "fast", "slow"])
        self.assertTrue(by_name["slow"]["alert"])
        self.assertGreater(by_name["slow"]["p95_ms"], 100)
        self.assertTrue(by_name["slow"]["text"].startswith("ALERT "))
        self.assertFalse(by_name["fast"]["alert"])
        self.assertLess(by_name["fast"]["p95_ms"], 400)
        self.assertIsNone(by_name["db"]["threshold_ms"])
        self.assertEqual(by_name["db"]["text"], "-")
        self.assertEqual(section["summary"], {
            "alerting": 1, "within": 1, "no_data": 0, "no_threshold": 1,
        })
        # The db endpoint is down, so the digest went out for a
        # real reason — the p95 section rode along on it.
        self.assertEqual(digests[-1]["currently_down"], ["db"])


if __name__ == "__main__":
    unittest.main()
