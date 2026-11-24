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


def incidents_report(history, endpoints, now=None, name=None, limit=None):
    """One row per endpoint plus totals.

    Each row is {"name", "incidents", "total_incidents",
    "ongoing_incidents", "total_downtime_seconds",
    "duration_histogram"} where
    "incidents" is the endpoint's full recorded incident list,
    newest first — each {"start", "end", "checks", "ongoing",
    "duration_s"} from `History.recent_incidents()`, so the
    export reports exactly what the dashboard and the APIs show.
    "duration_histogram" is the same incidents bucketed by
    duration (`History.incident_duration_histogram`, same limit),
    so the histogram can never disagree with the incidents the
    row lists.
    An ongoing incident has "end" None and its duration runs to
    `now` (the current time when not given). Returns
    {"generated_at", "endpoints", "totals"} where "totals" sums
    the per-endpoint counts across endpoints. Rows follow config
    order. The recorded history is bounded, so this is every
    incident still recorded, not necessarily every incident since
    the monitor first ran.

    `name`, when given, exports only that endpoint — an unknown
    name raises ValueError, like `GET /api/incidents` answers a
    bad `?endpoint=` with a 400 instead of an empty report, so a
    typo does not silently export nothing. `limit`, when given,
    caps the incidents per endpoint (newest first); with no limit
    every recorded incident is exported. The totals always sum
    the rows actually exported, so a filtered export's totals
    are consistent with its own rows, and an unfiltered export's
    totals are the same as before the filters existed.
    """
    known = {ep["name"] for ep in endpoints}
    if name is not None and name not in known:
        raise ValueError(f"unknown endpoint: {name}")
    selected = ([ep for ep in endpoints if ep["name"] == name]
                if name is not None else endpoints)
    ref = time.time() if now is None else now
    rows = []
    for ep in selected:
        incidents = history.recent_incidents(
            ep["name"], limit=limit, now=ref)
        rows.append({
            "name": ep["name"],
            "incidents": incidents,
            "total_incidents": len(incidents),
            "ongoing_incidents": sum(
                1 for i in incidents if i["ongoing"]),
            "total_downtime_seconds": round(
                sum(i["duration_s"] for i in incidents), 1),
            "duration_histogram":
                history.incident_duration_histogram(
                    ep["name"], limit=limit, now=ref),
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
    check count, then the endpoint's duration histogram (log-spaced
    buckets over the same incidents — omitted when the endpoint
    has no incidents); a totals line at the end.
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
        if row["total_incidents"]:
            lines.append("    Duration histogram:")
            for bucket in row["duration_histogram"]:
                lines.append(f"      {bucket['label']}: "
                             f"{bucket['count']}")
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
