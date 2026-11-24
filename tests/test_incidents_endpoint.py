"""Tests for GET /api/incidents: recent incidents over HTTP."""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from healthcheck.checker import CheckResult
from healthcheck.server import incidents_snapshot, make_handler
from healthcheck.store import History


def _at(year, month, day, hour=12, minute=0):
    """Epoch seconds for a local-time date, so the tests work in any
    timezone: incident grouping uses the recorded timestamps."""
    return time.mktime((year, month, day, hour, minute, 0, 0, 0, -1))


def _result(name, ok, at, latency=10.0, **kwargs):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at, **kwargs)


class IncidentsSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        # api: one closed incident (down 12:08, recovered 12:09)
        # and one ongoing incident (down 12:10, no recovery).
        for minute in range(8):
            self.history.record(
                _result("api", True, _at(2026, 11, 5, 12, minute)))
        self.history.record(_result("api", False, _at(2026, 11, 5, 12, 8)))
        self.history.record(_result("api", True, _at(2026, 11, 5, 12, 9)))
        self.history.record(_result("api", False, _at(2026, 11, 5, 12, 10)))
        self.history.record(_result("api", False, _at(2026, 11, 5, 12, 11)))
        # db: two downs then a recovery — one closed incident.
        self.history.record(_result("db", True, _at(2026, 11, 5, 12, 0)))
        self.history.record(_result("db", False, _at(2026, 11, 5, 12, 1)))
        self.history.record(_result("db", False, _at(2026, 11, 5, 12, 2)))
        self.history.record(_result("db", True, _at(2026, 11, 5, 12, 3)))
        self.endpoints = [
            {"name": "api", "type": "http", "url": "http://example.com"},
            {"name": "db", "type": "tcp", "host": "127.0.0.1",
             "port": 5432},
            {"name": "quiet", "type": "tcp", "host": "127.0.0.1",
             "port": 5433},
        ]
        self.now = _at(2026, 11, 5, 12, 30)

    def test_snapshot_lists_every_endpoint_newest_first(self):
        snap = incidents_snapshot(self.history, self.endpoints,
                                  now=self.now)
        self.assertEqual([ep["name"] for ep in snap["endpoints"]],
                         ["api", "db", "quiet"])
        api = snap["endpoints"][0]
        # Newest first: the ongoing incident comes before the
        # closed one.
        self.assertEqual(len(api["incidents"]), 2)
        ongoing, closed = api["incidents"]
        self.assertIs(ongoing["ongoing"], True)
        self.assertIsNone(ongoing["end"])
        self.assertEqual(ongoing["start"], _at(2026, 11, 5, 12, 10))
        self.assertEqual(ongoing["checks"], 2)
        # Ongoing duration runs to now: 12:10 -> 12:30 is 20 minutes.
        self.assertEqual(ongoing["duration_s"], 1200.0)
        self.assertIs(closed["ongoing"], False)
        self.assertEqual(closed["start"], _at(2026, 11, 5, 12, 8))
        self.assertEqual(closed["end"], _at(2026, 11, 5, 12, 9))
        self.assertEqual(closed["checks"], 1)
        self.assertEqual(closed["duration_s"], 60.0)
        db = snap["endpoints"][1]
        self.assertEqual(len(db["incidents"]), 1)
        self.assertEqual(db["incidents"][0]["checks"], 2)
        self.assertEqual(db["incidents"][0]["duration_s"], 120.0)
        # An endpoint with no incidents is listed with an empty list.
        self.assertEqual(snap["endpoints"][2]["incidents"], [])

    def test_snapshot_filters_to_one_endpoint(self):
        snap = incidents_snapshot(self.history, self.endpoints,
                                  name="db", now=self.now)
        self.assertEqual([ep["name"] for ep in snap["endpoints"]], ["db"])
        self.assertEqual(len(snap["endpoints"][0]["incidents"]), 1)

    def test_snapshot_limit_caps_incidents_per_endpoint(self):
        snap = incidents_snapshot(self.history, self.endpoints,
                                  limit=1, now=self.now)
        api = snap["endpoints"][0]
        self.assertEqual(len(api["incidents"]), 1)
        # The one kept is the newest: the ongoing incident.
        self.assertIs(api["incidents"][0]["ongoing"], True)

    def test_snapshot_unknown_endpoint_raises(self):
        with self.assertRaises(ValueError):
            incidents_snapshot(self.history, self.endpoints,
                               name="nope", now=self.now)


class IncidentsApiTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        for minute in range(8):
            self.history.record(
                _result("api", True, _at(2026, 11, 5, 12, minute)))
        self.history.record(_result("api", False, _at(2026, 11, 5, 12, 8)))
        self.history.record(_result("api", True, _at(2026, 11, 5, 12, 9)))
        self.history.record(_result("api", False, _at(2026, 11, 5, 12, 10)))
        self.endpoints = [
            {"name": "api", "type": "http", "url": "http://example.com"},
            {"name": "quiet", "type": "tcp", "host": "127.0.0.1",
             "port": 5432},
        ]
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.history, self.endpoints))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}{path}") as resp:
            return (resp.status,
                    resp.headers.get("Content-Type"),
                    resp.read().decode("utf-8"))

    def test_incidents_served_over_http(self):
        status, content_type, body = self._get("/api/incidents")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        payload = json.loads(body)
        self.assertEqual([ep["name"] for ep in payload["endpoints"]],
                         ["api", "quiet"])
        api = payload["endpoints"][0]
        self.assertEqual(len(api["incidents"]), 2)
        # Newest first: the ongoing incident comes first, and its
        # duration runs to the request time.
        ongoing, closed = api["incidents"]
        self.assertIs(ongoing["ongoing"], True)
        self.assertIsNone(ongoing["end"])
        self.assertGreater(ongoing["duration_s"], 0)
        self.assertIs(closed["ongoing"], False)
        self.assertEqual(closed["duration_s"], 60.0)
        self.assertEqual(payload["endpoints"][1]["incidents"], [])

    def test_incidents_filter_and_limit(self):
        _, _, body = self._get("/api/incidents?endpoint=api&limit=1")
        payload = json.loads(body)
        self.assertEqual([ep["name"] for ep in payload["endpoints"]],
                         ["api"])
        self.assertEqual(len(payload["endpoints"][0]["incidents"]), 1)
        self.assertIs(payload["endpoints"][0]["incidents"][0]["ongoing"],
                      True)

    def test_bad_endpoint_is_a_400_with_a_json_error(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/incidents"
                "?endpoint=nope")
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("application/json",
                      ctx.exception.headers.get("Content-Type"))
        error = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertIn("unknown endpoint", error["error"])

    def test_bad_limit_is_a_400_with_a_json_error(self):
        for bad in ("zero", "0", "-1", "1.5"):
            with self.subTest(limit=bad):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/api/incidents"
                        f"?limit={bad}")
                self.assertEqual(ctx.exception.code, 400)
                self.assertIn("application/json",
                              ctx.exception.headers.get("Content-Type"))
                error = json.loads(ctx.exception.read().decode("utf-8"))
                self.assertIn("error", error)


if __name__ == "__main__":
    unittest.main()
