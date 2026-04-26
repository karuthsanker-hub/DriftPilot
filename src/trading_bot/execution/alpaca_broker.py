from __future__ import annotations

from dataclasses import dataclass
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

        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        client = TradingClient(
            self.settings.alpaca_api_key.get_secret_value(),
            self.settings.alpaca_secret_key.get_secret_value(),
            paper=True,
        )
        side = OrderSide.BUY if intent.side in {"buy", "cover"} else OrderSide.SELL
        order = client.submit_order(
            MarketOrderRequest(
                symbol=intent.ticker,
                qty=intent.shares,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        )
        return OrderResult(intent.ticker, intent.side, intent.shares, True, "submitted to Alpaca paper", str(order.id))

