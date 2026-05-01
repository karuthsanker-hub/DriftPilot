from __future__ import annotations

# Current Alpaca docs (verified May 1, 2026) list Trading API Algo Trader Plus
# equities WebSocket subscriptions as "Unlimited" and note most plans,
# including Algo Trader Plus, allow 1 connection per endpoint. The checked-in
# universe has 25 symbols, so discovery sharding is only activated when a lower
# max_symbols_per_connection budget is explicitly supplied.

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from driftpilot.clock import DriftPilotClock, datetime_from_storage, require_aware
from driftpilot.settings import DriftPilotSettings


ALPACA_ALGO_TRADER_PLUS_EQUITIES_SYMBOL_LIMIT: int | None = None
DISCOVERY_STREAM_STATE_NAME = "alpaca_sip_discovery"


@dataclass(frozen=True, slots=True)
class MarketBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None


@dataclass(frozen=True, slots=True)
class MarketQuote:
    symbol: str
    timestamp: datetime
    bid_price: float
    ask_price: float
    bid_size: float | None = None
    ask_size: float | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    always_on_symbols: tuple[str, ...]
    discovery_symbols: tuple[str, ...]
    active_symbols: tuple[str, ...]
    discovery_shards: tuple[tuple[str, ...], ...]
    active_discovery_shard: int | None
    universe_partially_streamed: bool


class StockStream(Protocol):
    def subscribe_bars(
        self,
        handler: Callable[[Any], Awaitable[None]],
        *symbols: str,
    ) -> None: ...

    def subscribe_quotes(
        self,
        handler: Callable[[Any], Awaitable[None]],
        *symbols: str,
    ) -> None: ...

    def run(self) -> None: ...

    def stop(self) -> None: ...


def _normalize_symbols(symbols: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        cleaned = symbol.strip().upper()
        if not cleaned or cleaned in seen:
            continue
        normalized.append(cleaned)
        seen.add(cleaned)
    return normalized


def _chunks(symbols: list[str], size: int) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(symbols[index : index + size]) for index in range(0, len(symbols), size)
    )


def plan_two_tier_subscriptions(
    *,
    universe_symbols: Iterable[str],
    open_position_symbols: Iterable[str],
    ranked_candidate_symbols: Iterable[str],
    settings: DriftPilotSettings,
    shard_cursor: int = 0,
    max_symbols_per_connection: int
    | None = ALPACA_ALGO_TRADER_PLUS_EQUITIES_SYMBOL_LIMIT,
) -> SubscriptionPlan:
    top_candidates = _normalize_symbols(ranked_candidate_symbols)[
        : settings.always_on_candidate_count
    ]
    always_on = _normalize_symbols(
        ("SPY", "QQQ", *open_position_symbols, *top_candidates)
    )
    universe = _normalize_symbols(universe_symbols)
    discovery = [symbol for symbol in universe if symbol not in set(always_on)]

    if max_symbols_per_connection is None:
        active = _normalize_symbols((*always_on, *discovery))
        return SubscriptionPlan(
            always_on_symbols=tuple(always_on),
            discovery_symbols=tuple(discovery),
            active_symbols=tuple(active),
            discovery_shards=(tuple(discovery),),
            active_discovery_shard=None,
            universe_partially_streamed=False,
        )

    if max_symbols_per_connection < 1:
        raise ValueError("max_symbols_per_connection must be positive")
    if len(always_on) > max_symbols_per_connection:
        raise ValueError(
            "always-on symbols exceed the Alpaca stream subscription budget"
        )

    discovery_budget = max_symbols_per_connection - len(always_on)
    if len(discovery) <= discovery_budget:
        active = _normalize_symbols((*always_on, *discovery))
        return SubscriptionPlan(
            always_on_symbols=tuple(always_on),
            discovery_symbols=tuple(discovery),
            active_symbols=tuple(active),
            discovery_shards=(tuple(discovery),),
            active_discovery_shard=None,
            universe_partially_streamed=False,
        )

    if discovery_budget == 0:
        shards: tuple[tuple[str, ...], ...] = tuple((symbol,) for symbol in discovery)
        active_index = shard_cursor % len(shards)
        active_discovery: tuple[str, ...] = ()
    else:
        shards = _chunks(discovery, discovery_budget)
        active_index = shard_cursor % len(shards)
        active_discovery = shards[active_index]

    active = _normalize_symbols((*always_on, *active_discovery))
    return SubscriptionPlan(
        always_on_symbols=tuple(always_on),
        discovery_symbols=tuple(discovery),
        active_symbols=tuple(active),
        discovery_shards=shards,
        active_discovery_shard=active_index,
        universe_partially_streamed=True,
    )


def plan_persisted_two_tier_subscriptions(
    *,
    repository: Any,
    universe_symbols: Iterable[str],
    open_position_symbols: Iterable[str],
    ranked_candidate_symbols: Iterable[str],
    settings: DriftPilotSettings,
    max_symbols_per_connection: int | None = ALPACA_ALGO_TRADER_PLUS_EQUITIES_SYMBOL_LIMIT,
) -> SubscriptionPlan:
    stream_state = getattr(repository, "stream_state", None)
    if stream_state is None:
        raise ValueError("repository must expose stream_state for persisted shard cursors")
    current = stream_state.get(DISCOVERY_STREAM_STATE_NAME)
    plan = plan_two_tier_subscriptions(
        universe_symbols=universe_symbols,
        open_position_symbols=open_position_symbols,
        ranked_candidate_symbols=ranked_candidate_symbols,
        settings=settings,
        shard_cursor=current.shard_cursor,
        max_symbols_per_connection=max_symbols_per_connection,
    )
    next_cursor = current.shard_cursor
    if plan.universe_partially_streamed and plan.discovery_shards:
        next_cursor = (current.shard_cursor + 1) % len(plan.discovery_shards)
    stream_state.set_cursor(
        DISCOVERY_STREAM_STATE_NAME,
        next_cursor,
        metadata={
            "active_discovery_shard": plan.active_discovery_shard,
            "shard_count": len(plan.discovery_shards),
            "universe_partially_streamed": plan.universe_partially_streamed,
        },
    )
    return plan


class AlpacaSIPStream:
    def __init__(
        self,
        settings: DriftPilotSettings,
        *,
        clock: DriftPilotClock | None = None,
        stream: StockStream | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self._stream = stream
        self._latest_bars: dict[str, MarketBar] = {}
        self._latest_quotes: dict[str, MarketQuote] = {}
        self._session_bars: dict[str, list[MarketBar]] = {}
        self._bar_subscriptions: set[str] = set()
        self._quote_subscriptions: set[str] = set()

    @property
    def stream(self) -> StockStream:
        if self._stream is None:
            from alpaca.data.enums import DataFeed  # type: ignore[import-not-found]
            from alpaca.data.live import StockDataStream  # type: ignore[import-not-found]

            if self.settings.alpaca_data_feed.lower() != "sip":
                raise ValueError("DriftPilot autonomous stream requires ALPACA_DATA_FEED=sip")
            self._stream = StockDataStream(
                self.settings.alpaca_key_id,
                self.settings.alpaca_secret_key,
                feed=DataFeed.SIP,
            )
        return self._stream

    def subscribe_bars(self, symbols: Iterable[str]) -> None:
        requested = _normalize_symbols(symbols)
        new_symbols = [
            symbol for symbol in requested if symbol not in self._bar_subscriptions
        ]
        if not new_symbols:
            return
        self.stream.subscribe_bars(self._handle_bar_message, *new_symbols)
        self._bar_subscriptions.update(new_symbols)

    def subscribe_quotes(self, symbols: Iterable[str]) -> None:
        requested = _normalize_symbols(symbols)
        new_symbols = [
            symbol for symbol in requested if symbol not in self._quote_subscriptions
        ]
        if not new_symbols:
            return
        self.stream.subscribe_quotes(self._handle_quote_message, *new_symbols)
        self._quote_subscriptions.update(new_symbols)

    def latest_bar(self, symbol: str) -> MarketBar | None:
        return self._latest_bars.get(symbol.upper())

    def latest_quote(self, symbol: str) -> MarketQuote | None:
        return self._latest_quotes.get(symbol.upper())

    def session_bars(self, symbol: str) -> list[MarketBar]:
        return list(self._session_bars.get(symbol.upper(), []))

    def run(self) -> None:
        self.stream.run()

    def stop(self) -> None:
        self.stream.stop()

    async def _handle_bar_message(self, message: Any) -> None:
        bar = _parse_bar(message)
        self._latest_bars[bar.symbol] = bar
        current_session = self.clock.date_et()
        stored = [
            item
            for item in self._session_bars.get(bar.symbol, [])
            if self.clock.date_et(item.timestamp) == current_session
        ]
        stored.append(bar)
        self._session_bars[bar.symbol] = stored

    async def _handle_quote_message(self, message: Any) -> None:
        quote = _parse_quote(message)
        self._latest_quotes[quote.symbol] = quote


def _field(message: Any, *names: str) -> Any:
    for name in names:
        if isinstance(message, dict) and name in message:
            return message[name]
        if hasattr(message, name):
            return getattr(message, name)
    raise ValueError(f"message missing required field {names[0]}")


def _optional_field(message: Any, *names: str) -> Any | None:
    for name in names:
        if isinstance(message, dict) and name in message:
            return message[name]
        if hasattr(message, name):
            return getattr(message, name)
    return None


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return require_aware(value)
    if isinstance(value, str):
        return datetime_from_storage(value.replace("Z", "+00:00"))
    raise ValueError("market data timestamp must be a datetime or ISO string")


def _parse_bar(message: Any) -> MarketBar:
    return MarketBar(
        symbol=str(_field(message, "symbol", "S")).upper(),
        timestamp=_parse_timestamp(_field(message, "timestamp", "t")),
        open=float(_field(message, "open", "o")),
        high=float(_field(message, "high", "h")),
        low=float(_field(message, "low", "l")),
        close=float(_field(message, "close", "c")),
        volume=float(_field(message, "volume", "v")),
        trade_count=_optional_int(_optional_field(message, "trade_count", "n")),
        vwap=_optional_float(_optional_field(message, "vwap", "vw")),
    )


def _parse_quote(message: Any) -> MarketQuote:
    return MarketQuote(
        symbol=str(_field(message, "symbol", "S")).upper(),
        timestamp=_parse_timestamp(_field(message, "timestamp", "t")),
        bid_price=float(_field(message, "bid_price", "bp")),
        ask_price=float(_field(message, "ask_price", "ap")),
        bid_size=_optional_float(_optional_field(message, "bid_size", "bs")),
        ask_size=_optional_float(_optional_field(message, "ask_size", "as")),
    )


def _optional_float(value: Any | None) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any | None) -> int | None:
    return None if value is None else int(value)
