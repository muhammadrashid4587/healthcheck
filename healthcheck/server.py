"""Status dashboard over stdlib http.server.

GET / renders a plain HTML table: each endpoint, its current status,
uptime %, most recent latency, a sparkline of recent latencies, and
last check time. GET /api/status returns the same data as JSON, for
scripts and other tools that want to consume the live status without
scraping the HTML. A check skipped because its dependency is not UP
renders as SKIPPED, with the dependency named in the Depends on
column; an endpoint inside a maintenance window renders as
MAINTENANCE, with its windows listed in the Maintenance column.
Anything else 404s.
"""

from __future__ import annotations

import html
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .maintenance import summarize_windows


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


def status_snapshot(history, endpoints, now=None):
    """Machine-readable status for every endpoint: the same data the
    dashboard renders, as plain JSON-friendly values.

    `now` pins the 24-hour windows (uptime 24h/7d and latency
    percentiles); it defaults to the current time. Returns
    {"generated_at": <epoch seconds>, "endpoints": [...]} where
    each endpoint dict has "name", "status" (UP / DOWN / SKIPPED /
    MAINTENANCE / UNKNOWN — same rules as the dashboard, including
    unconfirmed failures below the flap threshold still showing UP),
    "depends_on" (name or None), "maintenance_windows" (list of
    {"start", "end", "days"} dicts, or []), "uptime_pct",
    "uptime_24h_pct", "uptime_7d_pct" (None when there is no data in
    that window — no data is not downtime), "latency_ms" (most recent
    recorded latency, or None), "latency_percentiles" ({"p50", "p95",
    "p99"} in ms over the last 24 hours, or None when there are no
    recorded latencies in that window), and "last_checked_at"
    (epoch seconds, or None when never checked).
    """
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
        out.append({
            "name": name,
            "status": status,
            "depends_on": ep.get("depends_on"),
            "maintenance_windows": windows,
            "uptime_pct": history.uptime_pct(name),
            "uptime_24h_pct": history.uptime_pct_window(name, 24 * 3600),
            "uptime_7d_pct": history.uptime_pct_window(name, 7 * 24 * 3600),
            "latency_ms": latency,
            "latency_percentiles": history.latency_percentiles(name, now=now),
            "last_checked_at": last,
        })
    return {"generated_at": time.time(), "endpoints": out}


def render_dashboard(history, endpoints, now=None):
    rows = []
    for ep in endpoints:
        name = ep["name"]
        cur = history.current(name)
        uptime = history.uptime_pct(name)
        up_24h = history.uptime_pct_window(name, 24 * 3600, now=now)
        up_7d = history.uptime_pct_window(name, 7 * 24 * 3600, now=now)
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
        pct = history.latency_percentiles(name, now=now)
        pct_text = (f"{pct['p50']:.1f} / {pct['p95']:.1f} / {pct['p99']:.1f} ms"
                    if pct is not None else "-")
        cls = ("up" if status == "UP" else "down" if status == "DOWN"
               else "skipped" if status == "SKIPPED"
               else "maintenance" if status == "MAINTENANCE" else "")
        rows.append(
            f"<tr><td>{html.escape(name)}</td>"
            f"<td class=\"{cls}\">{status}</td>"
            f"<td>{html.escape(depends_on)}</td>"
            f"<td>{html.escape(windows)}</td>"
            f"<td>{up}</td><td>{day}</td><td>{week}</td><td>{latency}</td>"
            f"<td>{pct_text}</td>"
            f"<td>{latency_sparkline(history, name)}</td><td>{last}</td></tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan=\"11\">No endpoints configured.</td></tr>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>healthcheck</title>
<style>body{{font-family:sans-serif;max-width:720px;margin:2em auto}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}.up{{color:green;font-weight:bold}}.down{{color:red;font-weight:bold}}.skipped{{color:#b8860b;font-weight:bold}}.maintenance{{color:#6a5acd;font-weight:bold}}</style>
</head><body><h1>healthcheck</h1>
<table><tr><th>Endpoint</th><th>Status</th><th>Depends on</th><th>Maintenance</th><th>Uptime</th><th>Uptime 24h</th><th>Uptime 7d</th><th>Latency</th><th>Latency p50 / p95 / p99</th><th>Latency history</th><th>Last check</th></tr>
{body}</table><p>Rendered {time.strftime('%Y-%m-%d %H:%M:%S')} — reload to update.</p></body></html>"""


def make_handler(history, endpoints):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/status":
                payload = json.dumps(
                    status_snapshot(history, endpoints)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path != "/":
                self.send_response(404)
                self.end_headers()
                return
            page = render_dashboard(history, endpoints).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass  # dashboard access is not logged; check results are

    return Handler


def serve(history, endpoints, host="127.0.0.1", port=8080):
    """Serve the dashboard forever (Ctrl-C to stop)."""
    server = ThreadingHTTPServer((host, port), make_handler(history, endpoints))
    server.serve_forever()
