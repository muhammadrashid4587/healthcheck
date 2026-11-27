"""Tests for the per-endpoint alert threshold.

`alert_threshold` is how many consecutive failures must pile up
before an endpoint's outage pages, independent of the flap
threshold that flips it DOWN: an endpoint with alert_threshold 5
and the default flap threshold 3 flips DOWN on the third failure
but only alerts on the fifth, and one that recovers before the
fifth never pages at all. An endpoint without one alerts at
confirmation, like before.
"""

import json
import os
import tempfile
import threading
import types
import unittest

import healthcheck.alerts as alerts
from healthcheck.alerts import WebhookAlerter
from healthcheck.checker import CheckResult
from healthcheck.cli import (apply_alert_threshold_overrides, check_all,
                             endpoint_alert_threshold, load_config,
                             monitor_loop, parse_alert_threshold,
                             resolve_alert_thresholds)
from healthcheck.flap import FlapDetector
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History


def _args(**overrides):
    args = types.SimpleNamespace(
        history_file=None, webhook_url=None, flap_threshold=None,
        alert_threshold=None)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _write_config(directory, cfg):
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return path


def _closed(name="closed"):
    # A TCP check against a closed localhost port: deterministic
    # DOWN result, no network access needed.
    return [{"name": name, "type": "tcp", "host": "127.0.0.1",
             "port": 1, "timeout": 1}]


class ValidationTests(unittest.TestCase):
    def _load(self, endpoints):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(
                d, {"endpoints": endpoints})
            return load_config(path)

    def test_accepts_a_positive_integer(self):
        cfg = self._load([dict(_closed()[0], alert_threshold=5)])
        self.assertEqual(cfg["endpoints"][0]["alert_threshold"], 5)

    def test_rejects_zero(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(
                d, {"endpoints": [dict(_closed()[0],
                                       alert_threshold=0)]})
            with self.assertRaises(ValueError) as ctx:
                load_config(path)
            self.assertIn("alert_threshold", str(ctx.exception))

    def test_rejects_negative(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(
                d, {"endpoints": [dict(_closed()[0],
                                       alert_threshold=-1)]})
            with self.assertRaises(ValueError) as ctx:
                load_config(path)
            self.assertIn("alert_threshold", str(ctx.exception))

    def test_rejects_string_and_float_and_bool(self):
        for bad in ("5", 2.5, True):
            with tempfile.TemporaryDirectory() as d:
                path = _write_config(
                    d, {"endpoints": [dict(_closed()[0],
                                           alert_threshold=bad)]})
                with self.assertRaises(ValueError, msg=repr(bad)):
                    load_config(path)

    def test_absent_is_fine(self):
        cfg = self._load(_closed())
        self.assertNotIn("alert_threshold", cfg["endpoints"][0])


class ResolutionTests(unittest.TestCase):
    def test_explicit_value_wins(self):
        ep = {"name": "a", "alert_threshold": 5}
        self.assertEqual(endpoint_alert_threshold(ep, 3), 5)

    def test_unset_falls_back_to_flap_threshold(self):
        self.assertEqual(endpoint_alert_threshold({"name": "a"}, 3), 3)
        self.assertEqual(endpoint_alert_threshold({"name": "a"}, 2), 2)

    def test_resolve_fills_unset_from_config_flap_threshold(self):
        cfg = {"flap_threshold": 2,
               "endpoints": [{"name": "a"},
                             {"name": "b", "alert_threshold": 5}]}
        resolve_alert_thresholds(cfg, _args())
        by_name = {ep["name"]: ep for ep in cfg["endpoints"]}
        self.assertEqual(by_name["a"]["alert_threshold"], 2)
        self.assertEqual(by_name["b"]["alert_threshold"], 5)

    def test_resolve_uses_default_3_without_a_flap_threshold(self):
        cfg = {"endpoints": [{"name": "a"}]}
        resolve_alert_thresholds(cfg, _args())
        self.assertEqual(cfg["endpoints"][0]["alert_threshold"], 3)

    def test_resolve_uses_flag_flap_threshold(self):
        cfg = {"flap_threshold": 2,
               "endpoints": [{"name": "a"}]}
        resolve_alert_thresholds(cfg, _args(flap_threshold=4))
        self.assertEqual(cfg["endpoints"][0]["alert_threshold"], 4)


class FlagTests(unittest.TestCase):
    def test_parse_ok(self):
        self.assertEqual(parse_alert_threshold("api=5"), ("api", 5))

    def test_parse_rejects_bad_specs(self):
        for bad in ("api", "api=", "=5", "api=0", "api=-1",
                    "api=2.5", "api=five"):
            with self.assertRaises(ValueError, msg=bad):
                parse_alert_threshold(bad)

    def test_flag_wins_over_config(self):
        cfg = {"endpoints": [{"name": "api", "alert_threshold": 2}]}
        apply_alert_threshold_overrides(
            cfg, _args(alert_threshold=["api=5"]))
        self.assertEqual(cfg["endpoints"][0]["alert_threshold"], 5)

    def test_flag_sets_an_unset_endpoint(self):
        cfg = {"endpoints": [{"name": "api"}]}
        apply_alert_threshold_overrides(
            cfg, _args(alert_threshold=["api=4"]))
        self.assertEqual(cfg["endpoints"][0]["alert_threshold"], 4)

    def test_flag_naming_unknown_endpoint_is_rejected(self):
        cfg = {"endpoints": [{"name": "api"}]}
        with self.assertRaises(ValueError) as ctx:
            apply_alert_threshold_overrides(
                cfg, _args(alert_threshold=["nope=4"]))
        self.assertIn("nope", str(ctx.exception))

    def test_no_flag_changes_nothing(self):
        cfg = {"endpoints": [{"name": "api", "alert_threshold": 2}]}
        apply_alert_threshold_overrides(cfg, _args())
        self.assertEqual(cfg["endpoints"][0]["alert_threshold"], 2)


class AlertingTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = alerts.post_alert
        alerts.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts.post_alert = self._real_post

    def _alerter(self):
        return WebhookAlerter("http://example.invalid/hook")

    def test_alert_waits_for_alert_threshold(self):
        # Flap 3 flips the endpoint DOWN on round 3, but with
        # alert_threshold 5 nothing pages until round 5.
        endpoints = [dict(_closed()[0], alert_threshold=5)]
        history = History()
        detector = FlapDetector()  # default flap threshold 3
        alerter = self._alerter()
        for round_no in range(5):
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
            if round_no < 4:
                self.assertEqual(self.sent, [],
                                 f"alert fired on round {round_no + 1}")
        self.assertEqual(len(self.sent), 1)
        url, payload = self.sent[0]
        self.assertEqual(payload["endpoint"], "closed")
        self.assertEqual(payload["event"], "down")
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["previous_ok"])
        # The endpoint was DOWN from round 3 on, even though the
        # alert only fired on round 5.
        self.assertEqual(history.consecutive_failures("closed"), 5)
        # And it does not re-fire while still down.
        check_all(endpoints, history, alerter=alerter,
                  flap_detector=detector)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_before_alerting_pages_nothing(self):
        # Three failures confirm the outage (flap 3) but never
        # reach alert_threshold 5; a success then resets the run
        # with no down alert and no recovery alert either.
        endpoints = [dict(_closed()[0], alert_threshold=5)]
        history = History()
        detector = FlapDetector()
        alerter = self._alerter()
        for _ in range(3):
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
        self.assertEqual(self.sent, [])
        # The endpoint is DOWN in history (confirmed at flap 3).
        self.assertFalse(history.current("closed")["ok"])
        self.assertFalse(history.current("closed")["unconfirmed"])
        # Now it recovers: the alerter never saw it go down, so
        # there is no transition to recover from.
        alerter.notify(CheckResult(
            name="closed", ok=True, latency_ms=1.0,
            checked_at=1_700_000_000.0))
        self.assertEqual(self.sent, [])

    def test_recovery_after_alerting_still_alerts(self):
        endpoints = [dict(_closed()[0], alert_threshold=5)]
        history = History()
        detector = FlapDetector()
        alerter = self._alerter()
        for _ in range(5):
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["event"], "down")
        # Recover with a real check: start a server on a free port
        # and point the endpoint at it.
        import socket
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _OkHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        endpoints[0]["port"] = port
        server = HTTPServer(("127.0.0.1", port), _OkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[1][1]["event"], "recovered")
        self.assertTrue(self.sent[1][1]["ok"])

    def test_unset_alert_threshold_alerts_at_confirmation(self):
        # No alert_threshold: the down alert fires on the third
        # failure, like before.
        history = History()
        detector = FlapDetector()
        alerter = self._alerter()
        for round_no in range(3):
            check_all(_closed(), history, alerter=alerter,
                      flap_detector=detector)
            if round_no < 2:
                self.assertEqual(self.sent, [])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["event"], "down")

    def test_alert_threshold_below_flap_alerts_at_confirmation(self):
        # alert_threshold 1 cannot page before the outage is
        # confirmed: the alert fires with the confirmation, on
        # the third failure.
        endpoints = [dict(_closed()[0], alert_threshold=1)]
        history = History()
        detector = FlapDetector()
        alerter = self._alerter()
        for round_no in range(3):
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
            if round_no < 2:
                self.assertEqual(self.sent, [])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["event"], "down")


class ApiAndDashboardTests(unittest.TestCase):
    def test_snapshot_carries_alert_threshold(self):
        endpoints = [
            {"name": "noisy", "type": "tcp", "host": "127.0.0.1",
             "port": 1, "alert_threshold": 5},
            {"name": "plain", "type": "tcp", "host": "127.0.0.1",
             "port": 1},
        ]
        snap = status_snapshot(History(), endpoints)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["noisy"]["alert_threshold"], 5)
        # No alert_threshold set: the default flap threshold, 3.
        self.assertEqual(by_name["plain"]["alert_threshold"], 3)

    def test_dashboard_shows_alert_at_column(self):
        endpoints = [
            {"name": "noisy", "type": "tcp", "host": "127.0.0.1",
             "port": 1, "alert_threshold": 5},
            {"name": "plain", "type": "tcp", "host": "127.0.0.1",
             "port": 1},
        ]
        page = render_dashboard(History(), endpoints)
        self.assertIn("<th>Alert at</th>", page)
        self.assertIn("<td>5</td>", page)
        self.assertIn("<td>3</td>", page)


class ReloadTests(unittest.TestCase):
    def test_loop_reloads_alert_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"interval_seconds": 1,
                           "endpoints": [_closed("a")[0]]}, fh)
            cfg = load_config(path)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"interval_seconds": 1,
                           "flap_threshold": 2,
                           "endpoints": [
                               dict(_closed("a")[0],
                                    alert_threshold=5),
                               _closed("b")[0]]}, fh)
            history = History()
            stop = threading.Event()
            event = threading.Event()
            event.set()  # reload before the first round
            final = monitor_loop(cfg, history, None, _args(), stop,
                                 config_path=path,
                                 reload_requested=event,
                                 max_rounds=1)
            by_name = {ep["name"]: ep for ep in final["endpoints"]}
            # The explicit value survives the reload; the endpoint
            # without one resolves to the reloaded flap threshold.
            self.assertEqual(by_name["a"]["alert_threshold"], 5)
            self.assertEqual(by_name["b"]["alert_threshold"], 2)


if __name__ == "__main__":
    unittest.main()
