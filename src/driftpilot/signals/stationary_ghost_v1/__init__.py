"""Stationary Ghost v1 — intraday mean-reversion signal with ADX trend gate.

See README.md for thesis and KNOWN_RISKS.md for documented validation
concerns. Parameters are locked in config.py and must not be tuned within
a single backtest dataset.
"""

from __future__ import annotations

from driftpilot.signals.stationary_ghost_v1.config import (
    SIGNAL_NAME,
    SIGNAL_VERSION,
)
from driftpilot.signals.stationary_ghost_v1.signal import StationaryGhostV1Signal


__all__ = [
    "SIGNAL_NAME",
    "SIGNAL_VERSION",
    "StationaryGhostV1Signal",
]
