"""Tests for per-endpoint alert routing (0.19.0).

An endpoint can name its own `webhook` URL: its state-change alerts
go to its own channel, and its slice of the alert digest is POSTed
there too. Endpoints without one fall back to the global `webhook_url`
(or the digest block's `webhook_url`).
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import healthcheck.alerts as alerts_mod
import healthcheck.digest as digest_mod
from healthcheck.alerts import WebhookAlerter
from healthcheck.checker import CheckResult
from healthcheck.cli import (check_all, endpoint_webhooks, load_config,
                             make_alerter)
from healthcheck.digest import DigestAlerter
from healthcheck.flap import FlapDetector
from healthcheck.store import History


def _result(name, ok, latency=12.3, status=None, error=None,
            at=1_700_000_000.0):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       status_code=status, error=error, checked_at=at)


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records POST bodies; status code comes from a mutable attribute
    so a test can flip an endpoint from up to down between rounds."""
    received = []
    status = 200

    def do_GET(self):
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _run_server():
    _CaptureHandler.received = []
    _CaptureHandler.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class ValidateEndpointWebhookTests(unittest.TestCase):
    def _load(self, cfg):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            return load_config(path)
        finally:
            os.unlink(path)

    def _cfg(self, webhook):
        return {"endpoints": [{"name": "api", "type": "http",
                               "url": "https://example.com",
                               "webhook": webhook}]}

    def test_valid_webhook_loads(self):
        cfg = self._load(self._cfg("https://hooks.example/api"))
        self.assertEqual(cfg["endpoints"][0]["webhook"],
                         "https://hooks.example/api")

    def test_missing_webhook_loads(self):
        cfg = self._load({"endpoints": [{"name": "api", "type": "http",
                                         "url": "https://example.com"}]})
        self.assertNotIn("webhook", cfg["endpoints"][0])

    def test_bad_webhook_rejected(self):
        for bad in (42, "", "ftp://hooks.example/api",
                    "hooks.example/api"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self._load(self._cfg(bad))

    def test_null_webhook_means_unset(self):
        cfg = self._load(self._cfg(None))
        self.assertIsNone(cfg["endpoints"][0]["webhook"])

    def test_bad_webhook_on_one_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            self._load({
                "endpoints": [
                    {"name": "api", "type": "http",
                     "url": "https://example.com",
                     "webhook": "https://hooks.example/api"},
                    {"name": "db", "type": "tcp", "host": "127.0.0.1",
                     "port": 5432, "webhook": "not-a-url"},
                ]})


class EndpointWebhooksTests(unittest.TestCase):
    def test_routes_built_from_config(self):
        cfg = {"endpoints": [
            {"name": "api", "webhook": "https://hooks.example/api"},
            {"name": "db"},
            {"name": "web", "webhook": "https://hooks.example/web"},
        ]}
        self.assertEqual(endpoint_webhooks(cfg), {
            "api": "https://hooks.example/api",
            "web": "https://hooks.example/web",
        })

    def test_empty_when_no_routes(self):
        self.assertEqual(endpoint_webhooks({"endpoints": [{"name": "db"}]}),
                         {})


class WebhookAlerterRoutingTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = alerts_mod.post_alert
        alerts_mod.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts_mod.post_alert = self._real_post

    def test_routed_endpoint_uses_its_own_webhook(self):
        alerter = WebhookAlerter(
            "https://hooks.example/global",
            routes={"api": "https://hooks.example/api"})
        alerter.notify(_result("api", True))
        alerter.notify(_result("api", False, error="boom"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "https://hooks.example/api")
        self.assertEqual(self.sent[0][1]["endpoint"], "api")

    def test_unrouted_endpoint_falls_back_to_global(self):
        alerter = WebhookAlerter(
            "https://hooks.example/global",
            routes={"api": "https://hooks.example/api"})
        alerter.notify(_result("db", True))
        alerter.notify(_result("db", False, error="boom"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "https://hooks.example/global")
        self.assertEqual(self.sent[0][1]["endpoint"], "db")

    def test_routed_endpoint_works_with_no_global_url(self):
        alerter = WebhookAlerter(
            None, routes={"api": "https://hooks.example/api"})
        alerter.notify(_result("api", True))
        alerter.notify(_result("api", False, error="boom"))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "https://hooks.example/api")

    def test_no_url_anywhere_sends_nothing_but_tracks_state(self):
        alerter = WebhookAlerter(None)
        alerter.notify(_result("api", True))
        self.assertIsNone(alerter.notify(_result("api", False)))
        self.assertEqual(self.sent, [])
        # State is still tracked: a recovery is a transition too,
        # it just has nowhere to go either.
        self.assertIsNone(alerter.notify(_result("api", True)))
        self.assertEqual(self.sent, [])

    def test_url_for(self):
        alerter = WebhookAlerter(
            "https://hooks.example/global",
            routes={"api": "https://hooks.example/api"})
        self.assertEqual(alerter.url_for("api"),
                         "https://hooks.example/api")
        self.assertEqual(alerter.url_for("db"),
                         "https://hooks.example/global")
        self.assertIsNone(WebhookAlerter(None).url_for("db"))


class MakeAlerterRoutingTests(unittest.TestCase):
    class Args:
        webhook_url = None

    def setUp(self):
        self.sent = []
        self._real_post = digest_mod.post_alert
        digest_mod.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        digest_mod.post_alert = self._real_post

    def test_alerter_exists_with_only_per_endpoint_webhooks(self):
        cfg = {"endpoints": [
            {"name": "api", "webhook": "https://hooks.example/api"}]}
        alerter = make_alerter(cfg, self.Args())
        self.assertIsNotNone(alerter)
        self.assertEqual(alerter.routes,
                         {"api": "https://hooks.example/api"})

    def test_no_alerter_when_no_webhook_anywhere(self):
        cfg = {"endpoints": [{"name": "api"}]}
        self.assertIsNone(make_alerter(cfg, self.Args()))

    def test_digest_alerter_gets_routes(self):
        cfg = {"endpoints": [
            {"name": "api", "webhook": "https://hooks.example/api"}],
               "alert_digest": {"interval_seconds": 900,
                                "webhook_url": "https://hooks.example/digest"}}
        alerter = make_alerter(cfg, self.Args())
        self.assertIsInstance(alerter, DigestAlerter)
        self.assertEqual(alerter.routes,
                         {"api": "https://hooks.example/api"})
        # And it actually works: a transition is recorded and the
        # digest goes out (a pre-validated config used to be
        # re-validated and rejected as having an unknown "smtp" key).
        start = alerter._window_start
        alerter.notify(_result("api", True, at=start))
        alerter.notify(_result("api", False, at=start + 100.0))
        result = alerter.maybe_send(now=start + 900, current_down=["api"])
        self.assertIsNotNone(result)
        self.assertEqual(len(result["digest"]["changes"]), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "https://hooks.example/api")


class DigestRoutingTests(unittest.TestCase):
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

    def test_digest_split_across_routed_webhooks(self):
        alerter = self._alerter(
            routes={"api": "https://hooks.example/api"})
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0, error="boom"))
        alerter.notify(_result("db", True, at=1000.0))
        alerter.notify(_result("db", False, at=1150.0, error="boom"))
        result = alerter.maybe_send(now=2000.0,
                                    current_down=["api", "db"])
        self.assertIsNotNone(result)
        # One digest per destination, each with only its endpoint.
        self.assertEqual(len(self.sent), 2)
        by_url = {url: payload for url, payload in self.sent}
        api_digest = by_url["https://hooks.example/api"]
        self.assertEqual([c["endpoint"] for c in api_digest["changes"]],
                         ["api"])
        self.assertEqual(api_digest["currently_down"], ["api"])
        global_digest = by_url["https://hooks.example/digest"]
        self.assertEqual([c["endpoint"]
                          for c in global_digest["changes"]], ["db"])
        self.assertEqual(global_digest["currently_down"], ["db"])
        # The combined digest (for the email) still covers everyone.
        self.assertEqual(len(result["digest"]["changes"]), 2)
        self.assertEqual(result["digest"]["currently_down"],
                         ["api", "db"])
        # Multiple destinations: webhook_ok is a per-URL mapping.
        self.assertEqual(result["webhook_ok"], {
            "https://hooks.example/api": True,
            "https://hooks.example/digest": True,
        })

    def test_single_destination_keeps_bool_webhook_ok(self):
        alerter = self._alerter()
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0))
        result = alerter.maybe_send(now=2000.0, current_down=["api"])
        self.assertTrue(result["webhook_ok"])
        self.assertEqual(len(self.sent), 1)

    def test_currently_down_goes_only_to_its_route(self):
        alerter = self._alerter(
            routes={"api": "https://hooks.example/api"})
        result = alerter.maybe_send(now=2000.0, current_down=["api"])
        self.assertIsNotNone(result)
        self.assertEqual(len(self.sent), 1)
        url, payload = self.sent[0]
        self.assertEqual(url, "https://hooks.example/api")
        self.assertEqual(payload["currently_down"], ["api"])
        self.assertEqual(payload["changes"], [])

    def test_routed_digest_with_email_only_config(self):
        # The digest block has no webhook of its own, only email:
        # a routed endpoint's changes are still POSTed to its
        # webhook, and the email still covers every endpoint.
        alerter = self._alerter(
            routes={"api": "https://hooks.example/api"},
            webhook_url=None, email_to=["ops@example.com"])
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0))
        alerter.notify(_result("db", True, at=1000.0))
        alerter.notify(_result("db", False, at=1150.0))
        result = alerter.maybe_send(now=2000.0,
                                    current_down=["api", "db"])
        self.assertIsNotNone(result)
        self.assertEqual(len(self.sent), 1)
        url, payload = self.sent[0]
        self.assertEqual(url, "https://hooks.example/api")
        self.assertEqual([c["endpoint"] for c in payload["changes"]],
                         ["api"])
        self.assertIn("api went DOWN", result["email"])
        self.assertIn("db went DOWN", result["email"])


class CheckAllRoutingTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = alerts_mod.post_alert
        alerts_mod.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        alerts_mod.post_alert = self._real_post

    def test_check_all_routes_alerts_per_endpoint(self):
        # A local HTTP server whose status flips between rounds:
        # 200, then 500, so both endpoints transition up -> down.
        server = _run_server()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            endpoints = [
                {"name": "api", "type": "http", "url": f"{base}/api",
                 "webhook": "https://hooks.example/api"},
                {"name": "web", "type": "http", "url": f"{base}/web"},
            ]
            cfg = {"endpoints": endpoints,
                   "webhook_url": "https://hooks.example/global"}
            alerter = make_alerter(cfg, None)
            detector = FlapDetector(threshold=1)
            history = History()
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
            self.assertEqual(self.sent, [])  # first round: baseline
            _CaptureHandler.status = 500
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
            self.assertEqual(len(self.sent), 2)
            by_url = {url: payload for url, payload in self.sent}
            self.assertEqual(by_url["https://hooks.example/api"]["endpoint"],
                             "api")
            self.assertEqual(
                by_url["https://hooks.example/global"]["endpoint"], "web")
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
