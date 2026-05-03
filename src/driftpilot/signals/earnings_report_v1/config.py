from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class EarningsReportConfig:
    max_hold_minutes: int = 60
    profit_take_pct: float = 1.0
    stop_loss_pct: float = 1.5
    max_event_age_minutes: int = 60
    # v3 directional gate: only candidates from events tagged with this
    # sentiment by Qwen will be admitted by `scan()`. Set to None to admit
    # all events (matches the spike behavior). Set to "positive" for the
    # validated GATED config (Jul-Dec 2024: edge_ratio=1.105, N=185).
    require_sentiment: str | None = "positive"
