#!/usr/bin/env python3
"""Run a v3 catalyst signal backtest and write a report JSON.

Usage:
  python scripts/run_catalyst_signal_backtest.py \\
      --signal earnings_report_v1 \\
      --start 2024-10-01 --end 2024-11-30 \\
      --catalyst-db data/driftpilot/catalyst_events_2024.sqlite3 \\
      --bar-root data/bars/databento

Writes reports/<signal>/<timestamp>_<verdict>.json compatible with the
existing report pipeline (compute_locked_spec_metrics, determine_verdict).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from driftpilot.backtest.catalyst_replay import replay_catalyst_signal  # noqa: E402

logging.basicConfig(format="[%(asctime)s] %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("catalyst_backtest")


# Signal config registry — must match what's in the signal packages
SIGNAL_CONFIGS: dict[str, dict] = {
    "earnings_report_v1": {
        "category": "earnings",
        "subcategory": "report",
        "max_hold_minutes": 60,
        "profit_take_pct": 1.0,
        "stop_loss_pct": 1.5,
        "max_event_age_minutes": 60,
        "verdict_gate_edge_ratio": 1.5,  # tightened per requirements.md (5.09× cell)
    },
    "analyst_target_raise_v1": {
        "category": "analyst",
        "subcategory": "target_raise",
        "max_hold_minutes": 60,
        "profit_take_pct": 0.8,
        "stop_loss_pct": 1.0,
        "max_event_age_minutes": 60,
        "verdict_gate_edge_ratio": 1.2,  # 1.42× cell
    },
}


def _compute_metrics(trades) -> dict:
    if not trades:
        return {"total_trades": 0}
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    actual_win_rate = len(wins) / len(trades)
    avg_winner = sum(t.return_pct for t in wins) / max(1, len(wins))
    avg_loser = sum(t.return_pct for t in losses) / max(1, len(losses))
    realized_rr = abs(avg_winner) / abs(avg_loser) if avg_loser else 0
    breakeven_win_rate = 1 / (1 + realized_rr) if realized_rr else 0
    edge_ratio = actual_win_rate / breakeven_win_rate if breakeven_win_rate else 0
    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "actual_win_rate": actual_win_rate,
        "breakeven_win_rate": breakeven_win_rate,
        "realized_rr": realized_rr,
        "avg_winner_pct": avg_winner,
        "avg_loser_pct": avg_loser,
        "edge_ratio": edge_ratio,
        "fill_rate_pct": 1.0,  # market entry — always fills
    }


def _determine_verdict(metrics: dict, gate: float) -> tuple[str, str]:
    if metrics.get("total_trades", 0) == 0:
        return "FAIL", "no trades produced"
    edge = metrics.get("edge_ratio", 0.0)
    if edge < 1.1:
        return "FAIL", f"edge_ratio={edge:.3f} below universal 1.1 threshold"
    if edge < gate:
        return "GATED", f"edge_ratio={edge:.3f} between 1.1 and signal-specific gate {gate}"
    return "PASS", ""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--signal", required=True, choices=list(SIGNAL_CONFIGS.keys()))
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--catalyst-db", default="data/driftpilot/catalyst_events_2024.sqlite3")
    p.add_argument("--bar-root", default="data/bars/databento")
    p.add_argument("--starting-capital", type=float, default=10_000.0)
    p.add_argument("--slot-value", type=float, default=1_000.0)
    args = p.parse_args()

    cfg = SIGNAL_CONFIGS[args.signal]
    start_d = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end_d = datetime.fromisoformat(args.end).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

    logger.info("=" * 70)
    logger.info("RUNNING %s on [%s, %s]", args.signal, start_d.date(), end_d.date())
    logger.info("config: %s", cfg)
    logger.info("=" * 70)

    result = replay_catalyst_signal(
        catalyst_db_path=args.catalyst_db,
        bar_root=args.bar_root,
        signal_factory=lambda: None,
        category=cfg["category"], subcategory=cfg["subcategory"],
        start=start_d, end=end_d,
        max_hold_minutes=cfg["max_hold_minutes"],
        profit_take_pct=cfg["profit_take_pct"],
        stop_loss_pct=cfg["stop_loss_pct"],
        max_event_age_minutes=cfg["max_event_age_minutes"],
        slot_value=args.slot_value,
        starting_capital=args.starting_capital,
    )

    metrics = _compute_metrics(result.trades)
    metrics["total_return_pct"] = (result.ending_capital / result.starting_capital - 1) * 100.0
    metrics["starting_capital"] = result.starting_capital
    metrics["ending_capital"] = result.ending_capital

    verdict, fail_reason = _determine_verdict(metrics, cfg["verdict_gate_edge_ratio"])

    exit_breakdown = {}
    if result.trades:
        for reason, group in _group_by(result.trades, lambda t: t.exit_reason).items():
            exit_breakdown[reason] = {
                "count": len(group),
                "avg_pnl_pct": sum(t.return_pct for t in group) / len(group),
                "avg_hold_mins": sum(t.hold_minutes for t in group) / len(group),
            }

    report = {
        "signal": args.signal,
        "verdict": verdict,
        "fail_reason": fail_reason,
        "config": cfg,
        "window": {"start": args.start, "end": args.end},
        "headline_metrics": metrics,
        "diagnostics": {
            "exit_breakdown_detailed": exit_breakdown,
            "caveats": result.caveats,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    out_dir = ROOT / "reports" / args.signal
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{ts}_{verdict.lower()}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    logger.info("=" * 70)
    logger.info("VERDICT: %s   reason: %s", verdict, fail_reason or "—")
    logger.info("edge_ratio: %.3f   win_rate: %.2f%%   breakeven: %.2f%%   trades: %d",
                metrics.get("edge_ratio", 0), metrics.get("actual_win_rate", 0)*100,
                metrics.get("breakeven_win_rate", 0)*100, metrics.get("total_trades", 0))
    logger.info("total_return: %.2f%%   exits: %s", metrics["total_return_pct"], dict(Counter(t.exit_reason for t in result.trades)))
    logger.info("REPORT: %s", out_path)


def _group_by(items, key):
    out: dict = {}
    for x in items:
        out.setdefault(key(x), []).append(x)
    return out


if __name__ == "__main__":
    main()
