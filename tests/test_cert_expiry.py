"""Tests for TLS certificate-expiry warnings.

Every https check reads the peer certificate's expiry (one extra
short TLS handshake) and records it with the check; the dashboard
gains a Cert expires column, GET /api/status gains cert_expiry_*
fields, and GET /metrics gains a healthcheck_cert_expiry_days
gauge. An expiring cert is a warning, never downtime: the check's
verdict does not change. Certs are generated on the fly with
`openssl` into a temp dir and served by a local TLS server — no
live external server is involved. Skipped entirely when openssl
is not installed.
"""

import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from healthcheck.checker import (DEFAULT_CERT_EXPIRY_WARN_DAYS,
                                 cert_expiring_soon, cert_expiry_days,
                                 check_http, fetch_cert_expiry,
                                 parse_cert_not_after)
from healthcheck.cli import validate_cert_expiry_warnings
from healthcheck.server import (render_dashboard, render_metrics,
                                status_snapshot)
from healthcheck.store import History

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


def _make_certs(tmpdir, days="2"):
    """A test CA and a server cert for 127.0.0.1, valid `days` days."""
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
                  "server.crt", "-days", days, "-extfile", "server.ext"],
                 d)
    return {name: str(d / name) for name in
            ("ca.crt", "server.crt", "server.key")}


class _TLSServer:
    """An HTTPS server on 127.0.0.1."""

    def __init__(self, paths):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(paths["server.crt"], paths["server.key"])
        context.load_verify_locations(cafile=paths["ca.crt"])
        self.server = HTTPServer(("127.0.0.1", 0), _OkHandler)
        self.server.socket = context.wrap_socket(
            self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    @property
    def url(self):
        return f"https://127.0.0.1:{self.server.server_address[1]}/"

    @property
    def port(self):
        return self.server.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class CertParsingTests(unittest.TestCase):
    def test_parse_not_after(self):
        expires = parse_cert_not_after(
            {"notAfter": "Nov 24 12:00:00 2028 GMT"})
        self.assertIsNotNone(expires)
        # 2028-11-24 12:00 UTC, as an epoch.
        self.assertAlmostEqual(expires, 1858680000.0, delta=1)

    def test_parse_not_after_missing_or_bad(self):
        self.assertIsNone(parse_cert_not_after({}))
        self.assertIsNone(parse_cert_not_after(None))
        self.assertIsNone(parse_cert_not_after(
            {"notAfter": "not a date"}))
        self.assertIsNone(parse_cert_not_after({"notAfter": 123}))

    def test_cert_expiry_days(self):
        now = time.time()
        self.assertEqual(cert_expiry_days(now + 90 * 86400, now=now), 90)
        self.assertEqual(cert_expiry_days(now - 86400, now=now), -1)
        self.assertIsNone(cert_expiry_days(None, now=now))

    def test_cert_expiring_soon(self):
        now = time.time()
        self.assertTrue(cert_expiring_soon(now + 5 * 86400, 30, now=now))
        self.assertFalse(cert_expiring_soon(now + 90 * 86400, 30,
                                            now=now))
        # Already expired counts: it is within (past) the window.
        self.assertTrue(cert_expiring_soon(now - 86400, 30, now=now))
        # Unknown expiry is not a warning, it is just unknown.
        self.assertFalse(cert_expiring_soon(None, 30, now=now))
        self.assertEqual(DEFAULT_CERT_EXPIRY_WARN_DAYS, 30)


@unittest.skipUnless(OPENSSL, "openssl is required to generate test certs")
class CertExpiryCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.paths = _make_certs(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_fetch_cert_expiry_local_tls(self):
        with _TLSServer(self.paths) as server:
            context = ssl.create_default_context(
                cafile=self.paths["ca.crt"])
            expires = fetch_cert_expiry("127.0.0.1", server.port,
                                        context)
        self.assertIsNotNone(expires)
        # The test cert is valid 2 days; allow a wide margin for
        # slow test runs.
        self.assertGreater(expires, time.time())
        self.assertLess(expires, time.time() + 3 * 86400)

    def test_fetch_cert_expiry_failure_is_none(self):
        # Nothing listening: no exception, just unknown expiry.
        self.assertIsNone(fetch_cert_expiry("127.0.0.1", 1))

    def test_check_http_records_cert_expiry(self):
        with _TLSServer(self.paths) as server:
            result = check_http("tls", server.url,
                                cafile=self.paths["ca.crt"])
        self.assertTrue(result.ok)
        self.assertEqual(result.status_code, 200)
        self.assertIsNotNone(result.cert_expires_at)
        self.assertGreater(result.cert_expires_at, time.time())
        self.assertLess(result.cert_expires_at,
                        time.time() + 3 * 86400)

    def test_check_http_failed_tls_has_no_cert_expiry(self):
        with _TLSServer(self.paths) as server:
            result = check_http("tls", server.url)
        self.assertFalse(result.ok)
        self.assertIsNone(result.cert_expires_at)

    def test_check_http_plain_has_no_cert_expiry(self):
        server = HTTPServer(("127.0.0.1", 0), _OkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            result = check_http(
                "plain", f"http://127.0.0.1:{server.server_address[1]}/")
        finally:
            server.shutdown()
            server.server_close()
        self.assertTrue(result.ok)
        self.assertIsNone(result.cert_expires_at)


class CertExpiryValidationTests(unittest.TestCase):
    def test_good_values_pass(self):
        validate_cert_expiry_warnings([
            {"name": "a", "type": "http", "url": "https://example.com",
             "cert_expiry_warn_days": 14},
            {"name": "b", "type": "http", "url": "https://example.com",
             "cert_expiry_warn_days": 7.5},
            {"name": "c", "type": "http", "url": "https://example.com"},
        ])

    def test_bad_values_rejected(self):
        for value in (0, -5, "30", True, [30]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_cert_expiry_warnings([
                        {"name": "a", "type": "http",
                         "url": "https://example.com",
                         "cert_expiry_warn_days": value}])

    def test_tcp_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            validate_cert_expiry_warnings([
                {"name": "db", "type": "tcp", "host": "127.0.0.1",
                 "port": 5432, "cert_expiry_warn_days": 30}])


class CertExpirySurfaceTests(unittest.TestCase):
    def _history_with_cert(self, expires_at):
        from healthcheck.checker import CheckResult
        hist = History()
        hist.record(CheckResult(name="api", ok=True, latency_ms=12.3,
                                status_code=200,
                                checked_at=time.time(),
                                cert_expires_at=expires_at))
        return hist

    def test_history_records_cert_expiry(self):
        now = time.time()
        hist = self._history_with_cert(now + 90 * 86400)
        self.assertAlmostEqual(hist.cert_expiry("api"),
                               now + 90 * 86400, delta=1)
        self.assertIsNone(History().cert_expiry("api"))

    def test_status_snapshot_cert_fields(self):
        now = time.time()
        hist = self._history_with_cert(now + 5 * 86400)
        endpoints = [{"name": "api", "type": "http",
                      "url": "https://example.com"}]
        snap = status_snapshot(hist, endpoints, now=now)
        row = snap["endpoints"][0]
        self.assertAlmostEqual(row["cert_expires_at"],
                               now + 5 * 86400, delta=1)
        self.assertEqual(row["cert_expiry_days"], 5)
        # Five days out is inside the default 30-day window.
        self.assertTrue(row["cert_expiry_warning"])
        self.assertEqual(row["cert_expiry_warn_days"], 30)
        self.assertEqual(row["status"], "UP")

    def test_status_snapshot_no_cert_is_not_a_warning(self):
        hist = History()
        from healthcheck.checker import CheckResult
        hist.record(CheckResult(name="db", ok=True, latency_ms=1.0,
                                checked_at=time.time()))
        snap = status_snapshot(
            hist, [{"name": "db", "type": "tcp", "host": "127.0.0.1",
                    "port": 5432}])
        row = snap["endpoints"][0]
        self.assertIsNone(row["cert_expires_at"])
        self.assertIsNone(row["cert_expiry_days"])
        self.assertFalse(row["cert_expiry_warning"])

    def test_status_snapshot_custom_warn_window(self):
        now = time.time()
        hist = self._history_with_cert(now + 5 * 86400)
        endpoints = [{"name": "api", "type": "http",
                      "url": "https://example.com",
                      "cert_expiry_warn_days": 3}]
        snap = status_snapshot(hist, endpoints, now=now)
        row = snap["endpoints"][0]
        self.assertEqual(row["cert_expiry_warn_days"], 3)
        self.assertFalse(row["cert_expiry_warning"])

    def test_dashboard_cert_column(self):
        now = time.time()
        hist = self._history_with_cert(now + 90 * 86400)
        page = render_dashboard(
            hist, [{"name": "api", "type": "http",
                    "url": "https://example.com"}], now=now)
        self.assertIn("Cert expires", page)
        self.assertIn("90d", page)
        self.assertNotIn("(soon)", page)

    def test_dashboard_cert_column_soon_and_expired(self):
        now = time.time()
        hist = self._history_with_cert(now + 5 * 86400)
        page = render_dashboard(
            hist, [{"name": "api", "type": "http",
                    "url": "https://example.com"}], now=now)
        self.assertIn("5d (soon)", page)
        hist = self._history_with_cert(now - 86400)
        page = render_dashboard(
            hist, [{"name": "api", "type": "http",
                    "url": "https://example.com"}], now=now)
        self.assertIn("expired", page)

    def test_metrics_cert_gauge(self):
        now = time.time()
        hist = self._history_with_cert(now + 90 * 86400)
        text = render_metrics(
            hist, [{"name": "api", "type": "http",
                    "url": "https://example.com"}], now=now)
        self.assertIn("healthcheck_cert_expiry_days", text)
        self.assertIn('healthcheck_cert_expiry_days{name="api"} 90', text)

    def test_metrics_no_gauge_without_cert(self):
        hist = History()
        text = render_metrics(
            hist, [{"name": "db", "type": "tcp", "host": "127.0.0.1",
                    "port": 5432}])
        self.assertNotIn("healthcheck_cert_expiry_days{name=", text)


if __name__ == "__main__":
    unittest.main()
