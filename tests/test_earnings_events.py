from __future__ import annotations

from datetime import date

import pandas as pd

from trading_bot.data.earnings_events import EarningsEventStore
from trading_bot.data.market_data import EarningsEvent
from trading_bot.data.hybrid_market_data import HybridMarketDataProvider
from trading_bot.data.market_data import CompanyProfile, MomentumFundamentals


class FakePrimary:
    def company_profile(self, ticker: str) -> CompanyProfile:
        return CompanyProfile(ticker=ticker, market_cap_m=1000, analyst_count=5, current_price=10, avg_volume=1_000_000)

    def daily_history(self, ticker: str, *, period: str = "1y") -> pd.DataFrame:
        return pd.DataFrame({"close": [10.0]})

    def latest_earnings_event(self, ticker: str, scan_date: date):
        return None

    def momentum_fundamentals(self, ticker: str) -> MomentumFundamentals:
        return MomentumFundamentals(ticker=ticker, earnings_surprises_pct=[], roe=20, debt_to_equity=0.4, profit_margin=15)

    def spy_premarket_change_pct(self):
        return None


def test_earnings_event_store_reads_latest_event_and_surprise_history(tmp_path) -> None:
    csv_path = tmp_path / "earnings.csv"
    csv_path.write_text(
        "ticker,earnings_date,actual_eps,estimate_eps,text\n"
        "ABC,2026-04-24,1.20,1.00,ABC beat estimates with strong growth\n"
        "ABC,2026-01-24,0.90,1.00,ABC missed estimates\n"
    )
    store = EarningsEventStore(csv_path)

    event = store.latest_event("abc", date(2026, 4, 26))

    assert event is not None
    assert event.actual_eps == 1.2
    assert [round(value, 2) for value in store.surprise_history("ABC")] == [20.0, -10.0]


def test_hybrid_market_data_prefers_local_earnings_event(tmp_path) -> None:
    csv_path = tmp_path / "earnings.csv"
    csv_path.write_text("ticker,earnings_date,actual_eps,estimate_eps,text\nABC,2026-04-24,1.20,1.00,ABC beat estimates\n")
    provider = HybridMarketDataProvider(FakePrimary(), EarningsEventStore(csv_path))

    event = provider.latest_earnings_event("ABC", date(2026, 4, 26))

    assert event is not None
    assert event.estimate_eps == 1.0


def test_hybrid_market_data_uses_local_surprises_for_momentum(tmp_path) -> None:
    csv_path = tmp_path / "earnings.csv"
    csv_path.write_text(
        "ticker,earnings_date,actual_eps,estimate_eps,text\n"
        "ABC,2026-04-24,1.20,1.00,beat\n"
        "ABC,2026-01-24,1.10,1.00,beat\n"
        "ABC,2025-10-24,1.05,1.00,beat\n"
        "ABC,2025-07-24,0.95,1.00,miss\n"
    )
    provider = HybridMarketDataProvider(FakePrimary(), EarningsEventStore(csv_path))

    fundamentals = provider.momentum_fundamentals("ABC")

    assert [round(value, 2) for value in fundamentals.earnings_surprises_pct[:3]] == [20.0, 10.0, 5.0]


def test_earnings_event_store_writes_events(tmp_path) -> None:
    csv_path = tmp_path / "nested" / "earnings.csv"
    store = EarningsEventStore(csv_path)

    store.write_events([EarningsEvent(ticker="ABC", earnings_date=date(2026, 4, 24), actual_eps=1.2, estimate_eps=1.0, text="beat")])

    assert "ABC,2026-04-24,1.2,1.0,beat" in csv_path.read_text()
