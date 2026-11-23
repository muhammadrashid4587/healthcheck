"""Tests for per-endpoint check intervals (check_interval_s)."""

import json
import os
import socket
import tempfile
import threading
import types
import unittest

from healthcheck.cli import (apply_check_interval_overrides, check_all,
                             endpoint_interval, load_config, main,
                             monitor_loop, parse_check_interval,
                             reload_config)
from healthcheck.flap import FlapDetector
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History


def _tcp(name, port=1):
    return {"name": name, "type": "tcp", "host": "127.0.0.1",
            "port": port, "timeout": 1}


def _args():
    return types.SimpleNamespace(history_file=None, webhook_url=None,
                                 flap_threshold=None)


class ConfigValidationTests(unittest.TestCase):
    def _load(self, endpoints, extra=None):
        cfg = {"endpoints": endpoints}
        if extra:
            cfg.update(extra)
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        try:
            return load_config(path)
        finally:
            os.unlink(path)

    def test_valid_interval_loads(self):
        cfg = self._load([dict(_tcp("web"), check_interval_s=60)])
        self.assertEqual(cfg["endpoints"][0]["check_interval_s"], 60)

    def test_fractional_interval_loads(self):
        cfg = self._load([dict(_tcp("web"), check_interval_s=2.5)])
        self.assertEqual(cfg["endpoints"][0]["check_interval_s"], 2.5)

    def test_interval_allowed_on_http_too(self):
        cfg = self._load([{
            "name": "web", "type": "http",
            "url": "http://127.0.0.1:1/", "check_interval_s": 120,
        }])
        self.assertEqual(cfg["endpoints"][0]["check_interval_s"], 120)

    def test_bad_intervals_rejected(self):
        for bad in (0, 0.0, -30, "60", True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._load([dict(_tcp("web"), check_interval_s=bad)])

    def test_endpoint_interval_prefers_endpoint(self):
        cfg = {"interval_seconds": 30,
               "endpoints": [dict(_tcp("web"), check_interval_s=60)]}
        self.assertEqual(endpoint_interval(cfg, cfg["endpoints"][0]), 60)

    def test_endpoint_interval_falls_back_to_global(self):
        cfg = {"interval_seconds": 45, "endpoints": [_tcp("web")]}
        self.assertEqual(endpoint_interval(cfg, cfg["endpoints"][0]), 45)

    def test_endpoint_interval_defaults_to_30(self):
        cfg = {"endpoints": [_tcp("web")]}
        self.assertEqual(endpoint_interval(cfg, cfg["endpoints"][0]), 30)


class CliOverrideTests(unittest.TestCase):
    def test_parse_valid_specs(self):
        self.assertEqual(parse_check_interval("web=60"), ("web", 60.0))
        self.assertEqual(parse_check_interval("web=2.5"), ("web", 2.5))

    def test_parse_rejects_bad_specs(self):
        for bad in ("web", "web=", "=60", "web=abc",
                    "web=0", "web=-5", "web=0.0"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_check_interval(bad)

    def test_apply_sets_and_replaces(self):
        cfg = {"endpoints": [
            dict(_tcp("web"), check_interval_s=60),
            _tcp("api"),
        ]}
        args = types.SimpleNamespace(check_interval=["web=120"])
        apply_check_interval_overrides(cfg, args)
        self.assertEqual(cfg["endpoints"][0]["check_interval_s"], 120.0)
        self.assertNotIn("check_interval_s", cfg["endpoints"][1])

    def test_apply_rejects_unknown_endpoint(self):
        cfg = {"endpoints": [_tcp("web")]}
        args = types.SimpleNamespace(check_interval=["nope=60"])
        with self.assertRaises(ValueError) as ctx:
            apply_check_interval_overrides(cfg, args)
        self.assertIn("unknown endpoint", str(ctx.exception))

    def test_apply_is_a_noop_without_the_attribute(self):
        cfg = {"endpoints": [_tcp("web")]}
        apply_check_interval_overrides(cfg, types.SimpleNamespace())
        self.assertNotIn("check_interval_s", cfg["endpoints"][0])

    def test_flag_naming_unknown_endpoint_exits_with_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"endpoints": [_tcp("web")]}, fh)
            with self.assertRaises(SystemExit):
                main(["--config", cfg_path, "--once",
                      "--check-interval", "nope=60"])


class CheckAllSubsetTests(unittest.TestCase):
    def test_only_checks_the_named_endpoints(self):
        # Both endpoints point at a closed port: a deterministic
        # DOWN, no network access needed.
        history = History()
        endpoints = [_tcp("a"), _tcp("b")]
        check_all(endpoints, history, flap_detector=FlapDetector(1),
                  only={"a"})
        self.assertEqual(history.names(), ["a"])
        self.assertIsNotNone(history.current("a"))
        self.assertIsNone(history.current("b"))

    def test_dependent_due_while_dependency_not_due_uses_last_state(self):
        # Dependency down in history: the dependent is skipped even
        # though the dependency was not checked this round, and the
        # dependency gains no new record.
        history = History()
        endpoints = [_tcp("db"), dict(_tcp("api"), depends_on="db")]
        check_all(endpoints, history, flap_detector=FlapDetector(1))
        self.assertFalse(history.current("db")["ok"])
        self.assertTrue(history.current("api")["skipped"])
        check_all(endpoints, history, flap_detector=FlapDetector(1),
                  only={"api"})
        self.assertEqual(len(history.recent("db", 100)), 1)
        self.assertTrue(history.current("api")["skipped"])

    def test_dependent_runs_when_dependency_was_up(self):
        # A listening socket makes the dependency UP; the dependent
        # is then checked (not skipped) on a round of its own.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            history = History()
            endpoints = [
                {"name": "db", "type": "tcp", "host": "127.0.0.1",
                 "port": port, "timeout": 1},
                dict(_tcp("api"), depends_on="db"),
            ]
            check_all(endpoints, history, flap_detector=FlapDetector(1))
            self.assertTrue(history.current("db")["ok"])
            self.assertFalse(history.current("api").get("skipped", False))
            # The dependent alone on the next round: the dependency
            # is up in history, so it runs again instead of skipping.
            before = len(history.recent("api", 100))
            check_all(endpoints, history, flap_detector=FlapDetector(1),
                      only={"api"})
            self.assertEqual(len(history.recent("api", 100)), before + 1)
            self.assertFalse(history.current("api").get("skipped", False))
        finally:
            listener.close()


class MonitorLoopSchedulingTests(unittest.TestCase):
    def test_fast_endpoint_checked_more_often_than_slow(self):
        # Fast every 0.05 s, slow every 1 s: eight passes take about
        # 0.35 s, so the slow endpoint is only checked on the first
        # pass (everything is due at startup) while the fast one is
        # checked every pass.
        cfg = {"interval_seconds": 0.05, "endpoints": [
            dict(_tcp("fast"), check_interval_s=0.05),
            dict(_tcp("slow"), check_interval_s=1.0),
        ]}
        history = History()
        monitor_loop(cfg, history, None, _args(), threading.Event(),
                     max_rounds=8)
        self.assertEqual(len(history.recent("fast", 100)), 8)
        self.assertEqual(len(history.recent("slow", 100)), 1)

    def test_global_interval_is_the_default_cadence(self):
        cfg = {"interval_seconds": 0.05,
               "endpoints": [_tcp("a"), _tcp("b")]}
        history = History()
        monitor_loop(cfg, history, None, _args(), threading.Event(),
                     max_rounds=3)
        self.assertEqual(len(history.recent("a", 100)), 3)
        self.assertEqual(len(history.recent("b", 100)), 3)


class ReloadTests(unittest.TestCase):
    def _write(self, path, cfg):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def test_reload_picks_up_interval_change(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            self._write(path, {"interval_seconds": 1,
                               "endpoints": [_tcp("a")]})
            current = load_config(path)
            self._write(path, {"interval_seconds": 1, "endpoints": [
                dict(_tcp("a"), check_interval_s=120)]})
            new = reload_config(path, _args(), current)
            self.assertIsNot(new, current)
            self.assertEqual(new["endpoints"][0]["check_interval_s"], 120)

    def test_bad_interval_on_reload_keeps_current(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            self._write(path, {"interval_seconds": 1,
                               "endpoints": [_tcp("a")]})
            current = load_config(path)
            self._write(path, {"interval_seconds": 1, "endpoints": [
                dict(_tcp("a"), check_interval_s=0)]})
            self.assertIs(reload_config(path, _args(), current), current)


class ServerTests(unittest.TestCase):
    def test_snapshot_reports_interval(self):
        from healthcheck.checker import CheckResult
        history = History()
        history.record(CheckResult(
            name="fast", ok=True, latency_ms=2.0, status_code=200,
            checked_at=1_700_000_000.0))
        history.record(CheckResult(
            name="plain", ok=True, latency_ms=2.0, status_code=200,
            checked_at=1_700_000_000.0))
        snap = status_snapshot(history, [
            dict(_tcp("fast"), check_interval_s=10),
            _tcp("plain"),
        ])
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["fast"]["check_interval_s"], 10)
        self.assertIsNone(by_name["plain"]["check_interval_s"])

    def test_dashboard_shows_interval_column(self):
        from healthcheck.checker import CheckResult
        history = History()
        history.record(CheckResult(
            name="fast", ok=True, latency_ms=2.0, status_code=200,
            checked_at=1_700_000_000.0))
        page = render_dashboard(history, [
            dict(_tcp("fast"), check_interval_s=60),
            _tcp("plain"),
        ])
        self.assertIn("Check every", page)
        self.assertIn("<td>60 s</td>", page)
        # No per-endpoint interval: a dash in the interval column,
        # meaning the global interval applies.
        self.assertIn("<td>-</td>", page)


if __name__ == "__main__":
    unittest.main()
