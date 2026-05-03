"""Stationary Ghost v1 — SignalProtocol implementation.

Mean-reversion entry: 2.5σ below 15-bar mean, ADX trend filter, low-volume
pullback, green-on-day. Standard target/stop/time exits (no custom
evaluate_exit — harness defaults apply).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from statistics import pstdev
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from driftpilot.clock import require_aware
from driftpilot.signals.base import Candidate
from driftpilot.signals.features import DEFAULT_RVOL_LOOKBACK, MinuteBar, Quote
from driftpilot.signals.regime import RegimeSnapshot, compute_market_regime
from driftpilot.signals.stationary_ghost_v1.config import (
    ADX_MAX_THRESHOLD,
    ADX_PERIOD,
    ENTRY_Z_SCORE_THRESHOLD,
    LOOKBACK_BARS,
    PULLBACK_VOLUME_RATIO_MAX,
    REQUIRE_GREEN_ON_DAY,
    SCAN_END_TIME_ET,
    SCAN_START_TIME_ET,
    SIGNAL_NAME,
    SIGNAL_VERSION,
)
from driftpilot.signals.stationary_ghost_v1.features import (
    adx,
    bollinger_bands,
    distance_z_score,
    relative_volume,
)
from driftpilot.states import BlockedReason


_ET = ZoneInfo("America/New_York")


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


_SCAN_START = _parse_hhmm(SCAN_START_TIME_ET)
_SCAN_END = _parse_hhmm(SCAN_END_TIME_ET)


def _within_scan_window(latest_ts: datetime) -> bool:
    et = require_aware(latest_ts).astimezone(_ET).time()
    return _SCAN_START <= et <= _SCAN_END


def _session_bars(bars: list[MinuteBar]) -> list[MinuteBar]:
    """Return only bars whose ET date matches the latest bar's ET date."""
    if not bars:
        return []
    last_date = bars[-1].timestamp.astimezone(_ET).date()
    return [b for b in bars if b.timestamp.astimezone(_ET).date() == last_date]


def _day_return_pct(session_bars: list[MinuteBar]) -> float:
    """Cumulative return from session open to latest close."""
    if not session_bars:
        return 0.0
    open_price = session_bars[0].open
    last_close = session_bars[-1].close
    if open_price <= 0:
        return 0.0
    return (last_close - open_price) / open_price


@dataclass(frozen=True, slots=True)
class StationaryGhostV1Signal:
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
        regime = compute_market_regime(spy_bars) if spy_bars else _empty_regime(symbol_bars)
        candidates: list[Candidate] = []

        # Determine the latest timestamp seen across symbols to gate scanning.
        latest_ts: datetime | None = None
        for bars in symbol_bars.values():
            if bars:
                ts = bars[-1].timestamp
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts

        if latest_ts is None:
            return regime, []

        in_window = _within_scan_window(latest_ts)

        for symbol, bars in symbol_bars.items():
            if not bars:
                continue
            sector = ""
            quote = quotes.get(symbol.upper())
            candidate = self._evaluate_symbol(
                symbol=symbol,
                bars=bars,
                quote=quote,
                sector=sector,
                in_window=in_window,
            )
            if candidate is not None:
                candidates.append(candidate)

        # Rank survivors (allowed=True) by |z_score| descending. Blocked
        # candidates retain their natural order.
        allowed = [c for c in candidates if c.allowed]
        blocked = [c for c in candidates if not c.allowed]
        allowed.sort(
            key=lambda c: abs(float(c.features.get("z_score", 0.0))),
            reverse=True,
        )
        return regime, [*allowed, *blocked]

    def _evaluate_symbol(
        self,
        *,
        symbol: str,
        bars: list[MinuteBar],
        quote: Quote | None,
        sector: str,
        in_window: bool,
    ) -> Candidate | None:
        latest = bars[-1]
        price = latest.close

        if not in_window:
            return _blocked(symbol, sector, BlockedReason.OUTSIDE_SCAN_WINDOW)

        # Need enough bars for ADX + Bollinger + rvol lookback.
        # ADX requires 2*period+1 bars; Bollinger requires LOOKBACK_BARS;
        # rvol requires LOOKBACK_BARS preceding bars.
        adx_required = 2 * ADX_PERIOD + 1
        if len(bars) < max(adx_required, LOOKBACK_BARS + 1):
            return None

        # Bollinger / Z-score over the LOOKBACK_BARS most recent bars.
        upper, middle, lower = bollinger_bands(
            bars, period=LOOKBACK_BARS, std_dev=2.0
        )
        # std comes from the same window — recompute for z-score.
        window_closes = [b.close for b in bars[-LOOKBACK_BARS:]]
        std = pstdev(window_closes)
        if std <= 0:
            return None
        z_score = distance_z_score(price, middle, std)

        # ADX
        adx_value = adx(bars, period=ADX_PERIOD)

        # Relative volume — current bar EXCLUDED from average.
        # Use the LOOKBACK_BARS bars STRICTLY BEFORE the latest bar.
        lookback_for_rvol = bars[:-1]
        if len(lookback_for_rvol) < LOOKBACK_BARS:
            return None
        rvol = relative_volume(latest, lookback_for_rvol, lookback_n=LOOKBACK_BARS)

        # Session day return
        day_return = _day_return_pct(_session_bars(bars))

        features: dict[str, float] = {
            "z_score": z_score,
            "mean_15bar": middle,
            "std_15bar": std,
            "adx": adx_value,
            "relative_volume": rvol,
            "day_return_pct": day_return,
            "lower_band": lower,
            "upper_band": upper,
            "price": price,
        }

        # Filter chain (order matters for the surfaced reason).
        if adx_value >= ADX_MAX_THRESHOLD:
            return _blocked(symbol, sector, BlockedReason.ADX_TOO_HIGH, features)
        if z_score > ENTRY_Z_SCORE_THRESHOLD:
            return _blocked(symbol, sector, BlockedReason.NOT_EXTENDED_ENOUGH, features)
        if rvol >= PULLBACK_VOLUME_RATIO_MAX:
            return _blocked(
                symbol, sector, BlockedReason.PULLBACK_VOLUME_TOO_HIGH, features
            )
        if REQUIRE_GREEN_ON_DAY and day_return <= 0:
            return _blocked(symbol, sector, BlockedReason.STOCK_RED_ON_DAY, features)

        # Score = magnitude of z-score (more extended = higher rank).
        score = abs(z_score)
        return Candidate(
            symbol=symbol,
            score=score,
            sector=sector,
            allowed=True,
            blocked_reason=None,
            features=features,
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
    """Fallback when no SPY bars are provided in tests; constructs a
    minimal RED regime so callers don't crash."""
    # Use any symbol's bars to seed a stub. If none, raise.
    for bars in symbol_bars.values():
        if bars:
            return compute_market_regime(bars)
    raise ValueError("scan requires either spy_bars or non-empty symbol_bars")


__all__ = ["StationaryGhostV1Signal"]
