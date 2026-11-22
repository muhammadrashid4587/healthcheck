"""Endpoint checks: HTTP status + latency, TCP connect.

Pure Python 3 standard library. A check returns a CheckResult; it never
raises for a failed endpoint — failure is data, not an exception.
"""

from __future__ import annotations

import socket
import ssl
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


def build_tls_context(cafile=None, cert=None, key=None):
    """SSL context for an HTTPS check.

    `cafile` is a PEM bundle of CAs to trust in addition to (or instead
    of, when given) the system defaults — for endpoints behind a
    private or self-signed CA. `cert`/`key` are a client certificate
    and private key for mutual TLS. Any of them may be None.
    """
    context = ssl.create_default_context(cafile=cafile)
    if cert is not None:
        context.load_cert_chain(certfile=cert, keyfile=key)
    return context


def check_http(name, url, timeout=5.0, cafile=None, cert=None, key=None):
    """GET url; ok when the status is 2xx or 3xx.

    For https:// URLs, `cafile`/`cert`/`key` select a custom TLS
    context (see build_tls_context); without them the default context
    verifies against the system CA store. The options are ignored for
    plain http:// URLs. A bad cert file is a failed check, not a crash.
    """
    started = time.monotonic()
    try:
        context = None
        if url.lower().startswith("https://") and (
                cafile is not None or cert is not None or key is not None):
            context = build_tls_context(cafile=cafile, cert=cert, key=key)
        with urllib.request.urlopen(url, timeout=timeout,
                                    context=context) as resp:
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
        return check_http(name, endpoint["url"], timeout,
                          cafile=endpoint.get("cafile"),
                          cert=endpoint.get("cert"),
                          key=endpoint.get("key"))
    raise ValueError(f"unknown endpoint type: {kind!r}")
