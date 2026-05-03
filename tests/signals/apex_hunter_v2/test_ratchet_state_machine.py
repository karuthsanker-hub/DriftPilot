"""Three-stage Ratchet state machine.

Position opens at $100 with ATR=$1.50 (so initial Stage 1 stop = $97).
Walk through: rally to $102 (Stage 1 ratchets), +1.1% triggers Stage 2,
+2.1% triggers Stage 3, time-based forced Stage 3, hard exit at 15:45.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from driftpilot.signals.apex_hunter_v2.exits import evaluate_exit
from driftpilot.signals.features import MinuteBar


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


def _new_position(*, atr_at_entry: float = 1.5) -> _FakePosition:
    return _FakePosition(
        symbol="ABC",
        entry_at=datetime(2024, 6, 5, 10, 30, tzinfo=ET),
        entry_price=100.0,
        metadata={"atr_at_entry": atr_at_entry},
    )


def test_initial_stop_at_stage_1_two_atr_below_entry():
    pos = _new_position(atr_at_entry=1.5)
    decision = evaluate_exit(pos, _bar(10, 31, 100.0), settings=None)
    assert not decision.should_exit
    assert pos.metadata["ratchet_stage"] == 1
    # Initial trailing stop = 100 - 2.0 * 1.5 = 97
    assert pos.metadata["trailing_stop_price"] == pytest.approx(97.0, abs=1e-9)


def test_rally_ratchets_stop_up_within_stage_1():
    """+0.5% rally stays in Stage 1 (Stage 2 trigger is +1.0%). The
    trailing stop should still ratchet upward from the initial 97."""
    pos = _new_position(atr_at_entry=1.5)
    evaluate_exit(pos, _bar(10, 31, 100.0), settings=None)
    evaluate_exit(pos, _bar(10, 32, 100.5), settings=None)
    # peak_price = 100.5 → candidate_stop = 100.5 - 3.0 = 97.5
    # trailing = max(97, 97.5) = 97.5; stage still 1
    assert pos.metadata["ratchet_stage"] == 1
    assert pos.metadata["trailing_stop_price"] == pytest.approx(97.5, abs=1e-9)


def test_one_pct_profit_triggers_stage_2():
    pos = _new_position(atr_at_entry=1.5)
    evaluate_exit(pos, _bar(10, 31, 101.0), settings=None)  # +1.0% peak
    assert pos.metadata["ratchet_stage"] == 2
    assert pos.metadata["current_atr_mult"] == 1.0
    # Stage 2: stop = peak_price (101) - 1.0 * 1.5 = 99.5; new stop should
    # only update if higher than prior. Prior trailing = max(97, 101-3=98) = 98.
    # New stop = max(98, 99.5) = 99.5
    assert pos.metadata["trailing_stop_price"] == pytest.approx(99.5, abs=1e-9)


def test_two_pct_profit_triggers_stage_3():
    pos = _new_position(atr_at_entry=1.5)
    evaluate_exit(pos, _bar(10, 31, 101.0), settings=None)  # → Stage 2
    evaluate_exit(pos, _bar(10, 32, 102.5), settings=None)  # +2.5% → Stage 3
    assert pos.metadata["ratchet_stage"] == 3
    assert pos.metadata["current_atr_mult"] == 0.5
    # Stage 3: stop = 102.5 - 0.75 = 101.75. Must be max of prior stop.
    assert pos.metadata["trailing_stop_price"] >= 101.75 - 1e-9


def test_time_15_00_forces_stage_3_even_without_profit_trigger():
    """Even at modest +0.8% profit, after 15:00 ET stage 2 → 3 forces."""
    pos = _new_position(atr_at_entry=1.5)
    evaluate_exit(pos, _bar(10, 31, 101.0), settings=None)  # +1.0% → Stage 2
    evaluate_exit(pos, _bar(15, 0, 100.8), settings=None)  # 15:00 forces Stage 3
    assert pos.metadata["ratchet_stage"] == 3
    assert pos.metadata["current_atr_mult"] == 0.5


def test_hard_exit_at_15_45():
    pos = _new_position(atr_at_entry=1.5)
    evaluate_exit(pos, _bar(10, 31, 101.0), settings=None)
    decision = evaluate_exit(pos, _bar(15, 45, 100.5), settings=None)
    assert decision.should_exit
    assert decision.exit_reason == "HARD_EXIT"


def test_ratchet_stop_fires_when_close_breaches():
    pos = _new_position(atr_at_entry=1.5)
    evaluate_exit(pos, _bar(10, 31, 101.0), settings=None)  # → Stage 2, stop ~99.5
    decision = evaluate_exit(pos, _bar(10, 32, 99.4), settings=None)
    assert decision.should_exit
    assert decision.exit_reason == "RATCHET_STOP"
    assert decision.metadata["final_ratchet_stage"] == 2


# pytest.approx is needed
import pytest  # noqa: E402
