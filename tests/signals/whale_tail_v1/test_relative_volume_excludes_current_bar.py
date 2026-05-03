"""Lookahead-bias guard: relative_volume must EXCLUDE the current bar."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.whale_tail_v1.features import relative_volume


ET = ZoneInfo("America/New_York")


def _bar(symbol: str, ts: datetime, volume: float, price: float = 100.0) -> MinuteBar:
    return MinuteBar(
        symbol=symbol,
        timestamp=ts,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
    )


def _make_bars(symbol: str, volumes: list[float]) -> list[MinuteBar]:
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    return [_bar(symbol, start + timedelta(minutes=i), v) for i, v in enumerate(volumes)]


def test_relative_volume_excludes_current_bar_from_average():
    # 15 lookback bars all volume=1000, current bar volume=10000.
    # Correct rvol = 10000 / 1000 = 10.0.
    # Lookahead-buggy rvol would be 10000 / mean([1000]*15 + [10000]) = 10000 / 1562.5 ≈ 6.4.
    lookback = _make_bars("TEST", [1000.0] * 15)
    current = _bar("TEST", datetime(2025, 6, 2, 10, 15, tzinfo=ET), 10000.0)

    rvol = relative_volume(current, lookback, lookback_n=15)
    assert rvol == pytest.approx(10.0, rel=1e-9), (
        f"relative_volume must divide by mean of preceding bars only. "
        f"Got {rvol}, expected 10.0."
    )


def test_relative_volume_uses_only_last_n_lookback_bars():
    # 30 prior bars. The most recent 15 should drive the average.
    older = [10000.0] * 15
    recent = [1000.0] * 15
    lookback = _make_bars("TEST", older + recent)
    current = _bar("TEST", datetime(2025, 6, 2, 10, 30, tzinfo=ET), 5000.0)
    rvol = relative_volume(current, lookback, lookback_n=15)
    # 5000 / 1000 = 5.0
    assert rvol == pytest.approx(5.0, rel=1e-9)


def test_relative_volume_raises_on_insufficient_history():
    lookback = _make_bars("TEST", [1000.0] * 5)
    current = _bar("TEST", datetime(2025, 6, 2, 10, 5, tzinfo=ET), 1000.0)
    with pytest.raises(ValueError):
        relative_volume(current, lookback, lookback_n=15)


def test_relative_volume_zero_volume_lookback_raises():
    lookback = _make_bars("TEST", [0.0] * 15)
    current = _bar("TEST", datetime(2025, 6, 2, 10, 15, tzinfo=ET), 1000.0)
    with pytest.raises(ValueError):
        relative_volume(current, lookback, lookback_n=15)
