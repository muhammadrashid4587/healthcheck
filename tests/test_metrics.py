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

    def test_incidents_total_counts_closed_and_ongoing(self):
        hist = History()
        base = 1_700_000_000.0
        # web: one closed incident (2 downs, recovery) and one
        # ongoing incident (1 down, no recovery yet).
        hist.record(_result("web", True, at=base))
        hist.record(_result("web", False, at=base + 1))
        hist.record(_result("web", False, at=base + 2))
        hist.record(_result("web", True, at=base + 3))
        hist.record(_result("web", False, at=base + 4))
        # db: one closed incident of 2 downs.
        hist.record(_result("db", False, at=base))
        hist.record(_result("db", False, at=base + 1))
        hist.record(_result("db", True, at=base + 2))
        m = _metrics(render_metrics(hist, self.endpoints + [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1",
             "port": 1}], now=base + 10))
        self.assertEqual(m[("healthcheck_incidents_total",
                            (("name", "web"),))], "2")
        self.assertEqual(m[("healthcheck_incidents_total",
                            (("name", "db"),))], "1")
        # An endpoint with no incidents still gets a 0 series.
        self.assertEqual(m[("healthcheck_incidents_total",
                            (("name", "fresh"),))], "0")

    def test_incidents_total_ignores_skips_unconfirmed_and_maintenance(self):
        hist = History()
        base = 1_700_000_000.0
        # An unconfirmed failure never starts an incident.
        hist.record(_result("app", True, at=base))
        hist.record(_result("app", False, at=base + 1, unconfirmed=True))
        hist.record(_result("app", True, at=base + 2))
        # A skip in the middle of a run of downs does not split it,
        # and a maintenance round does not end it either.
        hist.record(_result("svc", False, at=base))
        hist.record(_result("svc", False, at=base + 1, skipped=True,
                            depends_on="db"))
        hist.record(_result("svc", False, at=base + 2, maintenance=True))
        hist.record(_result("svc", True, at=base + 3))
        m = _metrics(render_metrics(hist, [
            {"name": "app", "type": "http", "url": "http://x"},
            {"name": "svc", "type": "tcp", "host": "127.0.0.1",
             "port": 1}], now=base + 10))
        self.assertEqual(m[("healthcheck_incidents_total",
                            (("name", "app"),))], "0")
        self.assertEqual(m[("healthcheck_incidents_total",
                            (("name", "svc"),))], "1")

    def test_incidents_total_agrees_with_incidents_snapshot(self):
        # With at most 5 incidents the counter is exactly the number
        # of incidents the /api/incidents snapshot lists, because
        # both read the same grouping.
        from healthcheck.server import incidents_snapshot
        hist = History()
        base = 1_700_000_000.0
        for i in range(3):
            hist.record(_result("web", False, at=base + 2 * i))
            hist.record(_result("web", True, at=base + 2 * i + 1))
        snap = incidents_snapshot(hist, self.endpoints[:1], now=base + 10)
        m = _metrics(render_metrics(hist, self.endpoints[:1],
                                    now=base + 10))
        self.assertEqual(
            m[("healthcheck_incidents_total", (("name", "web"),))],
            str(len(snap["endpoints"][0]["incidents"])))
        self.assertEqual(len(snap["endpoints"][0]["incidents"]), 3)

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
        # The live endpoint serves the incidents counter too: db's
        # single confirmed-down check is one ongoing incident,
        # web has none.
        self.assertEqual(m[("healthcheck_incidents_total",
                            (("name", "web"),))], "0")
        self.assertEqual(m[("healthcheck_incidents_total",
                            (("name", "db"),))], "1")


if __name__ == "__main__":
    unittest.main()
