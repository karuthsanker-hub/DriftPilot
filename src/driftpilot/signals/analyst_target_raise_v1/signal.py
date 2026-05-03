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
        self._sub_id: str | None = None

        # Subscribe synchronously at construction. The bus subscribe
        # method is async; resolve it on the running loop or via
        # asyncio.run if no loop is active (test harnesses do this).
        self._sub_id = self._await(
            self._bus.subscribe(
                EVENT_CATEGORY, EVENT_SUBCATEGORY, self._on_event
            )
        )

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
        fresh: dict[str, CatalystEvent] = {}
        for symbol, event in self._active_events.items():
            if is_event_fresh(now, event.ts, self.config.max_event_age_minutes):
                fresh[symbol] = event
        self._active_events = fresh

        candidates: list[Candidate] = []
        for symbol, event in fresh.items():
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
                    },
                )
            )
        candidates.sort(key=lambda c: (-c.score, c.symbol))
        return candidates

    def evaluate_exit(
        self,
        position: Any,
        latest_bar: Any | None = None,
        settings: Any | None = None,
    ) -> ExitDecision:
        """Delegate to `exits.evaluate_all` with current clock time.

        `position` must expose `entry_at` (or `entry_ts`) and either
        `unrealized_pct` or (`entry_price`, `latest_bar.close`).
        """
        now = self._clock() if callable(self._clock) else self._clock

        entry_ts = (
            getattr(position, "entry_ts", None)
            or getattr(position, "entry_at", None)
        )
        if entry_ts is None:
            raise AttributeError(
                "position must expose entry_ts or entry_at"
            )

        unrealized_pct = getattr(position, "unrealized_pct", None)
        if unrealized_pct is None:
            entry_price = float(getattr(position, "entry_price", 0.0))
            close = float(getattr(latest_bar, "close", entry_price)) if latest_bar else entry_price
            if entry_price <= 0:
                unrealized_pct = 0.0
            else:
                unrealized_pct = ((close - entry_price) / entry_price) * 100.0

        return evaluate_all(now, entry_ts, float(unrealized_pct), self.config)


__all__ = ["AnalystTargetRaiseV1Signal"]
