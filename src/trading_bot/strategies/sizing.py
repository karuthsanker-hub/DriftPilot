from __future__ import annotations

from pydantic import BaseModel, Field


class PositionSize(BaseModel):
    shares: int = Field(ge=0)
    stop_price: float
    risk_dollars: float
    position_value: float


def calculate_position_size(
    portfolio_value: float,
    entry_price: float,
    atr_value: float,
    *,
    atr_multiplier: float = 2.0,
    risk_pct: float = 0.01,
    max_position_pct: float = 0.20,
) -> PositionSize:
    if portfolio_value <= 0:
        raise ValueError("portfolio_value must be positive")
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if atr_value <= 0:
        raise ValueError("atr_value must be positive")

    risk_dollars = portfolio_value * risk_pct
    stop_distance = atr_value * atr_multiplier
    shares = int(risk_dollars / stop_distance)
    max_dollars = portfolio_value * max_position_pct
    if shares * entry_price > max_dollars:
        shares = int(max_dollars / entry_price)
    return PositionSize(
        shares=max(0, shares),
        stop_price=entry_price - stop_distance,
        risk_dollars=risk_dollars,
        position_value=max(0, shares) * entry_price,
    )

