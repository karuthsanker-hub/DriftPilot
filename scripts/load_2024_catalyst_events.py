#!/usr/bin/env python3
"""Pull 2024 Alpaca News for the full universe and load into SQLite.

This is the input to the v3 retrofit backtests:
  - earnings_report_v1, analyst_target_raise_v1 (catalyst signals)
  - apex_hunter_v2_2, rs_drift_v1, whale_tail_v1, stationary_ghost_v1
    (technical signals re-backtested on the catalyst-filtered universe)

Reuses the spike's _fetch_news pagination pattern and the production
classifier from src/driftpilot/catalyst/classifier.py — same regex that
produced the validated 5.09x earnings/report and 1.42x analyst/target_raise
edge ratios.

Usage:
  python scripts/load_2024_catalyst_events.py \\
      --start 2024-01-01 --end 2024-12-31 \\
      --output data/driftpilot/catalyst_events_2024.sqlite3 \\
      --universe config/universe.csv

  # smoke test on 5 symbols × 1 month before the full pull
  python scripts/load_2024_catalyst_events.py \\
      --start 2024-01-01 --end 2024-01-31 --symbol-limit 5 \\
      --output /tmp/catalyst_smoke.sqlite3

Logs progress every 50 symbols. Idempotent — re-running on the same
output DB skips already-ingested events via the (symbol, headline_hash,
event_ts) UNIQUE constraint.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import dotenv_values  # noqa: E402

from driftpilot.catalyst.classifier import CatalystClassifier  # noqa: E402
from driftpilot.catalyst.db import init_catalyst_schema, insert_event  # noqa: E402
from driftpilot.catalyst.event import CatalystEvent  # noqa: E402

# Default horizon by category — must agree with feed_alpaca/feed_rss.
DEFAULT_HORIZON_BY_CATEGORY: dict[str, int] = {
    "earnings": 240, "analyst": 60, "filing": 60, "m_and_a": 60,
    "product": 60, "regulatory": 240, "legal": 240, "insider": 60,
    "macro": 240, "other": 60,
}
PILLAR_BY_CATEGORY: dict[str, str] = {
    "earnings": "micro", "analyst": "micro", "filing": "micro",
    "m_and_a": "micro", "product": "micro", "regulatory": "micro",
    "legal": "micro", "insider": "micro", "macro": "macro", "other": "micro",
}

logging.basicConfig(
    format="[%(asctime)s] %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("news_loader")


def _load_alpaca_keys(env_file: Path) -> tuple[str, str]:
    api = os.environ.get("ALPACA_API_KEY", "") or os.environ.get("ALPACA_KEY_ID", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if (not api or not sec) and env_file.exists():
        v = dotenv_values(env_file)
        api = api or v.get("ALPACA_API_KEY", "") or v.get("ALPACA_KEY_ID", "") or ""
        sec = sec or v.get("ALPACA_SECRET_KEY", "") or ""
    return api, sec


def _load_universe(universe_csv: Path, limit: int | None = None) -> list[str]:
    symbols: list[str] = []
    with open(universe_csv) as f:
        next(f, None)
        for line in f:
            sym = line.split(",", 1)[0].strip()
            if sym:
                symbols.append(sym)
    if limit:
        symbols = symbols[:limit]
    return symbols


def _classify_and_persist(
    articles: list[dict],
    classifier: CatalystClassifier,
    db_path: str,
) -> tuple[int, int, dict[str, int]]:
    """Classify each article → insert (or dedupe) into catalyst_events.

    Returns (n_inserted, n_skipped_other_generic, per_category_count).
    """
    inserted = 0
    skipped_other = 0
    by_category: dict[str, int] = {}

    for art in articles:
        symbol = art["symbol"]
        headline = art["headline"] or ""
        ts = art["at"]
        if not headline:
            continue

        category, subcategory, pillar_str = classifier.classify(headline)
        if category == "other" and subcategory == "generic":
            skipped_other += 1
            continue

        pillar = PILLAR_BY_CATEGORY.get(category, "micro")
        horizon = DEFAULT_HORIZON_BY_CATEGORY.get(category, 60)
        headline_hash = hashlib.sha256(f"{symbol}|{headline}".encode()).hexdigest()[:16]

        if not isinstance(ts, datetime):
            try:
                ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except Exception:
                continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        try:
            event = CatalystEvent(
                symbol=symbol,
                category=category,
                subcategory=subcategory,
                pillar=pillar,  # type: ignore[arg-type]
                ts=ts,
                headline=headline[:500],
                source="alpaca_2024_pull",
                horizon_minutes=horizon,
                headline_hash=headline_hash,
            )
        except ValueError:
            # invalid pillar/horizon — should not happen with our maps
            continue

        n = insert_event(db_path, event)
        inserted += n
        if n:
            by_category[f"{category}/{subcategory}"] = (
                by_category.get(f"{category}/{subcategory}", 0) + 1
            )

    return inserted, skipped_other, by_category


def _fetch_news_for_chunk(
    chunk: list[str],
    start: datetime,
    end: datetime,
    api_key: str,
    secret_key: str,
    max_pages: int = 100,
) -> list[dict]:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    client = NewsClient(api_key=api_key, secret_key=secret_key)
    symbols_set = set(chunk)
    out: list[dict] = []
    page_token = None
    pages = 0

    while pages < max_pages:
        req = NewsRequest(
            symbols=",".join(chunk),
            start=start,
            end=end,
            limit=50,
            include_content=False,
            page_token=page_token,
        )
        try:
            result = client.get_news(req)
        except Exception as exc:
            logger.warning("news pull failed for chunk %s: %s", chunk, exc)
            break

        data = getattr(result, "data", None) or {}
        articles = data.get("news") if isinstance(data, dict) else None
        if not articles:
            break

        for a in articles:
            tagged = getattr(a, "symbols", None) or []
            created = getattr(a, "created_at", None) or getattr(a, "updated_at", None)
            if created is None:
                continue
            for sym in tagged:
                if sym in symbols_set:
                    out.append({
                        "symbol": sym,
                        "at": created,
                        "headline": (getattr(a, "headline", "") or "")[:500],
                    })

        pages += 1
        page_token = getattr(result, "next_page_token", None)
        if not page_token:
            break

    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Load 2024 Alpaca News into catalyst_events SQLite.")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default="2024-12-31")
    p.add_argument("--universe", default="config/universe.csv")
    p.add_argument(
        "--output",
        default="data/driftpilot/catalyst_events_2024.sqlite3",
        help="SQLite output path",
    )
    p.add_argument("--chunk-size", type=int, default=5, help="Symbols per Alpaca request (Alpaca recommends ≤10)")
    p.add_argument("--symbol-limit", type=int, default=0, help="Cap universe size (smoke testing)")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--progress-every", type=int, default=50, help="Log every N symbols processed")
    args = p.parse_args()

    api_key, secret_key = _load_alpaca_keys(Path(args.env_file))
    if not api_key or not secret_key:
        logger.error("ALPACA_API_KEY / ALPACA_SECRET_KEY missing in %s", args.env_file)
        sys.exit(2)

    start_d = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_d = datetime.fromisoformat(args.end).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

    symbols = _load_universe(Path(args.universe), args.symbol_limit or None)
    logger.info(
        "PULL CONFIG: %d symbols, %s → %s, chunk=%d, output=%s",
        len(symbols), start_d.date(), end_d.date(), args.chunk_size, args.output,
    )

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    init_catalyst_schema(args.output)
    classifier = CatalystClassifier()

    t_start = time.time()
    total_articles = 0
    total_inserted = 0
    total_skipped = 0
    total_categories: dict[str, int] = {}

    for chunk_start in range(0, len(symbols), args.chunk_size):
        chunk = symbols[chunk_start: chunk_start + args.chunk_size]
        articles = _fetch_news_for_chunk(chunk, start_d, end_d, api_key, secret_key)
        total_articles += len(articles)

        inserted, skipped, by_cat = _classify_and_persist(articles, classifier, args.output)
        total_inserted += inserted
        total_skipped += skipped
        for k, v in by_cat.items():
            total_categories[k] = total_categories.get(k, 0) + v

        symbols_done = min(chunk_start + args.chunk_size, len(symbols))
        if symbols_done % args.progress_every < args.chunk_size or symbols_done == len(symbols):
            elapsed = time.time() - t_start
            rate = symbols_done / elapsed if elapsed > 0 else 0
            eta_s = (len(symbols) - symbols_done) / rate if rate > 0 else 0
            logger.info(
                "progress: %d/%d symbols (%.1f sym/s, eta %.0fs) — %d articles, %d events inserted",
                symbols_done, len(symbols), rate, eta_s, total_articles, total_inserted,
            )

    elapsed = time.time() - t_start
    logger.info("=" * 70)
    logger.info("DONE in %.1fs (%.1f min)", elapsed, elapsed / 60)
    logger.info("articles fetched: %d", total_articles)
    logger.info("events inserted:  %d", total_inserted)
    logger.info("skipped (other/generic): %d", total_skipped)
    logger.info("DB: %s", args.output)
    logger.info("")
    logger.info("category distribution (top 15):")
    for k, v in sorted(total_categories.items(), key=lambda kv: -kv[1])[:15]:
        logger.info("  %-30s %d", k, v)

    # Sanity floor scales with universe size: at least 1 event per 10 symbols
    # over a year. Smaller smoke runs skip this check.
    floor = max(20, len(symbols) // 10)
    if total_inserted < floor and len(symbols) >= 100:
        logger.warning("LOW EVENT COUNT — only %d events for %d symbols. Check API quota.", total_inserted, len(symbols))


if __name__ == "__main__":
    main()
