"""Tests for flap detection: alert only after N consecutive failures."""

import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import healthcheck.alerts as alerts
from healthcheck.alerts import WebhookAlerter
from healthcheck.checker import CheckResult
from healthcheck.cli import check_all, flap_threshold, load_config
from healthcheck.flap import FlapDetector, validate_threshold
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


def _result(name, ok, latency=10.0, at=1_700_000_000.0, unconfirmed=False):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at, unconfirmed=unconfirmed)


def _write_config(directory, cfg):
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return path


class DetectorTests(unittest.TestCase):
    def test_default_threshold_is_3(self):
        self.assertEqual(FlapDetector().threshold, 3)

    def test_single_failure_is_unconfirmed(self):
        outcome = FlapDetector().observe("api", False)
        self.assertFalse(outcome.confirmed_down)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.consecutive_failures, 1)

    def test_second_failure_still_unconfirmed(self):
        detector = FlapDetector()
        detector.observe("api", False)
        outcome = detector.observe("api", False)
        self.assertFalse(outcome.confirmed_down)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.consecutive_failures, 2)

    def test_nth_failure_confirms_and_reports_change(self):
        detector = FlapDetector()
        detector.observe("api", False)
        detector.observe("api", False)
        outcome = detector.observe("api", False)
        self.assertTrue(outcome.confirmed_down)
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.consecutive_failures, 3)

    def test_further_failures_stay_down_without_repeat_change(self):
        detector = FlapDetector()
        for _ in range(3):
            detector.observe("api", False)
        outcome = detector.observe("api", False)
        self.assertTrue(outcome.confirmed_down)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.consecutive_failures, 4)

    def test_success_resets_the_counter(self):
        detector = FlapDetector()
        detector.observe("api", False)
        detector.observe("api", False)
        outcome = detector.observe("api", True)
        self.assertFalse(outcome.confirmed_down)
        self.assertFalse(outcome.changed)  # was never confirmed down
        self.assertEqual(outcome.consecutive_failures, 0)
        # A fresh run of failures starts from zero again.
        self.assertFalse(detector.observe("api", False).confirmed_down)
        self.assertFalse(detector.observe("api", False).confirmed_down)

    def test_success_after_confirmed_down_is_a_recovery(self):
        detector = FlapDetector()
        for _ in range(3):
            detector.observe("api", False)
        outcome = detector.observe("api", True)
        self.assertFalse(outcome.confirmed_down)
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.consecutive_failures, 0)
        self.assertFalse(detector.is_down("api"))

    def test_threshold_1_confirms_on_first_failure(self):
        outcome = FlapDetector(threshold=1).observe("api", False)
        self.assertTrue(outcome.confirmed_down)
        self.assertTrue(outcome.changed)

    def test_custom_threshold(self):
        detector = FlapDetector(threshold=2)
        self.assertFalse(detector.observe("api", False).confirmed_down)
        self.assertTrue(detector.observe("api", False).confirmed_down)

    def test_endpoints_tracked_independently(self):
        detector = FlapDetector()
        for _ in range(3):
            detector.observe("api", False)
        self.assertTrue(detector.is_down("api"))
        self.assertFalse(detector.is_down("db"))
        self.assertFalse(detector.observe("db", False).confirmed_down)
        self.assertEqual(detector.consecutive_failures("api"), 3)
        self.assertEqual(detector.consecutive_failures("db"), 1)
        self.assertEqual(detector.consecutive_failures("never-seen"), 0)

    def test_never_observed_is_not_down(self):
        self.assertFalse(FlapDetector().is_down("api"))


class ThresholdValidationTests(unittest.TestCase):
    def test_zero_rejected(self):
        with self.assertRaises(ValueError):
            validate_threshold(0)

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            validate_threshold(-2)

    def test_float_rejected(self):
        with self.assertRaises(ValueError):
            validate_threshold(2.5)

    def test_string_rejected(self):
        with self.assertRaises(ValueError):
            validate_threshold("3")

    def test_bool_rejected(self):
        # True is an int subclass; it is not a real threshold.
        with self.assertRaises(ValueError):
            validate_threshold(True)

    def test_none_rejected(self):
        with self.assertRaises(ValueError):
            validate_threshold(None)

    def test_positive_ints_accepted(self):
        self.assertEqual(validate_threshold(1), 1)
        self.assertEqual(validate_threshold(5), 5)

    def test_detector_rejects_bad_threshold(self):
        with self.assertRaises(ValueError):
            FlapDetector(threshold=0)


class ConfigValidationTests(unittest.TestCase):
    def _endpoints(self):
        return [{"name": "closed", "type": "tcp", "host": "127.0.0.1",
                 "port": 1}]

    def test_zero_threshold_rejected_at_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, {"flap_threshold": 0,
                                      "endpoints": self._endpoints()})
            with self.assertRaises(ValueError) as ctx:
                load_config(path)
        self.assertIn("flap_threshold", str(ctx.exception))

    def test_string_threshold_rejected_at_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, {"flap_threshold": "3",
                                      "endpoints": self._endpoints()})
            with self.assertRaises(ValueError):
                load_config(path)

    def test_valid_threshold_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, {"flap_threshold": 2,
                                      "endpoints": self._endpoints()})
            cfg = load_config(path)
        self.assertEqual(cfg["flap_threshold"], 2)

    def test_no_threshold_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, {"endpoints": self._endpoints()})
            cfg = load_config(path)
        self.assertNotIn("flap_threshold", cfg)


class FlapThresholdHelperTests(unittest.TestCase):
    class _Args:
        def __init__(self, flap_threshold=None):
            self.flap_threshold = flap_threshold

    def test_flag_beats_config(self):
        cfg = {"flap_threshold": 2}
        self.assertEqual(flap_threshold(cfg, self._Args(5)), 5)

    def test_config_used_when_no_flag(self):
        cfg = {"flap_threshold": 2}
        self.assertEqual(flap_threshold(cfg, self._Args(None)), 2)

    def test_default_3_when_neither_set(self):
        self.assertEqual(flap_threshold({}, self._Args(None)), 3)

    def test_bad_flag_value_rejected(self):
        with self.assertRaises(ValueError):
            flap_threshold({}, self._Args(0))

    def test_bad_config_value_rejected(self):
        with self.assertRaises(ValueError):
            flap_threshold({"flap_threshold": -1}, self._Args(None))


class CheckAllFlapTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = alerts.post_alert
        alerts.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts.post_alert = self._real_post

    def _alerter(self):
        return WebhookAlerter("http://example.invalid/hook")

    def _closed(self):
        # Port 1 on localhost refuses connections deterministically.
        return [{"name": "closed", "type": "tcp", "host": "127.0.0.1",
                 "port": 1, "timeout": 1}]

    def test_single_failure_no_alert(self):
        history = History()
        check_all(self._closed(), history, alerter=self._alerter())
        self.assertEqual(self.sent, [])
        entry = history.current("closed")
        self.assertFalse(entry["ok"])
        self.assertTrue(entry["unconfirmed"])
        # A lone blip is not downtime: no uptime figure at all.
        self.assertIsNone(history.uptime_pct("closed"))

    def test_n_consecutive_failures_alert_once(self):
        history = History()
        detector = FlapDetector()  # default threshold 3
        alerter = self._alerter()
        for round_no in range(3):
            check_all(self._closed(), history, alerter=alerter,
                      flap_detector=detector)
            if round_no < 2:
                self.assertEqual(self.sent, [],
                                 f"alert fired on round {round_no + 1}")
        self.assertEqual(len(self.sent), 1)
        url, payload = self.sent[0]
        self.assertEqual(payload["endpoint"], "closed")
        self.assertEqual(payload["event"], "down")
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["previous_ok"])
        # The first two failures are unconfirmed; only the third
        # counts against uptime.
        entries = history.recent("closed", 3)
        self.assertTrue(entries[0]["unconfirmed"])
        self.assertTrue(entries[1]["unconfirmed"])
        self.assertFalse(entries[2]["unconfirmed"])
        self.assertEqual(history.uptime_pct("closed"), 0.0)

    def test_no_repeat_alert_while_still_down(self):
        history = History()
        detector = FlapDetector()
        alerter = self._alerter()
        for _ in range(5):
            check_all(self._closed(), history, alerter=alerter,
                      flap_detector=detector)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_alert_after_success_resets_counter(self):
        port = _free_port()
        endpoints = [{"name": "svc", "type": "tcp", "host": "127.0.0.1",
                      "port": port, "timeout": 1}]
        history = History()
        detector = FlapDetector()
        alerter = self._alerter()
        for _ in range(3):
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["event"], "down")
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
        self.assertFalse(self.sent[1][1]["previous_ok"])
        # The counter reset: one more failure after recovery is a
        # blip again, not a second outage.
        check_all(endpoints, history, alerter=alerter,
                  flap_detector=detector)
        self.assertEqual(len(self.sent), 2)

    def test_success_between_failures_resets_counter(self):
        port = _free_port()
        endpoints = [{"name": "svc", "type": "tcp", "host": "127.0.0.1",
                      "port": port, "timeout": 1}]
        history = History()
        detector = FlapDetector()
        alerter = self._alerter()
        server = HTTPServer(("127.0.0.1", port), _OkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)  # up
        finally:
            server.shutdown()
            server.server_close()
        # Two failures, then up again (counter resets), then one
        # failure: never three in a row, so no alert at all.
        check_all(endpoints, history, alerter=alerter,
                  flap_detector=detector)
        check_all(endpoints, history, alerter=alerter,
                  flap_detector=detector)
        server = HTTPServer(("127.0.0.1", port), _OkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)  # up: resets counter
        finally:
            server.shutdown()
            server.server_close()
        check_all(endpoints, history, alerter=alerter,
                  flap_detector=detector)  # one failure
        self.assertEqual(self.sent, [])

    def test_custom_threshold_via_detector(self):
        history = History()
        detector = FlapDetector(threshold=2)
        alerter = self._alerter()
        check_all(self._closed(), history, alerter=alerter,
                  flap_detector=detector)
        self.assertEqual(self.sent, [])
        check_all(self._closed(), history, alerter=alerter,
                  flap_detector=detector)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["event"], "down")


class UptimeExcludesUnconfirmedTests(unittest.TestCase):
    def test_unconfirmed_failures_do_not_count(self):
        history = History()
        history.record(_result("api", True, at=100.0))
        history.record(_result("api", False, at=200.0, unconfirmed=True))
        self.assertEqual(history.uptime_pct("api"), 100.0)
        self.assertEqual(
            history.uptime_pct_window("api", 1000, now=300.0), 100.0)

    def test_confirmed_failure_counts(self):
        history = History()
        history.record(_result("api", True, at=100.0))
        history.record(_result("api", False, at=200.0))
        self.assertEqual(history.uptime_pct("api"), 50.0)

    def test_only_unconfirmed_is_no_data(self):
        history = History()
        history.record(_result("api", False, at=100.0, unconfirmed=True))
        self.assertIsNone(history.uptime_pct("api"))
        self.assertIsNone(
            history.uptime_pct_window("api", 1000, now=200.0))

    def test_old_history_without_the_key_still_counts(self):
        # Entries written before flap detection have no
        # "unconfirmed" key; they count exactly as before.
        history = History()
        history.record(_result("api", True, at=100.0))
        history.record(_result("api", False, at=200.0))
        del history._checks["api"][1]["unconfirmed"]
        self.assertEqual(history.uptime_pct("api"), 50.0)


class DependencyFlapTests(unittest.TestCase):
    def test_dependent_still_checked_on_unconfirmed_dependency_failure(self):
        db_port = _free_port()
        api_port = _free_port()
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": db_port, "timeout": 1},
            {"name": "api", "type": "tcp", "host": "127.0.0.1",
             "port": api_port, "timeout": 1, "depends_on": "db"},
        ]
        history = History()
        check_all(endpoints, history)  # one failure each: db unconfirmed
        self.assertTrue(history.current("db")["unconfirmed"])
        api_entry = history.current("api")
        self.assertFalse(api_entry["skipped"])  # db not confirmed down
        self.assertFalse(api_entry["ok"])       # api really was checked

    def test_dependent_skipped_once_dependency_confirmed_down(self):
        db_port = _free_port()
        api_port = _free_port()
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": db_port, "timeout": 1},
            {"name": "api", "type": "tcp", "host": "127.0.0.1",
             "port": api_port, "timeout": 1, "depends_on": "db"},
        ]
        history = History()
        detector = FlapDetector(threshold=1)
        check_all(endpoints, history, flap_detector=detector)
        self.assertTrue(history.current("api")["skipped"])


class DashboardFlapTests(unittest.TestCase):
    def test_unconfirmed_failure_still_shows_up(self):
        history = History()
        history.record(_result("api", False, at=1_700_000_000.0,
                               unconfirmed=True))
        endpoints = [{"name": "api", "type": "http",
                      "url": "http://127.0.0.1:8000"}]
        page = render_dashboard(history, endpoints)
        self.assertIn('<td class="up">UP</td>', page)
        self.assertNotIn('<td class="down">DOWN</td>', page)

    def test_confirmed_failure_shows_down(self):
        history = History()
        history.record(_result("api", False, at=1_700_000_000.0))
        endpoints = [{"name": "api", "type": "http",
                      "url": "http://127.0.0.1:8000"}]
        page = render_dashboard(history, endpoints)
        self.assertIn('<td class="down">DOWN</td>', page)


if __name__ == "__main__":
    unittest.main()
