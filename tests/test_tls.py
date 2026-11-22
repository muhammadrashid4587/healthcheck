"""Tests for HTTPS checks with a custom CA and client certificates.

Certs are generated on the fly with `openssl` into a temp dir and
served by a local TLS server — no live external server is involved.
Skipped entirely when openssl is not installed.
"""

import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from healthcheck.checker import check_endpoint, check_http

OPENSSL = shutil.which("openssl")


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _run_openssl(args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True,
                   capture_output=True)


def _make_certs(tmpdir):
    """A test CA, a server cert for 127.0.0.1, and a client cert."""
    d = Path(tmpdir)
    _run_openssl(["req", "-x509", "-newkey", "rsa:2048", "-nodes",
                  "-keyout", "ca.key", "-out", "ca.crt", "-days", "2",
                  "-subj", "/CN=test-ca"], d)
    _run_openssl(["req", "-newkey", "rsa:2048", "-nodes",
                  "-keyout", "server.key", "-out", "server.csr",
                  "-subj", "/CN=127.0.0.1"], d)
    (d / "server.ext").write_text(
        "subjectAltName=IP:127.0.0.1\n", encoding="utf-8")
    _run_openssl(["x509", "-req", "-in", "server.csr", "-CA", "ca.crt",
                  "-CAkey", "ca.key", "-CAcreateserial", "-out",
                  "server.crt", "-days", "2", "-extfile", "server.ext"], d)
    _run_openssl(["req", "-newkey", "rsa:2048", "-nodes",
                  "-keyout", "client.key", "-out", "client.csr",
                  "-subj", "/CN=test-client"], d)
    _run_openssl(["x509", "-req", "-in", "client.csr", "-CA", "ca.crt",
                  "-CAkey", "ca.key", "-CAcreateserial", "-out",
                  "client.crt", "-days", "2"], d)
    return {name: str(d / name) for name in
            ("ca.crt", "server.crt", "server.key",
             "client.crt", "client.key")}


class _TLSServer:
    """An HTTPS server on 127.0.0.1, optionally requiring a client cert."""

    def __init__(self, paths, require_client_cert=False):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(paths["server.crt"], paths["server.key"])
        context.load_verify_locations(cafile=paths["ca.crt"])
        if require_client_cert:
            context.verify_mode = ssl.CERT_REQUIRED
        self.server = HTTPServer(("127.0.0.1", 0), _OkHandler)
        self.server.socket = context.wrap_socket(
            self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    @property
    def url(self):
        return f"https://127.0.0.1:{self.server.server_address[1]}/"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(OPENSSL, "openssl is required to generate test certs")
class TlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.paths = _make_certs(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_https_up_with_custom_ca(self):
        with _TLSServer(self.paths) as server:
            result = check_http("tls", server.url,
                                cafile=self.paths["ca.crt"])
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)

    def test_https_fails_without_custom_ca(self):
        with _TLSServer(self.paths) as server:
            result = check_http("tls", server.url)
        self.assertFalse(result.ok)
        self.assertIsNone(result.status_code)
        self.assertIn("certificate", result.error.lower())

    def test_https_client_cert(self):
        with _TLSServer(self.paths, require_client_cert=True) as server:
            ok = check_http("mtls", server.url,
                            cafile=self.paths["ca.crt"],
                            cert=self.paths["client.crt"],
                            key=self.paths["client.key"])
            no_cert = check_http("mtls", server.url,
                                 cafile=self.paths["ca.crt"])
        self.assertTrue(ok.ok)
        self.assertEqual(ok.status_code, 200)
        self.assertFalse(no_cert.ok)
        self.assertIsNotNone(no_cert.error)

    def test_bad_cafile_is_a_failed_check_not_a_crash(self):
        with _TLSServer(self.paths) as server:
            result = check_http("tls", server.url,
                                cafile="/nonexistent/ca.crt")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_tls_options_ignored_for_plain_http(self):
        server = HTTPServer(("127.0.0.1", 0), _OkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = check_http(
                "plain", f"http://127.0.0.1:{server.server_address[1]}/",
                cafile=self.paths["ca.crt"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)

    def test_check_endpoint_passes_tls_fields(self):
        with _TLSServer(self.paths) as server:
            result = check_endpoint({
                "name": "tls", "type": "http", "url": server.url,
                "cafile": self.paths["ca.crt"],
                "cert": self.paths["client.crt"],
                "key": self.paths["client.key"]})
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)


if __name__ == "__main__":
    unittest.main()
