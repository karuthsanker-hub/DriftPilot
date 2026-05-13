from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from driftpilot.catalyst.universe_filter import CatalystUniverseFilter
from driftpilot.clock import DriftPilotClock, datetime_to_storage, require_aware
from driftpilot.execution.slot_allocator import AllocationCandidate, AllocationResult
from driftpilot.settings import DriftPilotSettings
from driftpilot.states import OperatorState
from driftpilot.storage.repositories import DriftPilotRepository, StateTransitionRecord

if TYPE_CHECKING:
    from driftpilot.agents.orchestrator import AgentOrchestrator

logger = logging.getLogger(__name__)


class CatalystEventBusProtocol(Protocol):
    async def subscribe(
        self,
        category: str | None,
        subcategory: str | None,
        callback: Any,
    ) -> str: ...


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
        catalyst_event_bus: CatalystEventBusProtocol | None = None,
        catalyst_universe_filter: CatalystUniverseFilter | None = None,
        orchestrator: AgentOrchestrator | None = None,
        market_adapter: Any | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.market_clock = market_clock or MarketClock(self.clock)
        self.broker = broker
        self.scanner = scanner
        self.allocator = allocator
        self.position_monitor = position_monitor
        self.catalyst_event_bus = catalyst_event_bus
        self.catalyst_universe_filter = catalyst_universe_filter
        self.orchestrator = orchestrator
        self.market_adapter = market_adapter
        # TODO(operator-runtime): the SCANNING-state scanner pipeline currently
        # encapsulates symbol selection inside `ScannerService.scan()`. The
        # operator runtime should construct the scanner with this same
        # `catalyst_universe_filter` so that the technical signals
        # (apex_hunter, rs_drift, whale_tail, stationary_ghost) receive the
        # filtered+ranked universe BEFORE invoking their `scan(symbol_bars=...)`
        # entry points. The catalyst signals (earnings_report_v1,
        # analyst_target_raise_v1) MUST NOT be passed through this filter --
        # they read candidates from the catalyst event bus, not from a
        # universe scan. See `apply_universe_filter()` below for the hook used
        # by tests and runtime wiring.
        self._booted = False
        self._error_count = 0
        self._catalyst_subscription_id: str | None = None
        # TODO(operator-runtime): if `catalyst_event_bus` is None at construction
        # time, the operator runtime should call `await self.subscribe_to_catalyst_bus(bus)`
        # at startup so that analyst/target_cut events drive EMERGENCY_FLUSH.

    def apply_universe_filter(self, symbols: list[str]) -> list[str]:
        """Apply the catalyst universe filter to a list of symbols.

        Hook used by the SCANNING state (and the operator-runtime scanner
        wiring) to filter+rank the universe seen by the FOUR technical
        signals (apex_hunter, rs_drift, whale_tail, stationary_ghost).

        IMPORTANT: do NOT call this for catalyst signals
        (earnings_report_v1, analyst_target_raise_v1). Those signals receive
        their candidates from the catalyst event bus and must not be
        subjected to the universe filter.
        """
        if self.catalyst_universe_filter is None:
            return symbols
        return self.catalyst_universe_filter.filter_and_rank(symbols)

    async def subscribe_to_catalyst_bus(
        self, bus: CatalystEventBusProtocol | None = None
    ) -> None:
        """Wire this state machine to a CatalystEventBus.

        Subscribes to analyst/target_cut and routes the event through
        `on_analyst_target_cut`. Safe to call once at startup; subsequent calls
        are no-ops.
        """
        bus = bus or self.catalyst_event_bus
        if bus is None or self._catalyst_subscription_id is not None:
            return
        self.catalyst_event_bus = bus
        self._catalyst_subscription_id = await bus.subscribe(
            "analyst", "target_cut", self.on_analyst_target_cut
        )

    async def on_analyst_target_cut(self, event: Any) -> OperatorState | None:
        """Handle an `analyst/target_cut` catalyst event.

        If any open slot is on the event symbol, transition the state machine
        into EMERGENCY_FLUSH and trigger the flush pipeline. Returns the new
        state value (or None if no action was taken).
        """
        symbol = getattr(event, "symbol", "").upper()
        if not symbol:
            return None
        slots = self.repository.slots.list_all()
        affected = [
            slot for slot in slots
            if slot.symbol is not None and slot.symbol.upper() == symbol
        ]
        if not affected:
            return None
        await self._transition(
            OperatorState.EMERGENCY_FLUSH,
            "analyst_target_cut",
            {
                "symbol": symbol,
                "affected_slots": [s.slot_id for s in affected],
                "headline": getattr(event, "headline", None),
            },
        )
        await self.emergency_flush()
        return OperatorState.EMERGENCY_FLUSH

    async def emergency_flush(self) -> None:
        """Cancel open orders and trigger market exits on all open positions.

        Minimal implementation: delegates exit work to the existing position
        monitor / EXITING handler by recording the transition. Real venue-side
        order cancellation is the responsibility of the broker layer; this
        method is the documented entry point for that wiring.
        """
        # Delegate the actual exit work to the existing EXITING flow. The
        # state machine's normal `run_once` will pick up open positions on the
        # next bar and submit market exits via the position monitor. After the
        # cooldown timer in the operator runtime, transition to RECYCLING.
        await self._transition(
            OperatorState.EXITING,
            "emergency_flush_exit",
            {"source": "emergency_flush"},
        )

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

            # ── Agent tick: portfolio-level oversight ──
            self._tick_agents_pm()

            if self.position_monitor is not None:
                # Use decide/execute split when orchestrator is active
                if self.orchestrator is not None and self.orchestrator.running and hasattr(self.position_monitor, "decide"):
                    logger.info("[AGENT] decide/execute path ACTIVE — agents will intercept exit decisions")
                    decisions = self.position_monitor.decide()
                    logger.info(
                        "[AGENT] algo decisions: %s",
                        [(d.position.symbol, d.exit_reason or "HOLD", f"slot={d.position.slot_id}") for d in decisions],
                    )
                    decisions = self._agent_intercept_exits(decisions)
                    logger.info(
                        "[AGENT] post-intercept decisions: %s",
                        [(d.position.symbol, d.exit_reason or "HOLD", f"override={d.overridden_by_agent}") for d in decisions],
                    )
                    monitor_result = await self.position_monitor.execute(decisions)
                else:
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

            # ── Agent tick: scanner entry approval ──
            self._tick_agents_scanner(scan_result)

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
            # Build per-reason rejection counts for dashboard diagnostics.
            rejection_reasons: dict[str, int] = {}
            for rej in allocation_result.rejections:
                rejection_reasons[rej.reason] = rejection_reasons.get(rej.reason, 0) + 1
            await self._transition(
                OperatorState.IN_POSITION,
                "allocation_complete",
                {
                    "allocated": len(allocation_result.allocations),
                    "rejected": len(allocation_result.rejections),
                    "rejection_reasons": rejection_reasons,
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
        now = self.clock.now_utc()
        existing = {slot.slot_id: slot for slot in self.repository.slots.list_all()}
        for slot_id in range(1, self.settings.trade_slots + 1):
            if slot_id not in existing:
                self.repository.slots.upsert(
                    slot_id,
                    status="EMPTY",
                    slot_value=self.settings.slot_value,
                    updated_at=now,
                )

        # Clean stale RESERVED slots from previous operator runs.
        # A slot stays RESERVED only while the allocator holds the lock
        # and the broker fills the order — normally <60s. If it's still
        # RESERVED after 10 minutes, the previous operator died mid-fill.
        from datetime import timedelta

        stale_cutoff = now - timedelta(minutes=10)
        recycled = 0
        for slot in existing.values():
            if slot.status == "RESERVED" and slot.updated_at < stale_cutoff:
                self.repository.slots.upsert(
                    slot.slot_id,
                    status="EMPTY",
                    symbol=None,
                    slot_value=slot.slot_value,
                    metadata={},
                    updated_at=now,
                )
                recycled += 1
        if recycled:
            logger.info(
                "[BOOT] recycled %d stale RESERVED slots (older than 10min)",
                recycled,
            )

    # ── Agent bridge helpers (observe-only, Wave 1) ────────────────
    def _tick_agents_pm(self) -> None:
        """Portfolio-level oversight tick. No-op if orchestrator is None."""
        if self.orchestrator is None:
            return
        try:
            from driftpilot.agents.state_machine_bridge import tick_pm_from_repo

            n = tick_pm_from_repo(self.orchestrator, self.repository, self.settings)
            if n:
                logger.debug("agent PM tick processed %d messages", n)
        except Exception:
            logger.exception("agent PM tick failed (non-fatal)")

    def _agent_intercept_exits(self, decisions: list) -> list:
        """Let slot agents observe and potentially override exit decisions.

        Each ExitDecision is passed to the slot agent for that position.
        The agent can:
        - Agree with the algo (no change)
        - Request an early cut (flip HOLD → EXIT) — subject to guardrail
        - Veto an exit (flip EXIT → HOLD) — subject to override rate limit

        Returns the (possibly modified) decision list.
        """
        if self.orchestrator is None or not self.orchestrator.running:
            return decisions

        try:
            from driftpilot.agents.state_machine_bridge import tick_slots_from_positions
            from driftpilot.services import ExitDecision

            # Build exit_decisions map for the bridge
            positions = [d.position for d in decisions]
            exit_map: dict[int, tuple[str | None, float]] = {}
            for d in decisions:
                exit_map[d.position.id] = (d.exit_reason, d.reference_price)

            # Run agents — they log opinions and return verdicts
            agent_results = tick_slots_from_positions(
                self.orchestrator, positions, exit_map, self.settings,
                market_adapter=self.market_adapter,
            )

            if agent_results:
                logger.info("agent slot verdicts: %s", agent_results)

            # Check override rate before applying any agent overrides
            override_rate = self.orchestrator.get_override_rate()
            max_rate = self.orchestrator._config.max_override_rate
            rate_exceeded = override_rate >= max_rate

            if rate_exceeded:
                logger.warning(
                    "agent override rate %.1f%% >= limit %.1f%%, blocking new overrides",
                    override_rate * 100, max_rate * 100,
                )

            # Apply agent overrides to decisions (Phase 2 — active overrides)
            new_decisions = []
            for d in decisions:
                slot_id = d.position.slot_id
                agent_action = agent_results.get(slot_id) if slot_id is not None else None

                if agent_action is None:
                    new_decisions.append(d)
                    continue

                # Agent wants early cut but algo says HOLD
                if rate_exceeded:
                    new_decisions.append(d)  # rate exceeded, don't override
                    continue

                if agent_action == "request_early_cut" and d.exit_reason is None:
                    new_decisions.append(
                        ExitDecision(
                            position=d.position,
                            exit_reason="AGENT_CUT",
                            reference_price=d.reference_price,
                            overridden_by_agent=True,
                            agent_action=agent_action,
                        )
                    )
                    logger.info(
                        "agent override: slot %d %s HOLD→EXIT (early cut)",
                        slot_id, d.position.symbol,
                    )
                # Agent wants to hold but algo says EXIT — agent vetoes
                elif agent_action == "hold" and d.exit_reason is not None:
                    # Only non-mechanical exits can be vetoed (not TIME/STOP)
                    if d.exit_reason not in ("TIME", "STOP"):
                        new_decisions.append(
                            ExitDecision(
                                position=d.position,
                                exit_reason=None,  # vetoed → HOLD
                                reference_price=d.reference_price,
                                overridden_by_agent=True,
                                agent_action=agent_action,
                            )
                        )
                        logger.info(
                            "agent override: slot %d %s EXIT→HOLD (agent veto)",
                            slot_id, d.position.symbol,
                        )
                    else:
                        new_decisions.append(d)  # mechanical exits can't be vetoed
                else:
                    new_decisions.append(d)

            return new_decisions
        except Exception:
            logger.exception("agent exit intercept failed (non-fatal, using original decisions)")
            return decisions

    def _tick_agents_scanner(self, scan_result: ScanResult) -> None:
        """Scanner entry-approval tick. Lets agents weigh in on candidates."""
        if self.orchestrator is None:
            return
        if not scan_result.candidates:
            return
        try:
            from driftpilot.agents.state_machine_bridge import (
                tick_scanner_from_candidates,
            )

            n = tick_scanner_from_candidates(
                self.orchestrator,
                scan_result.candidates,
                scan_result.regime,
                scan_result.metadata,
            )
            if n:
                logger.debug("agent scanner tick requested %d entries", n)
        except Exception:
            logger.exception("agent scanner tick failed (non-fatal)")

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
