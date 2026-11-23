"""Tests for response-body assertions (expect_body /
expect_body_regex)."""

import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from healthcheck.checker import check_endpoint, check_http
from healthcheck.cli import load_config

_BODIES = {
    "/ok": (200, b'{"status": "ok", "version": "1.2.3"}'),
    "/error-page": (200, b"Something went wrong"),
    "/fail": (500, b"Internal Server Error"),
    "/binary": (200, b"\xff\xfe\x00ok\xff"),
}


class _BodyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        status, body = _BODIES[self.path]
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class BodyAssertionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _BodyHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def test_substring_present_is_up(self):
        result = check_http("api", self.url("/ok"),
                            expect_body='"status": "ok"')
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)

    def test_substring_absent_is_down(self):
        result = check_http("api", self.url("/error-page"),
                            expect_body='"status": "ok"')
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertIn('"status": "ok"', result.error)

    def test_regex_match_is_up(self):
        result = check_http("api", self.url("/ok"),
                            expect_body_regex=r'"version": "\d+\.\d+\.\d+"')
        self.assertTrue(result.ok)

    def test_regex_no_match_is_down(self):
        result = check_http("api", self.url("/ok"),
                            expect_body_regex=r'"version": "9\.9\.9"')
        self.assertFalse(result.ok)
        self.assertIn("body does not match", result.error)
        self.assertIn("version", result.error)

    def test_both_assertions_must_hold(self):
        # Substring holds, regex does not: the check still fails.
        result = check_http("api", self.url("/ok"),
                            expect_body="ok",
                            expect_body_regex=r"version.*2\.0\.0")
        self.assertFalse(result.ok)

    def test_failed_status_skips_assertion(self):
        result = check_http("api", self.url("/fail"),
                            expect_body="ok")
        self.assertFalse(result.ok)
        self.assertEqual(result.status_code, 500)
        self.assertEqual(result.error, "HTTP 500")

    def test_invalid_regex_is_a_failed_check_not_a_crash(self):
        result = check_http("api", self.url("/ok"),
                            expect_body_regex=r"(")
        self.assertFalse(result.ok)
        self.assertIn("invalid expect_body_regex", result.error)

    def test_non_utf8_body_does_not_crash(self):
        result = check_http("api", self.url("/binary"),
                            expect_body="missing")
        self.assertFalse(result.ok)
        self.assertIn("body does not contain", result.error)

    def test_no_assertion_unchanged(self):
        result = check_http("api", self.url("/error-page"))
        self.assertTrue(result.ok)


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

    def test_valid_assertion_config_loads(self):
        cfg = self._load([{
            "name": "api", "type": "http",
            "url": "http://127.0.0.1:1/",
            "expect_body": "ok",
            "expect_body_regex": r"ok",
        }])
        self.assertEqual(cfg["endpoints"][0]["expect_body"], "ok")

    def test_bad_regex_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._load([{
                "name": "api", "type": "http",
                "url": "http://127.0.0.1:1/",
                "expect_body_regex": r"(",
            }])
        self.assertIn("does not compile", str(ctx.exception))

    def test_non_string_expect_body_rejected(self):
        with self.assertRaises(ValueError):
            self._load([{
                "name": "api", "type": "http",
                "url": "http://127.0.0.1:1/",
                "expect_body": 42,
            }])

    def test_body_assertion_on_tcp_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._load([{
                "name": "db", "type": "tcp",
                "host": "127.0.0.1", "port": 5432,
                "expect_body": "ok",
            }])
        self.assertIn("only applies to http", str(ctx.exception))

    def test_check_endpoint_passes_assertion_through(self):
        server = HTTPServer(("127.0.0.1", 0), _BodyHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = check_endpoint({
                "name": "api", "type": "http",
                "url": f"http://127.0.0.1:{port}/error-page",
                "expect_body": "ok",
            })
        finally:
            server.shutdown()
            server.server_close()
        self.assertFalse(result.ok)
        self.assertIn("body does not contain", result.error)


if __name__ == "__main__":
    unittest.main()
