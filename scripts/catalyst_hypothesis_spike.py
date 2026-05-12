"""Catalyst hypothesis spike — does post-news price movement materially
exceed baseline non-news movement on the same symbols?

The premise behind v3 of the system is that the technical signals we built
in v1/v2 fail because they have no "why". A stock without a catalyst is
just drifting in a sea of other drifting stocks; selecting on a 1.25%
relative-strength threshold (RS-Drift) or a 2.5σ z-score (Stationary
Ghost) just samples noise out of that drift.

This script asks the simplest possible version of the catalyst question:

    Pick N high-volume symbols.
    Pull all Alpaca News articles tagging those symbols across a fixed
    historical window (2024-Q1 by default — three months covers the four
    earnings cycles for most large caps and is cheap to run).
    For each news timestamp T:
       compute |return| at T+30min, T+60min, T+120min
       (using cached Databento 1-min bars)
    For the same symbols, sample M random non-catalyst minutes from the
    same window; compute |return| at each forward horizon as baseline.
    Compare:
       ratio = mean_post_catalyst_return / mean_baseline_return
       Pr(>1% move | catalyst) vs Pr(>1% move | random)

If `ratio >= 2.0` and `Pr(>1% move | catalyst) > 2 * Pr(>1% move | random)`,
the catalyst layer is worth building. If `ratio < 1.5`, post-news
movement is already priced in and the catalyst layer doesn't help.

Cost: pulls Alpaca News (free with our existing data subscription) +
reads cached Databento parquet data we already have. Runs in 5–15 min
on the Mac depending on how many symbols and how many news events.

Run from the repo root:

    PYTHONPATH=src .venv/bin/python3 scripts/catalyst_hypothesis_spike.py

Optional flags:

    --symbols AAPL,MSFT,NVDA,TSLA,...     comma-separated; defaults to
                                          a built-in list of 20 names
    --start 2024-01-01                    inclusive
    --end 2024-03-31                      inclusive
    --bar-root data/bars/databento        parquet cache root
    --baseline-samples 200                random non-catalyst minutes per
                                          symbol for the baseline
    --output reports/catalyst_spike.json  where to write results

The output JSON carries the verdict line at the top:

    {
      "verdict": "ALIVE" | "MARGINAL" | "DEAD",
      "ratio_60m": 2.34,
      "p1pct_catalyst": 0.18,
      "p1pct_baseline": 0.06,
      ...
    }

ALIVE  → ratio >= 2.0 AND p1pct_catalyst >= 2 * p1pct_baseline
DEAD   → ratio < 1.5 OR p1pct_catalyst < 1.2 * p1pct_baseline
MARGINAL → otherwise (build with caution; sample sizes likely too small).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd  # type: ignore[import-untyped]
from dotenv import dotenv_values


DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    "AMD", "NFLX", "CRM", "ORCL", "ADBE", "PLTR", "COIN", "MU",
    "INTC", "DIS", "JPM", "BAC",
]


@dataclass(frozen=True, slots=True)
class CatalystSample:
    symbol: str
    catalyst_at: datetime
    headline: str
    # Forward windows (post-news): what the original v1 spike tested.
    abs_return_30m_pct: float | None
    abs_return_60m_pct: float | None
    abs_return_120m_pct: float | None
    # Backward windows (pre-news): tests the "market reacted 2 days earlier"
    # hypothesis. If pre-news shows abnormal movement vs baseline, the
    # catalyst signal lives in the price action BEFORE the article
    # publishes (smart-money positioning, leaks, options flow).
    abs_return_back_2h_pct: float | None
    abs_return_back_1d_pct: float | None
    abs_return_back_2d_pct: float | None
    abs_return_back_3d_pct: float | None


@dataclass(frozen=True, slots=True)
class BaselineSample:
    symbol: str
    sampled_at: datetime
    abs_return_30m_pct: float | None
    abs_return_60m_pct: float | None
    abs_return_120m_pct: float | None
    abs_return_back_2h_pct: float | None
    abs_return_back_1d_pct: float | None
    abs_return_back_2d_pct: float | None
    abs_return_back_3d_pct: float | None


def main() -> None:
    args = _parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("must provide at least one symbol")

    bar_root = Path(args.bar_root)
    if not bar_root.exists():
        raise SystemExit(f"bar root not found: {bar_root}")

    api_key, secret_key = _load_alpaca_keys(args.env_file)
    if not api_key or not secret_key:
        raise SystemExit(
            "ALPACA_API_KEY + ALPACA_SECRET_KEY required (in .env or environment) to fetch news"
        )

    print(f"[spike] symbols: {len(symbols)} ({', '.join(symbols[:8])}{'...' if len(symbols) > 8 else ''})")
    print(f"[spike] window: {args.start} to {args.end}")
    print(f"[spike] bar_root: {bar_root}")
    print()

    # 1) Pull Alpaca News for the window + symbols.
    news = _fetch_news(symbols, args.start, args.end, api_key, secret_key)
    print(f"[spike] pulled {len(news)} news articles from Alpaca")

    # 2) For each news event, compute |return| at +30 / +60 / +120 min from cached bars.
    catalyst_samples = _build_catalyst_samples(news, bar_root, args.start, args.end)
    print(f"[spike] {len(catalyst_samples)} catalyst samples with usable bars")

    # 3) Build a baseline of random non-catalyst minutes per symbol.
    rng = random.Random(args.seed)
    catalyst_minutes_by_symbol: dict[str, set[datetime]] = {}
    for c in catalyst_samples:
        # Treat any minute within 30 min of a catalyst as "catalyst-tainted" for baseline exclusion.
        for offset in range(-30, 31):
            catalyst_minutes_by_symbol.setdefault(c.symbol, set()).add(
                c.catalyst_at + timedelta(minutes=offset)
            )

    baseline_samples: list[BaselineSample] = []
    for sym in symbols:
        baseline_samples.extend(
            _build_baseline_for_symbol(
                sym,
                bar_root,
                args.start,
                args.end,
                excluded_minutes=catalyst_minutes_by_symbol.get(sym, set()),
                samples_wanted=args.baseline_samples,
                rng=rng,
            )
        )
    print(f"[spike] {len(baseline_samples)} baseline samples")

    # 4) Compute summary stats.
    summary = _summarize(catalyst_samples, baseline_samples)
    summary["meta"] = {
        "symbols": symbols,
        "start": args.start.isoformat(),
        "end": args.end.isoformat(),
        "baseline_samples_per_symbol": args.baseline_samples,
        "n_catalyst_samples": len(catalyst_samples),
        "n_baseline_samples": len(baseline_samples),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    summary["verdict"] = _verdict(summary)

    # 5) Write + print.
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, default=str))

    print()
    print("=" * 72)
    print(f" VERDICT: {summary['verdict']}")
    print("=" * 72)
    print()
    print(" === FORWARD windows (post-news) ===")
    for key in ("ratio_forward_30m", "ratio_forward_60m", "ratio_forward_120m",
                "mean_abs_return_60m_catalyst_pct",
                "mean_abs_return_60m_baseline_pct",
                "p1pct_forward_60m_catalyst", "p1pct_forward_60m_baseline"):
        print(f"   {key:<48} {summary.get(key)}")
    print()
    print(" === BACKWARD windows (pre-news) — 'market moves earlier' test ===")
    for key in ("ratio_back_2h", "ratio_back_1d", "ratio_back_2d", "ratio_back_3d",
                "mean_abs_return_back_1d_catalyst_pct",
                "mean_abs_return_back_1d_baseline_pct",
                "p1pct_back_1d_catalyst", "p1pct_back_1d_baseline",
                "p2pct_back_1d_catalyst", "p2pct_back_1d_baseline"):
        print(f"   {key:<48} {summary.get(key)}")
    print()
    print(f"  Full report: {output_path}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 3, 31))
    parser.add_argument("--bar-root", default="data/bars/databento")
    parser.add_argument("--baseline-samples", type=int, default=200)
    parser.add_argument("--output", default="reports/catalyst_spike.json")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--seed", type=int, default=42)
    return parser.parse_args()


def _load_alpaca_keys(env_file: Path) -> tuple[str, str]:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if (not api_key or not secret_key) and env_file.exists():
        values = dotenv_values(env_file)
        api_key = api_key or values.get("ALPACA_API_KEY", "") or ""
        secret_key = secret_key or values.get("ALPACA_SECRET_KEY", "") or ""
    return api_key, secret_key


def _fetch_news(
    symbols: list[str],
    start: date,
    end: date,
    api_key: str,
    secret_key: str,
) -> list[dict]:
    """Pull news for ``symbols`` between ``start`` and ``end`` (inclusive)
    using the alpaca-py NewsClient. Returns a list of {symbol, at, headline}.
    """
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
    except ImportError as exc:
        raise SystemExit(
            "alpaca-py not installed; run `uv sync --extra test` or pip install alpaca-py"
        ) from exc

    client = NewsClient(api_key=api_key, secret_key=secret_key)

    # Alpaca News: NewsSet.data is {"news": [News, ...]} per symbol-chunk request.
    # Chunk symbols to 5/request; paginate via next_page_token until exhausted.
    out: list[dict] = []
    symbols_set = set(symbols)
    for chunk_start in range(0, len(symbols), 5):
        chunk = symbols[chunk_start: chunk_start + 5]
        page_token: str | None = None
        pages_fetched = 0
        while True:
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
                print(f"[spike] WARN: news pull failed for {chunk}: {exc}")
                break
            data = getattr(result, "data", None) or {}
            articles = data.get("news") if isinstance(data, dict) else None
            if not articles:
                break
            for article in articles:
                tagged = getattr(article, "symbols", None) or []
                created_at = getattr(article, "created_at", None) or getattr(article, "updated_at", None)
                if created_at is None:
                    continue
                # An article is relevant if any of its tagged symbols is in our universe;
                # we attribute the timestamp to that symbol.
                for sym in tagged:
                    if sym in symbols_set:
                        out.append({
                            "symbol": sym,
                            "at": created_at,
                            "headline": (getattr(article, "headline", "") or "")[:200],
                        })
            pages_fetched += 1
            page_token = getattr(result, "next_page_token", None)
            if not page_token or pages_fetched >= 40:
                # Cap pages per chunk so we don't run forever on a long window.
                break
    return out


def _build_catalyst_samples(
    news: list[dict],
    bar_root: Path,
    start: date,
    end: date,
) -> list[CatalystSample]:
    samples: list[CatalystSample] = []
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    for article in news:
        sym = article["symbol"]
        if sym not in bars_by_symbol:
            bars_by_symbol[sym] = _load_bars(sym, bar_root, start, end)
        bars = bars_by_symbol[sym]
        if bars.empty:
            continue
        at = article["at"]
        if isinstance(at, str):
            at = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        sample = CatalystSample(
            symbol=sym,
            catalyst_at=at,
            headline=article.get("headline", ""),
            abs_return_30m_pct=_forward_abs_return_pct(bars, at, 30),
            abs_return_60m_pct=_forward_abs_return_pct(bars, at, 60),
            abs_return_120m_pct=_forward_abs_return_pct(bars, at, 120),
            abs_return_back_2h_pct=_backward_abs_return_pct(bars, at, 120),
            abs_return_back_1d_pct=_backward_abs_return_pct(bars, at, 60 * 24),
            abs_return_back_2d_pct=_backward_abs_return_pct(bars, at, 60 * 48),
            abs_return_back_3d_pct=_backward_abs_return_pct(bars, at, 60 * 72),
        )
        # Keep only samples where at least the 60m return is computable.
        if sample.abs_return_60m_pct is not None:
            samples.append(sample)
    return samples


def _build_baseline_for_symbol(
    symbol: str,
    bar_root: Path,
    start: date,
    end: date,
    excluded_minutes: set[datetime],
    samples_wanted: int,
    rng: random.Random,
) -> list[BaselineSample]:
    bars = _load_bars(symbol, bar_root, start, end)
    if bars.empty:
        return []
    # Eligible: bars whose timestamp is NOT in excluded_minutes (catalyst-tainted)
    # AND that have at least 120 minutes of forward bars in the cache.
    cutoff = bars["timestamp"].max() - pd.Timedelta(minutes=120)
    pool = bars[bars["timestamp"] <= cutoff]
    if pool.empty:
        return []
    picked = []
    indices = list(range(len(pool)))
    rng.shuffle(indices)
    for idx in indices:
        if len(picked) >= samples_wanted:
            break
        ts: datetime = pool.iloc[idx]["timestamp"].to_pydatetime()
        if ts in excluded_minutes:
            continue
        picked.append(
            BaselineSample(
                symbol=symbol,
                sampled_at=ts,
                abs_return_30m_pct=_forward_abs_return_pct(bars, ts, 30),
                abs_return_60m_pct=_forward_abs_return_pct(bars, ts, 60),
                abs_return_120m_pct=_forward_abs_return_pct(bars, ts, 120),
                abs_return_back_2h_pct=_backward_abs_return_pct(bars, ts, 120),
                abs_return_back_1d_pct=_backward_abs_return_pct(bars, ts, 60 * 24),
                abs_return_back_2d_pct=_backward_abs_return_pct(bars, ts, 60 * 48),
                abs_return_back_3d_pct=_backward_abs_return_pct(bars, ts, 60 * 72),
            )
        )
    return picked


def _load_bars(symbol: str, bar_root: Path, start: date, end: date) -> pd.DataFrame:
    path = bar_root / symbol.upper() / f"{start.year}.parquet"
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_parquet(path, columns=["timestamp", "symbol", "close"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    dates = frame["timestamp"].dt.date
    frame = frame[(dates >= start) & (dates <= end)].copy()
    return frame.sort_values("timestamp").reset_index(drop=True)


def _forward_abs_return_pct(bars: pd.DataFrame, anchor: datetime, minutes: int) -> float | None:
    if bars.empty:
        return None
    target = anchor + timedelta(minutes=minutes)
    # Find the bar at-or-just-before `anchor`
    pre = bars[bars["timestamp"] <= pd.Timestamp(anchor)]
    if pre.empty:
        return None
    base_close = float(pre.iloc[-1]["close"])
    # And the bar at-or-just-before `target`
    post = bars[bars["timestamp"] <= pd.Timestamp(target)]
    if post.empty or post.iloc[-1]["timestamp"] == pre.iloc[-1]["timestamp"]:
        return None
    fwd_close = float(post.iloc[-1]["close"])
    if base_close <= 0:
        return None
    return abs(fwd_close / base_close - 1.0) * 100.0


def _backward_abs_return_pct(bars: pd.DataFrame, anchor: datetime, minutes: int) -> float | None:
    """|return| over a backward window ending at ``anchor``.

    Tests the "market reacted before the news" hypothesis: was there
    abnormal price movement in the N minutes BEFORE the article timestamp?
    Returns absolute pct change between the close at (anchor - N min) and
    the close at anchor.
    """
    if bars.empty:
        return None
    earlier = anchor - timedelta(minutes=minutes)
    earlier_bars = bars[bars["timestamp"] <= pd.Timestamp(earlier)]
    if earlier_bars.empty:
        return None
    earlier_close = float(earlier_bars.iloc[-1]["close"])
    anchor_bars = bars[bars["timestamp"] <= pd.Timestamp(anchor)]
    if anchor_bars.empty or anchor_bars.iloc[-1]["timestamp"] == earlier_bars.iloc[-1]["timestamp"]:
        return None
    anchor_close = float(anchor_bars.iloc[-1]["close"])
    if earlier_close <= 0:
        return None
    return abs(anchor_close / earlier_close - 1.0) * 100.0


def _summarize(catalysts: list[CatalystSample], baselines: list[BaselineSample]) -> dict:
    def vec(samples, attr):
        return [getattr(s, attr) for s in samples if getattr(s, attr) is not None]

    def mean(xs):
        return statistics.fmean(xs) if xs else 0.0

    def gt(xs, threshold):
        return (sum(1 for x in xs if x >= threshold) / len(xs)) if xs else 0.0

    cat_30 = vec(catalysts, "abs_return_30m_pct")
    cat_60 = vec(catalysts, "abs_return_60m_pct")
    cat_120 = vec(catalysts, "abs_return_120m_pct")
    base_30 = vec(baselines, "abs_return_30m_pct")
    base_60 = vec(baselines, "abs_return_60m_pct")
    base_120 = vec(baselines, "abs_return_120m_pct")
    # Backward (pre-news) windows
    cat_back_2h = vec(catalysts, "abs_return_back_2h_pct")
    cat_back_1d = vec(catalysts, "abs_return_back_1d_pct")
    cat_back_2d = vec(catalysts, "abs_return_back_2d_pct")
    cat_back_3d = vec(catalysts, "abs_return_back_3d_pct")
    base_back_2h = vec(baselines, "abs_return_back_2h_pct")
    base_back_1d = vec(baselines, "abs_return_back_1d_pct")
    base_back_2d = vec(baselines, "abs_return_back_2d_pct")
    base_back_3d = vec(baselines, "abs_return_back_3d_pct")

    def safe_ratio(num, den):
        return (num / den) if den > 0 else float("inf") if num > 0 else 0.0

    return {
        # FORWARD (post-news) — what v1 of the spike tested
        "mean_abs_return_30m_catalyst_pct": round(mean(cat_30), 4),
        "mean_abs_return_30m_baseline_pct": round(mean(base_30), 4),
        "mean_abs_return_60m_catalyst_pct": round(mean(cat_60), 4),
        "mean_abs_return_60m_baseline_pct": round(mean(base_60), 4),
        "mean_abs_return_120m_catalyst_pct": round(mean(cat_120), 4),
        "mean_abs_return_120m_baseline_pct": round(mean(base_120), 4),
        "ratio_forward_30m": round(safe_ratio(mean(cat_30), mean(base_30)), 3),
        "ratio_forward_60m": round(safe_ratio(mean(cat_60), mean(base_60)), 3),
        "ratio_forward_120m": round(safe_ratio(mean(cat_120), mean(base_120)), 3),
        "p1pct_forward_60m_catalyst": round(gt(cat_60, 1.0), 4),
        "p1pct_forward_60m_baseline": round(gt(base_60, 1.0), 4),
        "p2pct_forward_60m_catalyst": round(gt(cat_60, 2.0), 4),
        "p2pct_forward_60m_baseline": round(gt(base_60, 2.0), 4),
        # BACKWARD (pre-news) — does the market move BEFORE the article?
        # If yes, the catalyst signal lives in pre-event positioning, not
        # in the news article itself.
        "mean_abs_return_back_2h_catalyst_pct": round(mean(cat_back_2h), 4),
        "mean_abs_return_back_2h_baseline_pct": round(mean(base_back_2h), 4),
        "mean_abs_return_back_1d_catalyst_pct": round(mean(cat_back_1d), 4),
        "mean_abs_return_back_1d_baseline_pct": round(mean(base_back_1d), 4),
        "mean_abs_return_back_2d_catalyst_pct": round(mean(cat_back_2d), 4),
        "mean_abs_return_back_2d_baseline_pct": round(mean(base_back_2d), 4),
        "mean_abs_return_back_3d_catalyst_pct": round(mean(cat_back_3d), 4),
        "mean_abs_return_back_3d_baseline_pct": round(mean(base_back_3d), 4),
        "ratio_back_2h": round(safe_ratio(mean(cat_back_2h), mean(base_back_2h)), 3),
        "ratio_back_1d": round(safe_ratio(mean(cat_back_1d), mean(base_back_1d)), 3),
        "ratio_back_2d": round(safe_ratio(mean(cat_back_2d), mean(base_back_2d)), 3),
        "ratio_back_3d": round(safe_ratio(mean(cat_back_3d), mean(base_back_3d)), 3),
        "p1pct_back_1d_catalyst": round(gt(cat_back_1d, 1.0), 4),
        "p1pct_back_1d_baseline": round(gt(base_back_1d, 1.0), 4),
        "p2pct_back_1d_catalyst": round(gt(cat_back_1d, 2.0), 4),
        "p2pct_back_1d_baseline": round(gt(base_back_1d, 2.0), 4),
    }


def _verdict(summary: dict) -> str:
    """Verdict combines forward AND backward windows:

    - ALIVE_FORWARD: post-news movement clearly elevated (the original
      v1 hypothesis).
    - ALIVE_PRE_NEWS: pre-news movement clearly elevated (smart-money
      positioning / leaks visible 1-3 days before).
    - ALIVE_BOTH: both directions show signal.
    - MARGINAL: one direction marginal, neither strong.
    - DEAD: neither direction shows meaningful signal.
    """
    fwd_ratio = summary.get("ratio_forward_60m", 0.0)
    fwd_p1c = summary.get("p1pct_forward_60m_catalyst", 0.0)
    fwd_p1b = summary.get("p1pct_forward_60m_baseline", 0.0)
    fwd_alive = fwd_ratio >= 2.0 and fwd_p1b > 0 and fwd_p1c >= 2.0 * fwd_p1b
    fwd_dead = fwd_ratio < 1.5 or (fwd_p1b > 0 and fwd_p1c < 1.2 * fwd_p1b)

    back_ratios = [
        summary.get("ratio_back_2h", 0.0),
        summary.get("ratio_back_1d", 0.0),
        summary.get("ratio_back_2d", 0.0),
        summary.get("ratio_back_3d", 0.0),
    ]
    back_alive_any = any(r >= 1.5 for r in back_ratios)
    back_alive_strong = any(r >= 2.0 for r in back_ratios)

    if fwd_alive and back_alive_strong:
        return "ALIVE_BOTH"
    if back_alive_strong:
        return "ALIVE_PRE_NEWS"
    if fwd_alive:
        return "ALIVE_FORWARD"
    if back_alive_any or not fwd_dead:
        return "MARGINAL"
    return "DEAD"


if __name__ == "__main__":
    main()
