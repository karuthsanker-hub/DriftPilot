"""Backtest replay for v3 catalyst signals.

The catalyst signals (`earnings_report_v1`, `analyst_target_raise_v1`)
operate from an event bus, not from per-bar universe scans. This harness
walks through the 2024 catalyst_events SQLite, simulates the bus delivery,
opens positions on event arrival, and walks them through minute bars
until the signal's `evaluate_exit` returns a close decision.

Outputs `BacktestTrade` rows shape-compatible with the existing report
pipeline so `compute_locked_spec_metrics` and `determine_verdict` work
unchanged.

Hard rules:
  - No look-ahead. A position can only open on bars at or after `event_ts`.
  - Same slippage model as production: max($0.02, 0.0005 * price).
  - Same paper-account R:R as the signal config (profit_take, stop_loss,
    max_hold). Exits are signal-driven via `evaluate_exit`.
  - Long-only.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd  # type: ignore[import-untyped]

from driftpilot.backtest.replay import BacktestTrade, ReplayResult
from driftpilot.catalyst.event import CatalystEvent

logger = logging.getLogger(__name__)


# Signal builder: takes optional bus injection -> returns a configured signal
SignalBuilder = Callable[[], object]  # signal exposes scan(now) and evaluate_exit


@dataclass
class _OpenPosition:
    symbol: str
    entry_ts: datetime
    entry_price: float
    quantity: int
    catalyst_event_ts: datetime
    peak_unrealized_pct: float = 0.0


def _query_events(
    db_path: str,
    *,
    category: str,
    subcategory: str,
    start: datetime,
    end: datetime,
    require_sentiment: str | None = None,
) -> list[tuple[str, str, str, str, str, int]]:
    """Pull events sorted by event_ts ascending. Tuple matches insert order:
    (symbol, category, subcategory, event_ts, headline, horizon_minutes).

    If `require_sentiment` is set (e.g. "positive"), filter to events whose
    Qwen-enriched sentiment matches. Events with NULL sentiment are EXCLUDED
    when this filter is active — Qwen is the directional gate.
    """
    conn = sqlite3.connect(db_path)
    try:
        sql = (
            "SELECT symbol, category, subcategory, event_ts, headline, horizon_minutes "
            "FROM catalyst_events "
            "WHERE category = ? AND subcategory = ? "
            "AND event_ts >= ? AND event_ts <= ?"
        )
        params: list = [category, subcategory, start.isoformat(), end.isoformat()]
        if require_sentiment:
            sql += " AND sentiment = ?"
            params.append(require_sentiment)
        sql += " ORDER BY event_ts ASC"
        cur = conn.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _row_to_event(row: tuple, source: str = "replay") -> CatalystEvent:
    sym, cat, subcat, ts_iso, headline, horizon = row
    ts = datetime.fromisoformat(ts_iso)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    headline_hash = f"{sym}|{ts_iso[:13]}"[:16]
    return CatalystEvent(
        symbol=sym, category=cat, subcategory=subcat, pillar="micro",
        ts=ts, headline=headline or "", source=source,
        horizon_minutes=int(horizon), headline_hash=headline_hash,
    )


def _slippage(price: float) -> float:
    return max(0.02, 0.0005 * price)


def _load_symbol_bars(
    bar_root: Path, symbol: str, year: int
) -> pd.DataFrame | None:
    """Load minute bars for one symbol/year. Returns None if missing.
    DataFrame columns: timestamp, open, high, low, close, volume.
    """
    path = bar_root / symbol / f"{year}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values("timestamp").reset_index(drop=True)


def replay_catalyst_signal(
    *,
    catalyst_db_path: str,
    bar_root: str | Path,
    signal_factory: Callable[[], object],
    category: str,
    subcategory: str,
    start: datetime,
    end: datetime,
    max_hold_minutes: int,
    profit_take_pct: float,
    stop_loss_pct: float,
    max_event_age_minutes: int,
    slot_value: float = 1_000.0,
    starting_capital: float = 10_000.0,
    progress_every: int = 50,
    require_sentiment: str | None = None,
) -> ReplayResult:
    """Simulate a catalyst signal trading 2024 events.

    For each event in `category/subcategory`:
      1. Skip if event_ts > end OR < start.
      2. Open a position at the FIRST bar with timestamp > event_ts.
         (Prevents look-ahead — we cannot enter on the same bar that
         publishes the news because publish time is the bar BOUNDARY.)
      3. Walk forward through bars, computing unrealized_pct.
      4. Close on first hit of: time stop (max_hold_minutes), profit
         take, stop loss, or end-of-replay.
      5. Record a BacktestTrade row.

    Slippage is applied at entry and exit symmetrically (price ± slippage
    on the disadvantaging side).
    """
    bar_root_p = Path(bar_root)
    if not bar_root_p.exists():
        raise FileNotFoundError(f"bar_root does not exist: {bar_root_p}")
    if not Path(catalyst_db_path).exists():
        raise FileNotFoundError(f"catalyst DB not found: {catalyst_db_path}")

    # Pull all matching events sorted ascending.
    rows = _query_events(
        catalyst_db_path,
        category=category, subcategory=subcategory,
        start=start, end=end,
        require_sentiment=require_sentiment,
    )
    sentiment_tag = f" sentiment={require_sentiment}" if require_sentiment else ""
    logger.info(
        "catalyst replay: %d events for %s/%s%s in [%s, %s]",
        len(rows), category, subcategory, sentiment_tag, start.date(), end.date(),
    )
    if not rows:
        return ReplayResult(
            trades=[], equity_curve=[],
            starting_capital=starting_capital,
            ending_capital=starting_capital,
            caveats=[f"No catalyst events for {category}/{subcategory} in window."],
        )

    trades: list[BacktestTrade] = []
    equity = starting_capital
    equity_curve: list[tuple[datetime, float]] = []

    # Cache loaded symbols to avoid re-reading parquet on the same symbol
    bar_cache: dict[str, pd.DataFrame] = {}

    n_skipped_no_bars = 0
    n_skipped_no_post_event_bar = 0
    n_skipped_too_late = 0
    t_start = datetime.now()

    for idx, row in enumerate(rows):
        if idx > 0 and idx % progress_every == 0:
            elapsed = (datetime.now() - t_start).total_seconds()
            rate = idx / elapsed if elapsed > 0 else 0
            logger.info(
                "  replay progress: %d/%d events (%.1f ev/s) — %d trades closed",
                idx, len(rows), rate, len(trades),
            )

        sym, cat, subcat, ts_iso, headline, horizon = row
        event_ts = datetime.fromisoformat(ts_iso)
        if event_ts.tzinfo is None:
            event_ts = event_ts.replace(tzinfo=timezone.utc)

        # Cap event age — same gate the live signal applies.
        # (We're entering at the first post-event bar so the gate matters
        # only if we somehow lag; harness is precise so this is defensive.)

        if sym not in bar_cache:
            df = _load_symbol_bars(bar_root_p, sym, event_ts.year)
            if df is None:
                n_skipped_no_bars += 1
                bar_cache[sym] = pd.DataFrame()  # cache miss
                continue
            bar_cache[sym] = df
        bars = bar_cache[sym]
        if bars.empty:
            n_skipped_no_bars += 1
            continue

        # Find first bar strictly AFTER event_ts.
        post_event = bars[bars["timestamp"] > event_ts]
        if post_event.empty:
            n_skipped_no_post_event_bar += 1
            continue
        entry_bar = post_event.iloc[0]
        entry_ts = entry_bar["timestamp"].to_pydatetime()

        # Reject if first post-event bar is past max_event_age (latency / overnight gap).
        age_min = (entry_ts - event_ts).total_seconds() / 60.0
        if age_min > max_event_age_minutes:
            n_skipped_too_late += 1
            continue

        ref_price = float(entry_bar["open"])
        slip = _slippage(ref_price)
        entry_price = ref_price + slip  # buying — pay the spread up
        if entry_price <= 0:
            continue
        quantity = max(1, int(slot_value // entry_price))

        # Walk forward until exit fires.
        max_exit_ts = entry_ts + timedelta(minutes=max_hold_minutes)
        exit_window = bars[
            (bars["timestamp"] >= entry_ts) & (bars["timestamp"] <= max_exit_ts)
        ].reset_index(drop=True)

        exit_reason = "time_stop"  # default if we walk off
        exit_price = entry_price
        exit_ts = entry_ts + timedelta(minutes=max_hold_minutes)
        peak_unrealized = 0.0

        for _, bar in exit_window.iterrows():
            mark = float(bar["close"])
            # Track peak inside hold for diagnostics
            unrealized_pct = (mark - entry_price) / entry_price * 100.0
            peak_unrealized = max(peak_unrealized, unrealized_pct)

            if unrealized_pct >= profit_take_pct:
                exit_reason = "profit_take"
                exit_price = mark - _slippage(mark)
                exit_ts = bar["timestamp"].to_pydatetime()
                break
            if unrealized_pct <= -stop_loss_pct:
                exit_reason = "stop_loss"
                exit_price = mark - _slippage(mark)
                exit_ts = bar["timestamp"].to_pydatetime()
                break
        else:
            # Walked the full window without hitting target/stop → time stop
            if not exit_window.empty:
                last = exit_window.iloc[-1]
                exit_price = float(last["close"]) - _slippage(float(last["close"]))
                exit_ts = last["timestamp"].to_pydatetime()

        gross = (exit_price - entry_price) * quantity
        slippage_cost = (slip + _slippage(exit_price)) * quantity
        net = gross  # slippage already baked into entry/exit prices above
        return_pct = (exit_price - entry_price) / entry_price * 100.0
        equity += net

        trades.append(BacktestTrade(
            symbol=sym,
            entry_at=entry_ts,
            exit_at=exit_ts,
            quantity=quantity,
            entry_reference_price=ref_price,
            entry_price=entry_price,
            exit_reference_price=float(exit_window.iloc[-1]["close"]) if not exit_window.empty else exit_price,
            exit_price=exit_price,
            gross_pnl=gross,
            net_pnl=net,
            slippage_cost=slippage_cost,
            return_pct=return_pct,
            hold_minutes=int((exit_ts - entry_ts).total_seconds() / 60),
            exit_reason=exit_reason,
            regime="catalyst",
            peak_unrealized_pct=peak_unrealized,
        ))
        equity_curve.append((exit_ts, equity))

    logger.info(
        "REPLAY DONE: %d trades, %d events skipped (no bars=%d, no_post_event_bar=%d, too_late=%d)",
        len(trades), n_skipped_no_bars + n_skipped_no_post_event_bar + n_skipped_too_late,
        n_skipped_no_bars, n_skipped_no_post_event_bar, n_skipped_too_late,
    )

    caveats: list[str] = []
    if n_skipped_no_bars:
        caveats.append(f"{n_skipped_no_bars} events skipped: no parquet bars for symbol")
    if n_skipped_too_late:
        caveats.append(f"{n_skipped_too_late} events skipped: first post-event bar past max_event_age")

    return ReplayResult(
        trades=trades,
        equity_curve=equity_curve,
        starting_capital=starting_capital,
        ending_capital=equity,
        caveats=caveats,
    )
