from __future__ import annotations
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .classifier import CatalystClassifier
from .context_assembler import ContextAssembler
from .db import insert_event, update_enrichment
from .event import CatalystEvent
from .event_bus import CatalystEventBus
from .qwen_enricher import QwenEnricher

logger = logging.getLogger(__name__)


# Default horizon (in minutes) by category. Earnings tends to persist; analyst
# fades fast; filings cluster around the publish minute.
DEFAULT_HORIZON_BY_CATEGORY: dict[str, int] = {
    "earnings": 240,
    "analyst": 60,
    "filing": 60,
    "m_and_a": 60,
    "product": 60,
    "regulatory": 240,
    "legal": 240,
    "insider": 60,
    "macro": 240,
    "other": 60,
}


class AlpacaNewsFeed:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbols: list[str],
        classifier: CatalystClassifier,
        enricher: QwenEnricher,
        bus: CatalystEventBus,
        db_path: str,
        poll_interval_s: int = 30,
        chunk_size: int = 50,
        client=None,  # injected for tests
        context_assembler: ContextAssembler | None = None,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._symbols = symbols
        self._classifier = classifier
        self._enricher = enricher
        self._bus = bus
        self._db_path = db_path
        self._poll_interval_s = poll_interval_s
        self._chunk_size = chunk_size
        self._client = client
        self._context_assembler = context_assembler
        self._last_poll_ts = datetime.now(timezone.utc) - timedelta(minutes=10)

    def _get_client(self):
        if self._client is not None:
            return self._client
        from alpaca.data.historical.news import NewsClient
        return NewsClient(api_key=self._api_key, secret_key=self._api_secret)

    async def run(self) -> None:
        while True:
            try:
                count = await self._poll_once()
                logger.info("alpaca feed published %d events", count)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("alpaca poll failed: %s", exc)
            await asyncio.sleep(self._poll_interval_s)

    async def _poll_once(self) -> int:
        from alpaca.data.requests import NewsRequest

        now = datetime.now(timezone.utc)
        start = self._last_poll_ts
        self._last_poll_ts = now

        client = self._get_client()
        published = 0

        for chunk in _chunk(self._symbols, self._chunk_size):
            page_token: str | None = None
            while True:
                req = NewsRequest(
                    symbols=",".join(chunk),  # comma-separated string, NOT list
                    start=start,
                    end=now,
                    limit=50,
                    page_token=page_token,
                )
                result = await asyncio.to_thread(client.get_news, req)
                # NewsSet.data is a dict with "news" key, NOT a flat list
                articles = []
                if hasattr(result, "data") and isinstance(result.data, dict):
                    articles = result.data.get("news", [])
                elif hasattr(result, "news"):
                    articles = result.news

                for article in articles:
                    n = await self._handle_article(article)
                    published += n

                page_token = getattr(result, "next_page_token", None)
                if not page_token:
                    break

        return published

    async def _handle_article(self, article) -> int:
        # Article shape varies; use getattr fallbacks
        symbols = getattr(article, "symbols", []) or []
        headline = getattr(article, "headline", "") or ""
        article_ts = getattr(article, "created_at", None) or getattr(article, "updated_at", None) or datetime.now(timezone.utc)
        if isinstance(article_ts, str):
            article_ts = datetime.fromisoformat(article_ts.replace("Z", "+00:00"))

        if not headline or not symbols:
            return 0

        category, subcategory, pillar = self._classifier.classify(headline)
        if category == "other" and subcategory == "generic":
            return 0  # uncategorizable, skip

        horizon = DEFAULT_HORIZON_BY_CATEGORY.get(category, 60)
        published = 0

        for symbol in symbols:
            headline_hash = hashlib.sha256(f"{symbol}|{headline}".encode()).hexdigest()[:16]
            event = CatalystEvent(
                symbol=symbol,
                category=category,
                subcategory=subcategory,
                pillar=pillar,  # type: ignore[arg-type]
                ts=article_ts,
                headline=headline,
                source="alpaca",
                horizon_minutes=horizon,
                headline_hash=headline_hash,
            )
            inserted = await asyncio.to_thread(insert_event, self._db_path, event)
            if inserted == 0:
                continue  # dup

            # Build context for V2 prompt (best-effort; None fields are fine)
            context = None
            if self._context_assembler is not None:
                try:
                    context = self._context_assembler.build_context(
                        event.symbol, headline, event.ts, category, subcategory,
                    )
                except Exception:
                    logger.debug("context assembly failed for %s, using V1 prompt", event.symbol)
            # Enrich (best-effort; Qwen failures fall back to defaults inside enricher)
            enrichment = await self._enricher.enrich(headline, category, subcategory, context=context)
            enriched = CatalystEvent(
                symbol=event.symbol,
                category=event.category,
                subcategory=event.subcategory,
                pillar=event.pillar,
                ts=event.ts,
                headline=event.headline,
                source=event.source,
                horizon_minutes=enrichment.horizon_override or event.horizon_minutes,
                headline_hash=event.headline_hash,
                sentiment=enrichment.sentiment,
                priority_modifier=enrichment.priority_modifier,
            )
            # Patch the DB row (inserted at line above with sentiment=NULL)
            # with the enrichment results so DB readers — bootstrap, the news
            # ticker, the negative-catalyst gate — see the same sentiment that
            # the bus carries.
            await asyncio.to_thread(
                update_enrichment,
                self._db_path, enriched.headline_hash, enriched.symbol,
                sentiment=enriched.sentiment,
                priority_modifier=enriched.priority_modifier,
                horizon_minutes=enriched.horizon_minutes,
            )
            await self._bus.publish(enriched)
            published += 1
            # Audit log — one structured line per published event so we can
            # reconstruct every signal/order chain post-hoc by grep alone.
            logger.info(
                "EVENT %s %s/%s sentiment=%s priority=%+.2f horizon=%dm hash=%s | %s",
                enriched.symbol, enriched.category, enriched.subcategory,
                enriched.sentiment or "NONE", enriched.priority_modifier,
                enriched.horizon_minutes, enriched.headline_hash,
                (enriched.headline or "")[:120],
            )

        return published


def _chunk(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
