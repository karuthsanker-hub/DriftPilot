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
    finnhub_api_key: SecretStr | None = None
    fmp_api_key: SecretStr | None = None
    polygon_api_key: SecretStr | None = None

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
    paper_portfolio_value: float = Field(default=50_000, gt=0)
    pead_target_pct: float = Field(default=0.08, gt=0)
    pead_stop_pct: float = Field(default=0.04, gt=0)
    pead_max_hold_days: int = Field(default=20, ge=1)
    pead_sentiment: str = "finbert"
    operator_paper_capital: float = Field(default=10_000, gt=0)
    operator_target_pct: float = Field(default=0.01, gt=0)
    operator_stop_pct: float = Field(default=0.01, gt=0)
    operator_max_candidates: int = Field(default=100, ge=1, le=100)
    operator_trade_slots: int = Field(default=10, ge=1, le=100)
    operator_min_candidates: int = Field(default=5, ge=1, le=100)
    operator_refresh_batch_size: int = Field(default=1, ge=1, le=10)
    operator_refresh_interval_minutes: int = Field(default=5, ge=1, le=60)
    operator_universe_refresh_interval_minutes: int = Field(default=5, ge=1, le=60)
    operator_monitor_interval_minutes: int = Field(default=5, ge=1, le=60)
    market_data_retry_attempts: int = Field(default=3, ge=1, le=8)
    market_data_retry_backoff_seconds: float = Field(default=1.0, ge=0, le=30)
    pead_scan_tickers: list[str] = Field(default_factory=list)
    pead_universe_file: str = "config/pead_universe.csv"
    earnings_events_file: str = "config/earnings_events.csv"

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
        finnhub_api_key=_secret("FINNHUB_API_KEY"),
        fmp_api_key=_secret("FMP_API_KEY"),
        polygon_api_key=_secret("POLYGON_API_KEY"),
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
        paper_portfolio_value=float(os.getenv("PAPER_PORTFOLIO_VALUE", "50000")),
        pead_target_pct=float(os.getenv("PEAD_TARGET_PCT", "0.08")),
        pead_stop_pct=float(os.getenv("PEAD_STOP_PCT", "0.04")),
        pead_max_hold_days=int(os.getenv("PEAD_MAX_HOLD_DAYS", "20")),
        pead_sentiment=os.getenv("PEAD_SENTIMENT", "finbert").lower(),
        operator_paper_capital=float(os.getenv("OPERATOR_PAPER_CAPITAL", "10000")),
        operator_target_pct=float(os.getenv("OPERATOR_TARGET_PCT", "0.01")),
        operator_stop_pct=float(os.getenv("OPERATOR_STOP_PCT", "0.01")),
        operator_max_candidates=int(os.getenv("OPERATOR_MAX_CANDIDATES", "100")),
        operator_trade_slots=int(os.getenv("OPERATOR_TRADE_SLOTS", "10")),
        operator_min_candidates=int(os.getenv("OPERATOR_MIN_CANDIDATES", "5")),
        operator_refresh_batch_size=int(os.getenv("OPERATOR_REFRESH_BATCH_SIZE", "1")),
        operator_refresh_interval_minutes=int(os.getenv("OPERATOR_REFRESH_INTERVAL_MINUTES", "5")),
        operator_universe_refresh_interval_minutes=int(os.getenv("OPERATOR_UNIVERSE_REFRESH_INTERVAL_MINUTES", "5")),
        operator_monitor_interval_minutes=int(os.getenv("OPERATOR_MONITOR_INTERVAL_MINUTES", "5")),
        market_data_retry_attempts=int(os.getenv("MARKET_DATA_RETRY_ATTEMPTS", "3")),
        market_data_retry_backoff_seconds=float(os.getenv("MARKET_DATA_RETRY_BACKOFF_SECONDS", "1.0")),
        pead_scan_tickers=_csv("PEAD_SCAN_TICKERS"),
        pead_universe_file=os.getenv("PEAD_UNIVERSE_FILE", "config/pead_universe.csv"),
        earnings_events_file=os.getenv("EARNINGS_EVENTS_FILE", "config/earnings_events.csv"),
    )


def _secret(key: str) -> SecretStr | None:
    value = os.getenv(key, "")
    return SecretStr(value) if value else None


def _bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _csv(key: str, default: str = "") -> list[str]:
    value = os.getenv(key, default)
    return [item.strip().upper() for item in value.split(",") if item.strip()]
