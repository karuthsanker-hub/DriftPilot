"""SPY heat tightening: when scanner sets effective_stop_pct = -0.0025 on
all open positions, the per-position evaluator stops out at the new level
(overriding break-even if it was already triggered)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.rs_drift_v1.config import SPY_HEAT_TIGHTENED_STOP_PCT
from driftpilot.signals.rs_drift_v1.exits import evaluate_exit, initial_exit_state


ET = ZoneInfo("America/New_York")


@dataclass
class _FakePosition:
    symbol: str
    entry_at: datetime
    entry_price: float
    metadata: dict[str, Any] = field(default_factory=dict)


def _bar(et_h: int, et_m: int, close: float) -> MinuteBar:
    ts = datetime(2024, 6, 5, et_h, et_m, tzinfo=ET)
    return MinuteBar(symbol="ABC", timestamp=ts, open=close, high=close, low=close, close=close, volume=1000.0)


def _new_position() -> _FakePosition:
    pos = _FakePosition(
        symbol="ABC",
        entry_at=datetime(2024, 6, 5, 10, 5, tzinfo=ET),
        entry_price=100.0,
    )
    pos.metadata.update(initial_exit_state())
    return pos


def test_heat_tightened_stop_constant():
    assert SPY_HEAT_TIGHTENED_STOP_PCT == 0.0025


def test_spy_heat_stop_fires_at_minus_25bp():
    """Scanner sets effective_stop_pct = -0.0025 + spy_heat_triggered_during_position
    flag. Position at -0.30% closes."""
    pos = _new_position()
    # Simulate scanner-side mutation
    pos.metadata["effective_stop_pct"] = -SPY_HEAT_TIGHTENED_STOP_PCT
    pos.metadata["spy_heat_triggered_during_position"] = True

    # +0.10%: above tightened stop, no exit
    decision = evaluate_exit(pos, _bar(11, 0, 100.10), settings=None)
    assert not decision.should_exit

    # -0.30%: below tightened stop → STOP
    decision = evaluate_exit(pos, _bar(11, 1, 99.70), settings=None)
    assert decision.should_exit
    assert decision.exit_reason == "STOP"
    assert decision.metadata["stop_attribution"] == "stop_at_spy_heat"


def test_spy_heat_overrides_break_even_when_tighter():
    """If BE was triggered (effective_stop ~ -0.10%) and SPY heat then
    tightens to -0.25%, scanner replaces effective_stop. BE-triggered flag
    stays True; the heat flag is the dominant attribution."""
    pos = _new_position()
    # Arm BE first
    evaluate_exit(pos, _bar(11, 0, 101.0), settings=None)
    assert pos.metadata["break_even_triggered"] is True

    # Scanner-side: SPY heat fires; effective_stop tightens further
    pos.metadata["effective_stop_pct"] = -SPY_HEAT_TIGHTENED_STOP_PCT
    pos.metadata["spy_heat_triggered_during_position"] = True

    # Pull back to -0.30%
    decision = evaluate_exit(pos, _bar(11, 1, 99.70), settings=None)
    assert decision.should_exit
    assert decision.exit_reason == "STOP"
    # spy_heat is the dominant flag → attribution is heat, not BE
    assert decision.metadata["stop_attribution"] == "stop_at_spy_heat"
