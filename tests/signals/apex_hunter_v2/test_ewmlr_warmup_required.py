"""EWMLR warm-up enforcement.

The Apex spec requires the first 10:30 ET cycle to be seeded with bars from
09:00 ET onward — calling EWMLR with insufficient history is a spec violation,
not a graceful-degraded mode. The function must raise ValueError.
"""

from __future__ import annotations

import pytest

from driftpilot.signals.apex_hunter_v2.features import calculate_ewmlr


def test_cold_start_raises_value_error():
    """At first cycle with no warm-up bars, EWMLR must raise."""
    with pytest.raises(ValueError, match="warm-up"):
        calculate_ewmlr([100.0, 100.5, 101.0], half_life_mins=15)


def test_just_under_warmup_raises():
    """half_life_mins=15 → minimum 30 prices."""
    prices = [100.0 + 0.1 * i for i in range(29)]
    with pytest.raises(ValueError, match="warm-up"):
        calculate_ewmlr(prices, half_life_mins=15)


def test_exactly_warmup_does_not_raise():
    prices = [100.0 + 0.1 * i for i in range(30)]
    slope, r2 = calculate_ewmlr(prices, half_life_mins=15)
    assert slope > 0
    assert 0.0 <= r2 <= 1.0


def test_negative_half_life_raises():
    with pytest.raises(ValueError, match="half_life_mins must be positive"):
        calculate_ewmlr([100.0] * 30, half_life_mins=0)
