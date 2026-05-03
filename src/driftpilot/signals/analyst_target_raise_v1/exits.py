"""Exit logic for analyst_target_raise_v1.

Pure functions. Precedence (checked in this order):
  1. time stop  — now - entry_ts >= max_hold_minutes
  2. profit take — unrealized_pct >= profit_take_pct
  3. stop loss   — unrealized_pct <= -stop_loss_pct

`evaluate_all` returns the first matching ExitDecision, or a
non-exiting ExitDecision if none of the branches fire.

The 60-minute time stop is load-bearing: the underlying horizon study
shows the (analyst, target_raise) edge fades from 1.42x at 60m to 0.97x
by 1day, so we MUST exit before the edge evaporates.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from driftpilot.signals.base import ExitDecision

from driftpilot.signals.analyst_target_raise_v1.config import AnalystTargetRaiseConfig


def _ensure_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def time_stop(
    now: datetime,
    entry_ts: datetime,
    cfg: AnalystTargetRaiseConfig,
) -> ExitDecision:
    """Exit when held longer than `cfg.max_hold_minutes`."""

    now = _ensure_aware(now)
    entry_ts = _ensure_aware(entry_ts)
    held = now - entry_ts
    if held >= timedelta(minutes=cfg.max_hold_minutes):
        return ExitDecision(
            should_exit=True,
            exit_reason="TIME_STOP",
            metadata={"held_minutes": held.total_seconds() / 60.0},
        )
    return ExitDecision(should_exit=False)


def profit_take(
    unrealized_pct: float,
    cfg: AnalystTargetRaiseConfig,
) -> ExitDecision:
    """Exit when unrealized P&L (in percent) >= `cfg.profit_take_pct`."""

    if unrealized_pct >= cfg.profit_take_pct:
        return ExitDecision(
            should_exit=True,
            exit_reason="PROFIT_TAKE",
            metadata={"unrealized_pct": unrealized_pct},
        )
    return ExitDecision(should_exit=False)


def stop_loss(
    unrealized_pct: float,
    cfg: AnalystTargetRaiseConfig,
) -> ExitDecision:
    """Exit when unrealized P&L (in percent) <= -`cfg.stop_loss_pct`."""

    if unrealized_pct <= -cfg.stop_loss_pct:
        return ExitDecision(
            should_exit=True,
            exit_reason="STOP_LOSS",
            metadata={"unrealized_pct": unrealized_pct},
        )
    return ExitDecision(should_exit=False)


def evaluate_all(
    now: datetime,
    entry_ts: datetime,
    unrealized_pct: float,
    cfg: AnalystTargetRaiseConfig,
) -> ExitDecision:
    """Apply exits in precedence order: time > profit > stop."""

    decision = time_stop(now, entry_ts, cfg)
    if decision.should_exit:
        return decision
    decision = profit_take(unrealized_pct, cfg)
    if decision.should_exit:
        return decision
    decision = stop_loss(unrealized_pct, cfg)
    if decision.should_exit:
        return decision
    return ExitDecision(should_exit=False)


__all__ = ["time_stop", "profit_take", "stop_loss", "evaluate_all"]
