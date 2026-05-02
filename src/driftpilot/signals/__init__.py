"""Shared signal registry for live scanning and backtest replay."""

from __future__ import annotations

from collections.abc import Callable

from driftpilot.signals.base import SignalProtocol
from driftpilot.signals.features import BarFeatureCache, MinuteBar, Quote, SignalFeatures
from driftpilot.signals.intraday_momentum_v1 import (
    SIGNAL_NAME as INTRADAY_MOMENTUM_V1_NAME,
    CandidateDecision,
    IntradayMomentumV1Signal,
    build_intraday_momentum_queue,
)
from driftpilot.signals.regime import Regime, RegimeSnapshot, compute_market_regime
from driftpilot.signals.scoring import ScoredCandidate, score_candidates


DEFAULT_SIGNAL = INTRADAY_MOMENTUM_V1_NAME
SignalFactory = Callable[[], SignalProtocol]
_SIGNAL_REGISTRY: dict[str, SignalFactory] = {}


def register_signal(name: str, factory: SignalFactory) -> None:
    normalized = _normalize_signal_name(name)
    if not normalized:
        raise ValueError("signal name must not be empty")
    _SIGNAL_REGISTRY[normalized] = factory


def get_signal(name: str | None = None) -> SignalProtocol:
    normalized = _normalize_signal_name(name or DEFAULT_SIGNAL)
    try:
        return _SIGNAL_REGISTRY[normalized]()
    except KeyError as exc:
        available = ", ".join(list_signals())
        raise ValueError(f"unknown signal '{normalized}'. Available signals: {available}") from exc


def list_signals() -> list[str]:
    return sorted(_SIGNAL_REGISTRY)


def _normalize_signal_name(name: str) -> str:
    return name.strip().lower().replace("-", "_")


register_signal(DEFAULT_SIGNAL, lambda: IntradayMomentumV1Signal())

__all__ = [
    "BarFeatureCache",
    "CandidateDecision",
    "DEFAULT_SIGNAL",
    "MinuteBar",
    "Quote",
    "Regime",
    "RegimeSnapshot",
    "ScoredCandidate",
    "SignalFeatures",
    "SignalProtocol",
    "build_intraday_momentum_queue",
    "compute_market_regime",
    "get_signal",
    "list_signals",
    "register_signal",
    "score_candidates",
]
