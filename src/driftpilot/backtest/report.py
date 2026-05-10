from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping

from driftpilot.backtest.metrics import (
    BacktestMetrics,
    compute_locked_spec_metrics,
    compute_metrics,
)
from driftpilot.backtest.replay import ReplayResult
from driftpilot.settings import DriftPilotSettings
from driftpilot.signals import get_signal


def determine_verdict(
    metrics: dict[str, float],
    signal_name: str,
) -> tuple[str, str]:
    """Locked Integration Refactor v1.1 verdict logic.

    Returns ``(verdict, fail_reason)``. ``fail_reason`` is the empty string
    when verdict is PASS or GATED.
    """
    edge: float = float(metrics.get("edge_ratio", 0.0))
    if edge < 1.1:
        return "FAIL", f"edge_ratio={edge:.3f} below 1.1 threshold"
    if signal_name == "rs_drift_v1":
        fill_rate: float = float(metrics.get("fill_rate_pct", 0.0))
        if fill_rate < 0.50:
            return "FAIL", f"fill_rate_pct={fill_rate:.3f} below 0.50 threshold"
    if signal_name == "apex_hunter_v2_2":
        give_back: float = float(metrics.get("give_back_ratio", 0.0))
        if give_back < 0.40:
            return "FAIL", f"give_back_ratio={give_back:.3f} below 0.40 threshold"
    if 1.10 <= edge < 1.25:
        return "GATED", ""
    return "PASS", ""


def build_expectancy_report(
    replay: ReplayResult,
    *,
    start: date,
    end: date,
    settings: DriftPilotSettings,
    point_in_time_constituents: bool,
    signal_name: str | None = None,
) -> dict[str, Any]:
    metrics = compute_metrics(replay.trades, starting_capital=replay.starting_capital)
    signal = get_signal(signal_name or settings.active_signal)
    survivorship_bias_note = not point_in_time_constituents
    survivorship_bias_text = None
    if not point_in_time_constituents:
        survivorship_bias_text = (
            "Point-in-time index constituents were unavailable; report may include survivorship bias."
        )
    live_gate = {
        "backtest_expectancy_positive": metrics.expectancy_per_dollar > 0,
        "paper_trading_60_days_positive_pnl_sharpe_gt_1": False,
        "equity_floor_buffer": False,
        "live_ok_env": settings.live_ok,
    }
    # Locked Integration Refactor v1.1 (Phase 4): compute the new headline
    # metrics + verdict logic.
    # TODO[phase 3.3 wiring]: signals_attempted is approximated as the trade
    # count until limit_fill.py wires real signal-attempted tracking into the
    # replay loop. fill_rate_pct will read 1.0 for every signal until then.
    locked_spec_metrics: dict[str, float] = compute_locked_spec_metrics(
        replay.trades,
        len(replay.trades),
    )
    headline_metrics: dict[str, Any] = {
        **_metrics_dict(metrics),
        **locked_spec_metrics,
    }
    verdict, fail_reason = determine_verdict(headline_metrics, signal.name)

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "signal": {
            "name": signal.name,
            "version": signal.version,
        },
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "run_config": {
            "bar_source": "databento_parquet",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "point_in_time_constituents": point_in_time_constituents,
            "signal": signal.name,
            "signal_version": signal.version,
        },
        "verdict": verdict,
        "fail_reason": fail_reason,
        "live_gate": live_gate,
        "live_gate_criteria": live_gate,
        "settings": {
            "paper_capital": settings.paper_capital,
            "trade_slots": settings.trade_slots,
            "slot_value": settings.slot_value,
            "target_pct": settings.target_pct,
            "stop_pct": settings.stop_pct,
            "max_hold_minutes": settings.max_hold_minutes,
        },
        "constituents": {
            "point_in_time": point_in_time_constituents,
            "survivorship_bias_note": survivorship_bias_note,
            "survivorship_bias_text": survivorship_bias_text,
        },
        "survivorship_bias_note": survivorship_bias_note,
        "survivorship_bias_text": survivorship_bias_text,
        "metrics": _metrics_dict(metrics),
        "headline_metrics": headline_metrics,
        "slippage_waterfall": {
            "gross_return_pct": metrics.gross_return_pct,
            "slippage_cost_pct": -metrics.slippage_return_pct,
            "net_return_pct": metrics.total_return_pct,
            "gross_pnl": metrics.gross_pnl,
            "slippage_cost": metrics.slippage_cost,
            "net_pnl": metrics.total_pnl,
        },
        "performance_by_regime": metrics.regime_performance,
        "exit_breakdown": metrics.exit_breakdown,
        "monthly_returns": metrics.monthly_returns,
        "drawdown_analysis": {"max_drawdown_pct": metrics.max_drawdown_pct},
        "return_distribution": {"daily_pnl": metrics.daily_pnl},
        "trades": [asdict(trade) for trade in replay.trades],
        "equity_curve": [
            {"timestamp": timestamp.isoformat(), "equity": equity}
            for timestamp, equity in replay.equity_curve
        ],
        "caveats": _dedupe([*replay.caveats, *([survivorship_bias_text] if survivorship_bias_text else [])]),
    }
    # Locked Integration Refactor v1.1 (Phase 5.2): standardized diagnostics
    # block. Additive — old report consumers continue to work.
    report["diagnostics"] = build_diagnostics_block(replay, {})
    return report


def build_diagnostics_block(
    replay_result: ReplayResult,
    blocked_reason_counts: Mapping[str, int],
) -> dict[str, Any]:
    """Return the standardized diagnostics block for an expectancy report.

    Schema (Locked Integration Refactor v1.1 § 6 Task 5.2):

    - ``performance_by_filter_block``: counts of trades blocked per filter
      reason. Currently passes through ``blocked_reason_counts`` as-is; the
      replay harness does not yet thread blocked-reason counts here, so the
      caller hands in ``{}`` until the wiring lands.
    - ``exit_breakdown_detailed``: per-``exit_reason`` count, mean PnL %, and
      mean hold minutes derived from ``replay_result.trades``.
    - ``signal_specific``: reserved for per-signal diagnostics aggregated from
      per-trade ``signal_state``. Empty until the signal layer wires it.
    - ``data_dependency_skips``: reserved for InsufficientDataError plumbing
      (Phase 2.2). Empty list until that wiring lands.
    """
    performance_by_filter_block: dict[str, int] = {
        str(reason): int(count) for reason, count in blocked_reason_counts.items()
    }

    grouped: dict[str, list[Any]] = defaultdict(list)
    for trade in replay_result.trades:
        grouped[trade.exit_reason].append(trade)

    exit_breakdown_detailed: dict[str, dict[str, float | int]] = {}
    for reason, trades in grouped.items():
        count = len(trades)
        avg_pnl_pct = sum(trade.return_pct * 100.0 for trade in trades) / count
        avg_hold_mins = sum(float(trade.hold_minutes) for trade in trades) / count
        exit_breakdown_detailed[reason] = {
            "count": count,
            "avg_pnl_pct": avg_pnl_pct,
            "avg_hold_mins": avg_hold_mins,
        }

    return {
        "performance_by_filter_block": performance_by_filter_block,
        "exit_breakdown_detailed": exit_breakdown_detailed,
        "signal_specific": {},
        "data_dependency_skips": [],
    }


def write_expectancy_report(report: dict[str, Any], path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    return output_path


def default_report_path(report: dict[str, Any], *, generated_at: datetime | None = None) -> Path:
    signal_name = str(report.get("signal", {}).get("name") or "unknown_signal")
    verdict = str(report.get("verdict") or "UNKNOWN").lower()
    timestamp = generated_at or datetime.now(UTC)
    filename = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{verdict}.json"
    return Path("reports") / signal_name / filename


def _metrics_dict(metrics: BacktestMetrics) -> dict[str, Any]:
    return {
        "total_return_pct": metrics.total_return_pct,
        "gross_return_pct": metrics.gross_return_pct,
        "slippage_return_pct": metrics.slippage_return_pct,
        "total_pnl": metrics.total_pnl,
        "gross_pnl": metrics.gross_pnl,
        "slippage_cost": metrics.slippage_cost,
        "total_trades": metrics.total_trades,
        "win_rate": metrics.win_rate,
        "average_hold_minutes": metrics.average_hold_minutes,
        "expectancy_per_trade": metrics.expectancy_per_trade,
        "expectancy_per_dollar": metrics.expectancy_per_dollar,
        "sharpe": metrics.sharpe,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "exit_breakdown": metrics.exit_breakdown,
        "regime_performance": metrics.regime_performance,
        "daily_pnl": metrics.daily_pnl,
        "monthly_returns": metrics.monthly_returns,
    }


def _dedupe(items: list[str | None]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
