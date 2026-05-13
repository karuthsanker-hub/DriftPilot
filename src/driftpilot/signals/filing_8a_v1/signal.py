"""Filing 8-A v1 — bus-driven catalyst signal.

Architecturally identical to earnings_report_v1 — the only difference is the
bus subscription tuple `(filing, 8a)`. We reuse earnings_report's exits.py
and features.py since the time/profit/stop math is independent of the
catalyst category.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus, SubscriptionId
from driftpilot.signals.base import Candidate, ExitDecision
from driftpilot.signals.earnings_report_v1.exits import evaluate_all
from driftpilot.signals.earnings_report_v1.features import event_age_minutes
from driftpilot.signals.filing_8a_v1.config import Filing8AConfig


logger = logging.getLogger(__name__)

SIGNAL_NAME = "filing_8a_v1"
SIGNAL_VERSION = "1.0.0"
EVENT_CATEGORY = "filing"
EVENT_SUBCATEGORY = "8a"


class Filing8ASignal:
    """SignalProtocol implementation for Filing 8-A v1."""

    name: str = SIGNAL_NAME
    version: str = SIGNAL_VERSION

    def __init__(
        self,
        config: Filing8AConfig,
        bus: CatalystEventBus,
    ) -> None:
        self._config = config
        self._bus = bus
        self._active_events: dict[str, CatalystEvent] = {}
        self._sub_id: SubscriptionId | None = None
        self._db_path: str | None = None
        self._last_sentiment_refresh: float = 0.0
        self._sentiment_refresh_interval: int = 120  # seconds

    async def _on_event(self, event: CatalystEvent) -> None:
        self._active_events[event.symbol.upper()] = event

    async def subscribe(self) -> None:
        if self._sub_id is not None:
            return
        self._sub_id = await self._bus.subscribe(
            EVENT_CATEGORY, EVENT_SUBCATEGORY, self._on_event
        )

    async def unsubscribe(self) -> None:
        if self._sub_id is None:
            return
        await self._bus.unsubscribe(self._sub_id)
        self._sub_id = None

    def bootstrap_from_db(self, db_path: str, lookback_minutes: int | None = None) -> int:
        """Backfill _active_events from SQLite for events younger than
        max_event_age_minutes. Mirrors earnings_report_v1.bootstrap_from_db."""
        import sqlite3
        from datetime import datetime, timedelta, timezone

        self._db_path = db_path
        max_age = lookback_minutes or self._config.max_event_age_minutes
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age)).isoformat()

        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "SELECT symbol, category, subcategory, pillar, event_ts, headline, "
                "source, horizon_minutes, headline_hash, sentiment, priority_modifier "
                "FROM catalyst_events "
                "WHERE category = ? AND subcategory = ? AND event_ts >= ? "
                "ORDER BY event_ts ASC",
                (EVENT_CATEGORY, EVENT_SUBCATEGORY, cutoff),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        loaded = 0
        for row in rows:
            try:
                ts = datetime.fromisoformat(row[4])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                event = CatalystEvent(
                    symbol=row[0],
                    category=row[1],
                    subcategory=row[2],
                    pillar=row[3] or "micro",  # type: ignore[arg-type]
                    ts=ts,
                    headline=row[5] or "",
                    source=row[6] or "db_bootstrap",
                    horizon_minutes=int(row[7] or 60),
                    headline_hash=row[8] or "",
                    sentiment=row[9],
                    priority_modifier=float(row[10] or 0.0),
                )
                self._active_events[event.symbol.upper()] = event
                loaded += 1
            except (ValueError, TypeError):
                continue
        return loaded

    def refresh_sentiment_from_db(self) -> int:
        """Re-read sentiment/priority_modifier from DB for active events.

        Called periodically during scan() so that events enriched after
        bootstrap become visible without an operator restart.
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
            placeholders = ",".join("?" for _ in symbols_needing_refresh)
            rows = conn.execute(
                f"SELECT symbol, sentiment, priority_modifier "
                f"FROM catalyst_events "
                f"WHERE symbol IN ({placeholders}) "
                f"AND category = ? AND subcategory = ? "
                f"AND sentiment IS NOT NULL",
                [*symbols_needing_refresh, EVENT_CATEGORY, EVENT_SUBCATEGORY],
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
                    updated += 1
        if updated:
            logger.info(
                "[SIGNAL] refreshed sentiment for %d events from DB "
                "(%d still unenriched)",
                updated,
                len(symbols_needing_refresh) - updated,
            )
        return updated

    async def scan(self, now: datetime | None = None) -> list[Candidate]:
        if now is None:
            now = datetime.now(timezone.utc)
        # Periodically refresh sentiment from DB for unenriched events
        self.refresh_sentiment_from_db()
        candidates: list[Candidate] = []
        max_age = self._config.max_event_age_minutes
        require_sentiment = self._config.require_sentiment
        exclude_negative = getattr(self._config, "exclude_negative", True)
        for symbol, event in self._active_events.items():
            age = event_age_minutes(event.ts, now)
            if age > max_age:
                continue
            if exclude_negative and event.sentiment == "negative":
                continue
            if require_sentiment is not None and event.sentiment != require_sentiment:
                continue
            candidates.append(
                Candidate(
                    symbol=symbol,
                    score=float(event.priority_modifier),
                    sector="",
                    allowed=True,
                    blocked_reason=None,
                    features={
                        "event_age_minutes": age,
                        "horizon_minutes": event.horizon_minutes,
                        "headline": event.headline,
                        "headline_hash": event.headline_hash,
                        "source": event.source,
                        "sentiment": event.sentiment,
                        "priority_modifier": event.priority_modifier,
                        "category": event.category,
                        "subcategory": event.subcategory,
                        "catalyst_event_ts": event.ts,
                        "signal_name": SIGNAL_NAME,  # exit-router needs this
                    },
                )
            )
        return candidates

    def evaluate_exit(
        self,
        position: Any,
        now: datetime,
        *_args: Any,
        **_kwargs: Any,
    ) -> ExitDecision | None:
        metadata = getattr(position, "metadata", {}) or {}
        entry_ts = metadata.get("entry_ts")
        entry_price = metadata.get("entry_price")
        if isinstance(entry_ts, str):
            try:
                entry_ts = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
            except ValueError:
                entry_ts = None
        if entry_ts is None or entry_price is None:
            return None
        entry_price_f = float(entry_price)
        if entry_price_f <= 0:
            return None
        current_price = float(
            getattr(position, "current_price", None)
            or metadata.get("current_price")
            or entry_price_f
        )
        unrealized_pct = (current_price - entry_price_f) / entry_price_f * 100.0
        peak_unrealized_pct = max(
            float(metadata.get("peak_unrealized_pct", 0.0)),
            unrealized_pct,
        )
        should_close, reason = evaluate_all(
            now=now,
            entry_ts=entry_ts,
            unrealized_pct=unrealized_pct,
            cfg=self._config,
            peak_unrealized_pct=peak_unrealized_pct,
        )
        if should_close:
            return ExitDecision(
                should_exit=True,
                exit_reason=reason,
                metadata={"unrealized_pct": unrealized_pct},
            )
        return None


__all__ = ["Filing8ASignal", "SIGNAL_NAME", "SIGNAL_VERSION"]
