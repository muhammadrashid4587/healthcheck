"""Status dashboard over stdlib http.server.

GET / renders a plain HTML table: each endpoint, its current status,
its expected vs actual HTTP status, uptime %, most recent latency,
a sparkline of recent latencies, and last check time — plus a
"Recent incidents" section listing each endpoint's recent outages,
from the same History.recent_incidents() the API and the static
status page use. GET /api/status returns the same data as JSON, for
scripts and other tools that want to consume the live status without
scraping the HTML. GET /api/sla returns the monthly SLA report as
JSON — the same payload as `--sla-report --sla-format json`, for
the current month unless `?month=YYYY-MM` names another. GET
/api/uptime returns the rolling-window uptime summary as JSON:
per endpoint, uptime % and total downtime over the last 24 hours,
7 days, and 30 days, with the SLA target compared against the
30-day figure. GET /api/incidents returns the recent incidents
— the same grouping as the dashboard, `GET /api/status`, and the
static status page — plus each endpoint's duration histogram over
those same incidents (the 7 log-spaced buckets from
`History.incident_duration_histogram()`, with the same limit, so
the histogram can never disagree with the incidents listed) — with
`?endpoint=NAME` to filter to one endpoint and `?limit=N` to cap
the incidents per endpoint (5 by default). GET /metrics returns the same data in Prometheus
text format, so a Prometheus server can scrape it. A check skipped
because its dependency is not UP renders as SKIPPED, with the
dependency named in the Depends on column; an endpoint inside a
maintenance window renders as MAINTENANCE, with its windows listed
in the Maintenance column. Anything else 404s.
"""

from __future__ import annotations

import html
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .checker import (DEFAULT_CERT_EXPIRY_WARN_DAYS, cert_expiry_days,
                      cert_expiring_soon)
from .maintenance import summarize_windows
from .sla import parse_month, sla_report, sla_report_payload


def latency_sparkline(history, name, width=100, height=24):
    """Inline SVG sparkline of recent latencies, or "-" with too little data.

    Uses the last 20 recorded latencies (oldest first), scaled so the
    lowest maps near the bottom and the highest near the top. A flat
    history renders as a flat line; a single point is not a line.
    Skipped and maintenance checks never ran, so they are left out
    of the line.
    """
    points = [c["latency_ms"] for c in history.recent(name, 20)
              if not c.get("skipped", False)
              and not c.get("maintenance", False)]
    if len(points) < 2:
        return "-"
    lo, hi = min(points), max(points)
    span = (hi - lo) or 1.0
    coords = []
    for i, value in enumerate(points):
        x = i * width / (len(points) - 1)
        y = height - 2 - (value - lo) / span * (height - 4)
        coords.append(f"{x:.1f},{y:.1f}")
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}"'
        f' role="img" aria-label="latency history">'
        f'<polyline points="{" ".join(coords)}" fill="none"'
        f' stroke="#3366cc" stroke-width="1.5"/></svg>'
    )


def format_duration(seconds):
    """Human duration for an outage: "45s", "12m 34s", "2h 5m", "3d 4h".

    Seconds are rounded down; the two largest units are shown, so
    a long outage stays short. A negative or zero duration is "0s".
    """
    total = max(0, int(seconds))
    days, total = divmod(total, 86400)
    hours, total = divmod(total, 3600)
    minutes, secs = divmod(total, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def status_snapshot(history, endpoints, now=None,
                    status_page_sources=None, sla_alerts=None,
                    cert_alerts=None):
    """Machine-readable status for every endpoint: the same data the
    dashboard renders, as plain JSON-friendly values.

    `now` pins the windows (uptime 24h/7d/30d, downtime 24h/7d/30d,
    and latency percentiles); it defaults to the current time. Returns
    {"generated_at": <epoch seconds>, "endpoints": [...]} where
    each endpoint dict has "name", "status" (UP / DOWN / SKIPPED /
    MAINTENANCE / UNKNOWN — same rules as the dashboard, including
    unconfirmed failures below the flap threshold still showing UP),
    "depends_on" (name or None), "maintenance_windows" (list of
    {"start", "end", "days"} dicts, or []), "expect_status" (the
    exact HTTP status the endpoint must return, or None when it
    accepts any 2xx/3xx), "status_code" (the most recent actual
    HTTP status, or None when the endpoint never returned one),
    "uptime_pct",
    "uptime_24h_pct", "uptime_7d_pct", "uptime_30d_pct" (None when
    there is no data in that window — no data is not downtime),
    "downtime_24h_seconds", "downtime_7d_seconds",
    "downtime_30d_seconds" (total outage time in seconds inside
    each window, from the same incident grouping the incident
    lists use — an outage that started before the window only
    counts the part inside it, and an ongoing outage accrues to
    `now`), "latency_ms" (most recent
    recorded latency, or None), "max_latency_ms" (the latency budget
    in ms the endpoint must answer within, or None when it has none
    — a check slower than the budget is DOWN, like any other failed
    check), "latency_percentiles" ({"p50", "p95", "p99"} in ms over
    the last 24 hours, or None when there are no recorded latencies
    in that window), "last_error" (the error string of the most
    recent failed check that actually ran — skipped and maintenance
    rounds do not count — or None when the endpoint never failed;
    it stays after a later success, so it answers "why did it last
    fail"), "check_interval_s" (the endpoint's own check interval
    in seconds, or None when it uses the global `interval_seconds`),
    "retries" (retries spent on the most recent recorded check — 0
    when the first attempt decided it, 1 when a first-attempt
    failure recovered on the retry — or None when never checked)
    and "retries_total" (retries spent across the recorded history,
    so a flapping endpoint's blips stay visible), and "last_checked_at"
    (epoch seconds, or None when never checked). "sla_target" is the
    endpoint's configured SLA target, or None; "sla_breach" is True
    when the endpoint is below its target in the current calendar
    month, False when it meets it or has no target, and
    "sla_shortfall_pct" is how far short it fell (None when it did
    not breach) — the same computation as `--sla-report`, so the
    live status and the report can never disagree. "down_since" is
    the epoch second the current outage started (the first failed
    check in the current run of failures, unconfirmed ones
    included), or None when the endpoint is not currently down.
    "recent_incidents" is the endpoint's recent outages, newest
    first (at most 5) — each {"start", "end", "checks", "ongoing",
    "duration_s"}, grouped the same way as the static status
    page's incident list, so the API and the page can never
    disagree; an ongoing incident has "end" None and its duration
    runs to `now`. "duration_histogram" buckets those same
    incidents by duration into the 7 log-spaced buckets the
    `--incidents` text report prints, from
    `History.incident_duration_histogram()` with the same limit
    of 5, so the histogram can never disagree with the incidents
    listed above it — an endpoint with no incidents gets all
    zeroes, not an omitted field.
    "cert_expires_at" is the epoch second the endpoint's TLS
    certificate expires (the most recent check that could read
    it), or None when the endpoint is not HTTPS, was never
    checked, or the check could not read the peer certificate.
    "cert_expiry_days" is the whole days from `now` until that
    expiry (negative when expired, None when unknown), and
    "cert_expiry_warning" is True when the certificate expires
    within the endpoint's `cert_expiry_warn_days` (30 by default)
    — the expiry is advisory and never decides up or down.
    When `sla_alerts` is given (the shared breach-alert records from
    the checker loop), "sla_breach_alerted_at" is the epoch second
    the breach alert fired for this endpoint in the current month,
    or None when it has not fired. When `cert_alerts` is given
    (the shared cert-expiry-alert records from the checker loop),
    "cert_expiry_alerted_at" is the epoch second the cert-expiry
    alert fired for the endpoint's current certificate, or None
    when it has not fired — a record for a different (renewed)
    certificate does not count. When
    `status_page_sources` is given, the merged status page sources
    (the config file's `status_page_sources` plus any
    --status-page-source flags) are listed under that key, as
    {"label", "path"|"url"} dicts, so a consumer can see what the
    static status page would merge in.
    """
    current = time.localtime()
    month_str = f"{current.tm_year:04d}-{current.tm_mon:02d}"
    report = {row["name"]: row for row in sla_report(
        history, endpoints, current.tm_year, current.tm_mon)}
    out = []
    for ep in endpoints:
        name = ep["name"]
        cur = history.current(name)
        if cur is None:
            status, latency, last = "UNKNOWN", None, None
        elif cur.get("maintenance", False):
            status, latency = "MAINTENANCE", None
            last = cur["checked_at"]
        elif cur.get("skipped", False):
            status, latency = "SKIPPED", None
            last = cur["checked_at"]
        else:
            # A failure below the flap threshold has not confirmed
            # the endpoint down yet: it still shows UP.
            status = "UP" if (cur["ok"] or cur.get("unconfirmed", False)) \
                else "DOWN"
            latency = round(cur["latency_ms"], 1)
            last = cur["checked_at"]
        windows = [
            {"start": w["start"], "end": w["end"],
             "days": w.get("days")}
            for w in (ep.get("maintenance_windows") or [])
        ]
        row = report.get(name, {})
        breached = row.get("met") is False
        alert = (sla_alerts or {}).get(name) or {}
        alerted_at = None
        if alert.get("month") == month_str:
            alerted_at = alert.get("alerted_at")
        expires_at = history.cert_expiry(name)
        warn_days = ep.get("cert_expiry_warn_days")
        if warn_days is None:
            warn_days = DEFAULT_CERT_EXPIRY_WARN_DAYS
        ref = now if now is not None else time.time()
        cert_alert = (cert_alerts or {}).get(name) or {}
        cert_alerted_at = (
            cert_alert.get("alerted_at")
            if cert_alert.get("cert_expires_at") == expires_at
            else None)
        out.append({
            "name": name,
            "status": status,
            "depends_on": ep.get("depends_on"),
            "maintenance_windows": windows,
            "expect_status": ep.get("expect_status"),
            "status_code": cur.get("status_code") if cur is not None
                         else None,
            "cert_expires_at": expires_at,
            "cert_expiry_days": cert_expiry_days(expires_at, now=ref),
            "cert_expiry_warn_days": warn_days,
            "cert_expiry_warning": cert_expiring_soon(
                expires_at, warn_days, now=ref),
            "cert_expiry_alerted_at": cert_alerted_at,
            "uptime_pct": history.uptime_pct(name),
            "uptime_24h_pct": history.uptime_pct_window(
                name, 24 * 3600, now=ref),
            "uptime_7d_pct": history.uptime_pct_window(
                name, 7 * 24 * 3600, now=ref),
            "uptime_30d_pct": history.uptime_pct_window(
                name, 30 * 24 * 3600, now=ref),
            "downtime_24h_seconds": history.downtime_seconds_window(
                name, 24 * 3600, now=ref),
            "downtime_7d_seconds": history.downtime_seconds_window(
                name, 7 * 24 * 3600, now=ref),
            "downtime_30d_seconds": history.downtime_seconds_window(
                name, 30 * 24 * 3600, now=ref),
            "latency_ms": latency,
            "max_latency_ms": ep.get("max_latency_ms"),
            "latency_percentiles": history.latency_percentiles(name, now=now),
            "last_error": history.last_error(name),
            "check_interval_s": ep.get("check_interval_s"),
            "retries": (cur.get("retries", 0) if cur is not None
                        else None),
            "retries_total": history.retries_total(name),
            "last_checked_at": last,
            "sla_target": ep.get("sla_target"),
            "sla_breach": breached,
            "sla_shortfall_pct": (row.get("shortfall_pct")
                                  if breached else None),
            "sla_breach_alerted_at": alerted_at,
            "down_since": history.down_since(name),
            "recent_incidents": history.recent_incidents(name, now=now),
            "duration_histogram": history.incident_duration_histogram(
                name, limit=5, now=now),
        })
    snapshot = {"generated_at": time.time(), "endpoints": out}
    if status_page_sources is not None:
        snapshot["status_page_sources"] = [
            dict(src) for src in status_page_sources]
    return snapshot


def sla_snapshot(history, endpoints, year=None, month=None):
    """The monthly SLA report as a JSON-ready dict: the same
    payload `--sla-report --sla-format json` writes, built from
    the same `sla_report` rows, so the endpoint and the export
    can never disagree.

    With no month given, the current calendar month (local time)
    is used. Each endpoint row has `name`, `target` (the
    configured `sla_target`, or None), `uptime_pct` (None when
    the month has no counted checks), `checks`, `met`, and
    `shortfall_pct`, plus the month detail — `incidents`
    (confirmed outages that overlapped the month),
    `failed_checks`, `downtime_seconds` (outage time inside the
    month), and `latency` (p50/p95/p99 in ms, or None). The
    payload also carries `breaches`, the names of the endpoints
    that fell short of their target, worst shortfall first.
    """
    if year is None or month is None:
        now = time.localtime()
        year, month = now.tm_year, now.tm_mon
    report = sla_report(history, endpoints, year, month)
    return sla_report_payload(report, year, month)


# Rolling windows the uptime summary reports, in order: label and
# seconds. The 30-day window is the longest the bounded history
# can still cover for a monitor checking every minute or slower.
UPTIME_WINDOWS = (("24h", 24 * 3600), ("7d", 7 * 24 * 3600),
                  ("30d", 30 * 24 * 3600))


def uptime_snapshot(history, endpoints, now=None):
    """Rolling-window uptime summary per endpoint, as a JSON-ready
    dict: the same figures `GET /api/status` carries, gathered in
    one place.

    Returns {"generated_at": <epoch seconds>, "windows": ["24h",
    "7d", "30d"], "endpoints": [...]} where each endpoint row has
    "name", "sla_target" (the configured target, or None), and
    "windows" — one {"uptime_pct", "downtime_seconds", "checks"}
    per window from `History.window_summary`, so the uptime %,
    the total outage time, and the check count in a window can
    never disagree with each other. The SLA target is compared
    against the 30-day window: "sla_met_30d" is True / False, or
    None when there is no target or no 30-day data, and
    "sla_shortfall_30d_pct" is the target minus the 30-day uptime
    when the endpoint fell short, else None. `now` pins the
    windows; it defaults to the current time.
    """
    ref = time.time() if now is None else now
    out = []
    for ep in endpoints:
        name = ep["name"]
        target = ep.get("sla_target")
        windows = {
            label: history.window_summary(name, seconds, now=ref)
            for label, seconds in UPTIME_WINDOWS
        }
        pct_30d = windows["30d"]["uptime_pct"]
        if target is None or pct_30d is None:
            met, shortfall = None, None
        elif pct_30d >= target:
            met, shortfall = True, None
        else:
            met, shortfall = False, round(target - pct_30d, 1)
        out.append({
            "name": name,
            "sla_target": target,
            "windows": windows,
            "sla_met_30d": met,
            "sla_shortfall_30d_pct": shortfall,
        })
    return {"generated_at": ref,
            "windows": [label for label, _ in UPTIME_WINDOWS],
            "endpoints": out}


def render_uptime_report(snapshot):
    """Render an uptime snapshot as plain text.

    Takes the dict `uptime_snapshot` returns, so the text report
    can never disagree with `GET /api/uptime`: the same uptime %,
    downtime, and counted checks per window (24h / 7d / 30d), and
    the same SLA verdict against the 30-day figure. A window with
    no counted checks prints "no data" instead of a percentage —
    no data is not downtime — while its downtime is still shown,
    because an outage that started before the window and never
    recovered accrues the whole window even with no check inside
    it. Downtime is printed with `format_duration`, so "600.0s"
    reads as "10m 0s".
    """
    lines = ["Uptime report (generated "
             + time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(snapshot["generated_at"]))
             + ")"]
    for row in snapshot["endpoints"]:
        lines.append(f"  {row['name']}:")
        for label in snapshot["windows"]:
            window = row["windows"][label]
            if window["uptime_pct"] is None:
                uptime = "no data"
            else:
                uptime = f"{window['uptime_pct']:.1f}% uptime"
            lines.append(
                f"    {label}: {uptime}, "
                f"{format_duration(window['downtime_seconds'])} "
                f"downtime ({window['checks']} "
                f"check{'s' if window['checks'] != 1 else ''})")
        target = row["sla_target"]
        if target is None:
            lines.append("    SLA target: none")
        elif row["sla_met_30d"] is None:
            lines.append(f"    SLA target {target:.1f}%: no 30d data")
        elif row["sla_met_30d"]:
            lines.append(
                f"    SLA target {target:.1f}%: met "
                f"(30d {row['windows']['30d']['uptime_pct']:.1f}%)")
        else:
            lines.append(
                f"    SLA target {target:.1f}%: short by "
                f"{row['sla_shortfall_30d_pct']:.1f}pp "
                f"(30d {row['windows']['30d']['uptime_pct']:.1f}%)")
    return "\n".join(lines) + "\n"


def incidents_snapshot(history, endpoints, limit=5, name=None, now=None):
    """Recent incidents per endpoint as a JSON-ready dict, built
    from the same `History.recent_incidents()` the dashboard, the
    status API, and the static status page use, so every surface
    reports the same incidents.

    Returns {"generated_at": <epoch seconds>, "endpoints": [...]}
    where each endpoint row has "name", "incidents" — the
    endpoint's recent outages, newest first (at most `limit` per
    endpoint, 5 by default), each {"start", "end", "checks",
    "ongoing", "duration_s"}; an ongoing incident has "end"
    None and its duration runs to `now` (the current time when
    not given) — and "duration_histogram", those same incidents
    bucketed by duration into the 7 log-spaced buckets the
    `--incidents` text report prints, from
    `History.incident_duration_histogram()` with the same
    `limit`, so the histogram can never disagree with the
    incidents listed above it. All 7 buckets are always present
    even at 0, so an endpoint with no incidents gets all zeroes
    instead of an omitted field. With `name` given only that
    endpoint is listed; an unknown name raises ValueError.
    Endpoints with no incidents are listed with an empty list,
    so a consumer can always find every configured endpoint.
    """
    known = {ep["name"] for ep in endpoints}
    if name is not None and name not in known:
        raise ValueError(f"unknown endpoint: {name}")
    selected = ([ep for ep in endpoints if ep["name"] == name]
                if name is not None else endpoints)
    ref = time.time() if now is None else now
    out = []
    for ep in selected:
        out.append({
            "name": ep["name"],
            "incidents": history.recent_incidents(
                ep["name"], limit=limit, now=ref),
            "duration_histogram": history.incident_duration_histogram(
                ep["name"], limit=limit, now=ref),
        })
    return {"generated_at": ref, "endpoints": out}


def _prom_label(value):
    """Escape a label value for Prometheus exposition format."""
    return (str(value).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n"))


def render_metrics(history, endpoints, now=None):
    """Prometheus text format for every endpoint.

    `now` pins the 24-hour / 7-day windows; it defaults to the
    current time. Five metric families:

    - ``healthcheck_up`` (gauge): 1 when the endpoint is currently
      UP, 0 when DOWN. An endpoint that was never checked, is
      currently SKIPPED, or is in maintenance has no current
      reading, so it is omitted — no data is not a 0. A failure
      below the flap threshold still shows UP, same as the
      dashboard.
    - ``healthcheck_down_seconds`` (gauge): seconds the endpoint
      has been down, 0 when it is up. An endpoint that was never
      checked, is currently SKIPPED, or is in maintenance has no
      current reading, so it is omitted — no data is not a 0.
    - ``healthcheck_uptime_ratio`` (gauge): share of checks that
      were up, all-time, plus ``_24h``, ``_7d``, and ``_30d``
      variants over the last 24 hours / 7 days / 30 days. Omitted
      when there is no counted data in that window — no data is
      not downtime. The ``_30d`` variant reads the same
      ``uptime_pct_window`` figure `GET /api/uptime` reports as
      ``uptime_30d_pct``, so the gauge and the API can never
      disagree.
    - ``healthcheck_latency_ms`` (summary): nearest-rank p50/p95/p99
      in ms over the last 24 hours, plus ``_sum`` and ``_count``.
      Skipped and maintenance checks never ran and are excluded;
      failed checks count. Omitted when there are no recorded
      latencies in the window.
    - ``healthcheck_checks_total`` (counter): total checks that ran,
      split by ``result="success"`` / ``"failure"``. Skipped and
      maintenance checks never ran and are not counted; an
      unconfirmed failure counts as a failure, since the check did
      fail. Both series are always present, even at 0.
    - ``healthcheck_incidents_total`` (counter): total recorded
      incidents (runs of consecutive confirmed-down checks) for
      the endpoint, from the same ``History.incidents_total()``
      grouping the incident lists use, so the counter and the
      lists can never disagree. Always present, even at 0; the
      bounded history can truncate a very long past, so it counts
      the incidents still recorded.
    - ``healthcheck_cert_expiry_days`` (gauge): whole days until
      the endpoint's TLS certificate expires (negative when
      expired). Omitted for endpoints with no recorded expiry —
      plain http / tcp, never checked, or a check that could not
      read the peer certificate — no data is not an expiry.
    """
    lines = []
    lines.append("# HELP healthcheck_up Whether the endpoint is "
                 "currently up (1) or down (0).")
    lines.append("# TYPE healthcheck_up gauge")
    for ep in endpoints:
        cur = history.current(ep["name"])
        if cur is None or cur.get("skipped", False) \
                or cur.get("maintenance", False):
            continue
        up = 1 if (cur["ok"] or cur.get("unconfirmed", False)) else 0
        lines.append(f'healthcheck_up{{name="{_prom_label(ep["name"])}"}} {up}')
    lines.append("# HELP healthcheck_down_seconds Seconds the endpoint "
                 "has been down (0 when up).")
    lines.append("# TYPE healthcheck_down_seconds gauge")
    ref = time.time() if now is None else now
    for ep in endpoints:
        cur = history.current(ep["name"])
        if cur is None or cur.get("skipped", False) \
                or cur.get("maintenance", False):
            continue
        since = history.down_since(ep["name"])
        down = 0 if since is None else max(0, int(ref - since))
        lines.append(
            f'healthcheck_down_seconds{{name="{_prom_label(ep["name"])}"}} '
            f"{down}")
    lines.append("# HELP healthcheck_cert_expiry_days Days until the "
                 "endpoint's TLS certificate expires (negative when "
                 "expired).")
    lines.append("# TYPE healthcheck_cert_expiry_days gauge")
    for ep in endpoints:
        expires_at = history.cert_expiry(ep["name"])
        days = cert_expiry_days(expires_at, now=now)
        if days is None:
            continue
        lines.append(
            f'healthcheck_cert_expiry_days'
            f'{{name="{_prom_label(ep["name"])}"}} {days}')
    lines.append("# HELP healthcheck_uptime_ratio Share of checks that "
                 "were up (all-time).")
    lines.append("# TYPE healthcheck_uptime_ratio gauge")
    for ep in endpoints:
        pct = history.uptime_pct(ep["name"])
        if pct is not None:
            lines.append(
                f'healthcheck_uptime_ratio{{name="{_prom_label(ep["name"])}"}} '
                f"{pct / 100.0}")
    for window, suffix in ((24 * 3600, "24h"), (7 * 24 * 3600, "7d"),
                           (30 * 24 * 3600, "30d")):
        lines.append(f"# HELP healthcheck_uptime_ratio_{suffix} Share of "
                     f"checks that were up over the last {suffix}.")
        lines.append(f"# TYPE healthcheck_uptime_ratio_{suffix} gauge")
        for ep in endpoints:
            pct = history.uptime_pct_window(ep["name"], window, now=now)
            if pct is not None:
                lines.append(
                    f'healthcheck_uptime_ratio_{suffix}'
                    f'{{name="{_prom_label(ep["name"])}"}} {pct / 100.0}')
    lines.append("# HELP healthcheck_latency_ms Check latency in ms over "
                 "the last 24 hours.")
    lines.append("# TYPE healthcheck_latency_ms summary")
    for ep in endpoints:
        summary = history.latency_summary(ep["name"], now=now)
        if summary is None:
            continue
        name = _prom_label(ep["name"])
        lines.append(
            f'healthcheck_latency_ms{{quantile="0.5",name="{name}"}} '
            f"{summary['p50']}")
        lines.append(
            f'healthcheck_latency_ms{{quantile="0.95",name="{name}"}} '
            f"{summary['p95']}")
        lines.append(
            f'healthcheck_latency_ms{{quantile="0.99",name="{name}"}} '
            f"{summary['p99']}")
        lines.append(f'healthcheck_latency_ms_sum{{name="{name}"}} '
                     f"{summary['sum']}")
        lines.append(f'healthcheck_latency_ms_count{{name="{name}"}} '
                     f"{summary['count']}")
    lines.append("# HELP healthcheck_checks_total Total checks that ran, "
                 "by result.")
    lines.append("# TYPE healthcheck_checks_total counter")
    for ep in endpoints:
        counts = history.check_counts(ep["name"])
        name = _prom_label(ep["name"])
        lines.append(f'healthcheck_checks_total{{name="{name}",'
                     f'result="success"}} {counts["success"]}')
        lines.append(f'healthcheck_checks_total{{name="{name}",'
                     f'result="failure"}} {counts["failure"]}')
    lines.append("# HELP healthcheck_incidents_total Total recorded "
                 "incidents (runs of consecutive confirmed-down checks).")
    lines.append("# TYPE healthcheck_incidents_total counter")
    for ep in endpoints:
        name = _prom_label(ep["name"])
        lines.append(f'healthcheck_incidents_total{{name="{name}"}} '
                     f"{history.incidents_total(ep['name'])}")
    return "\n".join(lines) + "\n"


def render_recent_incidents(history, endpoints, now=None):
    """Recent incidents section for the live dashboard, or "".

    One subsection per endpoint with recorded outages, newest first
    (at most 5 per endpoint), from the same History.recent_incidents()
    the JSON API and the static status page use, so the three surfaces
    can never disagree. An ongoing incident shows "ongoing" instead
    of an end time, with its duration running to `now`. Each
    subsection also carries the endpoint's duration histogram — the
    same 7 log-spaced buckets the --incidents text report prints,
    from History.incident_duration_histogram() with the same limit
    of 5, so the histogram can never disagree with the incidents
    listed above it. Endpoints with no incidents are omitted; with
    none at all the section is empty, so a quiet monitor's dashboard
    is unchanged.
    """
    sections = []
    for ep in endpoints:
        name = ep["name"]
        incidents = history.recent_incidents(name, now=now)
        if not incidents:
            continue
        items = []
        for incident in incidents:
            start = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(incident["start"]))
            if incident["ongoing"]:
                end = "ongoing"
            else:
                end = time.strftime("%Y-%m-%d %H:%M:%S",
                                    time.localtime(incident["end"]))
            items.append(
                f"<li>{start} – {end} "
                f"({format_duration(incident['duration_s'])}, "
                f"{incident['checks']} failed check"
                f"{'s' if incident['checks'] != 1 else ''})</li>")
        histogram = history.incident_duration_histogram(
            name, limit=5, now=now)
        buckets = ", ".join(
            f"{html.escape(bucket['label'])}: {bucket['count']}"
            for bucket in histogram)
        sections.append(
            f"<h3>{html.escape(name)}</h3><ul>{''.join(items)}</ul>"
            f"<p>Duration histogram: {buckets}</p>")
    if not sections:
        return ""
    return "<h2>Recent incidents</h2>\n" + "\n".join(sections)


def render_dashboard(history, endpoints, now=None,
                     status_page_sources=None, sla_alerts=None,
                     cert_alerts=None):
    """Render the dashboard HTML.

    When `status_page_sources` is given and non-empty, a "Status
    page sources" section lists the merged sources (label plus the
    history path or remote URL), so the dashboard shows what the
    static status page would merge in. With no sources the section
    is omitted and the page is unchanged. The Down since column
    shows how long a DOWN endpoint's outage has lasted ("12m 34s",
    "-" otherwise), counted from the first failed check in the
    current run of failures. The Cert expires column shows whole
    days until the endpoint's TLS certificate expires ("89d",
    "12d (soon)" inside its warning window, "expired" once it
    has lapsed, "-" when no certificate was ever seen — plain
    http / tcp, never checked, or a check that could not read the
    peer certificate). When `cert_alerts` carries a record for the
    endpoint's current certificate, the time its expiry alert
    fired is appended ("5d (soon) alerted 06:12"). The SLA breach column
    shows "BREACH -X.Xpp" for an endpoint below its `sla_target`
    in the current calendar month (with the time its breach alert
    fired, when `sla_alerts` carries a record for it), and "-"
    otherwise — no target, no data, or meeting the target. Below
    the table, a "Recent incidents" section lists each endpoint's
    recent outages (newest first, at most 5, from the same
    History.recent_incidents() the API and the static status page
    use) — an ongoing incident shows "ongoing" instead of an end
    time, with its duration running to now — plus each endpoint's
    duration histogram over those same incidents (the same 7
    log-spaced buckets the --incidents text report prints, from
    History.incident_duration_histogram() with the same limit of
    5). With no incidents recorded anywhere the section is omitted.
    The Uptime 30d column comes from the same `uptime_snapshot`
    the API and the text report use, so the dashboard can never
    disagree with them about a window.
    """
    current = time.localtime()
    report = {row["name"]: row for row in sla_report(
        history, endpoints, current.tm_year, current.tm_mon)}
    uptime_snap = {row["name"]: row for row in uptime_snapshot(
        history, endpoints, now=now)["endpoints"]}
    rows = []
    for ep in endpoints:
        name = ep["name"]
        cur = history.current(name)
        uptime = history.uptime_pct(name)
        up_24h = history.uptime_pct_window(name, 24 * 3600, now=now)
        up_7d = history.uptime_pct_window(name, 7 * 24 * 3600, now=now)
        up_30d = uptime_snap[name]["windows"]["30d"]["uptime_pct"]
        depends_on = ep.get("depends_on") or "-"
        windows = summarize_windows(ep.get("maintenance_windows"))
        if cur is None:
            status, latency, last = "UNKNOWN", "-", "-"
        elif cur.get("maintenance", False):
            status = "MAINTENANCE"
            latency = "-"
            last = time.strftime("%H:%M:%S", time.localtime(cur["checked_at"]))
        elif cur.get("skipped", False):
            status = "SKIPPED"
            latency = "-"
            last = time.strftime("%H:%M:%S", time.localtime(cur["checked_at"]))
        else:
            # A failure below the flap threshold has not confirmed
            # the endpoint down yet: it still shows UP.
            status = "UP" if (cur["ok"] or cur.get("unconfirmed", False)) \
                else "DOWN"
            latency = f"{cur['latency_ms']:.1f} ms"
            last = time.strftime("%H:%M:%S", time.localtime(cur["checked_at"]))
        up = f"{uptime:.1f}%" if uptime is not None else "-"
        day = f"{up_24h:.1f}%" if up_24h is not None else "-"
        week = f"{up_7d:.1f}%" if up_7d is not None else "-"
        month = f"{up_30d:.1f}%" if up_30d is not None else "-"
        pct = history.latency_percentiles(name, now=now)
        pct_text = (f"{pct['p50']:.1f} / {pct['p95']:.1f} / {pct['p99']:.1f} ms"
                    if pct is not None else "-")
        budget = ep.get("max_latency_ms")
        budget_text = f"{budget:g} ms" if budget is not None else "-"
        interval = ep.get("check_interval_s")
        interval_text = (f"{interval:g} s" if interval is not None
                         else "-")
        retries = cur.get("retries", 0) if cur is not None else None
        retries_text = str(retries) if retries is not None else "-"
        # A breaching check still records its latency, so a slow
        # endpoint shows both numbers side by side: how slow it was
        # and what it was allowed to be.
        cls = ("up" if status == "UP" else "down" if status == "DOWN"
               else "skipped" if status == "SKIPPED"
               else "maintenance" if status == "MAINTENANCE" else "")
        # Expected vs actual HTTP status: "expected 204, got 200"
        # when the endpoint asserts on a code, the bare actual code
        # when it does not, "-" when no status was ever returned
        # (TCP endpoints, never-checked, skipped, maintenance).
        expected = ep.get("expect_status")
        actual = cur.get("status_code") if cur is not None else None
        if expected is None and actual is None:
            http_status = "-"
        elif expected is None:
            http_status = str(actual)
        else:
            http_status = (f"expected {expected}, got "
                           f"{actual if actual is not None else '-'}")
        # SLA breach this month: the shortfall, and when the breach
        # alert fired — the same numbers as the SLA report, so the
        # dashboard and the report can never disagree.
        sla_row = report.get(name, {})
        if sla_row.get("met") is False:
            sla_text = (f"BREACH -{sla_row['shortfall_pct']:.1f}pp")
            alert = (sla_alerts or {}).get(name) or {}
            if alert.get("month") == (f"{current.tm_year:04d}-"
                                      f"{current.tm_mon:02d}"):
                alerted = alert.get("alerted_at")
                if alerted is not None:
                    sla_text += (" alerted " + time.strftime(
                        "%H:%M", time.localtime(alerted)))
        else:
            sla_text = "-"
        # How long the current outage has lasted: the first failed
        # check in the current run (unconfirmed failures included,
        # so the clock starts when it first failed, not when the
        # flap threshold confirmed it). "-" when not down.
        ref = time.time() if now is None else now
        since = history.down_since(name)
        down_text = (format_duration(ref - since)
                     if status == "DOWN" and since is not None else "-")
        # When the endpoint's TLS certificate expires: whole days
        # left, "expired" once it has lapsed, "(soon)" inside the
        # endpoint's warning window (30 days by default), "-" when
        # no certificate was ever seen (plain http / tcp, never
        # checked, or a check that could not read the peer cert).
        expires_at = history.cert_expiry(name)
        warn_days = ep.get("cert_expiry_warn_days")
        if warn_days is None:
            warn_days = DEFAULT_CERT_EXPIRY_WARN_DAYS
        if expires_at is None:
            cert_text = "-"
        else:
            days = cert_expiry_days(expires_at, now=ref)
            if days < 0:
                cert_text = "expired"
            elif cert_expiring_soon(expires_at, warn_days, now=ref):
                cert_text = f"{days}d (soon)"
            else:
                cert_text = f"{days}d"
            # When the expiry alert fired for this certificate,
            # say when — like the SLA breach column says when its
            # alert fired. A record for a different (renewed)
            # certificate does not count.
            cert_alert = (cert_alerts or {}).get(name) or {}
            if cert_alert.get("cert_expires_at") == expires_at:
                alerted = cert_alert.get("alerted_at")
                if alerted is not None:
                    cert_text += (" alerted " + time.strftime(
                        "%H:%M", time.localtime(alerted)))
        rows.append(
            f"<tr><td>{html.escape(name)}</td>"
            f"<td class=\"{cls}\">{status}</td>"
            f"<td>{html.escape(down_text)}</td>"
            f"<td>{html.escape(http_status)}</td>"
            f"<td>{html.escape(cert_text)}</td>"
            f"<td>{html.escape(depends_on)}</td>"
            f"<td>{html.escape(windows)}</td>"
            f"<td>{up}</td><td>{day}</td><td>{week}</td><td>{month}</td><td>{latency}</td>"
            f"<td>{budget_text}</td>"
            f"<td>{interval_text}</td>"
            f"<td>{retries_text}</td>"
            f"<td>{pct_text}</td>"
            f"<td>{latency_sparkline(history, name)}</td>"
            f"<td>{html.escape(history.last_error(name) or '-')}</td>"
            f"<td>{html.escape(sla_text)}</td>"
            f"<td>{last}</td></tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan=\"20\">No endpoints configured.</td></tr>"
    if status_page_sources:
        items = "".join(
            f"<li>{html.escape(src['label'])} — "
            f"{html.escape(src.get('path') or src.get('url') or '')}</li>"
            for src in status_page_sources)
        sources_html = (f"<h2>Status page sources</h2>\n<ul>{items}</ul>\n")
    else:
        sources_html = ""
    incidents_html = render_recent_incidents(history, endpoints, now=now)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>healthcheck</title>
<style>body{{font-family:sans-serif;max-width:720px;margin:2em auto}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}.up{{color:green;font-weight:bold}}.down{{color:red;font-weight:bold}}.skipped{{color:#b8860b;font-weight:bold}}.maintenance{{color:#6a5acd;font-weight:bold}}</style>
</head><body><h1>healthcheck</h1>
<table><tr><th>Endpoint</th><th>Status</th><th>Down since</th><th>HTTP status</th><th>Cert expires</th><th>Depends on</th><th>Maintenance</th><th>Uptime</th><th>Uptime 24h</th><th>Uptime 7d</th><th>Uptime 30d</th><th>Latency</th><th>Latency budget</th><th>Check every</th><th>Retries</th><th>Latency p50 / p95 / p99</th><th>Latency history</th><th>Last error</th><th>SLA breach</th><th>Last check</th></tr>
{body}</table>
{incidents_html}{sources_html}<p>Rendered {time.strftime('%Y-%m-%d %H:%M:%S')} — reload to update.</p></body></html>"""


def make_handler(history, endpoints, status_page_sources=None,
                 sla_alerts=None, cert_alerts=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/status":
                payload = json.dumps(
                    status_snapshot(history, endpoints,
                                    status_page_sources=status_page_sources,
                                    sla_alerts=sla_alerts,
                                    cert_alerts=cert_alerts)
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/api/sla":
                # ?month=YYYY-MM names the month; without it the
                # current month is served. A bad month is a 400,
                # not a 404: the path is right, the input is not.
                query = parse_qs(parsed.query)
                month_param = query.get("month", [None])[0]
                if month_param is None:
                    now = time.localtime()
                    year, month = now.tm_year, now.tm_mon
                else:
                    try:
                        year, month = parse_month(month_param)
                    except ValueError as exc:
                        error = json.dumps(
                            {"error": str(exc)}).encode("utf-8")
                        self.send_response(400)
                        self.send_header("Content-Type",
                                         "application/json")
                        self.send_header("Content-Length",
                                         str(len(error)))
                        self.end_headers()
                        self.wfile.write(error)
                        return
                payload = json.dumps(
                    sla_snapshot(history, endpoints, year, month)
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path == "/api/uptime":
                payload = json.dumps(
                    uptime_snapshot(history, endpoints)
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path == "/api/incidents":
                # ?endpoint=NAME filters to one endpoint and
                # ?limit=N caps the incidents per endpoint (5 by
                # default). A bad endpoint or limit is a 400, not
                # a 404: the path is right, the input is not.
                query = parse_qs(parsed.query)
                name = query.get("endpoint", [None])[0]
                limit_param = query.get("limit", [None])[0]
                if limit_param is None:
                    limit = 5
                else:
                    try:
                        limit = int(limit_param)
                        if limit < 1:
                            raise ValueError(
                                "limit must be at least 1")
                    except ValueError:
                        error = json.dumps(
                            {"error": f"invalid limit {limit_param!r}: "
                             "must be a positive integer"}).encode("utf-8")
                        self.send_response(400)
                        self.send_header("Content-Type",
                                         "application/json")
                        self.send_header("Content-Length",
                                         str(len(error)))
                        self.end_headers()
                        self.wfile.write(error)
                        return
                try:
                    snapshot = incidents_snapshot(
                        history, endpoints, limit=limit, name=name)
                except ValueError as exc:
                    error = json.dumps(
                        {"error": str(exc)}).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(error)))
                    self.end_headers()
                    self.wfile.write(error)
                    return
                payload = json.dumps(snapshot).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/metrics":
                payload = render_metrics(history, endpoints).encode("utf-8")
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path != "/":
                self.send_response(404)
                self.end_headers()
                return
            page = render_dashboard(
                history, endpoints,
                status_page_sources=status_page_sources,
                sla_alerts=sla_alerts,
                cert_alerts=cert_alerts).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass  # dashboard access is not logged; check results are

    return Handler


def serve(history, endpoints, host="127.0.0.1", port=8080,
          status_page_sources=None, sla_alerts=None, cert_alerts=None):
    """Serve the dashboard forever (Ctrl-C to stop).

    `status_page_sources` is the merged list of status page sources
    (config file plus CLI flags) the dashboard and GET /api/status
    report; pass the shared live list from `run_forever` so a
    SIGHUP reload updates what is shown. `sla_alerts` is the shared
    dict of breach-alert records from the checker loop, so the
    dashboard and GET /api/status show when each breach was alerted.
    `cert_alerts` is the shared dict of cert-expiry-alert records
    from the checker loop, so the dashboard and GET /api/status
    show when each expiry alert fired.
    """
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(history, endpoints,
                     status_page_sources=status_page_sources,
                     sla_alerts=sla_alerts,
                     cert_alerts=cert_alerts))
    server.serve_forever()
