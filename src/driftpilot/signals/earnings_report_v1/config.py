from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class EarningsReportConfig:
    max_hold_minutes: int = 60
    profit_take_pct: float = 1.0
    stop_loss_pct: float = 1.5
    max_event_age_minutes: int = 60
