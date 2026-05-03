"""analyst_target_raise_v1 — catalyst-driven signal package."""

from __future__ import annotations

from driftpilot.signals.analyst_target_raise_v1.config import (
    SIGNAL_NAME,
    SIGNAL_VERSION,
    AnalystTargetRaiseConfig,
)
from driftpilot.signals.analyst_target_raise_v1.signal import (
    AnalystTargetRaiseV1Signal,
)

__all__ = [
    "SIGNAL_NAME",
    "SIGNAL_VERSION",
    "AnalystTargetRaiseConfig",
    "AnalystTargetRaiseV1Signal",
]
