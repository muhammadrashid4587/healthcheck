"""SLA reporting: monthly uptime per endpoint vs a configured target.

An endpoint can declare `sla_target` in the config (a percentage,
e.g. 99.9). `sla_report` computes, for one calendar month, each
endpoint's uptime from the persisted check history and whether it
met its target, plus the breach list — endpoints that fell short,
with the shortfall in percentage points. The same exclusion rules
as everywhere else apply: skipped checks, unconfirmed failures
below the flap threshold, and maintenance rounds are not checks
that ran, so they count neither for nor against the month's
uptime. A month with no counted checks for an endpoint reports
`uptime_pct` None — no data is not a breach.

The month is a calendar month in the monitor's local time. The
report is plain text, printed by `--sla-report`, so it can be
pasted into a status update or piped to a file; `--sla-format
csv` / `json` exports the same rows (plus per-endpoint incident
counts, downtime, and latency percentiles over the month) as
CSV or JSON, for spreadsheets and scripts.
"""

from __future__ import annotations

import csv
import io
import json
import math
import time


def month_bounds(year, month):
    """Epoch (start, end) of a calendar month in local time.

    `start` is the first second of the month, `end` the first
    second of the next month (exclusive), so a check counts for
    exactly one month. Raises ValueError on an out-of-range month.
    """
    if not 1 <= month <= 12:
        raise ValueError(f"month must be 1..12, not {month}")
    start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
    if month == 12:
        end = time.mktime((year + 1, 1, 1, 0, 0, 0, 0, 0, -1))
    else:
        end = time.mktime((year, month + 1, 1, 0, 0, 0, 0, 0, -1))
    return start, end


def _counted(entry):
    """A check that really ran and counts toward uptime."""
    return (not entry.get("skipped", False)
            and not entry.get("unconfirmed", False)
            and not entry.get("maintenance", False))


def monthly_uptime(history, name, year, month):
    """Uptime % for one endpoint over one calendar month.

    Returns (pct, counted) where `pct` is the share of counted
    checks that were up (rounded to one decimal), or None when the
    month holds no counted checks, and `counted` is how many
    checks the figure is based on.
    """
    start, end = month_bounds(year, month)
    checks = history.recent(name, history.maxlen)
    window = [c for c in checks
              if start <= c["checked_at"] < end and _counted(c)]
    if not window:
        return None, 0
    up = sum(1 for c in window if c["ok"])
    return round(100.0 * up / len(window), 1), len(window)


def monthly_detail(history, name, year, month):
    """Incident, downtime, and latency detail for one endpoint
    over one calendar month.

    Returns {"incidents", "failed_checks", "downtime_seconds",
    "latency"} where `incidents` is how many confirmed outages
    overlapped the month, `failed_checks` is how many counted
    checks failed in it, `downtime_seconds` is the outage time
    inside the month (first down check to the first passing check
    after it, clipped to the month — an incident still open at
    the month's end accrues downtime to the month end), and
    `latency` is nearest-rank p50/p95/p99 in ms over the checks
    that ran in the month, or None when none ran. Incident
    grouping follows the status page: consecutive confirmed-down
    checks are one incident, and skips, unconfirmed failures,
    and maintenance rounds neither start nor end one.
    """
    start, end = month_bounds(year, month)
    entries = history.recent(name, history.maxlen)
    window = [c for c in entries
              if start <= c["checked_at"] < end and _counted(c)]
    failed = sum(1 for c in window if not c["ok"])

    latencies = sorted(
        c["latency_ms"] for c in entries
        if start <= c["checked_at"] < end
        and not c.get("skipped", False)
        and not c.get("maintenance", False))
    latency = None
    if latencies:
        n = len(latencies)

        def percentile(p):
            rank = max(1, math.ceil(p / 100.0 * n))
            return round(latencies[min(rank, n) - 1], 1)

        latency = {"p50": percentile(50), "p95": percentile(95),
                   "p99": percentile(99)}

    incidents = 0
    downtime = 0.0
    current = None  # start epoch of the open incident, or None
    for entry in entries:
        down = (not entry["ok"] and _counted(entry))
        up = (entry["ok"] and _counted(entry))
        if down:
            if current is None:
                current = entry["checked_at"]
        elif up and current is not None:
            overlap_start = max(current, start)
            overlap_end = min(entry["checked_at"], end)
            if overlap_end > overlap_start:
                incidents += 1
                downtime += overlap_end - overlap_start
            current = None
    if current is not None:
        # Still down at the end of the recorded history: the
        # outage accrues downtime to the month end.
        overlap_start = max(current, start)
        if end > overlap_start:
            incidents += 1
            downtime += end - overlap_start
    return {"incidents": incidents, "failed_checks": failed,
            "downtime_seconds": round(downtime, 1), "latency": latency}


def validate_sla_targets(endpoints):
    """Reject bad `sla_target` entries at config load.

    A target must be a number strictly above 0 and at most 100 —
    a target of 0 is meaningless and above 100 is impossible.
    Raises ValueError with a clear message; the monitor refuses
    to start rather than report against a nonsense target.
    """
    for ep in endpoints:
        target = ep.get("sla_target")
        if target is None:
            continue
        if isinstance(target, bool) or not isinstance(target, (int, float)):
            raise ValueError(
                f"endpoint {ep['name']!r}: sla_target must be a number, "
                f"not {target!r}")
        if not 0 < target <= 100:
            raise ValueError(
                f"endpoint {ep['name']!r}: sla_target must be > 0 and "
                f"<= 100, not {target!r}")


def sla_report(history, endpoints, year, month):
    """One SLA row per endpoint for a calendar month.

    Each row is a dict: `name`, `target` (the configured
    `sla_target`, or None when the endpoint has none),
    `uptime_pct` (None when the month has no counted checks),
    `checks` (how many counted checks the figure is based on),
    `met` (True / False, or None when there is no target or no
    data), `shortfall_pct` (target minus uptime when the
    endpoint breached, else None), plus the month detail from
    `monthly_detail`: `incidents` (confirmed outages that
    overlapped the month), `failed_checks`, `downtime_seconds`
    (outage time inside the month), and `latency` (p50/p95/p99
    in ms over the checks that ran in the month, or None).
    Rows follow config order.
    """
    rows = []
    for ep in endpoints:
        name = ep["name"]
        target = ep.get("sla_target")
        pct, counted = monthly_uptime(history, name, year, month)
        if target is None or pct is None:
            met, shortfall = None, None
        elif pct >= target:
            met, shortfall = True, None
        else:
            met, shortfall = False, round(target - pct, 1)
        detail = monthly_detail(history, name, year, month)
        rows.append({
            "name": name,
            "target": target,
            "uptime_pct": pct,
            "checks": counted,
            "met": met,
            "shortfall_pct": shortfall,
            "incidents": detail["incidents"],
            "failed_checks": detail["failed_checks"],
            "downtime_seconds": detail["downtime_seconds"],
            "latency": detail["latency"],
        })
    return rows


def breaches(report):
    """The breached rows of an SLA report, worst shortfall first."""
    return sorted((r for r in report if r["met"] is False),
                  key=lambda r: r["shortfall_pct"], reverse=True)


def parse_month(value):
    """Parse "YYYY-MM" into (year, month); raises ValueError on bad input."""
    try:
        year_s, month_s = value.split("-")
        year, month = int(year_s), int(month_s)
    except (ValueError, AttributeError):
        raise ValueError(
            f"month must look like YYYY-MM, not {value!r}")
    month_bounds(year, month)  # validates the range
    return year, month


def render_sla_report(report, year, month):
    """Render an SLA report as plain text, one line per endpoint
    plus a breach summary."""
    lines = [f"SLA report {year:04d}-{month:02d}"]
    for row in report:
        if row["uptime_pct"] is None:
            uptime = "no data"
        else:
            uptime = f"{row['uptime_pct']:.1f}% ({row['checks']} checks)"
        if row["target"] is None:
            verdict = "no target"
        elif row["met"] is None:
            verdict = "no data"
        elif row["met"]:
            verdict = f"MET target {row['target']:.1f}%"
        else:
            verdict = (f"BREACH target {row['target']:.1f}% "
                       f"(short {row['shortfall_pct']:.1f}pp)")
        lines.append(f"  {row['name']}: {uptime} — {verdict}")
    breached = breaches(report)
    if breached:
        lines.append(f"{len(breached)} breach"
                     f"{'es' if len(breached) != 1 else ''}: "
                     + ", ".join(r["name"] for r in breached))
    else:
        lines.append("No breaches.")
    return "\n".join(lines) + "\n"


def render_sla_report_csv(report, year, month):
    """Render an SLA report as CSV, one row per endpoint.

    Columns: month, endpoint, uptime_pct, checks, failed_checks,
    incidents, downtime_seconds, latency_p50_ms, latency_p95_ms,
    latency_p99_ms, sla_target, met, shortfall_pct. Missing
    values (no data, no target) are empty fields.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["month", "endpoint", "uptime_pct", "checks",
                     "failed_checks", "incidents", "downtime_seconds",
                     "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
                     "sla_target", "met", "shortfall_pct"])
    month = f"{year:04d}-{month:02d}"
    for row in report:
        latency = row.get("latency") or {}
        writer.writerow([
            month, row["name"],
            "" if row["uptime_pct"] is None else row["uptime_pct"],
            row["checks"], row.get("failed_checks", ""),
            row.get("incidents", ""), row.get("downtime_seconds", ""),
            latency.get("p50", ""), latency.get("p95", ""),
            latency.get("p99", ""),
            "" if row["target"] is None else row["target"],
            "" if row["met"] is None else str(row["met"]).lower(),
            "" if row["shortfall_pct"] is None else row["shortfall_pct"],
        ])
    return out.getvalue()


def sla_report_payload(report, year, month):
    """The JSON payload for an SLA report: the month, one object
    per endpoint (the full `sla_report` row), and the breach list.

    Both the `--sla-format json` export and the dashboard's
    GET /api/sla build on this, so the two can never disagree.
    """
    return {
        "month": f"{year:04d}-{month:02d}",
        "endpoints": report,
        "breaches": [r["name"] for r in breaches(report)],
    }


def render_sla_report_json(report, year, month):
    """Render an SLA report as JSON: the month, one object per
    endpoint (the full `sla_report` row), and the breach list."""
    return json.dumps(sla_report_payload(report, year, month),
                      indent=2) + "\n"
