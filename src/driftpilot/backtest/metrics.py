from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from math import sqrt
from statistics import fmean
from typing import Sequence

from driftpilot.backtest.replay import BacktestTrade


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_return_pct: float
    gross_return_pct: float
    slippage_return_pct: float
    total_pnl: float
    gross_pnl: float
    slippage_cost: float
    total_trades: int
    win_rate: float
    average_hold_minutes: float
    expectancy_per_trade: float
    expectancy_per_dollar: float
    sharpe: float
    max_drawdown_pct: float
    exit_breakdown: dict[str, int]
    regime_performance: dict[str, dict[str, float]]
    daily_pnl: dict[str, float]
    monthly_returns: dict[str, float]


def compute_metrics(trades: list[BacktestTrade], *, starting_capital: float) -> BacktestMetrics:
    if starting_capital <= 0:
        raise ValueError("starting_capital must be positive")
    total_pnl = sum(trade.net_pnl for trade in trades)
    gross_pnl = sum(trade.gross_pnl for trade in trades)
    slippage_cost = sum(trade.slippage_cost for trade in trades)
    invested = sum(trade.entry_price * trade.quantity for trade in trades)
    winning_trades = [trade for trade in trades if trade.net_pnl > 0]
    daily_pnl = _daily_pnl(trades)

    return BacktestMetrics(
        total_return_pct=total_pnl / starting_capital,
        gross_return_pct=gross_pnl / starting_capital,
        slippage_return_pct=slippage_cost / starting_capital,
        total_pnl=total_pnl,
        gross_pnl=gross_pnl,
        slippage_cost=slippage_cost,
        total_trades=len(trades),
        win_rate=(len(winning_trades) / len(trades)) if trades else 0.0,
        average_hold_minutes=fmean(trade.hold_minutes for trade in trades) if trades else 0.0,
        expectancy_per_trade=(total_pnl / len(trades)) if trades else 0.0,
        expectancy_per_dollar=(total_pnl / invested) if invested else 0.0,
        sharpe=_sharpe(list(daily_pnl.values())),
        max_drawdown_pct=_max_drawdown_pct(list(daily_pnl.values()), starting_capital),
        exit_breakdown=dict(Counter(trade.exit_reason for trade in trades)),
        regime_performance=_regime_performance(trades),
        daily_pnl={day.isoformat(): pnl for day, pnl in sorted(daily_pnl.items())},
        monthly_returns=_monthly_returns(daily_pnl, starting_capital),
    )


def compute_locked_spec_metrics(
    trades: Sequence[BacktestTrade],
    signals_attempted: Sequence[object] | int,
) -> dict[str, float]:
    """Compute the Locked Integration Refactor v1.1 verdict metrics.

    Returns a dict with keys: actual_win_rate, breakeven_win_rate, edge_ratio,
    give_back_ratio, fill_rate_pct, realized_avg_winner_pct,
    realized_avg_loser_pct, realized_rr.

    Edge cases:
    - No trades: every numeric field is 0.0 (does not raise).
    - No losers: realized_rr = +inf -> breakeven_win_rate = 0.0 (per plan
      formula 1/(1+rr)) -> edge_ratio = 0.0 via the breakeven_win_rate>0
      guard.
    - No winners: actual_win_rate = 0.0 -> edge_ratio = 0.0.
    - signals_attempted empty / zero: fill_rate_pct = 0.0.
    """
    if isinstance(signals_attempted, int):
        attempted_count: int = signals_attempted
    else:
        attempted_count = len(signals_attempted)

    total_trades: int = len(trades)
    if total_trades == 0:
        return {
            "actual_win_rate": 0.0,
            "breakeven_win_rate": 0.0,
            "edge_ratio": 0.0,
            "give_back_ratio": 0.0,
            "fill_rate_pct": 0.0,
            "realized_avg_winner_pct": 0.0,
            "realized_avg_loser_pct": 0.0,
            "realized_rr": 0.0,
        }

    winners: list[BacktestTrade] = [trade for trade in trades if trade.net_pnl > 0]
    losers: list[BacktestTrade] = [trade for trade in trades if trade.net_pnl < 0]

    actual_win_rate: float = len(winners) / total_trades

    realized_avg_winner_pct: float = (
        fmean(trade.return_pct for trade in winners) if winners else 0.0
    )
    realized_avg_loser_pct: float = (
        fmean(trade.return_pct for trade in losers) if losers else 0.0
    )

    if losers and winners:
        realized_rr: float = abs(realized_avg_winner_pct) / abs(realized_avg_loser_pct)
    elif winners and not losers:
        realized_rr = float("inf")
    else:
        realized_rr = 0.0

    if realized_rr > 0:
        breakeven_win_rate: float = 1.0 / (1.0 + realized_rr)
    else:
        breakeven_win_rate = 1.0

    if breakeven_win_rate > 0:
        edge_ratio: float = actual_win_rate / breakeven_win_rate
    else:
        edge_ratio = 0.0

    peak_pcts: list[float] = [
        trade.peak_unrealized_pct
        for trade in trades
        if getattr(trade, "peak_unrealized_pct", 0.0) > 0
    ]
    if peak_pcts:
        avg_realized_pct: float = fmean(trade.return_pct for trade in trades)
        avg_peak_unrealized_pct: float = fmean(peak_pcts)
        give_back_ratio: float = (
            avg_realized_pct / avg_peak_unrealized_pct
            if avg_peak_unrealized_pct > 0
            else 0.0
        )
    else:
        give_back_ratio = 0.0

    fill_rate_pct: float = (
        total_trades / attempted_count if attempted_count > 0 else 0.0
    )

    return {
        "actual_win_rate": actual_win_rate,
        "breakeven_win_rate": breakeven_win_rate,
        "edge_ratio": edge_ratio,
        "give_back_ratio": give_back_ratio,
        "fill_rate_pct": fill_rate_pct,
        "realized_avg_winner_pct": realized_avg_winner_pct,
        "realized_avg_loser_pct": realized_avg_loser_pct,
        "realized_rr": realized_rr,
    }


def _daily_pnl(trades: list[BacktestTrade]) -> dict[date, float]:
    pnl_by_day: dict[date, float] = defaultdict(float)
    for trade in trades:
        pnl_by_day[trade.exit_at.date()] += trade.net_pnl
    return dict(pnl_by_day)


def _monthly_returns(daily_pnl: dict[date, float], starting_capital: float) -> dict[str, float]:
    equity = starting_capital
    returns: dict[str, float] = {}
    monthly_pnl: dict[str, float] = defaultdict(float)
    for day, pnl in sorted(daily_pnl.items()):
        month = day.strftime("%Y-%m")
        monthly_pnl[month] += pnl
    for month, pnl in sorted(monthly_pnl.items()):
        returns[month] = pnl / equity if equity else 0.0
        equity += pnl
    return returns


def _sharpe(daily_returns_or_pnl: list[float]) -> float:
    if len(daily_returns_or_pnl) < 2:
        return 0.0
    mean = fmean(daily_returns_or_pnl)
    variance = fmean((item - mean) ** 2 for item in daily_returns_or_pnl)
    standard_deviation = sqrt(variance)
    if standard_deviation == 0:
        return 0.0
    return sqrt(252) * mean / standard_deviation


def _max_drawdown_pct(daily_pnl: list[float], starting_capital: float) -> float:
    equity = starting_capital
    peak = starting_capital
    max_drawdown = 0.0
    for pnl in daily_pnl:
        equity += pnl
        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        max_drawdown = min(max_drawdown, drawdown)
    return max_drawdown


def _regime_performance(trades: list[BacktestTrade]) -> dict[str, dict[str, float]]:
    by_regime: dict[str, list[BacktestTrade]] = defaultdict(list)
    for trade in trades:
        by_regime[trade.regime].append(trade)
    return {
        regime: {
            "trades": float(len(items)),
            "win_rate": sum(1 for item in items if item.net_pnl > 0) / len(items),
            "expectancy_per_trade": sum(item.net_pnl for item in items) / len(items),
            "pnl": sum(item.net_pnl for item in items),
        }
        for regime, items in sorted(by_regime.items())
        if items
    }
