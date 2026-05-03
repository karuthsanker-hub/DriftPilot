"""Feature helpers for analyst_target_raise_v1.

The signal is event-driven — its primary "feature" is the freshness of
the underlying CatalystEvent. Kept here as a pure function so it can be
unit-tested independently of the signal class.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def event_age_minutes(now: datetime, event_ts: datetime) -> float:
    """Return the age of an event in minutes (now - event_ts).

    Both arguments must be timezone-aware. Naive datetimes are treated
    as UTC for defensive interop, but callers should pass aware values.
    """

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if event_ts.tzinfo is None:
        event_ts = event_ts.replace(tzinfo=timezone.utc)
    delta: timedelta = now - event_ts
    return delta.total_seconds() / 60.0


def is_event_fresh(now: datetime, event_ts: datetime, max_age_minutes: int) -> bool:
    """True iff event is within `max_age_minutes` of `now`."""

    return event_age_minutes(now, event_ts) <= float(max_age_minutes)


__all__ = ["event_age_minutes", "is_event_fresh"]
