from __future__ import annotations
import asyncio

import pytest

from driftpilot.catalyst.discovery_service import DiscoveryService


@pytest.mark.asyncio
async def test_feeds_run_concurrently_until_cancelled():
    counts = {"a": 0, "b": 0}

    async def feed_a():
        while True:
            counts["a"] += 1
            await asyncio.sleep(0.01)

    async def feed_b():
        while True:
            counts["b"] += 1
            await asyncio.sleep(0.01)

    svc = DiscoveryService([("a", feed_a), ("b", feed_b)])
    task = asyncio.create_task(svc.start())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert counts["a"] > 0
    assert counts["b"] > 0


@pytest.mark.asyncio
async def test_one_feed_crash_does_not_kill_sibling():
    counts = {"good": 0, "bad_attempts": 0}

    async def good_feed():
        while True:
            counts["good"] += 1
            await asyncio.sleep(0.01)

    async def bad_feed():
        counts["bad_attempts"] += 1
        raise RuntimeError("boom")

    svc = DiscoveryService([("good", good_feed), ("bad", bad_feed)], restart_delay_s=0)
    task = asyncio.create_task(svc.start())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # bad_feed should have been restarted multiple times; good_feed kept running
    assert counts["good"] > 1
    assert counts["bad_attempts"] >= 1
