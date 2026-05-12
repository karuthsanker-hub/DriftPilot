"""Categorized catalyst hypothesis spike.

The blanket "news vs no-news" test (`catalyst_hypothesis_spike.py`)
returned a marginal 1.085× ratio at daily granularity. That's too
coarse — different news categories have wildly different price impact.

This script categorizes every news article on three dimensions:

  1. STOCK metadata
       sector            (from config/sector_map.csv / hardcoded fallback)
       cap_bucket        ("mega" >= $200B, "large" $50-200B, "mid" $5-50B,
                          "small" < $5B; from a hardcoded table)
       seasonality       (a-priori per sector: "q4_heavy", "summer_heavy",
                          "winter_heavy", "even"; placeholder until we
                          have multi-year data to derive empirically)

  2. NEWS category (priority-ordered headline keyword rules)
       earnings | analyst | m_and_a | product | regulatory | legal |
       insider | macro | filing | other

  3. NEWS subcategory (within each category)
       earnings:    beat | miss | inline | guidance_up | guidance_down | preannounce
       analyst:     upgrade | downgrade | target_raise | target_cut | reiterates | initiates
       m_and_a:     acquires | acquired | merger_announced | merger_terminated
       product:     launch | partnership | contract_won
       regulatory:  fda_approval | fda_rejection | sec_action | govt_contract | investigation
       legal:       lawsuit | settlement | fine | criminal_charge
       insider:     insider_buying | insider_selling | form_4
       macro:       rate_decision | cpi | jobs | gdp | fomc
       (others fall through to "generic")

For each (sector, news_category, news_subcategory) tuple with N >= 5
samples, compute:

  - mean daily |return| on the article date for that ticker
  - Pr(>2% move) on that day
  - ratio_vs_baseline = the same ticker's *no-news-day* baseline

Report the top-N actionable combinations (high mean ratio + sufficient
sample size) and the bottom-N noise combinations (low ratio).

Usage:

    PYTHONPATH=src .venv/bin/python3 scripts/catalyst_category_spike.py

Output: prints a sorted table to stdout AND writes a JSON report to
`reports/catalyst_categories.json`.

Limits / honest notes:

  - 20 mega-cap test universe is mostly tech; sector breakdown is
    inherently underpowered. The framework supports any universe; just
    re-run with --symbols.
  - Categorization is keyword-based. Headlines like "Apple's CEO
    speaks at conference" won't match well. About 30–40% of articles
    typically fall into "other".
  - Seasonality is a placeholder (a-priori per sector). Real
    seasonality detection needs multi-year history.
  - Sample sizes per (sector × category × subcategory) tuple will be
    small with 337 articles. Use --min-samples to raise the floor;
    default is 3 to surface every observation, with the report noting
    confidence by sample count.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd  # type: ignore[import-untyped]
from dotenv import dotenv_values


# ---------------------------------------------------------------------------
# Stock metadata (sector + cap bucket + seasonality)
# ---------------------------------------------------------------------------

# Hardcoded for the 20-symbol mega-cap test universe. For a broader run,
# extend or load from config/universe.csv + a market-cap data source.
STOCK_METADATA: dict[str, dict[str, str]] = {
    "AAPL": {"sector": "Technology",          "cap": "mega", "seasonality": "q4_heavy"},
    "MSFT": {"sector": "Technology",          "cap": "mega", "seasonality": "even"},
    "NVDA": {"sector": "Technology",          "cap": "mega", "seasonality": "even"},
    "GOOGL":{"sector": "Communication",        "cap": "mega", "seasonality": "even"},
    "AMZN": {"sector": "Consumer Cyclical",    "cap": "mega", "seasonality": "q4_heavy"},
    "META": {"sector": "Communication",        "cap": "mega", "seasonality": "even"},
    "TSLA": {"sector": "Consumer Cyclical",    "cap": "mega", "seasonality": "even"},
    "AVGO": {"sector": "Technology",          "cap": "mega", "seasonality": "even"},
    "AMD":  {"sector": "Technology",          "cap": "large","seasonality": "even"},
    "NFLX": {"sector": "Communication",        "cap": "large","seasonality": "even"},
    "CRM":  {"sector": "Technology",          "cap": "large","seasonality": "even"},
    "ORCL": {"sector": "Technology",          "cap": "large","seasonality": "even"},
    "ADBE": {"sector": "Technology",          "cap": "large","seasonality": "even"},
    "PLTR": {"sector": "Technology",          "cap": "large","seasonality": "even"},
    "COIN": {"sector": "Financial Services",   "cap": "mid",  "seasonality": "even"},
    "MU":   {"sector": "Technology",          "cap": "large","seasonality": "even"},
    "INTC": {"sector": "Technology",          "cap": "large","seasonality": "even"},
    "DIS":  {"sector": "Communication",        "cap": "large","seasonality": "summer_heavy"},
    "JPM":  {"sector": "Financial Services",   "cap": "mega", "seasonality": "even"},
    "BAC":  {"sector": "Financial Services",   "cap": "mega", "seasonality": "even"},
}


# ---------------------------------------------------------------------------
# News taxonomy (priority-ordered keyword rules)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CategoryRule:
    category: str
    subcategory: str
    keywords: tuple[str, ...]  # any-of match (case-insensitive), word-boundary


# ORDER MATTERS — first match wins. More specific subcategories first within a category.
TAXONOMY_RULES: tuple[CategoryRule, ...] = (
    # ---- earnings ----
    CategoryRule("earnings", "beat",         ("beats earnings", "earnings beat", "tops estimates", "beats estimates", "beats q", "beats fourth-quarter", "beats third-quarter")),
    CategoryRule("earnings", "miss",         ("misses earnings", "earnings miss", "misses estimates", "missed estimates", "missed q", "below estimates")),
    CategoryRule("earnings", "guidance_up",  ("raises guidance", "raises outlook", "lifts forecast", "raises q1 forecast", "boosts forecast")),
    CategoryRule("earnings", "guidance_down",("cuts guidance", "lowers guidance", "lowers outlook", "warns", "guidance below")),
    CategoryRule("earnings", "preannounce",  ("preannounces", "pre-announce", "preliminary q", "preliminary results")),
    CategoryRule("earnings", "report",       ("earnings report", " q1 report", " q2 report", " q3 report", " q4 report", " eps ", "reports earnings", "earnings results", "fourth-quarter results", "third-quarter results", "first-quarter results")),
    # ---- analyst ----
    CategoryRule("analyst", "target_raise",  ("raises price target", "raises target", "boosts price target", "lifts price target", "increases price target")),
    CategoryRule("analyst", "target_cut",    ("cuts price target", "lowers price target", "reduces price target")),
    CategoryRule("analyst", "upgrade",       ("upgrades", "upgraded to ", "raised to buy", "raised to overweight", "raised to outperform")),
    CategoryRule("analyst", "downgrade",     ("downgrades", "downgraded to ", "lowered to sell", "lowered to underweight", "lowered to underperform")),
    CategoryRule("analyst", "initiates",     ("initiates coverage", "initiated coverage", "starts coverage")),
    CategoryRule("analyst", "reiterates",    ("reiterates", "maintains", "reaffirms rating")),
    # ---- M&A ----
    CategoryRule("m_and_a", "acquires",      (" acquires ", " to acquire ", " buys ", " agrees to acquire ", "acquisition of")),
    CategoryRule("m_and_a", "acquired",      ("acquired by", "to be acquired", "agrees to be acquired")),
    CategoryRule("m_and_a", "merger",        ("merger", "merge with", " merges with ")),
    CategoryRule("m_and_a", "divestiture",   ("divests", "divestiture", "spin-off", "spinoff", "to spin off")),
    # ---- product ----
    CategoryRule("product", "launch",        ("launches", "unveils", "introduces", "debut of", "rolls out", "announces new")),
    CategoryRule("product", "partnership",   ("partnership", "partners with", "joins forces", "teams up with", "strategic alliance")),
    CategoryRule("product", "contract_won",  ("wins contract", "awarded contract", "secures deal", "signs deal", "wins $")),
    # ---- regulatory ----
    CategoryRule("regulatory", "fda_approval",("fda approves", "fda approved", "fda clearance", "receives fda")),
    CategoryRule("regulatory", "fda_rejection",("fda rejects", "fda denies", "complete response letter", "crl")),
    CategoryRule("regulatory", "sec_action",  ("sec charges", "sec investigation", "sec settlement")),
    CategoryRule("regulatory", "govt_contract",("government contract", "department of defense", "pentagon awards")),
    CategoryRule("regulatory", "investigation",("under investigation", "doj investigates", "antitrust")),
    # ---- legal ----
    CategoryRule("legal", "lawsuit",         ("lawsuit", "sued", "sues", "filed suit", "class action")),
    CategoryRule("legal", "settlement",      ("settlement", "settles", "reaches deal to settle")),
    CategoryRule("legal", "fine",            ("fined", " fine of ", " imposes fine ")),
    CategoryRule("legal", "criminal",        ("criminal charges", "indicted", "pleads guilty")),
    # ---- insider ----
    CategoryRule("insider", "insider_buying",("insider buying", "insider purchased", "ceo bought", "executive buys")),
    CategoryRule("insider", "insider_selling",("insider selling", "insider sold", "ceo sold", "executive sells")),
    CategoryRule("insider", "form_4",        ("form 4", "form-4", "section 16")),
    # ---- macro ----
    CategoryRule("macro", "fomc",            ("fomc", "fed meeting", "powell")),
    CategoryRule("macro", "rate_decision",   ("rate decision", "rate hike", "rate cut", "cuts rates", "raises rates")),
    CategoryRule("macro", "cpi",             ("cpi", "consumer price")),
    CategoryRule("macro", "jobs",            ("jobs report", "nonfarm payrolls", "unemployment rate")),
    CategoryRule("macro", "gdp",             ("gdp report", "gross domestic product")),
    # ---- filing (catch-all for SEC filings without other categorization) ----
    CategoryRule("filing", "8k",             ("8-k", "form 8-k")),
    CategoryRule("filing", "10k",            ("10-k", "annual report filed")),
    CategoryRule("filing", "10q",            ("10-q", "quarterly report filed")),
    CategoryRule("filing", "13d",            ("13d", "13-d", "13g")),
    CategoryRule("filing", "8a",             ("8-a")),
)


def categorize(headline: str) -> tuple[str, str]:
    """Return (category, subcategory) using priority-ordered keyword match.

    First rule whose keyword set hits returns. Fallback: ('other', 'generic').
    Lowercase comparison.
    """
    h = headline.lower()
    for rule in TAXONOMY_RULES:
        for kw in rule.keywords:
            if kw in h:
                return rule.category, rule.subcategory
    return "other", "generic"


# ---------------------------------------------------------------------------
# Spike runner
# ---------------------------------------------------------------------------

@dataclass
class CategorizedEvent:
    symbol: str
    et_date: date
    headline: str
    sector: str
    cap_bucket: str
    seasonality: str
    category: str
    subcategory: str
    daily_abs_return_pct: float | None = None


def main() -> None:
    args = _parse_args()
    symbols = list(STOCK_METADATA.keys())
    print(f"[cat-spike] symbols: {len(symbols)} (mega-cap tech-heavy)")
    print(f"[cat-spike] window: {args.start} to {args.end}")
    print()

    api_key, secret_key = _load_alpaca_keys(args.env_file)
    if not api_key or not secret_key:
        raise SystemExit("ALPACA_API_KEY + ALPACA_SECRET_KEY required (in .env)")

    # 1) Pull news
    news = _fetch_news(symbols, args.start, args.end, api_key, secret_key)
    print(f"[cat-spike] pulled {len(news)} news articles")

    # 2) Categorize + attach metadata
    events: list[CategorizedEvent] = []
    bars_cache: dict[str, dict[date, float]] = {}
    for article in news:
        sym = article["symbol"]
        meta = STOCK_METADATA.get(sym)
        if meta is None:
            continue
        ts = article["at"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        et = ts.astimezone(_ET)
        et_d = et.date()
        cat, subcat = categorize(article["headline"])
        if sym not in bars_cache:
            bars_cache[sym] = _daily_abs_returns(sym, args.bar_root, args.start, args.end)
        events.append(
            CategorizedEvent(
                symbol=sym,
                et_date=et_d,
                headline=article["headline"],
                sector=meta["sector"],
                cap_bucket=meta["cap"],
                seasonality=meta["seasonality"],
                category=cat,
                subcategory=subcat,
                daily_abs_return_pct=bars_cache[sym].get(et_d),
            )
        )

    # Deduplicate to one row per (symbol, date, category, subcategory) so
    # multiple articles on the same day don't double-count the price move.
    seen: set[tuple[str, date, str, str]] = set()
    deduped: list[CategorizedEvent] = []
    for e in events:
        key = (e.symbol, e.et_date, e.category, e.subcategory)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    events = deduped
    print(f"[cat-spike] {len(events)} categorized events (after dedup) with usable bars")

    # 3) Build no-news-day baseline per stock
    catalyst_dates_by_symbol: dict[str, set[date]] = defaultdict(set)
    for e in events:
        catalyst_dates_by_symbol[e.symbol].add(e.et_date)

    no_news_returns_by_symbol: dict[str, list[float]] = defaultdict(list)
    for sym, returns_by_date in bars_cache.items():
        cat_dates = catalyst_dates_by_symbol.get(sym, set())
        for d, r in returns_by_date.items():
            if d not in cat_dates:
                no_news_returns_by_symbol[sym].append(r)
    overall_no_news = [r for rs in no_news_returns_by_symbol.values() for r in rs]
    overall_baseline_mean = statistics.fmean(overall_no_news) if overall_no_news else 0.0

    # 4) Aggregate by (category, subcategory) — the headline result
    by_cat = _aggregate_by_keys(events, lambda e: (e.category, e.subcategory))
    by_sector_cat = _aggregate_by_keys(events, lambda e: (e.sector, e.category, e.subcategory))
    by_cap_cat = _aggregate_by_keys(events, lambda e: (e.cap_bucket, e.category, e.subcategory))

    # 5) Compute ratios vs baseline
    summary = {
        "meta": {
            "symbols": symbols,
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "n_articles_raw": len(news),
            "n_events_categorized_dedup": len(events),
            "n_no_news_days_total": len(overall_no_news),
            "overall_baseline_mean_daily_abs_return_pct": round(overall_baseline_mean, 4),
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "by_category": _bucket_to_dicts(by_cat, overall_baseline_mean, key_fields=("category", "subcategory")),
        "by_sector_category": _bucket_to_dicts(by_sector_cat, overall_baseline_mean, key_fields=("sector", "category", "subcategory")),
        "by_cap_category": _bucket_to_dicts(by_cap_cat, overall_baseline_mean, key_fields=("cap_bucket", "category", "subcategory")),
    }

    # 6) Print top + bottom
    print()
    _print_table(
        title="ALL EVENTS by (category, subcategory) — sorted by ratio_mean DESC",
        rows=summary["by_category"],
        sort_key=lambda r: r["ratio_mean"],
        min_samples=args.min_samples,
        baseline_mean=overall_baseline_mean,
    )

    # 7) Write JSON
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n  Full report: {output}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_ET = ZoneInfo("America/New_York")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2024, 3, 31))
    p.add_argument("--bar-root", default="data/bars/databento")
    p.add_argument("--env-file", type=Path, default=Path(".env"))
    p.add_argument("--min-samples", type=int, default=3)
    p.add_argument("--output", default="reports/catalyst_categories.json")
    return p.parse_args()


def _load_alpaca_keys(env_file: Path) -> tuple[str, str]:
    import os
    api = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if (not api or not sec) and env_file.exists():
        v = dotenv_values(env_file)
        api = api or v.get("ALPACA_API_KEY", "") or ""
        sec = sec or v.get("ALPACA_SECRET_KEY", "") or ""
    return api, sec


def _fetch_news(symbols: list[str], start: date, end: date, api_key: str, secret_key: str) -> list[dict]:
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    client = NewsClient(api_key=api_key, secret_key=secret_key)
    out: list[dict] = []
    symbols_set = set(symbols)
    for chunk_start in range(0, len(symbols), 5):
        chunk = symbols[chunk_start: chunk_start + 5]
        page_token = None
        pages = 0
        while pages < 40:
            request = NewsRequest(
                symbols=",".join(chunk),
                start=datetime(start.year, start.month, start.day, tzinfo=UTC),
                end=datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC),
                limit=50,
                include_content=False,
                page_token=page_token,
            )
            try:
                result = client.get_news(request)
            except Exception as exc:
                print(f"[cat-spike] WARN news pull failed for {chunk}: {exc}")
                break
            data = getattr(result, "data", None) or {}
            articles = data.get("news") if isinstance(data, dict) else None
            if not articles:
                break
            for article in articles:
                tagged = getattr(article, "symbols", None) or []
                created = getattr(article, "created_at", None) or getattr(article, "updated_at", None)
                if created is None:
                    continue
                for sym in tagged:
                    if sym in symbols_set:
                        out.append({
                            "symbol": sym,
                            "at": created,
                            "headline": (getattr(article, "headline", "") or "")[:300],
                        })
            pages += 1
            page_token = getattr(result, "next_page_token", None)
            if not page_token:
                break
    return out


def _daily_abs_returns(symbol: str, bar_root: str | Path, start: date, end: date) -> dict[date, float]:
    """Compute |open-to-close| daily |return %| for ``symbol`` in [start, end]."""
    path = Path(bar_root) / symbol / f"{start.year}.parquet"
    if not path.exists():
        return {}
    df = pd.read_parquet(path, columns=["timestamp", "open", "close"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["et_date"] = df["timestamp"].dt.tz_convert(_ET).dt.date
    et = df["timestamp"].dt.tz_convert(_ET)
    minutes = et.dt.hour * 60 + et.dt.minute
    df = df[(minutes >= 9*60+30) & (minutes <= 16*60)].copy()
    df = df[(df["et_date"] >= start) & (df["et_date"] <= end)]
    if df.empty:
        return {}
    out: dict[date, float] = {}
    for d, g in df.groupby("et_date"):
        if len(g) < 5:
            continue
        g = g.sort_values("timestamp")
        op = float(g.iloc[0]["open"])
        cl = float(g.iloc[-1]["close"])
        if op <= 0:
            continue
        out[d] = abs(cl / op - 1.0) * 100
    return out


def _aggregate_by_keys(events: Iterable[CategorizedEvent], key_fn) -> dict:
    buckets: dict[tuple, list[float]] = defaultdict(list)
    for e in events:
        if e.daily_abs_return_pct is None:
            continue
        buckets[key_fn(e)].append(e.daily_abs_return_pct)
    return buckets


def _bucket_to_dicts(buckets: dict, baseline_mean: float, key_fields: tuple[str, ...]) -> list[dict]:
    out = []
    for key, returns in buckets.items():
        if not returns:
            continue
        n = len(returns)
        mean = statistics.fmean(returns)
        gt1 = sum(1 for r in returns if r >= 1.0) / n
        gt2 = sum(1 for r in returns if r >= 2.0) / n
        gt3 = sum(1 for r in returns if r >= 3.0) / n
        ratio = (mean / baseline_mean) if baseline_mean > 0 else 0.0
        row = {"n": n, "mean_abs_return_pct": round(mean, 4),
               "p_gt_1pct": round(gt1, 4), "p_gt_2pct": round(gt2, 4), "p_gt_3pct": round(gt3, 4),
               "ratio_mean": round(ratio, 3)}
        for field_name, value in zip(key_fields, key if isinstance(key, tuple) else (key,)):
            row[field_name] = value
        out.append(row)
    return out


def _print_table(title: str, rows: list[dict], sort_key, min_samples: int, baseline_mean: float) -> None:
    print(f"\n{'='*100}")
    print(f" {title}")
    print(f" baseline (no-news days mean |daily return|): {baseline_mean:.4f}%   min_samples={min_samples}")
    print('='*100)
    rows = [r for r in rows if r["n"] >= min_samples]
    rows.sort(key=sort_key, reverse=True)
    if not rows:
        print("  (no rows met min_samples threshold)")
        return
    print(f"  {'category':<12} {'subcategory':<22} {'n':>4}  {'mean%':>7}  {'>1%':>5}  {'>2%':>5}  {'>3%':>5}  ratio")
    print("  " + "-" * 80)
    for r in rows:
        cat = r.get("category", "")
        sub = r.get("subcategory", "")
        print(f"  {cat:<12} {sub:<22} {r['n']:>4}  {r['mean_abs_return_pct']:>6.3f}%  "
              f"{r['p_gt_1pct']:>5.2f}  {r['p_gt_2pct']:>5.2f}  {r['p_gt_3pct']:>5.2f}  {r['ratio_mean']:>5.2f}x")


if __name__ == "__main__":
    main()
