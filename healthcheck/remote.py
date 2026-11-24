"""Remote status-page source: scrape another healthcheck instance's
`GET /api/status` over HTTP.

`fetch_remote_status` fetches the JSON snapshot a remote instance
serves (the same shape `status_snapshot` returns) and wraps it in an
`ApiStatusHistory` — a `History` that answers from the snapshot
instead of recorded checks, so the static status page can render the
remote instance under its own labelled section with the same rules
as a local source.

A snapshot is not history: it carries the remote instance's current
status and its uptime figures (all-time, 24h, 7d, computed by the
remote instance with the same rules), but no per-check records. So
there is no latency sparkline and no incident list for a remote
source — the page shows "-" for the sparkline and "No incidents
recorded." rather than inventing either from one sample. A remote
endpoint that is DOWN at scrape time is still counted down in the
section banner, like a local one.

An unreachable instance, a non-200 response, bad JSON, or a payload
that is not a status snapshot logs a warning and returns None — the
caller renders that source's section empty instead of taking the
whole page down.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from .store import History

log = logging.getLogger("healthcheck")

# Statuses a remote instance can report for an endpoint; anything
# else in a payload is not a status snapshot we understand.
KNOWN_STATUSES = ("UP", "DOWN", "SKIPPED", "MAINTENANCE", "UNKNOWN")

# How long to wait for a remote instance's /api/status before giving
# up on that source (seconds).
DEFAULT_TIMEOUT = 5.0


class ApiStatusHistory(History):
    """A History backed by one remote /api/status snapshot.

    Uptime and latency figures come straight from the snapshot —
    they were computed by the remote instance with the same rules
    as the local history math — while `recent` is empty, because a
    snapshot carries no per-check records: no sparkline, no
    incidents. `current` synthesises one entry per endpoint from
    its snapshot status, so `endpoint_status` and the page table
    judge it exactly like a local endpoint.
    """

    def __init__(self, snapshot, maxlen=100):
        super().__init__(maxlen=maxlen)
        if not isinstance(snapshot, dict):
            raise ValueError(
                f"remote status snapshot must be an object, not "
                f"{type(snapshot).__name__}")
        endpoints = snapshot.get("endpoints")
        if not isinstance(endpoints, list):
            raise ValueError(
                "remote status snapshot has no 'endpoints' list")
        self._snapshot = {}
        for ep in endpoints:
            if not isinstance(ep, dict) or not isinstance(
                    ep.get("name"), str):
                raise ValueError(
                    f"remote status endpoint is not an object with a "
                    f"name: {ep!r}")
            status = ep.get("status")
            if status not in KNOWN_STATUSES:
                raise ValueError(
                    f"remote endpoint {ep['name']!r} has unknown "
                    f"status {status!r}")
            for key in ("uptime_pct", "uptime_24h_pct", "uptime_7d_pct",
                        "uptime_30d_pct",
                        "latency_ms", "last_checked_at"):
                value = ep.get(key)
                if value is not None and not isinstance(value, (int, float)):
                    raise ValueError(
                        f"remote endpoint {ep['name']!r}: {key} must be "
                        f"a number or null, not {value!r}")
            self._snapshot[ep["name"]] = ep
        self._generated_at = snapshot.get("generated_at")

    def names(self):
        return sorted(self._snapshot)

    def _entry(self, name):
        ep = self._snapshot.get(name)
        if ep is None:
            return None
        status = ep["status"]
        if status == "UNKNOWN":
            return None
        checked_at = ep.get("last_checked_at")
        if checked_at is None:
            checked_at = self._generated_at or time.time()
        entry = {
            "ok": status == "UP",
            "latency_ms": round(ep["latency_ms"], 1)
            if ep.get("latency_ms") is not None else 0.0,
            "status_code": None,
            "error": None,
            "checked_at": checked_at,
            "skipped": status == "SKIPPED",
            "depends_on": ep.get("depends_on"),
            "unconfirmed": False,
            "maintenance": status == "MAINTENANCE",
        }
        if status == "SKIPPED" and ep.get("depends_on"):
            # The snapshot does not say why the dependency is not
            # up; name it, so the page shows the reason like a
            # local skip does.
            entry["error"] = (f"dependency {ep['depends_on']!r} is "
                              f"not up")
        return entry

    def current(self, name):
        return self._entry(name)

    def recent(self, name, n=10):
        # A snapshot has no per-check records: no sparkline and no
        # incidents can be derived from it, and none are invented.
        return []

    def uptime_pct(self, name):
        ep = self._snapshot.get(name)
        return None if ep is None else ep.get("uptime_pct")

    def uptime_pct_window(self, name, seconds, now=None):
        ep = self._snapshot.get(name)
        if ep is None:
            return None
        if seconds == 24 * 3600:
            return ep.get("uptime_24h_pct")
        if seconds == 7 * 24 * 3600:
            return ep.get("uptime_7d_pct")
        if seconds == 30 * 24 * 3600:
            return ep.get("uptime_30d_pct")
        return ep.get("uptime_pct")

    def latency_percentiles(self, name, seconds=24 * 3600, now=None):
        ep = self._snapshot.get(name)
        if ep is None:
            return None
        pct = ep.get("latency_percentiles")
        return dict(pct) if isinstance(pct, dict) else None

    def latency_summary(self, name, seconds=24 * 3600, now=None):
        # A snapshot has percentiles but no sum or count of the
        # latencies behind them, so there is no summary to report.
        return None


def _status_url(url):
    """The /api/status URL for a remote instance's base URL.

    A base URL gets "/api/status" appended; a URL that already
    points at /api/status is used as-is, so either form works.
    """
    base = url.rstrip("/")
    if base.endswith("/api/status"):
        return base
    return base + "/api/status"


def fetch_remote_status(url, timeout=DEFAULT_TIMEOUT):
    """Fetch a remote instance's /api/status as an ApiStatusHistory.

    `url` is the instance's base URL (or its /api/status URL
    directly). Returns None — after logging a warning — when the
    instance is unreachable, does not answer 200, returns bad
    JSON, or returns something that is not a status snapshot, so
    the caller can render that source empty instead of failing
    the whole page.
    """
    status_url = _status_url(url)
    try:
        with urllib.request.urlopen(status_url, timeout=timeout) as resp:
            if resp.status != 200:
                raise urllib.error.HTTPError(
                    status_url, resp.status, "non-200 response",
                    resp.headers, None)
            payload = json.load(resp)
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        log.warning("remote status source %s unreachable (%s); "
                    "rendering empty", url, exc)
        return None
    try:
        return ApiStatusHistory(payload)
    except ValueError as exc:
        log.warning("remote status source %s returned an invalid "
                    "snapshot (%s); rendering empty", url, exc)
        return None
