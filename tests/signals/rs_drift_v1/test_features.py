"""Feature math tests: opening_range_high, rs_score, post_open_vwap."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.rs_drift_v1.features import (
    opening_range_high,
    post_open_vwap,
    rs_score,
)


ET = ZoneInfo("America/New_York")


def _bar(
    symbol: str,
    ts: datetime,
    *,
    open_: float,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float = 1000.0,
) -> MinuteBar:
    return MinuteBar(
        symbol=symbol,
        timestamp=ts,
        open=open_,
        high=high if high is not None else open_,
        low=low if low is not None else open_,
        close=close if close is not None else open_,
        volume=volume,
    )


def _session(symbol: str, session_date: date, prices: list[tuple[time, float]]) -> list[MinuteBar]:
    """Build bars from a list of (et_time, price) pairs."""
    out: list[MinuteBar] = []
    for et_t, price in prices:
        ts = datetime(session_date.year, session_date.month, session_date.day, et_t.hour, et_t.minute, tzinfo=ET)
        out.append(_bar(symbol, ts, open_=price))
    return out


def test_opening_range_high_returns_max_high_in_window():
    session_date = date(2024, 6, 5)
    bars = []
    # 09:30 high=100, 09:45 high=101, 09:55 high=102, 10:00 high=99 (outside window)
    points = [
        (time(9, 30), 100.0),
        (time(9, 45), 101.0),
        (time(9, 55), 102.0),
        (time(10, 0), 99.0),  # boundary excluded — half-open [09:30, 10:00)
    ]
    for et_t, val in points:
        ts = datetime(2024, 6, 5, et_t.hour, et_t.minute, tzinfo=ET)
        bars.append(_bar("ABC", ts, open_=val, high=val, low=val, close=val))

    assert opening_range_high(bars, session_date) == 102.0


def test_opening_range_high_no_window_bars_raises():
    session_date = date(2024, 6, 5)
    # Only post-window bars
    bars = [
        _bar("ABC", datetime(2024, 6, 5, 11, 0, tzinfo=ET), open_=100.0),
    ]
    with pytest.raises(ValueError, match="opening_range_high"):
        opening_range_high(bars, session_date)


def test_rs_score_pure_relative_strength():
    session_date = date(2024, 6, 5)
    # Stock: 09:30 open 100, 09:59 close 101.5 → +1.5%
    stock_bars = [
        _bar("ABC", datetime(2024, 6, 5, 9, 30, tzinfo=ET), open_=100.0),
        _bar("ABC", datetime(2024, 6, 5, 9, 59, tzinfo=ET), open_=101.5),
    ]
    # SPY flat 500 → 500: 0%
    spy_bars = [
        _bar("SPY", datetime(2024, 6, 5, 9, 30, tzinfo=ET), open_=500.0),
        _bar("SPY", datetime(2024, 6, 5, 9, 59, tzinfo=ET), open_=500.0),
    ]
    rs = rs_score(stock_bars, spy_bars)
    assert rs == pytest.approx(1.5, abs=1e-6)


def test_rs_score_subtracts_spy_drift():
    # Stock +1%, SPY +0.3% → RS = 0.7
    stock_bars = [
        _bar("ABC", datetime(2024, 6, 5, 9, 30, tzinfo=ET), open_=100.0),
        _bar("ABC", datetime(2024, 6, 5, 9, 59, tzinfo=ET), open_=101.0),
    ]
    spy_bars = [
        _bar("SPY", datetime(2024, 6, 5, 9, 30, tzinfo=ET), open_=500.0),
        _bar("SPY", datetime(2024, 6, 5, 9, 59, tzinfo=ET), open_=501.5),  # +0.3%
    ]
    rs = rs_score(stock_bars, spy_bars)
    assert rs == pytest.approx(0.7, abs=1e-3)


def test_post_open_vwap_typical_price_volume_weighted():
    """Two bars after 10:00 ET. VWAP uses (H+L+C)/3 weighted by volume."""
    bars = [
        _bar(
            "ABC",
            datetime(2024, 6, 5, 10, 0, tzinfo=ET),
            open_=100.0, high=101.0, low=99.0, close=100.0,
            volume=1000.0,
        ),
        _bar(
            "ABC",
            datetime(2024, 6, 5, 10, 1, tzinfo=ET),
            open_=100.5, high=102.0, low=100.0, close=101.0,
            volume=2000.0,
        ),
    ]
    # typical_1 = (101+99+100)/3 = 100.0; typical_2 = (102+100+101)/3 = 101.0
    # vwap = (100.0*1000 + 101.0*2000) / 3000 = (100000 + 202000) / 3000 = 100.667
    vwap = post_open_vwap(bars)
    assert vwap == pytest.approx(100.6667, abs=1e-3)


def test_post_open_vwap_excludes_pre_open_bars():
    """Bars before 10:00 ET don't contribute to post-open VWAP."""
    bars = [
        _bar(  # pre-window — should be excluded
            "ABC",
            datetime(2024, 6, 5, 9, 45, tzinfo=ET),
            open_=99.0, high=99.0, low=99.0, close=99.0,
            volume=999_999.0,
        ),
        _bar(
            "ABC",
            datetime(2024, 6, 5, 10, 0, tzinfo=ET),
            open_=100.0, high=100.0, low=100.0, close=100.0,
            volume=1000.0,
        ),
    ]
    vwap = post_open_vwap(bars)
    assert vwap == pytest.approx(100.0, abs=1e-6)
