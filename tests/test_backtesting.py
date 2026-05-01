from __future__ import annotations

from trading_bot.backtesting import BacktestTrade, run_backtest, run_split_backtest


def test_backtest_computes_core_metrics_with_costs() -> None:
    result = run_backtest(
        [
            BacktestTrade(ticker="ABC", side="buy", entry_price=100, exit_price=110, shares=10),
            BacktestTrade(ticker="XYZ", side="short", entry_price=50, exit_price=45, shares=20),
            BacktestTrade(ticker="BAD", side="buy", entry_price=30, exit_price=25, shares=10),
        ],
        starting_equity=10_000,
        transaction_cost_bps=10,
        spy_return_pct=1.0,
    )

    assert result.trade_count == 3
    assert result.win_rate == 66.6667
    assert result.total_pnl == 145.45
    assert result.profit_factor > 3
    assert result.spy_comparison > 0


def test_backtest_handles_empty_trade_set() -> None:
    result = run_backtest([])

    assert result.trade_count == 0
    assert result.win_rate == 0
    assert result.total_pnl == 0
    assert result.sharpe == 0


def test_split_backtest_reports_train_validate_out_of_sample() -> None:
    trades = [
        BacktestTrade(ticker=str(index), side="buy", entry_price=10, exit_price=11, shares=1)
        for index in range(10)
    ]

    result = run_split_backtest(trades, starting_equity=1000, transaction_cost_bps=0)

    assert result.train.trade_count == 6
    assert result.validate.trade_count == 2
    assert result.out_of_sample.trade_count == 2
    assert "survivorship-biased" in result.survivorship_bias_note
