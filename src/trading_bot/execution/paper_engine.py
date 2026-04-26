from __future__ import annotations

from dataclasses import dataclass

from trading_bot.data.repositories import StrategyConfigRepository, TradingRepository
from trading_bot.execution.alpaca_broker import Broker, OrderIntent, OrderResult


@dataclass(frozen=True)
class ExecutionRunSummary:
    attempted: int
    submitted: int
    blocked_reason: str = ""


class PaperExecutionEngine:
    def __init__(
        self,
        trading_repo: TradingRepository,
        config_repo: StrategyConfigRepository,
        broker: Broker,
    ) -> None:
        self.trading_repo = trading_repo
        self.config_repo = config_repo
        self.broker = broker

    def execute_pending_watchlist(self, *, dry_run: bool = True) -> ExecutionRunSummary:
        if not self.config_repo.is_trading_active():
            return ExecutionRunSummary(attempted=0, submitted=0, blocked_reason="kill switch inactive")
        rows = self.trading_repo.list_pending_watchlist()
        submitted = 0
        for row in rows:
            intent = _intent_from_watchlist_row(row)
            result = self.broker.submit_market_order(intent, dry_run=dry_run)
            if result.submitted:
                submitted += 1
                self.trading_repo.mark_watchlist_status(row["id"], "entered")
        return ExecutionRunSummary(attempted=len(rows), submitted=submitted)


def _intent_from_watchlist_row(row: dict) -> OrderIntent:
    strategy = row["strategy"]
    side = "short" if strategy == "PEAD_SHORT" else "buy"
    shares = int(row.get("shares") or 1)
    return OrderIntent(ticker=row["ticker"], side=side, shares=shares, strategy=strategy)

