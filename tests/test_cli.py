"""Tests for history persistence wiring in the CLI."""

import json
import os
import tempfile
import unittest

from healthcheck import __version__
from healthcheck.checker import CheckResult
from healthcheck.cli import (
    history_path,
    load_history,
    main,
    save_history,
)
from healthcheck.store import History


class _Args:
    def __init__(self, history_file=None):
        self.history_file = history_file


def _result(name, ok, latency=10.0, at=1_700_000_000.0):
    return CheckResult(name=name, ok=ok, latency_ms=latency, checked_at=at)


class HistoryPathTests(unittest.TestCase):
    def test_flag_beats_config(self):
        cfg = {"history_file": "from-config.json"}
        self.assertEqual(history_path(cfg, _Args("from-flag.json")),
                         "from-flag.json")

    def test_config_used_when_no_flag(self):
        cfg = {"history_file": "from-config.json"}
        self.assertEqual(history_path(cfg, _Args(None)), "from-config.json")

    def test_none_when_neither_set(self):
        self.assertIsNone(history_path({}, _Args(None)))


class LoadSaveTests(unittest.TestCase):
    def test_missing_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as d:
            hist = load_history(os.path.join(d, "nope.json"))
        self.assertEqual(hist.names(), [])

    def test_no_path_starts_empty(self):
        self.assertEqual(load_history(None).names(), [])

    def test_save_then_load_round_trip(self):
        hist = History()
        hist.record(_result("api", True, at=100.0))
        hist.record(_result("api", False, at=200.0))
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.json")
            save_history(hist, path)
            loaded = load_history(path)
        self.assertEqual(loaded.uptime_pct("api"), 50.0)
        self.assertEqual(loaded.current("api")["checked_at"], 200.0)

    def test_save_with_no_path_is_a_noop(self):
        save_history(History(), None)  # must not raise

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            loaded = load_history(path)
        self.assertEqual(loaded.names(), [])

    def test_wrong_shape_starts_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "history.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(["api"], fh)  # a list, not a name->entries dict
            loaded = load_history(path)
        self.assertEqual(loaded.names(), [])


class OnceModePersistenceTests(unittest.TestCase):
    def test_once_writes_history_file(self):
        # A TCP check against a closed localhost port: deterministic
        # DOWN result, no network access needed.
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            hist_path = os.path.join(d, "history.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "interval_seconds": 30,
                    "history": 10,
                    "history_file": hist_path,
                    "endpoints": [
                        {"name": "closed", "type": "tcp",
                         "host": "127.0.0.1", "port": 1, "timeout": 1},
                    ],
                }, fh)
            self.assertEqual(
                main(["--config", cfg_path, "--once"]), 0)
            with open(hist_path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("closed", data)
            self.assertEqual(len(data["closed"]), 1)
            self.assertFalse(data["closed"][0]["ok"])

    def test_once_flag_overrides_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            cfg_hist = os.path.join(d, "from-config.json")
            flag_hist = os.path.join(d, "from-flag.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "history_file": cfg_hist,
                    "endpoints": [
                        {"name": "closed", "type": "tcp",
                         "host": "127.0.0.1", "port": 1, "timeout": 1},
                    ],
                }, fh)
            self.assertEqual(
                main(["--config", cfg_path, "--once",
                      "--history-file", flag_hist]), 0)
            self.assertTrue(os.path.exists(flag_hist))
            self.assertFalse(os.path.exists(cfg_hist))

    def test_once_without_history_file_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "endpoints": [
                        {"name": "closed", "type": "tcp",
                         "host": "127.0.0.1", "port": 1, "timeout": 1},
                    ],
                }, fh)
            self.assertEqual(main(["--config", cfg_path, "--once"]), 0)
            self.assertEqual(os.listdir(d), ["config.json"])


class VersionTests(unittest.TestCase):
    def test_version_is_0_9_0(self):
        self.assertEqual(__version__, "0.9.0")


if __name__ == "__main__":
    unittest.main()
