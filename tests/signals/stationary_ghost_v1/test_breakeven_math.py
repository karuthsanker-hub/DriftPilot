"""Breakeven win-rate math + default exit logic helpers."""

from __future__ import annotations

import pytest

from driftpilot.signals.stationary_ghost_v1.config import (
    MAX_HOLD_MINUTES,
    STOP_PCT,
    TARGET_PCT,
)
from driftpilot.signals.stationary_ghost_v1.exits import (
    breakeven_win_rate,
    evaluate_default_exit,
)


def test_breakeven_with_documented_slippage():
    # Per spec: $1.91 average slippage / trade at $1000 notional gives
    # win=$5.59, loss=$16.91, breakeven ~ 75.2%.
    rate = breakeven_win_rate(
        target_pct=TARGET_PCT,
        stop_pct=STOP_PCT,
        notional=1000.0,
        avg_slippage_per_trade=1.91,
    )
    assert rate == pytest.approx(16.91 / (5.59 + 16.91), rel=1e-6)
    assert 0.75 <= rate <= 0.76


def test_breakeven_zero_slippage():
    rate = breakeven_win_rate(
        target_pct=TARGET_PCT,
        stop_pct=STOP_PCT,
        notional=1000.0,
        avg_slippage_per_trade=0.0,
    )
    # win=$7.50, loss=$15.00 => 15/22.5 = 0.6667
    assert rate == pytest.approx(2.0 / 3.0, rel=1e-6)


def test_default_exit_target():
    assert evaluate_default_exit(pnl_pct=0.008, minutes_held=5) == "TARGET"


def test_default_exit_stop():
    assert evaluate_default_exit(pnl_pct=-0.016, minutes_held=5) == "STOP"


def test_default_exit_time():
    assert evaluate_default_exit(pnl_pct=0.002, minutes_held=20) == "TIME"


def test_default_exit_no_exit():
    assert evaluate_default_exit(pnl_pct=-0.005, minutes_held=19) is None


def test_default_exit_threshold_constants():
    assert TARGET_PCT == pytest.approx(0.0075)
    assert STOP_PCT == pytest.approx(0.015)
    assert MAX_HOLD_MINUTES == 20
