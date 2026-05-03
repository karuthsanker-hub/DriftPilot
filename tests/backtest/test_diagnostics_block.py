"""Tests for the diagnostics block in expectancy reports.

Locked Integration Refactor v1.1, Phase 5.2.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from driftpilot.backtest.replay import BacktestTrade, ReplayResult
from driftpilot.backtest.report import build_diagnostics_block, build_expectancy_report
from driftpilot.settings import DriftPilotSettings


def _make_trade(
    *,
    exit_reason: str,
    return_pct: float,
    hold_minutes: int,
    symbol: str = "AAPL",
) -> BacktestTrade:
    entry = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)
    exit_at = datetime(2024, 1, 2, 14, 30 + hold_minutes, tzinfo=UTC)
    return BacktestTrade(
        symbol=symbol,
        entry_at=entry,
        exit_at=exit_at,
        quantity=10,
        entry_reference_price=100.0,
        entry_price=100.0,
        exit_reference_price=100.0 * (1.0 + return_pct),
        exit_price=100.0 * (1.0 + return_pct),
        gross_pnl=10.0 * return_pct * 100.0,
        net_pnl=10.0 * return_pct * 100.0,
        slippage_cost=0.0,
        return_pct=return_pct,
        hold_minutes=hold_minutes,
        exit_reason=exit_reason,
        regime="GREEN",
    )


def _replay_result(trades: list[BacktestTrade]) -> ReplayResult:
    return ReplayResult(
        trades=trades,
        equity_curve=[(datetime(2024, 1, 2, 14, 30, tzinfo=UTC), 10_000.0)],
        starting_capital=10_000.0,
        ending_capital=10_000.0 + sum(trade.net_pnl for trade in trades),
        caveats=[],
    )


def test_diagnostics_block_present_in_every_report() -> None:
    replay = _replay_result(
        [_make_trade(exit_reason="TARGET", return_pct=0.01, hold_minutes=10)]
    )
    settings = DriftPilotSettings()

    report = build_expectancy_report(
        replay,
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        settings=settings,
        point_in_time_constituents=False,
    )

    assert "diagnostics" in report
    diagnostics = report["diagnostics"]
    assert set(diagnostics.keys()) == {
        "performance_by_filter_block",
        "exit_breakdown_detailed",
        "signal_specific",
        "data_dependency_skips",
    }


def test_exit_breakdown_aggregates_by_reason() -> None:
    trades = [
        _make_trade(exit_reason="TARGET", return_pct=0.01, hold_minutes=10),
        _make_trade(exit_reason="TARGET", return_pct=0.02, hold_minutes=20),
        _make_trade(exit_reason="STOP", return_pct=-0.01, hold_minutes=5),
    ]
    diagnostics = build_diagnostics_block(_replay_result(trades), {})

    detailed = diagnostics["exit_breakdown_detailed"]
    assert set(detailed.keys()) == {"TARGET", "STOP"}

    target = detailed["TARGET"]
    assert target["count"] == 2
    assert target["avg_pnl_pct"] == pytest.approx(1.5)
    assert target["avg_hold_mins"] == pytest.approx(15.0)

    stop = detailed["STOP"]
    assert stop["count"] == 1
    assert stop["avg_pnl_pct"] == pytest.approx(-1.0)
    assert stop["avg_hold_mins"] == pytest.approx(5.0)


def test_diagnostics_block_handles_zero_trades() -> None:
    diagnostics = build_diagnostics_block(_replay_result([]), {})
    assert diagnostics["exit_breakdown_detailed"] == {}


def test_signal_specific_block_is_empty_dict_until_signal_wires() -> None:
    diagnostics = build_diagnostics_block(
        _replay_result([_make_trade(exit_reason="TARGET", return_pct=0.01, hold_minutes=10)]),
        {},
    )
    assert diagnostics["signal_specific"] == {}


def test_data_dependency_skips_is_empty_list_until_signal_wires() -> None:
    diagnostics = build_diagnostics_block(
        _replay_result([_make_trade(exit_reason="TARGET", return_pct=0.01, hold_minutes=10)]),
        {},
    )
    assert diagnostics["data_dependency_skips"] == []


def test_performance_by_filter_block_passes_through_counts() -> None:
    diagnostics = build_diagnostics_block(
        _replay_result([]),
        {"low_rvol": 5, "stale_quote": 2},
    )
    assert diagnostics["performance_by_filter_block"] == {"low_rvol": 5, "stale_quote": 2}
