"""Status dashboard over stdlib http.server.

GET / renders a plain HTML table: each endpoint, its current status,
uptime %, most recent latency, and last check time. Anything else 404s.
"""

from __future__ import annotations

import html
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def render_dashboard(history, endpoints):
    rows = []
    for ep in endpoints:
        name = ep["name"]
        cur = history.current(name)
        uptime = history.uptime_pct(name)
        if cur is None:
            status, latency, last = "UNKNOWN", "-", "-"
        else:
            status = "UP" if cur["ok"] else "DOWN"
            latency = f"{cur['latency_ms']:.1f} ms"
            last = time.strftime("%H:%M:%S", time.localtime(cur["checked_at"]))
        up = f"{uptime:.1f}%" if uptime is not None else "-"
        cls = "up" if status == "UP" else "down" if status == "DOWN" else ""
        rows.append(
            f"<tr><td>{html.escape(name)}</td>"
            f"<td class=\"{cls}\">{status}</td>"
            f"<td>{up}</td><td>{latency}</td><td>{last}</td></tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan=\"5\">No endpoints configured.</td></tr>"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>healthcheck</title>
<style>body{{font-family:sans-serif;max-width:720px;margin:2em auto}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}.up{{color:green;font-weight:bold}}.down{{color:red;font-weight:bold}}</style>
</head><body><h1>healthcheck</h1>
<table><tr><th>Endpoint</th><th>Status</th><th>Uptime</th><th>Latency</th><th>Last check</th></tr>
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
