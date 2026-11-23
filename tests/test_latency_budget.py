"""Tests for per-endpoint latency budgets (max_latency_ms)."""

import json
import os
import tempfile
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from healthcheck.cli import (apply_max_latency_overrides, check_all,
                             load_config, main, parse_max_latency,
                             reload_config)
from healthcheck.flap import FlapDetector
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History


class _LatencyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/slow":
            time.sleep(0.15)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class CheckAllIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _LatencyHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _endpoint(self, path, budget):
        return {"name": "slow", "type": "http",
                "url": f"http://127.0.0.1:{self.port}{path}",
                "max_latency_ms": budget, "timeout": 5}

    def test_slow_check_over_budget_is_down_and_names_both(self):
        history = History()
        check_all([self._endpoint("/slow", 10)], history,
                  flap_detector=FlapDetector(threshold=1))
        entry = history.current("slow")
        self.assertFalse(entry["ok"])
        self.assertIn("exceeds budget 10 ms", entry["error"])
        self.assertIn("latency", entry["error"])
        # The slow latency is still recorded, so the dashboard can
        # show how slow it was.
        self.assertGreater(entry["latency_ms"], 100)
        self.assertEqual(history.uptime_pct("slow"), 0.0)

    def test_breach_below_threshold_is_unconfirmed(self):
        history = History()
        check_all([self._endpoint("/slow", 10)], history,
                  flap_detector=FlapDetector(threshold=3))
        entry = history.current("slow")
        self.assertFalse(entry["ok"])
        self.assertTrue(entry["unconfirmed"])
        # Unconfirmed failures never count against uptime.
        self.assertIsNone(history.uptime_pct("slow"))

    def test_breach_confirms_down_after_threshold_rounds(self):
        history = History()
        detector = FlapDetector(threshold=2)
        check_all([self._endpoint("/slow", 10)], history,
                  flap_detector=detector)
        self.assertTrue(history.current("slow")["unconfirmed"])
        check_all([self._endpoint("/slow", 10)], history,
                  flap_detector=detector)
        entry = history.current("slow")
        self.assertFalse(entry["ok"])
        self.assertFalse(entry["unconfirmed"])
        self.assertEqual(history.uptime_pct("slow"), 0.0)

    def test_fast_check_within_budget_is_up(self):
        history = History()
        check_all([self._endpoint("/fast", 10_000)], history,
                  flap_detector=FlapDetector(threshold=1))
        self.assertTrue(history.current("slow")["ok"])
        self.assertEqual(history.uptime_pct("slow"), 100.0)

    def test_slow_check_without_a_budget_is_up(self):
        history = History()
        ep = {"name": "slow", "type": "http",
              "url": f"http://127.0.0.1:{self.port}/slow", "timeout": 5}
        check_all([ep], history, flap_detector=FlapDetector(threshold=1))
        self.assertTrue(history.current("slow")["ok"])

    def test_fractional_budget(self):
        history = History()
        check_all([self._endpoint("/slow", 250.5)], history,
                  flap_detector=FlapDetector(threshold=1))
        self.assertTrue(history.current("slow")["ok"])


class ConfigValidationTests(unittest.TestCase):
    def _load(self, endpoints):
        cfg = {"endpoints": endpoints}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        try:
            return load_config(path)
        finally:
            os.unlink(path)

    def test_valid_budget_loads(self):
        cfg = self._load([{
            "name": "web", "type": "http",
            "url": "http://127.0.0.1:1/", "max_latency_ms": 500,
        }])
        self.assertEqual(cfg["endpoints"][0]["max_latency_ms"], 500)

    def test_fractional_budget_loads(self):
        cfg = self._load([{
            "name": "web", "type": "http",
            "url": "http://127.0.0.1:1/", "max_latency_ms": 250.5,
        }])
        self.assertEqual(cfg["endpoints"][0]["max_latency_ms"], 250.5)

    def test_budget_allowed_on_tcp(self):
        # A TCP connect can be slow too; the budget applies to
        # either endpoint type.
        cfg = self._load([{
            "name": "db", "type": "tcp",
            "host": "127.0.0.1", "port": 5432, "max_latency_ms": 100,
        }])
        self.assertEqual(cfg["endpoints"][0]["max_latency_ms"], 100)

    def test_bad_budgets_rejected(self):
        for bad in (0, 0.0, -100, "500", True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._load([{
                    "name": "web", "type": "http",
                    "url": "http://127.0.0.1:1/", "max_latency_ms": bad,
                }])


class CliOverrideTests(unittest.TestCase):
    def test_parse_valid_specs(self):
        self.assertEqual(parse_max_latency("web=500"), ("web", 500.0))
        self.assertEqual(parse_max_latency("web=250.5"), ("web", 250.5))

    def test_parse_rejects_bad_specs(self):
        for bad in ("web", "web=", "=500", "web=abc",
                    "web=0", "web=-5", "web=0.0"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_max_latency(bad)

    def test_apply_sets_and_replaces(self):
        cfg = {"endpoints": [
            {"name": "web", "type": "http",
             "url": "http://127.0.0.1:1/", "max_latency_ms": 500},
            {"name": "api", "type": "http",
             "url": "http://127.0.0.1:1/"},
        ]}
        args = types.SimpleNamespace(max_latency=["web=100"])
        apply_max_latency_overrides(cfg, args)
        self.assertEqual(cfg["endpoints"][0]["max_latency_ms"], 100.0)
        self.assertNotIn("max_latency_ms", cfg["endpoints"][1])

    def test_apply_rejects_unknown_endpoint(self):
        cfg = {"endpoints": [
            {"name": "web", "type": "http",
             "url": "http://127.0.0.1:1/"},
        ]}
        args = types.SimpleNamespace(max_latency=["nope=500"])
        with self.assertRaises(ValueError) as ctx:
            apply_max_latency_overrides(cfg, args)
        self.assertIn("unknown endpoint", str(ctx.exception))

    def test_apply_is_a_noop_without_the_attribute(self):
        cfg = {"endpoints": [
            {"name": "web", "type": "http",
             "url": "http://127.0.0.1:1/"},
        ]}
        apply_max_latency_overrides(cfg, types.SimpleNamespace())
        self.assertNotIn("max_latency_ms", cfg["endpoints"][0])

    def test_once_with_flag_marks_slow_endpoint_down(self):
        server = HTTPServer(("127.0.0.1", 0), _LatencyHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = os.path.join(d, "config.json")
                hist_path = os.path.join(d, "history.json")
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump({
                        "history_file": hist_path,
                        "flap_threshold": 1,
                        "endpoints": [{
                            "name": "slow", "type": "http",
                            "url": f"http://127.0.0.1:{port}/slow",
                        }],
                    }, fh)
                # No budget in the config; the flag sets 10 ms,
                # which the ~150 ms response breaches.
                self.assertEqual(main([
                    "--config", cfg_path, "--once",
                    "--max-latency", "slow=10"]), 0)
                with open(hist_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.assertFalse(data["slow"][0]["ok"])
                self.assertIn("exceeds budget 10 ms",
                              data["slow"][0]["error"])
        finally:
            server.shutdown()
            server.server_close()

    def test_flag_naming_unknown_endpoint_exits_with_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"endpoints": [{
                    "name": "web", "type": "http",
                    "url": "http://127.0.0.1:1/",
                }]}, fh)
            with self.assertRaises(SystemExit):
                main(["--config", cfg_path, "--once",
                      "--max-latency", "nope=500"])


class ReloadTests(unittest.TestCase):
    def _write(self, path, cfg):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def test_reload_picks_up_budget_change(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            ep = {"name": "web", "type": "http",
                  "url": "http://127.0.0.1:1/"}
            self._write(path, {"endpoints": [ep]})
            current = load_config(path)
            self._write(path, {"endpoints": [
                dict(ep, max_latency_ms=500)]})
            new = reload_config(
                path, types.SimpleNamespace(), current)
            self.assertIsNot(new, current)
            self.assertEqual(new["endpoints"][0]["max_latency_ms"], 500)

    def test_bad_budget_on_reload_keeps_current(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            ep = {"name": "web", "type": "http",
                  "url": "http://127.0.0.1:1/"}
            self._write(path, {"endpoints": [ep]})
            current = load_config(path)
            self._write(path, {"endpoints": [
                dict(ep, max_latency_ms=-5)]})
            self.assertIs(reload_config(
                path, types.SimpleNamespace(), current), current)


class ServerTests(unittest.TestCase):
    def test_snapshot_reports_budget(self):
        from healthcheck.checker import CheckResult
        history = History()
        history.record(CheckResult(
            name="slow", ok=False, latency_ms=812.3,
            status_code=200,
            error="latency 812.3 ms exceeds budget 500 ms",
            checked_at=1_700_000_000.0))
        history.record(CheckResult(
            name="plain", ok=True, latency_ms=2.0, status_code=200,
            checked_at=1_700_000_000.0))
        snap = status_snapshot(history, [
            {"name": "slow", "type": "http",
             "url": "http://127.0.0.1:1/slow", "max_latency_ms": 500},
            {"name": "plain", "type": "http",
             "url": "http://127.0.0.1:1/"},
        ])
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["slow"]["max_latency_ms"], 500)
        self.assertEqual(by_name["slow"]["status"], "DOWN")
        self.assertEqual(by_name["slow"]["latency_ms"], 812.3)
        self.assertIsNone(by_name["plain"]["max_latency_ms"])
        self.assertEqual(by_name["plain"]["status"], "UP")

    def test_dashboard_shows_budget_column(self):
        from healthcheck.checker import CheckResult
        history = History()
        history.record(CheckResult(
            name="slow", ok=True, latency_ms=120.0, status_code=200,
            checked_at=1_700_000_000.0))
        page = render_dashboard(history, [
            {"name": "slow", "type": "http",
             "url": "http://127.0.0.1:1/slow", "max_latency_ms": 500},
            {"name": "plain", "type": "http",
             "url": "http://127.0.0.1:1/"},
        ])
        self.assertIn("Latency budget", page)
        self.assertIn("<td>500 ms</td>", page)
        # No budget: a dash in the budget column.
        self.assertIn("<td>-</td>", page)


if __name__ == "__main__":
    unittest.main()
