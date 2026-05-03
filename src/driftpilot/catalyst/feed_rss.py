from __future__ import annotations
import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from typing import Callable

from .classifier import CatalystClassifier
from .db import insert_event
from .event import CatalystEvent
from .event_bus import CatalystEventBus
from .qwen_enricher import QwenEnricher

logger = logging.getLogger(__name__)

# Default horizon by category — must agree with feed_alpaca.DEFAULT_HORIZON_BY_CATEGORY.
DEFAULT_HORIZON_BY_CATEGORY: dict[str, int] = {
    "earnings": 240, "analyst": 60, "filing": 60, "m_and_a": 60,
    "product": 60, "regulatory": 240, "legal": 240, "insider": 60,
    "macro": 240, "other": 60,
}

DEFAULT_FEEDS: tuple[str, ...] = (
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # Top news
    "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
)

# Match plausible US tickers (1-5 uppercase letters). Validated against universe.
_TICKER_RE = re.compile(r"\b([A-Z]{1,5})\b")


def _load_universe(universe_csv_path: str) -> set[str]:
    symbols: set[str] = set()
    with open(universe_csv_path) as f:
        next(f, None)  # header
        for line in f:
            sym = line.split(",", 1)[0].strip()
            if sym:
                symbols.add(sym)
    return symbols


class RssNewsFeed:
    def __init__(
        self,
        feed_urls: tuple[str, ...],
        universe: set[str],
        classifier: CatalystClassifier,
        enricher: QwenEnricher,
        bus: CatalystEventBus,
        db_path: str,
        poll_interval_s: int = 60,
        parser: Callable | None = None,  # injected for tests
    ) -> None:
        self._feed_urls = feed_urls
        self._universe = universe
        self._classifier = classifier
        self._enricher = enricher
        self._bus = bus
        self._db_path = db_path
        self._poll_interval_s = poll_interval_s
        self._parser = parser

    def _parse(self, url: str):
        if self._parser is not None:
            return self._parser(url)
        import feedparser
        return feedparser.parse(url)

    async def run(self) -> None:
        while True:
            try:
                count = await self._poll_once()
                logger.info("rss feed published %d events", count)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("rss poll failed: %s", exc)
            await asyncio.sleep(self._poll_interval_s)

    async def _poll_once(self) -> int:
        published = 0
        for url in self._feed_urls:
            try:
                feed = await asyncio.to_thread(self._parse, url)
            except Exception as exc:
                logger.warning("rss source %s failed: %s — skipping (additive only)", url, exc)
                continue

            entries = getattr(feed, "entries", []) or []
            for entry in entries:
                try:
                    n = await self._handle_entry(entry, source=url)
                    published += n
                except Exception as exc:
                    logger.warning("rss entry failed (%s): %s — continuing", type(exc).__name__, exc)
                    continue
        return published

    async def _handle_entry(self, entry, source: str) -> int:
        title = getattr(entry, "title", "") or ""
        if not title:
            return 0

        # Extract first plausible ticker that is in the universe
        symbol: str | None = None
        for candidate in _TICKER_RE.findall(title):
            if candidate in self._universe:
                symbol = candidate
                break
        if symbol is None:
            return 0

        category, subcategory, pillar = self._classifier.classify(title)
        if category == "other" and subcategory == "generic":
            return 0

        # Use entry's published time if available, else now
        ts = datetime.now(timezone.utc)
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                import time as _time
                ts = datetime.fromtimestamp(_time.mktime(entry.published_parsed), tz=timezone.utc)
            except Exception:
                pass

        horizon = DEFAULT_HORIZON_BY_CATEGORY.get(category, 60)
        headline_hash = hashlib.sha256(f"{symbol}|{title}".encode()).hexdigest()[:16]

        event = CatalystEvent(
            symbol=symbol,
            category=category,
            subcategory=subcategory,
            pillar=pillar,  # type: ignore[arg-type]
            ts=ts,
            headline=title,
            source=f"rss:{source[:32]}",
            horizon_minutes=horizon,
            headline_hash=headline_hash,
        )
        inserted = await asyncio.to_thread(insert_event, self._db_path, event)
        if inserted == 0:
            return 0

        enrichment = await self._enricher.enrich(title, category, subcategory)
        enriched = CatalystEvent(
            symbol=event.symbol, category=event.category, subcategory=event.subcategory,
            pillar=event.pillar, ts=event.ts, headline=event.headline, source=event.source,
            horizon_minutes=enrichment.horizon_override or event.horizon_minutes,
            headline_hash=event.headline_hash,
            sentiment=enrichment.sentiment,
            priority_modifier=enrichment.priority_modifier,
        )
        await self._bus.publish(enriched)
        return 1
