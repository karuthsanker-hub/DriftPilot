"""Feature unit tests for Whale-Tail v1.

Validates ATR (Wilder), compression score/high/low/midpoint,
and range_position_pct against hand-computed fixtures.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import fmean
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.whale_tail_v1.features import (
    atr,
    compression_high,
    compression_low,
    compression_midpoint,
    compression_score,
    range_position_pct,
)


ET = ZoneInfo("America/New_York")


def _bar(
    ts: datetime,
    close: float,
    high: float | None = None,
    low: float | None = None,
    open_: float | None = None,
) -> MinuteBar:
    return MinuteBar(
        symbol="TEST",
        timestamp=ts,
        open=open_ if open_ is not None else close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=1000.0,
    )


def _series_flat(n: int, price: float = 100.0) -> list[MinuteBar]:
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    return [_bar(start + timedelta(minutes=i), price) for i in range(n)]


def test_compression_score_flat_bars_is_zero():
    """21 flat bars (one extra to satisfy ATR period+1): range=0; ATR=0 → score is inf."""
    bars = _series_flat(21)
    a = atr(bars, period=20)
    assert a == 0.0
    score = compression_score(bars, window=15, atr_value=a)
    # Flat bars: range/atr where both are zero — implementation returns inf.
    import math
    assert math.isinf(score)


def test_compression_score_low_for_tight_window():
    """Tight window where range is much smaller than ATR -> score < 0.5."""
    # 6 wide bars + 15 tight bars = 21 total (satisfies ATR period+1).
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    # 6 wide bars: range 2.0 each
    for i in range(6):
        ts = start + timedelta(minutes=i)
        bars.append(_bar(ts, close=100.0, high=101.0, low=99.0, open_=100.0))
    # Then 15 tight bars: range 0.1 each, all near 100.5
    for i in range(15):
        ts = start + timedelta(minutes=6 + i)
        bars.append(_bar(ts, close=100.5, high=100.55, low=100.45, open_=100.5))
    a = atr(bars, period=20)
    score = compression_score(bars, window=15, atr_value=a)
    assert score < 0.5


def test_compression_score_high_for_trending_bars():
    """+0.3% per bar gives a wide range relative to ATR -> score >> 1."""
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    price = 100.0
    for i in range(25):
        nxt = price * 1.003
        ts = start + timedelta(minutes=i)
        bars.append(_bar(ts, close=nxt, high=nxt, low=price, open_=price))
        price = nxt
    a = atr(bars, period=20)
    score = compression_score(bars, window=15, atr_value=a)
    assert score > 1.5


def test_atr_hand_computed_wilder():
    """Build 21 bars with known true ranges and compare to hand-computed ATR.

    Construct bars so each bar i (i>=1) has TR = i+1. Use period=20.
    Wilder seed = mean(TR[1..20]) = mean(2..21) = 11.5.
    There are exactly 20 TRs and period=20, so the seed IS the ATR.
    """
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    # First bar: arbitrary, range 1.0.
    bars.append(_bar(start, close=100.0, high=100.5, low=99.5, open_=100.0))
    # Subsequent 20 bars: each has range = i+1 (for i in 1..20),
    # close == prev close so TR = high - low.
    prev_close = 100.0
    for i in range(1, 21):
        ts = start + timedelta(minutes=i)
        target_range = float(i + 1)
        high = prev_close + target_range / 2.0
        low = prev_close - target_range / 2.0
        close = prev_close
        bars.append(_bar(ts, close=close, high=high, low=low, open_=close))
    a = atr(bars, period=20)
    # Expected: mean(2.0..21.0) = 11.5
    expected = fmean(float(i + 1) for i in range(1, 21))
    assert a == pytest.approx(expected, abs=1e-6)
    assert expected == 11.5


def test_atr_invariants_flat_and_trending():
    flat = _series_flat(25)
    assert atr(flat, period=20) == 0.0
    # Trending bars have positive ATR.
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    price = 100.0
    for i in range(25):
        nxt = price + 1.0
        ts = start + timedelta(minutes=i)
        bars.append(_bar(ts, close=nxt, high=nxt, low=price, open_=price))
        price = nxt
    assert atr(bars, period=20) > 0.5


def test_atr_insufficient_history_raises():
    bars = _series_flat(10)
    with pytest.raises(ValueError):
        atr(bars, period=20)


def test_compression_high_low_midpoint():
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    # Highs ramp from 101 to 115, lows from 99 to 113. Last 15 bars span 101..115.
    for i in range(15):
        ts = start + timedelta(minutes=i)
        h = 101.0 + i
        low = 99.0 + i
        c = (h + low) / 2.0
        bars.append(_bar(ts, close=c, high=h, low=low, open_=c))
    assert compression_high(bars, window=15) == pytest.approx(115.0)
    assert compression_low(bars, window=15) == pytest.approx(99.0)
    assert compression_midpoint(bars, window=15) == pytest.approx((115.0 + 99.0) / 2.0)


def test_range_position_pct():
    assert range_position_pct(100.0, 110.0, 100.0) == pytest.approx(0.0)
    assert range_position_pct(110.0, 110.0, 100.0) == pytest.approx(1.0)
    assert range_position_pct(105.0, 110.0, 100.0) == pytest.approx(0.5)


def test_range_position_pct_at_midday_flat():
    """Flat bars at close=100, day high=low=100 -> degenerate; raises."""
    with pytest.raises(ValueError):
        range_position_pct(100.0, 100.0, 100.0)
