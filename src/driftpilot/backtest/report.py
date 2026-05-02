from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from driftpilot.backtest.metrics import BacktestMetrics, compute_metrics
from driftpilot.backtest.replay import ReplayResult
from driftpilot.settings import DriftPilotSettings
from driftpilot.signals import get_signal


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
    verdict = "PASS" if all(live_gate.values()) else "GATED"
    if not live_gate["backtest_expectancy_positive"]:
        verdict = "FAIL"

    return {
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
        "headline_metrics": _metrics_dict(metrics),
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
