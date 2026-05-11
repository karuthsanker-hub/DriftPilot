"""Pure feature functions for Stationary Ghost v1.

All functions are pure — no I/O, no globals. Datetimes are timezone-aware
via the canonical `MinuteBar` (which calls `require_aware`).
"""

from __future__ import annotations

from statistics import fmean, pstdev

from driftpilot.signals.features import MinuteBar


def bollinger_bands(
    bars: list[MinuteBar],
    period: int = 15,
    std_dev: float = 2.0,
) -> tuple[float, float, float]:
    """Returns (upper_band, middle, lower_band) over the last `period` closes.

    Uses population stdev (Wilder/typical TA convention) on the trailing
    `period` closes. Raises ValueError if fewer than `period` bars are
    provided.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(bars) < period:
        raise ValueError(
            f"bollinger_bands requires at least {period} bars, got {len(bars)}"
        )
    window = [b.close for b in bars[-period:]]
    middle = fmean(window)
    std = pstdev(window)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def distance_z_score(price: float, mean: float, std: float) -> float:
    """Returns (price - mean) / std. Negative => price below mean.

    Raises ValueError if std is non-positive (degenerate window).
    """
    if std <= 0:
        raise ValueError("std must be positive")
    return (price - mean) / std


def relative_volume(
    current_bar: MinuteBar,
    lookback_bars: list[MinuteBar],
    lookback_n: int = 15,
) -> float:
    """current_bar is EXCLUDED from the lookback average.

    Returns current_bar.volume / mean(lookback_bars[-lookback_n:].volume).

    The exclusion is enforced by tests/signals/stationary_ghost_v1/
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


def _wilder_smooth(values: list[float], period: int) -> list[float]:
    """Wilder's 1978 smoothing.

    First smoothed value at index `period - 1` is the simple sum of the
    first `period` values (Wilder's seeding). Each subsequent value is:
        smoothed[i] = smoothed[i-1] - smoothed[i-1]/period + value[i]
    Returns a list aligned to `values` with leading entries set to 0.0
    until the seeding index. Length equals len(values).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        raise ValueError(
            f"_wilder_smooth requires at least {period} values, got {len(values)}"
        )
    smoothed: list[float] = [0.0] * len(values)
    seed = sum(values[:period])
    smoothed[period - 1] = seed
    for i in range(period, len(values)):
        prev = smoothed[i - 1]
        smoothed[i] = prev - prev / period + values[i]
    return smoothed


def adx(bars: list[MinuteBar], period: int = 14) -> float:
    """Wilder's 1978 Average Directional Index.

    Recipe (per spec):
      1. DM+ = max(high-prev_high, 0) if (high-prev_high) > (prev_low-low) else 0
         DM- = max(prev_low-low, 0)  if (prev_low-low) > (high-prev_high) else 0
      2. TR  = max(high-low, |high-prev_close|, |low-prev_close|)
      3. Wilder-smooth DM+, DM-, TR over `period`.
      4. DI+ = 100 * smoothed_DM+ / smoothed_TR
         DI- = 100 * smoothed_DM- / smoothed_TR
         DX  = 100 * |DI+ - DI-| / (DI+ + DI-)
      5. ADX = Wilder-smoothed DX over `period`.

    Requires at least 2*period bars (period for DI seeding, another period
    of DX for the ADX seeding). Raises ValueError otherwise.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    min_required = 2 * period + 1
    if len(bars) < min_required:
        raise ValueError(
            f"adx requires at least {min_required} bars, got {len(bars)}"
        )

    # Per-bar primitives (length n-1, since each needs a prior bar).
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr: list[float] = []
    for i in range(1, len(bars)):
        prev = bars[i - 1]
        cur = bars[i]
        up = cur.high - prev.high
        down = prev.low - cur.low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )

    sm_plus = _wilder_smooth(plus_dm, period)
    sm_minus = _wilder_smooth(minus_dm, period)
    sm_tr = _wilder_smooth(tr, period)

    # Compute DX where smoothed TR is non-zero, starting at index period-1.
    dx_series: list[float] = []
    for i in range(period - 1, len(sm_tr)):
        if sm_tr[i] <= 0:
            dx_series.append(0.0)
            continue
        di_plus = 100.0 * sm_plus[i] / sm_tr[i]
        di_minus = 100.0 * sm_minus[i] / sm_tr[i]
        denom = di_plus + di_minus
        if denom <= 0:
            dx_series.append(0.0)
            continue
        dx_series.append(100.0 * abs(di_plus - di_minus) / denom)

    if len(dx_series) < period:
        raise ValueError(
            f"adx: insufficient DX history ({len(dx_series)}) for period {period}"
        )

    # ADX = Wilder average of DX series. Seed with simple mean of first
    # `period` DX values, then continue Wilder recurrence.
    adx_value = fmean(dx_series[:period])
    for i in range(period, len(dx_series)):
        adx_value = (adx_value * (period - 1) + dx_series[i]) / period
    return adx_value


__all__ = [
    "bollinger_bands",
    "distance_z_score",
    "relative_volume",
    "adx",
]
