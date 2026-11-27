"""Tests for the Prometheus /metrics export."""

import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from healthcheck.checker import CheckResult
from healthcheck.server import make_handler, render_metrics, uptime_snapshot
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

    def test_consecutive_failures_gauge(self):
        hist = History()
        hist.record(_result("web", True))
        hist.record(_result("web", False))
        hist.record(_result("web", False))
        hist.record(_result("db", False))
        m = _metrics(render_metrics(hist, self.endpoints + [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1",
             "port": 1}], now=1_700_000_000.0))
        self.assertEqual(m[("healthcheck_consecutive_failures",
                            (("name", "web"),))], "2")
        self.assertEqual(m[("healthcheck_consecutive_failures",
                            (("name", "db"),))], "1")
        # Always present, even at 0 — safe to alert on.
        self.assertEqual(m[("healthcheck_consecutive_failures",
                            (("name", "fresh"),))], "0")

    def test_consecutive_failures_gauge_resets_on_success(self):
        hist = History()
        hist.record(_result("web", False))
        hist.record(_result("web", False))
        hist.record(_result("web", True))
        m = _metrics(render_metrics(hist, self.endpoints[:1],
                                    now=1_700_000_000.0))
        self.assertEqual(m[("healthcheck_consecutive_failures",
                            (("name", "web"),))], "0")

    def test_consecutive_failures_gauge_agrees_with_api_status(self):
        # The gauge reads the same History.consecutive_failures
        # figure GET /api/status reports, so the two can never
        # disagree.
        from healthcheck.server import status_snapshot
        hist = History()
        hist.record(_result("web", False))
        hist.record(_result("web", False))
        snap = status_snapshot(hist, self.endpoints[:1],
                               now=1_700_000_000.0)
        m = _metrics(render_metrics(hist, self.endpoints[:1],
                                    now=1_700_000_000.0))
        self.assertEqual(
            m[("healthcheck_consecutive_failures",
               (("name", "web"),))],
            str(snap["endpoints"][0]["consecutive_failures"]))

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
        self.assertEqual(
            m[("healthcheck_uptime_ratio_30d", (("name", "web"),))], "1.0")
        self.assertEqual(
            m[("healthcheck_uptime_ratio_30d", (("name", "db"),))], "0.0")

    def test_uptime_ratio_30d_covers_checks_older_than_7d(self):
        # A check 10 days ago is outside the 24h/7d windows but
        # inside the 30-day one, so only the 30d gauge reports it.
        hist = History()
        hist.record(_result("web", True, at=1_700_000_000.0 - 10 * 86400))
        m = _metrics(render_metrics(hist, self.endpoints[:1],
                                    now=1_700_000_000.0))
        self.assertNotIn(
            ("healthcheck_uptime_ratio_24h", (("name", "web"),)), m)
        self.assertNotIn(
            ("healthcheck_uptime_ratio_7d", (("name", "web"),)), m)
        self.assertEqual(
            m[("healthcheck_uptime_ratio_30d", (("name", "web"),))], "1.0")

    def test_uptime_ratio_30d_agrees_with_uptime_snapshot(self):
        # The gauge reads the same uptime_pct_window figure GET
        # /api/uptime reports as uptime_30d_pct, so the two can
        # never disagree.
        snap = uptime_snapshot(self.history, self.endpoints,
                               now=1_700_000_000.0)
        m = _metrics(render_metrics(self.history, self.endpoints,
                                    now=1_700_000_000.0))
        for row in snap["endpoints"]:
            pct = row["windows"]["30d"]["uptime_pct"]
            self.assertEqual(
                m[("healthcheck_uptime_ratio_30d",
                   (("name", row["name"]),))],
                str(pct / 100.0))

    def test_uptime_ratio_omitted_without_data(self):
        text = render_metrics(self.history, self.endpoints + [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1", "port": 1}],
            now=1_700_000_000.0)
        m = _metrics(text)
        self.assertNotIn(
            ("healthcheck_uptime_ratio", (("name", "fresh"),)), m)
        self.assertNotIn(
            ("healthcheck_uptime_ratio_24h", (("name", "fresh"),)), m)
        self.assertNotIn(
            ("healthcheck_uptime_ratio_7d", (("name", "fresh"),)), m)
        self.assertNotIn(
            ("healthcheck_uptime_ratio_30d", (("name", "fresh"),)), m)

    def test_downtime_seconds_gauges(self):
        # A one-hour outage inside every window, and a day-long
        # outage 10 days ago that only the 30-day window covers.
        now = 1_700_000_000.0
        hist = History()
        hist.record(_result("db", False, at=now - 3600))
        hist.record(_result("db", True, at=now))
        hist.record(_result("web", False, at=now - 10 * 86400))
        hist.record(_result("web", True, at=now - 9 * 86400))
        m = _metrics(render_metrics(hist, self.endpoints, now=now))
        for suffix in ("24h", "7d", "30d"):
            self.assertEqual(
                m[(f"healthcheck_downtime_seconds_{suffix}",
                   (("name", "db"),))], "3600.0")
        self.assertEqual(
            m[("healthcheck_downtime_seconds_24h",
               (("name", "web"),))], "0.0")
        self.assertEqual(
            m[("healthcheck_downtime_seconds_7d",
               (("name", "web"),))], "0.0")
        self.assertEqual(
            m[("healthcheck_downtime_seconds_30d",
               (("name", "web"),))], "86400.0")

    def test_downtime_seconds_gauge_ongoing_outage_accrues_to_now(self):
        # An outage still open at the request time accrues
        # downtime to now in every window it falls in.
        now = 1_700_000_000.0
        hist = History()
        hist.record(_result("db", False, at=now - 600))
        m = _metrics(render_metrics(hist, self.endpoints[:1] + [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": 5432}], now=now))
        for suffix in ("24h", "7d", "30d"):
            self.assertEqual(
                m[(f"healthcheck_downtime_seconds_{suffix}",
                   (("name", "db"),))], "600.0")

    def test_downtime_seconds_gauges_always_present(self):
        # A never-checked endpoint has 0 downtime in every window,
        # not an omitted series — the gauges are safe to alert on.
        m = _metrics(render_metrics(self.history, self.endpoints + [
            {"name": "fresh", "type": "tcp", "host": "127.0.0.1", "port": 1}],
            now=1_700_000_000.0))
        for suffix in ("24h", "7d", "30d"):
            self.assertEqual(
                m[(f"healthcheck_downtime_seconds_{suffix}",
                   (("name", "fresh"),))], "0.0")

    def test_downtime_seconds_gauges_agree_with_api_status(self):
        # The gauges read the same downtime_seconds_window figure
        # GET /api/status reports as downtime_*_seconds, so the
        # two can never disagree.
        from healthcheck.server import status_snapshot
        snap = status_snapshot(self.history, self.endpoints,
                               now=1_700_000_000.0)
        m = _metrics(render_metrics(self.history, self.endpoints,
                                    now=1_700_000_000.0))
        for row in snap["endpoints"]:
            for suffix in ("24h", "7d", "30d"):
                self.assertEqual(
                    m[(f"healthcheck_downtime_seconds_{suffix}",
                       (("name", row["name"]),))],
                    str(row[f"downtime_{suffix}_seconds"]))

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
