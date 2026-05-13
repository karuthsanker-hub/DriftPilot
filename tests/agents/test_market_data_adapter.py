"""Tests for the market data adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from driftpilot.agents.market_data_adapter import (
    MarketDataAdapter,
    _count_consolidation,
    _session_return_pct,
    _std_dev,
)


@dataclass(frozen=True)
class FakeBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int | None = None
    vwap: float | None = None


@dataclass(frozen=True)
class FakeQuote:
    symbol: str
    timestamp: datetime
    bid_price: float
    ask_price: float
    bid_size: float | None = None
    ask_size: float | None = None


class FakeBarProvider:
    def __init__(self, bars=None, quotes=None):
        self._bars = bars or {}
        self._quotes = quotes or {}

    def session_bars(self, symbol):
        return list(self._bars.get(symbol.upper(), []))

    def latest_bar(self, symbol):
        bars = self._bars.get(symbol.upper(), [])
        return bars[-1] if bars else None

    def latest_quote(self, symbol):
        return self._quotes.get(symbol.upper())


NOW = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)


class TestStdDev:
    def test_empty(self):
        assert _std_dev([]) == 0.0

    def test_single(self):
        assert _std_dev([5.0]) == 0.0

    def test_known_values(self):
        # [2, 4, 4, 4, 5, 5, 7, 9] → mean=5, variance=4, std=2
        result = _std_dev([2, 4, 4, 4, 5, 5, 7, 9])
        assert abs(result - 2.0) < 0.01


class TestSessionReturn:
    def test_empty(self):
        assert _session_return_pct([]) == 0.0

    def test_single_bar(self):
        bar = FakeBar("SPY", NOW, 100, 101, 99, 100.5, 1000)
        assert _session_return_pct([bar]) == 0.0

    def test_positive_return(self):
        bars = [
            FakeBar("SPY", NOW, 100.0, 101, 99, 100.5, 1000),
            FakeBar("SPY", NOW, 100.5, 102, 100, 102.0, 1000),
        ]
        pct = _session_return_pct(bars)
        assert abs(pct - 2.0) < 0.01


class TestConsolidation:
    def test_empty(self):
        assert _count_consolidation([]) == 0

    def test_no_consolidation(self):
        assert _count_consolidation([100.0, 101.0, 102.0]) == 0

    def test_flat_bars(self):
        # Last 3 bars barely move
        closes = [100.0, 100.05, 100.05, 100.05]
        assert _count_consolidation(closes) >= 2


class TestMarketDataAdapter:
    def test_compute_with_bars(self):
        bars = [
            FakeBar("AAPL", NOW, 150.0, 151, 149, 150.5, 1000),
            FakeBar("AAPL", NOW, 150.5, 152, 150, 151.0, 1200),
            FakeBar("AAPL", NOW, 151.0, 152, 150.5, 151.5, 1100),
        ]
        spy_bars = [
            FakeBar("SPY", NOW, 500.0, 501, 499, 500.5, 10000),
            FakeBar("SPY", NOW, 500.5, 502, 500, 501.0, 11000),
        ]
        quote = FakeQuote("AAPL", NOW, 151.4, 151.6)
        provider = FakeBarProvider(
            bars={"AAPL": bars, "SPY": spy_bars},
            quotes={"AAPL": quote},
        )

        adapter = MarketDataAdapter(bar_provider=provider, vix_value=18.5)
        fields = adapter.compute("AAPL", sector="Technology")

        assert len(fields.last_10_closes) == 3
        assert fields.last_10_closes[-1] == 151.5
        assert fields.avg_vol > 0
        assert fields.vix == 18.5
        assert fields.current_price == (151.4 + 151.6) / 2.0
        assert fields.spy_move_pct != 0.0

    def test_compute_without_provider(self):
        adapter = MarketDataAdapter()
        fields = adapter.compute("AAPL")

        assert fields.last_10_closes == []
        assert fields.avg_vol == 0
        assert fields.current_price is None

    def test_set_vix(self):
        adapter = MarketDataAdapter()
        adapter.set_vix(25.0)
        fields = adapter.compute("AAPL")
        assert fields.vix == 25.0

    def test_compute_with_catalyst_db(self, tmp_path):
        import sqlite3

        db_path = str(tmp_path / "catalyst.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE catalyst_events ("
            "id INTEGER PRIMARY KEY, symbol TEXT, headline TEXT, event_ts TEXT, "
            "category TEXT, subcategory TEXT)"
        )
        conn.execute(
            "INSERT INTO catalyst_events (symbol, headline, event_ts, category, subcategory) "
            "VALUES (?, ?, ?, ?, ?)",
            ("AAPL", "AAPL beats Q3 earnings", "2026-05-12T13:00:00+00:00", "earnings", "beat"),
        )
        conn.commit()
        conn.close()

        adapter = MarketDataAdapter(catalyst_db_path=db_path)
        fields = adapter.compute(
            "AAPL",
            entry_time=datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
        )
        assert "AAPL beats Q3 earnings" in fields.new_headlines
