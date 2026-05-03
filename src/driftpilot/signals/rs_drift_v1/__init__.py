"""RS-Drift v1.1 — relative-strength-vs-SPY drift signal."""

from __future__ import annotations

from driftpilot.signals.rs_drift_v1.config import SIGNAL_NAME, SIGNAL_VERSION
from driftpilot.signals.rs_drift_v1.signal import RsDriftV1Signal

__all__ = ["SIGNAL_NAME", "SIGNAL_VERSION", "RsDriftV1Signal"]
