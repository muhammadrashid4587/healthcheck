"""Endpoint checks: HTTP status + latency, TCP connect.

Pure Python 3 standard library. A check returns a CheckResult; it never
raises for a failed endpoint — failure is data, not an exception.
"""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    latency_ms: float
    status_code: int | None = None
    error: str | None = None
    checked_at: float = 0.0


def check_http(name, url, timeout=5.0):
    """GET url; ok when the status is 2xx or 3xx."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            status = resp.status
            resp.read(1)  # make sure the response actually arrived
        latency = (time.monotonic() - started) * 1000
        return CheckResult(name=name, ok=200 <= status < 400,
                           latency_ms=latency, status_code=status,
                           checked_at=time.time())
    except urllib.error.HTTPError as exc:
        latency = (time.monotonic() - started) * 1000
        return CheckResult(name=name, ok=False, latency_ms=latency,
                           status_code=exc.code, error=f"HTTP {exc.code}",
                           checked_at=time.time())
    except Exception as exc:  # timeout, DNS failure, refused connection
        latency = (time.monotonic() - started) * 1000
        return CheckResult(name=name, ok=False, latency_ms=latency,
                           error=str(exc), checked_at=time.time())


def check_tcp(name, host, port, timeout=5.0):
    """Open a TCP connection; ok when the connect succeeds."""
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        latency = (time.monotonic() - started) * 1000
        return CheckResult(name=name, ok=True, latency_ms=latency,
                           checked_at=time.time())
    except Exception as exc:
        latency = (time.monotonic() - started) * 1000
        return CheckResult(name=name, ok=False, latency_ms=latency,
                           error=str(exc), checked_at=time.time())


def check_endpoint(endpoint, timeout=5.0):
    """Dispatch one config entry to the right checker."""
    kind = endpoint.get("type", "http")
    name = endpoint["name"]
    if kind == "tcp":
        return check_tcp(name, endpoint["host"], endpoint["port"], timeout)
    if kind == "http":
        return check_http(name, endpoint["url"], timeout)
    raise ValueError(f"unknown endpoint type: {kind!r}")
