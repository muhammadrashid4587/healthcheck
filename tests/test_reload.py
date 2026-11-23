"""Tests for config hot-reload (SIGHUP)."""

import json
import os
import signal
import tempfile
import threading
import types
import unittest

from healthcheck.cli import (install_reload_handler, monitor_loop,
                             reload_config)
from healthcheck.store import History


def _args():
    return types.SimpleNamespace(history_file=None, webhook_url=None,
                                 flap_threshold=None)


def _write(path, cfg):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


def _tcp(name, port=1):
    return {"name": name, "type": "tcp", "host": "127.0.0.1",
            "port": port, "timeout": 1}


class ReloadConfigTests(unittest.TestCase):
    def test_valid_reload_returns_new_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"endpoints": [_tcp("a")]})
            from healthcheck.cli import load_config
            current = load_config(path)
            _write(path, {"endpoints": [_tcp("b")]})
            new = reload_config(path, _args(), current)
            self.assertIsNot(new, current)
            self.assertEqual([ep["name"] for ep in new["endpoints"]],
                             ["b"])

    def test_bad_json_keeps_current(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"endpoints": [_tcp("a")]})
            from healthcheck.cli import load_config
            current = load_config(path)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            self.assertIs(reload_config(path, _args(), current), current)

    def test_missing_file_keeps_current(self):
        current = {"endpoints": [_tcp("a")]}
        self.assertIs(reload_config("/nonexistent/config.json",
                                    _args(), current), current)

    def test_invalid_config_keeps_current(self):
        # A bad sla_target fails validation: the running config stays.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"endpoints": [_tcp("a")]})
            from healthcheck.cli import load_config
            current = load_config(path)
            _write(path, {"endpoints": [
                dict(_tcp("a"), sla_target=101)]})
            self.assertIs(reload_config(path, _args(), current), current)


class InstallHandlerTests(unittest.TestCase):
    def test_sighup_sets_the_event(self):
        event = threading.Event()
        previous = signal.getsignal(signal.SIGHUP)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        self.assertTrue(install_reload_handler(event))
        os.kill(os.getpid(), signal.SIGHUP)
        self.assertTrue(event.is_set())


class MonitorLoopReloadTests(unittest.TestCase):
    def test_loop_picks_up_new_endpoints(self):
        # A TCP check against a closed localhost port: deterministic
        # DOWN result, no network access needed.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"interval_seconds": 1,
                          "endpoints": [_tcp("a")]})
            from healthcheck.cli import load_config
            cfg = load_config(path)
            _write(path, {"interval_seconds": 1,
                          "endpoints": [_tcp("b")]})
            history = History()
            stop = threading.Event()
            event = threading.Event()
            event.set()  # reload before the first round
            final = monitor_loop(cfg, history, None, _args(), stop,
                                 config_path=path,
                                 reload_requested=event,
                                 max_rounds=1)
            self.assertEqual([ep["name"] for ep in
                              final["endpoints"]], ["b"])
            self.assertEqual(history.names(), ["b"])

    def test_loop_keeps_old_config_on_bad_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"interval_seconds": 1,
                          "endpoints": [_tcp("a")]})
            from healthcheck.cli import load_config
            cfg = load_config(path)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            history = History()
            stop = threading.Event()
            event = threading.Event()
            event.set()
            final = monitor_loop(cfg, history, None, _args(), stop,
                                 config_path=path,
                                 reload_requested=event,
                                 max_rounds=1)
            self.assertIs(final, cfg)
            self.assertEqual(history.names(), ["a"])

    def test_loop_keeps_live_endpoints_in_sync(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"interval_seconds": 1,
                          "endpoints": [_tcp("a")]})
            from healthcheck.cli import load_config
            cfg = load_config(path)
            _write(path, {"interval_seconds": 1,
                          "endpoints": [_tcp("b"), _tcp("c")]})
            history = History()
            live = []
            stop = threading.Event()
            event = threading.Event()
            event.set()
            monitor_loop(cfg, history, None, _args(), stop,
                         config_path=path, reload_requested=event,
                         max_rounds=1, live_endpoints=live)
            self.assertEqual([ep["name"] for ep in live], ["b", "c"])

    def test_real_sighup_reloads_the_loop(self):
        # End to end: the handler installed on this (main) thread,
        # a SIGHUP sent mid-loop, the loop picks up the new config.
        previous = signal.getsignal(signal.SIGHUP)
        self.addCleanup(signal.signal, signal.SIGHUP, previous)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"interval_seconds": 0.05,
                          "endpoints": [_tcp("a")]})
            from healthcheck.cli import load_config
            cfg = load_config(path)
            _write(path, {"interval_seconds": 0.05,
                          "endpoints": [_tcp("b")]})
            event = threading.Event()
            self.assertTrue(install_reload_handler(event))
            timer = threading.Timer(
                0.15, lambda: os.kill(os.getpid(), signal.SIGHUP))
            timer.start()
            try:
                history = History()
                final = monitor_loop(cfg, history, None, _args(),
                                     threading.Event(),
                                     config_path=path,
                                     reload_requested=event,
                                     max_rounds=40)
            finally:
                timer.cancel()
            self.assertEqual([ep["name"] for ep in
                              final["endpoints"]], ["b"])
            self.assertIn("b", history.names())


if __name__ == "__main__":
    unittest.main()
