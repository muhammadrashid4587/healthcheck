"""Tests for the static status page generator."""

import json
import os
import re
import tempfile
import unittest

from healthcheck.checker import CheckResult
from healthcheck.cli import main, parse_status_page_source
from healthcheck.statuspage import (
    endpoint_status,
    recent_incidents,
    render_status_page,
    write_status_page,
)
from healthcheck.store import History

BASE = 1_700_000_000.0


def _result(name, ok, latency=12.5, at=BASE, **kw):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at, **kw)


class IncidentTests(unittest.TestCase):
    def test_no_history_no_incidents(self):
        self.assertEqual(recent_incidents(History(), "web"), [])

    def test_all_up_no_incidents(self):
        hist = History()
        for i in range(3):
            hist.record(_result("web", True, at=BASE + i * 60))
        self.assertEqual(recent_incidents(hist, "web"), [])

    def test_consecutive_downs_are_one_incident(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60))
        hist.record(_result("web", False, at=BASE + 120))
        hist.record(_result("web", True, at=BASE + 180))
        incidents = recent_incidents(hist, "web")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["start"], BASE + 60)
        self.assertEqual(incidents[0]["end"], BASE + 180)
        self.assertEqual(incidents[0]["duration_s"], 120.0)
        self.assertEqual(incidents[0]["checks"], 2)
        self.assertFalse(incidents[0]["ongoing"])

    def test_ongoing_incident_has_no_end(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60))
        incidents = recent_incidents(hist, "web")
        self.assertEqual(len(incidents), 1)
        self.assertIsNone(incidents[0]["end"])
        self.assertTrue(incidents[0]["ongoing"])
        self.assertGreaterEqual(incidents[0]["duration_s"], 60.0)

    def test_separate_outages_are_separate_incidents_newest_first(self):
        hist = History()
        hist.record(_result("web", False, at=BASE))
        hist.record(_result("web", True, at=BASE + 60))
        hist.record(_result("web", False, at=BASE + 120))
        hist.record(_result("web", True, at=BASE + 180))
        incidents = recent_incidents(hist, "web")
        self.assertEqual(len(incidents), 2)
        self.assertEqual(incidents[0]["start"], BASE + 120)  # newest first
        self.assertEqual(incidents[1]["start"], BASE)

    def test_unconfirmed_skipped_and_maintenance_are_not_incidents(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60, unconfirmed=True))
        hist.record(_result("web", False, at=BASE + 120, skipped=True,
                            depends_on="db"))
        hist.record(_result("web", False, at=BASE + 180, maintenance=True))
        hist.record(_result("web", True, at=BASE + 240))
        self.assertEqual(recent_incidents(hist, "web"), [])

    def test_skip_does_not_split_an_incident(self):
        hist = History()
        hist.record(_result("web", False, at=BASE))
        hist.record(_result("web", False, at=BASE + 60, skipped=True,
                            depends_on="db"))
        hist.record(_result("web", False, at=BASE + 120))
        hist.record(_result("web", True, at=BASE + 180))
        incidents = recent_incidents(hist, "web")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["checks"], 2)

    def test_limit_is_respected(self):
        hist = History()
        for i in range(8):
            hist.record(_result("web", False, at=BASE + i * 120))
            hist.record(_result("web", True, at=BASE + i * 120 + 60))
        self.assertEqual(len(recent_incidents(hist, "web")), 5)
        self.assertEqual(len(recent_incidents(hist, "web", limit=2)), 2)


class StatusTests(unittest.TestCase):
    def test_unknown_without_history(self):
        self.assertEqual(endpoint_status(History(), "web"), "UNKNOWN")

    def test_unconfirmed_failure_still_shows_up(self):
        hist = History()
        hist.record(_result("web", False, unconfirmed=True))
        self.assertEqual(endpoint_status(hist, "web"), "UP")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.history.record(_result("web", True, at=BASE))
        self.history.record(_result("web", True, latency=20.0, at=BASE + 60))
        self.history.record(_result("web", False, at=BASE + 120))
        self.history.record(_result("web", True, at=BASE + 180))
        self.history.record(_result("db", False, at=BASE))
        self.endpoints = [
            {"name": "web", "type": "http", "url": "http://example.com"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
        ]

    def test_page_contains_statuses_uptime_and_sparkline(self):
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        self.assertIn("web", page)
        self.assertIn("db", page)
        self.assertIn("UP", page)
        self.assertIn("DOWN", page)
        self.assertIn("75.0%", page)   # web: 3/4 up overall
        self.assertIn("Uptime 24h", page)
        self.assertIn("Uptime 7d", page)
        self.assertIn("Uptime 30d", page)
        self.assertIn("<svg", page)    # latency sparkline, inline
        self.assertIn("<polyline", page)

    def test_page_30d_column_reads_the_30d_window(self):
        # A down check 20 days ago is inside the 30d window but
        # outside the 24h/7d ones; one 40 days ago is outside all
        # of them. The 30d column must show 2/3 up while 24h and
        # 7d show 100% — it is not a copy of the 7d column.
        hist = History()
        hist.record(_result("web", False, at=BASE - 40 * 86400))
        hist.record(_result("web", False, at=BASE - 20 * 86400))
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", True, at=BASE + 60))
        page = render_status_page(
            hist, self.endpoints[:1], now=BASE + 200)
        self.assertIn(
            "<td>50.0%</td><td>100.0%</td><td>100.0%</td>"
            "<td>66.7%</td>", page)

    def test_banner_reports_down_endpoints(self):
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        self.assertIn("1 endpoint down", page)

    def test_all_up_banner_when_nothing_down(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        page = render_status_page(hist, self.endpoints[:1], now=BASE + 60)
        self.assertIn("All systems operational", page)

    def test_incidents_section_lists_the_outage(self):
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        self.assertIn("Recent incidents", page)
        self.assertIn("1 failed check", page)

    def test_histogram_shown_for_endpoint_with_incidents(self):
        # web's one closed incident lasts exactly 60 s, so it lands
        # in "1m – 5m" (lower edge inclusive); db's ongoing incident
        # is 200 s old at the page's generated time, so it lands
        # there too. Both endpoints get a histogram, so the page
        # has two — one per endpoint with incidents.
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        buckets = ("&lt; 1m: 0, 1m – 5m: 1, 5m – 15m: 0, "
                   "15m – 1h: 0, 1h – 6h: 0, 6h – 24h: 0, "
                   "&gt; 24h: 0")
        self.assertEqual(page.count("Duration histogram:"), 2)
        self.assertEqual(page.count(
            f"Duration histogram: {buckets}"), 2)

    def test_no_histogram_for_endpoint_without_incidents(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60))
        hist.record(_result("web", True, at=BASE + 120))
        hist.record(_result("api", True, at=BASE))
        hist.record(_result("api", True, at=BASE + 60))
        page = render_status_page(
            hist, self.endpoints[:1] + [
                {"name": "api", "type": "http",
                 "url": "http://api.example.com"}], now=BASE + 200)
        # One histogram only: the endpoint with no incidents gets
        # no histogram section, and its bucket counts are all zero
        # nowhere in the page.
        self.assertEqual(page.count("Duration histogram:"), 1)
        self.assertIn("&lt; 1m: 0, 1m – 5m: 1", page)

    def test_histogram_labels_are_html_escaped(self):
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        self.assertIn("&lt; 1m", page)
        self.assertIn("&gt; 24h", page)
        self.assertNotIn("< 1m:", page)
        self.assertNotIn("> 24h:", page)

    def test_no_incidents_message_when_none(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        page = render_status_page(hist, self.endpoints[:1], now=BASE + 60)
        self.assertIn("No incidents recorded.", page)

    def test_page_is_self_contained(self):
        page = render_status_page(self.history, self.endpoints, now=BASE + 200)
        self.assertNotIn("<script", page)
        self.assertNotIn("src=", page)
        self.assertNotIn("href=", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_html_escapes_endpoint_names(self):
        self.history.record(_result("<b>x</b>", True, at=BASE))
        page = render_status_page(
            self.history, [{"name": "<b>x</b>", "type": "http",
                            "url": "http://x"}], now=BASE + 60)
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)

    def test_alert_at_and_cooldown_columns(self):
        # The static page carries the same Alert at / Cooldown
        # columns as the dashboard, right after the Fail streak:
        # web pages at 5 failures and re-pages every 300 s, db
        # uses the defaults (alert at the flap threshold, 3, and
        # no re-paging).
        page = render_status_page(
            self.history,
            [{"name": "web", "type": "http",
              "url": "http://example.com",
              "alert_threshold": 5, "alert_cooldown_s": 300},
             {"name": "db", "type": "tcp", "host": "127.0.0.1",
              "port": 5432}],
            now=BASE + 200)
        self.assertIn(
            "<th>Fail streak</th><th>Alert at</th><th>Cooldown</th>",
            page)
        # web's row: streak 0 (last check up), alert at 5,
        # cooldown "300 s".
        self.assertIn("<td>0</td><td>5</td><td>300 s</td>", page)
        # db's row: streak 1 (one failed check), alert at the
        # default 3, no cooldown.
        self.assertIn("<td>1</td><td>3</td><td>-</td>", page)

    def test_down_since_column(self):
        # The static page carries the same Down since column as
        # the dashboard, right after Status and before the Fail
        # streak: web has been DOWN since BASE + 60 (three failed
        # checks in a row, so confirmed), so at the page's
        # generated time (BASE + 200) the outage is 2m 20s old;
        # db is UP, so it shows "-".
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60))
        hist.record(_result("web", False, at=BASE + 120))
        hist.record(_result("web", False, at=BASE + 180))
        hist.record(_result("db", True, at=BASE + 180))
        page = render_status_page(
            hist,
            [{"name": "web", "type": "http",
              "url": "http://example.com"},
             {"name": "db", "type": "tcp", "host": "127.0.0.1",
              "port": 5432}],
            now=BASE + 200)
        self.assertIn(
            "<th>Status</th><th>Down since</th><th>Fail streak</th>",
            page)
        self.assertIn("<td class=\"down\">DOWN</td><td>2m 20s</td>",
                      page)
        self.assertIn("<td class=\"up\">UP</td><td>-</td>", page)

    def test_down_since_is_dash_for_unconfirmed_failures(self):
        # A still-UP endpoint with failures below the flap
        # threshold has no outage clock on the page, like the
        # dashboard: its run is visible in the Fail streak
        # instead, not as a Down since.
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60,
                            unconfirmed=True))
        page = render_status_page(
            hist,
            [{"name": "web", "type": "http",
              "url": "http://example.com"}],
            now=BASE + 200)
        self.assertIn("<td class=\"up\">UP</td><td>-</td><td>1</td>",
                      page)

    def test_method_headers_http_status_and_cert_columns(self):
        # The static page carries the same Method / Headers /
        # HTTP status / Cert expires columns as the dashboard,
        # right after the Cooldown column: web is checked with
        # HEAD, sends two headers, asserts on 204 but got 200,
        # and its certificate expires 89 days after the page's
        # generated time; db is a TCP endpoint, so all four are
        # "-".
        hist = History()
        hist.record(_result("web", True, at=BASE, status_code=200,
                            cert_expires_at=BASE + 200 + 89 * 86400))
        hist.record(_result("db", True, at=BASE))
        page = render_status_page(
            hist,
            [{"name": "web", "type": "http",
              "url": "https://example.com", "method": "HEAD",
              "headers": {"X-Api-Key": "s3cret",
                          "Authorization": "Bearer x"},
              "expect_status": 204},
             {"name": "db", "type": "tcp", "host": "127.0.0.1",
              "port": 5432}],
            now=BASE + 200)
        self.assertIn(
            "<th>Cooldown</th><th>Method</th><th>Headers</th>"
            "<th>HTTP status</th><th>Cert expires</th>",
            page)
        self.assertIn(
            "<td>HEAD</td><td>Authorization, X-Api-Key</td>"
            "<td>expected 204, got 200</td><td>89d</td>",
            page)
        # The header values are secrets: only the names appear.
        self.assertNotIn("s3cret", page)
        self.assertNotIn("Bearer", page)
        # db's row: all four columns "-".
        self.assertIn("<td>-</td><td>-</td><td>-</td><td>-</td>",
                      page)

    def test_depends_on_and_maintenance_columns(self):
        # The static page carries the same Depends on / Maintenance
        # columns as the dashboard, right after Cert expires and
        # before Uptime — from the same shared helpers the
        # dashboard reads, so the two pages can never disagree.
        # Depends on is the name from `depends_on` ("-" when the
        # endpoint has no dependency); Maintenance is the
        # endpoint's declared windows ("-" when it declares
        # none). The dependency name is HTML-escaped, like the
        # endpoint name.
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("plain", True, at=BASE))
        hist.record(_result("evil", True, at=BASE))
        page = render_status_page(
            hist,
            [{"name": "web", "type": "http",
              "url": "http://example.com", "depends_on": "db",
              "maintenance_windows": [
                  {"days": "sat", "start": "02:00", "end": "04:00"},
                  {"days": "sun", "start": "03:00", "end": "05:00"}]},
             {"name": "plain", "type": "http",
              "url": "http://plain.example.com"},
             {"name": "evil", "type": "http",
              "url": "http://evil.example.com",
              "depends_on": "<b>db</b>"}],
            now=BASE + 200)
        self.assertIn(
            "<th>Cert expires</th><th>Depends on</th>"
            "<th>Maintenance</th><th>Uptime</th>",
            page)
        # web depends on db and has two windows, joined with "; ".
        self.assertIn(
            "<td>db</td><td>Sat 02:00–04:00; Sun 03:00–05:00</td>",
            page)
        # plain has no dependency and no windows: "-" for both,
        # right after its Cert expires "-" and before its 100%
        # uptime.
        self.assertIn(
            "<td>-</td><td>-</td><td>-</td><td>100.0%</td>", page)
        # The dependency name is escaped, never raw HTML.
        self.assertIn("<td>&lt;b&gt;db&lt;/b&gt;</td>", page)
        self.assertNotIn("<td><b>db</b></td>", page)

    def test_remote_source_depends_on_and_maintenance_columns_are_dash(self):
        # An extra source's endpoints carry no config, so they show
        # "-" for both columns — a file source's recorded checks
        # cannot supply them, and a remote source shows "-" for
        # them too, like the other config columns.
        page = render_status_page(
            History(), [], now=BASE + 200,
            extra_sources=[("remote", History(),
                            [{"name": "web"}])])
        self.assertIn("<h2>remote</h2>", page)
        self.assertIn(
            "<th>Cert expires</th><th>Depends on</th>"
            "<th>Maintenance</th><th>Uptime</th>",
            page)
        # The remote row's Method / Headers / HTTP status / Cert
        # expires / Depends on / Maintenance are all "-".
        self.assertIn(
            "<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>"
            "<td>-</td>",
            page)

    def test_cert_expires_soon_and_expired(self):
        # Inside the endpoint's warning window the page shows
        # "(soon)"; once the certificate has lapsed it shows
        # "expired" — the same figures the dashboard shows.
        hist = History()
        hist.record(_result("soon", True, at=BASE,
                            cert_expires_at=BASE + 200 + 12 * 86400))
        hist.record(_result("old", True, at=BASE,
                            cert_expires_at=BASE + 200 - 86400))
        page = render_status_page(
            hist,
            [{"name": "soon", "type": "http",
              "url": "https://soon.example.com"},
             {"name": "old", "type": "http",
              "url": "https://old.example.com"}],
            now=BASE + 200)
        self.assertIn("<td>12d (soon)</td>", page)
        self.assertIn("<td>expired</td>", page)

    def test_http_status_column_without_expectation(self):
        # Without expect_status the page shows the bare actual
        # code; a never-checked endpoint shows "-" for all four.
        hist = History()
        hist.record(_result("web", True, at=BASE, status_code=200))
        page = render_status_page(
            hist,
            [{"name": "web", "type": "http",
              "url": "http://example.com"},
             {"name": "ghost", "type": "http",
              "url": "http://ghost.example.com"}],
            now=BASE + 200)
        self.assertIn("<td>-</td><td>-</td><td>200</td><td>-</td>",
                      page)
        self.assertIn("<td>-</td><td>-</td><td>-</td><td>-</td>",
                      page)

    def test_latency_percentiles_column(self):
        # The static page carries the same Latency p50 / p95 / p99
        # column as the dashboard, right after Latency and before
        # the sparkline: nearest-rank percentiles over the last 24
        # hours of recorded latencies. A check older than the
        # window does not count, and a never-checked endpoint
        # shows "-".
        hist = History()
        hist.record(_result("web", True, latency=999.0,
                            at=BASE - 25 * 3600))
        hist.record(_result("web", True, latency=10.0, at=BASE))
        hist.record(_result("web", True, latency=20.0, at=BASE + 10))
        hist.record(_result("web", True, latency=30.0, at=BASE + 20))
        page = render_status_page(
            hist,
            [{"name": "web", "type": "http",
              "url": "http://example.com"},
             {"name": "ghost", "type": "http",
              "url": "http://ghost.example.com"}],
            now=BASE + 200)
        self.assertIn(
            "<th>Latency</th><th>Latency budget</th><th>Check every</th>"
            "<th>Retries</th><th>Latency p50 / p95 / p99</th>"
            "<th>Latency history</th>",
            page)
        self.assertIn("<td>20.0 / 30.0 / 30.0 ms</td>", page)
        # ghost's row ends Latency / budget / interval / retries /
        # percentiles / sparkline / last error / SLA breach / last
        # check, all "-".
        self.assertIn(
            "<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>"
            "<td>-</td><td>-</td><td>-</td><td>-</td></tr>",
            page)

    def test_last_error_column(self):
        # The static page carries the same Last error column as the
        # dashboard, right after the latency sparkline and before
        # Last check — from the same shared helper the dashboard
        # reads, so the two pages can never disagree. It is the
        # error string of the most recent failed check that
        # actually ran ("-" when the endpoint never failed), and
        # it stays after a later success, so a recovered endpoint
        # still shows why it last failed. The error is HTML-escaped.
        hist = History()
        hist.record(_result("web", False, at=BASE,
                            error="connection refused"))
        hist.record(_result("web", True, latency=10.0, at=BASE + 60))
        hist.record(_result("evil", False, at=BASE,
                            error='bad <script> & "quote"'))
        hist.record(_result("plain", True, latency=5.0, at=BASE))
        page = render_status_page(
            hist,
            [{"name": "web", "type": "http",
              "url": "http://example.com"},
             {"name": "evil", "type": "http",
              "url": "http://evil.example.com"},
             {"name": "plain", "type": "http",
              "url": "http://plain.example.com"},
             {"name": "ghost", "type": "http",
              "url": "http://ghost.example.com"}],
            now=BASE + 200)
        self.assertIn(
            "<th>Latency history</th><th>Last error</th>"
            "<th>SLA breach</th><th>Last check</th>",
            page)
        # web recovered, but its last error is still shown.
        self.assertIn("<td>connection refused</td>", page)
        # The error text is escaped, never raw HTML.
        self.assertIn(
            "<td>bad &lt;script&gt; &amp; &quot;quote&quot;</td>", page)
        self.assertNotIn("<script>", page)
        # plain never failed and ghost was never checked: "-".
        # plain's row ends with its last error "-" and then its
        # SLA breach "-" and its last check time; ghost's row
        # ends with "-" throughout.
        self.assertRegex(
            page,
            r"<td>-</td><td>-</td><td>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}</td></tr>")
        self.assertIn(
            "<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>"
            "<td>-</td><td>-</td><td>-</td><td>-</td></tr>",
            page)

    def test_sla_breach_column(self):
        # The static page carries the same SLA breach column as
        # the dashboard, right after Last error and before Last
        # check — from the same shared helper the dashboard
        # reads, so the two pages can never disagree. "BREACH
        # -X.Xpp" when the endpoint is below its sla_target in
        # the page's calendar month; "-" when it has no target,
        # no data in the month, or meets the target.
        hist = History()
        for i in range(9):
            hist.record(_result("api", True, at=BASE + i * 60))
        hist.record(_result("api", False, at=BASE + 9 * 60))
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("old", False, at=BASE - 40 * 86400))
        page = render_status_page(
            hist,
            [{"name": "api", "type": "http",
              "url": "http://api.example.com", "sla_target": 99.9},
             {"name": "web", "type": "http",
              "url": "http://example.com", "sla_target": 99.9},
             {"name": "plain", "type": "http",
              "url": "http://plain.example.com"},
             {"name": "old", "type": "http",
              "url": "http://old.example.com", "sla_target": 99.9},
             {"name": "ghost", "type": "http",
              "url": "http://ghost.example.com"}],
            now=BASE + 200)
        self.assertIn(
            "<th>Last error</th><th>SLA breach</th><th>Last check</th>",
            page)
        # api: 9/10 up in the page's month against 99.9 → breach.
        self.assertIn("<td>BREACH -9.9pp</td>", page)
        self.assertEqual(page.count("BREACH"), 1)
        # web meets its target, plain has no target, old has no
        # data in the page's month, ghost was never checked: all
        # "-" in the SLA breach column, so the count above is the
        # only breach on the page.

    def test_extra_source_sla_breach_column_is_dash(self):
        # An extra source's endpoints carry no config, so they
        # have no SLA target and always show "-" — even a file
        # source whose recorded history has failures in the
        # page's month.
        other = History()
        for i in range(9):
            other.record(_result("web", True, at=BASE + i * 60))
        other.record(_result("web", False, at=BASE + 9 * 60))
        page = render_status_page(
            History(), [], now=BASE + 200,
            extra_sources=[("eu-west", other,
                            [{"name": "web"}])])
        self.assertIn("<h2>eu-west</h2>", page)
        self.assertIn("<th>SLA breach</th>", page)
        self.assertNotIn("BREACH", page)

    def test_downtime_column(self):
        # The static page carries a Downtime column right after
        # Uptime 30d — total outage time inside the last 30 days,
        # from the same `endpoint_downtime_text` the API's
        # `downtime_30d_seconds` is built on, so the page can never
        # disagree with it: "11m 0s", "-" when the endpoint
        # recorded no downtime in the window.
        hist = History()
        # api: one 60-second outage 20 days ago (inside the 30d
        # window) plus one 10-minute outage an hour ago — 11
        # minutes total.
        hist.record(_result("api", True, at=BASE - 20 * 86400))
        hist.record(_result("api", False, at=BASE - 20 * 86400 + 60))
        hist.record(_result("api", True, at=BASE - 20 * 86400 + 120))
        hist.record(_result("api", True, at=BASE - 3600))
        hist.record(_result("api", False, at=BASE - 1800))
        hist.record(_result("api", True, at=BASE - 1200))
        # web never went down; old's only outage was 40 days ago,
        # outside the window.
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("old", False, at=BASE - 40 * 86400))
        hist.record(_result("old", True, at=BASE - 40 * 86400 + 60))
        page = render_status_page(
            hist,
            [{"name": "api", "type": "http",
              "url": "http://api.example.com"},
             {"name": "web", "type": "http",
              "url": "http://example.com"},
             {"name": "old", "type": "http",
              "url": "http://old.example.com"},
             {"name": "ghost", "type": "http",
              "url": "http://ghost.example.com"}],
            now=BASE + 200)
        self.assertIn(
            "<th>Uptime 30d</th><th>Downtime 30d</th><th>Latency</th>",
            page)
        self.assertIn("<td>11m 0s</td>", page)
        self.assertEqual(page.count("11m 0s"), 1)
        # web, old and ghost recorded no downtime in the window,
        # so their Downtime cells are "-"; api's is the only
        # duration on the page.

    def test_remote_source_downtime_column_is_dash(self):
        # A remote source's snapshot carries no per-check records,
        # so its Downtime column is "-", like its percentiles and
        # sparkline — no data is not downtime.
        page = render_status_page(
            History(), [], now=BASE + 200,
            extra_sources=[("remote", History(),
                            [{"name": "web"}])])
        self.assertIn("<h2>remote</h2>", page)
        self.assertIn("<th>Downtime 30d</th>", page)
        # The remote row's Uptime / Uptime 24h / Uptime 7d /
        # Uptime 30d / Downtime 30d / Latency cells are all "-".
        self.assertIn(
            "<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>"
            "<td>-</td>",
            page)
        self.assertNotIn("m 0s", page)

    def test_extra_source_file_keeps_downtime(self):
        # A file extra source's endpoints carry no config, but
        # Downtime comes from its recorded checks, so it shows
        # its real recorded downtime, like a local one.
        other = History()
        other.record(_result("web", True, at=BASE - 3600))
        other.record(_result("web", False, at=BASE - 1800))
        other.record(_result("web", True, at=BASE - 1200))
        page = render_status_page(
            History(), [], now=BASE + 200,
            extra_sources=[("eu-west", other,
                            [{"name": "web"}])])
        self.assertIn("<h2>eu-west</h2>", page)
        self.assertIn("<th>Downtime 30d</th>", page)
        self.assertIn("<td>10m 0s</td>", page)

    def test_remote_source_last_error_column_is_dash(self):
        # A remote source's snapshot carries no per-check records,
        # so its Last error column is "-", like its percentiles
        # and sparkline.
        page = render_status_page(
            History(), [], now=BASE + 200,
            extra_sources=[("remote", History(),
                            [{"name": "web"}])])
        self.assertIn("<h2>remote</h2>", page)
        self.assertIn("<th>Latency history</th><th>Last error</th>"
                      "<th>SLA breach</th><th>Last check</th>", page)
        self.assertIn(
            "<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>"
            "<td>-</td><td>-</td><td>-</td><td>-</td></tr>",
            page)

    def test_latency_budget_check_every_retries_columns(self):
        # The static page carries the same Latency budget, Check
        # every, and Retries columns as the dashboard, right after
        # Latency and before the percentiles, in the same order —
        # from the same shared helpers the dashboard reads, so the
        # two pages can never disagree. The budget is the
        # endpoint's max_latency_ms ("-" when it has none), Check
        # every its check_interval_s ("-" when it uses the global
        # interval), Retries the retries spent on the most recent
        # recorded check ("-" when never checked).
        hist = History()
        hist.record(_result("web", True, latency=10.0, at=BASE,
                            retries=1))
        hist.record(_result("plain", True, latency=5.0, at=BASE))
        page = render_status_page(
            hist,
            [{"name": "web", "type": "http",
              "url": "http://example.com",
              "max_latency_ms": 500, "check_interval_s": 60},
             {"name": "plain", "type": "http",
              "url": "http://plain.example.com"},
             {"name": "ghost", "type": "http",
              "url": "http://ghost.example.com"}],
            now=BASE + 200)
        self.assertIn(
            "<th>Latency</th><th>Latency budget</th><th>Check every</th>"
            "<th>Retries</th><th>Latency p50 / p95 / p99</th>",
            page)
        self.assertIn("<td>500 ms</td><td>60 s</td><td>1</td>", page)
        # plain has no budget or interval and its check needed no
        # retry.
        self.assertIn("<td>-</td><td>-</td><td>0</td>", page)
        # ghost's row ends Latency / budget / interval / retries /
        # percentiles / sparkline / last error / SLA breach / last
        # check, all "-".
        self.assertIn(
            "<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>"
            "<td>-</td><td>-</td><td>-</td><td>-</td></tr>",
            page)

    def test_remote_source_percentiles_column_is_dash(self):
        # A remote source's snapshot carries no per-check records,
        # so its percentiles column is "-", like the dashboard's
        # sparkline for the same source.
        page = render_status_page(
            History(), [], now=BASE + 200,
            extra_sources=[("remote", History(),
                            [{"name": "web"}])])
        self.assertIn("<h2>remote</h2>", page)
        self.assertIn("<th>Latency p50 / p95 / p99</th>", page)
        self.assertIn("<th>Latency budget</th><th>Check every</th>"
                      "<th>Retries</th>", page)
        # A remote source's row ends Latency / budget / interval /
        # retries / percentiles / sparkline / last error / SLA
        # breach / last check, all "-": its snapshot carries no
        # endpoint config and no per-check records.
        self.assertIn(
            "<td>-</td><td>-</td><td>-</td><td>-</td><td>-</td>"
            "<td>-</td><td>-</td><td>-</td><td>-</td></tr>",
            page)

    def test_extra_source_file_keeps_status_and_cert(self):
        # A file extra source's endpoints carry no config, so
        # Method and Headers are "-", but HTTP status and Cert
        # expires come from its recorded checks, like the local
        # source's.
        other = History()
        other.record(_result("web", True, at=BASE, status_code=200,
                             cert_expires_at=BASE + 200 + 89 * 86400))
        page = render_status_page(
            History(), [], now=BASE + 200,
            extra_sources=[("eu-west", other,
                            [{"name": "web"}])])
        self.assertIn("<h2>eu-west</h2>", page)
        self.assertIn(
            "<td>-</td><td>-</td><td>200</td><td>89d</td>", page)

    def test_empty_endpoints(self):
        page = render_status_page(History(), [], now=BASE)
        self.assertIn("No endpoints configured.", page)
        self.assertIn("All systems operational", page)


class CliAlertColumnsTests(unittest.TestCase):
    def test_status_page_resolves_threshold_and_cooldown(self):
        # Through the CLI the page shows the *resolved* values:
        # the configured flap_threshold (4) fills in an endpoint
        # without its own alert_threshold, and the global
        # alert_cooldown_s (300) fills in one without its own.
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            out_path = os.path.join(d, "status.html")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "flap_threshold": 4,
                    "alert_cooldown_s": 300,
                    "endpoints": [
                        {"name": "web", "type": "http",
                         "url": "http://example.com",
                         "alert_threshold": 5},
                        {"name": "db", "type": "tcp",
                         "host": "127.0.0.1", "port": 5432},
                    ]}, fh)
            self.assertEqual(
                main(["--config", cfg_path, "--status-page",
                      out_path]), 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
        # web: its own alert threshold 5, the global cooldown.
        self.assertIn("<td>5</td><td>300 s</td>", page)
        # db: the flap threshold 4 in effect, the global cooldown.
        self.assertIn("<td>4</td><td>300 s</td>", page)


class WriteTests(unittest.TestCase):
    def test_write_creates_file_matching_render(self):
        hist = History()
        hist.record(_result("web", True, at=BASE))
        endpoints = [{"name": "web", "type": "http", "url": "http://example.com"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "status.html")
            self.assertEqual(write_status_page(hist, endpoints, path, now=BASE + 60), path)
            with open(path, encoding="utf-8") as fh:
                written = fh.read()
            self.assertEqual(written, render_status_page(hist, endpoints, now=BASE + 60))
            self.assertFalse(os.path.exists(path + ".tmp"))  # no temp left behind


class CliTests(unittest.TestCase):
    def test_status_page_flag_writes_file_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            hist_path = os.path.join(d, "history.json")
            out_path = os.path.join(d, "status.html")
            hist = History()
            hist.record(_result("web", True, at=BASE))
            hist.record(_result("web", False, at=BASE + 60))
            hist.save(hist_path)
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"history_file": hist_path,
                           "endpoints": [{"name": "web", "type": "http",
                                          "url": "http://example.com"}]}, fh)
            rc = main(["--config", cfg_path, "--status-page", out_path])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("web", page)
            self.assertIn("DOWN", page)
            self.assertIn("1 failed check", page)

    def test_status_page_with_no_history_file_still_writes(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            out_path = os.path.join(d, "status.html")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"endpoints": [{"name": "web", "type": "http",
                                          "url": "http://example.com"}]}, fh)
            rc = main(["--config", cfg_path, "--status-page", out_path])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                self.assertIn("UNKNOWN", fh.read())

    def test_once_then_status_page_uses_the_fresh_check(self):
        # A TCP endpoint on a closed localhost port fails fast and
        # deterministically (connection refused), so --once records a
        # real down check the page then shows.
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            out_path = os.path.join(d, "status.html")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                # flap_threshold 1 so the single failed check is a
                # confirmed outage (the default 3 would leave it
                # unconfirmed and the page would still show UP).
                json.dump({"flap_threshold": 1,
                           "endpoints": [{"name": "db", "type": "tcp",
                                          "host": "127.0.0.1", "port": 1}]}, fh)
            rc = main(["--config", cfg_path, "--once",
                       "--status-page", out_path])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("DOWN", page)
            self.assertIn("1 failed check", page)


class ExtraSourceTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.history.record(_result("web", True, at=BASE))
        self.history.record(_result("web", True, latency=20.0, at=BASE + 60))
        self.history.record(_result("web", False, at=BASE + 120))
        self.history.record(_result("web", True, at=BASE + 180))
        self.endpoints = [
            {"name": "web", "type": "http", "url": "http://example.com"},
        ]
        # Second source: same endpoint name, different history — its
        # web endpoint never failed, and it also tracks "cache".
        self.other = History()
        self.other.record(_result("web", True, at=BASE))
        self.other.record(_result("web", True, at=BASE + 60))
        self.other.record(_result("cache", False, at=BASE))
        self.other.record(_result("cache", True, at=BASE + 60))
        self.other_endpoints = [
            {"name": "web", "type": "http", "url": "http://example.com"},
            {"name": "cache", "type": "tcp", "host": "127.0.0.1",
             "port": 6379},
        ]

    def test_extra_source_gets_its_own_labelled_section(self):
        page = render_status_page(
            self.history, self.endpoints, now=BASE + 200,
            extra_sources=[("eu-west", self.other, self.other_endpoints)])
        self.assertIn("<h2>local</h2>", page)
        self.assertIn("<h2>eu-west</h2>", page)
        self.assertIn("cache", page)

    def test_same_endpoint_name_is_judged_per_source(self):
        # Local web is 3/4 up (one outage); the other source's web
        # is 2/2 up — the two tables must not share the figure.
        page = render_status_page(
            self.history, self.endpoints, now=BASE + 200,
            extra_sources=[("eu-west", self.other, self.other_endpoints)])
        self.assertIn("75.0%", page)    # local web
        self.assertIn("100.0%", page)   # other source's web
        # The other source's cache outage is grouped as one
        # incident under its own section, not the local one.
        self.assertIn("1 failed check", page)

    def test_each_source_gets_its_own_banner(self):
        # Local web is UP at the end; the other source's cache is
        # also UP, but make a second source that is down to check
        # the banner is per-source, not global.
        down = History()
        down.record(_result("api", False, at=BASE))
        page = render_status_page(
            self.history, self.endpoints, now=BASE + 200,
            extra_sources=[("eu-west", down,
                            [{"name": "api", "type": "http",
                              "url": "http://api.example.com"}])])
        self.assertIn("All systems operational", page)  # local source
        self.assertIn("1 endpoint down", page)          # eu-west source

    def test_extra_source_label_is_escaped(self):
        page = render_status_page(
            self.history, self.endpoints, now=BASE + 200,
            extra_sources=[("<b>eu</b>", self.other,
                            self.other_endpoints)])
        self.assertNotIn("<b>eu</b>", page)
        self.assertIn("&lt;b&gt;eu&lt;/b&gt;", page)

    def test_no_extra_sources_page_is_unchanged(self):
        page = render_status_page(self.history, self.endpoints,
                                  now=BASE + 200)
        self.assertNotIn("<h2>local</h2>", page)
        self.assertIn("75.0%", page)


class ParseSourceTests(unittest.TestCase):
    def test_label_and_path(self):
        self.assertEqual(parse_status_page_source("eu-west=/x/hist.json"),
                         ("eu-west", "/x/hist.json"))

    def test_path_may_contain_equals(self):
        self.assertEqual(parse_status_page_source("eu=/x/a=b.json"),
                         ("eu", "/x/a=b.json"))

    def test_bare_path_rejected(self):
        with self.assertRaises(ValueError):
            parse_status_page_source("/x/hist.json")

    def test_empty_label_or_path_rejected(self):
        with self.assertRaises(ValueError):
            parse_status_page_source("=/x/hist.json")
        with self.assertRaises(ValueError):
            parse_status_page_source("eu=")


class CliExtraSourceTests(unittest.TestCase):
    def _write_config_and_histories(self, d):
        cfg_path = os.path.join(d, "config.json")
        hist_path = os.path.join(d, "history.json")
        other_path = os.path.join(d, "other-history.json")
        hist = History()
        hist.record(_result("web", True, at=BASE))
        hist.record(_result("web", False, at=BASE + 60))
        hist.save(hist_path)
        other = History()
        other.record(_result("cache", True, at=BASE))
        other.save(other_path)
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({"history_file": hist_path,
                       "endpoints": [{"name": "web", "type": "http",
                                      "url": "http://example.com"}]}, fh)
        return cfg_path, other_path

    def test_status_page_merges_second_source(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path, other_path = self._write_config_and_histories(d)
            out_path = os.path.join(d, "status.html")
            rc = main(["--config", cfg_path, "--status-page", out_path,
                       "--status-page-source", f"eu-west={other_path}"])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("<h2>local</h2>", page)
            self.assertIn("<h2>eu-west</h2>", page)
            self.assertIn("cache", page)
            self.assertIn("1 failed check", page)  # local web outage

    def test_missing_second_history_still_renders(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path, _ = self._write_config_and_histories(d)
            out_path = os.path.join(d, "status.html")
            rc = main(["--config", cfg_path, "--status-page", out_path,
                       "--status-page-source",
                       f"eu-west={os.path.join(d, 'nope.json')}"])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("<h2>eu-west</h2>", page)
            self.assertIn("No endpoints configured.", page)
            self.assertIn("web", page)

    def test_source_flag_without_status_page_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path, other_path = self._write_config_and_histories(d)
            with self.assertRaises(SystemExit) as cm:
                main(["--config", cfg_path, "--once",
                      "--status-page-source", f"eu-west={other_path}"])
            self.assertEqual(cm.exception.code, 2)

    def test_source_spec_without_equals_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path, other_path = self._write_config_and_histories(d)
            out_path = os.path.join(d, "status.html")
            with self.assertRaises(SystemExit) as cm:
                main(["--config", cfg_path, "--status-page", out_path,
                      "--status-page-source", other_path])
            self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
