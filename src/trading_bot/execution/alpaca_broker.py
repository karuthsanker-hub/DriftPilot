from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Protocol

from trading_bot.settings import AppSettings


@dataclass(frozen=True)
class OrderIntent:
    ticker: str
    side: str
    shares: int
    strategy: str


@dataclass(frozen=True)
class OrderResult:
    ticker: str
    side: str
    shares: int
    submitted: bool
    message: str
    order_id: str | None = None


class Broker(Protocol):
    def submit_market_order(self, intent: OrderIntent, *, dry_run: bool = True) -> OrderResult: ...


class AlpacaBroker:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def submit_market_order(self, intent: OrderIntent, *, dry_run: bool = True) -> OrderResult:
        if dry_run:
            return OrderResult(intent.ticker, intent.side, intent.shares, False, "dry-run order not submitted")
        if not self.settings.paper_mode:
            raise RuntimeError("Live mode is blocked in v1")
        if self.settings.alpaca_api_key is None or self.settings.alpaca_secret_key is None:
            raise RuntimeError("Alpaca credentials are not configured")

        from alpaca.common.exceptions import APIError
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
        from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

        client = TradingClient(
            self.settings.alpaca_api_key.get_secret_value(),
            self.settings.alpaca_secret_key.get_secret_value(),
            paper=True,
        )
        side = OrderSide.BUY if intent.side in {"buy", "cover"} else OrderSide.SELL
        _cancel_conflicting_open_orders(client, intent.ticker, side, QueryOrderStatus, GetOrdersRequest)
        try:
            order = client.submit_order(
                MarketOrderRequest(
                    symbol=intent.ticker,
                    qty=intent.shares,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
            )
        except APIError as exc:
            conflict = _alpaca_order_conflict(exc)
            if conflict:
                return OrderResult(intent.ticker, intent.side, intent.shares, False, conflict, _existing_order_id(exc))
            raise
        return OrderResult(intent.ticker, intent.side, intent.shares, True, "submitted to Alpaca paper", str(order.id))

    def reset_paper_account(self) -> dict[str, int | str]:
        if not self.settings.paper_mode:
            raise RuntimeError("Live mode is blocked in v1")
        if self.settings.alpaca_api_key is None or self.settings.alpaca_secret_key is None:
            raise RuntimeError("Alpaca credentials are not configured")

        from alpaca.trading.client import TradingClient

        client = TradingClient(
            self.settings.alpaca_api_key.get_secret_value(),
            self.settings.alpaca_secret_key.get_secret_value(),
            paper=True,
        )
        canceled = client.cancel_orders()
        closed = client.close_all_positions(cancel_orders=True)
        return {
            "canceled_orders": len(canceled) if isinstance(canceled, list) else 0,
            "closed_positions": len(closed) if isinstance(closed, list) else 0,
        }


def _cancel_conflicting_open_orders(client, ticker: str, side, query_order_status, get_orders_request) -> int:
    orders = client.get_orders(get_orders_request(status=query_order_status.OPEN, symbols=[ticker.upper()]))
    canceled = 0
    for order in orders:
        if str(getattr(order, "symbol", "")).upper() != ticker.upper():
            continue
        if getattr(order, "side", None) == side:
            continue
        client.cancel_order_by_id(getattr(order, "id"))
        canceled += 1
    return canceled


def _alpaca_order_conflict(exc: Exception) -> str | None:
    text = str(exc)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {}
    message = str(payload.get("message") or text)
    if "wash trade" not in message.lower() and "opposite side" not in message.lower():
        return None
    existing = payload.get("existing_order_id")
    suffix = f" Existing Alpaca order: {existing}." if existing else ""
    return f"Blocked by an existing opposite-side Alpaca paper order for this symbol.{suffix} Wait for cancel/fill, then retry."


def _existing_order_id(exc: Exception) -> str | None:
    try:
        payload = json.loads(str(exc))
    except json.JSONDecodeError:
        return None
    value = payload.get("existing_order_id")
    return str(value) if value else None
