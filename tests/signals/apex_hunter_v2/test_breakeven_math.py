"""Verify locked Apex Hunter constants and break-even framing.

Apex's Ratchet exit makes analytic break-even non-trivial because realized
winner/loser distributions depend on which Stage triggered the exit.
This test pins the locked constants so a typo in config.py is caught.
"""

from __future__ import annotations

from driftpilot.signals.apex_hunter_v2.config import (
    HALF_LIFE_MINS,
    RATCHET_STAGE_1_ATR_MULT,
    RATCHET_STAGE_2_ATR_MULT,
    RATCHET_STAGE_2_TRIGGER_PCT,
    RATCHET_STAGE_3_ATR_MULT,
    RATCHET_STAGE_3_TRIGGER_PCT,
    SECTOR_CAP,
    WINDOW_MINS,
)


def test_locked_window_and_halflife():
    assert WINDOW_MINS == 90
    assert HALF_LIFE_MINS == 15


def test_locked_ratchet_atr_multiples():
    assert RATCHET_STAGE_1_ATR_MULT == 2.0
    assert RATCHET_STAGE_2_ATR_MULT == 1.0
    assert RATCHET_STAGE_3_ATR_MULT == 0.5


def test_locked_stage_triggers():
    assert RATCHET_STAGE_2_TRIGGER_PCT == 0.01
    assert RATCHET_STAGE_3_TRIGGER_PCT == 0.02


def test_locked_sector_cap():
    assert SECTOR_CAP == 2


def test_atr_multipliers_strictly_decrease():
    """Spec invariant: as profit grows, the trailing-stop noise allowance
    contracts (lock-in). Stage1 > Stage2 > Stage3."""
    assert (
        RATCHET_STAGE_1_ATR_MULT
        > RATCHET_STAGE_2_ATR_MULT
        > RATCHET_STAGE_3_ATR_MULT
    )
