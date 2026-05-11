import pytest
from datetime import datetime, timezone

from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus


def make_event(category="earnings", subcategory="report") -> CatalystEvent:
    return CatalystEvent(
        symbol="AAPL",
        category=category,
        subcategory=subcategory,
        pillar="micro",
        ts=datetime.now(timezone.utc),
        headline="test headline",
        source="test",
        horizon_minutes=60,
        headline_hash="abc123",
    )


@pytest.mark.asyncio
async def test_subscribe_publish_callback_fires() -> None:
    bus = CatalystEventBus()
    received: list[CatalystEvent] = []

    async def cb(ev: CatalystEvent) -> None:
        received.append(ev)

    await bus.subscribe("earnings", "report", cb)
    await bus.publish(make_event())
    assert len(received) == 1


@pytest.mark.asyncio
async def test_wildcard_category_matches_all() -> None:
    bus = CatalystEventBus()
    received: list[CatalystEvent] = []

    async def cb(ev: CatalystEvent) -> None:
        received.append(ev)

    await bus.subscribe(None, None, cb)
    await bus.publish(make_event("earnings", "report"))
    await bus.publish(make_event("analyst", "target_raise"))
    assert len(received) == 2


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    bus = CatalystEventBus()
    received: list[CatalystEvent] = []

    async def cb(ev: CatalystEvent) -> None:
        received.append(ev)

    sub_id = await bus.subscribe(None, None, cb)
    await bus.publish(make_event())
    await bus.unsubscribe(sub_id)
    await bus.publish(make_event())
    assert len(received) == 1


@pytest.mark.asyncio
async def test_callback_exception_does_not_block_others() -> None:
    bus = CatalystEventBus()
    received: list[str] = []

    async def bad(ev: CatalystEvent) -> None:
        raise RuntimeError("boom")

    async def good(ev: CatalystEvent) -> None:
        received.append("ok")

    await bus.subscribe(None, None, bad)
    await bus.subscribe(None, None, good)
    await bus.publish(make_event())
    assert received == ["ok"]


@pytest.mark.asyncio
async def test_publish_with_no_subscribers() -> None:
    bus = CatalystEventBus()
    await bus.publish(make_event())  # no error
