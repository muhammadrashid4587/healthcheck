"""Per-endpoint check history and uptime math.

History is in-memory, bounded per endpoint. It can be saved to and
loaded from a JSON file (atomic write via rename + fsync).
"""

from __future__ import annotations

import json
import os
from collections import deque


class History:
    """Bounded per-endpoint history of check results."""

    def __init__(self, maxlen=100):
        self.maxlen = maxlen
        self._checks = {}  # name -> deque[dict]

    def record(self, result):
        """Append one CheckResult; returns the stored entry."""
        entry = {
            "ok": result.ok,
            "latency_ms": round(result.latency_ms, 1),
            "status_code": result.status_code,
            "error": result.error,
            "checked_at": result.checked_at,
        }
        self._checks.setdefault(result.name, deque(maxlen=self.maxlen)).append(entry)
        return entry

    def names(self):
        return sorted(self._checks)

    def recent(self, name, n=10):
        """Last n entries for an endpoint, oldest first."""
        return list(self._checks.get(name, []))[-n:]

    def current(self, name):
        """Most recent entry for an endpoint, or None if never checked."""
        checks = self._checks.get(name)
        return checks[-1] if checks else None

    def uptime_pct(self, name):
        """Share of recorded checks that were up, or None if no checks."""
        checks = self._checks.get(name)
        if not checks:
            return None
        up = sum(1 for c in checks if c["ok"])
        return round(100.0 * up / len(checks), 1)

    def save(self, path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({k: list(v) for k, v in self._checks.items()}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @classmethod
    def load(cls, path, maxlen=100):
        hist = cls(maxlen=maxlen)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for name, entries in data.items():
            hist._checks[name] = deque(entries, maxlen=maxlen)
        return hist
