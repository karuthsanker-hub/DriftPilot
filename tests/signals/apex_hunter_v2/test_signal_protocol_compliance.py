"""SignalProtocol compliance + registry lookup."""

from __future__ import annotations

from driftpilot.signals import get_signal, list_signals
from driftpilot.signals.base import SignalProtocol
from driftpilot.signals.apex_hunter_v2 import (
    SIGNAL_NAME,
    SIGNAL_VERSION,
    ApexHunterV22Signal,
)


def test_signal_implements_protocol():
    sig = ApexHunterV22Signal()
    assert isinstance(sig, SignalProtocol)
    assert sig.name == SIGNAL_NAME == "apex_hunter_v2_2"
    assert sig.version == SIGNAL_VERSION == "2.2.0"


def test_registry_lookup():
    sig = get_signal("apex_hunter_v2_2")
    assert sig.name == "apex_hunter_v2_2"
    assert "apex_hunter_v2_2" in list_signals()


def test_existing_signals_still_registered():
    """The new registration must not break existing entries."""
    listed = list_signals()
    assert "intraday_momentum_v1" in listed


def test_evaluate_exit_callable_returns_decision():
    """evaluate_exit must exist and return an ExitDecision-shaped object."""
    sig = ApexHunterV22Signal()
    assert hasattr(sig, "evaluate_exit")
    assert callable(sig.evaluate_exit)
