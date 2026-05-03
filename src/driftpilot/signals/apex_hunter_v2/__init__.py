"""Apex Hunter v2.2 — institutional drift / EWMLR signal."""

from __future__ import annotations

from driftpilot.signals.apex_hunter_v2.config import SIGNAL_NAME, SIGNAL_VERSION
from driftpilot.signals.apex_hunter_v2.signal import ApexHunterV22Signal

__all__ = ["SIGNAL_NAME", "SIGNAL_VERSION", "ApexHunterV22Signal"]
