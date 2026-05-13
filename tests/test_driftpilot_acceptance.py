from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pandas as pd  # type: ignore[import-untyped]
import pytest

from driftpilot.backtest.replay import replay_bars
from driftpilot.broker.alpaca_client import AlpacaBrokerClient
from driftpilot.clock import FixedClock
from driftpilot.execution.paper_fills import entry_fill, exit_fill, slippage_for_price
from driftpilot.execution.slot_allocator import AllocationCandidate, SlotAllocator
from driftpilot.settings import DriftPilotSettings
from driftpilot.signals.features import MinuteBar
from driftpilot.signals.intraday_momentum import entry_filter
from driftpilot.signals.regime import Regime, RegimeSnapshot, compute_index_regime_metrics
from driftpilot.storage.repositories import DriftPilotRepository


NOW = datetime(2026, 4, 30, 14, 30, tzinfo=UTC)


class LowEquityTradingClient:
    def get_account(self):
        return type(
            "Account",
            (),
            {"id": "acct", "equity": "25000", "buying_power": "25000", "cash": "25000", "status": "ACTIVE"},
        )()

    def get_all_positions(self) -> list[object]:
        return []

    def get_orders(self, request: object) -> list[object]:
        return []

    def submit_order(self, request: object) -> object:
        raise AssertionError("live gate should block before submit_order")

    def get_order_by_id(self, order_id: str) -> object:
        raise AssertionError("not used")

    def cancel_order_by_id(self, order_id: str) -> object:
        raise AssertionError("not used")

    def close_position(self, symbol: str) -> object:
        raise AssertionError("not used")


def _repo(tmp_path, *, slots: int = 10) -> DriftPilotRepository:
    repo = DriftPilotRepository.open(tmp_path / "operator.sqlite3", FixedClock(fixed_now=NOW))
    for slot_id in range(1, slots + 1):
        repo.slots.upsert(slot_id, status="EMPTY", slot_value=1_000.0)
    return repo


def _candidate(symbol: str, sector: str = "Tech", rank: int = 1) -> AllocationCandidate:
    return AllocationCandidate(symbol=symbol, score=10 - rank, sector=sector, latest_bar_at=NOW, rank=rank)


def test_acceptance_crash_recovery_reconciles_broker_as_truth(tmp_path) -> None:
    repo = _repo(tmp_path, slots=2)
    repo.positions.create_open(symbol="OLD", quantity=1, entry_price=10, target_price=11, stop_price=9, slot_id=1, opened_at=NOW)

    result = repo.positions.reconcile_broker_open_positions(
        broker_positions=[{"symbol": "NEW", "quantity": 2, "entry_price": 20}],
        slot_value=1_000,
        target_pct=0.01,
        stop_pct=0.01,
        trade_slots=2,
    )

    assert result == "mismatch_corrected"
    assert [position.symbol for position in repo.positions.list_open()] == ["NEW"]


def test_acceptance_allocator_concurrency_distinct_candidates(tmp_path) -> None:
    async def run() -> None:
        repo = _repo(tmp_path, slots=2)
        allocator = SlotAllocator(repo, DriftPilotSettings(), clock=FixedClock(fixed_now=NOW))
        first, second = await asyncio.gather(
            allocator.allocate([_candidate("AAA", rank=1), _candidate("BBB", rank=2)]),
            allocator.allocate([_candidate("AAA", rank=1), _candidate("BBB", rank=2)]),
        )
        assert {item.symbol for item in [*first.allocations, *second.allocations]} == {"AAA", "BBB"}

    asyncio.run(run())


def test_acceptance_red_regime_requires_relative_strength() -> None:
    red_spy_bars = [
        MinuteBar("SPY", NOW + timedelta(minutes=index), 100, 100.1, 99.5, 100 - index * 0.05, 1000)
        for index in range(20)
    ]
    regime = RegimeSnapshot(Regime.RED, compute_index_regime_metrics(red_spy_bars, symbol="SPY"))
    weak = type(
        "Features",
        (),
        {
            "symbol": "WEAK",
            "return_15m": 0.001,
            "has_rvol_history": True,
            "rvol": 2.5,
            "above_vwap": True,
            "has_15m_history": True,
            "spread": 0.01,
            "spread_ok": True,
        },
    )()

    assert entry_filter(weak, regime).allowed is False


def test_acceptance_pdt_guard_blocks_live_entries_but_not_paper() -> None:
    live = AlpacaBrokerClient(
        DriftPilotSettings(mode="live", live_ok=True, backtest_expectancy_passed=True, paper_trading_gate_passed=True),
        clock=FixedClock(fixed_now=NOW),
        trading_client=LowEquityTradingClient(),
    )

    with pytest.raises(RuntimeError, match="equity_floor_buffer"):
        asyncio.run(live.submit_entry_order(symbol="AAPL", quantity=1, slot_id=1))


def test_acceptance_slippage_formula_equivalent_between_backtest_and_paper() -> None:
    entry = entry_fill(symbol="AAA", quantity=1, reference_price=100, filled_at=NOW)
    exit_ = exit_fill(symbol="AAA", quantity=1, reference_price=100, filled_at=NOW)

    assert entry.price == pytest.approx(100 + slippage_for_price(100))
    assert exit_.price == pytest.approx(100 - slippage_for_price(100))


def test_acceptance_time_stop_fires_after_max_hold_minutes() -> None:
    rows = []
    start = datetime(2026, 4, 30, 13, 30, tzinfo=UTC)
    for history_day in range(20, 0, -1):
        for minute in range(70):
            timestamp = start - timedelta(days=history_day) + timedelta(minutes=minute)
            rows.append({"timestamp": timestamp, "symbol": "SPY", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000})
            rows.append({"timestamp": timestamp, "symbol": "AAA", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 100})
    for minute in range(70):
        timestamp = start + timedelta(minutes=minute)
        price = 100 + min(minute, 20) * 0.06
        rows.append({"timestamp": timestamp, "symbol": "SPY", "open": 100, "high": 101, "low": 99, "close": 100 + minute * 0.01, "volume": 1000})
        rows.append({"timestamp": timestamp, "symbol": "AAA", "open": price, "high": price + 0.02, "low": price - 0.02, "close": price, "volume": 300 if minute >= 20 else 100})

    result = replay_bars(pd.DataFrame(rows), settings=DriftPilotSettings(trade_slots=1, max_hold_minutes=45), rvol_lookback=20)

    assert any(trade.exit_reason == "TIME" and trade.hold_minutes == 45 for trade in result.trades)


def test_acceptance_sector_cap_blocks_fourth_same_sector_candidate(tmp_path) -> None:
    async def run() -> None:
        repo = _repo(tmp_path, slots=5)
        allocator = SlotAllocator(repo, DriftPilotSettings(), clock=FixedClock(fixed_now=NOW), max_slots_per_sector=3)
        result = await allocator.allocate([_candidate(f"T{i}", rank=i) for i in range(1, 6)])
        assert len(result.allocations) == 3
        assert [rejection.reason for rejection in result.rejections] == ["sector_cap_reached", "sector_cap_reached"]

    asyncio.run(run())


def test_acceptance_live_gate_lists_unmet_criteria() -> None:
    live = AlpacaBrokerClient(
        DriftPilotSettings(mode="live"),
        clock=FixedClock(fixed_now=NOW),
        trading_client=LowEquityTradingClient(),
    )

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(live.submit_entry_order(symbol="AAPL", quantity=1, slot_id=1))

    message = str(exc.value)
    assert "backtest_expectancy" in message
    assert "paper_trading_60_days" in message
    assert "LIVE_OK" in message
