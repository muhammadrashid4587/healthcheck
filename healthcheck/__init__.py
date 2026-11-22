"""healthcheck — a tiny self-hosted service health monitor.

Pure Python 3 standard library. Checks HTTP and TCP endpoints on a
schedule, tracks uptime per endpoint, and serves a small status
dashboard. Alerts are just log lines.
"""

__version__ = "0.1.0"

from .checker import CheckResult, check_endpoint, check_http, check_tcp
from .store import History

__all__ = [
    "__version__",
    "CheckResult",
    "History",
    "check_endpoint",
    "check_http",
    "check_tcp",
]
