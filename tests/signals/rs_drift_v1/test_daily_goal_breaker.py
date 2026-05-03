"""Daily circuit breakers: +$125 profit cap, -$100 loss limit.

These run at the state-machine layer (not in evaluate_exit). The signal
itself just exposes the constants for the runtime to read. This test pins
the constants per the locked spec.
"""

from __future__ import annotations

from driftpilot.signals.rs_drift_v1.config import (
    DAILY_LOSS_LIMIT_USD,
    DAILY_PROFIT_TARGET_USD,
)


def test_daily_profit_target_constant():
    assert DAILY_PROFIT_TARGET_USD == 125


def test_daily_loss_limit_constant():
    assert DAILY_LOSS_LIMIT_USD == 100


def test_circuit_breakers_are_asymmetric():
    """Spec calls out the asymmetry intentionally (KNOWN_RISKS #4)."""
    assert DAILY_PROFIT_TARGET_USD > DAILY_LOSS_LIMIT_USD
