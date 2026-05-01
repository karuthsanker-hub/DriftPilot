from __future__ import annotations

from datetime import date

import pandas as pd

from trading_bot.data.market_data import MomentumFundamentals
from trading_bot.scanners.momentum_scanner import MomentumScanner


class FakeMarketData:
    def daily_history(self, ticker: str, *, period: str = "1y") -> pd.DataFrame:
        return pd.DataFrame({"close": [100.0] * 64 + [110.0] * 63 + [130.0]})

    def momentum_fundamentals(self, ticker: str) -> MomentumFundamentals:
        return MomentumFundamentals(
            ticker=ticker,
            earnings_surprises_pct=[1.0, 2.0, -1.0, 3.0],
            roe=20.0,
            debt_to_equity=0.4,
            profit_margin=15.0,
        )


class FakeRepo:
    def __init__(self) -> None:
        self.records = []

    def insert_momentum_score(self, record) -> None:
        self.records.append(record)


def test_momentum_scanner_persists_scores_that_meet_threshold() -> None:
    repo = FakeRepo()
    scanner = MomentumScanner(FakeMarketData(), repo)

    result = scanner.scan_one("abc", date(2026, 4, 26), min_score=4)

    assert result.ticker == "ABC"
    assert result.score is not None
    assert result.score.total_score == 6
    assert result.persisted is True
    assert repo.records[0].ticker == "ABC"


def test_momentum_scanner_skips_short_history() -> None:
    class ShortHistoryMarketData(FakeMarketData):
        def daily_history(self, ticker: str, *, period: str = "1y") -> pd.DataFrame:
            return pd.DataFrame({"close": [100.0] * 20})

    scanner = MomentumScanner(ShortHistoryMarketData())

    result = scanner.scan_one("abc", date(2026, 4, 26))

    assert result.score is None
    assert result.persisted is False
    assert result.skip_reason == "not enough price history"


def test_momentum_scanner_scores_price_and_quality_when_earnings_history_missing() -> None:
    class MissingEarningsMarketData(FakeMarketData):
        def momentum_fundamentals(self, ticker: str) -> MomentumFundamentals:
            return MomentumFundamentals(
                ticker=ticker,
                earnings_surprises_pct=[],
                roe=20.0,
                debt_to_equity=0.4,
                profit_margin=15.0,
            )

    repo = FakeRepo()
    scanner = MomentumScanner(MissingEarningsMarketData(), repo)

    result = scanner.scan_one("abc", date(2026, 4, 26), min_score=4)

    assert result.score is not None
    assert result.score.total_score == 4
    assert result.score.earnings_momentum == 0
    assert result.persisted is True
    assert result.skip_reason == "earnings surprise history incomplete; found 0"


def test_momentum_scanner_uses_price_only_when_fundamentals_fail() -> None:
    class FailingFundamentalsMarketData(FakeMarketData):
        def momentum_fundamentals(self, ticker: str) -> MomentumFundamentals:
            raise RuntimeError("rate limited")

    repo = FakeRepo()
    scanner = MomentumScanner(FailingFundamentalsMarketData(), repo)

    result = scanner.scan_one("abc", date(2026, 4, 26), min_score=2)

    assert result.score is not None
    assert result.score.total_score == 2
    assert result.persisted is True
    assert "price-only score used" in result.skip_reason
