from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from driftpilot.broker.alpaca_client import AlpacaBrokerClient
from driftpilot.clock import FixedClock
from driftpilot.market_data.alpaca_stream import (
    MarketBar,
    MarketQuote,
    plan_two_tier_subscriptions,
)
from driftpilot.settings import DriftPilotSettings
from driftpilot.storage import DriftPilotRepository


@dataclass
class FakeQuoteProvider:
    quote: MarketQuote | None

    def latest_quote(self, _symbol: str) -> MarketQuote | None:
        return self.quote


class FakeTradingClient:
    def __init__(self) -> None:
        self.submitted_requests: list[Any] = []
        self.positions: list[Any] = []

    def get_account(self) -> Any:
        return SimpleNamespace(
            id="acct-1",
            equity="10000",
            buying_power="10000",
            cash="10000",
            status="ACTIVE",
        )

    def get_all_positions(self) -> list[Any]:
        return self.positions

    def get_orders(self, _request: Any) -> list[Any]:
        return []

    def submit_order(self, request: Any) -> Any:
        self.submitted_requests.append(request)
        return SimpleNamespace(id=f"order-{len(self.submitted_requests)}")

    def cancel_order_by_id(self, _order_id: str) -> None:
        return None

    def close_position(self, _symbol: str) -> None:
        return None


def test_entry_order_uses_marketable_limit_from_fresh_quote() -> None:
    now = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    trading_client = FakeTradingClient()
    quote_provider = FakeQuoteProvider(
        MarketQuote(
            symbol="AAPL",
            timestamp=now,
            bid_price=99.95,
            ask_price=100.00,
        )
    )
    broker = AlpacaBrokerClient(
        DriftPilotSettings(),
        clock=FixedClock(fixed_now=now),
        trading_client=trading_client,
        quote_provider=quote_provider,
    )

    result = asyncio.run(
        broker.submit_entry_order(symbol="aapl", quantity=10, slot_id=1)
    )

    request = trading_client.submitted_requests[0]
    assert result.submitted is True
    assert result.order_type == "limit"
    assert request.symbol == "AAPL"
    assert request.side.value == "buy"
    assert request.type.value == "limit"
    assert request.limit_price == 100.05


def test_exit_order_falls_back_to_market_when_quote_stale_and_stop_breached() -> None:
    now = datetime(2026, 4, 30, 14, 0, tzinfo=UTC)
    trading_client = FakeTradingClient()
    quote_provider = FakeQuoteProvider(
        MarketQuote(
            symbol="MSFT",
            timestamp=now - timedelta(seconds=120),
            bid_price=94.90,
            ask_price=95.00,
        )
    )
    broker = AlpacaBrokerClient(
        DriftPilotSettings(spy_stale_seconds=60),
        clock=FixedClock(fixed_now=now),
        trading_client=trading_client,
        quote_provider=quote_provider,
    )
    latest_bar = MarketBar(
        symbol="MSFT",
        timestamp=now,
        open=96,
        high=96,
        low=94,
        close=94.5,
        volume=1000,
    )

    result = asyncio.run(
        broker.submit_exit_order(
            symbol="msft",
            quantity=5,
            position_id=3,
            latest_bar=latest_bar,
            stop_price=95,
        )
    )

    request = trading_client.submitted_requests[0]
    assert result.submitted is True
    assert result.order_type == "market"
    assert result.reason == "emergency_market_exit_stale_quote_stop_breached"
    assert request.symbol == "MSFT"
    assert request.side.value == "sell"
    assert request.type.value == "market"


def test_boot_reconciliation_uses_broker_truth_when_local_position_mismatches() -> None:
    now = datetime(2026, 4, 30, 13, 30, tzinfo=UTC)
    repo = DriftPilotRepository.open(":memory:", FixedClock(fixed_now=now))
    repo.slots.upsert(1, status="occupied", slot_value=1_000, symbol="AAPL")
    local_position = repo.positions.create_open(
        symbol="AAPL",
        slot_id=1,
        quantity=4,
        entry_price=100,
        target_price=101,
        stop_price=99,
        opened_at=now,
    )
    trading_client = FakeTradingClient()
    trading_client.positions = [
        SimpleNamespace(
            symbol="MSFT",
            qty="7",
            avg_entry_price="250",
            market_value="1750",
            asset_id="asset-msft",
        )
    ]
    broker = AlpacaBrokerClient(
        DriftPilotSettings(),
        clock=FixedClock(fixed_now=now),
        trading_client=trading_client,
    )

    result = asyncio.run(broker.reconcile_boot(repo))

    stale_position = repo.positions.get(local_position.id)
    open_positions = repo.positions.list_open()
    slot = repo.slots.get(1)
    state = repo.state.get()
    assert result.action == "mismatch_corrected"
    assert result.mismatched_symbols == ("AAPL", "MSFT")
    assert stale_position is not None
    assert stale_position.status == "closed"
    assert stale_position.exit_reason == "broker_missing_at_boot"
    assert [position.symbol for position in open_positions] == ["MSFT"]
    assert slot is not None
    assert slot.status == "occupied"
    assert slot.symbol == "MSFT"
    assert state is not None
    assert state.current_state == "IN_POSITION"


def test_two_tier_subscription_routing_shards_only_discovery_symbols() -> None:
    settings = DriftPilotSettings(always_on_candidate_count=2)
    universe = ["AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META"]

    first_plan = plan_two_tier_subscriptions(
        universe_symbols=universe,
        open_position_symbols=["TSLA"],
        ranked_candidate_symbols=["AAPL", "MSFT", "NVDA"],
        settings=settings,
        shard_cursor=0,
        max_symbols_per_connection=6,
    )
    second_plan = plan_two_tier_subscriptions(
        universe_symbols=universe,
        open_position_symbols=["TSLA"],
        ranked_candidate_symbols=["AAPL", "MSFT", "NVDA"],
        settings=settings,
        shard_cursor=1,
        max_symbols_per_connection=6,
    )

    assert first_plan.always_on_symbols == ("SPY", "QQQ", "TSLA", "AAPL", "MSFT")
    assert first_plan.universe_partially_streamed is True
    assert "TSLA" in first_plan.active_symbols
    assert first_plan.active_symbols == ("SPY", "QQQ", "TSLA", "AAPL", "MSFT", "NVDA")
    assert second_plan.active_symbols == ("SPY", "QQQ", "TSLA", "AAPL", "MSFT", "AMD")
