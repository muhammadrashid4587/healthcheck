"""Status dashboard over stdlib http.server.

GET / renders a plain HTML table: each endpoint, its current status,
uptime %, most recent latency, a sparkline of recent latencies, and
last check time. Anything else 404s.
"""

from __future__ import annotations

import html
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def latency_sparkline(history, name, width=100, height=24):
    """Inline SVG sparkline of recent latencies, or "-" with too little data.

    Uses the last 20 recorded latencies (oldest first), scaled so the
    lowest maps near the bottom and the highest near the top. A flat
    history renders as a flat line; a single point is not a line.
    """
    points = [c["latency_ms"] for c in history.recent(name, 20)]
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


def render_dashboard(history, endpoints):
    rows = []
    for ep in endpoints:
        name = ep["name"]
        cur = history.current(name)
        uptime = history.uptime_pct(name)
        up_24h = history.uptime_pct_window(name, 24 * 3600)
        up_7d = history.uptime_pct_window(name, 7 * 24 * 3600)
        if cur is None:
            status, latency, last = "UNKNOWN", "-", "-"
        else:
            status = "UP" if cur["ok"] else "DOWN"
            latency = f"{cur['latency_ms']:.1f} ms"
            last = time.strftime("%H:%M:%S", time.localtime(cur["checked_at"]))
        up = f"{uptime:.1f}%" if uptime is not None else "-"
        day = f"{up_24h:.1f}%" if up_24h is not None else "-"
        week = f"{up_7d:.1f}%" if up_7d is not None else "-"
        cls = "up" if status == "UP" else "down" if status == "DOWN" else ""
        rows.append(
            f"<tr><td>{html.escape(name)}</td>"
            f"<td class=\"{cls}\">{status}</td>"
            f"<td>{up}</td><td>{day}</td><td>{week}</td><td>{latency}</td>"
            f"<td>{latency_sparkline(history, name)}</td><td>{last}</td></tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan=\"8\">No endpoints configured.</td></tr>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>healthcheck</title>
<style>body{{font-family:sans-serif;max-width:720px;margin:2em auto}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}.up{{color:green;font-weight:bold}}.down{{color:red;font-weight:bold}}</style>
</head><body><h1>healthcheck</h1>
<table><tr><th>Endpoint</th><th>Status</th><th>Uptime</th><th>Uptime 24h</th><th>Uptime 7d</th><th>Latency</th><th>Latency history</th><th>Last check</th></tr>
{body}</table><p>Rendered {time.strftime('%Y-%m-%d %H:%M:%S')} — reload to update.</p></body></html>"""


def make_handler(history, endpoints):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
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
