from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from driftpilot.clock import DriftPilotClock, datetime_to_storage, require_aware
from driftpilot.execution.slot_allocator import AllocationCandidate, AllocationResult
from driftpilot.settings import DriftPilotSettings
from driftpilot.states import OperatorState
from driftpilot.storage.repositories import DriftPilotRepository, StateTransitionRecord


class BrokerReconciler(Protocol):
    async def reconcile_open_positions(self) -> Any: ...


class ScannerService(Protocol):
    async def scan(self) -> ScanResult: ...


class AllocatorService(Protocol):
    async def allocate(self, candidates: list[AllocationCandidate]) -> AllocationResult: ...


class PositionMonitorService(Protocol):
    async def monitor(self) -> PositionMonitorResult: ...


class MarketClockService(Protocol):
    def session(self, now: datetime | None = None) -> MarketSession: ...


@dataclass(frozen=True, slots=True)
class ScanResult:
    spy_bar_at: datetime | None
    candidates: list[AllocationCandidate] = field(default_factory=list)
    regime: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.spy_bar_at is not None:
            require_aware(self.spy_bar_at)


@dataclass(frozen=True, slots=True)
class PositionMonitorResult:
    open_positions: int = 0
    exit_orders: int = 0
    recycled_slots: int = 0
    halted_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketSession:
    is_open: bool
    reason: str
    next_open_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.next_open_at is not None:
            require_aware(self.next_open_at)


class MarketClock:
    def __init__(self, clock: DriftPilotClock) -> None:
        self.clock = clock

    def session(self, now: datetime | None = None) -> MarketSession:
        et_now = self.clock.to_et(now or self.clock.now_utc())
        if et_now.weekday() >= 5:
            return MarketSession(False, "weekend", self._next_weekday_open(et_now))
        market_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = et_now.replace(hour=16, minute=0, second=0, microsecond=0)
        if et_now < market_open:
            return MarketSession(False, "before_open", market_open)
        if et_now >= market_close:
            return MarketSession(False, "after_close", self._next_weekday_open(et_now + timedelta(days=1)))
        return MarketSession(True, "regular_session")

    def _next_weekday_open(self, value: datetime) -> datetime:
        candidate = value.replace(hour=9, minute=30, second=0, microsecond=0)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        return candidate


class DriftPilotStateMachine:
    def __init__(
        self,
        repository: DriftPilotRepository,
        settings: DriftPilotSettings,
        *,
        clock: DriftPilotClock | None = None,
        market_clock: MarketClockService | None = None,
        broker: BrokerReconciler | None = None,
        scanner: ScannerService | None = None,
        allocator: AllocatorService | None = None,
        position_monitor: PositionMonitorService | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.market_clock = market_clock or MarketClock(self.clock)
        self.broker = broker
        self.scanner = scanner
        self.allocator = allocator
        self.position_monitor = position_monitor
        self._booted = False
        self._error_count = 0

    async def run_once(self) -> OperatorState:
        try:
            if not self._booted:
                await self._boot()
                self._booted = True

            session = self.market_clock.session()
            if not session.is_open:
                await self._transition(
                    OperatorState.MARKET_CLOSED,
                    session.reason,
                    {
                        "next_open_at": datetime_to_storage(session.next_open_at)
                        if session.next_open_at
                        else None
                    },
                )
                return OperatorState.MARKET_CLOSED

            if self.position_monitor is not None:
                monitor_result = await self.position_monitor.monitor()
                if monitor_result.halted_reason == "daily_loss_limit":
                    await self._transition(
                        OperatorState.HALTED_RISK,
                        "daily_loss_limit_hit",
                        monitor_result.metadata,
                    )
                    return OperatorState.HALTED_RISK
                if monitor_result.exit_orders:
                    await self._transition(
                        OperatorState.EXITING,
                        "exit_orders_submitted",
                        monitor_result.metadata,
                    )
                if monitor_result.recycled_slots:
                    await self._transition(
                        OperatorState.RECYCLING,
                        "slots_recycled",
                        monitor_result.metadata,
                    )

            await self._transition(OperatorState.REGIME_CHECK, "market_open")
            scan_result = await self._scan()
            self._require_fresh_spy(scan_result)
            await self._transition(
                OperatorState.SCANNING,
                "scan_complete",
                {
                    "candidate_count": len(scan_result.candidates),
                    "regime": scan_result.regime,
                    **scan_result.metadata,
                },
            )

            if not scan_result.candidates or self.allocator is None:
                await self._transition(
                    OperatorState.IN_POSITION,
                    "no_allocation_work",
                    {"candidate_count": len(scan_result.candidates)},
                )
                return OperatorState.IN_POSITION

            await self._transition(
                OperatorState.ALLOCATING,
                "allocating_ranked_candidates",
                {"candidate_count": len(scan_result.candidates)},
            )
            allocation_result = await self.allocator.allocate(scan_result.candidates)
            await self._transition(
                OperatorState.IN_POSITION,
                "allocation_complete",
                {
                    "allocated": len(allocation_result.allocations),
                    "rejected": len(allocation_result.rejections),
                },
            )
            self._error_count = 0
            return OperatorState.IN_POSITION
        except Exception as exc:
            await self._record_error(exc)
            return OperatorState.ERROR

    async def run_forever(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self.settings.scan_interval_seconds)

    async def _boot(self) -> None:
        await self._transition(OperatorState.BOOT, "boot")
        self._initialize_slots()
        if self.broker is not None:
            result = await self.broker.reconcile_open_positions()
            await self._transition(
                OperatorState.BOOT,
                "broker_reconciled",
                {"result": str(result)},
            )

    def _initialize_slots(self) -> None:
        existing = {slot.slot_id for slot in self.repository.slots.list_all()}
        for slot_id in range(1, self.settings.trade_slots + 1):
            if slot_id not in existing:
                self.repository.slots.upsert(
                    slot_id,
                    status="EMPTY",
                    slot_value=self.settings.slot_value,
                    updated_at=self.clock.now_utc(),
                )

    async def _scan(self) -> ScanResult:
        if self.scanner is None:
            return ScanResult(spy_bar_at=self.clock.now_utc(), candidates=[], regime=None)
        return await self.scanner.scan()

    def _require_fresh_spy(self, scan_result: ScanResult) -> None:
        if scan_result.spy_bar_at is None:
            raise RuntimeError("SPY bar missing, market data stream unhealthy")
        age_seconds = (
            self.clock.now_utc() - scan_result.spy_bar_at.astimezone(self.clock.now_utc().tzinfo)
        ).total_seconds()
        if age_seconds > self.settings.spy_stale_seconds:
            raise RuntimeError(
                f"SPY bar stale, market data stream unhealthy: {age_seconds:.0f}s old"
            )

    async def _transition(
        self,
        to_state: OperatorState,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> StateTransitionRecord:
        current = self.repository.state.get()
        from_state = current.current_state if current is not None else None
        transition = self.repository.transitions.append(
            from_state=from_state,
            to_state=to_state.value,
            reason=reason,
            metadata=metadata,
            timestamp=self.clock.now_utc(),
        )
        self.repository.state.set(
            to_state.value,
            last_transition_id=transition.id,
            metadata=metadata,
            updated_at=transition.timestamp,
        )
        return transition

    async def _record_error(self, exc: Exception) -> None:
        self._error_count += 1
        metadata = {
            "error_type": type(exc).__name__,
            "retry_after_seconds": min(
                self.settings.scan_interval_seconds * self._error_count,
                300,
            ),
        }
        error = self.repository.errors.record(
            severity="ERROR",
            message=str(exc),
            metadata=metadata,
            raised_at=self.clock.now_utc(),
        )
        current = self.repository.state.get()
        transition = self.repository.transitions.append(
            from_state=current.current_state if current is not None else None,
            to_state=OperatorState.ERROR.value,
            reason=str(exc),
            metadata=metadata,
            timestamp=error.raised_at,
        )
        self.repository.state.set(
            OperatorState.ERROR.value,
            last_transition_id=transition.id,
            last_error_id=error.id,
            metadata=metadata,
            updated_at=error.raised_at,
        )
