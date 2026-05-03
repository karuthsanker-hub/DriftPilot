from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from driftpilot.catalyst.classifier import CatalystClassifier
from driftpilot.catalyst.db import init_catalyst_schema
from driftpilot.catalyst.event import CatalystEvent
from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.catalyst.feed_alpaca import AlpacaNewsFeed, DEFAULT_HORIZON_BY_CATEGORY
from driftpilot.catalyst.qwen_enricher import EnrichmentResult, QwenEnricher


class FakeEnricher(QwenEnricher):
    def __init__(self, result=None):
        self._result = result or EnrichmentResult("neutral", 0.0, None)

    async def enrich(self, headline, category, subcategory):
        return self._result


def _article(symbols, headline, ts=None):
    return SimpleNamespace(
        symbols=symbols,
        headline=headline,
        created_at=ts or datetime.now(timezone.utc),
    )


def _result_with(articles, next_token=None):
    return SimpleNamespace(
        data={"news": articles},
        next_page_token=next_token,
    )


def _mock_alpaca_client(articles):
    """Return a mock NewsClient whose get_news returns one page of `articles`."""
    client = MagicMock()
    client.get_news = MagicMock(return_value=_result_with(articles))
    return client


@pytest.fixture
def db_path(tmp_path):
    p = str(tmp_path / "test.db")
    init_catalyst_schema(p)
    return p


@pytest.mark.asyncio
async def test_poll_publishes_events(db_path):
    bus = CatalystEventBus()
    received: list[CatalystEvent] = []

    async def cb(ev):
        received.append(ev)

    await bus.subscribe(None, None, cb)

    articles = [
        _article(["AAPL"], "Apple beats earnings expectations"),
        _article(["MSFT"], "Goldman raises MSFT price target to 500"),
        _article(["NVDA"], "NVIDIA launches new GPU lineup"),
    ]
    feed = AlpacaNewsFeed(
        api_key="x",
        api_secret="y",
        symbols=["AAPL", "MSFT", "NVDA"],
        classifier=CatalystClassifier(),
        enricher=FakeEnricher(),
        bus=bus,
        db_path=db_path,
        client=_mock_alpaca_client(articles),
    )
    n = await feed._poll_once()
    assert n == 3
    assert len(received) == 3


@pytest.mark.asyncio
async def test_poll_dedupes_via_db(db_path):
    bus = CatalystEventBus()
    articles = [_article(["AAPL"], "Apple beats earnings")]
    feed = AlpacaNewsFeed(
        api_key="x", api_secret="y", symbols=["AAPL"],
        classifier=CatalystClassifier(), enricher=FakeEnricher(),
        bus=bus, db_path=db_path,
        client=_mock_alpaca_client(articles),
    )
    n1 = await feed._poll_once()
    n2 = await feed._poll_once()
    assert n1 == 1
    assert n2 == 0  # same article, deduped by DB


@pytest.mark.asyncio
async def test_other_generic_skipped(db_path):
    bus = CatalystEventBus()
    received: list[CatalystEvent] = []

    async def cb(ev):
        received.append(ev)

    await bus.subscribe(None, None, cb)

    # Headline that won't match any taxonomy rule
    articles = [_article(["AAPL"], "XYZ stock moves up slightly")]
    feed = AlpacaNewsFeed(
        api_key="x", api_secret="y", symbols=["AAPL"],
        classifier=CatalystClassifier(), enricher=FakeEnricher(),
        bus=bus, db_path=db_path,
        client=_mock_alpaca_client(articles),
    )
    n = await feed._poll_once()
    assert n == 0
    assert len(received) == 0


@pytest.mark.asyncio
async def test_qwen_offline_does_not_block_publishing(db_path):
    bus = CatalystEventBus()
    received: list[CatalystEvent] = []

    async def cb(ev):
        received.append(ev)

    await bus.subscribe(None, None, cb)

    # FakeEnricher always returns defaults — same as Qwen offline behavior
    articles = [_article(["AAPL"], "Apple beats earnings")]
    feed = AlpacaNewsFeed(
        api_key="x", api_secret="y", symbols=["AAPL"],
        classifier=CatalystClassifier(), enricher=FakeEnricher(),
        bus=bus, db_path=db_path,
        client=_mock_alpaca_client(articles),
    )
    n = await feed._poll_once()
    assert n == 1
    assert received[0].sentiment == "neutral"
    assert received[0].priority_modifier == 0.0
