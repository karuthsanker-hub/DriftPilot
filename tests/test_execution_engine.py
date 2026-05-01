from __future__ import annotations

from datetime import UTC, datetime, timedelta

from trading_bot.data.market_data import CompanyProfile
from trading_bot.execution.alpaca_broker import OrderResult
from trading_bot.execution.paper_engine import PaperExecutionEngine


class FakeTradingRepo:
    def __init__(self) -> None:
        self.marked = []
        self.trades = []

    def list_pending_watchlist(self):
        return [{"id": "1", "ticker": "ABC", "strategy": "PEAD_LONG", "shares": 4}]

    def list_entered_watchlist(self):
        return [
            {
                "id": "2",
                "ticker": "XYZ",
                "strategy": "PEAD_LONG",
                "shares": 5,
                "target_price": 110,
                "stop_loss": 90,
                "created_at": datetime.now(UTC).isoformat(),
            }
        ]

    def mark_watchlist_status(self, watchlist_id, status):
        self.marked.append((watchlist_id, status))

    def insert_trade(self, trade):
        self.trades.append(trade)


class FakeConfigRepo:
    def __init__(self, active=True) -> None:
        self.active = active

    def is_trading_active(self):
        return self.active


class FakeBroker:
    def __init__(self, submitted=False) -> None:
        self.intents = []
        self.submitted = submitted

    def submit_market_order(self, intent, *, dry_run=True):
        self.intents.append((intent, dry_run))
        return OrderResult(intent.ticker, intent.side, intent.shares, self.submitted, "ok")


class FakeMarketData:
    def __init__(self, price: float) -> None:
        self.price = price

    def company_profile(self, ticker: str) -> CompanyProfile:
        return CompanyProfile(ticker=ticker, market_cap_m=1000, analyst_count=5, current_price=self.price, avg_volume=1_000_000)


def test_execute_pending_defaults_to_dry_run() -> None:
    broker = FakeBroker(submitted=False)
    engine = PaperExecutionEngine(FakeTradingRepo(), FakeConfigRepo(), broker)

    summary = engine.execute_pending_watchlist()

    assert summary.attempted == 1
    assert summary.submitted == 0
    assert broker.intents[0][1] is True


def test_execute_pending_blocks_when_kill_switch_inactive() -> None:
    broker = FakeBroker()
    engine = PaperExecutionEngine(FakeTradingRepo(), FakeConfigRepo(active=False), broker)

    summary = engine.execute_pending_watchlist()

    assert summary.attempted == 0
    assert summary.blocked_reason == "kill switch inactive"
    assert broker.intents == []


def test_execute_pending_respects_position_caps() -> None:
    class FullLongRepo(FakeTradingRepo):
        def list_entered_watchlist(self):
            return [
                {"id": "2", "ticker": "A", "strategy": "PEAD_LONG"},
                {"id": "3", "ticker": "B", "strategy": "PEAD_LONG"},
                {"id": "4", "ticker": "C", "strategy": "PEAD_LONG"},
            ]

    repo = FullLongRepo()
    broker = FakeBroker(submitted=True)
    engine = PaperExecutionEngine(repo, FakeConfigRepo(), broker)

    summary = engine.execute_pending_watchlist(dry_run=False, max_pead_long_positions=3)

    assert summary.attempted == 1
    assert summary.submitted == 0
    assert summary.skipped == 1
    assert summary.blocked_reason == "position limit reached"
    assert broker.intents == []
    assert repo.marked == [("1", "skipped")]


def test_execute_pending_skips_duplicate_open_ticker() -> None:
    class DuplicateRepo(FakeTradingRepo):
        def list_pending_watchlist(self):
            return [{"id": "1", "ticker": "XYZ", "strategy": "MOMENTUM", "shares": 1}]

        def list_entered_watchlist(self):
            return [{"id": "2", "ticker": "XYZ", "strategy": "MOMENTUM"}]

    repo = DuplicateRepo()
    broker = FakeBroker(submitted=True)
    engine = PaperExecutionEngine(repo, FakeConfigRepo(), broker)

    summary = engine.execute_pending_watchlist(dry_run=False, max_momentum_positions=10)

    assert summary.submitted == 0
    assert summary.skipped == 1
    assert broker.intents == []


def test_manage_open_positions_exits_long_at_target() -> None:
    class PricedRepo(FakeTradingRepo):
        def list_entered_watchlist(self):
            row = super().list_entered_watchlist()[0]
            row["entry_price"] = 100
            row["position_value"] = 500
            return [row]

    repo = PricedRepo()
    broker = FakeBroker(submitted=True)
    engine = PaperExecutionEngine(repo, FakeConfigRepo(), broker)

    summary = engine.manage_open_positions(FakeMarketData(111), dry_run=False)

    assert summary.attempted == 1
    assert summary.submitted == 1
    assert broker.intents[0][0].side == "sell"
    assert repo.trades[0].exit_reason == "target"
    assert repo.trades[0].exit_price == 111
    assert repo.trades[0].pnl == 55
    assert repo.trades[0].pnl_pct == 0.11
    assert repo.marked == [("2", "exited")]


def test_manage_open_positions_time_exit_defaults_to_twenty_days() -> None:
    class TimeExitRepo(FakeTradingRepo):
        def list_entered_watchlist(self):
            row = super().list_entered_watchlist()[0]
            row["created_at"] = (datetime.now(UTC) - timedelta(days=20)).isoformat()
            row["target_price"] = 999
            row["stop_loss"] = 1
            return [row]

    repo = TimeExitRepo()
    broker = FakeBroker(submitted=True)
    engine = PaperExecutionEngine(repo, FakeConfigRepo(), broker)

    summary = engine.manage_open_positions(FakeMarketData(100), dry_run=False)

    assert summary.attempted == 1
    assert repo.trades[0].exit_reason == "time_exit"


def test_open_position_status_explains_profit_exit() -> None:
    repo = FakeTradingRepo()
    broker = FakeBroker()
    engine = PaperExecutionEngine(repo, FakeConfigRepo(), broker)

    statuses = engine.open_position_statuses(FakeMarketData(111))

    assert statuses[0].action == "exit_profit"
    assert statuses[0].exit_reason == "target"
    assert statuses[0].unrealized_pnl is None


def test_open_position_status_explains_hold_with_pnl() -> None:
    class PricedRepo(FakeTradingRepo):
        def list_entered_watchlist(self):
            row = super().list_entered_watchlist()[0]
            row["entry_price"] = 100
            row["position_value"] = 500
            return [row]

    engine = PaperExecutionEngine(PricedRepo(), FakeConfigRepo(), FakeBroker())

    statuses = engine.open_position_statuses(FakeMarketData(105))

    assert statuses[0].action == "hold"
    assert statuses[0].unrealized_pnl == 25
    assert statuses[0].unrealized_pnl_pct == 0.05
