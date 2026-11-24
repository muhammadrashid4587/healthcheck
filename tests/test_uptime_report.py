"""Tests for the --uptime-report text export: the same figures
`GET /api/uptime` returns, rendered as plain text."""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from healthcheck.checker import CheckResult
from healthcheck.cli import main, save_history
from healthcheck.server import render_uptime_report, uptime_snapshot
from healthcheck.store import History

NOW = 1_700_000_000.0
DAY = 24 * 3600


def _result(name, ok, at, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=10.0,
                       checked_at=at, **kwargs)


def _history():
    """api: one 60-second outage 20 days ago, one 60-second
    outage 3 days ago, one 10-minute outage an hour ago.
    flaky: up 2 hours ago, down 1 hour ago, still down.
    clipped: down 25 hours ago, recovered 23 hours ago — the
    outage straddles the start of the 24h window. stale: down
    2 days ago, never recovered. empty: no checks at all."""
    hist = History()
    hist.record(_result("api", True, NOW - 20 * DAY))
    hist.record(_result("api", False, NOW - 20 * DAY + 60))
    hist.record(_result("api", True, NOW - 20 * DAY + 120))
    hist.record(_result("api", True, NOW - 3 * DAY))
    hist.record(_result("api", False, NOW - 3 * DAY + 60))
    hist.record(_result("api", True, NOW - 3 * DAY + 120))
    hist.record(_result("api", True, NOW - 3600))
    hist.record(_result("api", False, NOW - 1800))
    hist.record(_result("api", True, NOW - 1200))
    hist.record(_result("flaky", True, NOW - 7200))
    hist.record(_result("flaky", False, NOW - 3600))
    hist.record(_result("clipped", False, NOW - 25 * 3600))
    hist.record(_result("clipped", True, NOW - 23 * 3600))
    hist.record(_result("stale", False, NOW - 2 * DAY))
    return hist


ENDPOINTS = [
    {"name": "api", "sla_target": 99.9},
    {"name": "flaky", "sla_target": 50.0},
    {"name": "clipped", "sla_target": 99.0},
    {"name": "stale", "sla_target": 99.9},
    {"name": "empty", "sla_target": 99.9},
    {"name": "web"},
]


def _report():
    return render_uptime_report(
        uptime_snapshot(_history(), ENDPOINTS, now=NOW))


class RenderUptimeReportTests(unittest.TestCase):
    def test_header_and_per_window_figures(self):
        text = _report()
        self.assertIn("Uptime report (generated ", text)
        # api: 3 counted checks in 24h, 2 up; 600 s downtime.
        self.assertIn("  api:", text)
        self.assertIn("24h: 66.7% uptime, 10m 0s downtime "
                      "(3 checks)", text)
        self.assertIn("7d: 66.7% uptime, 11m 0s downtime "
                      "(6 checks)", text)
        self.assertIn("30d: 66.7% uptime, 12m 0s downtime "
                      "(9 checks)", text)

    def test_ongoing_outage_accrues_to_now(self):
        # flaky went down an hour ago and never recovered: the
        # full hour counts in every window.
        text = _report()
        self.assertIn("  flaky:", text)
        self.assertIn("24h: 50.0% uptime, 1h 0m downtime "
                      "(2 checks)", text)
        self.assertIn("30d: 50.0% uptime, 1h 0m downtime "
                      "(2 checks)", text)

    def test_clipped_outage_counts_only_the_part_inside(self):
        text = _report()
        self.assertIn("  clipped:", text)
        # No counted check falls inside the 24h window except the
        # recovery check an hour inside it, but only the hour of
        # outage inside the window counts.
        self.assertIn("24h: 100.0% uptime, 1h 0m downtime (1 check)",
                      text)
        self.assertIn("7d: 50.0% uptime, 2h 0m downtime "
                      "(2 checks)", text)

    def test_no_counted_checks_is_no_data_not_zero(self):
        text = _report()
        self.assertIn("  stale:", text)
        self.assertIn("24h: no data, 1d 0h downtime (0 checks)", text)
        self.assertIn("7d: 0.0% uptime, 2d 0h downtime (1 check)",
                      text)
        self.assertIn("  empty:", text)
        self.assertIn("24h: no data, 0s downtime (0 checks)", text)

    def test_sla_verdicts(self):
        text = _report()
        # api: 30d 66.7% vs target 99.9% — short.
        self.assertIn("SLA target 99.9%: short by 33.2pp (30d 66.7%)",
                      text)
        # flaky: 30d 50.0% vs target 50.0% — met.
        self.assertIn("SLA target 50.0%: met (30d 50.0%)", text)
        # empty: no 30d data, so no verdict.
        self.assertIn("SLA target 99.9%: no 30d data", text)
        # web: no target configured.
        self.assertIn("SLA target: none", text)


class UptimeReportCliTests(unittest.TestCase):
    def _setup(self, d):
        cfg_path = os.path.join(d, "config.json")
        hist_path = os.path.join(d, "history.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({
                "history_file": hist_path,
                "endpoints": [
                    {"name": "api", "type": "tcp", "host": "127.0.0.1",
                     "port": 1, "timeout": 1, "sla_target": 99.9},
                    {"name": "web", "type": "tcp", "host": "127.0.0.1",
                     "port": 1, "timeout": 1},
                ],
            }, fh)
        hist = History()
        now = time.time()
        hist.record(_result("api", True, now - 7200))
        hist.record(_result("api", False, now - 3600))
        hist.record(_result("api", True, now - 1800))
        hist.record(_result("web", True, now - 3600))
        save_history(hist, hist_path)
        return cfg_path

    def test_uptime_report_flag_prints_text(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path, "--uptime-report"]), 0)
            text = out.getvalue()
            self.assertIn("Uptime report (generated ", text)
            self.assertIn("  api:", text)
            self.assertIn("24h: 66.7% uptime, 30m 0s downtime "
                          "(3 checks)", text)
            self.assertIn("SLA target 99.9%: short by 33.2pp "
                          "(30d 66.7%)", text)
            self.assertIn("  web:", text)
            self.assertIn("SLA target: none", text)

    def test_uptime_out_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out_path = os.path.join(d, "uptime.txt")
            self.assertEqual(
                main(["--config", cfg_path, "--uptime-report",
                      "--uptime-out", out_path]), 0)
            with open(out_path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("Uptime report (generated ", text)
            self.assertIn("  api:", text)

    def test_uptime_out_without_flag_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            with self.assertRaises(SystemExit) as ctx:
                main(["--config", cfg_path, "--uptime-out",
                      os.path.join(d, "uptime.txt")])
            self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
