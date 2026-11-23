"""healthcheck — a tiny self-hosted service health monitor.

Pure Python 3 standard library. Checks HTTP and TCP endpoints on a
schedule, tracks uptime per endpoint, and serves a small status
dashboard. Alerts are log lines, plus an optional webhook that fires
when an endpoint changes state.
"""

__version__ = "0.13.0"

from .checker import (CheckResult, check_endpoint, check_http, check_tcp,
                      maintenance_result)
from .flap import DEFAULT_THRESHOLD, FlapDetector, FlapOutcome
from .maintenance import (active_window, in_maintenance, summarize_windows,
                          validate_maintenance_windows)
from .server import latency_sparkline, status_snapshot
from .sla import (breaches, monthly_uptime, parse_month, render_sla_report,
                  sla_report, validate_sla_targets)
from .statuspage import endpoint_status, recent_incidents, render_status_page, write_status_page
from .store import History

__all__ = [
    "__version__",
    "CheckResult",
    "FlapDetector",
    "FlapOutcome",
    "DEFAULT_THRESHOLD",
    "History",
    "active_window",
    "breaches",
    "check_endpoint",
    "check_http",
    "check_tcp",
    "endpoint_status",
    "in_maintenance",
    "maintenance_result",
    "monthly_uptime",
    "parse_month",
    "recent_incidents",
    "render_sla_report",
    "render_status_page",
    "latency_sparkline",
    "sla_report",
    "status_snapshot",
    "summarize_windows",
    "validate_sla_targets",
    "validate_maintenance_windows",
    "write_status_page",
]
