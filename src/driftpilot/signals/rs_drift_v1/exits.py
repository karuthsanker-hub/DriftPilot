"""Custom exit logic for RS-Drift v1.

The harness calls `evaluate_exit(position, latest_bar, settings)` once per
fresh bar. We mutate `position.metadata` to track per-position scratch state
across bars (peak P&L, break-even trigger flag, effective stop, SPY-heat).

Exit branches in evaluation order (per spec § RSD-Phase 3):
  1. unrealized P&L >= +1.5% → TARGET
  2. unrealized P&L <= effective_stop_pct → STOP
  3. clock time (latest_bar in ET) >= 15:55 → EOD_TIME
  4. update peak_unrealized_pct; if it crosses +0.75% and break_even has not
     fired yet, set effective_stop_pct = entry_slippage_cost_pct (the
     break-even net of slippage, not zero) and mark BE triggered.

The SPY-heat sensor sets effective_stop_pct on all OPEN positions to
-0.0025 (overriding break-even). It runs in the scanner loop, not here;
this evaluator just respects whatever effective_stop_pct is in the metadata.

Daily circuit breakers (+$125 cap, -$100 limit) are state-machine concerns;
the signal's `scan(...)` returns empty when daily_realized_pnl >= +$125.
"""

from __future__ import annotations

from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from driftpilot.clock import require_aware
from driftpilot.signals.base import ExitDecision
from driftpilot.signals.features import MinuteBar
from driftpilot.signals.rs_drift_v1.config import (
    BREAK_EVEN_TRIGGER_PCT,
    STOP_PCT,
    TARGET_PCT,
    TIME_STOP_TIME_ET,
)


ET = ZoneInfo("America/New_York")


def _et_time(bar: MinuteBar) -> time:
    return require_aware(bar.timestamp).astimezone(ET).time()


def _parse_hhmm(s: str) -> time:
    hours, minutes = s.split(":")
    return time(int(hours), int(minutes))


# Slippage cost as a percent of entry price for a $1000-ish notional trade.
# `max($0.02, 0.0005 * price)` per share. Two-sided (entry + exit) ≈
# 2 * 0.0005 = 0.10% in the worst case at typical prices. We model 0.10% as
# the break-even floor; spec says "break-even net of slippage, not zero".
DEFAULT_SLIPPAGE_COST_PCT = 0.001  # 0.10% round-trip


def initial_exit_state(*, atr_at_entry: float | None = None) -> dict[str, Any]:
    """Seed metadata at entry. Callers should merge into position.metadata."""
    return {
        "break_even_triggered": False,
        "effective_stop_pct": -STOP_PCT,
        "peak_unrealized_pct": 0.0,
        "spy_heat_triggered_during_position": False,
        "atr_at_entry": atr_at_entry,
    }


def evaluate_exit(position: Any, latest_bar: MinuteBar, settings: Any) -> ExitDecision:
    """RS-Drift exit evaluator. Mutates position.metadata.

    `position` must expose: entry_at, entry_price, metadata (dict).
    `settings` is forwarded by the harness; not consulted here directly,
    parameters come from RS-Drift's own config.
    """
    metadata = position.metadata
    if "effective_stop_pct" not in metadata:
        metadata.update(initial_exit_state())

    entry_price = float(position.entry_price)
    if entry_price <= 0:
        return ExitDecision(should_exit=False)

    current_unrealized_pct = (latest_bar.close - entry_price) / entry_price

    # Branch 1: TARGET
    if current_unrealized_pct >= TARGET_PCT:
        return ExitDecision(
            should_exit=True,
            exit_reason="TARGET",
            metadata={"unrealized_pct": current_unrealized_pct},
        )

    # Branch 2: STOP (uses effective_stop_pct — possibly tightened by SPY-heat
    # or break-even-trigger or initial -0.75%)
    effective_stop_pct = float(metadata["effective_stop_pct"])
    if current_unrealized_pct <= effective_stop_pct:
        reason = "STOP"
        # Distinguish in metadata for the report (stop_attribution block)
        if metadata.get("spy_heat_triggered_during_position"):
            stop_attribution = "stop_at_spy_heat"
        elif metadata.get("break_even_triggered"):
            stop_attribution = "stop_at_break_even"
        else:
            stop_attribution = "stop_at_initial_level"
        return ExitDecision(
            should_exit=True,
            exit_reason=reason,
            metadata={
                "unrealized_pct": current_unrealized_pct,
                "stop_attribution": stop_attribution,
            },
        )

    # Branch 3: EOD time stop
    eod_t = _parse_hhmm(TIME_STOP_TIME_ET)
    if _et_time(latest_bar) >= eod_t:
        return ExitDecision(
            should_exit=True,
            exit_reason="EOD_TIME",
            metadata={"unrealized_pct": current_unrealized_pct},
        )

    # Branch 4: update peak; trigger break-even if peak >= +0.75% and not yet triggered
    peak = max(metadata.get("peak_unrealized_pct", 0.0), current_unrealized_pct)
    metadata["peak_unrealized_pct"] = peak

    if peak >= BREAK_EVEN_TRIGGER_PCT and not metadata.get("break_even_triggered"):
        metadata["break_even_triggered"] = True
        # SPY-heat may have already tightened the stop further — never relax it.
        new_be_stop = -DEFAULT_SLIPPAGE_COST_PCT
        if new_be_stop > metadata["effective_stop_pct"]:
            metadata["effective_stop_pct"] = new_be_stop

    return ExitDecision(should_exit=False, metadata=dict(metadata))


__all__ = ["evaluate_exit", "initial_exit_state", "DEFAULT_SLIPPAGE_COST_PCT"]
