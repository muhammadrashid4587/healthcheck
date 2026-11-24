"""Tests for retry-once before a failure counts (0.28.0)."""

import io
import json
import os
import socket
import tempfile
import threading
import unittest
from collections import deque
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

from healthcheck.checker import CheckResult, check_endpoint_with_retry
from healthcheck.cli import (check_all, dry_run_check, format_check_line,
                             load_config, main, monitor_loop, retry_enabled)
from healthcheck.flap import FlapDetector
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FlakyHandler(BaseHTTPRequestHandler):
    """Fails the first GET with 500, then answers 200."""
    failures_left = 1

    def do_GET(self):
        if type(self).failures_left > 0:
            type(self).failures_left -= 1
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class _AlternatingHandler(BaseHTTPRequestHandler):
    """Fails every odd-numbered GET, answers 200 on even ones — so
    every check round fails its first attempt and recovers on the
    retry."""
    count = 0

    def do_GET(self):
        type(self).count += 1
        if type(self).count % 2 == 1:
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class _AlwaysDownHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(500)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"no")

    def log_message(self, *args):
        pass


def _run_server(handler):
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _tcp(name, port=1):
    return {"name": name, "type": "tcp", "host": "127.0.0.1",
            "port": port, "timeout": 1}


def _http(name, port):
    return {"name": name, "type": "http",
            "url": f"http://127.0.0.1:{port}/", "timeout": 2}


def _args():
    import types
    return types.SimpleNamespace(history_file=None, webhook_url=None,
                                 flap_threshold=None,
                                 no_retry_on_failure=False)


class CheckEndpointWithRetryTests(unittest.TestCase):
    def test_closed_port_retries_once_then_stays_down(self):
        result = check_endpoint_with_retry(_tcp("db"), retry=True)
        self.assertFalse(result.ok)
        self.assertEqual(result.retries, 1)

    def test_closed_port_without_retry_runs_once(self):
        result = check_endpoint_with_retry(_tcp("db"), retry=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.retries, 0)

    def test_success_never_retries(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            result = check_endpoint_with_retry(
                {"name": "db", "type": "tcp", "host": "127.0.0.1",
                 "port": port, "timeout": 1})
        finally:
            listener.close()
        self.assertTrue(result.ok)
        self.assertEqual(result.retries, 0)

    def test_first_failure_recovering_on_retry_is_up(self):
        _FlakyHandler.failures_left = 1
        server = _run_server(_FlakyHandler)
        try:
            result = check_endpoint_with_retry(
                _http("api", server.server_address[1]))
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(result.retries, 1)
        self.assertEqual(result.status_code, 200)

    def test_two_failures_stay_down_with_retry_error(self):
        server = _run_server(_AlwaysDownHandler)
        try:
            result = check_endpoint_with_retry(
                _http("api", server.server_address[1]))
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(result.ok)
        self.assertEqual(result.retries, 1)
        self.assertEqual(result.error, "HTTP 500")


class CheckAllRetryTests(unittest.TestCase):
    def test_recovered_blip_is_one_up_entry_not_a_flap_failure(self):
        _FlakyHandler.failures_left = 1
        server = _run_server(_FlakyHandler)
        try:
            history = History()
            detector = FlapDetector(3)
            results = check_all(
                [_http("api", server.server_address[1])], history,
                flap_detector=detector)
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(results["api"].ok)
        self.assertEqual(results["api"].retries, 1)
        self.assertEqual(len(history.recent("api", 100)), 1)
        self.assertTrue(history.current("api")["ok"])
        self.assertEqual(history.current("api")["retries"], 1)
        self.assertEqual(detector.consecutive_failures("api"), 0)
        self.assertEqual(history.uptime_pct("api"), 100.0)

    def test_two_failures_count_once_towards_the_flap_threshold(self):
        server = _run_server(_AlwaysDownHandler)
        try:
            history = History()
            detector = FlapDetector(3)
            for _ in range(3):
                results = check_all(
                    [_http("api", server.server_address[1])], history,
                    flap_detector=detector)
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(results["api"].ok)
        self.assertEqual(results["api"].retries, 1)
        # Three rounds, one recorded failure each: the third confirms
        # the outage. The retry did not inflate the count — three
        # recorded failures, not six.
        self.assertEqual(len(history.recent("api", 100)), 3)
        self.assertTrue(detector.is_down("api"))
        self.assertEqual(history.retries_total("api"), 3)

    def test_retry_disabled_by_callable_records_no_retry(self):
        history = History()
        results = check_all([_tcp("db")], history,
                            flap_detector=FlapDetector(1),
                            retry=lambda ep: False)
        self.assertFalse(results["db"].ok)
        self.assertEqual(results["db"].retries, 0)
        self.assertEqual(history.current("db")["retries"], 0)

    def test_retry_disabled_by_bool_records_no_retry(self):
        history = History()
        results = check_all([_tcp("db")], history,
                            flap_detector=FlapDetector(1), retry=False)
        self.assertFalse(results["db"].ok)
        self.assertEqual(results["db"].retries, 0)

    def test_retry_does_not_reset_the_check_schedule(self):
        # Every round fails its first attempt and recovers on the
        # retry: three rounds must record exactly three entries, one
        # per round — the retry is inside the round, not a second
        # check, so the next-due time is not reset by it.
        _AlternatingHandler.count = 0
        server = _run_server(_AlternatingHandler)
        try:
            cfg = {"interval_seconds": 0.05, "endpoints": [
                _http("api", server.server_address[1]),
            ]}
            history = History()
            monitor_loop(cfg, history, None, _args(),
                         threading.Event(), max_rounds=3)
        finally:
            server.shutdown()
            server.server_close()
        entries = history.recent("api", 100)
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e["ok"] for e in entries))
        self.assertTrue(all(e["retries"] == 1 for e in entries))


class DryRunRetryTests(unittest.TestCase):
    def _run_main(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()

    def _write_config(self, d, cfg):
        path = os.path.join(d, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        return path

    def test_dry_run_shows_the_retry_and_exits_zero(self):
        _FlakyHandler.failures_left = 1
        server = _run_server(_FlakyHandler)
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = self._write_config(d, {"endpoints": [
                    _http("api", server.server_address[1]),
                ]})
                code, out = self._run_main(
                    ["--config", cfg_path, "--check"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 0)
        self.assertIn("api: UP HTTP 200", out)
        self.assertIn("(retried once)", out)

    def test_dry_run_down_after_retry_still_names_it(self):
        server = _run_server(_AlwaysDownHandler)
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = self._write_config(d, {"endpoints": [
                    _http("api", server.server_address[1]),
                ]})
                code, out = self._run_main(
                    ["--config", cfg_path, "--check"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 1)
        self.assertIn("api: DOWN HTTP 500", out)
        self.assertIn("(retried once)", out)

    def test_no_retry_flag_makes_the_blip_down(self):
        _FlakyHandler.failures_left = 1
        server = _run_server(_FlakyHandler)
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = self._write_config(d, {"endpoints": [
                    _http("api", server.server_address[1]),
                ]})
                code, out = self._run_main(
                    ["--config", cfg_path, "--check",
                     "--no-retry-on-failure"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 1)
        self.assertIn("api: DOWN HTTP 500", out)
        self.assertNotIn("(retried once)", out)

    def test_format_check_line_marks_retries(self):
        line = format_check_line(
            CheckResult(name="api", ok=True, latency_ms=12.3,
                        status_code=200, checked_at=1.0, retries=1))
        self.assertEqual(line, "api: UP HTTP 200 12.3 ms (retried once)")
        line = format_check_line(
            CheckResult(name="api", ok=False, latency_ms=8.1,
                        status_code=500, error="HTTP 500",
                        checked_at=1.0, retries=1))
        self.assertEqual(line, "api: DOWN HTTP 500 8.1 ms (retried once)")
        line = format_check_line(
            CheckResult(name="api", ok=True, latency_ms=12.3,
                        status_code=200, checked_at=1.0, retries=0))
        self.assertEqual(line, "api: UP HTTP 200 12.3 ms")

    def test_dry_run_check_passes_retry_through(self):
        _FlakyHandler.failures_left = 1
        server = _run_server(_FlakyHandler)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                results, code = dry_run_check(
                    [_http("api", server.server_address[1])])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 0)
        self.assertTrue(results["api"].ok)
        self.assertEqual(results["api"].retries, 1)


class ConfigValidationTests(unittest.TestCase):
    def _load(self, cfg):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        try:
            return load_config(path)
        finally:
            os.unlink(path)

    def test_valid_retry_settings_load(self):
        cfg = self._load({
            "retry_on_failure": False,
            "endpoints": [dict(_tcp("web"), retry_on_failure=True)]})
        self.assertFalse(cfg["retry_on_failure"])
        self.assertTrue(cfg["endpoints"][0]["retry_on_failure"])

    def test_bad_global_retry_rejected(self):
        for bad in ("false", 1, 0, "yes"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._load({"retry_on_failure": bad, "endpoints": []})

    def test_bad_endpoint_retry_rejected(self):
        for bad in ("false", 1, 0):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._load({"endpoints": [
                    dict(_tcp("web"), retry_on_failure=bad)]})

    def test_retry_enabled_defaults_on(self):
        self.assertTrue(retry_enabled({}, _tcp("web")))

    def test_retry_enabled_global_off(self):
        self.assertFalse(
            retry_enabled({"retry_on_failure": False}, _tcp("web")))

    def test_retry_enabled_endpoint_wins_over_global(self):
        cfg = {"retry_on_failure": False}
        self.assertTrue(
            retry_enabled(cfg, dict(_tcp("web"), retry_on_failure=True)))
        self.assertFalse(
            retry_enabled({"retry_on_failure": True},
                          dict(_tcp("web"), retry_on_failure=False)))

    def test_retry_enabled_flag_wins_over_everything(self):
        import types
        args = types.SimpleNamespace(no_retry_on_failure=True)
        self.assertFalse(
            retry_enabled({"retry_on_failure": True},
                          dict(_tcp("web"), retry_on_failure=True), args))
        args = types.SimpleNamespace(no_retry_on_failure=False)
        self.assertTrue(
            retry_enabled({}, _tcp("web"), args))

    def test_retry_enabled_is_safe_without_the_flag_attribute(self):
        import types
        self.assertTrue(retry_enabled({}, _tcp("web"),
                                      types.SimpleNamespace()))


class HistoryRetryTests(unittest.TestCase):
    def test_retries_total_sums_entries(self):
        history = History()
        history.record(CheckResult(name="api", ok=True, latency_ms=1.0,
                                   checked_at=1.0, retries=1))
        history.record(CheckResult(name="api", ok=True, latency_ms=1.0,
                                   checked_at=2.0, retries=0))
        history.record(CheckResult(name="api", ok=False, latency_ms=1.0,
                                   checked_at=3.0, retries=1))
        self.assertEqual(history.retries_total("api"), 2)
        self.assertEqual(history.retries_total("never-checked"), 0)

    def test_old_entries_without_retries_count_zero(self):
        history = History()
        # A hand-built entry with no "retries" key, as written before
        # 0.28.0: it counts as 0, not as a missing record.
        history._checks["api"] = deque(
            [{"ok": True, "latency_ms": 1.0, "status_code": 200,
              "error": None, "checked_at": 1.0, "skipped": False,
              "depends_on": None, "unconfirmed": False,
              "maintenance": False}], maxlen=100)
        self.assertEqual(history.retries_total("api"), 0)


class ServerRetryTests(unittest.TestCase):
    def test_snapshot_reports_retries(self):
        history = History()
        history.record(CheckResult(
            name="api", ok=True, latency_ms=2.0, status_code=200,
            checked_at=1_700_000_000.0, retries=1))
        history.record(CheckResult(
            name="plain", ok=True, latency_ms=2.0, status_code=200,
            checked_at=1_700_000_000.0))
        snap = status_snapshot(history, [_http("api", 1), _tcp("plain")])
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["api"]["retries"], 1)
        self.assertEqual(by_name["api"]["retries_total"], 1)
        self.assertEqual(by_name["plain"]["retries"], 0)
        self.assertEqual(by_name["plain"]["retries_total"], 0)

    def test_snapshot_never_checked_has_null_retries(self):
        history = History()
        snap = status_snapshot(history, [_tcp("plain")])
        self.assertIsNone(snap["endpoints"][0]["retries"])
        self.assertEqual(snap["endpoints"][0]["retries_total"], 0)

    def test_dashboard_shows_retries_column(self):
        history = History()
        history.record(CheckResult(
            name="api", ok=True, latency_ms=2.0, status_code=200,
            checked_at=1_700_000_000.0, retries=1))
        page = render_dashboard(history, [_http("api", 1)])
        self.assertIn("Retries", page)
        self.assertIn("<td>1</td>", page)


if __name__ == "__main__":
    unittest.main()
