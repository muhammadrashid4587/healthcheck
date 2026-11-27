"""Tests for per-endpoint p95 latency alerts
(latency_p95_threshold_ms)."""

import json
import os
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import healthcheck.alerts as alerts
from healthcheck.alerts import (LatencyP95Alerter,
                                latency_p95_breach_payload)
from healthcheck.checker import CheckResult
from healthcheck.cli import (apply_latency_p95_threshold_overrides,
                             load_config, make_latency_p95_alerter,
                             monitor_loop, parse_latency_p95_threshold,
                             reload_config,
                             validate_latency_p95_thresholds)
from healthcheck.server import (endpoint_latency_p95_alert,
                                endpoint_latency_p95_alert_text,
                                render_dashboard, status_snapshot)
from healthcheck.store import History

NOW = 1_700_000_000.0


def _result(name, latency, at):
    return CheckResult(name=name, ok=True, latency_ms=latency,
                       checked_at=at)


def _history(name, latencies):
    """History with one check per latency, all in the last hour."""
    hist = History()
    for i, latency in enumerate(latencies):
        hist.record(_result(name, latency, NOW - 3600 + i))
    return hist


def _slow_history():
    # 20 checks: 10 at 100 ms, 10 at 800 ms — nearest-rank p95
    # (rank 19 of 20) is 800 ms.
    return _history("api", [100.0] * 10 + [800.0] * 10)


class EvaluationTests(unittest.TestCase):
    def test_p95_above_threshold_alerts(self):
        ep = {"name": "api", "latency_p95_threshold_ms": 500}
        state = endpoint_latency_p95_alert(_slow_history(), ep, now=NOW)
        self.assertTrue(state["alert"])
        self.assertEqual(state["p95_ms"], 800.0)
        self.assertEqual(state["threshold_ms"], 500)

    def test_p95_below_threshold_does_not_alert(self):
        ep = {"name": "api", "latency_p95_threshold_ms": 900}
        state = endpoint_latency_p95_alert(_slow_history(), ep, now=NOW)
        self.assertFalse(state["alert"])
        self.assertEqual(state["p95_ms"], 800.0)

    def test_p95_equal_to_threshold_does_not_alert(self):
        # The p95 must *cross* the threshold — exactly at it is
        # not a breach.
        ep = {"name": "api", "latency_p95_threshold_ms": 800}
        state = endpoint_latency_p95_alert(_slow_history(), ep, now=NOW)
        self.assertFalse(state["alert"])

    def test_no_threshold_never_alerts(self):
        ep = {"name": "api"}
        state = endpoint_latency_p95_alert(_slow_history(), ep, now=NOW)
        self.assertFalse(state["alert"])
        self.assertIsNone(state["threshold_ms"])
        self.assertEqual(state["p95_ms"], 800.0)

    def test_no_recorded_latencies_never_alerts(self):
        # No data is not a slowdown.
        ep = {"name": "api", "latency_p95_threshold_ms": 500}
        state = endpoint_latency_p95_alert(History(), ep, now=NOW)
        self.assertFalse(state["alert"])
        self.assertIsNone(state["p95_ms"])

    def test_old_latencies_outside_the_window_do_not_count(self):
        # A slow p95 a day ago is not today's slowdown: the
        # window is the last 24 hours, like the percentiles
        # column.
        hist = History()
        for i, latency in enumerate([100.0] * 10 + [800.0] * 10):
            hist.record(_result("api", latency, NOW - 25 * 3600 + i))
        hist.record(_result("api", 100.0, NOW - 60))
        ep = {"name": "api", "latency_p95_threshold_ms": 500}
        state = endpoint_latency_p95_alert(hist, ep, now=NOW)
        self.assertFalse(state["alert"])
        self.assertEqual(state["p95_ms"], 100.0)

    def test_text_forms(self):
        slow = _slow_history()
        self.assertEqual(
            endpoint_latency_p95_alert_text(
                slow, {"name": "api", "latency_p95_threshold_ms": 500},
                now=NOW),
            "ALERT 800.0 > 500 ms")
        self.assertEqual(
            endpoint_latency_p95_alert_text(
                slow, {"name": "api", "latency_p95_threshold_ms": 900},
                now=NOW),
            "p95 800.0 / 900 ms")
        self.assertEqual(
            endpoint_latency_p95_alert_text(
                History(), {"name": "api",
                            "latency_p95_threshold_ms": 500}, now=NOW),
            "500 ms (no data)")
        self.assertEqual(
            endpoint_latency_p95_alert_text(slow, {"name": "api"},
                                            now=NOW),
            "-")


class ValidationTests(unittest.TestCase):
    def test_bad_thresholds_rejected(self):
        for bad in [True, "500", 0, -100, 0.0]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_latency_p95_thresholds(
                    [{"name": "api",
                      "latency_p95_threshold_ms": bad}])

    def test_good_thresholds_accepted(self):
        validate_latency_p95_thresholds(
            [{"name": "api", "latency_p95_threshold_ms": 500},
             {"name": "db", "latency_p95_threshold_ms": 12.5}])

    def test_bad_threshold_rejected_at_config_load(self):
        cfg = {"endpoints": [
            {"name": "api", "type": "http",
             "url": "https://example.com",
             "latency_p95_threshold_ms": "fast"}]}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                load_config(path)
        finally:
            os.unlink(path)


class FlagTests(unittest.TestCase):
    def test_parse_flag(self):
        self.assertEqual(parse_latency_p95_threshold("api=500"),
                         ("api", 500.0))
        self.assertEqual(parse_latency_p95_threshold("api=12.5"),
                         ("api", 12.5))

    def test_parse_flag_rejects_bad_specs(self):
        for bad in ["api", "api=", "=500", "api=fast", "api=0",
                    "api=-5"]:
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_latency_p95_threshold(bad)

    def test_flag_sets_threshold_and_wins_over_config(self):
        cfg = {"endpoints": [
            {"name": "api", "latency_p95_threshold_ms": 500}]}
        args = types.SimpleNamespace(
            latency_p95_threshold=["api=250"])
        apply_latency_p95_threshold_overrides(cfg, args)
        self.assertEqual(cfg["endpoints"][0]["latency_p95_threshold_ms"],
                         250.0)

    def test_flag_naming_unknown_endpoint_rejected(self):
        cfg = {"endpoints": [{"name": "api"}]}
        args = types.SimpleNamespace(
            latency_p95_threshold=["nope=500"])
        with self.assertRaises(ValueError):
            apply_latency_p95_threshold_overrides(cfg, args)

    def test_reload_picks_up_threshold_change(self):
        cfg = {"endpoints": [
            {"name": "api", "type": "http",
             "url": "https://example.com",
             "latency_p95_threshold_ms": 500}]}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        try:
            current = load_config(path)
            cfg["endpoints"][0]["latency_p95_threshold_ms"] = 250
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            args = types.SimpleNamespace()
            reloaded = reload_config(path, args, current)
            self.assertIsNot(reloaded, current)
            self.assertEqual(
                reloaded["endpoints"][0]["latency_p95_threshold_ms"],
                250)
        finally:
            os.unlink(path)


class SurfaceTests(unittest.TestCase):
    def test_api_carries_threshold_and_alert(self):
        hist = _slow_history()
        endpoints = [
            {"name": "api", "latency_p95_threshold_ms": 500},
            {"name": "web"}]
        snap = status_snapshot(hist, endpoints, now=NOW)
        by_name = {row["name"]: row for row in snap["endpoints"]}
        self.assertTrue(by_name["api"]["latency_p95_alert"])
        self.assertEqual(by_name["api"]["latency_p95_threshold_ms"],
                         500)
        # An endpoint with no threshold never alerts, and the
        # field is still present.
        self.assertFalse(by_name["web"]["latency_p95_alert"])
        self.assertIsNone(by_name["web"]["latency_p95_threshold_ms"])

    def test_dashboard_column_shows_alert_and_dash(self):
        hist = _slow_history()
        endpoints = [
            {"name": "api", "latency_p95_threshold_ms": 500},
            {"name": "web"}]
        html = render_dashboard(hist, endpoints, now=NOW)
        self.assertIn("Latency p95 alert", html)
        self.assertIn("ALERT 800.0 &gt; 500 ms", html)
        # The API and the dashboard read the same helper, so the
        # alert in one is the alert in the other.
        snap = status_snapshot(hist, endpoints, now=NOW)
        self.assertTrue(snap["endpoints"][0]["latency_p95_alert"])

    def test_api_carries_alerted_at(self):
        hist = _slow_history()
        endpoints = [{"name": "api", "latency_p95_threshold_ms": 500}]
        records = {"api": {"p95_ms": 800.0, "threshold_ms": 500,
                           "alerted_at": NOW - 60}}
        snap = status_snapshot(hist, endpoints, now=NOW,
                               latency_p95_alerts=records)
        row = snap["endpoints"][0]
        self.assertTrue(row["latency_p95_alert"])
        self.assertEqual(row["latency_p95_alerted_at"], NOW - 60)
        # No record: the alert fires on the page, but no webhook
        # time is shown.
        snap = status_snapshot(hist, endpoints, now=NOW)
        self.assertIsNone(
            snap["endpoints"][0]["latency_p95_alerted_at"])

    def test_stale_record_does_not_count_when_recovered(self):
        # A record for a crossing the p95 has since recovered
        # from is not shown: the alert re-armed.
        hist = _history("api", [100.0] * 20)
        endpoints = [{"name": "api", "latency_p95_threshold_ms": 500}]
        records = {"api": {"p95_ms": 800.0, "threshold_ms": 500,
                           "alerted_at": NOW - 60}}
        snap = status_snapshot(hist, endpoints, now=NOW,
                               latency_p95_alerts=records)
        row = snap["endpoints"][0]
        self.assertFalse(row["latency_p95_alert"])
        self.assertIsNone(row["latency_p95_alerted_at"])

    def test_dashboard_shows_alert_time(self):
        hist = _slow_history()
        endpoints = [{"name": "api", "latency_p95_threshold_ms": 500}]
        records = {"api": {"p95_ms": 800.0, "threshold_ms": 500,
                           "alerted_at": NOW - 60}}
        html = render_dashboard(hist, endpoints, now=NOW,
                                latency_p95_alerts=records)
        when = time.strftime("%H:%M", time.localtime(NOW - 60))
        self.assertIn(f"ALERT 800.0 &gt; 500 ms alerted {when}", html)
        # Without a record the alert shows without a time.
        html = render_dashboard(hist, endpoints, now=NOW)
        self.assertIn("ALERT 800.0 &gt; 500 ms", html)
        self.assertNotIn("alerted", html)


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


_ENDPOINTS = [{"name": "api", "latency_p95_threshold_ms": 500}]


class PayloadTests(unittest.TestCase):
    def test_breach_payload(self):
        payload = latency_p95_breach_payload("api", 800.0, 500, NOW)
        self.assertEqual(payload, {
            "type": "latency_p95_breach",
            "endpoint": "api",
            "p95_ms": 800.0,
            "threshold_ms": 500,
            "window_hours": 24,
            "checked_at": NOW,
        })


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.history = _slow_history()
        self._real_post = alerts.post_alert
        self.sent = []
        alerts.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts.post_alert = self._real_post

    def test_fires_once_on_crossing(self):
        alerter = LatencyP95Alerter("http://hooks.example/p95")
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["endpoint"], "api")
        self.assertEqual(sent[0]["type"], "latency_p95_breach")
        self.assertEqual(sent[0]["p95_ms"], 800.0)
        self.assertEqual(sent[0]["threshold_ms"], 500)
        self.assertEqual(sent[0]["window_hours"], 24)
        self.assertEqual(self.sent[0][0], "http://hooks.example/p95")

    def test_sustained_breach_fires_exactly_once(self):
        alerter = LatencyP95Alerter("http://hooks.example/p95")
        alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW)
        # More slow checks: the breach is still going, but no
        # second page goes out.
        for i in range(5):
            self.history.record(_result("api", 800.0, NOW + 60 + i))
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW + 300)
        self.assertEqual(sent, [])
        self.assertEqual(len(self.sent), 1)

    def test_rearms_after_recovery(self):
        alerter = LatencyP95Alerter("http://hooks.example/p95")
        alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW)
        # Fast checks push the p95 back under the threshold: the
        # history holds 100 checks, so 100 fast ones evict the
        # slow ones entirely.
        for i in range(100):
            self.history.record(_result("api", 100.0, NOW + 60 + i))
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW + 300)
        self.assertEqual(sent, [])
        self.assertEqual(alerter.records, {})
        self.assertIsNone(alerter.alerted_for("api"))
        # Slow again: the next crossing fires a second time.
        for i in range(10):
            self.history.record(_result("api", 800.0, NOW + 600 + i))
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW + 900)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(self.sent), 2)

    def test_no_threshold_sends_nothing(self):
        alerter = LatencyP95Alerter("http://hooks.example/p95")
        sent = alerter.maybe_alert(self.history, [{"name": "api"}],
                                   now=NOW)
        self.assertEqual(sent, [])
        self.assertEqual(self.sent, [])

    def test_no_data_sends_nothing(self):
        alerter = LatencyP95Alerter("http://hooks.example/p95")
        sent = alerter.maybe_alert(History(), _ENDPOINTS, now=NOW)
        self.assertEqual(sent, [])

    def test_per_endpoint_webhook_wins(self):
        alerter = LatencyP95Alerter(
            "http://hooks.example/global",
            routes={"api": "http://hooks.example/api"})
        alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW)
        self.assertEqual(self.sent[0][0], "http://hooks.example/api")

    def test_no_webhook_anywhere_sends_nothing(self):
        alerter = LatencyP95Alerter(None)
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW)
        self.assertEqual(sent, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(alerter.records, {})

    def test_breach_without_url_is_not_consumed(self):
        # An endpoint in breach with nowhere to send to is not
        # marked alerted: when a webhook appears, the crossing
        # still pages.
        alerter = LatencyP95Alerter(None)
        alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW)
        self.assertEqual(alerter.records, {})
        wired = LatencyP95Alerter("http://hooks.example/p95")
        sent = wired.maybe_alert(self.history, _ENDPOINTS, now=NOW)
        self.assertEqual(len(sent), 1)

    def test_records_the_alert_and_survives_recreation(self):
        records = {}
        alerter = LatencyP95Alerter("http://hooks.example/p95",
                                   records=records)
        alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW)
        self.assertEqual(records["api"]["p95_ms"], 800.0)
        self.assertEqual(records["api"]["threshold_ms"], 500)
        self.assertEqual(records["api"]["alerted_at"], NOW)
        self.assertEqual(alerter.alerted_for("api"), records["api"])
        # A fresh alerter sharing the records (a SIGHUP reload)
        # does not re-fire for the ongoing breach.
        fresh = LatencyP95Alerter("http://hooks.example/p95",
                                  records=records)
        self.assertEqual(
            fresh.maybe_alert(self.history, _ENDPOINTS, now=NOW), [])
        self.assertEqual(len(self.sent), 1)

    def test_failed_delivery_is_still_recorded_not_resent(self):
        alerts.post_alert = lambda url, payload, timeout=5.0: False
        alerter = LatencyP95Alerter("http://hooks.example/p95")
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW)
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            alerter.maybe_alert(self.history, _ENDPOINTS, now=NOW), [])


class MakeLatencyP95AlerterTests(unittest.TestCase):
    def test_none_when_no_webhook_configured(self):
        cfg = {"endpoints": [{"name": "api", "type": "http",
                              "url": "http://example.com"}]}
        self.assertIsNone(make_latency_p95_alerter(cfg, None))

    def test_exists_when_an_endpoint_has_its_own_webhook(self):
        cfg = {"endpoints": [
            {"name": "api", "type": "http", "url": "http://example.com",
             "webhook": "http://hooks.example/api"}]}
        alerter = make_latency_p95_alerter(cfg, None)
        self.assertIsNotNone(alerter)
        self.assertIsNone(alerter.url)
        self.assertEqual(alerter.url_for("api"),
                         "http://hooks.example/api")


class MonitorLoopTests(unittest.TestCase):
    def test_loop_fires_one_webhook_per_crossing(self):
        # A TCP check against a closed localhost port: a fast,
        # deterministic result with no network access — the
        # pre-seeded slow history keeps the p95 above the
        # threshold, so the round fires once. The history is
        # seeded around the real clock, since the loop evaluates
        # the p95 over the last 24 hours from now.
        server = _run_server(status=200)
        try:
            url = f"http://127.0.0.1:{server.server_port}/hook"
            ref = time.time()
            history = History()
            for i, latency in enumerate([100.0] * 10 + [800.0] * 10):
                history.record(_result("api", latency, ref - 3600 + i))
            cfg = {"interval_seconds": 1, "webhook_url": url,
                   "endpoints": [
                       {"name": "api", "type": "tcp",
                        "host": "127.0.0.1", "port": 1, "timeout": 1,
                        "latency_p95_threshold_ms": 500}]}
            args = types.SimpleNamespace(history_file=None,
                                         webhook_url=None,
                                         flap_threshold=None)
            records = {}
            stop = threading.Event()
            monitor_loop(cfg, history, None, args, stop, max_rounds=1,
                         latency_p95_alerts=records)
            self.assertEqual(len(_CaptureHandler.received), 1)
            path, ctype, body = _CaptureHandler.received[0]
            self.assertEqual(path, "/hook")
            self.assertEqual(ctype, "application/json")
            payload = json.loads(body)
            self.assertEqual(payload["type"], "latency_p95_breach")
            self.assertEqual(payload["endpoint"], "api")
            self.assertEqual(payload["p95_ms"], 800.0)
            self.assertIn("api", records)
            # A second round (a fresh alerter sharing the records,
            # like a SIGHUP reload) does not re-fire for the same
            # crossing.
            monitor_loop(cfg, history, None, args, stop, max_rounds=1,
                         latency_p95_alerts=records)
            self.assertEqual(len(_CaptureHandler.received), 1)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
