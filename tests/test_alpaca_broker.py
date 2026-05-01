from __future__ import annotations

from dataclasses import dataclass

from alpaca.trading.enums import OrderSide

from trading_bot.execution.alpaca_broker import _alpaca_order_conflict, _cancel_conflicting_open_orders


@dataclass
class FakeOrder:
    id: str
    symbol: str
    side: OrderSide


class FakeClient:
    def __init__(self) -> None:
        self.canceled = []

    def get_orders(self, _request):
        return [
            FakeOrder("same", "AMZN", OrderSide.BUY),
            FakeOrder("opposite", "AMZN", OrderSide.SELL),
            FakeOrder("other", "MSFT", OrderSide.SELL),
        ]

    def cancel_order_by_id(self, order_id):
        self.canceled.append(order_id)


class FakeOrderStatus:
    OPEN = "open"


def test_cancel_conflicting_open_orders_only_cancels_opposite_symbol_side() -> None:
    client = FakeClient()

    canceled = _cancel_conflicting_open_orders(client, "AMZN", OrderSide.BUY, FakeOrderStatus, lambda **kwargs: kwargs)

    assert canceled == 1
    assert client.canceled == ["opposite"]


def test_alpaca_order_conflict_returns_operator_safe_message() -> None:
    exc = Exception(
        '{"code":40310000,"existing_order_id":"order-1","message":"potential wash trade detected. use complex orders","reject_reason":"opposite side market/stop order exists"}'
    )

    message = _alpaca_order_conflict(exc)

    assert message is not None
    assert "Blocked by an existing opposite-side Alpaca paper order" in message
    assert "order-1" in message
