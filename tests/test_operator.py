from __future__ import annotations

from trading_bot.execution.alpaca_broker import OrderResult
from trading_bot.operator import approve_paper_trades, build_top_bets, momentum_rows_to_operator_rows
from trading_bot.settings import AppSettings


def _settings() -> AppSettings:
    return AppSettings(operator_paper_capital=10_000, operator_target_pct=0.01, operator_stop_pct=0.01, operator_max_candidates=100, operator_trade_slots=10)


def test_build_top_bets_projects_one_percent_profit_and_loss() -> None:
    result = build_top_bets(
        [
            {"id": "1", "ticker": "ABC", "strategy": "PEAD_LONG", "status": "pending", "entry_price": 100, "surprise_pct": 8},
            {"id": "2", "ticker": "XYZ", "strategy": "PEAD_LONG", "status": "skipped", "entry_price": 50, "surprise_pct": 12},
        ],
        _settings(),
    )

    candidate = result["candidates"][0]

    assert result["paper_capital"] == 10_000
    assert result["max_candidates"] == 100
    assert result["trade_slots"] == 10
    assert candidate["allocation"] == 1000
    assert candidate["sector"] == "Unclassified"
    assert candidate["industry"] == "Unclassified"
    assert candidate["shares"] == 10
    assert candidate["target_price"] == 101
    assert candidate["stop_loss"] == 99
    assert candidate["target_profit"] == 10
    assert candidate["max_loss"] == 10
    assert result["projection"]["planned_capital"] == 1000


def test_build_top_bets_handles_short_direction() -> None:
    result = build_top_bets(
        [{"id": "1", "ticker": "ABC", "strategy": "PEAD_SHORT", "status": "pending", "entry_price": 100, "surprise_pct": -8}],
        _settings(),
    )

    candidate = result["candidates"][0]

    assert candidate["direction"] == "short"
    assert candidate["target_price"] == 99
    assert candidate["stop_loss"] == 101


def test_momentum_rows_to_operator_rows_builds_fallback_candidates() -> None:
    rows = momentum_rows_to_operator_rows([{"ticker": "abc", "total_score": 7}], {"ABC": 50})

    assert rows == [
        {
            "id": "momentum:ABC",
            "ticker": "ABC",
            "strategy": "MOMENTUM",
            "status": "pending",
            "entry_price": 50,
            "surprise_pct": 7,
            "operator_only": True,
            "momentum_score": 7,
            "sector": None,
            "industry": None,
        }
    ]


class FakeRepo:
    def __init__(self) -> None:
        self.updated = []
        self.marked = []
        self.inserted = []

    def update_watchlist_trade_plan(self, watchlist_id, **payload):
        self.updated.append((watchlist_id, payload))

    def mark_watchlist_status(self, watchlist_id, status):
        self.marked.append((watchlist_id, status))

    def insert_watchlist_candidate(self, record):
        self.inserted.append(record)


class FakeBroker:
    def __init__(self) -> None:
        self.intents = []

    def submit_market_order(self, intent, *, dry_run=True):
        self.intents.append((intent, dry_run))
        return OrderResult(intent.ticker, intent.side, intent.shares, not dry_run, "ok", "order-1")


def test_approve_paper_trades_updates_plan_and_submits_selected() -> None:
    repo = FakeRepo()
    broker = FakeBroker()

    result = approve_paper_trades(
        rows=[{"id": "1", "ticker": "ABC", "strategy": "PEAD_LONG", "status": "pending", "entry_price": 100, "surprise_pct": 8}],
        selected_ids=["1"],
        settings=_settings(),
        repository=repo,
        broker=broker,
        submit=True,
    )

    assert result["attempted"] == 1
    assert result["submitted"][0]["submitted"] is True
    assert repo.updated[0][1]["target_price"] == 101
    assert repo.marked == [("1", "entered")]
    assert broker.intents[0][0].shares == 10


def test_approve_paper_trades_persists_operator_only_momentum_position() -> None:
    repo = FakeRepo()
    broker = FakeBroker()

    result = approve_paper_trades(
        rows=[
            {
                "id": "momentum:ABC",
                "ticker": "ABC",
                "strategy": "MOMENTUM",
                "status": "pending",
                "entry_price": 100,
                "surprise_pct": 4,
                "operator_only": True,
                "momentum_score": 4,
            }
        ],
        selected_ids=["momentum:ABC"],
        settings=_settings(),
        repository=repo,
        broker=broker,
        submit=True,
    )

    assert result["attempted"] == 1
    assert repo.updated == []
    assert repo.marked == []
    assert repo.inserted[0].status == "entered"
    assert repo.inserted[0].target_price == 101


def test_approve_paper_trades_skips_already_open_ticker() -> None:
    repo = FakeRepo()
    broker = FakeBroker()

    result = approve_paper_trades(
        rows=[{"id": "1", "ticker": "ABC", "strategy": "PEAD_LONG", "status": "pending", "entry_price": 100, "surprise_pct": 8}],
        selected_ids=["1"],
        settings=_settings(),
        repository=repo,
        broker=broker,
        submit=True,
        open_rows=[{"ticker": "ABC", "status": "entered"}],
    )

    assert result["attempted"] == 1
    assert result["skipped"] == [{"id": "1", "ticker": "ABC", "reason": "ticker already has an open paper position"}]
    assert result["submitted"] == []
    assert broker.intents == []
