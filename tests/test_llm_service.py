from __future__ import annotations

from datetime import date

import pytest

from trading_bot.llm.base import LLMAdapter, LLMAdapterError
from trading_bot.llm.models import DailyConfig, EveningInput, LearningLog, MorningInput, ProviderName, ProviderStatus
from trading_bot.llm.service import StrategyLLMService


class FlakyAdapter(LLMAdapter):
    provider = ProviderName.OPENAI
    model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    def generate_daily_config(self, payload: MorningInput) -> DailyConfig:
        self.calls += 1
        if self.calls == 1:
            raise LLMAdapterError("temporary failure")
        return DailyConfig(
            date=payload.date,
            provider=self.provider,
            model=self.model,
            trading_enabled=False,
            vix=payload.vix,
            watchlist=[],
            regime_reason="Test fallback-free retry.",
        )

    def generate_evening_review(self, payload: EveningInput) -> LearningLog:
        return LearningLog(
            date=payload.date,
            provider=self.provider,
            model=self.model,
            summary="Reviewed trades.",
        )

    def health_check(self) -> ProviderStatus:
        return ProviderStatus(provider=self.provider, model=self.model, configured=True, ok=True, message="ok")


class BrokenAdapter(FlakyAdapter):
    def generate_daily_config(self, payload: MorningInput) -> DailyConfig:
        raise LLMAdapterError("broken")


class UnexpectedFailureAdapter(FlakyAdapter):
    def generate_daily_config(self, payload: MorningInput) -> DailyConfig:
        raise RuntimeError("sdk outage")


def test_service_retries_before_writing_daily_config(tmp_path) -> None:
    path = tmp_path / "daily_config.json"
    service = StrategyLLMService(FlakyAdapter(), daily_config_path=path)

    config = service.generate_daily_config(MorningInput(date=date(2026, 4, 25), watchlist=["NVDA"], vix=18.0))

    assert config.provider == ProviderName.OPENAI
    assert path.exists()


def test_service_falls_back_to_previous_valid_config(tmp_path) -> None:
    path = tmp_path / "daily_config.json"
    previous = DailyConfig(
        date=date(2026, 4, 24),
        provider=ProviderName.CLAUDE,
        model="claude",
        trading_enabled=False,
        vix=28.0,
        watchlist=[],
        regime_reason="High VIX.",
    )
    path.write_text(previous.model_dump_json())
    service = StrategyLLMService(BrokenAdapter(), daily_config_path=path)

    config = service.generate_daily_config(MorningInput(date=date(2026, 4, 25), watchlist=["NVDA"], vix=18.0))

    assert config.date == previous.date
    assert config.provider == ProviderName.CLAUDE


def test_service_raises_when_no_fallback_exists(tmp_path) -> None:
    service = StrategyLLMService(BrokenAdapter(), daily_config_path=tmp_path / "missing.json")

    with pytest.raises(LLMAdapterError):
        service.generate_daily_config(MorningInput(date=date(2026, 4, 25), watchlist=["NVDA"], vix=18.0))


def test_service_fallback_handles_unexpected_provider_exceptions(tmp_path) -> None:
    path = tmp_path / "daily_config.json"
    previous = DailyConfig(
        date=date(2026, 4, 24),
        provider=ProviderName.OPENAI,
        model="gpt",
        trading_enabled=False,
        vix=28.0,
        watchlist=[],
        regime_reason="High VIX.",
    )
    path.write_text(previous.model_dump_json())
    service = StrategyLLMService(UnexpectedFailureAdapter(), daily_config_path=path)

    config = service.generate_daily_config(MorningInput(date=date(2026, 4, 25), watchlist=["NVDA"], vix=18.0))

    assert config == previous
