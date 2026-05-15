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
import json
import logging
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from driftpilot.broker.alpaca_client import AlpacaBrokerClient, OrderSubmissionResult
from driftpilot.clock import DriftPilotClock
from driftpilot.execution.slot_allocator import (
    AllocationCandidate,
    AllocationResult,
    SlotAllocator,
)
from driftpilot.market_data.rest_quotes import AlpacaRestQuoteProvider
from driftpilot.settings import DriftPilotSettings
from driftpilot.signal_router import RoutingAction, RuleBasedRouter
from driftpilot.signals import get_signal
from driftpilot.signals.earnings_report_v1 import (
    EarningsReportConfig,
    EarningsReportSignal,
)
from driftpilot.services import (
    EodPositionSnapshot,
    _eod_dilution_step_count,
    _least_profitable_concentrated_sector_positions,
    _median,
    _tightened_eod_stop_price,
)
from driftpilot.state_machine import ReconciliationResult
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
    *price_args: float,
    atr_pct: float | None = None,
    drift_pct: float = 0.0,
    rvol: float = 1.0,
    beta: float | None = 1.0,
    catalyst: str | None = None,
    time_of_day: str = "morning",
    spread_pct: float = 0.0,
    default_target_pct: float | None = None,
    default_stop_pct: float | None = None,
    entry_time: datetime | None = None,
    category: str | None = None,
    subcategory: str | None = None,
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
    if price_args or default_target_pct is not None or default_stop_pct is not None or entry_time is not None:
        return _compute_legacy_dynamic_bands(
            atr_pct=atr_pct,
            drift_pct=drift_pct,
            beta=beta,
            default_target_pct=default_target_pct,
            default_stop_pct=default_stop_pct,
            entry_time=entry_time,
            category=category,
            subcategory=subcategory,
        )

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
    if beta is not None and beta > HIGH_BETA_THRESHOLD:
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
    reasons.append(f"time_profile={time_of_day}")

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


def _compute_legacy_dynamic_bands(
    *,
    atr_pct: float | None,
    drift_pct: float,
    beta: float | None,
    default_target_pct: float | None,
    default_stop_pct: float | None,
    entry_time: datetime | None,
    category: str | None,
    subcategory: str | None,
) -> DynamicBands:
    """Compatibility path for the pre-Task-8 dynamic-bands call shape."""
    target = default_target_pct if default_target_pct is not None else BASE_TARGET_PCT
    stop = default_stop_pct if default_stop_pct is not None else BASE_STOP_PCT
    reasons: list[str] = []

    if atr_pct is None:
        reasons.append("atr_profile=default")
    else:
        reasons.append(f"atr_pct={atr_pct}")

    if beta is not None and beta > HIGH_BETA_THRESHOLD:
        target *= 1.20
        stop *= 1.20
        reasons.append("beta_profile=high_beta")
    elif beta is not None and beta < 0.8:
        target *= 0.85
        stop *= 0.85
        reasons.append("beta_profile=low_beta")
    else:
        reasons.append("beta_profile=normal")

    if entry_time is not None:
        try:
            from zoneinfo import ZoneInfo

            et_time = entry_time.astimezone(ZoneInfo("America/New_York")).time()
        except Exception as exc:
            logger.warning("dynamic bands legacy time conversion failed: %s", exc)
            et_time = entry_time.time()

        if et_time < datetime.strptime("10:00", "%H:%M").time():
            stop *= 1.30
            reasons.append("time_profile=opening_volatility")
        elif datetime.strptime("12:00", "%H:%M").time() <= et_time < datetime.strptime("13:30", "%H:%M").time():
            stop *= 0.80
            reasons.append("time_profile=midday_quiet")
        elif et_time >= datetime.strptime("15:30", "%H:%M").time():
            stop *= 1.20
            reasons.append("time_profile=closing_volatility")
        else:
            reasons.append("time_profile=regular")

    if category and subcategory:
        if category == "earnings" and subcategory == "report":
            target *= 1.60
            stop *= 1.20
        elif category == "analyst" and subcategory == "target_raise":
            target *= 1.20
            stop *= 1.10
        reasons.append(f"catalyst_profile={category}/{subcategory}")

    if drift_pct > 0:
        drift_fraction = drift_pct / 100.0 if drift_pct > 0.5 else drift_pct
        target = max(target - (drift_fraction * DRIFT_TAX_FACTOR), 0.001)
        reasons.append("drift_adj")

    if stop > MAX_STOP_LOSS_PCT:
        stop = MAX_STOP_LOSS_PCT
        reasons.append("stop_clamped")

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


class MultiSignal:
    """Fan-out/fan-in wrapper for live paper signals."""

    name: str = "multi_signal"

    def __init__(self, signals: list[Any]) -> None:
        if not signals:
            raise ValueError("MultiSignal requires at least one sub-signal")
        self._signals = signals
        self._by_name: dict[str, Any] = {
            getattr(signal, "name", None) or signal.__class__.__name__: signal
            for signal in signals
        }

    @property
    def signals(self) -> list[Any]:
        return list(self._signals)

    async def subscribe(self) -> None:
        for signal in self._signals:
            subscribe = getattr(signal, "subscribe", None)
            if subscribe is None:
                continue
            result = subscribe()
            if inspect.isawaitable(result):
                await result

    def bootstrap_from_db(self, db_path: str) -> int:
        total = 0
        for signal in self._signals:
            bootstrap = getattr(signal, "bootstrap_from_db", None)
            if bootstrap is None:
                continue
            try:
                total += int(bootstrap(db_path) or 0)
            except Exception as exc:
                logger.warning("MultiSignal bootstrap %s failed: %s", signal.name, exc)
        return total

    async def scan(self, now: datetime | None = None) -> list[Any]:
        merged: dict[str, Any] = {}
        order: list[str] = []
        for signal in self._signals:
            try:
                params = inspect.signature(signal.scan).parameters
                result = signal.scan(now=now) if "now" in params else signal.scan()
                candidates = await result if inspect.isawaitable(result) else result
            except Exception as exc:
                logger.exception("MultiSignal: %s.scan raised: %s", signal.name, exc)
                continue

            for candidate in candidates or []:
                features = dict(getattr(candidate, "features", None) or {})
                features.setdefault("signal_name", getattr(signal, "name", None))
                try:
                    candidate = candidate.__class__(
                        symbol=candidate.symbol,
                        score=candidate.score,
                        sector=candidate.sector,
                        allowed=candidate.allowed,
                        blocked_reason=candidate.blocked_reason,
                        features=features,
                    )
                except Exception as exc:
                    logger.debug("MultiSignal candidate reconstruction skipped: %s", exc)
                symbol = str(getattr(candidate, "symbol", "")).upper()
                if not symbol:
                    continue
                if symbol not in merged:
                    order.append(symbol)
                    merged[symbol] = candidate
                    continue
                if float(getattr(candidate, "score", 0.0)) > float(getattr(merged[symbol], "score", 0.0)):
                    merged[symbol] = candidate
        return [merged[symbol] for symbol in order]

    def evaluate_exit(self, position, now, *args, **kwargs):
        metadata = getattr(position, "metadata", {}) or {}
        signal_name = metadata.get("signal_name")
        target = self._by_name.get(signal_name) if signal_name else None
        if target is None:
            target = self._signals[0]
        try:
            return target.evaluate_exit(position, now, *args, **kwargs)
        except Exception as exc:
            logger.exception("MultiSignal.evaluate_exit %s raised: %s", signal_name, exc)
            return None


@dataclass(frozen=True, slots=True)
class _RoutingEventStub:
    """Minimal event shape accepted by RuleBasedRouter."""

    category: str
    subcategory: str
    sentiment: str | None = None
    priority_modifier: float = 0.0


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

    async def reconcile_open_positions(self) -> ReconciliationResult:
        try:
            broker_positions = await self.alpaca.get_open_positions()
        except Exception as exc:
            logger.error("alpaca reconcile fetch failed: %s", exc, exc_info=True)
            return ReconciliationResult(
                ok=False,
                status="broker_unavailable",
                error=str(exc),
            )

        # Translate alpaca BrokerPosition → list of dicts the repo expects.
        # Use direct attribute access (no getattr defaults) so type mismatches
        # crash loudly instead of silently producing $0 prices.
        translated = []
        for p in broker_positions:
            entry = float(p.average_entry_price)
            if entry <= 0:
                logger.error(
                    "reconcile: %s has entry_price=%.4f — skipping",
                    p.symbol, entry,
                )
                continue
            translated.append({
                "symbol": str(p.symbol).upper(),
                "quantity": float(p.quantity),
                "entry_price": entry,
            })
        try:
            status = self.repository.positions.reconcile_broker_open_positions(
                broker_positions=translated,
                slot_value=self.settings.slot_value,
                target_pct=self.settings.target_pct,
                stop_pct=self.settings.stop_pct,
                trade_slots=self.settings.trade_slots,
            )
            return ReconciliationResult(
                ok=True,
                status=status,
                broker_symbols=tuple(sorted(position["symbol"] for position in translated)),
                metadata={"broker_position_count": len(translated)},
            )
        except Exception as exc:
            logger.error("repo reconcile failed: %s", exc, exc_info=True)
            return ReconciliationResult(
                ok=False,
                status="repo_reconcile_failed",
                broker_symbols=tuple(sorted(position["symbol"] for position in translated)),
                error=str(exc),
            )

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
        repository: DriftPilotRepository | None = None,
        router: RuleBasedRouter | None = None,
    ) -> None:
        self.signal = signal
        self.quote_provider = quote_provider
        self.clock = clock
        self.repository = repository
        self._router = router or RuleBasedRouter()
        self._brain_client = None
        self._last_eod_reflection_date: str | None = None
        self._last_eod_reflection_context: dict[str, Any] | None = None
        self._max_price_drift_pct = 3.0
        self._max_entry_atr_pct = 6.0
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

    def _load_context_json(self, symbol: str, headline_hash: str | None, features: dict[str, Any]) -> dict[str, Any]:
        raw = features.get("context_json")
        if raw is None and headline_hash:
            db_path = getattr(self.signal, "_db_path", None)
            if db_path:
                try:
                    conn = sqlite3.connect(db_path)
                    try:
                        row = conn.execute(
                            """
                            SELECT context_json
                            FROM catalyst_events
                            WHERE symbol = ? AND headline_hash = ?
                            ORDER BY event_ts DESC
                            LIMIT 1
                            """,
                            (symbol.upper(), headline_hash),
                        ).fetchone()
                    finally:
                        conn.close()
                    raw = row[0] if row else None
                except sqlite3.Error as exc:
                    logger.warning("context lookup failed for %s/%s: %s", symbol, headline_hash, exc)
        if not raw:
            return {}
        try:
            decoded = json.loads(str(raw))
        except json.JSONDecodeError as exc:
            logger.warning("context_json parse failed for %s/%s: %s", symbol, headline_hash, exc)
            return {}
        if not isinstance(decoded, dict):
            logger.warning("context_json for %s/%s was not an object", symbol, headline_hash)
            return {}
        return decoded

    def _record_price_drift(
        self,
        *,
        symbol: str,
        event_key: str | None,
        price: float,
        now: datetime,
        metadata: dict[str, Any],
    ) -> bool:
        if self.repository is None or not event_key:
            return True
        baseline = self.repository.price_drift_baselines.update_seen(
            symbol=symbol,
            event_key=event_key,
            price=price,
            seen_at=now,
            metadata={"headline": metadata.get("headline")},
        )
        metadata["first_seen_price"] = baseline.first_seen_price
        metadata["price_drift_pct"] = baseline.drift_pct
        if baseline.drift_pct <= self._max_price_drift_pct:
            return True
        self.repository.candidate_queue.mark_blocked(
            symbol,
            reason="price_drift",
            features=metadata,
            updated_at=now,
        )
        logger.info(
            "catalyst scanner: blocked %s price drift %.2f%% > %.2f%%",
            symbol,
            baseline.drift_pct,
            self._max_price_drift_pct,
        )
        return False

    def _passes_atr_guardrail(
        self,
        *,
        symbol: str,
        atr_pct: Any,
        now: datetime,
        metadata: dict[str, Any],
    ) -> bool:
        if atr_pct is None:
            return True
        try:
            atr_value = float(atr_pct)
        except (TypeError, ValueError):
            logger.warning("invalid atr_pct for %s: %r", symbol, atr_pct)
            return True
        metadata["atr_pct"] = atr_value
        if atr_value <= self._max_entry_atr_pct:
            return True
        if self.repository is not None:
            self.repository.candidate_queue.mark_blocked(
                symbol,
                reason="high_volatility_atr",
                features=metadata,
                updated_at=now,
            )
        logger.info(
            "catalyst scanner: blocked %s ATR %.2f%% > %.2f%%",
            symbol,
            atr_value,
            self._max_entry_atr_pct,
        )
        return False

    def _fetch_snapshot_features(
        self,
        symbols: list[str],
        now: datetime,
    ) -> dict[str, dict[str, float]]:
        """Batch-fetch Alpaca snapshots and compute RVOL + intraday return.

        Returns {SYMBOL: {"rvol": float, "return_15m_pct": float,
        "vwap_distance_pct": float, "today_volume": int}} for each symbol
        where the data was available.  Missing symbols get an empty dict.
        """
        if not symbols:
            return {}
        result: dict[str, dict[str, float]] = {}
        try:
            from alpaca.data.historical.stock import StockHistoricalDataClient
            from alpaca.data.requests import StockSnapshotRequest

            # Dedicated short-lived client to avoid CLOSE_WAIT connection reuse
            client = StockHistoricalDataClient(
                api_key=self.quote_provider._api_key,
                secret_key=self.quote_provider._api_secret,
            )
            req = StockSnapshotRequest(symbol_or_symbols=symbols)
            snaps = client.get_stock_snapshot(req)
            if not isinstance(snaps, dict):
                return result
            logger.debug("snapshot enrichment: fetched %d/%d symbols", len(snaps), len(symbols))

            from zoneinfo import ZoneInfo
            now_et = now.astimezone(ZoneInfo("America/New_York"))
            market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            elapsed_s = max((now_et - market_open).total_seconds(), 60.0)
            elapsed_frac = min(elapsed_s / 23400.0, 1.0)  # 6.5h trading day

            for sym, snap in snaps.items():
                try:
                    daily = getattr(snap, "daily_bar", None)
                    prev = getattr(snap, "prev_daily_bar", None)
                    minute = getattr(snap, "minute_bar", None)
                    if daily is None:
                        continue
                    today_vol = float(daily.volume or 0)
                    close_price = float(daily.close or 0)
                    open_price = float(daily.open or 0)
                    vwap = float(daily.vwap or 0)

                    # RVOL: today's volume pace vs expected volume
                    # Try prev_daily_bar first; fallback to trade_count ratio
                    avg_vol = float(prev.volume or 0) if prev else 0
                    rvol = 0.0
                    if avg_vol > 0 and elapsed_frac > 0:
                        expected = avg_vol * elapsed_frac
                        rvol = today_vol / expected if expected > 0 else 0.0
                    elif today_vol > 0 and elapsed_frac > 0:
                        # No prev day data — estimate RVOL from volume-per-minute
                        # vs. typical ~300K avg daily volume as rough benchmark
                        minutes_elapsed = max(elapsed_s / 60.0, 1.0)
                        vol_per_min = today_vol / minutes_elapsed
                        # Rough benchmark: avg stock trades ~770 shares/min in 6.5h
                        rvol = vol_per_min / 770.0 if vol_per_min > 0 else 0.0

                    # Intraday return (proxy for 15M — best available without bar history)
                    return_pct = 0.0
                    if open_price > 0:
                        return_pct = (close_price - open_price) / open_price

                    # VWAP distance
                    vwap_dist = 0.0
                    if vwap > 0 and close_price > 0:
                        vwap_dist = (close_price - vwap) / vwap

                    result[sym.upper()] = {
                        "rvol": round(rvol, 2),
                        "return_15m_pct": round(return_pct, 4),
                        "vwap_distance_pct": round(vwap_dist, 4),
                        "today_volume": int(today_vol),
                    }
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("snapshot enrichment failed: %s", exc)
        return result

    def _annotate_with_routing(
        self,
        candidates: list[AllocationCandidate],
        now: datetime,
    ) -> list[AllocationCandidate]:
        """Attach signal-router decisions and filter router-blocked candidates."""
        router = getattr(self, "_router", None)
        if router is None:
            return candidates

        routed: list[AllocationCandidate] = []
        for candidate in candidates:
            metadata = dict(candidate.metadata or {})
            category = metadata.get("category")
            subcategory = metadata.get("subcategory")
            if not category or not subcategory:
                routed.append(candidate)
                continue

            event = _RoutingEventStub(
                category=str(category),
                subcategory=str(subcategory),
                sentiment=metadata.get("sentiment"),
                priority_modifier=float(metadata.get("priority_modifier") or 0.0),
            )
            try:
                decisions = router.route(event, time_et=now)
            except Exception as exc:
                logger.warning(
                    "signal router failed for %s %s/%s: %s — keeping candidate",
                    candidate.symbol,
                    category,
                    subcategory,
                    exc,
                )
                routed.append(candidate)
                continue

            metadata["routing_decisions"] = [
                {
                    "action": decision.action.value,
                    "signal_name": decision.signal_name,
                    "horizon_minutes": decision.horizon_minutes,
                    "conviction": decision.conviction,
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                }
                for decision in decisions
            ]
            if any(decision.action == RoutingAction.BLOCK for decision in decisions):
                logger.info(
                    "signal router blocked %s: %s",
                    candidate.symbol,
                    "; ".join(d.reason for d in decisions if d.action == RoutingAction.BLOCK),
                )
                continue

            routed_signal = next(
                (
                    decision.signal_name
                    for decision in decisions
                    if decision.action in {RoutingAction.ROUTE, RoutingAction.DEFERRED}
                    and decision.signal_name
                ),
                None,
            )
            if routed_signal:
                metadata["routed_signal"] = routed_signal
                metadata["signal_name"] = routed_signal
            routed.append(replace(candidate, metadata=metadata))
        return routed

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
            self._max_price_drift_pct = cfg.max_price_drift_pct
            self._max_entry_atr_pct = cfg.max_entry_atr_pct
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
            scan_result = self.signal.scan(now=now)
            sig_candidates = await scan_result if inspect.isawaitable(scan_result) else scan_result
        except Exception as exc:
            logger.exception("catalyst scanner: signal.scan raised: %s", exc)
            return _build_scan_result([], now)

        # ── Batch-fetch Alpaca snapshots for RVOL / return enrichment ──
        candidate_symbols = [sc.symbol.upper() for sc in sig_candidates]
        snapshot_features = await asyncio.to_thread(
            self._fetch_snapshot_features, candidate_symbols, now
        ) if candidate_symbols else {}

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
            headline_hash = features.get("headline_hash")
            context = self._load_context_json(sc.symbol, headline_hash, features)

            # ── Compute spread quality ──
            spread_pct = 0.0
            if ref_price > 0 and quote.bid_price > 0 and quote.ask_price > 0:
                spread_pct = (quote.ask_price - quote.bid_price) / ref_price * 100.0

            # ── Extract enrichment data ──
            priority_mod = float(features.get("priority_modifier") or 0.0)
            confidence = float(features.get("confidence") or 0.5)
            event_age = float(features.get("event_age_minutes") or 0.0)

            metadata = {
                "reference_price": ref_price,
                "catalyst_event_ts": cat_ts_str,
                "headline": features.get("headline"),
                "headline_hash": headline_hash,
                "sentiment": features.get("sentiment"),
                "priority_modifier": priority_mod,
                "confidence": confidence,
                "category": features.get("category"),
                "subcategory": features.get("subcategory"),
                "event_age_minutes": event_age,
                "horizon_minutes": features.get("horizon_minutes"),
                "source": features.get("source", "catalyst_bus"),
                "spread_pct": round(spread_pct, 4),
                "bid_price": quote.bid_price,
                "ask_price": quote.ask_price,
            }
            # ── Merge snapshot-derived RVOL / return / VWAP into metadata ──
            # Prefer signal-provided features (e.g. volume_spike already has rvol)
            snap_feat = snapshot_features.get(sc.symbol.upper(), {})
            metadata["rvol"] = float(features.get("rvol") or snap_feat.get("rvol") or 0.0)
            metadata["return_15m_pct"] = float(features.get("return_15m_pct") or snap_feat.get("return_15m_pct") or 0.0)
            metadata["vwap_distance_pct"] = float(features.get("vwap_distance_pct") or snap_feat.get("vwap_distance_pct") or 0.0)
            metadata["today_volume"] = int(features.get("today_volume") or snap_feat.get("today_volume") or 0)

            atr_pct = context.get("atr_pct", features.get("atr_pct"))
            beta = context.get("beta", features.get("beta"))
            if atr_pct is not None:
                try:
                    metadata["atr_pct"] = float(atr_pct)
                except (TypeError, ValueError):
                    pass
            if beta is not None:
                try:
                    metadata["beta"] = float(beta)
                except (TypeError, ValueError):
                    logger.warning("invalid beta for %s: %r", sc.symbol, beta)
            if not self._passes_atr_guardrail(
                symbol=sc.symbol,
                atr_pct=atr_pct,
                now=now,
                metadata=metadata,
            ):
                continue
            if not self._record_price_drift(
                symbol=sc.symbol,
                event_key=str(headline_hash) if headline_hash else None,
                price=ref_price,
                now=now,
                metadata=metadata,
            ):
                continue

            # ── Compute real conviction score ──
            # Base: priority_modifier (Qwen's magnitude estimate, -0.20 to +0.20)
            # Weighted by: confidence (0-1), recency decay, spread quality
            recency_factor = max(0.3, 1.0 - event_age / 120.0)  # decay over 2 hours
            spread_penalty = max(0.5, 1.0 - spread_pct * 5.0)   # 0.2% spread = 0, wider = penalty
            conviction_score = round(
                abs(priority_mod) * max(confidence, 0.3) * recency_factor * spread_penalty,
                4,
            )
            metadata["conviction_score"] = conviction_score

            ac = AllocationCandidate(
                symbol=sc.symbol,
                score=conviction_score,
                sector=sector,
                latest_bar_at=now,
                rank=rank,
                metadata=metadata,
            )
            candidates.append(ac)
            logger.info(
                "CANDIDATE %s rank=%d score=%.4f (pm=%.2f conf=%.2f age=%.0fm spread=%.3f%% rvol=%.1fx ret=%.2f%%) sentiment=%s ref=%.2f | %s",
                ac.symbol, rank, ac.score,
                priority_mod, confidence, event_age, spread_pct,
                metadata.get("rvol", 0.0),
                metadata.get("return_15m_pct", 0.0) * 100,
                features.get("sentiment") or "NONE",
                ref_price,
                (features.get("headline") or "")[:60],
            )

            # ── Record passing candidate in queue for dashboard visibility ──
            if self.repository is not None:
                try:
                    self.repository.candidate_queue.mark_passed(
                        sc.symbol,
                        score=conviction_score,
                        rank=rank,
                        features=metadata,
                        updated_at=now,
                    )
                except Exception:
                    pass  # non-fatal

        if not candidates:
            logger.info("catalyst scanner: 0 candidates this cycle (no admitted events)")
        candidates = self._annotate_with_routing(candidates, now)
        return _build_scan_result(candidates, now)

    async def _trigger_eod_reflection(self, now: datetime, *, reason: str) -> asyncio.Task | None:
        try:
            from zoneinfo import ZoneInfo

            now_et = now.astimezone(ZoneInfo("America/New_York"))
        except Exception as exc:
            logger.warning("eod reflection timezone conversion failed: %s", exc)
            now_et = now
        if now_et.hour < 16:
            return None
        date_str = now_et.date().isoformat()
        if self._last_eod_reflection_date == date_str:
            return None

        context = self._build_eod_reflection_context(date_str)
        self._last_eod_reflection_context = context
        self._last_eod_reflection_date = date_str

        async def _run_reflection() -> None:
            brain = self._brain_client
            if brain is None:
                from driftpilot.agents.brain_client import BrainClient

                brain = BrainClient()
                self._brain_client = brain
            try:
                result = brain.reflect(date_str)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.warning("eod reflection failed for %s: %s", date_str, exc)

        logger.info("triggering eod reflection for %s (%s)", date_str, reason)
        return asyncio.create_task(_run_reflection())

    def _build_eod_reflection_context(self, date_str: str) -> dict[str, Any]:
        if self.repository is None:
            return {"date": date_str, "closed_trade_count": 0, "symbols": [], "closed_trades": []}
        rows = self.repository.connection.execute(
            """
            SELECT symbol, quantity, entry_price, closed_at, exit_reason, realized_pnl, metadata_json
            FROM positions
            WHERE status = 'closed' AND substr(closed_at, 1, 10) = ?
            ORDER BY closed_at
            """,
            (date_str,),
        ).fetchall()
        trades: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            quantity = float(row["quantity"] or 0.0)
            entry_price = float(row["entry_price"] or 0.0)
            realized_pnl = float(row["realized_pnl"] or 0.0)
            cost_basis = quantity * entry_price
            pnl_pct = (realized_pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0
            trades.append(
                {
                    "symbol": row["symbol"],
                    "closed_at": row["closed_at"],
                    "exit_reason": row["exit_reason"],
                    "realized_pnl": realized_pnl,
                    "pnl_pct": pnl_pct,
                    "signal_name": metadata.get("signal_name"),
                }
            )
        return {
            "date": date_str,
            "closed_trade_count": len(trades),
            "symbols": sorted({str(trade["symbol"]) for trade in trades}),
            "closed_trades": trades,
        }


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
            repository, settings, clock=self.clock,
            catalyst_db_path=catalyst_db_path,
            max_slots_per_sector=settings.max_slots_per_sector,
        )

    async def allocate(self, candidates: list[AllocationCandidate]) -> AllocationResult:
        result = await self.allocator.allocate(candidates)
        candidate_by_symbol = {c.symbol.upper(): c for c in candidates}

        for allocation in result.allocations:
            candidate = candidate_by_symbol[allocation.symbol.upper()]
            reference_price = float(candidate.metadata.get("reference_price", 100.0))
            try:
                slot_value_multiplier = float(candidate.metadata.get("slot_value_multiplier", 1.0) or 1.0)
            except (TypeError, ValueError):
                logger.warning(
                    "LIVE: invalid slot_value_multiplier for %s: %r",
                    allocation.symbol,
                    candidate.metadata.get("slot_value_multiplier"),
                )
                slot_value_multiplier = 1.0
            effective_slot_value = allocation.slot_value * slot_value_multiplier
            quantity = max(1, int(effective_slot_value // reference_price))

            raw_beta = candidate.metadata.get("beta")
            band_beta: float | None
            band_beta_profile = "unknown"
            try:
                band_beta = float(raw_beta) if raw_beta is not None else None
            except (TypeError, ValueError):
                band_beta = None
                logger.warning("LIVE: invalid beta metadata for %s: %r", allocation.symbol, raw_beta)
            if band_beta is not None:
                if band_beta > HIGH_BETA_THRESHOLD:
                    band_beta_profile = "high_beta"
                elif band_beta < 0.8:
                    band_beta_profile = "low_beta"
                else:
                    band_beta_profile = "normal"
            band_catalyst_profile = None
            if candidate.metadata.get("category") and candidate.metadata.get("subcategory"):
                band_catalyst_profile = (
                    f"{candidate.metadata.get('category')}/{candidate.metadata.get('subcategory')}"
                )
            # Derive time-of-day bucket from entry time for ATR-based bands
            _band_tod = "morning"
            try:
                from zoneinfo import ZoneInfo
                _et = allocation.reserved_at.astimezone(ZoneInfo("America/New_York"))
                _et_time = _et.time()
                if _et_time < datetime.strptime("10:00", "%H:%M").time():
                    _band_tod = "open"
                elif _et_time < datetime.strptime("12:00", "%H:%M").time():
                    _band_tod = "morning"
                elif _et_time < datetime.strptime("13:30", "%H:%M").time():
                    _band_tod = "midday"
                elif _et_time < datetime.strptime("15:30", "%H:%M").time():
                    _band_tod = "afternoon"
                else:
                    _band_tod = "close"
            except Exception:
                pass

            # Map category → catalyst key for CATALYST_WIDEN lookup
            _band_catalyst = candidate.metadata.get("category")  # "earnings", "analyst", "fda", etc.

            bands = compute_dynamic_bands(
                atr_pct=candidate.metadata.get("atr_pct"),
                drift_pct=float(candidate.metadata.get("drift_pct", 0.0)),
                rvol=float(candidate.metadata.get("rvol", 1.0)),
                beta=band_beta,
                catalyst=_band_catalyst,
                time_of_day=_band_tod,
                spread_pct=float(candidate.metadata.get("spread_pct", 0.0)),
            )
            band_time_profile = "regular"
            for token in bands.reasoning.split("; "):
                if token.startswith("time_profile="):
                    band_time_profile = token.split("=", 1)[1]
                    break

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
                    protective_stop_pct=bands.stop_pct,
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
            submission_metadata = submission.metadata or {}
            position = self.repository.positions.create_open(
                symbol=allocation.symbol,
                quantity=quantity,
                entry_price=entry_price,
                target_price=entry_price * (1 + bands.target_pct),
                stop_price=entry_price * (1 - bands.stop_pct),
                slot_id=allocation.slot_id,
                opened_at=allocation.reserved_at,
                metadata={
                    "sector": allocation.sector,
                    "broker_order_id": submission.broker_order_id,
                    "protective_stop_order_id": submission_metadata.get("protective_stop_order_id"),
                    "protective_stop_price": submission_metadata.get("protective_stop_price"),
                    "protective_stop_pct": submission_metadata.get("protective_stop_pct"),
                    "reference_price": reference_price,
                    "current_price": entry_price,
                    "entry_ts": allocation.reserved_at.isoformat(),
                    "entry_price": entry_price,
                    "slot_value_multiplier": slot_value_multiplier,
                    "effective_slot_value": effective_slot_value,
                    "band_target_pct": bands.target_pct,
                    "band_stop_pct": bands.stop_pct,
                    "band_reasoning": bands.reasoning,
                    "band_beta": band_beta,
                    "band_beta_profile": band_beta_profile,
                    "band_catalyst_profile": band_catalyst_profile,
                    "band_time_profile": band_time_profile,
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

    async def apply_eod_dilution(self):
        """Tighten EOD risk controls during the 15:15 ET fade window."""
        from driftpilot.state_machine import PositionMonitorResult

        now = self.clock.now_utc()
        positions = self.repository.positions.list_open()
        if not positions:
            return PositionMonitorResult(
                open_positions=0,
                metadata={
                    "source": "eod_dilution",
                    "checked_at": now.isoformat(),
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
                    "checked_at": now.isoformat(),
                    "active": False,
                    "reason": "before_1515_et",
                },
            )

        snapshots: list[EodPositionSnapshot] = []
        for position in positions:
            symbol = (position.symbol or "").upper()
            quote = None
            try:
                quote = await asyncio.wait_for(
                    asyncio.to_thread(self.quote_provider.latest_quote, symbol),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[EOD] dilution quote fetch for %s timed out", symbol)
            if quote is None:
                metadata = dict(position.metadata or {})
                current_price = float(
                    metadata.get("bid_price")
                    or metadata.get("current_price")
                    or metadata.get("reference_price")
                    or position.entry_price
                )
            else:
                current_price = quote.bid_price if quote.bid_price > 0 else (
                    quote.bid_price + quote.ask_price
                ) / 2.0
            unrealized_pct = (
                ((current_price - float(position.entry_price)) / float(position.entry_price)) * 100.0
                if float(position.entry_price) > 0
                else 0.0
            )
            sector = str((position.metadata or {}).get("sector") or "Unknown")
            snapshots.append(EodPositionSnapshot(position, current_price, unrealized_pct, sector))

        median_unrealized_pct = _median([snapshot.unrealized_pct for snapshot in snapshots])
        tightened = 0
        for snapshot in snapshots:
            new_stop = _tightened_eod_stop_price(
                current_price=snapshot.current_price,
                current_stop=float(snapshot.position.stop_price),
                step_count=step_count,
                lock_to_bid=snapshot.unrealized_pct > median_unrealized_pct,
            )
            if new_stop <= float(snapshot.position.stop_price):
                continue
            metadata = {
                "eod_dilution_active": True,
                "eod_dilution_step_count": step_count,
                "eod_dilution_stop_price": new_stop,
                "eod_dilution_reference_price": snapshot.current_price,
                "eod_dilution_unrealized_pct": snapshot.unrealized_pct,
                "eod_dilution_median_unrealized_pct": median_unrealized_pct,
            }
            self.repository.positions.update_stop_price(
                snapshot.position.id,
                stop_price=new_stop,
                metadata=metadata,
            )
            if snapshot.position.slot_id is not None:
                slot = self.repository.slots.get(snapshot.position.slot_id)
                slot_metadata = {**(slot.metadata if slot else {}), **metadata}
                self.repository.slots.upsert(
                    snapshot.position.slot_id,
                    status="OPEN",
                    symbol=snapshot.position.symbol,
                    position_id=snapshot.position.id,
                    slot_value=self.settings.slot_value,
                    metadata=slot_metadata,
                    updated_at=now,
                )
            tightened += 1

        sector_exits = 0
        for snapshot in _least_profitable_concentrated_sector_positions(snapshots):
            try:
                closed = await self._force_exit_position(
                    snapshot.position,
                    reference_price=snapshot.current_price,
                    exit_reason="EOD_SECTOR_DILUTION",
                )
            except Exception as exc:
                logger.warning("[EOD] sector dilution exit failed for %s: %s", snapshot.position.symbol, exc)
                closed = 0
            sector_exits += closed

        return PositionMonitorResult(
            open_positions=len(self.repository.positions.list_open()),
            exit_orders=sector_exits,
            recycled_slots=sector_exits,
            halted_reason=None,
            metadata={
                "source": "eod_dilution",
                "checked_at": now.isoformat(),
                "active": True,
                "step_count": step_count,
                "tightened": tightened,
                "median_unrealized_pct": median_unrealized_pct,
                "sector_exits": sector_exits,
            },
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
        alpaca_symbols = {(p.symbol or "").upper() for p in alpaca_positions}

        added = 0
        for position in local_open:
            sym = (position.symbol or "").upper()
            if sym in alpaca_symbols:
                continue
            metadata = dict(position.metadata or {})
            stop_order_id = metadata.get("protective_stop_order_id")
            if not stop_order_id:
                continue
            try:
                fill_price = await self.broker.get_fill_price(stop_order_id)
            except Exception as exc:
                logger.warning(
                    "monitor reconcile: protective stop fill lookup failed for %s/%s: %s",
                    sym,
                    stop_order_id,
                    exc,
                )
                continue
            if fill_price is None:
                fill_price = metadata.get("protective_stop_price")
            if fill_price is None:
                continue
            realized = (float(fill_price) - float(position.entry_price)) * float(position.quantity)
            try:
                self.repository.positions.close(
                    position.id,
                    exit_reason="broker_protective_stop_filled",
                    realized_pnl=realized,
                    closed_at=self.clock.now_utc(),
                    metadata={
                        "exit_price": float(fill_price),
                        "broker_exit_order_id": stop_order_id,
                        "exit_close_path": "protective_stop_reconcile",
                    },
                )
                if position.slot_id is not None:
                    self.repository.slots.free_slot(
                        position.slot_id,
                        slot_value=self.settings.slot_value,
                        reason="protective_stop_filled",
                        last_symbol=sym,
                        updated_at=self.clock.now_utc(),
                    )
                added += 1
                local_symbols.discard(sym)
                logger.info(
                    "monitor reconcile: closed local %s from filled protective stop %s",
                    sym,
                    stop_order_id,
                )
            except Exception as exc:
                logger.warning("monitor reconcile: failed to close stopped %s: %s", sym, exc)

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
            slot_md = dict(getattr(slot, "metadata", {}) or {})
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

    async def _force_exit_position(self, position, *, reference_price: float, exit_reason: str) -> int:
        symbol = (position.symbol or "").upper()
        quantity = float(getattr(position, "quantity", 0.0))
        if quantity <= 0:
            return 0
        metadata = dict(position.metadata or {})
        stop_oid = metadata.get("protective_stop_order_id")
        if stop_oid:
            try:
                await self.broker.cancel_order(stop_oid)
            except Exception as exc:
                logger.warning("[EOD] protective stop cancel failed for %s/%s: %s", symbol, stop_oid, exc)
        result = await self.broker.submit_exit_order(
            symbol=symbol,
            quantity=quantity,
            position_id=position.id,
        )
        if not result or not result.submitted:
            return 0
        exit_price = float(result.limit_price or reference_price)
        realized = (exit_price - float(position.entry_price)) * quantity
        self.repository.positions.close(
            position.id,
            exit_reason=exit_reason,
            realized_pnl=realized,
            closed_at=self.clock.now_utc(),
            metadata={
                "exit_price": exit_price,
                "broker_exit_order_id": result.broker_order_id,
                "exit_close_path": exit_reason.lower(),
            },
        )
        if position.slot_id is not None:
            self.repository.slots.free_slot(
                position.slot_id,
                slot_value=self.settings.slot_value,
                reason=exit_reason,
                last_symbol=symbol,
                updated_at=self.clock.now_utc(),
            )
        return 1

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
            # Use bid price for unrealized P&L — exit fills at bid (minus
            # offset), so mid overstates by half the spread.  This prevents
            # PROFIT_TAKE from firing on positions that would actually fill
            # below target.
            exit_ref = quote.bid_price if quote.bid_price > 0 else mid
            entry_price = float(getattr(position, "entry_price", 0.0))
            unrealized_pct = ((exit_ref - entry_price) / entry_price * 100.0) if entry_price > 0 else 0.0

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
            position_metadata["current_price"] = exit_ref
            position_metadata["mid_price"] = mid  # keep mid for reference
            position_metadata["bid_price"] = quote.bid_price
            position_metadata["ask_price"] = quote.ask_price
            position_metadata["peak_unrealized_pct"] = new_peak
            try:
                object.__setattr__(position, "metadata", position_metadata)
                if hasattr(position, "current_price"):
                    object.__setattr__(position, "current_price", exit_ref)
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
            protective_stop_order_id = position_metadata.get("protective_stop_order_id")
            try:
                if protective_stop_order_id:
                    await self.broker.cancel_order(protective_stop_order_id)
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
                        self.repository.slots.free_slot(
                            slot_id,
                            slot_value=self.settings.slot_value,
                            reason=exit_reason,
                            last_symbol=symbol,
                            updated_at=self.clock.now_utc(),
                        )
                    except Exception as slot_exc:
                        logger.warning("LIVE: slot %s free failed: %s", slot_id, slot_exc)
                # Reset price-drift baseline so the symbol isn't blocked
                # from re-entry by stale drift from the morning observation.
                try:
                    exit_price = float(position_metadata.get("current_price", mid))
                    n_reset = self.repository.price_drift_baselines.reset_all_baselines_for_symbol(
                        symbol, exit_price, at=self.clock.now_utc(),
                    )
                    if n_reset:
                        logger.info(
                            "LIVE: reset %d drift baselines for %s at $%.2f after exit",
                            n_reset, symbol, exit_price,
                        )
                except Exception as drift_exc:
                    logger.warning("LIVE: drift baseline reset failed for %s: %s", symbol, drift_exc)

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

    async def final_drain_all(self):
        """Final 15:50 ET drain: submit broker exits for every open position."""
        from driftpilot.state_machine import PositionMonitorResult

        positions = self.repository.positions.list_open()
        if not positions:
            return PositionMonitorResult(
                open_positions=0,
                metadata={
                    "source": "final_drain",
                    "reason": "no_open_positions",
                    "checked_at": self.clock.now_utc().isoformat(),
                },
            )

        closed = 0
        for position in positions:
            metadata = dict(position.metadata or {})
            reference_price = float(
                metadata.get("bid_price")
                or metadata.get("current_price")
                or metadata.get("reference_price")
                or position.entry_price
            )
            try:
                closed += await self._force_exit_position(
                    position,
                    reference_price=reference_price,
                    exit_reason="FINAL_DRAIN",
                )
            except Exception as exc:
                logger.warning("[FINAL_DRAIN] exit failed for %s: %s", position.symbol, exc)

        return PositionMonitorResult(
            open_positions=len(self.repository.positions.list_open()),
            exit_orders=closed,
            recycled_slots=closed,
            halted_reason=None,
            metadata={
                "source": "final_drain",
                "reason": "after_1550_et",
                "checked_at": self.clock.now_utc().isoformat(),
            },
        )

    async def eod_liquidate_all(self) -> int:
        """End-of-day: close every open position at market and free its slot.

        Called once when the market-close transition fires. For an intraday
        system, no position should survive overnight.  Submits real broker
        exit orders so the Alpaca side is flat before the operator shuts down.
        Returns the count of positions successfully closed.
        """
        positions = self.repository.positions.list_open()
        if not positions:
            logger.info("[EOD] no open positions — nothing to liquidate")
            return 0

        logger.info("[EOD] liquidating %d open positions", len(positions))
        closed = 0
        now = self.clock.now_utc()

        for position in positions:
            symbol = (position.symbol or "").upper()
            quantity = float(position.quantity)
            if quantity <= 0:
                continue

            # Best-effort broker exit — use market order semantics
            exit_price: float | None = None
            broker_oid: str | None = None
            try:
                # Cancel any protective stop first
                stop_oid = (position.metadata or {}).get("protective_stop_order_id")
                if stop_oid:
                    try:
                        await self.broker.cancel_order(stop_oid)
                    except Exception:
                        pass  # already cancelled or filled

                exit_result = await self.broker.submit_exit_order(
                    symbol=symbol,
                    quantity=quantity,
                    position_id=position.id,
                )
                if exit_result and exit_result.submitted:
                    exit_price = exit_result.limit_price
                    broker_oid = exit_result.broker_order_id
            except Exception as exc:
                logger.warning("[EOD] broker exit failed for %s: %s — using last known price", symbol, exc)

            # Fallback: use quote mid if broker didn't return a price
            if exit_price is None:
                try:
                    quote = await self.quote_provider.get_quote(symbol)
                    if quote and quote.bid_price and quote.ask_price:
                        exit_price = (quote.bid_price + quote.ask_price) / 2.0
                except Exception:
                    pass
            if exit_price is None:
                exit_price = float(position.entry_price)  # worst case: flat

            entry_price = float(position.entry_price)
            realized = (exit_price - entry_price) * quantity

            try:
                self.repository.positions.close(
                    position.id,
                    exit_reason="eod_liquidation",
                    realized_pnl=realized,
                    closed_at=now,
                    metadata={
                        "exit_price": exit_price,
                        "broker_exit_order_id": broker_oid,
                        "exit_close_path": "eod_liquidation",
                    },
                )
                if position.slot_id is not None:
                    self.repository.slots.free_slot(
                        position.slot_id,
                        slot_value=self.settings.slot_value,
                        reason="eod_liquidation",
                        last_symbol=symbol,
                        updated_at=now,
                    )
                closed += 1
                logger.info(
                    "[EOD] closed %s qty=%.0f entry=$%.2f exit=$%.2f realized=$%.2f slot=%s",
                    symbol, quantity, entry_price, exit_price, realized,
                    position.slot_id,
                )
            except Exception as exc:
                logger.error("[EOD] failed to close %s locally: %s", symbol, exc, exc_info=True)

        logger.info("[EOD] liquidation complete: %d/%d positions closed", closed, len(positions))
        return closed


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
