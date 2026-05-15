from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from driftpilot.clock import DriftPilotClock, datetime_to_storage
from driftpilot.execution.paper_fills import PaperFillEngine
from driftpilot.execution.slot_allocator import AllocationCandidate, AllocationResult, SlotAllocator
from driftpilot.settings import DriftPilotSettings
from driftpilot.state_machine import PositionMonitorResult, ReconciliationResult, ScanResult
from driftpilot.storage.repositories import DriftPilotRepository, PositionRecord

logger = logging.getLogger(__name__)

EOD_DILUTION_START_MINUTE_ET = 15 * 60 + 15
EOD_DILUTION_INTERVAL_MINUTES = 5
EOD_DILUTION_STEP_PCT = 0.10
EOD_DILUTION_MAX_TIGHTEN_PCT = 0.90
EOD_SECTOR_CONCENTRATION_LIMIT = 4


@dataclass(frozen=True, slots=True)
class ExitDecision:
    """An exit decision for one position (before execution)."""

    position: PositionRecord
    exit_reason: str | None  # None = HOLD
    reference_price: float
    overridden_by_agent: bool = False
    agent_action: str | None = None  # e.g. "request_early_cut", "hold"


@dataclass(frozen=True, slots=True)
class UniverseMember:
    symbol: str
    sector: str


class MockBrokerReconciler:
    def __init__(self, repository: DriftPilotRepository, settings: DriftPilotSettings) -> None:
        self.repository = repository
        self.settings = settings

    async def reconcile_open_positions(self) -> ReconciliationResult:
        if self.settings.mode == "paper" and self.repository.positions.list_open():
            return ReconciliationResult(ok=True, status="mock_paper_local_state_preserved")
        status = self.repository.positions.reconcile_broker_open_positions(
            broker_positions=[],
            slot_value=self.settings.slot_value,
            target_pct=self.settings.target_pct,
            stop_pct=self.settings.stop_pct,
            trade_slots=self.settings.trade_slots,
        )
        return ReconciliationResult(ok=True, status=status)


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
        catalyst_db_path: str | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.allocator = SlotAllocator(
            repository,
            settings,
            clock=self.clock,
            catalyst_db_path=catalyst_db_path,
            consecutive_loss_limit=settings.consecutive_loss_limit,
            max_slots_per_sector=settings.max_slots_per_sector,
        )
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

    def decide(self) -> list[ExitDecision]:
        """Phase 1: Compute exit decisions for all open positions.

        Returns a list of ExitDecision objects. Positions where
        exit_reason is None are HOLDs. This does NOT execute any exits.
        """
        now = self.clock.now_utc()
        decisions: list[ExitDecision] = []
        for position in self.repository.positions.list_open():
            exit_reason, reference_price = self._exit_signal(position, now)
            self._update_slot_mark(position, reference_price, now)
            decisions.append(
                ExitDecision(
                    position=position,
                    exit_reason=exit_reason,
                    reference_price=reference_price,
                )
            )
        return decisions

    async def execute(self, decisions: list[ExitDecision]) -> PositionMonitorResult:
        """Phase 2: Execute a list of exit decisions.

        Only decisions where exit_reason is not None will be executed.
        """
        now = self.clock.now_utc()
        recycled = 0
        exits = 0
        realized_today = 0.0

        for decision in decisions:
            if decision.exit_reason is None:
                continue
            position = decision.position
            exit_reason = decision.exit_reason
            reference_price = decision.reference_price

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
                metadata={
                    "exit_reason": exit_reason,
                    "overridden_by_agent": decision.overridden_by_agent,
                    "agent_action": decision.agent_action,
                },
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
                self.repository.slots.free_slot(
                    position.slot_id,
                    slot_value=self.settings.slot_value,
                    reason=exit_reason,
                    last_symbol=position.symbol,
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

    async def monitor(self) -> PositionMonitorResult:
        """Backward-compatible: decide then execute in one call."""
        decisions = self.decide()
        return await self.execute(decisions)

    async def apply_eod_dilution(self) -> PositionMonitorResult:
        now = self.clock.now_utc()
        positions = self.repository.positions.list_open()
        if not positions:
            return PositionMonitorResult(
                open_positions=0,
                metadata={
                    "source": "eod_dilution",
                    "checked_at": datetime_to_storage(now),
                    "active": False,
                    "reason": "no_open_positions",
                },
            )

        step_count = _eod_dilution_step_count(self.clock.to_et(now))
        if step_count <= 0:
            return PositionMonitorResult(
                open_positions=len(positions),
                metadata={
                    "source": "eod_dilution",
                    "checked_at": datetime_to_storage(now),
                    "active": False,
                    "reason": "before_1515_et",
                },
            )

        snapshots = [_position_eod_snapshot(position) for position in positions]
        median_unrealized_pct = _median([snapshot.unrealized_pct for snapshot in snapshots])
        tightened = 0
        for snapshot in snapshots:
            new_stop = _tightened_eod_stop_price(
                current_price=snapshot.current_price,
                current_stop=snapshot.position.stop_price,
                step_count=step_count,
                lock_to_bid=snapshot.unrealized_pct > median_unrealized_pct,
            )
            if new_stop <= snapshot.position.stop_price:
                continue
            self.repository.positions.update_stop_price(
                snapshot.position.id,
                stop_price=new_stop,
                metadata={
                    "eod_dilution_active": True,
                    "eod_dilution_step_count": step_count,
                    "eod_dilution_stop_price": new_stop,
                    "eod_dilution_reference_price": snapshot.current_price,
                    "eod_dilution_unrealized_pct": snapshot.unrealized_pct,
                    "eod_dilution_median_unrealized_pct": median_unrealized_pct,
                },
            )
            tightened += 1

        sector_exit_decisions = [
            ExitDecision(
                position=snapshot.position,
                exit_reason="EOD_SECTOR_DILUTION",
                reference_price=snapshot.current_price,
            )
            for snapshot in _least_profitable_concentrated_sector_positions(snapshots)
        ]
        sector_result = await self.execute(sector_exit_decisions) if sector_exit_decisions else PositionMonitorResult()
        return PositionMonitorResult(
            open_positions=len(self.repository.positions.list_open()),
            exit_orders=sector_result.exit_orders,
            recycled_slots=sector_result.recycled_slots,
            halted_reason=sector_result.halted_reason,
            metadata={
                "source": "eod_dilution",
                "checked_at": datetime_to_storage(now),
                "active": True,
                "step_count": step_count,
                "tightened": tightened,
                "median_unrealized_pct": median_unrealized_pct,
                "sector_exits": sector_result.exit_orders,
            },
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

    async def eod_liquidate_all(self) -> int:
        """End-of-day: close every open position at last known price and free slots.

        Paper mode equivalent — no broker orders, just local DB updates.
        """
        positions = self.repository.positions.list_open()
        if not positions:
            return 0
        now = self.clock.now_utc()
        closed = 0
        for position in positions:
            metadata = position.metadata or {}
            exit_price = float(metadata.get("current_price", position.entry_price))
            realized = (exit_price - position.entry_price) * position.quantity
            self.repository.positions.close(
                position.id,
                exit_reason="eod_liquidation",
                realized_pnl=realized,
                closed_at=now,
                metadata={"exit_price": exit_price, "exit_close_path": "eod_liquidation"},
            )
            if position.slot_id is not None:
                self.repository.slots.free_slot(
                    position.slot_id,
                    slot_value=self.settings.slot_value,
                    reason="eod_liquidation",
                    last_symbol=position.symbol,
                    updated_at=now,
                )
            closed += 1
            logger.info(
                "[EOD] paper close %s entry=$%.2f exit=$%.2f realized=$%.2f slot=%s",
                position.symbol, position.entry_price, exit_price, realized, position.slot_id,
            )
        logger.info("[EOD] liquidation complete: %d positions closed", closed)
        return closed

    async def final_drain_all(self) -> PositionMonitorResult:
        decisions = [
            ExitDecision(
                position=position,
                exit_reason="FINAL_DRAIN",
                reference_price=float((position.metadata or {}).get("current_price", position.entry_price)),
            )
            for position in self.repository.positions.list_open()
        ]
        result = await self.execute(decisions)
        return PositionMonitorResult(
            open_positions=result.open_positions,
            exit_orders=result.exit_orders,
            recycled_slots=result.recycled_slots,
            halted_reason=result.halted_reason,
            metadata={
                **(result.metadata or {}),
                "source": "final_drain",
                "reason": "after_1550_et",
            },
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


@dataclass(frozen=True, slots=True)
class EodPositionSnapshot:
    position: PositionRecord
    current_price: float
    unrealized_pct: float
    sector: str


def _eod_dilution_step_count(now_et: datetime) -> int:
    minutes = now_et.hour * 60 + now_et.minute
    if minutes < EOD_DILUTION_START_MINUTE_ET:
        return 0
    return ((minutes - EOD_DILUTION_START_MINUTE_ET) // EOD_DILUTION_INTERVAL_MINUTES) + 1


def _tightened_eod_stop_price(
    *,
    current_price: float,
    current_stop: float,
    step_count: int,
    lock_to_bid: bool,
) -> float:
    if current_price <= 0 or step_count <= 0:
        return current_stop
    if lock_to_bid:
        return round(max(current_stop, current_price), 4)
    if current_price <= current_stop:
        return current_stop
    tighten_pct = min(step_count * EOD_DILUTION_STEP_PCT, EOD_DILUTION_MAX_TIGHTEN_PCT)
    remaining_distance = (current_price - current_stop) * (1.0 - tighten_pct)
    return round(max(current_stop, current_price - remaining_distance), 4)


def _position_eod_snapshot(position: PositionRecord) -> EodPositionSnapshot:
    metadata = position.metadata or {}
    current_price = float(
        metadata.get("bid_price")
        or metadata.get("current_price")
        or metadata.get("reference_price")
        or position.entry_price
    )
    unrealized_pct = (
        ((current_price - position.entry_price) / position.entry_price) * 100.0
        if position.entry_price > 0
        else 0.0
    )
    sector = str(metadata.get("sector") or "Unknown")
    return EodPositionSnapshot(position, current_price, unrealized_pct, sector)


def _median(values: Iterable[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _least_profitable_concentrated_sector_positions(
    snapshots: list[EodPositionSnapshot],
) -> list[EodPositionSnapshot]:
    by_sector: dict[str, list[EodPositionSnapshot]] = {}
    for snapshot in snapshots:
        by_sector.setdefault(snapshot.sector, []).append(snapshot)
    selected: list[EodPositionSnapshot] = []
    for sector_snapshots in by_sector.values():
        if len(sector_snapshots) >= EOD_SECTOR_CONCENTRATION_LIMIT:
            selected.append(min(sector_snapshots, key=lambda snapshot: snapshot.unrealized_pct))
    return selected
