from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from statistics import fmean

from driftpilot.signals.features import MinuteBar, compute_session_vwap, latest_session_bars, return_over_minutes


GREEN_5M_RETURN_FLOOR = -0.001
CAUTION_5M_RETURN_FLOOR = -0.003
VWAP_ATR_BREAK_MULTIPLE = 1.5


class Regime(StrEnum):
    GREEN = "GREEN"
    CAUTION = "CAUTION"
    RED = "RED"


@dataclass(frozen=True, slots=True)
class IndexRegimeMetrics:
    symbol: str
    price: float
    session_vwap: float
    return_5m: float
    return_15m: float
    vwap_distance: float
    vwap_distance_pct: float
    atr: float
    atr_distance_from_vwap: float
    regime: Regime
    reason: str


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    regime: Regime
    spy: IndexRegimeMetrics

    @property
    def benchmark_return_15m(self) -> float:
        return self.spy.return_15m

    @property
    def reason(self) -> str:
        return self.spy.reason


def compute_atr(bars: list[MinuteBar], *, lookback: int = 14) -> float:
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    ordered = sorted(bars, key=lambda item: item.timestamp)
    if not ordered:
        raise ValueError("at least one bar is required")

    true_ranges: list[float] = []
    previous_close: float | None = None
    for bar in ordered:
        if previous_close is None:
            true_range = bar.high - bar.low
        else:
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        true_ranges.append(true_range)
        previous_close = bar.close
    return fmean(true_ranges[-lookback:])


def compute_index_regime_metrics(bars: list[MinuteBar], *, symbol: str) -> IndexRegimeMetrics:
    ordered = sorted(bars, key=lambda item: item.timestamp)
    if not ordered:
        raise ValueError(f"{symbol} bars are required")
    normalized = symbol.upper()
    if any(bar.symbol.upper() != normalized for bar in ordered):
        raise ValueError(f"all bars must be for {normalized}")

    session_bars = latest_session_bars(ordered)
    latest = session_bars[-1]
    vwap = compute_session_vwap(session_bars)
    return_5m, _ = return_over_minutes(session_bars, 5)
    return_15m, _ = return_over_minutes(session_bars, 15)
    atr = compute_atr(session_bars)
    vwap_distance = latest.close - vwap
    vwap_distance_pct = latest.close / vwap - 1.0
    atr_distance_from_vwap = (vwap - latest.close) / atr if atr > 0 else 0.0

    broken_below_atr = latest.close < vwap and atr_distance_from_vwap > VWAP_ATR_BREAK_MULTIPLE
    if return_5m < CAUTION_5M_RETURN_FLOOR:
        regime = Regime.RED
        reason = f"{normalized} 5m return below -0.3%"
    elif broken_below_atr:
        regime = Regime.RED
        reason = f"{normalized} below VWAP by more than 1.5x ATR"
    elif latest.close > vwap and return_5m > GREEN_5M_RETURN_FLOOR:
        regime = Regime.GREEN
        reason = f"{normalized} above VWAP and 5m return above -0.1%"
    elif latest.close < vwap and return_5m > CAUTION_5M_RETURN_FLOOR:
        regime = Regime.CAUTION
        reason = f"{normalized} below VWAP while 5m return above -0.3%"
    else:
        regime = Regime.RED
        reason = f"{normalized} failed GREEN/CAUTION thresholds"

    return IndexRegimeMetrics(
        symbol=normalized,
        price=latest.close,
        session_vwap=vwap,
        return_5m=return_5m,
        return_15m=return_15m,
        vwap_distance=vwap_distance,
        vwap_distance_pct=vwap_distance_pct,
        atr=atr,
        atr_distance_from_vwap=atr_distance_from_vwap,
        regime=regime,
        reason=reason,
    )


def compute_market_regime(spy_bars: list[MinuteBar]) -> RegimeSnapshot:
    spy = compute_index_regime_metrics(spy_bars, symbol="SPY")
    return RegimeSnapshot(regime=spy.regime, spy=spy)
