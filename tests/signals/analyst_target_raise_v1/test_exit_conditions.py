"""Exit-branch and precedence tests for analyst_target_raise_v1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from driftpilot.signals.analyst_target_raise_v1.config import (
    AnalystTargetRaiseConfig,
)
from driftpilot.signals.analyst_target_raise_v1.exits import (
    evaluate_all,
    profit_take,
    stop_loss,
    time_stop,
)


CFG = AnalystTargetRaiseConfig()
T0 = datetime(2024, 6, 3, 14, 30, tzinfo=timezone.utc)


def test_time_stop_fires_after_max_hold() -> None:
    now = T0 + timedelta(minutes=CFG.max_hold_minutes)
    decision = time_stop(now, T0, CFG)
    assert decision.should_exit is True
    assert decision.exit_reason == "TIME_STOP"


def test_time_stop_does_not_fire_before_max_hold() -> None:
    now = T0 + timedelta(minutes=CFG.max_hold_minutes - 1)
    decision = time_stop(now, T0, CFG)
    assert decision.should_exit is False


def test_profit_take_fires_at_target() -> None:
    decision = profit_take(CFG.profit_take_pct, CFG)
    assert decision.should_exit is True
    assert decision.exit_reason == "PROFIT_TAKE"


def test_profit_take_does_not_fire_below_target() -> None:
    decision = profit_take(CFG.profit_take_pct - 0.01, CFG)
    assert decision.should_exit is False


def test_stop_loss_fires_at_threshold() -> None:
    decision = stop_loss(-CFG.stop_loss_pct, CFG)
    assert decision.should_exit is True
    assert decision.exit_reason == "STOP_LOSS"


def test_stop_loss_does_not_fire_above_threshold() -> None:
    decision = stop_loss(-CFG.stop_loss_pct + 0.01, CFG)
    assert decision.should_exit is False


def test_precedence_time_stop_beats_profit_take_and_stop_loss() -> None:
    # All three would fire individually — time_stop must win.
    now = T0 + timedelta(minutes=CFG.max_hold_minutes + 5)
    # Use a P&L value that would trigger profit_take if it ran.
    decision_pt_concurrent = evaluate_all(now, T0, CFG.profit_take_pct + 1.0, CFG)
    assert decision_pt_concurrent.should_exit is True
    assert decision_pt_concurrent.exit_reason == "TIME_STOP"

    decision_sl_concurrent = evaluate_all(now, T0, -CFG.stop_loss_pct - 1.0, CFG)
    assert decision_sl_concurrent.should_exit is True
    assert decision_sl_concurrent.exit_reason == "TIME_STOP"


def test_precedence_profit_take_beats_stop_loss_when_no_time_stop() -> None:
    # Within hold window. unrealized_pct >= profit_take_pct AND
    # <= -stop_loss_pct can't both hold simultaneously, so to verify
    # the *order* we feed exactly the profit_take threshold.
    now = T0 + timedelta(minutes=1)
    decision = evaluate_all(now, T0, CFG.profit_take_pct, CFG)
    assert decision.should_exit is True
    assert decision.exit_reason == "PROFIT_TAKE"


def test_evaluate_all_no_exit_when_within_bounds() -> None:
    now = T0 + timedelta(minutes=10)
    decision = evaluate_all(now, T0, 0.1, CFG)
    assert decision.should_exit is False
