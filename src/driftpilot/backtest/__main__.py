from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from driftpilot.backtest.replay import replay_parquet_cache
from driftpilot.backtest.report import build_expectancy_report, default_report_path, write_expectancy_report
from driftpilot.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run DriftPilot intraday backtest replay.")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--bar-root", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--rvol-lookback", type=int, default=20)
    parser.add_argument("--point-in-time-constituents", action="store_true")
    parser.add_argument("--signal", default=None)
    args = parser.parse_args()

    settings = load_settings()
    signal_name = args.signal or settings.active_signal
    bar_root = Path(args.bar_root or settings.parquet_bar_root)
    replay = replay_parquet_cache(
        bar_root,
        start=args.start,
        end=args.end,
        settings=settings,
        rvol_lookback=args.rvol_lookback,
        point_in_time_constituents=args.point_in_time_constituents,
        signal_name=signal_name,
    )
    report = build_expectancy_report(
        replay,
        start=args.start,
        end=args.end,
        settings=settings,
        point_in_time_constituents=args.point_in_time_constituents,
        signal_name=signal_name,
    )
    output_path = write_expectancy_report(report, args.output or default_report_path(report))
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
