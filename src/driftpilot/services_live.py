"""Live (Alpaca paper) execution allocator + position monitor.

Drop-in replacements for PaperExecutionAllocator + PaperPositionMonitor
that submit real orders to Alpaca's paper broker. Used when the operator
runs with `--paper-live`.

Key differences vs the mock services:
  - allocator.allocate() calls AlpacaBrokerClient.submit_entry_order which
    submits a marketable limit, waits for fill, and falls back to
    cancel-and-recycle on timeout.
  - position_monitor periodically polls the latest quote (REST), computes
    unrealized_pct, and asks the signal's evaluate_exit; if it says close,
    submit_exit_order is called.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from driftpilot.broker.alpaca_client import AlpacaBrokerClient, OrderSubmissionResult
from driftpilot.clock import DriftPilotClock
from driftpilot.execution.slot_allocator import (
    AllocationCandidate,
    AllocationResult,
    SlotAllocator,
)
from driftpilot.market_data.rest_quotes import AlpacaRestQuoteProvider
from driftpilot.settings import DriftPilotSettings
from driftpilot.signals import get_signal
from driftpilot.signals.earnings_report_v1 import (
    EarningsReportConfig,
    EarningsReportSignal,
)
from driftpilot.storage.repositories import DriftPilotRepository

logger = logging.getLogger(__name__)


# Use the production ScanResult so the state machine's REGIME_CHECK
# (which reads spy_bar_at) is happy.
def _build_scan_result(candidates, now):
    from driftpilot.state_machine import ScanResult
    return ScanResult(
        spy_bar_at=now,
        candidates=candidates,
        regime="catalyst_event_driven",
        metadata={"source": "catalyst_scanner", "n_candidates": len(candidates)},
    )


class LiveBrokerReconciler:
    """Adapter that exposes BrokerReconciler protocol over AlpacaBrokerClient.

    The state machine's BOOT state calls broker.reconcile_open_positions()
    which AlpacaBrokerClient does not implement directly. This adapter
    queries Alpaca for open positions and reconciles them into the local
    operator state DB, matching the shape of MockBrokerReconciler.
    """

    def __init__(
        self,
        alpaca: AlpacaBrokerClient,
        repository: DriftPilotRepository,
        settings: DriftPilotSettings,
    ) -> None:
        self.alpaca = alpaca
        self.repository = repository
        self.settings = settings

    async def reconcile_open_positions(self) -> str:
        try:
            broker_positions = await self.alpaca.get_open_positions()
        except Exception as exc:
            logger.warning("alpaca reconcile fetch failed: %s — assuming no open positions", exc)
            broker_positions = []

        # Translate alpaca BrokerPosition → list of dicts the repo expects
        # (or empty list — mock-equivalent for "no open positions").
        try:
            return self.repository.positions.reconcile_broker_open_positions(
                broker_positions=[
                    {
                        "symbol": getattr(p, "symbol", "").upper(),
                        "quantity": float(getattr(p, "quantity", 0)),
                        "avg_entry_price": float(getattr(p, "avg_entry_price", 0)),
                    }
                    for p in broker_positions
                ],
                slot_value=self.settings.slot_value,
                target_pct=self.settings.target_pct,
                stop_pct=self.settings.stop_pct,
                trade_slots=self.settings.trade_slots,
            )
        except Exception as exc:
            logger.warning("repo reconcile failed: %s — continuing with no-op", exc)
            return "live_reconcile_noop"

    # Pass-through for any other broker calls the state machine might make
    def __getattr__(self, name):
        return getattr(self.alpaca, name)


class CatalystScannerService:
    """Scanner that emits AllocationCandidates from a catalyst signal's bus.

    Each cycle:
      1. Calls signal.scan(now) to get catalyst Candidates from active events
      2. Translates each to AllocationCandidate with a reference_price from
         the live REST quote (skips if no quote available — broker will
         reject anyway)
      3. Carries the catalyst event chain (sentiment, headline_hash, headline,
         event_age_minutes) through metadata so the live allocator records it
         on the position for forensic audit

    No bars, no synthetic state — purely event-driven.
    """

    def __init__(
        self,
        signal: EarningsReportSignal,
        quote_provider: AlpacaRestQuoteProvider,
        clock: DriftPilotClock,
    ) -> None:
        self.signal = signal
        self.quote_provider = quote_provider
        self.clock = clock

    async def scan(self):
        now = self.clock.now_utc()
        candidates: list[AllocationCandidate] = []
        try:
            sig_candidates = await self.signal.scan(now=now)
        except Exception as exc:
            logger.exception("catalyst scanner: signal.scan raised: %s", exc)
            return _build_scan_result([], now)

        for rank, sc in enumerate(sig_candidates, start=1):
            quote = await asyncio.to_thread(self.quote_provider.latest_quote, sc.symbol)
            if quote is None:
                logger.info(
                    "catalyst scanner: no live quote for %s — skipping (broker would reject)",
                    sc.symbol,
                )
                continue
            ref_price = (quote.bid_price + quote.ask_price) / 2.0
            features = sc.features or {}
            cat_ts_val = features.get("catalyst_event_ts")
            cat_ts_str = (
                cat_ts_val.isoformat() if hasattr(cat_ts_val, "isoformat") else cat_ts_val
            )
            ac = AllocationCandidate(
                symbol=sc.symbol,
                score=float(sc.score),
                sector=sc.sector or "Unknown",
                latest_bar_at=now,
                rank=rank,
                metadata={
                    "reference_price": ref_price,
                    "catalyst_event_ts": cat_ts_str,
                    "headline": features.get("headline"),
                    "headline_hash": features.get("headline_hash"),
                    "sentiment": features.get("sentiment"),
                    "event_age_minutes": features.get("event_age_minutes"),
                    "horizon_minutes": features.get("horizon_minutes"),
                    "source": features.get("source", "catalyst_bus"),
                },
            )
            candidates.append(ac)
            logger.info(
                "CANDIDATE %s rank=%d score=%+.2f sentiment=%s age=%.1fmin ref=%.2f | %s",
                ac.symbol, rank, ac.score,
                features.get("sentiment") or "NONE",
                float(features.get("event_age_minutes") or 0),
                ref_price,
                (features.get("headline") or "")[:80],
            )

        if not candidates:
            logger.info("catalyst scanner: 0 candidates this cycle (no admitted events)")
        return _build_scan_result(candidates, now)


class LiveAlpacaAllocator:
    """Allocator that submits real Alpaca paper orders."""

    def __init__(
        self,
        repository: DriftPilotRepository,
        settings: DriftPilotSettings,
        broker: AlpacaBrokerClient,
        *,
        clock: DriftPilotClock | None = None,
        catalyst_db_path: str | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.broker = broker
        self.clock = clock or DriftPilotClock(settings.timezone)
        self.allocator = SlotAllocator(
            repository, settings, clock=self.clock, catalyst_db_path=catalyst_db_path
        )

    async def allocate(self, candidates: list[AllocationCandidate]) -> AllocationResult:
        result = await self.allocator.allocate(candidates)
        candidate_by_symbol = {c.symbol.upper(): c for c in candidates}

        for allocation in result.allocations:
            candidate = candidate_by_symbol[allocation.symbol.upper()]
            reference_price = float(candidate.metadata.get("reference_price", 100.0))
            quantity = max(1, int(allocation.slot_value // reference_price))

            # Catalyst-event audit fields — passed through from the candidate
            # so we can post-hoc join trade rows back to the triggering event.
            cat_event_ts = candidate.metadata.get("catalyst_event_ts")
            cat_headline = (candidate.metadata.get("headline") or "")[:200]
            cat_headline_hash = candidate.metadata.get("headline_hash")
            cat_sentiment = candidate.metadata.get("sentiment")
            cat_event_age = candidate.metadata.get("event_age_minutes")

            logger.info(
                "LIVE: submitting paper buy %s qty=%d slot=%s ref_price=%.2f "
                "catalyst_sentiment=%s catalyst_event_age=%.1fmin catalyst_hash=%s | %s",
                allocation.symbol, quantity, allocation.slot_id, reference_price,
                cat_sentiment or "NONE",
                float(cat_event_age) if cat_event_age is not None else -1.0,
                cat_headline_hash or "NONE",
                cat_headline,
            )

            try:
                submission = await self.broker.submit_entry_order(
                    symbol=allocation.symbol,
                    quantity=quantity,
                    slot_id=allocation.slot_id,
                )
            except Exception as exc:
                logger.exception("LIVE: entry submission failed for %s: %s", allocation.symbol, exc)
                continue

            # Reconcile with Alpaca's actual position state. The broker's
            # _wait_for_fill can race-lose to a fast fill: order submitted →
            # immediately filled before our poll caught up → wait_for_fill
            # times out and cancel returns "order already in filled state".
            # Treat that as a successful fill if Alpaca actually has the position.
            if not submission.submitted:
                try:
                    open_positions = await self.broker.get_open_positions()
                except Exception:
                    open_positions = []
                actual = next(
                    (p for p in open_positions if p.symbol.upper() == allocation.symbol.upper()),
                    None,
                )
                if actual is not None:
                    logger.warning(
                        "LIVE: broker reported submitted=False (reason=%s) but alpaca shows "
                        "real position %s qty=%s entry=%.2f — treating as filled",
                        submission.reason, actual.symbol, actual.quantity,
                        float(actual.average_entry_price),
                    )
                    submission = OrderSubmissionResult(
                        submitted=True,
                        broker_order_id=submission.broker_order_id or actual.broker_position_id,
                        symbol=actual.symbol,
                        side="buy",
                        quantity=float(actual.quantity),
                        order_type="reconciled",
                        limit_price=float(actual.average_entry_price),
                        reason="reconciled_from_alpaca_position",
                    )
                else:
                    logger.warning(
                        "LIVE: entry not submitted for %s — reason=%s",
                        allocation.symbol, submission.reason,
                    )
                    continue

            # Order submitted AND filled (broker waited). Record the position
            # locally so the monitor can track it for exit.
            entry_price = submission.limit_price or reference_price
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
                    "broker_order_id": submission.broker_order_id,
                    "reference_price": reference_price,
                    "current_price": entry_price,
                    "entry_ts": allocation.reserved_at.isoformat(),
                    "entry_price": entry_price,
                    "source": "live_alpaca_paper",
                    # Catalyst event chain — for forensic analysis
                    "catalyst_event_ts": (
                        cat_event_ts.isoformat() if hasattr(cat_event_ts, "isoformat") else cat_event_ts
                    ),
                    "catalyst_headline_hash": cat_headline_hash,
                    "catalyst_sentiment": cat_sentiment,
                    "catalyst_headline": cat_headline,
                    "catalyst_event_age_min_at_entry": cat_event_age,
                    "signal_score_at_entry": float(getattr(candidate, "score", 0.0)),
                },
            )
            logger.info(
                "LIVE: position opened symbol=%s qty=%d entry=%.2f position_id=%s broker_order_id=%s",
                allocation.symbol, quantity, entry_price,
                getattr(position, "id", None), submission.broker_order_id,
            )

        return result


class LiveAlpacaPositionMonitor:
    """Polls latest quotes for open positions, evaluates the signal's exit
    decision, and submits real Alpaca exits when triggered.

    Designed for catalyst signals that need only current quote (not bar
    history). For technical signals that need bar history, the SIP stream
    integration is a separate sprint.
    """

    def __init__(
        self,
        repository: DriftPilotRepository,
        settings: DriftPilotSettings,
        broker: AlpacaBrokerClient,
        quote_provider: AlpacaRestQuoteProvider,
        *,
        clock: DriftPilotClock | None = None,
        signal=None,  # injected from operator startup; bypasses registry
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.broker = broker
        self.quote_provider = quote_provider
        self.clock = clock or DriftPilotClock(settings.timezone)
        self._signal = signal

    async def monitor(self):
        """State-machine-protocol entrypoint. Returns PositionMonitorResult."""
        from driftpilot.state_machine import PositionMonitorResult
        exits = await self.monitor_open_positions()
        positions = self.repository.positions.list_open()
        return PositionMonitorResult(
            open_positions=len(positions),
            exit_orders=exits,
            recycled_slots=0,
            halted_reason=None,
            metadata={"source": "live_alpaca_paper"},
        )

    def _get_signal(self):
        if self._signal is not None:
            return self._signal
        return get_signal(self.settings.active_signal)

    async def monitor_open_positions(self) -> int:
        """One sweep: for each open position, check exit and submit if so.
        Returns the number of exit orders submitted."""
        signal = self._get_signal()
        positions = self.repository.positions.list_open()
        now = self.clock.now_utc()
        exit_count = 0

        for position in positions:
            symbol = position.symbol.upper()
            quote = await asyncio.to_thread(self.quote_provider.latest_quote, symbol)
            if quote is None:
                logger.debug("monitor: no quote for %s — skipping", symbol)
                continue

            mid = (quote.bid_price + quote.ask_price) / 2.0
            entry_price = float(getattr(position, "entry_price", 0.0))
            unrealized_pct = ((mid - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

            # Set current_price on the position object so the signal sees fresh data
            try:
                position_metadata = dict(getattr(position, "metadata", {}) or {})
            except Exception:
                position_metadata = {}
            position_metadata["current_price"] = mid
            # In-memory shim — repository persistence is best-effort for live mode
            object.__setattr__(position, "current_price", mid) if hasattr(position, "current_price") else None

            try:
                decision = signal.evaluate_exit(position, now)
                if inspect.isawaitable(decision):
                    decision = await decision
            except Exception as exc:
                logger.exception("monitor: evaluate_exit raised for %s: %s", symbol, exc)
                continue

            if decision is None or not getattr(decision, "close", False):
                continue

            quantity = float(getattr(position, "quantity", 0))
            if quantity <= 0:
                continue

            logger.info(
                "LIVE: signal requests exit symbol=%s reason=%s unrealized=%.3f%%",
                symbol, getattr(decision, "reason", "unknown"), unrealized_pct,
            )
            try:
                exit_result = await self.broker.submit_exit_order(
                    symbol=symbol,
                    quantity=quantity,
                    position_id=getattr(position, "id", None),
                )
                if exit_result.submitted:
                    exit_price = exit_result.limit_price or mid
                    realized = (exit_price - entry_price) * quantity
                    self.repository.positions.close(
                        position_id=getattr(position, "id"),
                        exit_reason=getattr(decision, "reason", "signal_exit"),
                        realized_pnl=realized,
                        closed_at=self.clock.now_utc(),
                        metadata={
                            "exit_price": exit_price,
                            "broker_exit_order_id": exit_result.broker_order_id,
                        },
                    )
                    exit_count += 1
                    logger.info(
                        "LIVE: position closed symbol=%s broker_order_id=%s",
                        symbol, exit_result.broker_order_id,
                    )
            except Exception as exc:
                logger.exception("LIVE: exit submission failed for %s: %s", symbol, exc)

        return exit_count


def build_live_components(
    repository: DriftPilotRepository,
    settings: DriftPilotSettings,
    *,
    clock: DriftPilotClock | None = None,
    catalyst_db_path: str | None = None,
) -> tuple[AlpacaBrokerClient, LiveAlpacaAllocator, LiveAlpacaPositionMonitor]:
    """Construct the live trio: broker, allocator, position monitor."""
    if not settings.alpaca_key_id or not settings.alpaca_secret_key:
        raise RuntimeError(
            "LIVE mode requires ALPACA_API_KEY/ALPACA_KEY_ID and ALPACA_SECRET_KEY"
        )

    quote_provider = AlpacaRestQuoteProvider(
        api_key=settings.alpaca_key_id,
        api_secret=settings.alpaca_secret_key,
    )
    broker = AlpacaBrokerClient(
        settings,
        clock=clock,
        quote_provider=quote_provider,
        repository=repository,
    )
    allocator = LiveAlpacaAllocator(
        repository, settings, broker,
        clock=clock,
        catalyst_db_path=catalyst_db_path,
    )
    monitor = LiveAlpacaPositionMonitor(
        repository, settings, broker, quote_provider, clock=clock,
    )
    return broker, allocator, monitor
