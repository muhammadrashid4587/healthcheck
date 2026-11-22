"""Flap detection: only confirm an endpoint down after N consecutive
failed checks, so a single blip does not page anyone.

Pure Python 3 standard library. The detector is a small state machine:
per endpoint it counts consecutive failures and tracks whether the
endpoint is currently *confirmed* down. A success at any point resets
the counter, and a success after a confirmed outage is a recovery.
Isolated failures are real data — they are recorded in history — but
they are not a state change: no alert fires and the endpoint does not
flip to DOWN until the threshold is reached.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_THRESHOLD = 3


def validate_threshold(value):
    """`flap_threshold` must be a positive integer (not a bool, not a
    float, not a string). Raises ValueError; returns the value."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"flap_threshold must be a positive integer, not {value!r}")
    return value


@dataclass(frozen=True)
class FlapOutcome:
    """What one observation did to an endpoint's confirmed state.

    `confirmed_down` — the endpoint's confirmed state after this
    observation (False means still considered up). `changed` — the
    confirmed state just flipped (Nth consecutive failure, or a
    success after a confirmed outage). `consecutive_failures` — the
    current run of failures, 0 after any success.
    """
    confirmed_down: bool
    changed: bool
    consecutive_failures: int


class FlapDetector:
    """Per-endpoint consecutive-failure counting with a threshold.

    `threshold` is how many failures in a row confirm an outage
    (default 3). Endpoints are tracked independently; an endpoint
    that was never observed is not confirmed down.
    """

    def __init__(self, threshold=DEFAULT_THRESHOLD):
        self.threshold = validate_threshold(threshold)
        self._failures = {}  # name -> consecutive failure count
        self._down = set()   # names currently confirmed down

    def observe(self, name, ok):
        """Feed one check result; returns a FlapOutcome.

        A success resets the counter and, if the endpoint was
        confirmed down, flips it back to up (a recovery). A failure
        increments the counter and only confirms the outage — and
        only reports a change — once the threshold is reached; a
        failure below the threshold leaves the endpoint confirmed up.
        """
        if ok:
            was_down = name in self._down
            self._failures[name] = 0
            self._down.discard(name)
            return FlapOutcome(confirmed_down=False, changed=was_down,
                               consecutive_failures=0)
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        if count >= self.threshold:
            changed = name not in self._down
            self._down.add(name)
            return FlapOutcome(confirmed_down=True, changed=changed,
                               consecutive_failures=count)
        return FlapOutcome(confirmed_down=False, changed=False,
                           consecutive_failures=count)

    def is_down(self, name):
        """Whether `name` is currently confirmed down."""
        return name in self._down

    def consecutive_failures(self, name):
        """Current run of consecutive failures for `name` (0 if none)."""
        return self._failures.get(name, 0)
