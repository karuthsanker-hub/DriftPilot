"""Multi-feature market regime detector (Refactor Plan v2 § Phase C).

Replaces the SPY-only GREEN/CAUTION/RED scalar (still in
src/driftpilot/signals/regime.py) with a richer, named regime
classification that the signal router (Phase D) keys off.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from statistics import pstdev
from typing import Mapping

from driftpilot.clock import DriftPilotClock, require_aware
from driftpilot.signals.features import (
    MinuteBar,
    compute_session_vwap,
    latest_session_bars,
    return_over_minutes,
)
from driftpilot.signals.regime import compute_atr


BASELINE_VOLATILITY_PCT = 0.5
NEWS_SHOCK_VOL_MULT = 2.0
NEWS_SHOCK_5M_RETURN_THRESHOLD = 0.5
RANGE_30M_RETURN_THRESHOLD = 0.1
RANGE_BREADTH_LOWER = 40.0
RANGE_BREADTH_UPPER = 60.0
TREND_30M_RETURN_THRESHOLD = 0.3
TREND_BULL_BREADTH_THRESHOLD = 65.0
TRADING_MINUTES_PER_YEAR = 252 * 390
ADV_DECL_RATIO_CAP = 100.0
MIN_BARS_FOR_DETECTION = 30


class MarketRegime(StrEnum):
    TREND_BULL_LOW_VOL = "trend_bull_low_vol"
    TREND_BULL_HIGH_VOL = "trend_bull_high_vol"
    TREND_BEAR = "trend_bear"
    RANGE_BOUND = "range_bound"
    CHOPPY = "choppy"
    NEWS_SHOCK = "news_shock"
    OPENING_DRIFT = "opening_drift"
    CLOSING_DRIFT = "closing_drift"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RegimeSnapshotV2:
    timestamp_et: datetime
    regime: MarketRegime
    spy_5m_return_pct: float
    spy_15m_return_pct: float
    spy_30m_return_pct: float
    spy_atr_distance_from_vwap: float
    breadth_above_vwap_pct: float
    breadth_advance_decline_ratio: float
    realized_volatility_5m: float
    time_of_day_bucket: str
    minutes_until_close: int
    confidence_score: float


def _time_of_day_bucket(timestamp_et: datetime) -> str:
    """Time-of-day classification for the dashboard label.

    Note: this is the human-readable bucket only. The classification rule
    for CLOSING_DRIFT (spec rule 3: time_et >= 15:00 -> CLOSING_DRIFT)
    is applied directly against the hour in `detect_regime`, NOT against
    this bucket — otherwise the 15:00-15:30 window would silently fall
    through to CHOPPY. See `_in_closing_window` below.
    """
    minutes = timestamp_et.hour * 60 + timestamp_et.minute
    if 9 * 60 + 30 <= minutes < 10 * 60:
        return "open"
    if 10 * 60 <= minutes < 12 * 60:
        return "morning"
    if 12 * 60 <= minutes < 14 * 60:
        return "midday"
    if 14 * 60 <= minutes < 15 * 60:
        return "afternoon"
    if 15 * 60 <= minutes <= 16 * 60:
        return "close"
    return "off_session"


def _in_closing_window(timestamp_et: datetime) -> bool:
    """True from 15:00 ET to 16:00 ET inclusive — the spec's CLOSING_DRIFT window."""
    minutes = timestamp_et.hour * 60 + timestamp_et.minute
    return 15 * 60 <= minutes <= 16 * 60


def _minutes_until_close(timestamp_et: datetime) -> int:
    close_dt = datetime.combine(
        timestamp_et.date(), time(16, 0), tzinfo=timestamp_et.tzinfo
    )
    delta = close_dt - timestamp_et
    seconds = delta.total_seconds()
    if seconds <= 0:
        return 0
    return int(seconds // 60)


def _realized_vol_5m_pct(session_bars: list[MinuteBar]) -> float:
    if len(session_bars) < 6:
        return 0.0
    tail = session_bars[-6:]
    returns: list[float] = []
    for prev, cur in zip(tail[:-1], tail[1:]):
        if prev.close <= 0:
            continue
        returns.append(cur.close / prev.close - 1.0)
    if len(returns) < 2:
        return 0.0
    sigma = pstdev(returns)
    annualized = sigma * math.sqrt(TRADING_MINUTES_PER_YEAR)
    return annualized * 100.0


def _breadth(
    universe_bars: Mapping[str, list[MinuteBar]],
) -> tuple[float, float]:
    above_vwap_count = 0
    total = 0
    advancers = 0
    decliners = 0
    for symbol, bars in universe_bars.items():
        if not bars:
            continue
        try:
            session_bars = latest_session_bars(bars)
        except ValueError:
            # Symbol has no bars on the latest session date → skip silently;
            # malformed-symbol exclusion is the breadth metric's intent.
            continue
        if not session_bars:
            continue
        latest = session_bars[-1]
        try:
            vwap = compute_session_vwap(session_bars)
        except ValueError:
            # Zero/negative volume across the session → skip silently for
            # the same malformed-symbol reason as above.
            continue
        total += 1
        if latest.close > vwap:
            above_vwap_count += 1
        session_open = session_bars[0].open
        if latest.close > session_open:
            advancers += 1
        elif latest.close < session_open:
            decliners += 1
    if total == 0:
        return 0.0, 0.0
    above_pct = (above_vwap_count / total) * 100.0
    if decliners == 0:
        ratio = ADV_DECL_RATIO_CAP if advancers > 0 else 0.0
    else:
        ratio = min(advancers / decliners, ADV_DECL_RATIO_CAP)
    return above_pct, ratio


def _normalized_distance(value: float, threshold: float) -> float:
    """Return distance from threshold normalized by threshold magnitude, capped at 1.0."""
    if threshold == 0:
        return 1.0 if value != 0 else 0.0
    distance = abs(value - threshold) / abs(threshold)
    return min(distance, 1.0)


def detect_regime(
    spy_bars: list[MinuteBar],
    universe_bars: Mapping[str, list[MinuteBar]],
    clock: DriftPilotClock,
) -> RegimeSnapshotV2:
    for bar in spy_bars:
        require_aware(bar.timestamp)

    if len(spy_bars) < MIN_BARS_FOR_DETECTION:
        if spy_bars:
            ordered = sorted(spy_bars, key=lambda item: item.timestamp)
            ts_et = clock.to_et(ordered[-1].timestamp)
        else:
            ts_et = clock.now_et()
        return RegimeSnapshotV2(
            timestamp_et=ts_et,
            regime=MarketRegime.UNKNOWN,
            spy_5m_return_pct=0.0,
            spy_15m_return_pct=0.0,
            spy_30m_return_pct=0.0,
            spy_atr_distance_from_vwap=0.0,
            breadth_above_vwap_pct=0.0,
            breadth_advance_decline_ratio=0.0,
            realized_volatility_5m=0.0,
            time_of_day_bucket=_time_of_day_bucket(ts_et),
            minutes_until_close=_minutes_until_close(ts_et),
            confidence_score=0.0,
        )

    ordered = sorted(spy_bars, key=lambda item: item.timestamp)
    latest = ordered[-1]
    timestamp_et = clock.to_et(latest.timestamp)
    session_bars = latest_session_bars(ordered)

    spy_5m = return_over_minutes(ordered, 5)[0] * 100.0
    spy_15m = return_over_minutes(ordered, 15)[0] * 100.0
    spy_30m = return_over_minutes(ordered, 30)[0] * 100.0

    vwap = compute_session_vwap(session_bars)
    atr = compute_atr(ordered, lookback=14)
    atr_distance = (latest.close - vwap) / atr if atr > 0 else 0.0

    realized_vol = _realized_vol_5m_pct(session_bars)
    breadth_above_pct, adv_decl_ratio = _breadth(universe_bars)

    bucket = _time_of_day_bucket(timestamp_et)
    minutes_to_close = _minutes_until_close(timestamp_et)

    baseline_vol = BASELINE_VOLATILITY_PCT
    distances: list[float] = []
    regime: MarketRegime

    news_shock_vol_threshold = NEWS_SHOCK_VOL_MULT * baseline_vol
    if (
        realized_vol > news_shock_vol_threshold
        and abs(spy_5m) > NEWS_SHOCK_5M_RETURN_THRESHOLD
    ):
        regime = MarketRegime.NEWS_SHOCK
        distances.append(_normalized_distance(realized_vol, news_shock_vol_threshold))
        distances.append(_normalized_distance(abs(spy_5m), NEWS_SHOCK_5M_RETURN_THRESHOLD))
    elif bucket == "open":
        regime = MarketRegime.OPENING_DRIFT
        distances.append(1.0)
    elif _in_closing_window(timestamp_et):
        # Spec rule 3: time_et >= 15:00 ET → CLOSING_DRIFT. Use the
        # explicit window check here so the 15:00–15:30 half hour does
        # not silently fall through to CHOPPY (the human-readable
        # `time_of_day_bucket` only flips to "close" at 15:30).
        regime = MarketRegime.CLOSING_DRIFT
        distances.append(1.0)
    elif (
        abs(spy_30m) < RANGE_30M_RETURN_THRESHOLD
        and RANGE_BREADTH_LOWER <= breadth_above_pct <= RANGE_BREADTH_UPPER
    ):
        regime = MarketRegime.RANGE_BOUND
        distances.append(_normalized_distance(abs(spy_30m), RANGE_30M_RETURN_THRESHOLD))
        midpoint = (RANGE_BREADTH_LOWER + RANGE_BREADTH_UPPER) / 2.0
        half_width = (RANGE_BREADTH_UPPER - RANGE_BREADTH_LOWER) / 2.0
        breadth_dev = abs(breadth_above_pct - midpoint)
        distances.append(min((half_width - breadth_dev) / half_width, 1.0))
    elif (
        spy_30m > TREND_30M_RETURN_THRESHOLD
        and breadth_above_pct > TREND_BULL_BREADTH_THRESHOLD
        and realized_vol < 1.0 * baseline_vol
    ):
        regime = MarketRegime.TREND_BULL_LOW_VOL
        distances.append(_normalized_distance(spy_30m, TREND_30M_RETURN_THRESHOLD))
        distances.append(
            _normalized_distance(breadth_above_pct, TREND_BULL_BREADTH_THRESHOLD)
        )
        distances.append(_normalized_distance(realized_vol, baseline_vol))
    elif spy_30m > TREND_30M_RETURN_THRESHOLD and realized_vol >= 1.0 * baseline_vol:
        regime = MarketRegime.TREND_BULL_HIGH_VOL
        distances.append(_normalized_distance(spy_30m, TREND_30M_RETURN_THRESHOLD))
        distances.append(_normalized_distance(realized_vol, baseline_vol))
    elif spy_30m < -TREND_30M_RETURN_THRESHOLD:
        regime = MarketRegime.TREND_BEAR
        distances.append(_normalized_distance(abs(spy_30m), TREND_30M_RETURN_THRESHOLD))
    else:
        regime = MarketRegime.CHOPPY
        distances.append(0.3)

    if distances:
        confidence = sum(distances) / len(distances)
    else:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return RegimeSnapshotV2(
        timestamp_et=timestamp_et,
        regime=regime,
        spy_5m_return_pct=spy_5m,
        spy_15m_return_pct=spy_15m,
        spy_30m_return_pct=spy_30m,
        spy_atr_distance_from_vwap=atr_distance,
        breadth_above_vwap_pct=breadth_above_pct,
        breadth_advance_decline_ratio=adv_decl_ratio,
        realized_volatility_5m=realized_vol,
        time_of_day_bucket=bucket,
        minutes_until_close=minutes_to_close,
        confidence_score=confidence,
    )
