"""Tests for the alert digest (webhook + email body)."""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import healthcheck.digest as digest_mod
from healthcheck.checker import CheckResult
from healthcheck.cli import check_all, load_config
from healthcheck.digest import (DigestAlerter, build_digest,
                                render_digest_email,
                                validate_digest_config)
from healthcheck.flap import FlapDetector
from healthcheck.store import History


def _result(name, ok, latency=12.3, status=None, error=None,
            at=1_700_000_000.0):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       status_code=status, error=error, checked_at=at)


def _change(name, ok, at, status=None, error=None):
    return {
        "endpoint": name, "event": "recovered" if ok else "down",
        "ok": ok, "previous_ok": not ok, "latency_ms": 12.3,
        "status_code": status, "error": error, "checked_at": at,
    }


class _CaptureHandler(BaseHTTPRequestHandler):
    """Records POST bodies."""
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        type(self).received.append(body)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def _run_server():
    _CaptureHandler.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class ValidateDigestConfigTests(unittest.TestCase):
    def test_webhook_only_is_valid(self):
        cfg = validate_digest_config(
            {"webhook_url": "https://hooks.example/digest"})
        self.assertEqual(cfg["interval_seconds"], 900)
        self.assertEqual(cfg["webhook_url"],
                         "https://hooks.example/digest")
        self.assertIsNone(cfg["email_to"])
        self.assertEqual(cfg["email_from"], "healthcheck@localhost")

    def test_full_config_normalizes(self):
        cfg = validate_digest_config({
            "interval_seconds": 300,
            "webhook_url": "http://127.0.0.1:9000/hook",
            "email_to": "ops@example.com",
            "email_from": "monitor@example.com",
        })
        self.assertEqual(cfg["interval_seconds"], 300)
        self.assertEqual(cfg["email_to"], ["ops@example.com"])
        self.assertEqual(cfg["email_from"], "monitor@example.com")

    def test_email_list_kept(self):
        cfg = validate_digest_config(
            {"email_to": ["a@example.com", "b@example.com"]})
        self.assertEqual(cfg["email_to"],
                         ["a@example.com", "b@example.com"])

    def test_not_a_dict_rejected(self):
        for bad in ("https://example.com/hook", ["webhook_url"], None, 42):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_digest_config(bad)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            validate_digest_config(
                {"webhook_url": "https://example.com/hook",
                 "smtp_host": "mail.example.com"})

    def test_bad_interval_rejected(self):
        for bad in (0, -60, "900", 1.5, True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_digest_config(
                    {"interval_seconds": bad,
                     "webhook_url": "https://example.com/hook"})

    def test_bad_webhook_url_rejected(self):
        for bad in (42, "", "ftp://example.com/hook", "example.com/hook"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_digest_config({"webhook_url": bad})

    def test_bad_email_to_rejected(self):
        for bad in ("", [], [42], ["a@example.com", ""], 42):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_digest_config({"email_to": bad})

    def test_bad_email_from_rejected(self):
        for bad in ("", 42, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                validate_digest_config(
                    {"email_to": "ops@example.com", "email_from": bad})

    def test_no_destination_rejected(self):
        with self.assertRaises(ValueError):
            validate_digest_config({"interval_seconds": 300})
        with self.assertRaises(ValueError):
            validate_digest_config({})


class BuildDigestTests(unittest.TestCase):
    def test_includes_changes_in_window_only(self):
        changes = [
            _change("api", False, at=1000.0),   # window start: in
            _change("api", True, at=1500.0),    # in
            _change("db", False, at=999.0),     # before window: out
            _change("db", False, at=1900.0),    # at window end: out
        ]
        digest = build_digest(changes, ["db"], 1000.0, 1900.0)
        self.assertEqual(digest["type"], "digest")
        self.assertEqual(digest["window_start"], 1000.0)
        self.assertEqual(digest["window_end"], 1900.0)
        self.assertEqual([c["checked_at"] for c in digest["changes"]],
                         [1000.0, 1500.0])
        self.assertEqual(digest["currently_down"], ["db"])
        self.assertEqual(digest["summary"], {
            "changes": 2, "down": 1, "recovered": 1,
            "currently_down": 1, "flapping": 0,
        })

    def test_currently_down_sorted(self):
        digest = build_digest([], ["zeta", "alpha"], 0.0, 60.0)
        self.assertEqual(digest["currently_down"], ["alpha", "zeta"])

    def test_flapping_needs_three_changes(self):
        changes = [
            _change("api", False, at=10.0),
            _change("api", True, at=20.0),
            _change("api", False, at=30.0),
            _change("db", False, at=40.0),
            _change("db", True, at=50.0),
        ]
        digest = build_digest(changes, [], 0.0, 60.0)
        self.assertEqual(digest["flapping"], ["api"])
        self.assertEqual(digest["summary"]["flapping"], 1)

    def test_empty_window_digest(self):
        digest = build_digest([], [], 0.0, 60.0)
        self.assertEqual(digest["changes"], [])
        self.assertEqual(digest["summary"]["changes"], 0)


class RenderEmailTests(unittest.TestCase):
    def test_email_covers_changes_down_and_flapping(self):
        changes = [
            _change("api", False, at=1_700_000_000.0, status=500,
                    error="HTTP 500"),
            _change("api", True, at=1_700_000_100.0),
            _change("api", False, at=1_700_000_200.0),
            _change("db", False, at=1_700_000_300.0),
        ]
        digest = build_digest(changes, ["api", "db"],
                              1_700_000_000.0, 1_700_000_900.0)
        text = render_digest_email(digest, "monitor@example.com",
                                   ["ops@example.com"])
        self.assertIn("From: monitor@example.com", text)
        self.assertIn("To: ops@example.com", text)
        self.assertIn("Subject: healthcheck digest: 4 change(s), "
                      "2 currently down, 1 flapping", text)
        self.assertIn("api went DOWN", text)
        self.assertIn("api RECOVERED", text)
        self.assertIn("HTTP 500", text)
        self.assertIn("Currently down (2):", text)
        self.assertIn("Flapping (1):", text)
        self.assertIn("api (3 changes in this window)", text)

    def test_email_with_nothing_down(self):
        digest = build_digest(
            [_change("api", True, at=10.0)], [], 0.0, 60.0)
        text = render_digest_email(digest, "monitor@example.com",
                                   ["a@example.com", "b@example.com"])
        self.assertIn("To: a@example.com, b@example.com", text)
        self.assertIn("Currently down (0):", text)
        self.assertIn("(none)", text)


class DigestAlerterTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self._real_post = digest_mod.post_alert
        digest_mod.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        digest_mod.post_alert = self._real_post

    def _alerter(self, **overrides):
        cfg = {"interval_seconds": 900,
               "webhook_url": "https://hooks.example/digest"}
        cfg.update(overrides)
        return DigestAlerter(cfg, now_fn=lambda: 1000.0)

    def test_not_due_sends_nothing(self):
        alerter = self._alerter()
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0))
        self.assertIsNone(alerter.maybe_send(now=1500.0))
        self.assertEqual(self.sent, [])

    def test_empty_window_sends_nothing(self):
        alerter = self._alerter()
        self.assertIsNone(alerter.maybe_send(now=2000.0))
        self.assertEqual(self.sent, [])

    def test_due_window_posts_digest(self):
        alerter = self._alerter()
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0, status=500,
                               error="HTTP 500"))
        result = alerter.maybe_send(now=2000.0, current_down=["api"])
        self.assertIsNotNone(result)
        self.assertTrue(result["webhook_ok"])
        self.assertIsNone(result["email"])
        self.assertEqual(len(self.sent), 1)
        url, payload = self.sent[0]
        self.assertEqual(url, "https://hooks.example/digest")
        self.assertEqual(payload["type"], "digest")
        self.assertEqual(payload["window_start"], 1000.0)
        self.assertEqual(payload["window_end"], 2000.0)
        self.assertEqual(len(payload["changes"]), 1)
        self.assertEqual(payload["changes"][0]["event"], "down")
        self.assertEqual(payload["currently_down"], ["api"])
        self.assertEqual(payload["summary"]["down"], 1)

    def test_empty_window_after_send_sends_nothing(self):
        # A due send consumes the window: the next due send with no
        # new changes and nothing down is empty and sends nothing.
        alerter = self._alerter()
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0))
        self.assertIsNotNone(alerter.maybe_send(now=2000.0,
                                                current_down=["api"]))
        self.assertIsNone(alerter.maybe_send(now=3000.0,
                                             current_down=[]))
        self.assertEqual(len(self.sent), 1)

    def test_currently_down_alone_triggers_send(self):
        # No changes in the window, but something is still down:
        # the digest still goes out so the outage is not silent.
        alerter = self._alerter()
        result = alerter.maybe_send(now=2000.0, current_down=["db"])
        self.assertIsNotNone(result)
        self.assertEqual(result["digest"]["changes"], [])
        self.assertEqual(result["digest"]["currently_down"], ["db"])

    def test_email_rendered_when_configured(self):
        alerter = self._alerter(webhook_url=None,
                                email_to=["ops@example.com"])
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0))
        result = alerter.maybe_send(now=2000.0, current_down=["api"])
        self.assertIsNotNone(result)
        self.assertIsNone(result["webhook_ok"])
        self.assertIn("api went DOWN", result["email"])
        self.assertEqual(self.sent, [])

    def test_failed_webhook_still_advances_window(self):
        digest_mod.post_alert = lambda url, payload, timeout=5.0: False
        alerter = self._alerter()
        alerter.notify(_result("api", True, at=1000.0))
        alerter.notify(_result("api", False, at=1100.0))
        result = alerter.maybe_send(now=2000.0, current_down=["api"])
        self.assertIsNotNone(result)
        self.assertFalse(result["webhook_ok"])
        # The window advanced: the failed change is not re-sent.
        self.assertIsNone(alerter.maybe_send(now=3000.0,
                                             current_down=[]))

    def test_notify_tracks_transitions_without_inner_alerter(self):
        alerter = self._alerter()
        self.assertIsNone(alerter.notify(_result("api", False,
                                                 at=1000.0)))
        self.assertIsNone(alerter.notify(_result("api", False,
                                                 at=1100.0)))
        payload = alerter.notify(_result("api", True, at=1200.0))
        self.assertEqual(payload["event"], "recovered")
        result = alerter.maybe_send(now=2000.0, current_down=[])
        self.assertEqual(len(result["digest"]["changes"]), 1)

    def test_notify_delegates_to_inner_alerter(self):
        sent = []

        class Inner:
            def notify(self, result):
                sent.append(result)
                return {"endpoint": result.name, "ok": result.ok,
                        "checked_at": result.checked_at}

        alerter = DigestAlerter(
            {"interval_seconds": 900,
             "webhook_url": "https://hooks.example/digest"},
            inner=Inner(), now_fn=lambda: 1000.0)
        alerter.notify(_result("api", True, at=1000.0))
        self.assertEqual(len(sent), 1)
        result = alerter.maybe_send(now=2000.0, current_down=[])
        self.assertEqual(len(result["digest"]["changes"]), 1)


class DigestWebhookIntegrationTests(unittest.TestCase):
    def test_digest_posts_json_to_real_server(self):
        server = _run_server()
        try:
            url = f"http://127.0.0.1:{server.server_port}/digest"
            alerter = DigestAlerter(
                {"interval_seconds": 60, "webhook_url": url},
                now_fn=lambda: 1000.0)
            alerter.notify(_result("api", True, at=1000.0))
            alerter.notify(_result("api", False, at=1010.0))
            result = alerter.maybe_send(now=1100.0, current_down=["api"])
            self.assertTrue(result["webhook_ok"])
            self.assertEqual(len(_CaptureHandler.received), 1)
            payload = json.loads(_CaptureHandler.received[0])
            self.assertEqual(payload["type"], "digest")
            self.assertEqual(payload["currently_down"], ["api"])
            self.assertEqual(len(payload["changes"]), 1)
        finally:
            server.shutdown()
            server.server_close()


class DigestConfigLoadTests(unittest.TestCase):
    def _load(self, cfg):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            return load_config(path)
        finally:
            os.unlink(path)

    def test_valid_digest_loads(self):
        cfg = self._load({
            "endpoints": [],
            "alert_digest": {"interval_seconds": 300,
                             "webhook_url": "https://example.com/hook"},
        })
        self.assertEqual(cfg["alert_digest"]["interval_seconds"], 300)

    def test_bad_digest_rejected_at_load(self):
        with self.assertRaises(ValueError):
            self._load({
                "endpoints": [],
                "alert_digest": {"interval_seconds": 0,
                                 "webhook_url": "https://example.com/hook"},
            })
        with self.assertRaises(ValueError):
            self._load({
                "endpoints": [],
                "alert_digest": {"email_to": []},
            })


class CheckAllDigestTests(unittest.TestCase):
    def test_check_all_feeds_digest_alerter(self):
        # A TCP check against a closed localhost port: deterministic
        # DOWN result. Threshold 1, so the single failure is a
        # confirmed outage and the digest records it.
        alerter = DigestAlerter(
            {"interval_seconds": 60,
             "webhook_url": "https://hooks.example/digest"},
            now_fn=lambda: 1000.0)
        endpoints = [{"name": "closed", "type": "tcp",
                      "host": "127.0.0.1", "port": 1, "timeout": 1}]
        check_all(endpoints, History(), alerter=alerter,
                  flap_detector=FlapDetector(threshold=1))
        # First check sets the baseline, so nothing is recorded yet,
        # and nothing is down in the digest's view: empty window.
        self.assertIsNone(alerter.maybe_send(now=1100.0,
                                             current_down=[]))
        check_all(endpoints, History(), alerter=alerter,
                  flap_detector=FlapDetector(threshold=1))
        # Still the same state (down from the start): no transition,
        # but the endpoint is currently down, so the digest goes out.
        result = alerter.maybe_send(now=1200.0, current_down=["closed"])
        self.assertIsNotNone(result)
        self.assertEqual(result["digest"]["currently_down"], ["closed"])


if __name__ == "__main__":
    unittest.main()
