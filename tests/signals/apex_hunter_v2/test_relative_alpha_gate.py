"""Relative alpha gate: ticker EWMLR slope must be ≥ 1.5 × SPY EWMLR slope."""

from __future__ import annotations

import math

import pytest

from driftpilot.signals.apex_hunter_v2.config import RELATIVE_ALPHA_MIN
from driftpilot.signals.apex_hunter_v2.features import relative_alpha


def test_relative_alpha_min_constant():
    assert RELATIVE_ALPHA_MIN == 1.5


def test_alpha_just_above_threshold_passes():
    # ticker slope = 1.6 × SPY slope → alpha = 1.6 ≥ 1.5
    alpha = relative_alpha(0.0016, 0.001)
    assert alpha >= RELATIVE_ALPHA_MIN


def test_alpha_just_below_threshold_fails():
    # ticker slope = 1.2 × SPY slope → alpha = 1.2 < 1.5
    alpha = relative_alpha(0.0012, 0.001)
    assert alpha < RELATIVE_ALPHA_MIN


def test_alpha_equal_to_spy():
    assert relative_alpha(0.001, 0.001) == pytest.approx(1.0, abs=1e-9)


def test_alpha_negative_ticker_against_positive_spy():
    """Ticker going down while SPY rises → negative alpha → blocked."""
    alpha = relative_alpha(-0.0008, 0.001)
    assert alpha < 0
    assert alpha < RELATIVE_ALPHA_MIN


def test_alpha_inf_when_spy_flat():
    """SPY slope == 0; positive ticker → +inf which trivially passes the
    minimum (semantically: 'infinitely strong relative drift')."""
    alpha = relative_alpha(0.001, 0.0)
    assert math.isinf(alpha)
    assert alpha > RELATIVE_ALPHA_MIN
