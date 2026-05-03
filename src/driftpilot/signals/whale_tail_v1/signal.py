"""Whale-Tail v1 - SignalProtocol implementation.

Liquidity absorption: high relative volume traded within a compressed price
range, with price near the upper end of the compression box. Distribution-trap
invalidation rejects setups where price has recently broken below the
compression floor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from driftpilot.clock import require_aware
from driftpilot.signals.base import Candidate, ExitDecision
from driftpilot.signals.features import DEFAULT_RVOL_LOOKBACK, MinuteBar, Quote
from driftpilot.signals.regime import RegimeSnapshot, compute_market_regime
from driftpilot.signals.whale_tail_v1.config import (
    ATR_PERIOD,
    COMPRESSION_LOOKBACK_BARS,
    COMPRESSION_THRESHOLD,
    RANGE_POSITION_THRESHOLD,
    RVOL_THRESHOLD,
    RVOL_WINDOW_MINUTES,
    SCAN_END_TIME_ET,
    SCAN_START_TIME_ET,
    SIGNAL_NAME,
    SIGNAL_VERSION,
    STOP_ATR_MULT,
    TARGET_ATR_MULT,
    TIME_STOP_MINUTES,
)
from driftpilot.signals.whale_tail_v1.exits import evaluate_exit as _evaluate_exit_impl
from driftpilot.signals.whale_tail_v1.features import (
    atr,
    compression_high,
    compression_low,
    compression_midpoint,
    compression_score,
    range_position_pct,
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
    if not bars:
        return []
    last_date = bars[-1].timestamp.astimezone(_ET).date()
    return [b for b in bars if b.timestamp.astimezone(_ET).date() == last_date]


@dataclass(frozen=True, slots=True)
class WhaleTailV1Signal:
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
            compute_market_regime(spy_bars) if spy_bars else _empty_regime(symbol_bars)
        )
        candidates: list[Candidate] = []

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

        # Rank survivors by RVOL * (1 / compression_score) descending.
        allowed = [c for c in candidates if c.allowed]
        blocked = [c for c in candidates if not c.allowed]
        allowed.sort(key=lambda c: float(c.score), reverse=True)
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

        # Time gate is the first hard gate per spec.
        if not in_window:
            return _blocked(symbol, sector, BlockedReason.OUTSIDE_SCAN_WINDOW)

        # Need enough bars for ATR(period) and the RVOL/compression lookbacks.
        atr_required = ATR_PERIOD + 1
        rvol_required = RVOL_WINDOW_MINUTES + 1
        compression_required = COMPRESSION_LOOKBACK_BARS
        min_required = max(atr_required, rvol_required, compression_required)
        if len(bars) < min_required:
            return None

        # Universe filters (per-bar): price corridor.
        from driftpilot.signals.whale_tail_v1.config import PRICE_MAX, PRICE_MIN

        if price < PRICE_MIN or price > PRICE_MAX:
            return _blocked(symbol, sector, BlockedReason.OUTSIDE_PRICE_CORRIDOR)

        # ADV check requires daily history we don't carry here; skip per spec.

        # ATR
        atr_value = atr(bars, period=ATR_PERIOD)

        # Compression metrics over the last COMPRESSION_LOOKBACK_BARS bars.
        comp_high = compression_high(bars, window=COMPRESSION_LOOKBACK_BARS)
        comp_low = compression_low(bars, window=COMPRESSION_LOOKBACK_BARS)
        comp_mid = compression_midpoint(bars, window=COMPRESSION_LOOKBACK_BARS)
        comp_score = compression_score(
            bars, window=COMPRESSION_LOOKBACK_BARS, atr_value=atr_value
        )

        # RVOL — current bar EXCLUDED; lookback is the RVOL_WINDOW_MINUTES bars
        # strictly preceding the latest bar.
        lookback_for_rvol = bars[:-1]
        if len(lookback_for_rvol) < RVOL_WINDOW_MINUTES:
            return None
        rvol = relative_volume(
            latest, lookback_for_rvol, lookback_n=RVOL_WINDOW_MINUTES
        )

        # Range position within compression window.
        if comp_high > comp_low:
            range_pos = range_position_pct(price, comp_high, comp_low)
        else:
            range_pos = 0.0

        features: dict[str, Any] = {
            "rvol": rvol,
            "compression_score": comp_score,
            "range_position": range_pos,
            "atr": atr_value,
            "compression_high": comp_high,
            "compression_midpoint": comp_mid,
            "compression_low": comp_low,
            "sector": sector,
        }

        # Filter chain in spec order.
        if rvol <= RVOL_THRESHOLD:
            return _blocked(symbol, sector, BlockedReason.RVOL_TOO_LOW, features)
        if comp_score >= COMPRESSION_THRESHOLD:
            return _blocked(symbol, sector, BlockedReason.NOT_COMPRESSED, features)
        if range_pos <= RANGE_POSITION_THRESHOLD:
            return _blocked(
                symbol, sector, BlockedReason.NOT_IN_UPPER_RANGE, features
            )

        # Distribution-trap invalidation: any close in the last 5 min broke the
        # ESTABLISHED compression box.  comp_low above is min(lows of last 15
        # bars) — that calc is circular for break detection (a breaking bar
        # pulls comp_low down with it). The "established" baseline is the min
        # low of the 10 bars preceding the recent 5 (i.e. the compression box
        # as it existed before any potential breakdown attempt).
        if len(bars) >= COMPRESSION_LOOKBACK_BARS:
            established_low = min(
                b.low for b in bars[-COMPRESSION_LOOKBACK_BARS:-5]
            )
            recent_5 = bars[-5:]
            if any(b.close < established_low for b in recent_5):
                return _blocked(
                    symbol,
                    sector,
                    BlockedReason.DISTRIBUTION_BREAK_INVALIDATED,
                    features,
                )

        # Score = RVOL * (1 / compression_score). Compression > 0 here.
        score = rvol * (1.0 / comp_score) if comp_score > 0 else 0.0
        return Candidate(
            symbol=symbol,
            score=score,
            sector=sector,
            allowed=True,
            blocked_reason=None,
            features=features,
        )

    def evaluate_exit(
        self,
        position: Any,
        latest_bar: MinuteBar,
        settings: Any,
    ) -> ExitDecision:
        return _evaluate_exit_impl(
            position,
            latest_bar,
            settings,
            target_atr_mult=TARGET_ATR_MULT,
            stop_atr_mult=STOP_ATR_MULT,
            time_stop_minutes=TIME_STOP_MINUTES,
        )


def _blocked(
    symbol: str,
    sector: str,
    reason: BlockedReason,
    features: Mapping[str, Any] | None = None,
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


__all__ = ["WhaleTailV1Signal"]
