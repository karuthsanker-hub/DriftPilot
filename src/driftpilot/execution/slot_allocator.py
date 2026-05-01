from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from driftpilot.clock import DriftPilotClock, datetime_to_storage, require_aware
from driftpilot.settings import DriftPilotSettings
from driftpilot.storage.repositories import DriftPilotRepository, SlotRecord


FREE_SLOT_STATUSES = {"EMPTY", "AVAILABLE", "RECYCLING"}
ACTIVE_SLOT_STATUSES = {"RESERVED", "ENTERING", "OPEN", "EXITING"}
DEFAULT_MAX_SLOTS_PER_SECTOR = 3


class SlotRepositoryProtocol(Protocol):
    def list_all(self) -> list[SlotRecord]: ...

    def upsert(
        self,
        slot_id: int,
        *,
        status: str,
        slot_value: float,
        symbol: str | None = None,
        position_id: int | None = None,
        reserved_order_id: int | None = None,
        metadata: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> SlotRecord: ...


class SlotStoreProtocol(Protocol):
    slots: SlotRepositoryProtocol


@dataclass(frozen=True, slots=True)
class AllocationCandidate:
    symbol: str
    score: float
    sector: str
    latest_bar_at: datetime
    rank: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_aware(self.latest_bar_at)


@dataclass(frozen=True, slots=True)
class SlotAllocation:
    slot_id: int
    symbol: str
    sector: str
    slot_value: float
    reserved_at: datetime
    score: float
    rank: int | None = None


@dataclass(frozen=True, slots=True)
class AllocationRejection:
    symbol: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AllocationResult:
    allocations: tuple[SlotAllocation, ...]
    rejections: tuple[AllocationRejection, ...]


class SlotAllocator:
    def __init__(
        self,
        repository: DriftPilotRepository | SlotStoreProtocol,
        settings: DriftPilotSettings,
        *,
        clock: DriftPilotClock | None = None,
        max_slots_per_sector: int = DEFAULT_MAX_SLOTS_PER_SECTOR,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.max_slots_per_sector = max_slots_per_sector
        self._lock = asyncio.Lock()

    async def allocate(self, candidates: list[AllocationCandidate]) -> AllocationResult:
        async with self._lock:
            now = self.clock.now_utc()
            self._persist_allocator_state("LOCKED", now, {"candidate_count": len(candidates)})
            slots = self.repository.slots.list_all()
            free_slots = [slot for slot in slots if _slot_status(slot) in FREE_SLOT_STATUSES]
            free_slots.sort(key=lambda slot: slot.slot_id)

            active_symbols = {
                slot.symbol.upper()
                for slot in slots
                if slot.symbol is not None and _slot_status(slot) in ACTIVE_SLOT_STATUSES
            }
            sector_counts = self._active_sector_counts(slots)
            allocations: list[SlotAllocation] = []
            rejections: list[AllocationRejection] = []

            for candidate in _ranked(candidates):
                symbol = candidate.symbol.upper()
                stale_seconds = (now - candidate.latest_bar_at.astimezone(now.tzinfo)).total_seconds()
                if stale_seconds > self.settings.scan_interval_seconds * 2:
                    rejections.append(
                        AllocationRejection(
                            symbol,
                            "stale_bar",
                            {
                                "age_seconds": stale_seconds,
                                "latest_bar_at": datetime_to_storage(candidate.latest_bar_at),
                            },
                        )
                    )
                    continue

                if symbol in active_symbols:
                    rejections.append(AllocationRejection(symbol, "duplicate_symbol"))
                    continue

                sector = candidate.sector
                if sector_counts.get(sector, 0) >= self.max_slots_per_sector:
                    rejections.append(AllocationRejection(symbol, "sector_cap_reached", {"sector": sector}))
                    continue

                if not free_slots:
                    rejections.append(AllocationRejection(symbol, "no_free_slot"))
                    continue

                slot = free_slots.pop(0)
                reserved = self.repository.slots.upsert(
                    slot.slot_id,
                    status="RESERVED",
                    symbol=symbol,
                    slot_value=slot.slot_value,
                    metadata={
                        **(slot.metadata or {}),
                        "sector": sector,
                        "score": candidate.score,
                        "rank": candidate.rank,
                        "reserved_at": datetime_to_storage(now),
                        "candidate": candidate.metadata,
                    },
                    updated_at=now,
                )
                active_symbols.add(symbol)
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
                allocations.append(
                    SlotAllocation(
                        slot_id=reserved.slot_id,
                        symbol=symbol,
                        sector=sector,
                        slot_value=reserved.slot_value,
                        reserved_at=now,
                        score=candidate.score,
                        rank=candidate.rank,
                    )
                )

            self._persist_allocator_state(
                "IDLE",
                now,
                {
                    "allocated": len(allocations),
                    "rejected": len(rejections),
                    "reasons": _reason_counts(rejections),
                },
            )
            return AllocationResult(tuple(allocations), tuple(rejections))

    def _active_sector_counts(self, slots: list[SlotRecord]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for slot in slots:
            if _slot_status(slot) not in ACTIVE_SLOT_STATUSES:
                continue
            sector = (slot.metadata or {}).get("sector")
            if isinstance(sector, str) and sector:
                counts[sector] = counts.get(sector, 0) + 1
        return counts

    def _persist_allocator_state(self, status: str, timestamp: datetime, metadata: dict[str, Any]) -> None:
        allocator_state = getattr(self.repository, "allocator_state", None)
        if allocator_state is None:
            return
        set_state = getattr(allocator_state, "set", None)
        if set_state is None:
            return
        set_state(status=status, updated_at=timestamp, metadata=metadata)


def _slot_status(slot: SlotRecord) -> str:
    return slot.status.upper()


def _ranked(candidates: list[AllocationCandidate]) -> list[AllocationCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.rank if candidate.rank is not None else len(candidates) + 1,
            -candidate.score,
            candidate.symbol,
        ),
    )


def _reason_counts(rejections: list[AllocationRejection]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rejection in rejections:
        counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
    return counts
