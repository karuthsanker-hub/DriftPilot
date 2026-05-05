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


def trailing_stop(
    unrealized_pct: float,
    peak_unrealized_pct: float,
    activation_pct: float,
    trailing_distance_pct: float,
) -> tuple[bool, str]:
    """Trailing (ratchet) stop. Once peak crosses `activation_pct`, the stop
    becomes (peak − trailing_distance_pct). Fires when current drops below.

    Examples (activation=1%, trailing_distance=2%):
      peak +0.5%, current -0.3% → False (peak below activation)
      peak +3%,   current +0.8% → True  (3% − 2% = 1% stop, current below)
      peak +11%,  current +9.5% → True  (11% − 2% = 9% stop, current below)
      peak +11%,  current +9.1% → False (still above 9% stop)

    `floor_pct` (the original stop_loss_pct) is enforced by evaluate_all so
    the trailing stop never gives back more than the initial stop.
    """
    if peak_unrealized_pct < activation_pct:
        return False, ""
    stop_level = peak_unrealized_pct - trailing_distance_pct
    if unrealized_pct <= stop_level:
        return True, "TRAILING_STOP"
    return False, ""


def evaluate_all(
    now: datetime,
    entry_ts: datetime,
    unrealized_pct: float,
    cfg: EarningsReportConfig,
    peak_unrealized_pct: float = 0.0,
) -> tuple[bool, str]:
    """Precedence:
      1. time_stop          (hard hold limit always wins)
      2. stop_loss          (initial floor — non-negotiable downside cut)
      3. trailing_stop      (only after peak crosses activation)
      4. profit_take        (legacy fixed take — disabled if trailing enabled)
    """
    fired, reason = time_stop(now, entry_ts, cfg.max_hold_minutes)
    if fired:
        return True, reason
    fired, reason = stop_loss(unrealized_pct, cfg.stop_loss_pct)
    if fired:
        return True, reason
    if cfg.trailing_enabled:
        fired, reason = trailing_stop(
            unrealized_pct, peak_unrealized_pct,
            cfg.trailing_activation_pct, cfg.trailing_distance_pct,
        )
        if fired:
            return True, reason
    else:
        fired, reason = profit_take(unrealized_pct, cfg.profit_take_pct)
        if fired:
            return True, reason
    return False, ""


__all__ = ["time_stop", "profit_take", "stop_loss", "trailing_stop", "evaluate_all"]
