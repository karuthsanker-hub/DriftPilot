from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import date as datetime_date
from statistics import fmean

from driftpilot.clock import require_aware


DEFAULT_RVOL_LOOKBACK = 20


@dataclass(frozen=True, slots=True)
class MinuteBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        require_aware(self.timestamp)
        if self.open <= 0 or self.high <= 0 or self.low <= 0 or self.close <= 0:
            raise ValueError("bar prices must be positive")
        if self.volume < 0:
            raise ValueError("bar volume must be non-negative")
        if self.high < self.low:
            raise ValueError("bar high must be greater than or equal to low")


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float

    def __post_init__(self) -> None:
        require_aware(self.timestamp)
        if self.bid <= 0 or self.ask <= 0:
            raise ValueError("quote prices must be positive")
        if self.ask < self.bid:
            raise ValueError("quote ask must be greater than or equal to bid")

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class SignalFeatures:
    symbol: str
    timestamp: datetime
    price: float
    session_vwap: float
    rvol: float
    return_15m: float
    spread: float | None
    spread_limit: float
    distance_above_vwap_pct: float
    has_15m_history: bool
    has_rvol_history: bool

    @property
    def above_vwap(self) -> bool:
        return self.price > self.session_vwap

    @property
    def spread_ok(self) -> bool:
        return self.spread is not None and self.spread <= self.spread_limit


class BarFeatureCache:
    """In-memory 1-minute bar and quote cache shared by scanner and replay."""

    def __init__(self) -> None:
        self._bars: dict[str, list[MinuteBar]] = defaultdict(list)
        self._quotes: dict[str, Quote] = {}

    def add_bar(self, bar: MinuteBar) -> None:
        symbol = bar.symbol.upper()
        self._bars[symbol].append(bar)
        self._bars[symbol].sort(key=lambda item: item.timestamp)

    def add_quote(self, quote: Quote) -> None:
        self._quotes[quote.symbol.upper()] = quote

    def bars(self, symbol: str) -> list[MinuteBar]:
        return list(self._bars.get(symbol.upper(), []))

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(symbol.upper())

    def features(self, symbol: str, *, rvol_lookback: int = DEFAULT_RVOL_LOOKBACK) -> SignalFeatures:
        return compute_signal_features(self.bars(symbol), quote=self.quote(symbol), rvol_lookback=rvol_lookback)


def compute_session_vwap(bars: list[MinuteBar]) -> float:
    if not bars:
        raise ValueError("at least one bar is required")
    total_volume = sum(bar.volume for bar in bars)
    if total_volume <= 0:
        raise ValueError("session volume must be positive")
    return sum(typical_price(bar) * bar.volume for bar in bars) / total_volume


def typical_price(bar: MinuteBar) -> float:
    return (bar.high + bar.low + bar.close) / 3.0


def latest_session_bars(bars: list[MinuteBar]) -> list[MinuteBar]:
    ordered = sorted(bars, key=lambda item: item.timestamp)
    if not ordered:
        raise ValueError("at least one bar is required")
    latest_date = ordered[-1].timestamp.date()
    return [bar for bar in ordered if bar.timestamp.date() == latest_date]


def return_over_minutes(bars: list[MinuteBar], minutes: int) -> tuple[float, bool]:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    ordered = sorted(bars, key=lambda item: item.timestamp)
    if not ordered:
        raise ValueError("at least one bar is required")
    latest = ordered[-1]
    target_timestamp = latest.timestamp - timedelta(minutes=minutes)
    anchor = next((bar for bar in reversed(ordered) if bar.timestamp <= target_timestamp), None)
    if anchor is None:
        return 0.0, False
    return latest.close / anchor.close - 1.0, True


def compute_rvol(bars: list[MinuteBar], *, lookback: int = DEFAULT_RVOL_LOOKBACK) -> tuple[float, bool]:
    if lookback <= 0:
        raise ValueError("lookback must be positive")
    ordered = sorted(bars, key=lambda item: item.timestamp)
    if len(ordered) < 2:
        return 0.0, False
    latest = ordered[-1]
    current_minute = (latest.timestamp.hour, latest.timestamp.minute)
    prior_bars_by_date: dict[datetime_date, MinuteBar] = {}
    for bar in ordered[:-1]:
        if bar.timestamp.date() >= latest.timestamp.date():
            continue
        if (bar.timestamp.hour, bar.timestamp.minute) == current_minute:
            prior_bars_by_date[bar.timestamp.date()] = bar

    prior_dates = sorted(prior_bars_by_date)[-lookback:]
    if len(prior_dates) < lookback:
        return 0.0, False
    prior_bars = [prior_bars_by_date[prior_date] for prior_date in prior_dates]
    average_volume = fmean(bar.volume for bar in prior_bars)
    if average_volume <= 0:
        return 0.0, False
    return ordered[-1].volume / average_volume, True


def spread_limit_for_price(price: float) -> float:
    if price <= 0:
        raise ValueError("price must be positive")
    return max(0.02, 0.001 * price)


def compute_signal_features(
    bars: list[MinuteBar],
    *,
    quote: Quote | None = None,
    rvol_lookback: int = DEFAULT_RVOL_LOOKBACK,
) -> SignalFeatures:
    ordered = sorted(bars, key=lambda item: item.timestamp)
    if not ordered:
        raise ValueError("at least one bar is required")
    symbol = ordered[-1].symbol.upper()
    latest = ordered[-1]
    if any(bar.symbol.upper() != symbol for bar in ordered):
        raise ValueError("all bars must be for the same symbol")
    if quote is not None and quote.symbol.upper() != symbol:
        raise ValueError("quote symbol must match bar symbol")

    session_bars = latest_session_bars(ordered)
    vwap = compute_session_vwap(session_bars)
    rvol, has_rvol_history = compute_rvol(ordered, lookback=rvol_lookback)
    return_15m, has_15m_history = return_over_minutes(session_bars, 15)

    return SignalFeatures(
        symbol=symbol,
        timestamp=latest.timestamp,
        price=latest.close,
        session_vwap=vwap,
        rvol=rvol,
        return_15m=return_15m,
        spread=quote.spread if quote is not None else None,
        spread_limit=spread_limit_for_price(latest.close),
        distance_above_vwap_pct=latest.close / vwap - 1.0,
        has_15m_history=has_15m_history,
        has_rvol_history=has_rvol_history,
    )
