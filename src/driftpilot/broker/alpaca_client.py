from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from driftpilot.clock import DriftPilotClock, require_aware
from driftpilot.market_data.alpaca_stream import MarketBar, MarketQuote
from driftpilot.settings import DriftPilotSettings
from driftpilot.storage import DriftPilotRepository, PositionRecord


@dataclass(frozen=True, slots=True)
class BrokerAccount:
    account_id: str | None
    equity: float
    buying_power: float
    cash: float
    status: str


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    symbol: str
    quantity: float
    average_entry_price: float
    market_value: float | None = None
    broker_position_id: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    broker_order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    quantity: float
    limit_price: float | None
    submitted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OrderSubmissionResult:
    submitted: bool
    broker_order_id: str | None
    symbol: str
    side: str
    quantity: float
    order_type: str
    limit_price: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    broker_positions: tuple[BrokerPosition, ...]
    local_open_before: tuple[PositionRecord, ...]
    local_open_after: tuple[PositionRecord, ...]
    mismatched_symbols: tuple[str, ...]
    action: str


class TradingClientProtocol(Protocol):
    def get_account(self) -> Any: ...

    def get_all_positions(self) -> list[Any]: ...

    def get_orders(self, request: Any) -> list[Any]: ...

    def submit_order(self, request: Any) -> Any: ...

    def cancel_order_by_id(self, order_id: str) -> Any: ...

    def close_position(self, symbol: str) -> Any: ...


class OrderUpdateStreamProtocol(Protocol):
    def updates(self) -> AsyncIterator[Any]: ...


class QuoteProvider(Protocol):
    def latest_quote(self, symbol: str) -> MarketQuote | None: ...


class AlpacaBrokerClient:
    def __init__(
        self,
        settings: DriftPilotSettings,
        *,
        clock: DriftPilotClock | None = None,
        trading_client: TradingClientProtocol | None = None,
        quote_provider: QuoteProvider | None = None,
        order_update_stream: OrderUpdateStreamProtocol | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self._trading_client = trading_client
        self.quote_provider = quote_provider
        self.order_update_stream = order_update_stream

    @property
    def trading_client(self) -> TradingClientProtocol:
        if self._trading_client is None:
            from alpaca.trading.client import TradingClient

            client = cast(
                TradingClientProtocol,
                TradingClient(
                    self.settings.alpaca_key_id,
                    self.settings.alpaca_secret_key,
                    paper=self.settings.mode != "live",
                    url_override=self._base_url(),
                ),
            )
            self._trading_client = client
        return cast(TradingClientProtocol, self._trading_client)

    async def get_account(self) -> BrokerAccount:
        account = await asyncio.to_thread(self.trading_client.get_account)
        return BrokerAccount(
            account_id=_optional_str(account, "id", "account_number"),
            equity=_float_attr(account, "equity"),
            buying_power=_float_attr(account, "buying_power"),
            cash=_float_attr(account, "cash"),
            status=str(_attr(account, "status")),
        )

    async def get_open_positions(self) -> list[BrokerPosition]:
        positions = await asyncio.to_thread(self.trading_client.get_all_positions)
        return [_broker_position_from_alpaca(position) for position in positions]

    async def get_open_orders(self) -> list[BrokerOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = await asyncio.to_thread(
            self.trading_client.get_orders,
            GetOrdersRequest(status=QueryOrderStatus.OPEN),
        )
        return [_broker_order_from_alpaca(order) for order in orders]

    async def submit_entry_order(
        self,
        *,
        symbol: str,
        quantity: float,
        slot_id: int | None = None,
    ) -> OrderSubmissionResult:
        self._ensure_order_submission_allowed()
        quote = self._latest_quote(symbol)
        if quote is None or self._quote_is_stale(quote):
            return OrderSubmissionResult(
                submitted=False,
                broker_order_id=None,
                symbol=symbol.upper(),
                side="buy",
                quantity=quantity,
                order_type="none",
                limit_price=None,
                reason="quote_unavailable",
            )
        limit_price = _round_price(
            quote.ask_price + marketable_limit_offset(quote.ask_price)
        )
        request = _order_request(
            symbol=symbol,
            quantity=quantity,
            side="buy",
            order_type="limit",
            limit_price=limit_price,
            client_order_id=_client_order_id(
                "entry", symbol, slot_id, self.clock.now_utc()
            ),
        )
        order = await asyncio.to_thread(self.trading_client.submit_order, request)
        return OrderSubmissionResult(
            submitted=True,
            broker_order_id=str(_attr(order, "id")),
            symbol=symbol.upper(),
            side="buy",
            quantity=quantity,
            order_type="limit",
            limit_price=limit_price,
            reason="marketable_limit_submitted",
        )

    async def submit_exit_order(
        self,
        *,
        symbol: str,
        quantity: float,
        position_id: int | None = None,
        latest_bar: MarketBar | None = None,
        stop_price: float | None = None,
    ) -> OrderSubmissionResult:
        self._ensure_order_submission_allowed()
        quote = self._latest_quote(symbol)
        stop_breached = (
            latest_bar is not None
            and stop_price is not None
            and latest_bar.close <= stop_price
        )
        if quote is None or self._quote_is_stale(quote):
            if not stop_breached:
                return OrderSubmissionResult(
                    submitted=False,
                    broker_order_id=None,
                    symbol=symbol.upper(),
                    side="sell",
                    quantity=quantity,
                    order_type="none",
                    limit_price=None,
                    reason="quote_unavailable",
                )
            request = _order_request(
                symbol=symbol,
                quantity=quantity,
                side="sell",
                order_type="market",
                client_order_id=_client_order_id(
                    "emergency-exit", symbol, position_id, self.clock.now_utc()
                ),
            )
            order = await asyncio.to_thread(self.trading_client.submit_order, request)
            return OrderSubmissionResult(
                submitted=True,
                broker_order_id=str(_attr(order, "id")),
                symbol=symbol.upper(),
                side="sell",
                quantity=quantity,
                order_type="market",
                limit_price=None,
                reason="emergency_market_exit_stale_quote_stop_breached",
            )

        limit_price = _round_price(
            quote.bid_price - marketable_limit_offset(quote.bid_price)
        )
        request = _order_request(
            symbol=symbol,
            quantity=quantity,
            side="sell",
            order_type="limit",
            limit_price=limit_price,
            client_order_id=_client_order_id(
                "exit", symbol, position_id, self.clock.now_utc()
            ),
        )
        order = await asyncio.to_thread(self.trading_client.submit_order, request)
        return OrderSubmissionResult(
            submitted=True,
            broker_order_id=str(_attr(order, "id")),
            symbol=symbol.upper(),
            side="sell",
            quantity=quantity,
            order_type="limit",
            limit_price=limit_price,
            reason="marketable_limit_submitted",
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        await asyncio.to_thread(self.trading_client.cancel_order_by_id, broker_order_id)

    async def close_position(self, symbol: str) -> None:
        self._ensure_order_submission_allowed()
        await asyncio.to_thread(self.trading_client.close_position, symbol.upper())

    async def stream_order_updates(self) -> AsyncIterator[Any]:
        if self.order_update_stream is None:
            raise RuntimeError("order update stream is not configured")
        async for update in self.order_update_stream.updates():
            yield update

    async def reconcile_boot(
        self, repository: DriftPilotRepository
    ) -> ReconciliationResult:
        broker_positions = tuple(await self.get_open_positions())
        local_before = tuple(repository.positions.list_open())
        reconciliation = repository.positions.reconcile_broker_open_positions(
            broker_positions=[
                {
                    "broker_position_id": position.broker_position_id,
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "entry_price": position.average_entry_price,
                    "metadata": {"market_value": position.market_value},
                }
                for position in broker_positions
            ],
            slot_value=self.settings.slot_value,
            target_pct=self.settings.target_pct,
            stop_pct=self.settings.stop_pct,
            trade_slots=self.settings.trade_slots,
        )
        local_after = tuple(repository.positions.list_open())
        broker_symbols = {position.symbol for position in broker_positions}
        local_symbols = {position.symbol for position in local_before}
        mismatches = tuple(sorted(broker_symbols.symmetric_difference(local_symbols)))
        transition = repository.transitions.append(
            from_state="BOOT",
            to_state="IN_POSITION" if broker_positions else "SCANNING",
            reason="broker_reconciliation",
            metadata={
                "action": reconciliation,
                "mismatched_symbols": list(mismatches),
                "broker_positions": sorted(broker_symbols),
            },
        )
        repository.state.set(
            "IN_POSITION" if broker_positions else "SCANNING",
            last_transition_id=transition.id,
            metadata={"broker_reconciliation": reconciliation},
        )
        return ReconciliationResult(
            broker_positions=broker_positions,
            local_open_before=local_before,
            local_open_after=local_after,
            mismatched_symbols=mismatches,
            action=reconciliation,
        )

    def _base_url(self) -> str:
        return (
            self.settings.alpaca_live_base_url
            if self.settings.mode == "live"
            else self.settings.alpaca_paper_base_url
        )

    def _ensure_order_submission_allowed(self) -> None:
        if self.settings.mode == "live" and not self.settings.live_ok:
            raise RuntimeError(
                "Live order submission is blocked until live gate passes"
            )

    def _latest_quote(self, symbol: str) -> MarketQuote | None:
        if self.quote_provider is None:
            return None
        return self.quote_provider.latest_quote(symbol.upper())

    def _quote_is_stale(self, quote: MarketQuote) -> bool:
        return (
            self.clock.now_utc() - require_aware(quote.timestamp)
        ).total_seconds() > self.settings.spy_stale_seconds


def marketable_limit_offset(price: float) -> float:
    return max(0.02, 0.0005 * price)


def _order_request(
    *,
    symbol: str,
    quantity: float,
    side: str,
    order_type: str,
    client_order_id: str,
    limit_price: float | None = None,
) -> Any:
    from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

    order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL
    if order_type == "market":
        return MarketOrderRequest(
            symbol=symbol.upper(),
            qty=quantity,
            side=order_side,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
    if limit_price is None:
        raise ValueError("limit_price is required for limit orders")
    return LimitOrderRequest(
        symbol=symbol.upper(),
        qty=quantity,
        side=order_side,
        type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=client_order_id,
    )


def _client_order_id(
    prefix: str, symbol: str, identifier: int | None, timestamp: datetime
) -> str:
    safe_symbol = symbol.upper().replace(".", "-")
    suffix = "na" if identifier is None else str(identifier)
    return f"dp-{prefix}-{safe_symbol}-{suffix}-{int(timestamp.timestamp())}"


def _round_price(price: float) -> float:
    return round(price, 2 if price >= 1 else 4)


def _broker_position_from_alpaca(position: Any) -> BrokerPosition:
    return BrokerPosition(
        symbol=str(_attr(position, "symbol")).upper(),
        quantity=_float_attr(position, "qty"),
        average_entry_price=_float_attr(position, "avg_entry_price"),
        market_value=_optional_float_attr(position, "market_value"),
        broker_position_id=_optional_str(position, "asset_id", "id"),
    )


def _broker_order_from_alpaca(order: Any) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=str(_attr(order, "id")),
        symbol=str(_attr(order, "symbol")).upper(),
        side=str(_attr(order, "side")),
        order_type=str(_attr(order, "type")),
        status=str(_attr(order, "status")),
        quantity=_float_attr(order, "qty"),
        limit_price=_optional_float_attr(order, "limit_price"),
        submitted_at=_optional_datetime_attr(order, "submitted_at"),
    )


def _attr(value: Any, name: str) -> Any:
    if isinstance(value, dict) and name in value:
        return value[name]
    if hasattr(value, name):
        return getattr(value, name)
    raise ValueError(f"broker payload missing required field {name}")


def _optional_str(value: Any, *names: str) -> str | None:
    for name in names:
        if isinstance(value, dict) and name in value and value[name] is not None:
            return str(value[name])
        if hasattr(value, name) and getattr(value, name) is not None:
            return str(getattr(value, name))
    return None


def _float_attr(value: Any, name: str) -> float:
    return float(_attr(value, name))


def _optional_float_attr(value: Any, name: str) -> float | None:
    if isinstance(value, dict) and name in value and value[name] is not None:
        return float(value[name])
    if hasattr(value, name) and getattr(value, name) is not None:
        return float(getattr(value, name))
    return None


def _optional_datetime_attr(value: Any, name: str) -> datetime | None:
    if isinstance(value, dict) and name in value and value[name] is not None:
        raw = value[name]
    elif hasattr(value, name) and getattr(value, name) is not None:
        raw = getattr(value, name)
    else:
        return None
    if isinstance(raw, datetime):
        return require_aware(raw)
    if isinstance(raw, str):
        from driftpilot.clock import datetime_from_storage

        return datetime_from_storage(raw.replace("Z", "+00:00"))
    raise ValueError(f"broker field {name} must be datetime or ISO string")
