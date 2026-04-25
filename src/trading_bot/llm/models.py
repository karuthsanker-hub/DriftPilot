from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


class ProviderName(str, Enum):
    OPENAI = "openai"
    CLAUDE = "claude"
    GEMINI = "gemini"
    QWEN = "qwen"


class Sentiment(str, Enum):
    BULLISH = "bullish"
    NEUTRAL = "neutral"
    BEARISH = "bearish"


class ProviderStatus(BaseModel):
    provider: ProviderName
    model: str
    configured: bool
    ok: bool
    message: str
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CostEstimate(BaseModel):
    provider: ProviderName
    model: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_usd: float | None = None


class WatchlistPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=10)
    sentiment: Sentiment
    entry_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    max_shares: int = Field(ge=0, le=10_000)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=2_000)
    risk_notes: str = Field(default="", max_length=2_000)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper().strip()

    @model_validator(mode="after")
    def validate_price_ladder(self) -> WatchlistPick:
        if self.target_price <= self.entry_price:
            raise ValueError("target_price must be greater than entry_price")
        if self.stop_loss >= self.entry_price:
            raise ValueError("stop_loss must be less than entry_price")
        return self


class DailyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    provider: ProviderName
    model: str
    trading_enabled: bool
    vix: float = Field(ge=0, le=100)
    watchlist: list[WatchlistPick] = Field(default_factory=list, max_length=5)
    regime_reason: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def validate_regime_and_watchlist(self) -> DailyConfig:
        if self.vix > 25 and self.trading_enabled:
            raise ValueError("trading_enabled must be false when VIX is above 25")
        if not self.trading_enabled and self.watchlist and self.vix > 25:
            raise ValueError("watchlist must be empty when VIX disables trading")
        return self


class TradeSummary(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    price: float
    shares: int
    timestamp: str
    reason: str = ""
    pnl: float | None = None


class LearningLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: date
    provider: ProviderName
    model: str
    summary: str = Field(min_length=1, max_length=4_000)
    patterns_observed: list[str] = Field(default_factory=list, max_length=20)
    failed_patterns_to_suppress: list[str] = Field(default_factory=list, max_length=20)
    threshold_adjustments: dict[str, float | int | str | bool] = Field(default_factory=dict)
    risk_notes: str = Field(default="", max_length=2_000)


class MorningInput(BaseModel):
    date: date
    watchlist: list[str]
    overnight_news: list[dict[str, Any]] = Field(default_factory=list)
    vix: float = Field(ge=0, le=100)
    prior_summary: str = ""

    @field_validator("watchlist")
    @classmethod
    def normalize_watchlist(cls, value: list[str]) -> list[str]:
        return [ticker.upper().strip() for ticker in value if ticker.strip()]


class EveningInput(BaseModel):
    date: date
    trades: list[TradeSummary] = Field(default_factory=list)
    daily_pnl: float
    daily_pnl_pct: float
    notes: str = ""


class ProviderSettings(BaseModel):
    active_provider: ProviderName = ProviderName.OPENAI
    openai_model: str = "gpt-4.1"
    claude_model: str = "claude-sonnet-4-20250514"
    gemini_model: str = "gemini-2.5-pro"
    qwen_base_url: str = "http://localhost:8001/v1"
    qwen_model: str = "qwen2.5-coder"
    openai_configured: bool = False
    anthropic_configured: bool = False
    gemini_configured: bool = False
    qwen_configured: bool = False

    @field_validator("qwen_base_url")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return value.rstrip("/")


def daily_config_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["date", "provider", "model", "trading_enabled", "vix", "watchlist", "regime_reason"],
        "properties": {
            "date": {"type": "string", "format": "date"},
            "provider": {"type": "string", "enum": [provider.value for provider in ProviderName]},
            "model": {"type": "string"},
            "trading_enabled": {"type": "boolean"},
            "vix": {"type": "number", "minimum": 0, "maximum": 100},
            "watchlist": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "ticker",
                        "sentiment",
                        "entry_price",
                        "target_price",
                        "stop_loss",
                        "max_shares",
                        "confidence",
                        "rationale",
                        "risk_notes",
                    ],
                    "properties": {
                        "ticker": {"type": "string"},
                        "sentiment": {"type": "string", "enum": [sentiment.value for sentiment in Sentiment]},
                        "entry_price": {"type": "number", "exclusiveMinimum": 0},
                        "target_price": {"type": "number", "exclusiveMinimum": 0},
                        "stop_loss": {"type": "number", "exclusiveMinimum": 0},
                        "max_shares": {"type": "integer", "minimum": 0, "maximum": 10000},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string"},
                        "risk_notes": {"type": "string"},
                    },
                },
            },
            "regime_reason": {"type": "string"},
        },
    }


def learning_log_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "date",
            "provider",
            "model",
            "summary",
            "patterns_observed",
            "failed_patterns_to_suppress",
            "threshold_adjustments",
            "risk_notes",
        ],
        "properties": {
            "date": {"type": "string", "format": "date"},
            "provider": {"type": "string", "enum": [provider.value for provider in ProviderName]},
            "model": {"type": "string"},
            "summary": {"type": "string"},
            "patterns_observed": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "failed_patterns_to_suppress": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "threshold_adjustments": {"type": "object", "additionalProperties": True},
            "risk_notes": {"type": "string"},
        },
    }
