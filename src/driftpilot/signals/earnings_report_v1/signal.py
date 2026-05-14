"""Earnings Report v1 — post-earnings 60m drift signal.

Listens to CatalystEventBus for (category="earnings", subcategory="report")
events. The bus is the ONLY data source — this signal does not poll Alpaca
or any other API. It exposes a slim scan() that returns one Candidate per
fresh-enough catalyst, and an evaluate_exit() that applies the locked
time/profit/stop precedence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus, SubscriptionId
from driftpilot.signals.base import Candidate, ExitDecision
from driftpilot.signals.earnings_report_v1.config import EarningsReportConfig
from driftpilot.signals.earnings_report_v1.exits import evaluate_all
from driftpilot.signals.earnings_report_v1.features import event_age_minutes


logger = logging.getLogger(__name__)

SIGNAL_NAME = "earnings_report_v1"
SIGNAL_VERSION = "1.0.0"

_NEGATIVE_EARNINGS_HEADLINE_PHRASES = (
    "downbeat",
    "below estimates",
    "weak guidance",
    "posts loss",
    "widens loss",
)
_NEGATIVE_EARNINGS_HEADLINE_TOKENS = (
    "misses",
    "lowers",
)
_NEGATIVE_EARNINGS_CUTS_CONTEXTS = (
    "cuts guidance",
    "cuts forecast",
    "cuts outlook",
    "cuts view",
    "cuts estimate",
    "cuts estimates",
    "cuts dividend",
)


def _has_negative_earnings_headline_veto(headline: str) -> bool:
    normalized = " ".join(headline.casefold().split())
    if any(phrase in normalized for phrase in _NEGATIVE_EARNINGS_HEADLINE_PHRASES):
        return True
    words = set(normalized.replace(",", " ").replace(";", " ").split())
    if any(token in words for token in _NEGATIVE_EARNINGS_HEADLINE_TOKENS):
        return True
    return any(phrase in normalized for phrase in _NEGATIVE_EARNINGS_CUTS_CONTEXTS)


class EarningsReportSignal:
    """SignalProtocol implementation for Earnings Report v1."""

    name: str = SIGNAL_NAME
    version: str = SIGNAL_VERSION

    def __init__(
        self,
        config: EarningsReportConfig,
        bus: CatalystEventBus,
    ) -> None:
        self._config = config
        self._bus = bus
        self._active_events: dict[str, CatalystEvent] = {}
        self._sub_id: SubscriptionId | None = None
        self._db_path: str | None = None
        self._event_confidence: dict[str, float | None] = {}
        self._last_skip_counts: dict[str, int] = {}
        self._last_sentiment_refresh: float = 0.0
        self._sentiment_refresh_interval: int = 120  # seconds

    @property
    def last_skip_counts(self) -> dict[str, int]:
        return dict(self._last_skip_counts)

    async def _on_event(self, event: CatalystEvent) -> None:
        # Latest event wins, keyed by symbol.
        symbol = event.symbol.upper()
        self._active_events[symbol] = event
        self._event_confidence[symbol] = getattr(event, "confidence", None)

    async def subscribe(self) -> None:
        """Subscribe to the bus for earnings/report events."""
        if self._sub_id is not None:
            return
        self._sub_id = await self._bus.subscribe(
            "earnings", "report", self._on_event
        )

    async def unsubscribe(self) -> None:
        if self._sub_id is None:
            return
        await self._bus.unsubscribe(self._sub_id)
        self._sub_id = None

    def bootstrap_from_db(self, db_path: str, lookback_minutes: int | None = None) -> int:
        """Populate _active_events from the catalyst SQLite for events <
        max_event_age_minutes old at startup.

        Without this, a freshly-restarted operator only sees events
        published AFTER startup. Events that landed before startup but
        within the signal's age window are invisible until they are
        re-published — which never happens (UNIQUE constraint dedups
        the source).

        Returns the number of events loaded.
        """
        import sqlite3
        from datetime import datetime, timedelta, timezone

        from driftpilot.catalyst.event import CatalystEvent

        self._db_path = db_path
        max_age = lookback_minutes or self._config.max_event_age_minutes
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age)).isoformat()

        conn = sqlite3.connect(db_path)
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(catalyst_events)")}
            confidence_expr = "confidence" if "confidence" in columns else "NULL AS confidence"
            cur = conn.execute(
                "SELECT symbol, category, subcategory, pillar, event_ts, headline, "
                "source, horizon_minutes, headline_hash, sentiment, priority_modifier, "
                f"{confidence_expr} "
                "FROM catalyst_events "
                "WHERE category = 'earnings' AND subcategory = 'report' "
                "AND event_ts >= ? "
                "ORDER BY event_ts ASC",
                (cutoff,),
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
                symbol = event.symbol.upper()
                self._active_events[symbol] = event
                self._event_confidence[symbol] = (
                    float(row[11]) if row[11] is not None else None
                )
                loaded += 1
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "[SIGNAL] skipping malformed earnings event from DB: %s",
                    exc,
                )
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
            if ev.sentiment is None or self._event_confidence.get(sym) is None
        ]
        if not symbols_needing_refresh:
            return 0

        conn = sqlite3.connect(self._db_path)
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(catalyst_events)")}
            confidence_expr = "confidence" if "confidence" in columns else "NULL AS confidence"
            placeholders = ",".join("?" for _ in symbols_needing_refresh)
            rows = conn.execute(
                f"SELECT symbol, sentiment, priority_modifier, {confidence_expr} "
                f"FROM catalyst_events "
                f"WHERE symbol IN ({placeholders}) "
                f"AND category = 'earnings' AND subcategory = 'report' "
                f"AND sentiment IS NOT NULL",
                symbols_needing_refresh,
            ).fetchall()
        finally:
            conn.close()

        updated = 0
        for row in rows:
            sym = row[0].upper()
            if sym in self._active_events:
                ev = self._active_events[sym]
                # Update in-place by creating a new event with enriched fields.
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
                self._event_confidence[sym] = (
                    float(row[3]) if row[3] is not None else None
                )
                updated += 1
        if updated:
            logger.info(
                "[SIGNAL] refreshed sentiment metadata for %d events from DB "
                "(%d still missing metadata)",
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
        skip_counts: dict[str, int] = {}

        def count_skip(reason: str) -> None:
            skip_counts[reason] = skip_counts.get(reason, 0) + 1

        max_age = self._config.max_event_age_minutes
        require_sentiment = self._config.require_sentiment
        for symbol, event in self._active_events.items():
            age = event_age_minutes(event.ts, now)
            if age > max_age:
                count_skip("stale_event")
                continue
            if _has_negative_earnings_headline_veto(event.headline):
                count_skip("negative_headline_veto")
                continue
            # Directional gate (v3 GATED config): only admit events whose
            # Qwen-enriched sentiment matches the configured filter.
            # Events not yet enriched (sentiment=None) are excluded when
            # the filter is active — Qwen IS the gate.
            if require_sentiment is not None and event.sentiment != require_sentiment:
                count_skip("sentiment_mismatch")
                continue
            if (
                require_sentiment == "positive"
                and self._config.require_positive_priority_modifier
                and event.priority_modifier <= 0.0
            ):
                count_skip("non_positive_priority_modifier")
                continue
            min_confidence = self._config.min_sentiment_confidence
            confidence = self._event_confidence.get(symbol)
            if (
                min_confidence > 0.0
                and confidence is not None
                and confidence < min_confidence
            ):
                count_skip("low_sentiment_confidence")
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
                        "sentiment_confidence": confidence,
                        "category": event.category,
                        "subcategory": event.subcategory,
                        "catalyst_event_ts": event.ts,
                    },
                )
            )
        self._last_skip_counts = skip_counts
        if skip_counts:
            logger.debug("[SIGNAL] earnings_report_v1 skip counts: %s", skip_counts)
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
        # Persisted JSON makes datetimes round-trip as ISO strings — coerce.
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
        # PositionRecord has no current_price attribute; the live monitor
        # stores it in metadata['current_price']. Without this fallback,
        # signal computes unrealized_pct=0 always — trailing_stop then
        # fires whenever peak >= activation + distance (regardless of
        # actual price), which masks intra-position price movement.
        current_price = float(
            getattr(position, "current_price", None)
            or metadata.get("current_price")
            or entry_price_f
        )
        unrealized_pct = (current_price - entry_price_f) / entry_price_f * 100.0

        # Trailing stop needs the running peak. The position monitor maintains
        # peak_unrealized_pct in the position's metadata before calling here.
        peak_unrealized_pct = max(
            float(metadata.get("peak_unrealized_pct", 0.0)),
            unrealized_pct,  # cover the case where metadata wasn't updated yet
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


def signal_data_dependencies() -> tuple[str, ...]:
    """Earnings Report v1 reads only from the catalyst event bus."""
    return ()


def signal_required_history_minutes() -> int:
    """No price-history warm-up required; entry is event-triggered."""
    return 0


__all__ = [
    "EarningsReportSignal",
    "SIGNAL_NAME",
    "SIGNAL_VERSION",
    "signal_data_dependencies",
    "signal_required_history_minutes",
]
