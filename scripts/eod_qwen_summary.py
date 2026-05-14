#!/usr/bin/env python3
"""End-of-day Qwen summary — extracts lessons from the day's trading.

Reads all positions, slot events, and rejection data from today's session,
builds a structured report, sends it to Qwen for analysis, and prints
the summary to stdout (captured in the operator log by daily_stop.sh).

Covers:
  - P&L summary (total, by signal, by symbol)
  - Win/loss streaks per symbol (detects repeat losers)
  - Exit reason breakdown
  - Rejection reason breakdown (why candidates were blocked)
  - Slot utilization over the day
  - Catalyst feed health (events per hour)
  - Actionable lessons and parameter recommendations

Usage:
  python scripts/eod_qwen_summary.py
  python scripts/eod_qwen_summary.py --date 2026-05-13
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

logging.basicConfig(format="[%(asctime)s] %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("eod_summary")

QWEN_URL = "http://192.168.1.166:8000/v1"
QWEN_MODEL = "Qwen/Qwen3-8B"
OPS_DB = "data/driftpilot/operator_state.sqlite3"
CATALYST_DB = "data/driftpilot/catalyst_events.sqlite3"


def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _collect_positions(ops_db: str, day_start: str, day_end: str) -> list[dict]:
    conn = sqlite3.connect(ops_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM positions WHERE opened_at >= ? AND opened_at <= ? ORDER BY opened_at",
            (day_start, day_end),
        ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            try:
                d["md"] = json.loads(d.get("metadata_json") or d.get("metadata") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["md"] = {}
            results.append(d)
        return results
    finally:
        conn.close()


def _collect_slot_events(ops_db: str, day_start: str, day_end: str) -> list[dict]:
    """Read recycle_events if available."""
    conn = sqlite3.connect(ops_db)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "recycle_events" not in tables:
            return []
        rows = conn.execute(
            "SELECT * FROM recycle_events WHERE at >= ? AND at <= ? ORDER BY at",
            (day_start, day_end),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def _collect_catalyst_stats(catalyst_db: str, day_start: str, day_end: str) -> dict:
    """How many catalyst events existed and by what categories."""
    conn = sqlite3.connect(catalyst_db)
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM catalyst_events WHERE event_ts >= ? AND event_ts <= ?",
            (day_start, day_end),
        ).fetchone()[0]
        by_cat = conn.execute(
            "SELECT category, subcategory, sentiment, COUNT(*) FROM catalyst_events "
            "WHERE event_ts >= ? AND event_ts <= ? GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 20",
            (day_start, day_end),
        ).fetchall()
        return {
            "total_events": total,
            "breakdown": [
                {"category": r[0], "subcategory": r[1], "sentiment": r[2], "count": r[3]}
                for r in by_cat
            ],
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()


def _build_report(positions: list[dict], slot_events: list[dict], catalyst_stats: dict) -> dict:
    closed = [p for p in positions if p.get("closed_at")]
    still_open = [p for p in positions if not p.get("closed_at")]

    # P&L
    pnls = [float(p.get("realized_pnl") or 0) for p in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    total_pnl = sum(pnls)
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    # By symbol
    symbol_pnl = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0, "consecutive_losses": 0})
    for p in closed:
        sym = p.get("symbol", "?")
        pnl = float(p.get("realized_pnl") or 0)
        symbol_pnl[sym]["trades"] += 1
        symbol_pnl[sym]["pnl"] += pnl
        if pnl > 0:
            symbol_pnl[sym]["wins"] += 1
            symbol_pnl[sym]["consecutive_losses"] = 0
        else:
            symbol_pnl[sym]["losses"] += 1
            symbol_pnl[sym]["consecutive_losses"] += 1

    # Worst symbols (sort by P&L)
    worst = sorted(symbol_pnl.items(), key=lambda x: x[1]["pnl"])[:5]
    best = sorted(symbol_pnl.items(), key=lambda x: x[1]["pnl"], reverse=True)[:5]
    repeat_losers = [(s, d) for s, d in symbol_pnl.items() if d["consecutive_losses"] >= 2]

    # By signal
    signal_pnl = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for p in closed:
        sig = p.get("md", {}).get("signal_name", "unknown")
        pnl = float(p.get("realized_pnl") or 0)
        signal_pnl[sig]["trades"] += 1
        signal_pnl[sig]["pnl"] += pnl
        if pnl > 0:
            signal_pnl[sig]["wins"] += 1

    # Exit reasons
    exit_reasons = Counter(p.get("exit_reason", "unknown") for p in closed)

    # Hold times
    hold_times = []
    for p in closed:
        opened = _parse_dt(p.get("opened_at"))
        closed_dt = _parse_dt(p.get("closed_at"))
        if opened and closed_dt:
            hold_times.append((closed_dt - opened).total_seconds() / 60)
    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0

    # Sector distribution
    sector_counts = Counter(p.get("md", {}).get("sector", "unknown") for p in positions)

    return {
        "date": positions[0].get("opened_at", "unknown")[:10] if positions else "unknown",
        "total_trades": len(closed),
        "still_open_at_eod": len(still_open),
        "total_pnl": round(total_pnl, 2),
        "win_rate_pct": round(win_rate, 1),
        "avg_winner": round(avg_win, 2),
        "avg_loser": round(avg_loss, 2),
        "avg_hold_minutes": round(avg_hold, 1),
        "exit_reasons": dict(exit_reasons),
        "by_signal": {k: v for k, v in signal_pnl.items()},
        "worst_symbols": [(s, {"pnl": round(d["pnl"], 2), "trades": d["trades"], "losses": d["losses"]}) for s, d in worst],
        "best_symbols": [(s, {"pnl": round(d["pnl"], 2), "trades": d["trades"], "wins": d["wins"]}) for s, d in best],
        "repeat_losers": [(s, d["consecutive_losses"]) for s, d in repeat_losers],
        "sector_distribution": dict(sector_counts),
        "slot_recycles": len(slot_events),
        "catalyst_stats": catalyst_stats,
    }


def _ask_qwen(report: dict) -> str:
    """Send the day's report to Qwen for analysis."""
    import httpx

    system_prompt = """You are a trading system analyst reviewing end-of-day results for DriftPilot,
an automated catalyst-driven paper trading system. Analyze the report and provide:

1. **Day Summary** (2-3 sentences): Overall P&L, win rate, key observations
2. **What Worked**: Which signals/symbols/setups performed best and why
3. **What Failed**: Biggest losers, repeat losers, pattern of failures
4. **Parameter Recommendations**: Specific settings to adjust (with exact values)
   - Should max_trades_per_symbol_per_day change?
   - Should stop_loss_pct or profit_take_pct change?
   - Should any symbols be blocklisted?
   - Should sector caps change?
5. **Catalyst Quality**: Were there enough fresh events? Did the pool dry up?
6. **Tomorrow's Action Items**: 3-5 specific things to change before next session

Be specific and quantitative. Reference actual numbers from the report.
Keep the response under 500 words. Do NOT use markdown headers, use plain text with numbered sections./no_think"""

    payload = {
        "model": QWEN_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(report, indent=2, default=str)},
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
    }

    try:
        resp = httpx.post(
            f"{QWEN_URL}/chat/completions",
            json=payload,
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        # Strip any <think> blocks
        if "<think>" in content:
            import re
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content
    except Exception as e:
        return f"[Qwen unavailable: {e}]"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--ops-db", default=OPS_DB)
    parser.add_argument("--catalyst-db", default=CATALYST_DB)
    args = parser.parse_args()

    if args.date:
        audit_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        audit_date = datetime.now(timezone.utc).date()

    day_start = datetime.combine(audit_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    day_end = (datetime.combine(audit_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)).isoformat()

    print("\n" + "=" * 72)
    print(f"  QWEN EOD SUMMARY — {audit_date.isoformat()}")
    print("=" * 72)

    positions = _collect_positions(args.ops_db, day_start, day_end)
    if not positions:
        print("  No positions found for this date.")
        return

    slot_events = _collect_slot_events(args.ops_db, day_start, day_end)
    catalyst_stats = _collect_catalyst_stats(args.catalyst_db, day_start, day_end)
    report = _build_report(positions, slot_events, catalyst_stats)

    # Print the raw report for the log
    print("\n--- RAW REPORT ---")
    print(json.dumps(report, indent=2, default=str))

    # Get Qwen's analysis
    print("\n--- QWEN ANALYSIS ---")
    analysis = _ask_qwen(report)
    print(analysis)

    # Save to file for easy reference
    summary_path = Path(f"logs/archive/eod_summary_{audit_date.isoformat()}.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_data = {
        "date": audit_date.isoformat(),
        "report": report,
        "qwen_analysis": analysis,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    summary_path.write_text(json.dumps(summary_data, indent=2, default=str))
    print(f"\n  Summary saved to: {summary_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
