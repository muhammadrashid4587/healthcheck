"""Tests for the endpoint checkers."""

import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from healthcheck.checker import check_endpoint, check_http, check_tcp


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CheckerTests(unittest.TestCase):
    def test_http_up_for_local_server(self):
        server = HTTPServer(("127.0.0.1", 0), _OkHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = check_http("local", f"http://127.0.0.1:{port}/")
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertGreaterEqual(result.latency_ms, 0.0)

    def test_http_down_for_closed_port(self):
        result = check_http("closed", f"http://127.0.0.1:{_free_port()}/")
        self.assertFalse(result.ok)
        self.assertIsNone(result.status_code)
        self.assertIsNotNone(result.error)

    def test_tcp_up_and_down(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            self.assertTrue(check_tcp("tcp-up", "127.0.0.1", port).ok)
        finally:
            listener.close()
        self.assertFalse(check_tcp("tcp-down", "127.0.0.1", port).ok)

    def test_check_endpoint_dispatch(self):
        server = HTTPServer(("127.0.0.1", 0), _OkHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            http_result = check_endpoint(
                {"name": "web", "type": "http", "url": f"http://127.0.0.1:{port}/"})
            tcp_result = check_endpoint(
                {"name": "svc", "type": "tcp", "host": "127.0.0.1", "port": port})
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(http_result.ok)
        self.assertTrue(tcp_result.ok)

    def test_check_endpoint_unknown_type(self):
        with self.assertRaises(ValueError):
            check_endpoint({"name": "x", "type": "udp", "url": "http://x/"})


if __name__ == "__main__":
    unittest.main()
