"""RS-Drift v1.1 — relative-strength-vs-SPY drift signal.

Per locked spec strategy_rs_drift_v1.md:
  - Universe: ADV >= 2M shares, $15 <= price <= $400.
  - Entry window: 10:00–10:30 ET only.
  - Filter chain: RS > 1.25%, price > post-10:00 VWAP, price > Opening Range High.
  - Custom exit logic (break-even trigger, EOD time stop, SPY-heat tightening,
    daily circuit breakers) — see exits.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from driftpilot.clock import require_aware
from driftpilot.signals.base import Candidate, ExitDecision
from driftpilot.signals.features import (
    DEFAULT_RVOL_LOOKBACK,
    MinuteBar,
    Quote,
)
from driftpilot.signals.regime import RegimeSnapshot, compute_market_regime
from driftpilot.signals.rs_drift_v1.config import (
    PRICE_MAX,
    PRICE_MIN,
    RS_THRESHOLD_PCT,
    SCAN_END_TIME_ET,
    SCAN_START_TIME_ET,
    SIGNAL_NAME,
    SIGNAL_VERSION,
)
from driftpilot.signals.rs_drift_v1.exits import evaluate_exit as _evaluate_exit
from driftpilot.signals.rs_drift_v1.features import (
    opening_range_high,
    post_open_vwap,
    rs_score,
)
from driftpilot.states import BlockedReason


ET = ZoneInfo("America/New_York")


def _to_et(ts: Any) -> Any:
    return require_aware(ts).astimezone(ET)


def _parse_hhmm(s: str) -> time:
    hours, minutes = s.split(":")
    return time(int(hours), int(minutes))


_SCAN_START = _parse_hhmm(SCAN_START_TIME_ET)
_SCAN_END = _parse_hhmm(SCAN_END_TIME_ET)


def _within_scan_window(bar: MinuteBar) -> bool:
    et_t = _to_et(bar.timestamp).time()
    return _SCAN_START <= et_t < _SCAN_END


def _latest_close(bars: list[MinuteBar]) -> tuple[MinuteBar, float]:
    sorted_bars = sorted(bars, key=lambda b: b.timestamp)
    latest = sorted_bars[-1]
    return latest, latest.close


def _blocked(symbol: str, reason: BlockedReason, sector: str = "", **features) -> Candidate:
    return Candidate(
        symbol=symbol,
        score=0.0,
        sector=sector,
        allowed=False,
        blocked_reason=reason,
        features=features,
    )


class RsDriftV1Signal:
    """SignalProtocol implementation for RS-Drift v1.1."""

    name: str = SIGNAL_NAME
    version: str = SIGNAL_VERSION

    def scan(
        self,
        symbol_bars: Mapping[str, list[MinuteBar]],
        quotes: Mapping[str, Quote],
        spy_bars: list[MinuteBar],
        *,
        rvol_lookback: int = DEFAULT_RVOL_LOOKBACK,
    ) -> tuple[RegimeSnapshot, list[Candidate]]:
        regime = compute_market_regime(spy_bars)
        candidates: list[Candidate] = []

        for symbol, bars in symbol_bars.items():
            normalized_symbol = symbol.upper()
            if not bars:
                continue
            latest_bar, latest_close = _latest_close(bars)

            # Time gate
            if not _within_scan_window(latest_bar):
                candidates.append(_blocked(normalized_symbol, BlockedReason.OUTSIDE_SCAN_WINDOW))
                continue

            # Universe price filter
            if latest_close < PRICE_MIN or latest_close > PRICE_MAX:
                candidates.append(
                    _blocked(
                        normalized_symbol,
                        BlockedReason.OUTSIDE_PRICE_CORRIDOR,
                        price=latest_close,
                    )
                )
                continue

            session_date = _to_et(latest_bar.timestamp).date()

            # RS score (vs SPY) over 09:30–10:00 ET
            try:
                rs = rs_score(bars, spy_bars)
            except ValueError:
                # Insufficient session-window history → treat as blocked
                candidates.append(
                    _blocked(normalized_symbol, BlockedReason.OUTSIDE_SCAN_WINDOW)
                )
                continue
            if rs < RS_THRESHOLD_PCT:
                candidates.append(
                    _blocked(
                        normalized_symbol,
                        BlockedReason.RS_BELOW_THRESHOLD,
                        rs_score=rs,
                    )
                )
                continue

            # Above post-10:00 VWAP
            try:
                vwap = post_open_vwap(bars)
            except ValueError:
                candidates.append(
                    _blocked(normalized_symbol, BlockedReason.OUTSIDE_SCAN_WINDOW)
                )
                continue
            if latest_close <= vwap:
                candidates.append(
                    _blocked(
                        normalized_symbol,
                        BlockedReason.BELOW_POST_OPEN_VWAP,
                        rs_score=rs,
                        post_open_vwap=vwap,
                        price=latest_close,
                    )
                )
                continue

            # Above Opening Range High
            try:
                orh = opening_range_high(bars, session_date)
            except ValueError:
                candidates.append(
                    _blocked(normalized_symbol, BlockedReason.OUTSIDE_SCAN_WINDOW)
                )
                continue
            if latest_close <= orh:
                candidates.append(
                    _blocked(
                        normalized_symbol,
                        BlockedReason.BELOW_OPENING_RANGE_HIGH,
                        rs_score=rs,
                        orh=orh,
                        post_open_vwap=vwap,
                        price=latest_close,
                    )
                )
                continue

            # All filters passed → emit allowed candidate, ranked by RS score
            candidates.append(
                Candidate(
                    symbol=normalized_symbol,
                    score=rs,
                    sector="",
                    allowed=True,
                    blocked_reason=None,
                    features={
                        "rs_score": rs,
                        "orh": orh,
                        "post_open_vwap": vwap,
                        "price": latest_close,
                        "sector": "",
                    },
                )
            )

        # Sort allowed candidates by RS score descending; blocked candidates
        # remain visible (matching intraday_momentum_v1 convention).
        allowed = [c for c in candidates if c.allowed]
        blocked = [c for c in candidates if not c.allowed]
        allowed.sort(key=lambda c: (-c.score, c.symbol))

        return regime, allowed + blocked

    def evaluate_exit(self, position: Any, latest_bar: MinuteBar, settings: Any) -> ExitDecision:
        return _evaluate_exit(position, latest_bar, settings)


__all__ = ["RsDriftV1Signal"]
