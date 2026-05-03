"""Pure feature functions for Whale-Tail v1.

All functions are pure - no I/O, no globals. Datetimes are timezone-aware
via the canonical `MinuteBar` (which calls `require_aware`).
"""

from __future__ import annotations

import math
from statistics import fmean

from driftpilot.signals.features import MinuteBar


def relative_volume(
    current_bar: MinuteBar,
    lookback_bars: list[MinuteBar],
    lookback_n: int = 15,
) -> float:
    """current_bar is EXCLUDED from the lookback average.

    Returns current_bar.volume / mean(lookback_bars[-lookback_n:].volume).

    Enforced by tests/signals/whale_tail_v1/
    test_relative_volume_excludes_current_bar.py.
    """
    if lookback_n <= 0:
        raise ValueError("lookback_n must be positive")
    if len(lookback_bars) < lookback_n:
        raise ValueError(
            f"relative_volume requires at least {lookback_n} prior bars, "
            f"got {len(lookback_bars)}"
        )
    window = lookback_bars[-lookback_n:]
    avg = fmean(b.volume for b in window)
    if avg <= 0:
        raise ValueError("lookback average volume must be positive")
    return current_bar.volume / avg


def _true_ranges(bars: list[MinuteBar]) -> list[float]:
    """True range series of length len(bars)-1 (each TR needs a prior bar)."""
    trs: list[float] = []
    for i in range(1, len(bars)):
        prev = bars[i - 1]
        cur = bars[i]
        trs.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )
    return trs


def atr(bars: list[MinuteBar], period: int = 20) -> float:
    """Wilder's 1978 ATR.

    Seed = simple mean of the first `period` true ranges. Subsequent values
    follow Wilder's recurrence:
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period

    Requires at least `period + 1` bars (period TRs need period+1 bars).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(bars) < period + 1:
        raise ValueError(
            f"atr requires at least {period + 1} bars, got {len(bars)}"
        )
    trs = _true_ranges(bars)
    if len(trs) < period:
        raise ValueError(
            f"atr requires at least {period} true ranges, got {len(trs)}"
        )
    atr_value = fmean(trs[:period])
    for i in range(period, len(trs)):
        atr_value = (atr_value * (period - 1) + trs[i]) / period
    return atr_value


def compression_high(bars: list[MinuteBar], window: int = 15) -> float:
    """max high in last `window` bars."""
    if window <= 0:
        raise ValueError("window must be positive")
    if len(bars) < window:
        raise ValueError(
            f"compression_high requires at least {window} bars, got {len(bars)}"
        )
    return max(b.high for b in bars[-window:])


def compression_low(bars: list[MinuteBar], window: int = 15) -> float:
    """min low in last `window` bars. Used for distribution-trap invalidation."""
    if window <= 0:
        raise ValueError("window must be positive")
    if len(bars) < window:
        raise ValueError(
            f"compression_low requires at least {window} bars, got {len(bars)}"
        )
    return min(b.low for b in bars[-window:])


def compression_midpoint(bars: list[MinuteBar], window: int = 15) -> float:
    """midpoint of compression window."""
    return (compression_high(bars, window) + compression_low(bars, window)) / 2.0


def compression_score(
    bars: list[MinuteBar],
    window: int = 15,
    atr_value: float | None = None,
) -> float:
    """(high(window) - low(window)) / atr_value. Lower = more compressed.

    Returns math.inf if atr_value is zero (degenerate flat-bar case).
    Raises ValueError if atr_value is negative or None.
    """
    if atr_value is None:
        raise ValueError("atr_value must be provided")
    if atr_value < 0:
        raise ValueError("atr_value must be non-negative")
    high = compression_high(bars, window)
    low = compression_low(bars, window)
    range_ = high - low
    if atr_value == 0:
        return math.inf
    return range_ / atr_value


def range_position_pct(
    current_price: float,
    day_high: float,
    day_low: float,
) -> float:
    """(current - low) / (high - low). 1.0 = at day high.

    Raises ValueError if high <= low (degenerate).
    """
    if day_high <= day_low:
        raise ValueError("day_high must be strictly greater than day_low")
    return (current_price - day_low) / (day_high - day_low)


__all__ = [
    "relative_volume",
    "atr",
    "compression_score",
    "compression_high",
    "compression_low",
    "compression_midpoint",
    "range_position_pct",
]
