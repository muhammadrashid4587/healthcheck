"""healthcheck CLI: check endpoints on a schedule, serve the dashboard.

Alerts are just log lines — no email, no SMS, no webhooks. Run it under
whatever supervisor you already use and watch the logs.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time

from .checker import check_endpoint
from .server import serve
from .store import History

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("healthcheck")


def load_config(path):
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    endpoints = cfg.get("endpoints", [])
    for ep in endpoints:
        if "name" not in ep or "type" not in ep:
            raise ValueError(f"endpoint missing name/type: {ep!r}")
        if ep["type"] == "http" and "url" not in ep:
            raise ValueError(f"http endpoint missing url: {ep!r}")
        if ep["type"] == "tcp" and ("host" not in ep or "port" not in ep):
            raise ValueError(f"tcp endpoint missing host/port: {ep!r}")
    return cfg


def check_all(endpoints, history, timeout=5.0):
    for ep in endpoints:
        result = check_endpoint(ep, timeout=timeout)
        history.record(result)
        detail = (f"HTTP {result.status_code}" if result.status_code
                  else (result.error or ""))
        log.info("%s %s %.1f ms %s", result.name,
                 "UP" if result.ok else "DOWN",
                 result.latency_ms, detail)


def run_forever(cfg, host, port):
    endpoints = cfg.get("endpoints", [])
    interval = cfg.get("interval_seconds", 30)
    history = History(maxlen=cfg.get("history", 100))
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            check_all(endpoints, history)
            stop.wait(interval)

    thread = threading.Thread(target=loop, name="checker", daemon=True)
    thread.start()
    log.info("dashboard at http://%s:%d/ — checking %d endpoint(s) every %ds",
             host, port, len(endpoints), interval)
    try:
        serve(history, endpoints, host=host, port=port)
    except KeyboardInterrupt:
        stop.set()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="healthcheck",
        description="Tiny service health monitor with a status dashboard.")
    parser.add_argument("--config", default="config.json",
                        help="JSON config file (default: config.json)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--once", action="store_true",
                        help="check every endpoint once and exit")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.once:
        check_all(cfg.get("endpoints", []), History())
        return 0
    run_forever(cfg, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
