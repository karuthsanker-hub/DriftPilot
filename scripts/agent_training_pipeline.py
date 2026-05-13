#!/usr/bin/env python3
"""Agent training data pipeline — backfill, export, and stats.

Usage:
  # Backfill outcomes from closed positions
  python scripts/agent_training_pipeline.py backfill

  # Export labeled training data as JSONL
  python scripts/agent_training_pipeline.py export --output data/agent_training/labeled.jsonl

  # Show stats
  python scripts/agent_training_pipeline.py stats

  # Export only overrides for review
  python scripts/agent_training_pipeline.py export --overrides-only --output data/agent_training/overrides.jsonl

  # Export with outcomes only (for fine-tuning)
  python scripts/agent_training_pipeline.py export --with-outcome-only --output data/agent_training/finetune.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from driftpilot.agents.training_exporter import (  # noqa: E402
    ExportFilters,
    TrainingExporter,
)

logging.basicConfig(format="[%(asctime)s] %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger("training_pipeline")


def cmd_backfill(args):
    """Backfill outcomes from the operator's positions table."""
    exporter = TrainingExporter(args.agent_db)
    count = exporter.backfill_outcomes(args.operator_db)
    logger.info("backfill complete: %d decisions updated with outcomes", count)


def cmd_export(args):
    """Export training data as JSONL."""
    filters = ExportFilters(
        start_date=args.start_date,
        end_date=args.end_date,
        agent_name=args.agent_name,
        decision_type=args.decision_type,
        overrides_only=args.overrides_only,
        with_outcome_only=args.with_outcome_only,
        symbol=args.symbol,
        limit=args.limit,
    )
    exporter = TrainingExporter(args.agent_db)
    stats = exporter.export_jsonl(args.output, filters)
    logger.info("export stats:")
    logger.info("  total decisions:   %d", stats.total_decisions)
    logger.info("  overrides:         %d (%.1f%%)", stats.overrides, stats.override_rate * 100)
    logger.info("  outcomes filled:   %d", stats.outcomes_filled)
    if stats.accuracy is not None:
        logger.info("  accuracy:          %.1f%%", stats.accuracy * 100)
    logger.info("  avg latency:       %.0fms", stats.avg_latency_ms)
    logger.info("  models used:       %s", json.dumps(stats.models_used))
    logger.info("  output:            %s", args.output)


def cmd_stats(args):
    """Show summary statistics without exporting."""
    filters = ExportFilters(
        start_date=args.start_date,
        end_date=args.end_date,
        agent_name=args.agent_name,
        overrides_only=args.overrides_only,
    )
    exporter = TrainingExporter(args.agent_db)
    stats = exporter.get_stats(filters)
    print(f"Total decisions:    {stats.total_decisions}")
    print(f"Overrides:          {stats.overrides} ({stats.override_rate:.1%})")
    print(f"Outcomes filled:    {stats.outcomes_filled}")
    if stats.accuracy is not None:
        print(f"Accuracy:           {stats.accuracy:.1%}")
    else:
        print("Accuracy:           N/A (no outcomes)")
    print(f"Avg latency:        {stats.avg_latency_ms:.0f}ms")
    print(f"Decision types:     {json.dumps(stats.decision_types)}")
    print(f"Agents:             {json.dumps(stats.agents)}")
    print(f"Models:             {json.dumps(stats.models_used)}")


def main():
    p = argparse.ArgumentParser(description="Agent training data pipeline")
    p.add_argument("--agent-db", default="data/driftpilot/agent_messages.sqlite3")
    p.add_argument("--operator-db", default="data/driftpilot/operator.sqlite3")

    sub = p.add_subparsers(dest="command")

    # backfill
    sub.add_parser("backfill", help="Backfill outcomes from closed positions")

    # export
    exp = sub.add_parser("export", help="Export training data as JSONL")
    exp.add_argument("--output", "-o", default="data/agent_training/labeled.jsonl")
    exp.add_argument("--start-date", help="ISO date (e.g. 2026-05-01)")
    exp.add_argument("--end-date", help="ISO date")
    exp.add_argument("--agent-name", help="Filter by agent (pm, scanner, slot_0)")
    exp.add_argument("--decision-type", help="Filter by type (entry_approval, exit_override)")
    exp.add_argument("--overrides-only", action="store_true")
    exp.add_argument("--with-outcome-only", action="store_true")
    exp.add_argument("--symbol", help="Filter by symbol")
    exp.add_argument("--limit", type=int, default=10000)

    # stats
    st = sub.add_parser("stats", help="Show summary statistics")
    st.add_argument("--start-date", help="ISO date")
    st.add_argument("--end-date", help="ISO date")
    st.add_argument("--agent-name")
    st.add_argument("--overrides-only", action="store_true")

    args = p.parse_args()
    if args.command == "backfill":
        cmd_backfill(args)
    elif args.command == "export":
        cmd_export(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
