from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean, pstdev


@dataclass(frozen=True)
class BacktestTrade:
    ticker: str
    side: str
    entry_price: float
    exit_price: float
    shares: int


@dataclass(frozen=True)
class BacktestResult:
    trade_count: int
    win_rate: float
    total_pnl: float
    sharpe: float
    max_drawdown: float
    profit_factor: float
    spy_comparison: float | None = None


@dataclass(frozen=True)
class BacktestSplitResult:
    train: BacktestResult
    validate: BacktestResult
    out_of_sample: BacktestResult
    survivorship_bias_note: str


def run_backtest(
    trades: list[BacktestTrade],
    *,
    starting_equity: float = 50_000,
    transaction_cost_bps: float = 5,
    spy_return_pct: float | None = None,
) -> BacktestResult:
    equity = starting_equity
    peak = starting_equity
    max_drawdown = 0.0
    pnls: list[float] = []
    returns: list[float] = []

    for trade in trades:
        pnl = _trade_pnl(trade)
        cost = _transaction_cost(trade, transaction_cost_bps)
        net_pnl = pnl - cost
        pnls.append(net_pnl)
        returns.append(net_pnl / equity if equity else 0)
        equity += net_pnl
        peak = max(peak, equity)
        drawdown = (peak - equity) / peak if peak else 0
        max_drawdown = max(max_drawdown, drawdown)

    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
    total_return_pct = ((equity / starting_equity) - 1) * 100 if starting_equity else 0
    return BacktestResult(
        trade_count=len(trades),
        win_rate=_win_rate(pnls),
        total_pnl=round(sum(pnls), 2),
        sharpe=round(_sharpe(returns), 4),
        max_drawdown=round(max_drawdown * 100, 4),
        profit_factor=round(gross_profit / gross_loss, 4) if gross_loss else 0.0,
        spy_comparison=round(total_return_pct - spy_return_pct, 4) if spy_return_pct is not None else None,
    )


def run_split_backtest(
    trades: list[BacktestTrade],
    *,
    starting_equity: float = 50_000,
    transaction_cost_bps: float = 5,
    train_pct: float = 0.60,
    validate_pct: float = 0.20,
) -> BacktestSplitResult:
    if not 0 < train_pct < 1 or not 0 <= validate_pct < 1 or train_pct + validate_pct >= 1:
        raise ValueError("train/validate split percentages must leave an out-of-sample segment")
    train_end = int(len(trades) * train_pct)
    validate_end = train_end + int(len(trades) * validate_pct)
    return BacktestSplitResult(
        train=run_backtest(trades[:train_end], starting_equity=starting_equity, transaction_cost_bps=transaction_cost_bps),
        validate=run_backtest(trades[train_end:validate_end], starting_equity=starting_equity, transaction_cost_bps=transaction_cost_bps),
        out_of_sample=run_backtest(trades[validate_end:], starting_equity=starting_equity, transaction_cost_bps=transaction_cost_bps),
        survivorship_bias_note="Current-universe backtests are survivorship-biased until historical universes are imported.",
    )


def _trade_pnl(trade: BacktestTrade) -> float:
    if trade.side in {"short", "sell_short"}:
        return (trade.entry_price - trade.exit_price) * trade.shares
    return (trade.exit_price - trade.entry_price) * trade.shares


def _transaction_cost(trade: BacktestTrade, transaction_cost_bps: float) -> float:
    notional = (trade.entry_price + trade.exit_price) * trade.shares
    return notional * (transaction_cost_bps / 10_000)


def _win_rate(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    return round(sum(1 for pnl in pnls if pnl > 0) / len(pnls) * 100, 4)


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    volatility = pstdev(returns)
    if volatility == 0:
        return 0.0
    return mean(returns) / volatility * sqrt(252)
