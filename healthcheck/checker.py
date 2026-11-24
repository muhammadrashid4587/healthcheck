"""Endpoint checks: HTTP status + latency, TCP connect.

Pure Python 3 standard library. A check returns a CheckResult; it never
raises for a failed endpoint — failure is data, not an exception.
"""

from __future__ import annotations

import dataclasses
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

# Most body assertions target small status pages; never read more
# than this per check, so a huge response cannot stall the monitor.
MAX_BODY_BYTES = 1024 * 1024

# Days before a TLS certificate expires that the monitor starts
# warning about it (per-endpoint `cert_expiry_warn_days` overrides
# this). Thirty days is enough to renew and redeploy without
# paging anyone the moment a cert is minted.
DEFAULT_CERT_EXPIRY_WARN_DAYS = 30

# HTTP methods an endpoint may ask to be checked with. The check
# sends no body, so only methods that make sense without one are
# allowed — a POST health endpoint that needs a payload is out of
# scope for a monitor that only asks "are you up".
ALLOWED_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"})


def normalize_headers(headers):
    """Validate a per-endpoint request-headers map, returning it.

    Returns None when no headers were given (the check sends no
    extra headers, as before). Raises ValueError for anything
    else — a non-dict, a non-string key, an empty key, or a
    non-string value — so config load rejects it before the
    monitor ever runs. Keys are kept as configured (header names
    are case-insensitive on the wire); values are sent verbatim,
    so an Authorization token goes out exactly as written.
    """
    if headers is None:
        return None
    if not isinstance(headers, dict):
        raise ValueError(f"headers must be an object, not {headers!r}")
    for key, value in headers.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"headers must have string names, not {key!r}")
        if not isinstance(value, str):
            raise ValueError(
                f"header {key!r} must have a string value, "
                f"not {value!r}")
    return headers


def normalize_http_method(method):
    """Upper-case `method` when it is an allowed HTTP method.

    Returns None when no method was given (the check uses GET, as
    before). Raises ValueError for anything else — a non-string,
    an empty string, or a method outside ALLOWED_HTTP_METHODS —
    so config load rejects it before the monitor ever runs.
    """
    if method is None:
        return None
    if not isinstance(method, str) or not method.strip():
        raise ValueError(
            f"method must be an HTTP method, not {method!r}")
    upper = method.strip().upper()
    if upper not in ALLOWED_HTTP_METHODS:
        raise ValueError(
            f"method must be one of {sorted(ALLOWED_HTTP_METHODS)}, "
            f"not {method!r}")
    return upper


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
    # How many retries this check used: 0 when the first attempt
    # decided it (or retries were off), 1 when the first attempt
    # failed and the retry's result is the recorded one. A retry is
    # part of the same check round, not a second check — only one
    # history entry is recorded either way.
    retries: int = 0
    # Epoch second the peer's TLS certificate expires, for https
    # checks that could read it; None for plain http / tcp checks
    # and when the handshake failed, so no expiry was seen. The
    # expiry is advisory — it never decides up or down — but it is
    # recorded with the check so the dashboard and API can warn
    # before the cert actually lapses.
    cert_expires_at: float | None = None


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


def parse_cert_not_after(cert):
    """Epoch second a getpeercert() dict's certificate expires.

    `notAfter` is an ASN.1 time like "Nov 24 12:00:00 2028 GMT";
    the trailing zone token is dropped and the rest parsed as UTC.
    Returns None when the dict has no usable notAfter, so a peer
    that presents no certificate (or an odd one) is "unknown
    expiry", not a crash.
    """
    not_after = (cert or {}).get("notAfter")
    if not isinstance(not_after, str):
        return None
    try:
        stamp, _zone = not_after.rsplit(" ", 1)
        parsed = datetime.strptime(" ".join(stamp.split()),
                                   "%b %d %H:%M:%S %Y")
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=timezone.utc).timestamp()


def fetch_cert_expiry(host, port, context=None, timeout=5.0):
    """Epoch second the peer's TLS certificate expires, or None.

    Opens one short TLS handshake (verified with `context`, or
    the system CA store when None) and reads the peer
    certificate's notAfter. Any failure — refused connection,
    failed handshake, unparseable cert — is None, never an
    exception: expiry is advisory, the check's own verdict is
    what matters.
    """
    try:
        ctx = (context if context is not None
               else ssl.create_default_context())
        with socket.create_connection((host, port),
                                     timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        return parse_cert_not_after(cert)
    except Exception:
        return None


def _cert_expiry_for(url, context, timeout):
    """Peer certificate expiry (epoch) for an https URL, or None.

    A second, short TLS handshake against the same host: urllib
    does not expose the socket it used, so the expiry is read
    separately. Any failure (plain http, bad host, failed
    handshake) is None, never a crash — a check that cannot read
    the cert still reports its own result.
    """
    if not url.lower().startswith("https://"):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    try:
        port = parsed.port or 443
    except ValueError:
        return None
    return fetch_cert_expiry(parsed.hostname, port, context, timeout)


def cert_expiry_days(expires_at, now=None):
    """Whole days from `now` until `expires_at` (epoch), or None.

    Negative when the certificate already expired; None when no
    expiry was recorded — no certificate was seen, so there is
    nothing to count down from.
    """
    if expires_at is None:
        return None
    ref = time.time() if now is None else now
    return int((expires_at - ref) // 86400)


def cert_expiring_soon(expires_at, warn_days=DEFAULT_CERT_EXPIRY_WARN_DAYS,
                       now=None):
    """True when the certificate expires within `warn_days` days.

    False when no expiry was recorded — an unknown expiry is not
    a warning, it is just unknown. An already-expired certificate
    counts: it is within (past) the warning window.
    """
    if expires_at is None:
        return False
    ref = time.time() if now is None else now
    return expires_at - ref <= warn_days * 86400


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
               expect_status=None, method=None, headers=None):
    """Check url; ok when the status is 2xx or 3xx.

    `headers` is a map of extra request headers to send with the
    check (e.g. `{"Authorization": "Bearer ..."}` for a health
    endpoint behind auth, or `{"X-Api-Key": "..."}`) — validated
    by `normalize_headers`, so a non-dict or a non-string key or
    value raises ValueError. With no headers the request is sent
    exactly as before, so existing endpoints behave unchanged.

    `method` is the HTTP method to send (GET by default): a HEAD
    check asks for headers only, and a POST check suits a health
    endpoint that only answers to POST. The check sends no body,
    so only the methods in ALLOWED_HTTP_METHODS are accepted — a
    bad value raises ValueError. A HEAD check carries no body, so
    body assertions never apply to it (config load rejects the
    combination); with any other method the assertions run as
    before.

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

    For https:// URLs the peer certificate's expiry is also read
    (one extra short TLS handshake — urllib does not expose the
    socket it used) and returned as `cert_expires_at` on the
    result, so the monitor can warn before the cert lapses. A
    failure to read it leaves the expiry None and never changes
    the verdict: an expiring cert is a warning, not downtime.
    """
    if expect_status is not None and (
            isinstance(expect_status, bool)
            or not isinstance(expect_status, int)
            or not 100 <= expect_status <= 599):
        raise ValueError(
            f"expect_status must be an HTTP status code (100-599), "
            f"not {expect_status!r}")
    method = normalize_http_method(method)
    headers = normalize_headers(headers)
    started = time.monotonic()
    try:
        context = None
        if url.lower().startswith("https://") and (
                cafile is not None or cert is not None or key is not None):
            context = build_tls_context(cafile=cafile, cert=cert, key=key)
        request = urllib.request.Request(url, method=method,
                                         headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=context) as resp:
            status = resp.status
            body = (resp.read(MAX_BODY_BYTES + 1)
                    if expect_body is not None
                    or expect_body_regex is not None else resp.read(1))
        latency = (time.monotonic() - started) * 1000
        # The handshake succeeded, so the peer certificate is
        # readable: record when it expires with the check. A
        # failure to read it leaves the expiry unknown and never
        # changes the verdict — the expiry is advisory.
        expiry = _cert_expiry_for(url, context, timeout)
        if expect_status is not None:
            if status != expect_status:
                result = CheckResult(
                    name=name, ok=False, latency_ms=latency,
                    status_code=status,
                    error=f"expected HTTP {expect_status}, got {status}",
                    checked_at=time.time())
            elif expect_body is not None or expect_body_regex is not None:
                result = _check_body(name, body, latency, status,
                                     expect_body, expect_body_regex)
            else:
                result = CheckResult(name=name, ok=True,
                                     latency_ms=latency,
                                     status_code=status,
                                     checked_at=time.time())
        elif not 200 <= status < 400:
            result = CheckResult(name=name, ok=False, latency_ms=latency,
                                 status_code=status, error=f"HTTP {status}",
                                 checked_at=time.time())
        elif expect_body is not None or expect_body_regex is not None:
            result = _check_body(name, body, latency, status,
                                 expect_body, expect_body_regex)
        else:
            result = CheckResult(name=name, ok=True, latency_ms=latency,
                                 status_code=status, checked_at=time.time())
        if expiry is None:
            return result
        return dataclasses.replace(result, cert_expires_at=expiry)
    except urllib.error.HTTPError as exc:
        latency = (time.monotonic() - started) * 1000
        expiry = _cert_expiry_for(url, context, timeout)
        if expect_status is not None and exc.code == expect_status:
            # The expected status arrived as an error response (4xx /
            # 5xx): it still counts, with body assertions applied.
            body = (exc.read(MAX_BODY_BYTES + 1)
                    if expect_body is not None
                    or expect_body_regex is not None else b"")
            if expect_body is not None or expect_body_regex is not None:
                result = _check_body(name, body, latency, exc.code,
                                     expect_body, expect_body_regex)
            else:
                result = CheckResult(name=name, ok=True,
                                     latency_ms=latency,
                                     status_code=exc.code,
                                     checked_at=time.time())
        elif expect_status is not None:
            result = CheckResult(
                name=name, ok=False, latency_ms=latency,
                status_code=exc.code,
                error=f"expected HTTP {expect_status}, got {exc.code}",
                checked_at=time.time())
        else:
            result = CheckResult(name=name, ok=False, latency_ms=latency,
                                 status_code=exc.code,
                                 error=f"HTTP {exc.code}",
                                 checked_at=time.time())
        if expiry is None:
            return result
        return dataclasses.replace(result, cert_expires_at=expiry)
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
                          expect_status=endpoint.get("expect_status"),
                          method=endpoint.get("method"),
                          headers=endpoint.get("headers"))
    raise ValueError(f"unknown endpoint type: {kind!r}")


def check_endpoint_with_retry(endpoint, timeout=5.0, retry=True):
    """Check once; on failure, immediately retry once and keep the
    retry's result.

    A first-attempt failure that recovers on the retry is recorded
    as UP with `retries=1` — a single blip is not downtime, and it
    never reaches the flap detector as a failure. Only when the
    retry fails too is the check DOWN, with `retries=1` and the
    retry's error (the freshest failure). With `retry` False the
    check runs once and `retries` is 0. The retry is part of the
    same check round, not a second check: the caller records one
    entry either way, and the endpoint's next-due time is computed
    from the round, so a retry never resets the schedule.
    """
    result = check_endpoint(endpoint, timeout=timeout)
    if result.ok or not retry:
        return result
    retried = check_endpoint(endpoint, timeout=timeout)
    return dataclasses.replace(retried, retries=1)
