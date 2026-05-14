"""Verify the signal exposes the required Signal Protocol surface."""

from __future__ import annotations


import pytest

from driftpilot.catalyst.event_bus import CatalystEventBus
from driftpilot.signals.analyst_target_raise_v1 import (
    AnalystTargetRaiseConfig,
    AnalystTargetRaiseV1Signal,
    SIGNAL_NAME,
    SIGNAL_VERSION,
)


def _make_signal() -> AnalystTargetRaiseV1Signal:
    bus = CatalystEventBus()
    return AnalystTargetRaiseV1Signal(AnalystTargetRaiseConfig(), bus)


def test_signal_has_name_and_version() -> None:
    sig = _make_signal()
    assert sig.name == SIGNAL_NAME == "analyst_target_raise_v1"
    assert sig.version == SIGNAL_VERSION
    assert isinstance(sig.version, str) and sig.version


def test_signal_has_scan_callable() -> None:
    sig = _make_signal()
    assert callable(getattr(sig, "scan", None))


def test_signal_has_evaluate_exit_callable() -> None:
    sig = _make_signal()
    assert callable(getattr(sig, "evaluate_exit", None))


def test_default_config_values() -> None:
    cfg = AnalystTargetRaiseConfig()
    assert cfg.max_hold_minutes == 60
    assert cfg.profit_take_pct == 0.8
    assert cfg.stop_loss_pct == 1.0
    assert cfg.max_event_age_minutes == 240
    assert cfg.require_sentiment == "positive"


def test_constructor_requires_bus() -> None:
    with pytest.raises(ValueError):
        AnalystTargetRaiseV1Signal(AnalystTargetRaiseConfig(), None)
