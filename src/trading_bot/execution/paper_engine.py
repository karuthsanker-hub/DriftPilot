from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from trading_bot.data.market_data import MarketDataProvider
from trading_bot.data.repositories import StrategyConfigRepository, TradeRecord, TradingRepository
from trading_bot.execution.alpaca_broker import Broker, OrderIntent, OrderResult


@dataclass(frozen=True)
class ExecutionRunSummary:
    attempted: int
    submitted: int
    blocked_reason: str = ""
    skipped: int = 0


@dataclass(frozen=True)
class PositionExitStatus:
    id: str
    ticker: str
    strategy: str
    side: str
    shares: int
    entry_price: float | None
    current_price: float | None
    target_price: float | None
    stop_loss: float | None
    position_value: float
    unrealized_pnl: float | None
    unrealized_pnl_pct: float | None
    hold_days: int | None
    exit_reason: str | None
    action: str
    status: str
    message: str


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

    def execute_pending_watchlist(
        self,
        *,
        dry_run: bool = True,
        max_total_positions: int = 6,
        max_pead_long_positions: int = 3,
        max_pead_short_positions: int = 2,
        max_momentum_positions: int = 1,
    ) -> ExecutionRunSummary:
        if not self.config_repo.is_trading_active():
            return ExecutionRunSummary(attempted=0, submitted=0, blocked_reason="kill switch inactive")
        rows = self.trading_repo.list_pending_watchlist()
        open_rows = self.trading_repo.list_entered_watchlist()
        caps = {
            "PEAD_LONG": max_pead_long_positions,
            "PEAD_SHORT": max_pead_short_positions,
            "MOMENTUM": max_momentum_positions,
        }
        counts = _strategy_counts(open_rows)
        open_tickers = {str(row.get("ticker", "")).upper() for row in open_rows if row.get("ticker")}
        submitted = 0
        skipped = 0
        for row in rows:
            ticker = str(row.get("ticker", "")).upper()
            if ticker in open_tickers:
                skipped += 1
                continue
            strategy = row["strategy"]
            if sum(counts.values()) >= max_total_positions or counts.get(strategy, 0) >= caps.get(strategy, 0):
                skipped += 1
                if not dry_run:
                    self.trading_repo.mark_watchlist_status(row["id"], "skipped")
                continue
            intent = _intent_from_watchlist_row(row)
            result = self.broker.submit_market_order(intent, dry_run=dry_run)
            if result.submitted:
                submitted += 1
                counts[strategy] = counts.get(strategy, 0) + 1
                open_tickers.add(ticker)
                self.trading_repo.mark_watchlist_status(row["id"], "entered")
        blocked_reason = "position limit reached" if skipped and submitted == 0 else ""
        return ExecutionRunSummary(attempted=len(rows), submitted=submitted, blocked_reason=blocked_reason, skipped=skipped)

    def manage_open_positions(
        self,
        market_data: MarketDataProvider,
        *,
        dry_run: bool = True,
        max_hold_days: int = 20,
    ) -> ExecutionRunSummary:
        if not self.config_repo.is_trading_active():
            return ExecutionRunSummary(attempted=0, submitted=0, blocked_reason="kill switch inactive")

        rows = self.trading_repo.list_entered_watchlist()
        attempted = 0
        submitted = 0
        for row in rows:
            status = _position_exit_status(row, _safe_current_price(market_data, row["ticker"]), max_hold_days=max_hold_days)
            if status.exit_reason is None:
                continue

            attempted += 1
            intent = _exit_intent_from_watchlist_row(row)
            result = self.broker.submit_market_order(intent, dry_run=dry_run)
            if result.submitted:
                submitted += 1
                self.trading_repo.insert_trade(_exit_trade_from_row(row, status.exit_reason, current_price=status.current_price))
                self.trading_repo.mark_watchlist_status(row["id"], "exited")
        return ExecutionRunSummary(attempted=attempted, submitted=submitted)

    def open_position_statuses(
        self,
        market_data: MarketDataProvider,
        *,
        max_hold_days: int = 20,
    ) -> list[PositionExitStatus]:
        return [
            _position_exit_status(row, _safe_current_price(market_data, row["ticker"]), max_hold_days=max_hold_days)
            for row in self.trading_repo.list_entered_watchlist()
        ]


def _intent_from_watchlist_row(row: dict) -> OrderIntent:
    strategy = row["strategy"]
    side = "short" if strategy == "PEAD_SHORT" else "buy"
    shares = int(row.get("shares") or 1)
    return OrderIntent(ticker=row["ticker"], side=side, shares=shares, strategy=strategy)


def _strategy_counts(rows: list[dict]) -> dict[str, int]:
    counts = {"PEAD_LONG": 0, "PEAD_SHORT": 0, "MOMENTUM": 0}
    for row in rows:
        strategy = row.get("strategy")
        if strategy in counts:
            counts[strategy] += 1
    return counts


def _exit_intent_from_watchlist_row(row: dict) -> OrderIntent:
    strategy = row["strategy"]
    side = "cover" if strategy == "PEAD_SHORT" else "sell"
    shares = int(row.get("shares") or 1)
    return OrderIntent(ticker=row["ticker"], side=side, shares=shares, strategy=strategy)


def _exit_reason(row: dict, current_price: float, *, max_hold_days: int) -> str | None:
    strategy = row["strategy"]
    target_price = _float_or_none(row.get("target_price"))
    stop_loss = _float_or_none(row.get("stop_loss"))
    if strategy == "PEAD_SHORT":
        if target_price is not None and current_price <= target_price:
            return "target"
        if stop_loss is not None and current_price >= stop_loss:
            return "stop"
    else:
        if target_price is not None and current_price >= target_price:
            return "target"
        if stop_loss is not None and current_price <= stop_loss:
            return "stop"

    hold_days = _hold_days(row.get("created_at"))
    if hold_days is not None and hold_days >= max_hold_days:
        return "time_exit"
    return None


def _position_exit_status(row: dict, current_price: float | None, *, max_hold_days: int) -> PositionExitStatus:
    strategy = row["strategy"]
    shares = int(row.get("shares") or 0)
    entry_price = _float_or_none(row.get("entry_price"))
    target_price = _float_or_none(row.get("target_price"))
    stop_loss = _float_or_none(row.get("stop_loss"))
    hold_days = _hold_days(row.get("created_at"))
    side = "short" if strategy == "PEAD_SHORT" else "long"
    position_value = _float_or_none(row.get("position_value"))
    if position_value is None and entry_price is not None:
        position_value = entry_price * shares
    position_value = round(position_value or 0.0, 2)

    if current_price is None:
        return PositionExitStatus(
            id=str(row.get("id", "")),
            ticker=row["ticker"],
            strategy=strategy,
            side=side,
            shares=shares,
            entry_price=entry_price,
            current_price=None,
            target_price=target_price,
            stop_loss=stop_loss,
            position_value=position_value,
            unrealized_pnl=None,
            unrealized_pnl_pct=None,
            hold_days=hold_days,
            exit_reason=None,
            action="price_unavailable",
            status="price_unavailable",
            message="Waiting for a live price before deciding exit.",
        )

    exit_reason = _exit_reason(row, current_price, max_hold_days=max_hold_days)
    pnl = _unrealized_pnl(strategy, entry_price, current_price, shares)
    pnl_pct = (pnl / position_value) if pnl is not None and position_value else None
    if exit_reason == "target":
        action = "exit_profit"
        message = "Target reached. Bot should sell/cover for profit."
    elif exit_reason == "stop":
        action = "exit_loss"
        message = "Stop reached. Bot should sell/cover to cut loss."
    elif exit_reason == "time_exit":
        action = "exit_time"
        message = "Maximum hold reached. Bot should close the position."
    else:
        action = "hold"
        message = "Holding. Neither target nor stop is reached."

    return PositionExitStatus(
        id=str(row.get("id", "")),
        ticker=row["ticker"],
        strategy=strategy,
        side=side,
        shares=shares,
        entry_price=entry_price,
        current_price=round(current_price, 4),
        target_price=target_price,
        stop_loss=stop_loss,
        position_value=position_value,
        unrealized_pnl=round(pnl, 2) if pnl is not None else None,
        unrealized_pnl_pct=round(pnl_pct, 4) if pnl_pct is not None else None,
        hold_days=hold_days,
        exit_reason=exit_reason,
        action=action,
        status="exit_ready" if exit_reason else "holding",
        message=message,
    )


def _exit_trade_from_row(row: dict, exit_reason: str, *, current_price: float | None) -> TradeRecord:
    side = "cover" if row["strategy"] == "PEAD_SHORT" else "sell"
    entry_price = _float_or_none(row.get("entry_price"))
    shares = int(row.get("shares") or 1)
    pnl = _unrealized_pnl(row["strategy"], entry_price, current_price, shares) if current_price is not None else None
    position_value = _float_or_none(row.get("position_value"))
    return TradeRecord(
        ticker=row["ticker"],
        strategy=row["strategy"],
        side=side,
        entry_price=entry_price,
        exit_price=current_price,
        shares=shares,
        pnl=round(pnl, 2) if pnl is not None else None,
        pnl_pct=round(pnl / position_value, 4) if pnl is not None and position_value else None,
        hold_days=_hold_days(row.get("created_at")),
        exit_reason=exit_reason,
        earnings_surprise_pct=_float_or_none(row.get("surprise_pct")),
        analyst_count=row.get("analyst_count"),
    )


def _hold_days(created_at: str | None) -> int | None:
    if not created_at:
        return None
    opened = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return (datetime.now(UTC) - opened.astimezone(UTC)).days


def _float_or_none(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _safe_current_price(market_data: MarketDataProvider, ticker: str) -> float | None:
    try:
        price = market_data.company_profile(ticker).current_price
    except Exception:
        return None
    return price if price and price > 0 else None


def _unrealized_pnl(strategy: str, entry_price: float | None, current_price: float, shares: int) -> float | None:
    if entry_price is None:
        return None
    if strategy == "PEAD_SHORT":
        return (entry_price - current_price) * shares
    return (current_price - entry_price) * shares
