"""Categorized catalyst spike — POST-NEWS anchoring at multiple horizons.

Replaces the previous daily-open-to-close measurement which was
structurally broken: a news event at 4:30 PM was being measured against
the day's 9:30-AM-to-4:00-PM range, which could not have been caused
by the news. Symmetrically, a news event at 11 AM had its impact mixed
with 2 hours of pre-news drift.

This script anchors strictly AFTER each news timestamp:

    For each news article at time T:
        bar0 = first cached 1-min bar with timestamp >= T (within 60-min tolerance)
        For each horizon H in {60m, 240m, 1 trading day, 2 trading days}:
            barH = first bar at-or-after T + H
            |return| = |barH.close / bar0.close - 1.0| × 100

Returns are bucketed by (news category, news subcategory, horizon),
and compared against a baseline computed with the SAME methodology on
random non-catalyst minutes (apples-to-apples).

The result tells us, for each category and subcategory:

    - At which horizon does the impact peak?
    - Is the impact > baseline at that horizon? (ratio > 1)
    - How many samples support the conclusion? (N)

This is the proper test of the user's claim that "type of news affects
the stock and the time period may be hours."

Output format mirrors `catalyst_category_spike.py` (top-N ratio sort
per horizon) plus a JSON dump.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd  # type: ignore[import-untyped]
from dotenv import dotenv_values

# Reuse the categorization rules and stock metadata from the daily-granularity
# script so we don't drift between two taxonomies.
from catalyst_category_spike import (
    STOCK_METADATA,
    categorize,
)


_ET = ZoneInfo("America/New_York")

# Horizons (in minutes from the news anchor)
HORIZONS = {
    "60m":   60,
    "240m":  240,        # 4 hours
    "1day":  60 * 24,    # 24 hours wall-clock; bars cross trading-session gap
    "2day":  60 * 48,    # 48 hours
}

# Tolerance: how far from the requested timestamp we'll accept a bar.
# Anchor is tight (within 60 min — most news happens in market hours).
# Target tolerance is generous (24 h) to absorb cross-session gaps.
ANCHOR_GAP_MINUTES = 60
TARGET_GAP_MINUTES = 60 * 24


@dataclass(frozen=True, slots=True)
class CategorizedHorizonEvent:
    symbol: str
    catalyst_at: datetime
    headline: str
    sector: str
    cap_bucket: str
    seasonality: str
    category: str
    subcategory: str
    abs_return_pct: dict[str, float | None]   # horizon_label -> value


@dataclass(frozen=True, slots=True)
class HorizonBaselineSample:
    symbol: str
    sampled_at: datetime
    abs_return_pct: dict[str, float | None]


def main() -> None:
    args = _parse_args()
    if args.symbols_csv:
        # Load a custom universe (e.g. mid-caps from config/universe.csv).
        # Symbols outside STOCK_METADATA get defaults so the script can run
        # without us having to enrich every name.
        df = pd.read_csv(args.symbols_csv)
        sym_col = "symbol" if "symbol" in df.columns else df.columns[0]
        sec_col = "sector" if "sector" in df.columns else None
        symbols = [s.upper() for s in df[sym_col].astype(str).tolist()]
        if args.symbol_filter_etf:
            etf_col = "source_etfs" if "source_etfs" in df.columns else None
            if etf_col:
                mask = df[etf_col].astype(str).str.contains(args.symbol_filter_etf, na=False)
                symbols = [s.upper() for s in df.loc[mask, sym_col].astype(str).tolist()]
        if args.symbol_limit and len(symbols) > args.symbol_limit:
            symbols = symbols[: args.symbol_limit]
        # Build STOCK_METADATA entries for symbols not already known.
        for i, sym in enumerate(symbols):
            if sym in STOCK_METADATA:
                continue
            sector = (
                str(df.iloc[i][sec_col]) if sec_col is not None and i < len(df)
                else "Unknown"
            )
            STOCK_METADATA[sym] = {
                "sector": sector,
                "cap": "mid",   # caller assumed mid-cap; flag if filtering
                "seasonality": "even",
            }
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = list(STOCK_METADATA.keys())
    print(f"[horizon-spike] symbols: {len(symbols)}")
    print(f"[horizon-spike] window: {args.start} to {args.end}")
    print(f"[horizon-spike] horizons: {list(HORIZONS)}")
    print()

    api_key, secret_key = _load_alpaca_keys(args.env_file)
    if not api_key or not secret_key:
        raise SystemExit("ALPACA_API_KEY + ALPACA_SECRET_KEY required (in .env)")

    # 1) Pull news.
    news = _fetch_news(symbols, args.start, args.end, api_key, secret_key)
    print(f"[horizon-spike] pulled {len(news)} news articles")

    # 2) Load bars per symbol once.
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        bars_by_symbol[sym] = _load_bars(sym, args.bar_root, args.start, args.end)

    # 3) For each news event compute post-anchor returns at every horizon.
    events: list[CategorizedHorizonEvent] = []
    for article in news:
        sym = article["symbol"]
        meta = STOCK_METADATA.get(sym)
        if meta is None:
            continue
        bars = bars_by_symbol.get(sym)
        if bars is None or bars.empty:
            continue
        ts = article["at"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        cat, subcat = categorize(article["headline"])
        returns_by_horizon = {
            label: _post_anchor_abs_return_pct(bars, ts, minutes)
            for label, minutes in HORIZONS.items()
        }
        events.append(
            CategorizedHorizonEvent(
                symbol=sym,
                catalyst_at=ts,
                headline=article["headline"],
                sector=meta["sector"],
                cap_bucket=meta["cap"],
                seasonality=meta["seasonality"],
                category=cat,
                subcategory=subcat,
                abs_return_pct=returns_by_horizon,
            )
        )
    print(f"[horizon-spike] {len(events)} categorized events with usable bars")

    # 4) Build baseline from random non-catalyst minutes (same methodology).
    #    Exclude minutes within ±240 min of any catalyst on that symbol so the
    #    baseline doesn't include the post-news window of another article.
    rng = random.Random(args.seed)
    excluded_minutes_by_symbol: dict[str, set[datetime]] = defaultdict(set)
    for e in events:
        # Round to minute resolution and exclude a 4h ring around each catalyst.
        center = e.catalyst_at.replace(second=0, microsecond=0)
        for offset in range(-240, 241, 1):
            excluded_minutes_by_symbol[e.symbol].add(center + timedelta(minutes=offset))

    baseline: list[HorizonBaselineSample] = []
    for sym in symbols:
        bars = bars_by_symbol.get(sym)
        if bars is None or bars.empty:
            continue
        # Pool: bars whose timestamp is strictly outside the catalyst exclusion zone
        # AND has at least 48 h of data after it (so 2day horizon is computable).
        cutoff = bars["timestamp"].max() - pd.Timedelta(minutes=HORIZONS["2day"])
        eligible = bars[bars["timestamp"] <= cutoff]
        if eligible.empty:
            continue
        idxs = list(range(len(eligible)))
        rng.shuffle(idxs)
        excluded = excluded_minutes_by_symbol.get(sym, set())
        kept = 0
        for i in idxs:
            if kept >= args.baseline_samples:
                break
            ts = eligible.iloc[i]["timestamp"].to_pydatetime()
            if ts.replace(second=0, microsecond=0) in excluded:
                continue
            baseline.append(
                HorizonBaselineSample(
                    symbol=sym,
                    sampled_at=ts,
                    abs_return_pct={
                        label: _post_anchor_abs_return_pct(bars, ts, minutes)
                        for label, minutes in HORIZONS.items()
                    },
                )
            )
            kept += 1
    print(f"[horizon-spike] {len(baseline)} baseline samples\n")

    # 5) Aggregate per (category, subcategory, horizon).
    summary: dict[str, list[dict]] = {}
    for horizon_label in HORIZONS:
        baseline_returns = [
            s.abs_return_pct[horizon_label]
            for s in baseline
            if s.abs_return_pct.get(horizon_label) is not None
        ]
        baseline_mean = statistics.fmean(baseline_returns) if baseline_returns else 0.0

        rows = _aggregate(events, horizon_label, baseline_mean)
        summary[horizon_label] = rows
        _print_table(
            title=f"horizon = {horizon_label}",
            rows=rows,
            min_samples=args.min_samples,
            baseline_mean=baseline_mean,
            n_baseline=len(baseline_returns),
        )

    # 6) Write JSON.
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "symbols": symbols,
            "start": args.start.isoformat(),
            "end": args.end.isoformat(),
            "horizons": HORIZONS,
            "n_articles_raw": len(news),
            "n_events": len(events),
            "n_baseline": len(baseline),
            "anchor_gap_minutes": ANCHOR_GAP_MINUTES,
            "target_gap_minutes": TARGET_GAP_MINUTES,
            "baseline_samples_per_symbol": args.baseline_samples,
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "by_horizon": summary,
    }
    output.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n  Full report: {output}")


# ---------------------------------------------------------------------------
# Per-anchor return measurement
# ---------------------------------------------------------------------------

def _post_anchor_abs_return_pct(
    bars: pd.DataFrame,
    anchor: datetime,
    horizon_minutes: int,
) -> float | None:
    """|return| from the first bar at-or-after `anchor` to the first bar
    at-or-after `anchor + horizon_minutes`. Respects cross-session gaps.

    Returns None if the anchor itself can't find a bar within
    ANCHOR_GAP_MINUTES (60), or if the target horizon doesn't have a bar
    within TARGET_GAP_MINUTES (24 h — covers weekends + holidays).
    """
    if bars.empty:
        return None
    anchor_ts = pd.Timestamp(anchor).tz_convert("UTC") if pd.Timestamp(anchor).tz else pd.Timestamp(anchor, tz="UTC")
    after_anchor = bars[bars["timestamp"] >= anchor_ts]
    if after_anchor.empty:
        return None
    anchor_bar = after_anchor.iloc[0]
    anchor_gap = (anchor_bar["timestamp"] - anchor_ts).total_seconds() / 60
    if anchor_gap > ANCHOR_GAP_MINUTES * 24:  # very generous: skip only if no bar within a day
        return None

    target_ts = anchor_bar["timestamp"] + pd.Timedelta(minutes=horizon_minutes)
    after_target = bars[bars["timestamp"] >= target_ts]
    if after_target.empty:
        return None
    target_bar = after_target.iloc[0]
    target_gap = (target_bar["timestamp"] - target_ts).total_seconds() / 60
    if target_gap > TARGET_GAP_MINUTES:
        return None

    base_close = float(anchor_bar["close"])
    target_close = float(target_bar["close"])
    if base_close <= 0:
        return None
    return abs(target_close / base_close - 1.0) * 100.0


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------

def _aggregate(events, horizon_label: str, baseline_mean: float) -> list[dict]:
    buckets: dict[tuple[str, str], list[float]] = defaultdict(list)
    for e in events:
        v = e.abs_return_pct.get(horizon_label)
        if v is None:
            continue
        buckets[(e.category, e.subcategory)].append(v)
    out = []
    for (cat, sub), returns in buckets.items():
        if not returns:
            continue
        n = len(returns)
        mean = statistics.fmean(returns)
        gt1 = sum(1 for r in returns if r >= 1.0) / n
        gt2 = sum(1 for r in returns if r >= 2.0) / n
        gt3 = sum(1 for r in returns if r >= 3.0) / n
        ratio = (mean / baseline_mean) if baseline_mean > 0 else 0.0
        out.append({
            "category": cat,
            "subcategory": sub,
            "n": n,
            "mean_abs_return_pct": round(mean, 4),
            "p_gt_1pct": round(gt1, 4),
            "p_gt_2pct": round(gt2, 4),
            "p_gt_3pct": round(gt3, 4),
            "ratio_mean": round(ratio, 3),
        })
    return out


def _print_table(title: str, rows: list[dict], min_samples: int, baseline_mean: float, n_baseline: int) -> None:
    print(f"{'=' * 100}")
    print(f" {title}")
    print(f"   baseline mean |return| (n={n_baseline}): {baseline_mean:.4f}%   min_samples={min_samples}")
    print('=' * 100)
    rows = [r for r in rows if r["n"] >= min_samples]
    rows.sort(key=lambda r: r["ratio_mean"], reverse=True)
    if not rows:
        print("  (no rows met min_samples)")
        return
    print(f"  {'category':<12} {'subcategory':<22} {'n':>4}  {'mean%':>7}  {'>1%':>5}  {'>2%':>5}  {'>3%':>5}  ratio")
    print("  " + "-" * 80)
    for r in rows:
        print(f"  {r['category']:<12} {r['subcategory']:<22} {r['n']:>4}  {r['mean_abs_return_pct']:>6.3f}%  "
              f"{r['p_gt_1pct']:>5.2f}  {r['p_gt_2pct']:>5.2f}  {r['p_gt_3pct']:>5.2f}  {r['ratio_mean']:>5.2f}x")
    print()


# ---------------------------------------------------------------------------
# Plumbing (mirrors catalyst_hypothesis_spike.py)
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    p.add_argument("--end", type=date.fromisoformat, default=date(2024, 3, 31))
    p.add_argument("--bar-root", default="data/bars/databento")
    p.add_argument("--env-file", type=Path, default=Path(".env"))
    p.add_argument("--baseline-samples", type=int, default=200)
    p.add_argument("--min-samples", type=int, default=3)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output", default="reports/catalyst_horizons.json")
    p.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbol list. Overrides the default mega-cap universe.",
    )
    p.add_argument(
        "--symbols-csv",
        default=None,
        help="Path to a CSV with a 'symbol' column. Use config/universe.csv for the full S&P 1500.",
    )
    p.add_argument(
        "--symbol-filter-etf",
        default=None,
        help="When --symbols-csv is given, only keep rows where source_etfs contains this string (e.g. IJH for mid-caps).",
    )
    p.add_argument(
        "--symbol-limit",
        type=int,
        default=None,
        help="Cap the number of symbols (after filtering). Useful for keeping API calls + runtime bounded.",
    )
    return p.parse_args()


def _load_alpaca_keys(env_file: Path) -> tuple[str, str]:
    api = os.environ.get("ALPACA_API_KEY", "")
    sec = os.environ.get("ALPACA_SECRET_KEY", "")
    if (not api or not sec) and env_file.exists():
        v = dotenv_values(env_file)
        api = api or v.get("ALPACA_API_KEY", "") or ""
        sec = sec or v.get("ALPACA_SECRET_KEY", "") or ""
    return api, sec


def _fetch_news(symbols, start, end, api_key, secret_key):
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
            req = NewsRequest(
                symbols=",".join(chunk),
                start=datetime(start.year, start.month, start.day, tzinfo=UTC),
                end=datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC),
                limit=50,
                include_content=False,
                page_token=page_token,
            )
            try:
                result = client.get_news(req)
            except Exception as exc:
                print(f"[horizon-spike] WARN news pull failed for {chunk}: {exc}")
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
                            "headline": (getattr(a, "headline", "") or "")[:300],
                        })
            pages += 1
            page_token = getattr(result, "next_page_token", None)
            if not page_token:
                break
    return out


def _load_bars(symbol, bar_root, start, end):
    path = Path(bar_root) / symbol / f"{start.year}.parquet"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path, columns=["timestamp", "close"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    dates = df["timestamp"].dt.date
    df = df[(dates >= start) & (dates <= end)].copy()
    return df.sort_values("timestamp").reset_index(drop=True)


if __name__ == "__main__":
    main()
