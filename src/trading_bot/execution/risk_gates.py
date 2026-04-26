from __future__ import annotations

from dataclasses import dataclass

from trading_bot.data.market_data import MarketDataProvider
from trading_bot.data.repositories import StrategyConfigRepository
from trading_bot.settings import AppSettings
from trading_bot.strategies.risk import PauseDecision, evaluate_daily_pause


@dataclass(frozen=True)
class RiskSnapshot:
    trading_active: bool
    vix: float | None
    daily_pnl_pct: float
    spy_premarket_change_pct: float | None


class RiskGate:
    def __init__(
        self,
        settings: AppSettings,
        config_repo: StrategyConfigRepository,
        market_data: MarketDataProvider,
    ) -> None:
        self.settings = settings
        self.config_repo = config_repo
        self.market_data = market_data

    def evaluate(self, *, vix: float | None, daily_pnl_pct: float = 0.0) -> PauseDecision:
        trading_active = self.settings.trading_active and self.config_repo.is_trading_active()
        return evaluate_daily_pause(
            trading_active=trading_active,
            vix=vix,
            daily_pnl_pct=daily_pnl_pct,
            spy_premarket_change_pct=self.market_data.spy_premarket_change_pct(),
            vix_threshold=self.settings.vix_pause_threshold,
            daily_loss_limit_pct=self.settings.daily_loss_limit_pct,
            spy_premarket_pause_pct=self.settings.spy_premarket_pause_pct,
        )

