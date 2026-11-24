"""Tests for SLA reporting: monthly uptime vs target, breaches."""

import unittest

from healthcheck.checker import CheckResult
from healthcheck.sla import (
    breaches,
    monthly_detail,
    monthly_uptime,
    month_bounds,
    parse_month,
    render_sla_report,
    render_sla_report_csv,
    render_sla_report_json,
    sla_report,
    validate_sla_targets,
)
from healthcheck.store import History


def _at(year, month, day, hour=12, minute=0):
    """Epoch seconds for a local-time date, so the tests work in any
    timezone: month bounds are computed in local time too."""
    import time
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def _result(name, ok, at, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=10.0,
                       checked_at=at, **kwargs)


class MonthBoundsTests(unittest.TestCase):
    def test_november_bounds(self):
        start, end = month_bounds(2026, 11)
        self.assertEqual(start, _at(2026, 11, 1, 0, 0))
        self.assertEqual(end, _at(2026, 12, 1, 0, 0))

    def test_december_rolls_to_january(self):
        start, end = month_bounds(2026, 12)
        self.assertEqual(start, _at(2026, 12, 1, 0, 0))
        self.assertEqual(end, _at(2027, 1, 1, 0, 0))

    def test_bad_month_rejected(self):
        for month in (0, 13, -1):
            with self.assertRaises(ValueError):
                month_bounds(2026, month)


class MonthlyUptimeTests(unittest.TestCase):
    def test_counts_only_checks_inside_the_month(self):
        hist = History()
        hist.record(_result("api", True, _at(2026, 10, 31, 23, 59)))
        hist.record(_result("api", False, _at(2026, 11, 5)))
        hist.record(_result("api", True, _at(2026, 11, 20)))
        hist.record(_result("api", False, _at(2026, 12, 1, 0, 0)))
        pct, counted = monthly_uptime(hist, "api", 2026, 11)
        self.assertEqual(pct, 50.0)
        self.assertEqual(counted, 2)
        # October sees only the one October check.
        pct, counted = monthly_uptime(hist, "api", 2026, 10)
        self.assertEqual(pct, 100.0)
        self.assertEqual(counted, 1)

    def test_month_boundaries_are_inclusive_of_the_first_second(self):
        hist = History()
        hist.record(_result("api", True, _at(2026, 11, 1, 0, 0)))
        pct, counted = monthly_uptime(hist, "api", 2026, 11)
        self.assertEqual(pct, 100.0)
        self.assertEqual(counted, 1)

    def test_no_checks_in_month_is_none_not_zero(self):
        hist = History()
        hist.record(_result("api", True, _at(2026, 10, 15)))
        pct, counted = monthly_uptime(hist, "api", 2026, 11)
        self.assertIsNone(pct)
        self.assertEqual(counted, 0)

    def test_skips_unconfirmed_and_maintenance_excluded(self):
        hist = History()
        hist.record(_result("api", False, _at(2026, 11, 2),
                            skipped=True))
        hist.record(_result("api", False, _at(2026, 11, 3),
                            unconfirmed=True))
        hist.record(_result("api", False, _at(2026, 11, 4),
                            maintenance=True))
        hist.record(_result("api", True, _at(2026, 11, 5)))
        pct, counted = monthly_uptime(hist, "api", 2026, 11)
        self.assertEqual(pct, 100.0)
        self.assertEqual(counted, 1)

    def test_month_of_only_excluded_checks_is_none(self):
        hist = History()
        hist.record(_result("api", False, _at(2026, 11, 2),
                            skipped=True))
        pct, counted = monthly_uptime(hist, "api", 2026, 11)
        self.assertIsNone(pct)
        self.assertEqual(counted, 0)


class ValidateTargetTests(unittest.TestCase):
    def test_valid_targets_pass(self):
        validate_sla_targets([{"name": "a", "sla_target": 99.9},
                              {"name": "b", "sla_target": 100},
                              {"name": "c"}])  # no target is fine

    def test_zero_and_over_100_rejected(self):
        for bad in (0, -1, 100.1, 101):
            with self.assertRaises(ValueError):
                validate_sla_targets([{"name": "a", "sla_target": bad}])

    def test_non_number_rejected(self):
        for bad in ("99.9", None, True):
            # None means "no target" and is allowed; anything else
            # non-numeric is rejected.
            if bad is None:
                continue
            with self.assertRaises(ValueError):
                validate_sla_targets([{"name": "a", "sla_target": bad}])


class MonthlyDetailTests(unittest.TestCase):
    def _latency_result(self, name, ok, at, latency):
        return CheckResult(name=name, ok=ok, latency_ms=latency,
                           checked_at=at)

    def test_incidents_downtime_and_failed_checks(self):
        hist = History()
        # Two down checks in a row (one incident), recovery 90
        # seconds after the first failure; then a lone down check
        # with no recovery inside the month (second incident,
        # downtime accrues to the month end).
        base = _at(2026, 11, 10, 12, 0)
        hist.record(_result("api", True, base))
        hist.record(_result("api", False, base + 60))
        hist.record(_result("api", False, base + 120))
        hist.record(_result("api", True, base + 150))
        hist.record(_result("api", False, _at(2026, 11, 20, 12, 0)))
        detail = monthly_detail(hist, "api", 2026, 11)
        self.assertEqual(detail["incidents"], 2)
        self.assertEqual(detail["failed_checks"], 3)
        month_end = month_bounds(2026, 11)[1]
        expected = 90.0 + (month_end - _at(2026, 11, 20, 12, 0))
        self.assertEqual(detail["downtime_seconds"], round(expected, 1))

    def test_unconfirmed_and_skipped_do_not_start_incidents(self):
        hist = History()
        base = _at(2026, 11, 10, 12, 0)
        hist.record(_result("api", False, base, unconfirmed=True))
        hist.record(_result("api", False, base + 60, skipped=True))
        hist.record(_result("api", True, base + 120))
        detail = monthly_detail(hist, "api", 2026, 11)
        self.assertEqual(detail["incidents"], 0)
        self.assertEqual(detail["failed_checks"], 0)
        self.assertEqual(detail["downtime_seconds"], 0.0)

    def test_incident_starting_before_the_month_counts_clipped(self):
        hist = History()
        hist.record(_result("api", False, _at(2026, 10, 31, 23, 0)))
        hist.record(_result("api", False, _at(2026, 11, 1, 1, 0)))
        hist.record(_result("api", True, _at(2026, 11, 1, 2, 0)))
        detail = monthly_detail(hist, "api", 2026, 11)
        self.assertEqual(detail["incidents"], 1)
        # Downtime inside November only: midnight to the 02:00
        # recovery, not the hour before midnight.
        self.assertEqual(detail["downtime_seconds"], 2 * 3600.0)

    def test_latency_percentiles_over_the_month(self):
        hist = History()
        for i, latency in enumerate((10.0, 20.0, 30.0, 40.0)):
            hist.record(self._latency_result(
                "api", True, _at(2026, 11, 5, 12, i), latency))
        hist.record(self._latency_result(
            "api", True, _at(2026, 10, 5, 12, 0), 999.0))
        detail = monthly_detail(hist, "api", 2026, 11)
        self.assertEqual(detail["latency"],
                         {"p50": 20.0, "p95": 40.0, "p99": 40.0})

    def test_no_data_gives_zeroes_and_no_latency(self):
        detail = monthly_detail(History(), "api", 2026, 11)
        self.assertEqual(detail, {"incidents": 0, "failed_checks": 0,
                                  "downtime_seconds": 0.0,
                                  "latency": None})


class ReportTests(unittest.TestCase):
    def _history(self):
        hist = History()
        for day, ok in ((2, True), (3, True), (4, False), (5, True)):
            hist.record(_result("api", ok, _at(2026, 11, day)))
        for day, ok in ((2, True), (3, True)):
            hist.record(_result("web", ok, _at(2026, 11, day)))
        return hist

    def test_met_breach_and_no_target_rows(self):
        endpoints = [{"name": "api", "sla_target": 99.9},
                     {"name": "web", "sla_target": 50.0},
                     {"name": "plain"}]
        rows = sla_report(self._history(), endpoints, 2026, 11)
        self.assertEqual([r["name"] for r in rows], ["api", "web", "plain"])
        api, web, plain = rows
        self.assertEqual(api["uptime_pct"], 75.0)
        self.assertEqual(api["checks"], 4)
        self.assertIs(api["met"], False)
        self.assertEqual(api["shortfall_pct"], 24.9)
        self.assertEqual(web["uptime_pct"], 100.0)
        self.assertIs(web["met"], True)
        self.assertIsNone(web["shortfall_pct"])
        self.assertEqual(plain["uptime_pct"], None)
        self.assertIsNone(plain["target"])
        self.assertIsNone(plain["met"])

    def test_no_data_is_not_a_breach(self):
        hist = History()
        rows = sla_report(hist, [{"name": "api", "sla_target": 99.9}],
                          2026, 11)
        self.assertIsNone(rows[0]["met"])
        self.assertEqual(breaches(rows), [])

    def test_breaches_sorted_worst_first(self):
        endpoints = [{"name": "a", "sla_target": 99.0},
                     {"name": "b", "sla_target": 90.0}]
        hist = History()
        hist.record(_result("a", False, _at(2026, 11, 2)))
        hist.record(_result("a", True, _at(2026, 11, 3)))
        hist.record(_result("b", False, _at(2026, 11, 2)))
        hist.record(_result("b", False, _at(2026, 11, 3)))
        hist.record(_result("b", False, _at(2026, 11, 4)))
        hist.record(_result("b", True, _at(2026, 11, 5)))
        rows = sla_report(hist, endpoints, 2026, 11)
        self.assertEqual([r["name"] for r in breaches(rows)], ["b", "a"])

    def test_parse_month(self):
        self.assertEqual(parse_month("2026-11"), (2026, 11))
        for bad in ("2026-13", "2026", "nov", "2026-1x"):
            with self.assertRaises(ValueError):
                parse_month(bad)


class RenderTests(unittest.TestCase):
    def test_render_shows_verdicts_and_breach_line(self):
        endpoints = [{"name": "api", "sla_target": 99.9},
                     {"name": "web", "sla_target": 50.0}]
        hist = History()
        hist.record(_result("api", True, _at(2026, 11, 2)))
        hist.record(_result("api", False, _at(2026, 11, 3)))
        hist.record(_result("web", True, _at(2026, 11, 2)))
        text = render_sla_report(sla_report(hist, endpoints, 2026, 11),
                                 2026, 11)
        self.assertIn("SLA report 2026-11", text)
        self.assertIn("api: 50.0% (2 checks) — BREACH target 99.9%",
                      text)
        self.assertIn("web: 100.0% (1 checks) — MET target 50.0%", text)
        self.assertIn("1 breach: api", text)

    def test_render_no_breaches(self):
        endpoints = [{"name": "web", "sla_target": 50.0}]
        hist = History()
        hist.record(_result("web", True, _at(2026, 11, 2)))
        text = render_sla_report(sla_report(hist, endpoints, 2026, 11),
                                 2026, 11)
        self.assertIn("No breaches.", text)


class RenderExportTests(unittest.TestCase):
    def _report(self):
        endpoints = [{"name": "api", "sla_target": 99.9},
                     {"name": "web"}]
        hist = History()
        hist.record(_result("api", True, _at(2026, 11, 2, 12, 0)))
        hist.record(_result("api", False, _at(2026, 11, 2, 12, 1)))
        hist.record(_result("api", True, _at(2026, 11, 2, 12, 2)))
        hist.record(_result("web", True, _at(2026, 11, 2, 12, 0)))
        return sla_report(hist, endpoints, 2026, 11)

    def test_rows_carry_incident_and_latency_detail(self):
        api, web = self._report()
        self.assertEqual(api["incidents"], 1)
        self.assertEqual(api["failed_checks"], 1)
        self.assertEqual(api["downtime_seconds"], 60.0)
        self.assertEqual(api["latency"],
                         {"p50": 10.0, "p95": 10.0, "p99": 10.0})
        self.assertEqual(web["incidents"], 0)
        self.assertEqual(web["downtime_seconds"], 0.0)

    def test_csv_has_one_row_per_endpoint(self):
        import csv
        import io
        rows = list(csv.DictReader(
            io.StringIO(render_sla_report_csv(self._report(), 2026, 11))))
        self.assertEqual(len(rows), 2)
        api, web = rows
        self.assertEqual(api["month"], "2026-11")
        self.assertEqual(api["endpoint"], "api")
        self.assertEqual(api["uptime_pct"], "66.7")
        self.assertEqual(api["checks"], "3")
        self.assertEqual(api["failed_checks"], "1")
        self.assertEqual(api["incidents"], "1")
        self.assertEqual(api["downtime_seconds"], "60.0")
        self.assertEqual(api["latency_p50_ms"], "10.0")
        self.assertEqual(api["sla_target"], "99.9")
        self.assertEqual(api["met"], "false")
        self.assertEqual(api["shortfall_pct"], "33.2")
        self.assertEqual(web["uptime_pct"], "100.0")
        self.assertEqual(web["sla_target"], "")
        self.assertEqual(web["met"], "")

    def test_json_round_trips_and_lists_breaches(self):
        import json
        payload = json.loads(
            render_sla_report_json(self._report(), 2026, 11))
        self.assertEqual(payload["month"], "2026-11")
        self.assertEqual(payload["breaches"], ["api"])
        api = payload["endpoints"][0]
        self.assertEqual(api["name"], "api")
        self.assertEqual(api["incidents"], 1)
        self.assertEqual(api["downtime_seconds"], 60.0)
        self.assertEqual(api["latency"],
                         {"p50": 10.0, "p95": 10.0, "p99": 10.0})
        self.assertIs(api["met"], False)


if __name__ == "__main__":
    unittest.main()
