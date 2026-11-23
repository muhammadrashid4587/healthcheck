"""Tests for the Prometheus /metrics export."""

import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from healthcheck.checker import CheckResult
from healthcheck.server import make_handler, render_metrics
from healthcheck.store import History


def _result(name, ok, latency=12.5, at=1_700_000_000.0, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=latency, checked_at=at,
                       **kwargs)


def _metrics(text):
    """Parse exposition text into {(metric, labels): value}."""
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        head, value = line.rsplit(" ", 1)
        if "{" in head:
            name, labels = head.split("{", 1)
            labels = labels.rstrip("}")
            pairs = {}
            for pair in labels.split(","):
                key, val = pair.split("=", 1)
                pairs[key] = val.strip('"')
            out[(name, tuple(sorted(pairs.items())))] = value
        else:
            out[(head, ())] = value
    return out


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.history.record(_result("web", True))
        self.history.record(_result("web", True, latency=20.0))
        self.history.record(_result("db", False))
        self.endpoints = [
            {"name": "web", "type": "http", "url": "http://example.com"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
        ]

    def test_up_gauge_reports_current_status(self):
        m = _metrics(render_metrics(self.history, self.endpoints,
                                    now=1_700_000_000.0))
        self.assertEqual(m[("healthcheck_up", (("name", "web"),))], "1")
        self.assertEqual(m[("healthcheck_up", (("name", "db"),))], "0")

    def test_up_gauge_omits_unknown_skipped_and_maintenance(self):
        hist = History()
        hist.record(_result("app", False, skipped=True, depends_on="db"))
        hist.record(_result("svc", False, maintenance=True))
        text = render_metrics(hist, [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1", "port": 1},
            {"name": "app", "type": "http", "url": "http://x",
             "depends_on": "db"},
            {"name": "svc", "type": "tcp", "host": "127.0.0.1", "port": 1},
        ], now=1_700_000_000.0)
        m = _metrics(text)
        self.assertNotIn(("healthcheck_up", (("name", "fresh"),)), m)
        self.assertNotIn(("healthcheck_up", (("name", "app"),)), m)
        self.assertNotIn(("healthcheck_up", (("name", "svc"),)), m)

    def test_up_gauge_unconfirmed_failure_still_up(self):
        hist = History()
        hist.record(_result("web", False, unconfirmed=True))
        m = _metrics(render_metrics(hist, self.endpoints[:1],
                                    now=1_700_000_000.0))
        self.assertEqual(m[("healthcheck_up", (("name", "web"),))], "1")

    def test_uptime_ratio_gauges(self):
        m = _metrics(render_metrics(self.history, self.endpoints,
                                    now=1_700_000_000.0))
        self.assertEqual(
            m[("healthcheck_uptime_ratio", (("name", "web"),))], "1.0")
        self.assertEqual(
            m[("healthcheck_uptime_ratio", (("name", "db"),))], "0.0")
        self.assertEqual(
            m[("healthcheck_uptime_ratio_24h", (("name", "web"),))], "1.0")
        self.assertEqual(
            m[("healthcheck_uptime_ratio_7d", (("name", "db"),))], "0.0")

    def test_uptime_ratio_omitted_without_data(self):
        text = render_metrics(self.history, self.endpoints + [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1", "port": 1}],
            now=1_700_000_000.0)
        m = _metrics(text)
        self.assertNotIn(
            ("healthcheck_uptime_ratio", (("name", "fresh"),)), m)
        self.assertNotIn(
            ("healthcheck_uptime_ratio_24h", (("name", "fresh"),)), m)

    def test_latency_summary_quantiles_sum_and_count(self):
        hist = History()
        for latency in (10.0, 20.0, 30.0, 40.0, 50.0,
                        60.0, 70.0, 80.0, 90.0, 100.0):
            hist.record(_result("web", True, latency=latency))
        hist.record(_result("db", False, latency=42.0))
        m = _metrics(render_metrics(hist, self.endpoints,
                                    now=1_700_000_000.0))
        web = lambda q: m[("healthcheck_latency_ms",
                           (("name", "web"), ("quantile", q)))]
        self.assertEqual(web("0.5"), "50.0")
        self.assertEqual(web("0.95"), "100.0")
        self.assertEqual(web("0.99"), "100.0")
        self.assertEqual(
            m[("healthcheck_latency_ms_sum", (("name", "web"),))], "550.0")
        self.assertEqual(
            m[("healthcheck_latency_ms_count", (("name", "web"),))], "10")
        # db has one failed check: a slow error response still counts.
        self.assertEqual(
            m[("healthcheck_latency_ms_count", (("name", "db"),))], "1")

    def test_latency_summary_omitted_without_data(self):
        text = render_metrics(self.history, self.endpoints + [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1", "port": 1}],
            now=1_700_000_000.0)
        m = _metrics(text)
        self.assertNotIn(
            ("healthcheck_latency_ms_count", (("name", "fresh"),)), m)

    def test_check_totals_by_result(self):
        m = _metrics(render_metrics(self.history, self.endpoints,
                                    now=1_700_000_000.0))
        self.assertEqual(m[("healthcheck_checks_total",
                            (("name", "web"), ("result", "success")))], "2")
        self.assertEqual(m[("healthcheck_checks_total",
                            (("name", "web"), ("result", "failure")))], "0")
        self.assertEqual(m[("healthcheck_checks_total",
                            (("name", "db"), ("result", "success")))], "0")
        self.assertEqual(m[("healthcheck_checks_total",
                            (("name", "db"), ("result", "failure")))], "1")

    def test_check_totals_exclude_skips_and_maintenance(self):
        hist = History()
        hist.record(_result("app", False, skipped=True, depends_on="db"))
        hist.record(_result("app", False, maintenance=True))
        hist.record(_result("app", True))
        m = _metrics(render_metrics(hist, [
            {"name": "app", "type": "http", "url": "http://x"}],
            now=1_700_000_000.0))
        self.assertEqual(m[("healthcheck_checks_total",
                            (("name", "app"), ("result", "success")))], "1")
        self.assertEqual(m[("healthcheck_checks_total",
                            (("name", "app"), ("result", "failure")))], "0")

    def test_label_values_are_escaped(self):
        hist = History()
        hist.record(_result('we"ird\\name', True))
        text = render_metrics(hist, [
            {"name": 'we"ird\\name', "type": "http", "url": "http://x"}],
            now=1_700_000_000.0)
        self.assertIn('healthcheck_up{name="we\\"ird\\\\name"} 1', text)

    def test_metrics_served_over_http(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.history, self.endpoints))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/metrics") as resp:
                body = resp.read().decode("utf-8")
                content_type = resp.headers.get("Content-Type")
                status = resp.status
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status, 200)
        self.assertIn("text/plain", content_type)
        self.assertIn("version=0.0.4", content_type)
        m = _metrics(body)
        self.assertEqual(m[("healthcheck_up", (("name", "web"),))], "1")
        self.assertEqual(m[("healthcheck_up", (("name", "db"),))], "0")


if __name__ == "__main__":
    unittest.main()
