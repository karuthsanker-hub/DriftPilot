from __future__ import annotations

from pydantic import BaseModel


class PauseDecision(BaseModel):
    paused: bool
    reason: str = ""


def evaluate_daily_pause(
    *,
    trading_active: bool,
    vix: float | None,
    daily_pnl_pct: float,
    spy_premarket_change_pct: float | None,
    vix_threshold: float = 25.0,
    daily_loss_limit_pct: float = -2.0,
    spy_premarket_pause_pct: float = -1.5,
) -> PauseDecision:
    if not trading_active:
        return PauseDecision(paused=True, reason="kill switch inactive")
    if vix is not None and vix > vix_threshold:
        return PauseDecision(paused=True, reason="VIX above threshold")
    if daily_pnl_pct <= daily_loss_limit_pct:
        return PauseDecision(paused=True, reason="daily loss limit breached")
    if spy_premarket_change_pct is not None and spy_premarket_change_pct < spy_premarket_pause_pct:
        return PauseDecision(paused=True, reason="SPY premarket drop below threshold")
    return PauseDecision(paused=False)

