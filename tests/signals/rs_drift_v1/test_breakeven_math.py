"""Break-even math: 2:1 R:R with break-even trigger.

target=1.5%, stop=0.75% → naive R:R = 2.0. With $0.001 round-trip slippage
(0.10%), net win = 1.5% - 0.10% = 1.40%; net loss = 0.75% + 0.10% = 0.85%.
Net R:R ≈ 1.65. Breakeven win rate (after slippage) = loss / (loss + win)
= 0.85 / (0.85 + 1.40) ≈ 37.78%.

After break-even trigger fires (peak >= +0.75%), the stop tightens to
roughly the slippage cost, dramatically reducing average loser size on
trades that achieved BE before reversing.
"""

from __future__ import annotations

import pytest

from driftpilot.signals.rs_drift_v1.config import (
    BREAK_EVEN_TRIGGER_PCT,
    STOP_PCT,
    TARGET_PCT,
)
from driftpilot.signals.rs_drift_v1.exits import DEFAULT_SLIPPAGE_COST_PCT


def test_locked_constants_match_spec():
    assert TARGET_PCT == 0.015
    assert STOP_PCT == 0.0075
    assert BREAK_EVEN_TRIGGER_PCT == 0.0075
    assert DEFAULT_SLIPPAGE_COST_PCT == 0.001


def test_naive_rr_is_two_to_one():
    naive_rr = TARGET_PCT / STOP_PCT
    assert naive_rr == pytest.approx(2.0, abs=1e-9)


def test_breakeven_win_rate_after_slippage():
    net_win = TARGET_PCT - DEFAULT_SLIPPAGE_COST_PCT
    net_loss = STOP_PCT + DEFAULT_SLIPPAGE_COST_PCT
    breakeven_wr = net_loss / (net_loss + net_win)
    # 0.85 / (0.85 + 1.40) ≈ 0.3778
    assert breakeven_wr == pytest.approx(0.3778, abs=1e-3)


def test_break_even_trigger_threshold_matches_stop():
    """In the spec, the BE trigger fires at the same percentage as the
    initial stop — symmetric: the trade has to gain as much as it could lose
    before earning the asymmetric exit."""
    assert BREAK_EVEN_TRIGGER_PCT == STOP_PCT
