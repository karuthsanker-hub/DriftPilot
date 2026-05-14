"""analyst_target_raise_v1 — catalyst-driven signal.

Pure event-bus consumer: subscribes to (category="analyst",
subcategory="target_raise") on a CatalystEventBus at construction time
and maintains an in-memory map of fresh events keyed by symbol.
`scan()` filters by `max_event_age_minutes` and emits one Candidate
per remaining symbol.

This signal NEVER polls Alpaca or any other market-data source — it
only reads from the injected bus.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.signals.base import Candidate, ExitDecision

from driftpilot.signals.analyst_target_raise_v1.config import (
    EVENT_CATEGORY,
    EVENT_SUBCATEGORY,
    SIGNAL_NAME,
    SIGNAL_VERSION,
    AnalystTargetRaiseConfig,
)
from driftpilot.signals.analyst_target_raise_v1.exits import evaluate_all
from driftpilot.signals.analyst_target_raise_v1.features import is_event_fresh


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AnalystTargetRaiseV1Signal:
    """Catalyst signal for analyst price-target raises.

    Validation: 1.42x forward-return ratio at 60m, N=104, in
    `reports/catalyst_horizons_midcap_2024.json`. The cell fades to
    0.97x by 1day — the 60m hold cap is load-bearing.
    """

    name: str = SIGNAL_NAME
    version: str = SIGNAL_VERSION

    def __init__(
        self,
        config: AnalystTargetRaiseConfig | None = None,
        bus: CatalystEventBus | None = None,
        *,
        clock: Any = None,
    ) -> None:
        self.config: AnalystTargetRaiseConfig = config or AnalystTargetRaiseConfig()
        if bus is None:
            raise ValueError("AnalystTargetRaiseV1Signal requires a CatalystEventBus")
        self._bus: CatalystEventBus = bus
        self._clock = clock or _utcnow
        self._active_events: dict[str, CatalystEvent] = {}
        self._event_context_json: dict[str, str] = {}
        self._sub_id: str | None = None
        self._db_path: str | None = None
        self._last_sentiment_refresh: float = 0.0
        self._sentiment_refresh_interval: int = 120  # seconds

        # Subscribe synchronously at construction. The bus subscribe
        # method is async; resolve it on the running loop or via
        # asyncio.run if no loop is active (test harnesses do this).
        #
        # We subscribe to all bullish catalyst categories — analyst
        # upgrades, target raises, product launches, partnerships, and
        # new coverage initiations all share the same 60-minute drift
        # thesis: a positive headline → short-term price momentum.
        _SUBSCRIPTIONS = [
            (EVENT_CATEGORY, EVENT_SUBCATEGORY),      # analyst/target_raise
            (EVENT_CATEGORY, "upgrade"),               # analyst/upgrade
            (EVENT_CATEGORY, "initiates"),             # analyst/initiates
            ("product", "launch"),                     # product launches
            ("product", "partnership"),                # strategic partnerships
        ]
        self._sub_ids: list = []
        for cat, sub in _SUBSCRIPTIONS:
            sid = self._await(
                self._bus.subscribe(cat, sub, self._on_event)
            )
            self._sub_ids.append(sid)

    @staticmethod
    def _await(coro: Any) -> Any:
        """Run an awaitable to completion regardless of loop context."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            # Inside a running loop — schedule and block via a new loop
            # is not possible; callers are expected to construct the
            # signal outside the loop or to await asynchronously. As a
            # fallback we schedule and return None; tests construct
            # outside the loop, so this branch is rarely hit.
            task = asyncio.ensure_future(coro)
            return task
        return asyncio.run(coro)

    def bootstrap_from_db(self, db_path: str, lookback_minutes: int | None = None) -> int:
        """Backfill in-memory _active_events from SQLite for analyst/target_raise
        events younger than max_event_age_minutes. Lets MultiSignal treat all
        sub-signals uniformly."""
        import sqlite3
        from datetime import datetime, timedelta, timezone
        self._db_path = db_path
        max_age = lookback_minutes or self.config.max_event_age_minutes
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age)).isoformat()
        try:
            conn = sqlite3.connect(db_path)
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(catalyst_events)").fetchall()
            }
            context_expr = "context_json" if "context_json" in columns else "NULL AS context_json"
            cur = conn.execute(
                "SELECT symbol, category, subcategory, pillar, event_ts, headline, "
                "source, horizon_minutes, headline_hash, sentiment, priority_modifier "
                f", {context_expr} FROM catalyst_events WHERE ("
                "  (category = 'analyst' AND subcategory IN ('target_raise', 'upgrade', 'initiates'))"
                "  OR (category = 'product' AND subcategory IN ('launch', 'partnership'))"
                ") AND event_ts >= ? "
                "ORDER BY event_ts ASC",
                (cutoff,),
            )
            rows = cur.fetchall()
            conn.close()
        except Exception:
            return 0
        loaded = 0
        for r in rows:
            try:
                ts = datetime.fromisoformat(r[4])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                ev = CatalystEvent(
                    symbol=r[0], category=r[1], subcategory=r[2],
                    pillar=r[3] or "micro", ts=ts, headline=r[5] or "",
                    source=r[6] or "db_bootstrap",
                    horizon_minutes=int(r[7] or 60), headline_hash=r[8] or "",
                    sentiment=r[9], priority_modifier=float(r[10] or 0.0),
                )
                self._active_events[ev.symbol.upper()] = ev
                if r[11]:
                    self._event_context_json[ev.symbol.upper()] = str(r[11])
                loaded += 1
            except (ValueError, TypeError):
                continue
        return loaded

    def refresh_sentiment_from_db(self) -> int:
        """Re-read sentiment/priority_modifier from DB for active events.

        Called periodically during scan() so that events enriched after
        bootstrap (e.g. by the Qwen batch enricher) become visible
        without an operator restart.

        Returns the number of events updated.
        """
        if self._db_path is None:
            return 0

        import sqlite3
        import time

        now_mono = time.monotonic()
        if now_mono - self._last_sentiment_refresh < self._sentiment_refresh_interval:
            return 0
        self._last_sentiment_refresh = now_mono

        symbols_needing_refresh = [
            sym for sym, ev in self._active_events.items()
            if ev.sentiment is None
        ]
        if not symbols_needing_refresh:
            return 0

        conn = sqlite3.connect(self._db_path)
        try:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(catalyst_events)").fetchall()
            }
            context_expr = "context_json" if "context_json" in columns else "NULL AS context_json"
            placeholders = ",".join("?" for _ in symbols_needing_refresh)
            rows = conn.execute(
                f"SELECT symbol, sentiment, priority_modifier, {context_expr} "
                f"FROM catalyst_events "
                f"WHERE symbol IN ({placeholders}) "
                f"AND ("
                f"  (category = 'analyst' AND subcategory IN ('target_raise', 'upgrade', 'initiates'))"
                f"  OR (category = 'product' AND subcategory IN ('launch', 'partnership'))"
                f") AND sentiment IS NOT NULL",
                symbols_needing_refresh,
            ).fetchall()
        finally:
            conn.close()

        updated = 0
        for row in rows:
            sym = row[0].upper()
            if sym in self._active_events:
                ev = self._active_events[sym]
                if ev.sentiment is None:
                    self._active_events[sym] = CatalystEvent(
                        symbol=ev.symbol,
                        category=ev.category,
                        subcategory=ev.subcategory,
                        pillar=ev.pillar,
                        ts=ev.ts,
                        headline=ev.headline,
                        source=ev.source,
                        horizon_minutes=ev.horizon_minutes,
                        headline_hash=ev.headline_hash,
                        sentiment=row[1],
                        priority_modifier=float(row[2] or 0.0),
                    )
                    if row[3]:
                        self._event_context_json[sym] = str(row[3])
                    updated += 1
        if updated:
            logger.info(
                "[SIGNAL:analyst_target_raise] refreshed sentiment for %d events "
                "from DB (%d still unenriched)",
                updated,
                len(symbols_needing_refresh) - updated,
            )
        return updated

    async def _on_event(self, event: CatalystEvent) -> None:
        """Bus callback — remember the most recent event per symbol."""
        symbol = event.symbol.upper()
        existing = self._active_events.get(symbol)
        if existing is None or event.ts >= existing.ts:
            self._active_events[symbol] = event

    def scan(self, *args: Any, **kwargs: Any) -> list[Candidate]:
        """Return one Candidate per symbol with a fresh active event.

        Drops events older than `max_event_age_minutes` from the
        in-memory store, then emits an allowed Candidate per remaining
        symbol scored by the event's `priority_modifier` (defaults to
        1.0 if zero, so equal-priority events still rank stably).
        """
        now = self._clock() if callable(self._clock) else self._clock
        # Periodically refresh sentiment from DB for unenriched events
        self.refresh_sentiment_from_db()

        fresh: dict[str, CatalystEvent] = {}
        for symbol, event in self._active_events.items():
            if is_event_fresh(now, event.ts, self.config.max_event_age_minutes):
                fresh[symbol] = event
        self._active_events = fresh
        self._event_context_json = {
            symbol: context_json
            for symbol, context_json in self._event_context_json.items()
            if symbol in fresh
        }

        require_sentiment = self.config.require_sentiment
        candidates: list[Candidate] = []
        skipped_no_sentiment = 0
        for symbol, event in fresh.items():
            # Directional gate: only admit events whose Qwen-enriched
            # sentiment matches the configured filter.  Events not yet
            # enriched (sentiment=None) are excluded when the filter is
            # active — Qwen IS the gate.
            if require_sentiment is not None and event.sentiment != require_sentiment:
                skipped_no_sentiment += 1
                continue
            score = event.priority_modifier if event.priority_modifier else 1.0
            candidates.append(
                Candidate(
                    symbol=symbol,
                    score=float(score),
                    sector="",
                    allowed=True,
                    blocked_reason=None,
                    features={
                        "event_category": event.category,
                        "event_subcategory": event.subcategory,
                        "event_ts": event.ts,
                        "headline_hash": event.headline_hash,
                        "horizon_minutes": event.horizon_minutes,
                        "source": event.source,
                        "sentiment": event.sentiment,
                        "priority_modifier": event.priority_modifier,
                        "context_json": self._event_context_json.get(symbol),
                    },
                )
            )
        if skipped_no_sentiment:
            logger.info(
                "[SIGNAL:analyst_target_raise] %d candidates emitted, "
                "%d skipped (sentiment != %r)",
                len(candidates), skipped_no_sentiment, require_sentiment,
            )
        candidates.sort(key=lambda c: (-c.score, c.symbol))
        return candidates

    def evaluate_exit(
        self,
        position: Any,
        latest_bar: Any | None = None,
        settings: Any | None = None,
    ) -> ExitDecision | None:
        """Delegate to `exits.evaluate_all` with current clock time.

        `position` must expose `entry_at` (or `entry_ts`) and either
        `unrealized_pct` or (`entry_price`, `latest_bar.close`).
        """
        now = self._clock() if callable(self._clock) else self._clock

        # DriftPilot's live PositionRecord stores entry_ts and current_price
        # inside `metadata`, not as direct attributes — same shape that
        # earnings_report_v1 already accommodates. Fall through metadata so
        # the monitor doesn't crash every cycle on AttributeError.
        metadata = getattr(position, "metadata", {}) or {}
        entry_ts = (
            getattr(position, "entry_ts", None)
            or getattr(position, "entry_at", None)
            or metadata.get("entry_ts")
            or metadata.get("entry_at")
        )
        if entry_ts is None:
            return None  # not enough data; let the time/price loop revisit
        # Persisted JSON makes datetimes round-trip as ISO strings — coerce.
        if isinstance(entry_ts, str):
            from datetime import datetime
            try:
                entry_ts = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
            except ValueError:
                return None

        unrealized_pct = getattr(position, "unrealized_pct", None)
        if unrealized_pct is None:
            entry_price = float(
                getattr(position, "entry_price", None)
                or metadata.get("entry_price")
                or 0.0
            )
            current_price = float(
                getattr(position, "current_price", None)
                or metadata.get("current_price")
                or (getattr(latest_bar, "close", entry_price) if latest_bar else entry_price)
                or entry_price
            )
            if entry_price <= 0:
                unrealized_pct = 0.0
            else:
                unrealized_pct = ((current_price - entry_price) / entry_price) * 100.0

        return evaluate_all(now, entry_ts, float(unrealized_pct), self.config)


__all__ = ["AnalystTargetRaiseV1Signal"]
