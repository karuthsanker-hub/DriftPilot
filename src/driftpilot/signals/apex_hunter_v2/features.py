"""Pure feature functions for Apex Hunter v2.2.

All functions are pure — no I/O, no globals. The canonical `MinuteBar`
enforces timezone-aware datetimes.
"""

from __future__ import annotations

import math
from statistics import fmean

import numpy as np

from driftpilot.signals.features import MinuteBar


def calculate_ewmlr(
    prices: list[float],
    half_life_mins: int = 15,
) -> tuple[float, float]:
    """Exponentially Weighted Moving Linear Regression.

    Returns (weighted_slope, weighted_r2).

    Cold-start guard: requires len(prices) >= half_life_mins * 2 (warm-up).
    The Apex spec requires the first 10:30 ET cycle to be seeded with bars
    going back to 09:00 ET — calling EWMLR with insufficient history is a
    spec violation, not a degraded mode, so this raises.

    Algorithm (per spec):
      1. weights w_i = exp(-ln(2) * (n - 1 - i) / half_life) for i in [0, n).
         The newest bar (index n-1) has weight 1.0; older bars decay.
      2. x_i = i (monotone index along the bar sequence).
      3. weighted means: x_bar = Σ(w_i x_i) / Σ(w_i); y_bar = Σ(w_i y_i) / Σ(w_i).
      4. slope = Σ(w_i (x_i - x_bar)(y_i - y_bar)) / Σ(w_i (x_i - x_bar)^2).
      5. weighted_residual_ss = Σ(w_i * (y_i - (y_bar + slope*(x_i - x_bar)))^2)
         weighted_total_ss    = Σ(w_i * (y_i - y_bar)^2)
         r2 = 1 - residual_ss / total_ss, clamped to [0, 1].
         If total_ss is 0 (degenerate flat input), r2 is returned as 0.0.
    """
    if half_life_mins <= 0:
        raise ValueError("half_life_mins must be positive")
    min_required = half_life_mins * 2
    if len(prices) < min_required:
        raise ValueError(
            f"calculate_ewmlr requires warm-up: at least {min_required} bars, "
            f"got {len(prices)}"
        )

    y = np.asarray(prices, dtype=float)
    n = y.shape[0]
    x = np.arange(n, dtype=float)
    decay = math.log(2.0) / float(half_life_mins)
    # newest bar has weight 1.0 (age = 0); oldest has age = n-1.
    ages = (n - 1) - x
    w = np.exp(-decay * ages)
    w_sum = float(w.sum())
    if w_sum <= 0:
        raise ValueError("ewmlr weights sum to zero — degenerate")

    x_bar = float((w * x).sum() / w_sum)
    y_bar = float((w * y).sum() / w_sum)
    dx = x - x_bar
    dy = y - y_bar
    denom = float((w * dx * dx).sum())
    if denom <= 0:
        # All x_i collapsed to x_bar — impossible for monotone index, defensive.
        return 0.0, 0.0
    slope = float((w * dx * dy).sum() / denom)

    residual = dy - slope * dx
    residual_ss = float((w * residual * residual).sum())
    total_ss = float((w * dy * dy).sum())
    if total_ss <= 0:
        # Flat input — r2 undefined; return 0.0 (callers test < 0.1 OR isnan).
        return slope, 0.0
    r2 = 1.0 - residual_ss / total_ss
    if r2 < 0.0:
        r2 = 0.0
    elif r2 > 1.0:
        r2 = 1.0
    return slope, r2


def calculate_acceleration(
    slope_history: list[float],
    window: int = 10,
) -> float:
    """Second derivative of price — slope-of-slopes.

    Take the last `window` slope observations and fit an unweighted linear
    regression on them; return that fit's slope. Positive = trend accelerating,
    negative = trend rounding (exhaustion).

    Raises ValueError if len(slope_history) < window.
    """
    if window <= 1:
        raise ValueError("window must be > 1")
    if len(slope_history) < window:
        raise ValueError(
            f"calculate_acceleration requires at least {window} slopes, "
            f"got {len(slope_history)}"
        )
    y = np.asarray(slope_history[-window:], dtype=float)
    x = np.arange(window, dtype=float)
    x_bar = float(x.mean())
    y_bar = float(y.mean())
    dx = x - x_bar
    dy = y - y_bar
    denom = float((dx * dx).sum())
    if denom <= 0:
        return 0.0
    return float((dx * dy).sum() / denom)


def relative_alpha(ticker_slope: float, spy_slope: float) -> float:
    """Ratio of ticker slope to SPY slope.

    If SPY slope is exactly zero, return +inf when ticker_slope > 0, -inf when
    ticker_slope < 0, and 0.0 when both are zero (truly degenerate).
    """
    if spy_slope == 0:
        if ticker_slope > 0:
            return math.inf
        if ticker_slope < 0:
            return -math.inf
        return 0.0
    return ticker_slope / spy_slope


def correlation_to_spy(
    ticker_returns: list[float],
    spy_returns: list[float],
    window: int = 30,
) -> float:
    """Pearson correlation between the trailing `window` ticker and SPY returns.

    Requires both series to have at least `window` observations. Aligns by
    taking the last `window` of each. Raises ValueError on insufficient data.
    Returns 0.0 if either series has zero variance over the window (Pearson
    is undefined; treat as no relationship for filter purposes).
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    if len(ticker_returns) < window or len(spy_returns) < window:
        raise ValueError(
            f"correlation_to_spy requires at least {window} returns on both "
            f"sides; got ticker={len(ticker_returns)}, spy={len(spy_returns)}"
        )
    a = np.asarray(ticker_returns[-window:], dtype=float)
    b = np.asarray(spy_returns[-window:], dtype=float)
    a_bar = float(a.mean())
    b_bar = float(b.mean())
    da = a - a_bar
    db = b - b_bar
    da_ss = float((da * da).sum())
    db_ss = float((db * db).sum())
    if da_ss <= 0 or db_ss <= 0:
        return 0.0
    return float((da * db).sum() / math.sqrt(da_ss * db_ss))


def atr(bars: list[MinuteBar], period: int = 14) -> float:
    """Wilder's Average True Range over the trailing bars.

    True Range_i = max(high_i - low_i, |high_i - close_{i-1}|, |low_i - close_{i-1}|).
    First ATR seed = simple mean of the first `period` TR values; thereafter
    Wilder's recurrence: ATR_i = (ATR_{i-1} * (period - 1) + TR_i) / period.

    Raises ValueError if len(bars) < period + 1 (need a prior bar to seed TR).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(bars) < period + 1:
        raise ValueError(
            f"atr requires at least {period + 1} bars, got {len(bars)}"
        )
    tr: list[float] = []
    for i in range(1, len(bars)):
        prev = bars[i - 1]
        cur = bars[i]
        tr.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )
    if len(tr) < period:
        raise ValueError(
            f"atr: insufficient TR history ({len(tr)}) for period {period}"
        )
    atr_value = fmean(tr[:period])
    for i in range(period, len(tr)):
        atr_value = (atr_value * (period - 1) + tr[i]) / period
    return atr_value


__all__ = [
    "calculate_ewmlr",
    "calculate_acceleration",
    "relative_alpha",
    "correlation_to_spy",
    "atr",
]
