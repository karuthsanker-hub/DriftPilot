"""Feature unit tests for Stationary Ghost v1 (Bollinger, Z-score)."""

from __future__ import annotations

from datetime import datetime, timedelta
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.stationary_ghost_v1.features import (
    bollinger_bands,
    distance_z_score,
)


ET = ZoneInfo("America/New_York")


def _bar(ts: datetime, close: float, high: float | None = None, low: float | None = None) -> MinuteBar:
    return MinuteBar(
        symbol="TEST",
        timestamp=ts,
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=1000.0,
    )


def _series(closes: list[float]) -> list[MinuteBar]:
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    return [_bar(start + timedelta(minutes=i), c) for i, c in enumerate(closes)]


def test_bollinger_constant_prices():
    bars = _series([100.0] * 15)
    upper, mid, lower = bollinger_bands(bars, period=15, std_dev=2.0)
    assert mid == pytest.approx(100.0)
    assert upper == pytest.approx(100.0)
    assert lower == pytest.approx(100.0)


def test_bollinger_known_window():
    closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0,
              20.0, 21.0, 22.0, 23.0, 24.0]
    bars = _series(closes)
    upper, mid, lower = bollinger_bands(bars, period=15, std_dev=2.0)
    expected_mid = fmean(closes)
    expected_std = pstdev(closes)
    assert mid == pytest.approx(expected_mid)
    assert upper == pytest.approx(expected_mid + 2.0 * expected_std)
    assert lower == pytest.approx(expected_mid - 2.0 * expected_std)


def test_bollinger_uses_only_last_period_bars():
    closes = [1.0] * 50 + [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0,
                            18.0, 19.0, 20.0, 21.0, 22.0, 23.0, 24.0]
    bars = _series(closes)
    upper, mid, lower = bollinger_bands(bars, period=15, std_dev=2.0)
    expected_mid = fmean(closes[-15:])
    assert mid == pytest.approx(expected_mid)


def test_bollinger_insufficient_history():
    bars = _series([100.0] * 5)
    with pytest.raises(ValueError):
        bollinger_bands(bars, period=15)


def test_distance_z_score_sign_and_magnitude():
    # price 95, mean 100, std 2 => z = -2.5
    assert distance_z_score(95.0, 100.0, 2.0) == pytest.approx(-2.5)
    # price 105 => z = +2.5
    assert distance_z_score(105.0, 100.0, 2.0) == pytest.approx(2.5)
    # price at mean => z = 0
    assert distance_z_score(100.0, 100.0, 2.0) == pytest.approx(0.0)


def test_distance_z_score_rejects_zero_std():
    with pytest.raises(ValueError):
        distance_z_score(100.0, 100.0, 0.0)
