"""Tests for per-endpoint p95 latency alerts
(latency_p95_threshold_ms)."""

import json
import os
import tempfile
import time
import types
import unittest

from healthcheck.checker import CheckResult
from healthcheck.cli import (apply_latency_p95_threshold_overrides,
                             load_config, parse_latency_p95_threshold,
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


if __name__ == "__main__":
    unittest.main()
