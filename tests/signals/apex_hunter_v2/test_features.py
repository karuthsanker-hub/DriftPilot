"""Feature-math tests beyond EWMLR: ATR, relative_alpha, correlation_to_spy."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.apex_hunter_v2.features import (
    atr,
    correlation_to_spy,
    relative_alpha,
)
from driftpilot.signals.features import MinuteBar


ET = ZoneInfo("America/New_York")


def _bar(ts: datetime, *, o: float, h: float, low: float, c: float, v: float = 1000.0) -> MinuteBar:
    return MinuteBar(symbol="X", timestamp=ts, open=o, high=h, low=low, close=c, volume=v)


def test_atr_flat_bars_is_zero():
    start = datetime(2024, 6, 5, 10, 0, tzinfo=ET)
    bars = [_bar(start + timedelta(minutes=i), o=100, h=100, low=100, c=100) for i in range(20)]
    assert atr(bars, period=14) == 0.0


def test_atr_constant_true_range():
    """All 14+1 bars have TR = 1.0 → ATR = 1.0."""
    start = datetime(2024, 6, 5, 10, 0, tzinfo=ET)
    bars: list[MinuteBar] = []
    prev_close = 100.0
    bars.append(_bar(start, o=prev_close, h=prev_close + 0.5, low=prev_close - 0.5, c=prev_close))
    for i in range(1, 16):
        ts = start + timedelta(minutes=i)
        # close == prev_close, range = 1.0 → TR = 1.0
        bars.append(_bar(ts, o=prev_close, h=prev_close + 0.5, low=prev_close - 0.5, c=prev_close))
    a = atr(bars, period=14)
    assert a == pytest.approx(1.0, abs=1e-9)


def test_atr_insufficient_history_raises():
    start = datetime(2024, 6, 5, 10, 0, tzinfo=ET)
    bars = [_bar(start + timedelta(minutes=i), o=100, h=100.5, low=99.5, c=100) for i in range(5)]
    with pytest.raises(ValueError):
        atr(bars, period=14)


def test_relative_alpha_basic():
    assert relative_alpha(0.002, 0.001) == pytest.approx(2.0, abs=1e-9)
    assert relative_alpha(-0.002, 0.001) == pytest.approx(-2.0, abs=1e-9)


def test_relative_alpha_spy_zero():
    """SPY slope == 0 → ticker > 0 → +inf, ticker < 0 → -inf, both 0 → 0.0."""
    assert math.isinf(relative_alpha(0.002, 0.0))
    assert relative_alpha(0.002, 0.0) > 0
    assert math.isinf(relative_alpha(-0.002, 0.0))
    assert relative_alpha(-0.002, 0.0) < 0
    assert relative_alpha(0.0, 0.0) == 0.0


def test_correlation_perfect_positive():
    """Two return series that move in lockstep → correlation = 1.0."""
    ticker = [0.001 * i for i in range(40)]
    spy = [0.001 * i for i in range(40)]
    assert correlation_to_spy(ticker, spy, window=30) == pytest.approx(1.0, abs=1e-9)


def test_correlation_perfect_negative():
    ticker = [0.001 * i for i in range(40)]
    spy = [-0.001 * i for i in range(40)]
    assert correlation_to_spy(ticker, spy, window=30) == pytest.approx(-1.0, abs=1e-9)


def test_correlation_insufficient_history_raises():
    with pytest.raises(ValueError):
        correlation_to_spy([0.001] * 10, [0.001] * 10, window=30)
