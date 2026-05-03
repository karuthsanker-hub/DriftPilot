"""Break-even trigger: when peak unrealized P&L crosses +0.75%, the
effective stop tightens to break-even (slippage-aware)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.rs_drift_v1.config import BREAK_EVEN_TRIGGER_PCT
from driftpilot.signals.rs_drift_v1.exits import (
    DEFAULT_SLIPPAGE_COST_PCT,
    evaluate_exit,
    initial_exit_state,
)


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


def test_break_even_not_triggered_below_threshold():
    pos = _new_position()
    # Peak only +0.5%; below +0.75% trigger → BE not yet armed.
    decision = evaluate_exit(pos, _bar(11, 0, 100.5), settings=None)
    assert not decision.should_exit
    assert pos.metadata["break_even_triggered"] is False
    assert pos.metadata["effective_stop_pct"] == -0.0075  # initial


def test_break_even_arms_when_peak_crosses_trigger():
    pos = _new_position()
    # Push peak to +1.0% (well above 0.75% trigger but below TARGET 1.5%).
    evaluate_exit(pos, _bar(11, 0, 101.0), settings=None)
    assert pos.metadata["break_even_triggered"] is True
    # Effective stop tightened from -0.75% to -DEFAULT_SLIPPAGE_COST_PCT (-0.10%).
    assert pos.metadata["effective_stop_pct"] == -DEFAULT_SLIPPAGE_COST_PCT


def test_break_even_stop_fires_at_be_after_pullback():
    pos = _new_position()
    evaluate_exit(pos, _bar(11, 0, 101.0), settings=None)  # arm BE
    # Pull back to -0.05%: above BE stop (-0.10%) → no exit.
    decision = evaluate_exit(pos, _bar(11, 1, 99.95), settings=None)
    assert not decision.should_exit
    # Pull back further to -0.15%: hits BE stop.
    decision = evaluate_exit(pos, _bar(11, 2, 99.85), settings=None)
    assert decision.should_exit
    assert decision.exit_reason == "STOP"
    assert decision.metadata["stop_attribution"] == "stop_at_break_even"


def test_initial_stop_attribution_when_be_not_armed():
    pos = _new_position()
    # Drop straight to -0.8% without ever breaching the BE trigger.
    decision = evaluate_exit(pos, _bar(11, 0, 99.2), settings=None)
    assert decision.should_exit
    assert decision.exit_reason == "STOP"
    assert decision.metadata["stop_attribution"] == "stop_at_initial_level"


def test_target_overrides_break_even():
    pos = _new_position()
    decision = evaluate_exit(pos, _bar(11, 0, 101.6), settings=None)  # +1.6%
    assert decision.should_exit
    assert decision.exit_reason == "TARGET"


def test_break_even_threshold_constant():
    assert BREAK_EVEN_TRIGGER_PCT == 0.0075
