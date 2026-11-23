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
import re
import threading
import time

from .alerts import WebhookAlerter
from .checker import check_endpoint, maintenance_result, skipped_result
from .flap import DEFAULT_THRESHOLD, FlapDetector, validate_threshold
from .maintenance import (active_window, summarize_window,
                          validate_maintenance_windows)
from .server import serve
from .sla import (parse_month, render_sla_report, sla_report,
                  validate_sla_targets)
from .statuspage import write_status_page
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
    validate_body_assertions(endpoints)
    validate_maintenance_windows(endpoints)
    validate_sla_targets(endpoints)
    if "flap_threshold" in cfg:
        validate_threshold(cfg["flap_threshold"])
    return cfg


def validate_body_assertions(endpoints):
    """Reject bad `expect_body` / `expect_body_regex` entries at
    config load. Body assertions only apply to http endpoints; a
    pattern that does not compile is rejected here rather than
    failing every check round. Raises ValueError with a clear
    message; the monitor refuses to start rather than run with a
    check it can never pass.
    """
    for ep in endpoints:
        for key in ("expect_body", "expect_body_regex"):
            if key not in ep:
                continue
            if ep["type"] != "http":
                raise ValueError(
                    f"endpoint {ep['name']!r}: {key} only applies to "
                    f"http endpoints")
            if not isinstance(ep[key], str):
                raise ValueError(
                    f"endpoint {ep['name']!r}: {key} must be a string, "
                    f"not {ep[key]!r}")
        pattern = ep.get("expect_body_regex")
        if pattern is not None:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(
                    f"endpoint {ep['name']!r}: expect_body_regex "
                    f"{pattern!r} does not compile: {exc}")


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


def flap_threshold(cfg, args):
    """Consecutive failures that confirm an outage, or the default.

    The --flap-threshold flag wins over the config file's
    "flap_threshold" key; either may be absent, in which case the
    default (3) applies. A bad value raises ValueError — the monitor
    refuses to start rather than alert on the wrong threshold.
    """
    if getattr(args, "flap_threshold", None) is not None:
        return validate_threshold(args.flap_threshold)
    if "flap_threshold" in cfg:
        return validate_threshold(cfg["flap_threshold"])
    return DEFAULT_THRESHOLD


def _dependency_state(name, results, history):
    """State of dependency `name`: "up", "down", "skipped", "maintenance",
    or "unknown".

    This round's result wins; otherwise the last recorded check is
    used, so a dependency that has never been checked is UNKNOWN.
    A failure below the flap threshold is not a confirmed outage, so
    the dependency still counts as UP and its dependents are checked.
    A dependency inside a maintenance window is MAINTENANCE, so its
    dependents are skipped — there is nothing to depend on while the
    thing they need is deliberately paused.
    """
    result = results.get(name)
    if result is not None:
        if result.maintenance:
            return "maintenance"
        if result.skipped:
            return "skipped"
        if result.ok or result.unconfirmed:
            return "up"
        return "down"
    entry = history.current(name)
    if entry is None:
        return "unknown"
    if entry.get("maintenance", False):
        return "maintenance"
    if entry.get("skipped", False):
        return "skipped"
    if entry["ok"] or entry.get("unconfirmed", False):
        return "up"
    return "down"


def check_all(endpoints, history, timeout=5.0, alerter=None,
              flap_detector=None, now=None):
    """One check round over all endpoints, dependencies first.

    An endpoint inside a maintenance window is not checked at all:
    the round is recorded as maintenance, so the dashboard shows
    MAINTENANCE and the window's summary, but no alert fires, no
    flap state changes, and uptime is untouched — a paused check is
    neither up nor down. `now` is epoch seconds and defaults to the
    current time; pass it in tests to pin the moment.

    An endpoint whose `depends_on` is not UP is skipped, not checked:
    the skip is recorded in history (so the dashboard shows SKIPPED
    and the reason), but it does not count against uptime and does not
    fire alerts — a dependent cannot be "down" when its dependency is
    what failed. A skipped dependency propagates: if A is down, B
    (depends on A) is skipped, and C (depends on B) is skipped too.

    Flap detection: a failed check is only a confirmed outage after
    `flap_detector`'s threshold of consecutive failures. Below the
    threshold the failure is recorded as unconfirmed — logged, but no
    alert, no uptime penalty, and the endpoint stays UP on the
    dashboard. A success resets the counter; a success after a
    confirmed outage is a recovery. Callers running repeated rounds
    must pass one shared detector; a fresh default (threshold 3) is
    created when none is given, which is only correct for a single
    round such as --once.
    """
    if flap_detector is None:
        flap_detector = FlapDetector()
    results = {}
    for ep in check_order(endpoints):
        name = ep["name"]
        window = active_window(ep.get("maintenance_windows"), now)
        if window is not None:
            summary = summarize_window(window)
            result = maintenance_result(name, summary)
            history.record(result)
            results[name] = result
            log.info("%s MAINTENANCE (%s)", name, summary)
            continue
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
        outcome = flap_detector.observe(name, result.ok)
        if not result.ok and not outcome.confirmed_down:
            result = dataclasses.replace(result, unconfirmed=True)
        history.record(result)
        results[name] = result
        if alerter is not None:
            # Feed the alerter the *confirmed* state: an unconfirmed
            # failure looks like "still up" to it, so nothing fires
            # until the threshold is reached. The Nth consecutive
            # failure and the success that recovers from it are real
            # transitions and alert exactly as before.
            effective = result if (result.ok or outcome.confirmed_down) \
                else dataclasses.replace(result, ok=True)
            alerter.notify(effective)
        if result.ok:
            log.info("%s UP %.1f ms", result.name, result.latency_ms)
        elif outcome.confirmed_down:
            detail = (f"HTTP {result.status_code}" if result.status_code
                      else (result.error or ""))
            log.info("%s DOWN %.1f ms %s", result.name,
                     result.latency_ms, detail)
        else:
            detail = (f"HTTP {result.status_code}" if result.status_code
                      else (result.error or ""))
            log.info("%s DOWN (unconfirmed %d/%d) %.1f ms %s", result.name,
                     outcome.consecutive_failures,
                     flap_detector.threshold, result.latency_ms, detail)
    return results


def run_forever(cfg, host, port, args=None):
    endpoints = cfg.get("endpoints", [])
    interval = cfg.get("interval_seconds", 30)
    path = history_path(cfg, args) if args is not None else cfg.get("history_file")
    history = load_history(path, maxlen=cfg.get("history", 100))
    alerter = make_alerter(cfg, args)
    # One detector for the life of the process: the consecutive
    # failure count must survive across rounds.
    detector = FlapDetector(flap_threshold(cfg, args))
    if path and history.names():
        log.info("loaded history for %d endpoint(s) from %s",
                 len(history.names()), path)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector)
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
    parser.add_argument("--flap-threshold", type=int, default=None,
                        help="alert only after this many consecutive "
                             "failed checks (overrides the config "
                             "file's flap_threshold, default 3)")
    parser.add_argument("--status-page", default=None, metavar="PATH",
                        help="write a self-contained static HTML status "
                             "page from the persisted check history to "
                             "PATH and exit (combines with --once to "
                             "check first, then render)")
    parser.add_argument("--sla-report", default=None, nargs="?",
                        const="current", metavar="YYYY-MM",
                        help="print the monthly SLA report (uptime per "
                             "endpoint vs its sla_target, with breaches) "
                             "for the given month and exit; with no month "
                             "given, the current month is used")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    history = None
    if args.sla_report:
        if args.sla_report == "current":
            now = time.localtime()
            year, month = now.tm_year, now.tm_mon
        else:
            try:
                year, month = parse_month(args.sla_report)
            except ValueError as exc:
                parser.error(str(exc))
        path = history_path(cfg, args)
        history = load_history(path, maxlen=cfg.get("history", 100))
        report = sla_report(history, cfg.get("endpoints", []), year, month)
        print(render_sla_report(report, year, month), end="")
        return 0
    if args.once:
        path = history_path(cfg, args)
        history = load_history(path, maxlen=cfg.get("history", 100))
        check_all(cfg.get("endpoints", []), history,
                  alerter=make_alerter(cfg, args),
                  flap_detector=FlapDetector(flap_threshold(cfg, args)))
        save_history(history, path)
    if args.status_page:
        if history is None:
            path = history_path(cfg, args)
            history = load_history(path, maxlen=cfg.get("history", 100))
        write_status_page(history, cfg.get("endpoints", []),
                          args.status_page)
        log.info("status page written to %s", args.status_page)
        return 0
    if args.once:
        return 0
    run_forever(cfg, args.host, args.port, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
