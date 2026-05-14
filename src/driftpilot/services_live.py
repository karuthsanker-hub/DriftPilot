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
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from driftpilot.broker.alpaca_client import AlpacaBrokerClient, OrderSubmissionResult
from driftpilot.clock import DriftPilotClock, market_time_profile
from driftpilot.execution.slot_allocator import (
    AllocationCandidate,
    AllocationResult,
    SlotAllocator,
)
from driftpilot.market_data.rest_quotes import AlpacaRestQuoteProvider
from driftpilot.settings import DriftPilotSettings
from driftpilot.signals import get_signal
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
        # Defect #7 fix: include metadata the signals need (entry_ts,
        # entry_price, sector) so evaluate_exit() doesn't return None
        # and positions don't become zombies.
        try:
            # Look up sectors from universe.csv for reconciled positions
            sector_map = self._load_sector_map()
            now_iso = self.alpaca.clock.now_utc().isoformat() if hasattr(self.alpaca, "clock") else __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()

            def _build_pos(p):
                sym = getattr(p, "symbol", "").upper()
                entry = float(getattr(p, "avg_entry_price", 0) or getattr(p, "average_entry_price", 0) or 0)
                return {
                    "symbol": sym,
                    "quantity": float(getattr(p, "quantity", 0)),
                    "entry_price": entry,
                    "avg_entry_price": entry,
                    "metadata": {
                        "reconciled": "broker_truth_at_boot",
                        "entry_ts": now_iso,
                        "entry_price": entry,
                        "sector": sector_map.get(sym, "Unknown"),
                        "signal_name": "reconciled_boot",
                    },
                }

            return self.repository.positions.reconcile_broker_open_positions(
                broker_positions=[_build_pos(p) for p in broker_positions],
                slot_value=self.settings.slot_value,
                target_pct=self.settings.target_pct,
                stop_pct=self.settings.stop_pct,
                trade_slots=self.settings.trade_slots,
            )
        except Exception as exc:
            logger.warning("repo reconcile failed: %s — continuing with no-op", exc)
            return "live_reconcile_noop"

    def _load_sector_map(self) -> dict[str, str]:
        """Load symbol→sector mapping from universe.csv."""
        sector_map: dict[str, str] = {}
        try:
            universe_file = getattr(self.settings, "universe_file", "config/universe.csv")
            with open(universe_file) as f:
                header = next(f, None)
                if header is None:
                    return sector_map
                # Find sector column index
                cols = [c.strip().lower() for c in header.split(",")]
                sym_idx = 0
                sec_idx = cols.index("sector") if "sector" in cols else -1
                if sec_idx < 0:
                    return sector_map
                for line in f:
                    parts = line.split(",")
                    if len(parts) > sec_idx:
                        sym = parts[sym_idx].strip().upper()
                        sec = parts[sec_idx].strip()
                        if sym and sec:
                            sector_map[sym] = sec
        except Exception as exc:
            logger.warning("sector map load failed: %s", exc)
        return sector_map

    # Pass-through for any other broker calls the state machine might make
    def __getattr__(self, name):
        return getattr(self.alpaca, name)


class MultiSignal:
    """Fan-out/fan-in aggregator over N catalyst signals.

    The single-signal architecture forces a tradeoff: pick the validated
    earnings cell (5.09 ratio, low N/day) or the broader filing_8a (2.05
    ratio, ~10x more flow). This wrapper runs both in parallel:

    - subscribe()  → subscribes every sub-signal to its own bus topic
    - bootstrap_from_db() → fans out per sub-signal
    - scan()  → concatenates candidates from each (each tags its own signal_name
                in `features` so the scanner can stamp position metadata for
                exit routing)
    - evaluate_exit(position, now) → looks up `metadata["signal_name"]` and
                delegates to the matching sub-signal. Falls back to the first
                sub-signal if name missing (back-compat with positions opened
                before MultiSignal was wired).

    Each sub-signal owns its own _config, so they can have different
    max_hold / profit_take / stop_loss. That's the whole point — different
    cells have different optimal hold periods.
    """

    name: str = "multi_signal"

    def __init__(self, signals: list[Any]) -> None:
        if not signals:
            raise ValueError("MultiSignal requires at least one sub-signal")
        self._signals = signals
        self._by_name: dict[str, Any] = {
            getattr(s, "name", None) or s.__class__.__name__: s for s in signals
        }

    @property
    def signals(self) -> list[Any]:
        return list(self._signals)

    @property
    def _config(self):  # for hot-reload compatibility (no-op shim)
        return getattr(self._signals[0], "_config", None)

    @_config.setter
    def _config(self, value):
        # Hot-reload of earnings config affects only sub-signals that accept
        # the same config type.
        for s in self._signals:
            try:
                if isinstance(getattr(s, "_config", None), type(value)):
                    s._config = value
            except Exception:
                # Some signals expose read-only or incompatible config
                # attributes; leaving them unchanged is intentional.
                pass

    async def subscribe(self) -> None:
        for s in self._signals:
            sub = getattr(s, "subscribe", None)
            if sub is None:
                continue
            res = sub()
            if hasattr(res, "__await__"):
                await res

    def bootstrap_from_db(self, db_path: str) -> int:
        total = 0
        for s in self._signals:
            boot = getattr(s, "bootstrap_from_db", None)
            if boot is None:
                continue
            try:
                total += int(boot(db_path) or 0)
            except Exception as exc:
                logger.warning("MultiSignal bootstrap %s failed: %s", s.name, exc)
        return total

    async def scan(self, now=None):
        import inspect as _inspect
        out: list[Any] = []
        for s in self._signals:
            try:
                res = s.scan(now=now) if "now" in _inspect.signature(s.scan).parameters else s.scan()
                cands = await res if _inspect.isawaitable(res) else res
            except Exception as exc:
                logger.exception("MultiSignal: %s.scan raised: %s", s.name, exc)
                continue
            for c in cands or []:
                # Ensure features carry signal_name so the scanner can stamp it
                feats = dict(getattr(c, "features", None) or {})
                feats.setdefault("signal_name", getattr(s, "name", None))
                # Candidate is a frozen-ish dataclass — replace via constructor
                try:
                    c = c.__class__(
                        symbol=c.symbol, score=c.score, sector=c.sector,
                        allowed=c.allowed, blocked_reason=c.blocked_reason,
                        features=feats,
                    )
                except Exception:
                    # If a candidate cannot be reconstructed, keep the
                    # original candidate rather than dropping the event.
                    pass
                out.append(c)
        return out

    def evaluate_exit(self, position, now, *args, **kwargs):
        metadata = getattr(position, "metadata", {}) or {}
        sig_name = metadata.get("signal_name")
        target = self._by_name.get(sig_name) if sig_name else None
        if target is None:
            # Fall back to the first sub-signal (covers positions opened
            # before MultiSignal stamped signal_name into metadata).
            target = self._signals[0]
        try:
            return target.evaluate_exit(position, now, *args, **kwargs)
        except Exception as exc:
            logger.exception("MultiSignal.evaluate_exit %s raised: %s", sig_name, exc)
            return None


@dataclass(frozen=True)
class _RoutingEventStub:
    """Lightweight event stub for the signal router.

    Carries just the fields the router needs — avoids importing CatalystEvent
    into services_live.py (which would create a circular dependency risk).
    """
    category: str
    subcategory: str
    sentiment: str | None = None
    priority_modifier: float = 0.0


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
        signal: Any,
        quote_provider: Any,
        clock: DriftPilotClock,
        universe_path: str | None = None,
        runtime_config_path: str | None = None,
        router: Any | None = None,
        repository: Any | None = None,
    ) -> None:
        self.signal = signal
        self.quote_provider = quote_provider
        self.clock = clock
        self._router = router  # Optional Phase 1 signal router
        self._repository = repository  # For blocked-symbol pre-filter
        # Hot-reload tracking — only re-read the file when its mtime changes.
        self._runtime_config_path = runtime_config_path
        self._runtime_config_mtime: float = 0.0
        self._scanning_paused: bool = False
        # Lazy-load real sectors so catalyst candidates spread across the
        # allocator's per-sector cap. Otherwise all our candidates end up in
        # "Unknown" and cap fires after 3.
        # Price drift protection: record the first-seen price for each
        # symbol-event pair. If price drifts beyond max_price_drift_pct from
        # the first-seen price, skip the candidate — we'd be chasing a move
        # that already happened. Keyed by (symbol, headline_hash) so a new
        # event on the same symbol resets the baseline.
        self._first_seen_prices: dict[tuple[str, str], float] = {}
        self._max_price_drift_pct: float = 3.0  # default; hot-reloaded
        self._max_entry_atr_pct: float = 6.0
        self._high_volatility_slot_multiplier: float = 0.5
        # Blocked symbol cache: symbols that the allocator permanently rejects
        # (day-cap, consecutive-loss cooldown) are cached here so the scanner
        # skips them without fetching a quote or sending to the allocator.
        # Cleared on each new trading day (UTC date).
        self._blocked_symbols: set[str] = set()
        self._blocked_date: str = ""  # YYYY-MM-DD; reset when date changes
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
            from driftpilot.signals.analyst_target_raise_v1.config import AnalystTargetRaiseConfig
            cfg = load_runtime_config(p)
            require_sent = cfg.earnings_require_sentiment
            new_earnings_cfg = EarningsReportConfig(
                max_hold_minutes=cfg.earnings_max_hold_minutes,
                profit_take_pct=cfg.earnings_profit_take_pct,
                stop_loss_pct=cfg.earnings_stop_loss_pct,
                max_event_age_minutes=cfg.earnings_max_event_age_minutes,
                require_sentiment=None if require_sent == "any" else require_sent,
                trailing_enabled=str(cfg.earnings_trailing_enabled).lower() == "true",
                trailing_activation_pct=cfg.earnings_trailing_activation_pct,
                trailing_distance_pct=cfg.earnings_trailing_distance_pct,
            )
            new_analyst_cfg = AnalystTargetRaiseConfig(
                max_hold_minutes=cfg.earnings_max_hold_minutes,
                profit_take_pct=0.8,   # locked spec from catalyst_horizons
                stop_loss_pct=1.0,     # locked spec from catalyst_horizons
                max_event_age_minutes=cfg.earnings_max_event_age_minutes,
                require_sentiment=None if require_sent == "any" else require_sent,
            )
            # Hot-reload: update each sub-signal's config by type.
            # MultiSignal._config setter fans out to compatible sub-signals.
            if hasattr(self.signal, "_config"):
                self.signal._config = new_earnings_cfg  # type: ignore[attr-defined]
            # Also reload analyst config for MultiSignal sub-signals
            if hasattr(self.signal, "_signals"):
                for s in self.signal._signals:
                    if isinstance(getattr(s, "config", None), AnalystTargetRaiseConfig):
                        s.config = new_analyst_cfg
            # Hot kill switch
            self._scanning_paused = str(cfg.scanning_paused).lower() == "true"
            self._max_price_drift_pct = cfg.max_price_drift_pct
            self._max_entry_atr_pct = cfg.max_entry_atr_pct
            self._high_volatility_slot_multiplier = cfg.high_volatility_slot_multiplier
            self._runtime_config_mtime = mtime
            logger.info(
                "🔄 hot-reloaded: max_hold=%dm profit=%.2f%% stop=%.2f%% "
                "max_age=%dm sentiment=%s max_drift=%.1f%% max_atr=%.1f%% "
                "vol_slot_mult=%.2f scanning_paused=%s",
                cfg.earnings_max_hold_minutes, cfg.earnings_profit_take_pct,
                cfg.earnings_stop_loss_pct, cfg.earnings_max_event_age_minutes,
                require_sent, cfg.max_price_drift_pct, cfg.max_entry_atr_pct,
                cfg.high_volatility_slot_multiplier, cfg.scanning_paused,
            )
        except Exception as exc:
            logger.warning("hot-reload failed: %s", exc)

    def _candidate_atr_pct(self, symbol: str, features: dict[str, Any]) -> float | None:
        """Read ATR percentage from feature or context metadata if available."""
        for key in ("atr_pct", "entry_atr_pct", "atr_percent", "atr_20d_pct"):
            val = features.get(key)
            if val is not None:
                return self._coerce_positive_float(val)

        context = self._candidate_context_metadata(features)
        if context is None:
            context = self._candidate_context_from_db(symbol, features)
        if isinstance(context, dict):
            for key in ("atr_pct", "entry_atr_pct", "atr_percent", "atr_20d_pct"):
                val = context.get(key)
                if val is not None:
                    return self._coerce_positive_float(val)
        return None

    def _candidate_beta(self, symbol: str, features: dict[str, Any]) -> float | None:
        """Read beta from feature or enrichment context metadata if available."""
        val = features.get("beta")
        if val is not None:
            return self._coerce_positive_float(val)

        context = self._candidate_context_metadata(features)
        if context is None:
            context = self._candidate_context_from_db(symbol, features)
        if isinstance(context, dict):
            val = context.get("beta")
            if val is not None:
                return self._coerce_positive_float(val)
        return None

    def _candidate_context_metadata(self, features: dict[str, Any]) -> dict[str, Any] | None:
        context = (
            features.get("context")
            or features.get("context_metadata")
            or features.get("enrichment_context")
            or features.get("context_json")
        )
        if isinstance(context, str):
            try:
                decoded = json.loads(context)
            except json.JSONDecodeError as exc:
                logger.debug("candidate context_json did not decode: %s", exc)
                return None
            context = decoded
        if isinstance(context, dict):
            return context
        return None

    def _candidate_context_from_db(
        self,
        symbol: str,
        features: dict[str, Any],
    ) -> dict[str, Any] | None:
        headline_hash = features.get("headline_hash")
        if not headline_hash:
            return None
        for db_path in self._candidate_context_db_paths():
            try:
                import sqlite3

                conn = sqlite3.connect(db_path)
                try:
                    columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(catalyst_events)").fetchall()
                    }
                    if "context_json" not in columns:
                        continue
                    row = conn.execute(
                        "SELECT context_json FROM catalyst_events "
                        "WHERE symbol = ? AND headline_hash = ? "
                        "ORDER BY event_ts DESC LIMIT 1",
                        (symbol.upper(), str(headline_hash)),
                    ).fetchone()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                logger.debug("candidate context lookup failed for %s: %s", symbol, exc)
                continue
            if row is None or not row[0]:
                continue
            try:
                decoded = json.loads(row[0])
            except json.JSONDecodeError as exc:
                logger.debug("candidate context_json from DB did not decode for %s: %s", symbol, exc)
                continue
            if isinstance(decoded, dict):
                features["context_json"] = row[0]
                return decoded
        return None

    def _candidate_context_db_paths(self) -> tuple[str, ...]:
        paths: list[str] = []
        db_path = getattr(self.signal, "_db_path", None)
        if isinstance(db_path, str) and db_path:
            paths.append(db_path)
        for sub_signal in getattr(self.signal, "_signals", ()) or ():
            sub_path = getattr(sub_signal, "_db_path", None)
            if isinstance(sub_path, str) and sub_path and sub_path not in paths:
                paths.append(sub_path)
        return tuple(paths)

    def _coerce_positive_float(self, value: Any) -> float | None:
        try:
            coerced = float(value)
        except (TypeError, ValueError):
            # Bad ATR metadata is treated as unavailable; missing ATR must not block entries.
            return None
        if not math.isfinite(coerced) or coerced < 0:
            return None
        return coerced

    def _mark_candidate_blocked(
        self,
        symbol: str,
        reason: str,
        features: dict[str, Any],
        updated_at: datetime,
    ) -> None:
        if self._repository is None:
            return
        candidate_queue = getattr(self._repository, "candidate_queue", None)
        if candidate_queue is None:
            return
        mark_blocked = getattr(candidate_queue, "mark_blocked", None)
        if mark_blocked is None:
            return
        try:
            mark_blocked(
                symbol,
                reason=reason,
                features=self._json_safe_metadata(features),
                updated_at=updated_at,
            )
        except Exception as exc:
            logger.warning("candidate_queue mark_blocked failed for %s: %s", symbol, exc)

    def _json_safe_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, value in metadata.items():
            if hasattr(value, "isoformat"):
                safe[key] = value.isoformat()
            elif isinstance(value, dict):
                safe[key] = self._json_safe_metadata(value)
            elif isinstance(value, (list, tuple)):
                safe[key] = [
                    item.isoformat() if hasattr(item, "isoformat") else item
                    for item in value
                ]
            else:
                safe[key] = value
        return safe

    def _refresh_blocked_symbols(self, now: datetime) -> None:
        """Rebuild the set of symbols that can't be allocated RIGHT NOW.

        Rebuilt from scratch each cycle (not accumulated) so that symbols
        become eligible again when the blocking condition clears — e.g.
        a slot goes EMPTY, the reentry cooldown expires, or a winning
        trade breaks a consecutive-loss streak.

        Includes:
        - Symbols currently in active slots (duplicate_symbol)
        - Symbols at the max_trades_per_symbol_per_day cap
        - Symbols in consecutive-loss cooldown (3+ consecutive losses)
        - Symbols in reentry cooldown (exited < min_reentry_minutes ago)
        """
        today_str = now.strftime("%Y-%m-%d")
        self._blocked_date = today_str

        if self._repository is None:
            self._blocked_symbols = set()
            return

        # Rebuild from scratch — never accumulate
        blocked: set[str] = set()

        try:
            slots = self._repository.slots.list_all()
            # Symbols currently in active slots → duplicate_symbol
            for slot in slots:
                if slot.symbol and (slot.status or "").upper() not in ("EMPTY",):
                    blocked.add(slot.symbol.upper())

            # Symbols at the per-day trade cap
            from driftpilot.runtime_config import load_runtime_config
            rcfg = load_runtime_config()
            max_per_day = rcfg.max_trades_per_symbol_per_day
            rows = self._repository.connection.execute(
                "SELECT symbol, COUNT(*) AS n FROM positions "
                "WHERE closed_at >= ? GROUP BY symbol HAVING n >= ?",
                (today_str, max_per_day),
            ).fetchall()
            for r in rows:
                blocked.add(r["symbol"].upper())

            # Symbols in consecutive-loss cooldown (3+ consecutive losses today)
            traded = self._repository.connection.execute(
                "SELECT DISTINCT symbol FROM positions WHERE closed_at >= ?",
                (today_str,),
            ).fetchall()
            for r in traded:
                sym = r["symbol"].upper()
                if sym in blocked:
                    continue  # already blocked
                losses = self._repository.connection.execute(
                    "SELECT realized_pnl FROM positions "
                    "WHERE symbol = ? AND status = 'closed' AND closed_at >= ? "
                    "ORDER BY closed_at DESC LIMIT 3",
                    (sym, today_str),
                ).fetchall()
                if len(losses) >= 3 and all(float(l["realized_pnl"]) <= 0 for l in losses):
                    blocked.add(sym)

            # Reentry cooldown — block symbols exited too recently
            min_reentry = rcfg.min_reentry_minutes
            if min_reentry > 0:
                cutoff_ts = (now - timedelta(minutes=min_reentry)).isoformat()
                recent_exits = self._repository.connection.execute(
                    "SELECT DISTINCT symbol FROM positions "
                    "WHERE status = 'closed' AND closed_at >= ? AND closed_at > ?",
                    (today_str, cutoff_ts),
                ).fetchall()
                for r in recent_exits:
                    blocked.add(r["symbol"].upper())

        except Exception as exc:
            logger.warning("blocked-symbol refresh failed: %s", exc)

        self._blocked_symbols = blocked

    def _record_price_drift_baseline(
        self,
        *,
        symbol: str,
        event_key: str,
        ref_price: float,
        seen_at: datetime,
        features: Any,
    ) -> tuple[float, float]:
        """Persist or memoize the first-seen price for a symbol/event pair."""
        normalized_symbol = symbol.upper()
        metadata = {
            "headline": features.get("headline"),
            "headline_hash": event_key,
            "category": features.get("category"),
            "subcategory": features.get("subcategory"),
            "source": features.get("source", "catalyst_bus"),
            "signal_name": features.get("signal_name")
            or getattr(self.signal, "name", None),
        }
        drift_repo = (
            getattr(self._repository, "price_drift_baselines", None)
            if self._repository is not None
            else None
        )
        if drift_repo is not None:
            baseline = drift_repo.update_seen(
                symbol=normalized_symbol,
                event_key=event_key,
                price=ref_price,
                seen_at=seen_at,
                metadata=metadata,
            )
            return baseline.first_seen_price, baseline.drift_pct

        drift_key = (normalized_symbol, event_key)
        first_price = self._first_seen_prices.get(drift_key)
        if first_price is None:
            self._first_seen_prices[drift_key] = ref_price
            first_price = ref_price
        drift_pct = (
            abs(ref_price - first_price) / first_price * 100.0
            if first_price > 0
            else 0.0
        )
        return first_price, drift_pct

    async def scan(self):
        self._maybe_hot_reload()
        now = self.clock.now_utc()
        candidates: list[AllocationCandidate] = []
        # Hot-reloadable kill switch — UI sets scanning_paused=true to halt
        # new entries without stopping the operator. Existing positions are
        # still managed by the monitor.
        if getattr(self, "_scanning_paused", False):
            logger.info("catalyst scanner: scanning_paused=true (UI lever) — 0 candidates")
            return _build_scan_result([], now)
        # Periodic cleanup of the in-memory fallback only. Repo-backed
        # baselines are intentionally persistent across operator restarts.
        if self._repository is None and len(self._first_seen_prices) > 500:
            self._first_seen_prices.clear()

        # Pre-filter: refresh the set of blocked symbols (day-cap, open,
        # consecutive-loss) BEFORE calling signal.scan(). Candidates for
        # blocked symbols are skipped without fetching a quote, eliminating
        # the 5700+ wasted rejections/day pattern.
        self._refresh_blocked_symbols(now)

        try:
            import inspect
            res = self.signal.scan(now=now)
            sig_candidates = await res if inspect.isawaitable(res) else res
        except Exception as exc:
            logger.exception("catalyst scanner: signal.scan raised: %s", exc)
            return _build_scan_result([], now)

        skipped_blocked = 0
        for rank, sc in enumerate(sig_candidates, start=1):
            # Skip symbols that we already know the allocator will reject.
            if sc.symbol.upper() in self._blocked_symbols:
                skipped_blocked += 1
                continue
            quote = await asyncio.to_thread(self.quote_provider.latest_quote, sc.symbol)
            if quote is None:
                logger.info(
                    "catalyst scanner: no live quote for %s — skipping (broker would reject)",
                    sc.symbol,
                )
                continue
            ref_price = (quote.bid_price + quote.ask_price) / 2.0
            features = dict(sc.features or {})

            # --- Price drift protection ---
            # Track the first-seen price for each (symbol, headline_hash).
            # If the stock has already moved beyond max_price_drift_pct from
            # first-seen, skip — we'd be buying the top of an already-played move.
            _hh = str(features.get("headline_hash") or "")
            first_price, drift_pct = self._record_price_drift_baseline(
                symbol=sc.symbol,
                event_key=_hh,
                ref_price=ref_price,
                seen_at=now,
                features=features,
            )
            max_drift = getattr(self, "_max_price_drift_pct", 3.0)
            if drift_pct > max_drift:
                logger.warning(
                    "DRIFT REJECT %s: price moved %.1f%% from first-seen $%.2f → $%.2f "
                    "(max_drift=%.1f%%) | %s",
                    sc.symbol, drift_pct, first_price, ref_price, max_drift,
                    (features.get("headline") or "")[:80],
                )
                continue

            atr_pct = self._candidate_atr_pct(sc.symbol, features)
            beta = self._candidate_beta(sc.symbol, features)
            max_atr = getattr(self, "_max_entry_atr_pct", 6.0)
            slot_value_multiplier = 1.0
            if atr_pct is not None and atr_pct > max_atr:
                detail = {
                    **features,
                    "atr_pct": atr_pct,
                    "max_entry_atr_pct": max_atr,
                    "reference_price": ref_price,
                    "blocked_reason": "high_volatility_atr",
                }
                logger.warning(
                    "ATR REJECT %s: atr_pct=%.2f%% exceeds max_entry_atr_pct=%.2f%% | %s",
                    sc.symbol, atr_pct, max_atr,
                    (features.get("headline") or "")[:80],
                )
                self._mark_candidate_blocked(sc.symbol, "high_volatility_atr", detail, now)
                continue
            if atr_pct is not None and atr_pct >= max_atr * 0.75:
                slot_value_multiplier = max(
                    0.1,
                    min(1.0, float(getattr(self, "_high_volatility_slot_multiplier", 0.5))),
                )
                features["slot_value_multiplier"] = slot_value_multiplier

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
                    "priority_modifier": features.get("priority_modifier"),
                    "category": features.get("category"),
                    "subcategory": features.get("subcategory"),
                    "event_age_minutes": features.get("event_age_minutes"),
                    "horizon_minutes": features.get("horizon_minutes"),
                    "source": features.get("source", "catalyst_bus"),
                    "atr_pct": atr_pct,
                    "beta": beta,
                    "rvol": features.get("rvol"),  # relative volume (from volume_spike signal)
                    "slot_value_multiplier": slot_value_multiplier,
                    "price_drift_pct": round(drift_pct, 2),
                    "first_seen_price": first_price,
                    # signal_name lets the position monitor route evaluate_exit
                    # to the right signal under MultiSignal mode (mixed
                    # filing_8a + earnings_report etc).
                    "signal_name": features.get("signal_name") or getattr(self.signal, "name", None),
                },
            )
            candidates.append(ac)
            logger.info(
                "CANDIDATE %s rank=%d score=%+.2f sentiment=%s age=%.1fmin ref=%.2f drift=%.1f%% | %s",
                ac.symbol, rank, ac.score,
                features.get("sentiment") or "NONE",
                float(features.get("event_age_minutes") or 0),
                ref_price, drift_pct,
                (features.get("headline") or "")[:80],
            )

        # Phase 1 Signal Router: annotate candidates with routing decisions.
        # The router does NOT filter candidates yet (that's Phase 2 / PM work).
        # It adds audit metadata so downstream code and logs show what the
        # router would have decided.
        if self._router is not None:
            candidates = self._annotate_with_routing(candidates, now)

        if not candidates:
            if skipped_blocked > 0:
                logger.info(
                    "catalyst scanner: 0 candidates this cycle "
                    "(%d skipped as blocked: %s)",
                    skipped_blocked,
                    ", ".join(sorted(self._blocked_symbols)[:10]),
                )
            else:
                logger.info("catalyst scanner: 0 candidates this cycle (no admitted events)")
        elif skipped_blocked > 0:
            logger.info(
                "catalyst scanner: %d candidates, %d pre-filtered as blocked",
                len(candidates), skipped_blocked,
            )
        return _build_scan_result(candidates, now)

    def _annotate_with_routing(
        self,
        candidates: list[AllocationCandidate],
        now: datetime,
    ) -> list[AllocationCandidate]:
        """Run each candidate through the router, attach routing metadata.

        BLOCK decisions filter out the candidate (negative catalyst gate).
        ROUTE/DEFERRED/SKIP decisions annotate but pass through — the signal
        already decided this candidate is valid.
        """
        from driftpilot.signal_router import RoutingAction

        router = self._router
        if router is None:
            return candidates

        routed: list[AllocationCandidate] = []
        for ac in candidates:
            # Build a lightweight event-like object from candidate metadata
            meta = ac.metadata or {}
            evt = _RoutingEventStub(
                category=meta.get("category", ""),
                subcategory=meta.get("subcategory", ""),
                sentiment=meta.get("sentiment"),
                priority_modifier=float(meta.get("priority_modifier", 0.0) or 0.0),
            )
            # Skip routing if no category info (pre-router candidates)
            if not evt.category:
                routed.append(ac)
                continue

            try:
                decisions = router.route(evt, time_et=now)
            except Exception as exc:
                logger.warning("router failed for %s: %s — passing through", ac.symbol, exc)
                routed.append(ac)
                continue

            # Check for BLOCK — negative catalyst gate
            blocks = [d for d in decisions if d.action == RoutingAction.BLOCK]
            if blocks:
                logger.info(
                    "ROUTER BLOCK %s: %s (rule=%s, ttl=%dm)",
                    ac.symbol, blocks[0].reason, blocks[0].rule_id,
                    blocks[0].horizon_minutes,
                )
                continue  # drop this candidate

            # Annotate with routing info
            route_decisions = [d for d in decisions if d.action == RoutingAction.ROUTE]
            new_meta = dict(meta)
            if route_decisions:
                new_meta["routed_signal"] = route_decisions[0].signal_name
                new_meta["routing_rule_id"] = route_decisions[0].rule_id
                new_meta["routing_conviction"] = route_decisions[0].conviction
            new_meta["routing_decisions"] = [
                {"action": d.action.value, "signal": d.signal_name, "rule": d.rule_id}
                for d in decisions
            ]

            routed.append(AllocationCandidate(
                symbol=ac.symbol,
                score=ac.score,
                sector=ac.sector,
                latest_bar_at=ac.latest_bar_at,
                rank=ac.rank,
                metadata=new_meta,
            ))

        return routed


# ---------------------------------------------------------------------------
# Dynamic Entry / Exit Band Calculator
# ---------------------------------------------------------------------------
# Replaces fixed 1%/1% target/stop with bands that adapt to:
#   1. ATR (volatility) — wider stops for volatile stocks
#   2. Price drift — reduce target if stock already ran
#   3. Volume conviction (RVOL) — high volume = wider targets
#   4. Spread cost — wide spreads eat into profit
#
# Authority: This is BELOW the guardrail validator — the guardrail will
# still clamp any values that exceed MAX_STOP_LOSS_PCT / MAX_PROFIT_CAP_PCT.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DynamicBands:
    """Computed target and stop bands for a single entry."""
    target_pct: float   # e.g. 0.025 = 2.5%
    stop_pct: float     # e.g. 0.015 = 1.5%
    reasoning: str      # human-readable explanation for logs


def _coerce_float_or_none(value: Any | None, *, field_name: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.debug("ignoring invalid numeric %s value for dynamic bands: %r", field_name, value)
        return None


def _catalyst_band_profile(
    *,
    category: str | None,
    subcategory: str | None,
    signal_name: str | None,
) -> tuple[str, float, float]:
    category_key = (category or "").lower()
    subcategory_key = (subcategory or "").lower()
    signal_key = (signal_name or "").lower()
    if signal_key == "volume_spike_v1":
        return "volume_spike_v1", 1.15, 1.10
    if category_key == "earnings" and subcategory_key in {"report", "beat", "guidance_up"}:
        return f"{category_key}/{subcategory_key or 'generic'}", 1.35, 1.10
    if category_key == "analyst" and subcategory_key in {"target_raise", "upgrade", "initiates"}:
        return f"{category_key}/{subcategory_key}", 0.85, 0.90
    if category_key == "m_and_a":
        return f"{category_key}/{subcategory_key or '*'}", 1.60, 1.20
    if category_key == "product" and subcategory_key in {"launch", "partnership"}:
        return f"{category_key}/{subcategory_key}", 1.10, 1.00
    if category_key == "filing" and subcategory_key in {"8a", "8k"}:
        return f"{category_key}/{subcategory_key}", 1.00, 1.00
    return "baseline", 1.0, 1.0


def _beta_band_profile(beta: Any | None) -> tuple[str, float, float]:
    beta_value = _coerce_float_or_none(beta, field_name="beta")
    if beta_value is None or beta_value <= 0:
        return "unknown", 1.0, 1.0
    if beta_value >= 1.5:
        return "high_beta", 1.20, 1.20
    if beta_value < 0.8:
        return "low_beta", 0.85, 0.85
    return "normal_beta", 1.0, 1.0


def _apply_band_multiplier(
    target_pct: float,
    stop_pct: float,
    *,
    target_mult: float,
    stop_mult: float,
) -> tuple[float, float]:
    return min(0.05, target_pct * target_mult), min(0.03, stop_pct * stop_mult)


def compute_dynamic_bands(
    entry_price: float,
    reference_price: float,
    *,
    atr_pct: float | None = None,
    beta: float | None = None,
    drift_pct: float = 0.0,
    rvol: float | None = None,
    spread_pct: float = 0.0,
    priority_modifier: float | None = None,
    signal_name: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    entry_time: datetime | None = None,
    # Fallback defaults from settings (used when no ATR data available)
    default_target_pct: float = 0.01,
    default_stop_pct: float = 0.01,
) -> DynamicBands:
    """Compute adaptive target/stop bands based on market conditions.

    What a real trader does:
    - Sets stop based on the stock's volatility (ATR), not a fixed %)
    - Reduces profit target when the stock already ran (drift consumed upside)
    - Gives wider targets when volume conviction is high (RVOL > 3 = momentum)
    - Accounts for spread cost (wide spread eats into effective profit)

    Returns a DynamicBands with target_pct and stop_pct as decimals (0.02 = 2%).
    """
    parts: list[str] = []
    atr_value = _coerce_float_or_none(atr_pct, field_name="atr_pct")
    beta_value = _coerce_float_or_none(beta, field_name="beta")
    rvol_value = _coerce_float_or_none(rvol, field_name="rvol")
    priority_value = _coerce_float_or_none(priority_modifier, field_name="priority_modifier")

    # ---- 1. BASE STOP: ATR-based (if available) ----
    # A trader sets stop loss at 1.5× ATR — gives the stock room to breathe
    # without getting stopped out on normal noise.
    if atr_value is not None and atr_value > 0:
        # ATR stop: 1.5× ATR, but floor at 0.5% and cap at 3%
        atr_stop = atr_value / 100.0 * 1.5
        atr_stop = max(0.005, min(atr_stop, 0.03))
        stop_pct = atr_stop
        parts.append(f"stop=1.5×ATR({atr_value:.1f}%)={stop_pct*100:.2f}%")
    else:
        # No ATR — use a moderate default wider than 1%
        stop_pct = max(default_stop_pct, 0.012)
        parts.append(f"stop=default({stop_pct*100:.1f}%,no ATR)")

    # ---- 2. BASE TARGET: risk/reward based on stop ----
    # Minimum 1.5:1 reward/risk ratio. A trader won't enter if the
    # target doesn't give at least 1.5× the stop distance.
    base_target = stop_pct * 2.0  # 2:1 R/R as starting point
    base_target = max(base_target, 0.008)  # floor 0.8%
    base_target = min(base_target, 0.05)   # cap 5% (guardrail will also cap)
    target_pct = base_target
    parts.append(f"base_target=2×stop={target_pct*100:.2f}%")

    # ---- 3. CATALYST-TYPE PROFILE ----
    # Different catalysts have different empirical behavior. Earnings and
    # M&A often travel farther; analyst target raises are more prone to fade.
    catalyst_profile, catalyst_target_mult, catalyst_stop_mult = _catalyst_band_profile(
        category=category,
        subcategory=subcategory,
        signal_name=signal_name,
    )
    if catalyst_target_mult != 1.0 or catalyst_stop_mult != 1.0:
        old_target, old_stop = target_pct, stop_pct
        target_pct, stop_pct = _apply_band_multiplier(
            target_pct,
            stop_pct,
            target_mult=catalyst_target_mult,
            stop_mult=catalyst_stop_mult,
        )
        parts.append(
            f"catalyst_profile={catalyst_profile}: target {old_target*100:.2f}%→{target_pct*100:.2f}% "
            f"stop {old_stop*100:.2f}%→{stop_pct*100:.2f}%"
        )

    # ---- 4. BETA PROFILE ----
    # High-beta names need more market-noise room; low-beta names should not
    # need as much room unless something material is really happening.
    beta_profile, beta_target_mult, beta_stop_mult = _beta_band_profile(beta_value)
    if beta_target_mult != 1.0 or beta_stop_mult != 1.0:
        old_target, old_stop = target_pct, stop_pct
        target_pct, stop_pct = _apply_band_multiplier(
            target_pct,
            stop_pct,
            target_mult=beta_target_mult,
            stop_mult=beta_stop_mult,
        )
        parts.append(
            f"beta_profile={beta_profile}({beta_value:.2f}): target {old_target*100:.2f}%→{target_pct*100:.2f}% "
            f"stop {old_stop*100:.2f}%→{stop_pct*100:.2f}%"
        )

    # ---- 5. TIME-OF-DAY PROFILE ----
    # Opening/closing periods carry wider noise; midday is usually tighter.
    time_profile, time_target_mult, time_stop_mult = market_time_profile(entry_time)
    if time_target_mult != 1.0 or time_stop_mult != 1.0:
        old_target, old_stop = target_pct, stop_pct
        target_pct, stop_pct = _apply_band_multiplier(
            target_pct,
            stop_pct,
            target_mult=time_target_mult,
            stop_mult=time_stop_mult,
        )
        parts.append(
            f"time_profile={time_profile}: target {old_target*100:.2f}%→{target_pct*100:.2f}% "
            f"stop {old_stop*100:.2f}%→{stop_pct*100:.2f}%"
        )

    # ---- 6. DRIFT ADJUSTMENT ----
    # If the stock already moved 1.5% since catalyst, the upside is partially
    # consumed. Reduce target by the drift amount, but don't go below 0.5%.
    # The user said: "if the market has moved > 1%, adjust the band — enter
    # for larger price and exit for a lower price."
    abs_drift = abs(drift_pct) / 100.0  # drift_pct comes as percentage (e.g. 1.5)
    if abs_drift > 0.005:  # meaningful drift > 0.5%
        drift_tax = abs_drift * 0.6  # take 60% of drift off the target
        old_target = target_pct
        target_pct = max(0.005, target_pct - drift_tax)
        if target_pct != old_target:
            parts.append(f"drift_adj: {old_target*100:.2f}%→{target_pct*100:.2f}% (drift={drift_pct:.1f}%)")
        # Also tighten stop slightly when we're entering late (less room for error)
        if abs_drift > 0.01:
            stop_pct = max(0.005, stop_pct * 0.9)
            parts.append(f"stop_tightened_for_drift={stop_pct*100:.2f}%")

    # ---- 7. VOLUME CONVICTION (RVOL) ----
    # High RVOL = strong conviction, momentum has legs.
    # RVOL > 3 = widen target by 20%, RVOL > 5 = widen by 40%
    # Low RVOL or no data = no adjustment.
    if rvol_value is not None and rvol_value > 2.0:
        if rvol_value >= 5.0:
            vol_boost = 1.4
        elif rvol_value >= 3.0:
            vol_boost = 1.2
        else:
            vol_boost = 1.0 + (rvol_value - 2.0) * 0.1  # 2-3x: gradual 10-20%
        old_target = target_pct
        target_pct = min(0.05, target_pct * vol_boost)
        parts.append(f"rvol_boost: RVOL={rvol_value:.1f}x → target {old_target*100:.2f}%→{target_pct*100:.2f}%")

    # ---- 8. SPREAD COST ----
    # Wide spread eats into effective profit. If spread is 0.3% of price,
    # that's 0.3% gone from the target before the trade even starts.
    if spread_pct > 0.001:
        old_target = target_pct
        target_pct = max(0.004, target_pct - spread_pct * 0.5)
        if target_pct != old_target:
            parts.append(f"spread_cost: {spread_pct*100:.2f}% → target {old_target*100:.2f}%→{target_pct*100:.2f}%")

    # ---- 9. PRIORITY MODIFIER (from Qwen sentiment strength) ----
    # Strong sentiment (priority_modifier > 0.3) = slightly wider target.
    if priority_value is not None and priority_value > 0.3:
        boost = 1.0 + min(priority_value, 1.0) * 0.15
        target_pct = min(0.05, target_pct * boost)
        parts.append(f"sentiment_boost: mod={priority_value:.2f} → target={target_pct*100:.2f}%")

    # ---- FINAL SANITY: ensure target > stop (reward > risk) ----
    if target_pct <= stop_pct:
        target_pct = stop_pct * 1.5
        parts.append(f"floor_rr: target bumped to 1.5×stop={target_pct*100:.2f}%")

    # Round to 4 decimal places
    target_pct = round(target_pct, 4)
    stop_pct = round(stop_pct, 4)

    reasoning = " | ".join(parts)
    return DynamicBands(target_pct=target_pct, stop_pct=stop_pct, reasoning=reasoning)


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
            repository, settings, clock=self.clock, catalyst_db_path=catalyst_db_path,
            consecutive_loss_limit=settings.consecutive_loss_limit,
        )

    async def allocate(self, candidates: list[AllocationCandidate]) -> AllocationResult:
        result = await self.allocator.allocate(candidates)
        candidate_by_symbol = {c.symbol.upper(): c for c in candidates}

        for allocation in result.allocations:
            candidate = candidate_by_symbol[allocation.symbol.upper()]
            reference_price = float(candidate.metadata.get("reference_price", 100.0))
            slot_value_multiplier = self._slot_value_multiplier(candidate.metadata)
            effective_slot_value = allocation.slot_value * slot_value_multiplier
            quantity = max(1, int(effective_slot_value // reference_price))

            # Catalyst-event audit fields — passed through from the candidate
            # so we can post-hoc join trade rows back to the triggering event.
            cat_event_ts = candidate.metadata.get("catalyst_event_ts")
            if isinstance(cat_event_ts, datetime):
                cat_event_ts_value = cat_event_ts.isoformat()
            elif isinstance(cat_event_ts, str):
                cat_event_ts_value = cat_event_ts
            else:
                cat_event_ts_value = None
            cat_headline = (candidate.metadata.get("headline") or "")[:200]
            cat_headline_hash = candidate.metadata.get("headline_hash")
            cat_sentiment = candidate.metadata.get("sentiment")
            cat_event_age = candidate.metadata.get("event_age_minutes")

            logger.info(
                "LIVE: submitting paper buy %s qty=%d slot=%s ref_price=%.2f slot_mult=%.2f "
                "catalyst_sentiment=%s catalyst_event_age=%.1fmin catalyst_hash=%s | %s",
                allocation.symbol, quantity, allocation.slot_id, reference_price,
                slot_value_multiplier,
                cat_sentiment or "NONE",
                float(cat_event_age) if cat_event_age is not None else -1.0,
                cat_headline_hash or "NONE",
                cat_headline,
            )

            # Pre-compute estimated dynamic stop for the broker's protective stop
            # (uses reference_price as proxy since we don't have fill price yet)
            _pre_meta = candidate.metadata or {}
            _pre_atr = _pre_meta.get("atr_pct")
            _pre_bands = compute_dynamic_bands(
                entry_price=reference_price,
                reference_price=reference_price,
                atr_pct=_pre_atr,
                beta=_pre_meta.get("beta"),
                drift_pct=_coerce_float_or_none(
                    _pre_meta.get("price_drift_pct"), field_name="price_drift_pct"
                ) or 0.0,
                rvol=_coerce_float_or_none(_pre_meta.get("rvol"), field_name="rvol"),
                priority_modifier=_coerce_float_or_none(
                    _pre_meta.get("priority_modifier"), field_name="priority_modifier"
                ),
                signal_name=_pre_meta.get("signal_name"),
                category=_pre_meta.get("category"),
                subcategory=_pre_meta.get("subcategory"),
                entry_time=allocation.reserved_at,
                default_target_pct=self.settings.target_pct,
                default_stop_pct=self.settings.stop_pct,
            )

            submission = None
            try:
                submission = await self.broker.submit_entry_order(
                    symbol=allocation.symbol,
                    quantity=quantity,
                    slot_id=allocation.slot_id,
                    protective_stop_pct=_pre_bands.stop_pct,
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
                except Exception as exc:
                    logger.warning(
                        "LIVE: open-position reconciliation failed after entry rejection for %s: %s",
                        allocation.symbol,
                        exc,
                    )
                    open_positions = []
                actual = next(
                    (p for p in open_positions if p.symbol.upper() == allocation.symbol.upper()),
                    None,
                )
                if actual is not None:
                    reason = "reconciled_from_alpaca_position"
                    prior_reason = (submission.reason if submission else "broker_already_filled_race")
                    prior_id = (submission.broker_order_id if submission else None)
                    reconciled_entry = float(actual.average_entry_price)
                    # Slippage guard on reconciled fills: if the Alpaca position
                    # price is >1.5% above our reference, something went wrong
                    # (stale quote, previous fill from different attempt, etc.)
                    recon_slippage = (
                        ((reconciled_entry - reference_price) / reference_price * 100.0)
                        if reference_price > 0 else 0.0
                    )
                    logger.warning(
                        "LIVE: reconciling %s from alpaca: qty=%s entry=$%.2f ref=$%.2f "
                        "slippage=%.1f%% (broker prior reason=%s)",
                        actual.symbol, actual.quantity, reconciled_entry,
                        reference_price, recon_slippage, prior_reason,
                    )
                    submission = OrderSubmissionResult(
                        submitted=True,
                        broker_order_id=prior_id or actual.broker_position_id,
                        symbol=actual.symbol,
                        side="buy",
                        quantity=float(actual.quantity),
                        order_type="reconciled",
                        limit_price=reconciled_entry,
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

            # --- Slippage guard ---
            # If the actual fill price is >1.5% above the reference price,
            # we entered much higher than the signal intended. The profit
            # target is already eaten by slippage. Log and skip recording
            # (the broker position will be caught by the next reconcile
            # cycle and closed as an orphan, or we exit immediately).
            if reference_price > 0:
                slippage_pct = ((entry_price - reference_price) / reference_price) * 100.0
                if slippage_pct > 1.5:
                    logger.warning(
                        "SLIPPAGE REJECT %s: fill=$%.2f ref=$%.2f slippage=%.1f%% "
                        "(exceeds 1.5%% — entering at a loss). Will exit immediately.",
                        allocation.symbol, entry_price, reference_price, slippage_pct,
                    )
                    # Still record the position so the monitor can exit it cleanly
                    # but mark it as slippage-rejected in metadata for forensics.

            # --- DYNAMIC BANDS: compute adaptive target/stop ---
            # Pull market data from candidate metadata for band calculation
            _meta = candidate.metadata or {}
            _atr_pct = _meta.get("atr_pct")  # ATR as % of price
            _atr_value = _coerce_float_or_none(_atr_pct, field_name="atr_pct")
            _beta = _coerce_float_or_none(_meta.get("beta"), field_name="beta")
            _drift_pct = (
                _coerce_float_or_none(_meta.get("price_drift_pct"), field_name="price_drift_pct")
                or 0.0
            )
            _rvol = _coerce_float_or_none(_meta.get("rvol"), field_name="rvol")
            _signal_name = _meta.get("signal_name")
            _priority_mod = (
                _coerce_float_or_none(_meta.get("priority_modifier"), field_name="priority_modifier")
                or 0.0
            )
            _band_catalyst_profile = _catalyst_band_profile(
                category=_meta.get("category"),
                subcategory=_meta.get("subcategory"),
                signal_name=_signal_name,
            )[0]
            _band_beta_profile = _beta_band_profile(_beta)[0]
            _band_time_profile = market_time_profile(allocation.reserved_at, clock=self.clock)[0]

            # Compute spread from entry vs reference (proxy for bid/ask width)
            _spread_pct = 0.0
            if reference_price > 0 and entry_price > 0:
                _spread_pct = abs(entry_price - reference_price) / reference_price

            bands = compute_dynamic_bands(
                entry_price=entry_price,
                reference_price=reference_price,
                atr_pct=_atr_pct,
                beta=_beta,
                drift_pct=_drift_pct,
                rvol=_rvol,
                spread_pct=_spread_pct,
                priority_modifier=_priority_mod if _priority_mod else None,
                signal_name=_signal_name,
                category=_meta.get("category"),
                subcategory=_meta.get("subcategory"),
                entry_time=allocation.reserved_at,
                default_target_pct=self.settings.target_pct,
                default_stop_pct=self.settings.stop_pct,
            )
            logger.info(
                "DYNAMIC BANDS %s: target=%.2f%% stop=%.2f%% | atr=%.1f%% beta=%s "
                "drift=%.1f%% rvol=%s spread=%.3f%% | %s",
                allocation.symbol,
                bands.target_pct * 100, bands.stop_pct * 100,
                _atr_value or 0.0,
                f"{_beta:.2f}" if _beta is not None else "N/A",
                _drift_pct,
                f"{_rvol:.1f}x" if _rvol else "N/A",
                _spread_pct * 100,
                bands.reasoning,
            )

            stop_price = round(entry_price * (1 - bands.stop_pct), 2)
            target_price = round(entry_price * (1 + bands.target_pct), 2)
            protective_stop_metadata = dict(getattr(submission, "metadata", {}) or {})
            position = self.repository.positions.create_open(
                symbol=allocation.symbol,
                quantity=quantity,
                entry_price=entry_price,
                target_price=target_price,
                stop_price=stop_price,
                slot_id=allocation.slot_id,
                opened_at=allocation.reserved_at,
                metadata={
                    "sector": allocation.sector,
                    "broker_order_id": submission.broker_order_id,
                    "reference_price": reference_price,
                    "slot_value_multiplier": slot_value_multiplier,
                    "effective_slot_value": effective_slot_value,
                    "current_price": entry_price,
                    "entry_ts": allocation.reserved_at.isoformat(),
                    "entry_price": entry_price,
                    "source": "live_alpaca_paper",
                    # Dynamic band audit trail
                    "dynamic_target_pct": bands.target_pct,
                    "dynamic_stop_pct": bands.stop_pct,
                    "dynamic_band_reasoning": bands.reasoning,
                    "band_atr_pct": _atr_pct,
                    "band_beta": _beta,
                    "band_beta_profile": _band_beta_profile,
                    "band_drift_pct": _drift_pct,
                    "band_rvol": _rvol,
                    "band_spread_pct": round(_spread_pct, 4),
                    "band_category": _meta.get("category"),
                    "band_subcategory": _meta.get("subcategory"),
                    "band_catalyst_profile": _band_catalyst_profile,
                    "band_time_profile": _band_time_profile,
                    # Catalyst event chain — for forensic analysis
                    "catalyst_event_ts": cat_event_ts_value,
                    "catalyst_headline_hash": cat_headline_hash,
                    "catalyst_sentiment": cat_sentiment,
                    "catalyst_headline": cat_headline,
                    "catalyst_event_age_min_at_entry": cat_event_age,
                    "signal_score_at_entry": float(getattr(candidate, "score", 0.0)),
                    # Stamp the originating signal so the monitor's MultiSignal
                    # router knows which exit logic to apply.
                    "signal_name": candidate.metadata.get("signal_name"),
                    **protective_stop_metadata,
                },
            )
            if (
                submission.reason == "reconciled_from_alpaca_position"
                and not protective_stop_metadata.get("protective_stop_order_id")
            ):
                try:
                    protective = await self.broker.submit_protective_stop_order(
                        symbol=allocation.symbol,
                        quantity=quantity,
                        stop_price=stop_price,
                        position_id=getattr(position, "id", None),
                        slot_id=allocation.slot_id,
                        parent_order_id=submission.broker_order_id,
                    )
                    protective_stop_metadata = {
                        "protective_stop_order_id": protective.broker_order_id,
                        "protective_stop_price": stop_price,
                        "protective_stop_pct": bands.stop_pct,
                    }
                    position = self.repository.positions.update_metadata(
                        getattr(position, "id"),
                        protective_stop_metadata,
                    )
                except Exception as exc:
                    logger.exception(
                        "LIVE: protective stop placement failed after reconcile for %s: %s",
                        allocation.symbol,
                        exc,
                    )
            logger.info(
                "LIVE: position opened symbol=%s qty=%d entry=%.2f position_id=%s broker_order_id=%s",
                allocation.symbol, quantity, entry_price,
                getattr(position, "id", None), submission.broker_order_id,
            )
            # Transition the slot RESERVED → OPEN and link to the position id
            # so the dashboard / state machine know the slot is held by a real
            # position. Without this, slots stay RESERVED forever and the UI
            # shows "submitting…" indefinitely.
            try:
                slot_record = next(
                    (s for s in self.repository.slots.list_all() if s.slot_id == allocation.slot_id),
                    None,
                )
                self.repository.slots.upsert(
                    allocation.slot_id,
                    status="OPEN",
                    symbol=allocation.symbol,
                    position_id=getattr(position, "id", None),
                    slot_value=allocation.slot_value,
                    metadata={
                        **((slot_record.metadata if slot_record else None) or {}),
                        "opened_at": allocation.reserved_at.isoformat(),
                        "entry_price": entry_price,
                        "slot_value_multiplier": slot_value_multiplier,
                        "effective_slot_value": effective_slot_value,
                        "broker_order_id": submission.broker_order_id,
                        **protective_stop_metadata,
                    },
                    updated_at=self.clock.now_utc(),
                )
            except Exception as exc:
                logger.warning(
                    "LIVE: slot transition RESERVED→OPEN failed for slot=%s: %s",
                    allocation.slot_id, exc,
                )

        return result

    def _slot_value_multiplier(self, metadata: dict[str, Any]) -> float:
        raw_value = metadata.get("slot_value_multiplier", 1.0)
        try:
            multiplier = float(raw_value)
        except (TypeError, ValueError):
            logger.warning("LIVE: ignoring invalid slot_value_multiplier=%r", raw_value)
            return 1.0
        if not math.isfinite(multiplier) or multiplier <= 0:
            logger.warning("LIVE: ignoring invalid slot_value_multiplier=%r", raw_value)
            return 1.0
        return min(multiplier, 1.0)


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
        # Lazy-loaded sector map from universe.csv for reconciled positions
        self.__sector_map: dict[str, str] | None = None

    @property
    def _sector_map(self) -> dict[str, str]:
        if self.__sector_map is None:
            self.__sector_map = {}
            try:
                universe_file = getattr(self.settings, "universe_file", "config/universe.csv")
                with open(universe_file) as f:
                    header = next(f, None)
                    if header:
                        cols = [c.strip().lower() for c in header.split(",")]
                        sec_idx = cols.index("sector") if "sector" in cols else -1
                        if sec_idx >= 0:
                            for line in f:
                                parts = line.split(",")
                                if len(parts) > sec_idx:
                                    sym = parts[0].strip().upper()
                                    sec = parts[sec_idx].strip()
                                    if sym and sec:
                                        self.__sector_map[sym] = sec
            except Exception as exc:
                logger.warning("sector map load failed: %s", exc)
                pass
        return self.__sector_map

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
        broker_symbols = {(p.symbol or "").upper() for p in alpaca_positions}

        changes = 0
        for local_position in local_open:
            local_symbol = (local_position.symbol or "").upper()
            if local_symbol in broker_symbols:
                continue
            local_metadata = getattr(local_position, "metadata", {}) or {}
            stop_order_id = local_metadata.get("protective_stop_order_id")
            actual_fill = None
            if stop_order_id:
                try:
                    actual_fill = await self.broker.get_fill_price(str(stop_order_id))
                except Exception as exc:
                    logger.warning(
                        "monitor reconcile: protective stop fill lookup failed for %s order=%s: %s",
                        local_symbol,
                        stop_order_id,
                        exc,
                    )
            exit_price = (
                actual_fill
                or local_metadata.get("protective_stop_price")
                or getattr(local_position, "stop_price", None)
                or local_metadata.get("current_price")
                or getattr(local_position, "entry_price", 0.0)
            )
            entry_price = float(getattr(local_position, "entry_price", 0.0) or 0.0)
            quantity = float(getattr(local_position, "quantity", 0.0) or 0.0)
            realized = (float(exit_price) - entry_price) * quantity
            close_reason = (
                "broker_protective_stop_filled"
                if stop_order_id
                else "broker_position_missing_at_reconcile"
            )
            try:
                self.repository.positions.close(
                    position_id=getattr(local_position, "id"),
                    exit_reason=close_reason,
                    realized_pnl=realized,
                    closed_at=self.clock.now_utc(),
                    metadata={
                        "exit_price": float(exit_price),
                        "broker_exit_order_id": stop_order_id,
                        "exit_close_path": "reconciled_from_broker_absence",
                        "protective_stop_fill_reconciled": bool(stop_order_id),
                    },
                )
                slot_id = getattr(local_position, "slot_id", None)
                if slot_id is not None:
                    self.repository.slots.upsert(
                        slot_id,
                        status="EMPTY",
                        symbol=None,
                        slot_value=self.settings.slot_value,
                        metadata={
                            "last_symbol": local_symbol,
                            "last_exit_reason": close_reason,
                            "emptied_at": self.clock.now_utc().isoformat(),
                            "empty_reason": f"Closed: {close_reason}",
                        },
                        updated_at=self.clock.now_utc(),
                    )
                changes += 1
                logger.warning(
                    "monitor reconcile: closed local %s because broker no longer reports the position (reason=%s)",
                    local_symbol,
                    close_reason,
                )
            except Exception as exc:
                logger.warning(
                    "monitor reconcile: failed to close broker-missing local %s: %s",
                    local_symbol,
                    exc,
                )

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
                # Older slot rows may not have JSON metadata; fall back to
                # empty metadata and still reconcile the broker position.
                pass
            cand = slot_md.get("candidate", {})
            entry_price = float(ap.average_entry_price)
            opened_at = slot_md.get("reserved_at") or datetime.now(timezone.utc).isoformat()

            # Look up sector from candidate metadata, falling back to universe.csv
            sector = cand.get("sector") or slot_md.get("sector")
            if not sector or sector == "Unknown":
                sector = self._sector_map.get(sym, "Unknown")
            position_md = {
                "sector": sector,
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
                    target_price=round(entry_price * (1 + self.settings.target_pct), 2),
                    stop_price=round(entry_price * (1 - self.settings.stop_pct), 2),
                    slot_id=slot.slot_id,
                    opened_at=datetime.fromisoformat(opened_at),
                    metadata=position_md,
                )
                self.repository.slots.upsert(
                    slot.slot_id, status="OPEN", symbol=sym,
                    slot_value=getattr(slot, "slot_value", self.settings.slot_value),
                    updated_at=datetime.now(timezone.utc),
                )
                changes += 1
                logger.info(
                    "monitor reconcile: created local position %s qty=%s entry=$%.2f from alpaca state",
                    sym, ap.quantity, entry_price,
                )
            except Exception as exc:
                logger.warning("monitor reconcile: failed to create local %s position: %s", sym, exc)
        return changes

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
                # Malformed legacy metadata should not block exit evaluation.
                position_metadata = {}
            position_id = getattr(position, "id", None)
            prev_peak = (
                self._peak_by_position_id.get(position_id, unrealized_pct)
                if isinstance(position_id, int)
                else unrealized_pct
            )
            new_peak = max(prev_peak, unrealized_pct)
            if isinstance(position_id, int):
                self._peak_by_position_id[position_id] = new_peak
            position_metadata["current_price"] = mid
            position_metadata["peak_unrealized_pct"] = new_peak
            try:
                object.__setattr__(position, "metadata", position_metadata)
                if hasattr(position, "current_price"):
                    object.__setattr__(position, "current_price", mid)
            except Exception:
                # PositionRecord is frozen; this is a best-effort in-memory hint
                # for signal exit evaluation only.
                pass

            try:
                decision = signal.evaluate_exit(position, now)
                if inspect.isawaitable(decision):
                    decision = await decision
            except Exception as exc:
                logger.exception("monitor: evaluate_exit raised for %s: %s", symbol, exc)
                decision = None

            should_close = getattr(decision, "should_exit", None)
            if should_close is None:
                should_close = getattr(decision, "close", False)

            # FAILSAFE TIME-STOP: if the signal didn't produce a decision
            # (e.g. reconciled position with no metadata), enforce a hard
            # time-stop from opened_at. Without this, zombie positions stay
            # open forever — MAS was held 480 min with max_hold=60 min.
            if decision is None or not should_close:
                opened_at = getattr(position, "opened_at", None)
                if opened_at is not None:
                    try:
                        if isinstance(opened_at, str):
                            opened_at = datetime.fromisoformat(
                                opened_at.replace("Z", "+00:00")
                            )
                        hold_minutes = (now - opened_at.astimezone(now.tzinfo)).total_seconds() / 60.0
                        max_hold = self.settings.max_hold_minutes
                        if hold_minutes > max_hold:
                            logger.warning(
                                "FAILSAFE TIME-STOP: %s held %.0f min (max=%d) — "
                                "signal returned None (likely reconciled position "
                                "with no metadata). Forcing close.",
                                symbol, hold_minutes, max_hold,
                            )
                            should_close = True
                            # Create a minimal decision-like object
                            class _FailsafeExit:
                                should_exit = True
                                exit_reason = "FAILSAFE_TIME_STOP"
                                reason = "FAILSAFE_TIME_STOP"
                                metadata = {"hold_minutes": hold_minutes, "max_hold": max_hold}
                            decision = _FailsafeExit()
                    except Exception as ts_exc:
                        logger.debug("failsafe time-stop check failed: %s", ts_exc)

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
            protective_stop_close_metadata = await self._cancel_protective_stop(
                position,
                reason=exit_reason,
            )
            if (
                protective_stop_close_metadata.get("protective_stop_cancel_status")
                == "cancel_failed"
            ):
                try:
                    live_positions = await asyncio.wait_for(
                        self.broker.get_open_positions(), timeout=5.0,
                    )
                    position_gone_after_cancel_failure = not any(
                        p.symbol.upper() == symbol for p in live_positions
                    )
                except Exception as exc:
                    logger.warning(
                        "LIVE: post-stop-cancel position check failed for %s: %s",
                        symbol,
                        exc,
                    )
                    position_gone_after_cancel_failure = False
                if position_gone_after_cancel_failure:
                    exit_price = (
                        protective_stop_close_metadata.get("protective_stop_price")
                        or getattr(position, "stop_price", None)
                        or mid
                    )
                    realized = (float(exit_price) - entry_price) * quantity
                    self.repository.positions.close(
                        position_id=getattr(position, "id"),
                        exit_reason="broker_protective_stop_filled",
                        realized_pnl=realized,
                        closed_at=self.clock.now_utc(),
                        metadata={
                            "exit_price": float(exit_price),
                            "exit_close_path": "reconciled_after_stop_cancel_failure",
                            "peak_unrealized_pct": new_peak,
                            **protective_stop_close_metadata,
                        },
                    )
                    slot_id = getattr(position, "slot_id", None)
                    if slot_id is not None:
                        self.repository.slots.upsert(
                            slot_id,
                            status="EMPTY",
                            symbol=None,
                            slot_value=self.settings.slot_value,
                            metadata={
                                "last_symbol": symbol,
                                "last_exit_reason": "broker_protective_stop_filled",
                                "emptied_at": self.clock.now_utc().isoformat(),
                                "empty_reason": "Closed: broker_protective_stop_filled",
                            },
                            updated_at=self.clock.now_utc(),
                        )
                    return 1
                logger.warning(
                    "LIVE: skipping software exit for %s because protective stop cancel failed",
                    symbol,
                )
                return 0

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
                elif "insufficient qty" in msg or "held_for_orders" in msg:
                    # Shares are locked by an open order (protective stop or
                    # other). Parse the related order IDs from the Alpaca error,
                    # cancel them, then retry the exit once.
                    logger.warning(
                        "LIVE: %s shares held by open orders — canceling blocking orders and retrying exit",
                        symbol,
                    )
                    import re as _re
                    related_ids = _re.findall(
                        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
                        str(exc),
                    )
                    for oid in related_ids:
                        try:
                            await self.broker.cancel_order(oid)
                            logger.info("LIVE: canceled blocking order %s for %s", oid, symbol)
                        except Exception as cancel_exc:
                            logger.warning(
                                "LIVE: failed to cancel blocking order %s for %s: %s",
                                oid, symbol, cancel_exc,
                            )
                    # Brief pause for Alpaca to release held qty
                    await asyncio.sleep(0.5)
                    try:
                        exit_result = await self.broker.submit_exit_order(
                            symbol=symbol,
                            quantity=quantity,
                            position_id=getattr(position, "id", None),
                        )
                        logger.info("LIVE: exit retry succeeded for %s after canceling blocking orders", symbol)
                    except Exception as retry_exc:
                        retry_msg = str(retry_exc).lower()
                        if "already" in retry_msg and "filled" in retry_msg:
                            # The blocking order (protective stop) already sold the
                            # shares. Position is gone on Alpaca — close locally.
                            logger.info(
                                "LIVE: %s already sold by protective stop — closing locally",
                                symbol,
                            )
                            stop_px = (
                                (getattr(position, "metadata", {}) or {}).get("protective_stop_price")
                                or getattr(position, "stop_price", None)
                                or mid
                            )
                            realized = (float(stop_px) - entry_price) * quantity
                            self.repository.positions.close(
                                position_id=getattr(position, "id"),
                                exit_reason="broker_protective_stop_filled",
                                realized_pnl=realized,
                                closed_at=self.clock.now_utc(),
                                metadata={
                                    "exit_price": float(stop_px),
                                    "exit_close_path": "detected_on_exit_retry_already_filled",
                                    "peak_unrealized_pct": new_peak,
                                    "blocking_orders_canceled": related_ids,
                                },
                            )
                            slot_id = getattr(position, "slot_id", None)
                            if slot_id is not None:
                                self.repository.slots.upsert(slot_id, status="FREE", symbol=None)
                            return 1
                        logger.exception(
                            "LIVE: exit retry ALSO failed for %s: %s", symbol, retry_exc,
                        )
                        return 0
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
            except Exception as exc:
                logger.warning(
                    "LIVE: post-exit position verification failed for %s: %s",
                    symbol,
                    exc,
                )
                position_gone = False

            if exit_result and exit_result.submitted:
                broker_oid = exit_result.broker_order_id
                close_reason_label = "submitted"
                # Try to get the ACTUAL fill price from Alpaca instead of
                # using the order's limit_price. Paper orders fill instantly
                # at a price that can differ from the limit.
                actual_fill = None
                if broker_oid:
                    try:
                        actual_fill = await self.broker.get_fill_price(broker_oid)
                    except Exception as exc:
                        logger.warning(
                            "LIVE: exit fill-price lookup failed for %s order=%s: %s",
                            symbol,
                            broker_oid,
                            exc,
                        )
                exit_price = actual_fill or exit_result.limit_price or mid
                if actual_fill:
                    logger.info(
                        "LIVE: exit fill price for %s: actual=$%.2f (limit=$%.2f mid=$%.2f)",
                        symbol, actual_fill, exit_result.limit_price or 0, mid,
                    )
            elif broker_race_filled or position_gone:
                broker_oid = getattr(exit_result, "broker_order_id", None) if exit_result else None
                close_reason_label = "reconciled_after_race"
                # Race condition: position already gone at Alpaca. Try to
                # retrieve the actual fill price from the order. Falls back
                # to mid if unavailable.
                actual_fill = None
                if broker_oid:
                    try:
                        actual_fill = await self.broker.get_fill_price(broker_oid)
                    except Exception as exc:
                        logger.warning(
                            "LIVE: race exit fill-price lookup failed for %s order=%s: %s",
                            symbol,
                            broker_oid,
                            exc,
                        )
                exit_price = actual_fill or mid
                if actual_fill:
                    logger.info(
                        "LIVE: race-reconciled exit fill for %s: actual=$%.2f (mid=$%.2f)",
                        symbol, actual_fill, mid,
                    )
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
                        **protective_stop_close_metadata,
                    },
                )
                slot_id = getattr(position, "slot_id", None)
                if slot_id is not None:
                    try:
                        self.repository.slots.upsert(
                            slot_id, status="EMPTY", symbol=None,
                            slot_value=self.settings.slot_value,
                            metadata={
                                "last_symbol": symbol,
                                "last_exit_reason": exit_reason,
                                "emptied_at": self.clock.now_utc().isoformat(),
                                "empty_reason": f"Closed: {exit_reason}",
                            },
                            updated_at=self.clock.now_utc(),
                        )
                    except Exception as slot_exc:
                        logger.warning("LIVE: slot %s free failed: %s", slot_id, slot_exc)
                # Clean up in-memory peak tracker
                close_position_id = getattr(position, "id", None)
                if isinstance(close_position_id, int):
                    self._peak_by_position_id.pop(close_position_id, None)
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

    async def _cancel_protective_stop(self, position: Any, *, reason: str) -> dict[str, Any]:
        metadata = getattr(position, "metadata", {}) or {}
        broker_order_id = metadata.get("protective_stop_order_id")
        if not broker_order_id:
            return {}
        stop_price = metadata.get("protective_stop_price") or getattr(
            position,
            "stop_price",
            None,
        )
        try:
            await self.broker.cancel_order(str(broker_order_id))
            logger.info(
                "LIVE: canceled protective stop for %s order=%s reason=%s",
                getattr(position, "symbol", "UNKNOWN"),
                broker_order_id,
                reason,
            )
            return {
                "protective_stop_cancel_status": "canceled",
                "protective_stop_canceled_order_id": str(broker_order_id),
                "protective_stop_price": stop_price,
            }
        except Exception as exc:
            logger.warning(
                "LIVE: protective stop cancel failed for %s order=%s reason=%s: %s",
                getattr(position, "symbol", "UNKNOWN"),
                broker_order_id,
                reason,
                exc,
            )
            return {
                "protective_stop_cancel_status": "cancel_failed",
                "protective_stop_canceled_order_id": str(broker_order_id),
                "protective_stop_cancel_error": str(exc),
                "protective_stop_price": stop_price,
            }

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
