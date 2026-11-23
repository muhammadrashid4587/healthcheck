"""Configurable alert hooks: POST a JSON payload to a webhook URL when an
endpoint changes state (up -> down or down -> up).

Pure Python 3 standard library. A failed delivery is a warning, never a
crash — the monitor must keep running even if the webhook is down.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

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
    """

    def __init__(self, url, timeout=5.0, routes=None):
        self.url = url
        self.timeout = timeout
        self.routes = dict(routes) if routes else {}
        self._last_ok = {}

    def url_for(self, name):
        """The webhook URL an alert for `name` goes to, or None."""
        return self.routes.get(name) or self.url

    def notify(self, result):
        """Send an alert if result.ok differs from the last known state.

        Returns the payload sent, or None when nothing was sent (first
        check, no change, or no webhook URL for this endpoint).
        Delivery failure does not change the return value: the
        transition was real even if the webhook was unreachable.
        """
        previous = self._last_ok.get(result.name)
        self._last_ok[result.name] = result.ok
        if previous is None or previous == result.ok:
            return None
        payload = alert_payload(result, previous)
        url = self.url_for(result.name)
        if url is None:
            return None
        if post_alert(url, payload, timeout=self.timeout):
            log.info("alert sent for %s: %s", result.name, payload["event"])
        return payload
