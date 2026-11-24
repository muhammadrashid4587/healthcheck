"""healthcheck CLI: check endpoints on a schedule, serve the dashboard.

Alerts are log lines, plus an optional webhook: when an endpoint changes
state, a JSON payload is POSTed to the configured webhook URL (see
`webhook_url` in the config, or --webhook-url). An `alert_digest`
block in the config batches confirmed changes into a periodic
digest instead of (or in addition to) one webhook per change — see
healthcheck.digest. No SMS: a digest configured with `email_to`
renders a MIME message and delivers it over SMTP when `smtp_host`
is set (or the HEALTHCHECK_SMTP_* env vars); with no SMTP host the
rendered message is logged instead, so an operator can pipe it to
sendmail.
Run it under whatever supervisor you already use and watch the logs.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import signal
import threading
import time

from .alerts import SlaBreachAlerter, WebhookAlerter
from .checker import (check_endpoint_with_retry, maintenance_result,
                      skipped_result)
from .digest import DigestAlerter, validate_digest_config
from .flap import DEFAULT_THRESHOLD, FlapDetector, validate_threshold
from .maintenance import (active_window, summarize_window,
                          validate_maintenance_windows)
from .remote import fetch_remote_status
from .server import serve
from .sla import (breaches, monthly_detail, monthly_uptime, parse_month,
                  render_sla_report, render_sla_report_csv,
                  render_sla_report_json, sla_report, validate_sla_targets)
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
    validate_expect_status(endpoints)
    validate_latency_budgets(endpoints)
    validate_check_intervals(endpoints)
    validate_retry_settings(cfg, endpoints)
    validate_maintenance_windows(endpoints)
    validate_sla_targets(endpoints)
    validate_endpoint_webhooks(endpoints)
    validate_status_page_sources(cfg)
    if "flap_threshold" in cfg:
        validate_threshold(cfg["flap_threshold"])
    if "alert_digest" in cfg:
        validate_digest_config(cfg["alert_digest"])
    return cfg


def reload_config(path, args, current_cfg):
    """Load the config file again, returning the new config.

    A bad edit must never take the monitor down: if the file is
    missing, unreadable, or fails validation, a warning is logged
    and the current config is returned unchanged (identity — the
    caller checks `is` to tell a reload from a no-op).
    """
    try:
        return load_config(path)
    except (OSError, ValueError) as exc:
        log.warning("config reload from %s failed (%s); keeping the "
                    "current config", path, exc)
        return current_cfg


def install_reload_handler(reload_requested):
    """Install a SIGHUP handler that flags a config reload.

    The handler only sets an event; the checker loop applies the
    reload before its next round, so a reload never interrupts a
    check in progress. Signals can only be installed from the main
    thread, and SIGHUP does not exist on Windows — where either
    applies, this logs a warning and returns False instead of
    crashing, and the monitor runs without hot-reload.
    """
    def handler(signum, frame):
        reload_requested.set()
        log.info("SIGHUP received; config reloads before the next "
                 "check round")

    try:
        signal.signal(signal.SIGHUP, handler)
    except (AttributeError, OSError, ValueError) as exc:
        log.warning("could not install SIGHUP handler (%s); config "
                    "reload unavailable", exc)
        return False
    return True


def validate_status_page_sources(cfg):
    """Normalize the `status_page_sources` config key.

    Each entry is {"label": ..., "path": ...} for another instance's
    persisted history file, or {"label": ..., "url": ...} for a
    remote instance's GET /api/status. A bad entry — not an object,
    no usable label, both or neither of path/url, a non-http(s) url,
    an unknown key, a duplicate label — is logged as a warning and
    skipped, never fatal: a bad second source must never take the
    monitor down, the same rule as a bad edit on a SIGHUP reload.
    Returns the normalized list, and stores it back in cfg so
    callers and reloads see the same shape.
    """
    raw = cfg.get("status_page_sources", [])
    if not isinstance(raw, list):
        log.warning("status_page_sources must be a list, not %s; "
                    "ignoring it", type(raw).__name__)
        cfg["status_page_sources"] = []
        return []
    sources, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning("status_page_sources entry %r is not an "
                        "object; skipping it", entry)
            continue
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            log.warning("status_page_sources entry %r has no label; "
                        "skipping it", entry)
            continue
        label = label.strip()
        if label in seen:
            log.warning("status_page_sources label %r is duplicated; "
                        "keeping the first entry", label)
            continue
        keys = set(entry)
        if keys - {"label", "path", "url"}:
            log.warning("status_page_sources entry for %r has unknown "
                        "keys %s; skipping it", label,
                        sorted(keys - {"label", "path", "url"}))
            continue
        path, url = entry.get("path"), entry.get("url")
        if (path is None) == (url is None):
            log.warning("status_page_sources entry for %r needs exactly "
                        "one of path/url; skipping it", label)
            continue
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                log.warning("status_page_sources entry for %r has a "
                            "bad path %r; skipping it", label, path)
                continue
            source = {"label": label, "path": path.strip()}
        else:
            if (not isinstance(url, str) or not url.strip()
                    or not url.strip().startswith(("http://", "https://"))):
                log.warning("status_page_sources entry for %r has a "
                            "bad url %r; skipping it", label, url)
                continue
            source = {"label": label, "url": url.strip()}
        seen.add(label)
        sources.append(source)
    cfg["status_page_sources"] = sources
    return sources


def merged_status_page_sources(cfg, args):
    """Config-file status page sources merged with --status-page-source.

    The config list comes first; a CLI flag with the same label as a
    config entry replaces it (the flag wins), and a new label is
    appended. Returns a list of {"label", "path"|"url"} dicts.
    Raises ValueError on a bad CLI spec — the caller reports it.
    `cfg` is expected to be a loaded config (see
    `validate_status_page_sources`); a hand-built config without
    the key simply contributes nothing.
    """
    merged = {src["label"]: dict(src)
              for src in cfg.get("status_page_sources", [])}
    for spec in getattr(args, "status_page_source", None) or []:
        label, src = parse_status_page_source(spec)
        if src.startswith(("http://", "https://")):
            merged[label] = {"label": label, "url": src}
        else:
            merged[label] = {"label": label, "path": src}
    return list(merged.values())


def load_status_page_source(label, src, cfg):
    """Load one status-page source as (label, history, endpoints).

    A remote (http(s)) source is scraped via fetch_remote_status;
    anything else is read as a persisted history file. A missing,
    corrupt, or unreachable source logs a warning and renders
    empty instead of taking the page down.
    """
    if src.startswith(("http://", "https://")):
        # A remote instance: scrape its GET /api/status.
        # Unreachable or bad data logs a warning and renders
        # empty, like a missing history file — a bad second
        # source must never take down the page.
        src_history = fetch_remote_status(src)
        if src_history is None:
            src_history = History(maxlen=cfg.get("history", 100))
        src_endpoints = [{"name": name}
                         for name in src_history.names()]
        log.info("status page source %r fetched from %s "
                 "(%d endpoints)", label, src, len(src_endpoints))
    else:
        # A missing or corrupt second history logs a warning and
        # renders empty, like the primary one — a bad second
        # source must never take down the whole page.
        src_history = load_history(src, maxlen=cfg.get("history", 100))
        src_endpoints = [{"name": name}
                         for name in src_history.names()]
        log.info("status page source %r loaded from %s "
                 "(%d endpoints)", label, src, len(src_endpoints))
    return label, src_history, src_endpoints


def validate_endpoint_webhooks(endpoints):
    """Reject bad per-endpoint `webhook` entries at config load.

    An endpoint may name its own webhook URL, so its alerts go to a
    different channel than the global `webhook_url` (see
    endpoint_webhooks). A bad value is rejected here rather than
    failing every alert — the monitor refuses to start with a
    webhook it can never deliver to. Raises ValueError with a
    clear message.
    """
    for ep in endpoints:
        url = ep.get("webhook")
        if url is None:
            continue
        if (not isinstance(url, str) or not url
                or not url.startswith(("http://", "https://"))):
            raise ValueError(
                f"endpoint {ep['name']!r}: webhook must be an "
                f"http(s) URL, not {url!r}")


def validate_check_intervals(endpoints):
    """Reject bad `check_interval_s` entries at config load.

    An endpoint may define its own check interval in seconds: it
    is checked on its own cadence instead of the global
    `interval_seconds`. Any endpoint type can set one, and the
    value must be a positive number (an integer or a float, not a
    bool, not a string). Raises ValueError with a clear message;
    the monitor refuses to start rather than run with an interval
    it can never schedule.
    """
    for ep in endpoints:
        value = ep.get("check_interval_s")
        if value is None:
            continue
        if (isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0):
            raise ValueError(
                f"endpoint {ep['name']!r}: check_interval_s must be a "
                f"positive number of seconds, not {value!r}")


def validate_retry_settings(cfg, endpoints):
    """Reject bad `retry_on_failure` entries at config load.

    Retries are on by default: a failed check is immediately retried
    once, and only a second consecutive failure counts as DOWN. The
    setting can be disabled globally (`retry_on_failure` in the
    config) or per endpoint (the endpoint's own `retry_on_failure`
    wins over the global one). Either value must be a bool, not a
    string or a number — a truthy-looking "false" must not silently
    enable retries. Raises ValueError with a clear message; the
    monitor refuses to start rather than run with a retry setting
    it cannot judge.
    """
    global_value = cfg.get("retry_on_failure")
    if global_value is not None and not isinstance(global_value, bool):
        raise ValueError(
            f"retry_on_failure must be true or false, not "
            f"{global_value!r}")
    for ep in endpoints:
        value = ep.get("retry_on_failure")
        if value is not None and not isinstance(value, bool):
            raise ValueError(
                f"endpoint {ep['name']!r}: retry_on_failure must be "
                f"true or false, not {value!r}")


def retry_enabled(cfg, ep, args=None):
    """Whether a failed check of `ep` is retried once before it counts.

    Default on. The --no-retry-on-failure flag disables retries for
    every endpoint; otherwise a per-endpoint `retry_on_failure`
    wins over the global `retry_on_failure` in the config, which
    defaults to True when absent.
    """
    if getattr(args, "no_retry_on_failure", False):
        return False
    value = ep.get("retry_on_failure")
    if value is not None:
        return value
    return cfg.get("retry_on_failure", True)


def endpoint_interval(cfg, ep):
    """Seconds between checks of one endpoint: its own
    `check_interval_s` when set, else the global `interval_seconds`
    (default 30)."""
    value = ep.get("check_interval_s")
    if value is not None:
        return value
    return cfg.get("interval_seconds", 30)


def parse_check_interval(spec):
    """Parse a --check-interval NAME=SECONDS spec.

    The name is an endpoint in the config; the interval is how many
    seconds pass between that endpoint's checks. A spec without '=',
    an empty name, or an interval that is not a positive number is
    rejected. Raises ValueError with a clear message.
    """
    if "=" not in spec:
        raise ValueError(
            f"--check-interval needs NAME=SECONDS, got {spec!r}")
    name, interval = spec.split("=", 1)
    name = name.strip()
    interval = interval.strip()
    if not name or not interval:
        raise ValueError(
            f"--check-interval needs a non-empty name and interval, "
            f"got {spec!r}")
    try:
        value = float(interval)
    except ValueError:
        raise ValueError(
            f"--check-interval interval must be a number, got "
            f"{interval!r}") from None
    if value <= 0:
        raise ValueError(
            f"--check-interval interval must be a positive number of "
            f"seconds, got {interval!r}")
    return name, value


def apply_check_interval_overrides(cfg, args):
    """Apply --check-interval flags to the config's endpoints.

    Each flag sets (or replaces) the endpoint's `check_interval_s` —
    the flag wins over the config file, like --flap-threshold and
    --webhook-url do for their settings. A flag naming an endpoint
    that is not in the config is rejected, so a typo does not
    silently do nothing. `args` may be a namespace without the
    attribute (tests build their own) — then there is nothing to
    apply. Raises ValueError with a clear message.
    """
    specs = getattr(args, "check_interval", None) or []
    if not specs:
        return
    by_name = {ep["name"]: ep for ep in cfg.get("endpoints", [])}
    for spec in specs:
        name, value = parse_check_interval(spec)
        if name not in by_name:
            raise ValueError(
                f"--check-interval names unknown endpoint {name!r}")
        by_name[name]["check_interval_s"] = value


def validate_expect_status(endpoints):
    """Reject bad `expect_status` entries at config load.

    An endpoint may assert on the exact HTTP status it expects
    (e.g. 204 for a no-content health endpoint). Only http
    endpoints can set it, and the value must be an integer HTTP
    status code (100-599). Raises ValueError with a clear message;
    the monitor refuses to start rather than run with a check it
    can never pass.
    """
    for ep in endpoints:
        value = ep.get("expect_status")
        if value is None:
            continue
        if ep["type"] != "http":
            raise ValueError(
                f"endpoint {ep['name']!r}: expect_status only applies "
                f"to http endpoints")
        if (isinstance(value, bool) or not isinstance(value, int)
                or not 100 <= value <= 599):
            raise ValueError(
                f"endpoint {ep['name']!r}: expect_status must be an "
                f"HTTP status code (100-599), not {value!r}")


def validate_latency_budgets(endpoints):
    """Reject bad `max_latency_ms` entries at config load.

    An endpoint may declare a latency budget in milliseconds: a
    check that answers slower than the budget is DOWN, like any
    other failed check. Any endpoint type can set one, and the
    value must be a positive number (an integer or a float, not a
    bool, not a string). Raises ValueError with a clear message;
    the monitor refuses to start rather than run with a budget it
    can never judge.
    """
    for ep in endpoints:
        value = ep.get("max_latency_ms")
        if value is None:
            continue
        if (isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0):
            raise ValueError(
                f"endpoint {ep['name']!r}: max_latency_ms must be a "
                f"positive number of milliseconds, not {value!r}")


def parse_max_latency(spec):
    """Parse a --max-latency NAME=MS spec.

    The name is an endpoint in the config; the budget is the
    maximum latency in milliseconds that endpoint may answer
    within before the check counts as failed. A spec without '=',
    an empty name, or a budget that is not a positive number is
    rejected. Raises ValueError with a clear message.
    """
    if "=" not in spec:
        raise ValueError(
            f"--max-latency needs NAME=MS, got {spec!r}")
    name, budget = spec.split("=", 1)
    name = name.strip()
    budget = budget.strip()
    if not name or not budget:
        raise ValueError(
            f"--max-latency needs a non-empty name and budget, "
            f"got {spec!r}")
    try:
        value = float(budget)
    except ValueError:
        raise ValueError(
            f"--max-latency budget must be a number, got "
            f"{budget!r}") from None
    if value <= 0:
        raise ValueError(
            f"--max-latency budget must be a positive number of "
            f"milliseconds, got {budget!r}")
    return name, value


def apply_max_latency_overrides(cfg, args):
    """Apply --max-latency flags to the config's endpoints.

    Each flag sets (or replaces) the endpoint's `max_latency_ms` —
    the flag wins over the config file, like --flap-threshold and
    --webhook-url do for their settings. A flag naming an endpoint
    that is not in the config is rejected, so a typo does not
    silently do nothing. `args` may be a namespace without the
    attribute (tests build their own) — then there is nothing to
    apply. Raises ValueError with a clear message.
    """
    specs = getattr(args, "max_latency", None) or []
    if not specs:
        return
    by_name = {ep["name"]: ep for ep in cfg.get("endpoints", [])}
    for spec in specs:
        name, value = parse_max_latency(spec)
        if name not in by_name:
            raise ValueError(
                f"--max-latency names unknown endpoint {name!r}")
        by_name[name]["max_latency_ms"] = value


def parse_expect_status(spec):
    """Parse a --expect-status NAME=STATUS spec.

    The name is an endpoint in the config; the status is the exact
    HTTP status code that endpoint must return to count as up.
    A spec without '=', an empty name, or a status that is not an
    integer 100-599 is rejected. Raises ValueError with a clear
    message.
    """
    if "=" not in spec:
        raise ValueError(
            f"--expect-status needs NAME=STATUS, got {spec!r}")
    name, status = spec.split("=", 1)
    name = name.strip()
    status = status.strip()
    if not name or not status:
        raise ValueError(
            f"--expect-status needs a non-empty name and status, "
            f"got {spec!r}")
    try:
        code = int(status)
    except ValueError:
        raise ValueError(
            f"--expect-status status must be an integer, got "
            f"{status!r}") from None
    if str(code) != status or not 100 <= code <= 599:
        raise ValueError(
            f"--expect-status status must be an HTTP status code "
            f"(100-599), got {status!r}")
    return name, code


def apply_expect_status_overrides(cfg, args):
    """Apply --expect-status flags to the config's endpoints.

    Each flag sets (or replaces) the endpoint's `expect_status` —
    the flag wins over the config file, like --flap-threshold and
    --webhook-url do for their settings. A flag naming an endpoint
    that is not in the config is rejected, so a typo does not
    silently do nothing. `args` may be a namespace without the
    attribute (tests build their own) — then there is nothing to
    apply. Raises ValueError with a clear message.
    """
    specs = getattr(args, "expect_status", None) or []
    if not specs:
        return
    by_name = {ep["name"]: ep for ep in cfg.get("endpoints", [])}
    for spec in specs:
        name, code = parse_expect_status(spec)
        if name not in by_name:
            raise ValueError(
                f"--expect-status names unknown endpoint {name!r}")
        by_name[name]["expect_status"] = code


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


def parse_status_page_source(spec):
    """Parse a --status-page-source LABEL=PATH spec.

    The label names the second source in the generated status page,
    so its endpoints are attributed to it; the value is another
    healthcheck instance's persisted history file, or an http(s)
    URL for a remote instance, whose GET /api/status is scraped
    instead (see fetch_remote_status). A spec without '=' is
    rejected — a bare path has no label to attribute its endpoints
    to. Raises ValueError with a clear message.
    """
    if "=" not in spec:
        raise ValueError(
            f"--status-page-source needs LABEL=PATH, got {spec!r}")
    label, path = spec.split("=", 1)
    label = label.strip()
    path = path.strip()
    if not label or not path:
        raise ValueError(
            f"--status-page-source needs a non-empty label and path, "
            f"got {spec!r}")
    return label, path


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


def endpoint_webhooks(cfg):
    """Per-endpoint webhook URLs from the config: {name: url}.

    An endpoint's own `webhook` overrides the global `webhook_url`
    for that endpoint's alerts — state-change alerts and its slice
    of the digest go to its own channel. Endpoints without one fall
    back to the global URL.
    """
    return {ep["name"]: ep["webhook"]
            for ep in cfg.get("endpoints", [])
            if ep.get("webhook")}


def make_alerter(cfg, args):
    """A WebhookAlerter for the configured URL, or None if none is set.

    When `alert_digest` is configured, the alerter is wrapped in a
    DigestAlerter that batches confirmed changes into a periodic
    digest as well as (or instead of) firing per-change webhooks.
    An endpoint's own `webhook` overrides the global URL for that
    endpoint, in both the per-change alerts and the digest — so an
    alerter exists whenever any endpoint has its own webhook, even
    with no global URL set.
    """
    url = webhook_url(cfg, args) if args is not None else cfg.get("webhook_url")
    routes = endpoint_webhooks(cfg)
    inner = WebhookAlerter(url, routes=routes) if url or routes else None
    digest_cfg = cfg.get("alert_digest")
    if digest_cfg is not None:
        # Pass the raw block: DigestAlerter validates it itself. (Do
        # not pre-validate here — the normalized dict carries an
        # "smtp" key that validate_digest_config would reject as
        # unknown on a second pass.)
        return DigestAlerter(digest_cfg, inner=inner, routes=routes)
    return inner


def make_sla_alerter(cfg, args, records=None):
    """An SlaBreachAlerter for the configured webhook, or None if
    there is nowhere to send one.

    The same webhook routing as state-change alerts: the global
    `webhook_url` (or --webhook-url) plus each endpoint's own
    `webhook` URL. With no URL anywhere there is no alerter — the
    breach still shows on the dashboard and in GET /api/status,
    it just cannot be POSTed anywhere.
    """
    url = webhook_url(cfg, args) if args is not None else cfg.get("webhook_url")
    routes = endpoint_webhooks(cfg)
    if not url and not routes:
        return None
    return SlaBreachAlerter(url, routes=routes, records=records)


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
              flap_detector=None, now=None, only=None, retry=None):
    """One check round over all endpoints, dependencies first.

    `only`, when given, is a set of endpoint names to check this
    round: the others are not touched at all — no check runs, no
    history is recorded — so the monitor loop can check each
    endpoint on its own `check_interval_s` cadence while the rest
    wait. A dependent that is due while its dependency is not due
    this round is still judged: `_dependency_state` falls back to
    the dependency's last recorded check, so a down dependency
    still skips it and a recovered one lets it run.

    `retry` decides whether a failed check is retried once before
    it counts: None (the default) retries every endpoint, a bool
    applies to all of them, and a callable is asked per endpoint
    (see `retry_enabled`). A first-attempt failure that recovers on
    the retry is recorded as UP with `retries=1` — a single blip
    is not downtime, never reaches the flap detector, and costs no
    uptime. Only when the retry fails too is the failure recorded,
    with `retries=1` and the retry's error. The retry is part of
    the same round, not a second check: one history entry is
    recorded either way, and the endpoint's next-due time is
    computed from the round, so a retry never resets the schedule.

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
    ordered = check_order(endpoints)
    if only is not None:
        ordered = [ep for ep in ordered if ep["name"] in only]
    for ep in ordered:
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
        result = check_endpoint_with_retry(
            ep, timeout=timeout,
            retry=True if retry is None else
            (retry(ep) if callable(retry) else bool(retry)))
        if dep is not None:
            result = dataclasses.replace(result, depends_on=dep)
        budget = ep.get("max_latency_ms")
        if (result.ok and budget is not None
                and result.latency_ms > budget):
            # The endpoint answered, but too slowly: a latency
            # budget breach is a failed check, exactly like a bad
            # status or a failed body assertion — it goes through
            # the same flap rules (unconfirmed below the threshold,
            # confirmed DOWN and alerting at it) and the same
            # uptime penalty. The latency is still recorded, so the
            # dashboard shows how slow it was.
            result = dataclasses.replace(
                result, ok=False,
                error=f"latency {result.latency_ms:.1f} ms exceeds "
                      f"budget {budget:g} ms")
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
            log.info("%s UP %.1f ms%s", result.name, result.latency_ms,
                     _retry_suffix(result))
        elif outcome.confirmed_down:
            detail = (f"HTTP {result.status_code}" if result.status_code
                      else (result.error or ""))
            log.info("%s DOWN %.1f ms %s%s", result.name,
                     result.latency_ms, detail, _retry_suffix(result))
        else:
            detail = (f"HTTP {result.status_code}" if result.status_code
                      else (result.error or ""))
            log.info("%s DOWN (unconfirmed %d/%d) %.1f ms %s%s", result.name,
                     outcome.consecutive_failures,
                     flap_detector.threshold, result.latency_ms, detail,
                     _retry_suffix(result))
    return results


def select_check_targets(endpoints, name):
    """Endpoints a --check run covers: all of them, or one by name.

    A named endpoint brings its transitive dependencies along —
    without them a dependent could never be judged (its dependency
    would be UNKNOWN and it would always read SKIPPED). The result
    is in dependency order. Raises ValueError naming an unknown
    endpoint, so a typo does not silently check nothing.
    """
    if name is None:
        return list(endpoints)
    by_name = {ep["name"]: ep for ep in endpoints}
    if name not in by_name:
        raise ValueError(f"--check names unknown endpoint {name!r}")
    selected, seen = [], set()

    def add(ep_name):
        if ep_name in seen:
            return
        seen.add(ep_name)
        dep = by_name[ep_name].get("depends_on")
        if dep is not None:
            add(dep)
        selected.append(by_name[ep_name])

    add(name)
    return selected


def check_verdict(result):
    """One-word verdict for a dry-run result: UP, DOWN, SKIPPED, or
    MAINTENANCE. A failed check is DOWN; a check that never ran is
    SKIPPED (dependency not up) or MAINTENANCE (in a window)."""
    if result.ok:
        return "UP"
    if result.maintenance:
        return "MAINTENANCE"
    if result.skipped:
        return "SKIPPED"
    return "DOWN"


def _retry_suffix(result):
    """Readable retry note for a check result: "" when the first
    attempt decided it, " (retried once)" when the retry did."""
    if not getattr(result, "retries", 0):
        return ""
    if result.retries == 1:
        return " (retried once)"
    return f" (retried {result.retries} times)"


def format_check_line(result):
    """One printable line for a dry-run result: verdict, status or
    the rule that fired, and latency.

    The error text already names the rule that failed the check —
    a latency budget breach, an expect_status mismatch, a body
    assertion, a dependency skip, a maintenance window — so the
    operator sees why, not just that it is down. When the check
    was retried, the line says so: a recovered endpoint reads UP
    with "(retried once)", and a still-down one reads DOWN with
    the same note, so the retry is visible in the dry run too.
    """
    verdict = check_verdict(result)
    if result.ok:
        detail = (f"HTTP {result.status_code}"
                  if result.status_code is not None else "")
        return (f"{result.name}: UP"
                + (f" {detail}" if detail else "")
                + f" {result.latency_ms:.1f} ms"
                + _retry_suffix(result)).strip()
    if verdict == "DOWN":
        if result.status_code is not None:
            detail = f"HTTP {result.status_code}"
            if result.error and result.error != f"HTTP {result.status_code}":
                detail += f" ({result.error})"
        else:
            detail = result.error or ""
        return (f"{result.name}: DOWN"
                + (f" {detail}" if detail else "")
                + f" {result.latency_ms:.1f} ms"
                + _retry_suffix(result)).strip()
    return f"{result.name}: {verdict} {result.error or ''}".strip()


def dry_run_check(endpoints, name=None, retry=None):
    """One check pass over the selected endpoints, printed, nothing
    else: no history is written, no alert fires, no dashboard
    starts. Returns (results, exit_code): exit 0 when every checked
    endpoint is UP or in maintenance, 1 when any is DOWN (a SKIPPED
    endpoint's dependency is in the results too, so a down
    dependency already fails the run on its own line).

    A dry run is a single pass, so the flap threshold does not
    apply: one failed check is DOWN here, where the monitor would
    call the same failure unconfirmed until the threshold's
    consecutive failures. The retry rule does apply, like in the
    monitor: a first-attempt failure is retried once, and the line
    says so — a recovered endpoint reads UP "(retried once)" — so
    the dry run judges the same outcome the monitor would record.
    `retry` is passed through to `check_all` (None retries every
    endpoint; a bool or per-endpoint callable overrides that).
    The check itself — status, body and
    latency-budget rules, dependencies, maintenance windows — is
    the same code the monitor runs.
    """
    targets = select_check_targets(endpoints, name)
    history = History()
    results = check_all(targets, history, alerter=None,
                        flap_detector=FlapDetector(1), retry=retry)
    for result in results.values():
        print(format_check_line(result))
    counts = {"UP": 0, "DOWN": 0, "SKIPPED": 0, "MAINTENANCE": 0}
    for result in results.values():
        counts[check_verdict(result)] += 1
    parts = [f"{counts[verdict]} {verdict.lower()}"
             for verdict in ("UP", "DOWN", "SKIPPED", "MAINTENANCE")
             if counts[verdict]]
    print(f"{len(results)} endpoint(s) checked: "
          f"{', '.join(parts) if parts else 'none'}")
    return results, 1 if counts["DOWN"] else 0


def monitor_loop(cfg, history, path, args, stop, config_path=None,
                 reload_requested=None, max_rounds=None,
                 live_endpoints=None, live_sources=None, sla_alerts=None):
    """Check rounds until `stop` is set; returns the config in effect
    when the loop ends.

    When `reload_requested` is set (by the SIGHUP handler installed
    in the main thread) and `config_path` is given, the config file
    is reloaded before the next round and the new endpoints,
    interval, webhook, and flap threshold take effect immediately.
    A reload that fails validation keeps the old config — see
    `reload_config`. Flap state resets on a reload: the new detector
    has no memory of the failures counted under the old config, so
    an endpoint mid-outage needs the threshold's failures again
    before it re-alerts. History is untouched by a reload, and the
    persisted `history_file` is not re-read: changing it needs a
    restart. `live_endpoints`, when given, is a list the loop keeps
    in sync with the current endpoints (slice assignment, so the
    dashboard server holding the same list sees reloads too).
    `live_sources` is the same for the merged status page sources,
    so the dashboard and GET /api/status follow a reload too.
    `max_rounds` caps the loop for tests; production runs pass None
    and stop only on Ctrl-C. The digest window resets on a reload,
    like flap state: the new alerter starts a fresh window.

    Each endpoint is checked on its own cadence: an endpoint with
    `check_interval_s` is checked that often, and one without it
    falls back to the global `interval_seconds` (see
    `endpoint_interval`). The loop sleeps until the next endpoint
    is due, so a fast endpoint does not wait on a slow one and a
    slow one is not checked every round — a round here is one pass
    over whichever endpoints are due, and `max_rounds` counts
    those passes. A reload resets every endpoint to due, so new
    endpoints (and new intervals) are picked up on the next pass.

    After every round the month's SLA report is checked for
    breaches: an endpoint below its `sla_target` fires one webhook
    alert for the month (see `SlaBreachAlerter`), so a breach is
    paged while the month is still running, not only when someone
    prints the report. `sla_alerts`, when given, is a dict shared
    with the dashboard server that records when each breach was
    alerted, so the dashboard and GET /api/status show it.
    """
    state_cfg = cfg
    endpoints = list(cfg.get("endpoints", []))
    interval = cfg.get("interval_seconds", 30)
    alerter = make_alerter(cfg, args)
    sla_alerter = make_sla_alerter(cfg, args, records=sla_alerts)
    # One detector for the life of a config: the consecutive failure
    # count must survive across rounds.
    detector = FlapDetector(flap_threshold(cfg, args))
    if live_endpoints is not None:
        live_endpoints[:] = endpoints
    if live_sources is not None:
        live_sources[:] = merged_status_page_sources(cfg, args)
    # Epoch seconds each endpoint is next due a check; 0 means due
    # immediately, so the first pass checks everything.
    next_due = {ep["name"]: 0.0 for ep in endpoints}
    rounds = 0
    while not stop.is_set():
        if reload_requested is not None and reload_requested.is_set():
            reload_requested.clear()
            if config_path is not None:
                new_cfg = reload_config(config_path, args, state_cfg)
                if new_cfg is not state_cfg:
                    try:
                        apply_expect_status_overrides(new_cfg, args)
                        apply_max_latency_overrides(new_cfg, args)
                        apply_check_interval_overrides(new_cfg, args)
                    except ValueError as exc:
                        # A flag naming an endpoint the reloaded file
                        # dropped: keep the running config, like a
                        # reload that fails validation.
                        log.warning(
                            "config reload from %s failed (%s); keeping "
                            "the current config", config_path, exc)
                        new_cfg = state_cfg
                if new_cfg is not state_cfg:
                    old_names = {ep["name"] for ep in endpoints}
                    endpoints = list(new_cfg.get("endpoints", []))
                    new_names = {ep["name"] for ep in endpoints}
                    interval = new_cfg.get("interval_seconds", 30)
                    alerter = make_alerter(new_cfg, args)
                    sla_alerter = make_sla_alerter(
                        new_cfg, args, records=sla_alerts)
                    detector = FlapDetector(flap_threshold(new_cfg, args))
                    state_cfg = new_cfg
                    # New endpoints and new intervals apply from the
                    # next pass: everything is due right away.
                    next_due = {ep["name"]: 0.0 for ep in endpoints}
                    if live_endpoints is not None:
                        live_endpoints[:] = endpoints
                    if live_sources is not None:
                        live_sources[:] = merged_status_page_sources(
                            new_cfg, args)
                    log.info(
                        "config reloaded: %d endpoint(s), added %s, "
                        "removed %s, checking every %ds",
                        len(endpoints),
                        sorted(new_names - old_names) or "none",
                        sorted(old_names - new_names) or "none",
                        interval)
        now = time.time()
        due = {ep["name"] for ep in endpoints
               if next_due.get(ep["name"], 0.0) <= now}
        if due:
            check_all(endpoints, history, alerter=alerter,
                      flap_detector=detector, only=due,
                      retry=lambda ep: retry_enabled(state_cfg, ep, args))
            checked_at = time.time()
            for name in due:
                ep = next(ep for ep in endpoints
                          if ep["name"] == name)
                next_due[name] = (checked_at
                                  + endpoint_interval(state_cfg, ep))
            if isinstance(alerter, DigestAlerter):
                # The digest window advances on its own interval; an
                # empty window sends nothing. When no SMTP host is
                # configured, the rendered email body goes to the log —
                # that is where an operator picks it up (or pipes it to
                # sendmail); when SMTP delivery ran, success or failure
                # is already logged by the sender.
                current_down = [ep["name"] for ep in endpoints
                                if detector.is_down(ep["name"])]
                sent = alerter.maybe_send(current_down=current_down)
                if (sent is not None and sent["email"]
                        and sent["email_ok"] is None):
                    log.info("alert digest email:\n%s", sent["email"])
            if sla_alerter is not None:
                # One webhook per breached endpoint per month; a
                # month that is not breaching sends nothing.
                sla_alerter.maybe_alert(history, endpoints)
            save_history(history, path)
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                break
        # Sleep until the next endpoint is due; with no endpoints
        # at all, wake on the global interval so a reload that
        # adds one is picked up promptly.
        if endpoints:
            wait = max(0.0, min(next_due.get(ep["name"], 0.0)
                                for ep in endpoints) - time.time())
        else:
            wait = interval
        stop.wait(wait)
    return state_cfg


def run_forever(cfg, host, port, args=None, config_path=None):
    path = history_path(cfg, args) if args is not None else cfg.get("history_file")
    history = load_history(path, maxlen=cfg.get("history", 100))
    if path and history.names():
        log.info("loaded history for %d endpoint(s) from %s",
                 len(history.names()), path)
    stop = threading.Event()
    reload_requested = threading.Event()
    if config_path is not None:
        # Installed here, on the main thread: signal.signal only
        # works there, and the checker loop runs in its own thread.
        install_reload_handler(reload_requested)
    # One list shared with the dashboard server, kept in sync by the
    # checker loop, so a SIGHUP reload changes what the dashboard
    # shows too.
    live_endpoints = list(cfg.get("endpoints", []))
    # The same for the merged status page sources: the dashboard and
    # GET /api/status list them, and a reload updates the list.
    live_sources = merged_status_page_sources(cfg, args)
    # Breach alerts fired this month, shared with the dashboard
    # server so it can show when each breach was alerted.
    sla_alerts = {}
    thread = threading.Thread(
        target=monitor_loop,
        args=(cfg, history, path, args, stop),
        kwargs={"config_path": config_path,
                "reload_requested": reload_requested,
                "live_endpoints": live_endpoints,
                "live_sources": live_sources,
                "sla_alerts": sla_alerts},
        name="checker", daemon=True)
    thread.start()
    log.info("dashboard at http://%s:%d/ — checking %d endpoint(s) every %ds "
             "(send SIGHUP to reload the config)",
             host, port, len(live_endpoints),
             cfg.get("interval_seconds", 30))
    try:
        serve(history, live_endpoints, host=host, port=port,
              status_page_sources=live_sources, sla_alerts=sla_alerts)
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
    parser.add_argument("--check", nargs="?", const="__all__",
                        default=None, metavar="NAME",
                        help="dry run: validate the config, check every "
                             "endpoint once (or just the named one, with "
                             "its dependencies) and exit with 0 when all "
                             "checked endpoints are up, 1 when any is "
                             "down — no history is written, no alert "
                             "fires, no dashboard starts")
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
    parser.add_argument("--expect-status", default=None,
                        action="append", metavar="NAME=STATUS",
                        help="require this exact HTTP status code from "
                             "the named endpoint (overrides the config "
                             "file's expect_status for that endpoint; "
                             "repeatable)")
    parser.add_argument("--max-latency", default=None,
                        action="append", metavar="NAME=MS",
                        help="fail the named endpoint's check when its "
                             "latency exceeds MS milliseconds (overrides "
                             "the config file's max_latency_ms for that "
                             "endpoint; repeatable)")
    parser.add_argument("--check-interval", default=None,
                        action="append", metavar="NAME=SECONDS",
                        help="check the named endpoint every SECONDS "
                             "seconds instead of the global "
                             "interval_seconds (overrides the config "
                             "file's check_interval_s for that "
                             "endpoint; repeatable)")
    parser.add_argument("--no-retry-on-failure", action="store_true",
                        help="do not retry a failed check once before "
                             "counting it as down (retries are on by "
                             "default; a global retry_on_failure or a "
                             "per-endpoint retry_on_failure in the "
                             "config file disables them too)")
    parser.add_argument("--status-page", default=None, metavar="PATH",
                        help="write a self-contained static HTML status "
                             "page from the persisted check history to "
                             "PATH and exit (combines with --once to "
                             "check first, then render)")
    parser.add_argument("--status-page-source", default=None,
                        action="append", metavar="LABEL=PATH",
                        help="merge another healthcheck instance's "
                             "persisted history file (or its GET "
                             "/api/status, when the value is an "
                             "http(s) URL) into the static status page "
                             "as an extra source, labelled LABEL in "
                             "the page (repeatable; needs "
                             "--status-page). Merges with the config "
                             "file's status_page_sources; a flag with "
                             "the same label replaces that entry")
    parser.add_argument("--sla-report", default=None, nargs="?",
                        const="current", metavar="YYYY-MM",
                        help="print the monthly SLA report (uptime per "
                             "endpoint vs its sla_target, with breaches) "
                             "for the given month and exit; with no month "
                             "given, the current month is used")
    parser.add_argument("--sla-format", default="text",
                        choices=["text", "csv", "json"],
                        help="format for --sla-report: plain text "
                             "(default), CSV, or JSON — the CSV and JSON "
                             "exports add per-endpoint incident counts, "
                             "downtime, and latency percentiles over the "
                             "month")
    parser.add_argument("--sla-out", default=None, metavar="PATH",
                        help="write the --sla-report output to PATH "
                             "instead of printing it")
    parser.add_argument("--sla-alert", default=None, nargs="?",
                        const="current", metavar="YYYY-MM",
                        help="fire a webhook alert for every endpoint "
                             "breaching its sla_target in the given "
                             "month and exit (one alert per endpoint "
                             "per month); with no month given, the "
                             "current month is used. Needs a webhook "
                             "URL in the config (or --webhook-url) or "
                             "a per-endpoint webhook — with none "
                             "configured, nothing is sent")
    args = parser.parse_args(argv)
    if args.status_page_source and not args.status_page:
        parser.error("--status-page-source needs --status-page")
    if (args.sla_format != "text" or args.sla_out) and not args.sla_report:
        parser.error("--sla-format and --sla-out need --sla-report")
    cfg = load_config(args.config)
    try:
        apply_expect_status_overrides(cfg, args)
        apply_max_latency_overrides(cfg, args)
        apply_check_interval_overrides(cfg, args)
    except ValueError as exc:
        parser.error(str(exc))
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
        if args.sla_format == "csv":
            output = render_sla_report_csv(report, year, month)
        elif args.sla_format == "json":
            output = render_sla_report_json(report, year, month)
        else:
            output = render_sla_report(report, year, month)
        if args.sla_out:
            with open(args.sla_out, "w", encoding="utf-8") as fh:
                fh.write(output)
        else:
            print(output, end="")
        return 0
    if args.sla_alert:
        if args.sla_alert == "current":
            now = time.localtime()
            year, month = now.tm_year, now.tm_mon
        else:
            try:
                year, month = parse_month(args.sla_alert)
            except ValueError as exc:
                parser.error(str(exc))
        path = history_path(cfg, args)
        history = load_history(path, maxlen=cfg.get("history", 100))
        sla_alerter = make_sla_alerter(cfg, args)
        if sla_alerter is None:
            print("No webhook configured; no SLA breach alert sent.")
            return 0
        sent = sla_alerter.maybe_alert(
            history, cfg.get("endpoints", []), year, month)
        if sent:
            for payload in sent:
                print(f"SLA breach alert sent for {payload['endpoint']}: "
                      f"{payload['uptime_pct']:.1f}% vs target "
                      f"{payload['target']:.1f}% "
                      f"(short {payload['shortfall_pct']:.1f}pp) "
                      f"in {payload['month']}")
        else:
            print("No SLA breaches to alert on.")
        return 0
    if args.check is not None:
        name = None if args.check == "__all__" else args.check
        try:
            _, code = dry_run_check(
                cfg.get("endpoints", []), name,
                retry=lambda ep: retry_enabled(cfg, ep, args))
        except ValueError as exc:
            parser.error(str(exc))
        return code
    if args.once:
        path = history_path(cfg, args)
        history = load_history(path, maxlen=cfg.get("history", 100))
        check_all(cfg.get("endpoints", []), history,
                  alerter=make_alerter(cfg, args),
                  flap_detector=FlapDetector(flap_threshold(cfg, args)),
                  retry=lambda ep: retry_enabled(cfg, ep, args))
        save_history(history, path)
    if args.status_page:
        if history is None:
            path = history_path(cfg, args)
            history = load_history(path, maxlen=cfg.get("history", 100))
        try:
            sources = merged_status_page_sources(cfg, args)
        except ValueError as exc:
            parser.error(str(exc))
        extra_sources = [
            load_status_page_source(
                src["label"], src.get("path") or src.get("url"), cfg)
            for src in sources]
        write_status_page(history, cfg.get("endpoints", []),
                          args.status_page,
                          extra_sources=extra_sources or None)
        log.info("status page written to %s", args.status_page)
        return 0
    if args.once:
        return 0
    run_forever(cfg, args.host, args.port, args, config_path=args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
