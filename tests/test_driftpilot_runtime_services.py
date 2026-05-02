from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from driftpilot.clock import FixedClock
from driftpilot.operator import MockOpenMarketClock
from driftpilot.services import MockBrokerReconciler, PaperExecutionAllocator, PaperPositionMonitor, SyntheticScannerService
from driftpilot.settings import DriftPilotSettings
from driftpilot.state_machine import DriftPilotStateMachine
from driftpilot.states import OperatorState
from driftpilot.storage.repositories import DriftPilotRepository


NOW = datetime(2026, 4, 30, 14, 30, tzinfo=UTC)


def test_operator_runtime_once_writes_slots_candidates_and_positions(tmp_path) -> None:
    async def run() -> None:
        settings = DriftPilotSettings(sqlite_path=str(tmp_path / "operator.sqlite3"), trade_slots=2, slot_value=1_000)
        repo = DriftPilotRepository.open(settings.sqlite_path, FixedClock(fixed_now=NOW))
        machine = DriftPilotStateMachine(
            repo,
            settings,
            clock=FixedClock(fixed_now=NOW),
            market_clock=MockOpenMarketClock(),
            broker=MockBrokerReconciler(repo, settings),
            scanner=SyntheticScannerService(repo, settings, clock=FixedClock(fixed_now=NOW), universe_file=tmp_path / "missing.csv"),
            allocator=PaperExecutionAllocator(repo, settings, clock=FixedClock(fixed_now=NOW)),
            position_monitor=PaperPositionMonitor(repo, settings, clock=FixedClock(fixed_now=NOW)),
        )

        state = await machine.run_once()

        assert state == OperatorState.IN_POSITION
        assert len(repo.positions.list_open()) == 2
        assert [slot.status for slot in repo.slots.list_all()] == ["OPEN", "OPEN"]
        assert repo.list_candidates(limit=20)

    asyncio.run(run())


def test_position_monitor_exits_and_recycles_slot(tmp_path) -> None:
    async def run() -> None:
        settings = DriftPilotSettings(sqlite_path=str(tmp_path / "operator.sqlite3"), trade_slots=1, slot_value=1_000)
        opened_at = NOW - timedelta(minutes=2)
        repo = DriftPilotRepository.open(settings.sqlite_path, FixedClock(fixed_now=NOW))
        repo.slots.upsert(1, status="OPEN", symbol="AAA", slot_value=1_000, updated_at=opened_at)
        position = repo.positions.create_open(
            symbol="AAA",
            quantity=10,
            entry_price=100,
            target_price=101,
            stop_price=99,
            slot_id=1,
            opened_at=opened_at,
            metadata={"scenario": "TARGET"},
        )
        repo.slots.upsert(1, status="OPEN", symbol="AAA", position_id=position.id, slot_value=1_000, updated_at=opened_at)

        result = await PaperPositionMonitor(repo, settings, clock=FixedClock(fixed_now=NOW)).monitor()

        assert result.recycled_slots == 1
        assert repo.positions.list_open() == []
        assert repo.slots.get(1).status == "EMPTY"  # type: ignore[union-attr]
        assert repo.list_recycle_events(limit=1)[0].exit_reason == "TARGET"

    asyncio.run(run())
