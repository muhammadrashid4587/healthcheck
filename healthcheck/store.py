"""Per-endpoint check history and uptime math.

History is in-memory, bounded per endpoint. It can be saved to and
loaded from a JSON file (atomic write via rename + fsync).
"""

from __future__ import annotations

import json
import os
import time
from collections import deque


class History:
    """Bounded per-endpoint history of check results."""

    def __init__(self, maxlen=100):
        self.maxlen = maxlen
        self._checks = {}  # name -> deque[dict]

    def record(self, result):
        """Append one CheckResult; returns the stored entry.

        Skipped checks are recorded too (so the dashboard and history
        show *why* an endpoint was not checked), with their declared
        dependency, but they never count in uptime math.
        """
        entry = {
            "ok": result.ok,
            "latency_ms": round(result.latency_ms, 1),
            "status_code": result.status_code,
            "error": result.error,
            "checked_at": result.checked_at,
            "skipped": result.skipped,
            "depends_on": result.depends_on,
            "unconfirmed": result.unconfirmed,
            "maintenance": result.maintenance,
            # Retries spent on this check (0 when the first attempt
            # decided it). Entries written before retries existed
            # carry no key and count as 0 wherever it is read.
            "retries": getattr(result, "retries", 0),
            # Epoch second the peer's TLS certificate expires, for
            # https checks that could read it (None otherwise).
            # Entries written before cert expiry existed carry no
            # key and count as None wherever it is read.
            "cert_expires_at": getattr(result, "cert_expires_at", None),
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

    def _group_incidents(self, name, now=None):
        """All outage incidents for an endpoint, newest first.

        Consecutive confirmed-down checks form one incident: it
        starts at the first down check and ends at the first
        passing check after it (the recovery). An incident still
        in progress at the end of the recorded history has `end`
        None and `ongoing` True, and its duration runs to `now`
        (the current time when not given). Skips, unconfirmed
        failures and maintenance rounds are not outages and
        neither start nor end an incident — a skip in the middle
        of a run of downs does not split it, and a recovery must
        be a real passing check. Each incident is {"start", "end",
        "checks", "ongoing", "duration_s"}. The same grouping
        the static status page and the SLA export use, so every
        surface reports the same incidents.
        """
        entries = self.recent(name, self.maxlen)
        incidents = []
        current = None
        for entry in entries:
            down = (not entry["ok"]
                    and not entry.get("skipped", False)
                    and not entry.get("unconfirmed", False)
                    and not entry.get("maintenance", False))
            up = (entry["ok"]
                  and not entry.get("skipped", False)
                  and not entry.get("unconfirmed", False)
                  and not entry.get("maintenance", False))
            if down:
                if current is None:
                    current = {"start": entry["checked_at"], "end": None,
                               "checks": 0, "ongoing": True}
                current["checks"] += 1
            elif up:
                if current is not None:
                    current["end"] = entry["checked_at"]
                    current["ongoing"] = False
                    incidents.append(current)
                    current = None
        if current is not None:
            incidents.append(current)
        ref = time.time() if now is None else now
        for incident in incidents:
            end = incident["end"] if incident["end"] is not None else ref
            incident["duration_s"] = round(end - incident["start"], 1)
        return incidents[::-1]

    def recent_incidents(self, name, limit=5, now=None):
        """Recent outage incidents for an endpoint, newest first.

        At most `limit` incidents (default 5); see
        `_group_incidents` for the grouping rules. An ongoing
        incident has `end` None and its duration runs to `now`
        (the current time when not given).
        """
        return self._group_incidents(name, now=now)[:limit]

    def incidents_total(self, name):
        """Total recorded incidents for an endpoint.

        The count of every incident in the recorded history —
        the same grouping `recent_incidents` uses, without its
        per-endpoint cap, so a counter built on this can never
        disagree with the incident lists. The bounded history
        can truncate a very long past, so this counts the
        incidents still recorded, not necessarily every incident
        since the monitor first ran.
        """
        return len(self._group_incidents(name))

    # Log-spaced bucket edges (seconds) for the incident-duration
    # histogram: a bucket's lower edge is inclusive, its upper edge
    # exclusive; the last bucket is open-ended. The steps (1m, 5m,
    # 15m, 1h, 6h, 24h) are roughly log-spaced, so a blip, a short
    # outage, and a day-long outage land in different buckets
    # instead of all piling into one linear bin.
    DURATION_HISTOGRAM_BUCKETS = (
        ("< 1m", 0, 60),
        ("1m – 5m", 60, 300),
        ("5m – 15m", 300, 900),
        ("15m – 1h", 900, 3600),
        ("1h – 6h", 3600, 21600),
        ("6h – 24h", 21600, 86400),
        ("> 24h", 86400, None),
    )

    def incident_duration_histogram(self, name, limit=None, now=None):
        """A duration histogram over an endpoint's incidents.

        One entry per bucket — {"label", "min_s", "max_s", "count"}
        — with every bucket present even at 0, so the shape is
        stable. The incidents are the same `_group_incidents`
        grouping `recent_incidents` uses (an ongoing incident's
        duration runs to `now`), and `limit` caps them newest
        first the same way, so a histogram built for an export
        can never disagree with the incidents that export lists.
        """
        incidents = self._group_incidents(name, now=now)
        if limit is not None:
            incidents = incidents[:limit]
        counts = [0] * len(self.DURATION_HISTOGRAM_BUCKETS)
        for incident in incidents:
            for i, (_label, low, high) in enumerate(
                    self.DURATION_HISTOGRAM_BUCKETS):
                if incident["duration_s"] >= low and (
                        high is None or incident["duration_s"] < high):
                    counts[i] += 1
                    break
        return [{"label": label, "min_s": low, "max_s": high,
                 "count": count}
                for (label, low, high), count in zip(
                    self.DURATION_HISTOGRAM_BUCKETS, counts)]

    def down_since(self, name):
        """Epoch second the current outage started, or None.

        The start of the current run of consecutive failed checks
        that actually ran: walk back from the most recent entry
        while it is a failure, and return the earliest failure's
        timestamp. Unconfirmed failures below the flap threshold
        count — the outage started when checks first failed, not
        when the threshold confirmed it. A success ends the run,
        so a recovered endpoint has no down-since; skipped and
        maintenance rounds never ran, so one also ends the run
        (an endpoint currently SKIPPED or in maintenance is not
        DOWN, so it has no outage start). Returns None when the
        endpoint was never checked. The bounded history can
        truncate a very long outage, so this is the earliest
        recorded failure, not necessarily the true start.
        """
        checks = self._checks.get(name)
        if not checks:
            return None
        start = None
        for entry in reversed(checks):
            if (entry.get("ok", False) or entry.get("skipped", False)
                    or entry.get("maintenance", False)):
                break
            start = entry["checked_at"]
        return start

    def last_error(self, name):
        """Error string of the endpoint's most recent failed check.

        Only checks that actually ran count: skipped and maintenance
        rounds never failed, they just did not run, and their entries
        carry a synthetic "skipped:" / "maintenance window:" note
        instead of a failure. Unconfirmed failures below the flap
        threshold are real failures, so they count. The error stays
        after a later success — it answers "why did it last fail",
        not "why is it down now". Returns None when the endpoint
        never failed (or was never checked).
        """
        checks = self._checks.get(name)
        if not checks:
            return None
        for entry in reversed(checks):
            if (not entry.get("ok", False)
                    and not entry.get("skipped", False)
                    and not entry.get("maintenance", False)
                    and entry.get("error") is not None):
                return entry["error"]
        return None

    def cert_expiry(self, name):
        """Epoch second the endpoint's certificate expires, or None.

        The most recent recorded check's `cert_expires_at` — None
        when the endpoint was never checked, is not HTTPS, or the
        check could not read the peer certificate. Entries written
        before cert expiry existed carry no key and count as None.
        """
        cur = self.current(name)
        if cur is None:
            return None
        return cur.get("cert_expires_at")

    def retries_total(self, name):
        """Total retries spent on `name`'s recorded checks.

        A retry is the second attempt after a first failure; entries
        written before retries existed carry no "retries" key and
        count as 0. Returns 0 when the endpoint was never checked.
        """
        return sum(c.get("retries", 0)
                   for c in self._checks.get(name, []))

    def uptime_pct(self, name):
        """Share of recorded checks that were up, or None if no checks.

        Skipped checks are excluded from both sides: a check that
        never ran is neither up nor down. A history of only skips is
        None, not 0 — no data is not downtime. Unconfirmed failures
        and maintenance windows are excluded the same way: a single
        blip below the flap threshold is not confirmed downtime, and
        a check paused for maintenance never ran, so neither counts
        for nor against uptime. (Entries written before flap
        detection carry no "unconfirmed" key and count as before.)
        """
        checks = self._checks.get(name)
        if not checks:
            return None
        counted = [c for c in checks
                   if not c.get("skipped", False)
                   and not c.get("unconfirmed", False)
                   and not c.get("maintenance", False)]
        if not counted:
            return None
        up = sum(1 for c in counted if c["ok"])
        return round(100.0 * up / len(counted), 1)

    def uptime_pct_window(self, name, seconds, now=None):
        """Uptime % over checks in the last `seconds`, or None if none.

        `now` defaults to time.time(); pass it in tests to pin the
        window. Checks older than the window are ignored, so this
        answers "how reliable was it in the last day / week" instead
        of "over everything ever recorded". A window with no checks
        is None, not 0 — no data is not downtime. Unconfirmed
        failures (below the flap threshold) are excluded, like
        skipped checks and maintenance windows: not confirmed
        downtime, and not a check that ran.
        """
        checks = self._checks.get(name)
        if not checks:
            return None
        cutoff = (time.time() if now is None else now) - seconds
        window = [c for c in checks
                  if c["checked_at"] >= cutoff
                  and not c.get("skipped", False)
                  and not c.get("unconfirmed", False)
                  and not c.get("maintenance", False)]
        if not window:
            return None
        up = sum(1 for c in window if c["ok"])
        return round(100.0 * up / len(window), 1)

    def downtime_seconds_window(self, name, seconds, now=None):
        """Total outage time (seconds) for an endpoint over the last
        `seconds`, or 0.0 when it was never down in the window.

        The same incident grouping `recent_incidents` uses:
        consecutive confirmed-down checks form one incident, from
        the first down check to the first passing check after it.
        Each incident is clipped to the window, so an outage that
        started before the window only counts the part inside it;
        an incident still open at the end of the recorded history
        accrues downtime to `now` (the current time when not
        given), capped at the window end. Skips, unconfirmed
        failures, and maintenance rounds are not outages and
        neither start nor end one, so they contribute nothing.
        """
        ref = time.time() if now is None else now
        cutoff = ref - seconds
        total = 0.0
        for incident in self._group_incidents(name, now=ref):
            end = (incident["end"] if incident["end"] is not None
                   else ref)
            overlap_start = max(incident["start"], cutoff)
            overlap_end = min(end, ref)
            if overlap_end > overlap_start:
                total += overlap_end - overlap_start
        return round(total, 1)

    def window_summary(self, name, seconds, now=None):
        """Uptime %, downtime, and counted checks for one endpoint
        over the last `seconds`.

        Returns {"uptime_pct", "downtime_seconds", "checks"} where
        `uptime_pct` is None when the window holds no counted
        checks (no data is not downtime), `downtime_seconds` is
        the outage time inside the window from
        `downtime_seconds_window`, and `checks` is how many
        counted checks the figure is based on. The same exclusion
        rules as `uptime_pct_window` apply: skipped checks,
        unconfirmed failures, and maintenance rounds count
        neither for nor against uptime.
        """
        ref = time.time() if now is None else now
        cutoff = ref - seconds
        checks = self._checks.get(name)
        # Downtime is incident time clipped to the window, so an
        # outage that started before the window and is still open
        # accrues the whole window even when no counted check falls
        # inside it — it must not report 0.0 while the endpoint
        # has been down throughout.
        downtime = self.downtime_seconds_window(name, seconds, now=ref)
        if not checks:
            return {"uptime_pct": None, "downtime_seconds": downtime,
                    "checks": 0}
        window = [c for c in checks
                  if c["checked_at"] >= cutoff
                  and not c.get("skipped", False)
                  and not c.get("unconfirmed", False)
                  and not c.get("maintenance", False)]
        if not window:
            return {"uptime_pct": None, "downtime_seconds": downtime,
                    "checks": 0}
        up = sum(1 for c in window if c["ok"])
        return {
            "uptime_pct": round(100.0 * up / len(window), 1),
            "downtime_seconds": downtime,
            "checks": len(window),
        }

    def check_counts(self, name):
        """Total checks that ran per endpoint, by result.

        Skipped and maintenance checks never ran, so they are not
        counted; every other check is "success" when it was ok and
        "failure" otherwise (an unconfirmed failure below the flap
        threshold still failed the check, so it counts as a failure
        here even though the endpoint still shows UP). Returns
        {"success": n, "failure": n}.
        """
        checks = self._checks.get(name, [])
        ran = [c for c in checks
               if not c.get("skipped", False)
               and not c.get("maintenance", False)]
        return {
            "success": sum(1 for c in ran if c["ok"]),
            "failure": sum(1 for c in ran if not c["ok"]),
        }

    def latency_summary(self, name, seconds=24 * 3600, now=None):
        """Latency summary (ms) for an endpoint over the last `seconds`
        (default 24 hours): nearest-rank p50/p95/p99 plus the sum and
        count of recorded latencies.

        Same window rules as latency_percentiles: skipped and
        maintenance checks never ran, so they carry no latency and
        are left out; failed checks do count. Returns None when the
        endpoint has no recorded latencies in the window, else
        {"p50", "p95", "p99", "sum", "count"}.
        """
        import math

        checks = self._checks.get(name)
        if not checks:
            return None
        cutoff = (time.time() if now is None else now) - seconds
        latencies = sorted(
            c["latency_ms"] for c in checks
            if c["checked_at"] >= cutoff
            and not c.get("skipped", False)
            and not c.get("maintenance", False))
        if not latencies:
            return None
        n = len(latencies)

        def percentile(p):
            rank = max(1, math.ceil(p / 100.0 * n))
            return round(latencies[min(rank, n) - 1], 1)

        return {"p50": percentile(50), "p95": percentile(95),
                "p99": percentile(99), "sum": round(sum(latencies), 1),
                "count": n}

    def latency_percentiles(self, name, seconds=24 * 3600, now=None):
        """p50/p95/p99 of recorded latencies (ms) for an endpoint over
        the last `seconds` (default 24 hours).

        Nearest-rank percentiles over checks that actually ran in the
        window: skipped and maintenance checks never ran, so they carry
        no latency and are left out. Failed checks do count — a slow
        error response is still a slow response. `now` defaults to
        time.time(); pass it in tests to pin the window. Returns None
        when the endpoint has no recorded latencies in the window
        (never checked, or only skips/maintenance), else
        {"p50": ..., "p95": ..., "p99": ...}.
        """
        summary = self.latency_summary(name, seconds=seconds, now=now)
        if summary is None:
            return None
        return {"p50": summary["p50"], "p95": summary["p95"],
                "p99": summary["p99"]}

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
