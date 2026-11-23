"""Tests for per-endpoint expected HTTP status (expect_status)."""

import json
import os
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from healthcheck.checker import check_endpoint, check_http
from healthcheck.cli import (apply_expect_status_overrides, check_all,
                             load_config, main, parse_expect_status,
                             reload_config)
from healthcheck.flap import FlapDetector
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History

_STATUSES = {
    "/ok": (200, b"ok"),
    "/created": (201, b"created"),
    "/no-content": (204, b""),
    "/not-found": (404, b"not here"),
    "/error": (500, b"boom"),
}


class _StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = _STATUSES[self.path]
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ExpectStatusCheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _StatusHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_matching_204_is_up(self):
        result = check_http("health", self.url("/no-content"),
                            expect_status=204)
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 204)

    def test_matching_201_is_up(self):
        result = check_http("api", self.url("/created"),
                            expect_status=201)
        self.assertTrue(result.ok)

    def test_mismatch_is_down_and_names_both_codes(self):
        result = check_http("health", self.url("/ok"),
                            expect_status=204)
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.error, "expected HTTP 204, got 200")

    def test_204_endpoint_returning_200_is_down(self):
        # The motivating case: a no-content health endpoint that
        # starts returning 200 (an error page, say) is not "up".
        result = check_http("health", self.url("/ok"),
                            expect_status=204)
        self.assertFalse(result.ok)

    def test_expected_404_matches_error_response(self):
        # urllib raises HTTPError for 4xx, so an expected 404 must
        # still count — the error path has to check the expectation.
        result = check_http("gone", self.url("/not-found"),
                            expect_status=404)
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 404)

    def test_expected_500_matches_error_response(self):
        result = check_http("flaky", self.url("/error"),
                            expect_status=500)
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 500)

    def test_mismatch_on_error_response(self):
        result = check_http("gone", self.url("/not-found"),
                            expect_status=200)
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(result.error, "expected HTTP 200, got 404")

    def test_no_expect_status_unchanged(self):
        self.assertTrue(check_http("api", self.url("/ok")).ok)
        self.assertFalse(check_http("api", self.url("/error")).ok)
        self.assertEqual(check_http("api", self.url("/error")).error,
                         "HTTP 500")

    def test_expect_status_and_body_assertion_both_must_hold(self):
        self.assertTrue(check_http(
            "health", self.url("/no-content"), expect_status=204,
            expect_body="").ok)
        result = check_http("health", self.url("/ok"),
                            expect_status=200, expect_body="missing")
        self.assertFalse(result.ok)
        self.assertIn("body does not contain", result.error)

    def test_body_assertion_runs_on_expected_error_status(self):
        result = check_http("gone", self.url("/not-found"),
                            expect_status=404,
                            expect_body="not here")
        self.assertTrue(result.ok)
        result = check_http("gone", self.url("/not-found"),
                            expect_status=404,
                            expect_body="something else")
        self.assertFalse(result.ok)
        self.assertIn("body does not contain", result.error)

    def test_bad_expect_status_raises(self):
        for bad in (True, "204", 99, 600, 0, 204.0):
            with self.assertRaises(ValueError, msg=repr(bad)):
                check_http("api", self.url("/ok"), expect_status=bad)

    def test_check_endpoint_passes_expect_status_through(self):
        result = check_endpoint({
            "name": "health", "type": "http",
            "url": self.url("/no-content"), "expect_status": 204,
        })
        self.assertTrue(result.ok)
        result = check_endpoint({
            "name": "health", "type": "http",
            "url": self.url("/ok"), "expect_status": 204,
        })
        self.assertFalse(result.ok)


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

    def test_valid_expect_status_loads(self):
        cfg = self._load([{
            "name": "health", "type": "http",
            "url": "http://127.0.0.1:1/health", "expect_status": 204,
        }])
        self.assertEqual(cfg["endpoints"][0]["expect_status"], 204)

    def test_non_integer_rejected(self):
        with self.assertRaises(ValueError):
            self._load([{
                "name": "health", "type": "http",
                "url": "http://127.0.0.1:1/", "expect_status": "204",
            }])

    def test_bool_rejected(self):
        with self.assertRaises(ValueError):
            self._load([{
                "name": "health", "type": "http",
                "url": "http://127.0.0.1:1/", "expect_status": True,
            }])

    def test_out_of_range_rejected(self):
        for bad in (99, 600, 0):
            with self.assertRaises(ValueError, msg=str(bad)):
                self._load([{
                    "name": "health", "type": "http",
                    "url": "http://127.0.0.1:1/",
                    "expect_status": bad,
                }])

    def test_expect_status_on_tcp_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._load([{
                "name": "db", "type": "tcp",
                "host": "127.0.0.1", "port": 5432, "expect_status": 200,
            }])
        self.assertIn("only applies to http", str(ctx.exception))


class CliOverrideTests(unittest.TestCase):
    def test_parse_valid_spec(self):
        self.assertEqual(parse_expect_status("health=204"),
                         ("health", 204))

    def test_parse_rejects_bad_specs(self):
        for bad in ("health", "health=", "=204", "health=abc",
                    "health=99", "health=600", "health=204.5",
                    "health=0204"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_expect_status(bad)

    def test_apply_sets_and_replaces(self):
        cfg = {"endpoints": [
            {"name": "health", "type": "http",
             "url": "http://127.0.0.1:1/", "expect_status": 200},
            {"name": "api", "type": "http",
             "url": "http://127.0.0.1:1/"},
        ]}
        args = types.SimpleNamespace(expect_status=["health=204"])
        apply_expect_status_overrides(cfg, args)
        self.assertEqual(cfg["endpoints"][0]["expect_status"], 204)
        self.assertNotIn("expect_status", cfg["endpoints"][1])

    def test_apply_rejects_unknown_endpoint(self):
        cfg = {"endpoints": [
            {"name": "health", "type": "http",
             "url": "http://127.0.0.1:1/"},
        ]}
        args = types.SimpleNamespace(expect_status=["nope=204"])
        with self.assertRaises(ValueError) as ctx:
            apply_expect_status_overrides(cfg, args)
        self.assertIn("unknown endpoint", str(ctx.exception))

    def test_apply_is_a_noop_without_the_attribute(self):
        # Namespaces built by older callers (e.g. test helpers)
        # carry no expect_status: applying must not crash.
        cfg = {"endpoints": [
            {"name": "health", "type": "http",
             "url": "http://127.0.0.1:1/"},
        ]}
        apply_expect_status_overrides(cfg, types.SimpleNamespace())
        self.assertNotIn("expect_status", cfg["endpoints"][0])

    def test_once_with_flag_overrides_config(self):
        server = HTTPServer(("127.0.0.1", 0), _StatusHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = os.path.join(d, "config.json")
                hist_path = os.path.join(d, "history.json")
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump({
                        "history_file": hist_path,
                        "endpoints": [{
                            "name": "health", "type": "http",
                            "url": f"http://127.0.0.1:{port}/no-content",
                        }],
                    }, fh)
                # Config says nothing; the flag demands 204, which
                # the endpoint returns, so the check is up.
                self.assertEqual(main([
                    "--config", cfg_path, "--once",
                    "--expect-status", "health=204"]), 0)
                with open(hist_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.assertTrue(data["health"][0]["ok"])
                self.assertEqual(data["health"][0]["status_code"], 204)
                # The same flag against a 200 endpoint is down.
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump({
                        "history_file": hist_path,
                        "endpoints": [{
                            "name": "health", "type": "http",
                            "url": f"http://127.0.0.1:{port}/ok",
                        }],
                    }, fh)
                self.assertEqual(main([
                    "--config", cfg_path, "--once",
                    "--expect-status", "health=204"]), 0)
                with open(hist_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.assertFalse(data["health"][1]["ok"])
                self.assertEqual(
                    data["health"][1]["error"],
                    "expected HTTP 204, got 200")
        finally:
            server.shutdown()
            server.server_close()

    def test_flag_naming_unknown_endpoint_exits_with_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"endpoints": [{
                    "name": "health", "type": "http",
                    "url": "http://127.0.0.1:1/",
                }]}, fh)
            with self.assertRaises(SystemExit):
                main(["--config", cfg_path, "--once",
                      "--expect-status", "nope=204"])


class CheckAllIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _StatusHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _endpoint(self, path, expect_status):
        return {"name": "health", "type": "http",
                "url": f"http://127.0.0.1:{self.port}{path}",
                "expect_status": expect_status, "timeout": 5}

    def test_mismatch_confirms_down_like_any_failure(self):
        history = History()
        check_all([self._endpoint("/ok", 204)], history,
                  flap_detector=FlapDetector(threshold=1))
        self.assertFalse(history.current("health")["ok"])
        self.assertEqual(history.current("health")["status_code"], 200)
        self.assertEqual(history.uptime_pct("health"), 0.0)

    def test_mismatch_below_threshold_is_unconfirmed(self):
        history = History()
        check_all([self._endpoint("/ok", 204)], history,
                  flap_detector=FlapDetector(threshold=3))
        entry = history.current("health")
        self.assertFalse(entry["ok"])
        self.assertTrue(entry["unconfirmed"])
        # Unconfirmed failures never count against uptime.
        self.assertIsNone(history.uptime_pct("health"))

    def test_matching_status_is_up(self):
        history = History()
        check_all([self._endpoint("/no-content", 204)], history,
                  flap_detector=FlapDetector(threshold=1))
        self.assertTrue(history.current("health")["ok"])
        self.assertEqual(history.uptime_pct("health"), 100.0)


class ReloadTests(unittest.TestCase):
    def _write(self, path, cfg):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def test_reload_picks_up_expect_status_change(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            ep = {"name": "health", "type": "http",
                  "url": "http://127.0.0.1:1/health"}
            self._write(path, {"endpoints": [ep]})
            current = load_config(path)
            self._write(path, {"endpoints": [
                dict(ep, expect_status=204)]})
            new = reload_config(
                path, types.SimpleNamespace(), current)
            self.assertIsNot(new, current)
            self.assertEqual(new["endpoints"][0]["expect_status"], 204)

    def test_bad_expect_status_on_reload_keeps_current(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            ep = {"name": "health", "type": "http",
                  "url": "http://127.0.0.1:1/health"}
            self._write(path, {"endpoints": [ep]})
            current = load_config(path)
            self._write(path, {"endpoints": [
                dict(ep, expect_status=99)]})
            self.assertIs(reload_config(
                path, types.SimpleNamespace(), current), current)


class ServerTests(unittest.TestCase):
    def test_snapshot_reports_expected_and_actual_status(self):
        from healthcheck.checker import CheckResult
        history = History()
        history.record(CheckResult(
            name="health", ok=False, latency_ms=3.0,
            status_code=200,
            error="expected HTTP 204, got 200",
            checked_at=1_700_000_000.0))
        history.record(CheckResult(
            name="plain", ok=True, latency_ms=2.0, status_code=200,
            checked_at=1_700_000_000.0))
        snap = status_snapshot(history, [
            {"name": "health", "type": "http",
             "url": "http://127.0.0.1:1/health", "expect_status": 204},
            {"name": "plain", "type": "http",
             "url": "http://127.0.0.1:1/"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": 5432},
        ])
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["health"]["expect_status"], 204)
        self.assertEqual(by_name["health"]["status_code"], 200)
        self.assertEqual(by_name["health"]["status"], "DOWN")
        self.assertIsNone(by_name["plain"]["expect_status"])
        self.assertEqual(by_name["plain"]["status_code"], 200)
        self.assertIsNone(by_name["db"]["expect_status"])
        self.assertIsNone(by_name["db"]["status_code"])

    def test_snapshot_unknown_endpoint_has_null_statuses(self):
        snap = status_snapshot(History(), [
            {"name": "fresh", "type": "http",
             "url": "http://127.0.0.1:1/", "expect_status": 204},
        ])
        ep = snap["endpoints"][0]
        self.assertEqual(ep["status"], "UNKNOWN")
        self.assertEqual(ep["expect_status"], 204)
        self.assertIsNone(ep["status_code"])

    def test_dashboard_shows_expected_vs_actual(self):
        from healthcheck.checker import CheckResult
        history = History()
        history.record(CheckResult(
            name="health", ok=False, latency_ms=3.0,
            status_code=200,
            error="expected HTTP 204, got 200",
            checked_at=1_700_000_000.0))
        history.record(CheckResult(
            name="plain", ok=True, latency_ms=2.0, status_code=200,
            checked_at=1_700_000_000.0))
        page = render_dashboard(history, [
            {"name": "health", "type": "http",
             "url": "http://127.0.0.1:1/health", "expect_status": 204},
            {"name": "plain", "type": "http",
             "url": "http://127.0.0.1:1/"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": 5432},
        ])
        self.assertIn("HTTP status", page)
        self.assertIn("expected 204, got 200", page)
        # No expectation: the bare actual code. TCP: a dash.
        self.assertIn("<td>200</td>", page)
        self.assertIn("<td>-</td>", page)


if __name__ == "__main__":
    unittest.main()
