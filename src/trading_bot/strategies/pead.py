from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class PEADAction(str, Enum):
    BUY_NEXT_DAY = "BUY_NEXT_DAY"
    SHORT_NEXT_DAY = "SHORT_NEXT_DAY"
    SKIP = "SKIP"


class SentimentResult(BaseModel):
    label: str
    score: float = Field(ge=0, le=1)


class PEADInput(BaseModel):
    ticker: str
    actual_eps: float
    estimate_eps: float
    sentiment: SentimentResult
    analyst_count: int = Field(ge=0)
    market_cap_m: float
    price: float
    ema50: float
    earnings_day_volume: float
    avg_volume_20d: float
    is_shortable: bool = True


class PEADSignal(BaseModel):
    ticker: str
    action: PEADAction
    surprise_pct: float
    skip_reason: str = ""


def eps_surprise_pct(actual_eps: float, estimate_eps: float) -> float:
    if estimate_eps == 0:
        raise ValueError("estimate_eps cannot be zero")
    return (actual_eps - estimate_eps) / abs(estimate_eps) * 100


def evaluate_pead_signal(
    payload: PEADInput,
    *,
    min_surprise_pct: float = 5.0,
    min_sentiment_score: float = 0.70,
) -> PEADSignal:
    ticker = payload.ticker.upper()
    surprise = eps_surprise_pct(payload.actual_eps, payload.estimate_eps)
    common_skip = _common_universe_skip(payload)
    if common_skip:
        return PEADSignal(ticker=ticker, action=PEADAction.SKIP, surprise_pct=surprise, skip_reason=common_skip)

    volume_ratio = payload.earnings_day_volume / payload.avg_volume_20d if payload.avg_volume_20d else 0
    if volume_ratio < 2.0:
        return PEADSignal(ticker=ticker, action=PEADAction.SKIP, surprise_pct=surprise, skip_reason="earnings volume below 2x average")

    if surprise >= min_surprise_pct:
        if payload.sentiment.label.lower() != "positive" or payload.sentiment.score < min_sentiment_score:
            return PEADSignal(ticker=ticker, action=PEADAction.SKIP, surprise_pct=surprise, skip_reason="positive surprise not confirmed by FinBERT")
        if payload.price < payload.ema50:
            return PEADSignal(ticker=ticker, action=PEADAction.SKIP, surprise_pct=surprise, skip_reason="price below 50-day EMA")
        return PEADSignal(ticker=ticker, action=PEADAction.BUY_NEXT_DAY, surprise_pct=surprise)

    if surprise <= -min_surprise_pct:
        if payload.sentiment.label.lower() != "negative" or payload.sentiment.score < min_sentiment_score:
            return PEADSignal(ticker=ticker, action=PEADAction.SKIP, surprise_pct=surprise, skip_reason="negative surprise not confirmed by FinBERT")
        if payload.price > payload.ema50:
            return PEADSignal(ticker=ticker, action=PEADAction.SKIP, surprise_pct=surprise, skip_reason="price above 50-day EMA")
        if not payload.is_shortable:
            return PEADSignal(ticker=ticker, action=PEADAction.SKIP, surprise_pct=surprise, skip_reason="ticker is not shortable")
        return PEADSignal(ticker=ticker, action=PEADAction.SHORT_NEXT_DAY, surprise_pct=surprise)

    return PEADSignal(ticker=ticker, action=PEADAction.SKIP, surprise_pct=surprise, skip_reason="EPS surprise below threshold")


def _common_universe_skip(payload: PEADInput) -> str:
    if not 200 <= payload.market_cap_m <= 2_000:
        return "market cap outside PEAD universe"
    if payload.analyst_count > 5:
        return "too much analyst coverage"
    if payload.price <= 5:
        return "price below minimum"
    if payload.avg_volume_20d <= 50_000:
        return "average volume below minimum"
    return ""

