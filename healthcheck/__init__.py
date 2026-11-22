"""healthcheck — a tiny self-hosted service health monitor.

Pure Python 3 standard library. Checks HTTP and TCP endpoints on a
schedule, tracks uptime per endpoint, and serves a small status
dashboard. Alerts are log lines, plus an optional webhook that fires
when an endpoint changes state.
"""

__version__ = "0.8.0"

from .checker import CheckResult, check_endpoint, check_http, check_tcp
from .flap import DEFAULT_THRESHOLD, FlapDetector, FlapOutcome
from .store import History

__all__ = [
    "__version__",
    "CheckResult",
    "FlapDetector",
    "FlapOutcome",
    "DEFAULT_THRESHOLD",
    "History",
    "check_endpoint",
    "check_http",
    "check_tcp",
]
