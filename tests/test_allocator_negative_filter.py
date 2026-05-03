from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from driftpilot.catalyst.db import init_catalyst_schema, insert_event
from driftpilot.catalyst.event import CatalystEvent
from driftpilot.clock import FixedClock
from driftpilot.execution.slot_allocator import AllocationCandidate, SlotAllocator
from driftpilot.settings import DriftPilotSettings
from driftpilot.states import BlockedReason
from driftpilot.storage.repositories import DriftPilotRepository


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _settings() -> DriftPilotSettings:
    return DriftPilotSettings(scan_interval_seconds=30)


def _repo(tmp_path, *, slot_count: int = 5) -> DriftPilotRepository:
    repo = DriftPilotRepository.open(
        tmp_path / "operator.sqlite3", FixedClock(fixed_now=_now())
    )
    for slot_id in range(1, slot_count + 1):
        repo.slots.upsert(slot_id, status="EMPTY", slot_value=1_000.0)
    return repo


def _candidate(symbol: str, *, sector: str = "Technology", rank: int = 1) -> AllocationCandidate:
    return AllocationCandidate(
        symbol=symbol,
        score=100.0 - rank,
        sector=sector,
        latest_bar_at=_now() - timedelta(seconds=5),
        rank=rank,
    )


def _make_event(symbol: str, *, ts: datetime, headline: str = "Target cut") -> CatalystEvent:
    h = hashlib.sha256(f"{symbol}-{headline}-{ts.isoformat()}".encode()).hexdigest()
    return CatalystEvent(
        symbol=symbol,
        category="analyst",
        subcategory="target_cut",
        pillar="micro",
        ts=ts,
        headline=headline,
        source="test",
        horizon_minutes=240,
        headline_hash=h,
        sentiment="negative",
        priority_modifier=0.0,
    )


@pytest.fixture
def catalyst_db(tmp_path):
    p = str(tmp_path / "catalyst.db")
    init_catalyst_schema(p)
    return p


@pytest.mark.asyncio
async def test_blocks_when_target_cut_recent(tmp_path, catalyst_db) -> None:
    insert_event(catalyst_db, _make_event("AAPL", ts=_now() - timedelta(minutes=30)))
    repo = _repo(tmp_path)
    allocator = SlotAllocator(
        repo,
        _settings(),
        clock=FixedClock(fixed_now=_now()),
        catalyst_db_path=catalyst_db,
    )

    result = await allocator.allocate([_candidate("AAPL")])

    assert len(result.allocations) == 0
    assert len(result.rejections) == 1
    assert result.rejections[0].reason == BlockedReason.CATALYST_NEGATIVE.value
    assert result.rejections[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_allows_when_target_cut_old(tmp_path, catalyst_db) -> None:
    insert_event(catalyst_db, _make_event("AAPL", ts=_now() - timedelta(minutes=300)))
    repo = _repo(tmp_path)
    allocator = SlotAllocator(
        repo,
        _settings(),
        clock=FixedClock(fixed_now=_now()),
        catalyst_db_path=catalyst_db,
    )

    result = await allocator.allocate([_candidate("AAPL")])

    assert len(result.allocations) == 1
    assert result.allocations[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_allows_when_no_target_cut(tmp_path, catalyst_db) -> None:
    repo = _repo(tmp_path)
    allocator = SlotAllocator(
        repo,
        _settings(),
        clock=FixedClock(fixed_now=_now()),
        catalyst_db_path=catalyst_db,
    )

    result = await allocator.allocate([_candidate("AAPL")])

    assert len(result.allocations) == 1
    assert result.allocations[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_allows_when_db_path_none(tmp_path) -> None:
    repo = _repo(tmp_path)
    allocator = SlotAllocator(
        repo,
        _settings(),
        clock=FixedClock(fixed_now=_now()),
        catalyst_db_path=None,
    )

    result = await allocator.allocate([_candidate("AAPL")])

    assert len(result.allocations) == 1
    assert result.allocations[0].symbol == "AAPL"
