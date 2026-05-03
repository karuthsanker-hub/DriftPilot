"""Acceleration gate: positive when slope-history is rising, negative when falling."""

from __future__ import annotations

import pytest

from driftpilot.signals.apex_hunter_v2.features import calculate_acceleration


def test_rising_slope_history_positive_acceleration():
    history = [0.001 * i for i in range(20)]  # 0, 0.001, 0.002, ...
    assert calculate_acceleration(history, window=10) > 0


def test_falling_slope_history_negative_acceleration():
    history = [0.001 * (20 - i) for i in range(20)]  # decreasing
    assert calculate_acceleration(history, window=10) < 0


def test_flat_slope_history_zero_acceleration():
    history = [0.001] * 20
    assert calculate_acceleration(history, window=10) == pytest.approx(0.0, abs=1e-9)


def test_insufficient_history_raises():
    with pytest.raises(ValueError):
        calculate_acceleration([0.001] * 5, window=10)


def test_window_too_small_raises():
    with pytest.raises(ValueError):
        calculate_acceleration([0.001] * 20, window=1)
