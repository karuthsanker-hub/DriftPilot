"""Sector cap is allocator-side; this signal just exposes the constant.

The actual allocator integration lives in the runtime / state machine. We
pin the cap value here so a typo in config.py would be caught.
"""

from __future__ import annotations

from driftpilot.signals.rs_drift_v1.config import SECTOR_CAP


def test_sector_cap_constant_per_spec():
    """Locked spec: SECTOR_CAP = 2 (max 2 positions per GICS sector).
    Added in v1.1 — was not in original RS-Drift design (KNOWN_RISKS #7).
    """
    assert SECTOR_CAP == 2
