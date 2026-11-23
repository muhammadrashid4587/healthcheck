"""Tests for maintenance windows: paused checks, no alerts, no uptime hit."""

import json
import os
import socket
import tempfile
import unittest
from datetime import datetime

from healthcheck.checker import CheckResult, maintenance_result
from healthcheck.cli import check_all, load_config
from healthcheck.flap import FlapDetector
from healthcheck.maintenance import (active_window, in_maintenance,
                                     parse_maintenance_window,
                                     summarize_window, summarize_windows)
from healthcheck.server import latency_sparkline, render_dashboard
from healthcheck.store import History


def _at(year, month, day, hour, minute):
    """Epoch seconds for a local date/time. 2026-11-21 is a Saturday."""
    return datetime(year, month, day, hour, minute).timestamp()


# Saturday 2026-11-21 02:30, inside a Sat 02:00–04:00 window.
SAT_0230 = _at(2026, 11, 21, 2, 30)
SAT_0130 = _at(2026, 11, 21, 1, 30)   # before the window
SAT_0430 = _at(2026, 11, 21, 4, 30)   # after the window
SUN_0230 = _at(2026, 11, 22, 2, 30)   # Sunday, not a Saturday window
SAT_WINDOW = {"days": "sat", "start": "02:00", "end": "04:00"}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _result(name, ok, latency=10.0, at=1_700_000_000.0, maintenance=False):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at, maintenance=maintenance)


def _write_config(directory, endpoints):
    path = os.path.join(directory, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"endpoints": endpoints}, fh)
    return path


class _RecordingAlerter:
    """Stands in for WebhookAlerter; records what it was told about."""

    def __init__(self):
        self.notified = []

    def notify(self, result):
        self.notified.append(result.name)
        return None


class WindowMatchingTests(unittest.TestCase):
    def test_daily_window_matches_any_day(self):
        window = {"start": "02:00", "end": "03:00"}
        self.assertTrue(in_maintenance([window], now=SAT_0230))
        self.assertTrue(in_maintenance([window], now=SUN_0230))
        self.assertFalse(in_maintenance([window], now=SAT_0130))

    def test_day_list_matches_only_those_days(self):
        self.assertTrue(in_maintenance([SAT_WINDOW], now=SAT_0230))
        self.assertFalse(in_maintenance([SAT_WINDOW], now=SUN_0230))
        self.assertFalse(in_maintenance([SAT_WINDOW], now=SAT_0130))
        self.assertFalse(in_maintenance([SAT_WINDOW], now=SAT_0430))

    def test_weekdays_and_weekend_shortcuts(self):
        weekdays = {"days": "weekdays", "start": "02:00", "end": "03:00"}
        weekend = {"days": "weekend", "start": "02:00", "end": "03:00"}
        # 2026-11-20 is a Friday, 2026-11-21 a Saturday.
        self.assertTrue(in_maintenance([weekdays],
                                       now=_at(2026, 11, 20, 2, 30)))
        self.assertFalse(in_maintenance([weekdays], now=SAT_0230))
        self.assertTrue(in_maintenance([weekend], now=SAT_0230))
        self.assertFalse(in_maintenance([weekend],
                                        now=_at(2026, 11, 20, 2, 30)))

    def test_overnight_window_spans_midnight(self):
        window = {"days": "sat", "start": "22:00", "end": "02:00"}
        # Saturday night, after the start: in.
        self.assertTrue(in_maintenance([window],
                                       now=_at(2026, 11, 21, 23, 30)))
        # Sunday 01:30, still the Saturday window's tail: in.
        self.assertTrue(in_maintenance([window],
                                       now=_at(2026, 11, 22, 1, 30)))
        # Sunday 03:00, past the end: out. Saturday 21:00: not yet.
        self.assertFalse(in_maintenance([window],
                                        now=_at(2026, 11, 22, 3, 0)))
        self.assertFalse(in_maintenance([window],
                                        now=_at(2026, 11, 21, 21, 0)))

    def test_window_end_is_exclusive(self):
        # 04:00 is the end of a 02:00–04:00 window: out.
        self.assertFalse(in_maintenance([SAT_WINDOW],
                                        now=_at(2026, 11, 21, 4, 0)))
        self.assertTrue(in_maintenance([SAT_WINDOW],
                                       now=_at(2026, 11, 21, 3, 59)))

    def test_no_windows_is_never_maintenance(self):
        self.assertIsNone(active_window(None, now=SAT_0230))
        self.assertIsNone(active_window([], now=SAT_0230))
        self.assertFalse(in_maintenance([], now=SAT_0230))

    def test_active_window_returns_the_matching_window(self):
        window = active_window([SAT_WINDOW,
                                {"days": "sun", "start": "02:00",
                                 "end": "03:00"}], now=SAT_0230)
        self.assertEqual(window["start_text"], "02:00")
        self.assertEqual(window["days"], frozenset({5}))

    def test_day_list_and_case_insensitive_days(self):
        window = parse_maintenance_window(
            {"days": ["Sat", "WED"], "start": "02:00", "end": "03:00"})
        self.assertEqual(window["days"], frozenset({2, 5}))


class WindowSummaryTests(unittest.TestCase):
    def test_summaries(self):
        self.assertEqual(
            summarize_window(parse_maintenance_window(SAT_WINDOW)),
            "Sat 02:00–04:00")
        self.assertEqual(
            summarize_window(parse_maintenance_window(
                {"start": "02:00", "end": "03:00"})),
            "daily 02:00–03:00")
        self.assertEqual(
            summarize_window(parse_maintenance_window(
                {"days": "weekdays", "start": "22:00", "end": "02:00"})),
            "Mon–Fri 22:00–02:00")
        self.assertEqual(
            summarize_window(parse_maintenance_window(
                {"days": ["mon", "wed"], "start": "02:00",
                 "end": "03:00"})),
            "Mon, Wed 02:00–03:00")

    def test_summarize_windows_joins_and_dashes(self):
        self.assertEqual(summarize_windows(None), "-")
        self.assertEqual(summarize_windows([]), "-")
        self.assertEqual(summarize_windows([SAT_WINDOW,
                                            {"days": "sun", "start": "02:00",
                                             "end": "03:00"}]),
                         "Sat 02:00–04:00; Sun 02:00–03:00")


class WindowValidationTests(unittest.TestCase):
    def test_bad_time_formats_rejected(self):
        for bad in ("2:00", "24:00", "02:60", "02", 200, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_maintenance_window({"start": bad, "end": "03:00"})

    def test_zero_length_window_rejected(self):
        with self.assertRaises(ValueError):
            parse_maintenance_window({"start": "02:00", "end": "02:00"})

    def test_missing_start_or_end_rejected(self):
        with self.assertRaises(ValueError):
            parse_maintenance_window({"start": "02:00"})
        with self.assertRaises(ValueError):
            parse_maintenance_window({"end": "02:00"})

    def test_bad_days_rejected(self):
        for bad in ("funday", ["mon", "funday"], [], 5, {"mon"}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_maintenance_window({"days": bad, "start": "02:00",
                                          "end": "03:00"})

    def test_unknown_key_and_non_object_rejected(self):
        with self.assertRaises(ValueError):
            parse_maintenance_window({"start": "02:00", "end": "03:00",
                                      "tz": "UTC"})
        with self.assertRaises(ValueError):
            parse_maintenance_window("02:00-03:00")

    def test_config_rejects_bad_windows(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, [
                {"name": "web", "type": "http", "url": "http://x",
                 "maintenance_windows": [
                     {"days": "sat", "start": "02:00", "end": "02:00"}]},
            ])
            with self.assertRaises(ValueError):
                load_config(path)

    def test_config_rejects_non_list_windows(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, [
                {"name": "web", "type": "http", "url": "http://x",
                 "maintenance_windows": SAT_WINDOW},
            ])
            with self.assertRaises(ValueError):
                load_config(path)

    def test_valid_windows_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_config(d, [
                {"name": "web", "type": "http", "url": "http://x",
                 "maintenance_windows": [SAT_WINDOW]},
            ])
            cfg = load_config(path)
        self.assertEqual(cfg["endpoints"][0]["maintenance_windows"],
                         [SAT_WINDOW])


class CheckRoundTests(unittest.TestCase):
    def test_endpoint_in_window_is_not_checked(self):
        # Nothing listens on this port: if the check ran, it would
        # fail. In the window it must not run at all.
        port = _free_port()
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": port, "maintenance_windows": [SAT_WINDOW]},
        ]
        history = History()
        alerter = _RecordingAlerter()
        detector = FlapDetector()
        check_all(endpoints, history, alerter=alerter,
                  flap_detector=detector, now=SAT_0230)
        entry = history.current("db")
        self.assertTrue(entry["maintenance"])
        self.assertFalse(entry["ok"])
        self.assertIn("Sat 02:00", entry["error"])
        # No alert, no flap state: the check never happened.
        self.assertEqual(alerter.notified, [])
        self.assertEqual(detector.consecutive_failures("db"), 0)
        self.assertFalse(detector.is_down("db"))
        # A history of only maintenance is no data, not 0% uptime.
        self.assertIsNone(history.uptime_pct("db"))
        self.assertIsNone(history.uptime_pct_window(
            "db", 1000, now=SAT_0230 + 60))

    def test_endpoint_checked_outside_window(self):
        port = _free_port()
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": port, "maintenance_windows": [SAT_WINDOW]},
        ]
        history = History()
        check_all(endpoints, history, now=SAT_0130)
        entry = history.current("db")
        self.assertFalse(entry.get("maintenance", False))
        self.assertFalse(entry["ok"])  # nothing listens: a real failure

    def test_maintenance_does_not_reset_flap_state(self):
        # Two failed rounds outside the window, one maintenance round,
        # then a third failure: the outage still confirms on the
        # third failure, because the maintenance round never ran.
        port = _free_port()
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": port, "maintenance_windows": [SAT_WINDOW]},
        ]
        history = History()
        detector = FlapDetector()
        check_all(endpoints, history, flap_detector=detector,
                  now=SAT_0130)
        check_all(endpoints, history, flap_detector=detector,
                  now=SAT_0130)
        check_all(endpoints, history, flap_detector=detector,
                  now=SAT_0230)  # maintenance: no observation
        self.assertEqual(detector.consecutive_failures("db"), 2)
        check_all(endpoints, history, flap_detector=detector,
                  now=SAT_0430)
        self.assertTrue(detector.is_down("db"))

    def test_dependent_skipped_while_dependency_in_maintenance(self):
        db_port, api_port = _free_port(), _free_port()
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": db_port, "maintenance_windows": [SAT_WINDOW]},
            {"name": "api", "type": "tcp", "host": "127.0.0.1",
             "port": api_port, "depends_on": "db"},
        ]
        history = History()
        check_all(endpoints, history, now=SAT_0230)
        self.assertTrue(history.current("db")["maintenance"])
        api_entry = history.current("api")
        self.assertTrue(api_entry["skipped"])
        self.assertIn("MAINTENANCE", api_entry["error"])
        self.assertIsNone(history.uptime_pct("api"))

    def test_maintenance_result_helper(self):
        result = maintenance_result("db", "Sat 02:00–04:00")
        self.assertTrue(result.maintenance)
        self.assertFalse(result.ok)
        self.assertIn("Sat 02:00", result.error)


class UptimeExcludesMaintenanceTests(unittest.TestCase):
    def test_uptime_counts_only_real_checks(self):
        history = History()
        history.record(_result("db", True, at=100.0))
        history.record(_result("db", False, at=200.0, maintenance=True))
        history.record(_result("db", False, at=300.0))
        self.assertEqual(history.uptime_pct("db"), 50.0)  # 1/2, not 1/3
        self.assertEqual(
            history.uptime_pct_window("db", 1000, now=400.0), 50.0)

    def test_maintenance_entries_not_in_sparkline(self):
        history = History()
        history.record(_result("db", True, latency=10.0, at=100.0))
        history.record(_result("db", False, latency=0.0, at=200.0,
                               maintenance=True))
        history.record(_result("db", True, latency=20.0, at=300.0))
        svg = latency_sparkline(history, "db")
        points = svg.split('points="')[1].split('"')[0].split()
        self.assertEqual(len(points), 2)  # the maintenance entry is out

    def test_history_round_trips_maintenance_flag(self):
        history = History()
        history.record(_result("db", False, at=100.0, maintenance=True))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.json")
            history.save(path)
            loaded = History.load(path)
        self.assertTrue(loaded.current("db")["maintenance"])
        self.assertIsNone(loaded.uptime_pct("db"))


class DashboardMaintenanceTests(unittest.TestCase):
    def test_dashboard_shows_maintenance_and_windows(self):
        history = History()
        history.record(_result("db", True, at=1_700_000_000.0))
        history.record(_result("db", False, at=1_700_000_001.0,
                               maintenance=True))
        endpoints = [
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": 5432, "maintenance_windows": [SAT_WINDOW]},
        ]
        page = render_dashboard(history, endpoints)
        self.assertIn("MAINTENANCE", page)
        self.assertIn("Maintenance", page)
        self.assertIn("Sat 02:00–04:00", page)
        # Maintenance is not downtime: the one real check still
        # counts, so uptime is 100.0%, not dragged down by the
        # maintenance round.
        self.assertIn("<td class=\"maintenance\">MAINTENANCE</td>", page)
        self.assertIn("<td>100.0%</td>", page)

    def test_dashboard_shows_dash_when_no_windows(self):
        history = History()
        history.record(_result("web", True))
        page = render_dashboard(history, [{"name": "web", "type": "http",
                                           "url": "http://example.com"}])
        self.assertIn("<td>-</td>", page)  # Maintenance column


if __name__ == "__main__":
    unittest.main()
