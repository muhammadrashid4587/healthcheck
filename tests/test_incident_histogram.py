"""Tests for the per-incident duration histogram in the
--incidents export."""

import json
import time
import unittest

from healthcheck.checker import CheckResult
from healthcheck.incidents import (
    incidents_report,
    render_incidents_report,
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


def _closed_incident(hist, name, start, duration_s):
    """One closed incident: up, down at `start`, recovered
    `duration_s` later."""
    hist.record(_result(name, True, start - 60))
    hist.record(_result(name, False, start))
    hist.record(_result(name, True, start + duration_s))


def _history():
    """api: one incident in every duration bucket — 30 s, exactly
    60 s (the 1m boundary), 10 min, 30 min, 2 h, 12 h, and one
    ongoing incident over a day old; web: no incidents."""
    hist = History()
    _closed_incident(hist, "api", _at(2026, 11, 1, 12, 0), 30)
    _closed_incident(hist, "api", _at(2026, 11, 2, 12, 0), 60)
    _closed_incident(hist, "api", _at(2026, 11, 3, 12, 0), 600)
    _closed_incident(hist, "api", _at(2026, 11, 4, 12, 0), 1800)
    _closed_incident(hist, "api", _at(2026, 11, 5, 12, 0), 7200)
    _closed_incident(hist, "api", _at(2026, 11, 6, 12, 0), 43200)
    # Ongoing incident: down Nov 10, still down at the reference
    # time Nov 20 13:00 — over ten days, so it lands in "> 24h".
    hist.record(_result("api", True, _at(2026, 11, 10, 11, 59)))
    hist.record(_result("api", False, _at(2026, 11, 10, 12, 0)))
    hist.record(_result("web", True, _at(2026, 11, 10, 12, 0)))
    return hist


NOW = _at(2026, 11, 20, 13, 0)


def _counts(histogram):
    return {bucket["label"]: bucket["count"] for bucket in histogram}


class HistogramTests(unittest.TestCase):
    def test_one_incident_per_bucket(self):
        counts = _counts(_history().incident_duration_histogram(
            "api", now=NOW))
        self.assertEqual(counts, {
            "< 1m": 1, "1m – 5m": 1, "5m – 15m": 1, "15m – 1h": 1,
            "1h – 6h": 1, "6h – 24h": 1, "> 24h": 1,
        })

    def test_bucket_edges_are_log_spaced(self):
        histogram = _history().incident_duration_histogram(
            "api", now=NOW)
        self.assertEqual(
            [(b["min_s"], b["max_s"]) for b in histogram],
            [(0, 60), (60, 300), (300, 900), (900, 3600),
             (3600, 21600), (21600, 86400), (86400, None)])

    def test_lower_edge_is_inclusive(self):
        # The 60 s incident lands in "1m – 5m", not "< 1m".
        counts = _counts(_history().incident_duration_histogram(
            "api", now=NOW))
        self.assertEqual(counts["< 1m"], 1)
        self.assertEqual(counts["1m – 5m"], 1)

    def test_ongoing_incident_duration_runs_to_now(self):
        # At an earlier `now` the ongoing incident is 23 h old, so
        # it joins the 12 h closed incident in "6h – 24h" instead
        # of "> 24h".
        counts = _counts(_history().incident_duration_histogram(
            "api", now=_at(2026, 11, 11, 11, 0)))
        self.assertEqual(counts["> 24h"], 0)
        self.assertEqual(counts["6h – 24h"], 2)

    def test_limit_caps_the_histogram_like_the_list(self):
        # Newest first: the ongoing incident alone survives limit 1.
        counts = _counts(_history().incident_duration_histogram(
            "api", limit=1, now=NOW))
        self.assertEqual(sum(counts.values()), 1)
        self.assertEqual(counts["> 24h"], 1)

    def test_no_incidents_is_all_zeroes(self):
        histogram = _history().incident_duration_histogram(
            "web", now=NOW)
        self.assertEqual(len(histogram), 7)
        self.assertTrue(all(b["count"] == 0 for b in histogram))


class ReportHistogramTests(unittest.TestCase):
    def test_row_histogram_matches_its_incidents(self):
        report = incidents_report(_history(), [{"name": "api"},
                                               {"name": "web"}], now=NOW)
        api, web = report["endpoints"]
        self.assertEqual(sum(b["count"] for b in
                             api["duration_histogram"]),
                         api["total_incidents"])
        self.assertEqual(_counts(api["duration_histogram"]), {
            "< 1m": 1, "1m – 5m": 1, "5m – 15m": 1, "15m – 1h": 1,
            "1h – 6h": 1, "6h – 24h": 1, "> 24h": 1,
        })
        self.assertTrue(all(b["count"] == 0 for b in
                            web["duration_histogram"]))

    def test_filtered_export_histograms_only_exported_incidents(self):
        # Limit 1 keeps only the ongoing incident, so the histogram
        # must show one incident in "> 24h" and nothing elsewhere —
        # it can never disagree with the rows actually exported.
        report = incidents_report(_history(), [{"name": "api"}],
                                  now=NOW, limit=1)
        row = report["endpoints"][0]
        self.assertEqual(len(row["incidents"]), 1)
        self.assertEqual(_counts(row["duration_histogram"])["> 24h"], 1)
        self.assertEqual(sum(b["count"] for b in
                             row["duration_histogram"]), 1)

    def test_text_report_shows_histogram_per_endpoint(self):
        text = render_incidents_report(
            incidents_report(_history(), [{"name": "api"},
                                          {"name": "web"}], now=NOW))
        self.assertIn("    Duration histogram:\n"
                      "      < 1m: 1\n"
                      "      1m – 5m: 1\n"
                      "      5m – 15m: 1\n"
                      "      15m – 1h: 1\n"
                      "      1h – 6h: 1\n"
                      "      6h – 24h: 1\n"
                      "      > 24h: 1", text)
        # One histogram only: the endpoint with no incidents gets
        # no histogram section.
        self.assertEqual(text.count("Duration histogram:"), 1)

    def test_json_round_trips_histogram(self):
        payload = json.loads(render_incidents_report_json(
            incidents_report(_history(), [{"name": "api"}],
                             now=NOW)))
        row = payload["endpoints"][0]
        self.assertEqual(_counts(row["duration_histogram"]), {
            "< 1m": 1, "1m – 5m": 1, "5m – 15m": 1, "15m – 1h": 1,
            "1h – 6h": 1, "6h – 24h": 1, "> 24h": 1,
        })


if __name__ == "__main__":
    unittest.main()
