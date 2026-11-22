"""Tests for configurable alert hooks (webhook URL)."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import healthcheck.alerts as alerts
from healthcheck.alerts import WebhookAlerter, alert_payload, post_alert
from healthcheck.checker import CheckResult
from healthcheck.cli import check_all, webhook_url
from healthcheck.store import History


def _result(name, ok, latency=12.3, status=None, error=None,
            at=1_700_000_000.0):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       status_code=status, error=error, checked_at=at)


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


def _run_server(status=200):
    _CaptureHandler.received = []
    _CaptureHandler.status = status
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class PayloadTests(unittest.TestCase):
    def test_down_payload(self):
        payload = alert_payload(_result("api", False, status=500,
                                        error="HTTP 500"), previous_ok=True)
        self.assertEqual(payload, {
            "endpoint": "api", "event": "down", "ok": False,
            "previous_ok": True, "latency_ms": 12.3, "status_code": 500,
            "error": "HTTP 500", "checked_at": 1_700_000_000.0,
        })

    def test_recovered_payload(self):
        payload = alert_payload(_result("api", True), previous_ok=False)
        self.assertEqual(payload["event"], "recovered")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["previous_ok"])


class PostAlertTests(unittest.TestCase):
    def test_posts_json_and_returns_true_on_2xx(self):
        server = _run_server(status=200)
        try:
            url = f"http://127.0.0.1:{server.server_port}/hook"
            payload = {"endpoint": "api", "event": "down"}
            self.assertTrue(post_alert(url, payload))
            self.assertEqual(len(_CaptureHandler.received), 1)
            path, content_type, body = _CaptureHandler.received[0]
            self.assertEqual(path, "/hook")
            self.assertEqual(content_type, "application/json")
            self.assertEqual(json.loads(body), payload)
        finally:
            server.shutdown()
            server.server_close()

    def test_non_2xx_returns_false(self):
        server = _run_server(status=500)
        try:
            url = f"http://127.0.0.1:{server.server_port}/hook"
            self.assertFalse(post_alert(url, {"event": "down"}))
            self.assertEqual(len(_CaptureHandler.received), 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_unreachable_url_returns_false_without_raising(self):
        # Port 1 on localhost refuses connections deterministically.
        self.assertFalse(post_alert("http://127.0.0.1:1/hook",
                                    {"event": "down"}, timeout=1))


class AlerterTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = alerts.post_alert
        alerts.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts.post_alert = self._real_post

    def test_first_check_sends_nothing(self):
        alerter = WebhookAlerter("http://example.invalid/hook")
        self.assertIsNone(alerter.notify(_result("api", False)))
        self.assertEqual(self.sent, [])

    def test_same_state_sends_nothing(self):
        alerter = WebhookAlerter("http://example.invalid/hook")
        alerter.notify(_result("api", True))
        self.assertIsNone(alerter.notify(_result("api", True)))
        self.assertEqual(self.sent, [])

    def test_down_transition_sends_down_payload(self):
        alerter = WebhookAlerter("http://example.invalid/hook")
        alerter.notify(_result("api", True))
        payload = alerter.notify(_result("api", False, error="boom"))
        self.assertEqual(payload["event"], "down")
        self.assertEqual(payload["previous_ok"], True)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "http://example.invalid/hook")
        self.assertEqual(self.sent[0][1], payload)

    def test_recovery_transition_sends_recovered_payload(self):
        alerter = WebhookAlerter("http://example.invalid/hook")
        alerter.notify(_result("api", False))
        payload = alerter.notify(_result("api", True))
        self.assertEqual(payload["event"], "recovered")
        self.assertEqual(len(self.sent), 1)

    def test_failed_delivery_still_reports_transition(self):
        alerts.post_alert = lambda url, payload, timeout=5.0: False
        alerter = WebhookAlerter("http://example.invalid/hook")
        alerter.notify(_result("api", True))
        payload = alerter.notify(_result("api", False))
        self.assertEqual(payload["event"], "down")

    def test_endpoints_tracked_independently(self):
        alerter = WebhookAlerter("http://example.invalid/hook")
        alerter.notify(_result("api", True))
        alerter.notify(_result("db", False))
        alerter.notify(_result("api", False))
        alerter.notify(_result("db", False))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["endpoint"], "api")


class WebhookUrlTests(unittest.TestCase):
    def test_flag_beats_config(self):
        cfg = {"webhook_url": "https://config.example/hook"}

        class Args:
            webhook_url = "https://flag.example/hook"

        self.assertEqual(webhook_url(cfg, Args()),
                         "https://flag.example/hook")

    def test_config_used_when_no_flag(self):
        cfg = {"webhook_url": "https://config.example/hook"}

        class Args:
            webhook_url = None

        self.assertEqual(webhook_url(cfg, Args()),
                         "https://config.example/hook")

    def test_none_when_neither_set(self):
        class Args:
            webhook_url = None

        self.assertIsNone(webhook_url({}, Args()))


class CheckAllAlerterTests(unittest.TestCase):
    def test_check_all_calls_alerter_with_each_result(self):
        # A TCP check against a closed localhost port: deterministic
        # DOWN result, no network access needed.
        seen = []

        class Spy:
            def notify(self, result):
                seen.append(result)
                return None

        endpoints = [{"name": "closed", "type": "tcp",
                      "host": "127.0.0.1", "port": 1, "timeout": 1}]
        check_all(endpoints, History(), alerter=Spy())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].name, "closed")
        self.assertFalse(seen[0].ok)

    def test_check_all_without_alerter_still_works(self):
        endpoints = [{"name": "closed", "type": "tcp",
                      "host": "127.0.0.1", "port": 1, "timeout": 1}]
        check_all(endpoints, History())  # must not raise


if __name__ == "__main__":
    unittest.main()
