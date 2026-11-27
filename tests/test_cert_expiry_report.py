"""Tests for the --cert-expiry-report text export: every endpoint's
certificate expiry from the persisted history — days left, soonest
first, with a summary line — rendered from the same helpers the
dashboard reads, so the report can never disagree with it."""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from healthcheck.checker import CheckResult
from healthcheck.cli import main, save_history
from healthcheck.server import (cert_expiry_snapshot,
                                endpoint_cert_expiry_text,
                                render_cert_expiry_report)
from healthcheck.store import History

NOW = 1_700_000_000.0
DAY = 24 * 3600


def _result(name, ok, at, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=10.0,
                       checked_at=at, **kwargs)


def _history():
    """Seed: stale's cert lapsed 2 days ago; edge expires in 5 days
    inside its 7-day warning window; api expires in 12 days inside
    its 30-day window; far expires in 20 days, outside its 7-day
    window; web expires in 89 days; db never showed a certificate
    (plain TCP)."""
    hist = History()
    hist.record(_result("stale", True, NOW,
                        cert_expires_at=NOW - 2 * DAY))
    hist.record(_result("edge", True, NOW,
                        cert_expires_at=NOW + 5 * DAY))
    hist.record(_result("api", True, NOW,
                        cert_expires_at=NOW + 12 * DAY))
    hist.record(_result("far", True, NOW,
                        cert_expires_at=NOW + 20 * DAY))
    hist.record(_result("web", True, NOW,
                        cert_expires_at=NOW + 89 * DAY))
    hist.record(_result("db", True, NOW))
    return hist


ENDPOINTS = [
    {"name": "stale", "type": "http", "url": "https://example.com"},
    {"name": "edge", "type": "http", "url": "https://example.com",
     "cert_expiry_warn_days": 7},
    {"name": "api", "type": "http", "url": "https://example.com"},
    {"name": "far", "type": "http", "url": "https://example.com",
     "cert_expiry_warn_days": 7},
    {"name": "web", "type": "http", "url": "https://example.com"},
    {"name": "db", "type": "tcp", "host": "127.0.0.1", "port": 5432},
]


def _snapshot():
    return cert_expiry_snapshot(_history(), ENDPOINTS, now=NOW)


def _report():
    return render_cert_expiry_report(_snapshot())


class CertExpirySnapshotTests(unittest.TestCase):
    def test_rows_sorted_soonest_first_unknown_last(self):
        snapshot = _snapshot()
        self.assertEqual(
            [row["name"] for row in snapshot["endpoints"]],
            ["stale", "edge", "api", "far", "web", "db"])

    def test_row_fields(self):
        snapshot = _snapshot()
        by_name = {row["name"]: row for row in snapshot["endpoints"]}
        self.assertEqual(by_name["stale"]["days_remaining"], -2)
        self.assertEqual(by_name["edge"]["days_remaining"], 5)
        self.assertEqual(by_name["edge"]["warn_days"], 7)
        self.assertEqual(by_name["api"]["warn_days"], 30)
        self.assertIsNone(by_name["db"]["expires_at"])
        self.assertIsNone(by_name["db"]["days_remaining"])
        self.assertEqual(by_name["db"]["warn_days"], 30)


class RenderCertExpiryReportTests(unittest.TestCase):
    def test_header_and_per_endpoint_lines(self):
        text = _report()
        self.assertIn("Certificate expiry report (generated ", text)
        self.assertIn("  stale: expired (warn 30d)", text)
        self.assertIn("  edge: 5d (soon) (warn 7d)", text)
        self.assertIn("  api: 12d (soon) (warn 30d)", text)
        self.assertIn("  far: 20d (warn 7d)", text)
        self.assertIn("  web: 89d (warn 30d)", text)
        self.assertIn("  db: - (no certificate seen)", text)

    def test_sorted_soonest_first_in_the_text(self):
        text = _report()
        positions = [text.index(f"  {name}:")
                     for name in ("stale", "edge", "api", "far", "web",
                                  "db")]
        self.assertEqual(positions, sorted(positions))

    def test_summary_counts(self):
        text = _report()
        self.assertIn("  Summary: 1 expired, 2 expiring soon, "
                      "2 ok, 1 unknown", text)

    def test_matches_dashboard_text(self):
        # Every line's cert text is the dashboard's own text for
        # that endpoint, so the report can never disagree with it.
        history = _history()
        text = _report()
        for ep in ENDPOINTS:
            shown = endpoint_cert_expiry_text(history, ep["name"], ep,
                                              now=NOW)
            self.assertIn(f"  {ep['name']}: {shown}", text)

    def test_all_unknown_reports_zeroes(self):
        empty = History()
        snapshot = cert_expiry_snapshot(empty, ENDPOINTS, now=NOW)
        text = render_cert_expiry_report(snapshot)
        self.assertIn("  Summary: 0 expired, 0 expiring soon, "
                      "0 ok, 6 unknown", text)
        self.assertIn("  api: - (no certificate seen)", text)


class CertExpiryReportCliTests(unittest.TestCase):
    def _setup(self, d):
        cfg_path = os.path.join(d, "config.json")
        hist_path = os.path.join(d, "history.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({
                "history_file": hist_path,
                "endpoints": [
                    {"name": "api", "type": "tcp", "host": "127.0.0.1",
                     "port": 1, "timeout": 1},
                    {"name": "web", "type": "tcp", "host": "127.0.0.1",
                     "port": 1, "timeout": 1},
                ],
            }, fh)
        hist = History()
        now = time.time()
        # 12 days plus an hour: the report runs a fraction of a
        # second later, so a bare 12 days would floor to 11.
        hist.record(_result("api", True, now,
                            cert_expires_at=now + 12 * DAY + 3600))
        hist.record(_result("web", True, now))
        save_history(hist, hist_path)
        return cfg_path

    def test_cert_expiry_report_flag_prints_text(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path,
                          "--cert-expiry-report"]),
                    0)
            text = out.getvalue()
            self.assertIn("Certificate expiry report (generated ", text)
            self.assertIn("  api: 12d (soon) (warn 30d)", text)
            self.assertIn("  web: - (no certificate seen)", text)
            self.assertIn("  Summary: 0 expired, 1 expiring soon, "
                          "0 ok, 1 unknown", text)
            self.assertLess(text.index("  api:"),
                            text.index("  web:"))

    def test_cert_expiry_out_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out_path = os.path.join(d, "certs.txt")
            self.assertEqual(
                main(["--config", cfg_path, "--cert-expiry-report",
                      "--cert-expiry-out", out_path]), 0)
            with open(out_path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("Certificate expiry report (generated ", text)
            self.assertIn("  api: 12d (soon) (warn 30d)", text)

    def test_cert_expiry_out_without_flag_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            with self.assertRaises(SystemExit) as ctx:
                main(["--config", cfg_path, "--cert-expiry-out",
                      os.path.join(d, "certs.txt")])
            self.assertEqual(ctx.exception.code, 2)

    def test_report_with_no_history_file_reports_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "endpoints": [
                        {"name": "api", "type": "tcp",
                         "host": "127.0.0.1", "port": 1, "timeout": 1},
                    ],
                }, fh)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path,
                          "--cert-expiry-report"]),
                    0)
            text = out.getvalue()
            self.assertIn("  api: - (no certificate seen)", text)
            self.assertIn("  Summary: 0 expired, 0 expiring soon, "
                          "0 ok, 1 unknown", text)
