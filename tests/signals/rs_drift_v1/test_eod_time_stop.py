"""EOD time stop fires at 15:55 ET regardless of P&L."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from driftpilot.signals.features import MinuteBar
from driftpilot.signals.rs_drift_v1.config import TIME_STOP_TIME_ET
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


def test_eod_time_stop_constant():
    assert TIME_STOP_TIME_ET == "15:55"


def test_no_exit_before_15_55():
    pos = _new_position()
    decision = evaluate_exit(pos, _bar(15, 54, 100.2), settings=None)
    assert not decision.should_exit


def test_eod_time_fires_at_15_55_flat():
    pos = _new_position()
    decision = evaluate_exit(pos, _bar(15, 55, 100.05), settings=None)
    assert decision.should_exit
    assert decision.exit_reason == "EOD_TIME"


def test_eod_time_fires_after_15_55_modest_profit():
    pos = _new_position()
    decision = evaluate_exit(pos, _bar(15, 58, 100.5), settings=None)
    assert decision.should_exit
    assert decision.exit_reason == "EOD_TIME"


def test_target_at_15_55_takes_precedence():
    """If both target and EOD-time would fire, TARGET branch executes first."""
    pos = _new_position()
    decision = evaluate_exit(pos, _bar(15, 55, 102.0), settings=None)  # +2%
    assert decision.should_exit
    assert decision.exit_reason == "TARGET"
