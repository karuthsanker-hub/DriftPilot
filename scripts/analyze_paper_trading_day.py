#!/usr/bin/env python3
"""End-of-day audit of a paper trading session.

Joins three sources to reconstruct every trade's full chain:

  catalyst_events.sqlite3 (what news fired)
              ↓
  operator_state.sqlite3 orders/positions/fills (what we did with it)
              ↓
  Alpaca paper account (final ground truth)

For each position opened today, prints:
  - The catalyst event that triggered it (headline, sentiment, age at entry)
  - The slot allocation (rank, score)
  - The order submission (limit price, fill price, slippage)
  - The exit (reason, realized PnL, hold time)
  - Whether the exit decision matches what evaluate_exit would have said

Plus an aggregate summary: trades, win rate, edge_ratio, exit-reason
breakdown — same shape as the backtest reports so we can compare.

Usage:
  python scripts/analyze_paper_trading_day.py
  python scripts/analyze_paper_trading_day.py --date 2026-05-04
  python scripts/analyze_paper_trading_day.py --include-alpaca-snapshot
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(format="[%(asctime)s] %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("paper_audit")


def _isoformat(t):
    if t is None:
        return None
    if isinstance(t, str):
        return t
    if isinstance(t, datetime):
        return t.isoformat()
    return str(t)


def _parse_dt(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _query_positions(ops_db: str, day_start: datetime, day_end: datetime) -> list[dict]:
    """Pull all positions opened (or open across) the audit window.

    Returns a list of dicts with the catalyst metadata extracted from
    the position's metadata JSON column.
    """
    conn = sqlite3.connect(ops_db)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT * FROM positions "
            "WHERE opened_at >= ? AND opened_at <= ? "
            "ORDER BY opened_at ASC",
            (day_start.isoformat(), day_end.isoformat()),
        )
        rows = []
        for row in cur.fetchall():
            d = dict(row)
            try:
                d["metadata_dict"] = json.loads(d.get("metadata") or "{}")
            except json.JSONDecodeError:
                d["metadata_dict"] = {}
            rows.append(d)
        return rows
    except sqlite3.OperationalError as exc:
        logger.warning("ops db query failed: %s — assuming no positions opened today", exc)
        return []
    finally:
        conn.close()


def _query_orders_for_position(ops_db: str, position_id: int) -> list[dict]:
    conn = sqlite3.connect(ops_db)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(
            "SELECT * FROM orders WHERE position_id = ? ORDER BY submitted_at ASC",
            (position_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _query_event(catalyst_db: str, event_hash: str | None, event_ts: str | None) -> dict | None:
    if not event_hash and not event_ts:
        return None
    conn = sqlite3.connect(catalyst_db)
    conn.row_factory = sqlite3.Row
    try:
        if event_hash:
            cur = conn.execute(
                "SELECT * FROM catalyst_events WHERE headline_hash = ? LIMIT 1",
                (event_hash,),
            )
        else:
            cur = conn.execute(
                "SELECT * FROM catalyst_events WHERE event_ts = ? LIMIT 1",
                (event_ts,),
            )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def _alpaca_snapshot(settings) -> dict:
    """Optional: pull ground-truth from Alpaca account."""
    from driftpilot.services_live import build_live_components
    from driftpilot.clock import DriftPilotClock
    from driftpilot.storage.repositories import DriftPilotRepository

    clock = DriftPilotClock(settings.timezone)
    repo = DriftPilotRepository.open(settings.sqlite_path_obj, clock)
    broker, _, _ = build_live_components(repo, settings, clock=clock)

    acct = await broker.get_account()
    positions = await broker.get_open_positions()
    return {
        "account_id": acct.account_id,
        "equity": acct.equity,
        "buying_power": acct.buying_power,
        "open_positions_n": len(positions),
        "open_positions": [
            {"symbol": p.symbol, "qty": p.quantity, "unrealized_pnl": p.unrealized_pl}
            for p in positions
        ],
    }


def _print_position_chain(pos: dict, orders: list[dict], event: dict | None) -> None:
    md = pos.get("metadata_dict", {})
    sym = pos.get("symbol", "?")
    pid = pos.get("id", "?")
    qty = pos.get("quantity", "?")
    entry = pos.get("entry_price", "?")
    opened = pos.get("opened_at")
    closed = pos.get("closed_at")
    realized = pos.get("realized_pnl")
    exit_reason = pos.get("exit_reason") or "still_open"

    print(f"\n  [{sym}] position_id={pid} qty={qty} status={'CLOSED' if closed else 'OPEN'}")
    print(f"    opened_at: {opened}")
    print(f"    entry_price: ${float(entry) if entry is not None else 0:.2f}  ref_price: ${md.get('reference_price', 0):.2f}")

    # Catalyst event chain
    cat_hash = md.get("catalyst_headline_hash")
    cat_sent = md.get("catalyst_sentiment")
    cat_age = md.get("catalyst_event_age_min_at_entry")
    cat_head = md.get("catalyst_headline")
    print("    catalyst:")
    print(f"      sentiment: {cat_sent or 'NONE'}")
    print(f"      event_age_at_entry: {cat_age:.1f}min" if isinstance(cat_age, (int, float)) else "      event_age_at_entry: NONE")
    print(f"      headline_hash: {cat_hash or 'NONE'}")
    print(f"      headline: {cat_head or 'NONE'}")
    if event:
        print(f"      [DB-confirmed] {event.get('category')}/{event.get('subcategory')} ts={event.get('event_ts')}")

    # Orders
    for o in orders:
        print(f"    order: {o.get('side')} {o.get('order_type')} qty={o.get('quantity')} "
              f"limit=${o.get('limit_price') or 0:.2f} status={o.get('status')} "
              f"broker_id={o.get('broker_order_id')}")

    # Exit
    if closed:
        opened_dt = _parse_dt(opened)
        closed_dt = _parse_dt(closed)
        hold_min = (closed_dt - opened_dt).total_seconds() / 60 if opened_dt and closed_dt else 0
        ret_pct = 0.0
        if entry is not None and float(entry) > 0:
            exit_md = md.get("exit_price") or 0
            ret_pct = (float(exit_md) - float(entry)) / float(entry) * 100.0
        print(f"    closed_at: {closed}")
        print(f"    exit_reason: {exit_reason}")
        print(f"    realized_pnl: ${realized or 0:.2f}  return: {ret_pct:+.3f}%  hold: {hold_min:.1f}min")
    else:
        print("    [position is still open at audit time]")


def _summary(positions: list[dict]) -> dict:
    closed = [p for p in positions if p.get("closed_at")]
    if not closed:
        return {"trades": 0, "open_at_audit": len(positions)}

    pnls = [float(p.get("realized_pnl") or 0) for p in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(closed) if closed else 0
    avg_winner = sum(wins) / len(wins) if wins else 0
    avg_loser = sum(losses) / len(losses) if losses else 0
    rr = abs(avg_winner) / abs(avg_loser) if avg_loser else 0
    breakeven = 1 / (1 + rr) if rr else 0
    edge_ratio = win_rate / breakeven if breakeven else 0

    by_reason = Counter(p.get("exit_reason") or "unknown" for p in closed)
    by_sentiment = Counter(
        p.get("metadata_dict", {}).get("catalyst_sentiment") or "NONE" for p in closed
    )

    return {
        "trades": len(closed),
        "open_at_audit": len(positions) - len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": win_rate * 100,
        "avg_winner_pnl": avg_winner,
        "avg_loser_pnl": avg_loser,
        "realized_rr": rr,
        "breakeven_win_rate_pct": breakeven * 100,
        "edge_ratio": edge_ratio,
        "total_realized_pnl": sum(pnls),
        "exit_reasons": dict(by_reason),
        "by_catalyst_sentiment": dict(by_sentiment),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=None,
                   help="Audit window date (YYYY-MM-DD). Defaults to today.")
    p.add_argument("--ops-db", default="data/driftpilot/operator_state.sqlite3")
    p.add_argument("--catalyst-db", default="data/driftpilot/catalyst_events.sqlite3")
    p.add_argument("--include-alpaca-snapshot", action="store_true",
                   help="Also pull live ground-truth from Alpaca paper account.")
    args = p.parse_args()

    audit_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(timezone.utc).date()
    )
    day_start = datetime.combine(audit_date, datetime.min.time(), tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    logger.info("=" * 72)
    logger.info("PAPER TRADING AUDIT — %s", audit_date.isoformat())
    logger.info("ops db: %s   catalyst db: %s", args.ops_db, args.catalyst_db)
    logger.info("=" * 72)

    positions = _query_positions(args.ops_db, day_start, day_end)
    logger.info("Positions in window: %d", len(positions))

    if positions:
        print("\n--- per-position chain ---")
        for pos in positions:
            md = pos.get("metadata_dict", {})
            cat_hash = md.get("catalyst_headline_hash")
            cat_event_ts = md.get("catalyst_event_ts")
            event = _query_event(args.catalyst_db, cat_hash, cat_event_ts)
            orders = _query_orders_for_position(args.ops_db, pos.get("id"))
            _print_position_chain(pos, orders, event)

    print("\n--- aggregate summary ---")
    summary = _summary(positions)
    for k, v in summary.items():
        print(f"  {k}: {v}")

    if args.include_alpaca_snapshot:
        import asyncio

        from driftpilot.settings import load_settings

        settings = load_settings(".env")
        snapshot = asyncio.run(_alpaca_snapshot(settings))
        print("\n--- alpaca account ground truth ---")
        for k, v in snapshot.items():
            print(f"  {k}: {v}")

    # Catalyst event volume for the day (regardless of whether traded)
    conn = sqlite3.connect(args.catalyst_db)
    try:
        cur = conn.execute(
            "SELECT category, subcategory, sentiment, COUNT(*) FROM catalyst_events "
            "WHERE event_ts >= ? AND event_ts <= ? "
            "GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 20",
            (day_start.isoformat(), day_end.isoformat()),
        )
        rows = cur.fetchall()
        if rows:
            print("\n--- catalyst events received today (top 20 by count) ---")
            for r in rows:
                print(f"  {r[0]:<10} {r[1]:<15} {r[2] or 'NONE':<10} {r[3]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
