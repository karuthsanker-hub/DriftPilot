from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from trading_bot.llm.models import DailyConfig, ProviderName, Sentiment, WatchlistPick, daily_config_schema


def valid_pick(**overrides) -> dict:
    payload = {
        "ticker": "nvda",
        "sentiment": Sentiment.BULLISH,
        "entry_price": 100.0,
        "target_price": 101.0,
        "stop_loss": 99.5,
        "max_shares": 5,
        "confidence": 0.8,
        "rationale": "Positive overnight catalyst.",
        "risk_notes": "Paper trade only.",
    }
    payload.update(overrides)
    return payload


def test_daily_config_accepts_valid_provider_neutral_shape() -> None:
    config = DailyConfig.model_validate(
        {
            "date": date(2026, 4, 25),
            "provider": ProviderName.OPENAI,
            "model": "gpt-4.1",
            "trading_enabled": True,
            "vix": 18.0,
            "watchlist": [valid_pick()],
            "regime_reason": "Normal volatility.",
        }
    )

    assert config.watchlist[0].ticker == "NVDA"


def test_rejects_invalid_target_and_stop_math() -> None:
    with pytest.raises(ValidationError):
        WatchlistPick.model_validate(valid_pick(target_price=99.0))

    with pytest.raises(ValidationError):
        WatchlistPick.model_validate(valid_pick(stop_loss=101.0))


def test_rejects_more_than_five_watchlist_picks() -> None:
    with pytest.raises(ValidationError):
        DailyConfig.model_validate(
            {
                "date": date(2026, 4, 25),
                "provider": ProviderName.CLAUDE,
                "model": "claude",
                "trading_enabled": True,
                "vix": 18.0,
                "watchlist": [valid_pick(ticker=f"T{i}") for i in range(6)],
                "regime_reason": "",
            }
        )


def test_vix_above_threshold_forces_trading_disabled() -> None:
    with pytest.raises(ValidationError):
        DailyConfig.model_validate(
            {
                "date": date(2026, 4, 25),
                "provider": ProviderName.GEMINI,
                "model": "gemini",
                "trading_enabled": True,
                "vix": 26.0,
                "watchlist": [],
                "regime_reason": "High VIX.",
            }
        )


def test_daily_config_schema_is_provider_adapter_contract() -> None:
    schema = daily_config_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"date", "provider", "model", "trading_enabled", "vix", "watchlist", "regime_reason"}
    assert schema["properties"]["watchlist"]["maxItems"] == 5
