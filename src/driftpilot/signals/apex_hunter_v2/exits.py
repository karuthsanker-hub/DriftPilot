"""Apex Hunter v2.2 — three-stage Ratchet exit.

Per-position state lives in `position.metadata`. The exit function is called
once per fresh bar via the replay harness's `evaluate_exit` hook. The trailing
stop ONLY MOVES UP — never relaxes — even when ATR-derived recomputation
suggests a lower stop (e.g. on a pullback after a stage transition).

Stage transitions are one-way:
    Stage 1 (2.0× ATR) → Stage 2 (1.0× ATR) when peak_unrealized_pct ≥ 1%
    Stage 2 (1.0× ATR) → Stage 3 (0.5× ATR) when peak_unrealized_pct ≥ 2%
                                              OR ET time ≥ 15:00
After 15:45 ET → HARD_EXIT regardless of stage.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from driftpilot.clock import require_aware
from driftpilot.signals.apex_hunter_v2.config import (
    HARD_EXIT_TIME_ET,
    RATCHET_STAGE_1_ATR_MULT,
    RATCHET_STAGE_2_ATR_MULT,
    RATCHET_STAGE_2_TRIGGER_PCT,
    RATCHET_STAGE_3_ATR_MULT,
    RATCHET_STAGE_3_TRIGGER_PCT,
    RATCHET_STAGE_3_TRIGGER_TIME_ET,
)
from driftpilot.signals.base import ExitDecision
from driftpilot.signals.features import MinuteBar


_ET = ZoneInfo("America/New_York")


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


_HARD_EXIT_T = _parse_hhmm(HARD_EXIT_TIME_ET)
_STAGE_3_TIME_T = _parse_hhmm(RATCHET_STAGE_3_TRIGGER_TIME_ET)


def _et_time(timestamp: datetime) -> time:
    return require_aware(timestamp).astimezone(_ET).time()


def evaluate_exit(position: Any, latest_bar: MinuteBar, settings: Any) -> ExitDecision:
    """Three-stage Ratchet exit decision.

    Mutates `position.metadata` in place to persist ratchet state across bars.

    Branches in order:
      1. Hard time exit at/after 15:45 ET.
      2. Update peak price.
      3. Recompute candidate trailing stop and apply ratchet rule (only-up).
      4. Stop hit ⇒ exit RATCHET_STOP.
      5. Update peak unrealized %.
      6. Stage transitions (one-way), then re-apply ratchet rule with new mult.
      7. Persist + return no-exit.
    """
    md: dict[str, Any] = position.metadata
    entry_price = float(position.entry_price)
    close = float(latest_bar.close)

    # First-call initialization. atr_at_entry must be set by the entry hook;
    # if missing, fall back to 1% of entry price (documented in KNOWN_RISKS).
    if "ratchet_stage" not in md:
        atr_at_entry = float(md.get("atr_at_entry", 0.0))
        if atr_at_entry <= 0:
            atr_at_entry = 0.01 * entry_price
            md["atr_at_entry"] = atr_at_entry
        md["ratchet_stage"] = 1
        md["current_atr_mult"] = RATCHET_STAGE_1_ATR_MULT
        md["peak_price"] = entry_price
        md["peak_unrealized_pct"] = 0.0
        md["trailing_stop_price"] = (
            entry_price - RATCHET_STAGE_1_ATR_MULT * atr_at_entry
        )

    atr_at_entry = float(md["atr_at_entry"])

    # 1) Hard time exit.
    if _et_time(latest_bar.timestamp) >= _HARD_EXIT_T:
        return ExitDecision(
            should_exit=True,
            exit_reason="HARD_EXIT",
            metadata={
                "final_ratchet_stage": int(md["ratchet_stage"]),
                "trailing_stop_price": float(md["trailing_stop_price"]),
            },
        )

    # 2) Update peak price.
    md["peak_price"] = max(float(md["peak_price"]), close)

    # 3) Recompute trailing stop, apply only-up ratchet rule.
    candidate_stop = float(md["peak_price"]) - float(md["current_atr_mult"]) * atr_at_entry
    md["trailing_stop_price"] = max(float(md["trailing_stop_price"]), candidate_stop)

    # 4) Stop hit?
    if close <= float(md["trailing_stop_price"]):
        return ExitDecision(
            should_exit=True,
            exit_reason="RATCHET_STOP",
            metadata={
                "final_ratchet_stage": int(md["ratchet_stage"]),
                "trailing_stop_price": float(md["trailing_stop_price"]),
                "peak_price": float(md["peak_price"]),
                "peak_unrealized_pct": float(md["peak_unrealized_pct"]),
            },
        )

    # 5) Update peak unrealized %.
    if entry_price > 0:
        current_unrealized_pct = (close - entry_price) / entry_price
    else:
        current_unrealized_pct = 0.0
    md["peak_unrealized_pct"] = max(
        float(md["peak_unrealized_pct"]), current_unrealized_pct
    )

    # 6) Stage transitions (one-way; never revert).
    et_now = _et_time(latest_bar.timestamp)
    if (
        md["ratchet_stage"] == 1
        and float(md["peak_unrealized_pct"]) >= RATCHET_STAGE_2_TRIGGER_PCT
    ):
        md["ratchet_stage"] = 2
        md["current_atr_mult"] = RATCHET_STAGE_2_ATR_MULT
        new_stop = float(md["peak_price"]) - RATCHET_STAGE_2_ATR_MULT * atr_at_entry
        md["trailing_stop_price"] = max(float(md["trailing_stop_price"]), new_stop)

    if md["ratchet_stage"] == 2 and (
        float(md["peak_unrealized_pct"]) >= RATCHET_STAGE_3_TRIGGER_PCT
        or et_now >= _STAGE_3_TIME_T
    ):
        md["ratchet_stage"] = 3
        md["current_atr_mult"] = RATCHET_STAGE_3_ATR_MULT
        new_stop = float(md["peak_price"]) - RATCHET_STAGE_3_ATR_MULT * atr_at_entry
        md["trailing_stop_price"] = max(float(md["trailing_stop_price"]), new_stop)

    return ExitDecision(
        should_exit=False,
        exit_reason=None,
        metadata={
            "ratchet_stage": int(md["ratchet_stage"]),
            "trailing_stop_price": float(md["trailing_stop_price"]),
            "peak_price": float(md["peak_price"]),
            "peak_unrealized_pct": float(md["peak_unrealized_pct"]),
        },
    )


__all__ = ["evaluate_exit"]
