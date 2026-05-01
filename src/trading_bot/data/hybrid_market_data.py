from __future__ import annotations

from datetime import date

import pandas as pd

from trading_bot.data.earnings_events import EarningsEventStore
from trading_bot.data.market_data import CompanyProfile, EarningsEvent, MarketDataProvider, MomentumFundamentals


class HybridMarketDataProvider:
    def __init__(self, primary: MarketDataProvider, earnings_store: EarningsEventStore | None = None) -> None:
        self.primary = primary
        self.earnings_store = earnings_store

    def company_profile(self, ticker: str) -> CompanyProfile:
        return self.primary.company_profile(ticker)

    def daily_history(self, ticker: str, *, period: str = "1y") -> pd.DataFrame:
        return self.primary.daily_history(ticker, period=period)

    def latest_earnings_event(self, ticker: str, scan_date: date) -> EarningsEvent | None:
        if self.earnings_store is not None:
            event = self.earnings_store.latest_event(ticker, scan_date)
            if event is not None:
                return event
        return self.primary.latest_earnings_event(ticker, scan_date)

    def momentum_fundamentals(self, ticker: str) -> MomentumFundamentals:
        fundamentals = self.primary.momentum_fundamentals(ticker)
        if self.earnings_store is None:
            return fundamentals
        surprises = self.earnings_store.surprise_history(ticker)
        if not surprises:
            return fundamentals
        return MomentumFundamentals(
            ticker=fundamentals.ticker,
            earnings_surprises_pct=surprises,
            roe=fundamentals.roe,
            debt_to_equity=fundamentals.debt_to_equity,
            profit_margin=fundamentals.profit_margin,
        )

    def spy_premarket_change_pct(self) -> float | None:
        return self.primary.spy_premarket_change_pct()
