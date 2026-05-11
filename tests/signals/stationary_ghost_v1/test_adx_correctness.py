"""ADX correctness — invariants only.

The Wilder 1978 IBM textbook fixture cross-check is owned by the user
before merge per spec section 7.2 (see KNOWN_RISKS.md). These tests
enforce the documented invariants:

  - Synthetic flat bars (constant price) -> ADX < 10
  - Synthetic trending bars (+0.5%/bar)  -> ADX > 30

Plus structural sanity checks (insufficient-history error, period
boundary).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.stationary_ghost_v1.features import adx


ET = ZoneInfo("America/New_York")


def _bar(ts: datetime, o: float, h: float, low: float, c: float) -> MinuteBar:
    return MinuteBar(
        symbol="TEST", timestamp=ts,
        open=o, high=h, low=low, close=c, volume=1000.0,
    )


def _flat_bars(n: int, price: float = 100.0) -> list[MinuteBar]:
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    return [_bar(start + timedelta(minutes=i), price, price, price, price)
            for i in range(n)]


def _trending_bars(n: int, start_price: float = 100.0, pct_per_bar: float = 0.005) -> list[MinuteBar]:
    """Each bar's high/low/close all move +0.5% from the previous close,
    producing a clean upward trend with no countertrend movement."""
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    price = start_price
    for i in range(n):
        prev_close = price
        next_close = prev_close * (1.0 + pct_per_bar)
        # Upward bar: high=next_close, low=prev_close, open=prev_close,
        # close=next_close. Ensures a positive high-prev_high and
        # negative-or-zero low movement.
        bars.append(_bar(
            start + timedelta(minutes=i),
            o=prev_close,
            h=next_close,
            low=prev_close,
            c=next_close,
        ))
        price = next_close
    return bars


def test_adx_flat_bars_below_ten():
    bars = _flat_bars(60)
    value = adx(bars, period=14)
    assert value < 10.0, f"flat bars must yield ADX < 10, got {value}"


def test_adx_trending_bars_above_thirty():
    bars = _trending_bars(60, start_price=100.0, pct_per_bar=0.005)
    value = adx(bars, period=14)
    assert value > 30.0, f"trending bars must yield ADX > 30, got {value}"


def test_adx_insufficient_history_raises():
    bars = _flat_bars(10)
    with pytest.raises(ValueError):
        adx(bars, period=14)


def test_adx_returns_finite_non_negative():
    bars = _trending_bars(40)
    value = adx(bars, period=14)
    assert value >= 0.0
    assert value <= 100.0
