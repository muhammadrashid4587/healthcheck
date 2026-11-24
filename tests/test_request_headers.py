"""Tests for per-endpoint custom request headers (`headers`).

An http endpoint can send extra headers with its checks (e.g. an
Authorization token for a health endpoint behind auth). The values
are secrets, so the dashboard and GET /api/status show only the
header names, never the values.
"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from healthcheck.checker import (check_endpoint, check_http,
                                 normalize_headers)
from healthcheck.cli import load_config, validate_request_headers
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History


class _HeadersHandler(BaseHTTPRequestHandler):
    """Records the request headers of the last request; answers 200."""

    seen = []

    def _answer(self):
        _HeadersHandler.seen.append(dict(self.headers.items()))
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer

    def log_message(self, *args):
        pass


def _serve():
    server = HTTPServer(("127.0.0.1", 0), _HeadersHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class RequestHeadersCheckerTests(unittest.TestCase):
    def setUp(self):
        _HeadersHandler.seen = []

    def test_headers_are_sent(self):
        server = _serve()
        try:
            result = check_http(
                "web", f"http://127.0.0.1:{server.server_address[1]}/",
                headers={"X-Api-Key": "secret", "X-Tenant": "acme"})
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(len(_HeadersHandler.seen), 1)
        sent = _HeadersHandler.seen[0]
        self.assertEqual(sent.get("X-Api-Key"), "secret")
        self.assertEqual(sent.get("X-Tenant"), "acme")

    def test_no_headers_sends_none_extra(self):
        server = _serve()
        try:
            result = check_http(
                "web", f"http://127.0.0.1:{server.server_address[1]}/")
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        sent = _HeadersHandler.seen[0]
        self.assertNotIn("X-Api-Key", sent)

    def test_headers_work_with_custom_method(self):
        server = _serve()
        try:
            result = check_http(
                "web", f"http://127.0.0.1:{server.server_address[1]}/",
                method="POST", headers={"Authorization": "Bearer abc"})
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(_HeadersHandler.seen[0].get("Authorization"),
                         "Bearer abc")

    def test_invalid_headers_raise(self):
        for bad in ("Authorization: Bearer x", ["X-Api-Key"],
                    {"X-Api-Key": 7}, {"X-Api-Key": None},
                    {7: "secret"}, {"": "secret"}, {"  ": "secret"}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_headers(bad)
            with self.assertRaises(ValueError, msg=repr(bad)):
                check_http("web", "http://127.0.0.1:1/", headers=bad)

    def test_check_endpoint_passes_headers_through(self):
        server = _serve()
        try:
            result = check_endpoint(
                {"name": "web", "type": "http",
                 "url": f"http://127.0.0.1:{server.server_address[1]}/",
                 "headers": {"X-Api-Key": "secret"}})
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(_HeadersHandler.seen[0].get("X-Api-Key"),
                         "secret")


class RequestHeadersValidationTests(unittest.TestCase):
    def test_bad_headers_rejected(self):
        for bad in ("not-a-dict", {"X-Api-Key": 7}, {7: "x"},
                    {"": "x"}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_request_headers([
                    {"name": "web", "type": "http", "url": "http://x",
                     "headers": bad}])

    def test_headers_on_tcp_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            validate_request_headers([
                {"name": "db", "type": "tcp", "host": "127.0.0.1",
                 "port": 5432, "headers": {"X-Api-Key": "secret"}}])

    def test_valid_headers_accepted_at_load(self):
        cfg = {"endpoints": [
            {"name": "web", "type": "http", "url": "http://x",
             "headers": {"Authorization": "Bearer abc",
                         "X-Api-Key": "secret"}}]}
        with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            path = fh.name
        loaded = load_config(path)
        self.assertEqual(loaded["endpoints"][0]["headers"],
                         {"Authorization": "Bearer abc",
                          "X-Api-Key": "secret"})

    def test_no_headers_is_fine(self):
        validate_request_headers([
            {"name": "web", "type": "http", "url": "http://x"}])


class RequestHeadersSnapshotTests(unittest.TestCase):
    def test_snapshot_lists_header_names_only(self):
        history = History()
        endpoints = [
            {"name": "web", "type": "http", "url": "http://x",
             "headers": {"Authorization": "Bearer abc",
                         "X-Api-Key": "secret"}},
            {"name": "plain", "type": "http", "url": "http://y"},
        ]
        snap = status_snapshot(history, endpoints)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["web"]["headers"],
                         ["Authorization", "X-Api-Key"])
        self.assertIsNone(by_name["plain"]["headers"])
        # The secret values never appear in the API payload.
        self.assertNotIn("Bearer abc", json.dumps(snap))
        self.assertNotIn("secret", json.dumps(snap))

    def test_dashboard_shows_header_names_not_values(self):
        history = History()
        endpoints = [
            {"name": "web", "type": "http", "url": "http://x",
             "headers": {"Authorization": "Bearer abc"}},
            {"name": "plain", "type": "http", "url": "http://y"},
        ]
        page = render_dashboard(history, endpoints)
        self.assertIn("<th>Headers</th>", page)
        self.assertIn("<td>Authorization</td>", page)
        self.assertIn("<td>-</td>", page)
        self.assertNotIn("Bearer abc", page)


if __name__ == "__main__":
    unittest.main()
