"""Earnings Report v1 — post-earnings 60m drift signal.

Listens to CatalystEventBus for (category="earnings", subcategory="report")
events. The bus is the ONLY data source — this signal does not poll Alpaca
or any other API. It exposes a slim scan() that returns one Candidate per
fresh-enough catalyst, and an evaluate_exit() that applies the locked
time/profit/stop precedence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus, SubscriptionId
from driftpilot.signals.base import Candidate, ExitDecision
from driftpilot.signals.earnings_report_v1.config import EarningsReportConfig
from driftpilot.signals.earnings_report_v1.exits import evaluate_all
from driftpilot.signals.earnings_report_v1.features import event_age_minutes


SIGNAL_NAME = "earnings_report_v1"
SIGNAL_VERSION = "1.0.0"


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

    async def _on_event(self, event: CatalystEvent) -> None:
        # Latest event wins, keyed by symbol.
        self._active_events[event.symbol.upper()] = event

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
            # Directional gate (v3 GATED config): only admit events whose
            # Qwen-enriched sentiment matches the configured filter.
            # Events not yet enriched (sentiment=None) are excluded when
            # the filter is active — Qwen IS the gate.
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
        if entry_ts is None or entry_price is None:
            return None
        entry_price_f = float(entry_price)
        if entry_price_f <= 0:
            return None
        current_price = float(getattr(position, "current_price", entry_price_f))
        unrealized_pct = (current_price - entry_price_f) / entry_price_f * 100.0

        should_close, reason = evaluate_all(
            now=now,
            entry_ts=entry_ts,
            unrealized_pct=unrealized_pct,
            cfg=self._config,
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
