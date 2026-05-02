"""Z-score distance fixture tests."""

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


def test_z_score_minus_two_point_five_sigma():
    # 14 bars at 100, 1 dip bar at 99 (which we drop), then a probe price
    # at exactly mean - 2.5*std.
    closes = [100.0] * 15
    closes[7] = 102.0
    closes[12] = 98.0
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars = [
        MinuteBar(
            symbol="TEST",
            timestamp=start + timedelta(minutes=i),
            open=c, high=c, low=c, close=c, volume=1000.0,
        )
        for i, c in enumerate(closes)
    ]
    _upper, mid, _lower = bollinger_bands(bars, period=15, std_dev=2.0)
    std = pstdev(closes)
    probe = mid - 2.5 * std
    assert distance_z_score(probe, mid, std) == pytest.approx(-2.5)
    assert mid == pytest.approx(fmean(closes))


def test_z_score_at_lower_band_is_minus_two():
    closes = [100.0, 101.0, 99.0, 102.0, 98.0, 100.0, 101.0, 99.0, 102.0,
              98.0, 100.0, 101.0, 99.0, 102.0, 98.0]
    start = datetime(2025, 6, 2, 10, 0, tzinfo=ET)
    bars = [
        MinuteBar(
            symbol="TEST",
            timestamp=start + timedelta(minutes=i),
            open=c, high=c, low=c, close=c, volume=1000.0,
        )
        for i, c in enumerate(closes)
    ]
    _upper, mid, lower = bollinger_bands(bars, period=15, std_dev=2.0)
    std = pstdev(closes)
    assert distance_z_score(lower, mid, std) == pytest.approx(-2.0)
