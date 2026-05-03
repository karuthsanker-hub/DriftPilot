"""Pure exit-condition helpers for Earnings Report v1.

Each helper returns (should_close, reason). `evaluate_all` applies the spec
precedence: time stop > profit take > stop loss when all three trigger on
the same bar.
"""

from __future__ import annotations

from datetime import datetime

from driftpilot.signals.earnings_report_v1.config import EarningsReportConfig


def time_stop(
    now: datetime, entry_ts: datetime, max_hold_minutes: int
) -> tuple[bool, str]:
    elapsed = (now - entry_ts).total_seconds() / 60.0
    if elapsed >= max_hold_minutes:
        return True, "TIME_STOP"
    return False, ""


def profit_take(unrealized_pct: float, profit_take_pct: float) -> tuple[bool, str]:
    if unrealized_pct >= profit_take_pct:
        return True, "PROFIT_TAKE"
    return False, ""


def stop_loss(unrealized_pct: float, stop_loss_pct: float) -> tuple[bool, str]:
    if unrealized_pct <= -abs(stop_loss_pct):
        return True, "STOP_LOSS"
    return False, ""


def evaluate_all(
    now: datetime,
    entry_ts: datetime,
    unrealized_pct: float,
    cfg: EarningsReportConfig,
) -> tuple[bool, str]:
    # Precedence: time stop > profit take > stop loss
    fired, reason = time_stop(now, entry_ts, cfg.max_hold_minutes)
    if fired:
        return True, reason
    fired, reason = profit_take(unrealized_pct, cfg.profit_take_pct)
    if fired:
        return True, reason
    fired, reason = stop_loss(unrealized_pct, cfg.stop_loss_pct)
    if fired:
        return True, reason
    return False, ""


__all__ = ["time_stop", "profit_take", "stop_loss", "evaluate_all"]
