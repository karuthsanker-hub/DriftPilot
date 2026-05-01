from __future__ import annotations

from pathlib import Path

from trading_bot.data.earnings_events import EarningsEventStore
from trading_bot.data.hybrid_market_data import HybridMarketDataProvider
from trading_bot.data.market_data import MarketDataProvider
from trading_bot.data.replacement_stack import ReplacementStackMarketDataProvider
from trading_bot.settings import AppSettings


def create_market_data_provider(settings: AppSettings, *, env_path: str | Path = ".env") -> MarketDataProvider:
    earnings_store = EarningsEventStore.from_env_path(settings.earnings_events_file, env_path=env_path)
    primary = ReplacementStackMarketDataProvider(settings)
    return HybridMarketDataProvider(primary, earnings_store)
