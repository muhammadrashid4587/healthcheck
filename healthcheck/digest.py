"""Alert digests: batch state changes into a periodic summary.

Instead of (or in addition to) one webhook per state change, a digest
collects every confirmed change over a window (default 15 minutes)
and sends one summary: the changes in the window, the endpoints
currently down, and any endpoints that flapped (changed confirmed
state three or more times in the window). The digest is POSTed as
JSON to a webhook URL and/or rendered as a plain-text MIME email
message.

Pure Python 3 standard library. SMTP delivery is not wired up yet:
the rendered message is returned to the caller (the monitor logs
it), so it can be piped to sendmail or handed to an SMTP step later.
A failed webhook delivery is a warning, never a crash — the monitor
must keep running even if the webhook is down.
"""

from __future__ import annotations

import logging
import time
from email.message import EmailMessage

from .alerts import alert_payload, post_alert

log = logging.getLogger("healthcheck.digest")

DEFAULT_DIGEST_INTERVAL_SECONDS = 900  # 15 minutes
DEFAULT_DIGEST_SENDER = "healthcheck@localhost"
# An endpoint that changes confirmed state this many times in one
# digest window is flapping, not just having a bad day.
FLAPPING_CHANGE_THRESHOLD = 3

_DIGEST_KEYS = {"interval_seconds", "webhook_url", "email_to",
                "email_from"}


def validate_digest_config(value):
    """Validate the top-level `alert_digest` config block.

    Returns a normalized dict: interval_seconds filled in (default
    900), email_to normalized to a list, email_from filled in
    (default "healthcheck@localhost"). Raises ValueError on a bad
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
    return {
        "interval_seconds": interval,
        "webhook_url": webhook_url,
        "email_to": recipients,
        "email_from": email_from,
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

    SMTP delivery is not wired up yet: the caller prints or writes
    this string (the monitor logs it), so it can be piped to
    sendmail or handed to an SMTP step later.
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
    """

    def __init__(self, config, inner=None, now_fn=None):
        self.config = validate_digest_config(config)
        self.inner = inner
        self._now = now_fn or time.time
        self._changes = []
        self._window_start = self._now()
        self._last_ok = {}

    def notify(self, result):
        """Record a confirmed state change; delegates to the inner
        alerter when one is set. Returns the payload, or None when
        nothing changed — the same contract as WebhookAlerter.notify.
        """
        if self.inner is not None:
            payload = self.inner.notify(result)
            if payload is not None:
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
        payload, whether the webhook delivery succeeded (None when
        no webhook is configured), and the rendered email message
        (None when no email_to is configured).
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
        webhook_ok = None
        if self.config["webhook_url"]:
            webhook_ok = post_alert(self.config["webhook_url"], digest)
            if webhook_ok:
                log.info("alert digest sent (%d change(s))",
                         digest["summary"]["changes"])
        email = None
        if self.config["email_to"]:
            email = render_digest_email(
                digest, self.config["email_from"],
                self.config["email_to"])
        return {"digest": digest, "webhook_ok": webhook_ok,
                "email": email}
