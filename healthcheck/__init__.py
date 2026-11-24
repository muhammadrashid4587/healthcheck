"""healthcheck — a tiny self-hosted service health monitor.

Pure Python 3 standard library. Checks HTTP and TCP endpoints on a
schedule, tracks uptime per endpoint, and serves a small status
dashboard. Alerts are log lines, plus an optional webhook that fires
when an endpoint changes state, or a periodic digest that batches
the changes into one summary (webhook and/or a rendered email body).
An endpoint that falls below its SLA target in the current month
fires one webhook alert for the month, and the breach shows on the
dashboard and in GET /api/status. An endpoint whose TLS certificate
enters its warning window fires one webhook alert for that
certificate, and the alert shows on the dashboard and in
GET /api/status too.
"""

__version__ = "0.49.0"

from .alerts import (CertExpiryAlerter, SlaBreachAlerter, WebhookAlerter,
                     alert_payload, cert_expiry_alert_payload,
                     post_alert, sla_breach_payload)

from .checker import (DEFAULT_CERT_EXPIRY_WARN_DAYS, CheckResult,
                      cert_expiring_soon, cert_expiry_days,
                      check_endpoint, check_endpoint_with_retry,
                      check_http, check_tcp, fetch_cert_expiry,
                      maintenance_result, parse_cert_not_after)
from .cli import (apply_check_interval_overrides, apply_max_latency_overrides,
                  dry_run_check, endpoint_interval, format_check_line,
                  install_reload_handler, load_status_page_source,
                  make_cert_alerter, make_sla_alerter,
                  merged_status_page_sources, monitor_loop,
                  parse_check_interval, parse_max_latency, reload_config,
                  retry_enabled, select_check_targets,
                  validate_cert_expiry_warnings, validate_check_intervals,
                  validate_latency_budgets, validate_retry_settings,
                  validate_status_page_sources)
from .digest import (DEFAULT_DIGEST_INTERVAL_SECONDS, DigestAlerter,
                     build_digest, render_digest_email,
                     resolve_smtp_config, send_digest_email,
                     validate_digest_config)
from .flap import DEFAULT_THRESHOLD, FlapDetector, FlapOutcome
from .incidents import (incidents_report, render_incidents_report,
                        render_incidents_report_csv,
                        render_incidents_report_json)
from .maintenance import (active_window, in_maintenance, summarize_windows,
                          validate_maintenance_windows)
from .remote import ApiStatusHistory, fetch_remote_status
from .server import (format_duration, incidents_snapshot,
                     latency_sparkline, render_metrics, sla_snapshot,
                     status_snapshot, uptime_snapshot)
from .sla import (breaches, monthly_detail, monthly_uptime, parse_month,
                  render_sla_report, render_sla_report_csv,
                  render_sla_report_json, sla_report,
                  sla_report_payload, validate_sla_targets)
from .statuspage import endpoint_status, recent_incidents, render_status_page, write_status_page
from .store import History

__all__ = [
    "__version__",
    "ApiStatusHistory",
    "CertExpiryAlerter",
    "CheckResult",
    "DEFAULT_CERT_EXPIRY_WARN_DAYS",
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
    "cert_expiring_soon",
    "cert_expiry_alert_payload",
    "cert_expiry_days",
    "check_endpoint",
    "check_endpoint_with_retry",
    "check_http",
    "check_tcp",
    "dry_run_check",
    "endpoint_interval",
    "endpoint_status",
    "fetch_cert_expiry",
    "fetch_remote_status",
    "format_check_line",
    "format_duration",
    "in_maintenance",
    "incidents_report",
    "incidents_snapshot",
    "install_reload_handler",
    "load_status_page_source",
    "maintenance_result",
    "make_cert_alerter",
    "make_sla_alerter",
    "merged_status_page_sources",
    "monitor_loop",
    "monthly_detail",
    "monthly_uptime",
    "parse_cert_not_after",
    "parse_check_interval",
    "parse_max_latency",
    "parse_month",
    "recent_incidents",
    "reload_config",
    "render_digest_email",
    "render_incidents_report",
    "render_incidents_report_csv",
    "render_incidents_report_json",
    "render_metrics",
    "render_sla_report",
    "render_sla_report_csv",
    "render_sla_report_json",
    "render_status_page",
    "resolve_smtp_config",
    "retry_enabled",
    "latency_sparkline",
    "send_digest_email",
    "select_check_targets",
    "sla_breach_payload",
    "SlaBreachAlerter",
    "sla_report",
    "sla_report_payload",
    "sla_snapshot",
    "status_snapshot",
    "summarize_windows",
    "uptime_snapshot",
    "validate_cert_expiry_warnings",
    "validate_check_intervals",
    "validate_digest_config",
    "validate_latency_budgets",
    "validate_retry_settings",
    "validate_sla_targets",
    "validate_status_page_sources",
    "validate_maintenance_windows",
    "write_status_page",
]
