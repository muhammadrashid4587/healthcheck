"""Tests for the --downtime-report text export: the same downtime
figures `GET /api/uptime` returns and `--uptime-report` prints,
on their own, plus a total across endpoints."""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from healthcheck.checker import CheckResult
from healthcheck.cli import main, save_history
from healthcheck.server import (render_downtime_report,
                                render_uptime_report, uptime_snapshot)
from healthcheck.store import History

NOW = 1_700_000_000.0
DAY = 24 * 3600


def _result(name, ok, at, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=10.0,
                       checked_at=at, **kwargs)


def _history():
    """Same seed as the uptime-report tests: api has one 60-second
    outage 20 days ago, one 60-second outage 3 days ago, and one
    10-minute outage an hour ago. flaky: up 2 hours ago, down 1
    hour ago, still down. clipped: down 25 hours ago, recovered
    23 hours ago — the outage straddles the start of the 24h
    window. stale: down 2 days ago, never recovered. empty: no
    checks at all."""
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


def _snapshot():
    return uptime_snapshot(_history(), ENDPOINTS, now=NOW)


def _report():
    return render_downtime_report(_snapshot())


class RenderDowntimeReportTests(unittest.TestCase):
    def test_header_and_per_window_figures(self):
        text = _report()
        self.assertIn("Downtime report (generated ", text)
        self.assertIn("  api:", text)
        self.assertIn("24h: 10m 0s downtime", text)
        self.assertIn("7d: 11m 0s downtime", text)
        self.assertIn("30d: 12m 0s downtime", text)

    def test_ongoing_outage_accrues_to_now(self):
        # flaky went down an hour ago and never recovered: the
        # full hour counts in every window.
        text = _report()
        self.assertIn("  flaky:", text)
        self.assertIn("24h: 1h 0m downtime", text)
        self.assertIn("30d: 1h 0m downtime", text)

    def test_clipped_outage_counts_only_the_part_inside(self):
        text = _report()
        self.assertIn("  clipped:", text)
        # Only the hour of the outage inside the 24h window
        # counts; the full 2 hours count in the 7d window.
        self.assertIn("24h: 1h 0m downtime", text)
        self.assertIn("7d: 2h 0m downtime", text)

    def test_ongoing_outage_with_no_recent_check_still_counts(self):
        # stale went down 2 days ago and never recovered: a full
        # day of downtime in the 24h window, two days in the 7d
        # window — even though no check falls inside the 24h
        # window at all.
        text = _report()
        self.assertIn("  stale:", text)
        self.assertIn("24h: 1d 0h downtime", text)
        self.assertIn("7d: 2d 0h downtime", text)

    def test_no_downtime_is_zero_not_missing(self):
        text = _report()
        self.assertIn("  empty:", text)
        self.assertIn("24h: 0s downtime", text)
        self.assertIn("30d: 0s downtime", text)

    def test_totals_sum_the_per_endpoint_figures(self):
        text = _report()
        # 24h: 600 (api) + 3600 (flaky) + 3600 (clipped)
        #       + 86400 (stale) + 0 (empty) = 94200 s = 1d 2h 10m.
        # 7d: 660 + 3600 + 7200 + 172800 = 184260 s = 2d 3h 11m.
        # 30d: 720 + 3600 + 7200 + 172800 = 184320 s = 2d 3h 12m.
        self.assertIn("  Total:", text)
        self.assertIn("24h: 1d 2h downtime", text)
        self.assertIn("7d: 2d 3h downtime", text)
        self.assertIn("30d: 2d 3h downtime", text)

    def test_matches_uptime_report_figures(self):
        # Both renders read the same snapshot, so every downtime
        # figure the uptime report prints must appear in the
        # downtime report too — the two exports can never
        # disagree about an endpoint's downtime.
        snapshot = _snapshot()
        downtime_text = render_downtime_report(snapshot)
        uptime_text = render_uptime_report(snapshot)
        for figure in ("10m 0s downtime", "11m 0s downtime",
                       "12m 0s downtime", "1h 0m downtime",
                       "1d 0h downtime", "2d 0h downtime",
                       "0s downtime"):
            self.assertIn(figure, uptime_text)
            self.assertIn(figure, downtime_text)


class DowntimeReportCliTests(unittest.TestCase):
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

    def test_downtime_report_flag_prints_text(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path, "--downtime-report"]),
                    0)
            text = out.getvalue()
            self.assertIn("Downtime report (generated ", text)
            self.assertIn("  api:", text)
            self.assertIn("24h: 30m 0s downtime", text)
            self.assertIn("  web:", text)
            self.assertIn("24h: 0s downtime", text)
            self.assertIn("  Total:", text)
            self.assertIn("24h: 30m 0s downtime", text)

    def test_downtime_out_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out_path = os.path.join(d, "downtime.txt")
            self.assertEqual(
                main(["--config", cfg_path, "--downtime-report",
                      "--downtime-out", out_path]), 0)
            with open(out_path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("Downtime report (generated ", text)
            self.assertIn("  api:", text)

    def test_downtime_out_without_flag_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            with self.assertRaises(SystemExit) as ctx:
                main(["--config", cfg_path, "--downtime-out",
                      os.path.join(d, "downtime.txt")])
            self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
