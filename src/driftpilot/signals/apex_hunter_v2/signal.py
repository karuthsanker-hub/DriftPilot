"""Apex Hunter v2.2 — SignalProtocol implementation.

EWMLR-based institutional-drift detection: tickers whose 90-minute weighted
linear regression shows positive, accelerating slope with strong fit (R²),
high relative alpha vs SPY, and meaningful correlation to SPY.

Filter chain order matches the spec; the first failing filter determines
the surfaced BlockedReason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from driftpilot.clock import require_aware
from driftpilot.signals.apex_hunter_v2.config import (
    ATR_PERIOD,
    DEFAULT_CORR_MIN,
    DEFAULT_R2_THRESHOLD,
    HALF_LIFE_MINS,
    RELATIVE_ALPHA_MIN,
    SCAN_END_TIME_ET,
    SCAN_START_TIME_ET,
    SIGNAL_NAME,
    SIGNAL_VERSION,
    WINDOW_MINS,
)
from driftpilot.signals.apex_hunter_v2.exits import evaluate_exit as _evaluate_exit
from driftpilot.signals.apex_hunter_v2.features import (
    atr,
    calculate_acceleration,
    calculate_ewmlr,
    correlation_to_spy,
    relative_alpha,
)
from driftpilot.signals.base import Candidate, ExitDecision
from driftpilot.signals.features import DEFAULT_RVOL_LOOKBACK, MinuteBar, Quote
from driftpilot.signals.regime import RegimeSnapshot, compute_market_regime
from driftpilot.states import BlockedReason


_ET = ZoneInfo("America/New_York")

# Number of trailing slope observations consumed by the acceleration filter.
ACCELERATION_WINDOW = 10
# Number of trailing 1-bar returns consumed by the SPY correlation filter.
CORRELATION_WINDOW = 30
# Top-percentile threshold (0.01 = top 1%).
TOP_PERCENTILE = 0.01


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


_SCAN_START = _parse_hhmm(SCAN_START_TIME_ET)
_SCAN_END = _parse_hhmm(SCAN_END_TIME_ET)


def _within_scan_window(latest_ts: datetime) -> bool:
    et = require_aware(latest_ts).astimezone(_ET).time()
    return _SCAN_START <= et <= _SCAN_END


def _bar_returns(bars: list[MinuteBar]) -> list[float]:
    """One-bar percent returns, length = len(bars) - 1."""
    out: list[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        if prev_close <= 0:
            out.append(0.0)
            continue
        out.append(bars[i].close / prev_close - 1.0)
    return out


def _slope_history(prices: list[float], steps: int = ACCELERATION_WINDOW) -> list[float]:
    """Compute the trailing `steps` EWMLR slopes by sliding the window.

    Each slope is computed on the WINDOW_MINS-length sub-window ending at the
    given offset. Requires len(prices) >= WINDOW_MINS + steps - 1.
    """
    history: list[float] = []
    needed = WINDOW_MINS
    if len(prices) < needed + steps - 1:
        raise ValueError(
            f"slope history requires {needed + steps - 1} prices, got {len(prices)}"
        )
    for offset in range(steps):
        end = len(prices) - (steps - 1 - offset)
        start = end - needed
        sub = prices[start:end]
        slope, _ = calculate_ewmlr(sub, half_life_mins=HALF_LIFE_MINS)
        history.append(slope)
    return history


@dataclass(frozen=True, slots=True)
class ApexHunterV22Signal:
    name: str = SIGNAL_NAME
    version: str = SIGNAL_VERSION

    def scan(
        self,
        symbol_bars: Mapping[str, list[MinuteBar]],
        quotes: Mapping[str, Quote],
        spy_bars: list[MinuteBar],
        *,
        rvol_lookback: int = DEFAULT_RVOL_LOOKBACK,
    ) -> tuple[RegimeSnapshot, Sequence[Candidate]]:
        regime = (
            compute_market_regime(spy_bars)
            if spy_bars
            else _empty_regime(symbol_bars)
        )

        # Determine latest timestamp across symbols for the time gate.
        latest_ts: datetime | None = None
        for bars in symbol_bars.values():
            if bars:
                ts = bars[-1].timestamp
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
        if latest_ts is None:
            return regime, []

        in_window = _within_scan_window(latest_ts)

        # Pre-compute SPY EWMLR slope and SPY return series for relative alpha
        # and correlation filters. If SPY history is too short, treat alpha as
        # neutral (slope = 0) so per-symbol alpha collapses to inf/-inf and
        # correlation filter blocks via insufficient data → caught below.
        spy_slope: float = 0.0
        spy_returns: list[float] = []
        if len(spy_bars) >= WINDOW_MINS:
            try:
                spy_slope, _ = calculate_ewmlr(
                    [b.close for b in spy_bars[-WINDOW_MINS:]],
                    half_life_mins=HALF_LIFE_MINS,
                )
            except ValueError:
                spy_slope = 0.0
            spy_returns = _bar_returns(spy_bars)

        # First pass: evaluate per-symbol features and apply order-sensitive
        # filters down through the alpha + correlation gates. Defer the
        # top-1%-of-universe filter until we have a full survivor list.
        scratch: list[tuple[Candidate, dict[str, float]]] = []

        for symbol, bars in symbol_bars.items():
            if not bars:
                continue
            sector = ""
            evaluated = self._evaluate_symbol(
                symbol=symbol,
                bars=bars,
                sector=sector,
                in_window=in_window,
                spy_slope=spy_slope,
                spy_returns=spy_returns,
            )
            if evaluated is None:
                continue
            candidate, features = evaluated
            scratch.append((candidate, features))

        # Top-1%-of-universe filter on `trend_quality_score`.
        survivors = [
            (c, f) for c, f in scratch if c.allowed
        ]
        if survivors:
            scores = sorted(
                (float(f.get("trend_quality_score", 0.0)) for _, f in scratch),
                reverse=True,
            )
            cutoff_index = max(0, int(len(scores) * TOP_PERCENTILE) - 1)
            # Always include at least the top scorer.
            cutoff = scores[cutoff_index] if scores else 0.0
            promoted: list[Candidate] = []
            for candidate, features in scratch:
                if not candidate.allowed:
                    promoted.append(candidate)
                    continue
                tqs = float(features.get("trend_quality_score", 0.0))
                if tqs >= cutoff:
                    promoted.append(candidate)
                else:
                    promoted.append(
                        _blocked(
                            candidate.symbol,
                            candidate.sector,
                            BlockedReason.NOT_TOP_1PCT,
                            dict(features),
                        )
                    )
            scratch_candidates = promoted
        else:
            scratch_candidates = [c for c, _ in scratch]

        allowed = [c for c in scratch_candidates if c.allowed]
        blocked = [c for c in scratch_candidates if not c.allowed]
        allowed.sort(key=lambda c: c.score, reverse=True)
        return regime, [*allowed, *blocked]

    def evaluate_exit(self, position: Any, latest_bar: MinuteBar, settings: Any) -> ExitDecision:
        return _evaluate_exit(position, latest_bar, settings)

    def _evaluate_symbol(
        self,
        *,
        symbol: str,
        bars: list[MinuteBar],
        sector: str,
        in_window: bool,
        spy_slope: float,
        spy_returns: list[float],
    ) -> tuple[Candidate, dict[str, float]] | None:
        if not in_window:
            return _blocked(symbol, sector, BlockedReason.OUTSIDE_SCAN_WINDOW), {}

        # EWMLR warm-up: need WINDOW_MINS price points + acceleration history
        # + correlation history. If insufficient, drop silently (don't crash).
        needed_prices = WINDOW_MINS + ACCELERATION_WINDOW - 1
        needed_bars_for_corr = CORRELATION_WINDOW + 1
        if len(bars) < max(needed_prices, needed_bars_for_corr, ATR_PERIOD + 1):
            return None

        prices = [b.close for b in bars]
        try:
            weighted_slope, weighted_r2 = calculate_ewmlr(
                prices[-WINDOW_MINS:], half_life_mins=HALF_LIFE_MINS
            )
        except ValueError:
            return None

        try:
            slope_history = _slope_history(prices, steps=ACCELERATION_WINDOW)
            acceleration = calculate_acceleration(
                slope_history, window=ACCELERATION_WINDOW
            )
        except ValueError:
            return None

        alpha = relative_alpha(weighted_slope, spy_slope)

        ticker_returns = _bar_returns(bars)
        correlation = 0.0
        if len(spy_returns) >= CORRELATION_WINDOW and len(ticker_returns) >= CORRELATION_WINDOW:
            try:
                correlation = correlation_to_spy(
                    ticker_returns, spy_returns, window=CORRELATION_WINDOW
                )
            except ValueError:
                correlation = 0.0

        try:
            atr_value = atr(bars, period=ATR_PERIOD)
        except ValueError:
            return None

        trend_quality_score = weighted_slope * weighted_r2

        features: dict[str, float] = {
            "weighted_slope": weighted_slope,
            "weighted_r2": weighted_r2,
            "acceleration": acceleration,
            "relative_alpha": alpha if alpha not in (float("inf"), float("-inf")) else 0.0,
            "correlation": correlation,
            "trend_quality_score": trend_quality_score,
            "atr": atr_value,
            "sector": sector,  # type: ignore[dict-item]
        }

        # Filter chain (order from spec).
        if weighted_r2 < DEFAULT_R2_THRESHOLD:
            return _blocked(symbol, sector, BlockedReason.R2_TOO_LOW, features), features
        if not (weighted_slope > 0 and acceleration >= 0):
            return (
                _blocked(
                    symbol, sector, BlockedReason.SLOPE_NEGATIVE_OR_DECELERATING, features
                ),
                features,
            )
        # For relative_alpha: spy_slope == 0 yields +inf (good) or -inf (bad).
        # Accept +inf as "infinitely strong alpha"; treat -inf as fail.
        if alpha < RELATIVE_ALPHA_MIN:
            return _blocked(symbol, sector, BlockedReason.ALPHA_TOO_LOW, features), features
        if correlation < DEFAULT_CORR_MIN:
            return (
                _blocked(symbol, sector, BlockedReason.CORRELATION_TOO_LOW, features),
                features,
            )

        return (
            Candidate(
                symbol=symbol,
                score=trend_quality_score,
                sector=sector,
                allowed=True,
                blocked_reason=None,
                features=features,
            ),
            features,
        )


def _blocked(
    symbol: str,
    sector: str,
    reason: BlockedReason,
    features: dict[str, float] | None = None,
) -> Candidate:
    return Candidate(
        symbol=symbol,
        score=0.0,
        sector=sector,
        allowed=False,
        blocked_reason=reason,
        features=features or {},
    )


def _empty_regime(symbol_bars: Mapping[str, list[MinuteBar]]) -> RegimeSnapshot:
    for bars in symbol_bars.values():
        if bars:
            return compute_market_regime(bars)
    raise ValueError("scan requires either spy_bars or non-empty symbol_bars")


__all__ = ["ApexHunterV22Signal"]
