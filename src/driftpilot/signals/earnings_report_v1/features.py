from __future__ import annotations
from datetime import datetime, timezone


def event_age_minutes(event_ts: datetime, now: datetime) -> float:
    if event_ts.tzinfo is None:
        event_ts = event_ts.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - event_ts).total_seconds() / 60.0


__all__ = ["event_age_minutes"]
