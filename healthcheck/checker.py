"""Endpoint checks: HTTP status + latency, TCP connect.

Pure Python 3 standard library. A check returns a CheckResult; it never
raises for a failed endpoint — failure is data, not an exception.
"""

from __future__ import annotations

import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# Most body assertions target small status pages; never read more
# than this per check, so a huge response cannot stall the monitor.
MAX_BODY_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    latency_ms: float
    status_code: int | None = None
    error: str | None = None
    checked_at: float = 0.0
    skipped: bool = False
    depends_on: str | None = None
    # A failed check below the flap threshold: the failure really
    # happened, but the endpoint is not confirmed down yet — no alert,
    # no uptime penalty, no DOWN on the dashboard.
    unconfirmed: bool = False
    # The check never ran because the endpoint was in a maintenance
    # window: neither up nor down, no uptime penalty, no alert.
    maintenance: bool = False


def maintenance_result(name, summary):
    """A check that never ran because the endpoint was in a
    maintenance window. `ok` is False (nothing confirmed up) but
    `maintenance` marks it as neither up nor down: it must not count
    against uptime, must not fire alerts, and must not touch flap
    state. `summary` is the window's display string, e.g.
    "Sat 02:00–04:00".
    """
    return CheckResult(
        name=name, ok=False, latency_ms=0.0,
        error=f"maintenance window: {summary}",
        checked_at=time.time(), maintenance=True)


def skipped_result(name, depends_on, reason):
    """A check that never ran because its dependency was not UP.

    `ok` is False (nothing confirmed up) but `skipped` marks it as
    neither up nor down: it must not count against uptime and must
    not fire alerts. `reason` names the dependency's state, e.g.
    "dependency 'db' is DOWN".
    """
    return CheckResult(
        name=name, ok=False, latency_ms=0.0,
        error=f"skipped: {reason}", checked_at=time.time(),
        skipped=True, depends_on=depends_on)


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


def _check_body(name, body, latency, status, expect_body,
                  expect_body_regex):
    """Apply body assertions to a response that already had a good
    status. Returns a CheckResult: ok when every configured
    assertion holds, otherwise a failure naming the one that did
    not. `body` is raw bytes; it is decoded leniently, so a
    non-UTF-8 response fails the assertion instead of crashing the
    check. An uncompilable regex is a failed check, not a crash —
    config load rejects bad patterns first, but check_http must
    stay safe when called directly.
    """
    text = body.decode("utf-8", errors="replace")
    if expect_body is not None and expect_body not in text:
        return CheckResult(
            name=name, ok=False, latency_ms=latency, status_code=status,
            error=f"body does not contain {expect_body!r}",
            checked_at=time.time())
    if expect_body_regex is not None:
        try:
            pattern = re.compile(expect_body_regex)
        except re.error as exc:
            return CheckResult(
                name=name, ok=False, latency_ms=latency,
                status_code=status,
                error=f"invalid expect_body_regex {expect_body_regex!r}: {exc}",
                checked_at=time.time())
        if not pattern.search(text):
            return CheckResult(
                name=name, ok=False, latency_ms=latency,
                status_code=status,
                error=f"body does not match {expect_body_regex!r}",
                checked_at=time.time())
    return CheckResult(name=name, ok=True, latency_ms=latency,
                       status_code=status, checked_at=time.time())


def check_http(name, url, timeout=5.0, cafile=None, cert=None, key=None,
               expect_body=None, expect_body_regex=None,
               expect_status=None):
    """GET url; ok when the status is 2xx or 3xx.

    For https:// URLs, `cafile`/`cert`/`key` select a custom TLS
    context (see build_tls_context); without them the default context
    verifies against the system CA store. The options are ignored for
    plain http:// URLs. A bad cert file is a failed check, not a crash.

    `expect_body` is a substring the response body must contain and
    `expect_body_regex` a pattern it must match — a 200 that serves
    an error page is not "up". Either may be given, and when both
    are, both must hold. The body is only read when an assertion is
    configured, and never more than MAX_BODY_BYTES of it. A failed
    status (4xx/5xx) is already not ok, so no assertion runs there.

    `expect_status` is an exact status code the response must have —
    for a health endpoint that answers 204 and never 200, say. When
    it is set it replaces the 2xx/3xx rule entirely: any other status
    is DOWN, with the expected and actual codes named in the error.
    It is checked against the final response status, after any
    redirects are followed (urllib follows them, as before). Body
    assertions still apply once the status matches. A non-integer
    or out-of-range value raises ValueError — config load rejects
    bad values first, but check_http must stay safe when called
    directly.
    """
    if expect_status is not None and (
            isinstance(expect_status, bool)
            or not isinstance(expect_status, int)
            or not 100 <= expect_status <= 599):
        raise ValueError(
            f"expect_status must be an HTTP status code (100-599), "
            f"not {expect_status!r}")
    started = time.monotonic()
    try:
        context = None
        if url.lower().startswith("https://") and (
                cafile is not None or cert is not None or key is not None):
            context = build_tls_context(cafile=cafile, cert=cert, key=key)
        with urllib.request.urlopen(url, timeout=timeout,
                                    context=context) as resp:
            status = resp.status
            body = (resp.read(MAX_BODY_BYTES + 1)
                    if expect_body is not None
                    or expect_body_regex is not None else resp.read(1))
        latency = (time.monotonic() - started) * 1000
        if expect_status is not None:
            if status != expect_status:
                return CheckResult(
                    name=name, ok=False, latency_ms=latency,
                    status_code=status,
                    error=f"expected HTTP {expect_status}, got {status}",
                    checked_at=time.time())
        elif not 200 <= status < 400:
            return CheckResult(name=name, ok=False, latency_ms=latency,
                               status_code=status, error=f"HTTP {status}",
                               checked_at=time.time())
        if expect_body is not None or expect_body_regex is not None:
            return _check_body(name, body, latency, status,
                               expect_body, expect_body_regex)
        return CheckResult(name=name, ok=True, latency_ms=latency,
                           status_code=status, checked_at=time.time())
    except urllib.error.HTTPError as exc:
        latency = (time.monotonic() - started) * 1000
        if expect_status is not None and exc.code == expect_status:
            # The expected status arrived as an error response (4xx /
            # 5xx): it still counts, with body assertions applied.
            body = (exc.read(MAX_BODY_BYTES + 1)
                    if expect_body is not None
                    or expect_body_regex is not None else b"")
            if expect_body is not None or expect_body_regex is not None:
                return _check_body(name, body, latency, exc.code,
                                   expect_body, expect_body_regex)
            return CheckResult(name=name, ok=True, latency_ms=latency,
                               status_code=exc.code,
                               checked_at=time.time())
        if expect_status is not None:
            return CheckResult(
                name=name, ok=False, latency_ms=latency,
                status_code=exc.code,
                error=f"expected HTTP {expect_status}, got {exc.code}",
                checked_at=time.time())
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
                          key=endpoint.get("key"),
                          expect_body=endpoint.get("expect_body"),
                          expect_body_regex=endpoint.get("expect_body_regex"),
                          expect_status=endpoint.get("expect_status"))
    raise ValueError(f"unknown endpoint type: {kind!r}")
