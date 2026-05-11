from __future__ import annotations
from dataclasses import dataclass

import pytest

from driftpilot.catalyst.classifier import CatalystClassifier
from driftpilot.catalyst.db import init_catalyst_schema
from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.catalyst.feed_rss import RssNewsFeed
from driftpilot.catalyst.qwen_enricher import EnrichmentResult, QwenEnricher


class FakeEnricher(QwenEnricher):
    def __init__(self):
        pass

    async def enrich(self, *_):
        return EnrichmentResult("neutral", 0.0, None)


@dataclass
class FakeEntry:
    title: str
    published_parsed = None


@dataclass
class FakeFeed:
    entries: list


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "rss.db")
    init_catalyst_schema(p)
    return p


@pytest.mark.asyncio
async def test_feedparser_exception_does_not_crash(db_path):
    """If feedparser raises, _poll_once should return 0, not raise."""
    bus = CatalystEventBus()
    universe = {"AAPL"}
    raised = {"count": 0}

    def bad_parser(url):
        raised["count"] += 1
        raise RuntimeError("network unreachable")

    feed = RssNewsFeed(
        feed_urls=("https://example.com/feed",),
        universe=universe,
        classifier=CatalystClassifier(),
        enricher=FakeEnricher(),
        bus=bus,
        db_path=db_path,
        parser=bad_parser,
    )
    n = await feed._poll_once()
    assert n == 0
    assert raised["count"] == 1


@pytest.mark.asyncio
async def test_one_bad_feed_does_not_block_others(db_path):
    """First feed raises, second feed returns valid entry → published=1."""
    bus = CatalystEventBus()
    universe = {"AAPL", "MSFT"}
    received: list[CatalystEvent] = []

    async def cb(ev):
        received.append(ev)

    await bus.subscribe(None, None, cb)

    def parser(url):
        if "bad" in url:
            raise RuntimeError("boom")
        return FakeFeed(entries=[FakeEntry(title="Apple beats earnings AAPL")])

    feed = RssNewsFeed(
        feed_urls=("https://bad.example.com/feed", "https://good.example.com/feed"),
        universe=universe,
        classifier=CatalystClassifier(),
        enricher=FakeEnricher(),
        bus=bus,
        db_path=db_path,
        parser=parser,
    )
    n = await feed._poll_once()
    assert n == 1
    assert len(received) == 1
    assert received[0].symbol == "AAPL"


@pytest.mark.asyncio
async def test_no_universe_match_skipped(db_path):
    bus = CatalystEventBus()
    universe = {"AAPL"}  # NVDA won't match

    def parser(url):
        return FakeFeed(entries=[FakeEntry(title="NVDA beats earnings")])

    feed = RssNewsFeed(
        feed_urls=("https://example.com/feed",),
        universe=universe,
        classifier=CatalystClassifier(),
        enricher=FakeEnricher(),
        bus=bus,
        db_path=db_path,
        parser=parser,
    )
    n = await feed._poll_once()
    assert n == 0
