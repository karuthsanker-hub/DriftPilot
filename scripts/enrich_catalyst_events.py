#!/usr/bin/env python3
"""Backfill Qwen sentiment for events in the catalyst SQLite DB.

Reads rows where sentiment IS NULL, calls Qwen on DGX with bounded
concurrency, writes sentiment + priority_modifier + horizon_override
back. Idempotent — re-running only enriches the still-NULL rows.

Usage:
  python scripts/enrich_catalyst_events.py \\
      --db data/driftpilot/catalyst_events_2024.sqlite3 \\
      --concurrency 16

  # smoke test on 100 events first
  python scripts/enrich_catalyst_events.py --db <path> --limit 100
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from driftpilot.catalyst.qwen_enricher import (  # noqa: E402
    EnrichmentResult,
    QwenEnricher,
    _strip_thinking_and_extract_json,
)

logging.basicConfig(format="[%(asctime)s] %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("enricher")


def _fetch_pending(
    db_path: str,
    limit: int | None = None,
    categories: list[tuple[str, str]] | None = None,
) -> list[tuple]:
    """Return rows that need enrichment: (id, headline, category, subcategory)."""
    conn = sqlite3.connect(db_path)
    try:
        sql = (
            "SELECT id, headline, category, subcategory FROM catalyst_events "
            "WHERE sentiment IS NULL"
        )
        params: list = []
        if categories:
            placeholders = ",".join("(?,?)" for _ in categories)
            sql += f" AND (category, subcategory) IN ({placeholders})"
            for cat, sub in categories:
                params.extend([cat, sub])
        sql += " ORDER BY id"
        if limit:
            sql += f" LIMIT {int(limit)}"
        cur = conn.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _update_row(db_path: str, row_id: int, result: EnrichmentResult) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE catalyst_events SET sentiment = ?, priority_modifier = ?, "
            "horizon_minutes = COALESCE(?, horizon_minutes) WHERE id = ?",
            (result.sentiment, result.priority_modifier, result.horizon_override, row_id),
        )
        conn.commit()
    finally:
        conn.close()


async def _enrich_one(
    sem: asyncio.Semaphore,
    client: httpx.AsyncClient,
    enricher: QwenEnricher,
    db_path: str,
    row: tuple,
) -> str:
    """Enrich one row; return the sentiment label (for stats)."""
    async with sem:
        row_id, headline, category, subcategory = row
        # Inject our shared client into the enricher for this call to avoid
        # creating one connection per request.
        enricher._client = client
        result = await enricher.enrich(headline, category, subcategory)
        # DB write isn't async-friendly via sqlite3, but it's fast enough.
        _update_row(db_path, row_id, result)
        return result.sentiment


async def main_async(args) -> None:
    cats = None
    if args.priority_only:
        cats = [
            ("earnings", "report"),
            ("earnings", "beat"),
            ("earnings", "miss"),
            ("earnings", "guidance_up"),
            ("earnings", "guidance_down"),
            ("analyst", "target_raise"),
            ("analyst", "target_cut"),
            ("analyst", "upgrade"),
            ("analyst", "downgrade"),
            ("m_and_a", "acquires"),
            ("m_and_a", "merger"),
        ]
    pending = _fetch_pending(args.db, args.limit, categories=cats)
    if not pending:
        logger.info("no rows pending enrichment — DB is fully enriched")
        return

    logger.info(
        "enriching %d events using %s (concurrency=%d, timeout=%dms)",
        len(pending), args.qwen_url, args.concurrency, args.timeout_ms,
    )

    enricher = QwenEnricher(
        base_url=args.qwen_url,
        model=args.model,
        timeout_ms=args.timeout_ms,
    )
    sem = asyncio.Semaphore(args.concurrency)

    # Shared httpx client = pooled connections, no per-request handshake cost.
    async with httpx.AsyncClient(timeout=args.timeout_ms / 1000.0 + 1) as client:
        t_start = time.time()
        results: list[str] = []
        # Process in batches so we can log progress mid-flight.
        BATCH = max(args.concurrency, 200)
        for batch_start in range(0, len(pending), BATCH):
            batch = pending[batch_start: batch_start + BATCH]
            tasks = [_enrich_one(sem, client, enricher, args.db, row) for row in batch]
            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)
            done = batch_start + len(batch)
            elapsed = time.time() - t_start
            rate = done / elapsed if elapsed > 0 else 0
            eta = (len(pending) - done) / rate if rate > 0 else 0
            counts = Counter(results)
            logger.info(
                "  progress: %d/%d (%.1f ev/s, eta %.0fs)  pos=%d neg=%d neu=%d",
                done, len(pending), rate, eta,
                counts.get("positive", 0), counts.get("negative", 0), counts.get("neutral", 0),
            )

    elapsed = time.time() - t_start
    counts = Counter(results)
    logger.info("=" * 70)
    logger.info("DONE in %.1fs (%.1f min)", elapsed, elapsed / 60)
    logger.info("sentiment distribution:")
    total = len(results)
    for label in ("positive", "negative", "neutral"):
        n = counts.get(label, 0)
        logger.info("  %-10s %5d (%.1f%%)", label, n, n / total * 100 if total else 0)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/driftpilot/catalyst_events_2024.sqlite3")
    p.add_argument("--qwen-url", default="http://192.168.1.166:8000/v1")
    p.add_argument("--model", default="Qwen/Qwen3-8B")
    p.add_argument("--timeout-ms", type=int, default=10000, help="Per-call timeout (ms)")
    p.add_argument("--concurrency", type=int, default=16, help="Concurrent Qwen requests")
    p.add_argument("--limit", type=int, default=0, help="Cap rows enriched (smoke testing)")
    p.add_argument("--priority-only", action="store_true",
                   help="Only enrich trading-relevant categories (earnings, analyst, m_and_a)")
    args = p.parse_args()
    args.limit = args.limit or None

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
