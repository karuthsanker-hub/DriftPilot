"""Filing 8-A v1 — bus-driven catalyst signal.

Architecturally identical to earnings_report_v1 — the only difference is the
bus subscription tuple `(filing, 8a)`. We reuse earnings_report's exits.py
and features.py since the time/profit/stop math is independent of the
catalyst category.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus, SubscriptionId
from driftpilot.signals.base import Candidate, ExitDecision
from driftpilot.signals.earnings_report_v1.exits import evaluate_all
from driftpilot.signals.earnings_report_v1.features import event_age_minutes
from driftpilot.signals.filing_8a_v1.config import Filing8AConfig


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

    async def scan(self, now: datetime | None = None) -> list[Candidate]:
        if now is None:
            now = datetime.now(timezone.utc)
        candidates: list[Candidate] = []
        max_age = self._config.max_event_age_minutes
        require_sentiment = self._config.require_sentiment
        for symbol, event in self._active_events.items():
            age = event_age_minutes(event.ts, now)
            if age > max_age:
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
