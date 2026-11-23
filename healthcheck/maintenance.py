"""Maintenance windows: scheduled times when an endpoint is not checked.

An endpoint can declare `maintenance_windows` in the config: a list of
recurring weekly windows, each with a `start` and `end` time ("HH:MM",
local time) and an optional `days` selector. During a window the
endpoint is not checked at all — no alert fires, no flap state changes,
and the round is recorded as maintenance, which never counts against
uptime. A window whose end is earlier than its start runs overnight
(e.g. 22:00–02:00 runs from 22:00 on its day to 02:00 the next
morning). A window with no `days` runs every day.

Pure Python 3 standard library. Times are the monitor's local time:
a window is an operator's "Saturday 02:00–04:00", not a UTC instant.
"""

from __future__ import annotations

import re
import time

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4,
             "sat": 5, "sun": 6}
DAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_WINDOW_KEYS = {"days", "start", "end"}


def _parse_time(value, name):
    """An "HH:MM" time as minutes after midnight. Raises ValueError."""
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise ValueError(
            f"endpoint {name!r}: maintenance window time must be "
            f"\"HH:MM\", not {value!r}")
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _parse_days(value, name):
    """A days selector as a frozenset of weekday ints (Mon=0..Sun=6).

    `None` means every day. A string is one day ("sat"), "weekdays"
    (Mon–Fri), or "weekend" (Sat–Sun); a list is day abbreviations.
    Matching is case-insensitive. Raises ValueError on anything else.
    """
    if value is None:
        return None
    if isinstance(value, str):
        low = value.lower()
        if low == "weekdays":
            return frozenset(range(5))
        if low == "weekend":
            return frozenset((5, 6))
        if low in DAY_NAMES:
            return frozenset((DAY_NAMES[low],))
        raise ValueError(
            f"endpoint {name!r}: maintenance window days must be a "
            f"day (\"mon\"..\"sun\"), \"weekdays\", \"weekend\", or a "
            f"list of days, not {value!r}")
    if isinstance(value, list):
        days = set()
        for item in value:
            if not isinstance(item, str) or item.lower() not in DAY_NAMES:
                raise ValueError(
                    f"endpoint {name!r}: maintenance window days must "
                    f"be day abbreviations (\"mon\"..\"sun\"), "
                    f"not {item!r}")
            days.add(DAY_NAMES[item.lower()])
        if not days:
            raise ValueError(
                f"endpoint {name!r}: maintenance window days must not "
                f"be an empty list")
        return frozenset(days)
    raise ValueError(
        f"endpoint {name!r}: maintenance window days must be a string "
        f"or a list, not {value!r}")


def parse_maintenance_window(spec, name="?"):
    """Validate one window spec and return its normalized form.

    The normalized window is a dict with `days` (frozenset of weekday
    ints, or None for every day), `start`/`end` (minutes after
    midnight), and `start_text`/`end_text` (the original "HH:MM"
    strings, kept for display). Raises ValueError with a clear
    message; a zero-length window (start == end) is rejected, while
    end earlier than start is an overnight window, not an error.
    """
    if not isinstance(spec, dict):
        raise ValueError(
            f"endpoint {name!r}: maintenance window must be an object, "
            f"not {spec!r}")
    unknown = set(spec) - _WINDOW_KEYS
    if unknown:
        raise ValueError(
            f"endpoint {name!r}: unknown maintenance window key(s): "
            f"{', '.join(sorted(unknown))}")
    if "start" not in spec or "end" not in spec:
        raise ValueError(
            f"endpoint {name!r}: maintenance window needs both "
            f"\"start\" and \"end\"")
    start = _parse_time(spec["start"], name)
    end = _parse_time(spec["end"], name)
    if start == end:
        raise ValueError(
            f"endpoint {name!r}: maintenance window {spec['start']!r}–"
            f"{spec['end']!r} is zero-length; use a real range, or an "
            f"overnight range such as 22:00–02:00")
    return {"days": _parse_days(spec.get("days"), name),
            "start": start, "end": end,
            "start_text": spec["start"], "end_text": spec["end"]}


def validate_maintenance_windows(endpoints):
    """Reject bad `maintenance_windows` entries at config load.

    `maintenance_windows` must be a list of window objects; each one
    is checked by parse_maintenance_window. Raises ValueError; the
    monitor refuses to start rather than silently ignore a window
    that never matches.
    """
    for ep in endpoints:
        windows = ep.get("maintenance_windows")
        if windows is None:
            continue
        if not isinstance(windows, list):
            raise ValueError(
                f"endpoint {ep['name']!r}: maintenance_windows must be "
                f"a list, not {windows!r}")
        for spec in windows:
            parse_maintenance_window(spec, ep["name"])


def _in_window(window, weekday, minutes):
    """Whether a normalized window covers this weekday and minute."""
    days, start, end = window["days"], window["start"], window["end"]
    if start < end:
        return (days is None or weekday in days) \
            and start <= minutes < end
    # Overnight window: start on its day, end the next morning.
    if minutes >= start:
        return days is None or weekday in days
    if minutes < end:
        return days is None or (weekday - 1) % 7 in days
    return False


def active_window(windows, now=None):
    """The first window covering `now`, or None.

    `windows` is the raw config list (each spec is parsed here, so a
    bad spec raises ValueError). `now` is epoch seconds and defaults
    to the current time; pass it in tests to pin the moment. Windows
    are in the monitor's local time.
    """
    if not windows:
        return None
    moment = time.localtime(time.time() if now is None else now)
    weekday = moment.tm_wday
    minutes = moment.tm_hour * 60 + moment.tm_min
    for spec in windows:
        window = parse_maintenance_window(spec)
        if _in_window(window, weekday, minutes):
            return window
    return None


def in_maintenance(windows, now=None):
    """Whether `now` falls inside any of the given windows."""
    return active_window(windows, now) is not None


def summarize_window(window):
    """A short display string for a normalized window.

    "daily 02:00–03:00", "Sat 02:00–04:00", "Mon–Fri 22:00–02:00",
    "Sat–Sun 02:00–04:00", or "Mon, Wed 02:00–03:00" for a list.
    """
    days = window["days"]
    if days is None:
        label = "daily"
    elif days == frozenset(range(5)):
        label = "Mon–Fri"
    elif days == frozenset((5, 6)):
        label = "Sat–Sun"
    elif len(days) == 1:
        label = DAY_SHORT[next(iter(days))]
    else:
        label = ", ".join(DAY_SHORT[d] for d in sorted(days))
    return f"{label} {window['start_text']}–{window['end_text']}"


def summarize_windows(windows):
    """Display string for a config window list, or "-" when none."""
    if not windows:
        return "-"
    return "; ".join(summarize_window(parse_maintenance_window(w))
                     for w in windows)
