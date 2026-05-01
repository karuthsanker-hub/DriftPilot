from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from driftpilot.clock import FixedClock
from driftpilot.execution.paper_fills import (
    PaperFill,
    PaperFillEngine,
    entry_fill,
    exit_fill,
    slippage_for_price,
)
from driftpilot.execution.slot_allocator import AllocationCandidate, SlotAllocator
from driftpilot.settings import DriftPilotSettings
from driftpilot.storage.repositories import DriftPilotRepository


NOW = datetime(2026, 4, 30, 14, 30, tzinfo=UTC)


class RecordedFills:
    def __init__(self) -> None:
        self.records: list[PaperFill] = []

    def record(self, fill: PaperFill) -> None:
        self.records.append(fill)


class FillRepository:
    def __init__(self) -> None:
        self.fills = RecordedFills()


def _settings() -> DriftPilotSettings:
    return DriftPilotSettings(scan_interval_seconds=30)


def _repo(tmp_path, *, slot_count: int = 10) -> DriftPilotRepository:
    repo = DriftPilotRepository.open(tmp_path / "operator.sqlite3", FixedClock(fixed_now=NOW))
    for slot_id in range(1, slot_count + 1):
        repo.slots.upsert(slot_id, status="EMPTY", slot_value=1_000.0)
    return repo


def _candidate(symbol: str, *, sector: str = "Technology", rank: int = 1) -> AllocationCandidate:
    return AllocationCandidate(
        symbol=symbol,
        score=100.0 - rank,
        sector=sector,
        latest_bar_at=NOW - timedelta(seconds=5),
        rank=rank,
    )


def test_allocator_lock_prevents_duplicate_allocation_with_two_simultaneous_frees(tmp_path) -> None:
    async def run() -> None:
        repo = _repo(tmp_path, slot_count=2)
        allocator = SlotAllocator(repo, _settings(), clock=FixedClock(fixed_now=NOW))
        candidates = [_candidate("AAA", rank=1), _candidate("BBB", rank=2)]

        first, second = await asyncio.gather(allocator.allocate(candidates), allocator.allocate(candidates))

        allocations = [*first.allocations, *second.allocations]
        assert len(allocations) == 2
        assert {allocation.symbol for allocation in allocations} == {"AAA", "BBB"}
        assert len({allocation.slot_id for allocation in allocations}) == 2
        assert all(slot.status == "RESERVED" for slot in repo.slots.list_all())

    asyncio.run(run())


def test_allocator_sector_cap_limits_five_same_sector_candidates_to_three(tmp_path) -> None:
    async def run() -> None:
        repo = _repo(tmp_path)
        allocator = SlotAllocator(repo, _settings(), clock=FixedClock(fixed_now=NOW))
        candidates = [_candidate(f"T{i}", sector="Technology", rank=i) for i in range(1, 6)]

        result = await allocator.allocate(candidates)

        assert len(result.allocations) == 3
        assert [allocation.symbol for allocation in result.allocations] == ["T1", "T2", "T3"]
        assert [rejection.reason for rejection in result.rejections] == [
            "sector_cap_reached",
            "sector_cap_reached",
        ]

    asyncio.run(run())


def test_allocator_rejects_stale_candidates_and_duplicate_symbols(tmp_path) -> None:
    async def run() -> None:
        repo = _repo(tmp_path)
        repo.slots.upsert(
            1,
            status="OPEN",
            symbol="OPEN",
            slot_value=1_000.0,
            metadata={"sector": "Technology"},
        )
        allocator = SlotAllocator(repo, _settings(), clock=FixedClock(fixed_now=NOW))
        stale = AllocationCandidate(
            symbol="OLD",
            score=99.0,
            sector="Healthcare",
            latest_bar_at=NOW - timedelta(seconds=61),
            rank=1,
        )

        result = await allocator.allocate([stale, _candidate("OPEN", rank=2), _candidate("FRESH", rank=3)])

        assert [rejection.reason for rejection in result.rejections] == ["stale_bar", "duplicate_symbol"]
        assert [allocation.symbol for allocation in result.allocations] == ["FRESH"]
        allocator_state = repo.allocator_state.get()
        assert allocator_state is not None
        assert allocator_state.status == "IDLE"
        assert allocator_state.metadata is not None
        assert allocator_state.metadata["reasons"]["stale_bar"] == 1
        assert repo.candidate_queue.blocked_reason("OLD") == "stale_bar"

    asyncio.run(run())


def test_paper_fill_slippage_formula_entry_exit_quantity_and_persistence() -> None:
    repository = FillRepository()
    engine = PaperFillEngine(repository, _settings(), clock=FixedClock(fixed_now=NOW))

    assert slippage_for_price(10.0) == 0.02
    assert slippage_for_price(100.0) == 0.05

    direct_entry = entry_fill(symbol="abc", quantity=1, reference_price=100.0, filled_at=NOW)
    direct_exit = exit_fill(symbol="abc", quantity=1, reference_price=100.0, filled_at=NOW)

    async def run() -> None:
        entry = await engine.apply_entry(symbol="abc", quantity=10, reference_price=100.0, current_quantity=5)
        exit_ = await engine.apply_exit(symbol="abc", quantity=4, reference_price=100.0, current_quantity=15)

        assert entry.resulting_quantity == 15
        assert exit_.resulting_quantity == 11
        assert entry.fill.price == direct_entry.price
        assert exit_.fill.price == direct_exit.price
        assert entry.fill.metadata["slippage"] == 0.05
        assert exit_.fill.metadata["slippage"] == 0.05

    asyncio.run(run())

    assert [fill.side for fill in repository.fills.records] == ["buy", "sell"]
    assert [fill.metadata["slippage_formula"] for fill in repository.fills.records] == [
        "max(0.02,0.0005*price)",
        "max(0.02,0.0005*price)",
    ]


def test_paper_fill_persists_to_sqlite_repository(tmp_path) -> None:
    repo = _repo(tmp_path)
    engine = PaperFillEngine(repo, _settings(), clock=FixedClock(fixed_now=NOW))

    async def run() -> None:
        await engine.apply_entry(symbol="abc", quantity=3, reference_price=20.0)
        await engine.apply_exit(symbol="abc", quantity=2, reference_price=20.0, current_quantity=3)

    asyncio.run(run())

    fills = repo.fills.list_all()
    assert [fill.side for fill in fills] == ["buy", "sell"]
    assert [fill.symbol for fill in fills] == ["ABC", "ABC"]
    assert all(fill.metadata is not None for fill in fills)
    assert [fill.metadata["slippage"] for fill in fills if fill.metadata is not None] == [0.02, 0.02]
