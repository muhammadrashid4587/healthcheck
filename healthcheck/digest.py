"""Alert digests: batch state changes into a periodic summary.

Instead of (or in addition to) one webhook per state change, a digest
collects every confirmed change over a window (default 15 minutes)
and sends one summary: the changes in the window, the endpoints
currently down, and any endpoints that flapped (changed confirmed
state three or more times in the window). The digest is POSTed as
JSON to a webhook URL and/or rendered as a plain-text MIME email
message and delivered over SMTP (STARTTLS by default, implicit TLS
on port 465); with no SMTP host configured the rendered message is
returned to the caller (the monitor logs it), so it can be piped to
sendmail.

Pure Python 3 standard library. A failed webhook or SMTP delivery
is a warning, never a crash — the monitor must keep running even if
the webhook or the mail server is down.
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
import time
from email.message import EmailMessage

from .alerts import alert_payload, post_alert

log = logging.getLogger("healthcheck.digest")

DEFAULT_DIGEST_INTERVAL_SECONDS = 900  # 15 minutes
DEFAULT_DIGEST_SENDER = "healthcheck@localhost"
DEFAULT_SMTP_PORT = 587
# Port 465 is the implicit-TLS submission port: the connection is
# TLS from the first byte, so no STARTTLS upgrade is attempted.
IMPLICIT_TLS_SMTP_PORT = 465
DEFAULT_SMTP_TIMEOUT_SECONDS = 10.0
# An endpoint that changes confirmed state this many times in one
# digest window is flapping, not just having a bad day.
FLAPPING_CHANGE_THRESHOLD = 3

_DIGEST_KEYS = {"interval_seconds", "webhook_url", "email_to",
                "email_from", "smtp_host", "smtp_port", "smtp_user",
                "smtp_password", "smtp_starttls"}

_SMTP_ENV_VARS = {
    "host": "HEALTHCHECK_SMTP_HOST",
    "port": "HEALTHCHECK_SMTP_PORT",
    "user": "HEALTHCHECK_SMTP_USER",
    "password": "HEALTHCHECK_SMTP_PASSWORD",
    "starttls": "HEALTHCHECK_SMTP_STARTTLS",
}


def _parse_bool_env(value, name):
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        f"{name} must be true/false, 1/0, yes/no, or on/off, "
        f"not {value!r}")


def resolve_smtp_config(digest_cfg, environ=None):
    """Normalized SMTP settings for a digest config, or None.

    Config keys `smtp_host`, `smtp_port`, `smtp_user`,
    `smtp_password`, and `smtp_starttls` set the defaults; the
    HEALTHCHECK_SMTP_HOST / _PORT / _USER / _PASSWORD / _STARTTLS
    environment variables override them when set, so credentials
    never have to live in the config file. Returns None when no
    SMTP host is configured anywhere — the digest email is then
    only rendered and logged, never sent. Raises ValueError on a
    bad value (bad port, bad STARTTLS flag) so a broken mail setup
    is rejected at config load instead of failing silently every
    digest window.
    """
    env = os.environ if environ is None else environ
    host = env.get(_SMTP_ENV_VARS["host"]) or digest_cfg.get("smtp_host")
    if host is not None and (not isinstance(host, str) or not host):
        raise ValueError(
            f"alert_digest smtp_host must be a non-empty string, "
            f"not {host!r}")
    if host is None:
        return None
    port = digest_cfg.get("smtp_port", DEFAULT_SMTP_PORT)
    port_env = env.get(_SMTP_ENV_VARS["port"])
    if port_env:
        try:
            port = int(port_env)
        except ValueError:
            raise ValueError(
                f"{_SMTP_ENV_VARS['port']} must be an integer, "
                f"not {port_env!r}")
    if (isinstance(port, bool) or not isinstance(port, int)
            or not 1 <= port <= 65535):
        raise ValueError(
            f"alert_digest smtp_port must be an integer between 1 "
            f"and 65535, not {port!r}")
    user = env.get(_SMTP_ENV_VARS["user"])
    if user is None:
        user = digest_cfg.get("smtp_user")
    if user is not None and not isinstance(user, str):
        raise ValueError(
            f"alert_digest smtp_user must be a string, not {user!r}")
    user = user or None
    password = env.get(_SMTP_ENV_VARS["password"])
    if password is None:
        password = digest_cfg.get("smtp_password")
    if password is not None and not isinstance(password, str):
        raise ValueError(
            f"alert_digest smtp_password must be a string, "
            f"not {password!r}")
    password = password or None
    starttls = digest_cfg.get("smtp_starttls", True)
    starttls_env = env.get(_SMTP_ENV_VARS["starttls"])
    if starttls_env:
        starttls = _parse_bool_env(starttls_env,
                                   _SMTP_ENV_VARS["starttls"])
    if not isinstance(starttls, bool):
        raise ValueError(
            f"alert_digest smtp_starttls must be true or false, "
            f"not {starttls!r}")
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "starttls": starttls,
    }


def _default_smtp_factory(host, port, timeout, implicit_tls):
    if implicit_tls:
        return smtplib.SMTP_SSL(host, port, timeout=timeout)
    return smtplib.SMTP(host, port, timeout=timeout)


def send_digest_email(message, sender, recipients, smtp,
                      smtp_factory=None, timeout=DEFAULT_SMTP_TIMEOUT_SECONDS):
    """Send a rendered digest message via SMTP.

    `smtp` is a resolved config (see resolve_smtp_config): host,
    port, optional user/password, and starttls. Port 465 uses
    implicit TLS; any other port upgrades with STARTTLS when
    `starttls` is set, and otherwise sends in the clear. Logs in
    with `user`/`password` when a user is set. Returns True when
    the message was accepted, False on any failure — never raises,
    so a dead mail server can never take the monitor down. The
    `smtp_factory` argument exists for tests: a callable taking
    (host, port, timeout, implicit_tls) and returning an object
    with the smtplib.SMTP interface.
    """
    factory = smtp_factory or _default_smtp_factory
    host, port = smtp["host"], smtp["port"]
    implicit_tls = port == IMPLICIT_TLS_SMTP_PORT
    try:
        server = factory(host, port, timeout, implicit_tls)
        try:
            if not implicit_tls and smtp["starttls"]:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if smtp["user"]:
                server.login(smtp["user"], smtp["password"] or "")
            server.sendmail(sender, recipients, message)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except Exception as exc:  # refused, timeout, auth, TLS, send error
        log.warning("alert digest email delivery via SMTP %s:%d "
                    "failed (%s)", host, port, exc)
        return False
    log.info("alert digest email sent via SMTP %s:%d to %s",
             host, port, ", ".join(recipients))
    return True


def validate_digest_config(value):
    """Validate the top-level `alert_digest` config block.

    Returns a normalized dict: interval_seconds filled in (default
    900), email_to normalized to a list, email_from filled in
    (default "healthcheck@localhost"), and `smtp` holding the
    resolved SMTP settings (see resolve_smtp_config) or None when
    no SMTP host is configured — the digest email is then only
    rendered and logged, never sent. Raises ValueError on a bad
    block — the monitor refuses to start rather than run with a
    digest that can never be delivered.
    """
    if not isinstance(value, dict):
        raise ValueError(
            f"alert_digest must be an object, not {value!r}")
    unknown = sorted(set(value) - _DIGEST_KEYS)
    if unknown:
        raise ValueError(
            f"alert_digest has unknown key(s): {', '.join(unknown)}")
    interval = value.get("interval_seconds",
                         DEFAULT_DIGEST_INTERVAL_SECONDS)
    if (isinstance(interval, bool) or not isinstance(interval, int)
            or interval < 1):
        raise ValueError(
            f"alert_digest interval_seconds must be a positive "
            f"integer, not {interval!r}")
    webhook_url = value.get("webhook_url")
    if webhook_url is not None:
        if (not isinstance(webhook_url, str) or not webhook_url
                or not webhook_url.startswith(("http://", "https://"))):
            raise ValueError(
                f"alert_digest webhook_url must be an http(s) URL, "
                f"not {webhook_url!r}")
    email_to = value.get("email_to")
    recipients = None
    if email_to is not None:
        if isinstance(email_to, str) and email_to:
            recipients = [email_to]
        elif (isinstance(email_to, list) and email_to
              and all(isinstance(r, str) and r for r in email_to)):
            recipients = list(email_to)
        else:
            raise ValueError(
                f"alert_digest email_to must be an address or a "
                f"non-empty list of addresses, not {email_to!r}")
    email_from = value.get("email_from", DEFAULT_DIGEST_SENDER)
    if not isinstance(email_from, str) or not email_from:
        raise ValueError(
            f"alert_digest email_from must be a non-empty string, "
            f"not {email_from!r}")
    if webhook_url is None and recipients is None:
        raise ValueError(
            "alert_digest needs somewhere to send to: set "
            "webhook_url and/or email_to")
    smtp = resolve_smtp_config(value)
    return {
        "interval_seconds": interval,
        "webhook_url": webhook_url,
        "email_to": recipients,
        "email_from": email_from,
        "smtp": smtp,
    }


def flapping_endpoints(changes, threshold=FLAPPING_CHANGE_THRESHOLD):
    """Names that changed confirmed state `threshold` or more times
    in this window, most changes first (ties broken by name)."""
    counts = {}
    for change in changes:
        counts[change["endpoint"]] = counts.get(change["endpoint"], 0) + 1
    return sorted((name for name, n in counts.items() if n >= threshold),
                  key=lambda name: (-counts[name], name))


def build_digest(changes, currently_down, window_start, window_end):
    """The digest payload for one window.

    `changes` are state-change payloads (see alerts.alert_payload);
    only the ones with checked_at inside [window_start, window_end)
    are included — a change outside the window belongs to another
    digest, never this one. `currently_down` is the list of endpoint
    names confirmed down at window_end.
    """
    in_window = [c for c in changes
                 if window_start <= c["checked_at"] < window_end]
    down = sum(1 for c in in_window if not c["ok"])
    recovered = sum(1 for c in in_window if c["ok"])
    flapping = flapping_endpoints(in_window)
    return {
        "type": "digest",
        "window_start": window_start,
        "window_end": window_end,
        "changes": in_window,
        "currently_down": sorted(currently_down),
        "flapping": flapping,
        "summary": {
            "changes": len(in_window),
            "down": down,
            "recovered": recovered,
            "currently_down": len(currently_down),
            "flapping": len(flapping),
        },
    }


def _fmt_time(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def render_digest_body(digest):
    """Plain-text body of the digest email."""
    lines = [
        "Healthcheck alert digest",
        f"Window: {_fmt_time(digest['window_start'])} - "
        f"{_fmt_time(digest['window_end'])} (local time)",
        "",
    ]
    changes = digest["changes"]
    lines.append(f"State changes ({len(changes)}):")
    if not changes:
        lines.append("  (none)")
    for change in changes:
        verb = "RECOVERED" if change["ok"] else "went DOWN"
        detail = change.get("error") or ""
        if change.get("status_code"):
            detail = f"HTTP {change['status_code']}" + (
                f" - {detail}" if detail else "")
        lines.append(f"  - {change['endpoint']} {verb} at "
                     f"{_fmt_time(change['checked_at'])}"
                     + (f" ({detail})" if detail else ""))
    lines.append("")
    down = digest["currently_down"]
    lines.append(f"Currently down ({len(down)}):")
    if not down:
        lines.append("  (none)")
    for name in down:
        lines.append(f"  - {name}")
    lines.append("")
    flapping = digest["flapping"]
    lines.append(f"Flapping ({len(flapping)}):")
    if not flapping:
        lines.append("  (none)")
    for name in flapping:
        count = sum(1 for c in changes if c["endpoint"] == name)
        lines.append(f"  - {name} ({count} changes in this window)")
    return "\n".join(lines) + "\n"


def render_digest_email(digest, sender, recipients):
    """The digest as a MIME message string (plain text).

    Delivered over SMTP by DigestAlerter when an SMTP host is
    configured; otherwise the caller prints or logs this string,
    so it can be piped to sendmail.
    """
    summary = digest["summary"]
    subject = (f"healthcheck digest: {summary['changes']} change(s), "
               f"{summary['currently_down']} currently down")
    if summary["flapping"]:
        subject += f", {summary['flapping']} flapping"
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(render_digest_body(digest))
    return msg.as_string()


class DigestAlerter:
    """Batches confirmed state changes into a periodic digest.

    Wraps an optional per-change WebhookAlerter (pass None for
    digest-only alerting): `notify` delegates to it and records the
    change payload. `maybe_send(now, current_down)` sends the digest
    when the interval has elapsed since the last one; an empty
    window — no changes, nothing currently down — sends nothing.
    The window advances on every due send, delivered or not.

    `routes` is an optional per-endpoint override, a mapping of
    endpoint name to webhook URL: a routed endpoint's changes go in
    a digest POSTed to its own webhook, while every other
    endpoint's changes go in a digest POSTed to the digest block's
    `webhook_url` (or nowhere, when the block has no webhook and
    only an email destination). The rendered email always covers
    every endpoint, whatever the routing.
    """

    def __init__(self, config, inner=None, now_fn=None,
                 smtp_factory=None, routes=None):
        self.config = validate_digest_config(config)
        self.inner = inner
        self._now = now_fn or time.time
        self._smtp_factory = smtp_factory
        self.routes = dict(routes) if routes else {}
        self._changes = []
        self._window_start = self._now()
        self._last_ok = {}

    def _webhook_for(self, name):
        """The digest webhook URL for `name`'s changes: its own
        webhook when one is configured, else the digest block's."""
        return self.routes.get(name) or self.config["webhook_url"]

    def notify(self, result, cooldown_s=None):
        """Record a confirmed state change; delegates to the inner
        alerter when one is set. Returns the payload, or None when
        nothing changed — the same contract as WebhookAlerter.notify.

        `cooldown_s` is forwarded to the inner alerter, so a still-
        down endpoint re-pages through it on the same schedule as
        a bare webhook alerter. A repeat page is not a state
        change, so it is returned but not recorded in the digest's
        changes — recording it would count it as a fresh "went
        DOWN" in the digest and inflate the flapping count, while
        the endpoint already appears in the digest's currently-down
        list. With no inner alerter there is no immediate page to
        repeat, so only transitions are recorded.
        """
        if self.inner is not None:
            payload = self.inner.notify(result, cooldown_s=cooldown_s)
            if payload is not None and not payload.get("repeat"):
                self._changes.append(payload)
            return payload
        previous = self._last_ok.get(result.name)
        self._last_ok[result.name] = result.ok
        if previous is None or previous == result.ok:
            return None
        payload = alert_payload(result, previous)
        self._changes.append(payload)
        return payload

    def maybe_send(self, now=None, current_down=()):
        """Send the digest if the interval has elapsed.

        Returns None when not due, or when the window is empty (no
        changes, nothing currently down) — an empty digest is noise,
        not signal. Otherwise returns a dict with the digest
        payload (all endpoints, for the email), whether the webhook
        delivery succeeded — a single bool when the window went to
        one webhook, a {url: bool} mapping when per-endpoint routing
        split it across several, None when no webhook is configured
        — the rendered email message (None when no email_to is
        configured), and whether the SMTP delivery succeeded (None
        when no SMTP host is configured — the rendered message is
        then the operator's copy, logged by the caller). A failed
        SMTP delivery never raises and never stops the monitor: it
        is logged and reported as False, and the window still
        advances.
        """
        now = self._now() if now is None else now
        if now - self._window_start < self.config["interval_seconds"]:
            return None
        window_start, window_end = self._window_start, now
        self._window_start = now
        digest = build_digest(self._changes, current_down,
                              window_start, window_end)
        self._changes = []
        if not digest["changes"] and not digest["currently_down"]:
            return None
        # Split the window by webhook destination: each routed
        # endpoint's changes (and its currently-down entry) go in a
        # digest POSTed to its own webhook; the rest go to the
        # digest block's webhook. A group with no changes and
        # nothing down sends nothing — an ongoing outage is still
        # reported, but only to the channel that owns the endpoint.
        groups = {}
        for change in digest["changes"]:
            url = self._webhook_for(change["endpoint"])
            groups.setdefault(url, {"changes": [], "down": []})
            groups[url]["changes"].append(change)
        for name in digest["currently_down"]:
            url = self._webhook_for(name)
            groups.setdefault(url, {"changes": [], "down": []})
            groups[url]["down"].append(name)
        webhook_results = {}
        for url, group in groups.items():
            if url is None:
                continue
            if not group["changes"] and not group["down"]:
                continue
            routed = build_digest(group["changes"], group["down"],
                                  window_start, window_end)
            ok = post_alert(url, routed)
            webhook_results[url] = ok
            if ok:
                log.info("alert digest sent (%d change(s)) to %s",
                         routed["summary"]["changes"], url)
        if not webhook_results:
            webhook_ok = None
        elif len(webhook_results) == 1:
            webhook_ok = next(iter(webhook_results.values()))
        else:
            webhook_ok = webhook_results
        email = None
        email_ok = None
        if self.config["email_to"]:
            email = render_digest_email(
                digest, self.config["email_from"],
                self.config["email_to"])
            if self.config["smtp"] is not None:
                email_ok = send_digest_email(
                    email, self.config["email_from"],
                    self.config["email_to"], self.config["smtp"],
                    smtp_factory=self._smtp_factory)
        return {"digest": digest, "webhook_ok": webhook_ok,
                "email": email, "email_ok": email_ok}
