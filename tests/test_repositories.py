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

    def execute(self):
        if self.operation == "select":
            return Result({"value": "true"})
        self.client.operations.append((self.table_name, self.operation, self.payload))
        return Result(self.payload)


class FakeSupabase:
    def __init__(self) -> None:
        self.operations = []
        self.filters = []

    def table(self, table_name: str):
        return FakeQuery(self, table_name)


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

