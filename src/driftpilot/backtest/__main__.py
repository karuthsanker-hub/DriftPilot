from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from driftpilot.backtest.replay import load_parquet_bars, replay_bars
from driftpilot.backtest.report import build_expectancy_report, write_expectancy_report
from driftpilot.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run DriftPilot intraday backtest replay.")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--bar-root", default=None)
    parser.add_argument("--output", default="expectancy_report.json")
    parser.add_argument("--rvol-lookback", type=int, default=20)
    parser.add_argument("--point-in-time-constituents", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    bar_root = Path(args.bar_root or settings.parquet_bar_root)
    bars = load_parquet_bars(bar_root, start=args.start, end=args.end)
    replay = replay_bars(
        bars,
        settings=settings,
        rvol_lookback=args.rvol_lookback,
        point_in_time_constituents=args.point_in_time_constituents,
    )
    report = build_expectancy_report(
        replay,
        start=args.start,
        end=args.end,
        settings=settings,
        point_in_time_constituents=args.point_in_time_constituents,
    )
    output_path = write_expectancy_report(report, args.output)
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
