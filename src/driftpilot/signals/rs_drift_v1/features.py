"""Pure feature functions for RS-Drift v1.

All functions are pure — no I/O, no globals. Datetimes are timezone-aware
via the canonical `MinuteBar` (which calls `require_aware`).
"""

from __future__ import annotations

from datetime import date, datetime, time
from statistics import fmean
from zoneinfo import ZoneInfo

from driftpilot.clock import require_aware
from driftpilot.signals.features import MinuteBar


ET = ZoneInfo("America/New_York")


def _to_et(ts: datetime) -> datetime:
    return require_aware(ts).astimezone(ET)


def _et_time(bar: MinuteBar) -> time:
    return _to_et(bar.timestamp).time()


def _et_date(bar: MinuteBar) -> date:
    return _to_et(bar.timestamp).date()


def opening_range_high(bars: list[MinuteBar], session_date: date) -> float:
    """Highest high from 09:30 to 10:00 ET on `session_date`.

    Raises ValueError if no bars in the window.
    """
    window = [
        bar
        for bar in bars
        if _et_date(bar) == session_date
        and time(9, 30) <= _et_time(bar) < time(10, 0)
    ]
    if not window:
        raise ValueError(
            f"opening_range_high: no bars in 09:30–10:00 ET window on {session_date.isoformat()}"
        )
    return max(bar.high for bar in window)


def rs_score(
    stock_bars: list[MinuteBar],
    spy_bars: list[MinuteBar],
    *,
    t_start_et: time = time(9, 30),
    t_end_et: time = time(10, 0),
) -> float:
    """Returns (stock_pct_change - spy_pct_change) for the [t_start, t_end) window.

    Both inputs MUST be from the same session_date.
    Returns percentage points (1.5 means 1.5%, not 0.015).
    Raises ValueError if either side has no bars in the window.
    """
    stock_pct = _pct_change_in_window(stock_bars, t_start_et, t_end_et, "stock")
    spy_pct = _pct_change_in_window(spy_bars, t_start_et, t_end_et, "SPY")
    return (stock_pct - spy_pct) * 100.0


def _pct_change_in_window(
    bars: list[MinuteBar], t_start: time, t_end: time, label: str
) -> float:
    window = [bar for bar in bars if t_start <= _et_time(bar) < t_end]
    if not window:
        raise ValueError(f"{label}: no bars in {t_start}–{t_end} ET window")
    window.sort(key=lambda b: b.timestamp)
    open_price = window[0].open
    close_price = window[-1].close
    if open_price <= 0:
        raise ValueError(f"{label}: window open price must be positive")
    return close_price / open_price - 1.0


def post_open_vwap(
    bars: list[MinuteBar],
    *,
    t_start_et: time = time(10, 0),
    current_t: time | None = None,
) -> float:
    """Volume-weighted average price using typical (H+L+C)/3 from `t_start_et`
    through `current_t` (inclusive). If `current_t` is None, uses the latest
    bar's ET time.

    Raises ValueError if no qualifying bars or zero total volume.
    """
    if not bars:
        raise ValueError("post_open_vwap requires at least one bar")
    sorted_bars = sorted(bars, key=lambda b: b.timestamp)
    if current_t is None:
        current_t = _et_time(sorted_bars[-1])

    window = [
        bar
        for bar in sorted_bars
        if t_start_et <= _et_time(bar) <= current_t
    ]
    if not window:
        raise ValueError(
            f"post_open_vwap: no bars between {t_start_et} and {current_t} ET"
        )

    total_volume = sum(bar.volume for bar in window)
    if total_volume <= 0:
        raise ValueError("post_open_vwap: total volume must be positive")

    typical_volume_product = sum(
        ((bar.high + bar.low + bar.close) / 3.0) * bar.volume for bar in window
    )
    return typical_volume_product / total_volume


def adv_20day(daily_bars: list[MinuteBar], current_date: date) -> int:
    """Average daily volume across the 20 trading days BEFORE `current_date`.

    `current_date` is EXCLUDED from the average — same lookahead-bias rule as
    Stationary Ghost / Whale-Tail relative_volume. Test
    `test_adv_excludes_current_date.py` enforces this.

    Treats each bar's `timestamp.date()` (in ET) as that bar's session date and
    aggregates volume per session. Returns int (rounded).

    Raises ValueError if fewer than 20 prior trading days are available.
    """
    if not daily_bars:
        raise ValueError("adv_20day requires at least one bar")

    # Aggregate volume per ET session date.
    by_session: dict[date, float] = {}
    for bar in daily_bars:
        session = _et_date(bar)
        by_session[session] = by_session.get(session, 0.0) + bar.volume

    # Exclude current_date from the average — the lookahead-bias guard.
    prior_dates = sorted(d for d in by_session if d < current_date)
    if len(prior_dates) < 20:
        raise ValueError(
            f"adv_20day requires 20 prior trading days, found {len(prior_dates)}"
        )

    last_20 = prior_dates[-20:]
    avg = fmean(by_session[d] for d in last_20)
    return int(round(avg))


__all__ = [
    "ET",
    "opening_range_high",
    "rs_score",
    "post_open_vwap",
    "adv_20day",
]
