"""Lookahead-bias guard: relative_volume must EXCLUDE the current bar from
the lookback average. Including the current bar in its own denominator
silently leaks the future into the feature.

This test is intentionally written before the implementation — it should
go red first, then green once `relative_volume` correctly excludes the
current bar.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.stationary_ghost_v1.features import relative_volume


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
    # 15 lookback bars all volume=100, current bar volume=50.
    # Correct rvol = 50 / 100 = 0.5.
    # Lookahead-buggy rvol would be 50 / mean([100]*15 + [50]) = 50 / 96.875 ≈ 0.516.
    lookback = _make_bars("TEST", [100.0] * 15)
    current = _bar("TEST", datetime(2025, 6, 2, 10, 15, tzinfo=ET), 50.0)

    rvol = relative_volume(current, lookback, lookback_n=15)
    assert rvol == pytest.approx(0.5, rel=1e-9), (
        f"relative_volume must divide by mean of preceding bars only. "
        f"Got {rvol}, expected 0.5."
    )


def test_relative_volume_uses_only_last_n_lookback_bars():
    # 30 prior bars, last 15 average to 200; older 15 are 1000.
    # Correct rvol = 100 / 200 = 0.5.
    older = [1000.0] * 15
    recent = [200.0] * 15
    lookback = _make_bars("TEST", older + recent)
    current = _bar("TEST", datetime(2025, 6, 2, 10, 30, tzinfo=ET), 100.0)

    rvol = relative_volume(current, lookback, lookback_n=15)
    assert rvol == pytest.approx(0.5, rel=1e-9)


def test_relative_volume_raises_on_insufficient_history():
    lookback = _make_bars("TEST", [100.0] * 5)
    current = _bar("TEST", datetime(2025, 6, 2, 10, 5, tzinfo=ET), 100.0)
    with pytest.raises(ValueError):
        relative_volume(current, lookback, lookback_n=15)


def test_relative_volume_zero_volume_lookback_raises():
    lookback = _make_bars("TEST", [0.0] * 15)
    current = _bar("TEST", datetime(2025, 6, 2, 10, 15, tzinfo=ET), 50.0)
    with pytest.raises(ValueError):
        relative_volume(current, lookback, lookback_n=15)
