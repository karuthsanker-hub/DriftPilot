from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from driftpilot.clock import FixedClock
from driftpilot.execution.slot_allocator import AllocationCandidate, AllocationResult
from driftpilot.settings import DriftPilotSettings
from driftpilot.state_machine import DriftPilotStateMachine, MarketSession, ScanResult
from driftpilot.states import OperatorState
from driftpilot.storage.repositories import DriftPilotRepository


NOW = datetime(2026, 4, 30, 14, 30, tzinfo=UTC)


class AlwaysOpenClock:
    def session(self, now=None) -> MarketSession:
        return MarketSession(True, "regular_session")


class AlwaysClosedClock:
    def session(self, now=None) -> MarketSession:
        return MarketSession(False, "after_close", NOW + timedelta(days=1))


class Scanner:
    def __init__(self, result: ScanResult) -> None:
        self.result = result

    async def scan(self) -> ScanResult:
        return self.result


class Allocator:
    def __init__(self) -> None:
        self.calls = 0

    async def allocate(self, candidates: list[AllocationCandidate]) -> AllocationResult:
        self.calls += 1
        return AllocationResult(allocations=(), rejections=())


def _repo(tmp_path) -> DriftPilotRepository:
    return DriftPilotRepository.open(tmp_path / "operator.sqlite3", FixedClock(fixed_now=NOW))


def _candidate() -> AllocationCandidate:
    return AllocationCandidate(
        symbol="AAA",
        score=1.0,
        sector="Technology",
        latest_bar_at=NOW,
        rank=1,
    )


def test_state_machine_boots_scans_allocates_and_logs_transitions(tmp_path) -> None:
    repo = _repo(tmp_path)
    allocator = Allocator()
    machine = DriftPilotStateMachine(
        repo,
        DriftPilotSettings(trade_slots=2, slot_value=1_000),
        clock=FixedClock(fixed_now=NOW),
        market_clock=AlwaysOpenClock(),
        scanner=Scanner(ScanResult(spy_bar_at=NOW, candidates=[_candidate()], regime="GREEN")),
        allocator=allocator,
    )

    state = asyncio.run(machine.run_once())

    assert state == OperatorState.IN_POSITION
    assert allocator.calls == 1
    current = repo.state.get()
    assert current is not None
    assert current.current_state == "IN_POSITION"
    assert [slot.status for slot in repo.slots.list_all()] == ["EMPTY", "EMPTY"]
    latest = repo.transitions.latest()
    assert latest is not None
    assert latest.reason == "allocation_complete"


def test_state_machine_market_closed_sets_countdown_state(tmp_path) -> None:
    repo = _repo(tmp_path)
    machine = DriftPilotStateMachine(
        repo,
        DriftPilotSettings(),
        clock=FixedClock(fixed_now=NOW),
        market_clock=AlwaysClosedClock(),
    )

    state = asyncio.run(machine.run_once())

    assert state == OperatorState.MARKET_CLOSED
    current = repo.state.get()
    assert current is not None
    assert current.current_state == "MARKET_CLOSED"
    assert current.metadata is not None
    assert current.metadata["next_open_at"] is not None


def test_state_machine_stale_spy_bar_transitions_to_error(tmp_path) -> None:
    repo = _repo(tmp_path)
    stale_spy = NOW - timedelta(seconds=61)
    machine = DriftPilotStateMachine(
        repo,
        DriftPilotSettings(spy_stale_seconds=60),
        clock=FixedClock(fixed_now=NOW),
        market_clock=AlwaysOpenClock(),
        scanner=Scanner(ScanResult(spy_bar_at=stale_spy, candidates=[], regime="GREEN")),
    )

    state = asyncio.run(machine.run_once())

    assert state == OperatorState.ERROR
    current = repo.state.get()
    assert current is not None
    assert current.current_state == "ERROR"
    assert current.last_error_id is not None
    latest = repo.transitions.latest()
    assert latest is not None
    assert "SPY bar stale" in latest.reason
