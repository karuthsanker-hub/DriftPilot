from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd  # type: ignore[import-untyped]
import pytest

from driftpilot.backtest.metrics import compute_metrics
from driftpilot.backtest.replay import BacktestTrade, replay_bars
from driftpilot.backtest.report import build_expectancy_report
from driftpilot.execution.paper_fills import slippage_for_price
from driftpilot.settings import DriftPilotSettings


START = datetime(2026, 4, 30, 13, 30, tzinfo=UTC)


def _bars() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for history_day in range(20, 0, -1):
        for minute in range(47):
            timestamp = START - timedelta(days=history_day) + timedelta(minutes=minute)
            for symbol, volume in {"SPY": 1000, "AAA": 100}.items():
                rows.append(
                    {
                        "timestamp": timestamp,
                        "symbol": symbol,
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100,
                        "volume": volume,
                    }
                )
    for minute in range(47):
        timestamp = START + timedelta(minutes=minute)
        spy_close = 100 + (minute * 0.01)
        aaa_close = 100 + (minute * 0.06)
        rows.extend(
            [
                {
                    "timestamp": timestamp,
                    "symbol": "SPY",
                    "open": spy_close - 0.01,
                    "high": spy_close + 0.02,
                    "low": spy_close - 0.02,
                    "close": spy_close,
                    "volume": 1000,
                },
                {
                    "timestamp": timestamp,
                    "symbol": "AAA",
                    "open": aaa_close - 0.02,
                    "high": aaa_close + 0.03,
                    "low": aaa_close - 0.03,
                    "close": aaa_close,
                    "volume": 300 if minute >= 20 else 100,
                },
            ]
        )
    return pd.DataFrame(rows)


def test_backtest_replay_uses_shared_signal_and_slippage_paths() -> None:
    settings = DriftPilotSettings(trade_slots=1, slot_value=1_000, max_hold_minutes=45)

    result = replay_bars(_bars(), settings=settings, rvol_lookback=20)

    assert result.trades
    first = result.trades[0]
    expected_entry_slippage = slippage_for_price(first.entry_reference_price)
    expected_exit_slippage = slippage_for_price(first.exit_reference_price)
    assert first.entry_price == pytest.approx(first.entry_reference_price + expected_entry_slippage)
    assert first.exit_price == pytest.approx(first.exit_reference_price - expected_exit_slippage)
    assert first.exit_reason in {"TARGET", "TIME", "end_of_replay"}
    assert any("survivorship bias" in caveat for caveat in result.caveats)


def test_expectancy_report_contains_live_gate_and_survivorship_bias_note() -> None:
    settings = DriftPilotSettings()
    result = replay_bars(_bars(), settings=settings, rvol_lookback=20)

    report = build_expectancy_report(
        result,
        start=date(2026, 4, 30),
        end=date(2026, 4, 30),
        settings=settings,
        point_in_time_constituents=False,
    )

    assert report["verdict"] in {"GATED", "FAIL"}
    assert "backtest_expectancy_positive" in report["live_gate"]
    assert report["constituents"]["survivorship_bias_note"] is not None
    assert report["slippage_waterfall"]["net_return_pct"] == pytest.approx(
        report["slippage_waterfall"]["gross_return_pct"]
        + report["slippage_waterfall"]["slippage_cost_pct"]
    )


def test_metrics_compute_expectancy_and_exit_breakdown() -> None:
    trade = BacktestTrade(
        symbol="AAA",
        entry_at=START,
        exit_at=START + timedelta(minutes=5),
        quantity=10,
        entry_reference_price=100,
        entry_price=100.05,
        exit_reference_price=101,
        exit_price=100.95,
        gross_pnl=10,
        net_pnl=9,
        slippage_cost=1,
        return_pct=9 / 1000.5,
        hold_minutes=5,
        exit_reason="TARGET",
        regime="GREEN",
    )

    metrics = compute_metrics([trade], starting_capital=10_000)

    assert metrics.total_trades == 1
    assert metrics.expectancy_per_trade == 9
    assert metrics.exit_breakdown == {"TARGET": 1}
    assert metrics.regime_performance["GREEN"]["trades"] == 1
