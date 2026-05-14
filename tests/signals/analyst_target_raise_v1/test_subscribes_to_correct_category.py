"""Verify the signal only reacts to (analyst, target_raise) events."""

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


def _event(category: str, subcategory: str, symbol: str = "AAPL") -> CatalystEvent:
    return CatalystEvent(
        symbol=symbol,
        category=category,
        subcategory=subcategory,
        pillar="micro",
        ts=T0,
        headline=f"{symbol} {category}/{subcategory}",
        source="unit-test",
        horizon_minutes=60,
        headline_hash=f"hash-{symbol}-{category}-{subcategory}",
        sentiment="positive",
        priority_modifier=0.08,
    )


def test_only_subscribes_to_analyst_target_raise() -> None:
    bus = CatalystEventBus()
    now_holder = {"now": T0 + timedelta(minutes=10)}
    sig = AnalystTargetRaiseV1Signal(
        AnalystTargetRaiseConfig(),
        bus,
        clock=lambda: now_holder["now"],
    )

    # 1) Publish an off-topic earnings event — must NOT produce a candidate.
    asyncio.run(bus.publish(_event("earnings", "report", symbol="AAPL")))
    assert sig.scan() == []

    # 2) Publish the matching analyst/target_raise event — MUST produce 1.
    asyncio.run(bus.publish(_event("analyst", "target_raise", symbol="AAPL")))
    candidates = sig.scan()
    assert len(candidates) == 1
    assert candidates[0].symbol == "AAPL"
    assert candidates[0].allowed is True
