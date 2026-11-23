"""Tests for the --check dry-run mode."""

import io
import json
import os
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

from healthcheck.checker import CheckResult, maintenance_result, skipped_result
from healthcheck.cli import (dry_run_check, format_check_line, main,
                             select_check_targets)


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records POST bodies, so a test can prove no alert was sent."""
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        type(self).received.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_ok_server():
    server = HTTPServer(("127.0.0.1", 0), _OkHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_capture_server():
    _CaptureHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _write_config(d, cfg):
    path = os.path.join(d, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    return path


def _run_main(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(argv)
    return code, out.getvalue()


class FormatTests(unittest.TestCase):
    def test_up_http_line(self):
        line = format_check_line(
            CheckResult(name="api", ok=True, latency_ms=12.3,
                        status_code=200, checked_at=1.0))
        self.assertEqual(line, "api: UP HTTP 200 12.3 ms")

    def test_up_tcp_line_has_no_status(self):
        line = format_check_line(
            CheckResult(name="db", ok=True, latency_ms=1.2,
                        checked_at=1.0))
        self.assertEqual(line, "db: UP 1.2 ms")

    def test_down_http_line(self):
        line = format_check_line(
            CheckResult(name="api", ok=False, latency_ms=8.1,
                        status_code=500, error="HTTP 500",
                        checked_at=1.0))
        self.assertEqual(line, "api: DOWN HTTP 500 8.1 ms")

    def test_down_line_names_the_rule_that_fired(self):
        line = format_check_line(
            CheckResult(name="slow", ok=False, latency_ms=612.0,
                        status_code=200,
                        error="latency 612.0 ms exceeds budget 500 ms",
                        checked_at=1.0))
        self.assertEqual(
            line,
            "slow: DOWN HTTP 200 (latency 612.0 ms exceeds budget 500 ms) "
            "612.0 ms")

    def test_down_tcp_line(self):
        line = format_check_line(
            CheckResult(name="db", ok=False, latency_ms=0.4,
                        error="[Errno 111] Connection refused",
                        checked_at=1.0))
        self.assertEqual(
            line, "db: DOWN [Errno 111] Connection refused 0.4 ms")

    def test_skipped_and_maintenance_lines(self):
        self.assertEqual(
            format_check_line(
                skipped_result("api", "db", "dependency 'db' is DOWN")),
            "api: SKIPPED skipped: dependency 'db' is DOWN")
        self.assertEqual(
            format_check_line(maintenance_result("api", "Sat 02:00–04:00")),
            "api: MAINTENANCE maintenance window: Sat 02:00–04:00")


class TargetSelectionTests(unittest.TestCase):
    def test_all_endpoints_when_no_name(self):
        eps = [{"name": "a"}, {"name": "b"}]
        self.assertEqual(select_check_targets(eps, None), eps)

    def test_named_endpoint_brings_dependencies(self):
        eps = [{"name": "db"},
               {"name": "api", "depends_on": "db"},
               {"name": "other"}]
        selected = select_check_targets(eps, "api")
        self.assertEqual([ep["name"] for ep in selected], ["db", "api"])

    def test_unknown_name_rejected(self):
        with self.assertRaises(ValueError):
            select_check_targets([{"name": "a"}], "nope")


class DryRunTests(unittest.TestCase):
    def test_all_up_exits_zero(self):
        server = _run_ok_server()
        port = server.server_address[1]
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = _write_config(d, {"endpoints": [
                    {"name": "api", "type": "http",
                     "url": f"http://127.0.0.1:{port}/"},
                ]})
                code, out = _run_main(["--config", cfg_path, "--check"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 0)
        self.assertIn("api: UP HTTP 200", out)
        self.assertIn("1 endpoint(s) checked: 1 up", out)

    def test_down_endpoint_exits_one(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = _write_config(d, {"endpoints": [
                {"name": "closed", "type": "tcp", "host": "127.0.0.1",
                 "port": _free_port()},
            ]})
            code, out = _run_main(["--config", cfg_path, "--check"])
        self.assertEqual(code, 1)
        self.assertIn("closed: DOWN", out)
        self.assertIn("1 endpoint(s) checked: 1 down", out)

    def test_flap_threshold_does_not_apply_to_a_single_pass(self):
        # The monitor would call one failure unconfirmed with a
        # threshold of 3; the dry run judges the pass itself, so a
        # single failed check is DOWN and the exit code is 1.
        with tempfile.TemporaryDirectory() as d:
            cfg_path = _write_config(d, {
                "flap_threshold": 3,
                "endpoints": [
                    {"name": "closed", "type": "tcp", "host": "127.0.0.1",
                     "port": _free_port()},
                ]})
            code, out = _run_main(["--config", cfg_path, "--check"])
        self.assertEqual(code, 1)
        self.assertIn("closed: DOWN", out)

    def test_named_endpoint_only_checks_that_one(self):
        server = _run_ok_server()
        port = server.server_address[1]
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = _write_config(d, {"endpoints": [
                    {"name": "api", "type": "http",
                     "url": f"http://127.0.0.1:{port}/"},
                    {"name": "closed", "type": "tcp", "host": "127.0.0.1",
                     "port": _free_port()},
                ]})
                code, out = _run_main(
                    ["--config", cfg_path, "--check", "api"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 0)
        self.assertIn("api: UP HTTP 200", out)
        self.assertNotIn("closed", out)

    def test_unknown_name_exits_two(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = _write_config(d, {"endpoints": [
                {"name": "api", "type": "tcp", "host": "127.0.0.1",
                 "port": _free_port()},
            ]})
            with self.assertRaises(SystemExit) as ctx:
                _run_main(["--config", cfg_path, "--check", "nope"])
        self.assertEqual(ctx.exception.code, 2)

    def test_latency_budget_breach_is_down_and_names_the_budget(self):
        server = _run_ok_server()
        port = server.server_address[1]
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = _write_config(d, {"endpoints": [
                    {"name": "api", "type": "http",
                     "url": f"http://127.0.0.1:{port}/",
                     "max_latency_ms": 0.001},
                ]})
                code, out = _run_main(["--config", cfg_path, "--check"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 1)
        self.assertIn("api: DOWN", out)
        self.assertIn("exceeds budget 0.001 ms", out)

    def test_dependent_skipped_when_dependency_down(self):
        server = _run_ok_server()
        port = server.server_address[1]
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = _write_config(d, {"endpoints": [
                    {"name": "db", "type": "tcp", "host": "127.0.0.1",
                     "port": _free_port()},
                    {"name": "api", "type": "http",
                     "url": f"http://127.0.0.1:{port}/",
                     "depends_on": "db"},
                ]})
                code, out = _run_main(
                    ["--config", cfg_path, "--check", "api"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 1)
        self.assertIn("db: DOWN", out)
        self.assertIn("api: SKIPPED", out)
        self.assertIn("dependency 'db' is DOWN", out)

    def test_maintenance_endpoint_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = _write_config(d, {"endpoints": [
                {"name": "api", "type": "tcp", "host": "127.0.0.1",
                 "port": _free_port(),
                 "maintenance_windows": [
                     {"start": "00:00", "end": "23:59"}]},
            ]})
            code, out = _run_main(["--config", cfg_path, "--check"])
        self.assertEqual(code, 0)
        self.assertIn("api: MAINTENANCE", out)

    def test_no_history_written(self):
        with tempfile.TemporaryDirectory() as d:
            hist_path = os.path.join(d, "history.json")
            cfg_path = _write_config(d, {
                "history_file": hist_path,
                "endpoints": [
                    {"name": "closed", "type": "tcp", "host": "127.0.0.1",
                     "port": _free_port()},
                ]})
            code, _ = _run_main(["--config", cfg_path, "--check"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(hist_path))

    def test_no_alert_sent(self):
        server = _run_capture_server()
        port = server.server_address[1]
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = _write_config(d, {
                    "webhook_url": f"http://127.0.0.1:{port}/hook",
                    "endpoints": [
                        {"name": "closed", "type": "tcp",
                         "host": "127.0.0.1", "port": _free_port()},
                    ]})
                code, _ = _run_main(["--config", cfg_path, "--check"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 1)
        self.assertEqual(_CaptureHandler.received, [])

    def test_dry_run_check_returns_results_and_code(self):
        results, code = dry_run_check(
            [{"name": "closed", "type": "tcp", "host": "127.0.0.1",
              "port": _free_port()}])
        self.assertEqual(code, 1)
        self.assertFalse(results["closed"].ok)


if __name__ == "__main__":
    unittest.main()
