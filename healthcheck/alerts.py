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
    """

    def __init__(self, url, timeout=5.0):
        self.url = url
        self.timeout = timeout
        self._last_ok = {}

    def notify(self, result):
        """Send an alert if result.ok differs from the last known state.

        Returns the payload sent, or None when nothing was sent (first
        check, or no change). Delivery failure does not change the
        return value: the transition was real even if the webhook
        was unreachable.
        """
        previous = self._last_ok.get(result.name)
        self._last_ok[result.name] = result.ok
        if previous is None or previous == result.ok:
            return None
        payload = alert_payload(result, previous)
        if post_alert(self.url, payload, timeout=self.timeout):
            log.info("alert sent for %s: %s", result.name, payload["event"])
        return payload
