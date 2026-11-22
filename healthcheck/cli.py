"""healthcheck CLI: check endpoints on a schedule, serve the dashboard.

Alerts are log lines, plus an optional webhook: when an endpoint changes
state, a JSON payload is POSTed to the configured webhook URL (see
`webhook_url` in the config, or --webhook-url). No email, no SMS.
Run it under whatever supervisor you already use and watch the logs.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import threading
import time

from .alerts import WebhookAlerter
from .checker import check_endpoint, skipped_result
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
    validate_dependencies(endpoints)
    return cfg


def validate_dependencies(endpoints):
    """Reject bad `depends_on` entries at config load.

    Every dependency must name another endpoint in the same config,
    and the dependency graph must be acyclic — a cycle would leave
    every endpoint in it waiting on another forever. Raises
    ValueError with a clear message; the monitor refuses to start
    rather than silently checking a broken dependency chain.
    """
    names = {ep["name"] for ep in endpoints}
    for ep in endpoints:
        dep = ep.get("depends_on")
        if dep is None:
            continue
        if not isinstance(dep, str):
            raise ValueError(
                f"endpoint {ep['name']!r}: depends_on must be a name, "
                f"not {dep!r}")
        if dep == ep["name"]:
            raise ValueError(
                f"endpoint {ep['name']!r} depends on itself")
        if dep not in names:
            raise ValueError(
                f"endpoint {ep['name']!r} depends on unknown endpoint "
                f"{dep!r}")
    check_order(endpoints)  # raises ValueError on a cycle


def check_order(endpoints):
    """Endpoints ordered so each dependency is checked before its
    dependents (Kahn's algorithm, config order kept among ready
    endpoints). Raises ValueError naming the cycle if one exists.
    """
    by_name = {ep["name"]: ep for ep in endpoints}
    remaining = {ep["name"]: ep.get("depends_on") for ep in endpoints}
    ordered, ready = [], [ep["name"] for ep in endpoints
                          if not ep.get("depends_on")]
    while ready:
        name = ready.pop(0)
        ordered.append(by_name[name])
        for other, dep in list(remaining.items()):
            if dep == name:
                remaining[other] = None
                ready.append(other)
        remaining.pop(name, None)
    if remaining:
        cycle = " -> ".join(sorted(remaining))
        raise ValueError(
            f"dependency cycle involving: {cycle}")
    return ordered


def history_path(cfg, args):
    """Where history is persisted, or None to keep it in-memory only.

    The --history-file flag wins over the config file's "history_file"
    key; either may be absent, in which case nothing is persisted.
    """
    if getattr(args, "history_file", None):
        return args.history_file
    return cfg.get("history_file")


def load_history(path, maxlen=100):
    """Load persisted history, or an empty History if there is none.

    A missing file is normal on first run. A corrupt or unreadable
    file logs a warning and starts empty rather than crashing — the
    monitor must keep running even if its history is lost.
    """
    if not path:
        return History(maxlen=maxlen)
    try:
        return History.load(path, maxlen=maxlen)
    except FileNotFoundError:
        return History(maxlen=maxlen)
    except (OSError, ValueError, AttributeError, TypeError, KeyError) as exc:
        log.warning("history file %s unreadable (%s); starting empty",
                    path, exc)
        return History(maxlen=maxlen)


def save_history(history, path):
    """Persist history atomically; warn instead of crashing on failure."""
    if not path:
        return
    try:
        history.save(path)
    except OSError as exc:
        log.warning("could not save history to %s (%s)", path, exc)


def webhook_url(cfg, args):
    """The alert webhook URL, or None to alert by log lines only.

    The --webhook-url flag wins over the config file's "webhook_url"
    key; either may be absent, in which case no webhook is called.
    """
    if getattr(args, "webhook_url", None):
        return args.webhook_url
    return cfg.get("webhook_url")


def make_alerter(cfg, args):
    """A WebhookAlerter for the configured URL, or None if none is set."""
    url = webhook_url(cfg, args) if args is not None else cfg.get("webhook_url")
    return WebhookAlerter(url) if url else None


def _dependency_state(name, results, history):
    """State of dependency `name`: "up", "down", "skipped", or "unknown".

    This round's result wins; otherwise the last recorded check is
    used, so a dependency that has never been checked is UNKNOWN.
    """
    result = results.get(name)
    if result is not None:
        if result.skipped:
            return "skipped"
        return "up" if result.ok else "down"
    entry = history.current(name)
    if entry is None:
        return "unknown"
    if entry.get("skipped", False):
        return "skipped"
    return "up" if entry["ok"] else "down"


def check_all(endpoints, history, timeout=5.0, alerter=None):
    """One check round over all endpoints, dependencies first.

    An endpoint whose `depends_on` is not UP is skipped, not checked:
    the skip is recorded in history (so the dashboard shows SKIPPED
    and the reason), but it does not count against uptime and does not
    fire alerts — a dependent cannot be "down" when its dependency is
    what failed. A skipped dependency propagates: if A is down, B
    (depends on A) is skipped, and C (depends on B) is skipped too.
    """
    results = {}
    for ep in check_order(endpoints):
        name = ep["name"]
        dep = ep.get("depends_on")
        if dep is not None:
            state = _dependency_state(dep, results, history)
            if state != "up":
                result = skipped_result(
                    name, dep, f"dependency {dep!r} is {state.upper()}")
                history.record(result)
                results[name] = result
                log.info("%s SKIPPED (%s)", name, result.error)
                continue
        result = check_endpoint(ep, timeout=timeout)
        if dep is not None:
            result = dataclasses.replace(result, depends_on=dep)
        history.record(result)
        results[name] = result
        if alerter is not None:
            alerter.notify(result)
        detail = (f"HTTP {result.status_code}" if result.status_code
                  else (result.error or ""))
        log.info("%s %s %.1f ms %s", result.name,
                 "UP" if result.ok else "DOWN",
                 result.latency_ms, detail)
    return results


def run_forever(cfg, host, port, args=None):
    endpoints = cfg.get("endpoints", [])
    interval = cfg.get("interval_seconds", 30)
    path = history_path(cfg, args) if args is not None else cfg.get("history_file")
    history = load_history(path, maxlen=cfg.get("history", 100))
    alerter = make_alerter(cfg, args)
    if path and history.names():
        log.info("loaded history for %d endpoint(s) from %s",
                 len(history.names()), path)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            check_all(endpoints, history, alerter=alerter)
            save_history(history, path)
            stop.wait(interval)

    thread = threading.Thread(target=loop, name="checker", daemon=True)
    thread.start()
    log.info("dashboard at http://%s:%d/ — checking %d endpoint(s) every %ds",
             host, port, len(endpoints), interval)
    try:
        serve(history, endpoints, host=host, port=port)
    except KeyboardInterrupt:
        stop.set()
    finally:
        save_history(history, path)


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
    parser.add_argument("--history-file", default=None,
                        help="persist check history to this JSON file "
                             "(overrides the config file's history_file)")
    parser.add_argument("--webhook-url", default=None,
                        help="POST a JSON alert to this URL when an "
                             "endpoint changes state (overrides the "
                             "config file's webhook_url)")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.once:
        path = history_path(cfg, args)
        history = load_history(path, maxlen=cfg.get("history", 100))
        check_all(cfg.get("endpoints", []), history,
                  alerter=make_alerter(cfg, args))
        save_history(history, path)
        return 0
    run_forever(cfg, args.host, args.port, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
