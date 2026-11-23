"""Tests for SMTP delivery of the alert digest email.

The SMTP transport is exercised through a fake in-process SMTP
server (an object with the smtplib.SMTP interface, injected via
DigestAlerter's smtp_factory), so no real mail server or network
is needed: the tests verify the message content, the headers, the
STARTTLS / implicit-TLS choice, login, and that every failure mode
returns False instead of raising.
"""

import smtplib
import unittest

import healthcheck.digest as digest_mod
from healthcheck.checker import CheckResult
from healthcheck.digest import (DigestAlerter, build_digest,
                                resolve_smtp_config,
                                send_digest_email,
                                validate_digest_config)


def _change(name, ok, at, status=None, error=None):
    return {
        "endpoint": name, "event": "recovered" if ok else "down",
        "ok": ok, "previous_ok": not ok, "latency_ms": 12.3,
        "status_code": status, "error": error, "checked_at": at,
    }


class _FakeSMTP:
    """Records what the sender did; never touches the network."""

    instances = []

    def __init__(self, host, port, timeout=None, implicit_tls=False):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.implicit_tls = implicit_tls
        self.ehlo_calls = 0
        self.starttls_called = False
        self.starttls_context = None
        self.login_args = None
        self.sent = None
        self.quit_called = False
        type(self).instances.append(self)

    def ehlo(self):
        self.ehlo_calls += 1

    def starttls(self, context=None):
        self.starttls_called = True
        self.starttls_context = context

    def login(self, user, password):
        self.login_args = (user, password)

    def sendmail(self, sender, recipients, message):
        self.sent = (sender, list(recipients), message)
        return {}

    def quit(self):
        self.quit_called = True


class _FailingSMTP(_FakeSMTP):
    """Fails at the requested step."""

    fail_at = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if type(self).fail_at == "connect":
            raise smtplib.SMTPConnectError(421, "nope")

    def starttls(self, context=None):
        if type(self).fail_at == "starttls":
            raise smtplib.SMTPException("TLS failed")
        super().starttls(context=context)

    def login(self, user, password):
        if type(self).fail_at == "login":
            raise smtplib.SMTPAuthenticationError(535, "bad login")
        super().login(user, password)

    def sendmail(self, sender, recipients, message):
        if type(self).fail_at == "sendmail":
            raise smtplib.SMTPRecipientsRefused(
                {r: (550, "no such user") for r in recipients})
        return super().sendmail(sender, recipients, message)


def _factory(cls):
    def make(host, port, timeout, implicit_tls):
        return cls(host, port, timeout=timeout,
                   implicit_tls=implicit_tls)
    return make


class ResolveSmtpConfigTests(unittest.TestCase):
    def test_no_host_is_none(self):
        self.assertIsNone(resolve_smtp_config({}, environ={}))
        self.assertIsNone(resolve_smtp_config(
            {"email_to": "ops@example.com"}, environ={}))

    def test_config_defaults(self):
        smtp = resolve_smtp_config(
            {"smtp_host": "mail.example.com"}, environ={})
        self.assertEqual(smtp, {
            "host": "mail.example.com", "port": 587,
            "user": None, "password": None, "starttls": True,
        })

    def test_config_values_kept(self):
        smtp = resolve_smtp_config({
            "smtp_host": "mail.example.com", "smtp_port": 2525,
            "smtp_user": "monitor", "smtp_password": "s3cret",
            "smtp_starttls": False,
        }, environ={})
        self.assertEqual(smtp["port"], 2525)
        self.assertEqual(smtp["user"], "monitor")
        self.assertEqual(smtp["password"], "s3cret")
        self.assertFalse(smtp["starttls"])

    def test_env_overrides_config(self):
        smtp = resolve_smtp_config({
            "smtp_host": "mail.example.com", "smtp_port": 587,
            "smtp_user": "config-user",
            "smtp_password": "config-pass",
            "smtp_starttls": False,
        }, environ={
            "HEALTHCHECK_SMTP_HOST": "smtp.env.example",
            "HEALTHCHECK_SMTP_PORT": "465",
            "HEALTHCHECK_SMTP_USER": "env-user",
            "HEALTHCHECK_SMTP_PASSWORD": "env-pass",
            "HEALTHCHECK_SMTP_STARTTLS": "true",
        })
        self.assertEqual(smtp["host"], "smtp.env.example")
        self.assertEqual(smtp["port"], 465)
        self.assertEqual(smtp["user"], "env-user")
        self.assertEqual(smtp["password"], "env-pass")
        self.assertTrue(smtp["starttls"])

    def test_env_host_alone_enables_smtp(self):
        smtp = resolve_smtp_config(
            {}, environ={"HEALTHCHECK_SMTP_HOST": "mail.example.com"})
        self.assertEqual(smtp["host"], "mail.example.com")
        self.assertEqual(smtp["port"], 587)

    def test_bad_port_rejected(self):
        for bad in (0, 65536, -1, "587", 25.5, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                resolve_smtp_config({"smtp_host": "mail.example.com",
                                     "smtp_port": bad}, environ={})
        with self.assertRaises(ValueError):
            resolve_smtp_config(
                {"smtp_host": "mail.example.com"},
                environ={"HEALTHCHECK_SMTP_PORT": "not-a-port"})

    def test_bad_host_rejected(self):
        for bad in ("", 42):
            with self.assertRaises(ValueError, msg=repr(bad)):
                resolve_smtp_config({"smtp_host": bad}, environ={})

    def test_bad_starttls_rejected(self):
        with self.assertRaises(ValueError):
            resolve_smtp_config(
                {"smtp_host": "mail.example.com"},
                environ={"HEALTHCHECK_SMTP_STARTTLS": "yes-please"})
        for bad in (1, "yes"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                resolve_smtp_config(
                    {"smtp_host": "mail.example.com",
                     "smtp_starttls": bad}, environ={})

    def test_starttls_env_values(self):
        for raw, expected in (("true", True), ("1", True),
                              ("yes", True), ("on", True),
                              ("false", False), ("0", False),
                              ("no", False), ("off", False)):
            smtp = resolve_smtp_config(
                {"smtp_host": "mail.example.com"},
                environ={"HEALTHCHECK_SMTP_STARTTLS": raw})
            self.assertEqual(smtp["starttls"], expected, raw)

    def test_empty_user_and_password_become_none(self):
        smtp = resolve_smtp_config(
            {"smtp_host": "mail.example.com", "smtp_user": "",
             "smtp_password": ""}, environ={})
        self.assertIsNone(smtp["user"])
        self.assertIsNone(smtp["password"])


class ValidateSmtpConfigTests(unittest.TestCase):
    def test_smtp_keys_accepted(self):
        cfg = validate_digest_config({
            "email_to": "ops@example.com",
            "smtp_host": "mail.example.com",
            "smtp_port": 2525,
            "smtp_user": "monitor",
            "smtp_password": "s3cret",
        })
        self.assertEqual(cfg["smtp"]["host"], "mail.example.com")
        self.assertEqual(cfg["smtp"]["port"], 2525)

    def test_no_smtp_host_is_none(self):
        cfg = validate_digest_config(
            {"email_to": "ops@example.com"})
        self.assertIsNone(cfg["smtp"])

    def test_bad_smtp_port_rejected_at_load(self):
        with self.assertRaises(ValueError):
            validate_digest_config(
                {"email_to": "ops@example.com",
                 "smtp_host": "mail.example.com", "smtp_port": 0})


class SendDigestEmailTests(unittest.TestCase):
    def setUp(self):
        _FakeSMTP.instances = []
        _FailingSMTP.instances = []
        _FailingSMTP.fail_at = None
        self.message = (
            "From: monitor@example.com\r\n"
            "To: ops@example.com\r\n"
            "Subject: healthcheck digest: 1 change(s), 1 currently down\r\n"
            "\r\n"
            "Healthcheck alert digest\n")

    def _smtp(self, **overrides):
        smtp = {"host": "mail.example.com", "port": 587,
                "user": None, "password": None, "starttls": True}
        smtp.update(overrides)
        return smtp

    def test_starttls_and_send(self):
        ok = send_digest_email(
            self.message, "monitor@example.com",
            ["ops@example.com"], self._smtp(),
            smtp_factory=_factory(_FakeSMTP))
        self.assertTrue(ok)
        self.assertEqual(len(_FakeSMTP.instances), 1)
        server = _FakeSMTP.instances[0]
        self.assertEqual((server.host, server.port),
                         ("mail.example.com", 587))
        self.assertFalse(server.implicit_tls)
        self.assertTrue(server.starttls_called)
        self.assertIsNotNone(server.starttls_context)
        self.assertIsNone(server.login_args)
        self.assertEqual(server.sent[0], "monitor@example.com")
        self.assertEqual(server.sent[1], ["ops@example.com"])
        self.assertEqual(server.sent[2], self.message)
        self.assertTrue(server.quit_called)

    def test_login_when_user_set(self):
        ok = send_digest_email(
            self.message, "monitor@example.com",
            ["ops@example.com"],
            self._smtp(user="monitor", password="s3cret"),
            smtp_factory=_factory(_FakeSMTP))
        self.assertTrue(ok)
        server = _FakeSMTP.instances[0]
        self.assertEqual(server.login_args, ("monitor", "s3cret"))

    def test_no_starttls_when_disabled(self):
        ok = send_digest_email(
            self.message, "monitor@example.com",
            ["ops@example.com"], self._smtp(starttls=False),
            smtp_factory=_factory(_FakeSMTP))
        self.assertTrue(ok)
        server = _FakeSMTP.instances[0]
        self.assertFalse(server.starttls_called)
        self.assertTrue(server.quit_called)

    def test_port_465_uses_implicit_tls(self):
        ok = send_digest_email(
            self.message, "monitor@example.com",
            ["ops@example.com"], self._smtp(port=465),
            smtp_factory=_factory(_FakeSMTP))
        self.assertTrue(ok)
        server = _FakeSMTP.instances[0]
        self.assertTrue(server.implicit_tls)
        # No STARTTLS upgrade on an already-TLS connection.
        self.assertFalse(server.starttls_called)

    def test_message_content_and_headers_reach_server(self):
        digest = build_digest(
            [_change("api", False, at=1_700_000_000.0, status=500,
                     error="HTTP 500")],
            ["api"], 1_700_000_000.0, 1_700_000_900.0)
        from healthcheck.digest import render_digest_email
        rendered = render_digest_email(
            digest, "monitor@example.com", ["ops@example.com"])
        ok = send_digest_email(
            rendered, "monitor@example.com", ["ops@example.com"],
            self._smtp(), smtp_factory=_factory(_FakeSMTP))
        self.assertTrue(ok)
        sent = _FakeSMTP.instances[0].sent[2]
        self.assertIn("From: monitor@example.com", sent)
        self.assertIn("To: ops@example.com", sent)
        self.assertIn("Subject: healthcheck digest: 1 change(s), "
                      "1 currently down", sent)
        self.assertIn("api went DOWN", sent)
        self.assertIn("HTTP 500", sent)

    def test_every_failure_returns_false_without_raising(self):
        for step in ("connect", "starttls", "login", "sendmail"):
            _FailingSMTP.fail_at = step
            _FailingSMTP.instances = []
            ok = send_digest_email(
                self.message, "monitor@example.com",
                ["ops@example.com"],
                self._smtp(user="monitor", password="s3cret"),
                smtp_factory=_factory(_FailingSMTP))
            self.assertFalse(ok, step)

    def test_factory_raising_returns_false(self):
        def boom(host, port, timeout, implicit_tls):
            raise OSError("connection refused")
        ok = send_digest_email(
            self.message, "monitor@example.com",
            ["ops@example.com"], self._smtp(), smtp_factory=boom)
        self.assertFalse(ok)


class DigestAlerterSmtpTests(unittest.TestCase):
    def setUp(self):
        _FakeSMTP.instances = []
        self.sent = []
        self._real_post = digest_mod.post_alert
        digest_mod.post_alert = lambda url, payload, timeout=5.0: (
            self.sent.append((url, payload)) or True)

    def tearDown(self):
        digest_mod.post_alert = self._real_post

    def _alerter(self, smtp_factory=None, **overrides):
        cfg = {"interval_seconds": 60,
               "email_to": ["ops@example.com"],
               "email_from": "monitor@example.com",
               "smtp_host": "mail.example.com",
               "smtp_user": "monitor",
               "smtp_password": "s3cret"}
        cfg.update(overrides)
        return DigestAlerter(cfg, now_fn=lambda: 1000.0,
                             smtp_factory=smtp_factory)

    def _result(self, name, ok, at):
        return CheckResult(name=name, ok=ok, latency_ms=12.3,
                           status_code=None, error=None, checked_at=at)

    def test_digest_email_sent_via_smtp(self):
        alerter = self._alerter(smtp_factory=_factory(_FakeSMTP))
        alerter.notify(self._result("api", True, 1000.0))
        alerter.notify(self._result("api", False, 1100.0))
        result = alerter.maybe_send(now=1200.0, current_down=["api"])
        self.assertIsNotNone(result)
        self.assertTrue(result["email_ok"])
        self.assertIn("api went DOWN", result["email"])
        self.assertEqual(len(_FakeSMTP.instances), 1)
        server = _FakeSMTP.instances[0]
        self.assertEqual(server.login_args, ("monitor", "s3cret"))
        self.assertIn("From: monitor@example.com", server.sent[2])
        self.assertIn("To: ops@example.com", server.sent[2])

    def test_no_smtp_host_logs_only(self):
        alerter = self._alerter(
            smtp_factory=_factory(_FakeSMTP), smtp_host=None)
        # smtp_host=None means no SMTP: validate keeps smtp None.
        alerter.notify(self._result("api", True, 1000.0))
        alerter.notify(self._result("api", False, 1100.0))
        result = alerter.maybe_send(now=1200.0, current_down=["api"])
        self.assertIsNotNone(result)
        self.assertIsNone(result["email_ok"])
        self.assertIn("api went DOWN", result["email"])
        self.assertEqual(_FakeSMTP.instances, [])

    def test_failed_smtp_delivery_never_crashes_monitor(self):
        _FailingSMTP.fail_at = "sendmail"
        alerter = self._alerter(smtp_factory=_factory(_FailingSMTP))
        alerter.notify(self._result("api", True, 1000.0))
        alerter.notify(self._result("api", False, 1100.0))
        # maybe_send must return a result (not raise) and report
        # the failure, and the window must still advance.
        result = alerter.maybe_send(now=1200.0, current_down=["api"])
        self.assertIsNotNone(result)
        self.assertFalse(result["email_ok"])
        self.assertIsNone(alerter.maybe_send(now=1400.0,
                                             current_down=[]))
        _FailingSMTP.fail_at = None

    def test_smtp_failure_does_not_block_webhook(self):
        _FailingSMTP.fail_at = "connect"
        alerter = self._alerter(
            smtp_factory=_factory(_FailingSMTP),
            webhook_url="https://hooks.example/digest")
        alerter.notify(self._result("api", True, 1000.0))
        alerter.notify(self._result("api", False, 1100.0))
        result = alerter.maybe_send(now=1200.0, current_down=["api"])
        self.assertIsNotNone(result)
        self.assertFalse(result["email_ok"])
        self.assertTrue(result["webhook_ok"])
        self.assertEqual(len(self.sent), 1)
        _FailingSMTP.fail_at = None


if __name__ == "__main__":
    unittest.main()
