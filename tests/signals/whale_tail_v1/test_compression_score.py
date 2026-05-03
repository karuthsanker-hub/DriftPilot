"""Edge cases for compression_score: zero ATR, tight vs wide windows."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.whale_tail_v1.features import compression_score


ET = ZoneInfo("America/New_York")


def _bar(ts: datetime, close: float, high: float, low: float) -> MinuteBar:
    return MinuteBar(
        symbol="TEST",
        timestamp=ts,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
    )


def _flat_bars(n: int, price: float = 100.0) -> list[MinuteBar]:
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    return [_bar(start + timedelta(minutes=i), price, price, price) for i in range(n)]


def test_zero_atr_returns_inf():
    bars = _flat_bars(15)
    score = compression_score(bars, window=15, atr_value=0.0)
    assert math.isinf(score)


def test_negative_atr_raises():
    bars = _flat_bars(15)
    with pytest.raises(ValueError):
        compression_score(bars, window=15, atr_value=-1.0)


def test_missing_atr_raises():
    bars = _flat_bars(15)
    with pytest.raises(ValueError):
        compression_score(bars, window=15, atr_value=None)


def test_tightly_compressed_window_small_score():
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    # All bars in window: range 0.05 around 100.
    for i in range(15):
        ts = start + timedelta(minutes=i)
        bars.append(_bar(ts, 100.0, 100.025, 99.975))
    # ATR_value = 1.0 -> score = 0.05 / 1.0 = 0.05
    score = compression_score(bars, window=15, atr_value=1.0)
    assert score == pytest.approx(0.05, abs=1e-9)
    assert score < 0.5


def test_wide_range_window_large_score():
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    # Window range from 95 (low) to 105 (high) -> 10 wide
    for i in range(15):
        ts = start + timedelta(minutes=i)
        h = 100.0 + (5.0 if i == 0 else 0.5)
        low = 100.0 - (5.0 if i == 14 else 0.5)
        bars.append(_bar(ts, 100.0, h, low))
    score = compression_score(bars, window=15, atr_value=1.0)
    assert score > 1.5
