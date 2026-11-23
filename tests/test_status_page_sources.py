"""Tests for status_page_sources in the config file (0.22.0)."""

import json
import os
import tempfile
import threading
import types
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from healthcheck.checker import CheckResult
from healthcheck.cli import (load_config, load_status_page_source,
                             main, merged_status_page_sources,
                             monitor_loop, reload_config)
from healthcheck.server import (make_handler, render_dashboard,
                                status_snapshot)
from healthcheck.store import History

BASE = 1_700_000_000.0


def _tcp(name, port=1):
    return {"name": name, "type": "tcp", "host": "127.0.0.1",
            "port": port, "timeout": 1}


def _args(sources=None):
    return types.SimpleNamespace(history_file=None, webhook_url=None,
                                 flap_threshold=None,
                                 status_page_source=sources)


def _write(path, cfg):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)


def _result(name, ok, latency=12.5, at=BASE):
    return CheckResult(name=name, ok=ok, latency_ms=latency,
                       checked_at=at)


def _second_history(path):
    hist = History()
    hist.record(_result("other-web", True, at=BASE))
    hist.record(_result("other-db", False, at=BASE))
    hist.save(path)
    return hist


SNAPSHOT = {
    "generated_at": BASE + 200,
    "endpoints": [
        {"name": "remote-web", "status": "UP", "depends_on": None,
         "maintenance_windows": [],
         "uptime_pct": 99.5, "uptime_24h_pct": 100.0,
         "uptime_7d_pct": 99.0, "latency_ms": 12.5,
         "latency_percentiles": {"p50": 11.0, "p95": 20.0, "p99": 30.0},
         "last_checked_at": BASE + 180},
    ],
}


def _make_server(payload):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class ConfigSourcesTests(unittest.TestCase):
    def test_config_file_sources_load(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {
                "endpoints": [_tcp("a")],
                "status_page_sources": [
                    {"label": "file-src", "path": "other.json"},
                    {"label": "remote-src",
                     "url": "http://127.0.0.1:8080"},
                ]})
            cfg = load_config(path)
            self.assertEqual(cfg["status_page_sources"], [
                {"label": "file-src", "path": "other.json"},
                {"label": "remote-src", "url": "http://127.0.0.1:8080"},
            ])

    def test_bad_entries_are_warned_and_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {
                "endpoints": [_tcp("a")],
                "status_page_sources": [
                    {"label": "good", "path": "other.json"},
                    "not an object",
                    {"path": "no-label.json"},
                    {"label": "", "path": "empty-label.json"},
                    {"label": "both", "path": "x.json",
                     "url": "http://example.com"},
                    {"label": "neither"},
                    {"label": "bad-url", "url": "ftp://example.com"},
                    {"label": "empty-path", "path": "  "},
                    {"label": "unknown-key", "path": "x.json",
                     "extra": 1},
                    {"label": "good", "path": "duplicate.json"},
                ]})
            with self.assertLogs("healthcheck", level="WARNING") as logs:
                cfg = load_config(path)
            # Only the first good entry survives; the monitor still
            # loads with its endpoints intact.
            self.assertEqual(cfg["status_page_sources"], [
                {"label": "good", "path": "other.json"}])
            self.assertEqual([ep["name"] for ep in cfg["endpoints"]],
                             ["a"])
            joined = "\n".join(logs.output)
            self.assertIn("skipping", joined)
            self.assertIn("duplicated", joined)

    def test_non_list_sources_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"endpoints": [_tcp("a")],
                          "status_page_sources": "other.json"})
            with self.assertLogs("healthcheck", level="WARNING"):
                cfg = load_config(path)
            self.assertEqual(cfg["status_page_sources"], [])

    def test_no_sources_key_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"endpoints": [_tcp("a")]})
            cfg = load_config(path)
            self.assertEqual(cfg["status_page_sources"], [])


class MergeTests(unittest.TestCase):
    def test_cli_extends_config(self):
        cfg = {"status_page_sources": [
            {"label": "file-src", "path": "other.json"}]}
        merged = merged_status_page_sources(
            cfg, _args(["cli-src=http://127.0.0.1:9999"]))
        self.assertEqual(merged, [
            {"label": "file-src", "path": "other.json"},
            {"label": "cli-src", "url": "http://127.0.0.1:9999"},
        ])

    def test_cli_overrides_same_label(self):
        cfg = {"status_page_sources": [
            {"label": "shared", "path": "old.json"},
            {"label": "kept", "path": "kept.json"}]}
        merged = merged_status_page_sources(
            cfg, _args(["shared=new.json"]))
        self.assertEqual(merged, [
            {"label": "shared", "path": "new.json"},
            {"label": "kept", "path": "kept.json"},
        ])

    def test_cli_url_overrides_config_path(self):
        cfg = {"status_page_sources": [
            {"label": "shared", "path": "old.json"}]}
        merged = merged_status_page_sources(
            cfg, _args(["shared=http://127.0.0.1:9999"]))
        self.assertEqual(merged, [
            {"label": "shared", "url": "http://127.0.0.1:9999"}])

    def test_bad_cli_spec_raises(self):
        cfg = {"status_page_sources": []}
        with self.assertRaises(ValueError):
            merged_status_page_sources(cfg, _args(["no-equals-here"]))

    def test_no_cli_flags_keeps_config(self):
        cfg = {"status_page_sources": [
            {"label": "file-src", "path": "other.json"}]}
        self.assertEqual(merged_status_page_sources(cfg, _args()),
                         [{"label": "file-src", "path": "other.json"}])


class CliStatusPageSourcesTests(unittest.TestCase):
    def test_status_page_renders_config_file_source(self):
        with tempfile.TemporaryDirectory() as d:
            second = os.path.join(d, "other.json")
            _second_history(second)
            cfg_path = os.path.join(d, "config.json")
            _write(cfg_path, {
                "endpoints": [_tcp("a")],
                "status_page_sources": [
                    {"label": "second-site", "path": second}]})
            out_path = os.path.join(d, "status.html")
            rc = main(["--config", cfg_path, "--status-page", out_path])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("<h2>local</h2>", page)
            self.assertIn("<h2>second-site</h2>", page)
            self.assertIn("other-web", page)
            self.assertIn("other-db", page)

    def test_cli_flag_overrides_config_source(self):
        with tempfile.TemporaryDirectory() as d:
            first = os.path.join(d, "first.json")
            _second_history(first)
            override = os.path.join(d, "override.json")
            hist = History()
            hist.record(_result("override-web", True, at=BASE))
            hist.save(override)
            cfg_path = os.path.join(d, "config.json")
            _write(cfg_path, {
                "endpoints": [_tcp("a")],
                "status_page_sources": [
                    {"label": "shared", "path": first}]})
            out_path = os.path.join(d, "status.html")
            rc = main(["--config", cfg_path, "--status-page", out_path,
                       "--status-page-source", f"shared={override}"])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("override-web", page)
            self.assertNotIn("other-web", page)

    def test_config_file_remote_source(self):
        server, base = _make_server(SNAPSHOT)
        try:
            with tempfile.TemporaryDirectory() as d:
                cfg_path = os.path.join(d, "config.json")
                _write(cfg_path, {
                    "endpoints": [_tcp("a")],
                    "status_page_sources": [
                        {"label": "remote-site", "url": base}]})
                out_path = os.path.join(d, "status.html")
                rc = main(["--config", cfg_path, "--status-page",
                           out_path])
                self.assertEqual(rc, 0)
                with open(out_path, encoding="utf-8") as fh:
                    page = fh.read()
                self.assertIn("<h2>remote-site</h2>", page)
                self.assertIn("remote-web", page)
                self.assertIn("99.5%", page)
        finally:
            server.shutdown()
            server.server_close()

    def test_missing_config_source_file_renders_empty(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.json")
            _write(cfg_path, {
                "endpoints": [_tcp("a")],
                "status_page_sources": [
                    {"label": "gone",
                     "path": os.path.join(d, "nope.json")}]})
            out_path = os.path.join(d, "status.html")
            rc = main(["--config", cfg_path, "--status-page", out_path])
            self.assertEqual(rc, 0)
            with open(out_path, encoding="utf-8") as fh:
                page = fh.read()
            self.assertIn("<h2>gone</h2>", page)
            self.assertIn("No endpoints configured.", page)

    def test_load_status_page_source_local(self):
        with tempfile.TemporaryDirectory() as d:
            second = os.path.join(d, "other.json")
            _second_history(second)
            label, hist, endpoints = load_status_page_source(
                "second-site", second, {})
            self.assertEqual(label, "second-site")
            self.assertEqual([ep["name"] for ep in endpoints],
                             ["other-db", "other-web"])
            self.assertEqual(hist.uptime_pct("other-web"), 100.0)


class ReloadSourcesTests(unittest.TestCase):
    def test_reload_with_bad_entry_skips_it_and_keeps_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"endpoints": [_tcp("a")],
                          "status_page_sources": [
                              {"label": "good", "path": "a.json"}]})
            current = load_config(path)
            _write(path, {"endpoints": [_tcp("a")],
                          "status_page_sources": [
                              {"label": "good", "path": "a.json"},
                              {"label": "bad", "url": "ftp://x"}]})
            with self.assertLogs("healthcheck", level="WARNING"):
                new = reload_config(path, _args(), current)
            # A bad source entry is skipped, not a failed reload:
            # the new config still takes effect.
            self.assertIsNot(new, current)
            self.assertEqual(new["status_page_sources"], [
                {"label": "good", "path": "a.json"}])

    def test_reload_picks_up_new_sources(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"endpoints": [_tcp("a")]})
            current = load_config(path)
            _write(path, {"endpoints": [_tcp("a")],
                          "status_page_sources": [
                              {"label": "new", "path": "b.json"}]})
            new = reload_config(path, _args(), current)
            self.assertEqual(new["status_page_sources"], [
                {"label": "new", "path": "b.json"}])

    def test_loop_keeps_live_sources_in_sync_on_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            _write(path, {"interval_seconds": 1,
                          "endpoints": [_tcp("a")]})
            cfg = load_config(path)
            _write(path, {"interval_seconds": 1,
                          "endpoints": [_tcp("a")],
                          "status_page_sources": [
                              {"label": "new", "path": "b.json"}]})
            history = History()
            live = []
            event = threading.Event()
            event.set()  # reload before the first round
            monitor_loop(cfg, history, None, _args(), threading.Event(),
                         config_path=path, reload_requested=event,
                         max_rounds=1, live_sources=live)
            self.assertEqual(live, [{"label": "new", "path": "b.json"}])


class DashboardAndApiTests(unittest.TestCase):
    def setUp(self):
        self.history = History()
        self.history.record(_result("web", True))
        self.endpoints = [{"name": "web", "type": "http",
                           "url": "http://example.com"}]
        self.sources = [
            {"label": "second-site", "path": "history-second.json"},
            {"label": "remote-site", "url": "http://127.0.0.1:9999"},
        ]

    def test_snapshot_lists_sources_when_given(self):
        snap = status_snapshot(self.history, self.endpoints,
                               status_page_sources=self.sources)
        self.assertEqual(snap["status_page_sources"], self.sources)

    def test_snapshot_has_no_sources_key_when_not_given(self):
        snap = status_snapshot(self.history, self.endpoints)
        self.assertNotIn("status_page_sources", snap)

    def test_snapshot_sources_are_json_serializable(self):
        snap = status_snapshot(self.history, self.endpoints,
                               status_page_sources=self.sources)
        parsed = json.loads(json.dumps(snap))
        self.assertEqual(parsed, snap)

    def test_dashboard_lists_sources(self):
        page = render_dashboard(self.history, self.endpoints,
                                status_page_sources=self.sources)
        self.assertIn("Status page sources", page)
        self.assertIn("second-site", page)
        self.assertIn("history-second.json", page)
        self.assertIn("remote-site", page)
        self.assertIn("http://127.0.0.1:9999", page)

    def test_dashboard_has_no_sources_section_without_sources(self):
        page = render_dashboard(self.history, self.endpoints)
        self.assertNotIn("Status page sources", page)

    def test_dashboard_escapes_source_labels(self):
        page = render_dashboard(
            self.history, self.endpoints,
            status_page_sources=[{"label": "<b>x</b>",
                                  "path": "other.json"}])
        self.assertNotIn("<b>x</b>", page)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", page)

    def test_api_status_served_over_http_lists_sources(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.history, self.endpoints,
                         status_page_sources=self.sources))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/status") as resp:
                payload = json.load(resp)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(payload["status_page_sources"], self.sources)

    def test_dashboard_served_over_http_lists_sources(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.history, self.endpoints,
                         status_page_sources=self.sources))
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/") \
                    as resp:
                body = resp.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
        self.assertIn("Status page sources", body)
        self.assertIn("second-site", body)


if __name__ == "__main__":
    unittest.main()
