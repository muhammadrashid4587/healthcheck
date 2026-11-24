"""Tests for webhook alerting on TLS certificate expiry.

When an endpoint's certificate enters its warning window (its
`cert_expiry_warn_days`, 30 by default), one webhook POST goes out
with the expiry details — and only one per certificate, even across
check rounds and SIGHUP reloads. A renewed certificate fires again
when it enters the window. The endpoint stays UP: the alert is a
warning, not a state change. The alert shows on the dashboard and
in GET /api/status as `cert_expiry_alerted_at`. Certs are generated
on the fly with `openssl` and served by a local TLS server — no
live external server is involved. Skipped entirely when openssl
is not installed.
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

import healthcheck.alerts as alerts
from healthcheck.alerts import (CertExpiryAlerter,
                                cert_expiry_alert_payload)
from healthcheck.checker import CheckResult
from healthcheck.cli import make_cert_alerter, monitor_loop
from healthcheck.server import render_dashboard, status_snapshot
from healthcheck.store import History

OPENSSL = shutil.which("openssl")


def _at(year, month, day, hour=12, minute=0):
    """Epoch seconds for a local-time date, so the tests work in any
    timezone."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def _result(name, ok, at, latency=10.0, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at, **kwargs)


def _history_with_certs(now, expiries):
    """History where each named endpoint's latest check recorded the
    given cert expiry (epoch) — None means no certificate seen."""
    history = History()
    for name, expires_at in expiries.items():
        history.record(_result(name, True, now,
                               cert_expires_at=expires_at))
    return history


_ENDPOINTS = [
    {"name": "api", "type": "http", "url": "https://example.com"},
    {"name": "web", "type": "http", "url": "https://example.com"},
    {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
]


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records POST bodies; status code comes from the server attribute."""
    received = []
    status = 200

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append(
            (self.path, self.headers.get("Content-Type"), body))
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _run_capture_server(status=200):
    _CaptureHandler.received = []
    _CaptureHandler.status = status
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class PayloadTests(unittest.TestCase):
    def test_cert_expiry_payload(self):
        self.assertEqual(
            cert_expiry_alert_payload("api", 1_800_000_000.0, 5, 30,
                                      1_700_000_000.0),
            {
                "type": "cert_expiry",
                "endpoint": "api",
                "cert_expires_at": 1_800_000_000.0,
                "days_remaining": 5,
                "warn_days": 30,
                "checked_at": 1_700_000_000.0,
            })


class CertExpiryAlerterTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.history = _history_with_certs(self.now, {
            "api": self.now + 5 * 86400,   # inside the 30-day window
            "web": self.now + 90 * 86400,  # outside it
            "db": None,                    # tcp: no certificate
        })
        self._real_post = alerts.post_alert
        self.sent = []
        alerts.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts.post_alert = self._real_post

    def test_fires_once_for_the_endpoint_inside_the_window(self):
        alerter = CertExpiryAlerter("http://hooks.example/cert")
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["type"], "cert_expiry")
        self.assertEqual(sent[0]["endpoint"], "api")
        self.assertAlmostEqual(sent[0]["cert_expires_at"],
                               self.now + 5 * 86400, delta=1)
        self.assertEqual(sent[0]["days_remaining"], 5)
        self.assertEqual(sent[0]["warn_days"], 30)
        self.assertEqual(self.sent[0][0], "http://hooks.example/cert")
        # The endpoint outside the window and the one with no
        # certificate send nothing.
        self.assertEqual(
            [p["endpoint"] for _, p in self.sent], ["api"])

    def test_does_not_refire_for_the_same_certificate(self):
        alerter = CertExpiryAlerter("http://hooks.example/cert")
        alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now)
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now)
        self.assertEqual(sent, [])
        self.assertEqual(len(self.sent), 1)

    def test_fires_again_after_the_certificate_is_renewed(self):
        alerter = CertExpiryAlerter("http://hooks.example/cert")
        alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now)
        # The cert is renewed: a later expiry, far outside the
        # window — nothing fires while it is fresh.
        renewed = self.now + 400 * 86400
        self.history.record(_result("api", True, self.now,
                                    cert_expires_at=renewed))
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now)
        self.assertEqual(sent, [])
        # Time passes and the renewed cert enters the window: a new
        # entry, so it fires again. (Only the api endpoint is
        # judged here: at `later` the web endpoint's old cert is
        # long expired, which would fire for web too.)
        later = self.now + 390 * 86400
        self.history.record(_result("api", True, later,
                                    cert_expires_at=renewed))
        sent = alerter.maybe_alert(self.history, [_ENDPOINTS[0]],
                                   now=later)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["endpoint"], "api")
        self.assertEqual(sent[0]["days_remaining"], 10)
        self.assertEqual(len(self.sent), 2)

    def test_no_expiry_recorded_sends_nothing(self):
        alerter = CertExpiryAlerter("http://hooks.example/cert")
        sent = alerter.maybe_alert(History(), _ENDPOINTS, now=self.now)
        self.assertEqual(sent, [])
        self.assertEqual(self.sent, [])

    def test_outside_the_window_sends_nothing(self):
        history = _history_with_certs(self.now, {
            "api": self.now + 90 * 86400})
        alerter = CertExpiryAlerter("http://hooks.example/cert")
        self.assertEqual(
            alerter.maybe_alert(history, _ENDPOINTS, now=self.now), [])
        self.assertEqual(self.sent, [])

    def test_custom_warn_window_is_respected(self):
        endpoints = [
            {"name": "api", "type": "http",
             "url": "https://example.com", "cert_expiry_warn_days": 3},
        ]
        alerter = CertExpiryAlerter("http://hooks.example/cert")
        # Five days out is outside a 3-day window.
        self.assertEqual(
            alerter.maybe_alert(self.history, endpoints, now=self.now),
            [])
        endpoints[0]["cert_expiry_warn_days"] = 7
        sent = alerter.maybe_alert(self.history, endpoints, now=self.now)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["warn_days"], 7)

    def test_expired_certificate_fires(self):
        history = _history_with_certs(self.now, {
            "api": self.now - 86400})
        alerter = CertExpiryAlerter("http://hooks.example/cert")
        sent = alerter.maybe_alert(history, _ENDPOINTS, now=self.now)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["days_remaining"], -1)

    def test_per_endpoint_webhook_wins(self):
        alerter = CertExpiryAlerter(
            "http://hooks.example/global",
            routes={"api": "http://hooks.example/api"})
        alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now)
        self.assertEqual(self.sent[0][0], "http://hooks.example/api")

    def test_no_webhook_anywhere_sends_nothing(self):
        alerter = CertExpiryAlerter(None)
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now)
        self.assertEqual(sent, [])
        self.assertEqual(self.sent, [])
        self.assertIsNone(alerter.alerted_for("api"))

    def test_records_the_alert_and_survives_recreation(self):
        records = {}
        alerter = CertExpiryAlerter("http://hooks.example/cert",
                                    records=records)
        alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now)
        self.assertAlmostEqual(records["api"]["cert_expires_at"],
                               self.now + 5 * 86400, delta=1)
        self.assertEqual(records["api"]["warn_days"], 30)
        self.assertEqual(records["api"]["alerted_at"], self.now)
        self.assertEqual(alerter.alerted_for("api"), records["api"])
        self.assertIsNone(alerter.alerted_for("web"))
        # A fresh alerter sharing the records does not re-fire —
        # this is what a SIGHUP reload does.
        fresh = CertExpiryAlerter("http://hooks.example/cert",
                                  records=records)
        self.assertEqual(
            fresh.maybe_alert(self.history, _ENDPOINTS, now=self.now),
            [])
        self.assertEqual(len(self.sent), 1)

    def test_failed_delivery_is_still_recorded_not_resent(self):
        alerts.post_alert = lambda url, payload, timeout=5.0: False
        alerter = CertExpiryAlerter("http://hooks.example/cert")
        sent = alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now)
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            alerter.maybe_alert(self.history, _ENDPOINTS, now=self.now),
            [])


class MakeCertAlerterTests(unittest.TestCase):
    def test_none_when_no_webhook_configured(self):
        cfg = {"endpoints": [{"name": "api", "type": "http",
                              "url": "https://example.com"}]}
        self.assertIsNone(make_cert_alerter(cfg, None))

    def test_exists_when_an_endpoint_has_its_own_webhook(self):
        cfg = {"endpoints": [
            {"name": "api", "type": "http", "url": "https://example.com",
             "webhook": "http://hooks.example/api"}]}
        alerter = make_cert_alerter(cfg, None)
        self.assertIsNotNone(alerter)
        self.assertIsNone(alerter.url)
        self.assertEqual(alerter.url_for("api"),
                         "http://hooks.example/api")


class StatusSnapshotTests(unittest.TestCase):
    def test_alerted_at_in_status_snapshot(self):
        now = time.time()
        history = _history_with_certs(now, {
            "api": now + 5 * 86400, "web": now + 90 * 86400})
        records = {"api": {"cert_expires_at": now + 5 * 86400,
                           "warn_days": 30, "alerted_at": now - 3600}}
        snap = status_snapshot(history, _ENDPOINTS, now=now,
                               cert_alerts=records)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertEqual(by_name["api"]["cert_expiry_alerted_at"],
                         now - 3600)
        # No record for the other endpoint, and no cert at all for
        # the tcp one.
        self.assertIsNone(by_name["web"]["cert_expiry_alerted_at"])
        self.assertIsNone(by_name["db"]["cert_expiry_alerted_at"])
        # A record for a different (renewed) certificate does not
        # count for the current one.
        stale = {"api": {"cert_expires_at": now + 400 * 86400,
                         "warn_days": 30, "alerted_at": now - 3600}}
        snap = status_snapshot(history, _ENDPOINTS, now=now,
                               cert_alerts=stale)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertIsNone(by_name["api"]["cert_expiry_alerted_at"])

    def test_no_record_is_none(self):
        now = time.time()
        history = _history_with_certs(now, {"api": now + 5 * 86400})
        snap = status_snapshot(history, _ENDPOINTS, now=now)
        by_name = {ep["name"]: ep for ep in snap["endpoints"]}
        self.assertIsNone(by_name["api"]["cert_expiry_alerted_at"])
        # The warning itself still shows, alert or not.
        self.assertTrue(by_name["api"]["cert_expiry_warning"])


class DashboardTests(unittest.TestCase):
    def test_dashboard_shows_the_alert_time(self):
        now = time.time()
        history = _history_with_certs(now, {"api": now + 5 * 86400})
        alerted = now - 3600
        records = {"api": {"cert_expires_at": now + 5 * 86400,
                           "warn_days": 30, "alerted_at": alerted}}
        page = render_dashboard(history, _ENDPOINTS, now=now,
                                cert_alerts=records)
        expected = time.strftime("%H:%M", time.localtime(alerted))
        self.assertIn(f"5d (soon) alerted {expected}", page)

    def test_dashboard_shows_no_alert_time_without_a_record(self):
        now = time.time()
        history = _history_with_certs(now, {"api": now + 5 * 86400})
        page = render_dashboard(history, _ENDPOINTS, now=now)
        self.assertIn("5d (soon)", page)
        self.assertNotIn("alerted", page)


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
class LiveTlsSmokeTests(unittest.TestCase):
    """End to end: a real HTTPS check against a local TLS server
    whose certificate expires in 2 days — inside the default
    30-day window — fires exactly one cert-expiry webhook across
    two monitor rounds, and the endpoint stays UP."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.paths = _make_certs(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_monitor_loop_alerts_once_for_expiring_cert(self):
        server = _run_capture_server(status=200)
        try:
            hook_url = (f"http://127.0.0.1:{server.server_port}/hook")
            with _TLSServer(self.paths) as tls:
                cfg = {
                    "interval_seconds": 0.05,
                    "webhook_url": hook_url,
                    "endpoints": [
                        {"name": "tls", "type": "http", "url": tls.url,
                         "cafile": self.paths["ca.crt"]},
                    ],
                }
                history = History()
                cert_alerts = {}
                monitor_loop(cfg, history, None, None,
                             threading.Event(), max_rounds=2,
                             cert_alerts=cert_alerts)
            # The endpoint stayed UP — the expiring cert is a
            # warning, not downtime — and its expiry was recorded.
            entry = history.current("tls")
            self.assertTrue(entry["ok"])
            self.assertIsNotNone(entry["cert_expires_at"])
            # Exactly one webhook POST across both rounds: the
            # second round found the same certificate still inside
            # the window and did not re-fire.
            self.assertEqual(len(_CaptureHandler.received), 1)
            path, content_type, body = _CaptureHandler.received[0]
            self.assertEqual(path, "/hook")
            self.assertEqual(content_type, "application/json")
            payload = json.loads(body)
            self.assertEqual(payload["type"], "cert_expiry")
            self.assertEqual(payload["endpoint"], "tls")
            self.assertAlmostEqual(payload["cert_expires_at"],
                                   entry["cert_expires_at"], delta=1)
            self.assertIn(payload["days_remaining"], (1, 2))
            self.assertEqual(payload["warn_days"], 30)
            self.assertIsNotNone(payload["checked_at"])
            # The alert is recorded, so the dashboard and the API
            # can show when it fired.
            self.assertIn("tls", cert_alerts)
            self.assertEqual(cert_alerts["tls"]["cert_expires_at"],
                             entry["cert_expires_at"])
            self.assertIsNotNone(cert_alerts["tls"]["alerted_at"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
