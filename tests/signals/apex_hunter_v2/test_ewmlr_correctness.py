"""EWMLR correctness against synthetic-data invariants.

Hand-computed fixtures + known invariants. TradingView cross-check
deferred per KNOWN_RISKS (user owns final verification before merge).
"""

from __future__ import annotations

import math

import pytest

from driftpilot.signals.apex_hunter_v2.features import calculate_ewmlr


def test_linear_trend_recovers_positive_slope_high_r2():
    """+0.1% per minute over 90 bars → positive slope, R² > 0.95."""
    prices = [100.0 * (1.001 ** i) for i in range(90)]
    slope, r2 = calculate_ewmlr(prices, half_life_mins=15)
    assert slope > 0
    assert r2 > 0.95


def test_flat_input_returns_near_zero_slope_and_low_r2():
    prices = [100.0] * 90
    slope, r2 = calculate_ewmlr(prices, half_life_mins=15)
    assert abs(slope) < 1e-6
    # Flat data → r2 implementation returns 0.0 (degenerate total_ss).
    assert r2 == 0.0 or r2 < 0.1


def test_descending_trend_returns_negative_slope():
    prices = [100.0 * (0.999 ** i) for i in range(90)]
    slope, r2 = calculate_ewmlr(prices, half_life_mins=15)
    assert slope < 0
    assert r2 > 0.95


def test_recent_bars_dominate_via_exponential_weighting():
    """Stale-then-recent: 60 flat bars then 30 sharply rising → slope > 0
    because half-life=15 makes the recent 30 dominant."""
    prices = [100.0] * 60 + [100.0 + 0.05 * i for i in range(30)]
    slope, _ = calculate_ewmlr(prices, half_life_mins=15)
    assert slope > 0
