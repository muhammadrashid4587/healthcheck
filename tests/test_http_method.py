"""Tests for per-endpoint HTTP methods (`method` in the config)."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from healthcheck.checker import check_endpoint, check_http
from healthcheck.cli import load_config, validate_http_methods
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History


class _MethodHandler(BaseHTTPRequestHandler):
    """Records the method of the last request; answers 200 to any."""

    seen = []

    def _answer(self):
        _MethodHandler.seen.append(self.command)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = _answer
    do_HEAD = _answer
    do_POST = _answer
    do_PUT = _answer
    do_DELETE = _answer
    do_PATCH = _answer
    do_OPTIONS = _answer

    def log_message(self, *args):
        pass


def _serve():
    server = HTTPServer(("127.0.0.1", 0), _MethodHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class HttpMethodCheckerTests(unittest.TestCase):
    def setUp(self):
        _MethodHandler.seen = []

    def test_default_check_is_get(self):
        server = _serve()
        try:
            result = check_http(
                "web", f"http://127.0.0.1:{server.server_address[1]}/")
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(_MethodHandler.seen, ["GET"])

    def test_head_check_sends_head(self):
        server = _serve()
        try:
            result = check_http(
                "web", f"http://127.0.0.1:{server.server_address[1]}/",
                method="HEAD")
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(_MethodHandler.seen, ["HEAD"])

    def test_post_check_sends_post(self):
        server = _serve()
        try:
            result = check_http(
                "web", f"http://127.0.0.1:{server.server_address[1]}/",
                method="POST")
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(_MethodHandler.seen, ["POST"])

    def test_lowercase_method_is_normalized(self):
        server = _serve()
        try:
            result = check_http(
                "web", f"http://127.0.0.1:{server.server_address[1]}/",
                method="put")
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(_MethodHandler.seen, ["PUT"])

    def test_invalid_method_raises(self):
        for bad in ("TRACE", "CONNECT", "", "  ", 123, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                check_http("web", "http://127.0.0.1:1/", method=bad)

    def test_check_endpoint_passes_method_through(self):
        server = _serve()
        try:
            result = check_endpoint(
                {"name": "web", "type": "http",
                 "url": f"http://127.0.0.1:{server.server_address[1]}/",
                 "method": "DELETE"})
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(_MethodHandler.seen, ["DELETE"])


class HttpMethodValidationTests(unittest.TestCase):
    def test_bad_method_rejected(self):
        with self.assertRaises(ValueError):
            validate_http_methods([
                {"name": "web", "type": "http",
                 "url": "http://x", "method": "TRACE"}])

    def test_non_string_method_rejected(self):
        with self.assertRaises(ValueError):
            validate_http_methods([
                {"name": "web", "type": "http",
                 "url": "http://x", "method": 7}])

    def test_method_on_tcp_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            validate_http_methods([
                {"name": "db", "type": "tcp", "host": "127.0.0.1",
                 "port": 5432, "method": "GET"}])

    def test_head_with_body_assertion_rejected(self):
        with self.assertRaises(ValueError):
            validate_http_methods([
                {"name": "web", "type": "http", "url": "http://x",
                 "method": "HEAD", "expect_body": "ok"}])
        with self.assertRaises(ValueError):
            validate_http_methods([
                {"name": "web", "type": "http", "url": "http://x",
                 "method": "HEAD", "expect_body_regex": "o."}])

    def test_lowercase_method_normalized_at_load(self):
        cfg = {"endpoints": [
            {"name": "web", "type": "http", "url": "http://x",
             "method": "head"}]}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        loaded = load_config(path)
        self.assertEqual(loaded["endpoints"][0]["method"], "HEAD")

    def test_no_method_is_fine(self):
        validate_http_methods([
            {"name": "web", "type": "http", "url": "http://x"}])


class HttpMethodSnapshotTests(unittest.TestCase):
    def test_snapshot_and_dashboard_show_method(self):
        history = History()
        endpoints = [
            {"name": "web", "type": "http", "url": "http://x",
             "method": "HEAD"},
            {"name": "plain", "type": "http", "url": "http://y"},
        ]
        snap = status_snapshot(history, endpoints)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["web"]["method"], "HEAD")
        self.assertIsNone(by_name["plain"]["method"])
        page = render_dashboard(history, endpoints)
        self.assertIn("<th>Method</th>", page)
        self.assertIn("<td>HEAD</td>", page)
        self.assertIn("<td>-</td>", page)


if __name__ == "__main__":
    unittest.main()
