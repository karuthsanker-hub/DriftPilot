from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, cast

from driftpilot.clock import DriftPilotClock, require_aware
from driftpilot.market_data.alpaca_stream import MarketBar, MarketQuote
from driftpilot.settings import DriftPilotSettings
from driftpilot.storage import DriftPilotRepository, PositionRecord

logger = logging.getLogger(__name__)


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
    metadata: dict[str, Any] = field(default_factory=dict)


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

    def get_order_by_id(self, order_id: str) -> Any: ...

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
        repository: DriftPilotRepository | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self._trading_client = trading_client
        self.quote_provider = quote_provider
        self.order_update_stream = order_update_stream
        self.repository = repository

    @property
    def trading_client(self) -> TradingClientProtocol:
        if self._trading_client is None:
            from alpaca.trading.client import TradingClient  # type: ignore[import-not-found]

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
        from alpaca.trading.enums import QueryOrderStatus  # type: ignore[import-not-found]
        from alpaca.trading.requests import GetOrdersRequest  # type: ignore[import-not-found]

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
        protective_stop_pct: float | None = None,
    ) -> OrderSubmissionResult:
        await self._ensure_order_submission_allowed()
        quote = self._latest_quote(symbol)
        if quote is None or self._quote_is_stale(quote):
            self._record_order(
                broker_order_id=None,
                symbol=symbol,
                side="buy",
                order_type="none",
                status="rejected_quote_unavailable",
                quantity=quantity,
                slot_id=slot_id,
                metadata={"reason": "quote_unavailable"},
            )
            self._log_transition(
                "ALLOCATING",
                "entry_quote_unavailable",
                {"symbol": symbol.upper(), "slot_id": slot_id},
            )
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
        local_order_id = self._record_order(
            broker_order_id=str(_attr(order, "id")),
            symbol=symbol,
            side="buy",
            order_type="limit",
            status="submitted",
            quantity=quantity,
            slot_id=slot_id,
            limit_price=limit_price,
            metadata={"reason": "marketable_limit_submitted"},
        )
        filled = await self._wait_for_fill(str(_attr(order, "id")), self.settings.entry_limit_timeout_seconds)
        if filled is False:
            await self.cancel_order(str(_attr(order, "id")))
            self._update_order_status(
                local_order_id,
                "canceled_entry_timeout",
                {"fallback": "cancel_and_recycle", "timeout_seconds": self.settings.entry_limit_timeout_seconds},
            )
            self._log_transition(
                "ENTRY_TIMEOUT",
                "entry_limit_timeout_cancel_recycle",
                {"symbol": symbol.upper(), "broker_order_id": str(_attr(order, "id"))},
            )
            return OrderSubmissionResult(
                submitted=False,
                broker_order_id=str(_attr(order, "id")),
                symbol=symbol.upper(),
                side="buy",
                quantity=quantity,
                order_type="limit",
                limit_price=limit_price,
                reason="entry_limit_timeout_cancel_recycle",
            )
        if filled is None:
            await self.cancel_order(str(_attr(order, "id")))
            self._update_order_status(
                local_order_id,
                "canceled_entry_unknown_fill_state",
                {"fallback": "cancel_and_recycle"},
            )
            self._log_transition(
                "ENTRY_TIMEOUT",
                "entry_unknown_fill_state_cancel_recycle",
                {"symbol": symbol.upper(), "broker_order_id": str(_attr(order, "id"))},
            )
            return OrderSubmissionResult(
                submitted=False,
                broker_order_id=str(_attr(order, "id")),
                symbol=symbol.upper(),
                side="buy",
                quantity=quantity,
                order_type="limit",
                limit_price=limit_price,
                reason="entry_unknown_fill_state_cancel_recycle",
            )
        # Retrieve the actual fill price from Alpaca instead of returning
        # the order's limit_price. Paper orders fill instantly, often at a
        # better price than the limit.
        actual_fill = await self.get_fill_price(str(_attr(order, "id")))
        entry_price = actual_fill or limit_price
        metadata: dict[str, Any] = {}
        if protective_stop_pct is not None and protective_stop_pct > 0:
            stop_price = _round_price(entry_price * (1 - protective_stop_pct))
            try:
                protective = await self.submit_protective_stop_order(
                    symbol=symbol,
                    quantity=quantity,
                    stop_price=stop_price,
                    slot_id=slot_id,
                    parent_order_id=str(_attr(order, "id")),
                )
                metadata.update(
                    {
                        "protective_stop_order_id": protective.broker_order_id,
                        "protective_stop_price": stop_price,
                        "protective_stop_pct": protective_stop_pct,
                    }
                )
            except Exception as exc:
                logger.exception(
                    "protective stop placement failed for %s after entry fill; flattening: %s",
                    symbol.upper(),
                    exc,
                )
                flatten = await self.submit_emergency_market_exit(
                    symbol=symbol,
                    quantity=quantity,
                    position_id=None,
                    reason="protective_stop_failed_flatten",
                )
                return OrderSubmissionResult(
                    submitted=False,
                    broker_order_id=str(_attr(order, "id")),
                    symbol=symbol.upper(),
                    side="buy",
                    quantity=quantity,
                    order_type="limit",
                    limit_price=entry_price,
                    reason="protective_stop_failed_flattened",
                    metadata={
                        "protective_stop_error": str(exc),
                        "flatten_order_id": flatten.broker_order_id,
                    },
                )
        return OrderSubmissionResult(
            submitted=True,
            broker_order_id=str(_attr(order, "id")),
            symbol=symbol.upper(),
            side="buy",
            quantity=quantity,
            order_type="limit",
            limit_price=entry_price,
            reason="marketable_limit_submitted",
            metadata=metadata,
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
        await self._ensure_order_submission_allowed()
        quote = self._latest_quote(symbol)
        stop_breached = (
            latest_bar is not None
            and stop_price is not None
            and latest_bar.close <= stop_price
        )
        if quote is None or self._quote_is_stale(quote):
            if not stop_breached:
                self._record_order(
                    broker_order_id=None,
                    symbol=symbol,
                    side="sell",
                    order_type="none",
                    status="rejected_quote_unavailable",
                    quantity=quantity,
                    position_id=position_id,
                    metadata={"reason": "quote_unavailable"},
                )
                self._log_transition(
                    "EXITING",
                    "exit_quote_unavailable",
                    {"symbol": symbol.upper(), "position_id": position_id},
                )
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
            self._record_order(
                broker_order_id=str(_attr(order, "id")),
                symbol=symbol,
                side="sell",
                order_type="market",
                status="submitted",
                quantity=quantity,
                position_id=position_id,
                limit_price=None,
                metadata={"reason": "emergency_market_exit_stale_quote_stop_breached"},
            )
            self._log_transition(
                "EXITING",
                "emergency_market_exit_stale_quote_stop_breached",
                {"symbol": symbol.upper(), "broker_order_id": str(_attr(order, "id"))},
            )
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
        local_order_id = self._record_order(
            broker_order_id=str(_attr(order, "id")),
            symbol=symbol,
            side="sell",
            order_type="limit",
            status="submitted",
            quantity=quantity,
            position_id=position_id,
            limit_price=limit_price,
            metadata={"reason": "marketable_limit_submitted"},
        )
        filled = await self._wait_for_fill(str(_attr(order, "id")), self.settings.exit_limit_timeout_seconds)
        if filled is False:
            await self.cancel_order(str(_attr(order, "id")))
            self._update_order_status(
                local_order_id,
                "canceled_exit_timeout",
                {"fallback": "cancel_replace_once", "timeout_seconds": self.settings.exit_limit_timeout_seconds},
            )
            replacement_quote = self._latest_quote(symbol)
            if replacement_quote is not None and not self._quote_is_stale(replacement_quote):
                replacement_limit = _round_price(
                    replacement_quote.bid_price - marketable_limit_offset(replacement_quote.bid_price)
                )
                replacement_request = _order_request(
                    symbol=symbol,
                    quantity=quantity,
                    side="sell",
                    order_type="limit",
                    limit_price=replacement_limit,
                    client_order_id=_client_order_id(
                        "exit-replace", symbol, position_id, self.clock.now_utc()
                    ),
                )
                replacement = await asyncio.to_thread(self.trading_client.submit_order, replacement_request)
                replacement_local_id = self._record_order(
                    broker_order_id=str(_attr(replacement, "id")),
                    symbol=symbol,
                    side="sell",
                    order_type="limit",
                    status="submitted",
                    quantity=quantity,
                    position_id=position_id,
                    limit_price=replacement_limit,
                    metadata={"reason": "replacement_after_exit_timeout"},
                )
                replacement_filled = await self._wait_for_fill(
                    str(_attr(replacement, "id")), self.settings.exit_limit_timeout_seconds
                )
                if replacement_filled is not False:
                    return OrderSubmissionResult(
                        submitted=True,
                        broker_order_id=str(_attr(replacement, "id")),
                        symbol=symbol.upper(),
                        side="sell",
                        quantity=quantity,
                        order_type="limit",
                        limit_price=replacement_limit,
                        reason="replacement_limit_submitted",
                    )
                await self.cancel_order(str(_attr(replacement, "id")))
                self._update_order_status(
                    replacement_local_id,
                    "canceled_exit_replacement_timeout",
                    {"fallback": "emergency_market_exit"},
                )

            market_request = _order_request(
                symbol=symbol,
                quantity=quantity,
                side="sell",
                order_type="market",
                client_order_id=_client_order_id(
                    "emergency-exit-timeout", symbol, position_id, self.clock.now_utc()
                ),
            )
            market_order = await asyncio.to_thread(self.trading_client.submit_order, market_request)
            self._record_order(
                broker_order_id=str(_attr(market_order, "id")),
                symbol=symbol,
                side="sell",
                order_type="market",
                status="submitted",
                quantity=quantity,
                position_id=position_id,
                metadata={"reason": "emergency_market_exit_after_timeout"},
            )
            self._log_transition(
                "EXIT_TIMEOUT",
                "exit_limit_timeout_emergency_market",
                {"symbol": symbol.upper(), "broker_order_id": str(_attr(market_order, "id"))},
            )
            return OrderSubmissionResult(
                submitted=True,
                broker_order_id=str(_attr(market_order, "id")),
                symbol=symbol.upper(),
                side="sell",
                quantity=quantity,
                order_type="market",
                limit_price=None,
                reason="emergency_market_exit_after_timeout",
            )
        # Retrieve actual fill price from Alpaca
        actual_fill = await self.get_fill_price(str(_attr(order, "id")))
        return OrderSubmissionResult(
            submitted=True,
            broker_order_id=str(_attr(order, "id")),
            symbol=symbol.upper(),
            side="sell",
            quantity=quantity,
            order_type="limit",
            limit_price=actual_fill or limit_price,
            reason="marketable_limit_submitted",
            )

    async def submit_protective_stop_order(
        self,
        *,
        symbol: str,
        quantity: float,
        stop_price: float,
        position_id: int | None = None,
        slot_id: int | None = None,
        parent_order_id: str | None = None,
    ) -> OrderSubmissionResult:
        await self._ensure_order_submission_allowed()
        request = _order_request(
            symbol=symbol,
            quantity=quantity,
            side="sell",
            order_type="stop",
            stop_price=stop_price,
            client_order_id=_client_order_id(
                "protective-stop", symbol, position_id or slot_id, self.clock.now_utc()
            ),
        )
        order = await asyncio.to_thread(self.trading_client.submit_order, request)
        broker_order_id = str(_attr(order, "id"))
        self._record_order(
            broker_order_id=broker_order_id,
            symbol=symbol,
            side="sell",
            order_type="stop",
            status="submitted",
            quantity=quantity,
            position_id=position_id,
            slot_id=slot_id,
            limit_price=stop_price,
            metadata={
                "reason": "protective_stop_after_entry",
                "stop_price": stop_price,
                "parent_order_id": parent_order_id,
            },
        )
        self._log_transition(
            "ALLOCATING",
            "protective_stop_submitted",
            {
                "symbol": symbol.upper(),
                "broker_order_id": broker_order_id,
                "parent_order_id": parent_order_id,
                "stop_price": stop_price,
            },
        )
        return OrderSubmissionResult(
            submitted=True,
            broker_order_id=broker_order_id,
            symbol=symbol.upper(),
            side="sell",
            quantity=quantity,
            order_type="stop",
            limit_price=stop_price,
            reason="protective_stop_submitted",
            metadata={"stop_price": stop_price, "parent_order_id": parent_order_id},
        )

    async def submit_emergency_market_exit(
        self,
        *,
        symbol: str,
        quantity: float,
        position_id: int | None = None,
        reason: str = "emergency_market_exit",
    ) -> OrderSubmissionResult:
        await self._ensure_order_submission_allowed()
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
        broker_order_id = str(_attr(order, "id"))
        self._record_order(
            broker_order_id=broker_order_id,
            symbol=symbol,
            side="sell",
            order_type="market",
            status="submitted",
            quantity=quantity,
            position_id=position_id,
            metadata={"reason": reason},
        )
        self._log_transition(
            "EXITING",
            reason,
            {"symbol": symbol.upper(), "broker_order_id": broker_order_id},
        )
        return OrderSubmissionResult(
            submitted=True,
            broker_order_id=broker_order_id,
            symbol=symbol.upper(),
            side="sell",
            quantity=quantity,
            order_type="market",
            limit_price=None,
            reason=reason,
        )

    async def cancel_order(self, broker_order_id: str) -> None:
        await asyncio.to_thread(self.trading_client.cancel_order_by_id, broker_order_id)

    async def close_position(self, symbol: str) -> None:
        await self._ensure_order_submission_allowed()
        await asyncio.to_thread(self.trading_client.close_position, symbol.upper())

    async def stream_order_updates(self) -> AsyncIterator[Any]:
        if self.order_update_stream is None:
            self.order_update_stream = _AlpacaTradingUpdateStream(
                key_id=self.settings.alpaca_key_id,
                secret_key=self.settings.alpaca_secret_key,
                paper=self.settings.mode != "live",
            )
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

    async def _ensure_order_submission_allowed(self) -> None:
        if self.settings.mode != "live":
            return
        unmet: list[str] = []
        if not self.settings.backtest_expectancy_passed:
            unmet.append("backtest_expectancy")
        if not self.settings.paper_trading_gate_passed:
            unmet.append("paper_trading_60_days")
        if not self.settings.live_ok:
            unmet.append("LIVE_OK")
        account = await self.get_account()
        required_equity = self.settings.equity_floor + self.settings.live_equity_buffer
        if account.equity < required_equity:
            unmet.append("equity_floor_buffer")
        if unmet:
            raise RuntimeError(
                "Live order submission is blocked until live gate passes: "
                + ", ".join(unmet)
            )

    def _latest_quote(self, symbol: str) -> MarketQuote | None:
        if self.quote_provider is None:
            return None
        return self.quote_provider.latest_quote(symbol.upper())

    def _quote_is_stale(self, quote: MarketQuote) -> bool:
        return (
            self.clock.now_utc() - require_aware(quote.timestamp)
        ).total_seconds() > self.settings.spy_stale_seconds

    async def _wait_for_fill(self, broker_order_id: str, timeout_seconds: int) -> bool | None:
        if timeout_seconds <= 0:
            return await self._order_is_filled(broker_order_id)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() <= deadline:
            filled = await self._order_is_filled(broker_order_id)
            if filled:
                return True
            await asyncio.sleep(min(0.25, max(0.0, deadline - asyncio.get_running_loop().time())))
        return False

    async def _order_is_filled(self, broker_order_id: str) -> bool | None:
        get_order = getattr(self.trading_client, "get_order_by_id", None)
        if get_order is None:
            return None
        order = await asyncio.to_thread(get_order, broker_order_id)
        status = str(_attr(order, "status")).lower()
        if status in {"filled", "partially_filled"}:
            return True
        if status in {"canceled", "expired", "rejected"}:
            return False
        return False

    async def get_fill_price(self, broker_order_id: str) -> float | None:
        """Retrieve the actual average fill price for a completed order.

        Returns None if the order can't be found or hasn't filled.
        """
        get_order = getattr(self.trading_client, "get_order_by_id", None)
        if get_order is None:
            return None
        try:
            order = await asyncio.to_thread(get_order, broker_order_id)
            status = str(_attr(order, "status")).lower()
            if status not in {"filled", "partially_filled"}:
                return None
            # Alpaca order objects expose filled_avg_price
            fill_price = _optional_float_attr(order, "filled_avg_price")
            return fill_price
        except Exception as exc:
            logger.warning(
                "failed to retrieve fill price for broker_order_id=%s: %s",
                broker_order_id,
                exc,
            )
            return None

    def _record_order(
        self,
        *,
        broker_order_id: str | None,
        symbol: str,
        side: str,
        order_type: str,
        status: str,
        quantity: float,
        position_id: int | None = None,
        slot_id: int | None = None,
        limit_price: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        if self.repository is None:
            return None
        order = self.repository.orders.create(
            broker_order_id=broker_order_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            status=status,
            quantity=quantity,
            position_id=position_id,
            slot_id=slot_id,
            limit_price=limit_price,
            metadata=metadata,
        )
        return order.id

    def _update_order_status(
        self,
        local_order_id: int | None,
        status: str,
        metadata: dict[str, Any],
    ) -> None:
        if self.repository is None or local_order_id is None:
            return
        self.repository.orders.update_status(local_order_id, status=status, metadata=metadata)

    def _log_transition(self, from_state: str | None, reason: str, metadata: dict[str, Any]) -> None:
        if self.repository is None:
            return
        self.repository.transitions.append(
            from_state=from_state,
            to_state="EXITING" if "exit" in reason else "ALLOCATING",
            reason=reason,
            metadata=metadata,
        )


def marketable_limit_offset(price: float) -> float:
    return max(0.02, 0.0005 * price)


class _AlpacaTradingUpdateStream:
    def __init__(self, *, key_id: str, secret_key: str, paper: bool) -> None:
        self.key_id = key_id
        self.secret_key = secret_key
        self.paper = paper

    async def updates(self) -> AsyncIterator[Any]:
        from alpaca.trading.stream import TradingStream  # type: ignore[import-not-found]

        queue: asyncio.Queue[Any] = asyncio.Queue()
        stream = TradingStream(self.key_id, self.secret_key, paper=self.paper)

        async def handler(update: Any) -> None:
            await queue.put(update)

        stream.subscribe_trade_updates(handler)
        runner = asyncio.create_task(asyncio.to_thread(stream.run))
        try:
            while True:
                yield await queue.get()
        finally:
            stream.stop()
            runner.cancel()


def _order_request(
    *,
    symbol: str,
    quantity: float,
    side: str,
    order_type: str,
    client_order_id: str,
    limit_price: float | None = None,
    stop_price: float | None = None,
) -> Any:
    from alpaca.trading.enums import OrderSide, OrderType, TimeInForce  # type: ignore[import-not-found]
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest, StopOrderRequest  # type: ignore[import-not-found]

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
    if order_type == "stop":
        if stop_price is None:
            raise ValueError("stop_price is required for stop orders")
        return StopOrderRequest(
            symbol=symbol.upper(),
            qty=quantity,
            side=order_side,
            type=OrderType.STOP,
            time_in_force=TimeInForce.DAY,
            stop_price=_round_price(stop_price),
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
