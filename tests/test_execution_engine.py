from __future__ import annotations

from trading_bot.execution.alpaca_broker import OrderResult
from trading_bot.execution.paper_engine import PaperExecutionEngine


class FakeTradingRepo:
    def __init__(self) -> None:
        self.marked = []

    def list_pending_watchlist(self):
        return [{"id": "1", "ticker": "ABC", "strategy": "PEAD_LONG", "shares": 4}]

    def mark_watchlist_status(self, watchlist_id, status):
        self.marked.append((watchlist_id, status))


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

