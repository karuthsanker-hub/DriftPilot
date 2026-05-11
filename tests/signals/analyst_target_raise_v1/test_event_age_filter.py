"""Events older than max_event_age_minutes must NOT produce candidates."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone


from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.signals.analyst_target_raise_v1 import (
    AnalystTargetRaiseConfig,
    AnalystTargetRaiseV1Signal,
)


T0 = datetime(2024, 6, 3, 14, 30, tzinfo=timezone.utc)


def _event(symbol: str, ts: datetime) -> CatalystEvent:
    return CatalystEvent(
        symbol=symbol,
        category="analyst",
        subcategory="target_raise",
        pillar="micro",
        ts=ts,
        headline=f"{symbol} target raised",
        source="unit-test",
        horizon_minutes=60,
        headline_hash=f"hash-{symbol}-{ts.isoformat()}",
    )


def test_stale_event_does_not_produce_candidate() -> None:
    bus = CatalystEventBus()
    now_holder = {"now": T0 + timedelta(minutes=120)}
    sig = AnalystTargetRaiseV1Signal(
        AnalystTargetRaiseConfig(),
        bus,
        clock=lambda: now_holder["now"],
    )

    # Event published at T0 — 120 minutes stale relative to clock.
    asyncio.run(bus.publish(_event("AAPL", T0)))
    candidates = sig.scan()
    assert candidates == []


def test_fresh_event_produces_candidate() -> None:
    bus = CatalystEventBus()
    now_holder = {"now": T0 + timedelta(minutes=30)}
    sig = AnalystTargetRaiseV1Signal(
        AnalystTargetRaiseConfig(),
        bus,
        clock=lambda: now_holder["now"],
    )

    asyncio.run(bus.publish(_event("AAPL", T0)))
    candidates = sig.scan()
    assert len(candidates) == 1
    assert candidates[0].symbol == "AAPL"
    assert candidates[0].allowed is True


def test_event_at_exact_max_age_is_still_fresh() -> None:
    bus = CatalystEventBus()
    cfg = AnalystTargetRaiseConfig()
    now_holder = {"now": T0 + timedelta(minutes=cfg.max_event_age_minutes)}
    sig = AnalystTargetRaiseV1Signal(cfg, bus, clock=lambda: now_holder["now"])

    asyncio.run(bus.publish(_event("MSFT", T0)))
    candidates = sig.scan()
    assert len(candidates) == 1


def test_event_one_minute_past_max_age_is_dropped() -> None:
    bus = CatalystEventBus()
    cfg = AnalystTargetRaiseConfig()
    now_holder = {"now": T0 + timedelta(minutes=cfg.max_event_age_minutes + 1)}
    sig = AnalystTargetRaiseV1Signal(cfg, bus, clock=lambda: now_holder["now"])

    asyncio.run(bus.publish(_event("MSFT", T0)))
    candidates = sig.scan()
    assert candidates == []
