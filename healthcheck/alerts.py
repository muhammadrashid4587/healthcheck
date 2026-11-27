"""Configurable alert hooks: POST a JSON payload to a webhook URL when an
endpoint changes state (up -> down or down -> up).

Pure Python 3 standard library. A failed delivery is a warning, never a
crash — the monitor must keep running even if the webhook is down.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from .checker import (DEFAULT_CERT_EXPIRY_WARN_DAYS, cert_expiring_soon,
                      cert_expiry_days)
from .sla import breaches, sla_report

log = logging.getLogger("healthcheck.alerts")


def alert_payload(result, previous_ok):
    """JSON payload for a state change. `previous_ok` is the endpoint's
    state before this check (never None: the first check of an endpoint
    establishes its baseline and sends nothing)."""
    return {
        "endpoint": result.name,
        "event": "recovered" if result.ok else "down",
        "ok": result.ok,
        "previous_ok": previous_ok,
        "latency_ms": round(result.latency_ms, 1),
        "status_code": result.status_code,
        "error": result.error,
        "checked_at": result.checked_at,
    }


def post_alert(url, payload, timeout=5.0):
    """POST payload as JSON to url; True on a 2xx response, False otherwise.

    Never raises: a failed delivery is logged and reported as False.
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            resp.read(1)
            return 200 <= resp.status < 300
    except Exception as exc:  # timeout, refused connection, HTTP error
        log.warning("alert webhook %s failed (%s)", url, exc)
        return False


class WebhookAlerter:
    """Fires a webhook alert when an endpoint changes state.

    The first check of an endpoint establishes its baseline and sends
    nothing — an alert means a *change*, not the initial state.

    `url` is the default webhook for every endpoint. `routes` is an
    optional per-endpoint override, a mapping of endpoint name to
    webhook URL: an endpoint with its own webhook alerts its own
    channel, and falls back to `url` when it has none. Either or
    both may be absent — an endpoint with no URL anywhere sends
    nothing, but its state is still tracked.

    An endpoint that stays down can page again: when `notify` is
    called with a `cooldown_s` for an endpoint that is still down
    and that many seconds have passed since its last down alert,
    the down alert fires again, marked `"repeat": True` in the
    payload, so a long outage re-pages instead of going quiet
    after the first page. With no cooldown the endpoint pages
    exactly once per outage, like before. A recovery always pages
    (when the outage paged) and clears the cooldown state, so the
    next outage pages immediately, whatever the cooldown was.

    `records`, when given, is a dict the alerter writes into:
    `records[name] = {"alerted_at": <epoch seconds>}` for the last
    down alert fired for the endpoint's current outage. The
    dashboard server reads the same dict (passed through
    `run_forever`) to show when each endpoint last paged; a
    recovery deletes the record, so the slate is clean for the
    next outage. The records also seed the cooldown clock, so a
    SIGHUP reload (which recreates this alerter) does not reset
    the time since the last page — though a reload does reset flap
    state, so a mid-outage endpoint re-pages once it re-confirms,
    even inside the cooldown, and the cooldown then governs
    repeats after that page.
    """

    def __init__(self, url, timeout=5.0, routes=None, records=None):
        self.url = url
        self.timeout = timeout
        self.routes = dict(routes) if routes else {}
        self._last_ok = {}
        self.records = records if records is not None else {}
        # Epoch seconds of the last down alert per endpoint, seeded
        # from the shared records so a reload does not reset the
        # time since the last page (see the class docs).
        self._last_alert_at = {
            name: rec["alerted_at"]
            for name, rec in self.records.items()
            if isinstance(rec, dict) and "alerted_at" in rec
        }

    def url_for(self, name):
        """The webhook URL an alert for `name` goes to, or None."""
        return self.routes.get(name) or self.url

    def last_alerted_at(self, name):
        """Epoch seconds `name`'s current outage last paged, or
        None when it never paged (or has recovered since)."""
        return self._last_alert_at.get(name)

    def notify(self, result, cooldown_s=None):
        """Send an alert if result.ok differs from the last known state.

        Returns the payload sent, or None when nothing was sent (first
        check, no change, no webhook URL for this endpoint, or a
        still-down endpoint inside its cooldown). Delivery failure
        does not change the return value: the transition was real
        even if the webhook was unreachable.

        `cooldown_s` is the endpoint's alert cooldown in seconds
        (see the class docs): while the endpoint stays down, a
        repeat down alert fires once that many seconds have passed
        since the last one, with `"repeat": True` in the payload —
        and `previous_ok` False, since the endpoint was already
        down. A repeat is still a page: it is recorded as the last
        alert, so the next repeat waits a full cooldown from it.
        An endpoint with no webhook URL anywhere records nothing,
        like the SLA and cert-expiry alerters — no page went out,
        so there is no last alert to count from.
        """
        previous = self._last_ok.get(result.name)
        self._last_ok[result.name] = result.ok
        if previous is None:
            return None
        if previous == result.ok:
            if not result.ok:
                # Still down: a repeat page, but only once the
                # cooldown has elapsed since the last down alert.
                # No cooldown configured, or never paged (a fresh
                # alerter whose first check already saw it down):
                # nothing to repeat.
                last = self._last_alert_at.get(result.name)
                if (cooldown_s is None or last is None
                        or result.checked_at - last < cooldown_s):
                    return None
                return self._send_down(result, previous)
            return None
        if result.ok:
            # Recovery: the outage paged, so the recovery pages
            # too — and the cooldown state is cleared, so the
            # next outage pages immediately. The outage is over
            # whether or not the recovery page had anywhere to
            # go, so the state is cleared either way.
            payload = self._send(result, previous)
            self._last_alert_at.pop(result.name, None)
            self.records.pop(result.name, None)
            return payload
        # A fresh outage pages at once: the cooldown only spaces
        # repeats of the same outage, never the first page of a
        # new one.
        payload = self._send(result, previous)
        if payload is not None:
            self._last_alert_at[result.name] = result.checked_at
            self.records[result.name] = {
                "alerted_at": result.checked_at}
        return payload

    def _send(self, result, previous_ok, repeat=False):
        """POST the state-change payload for `result`, or None when
        there is no webhook URL for the endpoint. A repeat down
        alert carries `"repeat": True` so the receiver can tell it
        from the first page of the outage."""
        payload = alert_payload(result, previous_ok)
        if repeat:
            payload["repeat"] = True
        url = self.url_for(result.name)
        if url is None:
            return None
        if post_alert(url, payload, timeout=self.timeout):
            log.info("alert sent for %s: %s", result.name, payload["event"])
        return payload

    def _send_down(self, result, previous_ok):
        """Fire (and record) a down alert for an endpoint that is
        still down: the repeat page. Returns the payload, or None
        when there is no webhook URL for the endpoint — nothing
        went out, so the cooldown clock does not advance."""
        payload = self._send(result, previous_ok, repeat=True)
        if payload is not None:
            self._last_alert_at[result.name] = result.checked_at
            self.records[result.name] = {
                "alerted_at": result.checked_at}
        return payload


def sla_breach_payload(row, year, month):
    """JSON payload for an SLA breach: one endpoint that fell short
    of its configured `sla_target` in a calendar month.

    `row` is one `sla_report` row (so `met` is False here); the
    payload carries the month, the month's uptime and how many
    checks it is based on, the target and the shortfall in
    percentage points, plus the month detail — incidents and
    downtime inside the month, and latency p50/p95/p99 over it —
    so the receiver can judge the breach without fetching the
    report again.
    """
    return {
        "type": "sla_breach",
        "endpoint": row["name"],
        "month": f"{year:04d}-{month:02d}",
        "uptime_pct": row["uptime_pct"],
        "checks": row["checks"],
        "incidents": row["incidents"],
        "downtime_seconds": row["downtime_seconds"],
        "target": row["target"],
        "shortfall_pct": row["shortfall_pct"],
        "latency": row["latency"],
    }


def cert_expiry_alert_payload(endpoint, expires_at, days_remaining,
                              warn_days, checked_at):
    """JSON payload for a TLS certificate-expiry alert: an endpoint
    whose certificate entered its warning window.

    The endpoint stays UP — an expiring cert is a warning, not
    downtime — so the payload carries the expiry itself (epoch
    seconds), the whole days left, and the endpoint's warning
    window in days, so the receiver can judge the urgency without
    fetching the status again.
    """
    return {
        "type": "cert_expiry",
        "endpoint": endpoint,
        "cert_expires_at": expires_at,
        "days_remaining": days_remaining,
        "warn_days": warn_days,
        "checked_at": checked_at,
    }


class CertExpiryAlerter:
    """Fires a webhook alert when an endpoint's TLS certificate
    enters its warning window.

    Built on the same expiry the checks already record: an https
    endpoint whose certificate expires within its
    `cert_expiry_warn_days` (30 by default) gets one webhook POST
    with the expiry details (see `cert_expiry_alert_payload`). The
    alert goes to the endpoint's own `webhook` URL when it has
    one, else the global `webhook_url` — the same routing as
    state-change and SLA breach alerts — and an endpoint with no
    URL anywhere sends nothing. The endpoint stays UP: the alert
    is a warning, not a state change, so it never touches the
    flap detector or the state-change alerter.

    One alert per entry into the window: the first check that
    finds the certificate inside the window fires, and later
    checks while the same certificate is still inside the window
    do not re-fire — the dashboard and GET /api/status keep
    showing the live days left, so a cert creeping toward expiry
    stays visible there. A renewed certificate has a different
    expiry, so when it later enters the window it fires again.
    A failed delivery is logged by `post_alert` and never stops
    the monitor, and the alert is still recorded as fired — like
    the SLA breach alerter, a failed alert is not re-sent.

    `records`, when given, is a dict the alerter writes into:
    `records[name] = {"cert_expires_at", "warn_days",
    "alerted_at"}` for each alert fired. The dashboard server
    reads the same dict (passed through `run_forever`) to show
    when each alert fired; without it the record lives only on
    the alerter.
    """

    def __init__(self, url, timeout=5.0, routes=None, records=None):
        self.url = url
        self.timeout = timeout
        self.routes = dict(routes) if routes else {}
        self.records = records if records is not None else {}
        # Certificates already alerted for, keyed by endpoint:
        # seeded from the shared records, so a SIGHUP reload
        # (which recreates this alerter) does not re-fire an
        # alert sent for the same certificate.
        self._alerted = {
            name: rec["cert_expires_at"]
            for name, rec in self.records.items()
            if isinstance(rec, dict) and "cert_expires_at" in rec
        }

    def url_for(self, name):
        """The webhook URL a cert-expiry alert for `name` goes to."""
        return self.routes.get(name) or self.url

    def alerted_for(self, name):
        """The alert record for `name`, or None when it never fired."""
        return self.records.get(name)

    def maybe_alert(self, history, endpoints, now=None):
        """Alert on every endpoint whose certificate entered its
        warning window.

        Returns the list of payloads sent (attempted — a failed
        delivery still counts as sent, see the class docs), in
        config order. `now` pins the window in tests; it defaults
        to the current time. Endpoints with no recorded expiry
        (plain http / tcp, never checked, or a check that could
        not read the peer certificate) send nothing — an unknown
        expiry is not a warning.
        """
        ref = time.time() if now is None else now
        sent = []
        for ep in endpoints:
            name = ep["name"]
            expires_at = history.cert_expiry(name)
            if expires_at is None:
                continue
            warn_days = ep.get("cert_expiry_warn_days")
            if warn_days is None:
                warn_days = DEFAULT_CERT_EXPIRY_WARN_DAYS
            if not cert_expiring_soon(expires_at, warn_days, now=ref):
                continue
            if self._alerted.get(name) == expires_at:
                # Already alerted for this certificate: still
                # inside the same window, not a new entry.
                continue
            url = self.url_for(name)
            if url is None:
                continue
            current = history.current(name)
            payload = cert_expiry_alert_payload(
                name, expires_at,
                cert_expiry_days(expires_at, now=ref), warn_days,
                current["checked_at"] if current is not None else ref)
            if post_alert(url, payload, timeout=self.timeout):
                log.info("cert expiry alert sent for %s: %d day(s) "
                         "left (warn window %s days)",
                         name, payload["days_remaining"], warn_days)
            self._alerted[name] = expires_at
            self.records[name] = {
                "cert_expires_at": expires_at,
                "warn_days": warn_days,
                "alerted_at": ref,
            }
            sent.append(payload)
        return sent


class SlaBreachAlerter:
    """Fires a webhook alert when an endpoint breaches its SLA target.

    Built on the same monthly report as `--sla-report`: an endpoint
    with an `sla_target` whose uptime for the month fell short gets
    one webhook POST with the breach details (see
    `sla_breach_payload`). The alert goes to the endpoint's own
    `webhook` URL when it has one, else the global `webhook_url` —
    the same routing as state-change alerts — and an endpoint with
    no URL anywhere sends nothing. Either or both may be absent:
    with no webhook configured at all the alerter still exists but
    `maybe_alert` returns [].

    One alert per endpoint per month: the first time a month's
    report shows a breach, the alert fires and is recorded; later
    rounds in the same month do not re-fire, even if the shortfall
    grows — the dashboard and GET /api/status keep showing the
    live shortfall, so a worsening breach stays visible there. A
    new month starts fresh. A failed delivery is logged by
    `post_alert` and never stops the monitor, and the breach is
    still recorded as alerted — like the digest, a failed alert is
    not re-sent.

    `records`, when given, is a dict the alerter writes into:
    `records[name] = {"month", "shortfall_pct", "alerted_at"}` for
    each breach alerted. The dashboard server reads the same dict
    (passed through `run_forever`) to show when each breach was
    alerted; without it the record lives only on the alerter.
    """

    def __init__(self, url, timeout=5.0, routes=None, records=None):
        self.url = url
        self.timeout = timeout
        self.routes = dict(routes) if routes else {}
        self.records = records if records is not None else {}
        # (endpoint, month) pairs already alerted — seeded from the
        # shared records, so a SIGHUP reload (which recreates this
        # alerter) does not re-fire a breach alerted this month.
        self._alerted = {
            (name, rec["month"])
            for name, rec in self.records.items()
            if isinstance(rec, dict) and "month" in rec
        }

    def url_for(self, name):
        """The webhook URL an SLA breach alert for `name` goes to."""
        return self.routes.get(name) or self.url

    def alerted_for(self, name):
        """The alert record for `name`, or None when it never fired."""
        return self.records.get(name)

    def maybe_alert(self, history, endpoints, year=None, month=None,
                    now=None):
        """Alert on every breach in the month's SLA report.

        With no month given, the current calendar month (local time)
        is used. Returns the list of payloads sent (attempted — a
        failed delivery still counts as sent, see the class docs),
        in config order. `now` pins the alerted_at timestamp in
        tests; it defaults to the current time.
        """
        if year is None or month is None:
            current = time.localtime()
            year, month = current.tm_year, current.tm_mon
        month_str = f"{year:04d}-{month:02d}"
        report = sla_report(history, endpoints, year, month)
        sent = []
        for row in breaches(report):
            key = (row["name"], month_str)
            if key in self._alerted:
                continue
            url = self.url_for(row["name"])
            if url is None:
                continue
            payload = sla_breach_payload(row, year, month)
            if post_alert(url, payload, timeout=self.timeout):
                log.info("SLA breach alert sent for %s: %.1f%% vs "
                         "target %.1f%% (%s)",
                         row["name"], row["uptime_pct"], row["target"],
                         month_str)
            self._alerted.add(key)
            self.records[row["name"]] = {
                "month": month_str,
                "shortfall_pct": row["shortfall_pct"],
                "alerted_at": time.time() if now is None else now,
            }
            sent.append(payload)
        return sent
