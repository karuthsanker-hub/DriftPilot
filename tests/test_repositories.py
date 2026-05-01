from __future__ import annotations

from datetime import date

from trading_bot.data.repositories import (
    MomentumScoreRecord,
    StrategyConfigRepository,
    TradeRecord,
    TradingRepository,
    WatchlistRecord,
)


class Result:
    def __init__(self, data=None) -> None:
        self.data = data or {}


class FakeQuery:
    def __init__(self, client, table_name: str) -> None:
        self.client = client
        self.table_name = table_name
        self.operation = None
        self.payload = None

    def select(self, *_):
        self.operation = "select"
        return self

    def eq(self, key, value):
        self.client.filters.append((self.table_name, key, value))
        return self

    def in_(self, key, value):
        self.client.filters.append((self.table_name, key, tuple(value)))
        return self

    def order(self, key, desc=False):
        self.client.orders.append((self.table_name, key, desc))
        return self

    def limit(self, value):
        self.client.limits.append((self.table_name, value))
        return self

    def single(self):
        return self

    def insert(self, payload):
        self.operation = "insert"
        self.payload = payload
        return self

    def upsert(self, payload):
        self.operation = "upsert"
        self.payload = payload
        return self

    def update(self, payload):
        self.operation = "update"
        self.payload = payload
        return self

    def execute(self):
        if self.operation == "select":
            return Result({"value": "true"})
        self.client.operations.append((self.table_name, self.operation, self.payload))
        return Result(self.payload)


class FakeSupabase:
    def __init__(self) -> None:
        self.operations = []
        self.filters = []
        self.orders = []
        self.limits = []

    def table(self, table_name: str):
        return FakeQuery(self, table_name)

    def rpc(self, fn: str, params=None):
        self.operations.append(("rpc", fn, params))
        return self

    def execute(self):
        return Result([{"deleted_trades": 1, "deleted_watchlist": 2, "deleted_daily_summary": 3, "deleted_momentum_scores": 4}])


def test_strategy_config_reads_kill_switch() -> None:
    client = FakeSupabase()
    repo = StrategyConfigRepository(client)

    assert repo.is_trading_active() is True
    assert client.filters == [("strategy_config", "key", "trading_active")]


def test_trading_repository_maps_records_to_tables() -> None:
    client = FakeSupabase()
    repo = TradingRepository(client)

    repo.insert_trade(TradeRecord(ticker="ABC", strategy="PEAD_LONG", side="buy", shares=10))
    repo.insert_watchlist_candidate(WatchlistRecord(ticker="ABC", strategy="PEAD_LONG"))
    repo.insert_momentum_score(
        MomentumScoreRecord(
            ticker="ABC",
            scan_date=date(2026, 4, 25),
            total_score=5,
            price_momentum=2,
            earnings_momentum=2,
            quality_score=1,
        )
    )

    assert client.operations[0][0] == "trades"
    assert client.operations[1][0] == "watchlist"
    assert client.operations[2][0] == "momentum_scores"


def test_trading_repository_lists_recent_momentum_scores() -> None:
    client = FakeSupabase()
    repo = TradingRepository(client)

    repo.list_recent_momentum_scores(limit=10)

    assert client.orders == [("momentum_scores", "scan_date", True), ("momentum_scores", "total_score", True)]
    assert client.limits == [("momentum_scores", 10)]


def test_trading_repository_lists_pending_and_candidate_watchlist_for_entry() -> None:
    client = FakeSupabase()
    repo = TradingRepository(client)

    repo.list_pending_watchlist()

    assert client.filters[-1] == ("watchlist", "status", ("pending", "candidate"))


def test_trading_repository_lists_entered_watchlist() -> None:
    client = FakeSupabase()
    repo = TradingRepository(client)

    repo.list_entered_watchlist()

    assert client.filters[-1] == ("watchlist", "status", "entered")


def test_trading_repository_lists_candidate_watchlist() -> None:
    client = FakeSupabase()
    repo = TradingRepository(client)

    repo.list_candidate_watchlist()

    assert client.filters[-1] == ("watchlist", "status", ("candidate", "pending"))


def test_trading_repository_lists_watchlist_by_ids() -> None:
    client = FakeSupabase()
    repo = TradingRepository(client)

    repo.list_watchlist_by_ids(["a", "b"])

    assert client.filters[-1] == ("watchlist", "id", ("a", "b"))


def test_trading_repository_updates_watchlist_trade_plan() -> None:
    client = FakeSupabase()
    repo = TradingRepository(client)

    repo.update_watchlist_trade_plan(
        "abc",
        entry_price=100,
        target_price=101,
        stop_loss=99,
        shares=10,
        risk_dollars=10,
        position_value=1000,
    )

    assert client.operations[-1][0] == "watchlist"
    assert client.operations[-1][2]["target_price"] == 101
    assert client.filters[-1] == ("watchlist", "id", "abc")


def test_trading_repository_lists_trades_and_daily_summaries() -> None:
    client = FakeSupabase()
    repo = TradingRepository(client)

    repo.list_recent_trades(limit=7)
    repo.list_daily_summaries(limit=3)

    assert ("trades", "created_at", True) in client.orders
    assert ("daily_summary", "date", True) in client.orders
    assert ("trades", 7) in client.limits
    assert ("daily_summary", 3) in client.limits


def test_trading_repository_resets_operator_paper_state() -> None:
    client = FakeSupabase()
    repo = TradingRepository(client)

    result = repo.reset_operator_paper_state()

    assert client.operations[-1] == ("rpc", "reset_operator_paper_state", None)
    assert result["deleted_trades"] == 1
    assert result["deleted_watchlist"] == 2
