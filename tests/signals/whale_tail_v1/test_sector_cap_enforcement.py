"""Sector cap is allocator-side; the signal merely uses BlockedReason.SECTOR_CAP_REACHED.

This test documents the contract: SECTOR_CAP_REACHED exists in the enum and
is reachable. The actual cap-enforcement happens in the allocator/harness,
not in the signal's filter chain.
"""

from __future__ import annotations

from driftpilot.signals.base import Candidate
from driftpilot.states import BlockedReason


def test_sector_cap_reached_value_present():
    assert BlockedReason.SECTOR_CAP_REACHED == "sector_cap_reached"


def test_signal_can_construct_blocked_candidate_for_sector_cap():
    """If a future allocator hook needs to surface SECTOR_CAP_REACHED via a
    Candidate, the dataclass must accept it."""
    c = Candidate(
        symbol="ABC",
        score=0.0,
        sector="Technology",
        allowed=False,
        blocked_reason=BlockedReason.SECTOR_CAP_REACHED,
        features={"sector": "Technology"},
    )
    assert c.allowed is False
    assert c.blocked_reason == BlockedReason.SECTOR_CAP_REACHED
    assert c.sector == "Technology"
