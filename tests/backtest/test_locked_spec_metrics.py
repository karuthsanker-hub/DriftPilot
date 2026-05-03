"""Phase 4 — Locked Integration Refactor v1.1 metrics & verdict tests."""

from __future__ import annotations

from datetime import UTC, datetime

import math

from driftpilot.backtest.metrics import compute_locked_spec_metrics, compute_metrics
from driftpilot.backtest.replay import BacktestTrade
from driftpilot.backtest.report import determine_verdict


def _trade(
    *,
    return_pct: float,
    net_pnl: float,
    peak_unrealized_pct: float = 0.0,
    symbol: str = "AAA",
) -> BacktestTrade:
    entry_at: datetime = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    exit_at: datetime = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
    return BacktestTrade(
        symbol=symbol,
        entry_at=entry_at,
        exit_at=exit_at,
        quantity=10,
        entry_reference_price=100.0,
        entry_price=100.0,
        exit_reference_price=100.0 * (1.0 + return_pct),
        exit_price=100.0 * (1.0 + return_pct),
        gross_pnl=net_pnl,
        net_pnl=net_pnl,
        slippage_cost=0.0,
        return_pct=return_pct,
        hold_minutes=30,
        exit_reason="TARGET" if net_pnl > 0 else "STOP",
        regime="GREEN",
        peak_unrealized_pct=peak_unrealized_pct,
    )


def test_breakeven_win_rate_with_no_losers_is_zero() -> None:
    trades: list[BacktestTrade] = [
        _trade(return_pct=0.01, net_pnl=10.0),
        _trade(return_pct=0.02, net_pnl=20.0),
    ]
    metrics: dict[str, float] = compute_locked_spec_metrics(trades, len(trades))
    assert metrics["realized_rr"] == float("inf")
    assert metrics["breakeven_win_rate"] == 0.0
    assert metrics["edge_ratio"] == 0.0


def test_breakeven_win_rate_with_no_winners() -> None:
    trades: list[BacktestTrade] = [
        _trade(return_pct=-0.01, net_pnl=-10.0),
        _trade(return_pct=-0.02, net_pnl=-20.0),
    ]
    metrics: dict[str, float] = compute_locked_spec_metrics(trades, len(trades))
    assert metrics["actual_win_rate"] == 0.0
    assert metrics["edge_ratio"] == 0.0


def test_realized_rr_two_to_one_yields_breakeven_one_third() -> None:
    trades: list[BacktestTrade] = [
        _trade(return_pct=0.01, net_pnl=10.0),
        _trade(return_pct=0.01, net_pnl=10.0),
        _trade(return_pct=-0.005, net_pnl=-5.0),
        _trade(return_pct=-0.005, net_pnl=-5.0),
    ]
    metrics: dict[str, float] = compute_locked_spec_metrics(trades, len(trades))
    assert math.isclose(metrics["realized_rr"], 2.0, rel_tol=1e-9)
    assert math.isclose(metrics["breakeven_win_rate"], 1.0 / 3.0, rel_tol=1e-9)


def test_edge_ratio_below_1_1_yields_fail_verdict() -> None:
    metrics: dict[str, float] = {"edge_ratio": 1.05, "fill_rate_pct": 1.0}
    verdict, fail_reason = determine_verdict(metrics, "intraday_momentum_v1")
    assert verdict == "FAIL"
    assert "edge_ratio" in fail_reason


def test_edge_ratio_in_gated_band_yields_gated_verdict() -> None:
    metrics: dict[str, float] = {"edge_ratio": 1.15, "fill_rate_pct": 1.0}
    verdict, fail_reason = determine_verdict(metrics, "intraday_momentum_v1")
    assert verdict == "GATED"
    assert fail_reason == ""


def test_edge_ratio_above_1_25_yields_pass_verdict() -> None:
    metrics: dict[str, float] = {"edge_ratio": 1.30, "fill_rate_pct": 1.0}
    verdict, fail_reason = determine_verdict(metrics, "intraday_momentum_v1")
    assert verdict == "PASS"
    assert fail_reason == ""


def test_rs_drift_fill_rate_below_50_pct_overrides_pass() -> None:
    metrics: dict[str, float] = {"edge_ratio": 1.50, "fill_rate_pct": 0.40}
    verdict, fail_reason = determine_verdict(metrics, "rs_drift_v1")
    assert verdict == "FAIL"
    assert "fill_rate" in fail_reason


def test_apex_hunter_give_back_ratio_below_0_4_overrides_pass() -> None:
    metrics: dict[str, float] = {
        "edge_ratio": 1.50,
        "fill_rate_pct": 1.0,
        "give_back_ratio": 0.30,
    }
    verdict, fail_reason = determine_verdict(metrics, "apex_hunter_v2_2")
    assert verdict == "FAIL"
    assert "give_back" in fail_reason


def test_fail_reason_empty_string_on_pass() -> None:
    metrics: dict[str, float] = {"edge_ratio": 1.40, "fill_rate_pct": 1.0}
    _, fail_reason = determine_verdict(metrics, "intraday_momentum_v1")
    assert fail_reason == ""


def test_fail_reason_empty_string_on_gated() -> None:
    metrics: dict[str, float] = {"edge_ratio": 1.20, "fill_rate_pct": 1.0}
    _, fail_reason = determine_verdict(metrics, "intraday_momentum_v1")
    assert fail_reason == ""


def test_empty_trades_does_not_raise() -> None:
    metrics: dict[str, float] = compute_locked_spec_metrics([], 0)
    assert metrics["actual_win_rate"] == 0.0
    assert metrics["breakeven_win_rate"] == 0.0
    assert metrics["edge_ratio"] == 0.0
    assert metrics["give_back_ratio"] == 0.0
    assert metrics["fill_rate_pct"] == 0.0
    assert metrics["realized_avg_winner_pct"] == 0.0
    assert metrics["realized_avg_loser_pct"] == 0.0
    assert metrics["realized_rr"] == 0.0


def test_existing_compute_metrics_still_works() -> None:
    trades: list[BacktestTrade] = [
        _trade(return_pct=0.01, net_pnl=10.0),
        _trade(return_pct=-0.005, net_pnl=-5.0),
    ]
    summary = compute_metrics(trades, starting_capital=10_000.0)
    assert summary.total_trades == 2
    assert math.isclose(summary.total_pnl, 5.0, rel_tol=1e-9)
    assert math.isclose(summary.win_rate, 0.5, rel_tol=1e-9)
