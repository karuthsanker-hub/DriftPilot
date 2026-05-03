"""Tests for the multi-feature market regime detector (Phase C)."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from driftpilot.clock import DriftPilotClock
from driftpilot.regime_detector import (
    MarketRegime,
    RegimeSnapshotV2,
    detect_regime,
)
from driftpilot.signals.features import MinuteBar


ET = ZoneInfo("America/New_York")
CLOCK = DriftPilotClock()


def _bar(
    symbol: str,
    timestamp: datetime,
    close: float,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1_000_000.0,
) -> MinuteBar:
    o = open_ if open_ is not None else close
    h = high if high is not None else max(o, close)
    low_v = low if low is not None else min(o, close)
    return MinuteBar(
        symbol=symbol,
        timestamp=timestamp,
        open=o,
        high=h,
        low=low_v,
        close=close,
        volume=volume,
    )


def _spy_series(
    *,
    end_time: datetime,
    count: int,
    closes: list[float],
    volume: float = 1_000_000.0,
) -> list[MinuteBar]:
    assert len(closes) == count
    bars: list[MinuteBar] = []
    start = end_time - timedelta(minutes=count - 1)
    for i, close in enumerate(closes):
        ts = start + timedelta(minutes=i)
        bars.append(_bar("SPY", ts, close, volume=volume))
    return bars


def _flat_universe(
    *,
    end_time: datetime,
    above_count: int,
    below_count: int,
    bar_count: int = 30,
) -> dict[str, list[MinuteBar]]:
    universe: dict[str, list[MinuteBar]] = {}
    start = end_time - timedelta(minutes=bar_count - 1)
    for i in range(above_count):
        sym = f"UP{i}"
        bars: list[MinuteBar] = []
        for j in range(bar_count):
            ts = start + timedelta(minutes=j)
            close = 100.0 + j * 0.05  # rising, latest above session open and above vwap
            bars.append(_bar(sym, ts, close))
        universe[sym] = bars
    for i in range(below_count):
        sym = f"DN{i}"
        bars = []
        for j in range(bar_count):
            ts = start + timedelta(minutes=j)
            close = 100.0 - j * 0.05  # falling
            bars.append(_bar(sym, ts, close))
        universe[sym] = bars
    return universe


def test_unknown_regime_when_insufficient_history() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    spy = _spy_series(end_time=end, count=5, closes=[100.0, 100.1, 100.2, 100.1, 100.0])
    snap = detect_regime(spy, {}, CLOCK)
    assert snap.regime is MarketRegime.UNKNOWN
    assert snap.confidence_score == 0.0
    assert snap.spy_5m_return_pct == 0.0
    assert 0.0 <= snap.confidence_score <= 1.0


def test_news_shock_detected() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    closes = [100.0] * 25 + [100.05, 100.1, 100.15, 100.2, 100.8]
    spy = _spy_series(end_time=end, count=30, closes=closes)
    universe = _flat_universe(end_time=end, above_count=5, below_count=5)
    snap = detect_regime(spy, universe, CLOCK)
    assert snap.regime is MarketRegime.NEWS_SHOCK
    assert 0.0 <= snap.confidence_score <= 1.0


def test_opening_drift() -> None:
    end = datetime(2026, 5, 4, 9, 50, tzinfo=ET)
    closes = [100.0 + i * 0.01 for i in range(30)]
    spy = _spy_series(end_time=end, count=30, closes=closes)
    universe = _flat_universe(end_time=end, above_count=5, below_count=5)
    snap = detect_regime(spy, universe, CLOCK)
    assert snap.regime is MarketRegime.OPENING_DRIFT
    assert snap.time_of_day_bucket == "open"
    assert 0.0 <= snap.confidence_score <= 1.0


def test_closing_drift_at_15_15_et_per_spec_rule_3() -> None:
    """Spec rule 3: time_et >= 15:00 ET → CLOSING_DRIFT. Reviewer flagged
    that the 15:00-15:30 window was silently falling through to CHOPPY.
    This pins the fix.
    """
    end = datetime(2026, 5, 4, 15, 15, tzinfo=ET)
    closes = [100.0 + 0.001 * i for i in range(31)]
    spy = _spy_series(end_time=end, count=31, closes=closes)
    universe = _flat_universe(end_time=end, above_count=5, below_count=5)
    snap = detect_regime(spy, universe, CLOCK)
    assert snap.regime is MarketRegime.CLOSING_DRIFT


def test_closing_drift() -> None:
    end = datetime(2026, 5, 4, 15, 50, tzinfo=ET)
    closes = [100.0 + i * 0.01 for i in range(30)]
    spy = _spy_series(end_time=end, count=30, closes=closes)
    universe = _flat_universe(end_time=end, above_count=5, below_count=5)
    snap = detect_regime(spy, universe, CLOCK)
    assert snap.regime is MarketRegime.CLOSING_DRIFT
    assert snap.time_of_day_bucket == "close"
    assert 0.0 <= snap.confidence_score <= 1.0


def test_range_bound() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    # Flat with tiny noise: 30m return < 0.1%
    closes = [100.0 + (0.01 if i % 2 == 0 else -0.01) for i in range(30)]
    spy = _spy_series(end_time=end, count=30, closes=closes)
    universe = _flat_universe(end_time=end, above_count=5, below_count=5)
    snap = detect_regime(spy, universe, CLOCK)
    assert snap.regime is MarketRegime.RANGE_BOUND
    assert 40.0 <= snap.breadth_above_vwap_pct <= 60.0
    assert 0.0 <= snap.confidence_score <= 1.0


def test_trend_bull_low_vol() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    # +0.5% over 30 minutes — need 31 bars so return_over_minutes(bars, 30)
    # has both endpoints (bar at T and bar at T-30).
    closes = [100.0 + i * (0.5 / 30) for i in range(31)]
    spy = _spy_series(end_time=end, count=31, closes=closes)
    # 75% breadth above vwap: 9 up, 3 down
    universe = _flat_universe(end_time=end, above_count=9, below_count=3)
    snap = detect_regime(spy, universe, CLOCK)
    assert snap.spy_30m_return_pct > 0.3
    assert snap.regime is MarketRegime.TREND_BULL_LOW_VOL
    assert snap.breadth_above_vwap_pct > 65.0
    assert 0.0 <= snap.confidence_score <= 1.0


def test_trend_bull_high_vol() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    # +0.5% over 30 minutes but jagged final 5 bars (high stdev). 31 bars
    # needed so 30-min return is computable.
    base = [100.0 + i * (0.5 / 30) for i in range(26)]
    jagged = [100.5, 99.8, 100.6, 99.9, 100.5]
    closes = base + jagged
    spy = _spy_series(end_time=end, count=31, closes=closes)
    universe = _flat_universe(end_time=end, above_count=9, below_count=3)
    snap = detect_regime(spy, universe, CLOCK)
    assert snap.spy_30m_return_pct > 0.3
    assert snap.regime is MarketRegime.TREND_BULL_HIGH_VOL
    assert 0.0 <= snap.confidence_score <= 1.0


def test_trend_bear() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    # -0.5% over 30 minutes; 31 bars so 30-min return is computable.
    closes = [100.0 - i * (0.5 / 30) for i in range(31)]
    spy = _spy_series(end_time=end, count=31, closes=closes)
    universe = _flat_universe(end_time=end, above_count=3, below_count=9)
    snap = detect_regime(spy, universe, CLOCK)
    assert snap.spy_30m_return_pct < -0.3
    assert snap.regime is MarketRegime.TREND_BEAR
    assert 0.0 <= snap.confidence_score <= 1.0


def test_choppy_default() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    # 30m return between 0.1 and 0.3%, breadth skewed so not range_bound
    closes = [100.0 + i * (0.2 / 29) for i in range(30)]
    spy = _spy_series(end_time=end, count=30, closes=closes)
    # Skewed breadth (8 up, 2 down) -> 80% above vwap, not in 40-60 range; +0.2% < 0.3 trend
    universe = _flat_universe(end_time=end, above_count=8, below_count=2)
    snap = detect_regime(spy, universe, CLOCK)
    assert snap.regime is MarketRegime.CHOPPY
    assert 0.0 <= snap.confidence_score <= 1.0


def test_confidence_score_clamped_zero_to_one() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    closes = [100.0 + i * (0.5 / 29) for i in range(30)]
    spy = _spy_series(end_time=end, count=30, closes=closes)
    universe = _flat_universe(end_time=end, above_count=9, below_count=3)
    snap = detect_regime(spy, universe, CLOCK)
    assert 0.0 <= snap.confidence_score <= 1.0


def test_breadth_advance_decline_handles_zero_decliners() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    closes = [100.0 + i * (0.5 / 29) for i in range(30)]
    spy = _spy_series(end_time=end, count=30, closes=closes)
    universe = _flat_universe(end_time=end, above_count=10, below_count=0)
    snap = detect_regime(spy, universe, CLOCK)
    import math as _math
    assert _math.isfinite(snap.breadth_advance_decline_ratio)
    assert snap.breadth_advance_decline_ratio > 0.0


def test_minutes_until_close_at_15_55_is_5() -> None:
    end = datetime(2026, 5, 4, 15, 55, tzinfo=ET)
    closes = [100.0 + i * 0.001 for i in range(30)]
    spy = _spy_series(end_time=end, count=30, closes=closes)
    snap = detect_regime(spy, {}, CLOCK)
    assert snap.minutes_until_close == 5


def test_naive_datetime_rejected_at_bar_construction() -> None:
    naive = datetime(2026, 5, 4, 11, 0)
    with pytest.raises(ValueError):
        _bar("SPY", naive, 100.0)


def test_returns_regime_snapshot_v2_type() -> None:
    end = datetime(2026, 5, 4, 11, 0, tzinfo=ET)
    closes = [100.0 + i * 0.001 for i in range(30)]
    spy = _spy_series(end_time=end, count=30, closes=closes)
    snap = detect_regime(spy, {}, CLOCK)
    assert isinstance(snap, RegimeSnapshotV2)
