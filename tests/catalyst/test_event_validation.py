from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from driftpilot.catalyst.event import CatalystEvent


def _make(**overrides) -> CatalystEvent:
    base = dict(
        symbol="AAPL",
        category="earnings",
        subcategory="report",
        pillar="micro",
        ts=datetime(2026, 5, 3, 14, 30, tzinfo=timezone.utc),
        headline="Apple reports earnings",
        source="alpaca",
        horizon_minutes=60,
        headline_hash="abc123",
    )
    base.update(overrides)
    return CatalystEvent(**base)


def test_valid_event_constructs() -> None:
    event = _make()
    assert event.symbol == "AAPL"
    assert event.pillar == "micro"
    assert event.horizon_minutes == 60
    assert event.priority_modifier == 0.0
    assert event.sentiment is None


def test_bad_pillar_raises() -> None:
    with pytest.raises(ValueError, match="invalid pillar"):
        _make(pillar="banana")


def test_bad_horizon_raises() -> None:
    with pytest.raises(ValueError, match="invalid horizon_minutes"):
        _make(horizon_minutes=45)


def test_event_is_frozen() -> None:
    event = _make()
    with pytest.raises(FrozenInstanceError):
        event.symbol = "X"  # type: ignore[misc]


def test_event_is_hashable() -> None:
    event = _make()
    assert {event} == {event}
    event2 = _make(symbol="MSFT")
    assert len({event, event2}) == 2
