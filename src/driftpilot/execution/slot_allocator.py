from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from driftpilot.clock import DriftPilotClock, datetime_to_storage, require_aware
from driftpilot.settings import DriftPilotSettings
from driftpilot.states import BlockedReason
from driftpilot.storage.repositories import DriftPilotRepository, SlotRecord


def _has_negative_catalyst(
    db_path: str | None, symbol: str, lookback_minutes: int = 240
) -> bool:
    """Returns True if a negative analyst catalyst (target_cut) exists for `symbol`
    within the last `lookback_minutes`. Uses parameterized SQL.
    """
    if not db_path:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT 1 FROM catalyst_events WHERE symbol = ? AND category = ? "
            "AND subcategory = ? AND event_ts >= ? LIMIT 1",
            (symbol, "analyst", "target_cut", cutoff),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


FREE_SLOT_STATUSES = {"EMPTY", "RECYCLING"}
# OCCUPIED was a legacy status string written by reconcile_broker_open_positions.
# Keep it in the active set so any stale rows on disk still gate duplicates
# until they're rewritten as OPEN.
ACTIVE_SLOT_STATUSES = {"RESERVED", "ENTERING", "OPEN", "EXITING", "OCCUPIED"}
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
        catalyst_db_path: str | None = None,
        catalyst_lookback_minutes: int = 240,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.max_slots_per_sector = max_slots_per_sector
        self.catalyst_db_path = catalyst_db_path
        self.catalyst_lookback_minutes = catalyst_lookback_minutes
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
            # Day-cap: count today's trades per symbol (open + closed) so
            # the same symbol doesn't re-buy after exit on the same news.
            symbol_day_counts = self._closed_today_symbol_counts()
            for sym in active_symbols:
                symbol_day_counts[sym] = symbol_day_counts.get(sym, 0) + 1
            sector_counts = self._active_sector_counts(slots)
            allocations: list[SlotAllocation] = []
            rejections: list[AllocationRejection] = []

            for candidate in _ranked(candidates):
                symbol = candidate.symbol.upper()

                if self.catalyst_db_path and _has_negative_catalyst(
                    self.catalyst_db_path,
                    symbol,
                    lookback_minutes=self.catalyst_lookback_minutes,
                ):
                    detail = {
                        "category": "analyst",
                        "subcategory": "target_cut",
                        "lookback_minutes": self.catalyst_lookback_minutes,
                    }
                    self._mark_candidate_blocked(
                        symbol, BlockedReason.CATALYST_NEGATIVE.value, detail, now
                    )
                    rejections.append(
                        AllocationRejection(
                            symbol, BlockedReason.CATALYST_NEGATIVE.value, detail
                        )
                    )
                    continue

                stale_seconds = (now - candidate.latest_bar_at.astimezone(now.tzinfo)).total_seconds()
                if stale_seconds > self.settings.scan_interval_seconds * 2:
                    self._mark_candidate_blocked(
                        symbol,
                        "stale_bar",
                        {
                            "age_seconds": stale_seconds,
                            "latest_bar_at": datetime_to_storage(candidate.latest_bar_at),
                        },
                        now,
                    )
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

                day_count = symbol_day_counts.get(symbol, 0)
                if day_count >= self.settings.max_trades_per_symbol_per_day:
                    rejections.append(AllocationRejection(
                        symbol, "max_trades_per_symbol_per_day_reached",
                        {"day_count": day_count, "cap": self.settings.max_trades_per_symbol_per_day},
                    ))
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

    def _closed_today_symbol_counts(self) -> dict[str, int]:
        """Count today's positions per symbol — both closed AND still-open,
        anything that opened today.

        Why count opens too: the broker-race path could create a local
        position record while the slot got transiently released (slot link
        bugs in Alpaca-paper). If we only count closed, an in-flight ICHR
        re-buy slips through. Counting "opened_at >= today" catches both.
        """
        from datetime import datetime, timezone
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        counts: dict[str, int] = {}
        try:
            cur = self.repository.connection.execute(
                "SELECT symbol, COUNT(*) FROM positions "
                "WHERE opened_at >= ? GROUP BY symbol",
                (today_iso,),
            )
            for row in cur.fetchall():
                sym = (row[0] or "").upper()
                if sym:
                    counts[sym] = int(row[1] or 0)
        except Exception:
            pass  # best-effort — fall back to active-only check
        return counts

    def _persist_allocator_state(self, status: str, timestamp: datetime, metadata: dict[str, Any]) -> None:
        allocator_state = getattr(self.repository, "allocator_state", None)
        if allocator_state is None:
            return
        set_state = getattr(allocator_state, "set", None)
        if set_state is None:
            return
        set_state(
            status=status,
            locked_at=timestamp if status == "LOCKED" else None,
            updated_at=timestamp,
            metadata=metadata,
        )

    def _mark_candidate_blocked(
        self,
        symbol: str,
        reason: str,
        detail: dict[str, Any],
        timestamp: datetime,
    ) -> None:
        candidate_queue = getattr(self.repository, "candidate_queue", None)
        if candidate_queue is None:
            return
        mark_blocked = getattr(candidate_queue, "mark_blocked", None)
        if mark_blocked is None:
            return
        mark_blocked(symbol, reason=reason, features=detail, updated_at=timestamp)


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
