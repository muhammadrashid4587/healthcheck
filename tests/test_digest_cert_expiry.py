"""Tests for the certificate-expiry section in the periodic digest.

The digest carries a cert-expiry section built from the same
cert_expiry_snapshot the --cert-expiry-report prints — soonest
first, expired first, no-certificate-seen last, with the summary
counts — so the digest, the report, and the dashboard can never
disagree. The section rides along on a digest that goes out for
another reason; a certificate on its own never triggers a send.
"""

import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import (BaseHTTPRequestHandler, HTTPServer,
                         ThreadingHTTPServer)
from pathlib import Path

import healthcheck.digest as digest_mod
from healthcheck.checker import CheckResult
from healthcheck.cli import monitor_loop
from healthcheck.digest import (DigestAlerter, build_digest,
                                cert_expiry_summary,
                                render_digest_email)
from healthcheck.server import cert_expiry_snapshot
from healthcheck.store import History

NOW = 1_700_000_000.0
DAY = 24 * 3600

OPENSSL = shutil.which("openssl")


def _result(name, ok, at, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=10.0,
                       checked_at=at, **kwargs)


def _history():
    """Seed: stale's cert lapsed 2 days ago; edge expires in 5 days
    inside its 7-day warning window; api expires in 12 days inside
    its 30-day window; far expires in 20 days, outside its 7-day
    window; web expires in 89 days; db never showed a certificate
    (plain TCP)."""
    hist = History()
    hist.record(_result("stale", True, NOW,
                        cert_expires_at=NOW - 2 * DAY))
    hist.record(_result("edge", True, NOW,
                        cert_expires_at=NOW + 5 * DAY))
    hist.record(_result("api", True, NOW,
                        cert_expires_at=NOW + 12 * DAY))
    hist.record(_result("far", True, NOW,
                        cert_expires_at=NOW + 20 * DAY))
    hist.record(_result("web", True, NOW,
                        cert_expires_at=NOW + 89 * DAY))
    hist.record(_result("db", True, NOW))
    return hist


ENDPOINTS = [
    {"name": "stale", "type": "http", "url": "https://example.com"},
    {"name": "edge", "type": "http", "url": "https://example.com",
     "cert_expiry_warn_days": 7},
    {"name": "api", "type": "http", "url": "https://example.com"},
    {"name": "far", "type": "http", "url": "https://example.com",
     "cert_expiry_warn_days": 7},
    {"name": "web", "type": "http", "url": "https://example.com"},
    {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
]


def _snapshot():
    return cert_expiry_snapshot(_history(), ENDPOINTS, now=NOW)


def _change(name, ok, at, status=None, error=None):
    return {
        "endpoint": name, "event": "recovered" if ok else "down",
        "ok": ok, "previous_ok": not ok, "latency_ms": 12.3,
        "status_code": status, "error": error, "checked_at": at,
    }


class CertExpirySummaryTests(unittest.TestCase):
    def test_counts_match_the_report(self):
        self.assertEqual(cert_expiry_summary(_snapshot()), {
            "expired": 1, "expiring_soon": 2, "ok": 2, "unknown": 1,
        })

    def test_empty_snapshot_counts_zero(self):
        snapshot = cert_expiry_snapshot(History(), [], now=NOW)
        self.assertEqual(cert_expiry_summary(snapshot), {
            "expired": 0, "expiring_soon": 0, "ok": 0, "unknown": 0,
        })


class BuildDigestCertExpiryTests(unittest.TestCase):
    def test_payload_carries_snapshot_rows_soonest_first(self):
        digest = build_digest(
            [_change("api", False, at=1000.0)], ["api"],
            1000.0, 1900.0, cert_expiry=_snapshot())
        cert = digest["cert_expiry"]
        self.assertEqual(cert["generated_at"], NOW)
        self.assertEqual(
            [row["name"] for row in cert["endpoints"]],
            ["stale", "edge", "api", "far", "web", "db"])
        by_name = {row["name"]: row for row in cert["endpoints"]}
        self.assertEqual(by_name["stale"]["text"], "expired")
        self.assertEqual(by_name["edge"]["text"], "5d (soon)")
        self.assertEqual(by_name["api"]["text"], "12d (soon)")
        self.assertEqual(by_name["far"]["text"], "20d")
        self.assertEqual(by_name["web"]["text"], "89d")
        self.assertEqual(by_name["db"]["text"], "-")
        self.assertIsNone(by_name["db"]["days_remaining"])
        self.assertEqual(cert["summary"], {
            "expired": 1, "expiring_soon": 2, "ok": 2, "unknown": 1,
        })

    def test_no_snapshot_means_no_section(self):
        digest = build_digest([], [], 0.0, 60.0)
        self.assertNotIn("cert_expiry", digest)
        # And the existing summary shape is untouched.
        self.assertEqual(digest["summary"], {
            "changes": 0, "down": 0, "recovered": 0,
            "currently_down": 0, "flapping": 0,
        })


class RenderEmailCertExpiryTests(unittest.TestCase):
    def test_email_lists_certs_soonest_first_with_summary(self):
        digest = build_digest(
            [_change("api", False, at=NOW)], ["api"],
            NOW, NOW + 900.0, cert_expiry=_snapshot())
        text = render_digest_email(digest, "monitor@example.com",
                                   ["ops@example.com"])
        self.assertIn("Certificate expiry (6):", text)
        body = text[text.index("Certificate expiry (6):"):]
        self.assertLess(body.index("stale: expired"),
                        body.index("edge: 5d (soon)"))
        self.assertLess(body.index("edge: 5d (soon)"),
                        body.index("api: 12d (soon)"))
        self.assertLess(body.index("web: 89d"),
                        body.index("db: - (no certificate seen)"))
        self.assertIn("edge: 5d (soon) (warn 7d)", text)
        self.assertIn("api: 12d (soon) (warn 30d)", text)
        self.assertIn("Summary: 1 expired, 2 expiring soon, "
                      "2 ok, 1 unknown", text)

    def test_email_without_snapshot_has_no_section(self):
        digest = build_digest(
            [_change("api", False, at=10.0)], ["api"], 0.0, 60.0)
        text = render_digest_email(digest, "monitor@example.com",
                                   ["ops@example.com"])
        self.assertNotIn("Certificate expiry", text)


class DigestAlerterCertExpiryTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = digest_mod.post_alert
        digest_mod.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        digest_mod.post_alert = self._real_post

    def _alerter(self, routes=None, **overrides):
        cfg = {"interval_seconds": 900,
               "webhook_url": "https://hooks.example/digest"}
        cfg.update(overrides)
        return DigestAlerter(cfg, now_fn=lambda: 1000.0, routes=routes)

    def test_webhook_payload_and_email_carry_the_section(self):
        alerter = self._alerter(email_to=["ops@example.com"])
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0))
        result = alerter.maybe_send(now=2000.0, current_down=["api"],
                                    cert_expiry=_snapshot())
        self.assertIsNotNone(result)
        self.assertEqual(len(self.sent), 1)
        _, payload = self.sent[0]
        self.assertEqual(
            [row["name"] for row in payload["cert_expiry"]["endpoints"]],
            ["stale", "edge", "api", "far", "web", "db"])
        self.assertEqual(payload["cert_expiry"]["summary"], {
            "expired": 1, "expiring_soon": 2, "ok": 2, "unknown": 1,
        })
        self.assertIn("Certificate expiry (6):", result["email"])
        self.assertIn("stale: expired", result["email"])
        self.assertIn("Summary: 1 expired, 2 expiring soon, "
                      "2 ok, 1 unknown", result["email"])

    def test_expired_cert_alone_does_not_trigger_a_send(self):
        # The section rides along on a digest that goes out for
        # another reason; a quiet window still sends nothing.
        alerter = self._alerter()
        self.assertIsNone(alerter.maybe_send(
            now=2000.0, current_down=[], cert_expiry=_snapshot()))
        self.assertEqual(self.sent, [])

    def test_routed_digests_each_carry_the_full_cert_section(self):
        # The changes and currently-down lists split by routing,
        # but the certificate section is the whole estate in every
        # POST — a routed digest must not disagree with the report's
        # summary counts.
        alerter = self._alerter(
            routes={"api": "https://hooks.example/api"})
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0, error="boom"))
        alerter.notify(_result("db", True, at=1000.0))
        alerter.notify(_result("db", False, at=1150.0, error="boom"))
        result = alerter.maybe_send(now=2000.0,
                                    current_down=["api", "db"],
                                    cert_expiry=_snapshot())
        self.assertIsNotNone(result)
        self.assertEqual(len(self.sent), 2)
        by_url = {url: payload for url, payload in self.sent}
        for url, payload in by_url.items():
            self.assertEqual(
                [row["name"] for row in
                 payload["cert_expiry"]["endpoints"]],
                ["stale", "edge", "api", "far", "web", "db"])
            self.assertEqual(payload["cert_expiry"]["summary"], {
                "expired": 1, "expiring_soon": 2, "ok": 2,
                "unknown": 1,
            })
        # The split still applies to the changes themselves.
        self.assertEqual(
            [c["endpoint"] for c in
             by_url["https://hooks.example/api"]["changes"]],
            ["api"])
        self.assertEqual(
            [c["endpoint"] for c in
             by_url["https://hooks.example/digest"]["changes"]],
            ["db"])
        self.assertEqual(
            len(result["digest"]["cert_expiry"]["endpoints"]), 6)


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records POST bodies as parsed JSON."""
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append(json.loads(body))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _run_capture_server():
    _CaptureHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


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

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(OPENSSL, "openssl is required to generate test certs")
class MonitorLoopDigestCertExpirySmokeTests(unittest.TestCase):
    """Live smoke: the monitor loop's periodic digest POSTs a
    cert-expiry section built from the checks it just ran — the
    TLS endpoint's real 2-day certificate shows as expiring soon,
    the closed TCP endpoint as no certificate seen."""

    def test_loop_digest_posts_cert_expiry_section(self):
        server = _run_capture_server()
        try:
            hook_url = (f"http://127.0.0.1:{server.server_port}/hook")
            with tempfile.TemporaryDirectory() as tmpdir:
                paths = _make_certs(tmpdir, days="2")
                with _TLSServer(paths) as tls:
                    cfg = {
                        "interval_seconds": 0.05,
                        "webhook_url": hook_url,
                        "alert_digest": {
                            "interval_seconds": 1,
                            "webhook_url": hook_url,
                        },
                        "endpoints": [
                            {"name": "tls", "type": "http",
                             "url": tls.url, "cafile": paths["ca.crt"]},
                            {"name": "closed", "type": "tcp",
                             "host": "127.0.0.1", "port": 1,
                             "timeout": 1},
                        ],
                    }
                    monitor_loop(cfg, History(), None, None,
                                 threading.Event(), max_rounds=60)
        finally:
            server.shutdown()
            server.server_close()
        digests = [p for p in _CaptureHandler.received
                   if p.get("type") == "digest"]
        self.assertTrue(digests, "no digest was POSTed")
        cert = digests[-1]["cert_expiry"]
        by_name = {row["name"]: row for row in cert["endpoints"]}
        self.assertEqual(sorted(by_name), ["closed", "tls"])
        # The TLS cert expires in 2 days: inside the 30-day window.
        self.assertTrue(by_name["tls"]["text"].endswith("(soon)"),
                        by_name["tls"]["text"])
        self.assertIn(by_name["tls"]["days_remaining"], (1, 2))
        self.assertEqual(by_name["closed"]["text"], "-")
        self.assertIsNone(by_name["closed"]["days_remaining"])
        self.assertEqual(cert["summary"], {
            "expired": 0, "expiring_soon": 1, "ok": 0, "unknown": 1,
        })
        # The closed endpoint is down, so the digest went out for a
        # real reason — the cert section rode along on it.
        self.assertEqual(digests[-1]["currently_down"], ["closed"])


if __name__ == "__main__":
    unittest.main()
