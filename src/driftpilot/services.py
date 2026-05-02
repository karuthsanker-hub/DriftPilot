from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from driftpilot.clock import DriftPilotClock, datetime_to_storage
from driftpilot.execution.paper_fills import PaperFillEngine
from driftpilot.execution.slot_allocator import AllocationCandidate, AllocationResult, SlotAllocator
from driftpilot.settings import DriftPilotSettings
from driftpilot.state_machine import PositionMonitorResult, ScanResult
from driftpilot.storage.repositories import DriftPilotRepository, PositionRecord


@dataclass(frozen=True, slots=True)
class UniverseMember:
    symbol: str
    sector: str


class MockBrokerReconciler:
    def __init__(self, repository: DriftPilotRepository, settings: DriftPilotSettings) -> None:
        self.repository = repository
        self.settings = settings

    async def reconcile_open_positions(self) -> str:
        if self.settings.mode == "paper" and self.repository.positions.list_open():
            return "mock_paper_local_state_preserved"
        return self.repository.positions.reconcile_broker_open_positions(
            broker_positions=[],
            slot_value=self.settings.slot_value,
            target_pct=self.settings.target_pct,
            stop_pct=self.settings.stop_pct,
            trade_slots=self.settings.trade_slots,
        )


class SyntheticScannerService:
    def __init__(
        self,
        repository: DriftPilotRepository,
        settings: DriftPilotSettings,
        *,
        clock: DriftPilotClock | None = None,
        universe_file: str | Path | None = None,
        queue_limit: int = 100,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.universe = _load_universe(Path(universe_file or settings.universe_file))
        self.queue_limit = queue_limit

    async def scan(self) -> ScanResult:
        now = self.clock.now_utc()
        stream = self.repository.stream_state.get("mock_stream")
        cycle = stream.shard_cursor + 1
        selected = _rotating_window(self.universe, start=cycle - 1, size=min(self.queue_limit, len(self.universe)))
        candidates: list[AllocationCandidate] = []
        for rank, member in enumerate(selected, start=1):
            score = max(0.1, 5.0 - rank * 0.035)
            rvol = 2.0 + (rank % 9) * 0.17
            return_15m = 0.005 + (rank % 7) * 0.0011
            vwap_distance = 0.003 + (rank % 5) * 0.0013
            price = 25.0 + ((cycle + rank) % 180) * 1.15
            status = "queued"
            blocked_reason = None
            self.repository.upsert_candidate_queue_row(
                symbol=member.symbol,
                score=score,
                rvol=rvol,
                vwap_distance_pct=vwap_distance,
                return_15m_pct=return_15m,
                sector=member.sector,
                blocked_reason=blocked_reason,
                queue_status=status,
                cycle_at=now,
            )
            candidates.append(
                AllocationCandidate(
                    symbol=member.symbol,
                    score=score,
                    sector=member.sector,
                    latest_bar_at=now,
                    rank=rank,
                    metadata={
                        "reference_price": price,
                        "rvol": rvol,
                        "return_15m_pct": return_15m,
                        "vwap_distance_pct": vwap_distance,
                        "cycle": cycle,
                    },
                )
            )
        self.repository.stream_state.set_cursor(
            "mock_stream",
            cycle,
            metadata={"symbols_scanned": len(selected), "feed": "synthetic"},
            updated_at=now,
        )
        self.repository.clear_candidate_queue(before=now)
        return ScanResult(
            spy_bar_at=now,
            candidates=candidates,
            regime="GREEN",
            metadata={"symbols_scanned": len(selected), "feed": "synthetic", "cycle": cycle},
        )


class PaperExecutionAllocator:
    def __init__(
        self,
        repository: DriftPilotRepository,
        settings: DriftPilotSettings,
        *,
        clock: DriftPilotClock | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.allocator = SlotAllocator(repository, settings, clock=self.clock)
        self.fills = PaperFillEngine(repository, settings, clock=self.clock)

    async def allocate(self, candidates: list[AllocationCandidate]) -> AllocationResult:
        result = await self.allocator.allocate(candidates)
        candidate_by_symbol = {candidate.symbol.upper(): candidate for candidate in candidates}
        for allocation in result.allocations:
            candidate = candidate_by_symbol[allocation.symbol.upper()]
            reference_price = float(candidate.metadata.get("reference_price", 100.0))
            quantity = max(1, int(allocation.slot_value // reference_price))
            order = self.repository.orders.create(
                symbol=allocation.symbol,
                side="buy",
                order_type="paper_marketable_limit",
                status="filled",
                quantity=quantity,
                slot_id=allocation.slot_id,
                limit_price=round(reference_price * 1.002, 4),
                metadata={"source": "mock_stream", "allocation_rank": allocation.rank},
                submitted_at=allocation.reserved_at,
            )
            applied = await self.fills.apply_entry(
                symbol=allocation.symbol,
                quantity=quantity,
                reference_price=reference_price,
                order_id=order.id,
                filled_at=allocation.reserved_at,
                metadata={"slot_id": allocation.slot_id, "sector": allocation.sector},
            )
            entry_price = applied.fill.price
            position = self.repository.positions.create_open(
                symbol=allocation.symbol,
                quantity=quantity,
                entry_price=entry_price,
                target_price=entry_price * (1 + self.settings.target_pct),
                stop_price=entry_price * (1 - self.settings.stop_pct),
                slot_id=allocation.slot_id,
                opened_at=allocation.reserved_at,
                metadata={
                    "sector": allocation.sector,
                    "entry_slippage": applied.fill.slippage,
                    "reference_price": reference_price,
                    "current_price": entry_price,
                    "score": allocation.score,
                    "rank": allocation.rank,
                    "scenario": "TARGET" if (allocation.rank or 1) % 3 != 0 else "STOP",
                },
            )
            self.repository.orders.update_status(order.id, status="filled", metadata={"position_id": position.id})
            self.repository.slots.upsert(
                allocation.slot_id,
                status="OPEN",
                symbol=allocation.symbol,
                position_id=position.id,
                slot_value=allocation.slot_value,
                metadata={
                    "sector": allocation.sector,
                    "entry_price": entry_price,
                    "current_price": entry_price,
                    "slippage": applied.fill.slippage,
                    "score": allocation.score,
                    "rank": allocation.rank,
                },
                updated_at=allocation.reserved_at,
            )
            self.repository.increment_daily_counter(
                date_et=self.clock.date_et(allocation.reserved_at),
                counter_name="trades",
            )
            self.repository.increment_daily_counter(
                date_et=self.clock.date_et(allocation.reserved_at),
                counter_name=f"symbol:{allocation.symbol.upper()}:trades",
            )
        return result


class PaperPositionMonitor:
    def __init__(
        self,
        repository: DriftPilotRepository,
        settings: DriftPilotSettings,
        *,
        clock: DriftPilotClock | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.fills = PaperFillEngine(repository, settings, clock=self.clock)

    async def monitor(self) -> PositionMonitorResult:
        now = self.clock.now_utc()
        recycled = 0
        exits = 0
        realized_today = 0.0
        for position in self.repository.positions.list_open():
            exit_reason, reference_price = self._exit_signal(position, now)
            self._update_slot_mark(position, reference_price, now)
            if exit_reason is None:
                continue
            exits += 1
            order = self.repository.orders.create(
                symbol=position.symbol,
                side="sell",
                order_type="paper_marketable_limit",
                status="filled",
                quantity=position.quantity,
                position_id=position.id,
                slot_id=position.slot_id,
                limit_price=round(reference_price * 0.998, 4),
                metadata={"exit_reason": exit_reason},
                submitted_at=now,
            )
            applied = await self.fills.apply_exit(
                symbol=position.symbol,
                quantity=position.quantity,
                reference_price=reference_price,
                current_quantity=position.quantity,
                order_id=order.id,
                filled_at=now,
                metadata={"exit_reason": exit_reason, "slot_id": position.slot_id},
            )
            realized_pnl = (applied.fill.price - position.entry_price) * position.quantity
            realized_today += realized_pnl
            self.repository.positions.close(
                position.id,
                exit_reason=exit_reason,
                realized_pnl=realized_pnl,
                closed_at=now,
                metadata={"exit_price": applied.fill.price, "exit_slippage": applied.fill.slippage},
            )
            if position.slot_id is not None:
                self.repository.slots.upsert(
                    position.slot_id,
                    status="EMPTY",
                    symbol=None,
                    position_id=None,
                    slot_value=self.settings.slot_value,
                    metadata={"empty_reason": "Awaiting candidate", "last_exit_reason": exit_reason},
                    updated_at=now,
                )
                self.repository.record_recycle_event(
                    slot_id=position.slot_id,
                    freed_symbol=position.symbol,
                    exit_reason=exit_reason,
                    exit_pnl_pct=realized_pnl / max(position.entry_price * position.quantity, 1),
                    replacement_symbol=None,
                    at=now,
                )
                recycled += 1
        halted = None
        if realized_today <= -(self.settings.paper_capital * self.settings.daily_loss_limit_pct):
            halted = "daily_loss_limit"
        return PositionMonitorResult(
            open_positions=len(self.repository.positions.list_open()),
            exit_orders=exits,
            recycled_slots=recycled,
            halted_reason=halted,
            metadata={"realized_pnl": realized_today, "checked_at": datetime_to_storage(now)},
        )

    def _exit_signal(self, position: PositionRecord, now: datetime) -> tuple[str | None, float]:
        age_minutes = (now - position.opened_at).total_seconds() / 60
        metadata = position.metadata or {}
        scenario = str(metadata.get("scenario", "TARGET"))
        if age_minutes >= self.settings.max_hold_minutes:
            return "TIME", float(metadata.get("current_price", position.entry_price))
        if age_minutes >= 1 and scenario == "TARGET":
            return "TARGET", position.target_price
        if age_minutes >= 1 and scenario == "STOP":
            return "STOP", position.stop_price
        return None, float(metadata.get("current_price", position.entry_price))

    def _update_slot_mark(self, position: PositionRecord, reference_price: float, now: datetime) -> None:
        if position.slot_id is None:
            return
        metadata = {**(position.metadata or {}), "current_price": reference_price}
        self.repository.slots.upsert(
            position.slot_id,
            status="OPEN",
            symbol=position.symbol,
            position_id=position.id,
            slot_value=self.settings.slot_value,
            metadata=metadata,
            updated_at=now,
        )


def _load_universe(path: Path) -> list[UniverseMember]:
    if not path.exists():
        return [
            UniverseMember("AAPL", "Information Technology"),
            UniverseMember("MSFT", "Information Technology"),
            UniverseMember("NVDA", "Information Technology"),
            UniverseMember("UNH", "Health Care"),
            UniverseMember("XOM", "Energy"),
            UniverseMember("COST", "Consumer Staples"),
            UniverseMember("CAT", "Industrials"),
            UniverseMember("JPM", "Financials"),
        ]
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        members = [
            UniverseMember(
                symbol=str(row.get("symbol") or row.get("Symbol") or "").upper(),
                sector=str(row.get("sector") or row.get("Sector") or "Unknown"),
            )
            for row in reader
        ]
    return [member for member in members if member.symbol]


def _rotating_window(items: list[UniverseMember], *, start: int, size: int) -> list[UniverseMember]:
    if not items:
        return []
    return [items[(start + offset) % len(items)] for offset in range(size)]
