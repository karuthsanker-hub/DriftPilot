"""Locked configuration for analyst_target_raise_v1.

Defaults derived from `reports/catalyst_horizons_midcap_2024.json`:
the (analyst, target_raise) cell shows 1.42x mean / median forward-return
ratio at the 60-minute horizon (N=104). The same cell fades to 0.97x by
the 1-day horizon, so the 60-minute hold cap is load-bearing — we MUST
exit before the edge evaporates.
"""

from __future__ import annotations

from dataclasses import dataclass


SIGNAL_NAME = "analyst_target_raise_v1"
SIGNAL_VERSION = "1.0.0"

# Bus subscription identifiers.
EVENT_CATEGORY = "analyst"
EVENT_SUBCATEGORY = "target_raise"


@dataclass(frozen=True, slots=True)
class AnalystTargetRaiseConfig:
    """Tunables for the analyst_target_raise_v1 signal.

    Defaults are the locked spec — do not "improve" them without
    re-running validation against the midcap 2024 horizon study.
    """

    max_hold_minutes: int = 60
    profit_take_pct: float = 0.8
    stop_loss_pct: float = 1.0
    max_event_age_minutes: int = 60


__all__ = [
    "SIGNAL_NAME",
    "SIGNAL_VERSION",
    "EVENT_CATEGORY",
    "EVENT_SUBCATEGORY",
    "AnalystTargetRaiseConfig",
]
