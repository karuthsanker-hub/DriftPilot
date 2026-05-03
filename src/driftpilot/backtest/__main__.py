from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from driftpilot.backtest.baseline_lookup import find_latest_baseline_report, read_edge_ratio
from driftpilot.backtest.replay import replay_parquet_cache
from driftpilot.backtest.report import build_expectancy_report, default_report_path, write_expectancy_report
from driftpilot.settings import load_settings


# Locked Integration Refactor v1.1 (Phase 5.1): Step-Gate threshold. Sweeps are
# refused when the latest baseline edge_ratio is below this floor unless
# --force-sweep is passed.
EDGE_RATIO_GATE_THRESHOLD: float = 0.8


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m driftpilot.backtest",
        description="Run DriftPilot intraday backtest replay.",
    )
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--bar-root", default=None)
    parser.add_argument("--output", default=None)
    parser.add_argument("--rvol-lookback", type=int, default=20)
    parser.add_argument("--point-in-time-constituents", action="store_true")
    parser.add_argument("--signal", default=None)
    parser.add_argument(
        "--sweep",
        action="store_true",
        help=(
            "Run a parameter sweep instead of a baseline backtest. Requires the "
            "most recent baseline report to have edge_ratio >= 0.8 unless "
            "--force-sweep is also provided."
        ),
    )
    parser.add_argument(
        "--force-sweep",
        action="store_true",
        help="Bypass the Step-Gate edge_ratio check. Only valid with --sweep.",
    )
    parser.add_argument(
        "--reports-root",
        default="reports",
        help="Directory the Step-Gate scans for the latest baseline report.",
    )
    parser.add_argument(
        "--memory-profile",
        action="store_true",
        help=(
            "Enable tracemalloc + wall-clock profiling and emit a "
            "harness_performance block under diagnostics in the report. "
            "Per refactor plan v1.1 § 7."
        ),
    )
    return parser


def _run_sweep_gate(
    *,
    signal_name: str,
    reports_root: Path,
    force_sweep: bool,
) -> int:
    """Evaluate the Step-Gate. Returns the desired CLI exit code."""
    baseline = find_latest_baseline_report(signal_name, reports_root)
    if baseline is None:
        print(
            f"No baseline found for signal '{signal_name}' under {reports_root}. "
            "Sweep aborted; run a baseline backtest first.",
            file=sys.stderr,
        )
        return 2
    edge_ratio = read_edge_ratio(baseline)
    if edge_ratio is None:
        print(
            f"Baseline {baseline} is missing edge_ratio. Sweep aborted.",
            file=sys.stderr,
        )
        return 2
    if edge_ratio < EDGE_RATIO_GATE_THRESHOLD:
        if force_sweep:
            print(
                f"FORCED: edge_ratio={edge_ratio:.3f} is below 0.8 threshold; "
                "overriding gate per --force-sweep."
            )
        else:
            print(
                f"Baseline edge_ratio={edge_ratio:.3f} below 0.8 threshold. Sweep aborted.\n"
                "Run baseline again with --force-sweep to override (not recommended).",
                file=sys.stderr,
            )
            return 2
    print(
        f"Sweep gate passed (edge_ratio={edge_ratio:.3f}). "
        "Sweep execution is not yet implemented."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.force_sweep and not args.sweep:
        parser.error("--force-sweep is only valid when combined with --sweep")

    settings = load_settings()
    signal_name = args.signal or settings.active_signal

    if args.sweep:
        return _run_sweep_gate(
            signal_name=signal_name,
            reports_root=Path(args.reports_root),
            force_sweep=args.force_sweep,
        )

    bar_root = Path(args.bar_root or settings.parquet_bar_root)

    # Phase 6 (Locked Integration Refactor v1.1): optional memory + wall-clock
    # profiling. Enabled by --memory-profile; otherwise the harness_performance
    # block is omitted.
    harness_perf: dict[str, float | int] | None = None
    if args.memory_profile:
        import time
        import tracemalloc

        tracemalloc.start()
        wall_start = time.monotonic()
        replay = replay_parquet_cache(
            bar_root,
            start=args.start,
            end=args.end,
            settings=settings,
            rvol_lookback=args.rvol_lookback,
            point_in_time_constituents=args.point_in_time_constituents,
            signal_name=signal_name,
        )
        wall_elapsed = time.monotonic() - wall_start
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        scan_cycles = len(replay.equity_curve)
        avg_latency_ms = (
            (wall_elapsed * 1000.0) / scan_cycles if scan_cycles > 0 else 0.0
        )
        harness_perf = {
            "peak_memory_mb": round(peak_bytes / (1024 * 1024), 2),
            "wall_clock_seconds": round(wall_elapsed, 2),
            "scan_cycles_executed": scan_cycles,
            "avg_scan_latency_ms": round(avg_latency_ms, 3),
        }
    else:
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
    if harness_perf is not None:
        diagnostics = report.setdefault("diagnostics", {})
        diagnostics["harness_performance"] = harness_perf
    output_path = write_expectancy_report(report, args.output or default_report_path(report))
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
