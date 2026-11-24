"""Tests for the --incidents CLI export of recorded incident history."""

import csv
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from healthcheck.checker import CheckResult
from healthcheck.cli import main, save_history
from healthcheck.incidents import (
    incidents_report,
    render_incidents_report,
    render_incidents_report_csv,
    render_incidents_report_json,
)
from healthcheck.store import History


def _at(year, month, day, hour=12, minute=0, second=0):
    """Epoch seconds for a local-time date, so the tests work in any
    timezone: incident grouping uses the recorded timestamps."""
    return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))


def _result(name, ok, at, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=10.0,
                       checked_at=at, **kwargs)


def _history():
    """api: one closed incident (2 failed checks, recovered after
    90 s) and one ongoing incident (1 failed check, never
    recovered); web: one passing check, no incidents."""
    hist = History()
    base = _at(2026, 11, 10, 12, 0)
    hist.record(_result("api", True, base))
    hist.record(_result("api", False, base + 60))
    hist.record(_result("api", False, base + 120))
    hist.record(_result("api", True, base + 150))
    hist.record(_result("api", False, _at(2026, 11, 20, 12, 0)))
    hist.record(_result("web", True, _at(2026, 11, 10, 12, 0)))
    return hist


class IncidentsReportTests(unittest.TestCase):
    def test_counts_and_totals(self):
        now = _at(2026, 11, 20, 13, 0)
        report = incidents_report(_history(), [{"name": "api"},
                                               {"name": "web"}], now=now)
        api, web = report["endpoints"]
        self.assertEqual(api["total_incidents"], 2)
        self.assertEqual(api["ongoing_incidents"], 1)
        # Closed incident: 90 s. Ongoing: noon to 13:00, one hour.
        self.assertEqual(api["total_downtime_seconds"], 90.0 + 3600.0)
        self.assertEqual(web["total_incidents"], 0)
        self.assertEqual(web["ongoing_incidents"], 0)
        self.assertEqual(web["total_downtime_seconds"], 0.0)
        self.assertEqual(report["totals"], {
            "total_incidents": 2,
            "ongoing_incidents": 1,
            "total_downtime_seconds": 90.0 + 3600.0,
        })

    def test_ongoing_incident_has_end_null_and_duration_to_now(self):
        now = _at(2026, 11, 20, 13, 0)
        report = incidents_report(_history(), [{"name": "api"}], now=now)
        # Newest first: the ongoing incident comes first.
        ongoing, closed = report["endpoints"][0]["incidents"]
        self.assertIsNone(ongoing["end"])
        self.assertTrue(ongoing["ongoing"])
        self.assertEqual(ongoing["duration_s"], 3600.0)
        self.assertEqual(ongoing["checks"], 1)
        self.assertEqual(closed["end"], _at(2026, 11, 10, 12, 2, 30))
        self.assertFalse(closed["ongoing"])
        self.assertEqual(closed["duration_s"], 90.0)
        self.assertEqual(closed["checks"], 2)

    def test_report_matches_recent_incidents(self):
        # The export must report exactly what the dashboard, the
        # APIs, and the static page show — same source, same data.
        now = _at(2026, 11, 20, 13, 0)
        hist = _history()
        report = incidents_report(hist, [{"name": "api"},
                                         {"name": "web"}], now=now)
        for row in report["endpoints"]:
            self.assertEqual(
                row["incidents"],
                hist.recent_incidents(row["name"], limit=None, now=now))

    def test_endpoint_filter_narrows_rows(self):
        now = _at(2026, 11, 20, 13, 0)
        report = incidents_report(_history(), [{"name": "api"},
                                               {"name": "web"}],
                                  now=now, name="api")
        self.assertEqual([row["name"] for row in report["endpoints"]],
                         ["api"])
        self.assertEqual(report["endpoints"][0]["total_incidents"], 2)
        self.assertEqual(report["totals"]["total_incidents"], 2)

    def test_limit_caps_rows_newest_first(self):
        now = _at(2026, 11, 20, 13, 0)
        report = incidents_report(_history(), [{"name": "api"},
                                               {"name": "web"}],
                                  now=now, limit=1)
        api, web = report["endpoints"]
        # Only the newest incident survives: the ongoing one.
        self.assertEqual(len(api["incidents"]), 1)
        self.assertTrue(api["incidents"][0]["ongoing"])
        self.assertEqual(api["total_incidents"], 1)
        self.assertEqual(api["total_downtime_seconds"], 3600.0)
        self.assertEqual(web["total_incidents"], 0)
        self.assertEqual(report["totals"]["total_incidents"], 1)
        self.assertEqual(report["totals"]["total_downtime_seconds"],
                         3600.0)

    def test_combined_endpoint_and_limit(self):
        now = _at(2026, 11, 20, 13, 0)
        report = incidents_report(_history(), [{"name": "api"},
                                               {"name": "web"}],
                                  now=now, name="api", limit=1)
        self.assertEqual(len(report["endpoints"]), 1)
        self.assertEqual(report["endpoints"][0]["name"], "api")
        self.assertEqual(report["endpoints"][0]["total_incidents"], 1)
        self.assertEqual(report["totals"]["total_incidents"], 1)
        # An endpoint with no incidents still lists, empty.
        report = incidents_report(_history(), [{"name": "api"},
                                               {"name": "web"}],
                                  now=now, name="web", limit=1)
        self.assertEqual(len(report["endpoints"]), 1)
        self.assertEqual(report["endpoints"][0]["incidents"], [])
        self.assertEqual(report["totals"]["total_incidents"], 0)

    def test_unknown_endpoint_raises(self):
        with self.assertRaisesRegex(ValueError,
                                    "unknown endpoint: nope"):
            incidents_report(_history(), [{"name": "api"}],
                             now=_at(2026, 11, 20, 13, 0), name="nope")

    def test_totals_sum_the_rows_actually_exported(self):
        # The totals must stay consistent with the unfiltered
        # export (same rows, same totals) and with any filtered
        # one (totals are the sum of the exported rows, not of
        # the whole history).
        now = _at(2026, 11, 20, 13, 0)
        endpoints = [{"name": "api"}, {"name": "web"}]
        for kwargs in ({}, {"name": "api"}, {"limit": 1},
                       {"name": "api", "limit": 1}):
            report = incidents_report(_history(), endpoints, now=now,
                                      **kwargs)
            self.assertEqual(
                report["totals"]["total_incidents"],
                sum(r["total_incidents"] for r in report["endpoints"]))
            self.assertEqual(
                report["totals"]["ongoing_incidents"],
                sum(r["ongoing_incidents"] for r in report["endpoints"]))
            self.assertEqual(
                report["totals"]["total_downtime_seconds"],
                round(sum(r["total_downtime_seconds"]
                          for r in report["endpoints"]), 1))

    def test_no_incidents_is_zeroes_not_an_error(self):
        report = incidents_report(History(), [{"name": "api"}],
                                  now=_at(2026, 11, 20, 13, 0))
        self.assertEqual(report["endpoints"][0]["incidents"], [])
        self.assertEqual(report["totals"]["total_incidents"], 0)
        self.assertEqual(report["totals"]["total_downtime_seconds"], 0.0)


class RenderTests(unittest.TestCase):
    def test_text_names_counts_and_incidents(self):
        now = _at(2026, 11, 20, 13, 0)
        text = render_incidents_report(
            incidents_report(_history(), [{"name": "api"},
                                          {"name": "web"}], now=now))
        self.assertIn("Incident report (generated 2026-11-20 13:00:00)",
                      text)
        self.assertIn("api: 2 incidents (1 ongoing), 3690.0s total "
                      "downtime", text)
        self.assertIn("2026-11-10 12:01:00 – 2026-11-10 12:02:30 "
                      "(90.0s, 2 checks)", text)
        self.assertIn("2026-11-20 12:00:00 – ongoing "
                      "(3600.0s, 1 check)", text)
        self.assertIn("web: 0 incidents, 0.0s total downtime", text)
        self.assertIn("2 incidents total (1 ongoing), 3690.0s total "
                      "downtime", text)

    def test_csv_has_one_row_per_incident(self):
        now = _at(2026, 11, 20, 13, 0)
        rows = list(csv.DictReader(io.StringIO(
            render_incidents_report_csv(
                incidents_report(_history(), [{"name": "api"},
                                              {"name": "web"}], now=now)))))
        self.assertEqual(len(rows), 2)
        # Newest first: the ongoing incident is the first row.
        ongoing, closed = rows
        self.assertEqual(ongoing["endpoint"], "api")
        self.assertEqual(ongoing["start"],
                         str(_at(2026, 11, 20, 12, 0)))
        self.assertEqual(ongoing["end"], "")
        self.assertEqual(ongoing["ongoing"], "true")
        self.assertEqual(ongoing["duration_s"], "3600.0")
        self.assertEqual(ongoing["checks"], "1")
        self.assertEqual(closed["end"],
                         str(_at(2026, 11, 10, 12, 2, 30)))
        self.assertEqual(closed["ongoing"], "false")
        self.assertEqual(closed["duration_s"], "90.0")
        self.assertEqual(closed["checks"], "2")

    def test_csv_with_no_incidents_is_header_only(self):
        csv_text = render_incidents_report_csv(
            incidents_report(History(), [{"name": "api"}],
                             now=_at(2026, 11, 20, 13, 0)))
        self.assertEqual(
            csv_text,
            "endpoint,start,end,ongoing,duration_s,checks\r\n")

    def test_json_round_trips_with_totals(self):
        now = _at(2026, 11, 20, 13, 0)
        payload = json.loads(render_incidents_report_json(
            incidents_report(_history(), [{"name": "api"},
                                          {"name": "web"}], now=now)))
        self.assertEqual(payload["generated_at"], now)
        self.assertEqual(payload["totals"]["total_incidents"], 2)
        self.assertEqual(payload["totals"]["ongoing_incidents"], 1)
        api, web = payload["endpoints"]
        self.assertEqual(api["name"], "api")
        self.assertEqual(api["total_incidents"], 2)
        self.assertEqual(len(api["incidents"]), 2)
        self.assertIsNone(api["incidents"][0]["end"])
        self.assertTrue(api["incidents"][0]["ongoing"])
        self.assertEqual(web["incidents"], [])


class CliTests(unittest.TestCase):
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
        save_history(_history(), hist_path)
        return cfg_path

    def test_incidents_flag_prints_text(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path, "--incidents"]), 0)
            text = out.getvalue()
            self.assertIn("Incident report (generated ", text)
            self.assertIn("api: 2 incidents", text)
            self.assertIn("web: 0 incidents", text)
            self.assertIn("2 incidents total", text)

    def test_incidents_json_format(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path, "--incidents",
                          "--incidents-format", "json"]), 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["totals"]["total_incidents"], 2)
            api = payload["endpoints"][0]
            self.assertEqual(api["name"], "api")
            self.assertEqual(api["total_incidents"], 2)
            self.assertIsNone(api["incidents"][0]["end"])

    def test_incidents_csv_to_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out_path = os.path.join(d, "incidents.csv")
            self.assertEqual(
                main(["--config", cfg_path, "--incidents",
                      "--incidents-format", "csv",
                      "--incidents-out", out_path]), 0)
            with open(out_path, encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["endpoint"], "api")
            self.assertEqual(rows[0]["end"], "")

    def test_incidents_endpoint_filter(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path, "--incidents",
                          "--incidents-format", "json",
                          "--endpoint", "api"]), 0)
            payload = json.loads(out.getvalue())
            self.assertEqual([row["name"] for row in
                              payload["endpoints"]], ["api"])
            self.assertEqual(payload["totals"]["total_incidents"], 2)

    def test_incidents_limit_caps_rows(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path, "--incidents",
                          "--incidents-format", "json",
                          "--limit", "1"]), 0)
            payload = json.loads(out.getvalue())
            api, web = payload["endpoints"]
            self.assertEqual(len(api["incidents"]), 1)
            self.assertTrue(api["incidents"][0]["ongoing"])
            self.assertEqual(payload["totals"]["total_incidents"], 1)

    def test_incidents_endpoint_and_limit_combined(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path, "--incidents",
                          "--incidents-format", "json",
                          "--endpoint", "api", "--limit", "1"]), 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(len(payload["endpoints"]), 1)
            self.assertEqual(payload["endpoints"][0]["name"], "api")
            self.assertEqual(
                len(payload["endpoints"][0]["incidents"]), 1)
            self.assertEqual(payload["totals"]["total_incidents"], 1)

    def test_incidents_unknown_endpoint_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            with self.assertRaises(SystemExit) as ctx:
                main(["--config", cfg_path, "--incidents",
                      "--endpoint", "nope"])
            self.assertEqual(ctx.exception.code, 2)

    def test_incidents_non_positive_limit_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            for bad in ("0", "-3"):
                with self.assertRaises(SystemExit) as ctx:
                    main(["--config", cfg_path, "--incidents",
                          "--limit", bad])
                self.assertEqual(ctx.exception.code, 2)

    def test_incidents_non_integer_limit_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            with self.assertRaises(SystemExit) as ctx:
                main(["--config", cfg_path, "--incidents",
                      "--limit", "abc"])
            self.assertEqual(ctx.exception.code, 2)

    def test_endpoint_and_limit_without_flag_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = self._setup(d)
            for extra in (["--endpoint", "api"], ["--limit", "1"]):
                with self.assertRaises(SystemExit) as ctx:
                    main(["--config", cfg_path] + extra)
                self.assertEqual(ctx.exception.code, 2)

    def test_incidents_format_without_flag_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"endpoints": []}, fh)
            with self.assertRaises(SystemExit):
                main(["--config", cfg_path,
                      "--incidents-format", "json"])

    def test_incidents_with_no_history_file_reports_zeroes(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({"endpoints": [{"name": "api", "type": "tcp",
                                          "host": "127.0.0.1", "port": 1,
                                          "timeout": 1}]}, fh)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(
                    main(["--config", cfg_path, "--incidents"]), 0)
            self.assertIn("api: 0 incidents", out.getvalue())
            self.assertIn("0 incidents total", out.getvalue())


if __name__ == "__main__":
    unittest.main()
