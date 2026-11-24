"""Static status page: render the monitor's state as one self-contained
HTML file that can be hosted anywhere with no server.

`render_status_page` builds the same view as the live dashboard —
current status per endpoint, uptime all-time / 24h / 7d, most recent
latency, an inline latency sparkline — plus a recent-incidents list
per endpoint, derived from the persisted check history. The output
has no external references (no scripts, stylesheets, images or links
to other hosts), so the file works from any static host or straight
off disk. It is a snapshot: regenerate it to update it.

`write_status_page` writes the page atomically, like history
persistence does, so a reader never sees a half-written file.
"""

from __future__ import annotations

import html
import os
import time

from .server import latency_sparkline

# How many recent incidents to list per endpoint.
DEFAULT_INCIDENT_LIMIT = 5


def recent_incidents(history, name, limit=DEFAULT_INCIDENT_LIMIT):
    """Recent outage incidents for an endpoint, newest first.

    Consecutive confirmed-down checks form one incident: it starts
    at the first down check and ends at the first passing check
    after it (the recovery). An incident still in progress at the
    end of the recorded history has `end` None and `ongoing` True.
    Skips, unconfirmed failures and maintenance rounds are not
    outages and neither start nor end an incident — a skip in the
    middle of a run of downs does not split it, and a recovery must
    be a real passing check.

    The grouping lives on History so the dashboard API can report
    the same incidents; this is a thin wrapper over it.
    """
    return history.recent_incidents(name, limit=limit)


def endpoint_status(history, name):
    """Current status string for an endpoint, same as the dashboard:
    UP / DOWN / SKIPPED / MAINTENANCE / UNKNOWN. A failure below the
    flap threshold still shows UP — the outage is not confirmed."""
    cur = history.current(name)
    if cur is None:
        return "UNKNOWN"
    if cur.get("maintenance", False):
        return "MAINTENANCE"
    if cur.get("skipped", False):
        return "SKIPPED"
    return "UP" if (cur["ok"] or cur.get("unconfirmed", False)) else "DOWN"


def _fmt_time(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _fmt_duration(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _render_source(history, endpoints, generated, label=None):
    """Render one source's banner, endpoint table and incidents.

    A source is one healthcheck instance's history: the same status,
    uptime and incident-grouping rules apply to every source, so a
    second source's endpoints are judged exactly like the local
    ones. `label` names the source in the page; None means the page
    has a single source, so no heading is added and the output is
    the same as before extra sources existed.
    """
    rows = []
    incident_sections = []
    down_count = 0
    for ep in endpoints:
        name = ep["name"]
        status = endpoint_status(history, name)
        if status == "DOWN":
            down_count += 1
        uptime = history.uptime_pct(name)
        up_24h = history.uptime_pct_window(name, 24 * 3600, now=generated)
        up_7d = history.uptime_pct_window(name, 7 * 24 * 3600, now=generated)
        cur = history.current(name)
        if cur is None or cur.get("maintenance", False) \
                or cur.get("skipped", False):
            latency, last = "-", "-"
        else:
            latency = f"{cur['latency_ms']:.1f} ms"
            last = _fmt_time(cur["checked_at"])
        if cur is not None and cur.get("skipped", False):
            status_detail = f" — {html.escape(cur.get('error') or '')}"
        else:
            status_detail = ""
        up = f"{uptime:.1f}%" if uptime is not None else "-"
        day = f"{up_24h:.1f}%" if up_24h is not None else "-"
        week = f"{up_7d:.1f}%" if up_7d is not None else "-"
        cls = ("up" if status == "UP" else "down" if status == "DOWN"
               else "skipped" if status == "SKIPPED"
               else "maintenance" if status == "MAINTENANCE" else "")
        rows.append(
            f"<tr><td>{html.escape(name)}</td>"
            f"<td class=\"{cls}\">{status}{status_detail}</td>"
            f"<td>{up}</td><td>{day}</td><td>{week}</td><td>{latency}</td>"
            f"<td>{latency_sparkline(history, name)}</td><td>{last}</td></tr>"
        )
        incidents = recent_incidents(history, name)
        if incidents:
            items = []
            for incident in incidents:
                start = _fmt_time(incident["start"])
                if incident["ongoing"]:
                    end = "ongoing"
                else:
                    end = _fmt_time(incident["end"])
                items.append(
                    f"<li>{start} – {end} "
                    f"({_fmt_duration(incident['duration_s'])}, "
                    f"{incident['checks']} failed check"
                    f"{'s' if incident['checks'] != 1 else ''})</li>")
            incident_sections.append(
                f"<h3>{html.escape(name)}</h3><ul>{''.join(items)}</ul>")
    body = "\n".join(rows) or \
        "<tr><td colspan=\"8\">No endpoints configured.</td></tr>"
    if down_count:
        banner = (f"<p class=\"banner down\">{down_count} endpoint"
                  f"{'s' if down_count != 1 else ''} down</p>")
    else:
        banner = "<p class=\"banner up\">All systems operational</p>"
    incidents_html = ("\n".join(incident_sections)
                      if incident_sections
                      else "<p>No incidents recorded.</p>")
    table = (
        "<table><tr><th>Endpoint</th><th>Status</th><th>Uptime</th>"
        "<th>Uptime 24h</th><th>Uptime 7d</th><th>Latency</th>"
        "<th>Latency history</th><th>Last check</th></tr>\n"
        f"{body}</table>")
    if label is None:
        return (f"{banner}\n{table}\n<h2>Recent incidents</h2>\n"
                f"{incidents_html}")
    return (f"<h2>{html.escape(label)}</h2>\n{banner}\n{table}\n"
            f"<h3>Recent incidents</h3>\n{incidents_html}")


def render_status_page(history, endpoints, now=None, extra_sources=None):
    """Render the whole status page as one self-contained HTML string.

    `now` is epoch seconds and defaults to the current time; pass it
    in tests to pin the generated timestamp. `extra_sources` is a
    list of `(label, history, endpoints)` tuples — a second (or
    further) healthcheck instance's persisted history, merged into
    the same page under its own labelled section, with its endpoints
    attributed to that source. With no extra sources the page is
    exactly the single-source page as before.
    """
    generated = time.time() if now is None else now
    if not extra_sources:
        sections = _render_source(history, endpoints, generated)
    else:
        parts = [_render_source(history, endpoints, generated,
                                label="local")]
        for label, src_history, src_endpoints in extra_sources:
            parts.append(_render_source(src_history, src_endpoints,
                                        generated, label=label))
        sections = "\n".join(parts)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>healthcheck status</title>
<style>body{{font-family:sans-serif;max-width:720px;margin:2em auto}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}.up{{color:green;font-weight:bold}}.down{{color:red;font-weight:bold}}.skipped{{color:#b8860b;font-weight:bold}}.maintenance{{color:#6a5acd;font-weight:bold}}.banner{{font-size:1.2em}}</style>
</head><body><h1>Status</h1>
{sections}
<p>Generated {_fmt_time(generated)} — static snapshot; regenerate to update.</p></body></html>"""


def write_status_page(history, endpoints, path, now=None,
                      extra_sources=None):
    """Render the status page and write it atomically to `path`.

    Writes to a temp file in the same directory, fsyncs, then
    renames, so a reader never sees a half-written page. Returns
    the path written. `extra_sources` is passed through to
    `render_status_page` — see there.
    """
    page = render_status_page(history, endpoints, now=now,
                              extra_sources=extra_sources)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(page)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path
