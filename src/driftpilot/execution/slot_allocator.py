from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from driftpilot.clock import DriftPilotClock, datetime_to_storage, require_aware
from driftpilot.settings import DriftPilotSettings
from driftpilot.states import BlockedReason
from driftpilot.storage.repositories import DriftPilotRepository, SlotRecord

logger = logging.getLogger(__name__)


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


DEFAULT_CONSECUTIVE_LOSS_LIMIT = 2

FREE_SLOT_STATUSES = {"EMPTY", "RECYCLING", "AVAILABLE"}
# OCCUPIED was a legacy status string written by reconcile_broker_open_positions.
# "AVAILABLE" was another legacy status that old reconcile code wrote (lowercase
# "available" normalised to "AVAILABLE" by _slot_status()). Both are kept so
# any stale rows on disk are treated correctly until rewritten.
ACTIVE_SLOT_STATUSES = {"RESERVED", "ENTERING", "OPEN", "EXITING", "OCCUPIED"}
# Sector cap disabled by default — this is a day-trading system riding
# intraday momentum, not a long-term portfolio balancer.  If 5 tech stocks
# have signal, we should trade all 5.  Set via ALLOCATOR_MAX_SLOTS_PER_SECTOR
# env var or constructor arg to re-enable.
DEFAULT_MAX_SLOTS_PER_SECTOR = 0  # 0 = disabled


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
        consecutive_loss_limit: int = DEFAULT_CONSECUTIVE_LOSS_LIMIT,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.max_slots_per_sector = max_slots_per_sector
        self.catalyst_db_path = catalyst_db_path
        self.catalyst_lookback_minutes = catalyst_lookback_minutes
        self.consecutive_loss_limit = consecutive_loss_limit
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

            ranked_candidates = _ranked(candidates)
            if ranked_candidates:
                logger.info(
                    "[ALLOCATOR] evaluating %d candidates (sorted by score) | "
                    "free_slots: %d | active_symbols: %d | sector_cap: %s",
                    len(ranked_candidates), len(free_slots), len(active_symbols),
                    self.max_slots_per_sector if self.max_slots_per_sector > 0 else "disabled",
                )
                for i, c in enumerate(ranked_candidates[:10]):  # log top 10
                    logger.info(
                        "[ALLOCATOR]   #%d %s score=%.3f sector=%s rank=%s",
                        i + 1, c.symbol, c.score, c.sector, c.rank,
                    )

            for candidate in ranked_candidates:
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

                # Consecutive-loss cooldown: if the last N closed trades on
                # this symbol were ALL losses, skip it.  The catalyst may be
                # "fresh" but the price action is clearly against us.
                if self.consecutive_loss_limit > 0:
                    consec = self._consecutive_losses_today(symbol)
                    if consec >= self.consecutive_loss_limit:
                        rejections.append(AllocationRejection(
                            symbol, "consecutive_loss_cooldown",
                            {"consecutive_losses": consec, "limit": self.consecutive_loss_limit},
                        ))
                        continue

                # Defect #5 fix: machine-gun re-entry cooldown.
                # Prevent re-entering a symbol within N minutes of the last exit.
                reentry_wait = self._minutes_since_last_exit(symbol)
                min_reentry = self._min_reentry_minutes()
                if reentry_wait is not None and reentry_wait < min_reentry:
                    rejections.append(AllocationRejection(
                        symbol, "reentry_cooldown",
                        {"minutes_since_exit": round(reentry_wait, 1),
                         "min_reentry_minutes": min_reentry},
                    ))
                    continue

                sector = candidate.sector
                if self.max_slots_per_sector > 0 and sector_counts.get(sector, 0) >= self.max_slots_per_sector:
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

            reason_counts = _reason_counts(rejections)
            self._persist_allocator_state(
                "IDLE",
                now,
                {
                    "allocated": len(allocations),
                    "rejected": len(rejections),
                    "reasons": reason_counts,
                },
            )

            # Enhanced logging for sustainability diagnostics
            if allocations:
                logger.info(
                    "[ALLOCATOR] %d allocated, %d rejected | reasons: %s | free_slots_remaining: %d",
                    len(allocations), len(rejections), reason_counts, len(free_slots),
                )
            elif rejections:
                logger.warning(
                    "[ALLOCATOR] 0 allocated from %d candidates! All %d rejected | reasons: %s | "
                    "free_slots: %d | active_symbols: %d | day_caps_hit: %s",
                    len(candidates), len(rejections), reason_counts, len(free_slots),
                    len(active_symbols),
                    {s: c for s, c in symbol_day_counts.items() if c >= self.settings.max_trades_per_symbol_per_day},
                )
            elif not candidates:
                logger.info("[ALLOCATOR] no candidates to allocate (signal pool empty)")

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
        connection = getattr(self.repository, "connection", None)
        if connection is None:
            return counts
        try:
            cur = connection.execute(
                "SELECT symbol, COUNT(*) FROM positions "
                "WHERE opened_at >= ? GROUP BY symbol",
                (today_iso,),
            )
            for row in cur.fetchall():
                sym = (row[0] or "").upper()
                if sym:
                    counts[sym] = int(row[1] or 0)
        except Exception:
            # Best-effort fallback: allocator still has the active-slot duplicate gate.
            return counts
        return counts

    def _consecutive_losses_today(self, symbol: str) -> int:
        """Count how many of the most-recent closed trades on *symbol* today
        were losses (realized_pnl <= 0), reading backwards from the newest.

        Returns 0 if the most-recent closed trade was a winner, or if
        there are no closed trades today for this symbol.
        """
        connection = getattr(self.repository, "connection", None)
        if connection is None:
            return 0
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            rows = connection.execute(
                "SELECT realized_pnl FROM positions "
                "WHERE symbol = ? AND opened_at >= ? AND status = 'closed' "
                "ORDER BY closed_at DESC",
                (symbol, today_iso),
            ).fetchall()
        except Exception:
            return 0
        streak = 0
        for (pnl,) in rows:
            if pnl is not None and pnl <= 0:
                streak += 1
            else:
                break  # most-recent winner breaks the streak
        return streak

    def _min_reentry_minutes(self) -> float:
        """Read min_reentry_minutes from runtime_config.json (hot-reloadable)."""
        try:
            from driftpilot.runtime_config import load_runtime_config
            rcfg = load_runtime_config()
            val = getattr(rcfg, "min_reentry_minutes", None)
            if val is not None:
                return float(val)
        except Exception:
            pass
        return 15.0  # safe default

    def _minutes_since_last_exit(self, symbol: str) -> float | None:
        """Minutes since the most recent closed position on *symbol* today.

        Returns ``None`` if there are no closed positions today for this symbol.
        """
        connection = getattr(self.repository, "connection", None)
        if connection is None:
            return None
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            row = connection.execute(
                "SELECT closed_at FROM positions "
                "WHERE symbol = ? AND status = 'closed' AND closed_at >= ? "
                "ORDER BY closed_at DESC LIMIT 1",
                (symbol, today_iso),
            ).fetchone()
        except Exception:
            return None
        if row is None or row[0] is None:
            return None
        try:
            closed_at = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
            if closed_at.tzinfo is None:
                closed_at = closed_at.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - closed_at).total_seconds() / 60.0
        except Exception:
            return None

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
    """Sort candidates by signal quality — highest score wins the slot.

    For intraday momentum, signal strength (score) is the primary sort key.
    Rank is a secondary tiebreaker only (e.g. two candidates with identical
    scores).  Symbol is the final tiebreaker for determinism.
    """
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate.score,
            candidate.rank if candidate.rank is not None else len(candidates) + 1,
            candidate.symbol,
        ),
    )


def _reason_counts(rejections: list[AllocationRejection]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rejection in rejections:
        counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
    return counts
