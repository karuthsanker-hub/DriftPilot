"""Lookahead-bias guard: adv_20day must EXCLUDE current_date from the average."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.rs_drift_v1.features import adv_20day


ET = ZoneInfo("America/New_York")


def _bar(symbol: str, ts: datetime, volume: float) -> MinuteBar:
    return MinuteBar(symbol=symbol, timestamp=ts, open=10.0, high=10.0, low=10.0, close=10.0, volume=volume)


def _session_bars(symbol: str, session_dates: list[date], volume_per_day: float) -> list[MinuteBar]:
    """Build one bar at noon ET on each session date with the given volume."""
    bars: list[MinuteBar] = []
    for d in session_dates:
        ts = datetime(d.year, d.month, d.day, 12, 0, tzinfo=ET)
        bars.append(_bar(symbol, ts, volume_per_day))
    return bars


def test_adv_20day_excludes_current_date_from_average():
    """If the current bar's volume were included it would dominate the average.
    Excluding it keeps the average == the prior days' baseline volume.
    """
    current_date = date(2024, 6, 20)
    prior_dates = [current_date - timedelta(days=i) for i in range(1, 21)]
    prior_dates.sort()

    # 20 prior sessions at volume 1_000_000; current session at volume 100_000_000.
    bars = _session_bars("ABC", prior_dates, volume_per_day=1_000_000.0)
    bars.extend(_session_bars("ABC", [current_date], volume_per_day=100_000_000.0))

    result = adv_20day(bars, current_date)

    # If the current bar were included, the 21-day mean would be ~5.7M.
    # Excluding it gives exactly 1_000_000.
    assert result == 1_000_000, (
        f"adv_20day must exclude current_date — got {result}, expected 1_000_000. "
        "If you see ~5.7M, the current bar leaked into the lookback average."
    )


def test_adv_20day_requires_at_least_20_prior_sessions():
    current_date = date(2024, 6, 20)
    prior_dates = [current_date - timedelta(days=i) for i in range(1, 11)]  # only 10 priors
    bars = _session_bars("ABC", prior_dates, volume_per_day=1_000_000.0)

    with pytest.raises(ValueError, match="20 prior trading days"):
        adv_20day(bars, current_date)


def test_adv_20day_uses_only_last_20_prior_sessions():
    """If more than 20 prior sessions are available, use the most recent 20."""
    current_date = date(2024, 6, 20)
    far_past = [current_date - timedelta(days=i) for i in range(40, 21, -1)]  # ancient sessions
    recent = [current_date - timedelta(days=i) for i in range(20, 0, -1)]  # last 20 priors

    bars = _session_bars("ABC", far_past, volume_per_day=999_999_999.0)
    bars.extend(_session_bars("ABC", recent, volume_per_day=2_500_000.0))

    result = adv_20day(bars, current_date)
    assert result == 2_500_000, (
        f"expected last-20-prior-sessions to drive the average; got {result}"
    )
