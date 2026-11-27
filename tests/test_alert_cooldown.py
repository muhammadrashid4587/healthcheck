"""Tests for the per-endpoint alert cooldown / re-alert interval.

`alert_cooldown_s` is how many seconds must pass between pages for
the same outage: an endpoint that stays down pages again once the
cooldown has elapsed since its last down alert, so a long outage
re-pages instead of going quiet after the first page. The first
page of an outage is immediate — the cooldown only spaces repeats
of the same outage — and a recovery clears the cooldown state, so
the next outage pages immediately. An endpoint without one alerts
exactly once per outage, like before.
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
from healthcheck.cli import (apply_alert_cooldown_overrides, check_all,
                             endpoint_alert_cooldown, load_config,
                             monitor_loop, parse_alert_cooldown,
                             resolve_alert_cooldowns)
from healthcheck.flap import FlapDetector
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History


def _args(**overrides):
    args = types.SimpleNamespace(
        history_file=None, webhook_url=None, flap_threshold=None,
        alert_threshold=None, alert_cooldown=None)
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


def _result(name, ok, latency=12.3, status=None, error=None,
            at=1_700_000_000.0):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       status_code=status, error=error, checked_at=at)


class ValidationTests(unittest.TestCase):
    def _load(self, endpoints, extra=None):
        cfg = {"endpoints": endpoints}
        if extra:
            cfg.update(extra)
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, cfg)
            return load_config(path)

    def test_accepts_a_positive_integer(self):
        cfg = self._load([dict(_closed()[0], alert_cooldown_s=300)])
        self.assertEqual(cfg["endpoints"][0]["alert_cooldown_s"], 300)

    def test_accepts_a_positive_float(self):
        cfg = self._load([dict(_closed()[0], alert_cooldown_s=2.5)])
        self.assertEqual(cfg["endpoints"][0]["alert_cooldown_s"], 2.5)

    def test_accepts_a_global_value(self):
        cfg = self._load(_closed(), {"alert_cooldown_s": 600})
        self.assertEqual(cfg["alert_cooldown_s"], 600)

    def test_rejects_zero(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(
                d, {"endpoints": [dict(_closed()[0],
                                       alert_cooldown_s=0)]})
            with self.assertRaises(ValueError) as ctx:
                load_config(path)
            self.assertIn("alert_cooldown_s", str(ctx.exception))

    def test_rejects_negative(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(
                d, {"endpoints": [dict(_closed()[0],
                                       alert_cooldown_s=-5)]})
            with self.assertRaises(ValueError) as ctx:
                load_config(path)
            self.assertIn("alert_cooldown_s", str(ctx.exception))

    def test_rejects_string_and_bool(self):
        for bad in ("300", True):
            with tempfile.TemporaryDirectory() as d:
                path = _write_config(
                    d, {"endpoints": [dict(_closed()[0],
                                           alert_cooldown_s=bad)]})
                with self.assertRaises(ValueError, msg=repr(bad)):
                    load_config(path)

    def test_rejects_bad_global_value(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(
                d, {"alert_cooldown_s": 0, "endpoints": _closed()})
            with self.assertRaises(ValueError) as ctx:
                load_config(path)
            self.assertIn("alert_cooldown_s", str(ctx.exception))

    def test_absent_is_fine(self):
        cfg = self._load(_closed())
        self.assertNotIn("alert_cooldown_s", cfg["endpoints"][0])


class ResolutionTests(unittest.TestCase):
    def test_explicit_value_wins(self):
        cfg = {"alert_cooldown_s": 600}
        self.assertEqual(
            endpoint_alert_cooldown(cfg, {"name": "a",
                                          "alert_cooldown_s": 300}),
            300)

    def test_unset_falls_back_to_global(self):
        cfg = {"alert_cooldown_s": 600}
        self.assertEqual(
            endpoint_alert_cooldown(cfg, {"name": "a"}), 600)

    def test_unset_without_global_is_none(self):
        self.assertIsNone(endpoint_alert_cooldown({}, {"name": "a"}))

    def test_resolve_fills_unset_from_global(self):
        cfg = {"alert_cooldown_s": 600,
               "endpoints": [{"name": "a"},
                             {"name": "b", "alert_cooldown_s": 300}]}
        resolve_alert_cooldowns(cfg, _args())
        by_name = {ep["name"]: ep for ep in cfg["endpoints"]}
        self.assertEqual(by_name["a"]["alert_cooldown_s"], 600)
        self.assertEqual(by_name["b"]["alert_cooldown_s"], 300)

    def test_resolve_without_global_leaves_none(self):
        cfg = {"endpoints": [{"name": "a"}]}
        resolve_alert_cooldowns(cfg, _args())
        self.assertIsNone(cfg["endpoints"][0]["alert_cooldown_s"])


class FlagTests(unittest.TestCase):
    def test_parse_ok(self):
        self.assertEqual(parse_alert_cooldown("api=300"), ("api", 300.0))

    def test_parse_ok_float(self):
        self.assertEqual(parse_alert_cooldown("api=2.5"), ("api", 2.5))

    def test_parse_rejects_bad_specs(self):
        for bad in ("api", "api=", "=300", "api=0", "api=-1",
                    "api=five"):
            with self.assertRaises(ValueError, msg=bad):
                parse_alert_cooldown(bad)

    def test_flag_wins_over_config(self):
        cfg = {"endpoints": [{"name": "api", "alert_cooldown_s": 60}]}
        apply_alert_cooldown_overrides(
            cfg, _args(alert_cooldown=["api=300"]))
        self.assertEqual(cfg["endpoints"][0]["alert_cooldown_s"], 300.0)

    def test_flag_sets_an_unset_endpoint(self):
        cfg = {"endpoints": [{"name": "api"}]}
        apply_alert_cooldown_overrides(
            cfg, _args(alert_cooldown=["api=300"]))
        self.assertEqual(cfg["endpoints"][0]["alert_cooldown_s"], 300.0)

    def test_flag_naming_unknown_endpoint_is_rejected(self):
        cfg = {"endpoints": [{"name": "api"}]}
        with self.assertRaises(ValueError) as ctx:
            apply_alert_cooldown_overrides(
                cfg, _args(alert_cooldown=["nope=300"]))
        self.assertIn("nope", str(ctx.exception))

    def test_no_flag_changes_nothing(self):
        cfg = {"endpoints": [{"name": "api", "alert_cooldown_s": 60}]}
        apply_alert_cooldown_overrides(cfg, _args())
        self.assertEqual(cfg["endpoints"][0]["alert_cooldown_s"], 60)


class AlerterTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = alerts.post_alert
        alerts.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts.post_alert = self._real_post

    def _alerter(self, records=None):
        return WebhookAlerter("http://example.invalid/hook",
                              records=records)

    def test_no_cooldown_never_repeats(self):
        alerter = self._alerter()
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1010.0))
        self.assertEqual(len(self.sent), 1)
        # Still down, far later: no repeat without a cooldown.
        self.assertIsNone(
            alerter.notify(_result("api", False, at=10000.0)))
        self.assertEqual(len(self.sent), 1)

    def test_repeat_fires_once_cooldown_elapses(self):
        alerter = self._alerter()
        alerter.notify(_result("api", True, at=1000.0))
        payload = alerter.notify(_result("api", False, at=1010.0),
                                 cooldown_s=300)
        # The first page is not a repeat.
        self.assertNotIn("repeat", payload)
        self.assertEqual(len(self.sent), 1)
        # Inside the cooldown: nothing.
        self.assertIsNone(
            alerter.notify(_result("api", False, at=1200.0),
                           cooldown_s=300))
        self.assertEqual(len(self.sent), 1)
        # At exactly the cooldown: the repeat pages.
        payload = alerter.notify(_result("api", False, at=1310.0),
                                 cooldown_s=300)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(payload["event"], "down")
        self.assertTrue(payload["repeat"])
        self.assertFalse(payload["previous_ok"])
        self.assertFalse(payload["ok"])
        # The repeat resets the clock: 100 s later, nothing.
        self.assertIsNone(
            alerter.notify(_result("api", False, at=1410.0),
                           cooldown_s=300))
        self.assertEqual(len(self.sent), 2)
        # A full cooldown after the repeat: it pages again.
        payload = alerter.notify(_result("api", False, at=1610.0),
                                 cooldown_s=300)
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(payload["repeat"])

    def test_recovery_clears_the_cooldown(self):
        alerter = self._alerter()
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1010.0),
                       cooldown_s=300)
        alerter.notify(_result("api", False, at=1310.0),
                       cooldown_s=300)
        self.assertEqual(len(self.sent), 2)
        # Recovery pages, and the cooldown state is gone.
        payload = alerter.notify(_result("api", True, at=1400.0),
                                 cooldown_s=300)
        self.assertEqual(payload["event"], "recovered")
        self.assertEqual(len(self.sent), 3)
        self.assertIsNone(alerter.last_alerted_at("api"))
        # A fresh outage pages at once — the old cooldown does
        # not carry over the recovery.
        payload = alerter.notify(_result("api", False, at=1410.0),
                                 cooldown_s=300)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(payload["event"], "down")
        self.assertNotIn("repeat", payload)

    def test_records_track_the_last_page(self):
        records = {}
        alerter = self._alerter(records=records)
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1010.0),
                       cooldown_s=300)
        self.assertEqual(records, {"api": {"alerted_at": 1010.0}})
        self.assertEqual(alerter.last_alerted_at("api"), 1010.0)
        # The repeat updates the record.
        alerter.notify(_result("api", False, at=1310.0),
                       cooldown_s=300)
        self.assertEqual(records, {"api": {"alerted_at": 1310.0}})
        # Recovery deletes it.
        alerter.notify(_result("api", True, at=1400.0),
                       cooldown_s=300)
        self.assertEqual(records, {})

    def test_no_url_records_nothing(self):
        # No webhook anywhere: nothing goes out, so there is no
        # last alert for the cooldown to count from.
        records = {}
        alerter = WebhookAlerter(None, records=records)
        alerter.notify(_result("api", True, at=1000.0))
        self.assertIsNone(
            alerter.notify(_result("api", False, at=1010.0),
                           cooldown_s=300))
        self.assertEqual(records, {})
        self.assertIsNone(alerter.last_alerted_at("api"))
        # And a still-down check cannot repeat what never paged.
        self.assertIsNone(
            alerter.notify(_result("api", False, at=2000.0),
                           cooldown_s=300))

    def test_endpoints_repeat_independently(self):
        alerter = self._alerter()
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("db", True, at=1000.0))
        alerter.notify(_result("api", False, at=1010.0),
                       cooldown_s=300)
        alerter.notify(_result("db", False, at=1010.0),
                       cooldown_s=60)
        self.assertEqual(len(self.sent), 2)
        # db's shorter cooldown elapses first.
        alerter.notify(_result("api", False, at=1080.0),
                       cooldown_s=300)
        payload = alerter.notify(_result("db", False, at=1080.0),
                                 cooldown_s=60)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(payload["endpoint"], "db")
        self.assertTrue(payload["repeat"])


class CheckAllTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = alerts.post_alert
        alerts.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts.post_alert = self._real_post

    def _alerter(self):
        return WebhookAlerter("http://example.invalid/hook")

    def test_long_cooldown_suppresses_repeats(self):
        # Flap 3 pages on round 3; with a 3600 s cooldown the
        # fourth round, seconds later, does not re-page.
        endpoints = [dict(_closed()[0], alert_cooldown_s=3600)]
        history = History()
        detector = FlapDetector()
        alerter = self._alerter()
        for _ in range(4):
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["event"], "down")

    def test_tiny_cooldown_repeats_while_still_down(self):
        # A 0.1 ms cooldown is already elapsed by the next round
        # (a round takes far longer than that), so the still-down
        # endpoint pages again on round 4.
        endpoints = [dict(_closed()[0], alert_cooldown_s=0.0001)]
        history = History()
        detector = FlapDetector()
        alerter = self._alerter()
        for _ in range(4):
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.sent[0][1]["event"], "down")
        self.assertNotIn("repeat", self.sent[0][1])
        self.assertEqual(self.sent[1][1]["event"], "down")
        self.assertTrue(self.sent[1][1]["repeat"])


class ApiAndDashboardTests(unittest.TestCase):
    def test_snapshot_carries_cooldown_and_countdown(self):
        endpoints = [
            {"name": "noisy", "type": "tcp", "host": "127.0.0.1",
             "port": 1, "alert_cooldown_s": 300},
            {"name": "plain", "type": "tcp", "host": "127.0.0.1",
             "port": 1},
        ]
        history = History()
        # "noisy" is DOWN (a failed check recorded directly) and
        # paged at 1000; at 1100 it has 200 s of cooldown left.
        history.record(_result("noisy", False, at=1000.0))
        records = {"noisy": {"alerted_at": 1000.0}}
        snap = status_snapshot(history, endpoints, now=1100.0,
                               alert_records=records)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["noisy"]["alert_cooldown_s"], 300)
        self.assertEqual(by_name["noisy"]["last_alerted_at"], 1000.0)
        self.assertEqual(
            by_name["noisy"]["alert_cooldown_remaining_s"], 200.0)
        # No cooldown, never paged: nulls across the board.
        self.assertIsNone(by_name["plain"]["alert_cooldown_s"])
        self.assertIsNone(by_name["plain"]["last_alerted_at"])
        self.assertIsNone(
            by_name["plain"]["alert_cooldown_remaining_s"])

    def test_snapshot_countdown_is_zero_when_elapsed(self):
        endpoints = [{"name": "noisy", "type": "tcp",
                      "host": "127.0.0.1", "port": 1,
                      "alert_cooldown_s": 300}]
        history = History()
        history.record(_result("noisy", False, at=1000.0))
        records = {"noisy": {"alerted_at": 1000.0}}
        snap = status_snapshot(history, endpoints, now=1400.0,
                               alert_records=records)
        self.assertEqual(
            snap["endpoints"][0]["alert_cooldown_remaining_s"], 0.0)

    def test_snapshot_countdown_clears_on_recovery(self):
        # A recovery deletes the record, so even with a cooldown
        # configured there is no pending re-alert to count down.
        endpoints = [{"name": "noisy", "type": "tcp",
                      "host": "127.0.0.1", "port": 1,
                      "alert_cooldown_s": 300}]
        history = History()
        history.record(_result("noisy", True, at=1400.0))
        snap = status_snapshot(history, endpoints, now=1400.0,
                               alert_records={})
        row = snap["endpoints"][0]
        self.assertEqual(row["status"], "UP")
        self.assertIsNone(row["last_alerted_at"])
        self.assertIsNone(row["alert_cooldown_remaining_s"])

    def test_dashboard_shows_cooldown_column(self):
        endpoints = [
            {"name": "noisy", "type": "tcp", "host": "127.0.0.1",
             "port": 1, "alert_cooldown_s": 300},
            {"name": "plain", "type": "tcp", "host": "127.0.0.1",
             "port": 1},
        ]
        page = render_dashboard(History(), endpoints)
        self.assertIn("<th>Cooldown</th>", page)
        self.assertIn("<td>300 s</td>", page)
        self.assertIn("<td>-</td>", page)

    def test_dashboard_shows_realert_countdown(self):
        endpoints = [{"name": "noisy", "type": "tcp",
                      "host": "127.0.0.1", "port": 1,
                      "alert_cooldown_s": 300}]
        history = History()
        history.record(_result("noisy", False, at=1000.0))
        records = {"noisy": {"alerted_at": 1000.0}}
        page = render_dashboard(history, endpoints, now=1100.0,
                                alert_records=records)
        self.assertIn("300 s (re-alert in 3m 20s)", page)

    def test_dashboard_shows_realert_due(self):
        endpoints = [{"name": "noisy", "type": "tcp",
                      "host": "127.0.0.1", "port": 1,
                      "alert_cooldown_s": 300}]
        history = History()
        history.record(_result("noisy", False, at=1000.0))
        records = {"noisy": {"alerted_at": 1000.0}}
        page = render_dashboard(history, endpoints, now=1400.0,
                                alert_records=records)
        self.assertIn("300 s (re-alert due)", page)


class ReloadTests(unittest.TestCase):
    def test_loop_reloads_alert_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"interval_seconds": 1,
                           "endpoints": [_closed("a")[0]]}, fh)
            cfg = load_config(path)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"interval_seconds": 1,
                           "alert_cooldown_s": 600,
                           "endpoints": [
                               dict(_closed("a")[0],
                                    alert_cooldown_s=300),
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
            # without one resolves to the reloaded global.
            self.assertEqual(by_name["a"]["alert_cooldown_s"], 300)
            self.assertEqual(by_name["b"]["alert_cooldown_s"], 600)


if __name__ == "__main__":
    unittest.main()
