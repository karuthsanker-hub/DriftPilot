from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from driftpilot.signals.features import (
    DEFAULT_RVOL_LOOKBACK,
    MinuteBar,
    Quote,
    SignalFeatures,
    compute_signal_features,
)
from driftpilot.signals.regime import Regime, RegimeSnapshot, compute_market_regime
from driftpilot.signals.scoring import ScoredCandidate, score_candidates


MIN_RVOL = 2.0
MIN_RETURN_15M = 0.005
CAUTION_RELATIVE_STRENGTH_MIN = 0.005
RED_RELATIVE_STRENGTH_MIN = 0.010


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    symbol: str
    allowed: bool
    reasons: tuple[str, ...]
    features: SignalFeatures
    relative_strength: float
    scored_candidate: ScoredCandidate | None = None


def regime_allows_entry(features: SignalFeatures, regime: RegimeSnapshot) -> tuple[bool, str, float]:
    relative_strength = features.return_15m - regime.benchmark_return_15m
    if regime.regime == Regime.GREEN:
        return True, "GREEN regime allows valid entries", relative_strength
    if regime.regime == Regime.CAUTION:
        allowed = relative_strength > CAUTION_RELATIVE_STRENGTH_MIN
        return allowed, "CAUTION requires relative_strength > 0.5%", relative_strength

    allowed = relative_strength > RED_RELATIVE_STRENGTH_MIN and features.return_15m > 0
    return allowed, "RED requires relative_strength > 1.0% and positive 15m return", relative_strength


def entry_filter(features: SignalFeatures, regime: RegimeSnapshot) -> CandidateDecision:
    reasons: list[str] = []

    if not features.has_rvol_history:
        reasons.append("missing RVOL history")
    elif features.rvol < MIN_RVOL:
        reasons.append("RVOL below 2.0")

    if not features.above_vwap:
        reasons.append("price not above session VWAP")

    if not features.has_15m_history:
        reasons.append("missing 15m history")
    elif features.return_15m < MIN_RETURN_15M:
        reasons.append("15m return below 0.5%")

    if features.spread is None:
        reasons.append("missing spread")
    elif not features.spread_ok:
        reasons.append("spread exceeds max(0.02, 0.001 * price)")

    regime_allowed, regime_reason, relative_strength = regime_allows_entry(features, regime)
    if not regime_allowed:
        reasons.append(regime_reason)

    return CandidateDecision(
        symbol=features.symbol,
        allowed=not reasons,
        reasons=tuple(reasons),
        features=features,
        relative_strength=relative_strength,
    )


def build_intraday_momentum_queue(
    candidate_features: list[SignalFeatures],
    regime: RegimeSnapshot,
) -> list[CandidateDecision]:
    decisions = [entry_filter(features, regime) for features in candidate_features]
    passing_by_symbol = {decision.symbol: decision for decision in decisions if decision.allowed}
    scored = score_candidates(decision.features for decision in passing_by_symbol.values())

    return [
        CandidateDecision(
            symbol=passing_by_symbol[item.symbol].symbol,
            allowed=True,
            reasons=(),
            features=passing_by_symbol[item.symbol].features,
            relative_strength=passing_by_symbol[item.symbol].relative_strength,
            scored_candidate=item,
        )
        for item in scored
    ]


def scan_intraday_momentum(
    symbol_bars: Mapping[str, list[MinuteBar]],
    quotes: Mapping[str, Quote],
    spy_bars: list[MinuteBar],
    *,
    rvol_lookback: int = DEFAULT_RVOL_LOOKBACK,
) -> tuple[RegimeSnapshot, list[CandidateDecision]]:
    regime = compute_market_regime(spy_bars)
    features = [
        compute_signal_features(
            bars,
            quote=quotes.get(symbol.upper()),
            rvol_lookback=rvol_lookback,
        )
        for symbol, bars in symbol_bars.items()
    ]
    return regime, build_intraday_momentum_queue(features, regime)
