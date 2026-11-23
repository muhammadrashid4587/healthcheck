"""healthcheck — a tiny self-hosted service health monitor.

Pure Python 3 standard library. Checks HTTP and TCP endpoints on a
schedule, tracks uptime per endpoint, and serves a small status
dashboard. Alerts are log lines, plus an optional webhook that fires
when an endpoint changes state, or a periodic digest that batches
the changes into one summary (webhook and/or a rendered email body).
"""

__version__ = "0.27.0"

from .checker import (CheckResult, check_endpoint, check_http, check_tcp,
                      maintenance_result)
from .cli import (apply_check_interval_overrides, apply_max_latency_overrides,
                  dry_run_check, endpoint_interval, format_check_line,
                  install_reload_handler, load_status_page_source,
                  merged_status_page_sources, monitor_loop,
                  parse_check_interval, parse_max_latency, reload_config,
                  select_check_targets, validate_check_intervals,
                  validate_latency_budgets, validate_status_page_sources)
from .digest import (DEFAULT_DIGEST_INTERVAL_SECONDS, DigestAlerter,
                     build_digest, render_digest_email,
                     resolve_smtp_config, send_digest_email,
                     validate_digest_config)
from .flap import DEFAULT_THRESHOLD, FlapDetector, FlapOutcome
from .maintenance import (active_window, in_maintenance, summarize_windows,
                          validate_maintenance_windows)
from .remote import ApiStatusHistory, fetch_remote_status
from .server import latency_sparkline, render_metrics, status_snapshot
from .sla import (breaches, monthly_uptime, parse_month, render_sla_report,
                  sla_report, validate_sla_targets)
from .statuspage import endpoint_status, recent_incidents, render_status_page, write_status_page
from .store import History

__all__ = [
    "__version__",
    "ApiStatusHistory",
    "CheckResult",
    "DEFAULT_DIGEST_INTERVAL_SECONDS",
    "DigestAlerter",
    "FlapDetector",
    "FlapOutcome",
    "DEFAULT_THRESHOLD",
    "History",
    "active_window",
    "apply_check_interval_overrides",
    "apply_max_latency_overrides",
    "breaches",
    "build_digest",
    "check_endpoint",
    "check_http",
    "check_tcp",
    "dry_run_check",
    "endpoint_interval",
    "endpoint_status",
    "fetch_remote_status",
    "format_check_line",
    "in_maintenance",
    "install_reload_handler",
    "load_status_page_source",
    "maintenance_result",
    "merged_status_page_sources",
    "monitor_loop",
    "monthly_uptime",
    "parse_check_interval",
    "parse_max_latency",
    "parse_month",
    "recent_incidents",
    "reload_config",
    "render_digest_email",
    "render_metrics",
    "render_sla_report",
    "render_status_page",
    "resolve_smtp_config",
    "latency_sparkline",
    "send_digest_email",
    "select_check_targets",
    "sla_report",
    "status_snapshot",
    "summarize_windows",
    "validate_check_intervals",
    "validate_digest_config",
    "validate_latency_budgets",
    "validate_sla_targets",
    "validate_status_page_sources",
    "validate_maintenance_windows",
    "write_status_page",
]
