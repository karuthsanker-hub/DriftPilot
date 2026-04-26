from __future__ import annotations

from pydantic import BaseModel, Field


class MomentumInput(BaseModel):
    ticker: str
    current_close: float
    close_63d_ago: float
    close_126d_ago: float
    earnings_surprises_pct: list[float] = Field(min_length=4, max_length=4)
    roe: float
    debt_to_equity: float
    profit_margin: float


class MomentumScore(BaseModel):
    ticker: str
    total_score: int = Field(ge=0, le=6)
    price_momentum: int = Field(ge=0, le=2)
    earnings_momentum: int = Field(ge=0, le=2)
    quality_score: int = Field(ge=0, le=2)


def score_momentum(payload: MomentumInput) -> MomentumScore:
    ret_3m = (payload.current_close / payload.close_63d_ago - 1) * 100
    ret_6m = (payload.current_close / payload.close_126d_ago - 1) * 100

    price_score = 0
    if ret_3m > 10:
        price_score += 1
    if ret_6m > 20:
        price_score += 1

    earnings_score = 2 if sum(1 for surprise in payload.earnings_surprises_pct if surprise > 0) >= 3 else 0
    quality_score = 2 if payload.roe > 15 and payload.debt_to_equity < 1.0 and payload.profit_margin > 10 else 0

    return MomentumScore(
        ticker=payload.ticker.upper(),
        total_score=price_score + earnings_score + quality_score,
        price_momentum=price_score,
        earnings_momentum=earnings_score,
        quality_score=quality_score,
    )

