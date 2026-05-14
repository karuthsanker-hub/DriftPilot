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


# ---------------------------------------------------------------------------
# Dynamic band computation
# ---------------------------------------------------------------------------

DEFAULT_ATR_PCT = 0.012          # 1.2% when ATR is missing
MAX_STOP_LOSS_PCT = 0.03         # hard guardrail: stop never exceeds 3%
BASE_TARGET_PCT = 0.01           # 1% base target
BASE_STOP_PCT = 0.01             # 1% base stop

# Multipliers
ATR_TARGET_SCALE = 0.5           # target = ATR_pct * this
ATR_STOP_SCALE = 0.75            # stop = ATR_pct * this
DRIFT_TAX_FACTOR = 0.30          # reduce target by 30% of drift
RVOL_BOOST_FACTOR = 0.10         # each 1x RVOL above 1.0 widens target by 10%
HIGH_BETA_THRESHOLD = 1.5
BETA_WIDEN_FACTOR = 0.20         # widen bands 20% for high-beta
CATALYST_WIDEN = {
    "earnings": 0.40,            # earnings widen 40%
    "analyst": 0.15,             # analyst widen 15%
    "fda": 0.50,
}
TIME_OF_DAY_STOP_MULT = {
    "open": 1.30,                # wider stops during open (30% more)
    "morning": 1.00,
    "midday": 1.00,
    "afternoon": 1.00,
    "close": 1.10,
    "off_session": 1.00,
}


@dataclass(frozen=True, slots=True)
class DynamicBands:
    """Entry/exit price bands with an explanation trail."""
    target_pct: float
    stop_pct: float
    reasoning: str


def compute_dynamic_bands(
    *,
    atr_pct: float | None = None,
    drift_pct: float = 0.0,
    rvol: float = 1.0,
    beta: float = 1.0,
    catalyst: str | None = None,
    time_of_day: str = "morning",
    spread_pct: float = 0.0,
) -> DynamicBands:
    """Compute adaptive target/stop bands for a trade.

    Parameters
    ----------
    atr_pct : float | None
        Average true range as a fraction (e.g. 0.02 for 2%).
        ``None`` falls back to DEFAULT_ATR_PCT (1.2%).
    drift_pct : float
        How much the stock already drifted (absolute value, e.g. 0.03 = 3%).
    rvol : float
        Relative volume ratio (1.0 = average).
    beta : float
        Stock beta.
    catalyst : str | None
        Catalyst type key (``"earnings"``, ``"analyst"``, ``"fda"``, …).
    time_of_day : str
        Time bucket (``"open"``, ``"morning"``, ``"midday"``, …).
    spread_pct : float
        Bid-ask spread as a fraction.

    Returns
    -------
    DynamicBands
        Contains ``target_pct``, ``stop_pct``, and ``reasoning`` string.
    """
    reasons: list[str] = []

    # --- ATR base -------------------------------------------------------
    effective_atr = atr_pct if atr_pct is not None else DEFAULT_ATR_PCT
    if atr_pct is None:
        reasons.append(f"ATR missing, using default {DEFAULT_ATR_PCT:.1%}")
    else:
        reasons.append(f"ATR {effective_atr:.2%}")

    target = effective_atr * ATR_TARGET_SCALE
    stop = effective_atr * ATR_STOP_SCALE

    # --- Drift tax ------------------------------------------------------
    if drift_pct > 0:
        tax = drift_pct * DRIFT_TAX_FACTOR
        target = max(target - tax, 0.002)  # floor at 0.2%
        reasons.append(f"drift tax -{tax:.2%} on target (drift={drift_pct:.1%})")

    # --- RVOL conviction boost ------------------------------------------
    if rvol > 1.0:
        boost = (rvol - 1.0) * RVOL_BOOST_FACTOR
        target += boost
        reasons.append(f"RVOL boost +{boost:.2%} (rvol={rvol:.1f}x)")

    # --- Beta profile ---------------------------------------------------
    if beta > HIGH_BETA_THRESHOLD:
        target *= (1 + BETA_WIDEN_FACTOR)
        stop *= (1 + BETA_WIDEN_FACTOR)
        reasons.append(f"high-beta widen 20% (beta={beta:.2f})")

    # --- Catalyst profile -----------------------------------------------
    if catalyst and catalyst in CATALYST_WIDEN:
        factor = CATALYST_WIDEN[catalyst]
        target *= (1 + factor)
        stop *= (1 + factor)
        reasons.append(f"catalyst '{catalyst}' widen {factor:.0%}")

    # --- Time-of-day profile --------------------------------------------
    tod_mult = TIME_OF_DAY_STOP_MULT.get(time_of_day, 1.0)
    if tod_mult != 1.0:
        stop *= tod_mult
        reasons.append(f"time-of-day '{time_of_day}' stop x{tod_mult:.2f}")

    # --- Spread cost deduction ------------------------------------------
    if spread_pct > 0:
        target = max(target - spread_pct, 0.001)
        reasons.append(f"spread cost -{spread_pct:.2%}")

    # --- Guardrail clamping ---------------------------------------------
    if stop > MAX_STOP_LOSS_PCT:
        reasons.append(f"stop clamped from {stop:.2%} to {MAX_STOP_LOSS_PCT:.1%}")
        stop = MAX_STOP_LOSS_PCT

    return DynamicBands(
        target_pct=round(target, 6),
        stop_pct=round(stop, 6),
        reasoning="; ".join(reasons),
    )


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
        universe_path: str | None = None,
        runtime_config_path: str | None = None,
    ) -> None:
        self.signal = signal
        self.quote_provider = quote_provider
        self.clock = clock
        # Hot-reload tracking — only re-read the file when its mtime changes.
        self._runtime_config_path = runtime_config_path
        self._runtime_config_mtime: float = 0.0
        # Lazy-load real sectors so catalyst candidates spread across the
        # allocator's per-sector cap. Otherwise all our candidates end up in
        # "Unknown" and cap fires after 3.
        self._sector_map: dict[str, str] = {}
        if universe_path:
            try:
                with open(universe_path) as f:
                    next(f, None)  # header
                    for line in f:
                        parts = line.split(",")
                        if len(parts) >= 3:
                            sym = parts[0].strip()
                            sector = parts[2].strip() or "Unknown"
                            if sym:
                                self._sector_map[sym] = sector
            except FileNotFoundError:
                logger.warning("universe path not found: %s — sector cap will fire on Unknown", universe_path)

    def _maybe_hot_reload(self) -> None:
        """If the runtime_config.json file changed since last check, swap
        the signal's _config. Caller-driven so it runs once per scan cycle.
        """
        if not self._runtime_config_path:
            return
        try:
            from pathlib import Path
            p = Path(self._runtime_config_path)
            if not p.exists():
                return
            mtime = p.stat().st_mtime
            if mtime == self._runtime_config_mtime:
                return
            from driftpilot.runtime_config import load_runtime_config
            from driftpilot.signals.earnings_report_v1.config import EarningsReportConfig
            cfg = load_runtime_config(p)
            require_sent = cfg.earnings_require_sentiment
            new_signal_cfg = EarningsReportConfig(
                max_hold_minutes=cfg.earnings_max_hold_minutes,
                profit_take_pct=cfg.earnings_profit_take_pct,
                stop_loss_pct=cfg.earnings_stop_loss_pct,
                max_event_age_minutes=cfg.earnings_max_event_age_minutes,
                require_sentiment=None if require_sent == "any" else require_sent,
                trailing_enabled=str(cfg.earnings_trailing_enabled).lower() == "true",
                trailing_activation_pct=cfg.earnings_trailing_activation_pct,
                trailing_distance_pct=cfg.earnings_trailing_distance_pct,
            )
            self.signal._config = new_signal_cfg  # type: ignore[attr-defined]
            self._runtime_config_mtime = mtime
            logger.info(
                "🔄 hot-reloaded signal config: max_hold=%dm profit=%.2f%% stop=%.2f%% "
                "max_age=%dm sentiment=%s",
                cfg.earnings_max_hold_minutes, cfg.earnings_profit_take_pct,
                cfg.earnings_stop_loss_pct, cfg.earnings_max_event_age_minutes,
                require_sent,
            )
        except Exception as exc:
            logger.warning("hot-reload failed: %s", exc)

    async def scan(self):
        self._maybe_hot_reload()
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
            sector = sc.sector or self._sector_map.get(sc.symbol.upper()) or "Unknown"
            ac = AllocationCandidate(
                symbol=sc.symbol,
                score=float(sc.score),
                sector=sector,
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

            submission = None
            try:
                submission = await self.broker.submit_entry_order(
                    symbol=allocation.symbol,
                    quantity=quantity,
                    slot_id=allocation.slot_id,
                )
            except Exception as exc:
                # Broker race: paper orders can fill faster than _wait_for_fill
                # polls. The fallback cancel then errors with
                # "order is already in 'filled' state". When that happens,
                # check Alpaca for the actual position and treat as filled.
                msg = str(exc).lower()
                if "already" in msg and "filled" in msg:
                    logger.info(
                        "LIVE: broker race on %s (cancel after fast-fill) — checking Alpaca for position",
                        allocation.symbol,
                    )
                    submission = None  # fall through to reconcile below
                else:
                    logger.exception(
                        "LIVE: entry submission failed for %s: %s", allocation.symbol, exc
                    )
                    continue

            # Reconcile with Alpaca's actual position state. Two paths in:
            # (a) broker returned submission with submitted=False
            # (b) broker raised "already filled" and we set submission=None above
            if submission is None or not submission.submitted:
                try:
                    open_positions = await self.broker.get_open_positions()
                except Exception:
                    open_positions = []
                actual = next(
                    (p for p in open_positions if p.symbol.upper() == allocation.symbol.upper()),
                    None,
                )
                if actual is not None:
                    reason = "reconciled_from_alpaca_position"
                    prior_reason = (submission.reason if submission else "broker_already_filled_race")
                    prior_id = (submission.broker_order_id if submission else None)
                    logger.warning(
                        "LIVE: reconciling %s from alpaca: qty=%s entry=$%.2f (broker prior reason=%s)",
                        actual.symbol, actual.quantity, float(actual.average_entry_price),
                        prior_reason,
                    )
                    submission = OrderSubmissionResult(
                        submitted=True,
                        broker_order_id=prior_id or actual.broker_position_id,
                        symbol=actual.symbol,
                        side="buy",
                        quantity=float(actual.quantity),
                        order_type="reconciled",
                        limit_price=float(actual.average_entry_price),
                        reason=reason,
                    )
                else:
                    logger.warning(
                        "LIVE: entry not submitted for %s and no alpaca position found — skipping",
                        allocation.symbol,
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
        # Track peak unrealized% per position across monitor cycles. Survives
        # within one operator session; operator restart loses it (acceptable —
        # the position's first cycle after restart starts fresh peak).
        self._peak_by_position_id: dict[int, float] = {}

    async def monitor(self):
        """State-machine-protocol entrypoint. Returns PositionMonitorResult."""
        from driftpilot.state_machine import PositionMonitorResult
        # Reconcile Alpaca positions into local DB FIRST. Without this, any
        # position created via the broker-race path (order filled before
        # local record was written) is invisible to the exit-evaluation loop.
        try:
            await self._reconcile_alpaca_to_local()
        except Exception as exc:
            logger.warning("monitor: alpaca reconcile failed: %s", exc)
        exits = await self.monitor_open_positions()
        positions = self.repository.positions.list_open()
        return PositionMonitorResult(
            open_positions=len(positions),
            exit_orders=exits,
            recycled_slots=0,
            halted_reason=None,
            metadata={"source": "live_alpaca_paper"},
        )

    async def _reconcile_alpaca_to_local(self) -> int:
        """Insert local position records for any Alpaca positions missing
        from our DB. Uses slot data + catalyst metadata where available.
        """
        from datetime import datetime, timezone
        import json
        try:
            alpaca_positions = await asyncio.wait_for(
                self.broker.get_open_positions(),
                timeout=8.0,
            )
        except asyncio.TimeoutError:
            logger.warning("monitor reconcile: alpaca get_open_positions timed out — skipping")
            return 0
        local_open = self.repository.positions.list_open()
        local_symbols = {(p.symbol or "").upper() for p in local_open}

        added = 0
        for ap in alpaca_positions:
            sym = (ap.symbol or "").upper()
            if sym in local_symbols:
                continue
            # Find a slot that's RESERVED for this symbol (allocator did the
            # reservation; broker race ate the position-create step).
            slot = None
            for s in self.repository.slots.list_all():
                if s.symbol == sym and s.status in ("RESERVED", "OPEN"):
                    slot = s
                    break
            if slot is None:
                # Alpaca position outside our slot system (manual test trade,
                # leftover from prior session, etc). Expected — not noise.
                logger.debug(
                    "monitor reconcile: alpaca has %s qty=%s but no matching slot — skipping",
                    sym, ap.quantity,
                )
                continue

            # Pull catalyst metadata from slot's stored candidate
            slot_md = {}
            slot_md_raw = getattr(slot, "metadata_json", None) or "{}"
            try:
                slot_md = json.loads(slot_md_raw)
            except (TypeError, json.JSONDecodeError):
                pass
            cand = slot_md.get("candidate", {})
            entry_price = float(ap.average_entry_price)
            opened_at = slot_md.get("reserved_at") or datetime.now(timezone.utc).isoformat()

            position_md = {
                "sector": cand.get("sector", "Unknown"),
                "broker_order_id": ap.broker_position_id,
                "reference_price": cand.get("reference_price", entry_price),
                "current_price": entry_price,
                "entry_ts": opened_at,
                "entry_price": entry_price,
                "source": "live_alpaca_paper_monitor_reconcile",
                "catalyst_event_ts": cand.get("catalyst_event_ts"),
                "catalyst_headline_hash": cand.get("headline_hash"),
                "catalyst_sentiment": cand.get("sentiment"),
                "catalyst_headline": (cand.get("headline") or "")[:200],
                "catalyst_event_age_min_at_entry": cand.get("event_age_minutes"),
                "signal_score_at_entry": slot_md.get("score", 0.0),
            }
            try:
                self.repository.positions.create_open(
                    symbol=sym,
                    quantity=float(ap.quantity),
                    entry_price=entry_price,
                    target_price=entry_price * (1 + self.settings.target_pct),
                    stop_price=entry_price * (1 - self.settings.stop_pct),
                    slot_id=slot.slot_id,
                    opened_at=datetime.fromisoformat(opened_at),
                    metadata=position_md,
                )
                self.repository.slots.upsert(
                    slot.slot_id, status="OPEN", symbol=sym,
                    slot_value=getattr(slot, "slot_value", self.settings.slot_value),
                    updated_at=datetime.now(timezone.utc),
                )
                added += 1
                logger.info(
                    "monitor reconcile: created local position %s qty=%s entry=$%.2f from alpaca state",
                    sym, ap.quantity, entry_price,
                )
            except Exception as exc:
                logger.warning("monitor reconcile: failed to create local %s position: %s", sym, exc)
        return added

    def _get_signal(self):
        if self._signal is not None:
            return self._signal
        return get_signal(self.settings.active_signal)

    async def _process_one_position(self, position, signal, now) -> int:
        """Evaluate and (if needed) submit an exit for a single position.
        Returns 1 if an exit was completed, 0 otherwise. Exceptions are
        caught and logged so a single bad position can't kill the batch.
        """
        symbol = position.symbol.upper()
        try:
            try:
                quote = await asyncio.wait_for(
                    asyncio.to_thread(self.quote_provider.latest_quote, symbol),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("monitor: quote fetch for %s timed out — skipping cycle", symbol)
                return 0
            if quote is None:
                logger.debug("monitor: no quote for %s — skipping", symbol)
                return 0

            mid = (quote.bid_price + quote.ask_price) / 2.0
            entry_price = float(getattr(position, "entry_price", 0.0))
            unrealized_pct = ((mid - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

            # Set current_price + peak_unrealized_pct on the position so the
            # signal's evaluate_exit can compute trailing stop correctly.
            try:
                position_metadata = dict(getattr(position, "metadata", {}) or {})
            except Exception:
                position_metadata = {}
            position_id = getattr(position, "id", None)
            prev_peak = self._peak_by_position_id.get(position_id, unrealized_pct)
            new_peak = max(prev_peak, unrealized_pct)
            if position_id is not None:
                self._peak_by_position_id[position_id] = new_peak
            position_metadata["current_price"] = mid
            position_metadata["peak_unrealized_pct"] = new_peak
            try:
                object.__setattr__(position, "metadata", position_metadata)
                if hasattr(position, "current_price"):
                    object.__setattr__(position, "current_price", mid)
            except Exception:
                pass

            try:
                decision = signal.evaluate_exit(position, now)
                if inspect.isawaitable(decision):
                    decision = await decision
            except Exception as exc:
                logger.exception("monitor: evaluate_exit raised for %s: %s", symbol, exc)
                return 0

            should_close = getattr(decision, "should_exit", None)
            if should_close is None:
                should_close = getattr(decision, "close", False)
            if decision is None or not should_close:
                return 0

            quantity = float(getattr(position, "quantity", 0))
            if quantity <= 0:
                return 0

            exit_reason = (
                getattr(decision, "exit_reason", None)
                or getattr(decision, "reason", None)
                or "unknown"
            )
            logger.info(
                "LIVE: signal requests exit symbol=%s reason=%s unrealized=%.3f%% peak=%.3f%%",
                symbol, exit_reason, unrealized_pct, new_peak,
            )
            exit_result = None
            broker_race_filled = False
            try:
                exit_result = await self.broker.submit_exit_order(
                    symbol=symbol,
                    quantity=quantity,
                    position_id=getattr(position, "id", None),
                )
            except Exception as exc:
                msg = str(exc).lower()
                if "already" in msg and "filled" in msg:
                    logger.info("LIVE: broker race on exit %s (cancel after fast-fill)", symbol)
                    broker_race_filled = True
                else:
                    logger.exception("LIVE: exit submission failed for %s: %s", symbol, exc)
                    return 0

            # Verify position is gone at Alpaca (with timeout to prevent hang)
            position_gone = False
            try:
                live_positions = await asyncio.wait_for(
                    self.broker.get_open_positions(), timeout=5.0,
                )
                position_gone = not any(
                    p.symbol.upper() == symbol for p in live_positions
                )
            except Exception:
                position_gone = False

            if exit_result and exit_result.submitted:
                exit_price = exit_result.limit_price or mid
                broker_oid = exit_result.broker_order_id
                close_reason_label = "submitted"
            elif broker_race_filled or position_gone:
                exit_price = mid
                broker_oid = None
                close_reason_label = "reconciled_after_race"
            else:
                return 0

            realized = (exit_price - entry_price) * quantity
            try:
                self.repository.positions.close(
                    position_id=getattr(position, "id"),
                    exit_reason=exit_reason,
                    realized_pnl=realized,
                    closed_at=self.clock.now_utc(),
                    metadata={
                        "exit_price": exit_price,
                        "broker_exit_order_id": broker_oid,
                        "exit_close_path": close_reason_label,
                        "peak_unrealized_pct": new_peak,
                    },
                )
                slot_id = getattr(position, "slot_id", None)
                if slot_id is not None:
                    try:
                        self.repository.slots.upsert(
                            slot_id, status="EMPTY", symbol=None,
                            slot_value=self.settings.slot_value,
                            updated_at=self.clock.now_utc(),
                        )
                    except Exception as slot_exc:
                        logger.warning("LIVE: slot %s free failed: %s", slot_id, slot_exc)
                # Clean up in-memory peak tracker
                self._peak_by_position_id.pop(getattr(position, "id", None), None)
                logger.info(
                    "LIVE: position closed symbol=%s broker_order_id=%s reason=%s realized=$%.2f path=%s slot=%s peak=%.3f%% freed",
                    symbol, broker_oid, exit_reason, realized, close_reason_label, slot_id, new_peak,
                )
                return 1
            except Exception as close_exc:
                logger.warning(
                    "LIVE: position closed at broker but local close failed for %s: %s",
                    symbol, close_exc,
                )
                return 0
        except Exception as exc:
            logger.exception("monitor: unexpected error processing %s: %s", symbol, exc)
            return 0

    async def monitor_open_positions(self) -> int:
        """Process every open position in PARALLEL within one cycle.

        Previously serialized: 9 positions × ~15s broker call = 2+ min per
        cycle, by which time positions aged past time_stop and never got
        the chance to fire profit_take/stop_loss. Now: each position is its
        own task; all complete within ~10s regardless of count.
        """
        signal = self._get_signal()
        positions = self.repository.positions.list_open()
        if not positions:
            return 0
        now = self.clock.now_utc()
        results = await asyncio.gather(
            *(self._process_one_position(p, signal, now) for p in positions),
            return_exceptions=True,
        )
        exit_count = sum(r for r in results if isinstance(r, int))
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
