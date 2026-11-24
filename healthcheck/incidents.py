"""Incident history export: dump the recorded outage history.

`--incidents` exports the same incidents the dashboard, GET
/api/status, GET /api/incidents, and the static status page show —
every incident comes from `History.recent_incidents()`, so the
export can never disagree with them. An ongoing incident has
`end` None and its duration runs to the export time. The export
is a snapshot of the persisted history (needs `history_file` in
the config, or `--history-file`); it lists every recorded
incident per endpoint, with per-endpoint incident counts and
totals, so it can go into a spreadsheet or a script.
"""

from __future__ import annotations

import csv
import io
import json
import time


def incidents_report(history, endpoints, now=None):
    """One row per endpoint plus totals.

    Each row is {"name", "incidents", "total_incidents",
    "ongoing_incidents", "total_downtime_seconds"} where
    "incidents" is the endpoint's full recorded incident list,
    newest first — each {"start", "end", "checks", "ongoing",
    "duration_s"} from `History.recent_incidents()`, so the
    export reports exactly what the dashboard and the APIs show.
    An ongoing incident has "end" None and its duration runs to
    `now` (the current time when not given). Returns
    {"generated_at", "endpoints", "totals"} where "totals" sums
    the per-endpoint counts across endpoints. Rows follow config
    order. The recorded history is bounded, so this is every
    incident still recorded, not necessarily every incident since
    the monitor first ran.
    """
    ref = time.time() if now is None else now
    rows = []
    for ep in endpoints:
        incidents = history.recent_incidents(
            ep["name"], limit=None, now=ref)
        rows.append({
            "name": ep["name"],
            "incidents": incidents,
            "total_incidents": len(incidents),
            "ongoing_incidents": sum(
                1 for i in incidents if i["ongoing"]),
            "total_downtime_seconds": round(
                sum(i["duration_s"] for i in incidents), 1),
        })
    return {
        "generated_at": ref,
        "endpoints": rows,
        "totals": {
            "total_incidents": sum(r["total_incidents"] for r in rows),
            "ongoing_incidents": sum(r["ongoing_incidents"] for r in rows),
            "total_downtime_seconds": round(
                sum(r["total_downtime_seconds"] for r in rows), 1),
        },
    }


def _fmt_time(epoch):
    """Epoch seconds as a local-time timestamp for the text report."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _plural(n, word):
    return f"{n} {word}{'s' if n != 1 else ''}"


def render_incidents_report(report):
    """Render an incident report as plain text.

    One line per endpoint with its incident count, how many are
    ongoing, and its total downtime, then one line per incident
    with its start and end (or "ongoing"), duration, and failed
    check count; a totals line at the end.
    """
    lines = ["Incident report (generated "
             f"{_fmt_time(report['generated_at'])})"]
    for row in report["endpoints"]:
        summary = (f"  {row['name']}: "
                   f"{_plural(row['total_incidents'], 'incident')}")
        if row["ongoing_incidents"]:
            summary += f" ({row['ongoing_incidents']} ongoing)"
        summary += (f", {row['total_downtime_seconds']:.1f}s total "
                    f"downtime")
        lines.append(summary)
        for incident in row["incidents"]:
            end = ("ongoing" if incident["end"] is None
                   else _fmt_time(incident["end"]))
            lines.append(
                f"    {_fmt_time(incident['start'])} – {end} "
                f"({incident['duration_s']:.1f}s, "
                f"{_plural(incident['checks'], 'check')})")
    totals = report["totals"]
    line = _plural(totals["total_incidents"], "incident") + " total"
    if totals["ongoing_incidents"]:
        line += f" ({totals['ongoing_incidents']} ongoing)"
    line += f", {totals['total_downtime_seconds']:.1f}s total downtime"
    lines.append(line)
    return "\n".join(lines) + "\n"


def render_incidents_report_csv(report):
    """Render an incident report as CSV, one row per incident.

    Columns: endpoint, start, end, ongoing, duration_s, checks.
    Start and end are epoch seconds; `end` is empty for an ongoing
    incident. An endpoint with no incidents contributes no rows,
    and a report with no incidents anywhere is just the header.
    """
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["endpoint", "start", "end", "ongoing",
                     "duration_s", "checks"])
    for row in report["endpoints"]:
        for incident in row["incidents"]:
            writer.writerow([
                row["name"], incident["start"],
                "" if incident["end"] is None else incident["end"],
                str(incident["ongoing"]).lower(),
                incident["duration_s"], incident["checks"],
            ])
    return out.getvalue()


def render_incidents_report_json(report):
    """Render an incident report as JSON: the generated time, one
    object per endpoint (name, the full incident list, and the
    per-endpoint counts and totals), and the totals across
    endpoints."""
    return json.dumps(report, indent=2) + "\n"
