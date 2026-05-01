from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import fmean
from typing import Iterable

from driftpilot.signals.features import SignalFeatures


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    symbol: str
    features: SignalFeatures
    score: float
    rvol_zscore: float
    return_15m_zscore: float
    distance_above_vwap_zscore: float


def zscores(values: list[float]) -> list[float]:
    if not values:
        return []
    mean = fmean(values)
    variance = fmean((value - mean) ** 2 for value in values)
    standard_deviation = sqrt(variance)
    if standard_deviation == 0:
        return [0.0 for _ in values]
    return [(value - mean) / standard_deviation for value in values]


def score_candidates(features: Iterable[SignalFeatures]) -> list[ScoredCandidate]:
    pool = list(features)
    rvol_scores = zscores([item.rvol for item in pool])
    return_scores = zscores([item.return_15m for item in pool])
    vwap_distance_scores = zscores([item.distance_above_vwap_pct for item in pool])

    scored = [
        ScoredCandidate(
            symbol=item.symbol,
            features=item,
            score=0.4 * rvol_zscore + 0.3 * return_zscore + 0.3 * vwap_distance_zscore,
            rvol_zscore=rvol_zscore,
            return_15m_zscore=return_zscore,
            distance_above_vwap_zscore=vwap_distance_zscore,
        )
        for item, rvol_zscore, return_zscore, vwap_distance_zscore in zip(
            pool,
            rvol_scores,
            return_scores,
            vwap_distance_scores,
            strict=True,
        )
    ]
    return sorted(scored, key=lambda item: (-item.score, item.symbol))
