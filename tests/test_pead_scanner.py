from __future__ import annotations

from datetime import date

import pandas as pd

from trading_bot.data.market_data import CompanyProfile, EarningsEvent
from trading_bot.scanners.pead_scanner import PEADScanner
from trading_bot.sentiment import KeywordSentimentScorer
from trading_bot.strategies.pead import PEADAction


class FakeMarketData:
    def company_profile(self, ticker: str) -> CompanyProfile:
        return CompanyProfile(ticker=ticker, market_cap_m=800, analyst_count=3, current_price=12, avg_volume=100_000)

    def daily_history(self, ticker: str, *, period: str = "1y") -> pd.DataFrame:
        return pd.DataFrame(
            {
                "close": [10] * 55 + [12],
                "high": [11] * 56,
                "low": [9] * 56,
                "volume": [100_000] * 55 + [250_000],
            }
        )

    def latest_earnings_event(self, ticker: str, scan_date: date):
        return EarningsEvent(ticker=ticker, earnings_date=scan_date, actual_eps=1.1, estimate_eps=1.0, text="Company beat estimates with strong growth")

    def spy_premarket_change_pct(self):
        return 0


class FakeRepo:
    def __init__(self) -> None:
        self.records = []

    def insert_watchlist_candidate(self, record):
        self.records.append(record)


def test_pead_scanner_persists_passing_signal() -> None:
    repo = FakeRepo()
    scanner = PEADScanner(FakeMarketData(), KeywordSentimentScorer(), repo)

    [result] = scanner.scan(["abcd"], date(2026, 4, 25))

    assert result.signal.action == PEADAction.BUY_NEXT_DAY
    assert result.persisted is True
    assert result.entry_price == 12
    assert result.target_price == 12.96
    assert result.stop_loss == 11.52
    assert result.shares == 125
    assert repo.records[0].ticker == "ABCD"
    assert repo.records[0].strategy == "PEAD_LONG"
    assert repo.records[0].entry_price == 12
    assert repo.records[0].target_price == 12.96
    assert repo.records[0].stop_loss == 11.52
    assert repo.records[0].shares == 125


def test_pead_scanner_can_persist_skips() -> None:
    class NoEarningsMarketData(FakeMarketData):
        def latest_earnings_event(self, ticker: str, scan_date: date):
            return None

    repo = FakeRepo()
    scanner = PEADScanner(NoEarningsMarketData(), KeywordSentimentScorer(), repo)

    [result] = scanner.scan(["abcd"], date(2026, 4, 25), persist_skips=True)

    assert result.signal.action == PEADAction.SKIP
    assert result.persisted is True
    assert repo.records[0].status == "skipped"
