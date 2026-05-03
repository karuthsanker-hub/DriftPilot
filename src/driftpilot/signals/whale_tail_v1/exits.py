"""Whale-Tail v1 - custom exit logic.

ATR-scaled target (1.5x ATR) and stop (0.75x ATR), 60-minute time stop, and
distribution-break invalidation if `compression_low_at_entry` is in
`position.metadata`. The harness calls
`signal.evaluate_exit(position, latest_bar, settings)` first; if we return
`should_exit=True`, that wins. Otherwise the harness's default rules apply.

`position.metadata` is a mutable dict the harness initializes empty. We use it
as scratch state across bars — `peak_unrealized_pct` for trailing analysis,
plus optional `atr_at_entry` and `compression_low_at_entry` if the harness
populated them.
"""

from __future__ import annotations

from typing import Any

from driftpilot.signals.base import ExitDecision
from driftpilot.signals.features import MinuteBar


def evaluate_exit(
    position: Any,
    latest_bar: MinuteBar,
    settings: Any,
    *,
    target_atr_mult: float,
    stop_atr_mult: float,
    time_stop_minutes: int,
) -> ExitDecision:
    metadata: dict[str, Any] = position.metadata
    entry_price = float(position.entry_price)
    if entry_price <= 0:
        return ExitDecision(should_exit=False, exit_reason=None, metadata=dict(metadata))

    current_unrealized_pct = (latest_bar.close - entry_price) / entry_price

    atr_at_entry = metadata.get("atr_at_entry")
    if atr_at_entry is not None and atr_at_entry > 0:
        target_pct = target_atr_mult * (float(atr_at_entry) / entry_price)
        stop_pct = stop_atr_mult * (float(atr_at_entry) / entry_price)
    else:
        # Fall back to settings if ATR was not captured at entry.
        target_pct = float(getattr(settings, "target_pct", 0.0))
        stop_pct = float(getattr(settings, "stop_pct", 0.0))

    # 1) TARGET
    if target_pct > 0 and current_unrealized_pct >= target_pct:
        return ExitDecision(
            should_exit=True,
            exit_reason="TARGET",
            metadata=dict(metadata),
        )

    # 2) STOP
    if stop_pct > 0 and current_unrealized_pct <= -stop_pct:
        return ExitDecision(
            should_exit=True,
            exit_reason="STOP",
            metadata=dict(metadata),
        )

    # 3) TIME stop
    elapsed_minutes = (latest_bar.timestamp - position.entry_at).total_seconds() / 60.0
    if elapsed_minutes >= time_stop_minutes:
        return ExitDecision(
            should_exit=True,
            exit_reason="TIME",
            metadata=dict(metadata),
        )

    # 4) DISTRIBUTION_BREAK
    comp_low_at_entry = metadata.get("compression_low_at_entry")
    if comp_low_at_entry is not None and latest_bar.close < float(comp_low_at_entry):
        return ExitDecision(
            should_exit=True,
            exit_reason="DISTRIBUTION_BREAK",
            metadata=dict(metadata),
        )

    # Track peak unrealized P&L in metadata.
    peak = float(metadata.get("peak_unrealized_pct", 0.0))
    new_peak = max(peak, current_unrealized_pct)
    metadata["peak_unrealized_pct"] = new_peak

    return ExitDecision(
        should_exit=False,
        exit_reason=None,
        metadata={"peak_unrealized_pct": new_peak},
    )


def breakeven_win_rate(
    target_pct: float,
    stop_pct: float,
    notional: float,
    avg_slippage_per_trade: float,
) -> float:
    """Breakeven win rate given target/stop in percent of notional and round-trip slippage."""
    win = target_pct * notional - avg_slippage_per_trade
    loss = stop_pct * notional + avg_slippage_per_trade
    if win <= 0:
        return 1.0
    return loss / (win + loss)


__all__ = ["evaluate_exit", "breakeven_win_rate"]
