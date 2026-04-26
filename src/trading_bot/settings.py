from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, SecretStr, field_validator


class AppSettings(BaseModel):
    app_env: str = "development"
    paper_mode: bool = True

    supabase_url: str = ""
    supabase_key: SecretStr | None = None

    alpaca_api_key: SecretStr | None = None
    alpaca_secret_key: SecretStr | None = None
    alpaca_base_url: str = "https://paper-api.alpaca.markets"

    fred_api_key: SecretStr | None = None

    trading_active: bool = True
    risk_per_trade_pct: float = Field(default=0.01, gt=0, le=0.05)
    max_position_pct: float = Field(default=0.20, gt=0, le=1)
    max_total_positions: int = Field(default=6, ge=1)
    max_pead_long_positions: int = Field(default=3, ge=0)
    max_pead_short_positions: int = Field(default=2, ge=0)
    max_momentum_positions: int = Field(default=1, ge=0)
    pead_min_surprise_pct: float = Field(default=5.0, gt=0)
    finbert_min_score: float = Field(default=0.70, ge=0, le=1)
    vix_pause_threshold: float = Field(default=25.0, gt=0)
    daily_loss_limit_pct: float = Field(default=-2.0, lt=0)
    spy_premarket_pause_pct: float = Field(default=-1.5, lt=0)

    @field_validator("alpaca_base_url")
    @classmethod
    def strip_url(cls, value: str) -> str:
        return value.rstrip("/")


def load_settings(env_path: str | Path = ".env") -> AppSettings:
    load_dotenv(env_path, override=False)
    return AppSettings(
        app_env=os.getenv("APP_ENV", "development"),
        paper_mode=_bool("PAPER_MODE", True),
        supabase_url=os.getenv("SUPABASE_URL", ""),
        supabase_key=_secret("SUPABASE_KEY"),
        alpaca_api_key=_secret("ALPACA_API_KEY"),
        alpaca_secret_key=_secret("ALPACA_SECRET_KEY"),
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        fred_api_key=_secret("FRED_API_KEY"),
        trading_active=_bool("TRADING_ACTIVE", True),
        risk_per_trade_pct=float(os.getenv("RISK_PER_TRADE_PCT", "0.01")),
        max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.20")),
        max_total_positions=int(os.getenv("MAX_TOTAL_POSITIONS", "6")),
        max_pead_long_positions=int(os.getenv("MAX_PEAD_LONG_POSITIONS", "3")),
        max_pead_short_positions=int(os.getenv("MAX_PEAD_SHORT_POSITIONS", "2")),
        max_momentum_positions=int(os.getenv("MAX_MOMENTUM_POSITIONS", "1")),
        pead_min_surprise_pct=float(os.getenv("PEAD_MIN_SURPRISE_PCT", "5.0")),
        finbert_min_score=float(os.getenv("FINBERT_MIN_SCORE", "0.70")),
        vix_pause_threshold=float(os.getenv("VIX_PAUSE_THRESHOLD", "25.0")),
        daily_loss_limit_pct=float(os.getenv("DAILY_LOSS_LIMIT_PCT", "-2.0")),
        spy_premarket_pause_pct=float(os.getenv("SPY_PREMARKET_PAUSE_PCT", "-1.5")),
    )


def _secret(key: str) -> SecretStr | None:
    value = os.getenv(key, "")
    return SecretStr(value) if value else None


def _bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}

