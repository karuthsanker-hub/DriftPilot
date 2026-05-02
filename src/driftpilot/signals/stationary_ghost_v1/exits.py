"""Stationary Ghost v1 — exit constants.

This module is documentation/consistency only. The signal does NOT
implement custom `evaluate_exit`. The harness's default TARGET/STOP/TIME
exit logic applies, parameterized by these constants.
"""

from __future__ import annotations

from driftpilot.signals.stationary_ghost_v1.config import (
    MAX_HOLD_MINUTES,
    STOP_PCT,
    TARGET_PCT,
)


def evaluate_default_exit(
    pnl_pct: float,
    minutes_held: int,
    *,
    target_pct: float = TARGET_PCT,
    stop_pct: float = STOP_PCT,
    max_hold_minutes: int = MAX_HOLD_MINUTES,
) -> str | None:
    """Pure helper mirroring the harness's default exit rules.

    Returns one of {"TARGET", "STOP", "TIME", None}. Used by the breakeven /
    exit-math test to verify the constants resolve as the spec requires.
    """
    if pnl_pct >= target_pct:
        return "TARGET"
    if pnl_pct <= -stop_pct:
        return "STOP"
    if minutes_held >= max_hold_minutes:
        return "TIME"
    return None


def breakeven_win_rate(
    target_pct: float,
    stop_pct: float,
    notional: float,
    avg_slippage_per_trade: float,
) -> float:
    """Breakeven win rate accounting for round-trip slippage.

    win_amount_net  = target_pct * notional - avg_slippage_per_trade
    loss_amount_net = stop_pct   * notional + avg_slippage_per_trade
    breakeven       = loss / (win + loss)
    """
    win = target_pct * notional - avg_slippage_per_trade
    loss = stop_pct * notional + avg_slippage_per_trade
    if win <= 0:
        # Strategy cannot be profitable even at 100% win rate after slippage.
        return 1.0
    return loss / (win + loss)


__all__ = [
    "MAX_HOLD_MINUTES",
    "STOP_PCT",
    "TARGET_PCT",
    "breakeven_win_rate",
    "evaluate_default_exit",
]
