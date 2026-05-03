"""Sector cap is allocator-side; this test pins the locked constant."""

from __future__ import annotations

from driftpilot.signals.apex_hunter_v2.config import SECTOR_CAP


def test_sector_cap_constant():
    """Locked spec: SECTOR_CAP = 2 (max 2 positions per GICS sector)."""
    assert SECTOR_CAP == 2
