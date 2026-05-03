"""Verify CatalystUniverseFilter is wired into the state machine.

The SCANNING state currently delegates universe selection to a pluggable
`ScannerService.scan()`. The state machine exposes a thin
`apply_universe_filter()` hook that the operator runtime / scanner uses to
push the catalyst-filtered universe down to the four technical signals.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from driftpilot.catalyst.universe_filter import CatalystUniverseFilter
from driftpilot.settings import DriftPilotSettings
from driftpilot.state_machine import DriftPilotStateMachine


def _make_sm(uf=None) -> DriftPilotStateMachine:
    repo = MagicMock()
    repo.slots.list_all.return_value = []
    settings = DriftPilotSettings()
    return DriftPilotStateMachine(
        repository=repo,
        settings=settings,
        catalyst_universe_filter=uf,
    )


def test_no_filter_returns_input_unchanged():
    sm = _make_sm(None)
    assert sm.apply_universe_filter(["AAPL", "MSFT"]) == ["AAPL", "MSFT"]


def test_filter_invoked_when_present():
    fake = MagicMock(spec=CatalystUniverseFilter)
    fake.filter_and_rank.return_value = ["MSFT", "AAPL"]
    sm = _make_sm(fake)
    out = sm.apply_universe_filter(["AAPL", "MSFT", "BAD"])
    fake.filter_and_rank.assert_called_once_with(["AAPL", "MSFT", "BAD"])
    assert out == ["MSFT", "AAPL"]


def test_state_machine_accepts_filter_kwarg():
    """Constructor must accept catalyst_universe_filter without errors."""
    uf = CatalystUniverseFilter(db_path=None)
    sm = _make_sm(uf)
    assert sm.catalyst_universe_filter is uf
